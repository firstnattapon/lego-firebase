"""test_webull_io.py — retry/backoff + holdings fallback (รันได้โดยไม่ต้องมี webull SDK)

ครอบคลุม:
  - _is_transient จำแนก 504/timeout (True) vs 403/INVALID_TOKEN/ValueError (False)
  - _call_with_retry: transient แล้วหาย, transient ตลอด, non-transient, deadline
  - fetch_snapshot: positions ล้ม + fallback -> ใช้ค่าเก่า ; ไม่มี fallback -> raise
"""
from __future__ import annotations

import os
import sys

# ให้ retry เร็ว (ไม่ต้อง sleep จริง) ก่อน import module
os.environ.setdefault("WEBULL_RETRY_BASE_SLEEP", "0")
os.environ.setdefault("WEBULL_RETRY_ATTEMPTS", "3")
os.environ.setdefault("WEBULL_ACCOUNT_ID", "TEST_ACC")

import webull_io as W
from lego_one_row import Config


class ServerException(Exception):
    """เลียนแบบ webull.core.exception ...ServerException (status ฝังในข้อความ)"""


def _transient_504():
    return ServerException("HTTP Status: 504, Code: GATEWAY_TIMEOUT, Msg: , RequestID: x")


# ---- _is_transient ---------------------------------------------------------
def test_is_transient():
    assert W._is_transient(_transient_504()) is True
    assert W._is_transient(ServerException("Read timed out")) is True
    assert W._is_transient(Exception("Connection aborted")) is True
    assert W._is_transient(ServerException("HTTP Status: 403, Code: FORBIDDEN")) is False
    assert W._is_transient(ServerException("INVALID_TOKEN")) is False
    assert W._is_transient(ValueError("price ไม่ถูกต้อง")) is False


# ---- _call_with_retry ------------------------------------------------------
def test_retry_recovers():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _transient_504()
        return "ok"

    assert W._call_with_retry(flaky) == "ok"
    assert calls["n"] == 3


def test_retry_exhausts_then_raises():
    calls = {"n": 0}

    def always():
        calls["n"] += 1
        raise _transient_504()

    try:
        W._call_with_retry(always)
        assert False, "ควร raise หลังครบ attempts"
    except ServerException:
        pass
    assert calls["n"] == W._retry_attempts()


def test_retry_non_transient_raises_immediately():
    calls = {"n": 0}

    def forbidden():
        calls["n"] += 1
        raise ServerException("HTTP Status: 403, Code: FORBIDDEN")

    try:
        W._call_with_retry(forbidden)
        assert False, "non-transient ต้อง raise ทันที"
    except ServerException:
        pass
    assert calls["n"] == 1


def test_retry_deadline_stops(monkeypatch=None):
    # deadline=0 -> เลิก retry หลัง attempt แรก แม้ transient
    old = os.environ.get("WEBULL_RETRY_DEADLINE")
    os.environ["WEBULL_RETRY_DEADLINE"] = "0"
    try:
        calls = {"n": 0}

        def always():
            calls["n"] += 1
            raise _transient_504()

        try:
            W._call_with_retry(always)
            assert False
        except ServerException:
            pass
        assert calls["n"] == 1        # ไม่ retry เพราะ deadline=0
    finally:
        if old is None:
            os.environ.pop("WEBULL_RETRY_DEADLINE", None)
        else:
            os.environ["WEBULL_RETRY_DEADLINE"] = old


# ---- fetch_snapshot (mock clients) -----------------------------------------
class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


class _MarketData:
    def get_snapshot(self, *a, **k):
        return _Resp([{"symbol": "AAPL", "last": "333.32"}])


class _DataClient:
    market_data = _MarketData()


class _AccountFail:
    def get_account_position(self, account_id):
        raise _transient_504()


class _AccountOK:
    def get_account_position(self, account_id):
        return _Resp({"positions": [{"symbol": "AAPL", "quantity": "4.61492"}]})


class _TradeClient:
    def __init__(self, account):
        self.account_v2 = account


CFG = Config(symbol="AAPL", fix_c=1500.0, diff=10.0)


def test_fetch_positions_fallback_used():
    snap = W.fetch_snapshot(_TradeClient(_AccountFail()), _DataClient(), CFG,
                            fallback_holdings=4.61492)
    assert snap["price"] == 333.32
    assert snap["holdings"] == 4.61492   # ใช้ค่า fallback แทน (ไม่ raise)


def test_fetch_positions_no_fallback_raises():
    try:
        W.fetch_snapshot(_TradeClient(_AccountFail()), _DataClient(), CFG,
                         fallback_holdings=None)
        assert False, "ไม่มี fallback ต้อง raise"
    except ServerException:
        pass


def test_fetch_positions_fresh_when_ok():
    snap = W.fetch_snapshot(_TradeClient(_AccountOK()), _DataClient(), CFG,
                            fallback_holdings=99.0)
    assert snap["holdings"] == 4.61492   # ดึงของสด ไม่ใช้ fallback


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
