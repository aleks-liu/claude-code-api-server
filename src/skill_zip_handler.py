"""
Secure ZIP archive validation and extraction for skill uploads.

Handles all security checks for skill ZIP archives before they are
deployed to the plugin directory.  Keeps ZIP-specific logic separated
from the business-level SkillManager.

Security layers (defense-in-depth):
  1. Compressed size limit (transport)
  2. Pre-extraction validation (no disk writes):
     - Format check, symlink detection, path traversal, duplicates,
       size limits, file count, filename allowlist, nesting depth,
       structure validation (SKILL.md required)
  3. Extraction with actual-bytes tracking (zip bomb defense)
  4. Post-extraction SKILL.md content validation
"""

import io
import os
import re
import shutil
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .logging_config import get_logger

logger = get_logger(__name__)


# =============================================================================
# Constants
# =============================================================================

MAX_SKILL_ZIP_SIZE = 15 * 1024 * 1024  # 15 MB compressed
MAX_SKILL_EXTRACTED_SIZE = 50 * 1024 * 1024  # 50 MB total extracted
MAX_SKILL_FILE_COUNT = 100
MAX_SKILL_NESTING_DEPTH = 5  # levels below skill root
MAX_SKILL_INDIVIDUAL_FILE_SIZE = 5 * 1024 * 1024  # 5 MB per file
MAX_FILENAME_LENGTH = 100  # per path component
MAX_PATH_LENGTH = 512  # total relative path

SKILL_FILENAME = "SKILL.md"

# Filename: must start with alphanumeric, then alphanumeric + dots + underscores + hyphens.
_FILENAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")

# Directory name: must start with alphanumeric, then alphanumeric + underscores + hyphens (no dots).
_DIRNAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")

# Unix symlink flag in external_attr.
_SYMLINK_TYPE = 0o120000
_TYPE_MASK = 0o170000


# =============================================================================
# Exceptions
# =============================================================================


class SkillZipError(Exception):
    """Base exception for skill ZIP processing errors."""


class SkillZipSizeError(SkillZipError):
    """ZIP or extracted content exceeds size limits."""


class SkillZipStructureError(SkillZipError):
    """ZIP structure is invalid (missing SKILL.md, disallowed dirs, etc.)."""


class SkillZipSecurityError(SkillZipError):
    """Security violation detected (path traversal, symlinks, etc.)."""


# =============================================================================
# Result
# =============================================================================


@dataclass
class SkillZipResult:
    """Result of successful ZIP validation and extraction."""

    temp_dir: Path
    """Temp directory containing the extracted skill root directory."""

    skill_name: str
    """Skill name derived from the ZIP root dir or name override."""

    skill_md_content: str
    """Raw content of SKILL.md (for frontmatter parsing)."""

    file_count: int
    """Number of files in the archive."""

    total_size_bytes: int
    """Total uncompressed size in bytes."""

    file_listing: list[str] = field(default_factory=list)
    """Sorted list of relative paths from the skill root."""

    def cleanup(self) -> None:
        """Remove the temporary directory.  Safe to call multiple times."""
        if self.temp_dir and self.temp_dir.exists():
            try:
                shutil.rmtree(self.temp_dir)
            except OSError as exc:
                logger.warning(
                    "skill_zip_temp_cleanup_failed",
                    path=str(self.temp_dir),
                    error=str(exc),
                )


# =============================================================================
# Internal Validators
# =============================================================================


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    """Check if a ZIP entry represents a symlink."""
    unix_mode = (info.external_attr >> 16) & _TYPE_MASK
    return unix_mode == _SYMLINK_TYPE


def _has_path_traversal(filename: str) -> bool:
    """Check if a filename contains path traversal sequences."""
    normalized = filename.replace("\\", "/")
    if ".." in normalized.split("/"):
        return True
    if normalized.startswith("/"):
        return True
    # Windows absolute paths
    if len(normalized) >= 2 and normalized[1] == ":":
        return True
    return False


def _validate_path_component(component: str, is_dir: bool) -> str | None:
    """
    Validate a single path component (filename or directory name).

    Returns an error message string if invalid, None if valid.
    """
    if not component:
        return "empty path component"

    if len(component) > MAX_FILENAME_LENGTH:
        return (
            f"path component '{component}' exceeds maximum length "
            f"({len(component)} > {MAX_FILENAME_LENGTH})"
        )

    if is_dir:
        if not _DIRNAME_RE.match(component):
            return (
                f"directory name '{component}' contains invalid characters "
                f"(allowed: alphanumeric, underscores, hyphens; must start with alphanumeric)"
            )
    else:
        if not _FILENAME_RE.match(component):
            return (
                f"filename '{component}' contains invalid characters "
                f"(allowed: alphanumeric, dots, underscores, hyphens; must start with alphanumeric)"
            )

    return None


