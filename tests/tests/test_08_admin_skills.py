"""Skill management admin endpoint tests (ZIP-based upload)."""

import io
import zipfile

from helpers.test_data import (
    make_skill_content,
    make_skill_zip,
    make_skill_zip_with_scripts,
    make_valid_zip,
    random_suffix,
)


def _skill_name():
    return f"test-skill-{random_suffix()}"


def _upload_skill(api, admin_headers, name, zip_bytes, extra_fields=None):
    """Helper to POST a skill ZIP."""
    files = {"skill_data": ("skill.zip", io.BytesIO(zip_bytes), "application/zip")}
    data = {}
    if extra_fields:
        data.update(extra_fields)
    return api.post("/v1/admin/skills", headers=admin_headers, files=files, data=data)


def _update_skill(api, admin_headers, name, zip_bytes):
    """Helper to PUT a skill ZIP."""
    files = {"skill_data": ("skill.zip", io.BytesIO(zip_bytes), "application/zip")}
    return api.put(f"/v1/admin/skills/{name}", headers=admin_headers, files=files)


# =========================================================================
# Add skill (POST)
# =========================================================================


def test_add_skill_simple(api, admin_headers):
    """Add a skill with just SKILL.md in a root dir."""
    name = _skill_name()
    zip_bytes = make_skill_zip(name)
    resp = _upload_skill(api, admin_headers, name, zip_bytes)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == name
    assert body["file_count"] == 1
    assert body["skill_size_bytes"] > 0
    api.delete(f"/v1/admin/skills/{name}", headers=admin_headers)


def test_add_skill_with_subdirs(api, admin_headers):
    """Add a skill with scripts/ and references/ subdirectories."""
    name = _skill_name()
    zip_bytes = make_skill_zip_with_scripts(name)
    resp = _upload_skill(api, admin_headers, name, zip_bytes)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == name
    assert body["file_count"] == 4  # SKILL.md + 2 scripts + 1 reference
    api.delete(f"/v1/admin/skills/{name}", headers=admin_headers)


def test_add_skill_with_name_override(api, admin_headers):
    """Add a skill overriding the name from the ZIP root dir."""
    zip_name = _skill_name()
    override_name = _skill_name()
    # The ZIP root dir is zip_name, but we override to override_name.
    # SKILL.md frontmatter must NOT have a name field, or it must match override.
    skill_md = (
        f"---\ndescription: Test skill\n---\n\nYou are a test skill.\n"
    )
    zip_bytes = make_valid_zip({f"{zip_name}/SKILL.md": skill_md})
    resp = _upload_skill(api, admin_headers, override_name, zip_bytes, {"name": override_name})
    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == override_name
    api.delete(f"/v1/admin/skills/{override_name}", headers=admin_headers)


def test_add_skill_flat_layout_with_name(api, admin_headers):
    """Add a skill with flat layout (SKILL.md at archive root) + name field."""
    name = _skill_name()
    zip_bytes = make_skill_zip(name, flat=True)
    resp = _upload_skill(api, admin_headers, name, zip_bytes, {"name": name})
    assert resp.status_code == 201, resp.text
    api.delete(f"/v1/admin/skills/{name}", headers=admin_headers)


def test_add_skill_flat_layout_without_name_returns_400(api, admin_headers):
    """Flat layout without name field should fail."""
    name = _skill_name()
    zip_bytes = make_skill_zip(name, flat=True)
    resp = _upload_skill(api, admin_headers, name, zip_bytes)
    assert resp.status_code == 400


def test_add_skill_duplicate_returns_409(api, admin_headers):
    name = _skill_name()
    zip_bytes = make_skill_zip(name)
    _upload_skill(api, admin_headers, name, zip_bytes)
    resp = _upload_skill(api, admin_headers, name, zip_bytes)
    assert resp.status_code == 409
    api.delete(f"/v1/admin/skills/{name}", headers=admin_headers)


def test_add_skill_invalid_zip_returns_400(api, admin_headers):
    """Not a valid ZIP file."""
    files = {"skill_data": ("skill.zip", io.BytesIO(b"not a zip"), "application/zip")}
    resp = api.post("/v1/admin/skills", headers=admin_headers, files=files)
    assert resp.status_code == 400


def test_add_skill_empty_file_returns_400(api, admin_headers):
    """Empty upload."""
    files = {"skill_data": ("skill.zip", io.BytesIO(b""), "application/zip")}
    resp = api.post("/v1/admin/skills", headers=admin_headers, files=files)
    assert resp.status_code == 400


def test_add_skill_missing_skill_md_returns_400(api, admin_headers):
    """ZIP with no SKILL.md."""
    name = _skill_name()
    zip_bytes = make_valid_zip({f"{name}/readme.txt": "hello"})
    resp = _upload_skill(api, admin_headers, name, zip_bytes)
    assert resp.status_code == 400


def test_add_skill_invalid_frontmatter_returns_422(api, admin_headers):
    """SKILL.md with invalid YAML frontmatter."""
    name = _skill_name()
    bad_content = "---\n: invalid: yaml: {{{\n---\nBody text"
    zip_bytes = make_valid_zip({f"{name}/SKILL.md": bad_content})
    resp = _upload_skill(api, admin_headers, name, zip_bytes)
    assert resp.status_code == 422


def test_add_skill_missing_description_returns_422(api, admin_headers):
    """SKILL.md frontmatter missing description."""
    name = _skill_name()
    content = f"---\nname: {name}\n---\n\nBody text.\n"
    zip_bytes = make_valid_zip({f"{name}/SKILL.md": content})
    resp = _upload_skill(api, admin_headers, name, zip_bytes)
    assert resp.status_code == 422


