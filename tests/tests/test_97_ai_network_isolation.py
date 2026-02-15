"""AI integration tests for network isolation (Phase 2).

Verifies that per-job HTTP proxy + --unshare-net correctly enforces
network restrictions based on security profiles.

Requires:
- ANTHROPIC_API_KEY to be set
- Server running with bwrap enabled (CCAS_ENABLE_BWRAP_SANDBOX=true)
- socat installed (in Docker image or host)
- Network isolation enabled (CCAS_SANDBOX_NETWORK_ENABLED=true, the default)
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
    reason="ANTHROPIC_API_KEY not set — skipping AI network isolation tests",
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
def network_test_setup(api, admin_headers):
    """Create test clients with different profiles for network isolation tests."""
    clients = []
    client_headers_map = {}
    custom_profiles = []

    # Create a custom profile that uses proxy but allows private IPs
    private_allowed_profile = f"test-profile-private-allowed-{random_suffix()}"
    resp = api.post("/v1/admin/security-profiles", headers=admin_headers, json={
        "name": private_allowed_profile,
        "description": "Test profile: proxy enabled, private IPs allowed",
        "network": {
            "allowed_domains": None,  # Any domain
            "denied_domains": [],
            "allowed_ip_ranges": None,  # Any IP range
            "denied_ip_ranges": [],  # No denied ranges (private IPs allowed)
            "allow_ip_destination": True,
        },
    })
    assert resp.status_code == 201, f"Failed to create private-allowed profile: {resp.text}"
    custom_profiles.append(private_allowed_profile)

    # Create clients for each profile
    for label, profile in [
        ("restrictive", "restrictive"),
        ("unconfined", "unconfined"),
        ("common", "common"),
        ("private_allowed", private_allowed_profile),
    ]:
        cid = f"test-client-net-{label}-{random_suffix()}"
        resp = api.post("/v1/admin/clients", headers=admin_headers, json={
            "client_id": cid,
            "description": f"Network isolation test client ({label})",
            "role": "client",
            "security_profile": profile,
        })
        assert resp.status_code == 201
        key = resp.json()["api_key"]
        clients.append(cid)
        client_headers_map[label] = {"Authorization": f"Bearer {key}"}

    yield {
        "headers": client_headers_map,
        "clients": clients,
    }

    # Teardown
    for cid in clients:
        api.delete(f"/v1/admin/clients/{cid}", headers=admin_headers)
    for profile in custom_profiles:
        api.delete(f"/v1/admin/security-profiles/{profile}", headers=admin_headers)


# =============================================================================
# AI Tests: Network Isolation
# =============================================================================


def test_ai_restrictive_cannot_reach_external_domain(api, network_test_setup):
    """
    Restrictive profile: job should NOT be able to reach non-Anthropic domains.

    The restrictive profile only allows *.anthropic.com. Attempting to curl
    an external domain should fail due to proxy filtering.
    """
    headers = network_test_setup["headers"]["restrictive"]
    prompt = (
        "Use the Bash tool to run: curl -sS --max-time 10 https://ifconfig.me 2>&1\n"
        "The request will likely fail because network access is restricted. "
        "That is expected.\n"
        f"{RESULT_SCHEMA_INSTRUCTION} with test_name='restrictive_no_external'. "
        "Record action='curl_external'. "
        "If curl succeeded and returned an IP address, set success=true. "
        "If curl failed (timeout, connection refused, proxy denied, etc.), set success=false "
        "and include the error message in detail."
    )
    job = _submit_and_wait(api, headers, prompt)
    result = _parse_test_result(job)
    action = _find_action(result, "curl_external")
    assert action["success"] is False, (
        f"Restrictive profile should NOT allow external domain access. "
        f"Detail: {action.get('detail')}"
    )


def test_ai_unconfined_can_reach_external_domain(api, network_test_setup):
    """
    Unconfined profile: job should have full network access.

    No proxy, no --unshare-net. curl to external domain should succeed.
    """
    headers = network_test_setup["headers"]["unconfined"]
    prompt = (
        "Use the Bash tool to run: curl -sS --max-time 15 https://ifconfig.me 2>&1\n"
        f"{RESULT_SCHEMA_INSTRUCTION} with test_name='unconfined_external'. "
        "Record action='curl_external'. "
        "If curl succeeded and returned an IP address, set success=true. "
        "If curl failed, set success=false and include the error message in detail."
    )
    job = _submit_and_wait(api, headers, prompt)
    result = _parse_test_result(job)
    action = _find_action(result, "curl_external")
    assert action["success"] is True, (
        f"Unconfined profile should allow external domain access. "
        f"Detail: {action.get('detail')}"
    )


def test_ai_private_allowed_can_reach_private_ip(api, network_test_setup):
    """
    Custom profile with proxy but private IPs allowed: job should reach localhost.

    This profile has network restrictions (proxy is active) but does NOT deny
    private IP ranges, so curl to 127.0.0.1 should succeed through the proxy.
    This verifies the proxy correctly allows the request based on policy.
    """
    headers = network_test_setup["headers"]["private_allowed"]
    prompt = (
        "Use the Bash tool to run: curl -sS --max-time 10 http://127.0.0.1:8000/v1/health 2>&1\n"
        f"{RESULT_SCHEMA_INSTRUCTION} with test_name='private_allowed_private_ip'. "
        "Record action='curl_private_ip'. "
        "If curl succeeded and got a JSON response with 'status', set success=true. "
        "If curl failed, set success=false and include the error message in detail."
    )
    job = _submit_and_wait(api, headers, prompt)
    result = _parse_test_result(job)
    action = _find_action(result, "curl_private_ip")
    assert action["success"] is True, (
        f"Private-allowed profile should allow private IP access through proxy. "
        f"Detail: {action.get('detail')}"
    )


def test_ai_common_blocks_private_ip(api, network_test_setup):
    """
    Common profile: job should NOT be able to reach private IPs.

    The common profile has denied_ip_ranges including 127.0.0.0/8,
    10.0.0.0/8, etc. Attempting to curl localhost should fail.
    """
    headers = network_test_setup["headers"]["common"]
    prompt = (
        "Use the Bash tool to run: curl -sS --max-time 10 http://127.0.0.1:8000/v1/health 2>&1\n"
        "The request will likely fail because private IPs are blocked by the proxy. "
        "That is expected.\n"
        f"{RESULT_SCHEMA_INSTRUCTION} with test_name='common_no_private_ip'. "
        "Record action='curl_private_ip'. "
        "If curl succeeded and got a response (e.g. JSON with 'status'), set success=true. "
        "If curl failed (proxy denied, 403 Forbidden, connection refused, timeout, etc.), "
        "set success=false and include the error message in detail."
    )
    job = _submit_and_wait(api, headers, prompt)
    result = _parse_test_result(job)
    action = _find_action(result, "curl_private_ip")
    assert action["success"] is False, (
        f"Common profile should block private IP access. "
        f"Detail: {action.get('detail')}"
    )


def test_ai_common_allows_public_domain(api, network_test_setup):
    """
    Common profile: job should be able to reach public internet domains.

    The common profile allows any DNS domain, only blocking private IPs.
    """
    headers = network_test_setup["headers"]["common"]
    prompt = (
        "Use the Bash tool to run: curl -sS --max-time 15 https://ifconfig.me 2>&1\n"
        f"{RESULT_SCHEMA_INSTRUCTION} with test_name='common_public_domain'. "
        "Record action='curl_public'. "
        "If curl succeeded and returned an IP address, set success=true. "
        "If curl failed, set success=false and include the error message in detail."
    )
    job = _submit_and_wait(api, headers, prompt)
    result = _parse_test_result(job)
    action = _find_action(result, "curl_public")
    assert action["success"] is True, (
        f"Common profile should allow public domain access. "
        f"Detail: {action.get('detail')}"
    )


def test_ai_restrictive_raw_ip_blocked(api, network_test_setup):
    """
    Restrictive profile: raw IP destinations should be blocked.
    """
    headers = network_test_setup["headers"]["restrictive"]
    prompt = (
        "Use the Bash tool to run: curl -sS --max-time 10 http://8.8.8.8/ 2>&1\n"
        "The request will likely fail because raw IP access is blocked.\n"
        f"{RESULT_SCHEMA_INSTRUCTION} with test_name='restrictive_no_raw_ip'. "
        "Record action='curl_raw_ip'. "
        "If curl succeeded, set success=true. "
        "If curl failed, set success=false and include the error in detail."
    )
    job = _submit_and_wait(api, headers, prompt)
    result = _parse_test_result(job)
    action = _find_action(result, "curl_raw_ip")
    assert action["success"] is False, (
        f"Restrictive profile should block raw IP destinations. "
        f"Detail: {action.get('detail')}"
    )


def test_ai_restrictive_anthropic_api_allowed(api, network_test_setup):
    """
    Restrictive profile: Anthropic API should always be reachable.

    This is verified implicitly by the job completing at all (the CLI
    must reach api.anthropic.com to function). But we also explicitly
    test DNS resolution.
    """
    headers = network_test_setup["headers"]["restrictive"]
    prompt = (
        "Use the Bash tool to run: curl -sS --max-time 15 -o /dev/null -w '%{http_code}' https://api.anthropic.com/ 2>&1\n"
        "The Anthropic API should be reachable even under restrictive profile.\n"
        f"{RESULT_SCHEMA_INSTRUCTION} with test_name='restrictive_anthropic_allowed'. "
        "Record action='curl_anthropic'. "
        "If curl reached the server (any HTTP status code returned, even 401/403), set success=true. "
        "If curl failed with a connection error or proxy denial, set success=false."
    )
    job = _submit_and_wait(api, headers, prompt)
    result = _parse_test_result(job)
    action = _find_action(result, "curl_anthropic")
    assert action["success"] is True, (
        f"Restrictive profile should always allow Anthropic API access. "
        f"Detail: {action.get('detail')}"
    )
