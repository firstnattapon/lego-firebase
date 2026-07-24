"""Google Cloud Function entrypoint for one LEGO row plus durable order outbox.

Pipeline status is explicit:
- ROW_COMMITTED: row persisted, no broker action required.
- ROW_COMMITTED_ORDER_FILLED/TERMINAL: broker path reached a terminal state.
- ORDER_PENDING: broker accepted/non-terminal; durable state will reconcile later.
- ORDER_RETRY_REQUIRED: uncertain/transient failure; HTTP 503, intent remains durable.
"""
from __future__ import annotations

import os
import time
import traceback

import firebase_admin
import functions_framework
from firebase_admin import credentials, db

from lego_one_row import READY_BUY, READY_SELL, compute_row
from lego_orders import (REALIZED_STATUSES, TERMINAL_STATUSES, UAT,
                         evaluate_submit_gate, normalize_status,
                         order_confirmation_phrase, summarize_order_result)
from lego_state import (PendingOrderExists, SlotAlreadyConsumed, StaleAnchorError,
                        apply_realized_fill, chain_key, clear_pending_order,
                        commit_final_row, get_pending_order, read_anchor,
                        update_order_audit, update_pending_order,
                        write_order_audit)
from webull_io import (build_clients, build_order_payload, environment_label,
                       fetch_open_orders, fetch_order_detail, fetch_snapshot,
                       is_transient_exception, is_us_market_open, load_config,
                       place_market_order, preview_market_order)

ORDER_POLL_ATTEMPTS = 3
ORDER_POLL_DELAY_S = 2.0


def _init_firebase():
    if not firebase_admin._apps:
        firebase_admin.initialize_app(
            credentials.ApplicationDefault(),
            {"databaseURL": os.environ["FIREBASE_DB_URL"]},
        )


def _poll_order_status(trade_client, client_order_id: str, place_res: dict) -> dict:
    detail = None
    for i in range(ORDER_POLL_ATTEMPTS):
        if i:
            time.sleep(ORDER_POLL_DELAY_S)
        detail = fetch_order_detail(trade_client, client_order_id)
        summary = summarize_order_result(place_res, detail)
        if summary["status"] in TERMINAL_STATUSES:
            return summary
    return summarize_order_result(place_res, detail)


def _pending_row_shape(pending: dict) -> dict:
    return {
        "สถานะ": pending["row_status"],
        "สินทรัพย์": pending["symbol"],
        "_meta": {
            "side": pending["side"],
            "quantity": float(pending["quantity"]),
            "step": int(pending["step"]),
        },
    }


def _apply_realized_if_available(cfg, pending: dict, summary: dict) -> dict:
    status = normalize_status(summary.get("status"))
    if status not in REALIZED_STATUSES:
        return summary
    qty = summary.get("filled_quantity")
    price = summary.get("filled_price")
    if qty is None or price is None:
        summary = dict(summary)
        summary["realized_warning"] = (
            "broker ยืนยัน fill แต่ยังไม่มี filled_quantity/filled_price — ยังไม่นับ realized")
        return summary
    realized = apply_realized_fill(
        pending["chain_key"], pending["run_id"], pending["side"],
        cumulative_qty=float(qty), price=float(price),
        cumulative_fee=float(summary.get("filled_fee", 0.0) or 0.0),
    )
    summary = dict(summary)
    summary.update(realized)
    return summary


def _persist_order_summary(cfg, pending: dict, summary: dict) -> None:
    run_id = pending["run_id"]
    update_order_audit(run_id, summary)
    status = normalize_status(summary.get("status"))
    update_pending_order(cfg, run_id, {**summary, "status": status})
    if status in TERMINAL_STATUSES:
        clear_pending_order(cfg, run_id)


