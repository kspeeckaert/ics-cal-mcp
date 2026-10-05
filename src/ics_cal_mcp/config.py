"""Configuration, read and validated from the environment once at startup.

Nothing else in the package reads the environment. Error messages never
echo values back, because ICS_URL is a secret.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from .errors import ConfigError, parse_url
from .ics.tzids import valid_iana
from .profile import PROFILE_NAMES, FeedProfile, select_profile

DEFAULT_TZ = "Europe/Brussels"
DEFAULT_CACHE_TTL_SECONDS = 300
DEFAULT_FETCH_TIMEOUT_MS = 15_000

_UINT_RE = re.compile(r"\+?[0-9]+")


@dataclass(frozen=True)
class Config:
    ics_url: str
    tz_default: ZoneInfo
    cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS
    fetch_timeout_ms: int = DEFAULT_FETCH_TIMEOUT_MS
    # auto | exchange | generic
    profile_override: str = "auto"

    @property
    def tz_name(self) -> str:
        return self.tz_default.key

    @property
    def profile(self) -> FeedProfile:
        return select_profile(self.ics_url, self.profile_override)


def load_config(get: Callable[[str], str | None] | Mapping[str, str]) -> Config:
    """Parse the configuration. A value that trims to empty counts as unset.
    All problems are collected and reported together."""
    getter = get.get if isinstance(get, Mapping) else get

    def var(key: str) -> str | None:
        value = getter(key)
        if value is None or not value.strip():
            return None
        return value

    issues: list[str] = []

    cache_ttl = DEFAULT_CACHE_TTL_SECONDS
    raw = var("CACHE_TTL_SECONDS")
    if raw is not None:
        if _UINT_RE.fullmatch(raw.strip()):
            cache_ttl = int(raw.strip())
        else:
            issues.append("CACHE_TTL_SECONDS must be a non-negative integer")

    fetch_timeout = DEFAULT_FETCH_TIMEOUT_MS
    raw = var("FETCH_TIMEOUT_MS")
    if raw is not None:
        if _UINT_RE.fullmatch(raw.strip()) and int(raw.strip()) >= 1:
            fetch_timeout = int(raw.strip())
        else:
            issues.append("FETCH_TIMEOUT_MS must be a positive integer")

    ics_url = var("ICS_URL")
    if ics_url is None:
        issues.append("ICS_URL is required")
    else:
        parts = parse_url(ics_url)
        if parts is None or parts.scheme not in ("http", "https"):
            issues.append("ICS_URL must be an http(s) URL")

    tz: ZoneInfo | None = ZoneInfo(DEFAULT_TZ)
    raw = var("TZ_DEFAULT")
    if raw is not None:
        tz = valid_iana(raw.strip())
        if tz is None:
            issues.append("TZ_DEFAULT must be a valid IANA timezone")

    profile_override = "auto"
    raw = var("ICS_PROFILE")
    if raw is not None:
        if raw.strip().lower() in PROFILE_NAMES:
            profile_override = raw.strip().lower()
        else:
            issues.append("ICS_PROFILE must be one of: " + ", ".join(PROFILE_NAMES))

    if issues:
        raise ConfigError("; ".join(issues))
    assert ics_url is not None and tz is not None
    return Config(
        ics_url=ics_url,
        tz_default=tz,
        cache_ttl_seconds=cache_ttl,
        fetch_timeout_ms=fetch_timeout,
        profile_override=profile_override,
    )
