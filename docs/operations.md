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
2025-01-25T10:30:00Z [info    ] job_created    client_id=scanner job_id=job_abc123
2025-01-25T10:30:01Z [debug   ] tool_allowed   job_id=job_abc123 path=src/main.py tool=Read
2025-01-25T10:30:02Z [warning ] security_deny  job_id=job_abc123 path=/etc/passwd tool=Read
```

**Production Mode** (JSON):
```json
{"timestamp":"2025-01-25T10:30:00Z","level":"info","event":"job_created","job_id":"job_abc123","client_id":"scanner"}
```

**Key log events to monitor:**

| Event | Level | Description |
|-------|-------|-------------|
| `job_created` | info | New job submitted |
| `job_completed` | info | Job finished successfully |
| `proxy_connection_denied` | warning | Network proxy blocked a connection (policy violation) |
| `proxy_connection_error` | warning | Network proxy failed to connect to destination |
| `proxy_upstream_unreachable` | warning | Cannot reach the configured upstream proxy |
| `tool_denied_by_profile` | warning | Security profile blocked a tool call |
| `process_sandbox_failed` | error | bwrap sandbox creation failed |

---

## Health Monitoring

```bash
# Health check endpoint
curl http://localhost:8000/v1/health

# Use with monitoring tools
curl -f http://localhost:8000/v1/health || alert "Server down"
```

---

## Cleanup Behavior

Background cleanup runs every 15 minutes (configurable):

1. **Expired Uploads**: Deleted after 30 minutes if unused
2. **Job Inputs**: Deleted 60 minutes after job completion
3. **Job Outputs**: Kept forever (for audit trail)

---

## Backup

Important data to backup:
```bash
# Client credentials
/data/auth/clients.json

# MCP server configuration
/data/mcp/servers.json

# Job results (if needed for audit)
/data/jobs/*/status.json
/data/jobs/*/output/
```
