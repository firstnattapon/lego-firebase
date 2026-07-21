"""test_lego_fixes.py — pure-function tests (ไม่ต้องมี secret/network)

รัน: python3 -m pytest test_lego_fixes.py -q
ครอบคลุม: decision band ตาม spec (ไม่มี clamp), สมการ recurrence, DNA golden,
order payload ตาม decimal_precision, _extract_qty fail-closed, submit gate,
summarize/retry เดิม, และ Step 18 commit protocol (fake RTDB):
idempotent / stale-anchor / repair / PARTIAL ยังถูก reconcile
"""
import math

import pytest

from dna_engine import DNAError, decode_dna
from lego_one_row import (Anchor, Config, DNAExhausted, PASS_DNA_ZERO,
                          PASS_THRESHOLD, READY_BUY, READY_SELL,
                          RowValidationError, COLUMN_ORDER, build_decision,
                          compute_recurrence, compute_row, dna_signal_for,
                          dna_step_for, validate_row_columns)
from lego_orders import (REALIZED_STATUSES, TERMINAL_STATUSES, SubmitGateError,
                         evaluate_submit_gate, order_confirmation_phrase,
                         summarize_order_result)
from webull_io import _extract_qty, _retry_transient, build_order_payload

CFG = Config(symbol="APLS", fix_c=1500.0, diff=60.0)


# ---- Step 8: decision band ตามสัญญา (invariant #4, #5 — ไม่มี clamp) --------
def test_decision_buy_spec():
    d = build_decision(CFG, price=10.0, holdings=100.0, signal=1)
    assert (d.status, d.action, d.side) == (READY_BUY, "TRIGGER_ACTION", "BUY")
    assert d.value == 1000.0 and d.gap == 500.0
    assert d.quantity == round(500.0 / 10.0, 5) == 50.0


def test_decision_sell_spec_no_clamp():
    # คอลัมน์ 11 = round(|gap|/Pₙ, dp) ตรง ๆ — ห้ามมีเงื่อนไขอื่นแทรก
    d = build_decision(CFG, price=12.0, holdings=150.0, signal=1)
    assert (d.status, d.side) == (READY_SELL, "SELL")
    assert d.gap == -300.0
    assert d.quantity == round(300.0 / 12.0, 5) == 25.0
    # qty < holdings เสมอ (คณิต: qty = holdings − FIX_C/Pₙ)
    assert d.quantity < 150.0


def test_decision_pass_threshold_band():
    d = build_decision(CFG, price=10.0, holdings=145.5, signal=1)   # gap=45 ≤ 60
    assert d.status == PASS_THRESHOLD and d.quantity == 0.0 and d.side == ""


def test_decision_gate_signal_zero_wins():
    d = build_decision(CFG, price=10.0, holdings=0.0, signal=0)     # gap เต็ม 1500
    assert d.status == PASS_DNA_ZERO and d.quantity == 0.0 and d.action == "PASS"


def test_decision_invalid_inputs_fail_closed():
    with pytest.raises(ValueError):
        build_decision(CFG, price=0.0, holdings=1.0, signal=1)
    with pytest.raises(ValueError):
        build_decision(CFG, price=float("nan"), holdings=1.0, signal=1)
    with pytest.raises(ValueError):
        build_decision(CFG, price=10.0, holdings=float("nan"), signal=1)
    with pytest.raises(ValueError):
        build_decision(CFG, price=10.0, holdings=-1.0, signal=1)
    with pytest.raises(ValueError):
        build_decision(CFG, price=10.0, holdings=1.0, signal=2)


def test_config_validation():
    with pytest.raises(ValueError):
        Config(symbol="A", fix_c=0.0)
    with pytest.raises(ValueError):
        Config(symbol="A", fix_c=1500.0, diff=float("inf"))
    with pytest.raises(ValueError):
        Config(symbol="A", fix_c=1500.0, decimal_precision=6)
    with pytest.raises(ValueError):
        Config(symbol="A", fix_c=1500.0, decimal_precision=-1)
    assert Config(symbol="A", fix_c=1500.0, decimal_precision=0).decimal_precision == 0


