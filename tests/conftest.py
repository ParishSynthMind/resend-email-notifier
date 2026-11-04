from pathlib import Path

import pytest

from resend_notifier import Notifier, ResendClient, SuppressionStore, TemplateRenderer

ROOT = Path(__file__).resolve().parent.parent
FAKE_KEY = "re_test_not_a_real_key_123"


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def client(sleeps):
    # rng fixed at 0.5 so backoff values are predictable
    return ResendClient(api_key=FAKE_KEY, base_url="https://api.resend.com",
                        max_attempts=4, backoff_base=1.0, backoff_max=8.0,
                        sleep=sleeps.append, rng=lambda: 0.5)


@pytest.fixture
def store(tmp_path):
    s = SuppressionStore(tmp_path / "s.db")
    yield s
    s.close()


@pytest.fixture
def renderer():
    return TemplateRenderer(ROOT / "templates")


@pytest.fixture
def notifier(client, renderer, store):
    return Notifier(client, renderer, store, from_address="Shop <orders@shop.example.com>")


@pytest.fixture
def order_context():
    import json
    return json.loads((ROOT / "samples" / "order.json").read_text())
