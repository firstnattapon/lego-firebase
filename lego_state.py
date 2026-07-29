"""RTDB persistence, durable pending-order outbox, and realized fill ledger."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import uuid
from datetime import datetime, timezone

from firebase_admin import db

from dna_engine import dna_fingerprint
from lego_one_row import (ACTUAL_COLUMN, DELTA_COLUMN, EXCESS_COLUMN,
                          REFERENCE_COLUMN, Anchor, Config, ExecutionFill,
                          finalize_recurrence, validate_row_columns)
from lego_orders import apply_fill, normalize_status
from market_clock import calendar_fingerprint, market_ordinal_for_slot_id
from webull_io import redact_sensitive_text

ROWS_PATH = "webull_lego_rows"
STATE_PATH = "webull_lego_state"
AUDIT_PATH = "webull_lego_order_audit"
REALIZED_PATH = "webull_lego_realized"
# Every cashflow accounting this chain family has ever written, oldest first.
# The order is the whole point: each name means a different thing by "Aₙ", so a
# chain may be carried *forward* through this list — read_anchor restarts its
# baseline at the boundary — but never backwards.
#
# Backwards is not hypothetical. A revision that predates the split books a
# decision (or, further back, every row) as an act, and Cloud Run keeps an old
# revision serving until something takes its traffic away. Pointed at a chain
# that has already moved to execution-confirmed accounting, it writes ΔAₙ on
# PASS rows and drags P_acted forward with them, and every guard in this module
# lets it: the version increments, the slot is fresh, the calendar and the DNA
# still match. Only the semantics name disagrees, so that name is the fence.
CASHFLOW_SEMANTICS_HISTORY = (
    "cycle_realized_v1",        # ΔAₙ from FIFO-matched broker cycles
    "gated_theoretical_v2",     # a READY_* decision advanced Aₙ by itself
    "execution_confirmed_v1",   # only a broker-confirmed fill advances Aₙ
)
CASHFLOW_SEMANTICS = CASHFLOW_SEMANTICS_HISTORY[-1]

# Execution cashflow state, owned by lego_order_worker and nested under its own
# key so the decision pointer (version, dna_step, p0, slot_id, market_ordinal)
# and the money that has actually moved are never written by the same author.
EXECUTION_STATE_KEY = "execution_cashflow"
# How many finalized run_ids to remember. Only re-finalization of the *same*
# run_id has to be refused, and an intent leaves the actionable queue the moment
# it finalizes, so the window only has to outlive one intent — this outlives
# weeks of them.
FINALIZED_RUN_HISTORY = 200

# Row-level provenance, stored beside run_id/version and never as an 18th column.
CASHFLOW_NO_ACTION = "NO_ACTION"           # PASS row: nothing to execute
CASHFLOW_PENDING = "PENDING_EXECUTION"     # READY_*: waiting for a broker fill
CASHFLOW_FINALIZED = "FINALIZED"           # fill confirmed, ΔAₙ/Aₙ/Eₙ written


class StaleAnchorError(RuntimeError):
    pass


class SlotAlreadyConsumed(RuntimeError):
    pass


class CalendarDriftError(RuntimeError):
    """The market calendar no longer reproduces this chain's committed slots."""


class DNADriftError(RuntimeError):
    """The same dna_code no longer decodes to the gate array this chain traded.

    Same family as CalendarDriftError: an input the chain was built on changed
    underneath it, so continuing would trade a different strategy under the same
    name. Fail closed and let a human decide.
    """


class OrdinalRegression(RuntimeError):
    """A later commit resolved to an ordinal at or before the chain's last one.

    market_ordinal(t) must reproduce the bar index the DNA was trained on, so it
    may only move forward. Letting it move back would replay an older gate on a
    newer slot, which is a corrupt chain, not a retry.
    """


class RuntimeIdentityError(RuntimeError):
    """The persisted chain cannot safely be used by this account/environment."""


class RuntimeIdentityMismatch(RuntimeIdentityError):
    """The chain was created under another opaque runtime identity."""


class CashflowSemanticsDowngrade(RuntimeError):
    """An older cashflow accounting tried to write a chain that moved past it.

    Same family as CalendarDriftError and DNADriftError — an input the chain was
    built on changed underneath it — except the input here is the running code.
    """


class ExecutionFinalizeError(RuntimeError):
    """A confirmed fill could not be booked against this chain's cashflow."""


class _Idempotent(Exception):
    pass


