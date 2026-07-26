"""Cloud Functions for time-aligned LEGO DNA and independent order execution.

lego_one_row: market clock -> snapshot -> model row -> durable outbox candidate.
lego_order_worker: dispatch/reconcile outbox intents without blocking DNA time.
lego_archive_worker: move finished order records out of the live paths.
"""
from __future__ import annotations

import os
import time
import traceback
from datetime import datetime, timedelta, timezone

import firebase_admin
import functions_framework
from firebase_admin import credentials, db

from lego_archive import archive_terminal_records
from lego_one_row import (READY_BUY, READY_SELL, DNAExhausted, HoldingsAnomaly,
                          check_holdings_continuity, compute_row, dna_step_for,
                          dna_steps_remaining)
from lego_orders import (TERMINAL_STATUSES, UAT, evaluate_submit_gate,
                         normalize_status, order_confirmation_phrase,
                         summarize_order_result)
from lego_outbox import (expire_unsent_before, list_actionable, put_intent,
                         read_committed_row, update_intent)
from lego_state import (CalendarDriftError, DNADriftError, OrdinalRegression,
                        SlotAlreadyConsumed, StaleAnchorError, apply_realized_fill,
                        chain_key, commit_final_row, read_anchor,
                        update_order_audit, write_order_audit)
from market_clock import (MarketClockError, clock_mode, fallback_slot_id,
                          is_regular_session, resolve_dna_step, resolve_market_slot,
                          slot_seconds)
from webull_io import (build_clients, build_order_payload, environment_label,
                       fetch_open_orders, fetch_order_detail, fetch_snapshot,
                       is_transient_exception, load_config, market_category,
                       place_market_order, preview_market_order, token_health)

