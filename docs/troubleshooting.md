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
Cause: Max concurrent jobs reached
Fix: Wait for other jobs to complete, or increase CCAS_MAX_CONCURRENT_JOBS
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

### "Access denied: file path outside allowed directory"

```
Cause: Claude tried to access a file outside the job's input directory
Status: Working as intended (security feature)
Note: With bwrap sandbox enabled, the file is also invisible at the filesystem level
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
Fix: Check /v1/health endpoint for mcp_servers status.
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
