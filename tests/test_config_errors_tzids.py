"""Configuration, secret hygiene, unfolding and TZID normalization."""

from zoneinfo import ZoneInfo

import pytest

from ics_cal_mcp.config import DEFAULT_TZ, load_config
from ics_cal_mcp.errors import (
    ConfigError,
    FeedFetchError,
    FeedTruncatedError,
    mask_ics_url,
    sanitize_text,
)
from ics_cal_mcp.ics.tzids import normalize_tzids, unfold_ics, valid_iana

from conftest import TZ_NAME

ICS_URL = "https://outlook.office365.com/owa/calendar/abc123secret/reachcalendar.ics"


# --- config ---------------------------------------------------------------


def test_applies_defaults_when_only_ics_url_is_set():
    cfg = load_config({"ICS_URL": ICS_URL})
    assert cfg.ics_url == ICS_URL
    assert cfg.tz_name == DEFAULT_TZ == "Europe/Brussels"
    assert cfg.cache_ttl_seconds == 300
    assert cfg.fetch_timeout_ms == 15_000


def test_accepts_explicit_overrides():
    cfg = load_config(
        {
            "CACHE_TTL_SECONDS": "60",
            "FETCH_TIMEOUT_MS": "5000",
            "ICS_URL": ICS_URL,
            "TZ_DEFAULT": "Europe/Berlin",
        }
    )
    assert cfg.cache_ttl_seconds == 60
    assert cfg.fetch_timeout_ms == 5000
    assert cfg.tz_default == ZoneInfo("Europe/Berlin")


def test_fails_without_ics_url():
    with pytest.raises(ConfigError, match="ICS_URL is required") as exc:
        load_config({})
    assert exc.value.code == "CONFIG"
    with pytest.raises(ConfigError, match="ICS_URL"):
        load_config({"ICS_URL": ""})


def test_rejects_non_http_ics_url_without_echoing_its_value():
    with pytest.raises(ConfigError) as exc:
        load_config({"ICS_URL": "ftp://secret-host.example/private-path"})
    msg = exc.value.message
    assert "ICS_URL must be an http(s) URL" in msg
    assert "secret-host" not in msg
    assert "private-path" not in msg


def test_rejects_an_invalid_tz_default():
    with pytest.raises(ConfigError, match="TZ_DEFAULT"):
        load_config({"ICS_URL": ICS_URL, "TZ_DEFAULT": "Mars/Olympus_Mons"})


def test_rejects_a_non_numeric_cache_ttl():
    with pytest.raises(ConfigError, match="CACHE_TTL_SECONDS"):
        load_config({"CACHE_TTL_SECONDS": "soon", "ICS_URL": ICS_URL})


def test_rejects_a_negative_or_zero_fetch_timeout():
    with pytest.raises(ConfigError, match="FETCH_TIMEOUT_MS"):
        load_config({"FETCH_TIMEOUT_MS": "-1", "ICS_URL": ICS_URL})
    with pytest.raises(ConfigError, match="FETCH_TIMEOUT_MS"):
        load_config({"FETCH_TIMEOUT_MS": "0", "ICS_URL": ICS_URL})


def test_reports_all_issues_together():
    with pytest.raises(ConfigError) as exc:
        load_config({"CACHE_TTL_SECONDS": "x", "TZ_DEFAULT": "nope"})
    msg = exc.value.message
    assert "CACHE_TTL_SECONDS" in msg and "ICS_URL" in msg and "TZ_DEFAULT" in msg


def test_blank_values_fall_back_to_defaults():
    cfg = load_config({"CACHE_TTL_SECONDS": "  ", "ICS_URL": ICS_URL, "TZ_DEFAULT": ""})
    assert cfg.cache_ttl_seconds == 300
    assert cfg.tz_name == DEFAULT_TZ


# --- errors ---------------------------------------------------------------


def test_mask_keeps_scheme_and_hostname_only():
    assert mask_ics_url(ICS_URL) == "https://outlook.office365.com/…"


def test_mask_drops_userinfo_port_path_and_query():
    assert (
        mask_ics_url("https://user:pass@host.example:8443/secret/path?token=abc")
        == "https://host.example/…"
    )


def test_mask_tolerates_garbage():
    assert mask_ics_url("not a url") == "<invalid url>"


def test_sanitize_replaces_the_full_url():
    assert (
        sanitize_text(f"fetch failed for {ICS_URL} (500)", ICS_URL)
        == "fetch failed for https://outlook.office365.com/… (500)"
    )


def test_sanitize_replaces_bare_path_and_query():
    text = "GET /owa/calendar/abc123secret/reachcalendar.ics returned 404"
    assert sanitize_text(text, ICS_URL) == "GET /… returned 404"


