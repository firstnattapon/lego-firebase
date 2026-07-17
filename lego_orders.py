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


def summarize_order_result(place_response: dict) -> dict:
    """ไม่โม้ fill — FILLED เฉพาะ status ที่ยืนยัน filled จริง"""
    status = str(place_response.get("status", "")).upper()
    realized = status in {"FILLED", "PARTIALLY_FILLED"}
    return {
        "status": status or "UNKNOWN",
        "realized": realized,
        "note": "SUBMITTED/PENDING ไม่นับ realized — ยืนยัน FILLED จาก Trade Events (gRPC)",
    }
