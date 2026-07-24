from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from conftest import FAKE_DB
from lego_one_row import Config, compute_row
from lego_outbox import list_actionable, put_intent, row_is_committed, update_intent
from lego_state import (STATE_PATH, CalendarDriftError, SlotAlreadyConsumed, chain_key,
                        commit_final_row, read_anchor)
from market_clock import (MarketClockError, _session_slots, calendar_fingerprint,
                          fallback_slot_id, market_ordinal_for_slot_id,
                          resolve_dna_step, resolve_market_slot, slot_seconds)

UTC = timezone.utc
NORMAL_SESSION = date(2026, 7, 23)        # Thursday, 09:30-16:00 ET
EARLY_CLOSE = date(2026, 11, 27)          # day after Thanksgiving, 09:30-13:00 ET
CFG = Config("AAPL", 3000.0, diff=5.0, dna_code="bypass:100", decimal_precision=2)


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    FAKE_DB.store.clear()
    monkeypatch.setenv("LEGO_SLOT_SECONDS", "1800")          # 30m, a trained timeframe
    monkeypatch.setenv("LEGO_DNA_ORIGIN_UTC", "2026-07-23T18:00:00Z")
    monkeypatch.setenv("LEGO_DNA_CLOCK_MODE", "market")
    monkeypatch.delenv("LEGO_MARKET_HOLIDAYS", raising=False)
    monkeypatch.delenv("LEGO_MARKET_EARLY_CLOSES", raising=False)


def _commit(snapshot, anchor, step, slot_id, ordinal):
    row = compute_row(CFG, snapshot, anchor, dna_step=step)
    return commit_final_row(CFG, snapshot, anchor, row,
                            slot_id=slot_id, market_ordinal=ordinal, clock_mode="market")


# --- grid must reproduce the yfinance bar count the DNA was trained on --------

@pytest.mark.parametrize("sec,normal,early", [
    (900, 26, 14),      # 15m
    (1800, 13, 7),      # 30m
    (3600, 7, 4),       # 1h  - trailing half bar counts
    (14400, 2, 1),      # 4h
    (86400, 1, 1),      # 1d
])
def test_session_slot_count_matches_trained_bars(sec, normal, early):
    assert _session_slots(NORMAL_SESSION, sec) == normal
    assert _session_slots(EARLY_CLOSE, sec) == early


def test_untrained_slot_size_is_rejected(monkeypatch):
    monkeypatch.setenv("LEGO_SLOT_SECONDS", "600")
    with pytest.raises(MarketClockError):
        slot_seconds()


def test_missing_slot_size_is_rejected(monkeypatch):
    monkeypatch.delenv("LEGO_SLOT_SECONDS", raising=False)
    with pytest.raises(MarketClockError):
        slot_seconds()


# --- clock semantics ---------------------------------------------------------

def test_market_clock_skips_missed_scheduler_slots():
    s0 = resolve_market_slot(datetime(2026, 7, 23, 18, 0, 5, tzinfo=UTC))
    s2 = resolve_market_slot(datetime(2026, 7, 23, 19, 0, 5, tzinfo=UTC))
    assert s0 is not None and s2 is not None
    assert (s0.market_ordinal, s2.market_ordinal) == (0, 2)
    effective, error = resolve_dna_step(legacy_step=1, slot=s2)
    assert effective == 2
    assert error == -1


def test_market_clock_does_not_count_overnight():
    friday_last = resolve_market_slot(datetime(2026, 7, 24, 19, 30, 5, tzinfo=UTC))
    monday_first = resolve_market_slot(datetime(2026, 7, 27, 13, 30, 5, tzinfo=UTC))
    assert friday_last is not None and monday_first is not None
    assert monday_first.market_ordinal == friday_last.market_ordinal + 1


def test_slot_id_round_trips_to_the_same_ordinal():
    slot = resolve_market_slot(datetime(2026, 7, 24, 15, 0, 5, tzinfo=UTC))
    assert market_ordinal_for_slot_id(slot.slot_id) == slot.market_ordinal


