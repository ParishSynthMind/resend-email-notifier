// Pure helpers shared by the n8n Code nodes. No n8n globals, no network.
// The build script (scripts/build_n8n.py) pastes this file into each Code
// node, followed by that node's glue code. The tests run it with Node.

const crypto = require('crypto');

const TOLERANCE_SECONDS = 5 * 60;
const SUPPRESS_ON = {
  'email.bounced': 'bounced',
  'email.complained': 'complained',
  'email.suppressed': 'suppressed_by_resend',
};

// Same scheme as the Python package: "<scope>/<sha256(scope \x1f event_id)>".
// So the Python sender and the n8n sender agree on the key for one event.
function idempotencyKey(eventId, scope) {
  scope = scope || 'email';
  if (eventId === undefined || eventId === null || !String(eventId).trim()) {
    throw new Error('event_id is required for an idempotency key');
  }
  const digest = crypto.createHash('sha256').update(`${scope}\x1f${eventId}`, 'utf8').digest('hex');
  const safeScope = scope.replace(/[^A-Za-z0-9_.-]+/g, '-').slice(0, 64) || 'email';
  return `${safeScope}/${digest}`;
}

function normalise(address) {
  let a = String(address || '').trim();
  const lt = a.lastIndexOf('<');
  if (lt !== -1 && a.endsWith('>')) a = a.slice(lt + 1, -1);
  return a.trim().toLowerCase();
}

// Validate an incoming send request and build the Resend payload.
function buildEmail(input, defaults) {
  const errors = [];
  const to = Array.isArray(input.to) ? input.to : input.to ? [input.to] : [];
  if (!input.event_id) errors.push('event_id is required');
  if (!to.length) errors.push('to is required');
  if (to.length > 50) errors.push('to has more than 50 recipients');
  if (!input.subject) errors.push('subject is required');
  if (!input.html && !input.text) errors.push('html or text is required');
  const from = input.from || defaults.from;
  if (!from) errors.push('from is required (set it in Config)');
  if (errors.length) return { ok: false, errors };
  const payload = {
    from,
    to,
    subject: String(input.subject).replace(/\s+/g, ' ').trim(),
  };
  if (input.html) payload.html = input.html;
  if (input.text) payload.text = input.text;
  if (input.reply_to || defaults.reply_to) payload.reply_to = input.reply_to || defaults.reply_to;
  if (input.tags && typeof input.tags === 'object') {
    payload.tags = Object.entries(input.tags).map(([name, value]) => ({ name, value: String(value) }));
  }
  const scope = input.scope || 'n8n-email';
  return { ok: true, payload, idempotencyKey: idempotencyKey(input.event_id, scope), to: to.map(normalise) };
}

function getHeader(headers, name) {
  const lower = {};
  for (const [k, v] of Object.entries(headers || {})) lower[k.toLowerCase()] = v;
  return lower[`svix-${name}`] || lower[`webhook-${name}`];
}

// Svix verification, as documented for Resend webhooks.
// rawBody must be the exact bytes received (a Buffer or a string).
function verifySvix(rawBody, headers, secret, nowSeconds) {
  const fail = (reason) => ({ valid: false, reason });
  const id = getHeader(headers, 'id');
  const ts = getHeader(headers, 'timestamp');
  const sigs = getHeader(headers, 'signature');
  if (!id || !ts || !sigs) return fail('missing svix headers');
  if (!/^\d+$/.test(String(ts))) return fail('bad timestamp');
  const now = nowSeconds === undefined ? Math.floor(Date.now() / 1000) : nowSeconds;
  if (Math.abs(now - Number(ts)) > TOLERANCE_SECONDS) return fail('timestamp outside the allowed window');
  if (!secret) return fail('webhook secret is not configured');
  const secretBytes = Buffer.from(secret.startsWith('whsec_') ? secret.slice(6) : secret, 'base64');
  const body = Buffer.isBuffer(rawBody) ? rawBody : Buffer.from(String(rawBody), 'utf8');
  const signed = Buffer.concat([Buffer.from(`${id}.${ts}.`, 'utf8'), body]);
  const expected = Buffer.from(crypto.createHmac('sha256', secretBytes).update(signed).digest('base64'));
  let matched = false;
  for (const entry of String(sigs).split(' ')) {
    const comma = entry.indexOf(',');
    if (comma === -1 || entry.slice(0, comma) !== 'v1') continue;
    const given = Buffer.from(entry.slice(comma + 1));
    if (given.length === expected.length && crypto.timingSafeEqual(given, expected)) {
      matched = true;
      break;
    }
  }
  if (!matched) return fail('no matching signature');
  let event;
  try {
    event = JSON.parse(body.toString('utf8'));
  } catch (e) {
    return fail('body is not valid JSON');
  }
  return { valid: true, svixId: id, event };
}

function suppressionReason(event) {
  const reason = SUPPRESS_ON[event && event.type];
  if (!reason) return null;
  if (event.type === 'email.bounced') {
    const bounce = (event.data && event.data.bounce) || {};
    if (String(bounce.type || '').toLowerCase() === 'transient') return null;
  }
  return reason;
}

// Apply a verified event to a store shaped like n8n static data:
// { suppressed: { email: {reason, svixId, at} }, seen: [svixId, ...] }
function applyEvent(store, svixId, event, nowIso) {
  store.suppressed = store.suppressed || {};
  store.seen = store.seen || [];
  if (store.seen.includes(svixId)) return { type: event.type, duplicate: true, suppressed: [] };
  store.seen.push(svixId);
  if (store.seen.length > 1000) store.seen.splice(0, store.seen.length - 1000);
  const added = [];
  const reason = suppressionReason(event);
  if (reason) {
    const to = (event.data && event.data.to) || [];
    for (const addr of Array.isArray(to) ? to : [to]) {
      const key = normalise(addr);
      if (key && !store.suppressed[key]) {
        store.suppressed[key] = { reason, svixId, at: nowIso || new Date().toISOString() };
        added.push(key);
      }
    }
  }
  return { type: event.type, duplicate: false, suppressed: added };
}

if (typeof module !== 'undefined') {
  module.exports = { idempotencyKey, normalise, buildEmail, verifySvix, suppressionReason, applyEvent };
}
