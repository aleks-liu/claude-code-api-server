"""
Claude Code API Server - Main FastAPI Application.

A REST API server that provides access to Claude Code agent capabilities
for automated pipelines and integrations.
"""

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    HTTPException,
    Path,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import __version__
from .admin_router import router as admin_router
from .auth import AuthManager, ClientInfo, get_anthropic_key, get_auth_manager, get_current_client
from .cleanup import get_cleanup_task, start_cleanup, stop_cleanup
from .claude_runner import ClaudeResult, get_claude_runner
from .config import Settings, ensure_directories, get_settings
from .crypto import encrypt_token
from .models import ClientRole
from .mcp_loader import (
    McpConfig,
    check_all_mcp_servers,
    load_mcp_config,
)
from .mcp_manager import McpManager
from .job_manager import (
    JobAccessDeniedError,
    JobError,
    JobNotFoundError,
    TooManyPendingJobsError,
    get_job_manager,
)
from .logging_config import (
    LogContext,
    get_logger,
    set_request_context,
    setup_logging,
)
from .models import (
    CreateJobRequest,
    CreateJobResponse,
    ErrorResponse,
    HealthResponse,
    JobResponse,
    JobStatus,
    UploadResponse,
    utcnow,
)
from .upload_handler import (
    ArchiveTooLargeError,
    InvalidArchiveError,
    PathTraversalError,
    TooManyFilesError,
    UploadError,
    UploadOwnershipError,
    get_upload_manager,
)

logger = get_logger(__name__)


