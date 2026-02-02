# API Reference

[← Back to README](../README.md)

## Table of Contents

- [Authentication](#authentication)
- [GET /v1/health](#get-v1health)
- [POST /v1/uploads](#post-v1uploads)
- [POST /v1/jobs](#post-v1jobs)
- [GET /v1/jobs/{job_id}](#get-v1jobsjob_id)
- [Job Status Values](#job-status-values)
- [Admin API](#admin-api)
  - [Client Management](#client-management)
  - [Security Profile Management](#security-profile-management)
  - [MCP Server Management](#mcp-server-management)
  - [Agent Management](#agent-management)
  - [Skill Management](#skill-management)

---

## Authentication

All endpoints (except `/v1/health`) require authentication:

```
Authorization: Bearer <your_server_api_key>
```

For job creation, also provide your Anthropic API key:

```
X-Anthropic-Key: <your_anthropic_api_key>
```

### Client Roles

Clients have one of two roles:

| Role | Access |
|------|--------|
| `client` | `/v1/uploads`, `/v1/jobs` endpoints |
| `admin` | All endpoints including `/v1/admin/*` |

Admin endpoints return `403 Forbidden` for non-admin clients.

---

## GET /v1/health

Health check endpoint. No authentication required.

**Response:**
```json
{
  "status": "ok"
}
```

This is a minimal public endpoint. For detailed server status including active jobs, MCP server health, and version info, use `GET /v1/admin/status` (admin only).

---

## POST /v1/uploads

Upload a ZIP archive for later processing.

**Headers:**
```
Authorization: Bearer <server_api_key>
Content-Type: multipart/form-data
```

**Body:**
- `file`: ZIP archive (max 50MB)

**Response (201 Created):**
```json
{
  "upload_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "expires_at": "2025-01-25T11:00:00Z",
  "size_bytes": 15234567
}
```

**Errors:**
| Status | Error Code | Description |
|--------|------------|-------------|
| 400 | `INVALID_ARCHIVE` | Not a valid ZIP file |
| 400 | `TOO_MANY_FILES` | Archive exceeds file count limit |
| 400 | `PATH_TRAVERSAL` | Archive contains unsafe paths |
| 401 | - | Invalid API key |
| 413 | `ARCHIVE_TOO_LARGE` | Exceeds size limit |

---

## POST /v1/jobs

Create a new analysis job.

**Headers:**
```
Authorization: Bearer <server_api_key>
X-Anthropic-Key: <anthropic_api_key>
Content-Type: application/json
```

**Body:**
```json
{
  "upload_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "prompt": "Analyze this Python codebase for SQL injection vulnerabilities. Write detailed findings to a file called vulnerabilities.json",
  "claude_md": "Focus on database query patterns. Use security best practices.",
  "timeout_seconds": 1800
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `upload_id` | No | ID from previous upload (omit for prompt-only jobs) |
| `prompt` | Yes | Task description for Claude |
| `claude_md` | No | Additional CLAUDE.md instructions |
| `model` | No | Claude model to use (default: server's `CCAS_DEFAULT_MODEL`) |
| `timeout_seconds` | No | Job timeout (default: 1800) |

**Response (202 Accepted):**
```json
{
  "job_id": "job_a1b2c3d4e5f6",
  "status": "PENDING",
  "created_at": "2025-01-25T10:30:00Z"
}
```

**Errors:**
| Status | Error Code | Description |
|--------|------------|-------------|
| 400 | `UPLOAD_ERROR` | Upload not found or expired |
| 400 | - | Invalid request body |
| 400 | - | Missing `X-Anthropic-Key` header |
| 401 | - | Invalid server API key |

---

## GET /v1/jobs/{job_id}

Get job status and results.

**Headers:**
```
Authorization: Bearer <server_api_key>
```

**Response — PENDING/RUNNING:**
```json
{
  "job_id": "job_a1b2c3d4e5f6",
  "status": "RUNNING",
  "model": "claude-sonnet-4-5",
  "created_at": "2025-01-25T10:30:00Z",
  "started_at": "2025-01-25T10:30:01Z"
}
```

**Response — COMPLETED:**
```json
{
  "job_id": "job_a1b2c3d4e5f6",
  "status": "COMPLETED",
  "model": "claude-sonnet-4-5",
  "created_at": "2025-01-25T10:30:00Z",
  "started_at": "2025-01-25T10:30:01Z",
  "completed_at": "2025-01-25T10:35:42Z",
  "duration_ms": 341000,
  "cost_usd": 0.23,
  "output": {
    "text": "Analysis complete. I found 3 potential SQL injection vulnerabilities...",
    "files": {
      "vulnerabilities.json": "eyJ2dWxuZXJhYmlsaXRpZXMiOiBbLi4uXX0="
    }
  }
}
```

**Response — FAILED:**
```json
{
  "job_id": "job_a1b2c3d4e5f6",
  "status": "FAILED",
  "error": "API rate limit exceeded",
  "output": {
    "text": "Partial analysis completed before error...",
    "files": {}
  }
}
```

---

## Job Status Values

| Status | Description |
|--------|-------------|
| `PENDING` | Job created, waiting to start |
| `RUNNING` | Claude agent is executing |
| `COMPLETED` | Job finished successfully |
| `FAILED` | Job encountered an error |
| `TIMEOUT` | Job exceeded time limit |

---

## Admin API

All `/v1/admin/*` endpoints require authentication with an **admin** role client. See [Client Management](client-management.md) for how to create admin users.

### Client Management

#### POST /v1/admin/clients

Create a new API client.

**Request:**
```json
{
  "client_id": "my-client",
  "description": "Optional description",
  "role": "client",
  "security_profile": "common"
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `client_id` | Yes | Unique identifier (alphanumeric, hyphens, underscores) |
| `description` | No | Human-readable description |
| `role` | No | `"admin"` or `"client"` (default: `"client"`) |
| `security_profile` | No | Security profile name (default: `"common"`). Must reference an existing profile. |

**Response (201 Created):**
```json
{
  "client_id": "my-client",
  "api_key": "ccas_abc123...",
  "role": "client"
}
```

The `api_key` is shown only once. Store it securely.

---

#### GET /v1/admin/clients

List all clients.

**Response:**
```json
[
  {
    "client_id": "my-admin",
    "description": "Primary admin",
    "created_at": "2025-01-25T10:00:00Z",
    "active": true,
    "role": "admin",
    "security_profile": "unconfined"
  },
  {
    "client_id": "scanner",
    "description": "Security scanner",
    "created_at": "2025-01-25T11:00:00Z",
    "active": true,
    "role": "client",
    "security_profile": "common"
  }
]
```

---

#### GET /v1/admin/clients/{client_id}

Get a specific client.

**Response:** Same format as list item.

---

#### PATCH /v1/admin/clients/{client_id}

Update a client's description, role, or security profile.

**Request:**
```json
{
  "description": "New description",
  "role": "admin",
  "security_profile": "restrictive"
}
```

All fields are optional. `security_profile` must reference an existing profile.

**Response:** Updated client object.

---

#### DELETE /v1/admin/clients/{client_id}

Delete a client permanently.

**Response:** `204 No Content`

**Errors:**
- `400` — Cannot delete yourself or the last admin

---

#### POST /v1/admin/clients/{client_id}/activate

Reactivate a deactivated client.

**Response:** Updated client object.

---

#### POST /v1/admin/clients/{client_id}/deactivate

Deactivate a client (soft delete).

**Response:** Updated client object.

**Errors:**
- `400` — Cannot deactivate yourself or the last admin

---

### Security Profile Management

Security profiles define tool restrictions, MCP server access, and network policy for client jobs. See [Security — Security Profiles](security-model.md#security-profiles) for details.

#### POST /v1/admin/security-profiles

Create a new security profile.

**Request:**
```json
{
  "name": "internal-only",
  "description": "Access to internal APIs only",
  "denied_tools": ["WebSearch"],
  "allowed_mcp_servers": ["sequential-thinking"],
  "network": {
    "allowed_domains": ["api.anthropic.com", "*.internal.corp.com"],
    "denied_ip_ranges": ["169.254.0.0/16"],
    "allow_ip_destination": false
  }
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Lowercase alphanumeric with hyphens (`^[a-z0-9-]+$`), max 100 chars |
| `description` | No | Human-readable description |
| `denied_tools` | No | Tools to deny (e.g., `["Bash", "WebFetch"]`) |
| `allowed_mcp_servers` | No | MCP servers to allow (`null` = all, `[]` = none) |
| `network` | No | Network policy (see below) |

**Network policy fields:**

| Field | Default | Description |
|-------|---------|-------------|
| `allowed_domains` | `null` | Allowed DNS domains (`null` = any, `[]` = none). Supports `*.example.com` wildcards. |
| `denied_domains` | `[]` | Denied DNS domains (checked first, overrides allowed) |
| `allowed_ip_ranges` | `null` | Allowed IP ranges in CIDR (`null` = any) |
| `denied_ip_ranges` | `[]` | Denied IP ranges in CIDR |
| `allow_ip_destination` | `false` | Whether raw IP destinations (not DNS names) are permitted |

**Response:** `201 Created` with full profile object.

**Errors:**
- `409` — Profile with this name already exists
- `422` — Invalid name, domain pattern, or CIDR

---

#### GET /v1/admin/security-profiles

List all security profiles (built-in and custom).

**Response:**
```json
[
  {
    "name": "common",
    "description": "Balanced security...",
    "network": { ... },
    "denied_tools": [],
    "allowed_mcp_servers": null,
    "is_builtin": true,
    "is_default": true,
    "created_at": "2026-02-01T00:00:00",
    "updated_at": "2026-02-01T00:00:00"
  }
]
```

---

#### GET /v1/admin/security-profiles/{name}

Get a specific security profile.

**Response:** Full profile object. `404` if not found.

---

#### PATCH /v1/admin/security-profiles/{name}

Update a security profile. Only provided fields are changed.

**Request:**
```json
{
  "description": "Updated description",
  "denied_tools": ["Bash", "WebFetch"]
}
```

**Response:** `200 OK` with updated profile. `404` if not found. `422` for invalid values.

---

#### DELETE /v1/admin/security-profiles/{name}

Delete a custom security profile.

**Response:** `204 No Content`

**Errors:**
- `400` — Cannot delete built-in profiles
- `404` — Profile not found
- `409` — Profile is assigned to one or more clients

---

#### POST /v1/admin/security-profiles/{name}/set-default

Set a profile as the server-wide default for new clients.

**Response:** `200 OK` with the updated profile object. `404` if not found.

---

### MCP Server Management

#### POST /v1/admin/mcp

Add an MCP server with manual configuration.

> **Note:** Manual stdio servers require the command/script to already exist on the server filesystem (via Docker image, volume mount, or system package). For installing MCP servers from package registries, use `POST /v1/admin/mcp/install` instead.

**Request (stdio server):**
```json
{
  "name": "my-server",
  "type": "stdio",
  "command": "node",
  "args": ["/path/to/server.js"],
  "env": {"KEY": "value"},
  "description": "My MCP server"
}
```

**Request (HTTP/SSE server):**
```json
{
  "name": "remote-mcp",
  "type": "http",
  "url": "https://example.com/mcp",
  "headers": {"Authorization": "Bearer ${MCP_TOKEN}"},
  "description": "Remote MCP server"
}
```

**Response (201 Created):**
```json
{
  "name": "my-server",
  "type": "stdio",
  "description": "My MCP server",
  "package_manager": null,
  "package": null,
  "added_at": "2025-01-25T12:00:00Z"
}
```

---

#### POST /v1/admin/mcp/install

Install an MCP server from npm or pip.

**Request:**
```json
{
  "package": "@anthropic/mcp-server-memory",
  "name": "memory",
  "description": "Memory MCP server",
  "pip": false
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `package` | Yes | Package name (npm) or `pip://package` |
| `name` | No | Server name (auto-derived if omitted) |
| `description` | No | Description |
| `pip` | No | Force pip installation (default: auto-detect) |

**Response:** Same as POST /v1/admin/mcp.

---

#### GET /v1/admin/mcp

List all MCP servers.

**Response:**
```json
[
  {
    "name": "memory",
    "type": "stdio",
    "description": "Memory MCP server",
    "package_manager": "npm",
    "package": "@anthropic/mcp-server-memory",
    "added_at": "2025-01-25T12:00:00Z"
  }
]
```

---

#### GET /v1/admin/mcp/{name}

Get a specific MCP server.

---

#### DELETE /v1/admin/mcp/{name}

Remove an MCP server.

**Query Parameters:**
- `keep_package=true` — Don't uninstall the npm/pip package

**Response:** `204 No Content`

---

#### POST /v1/admin/mcp/health-check

Health check all MCP servers.

**Response:**
```json
[
  {"name": "memory", "healthy": true, "detail": "ok"},
  {"name": "broken", "healthy": false, "detail": "Connection refused"}
]
```

---

#### POST /v1/admin/mcp/{name}/health-check

Health check a specific MCP server.

**Response:**
```json
{"name": "memory", "healthy": true, "detail": "ok"}
```

---

### Agent Management

Agents are subagent definitions (markdown files with YAML frontmatter).

#### POST /v1/admin/agents

Add a new agent.

**Request:**
```json
{
  "name": "vuln-scanner",
  "content": "---\nname: vuln-scanner\ndescription: Security scanner\ntools: Read, Grep, Bash\n---\n\nYou are a security scanner...",
  "description": "Security vulnerability scanner"
}
```

Or with base64-encoded content (supports gzip):
```json
{
  "name": "vuln-scanner",
  "content_base64": "LS0tCm5hbWU6...",
  "description": "Security vulnerability scanner"
}
```

**Response (201 Created):**
```json
{
  "name": "vuln-scanner",
  "description": "Security vulnerability scanner",
  "prompt_size_bytes": 1234,
  "added_at": "2025-01-25T12:00:00Z"
}
```

---

#### GET /v1/admin/agents

List all agents.

**Response:** Array of agent objects.

---

#### GET /v1/admin/agents/{name}

Get agent details including frontmatter and body preview.

**Response:**
```json
{
  "name": "vuln-scanner",
  "description": "Security vulnerability scanner",
  "prompt_size_bytes": 1234,
  "added_at": "2025-01-25T12:00:00Z",
  "frontmatter": {
    "name": "vuln-scanner",
    "description": "Security scanner",
    "tools": "Read, Grep, Bash"
  },
  "body_preview": "You are a security scanner..."
}
```

---

#### PUT /v1/admin/agents/{name}

Update an agent's content or description.

**Request:**
```json
{
  "content": "---\nname: vuln-scanner\n...",
  "description": "Updated description"
}
```

Both fields are optional. Use `content_base64` for binary-safe transfer.

**Response:** Updated agent object.

---

#### DELETE /v1/admin/agents/{name}

Remove an agent.

**Response:** `204 No Content`

---

### Skill Management

Skills are plugin-based definitions (SKILL.md files in directories).

#### POST /v1/admin/skills

Add a new skill.

**Request:**
```json
{
  "name": "code-review",
  "content": "---\nname: code-review\ndescription: Code reviewer\n---\n\nReview the code...",
  "description": "Code review skill"
}
```

**Response (201 Created):**
```json
{
  "name": "code-review",
  "description": "Code review skill",
  "skill_size_bytes": 567,
  "added_at": "2025-01-25T12:00:00Z"
}
```

---

#### GET /v1/admin/skills

List all skills.

---

#### GET /v1/admin/skills/{name}

Get skill details.

**Response:**
```json
{
  "name": "code-review",
  "description": "Code review skill",
  "skill_size_bytes": 567,
  "added_at": "2025-01-25T12:00:00Z",
  "frontmatter": {"name": "code-review", "description": "Code reviewer"},
  "body_preview": "Review the code..."
}
```

---

#### PUT /v1/admin/skills/{name}

Update a skill.

---

#### DELETE /v1/admin/skills/{name}

Remove a skill.

**Response:** `204 No Content`
