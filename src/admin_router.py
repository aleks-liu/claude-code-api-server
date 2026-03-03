"""
Admin API Router for Claude Code API Server.

Provides HTTP endpoints for all server administration operations:
- Client management (create, list, get, delete, activate/deactivate, update)
- MCP server management (add, install, list, get, delete, health-check)
- Agent management (add, list, get, update, delete)
- Skill management (add, list, get, update, delete)

All endpoints require admin role authentication.
"""

import asyncio
import base64
import gzip
from typing import Annotated, Any

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Path, Query, Request,
    UploadFile, status,
)

from .auth import AuthManager, ClientInfo, get_admin_client, get_auth_manager
from .agent_manager import (
    AgentExistsError,
    AgentManager,
    AgentNotFoundError,
    AgentValidationError,
    MAX_AGENT_FILE_SIZE,
    parse_agent_file,
)
from .config import get_settings
from .logging_config import get_logger
from .mcp_loader import check_mcp_server, load_mcp_config
from .mcp_manager import McpManager, McpServerExistsError
from .models import (
    MAX_NAME_LENGTH,
    AddAgentRequest,
    AdminStatusResponse,
    AddMcpServerRequest,
    AgentDetailResponse,
    AgentResponse,
    ClientResponse,
    ClientRole,
    CreateClientRequest,
    CreateClientResponse,
    CreateProfileRequest,
    InstallMcpServerRequest,
    McpHealthCheckResponse,
    McpServerResponse,
    ProfileResponse,
    SkillDetailResponse,
    SkillResponse,
    UpdateAgentRequest,
    UpdateClientRequest,
    UpdateProfileRequest,
)
from .security_profiles import (
    SecurityProfileManager,
    ProfileExistsError,
    ProfileNotFoundError,
    ProfileValidationError,
    ProfileDeleteError,
    get_profile_manager,
)
from .skill_manager import (
    SkillExistsError,
    SkillManager,
    SkillNotFoundError,
    SkillValidationError,
)
from .skill_zip_handler import (
    SkillZipError,
    SkillZipSecurityError,
    SkillZipSizeError,
    SkillZipStructureError,
    MAX_SKILL_ZIP_SIZE,
    validate_and_extract_skill_zip,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["Admin"])

# MCP server names that collide with sibling route segments under /admin/mcp/.
_MCP_RESERVED_NAMES = frozenset({"install", "health-check"})


# =============================================================================
# Helper Functions
# =============================================================================


def _decode_base64_content(b64_string: str) -> str:
    """
    Decode a base64-encoded string, with optional gzip decompression.

    Args:
        b64_string: Base64-encoded string, optionally gzip-compressed

    Returns:
        Decoded UTF-8 string

    Raises:
        HTTPException: On decode errors
    """
    try:
        raw_bytes = base64.b64decode(b64_string, validate=True)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid base64 encoding: {exc}",
        )

    # Auto-detect gzip compression
    if raw_bytes[:2] == b"\x1f\x8b":
        try:
            raw_bytes = gzip.decompress(raw_bytes)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to decompress gzip content: {exc}",
            )

    try:
        return raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Decoded content is not valid UTF-8: {exc}",
        )


def _get_content(content: str | None, content_base64: str | None) -> str | None:
    """
    Get content from either raw string or base64-encoded field.

    Exactly one must be provided if content is expected.

    Args:
        content: Raw string content
        content_base64: Base64-encoded content

    Returns:
        Decoded content string, or None if both are None

    Raises:
        HTTPException: If both are provided
    """
    if content is not None and content_base64 is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide either 'content' or 'content_base64', not both",
        )

    if content_base64 is not None:
        return _decode_base64_content(content_base64)

    return content


def _get_auth_manager() -> AuthManager:
    """Get the auth manager."""
    return get_auth_manager()


def _get_mcp_manager() -> McpManager:
    """Get the MCP manager."""
    settings = get_settings()
    return McpManager(settings.mcp_servers_file)


def _get_agent_manager() -> AgentManager:
    """Get the agent manager."""
    settings = get_settings()
    return AgentManager(settings.agents_dir, plugin_agents_dir=settings.plugin_agents_dir)


def _get_profile_manager() -> SecurityProfileManager:
    """Get the security profile manager."""
    return get_profile_manager()


def _get_skill_manager() -> SkillManager:
    """Get the skill manager."""
    settings = get_settings()
    return SkillManager(
        skills_dir=settings.skills_dir,
        meta_dir=settings.skills_meta_dir,
        plugin_dir=settings.skills_plugin_dir,
    )


# =============================================================================
# Status Endpoint
# =============================================================================