def _reconcile_existing_pending(trade_client, cfg, pending: dict) -> tuple[dict, int]:
    run_id = pending["run_id"]
    try:
        detail = fetch_order_detail(trade_client, run_id)
        summary = summarize_order_result({}, detail)
        if summary["status"] == "UNKNOWN":
            raise RuntimeError("broker order detail ยัง UNKNOWN")
        summary = _apply_realized_if_available(cfg, pending, summary)
        _persist_order_summary(cfg, pending, summary)
        status = normalize_status(summary["status"])
        if status in TERMINAL_STATUSES:
            return {"pipeline_status": "ORDER_RECONCILED_TERMINAL", "order": summary}, 200
        return {"pipeline_status": "ORDER_PENDING", "order": summary}, 202
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        update_pending_order(cfg, run_id, {"status": pending.get("status", "PLACING"),
                                           "last_error": err[:500]})
        update_order_audit(run_id, {"last_error": err[:500]})
        return {"pipeline_status": "ORDER_RETRY_REQUIRED", "run_id": run_id,
                "error": err}, 503


def _dispatch_pending(trade_client, cfg, pending: dict) -> tuple[dict, int]:
    run_id = pending["run_id"]
    env = environment_label()
    if env != UAT:
        err = f"environment={env}; pending order ส่งได้เฉพาะ UAT"
        update_pending_order(cfg, run_id, {"status": "PENDING", "last_error": err})
        return {"pipeline_status": "ORDER_RETRY_REQUIRED", "run_id": run_id,
                "error": err}, 503

    try:
        open_orders = fetch_open_orders(trade_client, cfg.symbol)
        if open_orders:
            update_pending_order(cfg, run_id, {
                "status": "PENDING",
                "last_error": f"open order ค้าง {len(open_orders)} ตัว",
            })
            return {"pipeline_status": "ORDER_BLOCKED_OPEN_ORDER", "run_id": run_id,
                    "open_orders": len(open_orders)}, 202

        row = _pending_row_shape(pending)
        order = build_order_payload(cfg, pending["side"], float(pending["quantity"]), run_id)
        write_order_audit(run_id, {
            "run_id": run_id,
            "chain_key": pending["chain_key"],
            "side": pending["side"],
            "quantity": float(pending["quantity"]),
            "symbol": pending["symbol"],
            "environment": env,
            "status": "PENDING",
            "realized": False,
            "placed_at": pending["created_at"],
        })

        preview_ok = preview_market_order(trade_client, order)
        evaluate_submit_gate(env, row, preview_ok,
                             order_confirmation_phrase(row), committed=True)
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        update_pending_order(cfg, run_id, {"status": "PENDING", "last_error": err[:500]})
        update_order_audit(run_id, {"status": "PENDING", "last_error": err[:500]})
        return {"pipeline_status": "ORDER_RETRY_REQUIRED", "run_id": run_id,
                "phase": "PRE_PLACE", "error": err}, 503

    update_pending_order(cfg, run_id, {"status": "PLACING", "place_attempted": True})
    update_order_audit(run_id, {"status": "PLACING", "place_attempted": True})
    try:
        place_res = place_market_order(trade_client, order)
        summary = _poll_order_status(trade_client, run_id, place_res)
        summary = _apply_realized_if_available(cfg, pending, summary)
        _persist_order_summary(cfg, pending, summary)
        status = normalize_status(summary["status"])
        if status in TERMINAL_STATUSES:
            return {"pipeline_status": "ROW_COMMITTED_ORDER_TERMINAL",
                    "run_id": run_id, "order": summary}, 200
        return {"pipeline_status": "ORDER_PENDING", "run_id": run_id,
                "order": summary}, 202
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        update_pending_order(cfg, run_id, {"status": "PLACING", "last_error": err[:500]})
        update_order_audit(run_id, {"status": "PLACING", "last_error": err[:500]})
        return {"pipeline_status": "ORDER_RETRY_REQUIRED", "run_id": run_id,
                "phase": "PLACE_OR_POLL", "error": err}, 503


