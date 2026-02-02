"""
Authentication module for Claude Code API Server.

Handles API key generation, storage, and verification using argon2
password hashing for secure key storage.
"""

import re
import secrets
import string
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated

import argon2
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, Header, HTTPException, status

from .config import Settings, get_settings
from .logging_config import get_logger
from .models import ClientAuth, ClientRole, ClientsFile, utcnow

logger = get_logger(__name__)

# Argon2 hasher with secure defaults
_hasher = PasswordHasher(
    time_cost=3,        # Number of iterations
    memory_cost=65536,  # Memory usage in KiB (64 MB)
    parallelism=4,      # Number of parallel threads
    hash_len=32,        # Length of the hash in bytes
    salt_len=16,        # Length of the salt in bytes
)

# Token format constants
_TOKEN_PREFIX = "ccas_"
_KEY_ID_LEN = 8
_KEY_ID_RE = re.compile(r'^[a-zA-Z0-9]{8}$')
_KEY_ID_ALPHABET = string.ascii_letters + string.digits
# Minimum token length: "ccas_" (5) + key_id (8) + "_" (1) + at least 1 char secret
_MIN_TOKEN_LEN = len(_TOKEN_PREFIX) + _KEY_ID_LEN + 1 + 1


@dataclass
class ClientInfo:
    """Authenticated client identity returned by auth dependencies."""

    client_id: str
    role: ClientRole


