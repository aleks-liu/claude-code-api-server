"""AI integration tests for security profile enforcement.

Verifies that Claude Code respects tool denials, MCP server filtering,
and selective tool policies under different security profiles.

Requires ANTHROPIC_API_KEY to be set.
"""

import base64
import json
import os
import time

import pytest

from helpers.test_data import random_suffix

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

pytestmark = pytest.mark.skipif(
    not ANTHROPIC_API_KEY,
    reason="ANTHROPIC_API_KEY not set — skipping AI security profile tests",
)

JOB_TIMEOUT = 300
POLL_INTERVAL = 10

RESULT_FILE = "test_result.json"
RESULT_SCHEMA_INSTRUCTION = (
    "You MUST write a file called 'test_result.json' with this EXACT JSON structure:\n"
    '{"test_name": "<test_name>", "actions": [{"action": "<what_you_tried>", '
    '"success": <true_or_false>, "detail": "<brief_explanation>"}]}\n'
    "Write ONLY valid JSON, no other text in the file."
)


# =============================================================================
# Helpers
# =============================================================================


def _submit_and_wait(api, client_headers, prompt, timeout=JOB_TIMEOUT):
    """Submit job, poll until terminal state, return job response dict."""
    resp = api.post("/v1/jobs", headers={
        **client_headers,
        "X-Anthropic-Key": ANTHROPIC_API_KEY,
    }, json={
        "prompt": prompt,
        "timeout_seconds": timeout,
    })
    assert resp.status_code == 202, f"Job submission failed: {resp.status_code} {resp.text}"
    job_id = resp.json()["job_id"]

    start = time.time()
    while time.time() - start < timeout:
        resp = api.get(f"/v1/jobs/{job_id}", headers=client_headers)
        assert resp.status_code == 200
        status = resp.json()["status"]
        if status in ("COMPLETED", "FAILED", "TIMEOUT"):
            return resp.json()
        time.sleep(POLL_INTERVAL)

    pytest.fail(f"Job {job_id} did not complete within {timeout}s")


def _parse_test_result(job_response):
    """Extract and parse test_result.json from job output files."""
    assert job_response["status"] == "COMPLETED", (
        f"Job status: {job_response['status']}. "
        f"Error: {job_response.get('error')}. "
        f"Output: {(job_response.get('output') or {}).get('text', '')[:500]}"
    )

    output_files = (job_response.get("output") or {}).get("files", {})

    if RESULT_FILE in output_files:
        raw = base64.b64decode(output_files[RESULT_FILE])
        return json.loads(raw)

    # Fallback: try to find JSON in output text
    output_text = (job_response.get("output") or {}).get("text", "")
    for line in output_text.split("\n"):
        line = line.strip()
        if line.startswith("{") and "test_name" in line:
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue

    available = list(output_files.keys())
    pytest.fail(
        f"test_result.json not found in output files. "
        f"Available files: {available}. "
        f"Output text: {output_text[:500]}"
    )


def _find_action(result, action_name):
    """Find action by name in result actions list."""
    for a in result.get("actions", []):
        if a.get("action") == action_name:
            return a
    action_names = [a.get("action") for a in result.get("actions", [])]
    pytest.fail(f"Action '{action_name}' not found. Available: {action_names}")


# =============================================================================
# Module-Scoped Fixtures
# =============================================================================