def _process_pending_order(trade_client, cfg, pending: dict) -> tuple[dict, int]:
    status = normalize_status(pending.get("status"))
    if status in {"PLACING", "SUBMITTED", "PENDING_SUBMIT", "UNKNOWN",
                  "PARTIAL_FILLED", "PARTIALLY_FILLED"}:
        return _reconcile_existing_pending(trade_client, cfg, pending)
    return _dispatch_pending(trade_client, cfg, pending)


def _build_order_intent(cfg, row: dict, captured_at: str) -> dict:
    m = row["_meta"]
    return {
        "status": "PENDING",
        "row_status": row["สถานะ"],
        "side": m["side"],
        "quantity": m["quantity"],
        "symbol": cfg.symbol,
        "step": m["step"],
        "created_at": captured_at,
    }


@functions_framework.http
def lego_one_row(request):
    _init_firebase()
    cfg = load_config()
    if not is_us_market_open():
        return {"status": "PASS_MARKET_CLOSED", "committed": False,
                "pipeline_status": "MARKET_CLOSED"}, 200

    try:
        trade_client, data_client = build_clients()

        pending = get_pending_order(cfg)
        if pending:
            pending_out, pending_code = _process_pending_order(trade_client, cfg, pending)
            if pending_code != 200:
                return pending_out, pending_code

        anchor = read_anchor(cfg)
        snapshot = fetch_snapshot(trade_client, data_client, cfg)
        row = compute_row(cfg, snapshot, anchor)

        env = environment_label()
        auto = os.environ.get("AUTO_SUBMIT", "false").lower() == "true"
        should_submit = auto and env == UAT and row["สถานะ"] in (READY_BUY, READY_SELL)
        intent = _build_order_intent(cfg, row, snapshot["captured_at"]) if should_submit else None

        result = commit_final_row(cfg, snapshot, anchor, row, order_intent=intent)
        out = {
            "status": row["สถานะ"],
            "committed": result["committed"],
            "idempotent": result.get("idempotent", False),
            "run_id": result["run_id"],
            "version": result.get("version"),
            "step": row["DNA step"],
            "signal": row["DNA signal"],
            "model_acted": row["_meta"]["acted"],
            "pipeline_status": "ROW_COMMITTED",
        }

        if auto and env != UAT and row["สถานะ"] in (READY_BUY, READY_SELL):
            out["pipeline_status"] = "ROW_COMMITTED_ORDER_DISABLED_PRODUCTION"
            out["order_skipped"] = f"environment={env} — read-only"
            return out, 200

        if intent and result["committed"]:
            pending = get_pending_order(cfg)
            if not pending:
                return {**out, "pipeline_status": "ORDER_OUTBOX_MISSING"}, 500
            order_out, order_code = _dispatch_pending(trade_client, cfg, pending)
            return {**out, **order_out}, order_code

        return out, 200

    except SlotAlreadyConsumed as exc:
        return {"status": "PASS_SLOT_CONSUMED", "committed": False,
                "pipeline_status": "SLOT_CONSUMED", "note": str(exc)}, 200
    except PendingOrderExists as exc:
        return {"status": "PENDING_ORDER_EXISTS", "committed": False,
                "pipeline_status": "ORDER_PENDING", "note": str(exc)}, 202
    except StaleAnchorError as exc:
        return {"status": "STALE_ANCHOR", "committed": False,
                "pipeline_status": "STALE_ANCHOR", "note": str(exc)}, 409
    except Exception as exc:
        try:
            db.reference("webull_lego_errors").push({
                "error": str(exc),
                "type": type(exc).__name__,
                "trace": traceback.format_exc()[:2000],
            })
        except Exception:
            pass
        code = 503 if is_transient_exception(exc) else 500
        return {"status": "ERROR", "committed": False,
                "pipeline_status": "SNAPSHOT_OR_ENGINE_ERROR",
                "error": str(exc), "type": type(exc).__name__}, code
