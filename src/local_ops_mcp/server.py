import argparse
import os
from pathlib import Path
import tomllib
from typing import Any

from mcp.server.fastmcp import FastMCP
import psycopg

from . import database, local_io


def create_server(config_path: Path | None = None) -> FastMCP:
    if config_path is None:
        config = {}
        base = Path.cwd()
    else:
        config_path = config_path.expanduser().resolve()
        with config_path.open("rb") as stream:
            config = tomllib.load(stream)
        base = config_path.parent

    def path(value: str) -> Path:
        return (base / Path(value).expanduser()).resolve()

    def section(name: str) -> dict:
        return config.get(name, {})

    def entry(section_name: str, name: str) -> dict:
        entries = section(section_name)
        if name not in entries:
            raise ValueError(f"Unknown {section_name} alias")
        return entries[name]

    def postgres_call(operation, *args):
        variable = section("postgres").get("dsn_env", "LOCAL_OPS_POSTGRES_DSN")
        dsn = local_io.configured_secret(variable)
        try:
            return operation(dsn, *args)
        except psycopg.Error as exc:
            code = exc.sqlstate or "connection error"
            raise ValueError(f"Postgres request failed ({code}). Check the DSN, TLS and role grants.") from None

    def sqlite_path() -> Path:
        value = section("sqlite").get("path")
        if not value:
            raise ValueError("Configure [sqlite].path first")
        return path(value)

    def broker_config() -> dict:
        broker = section("mqtt").copy()
        if not broker.get("host"):
            raise ValueError("Configure [mqtt].host first")
        if broker.get("ca_file"):
            broker["ca_file"] = str(path(broker["ca_file"]))
        return broker

    mcp = FastMCP("local-ops-mcp")

    @mcp.tool()
    def list_integrations() -> dict[str, Any]:
        """List configured integration aliases without connection details or credentials."""
        broker = section("mqtt")
        dsn_env = section("postgres").get("dsn_env", "LOCAL_OPS_POSTGRES_DSN")
        return {
            "sqlite": bool(section("sqlite").get("path")),
            "postgres": bool(os.environ.get(dsn_env)),
            "routers": sorted(section("routers")),
            "dns_logs": sorted(section("dns_logs")),
            "serial_devices": sorted(section("serial")),
            "mqtt_sensors": sorted(broker.get("sensors", {})),
            "mqtt_relays": sorted(broker.get("relays", {})) if broker.get("enable_relay_control") is True else [],
        }

    @mcp.tool()
    def sqlite_schema(limit: int = 100) -> dict[str, Any]:
        """Inspect DDL for objects in the configured SQLite database."""
        return database.sqlite_schema(sqlite_path(), database.row_limit(limit))

    @mcp.tool()
    def sqlite_query(query: str, limit: int = 25) -> dict[str, Any]:
        """Run one read-only SELECT or CTE on the configured SQLite file."""
        return database.sqlite_query(sqlite_path(), query, database.row_limit(limit))

    @mcp.tool()
    def postgres_schema(schema: str = "public", table: str | None = None, limit: int = 100) -> dict[str, Any]:
        """List visible Postgres columns. Give a table name to include constraints and indexes."""
        return postgres_call(database.postgres_schema, schema, table, database.row_limit(limit))

    @mcp.tool()
    def postgres_rls(schema: str, table: str, limit: int = 50) -> dict[str, Any]:
        """Inspect a table's policies and whether RLS applies to the connected role."""
        return postgres_call(database.postgres_rls, schema, table, database.row_limit(limit))

    @mcp.tool()
    def postgres_sample(schema: str, table: str, columns: list[str], limit: int = 10) -> dict[str, Any]:
        """Sample named columns from a Postgres table as the connected role."""
        return postgres_call(database.postgres_sample, schema, table, columns, database.row_limit(limit))

    @mcp.tool()
    def router_status(name: str) -> dict[str, Any]:
        """GET the fixed status endpoint for a configured router alias."""
        router = entry("routers", name)
        return local_io.router_status(router["url"], router.get("token_env"))

    @mcp.tool()
    def dns_log_tail(name: str, limit: int = 30) -> dict[str, Any]:
        """Read recent lines from a configured local DNS log."""
        log = entry("dns_logs", name)
        return local_io.dns_log_tail(path(log["path"]), database.row_limit(limit))

    @mcp.tool()
    def serial_read(name: str, seconds: float = 1.0, limit: int = 1024) -> dict[str, Any]:
        """Read bounded UTF-8 output from a configured serial device."""
        device = entry("serial", name)
        return local_io.serial_read(device["port"], device.get("baudrate", 115200), seconds, limit)

    @mcp.tool()
    def mqtt_read(name: str, seconds: float = 3.0) -> dict[str, Any]:
        """Wait for one message on a configured MQTT sensor topic."""
        broker = broker_config()
        sensor = broker.get("sensors", {}).get(name)
        if sensor is None:
            raise ValueError("Unknown MQTT sensor alias")
        return local_io.mqtt_read(broker, sensor["topic"], seconds)

    @mcp.tool()
    def mqtt_set_relay(name: str, on: bool) -> dict[str, Any]:
        """Publish a configured bench relay command. Broker acknowledgment does not confirm device state."""
        broker = broker_config()
        relay = broker.get("relays", {}).get(name)
        if relay is None:
            raise ValueError("Unknown MQTT relay alias")
        return local_io.mqtt_relay(broker, relay, on)

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="Local database, network and bench device MCP server")
    parser.add_argument("--config", type=Path, help="Path to the integration TOML file")
    args = parser.parse_args()
    config = args.config
    if config is None and os.environ.get("LOCAL_OPS_CONFIG"):
        config = Path(os.environ["LOCAL_OPS_CONFIG"])
    create_server(config).run(transport="stdio")


if __name__ == "__main__":
    main()
