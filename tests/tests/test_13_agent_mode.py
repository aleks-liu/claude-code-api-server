"""Agent mode for jobs — integration tests.

Tests the `agent` field on POST /v1/jobs:
- Validation (format, existence)
- Job creation with valid agent
- Job response includes agent field
- Backward compatibility (no agent)
"""

from helpers.test_data import make_agent_content, random_suffix

DUMMY_ANTHROPIC_KEY = "sk-ant-test-dummy-key-for-testing-00000"


def _job_headers(client_headers: dict) -> dict:
    return {**client_headers, "X-Anthropic-Key": DUMMY_ANTHROPIC_KEY}


def _create_agent(api, admin_headers, name: str) -> None:
    """Create a test agent via admin API."""
    content = make_agent_content(name)
    resp = api.post("/v1/admin/agents", headers=admin_headers, json={
        "name": name,
        "content": content,
    })
    assert resp.status_code == 201, f"Failed to create agent: {resp.text}"


def _delete_agent(api, admin_headers, name: str) -> None:
    """Delete a test agent via admin API."""
    api.delete(f"/v1/admin/agents/{name}", headers=admin_headers)


# ── Validation: agent name format (Pydantic 422) ────────────────


def test_agent_name_uppercase_accepted(api, test_client):
    """Uppercase agent names are accepted by validation (agent may not exist, but format is valid)."""
    _, _, headers = test_client
    resp = api.post("/v1/jobs", headers=_job_headers(headers), json={
        "prompt": "test",
        "agent": "VulnScanner",
    })
    # 404 = agent not found (name format accepted), not 422 (validation rejected)
    assert resp.status_code != 422


def test_agent_name_special_chars_returns_422(api, test_client):
    _, _, headers = test_client
    resp = api.post("/v1/jobs", headers=_job_headers(headers), json={
        "prompt": "test",
        "agent": "vuln_scanner!",
    })
    assert resp.status_code == 422


def test_agent_name_starts_with_digit_returns_422(api, test_client):
    _, _, headers = test_client
    resp = api.post("/v1/jobs", headers=_job_headers(headers), json={
        "prompt": "test",
        "agent": "1scanner",
    })
    assert resp.status_code == 422


def test_agent_name_starts_with_hyphen_returns_422(api, test_client):
    _, _, headers = test_client
    resp = api.post("/v1/jobs", headers=_job_headers(headers), json={
        "prompt": "test",
        "agent": "-scanner",
    })
    assert resp.status_code == 422


def test_agent_name_with_underscore_returns_422(api, test_client):
    _, _, headers = test_client
    resp = api.post("/v1/jobs", headers=_job_headers(headers), json={
        "prompt": "test",
        "agent": "vuln_scanner",
    })
    assert resp.status_code == 422


def test_agent_name_too_long_returns_422(api, test_client):
    _, _, headers = test_client
    resp = api.post("/v1/jobs", headers=_job_headers(headers), json={
        "prompt": "test",
        "agent": "a" * 101,
    })
    assert resp.status_code == 422


# ── Validation: agent not found (fail-fast 400) ─────────────────


def test_agent_not_found_returns_400(api, test_client):
    _, _, headers = test_client
    resp = api.post("/v1/jobs", headers=_job_headers(headers), json={
        "prompt": "test",
        "agent": f"nonexistent-{random_suffix()}",
    })
    assert resp.status_code == 400
    body = resp.json()
    assert body.get("error_code") == "JOB_ERROR"
    assert "not found" in body["detail"].lower()


# ── Job creation with valid agent ────────────────────────────────


def test_create_job_with_agent(api, admin_headers, test_client):
    _, _, headers = test_client
    agent_name = f"test-agent-{random_suffix()}"
    _create_agent(api, admin_headers, agent_name)

    try:
        resp = api.post("/v1/jobs", headers=_job_headers(headers), json={
            "prompt": "Analyze for vulnerabilities",
            "agent": agent_name,
        })
        assert resp.status_code == 202
        body = resp.json()
        assert "job_id" in body
        assert body["status"] == "PENDING"
    finally:
        _delete_agent(api, admin_headers, agent_name)


def test_create_job_with_agent_and_claude_md(api, admin_headers, test_client):
    """Agent and claude_md can coexist (agent provides base prompt, claude_md adds context)."""
    _, _, headers = test_client
    agent_name = f"test-agent-{random_suffix()}"
    _create_agent(api, admin_headers, agent_name)

    try:
        resp = api.post("/v1/jobs", headers=_job_headers(headers), json={
            "prompt": "Analyze for vulnerabilities",
            "agent": agent_name,
            "claude_md": "Focus on Python files only.",
        })
        assert resp.status_code == 202
    finally:
        _delete_agent(api, admin_headers, agent_name)


# ── Job response includes agent field ────────────────────────────


def test_get_job_includes_agent_field(api, admin_headers, test_client):
    _, _, headers = test_client
    agent_name = f"test-agent-{random_suffix()}"
    _create_agent(api, admin_headers, agent_name)

    try:
        create_resp = api.post("/v1/jobs", headers=_job_headers(headers), json={
            "prompt": "test",
            "agent": agent_name,
        })
        assert create_resp.status_code == 202
        job_id = create_resp.json()["job_id"]

        get_resp = api.get(f"/v1/jobs/{job_id}", headers=headers)
        assert get_resp.status_code == 200
        body = get_resp.json()
        assert body["agent"] == agent_name
    finally:
        _delete_agent(api, admin_headers, agent_name)


def test_get_job_without_agent_has_null_agent(api, test_client):
    _, _, headers = test_client
    create_resp = api.post("/v1/jobs", headers=_job_headers(headers), json={
        "prompt": "test without agent",
    })
    assert create_resp.status_code == 202
    job_id = create_resp.json()["job_id"]

    get_resp = api.get(f"/v1/jobs/{job_id}", headers=headers)
    assert get_resp.status_code == 200
    body = get_resp.json()
    assert body["agent"] is None


# ── Backward compatibility ───────────────────────────────────────


def test_job_without_agent_field_still_works(api, test_client):
    """Omitting agent entirely must behave identically to pre-feature behavior."""
    _, _, headers = test_client
    resp = api.post("/v1/jobs", headers=_job_headers(headers), json={
        "prompt": "What is 2+2?",
    })
    assert resp.status_code == 202


def test_agent_null_is_same_as_omitted(api, test_client):
    """Explicitly passing agent=null behaves the same as omitting it."""
    _, _, headers = test_client
    resp = api.post("/v1/jobs", headers=_job_headers(headers), json={
        "prompt": "What is 2+2?",
        "agent": None,
    })
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    get_resp = api.get(f"/v1/jobs/{job_id}", headers=headers)
    assert get_resp.json()["agent"] is None


def test_agent_whitespace_only_treated_as_null(api, test_client):
    """Whitespace-only agent string is normalized to null (no agent)."""
    _, _, headers = test_client
    resp = api.post("/v1/jobs", headers=_job_headers(headers), json={
        "prompt": "What is 2+2?",
        "agent": "   ",
    })
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    get_resp = api.get(f"/v1/jobs/{job_id}", headers=headers)
    assert get_resp.json()["agent"] is None
