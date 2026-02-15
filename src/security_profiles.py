"""
Security profile manager for Claude Code API Server.

Handles CRUD operations on security profiles, storage, validation,
and built-in profile initialization.
"""

import ipaddress
import re
from pathlib import Path

from .logging_config import get_logger
from .models import (
    NetworkPolicy,
    SecurityProfile,
    SecurityProfilesFile,
    utcnow,
)

logger = get_logger(__name__)


# =============================================================================
# Exceptions
# =============================================================================


class ProfileError(Exception):
    """Base exception for profile-related errors."""

    pass


class ProfileNotFoundError(ProfileError):
    """Raised when a profile is not found."""

    pass


class ProfileExistsError(ProfileError):
    """Raised when a profile name already exists."""

    pass


class ProfileValidationError(ProfileError):
    """Raised when profile data is invalid."""

    pass


class ProfileDeleteError(ProfileError):
    """Raised when a profile cannot be deleted."""

    pass


# =============================================================================
# Validation
# =============================================================================

_PROFILE_NAME_RE = re.compile(r"^[a-z0-9-]+$")

# Domain patterns: exact or *.suffix (suffix must have at least one dot)
_DOMAIN_PATTERN_RE = re.compile(
    r"^(\*\.)?[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?)+$"
)


def _validate_domain_pattern(pattern: str) -> None:
    """Validate a domain pattern (exact or wildcard)."""
    if not _DOMAIN_PATTERN_RE.match(pattern):
        raise ProfileValidationError(
            f"Invalid domain pattern: '{pattern}'. "
            "Must be an exact domain (e.g. 'github.com') or "
            "wildcard subdomain (e.g. '*.github.com'). "
            "Patterns like '*.com' or '*' are not allowed."
        )


def _validate_cidr(cidr: str) -> None:
    """Validate a CIDR notation string."""
    try:
        ipaddress.ip_network(cidr, strict=False)
    except ValueError as e:
        raise ProfileValidationError(
            f"Invalid CIDR notation: '{cidr}'. {e}"
        )


def _validate_network_policy(network: NetworkPolicy) -> None:
    """Validate all fields in a NetworkPolicy."""
    if network.allowed_domains is not None:
        for d in network.allowed_domains:
            _validate_domain_pattern(d)
    for d in network.denied_domains:
        _validate_domain_pattern(d)
    if network.allowed_ip_ranges is not None:
        for r in network.allowed_ip_ranges:
            _validate_cidr(r)
    for r in network.denied_ip_ranges:
        _validate_cidr(r)


def _validate_profile_name(name: str) -> None:
    """Validate profile name format."""
    if not name or len(name) > 100:
        raise ProfileValidationError(
            "Profile name must be 1-100 characters"
        )
    if not _PROFILE_NAME_RE.match(name):
        raise ProfileValidationError(
            f"Profile name '{name}' is invalid. "
            "Must contain only lowercase letters, digits, and hyphens."
        )


# =============================================================================
# Built-in Profiles
# =============================================================================

_PRIVATE_IP_RANGES = [
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
    "127.0.0.0/8",
    "::1/128",
    "fc00::/7",
    "fe80::/10",
]


