"""Shape expanded instances into the tool output format.

Timed instants use the offset of TZ_DEFAULT. All-day events stay date-only.
"""

from __future__ import annotations

import re
from typing import Any
from zoneinfo import ZoneInfo

from ..profile import EXCHANGE, FeedProfile
from .expand import AllDay, ExpandedInstance
from .model import ParsedEvent
from .timeutil import from_ms

BUSY_STATUSES = ("BUSY", "TENTATIVE", "FREE", "OOF")
DESCRIPTION_MAX_UTF16_UNITS = 2000

# Everything from the first Teams separator line (a run of underscores) to
# the end is dial-in/join boilerplate.
_BOILERPLATE_START = re.compile(r"_{8,}")
_BLANK_RUNS = re.compile(r"\n{3,}")
_MEETING_URL_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"""https://teams\.microsoft\.com/l/meetup-join/[^\s<>"']+""",
        r"""https://teams\.live\.com/meet/[^\s<>"']+""",
        r"""https://[\w.-]*zoom\.us/(?:j|my|s|w)/[^\s<>"']+""",
        r"""https://meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}[^\s<>"']*""",
    )
]


def format_instant(ms: int, tz: ZoneInfo) -> str:
    """`2026-03-16T09:00:00+01:00`: offset shown, seconds precision."""
    return from_ms(ms).astimezone(tz).isoformat(timespec="seconds")


def busy_status(ev: ParsedEvent, profile: FeedProfile = EXCHANGE) -> str:
    """Exchange: X-MICROSOFT-CDO-BUSYSTATUS. Generic: TRANSP:TRANSPARENT is
    FREE, STATUS:TENTATIVE is TENTATIVE. The default is BUSY."""
    if profile.microsoft_props and ev.busy_status_raw is not None:
        upper = ev.busy_status_raw.upper()
        if upper in BUSY_STATUSES:
            return upper
    if profile.standard_busy_status:
        if ev.transp == "TRANSPARENT":
            return "FREE"
        if ev.status == "TENTATIVE":
            return "TENTATIVE"
    return "BUSY"


def extract_meeting_url(ev: ParsedEvent, profile: FeedProfile = EXCHANGE) -> str | None:
    """First Teams/Zoom/Meet link. Search order: the Exchange
    X-MICROSOFT-SKYPETEAMSMEETINGURL property (Exchange profile only), then
    the RAW description (the link usually sits inside the boilerplate that
    `clean_description` removes), then the location."""
    teams_prop = ev.teams_url_prop if profile.microsoft_props else None
    for source in (teams_prop, ev.description, ev.location):
        if not source:
            continue
        for pattern in _MEETING_URL_PATTERNS:
            m = pattern.search(source)
            if m:
                return m.group(0).rstrip(">).,;")
    return None


def clean_description(raw: str | None, strip_teams_boilerplate: bool = True) -> str | None:
    """Remove Teams/dial-in boilerplate (when asked), collapse blank-line
    runs, and cap the text at about 2000 UTF-16 code units without splitting
    a character."""
    if raw is None:
        return None
    text = raw.replace("\r\n", "\n")
    m = _BOILERPLATE_START.search(text) if strip_teams_boilerplate else None
    if m:
        text = text[: m.start()]
    text = _BLANK_RUNS.sub("\n\n", text).strip()
    if not text:
        return None
    units = 0
    for i, ch in enumerate(text):
        width = 2 if ord(ch) > 0xFFFF else 1
        if units + width > DESCRIPTION_MAX_UTF16_UNITS:
            return text[:i].rstrip() + "…"
        units += width
    return text


def to_api_event(
    inst: ExpandedInstance, tz_default: ZoneInfo, profile: FeedProfile = EXCHANGE
) -> dict[str, Any]:
    ev = inst.event
    location = ev.location.strip() if ev.location else None
    t = inst.time
    if isinstance(t, AllDay):
        start, end = t.start.isoformat(), t.end.isoformat()
    else:
        start = format_instant(t.start_ms, tz_default)
        end = format_instant(t.end_ms, tz_default)
    # Key order is fixed: the JSON text mirrors structuredContent exactly.
    return {
        "all_day": inst.is_all_day,
        "busy_status": busy_status(ev, profile),
        "description": clean_description(ev.description, profile.strip_teams_boilerplate),
        "end": end,
        "is_recurring": inst.is_recurring,
        "location": location or None,
        "meeting_url": extract_meeting_url(ev, profile),
        "start": start,
        "summary": ev.summary or "",
    }
