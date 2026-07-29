"""Cloud Functions for time-aligned LEGO DNA and independent order execution.

lego_one_row: market clock -> snapshot -> model row -> durable outbox candidate.
    Decides. Never books a cashflow: READY_BUY/READY_SELL is an intent, so the
    row it commits carries ΔAₙ/Aₙ/Eₙ forward unchanged.
lego_order_worker: dispatch/reconcile outbox intents without blocking DNA time.
    Executes and finalizes. Once the broker confirms a fill and the position has
    actually moved, it books ΔAₙ/Aₙ/Eₙ from the filled price onto that row.
lego_archive_worker: move finished order records out of the live paths.
"""
from __future__ import annotations

import logging
import os
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone

import firebase_admin
import functions_framework
from firebase_admin import credentials, db

from lego_archive import archive_terminal_records
from lego_one_row import (READY_BUY, READY_SELL, DNAExhausted, ExecutionFill,
                          HoldingsAnomaly, check_holdings_continuity,
                          compute_row, dna_step_for, dna_steps_remaining,
                          position_vanished)
from lego_orders import (TERMINAL_STATUSES, UAT, evaluate_submit_gate,
                         normalize_status, order_confirmation_phrase,
                         summarize_order_result)
from lego_outbox import (begin_place_attempt, claim_intent,
                         expire_unsent_before, list_actionable,
                         list_audit_pending, put_intent, read_committed_row,
                         release_intent_claim, update_intent)
from lego_preflight import DEFAULT_MIN_DNA_REMAINING, auto_submit_preflight
from lego_state import (CalendarDriftError, DNADriftError,
                         ExecutionFinalizeError, OrdinalRegression,
                         RuntimeIdentityError, SlotAlreadyConsumed,
                         StaleAnchorError, apply_realized_fill, chain_key,
                         chain_runtime_identity_is_verified, commit_final_row,
                         finalize_execution_fill, mark_order_intent_materialized,
                         pending_order_intents, read_anchor, read_chain_state,
                         UNREAD_STATE, update_order_audit,
                         verify_runtime_identity, write_order_audit)
from market_clock import (MarketClockError, clock_mode, fallback_slot_id,
                          is_regular_session, resolve_dna_step, resolve_market_slot,
                          slot_seconds)
from webull_io import (IncompleteOpenOrdersError, build_clients,
                        build_order_payload, environment_label,
                        fetch_holdings, fetch_open_orders, fetch_order_detail,
                        fetch_snapshot, is_transient_exception, load_config,
                        market_category, place_market_order,
                        preview_market_order, redact_sensitive_text,
                        runtime_identity_fingerprint, token_health)

logger = logging.getLogger(__name__)

ORDER_POLL_ATTEMPTS = 3
ORDER_POLL_DELAY_S = 2.0
UTC = timezone.utc
WARNINGS_PATH = "webull_lego_warnings"
ERRORS_PATH = "webull_lego_errors"
# Non-terminal: the broker has answered with a fill, but the position has not
# caught up yet, so the model ledger cannot be finalized on this tick. Kept out
# of lego_outbox.TERMINAL on purpose — the intent has to come back — and bounded
# below so an account whose position feed never moves cannot hold the queue.
AWAITING_FILL_CONFIRMATION = "AWAITING_FILL_CONFIRMATION"
DEFAULT_FILL_CONFIRM_MAX_ATTEMPTS = 5
RECONCILE_STATUSES = {
    "PLACING_UNKNOWN", "PLACING", "SUBMITTED", "UNKNOWN",
    "PARTIAL_FILLED", "PARTIALLY_FILLED", AWAITING_FILL_CONFIRMATION,
}


class FillNotConfirmed(RuntimeError):
    """The broker reports a fill the account position has not shown yet.

    Not an error about the order — the order is fine. It says only that the two
    witnesses required before the model ledger may move (a cumulative filled
    quantity, and a position read back after execution) do not yet agree, so
    this tick must wait rather than book a cashflow on the broker's word alone.
    """


class RealizedMathError(RuntimeError):
    """The broker confirmed a fill, but our incremental realized math refused it.

    A different question from 'does this order exist?': the order does exist and
    is filled. Only the ledger update failed, and asking the broker again cannot
    change that, so it must not spend the reconcile budget.
    """


def _init_firebase():
    if not firebase_admin._apps:
        firebase_admin.initialize_app(
            credentials.ApplicationDefault(),
            {"databaseURL": os.environ["FIREBASE_DB_URL"]},
        )


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _error_text(exc: Exception, *, with_type: bool = True) -> str:
    text = redact_sensitive_text(exc)
    return f"{type(exc).__name__}: {text}" if with_type else text


def _poll_order_status(trade_client, client_order_id: str, place_res: dict) -> dict:
    detail = None
    for i in range(ORDER_POLL_ATTEMPTS):
        if i:
            time.sleep(ORDER_POLL_DELAY_S)
        detail = fetch_order_detail(trade_client, client_order_id)
        summary = summarize_order_result(place_res, detail)
        if normalize_status(summary.get("status")) in TERMINAL_STATUSES:
            return summary
    return summarize_order_result(place_res, detail)


def _record_warning(kind: str, message: str, extra: dict | None = None) -> None:
    """Make a silent skip alertable without growing without bound.

    One node per kind, updated in place with a counter, instead of a push per
    slot: a degraded clock repeats every slot forever, so pushing a record each
    time would recreate the unbounded path this system is already trimming.
    Never raises — a warning that breaks the run it is warning about is worse
    than no warning.

    Also emitted to the log, because RTDB was the only place it went. A blocked
    order left three traces and an operator could reach none of them: the
    response body, which Cloud Scheduler discards; a single counter node nobody
    watches; and nothing at all in Cloud Logging, where the run showed HTTP 200
    in 0.3s. Diagnosing this took a CSV export of the row table. One log line per
    warning ends that — `kind` is greppable and `extra` carries blocked_by.
    """
    logger.warning("lego warning kind=%s %s %s", kind, message,
                   redact_sensitive_text(extra) if extra else "")
    try:
        ref = db.reference(f"{WARNINGS_PATH}/{kind}")
        now = _iso(datetime.now(UTC))

        def txn(current):
            doc = dict(current or {})
            doc["count"] = int(doc.get("count", 0) or 0) + 1
            doc.setdefault("first_at", now)
            doc.update({"last_at": now, "message": message[:500], **(extra or {})})
            return doc

        ref.transaction(txn)
    except Exception:
        pass


