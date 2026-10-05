"""Event model produced by the parser.

Date-times stay in the domain the feed declared (calendar date, wall time in
a zone, UTC instant, or floating). Conversion to absolute instants happens at
expansion time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

Kind = Literal["date", "utc", "zoned", "floating"]


@dataclass(frozen=True)
class IcsDateTime:
    """A date-time as written in the feed (after TZID normalization).

    - `date`: VALUE=DATE, the all-day domain. `value` is a `date`.
    - `utc`: a value with a `Z` suffix. `value` is a naive UTC `datetime`.
    - `zoned`: TZID=<IANA>. `value` is the naive wall time in `tz`.
    - `floating`: no `Z` and no TZID. Interpreted as UTC (host-independent).
    """

    kind: Kind
    value: date | datetime
    tz: ZoneInfo | None = None

    @property
    def is_date(self) -> bool:
        return self.kind == "date"

    @staticmethod
    def of_date(d: date) -> IcsDateTime:
        return IcsDateTime("date", d)

    @staticmethod
    def of_utc(dt: datetime) -> IcsDateTime:
        return IcsDateTime("utc", dt)

    @staticmethod
    def of_zoned(dt: datetime, tz: ZoneInfo) -> IcsDateTime:
        return IcsDateTime("zoned", dt, tz)

    @staticmethod
    def of_floating(dt: datetime) -> IcsDateTime:
        return IcsDateTime("floating", dt)


@dataclass
class ParsedEvent:
    uid: str
    dtstart: IcsDateTime
    summary: str | None = None
    description: str | None = None
    location: str | None = None
    # X-MICROSOFT-CDO-BUSYSTATUS raw value.
    busy_status_raw: str | None = None
    # X-MICROSOFT-SKYPETEAMSMEETINGURL raw value.
    teams_url_prop: str | None = None
    dtend: IcsDateTime | None = None
    rrule_raw: str | None = None
    # EXDATE values, multi-value lines and repeated lines merged.
    exdates: list[IcsDateTime] = field(default_factory=list)
    recurrence_id: IcsDateTime | None = None
    # SEQUENCE, 0 when absent.
    sequence: int = 0
    # Standard properties that Exchange does not emit. Used by the generic
    # profile only.
    status: str | None = None  # STATUS, upper case
    transp: str | None = None  # TRANSP, upper case
    rdates: list[IcsDateTime] = field(default_factory=list)

    @property
    def is_all_day(self) -> bool:
        return self.dtstart.is_date


@dataclass
class EventGroup:
    """All VEVENTs that share a UID: at most one master plus its overrides.

    `master is None` means orphan overrides whose master is not in the feed
    (Exchange emits these).
    """

    master: ParsedEvent | None
    overrides: list[ParsedEvent] = field(default_factory=list)


@dataclass
class ParsedCalendar:
    # Groups in feed order. First-wins de-duplication depends on this order.
    groups: list[EventGroup] = field(default_factory=list)
