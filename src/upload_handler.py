"""
Upload handling module for Claude Code API Server.

Handles secure archive upload, validation, storage, and extraction.
Only ZIP format is supported for simplicity and security.
"""

import shutil
import uuid
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from .config import Settings, get_settings
from .logging_config import get_logger
from .models import UploadMeta, utcnow

logger = get_logger(__name__)


# ZIP file magic bytes
ZIP_MAGIC = b"PK"


class UploadError(Exception):
    """Base exception for upload-related errors."""

    pass


class InvalidArchiveError(UploadError):
    """Raised when the archive is invalid or unsupported."""

    pass


class ArchiveTooLargeError(UploadError):
    """Raised when the archive exceeds size limits."""

    pass


class TooManyFilesError(UploadError):
    """Raised when the archive contains too many files."""

    pass


class PathTraversalError(UploadError):
    """Raised when path traversal is detected."""

    pass


class UploadOwnershipError(UploadError):
    """Raised when a client tries to use another client's upload."""

    pass


class ExtractionError(UploadError):
    """Raised when archive extraction fails."""

    pass


class UploadManager:
    """
    Manages file uploads with secure storage and extraction.

    Uploads are stored with server-generated UUIDs and automatically
    expire after the configured TTL.
    """

    def __init__(self, settings: Settings | None = None):
        """
        Initialize the upload manager.

        Args:
            settings: Application settings (uses get_settings() if not provided)
        """
        if settings is None:
            settings = get_settings()

        self._settings = settings
        self._uploads_dir = settings.uploads_dir

        # Ensure uploads directory exists
        self._uploads_dir.mkdir(parents=True, exist_ok=True)

    def save_upload(
        self,
        file_content: BinaryIO | bytes,
        original_filename: str | None = None,
        content_type: str | None = None,
        client_id: str | None = None,
    ) -> UploadMeta:
        """
        Save an uploaded archive.

        Args:
            file_content: File content (file-like object or bytes)
            original_filename: Original filename (for logging only)
            content_type: Content-Type header (not trusted, for logging only)

        Returns:
            UploadMeta with upload details

        Raises:
            InvalidArchiveError: If the file is not a valid ZIP
            ArchiveTooLargeError: If the file exceeds size limits
        """
        # Generate unique ID
        upload_id = str(uuid.uuid4())

        # Create upload directory
        upload_dir = self._uploads_dir / upload_id
        upload_dir.mkdir(parents=True, exist_ok=True)

        archive_path = upload_dir / "archive.zip"

        try:
            # Read content if it's a file-like object
            if hasattr(file_content, "read"):
                content = file_content.read()
            else:
                content = file_content

            # Check size limit
            if len(content) > self._settings.max_upload_size_bytes:
                raise ArchiveTooLargeError(
                    f"Upload exceeds maximum size of {self._settings.max_upload_size_mb}MB"
                )

            # Validate ZIP format
            if not self._is_valid_zip(content):
                raise InvalidArchiveError("File is not a valid ZIP archive")

            # Validate archive contents without extracting
            self._validate_archive_contents(content)

            # Save the file
            archive_path.write_bytes(content)

            # Create metadata
            now = utcnow()
            meta = UploadMeta(
                upload_id=upload_id,
                client_id=client_id,
                created_at=now,
                expires_at=now + timedelta(seconds=self._settings.upload_ttl_seconds),
                size_bytes=len(content),
                original_filename=original_filename,
                content_type=content_type,
            )

            # Save metadata
            meta_path = upload_dir / "meta.json"
            meta_path.write_text(meta.model_dump_json(indent=2), encoding="utf-8")

            logger.info(
                "upload_saved",
                upload_id=upload_id,
                size_bytes=len(content),
                original_filename=original_filename,
            )

            return meta

        except UploadError:
            # Clean up on error
            self._cleanup_upload_dir(upload_dir)
            raise
        except Exception as e:
            # Clean up on unexpected error
            self._cleanup_upload_dir(upload_dir)
            logger.error("upload_save_failed", error=str(e))
            raise UploadError(f"Failed to save upload: {e}") from e

    def get_upload(self, upload_id: str) -> UploadMeta | None:
        """
        Get upload metadata by ID.

        Args:
            upload_id: Upload identifier

        Returns:
            UploadMeta if found and not expired, None otherwise
        """
        # Validate upload_id format to prevent path traversal
        try:
            uuid.UUID(upload_id)
        except ValueError:
            logger.warning("invalid_upload_id_format", upload_id=upload_id)
            return None

        upload_dir = self._uploads_dir / upload_id
        meta_path = upload_dir / "meta.json"

        if not meta_path.exists():
            return None

        try:
            meta = UploadMeta.model_validate_json(
                meta_path.read_text(encoding="utf-8")
            )

            if meta.is_expired():
                logger.debug("upload_expired", upload_id=upload_id)
                return None

            return meta
        except Exception as e:
            logger.error("upload_meta_read_failed", upload_id=upload_id, error=str(e))
            return None

    def extract_to(self, upload_id: str, dest_dir: Path) -> int:
        """
        Extract an upload's archive to a destination directory.

        If all files in the archive share a single common root directory,
        that root directory is stripped so files are extracted directly
        into dest_dir. For example, an archive containing
        ``project-main/src/app.py`` extracts to ``dest_dir/src/app.py``.

        Args:
            upload_id: Upload identifier
            dest_dir: Destination directory for extraction

        Returns:
            Number of files extracted

        Raises:
            UploadError: If upload not found
            ExtractionError: If extraction fails
        """
        # Validate upload_id
        try:
            uuid.UUID(upload_id)
        except ValueError:
            raise UploadError(f"Invalid upload ID format: {upload_id}")

        upload_dir = self._uploads_dir / upload_id
        archive_path = upload_dir / "archive.zip"

        if not archive_path.exists():
            raise UploadError(f"Upload not found: {upload_id}")

        # Ensure destination exists
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_dir_resolved = dest_dir.resolve()

        extracted_count = 0

        try:
            with zipfile.ZipFile(archive_path, "r") as zf:
                # Detect single root directory to strip
                strip_prefix = self._detect_single_root(zf)

                if strip_prefix:
                    logger.info(
                        "stripping_archive_root_dir",
                        upload_id=upload_id,
                        root_dir=strip_prefix,
                    )

                for member in zf.infolist():
                    # Skip directories (they'll be created automatically)
                    if member.is_dir():
                        continue

                    # Skip empty filenames
                    if not member.filename:
                        continue

                    # Strip the common root directory prefix
                    relative_name = member.filename
                    if strip_prefix and relative_name.startswith(strip_prefix):
                        relative_name = relative_name[len(strip_prefix):]

                    # Skip if stripping resulted in empty name (the dir entry itself)
                    if not relative_name:
                        continue

                    # Validate path (no traversal)
                    if not self._is_safe_path(relative_name, dest_dir_resolved):
                        raise PathTraversalError(
                            f"Path traversal detected: {member.filename}"
                        )

                    # Determine target path
                    target_path = dest_dir / relative_name

                    # Create parent directories
                    target_path.parent.mkdir(parents=True, exist_ok=True)

                    # Extract file
                    with zf.open(member) as source:
                        with open(target_path, "wb") as target:
                            # Read in chunks to handle large files
                            while True:
                                chunk = source.read(65536)
                                if not chunk:
                                    break
                                target.write(chunk)

                    extracted_count += 1

            logger.info(
                "archive_extracted",
                upload_id=upload_id,
                dest_dir=str(dest_dir),
                file_count=extracted_count,
            )

            return extracted_count

        except PathTraversalError:
            # Clean up partial extraction
            self._cleanup_extraction(dest_dir)
            raise
        except zipfile.BadZipFile as e:
            self._cleanup_extraction(dest_dir)
            raise ExtractionError(f"Corrupted ZIP file: {e}") from e
        except Exception as e:
            self._cleanup_extraction(dest_dir)
            raise ExtractionError(f"Extraction failed: {e}") from e

    @staticmethod
    def _detect_single_root(zf: zipfile.ZipFile) -> str | None:
        """
        Detect if all entries in a ZIP share a single root directory.

        Returns the root directory prefix (with trailing slash) to strip,
        or None if no stripping is needed.

        Examples:
            - ["proj/src/a.py", "proj/README.md"] -> "proj/"
            - ["src/a.py", "README.md"]           -> None (no common root)
            - ["d1/a.py", "d2/b.py"]              -> None (multiple roots)
        """
        names = [
            n.replace("\\", "/")
            for n in zf.namelist()
            if n and not n.endswith("/")  # Skip directory entries
        ]

        if not names:
            return None

        # Get the first path component of each file
        roots = set()
        for name in names:
            parts = name.split("/", 1)
            if len(parts) < 2:
                # File at the archive root -> no single root directory
                return None
            roots.add(parts[0])

        # All files share exactly one root directory
        if len(roots) == 1:
            root = roots.pop()
            return root + "/"

        return None

    def delete_upload(self, upload_id: str) -> bool:
        """
        Delete an upload.

        Args:
            upload_id: Upload identifier

        Returns:
            True if deleted, False if not found
        """
        try:
            uuid.UUID(upload_id)
        except ValueError:
            return False

        upload_dir = self._uploads_dir / upload_id

        if not upload_dir.exists():
            return False

        self._cleanup_upload_dir(upload_dir)
        logger.info("upload_deleted", upload_id=upload_id)
        return True

    def list_expired_uploads(self) -> list[str]:
        """
        List all expired upload IDs.

        Returns:
            List of expired upload IDs
        """
        expired = []

        for upload_dir in self._uploads_dir.iterdir():
            if not upload_dir.is_dir():
                continue

            meta_path = upload_dir / "meta.json"
            if not meta_path.exists():
                # No metadata, consider it orphaned
                expired.append(upload_dir.name)
                continue

            try:
                meta = UploadMeta.model_validate_json(
                    meta_path.read_text(encoding="utf-8")
                )
                if meta.is_expired():
                    expired.append(meta.upload_id)
            except Exception:
                # Corrupted metadata, consider it expired
                expired.append(upload_dir.name)

        return expired

    def cleanup_expired(self) -> int:
        """
        Delete all expired uploads.

        Returns:
            Number of uploads deleted
        """
        expired = self.list_expired_uploads()
        deleted_count = 0

        for upload_id in expired:
            if self.delete_upload(upload_id):
                deleted_count += 1

        if deleted_count > 0:
            logger.info("expired_uploads_cleaned", count=deleted_count)

        return deleted_count

    def count_pending(self) -> int:
        """
        Count non-expired pending uploads.

        Returns:
            Number of pending uploads
        """
        count = 0

        for upload_dir in self._uploads_dir.iterdir():
            if not upload_dir.is_dir():
                continue

            meta_path = upload_dir / "meta.json"
            if not meta_path.exists():
                continue

            try:
                meta = UploadMeta.model_validate_json(
                    meta_path.read_text(encoding="utf-8")
                )
                if not meta.is_expired():
                    count += 1
            except Exception:
                pass

        return count

    # =========================================================================
    # Private Methods
    # =========================================================================

    def _is_valid_zip(self, content: bytes) -> bool:
        """Check if content is a valid ZIP file."""
        if len(content) < 4:
            return False

        # Check magic bytes
        if not content.startswith(ZIP_MAGIC):
            return False

        # Try to open as ZIP
        try:
            import io
            with zipfile.ZipFile(io.BytesIO(content), "r") as zf:
                # Check if we can read the file list
                _ = zf.namelist()
                return True
        except (zipfile.BadZipFile, Exception):
            return False

    def _validate_archive_contents(self, content: bytes) -> None:
        """
        Validate archive contents without extracting.

        Checks:
        - Total uncompressed size (zip bomb protection)
        - File count
        - Path traversal in filenames

        Raises:
            ArchiveTooLargeError: If extracted size would exceed limit
            TooManyFilesError: If too many files
            PathTraversalError: If path traversal detected
        """
        import io

        with zipfile.ZipFile(io.BytesIO(content), "r") as zf:
            info_list = zf.infolist()

            # Check file count
            file_count = sum(1 for info in info_list if not info.is_dir())
            if file_count > self._settings.max_files_per_archive:
                raise TooManyFilesError(
                    f"Archive contains {file_count} files, "
                    f"maximum is {self._settings.max_files_per_archive}"
                )

            # Check total uncompressed size
            total_size = sum(info.file_size for info in info_list)
            if total_size > self._settings.max_extracted_size_bytes:
                raise ArchiveTooLargeError(
                    f"Archive would extract to {total_size / (1024*1024):.1f}MB, "
                    f"maximum is {self._settings.max_extracted_size_mb}MB"
                )

            # Check for path traversal
            for info in info_list:
                if self._has_path_traversal(info.filename):
                    raise PathTraversalError(
                        f"Path traversal detected in archive: {info.filename}"
                    )

    def _has_path_traversal(self, filename: str) -> bool:
        """Check if a filename contains path traversal."""
        # Normalize path separators
        normalized = filename.replace("\\", "/")

        # Check for parent directory references
        if ".." in normalized:
            return True

        # Check for absolute paths
        if normalized.startswith("/"):
            return True

        # Check for Windows absolute paths
        if len(normalized) >= 2 and normalized[1] == ":":
            return True

        return False

    def _is_safe_path(self, filename: str, dest_dir: Path) -> bool:
        """
        Check if extracting filename to dest_dir is safe.

        Args:
            filename: Filename from archive
            dest_dir: Resolved destination directory

        Returns:
            True if safe, False if path traversal detected
        """
        try:
            # Construct and resolve target path
            target = (dest_dir / filename).resolve()

            # Verify it's under dest_dir
            return target.is_relative_to(dest_dir)
        except (ValueError, OSError):
            return False

    def _cleanup_upload_dir(self, upload_dir: Path) -> None:
        """Safely remove an upload directory."""
        try:
            if upload_dir.exists():
                shutil.rmtree(upload_dir)
        except Exception as e:
            logger.error("cleanup_failed", path=str(upload_dir), error=str(e))

    def _cleanup_extraction(self, dest_dir: Path) -> None:
        """Clean up a partial extraction."""
        try:
            if dest_dir.exists():
                shutil.rmtree(dest_dir)
        except Exception as e:
            logger.error("extraction_cleanup_failed", path=str(dest_dir), error=str(e))


# =============================================================================
# Singleton Instance
# =============================================================================

_upload_manager: UploadManager | None = None


def get_upload_manager(settings: Settings | None = None) -> UploadManager:
    """
    Get the singleton UploadManager instance.

    Args:
        settings: Optional settings (uses get_settings() if not provided)

    Returns:
        UploadManager instance
    """
    global _upload_manager
    if _upload_manager is None:
        _upload_manager = UploadManager(settings)
    return _upload_manager


def reset_upload_manager() -> None:
    """Reset the upload manager (for testing)."""
    global _upload_manager
    _upload_manager = None
