"""
Per-job HTTP CONNECT proxy with domain/IP filtering for network isolation.

Each job with network restrictions gets its own proxy instance listening
on a Unix domain socket. Inside the bwrap sandbox, socat bridges this
socket to TCP 127.0.0.1:3128, and HTTP_PROXY/HTTPS_PROXY env vars point
there. The proxy evaluates the job's NetworkPolicy to allow or deny
each outbound connection.

Architecture::

    Inside sandbox (--unshare-net)         Host process
    ┌─────────────────────────┐     ┌──────────────────────────┐
    │ Claude CLI               │     │                          │
    │   -> HTTP_PROXY=:3128    │     │                          │
    │   -> socat :3128 ──────────────> proxy.sock               │
    │                         │     │   -> NetworkPolicy check  │
    │                         │     │   -> upstream connect      │
    └─────────────────────────┘     └──────────────────────────┘

Protocol support:
- HTTP CONNECT (HTTPS tunneling) — primary protocol
- Plain HTTP — hostname extracted from request URL
"""

import asyncio
import ipaddress
import re
import socket
from pathlib import Path

from .logging_config import get_logger
from .models import NetworkPolicy

logger = get_logger(__name__)


# =============================================================================
# Constants
# =============================================================================

# Mandatory domains always allowed regardless of profile configuration.
# Ensures Claude CLI can always communicate with the Anthropic API.
_MANDATORY_ALLOWED_DOMAINS = [
    "api.anthropic.com",
    "*.anthropic.com",
]

# Buffer size for bidirectional relay
_RELAY_BUFFER_SIZE = 65536

# DNS resolution timeout
_DNS_RESOLVE_TIMEOUT = 10.0

# Upstream connection timeout
_UPSTREAM_CONNECT_TIMEOUT = 30.0

# Proxy socket file name within job directory
PROXY_SOCKET_NAME = "proxy.sock"


# =============================================================================
# Domain/IP Matching Helpers
# =============================================================================


def _match_domain_pattern(hostname: str, pattern: str) -> bool:
    """
    Check if a hostname matches a domain pattern.

    Supports:
    - Exact match: "github.com" matches "github.com"
    - Wildcard subdomain: "*.github.com" matches "api.github.com",
      "raw.github.com" but NOT "github.com" itself.
    """
    hostname = hostname.lower().rstrip(".")
    pattern = pattern.lower().rstrip(".")

    if pattern.startswith("*."):
        suffix = pattern[2:]
        return hostname == suffix or hostname.endswith("." + suffix)

    return hostname == pattern


def _match_any_domain(hostname: str, patterns: list[str]) -> bool:
    """Check if hostname matches any pattern in the list."""
    return any(_match_domain_pattern(hostname, p) for p in patterns)


def _ip_in_ranges(ip_str: str, ranges: list[str]) -> bool:
    """Check if an IP address falls within any of the given CIDR ranges."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False

    for cidr in ranges:
        try:
            network = ipaddress.ip_network(cidr, strict=False)
            if addr in network:
                return True
        except ValueError:
            continue

    return False


def _is_ip_address(host: str) -> bool:
    """Check if a string is an IP address (v4 or v6)."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _is_mandatory_domain(hostname: str) -> bool:
    """Check if hostname is a mandatory always-allowed domain."""
    return _match_any_domain(hostname, _MANDATORY_ALLOWED_DOMAINS)


# =============================================================================
# Policy Evaluation
# =============================================================================


