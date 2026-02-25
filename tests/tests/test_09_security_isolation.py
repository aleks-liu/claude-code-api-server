"""Security isolation tests — BOLA, role enforcement, deactivated clients."""

from helpers.test_data import make_valid_zip

DUMMY_ANTHROPIC_KEY = "sk-ant-test-dummy-key-for-testing-00000"


# ── BOLA — Jobs ────────────────────────────────────────────────────


def test_client_cannot_read_other_clients_job(api, test_client, second_test_client):
    _, _, headers_a = test_client
    _, _, headers_b = second_test_client

    # Client A creates a job
    resp = api.post("/v1/jobs", headers={**headers_a, "X-Anthropic-Key": DUMMY_ANTHROPIC_KEY}, json={
        "prompt": "test isolation",
    })
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    # Client B tries to read it
    resp = api.get(f"/v1/jobs/{job_id}", headers=headers_b)
    assert resp.status_code == 404  # not 403 — prevents enumeration


def test_client_can_read_own_job(api, test_client):
    _, _, headers = test_client
    resp = api.post("/v1/jobs", headers={**headers, "X-Anthropic-Key": DUMMY_ANTHROPIC_KEY}, json={
        "prompt": "test own job",
    })
    assert resp.status_code == 202
    job_id = resp.json()["job_id"]

    resp = api.get(f"/v1/jobs/{job_id}", headers=headers)
    assert resp.status_code == 200


# ── BOLA — Uploads ─────────────────────────────────────────────────


def test_client_cannot_use_other_clients_upload(api, test_client, second_test_client):
    _, _, headers_a = test_client
    _, _, headers_b = second_test_client

    # Client A uploads
    zip_bytes = make_valid_zip()
    resp = api.post("/v1/uploads", headers=headers_a,
                    files={"file": ("test.zip", zip_bytes, "application/zip")})
    assert resp.status_code == 201
    upload_id = resp.json()["upload_id"]

    # Client B tries to use it
    resp = api.post("/v1/jobs", headers={**headers_b, "X-Anthropic-Key": DUMMY_ANTHROPIC_KEY}, json={
        "upload_ids": [upload_id],
        "prompt": "steal upload",
    })
    # App may return 403 (explicit ownership check) or 400 (upload not found for this client)
    assert resp.status_code in (400, 403)


def test_client_can_use_own_upload(api, test_client):
    _, _, headers = test_client
    zip_bytes = make_valid_zip()
    resp = api.post("/v1/uploads", headers=headers,
                    files={"file": ("test.zip", zip_bytes, "application/zip")})
    upload_id = resp.json()["upload_id"]

    resp = api.post("/v1/jobs", headers={**headers, "X-Anthropic-Key": DUMMY_ANTHROPIC_KEY}, json={
        "upload_ids": [upload_id],
        "prompt": "use own upload",
    })
    assert resp.status_code == 202


# ── Role Enforcement — All Admin Endpoints ─────────────────────────


def test_client_forbidden_admin_status(api, test_client):
    _, _, h = test_client
    assert api.get("/v1/admin/status", headers=h).status_code == 403


def test_client_forbidden_list_clients(api, test_client):
    _, _, h = test_client
    assert api.get("/v1/admin/clients", headers=h).status_code == 403


def test_client_forbidden_create_client(api, test_client):
    _, _, h = test_client
    assert api.post("/v1/admin/clients", headers=h, json={"client_id": "x"}).status_code == 403


def test_client_forbidden_list_mcp(api, test_client):
    _, _, h = test_client
    assert api.get("/v1/admin/mcp", headers=h).status_code == 403


def test_client_forbidden_create_mcp(api, test_client):
    _, _, h = test_client
    assert api.post("/v1/admin/mcp", headers=h, json={"name": "x", "type": "stdio", "command": "echo"}).status_code == 403


def test_client_forbidden_list_agents(api, test_client):
    _, _, h = test_client
    assert api.get("/v1/admin/agents", headers=h).status_code == 403


def test_client_forbidden_create_agent(api, test_client):
    _, _, h = test_client
    assert api.post("/v1/admin/agents", headers=h, json={"name": "x", "content": "y"}).status_code == 403


def test_client_forbidden_list_skills(api, test_client):
    _, _, h = test_client
    assert api.get("/v1/admin/skills", headers=h).status_code == 403


def test_client_forbidden_create_skill(api, test_client):
    _, _, h = test_client
    assert api.post("/v1/admin/skills", headers=h, json={"name": "x", "content": "y"}).status_code == 403


# ── Deactivated Client ─────────────────────────────────────────────


def test_deactivated_client_rejected_uploads(api, admin_headers, test_client):
    client_id, _, headers = test_client
    api.post(f"/v1/admin/clients/{client_id}/deactivate", headers=admin_headers)
    zip_bytes = make_valid_zip()
    resp = api.post("/v1/uploads", headers=headers,
                    files={"file": ("test.zip", zip_bytes, "application/zip")})
    assert resp.status_code == 401
    api.post(f"/v1/admin/clients/{client_id}/activate", headers=admin_headers)


def test_deactivated_client_rejected_jobs(api, admin_headers, test_client):
    client_id, _, headers = test_client
    api.post(f"/v1/admin/clients/{client_id}/deactivate", headers=admin_headers)
    resp = api.post("/v1/jobs", headers={**headers, "X-Anthropic-Key": DUMMY_ANTHROPIC_KEY}, json={
        "prompt": "test",
    })
    assert resp.status_code == 401
    api.post(f"/v1/admin/clients/{client_id}/activate", headers=admin_headers)


def test_deactivated_client_rejected_get_job(api, admin_headers, test_client):
    client_id, _, headers = test_client
    api.post(f"/v1/admin/clients/{client_id}/deactivate", headers=admin_headers)
    resp = api.get("/v1/jobs/job_aaaaaaaaaaaa", headers=headers)
    assert resp.status_code == 401
    api.post(f"/v1/admin/clients/{client_id}/activate", headers=admin_headers)
