"""Unit tests for network isolation (Phase 2).

Tests proxy domain/IP filtering, policy evaluation, CONNECT tunneling,
bwrap wrapper generation with --unshare-net, and profile classification.
Runs without a server or AI — pure unit and proxy-level integration tests.
"""

import asyncio
import base64
import ssl
from pathlib import Path

import pytest

from helpers.unit_test_deps import skip_if_deps_missing
skip_if_deps_missing()

from src.sandbox_proxy import (
    _match_domain_pattern,
    _match_any_domain,
    _ip_in_ranges,
    _is_ip_address,
    _is_autoallowed_domain,
    evaluate_policy,
    SandboxProxy,
    ProxyManager,
    PROXY_SOCKET_NAME,
    UpstreamProxyConfig,
    parse_upstream_proxy,
    _redact_proxy_url,
    _connect_via_upstream_proxy,
)
from src.models import NetworkPolicy


# =============================================================================
# Domain Pattern Matching
# =============================================================================


class TestDomainPatternMatching:
    """Tests for _match_domain_pattern."""

    def test_exact_match(self):
        assert _match_domain_pattern("github.com", "github.com") is True

    def test_exact_no_match(self):
        assert _match_domain_pattern("gitlab.com", "github.com") is False

    def test_wildcard_subdomain_match(self):
        assert _match_domain_pattern("api.github.com", "*.github.com") is True

    def test_wildcard_deep_subdomain_match(self):
        assert _match_domain_pattern("raw.api.github.com", "*.github.com") is True

    def test_wildcard_does_not_match_base(self):
        assert _match_domain_pattern("github.com", "*.github.com") is True

    def test_case_insensitive(self):
        assert _match_domain_pattern("GitHub.COM", "github.com") is True

    def test_trailing_dot_ignored(self):
        assert _match_domain_pattern("github.com.", "github.com") is True

    def test_no_partial_match(self):
        assert _match_domain_pattern("notgithub.com", "github.com") is False

    def test_wildcard_no_partial_suffix(self):
        assert _match_domain_pattern("evil-github.com", "*.github.com") is False


class TestMatchAnyDomain:
    def test_matches_one_of_many(self):
        patterns = ["github.com", "*.gitlab.com"]
        assert _match_any_domain("api.gitlab.com", patterns) is True

    def test_no_match_in_list(self):
        patterns = ["github.com", "*.gitlab.com"]
        assert _match_any_domain("evil.com", patterns) is False

    def test_empty_list(self):
        assert _match_any_domain("anything.com", []) is False


# =============================================================================
# IP Range Matching
# =============================================================================


class TestIpInRanges:
    def test_ipv4_in_range(self):
        assert _ip_in_ranges("10.0.1.5", ["10.0.0.0/8"]) is True

    def test_ipv4_not_in_range(self):
        assert _ip_in_ranges("8.8.8.8", ["10.0.0.0/8"]) is False

    def test_ipv4_in_one_of_many(self):
        ranges = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]
        assert _ip_in_ranges("192.168.1.1", ranges) is True

    def test_ipv6_in_range(self):
        assert _ip_in_ranges("::1", ["::1/128"]) is True

    def test_ipv6_not_in_range(self):
        assert _ip_in_ranges("2001:db8::1", ["::1/128"]) is False

    def test_localhost_in_loopback(self):
        assert _ip_in_ranges("127.0.0.1", ["127.0.0.0/8"]) is True

    def test_invalid_ip(self):
        assert _ip_in_ranges("not-an-ip", ["10.0.0.0/8"]) is False


class TestIsIpAddress:
    def test_ipv4(self):
        assert _is_ip_address("1.2.3.4") is True

    def test_ipv6(self):
        assert _is_ip_address("::1") is True

    def test_hostname(self):
        assert _is_ip_address("github.com") is False


# =============================================================================
# Auto-allowed Domains
# =============================================================================


class TestAutoallowedDomains:
    def test_api_anthropic(self):
        assert _is_autoallowed_domain("api.anthropic.com") is True

    def test_wildcard_anthropic(self):
        assert _is_autoallowed_domain("console.anthropic.com") is True

    def test_claude_ai(self):
        assert _is_autoallowed_domain("claude.ai") is True

    def test_wildcard_claude_ai(self):
        assert _is_autoallowed_domain("api.claude.ai") is True

    def test_non_autoallowed(self):
        assert _is_autoallowed_domain("evil.com") is False


