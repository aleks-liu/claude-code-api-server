"""Unit tests for seccomp BPF hardening (Phase 3).

Tests seccomp binary/filter detection, architecture selection,
npm package discovery, exec prefix generation, inner wrapper
script integration, graceful fallback when seccomp artifacts are
missing, and the full wiring from claude_runner → sandbox_wrapper.

Runs without a server or AI — pure unit tests.
"""

import asyncio
import os
import platform
import stat
import subprocess
import types
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from helpers.unit_test_deps import skip_if_deps_missing
skip_if_deps_missing()

from src.sandbox_seccomp import (
    SeccompConfig,
    _detect_arch_subdir,
    _find_npm_seccomp_dir,
    _ARCH_TO_SUBDIR,
    _SANDBOX_RUNTIME_PKG,
    _SANDBOX_RUNTIME_SECCOMP_SUBPATH,
    detect_seccomp,
    check_seccomp_at_startup,
    APPLY_SECCOMP_BINARY,
    BPF_FILTER_NAME,
    DEFAULT_SECCOMP_DIR,
)
from src.sandbox import _generate_inner_script


# =============================================================================
# Helper
# =============================================================================


def _create_seccomp_dir(tmp_path: Path, arch: str = "x86_64") -> Path:
    """Create a valid seccomp directory with fake binaries for the given arch."""
    seccomp_dir = tmp_path / "seccomp"
    seccomp_dir.mkdir(exist_ok=True)

    subdir_name = _ARCH_TO_SUBDIR.get(arch.lower(), "x64")
    arch_dir = seccomp_dir / subdir_name
    arch_dir.mkdir(exist_ok=True)

    # Create apply-seccomp binary (fake but executable)
    binary = arch_dir / APPLY_SECCOMP_BINARY
    binary.write_text("#!/bin/sh\n")
    binary.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP)

    # Create BPF filter
    bpf_file = arch_dir / BPF_FILTER_NAME
    bpf_file.write_bytes(b"\x00" * 104)

    return seccomp_dir


def _create_seccomp_dir_at(seccomp_dir: Path, arch: str = "x86_64") -> Path:
    """Create a valid seccomp directory at an exact path."""
    seccomp_dir.mkdir(parents=True, exist_ok=True)

    subdir_name = _ARCH_TO_SUBDIR.get(arch.lower(), "x64")
    arch_dir = seccomp_dir / subdir_name
    arch_dir.mkdir(exist_ok=True)

    binary = arch_dir / APPLY_SECCOMP_BINARY
    binary.write_text("#!/bin/sh\n")
    binary.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP)

    bpf_file = arch_dir / BPF_FILTER_NAME
    bpf_file.write_bytes(b"\x00" * 104)

    return seccomp_dir


# =============================================================================
# Architecture Detection
# =============================================================================


class TestDetectArchSubdir:
    """Tests for _detect_arch_subdir."""

    def test_x86_64_returns_x64(self):
        with patch("src.sandbox_seccomp.platform.machine", return_value="x86_64"):
            assert _detect_arch_subdir() == "x64"

    def test_amd64_returns_x64(self):
        with patch("src.sandbox_seccomp.platform.machine", return_value="amd64"):
            assert _detect_arch_subdir() == "x64"

    def test_aarch64_returns_arm64(self):
        with patch("src.sandbox_seccomp.platform.machine", return_value="aarch64"):
            assert _detect_arch_subdir() == "arm64"

    def test_arm64_returns_arm64(self):
        with patch("src.sandbox_seccomp.platform.machine", return_value="arm64"):
            assert _detect_arch_subdir() == "arm64"

    def test_unknown_arch_returns_none(self):
        with patch("src.sandbox_seccomp.platform.machine", return_value="s390x"):
            assert _detect_arch_subdir() is None

    def test_case_insensitive(self):
        with patch("src.sandbox_seccomp.platform.machine", return_value="X86_64"):
            assert _detect_arch_subdir() == "x64"


# =============================================================================
# SeccompConfig
# =============================================================================


class TestSeccompConfig:
    """Tests for SeccompConfig dataclass."""

    def test_exec_prefix_format(self):
        cfg = SeccompConfig(
            apply_seccomp_path=Path("/opt/ccas/seccomp/x64/apply-seccomp"),
            bpf_filter_path=Path("/opt/ccas/seccomp/x64/unix-block.bpf"),
        )
        prefix = cfg.exec_prefix()
        assert "apply-seccomp" in prefix
        assert "unix-block.bpf" in prefix
        assert "--" not in prefix

    def test_exec_prefix_paths_with_spaces(self):
        cfg = SeccompConfig(
            apply_seccomp_path=Path("/opt/my dir/x64/apply-seccomp"),
            bpf_filter_path=Path("/opt/my dir/x64/unix-block.bpf"),
        )
        prefix = cfg.exec_prefix()
        # shlex.quote wraps paths with spaces in single quotes
        assert "'" in prefix

    def test_frozen_dataclass(self):
        cfg = SeccompConfig(
            apply_seccomp_path=Path("/a"),
            bpf_filter_path=Path("/b"),
        )
        with pytest.raises(AttributeError):
            cfg.apply_seccomp_path = Path("/c")


