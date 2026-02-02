"""Dependency check for unit tests that import application source code (src.*)."""

import importlib

import pytest

# Packages required to import any src.* module.
# These correspond to the main requirements.txt dependencies.
_REQUIRED_DEPS = (
    "structlog",
    "pydantic",
    "pydantic_settings",
    "aiofiles",
    "yaml",
    "fastapi",
    "claude_agent_sdk",
    "slowapi",
    "argon2",
    "cryptography",
)


def skip_if_deps_missing() -> None:
    """Call at module level to skip the entire test module if app deps are missing."""
    for dep in _REQUIRED_DEPS:
        try:
            importlib.import_module(dep)
        except ImportError:
            pytest.skip(
                f"Application dependency '{dep}' is not installed. "
                f"Unit tests that import from src.* require the main project "
                f"dependencies: pip install -r requirements.txt",
                allow_module_level=True,
            )
