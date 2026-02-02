"""Health check endpoint tests."""


def test_health_returns_200(api):
    resp = api.get("/v1/health")
    assert resp.status_code == 200


def test_health_response_has_status_field(api):
    resp = api.get("/v1/health")
    assert resp.json()["status"] == "ok"


def test_health_no_auth_required(api):
    resp = api.get("/v1/health")
    assert resp.status_code == 200


def test_health_accepts_arbitrary_auth(api):
    resp = api.get("/v1/health", headers={"Authorization": "Bearer garbage"})
    assert resp.status_code == 200
