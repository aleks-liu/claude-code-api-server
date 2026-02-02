# Skill Management

[← Back to README](../README.md)

Skills extend Claude Code with reusable, invokable capabilities. Unlike [subagents](subagents.md) (which are spawned via the `Task` tool and run as child processes), skills are **plugin-based extensions** that Claude Code discovers via the `--plugin-dir` mechanism. Each skill is a directory containing a `SKILL.md` file with YAML frontmatter and instructions.

## Table of Contents

- [Skills vs Subagents](#skills-vs-subagents)
- [How Skills Work](#how-skills-work)
- [Skill File Format](#skill-file-format)
- [Add a Skill](#add-a-skill)
- [List Skills](#list-skills)
- [Show Skill Details](#show-skill-details)
- [Update a Skill](#update-a-skill)
- [Remove a Skill](#remove-a-skill)
- [Storage Layout](#storage-layout)
- [Sandbox Integration](#sandbox-integration)

---

## Skills vs Subagents

| Aspect | Skills | Subagents |
|--------|--------|-----------|
| **Delivery** | Plugin directory (`--plugin-dir`) | SDK parameter (`options.agents` dict) |
| **Storage** | Directory per skill (`<name>/SKILL.md`) | Flat file per agent (`<name>.md`) |
| **Discovery** | Claude Code CLI discovers from filesystem | Programmatically injected at job start |
| **Invocation** | Claude invokes via `/skill-name` or automatically based on description | Claude invokes via `Task` tool |
| **Namespace** | `cca-skills:<name>` | Plain name |
| **User-invocable** | Supported (`user-invocable: true` frontmatter) | Not applicable |
| **Context** | Can fork context (`context: fork`) | Inherits parent context |

**When to use which:**

- **Skills** — for reusable capabilities that Claude should auto-detect and invoke based on context (e.g., "code vulnerability analysis", "generate unit tests"), or that users invoke explicitly via `/skill-name`.
- **Subagents** — for specialized child agents that the main agent delegates to via `Task` (e.g., a focused scanner that explores code and returns a report).

---

## How Skills Work

The integration uses Claude Code's **plugin mechanism**. All skills are stored inside a single plugin directory, which Claude Code discovers via the `--plugin-dir` flag.

1. **Management**: Skill files (`SKILL.md` with YAML frontmatter) are written to `/data/skills-plugin/skills/<name>/SKILL.md` via the Admin API. A plugin manifest (`plugin.json`) is auto-generated.

2. **Per-job attachment**: At job execution time, the server checks if any skills exist. If so, it passes the plugin directory to the Claude Agent SDK via `options.plugins = [{"type": "local", "path": "/data/skills-plugin"}]`. This translates to the `--plugin-dir` CLI flag.

3. **CLI discovery**: Claude Code reads the plugin manifest, scans the `skills/` subdirectory, and registers each `SKILL.md` as a skill namespaced under `cca-skills:<name>`.

4. **No restart needed**: Since the plugin directory is attached at each job execution, additions and removals via the Admin API take effect immediately for new jobs.

5. **Sandbox visibility**: The plugin directory is bind-mounted **read-only** into the bwrap sandbox so the CLI process can discover skills without having write access to them.

```
POST /v1/admin/skills ──► /data/skills-plugin/skills/my-skill/SKILL.md
                          /data/skills-plugin/.claude-plugin/plugin.json (auto)
                                    │
                      ┌─────────────┘
                      ▼
            Job execution starts
                      │
                      ▼
             Server checks skills/ for */SKILL.md
             Sets options.plugins = [{type: local, path: ...}]
                      │
                      ▼
             bwrap bind-mounts /data/skills-plugin (read-only)
             Claude Code CLI starts with --plugin-dir
                      │
                      ▼
             CLI reads plugin.json, discovers skills/
             Registers "cca-skills:my-skill"
                      │
                      ▼
             Claude can now invoke the skill automatically
             (or user invokes via /my-skill if user-invocable)
```

---

## Skill File Format

Skill definitions use `SKILL.md` files with YAML frontmatter:

```markdown
---
name: code-vuln-analysis
description: Analyze codebase for security vulnerabilities
allowed-tools: Read, Grep, Glob, Bash, Write
model: sonnet
user-invocable: true
---

You are a security vulnerability analyst.

## Task

Analyze the provided codebase for OWASP Top 10 vulnerabilities.

## Output Format

For each vulnerability found, report:
1. File and line number
2. Vulnerability type and CWE ID
3. Severity (Critical/High/Medium/Low)
4. Recommended fix
```

### Frontmatter Fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | No* | Unique identifier. If omitted, auto-injected from the API request name. If present, must match exactly. |
| `description` | Yes | What the skill does. Claude uses this to decide when to auto-invoke it. |
| `allowed-tools` | No | Comma-separated list of tools the skill may use. If omitted, inherits defaults. |
| `model` | No | Model override (`sonnet`, `opus`, `haiku`). If omitted, uses the job's model. |
| `user-invocable` | No | Boolean. If `true`, the skill can be invoked by name (e.g., `/code-vuln-analysis`). |
| `disable-model-invocation` | No | Boolean. If `true`, Claude cannot auto-invoke the skill — only explicit user invocation works. |
| `context` | No | Must be `"fork"` if present. Forks the conversation context so skill execution doesn't pollute the main thread. |
| `agent` | No | Agent type to use for the skill. |
| `hooks` | No | Hook configuration for the skill. |
| `argument-hint` | No | Hint text shown when the user invokes the skill. |

\* `name` is auto-injected if missing.

### Body

Everything after the closing `---` is the skill's instruction prompt. It can be as long as needed (up to 500 KB total file size).

---

## Add a Skill

```bash
# From a SKILL.md file with frontmatter
curl -X POST http://localhost:8000/v1/admin/skills \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "code-vuln-analysis",
    "content_base64": "'$(base64 -w0 code-vuln-analysis-SKILL.md)'",
    "description": "Analyze codebase for security vulnerabilities"
  }'
```

For large files, gzip-compress before base64 encoding — the server auto-detects and decompresses:

```bash
curl -X POST http://localhost:8000/v1/admin/skills \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "code-vuln-analysis",
    "content_base64": "'$(gzip -c code-vuln-analysis-SKILL.md | base64 -w0)'"
  }'
```

**Validation rules:**
- Skill name must start with a lowercase letter and contain only lowercase letters, digits, and hyphens (max 64 characters)
- If frontmatter `name` is present, it must match the API request name
- Frontmatter must include `description` (either in file or via request field)
- Body (instructions) cannot be empty
- Maximum file size: 500 KB
- Boolean fields (`user-invocable`, `disable-model-invocation`) must be actual booleans
- `context` must be `"fork"` if present

---

## List Skills

```bash
curl http://localhost:8000/v1/admin/skills \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

---

## Show Skill Details

```bash
curl http://localhost:8000/v1/admin/skills/code-vuln-analysis \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

Returns skill metadata, parsed frontmatter, and a body preview.

---

## Update a Skill

Replace the entire SKILL.md or update just the description:

```bash
# Replace content
curl -X PUT http://localhost:8000/v1/admin/skills/code-vuln-analysis \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "content_base64": "'$(base64 -w0 code-vuln-analysis-v2-SKILL.md)'"
  }'

# Update description only
curl -X PUT http://localhost:8000/v1/admin/skills/code-vuln-analysis \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"description": "V2: OWASP Top 10 + CWE coverage"}'
```

---

## Remove a Skill

```bash
curl -X DELETE http://localhost:8000/v1/admin/skills/code-vuln-analysis \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

The skill is immediately unavailable to new jobs. The entire skill directory is deleted.

---

## Storage Layout

Skills use two separate directory trees:

```
/data/skills-plugin/                  ← Plugin directory (passed to Claude Code via --plugin-dir)
├── .claude-plugin/
│   └── plugin.json                   ← Auto-generated manifest
│                                       {"name": "cca-skills", "version": "1.0.0", ...}
└── skills/
    ├── code-vuln-analysis/
    │   └── SKILL.md                  ← Skill definition (frontmatter + body)
    └── unit-test-generator/
        └── SKILL.md

/data/skills-meta/                    ← Management metadata (not visible to Claude Code)
├── skills.json                       ← {name: {added_at, description, skill_size_bytes}}
└── .lock                             ← File lock for concurrent access safety
```

- **`skills-plugin/`** — The actual plugin directory. This is the only path Claude Code sees. Contains the manifest and skill subdirectories. Mounted read-only in the sandbox.
- **`skills-meta/`** — Provenance metadata used by the admin API for listing and display. Stored separately to keep the plugin directory clean (Claude Code only expects `.claude-plugin/` and `skills/`).
- **`plugin.json`** — Auto-generated on first skill addition. Fixed identity: name `cca-skills`, version `1.0.0`. Regenerated if missing.
- **`.lock`** — File-based lock (`fcntl.flock`) preventing concurrent modifications from corrupting metadata.

---

## Sandbox Integration

The skills plugin directory is bind-mounted **read-only** into the bwrap process-level sandbox. This is handled automatically by the server:

```
Outside sandbox:  /data/skills-plugin/  (writable by admin API)
                          │
                  ┌───────┘
                  ▼
Inside sandbox:   /data/skills-plugin/  (read-only bind mount)
                          │
                  Claude Code CLI reads plugin.json
                  Discovers skills/*/SKILL.md
                  Registers as cca-skills:<name>
```

This means:

- Claude Code **can read** the plugin manifest and all SKILL.md files
- Claude Code **cannot modify** skill files from inside the sandbox
- The rest of `/data/` remains hidden behind a tmpfs overlay
- If no skills exist, the plugin directory is not mounted (no overhead)

For details on the bwrap sandbox, see [Security — Sandbox Isolation](security-model.md#sandbox-isolation-bwrap).

See [API Reference — Skill Management](api-reference.md#skill-management) for full endpoint details.