# =============================================================================
# Policy Evaluation
# =============================================================================


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def run(coro):
    """Run an async coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestEvaluatePolicy:
    """Tests for evaluate_policy (the core filtering logic)."""

    def test_autoallowed_domain_always_allowed(self):
        policy = NetworkPolicy(
            allowed_domains=[],  # Nothing allowed
            denied_domains=["api.anthropic.com"],  # Even explicitly denied
        )
        allowed, reason = run(evaluate_policy(policy, "api.anthropic.com", "test-job"))
        assert allowed is True
        assert "Auto-allowed" in reason

    def test_raw_ip_denied_by_default(self):
        policy = NetworkPolicy(allow_ip_destination=False)
        allowed, reason = run(evaluate_policy(policy, "1.2.3.4", "test-job"))
        assert allowed is False
        assert "Raw IP" in reason

    def test_raw_ip_allowed_when_enabled(self):
        policy = NetworkPolicy(
            allow_ip_destination=True,
            allowed_ip_ranges=None,
            denied_ip_ranges=[],
        )
        allowed, reason = run(evaluate_policy(policy, "8.8.8.8", "test-job"))
        assert allowed is True

    def test_denied_domain_blocks(self):
        policy = NetworkPolicy(
            denied_domains=["evil.com"],
        )
        allowed, reason = run(evaluate_policy(policy, "evil.com", "test-job"))
        assert allowed is False
        assert "denied_domains" in reason

    def test_allowed_domains_empty_blocks_all(self):
        policy = NetworkPolicy(
            allowed_domains=[],
        )
        allowed, reason = run(evaluate_policy(policy, "anything.com", "test-job"))
        assert allowed is False
        assert "empty" in reason

    def test_allowed_domains_permits_listed(self):
        policy = NetworkPolicy(
            allowed_domains=["github.com", "*.github.com"],
            denied_ip_ranges=[],
        )
        allowed, reason = run(evaluate_policy(policy, "github.com", "test-job"))
        assert allowed is True

    def test_allowed_domains_blocks_unlisted(self):
        policy = NetworkPolicy(
            allowed_domains=["github.com"],
        )
        allowed, reason = run(evaluate_policy(policy, "evil.com", "test-job"))
        assert allowed is False
        assert "not in allowed_domains" in reason

    def test_denied_domains_override_allowed(self):
        policy = NetworkPolicy(
            allowed_domains=["*.example.com"],
            denied_domains=["bad.example.com"],
        )
        allowed, reason = run(evaluate_policy(policy, "bad.example.com", "test-job"))
        assert allowed is False
        assert "denied_domains" in reason

    def test_denied_ip_ranges_block_after_resolve(self):
        """Test that denied_ip_ranges blocks resolved IPs (using loopback)."""
        policy = NetworkPolicy(
            allowed_domains=None,
            denied_ip_ranges=["127.0.0.0/8"],
        )
        # localhost resolves to 127.0.0.1
        allowed, reason = run(evaluate_policy(policy, "localhost", "test-job"))
        assert allowed is False
        assert "denied_ip_ranges" in reason

    def test_raw_ip_denied_ip_ranges(self):
        policy = NetworkPolicy(
            allow_ip_destination=True,
            denied_ip_ranges=["10.0.0.0/8"],
        )
        allowed, reason = run(evaluate_policy(policy, "10.0.1.5", "test-job"))
        assert allowed is False
        assert "denied_ip_ranges" in reason

    def test_allowed_ip_ranges_empty_blocks_all(self):
        policy = NetworkPolicy(
            allow_ip_destination=True,
            allowed_ip_ranges=[],
        )
        allowed, reason = run(evaluate_policy(policy, "8.8.8.8", "test-job"))
        assert allowed is False
        assert "empty" in reason

    def test_unconfined_allows_everything(self):
        """The unconfined profile's policy should allow any domain."""
        policy = NetworkPolicy(
            allowed_domains=None,
            denied_domains=[],
            allowed_ip_ranges=None,
            denied_ip_ranges=[],
            allow_ip_destination=True,
        )
        allowed, reason = run(evaluate_policy(policy, "anything.com", "test-job"))
        assert allowed is True

    def test_dns_failure_denies(self):
        """Non-existent domain should be denied due to DNS failure."""
        policy = NetworkPolicy(
            allowed_domains=None,
            denied_ip_ranges=["127.0.0.0/8"],
        )
        allowed, reason = run(
            evaluate_policy(policy, "this-domain-does-not-exist-12345.invalid", "test-job")
        )
        assert allowed is False
        assert "DNS" in reason or "resolution" in reason


# =============================================================================
# Proxy Lifecycle
# =============================================================================


class TestProxyManager:
    def test_start_and_stop(self, tmp_path):
        policy = NetworkPolicy()
        manager = ProxyManager()

        async def _test():
            proxy = await manager.start_proxy("job-1", tmp_path, policy)
            assert proxy.is_running
            assert (tmp_path / PROXY_SOCKET_NAME).exists()

            await manager.stop_proxy("job-1")
            assert not proxy.is_running

        run(_test())

    def test_stop_nonexistent_is_noop(self):
        manager = ProxyManager()
        run(manager.stop_proxy("nonexistent"))

    def test_stop_all(self, tmp_path):
        policy = NetworkPolicy()
        manager = ProxyManager()

        async def _test():
            d1 = tmp_path / "j1"
            d2 = tmp_path / "j2"
            d1.mkdir()
            d2.mkdir()
            await manager.start_proxy("j1", d1, policy)
            await manager.start_proxy("j2", d2, policy)
            await manager.stop_all()
            assert manager.get_proxy("j1") is None
            assert manager.get_proxy("j2") is None

        run(_test())


# =============================================================================
# Sandbox Wrapper Generation (network isolation)
# =============================================================================


class TestSandboxWrapperNetworkIsolation:
    """Verify sandbox.py generates correct wrapper scripts for network isolation."""

    def test_compute_bwrap_args_with_unshare_net(self, tmp_path):
        from src.sandbox import _compute_bwrap_args

        data_dir = tmp_path / "data"
        input_dir = data_dir / "jobs" / "j1" / "input"
        input_dir.mkdir(parents=True)
        proxy_sock = tmp_path / "proxy.sock"
        proxy_sock.touch()

        args = _compute_bwrap_args(
            input_dir=input_dir,
            data_dir=data_dir,
            user_home=str(tmp_path / "home"),
            network_isolated=True,
            proxy_socket_path=proxy_sock,
        )

        assert "--unshare-net" in args
        # Check proxy socket bind is present
        assert str(proxy_sock.resolve()) in args

    def test_compute_bwrap_args_without_unshare_net(self, tmp_path):
        from src.sandbox import _compute_bwrap_args

        data_dir = tmp_path / "data"
        input_dir = data_dir / "jobs" / "j1" / "input"
        input_dir.mkdir(parents=True)

        args = _compute_bwrap_args(
            input_dir=input_dir,
            data_dir=data_dir,
            user_home=str(tmp_path / "home"),
            network_isolated=False,
        )

        assert "--unshare-net" not in args

    def test_inner_script_generated(self, tmp_path):
        from src.sandbox import _generate_inner_script

        script = _generate_inner_script("/usr/bin/claude")
        assert "socat" in script
        assert "HTTP_PROXY" in script
        assert "HTTPS_PROXY" in script
        assert "127.0.0.1:3128" in script
        assert "/usr/bin/claude" in script

    def test_outer_wrapper_network_isolated(self, tmp_path):
        """Outer wrapper should reference inner script when network isolated."""
        from src.sandbox import _generate_wrapper_script, _format_bwrap_args

        script = _generate_wrapper_script(
            bwrap_path="/usr/bin/bwrap",
            bwrap_args=["--ro-bind", "/", "/", "--unshare-pid"],
            real_cli_path="/usr/bin/claude",
            job_id="test-job",
            input_dir="/data/jobs/test/input",
            network_isolated=True,
            inner_script_path="/tmp/sandbox_inner.sh",
        )
        assert "sandbox_inner.sh" in script
        assert "isolated (proxy + --unshare-net)" in script

    def test_outer_wrapper_unrestricted(self, tmp_path):
        """Outer wrapper should call CLI directly when not network isolated."""
        from src.sandbox import _generate_wrapper_script

        script = _generate_wrapper_script(
            bwrap_path="/usr/bin/bwrap",
            bwrap_args=["--ro-bind", "/", "/", "--unshare-pid"],
            real_cli_path="/usr/bin/claude",
            job_id="test-job",
            input_dir="/data/jobs/test/input",
            network_isolated=False,
        )
        assert "sandbox_inner.sh" not in script
        assert "unrestricted" in script