def _apply_realized_if_available(intent: dict, summary: dict) -> dict:
    # summarize_order_result decides this from the filled quantity, not from the
    # status alone, so a fill that ends CANCELLED or EXPIRED is still accounted.
    if not summary.get("realized"):
        return summary
    qty = summary.get("filled_quantity")
    price = summary.get("filled_price")
    if qty is None or price is None:
        raise RealizedMathError(
            "fill confirmed but quantity/price unavailable — needs manual check")
    try:
        realized = apply_realized_fill(
            intent["chain_key"], intent["run_id"], intent["side"],
            cumulative_qty=float(qty), price=float(price),
            cumulative_fee=float(summary.get("filled_fee", 0.0) or 0.0),
        )
    except ValueError as exc:
        # Raised by apply_realized_fill/apply_fill when the incremental fill
        # numbers cannot be made sense of. Kept distinct from every other failure
        # here, all of which mean 'we could not reach or read the broker'.
        raise RealizedMathError(str(exc)) from exc
    out = dict(summary)
    out.update(realized)
    return out


def _persist(chain_key_: str, run_id: str, fields: dict) -> dict:
    """Keep the outbox authoritative and make an interrupted audit repairable."""
    update_intent(chain_key_, run_id, {**fields, "audit_pending": True})
    _mirror_order_audit(chain_key_, run_id, fields)
    return {"run_id": run_id, **fields}


def _mirror_order_audit(chain_key_: str, run_id: str, fields: dict) -> None:
    """Best-effort audit mirror; the outbox marker makes failure recoverable."""
    try:
        update_order_audit(run_id, fields)
    except Exception as exc:
        _record_warning(
            "order_audit_repair",
            "outbox อัปเดตแล้วแต่ audit ยังไม่สำเร็จ — worker จะซ่อมซ้ำ",
            {"run_id": run_id, "error_type": type(exc).__name__},
        )
    else:
        # If this clear fails the marker safely remains and the repair pass writes
        # the same audit payload again.
        try:
            update_intent(chain_key_, run_id, {"audit_pending": False})
        except Exception as exc:
            _record_warning(
                "order_audit_repair",
                "audit เขียนแล้วแต่ล้าง repair marker ไม่สำเร็จ — "
                "รอบถัดไปเขียนซ้ำได้อย่างปลอดภัย",
                {"run_id": run_id, "error_type": type(exc).__name__},
            )


_AUDIT_INTERNAL_FIELDS = {
    "audit_pending", "claim_owner", "claim_until", "claim_generation",
    "place_fence",
}


def _repair_pending_audits(chain_key_: str) -> int:
    repaired = 0
    for intent in list_audit_pending(chain_key_):
        run_id = str(intent["run_id"])
        fields = {
            key: value for key, value in intent.items()
            if key not in _AUDIT_INTERNAL_FIELDS
        }
        try:
            update_order_audit(run_id, fields)
            update_intent(chain_key_, run_id, {"audit_pending": False})
            repaired += 1
        except Exception as exc:
            _record_warning(
                "order_audit_repair",
                "audit repair ยังไม่สำเร็จ — เก็บ marker ไว้ลองรอบถัดไป",
                {"run_id": run_id, "error_type": type(exc).__name__},
            )
    return repaired


def _announce_identity_adoption(adopted: bool, runtime_identity: str) -> None:
    """Say once that a pre-guard chain was bound to this account/environment."""
    if not adopted:
        return
    _record_warning(
        "runtime_identity_adopted",
        "chain เดิมไม่มี runtime identity — ผูกกับ account/environment ปัจจุบัน "
        "อัตโนมัติ ตรวจว่า WEBULL_ACCOUNT_ID และ WEBULL_ENV ถูกต้อง",
        {"identity_prefix": runtime_identity[:8]},
    )


def _recover_pending_order_intents(cfg, runtime_identity: str,
                                   state=UNREAD_STATE) -> int:
    """Materialize state-transaction markers into the idempotent private outbox."""
    recovered = 0
    for run_id, payload in pending_order_intents(
            cfg, runtime_identity=runtime_identity, state=state).items():
        try:
            put_intent(chain_key(cfg), run_id, payload)
            mark_order_intent_materialized(
                cfg, run_id, runtime_identity=runtime_identity)
            recovered += 1
        except RuntimeIdentityError:
            raise
        except Exception as exc:
            _record_warning(
                "outbox_recovery",
                "committed row ยัง materialize เข้า outbox ไม่สำเร็จ — "
                "marker ยังอยู่และจะลองใหม่",
                {"run_id": run_id, "error_type": type(exc).__name__},
            )
    return recovered


def _persist_summary(intent: dict, summary: dict) -> None:
    _persist(intent["chain_key"], intent["run_id"],
             {**summary, "status": normalize_status(summary.get("status"))})


def _persist_error(chain_key_: str, run_id: str, status: str, exc: Exception,
                   extra: dict | None = None) -> dict:
    err = _error_text(exc)
    _persist(chain_key_, run_id,
             {"status": status, "last_error": err[:500], **(extra or {})})
    return {"run_id": run_id, "status": status, "error": err}


def _reconcile_max_attempts() -> int:
    return max(1, int(os.environ.get("LEGO_RECONCILE_MAX_ATTEMPTS", "20")))


