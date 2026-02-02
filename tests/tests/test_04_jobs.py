"""Job creation and retrieval tests."""

from helpers.test_data import make_valid_zip

# Dummy Anthropic key that passes length validation but won't actually work
DUMMY_ANTHROPIC_KEY = "sk-ant-test-dummy-key-for-testing-00000"


def _job_headers(client_headers: dict) -> dict:
    return {**client_headers, "X-Anthropic-Key": DUMMY_ANTHROPIC_KEY}


# ── Job creation tests ─────────────────────────────────────────────


def test_create_job_with_upload(api, test_client):
    _, _, headers = test_client
    # Create upload first
    zip_bytes = make_valid_zip()
    upload_resp = api.post("/v1/uploads", headers=headers,
                           files={"file": ("test.zip", zip_bytes, "application/zip")})
    assert upload_resp.status_code == 201
    upload_id = upload_resp.json()["upload_id"]

    resp = api.post("/v1/jobs", headers=_job_headers(headers), json={
        "upload_id": upload_id,
        "prompt": "Analyze this code",
    })
    assert resp.status_code == 202
    body = resp.json()
    assert "job_id" in body
    assert body["status"] == "PENDING"
    assert "created_at" in body


def test_create_job_without_upload(api, test_client):
    _, _, headers = test_client
    resp = api.post("/v1/jobs", headers=_job_headers(headers), json={
        "prompt": "What is 2+2?",
    })
    assert resp.status_code == 202


def test_create_job_with_all_optional_fields(api, test_client):
    _, _, headers = test_client
    zip_bytes = make_valid_zip()
    upload_resp = api.post("/v1/uploads", headers=headers,
                           files={"file": ("test.zip", zip_bytes, "application/zip")})
    upload_id = upload_resp.json()["upload_id"]

    resp = api.post("/v1/jobs", headers=_job_headers(headers), json={
        "upload_id": upload_id,
        "prompt": "Analyze this code",
        "claude_md": "You are a security auditor.",
        "timeout_seconds": 120,
        "model": "claude-sonnet-4-20250514",
    })
    assert resp.status_code == 202


def test_create_job_missing_prompt_returns_422(api, test_client):
    _, _, headers = test_client
    resp = api.post("/v1/jobs", headers=_job_headers(headers), json={
        "upload_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
    })
    assert resp.status_code == 422


def test_create_job_empty_prompt_returns_422(api, test_client):
    _, _, headers = test_client
    resp = api.post("/v1/jobs", headers=_job_headers(headers), json={
        "prompt": "",
    })
    assert resp.status_code == 422


def test_create_job_whitespace_prompt_returns_422(api, test_client):
    _, _, headers = test_client
    resp = api.post("/v1/jobs", headers=_job_headers(headers), json={
        "prompt": "   ",
    })
    assert resp.status_code == 422


def test_create_job_missing_anthropic_key_returns_400(api, test_client):
    _, _, headers = test_client
    resp = api.post("/v1/jobs", headers=headers, json={
        "prompt": "test",
    })
    assert resp.status_code == 400


def test_create_job_empty_anthropic_key_returns_400(api, test_client):
    _, _, headers = test_client
    resp = api.post("/v1/jobs", headers={**headers, "X-Anthropic-Key": ""}, json={
        "prompt": "test",
    })
    assert resp.status_code == 400


def test_create_job_short_anthropic_key_returns_400(api, test_client):
    _, _, headers = test_client
    resp = api.post("/v1/jobs", headers={**headers, "X-Anthropic-Key": "short"}, json={
        "prompt": "test",
    })
    assert resp.status_code == 400


def test_create_job_invalid_upload_id_format_returns_422(api, test_client):
    _, _, headers = test_client
    resp = api.post("/v1/jobs", headers=_job_headers(headers), json={
        "upload_id": "not-a-uuid",
        "prompt": "test",
    })
    assert resp.status_code == 422


def test_create_job_nonexistent_upload_returns_400(api, test_client):
    _, _, headers = test_client
    resp = api.post("/v1/jobs", headers=_job_headers(headers), json={
        "upload_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
        "prompt": "test",
    })
    assert resp.status_code == 400
    assert resp.json().get("error_code") in ("UPLOAD_ERROR", "JOB_ERROR")


def test_create_job_invalid_model_format_returns_422(api, test_client):
    _, _, headers = test_client
    resp = api.post("/v1/jobs", headers=_job_headers(headers), json={
        "prompt": "test",
        "model": "-invalid",
    })
    assert resp.status_code == 422


def test_create_job_timeout_below_min_returns_422(api, test_client):
    _, _, headers = test_client
    resp = api.post("/v1/jobs", headers=_job_headers(headers), json={
        "prompt": "test",
        "timeout_seconds": 10,
    })
    assert resp.status_code == 422


def test_create_job_timeout_above_max_returns_422(api, test_client):
    _, _, headers = test_client
    resp = api.post("/v1/jobs", headers=_job_headers(headers), json={
        "prompt": "test",
        "timeout_seconds": 99999,
    })
    assert resp.status_code == 422


# ── Job retrieval tests ────────────────────────────────────────────


def test_get_job_valid_id(api, test_client):
    _, _, headers = test_client
    # Create a job first
    create_resp = api.post("/v1/jobs", headers=_job_headers(headers), json={
        "prompt": "test retrieval",
    })
    assert create_resp.status_code == 202
    job_id = create_resp.json()["job_id"]

    resp = api.get(f"/v1/jobs/{job_id}", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == job_id
    assert "status" in body
    assert "created_at" in body


def test_get_job_invalid_id_format_returns_422(api, test_client):
    _, _, headers = test_client
    resp = api.get("/v1/jobs/not-a-job-id", headers=headers)
    assert resp.status_code == 422


def test_get_job_nonexistent_returns_404(api, test_client):
    _, _, headers = test_client
    resp = api.get("/v1/jobs/job_aaaaaaaaaaaa", headers=headers)
    assert resp.status_code == 404


def test_get_job_response_fields_pending(api, test_client):
    _, _, headers = test_client
    create_resp = api.post("/v1/jobs", headers=_job_headers(headers), json={
        "prompt": "test pending fields",
    })
    job_id = create_resp.json()["job_id"]

    resp = api.get(f"/v1/jobs/{job_id}", headers=headers)
    body = resp.json()
    assert body["status"] in ("PENDING", "RUNNING")
    if body["status"] == "PENDING":
        assert body["output"] is None


def test_job_id_format_validation(api, test_client):
    _, _, headers = test_client
    resp = api.get("/v1/jobs/job_AAAAAAAAAAAA", headers=headers)
    assert resp.status_code == 422
