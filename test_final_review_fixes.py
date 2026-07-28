"""Regression tests for the final money-path and identity review."""
from __future__ import annotations

import threading
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

import lego_outbox
import lego_state
import main
import webull_io
from conftest import FAKE_DB, FakeReference
from lego_one_row import compute_row
from lego_outbox import (OUTBOX_PATH, begin_place_attempt, claim_intent,
                         put_intent, release_intent_claim, update_intent)
from lego_state import (REALIZED_PATH, STATE_PATH, RuntimeIdentityMismatch,
                        apply_realized_fill, chain_key, commit_final_row,
                        read_anchor)


UTC = timezone.utc
SLOT = datetime(2026, 7, 23, 18, 0, 5, tzinfo=UTC)


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
        "LEGO_SYMBOL": "AAPL",
        "LEGO_FIX_C": "3000",
        "LEGO_DIFF": "5",
        "LEGO_DNA_CODE": "bypass:100",
        "LEGO_DECIMAL_PRECISION": "2",
        "LEGO_SLOT_SECONDS": "1800",
        "LEGO_DNA_ORIGIN_UTC": "2026-07-23T18:00:00Z",
        "LEGO_DNA_CLOCK_MODE": "market",
        "FIREBASE_DB_URL": "https://x.firebaseio.com",
        "WEBULL_ENV": "UAT",
        "WEBULL_ACCOUNT_ID": "review-account-a",
    }.items():
        monkeypatch.setenv(key, value)
    for key in ("AUTO_SUBMIT", "LEGO_INLINE_ORDER_WORKER"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(main, "build_clients", lambda: (object(), object()))
    monkeypatch.setattr(
        main, "token_health", lambda: {"ok": True, "ready": True, "reasons": []})


def _run_row(monkeypatch, *, price=320.0, holdings=9.0):
    monkeypatch.setattr(main, "datetime", _fixed_now(SLOT))
    monkeypatch.setattr(main, "fetch_snapshot", lambda _t, _d, _cfg: {
        "captured_at": SLOT.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "price": price,
        "holdings": holdings,
    })
    return main.lego_one_row(object())


def test_invalid_environment_is_config_error_and_never_reads_broker(monkeypatch):
    monkeypatch.setenv("WEBULL_ENV", "STAGING")
    touched = []
    monkeypatch.setattr(
        main, "build_clients", lambda: touched.append(True) or (object(), object()))

    body, code = _run_row(monkeypatch)

    assert code == 500
    assert body["status"] == body["pipeline_status"] == "CONFIG_ERROR"
    assert touched == []
    assert FAKE_DB.reference("webull_lego_rows").get() is None


def test_runtime_identity_is_opaque_alias_stable_and_account_specific(monkeypatch):
    first = webull_io.runtime_identity_fingerprint()
    assert "review-account-a" not in first
    assert len(first) == 64

    monkeypatch.setenv("WEBULL_ENV", "PROD")
    prod = webull_io.runtime_identity_fingerprint()
    monkeypatch.setenv("WEBULL_ENV", "PRODUCTION")
    assert webull_io.runtime_identity_fingerprint() == prod
    assert prod != first

    monkeypatch.setenv("WEBULL_ENV", "UAT")
    monkeypatch.setenv("WEBULL_ACCOUNT_ID", "review-account-b")
    assert webull_io.runtime_identity_fingerprint() != first


def test_runtime_identity_mismatch_blocks_existing_chain_without_leaking_id():
    cfg = main.load_config()
    snap = {
        "captured_at": "2026-07-23T18:00:05Z",
        "price": 320.0,
        "holdings": 9.0,
    }
    row = compute_row(cfg, snap, None, dna_step=0)
    commit_final_row(
        cfg, snap, None, row, runtime_identity="a" * 64)

    with pytest.raises(RuntimeIdentityMismatch) as raised:
        read_anchor(cfg, runtime_identity="b" * 64)
    assert "review-account" not in str(raised.value)


def test_legacy_chain_is_adopted_once_and_says_so(monkeypatch):
    """A pre-guard chain keeps running; the DNA clock never stops to wait."""
    cfg = main.load_config()
    snap0 = {
        "captured_at": "2026-07-23T18:00:05Z",
        "price": 320.0,
        "holdings": 9.0,
    }
    row0 = compute_row(cfg, snap0, None, dna_step=0)
    commit_final_row(cfg, snap0, None, row0)
    identity = webull_io.runtime_identity_fingerprint()

    stored = FAKE_DB.reference(f"{STATE_PATH}/{chain_key(cfg)}").get()
    assert "runtime_identity_fingerprint" not in stored
    assert lego_state.verify_runtime_identity(stored, identity) is True

    anchor = read_anchor(cfg, runtime_identity=identity)
    assert anchor is not None and anchor.version == 1

    body, code = _run_row(monkeypatch, price=321.0)
    assert code == 200 and body["committed"] is True

    stored = FAKE_DB.reference(f"{STATE_PATH}/{chain_key(cfg)}").get()
    assert stored["runtime_identity_fingerprint"] == identity
    # Said out loud exactly once, and without the raw account id.
    warned = FAKE_DB.reference(f"{main.WARNINGS_PATH}/runtime_identity_adopted").get()
    assert warned["count"] == 1
    assert identity[:8] in json.dumps(warned) and "review-account" not in json.dumps(warned)

    # Adoption is not a permanent bypass: it is stamped, so the guard now bites.
    assert lego_state.verify_runtime_identity(stored, identity) is False
    with pytest.raises(RuntimeIdentityMismatch):
        read_anchor(cfg, runtime_identity="c" * 64)


def _count_state_reads(monkeypatch, cfg):
    """Count how many times one tick fetches this chain's state document."""
    path = f"{STATE_PATH}/{chain_key(cfg)}"
    reads = []
    real_get = FakeReference.get

    def counting_get(self):
        if "/".join(self.parts) == path:
            reads.append(1)
        return real_get(self)

    monkeypatch.setattr(FakeReference, "get", counting_get)
    return reads


def test_one_tick_reads_the_chain_state_exactly_three_times(monkeypatch):
    """Three reads: one shared by the guards, then the commit's own two.

    The identity guard, the pending-intent recovery and the anchor all need the
    same document, so they share one read. commit_final_row's pre-transaction
    read and the transaction itself must both see a fresh document — that is the
    stale-anchor guard — so they stay. A fourth read means a guard fetched again
    instead of taking the document it was handed.
    """
    cfg = main.load_config()
    reads = _count_state_reads(monkeypatch, cfg)
    body, code = _run_row(monkeypatch)
    assert code == 200 and body["committed"] is True
    assert len(reads) == 3


def test_order_worker_reads_the_chain_state_once(monkeypatch):
    cfg = main.load_config()
    _run_row(monkeypatch)
    reads = _count_state_reads(monkeypatch, cfg)
    assert main._run_order_worker(cfg, limit=1)["processed"] == 0
    assert len(reads) == 1


def test_committed_row_recovers_intent_after_outbox_write_crash(monkeypatch):
    monkeypatch.setenv("AUTO_SUBMIT", "true")
    real_put = lego_outbox.put_intent

    def crash_after_commit(*_args, **_kwargs):
        raise RuntimeError("fault after state transaction")

    monkeypatch.setattr(main, "put_intent", crash_after_commit)
    body, code = _run_row(monkeypatch)
    assert code == 200 and body["committed"] is True
    assert "fault after state transaction" in body["outbox_error"]

    cfg = main.load_config()
    ck = chain_key(cfg)
    state = FAKE_DB.reference(f"{STATE_PATH}/{ck}").get()
    assert body["run_id"] in state["pending_order_intents"]
    assert FAKE_DB.reference(f"{OUTBOX_PATH}/{ck}/{body['run_id']}").get() is None

    monkeypatch.setattr(main, "put_intent", real_put)
    recovered = main._recover_pending_order_intents(
        cfg, webull_io.runtime_identity_fingerprint())
    assert recovered == 1
    assert FAKE_DB.reference(
        f"{OUTBOX_PATH}/{ck}/{body['run_id']}").get()["status"] == "PENDING_DISPATCH"
    assert "pending_order_intents" not in FAKE_DB.reference(
        f"{STATE_PATH}/{ck}").get()


def test_claim_lease_is_atomic_under_real_threads(monkeypatch):
    class AtomicReference:
        def __init__(self):
            self.value = {
                "run_id": "r1",
                "status": "PENDING_DISPATCH",
            }
            self.lock = threading.Lock()

        def transaction(self, fn):
            with self.lock:
                self.value = fn(dict(self.value))
                return dict(self.value)

    ref = AtomicReference()
    monkeypatch.setattr(
        lego_outbox.db, "reference", lambda _path: ref)
    barrier = threading.Barrier(2)

    def compete(worker):
        barrier.wait()
        return claim_intent(
            "ck", "r1", worker, now_utc=SLOT, lease_seconds=60)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(compete, ("w1", "w2")))

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0]["status"] == "PENDING_DISPATCH"
    winner = winners[0]["claim_owner"]
    release_intent_claim("ck", "r1", winner)
    assert "claim_owner" not in ref.value


