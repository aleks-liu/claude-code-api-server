"""
seccomp BPF integration for Claude Code API Server sandbox.

Detects and configures pre-compiled seccomp BPF filters that block direct
socket creation inside the bwrap sandbox.  This forces all network traffic
through the HTTP proxy, preventing bypass via raw sockets.

The filters and the ``apply-seccomp`` helper binary are sourced from
Anthropic's ``@anthropic-ai/sandbox-runtime`` npm package (MIT licensed).
In Docker, a symlink at ``/opt/ccas/seccomp`` points to the package's
``vendor/seccomp/`` directory.  Outside Docker, the module auto-discovers
the package via ``npm root -g``.

Expected filesystem layout::

    <seccomp_dir>/
        x64/
            apply-seccomp       # Static C binary for x86_64
            unix-block.bpf      # Pre-compiled BPF filter for x86_64
        arm64/
            apply-seccomp       # Static C binary for aarch64
            unix-block.bpf      # Pre-compiled BPF filter for aarch64

When the binary or filter is missing, the sandbox falls back to running
without seccomp (degraded security).  A warning is logged so operators
are aware of the reduced protection.
"""

import os
import platform
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .logging_config import get_logger

logger = get_logger(__name__)


# =============================================================================
# Constants
# =============================================================================

# Default directory where seccomp artifacts are expected.
# In Docker this is a symlink to the sandbox-runtime npm package.
# Overridden via Settings.seccomp_dir (CCAS_SECCOMP_DIR env var).
DEFAULT_SECCOMP_DIR = Path("/opt/ccas/seccomp")

# npm package that ships seccomp artifacts.
_SANDBOX_RUNTIME_PKG = "@anthropic-ai/sandbox-runtime"
_SANDBOX_RUNTIME_SECCOMP_SUBPATH = Path("vendor/seccomp")

# Binary and filter names (same across architectures).
APPLY_SECCOMP_BINARY = "apply-seccomp"
BPF_FILTER_NAME = "unix-block.bpf"

# Mapping from platform.machine() values to the arch subdirectory name
# used in the vendored seccomp directory structure.
_ARCH_TO_SUBDIR: dict[str, str] = {
    "x86_64": "x64",
    "amd64": "x64",
    "aarch64": "arm64",
    "arm64": "arm64",
}


# =============================================================================
# npm Package Discovery
# =============================================================================


