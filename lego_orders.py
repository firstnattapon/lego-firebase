"""lego_orders.py — เส้นทาง order UAT (invariant #9, #10)

- Preview/Submit ใช้ได้เฉพาะ environment "Test (UAT)" และเฉพาะแถว READY_BUY/READY_SELL
  ที่ persist แล้ว (Step 18 committed)
- evaluate_submit_gate fail-closed: payload valid + preview ตรง + confirmation phrase ตรง
  + ไม่ใช่ Production
- summarize_order_result: เรียก FILLED เฉพาะ filled ชัดเจน — SUBMITTED/PENDING ไม่นับ realized
  พร้อมดึง execution facts (filled_price/filled_fee) เมื่อ broker ส่งมา — ห้ามใช้ราคา quote แทน
- unapplied_fill_increments: ส่วนเพิ่มของ fill (cumulative − applied) ที่ยังไม่เคยนับเข้า
  realized ledger — กันนับซ้ำจาก partial fill / duplicate snapshot / restart
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


# ---- realized ledger input: ส่วนเพิ่มของ fill ที่ยังไม่เคยนับ ---------------
def _safe_float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def unapplied_fill_increments(audits: dict | None, applied: dict | None,
                              chain_key: str) -> list[dict]:
    """คืนรายการ fill ส่วนเพิ่ม (cumulative − applied) ต่อ client_order_id เรียง placed_at

    fail closed ทุกทาง — ข้ามรายการ (รอ reconcile รอบหน้า ไม่นับมั่ว) เมื่อ:
      - audit ไม่ใช่ของ chain นี้ (chain_key ไม่ตรง / payload เก่าไม่มี chain_key)
      - status ไม่อยู่ใน REALIZED_STATUSES (SUBMITTED/PENDING/REJECTED/... ไม่นับ)
      - ไม่มี filled_quantity > 0 หรือไม่มี execution price > 0 (quote ใช้แทนไม่ได้)
      - cumulative ไม่เพิ่มจากที่ applied แล้ว (duplicate/stale snapshot -> กันนับซ้ำ)
    ผลลัพธ์แต่ละรายการ: {client_order_id, side, qty, price, fee, applied}
    โดย applied = cumulative ใหม่ที่ต้อง persist หลังนำเข้า ledger สำเร็จ"""
    out = []
    ordered = sorted(
        (audits or {}).items(),
        key=lambda kv: (str((kv[1] or {}).get("placed_at", "")
                            if isinstance(kv[1], dict) else ""), kv[0]))
    for cid, payload in ordered:
        if not isinstance(payload, dict):
            continue
        if payload.get("chain_key") != chain_key:
            continue
        side = str(payload.get("side", "")).upper()
        if side not in ("BUY", "SELL"):
            continue
        if _normalize_status(payload.get("status")) not in REALIZED_STATUSES:
            continue
        cum_qty = _safe_float(payload.get("filled_quantity"))
        price = _safe_float(payload.get("filled_price"))
        fee = _safe_float(payload.get("filled_fee"))
        cum_fee = fee if (fee is not None and fee >= 0) else 0.0
        if cum_qty is None or cum_qty <= 0:
            continue
        if price is None or price <= 0:
            continue
        prev = (applied or {}).get(cid) or {}
        prev_qty = float(prev.get("qty", 0.0) or 0.0)
        prev_notional = float(prev.get("notional", 0.0) or 0.0)
        prev_fee = float(prev.get("fee", 0.0) or 0.0)
        inc_qty = cum_qty - prev_qty
        if inc_qty <= 1e-9:
            continue
        inc_notional = cum_qty * price - prev_notional
        if inc_notional <= 0:
            continue        # ข้อมูล cumulative ถอยหลัง — ไม่น่าเชื่อถือ ไม่นับ
        out.append({
            "client_order_id": cid,
            "side": side,
            "qty": inc_qty,
            "price": inc_notional / inc_qty,
            "fee": max(0.0, cum_fee - prev_fee),
            "applied": {"qty": cum_qty, "notional": cum_qty * price,
                        "fee": max(cum_fee, prev_fee)},
        })
    return out
