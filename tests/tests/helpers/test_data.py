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


def random_suffix(length: int = 6) -> str:
    """Generate a random alphanumeric suffix for unique test resource names."""
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))