# =============================================================================
# npm Package Discovery
# =============================================================================


class TestFindNpmSeccompDir:
    """Tests for _find_npm_seccomp_dir."""

    def test_finds_package_when_installed(self, tmp_path):
        """Returns path when sandbox-runtime is installed."""
        # Simulate npm root -g pointing to tmp_path
        pkg_dir = tmp_path / _SANDBOX_RUNTIME_PKG / _SANDBOX_RUNTIME_SECCOMP_SUBPATH
        pkg_dir.mkdir(parents=True)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = str(tmp_path) + "\n"

        with patch("src.sandbox_seccomp.subprocess.run", return_value=mock_result):
            result = _find_npm_seccomp_dir()
            assert result == pkg_dir

    def test_returns_none_when_not_installed(self, tmp_path):
        """Returns None when package dir doesn't exist."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = str(tmp_path) + "\n"

        with patch("src.sandbox_seccomp.subprocess.run", return_value=mock_result):
            result = _find_npm_seccomp_dir()
            assert result is None

    def test_returns_none_when_npm_fails(self):
        """Returns None when npm command fails."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""

        with patch("src.sandbox_seccomp.subprocess.run", return_value=mock_result):
            result = _find_npm_seccomp_dir()
            assert result is None

    def test_returns_none_when_npm_not_found(self):
        """Returns None when npm binary is not installed."""
        with patch("src.sandbox_seccomp.subprocess.run", side_effect=FileNotFoundError):
            result = _find_npm_seccomp_dir()
            assert result is None

    def test_returns_none_on_timeout(self):
        """Returns None when npm hangs."""
        with patch("src.sandbox_seccomp.subprocess.run", side_effect=subprocess.TimeoutExpired("npm", 5)):
            result = _find_npm_seccomp_dir()
            assert result is None


# =============================================================================
# detect_seccomp
# =============================================================================


