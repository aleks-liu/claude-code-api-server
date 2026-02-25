"""Configuration resolution for the CCAS client."""

from __future__ import annotations

import getpass
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from ..formatters import Console


CONFIG_DIR = Path.home() / ".config" / "ccas"
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_LOCAL_CLAUDE_PATH = str(Path.home() / ".claude")


@dataclass
class Config:
    url: str
    admin_key: str
    local_claude_path: str
    json_mode: bool = False
    no_color: bool = False
    verbose: bool = False
    dry_run: bool = False
    auto_confirm: bool = False


def load_config_file() -> dict:
    """Load config file, returning empty dict if absent or invalid."""
    if not CONFIG_FILE.is_file():
        return {}
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_config_file(data: dict) -> None:
    """Save config data to the config file."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def resolve_config(args) -> Config:
    """Resolve configuration from CLI args, env vars, and config file.

    Priority: CLI flag > env var > config file > default/error
    """
    file_config = load_config_file()

    # URL
    url = getattr(args, "url", None) or os.environ.get("CCAS_URL") or file_config.get("url") or ""
    url = url.rstrip("/")

    # Admin key
    admin_key = getattr(args, "key", None) or os.environ.get("CCAS_ADMIN_API_KEY") or ""

    # Local Claude path
    local_claude_path = (
        file_config.get("local_claude_path") or DEFAULT_LOCAL_CLAUDE_PATH
    )
    local_claude_path = os.path.expanduser(local_claude_path)

    # Flags
    json_mode = getattr(args, "json", False)
    no_color = getattr(args, "no_color", False)
    verbose = getattr(args, "verbose", False)
    dry_run = getattr(args, "dry_run", False)
    auto_confirm = getattr(args, "yes", False)

    return Config(
        url=url,
        admin_key=admin_key,
        local_claude_path=local_claude_path,
        json_mode=json_mode,
        no_color=no_color,
        verbose=verbose,
        dry_run=dry_run,
        auto_confirm=auto_confirm,
    )


def ensure_url(config: Config, console: Console) -> str:
    """Ensure CCAS server URL is set, raising SystemExit if not."""
    if not config.url:
        console.error(
            "CCAS server URL not set. "
            "Use --url flag, set CCAS_URL env var, or run `ccas config init`."
        )
        sys.exit(1)
    return config.url


def ensure_key(config: Config, console: Console) -> str:
    """Ensure admin key is available, prompting interactively if needed."""
    if config.admin_key:
        # Warn if key was passed via --key flag
        if hasattr(sys, "_ccas_key_from_flag") and sys._ccas_key_from_flag:
            console.warning(
                "Passing key via --key flag is visible in process list. "
                "Prefer CCAS_ADMIN_API_KEY env var."
            )
        return config.admin_key

    # Interactive prompt
    try:
        key = getpass.getpass("  CCAS Admin API Key: ")
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        console.error("Admin API key is required.")
        sys.exit(1)

    if not key:
        console.error("Admin API key is required.")
        sys.exit(1)

    config.admin_key = key
    return key


# --- Config commands ---

def cmd_config_init(args, config: Config, console: Console) -> None:
    """Interactive config setup wizard."""
    import requests

    console.blank()
    console.info(console.bold("CCAS Client Configuration"))
    console.blank()

    # URL
    default_url = os.environ.get("CCAS_URL", "http://localhost:8000")
    try:
        url = input(f"  CCAS URL [{default_url}]: ").strip() or default_url
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        return

    url = url.rstrip("/")

    # Validate URL
    console.step(f"Checking {console.dim(url)}/v1/health ...")
    try:
        resp = requests.get(f"{url}/v1/health", timeout=10)
        if resp.status_code == 200:
            console.success("Server is reachable")
        else:
            console.warning(f"Server returned HTTP {resp.status_code}")
    except requests.ConnectionError:
        console.warning(f"Cannot connect to {url}. Saving anyway.")
    except requests.Timeout:
        console.warning("Connection timed out. Saving anyway.")

    # Local Claude path
    default_path = DEFAULT_LOCAL_CLAUDE_PATH
    try:
        local_path = input(f"  Local Claude Code path [{default_path}]: ").strip() or default_path
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        return

    local_path = os.path.expanduser(local_path)
    if not os.path.isdir(local_path):
        console.warning(f"Path does not exist: {local_path}")

    # Save
    save_config_file({"url": url, "local_claude_path": local_path})
    console.blank()
    console.success(f"Config saved to {CONFIG_FILE}")
    console.detail("Set CCAS_ADMIN_API_KEY env var in your shell profile for the API key.")
    console.blank()


def cmd_config_show(args, config: Config, console: Console) -> None:
    """Show effective configuration."""
    file_config = load_config_file()

    data = {
        "url": config.url or "(not set)",
        "admin_key": "***" if config.admin_key else "(not set — will prompt)",
        "local_claude_path": config.local_claude_path,
        "config_file": str(CONFIG_FILE),
        "config_file_exists": str(CONFIG_FILE.is_file()),
    }

    if config.json_mode:
        console.json_output(data)
        return

    console.blank()
    console.info(console.bold("Effective Configuration"))
    console.blank()

    source_url = "(flag)" if getattr(args, "url", None) else \
                 "(env)" if os.environ.get("CCAS_URL") else \
                 "(config)" if file_config.get("url") else "(not set)"
    source_key = "(flag)" if getattr(args, "key", None) else \
                 "(env)" if os.environ.get("CCAS_ADMIN_API_KEY") else "(prompt)"

    console.detail(f"url:              {config.url or '(not set)'} {console.dim(source_url)}")
    console.detail(f"admin_key:        {'***' if config.admin_key else '(not set)'} {console.dim(source_key)}")
    console.detail(f"local_claude_path: {config.local_claude_path}")
    console.detail(f"config_file:      {CONFIG_FILE}")
    console.blank()
