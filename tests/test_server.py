"""Tool handlers through `ServerState.call_tool`, plus MCP type conversion."""

import json

import pytest

from ics_cal_mcp.feed import FeedClient, HttpErr, HttpOk
from ics_cal_mcp.server import ServerState, UnknownToolError, build_tools, to_call_tool_result

from conftest import FIXTURE, FIXTURE_TRUNCATED, TZ_NAME, make_config, utc_ms


class FixtureTransport:
    def __init__(self, body=FIXTURE, status=200, error=None):
        self.body, self.status, self.error = body, status, error

    def get(self, url):
        if self.error:
            raise self.error
        return HttpOk(self.status, self.body)


def state_with(transport=None, now_ms=1_000_000) -> ServerState:
    cfg = make_config()
    feed = FeedClient(cfg, transport or FixtureTransport(), lambda: now_ms)
    return ServerState(cfg, feed)


def structured(res):
    assert "isError" not in res, res
    text = json.loads(res["content"][0]["text"])
    assert text == res["structuredContent"]  # text mirrors structuredContent exactly
    return res["structuredContent"]


def test_get_events_returns_the_expected_events_for_an_override_day_sorted():
    s = structured(state_with().call_tool("get_events", {"date": "2026-03-18"}))
    assert s["date"] == "2026-03-18"
    assert s["timezone"] == TZ_NAME
    assert [e["summary"] for e in s["events"]] == [
        "Dovolená",
        "Product check-in",
        "Team sync (moved)",
        "CEST name variant",
    ]
    assert list(s["events"][0].keys()) == [
        "all_day",
        "busy_status",
        "description",
        "end",
        "is_recurring",
        "location",
        "meeting_url",
        "start",
        "summary",
    ]


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
    assert s["ends_with_end_vcalendar"] is True
    assert s["feed_bytes"] == len(FIXTURE.encode("utf-8"))
    assert s["vevent_count"] == 18
    assert s["cache_age_seconds"] == 0
    assert s["source_host"] == "https://feedhost.example.com/…"
    assert s["last_fetch_at"] == "1970-01-01T00:16:40.000Z"
    assert s["dtstart_min"] is not None and s["dtstart_max"] is not None
    assert "SECRETPATH123" not in json.dumps(res)


def test_feed_info_still_answers_on_a_truncated_feed():
    res = state_with(FixtureTransport(FIXTURE_TRUNCATED)).call_tool("feed_info", {})
    s = structured(res)
    assert s["ends_with_end_vcalendar"] is False
    assert s["feed_bytes"] == len(FIXTURE_TRUNCATED.encode("utf-8"))
    assert s["dtstart_min"] is None and s["dtstart_max"] is None


def test_lists_exactly_the_three_tools_with_read_only_annotations():
    tools = build_tools(TZ_NAME)
    assert [t["name"] for t in tools] == ["feed_info", "get_events", "get_events_range"]
    for tool in tools:
        assert tool["annotations"]["destructiveHint"] is False
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["inputSchema"]["type"] == "object"


def test_descriptions_interpolate_the_configured_timezone():
    get_events = build_tools(TZ_NAME)[1]
    assert TZ_NAME in get_events["description"]
    assert TZ_NAME in get_events["inputSchema"]["properties"]["date"]["description"]


def test_unknown_tools_raise():
    with pytest.raises(UnknownToolError):
        state_with().call_tool("bogus", {})


def test_converts_results_to_mcp_types():
    ok = to_call_tool_result(state_with().call_tool("feed_info", {}))
    assert ok.is_error is False
    assert ok.structured_content["vevent_count"] == 18
    err = to_call_tool_result(state_with().call_tool("get_events", {"date": "x"}))
    assert err.is_error is True
    assert err.structured_content is None