class TestDetectSeccomp:
    """Tests for detect_seccomp function."""

    def test_valid_seccomp_dir(self, tmp_path):
        """Returns SeccompConfig when everything is present."""
        machine = platform.machine().lower()
        if machine not in _ARCH_TO_SUBDIR:
            pytest.skip(f"Unsupported test architecture: {machine}")

        seccomp_dir = _create_seccomp_dir(tmp_path, machine)
        result = detect_seccomp(seccomp_dir)

        assert result is not None
        assert isinstance(result, SeccompConfig)
        assert result.apply_seccomp_path.name == APPLY_SECCOMP_BINARY
        assert result.bpf_filter_path.name == BPF_FILTER_NAME
        assert result.bpf_filter_path.exists()

    def test_missing_root_directory(self, tmp_path):
        with patch("src.sandbox_seccomp._find_npm_seccomp_dir", return_value=None):
            result = detect_seccomp(tmp_path / "nonexistent")
            assert result is None

    def test_missing_arch_subdirectory(self, tmp_path):
        """Root dir exists but arch subdir does not."""
        seccomp_dir = tmp_path / "seccomp"
        seccomp_dir.mkdir()
        # No x64/ or arm64/ subdirectory
        result = detect_seccomp(seccomp_dir)
        assert result is None

    def test_missing_binary(self, tmp_path):
        machine = platform.machine().lower()
        if machine not in _ARCH_TO_SUBDIR:
            pytest.skip(f"Unsupported test architecture: {machine}")

        seccomp_dir = tmp_path / "seccomp"
        arch_dir = seccomp_dir / _ARCH_TO_SUBDIR[machine]
        arch_dir.mkdir(parents=True)
        # Only BPF filter, no binary
        (arch_dir / BPF_FILTER_NAME).write_bytes(b"\x00")

        result = detect_seccomp(seccomp_dir)
        assert result is None

    def test_binary_not_executable(self, tmp_path):
        machine = platform.machine().lower()
        if machine not in _ARCH_TO_SUBDIR:
            pytest.skip(f"Unsupported test architecture: {machine}")

        seccomp_dir = tmp_path / "seccomp"
        arch_dir = seccomp_dir / _ARCH_TO_SUBDIR[machine]
        arch_dir.mkdir(parents=True)

        binary = arch_dir / APPLY_SECCOMP_BINARY
        binary.write_text("#!/bin/sh\n")
        binary.chmod(stat.S_IRUSR)  # Read only

        (arch_dir / BPF_FILTER_NAME).write_bytes(b"\x00")

        result = detect_seccomp(seccomp_dir)
        assert result is None

    def test_missing_bpf_filter(self, tmp_path):
        machine = platform.machine().lower()
        if machine not in _ARCH_TO_SUBDIR:
            pytest.skip(f"Unsupported test architecture: {machine}")

        seccomp_dir = tmp_path / "seccomp"
        arch_dir = seccomp_dir / _ARCH_TO_SUBDIR[machine]
        arch_dir.mkdir(parents=True)

        binary = arch_dir / APPLY_SECCOMP_BINARY
        binary.write_text("#!/bin/sh\n")
        binary.chmod(stat.S_IRWXU)
        # No BPF filter

        result = detect_seccomp(seccomp_dir)
        assert result is None

    def test_wrong_arch_only(self, tmp_path):
        """Only the opposite arch dir exists."""
        machine = platform.machine().lower()
        if machine not in _ARCH_TO_SUBDIR:
            pytest.skip(f"Unsupported test architecture: {machine}")

        # Create for opposite arch
        if _ARCH_TO_SUBDIR[machine] == "x64":
            wrong_arch = "aarch64"
        else:
            wrong_arch = "x86_64"

        seccomp_dir = _create_seccomp_dir(tmp_path, wrong_arch)
        result = detect_seccomp(seccomp_dir)
        assert result is None

    def test_custom_seccomp_dir(self, tmp_path):
        machine = platform.machine().lower()
        if machine not in _ARCH_TO_SUBDIR:
            pytest.skip(f"Unsupported test architecture: {machine}")

        custom_dir = _create_seccomp_dir(tmp_path, machine)
        result = detect_seccomp(custom_dir)
        assert result is not None
        assert str(custom_dir.resolve()) in str(result.apply_seccomp_path)

    def test_falls_back_to_npm_when_dir_missing(self, tmp_path):
        """When primary dir doesn't exist, falls back to npm package."""
        machine = platform.machine().lower()
        if machine not in _ARCH_TO_SUBDIR:
            pytest.skip(f"Unsupported test architecture: {machine}")

        # Create seccomp dir inside a fake npm root
        npm_root = tmp_path / "npm_global"
        pkg_seccomp = npm_root / _SANDBOX_RUNTIME_PKG / _SANDBOX_RUNTIME_SECCOMP_SUBPATH
        _create_seccomp_dir_at(pkg_seccomp, machine)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = str(npm_root) + "\n"

        with patch("src.sandbox_seccomp.subprocess.run", return_value=mock_result):
            result = detect_seccomp(tmp_path / "nonexistent")
            assert result is not None
            assert isinstance(result, SeccompConfig)

    def test_default_dir_constant(self):
        assert DEFAULT_SECCOMP_DIR == Path("/opt/ccas/seccomp")


# =============================================================================
# check_seccomp_at_startup
# =============================================================================


class TestCheckSeccompAtStartup:
    def test_returns_config_when_available(self, tmp_path):
        machine = platform.machine().lower()
        if machine not in _ARCH_TO_SUBDIR:
            pytest.skip(f"Unsupported test architecture: {machine}")

        seccomp_dir = _create_seccomp_dir(tmp_path, machine)
        result = check_seccomp_at_startup(seccomp_dir)
        assert result is not None

    def test_returns_none_when_unavailable(self, tmp_path):
        with patch("src.sandbox_seccomp._find_npm_seccomp_dir", return_value=None):
            result = check_seccomp_at_startup(tmp_path / "nonexistent")
            assert result is None


# =============================================================================
# Inner Script with seccomp
# =============================================================================


