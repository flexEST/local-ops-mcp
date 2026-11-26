import asyncio
from contextlib import closing, contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sqlite3
import sys
import sysconfig
from threading import Thread, Timer
from types import SimpleNamespace

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import pytest

from local_ops_mcp import database, local_io


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "fixture ?#.sqlite"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE readings (id INTEGER PRIMARY KEY, temperature REAL)")
        conn.executemany("INSERT INTO readings VALUES (?, ?)", [(1, 22.5), (2, 24.5)])
    return path


@pytest.mark.parametrize("query", [
    "DELETE FROM readings", "ATTACH DATABASE ':memory:' AS other", "PRAGMA query_only=OFF",
    "SELECT load_extension('/tmp/no-such-extension')",
])
def test_sqlite_denies_non_read_operations(db_path, query):
    with pytest.raises(sqlite3.DatabaseError, match="authoriz"):
        database.sqlite_query(db_path, query, 10)
    assert database.sqlite_query(db_path, "SELECT count(*) FROM readings", 10)["rows"] == [[2]]


def test_sqlite_bounds_results(db_path):
    assert database.sqlite_query(db_path, "SELECT id FROM readings ORDER BY id", 1) == {
        "columns": ["id"], "rows": [[1]], "truncated": True,
    }
    assert database.sqlite_query(db_path, "SELECT id FROM readings ORDER BY id", 2)["truncated"] is False
    assert database.sqlite_query(db_path, "SELECT zeroblob(5000)", 1)["rows"][0][0]["truncated"] is True


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


def test_stdio_workflow(tmp_path, db_path):
    (tmp_path / "dns.log").write_text("first\nsecond\nthird\n")
    with local_router() as router:
        config = tmp_path / "ops.toml"
        config.write_text(
            f'[sqlite]\npath = "{db_path.name}"\n'
            f'[routers.home]\nurl = "{router}/status"\n'
            f'[routers.redirect]\nurl = "{router}/redirect"\n'
            '[dns_logs.lan]\npath = "dns.log"\n'
            '[mqtt]\nhost = "localhost"\n'
            '[mqtt.relays.led]\ntopic = "bench/led/set"\non_payload = "ON"\noff_payload = "OFF"\n'
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
                assert len(tools) == 11

                async def call(name, args=None, *, error=False):
                    result = await client.call_tool(name, args or {})
                    assert result.isError == error, result
                    return result.content[0].text if error else result.structuredContent

                integrations = await call("list_integrations")
                assert integrations["sqlite"] and integrations["routers"] == ["home", "redirect"]
                assert integrations["mqtt_relays"] == []
                schema = await call("sqlite_schema")
                assert [row[1] for row in schema["rows"]] == ["readings"]
                output = await call("sqlite_query", {
                    "query": "WITH subset AS (SELECT temperature FROM readings WHERE id = 1) SELECT * FROM subset",
                })
                assert output["rows"] == [[22.5]]
                assert "not authorized" in await call("sqlite_query", {"query": "DELETE FROM readings"}, error=True)
                assert (await call("router_status", {"name": "home"}))["body"] == '{"online":true}'
                assert "302" in await call("router_status", {"name": "redirect"}, error=True)
                assert "Unknown routers alias" in await call("router_status", {"name": "other"}, error=True)
                assert await call("dns_log_tail", {"name": "lan", "limit": 2}) == {
                    "lines": ["second", "third"], "truncated": True,
                }
                assert "disabled" in await call("mqtt_set_relay", {"name": "led", "on": True}, error=True)

        asyncio.run(asyncio.wait_for(check(), timeout=15))


def test_serial_pseudoterminal():
    pty = pytest.importorskip("pty")
    import os

    master, slave = pty.openpty()
    timer = Timer(0.1, os.write, args=(master, b"23.5\nignored"))
    try:
        timer.start()
        assert local_io.serial_read(os.ttyname(slave), 115200, 1, 5) == {
            "text": "23.5\n", "bytes_read": 5,
        }
    finally:
        timer.cancel()
        timer.join()
        os.close(master)
        os.close(slave)


@dataclass
class FakeMQTTClient:
    published: tuple | None = None

    def subscribe(self, topic, qos):
        assert (topic, qos) == ("bench/temp", 1)
        self.on_message(self, None, SimpleNamespace(topic="bench/other", payload=b"ignore", retain=False))
        self.on_message(self, None, SimpleNamespace(topic=topic, payload=b"23.5", retain=True))
        return local_io.mqtt.MQTT_ERR_SUCCESS, 1

    def publish(self, topic, payload, qos, retain):
        self.published = (topic, payload, qos, retain)
        return SimpleNamespace(wait_for_publish=lambda timeout: None, is_published=lambda: True)


def test_mqtt_exact_topics_and_relay_payload(monkeypatch):
    client = FakeMQTTClient()

    class FakeBroker:
        def __init__(self, config):
            self.client = client

        def __enter__(self):
            return self.client

        def __exit__(self, *exc):
            pass

    monkeypatch.setattr(local_io, "Broker", FakeBroker)
    with pytest.raises(ValueError, match="wildcards"):
        local_io.mqtt_read({}, "bench/#", 1)
    assert local_io.mqtt_read({}, "bench/temp", 1) == {
        "text": "23.5", "retained": True, "truncated": False,
    }
    relay = {"topic": "bench/led/set", "on_payload": "on", "off_payload": "off"}
    with pytest.raises(ValueError, match="disabled"):
        local_io.mqtt_relay({}, relay, True)
    assert client.published is None
    assert local_io.mqtt_relay({"enable_relay_control": True}, relay, False) == {
        "broker_acknowledged": True, "requested_on": False,
    }
    assert client.published == ("bench/led/set", "off", 1, False)


def test_stays_on_local_router_targets():
    for url in ("http://example.com/status", "http://169.254.169.254/", "http://user@127.0.0.1/status"):
        with pytest.raises(ValueError, match="private or loopback"):
            local_io.router_status(url, None)
