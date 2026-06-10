/**
 * Cinexis frps auth plugin — per-node tunnel-token validation.
 *
 * frps calls this HTTP service on Login + NewProxy operations (frp "manager"
 * server plugin protocol). It closes the shared-FRP-token squatting hole:
 * a leaked image token can no longer be used to claim another customer's
 * subdomain, because each addon now also sends a per-node HMAC token as frpc
 * metadata, and this plugin validates it + checks subdomain ownership.
 *
 * SAFETY MODEL — this is the only thing standing between frps and every live
 * tunnel, so it is built to FAIL OPEN:
 *   - MODE=observe (default): NEVER rejects. Logs what it would have done.
 *     Deploy in this mode first, watch the logs until every live client sends
 *     a valid token, THEN flip to enforce.
 *   - MODE=enforce: rejects invalid per-node tokens and subdomain squatting.
 *     Legacy clients with NO metadata are still allowed UNLESS REQUIRE_META=1
 *     (only set that once the whole fleet runs addon >= v1.18.0).
 *   - Any internal error (DB down, bad input, secret missing) → ALLOW + log.
 *     A bug here must never take down customer tunnels.
 *
 * Env:
 *   PORT                 (default 7501)              — localhost bind
 *   FRP_SIGNING_SECRET_FILE (default /opt/cinexis/data/frp_signing_secret)
 *   P2P_DB               (default /opt/cinexis/data/p2p.db)
 *   MODE                 observe | enforce           (default observe)
 *   REQUIRE_META         0 | 1                        (default 0)
 */
'use strict';

const http = require('http');
const fs = require('fs');
const crypto = require('crypto');

const PORT = parseInt(process.env.PORT || '7501', 10);
const SECRET_FILE = process.env.FRP_SIGNING_SECRET_FILE || '/opt/cinexis/data/frp_signing_secret';
const P2P_DB = process.env.P2P_DB || '/opt/cinexis/data/p2p.db';
const MODE = (process.env.MODE || 'observe').toLowerCase();
const REQUIRE_META = process.env.REQUIRE_META === '1';

let SIGNING_SECRET = '';
try { SIGNING_SECRET = fs.readFileSync(SECRET_FILE, 'utf8').trim(); }
catch (e) { console.error('[frps-plugin] WARNING: cannot read signing secret —', e.message, '(failing OPEN)'); }

// Lazy, defensive DB open. If better-sqlite3 isn't available or the DB can't
// open, subdomain checks are skipped (fail open) rather than crash.
let db = null;
try { db = require('better-sqlite3')(P2P_DB, { readonly: true, fileMustExist: true }); }
catch (e) { console.error('[frps-plugin] WARNING: cannot open p2p.db —', e.message, '(subdomain checks disabled)'); }

function expectedToken(nodeId) {
  if (!SIGNING_SECRET || !nodeId) return null;
  return crypto.createHmac('sha256', SIGNING_SECRET).update(String(nodeId)).digest('hex');
}

// The node's assigned subdomain == nodes.custom_name. customDomains look like
// "<custom_name>.ha1.cinexis.cloud" and "<custom_name>alexa.ha1.cinexis.cloud".
function nodeOwnsDomains(nodeId, customDomains, subdomain) {
  if (!db) return true; // fail open — can't check
  let row;
  try { row = db.prepare('SELECT custom_name FROM nodes WHERE node_id = ?').get(nodeId); }
  catch (_) { return true; }
  if (!row || !row.custom_name) return true; // unknown node — let token check be the gate
  const cn = String(row.custom_name).toLowerCase();
  const doms = [].concat(customDomains || [], subdomain ? [subdomain] : []);
  if (doms.length === 0) return true;
  // Every requested domain must start with this node's custom_name.
  return doms.every(d => {
    const host = String(d || '').toLowerCase();
    return host === cn || host.startsWith(cn + '.') || host.startsWith(cn + 'alexa.') ||
           host.startsWith(cn + 'bot.') || host.startsWith(cn);
  });
}

function decide(op, content) {
  // Returns { allow:boolean, reason:string }
  const metas = (content && content.user && content.user.metas) || content.metas || {};
  const nodeId = metas.node_id || '';
  const nodeToken = metas.node_token || '';

  // No metadata at all → legacy addon.
  if (!nodeId || !nodeToken) {
    if (REQUIRE_META) return { allow: false, reason: 'metadata_required' };
    return { allow: true, reason: 'legacy_no_meta' };
  }

  // Validate the per-node token.
  const exp = expectedToken(nodeId);
  if (!exp) return { allow: true, reason: 'no_secret_fail_open' };
  let tokenOk = false;
  try { tokenOk = crypto.timingSafeEqual(Buffer.from(exp), Buffer.from(String(nodeToken))); }
  catch (_) { tokenOk = false; }
  if (!tokenOk) return { allow: false, reason: 'bad_node_token' };

  // On NewProxy, also enforce subdomain ownership.
  if (op === 'NewProxy') {
    const customDomains = content.custom_domains || content.customDomains || [];
    const subdomain = content.subdomain || '';
    if (!nodeOwnsDomains(nodeId, customDomains, subdomain)) {
      return { allow: false, reason: 'subdomain_not_owned' };
    }
  }
  return { allow: true, reason: 'ok' };
}

const server = http.createServer((req, res) => {
  if (req.method !== 'POST') { res.writeHead(200); return res.end(JSON.stringify({ reject: false, unchange: true })); }
  let body = '';
  req.on('data', c => { body += c; if (body.length > 1e6) req.destroy(); });
  req.on('end', () => {
    let allow = true, reason = 'default_allow', op = '?';
    try {
      const payload = JSON.parse(body || '{}');
      op = payload.op || '?';
      const d = decide(op, payload.content || {});
      allow = d.allow; reason = d.reason;
    } catch (e) {
      // Parse/logic error → FAIL OPEN.
      allow = true; reason = 'plugin_error_fail_open:' + e.message;
    }

    const enforcing = MODE === 'enforce';
    const finalReject = enforcing && !allow;
    // Always log a decision line so observe-mode gives us the data to flip safely.
    console.log(`[frps-plugin] op=${op} mode=${MODE} allow=${allow} reason=${reason}${finalReject ? ' -> REJECTED' : ''}`);

    res.writeHead(200, { 'Content-Type': 'application/json' });
    if (finalReject) res.end(JSON.stringify({ reject: true, reject_reason: 'cinexis: ' + reason }));
    else res.end(JSON.stringify({ reject: false, unchange: true }));
  });
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`[frps-plugin] listening on 127.0.0.1:${PORT} — MODE=${MODE} REQUIRE_META=${REQUIRE_META ? 1 : 0} secret=${SIGNING_SECRET ? 'loaded' : 'MISSING(fail-open)'} db=${db ? 'open' : 'unavailable'}`);
});

process.on('uncaughtException', e => console.error('[frps-plugin] uncaught:', e.message));
process.on('unhandledRejection', e => console.error('[frps-plugin] unhandledRejection:', e && e.message));
