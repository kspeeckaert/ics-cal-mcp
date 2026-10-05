"""Minimal ICS content-line parser.

Input is already unfolded and TZID-normalized (see `tzids`). The parser
handles only what this server needs: BEGIN/END component tracking,
`NAME(;PARAM=VAL|"VAL")*:VALUE` lines, VALUE=DATE / TZID / Z date-time
literals, multi-value EXDATE, DURATION, SEQUENCE, and RFC 5545 section
3.3.11 text unescaping.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .. import log
from .model import EventGroup, IcsDateTime, ParsedCalendar, ParsedEvent
from .timeutil import from_ms, instant_ms
from .tzids import split_quote_aware, valid_iana, value_separator_index


class ParseError(Exception):
    """Truncated or malformed feed. Messages hold structure only, never
    property values, so they are safe for stderr."""


def unescape_text(value: str) -> str:
    r"""`\n`/`\N` -> newline. Any other escaped character -> itself."""
    out: list[str] = []
    i = 0
    n = len(value)
    while i < n:
        c = value[i]
        if c == "\\":
            if i + 1 < n:
                nxt = value[i + 1]
                out.append("\n" if nxt in "nN" else nxt)
                i += 2
                continue
            out.append("\\")
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _param(params: list[str], key: str) -> str | None:
    for p in params:
        k, sep, v = p.partition("=")
        if sep and k.upper() == key:
            return v.strip('"')
    return None


_DATE_RE = re.compile(r"[0-9]{8}")
_DATETIME_RE = re.compile(r"([0-9]{4})([0-9]{2})([0-9]{2})T([0-9]{2})([0-9]{2})([0-9]{2})")


def _parse_date(value: str) -> date:
    if not _DATE_RE.fullmatch(value):
        raise ParseError("invalid DATE literal")
    try:
        return date(int(value[0:4]), int(value[4:6]), int(value[6:8]))
    except ValueError as e:
        raise ParseError("invalid DATE literal") from e


def parse_datetime(
    value: str,
    params: list[str],
    tz_default: ZoneInfo,
    floating_tz: ZoneInfo | None = None,
) -> IcsDateTime:
    value_type = _param(params, "VALUE")
    if (value_type is not None and value_type.upper() == "DATE") or _DATE_RE.fullmatch(value):
        return IcsDateTime.of_date(_parse_date(value))
    is_utc = value.endswith("Z")
    body = value[:-1] if is_utc else value
    m = _DATETIME_RE.fullmatch(body)
    if not m:
        raise ParseError("invalid DATE-TIME literal")
    try:
        local = datetime(*(int(g) for g in m.groups()))
    except ValueError as e:
        raise ParseError("invalid DATE-TIME literal") from e
    if is_utc:
        return IcsDateTime.of_utc(local)
    tzid = _param(params, "TZID")
    if tzid is not None:
        # After normalization every TZID is valid IANA. Fall back defensively.
        return IcsDateTime.of_zoned(local, valid_iana(tzid) or tz_default)
    if floating_tz is not None:
        # Generic profile: a floating time is local time in the calendar zone.
        return IcsDateTime.of_zoned(local, floating_tz)
    return IcsDateTime.of_floating(local)


_I64_MAX = 2**63 - 1
_MAX_DURATION_SECONDS = _I64_MAX // 1000


def parse_duration(value: str) -> int | None:
    """RFC 5545 section 3.3.6 DURATION -> seconds. None when malformed."""
    sign = 1
    rest = value
    if rest.startswith("-"):
        sign, rest = -1, rest[1:]
    elif rest.startswith("+"):
        rest = rest[1:]
    if not rest.startswith("P"):
        return None
    rest = rest[1:]
    date_part, t, time_part = rest.partition("T")

    def accumulate(part: str, units: dict[str, int]) -> int | None:
        total = 0
        digits = ""
        for ch in part:
            if "0" <= ch <= "9":
                digits += ch
                continue
            if not digits or ch not in units:
                return None
            total += int(digits) * units[ch]
            if total > _I64_MAX:
                return None
            digits = ""
        return None if digits else total

    seconds = accumulate(date_part, {"W": 604_800, "D": 86_400})
    if seconds is None:
        return None
    if t:
        extra = accumulate(time_part, {"H": 3600, "M": 60, "S": 1})
        if extra is None:
            return None
        seconds += extra
    # Same bound as a millisecond count in a signed 64-bit integer.
    if seconds > _MAX_DURATION_SECONDS:
        return None
    return sign * seconds


def _end_from_duration(dtstart: IcsDateTime, seconds: int) -> IcsDateTime | None:
    """No DTEND: end = start + DURATION. Whole days for all-day starts,
    absolute arithmetic for timed starts. None when unrepresentable."""
    try:
        if dtstart.kind == "date":
            days = abs(seconds) // 86_400 * (1 if seconds >= 0 else -1)
            return IcsDateTime.of_date(dtstart.value + timedelta(days=days))
        end = from_ms(instant_ms(dtstart) + seconds * 1000)
        return IcsDateTime.of_utc(end.replace(tzinfo=None))
    except (OverflowError, ValueError):
        return None


@dataclass
class _EventBuilder:
    uid: str | None = None
    summary: str | None = None
    description: str | None = None
    location: str | None = None
    busy_status_raw: str | None = None
    teams_url_prop: str | None = None
    dtstart: IcsDateTime | None = None
    dtend: IcsDateTime | None = None
    duration: int | None = None
    rrule_raw: str | None = None
    exdates: list[IcsDateTime] = field(default_factory=list)
    recurrence_id: IcsDateTime | None = None
    sequence: int = 0
    status: str | None = None
    transp: str | None = None
    rdates: list[IcsDateTime] = field(default_factory=list)

    def finish(self) -> ParsedEvent:
        if self.uid is None:
            raise ParseError("VEVENT without UID")
        if self.dtstart is None:
            raise ParseError("VEVENT without DTSTART")
        dtend = self.dtend
        # DTEND wins over DURATION when a feed (invalidly) carries both.
        if dtend is None and self.duration is not None:
            dtend = _end_from_duration(self.dtstart, self.duration)
        return ParsedEvent(
            uid=self.uid,
            dtstart=self.dtstart,
            summary=self.summary,
            description=self.description,
            location=self.location,
            busy_status_raw=self.busy_status_raw,
            teams_url_prop=self.teams_url_prop,
            dtend=dtend,
            rrule_raw=self.rrule_raw,
            exdates=self.exdates,
            recurrence_id=self.recurrence_id,
            sequence=self.sequence,
            status=self.status,
            transp=self.transp,
            rdates=self.rdates,
        )


_MERGE_OPTIONAL = (
    "summary",
    "description",
    "location",
    "busy_status_raw",
    "teams_url_prop",
    "dtend",
    "rrule_raw",
    "recurrence_id",
    "status",
    "transp",
)


def _merge_component(existing: ParsedEvent, newer: ParsedEvent) -> None:
    """A winning duplicate merges key by key onto the stored component.

    Properties missing from the newer component survive from the older one,
    so a partial re-emit cannot erase a title or bring back an EXDATE'd
    occurrence.
    """
    existing.dtstart = newer.dtstart
    existing.sequence = newer.sequence
    for name in _MERGE_OPTIONAL:
        value = getattr(newer, name)
        if value is not None:
            setattr(existing, name, value)
    if newer.exdates:
        existing.exdates = newer.exdates
    if newer.rdates:
        existing.rdates = newer.rdates


def _parse_sequence(value: str) -> int:
    v = value.strip()
    if re.fullmatch(r"[+-]?[0-9]+", v):
        return int(v)
    return 0


def parse_calendar(
    ics: str, tz_default: ZoneInfo, *, floating_in_local_zone: bool = False
) -> ParsedCalendar:
    """Parse a normalized, unfolded feed into UID-grouped events.

    Only VEVENTs that are direct children of VCALENDAR count. VTIMEZONE and
    VALARM content is skipped. Raises ParseError on truncated input.

    With `floating_in_local_zone`, floating times use the calendar's
    X-WR-TIMEZONE (when it appears before the events), else `tz_default`.
    Without it, floating times are read as UTC.
    """
    floating_tz: ZoneInfo | None = tz_default if floating_in_local_zone else None
    stack: list[str] = []
    current: _EventBuilder | None = None
    events: list[ParsedEvent] = []

    for raw_line in ics.split("\n"):
        line = raw_line[:-1] if raw_line.endswith("\r") else raw_line
        if not line:
            continue
        sep = value_separator_index(line)
        if sep is None:
            continue  # structurally meaningless, skipped leniently
        head = line[:sep]
        value = line[sep + 1 :]
        params = split_quote_aware(head, ";")
        name = params[0].upper() if params else ""

        if name == "BEGIN":
            component = value.strip().upper()
            if component == "VEVENT" and len(stack) == 1 and current is None:
                current = _EventBuilder()
            stack.append(component)
            continue
        if name == "END":
            component = value.strip().upper()
            if not stack or stack.pop() != component:
                raise ParseError(f"unbalanced END:{component}")
            if component == "VEVENT" and len(stack) == 1 and current is not None:
                events.append(current.finish())
                current = None
            continue

        if (
            floating_in_local_zone
            and name == "X-WR-TIMEZONE"
            and len(stack) == 1
            and stack[0] == "VCALENDAR"
        ):
            floating_tz = valid_iana(value.strip()) or floating_tz
            continue

        # Only properties directly inside a VEVENT (VCALENDAR > VEVENT).
        if current is None or len(stack) != 2 or stack[-1] != "VEVENT":
            continue
        b = current
        if name == "UID":
            b.uid = unescape_text(value)
        elif name == "SUMMARY":
            b.summary = unescape_text(value)
        elif name == "DESCRIPTION":
            b.description = unescape_text(value)
        elif name == "LOCATION":
            b.location = unescape_text(value)
        elif name == "X-MICROSOFT-CDO-BUSYSTATUS":
            b.busy_status_raw = unescape_text(value)
        elif name == "X-MICROSOFT-SKYPETEAMSMEETINGURL":
            b.teams_url_prop = unescape_text(value)
        elif name == "DTSTART":
            b.dtstart = parse_datetime(value, params, tz_default, floating_tz)
        elif name == "DTEND":
            b.dtend = parse_datetime(value, params, tz_default, floating_tz)
        elif name == "DURATION":
            seconds = parse_duration(value)
            if seconds is None:
                log.warn(
                    "Ignoring unusable DURATION in feed; treating it as zero length",
                    duration=value,
                )
                seconds = 0
            b.duration = seconds
        elif name == "SEQUENCE":
            b.sequence = _parse_sequence(value)
        elif name == "RRULE":
            b.rrule_raw = value
        elif name == "EXDATE":
            for part in value.split(","):
                if part:
                    b.exdates.append(parse_datetime(part, params, tz_default, floating_tz))
        elif name == "STATUS":
            b.status = value.strip().upper()
        elif name == "TRANSP":
            b.transp = value.strip().upper()
        elif name == "RDATE":
            value_type = _param(params, "VALUE")
            if value_type is not None and value_type.upper() == "PERIOD":
                continue  # PERIOD values are not supported
            for part in value.split(","):
                if part:
                    b.rdates.append(parse_datetime(part, params, tz_default, floating_tz))
        elif name == "RECURRENCE-ID":
            b.recurrence_id = parse_datetime(value, params, tz_default, floating_tz)

    if stack:
        raise ParseError(f"unterminated component: {' > '.join(stack)}")
    return _group(events)


def _group(events: list[ParsedEvent]) -> ParsedCalendar:
    """Group by UID in feed order. The master is the VEVENT without
    RECURRENCE-ID. Overrides attach to it wherever they appear."""
    groups: list[EventGroup] = []
    index_by_uid: dict[str, int] = {}
    for ev in events:
        i = index_by_uid.get(ev.uid)
        if i is None:
            index_by_uid[ev.uid] = len(groups)
            if ev.recurrence_id is not None:
                groups.append(EventGroup(master=None, overrides=[ev]))
            else:
                groups.append(EventGroup(master=ev))
            continue
        group = groups[i]
        if ev.recurrence_id is not None:
            # Same original slot: higher SEQUENCE wins, ties go to the later
            # component. Identity is the exact original slot instant.
            rid_ms = instant_ms(ev.recurrence_id)
            existing = next(
                (
                    o
                    for o in group.overrides
                    if o.recurrence_id is not None and instant_ms(o.recurrence_id) == rid_ms
                ),
                None,
            )
            if existing is None:
                group.overrides.append(ev)
            elif ev.sequence >= existing.sequence:
                _merge_component(existing, ev)
        else:
            if group.master is None:
                group.master = ev
            elif ev.sequence >= group.master.sequence:
                _merge_component(group.master, ev)
    return ParsedCalendar(groups=groups)
