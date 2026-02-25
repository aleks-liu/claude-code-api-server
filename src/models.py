"""
Pydantic models for API requests, responses, and internal data structures.

This module defines all data models used throughout the application,
including API contracts, persistence models, and internal state.
"""

import re
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from . import __version__


def utcnow() -> datetime:
    """UTC now as naive datetime. Non-deprecated replacement for utcnow()."""
    return datetime.now(UTC).replace(tzinfo=None)


# =============================================================================
# Enums
# =============================================================================


class JobStatus(str, Enum):
    """Status of a job in its lifecycle."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"


class ClientRole(str, Enum):
    """Role of an API client."""

    ADMIN = "admin"
    CLIENT = "client"


# =============================================================================
# API Request Models
# =============================================================================


_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')

# Maximum length for entity names (agents, skills, profiles, MCP servers, clients).
# Single source of truth — imported by managers and admin_router.
MAX_NAME_LENGTH = 100

MAX_UPLOADS_PER_JOB = 5


class CreateJobRequest(BaseModel):
    """Request body for creating a new job."""

    upload_ids: list[str] = Field(
        default_factory=list,
        description=(
            "IDs of previously uploaded archives to combine in the job's working directory. "
            "Order matters: if archives contain files with the same path, later uploads overwrite earlier ones. "
            f"Maximum {MAX_UPLOADS_PER_JOB} uploads per job. "
            "Pass an empty list (or omit) for prompt-only jobs."
        ),
        max_length=MAX_UPLOADS_PER_JOB,
        examples=[["f47ac10b-58cc-4372-a567-0e02b2c3d479"]],
    )
    prompt: str | None = Field(
        default=None,
        description="Task description for the Claude agent. Optional when 'agent' is specified.",
        max_length=100_000,
        examples=["Analyze all Python files for SQL injection vulnerabilities"],
    )
    claude_md: str | None = Field(
        default=None,
        description="Optional CLAUDE.md content for agent configuration",
        max_length=50_000,
    )
    timeout_seconds: int | None = Field(
        default=None,
        description="Job timeout in seconds (uses default if not specified)",
        ge=60,
        le=7200,
    )
    model: str | None = Field(
        default=None,
        description=(
            "Claude model to use for this job. "
            "If not specified, the server default model is used "
            "(configurable via CCAS_DEFAULT_MODEL, defaults to 'claude-sonnet-4-6'). "
            "Examples: 'claude-sonnet-4-6', 'claude-opus-4-6'."
        ),
        examples=["claude-sonnet-4-6", "claude-opus-4-6"],
    )
    agent: str | None = Field(
        default=None,
        description=(
            "Name of a pre-configured agent to run this job as. "
            "When specified, the agent's system prompt replaces the default "
            "Claude Code prompt. The agent must exist (added via Admin API). "
            "Omit for standard Claude Code behavior."
        ),
        max_length=MAX_NAME_LENGTH,
        examples=["vuln-scanner", "code-reviewer"],
    )

    @field_validator("upload_ids")
    @classmethod
    def validate_upload_ids(cls, v: list[str]) -> list[str]:
        """Validate each upload ID is a UUID and there are no duplicates."""
        for i, uid in enumerate(v):
            if not _UUID_RE.match(uid):
                raise ValueError(
                    f"upload_ids[{i}]: '{uid}' is not a valid UUID"
                )
        if len(v) != len(set(v)):
            raise ValueError("upload_ids contains duplicate entries")
        return v

    @model_validator(mode="after")
    def validate_prompt_or_agent(self) -> "CreateJobRequest":
        """Ensure prompt is provided or auto-fill when agent is set."""
        if self.prompt is not None and self.prompt.strip():
            return self
        if self.agent:
            self.prompt = "Execute your task"
            return self
        raise ValueError("Prompt is required when no agent is specified")

    @field_validator("model")
    @classmethod
    def validate_model_format(cls, v: str | None) -> str | None:
        """
        Validate model identifier format.

        Allows any alphanumeric string with hyphens, dots, and underscores.
        Must start with an alphanumeric character to prevent CLI flag injection.
        The actual model validity is verified by the Anthropic API at execution time.
        """
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("Model cannot be empty or whitespace only")
        if len(v) > 256:
            raise ValueError(
                f"Model name too long ({len(v)} chars, max 256)"
            )
        if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$", v):
            raise ValueError(
                "Model must start with an alphanumeric character and contain "
                "only alphanumeric characters, hyphens, dots, and underscores"
            )
        return v

    @field_validator("agent")
    @classmethod
    def validate_agent_format(cls, v: str | None) -> str | None:
        """Validate agent name format (must match AgentManager naming rules)."""
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None  # treat whitespace-only as "no agent"
        if len(v) > MAX_NAME_LENGTH:
            raise ValueError(f"Agent name too long ({len(v)} chars, max {MAX_NAME_LENGTH})")
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9-]*$", v):
            raise ValueError(
                "Agent name must start with a letter and contain "
                "only letters, digits, and hyphens"
            )
        return v


# =============================================================================
# API Response Models
# =============================================================================


class UploadResponse(BaseModel):
    """Response after successful file upload."""

    upload_id: str = Field(
        ...,
        description="Unique identifier for the uploaded archive",
    )
    expires_at: datetime = Field(
        ...,
        description="Timestamp when the upload will be automatically deleted",
    )
    size_bytes: int = Field(
        ...,
        description="Size of the uploaded archive in bytes",
    )


class CreateJobResponse(BaseModel):
    """Response after job creation."""

    job_id: str = Field(
        ...,
        description="Unique identifier for the job",
    )
    status: JobStatus = Field(
        ...,
        description="Current job status",
    )
    created_at: datetime = Field(
        ...,
        description="Timestamp when the job was created",
    )


class JobOutput(BaseModel):
    """Output from a completed job."""

    text: str = Field(
        ...,
        description="Claude's text output",
    )
    files: dict[str, str] = Field(
        default_factory=dict,
        description="Map of output file paths to base64-encoded contents",
    )


class JobResponse(BaseModel):
    """
    Full job status response.

    Fields are populated based on job status:
    - PENDING: job_id, status, created_at
    - RUNNING: + started_at
    - COMPLETED: + completed_at, duration_ms, cost_usd, output
    - FAILED/TIMEOUT: + error, output (partial)
    """

    job_id: str = Field(
        ...,
        description="Unique identifier for the job",
    )
    status: JobStatus = Field(
        ...,
        description="Current job status",
    )
    model: str | None = Field(
        default=None,
        description="Claude model used for this job",
    )
    agent: str | None = Field(
        default=None,
        description="Agent used for this job (null if default Claude Code)",
    )
    created_at: datetime = Field(
        ...,
        description="Timestamp when the job was created",
    )
    started_at: datetime | None = Field(
        default=None,
        description="Timestamp when the job started running",
    )
    completed_at: datetime | None = Field(
        default=None,
        description="Timestamp when the job completed",
    )
    duration_ms: int | None = Field(
        default=None,
        description="Total job duration in milliseconds",
    )
    cost_usd: float | None = Field(
        default=None,
        description="Total API cost in USD",
    )
    output: JobOutput | None = Field(
        default=None,
        description="Job output (text and files)",
    )
    error: str | None = Field(
        default=None,
        description="Error message if job failed",
    )


class HealthResponse(BaseModel):
    """Public health check response (minimal)."""

    status: str = Field(
        ...,
        description="Server health status",
    )


class AdminStatusResponse(BaseModel):
    """Detailed server status (admin-only)."""

    status: str = Field(
        ...,
        description="Server health status",
    )
    active_jobs: int = Field(
        ...,
        description="Number of currently running jobs",
    )
    pending_uploads: int = Field(
        ...,
        description="Number of pending uploads awaiting job creation",
    )
    version: str = Field(
        default=__version__,
        description="Server version",
    )
    mcp_servers: dict[str, str] = Field(
        default_factory=dict,
        description="MCP server status: name -> 'ok' | 'failed' | 'skipped'",
    )


class ErrorResponse(BaseModel):
    """Standard error response for API documentation."""

    detail: str = Field(
        ...,
        description="Error message",
    )
    error_code: str | None = Field(
        default=None,
        description="Machine-readable error code",
    )


# =============================================================================
# Internal Persistence Models
# =============================================================================


class UploadMeta(BaseModel):
    """Metadata for an uploaded archive (persisted to disk)."""

    upload_id: str
    client_id: str | None = None
    created_at: datetime
    expires_at: datetime
    size_bytes: int
    original_filename: str | None = None
    content_type: str | None = None

    def is_expired(self) -> bool:
        """Check if this upload has expired."""
        return utcnow() > self.expires_at


class JobMeta(BaseModel):
    """
    Job metadata and state (persisted to disk).

    This is the authoritative state of a job, stored in status.json.
    """

    job_id: str
    client_id: str | None = None  # Client who created this job (None for legacy jobs pre-authorization)
    status: JobStatus
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None

    # Request data
    upload_ids: list[str] = Field(default_factory=list)
    prompt: str
    claude_md: str | None = None
    timeout_seconds: int
    model: str | None = None  # Claude model identifier (resolved from request or server default)
    agent: str | None = None  # Agent name used for this job (None = default Claude Code)

    # Results (populated on completion)
    duration_ms: int | None = None
    cost_usd: float | None = None
    output_text: str | None = None
    error: str | None = None

    # Tracking
    num_turns: int | None = None
    exit_code: int | None = None

    @model_validator(mode="before")
    @classmethod
    def migrate_upload_id(cls, data: Any) -> Any:
        """Migrate old status.json files that have 'upload_id' instead of 'upload_ids'."""
        if isinstance(data, dict):
            old = data.pop("upload_id", None)
            if old is not None and "upload_ids" not in data:
                data["upload_ids"] = [old]
        return data

    def to_response(self, output: JobOutput | None = None) -> JobResponse:
        """Convert to API response model."""
        return JobResponse(
            job_id=self.job_id,
            status=self.status,
            model=self.model,
            agent=self.agent,
            created_at=self.created_at,
            started_at=self.started_at,
            completed_at=self.completed_at,
            duration_ms=self.duration_ms,
            cost_usd=self.cost_usd,
            output=output,
            error=self.error,
        )


class ClientAuth(BaseModel):
    """Authentication data for a client (stored in clients.json)."""

    client_id: str = Field(
        ...,
        description="Unique client identifier",
    )
    key_id: str = Field(
        ...,
        description="Unique 8-character identifier embedded in the API token for O(1) client lookup",
        min_length=8,
        max_length=8,
        pattern=r'^[a-zA-Z0-9]{8}$',
    )
    key_hash: str = Field(
        ...,
        description="Argon2 hash of the API key",
    )
    description: str = Field(
        default="",
        description="Human-readable description of the client",
    )
    created_at: datetime = Field(
        default_factory=utcnow,
        description="When the client was created",
    )
    active: bool = Field(
        default=True,
        description="Whether the client is active",
    )
    role: ClientRole = Field(
        default=ClientRole.CLIENT,
        description="Client role (admin or client)",
    )
    security_profile: str = Field(
        default="common",
        description="Security profile assigned to this client",
    )


class ClientsFile(BaseModel):
    """Structure of the clients.json file."""

    clients: list[ClientAuth] = Field(default_factory=list)


# =============================================================================
# Security Event Models (for logging)
# =============================================================================


# =============================================================================
# Security Profile Models
# =============================================================================


class NetworkPolicy(BaseModel):
    """Network filtering policy for a security profile."""

    allowed_domains: list[str] | None = Field(
        default=None,
        description="DNS domains allowed. None = any domain, [] = none",
    )
    denied_domains: list[str] = Field(
        default_factory=list,
        description="DNS domains always denied (overrides allowed)",
    )
    allowed_ip_ranges: list[str] | None = Field(
        default=None,
        description="CIDR ranges allowed. None = any IP, [] = none",
    )
    denied_ip_ranges: list[str] = Field(
        default_factory=list,
        description="CIDR ranges always denied (overrides allowed)",
    )
    allow_ip_destination: bool = Field(
        default=False,
        description="Whether connections to raw IP addresses (not DNS names) are permitted",
    )


class SecurityProfile(BaseModel):
    """A security profile that defines restrictions for client jobs."""

    name: str
    description: str = ""
    network: NetworkPolicy = Field(default_factory=NetworkPolicy)
    denied_tools: list[str] = Field(default_factory=list)
    allowed_mcp_servers: list[str] | None = Field(
        default=None,
        description="MCP servers available to jobs. None = all, [] = none",
    )
    is_builtin: bool = False
    is_default: bool = False
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    def has_network_restrictions(self) -> bool:
        """Check if this profile has any network restrictions (not unconfined)."""
        net = self.network
        return not (
            net.allowed_domains is None
            and len(net.denied_domains) == 0
            and net.allowed_ip_ranges is None
            and len(net.denied_ip_ranges) == 0
            and net.allow_ip_destination is True
        )


class SecurityProfilesFile(BaseModel):
    """Structure of the profiles.json file."""

    profiles: dict[str, SecurityProfile] = Field(default_factory=dict)
    default_profile: str = "common"


class SecurityDecision(str, Enum):
    """Security decision for a tool call."""

    ALLOW = "allow"
    DENY = "deny"


class SecurityEvent(BaseModel):
    """Record of a security decision (for logging/auditing)."""

    timestamp: datetime = Field(default_factory=utcnow)
    job_id: str
    tool_name: str
    decision: SecurityDecision
    reason: str | None = None
    path: str | None = None
    command: str | None = None


# =============================================================================
# Tool Tracking Models
# =============================================================================


class ToolCall(BaseModel):
    """Record of a tool call made by the Claude agent."""

    tool_name: str
    input_data: dict[str, Any]
    timestamp: datetime = Field(default_factory=utcnow)
    allowed: bool = True
    duration_ms: int | None = None


# =============================================================================
# MCP Server Models
# =============================================================================


class McpServerEntry(BaseModel):
    """
    An MCP server configuration entry.

    Represents a single MCP server (stdio or HTTP) with its connection
    details, metadata, and installation provenance.
    """

    name: str = Field(
        ...,
        description="Unique server name (used in tool naming: mcp__<name>__*)",
    )
    type: str = Field(
        default="stdio",
        description="Transport type: 'stdio', 'http', or 'sse'",
    )
    command: str | None = Field(
        default=None,
        description="Executable command for stdio servers (e.g., 'node', '/data/mcp/venv/bin/markitdown-mcp')",
    )
    args: list[str] = Field(
        default_factory=list,
        description="Command arguments for stdio servers",
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Environment variables for stdio servers. Supports ${VAR} and ${VAR:-default} placeholders",
    )
    url: str | None = Field(
        default=None,
        description="Server URL for http/sse servers",
    )
    headers: dict[str, str] = Field(
        default_factory=dict,
        description="HTTP headers for http/sse servers. Supports ${VAR} placeholders",
    )
    description: str = Field(
        default="",
        description="Human-readable description of the server",
    )
    added_at: datetime = Field(
        default_factory=utcnow,
        description="When the server was registered",
    )
    package_manager: str | None = Field(
        default=None,
        description="Package manager used for installation: 'npm', 'pip', or None (manual)",
    )
    package: str | None = Field(
        default=None,
        description="Package name for uninstall tracking (e.g., '@modelcontextprotocol/server-sequential-thinking')",
    )


# =============================================================================
# Subagent Models
# =============================================================================


class AgentEntry(BaseModel):
    """
    Management metadata for a subagent definition.

    The actual agent configuration (name, description, tools, model, prompt)
    lives in the agent's markdown file with YAML frontmatter at
    /data/agents/prompts/<name>.md.  This model stores only provenance
    metadata used by the admin API for listing and tracking.

    Claude Code discovers agents natively from ``.claude/agents/`` or
    ``~/.claude/agents/`` — the markdown files are the source of truth.
    """

    name: str = Field(
        ...,
        description="Unique agent name (must match the 'name' field in the .md frontmatter)",
    )
    description: str = Field(
        default="",
        description="Human-readable description (mirrors frontmatter for quick listing)",
    )
    added_at: datetime = Field(
        default_factory=utcnow,
        description="When the agent was added",
    )
    prompt_size_bytes: int = Field(
        default=0,
        description="Size of the agent definition file in bytes",
    )


# =============================================================================
# Skill Models
# =============================================================================


class SkillEntry(BaseModel):
    """
    Management metadata for a skill definition.

    The actual skill configuration lives in the SKILL.md file at
    /data/skills-plugin/skills/<name>/SKILL.md.  This model stores only
    provenance metadata used by the admin API for listing and tracking.

    Skills differ from agents: they are directory-based (each skill is a
    directory containing a SKILL.md file) and are delivered to the Claude
    Code CLI via the plugin mechanism (``--plugin-dir``), not via the
    programmatic ``options.agents`` dict.
    """

    name: str = Field(
        ...,
        description="Unique skill name (must match the 'name' field in SKILL.md frontmatter)",
    )
    description: str = Field(
        default="",
        description="Human-readable description (mirrors frontmatter for quick listing)",
    )
    added_at: datetime = Field(
        default_factory=utcnow,
        description="When the skill was added",
    )
    skill_size_bytes: int = Field(
        default=0,
        description="Total uncompressed size of the skill directory in bytes",
    )
    file_count: int = Field(
        default=1,
        description="Number of files in the skill directory",
    )


# =============================================================================
# Admin API Models
# =============================================================================


class CreateClientRequest(BaseModel):
    """Request body for creating a new client via admin API."""

    client_id: str = Field(..., min_length=1, max_length=MAX_NAME_LENGTH, pattern=r'^[a-zA-Z0-9_-]+$')
    description: str = Field(default="", max_length=1000)
    role: ClientRole = ClientRole.CLIENT
    security_profile: str = Field(
        default="common",
        description="Security profile to assign (must exist)",
    )


class ClientResponse(BaseModel):
    """Response model for client info."""

    client_id: str
    description: str
    created_at: datetime
    active: bool
    role: ClientRole
    security_profile: str


class CreateClientResponse(BaseModel):
    """Response after creating a client (includes plaintext API key)."""

    client_id: str
    api_key: str
    role: ClientRole


class UpdateClientRequest(BaseModel):
    """Request body for updating a client."""

    description: str | None = Field(default=None, max_length=1000)
    role: ClientRole | None = None
    security_profile: str | None = None


# =============================================================================
# Security Profile API Models
# =============================================================================


class CreateProfileRequest(BaseModel):
    """Request body for creating a security profile."""

    name: str = Field(..., min_length=1, max_length=MAX_NAME_LENGTH, pattern=r'^[a-zA-Z0-9-]+$')
    description: str = Field(default="", max_length=1000)
    network: NetworkPolicy = Field(default_factory=NetworkPolicy)
    denied_tools: list[str] = Field(default_factory=list)
    allowed_mcp_servers: list[str] | None = None


class UpdateProfileRequest(BaseModel):
    """Request body for updating a security profile (partial update).

    Fields set to their default are considered "not provided" and won't
    be updated. Use model_fields_set to detect which fields were
    explicitly provided in the request JSON.
    """

    description: str | None = Field(default=None, max_length=1000)
    network: NetworkPolicy | None = None
    denied_tools: list[str] | None = None
    allowed_mcp_servers: list[str] | None = None


class ProfileResponse(BaseModel):
    """Response model for security profile info."""

    name: str
    description: str
    network: NetworkPolicy
    denied_tools: list[str]
    allowed_mcp_servers: list[str] | None
    is_builtin: bool
    is_default: bool
    created_at: datetime
    updated_at: datetime


class AddMcpServerRequest(BaseModel):
    """Request body for manually adding an MCP server."""

    name: str = Field(..., min_length=1, max_length=MAX_NAME_LENGTH, pattern=r'^[a-zA-Z0-9_-]+$')
    type: Literal["stdio", "http", "sse"] = "stdio"
    command: str | None = Field(
        default=None,
        max_length=500,
        pattern=r'^[^;&|`$]+$',
    )
    args: list[str] | None = Field(default=None, max_length=100)
    env: dict[str, str] | None = None
    url: str | None = Field(default=None, max_length=2048)
    headers: dict[str, str] | None = None
    description: str = Field(default="", max_length=1000)

    @field_validator("args")
    @classmethod
    def validate_args(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            for i, arg in enumerate(v):
                if len(arg) > 2000:
                    raise ValueError(f"Arg at index {i} exceeds max length 2000")
        return v

    @field_validator("env")
    @classmethod
    def validate_env(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v is not None:
            if len(v) > 50:
                raise ValueError(f"Too many env vars ({len(v)}, max 50)")
            for key, val in v.items():
                if len(key) > 256:
                    raise ValueError(f"Env key '{key[:50]}...' exceeds max length 256")
                if len(val) > 8192:
                    raise ValueError(f"Env value for '{key}' exceeds max length 8192")
        return v

    @field_validator("url")
    @classmethod
    def validate_url_scheme(cls, v: str | None) -> str | None:
        if v is not None:
            if not v.startswith(("http://", "https://")):
                raise ValueError("URL must start with http:// or https://")
        return v

    @field_validator("headers")
    @classmethod
    def validate_headers(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v is not None:
            if len(v) > 50:
                raise ValueError(f"Too many headers ({len(v)}, max 50)")
            for key, val in v.items():
                if len(key) > 256:
                    raise ValueError(f"Header name '{key[:50]}...' exceeds max length 256")
                if len(val) > 8192:
                    raise ValueError(f"Header value for '{key}' exceeds max length 8192")
        return v


class InstallMcpServerRequest(BaseModel):
    """Request body for installing an MCP server from npm/pip."""

    package: str = Field(
        ...,
        min_length=1,
        max_length=500,
        pattern=r'^[a-zA-Z0-9@_./:>=<!\-\[\],\s]+$',
    )
    name: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_NAME_LENGTH,
        pattern=r'^[a-zA-Z0-9_-]+$',
    )
    description: str = Field(default="", max_length=1000)
    pip: bool = False


class McpServerResponse(BaseModel):
    """Response model for MCP server info."""

    name: str
    type: str
    description: str
    package_manager: str | None
    package: str | None
    added_at: datetime


class McpHealthCheckResponse(BaseModel):
    """Response model for MCP server health check."""

    name: str
    healthy: bool
    detail: str


class AddAgentRequest(BaseModel):
    """Request body for adding a subagent."""

    name: str = Field(..., min_length=1, max_length=MAX_NAME_LENGTH, pattern=r'^[a-zA-Z0-9-]+$')
    content: str | None = None
    content_base64: str | None = None
    description: str = Field(default="", max_length=1000)


class UpdateAgentRequest(BaseModel):
    """Request body for updating a subagent."""

    content: str | None = None
    content_base64: str | None = None
    description: str | None = Field(default=None, max_length=1000)


class AgentResponse(BaseModel):
    """Response model for agent info."""

    name: str
    description: str
    prompt_size_bytes: int
    added_at: datetime


class AgentDetailResponse(AgentResponse):
    """Detailed response model for agent info."""

    frontmatter: dict | None
    body_preview: str


class SkillResponse(BaseModel):
    """Response model for skill info."""

    name: str
    description: str
    skill_size_bytes: int
    file_count: int
    added_at: datetime


class SkillDetailResponse(SkillResponse):
    """Detailed response model for skill info."""

    frontmatter: dict | None
    body_preview: str
    file_listing: list[str]
