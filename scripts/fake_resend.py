"""A tiny local stand-in for the Resend API, for end-to-end runs with no account.

It is NOT Resend. It mimics only what this project relies on:
  - POST /emails and POST /emails/batch, Bearer auth required
  - 422 missing_required_field when from/to/subject is missing
  - Idempotency-Key: same key + same body returns the first id;
    same key + different body returns 409 invalid_idempotent_request
  - --fail-first N: the first N requests get 429 (Retry-After: 1) then 503
Every request is appended to --log as one JSON line (Authorization is never logged).

    python scripts/fake_resend.py --port 8846 --fail-first 2 --log fake-resend.jsonl
"""
import argparse
import hashlib
import json
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"seen": 0, "keys": {}}


def make_handler(fail_first, log_path):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, status, body, headers=None):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            STATE["seen"] += 1
            n = STATE["seen"]
            key = self.headers.get("Idempotency-Key")
            entry = {"n": n, "path": self.path, "idempotency_key": key,
                     "has_bearer": (self.headers.get("Authorization") or "").startswith("Bearer ")}
            def done(status, body, headers=None):
                entry["status"] = status
                with open(log_path, "a") as fh:
                    fh.write(json.dumps(entry) + "\n")
                self._send(status, body, headers)

            if self.path not in ("/emails", "/emails/batch"):
                return done(404, {"statusCode": 404, "name": "not_found", "message": "not found"})
            if not entry["has_bearer"]:
                return done(401, {"statusCode": 401, "name": "missing_api_key", "message": "Missing API key"})
            if n <= fail_first:
                if n % 2 == 1:
                    return done(429, {"statusCode": 429, "name": "rate_limit_exceeded",
                                      "message": "Too many requests"}, {"Retry-After": "1"})
                return done(503, {"statusCode": 503, "name": "service_unavailable", "message": "try again"})
            body = json.loads(raw or b"null")
            emails = body if self.path == "/emails/batch" else [body]
            for e in emails:
                missing = [f for f in ("from", "to", "subject") if not e.get(f)]
                if missing:
                    return done(422, {"statusCode": 422, "name": "missing_required_field",
                                      "message": f"Missing `{missing[0]}` field."})
            digest = hashlib.sha256(raw).hexdigest()
            if key and key in STATE["keys"]:
                first_digest, first_body = STATE["keys"][key]
                if first_digest != digest:
                    return done(409, {"statusCode": 409, "name": "invalid_idempotent_request",
                                      "message": "Same idempotency key used with a different request payload."})
                entry["replayed"] = True
                return done(200, first_body)
            ids = [str(uuid.uuid4()) for _ in emails]
            entry["to"] = [e["to"] for e in emails]
            entry["subjects"] = [e["subject"] for e in emails]
            result = {"data": [{"id": i} for i in ids]} if self.path == "/emails/batch" else {"id": ids[0]}
            if key:
                STATE["keys"][key] = (digest, result)
            return done(200, result)
    return H


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8846)
    ap.add_argument("--fail-first", type=int, default=0)
    ap.add_argument("--log", default="fake-resend.jsonl")
    a = ap.parse_args()
    ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(a.fail_first, a.log)).serve_forever()
