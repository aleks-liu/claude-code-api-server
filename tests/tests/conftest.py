"""Shared fixtures, hooks, and ApiClient setup for integration tests."""

import os

import pytest

from helpers.api_client import ApiClient
from helpers.test_data import make_valid_zip, random_suffix

# ── Configuration ──────────────────────────────────────────────────

BASE_URL = os.environ.get("TEST_BASE_URL", "http://127.0.0.1:8080")
ADMIN_API_KEY = os.environ.get("TEST_ADMIN_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


# ── Fixtures ───────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def api() -> ApiClient:
    """Session-scoped API client."""
    return ApiClient(BASE_URL)


@pytest.fixture(scope="session")
def admin_headers() -> dict[str, str]:
    """Headers for admin-authenticated requests."""
    assert ADMIN_API_KEY, "TEST_ADMIN_API_KEY environment variable is required"
    return {"Authorization": f"Bearer {ADMIN_API_KEY}"}



@pytest.fixture
def test_client(api, admin_headers):
    """
    Create a temporary test client via admin API.

    Yields (client_id, api_key, headers).
    Deletes the client on teardown.
    """
    suffix = random_suffix()
    client_id = f"test-client-{suffix}"
    resp = api.post("/v1/admin/clients", headers=admin_headers, json={
        "client_id": client_id,
        "description": "Integration test client",
        "role": "client",
    })
    assert resp.status_code == 201
    api_key = resp.json()["api_key"]
    headers = {"Authorization": f"Bearer {api_key}"}

    yield client_id, api_key, headers

    api.delete(f"/v1/admin/clients/{client_id}", headers=admin_headers)


@pytest.fixture
def second_test_client(api, admin_headers):
    """
    Create a second temporary test client (for cross-client isolation tests).

    Yields (client_id, api_key, headers).
    Deletes the client on teardown.
    """
    suffix = random_suffix()
    client_id = f"test-client2-{suffix}"
    resp = api.post("/v1/admin/clients", headers=admin_headers, json={
        "client_id": client_id,
        "description": "Integration test client 2",
        "role": "client",
    })
    assert resp.status_code == 201
    api_key = resp.json()["api_key"]
    headers = {"Authorization": f"Bearer {api_key}"}

    yield client_id, api_key, headers

    api.delete(f"/v1/admin/clients/{client_id}", headers=admin_headers)


@pytest.fixture
def test_upload(api, test_client):
    """
    Upload a valid test ZIP archive.

    Yields (upload_id, client_headers).
    """
    _, _, headers = test_client
    zip_bytes = make_valid_zip()
    resp = api.post("/v1/uploads", headers=headers,
                    files={"file": ("test.zip", zip_bytes, "application/zip")})
    assert resp.status_code == 201
    upload_id = resp.json()["upload_id"]

    yield upload_id, headers


# ── Pytest Hooks for Failure Metadata ──────────────────────────────


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Attach last request/response pair to test report on failure."""
    outcome = yield
    report = outcome.get_result()

    if report.when == "call" and report.failed:
        api_client = item.funcargs.get("api")
        if api_client and hasattr(api_client, "last_exchange"):
            exchange = api_client.last_exchange
            if exchange:
                report.user_properties.append((
                    "last_api_exchange",
                    {
                        "request": {
                            "method": exchange.request_method,
                            "url": exchange.request_url,
                            "headers": exchange.request_headers,
                            "body": exchange.request_body,
                        },
                        "response": {
                            "status": exchange.response_status,
                            "headers": exchange.response_headers,
                            "body": exchange.response_body,
                        },
                    }
                ))
