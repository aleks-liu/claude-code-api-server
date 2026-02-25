# Troubleshooting

[← Back to README](../README.md)

## Table of Contents

- [Common Issues](#common-issues)
- [Debug Mode](#debug-mode)
- [Inspecting Job Directory](#inspecting-job-directory)

---

## Common Issues

### "Upload not found or expired"

```
Cause: Upload expired before job was created (default: 30 minutes)
Fix: Create job immediately after upload, or increase CCAS_UPLOAD_TTL_MINUTES
```

### "Claude Agent SDK not available"

```
Cause: SDK not installed or Node.js missing
Fix: pip install claude-agent-sdk
     Ensure Node.js 18+ is installed
```

### Job stuck in PENDING

```
Cause: Max concurrent jobs reached (CCAS_MAX_CONCURRENT_JOBS)
Fix: Wait for other jobs to complete, or increase CCAS_MAX_CONCURRENT_JOBS
Note: If the pending queue is full (CCAS_MAX_PENDING_JOBS), new submissions
      are rejected with HTTP 429 instead of queuing
```

### "Sandbox creation failed (fail-closed)"

```
Cause: bwrap is not installed or not functional, and unsandboxed fallback is disabled (default)
Fix: Install bubblewrap: sudo apt-get install bubblewrap
     Or for development: export CCAS_BWRAP_ALLOW_UNSANDBOXED_FALLBACK=true
```

### "bwrap smoke test failed"

```
Cause: bwrap is installed but cannot create namespaces (kernel config, AppArmor, or container restrictions)
Fix: Ensure user namespaces are enabled: sysctl kernel.unprivileged_userns_clone=1
     In Docker: run with --privileged or --security-opt apparmor=unconfined
```

### "Claude Code CLI binary not found"

```
Cause: The sandbox wrapper cannot locate the 'claude' binary
Fix: Ensure Claude Code is installed: npm install -g @anthropic-ai/claude-code
     Verify: which claude
```

### Files outside the job directory are inaccessible

```
Cause: The bwrap sandbox restricts filesystem visibility — files outside the job's
       input directory are invisible (not just access-denied)
Status: Working as intended (security feature)
Note: The sandbox uses OS-level namespace isolation, not application-level path checks
```

### "execvp failed: No such file or directory" in job logs

```
Cause: A binary referenced in the sandbox wrapper script cannot be found.
       Common causes: Node.js not installed, Claude CLI not installed,
       or apply-seccomp binary missing.
Fix: Verify Claude CLI: claude --version
     Verify Node.js: node --version
     Check seccomp availability in startup logs (look for seccomp_available or seccomp_not_available)
```

### "seccomp BPF hardening is NOT available" at startup

```
Cause: The apply-seccomp binary or BPF filter was not found.
       Jobs still run with proxy-based network filtering but without
       syscall-level socket blocking (degraded security).
Fix: Ensure @anthropic-ai/sandbox-runtime is installed: npm list -g @anthropic-ai/sandbox-runtime
     Check symlink: ls -la /opt/ccas/seccomp
     Or set CCAS_SECCOMP_DIR to the correct path.
```

### "Cannot connect to upstream proxy: Name or service not known"

```
Cause: The CCAS_UPSTREAM_HTTPS_PROXY hostname is not resolvable from inside the Docker container.
       Common when using host.docker.internal on Linux Docker without extra_hosts configuration.
Fix: Add extra_hosts to docker-compose.yml:
       extra_hosts:
         - "host.docker.internal:host-gateway"
     Or use the host's IP address directly instead of host.docker.internal.
```

### "Cannot connect to upstream proxy: Connection refused"

```
Cause: The upstream proxy is not running or not reachable on the configured host:port.
Fix: Verify the upstream proxy is running and listening on the correct port.
     Check that CCAS_UPSTREAM_HTTPS_PROXY and/or CCAS_UPSTREAM_HTTP_PROXY are set correctly.
     For Docker: ensure the proxy host is reachable from the container network.
```

### "Upstream proxy returned 407 Proxy Authentication Required"

```
Cause: The upstream proxy requires authentication but credentials are missing or incorrect.
Fix: Include credentials in the proxy URL: http://user:pass@proxy:3128
     Verify the username and password are correct.
     Check proxy logs for more details on the authentication failure.
```

### Zombie `<defunct>` bwrap processes accumulating

```
Cause: Container was built without tini, so orphaned bwrap grandchildren are never reaped.
Fix: Rebuild the image — tini is included in the Dockerfile ENTRYPOINT since the fix.
     Verify: docker exec claude-code-api ps -p 1 -o comm=  → should show "tini"
     See: docs/architecture.md § Container Process Model (tini)
```

### Rate limit errors from Anthropic

```
Cause: Too many concurrent API requests
Fix: Reduce CCAS_MAX_CONCURRENT_JOBS or use different Anthropic keys
```

### "MCP health check failed for: [...]"

```
Cause: One or more configured MCP servers failed their health check at startup.
       The server starts in degraded mode — failed servers are removed from the
       active configuration and excluded from jobs.
Fix: Use the admin API to check health: POST /v1/admin/mcp/health-check
     Fix the server configuration or remove it: DELETE /v1/admin/mcp/<name>
```

### MCP server "command not found" during health check

```
Cause: The MCP server binary is not installed or not in PATH.
Fix: For npm packages, verify installation: ls /data/mcp/npm/node_modules/<package>/
     For pip packages, verify installation: /data/mcp/venv/bin/pip list
     Reinstall via admin API: DELETE /v1/admin/mcp/<name>, then POST /v1/admin/mcp/install
```

### MCP tools not appearing in job results

```
Cause: MCP servers may have failed health check and been excluded (degraded mode),
       or the server configuration is missing.
Fix: Check admin status: GET /v1/admin/status (shows MCP server health).
     List servers via admin API: GET /v1/admin/mcp
     Run health check: POST /v1/admin/mcp/health-check
```

---

## Debug Mode

Enable verbose logging:

```bash
export CCAS_DEBUG=true
export CCAS_LOG_LEVEL=DEBUG
```

---

## Inspecting Job Directory

```bash
# Inspect job state
cat /data/jobs/job_xxx/status.json | jq .

# Check Claude's output
cat /data/jobs/job_xxx/stdout.txt

# List output files
ls -la /data/jobs/job_xxx/output/
```
