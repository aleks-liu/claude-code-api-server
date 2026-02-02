"""
Logging configuration for Claude Code API Server.

Uses structlog for structured logging with support for both
human-readable (debug) and JSON (production) output formats.
"""

import logging
import sys
from contextvars import ContextVar
from typing import Any

import structlog
from structlog.types import EventDict, WrappedLogger

# Context variables for request/job tracking
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
job_id_var: ContextVar[str | None] = ContextVar("job_id", default=None)
client_id_var: ContextVar[str | None] = ContextVar("client_id", default=None)


def add_context_vars(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """
    Add context variables to log events.

    This processor adds request_id, job_id, and client_id to all log
    entries if they are set in the current context.
    """
    # Add context vars if set and not already present
    if (request_id := request_id_var.get()) is not None:
        event_dict.setdefault("request_id", request_id)
    if (job_id := job_id_var.get()) is not None:
        event_dict.setdefault("job_id", job_id)
    if (client_id := client_id_var.get()) is not None:
        event_dict.setdefault("client_id", client_id)

    return event_dict


def sanitize_sensitive_data(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """
    Remove or mask sensitive data from log events.

    Prevents accidental logging of API keys and other secrets.
    """
    sensitive_keys = {
        "api_key",
        "anthropic_key",
        "anthropic_api_key",
        "password",
        "secret",
        "token",
        "authorization",
    }

    # Keys that are safe to log despite matching sensitive patterns
    # (e.g., already encrypted values)
    safe_keys = {
        "encrypted_api_key",
    }

    def mask_value(key: str, value: Any) -> Any:
        """Mask sensitive values."""
        key_lower = key.lower()
        # Skip masking for explicitly safe keys
        if key_lower in safe_keys:
            return value
        if any(s in key_lower for s in sensitive_keys):
            if isinstance(value, str) and len(value) > 8:
                return f"{value[:4]}...{value[-4:]}"
            return "***REDACTED***"
        return value

    # Recursively sanitize dict values
    def sanitize_dict(d: dict) -> dict:
        return {k: mask_value(k, v) if not isinstance(v, dict) else sanitize_dict(v)
                for k, v in d.items()}

    return sanitize_dict(event_dict)


def setup_logging(debug: bool = False, log_level: str = "INFO") -> None:
    """
    Configure structlog for the application.

    Args:
        debug: If True, use human-readable console output.
               If False, use JSON format for production.
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
    """
    # Convert string level to logging constant
    level = getattr(logging, log_level.upper(), logging.INFO)

    # Shared processors for all modes
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        add_context_vars,
        sanitize_sensitive_data,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if debug:
        # Development: human-readable colored output
        processors = shared_processors + [
            structlog.processors.ExceptionPrettyPrinter(),
            structlog.dev.ConsoleRenderer(
                colors=True,
                exception_formatter=structlog.dev.plain_traceback,
            ),
        ]
    else:
        # Production: JSON output
        processors = shared_processors + [
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Also configure standard library logging to use structlog
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )


def get_logger(name: str | None = None) -> structlog.BoundLogger:
    """
    Get a structlog logger instance.

    The ``logger_name`` is passed as an **initial value** rather than
    via ``.bind()``.  This is critical because ``.bind()`` forces the
    lazy proxy returned by ``structlog.get_logger()`` to resolve
    immediately against whatever configuration is active at call time.

    Since module-level loggers are created at import time — before
    ``setup_logging()`` has configured structlog — calling ``.bind()``
    would permanently lock them to structlog's **builtin defaults**
    (no ``sanitize_sensitive_data``, no ``add_context_vars``, wrong
    ``TimeStamper`` format).

    By using ``structlog.get_logger(logger_name=name)`` the proxy
    stays lazy and resolves on first actual use, by which time
    ``setup_logging()`` has already run.

    Args:
        name: Optional logger name (typically ``__name__``)

    Returns:
        A lazy proxy that resolves to a correctly configured bound
        logger on first use.
    """
    if name:
        return structlog.get_logger(logger_name=name)
    return structlog.get_logger()


class LogContext:
    """
    Context manager for setting logging context variables.

    Usage:
        with LogContext(job_id="job_123", client_id="client_001"):
            logger.info("Processing job")  # Will include job_id and client_id
    """

    def __init__(
        self,
        request_id: str | None = None,
        job_id: str | None = None,
        client_id: str | None = None,
    ):
        self.request_id = request_id
        self.job_id = job_id
        self.client_id = client_id
        self._tokens: list[Any] = []

    def __enter__(self) -> "LogContext":
        if self.request_id is not None:
            self._tokens.append(request_id_var.set(self.request_id))
        if self.job_id is not None:
            self._tokens.append(job_id_var.set(self.job_id))
        if self.client_id is not None:
            self._tokens.append(client_id_var.set(self.client_id))
        return self

    def __exit__(self, *args: Any) -> None:
        # Reset context vars in reverse order
        for token in reversed(self._tokens):
            # Each token knows which var it belongs to
            if hasattr(token, "var"):
                token.var.reset(token)


def set_job_context(job_id: str) -> None:
    """Set job_id in current context."""
    job_id_var.set(job_id)


def set_client_context(client_id: str) -> None:
    """Set client_id in current context."""
    client_id_var.set(client_id)


def set_request_context(request_id: str) -> None:
    """Set request_id in current context."""
    request_id_var.set(request_id)


def clear_context() -> None:
    """Clear all context variables."""
    request_id_var.set(None)
    job_id_var.set(None)
    client_id_var.set(None)