def _normalize_entry_path(filename: str) -> str:
    """Normalize a ZIP entry path: forward slashes, strip trailing slash."""
    return filename.replace("\\", "/").rstrip("/")


# =============================================================================
# Pre-extraction Validation
# =============================================================================


def _pre_validate_zip(
    zf: zipfile.ZipFile,
    name_override: str | None,
) -> tuple[str, str]:
    """
    Validate all ZIP entries without extracting any files.

    Returns:
        Tuple of (strip_prefix, skill_name) where strip_prefix is the
        path prefix to strip from entries during extraction.

    Raises:
        SkillZipSecurityError: On security violations.
        SkillZipSizeError: On size/count violations.
        SkillZipStructureError: On structural violations.
    """
    info_list = zf.infolist()

    # --- File count ---
    file_entries = [e for e in info_list if not e.is_dir()]
    if len(file_entries) > MAX_SKILL_FILE_COUNT:
        raise SkillZipSizeError(
            f"Archive contains {len(file_entries)} files, "
            f"maximum is {MAX_SKILL_FILE_COUNT}"
        )

    if not file_entries:
        raise SkillZipStructureError("Archive contains no files")

    # --- Aggregate size check (declared) ---
    total_declared_size = sum(e.file_size for e in info_list)
    if total_declared_size > MAX_SKILL_EXTRACTED_SIZE:
        raise SkillZipSizeError(
            f"Archive declared uncompressed size "
            f"({total_declared_size / (1024 * 1024):.1f} MB) "
            f"exceeds maximum ({MAX_SKILL_EXTRACTED_SIZE / (1024 * 1024):.0f} MB)"
        )

    # --- Per-entry validation ---
    seen_paths: set[str] = set()

    for entry in info_list:
        normalized = _normalize_entry_path(entry.filename)
        if not normalized:
            continue

        # Symlink check
        if _is_symlink(entry):
            raise SkillZipSecurityError(
                f"Symlinks are not allowed: {entry.filename}"
            )

        # Path traversal check
        if _has_path_traversal(normalized):
            raise SkillZipSecurityError(
                f"Path traversal detected: {entry.filename}"
            )

        # Duplicate check
        lower_path = normalized.lower()
        if lower_path in seen_paths:
            raise SkillZipSecurityError(
                f"Duplicate entry detected: {entry.filename}"
            )
        seen_paths.add(lower_path)

        # Individual file size check (skip directories)
        if not entry.is_dir() and entry.file_size > MAX_SKILL_INDIVIDUAL_FILE_SIZE:
            raise SkillZipSizeError(
                f"File '{entry.filename}' is too large "
                f"({entry.file_size / (1024 * 1024):.1f} MB, "
                f"max {MAX_SKILL_INDIVIDUAL_FILE_SIZE / (1024 * 1024):.0f} MB)"
            )

        # Path length check
        if len(normalized) > MAX_PATH_LENGTH:
            raise SkillZipSecurityError(
                f"Path too long ({len(normalized)} chars, "
                f"max {MAX_PATH_LENGTH}): {entry.filename}"
            )

    # --- Detect root structure ---
    # Collect all top-level entries to determine structure
    file_paths = [
        _normalize_entry_path(e.filename)
        for e in info_list
        if not e.is_dir() and _normalize_entry_path(e.filename)
    ]

    top_level_components: set[str] = set()
    for path in file_paths:
        top_level_components.add(path.split("/")[0])

    # Case 1: Single root directory wrapping everything
    # Case 2: SKILL.md directly at archive root (flat layout)
    strip_prefix = ""
    skill_name: str

    if len(top_level_components) == 1:
        sole_component = top_level_components.pop()
        # Check if all files are inside a single directory
        all_nested = all("/" in p for p in file_paths)

        if all_nested:
            # Single root dir — use it as skill name
            strip_prefix = sole_component + "/"
            skill_name = name_override if name_override else sole_component
        elif sole_component == SKILL_FILENAME:
            # Single file: just SKILL.md at root
            if not name_override:
                raise SkillZipStructureError(
                    "ZIP contains files at root level without a wrapping directory. "
                    "Provide a 'name' field to specify the skill name."
                )
            skill_name = name_override
        else:
            raise SkillZipStructureError(
                f"Unexpected root-level file: {sole_component}. "
                f"ZIP must contain a skill directory with SKILL.md inside it, "
                f"or SKILL.md at the archive root with a 'name' field."
            )
    else:
        # Multiple top-level items — check if it's a flat layout
        if SKILL_FILENAME in top_level_components:
            if not name_override:
                raise SkillZipStructureError(
                    "ZIP contains files at root level without a wrapping directory. "
                    "Provide a 'name' field to specify the skill name."
                )
            skill_name = name_override
        else:
            raise SkillZipStructureError(
                "ZIP must contain either a single root directory with SKILL.md "
                "inside it, or SKILL.md at the archive root. "
                f"Found top-level entries: {sorted(top_level_components)}"
            )

    # --- Validate entries relative to skill root ---
    skill_md_found = False

    for path in file_paths:
        # Strip the root prefix to get the path relative to skill root
        relative = path[len(strip_prefix):] if strip_prefix else path
        if not relative:
            continue

        parts = relative.split("/")

        # Nesting depth check
        if len(parts) > MAX_SKILL_NESTING_DEPTH + 1:  # +1 for the file itself
            raise SkillZipStructureError(
                f"Path '{relative}' exceeds maximum nesting depth "
                f"of {MAX_SKILL_NESTING_DEPTH}"
            )

        # Track SKILL.md presence
        root_entry = parts[0]
        if root_entry == SKILL_FILENAME and len(parts) == 1:
            skill_md_found = True

        # Validate each path component
        for i, component in enumerate(parts):
            is_directory = i < len(parts) - 1  # all except last are dirs
            error = _validate_path_component(component, is_dir=is_directory)
            if error:
                raise SkillZipSecurityError(
                    f"Invalid path in '{relative}': {error}"
                )

    # Validate directory-only entries (dirname regex check)
    dir_paths = [
        _normalize_entry_path(e.filename)
        for e in info_list
        if e.is_dir() and _normalize_entry_path(e.filename)
    ]
    for dpath in dir_paths:
        relative = dpath[len(strip_prefix):] if strip_prefix else dpath
        if not relative:
            continue
        parts = relative.split("/")
        for component in parts:
            if component:
                error = _validate_path_component(component, is_dir=True)
                if error:
                    raise SkillZipSecurityError(
                        f"Invalid directory in '{relative}': {error}"
                    )

    if not skill_md_found:
        raise SkillZipStructureError(
            "Archive must contain SKILL.md at the skill root level"
        )

    return strip_prefix, skill_name


