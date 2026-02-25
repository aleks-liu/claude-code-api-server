"""
Subagent definition management for Claude Code API Server.

Manages subagent definition files (markdown with YAML frontmatter) and
their metadata.  Claude Code discovers agents natively from
``~/.claude/agents/`` — the markdown ``.md`` files in
``/data/agents/prompts/`` are the source of truth.  This module provides
CRUD operations for those files and a lightweight metadata registry
(``agents.json``) for the admin API.

Storage layout::

    /data/agents/
    ├── agents.json        ← Management metadata (added_at, description)
    ├── prompts/           ← Agent definition .md files
    │   ├── vuln-scanner.md
    │   └── code-reviewer.md
    └── .lock              ← File lock for concurrent safety

Agent file format (Claude Code native)::

    ---
    name: vuln-scanner
    description: Specialized vulnerability scanner
    tools: Read, Grep, Glob, Bash, Write, Edit
    model: sonnet
    ---

    Full system prompt body in Markdown…
"""

import fcntl
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .logging_config import get_logger
from .models import MAX_NAME_LENGTH, AgentEntry, utcnow

logger = get_logger(__name__)


# =============================================================================
# Constants
# =============================================================================

# Agent name must start with a letter, then letters + digits + hyphens.
# Kept strict for safe filesystem paths and Claude Code compatibility.
# Case-insensitive uniqueness is enforced at the manager level.
_AGENT_NAME_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9-]*$")

# Maximum size of a single agent definition file (500 KB).
MAX_AGENT_FILE_SIZE = 512_000


# =============================================================================
# Exceptions
# =============================================================================


class AgentError(Exception):
    """Base exception for agent management errors."""

    pass


class AgentExistsError(AgentError):
    """Raised when trying to add an agent with a name that already exists."""

    pass


class AgentNotFoundError(AgentError):
    """Raised when a referenced agent does not exist."""

    pass


class AgentValidationError(AgentError):
    """Raised when an agent file fails validation."""

    pass


# =============================================================================
# YAML Frontmatter Parsing
# =============================================================================


def parse_agent_file(content: str) -> tuple[dict[str, Any], str]:
    """
    Parse a markdown file with YAML frontmatter.

    The expected format is::

        ---
        key: value
        ---
        body content…

    Args:
        content: Raw file content (UTF-8 string).

    Returns:
        Tuple of (frontmatter_dict, body_text).  If no frontmatter is
        present, returns an empty dict and the full content as body.

    Raises:
        AgentValidationError: If the frontmatter is present but malformed.
    """
    stripped = content.strip()
    if not stripped.startswith("---"):
        return {}, stripped

    lines = stripped.split("\n")
    # lines[0] is the opening '---'
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break

    if end_idx is None:
        raise AgentValidationError(
            "YAML frontmatter opened with '---' but no closing '---' found"
        )

    yaml_text = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1 :]).strip()

    try:
        frontmatter = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise AgentValidationError(
            f"Invalid YAML in frontmatter: {exc}"
        ) from exc

    if frontmatter is None:
        frontmatter = {}
    if not isinstance(frontmatter, dict):
        raise AgentValidationError(
            "YAML frontmatter must be a mapping (key: value pairs), "
            f"got {type(frontmatter).__name__}"
        )

    return frontmatter, body


def compose_agent_file(frontmatter: dict[str, Any], body: str) -> str:
    """
    Compose a markdown file from YAML frontmatter and body text.

    Args:
        frontmatter: Dictionary of frontmatter fields.
        body: Markdown body text.

    Returns:
        Complete file content with ``---`` delimiters.
    """
    yaml_text = yaml.dump(
        frontmatter,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )
    return f"---\n{yaml_text}---\n\n{body}\n"


# =============================================================================
# Validation Helpers
# =============================================================================


def validate_agent_name(name: str) -> None:
    """
    Validate an agent name.

    Rules:
    - Must start with a lowercase letter.
    - May contain lowercase letters, digits, and hyphens.
    - Maximum length: 64 characters.

    Args:
        name: Proposed agent name.

    Raises:
        AgentValidationError: If the name is invalid.
    """
    if not name:
        raise AgentValidationError("Agent name cannot be empty")
    if len(name) > MAX_NAME_LENGTH:
        raise AgentValidationError(
            f"Agent name too long ({len(name)} chars, max {MAX_NAME_LENGTH})"
        )
    if not _AGENT_NAME_PATTERN.match(name):
        raise AgentValidationError(
            f"Agent name '{name}' is invalid.  Must start with a letter "
            "and contain only letters, digits, and hyphens."
        )