class TestInnerScriptSeccomp:
    """Tests for _generate_inner_script with seccomp integration."""

    def test_with_seccomp_prefix(self):
        prefix = "/opt/ccas/seccomp/x64/apply-seccomp /opt/ccas/seccomp/x64/unix-block.bpf"
        script = _generate_inner_script(
            real_cli_path="/usr/local/bin/claude",
            seccomp_exec_prefix=prefix,
        )

        assert "apply-seccomp" in script
        assert "unix-block.bpf" in script
        assert "socat" in script
        assert "HTTP_PROXY" in script
        assert "degraded security" not in script
        assert "WARNING: Running without seccomp" not in script

    def test_without_seccomp_has_warning(self):
        script = _generate_inner_script(
            real_cli_path="/usr/local/bin/claude",
            seccomp_exec_prefix=None,
        )

        assert "apply-seccomp" not in script
        assert "WARNING: Running without seccomp" in script
        assert "degraded security" in script
        assert "socat" in script
        assert "HTTP_PROXY" in script

    def test_seccomp_exec_line_format(self):
        prefix = "/opt/ccas/seccomp/x64/apply-seccomp /opt/ccas/seccomp/x64/unix-block.bpf"
        script = _generate_inner_script(
            real_cli_path="/usr/local/bin/claude",
            seccomp_exec_prefix=prefix,
        )

        exec_lines = [l for l in script.split("\n") if l.strip().startswith("exec ")]
        assert len(exec_lines) == 1
        exec_line = exec_lines[0].strip()

        assert exec_line.startswith("exec ")
        assert "apply-seccomp" in exec_line
        assert "unix-block.bpf" in exec_line
        assert "claude" in exec_line
        assert '"$@"' in exec_line

    def test_no_seccomp_exec_line_direct(self):
        script = _generate_inner_script(
            real_cli_path="/usr/local/bin/claude",
            seccomp_exec_prefix=None,
        )

        exec_lines = [l for l in script.split("\n") if l.strip().startswith("exec ")]
        assert len(exec_lines) == 1
        exec_line = exec_lines[0].strip()

        assert "apply-seccomp" not in exec_line
        assert "claude" in exec_line

    def test_socat_always_present(self):
        for prefix in [
            "/opt/ccas/seccomp/x64/apply-seccomp /opt/ccas/seccomp/x64/unix-block.bpf",
            None,
        ]:
            script = _generate_inner_script(
                real_cli_path="/usr/local/bin/claude",
                seccomp_exec_prefix=prefix,
            )
            assert "socat TCP-LISTEN:3128" in script
            assert "UNIX-CONNECT:/tmp/proxy.sock" in script


# =============================================================================
# Sandbox Wrapper with seccomp pass-through
# =============================================================================


class TestSandboxWrapperSeccomp:
    """Verify create_sandbox_wrapper passes seccomp_exec_prefix to inner script."""

    def test_wrapper_with_seccomp(self, tmp_path):
        from src.sandbox import create_sandbox_wrapper, INNER_SCRIPT_NAME

        data_dir = tmp_path / "data"
        job_dir = data_dir / "jobs" / "test-job"
        input_dir = job_dir / "input"
        input_dir.mkdir(parents=True)

        proxy_sock = job_dir / "proxy.sock"
        proxy_sock.touch()

        fake_cli = tmp_path / "claude"
        fake_cli.write_text("#!/bin/sh\n")
        fake_cli.chmod(stat.S_IRWXU)

        seccomp_prefix = "/opt/ccas/seccomp/x64/apply-seccomp /opt/ccas/seccomp/x64/unix-block.bpf"

        try:
            create_sandbox_wrapper(
                job_id="test-seccomp-job",
                job_dir=job_dir,
                input_dir=input_dir,
                data_dir=data_dir,
                cli_path=str(fake_cli),
                network_isolated=True,
                proxy_socket_path=proxy_sock,
                seccomp_exec_prefix=seccomp_prefix,
            )
        except Exception:
            pytest.skip("bwrap not available in test environment")

        inner_script = input_dir / INNER_SCRIPT_NAME
        assert inner_script.exists()
        content = inner_script.read_text()
        assert "apply-seccomp" in content
        assert "unix-block.bpf" in content

    def test_wrapper_without_seccomp(self, tmp_path):
        from src.sandbox import create_sandbox_wrapper, INNER_SCRIPT_NAME

        data_dir = tmp_path / "data"
        job_dir = data_dir / "jobs" / "test-job"
        input_dir = job_dir / "input"
        input_dir.mkdir(parents=True)

        proxy_sock = job_dir / "proxy.sock"
        proxy_sock.touch()

        fake_cli = tmp_path / "claude"
        fake_cli.write_text("#!/bin/sh\n")
        fake_cli.chmod(stat.S_IRWXU)

        try:
            create_sandbox_wrapper(
                job_id="test-no-seccomp-job",
                job_dir=job_dir,
                input_dir=input_dir,
                data_dir=data_dir,
                cli_path=str(fake_cli),
                network_isolated=True,
                proxy_socket_path=proxy_sock,
                seccomp_exec_prefix=None,
            )
        except Exception:
            pytest.skip("bwrap not available in test environment")

        inner_script = input_dir / INNER_SCRIPT_NAME
        assert inner_script.exists()
        content = inner_script.read_text()
        assert "apply-seccomp" not in content
        assert "WARNING: Running without seccomp" in content

    def test_unconfined_no_inner_script(self, tmp_path):
        from src.sandbox import create_sandbox_wrapper, INNER_SCRIPT_NAME

        data_dir = tmp_path / "data"
        job_dir = data_dir / "jobs" / "test-job"
        input_dir = job_dir / "input"
        input_dir.mkdir(parents=True)

        fake_cli = tmp_path / "claude"
        fake_cli.write_text("#!/bin/sh\n")
        fake_cli.chmod(stat.S_IRWXU)

        try:
            create_sandbox_wrapper(
                job_id="test-unconfined-job",
                job_dir=job_dir,
                input_dir=input_dir,
                data_dir=data_dir,
                cli_path=str(fake_cli),
                network_isolated=False,
                seccomp_exec_prefix=None,
            )
        except Exception:
            pytest.skip("bwrap not available in test environment")

        inner_script = input_dir / INNER_SCRIPT_NAME
        assert not inner_script.exists()


