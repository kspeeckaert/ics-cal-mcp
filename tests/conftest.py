"""Shared fixtures and helpers.

The fixture feed (tests/fixtures/feed.ics) is an anonymized Exchange-style
feed around March 2026 (DST change on 2026-03-29). It comes from
hromadkom/calendar-ics-mcp (MIT). Treat it as frozen. Its expectations are
written for Europe/Prague.
"""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from ics_cal_mcp.config import Config, FeedConfig
from ics_cal_mcp.ics.expand import ExpandedInstance, Timed, day_window, expand_events
from ics_cal_mcp.ics.model import ParsedCalendar
from ics_cal_mcp.ics.parse import parse_calendar
from ics_cal_mcp.ics.timeutil import to_ms
from ics_cal_mcp.ics.tzids import normalize_tzids, unfold_ics

FIXTURES = Path(__file__).parent / "fixtures"
TZ_NAME = "Europe/Prague"
TZ = ZoneInfo(TZ_NAME)


def read_fixture(name: str) -> str:
    # newline="" keeps the CRLF line endings byte-for-byte.
    with open(FIXTURES / name, encoding="utf-8", newline="") as f:
        return f.read()


FIXTURE = read_fixture("feed.ics")
FIXTURE_TRUNCATED = read_fixture("feed-truncated.ics")

# The path segment doubles as the "secret" that must never leak into output.
TEST_ICS_URL = "https://feedhost.example.com/owa/calendar/SECRETPATH123/reachcalendar.ics"


def make_config(feeds: dict[str, str] | None = None, **overrides: object) -> Config:
    """Config with the given feeds (name -> URL). Default: one feed called
    'default' with TEST_ICS_URL."""
    feeds = feeds or {"default": TEST_ICS_URL}
    values: dict[str, object] = {
        "feeds": tuple(FeedConfig(name=n, url=u) for n, u in feeds.items()),
        "tz_default": TZ,
        "cache_ttl_seconds": 300,
        "fetch_timeout_ms": 15_000,
    }
    values.update(overrides)
    return Config(**values)  # type: ignore[arg-type]


@lru_cache(maxsize=1)
def parsed() -> ParsedCalendar:
    normalized = normalize_tzids(unfold_ics(FIXTURE), TZ_NAME)
    return parse_calendar(normalized.ics, TZ)


def instances_on(date: str) -> list[ExpandedInstance]:
    return expand_events(parsed(), day_window(date, TZ, 0))


def uids_on(date: str) -> list[str]:
    return [inst.event.uid for inst in instances_on(date)]


def instance_of(date: str, uid: str) -> ExpandedInstance | None:
    return next((i for i in instances_on(date) if i.event.uid == uid), None)


def utc_ms(iso: str) -> int:
    return to_ms(datetime.fromisoformat(iso.replace("Z", "+00:00")))


def start_ms(inst: ExpandedInstance) -> int:
    assert isinstance(inst.time, Timed), "expected a timed instance"
    return inst.time.start_ms