@pytest.fixture(scope="module")
def profile_setup(api, admin_headers):
    """Create test profiles and clients for AI tests."""
    profiles = []
    clients = []
    client_headers_map = {}

    # Custom profile: deny WebFetch only
    deny_wf_name = f"test-profile-deny-wf-{random_suffix()}"
    resp = api.post("/v1/admin/security-profiles", headers=admin_headers, json={
        "name": deny_wf_name,
        "description": "Denies WebFetch only",
        "denied_tools": ["WebFetch"],
    })
    assert resp.status_code in (201, 409)
    profiles.append(deny_wf_name)

    # Custom profile: deny WebSearch only
    deny_ws_name = f"test-profile-deny-ws-{random_suffix()}"
    resp = api.post("/v1/admin/security-profiles", headers=admin_headers, json={
        "name": deny_ws_name,
        "description": "Denies WebSearch only",
        "denied_tools": ["WebSearch"],
    })
    assert resp.status_code in (201, 409)
    profiles.append(deny_ws_name)

    # Create a test MCP server (inline JSON-RPC) for MCP filtering tests.
    # Uses an inline python3 -c script so it works inside Docker without
    # mounting external files.
    mcp_name = f"test-mcp-sp-{random_suffix()}"
    fake_mcp_code = (
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
    resp = api.post("/v1/admin/mcp", headers=admin_headers, json={
        "name": mcp_name,
        "type": "stdio",
        "command": "python3",
        "args": ["-c", fake_mcp_code],
        "description": "Test MCP for security profile filtering",
    })
    assert resp.status_code in (201, 409)

    # Create clients for each profile
    for label, profile in [
        ("restrictive", "restrictive"),
        ("unconfined", "unconfined"),
        ("deny-webfetch", deny_wf_name),
        ("deny-websearch", deny_ws_name),
    ]:
        cid = f"test-client-sp-{label}-{random_suffix()}"
        resp = api.post("/v1/admin/clients", headers=admin_headers, json={
            "client_id": cid,
            "description": f"AI test client ({label})",
            "role": "client",
            "security_profile": profile,
        })
        assert resp.status_code == 201
        key = resp.json()["api_key"]
        clients.append(cid)
        client_headers_map[label] = {"Authorization": f"Bearer {key}"}

    yield {
        "headers": client_headers_map,
        "profiles": profiles,
        "clients": clients,
        "mcp_name": mcp_name,
    }

    # Teardown
    for cid in clients:
        api.delete(f"/v1/admin/clients/{cid}", headers=admin_headers)
    for pname in profiles:
        api.delete(f"/v1/admin/security-profiles/{pname}", headers=admin_headers)
    api.delete(f"/v1/admin/mcp/{mcp_name}", headers=admin_headers, params={"keep_package": "true"})


# =============================================================================
# AI Tests
# =============================================================================


def test_ai_basic_functionality_restrictive(api, profile_setup):
    """Verify basic file operations work under restrictive profile."""
    headers = profile_setup["headers"]["restrictive"]
    prompt = (
        "Write a file called hello.txt containing 'Hello from restricted sandbox'. "
        f"Then {RESULT_SCHEMA_INSTRUCTION} with test_name='basic', "
        "action='write_file', success=true."
    )
    job = _submit_and_wait(api, headers, prompt)
    result = _parse_test_result(job)
    action = _find_action(result, "write_file")
    assert action["success"] is True

    output_files = (job.get("output") or {}).get("files", {})
    assert "hello.txt" in output_files


def test_ai_tool_denied_webfetch(api, profile_setup):
    """Verify WebFetch is denied under restrictive profile."""
    headers = profile_setup["headers"]["restrictive"]
    prompt = (
        "Try to use the WebFetch tool to fetch the URL https://example.com. "
        "You will likely get a permission denied error — that is expected. "
        f"{RESULT_SCHEMA_INSTRUCTION} with test_name='webfetch_denied'. "
        "Record action='use_webfetch' with success=false if denied, or success=true if it worked."
    )
    job = _submit_and_wait(api, headers, prompt)
    result = _parse_test_result(job)
    action = _find_action(result, "use_webfetch")
    assert action["success"] is False


def test_ai_tool_denied_websearch(api, profile_setup):
    """Verify WebSearch is denied under restrictive profile."""
    headers = profile_setup["headers"]["restrictive"]
    prompt = (
        "Try to use the WebSearch tool to search for 'test query'. "
        "You will likely get a permission denied error — that is expected. "
        f"{RESULT_SCHEMA_INSTRUCTION} with test_name='websearch_denied'. "
        "Record action='use_websearch' with success=false if denied, or success=true if it worked."
    )
    job = _submit_and_wait(api, headers, prompt)
    result = _parse_test_result(job)
    action = _find_action(result, "use_websearch")
    assert action["success"] is False


def test_ai_selective_denial_webfetch_denied_bash_allowed(api, profile_setup):
    """Verify selective denial: Bash allowed, WebFetch denied."""
    headers = profile_setup["headers"]["deny-webfetch"]
    prompt = (
        "Do two things: "
        "(1) Use the Bash tool to run 'echo hello'. "
        "(2) Try to use WebFetch to fetch https://example.com. "
        f"{RESULT_SCHEMA_INSTRUCTION} with test_name='selective_denial'. "
        "Record action='use_bash' and action='use_webfetch'."
    )
    job = _submit_and_wait(api, headers, prompt)
    result = _parse_test_result(job)
    bash_action = _find_action(result, "use_bash")
    wf_action = _find_action(result, "use_webfetch")
    assert bash_action["success"] is True
    assert wf_action["success"] is False


def test_ai_custom_deny_websearch(api, profile_setup):
    """Verify WebSearch is denied under custom deny-websearch profile."""
    headers = profile_setup["headers"]["deny-websearch"]
    prompt = (
        "Try to use the WebSearch tool to search for 'test query'. "
        "You will likely get a permission denied error — that is expected. "
        f"{RESULT_SCHEMA_INSTRUCTION} with test_name='websearch_custom_denied'. "
        "Record action='use_websearch' with success=false if denied, or success=true if it worked."
    )
    job = _submit_and_wait(api, headers, prompt)
    result = _parse_test_result(job)
    action = _find_action(result, "use_websearch")
    assert action["success"] is False


def test_ai_unconfined_tools_work(api, profile_setup):
    """Verify all tools work under unconfined profile."""
    headers = profile_setup["headers"]["unconfined"]
    prompt = (
        "Do two things: "
        "(1) Use the Bash tool to run 'echo hello_from_unconfined'. "
        "(2) Write a file called output.txt containing 'unconfined works'. "
        f"{RESULT_SCHEMA_INSTRUCTION} with test_name='unconfined_all'. "
        "Record action='use_bash' and action='write_file'."
    )
    job = _submit_and_wait(api, headers, prompt)
    result = _parse_test_result(job)
    bash_action = _find_action(result, "use_bash")
    write_action = _find_action(result, "write_file")
    assert bash_action["success"] is True
    assert write_action["success"] is True

    output_files = (job.get("output") or {}).get("files", {})
    assert "output.txt" in output_files


def test_ai_mcp_visible_under_unconfined(api, profile_setup):
    """Verify a known MCP server is visible under unconfined profile."""
    mcp_name = profile_setup["mcp_name"]
    headers = profile_setup["headers"]["unconfined"]
    prompt = (
        f"Check if you have any MCP server tools available whose name contains '{mcp_name}'. "
        f"Look through all your available tools for anything starting with 'mcp__'. "
        f"Specifically look for tool names containing '{mcp_name}'. "
        f"{RESULT_SCHEMA_INSTRUCTION} with test_name='mcp_visible'. "
        f"Record action='find_mcp_server'. "
        f"If you found any tool containing '{mcp_name}', set success=true and detail=the tool name. "
        f"If not found, set success=false and detail='not_found'."
    )
    job = _submit_and_wait(api, headers, prompt)
    result = _parse_test_result(job)
    action = _find_action(result, "find_mcp_server")
    assert action["success"] is True, (
        f"MCP server '{mcp_name}' should be visible under unconfined profile. "
        f"Detail: {action.get('detail')}"
    )


def test_ai_mcp_filtered_under_restrictive(api, profile_setup):
    """Verify the same MCP server is NOT visible under restrictive profile."""
    mcp_name = profile_setup["mcp_name"]
    headers = profile_setup["headers"]["restrictive"]
    prompt = (
        f"Check if you have any MCP server tools available whose name contains '{mcp_name}'. "
        f"Look through all your available tools for anything starting with 'mcp__'. "
        f"Specifically look for tool names containing '{mcp_name}'. "
        f"{RESULT_SCHEMA_INSTRUCTION} with test_name='mcp_filtered'. "
        f"Record action='find_mcp_server'. "
        f"If you found any tool containing '{mcp_name}', set success=true and detail=the tool name. "
        f"If not found, set success=false and detail='not_found'."
    )
    job = _submit_and_wait(api, headers, prompt)
    result = _parse_test_result(job)
    action = _find_action(result, "find_mcp_server")
    assert action["success"] is False, (
        f"MCP server '{mcp_name}' should NOT be visible under restrictive profile "
        f"(allowed_mcp_servers=[]). Detail: {action.get('detail')}"
    )


def test_ai_webfetch_ifconfig_denied_restrictive(api, profile_setup):
    """Verify WebFetch to ifconfig.me is denied under restrictive profile."""
    headers = profile_setup["headers"]["restrictive"]
    prompt = (
        "Try to use WebFetch to fetch https://ifconfig.me. "
        "You will likely get a permission denied error. "
        f"{RESULT_SCHEMA_INSTRUCTION} with test_name='fetch_ifconfig_denied'. "
        "Record action='webfetch_ifconfig' with success=false if denied, or success=true if it worked."
    )
    job = _submit_and_wait(api, headers, prompt)
    result = _parse_test_result(job)
    action = _find_action(result, "webfetch_ifconfig")
    assert action["success"] is False
