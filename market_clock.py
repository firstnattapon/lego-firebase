"""Canonical regular-session market clock for time-trained DNA.

The clock is deterministic and independent from broker/order state. It supports
NYSE regular sessions, observed holidays, common early closes, and an explicit
origin. Production must use the same calendar/session rules as DNA training.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
UTC = timezone.utc


class MarketClockError(RuntimeError):
    pass


@dataclass(frozen=True)
class MarketSlot:
    slot_id: str
    session_date: date
    session_open_utc: datetime
    session_close_utc: datetime
    slot_start_utc: datetime
    slot_end_utc: datetime
    slot_in_session: int
    market_ordinal: int


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    return d + timedelta(days=(weekday - d.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        d = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: date) -> date:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _easter_sunday(year: int) -> date:
    # Anonymous Gregorian computus.
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


@lru_cache(maxsize=64)
def us_market_holidays(year: int) -> frozenset[date]:
    holidays = {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),       # MLK
        _nth_weekday(year, 2, 0, 3),       # Presidents Day
        _easter_sunday(year) - timedelta(days=2),
        _last_weekday(year, 5, 0),         # Memorial Day
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),       # Labor Day
        _nth_weekday(year, 11, 3, 4),      # Thanksgiving
        _observed(date(year, 12, 25)),
    }
    if year >= 2022:
        holidays.add(_observed(date(year, 6, 19)))  # Juneteenth
    # New Year's observed can fall in the previous year.
    holidays.add(_observed(date(year + 1, 1, 1)))
    extra = os.environ.get("LEGO_MARKET_HOLIDAYS", "")
    for raw in extra.split(","):
        raw = raw.strip()
        if raw:
            holidays.add(date.fromisoformat(raw))
    return frozenset(holidays)


def _early_close(d: date) -> bool:
    # Day after Thanksgiving.
    thanksgiving = _nth_weekday(d.year, 11, 3, 4)
    if d == thanksgiving + timedelta(days=1):
        return True
    # Christmas Eve when it is a weekday and not itself an observed holiday.
    if d.month == 12 and d.day == 24 and d.weekday() < 5:
        return True
    # Common Independence Day early closes.
    if d.month == 7 and ((d.day == 3 and d.weekday() < 5) or
                         (d.day == 2 and d.weekday() == 4)):
        return True
    extra = os.environ.get("LEGO_MARKET_EARLY_CLOSES", "")
    return d.isoformat() in {x.strip() for x in extra.split(",") if x.strip()}


def session_bounds(session_date: date) -> tuple[datetime, datetime] | None:
    if session_date.weekday() >= 5 or session_date in us_market_holidays(session_date.year):
        return None
    open_ny = datetime.combine(session_date, time(9, 30), tzinfo=NY)
    close_ny = datetime.combine(session_date, time(13 if _early_close(session_date) else 16, 0), tzinfo=NY)
    return open_ny.astimezone(UTC), close_ny.astimezone(UTC)


def _parse_origin() -> datetime:
    raw = os.environ.get("LEGO_DNA_ORIGIN_UTC", "").strip()
    if not raw:
        raise MarketClockError("ต้องตั้ง LEGO_DNA_ORIGIN_UTC เช่น 2026-07-23T18:00:00Z")
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)


def _slot_seconds() -> int:
    sec = int(os.environ.get("LEGO_SLOT_SECONDS", "600"))
    if sec <= 0:
        raise MarketClockError("LEGO_SLOT_SECONDS ต้อง > 0")
    return sec


def _session_slots(d: date, sec: int) -> int:
    bounds = session_bounds(d)
    if not bounds:
        return 0
    start, end = bounds
    return int((end - start).total_seconds()) // sec


def _ordinal_from_origin(origin: datetime, slot_start: datetime, sec: int) -> int:
    if slot_start < origin:
        raise MarketClockError("เวลาปัจจุบันอยู่ก่อน LEGO_DNA_ORIGIN_UTC")
    origin_date = origin.astimezone(NY).date()
    slot_date = slot_start.astimezone(NY).date()
    origin_bounds = session_bounds(origin_date)
    slot_bounds = session_bounds(slot_date)
    if not origin_bounds or not slot_bounds:
        raise MarketClockError("origin หรือ slot ไม่อยู่ใน regular market session")
    origin_open, _ = origin_bounds
    slot_open, _ = slot_bounds
    origin_index = int((origin - origin_open).total_seconds()) // sec
    slot_index = int((slot_start - slot_open).total_seconds()) // sec
    total = -origin_index + slot_index
    d = origin_date
    while d < slot_date:
        total += _session_slots(d, sec)
        d += timedelta(days=1)
    return total


def resolve_market_slot(at: datetime | None = None) -> MarketSlot | None:
    """Return the canonical slot containing *at*, or None outside regular hours."""
    at = (at or datetime.now(UTC)).astimezone(UTC)
    sec = _slot_seconds()
    session_date = at.astimezone(NY).date()
    bounds = session_bounds(session_date)
    if not bounds:
        return None
    session_open, session_close = bounds
    if not (session_open <= at < session_close):
        return None
    slot_index = int((at - session_open).total_seconds()) // sec
    slot_start = session_open + timedelta(seconds=slot_index * sec)
    slot_end = min(slot_start + timedelta(seconds=sec), session_close)
    origin = _parse_origin()
    ordinal = _ordinal_from_origin(origin, slot_start, sec)
    return MarketSlot(
        slot_id=f"{session_date.isoformat()}:{slot_index}",
        session_date=session_date,
        session_open_utc=session_open,
        session_close_utc=session_close,
        slot_start_utc=slot_start,
        slot_end_utc=slot_end,
        slot_in_session=slot_index,
        market_ordinal=ordinal,
    )


def resolve_dna_step(legacy_step: int, slot: MarketSlot) -> tuple[int, int]:
    """Return (effective_step, alignment_error).

    shadow: preserve legacy output and only report alignment.
    market: market ordinal is authoritative.
    legacy: old behavior, provided only for rollback.
    """
    mode = os.environ.get("LEGO_DNA_CLOCK_MODE", "shadow").strip().lower()
    if mode not in {"shadow", "market", "legacy"}:
        raise MarketClockError("LEGO_DNA_CLOCK_MODE ต้องเป็น shadow|market|legacy")
    error = legacy_step - slot.market_ordinal
    return (slot.market_ordinal if mode == "market" else legacy_step), error
