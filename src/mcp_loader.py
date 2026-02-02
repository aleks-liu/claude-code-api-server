"""
Runtime MCP configuration loading for Claude Code API Server.

Responsible for:
1. Reading MCP config via McpManager
2. Expanding ${ENV_VAR} references in env and headers values
3. Computing sandbox read-only bind paths for MCP package directories
4. Running health checks against configured MCP servers
5. Providing the expanded config to ClaudeRunner

This module is the bridge between static configuration (servers.json)
and runtime SDK integration. It does NOT modify the configuration —
that is the responsibility of McpManager and the admin API.
"""

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .logging_config import get_logger
from .mcp_manager import McpManager

logger = get_logger(__name__)


# =============================================================================
# Exceptions
# =============================================================================


class McpHealthCheckError(Exception):
    """Raised when MCP health checks fail and fail-closed is configured."""

    pass


# =============================================================================
# Environment Variable Expansion
# =============================================================================

# Matches ${VAR_NAME} or ${VAR_NAME:-default_value}
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}:]+)(?::-([^}]*))?\}")


def _expand_env_value(value: str) -> str:
    """
    Expand ${VAR} and ${VAR:-default} placeholders in a string value.

    If a referenced variable is not set and has no default, the raw
    placeholder is preserved. This causes the MCP server to receive
    the unexpanded placeholder, which will likely cause it to fail
    its health check — surfacing the misconfiguration early.

    Args:
        value: String potentially containing ${VAR} placeholders.

    Returns:
        String with placeholders expanded from os.environ.
    """
    def replacer(match: re.Match) -> str:
        var_name = match.group(1)
        default_value = match.group(2)  # None if no default specified

        env_value = os.environ.get(var_name)
        if env_value is not None:
            return env_value
        if default_value is not None:
            return default_value

        logger.warning(
            "mcp_env_var_not_set",
            var_name=var_name,
            message=(
                f"Environment variable ${{{var_name}}} is not set and has no "
                "default. The placeholder will be passed as-is to the MCP server."
            ),
        )
        return match.group(0)  # Return the raw ${VAR} placeholder

    return _ENV_VAR_PATTERN.sub(replacer, value)


def _expand_env_dict(d: dict[str, str]) -> dict[str, str]:
    """Expand environment variable placeholders in all values of a dict."""
    return {key: _expand_env_value(value) for key, value in d.items()}


# =============================================================================
# MCP Config Dataclass
# =============================================================================