# ---- Step 4–5 + 14–17: step, signal, recurrence ----------------------------
def test_dna_step_genesis_and_increment():
    assert dna_step_for(None) == 0
    a = Anchor(version=4, dna_step=4, p0=10.0, prev_price=12.0, prev_actual=0.0)
    assert dna_step_for(a) == 5


def test_dna_signal_exhausted_fail_closed():
    assert dna_signal_for("bypass:3", 2) == 1
    with pytest.raises(DNAExhausted):
        dna_signal_for("bypass:3", 3)


def test_recurrence_genesis_all_zero():
    r = compute_recurrence(CFG, price=10.0, anchor=None)
    assert (r.R, r.dA, r.A, r.E) == (0.0, 0.0, 0.0, 0.0)


def test_recurrence_formulas_golden():
    a = Anchor(version=1, dna_step=0, p0=10.0, prev_price=12.0, prev_actual=250.0)
    r = compute_recurrence(CFG, price=11.0, anchor=a)
    assert r.R == pytest.approx(1500.0 * math.log(11.0 / 10.0))
    assert r.R == pytest.approx(142.96527, rel=1e-6)          # ค่าอิสระยืนยันสูตร
    assert r.dA == pytest.approx(1500.0 * (11.0 / 12.0 - 1.0))
    assert r.A == pytest.approx(250.0 + r.dA)
    assert r.E == pytest.approx(r.A - r.R)


def test_recurrence_bad_prices_fail_closed():
    a = Anchor(version=1, dna_step=0, p0=0.0, prev_price=12.0, prev_actual=0.0)
    with pytest.raises(ValueError):
        compute_recurrence(CFG, price=11.0, anchor=a)


# ---- compute_row: สัญญา 17 คอลัมน์ -----------------------------------------
SNAP = {"captured_at": "2026-07-20T14:30:00Z", "price": 12.0, "holdings": 150.0}


def test_compute_row_17_columns_exact_order():
    row = compute_row(CFG, SNAP, anchor=None)
    assert [k for k in row if k != "_meta"] == COLUMN_ORDER
    assert row["สถานะ"] == READY_SELL and row["DNA step"] == 0
    assert row["Rₙ อ้างอิง (USD)"] == 0.0                     # แถว genesis
    assert row["_meta"]["p0_next"] == 12.0


def test_validate_row_columns_fail_closed():
    row = compute_row(CFG, SNAP, anchor=None)
    bad = {k: v for k, v in row.items() if k != "ฝั่ง"}
    with pytest.raises(RowValidationError):
        validate_row_columns(bad)


# ---- DNA golden (ตรง skill / ตัว encode) -----------------------------------
def test_dna_stream_golden_champion():
    d = decode_dna("26021034252903219354832053493")
    assert len(d) == 60 and sum(d) == 25
    assert d[:20] == [1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 1, 1, 0, 1, 1, 1]
    assert d[0] == 1
    assert decode_dna("26021034252903219354832053493") == d    # deterministic


def test_dna_bypass_forms():
    assert decode_dna("bypass:4") == [1, 1, 1, 1]
    assert decode_dna("[1, 4]") == [1, 1, 1, 1]


def test_dna_bad_specs_fail_closed():
    for bad in ("seed:425:60", "", "abc", "[2, 4]", "bypass:0"):
        with pytest.raises(DNAError):
            decode_dna(bad)
    with pytest.raises(DNAError):
        decode_dna("26033003425")      # rate 300 = 300% -> DNAError


# ---- order payload: quantity ตาม decimal_precision -------------------------
def test_payload_quantity_respects_precision():
    assert build_order_payload(CFG, "BUY", 25.0, "x")[0]["quantity"] == "25"
    assert build_order_payload(CFG, "SELL", 0.25, "x")[0]["quantity"] == "0.25"
    cfg0 = Config(symbol="APLS", fix_c=1500.0, decimal_precision=0)
    assert build_order_payload(cfg0, "BUY", 3.0, "x")[0]["quantity"] == "3"
    assert build_order_payload(CFG, "BUY", 1.23456, "x")[0]["quantity"] == "1.23456"


