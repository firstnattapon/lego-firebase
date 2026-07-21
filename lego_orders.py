"""lego_orders.py — เส้นทาง order UAT (invariant #9, #10)

- Preview/Submit ใช้ได้เฉพาะ environment "Test (UAT)" และเฉพาะแถว READY_BUY/READY_SELL
  ที่ persist แล้ว (Step 18 committed)
- evaluate_submit_gate fail-closed: payload valid + preview ตรง + confirmation phrase ตรง
  + ไม่ใช่ Production
- summarize_order_result: เรียก FILLED เฉพาะ filled ชัดเจน — SUBMITTED/PENDING ไม่นับ realized
"""
from __future__ import annotations

from lego_one_row import READY_BUY, READY_SELL

UAT = "Test (UAT)"
PROD = "Production"


class SubmitGateError(RuntimeError):
    """submit gate ไม่ผ่าน -> ไม่ส่ง order"""


def order_confirmation_phrase(row: dict) -> str:
    """วลียืนยันที่ผูกกับ side/qty/symbol/step — ต้องพิมพ์ตรงถึงจะ submit ได้"""
    m = row["_meta"]
    return f"CONFIRM {m['side']} {m['quantity']} {row['สินทรัพย์']} STEP {m['step']}"


def evaluate_submit_gate(environment: str, row: dict, preview_ok: bool,
                         confirmation_input: str, committed: bool) -> None:
    """ทุกเงื่อนไขต้องผ่าน มิฉะนั้น raise (fail closed)"""
    if environment != UAT:
        raise SubmitGateError(f"ส่ง order ได้เฉพาะ {UAT}; ปัจจุบัน={environment} (Production read-only)")
    if not committed:
        raise SubmitGateError("แถวยังไม่ persist (Step 18) — ห้าม submit")
    if row["สถานะ"] not in (READY_BUY, READY_SELL):
        raise SubmitGateError(f"สถานะ {row['สถานะ']} ไม่ใช่ READY_* — ไม่ส่ง")
    m = row["_meta"]
    if not (m["quantity"] and m["quantity"] > 0):
        raise SubmitGateError("quantity <= 0 — ไม่ส่ง")
    if not preview_ok:
        raise SubmitGateError("preview ไม่ผ่าน/ไม่ตรง — ไม่ส่ง")
    if confirmation_input != order_confirmation_phrase(row):
        raise SubmitGateError("confirmation phrase ไม่ตรง — ไม่ส่ง")


# status terminal = จบแล้ว ไม่ต้องตามต่อ; realized = มีของเข้าพอร์ตจริง (invariant #10)
# PARTIAL_FILLED = realized แต่ "ไม่ terminal" — ส่วนที่เหลือยัง fill/cancel ต่อได้
# ต้อง reconcile ตามต่อจนได้ FILLED/CANCELLED/EXPIRED จริง
REALIZED_STATUSES = {"FILLED", "PARTIAL_FILLED", "PARTIALLY_FILLED"}
TERMINAL_STATUSES = {"FILLED", "CANCELLED", "FAILED", "REJECTED", "EXPIRED"}


def _normalize_status(raw) -> str:
    # SDK enum ใช้ "PARTIAL FILLED" (มีช่องว่าง) — normalize เป็น underscore
    return str(raw or "").strip().upper().replace(" ", "_")


def _order_fields(detail) -> dict:
    """ดึง order fields จาก get_order_detail ทุก shape: dict ตรง / nest ใน items/orders"""
    if isinstance(detail, list):
        detail = detail[0] if detail else {}
    if not isinstance(detail, dict):
        return {}
    for key in ("items", "orders", "data"):
        inner = detail.get(key)
        if isinstance(inner, list) and inner and isinstance(inner[0], dict):
            merged = dict(detail)
            merged.update(inner[0])
            return merged
        if isinstance(inner, dict):
            merged = dict(detail)
            merged.update(inner)
            return merged
    return detail


def summarize_order_result(place_response: dict, detail: dict | None = None) -> dict:
    """ไม่โม้ fill — FILLED เฉพาะ status ที่ยืนยัน filled จริง
    detail (จาก get_order_detail) เป็นแหล่งสถานะหลัก — place response v3 ไม่มี status"""
    fields = _order_fields(detail) if detail else {}
    status = _normalize_status(
        fields.get("order_status") or fields.get("status")
        or (place_response or {}).get("order_status")
        or (place_response or {}).get("status"))
    realized = status in REALIZED_STATUSES
    out = {
        "status": status or "UNKNOWN",
        "realized": realized,
        "note": "SUBMITTED/PENDING ไม่นับ realized — ยืนยันจาก get_order_detail",
    }
    filled = fields.get("filled_quantity") or fields.get("filled_qty")
    if filled is not None:
        out["filled_quantity"] = filled
    reason = (fields.get("reason") or fields.get("message")
              or fields.get("error_msg") or fields.get("reject_reason"))
    if reason:
        out["reject_reason"] = str(reason)
    return out
