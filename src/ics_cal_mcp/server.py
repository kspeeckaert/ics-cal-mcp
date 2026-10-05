"""Tool registry and handlers: schemas, argument checks, result shaping.

`ServerState` is synchronous and free of MCP SDK types, so tests can call it
directly. `build_mcp_server` wraps it in an MCP SDK low-level server.
"""

from __future__ import annotations

import json
import re
from typing import Any

import anyio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.shared.exceptions import MCPError

from . import SERVER_NAME, __version__, log
from .config import Config
from .errors import AppError, mask_ics_url, sanitize_text
from .feed import FeedClient
from .ics.expand import EventWindow, day_window, expand_events, range_window
from .ics.format import BUSY_STATUSES, to_api_event
from .ics.model import ParsedCalendar
from .ics.parse import ParseError, parse_calendar
from .ics.rrule_slots import SlotError
from .ics.timeutil import from_ms, instant_ms
from .ics.tzids import normalize_tzids, unfold_ics

INVALID_PARAMS = -32602
INTERNAL_ERROR_TEXT = (
    "Internal error while processing the calendar feed; details are on stderr (MCP client log)."
)
_DATE_SHAPE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class UnknownToolError(Exception):
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


class ServerState:
    def __init__(self, cfg: Config, feed: FeedClient) -> None:
        self.cfg = cfg
        self.feed = feed
        self.profile = cfg.profile
        self.tools = build_tools(cfg.tz_name)
        self._warned_tzids: set[str] = set()

    # --- helpers -----------------------------------------------------------

    def _normalize_and_warn(self, unfolded: str) -> str:
        normalized = normalize_tzids(unfolded, self.cfg.tz_name)
        for name in normalized.unknown:
            if name not in self._warned_tzids:
                self._warned_tzids.add(name)
                log.warn(
                    "Unknown TZID in feed; interpreting it as TZ_DEFAULT",
                    fallback=self.cfg.tz_name,
                    tzid=name,
                )
        return normalized.ics

    def _failure(self, err: AppError) -> dict[str, Any]:
        return error_result(sanitize_text(err.message, self.cfg.ics_url))

    def _internal(self, detail: str) -> dict[str, Any]:
        log.error(
            "Tool failed with an unexpected error",
            detail=sanitize_text(detail, self.cfg.ics_url),
        )
        return error_result(INTERNAL_ERROR_TEXT)

    def _parsed(self, raw: str) -> ParsedCalendar:
        ics = self._normalize_and_warn(unfold_ics(raw))
        return parse_calendar(
            ics,
            self.cfg.tz_default,
            floating_in_local_zone=self.profile.floating_in_local_zone,
        )

    def _events_for_window(self, win: EventWindow) -> list[dict[str, Any]]:
        raw = self.feed.get_validated_raw()
        cal = self._parsed(raw)
        instances = expand_events(cal, win, self.profile)
        return [to_api_event(inst, self.cfg.tz_default, self.profile) for inst in instances]

    # --- tools -------------------------------------------------------------

    def call_tool(self, name: str, args: dict[str, Any] | None) -> dict[str, Any]:
        args = args or {}
        handlers = {
            "feed_info": lambda: self.feed_info(),
            "get_events": lambda: self.get_events(args),
            "get_events_range": lambda: self.get_events_range(args),
        }
        handler = handlers.get(name)
        if handler is None:
            raise UnknownToolError(f"Unknown tool: {name}")
        try:
            return handler()
        except AppError as err:
            return self._failure(err)
        except ParseError as err:
            return self._internal(f"ParseError: {err}")
        except SlotError as err:
            return self._internal(f"SlotError: {err}")
        except Exception as err:  # noqa: BLE001 - never crash across the MCP boundary
            return self._internal(f"{type(err).__name__}: {err}")

    def get_events(self, args: dict[str, Any]) -> dict[str, Any]:
        ok, date = _date_arg(args, "date", required=False)
        if not ok:
            return _invalid_args("get_events", date or "")
        win = day_window(date, self.cfg.tz_default, self.feed.now_ms())
        events = self._events_for_window(win)
        return success(
            {
                "date": win.start_date.isoformat(),
                "events": events,
                "timezone": self.cfg.tz_name,
            }
        )

    def get_events_range(self, args: dict[str, Any]) -> dict[str, Any]:
        ok_from, start = _date_arg(args, "from", required=True)
        if not ok_from:
            return _invalid_args("get_events_range", start or "")
        ok_to, end = _date_arg(args, "to", required=True)
        if not ok_to:
            return _invalid_args("get_events_range", end or "")
        assert start is not None and end is not None
        win = range_window(start, end, self.cfg.tz_default)
        events = self._events_for_window(win)
        return success(
            {
                "events": events,
                "from": start,
                "timezone": self.cfg.tz_name,
                "to": end,
            }
        )

    def feed_info(self) -> dict[str, Any]:
        snapshot = self.feed.get_snapshot()
        cache_age_seconds = round((self.feed.now_ms() - snapshot.fetched_at_ms) / 1000)
        # DTSTART range over masters AND overrides. An unparseable (for
        # example truncated) feed gives a null range: that IS the diagnosis.
        dtstart_min: str | None = None
        dtstart_max: str | None = None
        try:
            cal = self._parsed(snapshot.raw)
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
        return success(
            {
                "cache_age_seconds": cache_age_seconds,
                "dtstart_max": dtstart_max,
                "dtstart_min": dtstart_min,
                "ends_with_end_vcalendar": snapshot.ends_valid,
                "feed_bytes": snapshot.num_bytes,
                "last_fetch_at": _iso_utc_millis(snapshot.fetched_at_ms),
                "profile": self.profile.name,
                "source_host": mask_ics_url(self.cfg.ics_url),
                "vevent_count": _count_vevents(snapshot.raw),
            }
        )


