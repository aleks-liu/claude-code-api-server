# Skill Management

[← Back to README](../README.md)

Skills extend Claude Code with reusable, invokable capabilities. Unlike [subagents](subagents.md) (which are spawned via the `Task` tool and run as child processes), skills are **plugin-based extensions** that Claude Code discovers via the `--plugin-dir` mechanism. Each skill is a directory containing a `SKILL.md` file with YAML frontmatter and instructions, plus optional subdirectories for scripts, references, and assets.

## Table of Contents

- [Skills vs Subagents](#skills-vs-subagents)
- [How Skills Work](#how-skills-work)
- [Skill Directory Structure](#skill-directory-structure)
- [Skill File Format](#skill-file-format)
- [Add a Skill](#add-a-skill)
- [List Skills](#list-skills)
- [Show Skill Details](#show-skill-details)
- [Update a Skill](#update-a-skill)
- [Remove a Skill](#remove-a-skill)
- [Storage Layout](#storage-layout)
- [Security](#security)
- [Sandbox Integration](#sandbox-integration)

---

## Skills vs Subagents

| Aspect | Skills | Subagents |
|--------|--------|-----------|
| **Delivery** | Plugin directory (`--plugin-dir`) | Plugin directory (`--plugin-dir`, synced from source) |
| **Storage** | Directory per skill (`<name>/SKILL.md` + optional subdirs) | Flat file per agent (`<name>.md`) |
| **Discovery** | Claude Code CLI discovers from `skills/` in plugin dir | Claude Code CLI discovers from `agents/` in plugin dir |
| **Invocation** | Claude invokes via `/skill-name` or automatically based on description | Claude invokes via `Task` tool |
| **Namespace** | `ccas-plugin:<name>` | Plain name |
| **User-invocable** | Supported (`user-invocable: true` frontmatter) | Not applicable |
| **Context** | Can fork context (`context: fork`) | Inherits parent context |

**When to use which:**

- **Skills** — for reusable capabilities that Claude should auto-detect and invoke based on context (e.g., "code vulnerability analysis", "generate unit tests"), or that users invoke explicitly via `/skill-name`.
- **Subagents** — for specialized child agents that the main agent delegates to via `Task` (e.g., a focused scanner that explores code and returns a report).

---

## How Skills Work

The integration uses Claude Code's **plugin mechanism**. All skills are stored inside a single plugin directory, which Claude Code discovers via the `--plugin-dir` flag.

1. **Management**: Skill directories are uploaded as ZIP archives via the Admin API and extracted to `/data/skills-plugin/skills/<name>/`. A plugin manifest (`plugin.json`) is auto-generated.

2. **Per-job attachment**: At job execution time, the server checks if any skills exist. If so, it passes the plugin directory to the Claude Agent SDK via `options.plugins = [{"type": "local", "path": "/data/skills-plugin"}]`. This translates to the `--plugin-dir` CLI flag.

3. **CLI discovery**: Claude Code reads the plugin manifest, scans the `skills/` subdirectory, and registers each `SKILL.md` as a skill namespaced under `ccas-plugin:<name>`.

4. **No restart needed**: Since the plugin directory is attached at each job execution, additions and removals via the Admin API take effect immediately for new jobs.

5. **Sandbox visibility**: The plugin directory is bind-mounted **read-only** into the bwrap sandbox so the CLI process can discover skills without having write access to them.

```
POST /v1/admin/skills (multipart/form-data with ZIP)
      │
      ▼
Validate & extract ZIP
      │
      ▼
/data/skills-plugin/skills/my-skill/
├── SKILL.md
├── scripts/
├── references/
└── assets/
      │
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
Registers "ccas-plugin:my-skill"
      │
      ▼
Claude can now invoke the skill automatically
(or user invokes via /my-skill if user-invocable)
```

---

## Skill Directory Structure

Each skill is a directory containing at minimum `SKILL.md`, plus optional subdirectories:

```
your-skill-name/
├── SKILL.md              # Required — main skill file
├── scripts/              # Optional — executable code
│   ├── process_data.py
│   └── validate.sh
├── references/           # Optional — documentation
│   ├── api-guide.md
│   └── examples/
├── configs/              # Optional — configuration files
│   └── settings.json
└── assets/               # Optional — templates, etc.
    └── report-template.md
```

**Requirements:**
- `SKILL.md` is required at the root level
- Additional directories and files are allowed, provided names match the naming rules (alphanumeric start, then alphanumeric, underscores, hyphens)

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
| `name` | No* | Unique identifier. If omitted, resolved from the ZIP directory name or the API `name` parameter. If present, must match exactly. |
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

Skills are uploaded as ZIP archives via `multipart/form-data`:

```bash
# Zip your skill directory
cd /path/to/skills
zip -r code-vuln-analysis.zip code-vuln-analysis/

# Upload via API
curl -X POST http://localhost:8000/v1/admin/skills \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -F "skill_data=@code-vuln-analysis.zip"
```

The skill name is derived from the root directory name in the ZIP. To override:

```bash
curl -X POST http://localhost:8000/v1/admin/skills \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -F "skill_data=@my-skill.zip" \
  -F "name=custom-skill-name"
```

**Validation rules:**
- Skill name must start with a letter and contain only letters, digits, and hyphens
- ZIP must contain `SKILL.md` at the skill root level
- Directory and file names must start with alphanumeric and contain only alphanumeric, underscores, hyphens (dirs) or dots (files)
- Frontmatter must include `description`
- Body (instructions) cannot be empty
- Maximum ZIP size: 15 MB (compressed), 50 MB (extracted)
- Maximum files per archive: 100
- Maximum individual file size: 5 MB
- Maximum nesting depth: 5 levels
- No symlinks, no path traversal, no hidden files

---

## List Skills

```bash
curl http://localhost:8000/v1/admin/skills \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

Response includes `file_count` for each skill.

---

## Show Skill Details

```bash
curl http://localhost:8000/v1/admin/skills/code-vuln-analysis \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

Returns skill metadata, parsed frontmatter, body preview, and a `file_listing` of all files in the skill directory.

---

## Update a Skill

Upload a new ZIP to fully replace the skill directory:

```bash
curl -X PUT http://localhost:8000/v1/admin/skills/code-vuln-analysis \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -F "skill_data=@code-vuln-analysis-v2.zip"
```

This is a **full replacement** — the entire skill directory is swapped atomically. To change the description, update `SKILL.md` in the ZIP and re-upload.

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
│                                       {"name": "ccas-plugin", "version": "1.0.0", ...}
├── agents/                           ← Agent .md files synced from /data/agents/prompts/
│   └── (agent-name.md)
└── skills/
    ├── code-vuln-analysis/
    │   ├── SKILL.md                  ← Skill definition (frontmatter + body)
    │   ├── scripts/
    │   │   └── analyze.py
    │   └── references/
    │       └── owasp-guide.md
    └── unit-test-generator/
        └── SKILL.md

/data/skills-meta/                    ← Management metadata (not visible to Claude Code)
├── skills.json                       ← {"_metadata": {name: {added_at, description, skill_size_bytes, file_count}}}
└── .lock                             ← File lock for concurrent access safety
```

- **`skills-plugin/`** — The actual plugin directory. This is the only path Claude Code sees. Contains the manifest, skill subdirectories, and agent files. Mounted read-only in the sandbox.
- **`skills-meta/`** — Provenance metadata used by the admin API for listing and display. Stored separately to keep the plugin directory clean.
- **`plugin.json`** — Auto-generated on first skill addition. Fixed identity: name `ccas-plugin`, version `1.0.0`. Regenerated if missing.
- **`.lock`** — File-based lock (`fcntl.flock`) preventing concurrent modifications from corrupting metadata.

---

## Security

Skill ZIP archives undergo comprehensive security validation:

| Check | Description |
|-------|-------------|
| **Size limits** | 15 MB compressed, 50 MB extracted, 5 MB per file, 100 files max |
| **Format validation** | ZIP magic bytes + structure verification |
| **Path traversal** | Multi-layer: `..` rejection, absolute path rejection, resolve() check |
| **Symlink rejection** | All symlink entries are rejected via external_attr check |
| **Zip bomb protection** | Declared sizes checked pre-extraction + actual bytes tracked during extraction |
| **Filename allowlist** | Files: `[a-zA-Z0-9][a-zA-Z0-9._-]*`, dirs: `[a-zA-Z0-9][a-zA-Z0-9_-]*` |
| **Structure enforcement** | SKILL.md required at root level |
| **Duplicate detection** | No two entries may resolve to the same path |
| **Nesting depth** | Maximum 5 levels below skill root |
| **Atomic deployment** | Skill directory fully written before becoming visible; rollback on failure |

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
                  Registers as ccas-plugin:<name>
```

This means:

- Claude Code **can read** the plugin manifest, all SKILL.md files, and all supporting files (scripts, references, assets)
- Claude Code **cannot modify** skill files from inside the sandbox
- The rest of `/data/` remains hidden behind a tmpfs overlay
- If no skills exist, the plugin directory is not mounted (no overhead)

For details on the bwrap sandbox, see [Security — Sandbox Isolation](security-model.md#sandbox-isolation-bwrap).

See [API Reference — Skill Management](api-reference.md#skill-management) for full endpoint details.