def test_payload_fixed_fields():
    o = build_order_payload(CFG, "BUY", 1.0, "cid123")[0]
    assert (o["order_type"], o["time_in_force"], o["entrust_type"]) == ("MARKET", "DAY", "QTY")
    assert o["client_order_id"] == "cid123" and o["symbol"] == "APLS"


# ---- _extract_qty: fail closed เมื่อ shape ไม่รู้จัก ------------------------
def test_extract_qty_known_shapes():
    assert _extract_qty([{"symbol": "APLS", "quantity": "4.5"}], "APLS") == 4.5
    assert _extract_qty({"positions": [{"symbol": "APLS", "quantity": 2}]}, "APLS") == 2.0
    assert _extract_qty({"items": [{"symbol": "TSLA", "quantity": 9}]}, "APLS") == 0.0
    assert _extract_qty({"data": []}, "APLS") == 0.0
    assert _extract_qty({"positions": []}, "APLS") == 0.0      # "ไม่มีหุ้น" ที่ถูกต้อง
    assert _extract_qty([], "APLS") == 0.0


def test_extract_qty_unknown_shape_fail_closed():
    # holdings=0 ปลอม -> READY_BUY ซ้ำทั้งก้อน — ต้อง raise ไม่ใช่คืน 0
    with pytest.raises(ValueError):
        _extract_qty({"unexpected": []}, "APLS")
    with pytest.raises(ValueError):
        _extract_qty(None, "APLS")


# ---- submit gate (invariant #9) --------------------------------------------
def _ready_row():
    return compute_row(CFG, SNAP, anchor=None)                 # READY_SELL qty 25


def test_gate_blocks_production_even_with_preview():
    with pytest.raises(SubmitGateError):
        evaluate_submit_gate("Production", _ready_row(), True,
                             order_confirmation_phrase(_ready_row()), committed=True)


def test_gate_blocks_uncommitted_and_bad_phrase_and_preview():
    row = _ready_row()
    phrase = order_confirmation_phrase(row)
    with pytest.raises(SubmitGateError):
        evaluate_submit_gate("Test (UAT)", row, True, phrase, committed=False)
    with pytest.raises(SubmitGateError):
        evaluate_submit_gate("Test (UAT)", row, False, phrase, committed=True)
    with pytest.raises(SubmitGateError):
        evaluate_submit_gate("Test (UAT)", row, True, "CONFIRM WRONG", committed=True)
    evaluate_submit_gate("Test (UAT)", row, True, phrase, committed=True)   # ผ่าน


# ---- summarize_order_result (invariant #10) --------------------------------
def test_place_only_no_status_is_unknown():
    s = summarize_order_result({"client_order_id": "abc"})
    assert s["status"] == "UNKNOWN"
    assert s["realized"] is False


def test_detail_flat_filled():
    s = summarize_order_result({}, {"order_status": "FILLED", "filled_quantity": "1.5"})
    assert s["status"] == "FILLED"
    assert s["realized"] is True
    assert s["filled_quantity"] == "1.5"


def test_detail_partial_filled_with_space_from_sdk_enum():
    # SDK enum จริงคือ "PARTIAL FILLED" (มีช่องว่าง) — normalize แล้วนับ realized
    s = summarize_order_result({}, {"order_status": "PARTIAL FILLED"})
    assert s["status"] == "PARTIAL_FILLED"
    assert s["realized"] is True


def test_partial_realized_but_not_terminal():
    # partial = ของเข้าพอร์ตแล้วบางส่วน (realized) แต่ order ยังไม่จบ — ต้องตามต่อ
    assert "PARTIAL_FILLED" in REALIZED_STATUSES
    assert "PARTIAL_FILLED" not in TERMINAL_STATUSES
    assert "FILLED" in TERMINAL_STATUSES and "CANCELLED" in TERMINAL_STATUSES


def test_detail_nested_in_items():
    detail = {"items": [{"order_status": "FAILED", "reason": "insufficient buying power"}]}
    s = summarize_order_result({}, detail)
    assert s["status"] == "FAILED"
    assert s["realized"] is False
    assert s["reject_reason"] == "insufficient buying power"


