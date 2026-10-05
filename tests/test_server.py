"""Tool handlers through `ServerState.call_tool`, plus MCP type conversion."""

import json

import pytest

from ics_cal_mcp.feed import FeedClient, HttpErr, HttpOk
from ics_cal_mcp.server import ServerState, UnknownToolError, build_tools, to_call_tool_result

from conftest import FIXTURE, FIXTURE_TRUNCATED, TEST_ICS_URL, TZ_NAME, make_config, utc_ms

GOOGLE_URL = "https://calendar.google.com/calendar/ical/me%40example.com/private-SECRETG/basic.ics"

# A small generic-profile feed for multi-calendar tests.
GENERIC_FEED = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
    "BEGIN:VEVENT\r\nUID:g1\r\nDTSTART:20260318T070000Z\r\nDTEND:20260318T073000Z\r\n"
    "SUMMARY:Gym\r\nTRANSP:TRANSPARENT\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:g2\r\nDTSTART;VALUE=DATE:20260318\r\nDTEND;VALUE=DATE:20260319\r\n"
    "SUMMARY:Birthday\r\nEND:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


class FixtureTransport:
    def __init__(self, body=FIXTURE, status=200, error=None):
        self.body, self.status, self.error = body, status, error
        self.calls = 0

    def get(self, url):
        self.calls += 1
        if self.error:
            raise self.error
        return HttpOk(self.status, self.body)


def state_with(transport=None, now_ms=1_000_000) -> ServerState:
    cfg = make_config()
    client = FeedClient(cfg, TEST_ICS_URL, transport or FixtureTransport(), lambda: now_ms)
    return ServerState(cfg, {"default": client})


def multi_state(work=None, home=None, now_ms=1_000_000) -> ServerState:
    """Two calendars: 'work' (Exchange fixture) and 'home' (generic feed)."""
    cfg = make_config({"home": GOOGLE_URL, "work": TEST_ICS_URL})
    transports = {
        "home": home or FixtureTransport(GENERIC_FEED),
        "work": work or FixtureTransport(),
    }
    urls = {"home": GOOGLE_URL, "work": TEST_ICS_URL}
    clients = {n: FeedClient(cfg, urls[n], transports[n], lambda: now_ms) for n in ("home", "work")}
    return ServerState(cfg, clients)


def structured(res):
    assert "isError" not in res, res
    text = json.loads(res["content"][0]["text"])
    assert text == res["structuredContent"]  # text mirrors structuredContent exactly
    return res["structuredContent"]


# --- single calendar (backward compatible behavior) -------------------------


def test_get_events_returns_the_expected_events_for_an_override_day_sorted():
    s = structured(state_with().call_tool("get_events", {"date": "2026-03-18"}))
    assert s["date"] == "2026-03-18"
    assert s["timezone"] == TZ_NAME
    assert s["errors"] == []
    assert [e["summary"] for e in s["events"]] == [
        "Dovolená",
        "Product check-in",
        "Team sync (moved)",
        "CEST name variant",
    ]
    assert {e["calendar"] for e in s["events"]} == {"default"}
    assert list(s["events"][0].keys()) == [
        "all_day",
        "busy_status",
        "calendar",
        "description",
        "end",
        "is_recurring",
        "location",
        "meeting_url",
        "start",
        "summary",
    ]
    assert list(s.keys()) == ["date", "errors", "events", "timezone"]


def test_get_events_defaults_to_today_in_tz_default():
    # 2026-03-17T23:30Z is already 2026-03-18 in Prague (UTC+1).
    s = structured(state_with(now_ms=utc_ms("2026-03-17T23:30:00Z")).call_tool("get_events", {}))
    assert s["date"] == "2026-03-18"


def test_malformed_date_arguments_become_is_error_results():
    st = state_with()
    res = st.call_tool("get_events", {"date": "18-03-2026"})
    assert res["isError"] is True
    assert "YYYY-MM-DD" in res["content"][0]["text"]
    res = st.call_tool("get_events_range", {"from": "2026-03-01"})
    assert res["isError"] is True
    assert "to " in res["content"][0]["text"]
    res = st.call_tool("get_events", {"date": 20260318})
    assert res["isError"] is True


