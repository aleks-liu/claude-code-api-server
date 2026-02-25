# Subagent Management

[← Back to README](../README.md)

Subagents extend Claude Code with specialized task-handling agents. Each subagent has its own system prompt, description, allowed tools, and optional model override. Subagents are defined as Markdown files with YAML frontmatter, managed via the Admin API, and automatically made available to every job.

## Table of Contents

- [How Subagents Work](#how-subagents-work)
- [Agent File Format](#agent-file-format)
- [Add an Agent](#add-an-agent)
- [List Agents](#list-agents)
- [Show Agent Details](#show-agent-details)
- [Update an Agent](#update-an-agent)
- [Remove an Agent](#remove-an-agent)
- [Storage Layout](#storage-layout)
- [Sandbox Integration](#sandbox-integration)

---

## How Subagents Work

When Claude Code executes a job, it has access to the `Task` tool which can spawn specialized subagents. Custom subagents let you define purpose-built agents — for example, a vulnerability scanner, a code reviewer, or a PDF content extractor — each with tailored instructions.

The integration works as follows:

1. **Management**: Agent definition files (`.md` with YAML frontmatter) are stored at `/data/agents/prompts/` and managed via the Admin API.

2. **Plugin-dir delivery**: Agent `.md` files are synced to a shared plugin directory (`/data/skills-plugin/agents/`). On startup and on each add/update/delete, the server copies files from the prompts directory to the plugin directory.

3. **SDK integration**: At job execution time, the plugin directory is passed to the Claude Agent SDK via `ClaudeAgentOptions.plugins` (the `--plugin-dir` mechanism). Claude Code discovers agent `.md` files by scanning the plugin directory's `agents/` subdirectory.

4. **No restart needed**: Since the plugin directory is checked at each job execution, additions and removals via the Admin API take effect immediately for new jobs.

5. **Sandbox inheritance**: All subagents execute within the same bwrap process-level sandbox as the main agent. Every tool call — Bash, Read, Write, etc. — inherits the restricted filesystem namespace.

```
POST /v1/admin/agents ──► /data/agents/prompts/my-agent.md
                                       │
                        ┌──────────────┘
                        ▼
              Synced to /data/skills-plugin/agents/my-agent.md
                        │
                        ▼
              Job execution starts
              Plugin dir passed via --plugin-dir
                        │
                        ▼
              Claude Code discovers agents
              from the plugin directory
                        │
                        ▼
              Claude Code can now use Task tool
              to invoke "my-agent" subagent
```

---

## Agent File Format

Agent definitions use Markdown files with YAML frontmatter:

```markdown
---
name: vuln-scanner
description: Specialized agent for security vulnerability analysis
tools: Read, Grep, Glob, Bash, Write, Edit
model: sonnet
---

You are a specialized security vulnerability scanner.

## Your Task

Analyze the provided codebase for security vulnerabilities including:
- SQL injection
- Cross-site scripting (XSS)
- Authentication bypass
- Insecure deserialization

## Output Format

For each vulnerability found, report:
1. File and line number
2. Vulnerability type
3. Severity (Critical/High/Medium/Low)
4. Recommended fix
```

### Frontmatter Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Unique identifier. Letters, digits, and hyphens only; must start with a letter. |
| `description` | Yes | Short description shown to Claude when selecting agents via the `Task` tool. |
| `tools` | No | Comma-separated list or YAML list of allowed tools. If omitted, the agent inherits default tools. Examples: `Read, Grep, Bash` or `[Read, Grep, Bash]` |
| `model` | No | Model override: `sonnet`, `opus`, `haiku`, or `inherit`. If omitted, the agent uses the job's model. |

### Body

Everything after the closing `---` is the agent's system prompt. This is the full set of instructions the subagent receives. It can be as long as needed (up to 500 KB total file size).

---

## Add an Agent

Agent content can be passed as base64-encoded or plain text via the Admin API:

```bash
# Encode and send
curl -X POST http://localhost:8000/v1/admin/agents \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "vuln-scanner",
    "content_base64": "'$(base64 -w0 vuln-scanner.md)'",
    "description": "Security vulnerability scanner"
  }'
```

For large files, gzip-compress before base64 encoding — the server auto-detects and decompresses:

```bash
curl -X POST http://localhost:8000/v1/admin/agents \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "vuln-scanner",
    "content_base64": "'$(gzip -c vuln-scanner.md | base64 -w0)'",
    "description": "Security vulnerability scanner"
  }'
```

**Validation rules:**
- Agent name must start with a letter and contain only letters, digits, and hyphens
- The `name` field in YAML frontmatter must match the API request name
- Frontmatter must include both `name` and `description` fields
- Prompt body cannot be empty
- Maximum file size: 500 KB

---

## List Agents

```bash
curl http://localhost:8000/v1/admin/agents \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

---

## Show Agent Details

```bash
curl http://localhost:8000/v1/admin/agents/vuln-scanner \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

Returns agent metadata, parsed frontmatter, and a body preview.

---

## Update an Agent

Replace the entire definition or update just the description:

```bash
# Replace content
curl -X PUT http://localhost:8000/v1/admin/agents/vuln-scanner \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "content_base64": "'$(base64 -w0 vuln-scanner-v2.md)'"
  }'

# Update description only
curl -X PUT http://localhost:8000/v1/admin/agents/vuln-scanner \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"description": "V2 with OWASP Top 10 focus"}'
```

---

## Remove an Agent

```bash
curl -X DELETE http://localhost:8000/v1/admin/agents/vuln-scanner \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

The agent is immediately unavailable to new jobs.

---

## Storage Layout

```
/data/agents/
├── agents.json        ← Management metadata (added_at, description)
├── prompts/           ← Agent definition .md files (source of truth)
│   ├── vuln-scanner.md
│   └── code-reviewer.md
└── .lock              ← File lock for concurrent safety
```

- `agents.json` — Metadata registry used for listing and display. The SDK integration reads `.md` files directly.
- `prompts/*.md` — The actual agent definition files. These are the source of truth.
- `.lock` — File-based lock (via `fcntl.flock`) to prevent concurrent modifications from corrupting metadata.

---

## Sandbox Integration

Subagents execute within the same bwrap process-level sandbox as the main Claude Code agent. Because the sandbox wraps the entire CLI process tree, all subagent tool invocations — Bash commands, file reads, writes — inherit the restricted filesystem namespace automatically.

This means:

- Subagents **can** read/write only the job's `input/` directory
- Subagents **cannot** access other jobs' data, the user's home directory, or system files
- Subagents **can** use all available tools (Read, Write, Edit, Bash, Grep, Glob, etc.) — the bwrap sandbox is the security boundary, not per-tool restrictions
- Network access is governed by the client's security profile — unrestricted for `unconfined`, filtered through a per-job proxy for other profiles (Anthropic API is always allowed)

For details on the bwrap sandbox, see [Security — Sandbox Isolation](security-model.md#sandbox-isolation-bwrap).

**CLI alternative:** `python cli/manage.py agent` provides a friendlier command-line interface for agent management. See [Usage Examples](usage-examples.md#ccas-manager-climanagepy--server-administration).

See [API Reference — Agent Management](api-reference.md#agent-management) for full endpoint details.