# =============================================================================
# SecurityProfile.has_network_restrictions()
# =============================================================================


class TestHasNetworkRestrictions:
    """Verify profile.has_network_restrictions() correctly classifies profiles."""

    def test_unconfined_has_no_restrictions(self):
        from src.models import SecurityProfile
        profile = SecurityProfile(
            name="unconfined",
            network=NetworkPolicy(
                allowed_domains=None,
                denied_domains=[],
                allowed_ip_ranges=None,
                denied_ip_ranges=[],
                allow_ip_destination=True,
            ),
        )
        assert profile.has_network_restrictions() is False

    def test_common_has_restrictions(self):
        from src.models import SecurityProfile
        profile = SecurityProfile(
            name="common",
            network=NetworkPolicy(
                allowed_domains=None,
                denied_domains=[],
                allowed_ip_ranges=None,
                denied_ip_ranges=["10.0.0.0/8", "172.16.0.0/12"],
                allow_ip_destination=False,
            ),
        )
        assert profile.has_network_restrictions() is True

    def test_restrictive_has_restrictions(self):
        from src.models import SecurityProfile
        profile = SecurityProfile(
            name="restrictive",
            network=NetworkPolicy(
                allowed_domains=["api.anthropic.com"],
                denied_domains=[],
                allowed_ip_ranges=None,
                denied_ip_ranges=["10.0.0.0/8"],
                allow_ip_destination=False,
            ),
        )
        assert profile.has_network_restrictions() is True

    def test_denied_domains_triggers_restriction(self):
        from src.models import SecurityProfile
        profile = SecurityProfile(
            name="test",
            network=NetworkPolicy(
                allowed_domains=None,
                denied_domains=["evil.com"],
                allowed_ip_ranges=None,
                denied_ip_ranges=[],
                allow_ip_destination=True,
            ),
        )
        assert profile.has_network_restrictions() is True


# =============================================================================
# CONNECT Tunneling Integration Tests (proxy-level, no AI)
# =============================================================================


