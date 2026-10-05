"""Recurrence expansion, the correctness core.

Handles RRULE series, EXDATE (timed and date-only, multi-value,
TZID-qualified), RECURRENCE-ID overrides (in place, moved across days, or
moved into the window from outside), orphan overrides whose master is not in
the feed, and all-day events with an exclusive DTEND.

Timed instances live in the absolute-instant domain (epoch ms). All-day
instances stay in the calendar-date domain and never pass through fake
midnights. Nothing here consults the host time zone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from ..errors import InvalidRangeError
from ..profile import EXCHANGE, FeedProfile
from .model import IcsDateTime, ParsedCalendar, ParsedEvent
from .rrule_slots import series_slots
from .timeutil import date_part, instant_date, instant_ms, midnight_ms, utc_ms_date

MS_PER_DAY = 86_400_000
MAX_RANGE_DAYS = 31


@dataclass(frozen=True)
class EventWindow:
    start_date: date  # inclusive
    end_date: date  # EXCLUSIVE
    start_ms: int  # inclusive
    end_ms: int  # exclusive
    tz: ZoneInfo


@dataclass(frozen=True)
class Timed:
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class AllDay:
    start: date
    end: date  # exclusive


@dataclass(frozen=True)
class ExpandedInstance:
    event: ParsedEvent
    time: Timed | AllDay
    is_recurring: bool

    @property
    def is_all_day(self) -> bool:
        return isinstance(self.time, AllDay)


def _plain_date(iso: str, field: str) -> date:
    try:
        y, m, d = iso.split("-")
        if len(y) != 4 or len(m) != 2 or len(d) != 2:
            raise ValueError
        return date(int(y), int(m), int(d))
    except ValueError:
        raise InvalidRangeError(
            f'{field} must be a valid calendar date in YYYY-MM-DD format (got "{iso}")'
        ) from None


def _window_from_dates(start: date, end_exclusive: date, tz: ZoneInfo) -> EventWindow:
    return EventWindow(
        start_date=start,
        end_date=end_exclusive,
        start_ms=midnight_ms(start, tz),
        end_ms=midnight_ms(end_exclusive, tz),
        tz=tz,
    )


def day_window(date_iso: str | None, tz: ZoneInfo, now_ms: int) -> EventWindow:
    """Window for one local day. `None` means today in `tz` at `now_ms`."""
    d = instant_date(now_ms, tz) if date_iso is None else _plain_date(date_iso, "date")
    return _window_from_dates(d, d + timedelta(days=1), tz)


def range_window(from_iso: str, to_iso: str, tz: ZoneInfo) -> EventWindow:
    """Window for an inclusive date range of at most 31 days."""
    start = _plain_date(from_iso, "from")
    end = _plain_date(to_iso, "to")
    if end < start:
        raise InvalidRangeError(f'"from" ({from_iso}) must be on or before "to" ({to_iso})')
    inclusive_days = (end - start).days + 1
    if inclusive_days > MAX_RANGE_DAYS:
        raise InvalidRangeError(
            f"range spans {inclusive_days} days; the maximum is {MAX_RANGE_DAYS} days"
        )
    return _window_from_dates(start, end + timedelta(days=1), tz)


def _timed_overlaps(win: EventWindow, start_ms: int, end_ms: int) -> bool:
    if start_ms == end_ms:
        # Zero-length events use half-open containment.
        return win.start_ms <= start_ms < win.end_ms
    return start_ms < win.end_ms and end_ms > win.start_ms


def _overlaps(win: EventWindow, inst: ExpandedInstance) -> bool:
    t = inst.time
    if isinstance(t, Timed):
        return _timed_overlaps(win, t.start_ms, t.end_ms)
    return t.start < win.end_date and t.end > win.start_date


def _from_component(component: ParsedEvent, is_recurring: bool) -> ExpandedInstance:
    """Instance from a component's own DTSTART/DTEND (non-recurring events,
    overrides, orphans)."""
    if component.is_all_day:
        start = date_part(component.dtstart)
        end = date_part(component.dtend) if component.dtend else start + timedelta(days=1)
        if end <= start:
            end = start + timedelta(days=1)
        return ExpandedInstance(component, AllDay(start, end), is_recurring)
    start_ms = instant_ms(component.dtstart)
    # A DTEND before DTSTART is clamped to zero length.
    end_ms = instant_ms(component.dtend) if component.dtend else start_ms
    return ExpandedInstance(component, Timed(start_ms, max(end_ms, start_ms)), is_recurring)


def _sort_key(win: EventWindow, inst: ExpandedInstance) -> tuple[int, str]:
    # All-day events sort at their local midnight in the window zone, so they
    # come before same-morning timed events. Ties: summary, codepoint order.
    t = inst.time
    ms = t.start_ms if isinstance(t, Timed) else midnight_ms(t.start, win.tz)
    return ms, inst.event.summary or ""


def _cancelled(profile: FeedProfile, ev: ParsedEvent) -> bool:
    return profile.skip_cancelled and ev.status == "CANCELLED"


def _rdate_slots(master: ParsedEvent, lower_ms: int, upper_ms: int) -> list[int]:
    return [ms for ms in (instant_ms(rd) for rd in master.rdates) if lower_ms <= ms <= upper_ms]


def expand_events(
    cal: ParsedCalendar, win: EventWindow, profile: FeedProfile = EXCHANGE
) -> list[ExpandedInstance]:
    """Expand every event group into concrete instances overlapping `win`.

    De-duplication key: uid + ORIGINAL slot instant (RECURRENCE-ID), never the
    moved start. First wins. This prevents a master occurrence and its
    override from both being returned.

    The generic profile also leaves out STATUS:CANCELLED events and
    occurrences, and adds RDATE occurrences.
    """
    seen: set[tuple[str, int]] = set()
    out: list[ExpandedInstance] = []

    def emit(uid: str, slot_ms: int, inst: ExpandedInstance) -> None:
        key = (uid, slot_ms)
        if key not in seen:
            seen.add(key)
            out.append(inst)

    for group in cal.groups:
        master = group.master
        if master is None:
            # Orphan overrides: each is one instance keyed by its original slot.
            for ov in group.overrides:
                if ov.recurrence_id is None or _cancelled(profile, ov):
                    continue
                inst = _from_component(ov, True)
                if _overlaps(win, inst):
                    emit(ov.uid, instant_ms(ov.recurrence_id), inst)
            continue

        if _cancelled(profile, master):
            continue  # a cancelled master cancels the whole series

        use_rdates = profile.rdate and bool(master.rdates)
        if master.rrule_raw is None and not use_rdates:
            # Non-recurring event. Attached overrides, if any, are ignored.
            inst = _from_component(master, False)
            if _overlaps(win, inst):
                emit(master.uid, instant_ms(master.dtstart), inst)
            continue

        # --- RRULE series ---
        all_day = master.is_all_day
        start_ms = instant_ms(master.dtstart)
        dur_ms = max(instant_ms(master.dtend) - start_ms, 0) if master.dtend else 0
        if all_day:
            dur_days = (
                (date_part(master.dtend) - date_part(master.dtstart)).days if master.dtend else 1
            )
            dur_days = max(dur_days, 1)
        else:
            dur_days = 0

        # EXDATE sets. Timed EXDATEs match by instant. Date-only EXDATEs match
        # the occurrence's calendar day.
        ex_times: set[int] = set()
        ex_dates: set[date] = set()
        for ex in master.exdates:
            if ex.kind == "date":
                ex_dates.add(ex.value)
            else:
                ex_times.add(instant_ms(ex))

        event_tz = _event_tz(master.dtstart, win.tz)

        # Overrides keyed by their ORIGINAL slot instant, in sorted order.
        overrides: dict[int, ParsedEvent] = {}
        for ov in group.overrides:
            if ov.recurrence_id is not None:
                overrides[instant_ms(ov.recurrence_id)] = ov
        overrides = dict(sorted(overrides.items()))

        # Widen backwards so occurrences that start before the window but
        # reach into it (for example 23:30-00:30) are found.
        widen_ms = (dur_days + 1) * MS_PER_DAY if all_day else dur_ms
        lower_ms = win.start_ms - widen_ms
        if master.rrule_raw is not None:
            slots = series_slots(master.dtstart, master.rrule_raw, lower_ms, win.end_ms)
        else:
            # RDATE without RRULE: DTSTART is the first occurrence.
            slots = [start_ms] if lower_ms <= start_ms <= win.end_ms else []
        if use_rdates:
            slots = sorted(set(slots) | set(_rdate_slots(master, lower_ms, win.end_ms)))

        consumed: set[int] = set()
        for slot_ms in slots:
            if slot_ms in ex_times:
                continue
            if ex_dates:
                slot_date = utc_ms_date(slot_ms) if all_day else instant_date(slot_ms, event_tz)
                if slot_date in ex_dates:
                    continue
            ov = overrides.get(slot_ms)
            if ov is not None:
                # The override replaces the base slot, even when it moved out
                # of the window: the base occurrence must not reappear.
                consumed.add(slot_ms)
                if _cancelled(profile, ov):
                    continue  # cancelled occurrence
                inst = _from_component(ov, True)
                if _overlaps(win, inst):
                    emit(master.uid, slot_ms, inst)
                continue
            if all_day:
                start = utc_ms_date(slot_ms)
                inst = ExpandedInstance(
                    master, AllDay(start, start + timedelta(days=dur_days)), True
                )
            else:
                inst = ExpandedInstance(master, Timed(slot_ms, slot_ms + dur_ms), True)
            if _overlaps(win, inst):
                emit(master.uid, slot_ms, inst)

        # Overrides whose original slot is outside the queried slots (moved in
        # from another day, or on an EXDATE'd slot: override wins over EXDATE).
        for slot_ms, ov in overrides.items():
            if slot_ms in consumed or _cancelled(profile, ov):
                continue
            inst = _from_component(ov, True)
            if _overlaps(win, inst):
                emit(master.uid, slot_ms, inst)

    out.sort(key=lambda inst: _sort_key(win, inst))
    return out


def _event_tz(dtstart: IcsDateTime, fallback: ZoneInfo) -> ZoneInfo:
    if dtstart.kind == "zoned" and dtstart.tz is not None:
        return dtstart.tz
    if dtstart.kind == "utc":
        return ZoneInfo("UTC")
    return fallback