def test_fallback_slot_id_is_namespaced():
    assert fallback_slot_id("2026-07-23T18:05:00Z").startswith("epoch:")


# --- DNA time alignment, output contract -------------------------------------

def test_explicit_market_step_preserves_17_columns_and_can_jump():
    snap0 = {"captured_at": "2026-07-23T18:00:05Z", "price": 320.0, "holdings": 9.0}
    res0 = _commit(snap0, None, 0, "2026-07-23:9", 0)
    anchor = read_anchor(CFG)
    snap3 = {"captured_at": "2026-07-23T19:30:05Z", "price": 321.0, "holdings": 9.0}
    row3 = compute_row(CFG, snap3, anchor, dna_step=3)
    res3 = commit_final_row(CFG, snap3, anchor, row3,
                            slot_id="2026-07-23:12", market_ordinal=3, clock_mode="market")
    assert res0["version"] == 1 and res3["version"] == 2
    assert row3["DNA step"] == 3
    assert len([k for k in row3 if k != "_meta"]) == 17


def test_committed_row_carries_slot_provenance_outside_the_17_columns():
    snap = {"captured_at": "2026-07-23T18:00:05Z", "price": 320.0, "holdings": 9.0}
    res = _commit(snap, None, 0, "2026-07-23:9", 0)
    doc = FAKE_DB.reference(f"webull_lego_rows/{res['run_id']}").get()
    assert doc["market_slot_id"] == "2026-07-23:9"
    assert doc["market_ordinal"] == 0
    assert doc["clock_mode"] == "market"
    columns = [k for k in compute_row(CFG, snap, None, dna_step=0) if k != "_meta"]
    assert [k for k in doc if k in columns] == columns


def test_legacy_pending_order_in_state_does_not_block_new_row():
    snap0 = {"captured_at": "2026-07-23T18:00:05Z", "price": 320.0, "holdings": 9.0}
    _commit(snap0, None, 0, "2026-07-23:9", 0)
    ck = chain_key(CFG)
    state = FAKE_DB.reference(f"{STATE_PATH}/{ck}").get()
    state["pending_order"] = {"run_id": "old", "status": "SUBMITTED"}
    FAKE_DB.reference(f"{STATE_PATH}/{ck}").set(state)

    anchor = read_anchor(CFG)
    snap1 = {"captured_at": "2026-07-23T18:30:05Z", "price": 321.0, "holdings": 9.0}
    result = _commit(snap1, anchor, 1, "2026-07-23:10", 1)
    assert result["committed"] is True
    assert read_anchor(CFG).dna_step == 1


# --- one commit per slot -----------------------------------------------------

def test_scheduler_retry_cannot_consume_the_same_slot_twice():
    snap0 = {"captured_at": "2026-07-23T18:00:05Z", "price": 320.0, "holdings": 9.0}
    _commit(snap0, None, 0, "2026-07-23:9", 0)
    anchor = read_anchor(CFG)
    # Same slot, later retry: different captured_at/price gives a different run_id.
    snap_retry = {"captured_at": "2026-07-23T18:12:41Z", "price": 320.4, "holdings": 9.0}
    with pytest.raises(SlotAlreadyConsumed):
        _commit(snap_retry, anchor, 0, "2026-07-23:9", 0)
    assert read_anchor(CFG).version == 1


def test_degraded_clock_still_guards_duplicate_slots():
    snap0 = {"captured_at": "2026-07-23T18:00:05Z", "price": 320.0, "holdings": 9.0}
    slot_id = fallback_slot_id(snap0["captured_at"])
    row0 = compute_row(CFG, snap0, None, dna_step=0)
    commit_final_row(CFG, snap0, None, row0, slot_id=slot_id, clock_mode="shadow:degraded")
    anchor = read_anchor(CFG)
    snap_retry = {"captured_at": "2026-07-23T18:20:00Z", "price": 320.4, "holdings": 9.0}
    assert fallback_slot_id(snap_retry["captured_at"]) == slot_id
    row_retry = compute_row(CFG, snap_retry, anchor, dna_step=1)
    with pytest.raises(SlotAlreadyConsumed):
        commit_final_row(CFG, snap_retry, anchor, row_retry,
                         slot_id=slot_id, clock_mode="shadow:degraded")


