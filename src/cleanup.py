"""
Background cleanup tasks for Claude Code API Server.

Handles periodic cleanup of:
- Expired uploads (unused archives)
- Job input files (after completion)
"""

import asyncio
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from .config import Settings, get_settings
from .logging_config import get_logger
from .models import JobMeta, JobStatus, utcnow

logger = get_logger(__name__)


class CleanupTask:
    """
    Background task for periodic cleanup operations.

    Runs as an asyncio task and performs cleanup at configured intervals.
    """

    def __init__(self, settings: Settings | None = None):
        """
        Initialize the cleanup task.

        Args:
            settings: Application settings (uses get_settings() if not provided)
        """
        if settings is None:
            settings = get_settings()

        self._settings = settings
        self._shutdown_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._is_running = False

    def start(self) -> None:
        """Start the background cleanup task."""
        if self._is_running:
            logger.warning("cleanup_task_already_running")
            return

        self._shutdown_event.clear()
        self._task = asyncio.create_task(self._run_loop())
        self._is_running = True
        logger.info(
            "cleanup_task_started",
            interval_minutes=self._settings.cleanup_interval_minutes,
        )

    async def stop(self, timeout: float = 10.0) -> None:
        """
        Stop the cleanup task gracefully.

        Args:
            timeout: Maximum time to wait for task to finish
        """
        if not self._is_running or self._task is None:
            return

        logger.info("cleanup_task_stopping")
        self._shutdown_event.set()

        try:
            await asyncio.wait_for(self._task, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("cleanup_task_stop_timeout")
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        self._is_running = False
        self._task = None
        logger.info("cleanup_task_stopped")

    async def run_once(self) -> dict[str, int]:
        """
        Run cleanup operations once (for testing or manual trigger).

        Returns:
            Dict with counts of cleaned items
        """
        return await self._do_cleanup()

    async def _run_loop(self) -> None:
        """Main cleanup loop."""
        interval = self._settings.cleanup_interval_seconds

        while not self._shutdown_event.is_set():
            try:
                # Wait for interval or shutdown
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=interval,
                    )
                    # If we get here, shutdown was requested
                    break
                except asyncio.TimeoutError:
                    # Timeout means interval elapsed, do cleanup
                    pass

                # Perform cleanup
                await self._do_cleanup()

            except Exception as e:
                logger.error("cleanup_loop_error", error=str(e))
                # Don't crash the loop on errors
                await asyncio.sleep(60)  # Wait a bit before retrying

    async def _do_cleanup(self) -> dict[str, int]:
        """
        Perform all cleanup operations.

        Returns:
            Dict with counts of cleaned items
        """
        results = {
            "expired_uploads": 0,
            "job_inputs": 0,
        }

        try:
            results["expired_uploads"] = await self._cleanup_expired_uploads()
        except Exception as e:
            logger.error("upload_cleanup_error", error=str(e))

        try:
            results["job_inputs"] = await self._cleanup_job_inputs()
        except Exception as e:
            logger.error("job_input_cleanup_error", error=str(e))

        if results["expired_uploads"] > 0 or results["job_inputs"] > 0:
            logger.info(
                "cleanup_completed",
                expired_uploads=results["expired_uploads"],
                job_inputs=results["job_inputs"],
            )

        return results

    async def _cleanup_expired_uploads(self) -> int:
        """
        Delete expired uploads.

        Returns:
            Number of uploads deleted
        """
        from .upload_handler import get_upload_manager

        upload_manager = get_upload_manager(self._settings)
        return upload_manager.cleanup_expired()

    async def _cleanup_job_inputs(self) -> int:
        """
        Delete input files from completed jobs.

        Only deletes if job completed more than cleanup_delay ago.

        Returns:
            Number of job inputs deleted
        """
        jobs_dir = self._settings.jobs_dir
        cleanup_delay = timedelta(
            seconds=self._settings.job_input_cleanup_delay_seconds
        )
        now = utcnow()
        deleted_count = 0

        if not jobs_dir.exists():
            return 0

        for job_dir in jobs_dir.iterdir():
            if not job_dir.is_dir():
                continue

            input_dir = job_dir / "input"
            if not input_dir.exists():
                continue

            status_path = job_dir / "status.json"
            if not status_path.exists():
                continue

            try:
                meta = JobMeta.model_validate_json(
                    status_path.read_text(encoding="utf-8")
                )

                # Only cleanup completed/failed/timeout jobs
                if meta.status not in (
                    JobStatus.COMPLETED,
                    JobStatus.FAILED,
                    JobStatus.TIMEOUT,
                ):
                    continue

                # Check if enough time has passed
                if meta.completed_at is None:
                    continue

                age = now - meta.completed_at
                if age < cleanup_delay:
                    continue

                # Delete input directory
                try:
                    shutil.rmtree(input_dir)
                    deleted_count += 1
                    logger.debug(
                        "job_input_deleted",
                        job_id=meta.job_id,
                        age_minutes=int(age.total_seconds() / 60),
                    )
                except Exception as e:
                    logger.error(
                        "job_input_delete_failed",
                        job_id=meta.job_id,
                        error=str(e),
                    )

            except Exception as e:
                logger.debug(
                    "job_status_read_failed",
                    job_dir=job_dir.name,
                    error=str(e),
                )

        return deleted_count


# =============================================================================
# Singleton Instance
# =============================================================================

_cleanup_task: CleanupTask | None = None


def get_cleanup_task(settings: Settings | None = None) -> CleanupTask:
    """
    Get the singleton CleanupTask instance.

    Args:
        settings: Optional settings (uses get_settings() if not provided)

    Returns:
        CleanupTask instance
    """
    global _cleanup_task
    if _cleanup_task is None:
        _cleanup_task = CleanupTask(settings)
    return _cleanup_task


def reset_cleanup_task() -> None:
    """Reset the cleanup task (for testing)."""
    global _cleanup_task
    _cleanup_task = None


# =============================================================================
# Convenience Functions
# =============================================================================


def start_cleanup() -> None:
    """Start the background cleanup task."""
    task = get_cleanup_task()
    task.start()


async def stop_cleanup() -> None:
    """Stop the background cleanup task."""
    task = get_cleanup_task()
    await task.stop()


async def run_cleanup_now() -> dict[str, int]:
    """Run cleanup immediately (for testing or manual trigger)."""
    task = get_cleanup_task()
    return await task.run_once()