def test_expired_claim_generation_cannot_cross_the_irreversible_place_fence():
    put_intent("ck", "r1", {"status": "PENDING_DISPATCH"})
    first = claim_intent(
        "ck", "r1", "w1", now_utc=SLOT, lease_seconds=1)
    second = claim_intent(
        "ck", "r1", "w2", now_utc=SLOT + timedelta(seconds=2),
        lease_seconds=60)
    assert first is not None and second is not None

    assert begin_place_attempt(
        "ck", "r1", "w1", first["claim_generation"]) is None
    started = begin_place_attempt(
        "ck", "r1", "w2", second["claim_generation"])
    assert started is not None
    assert started["status"] == "PLACING_UNKNOWN"


def test_two_concurrent_workers_place_exactly_once(monkeypatch):
    cfg = main.load_config()
    identity = webull_io.runtime_identity_fingerprint()
    ck = chain_key(cfg)
    run_id = "a" * 32
    row = compute_row(cfg, {
        "captured_at": "2026-07-23T18:00:05Z",
        "price": 320.0,
        "holdings": 9.0,
    }, None, dna_step=0)
    FAKE_DB.reference(f"{STATE_PATH}/{ck}").set({
        "version": 1,
        "dna_step": 0,
        "p0": 320.0,
        "prev_price": 320.0,
        "prev_actual": 0.0,
        "prev_holdings": 9.0,
        "runtime_identity_fingerprint": identity,
    })
    FAKE_DB.reference(f"webull_lego_rows/{run_id}").set({
        **{key: value for key, value in row.items() if key != "_meta"},
        "committed": True,
    })
    put_intent(ck, run_id, {
        "status": "PENDING_DISPATCH",
        "row_status": row["สถานะ"],
        "side": row["_meta"]["side"],
        "quantity": row["_meta"]["quantity"],
        "symbol": cfg.symbol,
        "step": row["DNA step"],
        "decision_holdings": 9.0,
        "created_at": "2026-07-23T18:00:05Z",
        "expires_at": "2099-07-23T18:30:00Z",
    })

    transaction_lock = threading.RLock()
    original_transaction = FakeReference.transaction

    def atomic_transaction(self, fn):
        with transaction_lock:
            return original_transaction(self, fn)

    monkeypatch.setattr(FakeReference, "transaction", atomic_transaction)
    monkeypatch.setattr(main, "fetch_open_orders", lambda _tc, _symbol: [])
    monkeypatch.setattr(main, "fetch_snapshot", lambda _tc, _dc, _cfg: {
        "captured_at": "2026-07-23T18:00:06Z",
        "price": 320.0,
        "holdings": 9.0,
    })
    monkeypatch.setattr(main, "preview_market_order", lambda _tc, _order: True)
    monkeypatch.setattr(main, "fetch_order_detail", lambda _tc, _run_id: {
        "order_status": "FILLED",
        "filled_quantity": row["_meta"]["quantity"],
        "avg_filled_price": 320.0,
    })
    placed = []
    placed_lock = threading.Lock()

    def place(_tc, _order):
        with placed_lock:
            placed.append(run_id)
        return {"order_status": "FILLED"}

    monkeypatch.setattr(main, "place_market_order", place)
    original_list = main.list_actionable
    barrier = threading.Barrier(2)

    def simultaneous_list(*args, **kwargs):
        docs = original_list(*args, **kwargs)
        barrier.wait(timeout=5)
        return docs

    monkeypatch.setattr(main, "list_actionable", simultaneous_list)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda _: main._run_order_worker(
                cfg, limit=1, runtime_identity=identity),
            range(2),
        ))

    assert len(placed) == 1
    assert sum(result["processed"] for result in results) == 1
    assert FAKE_DB.reference(
        f"{OUTBOX_PATH}/{ck}/{run_id}").get()["status"] == "FILLED"


