"""lego_one_row.py — Step 0–17 (pure, ไม่มี I/O)

ยึด LEGO invariant: DNA step +1 ทุกแถว, gate, decision band, recurrence คิดทุกแถว,
17 คอลัมน์ลำดับตายตัว (validate_row_columns fail closed).
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
    prev_actual: float


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
        if not (self.diff >= 0):
            raise ValueError("diff ต้อง >= 0")
        if self.decimal_precision < 0:
            raise ValueError("decimal_precision ต้อง >= 0")


def _floor_dp(x: float, dp: int) -> float:
    """floor ที่ dp ตำแหน่ง — ใช้ clamp SELL ห้ามปัดขึ้น"""
    scale = 10 ** dp
    return math.floor(x * scale) / scale


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
    if holdings < 0:
        raise ValueError("holdings ต้อง >= 0")

    value = holdings * price
    gap = cfg.fix_c - value

    if signal == 0:                                # gate (invariant #4)
        return Decision(PASS_DNA_ZERO, "PASS", "", PASS_DNA_ZERO, 0.0, value, gap)

    if abs(gap) <= cfg.diff:                       # band (invariant #5)
        return Decision(PASS_THRESHOLD, "PASS", "", PASS_THRESHOLD, 0.0, value, gap)

    qty = round(abs(gap) / price, cfg.decimal_precision)
    if gap > cfg.diff:
        return Decision(READY_BUY, "TRIGGER_ACTION", "BUY", READY_BUY, qty, value, gap)
    # gap < −diff -> SELL ; clamp ด้วย floor(holdings) ห้ามปัดขึ้นแล้วขายเกินที่ถือ
    qty = min(qty, _floor_dp(holdings, cfg.decimal_precision))
    if qty <= 0:
        return Decision(PASS_THRESHOLD, "PASS", "", PASS_THRESHOLD, 0.0, value, gap)
    return Decision(READY_SELL, "TRIGGER_ACTION", "SELL", READY_SELL, qty, value, gap)


# ---- Step 14–17: recurrence (คิดทุกแถว ไม่ขึ้นกับ decision) ----------------
@dataclass(frozen=True)
class Recurrence:
    R: float    # Rₙ = FIX_C·ln(Pₙ/P₀)
    dA: float   # ΔAₙ = FIX_C·(Pₙ/Pₙ₋₁ − 1)
    A: float    # Aₙ = Aₙ₋₁ + ΔAₙ
    E: float    # Eₙ = Aₙ − Rₙ


def compute_recurrence(cfg: Config, price: float, anchor: Anchor | None) -> Recurrence:
    """แถวแรก: P₀=Pₙ, R=ΔA=A=E=0 ; อื่น ๆ ตามสูตร (price/p0/prev_price ≤ 0 -> fail closed)"""
    if anchor is None:
        return Recurrence(0.0, 0.0, 0.0, 0.0)
    if not (price > 0 and anchor.p0 > 0 and anchor.prev_price > 0):
        raise ValueError("price / p0 / prev_price ต้อง > 0")   # invariant #6
    R = cfg.fix_c * math.log(price / anchor.p0)
    dA = cfg.fix_c * (price / anchor.prev_price - 1.0)
    A = anchor.prev_actual + dA
    E = A - R
    return Recurrence(R, dA, A, E)


# ---- ประกอบ 17 คอลัมน์ -----------------------------------------------------
def compute_row(cfg: Config, snapshot: dict, anchor: Anchor | None) -> dict:
    """snapshot = {captured_at, price, holdings}; คืน row dict 17 คอลัมน์ตามลำดับ"""
    step = dna_step_for(anchor)
    signal = dna_signal_for(cfg.dna_code, step)
    price = float(snapshot["price"])
    holdings = float(snapshot.get("holdings", 0.0) or 0.0)

    dec = build_decision(cfg, price, holdings, signal)
    rec = compute_recurrence(cfg, price, anchor)

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
