"""
Process-level bwrap (bubblewrap) sandbox for Claude Code API Server.

This module provides the PRIMARY security isolation mechanism: wrapping the
entire Claude Code CLI process in a bwrap namespace so that ALL tools
(Bash, Read, Write, sub-agents, etc.) run inside an isolated filesystem.

Architecture::

    HOST PROCESS (Python API server)
      |
      +-- SDK spawns cli_path (our wrapper script)
            |
            +-- wrapper.sh: exec bwrap [...] -- /real/claude "$@"
                  |
                  +-- Claude Code CLI (Node.js) -- EVERYTHING sandboxed
                        |-- Bash commands  -> sandboxed
                        |-- Read/Write/Edit -> sandboxed
                        |-- Sub-agents     -> sandboxed
                        +-- Child processes -> sandboxed

Why this is necessary:

    By sandboxing the entire CLI process, every child process -- regardless
    of how the CLI spawns it -- inherits the restricted namespace.

Filesystem layout inside the sandbox::

    /                -> read-only bind from host
    /home/<user>     -> empty tmpfs (user data hidden)
    /root            -> empty tmpfs
    /tmp             -> fresh writable tmpfs
    <data_dir>       -> empty tmpfs (cross-job isolation)
    <input_dir>      -> read-write bind from host (job workspace)
    /dev             -> devtmpfs
    /proc            -> procfs

When the security profile has network restrictions (non-unconfined),
the sandbox adds ``--unshare-net`` for full network namespace isolation
and bind-mounts a proxy Unix socket into the sandbox.  An inner wrapper
script (``sandbox_inner.sh``) runs socat to bridge the socket to TCP
``127.0.0.1:3128`` and sets ``HTTP_PROXY``/``HTTPS_PROXY`` env vars.

For the ``unconfined`` profile, the wrapper is generated WITHOUT
``--unshare-net`` and without the inner wrapper (current behavior).

The ``can_use_tool`` callback in ``security.py`` enforces security
profile policies (denied tools, MCP server access).
"""

import os
import pwd
import shlex
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Optional

from .logging_config import get_logger

logger = get_logger(__name__)


# =============================================================================
# Exceptions
# =============================================================================


class SandboxError(Exception):
    """Base exception for sandbox-related errors."""

    pass


class SandboxSetupError(SandboxError):
    """Raised when the sandbox cannot be set up (missing binary, bad config)."""

    pass


class SandboxCreationError(SandboxError):
    """Raised when wrapper script creation fails at runtime."""

    pass


# =============================================================================
# Constants
# =============================================================================

# Name of the wrapper scripts created in each job directory.
WRAPPER_SCRIPT_NAME = "sandbox_wrapper.sh"
INNER_SCRIPT_NAME = "sandbox_inner.sh"

# Path where the proxy socket is mounted inside the sandbox.
_SANDBOX_PROXY_SOCKET = "/tmp/proxy.sock"

# TCP port that socat listens on inside the sandbox.
_SANDBOX_PROXY_PORT = 3128

# Common Claude CLI installation paths to search if ``which`` fails.
_CLAUDE_CLI_SEARCH_PATHS = [
    "/usr/local/bin/claude",
    "/usr/bin/claude",
    "/opt/claude/bin/claude",
]

# Minimum bwrap version is not enforced, but we log the detected version
# for diagnostic purposes.


# =============================================================================
# Discovery Helpers
# =============================================================================


def _find_claude_cli() -> str:
    """
    Locate the real Claude Code CLI binary.

    Searches ``PATH`` first via ``shutil.which``, then falls back to
    common installation paths.

    Returns:
        Absolute path to the Claude CLI binary.

    Raises:
        SandboxSetupError: If the CLI cannot be found.
    """
    # 1. Try PATH lookup (most reliable)
    cli_path = shutil.which("claude")
    if cli_path:
        resolved = str(Path(cli_path).resolve())
        logger.debug("claude_cli_found_via_path", path=resolved)
        return resolved

    # 2. Try common installation paths
    for candidate in _CLAUDE_CLI_SEARCH_PATHS:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            resolved = str(Path(candidate).resolve())
            logger.debug("claude_cli_found_via_fallback", path=resolved)
            return resolved

    raise SandboxSetupError(
        "Claude Code CLI binary not found. Searched PATH and common "
        f"locations: {_CLAUDE_CLI_SEARCH_PATHS}. Ensure Claude Code is "
        "installed and available in PATH."
    )