@router.get(
    "/status",
    response_model=AdminStatusResponse,
    summary="Detailed server status",
)
async def admin_status(
    request: Request,
    admin: Annotated[ClientInfo, Depends(get_admin_client)],
):
    """Get detailed server status including job counts, version, and MCP status."""
    from .job_manager import get_job_manager
    from .upload_handler import get_upload_manager
    from .mcp_loader import McpConfig

    job_manager = get_job_manager()
    upload_manager = get_upload_manager()

    mcp_config: McpConfig = getattr(request.app.state, "mcp_config", McpConfig())
    mcp_statuses = mcp_config.server_statuses if mcp_config else {}

    return AdminStatusResponse(
        status="ok",
        active_jobs=job_manager.count_active(),
        pending_uploads=upload_manager.count_pending(),
        mcp_servers=mcp_statuses,
    )


# =============================================================================
# Client Endpoints
# =============================================================================


@router.post(
    "/clients",
    response_model=CreateClientResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new client",
)
async def create_client(
    request: CreateClientRequest,
    admin: Annotated[ClientInfo, Depends(get_admin_client)],
):
    """Create a new API client. Returns the API key (shown only once)."""
    auth_manager = _get_auth_manager()
    profile_manager = _get_profile_manager()

    # Validate security profile exists
    if profile_manager.get_profile(request.security_profile) is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Security profile not found: {request.security_profile}",
        )

    try:
        api_key = auth_manager.add_client(
            client_id=request.client_id,
            description=request.description,
            role=request.role,
            security_profile=request.security_profile,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )

    logger.info(
        "admin_client_created",
        client_id=request.client_id,
        role=request.role.value,
        by_admin=admin.client_id,
    )

    return CreateClientResponse(
        client_id=request.client_id,
        api_key=api_key,
        role=request.role,
    )


@router.get(
    "/clients",
    response_model=list[ClientResponse],
    summary="List all clients",
)
async def list_clients(
    admin: Annotated[ClientInfo, Depends(get_admin_client)],
):
    """List all configured API clients."""
    auth_manager = _get_auth_manager()
    clients = auth_manager.list_clients()

    return [
        ClientResponse(
            client_id=c.client_id,
            description=c.description,
            created_at=c.created_at,
            active=c.active,
            role=c.role,
            security_profile=c.security_profile,
        )
        for c in clients
    ]


@router.get(
    "/clients/{client_id}",
    response_model=ClientResponse,
    summary="Get a specific client",
)
async def get_client(
    client_id: Annotated[str, Path(min_length=1, max_length=MAX_NAME_LENGTH, pattern=r'^[a-zA-Z0-9_-]+$')],
    admin: Annotated[ClientInfo, Depends(get_admin_client)],
):
    """Get details of a specific client."""
    auth_manager = _get_auth_manager()
    client = auth_manager.get_client(client_id)

    if client is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Client not found: {client_id}",
        )

    return ClientResponse(
        client_id=client.client_id,
        description=client.description,
        created_at=client.created_at,
        active=client.active,
        role=client.role,
        security_profile=client.security_profile,
    )


@router.delete(
    "/clients/{client_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a client",
)
async def delete_client(
    client_id: Annotated[str, Path(min_length=1, max_length=MAX_NAME_LENGTH, pattern=r'^[a-zA-Z0-9_-]+$')],
    admin: Annotated[ClientInfo, Depends(get_admin_client)],
):
    """Delete a client. Admins cannot delete themselves."""
    auth_manager = _get_auth_manager()

    # Prevent self-deletion
    if client_id == admin.client_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete yourself",
        )

    # Prevent deleting the last admin
    client = auth_manager.get_client(client_id)
    if client and client.role == ClientRole.ADMIN:
        if auth_manager.count_admins() <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot delete the last admin",
            )

    if not auth_manager.remove_client(client_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Client not found: {client_id}",
        )

    logger.info(
        "admin_client_deleted",
        client_id=client_id,
        by_admin=admin.client_id,
    )


@router.post(
    "/clients/{client_id}/activate",
    response_model=ClientResponse,
    summary="Activate a client",
)
async def activate_client(
    client_id: Annotated[str, Path(min_length=1, max_length=MAX_NAME_LENGTH, pattern=r'^[a-zA-Z0-9_-]+$')],
    admin: Annotated[ClientInfo, Depends(get_admin_client)],
):
    """Activate a deactivated client."""
    auth_manager = _get_auth_manager()

    if not auth_manager.activate_client(client_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Client not found: {client_id}",
        )

    client = auth_manager.get_client(client_id)
    logger.info(
        "admin_client_activated",
        client_id=client_id,
        by_admin=admin.client_id,
    )

    return ClientResponse(
        client_id=client.client_id,
        description=client.description,
        created_at=client.created_at,
        active=client.active,
        role=client.role,
        security_profile=client.security_profile,
    )


