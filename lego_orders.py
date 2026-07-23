"""lego_orders.py — เส้นทาง order UAT (invariant #9, #10)

- Preview/Submit ใช้ได้เฉพาะ environment "Test (UAT)" และเฉพาะแถว READY_BUY/READY_SELL
  ที่ persist แล้ว (Step 18 committed)
- evaluate_submit_gate fail-closed: payload valid + preview ตรง + confirmation phrase ตรง
  + ไม่ใช่ Production
- summarize_order_result: เรียก FILLED เฉพาะ filled ชัดเจน — SUBMITTED/PENDING ไม่นับ realized
  พร้อมดึง execution facts (filled_price/filled_fee) เมื่อ broker ส่งมา — ห้ามใช้ราคา quote แทน
  (facts เก็บลง audit เพื่อ observability; ledger ΔAₙ/Aₙ เป็นทฤษฎีแบบ gated
  ใน lego_one_row ไม่ได้ใช้ fill — ดู gated_theoretical_v2)
"""
from __future__ import annotations

import math

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

# ราคา execute จริงเท่านั้น — เจตนาไม่มี last_price/price (นั่นคือ quote ตอนตัดสินใจ
# พิสูจน์เงินที่ broker แลกจริงไม่ได้; ชุดชื่อเดียวกับ Webull_Dashboard/trade_log.py)
EXECUTION_PRICE_FIELDS = (
    "average_filled_price", "avg_filled_price", "average_fill_price",
    "avg_fill_price", "filled_price", "fill_price", "executed_price",
    "execution_price",
)
FEE_FIELDS = ("transaction_fee", "filled_fee", "execution_fee", "commission", "fee")


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


def _coalesce_float(fields: dict, names: tuple[str, ...],
                    minimum_exclusive: float | None = None) -> float | None:
    """ค่าตัวเลขแรกที่ parse ได้และผ่านเกณฑ์ (string จาก API -> float)"""
    for name in names:
        value = fields.get(name)
        if value is None or value == "":
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(number):
            continue
        if minimum_exclusive is not None and not (number > minimum_exclusive):
            continue
        if minimum_exclusive is None and number < 0:
            continue
        return number
    return None


def summarize_order_result(place_response: dict, detail: dict | None = None) -> dict:
    """ไม่โม้ fill — FILLED เฉพาะ status ที่ยืนยัน filled จริง
    detail (จาก get_order_detail) เป็นแหล่งสถานะหลัก — place response v3 ไม่มี status
    execution facts (filled_price/filled_fee) ใส่เฉพาะเมื่อ broker ส่งมาจริง
    — ไม่มี = ไม่ใส่ (ledger จะ fail closed ไม่นับจนกว่าจะมีราคา execute)"""
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
    price = _coalesce_float(fields, EXECUTION_PRICE_FIELDS, minimum_exclusive=0.0)
    if price is not None:
        out["filled_price"] = price
    fee = _coalesce_float(fields, FEE_FIELDS)
    if fee is not None:
        out["filled_fee"] = fee
    reason = (fields.get("reason") or fields.get("message")
              or fields.get("error_msg") or fields.get("reject_reason"))
    if reason:
        out["reject_reason"] = str(reason)
    return out
