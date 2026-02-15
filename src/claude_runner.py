"""
Claude Agent SDK wrapper for Claude Code API Server.

Provides the ClaudeRunner class that executes Claude Agent jobs with
proper timeout handling, security enforcement, and result collection.
"""

import asyncio
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Settings, get_settings
from .logging_config import get_logger, set_job_context
from .mcp_loader import McpConfig, load_mcp_config
from .mcp_manager import McpManager
from .models import JobMeta
from .security import create_permission_handler
from .security_profiles import get_profile_manager

logger = get_logger(__name__)


# =============================================================================
# Server System Preamble
# =============================================================================

# Loaded from src/server_preamble.md at module init.
# Injected into every job's system prompt AFTER any client-provided claude_md.
# Positioned last so these rules take precedence over client instructions.
_PREAMBLE_PATH = Path(__file__).parent / "server_preamble.md"
SERVER_SYSTEM_PREAMBLE: str = _PREAMBLE_PATH.read_text(encoding="utf-8")


# =============================================================================
# Result Types
# =============================================================================


@dataclass
class ClaudeResult:
    """Result from a Claude Agent execution."""

    # Output
    output_text: str = ""

    # Metrics
    cost_usd: float | None = None
    duration_ms: int | None = None
    num_turns: int | None = None

    # Error state
    error: str | None = None
    is_timeout: bool = False
    is_error: bool = False

    # Raw messages (for debugging)
    messages: list[Any] = field(default_factory=list)


# =============================================================================
# Runner Errors
# =============================================================================


class RunnerError(Exception):
    """Base exception for runner errors."""

    pass


class SDKNotAvailableError(RunnerError):
    """Raised when the Claude Agent SDK is not available."""

    pass


class ExecutionError(RunnerError):
    """Raised when job execution fails."""

    pass


# =============================================================================
# Claude Runner
# =============================================================================


