"""Multi-intent RTDB order outbox, independent from the DNA state pointer."""
from __future__ import annotations

from datetime import datetime, timezone
from firebase_admin import db

from lego_orders import normalize_status as _normalize_status

OUTBOX_PATH = "webull_lego_order_outbox"
ROWS_PATH = "webull_lego_rows"
# Terminal for the *outbox*: nothing more to dispatch. Wider than
# lego_orders.TERMINAL_STATUSES, which only means the broker is done.
TERMINAL = {
    "FILLED", "CANCELLED", "FAILED", "REJECTED", "EXPIRED_UNSENT",
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
        current.update(fields)
        current["status"] = normalize_status(current.get("status"))
        current["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return current

    return ref.transaction(txn) or {}


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
