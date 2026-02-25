"""Sync engine: compares local vs remote and executes push operations."""

from __future__ import annotations

from ..api import AdminApiClient, ApiError
from ..formatters import Console
from .local import LocalScanner, _NAME_RE, _MAX_NAME_LEN
from .models import (
    LocalAgentInfo,
    LocalSkillInfo,
    PushResult,
    RemoteAgentInfo,
    RemoteSkillInfo,
    SyncEntry,
    SyncStatus,
)


def _incompat_reason(name: str, max_len: int = _MAX_NAME_LEN) -> str:
    """Explain why a name is incompatible."""
    if not _NAME_RE.match(name):
        return "name has invalid characters"
    if len(name) > max_len:
        return f"name too long ({len(name)} chars, max {max_len})"
    return "name incompatible"


class SyncEngine:
    """Compares local Claude Code entities with remote CCAS and pushes changes."""

    def __init__(self, api: AdminApiClient, local: LocalScanner):
        self._api = api
        self._local = local

    def compute_status(
        self, entity_types: list[str] | None = None
    ) -> list[SyncEntry]:
        """Compute sync status for all (or filtered) entity types."""
        types = entity_types or ["skill", "agent"]
        entries: list[SyncEntry] = []

        if "skill" in types:
            entries.extend(self._compute_skill_status())
        if "agent" in types:
            entries.extend(self._compute_agent_status())

        return entries

    # --- Skill comparison ---

    def _compute_skill_status(self) -> list[SyncEntry]:
        local_skills = self._local.scan_skills()
        try:
            remote_list = self._api.list_skills()
        except ApiError:
            remote_list = []

        remote_skills: dict[str, RemoteSkillInfo] = {}
        for s in remote_list:
            name = s["name"]
            remote_skills[name] = RemoteSkillInfo(
                name=name,
                description=s.get("description", ""),
                skill_size_bytes=s.get("skill_size_bytes", 0),
                file_count=s.get("file_count", 0),
                added_at=s.get("added_at", ""),
                file_listing=s.get("file_listing", []),
            )

        # Fetch detailed info for remote skills to get file_listing
        for name in remote_skills:
            try:
                detail = self._api.get_skill(name)
                remote_skills[name].file_listing = detail.get("file_listing", [])
                remote_skills[name].skill_size_bytes = detail.get("skill_size_bytes", 0)
            except ApiError:
                pass

        all_names = sorted(set(local_skills) | set(remote_skills))
        entries: list[SyncEntry] = []

        for name in all_names:
            local = local_skills.get(name)
            remote = remote_skills.get(name)

            if local and remote:
                if not local.name_compatible:
                    status = SyncStatus.INCOMPATIBLE
                    detail = _incompat_reason(name)
                else:
                    status = self._compare_skill(local, remote)
                    detail = self._skill_detail(local, remote, status)
            elif local:
                if not local.name_compatible:
                    status = SyncStatus.INCOMPATIBLE
                    detail = _incompat_reason(name)
                else:
                    status = SyncStatus.LOCAL_ONLY
                    detail = "not on remote"
            else:
                status = SyncStatus.REMOTE_ONLY
                detail = "not local"

            entries.append(SyncEntry(
                entity_type="skill",
                name=name,
                status=status,
                local_info=local,
                remote_info=remote,
                detail=detail,
            ))

        return entries

    def _compare_skill(self, local: LocalSkillInfo,
                       remote: RemoteSkillInfo) -> SyncStatus:
        local_listing = sorted(local.file_listing)
        remote_listing = sorted(remote.file_listing)
        if local_listing == remote_listing and local.size_bytes == remote.skill_size_bytes:
            return SyncStatus.SYNCED
        return SyncStatus.DIVERGED

    @staticmethod
    def _skill_detail(local: LocalSkillInfo, remote: RemoteSkillInfo,
                      status: SyncStatus) -> str:
        if status == SyncStatus.SYNCED:
            return ""
        parts = []
        if sorted(local.file_listing) != sorted(remote.file_listing):
            parts.append("files changed")
        if local.size_bytes != remote.skill_size_bytes:
            parts.append(f"local {Console.format_size(local.size_bytes)}, "
                        f"remote {Console.format_size(remote.skill_size_bytes)}")
        return "; ".join(parts) if parts else "content changed"

    # --- Agent comparison ---

    def _compute_agent_status(self) -> list[SyncEntry]:
        local_agents = self._local.scan_agents()
        try:
            remote_list = self._api.list_agents()
        except ApiError:
            remote_list = []

        remote_agents: dict[str, RemoteAgentInfo] = {}
        for a in remote_list:
            name = a["name"]
            remote_agents[name] = RemoteAgentInfo(
                name=name,
                description=a.get("description", ""),
                prompt_size_bytes=a.get("prompt_size_bytes", 0),
                added_at=a.get("added_at", ""),
            )

        all_names = sorted(set(local_agents) | set(remote_agents))
        entries: list[SyncEntry] = []

        for name in all_names:
            local = local_agents.get(name)
            remote = remote_agents.get(name)

            if local and remote:
                if not local.name_compatible:
                    status = SyncStatus.INCOMPATIBLE
                    detail = _incompat_reason(name)
                else:
                    status = self._compare_agent(local, remote)
                    detail = self._agent_detail(local, remote, status)
            elif local:
                if not local.name_compatible:
                    status = SyncStatus.INCOMPATIBLE
                    detail = _incompat_reason(name)
                else:
                    status = SyncStatus.LOCAL_ONLY
                    detail = "not on remote"
            else:
                status = SyncStatus.REMOTE_ONLY
                detail = "not local"

            entries.append(SyncEntry(
                entity_type="agent",
                name=name,
                status=status,
                local_info=local,
                remote_info=remote,
                detail=detail,
            ))

        return entries

    def _compare_agent(self, local: LocalAgentInfo,
                       remote: RemoteAgentInfo) -> SyncStatus:
        if local.size_bytes == remote.prompt_size_bytes:
            return SyncStatus.SYNCED
        return SyncStatus.DIVERGED

    @staticmethod
    def _agent_detail(local: LocalAgentInfo, remote: RemoteAgentInfo,
                      status: SyncStatus) -> str:
        if status == SyncStatus.SYNCED:
            return ""
        return (f"local {Console.format_size(local.size_bytes)}, "
                f"remote {Console.format_size(remote.prompt_size_bytes)}")

    # --- Push operations ---

    def push_agent(self, name: str) -> PushResult:
        """Push a local agent to remote."""
        try:
            content = self._local.read_agent_content(name)
        except FileNotFoundError as e:
            return PushResult("agent", name, False, str(e))

        agents = self._local.scan_agents()
        info = agents.get(name)
        description = info.description if info else ""

        try:
            # Check if exists on remote
            try:
                self._api.get_agent(name)
                # Exists: update
                self._api.update_agent(name, content=content, description=description or None)
                return PushResult("agent", name, True, "updated")
            except ApiError as e:
                if e.status_code == 404:
                    # New: create
                    self._api.add_agent(name, content, description)
                    return PushResult("agent", name, True, "created")
                raise
        except ApiError as e:
            return PushResult("agent", name, False, e.detail)

    def push_skill(self, name: str) -> PushResult:
        """Push a local skill to remote."""
        try:
            zip_bytes = self._local.create_skill_zip(name)
        except FileNotFoundError as e:
            return PushResult("skill", name, False, str(e))

        try:
            # Check if exists on remote
            try:
                self._api.get_skill(name)
                # Exists: update
                self._api.update_skill_zip(name, zip_bytes)
                return PushResult("skill", name, True, "updated")
            except ApiError as e:
                if e.status_code == 404:
                    # New: create
                    self._api.add_skill_zip(zip_bytes, name)
                    return PushResult("skill", name, True, "created")
                raise
        except ApiError as e:
            return PushResult("skill", name, False, e.detail)

    def push_batch(self, entries: list[SyncEntry],
                   console: Console | None = None,
                   auto_confirm: bool = False) -> list[PushResult]:
        """Push multiple entities sequentially."""
        results: list[PushResult] = []
        total = len(entries)

        for i, entry in enumerate(entries, 1):
            if console:
                console.progress(i, total, f"Pushing {entry.entity_type} '{entry.name}'...")

            if entry.entity_type == "agent":
                r = self.push_agent(entry.name)
            elif entry.entity_type == "skill":
                r = self.push_skill(entry.name)
            else:
                r = PushResult(entry.entity_type, entry.name, False, "unknown type")

            results.append(r)

            if console:
                if r.success:
                    console.success(f"{entry.entity_type} '{entry.name}': {r.message}")
                else:
                    console.error(f"{entry.entity_type} '{entry.name}': {r.message}")

        return results

    @staticmethod
    def _build_mcp_payload(name: str, config: dict) -> dict:
        """Build MCP server payload from local config."""
        payload: dict = {}

        if "url" in config:
            payload["type"] = config.get("type", "http")
            payload["url"] = config["url"]
            if "headers" in config:
                payload["headers"] = config["headers"]
        elif "command" in config:
            payload["type"] = "stdio"
            payload["command"] = config["command"]
            if "args" in config:
                payload["args"] = config["args"]
            if "env" in config:
                payload["env"] = config["env"]

        return payload
