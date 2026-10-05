"""Feed cache, integrity check, and URL-free error mapping."""

import pytest

from ics_cal_mcp.errors import FeedFetchError, FeedTruncatedError
from ics_cal_mcp.feed import FeedClient, HttpErr, HttpOk, ends_valid

from conftest import FIXTURE, FIXTURE_TRUNCATED, TEST_ICS_URL, make_config


class StubTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, url):
        self.calls += 1
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r


class ManualClock:
    def __init__(self, ms=1_000_000):
        self.ms = ms

    def __call__(self):
        return self.ms


def test_serves_from_cache_within_the_ttl():
    t, clock = StubTransport(HttpOk(200, FIXTURE)), ManualClock()
    feed = FeedClient(make_config(), TEST_ICS_URL, t, clock)
    feed.get_snapshot()
    clock.ms += 299_999
    feed.get_snapshot()
    assert t.calls == 1


def test_refetches_after_the_ttl_expires():
    t, clock = StubTransport(HttpOk(200, FIXTURE)), ManualClock()
    feed = FeedClient(make_config(), TEST_ICS_URL, t, clock)
    feed.get_snapshot()
    clock.ms += 300_000
    feed.get_snapshot()
    assert t.calls == 2


def test_does_not_serve_an_expired_snapshot_when_the_refetch_fails():
    t = StubTransport(HttpOk(200, FIXTURE), HttpOk(500, ""))
    clock = ManualClock()
    feed = FeedClient(make_config(), TEST_ICS_URL, t, clock)
    feed.get_snapshot()
    clock.ms += 301_000
    with pytest.raises(FeedFetchError, match="HTTP 500"):
        feed.get_snapshot()


def test_a_truncated_snapshot_is_reported_but_never_fresh():
    t = StubTransport(HttpOk(200, FIXTURE_TRUNCATED))
    feed = FeedClient(make_config(), TEST_ICS_URL, t, ManualClock())
    assert feed.get_snapshot().ends_valid is False
    with pytest.raises(FeedTruncatedError):
        feed.get_validated_raw()
    assert t.calls == 2  # retried at once, not pinned for a TTL


def test_maps_http_errors_without_leaking_the_url_path():
    feed = FeedClient(make_config(), TEST_ICS_URL, StubTransport(HttpOk(404, "")), ManualClock())
    with pytest.raises(FeedFetchError) as exc:
        feed.get_snapshot()
    assert exc.value.message == (
        "Failed to fetch the ICS feed from https://feedhost.example.com/…: HTTP 404"
    )
    assert "SECRETPATH123" not in exc.value.message


def test_maps_timeouts_to_a_readable_message():
    feed = FeedClient(make_config(), TEST_ICS_URL, StubTransport(HttpErr("timeout")), ManualClock())
    with pytest.raises(FeedFetchError, match="timeout after 15000ms"):
        feed.get_snapshot()


def test_maps_network_errors_to_the_error_class_never_a_raw_message():
    feed = FeedClient(
        make_config(), TEST_ICS_URL, StubTransport(HttpErr("io", "ConnectError")), ManualClock()
    )
    with pytest.raises(FeedFetchError, match="ConnectError$"):
        feed.get_snapshot()


def test_counts_utf8_bytes():
    feed = FeedClient(
        make_config(), TEST_ICS_URL, StubTransport(HttpOk(200, FIXTURE)), ManualClock()
    )
    assert feed.get_snapshot().num_bytes == len(FIXTURE.encode("utf-8"))


def test_ends_valid_tolerates_trailing_whitespace_and_case():
    assert ends_valid("BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n\r\n")
    assert ends_valid("end:vcalendar")
    assert not ends_valid("BEGIN:VCALENDAR\r\nBEGIN:VEVENT")
