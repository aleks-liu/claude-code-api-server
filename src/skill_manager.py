"""
Skill definition management for Claude Code API Server.

Manages skill directories (containing SKILL.md + optional subdirs) and
their metadata.  Skills are directory-based extensions delivered to
Claude Code via the plugin mechanism (``--plugin-dir``).

Unlike subagents (which are flat ``.md`` files passed via
``options.agents``), skills are **filesystem-based**: each skill is a
directory containing a ``SKILL.md`` file (and optionally ``scripts/``,
``references/``, ``assets/`` subdirectories), stored inside a plugin
directory that Claude Code discovers at startup.

Storage layout::

    /data/skills-plugin/                 <- Plugin directory (--plugin-dir)
    +-- .claude-plugin/
    |   +-- plugin.json                  <- Auto-generated manifest
    +-- skills/
        +-- vuln-scanner/
        |   +-- SKILL.md
        |   +-- scripts/
        |   |   +-- analyze.py
        |   +-- references/
        |       +-- owasp-guide.md
        +-- code-reviewer/
            +-- SKILL.md

    /data/skills-meta/                   <- Management metadata (separate)
    +-- skills.json                      <- {name: {added_at, description, ...}}
    +-- .lock                            <- File lock for concurrent safety

Plugin manifest (plugin.json)::

    {
      "name": "ccas-plugin",
      "description": "Server-managed plugins for Claude Code API Server",
      "version": "1.0.0"
    }

All skills are namespaced as ``ccas-plugin:<skill-name>`` by Claude Code.
"""

import fcntl
import json
import os
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .logging_config import get_logger
from .models import MAX_NAME_LENGTH, SkillEntry, utcnow

logger = get_logger(__name__)


# =============================================================================
# Constants
# =============================================================================

PLUGIN_NAME = "ccas-plugin"
PLUGIN_DESCRIPTION = "Server-managed plugins for Claude Code API Server"
PLUGIN_VERSION = "1.0.0"

# Skill name must start with a letter, then letters + digits + hyphens.
# Case-insensitive uniqueness is enforced at the manager level.
_SKILL_NAME_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9-]*$")

# Maximum size of a single SKILL.md file (500 KB).
MAX_SKILL_FILE_SIZE = 512_000

# The filename inside each skill directory.
SKILL_FILENAME = "SKILL.md"


# =============================================================================
# Exceptions
# =============================================================================


class SkillError(Exception):
    """Base exception for skill management errors."""

    pass


class SkillExistsError(SkillError):
    """Raised when trying to add a skill with a name that already exists."""

    pass


class SkillNotFoundError(SkillError):
    """Raised when a referenced skill does not exist."""

    pass


class SkillValidationError(SkillError):
    """Raised when a skill file fails validation."""

    pass


# =============================================================================
# Plugin Manifest Management
# =============================================================================


def ensure_plugin_manifest(plugin_dir: Path) -> None:
    """
    Create or verify the plugin.json manifest.

    If ``plugin.json`` does not exist, creates it with the fixed plugin
    identity.  If it exists but the ``name`` field does not match
    ``PLUGIN_NAME``, regenerates it (self-healing for renames).

    Args:
        plugin_dir: Root of the plugin directory (parent of ``.claude-plugin/``).
    """
    manifest_dir = plugin_dir / ".claude-plugin"
    manifest_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = manifest_dir / "plugin.json"

    if manifest_path.exists():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if existing.get("name") == PLUGIN_NAME:
                return  # Up to date
            logger.info(
                "plugin_manifest_stale",
                old_name=existing.get("name"),
                new_name=PLUGIN_NAME,
                message="Plugin manifest name mismatch — regenerating.",
            )
        except (json.JSONDecodeError, OSError):
            pass  # Corrupt or unreadable — regenerate

    manifest = {
        "name": PLUGIN_NAME,
        "description": PLUGIN_DESCRIPTION,
        "version": PLUGIN_VERSION,
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )
    logger.info(
        "plugin_manifest_created",
        path=str(manifest_path),
        plugin_name=PLUGIN_NAME,
    )