@router.post(
    "/clients/{client_id}/deactivate",
    response_model=ClientResponse,
    summary="Deactivate a client",
)
async def deactivate_client(
    client_id: Annotated[str, Path(min_length=1, max_length=MAX_NAME_LENGTH, pattern=r'^[a-zA-Z0-9_-]+$')],
    admin: Annotated[ClientInfo, Depends(get_admin_client)],
):
    """Deactivate a client (soft delete)."""
    auth_manager = _get_auth_manager()

    # Prevent self-deactivation
    if client_id == admin.client_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot deactivate yourself",
        )

    # Prevent deactivating the last admin
    client = auth_manager.get_client(client_id)
    if client and client.role == ClientRole.ADMIN:
        if auth_manager.count_admins() <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot deactivate the last admin",
            )

    if not auth_manager.deactivate_client(client_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Client not found: {client_id}",
        )

    client = auth_manager.get_client(client_id)
    logger.info(
        "admin_client_deactivated",
        client_id=client_id,
        by_admin=admin.client_id,
    )

    return ClientResponse(
        client_id=client.client_id,
        description=client.description,
        created_at=client.created_at,
        active=client.active,
        role=client.role,
        security_profile=client.security_profile,
    )


@router.patch(
    "/clients/{client_id}",
    response_model=ClientResponse,
    summary="Update a client",
)
async def update_client(
    client_id: Annotated[str, Path(min_length=1, max_length=MAX_NAME_LENGTH, pattern=r'^[a-zA-Z0-9_-]+$')],
    request: UpdateClientRequest,
    admin: Annotated[ClientInfo, Depends(get_admin_client)],
):
    """Update a client's description, role, or security profile."""
    auth_manager = _get_auth_manager()

    # Prevent demoting the last admin
    if request.role is not None and request.role == ClientRole.CLIENT:
        client = auth_manager.get_client(client_id)
        if client and client.role == ClientRole.ADMIN:
            if auth_manager.count_admins() <= 1:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Cannot demote the last admin",
                )

    # Validate security profile exists
    if request.security_profile is not None:
        profile_manager = _get_profile_manager()
        if profile_manager.get_profile(request.security_profile) is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Security profile not found: {request.security_profile}",
            )

    client = auth_manager.update_client(
        client_id=client_id,
        description=request.description,
        role=request.role,
        security_profile=request.security_profile,
    )

    if client is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Client not found: {client_id}",
        )

    logger.info(
        "admin_client_updated",
        client_id=client_id,
        by_admin=admin.client_id,
    )

    return ClientResponse(
        client_id=client.client_id,
        description=client.description,
        created_at=client.created_at,
        active=client.active,
        role=client.role,
        security_profile=client.security_profile,
    )


# =============================================================================
# Security Profile Endpoints
# =============================================================================


