"""Thin, careful client for the Resend email API.

Only two endpoints are used: POST /emails and POST /emails/batch.
Retries cover network errors, 429 rate limits and 5xx, with exponential
backoff plus jitter, and Retry-After is honoured when Resend sends it.
Anything else in the 4xx range is raised straight away, because sending
the same bad request again cannot succeed.
"""
import email.utils
import logging
import os
import random
import time
from typing import Any, Callable, Dict, List, Optional

import requests

from . import __version__
from .errors import (
    ConfigError,
    IdempotencyConflictError,
    NetworkError,
    QuotaExceededError,
    RateLimitError,
    ResendAPIError,
    RetriesExhaustedError,
    ServerError,
    ValidationError,
)

log = logging.getLogger("resend_notifier.client")

DEFAULT_BASE_URL = "https://api.resend.com"
BATCH_LIMIT = 100  # Resend: up to 100 emails per batch call
_QUOTA_ERRORS = {"daily_quota_exceeded", "monthly_quota_exceeded"}


def _parse_retry_after(value: Optional[str], now: Optional[float] = None) -> Optional[float]:
    """Retry-After may be a number of seconds or an HTTP date."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    now = time.time() if now is None else now
    return max(0.0, when.timestamp() - now)


def _error_from_response(resp: requests.Response) -> ResendAPIError:
    name, message = None, resp.text[:300] or resp.reason or "no body"
    try:
        body = resp.json()
        if isinstance(body, dict):
            name = body.get("name") or body.get("type")
            message = body.get("message") or message
    except ValueError:
        pass
    retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
    status = resp.status_code
    if status == 429:
        cls = QuotaExceededError if name in _QUOTA_ERRORS else RateLimitError
    elif status >= 500:
        cls = ServerError
    elif status == 409 and name == "concurrent_idempotent_requests":
        # The first request with this key is still in flight: worth a retry.
        cls = RateLimitError
    elif status == 409:
        cls = IdempotencyConflictError
    else:
        cls = ValidationError
    return cls(status, name, message, retry_after=retry_after)


class ResendClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 10.0,
        max_attempts: int = 5,
        backoff_base: float = 0.5,
        backoff_max: float = 30.0,
        max_retry_after: float = 60.0,
        session: Optional[requests.Session] = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
    ):
        api_key = api_key if api_key is not None else os.environ.get("RESEND_API_KEY", "")
        if not api_key:
            raise ConfigError("RESEND_API_KEY is not set")
        self._api_key = api_key
        self.base_url = (base_url or os.environ.get("RESEND_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.max_attempts = max(1, max_attempts)
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.max_retry_after = max_retry_after
        self.session = session or requests.Session()
        self._sleep = sleep
        self._rng = rng

    def __repr__(self) -> str:  # never show the key
        return f"ResendClient(base_url={self.base_url!r}, api_key='***')"

    # ---- public API -------------------------------------------------
    def send(self, email_payload: Dict[str, Any], idempotency_key: Optional[str] = None) -> str:
        """Send one email. Returns the Resend email ID."""
        for field in ("from", "to", "subject"):
            if not email_payload.get(field):
                raise ValueError(f"email payload is missing '{field}'")
        data = self._request("/emails", email_payload, idempotency_key)
        return data["id"]

    def send_batch(self, payloads: List[Dict[str, Any]], idempotency_key: Optional[str] = None) -> List[str]:
        """Send up to 100 emails in one call. Returns IDs in the same order."""
        if not payloads:
            return []
        if len(payloads) > BATCH_LIMIT:
            raise ValueError(f"a batch holds at most {BATCH_LIMIT} emails, got {len(payloads)}")
        for p in payloads:
            if p.get("attachments"):
                raise ValueError("Resend's batch endpoint does not accept attachments")
        data = self._request("/emails/batch", payloads, idempotency_key)
        return [item["id"] for item in data.get("data", [])]

    # ---- internals --------------------------------------------------
    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with 'equal jitter': half fixed, half random."""
        ceiling = min(self.backoff_max, self.backoff_base * (2 ** (attempt - 1)))
        return ceiling / 2 + self._rng() * ceiling / 2

    def _request(self, path: str, body: Any, idempotency_key: Optional[str]) -> Dict[str, Any]:
        url = self.base_url + path
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "User-Agent": f"resend-email-notifier/{__version__}",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        last_error: Exception = RuntimeError("no attempt made")
        for attempt in range(1, self.max_attempts + 1):
            started = time.monotonic()
            try:
                resp = self.session.post(url, json=body, headers=headers, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = NetworkError(f"{type(exc).__name__} calling {path}")
                log.warning("resend %s attempt=%d network error=%s", path, attempt, type(exc).__name__)
                wait = self._backoff(attempt)
            else:
                ms = int((time.monotonic() - started) * 1000)
                if 200 <= resp.status_code < 300:
                    log.info("resend %s attempt=%d status=%d ms=%d idempotency_key=%s",
                             path, attempt, resp.status_code, ms, idempotency_key or "-")
                    return resp.json()
                err = _error_from_response(resp)
                log.warning("resend %s attempt=%d status=%d error=%s ms=%d",
                            path, attempt, resp.status_code, err.name, ms)
                if not err.retryable:
                    raise err
                last_error = err
                if err.retry_after is not None:
                    if err.retry_after > self.max_retry_after:
                        log.error("resend %s Retry-After %.0fs is above the %.0fs cap, giving up",
                                  path, err.retry_after, self.max_retry_after)
                        raise err
                    wait = err.retry_after
                else:
                    wait = self._backoff(attempt)
            if attempt < self.max_attempts:
                log.info("resend %s retrying in %.2fs", path, wait)
                self._sleep(wait)
        raise RetriesExhaustedError(self.max_attempts, last_error)