# =============================================================================
# Validation Helpers
# =============================================================================


def validate_skill_name(name: str) -> None:
    """
    Validate a skill name.

    Rules:
    - Must start with a lowercase letter.
    - May contain lowercase letters, digits, and hyphens.
    - Maximum length: 64 characters.

    Args:
        name: Proposed skill name.

    Raises:
        SkillValidationError: If the name is invalid.
    """
    if not name:
        raise SkillValidationError("Skill name cannot be empty")
    if len(name) > MAX_NAME_LENGTH:
        raise SkillValidationError(
            f"Skill name too long ({len(name)} chars, max {MAX_NAME_LENGTH})"
        )
    if not _SKILL_NAME_PATTERN.match(name):
        raise SkillValidationError(
            f"Skill name '{name}' is invalid. Must start with a letter "
            "and contain only letters, digits, and hyphens."
        )


def validate_skill_content(content: str, name: str) -> tuple[dict[str, Any], str]:
    """
    Validate the full content of a SKILL.md file.

    Checks:
    - Content size within limits.
    - Valid YAML frontmatter with required field (``description``).
    - Non-empty skill body.
    - If frontmatter ``name`` is present, it must match the registered name.
    - Boolean fields must be booleans.
    - ``context`` field must be ``"fork"`` if present.

    Args:
        content: Raw file content.
        name: Expected skill name (from CLI argument).

    Returns:
        Tuple of (validated_frontmatter, body).

    Raises:
        SkillValidationError: If validation fails.
    """
    if len(content.encode("utf-8")) > MAX_SKILL_FILE_SIZE:
        raise SkillValidationError(
            f"Skill file exceeds maximum size "
            f"({len(content.encode('utf-8'))} bytes, max {MAX_SKILL_FILE_SIZE})"
        )

    # Reuse the generic YAML frontmatter parser from agent_manager
    from .agent_manager import parse_agent_file, AgentValidationError

    try:
        frontmatter, body = parse_agent_file(content)
    except AgentValidationError as exc:
        raise SkillValidationError(str(exc)) from exc

    if not frontmatter:
        raise SkillValidationError(
            "Skill file must have YAML frontmatter with at least "
            "a 'description' field"
        )

    # Validate 'name' field if present
    fm_name = frontmatter.get("name")
    if fm_name is not None:
        if not isinstance(fm_name, str):
            raise SkillValidationError(
                f"Frontmatter 'name' must be a string, got {type(fm_name).__name__}"
            )
        if fm_name != name:
            raise SkillValidationError(
                f"Frontmatter name '{fm_name}' does not match "
                f"the skill name '{name}'. They must be identical."
            )

    # Validate 'description' field (required)
    fm_description = frontmatter.get("description")
    if not fm_description:
        raise SkillValidationError(
            "YAML frontmatter missing required field: 'description'"
        )

    if not body:
        raise SkillValidationError(
            "Skill body (instructions) cannot be empty"
        )

    # Validate optional boolean fields
    for bool_field in ("disable-model-invocation", "user-invocable"):
        val = frontmatter.get(bool_field)
        if val is not None and not isinstance(val, bool):
            raise SkillValidationError(
                f"Frontmatter '{bool_field}' must be a boolean, "
                f"got {type(val).__name__}"
            )

    # Validate 'context' field
    context_val = frontmatter.get("context")
    if context_val is not None and context_val != "fork":
        raise SkillValidationError(
            f"Frontmatter 'context' must be 'fork' if present, "
            f"got '{context_val}'"
        )

    return frontmatter, body


# =============================================================================
# Skill Manager
# =============================================================================


