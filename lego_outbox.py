"""Multi-intent RTDB order outbox, independent from the DNA state pointer."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from firebase_admin import db

from lego_orders import normalize_status as _normalize_status

OUTBOX_PATH = "webull_lego_order_outbox"
ROWS_PATH = "webull_lego_rows"
# Terminal for the *outbox*: nothing more to dispatch. Wider than
# lego_orders.TERMINAL_STATUSES, which only means the broker is done.
TERMINAL = {
    "FILLED", "CANCELLED", "FAILED", "REJECTED", "EXPIRED_UNSENT",
    # The broker's own EXPIRED — a DAY order the session ended on — as opposed to
    # EXPIRED_UNSENT, which is ours for an intent that never left. It was missing
    # here while lego_orders.TERMINAL_STATUSES had it, so such an intent stayed
    # actionable forever: no branch in the dispatcher handles it, yet
    # list_actionable kept serving it oldest-first. Three of them fill
    # LEGO_ORDER_WORKER_LIMIT and no later decision is ever dispatched again —
    # the same starvation RECONCILE_ABANDONED was added to prevent.
    "EXPIRED",
    "SUPPRESSED_ACTIVE_ORDER", "SUPPRESSED_STATE_CHANGED", "NOT_PLACED",
    # The broker never resolved this order and the reconcile budget ran out.
    # Terminal for dispatch only — the order audit keeps needs_manual_check so
    # a human still answers whether the order exists.
    "RECONCILE_ABANDONED",
    # The broker confirmed the fill; only our realized math could not use it.
    # Nothing is left to dispatch — re-sending would duplicate a filled order —
    # so it must leave the queue, and needs_manual_check keeps the ledger gap
    # visible. Without this it would sit actionable forever and starve later
    # decisions exactly the way RECONCILE_ABANDONED was introduced to prevent.
    "REALIZED_MATH_ERROR",
}


def normalize_status(value) -> str:
    """Same normalization as lego_orders; a missing status reads as UNKNOWN."""
    return _normalize_status(value or "UNKNOWN")


def put_intent(chain_key: str, run_id: str, payload: dict) -> dict:
    """Idempotently create one intent per committed decision candidate."""
    ref = db.reference(f"{OUTBOX_PATH}/{chain_key}/{run_id}")
    doc = dict(payload)
    doc.update({
        "run_id": run_id,
        "client_order_id": run_id,
        "chain_key": chain_key,
        "status": normalize_status(doc.get("status") or "PENDING_DISPATCH"),
    })

    def txn(current):
        if current:
            return current
        return doc

    return ref.transaction(txn) or doc


def update_intent(chain_key: str, run_id: str, fields: dict) -> dict:
    ref = db.reference(f"{OUTBOX_PATH}/{chain_key}/{run_id}")

    def txn(current):
        current = dict(current or {})
        incoming = dict(fields)
        old_status = normalize_status(current.get("status"))
        new_status = normalize_status(incoming.get("status")) if "status" in incoming else old_status
        # Terminal statuses are absorbing, and an in-flight intent can never be
        # reset to PENDING_DISPATCH by a stale worker response.
        if old_status in TERMINAL and new_status != old_status:
            incoming.pop("status", None)
        elif new_status == "PENDING_DISPATCH" and old_status not in {
                "", "UNKNOWN", "PENDING_DISPATCH"}:
            incoming.pop("status", None)
        current.update(incoming)
        current["status"] = normalize_status(current.get("status"))
        current["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return current

    return ref.transaction(txn) or {}


def _claim_lease_seconds() -> int:
    return max(5, int(os.environ.get("LEGO_ORDER_CLAIM_LEASE_SECONDS", "120")))


def _parse_utc(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def claim_intent(chain_key: str, run_id: str, worker_id: str, *,
                 now_utc: datetime | None = None,
                 lease_seconds: int | None = None) -> dict | None:
    """Atomically lease one non-terminal intent without changing its status."""
    now_utc = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    lease_seconds = _claim_lease_seconds() if lease_seconds is None else max(
        1, int(lease_seconds))
    lease_until = now_utc + timedelta(seconds=lease_seconds)
    lease_text = lease_until.strftime("%Y-%m-%dT%H:%M:%SZ")
    ref = db.reference(f"{OUTBOX_PATH}/{chain_key}/{run_id}")

    def txn(current):
        if not isinstance(current, dict):
            return current
        doc = dict(current)
        if normalize_status(doc.get("status")) in TERMINAL:
            return doc
        owner = str(doc.get("claim_owner") or "")
        active_until = _parse_utc(doc.get("claim_until"))
        if owner and owner != worker_id and active_until and active_until > now_utc:
            return doc
        doc.update({
            "claim_owner": worker_id,
            "claim_until": lease_text,
            "claim_generation": int(doc.get("claim_generation", 0) or 0) + 1,
        })
        return doc

    result = ref.transaction(txn)
    if not isinstance(result, dict):
        return None
    if result.get("claim_owner") != worker_id or result.get("claim_until") != lease_text:
        return None
    return result


def begin_place_attempt(chain_key: str, run_id: str, worker_id: str,
                        claim_generation: int) -> dict | None:
    """Fence the irreversible broker call behind the current claim generation."""
    ref = db.reference(f"{OUTBOX_PATH}/{chain_key}/{run_id}")
    fence = f"{worker_id}:{int(claim_generation)}"

    def txn(current):
        if not isinstance(current, dict):
            return current
        doc = dict(current)
        if normalize_status(doc.get("status")) != "PENDING_DISPATCH":
            return doc
        if doc.get("claim_owner") != worker_id:
            return doc
        if int(doc.get("claim_generation", 0) or 0) != int(claim_generation):
            return doc
        doc.update({
            "status": "PLACING_UNKNOWN",
            "place_attempted": True,
            "place_fence": fence,
            "audit_pending": True,
            "updated_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
        })
        return doc

    result = ref.transaction(txn)
    if not isinstance(result, dict) or result.get("place_fence") != fence:
        return None
    return result


def release_intent_claim(chain_key: str, run_id: str, worker_id: str) -> None:
    """Release only the lease owned by this worker; a newer owner is untouched."""
    ref = db.reference(f"{OUTBOX_PATH}/{chain_key}/{run_id}")

    def txn(current):
        if not isinstance(current, dict):
            return current
        doc = dict(current)
        if doc.get("claim_owner") == worker_id:
            doc.pop("claim_owner", None)
            doc.pop("claim_until", None)
        return doc

    ref.transaction(txn)


def list_actionable(chain_key: str, limit: int = 20) -> list[dict]:
    raw = db.reference(f"{OUTBOX_PATH}/{chain_key}").get() or {}
    rows = []
    for run_id, payload in raw.items():
        if not isinstance(payload, dict):
            continue
        status = normalize_status(payload.get("status"))
        if status in TERMINAL:
            continue
        doc = dict(payload)
        doc.setdefault("run_id", run_id)
        rows.append(doc)
    rows.sort(key=lambda x: (str(x.get("slot_start_utc") or x.get("created_at") or ""),
                             str(x.get("run_id") or "")))
    return rows[:limit]


def list_audit_pending(chain_key: str, limit: int = 100) -> list[dict]:
    """Intents whose outbox update committed but audit mirror still needs repair."""
    raw = db.reference(f"{OUTBOX_PATH}/{chain_key}").get() or {}
    rows = []
    for run_id, payload in raw.items():
        if not isinstance(payload, dict) or not payload.get("audit_pending"):
            continue
        doc = dict(payload)
        doc.setdefault("run_id", run_id)
        rows.append(doc)
    rows.sort(key=lambda x: (
        str(x.get("updated_at") or x.get("created_at") or ""),
        str(x.get("run_id") or ""),
    ))
    return rows[:limit]


def read_committed_row(run_id: str) -> dict | None:
    """The committed row behind an intent, or None when it is not committed.

    The dispatcher already had to read this row to answer 'was it committed?';
    returning the document itself lets the submit gate compare the intent against
    what the engine actually persisted, at no extra read.
    """
    row = db.reference(f"{ROWS_PATH}/{run_id}").get()
    return row if isinstance(row, dict) and row.get("committed") is True else None


def row_is_committed(run_id: str) -> bool:
    return read_committed_row(run_id) is not None


def expire_unsent_before(chain_key: str, now_utc: datetime) -> int:
    count = 0
    for intent in list_actionable(chain_key, limit=100):
        if normalize_status(intent.get("status")) != "PENDING_DISPATCH":
            continue
        claim_until = _parse_utc(intent.get("claim_until"))
        if intent.get("claim_owner") and claim_until and claim_until > now_utc:
            continue
        raw = intent.get("expires_at")
        if not raw:
            continue
        expiry = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if now_utc >= expiry:
            update_intent(chain_key, intent["run_id"], {
                "status": "EXPIRED_UNSENT",
                "terminal_reason": "slot execution window expired before place",
            })
            count += 1
    return count
