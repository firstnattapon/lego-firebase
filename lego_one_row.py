"""Pure LEGO row engine for the fixed 17-column contract.

The 17-column recurrence is a *model ledger*, not broker-realized P&L.
An act occurs only when the decision is READY_BUY/READY_SELL. A raw DNA signal
of 1 that lands inside the threshold band is PASS_THRESHOLD and freezes the
model ledger.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from dna_engine import decode_dna

SNAPSHOT_READY = "SNAPSHOT_READY"
PASS_DNA_ZERO = "PASS_DNA_ZERO"
PASS_THRESHOLD = "PASS_THRESHOLD"
READY_BUY = "READY_BUY"
READY_SELL = "READY_SELL"
DECISION_STAGE = 8

COLUMN_ORDER = [
    "เวลา (UTC)",
    "สินทรัพย์",
    "สถานะ",
    "DNA step",
    "DNA signal",
    "ราคา Pₙ (USD)",
    "จำนวนถือครอง (หุ้น)",
    "คำสั่ง",
    "ฝั่ง",
    "เหตุผล",
    "จำนวนสั่ง (หุ้น)",
    "มูลค่าพอร์ต (USD)",
    "ส่วนต่างเป้าหมาย (USD)",
    "Rₙ อ้างอิง (USD)",
    "ΔAₙ ต่อสเต็ป (USD)",
    "Aₙ สะสม (USD)",
    "Eₙ ส่วนเกินสะสม (USD)",
]


class DNAExhausted(RuntimeError):
    pass


class RowValidationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Anchor:
    version: int
    dna_step: int
    p0: float
    prev_price: float
    prev_actual: float
    prev_holdings: float | None = None


@dataclass(frozen=True)
class Config:
    symbol: str
    fix_c: float
    diff: float = 0.0
    dna_code: str = "bypass:100"
    strategy_id: str = "shannon_demon_lego"
    decimal_precision: int = 5

    def __post_init__(self):
        if not self.symbol or not str(self.symbol).strip():
            raise ValueError("symbol ต้องไม่ว่าง")
        if not (math.isfinite(self.fix_c) and self.fix_c > 0):
            raise ValueError("fix_c ต้อง finite และ > 0")
        if not (math.isfinite(self.diff) and self.diff >= 0):
            raise ValueError("diff ต้อง finite และ >= 0")
        if not (0 <= self.decimal_precision <= 5):
            raise ValueError("decimal_precision ต้อง 0..5")


def dna_step_for(anchor: Anchor | None) -> int:
    return 0 if anchor is None else anchor.dna_step + 1


def dna_signal_for(dna_code: str, step: int) -> int:
    dna = decode_dna(dna_code)
    if step >= len(dna):
        raise DNAExhausted(f"DNA exhausted: step={step} len={len(dna)}")
    return int(dna[step])


@dataclass(frozen=True)
class Decision:
    status: str
    action: str
    side: str
    reason: str
    quantity: float
    value: float
    gap: float

    @property
    def acted(self) -> bool:
        return self.status in (READY_BUY, READY_SELL) and self.quantity > 0


def build_decision(cfg: Config, price: float, holdings: float, signal: int) -> Decision:
    if not (math.isfinite(price) and price > 0):
        raise ValueError("price (Pₙ) ต้อง finite และ > 0")
    if not (math.isfinite(holdings) and holdings >= 0):
        raise ValueError("holdings ต้อง finite และ >= 0")
    if signal not in (0, 1):
        raise ValueError("signal ต้อง ∈ {0,1}")
    value = holdings * price
    gap = cfg.fix_c - value
    if signal == 0:
        return Decision(PASS_DNA_ZERO, "PASS", "", PASS_DNA_ZERO, 0.0, value, gap)
    if abs(gap) <= cfg.diff:
        return Decision(PASS_THRESHOLD, "PASS", "", PASS_THRESHOLD, 0.0, value, gap)
    qty = round(abs(gap) / price, cfg.decimal_precision)
    if qty <= 0:
        return Decision(PASS_THRESHOLD, "PASS", "", PASS_THRESHOLD, 0.0, value, gap)
    if gap > cfg.diff:
        return Decision(READY_BUY, "TRIGGER_ACTION", "BUY", READY_BUY, qty, value, gap)
    return Decision(READY_SELL, "TRIGGER_ACTION", "SELL", READY_SELL, qty, value, gap)


@dataclass(frozen=True)
class Recurrence:
    R: float
    dA: float
    A: float
    E: float
    acted_price_next: float


def compute_recurrence(cfg: Config, price: float, anchor: Anchor | None,
                       acted: bool | None = None, *, signal: int | None = None) -> Recurrence:
    """Compute the model recurrence.

    Genesis: R=ΔA=A=E=0.
    acted=True: ΔA=Fix_c(P/P_acted-1), then move P_acted to P.
    acted=False: ΔA=0, A and P_acted freeze; E stays at the last act value.
    """
    # Backward-compatible direct API: legacy callers may pass signal=0/1.
    # The production compute_row path never uses raw signal here; it passes the
    # final decision boolean, which fixes PASS_THRESHOLD(signal=1) semantics.
    if acted is None:
        if signal not in (0, 1):
            raise ValueError("ต้องระบุ acted bool หรือ signal ∈ {0,1}")
        acted = bool(signal)
    if type(acted) is not bool:
        raise ValueError("acted ต้องเป็น bool")
    if not (math.isfinite(price) and price > 0):
        raise ValueError("price ต้อง finite และ > 0")
    if anchor is None:
        return Recurrence(0.0, 0.0, 0.0, 0.0, float(price))
    if not (anchor.p0 > 0 and anchor.prev_price > 0
            and math.isfinite(anchor.p0) and math.isfinite(anchor.prev_price)
            and math.isfinite(anchor.prev_actual)):
        raise ValueError("anchor recurrence values ต้อง finite และ price > 0")
    R = cfg.fix_c * math.log(price / anchor.p0)
    if acted:
        dA = cfg.fix_c * (price / anchor.prev_price - 1.0)
        A = anchor.prev_actual + dA
        return Recurrence(R, dA, A, A - R, float(price))
    A = anchor.prev_actual
    R_acted = cfg.fix_c * math.log(anchor.prev_price / anchor.p0)
    return Recurrence(R, 0.0, A, A - R_acted, float(anchor.prev_price))


def compute_row(cfg: Config, snapshot: dict, anchor: Anchor | None) -> dict:
    step = dna_step_for(anchor)
    signal = dna_signal_for(cfg.dna_code, step)
    price = float(snapshot["price"])
    holdings = float(snapshot.get("holdings", 0.0) or 0.0)
    dec = build_decision(cfg, price, holdings, signal)
    rec = compute_recurrence(cfg, price, anchor, acted=dec.acted)
    row = {
        "เวลา (UTC)": snapshot["captured_at"],
        "สินทรัพย์": cfg.symbol,
        "สถานะ": dec.status,
        "DNA step": step,
        "DNA signal": signal,
        "ราคา Pₙ (USD)": price,
        "จำนวนถือครอง (หุ้น)": holdings,
        "คำสั่ง": dec.action,
        "ฝั่ง": dec.side,
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
        "step": step,
        "price": price,
        "p0_next": anchor.p0 if anchor else price,
        "acted": dec.acted,
        "acted_price_next": rec.acted_price_next,
        "actual_next": rec.A,
        "status": dec.status,
        "side": dec.side,
        "quantity": dec.quantity,
        "action": dec.action,
    }
    return row


def validate_row_columns(row: dict) -> None:
    keys = [k for k in row.keys() if k != "_meta"]
    if keys != COLUMN_ORDER:
        raise RowValidationError(
            f"คอลัมน์ไม่ตรงสัญญา: got {len(keys)} / need 17 (ลำดับตายตัว)")


def columns_presented(row: dict) -> dict:
    money = {6, 12, 13, 14, 15, 16, 17}
    out = {}
    for i, k in enumerate(COLUMN_ORDER, start=1):
        v = row[k]
        out[k] = round(v, 2) if (i in money and isinstance(v, (int, float))) else v
    return out