class SkillManager:
    """
    Manages skill directories and metadata.

    Provides CRUD operations on skill directories under
    ``/data/skills-plugin/skills/<name>/`` and the
    ``/data/skills-meta/skills.json`` metadata registry.

    Thread/process safety: file-based locking via ``/data/skills-meta/.lock``
    prevents concurrent modification from multiple concurrent requests.
    """

    def __init__(self, skills_dir: Path, meta_dir: Path, plugin_dir: Path):
        """
        Initialize the skill manager.

        Args:
            skills_dir: Path to the skills directory inside the plugin
                (e.g., ``/data/skills-plugin/skills/``).
            meta_dir: Path to the metadata directory
                (e.g., ``/data/skills-meta/``).
            plugin_dir: Path to the plugin root directory
                (e.g., ``/data/skills-plugin/``).
        """
        self._skills_dir = skills_dir
        self._meta_dir = meta_dir
        self._plugin_dir = plugin_dir
        self._meta_file = meta_dir / "skills.json"
        self._lock_file = meta_dir / ".lock"
        self._metadata: dict[str, SkillEntry] = {}
        self._load_metadata()

    # =========================================================================
    # Internal -- Metadata Persistence
    # =========================================================================

    def _load_metadata(self) -> None:
        """Load skill metadata from skills.json."""
        if not self._meta_file.exists():
            logger.debug(
                "skill_metadata_file_not_found", path=str(self._meta_file)
            )
            self._metadata = {}
            return

        try:
            content = self._meta_file.read_text(encoding="utf-8")
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
                self._metadata[name] = SkillEntry(
                    name=name,
                    description=meta.get("description", ""),
                    added_at=added_at,
                    skill_size_bytes=meta.get("skill_size_bytes", 0),
                    file_count=meta.get("file_count", 1),
                )

            logger.info(
                "skill_metadata_loaded",
                count=len(self._metadata),
                path=str(self._meta_file),
            )

        except json.JSONDecodeError as exc:
            logger.error(
                "skill_metadata_parse_error",
                error=str(exc),
                path=str(self._meta_file),
            )
            raise SkillError(
                f"Failed to parse skill metadata at {self._meta_file}: {exc}"
            ) from exc
        except Exception as exc:
            logger.error(
                "skill_metadata_load_error",
                error=str(exc),
                path=str(self._meta_file),
            )
            raise SkillError(
                f"Failed to load skill metadata: {exc}"
            ) from exc

    def _save_metadata(self) -> None:
        """
        Save skill metadata to skills.json.

        Uses file-based locking for concurrent protection.
        """
        self._meta_dir.mkdir(parents=True, exist_ok=True)

        raw_meta: dict[str, dict[str, Any]] = {}
        for name, entry in self._metadata.items():
            raw_meta[name] = {
                "added_at": entry.added_at.isoformat(),
                "description": entry.description,
                "skill_size_bytes": entry.skill_size_bytes,
                "file_count": entry.file_count,
            }

        data = {"_metadata": raw_meta}

        try:
            self._lock_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._lock_file, "w") as lock_fd:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                try:
                    self._meta_file.write_text(
                        json.dumps(data, indent=2, default=str),
                        encoding="utf-8",
                    )
                finally:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)

            logger.info(
                "skill_metadata_saved",
                count=len(self._metadata),
                path=str(self._meta_file),
            )

        except Exception as exc:
            logger.error(
                "skill_metadata_save_error",
                error=str(exc),
                path=str(self._meta_file),
            )
            raise SkillError(
                f"Failed to save skill metadata: {exc}"
            ) from exc

    # =========================================================================
    # Internal -- Skill Directory I/O
    # =========================================================================

    def _skill_dir_path(self, name: str) -> Path:
        """Return path to a skill's directory."""
        return self._skills_dir / name

    def _skill_file_path(self, name: str) -> Path:
        """Return path to a skill's SKILL.md file."""
        return self._skills_dir / name / SKILL_FILENAME

    def _read_skill(self, name: str) -> str | None:
        """
        Read a skill's SKILL.md file content.

        Returns:
            File content as string, or None if the file does not exist.
        """
        path = self._skill_file_path(name)
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except Exception as exc:
            logger.error(
                "skill_file_read_error",
                skill_name=name,
                path=str(path),
                error=str(exc),
            )
            return None

    def _delete_skill(self, name: str) -> bool:
        """
        Delete a skill's entire directory.

        Returns:
            True if the directory existed and was deleted, False otherwise.
        """
        skill_dir = self._skill_dir_path(name)
        if not skill_dir.is_dir():
            return False
        try:
            shutil.rmtree(skill_dir)
            logger.info(
                "skill_directory_deleted",
                skill_name=name,
                path=str(skill_dir),
            )
            return True
        except OSError as exc:
            logger.error(
                "skill_directory_delete_error",
                skill_name=name,
                path=str(skill_dir),
                error=str(exc),
            )
            raise SkillError(
                f"Failed to delete skill directory for '{name}': {exc}"
            ) from exc

    # =========================================================================
    # Internal -- Atomic Directory Operations
    # =========================================================================

    def _atomic_install_directory(self, name: str, source_dir: Path) -> None:
        """
        Atomically install a skill directory (for new skills).

        Renames ``source_dir`` to the final skill directory path.
        Both must be on the same filesystem for atomic rename.

        Args:
            name: Skill name (determines final directory name).
            source_dir: Temp directory containing the extracted skill.

        Raises:
            SkillError: If the rename fails.
        """
        target = self._skill_dir_path(name)
        try:
            os.rename(source_dir, target)
            logger.info(
                "skill_directory_installed",
                skill_name=name,
                source=str(source_dir),
                target=str(target),
            )
        except OSError as exc:
            raise SkillError(
                f"Failed to install skill directory for '{name}': {exc}"
            ) from exc

    def _atomic_replace_directory(self, name: str, source_dir: Path) -> None:
        """
        Atomically replace an existing skill directory.

        Strategy:
          1. Rename existing to ``.old-<uuid>`` (backup)
          2. Rename source to final name (install)
          3. Delete backup
          On failure at step 2: rollback by renaming backup back.

        Args:
            name: Skill name.
            source_dir: Temp directory containing the new skill.

        Raises:
            SkillError: If the replacement fails.
        """
        target = self._skill_dir_path(name)
        backup = self._skills_dir / f".old-{uuid.uuid4().hex[:12]}"

        try:
            # Step 1: backup existing
            os.rename(target, backup)
        except OSError as exc:
            raise SkillError(
                f"Failed to backup existing skill directory for '{name}': {exc}"
            ) from exc

        try:
            # Step 2: install new
            os.rename(source_dir, target)
        except OSError as exc:
            # Rollback: restore backup
            try:
                os.rename(backup, target)
            except OSError:
                pass  # Best effort rollback
            raise SkillError(
                f"Failed to install updated skill directory for '{name}': {exc}"
            ) from exc

        # Step 3: cleanup backup
        try:
            shutil.rmtree(backup)
        except OSError as exc:
            logger.warning(
                "skill_backup_cleanup_failed",
                skill_name=name,
                backup_path=str(backup),
                error=str(exc),
            )

    # =========================================================================
    # Public API
    # =========================================================================

    def add_skill(
        self,
        name: str,
        source_dir: Path,
        skill_md_content: str,
        file_count: int,
        total_size_bytes: int,
        file_listing: list[str],
    ) -> SkillEntry:
        """
        Add a new skill from a validated extracted directory.

        The ``source_dir`` must contain the full skill structure
        (SKILL.md + optional subdirs) and must already be validated.
        It will be atomically renamed to the final location.

        Args:
            name: Unique skill name.
            source_dir: Temp directory with extracted skill contents.
            skill_md_content: Content of SKILL.md (already validated).
            file_count: Number of files in the skill directory.
            total_size_bytes: Total uncompressed size.
            file_listing: Sorted list of relative file paths.

        Returns:
            The created SkillEntry metadata.

        Raises:
            SkillExistsError: If a skill with the same name exists.
            SkillValidationError: If the content fails validation.
        """
        validate_skill_name(name)

        if name in self._metadata or self._skill_file_path(name).exists():
            raise SkillExistsError(
                f"Skill '{name}' already exists."
            )

        # Case-insensitive uniqueness: prevent "MySkill" when "myskill" exists
        name_lower = name.lower()
        for existing in self._metadata:
            if existing.lower() == name_lower and existing != name:
                raise SkillExistsError(
                    f"Skill '{name}' conflicts with existing skill "
                    f"'{existing}' (names differ only in case)."
                )

        # Parse frontmatter for description
        frontmatter, _body = validate_skill_content(skill_md_content, name)

        # Atomic install
        self._atomic_install_directory(name, source_dir)

        # Ensure plugin manifest
        ensure_plugin_manifest(self._plugin_dir)

        description = frontmatter.get("description", "")

        # Update metadata
        entry = SkillEntry(
            name=name,
            description=description,
            added_at=utcnow(),
            skill_size_bytes=total_size_bytes,
            file_count=file_count,
        )
        self._metadata[name] = entry
        self._save_metadata()

        logger.info(
            "skill_added",
            skill_name=name,
            description=description,
            skill_size_bytes=total_size_bytes,
            file_count=file_count,
        )

        return entry

    def update_skill(
        self,
        name: str,
        source_dir: Path,
        skill_md_content: str,
        file_count: int,
        total_size_bytes: int,
        file_listing: list[str],
    ) -> SkillEntry:
        """
        Update an existing skill by replacing its directory.

        The existing directory is atomically replaced with the new
        one from ``source_dir``.

        Args:
            name: Skill name to update.
            source_dir: Temp directory with new skill contents.
            skill_md_content: Content of new SKILL.md (already validated).
            file_count: Number of files in the new skill directory.
            total_size_bytes: Total uncompressed size.
            file_listing: Sorted list of relative file paths.

        Returns:
            Updated SkillEntry.

        Raises:
            SkillNotFoundError: If the skill does not exist.
            SkillValidationError: If new content fails validation.
        """
        if name not in self._metadata and not self._skill_file_path(name).exists():
            raise SkillNotFoundError(
                f"Skill '{name}' not found."
            )

        # Parse frontmatter for description
        frontmatter, _body = validate_skill_content(skill_md_content, name)

        # Atomic replace
        self._atomic_replace_directory(name, source_dir)

        # Ensure plugin manifest
        ensure_plugin_manifest(self._plugin_dir)

        description = frontmatter.get("description", "")

        # Update metadata (preserve added_at from existing entry)
        existing = self._metadata.get(name)
        entry = SkillEntry(
            name=name,
            description=description,
            added_at=existing.added_at if existing else utcnow(),
            skill_size_bytes=total_size_bytes,
            file_count=file_count,
        )
        self._metadata[name] = entry
        self._save_metadata()

        logger.info(
            "skill_updated",
            skill_name=name,
            skill_size_bytes=total_size_bytes,
            file_count=file_count,
        )

        return entry

    def remove_skill(self, name: str) -> SkillEntry | None:
        """
        Remove a skill definition.

        Deletes the skill directory and removes metadata from skills.json.

        Args:
            name: Skill name to remove.

        Returns:
            The removed SkillEntry metadata, or None if not found.
        """
        entry = self._metadata.pop(name, None)
        dir_deleted = self._delete_skill(name)

        if entry is None and not dir_deleted:
            return None

        self._save_metadata()

        logger.info(
            "skill_removed",
            skill_name=name,
            had_metadata=entry is not None,
            had_directory=dir_deleted,
        )

        return entry

    def get_skill(self, name: str) -> tuple[SkillEntry | None, str | None, list[str]]:
        """
        Get a specific skill's metadata, file content, and file listing.

        Args:
            name: Skill name.

        Returns:
            Tuple of (SkillEntry or None, SKILL.md content or None, file_listing).
        """
        entry = self._metadata.get(name)
        content = self._read_skill(name)
        file_listing = self.list_skill_files(name)
        return entry, content, file_listing

    def list_skill_files(self, name: str) -> list[str]:
        """
        List all files in a skill's directory as relative paths.

        Args:
            name: Skill name.

        Returns:
            Sorted list of relative file paths, or empty list if
            the skill directory does not exist.
        """
        skill_dir = self._skill_dir_path(name)
        if not skill_dir.is_dir():
            return []

        files: list[str] = []
        for path in skill_dir.rglob("*"):
            if path.is_file():
                files.append(str(path.relative_to(skill_dir)))

        files.sort()
        return files

    def list_skills(self) -> list[SkillEntry]:
        """
        List all skills with metadata.

        Cross-references the metadata registry with actual skill
        directories on disk.  Reports discrepancies via logging.

        Returns:
            List of SkillEntry objects for skills that have both
            metadata and a SKILL.md file on disk.
        """
        # Scan skill directories on disk
        disk_skills: set[str] = set()
        if self._skills_dir.is_dir():
            for path in self._skills_dir.iterdir():
                if (
                    path.is_dir()
                    and not path.name.startswith(".")
                    and (path / SKILL_FILENAME).exists()
                ):
                    disk_skills.add(path.name)

        # Report orphaned directories (on disk but not in metadata)
        orphaned = disk_skills - set(self._metadata.keys())
        if orphaned:
            logger.warning(
                "skill_orphaned_directories",
                orphaned=sorted(orphaned),
                message=(
                    "Skill directories found on disk without metadata entries. "
                    "These skills are still usable by Claude Code via the plugin "
                    "but are not tracked in skills.json."
                ),
            )

        # Report missing directories (in metadata but not on disk)
        missing = set(self._metadata.keys()) - disk_skills
        if missing:
            logger.warning(
                "skill_missing_directories",
                missing=sorted(missing),
                message=(
                    "Skill metadata entries found without corresponding "
                    "directories. These skills will NOT be available to "
                    "Claude Code."
                ),
            )

        # Return metadata for skills that exist on disk
        result: list[SkillEntry] = []
        for name, entry in sorted(self._metadata.items()):
            if name in disk_skills:
                result.append(entry)

        # Also include orphaned skills (on disk, not in metadata)
        for name in sorted(orphaned):
            content = self._read_skill(name)
            if content is not None:
                try:
                    from .agent_manager import parse_agent_file, AgentValidationError

                    frontmatter, _ = parse_agent_file(content)
                    skill_description = frontmatter.get("description", "")
                except (AgentValidationError, Exception):
                    skill_description = "(invalid frontmatter)"

                file_count = len(self.list_skill_files(name))

                result.append(
                    SkillEntry(
                        name=name,
                        description=f"[untracked] {skill_description}",
                        skill_size_bytes=len(content.encode("utf-8")),
                        file_count=max(file_count, 1),
                    )
                )

        return result

    def has_skills(self) -> bool:
        """Check whether any skills are configured (directories on disk)."""
        if not self._skills_dir.is_dir():
            return False
        return any(self._skills_dir.glob(f"*/{SKILL_FILENAME}"))

    @property
    def skills_dir(self) -> Path:
        """Return the path to the skills directory."""
        return self._skills_dir

    @property
    def plugin_dir(self) -> Path:
        """Return the path to the plugin root directory."""
        return self._plugin_dir

    def reload(self) -> None:
        """Reload metadata from disk."""
        self._load_metadata()
