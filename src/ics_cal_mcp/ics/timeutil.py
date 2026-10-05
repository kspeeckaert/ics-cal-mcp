"""Instant arithmetic in integer epoch milliseconds. Never uses the host zone."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .model import IcsDateTime

UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_ONE_MS = timedelta(milliseconds=1)


def to_ms(dt: datetime) -> int:
    """Aware datetime -> epoch milliseconds (exact integer arithmetic)."""
    return (dt - EPOCH) // _ONE_MS


def from_ms(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime."""
    return EPOCH + timedelta(milliseconds=ms)


def resolve_local(tz: ZoneInfo, naive: datetime) -> datetime:
    """Wall time in `tz` -> aware datetime, with 'compatible' disambiguation.

    `fold=0` (PEP 495) picks the earlier offset for a repeated hour and the
    pre-transition offset for a skipped hour. For a skipped hour this moves
    the instant forward by the length of the gap, the same result as
    Temporal's 'compatible' rule.
    """
    return naive.replace(tzinfo=tz, fold=0)


def midnight_ms(d: date, tz: ZoneInfo) -> int:
    return to_ms(resolve_local(tz, datetime.combine(d, time.min)))


def instant_ms(dt: IcsDateTime) -> int:
    """Absolute instant. All-day dates map to UTC midnight (the domain in
    which their recurrence slots are generated)."""
    if dt.kind == "date":
        return to_ms(datetime.combine(dt.value, time.min, tzinfo=UTC))
    if dt.kind in ("utc", "floating"):
        return to_ms(dt.value.replace(tzinfo=UTC))
    assert dt.tz is not None
    return to_ms(resolve_local(dt.tz, dt.value))


def date_part(dt: IcsDateTime) -> date:
    """Calendar date as written, without zone conversion."""
    if dt.kind == "date":
        return dt.value
    return dt.value.date()


def utc_ms_date(ms: int) -> date:
    return from_ms(ms).date()


def instant_date(ms: int, tz: ZoneInfo) -> date:
    return from_ms(ms).astimezone(tz).date()
