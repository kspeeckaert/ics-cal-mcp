# ics-cal-mcp

A read-only MCP server that turns a published ICS calendar feed into three JSON tools for an AI assistant. MCP (Model Context Protocol) is the standard that AI clients such as Claude Desktop use to call external tools.

You run it with `uvx`. You do not need Docker. The only tool to install is [uv](https://docs.astral.sh/uv/).

This is a Python reimplementation of [hromadkom/calendar-ics-mcp](https://github.com/hromadkom/calendar-ics-mcp), which is written in Rust and runs in Docker. It keeps the same tools, the same output format, and the same test fixtures. It adds feed profiles, so that feeds that do not come from Exchange also give correct results.

## What it does

The server downloads the full feed and checks that it is complete. It expands recurring events in the correct time zone, across daylight saving time changes. It returns small, ready-to-use JSON.

A generic web-fetch tool is not enough for a calendar feed, for two reasons:

- Fetch tools cut off large responses. A calendar feed is large, so events at the end are lost without a warning. This server checks that the feed ends with `END:VCALENDAR`.
- An ICS file is not a list of events. A weekly meeting is one `VEVENT` with an `RRULE` (recurrence rule). A cancelled occurrence is an `EXDATE` (excluded date). A moved occurrence is a separate component with a `RECURRENCE-ID`. The server applies all of these rules.

## Tools

| Tool | Input | Output |
| --- | --- | --- |
| `get_events` | `{ "date": "YYYY-MM-DD" }`, optional. The default is today in `TZ_DEFAULT`. | Events that overlap that local day, sorted by start time. |
| `get_events_range` | `{ "from": "YYYY-MM-DD", "to": "YYYY-MM-DD" }`, inclusive, 31 days at most. | The same format, for a date range. |
| `feed_info` | None. | Feed diagnostics: size, event count, date range, cache age, the profile in use, and whether the feed is complete. |

Each tool returns its result in `structuredContent` and as the same JSON text in `content[0].text`. If a tool fails (the feed is unreachable, the feed is truncated, or an argument is wrong), the result has `isError: true`. The server does not send a protocol error for these cases.

Example output of `get_events`:

```json
{
  "date": "2026-07-14",
  "events": [
    {
      "all_day": false,
      "busy_status": "TENTATIVE",
      "description": "Agenda without the Teams dial-in text",
      "end": "2026-07-14T14:45:00+02:00",
      "is_recurring": true,
      "location": "Microsoft Teams Meeting",
      "meeting_url": "https://teams.microsoft.com/l/meetup-join/…",
      "start": "2026-07-14T14:15:00+02:00",
      "summary": "Design review"
    }
  ],
  "timezone": "Europe/Brussels"
}
```

Timed events use ISO 8601 with the `TZ_DEFAULT` offset. All-day events use date-only strings, and the end date is exclusive. A day query returns every event that overlaps the local day. For example, a meeting that runs past midnight shows on both days.

## Feed profiles

The server selects a profile from the feed URL. The profile decides which feed-specific rules apply.

- If the host is `outlook.office365.com`, `outlook.office.com` or `outlook.live.com`, the `exchange` profile applies. If the URL path contains `/owa/calendar/`, the `exchange` profile also applies. On-premises Exchange servers publish calendars under that path. This profile gives the same results as the Rust original.
- The `generic` profile applies to all other feeds, for example Google Calendar, iCloud, Fastmail or Nextcloud.

To force a profile, set `ICS_PROFILE` to `exchange` or `generic`.

| Rule | `exchange` | `generic` |
| --- | --- | --- |
| `busy_status` source | `X-MICROSOFT-CDO-BUSYSTATUS` (`BUSY`, `TENTATIVE`, `FREE`, `OOF`) | `TRANSP:TRANSPARENT` gives `FREE`. `STATUS:TENTATIVE` gives `TENTATIVE`. |
| `meeting_url` sources | `X-MICROSOFT-SKYPETEAMSMEETINGURL`, then the description, then the location | The description, then the location |
| Teams dial-in text in the description | Removed, from the first line of `_` characters to the end | Kept |
| `STATUS:CANCELLED` | Ignored, as Exchange does not send it | The event or occurrence is left out |
| `RDATE` (extra occurrence dates) | Ignored, as Exchange does not send it | Added to the occurrences |
| Floating times (no `Z` and no `TZID`) | Read as UTC | Read in the calendar's `X-WR-TIMEZONE`, else in `TZ_DEFAULT` |
| Windows time zone names, for example `W. Europe Standard Time` | Mapped to IANA names | Mapped to IANA names |
| `RRULE`, `EXDATE`, `RECURRENCE-ID`, `DURATION`, `SEQUENCE` | Supported | Supported |

The Windows time zone mapping applies in both profiles. A Windows name can only mean one zone, and Outlook exports that are hosted elsewhere also use these names.

These items are not supported in either profile: `RANGE=THISANDFUTURE`, `RDATE` values of type `PERIOD`, and `EXRULE`.

## Get your ICS URL

For Outlook on the web, do these steps:

1. Open **Settings**, then **Calendar**, then **Shared calendars**.
2. Under **Publish a calendar**, select the calendar and select **Can view all details**. Lower permissions remove the fields that the tools return.
3. Select **Publish**, and copy the **ICS** link (not the HTML link).

For Google Calendar, open the calendar settings and copy the **Secret address in iCal format**.

> [!WARNING]
> Keep the URL secret, like a password. Anyone who has the URL can read the whole calendar. The URL does not expire. Never commit it to Git, never log it, and never paste it into an issue.

## Configure your MCP client

You need uv on your computer. On macOS, you can install it with `brew install uv`. When the client starts the server for the first time, uv downloads the package and a matching Python version. After that, uv uses its cache.

For Claude Desktop, add this to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "calendar": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/kspeeckaert/ics-cal-mcp@v1.0.20261005",
        "ics-cal-mcp"
      ],
      "env": {
        "ICS_URL": "https://outlook.office365.com/owa/calendar/…/calendar.ics",
        "TZ_DEFAULT": "Europe/Brussels"
      }
    }
  }
}
```

Notes:

- The `@v1.0.20261005` suffix pins a release tag. To update, change the tag. If you remove the suffix, uv uses the default branch.
- Claude Desktop starts commands with a minimal `PATH`. If the client cannot find `uvx`, use the full path. To find it, run `which uvx` in a terminal. With Homebrew on Apple Silicon, the path is usually `/opt/homebrew/bin/uvx`.
- Other MCP clients that support stdio servers use the same command, arguments and environment variables.

## Configuration

You configure the server with environment variables.

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `ICS_URL` | Yes | None | The published ICS feed URL (`http` or `https`). The URL is a secret. |
| `TZ_DEFAULT` | No | `Europe/Brussels` | The IANA zone for date queries and for output times. |
| `ICS_PROFILE` | No | `auto` | `auto`, `exchange` or `generic`. See [Feed profiles](#feed-profiles). |
| `CACHE_TTL_SECONDS` | No | `300` | How long the server keeps the downloaded feed in memory. |
| `FETCH_TIMEOUT_MS` | No | `15000` | The timeout for each network step (connect, read) of the download. |

The server ignores the time zone of your computer. The results depend only on `TZ_DEFAULT` and the feed. The same query gives the same result on any machine.

## Security

- The server is read-only. It sends one kind of request: a `GET` to the configured `ICS_URL`. It writes nothing and calls no other service.
- The calendar data stays in memory for at most `CACHE_TTL_SECONDS`. There is no telemetry.
- The URL does not leak. Logs, error messages and tool output show only the scheme and the host (for example `https://outlook.office365.com/…`). The tests make sure that the secret part of the URL never appears.
- If the feed does not end with `END:VCALENDAR`, the server treats it as truncated. The event tools then return an error and do not return partial data. `feed_info` still answers, so that you can see the problem.
- TLS certificates are checked against the trust store of your operating system.

