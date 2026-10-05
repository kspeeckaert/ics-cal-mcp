"""Entry point: load the configuration (exit 1 on failure), log the start
line, then serve MCP over stdio until the client disconnects (exit 0)."""

from __future__ import annotations

import os
import sys

import anyio

from . import SERVER_NAME, __version__, log
from .config import load_config
from .errors import ConfigError, mask_ics_url
from .feed import FeedClient, Httpx2Transport
from .server import ServerState, build_mcp_server


async def _serve(state: ServerState) -> None:
    from mcp.server.stdio import stdio_server

    server = build_mcp_server(state)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-V"):
        sys.stderr.write(f"{SERVER_NAME} {__version__}\n")
        return
    try:
        cfg = load_config(os.environ.get)
    except ConfigError as err:
        log.error("Startup failed", detail=err.message)
        sys.exit(1)

    feed = FeedClient(cfg, Httpx2Transport(cfg.fetch_timeout_ms))
    state = ServerState(cfg, feed)
    log.info(
        f"{SERVER_NAME} started",
        version=__version__,
        cacheTtlSeconds=cfg.cache_ttl_seconds,
        fetchTimeoutMs=cfg.fetch_timeout_ms,
        source=mask_ics_url(cfg.ics_url),
        tz=cfg.tz_name,
        profile=cfg.profile.name,
    )
    try:
        anyio.run(_serve, state)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