async def evaluate_policy(
    policy: NetworkPolicy,
    hostname: str,
    job_id: str,
) -> tuple[bool, str]:
    """
    Evaluate a NetworkPolicy for a given destination hostname.

    Implements the filtering flow from the spec (Section 3.2.2):
    1. Check mandatory Anthropic domains (always allowed)
    2. Check raw IP destination
    3. Check denied_domains
    4. Check allowed_domains
    5. Resolve DNS
    6. Check denied_ip_ranges
    7. Check allowed_ip_ranges

    Returns:
        Tuple of (allowed: bool, reason: str)
    """
    # Step 0: Mandatory domains bypass all checks
    if not _is_ip_address(hostname) and _is_mandatory_domain(hostname):
        return True, "Mandatory domain (Anthropic API)"

    # Step 1: Raw IP check
    if _is_ip_address(hostname):
        if not policy.allow_ip_destination:
            return False, "Raw IP destinations not allowed"
        # Skip domain checks, go directly to IP range checks
        resolved_ip = hostname
    else:
        # Step 2: Check denied_domains
        if policy.denied_domains and _match_any_domain(hostname, policy.denied_domains):
            return False, f"Domain '{hostname}' is in denied_domains"

        # Step 3: Check allowed_domains
        if policy.allowed_domains is not None:
            if len(policy.allowed_domains) == 0:
                return False, "No domains allowed (allowed_domains is empty)"
            if not _match_any_domain(hostname, policy.allowed_domains):
                return False, f"Domain '{hostname}' not in allowed_domains"

        # Step 4: Resolve DNS to get IP for range checks
        try:
            loop = asyncio.get_running_loop()
            infos = await asyncio.wait_for(
                loop.getaddrinfo(hostname, None, family=0, type=socket.SOCK_STREAM),
                timeout=_DNS_RESOLVE_TIMEOUT,
            )
            if not infos:
                return False, f"DNS resolution failed for '{hostname}': no results"
            resolved_ip = infos[0][4][0]
        except asyncio.TimeoutError:
            return False, f"DNS resolution timed out for '{hostname}'"
        except (socket.gaierror, OSError) as exc:
            return False, f"DNS resolution failed for '{hostname}': {exc}"

    # Step 5: Check denied_ip_ranges
    if policy.denied_ip_ranges and _ip_in_ranges(resolved_ip, policy.denied_ip_ranges):
        return False, f"Resolved IP {resolved_ip} is in denied_ip_ranges"

    # Step 6: Check allowed_ip_ranges
    if policy.allowed_ip_ranges is not None:
        if len(policy.allowed_ip_ranges) == 0:
            return False, "No IP ranges allowed (allowed_ip_ranges is empty)"
        if not _ip_in_ranges(resolved_ip, policy.allowed_ip_ranges):
            return False, f"Resolved IP {resolved_ip} not in allowed_ip_ranges"

    return True, "Allowed"


# =============================================================================
# HTTP Proxy Protocol Handling
# =============================================================================

# Regex for CONNECT request: CONNECT host:port HTTP/1.x
_CONNECT_RE = re.compile(
    rb"^CONNECT\s+([^\s:]+):(\d+)\s+HTTP/\d\.\d\r\n",
    re.IGNORECASE,
)

# Regex for plain HTTP request: METHOD http://host[:port]/path HTTP/1.x
_HTTP_REQUEST_RE = re.compile(
    rb"^(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+"
    rb"http://([^/:\s]+)(?::(\d+))?"
    rb"(/\S*)?\s+HTTP/(\d\.\d)\r\n",
    re.IGNORECASE,
)