@router.post(
    "/security-profiles",
    response_model=ProfileResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a security profile",
)
async def create_security_profile(
    request: CreateProfileRequest,
    admin: Annotated[ClientInfo, Depends(get_admin_client)],
):
    """Create a new security profile."""
    profile_manager = _get_profile_manager()

    try:
        profile = profile_manager.create_profile(
            name=request.name,
            description=request.description,
            network=request.network,
            denied_tools=request.denied_tools,
            allowed_mcp_servers=request.allowed_mcp_servers,
        )
    except ProfileExistsError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )
    except ProfileValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        )

    logger.info(
        "admin_security_profile_created",
        profile_name=request.name,
        by_admin=admin.client_id,
    )

    return ProfileResponse(
        name=profile.name,
        description=profile.description,
        network=profile.network,
        denied_tools=profile.denied_tools,
        allowed_mcp_servers=profile.allowed_mcp_servers,
        is_builtin=profile.is_builtin,
        is_default=profile.is_default,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


@router.get(
    "/security-profiles",
    response_model=list[ProfileResponse],
    summary="List security profiles",
)
async def list_security_profiles(
    admin: Annotated[ClientInfo, Depends(get_admin_client)],
):
    """List all security profiles."""
    profile_manager = _get_profile_manager()
    profiles = profile_manager.list_profiles()

    return [
        ProfileResponse(
            name=p.name,
            description=p.description,
            network=p.network,
            denied_tools=p.denied_tools,
            allowed_mcp_servers=p.allowed_mcp_servers,
            is_builtin=p.is_builtin,
            is_default=p.is_default,
            created_at=p.created_at,
            updated_at=p.updated_at,
        )
        for p in profiles
    ]


@router.get(
    "/security-profiles/{name}",
    response_model=ProfileResponse,
    summary="Get security profile details",
)
async def get_security_profile(
    name: Annotated[str, Path(min_length=1, max_length=MAX_NAME_LENGTH, pattern=r'^[a-zA-Z0-9-]+$')],
    admin: Annotated[ClientInfo, Depends(get_admin_client)],
):
    """Get details of a specific security profile."""
    profile_manager = _get_profile_manager()
    profile = profile_manager.get_profile(name)

    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Security profile not found: {name}",
        )

    return ProfileResponse(
        name=profile.name,
        description=profile.description,
        network=profile.network,
        denied_tools=profile.denied_tools,
        allowed_mcp_servers=profile.allowed_mcp_servers,
        is_builtin=profile.is_builtin,
        is_default=profile.is_default,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


@router.patch(
    "/security-profiles/{name}",
    response_model=ProfileResponse,
    summary="Update a security profile",
)
async def update_security_profile(
    name: Annotated[str, Path(min_length=1, max_length=MAX_NAME_LENGTH, pattern=r'^[a-zA-Z0-9-]+$')],
    request: UpdateProfileRequest,
    admin: Annotated[ClientInfo, Depends(get_admin_client)],
):
    """Update a security profile. Only provided fields are changed."""
    profile_manager = _get_profile_manager()

    try:
        profile = profile_manager.update_profile(
            name=name,
            description=request.description,
            network=request.network,
            denied_tools=request.denied_tools,
            allowed_mcp_servers=request.allowed_mcp_servers,
            fields_set=request.model_fields_set,
        )
    except ProfileNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )
    except ProfileValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        )

    logger.info(
        "admin_security_profile_updated",
        profile_name=name,
        by_admin=admin.client_id,
    )

    return ProfileResponse(
        name=profile.name,
        description=profile.description,
        network=profile.network,
        denied_tools=profile.denied_tools,
        allowed_mcp_servers=profile.allowed_mcp_servers,
        is_builtin=profile.is_builtin,
        is_default=profile.is_default,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


@router.delete(
    "/security-profiles/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a security profile",
)
async def delete_security_profile(
    name: Annotated[str, Path(min_length=1, max_length=MAX_NAME_LENGTH, pattern=r'^[a-zA-Z0-9-]+$')],
    admin: Annotated[ClientInfo, Depends(get_admin_client)],
):
    """Delete a security profile. Built-in profiles cannot be deleted."""
    profile_manager = _get_profile_manager()
    auth_manager = _get_auth_manager()

    # Find clients assigned to this profile
    assigned = [
        c.client_id for c in auth_manager.list_clients()
        if c.security_profile == name
    ]

    try:
        profile_manager.delete_profile(name, assigned_client_ids=assigned)
    except ProfileNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )
    except ProfileDeleteError as e:
        error_str = str(e)
        if "built-in" in error_str:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_str,
            )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=error_str,
        )

    logger.info(
        "admin_security_profile_deleted",
        profile_name=name,
        by_admin=admin.client_id,
    )


@router.post(
    "/security-profiles/{name}/set-default",
    response_model=ProfileResponse,
    summary="Set default security profile",
)
async def set_default_security_profile(
    name: Annotated[str, Path(min_length=1, max_length=MAX_NAME_LENGTH, pattern=r'^[a-zA-Z0-9-]+$')],
    admin: Annotated[ClientInfo, Depends(get_admin_client)],
):
    """Set a profile as the server-wide default for new clients."""
    profile_manager = _get_profile_manager()

    try:
        profile = profile_manager.set_default(name)
    except ProfileNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )

    logger.info(
        "admin_security_profile_default_set",
        profile_name=name,
        by_admin=admin.client_id,
    )

    return ProfileResponse(
        name=profile.name,
        description=profile.description,
        network=profile.network,
        denied_tools=profile.denied_tools,
        allowed_mcp_servers=profile.allowed_mcp_servers,
        is_builtin=profile.is_builtin,
        is_default=profile.is_default,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


# =============================================================================
# MCP Server Endpoints
# =============================================================================


@router.post(
    "/mcp",
    response_model=McpServerResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add an MCP server manually",
)
async def add_mcp_server(
    request: AddMcpServerRequest,
    admin: Annotated[ClientInfo, Depends(get_admin_client)],
):
    """Add an MCP server with manual configuration."""
    if request.name in _MCP_RESERVED_NAMES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Server name '{request.name}' is reserved",
        )

    mcp_manager = _get_mcp_manager()

    config: dict[str, Any] = {"type": request.type}

    if request.type in ("http", "sse"):
        if not request.url:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"'url' is required for {request.type} servers",
            )
        config["url"] = request.url
        if request.headers:
            config["headers"] = request.headers
    else:
        # stdio
        if not request.command:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="'command' is required for stdio servers",
            )
        config["command"] = request.command
        if request.args:
            config["args"] = request.args
        if request.env:
            config["env"] = request.env

    # Validate the server is operational before persisting
    settings = get_settings()
    is_healthy, detail = await asyncio.to_thread(
        check_mcp_server, request.name, config, settings.mcp_health_check_timeout
    )
    if not is_healthy:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"MCP server '{request.name}' failed health check: {detail}. "
                "Fix the server configuration and try again."
            ),
        )

    try:
        entry = mcp_manager.add_server(
            name=request.name,
            config=config,
            description=request.description,
        )
    except McpServerExistsError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )

    logger.info(
        "admin_mcp_server_added",
        server_name=request.name,
        by_admin=admin.client_id,
    )

    return McpServerResponse(
        name=entry.name,
        type=entry.type,
        description=entry.description,
        package_manager=entry.package_manager,
        package=entry.package,
        added_at=entry.added_at,
    )