def test_add_skill_empty_body_returns_422(api, admin_headers):
    """SKILL.md with empty body."""
    name = _skill_name()
    content = f"---\nname: {name}\ndescription: Test\n---\n"
    zip_bytes = make_valid_zip({f"{name}/SKILL.md": content})
    resp = _upload_skill(api, admin_headers, name, zip_bytes)
    assert resp.status_code == 422



def test_add_skill_path_traversal_returns_400(api, admin_headers):
    """ZIP with path traversal attempt."""
    name = _skill_name()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{name}/SKILL.md", make_skill_content(name))
        zf.writestr(f"{name}/../../etc/passwd", "root:x:0:0")
    zip_bytes = buf.getvalue()
    resp = _upload_skill(api, admin_headers, name, zip_bytes)
    assert resp.status_code == 400


def test_add_skill_symlink_returns_400(api, admin_headers):
    """ZIP with a symlink entry."""
    name = _skill_name()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{name}/SKILL.md", make_skill_content(name))
        # Create a symlink entry manually
        info = zipfile.ZipInfo(f"{name}/scripts/link.py")
        info.external_attr = (0o120777 << 16)  # symlink flag
        zf.writestr(info, "/etc/passwd")
    zip_bytes = buf.getvalue()
    resp = _upload_skill(api, admin_headers, name, zip_bytes)
    assert resp.status_code == 400


def test_add_skill_invalid_filename_returns_400(api, admin_headers):
    """ZIP with invalid filename characters."""
    name = _skill_name()
    zip_bytes = make_skill_zip(name, extra_files={
        "scripts/.hidden-file": "hidden",
    })
    resp = _upload_skill(api, admin_headers, name, zip_bytes)
    assert resp.status_code == 400


# =========================================================================
# List skills (GET)
# =========================================================================


def test_list_skills(api, admin_headers):
    resp = api.get("/v1/admin/skills", headers=admin_headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_list_skills_includes_file_count(api, admin_headers):
    name = _skill_name()
    zip_bytes = make_skill_zip_with_scripts(name)
    _upload_skill(api, admin_headers, name, zip_bytes)
    resp = api.get("/v1/admin/skills", headers=admin_headers)
    assert resp.status_code == 200
    found = [s for s in resp.json() if s["name"] == name]
    assert len(found) == 1
    assert found[0]["file_count"] == 4
    api.delete(f"/v1/admin/skills/{name}", headers=admin_headers)


# =========================================================================
# Get skill detail (GET /{name})
# =========================================================================


def test_get_skill_detail(api, admin_headers):
    name = _skill_name()
    zip_bytes = make_skill_zip_with_scripts(name)
    _upload_skill(api, admin_headers, name, zip_bytes)
    resp = api.get(f"/v1/admin/skills/{name}", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "frontmatter" in body
    assert "body_preview" in body
    assert "file_listing" in body
    assert "SKILL.md" in body["file_listing"]
    # Check subdirectory files appear
    listing = body["file_listing"]
    assert any("scripts/" in f for f in listing)
    assert any("references/" in f for f in listing)
    assert body["file_count"] == 4
    api.delete(f"/v1/admin/skills/{name}", headers=admin_headers)


def test_get_skill_not_found(api, admin_headers):
    resp = api.get(f"/v1/admin/skills/nonexistent-{random_suffix()}", headers=admin_headers)
    assert resp.status_code == 404


# =========================================================================
# Update skill (PUT)
# =========================================================================


def test_update_skill(api, admin_headers):
    name = _skill_name()
    zip_bytes = make_skill_zip(name)
    _upload_skill(api, admin_headers, name, zip_bytes)

    # Update with new ZIP that includes scripts
    new_zip = make_skill_zip_with_scripts(name)
    resp = _update_skill(api, admin_headers, name, new_zip)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["file_count"] == 4
    api.delete(f"/v1/admin/skills/{name}", headers=admin_headers)


def test_update_skill_not_found(api, admin_headers):
    name = f"nonexistent-{random_suffix()}"
    zip_bytes = make_skill_zip(name)
    resp = _update_skill(api, admin_headers, name, zip_bytes)
    assert resp.status_code == 404


def test_update_skill_replaces_directory(api, admin_headers):
    """Verify that update fully replaces the skill directory."""
    name = _skill_name()

    # First: create with scripts
    zip_bytes = make_skill_zip_with_scripts(name)
    _upload_skill(api, admin_headers, name, zip_bytes)

    # Verify initial state
    resp = api.get(f"/v1/admin/skills/{name}", headers=admin_headers)
    assert resp.json()["file_count"] == 4

    # Update: now with just SKILL.md (no scripts)
    new_zip = make_skill_zip(name, description="Updated skill")
    resp = _update_skill(api, admin_headers, name, new_zip)
    assert resp.status_code == 200
    assert resp.json()["file_count"] == 1

    # Verify old scripts are gone
    detail = api.get(f"/v1/admin/skills/{name}", headers=admin_headers).json()
    assert detail["file_count"] == 1
    assert detail["file_listing"] == ["SKILL.md"]

    api.delete(f"/v1/admin/skills/{name}", headers=admin_headers)


# =========================================================================
# Delete skill (DELETE)
# =========================================================================


def test_delete_skill(api, admin_headers):
    name = _skill_name()
    zip_bytes = make_skill_zip(name)
    _upload_skill(api, admin_headers, name, zip_bytes)
    resp = api.delete(f"/v1/admin/skills/{name}", headers=admin_headers)
    assert resp.status_code == 204


def test_delete_skill_not_found(api, admin_headers):
    resp = api.delete(f"/v1/admin/skills/nonexistent-{random_suffix()}", headers=admin_headers)
    assert resp.status_code == 404