# =============================================================================
# Config Integration
# =============================================================================


class TestSeccompSettingsConfig:
    """Tests for seccomp_dir in Settings."""

    def test_default_seccomp_dir(self):
        from src.config import Settings
        s = Settings(data_dir=Path("/tmp/test-data"))
        assert s.seccomp_dir == Path("/opt/ccas/seccomp")

    def test_custom_seccomp_dir_via_env(self):
        from src.config import Settings
        with patch.dict(os.environ, {"CCAS_SECCOMP_DIR": "/custom/seccomp"}):
            s = Settings(data_dir=Path("/tmp/test-data"))
            assert s.seccomp_dir == Path("/custom/seccomp")


# =============================================================================
# Runner → Sandbox Seccomp Wiring
# =============================================================================
#
# These tests verify that ClaudeRunner._execute() correctly detects seccomp
# and passes seccomp_exec_prefix to create_sandbox_wrapper().  This is the
# integration seam between claude_runner, sandbox_seccomp, and sandbox modules.
#
# The SDK and all non-seccomp dependencies are mocked; only the seccomp
# detection logic runs against controlled filesystem state.
# =============================================================================


# -- Mock SDK classes --------------------------------------------------------

class _MockResultMessage:
    """Minimal ResultMessage stand-in."""
    total_cost_usd = 0.001
    num_turns = 1
    duration_ms = 100


class _MockSDKClient:
    """Async-context-manager + async-iterator mock for ClaudeSDKClient."""

    def __init__(self, **kwargs):
        self._options = kwargs.get("options")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def query(self, prompt):
        pass

    async def receive_response(self):
        yield _MockResultMessage()

    async def interrupt(self):
        pass


