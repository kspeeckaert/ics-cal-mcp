"""Error types and secret hygiene.

The feed URL works as a bearer credential. Only the scheme and the host name
may appear in logs, error messages, or tool output.
"""

from __future__ import annotations

from dataclasses import dataclass


class AppError(Exception):
    """Domain error. `message` is safe to show to the MCP client."""

    code = "APP"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ConfigError(AppError):
    code = "CONFIG"

    def __init__(self, details: str) -> None:
        super().__init__(f"Invalid configuration — {details}")


class FeedFetchError(AppError):
    code = "FEED_FETCH"

    def __init__(self, masked: str, detail: str) -> None:
        super().__init__(f"Failed to fetch the ICS feed from {masked}: {detail}")


class FeedTruncatedError(AppError):
    code = "FEED_TRUNCATED"

    def __init__(self, num_bytes: int) -> None:
        super().__init__(
            f"ICS feed appears truncated ({num_bytes} bytes received, does not end with "
            "END:VCALENDAR); refusing to serve partial calendar data"
        )


class InvalidRangeError(AppError):
    code = "INVALID_RANGE"


@dataclass(frozen=True)
class UrlParts:
    scheme: str
    username: str
    password: str
    host: str
    # Path and query, fragment removed. Empty when the URL has neither.
    path_and_query: str


def parse_url(url: str) -> UrlParts | None:
    """Minimal URL splitter for the masking and sanitizing paths."""
    scheme, sep, rest = url.partition("://")
    if not sep or not scheme:
        return None
    if not all(c.isascii() and (c.isalnum() or c in "+-.") for c in scheme):
        return None
    auth_end = len(rest)
    for i, ch in enumerate(rest):
        if ch in "/?#":
            auth_end = i
            break
    authority = rest[:auth_end]
    after = rest[auth_end:]
    # User info ends at the LAST '@' of the authority (RFC 3986).
    at = authority.rfind("@")
    if at >= 0:
        userinfo, hostport = authority[:at], authority[at + 1 :]
    else:
        userinfo, hostport = "", authority
    username, _, password = userinfo.partition(":")
    if hostport.startswith("["):
        close = hostport.find("]")
        if close < 0:
            return None
        host = hostport[: close + 1]
    else:
        host = hostport.split(":", 1)[0]
    if not host:
        return None
    return UrlParts(
        scheme=scheme.lower(),
        username=username,
        password=password,
        host=host.lower(),
        path_and_query=after.split("#", 1)[0],
    )


def mask_ics_url(url: str) -> str:
    """Keep only the scheme and the host name."""
    parts = parse_url(url)
    if parts is None:
        return "<invalid url>"
    return f"{parts.scheme}://{parts.host}/…"


def sanitize_text(text: str, ics_url: str) -> str:
    """Remove the feed URL, its path, and its credentials from free text."""
    if not ics_url:
        return text
    out = text.replace(ics_url, mask_ics_url(ics_url))
    parts = parse_url(ics_url)
    if parts is not None:
        if len(parts.path_and_query) > 1:
            out = out.replace(parts.path_and_query, "/…")
        if parts.username:
            out = out.replace(parts.username, "…")
        if parts.password:
            out = out.replace(parts.password, "…")
    return out
