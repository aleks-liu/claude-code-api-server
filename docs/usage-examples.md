# Usage Examples

[← Back to README](../README.md)

## Table of Contents

- [CLI Tools](#cli-tools)
  - [Why CLI Tools?](#why-cli-tools)
  - [Environment Variables](#environment-variables)
  - [ccas-client (cli/client.py) — Job Submission](#ccas-client-cliclientpy--job-submission)
  - [ccas-manager (cli/manage.py) — Server Administration](#ccas-manager-climanagepy--server-administration)
  - [Key Capabilities](#key-capabilities)
- [Raw API (cURL)](#raw-api-curl)
- [n8n Webhook Integration](#n8n-webhook-integration)

---

## CLI Tools

### Why CLI Tools?

CCAS ships two CLI tools that replace raw curl commands and custom scripts. They handle ZIP archive creation, file uploads, job polling, and output extraction automatically — so you can focus on the prompt, not the plumbing.

### Environment Variables

| Variable | Used by | Description |
|----------|---------|-------------|
| `CCAS_URL` | Both | Server URL (e.g., `http://localhost:8000`) |
| `CCAS_CLIENT_API_KEY` | `cli/client.py` | Client API key for job submission |
| `ANTHROPIC_API_KEY` | `cli/client.py` | Your Anthropic API key |
| `CCAS_ADMIN_API_KEY` | `cli/manage.py` | Admin API key for server management |

Keys can also be passed via CLI flags (`--key`, `--anthropic-key`) or prompted interactively when missing.

---

### ccas-client (cli/client.py) — Job Submission

Three commands: **`run`**, **`fetch`**, **`status`**. Run `python cli/client.py --help` for full options.

#### Real-world example: security review of a project directory

```bash
export CCAS_URL=http://localhost:8000
export CCAS_CLIENT_API_KEY=ccas_your_client_key
export ANTHROPIC_API_KEY=sk-ant-your_key

python cli/client.py run \
  --files ./my-project \
  "Perform security review. Search for SQL injection vulnerabilities."
```

This auto-zips `./my-project` (excluding `node_modules/`, `.venv/`, `__pycache__/`, etc.), uploads the archive, creates a job, polls for completion with a progress spinner, and saves output files to a `<job_id>/` directory.

#### Additional examples

**Submit without waiting** — useful for long-running jobs:

```bash
# Submit and get job ID immediately
python cli/client.py run --no-wait --files ./src "Analyze this codebase"
# Job ID: job_abc123

# Check on it later
python cli/client.py status job_abc123

# Fetch results when done
python cli/client.py fetch job_abc123
```

**Read prompt from a file** — for complex, multi-paragraph prompts:

```bash
python cli/client.py run --prompt-file review-instructions.md --files ./src
```

**JSON output for CI pipelines** — machine-readable, no colors:

```bash
python cli/client.py run --json --no-color --files ./src "Run security scan" \
  | jq '.status'
```

---

### ccas-manager (cli/manage.py) — Server Administration

Command groups: **status**, **config**, **skill**, **agent**, **mcp**, **client**, **profile**, **sync**. Run `python cli/manage.py --help` for the full list. Run `config init` first for persistent configuration.

#### Quick examples

```bash
# Server health
python cli/manage.py status

# Create a client API key
python cli/manage.py client add ci-pipeline --description "CI/CD pipeline"

# List skills on the server
python cli/manage.py skill list

# Check sync status between local and remote
python cli/manage.py sync status

# Push a local skill to the server
python cli/manage.py sync push skill my-skill

# Add an MCP server from local config
python cli/manage.py mcp add --sync-local my-mcp-server

# Install an MCP package from npm
python cli/manage.py mcp install @anthropic/some-mcp-server
```

Most modifying commands support `--dry-run` to preview changes and `--yes` to skip confirmations.

---

### Key Capabilities

- **Auto-zipping** — directories are compressed automatically with smart excludes (`node_modules/`, `.venv/`, `__pycache__/`, etc.)
- **Progress spinner** — visual feedback while waiting for job completion
- **Output file extraction** — result files saved to a `<job_id>/` directory automatically
- **JSON mode** — `--json` flag for machine-readable output, CI-friendly
- **Interactive key prompts** — missing keys are prompted interactively (no need to export everything upfront)
- **Sync engine** — push skills and agents from local `~/.claude/` to the server with conflict detection
- **Dry-run support** — preview admin changes before applying

---

## Raw API (cURL)

> **Recommended:** Use the [CLI tools](#cli-tools) above for a simpler experience. The curl workflow below is provided as a reference for cases where you need direct API access.

```bash
# Configuration
SERVER="http://localhost:8000"
API_KEY="ccas_your_server_key_here"
ANTHROPIC_KEY="sk-ant-your_anthropic_key_here"

# 1. Create a ZIP archive of your code
cd /path/to/your/project
zip -r /tmp/project.zip . -x "*.git*" -x "node_modules/*" -x "venv/*"

# 2. Upload the archive
UPLOAD_RESPONSE=$(curl -s -X POST "$SERVER/v1/uploads" \
  -H "Authorization: Bearer $API_KEY" \
  -F "file=@/tmp/project.zip")

UPLOAD_ID=$(echo $UPLOAD_RESPONSE | jq -r '.upload_id')
echo "Upload ID: $UPLOAD_ID"

# 3. Create a job
JOB_RESPONSE=$(curl -s -X POST "$SERVER/v1/jobs" \
  -H "Authorization: Bearer $API_KEY" \
  -H "X-Anthropic-Key: $ANTHROPIC_KEY" \
  -H "Content-Type: application/json" \
  -d "{
    \"upload_ids\": [\"$UPLOAD_ID\"],
    \"prompt\": \"Analyze this codebase for security vulnerabilities.\",
    \"timeout_seconds\": 1800
  }")

JOB_ID=$(echo $JOB_RESPONSE | jq -r '.job_id')
echo "Job ID: $JOB_ID"

# 4. Poll for completion
while true; do
  STATUS_RESPONSE=$(curl -s "$SERVER/v1/jobs/$JOB_ID" \
    -H "Authorization: Bearer $API_KEY")

  STATUS=$(echo $STATUS_RESPONSE | jq -r '.status')
  echo "Status: $STATUS"

  if [ "$STATUS" = "COMPLETED" ] || [ "$STATUS" = "FAILED" ] || [ "$STATUS" = "TIMEOUT" ]; then
    echo "$STATUS_RESPONSE" | jq .
    break
  fi

  sleep 10
done

# 5. Extract output file (if any)
echo $STATUS_RESPONSE | jq -r '.output.files["report.json"]' | base64 -d > report.json
```

---

## n8n Webhook Integration

In n8n, create a workflow:

1. **Trigger**: Webhook or Schedule
2. **HTTP Request** (Upload):
   - Method: POST
   - URL: `http://your-server:8000/v1/uploads`
   - Header: `Authorization: Bearer {{$credentials.claudeApiKey}}`
   - Body: Form-Data with file
3. **HTTP Request** (Create Job):
   - Method: POST
   - URL: `http://your-server:8000/v1/jobs`
   - Headers: Auth + `X-Anthropic-Key`
   - Body: JSON with upload_ids and prompt
4. **Wait** node: 30 seconds
5. **Loop**: Poll `/v1/jobs/{job_id}` until complete
6. **Output**: Process results