class _FakeAgentOptions(dict):
    """Dict-like stand-in for ClaudeAgentOptions that accepts any kwargs."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.__dict__.update(kwargs)


# -- Fixture -----------------------------------------------------------------


def _build_runner_and_patches(
    tmp_path: Path,
    *,
    seccomp_available: bool,
    profile_has_network_restrictions: bool,
    bwrap_enabled: bool = True,
    network_enabled: bool = True,
):
    """
    Construct a ClaudeRunner and the ExitStack of patches needed to
    run ``runner.run()`` without real SDK or filesystem dependencies.

    Returns ``(runner, exit_stack, wrapper_spy)`` where ``wrapper_spy``
    is a MagicMock recording calls to ``create_sandbox_wrapper``.
    """
    from src.config import Settings
    from src.models import (
        JobMeta, JobStatus, SecurityProfile, NetworkPolicy,
    )
    from src.mcp_loader import McpConfig

    # -- settings --
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    job_dir = data_dir / "jobs" / "test-job"
    input_dir = job_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "output").mkdir(parents=True, exist_ok=True)

    # Seccomp dir — real filesystem so detect_seccomp actually runs
    if seccomp_available:
        seccomp_dir = _create_seccomp_dir(
            tmp_path, platform.machine().lower()
        )
    else:
        seccomp_dir = tmp_path / "empty-seccomp"
        seccomp_dir.mkdir(exist_ok=True)

    settings = Settings(
        data_dir=data_dir,
        enable_bwrap_sandbox=bwrap_enabled,
        sandbox_network_enabled=network_enabled,
        seccomp_dir=seccomp_dir,
    )

    # -- profile --
    if profile_has_network_restrictions:
        profile = SecurityProfile(
            name="restricted-test",
            network=NetworkPolicy(allowed_domains=["api.anthropic.com"]),
        )
    else:
        # Fully unconfined — no restrictions
        profile = SecurityProfile(
            name="unconfined-test",
            network=NetworkPolicy(allow_ip_destination=True),
        )

    # -- job meta --
    from datetime import datetime, UTC
    job_meta = JobMeta(
        job_id="test-seccomp-wiring",
        client_id="test-client",
        status=JobStatus.PENDING,
        created_at=datetime.now(UTC).replace(tzinfo=None),
        prompt="Hello",
        timeout_seconds=60,
        model="claude-sonnet-4-5",
    )

    # -- proxy mock --
    mock_proxy = MagicMock()
    mock_proxy.socket_path = job_dir / "proxy.sock"
    mock_proxy.socket_path.touch()

    mock_proxy_manager = MagicMock()
    mock_proxy_manager.start_proxy = AsyncMock(return_value=mock_proxy)
    mock_proxy_manager.stop_proxy = AsyncMock()

    # -- mock SDK module --
    mock_sdk = types.ModuleType("claude_agent_sdk")
    mock_sdk.ClaudeSDKClient = _MockSDKClient
    mock_sdk.ClaudeAgentOptions = _FakeAgentOptions
    mock_sdk.AssistantMessage = type("AssistantMessage", (), {})
    mock_sdk.ResultMessage = _MockResultMessage
    mock_sdk.TextBlock = type("TextBlock", (), {"text": ""})

    # -- profile manager mock --
    mock_profile_mgr = MagicMock()
    mock_profile_mgr.get_profile.return_value = profile
    mock_profile_mgr.get_default_profile.return_value = profile

    # -- auth manager mock --
    mock_client_record = MagicMock()
    mock_client_record.security_profile = profile.name

    mock_auth_mgr = MagicMock()
    mock_auth_mgr.get_client.return_value = mock_client_record

    # -- wrapper spy --
    wrapper_script = job_dir / "sandbox_wrapper.sh"
    wrapper_script.write_text("#!/bin/sh\n")
    wrapper_script.chmod(stat.S_IRWXU)
    wrapper_spy = MagicMock(return_value=wrapper_script)

    # -- enter all patches --
    stack = ExitStack()

    # SDK module
    stack.enter_context(
        patch.dict("sys.modules", {"claude_agent_sdk": mock_sdk})
    )

    # Profile manager
    stack.enter_context(
        patch("src.security_profiles.get_profile_manager", return_value=mock_profile_mgr)
    )

    # Auth manager (for _resolve_client_profile)
    stack.enter_context(
        patch("src.auth.get_auth_manager", return_value=mock_auth_mgr)
    )

    # Permission handler
    stack.enter_context(
        patch("src.security.create_permission_handler", return_value=lambda *a, **k: True)
    )

    # MCP — skip reload in _execute
    stack.enter_context(
        patch("src.claude_runner.McpManager")
    )
    stack.enter_context(
        patch("src.claude_runner.load_mcp_config", return_value=McpConfig())
    )

    # Proxy manager
    stack.enter_context(
        patch("src.sandbox_proxy.get_proxy_manager", return_value=mock_proxy_manager)
    )

    # Sandbox wrapper — SPY (this is what we assert on)
    stack.enter_context(
        patch("src.sandbox.create_sandbox_wrapper", wrapper_spy)
    )
    stack.enter_context(
        patch("src.sandbox.cleanup_sandbox_wrapper")
    )

    # bwrap validation (for __init__)
    stack.enter_context(
        patch("src.sandbox.validate_bwrap_installation", return_value="/usr/bin/bwrap")
    )

    # check_seccomp_at_startup (for __init__ — separate from per-job detect)
    stack.enter_context(
        patch("src.sandbox_seccomp.check_seccomp_at_startup", return_value=None)
    )

    # NOTE: We do NOT mock detect_seccomp — it runs against the real
    # seccomp_dir we created above so we test the full detection chain.
    # We DO need to suppress the npm fallback so it doesn't find the
    # real sandbox-runtime package on the dev machine.
    stack.enter_context(
        patch("src.sandbox_seccomp._find_npm_seccomp_dir", return_value=None)
    )

    # Construct runner (inside the patch context)
    from src.claude_runner import ClaudeRunner

    runner = ClaudeRunner(settings=settings, mcp_config=McpConfig())

    return runner, stack, wrapper_spy, job_meta, job_dir


class TestRunnerSeccompWiring:
    """
    Verify that ClaudeRunner._execute() correctly wires seccomp detection
    into the sandbox wrapper creation.

    These tests exercise the full path:
        runner.run()
          → _execute()
            → detect_seccomp(settings.seccomp_dir)   [real detection on fake fs]
            → seccomp_config.exec_prefix()            [real method]
            → create_sandbox_wrapper(..., seccomp_exec_prefix=...)  [spy]

    The SDK client, proxy, auth, profile, and MCP dependencies are mocked.
    """

    def test_network_isolated_with_seccomp_available(self, tmp_path):
        """
        When the profile has network restrictions and seccomp binaries
        exist, create_sandbox_wrapper must receive a non-None
        seccomp_exec_prefix containing apply-seccomp and the BPF filter.
        """
        machine = platform.machine().lower()
        if machine not in _ARCH_TO_SUBDIR:
            pytest.skip(f"Unsupported test architecture: {machine}")

        with ExitStack() as outer:
            runner, stack, wrapper_spy, job_meta, job_dir = _build_runner_and_patches(
                tmp_path,
                seccomp_available=True,
                profile_has_network_restrictions=True,
            )
            outer.enter_context(stack)

            result = asyncio.run(
                runner.run(
                    job_meta=job_meta,
                    job_dir=job_dir,
                    anthropic_key="sk-test-fake",
                )
            )

            # Verify wrapper was called
            wrapper_spy.assert_called_once()
            call_kwargs = wrapper_spy.call_args
            prefix = call_kwargs.kwargs.get(
                "seccomp_exec_prefix",
                call_kwargs[1].get("seccomp_exec_prefix") if len(call_kwargs) > 1 else None,
            )

            # If kwargs style, extract directly
            if prefix is None:
                # Try positional — unlikely but handle
                all_args = call_kwargs
                # create_sandbox_wrapper has seccomp_exec_prefix as keyword
                prefix = wrapper_spy.call_args.kwargs.get("seccomp_exec_prefix")

            assert prefix is not None, (
                "seccomp_exec_prefix should be set when seccomp is available"
            )
            assert "apply-seccomp" in prefix
            assert BPF_FILTER_NAME in prefix
            assert "--" not in prefix

            # Job should complete without error
            assert not result.is_error, f"Job failed: {result.error}"

    def test_network_isolated_without_seccomp(self, tmp_path):
        """
        When the profile has network restrictions but seccomp binaries
        are NOT available, create_sandbox_wrapper must receive
        seccomp_exec_prefix=None.
        """
        with ExitStack() as outer:
            runner, stack, wrapper_spy, job_meta, job_dir = _build_runner_and_patches(
                tmp_path,
                seccomp_available=False,
                profile_has_network_restrictions=True,
            )
            outer.enter_context(stack)

            result = asyncio.run(
                runner.run(
                    job_meta=job_meta,
                    job_dir=job_dir,
                    anthropic_key="sk-test-fake",
                )
            )

            wrapper_spy.assert_called_once()
            prefix = wrapper_spy.call_args.kwargs.get("seccomp_exec_prefix")
            assert prefix is None, (
                "seccomp_exec_prefix should be None when seccomp is unavailable"
            )

            assert not result.is_error, f"Job failed: {result.error}"

    def test_unconfined_profile_skips_seccomp_detection(self, tmp_path):
        """
        When the profile has NO network restrictions (unconfined),
        seccomp detection should not run and the sandbox wrapper
        (if created) should not receive a seccomp prefix.
        """
        machine = platform.machine().lower()
        if machine not in _ARCH_TO_SUBDIR:
            pytest.skip(f"Unsupported test architecture: {machine}")

        with ExitStack() as outer:
            runner, stack, wrapper_spy, job_meta, job_dir = _build_runner_and_patches(
                tmp_path,
                seccomp_available=True,  # Available but should NOT be used
                profile_has_network_restrictions=False,
            )
            outer.enter_context(stack)

            # Also spy on detect_seccomp to verify it's NOT called
            with patch("src.sandbox_seccomp.detect_seccomp", wraps=detect_seccomp) as detect_spy:
                result = asyncio.run(
                    runner.run(
                        job_meta=job_meta,
                        job_dir=job_dir,
                        anthropic_key="sk-test-fake",
                    )
                )

                # detect_seccomp should not be called for unconfined profiles
                detect_spy.assert_not_called()

            # Wrapper is still created (bwrap is enabled) but without seccomp
            wrapper_spy.assert_called_once()
            prefix = wrapper_spy.call_args.kwargs.get("seccomp_exec_prefix")
            assert prefix is None

            assert not result.is_error, f"Job failed: {result.error}"

    def test_bwrap_disabled_no_sandbox_wrapper(self, tmp_path):
        """
        When bwrap is disabled, no sandbox wrapper is created and
        seccomp detection is irrelevant.
        """
        with ExitStack() as outer:
            runner, stack, wrapper_spy, job_meta, job_dir = _build_runner_and_patches(
                tmp_path,
                seccomp_available=True,
                profile_has_network_restrictions=True,
                bwrap_enabled=False,
            )
            outer.enter_context(stack)

            result = asyncio.run(
                runner.run(
                    job_meta=job_meta,
                    job_dir=job_dir,
                    anthropic_key="sk-test-fake",
                )
            )

            # No wrapper created when bwrap is disabled
            wrapper_spy.assert_not_called()
            assert not result.is_error, f"Job failed: {result.error}"

    def test_network_isolation_disabled_no_seccomp(self, tmp_path):
        """
        When network isolation is disabled globally (sandbox_network_enabled=False),
        the profile's network restrictions are ignored and seccomp is not used.
        """
        machine = platform.machine().lower()
        if machine not in _ARCH_TO_SUBDIR:
            pytest.skip(f"Unsupported test architecture: {machine}")

        with ExitStack() as outer:
            runner, stack, wrapper_spy, job_meta, job_dir = _build_runner_and_patches(
                tmp_path,
                seccomp_available=True,
                profile_has_network_restrictions=True,
                network_enabled=False,
            )
            outer.enter_context(stack)

            with patch("src.sandbox_seccomp.detect_seccomp", wraps=detect_seccomp) as detect_spy:
                result = asyncio.run(
                    runner.run(
                        job_meta=job_meta,
                        job_dir=job_dir,
                        anthropic_key="sk-test-fake",
                    )
                )

                # detect_seccomp should not be called when network isolation is off
                detect_spy.assert_not_called()

            # Wrapper still created (bwrap enabled for filesystem isolation)
            # but network_isolated=False, so no seccomp prefix
            wrapper_spy.assert_called_once()
            prefix = wrapper_spy.call_args.kwargs.get("seccomp_exec_prefix")
            assert prefix is None

            assert not result.is_error, f"Job failed: {result.error}"

    def test_seccomp_prefix_matches_detected_config(self, tmp_path):
        """
        The seccomp_exec_prefix passed to create_sandbox_wrapper must
        exactly match what SeccompConfig.exec_prefix() returns for the
        detected binaries.
        """
        machine = platform.machine().lower()
        if machine not in _ARCH_TO_SUBDIR:
            pytest.skip(f"Unsupported test architecture: {machine}")

        with ExitStack() as outer:
            runner, stack, wrapper_spy, job_meta, job_dir = _build_runner_and_patches(
                tmp_path,
                seccomp_available=True,
                profile_has_network_restrictions=True,
            )
            outer.enter_context(stack)

            # Pre-compute expected prefix from the same seccomp dir
            expected_config = detect_seccomp(runner._settings.seccomp_dir)
            assert expected_config is not None, "detect_seccomp should find our test dir"
            expected_prefix = expected_config.exec_prefix()

            result = asyncio.run(
                runner.run(
                    job_meta=job_meta,
                    job_dir=job_dir,
                    anthropic_key="sk-test-fake",
                )
            )

            actual_prefix = wrapper_spy.call_args.kwargs.get("seccomp_exec_prefix")
            assert actual_prefix == expected_prefix, (
                f"Prefix mismatch.\n"
                f"  Expected: {expected_prefix!r}\n"
                f"  Actual:   {actual_prefix!r}"
            )

    def test_network_isolated_flag_passed_to_wrapper(self, tmp_path):
        """
        Verify network_isolated=True is passed to create_sandbox_wrapper
        when the profile has network restrictions.
        """
        with ExitStack() as outer:
            runner, stack, wrapper_spy, job_meta, job_dir = _build_runner_and_patches(
                tmp_path,
                seccomp_available=False,
                profile_has_network_restrictions=True,
            )
            outer.enter_context(stack)

            asyncio.run(
                runner.run(
                    job_meta=job_meta,
                    job_dir=job_dir,
                    anthropic_key="sk-test-fake",
                )
            )

            wrapper_spy.assert_called_once()
            net_isolated = wrapper_spy.call_args.kwargs.get("network_isolated")
            assert net_isolated is True

    def test_proxy_socket_passed_when_network_isolated(self, tmp_path):
        """
        When network-isolated, the proxy's socket_path must be forwarded
        to create_sandbox_wrapper.
        """
        with ExitStack() as outer:
            runner, stack, wrapper_spy, job_meta, job_dir = _build_runner_and_patches(
                tmp_path,
                seccomp_available=False,
                profile_has_network_restrictions=True,
            )
            outer.enter_context(stack)

            asyncio.run(
                runner.run(
                    job_meta=job_meta,
                    job_dir=job_dir,
                    anthropic_key="sk-test-fake",
                )
            )

            wrapper_spy.assert_called_once()
            proxy_sock = wrapper_spy.call_args.kwargs.get("proxy_socket_path")
            assert proxy_sock is not None
            assert "proxy.sock" in str(proxy_sock)
