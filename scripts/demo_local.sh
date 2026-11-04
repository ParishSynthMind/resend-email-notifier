#!/bin/bash
# End-to-end run on this machine with no Resend account:
#   a local fake Resend API (scripts/fake_resend.py) + the real webhook receiver,
#   driven through the real CLI, with curl for the signed webhooks.
# Ports: FAKE_PORT (default 8846) and HOOK_PORT (default 8847).
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python}
FAKE_PORT=${FAKE_PORT:-8846}
HOOK_PORT=${HOOK_PORT:-8847}
WORK=$(mktemp -d)
trap 'kill $FAKE_PID $HOOK_PID 2>/dev/null || true; wait 2>/dev/null || true; rm -rf "$WORK"' EXIT
export PYTHONWARNINGS="ignore:urllib3 v2 only supports OpenSSL"

# Throwaway values for this run only. Not real credentials.
export RESEND_API_KEY="re_demo_local_only"
export RESEND_WEBHOOK_SECRET="whsec_$($PY -c 'import base64,os;print(base64.b64encode(os.urandom(24)).decode())')"
export RESEND_BASE_URL="http://127.0.0.1:$FAKE_PORT"
export RESEND_FROM="Shop <orders@shop.example.com>"
export SUPPRESSION_DB="$WORK/suppressions.db"

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

$PY scripts/fake_resend.py --port "$FAKE_PORT" --fail-first 2 --log "$WORK/fake.jsonl" & FAKE_PID=$!
$PY -m resend_notifier serve --host 127.0.0.1 --port "$HOOK_PORT" 2>"$WORK/hook.log" & HOOK_PID=$!
for i in $(seq 1 50); do
  curl -s "http://127.0.0.1:$HOOK_PORT/health" >/dev/null 2>&1 && break; sleep 0.1
done

step "1. Send order confirmation (fake API answers 429 Retry-After:1, then 503, then OK)"
$PY -m resend_notifier send --event-id order-A-1001 --template order_confirmation \
    --to sam@example.com --context samples/order.json 2>&1 | sed 's/^[0-9-]* [0-9:,]* //'

step "2. Same event sent again (job re-run): same Idempotency-Key, no second email"
$PY -m resend_notifier send --event-id order-A-1001 --template order_confirmation \
    --to sam@example.com --context samples/order.json 2>/dev/null

URL="http://127.0.0.1:$HOOK_PORT/webhooks/resend"
post() { # $1 = body file, rest = extra sign args
  local body=$1; shift
  local h; h=$($PY scripts/sign_webhook.py "$body" "$@")
  curl -s -w '  -> HTTP %{http_code}\n' -X POST "$URL" -H 'Content-Type: application/json' \
       -H "$(sed -n 1p <<<"$h")" -H "$(sed -n 2p <<<"$h")" -H "$(sed -n 3p <<<"$h")" \
       --data-binary @"$body"
}

step "3. Correctly signed email.bounced webhook for bounce-me@example.com"
post samples/email.bounced.json

step "4. Tampered body (recipient changed after signing)"
sed 's/bounce-me@/someone-else@/' samples/email.bounced.json > "$WORK/tampered.json"
h=$($PY scripts/sign_webhook.py samples/email.bounced.json)
curl -s -w '  -> HTTP %{http_code}\n' -X POST "$URL" -H "$(sed -n 1p <<<"$h")" -H "$(sed -n 2p <<<"$h")" \
     -H "$(sed -n 3p <<<"$h")" --data-binary @"$WORK/tampered.json"

step "5. Signed with the wrong secret"
WRONG="whsec_$($PY -c 'import base64,os;print(base64.b64encode(os.urandom(24)).decode())')" \
  post samples/email.bounced.json --secret-env WRONG

step "6. Correct signature but timestamp 10 minutes old (replay)"
post samples/email.bounced.json --age 600

step "7. Try to email the bounced address: skipped, no API call"
$PY -m resend_notifier send --event-id order-A-1002 --template order_confirmation \
    --to bounce-me@example.com --context samples/order.json 2>/dev/null

step "8. Batch send (2 password resets in one POST /emails/batch)"
$PY -m resend_notifier batch samples/batch.json 2>/dev/null

step "Suppression list"
$PY -m resend_notifier suppress list
step "What the fake Resend API received"
cat "$WORK/fake.jsonl"
step "Webhook receiver log"
sed 's/^[0-9-]* [0-9:,]* //' "$WORK/hook.log"
