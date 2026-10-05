"""Tool registry and handlers: schemas, argument checks, result shaping.

`ServerState` is synchronous and free of MCP SDK types, so tests can call it
directly. `build_mcp_server` wraps it in an MCP SDK low-level server.

Several feeds ("calendars") can be configured. The event tools merge the
events of all requested calendars and label each event with its calendar.
If some calendars fail, the result holds the events of the others plus an
`errors` list. The result is an error only when every requested calendar
fails.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, TypeVar

import anyio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.shared.exceptions import MCPError

from . import SERVER_NAME, __version__, log
from .config import Config, FeedConfig
from .errors import AppError, mask_ics_url, sanitize_all
from .feed import FeedClient
from .ics.expand import EventWindow, day_window, expand_events, range_window, sort_key
from .ics.format import BUSY_STATUSES, to_api_event
from .ics.model import ParsedCalendar
from .ics.parse import ParseError, parse_calendar
from .ics.timeutil import from_ms, instant_ms
from .ics.tzids import normalize_tzids, unfold_ics
from .profile import FeedProfile

INVALID_PARAMS = -32602
INTERNAL_ERROR_TEXT = (
    "Internal error while processing the calendar feed; details are on stderr (MCP client log)."
)
MAX_PARALLEL_FETCHES = 8
_DATE_SHAPE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

T = TypeVar("T")


class UnknownToolError(Exception):
    pass


class _InvalidArgs(Exception):
    pass


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def success(structured: dict[str, Any]) -> dict[str, Any]:
    """`content[0].text` is the exact JSON serialization of structuredContent."""
    return {
        "content": [{"type": "text", "text": _json_text(structured)}],
        "structuredContent": structured,
    }


def error_result(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _iso_utc_millis(ms: int) -> str:
    dt = from_ms(ms)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _count_vevents(raw: str) -> int:
    """Lines that begin with BEGIN:VEVENT. Works on truncated feeds too."""
    return sum(1 for line in raw.splitlines() if line.startswith("BEGIN:VEVENT"))


@dataclass
class _Feed:
    cfg: FeedConfig
    client: FeedClient
    profile: FeedProfile
    warned_tzids: set[str] = field(default_factory=set)

    @property
    def name(self) -> str:
        return self.cfg.name


class ServerState:
    def __init__(self, cfg: Config, clients: Mapping[str, FeedClient]) -> None:
        self.cfg = cfg
        self.feeds: dict[str, _Feed] = {
            f.name: _Feed(cfg=f, client=clients[f.name], profile=f.profile) for f in cfg.feeds
        }
        self.tools = build_tools(cfg.tz_name, cfg.feed_names)

    # --- helpers -----------------------------------------------------------

    def _now_ms(self) -> int:
        return next(iter(self.feeds.values())).client.now_ms()

    def _sanitize(self, text: str) -> str:
        return sanitize_all(text, self.cfg.urls)

    def _parsed(self, feed: _Feed, raw: str) -> ParsedCalendar:
        normalized = normalize_tzids(unfold_ics(raw), self.cfg.tz_name)
        for tzid in normalized.unknown:
            if tzid not in feed.warned_tzids:
                feed.warned_tzids.add(tzid)
                log.warn(
                    "Unknown TZID in feed; interpreting it as TZ_DEFAULT",
                    calendar=feed.name,
                    fallback=self.cfg.tz_name,
                    tzid=tzid,
                )
        return parse_calendar(
            normalized.ics,
            self.cfg.tz_default,
            floating_in_local_zone=feed.profile.floating_in_local_zone,
        )

    def _error_message(self, feed: _Feed, err: Exception) -> str:
        """Client-safe message for one failed calendar."""
        if isinstance(err, AppError):
            return self._sanitize(err.message)
        log.error(
            "Tool failed with an unexpected error",
            calendar=feed.name,
            detail=self._sanitize(f"{type(err).__name__}: {err}"),
        )
        return INTERNAL_ERROR_TEXT

    def _run_per_feed(
        self, names: list[str], work: Callable[[_Feed], T]
    ) -> tuple[dict[str, T], list[dict[str, str]]]:
        """Run `work` for each calendar (in parallel when there are several).
        Returns the results by name and the errors, both in `names` order."""
        feeds = [self.feeds[n] for n in names]

        def guarded(feed: _Feed) -> tuple[bool, Any]:
            try:
                return True, work(feed)
            except Exception as err:  # noqa: BLE001 - reported per calendar
                return False, self._error_message(feed, err)

        if len(feeds) == 1:
            outcomes = [guarded(feeds[0])]
        else:
            with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_FETCHES, len(feeds))) as pool:
                outcomes = list(pool.map(guarded, feeds))

        results: dict[str, T] = {}
        errors: list[dict[str, str]] = []
        for feed, (ok, value) in zip(feeds, outcomes, strict=True):
            if ok:
                results[feed.name] = value
            else:
                errors.append({"calendar": feed.name, "message": value})
        return results, errors

    def _all_failed(self, errors: list[dict[str, str]]) -> dict[str, Any]:
        if len(errors) == 1:
            return error_result(errors[0]["message"])
        details = "; ".join(f"{e['calendar']}: {e['message']}" for e in errors)
        return error_result(f"All requested calendars failed. {details}")

    def _calendars_arg(self, args: dict[str, Any]) -> list[str]:
        """Requested calendar names, in configuration order. Default: all."""
        value = args.get("calendars")
        known = list(self.feeds)
        if value is None:
            return known
        message = "calendars must be a non-empty list of calendar names: " + ", ".join(known)
        if not isinstance(value, list) or not value:
            raise _InvalidArgs(message)
        unknown = [v for v in value if not isinstance(v, str) or v not in self.feeds]
        if unknown:
            raise _InvalidArgs(message)
        wanted = set(value)
        return [n for n in known if n in wanted]

    def _events(self, names: list[str], win: EventWindow) -> dict[str, Any]:
        """Merged, sorted events and per-calendar errors. When every requested
        calendar failed, returns {"failed": <error result>} instead."""

        def work(feed: _Feed) -> list[tuple[tuple[int, str], dict[str, Any]]]:
            cal = self._parsed(feed, feed.client.get_validated_raw())
            out = []
            for inst in expand_events(cal, win, feed.profile):
                api = to_api_event(inst, self.cfg.tz_default, feed.profile)
                out.append((sort_key(win, inst), _with_calendar(api, feed.name)))
            return out

        results, errors = self._run_per_feed(names, work)
        if not results:
            return {"failed": self._all_failed(errors)}
        order = {n: i for i, n in enumerate(names)}
        merged = [item for rows in results.values() for item in rows]
        # Start, then summary, then calendar order: deterministic output.
        merged.sort(key=lambda row: (row[0], order[row[1]["calendar"]]))
        return {"events": [row[1] for row in merged], "errors": errors}

    # --- tools -------------------------------------------------------------

    def call_tool(self, name: str, args: dict[str, Any] | None) -> dict[str, Any]:
        args = args or {}
        handlers: dict[str, Callable[[], dict[str, Any]]] = {
            "feed_info": lambda: self.feed_info(args),
            "get_events": lambda: self.get_events(args),
            "get_events_range": lambda: self.get_events_range(args),
            "list_calendars": lambda: self.list_calendars(),
        }
        handler = handlers.get(name)
        if handler is None:
            raise UnknownToolError(f"Unknown tool: {name}")
        try:
            return handler()
        except _InvalidArgs as err:
            return _invalid_args(name, str(err))
        except AppError as err:
            return error_result(self._sanitize(err.message))
        except Exception as err:  # noqa: BLE001 - never crash across the MCP boundary
            log.error(
                "Tool failed with an unexpected error",
                detail=self._sanitize(f"{type(err).__name__}: {err}"),
            )
            return error_result(INTERNAL_ERROR_TEXT)

    def list_calendars(self) -> dict[str, Any]:
        return success(
            {
                "calendars": [
                    {
                        "name": f.name,
                        "profile": f.profile.name,
                        "source_host": mask_ics_url(f.cfg.url),
                    }
                    for f in self.feeds.values()
                ],
                "timezone": self.cfg.tz_name,
            }
        )

    def get_events(self, args: dict[str, Any]) -> dict[str, Any]:
        date = _date_arg(args, "date", required=False)
        names = self._calendars_arg(args)
        win = day_window(date, self.cfg.tz_default, self._now_ms())
        merged = self._events(names, win)
        if "failed" in merged:
            return merged["failed"]
        return success(
            {
                "date": win.start_date.isoformat(),
                "errors": merged["errors"],
                "events": merged["events"],
                "timezone": self.cfg.tz_name,
            }
        )

    def get_events_range(self, args: dict[str, Any]) -> dict[str, Any]:
        start = _date_arg(args, "from", required=True)
        end = _date_arg(args, "to", required=True)
        names = self._calendars_arg(args)
        assert start is not None and end is not None
        win = range_window(start, end, self.cfg.tz_default)
        merged = self._events(names, win)
        if "failed" in merged:
            return merged["failed"]
        return success(
            {
                "errors": merged["errors"],
                "events": merged["events"],
                "from": start,
                "timezone": self.cfg.tz_name,
                "to": end,
            }
        )

    def feed_info(self, args: dict[str, Any]) -> dict[str, Any]:
        names = self._calendars_arg(args)

        def work(feed: _Feed) -> dict[str, Any]:
            snapshot = feed.client.get_snapshot()
            cache_age_seconds = round((feed.client.now_ms() - snapshot.fetched_at_ms) / 1000)
            # DTSTART range over masters AND overrides. An unparseable (for
            # example truncated) feed gives a null range: that IS the diagnosis.
            dtstart_min: str | None = None
            dtstart_max: str | None = None
            try:
                cal: ParsedCalendar | None = self._parsed(feed, snapshot.raw)
            except ParseError:
                cal = None
            if cal is not None:
                starts = [
                    instant_ms(ev.dtstart)
                    for g in cal.groups
                    for ev in ([g.master] if g.master else []) + g.overrides
                ]
                if starts:
                    dtstart_min = _iso_utc_millis(min(starts))
                    dtstart_max = _iso_utc_millis(max(starts))
            return {
                "cache_age_seconds": cache_age_seconds,
                "calendar": feed.name,
                "dtstart_max": dtstart_max,
                "dtstart_min": dtstart_min,
                "ends_with_end_vcalendar": snapshot.ends_valid,
                "feed_bytes": snapshot.num_bytes,
                "last_fetch_at": _iso_utc_millis(snapshot.fetched_at_ms),
                "profile": feed.profile.name,
                "source_host": mask_ics_url(feed.cfg.url),
                "vevent_count": _count_vevents(snapshot.raw),
            }

        results, errors = self._run_per_feed(names, work)
        if not results:
            return self._all_failed(errors)
        return success({"calendars": [results[n] for n in names if n in results], "errors": errors})


def _with_calendar(api: dict[str, Any], calendar: str) -> dict[str, Any]:
    """Insert `calendar` in alphabetical key position (after busy_status)."""
    out: dict[str, Any] = {}
    for key, value in api.items():
        out[key] = value
        if key == "busy_status":
            out["calendar"] = calendar
    return out


def _date_arg(args: dict[str, Any], name: str, *, required: bool) -> str | None:
    """Check the YYYY-MM-DD shape only. Real calendar dates are checked by
    the window builders."""
    message = f"{name} expected a date in YYYY-MM-DD format"
    if name not in args or args[name] is None:
        if required:
            raise _InvalidArgs(message)
        return None
    value = args[name]
    if isinstance(value, str) and _DATE_SHAPE.fullmatch(value):
        return value
    raise _InvalidArgs(message)


def _invalid_args(tool: str, detail: str) -> dict[str, Any]:
    return error_result(f"Invalid arguments for tool {tool}: {detail}")


def build_tools(tz: str, calendars: list[str]) -> list[dict[str, Any]]:
    annotations = {
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
        "readOnlyHint": True,
    }
    nullable_string = {"type": ["string", "null"]}
    calendar_names = ", ".join(calendars)
    calendars_input = {
        "type": "array",
        "items": {"type": "string", "enum": list(calendars)},
        "minItems": 1,
        "uniqueItems": True,
        "description": f"Calendars to include. Defaults to all: {calendar_names}.",
    }
    errors_array = {
        "type": "array",
        "description": "Calendars that failed. Results from the other calendars are complete.",
        "items": {
            "type": "object",
            "properties": {"calendar": {"type": "string"}, "message": {"type": "string"}},
            "required": ["calendar", "message"],
            "additionalProperties": False,
        },
    }
    api_event_schema = {
        "type": "object",
        "properties": {
            "all_day": {"type": "boolean"},
            "busy_status": {"type": "string", "enum": list(BUSY_STATUSES)},
            "calendar": {"type": "string"},
            "description": nullable_string,
            "end": {"type": "string"},
            "is_recurring": {"type": "boolean"},
            "location": nullable_string,
            "meeting_url": nullable_string,
            "start": {"type": "string"},
            "summary": {"type": "string"},
        },
        "required": [
            "all_day",
            "busy_status",
            "calendar",
            "description",
            "end",
            "is_recurring",
            "location",
            "meeting_url",
            "start",
            "summary",
        ],
        "additionalProperties": False,
    }
    events_array = {"type": "array", "items": api_event_schema}
    date_pattern = r"^\d{4}-\d{2}-\d{2}$"
    feed_info_item = {
        "type": "object",
        "properties": {
            "cache_age_seconds": {"type": "number"},
            "calendar": {"type": "string"},
            "dtstart_max": nullable_string,
            "dtstart_min": nullable_string,
            "ends_with_end_vcalendar": {"type": "boolean"},
            "feed_bytes": {"type": "number"},
            "last_fetch_at": {"type": "string"},
            "profile": {"type": "string", "enum": ["exchange", "generic"]},
            "source_host": {"type": "string"},
            "vevent_count": {"type": "number"},
        },
        "required": [
            "cache_age_seconds",
            "calendar",
            "dtstart_max",
            "dtstart_min",
            "ends_with_end_vcalendar",
            "feed_bytes",
            "last_fetch_at",
            "profile",
            "source_host",
            "vevent_count",
        ],
        "additionalProperties": False,
    }
    return [
        {
            "name": "feed_info",
            "title": "Feed diagnostics",
            "description": (
                "Diagnostics per calendar feed: size in bytes, VEVENT count, DTSTART range, "
                "last fetch time, cache age, the feed profile in use (exchange or generic), and "
                "whether the payload ended with END:VCALENDAR (i.e. arrived complete). "
                f"Calendars: {calendar_names}."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"calendars": calendars_input},
                "additionalProperties": False,
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "calendars": {"type": "array", "items": feed_info_item},
                    "errors": errors_array,
                },
                "required": ["calendars", "errors"],
                "additionalProperties": False,
            },
            "annotations": annotations,
        },
        {
            "name": "get_events",
            "title": "Get events for a day",
            "description": (
                "List calendar events that overlap the given local day, merged across "
                "calendars and sorted by start time. Each event names its calendar. "
                f"Times are ISO 8601 with the {tz} offset; all-day events use date-only strings "
                f"with an exclusive end date. Calendars: {calendar_names}."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "pattern": date_pattern,
                        "description": f"Day to list (YYYY-MM-DD). Defaults to today in {tz}.",
                    },
                    "calendars": calendars_input,
                },
                "additionalProperties": False,
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "errors": errors_array,
                    "events": events_array,
                    "timezone": {"type": "string"},
                },
                "required": ["date", "errors", "events", "timezone"],
                "additionalProperties": False,
            },
            "annotations": annotations,
        },
        {
            "name": "get_events_range",
            "title": "Get events for a date range",
            "description": (
                "List calendar events overlapping an inclusive date range (max 31 days), merged "
                "across calendars and sorted by start time. Same output format as get_events. "
                f"Calendars: {calendar_names}."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "from": {
                        "type": "string",
                        "pattern": date_pattern,
                        "description": "First day of the range (YYYY-MM-DD, inclusive).",
                    },
                    "to": {
                        "type": "string",
                        "pattern": date_pattern,
                        "description": "Last day of the range (YYYY-MM-DD, inclusive).",
                    },
                    "calendars": calendars_input,
                },
                "required": ["from", "to"],
                "additionalProperties": False,
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "errors": errors_array,
                    "events": events_array,
                    "from": {"type": "string"},
                    "timezone": {"type": "string"},
                    "to": {"type": "string"},
                },
                "required": ["errors", "events", "from", "timezone", "to"],
                "additionalProperties": False,
            },
            "annotations": annotations,
        },
        {
            "name": "list_calendars",
            "title": "List calendars",
            "description": (
                "List the configured calendars with their feed profile and masked source host. "
                "Does not download any feed."
            ),
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "outputSchema": {
                "type": "object",
                "properties": {
                    "calendars": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "profile": {"type": "string", "enum": ["exchange", "generic"]},
                                "source_host": {"type": "string"},
                            },
                            "required": ["name", "profile", "source_host"],
                            "additionalProperties": False,
                        },
                    },
                    "timezone": {"type": "string"},
                },
                "required": ["calendars", "timezone"],
                "additionalProperties": False,
            },
            "annotations": {**annotations, "openWorldHint": False},
        },
    ]


def to_call_tool_result(result: dict[str, Any]) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=c["text"]) for c in result["content"]],
        structured_content=result.get("structuredContent"),
        is_error=bool(result.get("isError", False)),
    )


def build_mcp_server(state: ServerState) -> Server[Any]:
    tools = [types.Tool.model_validate(t) for t in state.tools]
    # Tool calls are serialized: each feed cache sees one call at a time and
    # needs no locking of its own. Feeds are fetched in parallel inside a call.
    lock = anyio.Lock()

    async def on_list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tools)

    async def on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        async with lock:
            try:
                result = await anyio.to_thread.run_sync(
                    state.call_tool, params.name, params.arguments
                )
            except UnknownToolError as err:
                raise MCPError(code=INVALID_PARAMS, message=str(err)) from None
        return to_call_tool_result(result)

    return Server(
        SERVER_NAME,
        version=__version__,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )
