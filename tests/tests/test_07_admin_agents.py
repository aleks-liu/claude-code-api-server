"""Agent management admin endpoint tests."""

import base64

from helpers.test_data import make_agent_content, random_suffix


def _agent_name():
    return f"test-agent-{random_suffix()}"


def test_add_agent(api, admin_headers):
    name = _agent_name()
    content = make_agent_content(name)
    resp = api.post("/v1/admin/agents", headers=admin_headers, json={
        "name": name,
        "content": content,
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == name
    assert "description" in body
    assert "prompt_size_bytes" in body
    assert "added_at" in body
    api.delete(f"/v1/admin/agents/{name}", headers=admin_headers)


def test_add_agent_with_base64(api, admin_headers):
    name = _agent_name()
    content = make_agent_content(name)
    b64 = base64.b64encode(content.encode()).decode()
    resp = api.post("/v1/admin/agents", headers=admin_headers, json={
        "name": name,
        "content_base64": b64,
    })
    assert resp.status_code == 201
    api.delete(f"/v1/admin/agents/{name}", headers=admin_headers)


def test_add_agent_duplicate_returns_409(api, admin_headers):
    name = _agent_name()
    content = make_agent_content(name)
    api.post("/v1/admin/agents", headers=admin_headers, json={"name": name, "content": content})
    resp = api.post("/v1/admin/agents", headers=admin_headers, json={"name": name, "content": content})
    assert resp.status_code == 409
    api.delete(f"/v1/admin/agents/{name}", headers=admin_headers)


def test_add_agent_missing_content_returns_400(api, admin_headers):
    resp = api.post("/v1/admin/agents", headers=admin_headers, json={
        "name": _agent_name(),
    })
    assert resp.status_code == 400


def test_add_agent_both_content_fields_returns_400(api, admin_headers):
    name = _agent_name()
    content = make_agent_content(name)
    resp = api.post("/v1/admin/agents", headers=admin_headers, json={
        "name": name,
        "content": content,
        "content_base64": base64.b64encode(content.encode()).decode(),
    })
    assert resp.status_code == 400


def test_add_agent_invalid_frontmatter_returns_422(api, admin_headers):
    resp = api.post("/v1/admin/agents", headers=admin_headers, json={
        "name": _agent_name(),
        "content": "---\n: invalid: yaml: {{{\n---\nBody text",
    })
    assert resp.status_code == 422


def test_add_agent_name_mismatch_returns_422(api, admin_headers):
    name = _agent_name()
    content = make_agent_content("other-name")
    resp = api.post("/v1/admin/agents", headers=admin_headers, json={
        "name": name,
        "content": content,
    })
    assert resp.status_code == 422


def test_add_agent_missing_description_returns_422(api, admin_headers):
    name = _agent_name()
    content = f"---\nname: {name}\ntools: Read\n---\n\nBody text.\n"
    resp = api.post("/v1/admin/agents", headers=admin_headers, json={
        "name": name,
        "content": content,
    })
    assert resp.status_code == 422


def test_add_agent_empty_body_returns_422(api, admin_headers):
    name = _agent_name()
    content = f"---\nname: {name}\ndescription: Test\ntools: Read\n---\n"
    resp = api.post("/v1/admin/agents", headers=admin_headers, json={
        "name": name,
        "content": content,
    })
    assert resp.status_code == 422


def test_add_agent_invalid_name_returns_422(api, admin_headers):
    resp = api.post("/v1/admin/agents", headers=admin_headers, json={
        "name": "has spaces!",
        "content": make_agent_content("has spaces!"),
    })
    assert resp.status_code == 422


def test_add_agent_uppercase_name_accepted(api, admin_headers):
    name = f"UpperAgent-{_agent_name()}"
    content = make_agent_content(name)
    resp = api.post("/v1/admin/agents", headers=admin_headers, json={
        "name": name,
        "content": content,
    })
    assert resp.status_code == 201
    api.delete(f"/v1/admin/agents/{name}", headers=admin_headers)


def test_list_agents(api, admin_headers):
    resp = api.get("/v1/admin/agents", headers=admin_headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_get_agent_detail(api, admin_headers):
    name = _agent_name()
    content = make_agent_content(name)
    api.post("/v1/admin/agents", headers=admin_headers, json={"name": name, "content": content})
    resp = api.get(f"/v1/admin/agents/{name}", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "frontmatter" in body
    assert "body_preview" in body
    api.delete(f"/v1/admin/agents/{name}", headers=admin_headers)


def test_get_agent_not_found(api, admin_headers):
    resp = api.get(f"/v1/admin/agents/nonexistent-{random_suffix()}", headers=admin_headers)
    assert resp.status_code == 404


def test_update_agent_content(api, admin_headers):
    name = _agent_name()
    content = make_agent_content(name)
    api.post("/v1/admin/agents", headers=admin_headers, json={"name": name, "content": content})

    new_content = make_agent_content(name, description="Updated agent")
    resp = api.put(f"/v1/admin/agents/{name}", headers=admin_headers, json={
        "content": new_content,
    })
    assert resp.status_code == 200
    assert resp.json()["prompt_size_bytes"] > 0
    api.delete(f"/v1/admin/agents/{name}", headers=admin_headers)


def test_update_agent_description_only(api, admin_headers):
    name = _agent_name()
    content = make_agent_content(name)
    api.post("/v1/admin/agents", headers=admin_headers, json={"name": name, "content": content})
    resp = api.put(f"/v1/admin/agents/{name}", headers=admin_headers, json={
        "description": "New description",
    })
    assert resp.status_code == 200
    api.delete(f"/v1/admin/agents/{name}", headers=admin_headers)


def test_update_agent_nothing_returns_400(api, admin_headers):
    name = _agent_name()
    content = make_agent_content(name)
    api.post("/v1/admin/agents", headers=admin_headers, json={"name": name, "content": content})
    resp = api.put(f"/v1/admin/agents/{name}", headers=admin_headers, json={})
    assert resp.status_code == 400
    api.delete(f"/v1/admin/agents/{name}", headers=admin_headers)


def test_update_agent_not_found(api, admin_headers):
    resp = api.put(f"/v1/admin/agents/nonexistent-{random_suffix()}", headers=admin_headers, json={
        "description": "nope",
    })
    assert resp.status_code == 404


def test_delete_agent(api, admin_headers):
    name = _agent_name()
    content = make_agent_content(name)
    api.post("/v1/admin/agents", headers=admin_headers, json={"name": name, "content": content})
    resp = api.delete(f"/v1/admin/agents/{name}", headers=admin_headers)
    assert resp.status_code == 204


def test_delete_agent_not_found(api, admin_headers):
    resp = api.delete(f"/v1/admin/agents/nonexistent-{random_suffix()}", headers=admin_headers)
    assert resp.status_code == 404
