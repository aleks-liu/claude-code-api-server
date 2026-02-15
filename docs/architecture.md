# Architecture

[← Back to README](../README.md)

## Table of Contents

- [Problem Statement](#problem-statement)
- [Solution Overview](#solution-overview)
- [Component Overview](#component-overview)
- [Data Flow](#data-flow)
- [Directory Structure](#directory-structure)
- [Features](#features)
- [Limitations](#limitations)

---

## Problem Statement

**Claude Code** is a powerful AI agent that can iteratively analyze codebases, reading files, exploring code structure, and making intelligent decisions. It's invaluable for tasks like:

- Security vulnerability analysis
- Code review automation
- Documentation generation
- Bug investigation
- Compliance auditing

However, Claude Code is designed for interactive use on a developer's machine. **Enterprise and automation scenarios face challenges:**

| Challenge | Description |
|-----------|-------------|
| **No API Access** | Claude Code runs as a CLI tool, not as a callable API |
| **Manual Execution** | Each analysis requires human interaction |
| **No Multi-tenancy** | Can't serve multiple clients with isolated workspaces |
| **No Cost Attribution** | Can't track API costs per client/project |
| **Security Concerns** | Need to isolate file access between different analyses |

---

## Solution Overview

The **Claude Code API Server** wraps the official [Claude Agent SDK](https://platform.claude.com/docs/en/agent-sdk/overview) in a REST API, providing:

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        Your Automation Pipeline                         │
│   (n8n, Jira, CI/CD, Scripts, Security Scanners, etc.)                 │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      Claude Code API Server                             │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌──────────────┐  │
│  │   FastAPI   │  │   Auth      │  │   Job       │  │   Security   │  │
│  │   REST API  │  │   Manager   │  │   Manager   │  │   Sandbox    │  │
│  └─────────────┘  └─────────────┘  └─────────────┘  └──────────────┘  │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                       Claude Agent SDK                                  │
│            (Full Claude Code capabilities via Python)                   │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                        Anthropic Claude API                             │
└─────────────────────────────────────────────────────────────────────────┘
```

**Key Benefits:**

- **Asynchronous Jobs**: Submit analysis tasks and poll for results
- **Per-Client API Keys**: Track usage and costs per integration
- **Directory Isolation**: Each job can only access its own files
- **Process-Level Sandbox**: bwrap namespace isolation for filesystem and cross-job security
- **File-Based Persistence**: Simple deployment, no database required

---

## Component Overview

```
claude_code_api_server/
├── src/
│   ├── main.py              # FastAPI application and routes
│   ├── admin_router.py      # Admin API endpoints (clients, MCP, agents, skills)
│   ├── config.py            # Configuration management (Pydantic Settings)
│   ├── models.py            # Request/response data models
│   ├── auth.py              # API key authentication (Argon2 hashing)
│   ├── crypto.py            # RSA encryption for admin bootstrap
│   ├── security.py          # Directory isolation & tool permissions
│   ├── sandbox.py           # Process-level bwrap sandbox (primary isolation)
│   ├── sandbox_seccomp.py   # seccomp BPF detection and configuration
│   ├── sandbox_proxy.py     # Per-job HTTP proxy for network isolation
│   ├── security_profiles.py # Security profile CRUD and persistence
│   ├── upload_handler.py    # ZIP archive handling with security
│   ├── job_manager.py       # Job lifecycle and state persistence
│   ├── claude_runner.py     # Claude Agent SDK wrapper
│   ├── mcp_manager.py       # MCP server configuration CRUD
│   ├── mcp_installer.py     # MCP package installation (npm/pip)
│   ├── mcp_loader.py        # MCP runtime loading, env expansion, health checks
│   ├── agent_manager.py     # Subagent definition CRUD and SDK loading
│   ├── skill_manager.py     # Skill definition CRUD and plugin manifest management
│   ├── cleanup.py           # Background cleanup tasks
│   └── logging_config.py    # Structured logging (structlog)
├── create_admin.py          # CLI script for creating the first admin
├── Dockerfile               # Container image
├── docker-compose.yml       # Docker Compose deployment
└── requirements.txt         # Python dependencies
```

---

## Data Flow

```
1. CLIENT uploads ZIP archive
   └─► POST /v1/uploads
       └─► Server generates UUID, validates ZIP, stores temporarily

2. CLIENT creates job with upload reference
   └─► POST /v1/jobs
       └─► Server extracts archive to job directory (single root dir stripped)
       └─► Deletes original archive
       └─► Spawns background task for Claude execution

3. BACKGROUND: Claude Agent executes
   └─► Server snapshots all files in input/ (SHA-256 hashes)
   └─► Claude reads/writes files in input/ directory (its cwd)
   └─► Security callback validates ALL file access
   └─► Server compares post-execution files against snapshot
   └─► New and modified files are copied to output/

4. CLIENT polls for status
   └─► GET /v1/jobs/{job_id}
       └─► Returns status, output text, and base64-encoded output files
```

---

## Directory Structure

```
/data/
├── auth/
│   └── clients.json              # Hashed API keys (Argon2)
├── sandbox/
│   └── profiles.json             # Security profiles configuration
├── mcp/
│   ├── servers.json              # MCP server configuration
│   ├── .lock                     # Concurrent access lock
│   ├── npm/                      # npm-installed MCP packages
│   │   └── node_modules/
│   └── venv/                     # pip-installed MCP packages (isolated virtualenv)
│       └── bin/
├── agents/
│   ├── agents.json               # Subagent management metadata
│   ├── .lock                     # Concurrent access lock
│   └── prompts/                  # Agent definition .md files (source of truth)
│       └── (agent-name.md)
├── skills-plugin/                    # Shared plugin directory (--plugin-dir)
│   ├── .claude-plugin/
│   │   └── plugin.json              # Auto-generated plugin manifest
│   ├── agents/                      # Agent .md files synced from prompts/
│   │   └── (agent-name.md)
│   └── skills/
│       └── (skill-name)/
│           └── SKILL.md             # Skill definition file
├── skills-meta/
│   ├── skills.json                  # Skill management metadata
│   └── .lock                        # Concurrent access lock
├── uploads/
│   └── {upload_id}/
│       ├── archive.zip           # Temporary (deleted when job starts)
│       └── meta.json             # Upload metadata
└── jobs/
    └── {job_id}/
        ├── input/                # Extracted files, Claude's cwd (deleted after job)
        │   └── (user files)
        ├── output/               # New/modified files collected after execution (kept forever)
        │   └── (auto-collected)
        ├── status.json           # Job state and metadata
        └── stdout.txt            # Claude's text output
```

---

## Features

### Core Capabilities

| Feature | Description |
|---------|-------------|
| **Async Job Execution** | Jobs run in background, clients poll for results |
| **ZIP Archive Upload** | Upload codebases as ZIP files (max 50MB) |
| **Per-Job Anthropic Keys** | Each client provides their own Claude API key |
| **CLAUDE.md Support** | Custom agent instructions per job |
| **Configurable Timeouts** | Jobs timeout after configurable duration (default 30 min) |
| **Cost Tracking** | API cost returned with job results |
| **Output File Collection** | New/modified files automatically collected after execution |
| **MCP Server Support** | Extend Claude with external tools via the Model Context Protocol |
| **Custom Subagents** | Define specialized task agents with tailored prompts, tools, and model overrides — managed via admin API and loaded per-job |
| **Skills (Plugins)** | Reusable, auto-invocable capabilities delivered via Claude Code's plugin mechanism — each skill is a directory with a SKILL.md file, managed via admin API |
| **Admin API** | Full HTTP API for managing clients, MCP servers, agents, and skills |

### Security Features

| Feature | Description |
|---------|-------------|
| **Process-Level Sandbox (bwrap)** | Entire Claude CLI runs in an isolated filesystem namespace — user home, other jobs, and system paths are hidden or read-only |
| **Network Isolation** | Per-job HTTP proxy with domain/IP filtering + bwrap `--unshare-net` network namespace isolation. Supports upstream proxy chaining for corporate/enterprise environments |
| **seccomp BPF Hardening** | Blocks direct socket creation inside sandbox, preventing proxy bypass (from `@anthropic-ai/sandbox-runtime`) |
| **Security Profiles** | Configurable per-client profiles controlling network access, tool availability, and MCP server scoping |
| **API Key Authentication** | Server-side keys hashed with Argon2 |
| **Role-Based Access Control** | Admin and client roles with appropriate permissions |
| **Directory Isolation** | Each job can ONLY access its own input/ directory |
| **Path Traversal Protection** | All file paths validated against job boundary |
| **Archive Security** | Zip bomb protection, file count limits |
| **No Filename Injection** | Server generates all IDs, ignores client filenames |
| **Fail-Closed by Default** | Jobs are refused if the bwrap sandbox cannot be created |

For detailed security documentation, see [Security](security-model.md).

---

## Limitations

- **ZIP Only**: Currently only ZIP archives are supported
- **No Streaming**: Results are returned after job completes (no real-time updates)
- **Single Server**: Designed for single-instance deployment (no horizontal scaling)
- **No Job Cancellation**: Once started, jobs run to completion or timeout