@router.post(
    "/mcp/install",
    response_model=McpServerResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Install an MCP server from npm/pip",
)
async def install_mcp_server(
    request: InstallMcpServerRequest,
    admin: Annotated[ClientInfo, Depends(get_admin_client)],
):
    """Install an MCP server from npm or pip package."""
    from .mcp_installer import (
        detect_package_manager,
        derive_server_name,
        install_npm_package,
        install_pip_package,
        InstallError,
    )

    settings = get_settings()
    mcp_manager = _get_mcp_manager()

    package = request.package

    pkg_manager = detect_package_manager(package)
    if request.pip:
        pkg_manager = "pip"

    # Strip pip:// prefix
    if pkg_manager == "pip" and package.startswith("pip://"):
        package = package[6:]

    server_name = request.name if request.name else derive_server_name(package, pkg_manager)

    if server_name in _MCP_RESERVED_NAMES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Server name '{server_name}' is reserved",
        )

    if mcp_manager.get_server(server_name) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"MCP server '{server_name}' already exists",
        )

    mcp_dir = settings.mcp_dir
    installer = install_npm_package if pkg_manager == "npm" else install_pip_package

    try:
        entry = await asyncio.to_thread(
            installer, mcp_manager, mcp_dir, package, server_name, request.description
        )
    except InstallError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(e),
        )
    except McpServerExistsError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )

    logger.info(
        "admin_mcp_server_installed",
        server_name=server_name,
        package=package,
        package_manager=pkg_manager,
        by_admin=admin.client_id,
    )

    return McpServerResponse(
        name=entry.name,
        type=entry.type,
        description=entry.description,
        package_manager=entry.package_manager,
        package=entry.package,
        added_at=entry.added_at,
    )


@router.get(
    "/mcp",
    response_model=list[McpServerResponse],
    summary="List MCP servers",
)
async def list_mcp_servers(
    admin: Annotated[ClientInfo, Depends(get_admin_client)],
):
    """List all configured MCP servers."""
    mcp_manager = _get_mcp_manager()
    servers = mcp_manager.list_servers()

    return [
        McpServerResponse(
            name=s.name,
            type=s.type,
            description=s.description,
            package_manager=s.package_manager,
            package=s.package,
            added_at=s.added_at,
        )
        for s in servers
    ]


@router.get(
    "/mcp/{name}",
    response_model=McpServerResponse,
    summary="Get MCP server details",
)
async def get_mcp_server(
    name: Annotated[str, Path(min_length=1, max_length=MAX_NAME_LENGTH, pattern=r'^[a-zA-Z0-9_-]+$')],
    admin: Annotated[ClientInfo, Depends(get_admin_client)],
):
    """Get details of a specific MCP server."""
    mcp_manager = _get_mcp_manager()
    server = mcp_manager.get_server(name)

    if server is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"MCP server not found: {name}",
        )

    return McpServerResponse(
        name=server.name,
        type=server.type,
        description=server.description,
        package_manager=server.package_manager,
        package=server.package,
        added_at=server.added_at,
    )


