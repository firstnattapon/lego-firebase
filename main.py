"""main.py — Google Cloud Function entrypoint (Gen2, Python)

Cloud Scheduler --(HTTP/OIDC)--> ฟังก์ชันนี้ = "one new row" หนึ่งรอบ (Step 0–18)
order path ลำดับบังคับ (invariant #9): preview -> evaluate_submit_gate -> place
gate ไม่ผ่าน = ไม่มี order หลุดออกไปเด็ดขาด (fail closed)

หมายเหตุ AUTO_SUBMIT: ระบบ derive confirmation phrase เอง -> ชั้นป้องกัน phrase
เป็น formality; ที่เหลือของ gate (UAT-only + READY_* + committed + preview) ยังคุมจริง

ENV (map จาก Secret Manager):
  FIREBASE_DB_URL, WEBULL_APP_KEY, WEBULL_APP_SECRET, WEBULL_ACCOUNT_ID,
  WEBULL_ENV(UAT|PROD), LEGO_SYMBOL, LEGO_FIX_C, LEGO_DIFF, LEGO_DNA_CODE,
  LEGO_STRATEGY_ID, LEGO_DECIMAL_PRECISION, LEGO_SLOT_SECONDS, AUTO_SUBMIT(false)
"""
from __future__ import annotations

import os
import time
import traceback

import firebase_admin
import functions_framework
from firebase_admin import credentials, db

from lego_one_row import READY_BUY, READY_SELL, compute_row
from lego_state import (SlotAlreadyConsumed, StaleAnchorError, chain_key,
                        commit_final_row, pending_audits, read_anchor,
                        update_order_audit, write_order_audit)
from lego_orders import (TERMINAL_STATUSES, UAT, evaluate_submit_gate,
                         order_confirmation_phrase, summarize_order_result)
from webull_io import (build_clients, build_order_payload, environment_label,
                       fetch_open_orders, fetch_order_detail, fetch_snapshot,
                       is_us_market_open, load_config, place_market_order,
                       preview_market_order)

ORDER_POLL_ATTEMPTS = 3
ORDER_POLL_DELAY_S = 2.0


def _poll_order_status(trade_client, client_order_id: str, place_res: dict) -> dict:
    """poll get_order_detail จน status terminal หรือครบโควตา — สถานะจริง ไม่เดา"""
    detail = None
    for i in range(ORDER_POLL_ATTEMPTS):
        if i:
            time.sleep(ORDER_POLL_DELAY_S)
        detail = fetch_order_detail(trade_client, client_order_id)
        summary = summarize_order_result(place_res, detail)
        if summary["status"] in TERMINAL_STATUSES:
            return summary
    return summarize_order_result(place_res, detail)


def _reconcile_pending_orders(trade_client) -> None:
    """audit ค้าง (PLACING/UNKNOWN/SUBMITTED/PARTIAL_FILLED) -> เช็คสถานะจริงแล้ว update
    best-effort: ห้ามทำให้รอบล้ม; NOT_PLACED = จบแบบ local (gate ปัดตกก่อน place)"""
    for event_id, payload in pending_audits(TERMINAL_STATUSES | {"NOT_PLACED"}).items():
        try:
            detail = fetch_order_detail(trade_client, event_id)
            summary = summarize_order_result({}, detail)
            if summary["status"] != "UNKNOWN":
                update_order_audit(event_id, summary)
        except Exception:
            continue


def _init_firebase():
    if not firebase_admin._apps:
        firebase_admin.initialize_app(
            credentials.ApplicationDefault(),
            {"databaseURL": os.environ["FIREBASE_DB_URL"]},
        )


