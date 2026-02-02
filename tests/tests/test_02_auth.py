"""Authentication edge case tests."""

from helpers.test_data import make_valid_zip


def test_no_auth_header_returns_401(api):
    resp = api.get("/v1/admin/status")
    assert resp.status_code == 401


def test_empty_bearer_returns_401(api):
    # "Bearer " with trailing space is rejected by httpx as an illegal header.
    # Use "Bearer x" (single char) — still an invalid token for the server.
    resp = api.get("/v1/admin/status", headers={"Authorization": "Bearer x"})
    assert resp.status_code == 401


def test_invalid_token_returns_401(api):
    resp = api.get("/v1/admin/status", headers={"Authorization": "Bearer invalid_token_xxx"})
    assert resp.status_code == 401


def test_malformed_auth_no_bearer(api):
    resp = api.get("/v1/admin/status", headers={"Authorization": "Token abc123"})
    assert resp.status_code == 401


def test_malformed_auth_extra_parts(api):
    resp = api.get("/v1/admin/status", headers={"Authorization": "Bearer abc 123"})
    assert resp.status_code == 401


def test_deactivated_client_returns_401(api, admin_headers, test_client):
    client_id, _, client_headers = test_client
    # Deactivate
    resp = api.post(f"/v1/admin/clients/{client_id}/deactivate", headers=admin_headers)
    assert resp.status_code == 200

    # Try to use deactivated client
    zip_bytes = make_valid_zip()
    resp = api.post("/v1/uploads", headers=client_headers,
                    files={"file": ("test.zip", zip_bytes, "application/zip")})
    assert resp.status_code == 401

    # Reactivate for teardown
    api.post(f"/v1/admin/clients/{client_id}/activate", headers=admin_headers)


def test_reactivated_client_works(api, admin_headers, test_client):
    client_id, _, client_headers = test_client
    # Deactivate then reactivate
    api.post(f"/v1/admin/clients/{client_id}/deactivate", headers=admin_headers)
    api.post(f"/v1/admin/clients/{client_id}/activate", headers=admin_headers)

    # Should work again — upload a valid ZIP
    zip_bytes = make_valid_zip()
    resp = api.post("/v1/uploads", headers=client_headers,
                    files={"file": ("test.zip", zip_bytes, "application/zip")})
    assert resp.status_code != 401, f"Expected non-401 after reactivation, got {resp.status_code}"


def test_client_role_forbidden_on_admin(api, test_client):
    _, _, client_headers = test_client
    resp = api.get("/v1/admin/status", headers=client_headers)
    assert resp.status_code == 403


def test_client_role_forbidden_on_admin_clients(api, test_client):
    _, _, client_headers = test_client
    resp = api.get("/v1/admin/clients", headers=client_headers)
    assert resp.status_code == 403


def test_admin_role_can_access_client_endpoints(api, admin_headers):
    zip_bytes = make_valid_zip()
    resp = api.post("/v1/uploads", headers=admin_headers,
                    files={"file": ("test.zip", zip_bytes, "application/zip")})
    assert resp.status_code == 201