def test_sanitize_scrubs_credentials():
    url = "https://user:hunter2@host.example/cal.ics"
    assert sanitize_text("auth hunter2 rejected", url) == "auth … rejected"


def test_error_message_formats():
    assert (
        FeedFetchError("https://host.example/…", "HTTP 404").message
        == "Failed to fetch the ICS feed from https://host.example/…: HTTP 404"
    )
    assert FeedTruncatedError(1234).message == (
        "ICS feed appears truncated (1234 bytes received, does not end with END:VCALENDAR); "
        "refusing to serve partial calendar data"
    )


# --- tzids ----------------------------------------------------------------


def test_unfold_joins_crlf_space_and_lf_tab_continuations():
    assert unfold_ics("SUMMARY:Hello wo\r\n rld") == "SUMMARY:Hello world"
    assert unfold_ics("SUMMARY:Hello wo\n\trld") == "SUMMARY:Hello world"
    assert unfold_ics("A:1\r\nB:2") == "A:1\r\nB:2"


def test_valid_iana_accepts_names_case_insensitively_and_rejects_garbage():
    assert valid_iana("Europe/Prague") == ZoneInfo("Europe/Prague")
    assert valid_iana("UTC") is not None
    assert valid_iana("europe/prague") == ZoneInfo("Europe/Prague")
    assert valid_iana("W. Europe Standard Time") is None
    assert valid_iana("Mars/Olympus_Mons") is None
    assert valid_iana("") is None
    assert valid_iana("../../etc/passwd") is None


def test_rewrites_the_three_exchange_windows_names_wherever_tzid_appears():
    source = "\r\n".join(
        [
            "BEGIN:VTIMEZONE",
            "TZID:W. Europe Standard Time",
            "END:VTIMEZONE",
            "DTSTART;TZID=W. Europe Standard Time:20260302T090000",
            "DTSTART;TZID=Central Europe Standard Time:20260302T090000",
            "EXDATE;TZID=Central European Standard Time:20260309T090000,20260316T090000",
        ]
    )
    result = normalize_tzids(source, TZ_NAME)
    assert result.unknown == []
    assert "TZID:Europe/Berlin" in result.ics
    assert "TZID=Europe/Berlin:20260302T090000" in result.ics
    assert "TZID=Europe/Budapest:20260302T090000" in result.ics
    assert "TZID=Europe/Warsaw:20260309T090000,20260316T090000" in result.ics
    assert "Standard Time" not in result.ics


def test_rewrites_quoted_tzid_parameters():
    result = normalize_tzids('DTSTART;TZID="W. Europe Standard Time":20260302T090000', TZ_NAME)
    assert result.ics == "DTSTART;TZID=Europe/Berlin:20260302T090000"


def test_leaves_valid_iana_tzids_untouched():
    source = "DTSTART;TZID=Europe/Prague:20260302T090000"
    assert normalize_tzids(source, TZ_NAME).ics == source


def test_rewrites_unknown_tzids_to_the_default_zone_and_reports_them():
    result = normalize_tzids("DTSTART;TZID=Foo Standard Time:20260319T090000", TZ_NAME)
    assert result.ics == "DTSTART;TZID=Europe/Prague:20260319T090000"
    assert result.unknown == ["Foo Standard Time"]


def test_never_touches_tzid_lookalike_text_inside_property_values():
    description = (
        "DESCRIPTION:We must fix the bug where TZID=Pacific Standard Time entries are "
        "dropped\\, see ticket CAL-42 for details about TZID: parsing"
    )
    result = normalize_tzids(description, TZ_NAME)
    assert result.ics == description
    assert result.unknown == []


def test_never_touches_properties_whose_name_merely_ends_in_tzid():
    result = normalize_tzids("X-MICROSOFT-CDO-TZID:57", TZ_NAME)
    assert result.ics == "X-MICROSOFT-CDO-TZID:57"
    assert result.unknown == []


def test_rewrites_only_the_parameter_section_not_a_url_value_containing_colons():
    source = (
        "DTSTART;TZID=W. Europe Standard Time:20260302T090000\r\n"
        "URL:https://example.com/a;TZID=fake"
    )
    assert normalize_tzids(source, TZ_NAME).ics == (
        "DTSTART;TZID=Europe/Berlin:20260302T090000\r\nURL:https://example.com/a;TZID=fake"
    )


def test_normalize_is_idempotent_and_keeps_line_endings():
    source = "DTSTART;TZID=W. Europe Standard Time:20260302T090000\nA:1\rB:2\r\n"
    once = normalize_tzids(source, TZ_NAME).ics
    assert normalize_tzids(once, TZ_NAME).ics == once
    assert once.endswith("\nA:1\rB:2\r\n")
