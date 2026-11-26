import argparse
import os
from pathlib import Path
import tomllib
from typing import Any

from mcp.server.fastmcp import FastMCP
from . import local_io


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

    mcp = FastMCP("local-ops-mcp")

    @mcp.tool()
    def list_integrations() -> dict[str, Any]:
        """List enabled integrations without connection details or credentials."""
        return {
            "routers": sorted(section("routers")),
            "dns_logs": sorted(section("dns_logs")),
        }

    @mcp.tool()
    def router_status(name: str) -> dict[str, Any]:
        """GET the fixed status endpoint for a configured router alias."""
        router = entry("routers", name)
        return local_io.router_status(router["url"], router.get("token_env"))

    @mcp.tool()
    def dns_log_tail(name: str, limit: int = 30) -> dict[str, Any]:
        """Read recent lines from a configured local DNS log."""
        log = entry("dns_logs", name)
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        return local_io.dns_log_tail(path(log["path"]), limit)

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="Local router and DNS diagnostics MCP server")
    parser.add_argument("--config", type=Path, help="Path to the integration TOML file")
    args = parser.parse_args()
    config = args.config
    if config is None and os.environ.get("LOCAL_OPS_CONFIG"):
        config = Path(os.environ["LOCAL_OPS_CONFIG"])
    create_server(config).run(transport="stdio")


if __name__ == "__main__":
    main()
