"""Configuration, read and validated from the environment once at startup.

Nothing else in the package reads the environment. Error messages never
echo URL values back, because feed URLs are secrets. Feed names come from
variable names, so they are safe to show.

Feeds:
- `ICS_URL_<NAME>=<url>` defines a feed called `<name>` (lower case).
- `ICS_URL=<url>` defines a feed called `default` (backward compatible).
- `ICS_PROFILE_<NAME>` sets the profile of one feed. `ICS_PROFILE` sets the
  profile for all feeds that do not have their own setting.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from .errors import ConfigError, parse_url
from .ics.tzids import valid_iana
from .profile import PROFILE_NAMES, FeedProfile, select_profile

DEFAULT_TZ = "Europe/Brussels"
DEFAULT_CACHE_TTL_SECONDS = 300
DEFAULT_FETCH_TIMEOUT_MS = 15_000
DEFAULT_FEED_NAME = "default"

URL_PREFIX = "ICS_URL_"
PROFILE_PREFIX = "ICS_PROFILE_"

_UINT_RE = re.compile(r"\+?[0-9]+")
# Feed name: starts with a letter, then letters, digits or '_'.
_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


@dataclass(frozen=True)
class FeedConfig:
    name: str
    url: str
    # auto | exchange | generic
    profile_override: str = "auto"

    @property
    def profile(self) -> FeedProfile:
        return select_profile(self.url, self.profile_override)


@dataclass(frozen=True)
class Config:
    feeds: tuple[FeedConfig, ...]
    tz_default: ZoneInfo
    cache_ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS
    fetch_timeout_ms: int = DEFAULT_FETCH_TIMEOUT_MS

    @property
    def tz_name(self) -> str:
        return self.tz_default.key

    @property
    def feed_names(self) -> list[str]:
        return [f.name for f in self.feeds]

    @property
    def urls(self) -> list[str]:
        return [f.url for f in self.feeds]


def _profile_value(raw: str | None, var_name: str, issues: list[str]) -> str | None:
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in PROFILE_NAMES:
        return value
    issues.append(f"{var_name} must be one of: " + ", ".join(PROFILE_NAMES))
    return None


def load_config(env: Mapping[str, str]) -> Config:
    """Parse the configuration. A value that trims to empty counts as unset.
    All problems are collected and reported together."""

    def var(key: str) -> str | None:
        value = env.get(key)
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

    tz: ZoneInfo | None = ZoneInfo(DEFAULT_TZ)
    raw = var("TZ_DEFAULT")
    if raw is not None:
        tz = valid_iana(raw.strip())
        if tz is None:
            issues.append("TZ_DEFAULT must be a valid IANA timezone")

    global_profile = _profile_value(var("ICS_PROFILE"), "ICS_PROFILE", issues) or "auto"

    # Collect feeds: (name, variable name, url).
    found: list[tuple[str, str, str]] = []
    plain = var("ICS_URL")
    if plain is not None:
        found.append((DEFAULT_FEED_NAME, "ICS_URL", plain))
    for key in sorted(env):
        if not key.upper().startswith(URL_PREFIX) or var(key) is None:
            continue
        suffix = key[len(URL_PREFIX) :]
        if not _NAME_RE.fullmatch(suffix):
            issues.append(
                f"{key}: the feed name after {URL_PREFIX} must start with a letter and "
                "contain only letters, digits and _"
            )
            continue
        found.append((suffix.lower(), key, var(key) or ""))

    feeds: list[FeedConfig] = []
    seen: dict[str, str] = {}
    for name, key, url in found:
        if name in seen:
            issues.append(f"{key} and {seen[name]} both define the feed '{name}'")
            continue
        seen[name] = key
        parts = parse_url(url)
        if parts is None or parts.scheme not in ("http", "https"):
            issues.append(f"{key} must be an http(s) URL")
            continue
        own = _profile_value(
            var(PROFILE_PREFIX + name.upper()), PROFILE_PREFIX + name.upper(), issues
        )
        feeds.append(FeedConfig(name=name, url=url, profile_override=own or global_profile))

    if not found:
        issues.append("ICS_URL or at least one ICS_URL_<NAME> is required")

    # A profile setting for a feed that does not exist is almost always a typo.
    for key in sorted(env):
        if key.upper().startswith(PROFILE_PREFIX) and var(key) is not None:
            name = key[len(PROFILE_PREFIX) :].lower()
            if name not in seen:
                issues.append(f"{key} does not match any feed")

    if issues:
        raise ConfigError("; ".join(issues))
    assert tz is not None
    # 'default' first, then the named feeds in alphabetical order.
    feeds.sort(key=lambda f: (f.name != DEFAULT_FEED_NAME, f.name))
    return Config(
        feeds=tuple(feeds),
        tz_default=tz,
        cache_ttl_seconds=cache_ttl,
        fetch_timeout_ms=fetch_timeout,
    )