class AuthManager:
    """
    Manages client authentication and API key storage.

    API keys are stored as argon2 hashes in a JSON file.
    """

    def __init__(self, clients_file: Path):
        """
        Initialize the auth manager.

        Args:
            clients_file: Path to the clients.json file
        """
        self._clients_file = clients_file
        self._clients: dict[str, ClientAuth] = {}
        self._key_id_index: dict[str, ClientAuth] = {}
        self._load_clients()

    def _load_clients(self) -> None:
        """Load clients from the JSON file and build key_id index."""
        if not self._clients_file.exists():
            logger.info("clients_file_not_found", path=str(self._clients_file))
            self._clients = {}
            self._key_id_index = {}
            return

        try:
            content = self._clients_file.read_text(encoding="utf-8")
            if not content.strip():
                self._clients = {}
                self._key_id_index = {}
                return

            clients_data = ClientsFile.model_validate_json(content)
            self._clients = {c.client_id: c for c in clients_data.clients}

            # Migration: assign default profile to clients missing it
            migrated = 0
            for client in self._clients.values():
                if not hasattr(client, "security_profile") or not client.security_profile:
                    client.security_profile = "common"
                    migrated += 1
            if migrated > 0:
                logger.info(
                    "client_profile_migrated",
                    count=migrated,
                    default_profile="common",
                )
                self._save_clients()

            # Build key_id -> client index for O(1) token lookup
            self._key_id_index = {c.key_id: c for c in self._clients.values()}

            logger.info(
                "clients_loaded",
                count=len(self._clients),
                path=str(self._clients_file),
            )
        except Exception as e:
            logger.error(
                "clients_load_failed",
                error=str(e),
                path=str(self._clients_file),
            )
            raise RuntimeError(f"Failed to load clients file: {e}") from e

    def _save_clients(self) -> None:
        """Save clients to the JSON file."""
        try:
            clients_data = ClientsFile(clients=list(self._clients.values()))
            # Ensure parent directory exists
            self._clients_file.parent.mkdir(parents=True, exist_ok=True)
            self._clients_file.write_text(
                clients_data.model_dump_json(indent=2),
                encoding="utf-8",
            )
            logger.info(
                "clients_saved",
                count=len(self._clients),
                path=str(self._clients_file),
            )
        except Exception as e:
            logger.error(
                "clients_save_failed",
                error=str(e),
                path=str(self._clients_file),
            )
            raise RuntimeError(f"Failed to save clients file: {e}") from e

    def verify_key(self, api_key: str) -> ClientInfo | None:
        """
        Verify an API key and return client info if valid.

        Uses the embedded key_id in the token for O(1) client lookup,
        then performs a single Argon2 verification.

        Token format: ccas_{key_id}_{secret}
        - token[0:5]  = "ccas_"
        - token[5:13] = key_id (8 alphanumeric chars)
        - token[13]   = "_"
        - token[14:]  = secret

        Args:
            api_key: The API key to verify

        Returns:
            ClientInfo if key is valid and client is active, None otherwise
        """
        if not api_key or len(api_key) < _MIN_TOKEN_LEN:
            return None

        # Validate token structure
        if not api_key.startswith(_TOKEN_PREFIX):
            logger.debug("auth_failed", reason="invalid_token_prefix")
            return None

        key_id = api_key[5:13]
        if not _KEY_ID_RE.match(key_id):
            logger.debug("auth_failed", reason="invalid_key_id_format")
            return None

        if api_key[13] != "_":
            logger.debug("auth_failed", reason="missing_key_id_separator")
            return None

        # O(1) client lookup by key_id
        client = self._key_id_index.get(key_id)
        if client is None:
            logger.debug("auth_failed", reason="key_id_not_found")
            return None

        if not client.active:
            logger.debug("auth_failed", reason="client_inactive", client_id=client.client_id)
            return None

        # Single Argon2 verification
        try:
            _hasher.verify(client.key_hash, api_key)
        except VerifyMismatchError:
            logger.debug("auth_failed", reason="key_mismatch", client_id=client.client_id)
            return None
        except argon2.exceptions.InvalidHashError:
            logger.warning("invalid_hash_format", client_id=client.client_id)
            return None

        logger.debug("auth_success", client_id=client.client_id)

        # Check if rehash is needed (argon2 parameters changed)
        if _hasher.check_needs_rehash(client.key_hash):
            logger.info("rehashing_key", client_id=client.client_id)
            client.key_hash = _hasher.hash(api_key)
            self._save_clients()

        return ClientInfo(
            client_id=client.client_id,
            role=client.role,
        )

    def _generate_key_id(self) -> str:
        """
        Generate a unique 8-character alphanumeric key_id.

        Returns:
            Unique key_id string

        Raises:
            RuntimeError: If unable to generate a unique key_id after max attempts
        """
        for _ in range(10):
            key_id = "".join(secrets.choice(_KEY_ID_ALPHABET) for _ in range(_KEY_ID_LEN))
            if key_id not in self._key_id_index:
                return key_id
        raise RuntimeError("Failed to generate unique key_id after 10 attempts")

    def generate_api_key(
        self,
        client_id: str,
        description: str = "",
    ) -> tuple[str, str, str]:
        """
        Generate a new API key for a client.

        Token format: ccas_{key_id}_{secret}

        Args:
            client_id: Unique identifier for the client
            description: Human-readable description

        Returns:
            Tuple of (api_key, key_hash, key_id)

        Raises:
            ValueError: If client_id already exists
        """
        if client_id in self._clients:
            raise ValueError(f"Client {client_id} already exists")

        key_id = self._generate_key_id()

        # Generate secure random secret and compose token
        raw_secret = secrets.token_urlsafe(32)
        api_key = f"{_TOKEN_PREFIX}{key_id}_{raw_secret}"

        # Hash the full token
        key_hash = _hasher.hash(api_key)

        return api_key, key_hash, key_id

    def add_client(
        self,
        client_id: str,
        description: str = "",
        role: ClientRole = ClientRole.CLIENT,
        security_profile: str = "common",
    ) -> str:
        """
        Add a new client and generate their API key.

        Args:
            client_id: Unique identifier for the client
            description: Human-readable description
            role: Client role (admin or client)
            security_profile: Security profile name to assign

        Returns:
            The generated API key (only returned once!)

        Raises:
            ValueError: If client_id already exists
        """
        api_key, key_hash, key_id = self.generate_api_key(client_id, description)

        client = ClientAuth(
            client_id=client_id,
            key_id=key_id,
            key_hash=key_hash,
            description=description,
            created_at=utcnow(),
            active=True,
            role=role,
            security_profile=security_profile,
        )

        self._clients[client_id] = client
        self._key_id_index[key_id] = client
        self._save_clients()

        logger.info(
            "client_added",
            client_id=client_id,
            description=description,
            role=role.value,
        )

        return api_key

    def remove_client(self, client_id: str) -> bool:
        """
        Remove a client.

        Args:
            client_id: Client to remove

        Returns:
            True if client was removed, False if not found
        """
        if client_id not in self._clients:
            return False

        client = self._clients.pop(client_id)
        self._key_id_index.pop(client.key_id, None)
        self._save_clients()

        logger.info("client_removed", client_id=client_id)
        return True

    def deactivate_client(self, client_id: str) -> bool:
        """
        Deactivate a client (soft delete).

        Args:
            client_id: Client to deactivate

        Returns:
            True if client was deactivated, False if not found
        """
        if client_id not in self._clients:
            return False

        self._clients[client_id].active = False
        self._save_clients()

        logger.info("client_deactivated", client_id=client_id)
        return True

    def activate_client(self, client_id: str) -> bool:
        """
        Reactivate a deactivated client.

        Args:
            client_id: Client to activate

        Returns:
            True if client was activated, False if not found
        """
        if client_id not in self._clients:
            return False

        self._clients[client_id].active = True
        self._save_clients()

        logger.info("client_activated", client_id=client_id)
        return True

    def list_clients(self) -> list[ClientAuth]:
        """
        List all clients (without exposing key hashes).

        Returns:
            List of client info (key_hash is included but should not be exposed)
        """
        return list(self._clients.values())

    def get_client(self, client_id: str) -> ClientAuth | None:
        """
        Get a specific client by ID.

        Args:
            client_id: Client ID to look up

        Returns:
            Client info if found, None otherwise
        """
        return self._clients.get(client_id)

    def update_client(
        self,
        client_id: str,
        description: str | None = None,
        role: ClientRole | None = None,
        security_profile: str | None = None,
    ) -> ClientAuth | None:
        """
        Update a client's metadata.

        Args:
            client_id: Client ID to update
            description: New description (optional)
            role: New role (optional)
            security_profile: New security profile name (optional)

        Returns:
            Updated client if found, None otherwise
        """
        if client_id not in self._clients:
            return None

        client = self._clients[client_id]
        if description is not None:
            client.description = description
        if role is not None:
            client.role = role
        if security_profile is not None:
            client.security_profile = security_profile
        self._save_clients()

        logger.info(
            "client_updated",
            client_id=client_id,
            description_updated=description is not None,
            role_updated=role is not None,
        )

        return client

    def count_admins(self) -> int:
        """
        Count the number of admin clients.

        Returns:
            Number of clients with admin role
        """
        return sum(1 for c in self._clients.values() if c.role == ClientRole.ADMIN)

    def reload(self) -> None:
        """Reload clients from disk."""
        self._load_clients()


