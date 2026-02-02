"""AI integration test — verifies Claude Code sees configured agents, skills, and MCP servers.

This module runs last. It requires ANTHROPIC_API_KEY to be set.
"""

import base64
import json
import os
import time

import pytest

from helpers.api_client import ApiClient
from helpers.test_data import make_agent_content, make_skill_content

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
BASE_URL = os.environ.get("TEST_BASE_URL", "http://127.0.0.1:8080")
ADMIN_API_KEY = os.environ.get("TEST_ADMIN_API_KEY", "")

pytestmark = pytest.mark.skipif(
    not ANTHROPIC_API_KEY,
    reason="ANTHROPIC_API_KEY not set — skipping AI integration test",
)


@pytest.fixture(scope="module")
def ai_setup(api, admin_headers):
    """Module-scoped fixture: set up test agent, skill, and collect expected capabilities."""
    # Create test agent
    agent_name = "test-agent-diag"
    agent_content = make_agent_content(agent_name, description="Diagnostic test agent")
    resp = api.post("/v1/admin/agents", headers=admin_headers, json={
        "name": agent_name,
        "content": agent_content,
    })
    # May already exist from a previous run — accept 201 or 409
    assert resp.status_code in (201, 409)

    # Create test skill
    skill_name = "test-skill-diag"
    skill_content = make_skill_content(skill_name, description="Diagnostic test skill")
    resp = api.post("/v1/admin/skills", headers=admin_headers, json={
        "name": skill_name,
        "content": skill_content,
    })
    assert resp.status_code in (201, 409)

    # Collect expected MCP servers
    resp = api.get("/v1/admin/mcp", headers=admin_headers)
    expected_mcp = [s["name"] for s in resp.json()] if resp.status_code == 200 else []

    # Collect expected agents
    resp = api.get("/v1/admin/agents", headers=admin_headers)
    expected_agents = [a["name"] for a in resp.json()] if resp.status_code == 200 else [agent_name]

    # Collect expected skills
    resp = api.get("/v1/admin/skills", headers=admin_headers)
    expected_skills = [s["name"] for s in resp.json()] if resp.status_code == 200 else [skill_name]

    yield {
        "agent_name": agent_name,
        "skill_name": skill_name,
        "expected_mcp": expected_mcp,
        "expected_agents": expected_agents,
        "expected_skills": expected_skills,
    }

    # Teardown
    api.delete(f"/v1/admin/agents/{agent_name}", headers=admin_headers)
    api.delete(f"/v1/admin/skills/{skill_name}", headers=admin_headers)


@pytest.fixture(scope="module")
def ai_client_headers(api, admin_headers):
    """Create a test client for AI integration."""
    from helpers.test_data import random_suffix
    cid = f"test-client-ai-{random_suffix()}"
    resp = api.post("/v1/admin/clients", headers=admin_headers, json={
        "client_id": cid,
        "description": "AI integration test client",
        "role": "client",
    })
    assert resp.status_code == 201
    key = resp.json()["api_key"]
    headers = {"Authorization": f"Bearer {key}"}

    yield headers

    api.delete(f"/v1/admin/clients/{cid}", headers=admin_headers)


def test_claude_code_reports_capabilities(api, ai_setup, ai_client_headers, admin_headers):
    """Submit a job asking Claude to report its available capabilities."""
    client_headers = ai_client_headers

    # Submit job
    resp = api.post("/v1/jobs", headers={
        **client_headers,
        "X-Anthropic-Key": ANTHROPIC_API_KEY,
    }, json={
        "prompt": (
            "Your task: discover what agents, skills, and MCP servers are available to you. "
            "Write a JSON file called 'capabilities.json' with the following exact structure:\n"
            "{\n"
            '  "agents": ["agent-name-1", "agent-name-2"],\n'
            '  "skills": ["skill-name-1", "skill-name-2"],\n'
            '  "mcp_servers": ["server-name-1", "server-name-2"]\n'
            "}\n"
            "List the names of ALL agents, skills, and MCP servers you can see. "
            "Use the exact names as they appear in your configuration. "
            "If a category has no items, use an empty array. "
            "Do not include any other text in the file, only valid JSON."
        ),
        "timeout_seconds": 300,
    })
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    # Poll for completion
    max_wait = 300
    poll_interval = 10
    start = time.time()

    while time.time() - start < max_wait:
        resp = api.get(f"/v1/jobs/{job_id}", headers=client_headers)
        assert resp.status_code == 200
        status = resp.json()["status"]
        if status in ("COMPLETED", "FAILED", "TIMEOUT"):
            break
        time.sleep(poll_interval)
    else:
        pytest.fail(f"Job {job_id} did not complete within {max_wait}s")

    job = resp.json()
    assert job["status"] == "COMPLETED", (
        f"Job failed with status {job['status']}. "
        f"Error: {job.get('error')}. "
        f"Output: {job.get('output', {}).get('text', '')[:500] if job.get('output') else 'none'}"
    )

    # Parse capabilities.json from output files
    output_files = job.get("output", {}).get("files", {})
    assert "capabilities.json" in output_files, (
        f"capabilities.json not found in output files. "
        f"Available files: {list(output_files.keys())}. "
        f"Output text: {job.get('output', {}).get('text', '')[:500]}"
    )

    raw = base64.b64decode(output_files["capabilities.json"])
    capabilities = json.loads(raw)

    # Validate structure
    assert "agents" in capabilities
    assert "skills" in capabilities
    assert "mcp_servers" in capabilities
    assert isinstance(capabilities["agents"], list)
    assert isinstance(capabilities["skills"], list)
    assert isinstance(capabilities["mcp_servers"], list)

    # Validate test agent is visible (may be namespaced via plugin)
    agent_found = (
        "test-agent-diag" in capabilities["agents"]
        or "cca-skills:test-agent-diag" in capabilities["agents"]
    )
    assert agent_found, (
        f"Test agent not found. Reported agents: {capabilities['agents']}"
    )

    # Validate test skill is visible (may be namespaced)
    skill_found = (
        "test-skill-diag" in capabilities["skills"]
        or "cca-skills:test-skill-diag" in capabilities["skills"]
    )
    assert skill_found, (
        f"Test skill not found. Reported skills: {capabilities['skills']}"
    )

    # Validate MCP servers
    for expected_server in ai_setup["expected_mcp"]:
        assert expected_server in capabilities["mcp_servers"], (
            f"Expected MCP server '{expected_server}' not found. "
            f"Reported: {capabilities['mcp_servers']}"
        )
