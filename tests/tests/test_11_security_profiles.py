"""Security profile admin endpoint tests.

Tests profile CRUD, client-profile binding, built-in profile verification,
and validation edge cases.
"""

from helpers.test_data import random_suffix


# =============================================================================
# 4.1.1 Profile CRUD
# =============================================================================


def test_create_profile_returns_201(api, admin_headers):
    name = f"test-profile-{random_suffix()}"
    resp = api.post("/v1/admin/security-profiles", headers=admin_headers, json={
        "name": name,
        "description": "test",
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == name
    assert body["description"] == "test"
    assert body["is_builtin"] is False
    assert "network" in body
    api.delete(f"/v1/admin/security-profiles/{name}", headers=admin_headers)


def test_create_profile_with_network_policy(api, admin_headers):
    name = f"test-profile-{random_suffix()}"
    network = {
        "allowed_domains": ["github.com", "*.github.com"],
        "denied_domains": [],
        "allowed_ip_ranges": None,
        "denied_ip_ranges": ["10.0.0.0/8"],
        "allow_ip_destination": False,
    }
    resp = api.post("/v1/admin/security-profiles", headers=admin_headers, json={
        "name": name,
        "network": network,
    })
    assert resp.status_code == 201
    body = resp.json()
    assert body["network"]["allowed_domains"] == ["github.com", "*.github.com"]
    assert body["network"]["denied_ip_ranges"] == ["10.0.0.0/8"]
    assert body["network"]["allow_ip_destination"] is False
    api.delete(f"/v1/admin/security-profiles/{name}", headers=admin_headers)


def test_create_profile_with_denied_tools(api, admin_headers):
    name = f"test-profile-{random_suffix()}"
    resp = api.post("/v1/admin/security-profiles", headers=admin_headers, json={
        "name": name,
        "denied_tools": ["Bash", "WebFetch"],
    })
    assert resp.status_code == 201
    assert resp.json()["denied_tools"] == ["Bash", "WebFetch"]
    api.delete(f"/v1/admin/security-profiles/{name}", headers=admin_headers)


def test_create_profile_with_mcp_servers(api, admin_headers):
    name = f"test-profile-{random_suffix()}"
    resp = api.post("/v1/admin/security-profiles", headers=admin_headers, json={
        "name": name,
        "allowed_mcp_servers": ["server-a"],
    })
    assert resp.status_code == 201
    assert resp.json()["allowed_mcp_servers"] == ["server-a"]
    api.delete(f"/v1/admin/security-profiles/{name}", headers=admin_headers)


def test_create_profile_duplicate_returns_409(api, admin_headers):
    name = f"test-profile-{random_suffix()}"
    api.post("/v1/admin/security-profiles", headers=admin_headers, json={"name": name})
    resp = api.post("/v1/admin/security-profiles", headers=admin_headers, json={"name": name})
    assert resp.status_code == 409
    api.delete(f"/v1/admin/security-profiles/{name}", headers=admin_headers)


def test_create_profile_uppercase_name_accepted(api, admin_headers):
    name = f"UpperProfile-{random_suffix()}"
    resp = api.post("/v1/admin/security-profiles", headers=admin_headers, json={
        "name": name,
    })
    assert resp.status_code == 201
    api.delete(f"/v1/admin/security-profiles/{name}", headers=admin_headers)


def test_create_profile_invalid_name_spaces_returns_422(api, admin_headers):
    resp = api.post("/v1/admin/security-profiles", headers=admin_headers, json={
        "name": "has space",
    })
    assert resp.status_code == 422


def test_create_profile_invalid_name_special_chars_returns_422(api, admin_headers):
    resp = api.post("/v1/admin/security-profiles", headers=admin_headers, json={
        "name": "bad!name",
    })
    assert resp.status_code == 422


def test_create_profile_invalid_domain_pattern_returns_422(api, admin_headers):
    name = f"test-profile-{random_suffix()}"
    resp = api.post("/v1/admin/security-profiles", headers=admin_headers, json={
        "name": name,
        "network": {"allowed_domains": ["*.com"]},
    })
    assert resp.status_code == 422


def test_create_profile_invalid_cidr_returns_422(api, admin_headers):
    name = f"test-profile-{random_suffix()}"
    resp = api.post("/v1/admin/security-profiles", headers=admin_headers, json={
        "name": name,
        "network": {"denied_ip_ranges": ["not-a-cidr"]},
    })
    assert resp.status_code == 422


def test_list_profiles_includes_builtins(api, admin_headers):
    resp = api.get("/v1/admin/security-profiles", headers=admin_headers)
    assert resp.status_code == 200
    names = [p["name"] for p in resp.json()]
    assert "unconfined" in names
    assert "common" in names
    assert "restrictive" in names


def test_list_profiles_includes_custom(api, admin_headers):
    name = f"test-profile-{random_suffix()}"
    api.post("/v1/admin/security-profiles", headers=admin_headers, json={"name": name})
    resp = api.get("/v1/admin/security-profiles", headers=admin_headers)
    names = [p["name"] for p in resp.json()]
    assert name in names
    api.delete(f"/v1/admin/security-profiles/{name}", headers=admin_headers)


def test_get_profile_returns_200(api, admin_headers):
    resp = api.get("/v1/admin/security-profiles/common", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "common"
    assert body["is_builtin"] is True


def test_get_profile_not_found_returns_404(api, admin_headers):
    resp = api.get("/v1/admin/security-profiles/nonexistent-xxx", headers=admin_headers)
    assert resp.status_code == 404


def test_update_profile_description(api, admin_headers):
    name = f"test-profile-{random_suffix()}"
    api.post("/v1/admin/security-profiles", headers=admin_headers, json={"name": name})
    resp = api.patch(f"/v1/admin/security-profiles/{name}", headers=admin_headers, json={
        "description": "updated",
    })
    assert resp.status_code == 200
    assert resp.json()["description"] == "updated"
    api.delete(f"/v1/admin/security-profiles/{name}", headers=admin_headers)


def test_update_profile_network_policy(api, admin_headers):
    name = f"test-profile-{random_suffix()}"
    api.post("/v1/admin/security-profiles", headers=admin_headers, json={"name": name})
    resp = api.patch(f"/v1/admin/security-profiles/{name}", headers=admin_headers, json={
        "network": {
            "allowed_domains": ["example.com"],
            "denied_ip_ranges": ["192.168.0.0/16"],
        },
    })
    assert resp.status_code == 200
    assert resp.json()["network"]["allowed_domains"] == ["example.com"]
    api.delete(f"/v1/admin/security-profiles/{name}", headers=admin_headers)


def test_update_profile_denied_tools(api, admin_headers):
    name = f"test-profile-{random_suffix()}"
    api.post("/v1/admin/security-profiles", headers=admin_headers, json={"name": name})
    resp = api.patch(f"/v1/admin/security-profiles/{name}", headers=admin_headers, json={
        "denied_tools": ["Bash"],
    })
    assert resp.status_code == 200
    assert resp.json()["denied_tools"] == ["Bash"]
    api.delete(f"/v1/admin/security-profiles/{name}", headers=admin_headers)


def test_update_profile_allowed_mcp_servers(api, admin_headers):
    name = f"test-profile-{random_suffix()}"
    api.post("/v1/admin/security-profiles", headers=admin_headers, json={"name": name})
    resp = api.patch(f"/v1/admin/security-profiles/{name}", headers=admin_headers, json={
        "allowed_mcp_servers": [],
    })
    assert resp.status_code == 200
    assert resp.json()["allowed_mcp_servers"] == []
    api.delete(f"/v1/admin/security-profiles/{name}", headers=admin_headers)


def test_update_profile_not_found_returns_404(api, admin_headers):
    resp = api.patch("/v1/admin/security-profiles/nonexistent-xxx", headers=admin_headers, json={
        "description": "nope",
    })
    assert resp.status_code == 404


def test_update_profile_invalid_network_returns_422(api, admin_headers):
    name = f"test-profile-{random_suffix()}"
    api.post("/v1/admin/security-profiles", headers=admin_headers, json={"name": name})
    resp = api.patch(f"/v1/admin/security-profiles/{name}", headers=admin_headers, json={
        "network": {"denied_ip_ranges": ["not-a-cidr"]},
    })
    assert resp.status_code == 422
    api.delete(f"/v1/admin/security-profiles/{name}", headers=admin_headers)


def test_update_builtin_profile_allowed(api, admin_headers):
    resp = api.patch("/v1/admin/security-profiles/common", headers=admin_headers, json={
        "description": "modified",
    })
    assert resp.status_code == 200


def test_delete_custom_profile_returns_204(api, admin_headers):
    name = f"test-profile-{random_suffix()}"
    api.post("/v1/admin/security-profiles", headers=admin_headers, json={"name": name})
    resp = api.delete(f"/v1/admin/security-profiles/{name}", headers=admin_headers)
    assert resp.status_code == 204


def test_delete_builtin_profile_returns_400(api, admin_headers):
    resp = api.delete("/v1/admin/security-profiles/common", headers=admin_headers)
    assert resp.status_code == 400


def test_delete_nonexistent_returns_404(api, admin_headers):
    resp = api.delete("/v1/admin/security-profiles/nonexistent-xxx", headers=admin_headers)
    assert resp.status_code == 404


def test_delete_profile_assigned_to_client_returns_409(api, admin_headers):
    name = f"test-profile-{random_suffix()}"
    api.post("/v1/admin/security-profiles", headers=admin_headers, json={"name": name})
    cid = f"test-client-sp-{random_suffix()}"
    api.post("/v1/admin/clients", headers=admin_headers, json={
        "client_id": cid,
        "security_profile": name,
    })
    resp = api.delete(f"/v1/admin/security-profiles/{name}", headers=admin_headers)
    assert resp.status_code == 409
    # Cleanup
    api.delete(f"/v1/admin/clients/{cid}", headers=admin_headers)
    api.delete(f"/v1/admin/security-profiles/{name}", headers=admin_headers)


def test_set_default_profile_returns_200(api, admin_headers):
    name = f"test-profile-{random_suffix()}"
    api.post("/v1/admin/security-profiles", headers=admin_headers, json={"name": name})
    resp = api.post(f"/v1/admin/security-profiles/{name}/set-default", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["is_default"] is True
    # Restore common as default
    api.post("/v1/admin/security-profiles/common/set-default", headers=admin_headers)
    api.delete(f"/v1/admin/security-profiles/{name}", headers=admin_headers)


def test_set_default_nonexistent_returns_404(api, admin_headers):
    resp = api.post("/v1/admin/security-profiles/nonexistent-xxx/set-default", headers=admin_headers)
    assert resp.status_code == 404


def test_set_default_clears_previous_default(api, admin_headers):
    name = f"test-profile-{random_suffix()}"
    api.post("/v1/admin/security-profiles", headers=admin_headers, json={"name": name})
    api.post(f"/v1/admin/security-profiles/{name}/set-default", headers=admin_headers)
    # Verify common is no longer default
    resp = api.get("/v1/admin/security-profiles/common", headers=admin_headers)
    assert resp.json()["is_default"] is False
    # Restore
    api.post("/v1/admin/security-profiles/common/set-default", headers=admin_headers)
    api.delete(f"/v1/admin/security-profiles/{name}", headers=admin_headers)


# =============================================================================
# 4.1.2 Client-Profile Binding
# =============================================================================


def test_create_client_with_explicit_profile(api, admin_headers):
    cid = f"test-client-sp-{random_suffix()}"
    resp = api.post("/v1/admin/clients", headers=admin_headers, json={
        "client_id": cid,
        "security_profile": "restrictive",
    })
    assert resp.status_code == 201
    get_resp = api.get(f"/v1/admin/clients/{cid}", headers=admin_headers)
    assert get_resp.json()["security_profile"] == "restrictive"
    api.delete(f"/v1/admin/clients/{cid}", headers=admin_headers)


def test_create_client_default_profile(api, admin_headers):
    cid = f"test-client-sp-{random_suffix()}"
    resp = api.post("/v1/admin/clients", headers=admin_headers, json={
        "client_id": cid,
    })
    assert resp.status_code == 201
    get_resp = api.get(f"/v1/admin/clients/{cid}", headers=admin_headers)
    assert get_resp.json()["security_profile"] == "common"
    api.delete(f"/v1/admin/clients/{cid}", headers=admin_headers)


def test_create_client_nonexistent_profile_returns_400(api, admin_headers):
    cid = f"test-client-sp-{random_suffix()}"
    resp = api.post("/v1/admin/clients", headers=admin_headers, json={
        "client_id": cid,
        "security_profile": "nonexistent",
    })
    assert resp.status_code == 400


def test_update_client_profile(api, admin_headers):
    cid = f"test-client-sp-{random_suffix()}"
    api.post("/v1/admin/clients", headers=admin_headers, json={"client_id": cid})
    resp = api.patch(f"/v1/admin/clients/{cid}", headers=admin_headers, json={
        "security_profile": "unconfined",
    })
    assert resp.status_code == 200
    assert resp.json()["security_profile"] == "unconfined"
    api.delete(f"/v1/admin/clients/{cid}", headers=admin_headers)


def test_update_client_nonexistent_profile_returns_400(api, admin_headers):
    cid = f"test-client-sp-{random_suffix()}"
    api.post("/v1/admin/clients", headers=admin_headers, json={"client_id": cid})
    resp = api.patch(f"/v1/admin/clients/{cid}", headers=admin_headers, json={
        "security_profile": "nonexistent",
    })
    assert resp.status_code == 400
    api.delete(f"/v1/admin/clients/{cid}", headers=admin_headers)


def test_get_client_shows_profile(api, admin_headers):
    cid = f"test-client-sp-{random_suffix()}"
    api.post("/v1/admin/clients", headers=admin_headers, json={
        "client_id": cid,
        "security_profile": "restrictive",
    })
    resp = api.get(f"/v1/admin/clients/{cid}", headers=admin_headers)
    assert resp.status_code == 200
    assert "security_profile" in resp.json()
    assert resp.json()["security_profile"] == "restrictive"
    api.delete(f"/v1/admin/clients/{cid}", headers=admin_headers)


def test_list_clients_shows_profile(api, admin_headers):
    resp = api.get("/v1/admin/clients", headers=admin_headers)
    assert resp.status_code == 200
    for client in resp.json():
        assert "security_profile" in client


# =============================================================================
# 4.1.3 Built-in Profile Verification
# =============================================================================


def test_builtin_unconfined_settings(api, admin_headers):
    resp = api.get("/v1/admin/security-profiles/unconfined", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_builtin"] is True
    assert body["network"]["allowed_domains"] is None
    assert body["denied_tools"] == []
    assert body["allowed_mcp_servers"] is None


def test_builtin_common_settings(api, admin_headers):
    resp = api.get("/v1/admin/security-profiles/common", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_builtin"] is True
    assert body["network"]["allow_ip_destination"] is False
    assert "10.0.0.0/8" in body["network"]["denied_ip_ranges"]
    assert "192.168.0.0/16" in body["network"]["denied_ip_ranges"]


def test_builtin_restrictive_settings(api, admin_headers):
    resp = api.get("/v1/admin/security-profiles/restrictive", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_builtin"] is True
    assert body["network"]["allowed_domains"] == []
    assert "WebFetch" in body["denied_tools"]
    assert "WebSearch" in body["denied_tools"]
    assert body["allowed_mcp_servers"] == []


def test_builtin_common_is_default(api, admin_headers):
    resp = api.get("/v1/admin/security-profiles/common", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["is_default"] is True


# =============================================================================
# 4.1.4 Validation Edge Cases
# =============================================================================


def test_valid_domain_exact(api, admin_headers):
    name = f"test-profile-{random_suffix()}"
    resp = api.post("/v1/admin/security-profiles", headers=admin_headers, json={
        "name": name,
        "network": {"allowed_domains": ["github.com"]},
    })
    assert resp.status_code == 201
    api.delete(f"/v1/admin/security-profiles/{name}", headers=admin_headers)


def test_valid_domain_wildcard(api, admin_headers):
    name = f"test-profile-{random_suffix()}"
    resp = api.post("/v1/admin/security-profiles", headers=admin_headers, json={
        "name": name,
        "network": {"allowed_domains": ["*.github.com"]},
    })
    assert resp.status_code == 201
    api.delete(f"/v1/admin/security-profiles/{name}", headers=admin_headers)


def test_invalid_domain_star_tld(api, admin_headers):
    name = f"test-profile-{random_suffix()}"
    resp = api.post("/v1/admin/security-profiles", headers=admin_headers, json={
        "name": name,
        "network": {"allowed_domains": ["*.com"]},
    })
    assert resp.status_code == 422


def test_invalid_domain_bare_star(api, admin_headers):
    name = f"test-profile-{random_suffix()}"
    resp = api.post("/v1/admin/security-profiles", headers=admin_headers, json={
        "name": name,
        "network": {"allowed_domains": ["*"]},
    })
    assert resp.status_code == 422


def test_valid_cidr_ipv4(api, admin_headers):
    name = f"test-profile-{random_suffix()}"
    resp = api.post("/v1/admin/security-profiles", headers=admin_headers, json={
        "name": name,
        "network": {"denied_ip_ranges": ["10.0.0.0/8"]},
    })
    assert resp.status_code == 201
    api.delete(f"/v1/admin/security-profiles/{name}", headers=admin_headers)


def test_valid_cidr_ipv6(api, admin_headers):
    name = f"test-profile-{random_suffix()}"
    resp = api.post("/v1/admin/security-profiles", headers=admin_headers, json={
        "name": name,
        "network": {"denied_ip_ranges": ["::1/128"]},
    })
    assert resp.status_code == 201
    api.delete(f"/v1/admin/security-profiles/{name}", headers=admin_headers)


def test_invalid_cidr(api, admin_headers):
    name = f"test-profile-{random_suffix()}"
    resp = api.post("/v1/admin/security-profiles", headers=admin_headers, json={
        "name": name,
        "network": {"denied_ip_ranges": ["999.999.999.999/8"]},
    })
    assert resp.status_code == 422


def test_profile_name_too_long(api, admin_headers):
    name = "a" * 101
    resp = api.post("/v1/admin/security-profiles", headers=admin_headers, json={
        "name": name,
    })
    assert resp.status_code == 422