def test_returns_is_error_for_a_well_formed_but_invalid_calendar_date():
    res = state_with().call_tool("get_events", {"date": "2026-02-30"})
    assert res["isError"] is True
    assert "valid calendar date" in res["content"][0]["text"]


def test_reports_fetch_failures_without_leaking_the_url():
    for transport in (
        FixtureTransport(status=503),
        FixtureTransport(error=HttpErr("timeout")),
        FixtureTransport(error=HttpErr("io", "ConnectError")),
    ):
        res = state_with(transport).call_tool("get_events", {"date": "2026-03-18"})
        assert res["isError"] is True
        text = res["content"][0]["text"]
        assert text.startswith("Failed to fetch the ICS feed from https://feedhost.example.com/…")
        assert "SECRETPATH123" not in json.dumps(res)


def test_event_tools_refuse_a_truncated_feed():
    res = state_with(FixtureTransport(FIXTURE_TRUNCATED)).call_tool(
        "get_events", {"date": "2026-03-18"}
    )
    assert res["isError"] is True
    assert "truncated" in res["content"][0]["text"]


def test_get_events_range_works_and_rejects_from_after_to():
    st = state_with()
    s = structured(st.call_tool("get_events_range", {"from": "2026-03-16", "to": "2026-03-18"}))
    assert (s["from"], s["to"], s["timezone"]) == ("2026-03-16", "2026-03-18", TZ_NAME)
    # 5 + 3 + 3: the multi-day "Dovolená" appears once in a range.
    assert len(s["events"]) == 11
    res = st.call_tool("get_events_range", {"from": "2026-03-02", "to": "2026-03-01"})
    assert res["isError"] is True
    assert "must be on or before" in res["content"][0]["text"]


def test_feed_info_reports_a_healthy_feed_without_leaking_the_secret():
    res = state_with().call_tool("feed_info", {})
    s = structured(res)
    assert s["errors"] == []
    [info] = s["calendars"]
    assert info["calendar"] == "default"
    assert info["ends_with_end_vcalendar"] is True
    assert info["feed_bytes"] == len(FIXTURE.encode("utf-8"))
    assert info["vevent_count"] == 18
    assert info["cache_age_seconds"] == 0
    assert info["source_host"] == "https://feedhost.example.com/…"
    assert info["last_fetch_at"] == "1970-01-01T00:16:40.000Z"
    assert info["dtstart_min"] is not None and info["dtstart_max"] is not None
    assert "SECRETPATH123" not in json.dumps(res)


def test_feed_info_still_answers_on_a_truncated_feed():
    res = state_with(FixtureTransport(FIXTURE_TRUNCATED)).call_tool("feed_info", {})
    [info] = structured(res)["calendars"]
    assert info["ends_with_end_vcalendar"] is False
    assert info["feed_bytes"] == len(FIXTURE_TRUNCATED.encode("utf-8"))
    assert info["dtstart_min"] is None and info["dtstart_max"] is None


def test_lists_exactly_the_four_tools_with_read_only_annotations():
    tools = build_tools(TZ_NAME, ["default"])
    assert [t["name"] for t in tools] == [
        "feed_info",
        "get_events",
        "get_events_range",
        "list_calendars",
    ]
    for tool in tools:
        assert tool["annotations"]["destructiveHint"] is False
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["inputSchema"]["type"] == "object"


def test_descriptions_interpolate_the_timezone_and_the_calendar_names():
    tools = build_tools(TZ_NAME, ["home", "work"])
    get_events = tools[1]
    assert TZ_NAME in get_events["description"]
    assert TZ_NAME in get_events["inputSchema"]["properties"]["date"]["description"]
    assert "Calendars: home, work." in get_events["description"]
    assert get_events["inputSchema"]["properties"]["calendars"]["items"]["enum"] == [
        "home",
        "work",
    ]


def test_unknown_tools_raise():
    with pytest.raises(UnknownToolError):
        state_with().call_tool("bogus", {})


def test_converts_results_to_mcp_types():
    ok = to_call_tool_result(state_with().call_tool("feed_info", {}))
    assert ok.is_error is False
    assert ok.structured_content["calendars"][0]["vevent_count"] == 18
    err = to_call_tool_result(state_with().call_tool("get_events", {"date": "x"}))
    assert err.is_error is True
    assert err.structured_content is None


# --- several calendars -------------------------------------------------------


