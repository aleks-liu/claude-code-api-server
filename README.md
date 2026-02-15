# Claude Code API Server

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.14](https://img.shields.io/badge/Python-3.14%2B-blue.svg)](https://www.python.org/)

A service layer around the [Claude Agent SDK](https://platform.claude.com/docs/en/agent-sdk/overview) that turns Claude Code into a network-accessible, multi-client service with auth, sandboxing, and job management out of the box.

## Why This Exists

[Claude Code](https://docs.anthropic.com/en/docs/claude-code) is Anthropic's AI coding agent. It can write code, analyze codebases, find vulnerabilities, review pull requests, generate documentation — and it's genuinely good at it. It works interactively in your terminal, supports non-interactive mode via CLI flags, and Anthropic provides the [Claude Agent SDK](https://platform.claude.com/docs/en/agent-sdk/overview) for embedding it programmatically into your own code.

The SDK is the right building block when you're embedding Claude Code into a single script or application. But once you need **multiple clients** — CI/CD pipelines, webhook automations, security scanners, different teams — all talking to Claude Code over the network, you need a service layer on top:

- **An HTTP API** so any tool that speaks REST can submit tasks
- **Client authentication** so each pipeline or team gets its own API key
- **Job management** so tasks run asynchronously and results are retrievable later
- **Workspace isolation** so one client's code never leaks to another
- **File upload** so codebases can be sent as ZIP archives
- **Cost tracking** per client

This project wraps the Claude Agent SDK in exactly that — a ready-to-deploy service that opens Claude Code to network clients and covers the scenarios you'd need in practice.

## How It Works

```
  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
  │  GitHub  │ │  GitLab  │ │          │ │          │
  │  Actions │ │  CI/CD   │ │ Jenkins  │ │   n8n    │  ...any HTTP client
  └─────┬────┘ └────┬─────┘ └─────┬────┘ └────┬─────┘
        │           │             │           │
        └───────────┴──────┬──────┴───────────┘
                           │ REST API
                           ▼
  ┌──────────────────────────────────────────────────────────┐
  │                 Claude Code API Server                   │
  │                                                          │
  │  Auth & RBAC · Security Profiles · Network Isolation     │
  │  MCP Servers · Custom Agents · Skills · Cost Tracking    │
  │                                                          │
  │  ┌────────────────┐ ┌────────────────┐ ┌──────────────┐  │
  │  │ Job 1          │ │ Job 2          │ │ Job 3        │  │
  │  │ ┌────────────┐ │ │ ┌────────────┐ │ │ ┌──────────┐ │  │
  │  │ │ Claude     │ │ │ │ Claude     │ │ │ │ Claude   │ │  │
  │  │ │ Code       │ │ │ │ Code       │ │ │ │ Code     │ │  │
  │  │ └────────────┘ │ │ └────────────┘ │ │ └──────────┘ │  │
  │  │  bwrap sandbox │ │  bwrap sandbox │ │ bwrap sandbox│  │
  │  └────────────────┘ └────────────────┘ └──────────────┘  │
  └────────────────────────────┬─────────────────────────────┘
                               │
                               ▼
                  ┌───────────────────────┐
                  │  Anthropic Claude API │
                  └───────────────────────┘
```

### Example: Automated Security Review in CI/CD

1. A merge request is created in GitLab
2. A webhook triggers your automation (e.g., n8n)
3. The automation uploads the repository as a ZIP archive to the Claude Code API Server
4. The server runs Claude Code with your security review prompt
5. The automation retrieves the analysis report and posts it back to the MR

All of this happens without human intervention, using a single API key per pipeline.

## Design Philosophy

This project deliberately keeps things simple:

- **Single container** — one Python application, nothing else to deploy
- **No database** — all state is file-based (JSON on disk)
- **No message queue** — async jobs run in-process with background tasks
- **No external dependencies** — no Redis, no Postgres, nothing extra
- **Process-level isolation** — each job runs in a bwrap sandbox with its own filesystem namespace

The trade-off is horizontal scalability — this is designed for teams and organizations, not planet-scale SaaS. If you need to run dozens of concurrent analyses, scale vertically or run multiple instances behind a load balancer.

## Key Features

- **Async Job Execution** — submit analysis tasks and poll for results
- **ZIP Archive Upload** — upload codebases as ZIP files for processing
- **Per-Client API Keys** — track usage and costs per integration (Argon2 hashed)
- **Process-Level Sandbox** — bwrap namespace isolation; jobs cannot see each other's files
- **Security** — path traversal protection, directory isolation, fail-closed defaults, upstream proxy support for corporate environments
- **Output File Collection** — new/modified files automatically detected and returned
- **MCP Server Support** — extend Claude with external tools (npm, pip, HTTP/SSE)
- **Custom Subagents** — define specialized task agents with tailored prompts and tools
- **Skills (Plugin-Based)** — reusable, auto-invocable capabilities delivered via Claude Code's plugin mechanism
- **Admin API** — full HTTP API for managing clients, MCP servers, agents, and skills
- **Configurable Timeouts & Cleanup** — automatic resource management

## Current Limitations

This project makes deliberate trade-offs in favor of simplicity. Here's what that means in practice:

- **Not built for high concurrency.** File-based state, in-process job queue, no clustering. This is comfortable running tens of jobs per day for a team — not hundreds per minute for a platform. If you need more throughput, scale vertically or run independent instances.

- **Output size is bounded by HTTP.** Job results — including any files Claude produces — come back in a single HTTP response. That puts a practical limit around 10 MB per job. Fine for reports, patches, and code reviews; not for generating entire repositories.

- **Single node, no replication.** Each instance keeps its own state on disk. There's no shared storage, no built-in HA, no cluster mode. Data persistence is your responsibility — the `data/` directory needs to live on a persistent volume to survive restarts. There's no automatic failover.

- **No streaming.** You submit a task, the server works on it, you pick up the result when it's done. There is no partial output or real-time streaming — and that's by design. The execution model is fire-and-forget: give it a clear job, get back a complete result.

## Quick Start

### 1. Install Dependencies

```bash
cd claude_code_api_server

python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# or: .venv\Scripts\activate  # Windows

pip install -r requirements.txt
```

### 2. Create First Admin

```bash
mkdir -p data/auth data/jobs data/uploads data/mcp

export CCAS_DATA_DIR=./data

python create_admin.py my-admin
```

**Save the generated API key!** It cannot be retrieved later.

### 3. Start the Server

```bash
# Development mode
python -m uvicorn src.main:app --reload --host 0.0.0.0 --port 8000

# Or production mode
python -m uvicorn src.main:app --host 0.0.0.0 --port 8000 --workers 1
```

### 4. Test the API

```bash
# Health check
curl http://localhost:8000/v1/health

# Create a client via admin API
curl -X POST http://localhost:8000/v1/admin/clients \
  -H "Authorization: Bearer YOUR_ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"client_id": "my-pipeline", "description": "CI/CD pipeline"}'

# Upload a test archive
zip -r test.zip ./src
curl -X POST http://localhost:8000/v1/uploads \
  -H "Authorization: Bearer CLIENT_API_KEY" \
  -F "file=@test.zip"
```

See [Usage Examples](docs/usage-examples.md) for complete end-to-end workflows including n8n integration.

## Documentation

| Document | Description |
|----------|-------------|
| [Architecture](docs/architecture.md) | System design, components, data flow, feature details |
| [Installation](docs/installation.md) | Docker, local Python, and systemd deployment |
| [Configuration](docs/configuration.md) | Environment variables reference |
| [Client Management](docs/client-management.md) | Managing API clients and admin setup |
| [MCP Servers](docs/mcp-servers.md) | Installing and managing MCP server extensions |
| [Subagents](docs/subagents.md) | Defining and managing custom subagents |
| [Skills](docs/skills.md) | Plugin-based skills: auto-invocable capabilities for Claude Code |
| [API Reference](docs/api-reference.md) | Endpoints, request/response formats, error codes |
| [Usage Examples](docs/usage-examples.md) | cURL workflow, Python client, n8n integration |
| [Security](docs/security-model.md) | Sandbox isolation, authentication, directory isolation |
| [Operations](docs/operations.md) | Logging, health monitoring, cleanup, backup |
| [Troubleshooting](docs/troubleshooting.md) | Common issues and debug mode |
| [Development](docs/development.md) | Local setup, tests, code structure |

## Contributing

Contributions are welcome. Please open an issue first to discuss what you'd like to change.

## License

This project is licensed under the [Apache License 2.0](LICENSE).

## Author

**Aleksandr Liukov** — [github.com/aleks-liu](https://github.com/aleks-liu)

---

*Built with the [Claude Agent SDK](https://platform.claude.com/docs/en/agent-sdk/overview)*
