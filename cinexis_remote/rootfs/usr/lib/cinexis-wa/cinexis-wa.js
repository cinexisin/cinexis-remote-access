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

start().catch(err => {
  console.error('[CINEXIS-WA] startup error:', err);
  process.exit(1);
});

app.listen(PORT, '127.0.0.1', () => {
  console.log(`[CINEXIS-WA] HTTP service listening on 127.0.0.1:${PORT}`);
});

process.on('SIGTERM', () => { console.log('[CINEXIS-WA] SIGTERM, exiting'); process.exit(0); });
process.on('SIGINT',  () => { console.log('[CINEXIS-WA] SIGINT, exiting');  process.exit(0); });
