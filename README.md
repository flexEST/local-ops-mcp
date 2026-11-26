# Local Ops MCP

One Python MCP server for database inspection, LAN status, and IoT bench debugging. It runs over stdio and starts without external services. Enable only the integrations you need in the TOML file.

## Start with SQLite

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). From the repository root:

```sh
uv sync --locked
```

For a fresh demo, run:

```sh
mkdir -p /tmp/local-ops-demo
cp -n local-ops.example.toml /tmp/local-ops-demo/local-ops.toml
uv run --locked python - <<'PY'
from contextlib import closing
import sqlite3

with closing(sqlite3.connect("/tmp/local-ops-demo/demo.sqlite3")) as db, db:
    db.execute("CREATE TABLE IF NOT EXISTS readings (id INTEGER PRIMARY KEY, temperature REAL)")
    db.execute("INSERT OR IGNORE INTO readings VALUES (1, 22.5)")
PY
```

In your MCP client's configuration, register the server using the absolute checkout path. The following works as a Claude Desktop MCP configuration on Unix-like systems after substituting the checkout path:

```json
{
  "mcpServers": {
    "local-ops": {
      "command": "/absolute/path/to/local-ops-mcp/.venv/bin/local-ops-mcp",
      "args": ["--config", "/tmp/local-ops-demo/local-ops.toml"]
    }
  }
}
```

Use `.venv\Scripts\local-ops-mcp.exe` on Windows and a config path on that machine. With Claude Code, you can register the same executable with `claude mcp add --transport stdio local-ops -- /absolute/path/to/local-ops-mcp/.venv/bin/local-ops-mcp --config /tmp/local-ops-demo/local-ops.toml`. Your client launches a fresh process and manages its stdin/stdout. Try asking it to list configured integrations, inspect the SQLite schema, and show the temperature in `readings`.

To use your own services, copy `local-ops.example.toml` to `local-ops.toml` and point the client at its absolute path. You can also set `LOCAL_OPS_CONFIG` in the client's process environment instead of passing `--config`. An omitted config starts the server with no local device aliases.

## Integrations

| Tools | Configuration and behavior |
| --- | --- |
| `list_integrations` | Shows which integrations and aliases are configured. |
| `sqlite_schema`, `sqlite_query` | Set `[sqlite].path` to a local SQLite database. The server opens it read-only and accepts single SELECTs and CTEs. |
| `postgres_schema`, `postgres_rls`, `postgres_sample` | Set the DSN environment variable in the MCP server process. Inspect columns, constraints, indexes, RLS policies, and samples of named columns visible to the connected role. |
| `router_status` | Define a router alias with a fixed URL using a private or loopback IP. The server issues GET requests and refuses redirects. Use a documented status URL for your OpenWrt/Tenda model or a local read-only status exporter. Bearer auth requires HTTPS and a certificate valid for the configured IP. |
| `dns_log_tail` | Define a log alias with a readable regular file, such as a dnsmasq log. Check that your DNS service is configured to write the events you need. |
| `serial_read` | Define an alias with a serial device and baud rate. The server process needs OS access to that device. Opening a port can reset some ESP32 boards. |
| `mqtt_read`, `mqtt_set_relay` | Configure one broker and explicitly name each topic. Reads accept one retained or live message. Relay writes require explicit enablement and predefined ON/OFF payloads. |

Router diagnostics are based on configured status endpoints and local logs. Use the router's DHCP lease or device status endpoint for an inventory when the model offers one.

### Postgres and Supabase

Set `LOCAL_OPS_POSTGRES_DSN` in the environment inherited by the MCP client process, or rename it with `[postgres].dsn_env`. Use a dedicated LOGIN role with SELECT grants only on the schemas and relations you intend to inspect. For meaningful RLS checks, use a role without superuser, ownership, or BYPASSRLS privileges on the inspected table. The server verifies remote TLS hostnames and certificates. Supply a trusted CA via the DSN with `sslrootcert=/absolute/path/to/ca.pem` or, with supported libpq versions, `sslrootcert=system`.

`postgres_rls` reports whether policies apply to that database role. Supabase API requests can supply JWT claims and different roles, so validate claim-dependent policies in your application or test environment. To draft a migration, ask the client to inspect the target schema, table, and policies, then write a migration file for review. Apply migrations separately with your project's normal migration tooling.

### MQTT and hardware

Use a broker account with topic-level ACLs. TLS is enabled by default. Plaintext is available for loopback or a private IP on a trusted development network. The MQTT password and optional router bearer token are read from named environment variables. Keep credentials outside the TOML file.

Use relay control only with low-voltage bench loads such as an LED. Set `enable_relay_control = true` only when you intend to send device commands. The tool reports the QoS 1 broker acknowledgment. Verify the physical state on the device or on a separately configured state topic. Publish is non-retained.

Run the server on a computer with access to your router, logs, serial ports, and broker. A cloud container cannot access a laptop's USB devices or home LAN by default. Query rows, log lines, and sensor messages returned by tools can be sent to the MCP client and its model provider. Point integrations at data you intend to share with that client.

## Development

```sh
uv sync --locked
uv run --locked pytest
uv build
```

The default tests use a temporary SQLite database, a loopback HTTP server, a Linux pseudo-terminal where available, and a fake MQTT client. They require no database or broker credentials.
