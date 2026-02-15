"""AI integration tests for upstream proxy support.

Verifies that Claude Code can reach external services through the full
proxy chain when the server is configured with an upstream proxy:

    Claude CLI (bwrap sandbox)  →  SandboxProxy  →  upstream test proxy  →  internet

The test starts a real, fully functional CONNECT proxy on the host and
verifies that AI jobs complete successfully through it.

Requires:
- ANTHROPIC_API_KEY to be set
- TEST_UPSTREAM_PROXY_PORT to be set (e.g., 18128)
- Server started with CCAS_UPSTREAM_HTTPS_PROXY pointing to this proxy
  (e.g., CCAS_UPSTREAM_HTTPS_PROXY=http://host.docker.internal:18128)
- Server running with bwrap + network isolation enabled (the defaults)
"""

import asyncio
import base64
import json
import os
import threading
import time

import pytest

from helpers.test_data import random_suffix

# ── Configuration ─────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
UPSTREAM_PROXY_PORT = os.environ.get("TEST_UPSTREAM_PROXY_PORT", "")

pytestmark = pytest.mark.skipif(
    not ANTHROPIC_API_KEY or not UPSTREAM_PROXY_PORT,
    reason=(
        "Requires ANTHROPIC_API_KEY and TEST_UPSTREAM_PROXY_PORT. "
        "Server must be started with CCAS_UPSTREAM_HTTPS_PROXY=http://host.docker.internal:<port>"
    ),
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
# Helpers (same as test_97)
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
# Threaded CONNECT Proxy (fully functional, runs in background thread)
# =============================================================================


async def _relay_bidir(reader_a, writer_a, reader_b, writer_b):
    """Bidirectional relay between two stream pairs until either side closes."""

    async def _one_way(r, w):
        try:
            while True:
                data = await r.read(65536)
                if not data:
                    break
                w.write(data)
                await w.drain()
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass
        finally:
            try:
                if not w.is_closing():
                    w.close()
            except (ConnectionError, OSError):
                pass

    await asyncio.gather(_one_way(reader_a, writer_b), _one_way(reader_b, writer_a))


class _ThreadedConnectProxy:
    """
    A fully functional HTTP CONNECT proxy that runs in a background thread.

    Listens on 0.0.0.0:<port> so Docker containers can reach it via
    host.docker.internal. Tracks connection count for test assertions.
    """

    def __init__(self, port: int) -> None:
        self.port = port
        self.connect_count: int = 0
        self.targets: list[str] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._server: asyncio.AbstractServer | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError(f"CONNECT proxy did not start on port {self.port} within 10s")

    def stop(self) -> None:
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._start_server())
        self._ready.set()
        self._loop.run_forever()
        # Cleanup after loop stops
        if self._server:
            self._server.close()
            self._loop.run_until_complete(self._server.wait_closed())
        self._loop.close()

    async def _start_server(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, "0.0.0.0", self.port,
        )

    async def _handle(self, client_reader, client_writer) -> None:
        try:
            await self._process(client_reader, client_writer)
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass
        except Exception:
            try:
                client_writer.write(b"HTTP/1.1 500 Internal Server Error\r\n\r\n")
                await client_writer.drain()
            except (ConnectionError, OSError):
                pass
        finally:
            try:
                if not client_writer.is_closing():
                    client_writer.close()
            except (ConnectionError, OSError):
                pass

    async def _process(self, client_reader, client_writer) -> None:
        header_data = await asyncio.wait_for(
            client_reader.readuntil(b"\r\n\r\n"), timeout=15.0,
        )
        header_text = header_data.decode("ascii", errors="replace")
        lines = header_text.split("\r\n")
        first_line = lines[0]

        parts = first_line.split()
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            client_writer.write(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            await client_writer.drain()
            return

        target = parts[1]  # host:port

        # Parse target
        if ":" in target:
            host, port_str = target.rsplit(":", 1)
            port = int(port_str)
        else:
            host = target
            port = 443

        # Connect to destination
        try:
            dest_reader, dest_writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=15.0,
            )
        except Exception:
            client_writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await client_writer.drain()
            return

        self.connect_count += 1
        self.targets.append(target)

        # Tunnel established
        client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await client_writer.drain()

        # Relay until either side closes
        await _relay_bidir(client_reader, client_writer, dest_reader, dest_writer)

        try:
            dest_writer.close()
            await dest_writer.wait_closed()
        except (ConnectionError, OSError):
            pass


# =============================================================================
# Module-Scoped Fixtures
# =============================================================================


