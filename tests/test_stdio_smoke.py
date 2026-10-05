"""End to end: start the real server as a subprocess, talk MCP over stdio
with the SDK client, and serve the fixture from a loopback HTTP server."""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from conftest import FIXTURE


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib name
        body = FIXTURE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/calendar")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def feed_url():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    # The /owa/calendar/ path selects the exchange profile.
    yield f"http://127.0.0.1:{httpd.server_port}/owa/calendar/SECRETPATH123/calendar.ics"
    httpd.shutdown()


def test_full_stdio_session_with_a_real_subprocess(feed_url):
    async def session():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "ics_cal_mcp"],
            env={"ICS_URL": feed_url, "TZ_DEFAULT": "Europe/Prague"},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as s:
                init = await s.initialize()
                assert init.server_info.name == "ics-cal-mcp"

                tools = await s.list_tools()
                assert [t.name for t in tools.tools] == [
                    "feed_info",
                    "get_events",
                    "get_events_range",
                ]
                assert tools.tools[1].annotations.read_only_hint is True

                res = await s.call_tool("get_events", {"date": "2026-03-18"})
                assert res.is_error is False
                assert res.structured_content["date"] == "2026-03-18"
                assert len(res.structured_content["events"]) == 4
                assert json.loads(res.content[0].text) == res.structured_content

                info = await s.call_tool("feed_info", {})
                assert info.structured_content["profile"] == "exchange"
                assert "SECRETPATH123" not in info.model_dump_json()

                bad = await s.call_tool("get_events", {"date": "nope"})
                assert bad.is_error is True

    anyio.run(session)


def test_exits_with_code_1_on_bad_configuration():
    import subprocess

    proc = subprocess.run(
        [sys.executable, "-m", "ics_cal_mcp"],
        env={"ICS_URL": "ftp://secret-host.example/x", "PATH": ""},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert "ICS_URL must be an http(s) URL" in proc.stderr
    assert "secret-host" not in proc.stderr