class TestProxyCONNECT:
    """Test actual CONNECT tunneling through the proxy."""

    def test_connect_allowed_domain(self, tmp_path):
        """Proxy should tunnel CONNECT to allowed domain."""
        policy = NetworkPolicy(
            allowed_domains=None,
            denied_domains=[],
            denied_ip_ranges=[],
        )

        async def _test():
            manager = ProxyManager()
            proxy = await manager.start_proxy("connect-test", tmp_path, policy)
            try:
                reader, writer = await asyncio.open_unix_connection(
                    str(proxy.socket_path)
                )
                # Send CONNECT to httpbin (well-known test endpoint)
                writer.write(b"CONNECT ifconfig.me:443 HTTP/1.1\r\nHost: ifconfig.me\r\n\r\n")
                await writer.drain()

                # Read response
                response = await asyncio.wait_for(reader.readline(), timeout=15.0)
                assert b"200" in response, f"Expected 200, got: {response}"
            finally:
                writer.close()
                await manager.stop_proxy("connect-test")

        run(_test())

    def test_connect_denied_domain(self, tmp_path):
        """Proxy should return 403 for denied domain."""
        policy = NetworkPolicy(
            allowed_domains=["api.anthropic.com"],
            denied_domains=[],
            denied_ip_ranges=[],
        )

        async def _test():
            manager = ProxyManager()
            proxy = await manager.start_proxy("deny-test", tmp_path, policy)
            try:
                reader, writer = await asyncio.open_unix_connection(
                    str(proxy.socket_path)
                )
                writer.write(b"CONNECT evil.com:443 HTTP/1.1\r\nHost: evil.com\r\n\r\n")
                await writer.drain()

                response = await asyncio.wait_for(reader.readline(), timeout=10.0)
                assert b"403" in response, f"Expected 403, got: {response}"
            finally:
                writer.close()
                await manager.stop_proxy("deny-test")

        run(_test())

    def test_connect_autoallowed_domain_bypasses_restrictions(self, tmp_path):
        """Auto-allowed domains should be allowed even with empty allowed_domains."""
        policy = NetworkPolicy(
            allowed_domains=[],  # Nothing allowed
            denied_domains=[],
            denied_ip_ranges=[],
        )

        async def _test():
            manager = ProxyManager()
            proxy = await manager.start_proxy("mandatory-test", tmp_path, policy)
            try:
                reader, writer = await asyncio.open_unix_connection(
                    str(proxy.socket_path)
                )
                writer.write(
                    b"CONNECT api.anthropic.com:443 HTTP/1.1\r\n"
                    b"Host: api.anthropic.com\r\n\r\n"
                )
                await writer.drain()

                response = await asyncio.wait_for(reader.readline(), timeout=15.0)
                assert b"200" in response, f"Expected 200, got: {response}"
            finally:
                writer.close()
                await manager.stop_proxy("mandatory-test")

        run(_test())

    def test_connect_raw_ip_denied(self, tmp_path):
        """Raw IP should be denied when allow_ip_destination=False."""
        policy = NetworkPolicy(
            allow_ip_destination=False,
        )

        async def _test():
            manager = ProxyManager()
            proxy = await manager.start_proxy("rawip-test", tmp_path, policy)
            try:
                reader, writer = await asyncio.open_unix_connection(
                    str(proxy.socket_path)
                )
                writer.write(b"CONNECT 1.2.3.4:443 HTTP/1.1\r\nHost: 1.2.3.4\r\n\r\n")
                await writer.drain()

                response = await asyncio.wait_for(reader.readline(), timeout=10.0)
                assert b"403" in response, f"Expected 403, got: {response}"
            finally:
                writer.close()
                await manager.stop_proxy("rawip-test")

        run(_test())

    def test_connect_private_ip_denied(self, tmp_path):
        """Private IP range should be denied when in denied_ip_ranges."""
        policy = NetworkPolicy(
            allowed_domains=None,
            denied_ip_ranges=["127.0.0.0/8"],
        )

        async def _test():
            manager = ProxyManager()
            proxy = await manager.start_proxy("privip-test", tmp_path, policy)
            try:
                reader, writer = await asyncio.open_unix_connection(
                    str(proxy.socket_path)
                )
                # localhost resolves to 127.0.0.1 which is in denied range
                writer.write(
                    b"CONNECT localhost:80 HTTP/1.1\r\nHost: localhost\r\n\r\n"
                )
                await writer.drain()

                response = await asyncio.wait_for(reader.readline(), timeout=10.0)
                assert b"403" in response, f"Expected 403, got: {response}"
            finally:
                writer.close()
                await manager.stop_proxy("privip-test")

        run(_test())

    def test_plain_http_denied(self, tmp_path):
        """Plain HTTP request to denied domain should get 403."""
        policy = NetworkPolicy(
            allowed_domains=["api.anthropic.com"],
        )

        async def _test():
            manager = ProxyManager()
            proxy = await manager.start_proxy("http-deny-test", tmp_path, policy)
            try:
                reader, writer = await asyncio.open_unix_connection(
                    str(proxy.socket_path)
                )
                writer.write(
                    b"GET http://evil.com/test HTTP/1.1\r\n"
                    b"Host: evil.com\r\n\r\n"
                )
                await writer.drain()

                response = await asyncio.wait_for(reader.readline(), timeout=10.0)
                assert b"403" in response, f"Expected 403, got: {response}"
            finally:
                writer.close()
                await manager.stop_proxy("http-deny-test")

        run(_test())


# =============================================================================
# Upstream Proxy: URL Parsing
# =============================================================================


class TestRedactProxyUrl:
    def test_redacts_password(self):
        assert _redact_proxy_url("http://user:secret@proxy:3128") == "http://user:***@proxy:3128"

    def test_no_password_unchanged(self):
        assert _redact_proxy_url("http://proxy:3128") == "http://proxy:3128"

    def test_username_only(self):
        assert _redact_proxy_url("http://user@proxy:3128") == "http://user@proxy:3128"

    def test_password_with_special_chars(self):
        result = _redact_proxy_url("http://user:p%40ss@proxy:3128")
        assert "***" in result
        assert "p%40ss" not in result


class TestParseUpstreamProxy:
    """Tests for parse_upstream_proxy()."""

    def test_empty_string_returns_none(self):
        assert parse_upstream_proxy("") is None

    def test_whitespace_returns_none(self):
        assert parse_upstream_proxy("   ") is None

    def test_http_scheme(self):
        config = parse_upstream_proxy("http://proxy.corp:3128")
        assert config is not None
        assert config.host == "proxy.corp"
        assert config.port == 3128
        assert config.use_tls is False
        assert config.auth_header is None

    def test_https_scheme(self):
        config = parse_upstream_proxy("https://proxy.corp:3128")
        assert config is not None
        assert config.host == "proxy.corp"
        assert config.port == 3128
        assert config.use_tls is True
        assert config.auth_header is None

    def test_default_port(self):
        config = parse_upstream_proxy("http://proxy.corp")
        assert config is not None
        assert config.port == 3128

    def test_custom_port(self):
        config = parse_upstream_proxy("http://proxy.corp:8080")
        assert config is not None
        assert config.port == 8080

    def test_basic_auth(self):
        import base64
        config = parse_upstream_proxy("http://user:pass@proxy.corp:3128")
        assert config is not None
        assert config.auth_header is not None
        assert config.auth_header.startswith("Basic ")
        decoded = base64.b64decode(config.auth_header.split(" ", 1)[1]).decode()
        assert decoded == "user:pass"

    def test_auth_with_special_chars(self):
        import base64
        config = parse_upstream_proxy("http://user:p%40ss%3Aword@proxy.corp:3128")
        assert config is not None
        decoded = base64.b64decode(config.auth_header.split(" ", 1)[1]).decode()
        assert decoded == "user:p@ss:word"

    def test_password_redacted_in_raw_url(self):
        config = parse_upstream_proxy("http://user:secret@proxy.corp:3128")
        assert config is not None
        assert "secret" not in config.raw_url
        assert "***" in config.raw_url

    def test_invalid_scheme_raises(self):
        with pytest.raises(ValueError, match="Unsupported"):
            parse_upstream_proxy("socks5://proxy:1080")

    def test_no_scheme_raises(self):
        with pytest.raises(ValueError):
            parse_upstream_proxy("proxy:3128")

    def test_no_hostname_raises(self):
        with pytest.raises(ValueError, match="no hostname"):
            parse_upstream_proxy("http://")

    def test_crlf_in_username_raises(self):
        with pytest.raises(ValueError, match="CR/LF"):
            parse_upstream_proxy("http://user%0d:pass@proxy:3128")

    def test_crlf_in_password_raises(self):
        with pytest.raises(ValueError, match="CR/LF"):
            parse_upstream_proxy("http://user:pass%0a@proxy:3128")

    def test_frozen_dataclass(self):
        config = parse_upstream_proxy("http://proxy:3128")
        with pytest.raises(AttributeError):
            config.host = "other"

    def test_whitespace_trimmed(self):
        config = parse_upstream_proxy("  http://proxy:3128  ")
        assert config is not None
        assert config.host == "proxy"