def _min_dna_remaining() -> int:
    """How much DNA must be left before a new order may be opened.

    Never raises: an unreadable value is a deploy typo, and refusing to answer
    would abort the row instead of the order, which inverts the priority the
    whole pipeline is built on.
    """
    try:
        return int(os.environ.get("LEGO_AUTO_SUBMIT_MIN_DNA_REMAINING",
                                  str(DEFAULT_MIN_DNA_REMAINING)))
    except (TypeError, ValueError):
        return DEFAULT_MIN_DNA_REMAINING


def _persist_reconcile_failure(intent: dict, exc: Exception) -> dict:
    """Bound the reconcile loop so one unresolvable order cannot jam the outbox.

    PLACING_UNKNOWN is deliberately non-terminal: an order we failed to confirm
    may still exist at the broker, so the worker must ask again. But nothing
    counted the asking. An order the broker never accepted answers 'UNKNOWN'
    forever, and because list_actionable serves oldest-first up to
    LEGO_ORDER_WORKER_LIMIT, three such intents starve every later decision —
    the DNA keeps committing rows while no order is ever sent again.

    Past the bound the outbox stops asking and says so. The open question is not
    dropped: the order audit keeps run_id, last_error and needs_manual_check,
    and the dashboard already renders that table.
    """
    ck, run_id = intent["chain_key"], intent["run_id"]
    attempts = int(intent.get("reconcile_attempts", 0) or 0) + 1
    # last_error is overwritten every tick, and by the time a human reads it the
    # useful message ("insufficient buying power") has been buried under the
    # generic one. Keep the first failure, which is the one that explains why.
    extra = {"reconcile_attempts": attempts}
    if not intent.get("first_error"):
        extra["first_error"] = _error_text(exc)[:500]
    if attempts < _reconcile_max_attempts():
        return _persist_error(ck, run_id, "PLACING_UNKNOWN", exc, extra)
    return _persist_error(ck, run_id, "RECONCILE_ABANDONED", exc, {
        **extra,
        "needs_manual_check": True,
        "terminal_reason": (f"broker ไม่ยืนยันสถานะครบ {attempts} ครั้ง — "
                            "ต้องเช็คที่ broker เองว่า order นี้มีจริงหรือไม่"),
    })


def _persist_realized_math_error(intent: dict, summary: dict, exc: Exception) -> dict:
    """End an intent whose order filled but whose realized math did not.

    This used to share the reconcile path, which reads 'we do not know whether
    this order exists'. Here we do know: the broker answered, and the answer says
    filled. Retrying cannot fix arithmetic, so it ends immediately instead of
    spending attempts, and the broker's own numbers are kept on the audit so the
    person reconciling can see the fill was real and only the ledger is behind.
    """
    fields = {
        "status": "REALIZED_MATH_ERROR",
        "needs_manual_check": True,
        "realized": False,
        "broker_status": normalize_status(summary.get("status")),
        "last_error": _error_text(exc)[:500],
        "terminal_reason": ("broker ยืนยัน fill แล้ว แต่คำนวณ realized ไม่ได้ — "
                            "ห้ามส่ง order ซ้ำ ต้องกระทบยอด realized ledger เอง"),
    }
    for key in ("filled_quantity", "filled_price", "filled_fee", "reject_reason"):
        if key in summary:
            fields[key] = summary[key]
    _persist(intent["chain_key"], intent["run_id"], fields)
    _record_warning("realized_math_error",
                    "order fill ยืนยันแล้วแต่คำนวณ realized ไม่ได้ — ต้องกระทบยอดเอง",
                    {"run_id": intent["run_id"], "chain_key": intent["chain_key"]})
    return {"run_id": intent["run_id"], **fields}


def _holdings_drift_tolerance() -> float:
    """Below this a holdings difference is noise, not a position that moved."""
    try:
        return abs(float(os.environ.get("LEGO_HOLDINGS_DRIFT_TOLERANCE",
                                        "0.000001")))
    except (TypeError, ValueError):
        return 0.000001


def _fill_confirm_max_attempts() -> int:
    try:
        return max(1, int(os.environ.get("LEGO_FILL_CONFIRM_MAX_ATTEMPTS",
                                         str(DEFAULT_FILL_CONFIRM_MAX_ATTEMPTS))))
    except (TypeError, ValueError):
        return DEFAULT_FILL_CONFIRM_MAX_ATTEMPTS


def _positive_float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 and number == number and number != float("inf") else None


def _holdings_moved(side: str, before: float, after: float, tolerance: float) -> bool:
    """Did the position move the way this side of the trade requires?

    Direction, not just difference: a BUY whose position went *down* between the
    decision and the fill is somebody else's trade landing in the same account,
    and booking our ΔAₙ against it would be inventing a cashflow.
    """
    if side == "BUY":
        return after > before + tolerance
    if side == "SELL":
        return after < before - tolerance
    return False


