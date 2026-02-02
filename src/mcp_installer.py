"""
MCP server package installation and uninstallation.

Shared logic for installing MCP servers from npm/pip, detecting entry
points, and deriving server names.  The admin API
(``admin_router.py``) delegates to this module.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .logging_config import get_logger
from .mcp_manager import McpManager, McpServerExistsError
from .models import McpServerEntry

logger = get_logger(__name__)


# =============================================================================
# Exceptions
# =============================================================================


class InstallError(Exception):
    """Raised when package installation fails."""

    pass


class UninstallError(Exception):
    """Raised when package uninstallation fails."""

    pass


# =============================================================================
# Package Manager Detection & Name Derivation
# =============================================================================


def detect_package_manager(package: str) -> str:
    """
    Detect the package manager from the package specification.

    Args:
        package: Package name or specifier.

    Returns:
        ``"pip"`` or ``"npm"``.
    """
    if package.startswith("pip://"):
        return "pip"
    if package.startswith("@") or "/" in package:
        return "npm"
    return "npm"


def derive_server_name(package: str, pkg_manager: str) -> str:
    """
    Derive a CLI-friendly server name from a package name.

    Strips scopes, common prefixes/suffixes, and replaces invalid
    characters with hyphens.

    Args:
        package: Package name.
        pkg_manager: ``"npm"`` or ``"pip"``.

    Returns:
        Derived server name.
    """
    name = package

    if name.startswith("pip://"):
        name = name[6:]

    # Strip npm scope: @scope/name -> name
    if name.startswith("@"):
        parts = name.split("/", 1)
        name = parts[1] if len(parts) > 1 else parts[0]

    for prefix in ("mcp-server-", "server-", "mcp-"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break

    for suffix in ("-mcp",):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break

    name = re.sub(r"[^a-zA-Z0-9-]", "-", name)
    name = re.sub(r"-+", "-", name).strip("-")

    return name or "mcp-server"


# =============================================================================
# Entry Point Detection
# =============================================================================


def find_npm_entry_point(npm_dir: Path, package: str) -> str | None:
    """
    Find the main entry point for an installed npm package.

    Checks ``package.json`` ``bin`` field first, then falls back to ``main``,
    then ``index.js``.

    Args:
        npm_dir: Path to the npm managed directory.
        package: Package name.

    Returns:
        Absolute path to the entry point, or ``None`` if not found.
    """
    node_modules = npm_dir / "node_modules"
    pkg_dir = node_modules / package

    if not pkg_dir.is_dir():
        return None

    pkg_json_path = pkg_dir / "package.json"
    if not pkg_json_path.is_file():
        return None

    try:
        pkg_json = json.loads(pkg_json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    bin_field = pkg_json.get("bin")
    if isinstance(bin_field, str):
        return str(pkg_dir / bin_field)
    elif isinstance(bin_field, dict):
        for _bin_name, bin_path in bin_field.items():
            return str(pkg_dir / bin_path)

    main = pkg_json.get("main")
    if main:
        return str(pkg_dir / main)

    index_js = pkg_dir / "index.js"
    if index_js.is_file():
        return str(index_js)

    return None


def find_pip_entry_point(
    venv_dir: Path, package: str, server_name: str
) -> str | None:
    """
    Find the entry point script for a pip-installed MCP package.

    Tries common naming patterns, then searches the venv ``bin/``
    for MCP-related executables.

    Args:
        venv_dir: Path to the Python virtual environment.
        package: Package name.
        server_name: Derived server name.

    Returns:
        Absolute path to the entry point, or ``None`` if not found.
    """
    bin_dir = venv_dir / "bin"

    if not bin_dir.is_dir():
        return None

    candidates = [
        package,
        server_name,
        f"mcp-server-{server_name}",
        f"{server_name}-mcp",
        f"mcp-{server_name}",
    ]

    # De-duplicate while preserving order
    seen: set[str] = set()
    unique_candidates: list[str] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            unique_candidates.append(c)

    for candidate in unique_candidates:
        script = bin_dir / candidate
        if script.is_file() and os.access(str(script), os.X_OK):
            return str(script)

    # Fallback: search for MCP-related executables
    for path in bin_dir.iterdir():
        if not path.is_file() or not os.access(str(path), os.X_OK):
            continue
        name_lower = path.name.lower()
        if "mcp" in name_lower or server_name.lower() in name_lower:
            return str(path)

    return None


# =============================================================================
# Install
# =============================================================================


def install_npm_package(
    mcp_manager: McpManager,
    mcp_dir: Path,
    package: str,
    server_name: str,
    description: str,
) -> McpServerEntry:
    """
    Install an npm MCP package and register it.

    Args:
        mcp_manager: MCP manager instance.
        mcp_dir: MCP data directory (e.g. ``/data/mcp/``).
        package: npm package name.
        server_name: Derived server name.
        description: Human-readable description.

    Returns:
        The created McpServerEntry.

    Raises:
        InstallError: If installation or registration fails.
        McpServerExistsError: If a server with the same name exists.
    """
    npm_dir = mcp_dir / "npm"
    npm_dir.mkdir(parents=True, exist_ok=True)

    npm_path = shutil.which("npm")
    if npm_path is None:
        raise InstallError(
            "npm is not installed or not in PATH. "
            "Install Node.js to use npm packages."
        )

    try:
        result = subprocess.run(
            [npm_path, "install", "--save", package],
            cwd=str(npm_dir),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        raise InstallError("npm install timed out after 120 seconds")
    except Exception as e:
        raise InstallError(f"npm install failed: {e}")

    if result.returncode != 0:
        raise InstallError(f"npm install failed: {result.stderr}")

    logger.info("npm_install_succeeded", package=package)

    entry_point = find_npm_entry_point(npm_dir, package)
    if entry_point is None:
        entry_point = str(npm_dir / "node_modules" / ".bin" / server_name)
        logger.warning(
            "npm_entry_point_not_found",
            package=package,
            fallback=entry_point,
        )

    config = {
        "command": "node",
        "args": [str(entry_point)],
    }

    entry = mcp_manager.add_server(
        name=server_name,
        config=config,
        description=description or f"Installed from npm: {package}",
        package_manager="npm",
        package=package,
    )

    return entry


def install_pip_package(
    mcp_manager: McpManager,
    mcp_dir: Path,
    package: str,
    server_name: str,
    description: str,
) -> McpServerEntry:
    """
    Install a pip MCP package into an isolated venv and register it.

    Args:
        mcp_manager: MCP manager instance.
        mcp_dir: MCP data directory (e.g. ``/data/mcp/``).
        package: pip package name.
        server_name: Derived server name.
        description: Human-readable description.

    Returns:
        The created McpServerEntry.

    Raises:
        InstallError: If installation or registration fails.
        McpServerExistsError: If a server with the same name exists.
    """
    venv_dir = mcp_dir / "venv"

    # Create venv if needed
    if not venv_dir.is_dir():
        try:
            subprocess.run(
                [sys.executable, "-m", "venv", str(venv_dir)],
                check=True,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.CalledProcessError as e:
            raise InstallError(f"venv creation failed: {e.stderr}")
        except subprocess.TimeoutExpired:
            raise InstallError("venv creation timed out")

    venv_pip = venv_dir / "bin" / "pip"
    if not venv_pip.is_file():
        raise InstallError(f"pip not found in venv at {venv_pip}")

    try:
        result = subprocess.run(
            [str(venv_pip), "install", package],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        raise InstallError("pip install timed out after 120 seconds")
    except Exception as e:
        raise InstallError(f"pip install failed: {e}")

    if result.returncode != 0:
        raise InstallError(f"pip install failed: {result.stderr}")

    logger.info("pip_install_succeeded", package=package)

    entry_point = find_pip_entry_point(venv_dir, package, server_name)
    if entry_point is None:
        entry_point = str(venv_dir / "bin" / server_name)
        logger.warning(
            "pip_entry_point_not_found",
            package=package,
            fallback=entry_point,
        )

    config = {
        "command": str(entry_point),
    }

    entry = mcp_manager.add_server(
        name=server_name,
        config=config,
        description=description or f"Installed from pip: {package}",
        package_manager="pip",
        package=package,
    )

    return entry


# =============================================================================
# Uninstall
# =============================================================================


def uninstall_package(mcp_dir: Path, entry: McpServerEntry) -> None:
    """
    Attempt to uninstall the package associated with a removed MCP server.

    Best-effort: logs warnings on failure but does not raise.

    Args:
        mcp_dir: MCP data directory.
        entry: The removed server entry (needs package_manager and package).
    """
    if not entry.package_manager or not entry.package:
        return

    if entry.package_manager == "npm":
        npm_dir = mcp_dir / "npm"
        npm_path = shutil.which("npm")
        if npm_path and npm_dir.is_dir():
            try:
                result = subprocess.run(
                    [npm_path, "uninstall", entry.package],
                    cwd=str(npm_dir),
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if result.returncode == 0:
                    logger.info("npm_uninstall_succeeded", package=entry.package)
                else:
                    logger.warning(
                        "npm_uninstall_failed",
                        package=entry.package,
                        stderr=result.stderr.strip(),
                    )
            except Exception as e:
                logger.warning(
                    "npm_uninstall_error",
                    package=entry.package,
                    error=str(e),
                )

    elif entry.package_manager == "pip":
        venv_pip = mcp_dir / "venv" / "bin" / "pip"
        if venv_pip.is_file():
            try:
                result = subprocess.run(
                    [str(venv_pip), "uninstall", "-y", entry.package],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if result.returncode == 0:
                    logger.info("pip_uninstall_succeeded", package=entry.package)
                else:
                    logger.warning(
                        "pip_uninstall_failed",
                        package=entry.package,
                        stderr=result.stderr.strip(),
                    )
            except Exception as e:
                logger.warning(
                    "pip_uninstall_error",
                    package=entry.package,
                    error=str(e),
                )
