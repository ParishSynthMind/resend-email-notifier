"""Transactional email through Resend: templates, idempotency, retries,
signed webhooks and a suppression list."""
__version__ = "0.1.0"

from .client import ResendClient  # noqa: E402
from .errors import *  # noqa: E402,F401,F403
from .idempotency import batch_idempotency_key, idempotency_key  # noqa: E402
from .notifier import BatchItem, Notifier, SendResult  # noqa: E402
from .suppression import SuppressionStore  # noqa: E402
from .templates import TemplateRenderer  # noqa: E402
from .webhook import verify as verify_webhook  # noqa: E402

__all__ = [
    "ResendClient", "Notifier", "SendResult", "BatchItem", "SuppressionStore",
    "TemplateRenderer", "verify_webhook", "idempotency_key", "batch_idempotency_key",
]
