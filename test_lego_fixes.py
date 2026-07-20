"""test_lego_fixes.py — pure-function tests (ไม่ต้องมี secret/network)

รัน: python3 -m pytest test_lego_fixes.py -q
"""
import pytest

from lego_orders import (REALIZED_STATUSES, TERMINAL_STATUSES,
                         summarize_order_result)
from webull_io import _retry_transient


# ---- summarize_order_result ------------------------------------------------
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
    # SDK enum จริงคือ "PARTIAL FILLED" (มีช่องว่าง) — ต้อง normalize แล้วนับ realized
    s = summarize_order_result({}, {"order_status": "PARTIAL FILLED"})
    assert s["status"] == "PARTIAL_FILLED"
    assert s["realized"] is True


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
    assert "SUBMITTED" in TERMINAL_STATUSES or s["status"] not in TERMINAL_STATUSES


def test_realized_subset_of_terminal():
    assert REALIZED_STATUSES <= TERMINAL_STATUSES


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


# ---- Anchor.prev_holdings mapping -----------------------------------------
def test_read_anchor_prev_holdings_none_safe(monkeypatch):
    import lego_state

    state = {"version": 3, "dna_step": 2, "p0": 333.74,
             "prev_price": 326.51, "prev_actual": -43.56}

    class _Ref:
        def get(self):
            return state

    monkeypatch.setattr(lego_state.db, "reference", lambda path: _Ref())
    from lego_one_row import Config
    cfg = Config(symbol="AAPL", fix_c=2000.0, diff=10.0)

    a = lego_state.read_anchor(cfg)
    assert a.prev_holdings is None       # state เก่าไม่มี field -> None

    state["prev_holdings"] = 4.61492
    a = lego_state.read_anchor(cfg)
    assert a.prev_holdings == pytest.approx(4.61492)
