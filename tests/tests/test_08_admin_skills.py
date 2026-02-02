"""Skill management admin endpoint tests."""

import base64

from helpers.test_data import make_skill_content, random_suffix


def _skill_name():
    return f"test-skill-{random_suffix()}"


def test_add_skill(api, admin_headers):
    name = _skill_name()
    content = make_skill_content(name)
    resp = api.post("/v1/admin/skills", headers=admin_headers, json={
        "name": name,
        "content": content,
    })
    assert resp.status_code == 201
    api.delete(f"/v1/admin/skills/{name}", headers=admin_headers)


def test_add_skill_with_base64(api, admin_headers):
    name = _skill_name()
    content = make_skill_content(name)
    b64 = base64.b64encode(content.encode()).decode()
    resp = api.post("/v1/admin/skills", headers=admin_headers, json={
        "name": name,
        "content_base64": b64,
    })
    assert resp.status_code == 201
    api.delete(f"/v1/admin/skills/{name}", headers=admin_headers)


def test_add_skill_duplicate_returns_409(api, admin_headers):
    name = _skill_name()
    content = make_skill_content(name)
    api.post("/v1/admin/skills", headers=admin_headers, json={"name": name, "content": content})
    resp = api.post("/v1/admin/skills", headers=admin_headers, json={"name": name, "content": content})
    assert resp.status_code == 409
    api.delete(f"/v1/admin/skills/{name}", headers=admin_headers)


def test_add_skill_missing_content_returns_400(api, admin_headers):
    resp = api.post("/v1/admin/skills", headers=admin_headers, json={
        "name": _skill_name(),
    })
    assert resp.status_code == 400


def test_add_skill_both_content_fields_returns_400(api, admin_headers):
    name = _skill_name()
    content = make_skill_content(name)
    resp = api.post("/v1/admin/skills", headers=admin_headers, json={
        "name": name,
        "content": content,
        "content_base64": base64.b64encode(content.encode()).decode(),
    })
    assert resp.status_code == 400


def test_add_skill_invalid_frontmatter_returns_422(api, admin_headers):
    resp = api.post("/v1/admin/skills", headers=admin_headers, json={
        "name": _skill_name(),
        "content": "---\n: invalid: yaml: {{{\n---\nBody text",
    })
    assert resp.status_code == 422


def test_add_skill_missing_description_returns_422(api, admin_headers):
    name = _skill_name()
    content = f"---\nname: {name}\n---\n\nBody text.\n"
    resp = api.post("/v1/admin/skills", headers=admin_headers, json={
        "name": name,
        "content": content,
    })
    assert resp.status_code == 422


def test_add_skill_empty_body_returns_422(api, admin_headers):
    name = _skill_name()
    content = f"---\nname: {name}\ndescription: Test\n---\n"
    resp = api.post("/v1/admin/skills", headers=admin_headers, json={
        "name": name,
        "content": content,
    })
    assert resp.status_code == 422


def test_add_skill_invalid_name_returns_422(api, admin_headers):
    resp = api.post("/v1/admin/skills", headers=admin_headers, json={
        "name": "UPPERCASE",
        "content": make_skill_content("UPPERCASE"),
    })
    assert resp.status_code == 422


def test_list_skills(api, admin_headers):
    resp = api.get("/v1/admin/skills", headers=admin_headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_get_skill_detail(api, admin_headers):
    name = _skill_name()
    content = make_skill_content(name)
    api.post("/v1/admin/skills", headers=admin_headers, json={"name": name, "content": content})
    resp = api.get(f"/v1/admin/skills/{name}", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "frontmatter" in body
    assert "body_preview" in body
    api.delete(f"/v1/admin/skills/{name}", headers=admin_headers)


def test_get_skill_not_found(api, admin_headers):
    resp = api.get(f"/v1/admin/skills/nonexistent-{random_suffix()}", headers=admin_headers)
    assert resp.status_code == 404


def test_update_skill_content(api, admin_headers):
    name = _skill_name()
    content = make_skill_content(name)
    api.post("/v1/admin/skills", headers=admin_headers, json={"name": name, "content": content})
    new_content = make_skill_content(name, description="Updated skill")
    resp = api.put(f"/v1/admin/skills/{name}", headers=admin_headers, json={
        "content": new_content,
    })
    assert resp.status_code == 200
    api.delete(f"/v1/admin/skills/{name}", headers=admin_headers)


def test_update_skill_description_only(api, admin_headers):
    name = _skill_name()
    content = make_skill_content(name)
    api.post("/v1/admin/skills", headers=admin_headers, json={"name": name, "content": content})
    resp = api.put(f"/v1/admin/skills/{name}", headers=admin_headers, json={
        "description": "New description",
    })
    assert resp.status_code == 200
    api.delete(f"/v1/admin/skills/{name}", headers=admin_headers)


def test_update_skill_nothing_returns_400(api, admin_headers):
    name = _skill_name()
    content = make_skill_content(name)
    api.post("/v1/admin/skills", headers=admin_headers, json={"name": name, "content": content})
    resp = api.put(f"/v1/admin/skills/{name}", headers=admin_headers, json={})
    assert resp.status_code == 400
    api.delete(f"/v1/admin/skills/{name}", headers=admin_headers)


def test_update_skill_not_found(api, admin_headers):
    resp = api.put(f"/v1/admin/skills/nonexistent-{random_suffix()}", headers=admin_headers, json={
        "description": "nope",
    })
    assert resp.status_code == 404


def test_delete_skill(api, admin_headers):
    name = _skill_name()
    content = make_skill_content(name)
    api.post("/v1/admin/skills", headers=admin_headers, json={"name": name, "content": content})
    resp = api.delete(f"/v1/admin/skills/{name}", headers=admin_headers)
    assert resp.status_code == 204


def test_delete_skill_not_found(api, admin_headers):
    resp = api.delete(f"/v1/admin/skills/nonexistent-{random_suffix()}", headers=admin_headers)
    assert resp.status_code == 404
