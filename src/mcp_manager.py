"""
MCP server configuration management for Claude Code API Server.

Manages MCP server registration, configuration persistence, and
SDK-compatible config generation. Analogous to auth.py for client
credential management.

Storage format (/data/mcp/servers.json):

    {
        "mcpServers": {
            "server-name": {
                "command": "node",
                "args": ["/data/mcp/npm/node_modules/..."],
                "env": {}
            }
        },
        "_metadata": {
            "server-name": {
                "added_at": "2026-01-26T10:30:00Z",
                "description": "...",
                "package_manager": "npm",
                "package": "@scope/package-name"
            }
        }
    }

The ``mcpServers`` section uses the standard MCP config format compatible
with Claude Desktop, Claude Code CLI, and the Agent SDK. The ``_metadata``
section stores provenance information the SDK ignores.
"""

import json
import fcntl
from datetime import datetime
from pathlib import Path
from typing import Any

from .logging_config import get_logger
from .models import McpServerEntry, utcnow

logger = get_logger(__name__)


class McpConfigError(Exception):
    """Base exception for MCP configuration errors."""

    pass


class McpServerExistsError(McpConfigError):
    """Raised when trying to add a server with a name that already exists."""

    pass


class McpServerNotFoundError(McpConfigError):
    """Raised when a referenced server does not exist."""

    pass


