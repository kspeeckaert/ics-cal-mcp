"""Feed profiles: which feed-specific rules apply to a feed.

The `exchange` profile matches the behavior of hromadkom/calendar-ics-mcp
for Outlook/Exchange published feeds. The `generic` profile is for other
sources (Google, iCloud, Fastmail, Nextcloud, and so on): it drops the
Exchange-only rules and honors standard properties that Exchange does not
emit.

The profile is chosen from the feed URL, unless ICS_PROFILE overrides it.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import parse_url

# Host names that serve Outlook/Exchange published calendars.
EXCHANGE_HOSTS = frozenset(
    {
        "outlook.office365.com",
        "outlook.office.com",
        "outlook.live.com",
    }
)
# Published Exchange calendars (cloud and on-premises OWA) use this path.
EXCHANGE_PATH_MARKER = "/owa/calendar/"

PROFILE_NAMES = ("auto", "exchange", "generic")


@dataclass(frozen=True)
class FeedProfile:
    name: str
    # Read X-MICROSOFT-CDO-BUSYSTATUS for busy_status and
    # X-MICROSOFT-SKYPETEAMSMEETINGURL for meeting_url.
    microsoft_props: bool
    # Cut the description at the first Teams separator (a run of underscores).
    strip_teams_boilerplate: bool
    # Derive busy_status from TRANSP and STATUS (standard properties).
    standard_busy_status: bool
    # Leave out events and occurrences with STATUS:CANCELLED.
    skip_cancelled: bool
    # Add RDATE occurrences to recurring and non-recurring events.
    rdate: bool
    # Floating times use X-WR-TIMEZONE, else TZ_DEFAULT (instead of UTC).
    floating_in_local_zone: bool


EXCHANGE = FeedProfile(
    name="exchange",
    microsoft_props=True,
    strip_teams_boilerplate=True,
    standard_busy_status=False,
    skip_cancelled=False,
    rdate=False,
    floating_in_local_zone=False,
)

GENERIC = FeedProfile(
    name="generic",
    microsoft_props=False,
    strip_teams_boilerplate=False,
    standard_busy_status=True,
    skip_cancelled=True,
    rdate=True,
    floating_in_local_zone=True,
)


def is_exchange_url(url: str) -> bool:
    parts = parse_url(url)
    if parts is None:
        return False
    if parts.host in EXCHANGE_HOSTS:
        return True
    return EXCHANGE_PATH_MARKER in parts.path_and_query.lower()


def select_profile(url: str, override: str = "auto") -> FeedProfile:
    """`override` is one of PROFILE_NAMES. `auto` detects from the URL."""
    if override == "exchange":
        return EXCHANGE
    if override == "generic":
        return GENERIC
    return EXCHANGE if is_exchange_url(url) else GENERIC
