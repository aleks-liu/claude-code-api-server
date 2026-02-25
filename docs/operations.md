# Operations

[← Back to README](../README.md)

## Table of Contents

- [Logging](#logging)
- [Health Monitoring](#health-monitoring)
- [Cleanup Behavior](#cleanup-behavior)
- [Backup](#backup)

---

## Logging

Logs are written to stdout in structured format:

**Debug Mode** (human-readable):
```
2025-01-25T10:30:00Z [info    ] job_created    client_id=scanner job_id=job_abc123 model=claude-sonnet-4-6 timeout=1800
2025-01-25T10:30:01Z [debug   ] tool_allowed   job_id=job_abc123 tool=Read decision=allow
2025-01-25T10:30:02Z [warning ] tool_denied_by_profile job_id=job_abc123 tool=WebFetch profile=restrictive decision=deny
```

**Production Mode** (JSON):
```json
{"timestamp":"2025-01-25T10:30:00Z","level":"info","event":"job_created","job_id":"job_abc123","client_id":"scanner","model":"claude-sonnet-4-6","timeout":1800}
```

**Key log events to monitor:**

| Event | Level | Description |
|-------|-------|-------------|
| `job_created` | info | New job submitted |
| `job_started` | info | Job execution began |
| `job_completed` | info | Job finished successfully |
| `job_failed` | error | Job failed with an error |
| `job_timeout` | warning | Job exceeded its timeout limit |
| `proxy_connection_denied` | warning | Network proxy blocked a connection (policy violation) |
| `proxy_connection_error` | warning | Network proxy failed to connect to destination |
| `proxy_upstream_unreachable` | warning | Cannot reach the configured upstream proxy |
| `tool_denied_by_profile` | warning | Security profile blocked a tool call |
| `process_sandbox_failed_fallback` | warning | bwrap sandbox failed, running unsandboxed (if fallback enabled) |
| `process_sandbox_failed_aborting` | error | bwrap sandbox failed, job refused (fail-closed) |
| `orphaned_running_job` | warning | Job found in RUNNING state at startup (likely from a crash) |
| `cleanup_completed` | info | Background cleanup cycle finished |

---

## Health Monitoring

```bash
# Basic health check (returns {"status": "ok"})
curl http://localhost:8000/v1/health

# Detailed operational status (requires admin key) — includes active jobs, pending uploads, MCP server status
curl http://localhost:8000/v1/admin/status -H "Authorization: Bearer $ADMIN_TOKEN"

# Use with monitoring tools
curl -f http://localhost:8000/v1/health || alert "Server down"
```

**Resource limits:** The server enforces `CCAS_MAX_CONCURRENT_JOBS` (default 5) and `CCAS_MAX_PENDING_JOBS` (default 50). When the pending queue is full, new submissions are rejected with HTTP 429. See [Configuration](configuration.md) for tuning.

---

## Cleanup Behavior

Background cleanup runs at a configurable interval (`CCAS_CLEANUP_INTERVAL_MINUTES`, default 15):

1. **Expired Uploads**: Deleted after `CCAS_UPLOAD_TTL_MINUTES` (default 30) if unused
2. **Job Inputs**: Deleted `CCAS_JOB_INPUT_CLEANUP_DELAY_MINUTES` (default 60) after job completes, fails, or times out
3. **Job Outputs**: Kept forever (for audit trail)

See [Configuration](configuration.md) for all cleanup settings.

---

## Backup

Important data to backup:
```bash
# Client credentials
/data/auth/clients.json

# Security profiles
/data/sandbox/profiles.json

# MCP server configuration
/data/mcp/servers.json

# Subagent definitions
/data/agents/

# Skills
/data/skills-plugin/
/data/skills-meta/

# Job results (if needed for audit)
/data/jobs/*/status.json
/data/jobs/*/output/
```

See [Architecture — Directory Structure](architecture.md#directory-structure) for the full layout. See [Configuration](configuration.md), [Security](security-model.md), and [Troubleshooting](troubleshooting.md) for related operational guidance.