class McpManager:
    """
    Manages MCP server configuration in persistent storage.

    Provides CRUD operations on /data/mcp/servers.json and generates
    SDK-compatible configuration dicts.

    Thread/process safety: file-based locking via /data/mcp/.lock
    prevents concurrent modification from multiple concurrent requests.
    """

    def __init__(self, mcp_servers_file: Path):
        """
        Initialize the MCP manager.

        Args:
            mcp_servers_file: Path to the servers.json configuration file.
        """
        self._file = mcp_servers_file
        self._lock_file = mcp_servers_file.parent / ".lock"
        self._servers: dict[str, McpServerEntry] = {}
        self._load()

    def _load(self) -> None:
        """Load MCP server configuration from disk."""
        if not self._file.exists():
            logger.debug("mcp_config_file_not_found", path=str(self._file))
            self._servers = {}
            return

        try:
            content = self._file.read_text(encoding="utf-8")
            if not content.strip():
                self._servers = {}
                return

            data = json.loads(content)
            mcp_servers = data.get("mcpServers", {})
            metadata = data.get("_metadata", {})

            self._servers = {}
            for name, config in mcp_servers.items():
                meta = metadata.get(name, {})
                entry = self._parse_entry(name, config, meta)
                self._servers[name] = entry

            logger.info(
                "mcp_config_loaded",
                count=len(self._servers),
                path=str(self._file),
            )

        except json.JSONDecodeError as e:
            logger.error(
                "mcp_config_parse_error",
                error=str(e),
                path=str(self._file),
            )
            raise McpConfigError(
                f"Failed to parse MCP config at {self._file}: {e}"
            ) from e
        except Exception as e:
            logger.error(
                "mcp_config_load_error",
                error=str(e),
                path=str(self._file),
            )
            raise McpConfigError(
                f"Failed to load MCP config: {e}"
            ) from e

    @staticmethod
    def _parse_entry(
        name: str, config: dict[str, Any], meta: dict[str, Any]
    ) -> McpServerEntry:
        """
        Parse a server entry from the on-disk format.

        Args:
            name: Server name.
            config: The mcpServers[name] dict.
            meta: The _metadata[name] dict (may be empty).

        Returns:
            McpServerEntry populated from both dicts.
        """
        server_type = config.get("type", "stdio")

        added_at_str = meta.get("added_at")
        added_at = (
            datetime.fromisoformat(added_at_str)
            if added_at_str
            else utcnow()
        )

        return McpServerEntry(
            name=name,
            type=server_type,
            command=config.get("command"),
            args=config.get("args", []),
            env=config.get("env", {}),
            url=config.get("url"),
            headers=config.get("headers", {}),
            description=meta.get("description", ""),
            added_at=added_at,
            package_manager=meta.get("package_manager"),
            package=meta.get("package"),
        )

    def _save(self) -> None:
        """
        Save MCP server configuration to disk.

        Uses file-based locking to prevent concurrent modification
        from multiple concurrent requests.
        """
        # Ensure parent directory exists
        self._file.parent.mkdir(parents=True, exist_ok=True)

        # Build the on-disk structure
        mcp_servers: dict[str, dict[str, Any]] = {}
        metadata: dict[str, dict[str, Any]] = {}

        for name, entry in self._servers.items():
            mcp_servers[name] = self._entry_to_config(entry)
            metadata[name] = self._entry_to_metadata(entry)

        data = {
            "mcpServers": mcp_servers,
            "_metadata": metadata,
        }

        try:
            # Acquire file lock for concurrent protection
            self._lock_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._lock_file, "w") as lock_fd:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                try:
                    self._file.write_text(
                        json.dumps(data, indent=2, default=str),
                        encoding="utf-8",
                    )
                finally:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)

            logger.info(
                "mcp_config_saved",
                count=len(self._servers),
                path=str(self._file),
            )

        except Exception as e:
            logger.error(
                "mcp_config_save_error",
                error=str(e),
                path=str(self._file),
            )
            raise McpConfigError(
                f"Failed to save MCP config: {e}"
            ) from e

    @staticmethod
    def _entry_to_config(entry: McpServerEntry) -> dict[str, Any]:
        """Convert a McpServerEntry to the SDK-compatible config dict."""
        if entry.type in ("http", "sse"):
            config: dict[str, Any] = {
                "type": entry.type,
                "url": entry.url,
            }
            if entry.headers:
                config["headers"] = entry.headers
            return config

        # stdio (default)
        config = {}
        if entry.type != "stdio":
            config["type"] = entry.type
        if entry.command:
            config["command"] = entry.command
        if entry.args:
            config["args"] = entry.args
        if entry.env:
            config["env"] = entry.env
        return config

    @staticmethod
    def _entry_to_metadata(entry: McpServerEntry) -> dict[str, Any]:
        """Convert a McpServerEntry to the metadata dict."""
        meta: dict[str, Any] = {
            "added_at": entry.added_at.isoformat(),
        }
        if entry.description:
            meta["description"] = entry.description
        if entry.package_manager:
            meta["package_manager"] = entry.package_manager
        if entry.package:
            meta["package"] = entry.package
        return meta

    # =========================================================================
    # Public API
    # =========================================================================

    def add_server(
        self,
        name: str,
        config: dict[str, Any],
        description: str = "",
        package_manager: str | None = None,
        package: str | None = None,
    ) -> McpServerEntry:
        """
        Add an MCP server configuration.

        Args:
            name: Unique server name (used in tool naming: mcp__<name>__*).
            config: Server configuration dict (command/args/env or type/url/headers).
            description: Human-readable description.
            package_manager: 'npm', 'pip', or None (manual).
            package: Package name for uninstall tracking.

        Returns:
            The created McpServerEntry.

        Raises:
            McpServerExistsError: If a server with the same name exists.
        """
        if name in self._servers:
            raise McpServerExistsError(
                f"MCP server '{name}' already exists."
            )

        entry = McpServerEntry(
            name=name,
            type=config.get("type", "stdio"),
            command=config.get("command"),
            args=config.get("args", []),
            env=config.get("env", {}),
            url=config.get("url"),
            headers=config.get("headers", {}),
            description=description,
            added_at=utcnow(),
            package_manager=package_manager,
            package=package,
        )

        self._servers[name] = entry
        self._save()

        logger.info(
            "mcp_server_added",
            server_name=name,
            server_type=entry.type,
            package_manager=package_manager,
            package=package,
        )

        return entry

    def remove_server(self, name: str) -> McpServerEntry | None:
        """
        Remove an MCP server configuration.

        Args:
            name: Server name to remove.

        Returns:
            The removed McpServerEntry, or None if not found.
        """
        entry = self._servers.pop(name, None)
        if entry is None:
            return None

        self._save()
        logger.info("mcp_server_removed", server_name=name)
        return entry

    def get_server(self, name: str) -> McpServerEntry | None:
        """
        Get a specific server by name.

        Args:
            name: Server name.

        Returns:
            McpServerEntry if found, None otherwise.
        """
        return self._servers.get(name)

    def list_servers(self) -> list[McpServerEntry]:
        """
        List all configured MCP servers.

        Returns:
            List of McpServerEntry objects.
        """
        return list(self._servers.values())

    def get_mcp_servers_dict(self) -> dict[str, dict[str, Any]]:
        """
        Return the mcpServers dict in standard SDK format.

        Strips metadata, returns only command/args/env/type/url/headers.
        This is the format accepted by ClaudeAgentOptions.mcp_servers.

        Returns:
            Dict mapping server name to SDK-compatible config.
        """
        return {
            name: self._entry_to_config(entry)
            for name, entry in self._servers.items()
        }

    def get_allowed_tool_patterns(self) -> list[str]:
        """
        Return wildcard tool patterns for all configured servers.

        Returns:
            List of patterns like ['mcp__name__*', ...].
        """
        return [f"mcp__{name}__*" for name in self._servers]

    def has_servers(self) -> bool:
        """Check whether any MCP servers are configured."""
        return len(self._servers) > 0

    def reload(self) -> None:
        """Reload configuration from disk."""
        self._load()


# =============================================================================
# Singleton Instance
# =============================================================================

_mcp_manager: McpManager | None = None


def get_mcp_manager(mcp_servers_file: Path | None = None) -> McpManager:
    """
    Get the singleton McpManager instance.

    Args:
        mcp_servers_file: Path to servers.json. If None, derived from settings.

    Returns:
        McpManager instance.
    """
    global _mcp_manager
    if _mcp_manager is None:
        if mcp_servers_file is None:
            from .config import get_settings
            mcp_servers_file = get_settings().mcp_servers_file
        _mcp_manager = McpManager(mcp_servers_file)
    return _mcp_manager


def reset_mcp_manager() -> None:
    """Reset the MCP manager singleton (for testing)."""
    global _mcp_manager
    _mcp_manager = None