def _finalize_model_ledger(trade_client, cfg, intent: dict, summary: dict) -> dict:
    """Book ΔAₙ/Aₙ/Eₙ for this row, once the broker proves the shares moved.

    Both inputs come from the broker and neither from what we asked for: the
    cumulative filled quantity and price of this client_order_id, and the
    position read back after execution. The ordered quantity is never used to
    guess the resulting holdings, and the decision price is never used in place
    of the filled price.
    """
    quantity = _positive_float(summary.get("filled_quantity"))
    price = _positive_float(summary.get("filled_price"))
    if quantity is None or price is None:
        raise RealizedMathError(
            "fill confirmed but quantity/price unavailable — needs manual check")
    try:
        holdings_after = fetch_holdings(trade_client, cfg)
    except Exception as exc:
        # Could not read the position — a network blip, or a positions response
        # this adapter refuses to interpret. Neither says anything about the
        # fill, so it defers on the bounded budget instead of ending the intent
        # the way an arithmetic failure does.
        raise FillNotConfirmed(
            f"อ่าน holdings หลัง fill ไม่สำเร็จ: {_error_text(exc)}") from exc
    before = float(intent.get("decision_holdings", 0.0) or 0.0)
    # Same rule as the pre-decision guard, applied to the same reading: a
    # rebalance SELL targets value fix_c and always leaves shares behind, so a
    # position that reads exactly zero is a positions response that lost the
    # symbol, not a confirmation. Booking it would move P_acted onto that fill
    # *and* write prev_holdings = 0, which disarms the guard for every slot
    # after it — the compounding this system already refuses on the way in.
    if (os.environ.get("LEGO_ALLOW_ZERO_HOLDINGS", "false").lower() != "true"
            and position_vanished(before, holdings_after)):
        raise FillNotConfirmed(
            f"chain เคยถือ {before} หุ้น แต่ post-execution holdings อ่านได้ 0 — "
            "อาจเป็น positions response ที่ไม่ครบ จึงยังไม่ finalize cashflow")
    if not _holdings_moved(str(intent.get("side", "")).upper(), before,
                           holdings_after, _holdings_drift_tolerance()):
        raise FillNotConfirmed(
            f"broker แจ้ง filled {quantity} แต่ holdings หลังส่งยังเป็น {holdings_after} "
            f"(ตอนตัดสินใจ {before}) — ยังไม่ยืนยันว่าจำนวนถือครองเปลี่ยน")
    return finalize_execution_fill(
        cfg, str(intent["run_id"]),
        ExecutionFill(filled_price=price, filled_quantity=quantity,
                      holdings_after=holdings_after),
        runtime_identity=intent.get("runtime_identity_fingerprint"))


def _defer_fill_confirmation(intent: dict, summary: dict, exc: Exception) -> dict:
    """Come back for a fill whose position has not landed yet — but not forever.

    The intent stays actionable under its own non-terminal status so the next
    tick asks the broker again. Past the bound it stops asking and carries the
    broker's own status out of the queue with needs_manual_check, for the same
    reason RECONCILE_ABANDONED exists: an intent that can never resolve must not
    keep a dispatch slot from every decision behind it.
    """
    attempts = int(intent.get("fill_confirm_attempts", 0) or 0) + 1
    broker_status = normalize_status(summary.get("status"))
    fields = {
        **summary,
        "broker_status": broker_status,
        "cashflow_finalized": False,
        "fill_confirm_attempts": attempts,
        "last_error": _error_text(exc)[:500],
    }
    if attempts < _fill_confirm_max_attempts():
        fields["status"] = AWAITING_FILL_CONFIRMATION
        _persist(intent["chain_key"], intent["run_id"], fields)
        return {"run_id": intent["run_id"], **fields}
    fields.update({
        "status": broker_status,
        "cashflow_abandoned": True,
        "needs_manual_check": True,
        "terminal_reason": (f"broker แจ้ง fill แต่ holdings ไม่ยืนยันครบ {attempts} ครั้ง "
                            "— ΔAₙ/Aₙ/Eₙ ของแถวนี้ยังไม่ finalize ต้องกระทบยอดเอง"),
    })
    _persist(intent["chain_key"], intent["run_id"], fields)
    _record_warning(
        "cashflow_unconfirmed",
        "fill ยืนยันจาก broker แล้วแต่ holdings ไม่ขยับ — model ledger ยังไม่ finalize",
        {"run_id": intent["run_id"], "chain_key": intent["chain_key"]})
    return {"run_id": intent["run_id"], **fields}


def _persist_cashflow_error(intent: dict, summary: dict, exc: Exception) -> dict:
    """End an intent whose fill is real but whose model ledger refused it.

    Same shape as _persist_realized_math_error and for the same reason: the
    broker has answered, so re-sending would duplicate a filled order. Retrying
    cannot fix arithmetic or a missing chain state, so it ends here and the
    ledger gap stays visible instead of being retried forever.
    """
    fields = {
        **summary,
        "status": "CASHFLOW_FINALIZE_ERROR",
        "broker_status": normalize_status(summary.get("status")),
        "cashflow_finalized": False,
        "cashflow_abandoned": True,
        "needs_manual_check": True,
        "last_error": _error_text(exc)[:500],
        "terminal_reason": ("fill จริงแต่ finalize ΔAₙ/Aₙ/Eₙ ไม่ได้ — "
                            "ห้ามส่ง order ซ้ำ ต้องกระทบยอด model ledger เอง"),
    }
    _persist(intent["chain_key"], intent["run_id"], fields)
    _record_warning("cashflow_finalize_error",
                    "fill ยืนยันแล้วแต่ finalize model ledger ไม่ได้ — ต้องกระทบยอดเอง",
                    {"run_id": intent["run_id"], "chain_key": intent["chain_key"]})
    return {"run_id": intent["run_id"], **fields}


def _finish_with_realized(trade_client, cfg, intent: dict, summary: dict) -> dict:
    """The broker has answered; the only failure left belongs to us.

    Two ledgers move here and they are independent: the realized ledger from
    matched broker legs, and the 17-column model ledger this function finalizes
    from the same confirmed fill. A decision never reaches either one.
    """
    try:
        summary = _apply_realized_if_available(intent, summary)
    except RealizedMathError as exc:
        return _persist_realized_math_error(intent, summary, exc)
    # Acted is a cumulative filled quantity above zero and nothing else — not the
    # status, which is only a label on top of it. So a DAY order that fills 30 of
    # 70 and is cancelled at the close still finalizes those 30, while a REJECTED
    # or CANCELLED order that moved no shares leaves ΔAₙ = 0 and Aₙ exactly where
    # the last confirmed fill left it. (_apply_realized_if_available has already
    # refused a claimed fill with no readable quantity or price.)
    filled = _positive_float(summary.get("filled_quantity"))
    if filled is not None and not intent.get("cashflow_abandoned"):
        try:
            finalized = _finalize_model_ledger(trade_client, cfg, intent, summary)
        except FillNotConfirmed as exc:
            return _defer_fill_confirmation(intent, summary, exc)
        except RealizedMathError as exc:
            return _persist_realized_math_error(intent, summary, exc)
        except (ExecutionFinalizeError, ValueError, TypeError) as exc:
            return _persist_cashflow_error(intent, summary, exc)
        summary = {
            **summary,
            "cashflow_finalized": True,
            "cashflow_applied_now": finalized["applied"],
            "delta_actual": finalized["delta_actual"],
            "actual_cumulative": finalized["actual_cumulative"],
            "excess": finalized["excess"],
            "post_execution_holdings": finalized["holdings_after"],
        }
    _persist_summary(intent, summary)
    return {"run_id": intent["run_id"], **summary}


