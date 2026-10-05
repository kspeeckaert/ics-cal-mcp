"""JSON-lines logger on stderr.

On the stdio transport, stdout carries JSON-RPC only. Every log line goes to
stderr (MCP clients such as Claude Desktop show stderr in their MCP logs).
Logging is best-effort: a closed stderr pipe must never break a tool call.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _write(level: str, msg: str, ctx: dict[str, Any]) -> None:
    record: dict[str, Any] = {"level": level, "msg": msg, "time": _now_iso()}
    record.update(ctx)
    try:
        sys.stderr.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 - best effort by design
        pass


def info(msg: str, **ctx: Any) -> None:
    _write("info", msg, ctx)


def warn(msg: str, **ctx: Any) -> None:
    _write("warn", msg, ctx)


def error(msg: str, **ctx: Any) -> None:
    _write("error", msg, ctx)
