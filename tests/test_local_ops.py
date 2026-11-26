import asyncio
from pathlib import Path
import sys
import sysconfig

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from local_ops_mcp import local_io


@contextmanager
def local_router():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/status")
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"online":true}')

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()


def test_stdio_workflow(tmp_path):
    (tmp_path / "dns.log").write_text("first\nsecond\nthird\n")
    with local_router() as router:
        config = tmp_path / "ops.toml"
        config.write_text(
            f'[routers.home]\nurl = "{router}/status"\n'
            f'[routers.redirect]\nurl = "{router}/redirect"\n'
            '[dns_logs.lan]\npath = "dns.log"\n'
        )

        async def check():
            script = "local-ops-mcp.exe" if sys.platform == "win32" else "local-ops-mcp"
            params = StdioServerParameters(
                command=str(Path(sysconfig.get_path("scripts")) / script),
                args=["--config", str(config)],
            )
            async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as client:
                await client.initialize()
                tools = {tool.name for tool in (await client.list_tools()).tools}
                assert tools == {"list_integrations", "router_status", "dns_log_tail"}

                async def call(name, args=None, *, error=False):
                    result = await client.call_tool(name, args or {})
                    assert result.isError == error, result
                    return result.content[0].text if error else result.structuredContent

                integrations = await call("list_integrations")
                assert integrations == {"routers": ["home", "redirect"], "dns_logs": ["lan"]}
                assert (await call("router_status", {"name": "home"}))["body"] == '{"online":true}'
                assert "302" in await call("router_status", {"name": "redirect"}, error=True)
                assert "Unknown routers alias" in await call("router_status", {"name": "other"}, error=True)
                assert await call("dns_log_tail", {"name": "lan", "limit": 2}) == {
                    "lines": ["second", "third"], "truncated": True,
                }

        asyncio.run(asyncio.wait_for(check(), timeout=15))


def test_stays_on_local_router_targets():
    for url in ("http://example.com/status", "http://169.254.169.254/", "http://user@127.0.0.1/status"):
        with pytest.raises(ValueError, match="private or loopback"):
            local_io.router_status(url, None)
