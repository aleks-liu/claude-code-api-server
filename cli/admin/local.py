"""Local Claude Code installation scanner."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import zipfile
from pathlib import Path

from .models import LocalAgentInfo, LocalMcpInfo, LocalSkillInfo

# CCAS naming rule: alphanumeric with hyphens, starts with letter
_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9-]*$")
# Server enforces max 100 chars for all entity names
_MAX_NAME_LEN = 100

# Environment-specific directories to exclude from skill archives.
# These are never intentional skill content and would fail server-side
# dirname validation (e.g., __pycache__ starts with underscore).
_EXCLUDED_DIRS = frozenset({"__pycache__", "node_modules"})


def _is_name_compatible(name: str, max_len: int = _MAX_NAME_LEN) -> bool:
    """Check if name matches CCAS naming rules."""
    return bool(_NAME_RE.match(name)) and len(name) <= max_len


class LocalScanner:
    """Reads ~/.claude/ for agents, skills, and MCP server configs."""

    def __init__(self, claude_home: str | None = None):
        self._home = Path(claude_home or os.path.expanduser("~/.claude"))

    @property
    def home(self) -> Path:
        return self._home

    @property
    def exists(self) -> bool:
        return self._home.is_dir()

    def scan_agents(self) -> dict[str, LocalAgentInfo]:
        """Scan ~/.claude/agents/*.md files."""
        agents_dir = self._home / "agents"
        if not agents_dir.is_dir():
            return {}

        result: dict[str, LocalAgentInfo] = {}
        for path in sorted(agents_dir.glob("*.md")):
            if not path.is_file():
                continue

            text = path.read_text(errors="replace")
            # Encode with normalized line endings (LF) — matches what server stores
            normalized = text.encode("utf-8")
            content_hash = hashlib.sha256(normalized).hexdigest()

            # Extract name from frontmatter or filename stem
            name = self._extract_frontmatter_name(text) or path.stem
            description = self._extract_frontmatter_field(text, "description") or ""

            result[name] = LocalAgentInfo(
                name=name,
                description=description,
                size_bytes=len(normalized),
                path=str(path),
                content_hash=content_hash,
                name_compatible=_is_name_compatible(name),
            )

        return result

    def scan_skills(self) -> dict[str, LocalSkillInfo]:
        """Scan ~/.claude/skills/*/SKILL.md directories."""
        skills_dir = self._home / "skills"
        if not skills_dir.is_dir():
            return {}

        result: dict[str, LocalSkillInfo] = {}
        for skill_dir in sorted(skills_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.is_file():
                continue

            name = skill_dir.name
            files = self._list_skill_files(skill_dir)
            file_listing = [str(f.relative_to(skill_dir)) for f in files]
            total_size = sum(f.stat().st_size for f in files)
            content_hash = self._compute_skill_hash(skill_dir, files)

            # Parse description from SKILL.md frontmatter
            text = skill_md.read_text(errors="replace")
            description = self._extract_frontmatter_field(text, "description") or ""

            result[name] = LocalSkillInfo(
                name=name,
                description=description,
                size_bytes=total_size,
                path=str(skill_dir),
                content_hash=content_hash,
                file_count=len(files),
                file_listing=file_listing,
                name_compatible=_is_name_compatible(name),
            )

        return result

    def scan_mcps(self) -> dict[str, LocalMcpInfo]:
        """Scan ~/.claude/settings.json mcpServers section."""
        settings_file = self._home / "settings.json"
        if not settings_file.is_file():
            return {}

        try:
            data = json.loads(settings_file.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

        mcp_servers = data.get("mcpServers", {})
        if not isinstance(mcp_servers, dict):
            return {}

        result: dict[str, LocalMcpInfo] = {}
        for name, config in mcp_servers.items():
            if not isinstance(config, dict):
                continue
            # Detect type
            if "url" in config:
                mcp_type = config.get("type", "http")
            elif "command" in config:
                mcp_type = "stdio"
            else:
                mcp_type = "unknown"

            result[name] = LocalMcpInfo(
                name=name,
                type=mcp_type,
                config=config,
            )

        return result

    def read_agent_content(self, name: str) -> str:
        """Read full content of an agent .md file."""
        # Search by frontmatter name or filename
        agents = self.scan_agents()
        info = agents.get(name)
        if info is None:
            raise FileNotFoundError(f"Agent '{name}' not found locally")
        return Path(info.path).read_text(errors="replace")

    def create_skill_zip(self, name: str) -> bytes:
        """Create in-memory ZIP of a skill directory."""
        skills = self.scan_skills()
        info = skills.get(name)
        if info is None:
            raise FileNotFoundError(f"Skill '{name}' not found locally")

        skill_dir = Path(info.path)
        files = self._list_skill_files(skill_dir)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path in files:
                relative = file_path.relative_to(skill_dir)
                arcname = f"{name}/{relative}"
                zf.write(file_path, arcname)
        return buf.getvalue()

    def get_mcp_config(self, name: str) -> dict:
        """Get full MCP config dict for a specific server."""
        mcps = self.scan_mcps()
        info = mcps.get(name)
        if info is None:
            raise FileNotFoundError(f"MCP server '{name}' not found locally")
        return info.config

    # --- Private helpers ---

    @staticmethod
    def _list_skill_files(skill_dir: Path) -> list[Path]:
        """List all non-hidden, non-cache files in a skill directory."""
        files: list[Path] = []
        for path in sorted(skill_dir.rglob("*")):
            rel_parts = path.relative_to(skill_dir).parts
            if any(part.startswith(".") for part in rel_parts):
                continue
            if any(part in _EXCLUDED_DIRS for part in rel_parts):
                continue
            if path.is_file():
                files.append(path)
        return files

    @staticmethod
    def _compute_skill_hash(skill_dir: Path, files: list[Path]) -> str:
        """Deterministic SHA-256 of all skill files."""
        hasher = hashlib.sha256()
        for f in files:
            relative = str(f.relative_to(skill_dir))
            content = f.read_bytes()
            hasher.update(relative.encode("utf-8"))
            hasher.update(b"\x00")
            hasher.update(content)
            hasher.update(b"\x00")
        return hasher.hexdigest()

    @staticmethod
    def _extract_frontmatter_name(text: str) -> str | None:
        """Extract 'name' from YAML frontmatter."""
        return LocalScanner._extract_frontmatter_field(text, "name")

    @staticmethod
    def _extract_frontmatter_field(text: str, field: str) -> str | None:
        """Extract a field value from YAML frontmatter (simple parser)."""
        if not text.startswith("---"):
            return None
        end = text.find("---", 3)
        if end == -1:
            return None
        frontmatter = text[3:end]
        for line in frontmatter.split("\n"):
            line = line.strip()
            if line.startswith(f"{field}:"):
                value = line[len(field) + 1:].strip()
                # Strip quotes
                if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
                    value = value[1:-1]
                return value if value else None
        return None