# =============================================================================
# Extraction
# =============================================================================


def _extract_to_temp(
    zf: zipfile.ZipFile,
    strip_prefix: str,
    temp_parent: Path,
) -> tuple[Path, int, list[str]]:
    """
    Extract validated ZIP contents to a temporary directory.

    Tracks actual bytes written to defend against lying ZIP headers.

    Args:
        zf: Opened ZipFile (already validated).
        strip_prefix: Prefix to strip from entry paths.
        temp_parent: Parent directory for the temp extraction dir.

    Returns:
        Tuple of (temp_dir, actual_total_bytes, file_listing).

    Raises:
        SkillZipSizeError: If actual extracted bytes exceed limit.
        SkillZipSecurityError: If path safety check fails during extraction.
    """
    temp_dir = temp_parent / f".tmp-{uuid.uuid4().hex[:12]}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_dir_resolved = temp_dir.resolve()

    actual_total_bytes = 0
    file_listing: list[str] = []

    try:
        for entry in zf.infolist():
            if entry.is_dir():
                continue

            normalized = _normalize_entry_path(entry.filename)
            if not normalized:
                continue

            # Strip root prefix
            relative = normalized[len(strip_prefix):] if strip_prefix else normalized
            if not relative:
                continue

            # Safety: resolve target and verify containment
            target = temp_dir / relative
            target_resolved = target.resolve()
            # Parent may not exist yet, so check against temp_dir prefix
            if not str(target_resolved).startswith(str(temp_dir_resolved)):
                raise SkillZipSecurityError(
                    f"Path escape detected during extraction: {relative}"
                )

            # Create parent directories
            target.parent.mkdir(parents=True, exist_ok=True)

            # Extract with byte tracking
            with zf.open(entry) as source:
                with open(target, "wb") as dest:
                    while True:
                        chunk = source.read(65536)
                        if not chunk:
                            break
                        actual_total_bytes += len(chunk)
                        if actual_total_bytes > MAX_SKILL_EXTRACTED_SIZE:
                            raise SkillZipSizeError(
                                f"Actual extracted size exceeds maximum "
                                f"({MAX_SKILL_EXTRACTED_SIZE / (1024 * 1024):.0f} MB). "
                                f"Possible zip bomb."
                            )
                        dest.write(chunk)

            file_listing.append(relative)

        file_listing.sort()
        return temp_dir, actual_total_bytes, file_listing

    except (SkillZipSizeError, SkillZipSecurityError):
        # Clean up on validation failure
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise SkillZipError(f"Extraction failed: {exc}") from exc


# =============================================================================
# Public API
# =============================================================================