# =============================================================================
# Application Lifespan
# =============================================================================


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan handler.

    Startup:
    - Initialize logging
    - Create required directories
    - Start background cleanup task

    Shutdown:
    - Stop cleanup task gracefully
    """
    settings = get_settings()

    # Setup logging
    setup_logging(debug=settings.debug, log_level=settings.log_level)

    logger.info(
        "application_starting",
        debug=settings.debug,
        data_dir=str(settings.data_dir),
    )

    # Ensure directories exist
    ensure_directories(settings)

    # ---- Security Profiles -----------------------------------------------
    # Initialize security profiles (creates built-in profiles on first run).
    from .security_profiles import get_profile_manager
    profile_manager = get_profile_manager(settings.security_profiles_file)
    logger.info(
        "security_profiles_ready",
        count=len(profile_manager.list_profiles()),
        default=profile_manager.get_default_profile_name(),
    )
    # ----------------------------------------------------------------------

    # ---- Admin Bootstrap -------------------------------------------------
    if settings.generate_admin_on_first_startup:
        auth_manager = get_auth_manager(settings)
        admins = [c for c in auth_manager.list_clients() if c.role == ClientRole.ADMIN]
        if not admins:
            if not settings.admin_token_encryption_key:
                raise RuntimeError(
                    "CCAS_ADMIN_TOKEN_ENCRYPTION_KEY is required when "
                    "CCAS_GENERATE_ADMIN_ON_FIRST_STARTUP=true"
                )
            try:
                api_key = auth_manager.add_client(
                    client_id="auto-admin",
                    description="Auto-generated admin (first startup)",
                    role=ClientRole.ADMIN,
                )
                encrypted = encrypt_token(api_key, settings.admin_token_encryption_key)
                logger.info(
                    "admin_bootstrap_complete",
                    client_id="auto-admin",
                    encrypted_api_key=encrypted,
                    message=(
                        "Decrypt this value with your private key to obtain "
                        "the admin API token."
                    ),
                )
            except ValueError as e:
                raise RuntimeError(
                    f"Admin bootstrap failed: {e}. Check your "
                    "CCAS_ADMIN_TOKEN_ENCRYPTION_KEY configuration."
                ) from e
    # ----------------------------------------------------------------------

    # ---- MCP health check ------------------------------------------------
    mcp_config = McpConfig()  # Default: no servers

    try:
        mcp_manager = McpManager(settings.mcp_servers_file)
        mcp_config = load_mcp_config(mcp_manager)

        if mcp_config.has_servers:
            results = check_all_mcp_servers(
                mcp_config, settings.mcp_health_check_timeout
            )
            failed = [name for name, ok in results.items() if not ok]

            if failed:
                logger.warning(
                    "mcp_health_check_degraded",
                    failed=failed,
                    message=(
                        "Some MCP servers failed health checks. "
                        "They will be excluded from job execution."
                    ),
                )
                # Remove failed servers from the active config
                for name in failed:
                    mcp_config.servers.pop(name, None)
                # Recompute allowed tool patterns for surviving servers
                mcp_config.allowed_tool_patterns = [
                    f"mcp__{name}__*"
                    for name in mcp_config.servers
                ]

            logger.info(
                "mcp_servers_ready",
                active_count=len(mcp_config.servers),
                servers=list(mcp_config.servers.keys()),
            )
        else:
            logger.info("mcp_no_servers_configured")

    except Exception as e:
        logger.warning(
            "mcp_config_load_failed",
            error=str(e),
            message="MCP configuration could not be loaded. Server will start without MCP support.",
        )

    app.state.mcp_config = mcp_config
    # ----------------------------------------------------------------------

    # ---- Sync agents to plugin directory ---------------------------------
    # Agent .md files are delivered to Claude Code via the plugin mechanism
    # (--plugin-dir), which discovers agents from the agents/ subdirectory.
    # On startup, sync existing files from /data/agents/prompts/ to the
    # plugin agents dir for backward compatibility with pre-migration data.
    try:
        from .agent_manager import AgentManager

        agent_mgr = AgentManager(
            settings.agents_dir,
            plugin_agents_dir=settings.plugin_agents_dir,
        )
        synced = agent_mgr.sync_to_plugin_dir()
        if synced:
            logger.info(
                "agents_startup_sync_complete",
                synced_count=synced,
            )
    except Exception as e:
        logger.warning(
            "agents_startup_sync_failed",
            error=str(e),
            message="Agent files could not be synced to plugin directory.",
        )
    # ----------------------------------------------------------------------

    # Initialize Claude runner with the validated MCP config
    get_claude_runner(settings=settings, mcp_config=mcp_config)

    # Start cleanup task
    start_cleanup()

    logger.info("application_started")

    yield

    # Shutdown
    logger.info("application_stopping")
    await stop_cleanup()
    logger.info("application_stopped")


# =============================================================================
# Request Body Size Limit Middleware
# =============================================================================


class _RequestBodyTooLarge(Exception):
    """Internal exception used by body size middleware."""
    pass


class RequestBodySizeLimitMiddleware:
    """
    ASGI middleware that limits HTTP request body size.

    Tracks bytes received via the ASGI receive channel and returns
    HTTP 413 if the limit is exceeded. The upload endpoint is excluded
    because it has its own streaming size check.
    """

    def __init__(self, app: ASGIApp, max_bytes: int, excluded_paths: set[str] | None = None):
        self.app = app
        self.max_bytes = max_bytes
        self.excluded_paths = excluded_paths or set()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in self.excluded_paths:
            await self.app(scope, receive, send)
            return

        # Check Content-Length header first (fast reject)
        headers = dict(scope.get("headers", []))
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                if int(content_length) > self.max_bytes:
                    await self._send_413(send)
                    return
            except (ValueError, TypeError):
                pass

        # Wrap receive to track bytes
        bytes_received = 0

        async def limited_receive() -> Message:
            nonlocal bytes_received
            message = await receive()
            if message["type"] == "http.request":
                body = message.get("body", b"")
                bytes_received += len(body)
                if bytes_received > self.max_bytes:
                    raise _RequestBodyTooLarge()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _RequestBodyTooLarge:
            await self._send_413(send)

    @staticmethod
    async def _send_413(send: Send) -> None:
        body = b'{"detail":"Request body too large","error_code":"BODY_TOO_LARGE"}'
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [
                [b"content-type", b"application/json"],
                [b"content-length", str(len(body)).encode()],
            ],
        })
        await send({
            "type": "http.response.body",
            "body": body,
        })


# =============================================================================
# FastAPI Application
# =============================================================================

app = FastAPI(
    title="Claude Code API Server",
    description="REST API for Claude Code agent execution",
    version=__version__,
    lifespan=lifespan,
)

# Rate limiting
_settings = get_settings()
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[f"{_settings.rate_limit_rpm}/minute"] if _settings.rate_limit_rpm else [],
    enabled=bool(_settings.rate_limit_rpm),
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# Request body size limit middleware
app.add_middleware(
    RequestBodySizeLimitMiddleware,
    max_bytes=_settings.max_request_body_bytes,
    excluded_paths={"/v1/uploads"},
)

# API v1 router — all endpoints live under /v1/
v1_router = APIRouter(prefix="/v1")
v1_router.include_router(admin_router)


# =============================================================================
# Middleware
# =============================================================================


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    """Log all requests with unique request ID."""
    request_id = str(uuid.uuid4())[:8]
    set_request_context(request_id)

    start_time = utcnow()

    # Log request
    logger.info(
        "request_started",
        method=request.method,
        path=request.url.path,
        request_id=request_id,
    )

    try:
        response = await call_next(request)

        # Log response
        duration_ms = int((utcnow() - start_time).total_seconds() * 1000)
        logger.info(
            "request_completed",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
            request_id=request_id,
        )

        return response

    except Exception as e:
        duration_ms = int((utcnow() - start_time).total_seconds() * 1000)
        logger.error(
            "request_failed",
            method=request.method,
            path=request.url.path,
            error=str(e),
            duration_ms=duration_ms,
            request_id=request_id,
        )
        raise


# =============================================================================
# Exception Handlers
# =============================================================================


@app.exception_handler(UploadError)
async def upload_error_handler(request: Request, exc: UploadError):
    """Handle upload-related errors."""
    if isinstance(exc, InvalidArchiveError):
        status_code = status.HTTP_400_BAD_REQUEST
        error_code = "INVALID_ARCHIVE"
    elif isinstance(exc, ArchiveTooLargeError):
        status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
        error_code = "ARCHIVE_TOO_LARGE"
    elif isinstance(exc, TooManyFilesError):
        status_code = status.HTTP_400_BAD_REQUEST
        error_code = "TOO_MANY_FILES"
    elif isinstance(exc, UploadOwnershipError):
        status_code = status.HTTP_403_FORBIDDEN
        error_code = "UPLOAD_OWNERSHIP_DENIED"
    elif isinstance(exc, PathTraversalError):
        status_code = status.HTTP_400_BAD_REQUEST
        error_code = "PATH_TRAVERSAL"
    else:
        status_code = status.HTTP_400_BAD_REQUEST
        error_code = "UPLOAD_ERROR"

    return JSONResponse(
        status_code=status_code,
        content={"detail": str(exc), "error_code": error_code},
    )


@app.exception_handler(JobError)
async def job_error_handler(request: Request, exc: JobError):
    """Handle job-related errors."""
    if isinstance(exc, JobNotFoundError):
        status_code = status.HTTP_404_NOT_FOUND
        error_code = "JOB_NOT_FOUND"
    elif isinstance(exc, TooManyPendingJobsError):
        status_code = status.HTTP_429_TOO_MANY_REQUESTS
        error_code = "TOO_MANY_PENDING_JOBS"
    else:
        status_code = status.HTTP_400_BAD_REQUEST
        error_code = "JOB_ERROR"

    return JSONResponse(
        status_code=status_code,
        content={"detail": str(exc), "error_code": error_code},
    )


# =============================================================================
# Health Check
# =============================================================================


@v1_router.get(
    "/health",
    response_model=HealthResponse,
    tags=["System"],
    summary="Health check endpoint",
)
async def health_check():
    """
    Basic health check. No authentication required.
    Returns only server availability status.
    """
    return HealthResponse(status="ok")


# =============================================================================
# Upload Endpoint
# =============================================================================


@v1_router.post(
    "/uploads",
    response_model=UploadResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Uploads"],
    summary="Upload an archive",
    responses={
        400: {"model": ErrorResponse, "description": "Invalid archive"},
        401: {"model": ErrorResponse, "description": "Authentication failed"},
        413: {"model": ErrorResponse, "description": "File too large"},
    },
)
async def upload_archive(
    file: Annotated[UploadFile, File(description="ZIP archive to upload")],
    client: Annotated[ClientInfo, Depends(get_current_client)],
):
    """
    Upload a ZIP archive for later processing.

    The archive will be stored temporarily and can be referenced
    when creating a job. Uploads expire after the configured TTL.

    **Limits:**
    - Maximum file size: 50 MB
    - Supported format: ZIP only
    """
    with LogContext(client_id=client.client_id):
        settings = get_settings()

        # Check content type (informational only, we validate actual content)
        if file.content_type and "zip" not in file.content_type.lower():
            logger.warning(
                "upload_content_type_mismatch",
                content_type=file.content_type,
                filename=file.filename,
            )

        # Read file in chunks, enforcing size limit during read
        max_size = settings.max_upload_size_bytes
        chunks: list[bytes] = []
        total_size = 0
        chunk_size = 64 * 1024  # 64 KB

        while True:
            chunk = await file.read(chunk_size)
            if not chunk:
                break
            total_size += len(chunk)
            if total_size > max_size:
                raise ArchiveTooLargeError(
                    f"File size exceeds maximum of {settings.max_upload_size_mb}MB"
                )
            chunks.append(chunk)

        content = b"".join(chunks)

        # Save upload
        upload_manager = get_upload_manager()
        meta = upload_manager.save_upload(
            file_content=content,
            original_filename=file.filename,
            content_type=file.content_type,
            client_id=client.client_id,
        )

        logger.info(
            "upload_created",
            upload_id=meta.upload_id,
            size_bytes=meta.size_bytes,
            client_id=client.client_id,
        )

        return UploadResponse(
            upload_id=meta.upload_id,
            expires_at=meta.expires_at,
            size_bytes=meta.size_bytes,
        )


# =============================================================================
# Job Endpoints
# =============================================================================


@v1_router.post(
    "/jobs",
    response_model=CreateJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["Jobs"],
    summary="Create a new job",
    responses={
        400: {"model": ErrorResponse, "description": "Invalid request"},
        401: {"model": ErrorResponse, "description": "Authentication failed"},
        404: {"model": ErrorResponse, "description": "Upload not found"},
    },
)
async def create_job(
    request: CreateJobRequest,
    background_tasks: BackgroundTasks,
    client: Annotated[ClientInfo, Depends(get_current_client)],
    anthropic_key: Annotated[str, Depends(get_anthropic_key)],
):
    """
    Create a new Claude Code agent job.

    The job will start processing in the background. Use GET /v1/jobs/{job_id}
    to check status and retrieve results.

    **Required headers:**
    - `Authorization: Bearer <your_api_key>` - Server authentication
    - `X-Anthropic-Key: <anthropic_api_key>` - Anthropic API key for Claude
    """
    with LogContext(client_id=client.client_id):
        job_manager = get_job_manager()

        # Create job (extracts archive if upload was provided)
        job_meta = await job_manager.create_job(
            upload_id=request.upload_id,
            prompt=request.prompt,
            client_id=client.client_id,
            claude_md=request.claude_md,
            timeout_seconds=request.timeout_seconds,
            model=request.model,
        )

        # Schedule background execution
        background_tasks.add_task(
            execute_job_background,
            job_meta.job_id,
            anthropic_key,
        )

        logger.info(
            "job_created_and_scheduled",
            job_id=job_meta.job_id,
            client_id=client.client_id,
        )

        return CreateJobResponse(
            job_id=job_meta.job_id,
            status=job_meta.status,
            created_at=job_meta.created_at,
        )


@v1_router.get(
    "/jobs/{job_id}",
    response_model=JobResponse,
    tags=["Jobs"],
    summary="Get job status and results",
    responses={
        401: {"model": ErrorResponse, "description": "Authentication failed"},
        404: {"model": ErrorResponse, "description": "Job not found"},
    },
)
async def get_job(
    job_id: Annotated[str, Path(pattern=r'^job_[a-f0-9]{12}$')],
    client: Annotated[ClientInfo, Depends(get_current_client)],
):
    """
    Get the status and results of a job.

    For completed jobs, the response includes:
    - Output text from Claude
    - Any files written to the output directory (base64 encoded)
    - Cost and duration metrics
    """
    with LogContext(client_id=client.client_id, job_id=job_id):
        job_manager = get_job_manager()

        # Authorization: only the client that created the job can access it.
        # Returns None for both "not found" and "access denied" to prevent
        # job ID enumeration. Detailed reason is logged inside the method.
        job_meta = await job_manager.get_job_for_client(job_id, client.client_id)
        if job_meta is None:
            raise JobNotFoundError(f"Job not found: {job_id}")

        # Get output for completed/failed jobs
        output = None
        if job_meta.status in (
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.TIMEOUT,
        ):
            output = await job_manager.get_output(job_id)

        return job_meta.to_response(output=output)


# Register v1 router (must be after all route definitions above)
app.include_router(v1_router)


# =============================================================================
# Background Job Execution
# =============================================================================


async def execute_job_background(job_id: str, anthropic_key: str) -> None:
    """
    Execute a job in the background.

    This is called as a background task after job creation.

    Flow:
    1. Snapshot input files (for change detection)
    2. Run Claude agent
    3. Collect new/modified files from input/ → output/
    4. Update job status
    """
    job_manager = get_job_manager()
    runner = get_claude_runner()

    with LogContext(job_id=job_id):
        try:
            # Mark job as running
            job_meta = await job_manager.mark_running(job_id)

            # Get job directory
            job_dir = job_manager.get_job_dir(job_id)

            # Snapshot input files BEFORE Claude runs
            input_snapshot = job_manager.snapshot_input_files(job_id)

            # Execute
            result: ClaudeResult = await runner.run(
                job_meta=job_meta,
                job_dir=job_dir,
                anthropic_key=anthropic_key,
            )

            # Collect new/modified files from input/ to output/
            # This runs regardless of success/failure/timeout so that
            # partial results (e.g. files created before timeout) are captured.
            try:
                job_manager.collect_output_files(job_id, input_snapshot)
            except Exception as collect_err:
                logger.error(
                    "output_collection_failed",
                    job_id=job_id,
                    error=str(collect_err),
                )

            # Update job status based on result
            if result.is_timeout:
                await job_manager.mark_timeout(
                    job_id=job_id,
                    output_text=result.output_text,
                )
            elif result.is_error:
                await job_manager.mark_failed(
                    job_id=job_id,
                    error=result.error or "Unknown error",
                    output_text=result.output_text,
                )
            else:
                await job_manager.mark_completed(
                    job_id=job_id,
                    output_text=result.output_text,
                    cost_usd=result.cost_usd,
                    duration_ms=result.duration_ms,
                    num_turns=result.num_turns,
                )

        except Exception as e:
            logger.error(
                "background_job_execution_failed",
                job_id=job_id,
                error=str(e),
            )
            try:
                await job_manager.mark_failed(
                    job_id=job_id,
                    error=str(e),
                )
            except Exception:
                pass


# =============================================================================
# Entry Point
# =============================================================================


def main():
    """Run the server using uvicorn."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "src.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )


if __name__ == "__main__":
    main()
