"""Verify Resend webhooks (signed by Svix) and turn events into suppressions.

Resend signs each delivery with three headers: svix-id, svix-timestamp and
svix-signature. The signed content is ``{svix-id}.{svix-timestamp}.{raw body}``,
signed with HMAC-SHA256 using the base64-decoded part of the ``whsec_...``
secret. The signature header holds one or more space-separated
``v1,<base64>`` entries; any one matching is enough. Timestamps more than
5 minutes away from now are rejected to stop replays.
"""
import base64
import binascii
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict, List, Mapping, Optional

from .errors import WebhookVerificationError
from .suppression import SuppressionStore

log = logging.getLogger("resend_notifier.webhook")

DEFAULT_TOLERANCE_SECONDS = 5 * 60

# Events that should stop future sends to the recipient.
SUPPRESS_ON = {
    "email.bounced": "bounced",
    "email.complained": "complained",
    "email.suppressed": "suppressed_by_resend",
}


def _header(headers: Mapping[str, str], name: str) -> Optional[str]:
    """Case-insensitive lookup; accepts the svix-* and webhook-* prefixes."""
    lowered = {k.lower(): v for k, v in headers.items()}
    return lowered.get(f"svix-{name}") or lowered.get(f"webhook-{name}")


def _secret_bytes(secret: str) -> bytes:
    if not secret:
        raise WebhookVerificationError("webhook secret is not configured")
    raw = secret[len("whsec_"):] if secret.startswith("whsec_") else secret
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise WebhookVerificationError("webhook secret is not valid base64") from exc


def sign(payload: bytes, msg_id: str, timestamp: int, secret: str) -> str:
    """Build a ``v1,<sig>`` value. Used by tests and the local demo sender."""
    signed = f"{msg_id}.{timestamp}.".encode("utf-8") + payload
    digest = hmac.new(_secret_bytes(secret), signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode("ascii")


def verify(payload: bytes, headers: Mapping[str, str], secret: str,
           tolerance: int = DEFAULT_TOLERANCE_SECONDS, now: Optional[float] = None) -> Dict[str, Any]:
    """Verify a delivery and return the parsed JSON event.

    ``payload`` must be the raw request body bytes, exactly as received.
    Raises WebhookVerificationError on any failure.
    """
    msg_id = _header(headers, "id")
    timestamp = _header(headers, "timestamp")
    signatures = _header(headers, "signature")
    if not (msg_id and timestamp and signatures):
        raise WebhookVerificationError("missing svix-id, svix-timestamp or svix-signature header")
    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise WebhookVerificationError("svix-timestamp is not an integer") from exc
    now = time.time() if now is None else now
    if abs(now - ts) > tolerance:
        raise WebhookVerificationError("timestamp outside the allowed window")

    expected = sign(payload, msg_id, ts, secret).split(",", 1)[1].encode("ascii")
    matched = False
    for entry in signatures.split(" "):
        version, _, sig = entry.partition(",")
        if version != "v1" or not sig:
            continue
        if hmac.compare_digest(sig.encode("ascii", "ignore"), expected):
            matched = True
            break
    if not matched:
        raise WebhookVerificationError("no matching signature")
    try:
        event = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise WebhookVerificationError("body is not valid JSON") from exc
    if not isinstance(event, dict):
        raise WebhookVerificationError("body is not a JSON object")
    return event


def suppression_reason(event: Dict[str, Any]) -> Optional[str]:
    """Why this event should suppress its recipients, or None."""
    etype = event.get("type")
    reason = SUPPRESS_ON.get(etype)
    if etype == "email.bounced":
        bounce = (event.get("data") or {}).get("bounce") or {}
        # A transient (soft) bounce is a temporary problem: do not suppress.
        if str(bounce.get("type", "")).lower() == "transient":
            return None
    return reason


def handle_event(store: SuppressionStore, svix_id: str, event: Dict[str, Any]) -> Dict[str, Any]:
    """Record a verified event and suppress its recipients when needed."""
    is_new = store.record_event(svix_id, event)
    result: Dict[str, Any] = {"type": event.get("type"), "duplicate": not is_new, "suppressed": []}
    if not is_new:
        return result
    reason = suppression_reason(event)
    if reason:
        to = (event.get("data") or {}).get("to") or []
        recipients: List[str] = [to] if isinstance(to, str) else list(to)
        for address in recipients:
            if store.add(address, reason, source_event_id=svix_id):
                result["suppressed"].append(address)
        log.info("webhook %s type=%s suppressed=%d", svix_id, event.get("type"), len(result["suppressed"]))
    else:
        log.info("webhook %s type=%s recorded", svix_id, event.get("type"))
    return result
