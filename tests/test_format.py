"""Output shaping: offsets, busy status, meeting URL, description cleanup."""

from datetime import datetime

from ics_cal_mcp.ics.expand import ExpandedInstance, Timed
from ics_cal_mcp.ics.format import clean_description, extract_meeting_url, to_api_event
from ics_cal_mcp.ics.model import IcsDateTime, ParsedEvent

from conftest import TZ, instance_of, utc_ms


def api(date: str, uid: str) -> dict:
    inst = instance_of(date, uid)
    assert inst is not None, f"no instance of {uid} on {date}"
    return to_api_event(inst, TZ)


def fake_event(**kw) -> ParsedEvent:
    return ParsedEvent(
        uid="fake@test",
        summary="x",
        dtstart=IcsDateTime.of_utc(datetime(2026, 3, 20, 22, 30)),
        **kw,
    )


def instance_over(ev: ParsedEvent) -> ExpandedInstance:
    return ExpandedInstance(
        ev, Timed(utc_ms("2026-03-20T22:30:00Z"), utc_ms("2026-03-20T23:30:00Z")), False
    )


def test_renders_the_tz_default_offset_plus1_in_winter_plus2_in_summer():
    winter = api("2026-03-23", "weekly-mon@fixture")
    assert winter["all_day"] is False
    assert winter["start"] == "2026-03-23T09:00:00+01:00"
    assert winter["end"] == "2026-03-23T09:30:00+01:00"
    summer = api("2026-03-30", "weekly-mon@fixture")
    assert summer["start"] == "2026-03-30T09:00:00+02:00"
    assert summer["end"] == "2026-03-30T09:30:00+02:00"


def test_renders_date_only_with_the_exclusive_end_date():
    event = api("2026-03-16", "allday-multi@fixture")
    assert event["all_day"] is True
    assert event["busy_status"] == "OOF"
    assert (event["start"], event["end"]) == ("2026-03-16", "2026-03-19")
    assert event["summary"] == "Dovolená"


def test_unescapes_commas_semicolons_and_reassembles_folded_lines():
    event = api("2026-03-16", "escape@fixture")
    assert event["summary"] == "Q3 review, part 1; plán"
    description = event["description"]
    assert "line one\nline two, with a comma" in description
    assert "mid-word to prove" in description
    assert "diakritika ěščřžýáíé" in description
    assert "\\" not in description


def test_maps_all_four_exchange_busy_values():
    assert api("2026-03-23", "weekly-mon@fixture")["busy_status"] == "BUSY"
    assert api("2026-03-17", "teams@fixture")["busy_status"] == "TENTATIVE"
    assert api("2026-03-16", "escape@fixture")["busy_status"] == "FREE"
    assert api("2026-03-16", "allday-1@fixture")["busy_status"] == "OOF"


def test_the_override_carries_its_own_busy_status():
    assert api("2026-03-18", "weekly-ovr@fixture")["busy_status"] == "TENTATIVE"


def test_busy_defaults_to_busy_when_missing_or_unknown():
    assert to_api_event(instance_over(fake_event()), TZ)["busy_status"] == "BUSY"
    weird = fake_event(busy_status_raw="WORKINGELSEWHERE")
    assert to_api_event(instance_over(weird), TZ)["busy_status"] == "BUSY"


def test_finds_the_teams_link_inside_the_description_boilerplate():
    url = api("2026-03-17", "teams@fixture")["meeting_url"]
    assert url.startswith("https://teams.microsoft.com/l/meetup-join/19%3ameeting_ZmVlZDESCRIPTION")


def test_prefers_the_x_prop_over_the_description():
    url = api("2026-03-19", "teams-xprop@fixture")["meeting_url"]
    assert "meeting_WFBSTVBYUFJPUA" in url
    assert "REVDT1lERUNPWQ" not in url


def test_falls_back_to_a_zoom_link_in_the_location():
    assert (
        api("2026-03-17", "zoom-loc@fixture")["meeting_url"]
        == "https://example.zoom.us/j/2718281828?pwd=Zm9vYmFy"
    )


def test_meeting_url_is_null_when_there_is_no_link():
    assert api("2026-03-23", "weekly-mon@fixture")["meeting_url"] is None


def test_trims_trailing_punctuation_and_supports_google_meet():
    ev = fake_event(description="Join: https://meet.google.com/abc-defg-hij.")
    assert extract_meeting_url(ev) == "https://meet.google.com/abc-defg-hij"


def test_strips_everything_from_the_teams_underscore_separator_on():
    assert api("2026-03-17", "teams@fixture")["description"] == "Agenda: výsledky Q1 a plán Q2"


def test_description_is_null_for_missing_or_boilerplate_only_text():
    assert clean_description(None) is None
    assert clean_description("________________\nMicrosoft Teams meeting") is None
    assert clean_description("   \n ") is None


def test_caps_at_2000_characters_with_an_ellipsis():
    cleaned = clean_description("x" * 3000)
    assert len(cleaned) == 2001
    assert cleaned.endswith("…")


def test_cap_counts_utf16_units_and_never_splits_a_character():
    cleaned = clean_description("😀" * 1500)  # 2 UTF-16 units each
    assert cleaned == "😀" * 1000 + "…"


def test_collapses_runs_of_blank_lines():
    assert clean_description("a\n\n\n\n\nb") == "a\n\nb"


def test_passes_the_location_through_and_nulls_empty_ones():
    assert api("2026-03-23", "weekly-mon@fixture")["location"] == "Zasedačka A"
    assert api("2026-03-18", "weekly-ovr@fixture")["location"] == "Zasedačka C"
    assert api("2026-03-16", "allday-1@fixture")["location"] is None