# =============================================================================
# Singleton instance
# =============================================================================

_auth_manager: AuthManager | None = None


def get_auth_manager(settings: Settings | None = None) -> AuthManager:
    """
    Get the singleton AuthManager instance.

    Args:
        settings: Optional settings (uses get_settings() if not provided)

    Returns:
        AuthManager instance
    """
    global _auth_manager
    if _auth_manager is None:
        if settings is None:
            settings = get_settings()
        _auth_manager = AuthManager(settings.clients_file)
    return _auth_manager


def reset_auth_manager() -> None:
    """Reset the auth manager (for testing)."""
    global _auth_manager
    _auth_manager = None


# =============================================================================
# FastAPI Dependencies
# =============================================================================


async def get_current_client(
    authorization: Annotated[str | None, Header()] = None,
) -> ClientInfo:
    """
    FastAPI dependency to validate Authorization header and return client info.

    Usage:
        @app.get("/protected")
        async def protected(client: ClientInfo = Depends(get_current_client)):
            ...

    Raises:
        HTTPException: 401 if authentication fails
    """
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Extract token from "Bearer <token>" format
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization header format. Expected: Bearer <token>",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = parts[1]

    auth_manager = get_auth_manager()
    client_info = auth_manager.verify_key(token)

    if client_info is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return client_info


async def get_admin_client(
    client: ClientInfo = Depends(get_current_client),
) -> ClientInfo:
    """
    FastAPI dependency to validate Authorization header and require admin role.

    Usage:
        @app.get("/v1/admin/protected")
        async def admin_protected(client: ClientInfo = Depends(get_admin_client)):
            ...

    Raises:
        HTTPException: 401 if authentication fails, 403 if not admin
    """
    if client.role != ClientRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return client


async def get_anthropic_key(
    x_anthropic_key: Annotated[str | None, Header()] = None,
) -> str:
    """
    FastAPI dependency to extract Anthropic API key from header.

    Usage:
        @app.post("/v1/jobs")
        async def create_job(anthropic_key: str = Depends(get_anthropic_key)):
            ...

    Raises:
        HTTPException: 400 if header is missing or empty
    """
    if not x_anthropic_key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing X-Anthropic-Key header",
        )

    if not x_anthropic_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Anthropic-Key header cannot be empty",
        )

    # Basic validation: Anthropic keys typically start with "sk-ant-"
    # But don't enforce strictly in case format changes
    if len(x_anthropic_key) < 20:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Anthropic-Key appears to be invalid (too short)",
        )

    return x_anthropic_key.strip()


# =============================================================================
# CLI Utility Functions
# =============================================================================


def cli_add_client(
    client_id: str,
    description: str = "",
    clients_file: Path | None = None,
) -> str:
    """
    CLI utility to add a new client.

    Args:
        client_id: Unique client identifier
        description: Human-readable description
        clients_file: Path to clients.json (uses default if not specified)

    Returns:
        The generated API key
    """
    if clients_file is None:
        settings = get_settings()
        clients_file = settings.clients_file

    manager = AuthManager(clients_file)
    api_key = manager.add_client(client_id, description)

    print(f"Client added successfully!")
    print(f"Client ID: {client_id}")
    print(f"API Key: {api_key}")
    print()
    print("IMPORTANT: Save this API key securely. It cannot be retrieved later.")

    return api_key


def cli_list_clients(clients_file: Path | None = None) -> None:
    """
    CLI utility to list all clients.

    Args:
        clients_file: Path to clients.json (uses default if not specified)
    """
    if clients_file is None:
        settings = get_settings()
        clients_file = settings.clients_file

    manager = AuthManager(clients_file)
    clients = manager.list_clients()

    if not clients:
        print("No clients configured.")
        return

    print(f"{'Client ID':<30} {'Active':<8} {'Description':<40} {'Created'}")
    print("-" * 100)

    for client in clients:
        print(
            f"{client.client_id:<30} "
            f"{'Yes' if client.active else 'No':<8} "
            f"{client.description[:40]:<40} "
            f"{client.created_at.isoformat()}"
        )
