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
import traceback
import uuid

import firebase_admin
import functions_framework
from firebase_admin import credentials, db

from lego_one_row import READY_BUY, READY_SELL, compute_row
from lego_state import (SlotAlreadyConsumed, StaleAnchorError,
                        commit_final_row, read_anchor, write_order_audit)
from lego_orders import (UAT, SubmitGateError, evaluate_submit_gate,
                         order_confirmation_phrase, summarize_order_result)
from webull_io import (build_clients, build_order_payload, environment_label,
                       fetch_snapshot, is_us_market_open, load_config,
                       place_market_order, preview_market_order)


def _init_firebase():
    if not firebase_admin._apps:
        firebase_admin.initialize_app(
            credentials.ApplicationDefault(),
            {"databaseURL": os.environ["FIREBASE_DB_URL"]},
        )


def _log_error(exc: Exception) -> None:
    """best-effort log ทุก path (trade อย่าล้มเพราะ log ล้ม)"""
    try:
        db.reference("webull_lego_errors").push({
            "error": str(exc), "type": type(exc).__name__,
            "trace": traceback.format_exc()[:2000],
        })
    except Exception:
        pass


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

        # Step 1–17: snapshot -> engine (validate 17 คอลัมน์ในตัว)
        trade_client, data_client = build_clients()
        snapshot = fetch_snapshot(
            trade_client, data_client, cfg,
            fallback_holdings=(anchor.prev_holdings if anchor else None))
        row = compute_row(cfg, snapshot, anchor)

        # Step 18: commit (idempotent + stale-anchor + monotonic + slot guard)
        result = commit_final_row(cfg, snapshot, anchor, row)

        out = {
            "status": row["สถานะ"], "committed": result["committed"],
            "idempotent": result.get("idempotent", False),
            "run_id": result["run_id"], "version": result.get("version"),
            "step": row["DNA step"], "signal": row["DNA signal"],
        }

        # ---- order path: preview -> gate -> place (ลำดับนี้เท่านั้น) ----------
        # แถว commit สำเร็จแล้ว = deliverable หลักของรอบนี้ — order ล้ม/ถูก block
        # ต้องตอบ 200 เสมอ มิฉะนั้น scheduler เห็น 5xx แล้ว retry จะสร้างแถวใหม่
        # กิน DNA slot เกิน (ขัดเจตนา slot ละ 1 แถว); gap ที่พลาดรอบนี้
        # รอบถัดไปคำนวณใหม่เองอยู่แล้ว (self-correcting)
        env = environment_label()
        auto = os.environ.get("AUTO_SUBMIT", "false").lower() == "true"
        if (auto and result["committed"]
                and row["สถานะ"] in (READY_BUY, READY_SELL)):
            try:
                if env != UAT:   # กันยิง preview ใส่ Production (invariant #9: read-only)
                    raise SubmitGateError(
                        f"ส่ง order ได้เฉพาะ {UAT}; ปัจจุบัน={env} (Production read-only)")
                m = row["_meta"]
                client_order_id = uuid.uuid4().hex
                order = build_order_payload(cfg, m["side"], m["quantity"], client_order_id)

                preview_ok = preview_market_order(trade_client, order)   # 1) preview
                evaluate_submit_gate(env, row, preview_ok,               # 2) gate (raise = จบ)
                                     order_confirmation_phrase(row), committed=True)
                res = place_market_order(trade_client, order)            # 3) place

                summary = summarize_order_result(res)
                write_order_audit(client_order_id, {
                    "run_id": result["run_id"], "side": m["side"],
                    "quantity": m["quantity"], "symbol": cfg.symbol,
                    "environment": env, **summary,
                })
                out["order"] = {"client_order_id": client_order_id, **summary}
            except SubmitGateError as exc:
                out["order"] = {"blocked": True, "note": str(exc)}
            except Exception as exc:  # noqa: BLE001 — order ล้มห้ามพารอบทั้งรอบล้ม
                _log_error(exc)
                out["order"] = {"error": str(exc), "type": type(exc).__name__}

        return out, 200

    except SlotAlreadyConsumed as exc:
        # scheduler retry ใน slot เดิม — ไม่ใช่ error จริง
        return {"status": "PASS_SLOT_CONSUMED", "committed": False, "note": str(exc)}, 200
    except StaleAnchorError as exc:
        return {"status": "STALE_ANCHOR", "committed": False,
                "note": f"{exc} — restart Step 0 รอบถัดไป"}, 409
    except Exception as exc:  # noqa: BLE001
        _log_error(exc)
        return {"status": "ERROR", "error": str(exc), "type": type(exc).__name__}, 500
