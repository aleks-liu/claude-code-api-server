"""Reusable test data generators."""

import io
import random
import string
import zipfile


def make_valid_zip(files: dict[str, str | bytes] | None = None) -> bytes:
    """Create a valid ZIP archive in memory."""
    if files is None:
        files = {"hello.txt": "Hello, world!"}

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            if isinstance(content, str):
                content = content.encode("utf-8")
            zf.writestr(name, content)
    return buf.getvalue()


def make_zip_with_path_traversal() -> bytes:
    """Create a ZIP with '../../../etc/passwd' entry."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../../../etc/passwd", "root:x:0:0:root:/root:/bin/bash")
    return buf.getvalue()


def make_agent_content(name: str, description: str = "Test agent") -> str:
    """Generate valid agent markdown content with YAML frontmatter."""
    return (
        f"---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"tools: Read, Grep, Bash\n"
        f"---\n"
        f"\n"
        f"You are a test agent. Respond with \"OK\".\n"
    )


def make_skill_content(name: str, description: str = "Test skill") -> str:
    """Generate valid skill markdown content with YAML frontmatter."""
    return (
        f"---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"---\n"
        f"\n"
        f"You are a test skill. Respond with \"OK\".\n"
    )


def make_skill_zip(
    name: str,
    description: str = "Test skill",
    extra_files: dict[str, str | bytes] | None = None,
    flat: bool = False,
) -> bytes:
    """
    Create a valid skill ZIP archive.

    Args:
        name: Skill name (used as root dir name and in SKILL.md frontmatter).
        description: Skill description for SKILL.md frontmatter.
        extra_files: Additional files to include (paths relative to skill root).
        flat: If True, put files at archive root (no wrapping directory).

    Returns:
        ZIP archive bytes.
    """
    skill_md = make_skill_content(name, description)

    files: dict[str, str | bytes] = {}
    prefix = "" if flat else f"{name}/"

    files[f"{prefix}SKILL.md"] = skill_md

    if extra_files:
        for path, content in extra_files.items():
            files[f"{prefix}{path}"] = content

    return make_valid_zip(files)


def make_skill_zip_with_scripts(name: str) -> bytes:
    """Create a skill ZIP with scripts/ and references/ subdirs."""
    return make_skill_zip(
        name=name,
        extra_files={
            "scripts/analyze.py": "#!/usr/bin/env python3\nprint('analyzing')\n",
            "scripts/validate.sh": "#!/bin/bash\necho 'validating'\n",
            "references/guide.md": "# Guide\n\nSome reference documentation.\n",
        },
    )


def random_suffix(length: int = 6) -> str:
    """Generate a random alphanumeric suffix for unique test resource names."""
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))
