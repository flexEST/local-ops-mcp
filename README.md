# Local Ops MCP

Read router status and local DNS logs through one stdio MCP server.

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). Run from the checkout:

```sh
uv sync --locked
cp local-ops.example.toml local-ops.toml
```

Edit the TOML, then register the stdio server in your MCP client. For Claude Desktop, use this configuration with absolute paths to your checkout:

```json
{
  "mcpServers": {
    "local-ops-mcp": {
      "command": "/absolute/path/to/local-ops-mcp/.venv/bin/local-ops-mcp",
      "args": ["--config", "/absolute/path/to/local-ops-mcp/local-ops.toml"]
    }
  }
}
```

On Windows, use the executable under `.venv\Scripts`. Set `LOCAL_OPS_CONFIG` to the config file path to use an environment variable instead of `--config`.

## Tools

| Tool | Configuration |
| --- | --- |
| `list_integrations` | Lists router and DNS log aliases. |
| `router_status` | Requests an explicitly configured HTTP(S) status URL using a private or loopback IP. Use a documented read-only OpenWrt or Tenda endpoint, or a local status exporter. Redirects are refused. |
| `dns_log_tail` | Reads recent lines from a configured regular file, such as `/var/log/dnsmasq.log`. The MCP process needs read access. |

Router bearer tokens are read from the environment variable named by `token_env`. HTTPS requires a certificate valid for the configured IP. Run the server from a computer with access to the router and logs. Returned log lines and status bodies may be passed to your MCP client's model provider.

## Tests

Run `uv run --locked pytest`. The stdio test uses a loopback HTTP fixture and a temporary DNS log.
