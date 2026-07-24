"""Cloud Functions for time-aligned LEGO DNA and independent order execution.

lego_one_row: market clock -> snapshot -> model row -> durable outbox candidate.
lego_order_worker: dispatch/reconcile outbox intents without blocking DNA time.
"""
from __future__ import annotations

import os
import time
import traceback
from datetime import datetime, timedelta, timezone

import firebase_admin
import functions_framework
from firebase_admin import credentials, db

from lego_one_row import READY_BUY, READY_SELL, compute_row, dna_step_for
from lego_orders import (REALIZED_STATUSES, TERMINAL_STATUSES, UAT,
                         evaluate_submit_gate, normalize_status,
                         order_confirmation_phrase, summarize_order_result)
from lego_outbox import (expire_unsent_before, list_actionable, put_intent,
                         row_is_committed, update_intent)
from lego_state import (CalendarDriftError, SlotAlreadyConsumed, StaleAnchorError,
                        apply_realized_fill, chain_key, commit_final_row, make_run_id,
                        read_anchor, update_order_audit, write_order_audit)
from market_clock import (MarketClockError, fallback_slot_id, resolve_dna_step,
                          resolve_market_slot, slot_seconds)
from webull_io import (build_clients, build_order_payload, environment_label,
                       fetch_open_orders, fetch_order_detail, fetch_snapshot,
                       is_transient_exception, is_us_market_open, load_config,
                       place_market_order, preview_market_order)

ORDER_POLL_ATTEMPTS = 3
ORDER_POLL_DELAY_S = 2.0
UTC = timezone.utc


def _init_firebase():
    if not firebase_admin._apps:
        firebase_admin.initialize_app(
            credentials.ApplicationDefault(),
            {"databaseURL": os.environ["FIREBASE_DB_URL"]},
        )


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _poll_order_status(trade_client, client_order_id: str, place_res: dict) -> dict:
    detail = None
    for i in range(ORDER_POLL_ATTEMPTS):
        if i:
            time.sleep(ORDER_POLL_DELAY_S)
        detail = fetch_order_detail(trade_client, client_order_id)
        summary = summarize_order_result(place_res, detail)
        if normalize_status(summary.get("status")) in TERMINAL_STATUSES:
            return summary
    return summarize_order_result(place_res, detail)


def _apply_realized_if_available(intent: dict, summary: dict) -> dict:
    status = normalize_status(summary.get("status"))
    if status not in REALIZED_STATUSES:
        return summary
    qty = summary.get("filled_quantity")
    price = summary.get("filled_price")
    if qty is None or price is None:
        out = dict(summary)
        out["realized_warning"] = "fill confirmed but quantity/price unavailable"
        return out
    realized = apply_realized_fill(
        intent["chain_key"], intent["run_id"], intent["side"],
        cumulative_qty=float(qty), price=float(price),
        cumulative_fee=float(summary.get("filled_fee", 0.0) or 0.0),
    )
    out = dict(summary)
    out.update(realized)
    return out


def _persist_summary(intent: dict, summary: dict) -> None:
    run_id = intent["run_id"]
    status = normalize_status(summary.get("status"))
    fields = {**summary, "status": status}
    update_intent(intent["chain_key"], run_id, fields)
    update_order_audit(run_id, fields)


def _pending_row_shape(intent: dict) -> dict:
    return {
        "สถานะ": intent["row_status"],
        "สินทรัพย์": intent["symbol"],
        "_meta": {
            "side": intent["side"],
            "quantity": float(intent["quantity"]),
            "step": int(intent["step"]),
        },
    }


