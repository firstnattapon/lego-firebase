"""lego_one_row.py — Step 0–17 (pure, ไม่มี I/O)

ยึด LEGO invariant: DNA step +1 ทุกแถว, gate, decision band, recurrence คิดทุกแถว,
17 คอลัมน์ลำดับตายตัว (validate_row_columns fail closed).

หลักบัญชี (Webull_Dashboard · Rebalancing 101 บทที่ 4 — "กำไรเกิดเมื่อรอบปิด"):
  - Rₙ (คอลัมน์ 14) = Reference เชิงทฤษฎีจากราคา: FIX_C·ln(Pₙ/P₀) — สูตรเดิม
  - ΔAₙ/Aₙ (คอลัมน์ 15–16) = Realized Profit เฉพาะรอบซื้อขายที่จับคู่ปิดสมบูรณ์
    (Buy↔Sell หรือ Sell↔Buy จาก fill จริงของ broker) — ขาเดียว/PASS/ไม่มี fill = 0
    ห้ามใช้สูตรราคา FIX_C·(Pₙ/Pₙ₋₁−1) เป็นเงินจริง (นั่นคือโมเดลทฤษฎีที่สมมติว่า
    rebalance สำเร็จทุกสเต็ป — ปะปนกับเงินจริงไม่ได้)
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from dna_engine import decode_dna

# ---- สถานะ 5 ค่า + ค่าคงที่ ----------------------------------------------
SNAPSHOT_READY = "SNAPSHOT_READY"
PASS_DNA_ZERO = "PASS_DNA_ZERO"
PASS_THRESHOLD = "PASS_THRESHOLD"
READY_BUY = "READY_BUY"
READY_SELL = "READY_SELL"
DECISION_STAGE = 8

COLUMN_ORDER = [
    "เวลา (UTC)",            # 1
    "สินทรัพย์",             # 2
    "สถานะ",                 # 3
    "DNA step",             # 4
    "DNA signal",           # 5
    "ราคา Pₙ (USD)",         # 6
    "จำนวนถือครอง (หุ้น)",   # 7
    "คำสั่ง",                # 8  = action จาก build_decision (PASS | TRIGGER_ACTION)
    "ฝั่ง",                  # 9  = side (BUY | SELL | "")
    "เหตุผล",                # 10
    "จำนวนสั่ง (หุ้น)",       # 11
    "มูลค่าพอร์ต (USD)",     # 12
    "ส่วนต่างเป้าหมาย (USD)", # 13
    "Rₙ อ้างอิง (USD)",      # 14
    "ΔAₙ ต่อสเต็ป (USD)",    # 15
    "Aₙ สะสม (USD)",         # 16
    "Eₙ ส่วนเกินสะสม (USD)", # 17
]


class DNAExhausted(RuntimeError):
    """step >= len(dna) -> fail closed (invariant #3)"""


class RowValidationError(RuntimeError):
    """17 คอลัมน์ไม่ครบ/ผิดลำดับ -> fail closed"""


@dataclass(frozen=True)
class Anchor:
    """latest anchor (state pointer แถวก่อนหน้า). None = genesis."""
    version: int
    dna_step: int
    p0: float
    prev_price: float
    prev_actual: float                   # Aₙ₋₁ = กำไรสะสมจากรอบที่จับคู่ปิดแล้ว (realized)
    prev_holdings: float | None = None   # holdings ล่าสุด (fallback เมื่อ positions endpoint ล้ม); state เก่า = None
    open_legs: dict | None = None        # ขาเทรดที่ยังจับคู่ไม่ครบ {"buys": [[qty,price,fee_ps]], "sells": [...]}
    applied_fills: dict | None = None    # cumulative fill ที่นับเข้า ledger แล้ว ต่อ client_order_id (กันนับซ้ำ)


@dataclass(frozen=True)
class Config:
    symbol: str
    fix_c: float
    diff: float = 0.0
    dna_code: str = "bypass:100"
    strategy_id: str = "shannon_demon_lego"
    decimal_precision: int = 5

    def __post_init__(self):
        if not (math.isfinite(self.fix_c) and self.fix_c > 0):
            raise ValueError("fix_c ต้อง finite และ > 0")
        if not (math.isfinite(self.diff) and self.diff >= 0):
            raise ValueError("diff ต้อง finite และ >= 0")
        if not (0 <= self.decimal_precision <= 5):
            raise ValueError("decimal_precision ต้อง 0..5 (Webull รองรับเศษหุ้นสูงสุด 5 ตำแหน่ง)")


# ---- Step 4 ----------------------------------------------------------------
def dna_step_for(anchor: Anchor | None) -> int:
    """step = anchor.dna_step + 1 ; แถวแรก (genesis) = 0  (invariant #3)"""
    return 0 if anchor is None else anchor.dna_step + 1


# ---- Step 5 ----------------------------------------------------------------
def dna_signal_for(dna_code: str, step: int) -> int:
    """signal = decode_dna(dna_code)[step] ∈ {0,1}; เกินความยาว -> fail closed"""
    dna = decode_dna(dna_code)
    if step >= len(dna):
        raise DNAExhausted(f"DNA exhausted: step={step} len={len(dna)}")
    return int(dna[step])


# ---- Step 8: decision (สร้างครั้งเดียว) -----------------------------------
@dataclass(frozen=True)
class Decision:
    status: str
    action: str      # "PASS" | "TRIGGER_ACTION"
    side: str        # "BUY" | "SELL" | ""
    reason: str      # = status
    quantity: float
    value: float     # Vₙ
    gap: float       # FIX_C − Vₙ


def build_decision(cfg: Config, price: float, holdings: float, signal: int) -> Decision:
    """Step 8: Vₙ, gap, action, side, reason, quantity จาก object เดียว ครั้งเดียว"""
    if not (math.isfinite(price) and price > 0):
        raise ValueError("price (Pₙ) ต้อง finite และ > 0")   # invariant #6
    if not (math.isfinite(holdings) and holdings >= 0):
        raise ValueError("holdings ต้อง finite และ >= 0")
    if signal not in (0, 1):
        raise ValueError("signal ต้อง ∈ {0,1}")

    value = holdings * price
    gap = cfg.fix_c - value

    if signal == 0:                                # gate (invariant #4)
        return Decision(PASS_DNA_ZERO, "PASS", "", PASS_DNA_ZERO, 0.0, value, gap)

    if abs(gap) <= cfg.diff:                       # band (invariant #5)
        return Decision(PASS_THRESHOLD, "PASS", "", PASS_THRESHOLD, 0.0, value, gap)

    # qty ตามสัญญาคอลัมน์ 11 เท่านั้น — ไม่มี clamp ใน engine (broker safety อยู่ชั้น order)
    # SELL เกินที่ถือเป็นไปไม่ได้เชิงคณิต: gap < −diff ⟹ qty = holdings − FIX_C/Pₙ < holdings
    qty = round(abs(gap) / price, cfg.decimal_precision)
    if gap > cfg.diff:
        return Decision(READY_BUY, "TRIGGER_ACTION", "BUY", READY_BUY, qty, value, gap)
    return Decision(READY_SELL, "TRIGGER_ACTION", "SELL", READY_SELL, qty, value, gap)


# ---- Realized cycle ledger: จับคู่ Buy↔Sell แบบ FIFO (บทที่ 4) --------------
_LEG_EPS = 1e-9   # กันเศษ float ค้างเป็นขาเปิดปลอม


def empty_open_legs() -> dict:
    return {"buys": [], "sells": []}


def _normalize_legs(raw) -> dict:
    """state จาก RTDB อาจไม่มี key (RTDB ตัด list ว่างทิ้ง) -> เติมโครงให้ครบ"""
    raw = raw or {}
    out = empty_open_legs()
    for side in ("buys", "sells"):
        for leg in raw.get(side) or []:
            q, p, f = float(leg[0]), float(leg[1]), float(leg[2])
            if q > _LEG_EPS:
                out[side].append([q, p, f])
    return out


def apply_fill(open_legs: dict | None, side: str, qty: float, price: float,
               fee: float = 0.0) -> tuple[dict, float]:
    """นำ fill จริงหนึ่งก้อนเข้า ledger -> (open_legs ใหม่, realized profit)

    กำไรเกิดเฉพาะส่วนที่จับคู่กับขาฝั่งตรงข้ามที่เปิดค้าง (FIFO — เก่าสุดก่อน):
      Buy→Sell: realized = (P_sell − P_buy) × qty_matched
      Sell→Buy: realized = (P_sell − P_buy) × qty_matched   (ขายแพงก่อน ซื้อคืนถูก)
    fee เก็บเป็น fee/หุ้น ติดขาไว้ หักตอนจับคู่เฉพาะส่วนที่ matched ทั้งสองขา
    ขาเดียวที่ยังไม่มีคู่ -> เข้าคิวฝั่งตัวเอง, realized = 0.0 เป๊ะ (ห้ามนับเป็นกำไร)
    """
    side = str(side).upper()
    if side not in ("BUY", "SELL"):
        raise ValueError("side ต้อง BUY หรือ SELL")
    if not (math.isfinite(qty) and qty > 0):
        raise ValueError("fill qty ต้อง finite และ > 0")
    if not (math.isfinite(price) and price > 0):
        raise ValueError("fill price ต้อง finite และ > 0")
    if not (math.isfinite(fee) and fee >= 0):
        raise ValueError("fill fee ต้อง finite และ >= 0")

    legs = _normalize_legs(open_legs)
    fee_ps = fee / qty
    remaining = qty
    realized = 0.0
    opposite = legs["sells"] if side == "BUY" else legs["buys"]
    while remaining > _LEG_EPS and opposite:
        oq, op, ofps = opposite[0]
        m = min(remaining, oq)
        # (P_sell − P_buy) × matched — ทิศไหนก่อนก็สูตรเดียวกัน
        realized += ((op - price) if side == "BUY" else (price - op)) * m
        realized -= (ofps + fee_ps) * m
        remaining -= m
        if oq - m <= _LEG_EPS:
            opposite.pop(0)
        else:
            opposite[0][0] = oq - m
    if remaining > _LEG_EPS:
        (legs["buys"] if side == "BUY" else legs["sells"]).append(
            [remaining, price, fee_ps])
    return legs, realized


# ---- Step 14–17: recurrence (คิดทุกแถว ไม่ขึ้นกับ decision) ----------------
@dataclass(frozen=True)
class Recurrence:
    R: float    # Rₙ = FIX_C·ln(Pₙ/P₀) — Reference เชิงทฤษฎี (จากราคา)
    dA: float   # ΔAₙ = realized profit จากรอบที่จับคู่ปิดตั้งแต่แถวก่อนหน้า (0 ถ้าไม่มี)
    A: float    # Aₙ = Aₙ₋₁ + ΔAₙ — กำไรสะสมเฉพาะรอบที่ปิดแล้ว
    E: float    # Eₙ = Aₙ − Rₙ


def compute_recurrence(cfg: Config, price: float, anchor: Anchor | None,
                       realized_delta: float = 0.0) -> Recurrence:
    """แถวแรก: P₀=Pₙ, R=ΔA=A=E=0 ; อื่น ๆ: R จากสูตรราคา (Reference),
    ΔAₙ = realized_delta (กำไรจากรอบที่จับคู่ปิดเท่านั้น — ผ่าน apply_fill),
    price/p0/prev_price ≤ 0 หรือ realized_delta ไม่ finite -> fail closed"""
    if not math.isfinite(realized_delta):
        raise ValueError("realized_delta ต้อง finite")
    if anchor is None:
        if realized_delta != 0.0:
            raise ValueError("แถว genesis ยังไม่มี order -> realized_delta ต้อง 0")
        return Recurrence(0.0, 0.0, 0.0, 0.0)
    if not (price > 0 and anchor.p0 > 0 and anchor.prev_price > 0):
        raise ValueError("price / p0 / prev_price ต้อง > 0")   # invariant #6
    R = cfg.fix_c * math.log(price / anchor.p0)
    dA = float(realized_delta)
    A = anchor.prev_actual + dA
    E = A - R
    return Recurrence(R, dA, A, E)


# ---- ประกอบ 17 คอลัมน์ -----------------------------------------------------
def compute_row(cfg: Config, snapshot: dict, anchor: Anchor | None,
                realized_delta: float = 0.0) -> dict:
    """snapshot = {captured_at, price, holdings}; คืน row dict 17 คอลัมน์ตามลำดับ
    realized_delta = กำไรจากรอบที่จับคู่ปิดตั้งแต่แถวก่อนหน้า (default 0 = ไม่มี fill ใหม่)"""
    step = dna_step_for(anchor)
    signal = dna_signal_for(cfg.dna_code, step)
    price = float(snapshot["price"])
    holdings = float(snapshot.get("holdings", 0.0) or 0.0)

    dec = build_decision(cfg, price, holdings, signal)
    rec = compute_recurrence(cfg, price, anchor, realized_delta)

    row = {
        "เวลา (UTC)": snapshot["captured_at"],
        "สินทรัพย์": cfg.symbol,
        "สถานะ": dec.status,
        "DNA step": step,
        "DNA signal": signal,
        "ราคา Pₙ (USD)": price,
        "จำนวนถือครอง (หุ้น)": holdings,
        "คำสั่ง": dec.action,                 # คอลัมน์ 8 = action ตามสัญญา
        "ฝั่ง": dec.side,                     # คอลัมน์ 9 = side (PASS = ว่าง)
        "เหตุผล": dec.reason,
        "จำนวนสั่ง (หุ้น)": dec.quantity,
        "มูลค่าพอร์ต (USD)": dec.value,
        "ส่วนต่างเป้าหมาย (USD)": dec.gap,
        "Rₙ อ้างอิง (USD)": rec.R,
        "ΔAₙ ต่อสเต็ป (USD)": rec.dA,
        "Aₙ สะสม (USD)": rec.A,
        "Eₙ ส่วนเกินสะสม (USD)": rec.E,
    }
    validate_row_columns(row)
    row["_meta"] = {
        "step": step, "price": price, "p0_next": anchor.p0 if anchor else price,
        "actual_next": rec.A, "status": dec.status, "side": dec.side,
        "quantity": dec.quantity, "action": dec.action,
    }
    return row


def validate_row_columns(row: dict) -> None:
    """fail closed ถ้า 17 คอลัมน์ไม่ครบหรือผิดลำดับ"""
    keys = [k for k in row.keys() if k != "_meta"]
    if keys != COLUMN_ORDER:
        raise RowValidationError(
            f"คอลัมน์ไม่ตรงสัญญา: got {len(keys)} / need 17 (ลำดับตายตัว)"
        )


def columns_presented(row: dict) -> dict:
    """แสดง/ส่งออก: คอลัมน์เงิน round 2dp (full precision เก็บใน RTDB)"""
    money = {6, 12, 13, 14, 15, 16, 17}
    out = {}
    for i, k in enumerate(COLUMN_ORDER, start=1):
        v = row[k]
        out[k] = round(v, 2) if (i in money and isinstance(v, (int, float))) else v
    return out