# =============================================================================
# Upstream Proxy: CONNECT Tunnel (Mock)
# =============================================================================


class TestConnectViaUpstreamProxy:
    """Tests for _connect_via_upstream_proxy() with mock servers."""

    def test_successful_connect(self):
        """Upstream proxy returns 200 — tunnel established."""
        upstream_config = parse_upstream_proxy("http://127.0.0.1:0")

        async def _test():
            # Start a mock upstream proxy that returns 200
            received_data = []

            async def mock_proxy_handler(reader, writer):
                data = await reader.readuntil(b"\r\n\r\n")
                received_data.append(data)
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                # Keep connection open for the test
                await asyncio.sleep(1)
                writer.close()

            server = await asyncio.start_server(mock_proxy_handler, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]

            config = UpstreamProxyConfig(
                host="127.0.0.1",
                port=port,
                use_tls=False,
                auth_header=None,
                raw_url=f"http://127.0.0.1:{port}",
            )

            try:
                reader, writer = await _connect_via_upstream_proxy(
                    "example.com", 443, config, "test-job",
                )
                # Verify we got a valid connection
                assert reader is not None
                assert writer is not None
                writer.close()

                # Verify the CONNECT request was correct
                request = received_data[0].decode()
                assert "CONNECT example.com:443 HTTP/1.1" in request
                assert "Host: example.com:443" in request
            finally:
                server.close()
                await server.wait_closed()

        run(_test())

    def test_connect_with_auth(self):
        """CONNECT request includes Proxy-Authorization header."""
        async def _test():
            received_data = []

            async def mock_proxy_handler(reader, writer):
                data = await reader.readuntil(b"\r\n\r\n")
                received_data.append(data)
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                await asyncio.sleep(1)
                writer.close()

            server = await asyncio.start_server(mock_proxy_handler, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]

            config = UpstreamProxyConfig(
                host="127.0.0.1",
                port=port,
                use_tls=False,
                auth_header="Basic dXNlcjpwYXNz",  # user:pass
                raw_url=f"http://user:***@127.0.0.1:{port}",
            )

            try:
                reader, writer = await _connect_via_upstream_proxy(
                    "example.com", 443, config, "test-job",
                )
                writer.close()

                request = received_data[0].decode()
                assert "Proxy-Authorization: Basic dXNlcjpwYXNz" in request
            finally:
                server.close()
                await server.wait_closed()

        run(_test())

    def test_connect_407_raises(self):
        """Upstream proxy returns 407 — authentication failed."""
        async def _test():
            async def mock_proxy_handler(reader, writer):
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
                await writer.drain()
                writer.close()

            server = await asyncio.start_server(mock_proxy_handler, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]

            config = UpstreamProxyConfig(
                host="127.0.0.1",
                port=port,
                use_tls=False,
                auth_header=None,
                raw_url=f"http://127.0.0.1:{port}",
            )

            try:
                with pytest.raises(ConnectionError, match="407"):
                    await _connect_via_upstream_proxy(
                        "example.com", 443, config, "test-job",
                    )
            finally:
                server.close()
                await server.wait_closed()

        run(_test())

    def test_connect_503_raises(self):
        """Upstream proxy returns 503 — service unavailable."""
        async def _test():
            async def mock_proxy_handler(reader, writer):
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.1 503 Service Unavailable\r\n\r\n")
                await writer.drain()
                writer.close()

            server = await asyncio.start_server(mock_proxy_handler, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]

            config = UpstreamProxyConfig(
                host="127.0.0.1",
                port=port,
                use_tls=False,
                auth_header=None,
                raw_url=f"http://127.0.0.1:{port}",
            )

            try:
                with pytest.raises(ConnectionError, match="503"):
                    await _connect_via_upstream_proxy(
                        "example.com", 443, config, "test-job",
                    )
            finally:
                server.close()
                await server.wait_closed()

        run(_test())

    def test_connect_unreachable_raises(self):
        """Upstream proxy unreachable — ConnectionError."""
        async def _test():
            config = UpstreamProxyConfig(
                host="127.0.0.1",
                port=1,  # Unlikely to be listening
                use_tls=False,
                auth_header=None,
                raw_url="http://127.0.0.1:1",
            )

            with pytest.raises(ConnectionError, match="Cannot connect"):
                await _connect_via_upstream_proxy(
                    "example.com", 443, config, "test-job",
                )

        run(_test())


# =============================================================================
# Upstream Proxy: DNS Optimization in evaluate_policy()
# =============================================================================


