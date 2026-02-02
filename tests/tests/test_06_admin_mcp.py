"""MCP server management admin endpoint tests."""

from helpers.test_data import random_suffix

# Inline minimal MCP server that passes health checks.
# Speaks JSON-RPC: responds to initialize, tools/list, tools/call.
_FAKE_MCP_CODE = (
    "import json,sys\n"
    "def s(o):\n"
    " sys.stdout.write(json.dumps(o)+'\\n');sys.stdout.flush()\n"
    "for l in sys.stdin:\n"
    " l=l.strip()\n"
    " if not l:continue\n"
    " try:r=json.loads(l)\n"
    " except:continue\n"
    " m=r.get('method','');i=r.get('id')\n"
    " if m=='initialize':s({'jsonrpc':'2.0','id':i,'result':{'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'fake-mcp','version':'0.1.0'}}})\n"
    " elif m=='tools/list':s({'jsonrpc':'2.0','id':i,'result':{'tools':[{'name':'ping','description':'Returns pong','inputSchema':{'type':'object','properties':{}}}]}})\n"
    " elif m=='tools/call':s({'jsonrpc':'2.0','id':i,'result':{'content':[{'type':'text','text':'pong'}]}})\n"
    " elif i is not None:s({'jsonrpc':'2.0','id':i,'error':{'code':-32601,'message':'not found'}})\n"
)


def _mcp_name():
    return f"test-mcp-{random_suffix()}"


def _stdio_body(name, **overrides):
    """Build a stdio MCP server request body using the fake MCP server."""
    body = {
        "name": name,
        "type": "stdio",
        "command": "python3",
        "args": ["-c", _FAKE_MCP_CODE],
    }
    body.update(overrides)
    return body


def test_add_stdio_server(api, admin_headers):
    name = _mcp_name()
    resp = api.post("/v1/admin/mcp", headers=admin_headers, json=_stdio_body(name))
    assert resp.status_code == 201
    api.delete(f"/v1/admin/mcp/{name}", headers=admin_headers, params={"keep_package": "true"})


def test_add_http_server(api, admin_headers):
    name = _mcp_name()
    resp = api.post("/v1/admin/mcp", headers=admin_headers, json={
        "name": name,
        "type": "http",
        "url": "http://example.com/mcp",
    })
    assert resp.status_code == 201
    api.delete(f"/v1/admin/mcp/{name}", headers=admin_headers, params={"keep_package": "true"})


def test_add_sse_server(api, admin_headers):
    name = _mcp_name()
    resp = api.post("/v1/admin/mcp", headers=admin_headers, json={
        "name": name,
        "type": "sse",
        "url": "http://example.com/sse",
    })
    assert resp.status_code == 201
    api.delete(f"/v1/admin/mcp/{name}", headers=admin_headers, params={"keep_package": "true"})


def test_add_duplicate_returns_409(api, admin_headers):
    name = _mcp_name()
    api.post("/v1/admin/mcp", headers=admin_headers, json=_stdio_body(name))
    resp = api.post("/v1/admin/mcp", headers=admin_headers, json=_stdio_body(name))
    assert resp.status_code == 409
    api.delete(f"/v1/admin/mcp/{name}", headers=admin_headers, params={"keep_package": "true"})


def test_add_stdio_missing_command_returns_400(api, admin_headers):
    resp = api.post("/v1/admin/mcp", headers=admin_headers, json={
        "name": _mcp_name(),
        "type": "stdio",
    })
    assert resp.status_code == 400


def test_add_http_missing_url_returns_400(api, admin_headers):
    resp = api.post("/v1/admin/mcp", headers=admin_headers, json={
        "name": _mcp_name(),
        "type": "http",
    })
    assert resp.status_code == 400


def test_add_invalid_name_returns_422(api, admin_headers):
    resp = api.post("/v1/admin/mcp", headers=admin_headers, json={
        "name": "has spaces!",
        "type": "stdio",
        "command": "python3",
        "args": ["-c", _FAKE_MCP_CODE],
    })
    assert resp.status_code == 422


def test_add_server_with_env_vars(api, admin_headers):
    name = _mcp_name()
    resp = api.post("/v1/admin/mcp", headers=admin_headers, json=_stdio_body(
        name, env={"KEY": "value"},
    ))
    assert resp.status_code == 201
    api.delete(f"/v1/admin/mcp/{name}", headers=admin_headers, params={"keep_package": "true"})


def test_add_server_with_headers(api, admin_headers):
    name = _mcp_name()
    resp = api.post("/v1/admin/mcp", headers=admin_headers, json={
        "name": name,
        "type": "http",
        "url": "http://example.com/mcp",
        "headers": {"Authorization": "Bearer x"},
    })
    assert resp.status_code == 201
    api.delete(f"/v1/admin/mcp/{name}", headers=admin_headers, params={"keep_package": "true"})


def test_list_servers(api, admin_headers):
    resp = api.get("/v1/admin/mcp", headers=admin_headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_list_includes_created(api, admin_headers):
    name = _mcp_name()
    api.post("/v1/admin/mcp", headers=admin_headers, json=_stdio_body(name))
    resp = api.get("/v1/admin/mcp", headers=admin_headers)
    names = [s["name"] for s in resp.json()]
    assert name in names
    api.delete(f"/v1/admin/mcp/{name}", headers=admin_headers, params={"keep_package": "true"})


def test_get_server(api, admin_headers):
    name = _mcp_name()
    api.post("/v1/admin/mcp", headers=admin_headers, json=_stdio_body(name))
    resp = api.get(f"/v1/admin/mcp/{name}", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["name"] == name
    api.delete(f"/v1/admin/mcp/{name}", headers=admin_headers, params={"keep_package": "true"})


def test_get_server_not_found(api, admin_headers):
    resp = api.get(f"/v1/admin/mcp/nonexistent-{random_suffix()}", headers=admin_headers)
    assert resp.status_code == 404


def test_delete_server(api, admin_headers):
    name = _mcp_name()
    api.post("/v1/admin/mcp", headers=admin_headers, json=_stdio_body(name))
    resp = api.delete(f"/v1/admin/mcp/{name}", headers=admin_headers, params={"keep_package": "true"})
    assert resp.status_code == 204


def test_delete_server_not_found(api, admin_headers):
    resp = api.delete(f"/v1/admin/mcp/nonexistent-{random_suffix()}", headers=admin_headers, params={"keep_package": "true"})
    assert resp.status_code == 404


def test_delete_server_keep_package(api, admin_headers):
    name = _mcp_name()
    api.post("/v1/admin/mcp", headers=admin_headers, json=_stdio_body(name))
    resp = api.delete(f"/v1/admin/mcp/{name}", headers=admin_headers, params={"keep_package": "true"})
    assert resp.status_code == 204


def test_health_check_all(api, admin_headers):
    resp = api.post("/v1/admin/mcp/health-check", headers=admin_headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_health_check_specific_server(api, admin_headers):
    name = _mcp_name()
    api.post("/v1/admin/mcp", headers=admin_headers, json=_stdio_body(name))
    resp = api.post(f"/v1/admin/mcp/{name}/health-check", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["healthy"] is True
    api.delete(f"/v1/admin/mcp/{name}", headers=admin_headers, params={"keep_package": "true"})


def test_health_check_not_found(api, admin_headers):
    resp = api.post(f"/v1/admin/mcp/nonexistent-{random_suffix()}/health-check", headers=admin_headers)
    assert resp.status_code == 404
