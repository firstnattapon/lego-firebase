from __future__ import annotations

from datetime import datetime, timezone

import pytest

from conftest import FAKE_DB
from lego_one_row import Config, compute_row
from lego_outbox import list_actionable, put_intent, row_is_committed, update_intent
from lego_state import STATE_PATH, chain_key, commit_final_row, read_anchor
from market_clock import resolve_dna_step, resolve_market_slot

UTC = timezone.utc


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    FAKE_DB.store.clear()
    monkeypatch.setenv("LEGO_SLOT_SECONDS", "600")
    monkeypatch.setenv("LEGO_DNA_ORIGIN_UTC", "2026-07-23T18:00:00Z")
    monkeypatch.setenv("LEGO_DNA_CLOCK_MODE", "market")


def test_market_clock_skips_missed_scheduler_slots():
    s0 = resolve_market_slot(datetime(2026, 7, 23, 18, 0, 5, tzinfo=UTC))
    s3 = resolve_market_slot(datetime(2026, 7, 23, 18, 30, 5, tzinfo=UTC))
    assert s0 is not None and s3 is not None
    assert s0.market_ordinal == 0
    assert s3.market_ordinal == 3
    effective, error = resolve_dna_step(legacy_step=1, slot=s3)
    assert effective == 3
    assert error == -2


def test_market_clock_does_not_count_overnight():
    friday_last = resolve_market_slot(datetime(2026, 7, 24, 19, 50, 5, tzinfo=UTC))
    monday_first = resolve_market_slot(datetime(2026, 7, 27, 13, 30, 5, tzinfo=UTC))
    assert friday_last is not None and monday_first is not None
    assert monday_first.market_ordinal == friday_last.market_ordinal + 1


def test_explicit_market_step_preserves_17_columns_and_can_jump():
    cfg = Config("AAPL", 3000.0, diff=5.0, dna_code="bypass:100", decimal_precision=2)
    snap0 = {"captured_at": "2026-07-23T18:00:05Z", "price": 320.0, "holdings": 9.0}
    row0 = compute_row(cfg, snap0, None, dna_step=0)
    res0 = commit_final_row(cfg, snap0, None, row0)
    anchor = read_anchor(cfg)
    snap3 = {"captured_at": "2026-07-23T18:30:05Z", "price": 321.0, "holdings": 9.0}
    row3 = compute_row(cfg, snap3, anchor, dna_step=3)
    res3 = commit_final_row(cfg, snap3, anchor, row3)
    assert res0["version"] == 1 and res3["version"] == 2
    assert row3["DNA step"] == 3
    assert len([k for k in row3 if k != "_meta"]) == 17


def test_legacy_pending_order_in_state_does_not_block_new_row():
    cfg = Config("AAPL", 3000.0, diff=5.0, dna_code="bypass:100", decimal_precision=2)
    snap0 = {"captured_at": "2026-07-23T18:00:05Z", "price": 320.0, "holdings": 9.0}
    row0 = compute_row(cfg, snap0, None, dna_step=0)
    commit_final_row(cfg, snap0, None, row0)
    ck = chain_key(cfg)
    state = FAKE_DB.reference(f"{STATE_PATH}/{ck}").get()
    state["pending_order"] = {"run_id": "old", "status": "SUBMITTED"}
    FAKE_DB.reference(f"{STATE_PATH}/{ck}").set(state)

    anchor = read_anchor(cfg)
    snap1 = {"captured_at": "2026-07-23T18:10:05Z", "price": 321.0, "holdings": 9.0}
    row1 = compute_row(cfg, snap1, anchor, dna_step=1)
    result = commit_final_row(cfg, snap1, anchor, row1, order_intent=None)
    assert result["committed"] is True
    assert read_anchor(cfg).dna_step == 1


def test_outbox_supports_multiple_slots_without_overwrite():
    ck = "AAPL_test"
    put_intent(ck, "r1", {"status": "PENDING_DISPATCH", "created_at": "2026-07-23T18:00:00Z"})
    put_intent(ck, "r2", {"status": "PENDING_DISPATCH", "created_at": "2026-07-23T18:10:00Z"})
    assert [x["run_id"] for x in list_actionable(ck)] == ["r1", "r2"]
    update_intent(ck, "r1", {"status": "EXPIRED_UNSENT"})
    assert [x["run_id"] for x in list_actionable(ck)] == ["r2"]


def test_outbox_worker_must_require_committed_source_row():
    assert row_is_committed("missing") is False
