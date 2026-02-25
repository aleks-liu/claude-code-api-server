"""Polling loop with spinner for waiting on job completion."""

from __future__ import annotations

import time

from ..api import JobApiClient, ApiError
from ..formatters import Console

SPINNER_CHARS = "\u280b\u2819\u2839\u2838\u283c\u2834\u2826\u2827\u2807\u280f"

# Terminal states
_TERMINAL_STATUSES = {"COMPLETED", "FAILED", "TIMEOUT"}

# Maximum consecutive connection errors before aborting
_MAX_RETRIES = 3


def wait_for_job(
    api: JobApiClient,
    job_id: str,
    console: Console,
    initial_wait: int = 30,
    poll_interval: int = 10,
    max_wait: int | None = None,
) -> dict:
    """Poll until terminal state, showing spinner.

    Returns the full job result dict.
    Raises TimeoutError if max_wait exceeded.
    Raises KeyboardInterrupt on Ctrl+C (caller handles).
    """
    is_tty = console._is_tty
    start_time = time.monotonic()
    spinner_idx = 0
    consecutive_errors = 0

    # Initial wait phase: show spinner while waiting before first poll
    if is_tty:
        _spinner_wait(console, job_id, "PENDING", start_time, initial_wait, max_wait)
    else:
        console.status_line(f"Waiting for {job_id}...")
        time.sleep(initial_wait)

    # Check max_wait after initial wait
    if max_wait is not None and (time.monotonic() - start_time) >= max_wait:
        console.spinner_clear()
        raise TimeoutError(
            f"Wait timeout after {Console.format_duration(max_wait)}. "
            f"Job still running. Fetch later: ccas-client fetch {job_id}"
        )

    # Polling loop
    while True:
        try:
            result = api.get_job(job_id)
            consecutive_errors = 0
        except ApiError as e:
            consecutive_errors += 1
            if consecutive_errors >= _MAX_RETRIES:
                console.spinner_clear()
                raise ApiError(
                    e.status_code,
                    f"Lost connection to server after {_MAX_RETRIES} retries. "
                    f"Fetch later: ccas-client fetch {job_id}",
                )
            # Retry: wait poll_interval and try again
            if is_tty:
                elapsed = time.monotonic() - start_time
                _show_spinner_frame(
                    console, spinner_idx, job_id,
                    f"RETRYING ({consecutive_errors}/{_MAX_RETRIES})",
                    elapsed,
                )
                spinner_idx += 1
            time.sleep(poll_interval)
            continue

        status = result.get("status", "UNKNOWN")

        # Terminal?
        if status in _TERMINAL_STATUSES:
            console.spinner_clear()
            return result

        # Check max_wait
        elapsed = time.monotonic() - start_time
        if max_wait is not None and elapsed >= max_wait:
            console.spinner_clear()
            raise TimeoutError(
                f"Wait timeout after {Console.format_duration(max_wait)}. "
                f"Job still running. Fetch later: ccas-client fetch {job_id}"
            )

        # Show progress
        if is_tty:
            _spinner_wait(console, job_id, status, start_time, poll_interval, max_wait)
        else:
            console.status_line(
                f"Status: {status} ({Console.format_duration(elapsed)})"
            )
            time.sleep(poll_interval)


def _spinner_wait(
    console: Console,
    job_id: str,
    status: str,
    start_time: float,
    duration: int,
    max_wait: int | None,
) -> None:
    """Show spinner frames for a duration."""
    spinner_idx = 0
    wait_end = time.monotonic() + duration

    while time.monotonic() < wait_end:
        elapsed = time.monotonic() - start_time

        # Check max_wait
        if max_wait is not None and elapsed >= max_wait:
            return

        _show_spinner_frame(console, spinner_idx, job_id, status, elapsed)
        spinner_idx += 1
        time.sleep(0.1)


def _show_spinner_frame(
    console: Console,
    idx: int,
    job_id: str,
    status: str,
    elapsed: float,
) -> None:
    """Render one spinner frame."""
    char = SPINNER_CHARS[idx % len(SPINNER_CHARS)]
    duration_str = Console.format_duration(elapsed)
    console.spinner_frame(char, f"{job_id} | {status} | {duration_str}")
