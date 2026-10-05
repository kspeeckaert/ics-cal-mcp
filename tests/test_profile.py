"""Feed profile detection and the rules each profile applies."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from ics_cal_mcp.config import load_config
from ics_cal_mcp.errors import ConfigError
from ics_cal_mcp.feed import FeedClient, HttpOk
from ics_cal_mcp.ics.expand import day_window, expand_events
from ics_cal_mcp.ics.format import to_api_event
from ics_cal_mcp.ics.parse import parse_calendar
from ics_cal_mcp.profile import EXCHANGE, GENERIC, is_exchange_url, select_profile
from ics_cal_mcp.server import ServerState

from conftest import FIXTURE, TZ, TZ_NAME, make_config, utc_ms

# --- detection ------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://outlook.office365.com/owa/calendar/a@b.com/x/calendar.ics",
        "https://OUTLOOK.OFFICE.COM/anything.ics",
        "https://outlook.live.com/owa/calendar/00000000/cid-1/calendar.ics",
        "https://mail.contoso.example/owa/calendar/x@contoso.example/y/calendar.ics",
        "https://mail.contoso.example/OWA/Calendar/x/y/reachcalendar.ics",
    ],
)
def test_detects_exchange_feeds(url):
    assert is_exchange_url(url)
    assert select_profile(url) is EXCHANGE


@pytest.mark.parametrize(
    "url",
    [
        "https://calendar.google.com/calendar/ical/x%40gmail.com/private-abc/basic.ics",
        "https://p01-caldav.icloud.com/published/2/abc",
        "https://example.com/feeds/cal.ics",
        "https://outlook.office365.com.evil.example/owa-lookalike.ics",
        "not a url",
    ],
)
def test_other_feeds_use_the_generic_profile(url):
    assert not is_exchange_url(url)
    assert select_profile(url) is GENERIC


def test_ics_profile_overrides_detection():
    google = "https://calendar.google.com/calendar/ical/x/basic.ics"
    assert load_config({"ICS_URL": google}).profile is GENERIC
    assert load_config({"ICS_URL": google, "ICS_PROFILE": "Exchange"}).profile is EXCHANGE
    exchange = "https://outlook.office365.com/owa/calendar/x/y/calendar.ics"
    assert load_config({"ICS_URL": exchange, "ICS_PROFILE": "generic"}).profile is GENERIC
    assert load_config({"ICS_URL": exchange, "ICS_PROFILE": "auto"}).profile is EXCHANGE


def test_rejects_an_unknown_profile():
    with pytest.raises(ConfigError, match="ICS_PROFILE"):
        load_config({"ICS_URL": "https://example.com/c.ics", "ICS_PROFILE": "gmail"})


def test_feed_info_reports_the_profile():
    cfg = make_config()  # the fixture URL has /owa/calendar/ in its path
    state = ServerState(cfg, FeedClient(cfg, _Fixed(FIXTURE), lambda: 0))
    assert state.call_tool("feed_info", {})["structuredContent"]["profile"] == "exchange"


class _Fixed:
    def __init__(self, body):
        self.body = body

    def get(self, url):
        return HttpOk(200, self.body)


# --- rules ----------------------------------------------------------------


def cal(body: str, *, floating_local: bool = False, header: str = ""):
    ics = f"BEGIN:VCALENDAR\r\n{header}{body.strip()}\r\nEND:VCALENDAR\r\n"
    return parse_calendar(ics, TZ, floating_in_local_zone=floating_local)


def events_on(c, date, profile):
    return [
        to_api_event(i, TZ, profile) for i in expand_events(c, day_window(date, TZ, 0), profile)
    ]


EXCHANGE_STYLE = (
    "BEGIN:VEVENT\r\nUID:x\r\nDTSTART:20260310T090000Z\r\nDTEND:20260310T100000Z\r\n"
    "SUMMARY:x\r\nX-MICROSOFT-CDO-BUSYSTATUS:OOF\r\n"
    "X-MICROSOFT-SKYPETEAMSMEETINGURL:https://teams.microsoft.com/l/meetup-join/PROP\r\n"
    "DESCRIPTION:Agenda\\n________________\\nhttps://teams.microsoft.com/l/meetup-join/DESC\r\n"
    "TRANSP:TRANSPARENT\r\nEND:VEVENT"
)


def test_exchange_profile_uses_microsoft_props_and_strips_teams_boilerplate():
    [e] = events_on(cal(EXCHANGE_STYLE), "2026-03-10", EXCHANGE)
    assert e["busy_status"] == "OOF"
    assert e["meeting_url"].endswith("/PROP")
    assert e["description"] == "Agenda"


def test_generic_profile_ignores_microsoft_props_and_keeps_the_full_description():
    [e] = events_on(cal(EXCHANGE_STYLE), "2026-03-10", GENERIC)
    assert e["busy_status"] == "FREE"  # from TRANSP:TRANSPARENT
    assert e["meeting_url"].endswith("/DESC")  # found in the description
    assert "________________" in e["description"]


def test_generic_busy_status_from_status_tentative():
    c = cal("BEGIN:VEVENT\r\nUID:t\r\nDTSTART:20260310T090000Z\r\nSTATUS:TENTATIVE\r\nEND:VEVENT")
    assert events_on(c, "2026-03-10", GENERIC)[0]["busy_status"] == "TENTATIVE"
    assert events_on(c, "2026-03-10", EXCHANGE)[0]["busy_status"] == "BUSY"


CANCELLED = (
    "BEGIN:VEVENT\r\nUID:series\r\nDTSTART;TZID=Europe/Prague:20260302T090000\r\n"
    "DTEND;TZID=Europe/Prague:20260302T093000\r\nRRULE:FREQ=WEEKLY;BYDAY=MO\r\n"
    "SUMMARY:standup\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:series\r\nRECURRENCE-ID;TZID=Europe/Prague:20260309T090000\r\n"
    "DTSTART;TZID=Europe/Prague:20260309T090000\r\nDTEND;TZID=Europe/Prague:20260309T093000\r\n"
    "STATUS:CANCELLED\r\nSUMMARY:standup\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:gone\r\nDTSTART:20260309T120000Z\r\nSTATUS:CANCELLED\r\n"
    "SUMMARY:gone\r\nEND:VEVENT"
)


def test_generic_profile_skips_cancelled_events_and_occurrences():
    c = cal(CANCELLED)
    assert events_on(c, "2026-03-09", GENERIC) == []
    assert [e["summary"] for e in events_on(c, "2026-03-16", GENERIC)] == ["standup"]


def test_exchange_profile_keeps_cancelled_items_like_the_rust_original():
    summaries = [e["summary"] for e in events_on(cal(CANCELLED), "2026-03-09", EXCHANGE)]
    assert summaries == ["standup", "gone"]


def test_a_cancelled_master_cancels_the_whole_series_in_generic():
    c = cal(
        "BEGIN:VEVENT\r\nUID:s\r\nDTSTART:20260302T090000Z\r\nRRULE:FREQ=DAILY\r\n"
        "STATUS:CANCELLED\r\nEND:VEVENT"
    )
    assert events_on(c, "2026-03-05", GENERIC) == []


RDATES = (
    "BEGIN:VEVENT\r\nUID:r\r\nDTSTART;TZID=Europe/Prague:20260302T090000\r\n"
    "DTEND;TZID=Europe/Prague:20260302T100000\r\nRRULE:FREQ=WEEKLY;COUNT=2\r\n"
    "RDATE;TZID=Europe/Prague:20260304T150000,20260320T150000\r\nSUMMARY:r\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:only-rdate\r\nDTSTART:20260305T080000Z\r\nDTEND:20260305T090000Z\r\n"
    "RDATE:20260306T080000Z\r\nSUMMARY:only\r\nEND:VEVENT"
)


def test_generic_profile_adds_rdate_occurrences():
    c = cal(RDATES)
    [extra] = events_on(c, "2026-03-04", GENERIC)
    assert extra["start"] == "2026-03-04T15:00:00+01:00"
    assert extra["end"] == "2026-03-04T16:00:00+01:00"
    assert extra["is_recurring"] is True
    assert len(events_on(c, "2026-03-20", GENERIC)) == 1
    assert len(events_on(c, "2026-03-09", GENERIC)) == 1  # RRULE still applies
    # RDATE without RRULE: DTSTART plus the RDATE dates.
    assert [e["summary"] for e in events_on(c, "2026-03-05", GENERIC)] == ["only"]
    assert [e["summary"] for e in events_on(c, "2026-03-06", GENERIC)] == ["only"]


def test_exchange_profile_ignores_rdate_like_the_rust_original():
    c = cal(RDATES)
    assert events_on(c, "2026-03-04", EXCHANGE) == []
    assert events_on(c, "2026-03-06", EXCHANGE) == []


FLOATING = "BEGIN:VEVENT\r\nUID:f\r\nDTSTART:20260310T090000\r\nSUMMARY:f\r\nEND:VEVENT"


def test_floating_times_are_utc_in_exchange_and_local_in_generic():
    [ex] = events_on(cal(FLOATING), "2026-03-10", EXCHANGE)
    assert ex["start"] == "2026-03-10T10:00:00+01:00"  # 09:00 UTC
    [gen] = events_on(cal(FLOATING, floating_local=True), "2026-03-10", GENERIC)
    assert gen["start"] == "2026-03-10T09:00:00+01:00"  # 09:00 TZ_DEFAULT


def test_generic_floating_times_use_x_wr_timezone():
    c = cal(FLOATING, floating_local=True, header="X-WR-TIMEZONE:America/New_York\r\n")
    ev = c.groups[0].master
    assert ev.dtstart.tz == ZoneInfo("America/New_York")
    start = expand_events(c, day_window("2026-03-10", TZ, 0), GENERIC)[0].time.start_ms
    # 09:00 EDT (DST in the US started on 2026-03-08) = 13:00 UTC.
    assert start == utc_ms("2026-03-10T13:00:00Z")
    assert datetime(2026, 3, 10, 9) == ev.dtstart.value


def test_the_exchange_fixture_gives_the_same_core_results_in_both_profiles():
    """Recurrence, overrides, EXDATE and time zones do not depend on the profile."""
    from conftest import parsed

    win = day_window("2026-03-18", TZ, 0)
    exch = [i.event.uid for i in expand_events(parsed(), win, EXCHANGE)]
    gen = [i.event.uid for i in expand_events(parsed(), win, GENERIC)]
    assert exch == gen
    assert TZ_NAME == "Europe/Prague"
