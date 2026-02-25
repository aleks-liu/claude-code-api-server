# Development

[← Back to README](../README.md)

## Table of Contents

- [Local Development Setup](#local-development-setup)
- [Running Tests](#running-tests)
- [Code Structure](#code-structure)

---

## Local Development Setup

```bash
# Clone repository
git clone https://github.com/aleks-liu/claude-code-server.git
cd claude-code-server

# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Install test dependencies
pip install -r tests/requirements.txt

# Run in development mode
export CCAS_DEBUG=true
export CCAS_DATA_DIR=./data
python -m uvicorn src.main:app --reload
```

---

## Running Tests

Tests are **integration tests** that run against a live CCAS instance.

```bash
# 1. Start the server (e.g. via docker compose up -d)

# 2. Set required environment variables
export TEST_BASE_URL=http://localhost:8000   # default; can omit if using this URL
export TEST_ADMIN_API_KEY=ccas_your_admin_key
export ANTHROPIC_API_KEY=sk-ant-your_key     # required for AI tests; or pass --skip-ai

# 3. Run tests
python tests/run_tests.py
```

Useful flags: `--skip-ai` (skip tests that call the Anthropic API), `--module test_01_health` (run a single module), `--verbose`.

See `tests/README.md` for details on test structure and configuration.

---

## Code Structure

| Module | Responsibility |
|--------|----------------|
| `config.py` | Configuration validation with Pydantic |
| `models.py` | All data models (API + internal) |
| `auth.py` | API key hashing and validation |
| `sandbox.py` | Process-level bwrap sandbox (primary isolation) |
| `sandbox_seccomp.py` | seccomp BPF detection and configuration |
| `sandbox_proxy.py` | Per-job HTTP proxy for network isolation (domain/IP filtering) |
| `security.py` | can_use_tool callback implementation (profile-driven tool policy) |
| `security_profiles.py` | Security profile CRUD, validation, and persistence |
| `upload_handler.py` | ZIP handling with security checks |
| `job_manager.py` | Job state machine and persistence |
| `claude_runner.py` | Claude Agent SDK integration |
| `mcp_manager.py` | MCP server configuration CRUD and persistence |
| `mcp_loader.py` | MCP runtime loading, env expansion, sandbox binds, health checks |
| `agent_manager.py` | Subagent definition CRUD, YAML frontmatter parsing, SDK-oriented loading |
| `skill_manager.py` | Skill definition CRUD and plugin manifest management |
| `skill_zip_handler.py` | Skill ZIP validation, extraction, path traversal protection, zip bomb defense |
| `cleanup.py` | Background cleanup tasks |
| `main.py` | FastAPI routes and middleware |
| `admin_router.py` | Admin API endpoints (clients, profiles, MCP, agents, skills) |
| `logging_config.py` | Structured logging setup (structlog, JSON/console modes, sensitive data masking) |
| `crypto.py` | RSA OAEP encryption for admin bootstrap token |
| `mcp_installer.py` | npm/pip package installation and entry point detection for MCP servers |