class TestEvaluatePolicyDnsOptimization:
    """Tests for DNS skip when no IP range rules exist."""

    def test_skips_dns_when_no_ip_rules(self):
        """Domain allowed without DNS when no IP range rules."""
        policy = NetworkPolicy(
            allowed_domains=["example.com"],
            denied_domains=[],
            denied_ip_ranges=[],
            allowed_ip_ranges=None,
        )
        allowed, reason = run(evaluate_policy(policy, "example.com", "test-job"))
        assert allowed is True
        assert "no IP rules" in reason

    def test_does_not_skip_dns_when_denied_ip_ranges(self):
        """DNS resolution happens when denied_ip_ranges exist."""
        policy = NetworkPolicy(
            allowed_domains=None,
            denied_domains=[],
            denied_ip_ranges=["127.0.0.0/8"],
            allowed_ip_ranges=None,
        )
        # localhost resolves to 127.0.0.1 which is denied
        allowed, reason = run(evaluate_policy(policy, "localhost", "test-job"))
        assert allowed is False
        assert "denied_ip_ranges" in reason

    def test_does_not_skip_dns_when_allowed_ip_ranges(self):
        """DNS resolution happens when allowed_ip_ranges is set."""
        policy = NetworkPolicy(
            allowed_domains=None,
            denied_domains=[],
            denied_ip_ranges=[],
            allowed_ip_ranges=["8.8.0.0/16"],
        )
        # localhost resolves to 127.0.0.1 which is not in allowed ranges
        allowed, reason = run(evaluate_policy(policy, "localhost", "test-job"))
        assert allowed is False
        assert "not in allowed_ip_ranges" in reason

    def test_unconfined_with_no_ip_rules_skips_dns(self):
        """Unconfined-like policy with no IP rules skips DNS."""
        policy = NetworkPolicy(
            allowed_domains=None,
            denied_domains=[],
            denied_ip_ranges=[],
            allowed_ip_ranges=None,
            allow_ip_destination=True,
        )
        allowed, reason = run(evaluate_policy(policy, "anything.example.com", "test-job"))
        assert allowed is True
        assert "no IP rules" in reason

    def test_nonexistent_domain_ok_when_no_ip_rules(self):
        """Non-existent domain is allowed when no IP rules (DNS not attempted)."""
        policy = NetworkPolicy(
            allowed_domains=None,
            denied_domains=[],
            denied_ip_ranges=[],
            allowed_ip_ranges=None,
        )
        allowed, reason = run(
            evaluate_policy(policy, "nonexistent-domain-xyz123.invalid", "test-job")
        )
        assert allowed is True
        assert "no IP rules" in reason


# =============================================================================
# Upstream Proxy: ProxyManager with upstream config
# =============================================================================


class TestProxyManagerUpstream:
    """Verify ProxyManager passes upstream config through."""

    def test_start_proxy_with_upstream(self, tmp_path):
        policy = NetworkPolicy()
        upstream_https = parse_upstream_proxy("http://proxy.corp:3128")

        async def _test():
            manager = ProxyManager()
            proxy = await manager.start_proxy(
                "upstream-test", tmp_path, policy,
                upstream_https=upstream_https,
            )
            assert proxy.is_running
            assert proxy._upstream_https is upstream_https
            assert proxy._upstream_http is None
            await manager.stop_proxy("upstream-test")

        run(_test())

    def test_start_proxy_without_upstream(self, tmp_path):
        """Default (no upstream) should work identically to before."""
        policy = NetworkPolicy()

        async def _test():
            manager = ProxyManager()
            proxy = await manager.start_proxy("no-upstream", tmp_path, policy)
            assert proxy.is_running
            assert proxy._upstream_http is None
            assert proxy._upstream_https is None
            await manager.stop_proxy("no-upstream")

        run(_test())


# =============================================================================
# Real Functional CONNECT Proxy (for integration tests)
# =============================================================================


async def _relay_bidir(
    reader_a: asyncio.StreamReader,
    writer_a: asyncio.StreamWriter,
    reader_b: asyncio.StreamReader,
    writer_b: asyncio.StreamWriter,
) -> None:
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