def _date_arg(args: dict[str, Any], field: str, *, required: bool) -> tuple[bool, str | None]:
    """Check the YYYY-MM-DD shape only. Real calendar dates are checked by
    the window builders. Returns (ok, value) or (False, error detail)."""
    message = f"{field} expected a date in YYYY-MM-DD format"
    if field not in args or args[field] is None:
        return (False, message) if required else (True, None)
    value = args[field]
    if isinstance(value, str) and _DATE_SHAPE.fullmatch(value):
        return True, value
    return False, message


def _invalid_args(tool: str, detail: str) -> dict[str, Any]:
    return error_result(f"Invalid arguments for tool {tool}: {detail}")


def build_tools(tz: str) -> list[dict[str, Any]]:
    annotations = {
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
        "readOnlyHint": True,
    }
    nullable_string = {"type": ["string", "null"]}
    api_event_schema = {
        "type": "object",
        "properties": {
            "all_day": {"type": "boolean"},
            "busy_status": {"type": "string", "enum": list(BUSY_STATUSES)},
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
    return [
        {
            "name": "feed_info",
            "title": "Feed diagnostics",
            "description": (
                "Diagnostics for the ICS feed download: size in bytes, VEVENT count, DTSTART "
                "range, last fetch time, cache age, the feed profile in use (exchange or "
                "generic), and whether the payload ended with END:VCALENDAR (i.e. arrived "
                "complete)"
            ),
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "outputSchema": {
                "type": "object",
                "properties": {
                    "cache_age_seconds": {"type": "number"},
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
            },
            "annotations": annotations,
        },
        {
            "name": "get_events",
            "title": "Get events for a day",
            "description": (
                "List calendar events that overlap the given local day, sorted by start time. "
                f"Times are ISO 8601 with the {tz} offset; all-day events use date-only strings "
                "with an exclusive end date."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "pattern": date_pattern,
                        "description": f"Day to list (YYYY-MM-DD). Defaults to today in {tz}.",
                    }
                },
                "additionalProperties": False,
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "timezone": {"type": "string"},
                    "events": events_array,
                },
                "required": ["date", "timezone", "events"],
                "additionalProperties": False,
            },
            "annotations": annotations,
        },
        {
            "name": "get_events_range",
            "title": "Get events for a date range",
            "description": (
                "List calendar events overlapping an inclusive date range (max 31 days), sorted "
                "by start time. Same output format as get_events."
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
                },
                "required": ["from", "to"],
                "additionalProperties": False,
            },
            "outputSchema": {
                "type": "object",
                "properties": {
                    "from": {"type": "string"},
                    "timezone": {"type": "string"},
                    "to": {"type": "string"},
                    "events": events_array,
                },
                "required": ["from", "timezone", "to", "events"],
                "additionalProperties": False,
            },
            "annotations": annotations,
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
    # Tool calls are serialized: one fetch at a time, and the cache needs no
    # locking of its own.
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
