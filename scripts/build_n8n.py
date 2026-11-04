"""Build the two importable n8n workflows from n8n/src/lib.js plus per-node glue.

    python scripts/build_n8n.py         # writes n8n/resend-send-email.json and n8n/resend-events.json

Credentials are referenced by name only (no IDs, no values): after import,
pick or create "Resend API key" and "Notifier caller token" in n8n.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = (ROOT / "n8n" / "src" / "lib.js").read_text(encoding="utf-8")

RESEND_CRED = {"httpHeaderAuth": {"name": "Resend API key"}}
CALLER_CRED = {"httpHeaderAuth": {"name": "Notifier caller token"}}


def code(glue: str) -> str:
    return LIB + "\n// ---- n8n glue ----\n" + glue.strip() + "\n"


def node(name, ntype, version, pos, params, **extra):
    n = {"parameters": params, "name": name, "type": ntype, "typeVersion": version,
         "position": list(pos), "id": "n-" + name.lower().replace(" ", "-").replace(":", "")[:40]}
    n.update(extra)
    return n


def respond(name, pos, code_value, body_expr):
    return node(name, "n8n-nodes-base.respondToWebhook", 1.1, pos, {
        "respondWith": "json",
        "responseBody": body_expr,
        "options": {"responseCode": code_value},
    })


def connect(*pairs):
    """pairs of (from, to, output_index)."""
    out = {}
    for src, dst, idx in pairs:
        main = out.setdefault(src, {"main": []})["main"]
        while len(main) <= idx:
            main.append([])
        main[idx].append({"node": dst, "type": "main", "index": 0})
    return out


SETTINGS = {"executionOrder": "v1", "saveManualExecutions": True,
            "callerPolicy": "workflowsFromSameOwner", "errorWorkflow": ""}


def if_node(name, pos, left_expr):
    return node(name, "n8n-nodes-base.if", 2, pos, {
        "conditions": {
            "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose"},
            "conditions": [{"id": "c1", "leftValue": left_expr, "rightValue": "",
                            "operator": {"type": "boolean", "operation": "true", "singleValue": True}}],
            "combinator": "and",
        },
        "options": {},
    })


def send_workflow():
    nodes = [
        node("Send request", "n8n-nodes-base.webhook", 2, (0, 0), {
            "httpMethod": "POST", "path": "send-email", "authentication": "headerAuth",
            "responseMode": "responseNode", "options": {}},
            webhookId="send-email", credentials=CALLER_CRED),
        node("Config", "n8n-nodes-base.set", 3.4, (220, 0), {
            "assignments": {"assignments": [
                {"id": "cfg-1", "name": "from", "value": "Your App <notifications@yourdomain.example>", "type": "string"},
                {"id": "cfg-2", "name": "reply_to", "value": "", "type": "string"},
                {"id": "cfg-3", "name": "suppression_lookup_url",
                 "value": "https://your-n8n.example.com/webhook/resend-suppression", "type": "string"},
                {"id": "cfg-4", "name": "resend_api_url", "value": "https://api.resend.com/emails", "type": "string"},
            ]},
            "includeOtherFields": True, "options": {}}),
        node("Build email and idempotency key", "n8n-nodes-base.code", 2, (440, 0), {
            "mode": "runOnceForEachItem", "language": "javaScript",
            "jsCode": code("""
const cfg = $('Config').item.json;
const result = buildEmail($('Send request').item.json.body || {}, { from: cfg.from, reply_to: cfg.reply_to });
return { json: result };
""")}),
        if_node("Input valid?", (660, 0), "={{ $json.ok }}"),
        respond("422 invalid input", (880, 200), 422, "={{ { ok: false, errors: $json.errors } }}"),
        node("Check suppression list", "n8n-nodes-base.httpRequest", 4.2, (880, -100), {
            "method": "GET", "url": "={{ $('Config').item.json.suppression_lookup_url }}",
            "authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth",
            "sendQuery": True,
            "queryParameters": {"parameters": [{"name": "email", "value": "={{ $json.to.join(',') }}"}]},
            "options": {"timeout": 10000}},
            credentials=CALLER_CRED, retryOnFail=True, maxTries=2, waitBetweenTries=1000,
            onError="continueRegularOutput", alwaysOutputData=True),
        node("Drop suppressed recipients", "n8n-nodes-base.code", 2, (1100, -100), {
            "mode": "runOnceForEachItem", "language": "javaScript",
            "jsCode": code("""
const built = $('Build email and idempotency key').item.json;
const lookup = $json || {};
// If the lookup failed we still send (transactional mail such as password
// resets matters more), but say so in the result. Resend also keeps its own
// account-level suppression list, which stays in force either way.
const suppressed = Array.isArray(lookup.suppressed) ? lookup.suppressed.map(normalise) : [];
const lookupOk = Array.isArray(lookup.suppressed);
const keep = built.payload.to.filter((addr) => !suppressed.includes(normalise(addr)));
return { json: { ...built, payload: { ...built.payload, to: keep }, skipped: suppressed,
                 suppressionLookupOk: lookupOk, allSuppressed: keep.length === 0 } };