@router.delete(
    "/mcp/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove MCP server",
)
async def remove_mcp_server(
    name: Annotated[str, Path(min_length=1, max_length=MAX_NAME_LENGTH, pattern=r'^[a-zA-Z0-9_-]+$')],
    admin: Annotated[ClientInfo, Depends(get_admin_client)],
    keep_package: Annotated[bool, Query()] = False,
):
    """Remove an MCP server. Optionally keep the installed package."""
    settings = get_settings()
    mcp_manager = _get_mcp_manager()

    entry = mcp_manager.remove_server(name)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"MCP server not found: {name}",
        )

    # Uninstall package if requested
    if not keep_package and entry.package_manager and entry.package:
        from .mcp_installer import uninstall_package
        await asyncio.to_thread(uninstall_package, settings.mcp_dir, entry)

    logger.info(
        "admin_mcp_server_removed",
        server_name=name,
        keep_package=keep_package,
        by_admin=admin.client_id,
    )


@router.post(
    "/mcp/health-check",
    response_model=list[McpHealthCheckResponse],
    summary="Health check all MCP servers",
)
async def health_check_all_mcp_servers(
    admin: Annotated[ClientInfo, Depends(get_admin_client)],
):
    """Run health check on all configured MCP servers."""
    mcp_manager = _get_mcp_manager()
    mcp_config = load_mcp_config(mcp_manager)

    if not mcp_config.has_servers:
        return []

    results = []
    for name, config in mcp_config.servers.items():
        is_healthy, detail = check_mcp_server(name, config, timeout=15)
        results.append(McpHealthCheckResponse(
            name=name,
            healthy=is_healthy,
            detail=detail,
        ))

    return results


@router.post(
    "/mcp/{name}/health-check",
    response_model=McpHealthCheckResponse,
    summary="Health check specific MCP server",
)
async def health_check_mcp_server(
    name: Annotated[str, Path(min_length=1, max_length=MAX_NAME_LENGTH, pattern=r'^[a-zA-Z0-9_-]+$')],
    admin: Annotated[ClientInfo, Depends(get_admin_client)],
):
    """Run health check on a specific MCP server."""
    mcp_manager = _get_mcp_manager()
    mcp_config = load_mcp_config(mcp_manager)

    if name not in mcp_config.servers:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"MCP server not found: {name}",
        )

    config = mcp_config.servers[name]
    is_healthy, detail = check_mcp_server(name, config, timeout=15)

    return McpHealthCheckResponse(
        name=name,
        healthy=is_healthy,
        detail=detail,
    )


# =============================================================================
# Agent Endpoints
# =============================================================================


@router.post(
    "/agents",
    response_model=AgentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a subagent",
)
async def add_agent(
    request: AddAgentRequest,
    admin: Annotated[ClientInfo, Depends(get_admin_client)],
):
    """Add a new subagent definition."""
    agent_manager = _get_agent_manager()

    content = _get_content(request.content, request.content_base64)
    if content is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either 'content' or 'content_base64' must be provided",
        )

    # Size check
    content_bytes = len(content.encode("utf-8"))
    if content_bytes > MAX_AGENT_FILE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Content too large ({content_bytes:,} bytes, max {MAX_AGENT_FILE_SIZE:,})",
        )

    try:
        entry = agent_manager.add_agent(
            name=request.name,
            content=content,
            description=request.description,
        )
    except AgentExistsError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )
    except AgentValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        )

    logger.info(
        "admin_agent_added",
        agent_name=request.name,
        by_admin=admin.client_id,
    )

    return AgentResponse(
        name=entry.name,
        description=entry.description,
        prompt_size_bytes=entry.prompt_size_bytes,
        added_at=entry.added_at,
    )


@router.get(
    "/agents",
    response_model=list[AgentResponse],
    summary="List agents",
)
async def list_agents(
    admin: Annotated[ClientInfo, Depends(get_admin_client)],
):
    """List all configured subagents."""
    agent_manager = _get_agent_manager()
    agents = agent_manager.list_agents()

    return [
        AgentResponse(
            name=a.name,
            description=a.description,
            prompt_size_bytes=a.prompt_size_bytes,
            added_at=a.added_at,
        )
        for a in agents
    ]


@router.get(
    "/agents/{name}",
    response_model=AgentDetailResponse,
    summary="Get agent details",
)
async def get_agent(
    name: Annotated[str, Path(min_length=1, max_length=MAX_NAME_LENGTH, pattern=r'^[a-zA-Z0-9-]+$')],
    admin: Annotated[ClientInfo, Depends(get_admin_client)],
):
    """Get detailed information about a specific agent."""
    agent_manager = _get_agent_manager()
    entry, content = agent_manager.get_agent(name)

    if content is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {name}",
        )

    # Parse frontmatter
    frontmatter = None
    body_preview = content
    try:
        frontmatter, body = parse_agent_file(content)
        body_lines = body.split("\n")
        body_preview = "\n".join(body_lines[:50])
        if len(body_lines) > 50:
            body_preview += f"\n... ({len(body_lines) - 50} more lines)"
    except AgentValidationError:
        pass

    return AgentDetailResponse(
        name=entry.name if entry else name,
        description=entry.description if entry else "",
        prompt_size_bytes=entry.prompt_size_bytes if entry else len(content.encode("utf-8")),
        added_at=entry.added_at if entry else None,
        frontmatter=frontmatter,
        body_preview=body_preview,
    )


