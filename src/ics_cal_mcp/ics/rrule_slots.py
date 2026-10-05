"""RRULE slot generation with `dateutil.rrule`, used only as an occurrence
generator. EXDATE is never given to dateutil: all exclusion and override
logic lives in `expand`. RDATE is not supported (Exchange does not emit it).
"""

from __future__ import annotations

import re
from datetime import datetime, time

from dateutil.rrule import rrulestr

from .model import IcsDateTime
from .timeutil import UTC, resolve_local, to_ms

# Safety cap against hostile rules (for example FREQ=SECONDLY). Reaching it
# is an error, never a hang.
MAX_ITERATIONS = 100_000

_UNTIL_DT_RE = re.compile(r"([0-9]{4})([0-9]{2})([0-9]{2})T([0-9]{2})([0-9]{2})([0-9]{2})")
_UNTIL_DATE_RE = re.compile(r"([0-9]{4})([0-9]{2})([0-9]{2})")


class SlotError(Exception):
    pass


def _until_to_utc(value: str, dtstart: IcsDateTime) -> str:
    """Resolve a Z-less UNTIL in the event's DTSTART zone, then write it as UTC.

    A date-only UNTIL means the END of that day (23:59:59 wall time), so the
    occurrence on the UNTIL date itself is still included. UTC, floating and
    all-day starts already live in the UTC domain.
    """
    m = _UNTIL_DT_RE.fullmatch(value)
    try:
        if m:
            naive = datetime(*(int(g) for g in m.groups()))
        else:
            d = _UNTIL_DATE_RE.fullmatch(value)
            if not d:
                return value  # unparseable: let dateutil reject it
            naive = datetime(int(d[1]), int(d[2]), int(d[3]), 23, 59, 59)
    except ValueError:
        return value
    if dtstart.kind == "zoned" and dtstart.tz is not None:
        naive = resolve_local(dtstart.tz, naive).astimezone(UTC).replace(tzinfo=None)
    return naive.strftime("%Y%m%dT%H%M%SZ")


def ensure_until_utc(rrule_raw: str, dtstart: IcsDateTime) -> str:
    parts = []
    for part in rrule_raw.split(";"):
        key, sep, value = part.partition("=")
        if sep and key.upper() == "UNTIL" and not value.upper().endswith("Z"):
            parts.append(f"{key}={_until_to_utc(value, dtstart)}")
        else:
            parts.append(part)
    return ";".join(parts)


def dtstart_for_rrule(dtstart: IcsDateTime) -> datetime:
    """Zoned wall time generates in its own zone (wall clock kept across DST).
    UTC/floating instants and all-day dates live in the UTC domain."""
    if dtstart.kind == "date":
        return datetime.combine(dtstart.value, time.min, tzinfo=UTC)
    if dtstart.kind in ("utc", "floating"):
        return dtstart.value.replace(tzinfo=UTC)
    assert dtstart.tz is not None
    return resolve_local(dtstart.tz, dtstart.value)


def series_slots(dtstart: IcsDateTime, rrule_raw: str, lower_ms: int, upper_ms: int) -> list[int]:
    """Occurrence instants (epoch ms) in [lower_ms, upper_ms], both inclusive."""
    rule_value = ensure_until_utc(rrule_raw, dtstart)
    start = dtstart_for_rrule(dtstart)
    try:
        rule = rrulestr(rule_value, dtstart=start)
    except (ValueError, TypeError, OverflowError) as e:
        raise SlotError(f"invalid RRULE: {e}") from e

    slots: list[int] = []
    try:
        for i, occurrence in enumerate(rule):
            if i >= MAX_ITERATIONS:
                raise SlotError(f"RRULE expansion exceeded {MAX_ITERATIONS} iterations")
            # dateutil keeps the wall time and attaches DTSTART's tzinfo.
            # Re-resolve with fold=0 so DST gaps and folds are deterministic.
            if occurrence.tzinfo is not None and occurrence.tzinfo is not UTC:
                occurrence = resolve_local(occurrence.tzinfo, occurrence.replace(tzinfo=None))
            ms = to_ms(occurrence)
            if ms < lower_ms:
                continue
            if ms > upper_ms:
                break
            slots.append(ms)
    except (ValueError, OverflowError) as e:
        raise SlotError(f"invalid RRULE: {e}") from e
    return slots