async def _relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Relay data from reader to writer until EOF or error."""
    try:
        while True:
            data = await reader.read(_RELAY_BUFFER_SIZE)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError, asyncio.CancelledError):
        pass
    finally:
        try:
            if not writer.is_closing():
                writer.close()
        except (ConnectionError, OSError):
            pass


async def _send_response(
    writer: asyncio.StreamWriter,
    status_code: int,
    reason: str,
    body: str = "",
) -> None:
    """Send an HTTP response to the client."""
    response = (
        f"HTTP/1.1 {status_code} {reason}\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
        f"{body}"
    )
    writer.write(response.encode())
    await writer.drain()


class SandboxProxy:
    """
    Per-job HTTP CONNECT proxy with NetworkPolicy filtering.

    Listens on a Unix domain socket. Each connection is evaluated
    against the job's NetworkPolicy before being forwarded upstream.
    """

    def __init__(
        self,
        job_id: str,
        socket_path: Path,
        policy: NetworkPolicy,
    ):
        self._job_id = job_id
        self._socket_path = socket_path
        self._policy = policy
        self._server: asyncio.AbstractServer | None = None
        self._active_connections: int = 0

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    @property
    def is_running(self) -> bool:
        return self._server is not None and self._server.is_serving()

    async def start(self) -> None:
        """Start the proxy server on the Unix domain socket."""
        # Remove stale socket file if it exists
        if self._socket_path.exists():
            self._socket_path.unlink()

        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(self._socket_path),
        )

        logger.info(
            "network_proxy_started",
            job_id=self._job_id,
            socket_path=str(self._socket_path),
        )

    async def stop(self) -> None:
        """Stop the proxy server and clean up."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        # Remove socket file
        try:
            if self._socket_path.exists():
                self._socket_path.unlink()
        except OSError:
            pass

        logger.debug(
            "network_proxy_stopped",
            job_id=self._job_id,
        )

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a single proxy client connection."""
        self._active_connections += 1
        try:
            await self._process_request(reader, writer)
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass
        except Exception as exc:
            logger.warning(
                "proxy_connection_error",
                job_id=self._job_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
        finally:
            self._active_connections -= 1
            try:
                if not writer.is_closing():
                    writer.close()
                    await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _process_request(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Parse and process the incoming proxy request."""
        # Read the first line + headers (up to 8KB)
        try:
            header_data = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"),
                timeout=30.0,
            )
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            await _send_response(writer, 400, "Bad Request", "Timeout or incomplete request")
            return

        # Try CONNECT first
        connect_match = _CONNECT_RE.match(header_data)
        if connect_match:
            host = connect_match.group(1).decode("ascii", errors="replace")
            port = int(connect_match.group(2).decode())
            await self._handle_connect(host, port, reader, writer)
            return

        # Try plain HTTP
        http_match = _HTTP_REQUEST_RE.match(header_data)
        if http_match:
            host = http_match.group(2).decode("ascii", errors="replace")
            port = int(http_match.group(3).decode()) if http_match.group(3) else 80
            await self._handle_plain_http(host, port, header_data, reader, writer)
            return

        await _send_response(writer, 400, "Bad Request", "Unsupported proxy request")

    async def _handle_connect(
        self,
        host: str,
        port: int,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle an HTTP CONNECT tunnel request."""
        # Evaluate policy
        allowed, reason = await evaluate_policy(self._policy, host, self._job_id)

        if not allowed:
            logger.warning(
                "proxy_connection_denied",
                job_id=self._job_id,
                destination=f"{host}:{port}",
                reason=reason,
            )
            await _send_response(
                writer, 403, "Forbidden",
                f"Connection denied: {reason}",
            )
            return

        # Connect upstream
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=_UPSTREAM_CONNECT_TIMEOUT,
            )
        except Exception as exc:
            logger.warning(
                "proxy_connection_error",
                job_id=self._job_id,
                destination=f"{host}:{port}",
                error=str(exc),
            )
            await _send_response(
                writer, 502, "Bad Gateway",
                f"Upstream connection failed: {exc}",
            )
            return

        logger.debug(
            "proxy_connection_allowed",
            job_id=self._job_id,
            destination=f"{host}:{port}",
            reason=reason,
        )

        # Send 200 Connection Established
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()

        # Bidirectional relay
        try:
            await asyncio.gather(
                _relay(reader, upstream_writer),
                _relay(upstream_reader, writer),
            )
        finally:
            try:
                upstream_writer.close()
                await upstream_writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _handle_plain_http(
        self,
        host: str,
        port: int,
        header_data: bytes,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a plain HTTP request (non-CONNECT)."""
        # Evaluate policy
        allowed, reason = await evaluate_policy(self._policy, host, self._job_id)

        if not allowed:
            logger.warning(
                "proxy_connection_denied",
                job_id=self._job_id,
                destination=f"{host}:{port}",
                reason=reason,
            )
            await _send_response(
                writer, 403, "Forbidden",
                f"Connection denied: {reason}",
            )
            return

        # Connect upstream
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=_UPSTREAM_CONNECT_TIMEOUT,
            )
        except Exception as exc:
            logger.warning(
                "proxy_connection_error",
                job_id=self._job_id,
                destination=f"{host}:{port}",
                error=str(exc),
            )
            await _send_response(
                writer, 502, "Bad Gateway",
                f"Upstream connection failed: {exc}",
            )
            return

        logger.debug(
            "proxy_connection_allowed",
            job_id=self._job_id,
            destination=f"{host}:{port}",
            reason=reason,
        )

        # Forward the original request data (rewrite absolute URL to relative)
        # Convert "GET http://host/path HTTP/1.1" to "GET /path HTTP/1.1"
        http_match = _HTTP_REQUEST_RE.match(header_data)
        if http_match:
            method = http_match.group(1).decode()
            path = http_match.group(4).decode() if http_match.group(4) else "/"
            version = http_match.group(5).decode()
            # Reconstruct first line with relative path
            first_line = f"{method} {path} HTTP/{version}\r\n".encode()
            # Find end of first line in original data
            first_line_end = header_data.index(b"\r\n") + 2
            rewritten = first_line + header_data[first_line_end:]
            upstream_writer.write(rewritten)
        else:
            upstream_writer.write(header_data)

        await upstream_writer.drain()

        # Bidirectional relay for remaining data
        try:
            await asyncio.gather(
                _relay(reader, upstream_writer),
                _relay(upstream_reader, writer),
            )
        finally:
            try:
                upstream_writer.close()
                await upstream_writer.wait_closed()
            except (ConnectionError, OSError):
                pass