class ClaudeRunner:
    """
    Executes Claude Agent jobs with security enforcement and timeout handling.

    Uses asyncio.Semaphore to limit concurrent executions.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        mcp_config: McpConfig | None = None,
    ):
        """
        Initialize the Claude runner.

        Args:
            settings: Application settings (uses get_settings() if not provided)
            mcp_config: Pre-loaded MCP configuration. If None, loads from
                settings.mcp_servers_file. Pass an explicit McpConfig to
                share a validated config from the startup health check.
        """
        if settings is None:
            settings = get_settings()

        self._settings = settings
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)

        # Load MCP configuration
        if mcp_config is not None:
            self._mcp_config = mcp_config
        else:
            try:
                mcp_manager = McpManager(settings.mcp_servers_file)
                self._mcp_config = load_mcp_config(mcp_manager)
            except Exception as e:
                logger.warning(
                    "mcp_config_load_failed_in_runner",
                    error=str(e),
                    message="MCP configuration could not be loaded. No MCP servers will be available.",
                )
                self._mcp_config = McpConfig()

        if self._mcp_config.has_servers:
            logger.info(
                "mcp_servers_available_for_jobs",
                server_count=len(self._mcp_config.servers),
                servers=list(self._mcp_config.servers.keys()),
            )

        # Check if SDK is available
        self._sdk_available = self._check_sdk_available()
        if not self._sdk_available:
            logger.warning("claude_agent_sdk_not_available")

        # Check bwrap availability at startup for visibility
        self._check_bwrap_status()

        # Validate upstream proxy configuration at startup
        self._check_upstream_proxy_config()

    def _check_sdk_available(self) -> bool:
        """Check if the Claude Agent SDK is installed."""
        try:
            import claude_agent_sdk
            return True
        except ImportError:
            return False

    def _check_bwrap_status(self) -> None:
        """
        Validate bwrap process-level sandbox availability at startup.

        Performs a full validation (including smoke test) to surface
        configuration problems early rather than at first job execution.
        """
        if not self._settings.enable_bwrap_sandbox:
            logger.warning(
                "bwrap_sandbox_disabled_by_config",
                message=(
                    "Process-level bwrap sandbox is DISABLED. "
                    "Jobs will execute without filesystem isolation. "
                    "Set CCAS_ENABLE_BWRAP_SANDBOX=true for production use."
                ),
            )
            return

        from .sandbox import validate_bwrap_installation, SandboxSetupError

        try:
            resolved_path = validate_bwrap_installation(self._settings.bwrap_path)
            logger.info(
                "bwrap_process_sandbox_ready",
                bwrap_path=resolved_path,
                fail_closed=not self._settings.bwrap_allow_unsandboxed_fallback,
                message=(
                    "Process-level bwrap sandbox is available. "
                    "All Claude CLI processes will run in isolated namespaces."
                ),
            )
            # Check seccomp availability alongside bwrap
            from .sandbox_seccomp import check_seccomp_at_startup
            check_seccomp_at_startup(self._settings.seccomp_dir)

        except SandboxSetupError as exc:
            if self._settings.bwrap_allow_unsandboxed_fallback:
                logger.warning(
                    "bwrap_sandbox_unavailable_fallback_enabled",
                    bwrap_path=self._settings.bwrap_path,
                    error=str(exc),
                    message=(
                        "bwrap is not available. Jobs will execute WITHOUT "
                        "process-level sandbox isolation (fallback enabled). "
                        "Install bubblewrap: apt-get install bubblewrap"
                    ),
                )
            else:
                logger.error(
                    "bwrap_sandbox_unavailable_fail_closed",
                    bwrap_path=self._settings.bwrap_path,
                    error=str(exc),
                    message=(
                        "bwrap is not available and unsandboxed fallback is "
                        "disabled. ALL JOBS WILL FAIL until bwrap is installed "
                        "or CCAS_BWRAP_ALLOW_UNSANDBOXED_FALLBACK=true is set. "
                        "Install bubblewrap: apt-get install bubblewrap"
                    ),
                )

    def _check_upstream_proxy_config(self) -> None:
        """Validate and log upstream proxy configuration at startup."""
        from .sandbox_proxy import parse_upstream_proxy

        for var_name, value in [
            ("CCAS_UPSTREAM_HTTP_PROXY", self._settings.upstream_http_proxy),
            ("CCAS_UPSTREAM_HTTPS_PROXY", self._settings.upstream_https_proxy),
        ]:
            if not value.strip():
                continue
            try:
                config = parse_upstream_proxy(value)
                if config is not None:
                    logger.info(
                        "upstream_proxy_configured",
                        variable=var_name,
                        proxy_url=config.raw_url,
                        use_tls=config.use_tls,
                        has_auth=config.auth_header is not None,
                    )
            except ValueError as exc:
                logger.error(
                    "upstream_proxy_invalid",
                    variable=var_name,
                    error=str(exc),
                )

    def _resolve_client_profile(self, client_id: str) -> str | None:
        """Look up the security profile assigned to a client."""
        from .auth import get_auth_manager
        auth_manager = get_auth_manager()
        client = auth_manager.get_client(client_id)
        if client is not None:
            return client.security_profile
        return None

    async def run(
        self,
        job_meta: JobMeta,
        job_dir: Path,
        anthropic_key: str,
    ) -> ClaudeResult:
        """
        Execute a Claude Agent job.

        Args:
            job_meta: Job metadata
            job_dir: Job directory (contains input/ and output/)
            anthropic_key: Anthropic API key for this job

        Returns:
            ClaudeResult with output and metrics

        Raises:
            SDKNotAvailableError: If SDK is not installed
            ExecutionError: If execution fails unexpectedly
        """
        if not self._sdk_available:
            raise SDKNotAvailableError(
                "Claude Agent SDK is not installed. "
                "Install with: pip install claude-agent-sdk"
            )

        job_id = job_meta.job_id
        set_job_context(job_id)

        logger.info(
            "job_execution_starting",
            job_id=job_id,
            model=job_meta.model,
            timeout=job_meta.timeout_seconds,
        )

        # Acquire semaphore to limit concurrency
        async with self._semaphore:
            return await self._execute(
                job_meta=job_meta,
                job_dir=job_dir,
                anthropic_key=anthropic_key,
            )

    async def _execute(
        self,
        job_meta: JobMeta,
        job_dir: Path,
        anthropic_key: str,
    ) -> ClaudeResult:
        """
        Internal execution method.

        This is called after acquiring the semaphore.

        Security:

        - **Process-level sandbox**: If enabled, the entire Claude Code
          CLI is wrapped in a bwrap namespace via a wrapper script set
          as ``cli_path``. All tools and sub-agents execute with
          restricted filesystem access.

        - **can_use_tool callback**: Enforces security profile policies
          (denied tools, MCP server access).
        """
        # Import SDK here to allow the module to load even if SDK is not installed
        try:
            from claude_agent_sdk import (
                ClaudeSDKClient,
                ClaudeAgentOptions,
                AssistantMessage,
                ResultMessage,
                TextBlock,
            )
        except ImportError as e:
            raise SDKNotAvailableError(f"Failed to import Claude Agent SDK: {e}")

        # Note: We use client.receive_response() (NOT receive_messages()).
        # receive_response() terminates after receiving a ResultMessage,
        # while receive_messages() is for ongoing conversations and never stops.

        job_id = job_meta.job_id
        input_dir = job_dir / "input"
        output_dir = job_dir / "output"

        # Ensure output directory exists
        output_dir.mkdir(parents=True, exist_ok=True)

        # =================================================================
        # Resolve security profile
        # =================================================================
        # Profile is resolved at job start (snapshot) so running jobs
        # are unaffected by profile changes (NFR-3).
        profile_manager = get_profile_manager()
        client_profile_name = job_meta.client_id and self._resolve_client_profile(job_meta.client_id)
        if client_profile_name:
            profile = profile_manager.get_profile(client_profile_name)
        else:
            profile = profile_manager.get_default_profile()

        if profile is None:
            profile = profile_manager.get_default_profile()

        logger.info(
            "security_profile_resolved",
            job_id=job_id,
            client_id=job_meta.client_id,
            profile=profile.name,
        )

        # Create security permission handler from profile
        permission_handler = create_permission_handler(
            job_id=job_id,
            profile=profile,
        )

        # =================================================================
        # Reload MCP config from disk (per-job)
        # =================================================================
        # MCP config is reloaded per-job so that servers added/removed
        # via the admin API take effect immediately — matching the
        # per-job reload pattern already used for subagent definitions.
        try:
            mcp_manager = McpManager(self._settings.mcp_servers_file)
            mcp_config = load_mcp_config(mcp_manager)
        except Exception as mcp_exc:
            logger.warning(
                "mcp_config_reload_failed",
                job_id=job_id,
                error=str(mcp_exc),
                error_type=type(mcp_exc).__name__,
                message=(
                    "MCP configuration could not be reloaded from disk. "
                    "Job will proceed without MCP servers."
                ),
            )
            mcp_config = McpConfig()

        # =================================================================
        # Per-job network proxy
        # =================================================================
        # Start a per-job HTTP CONNECT proxy when the profile has network
        # restrictions and network isolation is enabled globally.
        proxy = None
        network_isolated = (
            self._settings.sandbox_network_enabled
            and self._settings.enable_bwrap_sandbox
            and profile.has_network_restrictions()
        )

        if network_isolated:
            try:
                from .sandbox_proxy import get_proxy_manager, parse_upstream_proxy

                # Parse upstream proxy config (once per job — cheap)
                upstream_http = None
                upstream_https = None
                try:
                    upstream_http = parse_upstream_proxy(
                        self._settings.upstream_http_proxy
                    )
                    upstream_https = parse_upstream_proxy(
                        self._settings.upstream_https_proxy
                    )
                except ValueError as exc:
                    logger.error(
                        "upstream_proxy_config_invalid",
                        job_id=job_id,
                        error=str(exc),
                    )
                    raise ExecutionError(
                        f"Invalid upstream proxy configuration: {exc}"
                    ) from exc

                proxy_manager = get_proxy_manager()
                proxy = await proxy_manager.start_proxy(
                    job_id=job_id,
                    job_dir=job_dir,
                    policy=profile.network,
                    upstream_http=upstream_http,
                    upstream_https=upstream_https,
                )
                logger.info(
                    "network_isolation_active",
                    job_id=job_id,
                    profile=profile.name,
                    proxy_socket=str(proxy.socket_path),
                )
            except Exception as proxy_exc:
                # Fail-closed: proxy failure aborts the job
                logger.error(
                    "network_proxy_start_failed",
                    job_id=job_id,
                    error=str(proxy_exc),
                    error_type=type(proxy_exc).__name__,
                    message="Network proxy could not be started. Job aborted (fail-closed).",
                )
                raise ExecutionError(
                    f"Network proxy could not be started: {proxy_exc}"
                ) from proxy_exc
        elif profile.has_network_restrictions():
            if not self._settings.sandbox_network_enabled:
                logger.warning(
                    "network_isolation_disabled_by_config",
                    job_id=job_id,
                    profile=profile.name,
                    message=(
                        "Profile has network restrictions but network isolation "
                        "is disabled (CCAS_SANDBOX_NETWORK_ENABLED=false)."
                    ),
                )
            elif not self._settings.enable_bwrap_sandbox:
                logger.warning(
                    "network_isolation_requires_bwrap",
                    job_id=job_id,
                    profile=profile.name,
                    message=(
                        "Profile has network restrictions but bwrap is disabled. "
                        "Network isolation requires bwrap for --unshare-net."
                    ),
                )

        # =================================================================
        # Process-level bwrap sandbox
        # =================================================================
        # Create a wrapper script that invokes the real Claude CLI inside
        # a bwrap namespace. The SDK's cli_path option is set to this
        # wrapper, so every child process inherits the sandbox.
        sandbox_wrapper_path: Path | None = None
        sandbox_active = False

        if self._settings.enable_bwrap_sandbox:
            try:
                from .sandbox import (
                    create_sandbox_wrapper,
                    cleanup_sandbox_wrapper,
                    SandboxError,
                )

                # Build combined extra_ro_binds list
                extra_ro_binds = list(mcp_config.sandbox_ro_binds or [])

                # Add skills plugin directory if it exists and has skills
                skills_plugin_dir = self._settings.skills_plugin_dir
                skills_subdir = skills_plugin_dir / "skills"
                if skills_subdir.is_dir() and any(
                    skills_subdir.glob("*/SKILL.md")
                ):
                    extra_ro_binds.append(skills_plugin_dir)
                    logger.debug(
                        "skills_plugin_added_to_sandbox_binds",
                        job_id=job_id,
                        path=str(skills_plugin_dir),
                    )

                # Detect seccomp for network-isolated jobs
                seccomp_exec_prefix = None
                if network_isolated:
                    from .sandbox_seccomp import detect_seccomp
                    seccomp_config = detect_seccomp(self._settings.seccomp_dir)
                    if seccomp_config is not None:
                        seccomp_exec_prefix = seccomp_config.exec_prefix()
                        logger.info(
                            "seccomp_enabled_for_job",
                            job_id=job_id,
                            apply_seccomp=str(seccomp_config.apply_seccomp_path),
                            bpf_filter=str(seccomp_config.bpf_filter_path),
                        )
                    else:
                        logger.warning(
                            "seccomp_not_available",
                            job_id=job_id,
                            message=(
                                "seccomp BPF hardening not available. "
                                "Proxy bypass via direct socket creation "
                                "is theoretically possible."
                            ),
                        )

                sandbox_wrapper_path = create_sandbox_wrapper(
                    job_id=job_id,
                    job_dir=job_dir,
                    input_dir=input_dir,
                    data_dir=self._settings.data_dir,
                    bwrap_path=self._settings.bwrap_path,
                    extra_ro_binds=extra_ro_binds or None,
                    network_isolated=network_isolated,
                    proxy_socket_path=proxy.socket_path if proxy else None,
                    seccomp_exec_prefix=seccomp_exec_prefix,
                )
                sandbox_active = True

                logger.info(
                    "process_sandbox_activated",
                    job_id=job_id,
                    wrapper_path=str(sandbox_wrapper_path),
                    network_isolated=network_isolated,
                )

            except Exception as sandbox_exc:
                # Determine whether to fail-closed or fail-open.
                if self._settings.bwrap_allow_unsandboxed_fallback:
                    logger.warning(
                        "process_sandbox_failed_fallback",
                        job_id=job_id,
                        error=str(sandbox_exc),
                        error_type=type(sandbox_exc).__name__,
                        message=(
                            "Process-level sandbox creation failed. "
                            "Executing WITHOUT sandbox (fallback enabled)."
                        ),
                    )
                    # Continue without sandbox (sandbox_wrapper_path stays None)
                else:
                    logger.error(
                        "process_sandbox_failed_aborting",
                        job_id=job_id,
                        error=str(sandbox_exc),
                        error_type=type(sandbox_exc).__name__,
                        message=(
                            "Process-level sandbox creation failed and "
                            "unsandboxed fallback is disabled (fail-closed). "
                            "Job will NOT execute."
                        ),
                    )
                    raise ExecutionError(
                        f"Sandbox creation failed (fail-closed): {sandbox_exc}"
                    ) from sandbox_exc
        else:
            logger.debug(
                "process_sandbox_skipped",
                job_id=job_id,
                reason="bwrap sandbox disabled by configuration",
            )

        # =================================================================
        # Build SDK options
        # =================================================================
        # Base tools always available to Claude
        base_tools = [
            "Read", "Write", "Edit", "Glob", "Grep", "Bash",
            "WebFetch", "WebSearch", "Task", "NotebookEdit",
        ]

        # Remove denied tools from allowed list (primary enforcement).
        # The can_use_tool callback is defense-in-depth but unreliable
        # in acceptEdits mode where the CLI auto-accepts "safe" tools
        # without consulting the callback.
        denied_set = set(profile.denied_tools)
        if denied_set:
            base_tools = [t for t in base_tools if t not in denied_set]
            logger.info(
                "denied_tools_removed_from_allowed_list",
                job_id=job_id,
                profile=profile.name,
                denied=sorted(denied_set),
            )

        # Append MCP tool patterns (e.g., "mcp__sequential-thinking__*")
        allowed_tools = base_tools + mcp_config.allowed_tool_patterns

        # Resolve setting_sources: "none" → None (omit flag),
        # empty string → [] (pass --setting-sources ""),
        # otherwise split by comma.
        raw_sources = self._settings.setting_sources.strip()
        if raw_sources.lower() == "none":
            resolved_setting_sources = None
        elif raw_sources == "":
            resolved_setting_sources = []
        else:
            resolved_setting_sources = [s.strip() for s in raw_sources.split(",") if s.strip()]

        options = ClaudeAgentOptions(
            cwd=str(input_dir),
            allowed_tools=allowed_tools,
            permission_mode="acceptEdits",
            can_use_tool=permission_handler,
            max_turns=200,  # Reasonable limit
            env={"ANTHROPIC_API_KEY": anthropic_key},
            setting_sources=resolved_setting_sources,
        )

        # Attach MCP servers as dict (Path form unusable — /data/ hidden in bwrap)
        # Filter by security profile's allowed_mcp_servers
        if mcp_config.has_servers:
            if profile.allowed_mcp_servers is not None:
                filtered_servers = {
                    name: cfg for name, cfg in mcp_config.servers.items()
                    if name in profile.allowed_mcp_servers
                }
                removed = set(mcp_config.servers.keys()) - set(filtered_servers.keys())
                if removed:
                    logger.info(
                        "mcp_servers_filtered_by_profile",
                        job_id=job_id,
                        profile=profile.name,
                        removed=sorted(removed),
                    )
                # Also filter tool patterns
                filtered_tool_patterns = [
                    p for p in mcp_config.allowed_tool_patterns
                    if any(p.startswith(f"mcp__{name}__") for name in filtered_servers)
                ]
            else:
                filtered_servers = mcp_config.servers
                filtered_tool_patterns = mcp_config.allowed_tool_patterns

            if filtered_servers:
                options.mcp_servers = filtered_servers
                # Replace tool patterns with filtered ones
                allowed_tools = base_tools + filtered_tool_patterns
                options.allowed_tools = allowed_tools

                logger.info(
                    "mcp_servers_attached_to_job",
                    job_id=job_id,
                    server_count=len(filtered_servers),
                    servers=list(filtered_servers.keys()),
                )

        # =================================================================
        # Attach plugin (agents + skills)
        # =================================================================
        # Both agents and skills are delivered via the plugin mechanism
        # (--plugin-dir).  Claude Code discovers agent .md files from
        # the plugin's agents/ subdirectory and skill SKILL.md files
        # from the skills/ subdirectory.  The --plugin-dir flag works
        # independently of --setting-sources (verified experimentally).
        #
        # This replaces the previous approach of passing agents via
        # ClaudeAgentOptions.agents dict (--agents CLI argument), which
        # failed for large agent payloads exceeding the 100KB command
        # line length limit.
        try:
            skills_plugin_dir = self._settings.skills_plugin_dir
            agents_subdir = skills_plugin_dir / "agents"
            skills_subdir = skills_plugin_dir / "skills"

            has_agents = (
                agents_subdir.is_dir()
                and any(agents_subdir.glob("*.md"))
            )
            has_skills = (
                skills_subdir.is_dir()
                and any(skills_subdir.glob("*/SKILL.md"))
            )

            if has_agents or has_skills:
                from .skill_manager import ensure_plugin_manifest

                ensure_plugin_manifest(skills_plugin_dir)

                options.plugins = [
                    {"type": "local", "path": str(skills_plugin_dir)}
                ]

                if has_agents:
                    agent_names = [
                        p.stem for p in agents_subdir.glob("*.md")
                    ]
                    logger.info(
                        "agents_plugin_attached_to_job",
                        job_id=job_id,
                        agent_count=len(agent_names),
                        agents=sorted(agent_names),
                    )

                if has_skills:
                    skill_names = [
                        p.parent.name
                        for p in skills_subdir.glob("*/SKILL.md")
                    ]
                    logger.info(
                        "skills_plugin_attached_to_job",
                        job_id=job_id,
                        skill_count=len(skill_names),
                        skills=sorted(skill_names),
                        plugin_dir=str(skills_plugin_dir),
                    )
        except Exception as plugin_exc:
            logger.warning(
                "plugin_attach_failed",
                job_id=job_id,
                error=str(plugin_exc),
                error_type=type(plugin_exc).__name__,
                message=(
                    "Plugin could not be attached. "
                    "Job will proceed without custom agents and skills."
                ),
            )

        # Set the Claude model if specified in job metadata.
        # The model is always resolved at job creation time (either
        # client-specified or the server default), so job_meta.model
        # should always have a value for newly created jobs.
        # Legacy jobs without a model field (None) will use the SDK default.
        if job_meta.model is not None:
            options.model = job_meta.model
            logger.info(
                "model_configured",
                job_id=job_id,
                model=job_meta.model,
            )
        else:
            logger.debug(
                "model_not_specified_using_sdk_default",
                job_id=job_id,
            )

        # Point the SDK to the sandbox wrapper instead of the real CLI.
        if sandbox_wrapper_path is not None:
            options.cli_path = str(sandbox_wrapper_path)
            logger.debug(
                "cli_path_set_to_sandbox_wrapper",
                job_id=job_id,
                cli_path=str(sandbox_wrapper_path),
            )

        # Build system prompt: always include server preamble.
        # Client's claude_md (if any) is wrapped in delimiters and placed
        # BEFORE the server preamble so that server environment rules
        # take precedence in case of conflict.
        append_parts: list[str] = []
        if job_meta.claude_md:
            append_parts.append(
                "--- BEGIN CLIENT INSTRUCTIONS ---\n"
                f"{job_meta.claude_md}\n"
                "--- END CLIENT INSTRUCTIONS ---"
            )
        append_parts.append(SERVER_SYSTEM_PREAMBLE)

        options.system_prompt = {
            "type": "preset",
            "preset": "claude_code",
            "append": "\n\n".join(append_parts),
        }

        # =================================================================
        # Execute
        # =================================================================
        output_parts: list[str] = []
        result_message: ResultMessage | None = None
        messages_received: list[Any] = []
        start_time = time.monotonic()
        deadline = start_time + job_meta.timeout_seconds
        is_timeout = False
        error: str | None = None

        # Wrap client prompt in delimiters to reduce prompt injection risk.
        # Combined with preamble rule 7, this frames the prompt as untrusted
        # task data rather than system-level instructions.
        framed_prompt = (
            "--- CLIENT TASK ---\n"
            f"{job_meta.prompt}\n"
            "--- END CLIENT TASK ---"
        )

        try:
            async with ClaudeSDKClient(options=options) as client:
                await client.query(framed_prompt)

                async for message in client.receive_response():
                    messages_received.append(message)

                    # Check timeout
                    if time.monotonic() > deadline:
                        logger.warning("job_timeout_reached", job_id=job_id)
                        await client.interrupt()
                        is_timeout = True
                        break

                    # Process message types
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                output_parts.append(block.text)

                    elif isinstance(message, ResultMessage):
                        result_message = message

        except asyncio.CancelledError:
            logger.warning("job_cancelled", job_id=job_id)
            error = "Job was cancelled"
        except Exception as e:
            logger.error("job_execution_error", job_id=job_id, error=str(e))
            error = str(e)
        finally:
            # Clean up per-job proxy
            if proxy is not None:
                try:
                    from .sandbox_proxy import get_proxy_manager
                    await get_proxy_manager().stop_proxy(job_id)
                except Exception as proxy_cleanup_exc:
                    logger.debug(
                        "proxy_cleanup_error",
                        job_id=job_id,
                        error=str(proxy_cleanup_exc),
                    )

            # Clean up sandbox wrapper script (optional — also removed
            # by job directory cleanup, but cleaning up early is tidy).
            if sandbox_wrapper_path is not None:
                try:
                    from .sandbox import cleanup_sandbox_wrapper
                    cleanup_sandbox_wrapper(sandbox_wrapper_path)
                except Exception as cleanup_exc:
                    logger.debug(
                        "sandbox_wrapper_cleanup_error",
                        job_id=job_id,
                        error=str(cleanup_exc),
                    )

        # Calculate duration
        end_time = time.monotonic()
        duration_ms = int((end_time - start_time) * 1000)

        # Build result
        output_text = "\n".join(output_parts)

        result = ClaudeResult(
            output_text=output_text,
            duration_ms=duration_ms,
            is_timeout=is_timeout,
            is_error=error is not None,
            error=error,
            messages=messages_received,
        )

        # Extract metrics from ResultMessage if available
        if result_message:
            result.cost_usd = result_message.total_cost_usd
            result.num_turns = result_message.num_turns
            # Use SDK's duration if available
            if result_message.duration_ms:
                result.duration_ms = result_message.duration_ms

        logger.info(
            "job_execution_completed",
            job_id=job_id,
            model=job_meta.model,
            duration_ms=result.duration_ms,
            cost_usd=result.cost_usd,
            is_timeout=is_timeout,
            is_error=result.is_error,
            output_length=len(output_text),
            sandbox_active=sandbox_active,
        )

        return result

    def get_current_load(self) -> int:
        """
        Get current number of running jobs.

        Returns:
            Number of jobs currently executing
        """
        # Semaphore value shows remaining capacity
        # max - remaining = currently used
        return self._settings.max_concurrent_jobs - self._semaphore._value

    def is_at_capacity(self) -> bool:
        """
        Check if runner is at capacity.

        Returns:
            True if no more jobs can be accepted
        """
        return self._semaphore._value <= 0


# =============================================================================
# Singleton Instance
# =============================================================================

_claude_runner: ClaudeRunner | None = None


def get_claude_runner(
    settings: Settings | None = None,
    mcp_config: McpConfig | None = None,
) -> ClaudeRunner:
    """
    Get the singleton ClaudeRunner instance.

    Args:
        settings: Optional settings (uses get_settings() if not provided)
        mcp_config: Optional pre-loaded MCP configuration

    Returns:
        ClaudeRunner instance
    """
    global _claude_runner
    if _claude_runner is None:
        _claude_runner = ClaudeRunner(settings, mcp_config=mcp_config)
    return _claude_runner


def reset_claude_runner() -> None:
    """Reset the Claude runner (for testing)."""
    global _claude_runner
    _claude_runner = None


# =============================================================================
# Job Execution Function
# =============================================================================


async def execute_job(
    job_meta: JobMeta,
    job_dir: Path,
    anthropic_key: str,
    on_started: Any = None,
    on_completed: Any = None,
    on_failed: Any = None,
) -> ClaudeResult:
    """
    High-level function to execute a job with lifecycle callbacks.

    Args:
        job_meta: Job metadata
        job_dir: Job directory
        anthropic_key: Anthropic API key
        on_started: Async callback when job starts (receives job_id)
        on_completed: Async callback when job completes (receives job_id, result)
        on_failed: Async callback when job fails (receives job_id, error)

    Returns:
        ClaudeResult with execution results
    """
    job_id = job_meta.job_id
    set_job_context(job_id)

    runner = get_claude_runner()

    # Notify started
    if on_started:
        try:
            await on_started(job_id)
        except Exception as e:
            logger.error("on_started_callback_failed", job_id=job_id, error=str(e))

    try:
        result = await runner.run(
            job_meta=job_meta,
            job_dir=job_dir,
            anthropic_key=anthropic_key,
        )

        if result.is_error or result.is_timeout:
            # Notify failed/timeout
            if on_failed:
                try:
                    await on_failed(job_id, result.error or "Timeout")
                except Exception as e:
                    logger.error(
                        "on_failed_callback_failed",
                        job_id=job_id,
                        error=str(e),
                    )
        else:
            # Notify completed
            if on_completed:
                try:
                    await on_completed(job_id, result)
                except Exception as e:
                    logger.error(
                        "on_completed_callback_failed",
                        job_id=job_id,
                        error=str(e),
                    )

        return result

    except Exception as e:
        # Notify failed
        if on_failed:
            try:
                await on_failed(job_id, str(e))
            except Exception as cb_error:
                logger.error(
                    "on_failed_callback_failed",
                    job_id=job_id,
                    error=str(cb_error),
                )

        return ClaudeResult(
            error=str(e),
            is_error=True,
        )
