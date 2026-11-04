"""The n8n exports: valid JSON, wired correctly, no secrets, and the JS helpers
inside the Code nodes agree with the Python package (run with Node if present)."""
import base64
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from resend_notifier import idempotency_key
from resend_notifier.webhook import sign

ROOT = Path(__file__).resolve().parent.parent
N8N = ROOT / "n8n"
FILES = ["resend-send-email.json", "resend-events.json"]


def load(name):
    return json.loads((N8N / name).read_text())


@pytest.mark.parametrize("name", FILES)
def test_workflow_is_valid_and_wired(name):
    wf = load(name)
    names = {n["name"] for n in wf["nodes"]}
    assert len(names) == len(wf["nodes"]), "duplicate node names"
    for n in wf["nodes"]:
        assert n["type"].startswith("n8n-nodes-base.") and "typeVersion" in n and "position" in n
    for src, conn in wf["connections"].items():
        assert src in names
        for branch in conn["main"]:
            for target in branch:
                assert target["node"] in names
    # every node except triggers has an incoming connection
    targets = {t["node"] for c in wf["connections"].values() for b in c["main"] for t in b}
    for n in wf["nodes"]:
        if n["type"] != "n8n-nodes-base.webhook":
            assert n["name"] in targets, f"{n['name']} is not connected"


SECRET_PATTERNS = [r"re_[A-Za-z0-9]{8,}", r"whsec_[A-Za-z0-9+/=]{8,}", r"Bearer\s+[A-Za-z0-9]",
                   r"sk-ant-", r"ghp_", r"@gmail\.com", r"/Users/"]


@pytest.mark.parametrize("name", FILES)
def test_no_secrets_in_export(name):
    text = (N8N / name).read_text()
    for pat in SECRET_PATTERNS:
        assert not re.search(pat, text), f"{pat} found in {name}"
    for n in load(name)["nodes"]:
        for cred in (n.get("credentials") or {}).values():
            assert set(cred) == {"name"}, "credentials must be referenced by name only"


def test_send_workflow_http_node():
    wf = load("resend-send-email.json")
    http = next(n for n in wf["nodes"] if n["name"] == "Resend: send email")
    p = http["parameters"]
    assert p["url"] == "={{ $('Config').item.json.resend_api_url }}" and p["method"] == "POST"
    cfg = next(n for n in wf["nodes"] if n["name"] == "Config")
    urls = {a["name"]: a["value"] for a in cfg["parameters"]["assignments"]["assignments"]}
    assert urls["resend_api_url"] == "https://api.resend.com/emails"  # real Resend by default
    headers = {h["name"]: h["value"] for h in p["headerParameters"]["parameters"]}
    assert "Idempotency-Key" in headers
    assert http["retryOnFail"] is True and http["maxTries"] >= 3
    assert http["credentials"] == {"httpHeaderAuth": {"name": "Resend API key"}}
    assert wf["connections"]["Resend: send email"]["main"][1][0]["node"] == "502 send failed"


def test_events_workflow_uses_raw_body_and_env_secret():
    wf = load("resend-events.json")
    hook = next(n for n in wf["nodes"] if n["name"] == "Resend events")
    assert hook["parameters"]["options"]["rawBody"] is True
    verify = next(n for n in wf["nodes"] if n["name"] == "Verify Svix signature")
    assert "$env.RESEND_WEBHOOK_SECRET" in verify["parameters"]["jsCode"]


def test_exports_match_build_script(tmp_path):
    """The committed JSON must be what scripts/build_n8n.py produces from n8n/src."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_n8n", ROOT / "scripts" / "build_n8n.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert load("resend-send-email.json") == json.loads(json.dumps(mod.send_workflow()))
    assert load("resend-events.json") == json.loads(json.dumps(mod.events_workflow()))


NODE = shutil.which("node")
SECRET = "whsec_" + base64.b64encode(b"local-test-secret-32-bytes-long!").decode()


def run_node(script: str):
    full = f"const lib = require({json.dumps(str(N8N / 'src' / 'lib.js'))});\n{script}"
    out = subprocess.run([NODE, "-e", full], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_js_idempotency_key_matches_python():
    got = run_node("console.log(JSON.stringify(lib.idempotencyKey('order-A-1001', 'order_confirmation')))")
    assert got == idempotency_key("order-A-1001", "order_confirmation")


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_js_svix_verification():
    body = (ROOT / "samples" / "email.bounced.json").read_text()
    ts = int(time.time())
    good = sign(body.encode(), "msg_1", ts, SECRET)
    other = "whsec_" + base64.b64encode(b"some-other-secret-32-bytes-long!").decode()
    script = f"""
const body = {json.dumps(body)};
const h = (sig, t) => ({{'svix-id': 'msg_1', 'svix-timestamp': String(t), 'svix-signature': sig}});
const out = {{
  official: lib.verifySvix('{{"event_type":"ping","data":{{"success":true}}}}',
     {{'svix-id':'msg_loFOjxBNrRLzqYUf','svix-timestamp':'1731705121',
       'svix-signature':'v1,rAvfW3dJ/X/qxhsaXPOyyCGmRKsaKWcsNccKXlIktD0='}},
     'whsec_plJ3nmyCDGBKInavdOK15jsl', 1731705121).valid,
  valid: lib.verifySvix(body, h({json.dumps(good)}, {ts}), {json.dumps(SECRET)}).valid,
  tampered: lib.verifySvix(body.replace('bounce-me', 'other'), h({json.dumps(good)}, {ts}), {json.dumps(SECRET)}).reason,
  wrongSecret: lib.verifySvix(body, h({json.dumps(good)}, {ts}), {json.dumps(other)}).reason,
  old: lib.verifySvix(body, h({json.dumps(good)}, {ts}), {json.dumps(SECRET)}, {ts} + 301).reason,
  multi: lib.verifySvix(body, h('v1,AAAA ' + {json.dumps(good)}, {ts}), {json.dumps(SECRET)}).valid,
}};
const store = {{}};
const ev = JSON.parse(body);
out.first = lib.applyEvent(store, 'm1', ev, 'now');
out.again = lib.applyEvent(store, 'm1', ev, 'now');
out.store = store.suppressed;
out.built = lib.buildEmail({{event_id: 'e1', to: 'A@Example.com', subject: 'Hi\\nthere', text: 'x'}}, {{from: 'f@x.example'}});
out.invalid = lib.buildEmail({{}}, {{}});
console.log(JSON.stringify(out));
"""
    r = run_node(script)
    assert r["official"] is True and r["valid"] is True and r["multi"] is True
    assert r["tampered"] == "no matching signature"
    assert r["wrongSecret"] == "no matching signature"
    assert r["old"] == "timestamp outside the allowed window"
    assert r["first"]["suppressed"] == ["bounce-me@example.com"] and r["again"]["duplicate"] is True
    assert r["store"]["bounce-me@example.com"]["reason"] == "bounced"
    assert r["built"]["ok"] and r["built"]["payload"]["subject"] == "Hi there"
    assert r["built"]["idempotencyKey"] == idempotency_key("e1", "n8n-email")
    assert r["invalid"]["ok"] is False and len(r["invalid"]["errors"]) >= 3