""")}),
        if_node("All suppressed?", (1320, -100), "={{ $json.allSuppressed }}"),
        respond("200 suppressed", (1540, -250), 200,
                "={{ { ok: true, status: 'suppressed', skipped: $json.skipped } }}"),
        node("Resend: send email", "n8n-nodes-base.httpRequest", 4.2, (1540, 0), {
            "method": "POST", "url": "={{ $('Config').item.json.resend_api_url }}",
            "authentication": "genericCredentialType", "genericAuthType": "httpHeaderAuth",
            "sendHeaders": True,
            "headerParameters": {"parameters": [{"name": "Idempotency-Key", "value": "={{ $json.idempotencyKey }}"}]},
            "sendBody": True, "specifyBody": "json", "jsonBody": "={{ JSON.stringify($json.payload) }}",
            "options": {"timeout": 15000}},
            credentials=RESEND_CRED, retryOnFail=True, maxTries=4, waitBetweenTries=2000,
            onError="continueErrorOutput"),
        respond("200 sent", (1760, -50), 200,
                "={{ { ok: true, status: 'sent', id: $json.id, idempotency_key: $('Drop suppressed recipients').item.json.idempotencyKey, skipped: $('Drop suppressed recipients').item.json.skipped } }}"),
        respond("502 send failed", (1760, 150), 502,
                "={{ { ok: false, status: 'failed', error: $json.error } }}"),
    ]
    connections = connect(
        ("Send request", "Config", 0),
        ("Config", "Build email and idempotency key", 0),
        ("Build email and idempotency key", "Input valid?", 0),
        ("Input valid?", "Check suppression list", 0),
        ("Input valid?", "422 invalid input", 1),
        ("Check suppression list", "Drop suppressed recipients", 0),
        ("Drop suppressed recipients", "All suppressed?", 0),
        ("All suppressed?", "200 suppressed", 0),
        ("All suppressed?", "Resend: send email", 1),
        ("Resend: send email", "200 sent", 0),
        ("Resend: send email", "502 send failed", 1),
    )
    return {"name": "Resend: send transactional email", "nodes": nodes, "connections": connections,
            "settings": SETTINGS, "pinData": {}, "meta": {"templateCredsSetupCompleted": False}, "tags": []}


def events_workflow():
    nodes = [
        node("Resend events", "n8n-nodes-base.webhook", 2, (0, 0), {
            "httpMethod": "POST", "path": "resend-events", "responseMode": "responseNode",
            "options": {"rawBody": True}}, webhookId="resend-events"),
        node("Verify Svix signature", "n8n-nodes-base.code", 2, (220, 0), {
            "mode": "runOnceForEachItem", "language": "javaScript",
            "jsCode": code("""
// The raw body is needed: re-serialised JSON can differ from what was signed.
let raw;
if ($input.item.binary && $input.item.binary.data) {
  raw = await this.helpers.getBinaryDataBuffer($itemIndex, 'data');
} else {
  raw = JSON.stringify($json.body); // fallback; see README
}
const secret = $env.RESEND_WEBHOOK_SECRET;
return { json: verifySvix(raw, $json.headers, secret) };
""")}),
        if_node("Signature valid?", (440, 0), "={{ $json.valid }}"),
        node("Record event and suppress", "n8n-nodes-base.code", 2, (660, -100), {
            "mode": "runOnceForEachItem", "language": "javaScript",
            "jsCode": code("""
const store = $getWorkflowStaticData('global');
return { json: applyEvent(store, $json.svixId, $json.event) };
""")}),
        respond("200 OK", (880, -100), 200, "={{ { ok: true, ...$json } }}"),
        respond("400 invalid signature", (660, 150), 400, "={{ { ok: false, error: 'invalid signature' } }}"),
        node("Suppression lookup", "n8n-nodes-base.webhook", 2, (0, 400), {
            "httpMethod": "GET", "path": "resend-suppression", "authentication": "headerAuth",
            "responseMode": "responseNode", "options": {}},
            webhookId="resend-suppression", credentials=CALLER_CRED),
        node("Look up addresses", "n8n-nodes-base.code", 2, (220, 400), {
            "mode": "runOnceForEachItem", "language": "javaScript",
            "jsCode": code("""
const store = $getWorkflowStaticData('global');
const asked = String(($json.query || {}).email || '').split(',').map(normalise).filter(Boolean);
const suppressed = asked.filter((a) => store.suppressed && store.suppressed[a]);
return { json: { checked: asked.length, suppressed } };
""")}),
        respond("Lookup result", (440, 400), 200, "={{ $json }}"),
    ]
    connections = connect(
        ("Resend events", "Verify Svix signature", 0),
        ("Verify Svix signature", "Signature valid?", 0),
        ("Signature valid?", "Record event and suppress", 0),
        ("Signature valid?", "400 invalid signature", 1),
        ("Record event and suppress", "200 OK", 0),
        ("Suppression lookup", "Look up addresses", 0),
        ("Look up addresses", "Lookup result", 0),
    )
    return {"name": "Resend: webhook events and suppression list", "nodes": nodes,
            "connections": connections, "settings": SETTINGS, "pinData": {},
            "meta": {"templateCredsSetupCompleted": False}, "tags": []}


if __name__ == "__main__":
    for fname, wf in (("resend-send-email.json", send_workflow()), ("resend-events.json", events_workflow())):
        (ROOT / "n8n" / fname).write_text(json.dumps(wf, indent=2) + "\n", encoding="utf-8")
        print("wrote n8n/" + fname)