@router.put(
    "/agents/{name}",
    response_model=AgentResponse,
    summary="Update agent",
)
async def update_agent(
    name: Annotated[str, Path(min_length=1, max_length=MAX_NAME_LENGTH, pattern=r'^[a-zA-Z0-9-]+$')],
    request: UpdateAgentRequest,
    admin: Annotated[ClientInfo, Depends(get_admin_client)],
):
    """Update an existing agent definition."""
    agent_manager = _get_agent_manager()

    content = _get_content(request.content, request.content_base64)

    if content is None and request.description is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Nothing to update. Provide 'content', 'content_base64', or 'description'",
        )

    # Size check
    if content is not None:
        content_bytes = len(content.encode("utf-8"))
        if content_bytes > MAX_AGENT_FILE_SIZE:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Content too large ({content_bytes:,} bytes, max {MAX_AGENT_FILE_SIZE:,})",
            )

    try:
        entry = agent_manager.update_agent(
            name=name,
            content=content,
            description=request.description,
        )
    except AgentNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )
    except AgentValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        )

    logger.info(
        "admin_agent_updated",
        agent_name=name,
        by_admin=admin.client_id,
    )

    return AgentResponse(
        name=entry.name,
        description=entry.description,
        prompt_size_bytes=entry.prompt_size_bytes,
        added_at=entry.added_at,
    )


@router.delete(
    "/agents/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove agent",
)
async def remove_agent(
    name: Annotated[str, Path(min_length=1, max_length=MAX_NAME_LENGTH, pattern=r'^[a-zA-Z0-9-]+$')],
    admin: Annotated[ClientInfo, Depends(get_admin_client)],
):
    """Remove a subagent definition."""
    agent_manager = _get_agent_manager()

    entry = agent_manager.remove_agent(name)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {name}",
        )

    logger.info(
        "admin_agent_removed",
        agent_name=name,
        by_admin=admin.client_id,
    )


# =============================================================================
# Skill Endpoints
# =============================================================================


@router.post(
    "/skills",
    response_model=SkillResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a skill",
)
async def add_skill(
    admin: Annotated[ClientInfo, Depends(get_admin_client)],
    skill_data: Annotated[UploadFile, File(description="ZIP archive of skill directory")],
    name: Annotated[str | None, Form(description="Override skill name (default: from ZIP root dir)")] = None,
):
    """
    Add a new skill from a ZIP archive.

    The ZIP must contain a skill directory with at least SKILL.md.
    Additional subdirectories are allowed.
    """
    skill_manager = _get_skill_manager()
    settings = get_settings()

    # Stream-read with size enforcement
    max_size = MAX_SKILL_ZIP_SIZE
    chunks: list[bytes] = []
    total_size = 0
    chunk_size = 64 * 1024

    while True:
        chunk = await skill_data.read(chunk_size)
        if not chunk:
            break
        total_size += len(chunk)
        if total_size > max_size:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"ZIP archive exceeds maximum size of {max_size / (1024 * 1024):.0f} MB",
            )
        chunks.append(chunk)

    zip_bytes = b"".join(chunks)
    if not zip_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empty file uploaded",
        )

    # Validate and extract
    result = None
    try:
        result = validate_and_extract_skill_zip(
            zip_bytes,
            name_override=name,
            temp_parent=settings.skills_dir,
        )

        entry = skill_manager.add_skill(
            name=result.skill_name,
            source_dir=result.temp_dir,
            skill_md_content=result.skill_md_content,
            file_count=result.file_count,
            total_size_bytes=result.total_size_bytes,
            file_listing=result.file_listing,
        )

        logger.info(
            "admin_skill_added",
            skill_name=result.skill_name,
            file_count=result.file_count,
            by_admin=admin.client_id,
        )

        return SkillResponse(
            name=entry.name,
            description=entry.description,
            skill_size_bytes=entry.skill_size_bytes,
            file_count=entry.file_count,
            added_at=entry.added_at,
        )

    except SkillExistsError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(e),
        )
    except SkillValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        )
    except SkillZipSizeError as e:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=str(e),
        )
    except SkillZipSecurityError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except SkillZipStructureError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    except SkillZipError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    finally:
        # Cleanup temp dir if it still exists (not moved by add_skill)
        if result is not None:
            result.cleanup()


