"""Argparse command hierarchy and command handlers for the CCAS client."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from typing import Any

from ..api import JobApiClient, ApiError
from .files import prepare_files
from ..formatters import Console
from .output import save_results
from .wait import wait_for_job


# =============================================================================
# Key resolution
# =============================================================================

def _resolve_ccas_key(args, console: Console) -> str:
    """Resolve CCAS client API key: flag > env > interactive prompt."""
    key = getattr(args, "key", None)
    if key:
        console.warning(
            "API key visible in process list. Prefer CCAS_CLIENT_API_KEY env var."
        )
        return key

    key = os.environ.get("CCAS_CLIENT_API_KEY")
    if key:
        return key

    # Interactive prompt
    if not sys.stdin.isatty():
        console.error("CCAS_CLIENT_API_KEY not set and stdin is not a TTY.")
        sys.exit(1)

    try:
        key = getpass.getpass("  CCAS API Key: ")
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        console.error("API key is required.")
        sys.exit(1)

    if not key.strip():
        console.error("API key is required.")
        sys.exit(1)

    return key.strip()


def _resolve_anthropic_key(args, console: Console) -> str:
    """Resolve Anthropic API key: flag > env > interactive prompt."""
    key = getattr(args, "anthropic_key", None)
    if key:
        console.warning(
            "Anthropic key visible in process list. Prefer ANTHROPIC_API_KEY env var."
        )
        return key

    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key

    # Interactive prompt
    if not sys.stdin.isatty():
        console.error("ANTHROPIC_API_KEY not set and stdin is not a TTY.")
        sys.exit(1)

    try:
        key = getpass.getpass("  Anthropic API Key: ")
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        console.error("Anthropic API key is required.")
        sys.exit(1)

    if not key.strip():
        console.error("Anthropic API key is required.")
        sys.exit(1)

    return key.strip()


def _resolve_url(args, console: Console) -> str:
    """Resolve CCAS server URL: flag > env. Exits if not set."""
    url = getattr(args, "url", None) or os.environ.get("CCAS_URL") or ""
    if not url.strip():
        console.error(
            "CCAS server URL not set. "
            "Use --url flag or set CCAS_URL environment variable."
        )
        sys.exit(1)
    return url.strip().rstrip("/")


# =============================================================================
# Command: run
# =============================================================================

def cmd_run(args, console: Console) -> None:
    """Submit a job, optionally wait for results."""
    # 1. Resolve prompt
    prompt = _resolve_prompt(args, console)

    # 2. Resolve keys
    url = _resolve_url(args, console)
    ccas_key = _resolve_ccas_key(args, console)
    anthropic_key = _resolve_anthropic_key(args, console)

    api = JobApiClient(url, ccas_key)

    # 3. Prepare and upload files
    upload_ids: list[str] = []
    files_arg = getattr(args, "files", None)
    if files_arg:
        console.step("Preparing files...")
        try:
            prepared = prepare_files(files_arg)
        except ValueError as e:
            console.error(str(e))
            sys.exit(1)

        for i, pf in enumerate(prepared):
            console.progress(i + 1, len(prepared), f"Uploading {pf.label}")
            try:
                upload_id = api.upload(pf.zip_bytes)
                upload_ids.append(upload_id)
            except ApiError as e:
                console.error(f"Upload failed for {pf.source_path}: {e.detail}")
                sys.exit(1)

        console.success(f"Uploaded {len(upload_ids)} archive(s)")

    # 4. Read CLAUDE.md if provided
    claude_md = None
    claude_md_path = getattr(args, "claude_md", None)
    if claude_md_path:
        try:
            with open(claude_md_path, "r", encoding="utf-8") as f:
                claude_md = f.read()
        except OSError as e:
            console.error(f"Cannot read CLAUDE.md: {e}")
            sys.exit(1)

    # 5. Create job
    console.step("Creating job...")
    try:
        job_data = api.create_job(
            prompt=prompt,
            anthropic_key=anthropic_key,
            upload_ids=upload_ids or None,
            agent=getattr(args, "agent", None),
            model=getattr(args, "model", None),
            timeout_seconds=getattr(args, "timeout", None),
            claude_md=claude_md,
        )
    except ApiError as e:
        console.error(f"Job creation failed: {e.detail}")
        sys.exit(1)

    job_id = job_data["job_id"]

    # 6. Print job_id (always — allows Ctrl+C resume)
    if args.json:
        if getattr(args, "no_wait", False):
            console.json_output(job_data)
            return
    else:
        console.success(f"Job created: {job_id}")

    # 7. No-wait mode
    if getattr(args, "no_wait", False):
        if not args.json:
            console.detail(f"Fetch later: ccas-client fetch {job_id}")
        return

    # 8. Wait for results
    console.step("Waiting for job to complete...")
    try:
        result = wait_for_job(
            api=api,
            job_id=job_id,
            console=console,
            max_wait=getattr(args, "max_wait", None),
        )
    except TimeoutError as e:
        if args.json:
            console.json_output({"job_id": job_id, "status": "RUNNING", "error": str(e)})
        else:
            console.warning(str(e))
        sys.exit(2)
    except KeyboardInterrupt:
        console.spinner_clear()
        console.error(f"Interrupted. Fetch results later: ccas-client fetch {job_id}")
        sys.exit(130)

    # 9. Handle result
    _handle_result(result, args, console)


# =============================================================================
# Command: fetch
# =============================================================================

def cmd_fetch(args, console: Console) -> None:
    """Fetch results of a previous job."""
    url = _resolve_url(args, console)
    ccas_key = _resolve_ccas_key(args, console)
    api = JobApiClient(url, ccas_key)
    job_id = args.job_id

    console.step(f"Fetching job {job_id}...")
    try:
        result = api.get_job(job_id)
    except ApiError as e:
        if e.status_code == 404:
            console.error(f"Job '{job_id}' not found.")
        else:
            console.error(f"Fetch failed: {e.detail}")
        sys.exit(1)

    status = result.get("status", "UNKNOWN")

    # Still running?
    if status in ("PENDING", "RUNNING"):
        if args.json:
            console.json_output(result)
        else:
            console.info(f"Job {job_id} is still {status}.")
            console.detail("Wait for completion or poll again later.")
        return

    _handle_result(result, args, console)


# =============================================================================
# Command: status
# =============================================================================

def cmd_status(args, console: Console) -> None:
    """Quick status check for a job."""
    url = _resolve_url(args, console)
    ccas_key = _resolve_ccas_key(args, console)
    api = JobApiClient(url, ccas_key)
    job_id = args.job_id

    try:
        result = api.get_job(job_id)
    except ApiError as e:
        if e.status_code == 404:
            console.error(f"Job '{job_id}' not found.")
        else:
            console.error(f"Status check failed: {e.detail}")
        sys.exit(1)

    if args.json:
        console.json_output(result)
        return

    status = result.get("status", "UNKNOWN")
    console.blank()
    console.info(f"  {console.bold('job_id')}:      {result.get('job_id', job_id)}")
    console.info(f"  {console.bold('status')}:      {_status_colored(console, status)}")
    console.info(f"  {console.bold('created_at')}:  {result.get('created_at', '')}")

    started = result.get("started_at")
    if started:
        console.info(f"  {console.bold('started_at')}:  {started}")

    completed = result.get("completed_at")
    if completed:
        console.info(f"  {console.bold('completed_at')}: {completed}")

    duration = result.get("duration_ms")
    if duration is not None:
        console.info(f"  {console.bold('duration')}:    {Console.format_duration(duration / 1000)}")

    cost = result.get("cost_usd")
    if cost is not None:
        console.info(f"  {console.bold('cost')}:        ${cost:.4f}")

    model = result.get("model")
    if model:
        console.info(f"  {console.bold('model')}:       {model}")

    agent = result.get("agent")
    if agent:
        console.info(f"  {console.bold('agent')}:       {agent}")

    error = result.get("error")
    if error:
        console.info(f"  {console.bold('error')}:       {console.red(error)}")

    console.blank()

    if status == "COMPLETED":
        console.detail(f"Results available. Use: ccas-client fetch {job_id}")


# =============================================================================
# Helpers
# =============================================================================

def _resolve_prompt(args, console: Console) -> str | None:
    """Resolve prompt from positional arg or --prompt-file. Returns None if neither given."""
    prompt_arg = getattr(args, "prompt", None)
    prompt_file = getattr(args, "prompt_file", None)

    if prompt_arg and prompt_file:
        console.error("Cannot use both positional prompt and --prompt-file.")
        sys.exit(1)

    if prompt_file:
        if prompt_file == "-":
            # Read from stdin — must be done before interactive prompts
            if sys.stdin.isatty():
                console.error("--prompt-file - expects piped input, but stdin is a TTY.")
                sys.exit(1)
            prompt = sys.stdin.read()
        else:
            try:
                with open(prompt_file, "r", encoding="utf-8") as f:
                    prompt = f.read()
            except OSError as e:
                console.error(f"Cannot read prompt file: {e}")
                sys.exit(1)

        if not prompt.strip():
            console.error("Prompt cannot be empty.")
            sys.exit(1)

        return prompt

    return prompt_arg  # str or None


def _handle_result(result: dict, args, console: Console) -> None:
    """Process and display a terminal job result."""
    status = result.get("status", "UNKNOWN")
    job_id = result.get("job_id", "unknown")

    if args.json:
        console.json_output(result)
        if status in ("FAILED", "TIMEOUT"):
            sys.exit(3)
        return

    # Save output files
    output_dir = getattr(args, "output_dir", ".")
    output = result.get("output")
    if output:
        save_dir = save_results(result, output_dir, job_id, console)

    # Print text output
    if output and output.get("text"):
        console.blank()
        print(output["text"])

    # Status-specific messaging
    if status == "COMPLETED":
        console.blank()
        cost = result.get("cost_usd")
        duration = result.get("duration_ms")
        parts = [f"Job {job_id} completed"]
        if duration is not None:
            parts.append(f"in {Console.format_duration(duration / 1000)}")
        if cost is not None:
            parts.append(f"(${cost:.4f})")
        console.success(" ".join(parts))

        files = output.get("files", {}) if output else {}
        if files:
            console.detail(f"Output files: {len(files)} saved to {save_dir}")

    elif status == "FAILED":
        error = result.get("error", "Unknown error")
        console.error(f"Job {job_id} failed: {error}")
        sys.exit(3)

    elif status == "TIMEOUT":
        console.error(f"Job {job_id} timed out on server.")
        sys.exit(3)


def _status_colored(console: Console, status: str) -> str:
    """Color a status string."""
    if status == "COMPLETED":
        return console.green(status)
    elif status in ("FAILED", "TIMEOUT"):
        return console.red(status)
    elif status == "RUNNING":
        return console.cyan(status)
    elif status == "PENDING":
        return console.yellow(status)
    return status


# =============================================================================
# Parser builder
# =============================================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ccas-client",
        description="CCAS Client — submit jobs to Claude Code API Server",
    )

    # Global flags
    parser.add_argument("--url", default=None,
                        help="CCAS server URL (env: CCAS_URL). Required.")
    parser.add_argument("--key", default=None,
                        help="CCAS client API key (env: CCAS_CLIENT_API_KEY)")
    parser.add_argument("--anthropic-key", default=None,
                        help="Anthropic API key (env: ANTHROPIC_API_KEY)")
    parser.add_argument("--json", action="store_true", default=False,
                        help="Machine-readable JSON output")
    parser.add_argument("--no-color", action="store_true", default=False,
                        help="Disable colored output")
    parser.add_argument("--verbose", "-v", action="store_true", default=False,
                        help="Verbose output")

    sub = parser.add_subparsers(dest="command")

    # --- run ---
    p_run = sub.add_parser("run", help="Submit a job")
    p_run.add_argument("prompt", nargs="?", default=None,
                       help="Task prompt (inline)")
    p_run.add_argument("--prompt-file", default=None, metavar="PATH",
                       help="Read prompt from file (use - for stdin)")
    p_run.add_argument("--files", nargs="+", metavar="PATH",
                       help="Files/dirs/zips to upload (max 5)")
    p_run.add_argument("--agent", default=None, metavar="NAME",
                       help="Agent context")
    p_run.add_argument("--model", default=None, metavar="NAME",
                       help="Model override")
    p_run.add_argument("--timeout", type=int, default=None, metavar="SECONDS",
                       help="Job timeout in seconds (60-7200)")
    p_run.add_argument("--claude-md", default=None, metavar="PATH",
                       help="CLAUDE.md file for agent configuration")
    p_run.add_argument("--no-wait", action="store_true", default=False,
                       help="Submit only, print job_id and exit")
    p_run.add_argument("--max-wait", type=int, default=None, metavar="SECONDS",
                       help="Maximum client-side wait time")
    p_run.add_argument("--output-dir", default=".", metavar="PATH",
                       help="Where to save results (default: current dir)")
    p_run.set_defaults(func=cmd_run)

    # --- fetch ---
    p_fetch = sub.add_parser("fetch", help="Fetch results of a previous job")
    p_fetch.add_argument("job_id", help="Job ID to fetch")
    p_fetch.add_argument("--output-dir", default=".", metavar="PATH",
                         help="Where to save results (default: current dir)")
    p_fetch.set_defaults(func=cmd_fetch)

    # --- status ---
    p_status = sub.add_parser("status", help="Check job status")
    p_status.add_argument("job_id", help="Job ID to check")
    p_status.set_defaults(func=cmd_status)

    return parser