def _pending_row_shape(intent: dict) -> dict:
    """What the outbox intent says it wants to send."""
    return {
        "สถานะ": intent["row_status"],
        "สินทรัพย์": intent["symbol"],
        "_meta": {
            "side": intent["side"],
            "quantity": float(intent["quantity"]),
            "step": int(intent["step"]),
        },
    }


def _committed_row_shape(doc: dict) -> dict:
    """What the engine actually committed — the gate's source of truth."""
    return {
        "สถานะ": doc.get("สถานะ"),
        "สินทรัพย์": doc.get("สินทรัพย์"),
        "_meta": {
            "side": doc.get("ฝั่ง"),
            "quantity": float(doc.get("จำนวนสั่ง (หุ้น)") or 0.0),
            "step": int(doc.get("DNA step") or 0),
        },
    }


def _stop(chain_key_: str, run_id: str, status: str, extra: dict | None = None,
          **reported) -> dict:
    """Close an intent in the outbox without touching the audit trail."""
    update_intent(chain_key_, run_id, {"status": status, **(extra or {})})
    return {"run_id": run_id, "status": status, **reported}


def _dispatch_or_reconcile_one(trade_client, data_client, cfg, intent: dict) -> dict:
    run_id = intent["run_id"]
    ck = intent["chain_key"]
    status = normalize_status(intent.get("status"))

    committed_row = read_committed_row(run_id)
    if committed_row is None:
        return _stop(ck, run_id, "NOT_PLACED",
                     {"terminal_reason": "source row was not committed"})

    if status in RECONCILE_STATUSES:
        try:
            summary = summarize_order_result({}, fetch_order_detail(trade_client, run_id))
            if normalize_status(summary.get("status")) == "UNKNOWN":
                raise RuntimeError("broker order detail still UNKNOWN")
        except Exception as exc:
            # Everything inside this try is 'can we reach and read the broker?'.
            # The realized and model ledgers are applied outside it so their
            # failures are not reported as an unresolved order.
            return _persist_reconcile_failure(intent, exc)
        return _finish_with_realized(trade_client, cfg, intent, summary)

    if status != "PENDING_DISPATCH":
        return {"run_id": run_id, "status": status}

    expiry = datetime.fromisoformat(str(intent["expires_at"]).replace("Z", "+00:00"))
    if datetime.now(UTC) >= expiry:
        return _stop(ck, run_id, "EXPIRED_UNSENT")

    try:
        open_orders = fetch_open_orders(trade_client, cfg.symbol)
    except IncompleteOpenOrdersError as exc:
        return _persist_error(
            ck, run_id, "PENDING_DISPATCH", exc,
            {"pagination_complete": False},
        )
    if open_orders:
        return _stop(ck, run_id, "SUPPRESSED_ACTIVE_ORDER",
                     {"terminal_reason": f"{len(open_orders)} active broker order(s)"})

    fresh = fetch_snapshot(trade_client, data_client, cfg)
    decision_holdings = float(intent.get("decision_holdings", 0.0) or 0.0)
    tolerance = float(os.environ.get("LEGO_HOLDINGS_DRIFT_TOLERANCE", "0.000001"))
    drift = abs(float(fresh["holdings"]) - decision_holdings)
    if drift > tolerance:
        return _stop(ck, run_id, "SUPPRESSED_STATE_CHANGED",
                     {"holdings_drift": drift, "dispatch_holdings": fresh["holdings"]},
                     holdings_drift=drift)

    env = environment_label()
    if env != UAT:
        return _stop(ck, run_id, "NOT_PLACED", {"terminal_reason": f"environment={env}"})

    # Audit-only on purpose: the outbox already holds every one of these fields
    # from put_intent, and begin_place_attempt below is the authoritative outbox
    # write for this step. Mirroring them here too would spend three extra RTDB
    # transactions on the hot money path to restate what is already there.
    write_order_audit(run_id, {
        "run_id": run_id, "chain_key": ck, "side": intent["side"],
        "quantity": float(intent["quantity"]), "symbol": cfg.symbol,
        "environment": env, "status": "PENDING_DISPATCH", "realized": False,
        "placed_at": intent["created_at"],
    })
    try:
        # Building the payload is part of the gate: a quantity that cannot be
        # expressed at this precision must end the intent, not abort the whole
        # worker run and leave the other intents of this tick unprocessed.
        order = build_order_payload(cfg, intent["side"], float(intent["quantity"]), run_id)
        preview_ok = preview_market_order(trade_client, order)
        # Two independent witnesses: the gate judges the committed row, the
        # phrase comes from the intent about to be sent. They are only equal
        # while the outbox still agrees with what the engine decided.
        evaluate_submit_gate(env, _committed_row_shape(committed_row), preview_ok,
                             order_confirmation_phrase(_pending_row_shape(intent)),
                             committed=True)
    except Exception as exc:
        return _persist_error(ck, run_id, "NOT_PLACED", exc)

    place_fields = {"status": "PLACING_UNKNOWN", "place_attempted": True}
    started = begin_place_attempt(
        ck, run_id, str(intent.get("claim_owner") or ""),
        int(intent.get("claim_generation", 0) or 0))
    if started is None:
        # The lease expired and another generation won before this worker reached
        # the irreversible call.  Do not place; that winner owns reconciliation.
        return {"run_id": run_id, "status": normalize_status(intent.get("status"))}
    _mirror_order_audit(ck, run_id, place_fields)
    try:
        place_res = place_market_order(trade_client, order)
        summary = _poll_order_status(trade_client, run_id, place_res)
    except Exception as exc:
        # Same open question as a failed reconcile — "does this order exist?" —
        # so it draws on the same bounded budget.
        return _persist_reconcile_failure(intent, exc)
    return _finish_with_realized(trade_client, cfg, intent, summary)