@dataclass
class McpConfig:
    """
    Loaded and validated MCP server configuration, ready for the SDK.

    This is an immutable snapshot of the MCP config at load time.
    It contains expanded env values and computed sandbox paths.
    """

    servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Server name -> SDK-compatible config dict (with env vars expanded)."""

    allowed_tool_patterns: list[str] = field(default_factory=list)
    """Tool patterns like ['mcp__name__*', ...] for allowed_tools."""

    sandbox_ro_binds: list[Path] = field(default_factory=list)
    """Paths under /data/mcp/ to expose read-only in bwrap sandbox."""

    server_statuses: dict[str, str] = field(default_factory=dict)
    """Health check results: server name -> 'ok' | 'failed' | 'skipped'."""

    @property
    def has_servers(self) -> bool:
        """Check whether any MCP servers are configured."""
        return len(self.servers) > 0


# =============================================================================
# Loading
# =============================================================================


def load_mcp_config(manager: McpManager) -> McpConfig:
    """
    Load MCP configuration from the manager, expand env vars,
    and compute sandbox binds.

    This reads the config once and returns an immutable snapshot.
    The McpConfig is passed to ClaudeRunner at initialization.

    Args:
        manager: McpManager instance with loaded config.

    Returns:
        McpConfig ready for SDK integration.
    """
    if not manager.has_servers():
        logger.info("mcp_config_empty", message="No MCP servers configured")
        return McpConfig()

    # Get the raw SDK-compatible dict from the manager
    raw_servers = manager.get_mcp_servers_dict()

    # Expand environment variables in env and headers values
    expanded_servers: dict[str, dict[str, Any]] = {}
    for name, config in raw_servers.items():
        expanded = dict(config)
        if "env" in expanded and expanded["env"]:
            expanded["env"] = _expand_env_dict(expanded["env"])
        if "headers" in expanded and expanded["headers"]:
            expanded["headers"] = _expand_env_dict(expanded["headers"])
        expanded_servers[name] = expanded

    # Compute allowed tool patterns
    allowed_patterns = manager.get_allowed_tool_patterns()

    # Compute sandbox binds
    sandbox_binds = _compute_sandbox_ro_binds(manager)

    server_names = list(expanded_servers.keys())
    logger.info(
        "mcp_config_loaded",
        server_count=len(expanded_servers),
        servers=server_names,
        sandbox_bind_count=len(sandbox_binds),
    )

    return McpConfig(
        servers=expanded_servers,
        allowed_tool_patterns=allowed_patterns,
        sandbox_ro_binds=sandbox_binds,
    )


# =============================================================================
# Sandbox Bind Computation
# =============================================================================


def _compute_sandbox_ro_binds(manager: McpManager) -> list[Path]:
    """
    Determine which /data/mcp/ subdirectories need --ro-bind in bwrap.

    Checks for the existence of npm and pip package directories. Only
    existing directories are included — there's no point binding a
    non-existent path.

    Args:
        manager: McpManager instance to determine data directory.

    Returns:
        List of absolute paths that should be read-only bind-mounted.
    """
    mcp_dir = manager._file.parent  # /data/mcp/
    binds: list[Path] = []

    npm_dir = mcp_dir / "npm" / "node_modules"
    venv_dir = mcp_dir / "venv"

    if npm_dir.is_dir():
        binds.append(npm_dir.resolve())
        logger.debug("mcp_sandbox_bind_npm", path=str(npm_dir))

    if venv_dir.is_dir():
        binds.append(venv_dir.resolve())
        logger.debug("mcp_sandbox_bind_venv", path=str(venv_dir))

    return binds


# =============================================================================
# Health Checking
# =============================================================================


def check_mcp_server(
    name: str,
    config: dict[str, Any],
    timeout: int = 15,
) -> tuple[bool, str]:
    """
    Health-check a single MCP server by sending a JSON-RPC initialize request.

    For stdio servers: spawns the server process, sends initialize via stdin,
    reads the response from stdout, then terminates the process.

    For http/sse servers: skipped (assumed reachable at runtime).

    Args:
        name: Server name (for logging).
        config: SDK-compatible server config dict.
        timeout: Timeout in seconds.

    Returns:
        Tuple of (is_healthy, detail_message).
    """
    server_type = config.get("type", "stdio")

    if server_type in ("http", "sse"):
        logger.debug(
            "mcp_health_check_skipped",
            server_name=name,
            server_type=server_type,
            reason="HTTP/SSE servers are assumed reachable",
        )
        return True, "skipped (http/sse)"

    # Build the command to spawn the MCP server
    command = config.get("command")
    args = config.get("args", [])
    env_overrides = config.get("env", {})

    if not command:
        return False, "no command specified"

    cmd = [command] + args

    # Build environment for the subprocess
    proc_env = dict(os.environ)
    if env_overrides:
        proc_env.update(env_overrides)

    # JSON-RPC initialize request
    initialize_request = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {
                "name": "claude-code-api-server-healthcheck",
                "version": "0.1.0",
            },
        },
    })

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=proc_env,
            text=True,
        )
    except FileNotFoundError:
        detail = f"command not found: {command}"
        logger.warning(
            "mcp_health_check_command_not_found",
            server_name=name,
            command=command,
        )
        return False, detail
    except OSError as e:
        detail = f"failed to spawn: {e}"
        logger.warning(
            "mcp_health_check_spawn_error",
            server_name=name,
            error=str(e),
        )
        return False, detail

    try:
        # Send the initialize request followed by a newline (JSON-RPC over stdio
        # uses newline-delimited JSON).
        stdout_data, stderr_data = proc.communicate(
            input=initialize_request + "\n",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        detail = f"timed out after {timeout}s"
        logger.warning(
            "mcp_health_check_timeout",
            server_name=name,
            timeout=timeout,
        )
        return False, detail
    except Exception as e:
        proc.kill()
        proc.wait()
        detail = f"communication error: {e}"
        logger.warning(
            "mcp_health_check_error",
            server_name=name,
            error=str(e),
        )
        return False, detail

    # Parse the response. MCP servers may output multiple lines; we look for
    # a line containing a valid JSON-RPC response with a "result" key.
    for line in stdout_data.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            response = json.loads(line)
            if isinstance(response, dict) and "result" in response:
                protocol_version = (
                    response.get("result", {})
                    .get("protocolVersion", "unknown")
                )
                logger.info(
                    "mcp_health_check_passed",
                    server_name=name,
                    protocol_version=protocol_version,
                )
                return True, f"ok (protocol v{protocol_version})"
        except json.JSONDecodeError:
            continue

    # No valid response found
    stderr_preview = (stderr_data or "").strip()[:200]
    stdout_preview = (stdout_data or "").strip()[:200]
    detail = (
        f"no valid JSON-RPC response. "
        f"stdout: {stdout_preview!r}, stderr: {stderr_preview!r}"
    )
    logger.warning(
        "mcp_health_check_invalid_response",
        server_name=name,
        stdout_preview=stdout_preview,
        stderr_preview=stderr_preview,
    )
    return False, detail


def check_all_mcp_servers(
    mcp_config: McpConfig,
    timeout: int = 15,
) -> dict[str, bool]:
    """
    Health-check all configured MCP servers.

    Args:
        mcp_config: Loaded MCP configuration.
        timeout: Timeout per server in seconds.

    Returns:
        Dict mapping server name to health status (True = healthy).
    """
    results: dict[str, bool] = {}

    for name, config in mcp_config.servers.items():
        is_healthy, detail = check_mcp_server(name, config, timeout)
        results[name] = is_healthy

        if is_healthy:
            if "skipped" in detail:
                mcp_config.server_statuses[name] = "skipped"
            else:
                mcp_config.server_statuses[name] = "ok"
        else:
            mcp_config.server_statuses[name] = "failed"
            logger.warning(
                "mcp_health_check_failed",
                server_name=name,
                detail=detail,
            )

    healthy_count = sum(1 for v in results.values() if v)
    total_count = len(results)
    logger.info(
        "mcp_health_check_summary",
        healthy=healthy_count,
        total=total_count,
    )

    return results