@router.get(
    "/skills",
    response_model=list[SkillResponse],
    summary="List skills",
)
async def list_skills(
    admin: Annotated[ClientInfo, Depends(get_admin_client)],
):
    """List all configured skills."""
    skill_manager = _get_skill_manager()
    skills = skill_manager.list_skills()

    return [
        SkillResponse(
            name=s.name,
            description=s.description,
            skill_size_bytes=s.skill_size_bytes,
            file_count=s.file_count,
            added_at=s.added_at,
        )
        for s in skills
    ]


@router.get(
    "/skills/{name}",
    response_model=SkillDetailResponse,
    summary="Get skill details",
)
async def get_skill(
    name: Annotated[str, Path(min_length=1, max_length=MAX_NAME_LENGTH, pattern=r'^[a-zA-Z0-9-]+$')],
    admin: Annotated[ClientInfo, Depends(get_admin_client)],
):
    """Get detailed information about a specific skill."""
    skill_manager = _get_skill_manager()
    entry, content, file_listing = skill_manager.get_skill(name)

    if content is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Skill not found: {name}",
        )

    # Parse frontmatter
    frontmatter = None
    body_preview = content
    try:
        frontmatter, body = parse_agent_file(content)
        body_lines = body.split("\n")
        body_preview = "\n".join(body_lines[:50])
        if len(body_lines) > 50:
            body_preview += f"\n... ({len(body_lines) - 50} more lines)"
    except AgentValidationError:
        pass

    return SkillDetailResponse(
        name=entry.name if entry else name,
        description=entry.description if entry else "",
        skill_size_bytes=entry.skill_size_bytes if entry else len(content.encode("utf-8")),
        file_count=entry.file_count if entry else len(file_listing),
        added_at=entry.added_at if entry else None,
        frontmatter=frontmatter,
        body_preview=body_preview,
        file_listing=file_listing,
    )


@router.put(
    "/skills/{name}",
    response_model=SkillResponse,
    summary="Update skill",
)
async def update_skill(
    name: Annotated[str, Path(min_length=1, max_length=MAX_NAME_LENGTH, pattern=r'^[a-zA-Z0-9-]+$')],
    admin: Annotated[ClientInfo, Depends(get_admin_client)],
    skill_data: Annotated[UploadFile, File(description="ZIP archive of skill directory")],
):
    """
    Replace an existing skill with a new ZIP archive.

    Full replacement — the entire skill directory is swapped atomically.
    """
    skill_manager = _get_skill_manager()
    settings = get_settings()

    # Stream-read with size enforcement
    max_size = MAX_SKILL_ZIP_SIZE
    chunks: list[bytes] = []
    total_size = 0
    chunk_size = 64 * 1024

    while True:
        chunk = await skill_data.read(chunk_size)
        if not chunk:
            break
        total_size += len(chunk)
        if total_size > max_size:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"ZIP archive exceeds maximum size of {max_size / (1024 * 1024):.0f} MB",
            )
        chunks.append(chunk)

    zip_bytes = b"".join(chunks)
    if not zip_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empty file uploaded",
        )

    # Validate and extract
    result = None
    try:
        result = validate_and_extract_skill_zip(
            zip_bytes,
            name_override=name,
            temp_parent=settings.skills_dir,
        )

        entry = skill_manager.update_skill(
            name=name,
            source_dir=result.temp_dir,
            skill_md_content=result.skill_md_content,
            file_count=result.file_count,
            total_size_bytes=result.total_size_bytes,
            file_listing=result.file_listing,
        )

        logger.info(
            "admin_skill_updated",
            skill_name=name,
            file_count=result.file_count,
            by_admin=admin.client_id,
        )

        return SkillResponse(
            name=entry.name,
            description=entry.description,
            skill_size_bytes=entry.skill_size_bytes,
            file_count=entry.file_count,
            added_at=entry.added_at,
        )

    except SkillNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )
    except SkillValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        )
    except SkillZipSizeError as e:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=str(e),
        )
    except (SkillZipSecurityError, SkillZipStructureError, SkillZipError) as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    finally:
        if result is not None:
            result.cleanup()


@router.delete(
    "/skills/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove skill",
)
async def remove_skill(
    name: Annotated[str, Path(min_length=1, max_length=MAX_NAME_LENGTH, pattern=r'^[a-zA-Z0-9-]+$')],
    admin: Annotated[ClientInfo, Depends(get_admin_client)],
):
    """Remove a skill definition."""
    skill_manager = _get_skill_manager()

    entry = skill_manager.remove_skill(name)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Skill not found: {name}",
        )

    logger.info(
        "admin_skill_removed",
        skill_name=name,
        by_admin=admin.client_id,
    )