# --- calendar drift ----------------------------------------------------------

def test_declaring_a_new_holiday_fails_closed_instead_of_rephasing(monkeypatch):
    snap0 = {"captured_at": "2026-07-23T18:00:05Z", "price": 320.0, "holdings": 9.0}
    _commit(snap0, None, 0, "2026-07-23:9", 0)
    anchor = read_anchor(CFG)
    monkeypatch.setenv("LEGO_MARKET_HOLIDAYS", "2026-07-22")
    snap1 = {"captured_at": "2026-07-23T18:30:05Z", "price": 321.0, "holdings": 9.0}
    with pytest.raises(CalendarDriftError):
        _commit(snap1, anchor, 1, "2026-07-23:10", 1)


def test_slot_that_no_longer_recomputes_fails_closed():
    snap0 = {"captured_at": "2026-07-23T18:00:05Z", "price": 320.0, "holdings": 9.0}
    _commit(snap0, None, 0, "2026-07-23:9", 0)
    ref = FAKE_DB.reference(f"{STATE_PATH}/{chain_key(CFG)}")
    state = ref.get()
    state["market_ordinal"] = 999          # as if the calendar had shifted the chain
    ref.set(state)
    anchor = read_anchor(CFG)
    snap1 = {"captured_at": "2026-07-23T18:30:05Z", "price": 321.0, "holdings": 9.0}
    with pytest.raises(CalendarDriftError):
        _commit(snap1, anchor, 1, "2026-07-23:10", 1)


def test_engine_only_commit_skips_calendar_guard():
    """Pure-engine callers (no clock) keep working exactly as before."""
    snap0 = {"captured_at": "2026-07-23T18:00:05Z", "price": 320.0, "holdings": 9.0}
    row0 = compute_row(CFG, snap0, None, dna_step=0)
    assert commit_final_row(CFG, snap0, None, row0)["committed"] is True
    assert "calendar_fingerprint" not in FAKE_DB.reference(f"{STATE_PATH}/{chain_key(CFG)}").get()


def test_calendar_fingerprint_tracks_slot_size(monkeypatch):
    before = calendar_fingerprint()
    monkeypatch.setenv("LEGO_SLOT_SECONDS", "3600")
    assert calendar_fingerprint() != before


def test_calendar_fingerprint_ignores_origin_spelling(monkeypatch):
    before = calendar_fingerprint()
    monkeypatch.setenv("LEGO_DNA_ORIGIN_UTC", "2026-07-23T18:00:00+00:00")
    assert calendar_fingerprint() == before


def test_degraded_commit_does_not_pin_a_calendar():
    snap0 = {"captured_at": "2026-07-23T18:00:05Z", "price": 320.0, "holdings": 9.0}
    row0 = compute_row(CFG, snap0, None, dna_step=0)
    commit_final_row(CFG, snap0, None, row0,
                     slot_id=fallback_slot_id(snap0["captured_at"]),
                     clock_mode="shadow:degraded")
    state = FAKE_DB.reference(f"{STATE_PATH}/{chain_key(CFG)}").get()
    assert state["slot_id"].startswith("epoch:")
    assert "calendar_fingerprint" not in state


# --- outbox stays independent from the DNA pointer ---------------------------

def test_outbox_supports_multiple_slots_without_overwrite():
    ck = "AAPL_test"
    put_intent(ck, "r1", {"status": "PENDING_DISPATCH", "created_at": "2026-07-23T18:00:00Z"})
    put_intent(ck, "r2", {"status": "PENDING_DISPATCH", "created_at": "2026-07-23T18:30:00Z"})
    assert [x["run_id"] for x in list_actionable(ck)] == ["r1", "r2"]
    update_intent(ck, "r1", {"status": "EXPIRED_UNSENT"})
    assert [x["run_id"] for x in list_actionable(ck)] == ["r2"]


def test_outbox_worker_must_require_committed_source_row():
    assert row_is_committed("missing") is False