def config_hash(cfg: Config) -> str:
    payload = json.dumps(
        {"s": cfg.strategy_id, "sym": cfg.symbol, "fix": cfg.fix_c,
         "diff": cfg.diff, "dp": cfg.decimal_precision, "dna": cfg.dna_code},
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def chain_key(cfg: Config) -> str:
    return f"{cfg.symbol}_{config_hash(cfg)}"


def verify_runtime_identity(state: dict | None,
                            runtime_identity: str | None) -> bool:
    """Guard a chain against another account/environment; adopt a legacy one.

    A chain written before this guard existed carries no fingerprint. Refusing it
    buys nothing — the account that wrote it is unknowable either way — while a
    hard failure would stop the DNA clock every slot until an operator noticed.
    So a missing fingerprint is adopted (the caller stamps it on the next commit)
    and reported; only a fingerprint that is present and different is a real
    cross-account collision, and that still fails closed.

    Returns True when this is the first adoption, so the caller can say it out
    loud once. Never logs or persists the raw broker account id.
    """
    if not state or runtime_identity is None:
        return False
    stored = state.get("runtime_identity_fingerprint")
    if not stored:
        return True
    if not hmac.compare_digest(str(stored), str(runtime_identity)):
        raise RuntimeIdentityMismatch(
            "runtime identity ไม่ตรงกับ chain ที่บันทึกไว้ "
            "(account/environment คนละชุด; account id ไม่ถูกเปิดเผย)")
    return False


def make_run_id(ck: str, anchor_version: int | None, snapshot: dict) -> str:
    raw = (f"{ck}|{anchor_version}|{snapshot['captured_at']}|"
           f"{snapshot['price']}|{snapshot.get('holdings', 0)}")
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def verify_calendar_continuity(state: dict | None) -> None:
    """Fail closed when the calendar would re-phase an existing chain.

    Two independent checks: the stored fingerprint (slot size, origin, declared
    holidays, rules version) and a recompute of the last committed slot id.
    """
    if not state:
        return
    stored = state.get("calendar_fingerprint")
    if stored and stored != calendar_fingerprint():
        raise CalendarDriftError(
            f"calendar/slot config เปลี่ยน: fingerprint {stored} -> {calendar_fingerprint()} "
            "(DNA จะเลื่อน phase ถาวร) ต้องเริ่ม chain ใหม่หรือคืนค่าเดิม")
    slot_id = state.get("slot_id")
    ordinal = state.get("market_ordinal")
    if not slot_id or ordinal is None or str(slot_id).startswith("epoch:"):
        return
    recomputed = market_ordinal_for_slot_id(str(slot_id))
    if recomputed != int(ordinal):
        raise CalendarDriftError(
            f"slot {slot_id} เคย commit เป็น ordinal {int(ordinal)} แต่คำนวณใหม่ได้ {recomputed}")


def verify_dna_continuity(cfg: Config, state: dict | None) -> None:
    """Fail closed when the same dna_code stops decoding to the traded array.

    Runs on every commit, not only clock-resolved ones: the gate array decides
    the row regardless of which slot it landed on. Chains written before the
    field simply have nothing to compare — the next commit records it.
    """
    if not state:
        return
    stored = state.get("dna_fingerprint")
    if stored and stored != dna_fingerprint(cfg.dna_code):
        raise DNADriftError(
            f"dna_code เดิมแต่ decode ได้ gate array คนละชุด: {stored} -> "
            f"{dna_fingerprint(cfg.dna_code)} (มักเกิดจาก numpy เปลี่ยนเวอร์ชัน) "
            "— chain นี้จะกลายเป็นคนละกลยุทธ์ ต้องคืน numpy เดิมหรือเริ่ม chain ใหม่")


def cashflow_semantics_rank(name: str | None) -> int | None:
    """Position of *name* in CASHFLOW_SEMANTICS_HISTORY, or None if unknown."""
    if not name:
        return None
    try:
        return CASHFLOW_SEMANTICS_HISTORY.index(str(name))
    except ValueError:
        return None


def verify_cashflow_semantics(state: dict | None) -> str | None:
    """Fail closed when older accounting tries to write a newer chain.

    Returns the name being migrated *from* when this runtime is legitimately
    ahead of the chain, so the caller can say the baseline reset out loud once —
    read_anchor drops Aₙ to zero at that boundary, and a silent reset looks
    exactly like the corruption this guard exists to stop. Returns None when
    there is nothing to report: an absent state, a chain written before the
    field existed, or a chain already on this runtime's semantics.

    An unrecognised name is refused rather than adopted. It can only come from
    code this deployment does not have, and guessing which side of the split it
    sits on is precisely the guess that produces an unexplainable ledger.
    """
    if not state:
        return None
    stored = state.get("cashflow_semantics")
    if not stored or str(stored) == CASHFLOW_SEMANTICS:
        return None
    stored_rank = cashflow_semantics_rank(stored)
    if stored_rank is None:
        raise CashflowSemanticsDowngrade(
            f"chain ใช้ cashflow semantics '{stored}' ที่ deployment นี้ไม่รู้จัก "
            f"(รู้จัก: {', '.join(CASHFLOW_SEMANTICS_HISTORY)}) — "
            "อาจมี revision อื่นเขียน RTDB อยู่ ต้องตรวจ deployment ก่อน")
    if stored_rank > cashflow_semantics_rank(CASHFLOW_SEMANTICS):
        raise CashflowSemanticsDowngrade(
            f"chain เดินไปถึง '{stored}' แล้ว แต่ runtime นี้เป็น "
            f"'{CASHFLOW_SEMANTICS}' ซึ่งเก่ากว่า — revision เก่าห้ามเขียนทับ "
            "ledger ที่ใหม่กว่า (ตรวจว่า revision ไหนยังรับ traffic อยู่)")
    return str(stored)


class _Unread:
    """Distinguishes 'caller passed no state' from a real empty/absent state."""

    def __repr__(self) -> str:                # pragma: no cover - debug aid only
        return "UNREAD_STATE"


UNREAD_STATE = _Unread()


def read_chain_state(cfg: Config) -> dict | None:
    """The one place a caller fetches this chain's state document.

    Every guard on the read path needs the same document, so callers fetch it
    once and hand it down instead of paying a round trip per guard.
    """
    return db.reference(f"{STATE_PATH}/{chain_key(cfg)}").get()


def _resolve_state(cfg: Config, state) -> dict | None:
    return read_chain_state(cfg) if state is UNREAD_STATE else state


def execution_cashflow(state: dict) -> dict:
    """This chain's confirmed cashflow: the only source of P_acted and Aₙ.

    Both values move on a broker-confirmed fill and on nothing else, so they
    live under their own key with the worker as sole author. A chain written
    before the split still holds them in the flat prev_price/prev_actual
    fields; they are read once here as the seed and stored under the new key by
    the next commit. prev_actual only survives that migration when the chain
    already used this module's semantics — the previous name counted a decision
    as an act, so its Aₙ is a different quantity and must not be chained onto.
    """
    stored = (state or {}).get(EXECUTION_STATE_KEY)
    if isinstance(stored, dict) and stored.get("last_action_price") is not None:
        return {
            "last_action_price": float(stored["last_action_price"]),
            "actual_cumulative": float(stored.get("actual_cumulative", 0.0) or 0.0),
        }
    same_semantics = state.get("cashflow_semantics") == CASHFLOW_SEMANTICS
    return {
        "last_action_price": float(state["prev_price"]),
        "actual_cumulative": float(state["prev_actual"]) if same_semantics else 0.0,
    }


def read_anchor(cfg: Config, *, runtime_identity: str | None = None,
                state=UNREAD_STATE) -> Anchor | None:
    state = _resolve_state(cfg, state)
    if not state:
        return None
    verify_runtime_identity(state, runtime_identity)
    ph = state.get("prev_holdings")
    cashflow = execution_cashflow(state)
    return Anchor(
        version=int(state["version"]),
        dna_step=int(state["dna_step"]),
        p0=float(state["p0"]),
        prev_price=cashflow["last_action_price"],
        prev_actual=cashflow["actual_cumulative"],
        prev_holdings=None if ph is None else float(ph),
    )


def _repair_pending_row(state: dict | None) -> None:
    if not state:
        return
    rid = state.get("last_run_id")
    if not rid:
        return
    ref = db.reference(f"{ROWS_PATH}/{rid}")
    doc = ref.get()
    if doc is not None and doc.get("committed") is False:
        ref.update({"committed": True})


def commit_final_row(cfg: Config, snapshot: dict, anchor: Anchor | None, row: dict,
                     *, slot_id: str | None = None, market_ordinal: int | None = None,
                     clock_mode: str | None = None,
                     runtime_identity: str | None = None,
                     pending_intent: dict | None = None) -> dict:
    """Commit one row and advance the DNA pointer.

    Advances the *decision* pointer only. The execution cashflow (P_acted, Aₙ)
    is carried through untouched, read from `current` inside the transaction so
    a fill finalized by the worker between this caller's anchor read and its
    commit is preserved rather than overwritten with the pre-fill value.

    Order execution is not part of this transaction: intents live in the
    outbox, so a broker failure can never roll back a committed slot. The row
    keeps exactly the original 17 columns; slot provenance is stored alongside
    run_id/version as metadata, never as a new column.

    Guards, all fail closed: replayed run_id (idempotent no-op), stale anchor,
    already-consumed slot, calendar drift, and an ordinal that does not move
    forward. Degraded 'epoch:*' slots carry no ordinal, so they skip the last one.
    """
    validate_row_columns(row)
    ck = chain_key(cfg)
    anchor_version = None if anchor is None else anchor.version
    run_id = make_run_id(ck, anchor_version, snapshot)
    expected_version = 1 if anchor is None else anchor.version + 1
    row_ref = db.reference(f"{ROWS_PATH}/{run_id}")
    state_ref = db.reference(f"{STATE_PATH}/{ck}")
    meta = row["_meta"]
    state_before = state_ref.get()
    verify_runtime_identity(state_before, runtime_identity)
    verify_dna_continuity(cfg, state_before)
    migrated_from = verify_cashflow_semantics(state_before)
    if slot_id is not None:
        verify_calendar_continuity(state_before)
    _repair_pending_row(state_before)

    existing = row_ref.get()
    if existing is not None and existing.get("committed"):
        return {"committed": False, "idempotent": True, "run_id": run_id,
                "version": existing.get("version")}

    doc = {k: v for k, v in row.items() if k != "_meta"}
    doc.update({
        "run_id": run_id,
        "chain_key": ck,
        "version": expected_version,
        "committed": False,
        "semantics": CASHFLOW_SEMANTICS,
        # Outside the 17 columns, like run_id and market_slot_id: says whether
        # the three cashflow columns are final or still waiting on a fill.
        "cashflow_status": CASHFLOW_PENDING
                           if meta.get("execution_pending", meta.get("acted"))
                           else CASHFLOW_NO_ACTION,
    })
    if slot_id is not None:
        doc["market_slot_id"] = slot_id
    if market_ordinal is not None:
        doc["market_ordinal"] = int(market_ordinal)
    if clock_mode is not None:
        doc["clock_mode"] = clock_mode
    row_ref.set(doc)

    def txn(current):
        current = current or None
        verify_runtime_identity(current, runtime_identity)
        # Re-checked inside the transaction, not only above: the write this
        # fences against is another revision's, and it can land between the read
        # at the top of this function and the moment this transaction runs.
        verify_cashflow_semantics(current)
        if current is None:
            if anchor_version is not None:
                raise StaleAnchorError("state ว่างแต่ anchor ไม่ใช่ genesis")
        else:
            if current.get("last_run_id") == run_id:
                raise _Idempotent()
            if anchor_version != current.get("version"):
                raise StaleAnchorError(
                    f"stale anchor: anchor.version={anchor_version} "
                    f"state.version={current.get('version')}")
            if slot_id is not None and current.get("slot_id") == slot_id:
                raise SlotAlreadyConsumed(f"slot {slot_id} commit ไปแล้ว")
            last_ordinal = current.get("market_ordinal")
            if market_ordinal is not None and last_ordinal is not None \
                    and int(market_ordinal) <= int(last_ordinal):
                raise OrdinalRegression(
                    f"market_ordinal ต้องเดินหน้า: chain อยู่ที่ {int(last_ordinal)} "
                    f"แต่ slot นี้ได้ {int(market_ordinal)} — DNA เดินถอยไม่ได้")

        # Read inside the transaction, so a fill the worker finalized after this
        # caller read its anchor is carried forward, not rolled back. Only an
        # absent cashflow is seeded — genesis, or the first commit of a chain
        # written before the split, where meta holds what read_anchor resolved.
        cashflow = dict((current or {}).get(EXECUTION_STATE_KEY) or {})
        if cashflow.get("last_action_price") is None:
            cashflow.update({
                "last_action_price": float(meta["acted_price_next"]),
                "actual_cumulative": float(meta["actual_next"]),
            })
            cashflow.setdefault("finalized_seq", 0)

        next_state = {
            "version": expected_version,
            "dna_step": int(meta["step"]),
            "p0": float(meta["p0_next"]),
            EXECUTION_STATE_KEY: cashflow,
            # Mirrors of the cashflow above, kept for readers written before the
            # split. Sourced from the cashflow and never from this decision, so
            # a READY_* row cannot move them.
            "prev_price": float(cashflow["last_action_price"]),
            "prev_actual": float(cashflow["actual_cumulative"]),
            "prev_holdings": float(snapshot.get("holdings", 0.0) or 0.0),
            "last_run_id": run_id,
            "updated_at": snapshot["captured_at"],
            "config_hash": config_hash(cfg),
            "dna_fingerprint": dna_fingerprint(cfg.dna_code),
            "symbol": cfg.symbol,
            "cashflow_semantics": CASHFLOW_SEMANTICS,
        }
        if runtime_identity is not None:
            next_state["runtime_identity_fingerprint"] = runtime_identity
        pending = dict((current or {}).get("pending_order_intents") or {})
        if pending_intent is not None:
            pending[run_id] = dict(pending_intent)
        if pending:
            next_state["pending_order_intents"] = pending
        if slot_id is not None:
            next_state["slot_id"] = slot_id
        if slot_id is not None and not slot_id.startswith("epoch:"):
            next_state["calendar_fingerprint"] = calendar_fingerprint()
        elif current and current.get("calendar_fingerprint"):
            # A degraded commit makes no claim about the calendar, so it must not
            # pin a new one — but dropping the chain's existing fingerprint would
            # disarm the drift guard for every commit after it, exactly when the
            # clock has just proven unreliable. Carry it forward, same reason as
            # market_ordinal below.
            next_state["calendar_fingerprint"] = current["calendar_fingerprint"]
        if market_ordinal is not None:
            next_state["market_ordinal"] = int(market_ordinal)
        elif current and current.get("market_ordinal") is not None:
            # A degraded commit resolves no ordinal, but dropping the chain's
            # last one would disarm the regression guard for every commit after
            # it — exactly when the clock has just proven unreliable. Carrying
            # the old value forward under-reports by the degraded slots, which
            # still catches a genuine walk backwards.
            next_state["market_ordinal"] = int(current["market_ordinal"])
        if clock_mode is not None:
            next_state["clock_mode"] = clock_mode
        return next_state

    try:
        state_ref.transaction(txn)
    except _Idempotent:
        row_ref.update({"committed": True})
        return {"committed": False, "idempotent": True,
                "run_id": run_id, "version": expected_version}
    except (StaleAnchorError, SlotAlreadyConsumed, OrdinalRegression,
            CashflowSemanticsDowngrade):
        row_ref.delete()
        raise

    row_ref.update({"committed": True})
    result = {"committed": True, "run_id": run_id, "version": expected_version,
              "market_slot_id": slot_id, "market_ordinal": market_ordinal}
    if migrated_from is not None:
        # read_anchor has already restarted Aₙ at zero for this commit. Saying so
        # is the point: a baseline reset and a corrupted ledger look identical in
        # the 17 columns, and only one of them is supposed to happen.
        result["cashflow_semantics_migrated_from"] = migrated_from
    return result


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_committed_row_for_chain(cfg: Config, run_id: str) -> dict:
    """The committed row a fill belongs to, refusing anything else."""
    doc = db.reference(f"{ROWS_PATH}/{run_id}").get()
    if not isinstance(doc, dict) or doc.get("committed") is not True:
        raise ExecutionFinalizeError(
            f"row {run_id} ยังไม่ committed — ห้าม finalize cashflow")
    ck = chain_key(cfg)
    if doc.get("chain_key") not in (None, ck):
        raise ExecutionFinalizeError(
            f"row {run_id} เป็นของ chain {doc.get('chain_key')} ไม่ใช่ {ck}")
    reference = doc.get(REFERENCE_COLUMN)
    if reference is None or not math.isfinite(float(reference)):
        raise ExecutionFinalizeError(f"row {run_id} ไม่มี Rₙ ที่ใช้ได้ — finalize ไม่ได้")
    return doc


def finalize_execution_fill(cfg: Config, run_id: str, fill: ExecutionFill, *,
                            runtime_identity: str | None = None) -> dict:
    """Book ΔAₙ/Aₙ/Eₙ for one broker-confirmed fill — once, atomically.

    This is the whole point of the split: the engine committed the row with the
    cashflow carried forward, and only here — with a filled price, a cumulative
    filled quantity and the holdings read back after execution — does the model
    ledger move.

    Idempotent on run_id, which is also the client_order_id, so a worker retry,
    a second poll of a partial fill, and a second worker instance racing the
    first all reduce to the first result. The state transaction is the fence:
    whoever loses sees the run_id already recorded and applies nothing. The row
    patch is repeated on that losing path too, because the failure it recovers
    from is a transaction that committed while the row write did not.

    Touches no field of the decision pointer: version, dna_step, p0, slot_id and
    market_ordinal all stay exactly where the engine left them, so finalizing
    can never present a stale anchor or consume a slot.
    """
    if not isinstance(fill, ExecutionFill):
        raise TypeError("fill ต้องเป็น ExecutionFill จาก broker จริง")
    run_id = str(run_id)
    ck = chain_key(cfg)
    row_doc = read_committed_row_for_chain(cfg, run_id)
    reference_R = float(row_doc[REFERENCE_COLUMN])
    # The row's own record of having been finalized. Second fence behind
    # finalized_runs, and the one that still holds after the run_id ages out of
    # that bounded history: a poll of a very old fill then reads as the no-op it
    # is, instead of booking the same ΔAₙ a second time.
    row_already_final = row_doc.get("cashflow_status") == CASHFLOW_FINALIZED
    state_ref = db.reference(f"{STATE_PATH}/{ck}")
    outcome: dict = {}

    def txn(current):
        if not isinstance(current, dict) or not current:
            raise ExecutionFinalizeError(
                "ไม่พบ chain state — finalize fill ไม่ได้")
        state = dict(current)
        verify_runtime_identity(state, runtime_identity)
        # A fill is the one write that is *supposed* to move Aₙ, which makes it
        # the worst one to let an out-of-date accounting perform: it would chain
        # a new ΔAₙ onto an Aₙ that means something else.
        verify_cashflow_semantics(state)
        cashflow = dict(state.get(EXECUTION_STATE_KEY) or {})
        if cashflow.get("last_action_price") is None:
            cashflow.update(execution_cashflow(state))
        finalized = dict(cashflow.get("finalized_runs") or {})
        already = finalized.get(run_id)
        if isinstance(already, dict):
            outcome.update({"applied": False, **already})
            return state                       # absorbing: never counted twice
        if row_already_final:
            outcome.update({
                "applied": False,
                "delta_actual": float(row_doc[DELTA_COLUMN]),
                "actual_cumulative": float(row_doc[ACTUAL_COLUMN]),
                "excess": float(row_doc[EXCESS_COLUMN]),
                "reference": reference_R,
                "filled_price": float(row_doc.get("execution_price")
                                      or fill.filled_price),
                "filled_quantity": float(row_doc.get("execution_quantity")
                                         or fill.filled_quantity),
                "holdings_after": float(row_doc.get("post_execution_holdings")
                                        if row_doc.get("post_execution_holdings")
                                        is not None else fill.holdings_after),
                "at": str(row_doc.get("cashflow_finalized_at") or ""),
            })
            return state

        result = finalize_recurrence(
            cfg, fill,
            last_action_price=float(cashflow["last_action_price"]),
            actual_cumulative=float(cashflow.get("actual_cumulative", 0.0) or 0.0),
            reference_R=reference_R)
        seq = int(cashflow.get("finalized_seq", 0) or 0) + 1
        record = {
            "delta_actual": result.dA,
            "actual_cumulative": result.A,
            "excess": result.E,
            "reference": reference_R,
            "previous_action_price": float(cashflow["last_action_price"]),
            "filled_price": float(fill.filled_price),
            "filled_quantity": float(fill.filled_quantity),
            "holdings_after": float(fill.holdings_after),
            "seq": seq,
            "at": _utc_stamp(),
        }
        finalized[run_id] = record
        if len(finalized) > FINALIZED_RUN_HISTORY:
            ordered = sorted(finalized.items(),
                             key=lambda kv: int((kv[1] or {}).get("seq", 0) or 0))
            finalized = dict(ordered[-FINALIZED_RUN_HISTORY:])
        cashflow.update({
            "last_action_price": result.acted_price_next,
            "actual_cumulative": result.A,
            "finalized_seq": seq,
            "last_finalized_run_id": run_id,
            "finalized_runs": finalized,
            "updated_at": record["at"],
        })
        state[EXECUTION_STATE_KEY] = cashflow
        state["prev_price"] = result.acted_price_next
        state["prev_actual"] = result.A
        # The post-execution reading, from the broker, replacing the decision's.
        state["prev_holdings"] = float(fill.holdings_after)
        outcome.update({"applied": True, **record})
        return state

    state_ref.transaction(txn)
    if not outcome:                            # pragma: no cover - defensive
        raise ExecutionFinalizeError("finalize transaction ไม่ได้คืนผลลัพธ์")

    # Always written, including on the idempotent path: the one crash this
    # repeats through is a committed transaction whose row patch never landed,
    # and rewriting the recorded numbers is exactly the repair.
    db.reference(f"{ROWS_PATH}/{run_id}").update({
        DELTA_COLUMN: outcome["delta_actual"],
        ACTUAL_COLUMN: outcome["actual_cumulative"],
        EXCESS_COLUMN: outcome["excess"],
        "cashflow_status": CASHFLOW_FINALIZED,
        "execution_price": outcome["filled_price"],
        "execution_quantity": outcome["filled_quantity"],
        "post_execution_holdings": outcome["holdings_after"],
        "cashflow_finalized_at": outcome["at"],
    })
    return {"run_id": run_id, "chain_key": ck, **outcome}


def execution_finalization(cfg: Config, run_id: str, *,
                           state=UNREAD_STATE) -> dict | None:
    """What was booked for *run_id*, or None if this chain never finalized it."""
    state = _resolve_state(cfg, state) or {}
    cashflow = state.get(EXECUTION_STATE_KEY) or {}
    record = (cashflow.get("finalized_runs") or {}).get(str(run_id))
    return dict(record) if isinstance(record, dict) else None


def pending_order_intents(cfg: Config, *,
                          runtime_identity: str | None = None,
                          state=UNREAD_STATE) -> dict[str, dict]:
    """Durable intent payloads committed atomically with the state pointer."""
    state = _resolve_state(cfg, state) or {}
    verify_runtime_identity(state, runtime_identity)
    raw = state.get("pending_order_intents") or {}
    return {
        str(run_id): dict(payload)
        for run_id, payload in raw.items()
        if isinstance(payload, dict)
    }


def chain_runtime_identity_is_verified(
        cfg: Config, runtime_identity: str | None, *, state=UNREAD_STATE) -> bool:
    """Return whether a state exists after applying the identity guard."""
    state = _resolve_state(cfg, state)
    if not state:
        return False
    verify_runtime_identity(state, runtime_identity)
    return True


def mark_order_intent_materialized(cfg: Config, run_id: str, *,
                                   runtime_identity: str | None = None) -> None:
    """Clear a recovery marker only after idempotent outbox creation succeeds."""
    ref = db.reference(f"{STATE_PATH}/{chain_key(cfg)}")

    def txn(current):
        if not isinstance(current, dict) or not current:
            raise RuntimeIdentityError(
                "state หายระหว่าง materialize outbox — หยุดเพื่อไม่สร้าง state ไม่ครบ")
        state = dict(current or {})
        verify_runtime_identity(state, runtime_identity)
        pending = dict(state.get("pending_order_intents") or {})
        pending.pop(run_id, None)
        if pending:
            state["pending_order_intents"] = pending
        else:
            state.pop("pending_order_intents", None)
        if runtime_identity is not None and not state.get("runtime_identity_fingerprint"):
            state["runtime_identity_fingerprint"] = runtime_identity
        return state

    ref.transaction(txn)


_AUDIT_SECRET_FIELDS = {
    "app_key", "app_secret", "access_token", "x-signature",
    "x-access-token", "x-app-key", "account_id", "webull_account_id",
    "authorization",
}


def _redact_audit_payload(payload: dict) -> dict:
    def clean(value):
        if isinstance(value, str):
            return redact_sensitive_text(value)
        if isinstance(value, dict):
            return {
                key: clean(item)
                for key, item in value.items()
                if str(key).lower() not in _AUDIT_SECRET_FIELDS
            }
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    return {
        key: clean(value)
        for key, value in payload.items()
        if str(key).lower() not in _AUDIT_SECRET_FIELDS
    }


def write_order_audit(event_id: str, payload: dict) -> None:
    redacted = _redact_audit_payload(payload)
    ref = db.reference(f"{AUDIT_PATH}/{event_id}")
    def txn(current):
        merged = dict(current or {})
        merged.update(redacted)
        return merged
    ref.transaction(txn)


def update_order_audit(event_id: str, fields: dict) -> None:
    safe = _redact_audit_payload(fields)
    db.reference(f"{AUDIT_PATH}/{event_id}").update(safe)


def pending_audits(terminal_statuses: set[str], limit: int = 20) -> dict:
    all_audits = db.reference(AUDIT_PATH).get() or {}
    rows: list[tuple[str, dict]] = []
    for event_id, payload in all_audits.items():
        if not isinstance(payload, dict):
            continue
        if normalize_status(payload.get("status")) in terminal_statuses:
            continue
        rows.append((event_id, payload))
    rows.sort(key=lambda item: str(item[1].get("placed_at") or item[1].get("created_at") or ""))
    return dict(rows[:limit])


def apply_realized_fill(ck: str, event_id: str, side: str,
                        cumulative_qty: float, price: float,
                        cumulative_fee: float = 0.0) -> dict:
    """Apply only the newly filled quantity for one broker order.

    Webull detail is treated as cumulative. applied_fills prevents double count
    across polling, retries, partial fills, and function restarts.
    """
    cumulative_qty = float(cumulative_qty)
    price = float(price)
    cumulative_fee = float(cumulative_fee or 0.0)
    if cumulative_qty < 0 or cumulative_fee < 0:
        raise ValueError("cumulative fill/fee ติดลบไม่ได้")
    ref = db.reference(f"{REALIZED_PATH}/{ck}")
    apply_token = uuid.uuid4().hex

    def txn(current):
        state = dict(current or {})
        applied = dict(state.get("applied_fills") or {})
        prev = dict(applied.get(event_id) or {})
        prev_qty = float(prev.get("quantity", 0.0) or 0.0)
        prev_fee = float(prev.get("fee", 0.0) or 0.0)
        prev_avg_price = float(prev.get("average_price", prev.get("price", 0.0)) or 0.0)
        delta_qty = cumulative_qty - prev_qty
        delta_fee = cumulative_fee - prev_fee
        if delta_qty < -1e-9:
            raise ValueError("cumulative filled quantity ถอยหลังไม่ได้")
        if delta_fee < -1e-9:
            raise ValueError("cumulative filled fee ถอยหลังไม่ได้")
        delta_qty = max(0.0, delta_qty)
        delta_fee = max(0.0, delta_fee)
        if (delta_qty <= 1e-9 and prev_qty > 1e-9
                and abs(price - prev_avg_price) > 1e-9):
            raise ValueError(
                "average fill price เปลี่ยนโดย quantity ไม่เพิ่ม — ต้องตรวจด้วยมือ")
        if delta_qty <= 1e-9 and delta_fee <= 1e-9:
            return state
        if delta_qty <= 1e-9:
            # Some brokers publish fees after the final quantity.  Recognizing
            # the fee now keeps cumulative P&L correct without inventing another
            # share fill or charging it twice on replay.
            realized_delta = -delta_fee
            cumulative = (
                float(state.get("cumulative_realized", 0.0) or 0.0)
                + realized_delta
            )
            applied[event_id] = {
                "quantity": cumulative_qty,
                "fee": cumulative_fee,
                "average_price": price,
                "side": str(side).upper(),
            }
            state.update({
                "applied_fills": applied,
                "cumulative_realized": cumulative,
                "last_realized_delta": realized_delta,
                "last_event_id": event_id,
                "last_apply_token": apply_token,
                "updated_at": datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"),
            })
            return state
        cumulative_notional = cumulative_qty * price
        previous_notional = prev_qty * prev_avg_price
        delta_price = (cumulative_notional - previous_notional) / delta_qty
        if not (delta_price > 0):
            raise ValueError("incremental fill price ต้อง > 0")
        legs, realized_delta = apply_fill(
            state.get("open_legs"), side, delta_qty, delta_price, delta_fee)
        cumulative = float(state.get("cumulative_realized", 0.0) or 0.0) + realized_delta
        applied[event_id] = {"quantity": cumulative_qty, "fee": cumulative_fee,
                             "average_price": price, "side": str(side).upper()}
        state.update({
            "open_legs": legs,
            "applied_fills": applied,
            "cumulative_realized": cumulative,
            "last_realized_delta": realized_delta,
            "last_event_id": event_id,
            "last_apply_token": apply_token,
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
        return state

    result = ref.transaction(txn) or {}
    applied_this_call = result.get("last_apply_token") == apply_token
    return {
        "realized_delta": (
            float(result.get("last_realized_delta", 0.0) or 0.0)
            if applied_this_call else 0.0
        ),
        "realized_cumulative": float(result.get("cumulative_realized", 0.0) or 0.0),
        "open_legs": result.get("open_legs") or {"buys": [], "sells": []},
    }
