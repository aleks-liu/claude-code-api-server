"""Data types for the CCAS client."""

from dataclasses import dataclass, field
from enum import Enum


class SyncStatus(Enum):
    SYNCED = "synced"
    DIVERGED = "diverged"
    LOCAL_ONLY = "local_only"
    REMOTE_ONLY = "remote_only"
    INCOMPATIBLE = "incompatible"


@dataclass
class EntityInfo:
    """Base info for any entity (local or remote)."""
    name: str
    description: str = ""
    size_bytes: int = 0


@dataclass
class LocalAgentInfo(EntityInfo):
    path: str = ""
    content_hash: str = ""
    name_compatible: bool = True


@dataclass
class LocalSkillInfo(EntityInfo):
    path: str = ""
    content_hash: str = ""
    file_count: int = 0
    file_listing: list[str] = field(default_factory=list)
    name_compatible: bool = True


@dataclass
class LocalMcpInfo:
    name: str
    type: str = "stdio"
    config: dict = field(default_factory=dict)


@dataclass
class RemoteAgentInfo(EntityInfo):
    prompt_size_bytes: int = 0
    added_at: str = ""
    frontmatter: dict | None = None


@dataclass
class RemoteSkillInfo(EntityInfo):
    skill_size_bytes: int = 0
    file_count: int = 0
    added_at: str = ""
    file_listing: list[str] = field(default_factory=list)
    frontmatter: dict | None = None


@dataclass
class RemoteMcpInfo:
    name: str
    type: str = "stdio"
    description: str = ""
    package_manager: str | None = None
    package: str | None = None
    added_at: str = ""


@dataclass
class SyncEntry:
    """A single entity's sync status."""
    entity_type: str
    name: str
    status: SyncStatus
    local_info: EntityInfo | LocalMcpInfo | None = None
    remote_info: EntityInfo | RemoteMcpInfo | None = None
    detail: str = ""


@dataclass
class PushResult:
    """Result of a push operation."""
    entity_type: str
    name: str
    success: bool
    message: str = ""
