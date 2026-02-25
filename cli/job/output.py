"""Result saving — text output and base64-encoded file extraction."""

from __future__ import annotations

import base64
import os
import re
from pathlib import Path

from ..formatters import Console


def save_results(
    result: dict,
    output_dir: str,
    job_id: str,
    console: Console,
) -> str:
    """Save job output to <output_dir>/<job_id>/.

    - Text -> output.txt
    - Files -> decoded from base64, saved with original names

    Returns the output directory path.
    """
    job_dir = Path(output_dir) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    output = result.get("output")
    if not output:
        return str(job_dir)

    # Save text output
    text = output.get("text", "")
    if text:
        text_path = job_dir / "output.txt"
        text_path.write_text(text, encoding="utf-8")
        console.detail(f"Text saved: {text_path}")

    # Save files (base64-encoded)
    files = output.get("files", {})
    if files:
        saved = 0
        for filename, b64_content in files.items():
            safe_name = _sanitize_filename(filename)
            if not safe_name:
                console.warning(f"Skipping file with empty name after sanitization: {filename!r}")
                continue

            file_path = job_dir / safe_name

            # Create parent directories for nested paths
            file_path.parent.mkdir(parents=True, exist_ok=True)

            try:
                content = base64.b64decode(b64_content)
                file_path.write_bytes(content)
                saved += 1
            except Exception as e:
                console.warning(f"Failed to decode file '{filename}': {e}")
                continue

        if saved:
            console.detail(f"Files saved: {saved} file(s) in {job_dir}")

    return str(job_dir)


def _sanitize_filename(name: str) -> str:
    """Remove path traversal and unsafe characters from output filename.

    Rules:
    1. Replace backslashes with forward slashes
    2. Remove leading / and ./
    3. Replace .. segments with _
    4. Replace non-alphanumeric/safe chars with _
    5. If empty after sanitization -> file_0
    """
    # Normalize separators
    name = name.replace("\\", "/")

    # Remove leading / and ./
    name = name.lstrip("/")
    while name.startswith("./"):
        name = name[2:]

    # Replace .. segments
    parts = name.split("/")
    safe_parts = []
    for part in parts:
        if part == "..":
            safe_parts.append("_")
        else:
            # Replace unsafe characters (keep alphanumeric, hyphens, underscores, dots, slashes)
            safe_part = re.sub(r"[^a-zA-Z0-9._/-]", "_", part)
            if safe_part:
                safe_parts.append(safe_part)

    result = "/".join(safe_parts)

    if not result:
        result = "file_0"

    return result
