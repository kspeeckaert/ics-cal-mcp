# Changelog

This file records the changes in each release. Versions use the format `major.minor.YYYYMMDD`.

## [1.1.20261005]

### Added

- Several feeds in one server. Each `ICS_URL_<NAME>` variable defines a calendar called `<name>`. `ICS_URL` still works and defines the calendar `default`.
- `ICS_PROFILE_<NAME>` sets the profile of one calendar. `ICS_PROFILE` sets the profile of all calendars that have no own value.
- The tool `list_calendars`. It lists the calendars with their profile and masked host, and it does not download any feed.
- An optional `calendars` input on `get_events`, `get_events_range` and `feed_info`, to select calendars by name.
- Feeds are downloaded in parallel. Each feed has its own cache.

### Changed

- Each event has a `calendar` field.
- `get_events` and `get_events_range` merge the events of all requested calendars, sorted by start time. They have an `errors` list. If some calendars fail, the result holds the events of the other calendars, and `errors` names the failed calendars. If all requested calendars fail, the result has `isError: true`.
- `feed_info` returns a `calendars` list with one entry per calendar, and an `errors` list.
- The configuration check reports bad calendar names, duplicate calendars, and `ICS_PROFILE_<NAME>` values that do not match a feed.

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