def test_detail_nested_list_response():
    s = summarize_order_result({}, [{"status": "CANCELLED"}])
    assert s["status"] == "CANCELLED"
    assert s["realized"] is False


def test_submitted_not_realized():
    s = summarize_order_result({}, {"order_status": "SUBMITTED"})
    assert s["status"] == "SUBMITTED"
    assert s["realized"] is False
    assert "SUBMITTED" not in TERMINAL_STATUSES


# ---- _retry_transient ------------------------------------------------------
class _Transient(Exception):
    def __init__(self, http_status=504):
        self.http_status = http_status


def test_retry_succeeds_on_third_attempt(monkeypatch):
    monkeypatch.setattr("webull_io.time.sleep", lambda s: None)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _Transient(504)
        return "ok"

    assert _retry_transient(fn) == "ok"
    assert calls["n"] == 3


def test_retry_exhausted_raises_last(monkeypatch):
    monkeypatch.setattr("webull_io.time.sleep", lambda s: None)

    def fn():
        raise _Transient(504)

    with pytest.raises(_Transient):
        _retry_transient(fn)


def test_non_transient_raises_immediately():
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise ValueError("signature mismatch")

    with pytest.raises(ValueError):
        _retry_transient(fn)
    assert calls["n"] == 1


def test_gateway_timeout_error_code_without_http_status(monkeypatch):
    monkeypatch.setattr("webull_io.time.sleep", lambda s: None)

    class _GwTimeout(Exception):
        error_code = "GATEWAY_TIMEOUT"

    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 2:
            raise _GwTimeout()
        return "ok"

    assert _retry_transient(fn) == "ok"


# ---- fake RTDB สำหรับทดสอบ Step 18 -----------------------------------------
class _FakeRef:
    def __init__(self, store: dict, path: str):
        self._store, self._path = store, path.strip("/")

    def _node(self, create=False):
        cur = self._store
        parts = self._path.split("/")
        for p in parts[:-1]:
            if p not in cur:
                if not create:
                    return None, parts[-1]
                cur[p] = {}
            cur = cur[p]
        return cur, parts[-1]

    def get(self):
        parent, leaf = self._node()
        if parent is None:
            return None
        v = parent.get(leaf)
        return None if v == {} else v

    def set(self, value):
        parent, leaf = self._node(create=True)
        parent[leaf] = value

    def update(self, fields):
        parent, leaf = self._node(create=True)
        parent.setdefault(leaf, {}).update(fields)

    def delete(self):
        parent, leaf = self._node()
        if parent is not None:
            parent.pop(leaf, None)

    def transaction(self, fn):
        self.set(fn(self.get()))


@pytest.fixture
def fake_db(monkeypatch):
    import lego_state
    store: dict = {}
    monkeypatch.setattr(lego_state.db, "reference",
                        lambda path: _FakeRef(store, path))
    return store


# ---- Step 18: commit protocol (invariant #7, #8) ---------------------------
def test_commit_genesis_then_idempotent_replay(fake_db):
    from lego_state import commit_final_row, read_anchor
    row = compute_row(CFG, SNAP, anchor=None)

    r1 = commit_final_row(CFG, SNAP, None, row)
    assert r1["committed"] is True and r1["version"] == 1
    rows = fake_db["webull_lego_rows"]
    assert rows[r1["run_id"]]["committed"] is True

    # replay snapshot เดิม -> no-op ไม่สร้างแถวใหม่ (invariant #7)
    r2 = commit_final_row(CFG, SNAP, None, row)
    assert r2["idempotent"] is True and len(rows) == 1

    a = read_anchor(CFG)
    assert a.version == 1 and a.dna_step == 0
    assert a.p0 == 12.0 and a.prev_price == 12.0


def test_commit_stale_anchor_fail_closed(fake_db):
    from lego_state import StaleAnchorError, commit_final_row
    row0 = compute_row(CFG, SNAP, anchor=None)
    commit_final_row(CFG, SNAP, None, row0)

    # anchor เก่า (version 0 ไม่มีจริง) + snapshot ใหม่ -> StaleAnchorError + ไม่มี orphan
    stale = Anchor(version=0, dna_step=0, p0=12.0, prev_price=12.0, prev_actual=0.0)
    snap2 = {**SNAP, "captured_at": "2026-07-20T15:00:00Z", "price": 13.0}
    row2 = compute_row(CFG, snap2, anchor=stale)
    with pytest.raises(StaleAnchorError):
        commit_final_row(CFG, snap2, stale, row2)
    assert len(fake_db["webull_lego_rows"]) == 1               # orphan ถูกลบแล้ว