def validate_and_extract_skill_zip(
    zip_bytes: bytes,
    name_override: str | None = None,
    temp_parent: Path | None = None,
) -> SkillZipResult:
    """
    Validate a skill ZIP archive and extract it to a temporary directory.

    Performs comprehensive security validation before and during
    extraction.  On success, the caller is responsible for calling
    ``result.cleanup()`` after the extracted directory is no longer
    needed (e.g. after atomic move to the final location).

    Args:
        zip_bytes: Raw bytes of the ZIP archive.
        name_override: Optional skill name override.  If not provided,
            the name is derived from the ZIP's root directory.
        temp_parent: Parent directory for temporary extraction.
            Must be on the same filesystem as the final skills
            directory for atomic rename.  If None, uses a default.

    Returns:
        SkillZipResult with extraction details.

    Raises:
        SkillZipSizeError: If size limits are exceeded.
        SkillZipStructureError: If ZIP structure is invalid.
        SkillZipSecurityError: If security violations are detected.
        SkillZipError: On other processing errors.
    """
    # --- 1. Compressed size check ---
    if len(zip_bytes) > MAX_SKILL_ZIP_SIZE:
        raise SkillZipSizeError(
            f"ZIP archive is too large "
            f"({len(zip_bytes) / (1024 * 1024):.1f} MB, "
            f"max {MAX_SKILL_ZIP_SIZE / (1024 * 1024):.0f} MB)"
        )

    # --- 2. Format check ---
    if len(zip_bytes) < 4 or not zip_bytes[:2] == b"PK":
        raise SkillZipStructureError("Not a valid ZIP archive (invalid magic bytes)")

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes), "r")
    except zipfile.BadZipFile as exc:
        raise SkillZipStructureError(f"Not a valid ZIP archive: {exc}") from exc

    try:
        # --- 3. Pre-extraction validation ---
        strip_prefix, skill_name = _pre_validate_zip(zf, name_override)

        # --- 4. Validate skill name ---
        from .skill_manager import validate_skill_name, SkillValidationError

        try:
            validate_skill_name(skill_name)
        except SkillValidationError as exc:
            raise SkillZipStructureError(
                f"Invalid skill name '{skill_name}': {exc}"
            ) from exc

        # --- 5. Extract ---
        if temp_parent is None:
            raise SkillZipError(
                "temp_parent is required (must be on same filesystem "
                "as the skills directory for atomic rename)"
            )

        temp_dir, actual_size, file_listing = _extract_to_temp(
            zf, strip_prefix, temp_parent
        )

        # --- 6. Post-extraction: validate SKILL.md content ---
        skill_md_path = temp_dir / SKILL_FILENAME
        try:
            skill_md_content = skill_md_path.read_text(encoding="utf-8")
        except Exception as exc:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise SkillZipStructureError(
                f"Failed to read SKILL.md: {exc}"
            ) from exc

        from .skill_manager import validate_skill_content

        try:
            validate_skill_content(skill_md_content, skill_name)
        except SkillValidationError as exc:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise  # Re-raise as SkillValidationError (handled by router)

        logger.info(
            "skill_zip_validated",
            skill_name=skill_name,
            file_count=len(file_listing),
            total_size_bytes=actual_size,
            compressed_size_bytes=len(zip_bytes),
        )

        return SkillZipResult(
            temp_dir=temp_dir,
            skill_name=skill_name,
            skill_md_content=skill_md_content,
            file_count=len(file_listing),
            total_size_bytes=actual_size,
            file_listing=file_listing,
        )

    finally:
        zf.close()


def cleanup_orphaned_temp_dirs(skills_dir: Path) -> int:
    """
    Remove orphaned ``.tmp-*`` directories left by interrupted operations.

    Call at startup to prevent disk leaks.

    Args:
        skills_dir: The skills directory (e.g. ``/data/skills-plugin/skills/``).

    Returns:
        Number of directories cleaned up.
    """
    if not skills_dir.is_dir():
        return 0

    cleaned = 0
    for entry in skills_dir.iterdir():
        if entry.is_dir() and entry.name.startswith(".tmp-"):
            try:
                shutil.rmtree(entry)
                cleaned += 1
                logger.info(
                    "skill_orphaned_temp_cleaned",
                    path=str(entry),
                )
            except OSError as exc:
                logger.warning(
                    "skill_orphaned_temp_cleanup_failed",
                    path=str(entry),
                    error=str(exc),
                )

    # Also clean old backup dirs from interrupted updates
    for entry in skills_dir.iterdir():
        if entry.is_dir() and entry.name.startswith(".old-"):
            try:
                shutil.rmtree(entry)
                cleaned += 1
                logger.info(
                    "skill_orphaned_backup_cleaned",
                    path=str(entry),
                )
            except OSError as exc:
                logger.warning(
                    "skill_orphaned_backup_cleanup_failed",
                    path=str(entry),
                    error=str(exc),
                )

    return cleaned