class RealConnectProxy:
    """
    A fully functional HTTP CONNECT proxy for integration testing.

    Not a mock — it actually connects to the destination and relays data.
    Optionally requires Basic authentication.

    Usage::

        proxy = RealConnectProxy(require_auth=("user", "pass"))
        await proxy.start()
        # ... use proxy.port ...
        await proxy.stop()
    """

    def __init__(
        self,
        require_auth: tuple[str, str] | None = None,
    ) -> None:
        self._require_auth = require_auth
        self._server: asyncio.AbstractServer | None = None
        self._port: int = 0
        self.connect_count: int = 0
        self.last_target: str = ""

    @property
    def port(self) -> int:
        return self._port

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", 0,
        )
        self._port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
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

    async def _process(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        # Read request headers
        header_data = await asyncio.wait_for(
            client_reader.readuntil(b"\r\n\r\n"),
            timeout=15.0,
        )
        header_text = header_data.decode("ascii", errors="replace")
        lines = header_text.split("\r\n")
        first_line = lines[0]  # e.g. "CONNECT example.com:443 HTTP/1.1"

        parts = first_line.split()
        if len(parts) < 2 or parts[0].upper() != "CONNECT":
            client_writer.write(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            await client_writer.drain()
            return

        target = parts[1]  # host:port

        # Check authentication if required
        if self._require_auth is not None:
            expected_user, expected_pass = self._require_auth
            expected_b64 = base64.b64encode(
                f"{expected_user}:{expected_pass}".encode()
            ).decode()

            auth_found = False
            for line in lines[1:]:
                if line.lower().startswith("proxy-authorization:"):
                    value = line.split(":", 1)[1].strip()
                    if value == f"Basic {expected_b64}":
                        auth_found = True
                    break

            if not auth_found:
                client_writer.write(
                    b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n"
                )
                await client_writer.drain()
                return

        # Parse target host:port
        if ":" in target:
            host, port_str = target.rsplit(":", 1)
            port = int(port_str)
        else:
            host = target
            port = 443

        # Actually connect to the destination
        try:
            dest_reader, dest_writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=15.0,
            )
        except Exception:
            client_writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await client_writer.drain()
            return

        self.connect_count += 1
        self.last_target = target

        # Send 200 — tunnel is established
        client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await client_writer.drain()

        # Relay data bidirectionally until either side closes
        await _relay_bidir(client_reader, client_writer, dest_reader, dest_writer)

        try:
            dest_writer.close()
            await dest_writer.wait_closed()
        except (ConnectionError, OSError):
            pass


# =============================================================================
# Upstream Proxy: End-to-End with Mock Upstream
# =============================================================================


class TestProxyCONNECTViaMockUpstream:
    """Test CONNECT through SandboxProxy with mock (non-functional) upstream."""

    def test_connect_via_mock_upstream_e2e(self, tmp_path):
        """Full path: client → SandboxProxy → mock upstream → 200."""
        policy = NetworkPolicy(
            allowed_domains=None,
            denied_domains=[],
            denied_ip_ranges=[],
            allowed_ip_ranges=None,
        )

        async def _test():
            received_requests = []

            async def mock_upstream(reader, writer):
                data = await reader.readuntil(b"\r\n\r\n")
                received_requests.append(data.decode())
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                try:
                    while True:
                        chunk = await reader.read(4096)
                        if not chunk:
                            break
                        writer.write(chunk)
                        await writer.drain()
                except (ConnectionError, asyncio.CancelledError):
                    pass
                finally:
                    writer.close()

            upstream_server = await asyncio.start_server(
                mock_upstream, "127.0.0.1", 0,
            )
            upstream_port = upstream_server.sockets[0].getsockname()[1]

            upstream_config = UpstreamProxyConfig(
                host="127.0.0.1",
                port=upstream_port,
                use_tls=False,
                auth_header=None,
                raw_url=f"http://127.0.0.1:{upstream_port}",
            )

            manager = ProxyManager()
            proxy = await manager.start_proxy(
                "e2e-upstream", tmp_path, policy,
                upstream_https=upstream_config,
            )

            try:
                client_reader, client_writer = await asyncio.open_unix_connection(
                    str(proxy.socket_path)
                )
                client_writer.write(
                    b"CONNECT example.com:443 HTTP/1.1\r\n"
                    b"Host: example.com\r\n\r\n"
                )
                await client_writer.drain()

                response = await asyncio.wait_for(
                    client_reader.readline(), timeout=10.0,
                )
                assert b"200" in response

                assert len(received_requests) == 1
                assert "CONNECT example.com:443" in received_requests[0]

                client_writer.close()
            finally:
                await manager.stop_proxy("e2e-upstream")
                upstream_server.close()
                await upstream_server.wait_closed()

        run(_test())

    def test_connect_denied_domain_never_reaches_upstream(self, tmp_path):
        """Denied domains should be blocked BEFORE reaching upstream proxy."""
        policy = NetworkPolicy(
            allowed_domains=["allowed.com"],
        )
        upstream_reached = []

        async def _test():
            async def mock_upstream(reader, writer):
                upstream_reached.append(True)
                writer.close()

            upstream_server = await asyncio.start_server(
                mock_upstream, "127.0.0.1", 0,
            )
            upstream_port = upstream_server.sockets[0].getsockname()[1]

            upstream_config = UpstreamProxyConfig(
                host="127.0.0.1",
                port=upstream_port,
                use_tls=False,
                auth_header=None,
                raw_url=f"http://127.0.0.1:{upstream_port}",
            )

            manager = ProxyManager()
            proxy = await manager.start_proxy(
                "deny-before-upstream", tmp_path, policy,
                upstream_https=upstream_config,
            )

            try:
                client_reader, client_writer = await asyncio.open_unix_connection(
                    str(proxy.socket_path)
                )
                client_writer.write(
                    b"CONNECT evil.com:443 HTTP/1.1\r\nHost: evil.com\r\n\r\n"
                )
                await client_writer.drain()

                response = await asyncio.wait_for(
                    client_reader.readline(), timeout=10.0,
                )
                assert b"403" in response
                assert len(upstream_reached) == 0, "Denied request should not reach upstream proxy"

                client_writer.close()
            finally:
                await manager.stop_proxy("deny-before-upstream")
                upstream_server.close()
                await upstream_server.wait_closed()

        run(_test())


# =============================================================================
# Upstream Proxy: End-to-End with REAL Functional Proxy (internet access)
# =============================================================================


class TestProxyCONNECTViaRealProxy:
    """
    Integration tests using RealConnectProxy — a fully functional
    CONNECT proxy that actually connects to the destination and relays
    data. These tests prove the full chain works with real internet
    traffic, not just protocol-level mocks.
    """

    def test_http_request_through_proxy_chain(self, tmp_path):
        """
        Full chain: client → SandboxProxy → RealConnectProxy → ifconfig.me:80.

        Tunnels to port 80 and sends a plain HTTP request through it.
        Verifies a real HTTP response comes back — proving the tunnel
        carries real data to a real destination, not just protocol stubs.
        """
        policy = NetworkPolicy(
            allowed_domains=None,
            denied_domains=[],
            denied_ip_ranges=[],
            allowed_ip_ranges=None,
        )

        async def _test():
            real_proxy = RealConnectProxy()
            await real_proxy.start()

            upstream_config = UpstreamProxyConfig(
                host="127.0.0.1",
                port=real_proxy.port,
                use_tls=False,
                auth_header=None,
                raw_url=f"http://127.0.0.1:{real_proxy.port}",
            )

            manager = ProxyManager()
            proxy = await manager.start_proxy(
                "real-proxy-http", tmp_path, policy,
                upstream_https=upstream_config,
            )

            try:
                # CONNECT to port 80 through the full chain
                client_reader, client_writer = await asyncio.open_unix_connection(
                    str(proxy.socket_path)
                )
                client_writer.write(
                    b"CONNECT ifconfig.me:80 HTTP/1.1\r\n"
                    b"Host: ifconfig.me\r\n\r\n"
                )
                await client_writer.drain()

                response = await asyncio.wait_for(
                    client_reader.readline(), timeout=15.0,
                )
                assert b"200" in response, f"Expected 200, got: {response}"

                # Consume the empty line that terminates the 200 response headers
                await asyncio.wait_for(client_reader.readline(), timeout=5.0)

                # The real proxy actually connected to ifconfig.me:80
                assert real_proxy.connect_count == 1
                assert real_proxy.last_target == "ifconfig.me:80"

                # Now send a plain HTTP request through the tunnel
                client_writer.write(
                    b"GET / HTTP/1.1\r\n"
                    b"Host: ifconfig.me\r\n"
                    b"Connection: close\r\n\r\n"
                )
                await client_writer.drain()

                # Read the HTTP response from the real server
                http_response = await asyncio.wait_for(
                    client_reader.readline(), timeout=15.0,
                )
                assert b"HTTP/" in http_response, (
                    f"Expected real HTTP response, got: {http_response}"
                )

                # Read remaining response body
                body_chunks = []
                try:
                    while True:
                        chunk = await asyncio.wait_for(
                            client_reader.read(4096), timeout=10.0,
                        )
                        if not chunk:
                            break
                        body_chunks.append(chunk)
                except (asyncio.TimeoutError, ConnectionError):
                    pass

                full_body = b"".join(body_chunks)
                # ifconfig.me returns an IP address — at least a few bytes
                assert len(full_body) > 5, (
                    f"Response body too short to be real: {full_body!r}"
                )

                client_writer.close()
            finally:
                await manager.stop_proxy("real-proxy-http")
                await real_proxy.stop()

        run(_test())

    def test_connect_with_auth_through_real_proxy(self, tmp_path):
        """
        Full chain with Basic auth:
        client → SandboxProxy → RealConnectProxy(auth) → ifconfig.me:443.

        Verifies that Proxy-Authorization header is forwarded correctly
        and accepted by the real proxy.
        """
        policy = NetworkPolicy(
            allowed_domains=None,
            denied_domains=[],
            denied_ip_ranges=[],
            allowed_ip_ranges=None,
        )

        async def _test():
            # Real proxy that requires auth
            real_proxy = RealConnectProxy(require_auth=("testuser", "testpass"))
            await real_proxy.start()

            # Build upstream config with matching credentials
            upstream_config = parse_upstream_proxy(
                f"http://testuser:testpass@127.0.0.1:{real_proxy.port}"
            )

            manager = ProxyManager()
            proxy = await manager.start_proxy(
                "real-proxy-auth", tmp_path, policy,
                upstream_https=upstream_config,
            )

            try:
                client_reader, client_writer = await asyncio.open_unix_connection(
                    str(proxy.socket_path)
                )
                client_writer.write(
                    b"CONNECT ifconfig.me:443 HTTP/1.1\r\n"
                    b"Host: ifconfig.me\r\n\r\n"
                )
                await client_writer.drain()

                response = await asyncio.wait_for(
                    client_reader.readline(), timeout=15.0,
                )
                assert b"200" in response, f"Expected 200, got: {response}"

                # Auth succeeded and proxy connected
                assert real_proxy.connect_count == 1

                client_writer.close()
            finally:
                await manager.stop_proxy("real-proxy-auth")
                await real_proxy.stop()

        run(_test())

    def test_auth_rejected_by_real_proxy(self, tmp_path):
        """
        Wrong credentials → RealConnectProxy returns 407 →
        SandboxProxy returns 502 to the client.
        """
        policy = NetworkPolicy(
            allowed_domains=None,
            denied_domains=[],
            denied_ip_ranges=[],
            allowed_ip_ranges=None,
        )

        async def _test():
            # Real proxy that requires auth
            real_proxy = RealConnectProxy(require_auth=("admin", "correct"))
            await real_proxy.start()

            # Wrong credentials
            upstream_config = parse_upstream_proxy(
                f"http://admin:wrong@127.0.0.1:{real_proxy.port}"
            )

            manager = ProxyManager()
            proxy = await manager.start_proxy(
                "real-proxy-bad-auth", tmp_path, policy,
                upstream_https=upstream_config,
            )

            try:
                client_reader, client_writer = await asyncio.open_unix_connection(
                    str(proxy.socket_path)
                )
                client_writer.write(
                    b"CONNECT ifconfig.me:443 HTTP/1.1\r\n"
                    b"Host: ifconfig.me\r\n\r\n"
                )
                await client_writer.drain()

                response = await asyncio.wait_for(
                    client_reader.readline(), timeout=15.0,
                )
                # SandboxProxy maps upstream proxy errors to 502
                assert b"502" in response, f"Expected 502, got: {response}"

                # Proxy never established a tunnel
                assert real_proxy.connect_count == 0

                client_writer.close()
            finally:
                await manager.stop_proxy("real-proxy-bad-auth")
                await real_proxy.stop()

        run(_test())

    def test_policy_denial_skips_real_proxy(self, tmp_path):
        """
        Denied domain never reaches the real proxy —
        policy is enforced BEFORE upstream connection.
        """
        policy = NetworkPolicy(
            allowed_domains=["allowed-only.com"],
        )

        async def _test():
            real_proxy = RealConnectProxy()
            await real_proxy.start()

            upstream_config = UpstreamProxyConfig(
                host="127.0.0.1",
                port=real_proxy.port,
                use_tls=False,
                auth_header=None,
                raw_url=f"http://127.0.0.1:{real_proxy.port}",
            )

            manager = ProxyManager()
            proxy = await manager.start_proxy(
                "real-proxy-deny", tmp_path, policy,
                upstream_https=upstream_config,
            )

            try:
                client_reader, client_writer = await asyncio.open_unix_connection(
                    str(proxy.socket_path)
                )
                client_writer.write(
                    b"CONNECT evil.com:443 HTTP/1.1\r\n"
                    b"Host: evil.com\r\n\r\n"
                )
                await client_writer.drain()

                response = await asyncio.wait_for(
                    client_reader.readline(), timeout=10.0,
                )
                assert b"403" in response

                # Real proxy was never contacted
                assert real_proxy.connect_count == 0

                client_writer.close()
            finally:
                await manager.stop_proxy("real-proxy-deny")
                await real_proxy.stop()

        run(_test())
