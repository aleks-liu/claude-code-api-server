"""Smart file processing — zip creation and upload preparation."""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from pathlib import Path

# Directories to exclude when zipping
DEFAULT_EXCLUDES = {
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".DS_Store",
    "Thumbs.db",
}

# File extensions to exclude
DEFAULT_EXCLUDE_EXTENSIONS = {".pyc", ".pyo"}

MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB
MAX_UPLOADS = 5


@dataclass
class PreparedFile:
    """A file ready for upload."""

    label: str  # Display label: "src/ (2.3 MB)"
    zip_bytes: bytes  # ZIP content
    source_path: str  # Original path (for error messages)


def prepare_files(paths: list[str]) -> list[PreparedFile]:
    """Process paths into ZIP-ready uploads.

    For each path:
    - Directory -> zip recursively (excluding defaults)
    - .zip file -> read as-is
    - Other file -> wrap in single-file ZIP

    Raises ValueError on: path not found, too many paths, ZIP too large.
    """
    if len(paths) > MAX_UPLOADS:
        raise ValueError(
            f"Too many files ({len(paths)}). Maximum {MAX_UPLOADS} uploads per job."
        )

    # Validate all paths exist first (fail fast)
    for p in paths:
        path = Path(p)
        if not path.exists():
            raise ValueError(f"File not found: {p}")

    results: list[PreparedFile] = []

    for p in paths:
        path = Path(p).resolve()

        if path.is_dir():
            zip_bytes = _zip_directory(path)
            label = f"{path.name}/ ({_format_size(len(zip_bytes))})"
        elif _is_zip_file(path):
            zip_bytes = path.read_bytes()
            label = f"{path.name} ({_format_size(len(zip_bytes))})"
        else:
            zip_bytes = _zip_single_file(path)
            label = f"{path.name} ({_format_size(len(zip_bytes))})"

        if len(zip_bytes) > MAX_UPLOAD_SIZE:
            raise ValueError(
                f"{p}: archive too large ({_format_size(len(zip_bytes))}, max 50MB)."
            )

        results.append(PreparedFile(
            label=label,
            zip_bytes=zip_bytes,
            source_path=str(path),
        ))

    return results


def _zip_directory(dir_path: Path) -> bytes:
    """Create ZIP from directory, excluding default patterns."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(dir_path.rglob("*")):
            if not file_path.is_file():
                continue

            # Check if any path component is in the exclude set
            rel = file_path.relative_to(dir_path)
            if any(part in DEFAULT_EXCLUDES for part in rel.parts):
                continue

            # Check extension
            if file_path.suffix in DEFAULT_EXCLUDE_EXTENSIONS:
                continue

            zf.write(file_path, str(rel))

    return buf.getvalue()


def _zip_single_file(file_path: Path) -> bytes:
    """Wrap a single file in a ZIP."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(file_path, file_path.name)
    return buf.getvalue()


def _is_zip_file(path: Path) -> bool:
    """Check if file is a ZIP (magic bytes)."""
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
        return magic[:2] == b"PK"
    except OSError:
        return False


def _format_size(size_bytes: int) -> str:
    """Quick size formatter for labels."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"
