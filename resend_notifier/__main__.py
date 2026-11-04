"""Command line.

    python -m resend_notifier send --event-id order-1001 --template order_confirmation \
        --to customer@example.com --context samples/order.json
    python -m resend_notifier batch samples/batch.json
    python -m resend_notifier serve --port 8000
    python -m resend_notifier suppress list | add ADDRESS | remove ADDRESS
    python -m resend_notifier events

Configuration comes from the environment: RESEND_API_KEY, RESEND_FROM,
RESEND_REPLY_TO, RESEND_WEBHOOK_SECRET, SUPPRESSION_DB, TEMPLATE_DIR,
RESEND_BASE_URL (only to point at a local fake API in tests).
"""
import argparse
import json
import logging
import os
import sys
from pathlib import Path

from .client import ResendClient
from .errors import ResendError
from .notifier import BatchItem, Notifier
from .suppression import SuppressionStore
from .templates import TemplateRenderer

DEFAULT_TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


def _context(value: str) -> dict:
    if value.startswith("{"):
        return json.loads(value)
    return json.loads(Path(value).read_text(encoding="utf-8"))


def _notifier(args) -> Notifier:
    sender = os.environ.get("RESEND_FROM")
    if not sender:
        raise SystemExit("RESEND_FROM is not set (for example 'Shop <orders@yourdomain.com>')")
    return Notifier(
        client=ResendClient(),
        renderer=TemplateRenderer(os.environ.get("TEMPLATE_DIR", str(DEFAULT_TEMPLATES))),
        suppressions=SuppressionStore(args.db),
        from_address=sender,
        reply_to=os.environ.get("RESEND_REPLY_TO") or None,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="resend_notifier")
    parser.add_argument("--db", default=os.environ.get("SUPPRESSION_DB", "suppressions.db"))
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("send", help="render a template and send one email")
    s.add_argument("--event-id", required=True, help="your business event ID, e.g. order-1001")
    s.add_argument("--template", required=True)
    s.add_argument("--to", required=True, action="append")
    s.add_argument("--context", default="{}", help="JSON string or path to a JSON file")

    b = sub.add_parser("batch", help="send a JSON list of emails via /emails/batch")
    b.add_argument("file")

    v = sub.add_parser("serve", help="run the webhook receiver")
    v.add_argument("--host", default=None)
    v.add_argument("--port", type=int, default=None)

    sp = sub.add_parser("suppress", help="manage the suppression list")
    sp.add_argument("action", choices=["list", "add", "remove"])
    sp.add_argument("address", nargs="?")

    sub.add_parser("events", help="show recent webhook events")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    try:
        if args.cmd == "send":
            result = _notifier(args).send(args.event_id, args.template, args.to, _context(args.context))
            print(json.dumps(result.__dict__))
        elif args.cmd == "batch":
            items = [BatchItem(**i) for i in json.loads(Path(args.file).read_text(encoding="utf-8"))]
            for result in _notifier(args).send_batch(items):
                print(json.dumps(result.__dict__))
        elif args.cmd == "serve":
            from .server import serve
            serve(args.host, args.port, args.db)
        elif args.cmd == "suppress":
            store = SuppressionStore(args.db)
            if args.action == "list":
                for row in store.list():
                    print(json.dumps(row))
            elif not args.address:
                parser.error("an address is required")
            elif args.action == "add":
                print("added" if store.add(args.address, "manual") else "already suppressed")
            else:
                print("removed" if store.remove(args.address) else "not on the list")
        elif args.cmd == "events":
            for row in SuppressionStore(args.db).events():
                print(json.dumps(row))
    except ResendError as exc:
        logging.getLogger("resend_notifier").error("%s: %s", type(exc).__name__, exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
