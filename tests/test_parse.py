"""ICS parser: grouping, TZIDs, EXDATE, escaping, DURATION and SEQUENCE."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from ics_cal_mcp.ics.model import EventGroup, IcsDateTime, ParsedCalendar
from ics_cal_mcp.ics.parse import ParseError, parse_calendar
from ics_cal_mcp.ics.timeutil import instant_ms
from ics_cal_mcp.ics.tzids import normalize_tzids, unfold_ics

from conftest import FIXTURE_TRUNCATED, TZ, TZ_NAME, parsed

BERLIN = ZoneInfo("Europe/Berlin")


def group(cal: ParsedCalendar, uid: str) -> EventGroup:
    for g in cal.groups:
        first = g.master or (g.overrides[0] if g.overrides else None)
        if first is not None and first.uid == uid:
            return g
    raise AssertionError(f"no group {uid}")


def zoned(y, mo, d, h, mi, tz=BERLIN) -> IcsDateTime:
    return IcsDateTime.of_zoned(datetime(y, mo, d, h, mi), tz)


def snippet(body: str) -> ParsedCalendar:
    ics = f"BEGIN:VCALENDAR\r\n{body.strip()}\r\nEND:VCALENDAR\r\n"
    return parse_calendar(ics, TZ)


def only_master(cal: ParsedCalendar):
    master = cal.groups[0].master
    assert master is not None
    return master


def test_parses_the_fixture_into_sixteen_groups_with_two_overrides():
    cal = parsed()
    assert len(cal.groups) == 16
    total = sum((1 if g.master else 0) + len(g.overrides) for g in cal.groups)
    assert total == 18
    assert len(group(cal, "weekly-ovr@fixture").overrides) == 1
    assert len(group(cal, "weekly-ovr-moved@fixture").overrides) == 1


def test_resolves_windows_tzids_to_zoned_wall_times():
    cal = parsed()
    ev = group(cal, "weekly-mon@fixture").master
    assert ev.dtstart == zoned(2026, 3, 2, 9, 0)
    assert ev.rrule_raw == "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO"
    cest = group(cal, "cest-tzid@fixture").master
    assert cest.dtstart.tz == ZoneInfo("Europe/Budapest")
    unknown = group(cal, "unknown-tzid@fixture").master
    assert unknown.dtstart.tz == ZoneInfo(TZ_NAME)


def test_merges_folded_multi_value_exdates():
    ev = group(parsed(), "weekly-exdate@fixture").master
    assert ev.exdates == [
        zoned(2026, 3, 9, 11, 0),
        zoned(2026, 3, 23, 11, 0),
        zoned(2026, 4, 6, 11, 0),
    ]


def test_keeps_all_day_events_in_the_calendar_date_domain():
    ev = group(parsed(), "allday-multi@fixture").master
    assert ev.is_all_day
    assert ev.dtstart == IcsDateTime.of_date(date(2026, 3, 16))
    assert ev.dtend == IcsDateTime.of_date(date(2026, 3, 19))


def test_unescapes_text_and_reassembles_folded_lines():
    ev = group(parsed(), "escape@fixture").master
    assert ev.summary == "Q3 review, part 1; plán"
    assert ev.description.startswith("line one\nline two, with a comma")
    assert "diakritika ěščřžýáíé" in ev.description
    assert "\\" not in ev.description


def test_captures_the_teams_meeting_url_x_prop():
    ev = group(parsed(), "teams-xprop@fixture").master
    assert ev.teams_url_prop.startswith("https://teams.microsoft.com/l/meetup-join/")
    assert ev.busy_status_raw == "BUSY"


def test_override_events_carry_their_recurrence_id_slot():
    ov = group(parsed(), "weekly-ovr@fixture").overrides[0]
    assert ov.recurrence_id == zoned(2026, 3, 18, 13, 0)
    assert ov.summary == "Team sync (moved)"


def test_errors_on_a_truncated_feed():
    normalized = normalize_tzids(unfold_ics(FIXTURE_TRUNCATED), TZ_NAME)
    with pytest.raises(ParseError):
        parse_calendar(normalized.ics, TZ)


def test_duration_synthesizes_the_end_when_dtend_is_absent():
    cal = snippet(
        "BEGIN:VEVENT\r\nUID:d1\r\nDTSTART;TZID=Europe/Berlin:20260303T100000\r\n"
        "DURATION:PT90M\r\nEND:VEVENT"
    )
    # 10:00 Berlin (CET) = 09:00Z; +90 min absolute = 10:30Z.
    assert only_master(cal).dtend == IcsDateTime.of_utc(datetime(2026, 3, 3, 10, 30))


def test_duration_on_all_day_events_stays_in_the_date_domain():
    cal = snippet(
        "BEGIN:VEVENT\r\nUID:d2\r\nDTSTART;VALUE=DATE:20260316\r\nDURATION:P3D\r\nEND:VEVENT"
    )
    assert only_master(cal).dtend == IcsDateTime.of_date(date(2026, 3, 19))
    weeks = snippet(
        "BEGIN:VEVENT\r\nUID:d3\r\nDTSTART;VALUE=DATE:20260316\r\nDURATION:P2W\r\nEND:VEVENT"
    )
    assert only_master(weeks).dtend == IcsDateTime.of_date(date(2026, 3, 30))


def test_dtend_wins_when_a_feed_carries_both_dtend_and_duration():
    cal = snippet(
        "BEGIN:VEVENT\r\nUID:d4\r\nDTSTART;TZID=Europe/Berlin:20260303T100000\r\n"
        "DTEND;TZID=Europe/Berlin:20260303T110000\r\nDURATION:PT15M\r\nEND:VEVENT"
    )
    assert only_master(cal).dtend == zoned(2026, 3, 3, 11, 0)


def test_duplicate_masters_keep_the_higher_sequence_regardless_of_order():
    newer_first = snippet(
        "BEGIN:VEVENT\r\nUID:s\r\nSEQUENCE:2\r\nDTSTART;TZID=Europe/Berlin:20260303T100000\r\n"
        "SUMMARY:newer\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:s\r\nSEQUENCE:1\r\nDTSTART;TZID=Europe/Berlin:20260303T120000\r\n"
        "SUMMARY:older\r\nEND:VEVENT"
    )
    assert only_master(newer_first).summary == "newer"
    newer_second = snippet(
        "BEGIN:VEVENT\r\nUID:s\r\nSEQUENCE:1\r\nDTSTART;TZID=Europe/Berlin:20260303T120000\r\n"
        "SUMMARY:older\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:s\r\nSEQUENCE:2\r\nDTSTART;TZID=Europe/Berlin:20260303T100000\r\n"
        "SUMMARY:newer\r\nEND:VEVENT"
    )
    assert only_master(newer_second).summary == "newer"
    # Ties (and the no-SEQUENCE default of 0) go to the later component.
    tie = snippet(
        "BEGIN:VEVENT\r\nUID:s\r\nDTSTART;TZID=Europe/Berlin:20260303T100000\r\n"
        "SUMMARY:first\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:s\r\nDTSTART;TZID=Europe/Berlin:20260303T120000\r\n"
        "SUMMARY:second\r\nEND:VEVENT"
    )
    assert only_master(tie).summary == "second"


def test_hostile_durations_degrade_to_zero_or_no_end():
    cal = snippet(
        "BEGIN:VEVENT\r\nUID:h1\r\nDTSTART;TZID=Europe/Berlin:20260303T100000\r\n"
        "DURATION:PT9223372036854775807S\r\nEND:VEVENT"
    )
    m = only_master(cal)
    assert instant_ms(m.dtend) == instant_ms(m.dtstart)  # zero length
    cal = snippet(
        "BEGIN:VEVENT\r\nUID:h2\r\nDTSTART;VALUE=DATE:20260316\r\nDURATION:P99999999D\r\nEND:VEVENT"
    )
    assert only_master(cal).dtend is None
    cal = snippet(
        "BEGIN:VEVENT\r\nUID:h3\r\nDTSTART;TZID=Europe/Berlin:20260303T100000\r\n"
        "DURATION:P99999999999999999W\r\nEND:VEVENT"
    )
    assert cal.groups[0].master is not None


def test_malformed_durations_never_fail_the_whole_feed():
    cal = snippet(
        "BEGIN:VEVENT\r\nUID:bad\r\nDTSTART;TZID=Europe/Berlin:20260303T100000\r\n"
        "DURATION:banana\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:good\r\nDTSTART;TZID=Europe/Berlin:20260304T100000\r\n"
        "DTEND;TZID=Europe/Berlin:20260304T110000\r\nSUMMARY:ok\r\nEND:VEVENT"
    )
    assert len(cal.groups) == 2
    bad = cal.groups[0].master
    assert instant_ms(bad.dtend) == instant_ms(bad.dtstart)


def test_duplicate_masters_merge_so_fields_absent_from_the_newer_one_survive():
    cal = snippet(
        "BEGIN:VEVENT\r\nUID:m\r\nSEQUENCE:1\r\nDTSTART;TZID=Europe/Berlin:20260304T130000\r\n"
        "DTEND;TZID=Europe/Berlin:20260304T133000\r\nRRULE:FREQ=WEEKLY;INTERVAL=1;BYDAY=WE\r\n"
        "EXDATE;TZID=Europe/Berlin:20260311T130000\r\nSUMMARY:old summary\r\n"
        "LOCATION:old location\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:m\r\nSEQUENCE:2\r\nDTSTART;TZID=Europe/Berlin:20260304T140000\r\n"
        "DTEND;TZID=Europe/Berlin:20260304T143000\r\nRRULE:FREQ=WEEKLY;INTERVAL=1;BYDAY=WE\r\n"
        "END:VEVENT"
    )
    master = only_master(cal)
    assert master.sequence == 2
    assert master.summary == "old summary"
    assert master.location == "old location"
    assert len(master.exdates) == 1
    assert master.dtstart == zoned(2026, 3, 4, 14, 0)


def test_duplicate_overrides_for_one_slot_keep_the_higher_sequence():
    cal = snippet(
        "BEGIN:VEVENT\r\nUID:o\r\nDTSTART;TZID=Europe/Berlin:20260304T130000\r\n"
        "DTEND;TZID=Europe/Berlin:20260304T133000\r\nRRULE:FREQ=WEEKLY;INTERVAL=1;BYDAY=WE\r\n"
        "SUMMARY:master\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:o\r\nSEQUENCE:5\r\nRECURRENCE-ID;TZID=Europe/Berlin:20260318T130000\r\n"
        "DTSTART;TZID=Europe/Berlin:20260318T150000\r\nSUMMARY:keep\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:o\r\nSEQUENCE:4\r\nRECURRENCE-ID;TZID=Europe/Berlin:20260318T130000\r\n"
        "DTSTART;TZID=Europe/Berlin:20260318T160000\r\nSUMMARY:drop\r\nEND:VEVENT"
    )
    overrides = cal.groups[0].overrides
    assert len(overrides) == 1
    assert overrides[0].summary == "keep"


def test_skips_valarm_and_vtimezone_content():
    cal = snippet(
        "BEGIN:VTIMEZONE\r\nTZID:Europe/Berlin\r\nBEGIN:STANDARD\r\nDTSTART:19701025T030000\r\n"
        "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU\r\nEND:STANDARD\r\nEND:VTIMEZONE\r\n"
        "BEGIN:VEVENT\r\nUID:a\r\nDTSTART:20260303T100000Z\r\nSUMMARY:outer\r\n"
        "BEGIN:VALARM\r\nDESCRIPTION:inner\r\nTRIGGER:-PT15M\r\nEND:VALARM\r\nEND:VEVENT"
    )
    m = only_master(cal)
    assert m.summary == "outer"
    assert m.description is None
    assert m.rrule_raw is None
