"""
Job lifecycle management for Claude Code API Server.

Handles job creation, state transitions, persistence, and cleanup.
Jobs are stored on disk with in-memory caching for active jobs.
"""

import asyncio
import base64
import hashlib
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Settings, get_settings, validate_timeout
from .logging_config import get_logger, set_job_context
from .models import JobMeta, JobOutput, JobStatus, utcnow
from .upload_handler import get_upload_manager, UploadError, UploadOwnershipError

logger = get_logger(__name__)


class JobError(Exception):
    """Base exception for job-related errors."""

    pass


class JobNotFoundError(JobError):
    """Raised when a job is not found."""

    pass


class JobAccessDeniedError(JobError):
    """Raised when a client attempts to access a job they do not own."""

    pass


class TooManyPendingJobsError(JobError):
    """Raised when the pending job queue is full."""

    pass


class InvalidJobStateError(JobError):
    """Raised when a job is in an invalid state for the operation."""

    pass


class JobManager:
    """
    Manages job lifecycle, state, and persistence.

    Jobs are stored on disk in individual directories with metadata
    in status.json. An in-memory cache is maintained for fast access
    to active jobs.
    """

    def __init__(self, settings: Settings | None = None):
        """
        Initialize the job manager.

        Args:
            settings: Application settings (uses get_settings() if not provided)
        """
        if settings is None:
            settings = get_settings()

        self._settings = settings
        self._jobs_dir = settings.jobs_dir
        self._lock = asyncio.Lock()

        # In-memory cache for active jobs
        self._active_jobs: dict[str, JobMeta] = {}

        # Ensure jobs directory exists
        self._jobs_dir.mkdir(parents=True, exist_ok=True)

        # Load existing jobs on startup
        self._load_existing_jobs()

    def _load_existing_jobs(self) -> None:
        """Load existing jobs from disk on startup."""
        loaded_count = 0
        orphaned_count = 0

        for job_dir in self._jobs_dir.iterdir():
            if not job_dir.is_dir():
                continue

            status_path = job_dir / "status.json"
            if not status_path.exists():
                continue

            try:
                meta = JobMeta.model_validate_json(
                    status_path.read_text(encoding="utf-8")
                )

                # Handle orphaned RUNNING jobs (server crashed while running)
                if meta.status == JobStatus.RUNNING:
                    logger.warning(
                        "orphaned_running_job",
                        job_id=meta.job_id,
                    )
                    meta.status = JobStatus.FAILED
                    meta.error = "Server restarted during job execution"
                    meta.completed_at = utcnow()
                    self._save_job_meta(meta)
                    orphaned_count += 1

                # Cache non-completed jobs for quick access
                if meta.status in (JobStatus.PENDING, JobStatus.RUNNING):
                    self._active_jobs[meta.job_id] = meta

                loaded_count += 1

            except Exception as e:
                logger.error(
                    "job_load_failed",
                    job_id=job_dir.name,
                    error=str(e),
                )

        logger.info(
            "jobs_loaded",
            total=loaded_count,
            orphaned=orphaned_count,
            active=len(self._active_jobs),
        )

    async def create_job(
        self,
        upload_ids: list[str],
        prompt: str,
        client_id: str,
        claude_md: str | None = None,
        timeout_seconds: int | None = None,
        model: str | None = None,
        agent: str | None = None,
    ) -> JobMeta:
        """
        Create a new job, optionally from one or more uploaded archives.

        Args:
            upload_ids: IDs of uploaded archives (empty list for prompt-only jobs).
                        Archives are extracted in order; later uploads overwrite
                        earlier ones on file-path conflicts.
            prompt: Task description for Claude
            client_id: ID of the client creating this job (used for authorization)
            claude_md: Optional CLAUDE.md content
            timeout_seconds: Job timeout (uses default if not specified)
            model: Claude model to use (None uses server default from settings)
            agent: Name of a pre-configured agent to run this job as (None for default)

        Returns:
            JobMeta for the created job

        Raises:
            UploadError: If any upload is not found or extraction fails
            JobError: If job creation fails
        """
        job_id = f"job_{uuid.uuid4().hex[:12]}"
        set_job_context(job_id)

        # Validate and normalize timeout
        timeout = validate_timeout(timeout_seconds, self._settings)

        # Resolve model: use client-specified model or fall back to server default
        resolved_model = model if model is not None else self._settings.default_model
        if model is None:
            logger.debug(
                "model_defaulted",
                job_id=job_id,
                default_model=resolved_model,
            )
        else:
            logger.info(
                "model_specified_by_client",
                job_id=job_id,
                model=resolved_model,
            )

        async with self._lock:
            # Check pending job queue limit
            pending_count = sum(
                1 for j in self._active_jobs.values()
                if j.status == JobStatus.PENDING
            )
            if pending_count >= self._settings.max_pending_jobs:
                raise TooManyPendingJobsError(
                    f"Pending job queue is full ({pending_count}/{self._settings.max_pending_jobs}). "
                    "Try again later."
                )

            # Validate agent exists (fail-fast at creation time)
            if agent is not None:
                agent_file = self._settings.plugin_agents_dir / f"{agent}.md"
                if not agent_file.is_file():
                    raise JobError(
                        f"Agent '{agent}' not found. "
                        "Use the Admin API to add agents before referencing them in jobs."
                    )
                logger.info(
                    "agent_mode_requested",
                    job_id=job_id,
                    agent=agent,
                )

            # Create job directory structure
            job_dir = self._jobs_dir / job_id
            input_dir = job_dir / "input"
            output_dir = job_dir / "output"

            try:
                job_dir.mkdir(parents=True, exist_ok=True)
                input_dir.mkdir(parents=True, exist_ok=True)
                output_dir.mkdir(parents=True, exist_ok=True)

                # Extract archives if uploads were provided
                if upload_ids:
                    upload_manager = get_upload_manager()
                    total = len(upload_ids)

                    for idx, upload_id in enumerate(upload_ids, 1):
                        upload_meta = upload_manager.get_upload(upload_id)

                        if upload_meta is None:
                            raise UploadError(
                                f"Upload not found or expired: {upload_id}"
                                + (f" (upload {idx} of {total})" if total > 1 else "")
                            )

                        # Verify upload ownership (BOLA prevention)
                        if upload_meta.client_id is not None and upload_meta.client_id != client_id:
                            logger.warning(
                                "upload_ownership_mismatch",
                                upload_id=upload_id,
                                upload_owner=upload_meta.client_id,
                                requesting_client=client_id,
                            )
                            raise UploadOwnershipError(
                                f"Upload {upload_id} does not belong to client {client_id}"
                            )

                        file_count = upload_manager.extract_to(upload_id, input_dir)
                        logger.info(
                            "files_extracted",
                            job_id=job_id,
                            upload_id=upload_id,
                            upload_index=idx,
                            upload_total=total,
                            file_count=file_count,
                        )

                        # Delete the original archive (no longer needed)
                        upload_manager.delete_upload(upload_id)

                # Create job metadata
                now = utcnow()
                meta = JobMeta(
                    job_id=job_id,
                    client_id=client_id,
                    status=JobStatus.PENDING,
                    created_at=now,
                    upload_ids=upload_ids,
                    prompt=prompt,
                    claude_md=claude_md,
                    timeout_seconds=timeout,
                    model=resolved_model,
                    agent=agent,
                )

                # Save to disk and cache
                self._save_job_meta(meta)
                self._active_jobs[job_id] = meta

                logger.info(
                    "job_created",
                    job_id=job_id,
                    client_id=client_id,
                    model=resolved_model,
                    agent=agent,
                    timeout=timeout,
                    prompt_length=len(prompt),
                    upload_count=len(upload_ids),
                )

                return meta

            except Exception as e:
                # Clean up on failure
                self._cleanup_job_dir(job_dir)
                logger.error("job_creation_failed", job_id=job_id, error=str(e))
                raise JobError(f"Failed to create job: {e}") from e

    async def get_job(self, job_id: str) -> JobMeta | None:
        """
        Get job metadata by ID.

        Args:
            job_id: Job identifier

        Returns:
            JobMeta if found, None otherwise
        """
        # Check cache first
        if job_id in self._active_jobs:
            return self._active_jobs[job_id]

        # Load from disk
        status_path = self._jobs_dir / job_id / "status.json"
        if not status_path.exists():
            return None

        try:
            return JobMeta.model_validate_json(
                status_path.read_text(encoding="utf-8")
            )
        except Exception as e:
            logger.error("job_load_failed", job_id=job_id, error=str(e))
            return None

    async def get_job_for_client(
        self, job_id: str, client_id: str
    ) -> JobMeta | None:
        """
        Get job metadata with ownership authorization enforcement.

        Returns the job only if:
        - The job exists, AND
        - The job belongs to the requesting client (client_id matches)

        Legacy jobs without a recorded owner (client_id is None) are denied
        because ownership cannot be verified. Operators can manually set
        "client_id" in the job's status.json to restore access.

        For security, both "job not found" and "access denied" return None
        to prevent job ID enumeration attacks. The actual reason is always
        logged for auditing purposes.

        Args:
            job_id: Job identifier
            client_id: Requesting client's identifier

        Returns:
            JobMeta if found and authorized, None otherwise
        """
        # Defensive: validate inputs are non-empty
        if not job_id or not client_id:
            logger.warning(
                "job_access_check_invalid_params",
                job_id=job_id or "(empty)",
                client_id=client_id or "(empty)",
            )
            return None

        logger.debug(
            "job_ownership_check_started",
            job_id=job_id,
            client_id=client_id,
        )

        # Load the job metadata
        job_meta = await self.get_job(job_id)
        if job_meta is None:
            logger.debug(
                "job_access_not_found",
                job_id=job_id,
                requesting_client_id=client_id,
            )
            return None

        # --- Authorization check ---

        if job_meta.client_id is None:
            # Legacy job created before authorization was implemented.
            # Deny access because ownership cannot be verified.
            # Operators can manually set "client_id" in status.json
            # to restore access to specific legacy jobs.
            logger.warning(
                "job_access_denied_legacy_no_owner",
                job_id=job_id,
                requesting_client_id=client_id,
            )
            return None

        if job_meta.client_id == client_id:
            # Authorized: requesting client owns this job
            logger.debug(
                "job_access_authorized",
                job_id=job_id,
                client_id=client_id,
            )
            return job_meta

        # Access denied: the requesting client does not own this job.
        # Return None (not a 403) to prevent job ID enumeration.
        logger.warning(
            "job_access_denied",
            job_id=job_id,
            requesting_client_id=client_id,
        )
        logger.debug(
            "job_access_denied_detail",
            job_id=job_id,
            requesting_client_id=client_id,
            owning_client_id=job_meta.client_id,
        )
        return None

    async def mark_running(self, job_id: str) -> JobMeta:
        """
        Mark a job as running.

        Args:
            job_id: Job identifier

        Returns:
            Updated JobMeta

        Raises:
            JobNotFoundError: If job not found
            InvalidJobStateError: If job is not in PENDING state
        """
        async with self._lock:
            meta = await self.get_job(job_id)
            if meta is None:
                raise JobNotFoundError(f"Job not found: {job_id}")

            if meta.status != JobStatus.PENDING:
                raise InvalidJobStateError(
                    f"Job {job_id} is {meta.status}, expected PENDING"
                )

            meta.status = JobStatus.RUNNING
            meta.started_at = utcnow()

            self._save_job_meta(meta)
            self._active_jobs[job_id] = meta

            logger.info("job_started", job_id=job_id)
            return meta

    async def mark_completed(
        self,
        job_id: str,
        output_text: str,
        cost_usd: float | None = None,
        duration_ms: int | None = None,
        num_turns: int | None = None,
    ) -> JobMeta:
        """
        Mark a job as completed.

        Args:
            job_id: Job identifier
            output_text: Claude's text output
            cost_usd: Total API cost
            duration_ms: Total duration
            num_turns: Number of agent turns

        Returns:
            Updated JobMeta

        Raises:
            JobNotFoundError: If job not found
        """
        async with self._lock:
            meta = await self.get_job(job_id)
            if meta is None:
                raise JobNotFoundError(f"Job not found: {job_id}")

            meta.status = JobStatus.COMPLETED
            meta.completed_at = utcnow()
            meta.output_text = output_text
            meta.cost_usd = cost_usd
            meta.duration_ms = duration_ms
            meta.num_turns = num_turns

            # Calculate duration if not provided
            if meta.duration_ms is None and meta.started_at:
                delta = meta.completed_at - meta.started_at
                meta.duration_ms = int(delta.total_seconds() * 1000)

            self._save_job_meta(meta)

            # Save stdout
            self._save_stdout(job_id, output_text)

            # Remove from active cache
            self._active_jobs.pop(job_id, None)

            logger.info(
                "job_completed",
                job_id=job_id,
                duration_ms=meta.duration_ms,
                cost_usd=meta.cost_usd,
            )

            return meta

    async def mark_failed(
        self,
        job_id: str,
        error: str,
        output_text: str | None = None,
    ) -> JobMeta:
        """
        Mark a job as failed.

        Args:
            job_id: Job identifier
            error: Error message
            output_text: Partial output (if any)

        Returns:
            Updated JobMeta

        Raises:
            JobNotFoundError: If job not found
        """
        async with self._lock:
            meta = await self.get_job(job_id)
            if meta is None:
                raise JobNotFoundError(f"Job not found: {job_id}")

            meta.status = JobStatus.FAILED
            meta.completed_at = utcnow()
            meta.error = error
            meta.output_text = output_text

            if meta.started_at:
                delta = meta.completed_at - meta.started_at
                meta.duration_ms = int(delta.total_seconds() * 1000)

            self._save_job_meta(meta)

            if output_text:
                self._save_stdout(job_id, output_text)

            self._active_jobs.pop(job_id, None)

            logger.error("job_failed", job_id=job_id, error=error)
            return meta

    async def mark_timeout(
        self,
        job_id: str,
        output_text: str | None = None,
    ) -> JobMeta:
        """
        Mark a job as timed out.

        Args:
            job_id: Job identifier
            output_text: Partial output (if any)

        Returns:
            Updated JobMeta

        Raises:
            JobNotFoundError: If job not found
        """
        async with self._lock:
            meta = await self.get_job(job_id)
            if meta is None:
                raise JobNotFoundError(f"Job not found: {job_id}")

            meta.status = JobStatus.TIMEOUT
            meta.completed_at = utcnow()
            meta.error = f"Job exceeded timeout of {meta.timeout_seconds} seconds"
            meta.output_text = output_text

            if meta.started_at:
                delta = meta.completed_at - meta.started_at
                meta.duration_ms = int(delta.total_seconds() * 1000)

            self._save_job_meta(meta)

            if output_text:
                self._save_stdout(job_id, output_text)

            self._active_jobs.pop(job_id, None)

            logger.warning("job_timeout", job_id=job_id)
            return meta

    async def get_output(self, job_id: str) -> JobOutput | None:
        """
        Get job output including text and output files.

        Args:
            job_id: Job identifier

        Returns:
            JobOutput if job exists and has output, None otherwise
        """
        meta = await self.get_job(job_id)
        if meta is None:
            return None

        output_text = meta.output_text or ""

        # Try to read from stdout.txt if not in metadata
        if not output_text:
            stdout_path = self._jobs_dir / job_id / "stdout.txt"
            if stdout_path.exists():
                try:
                    output_text = stdout_path.read_text(encoding="utf-8")
                except Exception:
                    pass

        # Collect output files
        output_files = self._collect_output_files(job_id)

        return JobOutput(text=output_text, files=output_files)

    def _collect_output_files(self, job_id: str) -> dict[str, str]:
        """
        Collect and base64-encode files from the output directory.

        Args:
            job_id: Job identifier

        Returns:
            Dict mapping relative path to base64-encoded content
        """
        output_dir = self._jobs_dir / job_id / "output"
        if not output_dir.exists():
            return {}

        files: dict[str, str] = {}

        for file_path in output_dir.rglob("*"):
            if not file_path.is_file():
                continue

            # Get relative path
            rel_path = file_path.relative_to(output_dir)
            rel_path_str = str(rel_path).replace("\\", "/")

            try:
                # Read and encode file
                content = file_path.read_bytes()
                encoded = base64.b64encode(content).decode("ascii")
                files[rel_path_str] = encoded
            except Exception as e:
                logger.warning(
                    "output_file_read_failed",
                    job_id=job_id,
                    file=rel_path_str,
                    error=str(e),
                )

        return files

    async def cleanup_input(self, job_id: str) -> bool:
        """
        Delete input files for a completed job.

        Args:
            job_id: Job identifier

        Returns:
            True if cleaned up, False if job not found or already cleaned
        """
        input_dir = self._jobs_dir / job_id / "input"
        if not input_dir.exists():
            return False

        try:
            shutil.rmtree(input_dir)
            logger.info("input_cleaned", job_id=job_id)
            return True
        except Exception as e:
            logger.error("input_cleanup_failed", job_id=job_id, error=str(e))
            return False

    def get_job_dir(self, job_id: str) -> Path:
        """Get the directory path for a job."""
        return self._jobs_dir / job_id

    def get_input_dir(self, job_id: str) -> Path:
        """Get the input directory path for a job."""
        return self._jobs_dir / job_id / "input"

    def get_output_dir(self, job_id: str) -> Path:
        """Get the output directory path for a job."""
        return self._jobs_dir / job_id / "output"

    def count_active(self) -> int:
        """Count active (pending or running) jobs."""
        return len(self._active_jobs)

    def count_running(self) -> int:
        """Count currently running jobs."""
        return sum(
            1 for job in self._active_jobs.values()
            if job.status == JobStatus.RUNNING
        )

    async def list_jobs(
        self,
        status: JobStatus | None = None,
        limit: int = 100,
    ) -> list[JobMeta]:
        """
        List jobs, optionally filtered by status.

        Args:
            status: Filter by status (None for all)
            limit: Maximum number of jobs to return

        Returns:
            List of JobMeta objects
        """
        jobs: list[JobMeta] = []

        for job_dir in self._jobs_dir.iterdir():
            if not job_dir.is_dir():
                continue

            if len(jobs) >= limit:
                break

            status_path = job_dir / "status.json"
            if not status_path.exists():
                continue

            try:
                meta = JobMeta.model_validate_json(
                    status_path.read_text(encoding="utf-8")
                )

                if status is None or meta.status == status:
                    jobs.append(meta)

            except Exception:
                pass

        # Sort by created_at descending
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs[:limit]

    # =========================================================================
    # Output File Collection
    # =========================================================================

    def snapshot_input_files(self, job_id: str) -> dict[str, str]:
        """
        Create a snapshot of all files in the job's input directory.

        Records relative path → SHA-256 hash for every file, so that
        after Claude finishes we can detect which files are new or modified.

        Args:
            job_id: Job identifier

        Returns:
            Dict mapping relative file path to SHA-256 hex digest
        """
        input_dir = self.get_input_dir(job_id)
        snapshot: dict[str, str] = {}

        if not input_dir.exists():
            return snapshot

        for file_path in input_dir.rglob("*"):
            if not file_path.is_file():
                continue

            # Skip SDK metadata directories
            rel = file_path.relative_to(input_dir)
            rel_str = str(rel).replace("\\", "/")

            if self._should_exclude_from_snapshot(rel_str):
                continue

            file_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()
            snapshot[rel_str] = file_hash

        logger.debug(
            "input_snapshot_created",
            job_id=job_id,
            file_count=len(snapshot),
        )

        return snapshot

    def collect_output_files(
        self,
        job_id: str,
        snapshot: dict[str, str],
    ) -> int:
        """
        Collect new and modified files from input/ into output/.

        Compares the current state of input/ against the pre-execution
        snapshot and copies any new or modified files to output/,
        preserving directory structure.

        Args:
            job_id: Job identifier
            snapshot: Pre-execution snapshot from snapshot_input_files()

        Returns:
            Number of files collected
        """
        input_dir = self.get_input_dir(job_id)
        output_dir = self.get_output_dir(job_id)

        if not input_dir.exists():
            return 0

        output_dir.mkdir(parents=True, exist_ok=True)
        collected = 0

        for file_path in input_dir.rglob("*"):
            if not file_path.is_file():
                continue

            rel = file_path.relative_to(input_dir)
            rel_str = str(rel).replace("\\", "/")

            # Skip excluded patterns
            if self._should_exclude_from_snapshot(rel_str):
                continue

            # Compute current hash
            current_hash = hashlib.sha256(file_path.read_bytes()).hexdigest()

            # Check if file is new or modified
            original_hash = snapshot.get(rel_str)
            if original_hash == current_hash:
                continue  # Unchanged, skip

            # Copy to output directory
            dest_path = output_dir / rel_str
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file_path, dest_path)
            collected += 1

            if original_hash is None:
                logger.debug(
                    "new_file_collected",
                    job_id=job_id,
                    file=rel_str,
                )
            else:
                logger.debug(
                    "modified_file_collected",
                    job_id=job_id,
                    file=rel_str,
                )

        if collected > 0:
            logger.info(
                "output_files_collected",
                job_id=job_id,
                count=collected,
            )

        return collected

    @staticmethod
    def _should_exclude_from_snapshot(rel_path: str) -> bool:
        """Check if a file should be excluded from snapshot/collection."""
        excluded_prefixes = (
            ".claude/",       # SDK metadata
            "__pycache__/",   # Python bytecode
            ".git/",          # Git metadata
            "node_modules/",  # npm packages
        )
        return rel_path.startswith(excluded_prefixes)

    # =========================================================================
    # Private Methods
    # =========================================================================

    def _save_job_meta(self, meta: JobMeta) -> None:
        """Save job metadata to disk."""
        status_path = self._jobs_dir / meta.job_id / "status.json"
        status_path.parent.mkdir(parents=True, exist_ok=True)
        status_path.write_text(
            meta.model_dump_json(indent=2),
            encoding="utf-8",
        )

    def _save_stdout(self, job_id: str, output: str) -> None:
        """Save Claude's text output to stdout.txt."""
        stdout_path = self._jobs_dir / job_id / "stdout.txt"
        try:
            stdout_path.write_text(output, encoding="utf-8")
        except Exception as e:
            logger.error("stdout_save_failed", job_id=job_id, error=str(e))

    def _cleanup_job_dir(self, job_dir: Path) -> None:
        """Clean up a job directory."""
        try:
            if job_dir.exists():
                shutil.rmtree(job_dir)
        except Exception as e:
            logger.error("job_dir_cleanup_failed", path=str(job_dir), error=str(e))


# =============================================================================
# Singleton Instance
# =============================================================================

_job_manager: JobManager | None = None


def get_job_manager(settings: Settings | None = None) -> JobManager:
    """
    Get the singleton JobManager instance.

    Args:
        settings: Optional settings (uses get_settings() if not provided)

    Returns:
        JobManager instance
    """
    global _job_manager
    if _job_manager is None:
        _job_manager = JobManager(settings)
    return _job_manager


def reset_job_manager() -> None:
    """Reset the job manager (for testing)."""
    global _job_manager
    _job_manager = None
