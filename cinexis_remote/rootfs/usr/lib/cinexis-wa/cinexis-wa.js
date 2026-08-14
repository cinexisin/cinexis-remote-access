/**
 * Cinexis WA — local WhatsApp Web service for the HA addon.
 *
 * The addon owner scans a QR with their personal WhatsApp once. From then on
 * this service can send messages from their number to anyone they configure.
 * Auth state persists to /share/cinexis/wa-auth so the session survives addon
 * restarts and updates.
 *
 * HTTP API (listens on 127.0.0.1:18083 — only the addon's Python ingress
 * talks to it, never exposed outside the container):
 *
 *   GET  /status            { connected, phone, since, has_qr }
 *   GET  /qr                { qr_data_url }    — PNG data URL of the pairing QR
 *   POST /send/text         { to, text }       → sends a text message
 *   POST /send/image        { to, image_url, caption? } → sends an image with optional caption
 *   POST /logout            wipe auth, force re-pairing
 *
 * `to` may be either a raw E.164 number (`919999999999`) or a full JID
 * (`919999999999@s.whatsapp.net`). Group JIDs (`xxxx@g.us`) work too.
 */

'use strict';

// Baileys is ESM-only in recent releases, so it CANNOT be require()'d from
// this CommonJS file — doing so throws ERR_REQUIRE_ESM at module load and the
// whole WhatsApp service dies before it starts. Load it via dynamic import()
// at boot instead: that works for both ESM and CJS builds and is version-proof.
let makeWASocket, useMultiFileAuthState, fetchLatestBaileysVersion, DisconnectReason;
let _baileysLoaded = false;
async function loadBaileys() {
  if (_baileysLoaded) return;
  const b = await import('@whiskeysockets/baileys');
  // Handle both shapes: ESM (default = makeWASocket, named exports on the
  // namespace) and CJS-interop (everything under .default).
  const lib = (b && b.default && (b.default.useMultiFileAuthState || b.default.makeWASocket)) ? b.default : b;
  makeWASocket              = (typeof b.default === 'function') ? b.default : (lib.makeWASocket || b.makeWASocket);
  useMultiFileAuthState     = lib.useMultiFileAuthState     || b.useMultiFileAuthState;
  fetchLatestBaileysVersion = lib.fetchLatestBaileysVersion || b.fetchLatestBaileysVersion;
  DisconnectReason          = lib.DisconnectReason          || b.DisconnectReason;
  if (typeof makeWASocket !== 'function') throw new Error('baileys loaded but makeWASocket not found');
  _baileysLoaded = true;
  console.log('[CINEXIS-WA] Baileys loaded via dynamic import');
}

const QRCode  = require('qrcode');
const express = require('express');
const pino    = require('pino');
const fs      = require('fs');
const path    = require('path');

const AUTH_DIR = process.env.WA_AUTH_DIR || '/data/cinexis/wa-auth';
const PORT     = parseInt(process.env.WA_PORT || '18083', 10);

// Quiet by default — Baileys is chatty at info level.
const logger = pino({ level: process.env.WA_LOG_LEVEL || 'warn' });

let sock           = null;
let currentQR      = null;
let connectedPhone = null;
let connectedSince = null;
let reconnectAttempts = 0;
let lastError      = null;   // surfaced via /status for diagnostics
let everConnected  = false;  // once true, NEVER wipe auth on boot failures

const SHARED_SECRET = process.env.WA_SHARED_SECRET || '';

// /notify abuse guards — protect the owner's personal WhatsApp number from a
// runaway HA automation loop (a flapping sensor firing notify hundreds of
// times would otherwise get their number flagged/banned by WhatsApp).
let notifyWindow = [];                       // recent /notify timestamps (ms)
const notifyRecipientLast = new Map();       // channel:addr -> last send ms
const NOTIFY_MAX_PER_MIN = 30;               // global cap across all calls
const NOTIFY_MAX_FANOUT  = 50;               // max recipients per single call
const NOTIFY_RECIPIENT_COOLDOWN_MS = 10_000; // min gap to one recipient

