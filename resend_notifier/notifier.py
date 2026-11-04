"""The piece an app actually calls: render a template, skip suppressed
addresses, and send with an idempotency key tied to the business event."""
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Union

from .client import BATCH_LIMIT, ResendClient
from .idempotency import batch_idempotency_key, idempotency_key
from .suppression import SuppressionStore
from .templates import TemplateRenderer

log = logging.getLogger("resend_notifier.notifier")


def _mask(address: str) -> str:
    """j***@example.com: enough to debug, not a full address in the logs."""
    local, _, domain = address.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


@dataclass
class SendResult:
    status: str                      # "sent" or "suppressed"
    event_id: str
    idempotency_key: Optional[str] = None
    email_id: Optional[str] = None
    skipped: List[str] = field(default_factory=list)


@dataclass
class BatchItem:
    event_id: str
    template: str
    to: Union[str, Sequence[str]]
    context: Dict[str, Any]
    tags: Optional[Dict[str, str]] = None


class Notifier:
    def __init__(self, client: ResendClient, renderer: TemplateRenderer,
                 suppressions: SuppressionStore, from_address: str,
                 reply_to: Optional[str] = None):
        self.client = client
        self.renderer = renderer
        self.suppressions = suppressions
        self.from_address = from_address
        self.reply_to = reply_to

    def _payload(self, template: str, to: List[str], context: Dict[str, Any],
                 tags: Optional[Dict[str, str]]) -> Dict[str, Any]:
        rendered = self.renderer.render(template, context)
        payload: Dict[str, Any] = {
            "from": self.from_address,
            "to": to,
            "subject": rendered.subject,
            "html": rendered.html,
            "text": rendered.text,
        }
        if self.reply_to:
            payload["reply_to"] = self.reply_to
        if tags:
            payload["tags"] = [{"name": k, "value": v} for k, v in tags.items()]
        return payload

    def _split(self, to: Union[str, Sequence[str]]):
        recipients = [to] if isinstance(to, str) else list(to)
        if not recipients:
            raise ValueError("at least one recipient is required")
        allowed = [r for r in recipients if not self.suppressions.is_suppressed(r)]
        skipped = [r for r in recipients if r not in allowed]
        return allowed, skipped

    def send(self, event_id: str, template: str, to: Union[str, Sequence[str]],
             context: Dict[str, Any], tags: Optional[Dict[str, str]] = None) -> SendResult:
        allowed, skipped = self._split(to)
        for r in skipped:
            log.info("event=%s skipping suppressed recipient %s", event_id, _mask(r))
        if not allowed:
            return SendResult(status="suppressed", event_id=event_id, skipped=skipped)
        key = idempotency_key(event_id, scope=template)
        payload = self._payload(template, allowed, context, tags)
        email_id = self.client.send(payload, idempotency_key=key)
        log.info("event=%s template=%s sent email_id=%s", event_id, template, email_id)
        return SendResult(status="sent", event_id=event_id, idempotency_key=key,
                          email_id=email_id, skipped=skipped)

    def send_batch(self, items: Sequence[BatchItem]) -> List[SendResult]:
        """Send many emails with POST /emails/batch, 100 per call.

        Suppressed recipients are dropped first; an item with nobody left is
        returned as "suppressed" and not sent.
        """
        results: List[SendResult] = []
        to_send = []
        for item in items:
            allowed, skipped = self._split(item.to)
            if not allowed:
                results.append(SendResult(status="suppressed", event_id=item.event_id, skipped=skipped))
                continue
            payload = self._payload(item.template, allowed, item.context, item.tags)
            result = SendResult(status="sent", event_id=item.event_id, skipped=skipped)
            results.append(result)
            to_send.append((result, payload))

        for start in range(0, len(to_send), BATCH_LIMIT):
            chunk = to_send[start:start + BATCH_LIMIT]
            key = batch_idempotency_key([r.event_id for r, _ in chunk])
            ids = self.client.send_batch([p for _, p in chunk], idempotency_key=key)
            if len(ids) != len(chunk):
                raise RuntimeError(f"batch returned {len(ids)} ids for {len(chunk)} emails")
            for (result, _), email_id in zip(chunk, ids):
                result.email_id = email_id
                result.idempotency_key = key
        return results
