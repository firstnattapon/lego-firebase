"""Move finished order records off the paths the live loops scan.

list_actionable and pending_audits each read a whole RTDB path and filter in
Python. That is correct and cheap while the path is small, and it stays correct
forever — but not cheap: every slot adds one intent and one audit, so after a
year the order worker downloads thousands of finished records on every tick to
find at most a handful of live ones, and the dashboard pays the same cost on
every page load.

Records are moved, never deleted, so the history stays queryable at
*_archive; only the working set shrinks. Two records are always left in place:
anything a human still has to answer (needs_manual_check) and anything that
carries no usable timestamp — an undatable record is never old enough to move.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from firebase_admin import db

from lego_orders import TERMINAL_STATUSES, normalize_status
from lego_outbox import OUTBOX_PATH, TERMINAL
from lego_state import AUDIT_PATH

UTC = timezone.utc
OUTBOX_ARCHIVE_PATH = f"{OUTBOX_PATH}_archive"
AUDIT_ARCHIVE_PATH = f"{AUDIT_PATH}_archive"
# An audit is finished when either ledger says so: the outbox has nothing left to
# dispatch, or the broker has nothing left to do.
AUDIT_TERMINAL = TERMINAL | TERMINAL_STATUSES
TIMESTAMP_FIELDS = ("updated_at", "placed_at", "created_at",
                    "decision_time", "slot_start_utc")


def retention_days() -> int:
    return max(1, int(os.environ.get("LEGO_ARCHIVE_RETENTION_DAYS", "30")))


def archive_limit() -> int:
    return max(1, int(os.environ.get("LEGO_ARCHIVE_LIMIT", "500")))


def _parse_ts(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _finished_before(doc: dict, cutoff: datetime) -> bool:
    """True only when every timestamp on the record is older than *cutoff*."""
    stamps = [ts for ts in (_parse_ts(doc.get(f)) for f in TIMESTAMP_FIELDS) if ts]
    return bool(stamps) and max(stamps) < cutoff


def _movable(doc, terminal: set[str], cutoff: datetime) -> bool:
    return (isinstance(doc, dict)
            and normalize_status(doc.get("status")) in terminal
            and not doc.get("needs_manual_check")
            and not doc.get("audit_pending")
            and _finished_before(doc, cutoff))


def _move(source_path: str, archive_path: str, key: str, doc: dict) -> None:
    """Write the copy first: a crash between the two leaves a duplicate, not a
    hole, and the next run re-moves it idempotently."""
    db.reference(f"{archive_path}/{key}").set(doc)
    db.reference(f"{source_path}/{key}").delete()


def archive_terminal_intents(cutoff: datetime, limit: int) -> int:
    moved = 0
    for ck, intents in (db.reference(OUTBOX_PATH).get() or {}).items():
        if not isinstance(intents, dict):
            continue
        for run_id, doc in intents.items():
            if moved >= limit:
                return moved
            if _movable(doc, TERMINAL, cutoff):
                _move(f"{OUTBOX_PATH}/{ck}", f"{OUTBOX_ARCHIVE_PATH}/{ck}", run_id, doc)
                moved += 1
    return moved


def archive_terminal_audits(cutoff: datetime, limit: int) -> int:
    moved = 0
    for event_id, doc in (db.reference(AUDIT_PATH).get() or {}).items():
        if moved >= limit:
            return moved
        if _movable(doc, AUDIT_TERMINAL, cutoff):
            _move(AUDIT_PATH, AUDIT_ARCHIVE_PATH, event_id, doc)
            moved += 1
    return moved


def archive_terminal_records(now_utc: datetime | None = None, *,
                             days: int | None = None, limit: int | None = None) -> dict:
    now_utc = (now_utc or datetime.now(UTC)).astimezone(UTC)
    days = retention_days() if days is None else days
    limit = archive_limit() if limit is None else limit
    cutoff = now_utc - timedelta(days=days)
    return {
        "cutoff": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "retention_days": days,
        "intents_archived": archive_terminal_intents(cutoff, limit),
        "audits_archived": archive_terminal_audits(cutoff, limit),
    }