def test_list_calendars_does_not_fetch_and_masks_urls():
    work = FixtureTransport()
    st = multi_state(work=work)
    res = st.call_tool("list_calendars", {})
    s = structured(res)
    assert s["calendars"] == [
        {"name": "home", "profile": "generic", "source_host": "https://calendar.google.com/…"},
        {"name": "work", "profile": "exchange", "source_host": "https://feedhost.example.com/…"},
    ]
    assert s["timezone"] == TZ_NAME
    assert work.calls == 0
    assert "SECRET" not in json.dumps(res)


def test_get_events_merges_calendars_sorted_and_labelled():
    s = structured(multi_state().call_tool("get_events", {"date": "2026-03-18"}))
    assert s["errors"] == []
    rows = [(e["calendar"], e["summary"], e["busy_status"]) for e in s["events"]]
    assert rows == [
        # All-day events first, by start date: "Dovolená" started on 03-16.
        ("work", "Dovolená", "OOF"),
        ("home", "Birthday", "BUSY"),
        ("home", "Gym", "FREE"),  # 08:00 Prague, generic profile: TRANSP -> FREE
        ("work", "Product check-in", "BUSY"),
        ("work", "Team sync (moved)", "TENTATIVE"),
        ("work", "CEST name variant", "BUSY"),
    ]


def test_each_calendar_uses_its_own_profile():
    s = structured(multi_state().call_tool("get_events", {"date": "2026-03-17"}))
    teams = next(e for e in s["events"] if e["summary"] == "Čtvrtletní review")
    # Exchange profile on 'work': busy status from X-MICROSOFT-CDO-BUSYSTATUS.
    assert teams["calendar"] == "work"
    assert teams["busy_status"] == "TENTATIVE"


def test_calendars_argument_filters_and_validates():
    st = multi_state()
    s = structured(st.call_tool("get_events", {"date": "2026-03-18", "calendars": ["home"]}))
    assert {e["calendar"] for e in s["events"]} == {"home"}
    for bad in ([], ["nope"], "home", [1]):
        res = st.call_tool("get_events", {"date": "2026-03-18", "calendars": bad})
        assert res["isError"] is True
        assert "calendar names: home, work" in res["content"][0]["text"]


def test_one_failed_calendar_gives_partial_results_with_errors():
    st = multi_state(home=FixtureTransport(status=500))
    s = structured(st.call_tool("get_events", {"date": "2026-03-18"}))
    assert {e["calendar"] for e in s["events"]} == {"work"}
    assert s["errors"] == [
        {
            "calendar": "home",
            "message": "Failed to fetch the ICS feed from https://calendar.google.com/…: HTTP 500",
        }
    ]


def test_all_failed_calendars_give_an_error_result_without_secrets():
    st = multi_state(
        home=FixtureTransport(error=HttpErr("timeout")),
        work=FixtureTransport(FIXTURE_TRUNCATED),
    )
    res = st.call_tool("get_events_range", {"from": "2026-03-16", "to": "2026-03-18"})
    assert res["isError"] is True
    text = res["content"][0]["text"]
    assert text.startswith("All requested calendars failed.")
    assert "home: Failed to fetch" in text and "work: ICS feed appears truncated" in text
    assert "SECRET" not in json.dumps(res)


def test_feed_info_per_calendar_with_partial_failure():
    st = multi_state(home=FixtureTransport(error=HttpErr("io", "ConnectError")))
    s = structured(st.call_tool("feed_info", {}))
    assert [c["calendar"] for c in s["calendars"]] == ["work"]
    assert s["calendars"][0]["profile"] == "exchange"
    assert s["errors"][0]["calendar"] == "home"
    assert s["errors"][0]["message"].endswith("ConnectError")
    only = structured(st.call_tool("feed_info", {"calendars": ["work"]}))
    assert only["errors"] == []


def test_each_calendar_has_its_own_cache():
    work, home = FixtureTransport(), FixtureTransport(GENERIC_FEED)
    st = multi_state(work=work, home=home)
    st.call_tool("get_events", {"date": "2026-03-18"})
    st.call_tool("get_events", {"date": "2026-03-19", "calendars": ["work"]})
    st.call_tool("get_events", {"date": "2026-03-19"})
    assert (work.calls, home.calls) == (1, 1)