def _outbox_intent(cfg, row: dict, snapshot: dict, slot, decision_time: datetime) -> dict:
    margin = int(os.environ.get("LEGO_ORDER_EXPIRY_MARGIN_SECONDS", "15"))
    expires_at = max(slot.slot_start_utc, slot.slot_end_utc - timedelta(seconds=margin))
    return {
        "status": "PENDING_DISPATCH",
        "row_status": row["สถานะ"], "side": row["_meta"]["side"],
        "quantity": row["_meta"]["quantity"], "symbol": cfg.symbol,
        "step": row["DNA step"], "signal": row["DNA signal"],
        "decision_price": snapshot["price"],
        "decision_holdings": snapshot["holdings"],
        "decision_time": _iso(decision_time),
        "created_at": snapshot["captured_at"],
        "slot_id": slot.slot_id,
        "slot_start_utc": _iso(slot.slot_start_utc),
        "slot_end_utc": _iso(slot.slot_end_utc),
        "expires_at": _iso(expires_at),
    }


def _run_order_worker(cfg, limit: int = 3,
                      runtime_identity: str | None = None) -> dict:
    runtime_identity = runtime_identity or runtime_identity_fingerprint()
    ck = chain_key(cfg)
    # Both guards below read the same document, so read it once and hand it down.
    state = read_chain_state(cfg)
    state_verified = chain_runtime_identity_is_verified(
        cfg, runtime_identity, state=state)
    _announce_identity_adoption(
        verify_runtime_identity(state, runtime_identity), runtime_identity)
    _recover_pending_order_intents(cfg, runtime_identity, state=state)
    _repair_pending_audits(ck)
    expired = expire_unsent_before(ck, datetime.now(UTC))
    results = []
    worker_id = uuid.uuid4().hex
    candidates = list_actionable(ck, limit=limit)
    if candidates and not state_verified:
        raise RuntimeIdentityError(
            "พบ outbox แต่ไม่พบ chain state สำหรับยืนยัน account/environment")
    if not candidates:
        # Nothing to dispatch, and saying so is the point: an idle tick and a
        # broken tick both answered HTTP 200 in 0.3s with an empty body, which is
        # what hid an outbox that never received a single intent. Building the
        # clients here would also have cost four auth calls — two config plus two
        # create_token, against a 10-per-30s cap shared with lego_one_row — to
        # authenticate for work that does not exist.
        logger.info("lego_order_worker actionable=0 expired_unsent=%d "
                    "chain_key=%s — no order intent to dispatch", expired, ck)
        return {"processed": 0, "actionable": 0, "expired_unsent": expired,
                "results": []}
    trade_client, data_client = build_clients()
    for intent in candidates:
        claimed = claim_intent(ck, intent["run_id"], worker_id)
        if claimed is None:
            continue
        try:
            intent_identity = claimed.get("runtime_identity_fingerprint")
            if (intent_identity is not None
                    and str(intent_identity) != runtime_identity):
                raise RuntimeIdentityError(
                    "outbox runtime identity ไม่ตรงกับ worker "
                    "(account/environment คนละชุด)")
            results.append(
                _dispatch_or_reconcile_one(trade_client, data_client, cfg, claimed))
        finally:
            release_intent_claim(ck, intent["run_id"], worker_id)
    logger.info("lego_order_worker actionable=%d processed=%d expired_unsent=%d "
                "statuses=%s", len(candidates), len(results), expired,
                [r.get("status") for r in results])
    return {"processed": len(results), "actionable": len(candidates),
            "expired_unsent": expired, "results": results}


