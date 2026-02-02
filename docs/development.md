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
git clone https://github.com/aleks-liu/claude_code_api_server.git
cd claude_code_api_server

# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Install dev dependencies
pip install pytest pytest-asyncio httpx

# Run in development mode
export CCAS_DEBUG=true
export CCAS_DATA_DIR=./data
python -m uvicorn src.main:app --reload
```

---

## Running Tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=src --cov-report=html
```

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
| `cleanup.py` | Background cleanup tasks |
| `main.py` | FastAPI routes and middleware |
| `admin_router.py` | Admin API endpoints (clients, profiles, MCP, agents, skills) |
| `logging_config.py` | Structured logging setup (structlog, JSON/console modes, sensitive data masking) |
| `crypto.py` | RSA OAEP encryption for admin bootstrap token |
| `mcp_installer.py` | npm/pip package installation and entry point detection for MCP servers |
