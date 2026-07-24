"""End-to-end wiring of the lego_one_row handler: clock -> row -> slot guard.

Broker access is stubbed; the point is that DNA time advances on its own and
that slot provenance reaches Firebase.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import main
from conftest import FAKE_DB
from lego_state import STATE_PATH, chain_key

UTC = timezone.utc
SESSION_OPEN_SLOT = datetime(2026, 7, 23, 18, 0, 5, tzinfo=UTC)     # ordinal 0 on a 30m grid


def _fixed_now(moment: datetime):
    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz else moment
    return _Now


@pytest.fixture(autouse=True)
def env(monkeypatch):
    FAKE_DB.store.clear()
    for key, value in {
        "LEGO_SYMBOL": "AAPL", "LEGO_FIX_C": "3000", "LEGO_DIFF": "5",
        "LEGO_DNA_CODE": "bypass:100", "LEGO_DECIMAL_PRECISION": "2",
        "LEGO_SLOT_SECONDS": "1800", "LEGO_DNA_ORIGIN_UTC": "2026-07-23T18:00:00Z",
        "LEGO_DNA_CLOCK_MODE": "market", "FIREBASE_DB_URL": "https://x.firebaseio.com",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AUTO_SUBMIT", raising=False)
    monkeypatch.delenv("LEGO_INLINE_ORDER_WORKER", raising=False)
    monkeypatch.setattr(main, "build_clients", lambda: (object(), object()))
    monkeypatch.setattr(main, "is_us_market_open", lambda now=None: True)


def _run(monkeypatch, moment: datetime, price: float, holdings: float = 9.0):
    monkeypatch.setattr(main, "datetime", _fixed_now(moment))
    monkeypatch.setattr(main, "fetch_snapshot", lambda t, d, cfg: {
        "captured_at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "price": price, "holdings": holdings,
    })
    return main.lego_one_row(object())


def test_row_commits_with_slot_provenance(monkeypatch):
    body, code = _run(monkeypatch, SESSION_OPEN_SLOT, 320.0)
    assert code == 200
    assert body["committed"] is True
    assert body["pipeline_status"] == "ROW_COMMITTED"
    assert (body["step"], body["market_step"], body["market_slot_id"]) == (0, 0, "2026-07-23:9")
    assert body["clock_mode"] == "market"
    doc = FAKE_DB.reference(f"webull_lego_rows/{body['run_id']}").get()
    assert doc["market_ordinal"] == 0 and doc["market_slot_id"] == "2026-07-23:9"
    assert "order_worker" not in body      # inline dispatch is off by default


def test_scheduler_retry_in_same_slot_does_not_double_commit(monkeypatch):
    first, _ = _run(monkeypatch, SESSION_OPEN_SLOT, 320.0)
    body, code = _run(monkeypatch, datetime(2026, 7, 23, 18, 12, 41, tzinfo=UTC), 320.9)
    assert code == 200
    assert body["committed"] is False
    assert body["pipeline_status"] == "SLOT_CONSUMED"
    state = FAKE_DB.reference(f"{STATE_PATH}/{chain_key(main.load_config())}").get()
    assert state["version"] == 1 and state["last_run_id"] == first["run_id"]


def test_dna_jumps_when_scheduler_misses_slots(monkeypatch):
    _run(monkeypatch, SESSION_OPEN_SLOT, 320.0)
    # 18:30 and 19:00 never fire; the 19:30 slot is ordinal 3.
    body, code = _run(monkeypatch, datetime(2026, 7, 23, 19, 30, 5, tzinfo=UTC), 322.0)
    assert code == 200
    assert body["committed"] is True
    assert (body["step"], body["market_step"]) == (3, 3)
    assert body["legacy_step"] == 1 and body["alignment_error"] == -2


def test_calendar_change_fails_closed(monkeypatch):
    _run(monkeypatch, SESSION_OPEN_SLOT, 320.0)
    monkeypatch.setenv("LEGO_MARKET_HOLIDAYS", "2026-07-22")
    body, code = _run(monkeypatch, datetime(2026, 7, 23, 18, 30, 5, tzinfo=UTC), 321.0)
    assert code == 409
    assert body["pipeline_status"] == "CALENDAR_DRIFT"
    assert body["committed"] is False


def test_untrained_slot_size_is_a_config_error(monkeypatch):
    monkeypatch.setenv("LEGO_SLOT_SECONDS", "600")
    body, code = _run(monkeypatch, SESSION_OPEN_SLOT, 320.0)
    assert code == 500 and body["pipeline_status"] == "CONFIG_ERROR"


def test_order_worker_failure_never_blocks_the_row(monkeypatch):
    monkeypatch.setenv("LEGO_INLINE_ORDER_WORKER", "true")
    monkeypatch.setattr(main, "_run_order_worker",
                        lambda cfg, limit=1: (_ for _ in ()).throw(RuntimeError("broker down")))
    body, code = _run(monkeypatch, SESSION_OPEN_SLOT, 320.0)
    assert code == 200
    assert body["committed"] is True
    assert "broker down" in body["order_worker"]["error"]
