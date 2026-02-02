"""Junk data and edge case input validation tests."""

DUMMY_ANTHROPIC_KEY = "sk-ant-test-dummy-key-for-testing-00000"


def _job_headers(client_headers: dict) -> dict:
    return {**client_headers, "X-Anthropic-Key": DUMMY_ANTHROPIC_KEY}


# ── Malformed JSON ─────────────────────────────────────────────────


def test_malformed_json_body(api, test_client):
    _, _, headers = test_client
    resp = api.post("/v1/jobs", headers={
        **_job_headers(headers),
        "Content-Type": "application/json",
    }, content=b"{invalid json")
    assert resp.status_code == 422


def test_empty_json_body(api, test_client):
    _, _, headers = test_client
    resp = api.post("/v1/jobs", headers=_job_headers(headers), json={})
    assert resp.status_code == 422


# ── Oversized body ─────────────────────────────────────────────────


def test_oversized_json_body_returns_413(api, test_client):
    _, _, headers = test_client
    # Default max_request_body_mb is 10MB, send > 10MB
    big_body = b'{"prompt": "' + b"A" * (11 * 1024 * 1024) + b'"}'
    resp = api.post("/v1/jobs", headers={
        **_job_headers(headers),
        "Content-Type": "application/json",
    }, content=big_body)
    assert resp.status_code == 413
    assert resp.json().get("error_code") == "BODY_TOO_LARGE"


# ── SQL injection attempts ─────────────────────────────────────────


def test_sql_injection_in_client_id(api, admin_headers):
    resp = api.post("/v1/admin/clients", headers=admin_headers, json={
        "client_id": "'; DROP TABLE--",
    })
    assert resp.status_code == 422


def test_sql_injection_in_prompt(api, test_client):
    _, _, headers = test_client
    resp = api.post("/v1/jobs", headers=_job_headers(headers), json={
        "prompt": "'; DROP TABLE jobs; --",
    })
    assert resp.status_code == 202


# ── XSS payloads ──────────────────────────────────────────────────


def test_xss_in_description(api, admin_headers):
    from helpers.test_data import random_suffix
    cid = f"test-client-{random_suffix()}"
    xss = "<script>alert(1)</script>"
    resp = api.post("/v1/admin/clients", headers=admin_headers, json={
        "client_id": cid,
        "description": xss,
    })
    assert resp.status_code == 201

    # Verify stored verbatim
    resp = api.get(f"/v1/admin/clients/{cid}", headers=admin_headers)
    assert resp.json()["description"] == xss
    api.delete(f"/v1/admin/clients/{cid}", headers=admin_headers)


# ── Special characters ─────────────────────────────────────────────


def test_unicode_in_prompt(api, test_client):
    _, _, headers = test_client
    resp = api.post("/v1/jobs", headers=_job_headers(headers), json={
        "prompt": "Analyze for \u6f0f\u6d1e vulnerabilities",
    })
    assert resp.status_code == 202


def test_null_bytes_in_prompt(api, test_client):
    _, _, headers = test_client
    resp = api.post("/v1/jobs", headers=_job_headers(headers), json={
        "prompt": "test\x00injection",
    })
    # Either 422 (validation rejects) or 202 (accepted) — document actual behavior
    assert resp.status_code in (422, 202)


def test_extremely_long_prompt(api, test_client):
    _, _, headers = test_client
    resp = api.post("/v1/jobs", headers=_job_headers(headers), json={
        "prompt": "A" * 100_000,
    })
    assert resp.status_code == 202


def test_prompt_exceeds_max_length(api, test_client):
    _, _, headers = test_client
    resp = api.post("/v1/jobs", headers=_job_headers(headers), json={
        "prompt": "A" * 100_001,
    })
    assert resp.status_code == 422


# ── Path parameter injection ──────────────────────────────────────


def test_path_traversal_in_client_id(api, admin_headers):
    # URL path traversal gets normalized by the HTTP stack, so use encoded dots
    # or a value that fails the pattern validation instead
    resp = api.get("/v1/admin/clients/..%2F..%2Fetc%2Fpasswd", headers=admin_headers)
    assert resp.status_code in (404, 422)


def test_path_traversal_in_agent_name(api, admin_headers):
    resp = api.get("/v1/admin/agents/..%2F..%2Fetc%2Fpasswd", headers=admin_headers)
    assert resp.status_code in (404, 422)


# ── Type confusion ─────────────────────────────────────────────────


def test_timeout_as_string(api, test_client):
    _, _, headers = test_client
    resp = api.post("/v1/jobs", headers=_job_headers(headers), json={
        "prompt": "test",
        "timeout_seconds": "not_a_number",
    })
    assert resp.status_code == 422


def test_role_invalid_value(api, admin_headers):
    from helpers.test_data import random_suffix
    resp = api.post("/v1/admin/clients", headers=admin_headers, json={
        "client_id": f"test-client-{random_suffix()}",
        "role": "superadmin",
    })
    assert resp.status_code == 422


# ── Content-Type mismatch ─────────────────────────────────────────


def test_json_endpoint_with_form_data(api, test_client):
    _, _, headers = test_client
    resp = api.post("/v1/jobs", headers={
        **_job_headers(headers),
        "Content-Type": "application/x-www-form-urlencoded",
    }, content=b"prompt=test")
    assert resp.status_code == 422