def test_audit_failure_is_repaired_from_authoritative_outbox(monkeypatch):
    put_intent("ck", "r1", {
        "status": "PENDING_DISPATCH",
        "created_at": "2026-07-23T18:00:00Z",
    })
    real_update = lego_state.update_order_audit

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(main, "update_order_audit", fail_audit)
    main._persist("ck", "r1", {
        "status": "PLACING_UNKNOWN",
        "place_attempted": True,
    })
    intent = FAKE_DB.reference(f"{OUTBOX_PATH}/ck/r1").get()
    assert intent["status"] == "PLACING_UNKNOWN"
    assert intent["audit_pending"] is True

    monkeypatch.setattr(main, "update_order_audit", real_update)
    assert main._repair_pending_audits("ck") == 1
    intent = FAKE_DB.reference(f"{OUTBOX_PATH}/ck/r1").get()
    audit = FAKE_DB.reference("webull_lego_order_audit/r1").get()
    assert intent["audit_pending"] is False
    assert audit["status"] == "PLACING_UNKNOWN"


def test_persisted_and_returned_errors_redact_credentials(monkeypatch):
    monkeypatch.setenv("WEBULL_APP_SECRET", "top-secret-value")
    put_intent("ck", "r1", {"status": "PENDING_DISPATCH"})
    result = main._persist_error(
        "ck", "r1", "NOT_PLACED",
        RuntimeError(
            "account_id=review-account-a app_secret=top-secret-value"),
    )
    serialized = json.dumps({
        "result": result,
        "outbox": FAKE_DB.reference(f"{OUTBOX_PATH}/ck/r1").get(),
        "audit": FAKE_DB.reference("webull_lego_order_audit/r1").get(),
    })
    assert "review-account-a" not in serialized
    assert "top-secret-value" not in serialized
    assert "<redacted>" in serialized


