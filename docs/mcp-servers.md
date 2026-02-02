# MCP Server Management

[← Back to README](../README.md)

The server supports [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) servers, which extend Claude's capabilities with external tools. MCP servers are configured via the Admin API and automatically attached to every job.

## Table of Contents

- [How MCP Works](#how-mcp-works)
- [Install an MCP Server (npm)](#install-an-mcp-server-npm)
- [Install an MCP Server (pip)](#install-an-mcp-server-pip)
- [Add an HTTP/SSE MCP Server](#add-an-httpsse-mcp-server)
- [Add a stdio MCP Server Manually](#add-a-stdio-mcp-server-manually)
- [List Configured Servers](#list-configured-servers)
- [Health-Check Servers](#health-check-servers)
- [Remove a Server](#remove-a-server)
- [Startup Behavior](#startup-behavior)
- [Sandbox Integration](#sandbox-integration)
- [Configuration File Format](#configuration-file-format)

---

## How MCP Works

When MCP servers are configured, the server:

1. **At startup**: loads `servers.json`, expands `${ENV_VAR}` placeholders, health-checks each stdio server by sending a JSON-RPC `initialize` request
2. **Per job**: passes the MCP server config to the Claude Agent SDK (filtered by the client's security profile `allowed_mcp_servers`), adds `mcp__<name>__*` patterns to `allowed_tools`, and exposes package directories read-only inside the bwrap sandbox
3. **At `/v1/health`**: reports MCP server status (`ok`, `skipped`, or `failed`)

---

## Install an MCP Server (npm)

Most MCP servers are npm packages:

```bash
curl -X POST http://localhost:8000/v1/admin/mcp/install \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "package": "@modelcontextprotocol/server-sequential-thinking",
    "description": "Sequential thinking for complex reasoning"
  }'
```

The package is installed into `/data/mcp/npm/node_modules/`. The server name is auto-derived (e.g., `sequential-thinking`), or you can specify it with the `"name"` field.

---

## Install an MCP Server (pip)

Python-based MCP servers are installed into an isolated virtualenv:

```bash
curl -X POST http://localhost:8000/v1/admin/mcp/install \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "package": "markitdown-mcp",
    "pip": true,
    "description": "Convert documents to Markdown"
  }'
```

Or use the `pip://` prefix for auto-detection:

```bash
curl -X POST http://localhost:8000/v1/admin/mcp/install \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"package": "pip://markitdown-mcp"}'
```

The package is installed into `/data/mcp/venv/`.

---

## Add an HTTP/SSE MCP Server

For remote MCP servers that use HTTP or SSE transport:

```bash
curl -X POST http://localhost:8000/v1/admin/mcp \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "web-search",
    "type": "http",
    "url": "https://mcp.example.com/v1/search",
    "headers": {"Authorization": "Bearer ${SEARCH_API_KEY}"},
    "description": "Web search via external service"
  }'
```

Environment variables in header values support `${VAR}` and `${VAR:-default}` placeholders, expanded from the server's environment at startup.

---

## Add a stdio MCP Server Manually

For custom or locally built MCP servers.

> **Note:** The command/script must already exist on the server filesystem. Use Docker image bundling (`COPY` in Dockerfile), volume mounts (`-v /host/path:/container/path`), or system packages to make files available. For package registry installations, use the install endpoint instead.

```bash
curl -X POST http://localhost:8000/v1/admin/mcp \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "my-tool",
    "type": "stdio",
    "command": "/usr/local/bin/my-mcp-server",
    "args": ["--port", "0"],
    "env": {"API_KEY": "${MY_TOOL_API_KEY}"},
    "description": "Custom internal tool"
  }'
```

---

## List Configured Servers

```bash
curl http://localhost:8000/v1/admin/mcp \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

---

## Health-Check Servers

Verify all servers respond to the MCP `initialize` protocol:

```bash
curl -X POST http://localhost:8000/v1/admin/mcp/health-check \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

HTTP/SSE servers are skipped during health checks (assumed reachable at runtime).

---

## Remove a Server

```bash
# Remove and uninstall the package
curl -X DELETE "http://localhost:8000/v1/admin/mcp/sequential-thinking" \
  -H "Authorization: Bearer $ADMIN_TOKEN"

# Remove but keep the installed package
curl -X DELETE "http://localhost:8000/v1/admin/mcp/sequential-thinking?keep_package=true" \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

---

## Startup Behavior

At startup, the server health-checks all configured stdio MCP servers. Servers that fail their health check are **removed from the active configuration** and the server starts in **degraded mode** — failed servers are excluded from jobs but the server operates normally. A `mcp_health_check_degraded` warning is logged listing the failed servers.

---

## Sandbox Integration

MCP package directories (`/data/mcp/npm/node_modules/` and `/data/mcp/venv/`) are automatically exposed **read-only** inside the bwrap sandbox. This allows MCP server processes spawned by the Claude CLI to read their own code while maintaining the sandbox's isolation guarantees.

For more on sandbox behavior, see [Security — Sandbox Isolation](security-model.md#sandbox-isolation-bwrap).

## Security Profile Filtering

MCP server availability per job is controlled by the client's security profile. The `allowed_mcp_servers` field determines which MCP servers are passed to the SDK:

- `null` (default) — all configured MCP servers are available
- `[]` — no MCP servers available
- `["server-a", "server-b"]` — only listed servers are available

This filtering is applied both at SDK configuration time (servers excluded from options) and at runtime via the `can_use_tool` callback (MCP tool calls denied if the server is not allowed).

For more on security profiles, see [Security — Security Profiles](security-model.md#security-profiles).

---

## Configuration File Format

The configuration is stored at `/data/mcp/servers.json` in a format compatible with Claude Desktop and the Agent SDK:

```json
{
  "mcpServers": {
    "sequential-thinking": {
      "command": "node",
      "args": ["/data/mcp/npm/node_modules/@modelcontextprotocol/server-sequential-thinking/dist/index.js"]
    },
    "web-search": {
      "type": "http",
      "url": "https://mcp.example.com/v1/search",
      "headers": {
        "Authorization": "Bearer ${SEARCH_API_KEY}"
      }
    }
  },
  "_metadata": {
    "sequential-thinking": {
      "added_at": "2026-01-26T10:30:00",
      "description": "Sequential thinking for complex reasoning",
      "package_manager": "npm",
      "package": "@modelcontextprotocol/server-sequential-thinking"
    }
  }
}
```

The `_metadata` section is used for uninstall tracking and display. The SDK ignores it.

See [API Reference — MCP Server Management](api-reference.md#mcp-server-management) for full endpoint details.