def _dispatch_or_reconcile_one(trade_client, data_client, cfg, intent: dict) -> dict:
    run_id = intent["run_id"]
    ck = intent["chain_key"]
    status = normalize_status(intent.get("status"))

    if not row_is_committed(run_id):
        update_intent(ck, run_id, {
            "status": "NOT_PLACED",
            "terminal_reason": "source row was not committed",
        })
        return {"run_id": run_id, "status": "NOT_PLACED"}

    if status in {"PLACING_UNKNOWN", "PLACING", "SUBMITTED", "UNKNOWN",
                  "PARTIAL_FILLED", "PARTIALLY_FILLED"}:
        try:
            summary = summarize_order_result({}, fetch_order_detail(trade_client, run_id))
            if normalize_status(summary.get("status")) == "UNKNOWN":
                raise RuntimeError("broker order detail still UNKNOWN")
            summary = _apply_realized_if_available(intent, summary)
            _persist_summary(intent, summary)
            return {"run_id": run_id, **summary}
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            update_intent(ck, run_id, {"status": "PLACING_UNKNOWN", "last_error": err[:500]})
            update_order_audit(run_id, {"status": "PLACING_UNKNOWN", "last_error": err[:500]})
            return {"run_id": run_id, "status": "PLACING_UNKNOWN", "error": err}

    if status != "PENDING_DISPATCH":
        return {"run_id": run_id, "status": status}

    now = datetime.now(UTC)
    expiry = datetime.fromisoformat(str(intent["expires_at"]).replace("Z", "+00:00"))
    if now >= expiry:
        update_intent(ck, run_id, {"status": "EXPIRED_UNSENT"})
        return {"run_id": run_id, "status": "EXPIRED_UNSENT"}

    open_orders = fetch_open_orders(trade_client, cfg.symbol)
    if open_orders:
        update_intent(ck, run_id, {
            "status": "SUPPRESSED_ACTIVE_ORDER",
            "terminal_reason": f"{len(open_orders)} active broker order(s)",
        })
        return {"run_id": run_id, "status": "SUPPRESSED_ACTIVE_ORDER"}

    fresh = fetch_snapshot(trade_client, data_client, cfg)
    decision_holdings = float(intent.get("decision_holdings", 0.0) or 0.0)
    tolerance = float(os.environ.get("LEGO_HOLDINGS_DRIFT_TOLERANCE", "0.000001"))
    drift = abs(float(fresh["holdings"]) - decision_holdings)
    if drift > tolerance:
        update_intent(ck, run_id, {
            "status": "SUPPRESSED_STATE_CHANGED",
            "holdings_drift": drift,
            "dispatch_holdings": fresh["holdings"],
        })
        return {"run_id": run_id, "status": "SUPPRESSED_STATE_CHANGED",
                "holdings_drift": drift}

    env = environment_label()
    if env != UAT:
        update_intent(ck, run_id, {"status": "NOT_PLACED", "terminal_reason": f"environment={env}"})
        return {"run_id": run_id, "status": "NOT_PLACED"}

    row = _pending_row_shape(intent)
    order = build_order_payload(cfg, intent["side"], float(intent["quantity"]), run_id)
    write_order_audit(run_id, {
        "run_id": run_id, "chain_key": ck, "side": intent["side"],
        "quantity": float(intent["quantity"]), "symbol": cfg.symbol,
        "environment": env, "status": "PENDING_DISPATCH", "realized": False,
        "placed_at": intent["created_at"],
    })
    try:
        preview_ok = preview_market_order(trade_client, order)
        evaluate_submit_gate(env, row, preview_ok,
                             order_confirmation_phrase(row), committed=True)
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        update_intent(ck, run_id, {"status": "NOT_PLACED", "last_error": err[:500]})
        update_order_audit(run_id, {"status": "NOT_PLACED", "last_error": err[:500]})
        return {"run_id": run_id, "status": "NOT_PLACED", "error": err}

    update_intent(ck, run_id, {"status": "PLACING_UNKNOWN", "place_attempted": True})
    update_order_audit(run_id, {"status": "PLACING_UNKNOWN", "place_attempted": True})
    try:
        place_res = place_market_order(trade_client, order)
        summary = _poll_order_status(trade_client, run_id, place_res)
        summary = _apply_realized_if_available(intent, summary)
        _persist_summary(intent, summary)
        return {"run_id": run_id, **summary}
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        update_intent(ck, run_id, {"status": "PLACING_UNKNOWN", "last_error": err[:500]})
        update_order_audit(run_id, {"status": "PLACING_UNKNOWN", "last_error": err[:500]})
        return {"run_id": run_id, "status": "PLACING_UNKNOWN", "error": err}


def _run_order_worker(cfg, limit: int = 3) -> dict:
    trade_client, data_client = build_clients()
    ck = chain_key(cfg)
    expire_unsent_before(ck, datetime.now(UTC))
    results = []
    for intent in list_actionable(ck, limit=limit):
        results.append(_dispatch_or_reconcile_one(trade_client, data_client, cfg, intent))
    return {"processed": len(results), "results": results}