ORDER_POLL_ATTEMPTS = 3
ORDER_POLL_DELAY_S = 2.0
UTC = timezone.utc
WARNINGS_PATH = "webull_lego_warnings"
ERRORS_PATH = "webull_lego_errors"


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
    """
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
        out = dict(summary)
        out["realized_warning"] = "fill confirmed but quantity/price unavailable"
        return out
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
    """Outbox and audit always move together; never write only one of them."""
    update_intent(chain_key_, run_id, fields)
    update_order_audit(run_id, fields)
    return {"run_id": run_id, **fields}


def _persist_summary(intent: dict, summary: dict) -> None:
    _persist(intent["chain_key"], intent["run_id"],
             {**summary, "status": normalize_status(summary.get("status"))})


def _persist_error(chain_key_: str, run_id: str, status: str, exc: Exception,
                   extra: dict | None = None) -> dict:
    err = f"{type(exc).__name__}: {exc}"
    _persist(chain_key_, run_id,
             {"status": status, "last_error": err[:500], **(extra or {})})
    return {"run_id": run_id, "status": status, "error": err}


def _reconcile_max_attempts() -> int:
    return max(1, int(os.environ.get("LEGO_RECONCILE_MAX_ATTEMPTS", "20")))


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
        extra["first_error"] = f"{type(exc).__name__}: {exc}"[:500]
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
        "last_error": f"{type(exc).__name__}: {exc}"[:500],
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


def _finish_with_realized(intent: dict, summary: dict) -> dict:
    """The broker has answered; the only failure left belongs to us."""
    try:
        summary = _apply_realized_if_available(intent, summary)
    except RealizedMathError as exc:
        return _persist_realized_math_error(intent, summary, exc)
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

    if status in {"PLACING_UNKNOWN", "PLACING", "SUBMITTED", "UNKNOWN",
                  "PARTIAL_FILLED", "PARTIALLY_FILLED"}:
        try:
            summary = summarize_order_result({}, fetch_order_detail(trade_client, run_id))
            if normalize_status(summary.get("status")) == "UNKNOWN":
                raise RuntimeError("broker order detail still UNKNOWN")
        except Exception as exc:
            # Everything inside this try is 'can we reach and read the broker?'.
            # The realized ledger is applied outside it so its failures are not
            # reported as an unresolved order.
            return _persist_reconcile_failure(intent, exc)
        return _finish_with_realized(intent, summary)

    if status != "PENDING_DISPATCH":
        return {"run_id": run_id, "status": status}

    expiry = datetime.fromisoformat(str(intent["expires_at"]).replace("Z", "+00:00"))
    if datetime.now(UTC) >= expiry:
        return _stop(ck, run_id, "EXPIRED_UNSENT")

    open_orders = fetch_open_orders(trade_client, cfg.symbol)
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

    _persist(ck, run_id, {"status": "PLACING_UNKNOWN", "place_attempted": True})
    try:
        place_res = place_market_order(trade_client, order)
        summary = _poll_order_status(trade_client, run_id, place_res)
    except Exception as exc:
        # Same open question as a failed reconcile — "does this order exist?" —
        # so it draws on the same bounded budget.
        return _persist_reconcile_failure(intent, exc)
    return _finish_with_realized(intent, summary)


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


def _run_order_worker(cfg, limit: int = 3) -> dict:
    trade_client, data_client = build_clients()
    ck = chain_key(cfg)
    expire_unsent_before(ck, datetime.now(UTC))
    results = []
    for intent in list_actionable(ck, limit=limit):
        results.append(_dispatch_or_reconcile_one(trade_client, data_client, cfg, intent))
    return {"processed": len(results), "results": results}


@functions_framework.http
def lego_one_row(request):
    """Commit the current model slot first. Order failures never block DNA time."""
    _init_firebase()
    cfg = load_config()
    decision_time = datetime.now(UTC)

    try:
        # Mandatory: the slot grid must match the timeframe the DNA was trained
        # on, and the clock mode decides which step the row gets. A missing or
        # unsupported value is a deploy error, not a runtime one, so all three are
        # resolved up front instead of surfacing later as an engine failure —
        # the market category included, since a typo there reaches the broker as
        # a query parameter and comes back as an unhelpful snapshot error.
        slot_seconds()
        mode = clock_mode()
        market_category()
    except (MarketClockError, ValueError) as exc:
        return {"status": "CONFIG_ERROR", "committed": False,
                "pipeline_status": "CONFIG_ERROR", "error": str(exc)}, 500

    # One calendar for every path: the same session rules the ordinal uses, so a
    # holiday or early close also blocks a degraded (clock-less) commit.
    if not is_regular_session(decision_time):
        return {"status": "PASS_MARKET_CLOSED", "committed": False,
                "pipeline_status": "MARKET_CLOSED"}, 200

    try:
        anchor = read_anchor(cfg)
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

        # Invariant #10: commit the slot first, then touch the outbox. A rejected
        # commit must not leave an intent behind, and the committed run_id is the
        # only client_order_id the worker may use.
        result = commit_final_row(
            cfg, snapshot, anchor, row, slot_id=slot_id, clock_mode=mode,
            market_ordinal=None if slot is None else slot.market_ordinal)

        env = environment_label()
        auto = os.environ.get("AUTO_SUBMIT", "false").lower() == "true"
        should_submit = auto and env == UAT and row["สถานะ"] in (READY_BUY, READY_SELL)
        row_durable = bool(result["committed"] or result.get("idempotent"))
        outbox_error = None
        outbox_skipped = None
        if should_submit and row_durable and slot is None:
            # A degraded clock has no slot window, so expires_at cannot be
            # computed and no intent can be created. Deciding that silently was
            # the worst of both worlds: the row still read READY_BUY with
            # committed=true and no error field, so a dashboard showed a working
            # bot that had never sent a single order. Say it on the response and
            # on a counter something can alert on.
            outbox_skipped = ("degraded clock: ไม่มี slot window จึงคำนวณ expires_at ไม่ได้ "
                              "— แถวนี้ commit แล้วแต่ไม่มีการสร้าง order intent")
            _record_warning("degraded_clock_no_order", outbox_skipped, {
                "run_id": result["run_id"], "row_status": row["สถานะ"],
                "clock_mode": mode, "hint": "ตั้ง LEGO_DNA_ORIGIN_UTC (find_origin.py) "
                                            "แล้วเปิด LEGO_DNA_CLOCK_MODE=market",
            })
        elif should_submit and row_durable:
            # The slot is already durable at this point, so a failed intent may
            # only cost this row its order — reporting it as a DNA failure would
            # be a lie and would invite a retry that finds the slot consumed.
            try:
                put_intent(chain_key(cfg), result["run_id"],
                           _outbox_intent(cfg, row, snapshot, slot, decision_time))
            except Exception as exc:
                outbox_error = f"{type(exc).__name__}: {exc}"

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
        # Warn while extending the DNA is still possible; silent until the last
        # few slots so a healthy chain keeps the response it has always had.
        remaining = dna_steps_remaining(cfg.dna_code, row["DNA step"])
        if remaining <= int(os.environ.get("LEGO_DNA_LOW_WATERMARK", "10")):
            out["dna_steps_remaining"] = remaining

        # Off by default: dispatching inline adds broker latency to the DNA
        # invocation, which raises the odds of a scheduler timeout+retry.
        if os.environ.get("LEGO_INLINE_ORDER_WORKER", "false").lower() == "true":
            try:
                out["order_worker"] = _run_order_worker(cfg, limit=1)
            except Exception as exc:
                out["order_worker"] = {"processed": 0, "error": f"{type(exc).__name__}: {exc}"}
        return out, 200

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
                "error": str(exc), "type": type(exc).__name__,
                "trace": traceback.format_exc()[:2000],
            })
        except Exception:
            pass
        code = 503 if is_transient_exception(exc) else 500
        return {"status": "ERROR", "committed": False,
                "pipeline_status": "SNAPSHOT_OR_ENGINE_ERROR",
                "error": str(exc), "type": type(exc).__name__}, code


@functions_framework.http
def lego_order_worker(request):
    """Independent worker; schedule separately when inline mode is disabled."""
    _init_firebase()
    cfg = load_config()
    try:
        limit = int(os.environ.get("LEGO_ORDER_WORKER_LIMIT", "3"))
        return {"pipeline_status": "ORDER_WORKER_OK", **_run_order_worker(cfg, limit)}, 200
    except Exception as exc:
        return {"pipeline_status": "ORDER_WORKER_ERROR",
                "error": f"{type(exc).__name__}: {exc}"}, 503


@functions_framework.http
def lego_archive_worker(request):
    """Daily housekeeping; touches no live intent and never runs on the DNA path."""
    _init_firebase()
    try:
        return {"pipeline_status": "ARCHIVE_OK",
                **archive_terminal_records(datetime.now(UTC))}, 200
    except Exception as exc:
        return {"pipeline_status": "ARCHIVE_ERROR",
                "error": f"{type(exc).__name__}: {exc}"}, 503