// Fallback WA web version if fetchLatestBaileysVersion() can't reach the net.
// Keeps the QR working during a transient DNS/connectivity blip at boot.
const FALLBACK_WA_VERSION = [2, 3000, 1015901307];

function asJid(to) {
  const s = String(to).trim();
  if (s.includes('@')) return s;                                 // already a JID
  return `${s.replace(/\D/g, '')}@s.whatsapp.net`;
}

async function start() {
  await loadBaileys();   // dynamic import — must run before any baileys symbol is used
  fs.mkdirSync(AUTH_DIR, { recursive: true });
  try { fs.chmodSync(AUTH_DIR, 0o700); } catch (_) {}
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);

  // Don't let a network blip fetching the WA version kill the boot — fall
  // back to a known-good pinned version so the QR still generates.
  let version = FALLBACK_WA_VERSION;
  try {
    const fetched = await fetchLatestBaileysVersion();
    if (fetched && fetched.version) version = fetched.version;
  } catch (e) {
    console.warn('[CINEXIS-WA] fetchLatestBaileysVersion failed, using pinned fallback:', e.message);
  }

  sock = makeWASocket({
    version,
    auth:                state,
    logger,
    printQRInTerminal:   false,
    syncFullHistory:     false,
    markOnlineOnConnect: false,
    browser:             ['Cinexis HA Addon', 'Chrome', '1.16.0'],
  });

  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', async (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      try {
        currentQR = await QRCode.toDataURL(qr, { width: 320, margin: 1 });
        console.log('[CINEXIS-WA] new QR ready — scan via the addon UI');
      } catch (e) {
        console.error('[CINEXIS-WA] QR render failed:', e.message);
      }
    }

    if (connection === 'open') {
      reconnectAttempts = 0;
      everConnected = true;   // we have a real pairing now — never auto-wipe it
      currentQR = null;
      const u = sock.user?.id || '';
      connectedPhone = u.split('@')[0]?.split(':')[0] || null;
      connectedSince = Date.now();
      console.log(`[CINEXIS-WA] connected as +${connectedPhone}`);
    }

    if (connection === 'close') {
      const code = lastDisconnect?.error?.output?.statusCode;
      const isLoggedOut = code === DisconnectReason.loggedOut;
      const isReplaced  = code === DisconnectReason.connectionReplaced;
      connectedPhone = null;
      connectedSince = null;
      if (isLoggedOut) {
        console.warn('[CINEXIS-WA] session logged out remotely — wiping auth so a fresh QR can be scanned');
        try { fs.rmSync(AUTH_DIR, { recursive: true, force: true }); } catch (_) {}
      } else if (isReplaced) {
        console.warn('[CINEXIS-WA] another client took over the session — not reconnecting');
        return;
      }
      reconnectAttempts++;
      const backoff = Math.min(60_000, 1500 * 2 ** Math.min(reconnectAttempts, 5));
      console.warn(`[CINEXIS-WA] disconnected (code ${code}); retry in ${backoff / 1000}s`);
      // start() is async and can throw (network blip fetching version); an
      // uncaught rejection here would silently kill the process. Catch it
      // and re-schedule so reconnect always self-heals.
      setTimeout(() => { start().catch(e => {
        lastError = e && e.message ? e.message : String(e);
        console.error('[CINEXIS-WA] reconnect start() threw:', lastError, '— retrying in 30s');
        setTimeout(() => start().catch(() => {}), 30000);
      }); }, backoff);
    }
  });
}

// ── HTTP control surface ─────────────────────────────────────────────────────
const app = express();
app.use(express.json({ limit: '2mb' }));

// Shared-secret guard for mutating / sending endpoints. We bind 0.0.0.0 so
// HA Core can reach /notify, which means the port is reachable from the HA
// host network — protect the dangerous verbs. Reads come through unguarded
// (status/qr only expose pairing state, no send capability). The ingress
// proxy and HA rest_command attach the secret via x-cinexis-secret or
// ?secret=. If WA_SHARED_SECRET is unset (older entrypoint), guard is a
// no-op so we don't break upgrades.
function requireSecret(req, res, next) {
  if (!SHARED_SECRET) return next();
  const provided = req.headers['x-cinexis-secret'] || req.query.secret || (req.body && req.body.secret);
  if (provided && String(provided) === SHARED_SECRET) return next();
  return res.status(401).json({ error: 'unauthorized' });
}

