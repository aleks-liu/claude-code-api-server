"""
Configuration management for Claude Code API Server.

Uses Pydantic Settings for type-safe, validated configuration
with support for environment variables and .env files.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application settings with validation and defaults.

    All settings can be overridden via environment variables with CCAS_ prefix.
    Example: CCAS_DEBUG=true, CCAS_PORT=9000
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="CCAS_",
        extra="ignore",
    )

    # ==========================================================================
    # Server Configuration
    # ==========================================================================

    host: str = Field(
        default="0.0.0.0",
        description="Host to bind the server to",
    )
    port: int = Field(
        default=8000,
        ge=1,
        le=65535,
        description="Port to bind the server to",
    )
    debug: bool = Field(
        default=False,
        description="Enable debug mode (verbose logging, human-readable format)",
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO",
        description="Logging level",
    )
    rate_limit_rpm: int = Field(
        default=100,
        ge=0,
        description=(
            "Rate limit in requests per minute for all endpoints. "
            "Set to 0 to disable rate limiting (useful for testing)."
        ),
    )
    max_request_body_mb: int = Field(
        default=10,
        ge=1,
        le=500,
        description=(
            "Maximum HTTP request body size in MB for non-upload endpoints. "
            "The upload endpoint has its own size limit (max_upload_size_mb)."
        ),
    )

    # ==========================================================================
    # Data Storage
    # ==========================================================================

    data_dir: Path = Field(
        default=Path("/data"),
        description="Base directory for all data storage",
    )

    # ==========================================================================
    # Upload Limits
    # ==========================================================================

    max_upload_size_mb: int = Field(
        default=50,
        ge=1,
        le=500,
        description="Maximum upload archive size in MB",
    )
    max_extracted_size_mb: int = Field(
        default=500,
        ge=10,
        le=5000,
        description="Maximum total size when archive is extracted in MB",
    )
    max_files_per_archive: int = Field(
        default=10000,
        ge=100,
        le=100000,
        description="Maximum number of files allowed in an archive",
    )

    # ==========================================================================
    # Job Limits
    # ==========================================================================

    max_concurrent_jobs: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Maximum number of jobs that can run simultaneously",
    )
    max_pending_jobs: int = Field(
        default=50,
        ge=1,
        le=1000,
        description="Maximum number of pending (queued) jobs. New submissions are rejected with HTTP 429 when this limit is reached.",
    )

    # ==========================================================================
    # Model Settings
    # ==========================================================================

    default_model: str = Field(
        default="claude-sonnet-4-5",
        description=(
            "Default Claude model to use when the client does not specify one. "
            "Can be overridden per-job via the 'model' field in the job request."
        ),
    )
    setting_sources: str = Field(
        default="",
        description=(
            "Comma-separated list of Claude Code setting sources to load "
            "(e.g. 'user', 'project', 'local'). Empty string disables all "
            "setting sources. Set to 'none' to pass None to the SDK "
            "(omits --setting-sources flag entirely). "
            "Default is empty string which passes --setting-sources '' to the CLI."
        ),
    )

    # ==========================================================================
    # Timeouts (in seconds)
    # ==========================================================================

    default_job_timeout: int = Field(
        default=1800,
        ge=60,
        description="Default job timeout in seconds (30 minutes)",
    )
    max_job_timeout: int = Field(
        default=7200,
        ge=60,
        description="Maximum allowed job timeout in seconds (2 hours)",
    )
    min_job_timeout: int = Field(
        default=60,
        ge=10,
        description="Minimum allowed job timeout in seconds",
    )

    # ==========================================================================
    # Sandbox Settings
    # ==========================================================================

    enable_bwrap_sandbox: bool = Field(
        default=True,
        description=(
            "Enable bwrap (bubblewrap) process-level sandbox. "
            "When enabled, the entire Claude Code CLI process runs "
            "inside an isolated namespace with restricted filesystem "
            "visibility. The user's home directory, other jobs' data, "
            "and system directories are hidden or read-only. "
            "Requires bwrap to be installed."
        ),
    )
    bwrap_path: str = Field(
        default="bwrap",
        description="Path to the bwrap binary (resolved via PATH if relative)",
    )
    bwrap_allow_unsandboxed_fallback: bool = Field(
        default=False,
        description=(
            "If True, allow jobs to execute WITHOUT sandbox when bwrap "
            "is unavailable or sandbox creation fails (fail-open). "
            "If False (default), jobs are REFUSED when the sandbox "
            "cannot be created (fail-closed). "
            "WARNING: Setting this to True degrades security. Only use "
            "during development or when bwrap is genuinely unavailable."
        ),
    )
    sandbox_network_enabled: bool = Field(
        default=True,
        description=(
            "Enable per-job network isolation via HTTP proxy and "
            "--unshare-net. When enabled, jobs with non-unconfined "
            "security profiles run in an isolated network namespace "
            "with outbound traffic filtered by a per-job proxy. "
            "Set to False to disable all network isolation (escape hatch "
            "for environments where --unshare-net doesn't work)."
        ),
    )
    seccomp_dir: Path = Field(
        default=Path("/opt/ccas/seccomp"),
        description=(
            "Directory containing seccomp artifacts "
            "(apply-seccomp binary and BPF filters) from the "
            "@anthropic-ai/sandbox-runtime npm package. "
            "In Docker this is a symlink to the npm package. "
            "If this path does not exist, the server auto-discovers "
            "the npm package via 'npm root -g' as a fallback."
        ),
    )
    autoallowed_domains: str = Field(
        default="api.anthropic.com,*.anthropic.com,claude.ai,*.claude.ai",
        description=(
            "Comma-separated list of domains that are always allowed "
            "through the network proxy regardless of security profile. "
            "These ensure Claude CLI can always reach Anthropic services."
        ),
    )
    upstream_http_proxy: str = Field(
        default="",
        description=(
            "Upstream HTTP proxy for plain HTTP connections from the "
            "per-job network proxy. Format: http://[user:pass@]host:port "
            "or https://[user:pass@]host:port. "
            "When set, the SandboxProxy forwards allowed plain HTTP "
            "requests through this proxy. Only affects network-isolated "
            "profiles. The unconfined profile is not affected."
        ),
    )
    upstream_https_proxy: str = Field(
        default="",
        description=(
            "Upstream HTTP proxy for HTTPS (CONNECT) connections from the "
            "per-job network proxy. Format: http://[user:pass@]host:port "
            "or https://[user:pass@]host:port. "
            "When set, the SandboxProxy tunnels allowed HTTPS connections "
            "through this proxy via CONNECT. Only affects network-isolated "
            "profiles. The unconfined profile is not affected."
        ),
    )


    # ==========================================================================
    # MCP Server Settings
    # ==========================================================================

    mcp_health_check_timeout: int = Field(
        default=15,
        ge=5,
        le=120,
        description="Timeout in seconds for each MCP server health check",
    )

    # ==========================================================================
    # Cleanup Settings (in minutes)
    # ==========================================================================

    upload_ttl_minutes: int = Field(
        default=30,
        ge=5,
        description="Time-to-live for unused uploads in minutes",
    )
    job_input_cleanup_delay_minutes: int = Field(
        default=60,
        ge=5,
        description="Delay before deleting job input files after completion",
    )
    cleanup_interval_minutes: int = Field(
        default=15,
        ge=1,
        description="Interval between cleanup task runs in minutes",
    )

    # ==========================================================================
    # Admin Bootstrap Settings
    # ==========================================================================

    generate_admin_on_first_startup: bool = Field(
        default=False,
        description="Auto-generate admin user on first startup when no admin users exist",
    )
    admin_token_encryption_key: str = Field(
        default="",
        description=(
            "Base64-encoded RSA public key (PEM) for encrypting the auto-generated "
            "admin token. Required when generate_admin_on_first_startup=true."
        ),
    )

    # ==========================================================================
    # Validators
    # ==========================================================================

    @field_validator("data_dir", mode="after")
    @classmethod
    def resolve_data_dir(cls, v: Path) -> Path:
        """Ensure data_dir is an absolute path."""
        return v.resolve()

    @model_validator(mode="after")
    def validate_timeout_range(self) -> "Settings":
        """Ensure timeout values form a valid range."""
        if self.min_job_timeout > self.default_job_timeout:
            raise ValueError(
                f"min_job_timeout ({self.min_job_timeout}) cannot be greater than "
                f"default_job_timeout ({self.default_job_timeout})"
            )
        if self.default_job_timeout > self.max_job_timeout:
            raise ValueError(
                f"default_job_timeout ({self.default_job_timeout}) cannot be greater than "
                f"max_job_timeout ({self.max_job_timeout})"
            )
        return self

    @model_validator(mode="after")
    def validate_size_limits(self) -> "Settings":
        """Ensure extracted size limit is greater than upload size limit."""
        if self.max_extracted_size_mb < self.max_upload_size_mb:
            raise ValueError(
                f"max_extracted_size_mb ({self.max_extracted_size_mb}) should be >= "
                f"max_upload_size_mb ({self.max_upload_size_mb})"
            )
        return self

    # ==========================================================================
    # Computed Properties - Directory Paths
    # ==========================================================================

    @property
    def jobs_dir(self) -> Path:
        """Directory for job data storage."""
        return self.data_dir / "jobs"

    @property
    def uploads_dir(self) -> Path:
        """Directory for temporary upload storage."""
        return self.data_dir / "uploads"

    @property
    def auth_dir(self) -> Path:
        """Directory for authentication data."""
        return self.data_dir / "auth"

    @property
    def clients_file(self) -> Path:
        """Path to the clients authentication file."""
        return self.auth_dir / "clients.json"

    @property
    def sandbox_dir(self) -> Path:
        """Directory for sandbox/security profile data."""
        return self.data_dir / "sandbox"

    @property
    def security_profiles_file(self) -> Path:
        """Path to the security profiles configuration file."""
        return self.sandbox_dir / "profiles.json"

    @property
    def mcp_dir(self) -> Path:
        """Directory for MCP server configuration and packages."""
        return self.data_dir / "mcp"

    @property
    def mcp_servers_file(self) -> Path:
        """Path to the MCP servers configuration file."""
        return self.mcp_dir / "servers.json"

    @property
    def agents_dir(self) -> Path:
        """Directory for subagent configuration."""
        return self.data_dir / "agents"

    @property
    def agents_prompts_dir(self) -> Path:
        """Directory for subagent definition files (markdown with YAML frontmatter)."""
        return self.agents_dir / "prompts"

    @property
    def agents_file(self) -> Path:
        """Path to the agents metadata file."""
        return self.agents_dir / "agents.json"

    # Skills plugin paths

    @property
    def skills_plugin_dir(self) -> Path:
        """Plugin directory containing skills for Claude Code (passed via --plugin-dir)."""
        return self.data_dir / "skills-plugin"

    @property
    def skills_plugin_manifest_dir(self) -> Path:
        """Plugin manifest directory (.claude-plugin/)."""
        return self.skills_plugin_dir / ".claude-plugin"

    @property
    def plugin_agents_dir(self) -> Path:
        """Agents subdirectory inside the plugin (agents/).

        Agent .md files placed here are discovered by Claude Code via
        --plugin-dir, avoiding the --agents CLI argument size limit.
        """
        return self.skills_plugin_dir / "agents"

    @property
    def skills_dir(self) -> Path:
        """Skills subdirectory inside the plugin (skills/)."""
        return self.skills_plugin_dir / "skills"

    @property
    def skills_meta_dir(self) -> Path:
        """Metadata directory for skill management."""
        return self.data_dir / "skills-meta"

    @property
    def skills_meta_file(self) -> Path:
        """Path to skills metadata file."""
        return self.skills_meta_dir / "skills.json"

    # ==========================================================================
    # Computed Properties - Byte Conversions
    # ==========================================================================

    @property
    def max_request_body_bytes(self) -> int:
        """Maximum request body size in bytes."""
        return self.max_request_body_mb * 1024 * 1024

    @property
    def max_upload_size_bytes(self) -> int:
        """Maximum upload size in bytes."""
        return self.max_upload_size_mb * 1024 * 1024

    @property
    def max_extracted_size_bytes(self) -> int:
        """Maximum extracted archive size in bytes."""
        return self.max_extracted_size_mb * 1024 * 1024

    # ==========================================================================
    # Computed Properties - Time Conversions
    # ==========================================================================

    @property
    def upload_ttl_seconds(self) -> int:
        """Upload TTL in seconds."""
        return self.upload_ttl_minutes * 60

    @property
    def job_input_cleanup_delay_seconds(self) -> int:
        """Job input cleanup delay in seconds."""
        return self.job_input_cleanup_delay_minutes * 60

    @property
    def cleanup_interval_seconds(self) -> int:
        """Cleanup interval in seconds."""
        return self.cleanup_interval_minutes * 60

    # ==========================================================================
    # Computed Properties - Network
    # ==========================================================================

    @property
    def autoallowed_domains_list(self) -> list[str]:
        """Auto-allowed domains parsed into a list."""
        if not self.autoallowed_domains.strip():
            return []
        return [d.strip() for d in self.autoallowed_domains.split(",") if d.strip()]


@lru_cache
def get_settings() -> Settings:
    """
    Get cached settings instance.

    Uses lru_cache to ensure settings are only loaded once.
    Clear cache with get_settings.cache_clear() if needed for testing.
    """
    return Settings()


def ensure_directories(settings: Settings) -> None:
    """
    Create required directories if they don't exist.

    This should be called at application startup, not during
    settings initialization to avoid side effects during import.

    Args:
        settings: Application settings instance

    Raises:
        PermissionError: If directories cannot be created
        OSError: If there's a filesystem error
    """
    directories = [
        settings.data_dir,
        settings.jobs_dir,
        settings.uploads_dir,
        settings.auth_dir,
        settings.sandbox_dir,
        settings.mcp_dir,
        settings.agents_dir,
        settings.agents_prompts_dir,
        settings.skills_plugin_dir,
        settings.skills_plugin_manifest_dir,
        settings.plugin_agents_dir,
        settings.skills_dir,
        settings.skills_meta_dir,
    ]

    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)


def validate_timeout(timeout: int | None, settings: Settings) -> int:
    """
    Validate and normalize a job timeout value.

    Args:
        timeout: Requested timeout in seconds, or None for default
        settings: Application settings

    Returns:
        Validated timeout value within allowed range
    """
    if timeout is None:
        return settings.default_job_timeout

    # Clamp to valid range
    return max(
        settings.min_job_timeout,
        min(timeout, settings.max_job_timeout)
    )