def _create_builtin_profiles() -> dict[str, SecurityProfile]:
    """Create the three built-in profiles."""
    now = utcnow()
    return {
        "unconfined": SecurityProfile(
            name="unconfined",
            description=(
                "No restrictions. Full network access, all tools and MCP servers "
                "available. Use for trusted clients or development."
            ),
            network=NetworkPolicy(
                allowed_domains=None,
                denied_domains=[],
                allowed_ip_ranges=None,
                denied_ip_ranges=[],
                allow_ip_destination=True,
            ),
            denied_tools=[],
            allowed_mcp_servers=None,
            is_builtin=True,
            is_default=False,
            created_at=now,
            updated_at=now,
        ),
        "common": SecurityProfile(
            name="common",
            description=(
                "Balanced security. Any internet domain allowed via DNS. "
                "Private networks and raw IP destinations blocked. "
                "All tools and MCP servers available."
            ),
            network=NetworkPolicy(
                allowed_domains=None,
                denied_domains=[],
                allowed_ip_ranges=None,
                denied_ip_ranges=list(_PRIVATE_IP_RANGES),
                allow_ip_destination=False,
            ),
            denied_tools=[],
            allowed_mcp_servers=None,
            is_builtin=True,
            is_default=True,
            created_at=now,
            updated_at=now,
        ),
        "restrictive": SecurityProfile(
            name="restrictive",
            description=(
                "Maximum security. Only auto-allowed domains reachable "
                "(see CCAS_AUTOALLOWED_DOMAINS). "
                "WebFetch and WebSearch denied. No MCP servers. "
                "Private networks and raw IP destinations blocked."
            ),
            network=NetworkPolicy(
                allowed_domains=[],
                denied_domains=[],
                allowed_ip_ranges=None,
                denied_ip_ranges=list(_PRIVATE_IP_RANGES),
                allow_ip_destination=False,
            ),
            denied_tools=["WebFetch", "WebSearch"],
            allowed_mcp_servers=[],
            is_builtin=True,
            is_default=False,
            created_at=now,
            updated_at=now,
        ),
    }


# =============================================================================
# Profile Manager
# =============================================================================