@functions_framework.http
def lego_one_row(request):
    """Commit the current model slot first. Order failures never block DNA time."""
    _init_firebase()
    decision_time = datetime.now(UTC)

    try:
        # Mandatory: the slot grid must match the timeframe the DNA was trained
        # on, and the clock mode decides which step the row gets. A missing or
        # unsupported value is a deploy error, not a runtime one, so all three are
        # resolved up front instead of surfacing later as an engine failure —
        # the market category included, since a typo there reaches the broker as
        # a query parameter and comes back as an unhelpful snapshot error.
        cfg = load_config()
        slot_seconds()
        mode = clock_mode()
        market_category()
        env = environment_label()
        runtime_identity = runtime_identity_fingerprint()
    except (KeyError, MarketClockError, ValueError) as exc:
        return {"status": "CONFIG_ERROR", "committed": False,
                "pipeline_status": "CONFIG_ERROR",
                "error": _error_text(exc, with_type=False)}, 500

    try:
        # Recovery and the anchor read both need this chain's state, so it is
        # fetched once here and passed to both instead of once each.
        state = read_chain_state(cfg)
        _announce_identity_adoption(
            verify_runtime_identity(state, runtime_identity), runtime_identity)
        _recover_pending_order_intents(cfg, runtime_identity, state=state)
    except RuntimeIdentityError as exc:
        return {"status": "CONFIG_ERROR", "committed": False,
                "pipeline_status": "CONFIG_ERROR",
                "error": _error_text(exc, with_type=False)}, 500

    # One calendar for every path: the same session rules the ordinal uses, so a
    # holiday or early close also blocks a degraded (clock-less) commit.
    if not is_regular_session(decision_time):
        return {"status": "PASS_MARKET_CLOSED", "committed": False,
                "pipeline_status": "MARKET_CLOSED"}, 200

    try:
        anchor = read_anchor(cfg, runtime_identity=runtime_identity, state=state)
        legacy_step = dna_step_for(anchor)
        slot = None
        clock_error = None
        try:
            slot = resolve_market_slot(decision_time)
            if slot is None:
                return {"status": "PASS_MARKET_CLOSED", "committed": False,
                        "pipeline_status": "MARKET_CLOSED"}, 200
            effective_step, alignment_error = resolve_dna_step(legacy_step, slot)
        except MarketClockError as exc:
            if mode == "market":
                raise
            effective_step, alignment_error = legacy_step, None
            clock_error = str(exc)

        trade_client, data_client = build_clients()
        # The token dies of old age silently: nothing in the SDK renews it, and
        # recovery needs a human to approve 2FA within 300 seconds. build_clients
        # refreshes it while it is still valid; this reports what is left so the
        # cases it cannot fix by itself — an ephemeral token dir, a token already
        # gone — are visible days before they stop the chain.
        health = token_health()
        token_warning = None if health["ok"] else "; ".join(health["reasons"])
        if token_warning:
            _record_warning("webull_token", token_warning, {
                k: v for k, v in health.items()
                if k in ("token_dir", "ephemeral_token_dir", "status",
                         "expires_at", "days_left") and v is not None})
        snapshot = fetch_snapshot(trade_client, data_client, cfg)
        # Reaching this line is live proof that the token can sign a trade
        # request: fetch_snapshot goes through account_v2.get_account_position on
        # the very same authenticated ApiClient that place_order will use, and any
        # auth failure would have left this block by exception instead. Recorded
        # here, next to the call that earns it, so a later reordering cannot leave
        # the claim standing without the evidence behind it.
        token_proved_live = True
        # Before the row exists: a snapshot that lost the position would make
        # gap = fix_c, the largest order possible, and committing it would also
        # write prev_holdings = 0 and disarm this check for every commit after.
        if os.environ.get("LEGO_ALLOW_ZERO_HOLDINGS", "false").lower() != "true":
            check_holdings_continuity(anchor, float(snapshot["holdings"]))
        if slot:
            slot_id = slot.slot_id
        else:
            # Degraded clock still gets a one-commit-per-slot key so a scheduler
            # retry cannot consume the same slot twice.
            slot_id = fallback_slot_id(snapshot["captured_at"])
            mode = f"{mode}:degraded"
        row = compute_row(cfg, snapshot, anchor, dna_step=effective_step)

        auto = os.environ.get("AUTO_SUBMIT", "false").lower() == "true"
        # Warn while extending the DNA is still possible; also one of the
        # preflight inputs, so it is resolved once and read twice.
        remaining = dna_steps_remaining(cfg.dna_code, row["DNA step"])
        outbox_error = None
        outbox_skipped = None
        outbox_blocked = None
        preflight = None
        pending_intent = None

        # Evaluate a candidate before the state transaction so the exact payload
        # can be stored in that same transaction.  row_durable=True here means
        # "activate only if commit succeeds"; no broker/outbox write happens yet.
        if auto and row["สถานะ"] in (READY_BUY, READY_SELL):
            preflight = auto_submit_preflight(
                auto_submit=auto, environment=env, row=row, row_durable=True,
                slot=slot, token=health, dna_remaining=remaining,
                min_dna_remaining=_min_dna_remaining(),
                token_proved_live=token_proved_live)
            if preflight["ok"]:
                pending_intent = _outbox_intent(
                    cfg, row, snapshot, slot, decision_time)
                pending_intent["runtime_identity_fingerprint"] = runtime_identity

        # A rejected commit leaves no outbox candidate.  A successful state
        # transaction carries a deterministic recovery marker, closing the crash
        # window between advancing DNA and materializing the private outbox.
        result = commit_final_row(
            cfg, snapshot, anchor, row, slot_id=slot_id, clock_mode=mode,
            market_ordinal=None if slot is None else slot.market_ordinal,
            runtime_identity=runtime_identity,
            pending_intent=pending_intent)

        if preflight is None:
            pass                                    # nothing to submit this slot
        elif preflight["ok"]:
            try:
                put_intent(chain_key(cfg), result["run_id"], pending_intent)
                mark_order_intent_materialized(
                    cfg, result["run_id"], runtime_identity=runtime_identity)
            except Exception as exc:
                outbox_error = _error_text(exc)
                _record_warning(
                    "outbox_recovery",
                    "committed row ยัง materialize เข้า outbox ไม่สำเร็จ — "
                    "marker ยังอยู่และจะลองใหม่",
                    {"run_id": result["run_id"],
                     "error_type": type(exc).__name__},
                )
        else:
            # Blocked, and said out loud on both channels. Deciding this silently
            # was the worst of both worlds: the row still read READY_BUY with
            # committed=true and no error field, so a dashboard showed a working
            # bot that had never sent a single order.
            message = preflight["message"]
            if preflight["field"] == "outbox_skipped":
                outbox_skipped = message
            else:
                outbox_blocked = message
            extra = {"run_id": result["run_id"], "row_status": row["สถานะ"],
                     "clock_mode": mode, "blocked_by": preflight["blocked_by"]}
            if preflight["hint"]:
                extra["hint"] = preflight["hint"]
            _record_warning(preflight["warning_kind"], message, extra)

        out = {
            "status": row["สถานะ"], "committed": result["committed"],
            "idempotent": result.get("idempotent", False),
            "run_id": result["run_id"], "version": result.get("version"),
            "step": row["DNA step"], "signal": row["DNA signal"],
            "model_acted": row["_meta"]["acted"],
            "pipeline_status": "ROW_COMMITTED",
            "clock_mode": mode,
            "legacy_step": legacy_step,
            "market_step": None if slot is None else slot.market_ordinal,
            "alignment_error": alignment_error,
            "market_slot_id": None if slot is None else slot.slot_id,
        }
        if clock_error:
            out["clock_warning"] = clock_error
        if token_warning:
            out["token_warning"] = token_warning
        if outbox_error:
            out["outbox_error"] = outbox_error
        if outbox_skipped:
            out["outbox_skipped"] = outbox_skipped
        if outbox_blocked:
            out["outbox_blocked"] = outbox_blocked
            out["outbox_blocked_checks"] = preflight["blocked_by"]
        # Silent until the last few slots so a healthy chain keeps the response
        # it has always had.
        if remaining <= int(os.environ.get("LEGO_DNA_LOW_WATERMARK", "10")):
            out["dna_steps_remaining"] = remaining

        # One line per slot in Cloud Logging. The response body says all of this
        # already, but nothing reads it: the caller is Cloud Scheduler, which
        # keeps the status code and throws the body away. Without this a chain
        # deciding READY_SELL every slot and sending nothing looks exactly like a
        # chain trading normally — 200, no error, no output.
        logger.info(
            "lego_one_row slot=%s step=%s status=%s committed=%s "
            "order_intent=%s blocked_by=%s",
            out.get("market_slot_id"), row["DNA step"], row["สถานะ"],
            result["committed"],
            "created" if (preflight and preflight["ok"] and not outbox_error)
            else "none",
            (preflight or {}).get("blocked_by") or [])

        # Off by default: dispatching inline adds broker latency to the DNA
        # invocation, which raises the odds of a scheduler timeout+retry.
        if os.environ.get("LEGO_INLINE_ORDER_WORKER", "false").lower() == "true":
            try:
                out["order_worker"] = _run_order_worker(cfg, limit=1)
            except Exception as exc:
                out["order_worker"] = {
                    "processed": 0, "error": _error_text(exc)}
        return out, 200

    except RuntimeIdentityError as exc:
        return {"status": "CONFIG_ERROR", "committed": False,
                "pipeline_status": "CONFIG_ERROR",
                "error": _error_text(exc, with_type=False)}, 500
    except SlotAlreadyConsumed as exc:
        return {"status": "PASS_SLOT_CONSUMED", "committed": False,
                "pipeline_status": "SLOT_CONSUMED", "note": str(exc)}, 200
    except StaleAnchorError as exc:
        return {"status": "STALE_ANCHOR", "committed": False,
                "pipeline_status": "STALE_ANCHOR", "note": str(exc)}, 409
    except CalendarDriftError as exc:
        return {"status": "CALENDAR_DRIFT", "committed": False,
                "pipeline_status": "CALENDAR_DRIFT", "note": str(exc)}, 409
    except OrdinalRegression as exc:
        return {"status": "ORDINAL_REGRESSION", "committed": False,
                "pipeline_status": "ORDINAL_REGRESSION", "note": str(exc)}, 409
    except DNADriftError as exc:
        return {"status": "DNA_DRIFT", "committed": False,
                "pipeline_status": "DNA_DRIFT", "note": str(exc)}, 409
    except HoldingsAnomaly as exc:
        return {"status": "HOLDINGS_ANOMALY", "committed": False,
                "pipeline_status": "HOLDINGS_ANOMALY", "note": str(exc)}, 409
    except DNAExhausted as exc:
        # The DNA finishing is an expected end state, not a fault: bypass:100 on
        # a 30m grid lasts about eight trading days. Without this clause it fell
        # through to the generic handler and answered 500 on every slot forever,
        # pushing an error record each time and firing any 5xx alert with nothing
        # actionable in it. 200 with its own status says what happened and what
        # to do, and the scheduler stops treating it as an outage.
        return {"status": "PASS_DNA_EXHAUSTED", "committed": False,
                "pipeline_status": "DNA_EXHAUSTED", "note": str(exc),
                "hint": "ต่ออายุด้วย LEGO_DNA_CODE ที่ยาวขึ้น (chain ใหม่) "
                        "หรือหยุด scheduler ของ chain นี้"}, 200
    except Exception as exc:
        try:
            db.reference(ERRORS_PATH).push({
                "error": _error_text(exc, with_type=False),
                "type": type(exc).__name__,
                "trace": redact_sensitive_text(traceback.format_exc())[:2000],
            })
        except Exception:
            pass
        code = 503 if is_transient_exception(exc) else 500
        return {"status": "ERROR", "committed": False,
                "pipeline_status": "SNAPSHOT_OR_ENGINE_ERROR",
                "error": _error_text(exc, with_type=False),
                "type": type(exc).__name__}, code


