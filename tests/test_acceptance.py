"""`get_events` for known dates must return the exact hand-computed list."""

from typing import Any

from ics_cal_mcp.ics.format import to_api_event

from conftest import TZ, instances_on


def api_events_on(date: str) -> list[dict[str, Any]]:
    return [to_api_event(inst, TZ) for inst in instances_on(date)]


def assert_matches_subset(actual: Any, expected: Any, path: str) -> None:
    """Every key in `expected` must match recursively. Lists must have the
    same length and match element by element."""
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: expected object, got {actual!r}"
        for key, value in expected.items():
            assert key in actual, f"{path}.{key}: missing (actual: {actual!r})"
            assert_matches_subset(actual[key], value, f"{path}.{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list), f"{path}: expected list, got {actual!r}"
        assert len(actual) == len(expected), f"{path}: length mismatch (actual: {actual!r})"
        for i, (a, e) in enumerate(zip(actual, expected, strict=True)):
            assert_matches_subset(a, e, f"{path}[{i}]")
    else:
        assert actual == expected, f"{path}: {actual!r} != {expected!r}"


def assert_day(date: str, expected: list[dict[str, Any]]) -> None:
    assert_matches_subset(api_events_on(date), expected, date)


def test_monday_2026_03_16_two_all_day_events_plus_three_timed_series_instances():
    assert_day(
        "2026-03-16",
        [
            {
                "all_day": True,
                "busy_status": "OOF",
                "end": "2026-03-19",
                "start": "2026-03-16",
                "summary": "Dovolená",
            },
            {
                "all_day": True,
                "busy_status": "OOF",
                "end": "2026-03-17",
                "start": "2026-03-16",
                "summary": "Školení",
            },
            {
                "all_day": False,
                "is_recurring": True,
                "location": "Zasedačka A",
                "start": "2026-03-16T09:00:00+01:00",
                "summary": "Weekly standup",
            },
            {"start": "2026-03-16T10:00:00+01:00", "summary": "Q3 review, part 1; plán"},
            {
                "is_recurring": True,
                "start": "2026-03-16T11:00:00+01:00",
                "summary": "Architecture board",
            },
        ],
    )


def test_wednesday_2026_03_18_the_moved_override_replaces_the_base_occurrence():
    assert_day(
        "2026-03-18",
        [
            {"all_day": True, "summary": "Dovolená"},
            {"start": "2026-03-18T14:00:00+01:00", "summary": "Product check-in"},
            {
                "busy_status": "TENTATIVE",
                "is_recurring": True,
                "location": "Zasedačka C",
                "start": "2026-03-18T15:00:00+01:00",
                "summary": "Team sync (moved)",
            },
            {"start": "2026-03-18T16:00:00+01:00", "summary": "CEST name variant"},
        ],
    )


def test_monday_2026_03_30_after_the_dst_change_times_keep_local_wall_clock_at_plus2():
    assert_day(
        "2026-03-30",
        [
            {"start": "2026-03-30T09:00:00+02:00", "summary": "Weekly standup"},
            {"start": "2026-03-30T11:00:00+02:00", "summary": "Architecture board"},
        ],
    )


def test_tuesday_2026_03_17_teams_zoom_biweekly_with_meeting_urls_extracted():
    assert_day(
        "2026-03-17",
        [
            {"all_day": True, "summary": "Dovolená"},
            {
                "busy_status": "TENTATIVE",
                "description": "Agenda: výsledky Q1 a plán Q2",
                "start": "2026-03-17T09:00:00+01:00",
                "summary": "Čtvrtletní review",
            },
            {"start": "2026-03-17T10:00:00+01:00", "summary": "Biweekly sync"},
            {
                "meeting_url": "https://example.zoom.us/j/2718281828?pwd=Zm9vYmFy",
                "start": "2026-03-17T11:00:00+01:00",
                "summary": "External call",
            },
        ],
    )