# =============================================================================
# Proxy Manager
# =============================================================================


class ProxyManager:
    """
    Manages per-job proxy instances.

    Tracks active proxies and provides start/stop lifecycle management.
    """

    def __init__(self) -> None:
        self._proxies: dict[str, SandboxProxy] = {}

    async def start_proxy(
        self,
        job_id: str,
        job_dir: Path,
        policy: NetworkPolicy,
    ) -> SandboxProxy:
        """
        Start a proxy for a job.

        Args:
            job_id: Job identifier.
            job_dir: Job directory (proxy.sock will be created here).
            policy: Network policy to enforce.

        Returns:
            The started SandboxProxy instance.

        Raises:
            RuntimeError: If proxy fails to start.
        """
        socket_path = job_dir / PROXY_SOCKET_NAME

        proxy = SandboxProxy(
            job_id=job_id,
            socket_path=socket_path,
            policy=policy,
        )

        try:
            await proxy.start()
        except Exception as exc:
            raise RuntimeError(
                f"Network proxy could not be started for job {job_id}: {exc}"
            ) from exc

        self._proxies[job_id] = proxy
        return proxy

    async def stop_proxy(self, job_id: str) -> None:
        """Stop and remove a job's proxy."""
        proxy = self._proxies.pop(job_id, None)
        if proxy is not None:
            await proxy.stop()

    async def stop_all(self) -> None:
        """Stop all active proxies (for graceful shutdown)."""
        job_ids = list(self._proxies.keys())
        for job_id in job_ids:
            await self.stop_proxy(job_id)

    def get_proxy(self, job_id: str) -> SandboxProxy | None:
        """Get the proxy for a job, or None."""
        return self._proxies.get(job_id)


# =============================================================================
# Singleton
# =============================================================================

_proxy_manager: ProxyManager | None = None


def get_proxy_manager() -> ProxyManager:
    """Get the singleton ProxyManager instance."""
    global _proxy_manager
    if _proxy_manager is None:
        _proxy_manager = ProxyManager()
    return _proxy_manager


def reset_proxy_manager() -> None:
    """Reset the proxy manager (for testing)."""
    global _proxy_manager
    _proxy_manager = None