@functions_framework.http
def lego_order_worker(request):
    """Independent worker; schedule separately when inline mode is disabled."""
    _init_firebase()
    try:
        cfg = load_config()
        environment_label()
        runtime_identity = runtime_identity_fingerprint()
        limit = int(os.environ.get("LEGO_ORDER_WORKER_LIMIT", "3"))
    except (KeyError, ValueError) as exc:
        return {
            "pipeline_status": "CONFIG_ERROR",
            "error": _error_text(exc, with_type=False),
        }, 500
    try:
        return {
            "pipeline_status": "ORDER_WORKER_OK",
            **_run_order_worker(
                cfg, limit, runtime_identity=runtime_identity),
        }, 200
    except RuntimeIdentityError as exc:
        return {
            "pipeline_status": "CONFIG_ERROR",
            "error": _error_text(exc, with_type=False),
        }, 500
    except Exception as exc:
        return {"pipeline_status": "ORDER_WORKER_ERROR",
                "error": _error_text(exc)}, 503


@functions_framework.http
def lego_archive_worker(request):
    """Daily housekeeping; touches no live intent and never runs on the DNA path."""
    _init_firebase()
    try:
        return {"pipeline_status": "ARCHIVE_OK",
                **archive_terminal_records(datetime.now(UTC))}, 200
    except Exception as exc:
        return {"pipeline_status": "ARCHIVE_ERROR",
                "error": _error_text(exc)}, 503