def _detect_user_home() -> str:
    """
    Detect the home directory of the user running the server process.

    Uses ``pwd.getpwuid`` for authoritative lookup, falling back to
    ``Path.home()`` and ``HOME`` environment variable.

    Returns:
        Absolute path to the user's home directory.

    Raises:
        SandboxSetupError: If home directory cannot be determined.
    """
    # 1. Authoritative: passwd database lookup
    try:
        pw_entry = pwd.getpwuid(os.getuid())
        home = pw_entry.pw_dir
        if home and os.path.isdir(home):
            logger.debug("user_home_detected_via_pwd", home=home)
            return str(Path(home).resolve())
    except (KeyError, OSError) as exc:
        logger.debug("pwd_lookup_failed", error=str(exc))

    # 2. Fallback: Path.home()
    try:
        home = str(Path.home())
        if home and os.path.isdir(home):
            logger.debug("user_home_detected_via_pathlib", home=home)
            return str(Path(home).resolve())
    except (RuntimeError, OSError) as exc:
        logger.debug("pathlib_home_failed", error=str(exc))

    # 3. Last resort: HOME environment variable
    home = os.environ.get("HOME", "")
    if home and os.path.isdir(home):
        logger.debug("user_home_detected_via_env", home=home)
        return str(Path(home).resolve())

    raise SandboxSetupError(
        "Cannot determine user home directory. Tried pwd database, "
        "Path.home(), and HOME environment variable."
    )


# =============================================================================
# bwrap Validation
# =============================================================================


