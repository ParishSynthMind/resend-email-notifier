"""Print curl -H arguments for a Resend-style signed webhook (local testing only).

    python scripts/sign_webhook.py samples/email.bounced.json [--age 0] [--secret-env RESEND_WEBHOOK_SECRET]

Outputs three lines: svix-id, svix-timestamp and svix-signature headers.
--age shifts the timestamp into the past (to show replay protection).
"""
import argparse
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from resend_notifier.webhook import sign  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("body_file")
ap.add_argument("--age", type=int, default=0)
ap.add_argument("--secret-env", default="RESEND_WEBHOOK_SECRET")
a = ap.parse_args()
body = Path(a.body_file).read_bytes()
msg_id = "msg_" + uuid.uuid4().hex[:24]
ts = int(time.time()) - a.age
print(f"svix-id: {msg_id}")
print(f"svix-timestamp: {ts}")
print(f"svix-signature: {sign(body, msg_id, ts, os.environ[a.secret_env])}")
