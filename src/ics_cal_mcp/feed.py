"""Feed download with a single-slot TTL cache.

The cache fails hard: an expired snapshot is never served stale. Fetch
errors are reduced to a short, URL-free description before any formatting,
because third-party error messages can contain the secret feed URL.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import httpx2

from . import __version__
from .config import Config
from .errors import FeedFetchError, FeedTruncatedError, mask_ics_url


@dataclass(frozen=True)
class FeedSnapshot:
    raw: str
    num_bytes: int  # UTF-8 byte length of `raw`
    ends_valid: bool  # payload ends with END:VCALENDAR
    fetched_at_ms: int


@dataclass(frozen=True)
class HttpOk:
    status: int
    body: str


class HttpErr(Exception):
    """URL-free transport failure. `kind` is 'timeout', 'io' or 'other'."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(kind)
        self.kind = kind
        self.detail = detail


class Transport(Protocol):
    def get(self, url: str) -> HttpOk: ...


Clock = Callable[[], int]


def system_clock() -> int:
    return time.time_ns() // 1_000_000


class Httpx2Transport:
    """Production transport. `httpx2` validates TLS against the operating
    system trust store, which works with uv-managed Python on macOS."""

    def __init__(self, timeout_ms: int) -> None:
        self._client = httpx2.Client(
            timeout=httpx2.Timeout(timeout_ms / 1000),
            follow_redirects=True,
            headers={
                "user-agent": f"ics-cal-mcp/{__version__}",
                "accept": "text/calendar, */*",
            },
        )

    def get(self, url: str) -> HttpOk:
        try:
            res = self._client.get(url)
            body = res.content.decode("utf-8", errors="replace")
        except httpx2.TimeoutException:
            raise HttpErr("timeout") from None
        except httpx2.TransportError as e:
            # Class name only. str(e) can contain the URL.
            raise HttpErr("io", type(e).__name__) from None
        except Exception:  # noqa: BLE001 - never format a third-party error
            raise HttpErr("other") from None
        return HttpOk(status=res.status_code, body=body)


_END_RE = re.compile(r"END:VCALENDAR\Z", re.IGNORECASE | re.ASCII)


def ends_valid(raw: str) -> bool:
    """True when the payload ends with END:VCALENDAR (trailing space allowed)."""
    return _END_RE.search(raw.rstrip()) is not None


class FeedClient:
    def __init__(self, cfg: Config, transport: Transport, clock: Clock = system_clock) -> None:
        self.cfg = cfg
        self._transport = transport
        self._clock = clock
        self._slot: FeedSnapshot | None = None

    def now_ms(self) -> int:
        return self._clock()

    def get_snapshot(self) -> FeedSnapshot:
        """Cached snapshot. Fetches when missing, expired, or truncated.

        A truncated snapshot is stored, so `feed_info` can report it, but it
        never counts as fresh: the next call retries at once.
        """
        ttl_ms = self.cfg.cache_ttl_seconds * 1000
        now = self._clock()
        slot = self._slot
        fresh = slot is not None and slot.ends_valid and now - slot.fetched_at_ms < ttl_ms
        if not fresh:
            # On failure the exception propagates and the old slot stays
            # unusable (it can never be fresh again).
            self._slot = self._fetch()
        assert self._slot is not None
        return self._slot

    def get_validated_raw(self) -> str:
        """Full raw feed, guaranteed complete."""
        snapshot = self.get_snapshot()
        if not snapshot.ends_valid:
            raise FeedTruncatedError(snapshot.num_bytes)
        return snapshot.raw

    def _fetch(self) -> FeedSnapshot:
        masked = mask_ics_url(self.cfg.ics_url)
        try:
            res = self._transport.get(self.cfg.ics_url)
        except HttpErr as err:
            raise FeedFetchError(masked, self._failure_detail(err)) from None
        if not 200 <= res.status < 300:
            raise FeedFetchError(masked, f"HTTP {res.status}")
        raw = res.body
        return FeedSnapshot(
            raw=raw,
            num_bytes=len(raw.encode("utf-8", errors="surrogatepass")),
            ends_valid=ends_valid(raw),
            fetched_at_ms=self._clock(),
        )

    def _failure_detail(self, err: HttpErr) -> str:
        if err.kind == "timeout":
            return f"timeout after {self.cfg.fetch_timeout_ms}ms"
        if err.kind == "io" and err.detail:
            return err.detail
        return "network error"
