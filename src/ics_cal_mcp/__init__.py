"""Read-only MCP server for an Outlook/Exchange published ICS calendar feed."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ics-cal-mcp")
except PackageNotFoundError:  # running from a source tree without install
    __version__ = "0.0.0"

SERVER_NAME = "ics-cal-mcp"
