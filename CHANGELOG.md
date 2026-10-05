# Changelog

This file records the changes in each release. Versions use the format `major.minor.YYYYMMDD`.

## [1.0.20261005]

### Added

- First release. This is a Python reimplementation of [hromadkom/calendar-ics-mcp](https://github.com/hromadkom/calendar-ics-mcp) 0.3.1, for use with `uvx` and without Docker.
- The tools `get_events`, `get_events_range` and `feed_info`, with the same schemas and output as the original. `feed_info` also reports the `profile` in use.
- Feed profiles, selected from the feed URL or with `ICS_PROFILE`. The `exchange` profile keeps the behavior of the original. The `generic` profile supports `STATUS:CANCELLED`, `TRANSP`, `RDATE` and `X-WR-TIMEZONE`.
- The MCP stdio transport, built on the official MCP Python SDK 2.x.

### Changed

- The default for `TZ_DEFAULT` is `Europe/Brussels`. The original uses `Europe/Prague`.

### Removed

- The Streamable HTTP transport, `HTTP_BIND`, `HTTP_BEARER_TOKEN` and the `--health` probe. Local MCP clients use stdio.
