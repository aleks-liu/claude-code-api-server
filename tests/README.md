# Integration Tests

## Prerequisites

- Docker container running at `127.0.0.1:8080`
- Python 3.12+ with `pytest`, `httpx`, `pytest-json-report` installed
- Admin API key and client ID for the target server

```bash
pip install pytest httpx pytest-json-report
```

## Container Setup

```bash
docker run -d \
  --name cca-test \
  -p 8080:8000 \
  -e CCAS_RATE_LIMIT="" \
  -e CCAS_MCP_HEALTH_CHECK_ALLOW_FAILURE=true \
  claude-code-api-server:latest

# Create admin (save the key from output)
docker exec cca-test python create_admin.py test-admin
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TEST_ADMIN_API_KEY` | Yes | Admin API key |
| `TEST_BASE_URL` | No | Server URL (default: `http://127.0.0.1:8080`) |
| `ANTHROPIC_API_KEY` | No | Required only for AI integration test |

```bash
export TEST_ADMIN_API_KEY="cca_..."
```

## Running Tests

### Via orchestrator (recommended)

```bash
# Run all tests
python tests/run_tests.py

# Skip AI integration test
python tests/run_tests.py --skip-ai

# Run a single module
python tests/run_tests.py --module test_03_uploads

# Verbose output
python tests/run_tests.py --verbose

# Custom server URL
python tests/run_tests.py --base-url http://localhost:9090
```

The orchestrator produces a JSON report in `tests/reports/`.

### Via pytest directly

```bash
# From project root
PYTHONPATH=. pytest tests/tests/test_01_health.py -v

# All modules
PYTHONPATH=. pytest tests/tests/ -v
```

## Test Modules

| Module | Tests | Description |
|--------|-------|-------------|
| `test_01_health` | 4 | Health endpoint |
| `test_02_auth` | 10 | Auth edge cases, deactivation, role enforcement |
| `test_03_uploads` | 10 | ZIP upload, validation, path traversal |
| `test_04_jobs` | 19 | Job creation, retrieval, validation |
| `test_05_admin_clients` | 21 | Client CRUD, self-protection |
| `test_06_admin_mcp` | 19 | MCP server config management |
| `test_07_admin_agents` | 19 | Agent CRUD, frontmatter validation |
| `test_08_admin_skills` | 18 | Skill CRUD, frontmatter validation |
| `test_09_security_isolation` | 16 | BOLA, role enforcement, deactivated clients |
| `test_10_input_validation` | 15 | Injection, XSS, type confusion, oversized body |
| `test_11_security_profiles` | 46 | Security profile CRUD, client-profile binding, built-in verification, validation |
| `test_12_network_isolation` | 56 | Network proxy unit tests: domain/IP filtering, policy evaluation, CONNECT tunneling, wrapper generation |
| `test_13_seccomp_hardening` | 44 | seccomp BPF detection, npm fallback discovery, inner script generation, runner wiring integration |
| `test_97_ai_network_isolation` | 6 | AI tests: network isolation enforcement per profile (skipped without API key) |
| `test_98_ai_security_profiles` | 9 | AI tests: tool denial, MCP filtering, selective policies (skipped without API key) |
| `test_99_ai_integration` | 1 | Live Claude execution (skipped without API key) |

## Cleanup

Tests clean up after themselves via fixture teardown. The orchestrator also runs a global cleanup pass that removes any resources with `test-client-`, `test-agent-`, `test-skill-`, `test-mcp-`, or `test-profile-` prefixes.
