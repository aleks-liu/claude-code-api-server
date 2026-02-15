# Configuration

[← Back to README](../README.md)

All configuration is via environment variables with `CCAS_` prefix.

## Table of Contents

- [Environment Variables](#environment-variables)
- [Using .env File](#using-env-file)

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CCAS_HOST` | `0.0.0.0` | Host to bind server |
| `CCAS_PORT` | `8000` | Port to bind server |
| `CCAS_DEBUG` | `false` | Enable debug mode (verbose logging) |
| `CCAS_LOG_LEVEL` | `INFO` | Log level: DEBUG, INFO, WARNING, ERROR |
| `CCAS_DATA_DIR` | `/data` | Base directory for all data |
| `CCAS_MAX_UPLOAD_SIZE_MB` | `50` | Maximum upload size in MB |
| `CCAS_MAX_EXTRACTED_SIZE_MB` | `500` | Max extracted archive size in MB |
| `CCAS_MAX_FILES_PER_ARCHIVE` | `10000` | Max files in archive |
| `CCAS_MAX_CONCURRENT_JOBS` | `5` | Max simultaneous jobs |
| `CCAS_MAX_PENDING_JOBS` | `50` | Max queued jobs. New submissions rejected with HTTP 429 when reached |
| `CCAS_DEFAULT_MODEL` | `claude-sonnet-4-5` | Default Claude model when client does not specify one. Overridden per-job via the `model` field |
| `CCAS_RATE_LIMIT_RPM` | `100` | Rate limit in requests per minute. Set to `0` to disable |
| `CCAS_MAX_REQUEST_BODY_MB` | `10` | Max HTTP request body size in MB (non-upload endpoints). Uploads have their own limit |
| `CCAS_SETTING_SOURCES` | *(empty)* | Comma-separated Claude Code setting sources (e.g. `user`, `project`). Empty disables all |
| `CCAS_DEFAULT_JOB_TIMEOUT` | `1800` | Default timeout (30 min) |
| `CCAS_MAX_JOB_TIMEOUT` | `7200` | Maximum timeout (2 hours) |
| `CCAS_MIN_JOB_TIMEOUT` | `60` | Minimum timeout (1 min) |
| `CCAS_ENABLE_BWRAP_SANDBOX` | `true` | Enable process-level bwrap sandbox (see [Security — Sandbox Isolation](security-model.md#sandbox-isolation-bwrap)) |
| `CCAS_BWRAP_PATH` | `bwrap` | Path to the bwrap binary |
| `CCAS_BWRAP_ALLOW_UNSANDBOXED_FALLBACK` | `false` | If `true`, allow jobs to run without sandbox when bwrap is unavailable (fail-open). **Not recommended for production.** |
| `CCAS_SANDBOX_NETWORK_ENABLED` | `true` | Enable per-job network isolation (HTTP proxy + `--unshare-net`). When enabled, jobs with non-unconfined profiles run in an isolated network namespace with outbound traffic filtered by a per-job proxy. Set to `false` to disable all network isolation (debugging escape hatch). Requires bwrap and socat. |
| `CCAS_AUTOALLOWED_DOMAINS` | `api.anthropic.com,*.anthropic.com,claude.ai,*.claude.ai` | Comma-separated domains always allowed through the network proxy regardless of security profile. Enforced at the proxy level, not stored in profile data. Supports wildcard subdomains (`*.example.com`). |
| `CCAS_SECCOMP_DIR` | `/opt/ccas/seccomp` | Directory containing seccomp artifacts (`apply-seccomp` binary and `unix-block.bpf` filter) from `@anthropic-ai/sandbox-runtime`. In Docker this is a symlink to the npm package. If missing, the server falls back to `npm root -g` discovery. See [Security — seccomp BPF Hardening](security-model.md#seccomp-bpf-hardening). |
| `CCAS_UPSTREAM_HTTP_PROXY` | *(empty)* | Upstream proxy for plain HTTP requests from the per-job network proxy. Format: `http://[user:pass@]host:port` or `https://[user:pass@]host:port`. Only affects network-isolated profiles. See [Security — Upstream Proxy](security-model.md#upstream-proxy-support). |
| `CCAS_UPSTREAM_HTTPS_PROXY` | *(empty)* | Upstream proxy for HTTPS (CONNECT) requests from the per-job network proxy. Format: `http://[user:pass@]host:port` or `https://[user:pass@]host:port`. Only affects network-isolated profiles. See [Security — Upstream Proxy](security-model.md#upstream-proxy-support). |
| `CCAS_MCP_HEALTH_CHECK_TIMEOUT` | `15` | Timeout per MCP server health check (5–120 seconds) |
| `CCAS_UPLOAD_TTL_MINUTES` | `30` | Upload expiration time |
| `CCAS_JOB_INPUT_CLEANUP_DELAY_MINUTES` | `60` | Delay before deleting job inputs |
| `CCAS_CLEANUP_INTERVAL_MINUTES` | `15` | Cleanup task interval |

### Admin Bootstrap

| Variable | Default | Description |
|----------|---------|-------------|
| `CCAS_GENERATE_ADMIN_ON_FIRST_STARTUP` | `false` | Auto-generate admin user on first startup when no admin exists |
| `CCAS_ADMIN_TOKEN_ENCRYPTION_KEY` | *(empty)* | Base64-encoded RSA public key (PEM) for encrypting the auto-generated admin token. Required when `CCAS_GENERATE_ADMIN_ON_FIRST_STARTUP=true`. |

See [Client Management — First Admin Setup](client-management.md#first-admin-setup) for step-by-step instructions.

---

## Using .env File

```bash
# Copy example
cp .env.example .env

# Edit as needed
nano .env
```