app.get('/status', (_req, res) => {
  res.json({
    connected:        !!connectedPhone,
    phone:            connectedPhone,
    since:            connectedSince,
    has_qr:           !!currentQR,
    auth_dir_exists:  fs.existsSync(AUTH_DIR),
    last_error:       lastError,
    reconnect_attempts: reconnectAttempts,
  });
});

app.get('/qr', (_req, res) => {
  if (!currentQR) {
    return res.status(404).json({
      error: 'no_qr_available',
      hint:  connectedPhone ? 'already_connected' : 'wait_a_few_seconds_then_retry',
    });
  }
  res.json({ qr_data_url: currentQR });
});

app.post('/send/text', requireSecret, async (req, res) => {
  if (!connectedPhone) return res.status(503).json({ error: 'not_connected' });
  const { to, text } = req.body || {};
  if (!to || !text) return res.status(400).json({ error: 'to_and_text_required' });
  try {
    const jid    = asJid(to);
    const result = await sock.sendMessage(jid, { text: String(text) });
    res.json({ ok: true, jid, message_id: result?.key?.id });
  } catch (e) {
    console.error('[CINEXIS-WA] send/text failed:', e.message);
    res.status(500).json({ error: e.message });
  }
});

app.post('/send/image', requireSecret, async (req, res) => {
  if (!connectedPhone) return res.status(503).json({ error: 'not_connected' });
  const { to, image_url, caption } = req.body || {};
  if (!to || !image_url) return res.status(400).json({ error: 'to_and_image_url_required' });
  try {
    const jid    = asJid(to);
    const result = await sock.sendMessage(jid, {
      image:   { url: image_url },
      caption: caption ? String(caption) : undefined,
    });
    res.json({ ok: true, jid, message_id: result?.key?.id });
  } catch (e) {
    console.error('[CINEXIS-WA] send/image failed:', e.message);
    res.status(500).json({ error: e.message });
  }
});

app.post('/logout', requireSecret, async (_req, res) => {
  try {
    if (sock) await sock.logout().catch(() => {});
  } finally {
    try { fs.rmSync(AUTH_DIR, { recursive: true, force: true }); } catch (_) {}
    connectedPhone = null;
    connectedSince = null;
    currentQR      = null;
    setTimeout(start, 1000);
    res.json({ ok: true });
  }
});