@functions_framework.http
def lego_one_row(request):
    """Commit the current model slot first. Order failures never block DNA time."""
    _init_firebase()
    cfg = load_config()
    decision_time = datetime.now(UTC)

    try:
        # Mandatory: the slot grid must match the timeframe the DNA was trained
        # on, so a missing/unsupported value is a deploy error, not a runtime one.
        slot_seconds()
    except MarketClockError as exc:
        return {"status": "CONFIG_ERROR", "committed": False,
                "pipeline_status": "CONFIG_ERROR", "error": str(exc)}, 500

    if not is_us_market_open(decision_time):
        return {"status": "PASS_MARKET_CLOSED", "committed": False,
                "pipeline_status": "MARKET_CLOSED"}, 200

    try:
        anchor = read_anchor(cfg)
        legacy_step = dna_step_for(anchor)
        slot = None
        clock_error = None
        try:
            slot = resolve_market_slot(decision_time)
            if slot is None:
                return {"status": "PASS_MARKET_CLOSED", "committed": False,
                        "pipeline_status": "MARKET_CLOSED"}, 200
            effective_step, alignment_error = resolve_dna_step(legacy_step, slot)
        except MarketClockError as exc:
            if os.environ.get("LEGO_DNA_CLOCK_MODE", "shadow").lower() == "market":
                raise
            effective_step, alignment_error = legacy_step, None
            clock_error = str(exc)

        trade_client, data_client = build_clients()
        snapshot = fetch_snapshot(trade_client, data_client, cfg)
        clock_mode = os.environ.get("LEGO_DNA_CLOCK_MODE", "shadow").lower()
        if slot:
            slot_id = slot.slot_id
        else:
            # Degraded clock still gets a one-commit-per-slot key so a scheduler
            # retry cannot consume the same slot twice.
            slot_id = fallback_slot_id(snapshot["captured_at"])
            clock_mode = f"{clock_mode}:degraded"
        row = compute_row(cfg, snapshot, anchor, dna_step=effective_step)

        env = environment_label()
        auto = os.environ.get("AUTO_SUBMIT", "false").lower() == "true"
        should_submit = auto and env == UAT and row["สถานะ"] in (READY_BUY, READY_SELL)

        # Current commit implementation derives the same deterministic run_id used below.
        prospective_run_id = make_run_id(chain_key(cfg), None if anchor is None else anchor.version, snapshot)
        if should_submit and slot:
            margin = int(os.environ.get("LEGO_ORDER_EXPIRY_MARGIN_SECONDS", "15"))
            expires_at = max(slot.slot_start_utc, slot.slot_end_utc - timedelta(seconds=margin))
            put_intent(chain_key(cfg), prospective_run_id, {
                "status": "PENDING_DISPATCH",
                "row_status": row["สถานะ"], "side": row["_meta"]["side"],
                "quantity": row["_meta"]["quantity"], "symbol": cfg.symbol,
                "step": row["DNA step"], "signal": row["DNA signal"],
                "decision_price": snapshot["price"],
                "decision_holdings": snapshot["holdings"],
                "decision_time": _iso(decision_time),
                "created_at": snapshot["captured_at"],
                "slot_id": slot.slot_id,
                "slot_start_utc": _iso(slot.slot_start_utc),
                "slot_end_utc": _iso(slot.slot_end_utc),
                "expires_at": _iso(expires_at),
            })

        result = commit_final_row(
            cfg, snapshot, anchor, row, slot_id=slot_id, clock_mode=clock_mode,
            market_ordinal=None if slot is None else slot.market_ordinal)
        out = {
            "status": row["สถานะ"], "committed": result["committed"],
            "idempotent": result.get("idempotent", False),
            "run_id": result["run_id"], "version": result.get("version"),
            "step": row["DNA step"], "signal": row["DNA signal"],
            "model_acted": row["_meta"]["acted"],
            "pipeline_status": "ROW_COMMITTED",
            "clock_mode": clock_mode,
            "legacy_step": legacy_step,
            "market_step": None if slot is None else slot.market_ordinal,
            "alignment_error": alignment_error,
            "market_slot_id": None if slot is None else slot.slot_id,
        }
        if clock_error:
            out["clock_warning"] = clock_error

        # Off by default: dispatching inline adds broker latency to the DNA
        # invocation, which raises the odds of a scheduler timeout+retry.
        if os.environ.get("LEGO_INLINE_ORDER_WORKER", "false").lower() == "true":
            try:
                out["order_worker"] = _run_order_worker(cfg, limit=1)
            except Exception as exc:
                out["order_worker"] = {"processed": 0, "error": f"{type(exc).__name__}: {exc}"}
        return out, 200

    except SlotAlreadyConsumed as exc:
        return {"status": "PASS_SLOT_CONSUMED", "committed": False,
                "pipeline_status": "SLOT_CONSUMED", "note": str(exc)}, 200
    except StaleAnchorError as exc:
        return {"status": "STALE_ANCHOR", "committed": False,
                "pipeline_status": "STALE_ANCHOR", "note": str(exc)}, 409
    except CalendarDriftError as exc:
        return {"status": "CALENDAR_DRIFT", "committed": False,
                "pipeline_status": "CALENDAR_DRIFT", "note": str(exc)}, 409
    except Exception as exc:
        try:
            db.reference("webull_lego_errors").push({
                "error": str(exc), "type": type(exc).__name__,
                "trace": traceback.format_exc()[:2000],
            })
        except Exception:
            pass
        code = 503 if is_transient_exception(exc) else 500
        return {"status": "ERROR", "committed": False,
                "pipeline_status": "SNAPSHOT_OR_ENGINE_ERROR",
                "error": str(exc), "type": type(exc).__name__}, code


@functions_framework.http
def lego_order_worker(request):
    """Independent worker; schedule separately when inline mode is disabled."""
    _init_firebase()
    cfg = load_config()
    try:
        limit = int(os.environ.get("LEGO_ORDER_WORKER_LIMIT", "3"))
        return {"pipeline_status": "ORDER_WORKER_OK", **_run_order_worker(cfg, limit)}, 200
    except Exception as exc:
        return {"pipeline_status": "ORDER_WORKER_ERROR",
                "error": f"{type(exc).__name__}: {exc}"}, 503
