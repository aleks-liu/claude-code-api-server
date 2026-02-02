"""Unit tests for network isolation (Phase 2).

Tests proxy domain/IP filtering, policy evaluation, CONNECT tunneling,
bwrap wrapper generation with --unshare-net, and profile classification.
Runs without a server or AI — pure unit and proxy-level integration tests.
"""

import asyncio
from pathlib import Path

import pytest

from helpers.unit_test_deps import skip_if_deps_missing
skip_if_deps_missing()

from src.sandbox_proxy import (
    _match_domain_pattern,
    _match_any_domain,
    _ip_in_ranges,
    _is_ip_address,
    _is_mandatory_domain,
    evaluate_policy,
    SandboxProxy,
    ProxyManager,
    PROXY_SOCKET_NAME,
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
# Mandatory Domains
# =============================================================================


class TestMandatoryDomains:
    def test_api_anthropic(self):
        assert _is_mandatory_domain("api.anthropic.com") is True

    def test_wildcard_anthropic(self):
        assert _is_mandatory_domain("console.anthropic.com") is True

    def test_non_anthropic(self):
        assert _is_mandatory_domain("evil.com") is False


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

    def test_mandatory_domain_always_allowed(self):
        policy = NetworkPolicy(
            allowed_domains=[],  # Nothing allowed
            denied_domains=["api.anthropic.com"],  # Even explicitly denied
        )
        allowed, reason = run(evaluate_policy(policy, "api.anthropic.com", "test-job"))
        assert allowed is True
        assert "Mandatory" in reason

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
            denied_ip_ranges=[],
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
            inner_script_path="/data/jobs/test/input/sandbox_inner.sh",
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

    def test_connect_mandatory_domain_bypasses_restrictions(self, tmp_path):
        """Anthropic API should be allowed even with empty allowed_domains."""
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