def validate_agent_content(content: str, name: str) -> tuple[dict[str, Any], str]:
    """
    Validate the full content of an agent definition file.

    Checks:
    - Content size within limits.
    - Valid YAML frontmatter with required fields (name, description).
    - Non-empty prompt body.
    - Frontmatter ``name`` matches the registered name.

    Args:
        content: Raw file content.
        name: Expected agent name (from CLI argument).

    Returns:
        Tuple of (validated_frontmatter, body).

    Raises:
        AgentValidationError: If validation fails.
    """
    if len(content.encode("utf-8")) > MAX_AGENT_FILE_SIZE:
        raise AgentValidationError(
            f"Agent file exceeds maximum size "
            f"({len(content.encode('utf-8'))} bytes, max {MAX_AGENT_FILE_SIZE})"
        )

    frontmatter, body = parse_agent_file(content)

    if not frontmatter:
        raise AgentValidationError(
            "Agent file must have YAML frontmatter with at least "
            "'name' and 'description' fields"
        )

    fm_name = frontmatter.get("name")
    if not fm_name:
        raise AgentValidationError(
            "YAML frontmatter missing required field: 'name'"
        )
    if not isinstance(fm_name, str):
        raise AgentValidationError(
            f"Frontmatter 'name' must be a string, got {type(fm_name).__name__}"
        )

    # The name in frontmatter must match the registered name.
    if fm_name != name:
        raise AgentValidationError(
            f"Frontmatter name '{fm_name}' does not match "
            f"the agent name '{name}'.  They must be identical."
        )

    fm_description = frontmatter.get("description")
    if not fm_description:
        raise AgentValidationError(
            "YAML frontmatter missing required field: 'description'"
        )

    if not body:
        raise AgentValidationError(
            "Agent definition body (system prompt) cannot be empty"
        )

    return frontmatter, body


# =============================================================================
# SDK-Oriented Loader
# =============================================================================


def load_agent_definitions(prompts_dir: Path) -> list[dict[str, Any]]:
    """
    Load all agent definitions from ``.md`` files for SDK consumption.

    Reads every Markdown file in *prompts_dir*, parses the YAML
    frontmatter and body, and returns structured data suitable for
    creating ``AgentDefinition`` objects (from ``claude_agent_sdk``).

    This is a standalone function — it does **not** require an
    :class:`AgentManager` instance and therefore avoids loading
    ``agents.json`` metadata, making it lightweight enough for
    per-job invocation.

    Args:
        prompts_dir: Directory containing agent ``.md`` files
            (e.g. ``/data/agents/prompts/``).

    Returns:
        List of dicts, each with keys:

        * ``name`` (str) — agent name from frontmatter.
        * ``description`` (str) — agent description from frontmatter.
        * ``prompt`` (str) — body text (the system prompt).
        * ``tools`` (list[str] | None) — parsed tool list, or *None*.
        * ``model`` (str | None) — model identifier, or *None*.

        Only agents with valid frontmatter **and** a non-empty body
        are included.  Invalid files are skipped with a warning log.
    """
    if not prompts_dir.is_dir():
        return []

    definitions: list[dict[str, Any]] = []

    for md_path in sorted(prompts_dir.glob("*.md")):
        try:
            content = md_path.read_text(encoding="utf-8")
            frontmatter, body = parse_agent_file(content)

            if not frontmatter:
                logger.warning(
                    "agent_def_skipped_no_frontmatter",
                    path=str(md_path),
                    message="Skipping agent file: no YAML frontmatter found.",
                )
                continue

            name = frontmatter.get("name")
            if not name or not isinstance(name, str):
                logger.warning(
                    "agent_def_skipped_no_name",
                    path=str(md_path),
                    message="Skipping agent file: 'name' field missing or not a string.",
                )
                continue

            if not body:
                logger.warning(
                    "agent_def_skipped_empty_body",
                    path=str(md_path),
                    name=name,
                    message="Skipping agent file: prompt body is empty.",
                )
                continue

            description = frontmatter.get("description", "")
            if not isinstance(description, str):
                description = str(description)

            # ---- Parse ``tools`` field ----
            # Accepts a comma-separated string ("Read, Grep, Bash") or
            # a YAML list (["Read", "Grep", "Bash"]).
            tools_raw = frontmatter.get("tools")
            tools: list[str] | None = None
            if tools_raw is not None:
                if isinstance(tools_raw, str):
                    tools = [t.strip() for t in tools_raw.split(",") if t.strip()]
                elif isinstance(tools_raw, list):
                    tools = [str(t).strip() for t in tools_raw if str(t).strip()]
                else:
                    logger.warning(
                        "agent_def_tools_invalid_type",
                        path=str(md_path),
                        name=name,
                        tools_type=type(tools_raw).__name__,
                        message="Ignoring 'tools' field: expected string or list.",
                    )

            # ---- Parse ``model`` field ----
            model_raw = frontmatter.get("model")
            model: str | None = None
            if model_raw is not None:
                model = str(model_raw).strip() or None

            definitions.append({
                "name": name,
                "description": description,
                "prompt": body,
                "tools": tools,
                "model": model,
            })

            logger.debug(
                "agent_def_loaded",
                name=name,
                prompt_length=len(body),
                has_tools=tools is not None,
                model=model,
            )

        except AgentValidationError as exc:
            logger.warning(
                "agent_def_skipped_validation",
                path=str(md_path),
                error=str(exc),
                message="Skipping agent file: validation error.",
            )
        except Exception as exc:
            logger.warning(
                "agent_def_skipped_error",
                path=str(md_path),
                error=str(exc),
                error_type=type(exc).__name__,
                message="Skipping agent file: unexpected error.",
            )

    if definitions:
        logger.info(
            "agent_definitions_loaded",
            count=len(definitions),
            agents=[d["name"] for d in definitions],
        )

    return definitions