def validate_bwrap_installation(bwrap_path: str = "bwrap") -> str:
    """
    Validate that bwrap is installed, executable, and functional.

    Performs a quick smoke test by running a trivial command inside a
    minimal bwrap sandbox to confirm the binary actually works (not
    just that it exists).

    Args:
        bwrap_path: Name or absolute path of the bwrap binary.

    Returns:
        Resolved absolute path to the bwrap binary.

    Raises:
        SandboxSetupError: If bwrap is not found or fails the smoke test.
    """
    # 1. Find the binary
    resolved_path = shutil.which(bwrap_path)
    if resolved_path is None:
        raise SandboxSetupError(
            f"bwrap binary not found at '{bwrap_path}'. "
            "Install bubblewrap: sudo apt-get install bubblewrap"
        )

    logger.debug("bwrap_binary_found", path=resolved_path)

    # 2. Smoke test: run a trivial command inside a minimal sandbox.
    #    This catches issues like missing kernel support for user namespaces,
    #    AppArmor/SELinux blocking bwrap, etc.
    try:
        result = subprocess.run(
            [
                resolved_path,
                "--ro-bind", "/usr", "/usr",
                "--symlink", "usr/lib", "/lib",
                "--symlink", "usr/lib64", "/lib64",
                "--symlink", "usr/bin", "/bin",
                "--symlink", "usr/sbin", "/sbin",
                "--proc", "/proc",
                "--dev", "/dev",
                "--unshare-pid",
                "--die-with-parent",
                "--", "/bin/echo", "bwrap_smoke_test_ok",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode != 0:
            raise SandboxSetupError(
                f"bwrap smoke test failed (exit code {result.returncode}). "
                f"stderr: {result.stderr.strip()!r}. "
                "This may indicate missing kernel namespace support or "
                "AppArmor/SELinux restrictions."
            )

        if "bwrap_smoke_test_ok" not in result.stdout:
            raise SandboxSetupError(
                "bwrap smoke test produced unexpected output: "
                f"{result.stdout.strip()!r}"
            )

        logger.debug("bwrap_smoke_test_passed", path=resolved_path)

    except subprocess.TimeoutExpired:
        raise SandboxSetupError(
            "bwrap smoke test timed out after 10 seconds. "
            "This may indicate a kernel or configuration issue."
        )
    except FileNotFoundError:
        raise SandboxSetupError(
            f"bwrap binary at '{resolved_path}' could not be executed. "
            "Check file permissions."
        )
    except OSError as exc:
        raise SandboxSetupError(
            f"bwrap smoke test OS error: {exc}"
        )

    return resolved_path


# =============================================================================
# Mount Argument Computation
# =============================================================================


def _compute_intermediate_dirs(
    parent_path: Path,
    child_path: Path,
    include_last: bool = False,
) -> list[str]:
    """
    Compute ``--dir`` arguments needed to create intermediate directories
    between ``parent_path`` and ``child_path`` inside a tmpfs.

    After ``--tmpfs parent_path``, the path is empty. To mount something
    at ``child_path``, we need ``--dir`` entries for every intermediate
    component so the mount point exists.

    Args:
        parent_path: The tmpfs'd parent directory.
        child_path: The target path that needs to be reachable.
        include_last: If True, include ``child_path`` itself (needed when
            the child will be ``--dir``'d, not bind-mounted).

    Returns:
        List of absolute paths that need ``--dir`` entries, in order.

    Raises:
        SandboxCreationError: If child_path is not under parent_path.
    """
    try:
        relative = child_path.relative_to(parent_path)
    except ValueError:
        raise SandboxCreationError(
            f"Cannot compute intermediate dirs: {child_path} is not "
            f"under {parent_path}"
        )

    parts = relative.parts
    if not include_last and len(parts) > 0:
        parts = parts[:-1]

    dirs: list[str] = []
    current = parent_path
    for part in parts:
        current = current / part
        dirs.append(str(current))

    return dirs


def _is_path_under(child: Path, parent: Path) -> bool:
    """Check if ``child`` is a descendant of ``parent`` (both resolved)."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _compute_bwrap_args(
    input_dir: Path,
    data_dir: Path,
    user_home: str,
    extra_ro_binds: Optional[list[Path]] = None,
    network_isolated: bool = False,
    proxy_socket_path: Optional[Path] = None,
) -> list[str]:
    """
    Compute the full list of bwrap command-line arguments for process-level
    sandboxing.

    This implements the mount strategy:

    1. ``--ro-bind / /`` — full host filesystem, read-only.
    2. ``--tmpfs <user_home>`` — hide user data.
    3. ``--tmpfs /root`` — hide root's home.
    4. ``--tmpfs /tmp`` — fresh writable tmp.
    5. ``--tmpfs <data_dir>`` — hide all job data (cross-job isolation).
       If ``data_dir`` is under ``user_home``, intermediate ``--dir``
       entries are created so the mount point is reachable.
    6. ``--dir`` entries from ``data_dir`` to ``input_dir``'s parent.
    7. ``--bind <input_dir> <input_dir>`` — job workspace, read-write.
    8. ``--dev /dev``, ``--proc /proc`` — device and proc filesystems.
    9. ``--unshare-pid``, ``--die-with-parent`` — PID isolation & cleanup.

    Args:
        input_dir: Resolved absolute path to the job's input directory.
        data_dir: Resolved absolute path to the server's data directory.
        user_home: Resolved absolute path to the user's home directory.
        extra_ro_binds: Additional paths under data_dir to expose
            read-only inside the sandbox (e.g., MCP package directories,
            plugin directories for agents and skills).
            These are bind-mounted after the data_dir tmpfs and before
            the job input bind.

    Returns:
        List of bwrap arguments (NOT including the bwrap binary itself).

    Raises:
        SandboxCreationError: If the mount layout cannot be computed.
    """
    input_dir_resolved = input_dir.resolve()
    data_dir_resolved = data_dir.resolve()
    user_home_resolved = Path(user_home).resolve()

    # Validate: input_dir must be under data_dir.
    if not _is_path_under(input_dir_resolved, data_dir_resolved):
        raise SandboxCreationError(
            f"input_dir ({input_dir_resolved}) is not under "
            f"data_dir ({data_dir_resolved}). Cannot create sandbox."
        )

    args: list[str] = []

    # ---- 1. Full host filesystem, read-only ----
    args.extend(["--ro-bind", "/", "/"])

    # ---- 2. Hide user's home directory ----
    args.extend(["--tmpfs", str(user_home_resolved)])

    # ---- 3. Hide root's home directory ----
    args.extend(["--tmpfs", "/root"])

    # ---- 4. Fresh /tmp ----
    args.extend(["--tmpfs", "/tmp"])

    # ---- 5. Hide data directory (cross-job isolation) ----
    # If data_dir is under user_home, we need to create intermediate
    # directories first (since user_home is now an empty tmpfs).
    if _is_path_under(data_dir_resolved, user_home_resolved):
        intermediate_dirs = _compute_intermediate_dirs(
            parent_path=user_home_resolved,
            child_path=data_dir_resolved,
            include_last=True,  # We need data_dir itself as a --dir before --tmpfs
        )
        for d in intermediate_dirs:
            args.extend(["--dir", d])

    args.extend(["--tmpfs", str(data_dir_resolved)])

    # ---- 5b. Re-expose additional read-only directories inside data_dir ----
    # Used for MCP package directories (e.g., /data/mcp/npm/node_modules,
    # /data/mcp/venv) that need to be visible inside the sandbox so MCP
    # server processes can read their own code.
    if extra_ro_binds:
        for bind_path in extra_ro_binds:
            bind_resolved = bind_path.resolve()

            # Only process paths that are actually under data_dir and exist
            if not _is_path_under(bind_resolved, data_dir_resolved):
                logger.warning(
                    "extra_ro_bind_outside_data_dir",
                    bind_path=str(bind_resolved),
                    data_dir=str(data_dir_resolved),
                    message="Skipping extra ro-bind: path is not under data_dir",
                )
                continue

            if not bind_resolved.is_dir():
                logger.debug(
                    "extra_ro_bind_not_found",
                    bind_path=str(bind_resolved),
                    message="Skipping extra ro-bind: directory does not exist",
                )
                continue

            # Create intermediate --dir entries from data_dir to bind_path
            # so the mount point is reachable within the tmpfs.
            intermediate_dirs = _compute_intermediate_dirs(
                parent_path=data_dir_resolved,
                child_path=bind_resolved,
                include_last=False,
            )
            for d in intermediate_dirs:
                args.extend(["--dir", d])

            # Read-only bind mount from host to sandbox
            args.extend(["--ro-bind", str(bind_resolved), str(bind_resolved)])

            logger.debug(
                "extra_ro_bind_added",
                bind_path=str(bind_resolved),
            )

    # ---- 6. Create intermediate dirs from data_dir to input_dir ----
    intermediate_dirs = _compute_intermediate_dirs(
        parent_path=data_dir_resolved,
        child_path=input_dir_resolved,
        include_last=False,  # input_dir itself is the bind mount target
    )
    for d in intermediate_dirs:
        args.extend(["--dir", d])

    # ---- 7. Bind-mount job input directory (read-write) ----
    args.extend(["--bind", str(input_dir_resolved), str(input_dir_resolved)])

    # ---- 8. Device and proc filesystems ----
    args.extend(["--dev", "/dev"])
    args.extend(["--proc", "/proc"])

    # ---- 9. Namespace isolation ----
    args.extend(["--unshare-pid"])

    # ---- 9b. Network namespace isolation ----
    if network_isolated:
        args.extend(["--unshare-net"])

        # Bind-mount the proxy Unix socket into the sandbox
        if proxy_socket_path is not None:
            args.extend([
                "--bind",
                str(proxy_socket_path.resolve()),
                _SANDBOX_PROXY_SOCKET,
            ])

    args.extend(["--die-with-parent"])

    return args


# =============================================================================
# Wrapper Script Generation
# =============================================================================


def _format_bwrap_args(bwrap_args: list[str]) -> str:
    """Format bwrap arguments as continuation lines for a shell script."""
    bwrap_lines: list[str] = []
    i = 0
    while i < len(bwrap_args):
        arg = bwrap_args[i]
        if arg.startswith("--") and (i + 1) < len(bwrap_args) and not bwrap_args[i + 1].startswith("--"):
            bwrap_lines.append(f"    {shlex.quote(arg)} {shlex.quote(bwrap_args[i + 1])}")
            i += 2
        else:
            bwrap_lines.append(f"    {shlex.quote(arg)}")
            i += 1
    return " \\\n".join(bwrap_lines)


def _generate_wrapper_script(
    bwrap_path: str,
    bwrap_args: list[str],
    real_cli_path: str,
    job_id: str,
    input_dir: str,
    network_isolated: bool = False,
    inner_script_path: Optional[str] = None,
) -> str:
    """
    Generate the content of the bwrap outer wrapper shell script.

    When ``network_isolated`` is True, the bwrap command invokes the
    inner wrapper script (which starts socat and sets proxy env vars)
    instead of the CLI directly.

    Args:
        bwrap_path: Absolute path to the bwrap binary.
        bwrap_args: Pre-computed bwrap arguments (from ``_compute_bwrap_args``).
        real_cli_path: Absolute path to the real Claude CLI binary.
        job_id: Job identifier (for the script header comment).
        input_dir: Job input directory (for the script header comment).
        network_isolated: Whether network isolation is active.
        inner_script_path: Path to the inner wrapper script inside the
            sandbox (only used when network_isolated is True).

    Returns:
        The complete shell script as a string.
    """
    bwrap_args_str = _format_bwrap_args(bwrap_args)

    if network_isolated and inner_script_path:
        # Two-part wrapper: bwrap -> inner script -> claude CLI
        exec_target = shlex.quote(inner_script_path)
    else:
        # Single wrapper: bwrap -> claude CLI directly
        exec_target = '"$REAL_CLI"'

    script = f"""\
#!/bin/sh
set -eu

# =============================================================================
# Process-level bwrap sandbox wrapper for Claude Code CLI
# Auto-generated by Claude Code API Server. DO NOT EDIT.
#
# Job ID:    {job_id}
# Input dir: {input_dir}
# Network:   {"isolated (proxy + --unshare-net)" if network_isolated else "unrestricted"}
# =============================================================================

REAL_CLI={shlex.quote(real_cli_path)}
BWRAP={shlex.quote(bwrap_path)}

# Allow unsandboxed version/help checks.
# The SDK may invoke these for capability probing and they do not
# access any sensitive data.
for arg in "$@"; do
    case "$arg" in
        -v|--version|-h|--help)
            exec "$REAL_CLI" "$@"
            ;;
    esac
done

# Execute the CLI inside the bwrap sandbox.
# All child processes (bash commands, sub-agents, etc.) inherit
# the restricted namespace.
exec "$BWRAP" \\
{bwrap_args_str} \\
    -- {exec_target} "$@"
"""
    return script


def _generate_inner_script(
    real_cli_path: str,
    seccomp_exec_prefix: Optional[str] = None,
) -> str:
    """
    Generate the inner wrapper script that runs inside the bwrap sandbox.

    This script:
    1. Starts socat to bridge the proxy Unix socket to TCP 127.0.0.1:3128
    2. Sets HTTP_PROXY/HTTPS_PROXY environment variables
    3. Optionally applies seccomp BPF filter to block direct socket creation
    4. Executes the real Claude CLI

    Args:
        real_cli_path: Absolute path to the real Claude CLI binary.
        seccomp_exec_prefix: If provided, the ``apply-seccomp`` command
            prefix (e.g. ``"/opt/ccas/seccomp/apply-seccomp /opt/.../filter.bpf --"``)
            that is prepended to the exec line.  When ``None``, the CLI
            runs without seccomp enforcement (degraded security).

    Returns:
        The complete inner script as a string.
    """
    if seccomp_exec_prefix:
        exec_line = f"exec {seccomp_exec_prefix} {shlex.quote(real_cli_path)} \"$@\""
        seccomp_comment = "# Apply seccomp BPF filter and execute Claude CLI"
    else:
        exec_line = f"exec {shlex.quote(real_cli_path)} \"$@\""
        seccomp_comment = (
            "# WARNING: Running without seccomp BPF filter (degraded security).\n"
            "# Direct socket creation is not blocked — proxy bypass is theoretically possible.\n"
            "# Install vendored seccomp binaries for full protection.\n"
            "# Execute Claude CLI"
        )

    return f"""\
#!/bin/sh
set -eu

# =============================================================================
# Inner sandbox wrapper — bridges proxy socket and runs Claude CLI
# Auto-generated by Claude Code API Server. DO NOT EDIT.
# =============================================================================

# Bridge Unix socket to TCP for HTTP proxy
socat TCP-LISTEN:{_SANDBOX_PROXY_PORT},fork,bind=127.0.0.1,reuseaddr UNIX-CONNECT:{_SANDBOX_PROXY_SOCKET} &
SOCAT_PID=$!

# Brief wait for socat to bind
sleep 0.1

# Set proxy environment variables (comprehensive list for compatibility)
export HTTP_PROXY=http://127.0.0.1:{_SANDBOX_PROXY_PORT}
export HTTPS_PROXY=http://127.0.0.1:{_SANDBOX_PROXY_PORT}
export http_proxy=http://127.0.0.1:{_SANDBOX_PROXY_PORT}
export https_proxy=http://127.0.0.1:{_SANDBOX_PROXY_PORT}
export ALL_PROXY=http://127.0.0.1:{_SANDBOX_PROXY_PORT}
export all_proxy=http://127.0.0.1:{_SANDBOX_PROXY_PORT}

{seccomp_comment}
{exec_line}
"""


# =============================================================================
# Public API
# =============================================================================


def create_sandbox_wrapper(
    job_id: str,
    job_dir: Path,
    input_dir: Path,
    data_dir: Path,
    bwrap_path: str = "bwrap",
    cli_path: Optional[str] = None,
    extra_ro_binds: Optional[list[Path]] = None,
    network_isolated: bool = False,
    proxy_socket_path: Optional[Path] = None,
    seccomp_exec_prefix: Optional[str] = None,
) -> Path:
    """
    Create a bwrap sandbox wrapper script for a specific job.

    The wrapper script is placed in ``job_dir/sandbox_wrapper.sh``
    (outside of ``input_dir``, so it cannot be tampered with from
    inside the sandbox).

    When ``network_isolated`` is True, an additional inner wrapper
    script is created that starts socat (bridging the proxy socket
    to TCP) and sets proxy environment variables before running the CLI.

    Args:
        job_id: Job identifier for logging and script comments.
        job_dir: Job directory (parent of ``input_dir``).
        input_dir: Absolute path to the job's input directory.
            This is the ONLY writable directory inside the sandbox.
        data_dir: Absolute path to the server's data directory.
            The entire data_dir is hidden (tmpfs) except for input_dir.
        bwrap_path: Path to the bwrap binary (resolved via ``validate_bwrap_installation``).
        cli_path: Path to the real Claude CLI binary.
            If None, auto-detected via ``_find_claude_cli()``.
        extra_ro_binds: Additional paths under data_dir to expose
            read-only inside the sandbox (e.g., MCP package directories,
            plugin directories for agents and skills).
        network_isolated: If True, add ``--unshare-net`` and generate
            a two-part wrapper with socat bridge and proxy env vars.
        proxy_socket_path: Path to the proxy Unix socket on the host
            (only used when ``network_isolated`` is True).
        seccomp_exec_prefix: Shell command prefix for applying seccomp
            BPF filter (e.g. ``"apply-seccomp filter.bpf --"``).
            Only used when ``network_isolated`` is True.  When None,
            the inner wrapper runs without seccomp (degraded security).

    Returns:
        Path to the created wrapper script.

    Raises:
        SandboxSetupError: If bwrap or Claude CLI cannot be found.
        SandboxCreationError: If the wrapper script cannot be created.
    """
    logger.info(
        "sandbox_wrapper_creating",
        job_id=job_id,
        input_dir=str(input_dir),
        data_dir=str(data_dir),
    )

    # ---- 1. Validate prerequisites ----
    resolved_bwrap = validate_bwrap_installation(bwrap_path)

    if cli_path is not None:
        real_cli = cli_path
        if not os.path.isfile(real_cli) or not os.access(real_cli, os.X_OK):
            raise SandboxSetupError(
                f"Specified Claude CLI path is not executable: {real_cli}"
            )
        logger.debug("claude_cli_path_explicit", path=real_cli)
    else:
        real_cli = _find_claude_cli()

    user_home = _detect_user_home()

    # ---- 2. Resolve paths ----
    input_dir_resolved = input_dir.resolve()
    data_dir_resolved = data_dir.resolve()
    job_dir_resolved = job_dir.resolve()

    # Validate directory structure
    if not input_dir_resolved.is_dir():
        raise SandboxCreationError(
            f"Input directory does not exist: {input_dir_resolved}"
        )

    if not data_dir_resolved.is_dir():
        raise SandboxCreationError(
            f"Data directory does not exist: {data_dir_resolved}"
        )

    if not _is_path_under(input_dir_resolved, data_dir_resolved):
        raise SandboxCreationError(
            f"Input directory ({input_dir_resolved}) is not under "
            f"data directory ({data_dir_resolved}). "
            "This is required for cross-job isolation."
        )

    # ---- 3. Compute bwrap arguments ----
    try:
        bwrap_args = _compute_bwrap_args(
            input_dir=input_dir_resolved,
            data_dir=data_dir_resolved,
            user_home=user_home,
            extra_ro_binds=extra_ro_binds,
            network_isolated=network_isolated,
            proxy_socket_path=proxy_socket_path,
        )
    except SandboxCreationError:
        raise
    except Exception as exc:
        raise SandboxCreationError(
            f"Failed to compute bwrap arguments: {exc}"
        ) from exc

    logger.debug(
        "bwrap_args_computed",
        job_id=job_id,
        arg_count=len(bwrap_args),
        user_home=user_home,
        network_isolated=network_isolated,
    )

    # ---- 4. Write inner script (network-isolated jobs only) ----
    inner_script_sandbox_path: str | None = None

    if network_isolated:
        try:
            inner_content = _generate_inner_script(
                real_cli_path=real_cli,
                seccomp_exec_prefix=seccomp_exec_prefix,
            )
            inner_path = job_dir_resolved / INNER_SCRIPT_NAME
            inner_path.write_text(inner_content, encoding="utf-8")
            inner_path.chmod(
                stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH
            )

            # The inner script must be accessible inside the sandbox.
            # It lives in job_dir which is under data_dir (hidden by tmpfs).
            # We need to bind-mount it. Use input_dir since it's writable.
            # Actually, job_dir itself is not mounted — only input_dir is.
            # So we place the inner script in input_dir for visibility.
            inner_in_input = input_dir_resolved / INNER_SCRIPT_NAME
            inner_in_input.write_text(inner_content, encoding="utf-8")
            inner_in_input.chmod(
                stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH
            )
            inner_script_sandbox_path = str(inner_in_input)

            logger.debug(
                "inner_wrapper_created",
                job_id=job_id,
                inner_path=str(inner_in_input),
            )
        except OSError as exc:
            raise SandboxCreationError(
                f"Failed to write inner wrapper script: {exc}"
            ) from exc

    # ---- 5. Generate outer wrapper script ----
    try:
        script_content = _generate_wrapper_script(
            bwrap_path=resolved_bwrap,
            bwrap_args=bwrap_args,
            real_cli_path=real_cli,
            job_id=job_id,
            input_dir=str(input_dir_resolved),
            network_isolated=network_isolated,
            inner_script_path=inner_script_sandbox_path,
        )
    except Exception as exc:
        raise SandboxCreationError(
            f"Failed to generate wrapper script: {exc}"
        ) from exc

    # ---- 6. Write outer wrapper script to disk ----
    wrapper_path = job_dir_resolved / WRAPPER_SCRIPT_NAME

    try:
        wrapper_path.write_text(script_content, encoding="utf-8")

        # Make executable (owner read+write+execute, group/other read+execute)
        wrapper_path.chmod(
            stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH
        )
    except OSError as exc:
        raise SandboxCreationError(
            f"Failed to write wrapper script to {wrapper_path}: {exc}"
        ) from exc

    logger.info(
        "sandbox_wrapper_created",
        job_id=job_id,
        wrapper_path=str(wrapper_path),
        real_cli=real_cli,
        bwrap=resolved_bwrap,
        user_home=user_home,
        network_isolated=network_isolated,
        seccomp_enabled=seccomp_exec_prefix is not None,
    )

    logger.debug(
        "sandbox_wrapper_script_content",
        job_id=job_id,
        script_length=len(script_content),
        script_preview=script_content[:500],
    )

    return wrapper_path


def cleanup_sandbox_wrapper(wrapper_path: Path) -> None:
    """
    Remove a sandbox wrapper script.

    This is optional — wrapper scripts are also cleaned up when the
    job directory is deleted by the normal cleanup process.

    Args:
        wrapper_path: Path to the wrapper script to remove.
    """
    try:
        if wrapper_path.exists():
            wrapper_path.unlink()
            logger.debug(
                "sandbox_wrapper_cleaned",
                path=str(wrapper_path),
            )
    except OSError as exc:
        # Non-fatal: the job cleanup will eventually remove this.
        logger.warning(
            "sandbox_wrapper_cleanup_failed",
            path=str(wrapper_path),
            error=str(exc),
        )