class SecurityProfileManager:
    """
    Manages security profiles: CRUD, persistence, validation.

    Profiles are stored in a JSON file and loaded into memory.
    Built-in profiles are created on first startup.
    """

    def __init__(self, profiles_file: Path):
        self._profiles_file = profiles_file
        self._data: SecurityProfilesFile = SecurityProfilesFile()
        self._load()

    def _load(self) -> None:
        """Load profiles from disk, creating built-ins if needed."""
        if not self._profiles_file.exists():
            logger.info(
                "security_profiles_initializing",
                path=str(self._profiles_file),
            )
            self._data = SecurityProfilesFile(
                profiles=_create_builtin_profiles(),
                default_profile="common",
            )
            self._save()
            return

        try:
            content = self._profiles_file.read_text(encoding="utf-8")
            if not content.strip():
                self._data = SecurityProfilesFile(
                    profiles=_create_builtin_profiles(),
                    default_profile="common",
                )
                self._save()
                return

            self._data = SecurityProfilesFile.model_validate_json(content)
            logger.info(
                "security_profiles_loaded",
                count=len(self._data.profiles),
                path=str(self._profiles_file),
            )
        except Exception as e:
            logger.error(
                "security_profiles_load_failed",
                error=str(e),
                path=str(self._profiles_file),
            )
            raise RuntimeError(f"Failed to load security profiles: {e}") from e

    def _save(self) -> None:
        """Save profiles to disk."""
        try:
            self._profiles_file.parent.mkdir(parents=True, exist_ok=True)
            self._profiles_file.write_text(
                self._data.model_dump_json(indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(
                "security_profiles_save_failed",
                error=str(e),
            )
            raise RuntimeError(f"Failed to save security profiles: {e}") from e

    # ---- Read operations ----

    def get_profile(self, name: str) -> SecurityProfile | None:
        """Get a profile by name."""
        return self._data.profiles.get(name)

    def get_default_profile(self) -> SecurityProfile:
        """Get the default profile."""
        profile = self._data.profiles.get(self._data.default_profile)
        if profile is None:
            # Fallback to common if default is missing
            profile = self._data.profiles.get("common")
        if profile is None:
            raise RuntimeError("No default security profile available")
        return profile

    def get_default_profile_name(self) -> str:
        """Get the name of the default profile."""
        return self._data.default_profile

    def list_profiles(self) -> list[SecurityProfile]:
        """List all profiles."""
        return list(self._data.profiles.values())

    # ---- Write operations ----

    def create_profile(
        self,
        name: str,
        description: str = "",
        network: NetworkPolicy | None = None,
        denied_tools: list[str] | None = None,
        allowed_mcp_servers: list[str] | None = None,
    ) -> SecurityProfile:
        """Create a new profile."""
        _validate_profile_name(name)

        if name in self._data.profiles:
            raise ProfileExistsError(f"Profile '{name}' already exists")

        if network is not None:
            _validate_network_policy(network)

        now = utcnow()
        profile = SecurityProfile(
            name=name,
            description=description,
            network=network or NetworkPolicy(),
            denied_tools=denied_tools or [],
            allowed_mcp_servers=allowed_mcp_servers,
            is_builtin=False,
            is_default=False,
            created_at=now,
            updated_at=now,
        )

        self._data.profiles[name] = profile
        self._save()

        logger.info("security_profile_created", name=name)
        return profile

    def update_profile(
        self,
        name: str,
        description: str | None = None,
        network: NetworkPolicy | None = None,
        denied_tools: list[str] | None = None,
        allowed_mcp_servers: list[str] | None = None,
        fields_set: set[str] | None = None,
    ) -> SecurityProfile:
        """
        Update an existing profile.

        Only fields present in `fields_set` are updated. This allows
        distinguishing between "not provided" and "set to None".
        """
        profile = self._data.profiles.get(name)
        if profile is None:
            raise ProfileNotFoundError(f"Profile '{name}' not found")

        if fields_set is None:
            fields_set = set()

        if "description" in fields_set and description is not None:
            profile.description = description

        if "network" in fields_set and network is not None:
            _validate_network_policy(network)
            profile.network = network

        if "denied_tools" in fields_set and denied_tools is not None:
            profile.denied_tools = denied_tools

        if "allowed_mcp_servers" in fields_set:
            # None means "all servers", [] means "no servers"
            profile.allowed_mcp_servers = allowed_mcp_servers

        profile.updated_at = utcnow()

        self._data.profiles[name] = profile
        self._save()

        logger.info("security_profile_updated", name=name)
        return profile

    def delete_profile(
        self,
        name: str,
        assigned_client_ids: list[str] | None = None,
    ) -> None:
        """
        Delete a profile.

        Args:
            name: Profile name to delete.
            assigned_client_ids: Client IDs currently assigned to this profile.
                If non-empty, deletion is blocked with 409.
        """
        profile = self._data.profiles.get(name)
        if profile is None:
            raise ProfileNotFoundError(f"Profile '{name}' not found")

        if profile.is_builtin:
            raise ProfileDeleteError(
                f"Cannot delete built-in profile '{name}'"
            )

        if assigned_client_ids:
            raise ProfileDeleteError(
                f"Cannot delete profile '{name}': assigned to clients: "
                f"{', '.join(assigned_client_ids)}"
            )

        del self._data.profiles[name]

        # If this was the default, reset to common
        if self._data.default_profile == name:
            self._data.default_profile = "common"

        self._save()
        logger.info("security_profile_deleted", name=name)

    def set_default(self, name: str) -> SecurityProfile:
        """Set a profile as the server-wide default for new clients."""
        profile = self._data.profiles.get(name)
        if profile is None:
            raise ProfileNotFoundError(f"Profile '{name}' not found")

        # Clear old default
        for p in self._data.profiles.values():
            p.is_default = False

        profile.is_default = True
        self._data.default_profile = name
        self._save()

        logger.info("security_profile_default_set", name=name)
        return profile

    def reload(self) -> None:
        """Reload profiles from disk."""
        self._load()


# =============================================================================
# Singleton
# =============================================================================

_profile_manager: SecurityProfileManager | None = None


def get_profile_manager(profiles_file: Path | None = None) -> SecurityProfileManager:
    """Get the singleton SecurityProfileManager instance."""
    global _profile_manager
    if _profile_manager is None:
        if profiles_file is None:
            from .config import get_settings
            profiles_file = get_settings().security_profiles_file
        _profile_manager = SecurityProfileManager(profiles_file)
    return _profile_manager


def reset_profile_manager() -> None:
    """Reset the profile manager (for testing)."""
    global _profile_manager
    _profile_manager = None
