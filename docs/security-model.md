# Security

[← Back to README](../README.md)

## Table of Contents

- [Security Architecture](#security-architecture)
- [Security Profiles](#security-profiles)
  - [Built-in Profiles](#built-in-profiles)
  - [Tool Restrictions](#tool-restrictions)
  - [MCP Server Scoping](#mcp-server-scoping)
  - [Client-Profile Binding](#client-profile-binding)
- [Role-Based Access Control](#role-based-access-control)
- [Sandbox Isolation (bwrap)](#sandbox-isolation-bwrap)
  - [Filesystem Layout Inside the Sandbox](#filesystem-layout-inside-the-sandbox)
  - [Network Isolation](#network-isolation)
  - [seccomp BPF Hardening](#seccomp-bpf-hardening)
  - [Fail-Closed Behavior](#fail-closed-behavior)
  - [How It Works](#how-it-works)
  - [Installing bwrap](#installing-bwrap)
- [Authentication Flow](#authentication-flow)
- [Output File Collection](#output-file-collection)
- [ZIP Archive Extraction](#zip-archive-extraction)
- [Claude Code Built-in Sandboxing](#claude-code-built-in-sandboxing)
- [Deployment Security Considerations](#deployment-security-considerations)
- [Best Practices](#best-practices)

---

## Security Architecture

```
HOST PROCESS (Python API server)
  │
  ├── Per-job HTTP proxy (sandbox_proxy.py)        ← Network filtering
  │     Listens on Unix socket, enforces NetworkPolicy
  │
  └── bwrap (creates isolated namespace)           ← Process-level sandbox
        │  --unshare-pid (PID isolation)
        │  --unshare-net (network isolation, when profile has restrictions)
        │
        ├── socat (bridges proxy.sock → TCP :3128)  ← Proxy bridge
        │
        ├── apply-seccomp (loads BPF filter)        ← Syscall filtering
        │     Blocks socket(), connect() for AF_UNIX/AF_INET
        │
        └── Claude Code CLI (Node.js)
              HTTP_PROXY=http://127.0.0.1:3128
```

**Defense layers:**

| Layer | Mechanism | Bypassed by LLM? |
|-------|-----------|-------------------|
| **Filesystem isolation** | bwrap namespace — read-only host, tmpfs over home/data, job-only write access | No (kernel enforced) |
| **Network isolation** | `--unshare-net` — no network interfaces inside sandbox | No (kernel enforced) |
| **Network filtering** | Per-job HTTP proxy — domain/IP allowlist per profile | No (only path to internet is through proxy) |
| **seccomp BPF** | `apply-seccomp` loads BPF filter blocking direct socket creation | No (kernel enforced, prevents proxy bypass) |
| **Tool policy** | `can_use_tool` callback — denied tools, MCP server scoping | Partially (Bash bypass in acceptEdits mode) |

**Process-level bwrap sandbox** (`sandbox.py`): wraps the entire Claude CLI process in a bubblewrap namespace. Every child process — Bash commands, file operations, sub-agents — inherits the restricted filesystem and network namespace. This layer cannot be bypassed by the LLM. Custom subagents (see [Subagent Management](subagents.md)) are passed via the SDK's `agents` parameter and execute within this same sandbox — all their tool invocations are fully isolated.

**Per-job network proxy** (`sandbox_proxy.py`): for profiles with network restrictions, a Python asyncio HTTP CONNECT proxy runs on a Unix socket in the job directory. The proxy evaluates each outbound connection against the profile's `NetworkPolicy` (domain allowlists, IP range denylists). Mandatory Anthropic API domains are always allowed regardless of profile. The proxy runs as coroutines in the API server's event loop — no separate process.

---

## Security Profiles

Security profiles are the primary mechanism for configuring per-client security policies. Each profile defines tool restrictions, MCP server access, and network policy. Profiles are managed via the Admin API (`/v1/admin/security-profiles`) and assigned to clients.

Every client is bound to exactly one security profile. The profile is resolved at job start and applied for the duration of that job (changes to profiles take effect on new jobs only).

### Built-in Profiles

Three built-in profiles are created on first startup:

| Profile | Tool restrictions | MCP servers | Network | Description |
|---------|------------------|-------------|---------|-------------|
| `unconfined` | None | All | Unrestricted (no proxy, no `--unshare-net`) | No restrictions. For trusted clients or development. |
| `common` (default) | None | All | Any internet domain; private IPs blocked | Balanced security. Proxy filters outbound connections; private networks (10.x, 172.16.x, 192.168.x, localhost) denied. |
| `restrictive` | WebFetch, WebSearch denied | None (`[]`) | Anthropic API only | Maximum security. Only `*.anthropic.com` reachable. |

Built-in profiles cannot be deleted but can be modified by admins. Custom profiles can be created for specific use cases.

### Tool Restrictions

Profiles specify a `denied_tools` list. When a job attempts to use a denied tool, the `can_use_tool` callback returns a deny response with a message identifying the profile. Tool denial is enforced via the Claude Agent SDK's permission callback.

**Known limitation**: In `acceptEdits` mode, Claude Code may auto-accept Bash commands without calling `can_use_tool`. Denying `Bash` in `denied_tools` is a best-effort control, not a hard boundary. Real Bash restriction requires OS-level enforcement.

### MCP Server Scoping

Profiles specify `allowed_mcp_servers`:
- `null` — all configured MCP servers are available (default)
- `[]` — no MCP servers available
- `["server-a", "server-b"]` — only listed servers available

MCP server filtering is applied in two places:
1. **SDK configuration**: MCP servers not in the allowed list are excluded from the SDK options before job execution
2. **can_use_tool callback**: MCP tool calls (`mcp__<server>__<tool>`) are denied if the server is not in the allowed list

### Client-Profile Binding

Every client has a `security_profile` field (default: `"common"`). The profile is assigned at client creation and can be updated via `PATCH /v1/admin/clients/{client_id}`. The specified profile must exist — the server validates this on create and update.

Existing clients that predate security profiles are automatically migrated to the `"common"` profile on first load.

---

## Role-Based Access Control

The server implements role-based access control (RBAC) for API authentication:

| Role | Endpoints | Description |
|------|-----------|-------------|
| `client` | `/v1/uploads`, `/v1/jobs/*` | Standard API client for running jobs |
| `admin` | All endpoints + `/v1/admin/*` | Full server administration (clients, MCP servers, security profiles, agents, skills) |

**Key behaviors:**
- Admin endpoints (`/v1/admin/*`) return `403 Forbidden` for non-admin clients
- Admins cannot delete or deactivate themselves (prevents lockout)
- The last remaining admin cannot be deleted or demoted (prevents lockout)
- Both roles use the same API key format (`ccas_...`) and authentication flow

**Admin bootstrap security:**
- When using `CCAS_GENERATE_ADMIN_ON_FIRST_STARTUP`, the API key is encrypted with RSA OAEP (SHA-256) before logging
- The plaintext key never appears in logs or environment variables
- Only the holder of the corresponding private key can decrypt the token

See [Client Management](client-management.md) for creating admin users.

---

## Sandbox Isolation (bwrap)

The process-level sandbox is the primary security control. It uses [bubblewrap (bwrap)](https://github.com/containers/bubblewrap) to run the Claude Code CLI inside an isolated Linux namespace.


### Filesystem Layout Inside the Sandbox

| Path | Mount type | Access | Purpose |
|------|-----------|--------|---------|
| `/` | `ro-bind` | Read-only | System tools, Node.js, Claude CLI all accessible |
| `/home/<user>` | `tmpfs` | Empty (writable tmpfs) | User data hidden from the job |
| `/root` | `tmpfs` | Empty (writable tmpfs) | Root home hidden |
| `/tmp` | `tmpfs` | Fresh writable | Scratch space for the job |
| `<data_dir>` | `tmpfs` | Empty (writable tmpfs) | **All other jobs hidden** (cross-job isolation) |
| `<data_dir>/mcp/npm/node_modules` | `ro-bind` | Read-only | npm-installed MCP server packages (if present) |
| `<data_dir>/mcp/venv` | `ro-bind` | Read-only | pip-installed MCP server packages (if present) |
| `<input_dir>` | `bind` | **Read-write** | Job workspace — the only real writable directory |
| `/dev` | `devtmpfs` | Standard | Device files |
| `/proc` | `procfs` | Standard | Process information |

**Key properties:**

- The job can only write to its own `input/` directory
- Other jobs' data is invisible (the entire `data_dir` is an empty tmpfs with only the current job's `input_dir` bind-mounted back in)
- The user's home directory is empty — no access to SSH keys, shell history, credentials, or other user data
- The host filesystem is read-only — system tools work but cannot be modified
- PID namespace is isolated (`--unshare-pid`) — the job cannot see host processes

### Network Isolation

When a security profile has network restrictions (any profile other than `unconfined`), the sandbox adds OS-level network isolation:

1. **`--unshare-net`**: The bwrap sandbox creates an isolated network namespace with no network interfaces
2. **Per-job HTTP proxy**: A Python asyncio proxy listens on a Unix domain socket (`proxy.sock` in the job directory) and enforces the profile's `NetworkPolicy`
3. **socat bridge**: Inside the sandbox, socat bridges the Unix socket to TCP `127.0.0.1:3128`, and `HTTP_PROXY`/`HTTPS_PROXY` environment variables are set

**Filtering flow** (evaluated per outbound connection):

1. Mandatory domains (`*.anthropic.com`) are always allowed — hardcoded, not configurable
2. Raw IP destinations checked against `allow_ip_destination`
3. `denied_domains` checked (overrides allowed)
4. `allowed_domains` checked (null = any, empty = none)
5. DNS resolved on the host side
6. `denied_ip_ranges` checked against resolved IP
7. `allowed_ip_ranges` checked against resolved IP

**For the `unconfined` profile**: No proxy is started, no `--unshare-net` is added. The job has the same unrestricted network access as the host process.

**Fail-closed**: If the proxy fails to start, the job is refused. There is no fallback to unrestricted network access.

**Kill switch**: Set `CCAS_SANDBOX_NETWORK_ENABLED=false` to disable all network isolation (for debugging or environments where `--unshare-net` doesn't work). When disabled, jobs with network-restricted profiles log a warning but run without network filtering.

**Dependencies**: Network isolation requires `socat` to be installed (included in the Docker image).

### seccomp BPF Hardening

Network isolation via `--unshare-net` + HTTP proxy forces all traffic through the proxy, but a process inside the sandbox could theoretically bypass the proxy by creating raw sockets directly (e.g., `socket(AF_INET, SOCK_STREAM, 0)` + `connect()`). seccomp BPF closes this gap.

**How it works**: The `apply-seccomp` binary (from `@anthropic-ai/sandbox-runtime` npm package) loads a pre-compiled BPF filter via `prctl(PR_SET_SECCOMP)`, then execs the Claude CLI. The filter blocks `socket()` and related syscalls for `AF_UNIX` and `AF_INET` families. All child processes inherit the filter — it cannot be removed once applied.

**Execution chain** (network-isolated jobs):
```
bwrap → inner_script.sh → socat (backgrounded) → apply-seccomp unix-block.bpf /path/to/cli.js
```
socat starts *before* seccomp is applied (it needs sockets). The seccomp filter only covers the CLI process and its children.

**Source**: The `apply-seccomp` binary and `unix-block.bpf` filter are from Anthropic's [`@anthropic-ai/sandbox-runtime`](https://www.npmjs.com/package/@anthropic-ai/sandbox-runtime) npm package (MIT licensed) — the same artifacts Claude Code uses internally for its own sandboxing.

**Discovery**: In Docker, a symlink at `/opt/ccas/seccomp` points to the npm package. Outside Docker, the server auto-discovers the package via `npm root -g`. Override with `CCAS_SECCOMP_DIR`.

**Degraded mode**: If seccomp binaries are not available, jobs still run with proxy-based network filtering but without syscall-level enforcement. The server logs `seccomp_not_available` at startup.

### Fail-Closed Behavior

By default, if the bwrap sandbox cannot be created (bwrap not installed, kernel namespace support missing, etc.), the **job is refused entirely**. This prevents accidental execution without isolation.

To allow unsandboxed execution (development only):

```bash
export CCAS_BWRAP_ALLOW_UNSANDBOXED_FALLBACK=true
```

### How It Works

For each job, the server:

1. Resolves the client's security profile (snapshot for the job's duration)
2. If the profile has network restrictions: starts a per-job HTTP proxy on a Unix socket
3. Generates wrapper shell scripts in the job directory
4. The SDK's `cli_path` option is set to the outer wrapper — the SDK thinks it's talking to Claude directly
5. stdin/stdout/stderr pass through transparently
6. On job completion: stops the proxy, cleans up wrapper scripts

**Without network isolation** (unconfined profile):
```
SDK spawns → sandbox_wrapper.sh → exec bwrap [...] -- /real/path/to/claude "$@"
```

**With network isolation** (common, restrictive, or custom profiles):
```
SDK spawns → sandbox_wrapper.sh → exec bwrap [--unshare-net ...] -- sandbox_inner.sh "$@"
  sandbox_inner.sh:
    1. socat bridges proxy.sock → TCP 127.0.0.1:3128
    2. Sets HTTP_PROXY/HTTPS_PROXY
    3. exec apply-seccomp unix-block.bpf claude "$@"   (if seccomp available)
       exec claude "$@"                                 (if seccomp unavailable)
```

### Installing bwrap and socat

```bash
# Debian/Ubuntu
sudo apt-get install bubblewrap socat

# RHEL/Fedora
sudo dnf install bubblewrap socat

# Verify
bwrap --version
socat -V | head -1
```

bwrap requires Linux kernel namespace support (user namespaces). On WSL2, this works out of the box. Some hardened kernels or container runtimes may need configuration — the server runs a smoke test at startup and reports any issues.

socat is required for network isolation — it bridges the proxy Unix socket to a TCP port inside the sandbox. Without socat, network isolation cannot function (jobs will fail if their profile has network restrictions). The Docker image includes socat.

---

## Authentication Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                      Client Request                             │
│  Authorization: Bearer ccas_abc123...                           │
│  X-Anthropic-Key: sk-ant-xyz789...                              │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  1. Extract Bearer token from Authorization header              │
│  2. Hash token with Argon2                                      │
│  3. Compare against stored hashes in clients.json               │
│  4. If match found and client active → authenticated            │
│  5. X-Anthropic-Key passed to Claude SDK (not stored)           │
└─────────────────────────────────────────────────────────────────┘
```

---

## Output File Collection

Claude Code operates inside the `input/` directory (its working directory). The server uses a deterministic, hash-based mechanism to detect files Claude creates or modifies — no reliance on LLM instructions:

1. **Before execution**: SHA-256 hash of every file in `input/` is recorded
2. **After execution**: All files are hashed again and compared against the snapshot
3. **New files** (not in snapshot) and **modified files** (hash differs) are copied to `output/`
4. Files excluded from tracking: `.claude/`, `__pycache__/`, `.git/`, `node_modules/`

This runs regardless of job outcome (success, failure, timeout) to capture partial results.

Clients do **not** need to instruct Claude where to save files. Any file Claude creates or modifies in its working directory is automatically collected and returned in the job response.

---

## ZIP Archive Extraction

When a ZIP archive contains a single root directory (common with GitHub downloads like `project-main/`), the server strips that wrapper so files are extracted directly into `input/`. For example:

- Archive: `my-project-main/src/app.py` → Extracts to: `input/src/app.py`
- Archive: `src/app.py`, `README.md` → Extracts to: `input/src/app.py`, `input/README.md`

---

## Tool Policy Enforcement

The `can_use_tool` callback enforces security profile policies at the SDK level:

1. **Denied tools** — tools listed in the profile's `denied_tools` are blocked with a descriptive message
2. **MCP server access** — MCP tool calls (`mcp__<server>__<tool>`) are blocked if the server is not in the profile's `allowed_mcp_servers`

Filesystem isolation is handled entirely by bwrap at the OS level — there is no application-level path validation. This is intentional: path validation in `can_use_tool` can be bypassed via Bash commands in `acceptEdits` mode and provides false security. The bwrap sandbox is the real filesystem boundary.

---

## Claude Code Built-in Sandboxing

Claude Code has its own [built-in sandboxing](https://code.claude.com/docs/en/sandboxing) that provides per-command filesystem and network isolation. This is **not currently enabled** in Claude Code API Server. Enabling it requires interactive configuration (approving network domains, confirming sandbox exceptions) which is not compatible with headless API execution — an enabled sandbox without the ability to confirm actions would likely block many legitimate commands (package installs, git operations, API calls, etc.). This is a potential feature for the future.

---

## Deployment Security Considerations

At its core, **Claude Code API Server** runs arbitrary tasks through Claude Code. You give it a job — Claude Code decides what commands to run and executes them. This is no different from CI/CD runners, Ansible, Jenkins, or any other tool that turns instructions into actions on a host. The security implications are the same: **it matters who can submit those instructions**.

**What the sandbox covers and what it doesn't:**

The bwrap sandbox isolates jobs from each other at the filesystem level — one job cannot access another job's data, and the host filesystem is read-only. Network access is controlled by the client's security profile: the `unconfined` profile allows unrestricted network access, while other profiles enforce domain and IP filtering through a per-job HTTP proxy with `--unshare-net` namespace isolation. The `common` profile (default) allows any public internet domain but blocks private networks. The `restrictive` profile only allows `*.anthropic.com`.

**Why Claude Code's built-in safety guardrails are not enough:**

Anthropic trains Claude to refuse clearly malicious requests, but these guardrails are a best-effort behavioral layer, not a security boundary. A determined attacker with access to a valid API key can craft prompts that get Claude Code to execute arbitrary network requests, download scripts, or interact with internal services. Relying on model-level refusals as your security control is not a viable strategy.

**The key deployment principle:**

Think about what networks **Claude Code API Server** can reach, and who can talk to it.

If every client that has access to **Claude Code API Server** already has network access to the same services it can reach — it does not expand your attack surface. For example: if your n8n automation talks to **Claude Code API Server**, and n8n already has direct access to GitLab, a compromised n8n can attack GitLab with or without **Claude Code API Server** in the picture.

The risk appears when **Claude Code API Server** has **broader network access** than its clients. In that scenario, a compromised API key turns it into a pivot point — an attacker can use it to reach services they couldn't access directly.

**Practical recommendations:**

- Following standard security practices, restrict network access to and from **Claude Code API Server** as tightly as possible.
- Treat API keys like credentials to an execution engine — because that's what they are. Rotate them, scope them per integration, revoke unused ones.
- Review job logs periodically for unexpected activity.

---

## Best Practices

1. **Keep bwrap enabled**: The process-level sandbox is the primary security boundary — do not disable it in production
2. **Use appropriate security profiles**: Assign `restrictive` or `common` profiles to untrusted clients. Reserve `unconfined` for trusted development use only
3. **Run in Container**: Provides an additional isolation layer on top of bwrap
4. **Keep network isolation enabled**: The `CCAS_SANDBOX_NETWORK_ENABLED` kill switch is for debugging only — keep it `true` in production
5. **Rotate Keys**: Periodically rotate client API keys
6. **Monitor Logs**: Watch for `proxy_connection_denied`, `tool_denied_by_profile`, and `process_sandbox_failed` events
7. **Limit Concurrency**: Prevent resource exhaustion
