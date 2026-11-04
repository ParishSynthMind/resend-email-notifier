import json
import logging

import pytest
import requests
import responses

from resend_notifier import idempotency_key
from resend_notifier.client import _parse_retry_after
from resend_notifier.errors import (
    ConfigError,
    IdempotencyConflictError,
    QuotaExceededError,
    RateLimitError,
    RetriesExhaustedError,
    ValidationError,
)

from .conftest import FAKE_KEY

URL = "https://api.resend.com/emails"
EMAIL = {"from": "Shop <orders@shop.example.com>", "to": ["a@example.com"],
         "subject": "Hi", "html": "<p>Hi</p>", "text": "Hi"}


@responses.activate
def test_send_payload_and_headers(client):
    responses.post(URL, json={"id": "em_1"})
    key = idempotency_key("order-1", scope="order_confirmation")
    assert client.send(EMAIL, idempotency_key=key) == "em_1"
    req = responses.calls[0].request
    assert json.loads(req.body) == EMAIL
    assert req.headers["Authorization"] == f"Bearer {FAKE_KEY}"
    assert req.headers["Idempotency-Key"] == key
    assert req.headers["Content-Type"] == "application/json"


def test_idempotency_key_is_deterministic_and_scoped():
    a = idempotency_key("order-1", scope="order_confirmation")
    assert a == idempotency_key("order-1", scope="order_confirmation")
    assert a != idempotency_key("order-2", scope="order_confirmation")
    assert a != idempotency_key("order-1", scope="admin_alert")
    assert a.startswith("order_confirmation/") and len(a) <= 256
    with pytest.raises(ValueError):
        idempotency_key("  ")


@responses.activate
def test_retries_5xx_with_exponential_backoff_then_succeeds(client, sleeps):
    responses.post(URL, status=500, json={"name": "application_error", "message": "boom"})
    responses.post(URL, status=503, json={"name": "service_unavailable", "message": "later"})
    responses.post(URL, json={"id": "em_2"})
    assert client.send(EMAIL, idempotency_key="k") == "em_2"
    assert len(responses.calls) == 3
    # equal jitter with rng=0.5: ceiling*0.75 -> 1.0*0.75, 2.0*0.75
    assert sleeps == [0.75, 1.5]
    # same idempotency key on every attempt, so a retry can never double-send
    assert {c.request.headers["Idempotency-Key"] for c in responses.calls} == {"k"}


@responses.activate
def test_backoff_is_capped(client, sleeps):
    for _ in range(4):
        responses.post(URL, status=502, body="bad gateway")
    with pytest.raises(RetriesExhaustedError) as exc:
        client.send(EMAIL)
    assert exc.value.attempts == 4 and len(responses.calls) == 4
    assert sleeps == [0.75, 1.5, 3.0]


def test_backoff_jitter_range(client):
    client._rng = lambda: 0.0
    low = client._backoff(10)
    client._rng = lambda: 0.999999
    high = client._backoff(10)
    assert low == pytest.approx(4.0) and high == pytest.approx(8.0, rel=1e-3)


@responses.activate
def test_429_honours_retry_after(client, sleeps):
    responses.post(URL, status=429, headers={"Retry-After": "3"},
                   json={"name": "rate_limit_exceeded", "message": "Too many requests"})
    responses.post(URL, json={"id": "em_3"})
    assert client.send(EMAIL) == "em_3"
    assert sleeps == [3.0]


@responses.activate
def test_retry_after_above_cap_gives_up(client, sleeps):
    responses.post(URL, status=429, headers={"Retry-After": "3600"},
                   json={"name": "rate_limit_exceeded", "message": "slow down"})
    with pytest.raises(RateLimitError):
        client.send(EMAIL)
    assert sleeps == [] and len(responses.calls) == 1


@responses.activate
def test_quota_exceeded_is_not_retried(client, sleeps):
    responses.post(URL, status=429, json={"name": "daily_quota_exceeded", "message": "quota"})
    with pytest.raises(QuotaExceededError):
        client.send(EMAIL)
    assert len(responses.calls) == 1 and sleeps == []


@pytest.mark.parametrize("status,name", [(422, "missing_required_field"), (400, "validation_error"),
                                         (401, "missing_api_key"), (403, "validation_error")])
@responses.activate
def test_4xx_is_never_retried(client, sleeps, status, name):
    responses.post(URL, status=status, json={"statusCode": status, "name": name, "message": "nope"})
    with pytest.raises(ValidationError) as exc:
        client.send(EMAIL)
    assert exc.value.status == status and exc.value.name == name
    assert len(responses.calls) == 1 and sleeps == []


@responses.activate
def test_409_reused_key_with_different_body(client):
    responses.post(URL, status=409, json={"name": "invalid_idempotent_request", "message": "changed body"})
    with pytest.raises(IdempotencyConflictError):
        client.send(EMAIL, idempotency_key="k")
    assert len(responses.calls) == 1


@responses.activate
def test_409_concurrent_request_is_retried(client, sleeps):
    responses.post(URL, status=409, json={"name": "concurrent_idempotent_requests", "message": "in flight"})
    responses.post(URL, json={"id": "em_4"})
    assert client.send(EMAIL, idempotency_key="k") == "em_4"
    assert len(sleeps) == 1


@responses.activate
def test_network_errors_are_retried(client, sleeps):
    responses.post(URL, body=requests.ConnectionError("reset"))
    responses.post(URL, body=requests.Timeout("slow"))
    responses.post(URL, json={"id": "em_5"})
    assert client.send(EMAIL) == "em_5"
    assert len(sleeps) == 2


@responses.activate
def test_batch_send(client):
    responses.post(URL + "/batch", json={"data": [{"id": "b1"}, {"id": "b2"}]})
    ids = client.send_batch([EMAIL, {**EMAIL, "to": ["b@example.com"]}], idempotency_key="bk")
    assert ids == ["b1", "b2"]
    body = json.loads(responses.calls[0].request.body)
    assert isinstance(body, list) and len(body) == 2
    assert responses.calls[0].request.headers["Idempotency-Key"] == "bk"


def test_batch_limits(client):
    with pytest.raises(ValueError):
        client.send_batch([EMAIL] * 101)
    with pytest.raises(ValueError):
        client.send_batch([{**EMAIL, "attachments": [{"filename": "a.txt", "content": "eA=="}]}])


def test_missing_key_and_fields():
    with pytest.raises(ConfigError):
        from resend_notifier import ResendClient
        ResendClient(api_key="")


def test_missing_required_field_is_caught_locally(client):
    with pytest.raises(ValueError):
        client.send({"from": "a@x.com", "to": "b@x.com"})


@responses.activate
def test_api_key_never_logged(client, caplog):
    caplog.set_level(logging.DEBUG)
    responses.post(URL, status=500, json={"name": "application_error", "message": "x"})
    responses.post(URL, json={"id": "em_6"})
    client.send(EMAIL, idempotency_key="k")
    assert FAKE_KEY not in caplog.text
    assert FAKE_KEY not in repr(client)
    assert "attempt=2 status=200" in caplog.text


def test_parse_retry_after_forms():
    assert _parse_retry_after("2") == 2.0
    assert _parse_retry_after("1.5") == 1.5
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("garbage") is None
    assert _parse_retry_after("Wed, 21 Oct 2015 07:28:10 GMT", now=1445412480.0) == pytest.approx(10.0)