def test_commit_second_row_advances_pointer(fake_db):
    from lego_state import commit_final_row, read_anchor
    row0 = compute_row(CFG, SNAP, anchor=None)
    commit_final_row(CFG, SNAP, None, row0)
    a1 = read_anchor(CFG)

    snap2 = {**SNAP, "captured_at": "2026-07-20T15:00:00Z", "price": 13.0}
    row2 = compute_row(CFG, snap2, anchor=a1)
    r = commit_final_row(CFG, snap2, a1, row2)
    assert r["version"] == 2

    a2 = read_anchor(CFG)
    assert a2.dna_step == 1 and a2.p0 == 12.0 and a2.prev_price == 13.0
    assert a2.prev_actual == pytest.approx(1500.0 * (13.0 / 12.0 - 1.0))


def test_commit_rejects_malformed_row(fake_db):
    from lego_state import commit_final_row
    row = compute_row(CFG, SNAP, anchor=None)
    bad = {k: v for k, v in row.items() if k != "ฝั่ง"}
    with pytest.raises(RowValidationError):
        commit_final_row(CFG, SNAP, None, bad)


def test_repair_pending_marks_committed(fake_db):
    from lego_state import commit_final_row
    row0 = compute_row(CFG, SNAP, anchor=None)
    r1 = commit_final_row(CFG, SNAP, None, row0)
    # จำลอง crash ระหว่างขั้น 2)-3): state advance แล้วแต่ row ค้าง committed=False
    fake_db["webull_lego_rows"][r1["run_id"]]["committed"] = False

    from lego_state import read_anchor
    a1 = read_anchor(CFG)
    snap2 = {**SNAP, "captured_at": "2026-07-20T15:00:00Z", "price": 13.0}
    commit_final_row(CFG, snap2, a1, compute_row(CFG, snap2, anchor=a1))
    assert fake_db["webull_lego_rows"][r1["run_id"]]["committed"] is True


def test_pending_audits_includes_partial_and_placing(fake_db):
    from lego_state import pending_audits, write_order_audit
    write_order_audit("e1", {"status": "PARTIAL_FILLED", "realized": True})
    write_order_audit("e2", {"status": "PLACING", "realized": False})
    write_order_audit("e3", {"status": "FILLED", "realized": True})
    write_order_audit("e4", {"status": "NOT_PLACED", "realized": False})
    pend = pending_audits(TERMINAL_STATUSES | {"NOT_PLACED"})
    assert set(pend) == {"e1", "e2"}     # partial ต้องถูกตามต่อ, terminal/local-final ไม่เอา


def test_write_order_audit_redacts_secrets(fake_db):
    from lego_state import write_order_audit
    write_order_audit("e9", {"status": "PLACING", "app_secret": "S", "access_token": "T"})
    doc = fake_db["webull_lego_order_audit"]["e9"]
    assert "app_secret" not in doc and "access_token" not in doc


# ---- Anchor.prev_holdings mapping -----------------------------------------
def test_read_anchor_prev_holdings_none_safe(monkeypatch):
    import lego_state

    state = {"version": 3, "dna_step": 2, "p0": 333.74,
             "prev_price": 326.51, "prev_actual": -43.56}

    class _Ref:
        def get(self):
            return state

    monkeypatch.setattr(lego_state.db, "reference", lambda path: _Ref())
    cfg = Config(symbol="AAPL", fix_c=2000.0, diff=10.0)

    a = lego_state.read_anchor(cfg)
    assert a.prev_holdings is None       # state เก่าไม่มี field -> None

    state["prev_holdings"] = 4.61492
    a = lego_state.read_anchor(cfg)
    assert a.prev_holdings == pytest.approx(4.61492)
