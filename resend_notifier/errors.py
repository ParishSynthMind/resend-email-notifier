"""Typed errors. Callers can catch ResendError for everything, or a subclass."""
from typing import Optional


class ResendError(Exception):
    """Base class for every error raised by this package."""


class ConfigError(ResendError):
    """Missing or invalid configuration (for example no API key)."""


class TemplateError(ResendError):
    """A template is missing or failed to render."""


class ResendAPIError(ResendError):
    """Resend answered with a non-2xx status.

    ``status`` is the HTTP status, ``name`` is Resend's error name
    (for example ``validation_error`` or ``rate_limit_exceeded``) when the
    body had one, and ``retryable`` says whether the client would retry it.
    """

    retryable = False

    def __init__(self, status: int, name: Optional[str], message: str,
                 retry_after: Optional[float] = None):
        self.status = status
        self.name = name or "unknown_error"
        self.message = message
        self.retry_after = retry_after
        super().__init__(f"Resend API {status} {self.name}: {message}")


class ValidationError(ResendAPIError):
    """4xx that will never succeed if repeated (bad payload, bad key, unverified domain)."""


class IdempotencyConflictError(ResendAPIError):
    """409: the same Idempotency-Key was reused with a different body."""


class RateLimitError(ResendAPIError):
    """429 rate_limit_exceeded. Retried with backoff, honouring Retry-After."""

    retryable = True


class QuotaExceededError(ResendAPIError):
    """429 daily or monthly quota exceeded. Not retried: waiting seconds will not help."""


class ServerError(ResendAPIError):
    """5xx from Resend. Retried with backoff."""

    retryable = True


class NetworkError(ResendError):
    """Connection error or timeout before a response arrived. Retried."""

    retryable = True


class RetriesExhaustedError(ResendError):
    """Every attempt failed with a retryable error. ``last_error`` holds the final one."""

    def __init__(self, attempts: int, last_error: Exception):
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(f"gave up after {attempts} attempts: {last_error}")


class WebhookVerificationError(ResendError):
    """A webhook request failed signature or timestamp verification."""