def _find_npm_seccomp_dir() -> Optional[Path]:
    """
    Locate seccomp artifacts inside the globally-installed sandbox-runtime
    npm package.

    Returns:
        Path to the ``vendor/seccomp`` directory, or ``None`` if the
        package is not installed or ``npm`` is not available.
    """
    try:
        result = subprocess.run(
            ["npm", "root", "-g"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None

        npm_root = Path(result.stdout.strip())
        seccomp_dir = npm_root / _SANDBOX_RUNTIME_PKG / _SANDBOX_RUNTIME_SECCOMP_SUBPATH
        if seccomp_dir.is_dir():
            logger.info(
                "seccomp_found_via_npm",
                path=str(seccomp_dir),
                message=f"Found seccomp artifacts in npm package: {seccomp_dir}",
            )
            return seccomp_dir
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        # npm not installed or not reachable
        pass
    return None


# =============================================================================
# Data Types
# =============================================================================


@dataclass(frozen=True)
class SeccompConfig:
    """Resolved paths for seccomp binary and BPF filter."""

    apply_seccomp_path: Path
    bpf_filter_path: Path

    def exec_prefix(self) -> str:
        """
        Return the shell command prefix that applies the seccomp filter
        before executing the target binary.

        Usage in inner wrapper::

            exec <prefix> /path/to/claude "$@"
        """
        return (
            f"{shlex.quote(str(self.apply_seccomp_path))} "
            f"{shlex.quote(str(self.bpf_filter_path))}"
        )


# =============================================================================
# Detection
# =============================================================================


def _detect_arch_subdir() -> Optional[str]:
    """
    Detect the CPU architecture and return the matching subdirectory name.

    Returns:
        Subdirectory name (``"x64"`` or ``"arm64"``), or ``None`` if the
        architecture is unsupported.
    """
    machine = platform.machine().lower()
    subdir = _ARCH_TO_SUBDIR.get(machine)
    if subdir is None:
        logger.warning(
            "seccomp_unsupported_architecture",
            machine=machine,
            supported=list(_ARCH_TO_SUBDIR.keys()),
            message=f"No seccomp artifacts available for architecture '{machine}'.",
        )
    return subdir


def detect_seccomp(seccomp_dir: Optional[Path] = None) -> Optional[SeccompConfig]:
    """
    Detect whether seccomp BPF enforcement is available.

    Checks for the ``apply-seccomp`` binary and ``unix-block.bpf`` filter
    in the architecture-specific subdirectory of ``seccomp_dir``.

    Args:
        seccomp_dir: Root directory containing arch subdirectories with
            seccomp artifacts.  Defaults to ``DEFAULT_SECCOMP_DIR``.

    Returns:
        A ``SeccompConfig`` with resolved paths if everything is present
        and executable, or ``None`` if seccomp is not available.
    """
    if seccomp_dir is None:
        seccomp_dir = DEFAULT_SECCOMP_DIR

    seccomp_dir = seccomp_dir.resolve()

    # 1. Check root directory exists; fall back to npm package discovery
    if not seccomp_dir.is_dir():
        npm_dir = _find_npm_seccomp_dir()
        if npm_dir is not None:
            seccomp_dir = npm_dir.resolve()
        else:
            logger.info(
                "seccomp_dir_not_found",
                seccomp_dir=str(seccomp_dir),
                message="seccomp directory not found. Running without seccomp hardening.",
            )
            return None

    # 2. Detect architecture subdirectory
    arch_subdir = _detect_arch_subdir()
    if arch_subdir is None:
        return None

    arch_dir = seccomp_dir / arch_subdir
    if not arch_dir.is_dir():
        logger.warning(
            "seccomp_arch_dir_not_found",
            expected_path=str(arch_dir),
            architecture=platform.machine(),
            message=(
                f"Architecture directory '{arch_subdir}' not found in "
                f"{seccomp_dir}. Running without seccomp hardening."
            ),
        )
        return None

    # 3. Check apply-seccomp binary
    apply_path = arch_dir / APPLY_SECCOMP_BINARY
    if not apply_path.is_file():
        logger.warning(
            "seccomp_binary_not_found",
            expected_path=str(apply_path),
            message="apply-seccomp binary not found. Running without seccomp hardening.",
        )
        return None

    if not os.access(apply_path, os.X_OK):
        logger.warning(
            "seccomp_binary_not_executable",
            path=str(apply_path),
            message="apply-seccomp binary is not executable. Running without seccomp hardening.",
        )
        return None

    # 4. Check BPF filter
    bpf_path = arch_dir / BPF_FILTER_NAME
    if not bpf_path.is_file():
        logger.warning(
            "seccomp_bpf_filter_not_found",
            expected_path=str(bpf_path),
            architecture=platform.machine(),
            message=f"BPF filter '{BPF_FILTER_NAME}' not found. Running without seccomp hardening.",
        )
        return None

    logger.info(
        "seccomp_available",
        apply_seccomp=str(apply_path),
        bpf_filter=str(bpf_path),
        architecture=platform.machine(),
        arch_subdir=arch_subdir,
        message="seccomp BPF hardening is available.",
    )

    return SeccompConfig(
        apply_seccomp_path=apply_path,
        bpf_filter_path=bpf_path,
    )


def check_seccomp_at_startup(seccomp_dir: Optional[Path] = None) -> Optional[SeccompConfig]:
    """
    Check seccomp availability at server startup and log the result.

    This is a convenience wrapper around ``detect_seccomp()`` that provides
    clear startup diagnostics.

    Args:
        seccomp_dir: Directory containing seccomp artifacts.

    Returns:
        SeccompConfig if available, None otherwise.
    """
    config = detect_seccomp(seccomp_dir)
    if config is None:
        logger.warning(
            "seccomp_not_available",
            seccomp_dir=str(seccomp_dir or DEFAULT_SECCOMP_DIR),
            message=(
                "seccomp BPF hardening is NOT available. Network-isolated "
                "jobs will run without AF_UNIX socket-level enforcement. "
                "TCP/UDP is still isolated by --unshare-net, but the process "
                "can connect to host-side Unix sockets (Docker, dbus, etc.) "
                "exposed via the read-only filesystem bind. Install the "
                "vendored seccomp binaries to /opt/ccas/seccomp/ for full "
                "protection."
            ),
        )
    return config