@functions_framework.http
def lego_one_row(request):
    """entrypoint (HTTP). คืน JSON สรุปผลหนึ่งรอบ"""
    _init_firebase()
    cfg = load_config()

    if not is_us_market_open():
        return {"status": "PASS_MARKET_CLOSED", "committed": False}, 200

    try:
        # Step 0: อ่าน anchor ล่าสุด (1 แถวเท่านั้น — invariant #1)
        anchor = read_anchor(cfg)

        trade_client, data_client = build_clients()

        # reconcile order ค้างจากรอบก่อน "ก่อน" สร้างแถว — audit ได้สถานะจริง
        # (observability เท่านั้น: ledger ΔAₙ/Aₙ เป็นทฤษฎีแบบ gated ไม่ใช้ fill)
        try:
            _reconcile_pending_orders(trade_client)
        except Exception:
            pass

        # Step 1–17: snapshot -> engine (validate 17 คอลัมน์ในตัว)
        snapshot = fetch_snapshot(trade_client, data_client, cfg)
        row = compute_row(cfg, snapshot, anchor)

        # Step 18: commit (idempotent + stale-anchor + monotonic + slot guard)
        result = commit_final_row(cfg, snapshot, anchor, row)

        out = {
            "status": row["สถานะ"], "committed": result["committed"],
            "idempotent": result.get("idempotent", False),
            "run_id": result["run_id"], "version": result.get("version"),
            "step": row["DNA step"], "signal": row["DNA signal"],
        }

        # ---- order path: env-gate -> preview -> gate -> place (ลำดับนี้เท่านั้น) ----
        env = environment_label()
        auto = os.environ.get("AUTO_SUBMIT", "false").lower() == "true"
        if (auto and result["committed"]
                and row["สถานะ"] in (READY_BUY, READY_SELL)):
            # invariant #9: Preview/Submit = UAT เท่านั้น — Production ห้ามแม้แต่ preview
            if env != UAT:
                out["order_skipped"] = f"environment={env} — read-only, ไม่ preview/place"
                return out, 200
            # order ล้ม/gate ไม่ผ่าน ห้ามทำให้ทั้งรอบเป็น 500 — แถว commit ไปแล้ว
            # (500 -> Scheduler retry -> สร้างแถวใหม่ = กิน DNA slot เกิน 1 ต่อรอบ)
            # client_order_id = run_id: deterministic 1 order/แถว — broker ปัดซ้ำเองถ้า retry
            client_order_id = result["run_id"]
            audit_written = False
            place_attempted = False
            try:
                # guard กัน order ซ้อน: ตัวเก่ายังค้างอยู่ -> ไม่ยิงเพิ่มรอบนี้
                open_orders = fetch_open_orders(trade_client, cfg.symbol)
                if open_orders:
                    out["order_skipped"] = (
                        f"open order ค้าง {len(open_orders)} ตัว — ข้ามการส่งรอบนี้ (กันซ้อน)")
                    return out, 200

                m = row["_meta"]
                order = build_order_payload(cfg, m["side"], m["quantity"], client_order_id)

                # write-ahead audit ก่อน place — crash หลัง place แล้ว order ต้องไม่ล่องหน
                # chain_key/placed_at: ผูก fill เข้า realized ledger ของ chain นี้เท่านั้น
                write_order_audit(client_order_id, {
                    "run_id": result["run_id"], "side": m["side"],
                    "quantity": m["quantity"], "symbol": cfg.symbol,
                    "environment": env, "status": "PLACING", "realized": False,
                    "chain_key": chain_key(cfg),
                    "placed_at": snapshot["captured_at"],
                })
                audit_written = True

                preview_ok = preview_market_order(trade_client, order)   # 1) preview
                evaluate_submit_gate(env, row, preview_ok,               # 2) gate (raise = จบ)
                                     order_confirmation_phrase(row), committed=True)
                place_attempted = True
                res = place_market_order(trade_client, order)            # 3) place

                # 4) ตามสถานะจริง — place v3 response ไม่มี status
                summary = _poll_order_status(trade_client, client_order_id, res)
                update_order_audit(client_order_id, summary)
                out["order"] = {"client_order_id": client_order_id, **summary}
            except Exception as order_exc:
                err = f"{type(order_exc).__name__}: {order_exc}"
                try:
                    if audit_written and not place_attempted:
                        # ยังไม่ได้ยิง place แน่นอน -> ปิด audit แบบ local
                        update_order_audit(client_order_id, {
                            "status": "NOT_PLACED", "realized": False, "error": err[:500]})
                    elif audit_written:
                        # ยิงไปแล้ว/ก้ำกึ่ง -> คง PLACING ให้ reconcile รอบถัดไปตามผลจริง
                        update_order_audit(client_order_id, {"error": err[:500]})
                except Exception:
                    pass
                try:
                    db.reference("webull_lego_errors").push({
                        "error": str(order_exc), "type": type(order_exc).__name__,
                        "phase": "order_path", "run_id": result["run_id"],
                    })
                except Exception:
                    pass
                out["order_error"] = err

        return out, 200

    except SlotAlreadyConsumed as exc:
        # scheduler retry ใน slot เดิม — ไม่ใช่ error จริง
        return {"status": "PASS_SLOT_CONSUMED", "committed": False, "note": str(exc)}, 200
    except StaleAnchorError as exc:
        return {"status": "STALE_ANCHOR", "committed": False,
                "note": f"{exc} — restart Step 0 รอบถัดไป"}, 409
    except Exception as exc:  # best-effort log ทุก path (trade อย่าล้มเพราะ log ล้ม)
        try:
            db.reference("webull_lego_errors").push({
                "error": str(exc), "type": type(exc).__name__,
                "trace": traceback.format_exc()[:2000],
            })
        except Exception:
            pass
        return {"status": "ERROR", "error": str(exc), "type": type(exc).__name__}, 500