## Development

You need uv. The commands below install the development tools in a local `.venv` folder.

```bash
git clone https://github.com/kspeeckaert/ics-cal-mcp.git
cd ics-cal-mcp
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

To check that the results do not depend on the host time zone, run the tests with another zone:

```bash
TZ=Pacific/Kiritimati uv run pytest
```

To run the server from your local copy against a real feed, without the URL in your shell history:

```bash
read -rs ICS_URL && export ICS_URL
uvx --from . ics-cal-mcp
```

Call `feed_info` first. It must show `ends_with_end_vcalendar: true`, a plausible `vevent_count`, and the profile you expect.

### Project layout

```
src/ics_cal_mcp/
├── __main__.py      # entry point: configuration, start log line, stdio transport
├── server.py        # tool definitions, argument checks, results, MCP wiring
├── config.py        # environment variables (the only module that reads them)
├── profile.py       # exchange and generic feed profiles, detection from the URL
├── feed.py          # download, single-slot TTL cache, END:VCALENDAR check
├── errors.py        # error types, URL masking and sanitizing
├── log.py           # JSON log lines on stderr (stdout is for JSON-RPC only)
└── ics/
    ├── tzids.py         # line unfolding, Windows to IANA TZID rewrite
    ├── windows_zones.py # CLDR Windows to IANA table
    ├── parse.py         # ICS content-line parser
    ├── model.py         # parsed event types
    ├── timeutil.py      # instant arithmetic in epoch milliseconds
    ├── rrule_slots.py   # RRULE occurrences from python-dateutil
    ├── expand.py        # RRULE, EXDATE, RDATE and override expansion
    └── format.py        # output: offsets, busy status, meeting URL, description
```

### Versions

Versions use the format `major.minor.YYYYMMDD`, for example `1.0.20261005`. Increase the version in `pyproject.toml` for each change, and add an entry to [CHANGELOG.md](CHANGELOG.md). Tag each release as `v<version>`.

## License

[MIT](LICENSE). The design, the tool contract and the test fixtures come from [hromadkom/calendar-ics-mcp](https://github.com/hromadkom/calendar-ics-mcp) (MIT, © Martin Hromádko). The Windows time zone table comes from the [Unicode CLDR project](https://github.com/unicode-org/cldr) (Unicode License v3).
