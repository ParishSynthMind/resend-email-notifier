import base64
import json
import threading
import time
import urllib.request
from pathlib import Path
from urllib.error import HTTPError

import pytest

from resend_notifier.errors import WebhookVerificationError
from resend_notifier.server import build_server
from resend_notifier.webhook import handle_event, sign, verify

SECRET = "whsec_" + base64.b64encode(b"local-test-secret-32-bytes-long!").decode()
OTHER_SECRET = "whsec_" + base64.b64encode(b"some-other-secret-32-bytes-long!").decode()
BODY = (Path(__file__).resolve().parent.parent / "samples" / "email.bounced.json").read_bytes()


def headers_for(body, secret=SECRET, ts=None, msg_id="msg_test_1"):
    ts = int(time.time()) if ts is None else ts
    return {"svix-id": msg_id, "svix-timestamp": str(ts), "svix-signature": sign(body, msg_id, ts, secret)}


def test_official_svix_test_vector():
    # Test case published in Svix's manual verification guide.
    body = b'{"event_type":"ping","data":{"success":true}}'
    h = {"svix-id": "msg_loFOjxBNrRLzqYUf", "svix-timestamp": "1731705121",
         "svix-signature": "v1,rAvfW3dJ/X/qxhsaXPOyyCGmRKsaKWcsNccKXlIktD0="}
    event = verify(body, h, "whsec_plJ3nmyCDGBKInavdOK15jsl", now=1731705121)
    assert event["event_type"] == "ping"


def test_valid_signature():
    assert verify(BODY, headers_for(BODY), SECRET)["type"] == "email.bounced"


def test_headers_are_case_insensitive():
    h = {k.upper(): v for k, v in headers_for(BODY).items()}
    assert verify(BODY, h, SECRET)["type"] == "email.bounced"


def test_tampered_body_rejected():
    h = headers_for(BODY)
    tampered = BODY.replace(b"bounce-me@example.com", b"someone-else@example.com")
    with pytest.raises(WebhookVerificationError, match="no matching signature"):
        verify(tampered, h, SECRET)


def test_wrong_secret_rejected():
    with pytest.raises(WebhookVerificationError):
        verify(BODY, headers_for(BODY, secret=OTHER_SECRET), SECRET)


def test_old_timestamp_rejected():
    old = int(time.time()) - 6 * 60
    with pytest.raises(WebhookVerificationError, match="timestamp"):
        verify(BODY, headers_for(BODY, ts=old), SECRET)


def test_future_timestamp_rejected():
    with pytest.raises(WebhookVerificationError, match="timestamp"):
        verify(BODY, headers_for(BODY, ts=int(time.time()) + 6 * 60), SECRET)


def test_timestamp_inside_window_accepted():
    assert verify(BODY, headers_for(BODY, ts=int(time.time()) - 4 * 60), SECRET)


def test_multiple_signatures_any_match():
    h = headers_for(BODY)
    good = h["svix-signature"]
    wrong = sign(BODY, "msg_test_1", int(h["svix-timestamp"]), OTHER_SECRET)
    h["svix-signature"] = f"{wrong} v2,ignored {good}"
    assert verify(BODY, h, SECRET)
    h["svix-signature"] = f"{wrong} v1,AAAA"
    with pytest.raises(WebhookVerificationError):
        verify(BODY, h, SECRET)


def test_signature_with_other_version_ignored():
    h = headers_for(BODY)
    h["svix-signature"] = h["svix-signature"].replace("v1,", "v2,")
    with pytest.raises(WebhookVerificationError):
        verify(BODY, h, SECRET)


def test_missing_headers_and_bad_inputs():
    with pytest.raises(WebhookVerificationError, match="missing"):
        verify(BODY, {}, SECRET)
    h = headers_for(BODY)
    h["svix-timestamp"] = "yesterday"
    with pytest.raises(WebhookVerificationError):
        verify(BODY, h, SECRET)
    with pytest.raises(WebhookVerificationError):
        verify(BODY, headers_for(BODY), "")
    with pytest.raises(WebhookVerificationError):
        verify(BODY, headers_for(BODY), "whsec_***not-base64***")


def test_webhook_prefix_headers_accepted():
    h = {k.replace("svix-", "webhook-"): v for k, v in headers_for(BODY).items()}
    assert verify(BODY, h, SECRET)


def _event(etype, to, bounce_type=None):
    data = {"email_id": "em_x", "to": to}
    if bounce_type:
        data["bounce"] = {"type": bounce_type, "subType": "General", "message": "x"}
    return {"type": etype, "created_at": "2026-11-02T00:00:00Z", "data": data}


def test_events_drive_suppression(store):
    handle_event(store, "m1", _event("email.bounced", ["Hard@Example.com"], "Permanent"))
    handle_event(store, "m2", _event("email.complained", ["angry@example.com"]))
    handle_event(store, "m3", _event("email.bounced", ["soft@example.com"], "Transient"))
    handle_event(store, "m4", _event("email.delivered", ["fine@example.com"]))
    assert store.is_suppressed("hard@example.com")
    assert store.is_suppressed("angry@example.com")
    assert not store.is_suppressed("soft@example.com")
    assert not store.is_suppressed("fine@example.com")
    assert len(store.events()) == 4


def test_duplicate_delivery_recorded_once(store):
    first = handle_event(store, "m1", _event("email.bounced", ["x@example.com"], "Permanent"))
    again = handle_event(store, "m1", _event("email.bounced", ["x@example.com"], "Permanent"))
    assert first["suppressed"] == ["x@example.com"] and again["duplicate"] is True
    assert len(store.events()) == 1


@pytest.fixture
def live_server(store):
    httpd = build_server("127.0.0.1", 0, store, SECRET)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _post(url, body, headers):
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except HTTPError as e:
        return e.code, json.loads(e.read())


def test_server_end_to_end(live_server, store):
    url = live_server + "/webhooks/resend"
    status, body = _post(url, BODY, headers_for(BODY))
    assert status == 200 and body["suppressed"] == ["bounce-me@example.com"]
    assert store.is_suppressed("bounce-me@example.com")
    status, _ = _post(url, BODY.replace(b"Permanent", b"Transient"), headers_for(BODY))
    assert status == 400
    status, _ = _post(url, BODY, {})
    assert status == 400
    status, _ = _post(live_server + "/other", BODY, headers_for(BODY))
    assert status == 404
    with urllib.request.urlopen(live_server + "/health") as r:
        assert r.status == 200
