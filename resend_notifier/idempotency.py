"""Deterministic Idempotency-Key values.

Resend keeps an Idempotency-Key for 24 hours. A retry, a duplicate job run or
a replayed event that reuses the key gets the original email back instead of
sending a second copy. The key must be the same every time for the same
business event, so it is derived from the event ID, never random.
"""
import hashlib
import re
from typing import Iterable

MAX_KEY_LENGTH = 256  # Resend's documented limit
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def idempotency_key(event_id: str, scope: str = "email") -> str:
    """Key for one email triggered by one event.

    ``scope`` separates different emails caused by the same event (for
    example an order receipt and an internal alert both triggered by
    order 1234), which would otherwise collide.
    """
    if not event_id or not str(event_id).strip():
        raise ValueError("event_id is required for an idempotency key")
    digest = hashlib.sha256(f"{scope}\x1f{event_id}".encode("utf-8")).hexdigest()
    safe_scope = _SAFE.sub("-", scope)[:64] or "email"
    return f"{safe_scope}/{digest}"


def batch_idempotency_key(event_ids: Iterable[str], scope: str = "batch") -> str:
    """Key for a batch call: stable for the same ordered set of event IDs."""
    ids = [str(e) for e in event_ids]
    if not ids:
        raise ValueError("a batch needs at least one event_id")
    return idempotency_key("\x1e".join(ids), scope=scope)
