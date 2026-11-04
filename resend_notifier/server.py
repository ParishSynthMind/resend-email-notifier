"""Webhook receiver for Resend events. Standard library only.

    POST /webhooks/resend   signed Resend event -> 200 {"ok": true, ...}
                            bad or missing signature -> 400
    GET  /health            -> 200 {"ok": true}

Resend retries a delivery that does not get a 2xx, so the handler stays
fast: verify, write two SQLite rows, answer.
"""
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from .errors import WebhookVerificationError
from .suppression import SuppressionStore
from .webhook import handle_event, verify

log = logging.getLogger("resend_notifier.server")

MAX_BODY_BYTES = 1024 * 1024
WEBHOOK_PATH = "/webhooks/resend"


def make_handler(store: SuppressionStore, secret: str, tolerance: int = 300):
    class Handler(BaseHTTPRequestHandler):
        server_version = "resend-email-notifier"

        def _reply(self, status: int, body: dict) -> None:
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):  # route access logs through logging
            log.debug("%s %s", self.address_string(), fmt % args)

        def do_GET(self):
            if self.path == "/health":
                self._reply(200, {"ok": True})
            else:
                self._reply(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            if self.path.split("?", 1)[0] != WEBHOOK_PATH:
                self._reply(404, {"ok": False, "error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY_BYTES:
                self._reply(413, {"ok": False, "error": "body too large or bad length"})
                return
            raw = self.rfile.read(length)
            try:
                event = verify(raw, dict(self.headers.items()), secret, tolerance=tolerance)
            except WebhookVerificationError as exc:
                log.warning("rejected webhook from %s: %s", self.client_address[0], exc)
                self._reply(400, {"ok": False, "error": "invalid signature"})
                return
            svix_id = self.headers.get("svix-id") or self.headers.get("webhook-id")
            try:
                result = handle_event(store, svix_id, event)
            except Exception:  # never leak internals; Resend will retry on 5xx
                log.exception("failed to store webhook %s", svix_id)
                self._reply(500, {"ok": False, "error": "storage error"})
                return
            self._reply(200, {"ok": True, **result})

    return Handler


def build_server(host: str, port: int, store: SuppressionStore, secret: str,
                 tolerance: int = 300) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(store, secret, tolerance))


def serve(host: Optional[str] = None, port: Optional[int] = None, db: Optional[str] = None) -> None:
    secret = os.environ.get("RESEND_WEBHOOK_SECRET", "")
    if not secret:
        raise SystemExit("RESEND_WEBHOOK_SECRET is not set")
    host = host or os.environ.get("HOST", "127.0.0.1")
    port = port or int(os.environ.get("PORT", "8000"))
    store = SuppressionStore(db or os.environ.get("SUPPRESSION_DB", "suppressions.db"))
    httpd = build_server(host, port, store, secret)
    log.info("listening on http://%s:%d%s", host, port, WEBHOOK_PATH)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        store.close()
