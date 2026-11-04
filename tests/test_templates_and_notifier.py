import json

import pytest
import responses

from resend_notifier import BatchItem, idempotency_key
from resend_notifier.errors import TemplateError

from .conftest import ROOT

URL = "https://api.resend.com/emails"


def test_render_html_escapes_user_input(renderer, order_context):
    r = renderer.render("order_confirmation", order_context)
    assert "&lt;the mugs&gt; &amp; add a card" in r.html
    assert "<the mugs>" not in r.html
    # plain text part is not HTML-escaped
    assert "<the mugs> & add a card" in r.text
    assert r.subject == "Order A-1001 confirmed, thanks Sam Rivera"
    assert "$54.50" in r.html and "$54.50" in r.text
    assert "2 x Ceramic mug" in r.text


def test_script_injection_is_escaped(renderer, order_context):
    order_context["customer_name"] = "<script>alert(1)</script>"
    r = renderer.render("order_confirmation", order_context)
    assert "<script>" not in r.html and "&lt;script&gt;" in r.html


def test_subject_cannot_inject_headers(renderer, order_context):
    order_context["customer_name"] = "Sam\r\nBcc: victim@example.com"
    r = renderer.render("order_confirmation", order_context)
    assert "\n" not in r.subject and "\r" not in r.subject


def test_missing_variable_raises(renderer):
    with pytest.raises(TemplateError):
        renderer.render("order_confirmation", {"customer_name": "x"})


def test_unknown_template_raises(renderer):
    with pytest.raises(TemplateError):
        renderer.render("nope", {})


@responses.activate
def test_notifier_sends_rendered_email_with_event_key(notifier, order_context):
    responses.post(URL, json={"id": "em_1"})
    res = notifier.send("order-A-1001", "order_confirmation", "Sam@Example.com", order_context,
                        tags={"category": "order"})
    assert res.status == "sent" and res.email_id == "em_1"
    req = responses.calls[0].request
    body = json.loads(req.body)
    assert body["from"] == "Shop <orders@shop.example.com>"
    assert body["to"] == ["Sam@Example.com"]
    assert body["subject"].startswith("Order A-1001")
    assert "html" in body and "text" in body
    assert body["tags"] == [{"name": "category", "value": "order"}]
    assert req.headers["Idempotency-Key"] == idempotency_key("order-A-1001", "order_confirmation")


@responses.activate
def test_suppressed_recipient_is_skipped(notifier, store, order_context):
    store.add("sam@example.com", "bounced")
    res = notifier.send("order-A-1001", "order_confirmation", "SAM@example.com ", order_context)
    assert res.status == "suppressed" and res.skipped == ["SAM@example.com "]
    assert len(responses.calls) == 0


@responses.activate
def test_partial_suppression_sends_to_the_rest(notifier, store, order_context):
    store.add("bad@example.com", "complained")
    responses.post(URL, json={"id": "em_2"})
    res = notifier.send("e1", "order_confirmation", ["Bad <bad@example.com>", "ok@example.com"], order_context)
    assert res.status == "sent" and res.skipped == ["Bad <bad@example.com>"]
    assert json.loads(responses.calls[0].request.body)["to"] == ["ok@example.com"]


@responses.activate
def test_batch_skips_suppressed_and_maps_ids(notifier, store):
    store.add("jo@example.com", "bounced")
    items = json.loads((ROOT / "samples" / "batch.json").read_text())
    items.append({**items[0], "event_id": "reset-79", "to": "kim@example.com"})
    responses.post(URL + "/batch", json={"data": [{"id": "b1"}, {"id": "b3"}]})
    results = notifier.send_batch([BatchItem(**i) for i in items])
    assert [r.status for r in results] == ["sent", "suppressed", "sent"]
    assert results[0].email_id == "b1" and results[2].email_id == "b3"
    body = json.loads(responses.calls[0].request.body)
    assert [b["to"] for b in body] == [["alex@example.com"], ["kim@example.com"]]
    assert "Idempotency-Key" in responses.calls[0].request.headers


@responses.activate
def test_batch_chunks_at_100(notifier):
    ctx = {"name": "A", "app_name": "X", "expires_minutes": 5, "reset_url": "https://x.example/r"}
    items = [BatchItem(f"e{i}", "password_reset", f"u{i}@example.com", ctx) for i in range(150)]
    responses.post(URL + "/batch", json={"data": [{"id": f"a{i}"} for i in range(100)]})
    responses.post(URL + "/batch", json={"data": [{"id": f"b{i}"} for i in range(50)]})
    results = notifier.send_batch(items)
    assert len(responses.calls) == 2
    assert results[0].email_id == "a0" and results[149].email_id == "b49"
    keys = {c.request.headers["Idempotency-Key"] for c in responses.calls}
    assert len(keys) == 2