@pytest.fixture(scope="module")
def upstream_proxy():
    """Start a real CONNECT proxy on the configured port."""
    port = int(UPSTREAM_PROXY_PORT)
    proxy = _ThreadedConnectProxy(port)
    proxy.start()
    yield proxy
    proxy.stop()


@pytest.fixture(scope="module")
def proxy_test_setup(api, admin_headers, upstream_proxy):
    """Create test client with a network-isolated profile for upstream proxy tests."""
    clients = []
    client_headers_map = {}

    # Use "common" profile — allows all domains, blocks private IPs.
    # The upstream proxy connection bypasses policy (it's server-side config),
    # so this profile works fine.
    for label, profile in [("common", "common")]:
        cid = f"test-client-upstream-{label}-{random_suffix()}"
        resp = api.post("/v1/admin/clients", headers=admin_headers, json={
            "client_id": cid,
            "description": f"Upstream proxy test client ({label})",
            "role": "client",
            "security_profile": profile,
        })
        assert resp.status_code == 201, f"Failed to create client: {resp.text}"
        key = resp.json()["api_key"]
        clients.append(cid)
        client_headers_map[label] = {"Authorization": f"Bearer {key}"}

    yield {
        "headers": client_headers_map,
        "proxy": upstream_proxy,
    }

    # Teardown
    for cid in clients:
        api.delete(f"/v1/admin/clients/{cid}", headers=admin_headers)


# =============================================================================
# AI Tests: Upstream Proxy
# =============================================================================


def test_ai_job_completes_through_upstream_proxy(api, proxy_test_setup):
    """
    A job with a network-isolated profile should complete successfully
    when the server is configured with an upstream proxy.

    This implicitly proves the full chain works: the Claude CLI must
    reach api.anthropic.com through SandboxProxy → upstream proxy → internet.
    If the chain is broken, the job cannot complete.
    """
    headers = proxy_test_setup["headers"]["common"]
    proxy = proxy_test_setup["proxy"]
    count_before = proxy.connect_count

    prompt = (
        "Use the Bash tool to run: echo 'upstream_proxy_chain_works'\n"
        f"{RESULT_SCHEMA_INSTRUCTION} with test_name='upstream_proxy_basic'. "
        "Record action='echo_test'. "
        "If the echo command succeeded, set success=true. "
        "If it failed, set success=false and include the error in detail."
    )
    job = _submit_and_wait(api, headers, prompt)
    result = _parse_test_result(job)
    action = _find_action(result, "echo_test")
    assert action["success"] is True, (
        f"Simple echo should succeed. Detail: {action.get('detail')}"
    )

    # The proxy must have been contacted — at minimum for api.anthropic.com
    assert proxy.connect_count > count_before, (
        f"Upstream proxy was not contacted during the job. "
        f"connect_count before={count_before}, after={proxy.connect_count}. "
        f"This suggests the job bypassed the upstream proxy chain."
    )


def test_ai_curl_external_through_upstream_proxy(api, proxy_test_setup):
    """
    A job should be able to curl an external HTTPS site through the full
    upstream proxy chain.

    Proves that not just the Anthropic API, but arbitrary public
    destinations are reachable through the chain.
    """
    headers = proxy_test_setup["headers"]["common"]
    proxy = proxy_test_setup["proxy"]
    count_before = proxy.connect_count

    prompt = (
        "Use the Bash tool to run: curl -sS --max-time 15 https://ifconfig.me 2>&1\n"
        f"{RESULT_SCHEMA_INSTRUCTION} with test_name='upstream_proxy_curl'. "
        "Record action='curl_external'. "
        "If curl succeeded and returned an IP address, set success=true. "
        "If curl failed, set success=false and include the error message in detail."
    )
    job = _submit_and_wait(api, headers, prompt)
    result = _parse_test_result(job)
    action = _find_action(result, "curl_external")
    assert action["success"] is True, (
        f"curl to external HTTPS site should succeed through upstream proxy chain. "
        f"Detail: {action.get('detail')}"
    )

    # Additional connections for the curl target
    assert proxy.connect_count > count_before, (
        f"Upstream proxy was not contacted for curl request. "
        f"connect_count before={count_before}, after={proxy.connect_count}."
    )

    # Verify ifconfig.me was among the targets
    new_targets = proxy.targets[count_before:]
    ifconfig_targets = [t for t in new_targets if "ifconfig.me" in t]
    assert len(ifconfig_targets) > 0, (
        f"Expected ifconfig.me in proxy targets. New targets: {new_targets}"
    )
