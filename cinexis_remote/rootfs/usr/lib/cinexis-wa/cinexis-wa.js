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

const {
  default:                makeWASocket,
  useMultiFileAuthState,
  fetchLatestBaileysVersion,
  DisconnectReason,
  isJidGroup,
} = require('@whiskeysockets/baileys');

const QRCode  = require('qrcode');
const express = require('express');
const pino    = require('pino');
const fs      = require('fs');
const path    = require('path');

const AUTH_DIR = process.env.WA_AUTH_DIR || '/share/cinexis/wa-auth';
const PORT     = parseInt(process.env.WA_PORT || '18083', 10);

// Quiet by default — Baileys is chatty at info level.
const logger = pino({ level: process.env.WA_LOG_LEVEL || 'warn' });

let sock           = null;
let currentQR      = null;
let connectedPhone = null;
let connectedSince = null;
let reconnectAttempts = 0;

function asJid(to) {
  const s = String(to).trim();
  if (s.includes('@')) return s;                                 // already a JID
  return `${s.replace(/\D/g, '')}@s.whatsapp.net`;
}

async function start() {
  fs.mkdirSync(AUTH_DIR, { recursive: true });
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const { version }          = await fetchLatestBaileysVersion();

  sock = makeWASocket({
    version,
    auth:                state,
    logger,
    printQRInTerminal:   false,
    syncFullHistory:     false,
    markOnlineOnConnect: false,
    browser:             ['Cinexis HA Addon', 'Chrome', '1.11.0'],
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
      setTimeout(start, backoff);
    }
  });
}

// ── HTTP control surface ─────────────────────────────────────────────────────
const app = express();
app.use(express.json({ limit: '2mb' }));

app.get('/status', (_req, res) => {
  res.json({
    connected:        !!connectedPhone,
    phone:            connectedPhone,
    since:            connectedSince,
    has_qr:           !!currentQR,
    auth_dir_exists:  fs.existsSync(AUTH_DIR),
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

app.post('/send/text', async (req, res) => {
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

app.post('/send/image', async (req, res) => {
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

app.post('/logout', async (_req, res) => {
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
app.post('/test', async (req, res) => {
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
const RECIPIENTS_FILE     = process.env.RECIPIENTS_FILE     || '/share/cinexis/recipients.json';
const AUTOMATION_MAP_FILE = process.env.AUTOMATION_MAP_FILE || '/share/cinexis/automation_recipient_map.json';
const TELEGRAM_CONFIG     = process.env.TELEGRAM_CONFIG_FILE || '/share/cinexis/telegram_config.json';

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

app.post('/notify', async (req, res) => {
  const body = req.body || {};
  const message    = String(body.message || '').slice(0, 4096);
  const automation = body.automation_id || null;
  const recipients = resolveRecipients(body.to, automation);
  if (recipients.length === 0) {
    return res.status(400).json({ ok: false, error: 'no_recipients_resolved', hint: 'Pass `to: "Dad"` or `to: ["Dad","Mom"]` or `to: "all"`. Use recipient names defined in the addon UI.' });
  }
  if (!message && !body.image_url && !body.image_entity && !body.video_url && !body.document_url) {
    return res.status(400).json({ ok: false, error: 'message_or_media_required' });
  }

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

start().catch(err => {
  console.error('[CINEXIS-WA] startup error:', err);
  process.exit(1);
});

app.listen(PORT, '127.0.0.1', () => {
  console.log(`[CINEXIS-WA] HTTP service listening on 127.0.0.1:${PORT}`);
});

process.on('SIGTERM', () => { console.log('[CINEXIS-WA] SIGTERM, exiting'); process.exit(0); });
process.on('SIGINT',  () => { console.log('[CINEXIS-WA] SIGINT, exiting');  process.exit(0); });