# =============================================================================
# Agent Manager
# =============================================================================


class AgentManager:
    """
    Manages subagent definition files and metadata.

    Provides CRUD operations on ``/data/agents/prompts/*.md`` and the
    ``/data/agents/agents.json`` metadata registry.

    Thread/process safety: file-based locking via ``/data/agents/.lock``
    prevents concurrent modification from multiple concurrent requests.
    """

    def __init__(self, agents_dir: Path, plugin_agents_dir: Path | None = None):
        """
        Initialize the agent manager.

        Args:
            agents_dir: Path to the agents directory (e.g., ``/data/agents/``).
            plugin_agents_dir: Path to the plugin agents directory
                (e.g., ``/data/skills-plugin/agents/``).  When provided,
                agent ``.md`` files are written here so that Claude Code
                discovers them via ``--plugin-dir`` (filesystem-based
                discovery).  This avoids the ``--agents`` CLI argument
                size limit.
        """
        self._agents_dir = agents_dir
        self._prompts_dir = agents_dir / "prompts"
        self._plugin_agents_dir = plugin_agents_dir
        self._metadata_file = agents_dir / "agents.json"
        self._lock_file = agents_dir / ".lock"
        self._metadata: dict[str, AgentEntry] = {}
        self._load_metadata()

    # =========================================================================
    # Internal — Metadata Persistence
    # =========================================================================

    def _load_metadata(self) -> None:
        """Load agent metadata from agents.json."""
        if not self._metadata_file.exists():
            logger.debug(
                "agent_metadata_file_not_found", path=str(self._metadata_file)
            )
            self._metadata = {}
            return

        try:
            content = self._metadata_file.read_text(encoding="utf-8")
            if not content.strip():
                self._metadata = {}
                return

            data = json.loads(content)
            raw_meta = data.get("_metadata", {})

            self._metadata = {}
            for name, meta in raw_meta.items():
                added_at_str = meta.get("added_at")
                added_at = (
                    datetime.fromisoformat(added_at_str)
                    if added_at_str
                    else utcnow()
                )
                self._metadata[name] = AgentEntry(
                    name=name,
                    description=meta.get("description", ""),
                    added_at=added_at,
                    prompt_size_bytes=meta.get("prompt_size_bytes", 0),
                )

            logger.info(
                "agent_metadata_loaded",
                count=len(self._metadata),
                path=str(self._metadata_file),
            )

        except json.JSONDecodeError as exc:
            logger.error(
                "agent_metadata_parse_error",
                error=str(exc),
                path=str(self._metadata_file),
            )
            raise AgentError(
                f"Failed to parse agent metadata at {self._metadata_file}: {exc}"
            ) from exc
        except Exception as exc:
            logger.error(
                "agent_metadata_load_error",
                error=str(exc),
                path=str(self._metadata_file),
            )
            raise AgentError(
                f"Failed to load agent metadata: {exc}"
            ) from exc

    def _save_metadata(self) -> None:
        """
        Save agent metadata to agents.json.

        Uses file-based locking for concurrent protection.
        """
        self._agents_dir.mkdir(parents=True, exist_ok=True)

        raw_meta: dict[str, dict[str, Any]] = {}
        for name, entry in self._metadata.items():
            raw_meta[name] = {
                "added_at": entry.added_at.isoformat(),
                "description": entry.description,
                "prompt_size_bytes": entry.prompt_size_bytes,
            }

        data = {"_metadata": raw_meta}

        try:
            self._lock_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._lock_file, "w") as lock_fd:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                try:
                    self._metadata_file.write_text(
                        json.dumps(data, indent=2, default=str),
                        encoding="utf-8",
                    )
                finally:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)

            logger.info(
                "agent_metadata_saved",
                count=len(self._metadata),
                path=str(self._metadata_file),
            )

        except Exception as exc:
            logger.error(
                "agent_metadata_save_error",
                error=str(exc),
                path=str(self._metadata_file),
            )
            raise AgentError(
                f"Failed to save agent metadata: {exc}"
            ) from exc

    # =========================================================================
    # Internal — Prompt File I/O
    # =========================================================================

    def _prompt_path(self, name: str) -> Path:
        """Return the path to an agent's prompt file."""
        return self._prompts_dir / f"{name}.md"

    def _plugin_prompt_path(self, name: str) -> Path | None:
        """Return the path in the plugin agents directory, or None."""
        if self._plugin_agents_dir is None:
            return None
        return self._plugin_agents_dir / f"{name}.md"

    def _write_prompt(self, name: str, content: str) -> int:
        """
        Write agent definition content to the prompts directory.

        Writes to a temporary file first, then atomically renames
        to prevent partial writes.

        Args:
            name: Agent name (determines filename).
            content: Full file content (frontmatter + body).

        Returns:
            Size of the written file in bytes.

        Raises:
            AgentError: If the file cannot be written.
        """
        self._prompts_dir.mkdir(parents=True, exist_ok=True)
        target = self._prompt_path(name)
        tmp_path = target.with_suffix(".md.tmp")

        try:
            encoded = content.encode("utf-8")
            tmp_path.write_bytes(encoded)
            tmp_path.rename(target)

            logger.info(
                "agent_prompt_written",
                agent_name=name,
                path=str(target),
                size_bytes=len(encoded),
            )

            # Also write to plugin agents directory for --plugin-dir discovery
            plugin_path = self._plugin_prompt_path(name)
            if plugin_path is not None:
                plugin_path.parent.mkdir(parents=True, exist_ok=True)
                plugin_path.write_bytes(encoded)
                logger.debug(
                    "agent_prompt_synced_to_plugin",
                    agent_name=name,
                    plugin_path=str(plugin_path),
                )

            return len(encoded)

        except Exception as exc:
            # Clean up temp file on failure
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise AgentError(
                f"Failed to write agent prompt for '{name}': {exc}"
            ) from exc

    def _read_prompt(self, name: str) -> str | None:
        """
        Read an agent's prompt file content.

        Returns:
            File content as string, or None if the file does not exist.
        """
        path = self._prompt_path(name)
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except Exception as exc:
            logger.error(
                "agent_prompt_read_error",
                agent_name=name,
                path=str(path),
                error=str(exc),
            )
            return None

    def _delete_prompt(self, name: str) -> bool:
        """
        Delete an agent's prompt file.

        Returns:
            True if the file existed and was deleted, False otherwise.
        """
        path = self._prompt_path(name)
        if not path.exists():
            return False
        try:
            path.unlink()
            logger.info(
                "agent_prompt_deleted",
                agent_name=name,
                path=str(path),
            )

            # Also remove from plugin agents directory
            plugin_path = self._plugin_prompt_path(name)
            if plugin_path is not None and plugin_path.exists():
                plugin_path.unlink()
                logger.debug(
                    "agent_prompt_deleted_from_plugin",
                    agent_name=name,
                    plugin_path=str(plugin_path),
                )

            return True
        except OSError as exc:
            logger.error(
                "agent_prompt_delete_error",
                agent_name=name,
                path=str(path),
                error=str(exc),
            )
            raise AgentError(
                f"Failed to delete agent prompt for '{name}': {exc}"
            ) from exc

    # =========================================================================
    # Public API
    # =========================================================================

    def add_agent(self, name: str, content: str, description: str = "") -> AgentEntry:
        """
        Add a new subagent definition.

        The ``content`` must be a complete markdown file with YAML
        frontmatter containing at least ``name`` and ``description``.

        Args:
            name: Unique agent name.
            content: Full agent definition file content.
            description: Optional description override for metadata
                (if empty, extracted from frontmatter).

        Returns:
            The created AgentEntry metadata.

        Raises:
            AgentExistsError: If an agent with the same name exists.
            AgentValidationError: If the content fails validation.
        """
        validate_agent_name(name)

        if name in self._metadata or self._prompt_path(name).exists():
            raise AgentExistsError(
                f"Agent '{name}' already exists."
            )

        # Case-insensitive uniqueness: prevent "MyAgent" when "myagent" exists
        name_lower = name.lower()
        for existing in self._metadata:
            if existing.lower() == name_lower and existing != name:
                raise AgentExistsError(
                    f"Agent '{name}' conflicts with existing agent "
                    f"'{existing}' (names differ only in case)."
                )

        frontmatter, _body = validate_agent_content(content, name)

        # Write the prompt file
        size_bytes = self._write_prompt(name, content)

        # Use frontmatter description if no explicit override provided
        final_description = description or frontmatter.get("description", "")

        # Update metadata
        entry = AgentEntry(
            name=name,
            description=final_description,
            added_at=utcnow(),
            prompt_size_bytes=size_bytes,
        )
        self._metadata[name] = entry
        self._save_metadata()

        logger.info(
            "agent_added",
            agent_name=name,
            description=final_description,
            prompt_size_bytes=size_bytes,
        )

        return entry

    def remove_agent(self, name: str) -> AgentEntry | None:
        """
        Remove a subagent definition.

        Deletes the prompt file and removes metadata from agents.json.

        Args:
            name: Agent name to remove.

        Returns:
            The removed AgentEntry metadata, or None if not found.
        """
        entry = self._metadata.pop(name, None)
        file_deleted = self._delete_prompt(name)

        if entry is None and not file_deleted:
            return None

        self._save_metadata()

        logger.info(
            "agent_removed",
            agent_name=name,
            had_metadata=entry is not None,
            had_file=file_deleted,
        )

        return entry

    def get_agent(self, name: str) -> tuple[AgentEntry | None, str | None]:
        """
        Get a specific agent's metadata and file content.

        Args:
            name: Agent name.

        Returns:
            Tuple of (AgentEntry or None, file_content or None).
        """
        entry = self._metadata.get(name)
        content = self._read_prompt(name)
        return entry, content

    def list_agents(self) -> list[AgentEntry]:
        """
        List all agents with metadata.

        Cross-references the metadata registry with actual prompt files
        on disk.  Reports discrepancies via logging.

        Returns:
            List of AgentEntry objects for agents that have both metadata
            and a prompt file on disk.
        """
        # Scan prompt files on disk
        disk_agents: set[str] = set()
        if self._prompts_dir.is_dir():
            for path in self._prompts_dir.glob("*.md"):
                disk_agents.add(path.stem)

        # Report orphaned files (on disk but not in metadata)
        orphaned = disk_agents - set(self._metadata.keys())
        if orphaned:
            logger.warning(
                "agent_orphaned_files",
                orphaned=sorted(orphaned),
                message=(
                    "Agent prompt files found on disk without metadata entries. "
                    "These agents are still usable by Claude Code but are not "
                    "tracked in agents.json."
                ),
            )

        # Report missing files (in metadata but not on disk)
        missing = set(self._metadata.keys()) - disk_agents
        if missing:
            logger.warning(
                "agent_missing_files",
                missing=sorted(missing),
                message=(
                    "Agent metadata entries found without corresponding "
                    "prompt files.  These agents will NOT be available to "
                    "Claude Code."
                ),
            )

        # Return metadata for agents that exist on disk
        result: list[AgentEntry] = []
        for name, entry in sorted(self._metadata.items()):
            if name in disk_agents:
                result.append(entry)

        # Also include orphaned agents (on disk, not in metadata)
        for name in sorted(orphaned):
            content = self._read_prompt(name)
            if content is not None:
                try:
                    frontmatter, _ = parse_agent_file(content)
                    description = frontmatter.get("description", "")
                except AgentValidationError:
                    description = "(invalid frontmatter)"
                result.append(
                    AgentEntry(
                        name=name,
                        description=f"[untracked] {description}",
                        prompt_size_bytes=len(content.encode("utf-8")),
                    )
                )

        return result

    def update_agent(
        self,
        name: str,
        content: str | None = None,
        description: str | None = None,
    ) -> AgentEntry:
        """
        Update an existing agent's definition or metadata.

        If ``content`` is provided, the prompt file is replaced (validated
        first).  If only ``description`` is provided, only the metadata
        is updated.

        Args:
            name: Agent name to update.
            content: New file content (optional — full replacement).
            description: New description for metadata (optional).

        Returns:
            Updated AgentEntry.

        Raises:
            AgentNotFoundError: If the agent does not exist.
            AgentValidationError: If new content fails validation.
        """
        if name not in self._metadata and not self._prompt_path(name).exists():
            raise AgentNotFoundError(
                f"Agent '{name}' not found."
            )

        entry = self._metadata.get(name)

        if content is not None:
            frontmatter, _body = validate_agent_content(content, name)
            size_bytes = self._write_prompt(name, content)

            if entry is None:
                entry = AgentEntry(name=name, added_at=utcnow())

            entry.prompt_size_bytes = size_bytes

            # Update description from frontmatter if not explicitly provided
            if description is None:
                entry.description = frontmatter.get(
                    "description", entry.description
                )

        if entry is None:
            entry = AgentEntry(name=name, added_at=utcnow())

        if description is not None:
            entry.description = description

        self._metadata[name] = entry
        self._save_metadata()

        logger.info(
            "agent_updated",
            agent_name=name,
            content_updated=content is not None,
            description_updated=description is not None,
        )

        return entry

    @property
    def prompts_dir(self) -> Path:
        """Return the path to the prompts directory."""
        return self._prompts_dir

    def has_agents(self) -> bool:
        """Check whether any agents are configured (files on disk)."""
        if not self._prompts_dir.is_dir():
            return False
        return any(self._prompts_dir.glob("*.md"))

    def get_agent_definitions(self) -> list[dict[str, Any]]:
        """
        Load all agent definitions from prompt files for SDK consumption.

        Thin wrapper around the module-level :func:`load_agent_definitions`
        using this manager's prompts directory.

        Returns:
            List of agent definition dicts (see :func:`load_agent_definitions`).
        """
        return load_agent_definitions(self._prompts_dir)

    def sync_to_plugin_dir(self) -> int:
        """
        Sync all agent .md files from prompts/ to the plugin agents/ dir.

        This handles migration: existing agents written before the plugin
        delivery mechanism was introduced are copied to the plugin dir
        so that Claude Code discovers them via ``--plugin-dir``.

        Returns:
            Number of files synced.
        """
        if self._plugin_agents_dir is None:
            return 0
        if not self._prompts_dir.is_dir():
            return 0

        self._plugin_agents_dir.mkdir(parents=True, exist_ok=True)
        count = 0
        for md_path in self._prompts_dir.glob("*.md"):
            target = self._plugin_agents_dir / md_path.name
            if not target.exists() or target.read_bytes() != md_path.read_bytes():
                target.write_bytes(md_path.read_bytes())
                count += 1

        if count:
            logger.info(
                "agents_synced_to_plugin_dir",
                count=count,
                plugin_dir=str(self._plugin_agents_dir),
            )
        return count

    def reload(self) -> None:
        """Reload metadata from disk."""
        self._load_metadata()
