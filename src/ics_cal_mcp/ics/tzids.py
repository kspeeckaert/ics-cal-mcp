"""Line unfolding and Windows/Exchange to IANA TZID rewriting.

Both passes run on the raw text BEFORE parsing. After the rewrite every TZID
in the feed is a valid IANA zone, so parsing never depends on the host time
zone.
"""

from __future__ import annotations

import re
import zoneinfo
from dataclasses import dataclass, field
from functools import lru_cache

from .windows_zones import WINDOWS_TO_IANA


@lru_cache(maxsize=1)
def _zone_names() -> dict[str, str]:
    """Lower-case name -> canonical spelling, for all known IANA zones."""
    names = set(zoneinfo.available_timezones()) | {"UTC"}
    return {name.lower(): name for name in names}


@lru_cache(maxsize=512)
def valid_iana(name: str) -> zoneinfo.ZoneInfo | None:
    """Return the zone for an IANA name (case-insensitive), else None.

    Only names from the tz database are accepted, so the lookup is the same
    on case-sensitive (Linux) and case-insensitive (macOS) file systems.
    """
    if not name:
        return None
    canonical = _zone_names().get(name.lower())
    if canonical is None:
        return None
    try:
        return zoneinfo.ZoneInfo(canonical)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return None


_UNFOLD_RE = re.compile(r"\r\n[ \t]|\n[ \t]")


def unfold_ics(raw: str) -> str:
    """RFC 5545 section 3.1 unfolding: a line break plus space or tab joins lines."""
    return _UNFOLD_RE.sub("", raw)


def value_separator_index(line: str) -> int | None:
    """Index of the ':' between name+parameters and value (quote-aware)."""
    in_quotes = False
    for i, ch in enumerate(line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == ":" and not in_quotes:
            return i
    return None


def split_quote_aware(s: str, delim: str) -> list[str]:
    parts: list[str] = []
    start = 0
    in_quotes = False
    for i, ch in enumerate(s):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == delim and not in_quotes:
            parts.append(s[start:i])
            start = i + 1
    parts.append(s[start:])
    return parts


# `;TZID=name` or `;TZID="name"` in the parameter section of a content line.
# The leading ';' anchors to a parameter named exactly TZID.
_TZID_PARAM_RE = re.compile(r';TZID=(?:"([^"]+)"|([^";:]+))', re.IGNORECASE)
_LINE_SPLIT_RE = re.compile(r"(\r\n|\n|\r)")


@dataclass
class NormalizedIcs:
    ics: str
    # Distinct TZID names that were neither IANA nor a known Windows name.
    unknown: list[str] = field(default_factory=list)


class _Resolver:
    def __init__(self, tz_default: str) -> None:
        self.mapping: dict[str, str] = {}
        self.unknown: list[str] = []
        self.tz_default = tz_default

    def resolve(self, name: str) -> str:
        if name in self.mapping:
            return self.mapping[name]
        if valid_iana(name) is not None:
            target = name
        else:
            mapped = WINDOWS_TO_IANA.get(name)
            if mapped is not None and valid_iana(mapped) is not None:
                target = mapped
            else:
                self.unknown.append(name)
                target = self.tz_default
        self.mapping[name] = target
        return target

    def rewrite_line(self, line: str) -> str:
        sep = value_separator_index(line)
        if sep is None:
            return line
        head = line[:sep]
        value = line[sep + 1 :]
        head = _TZID_PARAM_RE.sub(
            lambda m: f";TZID={self.resolve(m.group(1) or m.group(2) or '')}", head
        )
        # VTIMEZONE `TZID:<name>`: the property name must be exactly TZID.
        if head.split(";", 1)[0].upper() == "TZID" and value:
            value = self.resolve(value)
        return f"{head}:{value}"


def normalize_tzids(unfolded: str, tz_default: str) -> NormalizedIcs:
    """Rewrite Windows TZIDs to IANA, and unknown TZIDs to `tz_default`.

    Only structural positions change: the TZID= parameter and the VTIMEZONE
    TZID property. Free text in property values is never touched. The pass is
    idempotent and keeps the original line terminators.
    """
    resolver = _Resolver(tz_default)
    pieces = _LINE_SPLIT_RE.split(unfolded)
    # Even indexes are lines, odd indexes are the terminators between them.
    for i in range(0, len(pieces), 2):
        pieces[i] = resolver.rewrite_line(pieces[i])
    return NormalizedIcs(ics="".join(pieces), unknown=resolver.unknown)
