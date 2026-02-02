"""
Security module for Claude Code API Server.

Provides the can_use_tool callback that enforces security profile
policies: denied tools and MCP server access restrictions.

Filesystem isolation is handled by bwrap at the OS level.
Network isolation will be handled by the per-job proxy (Phase 2).
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from .logging_config import get_logger
from .models import SecurityDecision, SecurityProfile

try:
    from claude_agent_sdk.types import (
        PermissionResultAllow,
        PermissionResultDeny,
        ToolPermissionContext,
    )
except ImportError:
    @dataclass
    class PermissionResultAllow:  # type: ignore[no-redef]
        behavior: str = "allow"
        updated_input: dict[str, Any] | None = None
        updated_permissions: list | None = None

    @dataclass
    class PermissionResultDeny:  # type: ignore[no-redef]
        behavior: str = "deny"
        message: str = ""
        interrupt: bool = False

    @dataclass
    class ToolPermissionContext:  # type: ignore[no-redef]
        signal: Any | None = None
        suggestions: list = field(default_factory=list)

logger = get_logger(__name__)


def create_permission_handler(
    job_id: str,
    profile: SecurityProfile,
) -> Callable[
    [str, dict[str, Any], ToolPermissionContext],
    Awaitable[PermissionResultAllow | PermissionResultDeny],
]:
    """
    Create a can_use_tool callback driven by a security profile.

    Enforces:
    - denied_tools: tools listed in the profile are blocked
    - allowed_mcp_servers: MCP tools from unlisted servers are blocked

    Filesystem security is provided by bwrap (OS-level).
    Network security will be provided by the per-job proxy (Phase 2).

    Args:
        job_id: Job identifier for logging.
        profile: The resolved security profile for this job.

    Returns:
        An async callback compatible with ClaudeAgentOptions.can_use_tool
    """
    denied_tools = set(profile.denied_tools)
    allowed_mcp = profile.allowed_mcp_servers  # None = all allowed

    async def can_use_tool(
        tool_name: str,
        input_data: dict[str, Any],
        context: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        # Check denied tools
        if tool_name in denied_tools:
            logger.warning(
                "tool_denied_by_profile",
                job_id=job_id,
                tool=tool_name,
                profile=profile.name,
                decision=SecurityDecision.DENY.value,
            )
            return PermissionResultDeny(
                message=f"Tool '{tool_name}' denied by security profile '{profile.name}'",
            )

        # Check MCP server access
        if tool_name.startswith("mcp__") and allowed_mcp is not None:
            parts = tool_name.split("__")
            if len(parts) >= 2:
                server_name = parts[1]
                if server_name not in allowed_mcp:
                    logger.warning(
                        "mcp_server_denied_by_profile",
                        job_id=job_id,
                        tool=tool_name,
                        server=server_name,
                        profile=profile.name,
                        decision=SecurityDecision.DENY.value,
                    )
                    return PermissionResultDeny(
                        message=(
                            f"MCP server '{server_name}' not allowed by "
                            f"security profile '{profile.name}'"
                        ),
                    )

        logger.debug(
            "tool_allowed",
            job_id=job_id,
            tool=tool_name,
            decision=SecurityDecision.ALLOW.value,
        )
        return PermissionResultAllow(updated_input=input_data)

    return can_use_tool