// Convenience for the ingress UI's "Send test" button
app.post('/test', requireSecret, async (req, res) => {
  if (!connectedPhone) return res.status(503).json({ error: 'not_connected' });
  const to   = (req.body?.to || connectedPhone);
  const text = req.body?.text || `🧪 Cinexis test message at ${new Date().toLocaleString('en-IN')} — your addon's WhatsApp is wired up correctly.`;
  try {
    const jid = asJid(to);
    await sock.sendMessage(jid, { text });
    res.json({ ok: true, sent_to: to });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ── Unified /notify endpoint (the one HA automations call) ───────────────────
// POST /notify
// Body shape:
//   {
//     to:          "Dad" | ["Dad","Mom"] | "all" | ["all_whatsapp"] | ["all_telegram"]
//                  - resolves recipient names from /share/cinexis/recipients.json
//                  - "all" means every enabled recipient on any channel
//                  - "all_whatsapp" / "all_telegram" filters by channel
//                  - bare phone numbers / chat_ids are also accepted as a fallback
//     message:     "Front door opened at 2am"   (required for text+caption)
//     image_url:   "https://..."        (optional — http(s) URL to a static image)
//     image_entity:"camera.front_door"  (optional — addon snapshots this camera via HA Supervisor)
//     video_url:   "https://..."        (optional)
//     document_url:"https://..."        (optional)
//     document_name:"invoice.pdf"       (optional, for documents)
//     automation_id:"automation.front_door_at_night"  (optional — if `to` is missing,
//                  per-automation defaults are read from automation_recipient_map.json)
//   }
// Returns: { ok: true, sent: [{recipient, channel, ok, message_id?}], failed: [...] }
const RECIPIENTS_FILE     = process.env.RECIPIENTS_FILE     || '/data/cinexis/recipients.json';
const AUTOMATION_MAP_FILE = process.env.AUTOMATION_MAP_FILE || '/data/cinexis/automation_recipient_map.json';
const TELEGRAM_CONFIG     = process.env.TELEGRAM_CONFIG_FILE || '/data/cinexis/telegram_config.json';

function loadJsonSafe(file, fallback) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch (_) { return fallback; }
}

async function snapshotHaCamera(entityId) {
  // The Supervisor token is automatically injected by HA when the addon
  // has `homeassistant_api: true`. We hit /core/api/camera_proxy/<entity>
  // which returns a JPEG.
  const token = process.env.SUPERVISOR_TOKEN;
  if (!token || !entityId) return null;
  const url = `http://supervisor/core/api/camera_proxy/${encodeURIComponent(entityId)}`;
  try {
    const r = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
    if (!r.ok) { console.warn(`[CINEXIS-WA] camera_proxy ${entityId} → ${r.status}`); return null; }
    return Buffer.from(await r.arrayBuffer());
  } catch (e) { console.warn(`[CINEXIS-WA] camera snapshot failed:`, e.message); return null; }
}

function resolveRecipients(toArg, automationId) {
  const all = loadJsonSafe(RECIPIENTS_FILE, []);
  const enabled = all.filter(r => r.enabled !== false);

  // If `to` is empty, fall back to the per-automation default mapping.
  let names = toArg;
  if ((!names || (Array.isArray(names) && names.length === 0)) && automationId) {
    const m = loadJsonSafe(AUTOMATION_MAP_FILE, {});
    names = m[automationId];
  }
  if (typeof names === 'string') names = [names];
  if (!Array.isArray(names) || names.length === 0) return [];

  const out = [];
  const seen = new Set();
  for (const n of names.map(x => String(x).trim()).filter(Boolean)) {
    // Special tokens
    if (n === 'all') {
      for (const r of enabled) {
        if (!seen.has(r.id)) { out.push(r); seen.add(r.id); }
      }
      continue;
    }
    if (n === 'all_whatsapp') {
      for (const r of enabled.filter(x => x.channel === 'whatsapp')) {
        if (!seen.has(r.id)) { out.push(r); seen.add(r.id); }
      }
      continue;
    }
    if (n === 'all_telegram') {
      for (const r of enabled.filter(x => x.channel === 'telegram')) {
        if (!seen.has(r.id)) { out.push(r); seen.add(r.id); }
      }
      continue;
    }
    // Match by recipient name (case-insensitive)
    const matched = enabled.find(r => (r.name || '').toLowerCase() === n.toLowerCase());
    if (matched) {
      if (!seen.has(matched.id)) { out.push(matched); seen.add(matched.id); }
      continue;
    }
    // Fallback: treat as raw address. WA = digits only, Telegram = optional minus
    if (/^[-]?\d{6,}$/.test(n.replace(/\D/g, n.includes('-') ? '' : ''))) {
      // Default channel guess: starts with '-' → Telegram, else WhatsApp
      const channel = n.startsWith('-') ? 'telegram' : 'whatsapp';
      const synthetic = { id: 'inline_' + n, name: n, channel, address: n, enabled: true };
      if (!seen.has(synthetic.id)) { out.push(synthetic); seen.add(synthetic.id); }
    }
  }
  return out;
}

async function tgSend(chatId, text, media) {
  const cfg = loadJsonSafe(TELEGRAM_CONFIG, {});
  if (!cfg.bot_token) return { ok: false, error: 'no_telegram_token' };
  let method = 'sendMessage';
  let payload = { chat_id: chatId, text };
  if (media && media.kind === 'image' && media.url) {
    method = 'sendPhoto';
    payload = { chat_id: chatId, photo: media.url, caption: text };
  } else if (media && media.kind === 'image' && media.buffer) {
    // Telegram needs multipart for in-memory image. Use native FormData
    // + Blob from the buffer (Node 18+; baileys requires 20 anyway).
    try {
      const fd = new FormData();
      fd.append('chat_id', String(chatId));
      if (text) fd.append('caption', text);
      fd.append('photo', new Blob([media.buffer], { type: 'image/jpeg' }), 'snapshot.jpg');
      const r = await fetch(`https://api.telegram.org/bot${cfg.bot_token}/sendPhoto`, {
        method: 'POST', body: fd,
      });
      const d = await r.json();
      return { ok: !!d.ok, error: d.description, message_id: d.result?.message_id };
    } catch (e) { return { ok: false, error: e.message }; }
  } else if (media && media.kind === 'video' && media.url) {
    method = 'sendVideo';
    payload = { chat_id: chatId, video: media.url, caption: text };
  } else if (media && media.kind === 'document' && media.url) {
    method = 'sendDocument';
    payload = { chat_id: chatId, document: media.url, caption: text };
  }
  try {
    const r = await fetch(`https://api.telegram.org/bot${cfg.bot_token}/${method}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    return { ok: !!d.ok, error: d.description, message_id: d.result?.message_id };
  } catch (e) { return { ok: false, error: e.message }; }
}

app.post('/notify', requireSecret, async (req, res) => {
  const body = req.body || {};
  const message    = String(body.message || '').slice(0, 4096);
  const automation = body.automation_id || null;
  let recipients = resolveRecipients(body.to, automation);
  if (recipients.length === 0) {
    return res.status(400).json({ ok: false, error: 'no_recipients_resolved', hint: 'Pass `to: "Dad"` or `to: ["Dad","Mom"]` or `to: "all"`. Use recipient names defined in the addon UI.' });
  }
  if (!message && !body.image_url && !body.image_entity && !body.video_url && !body.document_url) {
    return res.status(400).json({ ok: false, error: 'message_or_media_required' });
  }

  // ── Abuse guards (protect the owner's own WhatsApp number from a runaway
  // automation loop that would get their personal number banned) ──
  const nowMs = Date.now();
  // 1. Global per-minute cap across all /notify calls
  notifyWindow = notifyWindow.filter(t => t > nowMs - 60_000);
  if (notifyWindow.length >= NOTIFY_MAX_PER_MIN) {
    console.error(`[CINEXIS-WA] /notify rate cap hit (${notifyWindow.length}/min) — refusing to protect your WhatsApp number from a send loop`);
    return res.status(429).json({ ok: false, error: 'rate_limited', hint: 'Too many notifications in the last minute — likely an automation loop. Sends paused briefly.' });
  }
  // 2. Fan-out cap per single call
  if (recipients.length > NOTIFY_MAX_FANOUT) {
    console.warn(`[CINEXIS-WA] /notify fan-out ${recipients.length} capped to ${NOTIFY_MAX_FANOUT}`);
    recipients = recipients.slice(0, NOTIFY_MAX_FANOUT);
  }
  // 3. Per-recipient cooldown — drop dupes hammering the same person
  recipients = recipients.filter(r => {
    const key = (r.channel || 'wa') + ':' + r.address;
    const last = notifyRecipientLast.get(key);
    if (last && (nowMs - last) < NOTIFY_RECIPIENT_COOLDOWN_MS) return false;
    notifyRecipientLast.set(key, nowMs);
    return true;
  });
  if (recipients.length === 0) {
    return res.status(429).json({ ok: false, error: 'recipient_cooldown', hint: 'All targeted recipients were messaged in the last few seconds — skipped to avoid spamming them.' });
  }
  notifyWindow.push(nowMs);

  // Resolve any media payload up front so we don't snapshot HA's camera N times.
  let media = null;
  if (body.image_entity) {
    const buf = await snapshotHaCamera(body.image_entity);
    if (buf) media = { kind: 'image', buffer: buf, url: null };
  } else if (body.image_url) {
    media = { kind: 'image', url: body.image_url };
  } else if (body.video_url) {
    media = { kind: 'video', url: body.video_url };
  } else if (body.document_url) {
    media = { kind: 'document', url: body.document_url, filename: body.document_name };
  }

  const sent = [];
  const failed = [];
  for (const r of recipients) {
    try {
      if (r.channel === 'telegram') {
        const result = await tgSend(r.address, message || '', media);
        if (result.ok) sent.push({ recipient: r.name, channel: 'telegram', message_id: result.message_id });
        else           failed.push({ recipient: r.name, channel: 'telegram', error: result.error });
      } else {
        // WhatsApp via Baileys
        if (!connectedPhone) { failed.push({ recipient: r.name, channel: 'whatsapp', error: 'wa_not_connected' }); continue; }
        const jid = asJid(r.address);
        let result;
        if (media && media.kind === 'image' && media.buffer) {
          result = await sock.sendMessage(jid, { image: media.buffer, caption: message || undefined });
        } else if (media && media.kind === 'image' && media.url) {
          result = await sock.sendMessage(jid, { image: { url: media.url }, caption: message || undefined });
        } else if (media && media.kind === 'video' && media.url) {
          result = await sock.sendMessage(jid, { video: { url: media.url }, caption: message || undefined });
        } else if (media && media.kind === 'document' && media.url) {
          result = await sock.sendMessage(jid, { document: { url: media.url }, fileName: media.filename || 'document', caption: message || undefined });
        } else {
          result = await sock.sendMessage(jid, { text: message });
        }
        sent.push({ recipient: r.name, channel: 'whatsapp', message_id: result?.key?.id });
      }
    } catch (e) {
      failed.push({ recipient: r.name, channel: r.channel, error: e.message });
    }
  }
  res.json({ ok: true, sent, failed, total: recipients.length });
});

// Boot the Baileys socket with retry. A network failure inside start()
// (e.g. fetchLatestBaileysVersion) used to throw → process.exit(1) → the
// bash entrypoint never restarted it → QR dead forever. Now we retry the
// boot itself with backoff so a transient blip self-heals; the bash
// watchdog is the outer safety net.
let bootAttempts = 0;
function bootWA() {
  start().catch(err => {
    bootAttempts++;
    lastError = err && err.message ? err.message : String(err);
    // Corrupt-auth recovery: if start() keeps failing AND we've never had a
    // successful connection, the cause may be a half-written auth state in
    // /share/cinexis/wa-auth. After 3 failures, wipe it so the next boot starts
    // fresh. Guarded by !everConnected so we NEVER un-pair a healthy session
    // that's just hitting a transient (network) error.
    if (bootAttempts === 3 && !everConnected) {
      try { fs.rmSync(AUTH_DIR, { recursive: true, force: true }); console.warn('[CINEXIS-WA] 3 boot failures, never connected — wiped possibly-corrupt auth state; a fresh QR will appear.'); }
      catch (_) {}
    }
    const backoff = Math.min(60000, 2000 * 2 ** Math.min(bootAttempts, 5));
    console.error(`[CINEXIS-WA] startup error (attempt ${bootAttempts}): ${lastError} — retrying in ${backoff/1000}s`);
    setTimeout(bootWA, backoff);
  });
}
bootWA();

// Bind 0.0.0.0 so HA Core can reach the /notify endpoint over the addon's
// published port (rest_command.cinexis_notify). The mutating endpoints are
// guarded by WA_SHARED_SECRET (see requireSecret middleware) so opening the
// bind doesn't create an unauthenticated send surface.
app.listen(PORT, '0.0.0.0', () => {
  console.log(`[CINEXIS-WA] HTTP service listening on 0.0.0.0:${PORT}`);
});

// Crash guards — log + exit predictably; the bash watchdog respawns us.
process.on('unhandledRejection', (reason) => {
  console.error('[CINEXIS-WA] unhandledRejection:', reason && reason.message ? reason.message : reason);
});
process.on('uncaughtException', (err) => {
  console.error('[CINEXIS-WA] uncaughtException:', err && err.message ? err.message : err);
  // Don't exit on every uncaught — many Baileys stream errors are recoverable
  // via the reconnect path. Only exit on truly fatal ones.
  if (err && /EADDRINUSE|EACCES/.test(String(err.code || err.message))) process.exit(1);
});
process.on('SIGTERM', () => { console.log('[CINEXIS-WA] SIGTERM, exiting'); process.exit(0); });
process.on('SIGINT',  () => { console.log('[CINEXIS-WA] SIGINT, exiting');  process.exit(0); });
