#!/bin/sh
# Run any command with the Resend secrets loaded from the macOS Keychain.
# The values go straight into the child process environment; nothing is printed or written.
#   scripts/with_keychain.sh python -m resend_notifier send ...
#   scripts/with_keychain.sh python -m resend_notifier serve --port 8000
# Keychain items (create them once, see README):
#   resend-api-key          the API key (re_...), needed to send
#   resend-webhook-secret   the webhook signing secret (whsec_...), needed for "serve"
set -eu
if key="$(security find-generic-password -s resend-api-key -w 2>/dev/null)"; then
  RESEND_API_KEY="$key"; export RESEND_API_KEY
else
  echo "note: Keychain item 'resend-api-key' not found; sending will fail." >&2
  echo "      add it with: security add-generic-password -s resend-api-key -a \"\$USER\" -w" >&2
fi
if secret="$(security find-generic-password -s resend-webhook-secret -w 2>/dev/null)"; then
  RESEND_WEBHOOK_SECRET="$secret"; export RESEND_WEBHOOK_SECRET
fi
unset key secret 2>/dev/null || true
exec "$@"