def test_incomplete_pagination_keeps_intent_pending_and_never_places(monkeypatch):
    cfg = main.load_config()
    run_id = "b" * 32
    ck = chain_key(cfg)
    FAKE_DB.reference(f"webull_lego_rows/{run_id}").set({"committed": True})
    intent = put_intent(ck, run_id, {
        "status": "PENDING_DISPATCH",
        "expires_at": "2099-07-23T18:30:00Z",
    })
    monkeypatch.setattr(
        main, "fetch_open_orders",
        lambda *_args: (_ for _ in ()).throw(
            webull_io.IncompleteOpenOrdersError("truncated")),
    )
    placed = []
    monkeypatch.setattr(
        main, "place_market_order", lambda *_args: placed.append(True))

    result = main._dispatch_or_reconcile_one(object(), object(), cfg, intent)

    assert result["status"] == "PENDING_DISPATCH"
    assert placed == []
    stored = FAKE_DB.reference(f"{OUTBOX_PATH}/{ck}/{run_id}").get()
    assert stored["status"] == "PENDING_DISPATCH"
    assert stored["pagination_complete"] is False


def test_missing_fill_fields_become_terminal_manual_check():
    intent = put_intent("ck", "r1", {
        "status": "PLACING_UNKNOWN",
        "side": "BUY",
    })
    result = main._finish_with_realized(intent, {
        "status": "FILLED",
        "realized": True,
    })
    assert result["status"] == "REALIZED_MATH_ERROR"
    assert result["needs_manual_check"] is True
    assert result["broker_status"] == "FILLED"


def test_fee_only_update_and_replay_are_counted_exactly_once():
    buy = apply_realized_fill("ck", "buy", "BUY", 1.0, 100.0, 0.0)
    assert buy["realized_delta"] == 0.0
    replay = apply_realized_fill("ck", "buy", "BUY", 1.0, 100.0, 0.0)
    assert replay["realized_delta"] == 0.0

    late_fee = apply_realized_fill("ck", "buy", "BUY", 1.0, 100.0, 0.25)
    assert late_fee["realized_delta"] == pytest.approx(-0.25)
    assert late_fee["realized_cumulative"] == pytest.approx(-0.25)
    replay_fee = apply_realized_fill("ck", "buy", "BUY", 1.0, 100.0, 0.25)
    assert replay_fee["realized_delta"] == 0.0
    assert replay_fee["realized_cumulative"] == pytest.approx(-0.25)
    with pytest.raises(ValueError, match="average fill price"):
        apply_realized_fill("ck", "buy", "BUY", 1.0, 101.0, 0.25)

    close = apply_realized_fill("ck", "sell", "SELL", 1.0, 110.0, 0.10)
    assert close["realized_cumulative"] == pytest.approx(9.65)
    stored = FAKE_DB.reference(f"{REALIZED_PATH}/ck").get()
    assert stored["applied_fills"]["buy"]["fee"] == pytest.approx(0.25)


def test_terminal_outbox_status_cannot_be_reopened():
    put_intent("ck", "r1", {"status": "PENDING_DISPATCH"})
    update_intent("ck", "r1", {"status": "FILLED"})
    update_intent("ck", "r1", {"status": "PENDING_DISPATCH"})
    assert FAKE_DB.reference(f"{OUTBOX_PATH}/ck/r1").get()["status"] == "FILLED"
