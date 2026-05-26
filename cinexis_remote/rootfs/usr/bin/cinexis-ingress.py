#!/usr/bin/env python3
"""
Cinexis Remote Access — Ingress Web UI
Served on INGRESS_PORT (HA ingress). Provides:
  - License / OTP activation (Send OTP, verify, show status)
  - Voice device management (enable/disable per platform per domain)
    Writes /share/cinexis/voice_exclusions.json — read by cinexis-alexa.py
"""

import json
import os
import http.server
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────
PORT              = int(os.environ.get("INGRESS_PORT", "18082"))
WA_SERVICE_URL    = os.environ.get("WA_SERVICE_URL", "http://127.0.0.1:18083")
STORAGE_DIR       = "/share/cinexis"
LICENSE_KEY_FILE  = f"{STORAGE_DIR}/license_key"
EXCLUSIONS_FILE   = f"{STORAGE_DIR}/voice_exclusions.json"
NODE_ID_FILE      = f"{STORAGE_DIR}/node_id"
DEVICE_SECRET_FILE= f"{STORAGE_DIR}/device_secret"
CUSTOMER_PROFILE_FILE = f"{STORAGE_DIR}/customer_profile.json"
TELEGRAM_CONFIG_FILE  = f"{STORAGE_DIR}/telegram_config.json"
RULES_FILE            = f"{STORAGE_DIR}/notification_rules.json"
DAILY_FILE            = f"{STORAGE_DIR}/daily_summary.json"
HA_BASE           = "http://supervisor/core"
CINEXIS_API       = "https://cinexis.cloud"

# s6-overlay stores container env vars in this directory
_S6_ENV_DIR = "/var/run/s6/container_environment"

def get_supervisor_token():
    """Read SUPERVISOR_TOKEN dynamically — env may not be populated at startup."""
    # 1. Standard env var (available after hassio_api/homeassistant_api granted)
    token = os.environ.get("SUPERVISOR_TOKEN", "") or os.environ.get("HASSIO_TOKEN", "")
    if token:
        return token
    # 2. s6-overlay container environment directory (set after init completes)
    for name in ("SUPERVISOR_TOKEN", "HASSIO_TOKEN"):
        try:
            with open(os.path.join(_S6_ENV_DIR, name)) as f:
                t = f.read().strip()
                if t:
                    return t
        except Exception:
            pass
    return ""

SUPPORTED_DOMAINS = {
    "light", "switch", "cover", "climate", "fan",
    "scene", "script", "media_player", "input_boolean"
}
DOMAIN_ICONS = {
    "light": "💡", "switch": "🔌", "cover": "🪟", "climate": "🌡️",
    "fan": "🌀", "scene": "🎬", "script": "📜", "media_player": "🔊",
    "input_boolean": "🔘"
}
PLATFORMS = ["alexa", "google", "siri"]
PLATFORM_LABELS = {"alexa": "Alexa", "google": "Google Home", "siri": "Siri"}

def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[cinexis-ingress] {ts} {msg}", flush=True)

# ── Helpers ───────────────────────────────────────────────────────────────────
def license_active():
    try:
        with open(LICENSE_KEY_FILE) as f:
            return bool(f.read().strip())
    except FileNotFoundError:
        return False

def load_exclusions():
    try:
        with open(EXCLUSIONS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_exclusions(data):
    os.makedirs(STORAGE_DIR, exist_ok=True)
    with open(EXCLUSIONS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def ha_get_states():
    token = get_supervisor_token()
    if not token:
        log("HA states fetch: SUPERVISOR_TOKEN not available (hassio_api not granted yet?)")
        return []
    try:
        req = urllib.request.Request(
            f"{HA_BASE}/api/states",
            headers={"Authorization": f"Bearer {token}"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log(f"HA states fetch failed: {e}")
        return []

def get_node_credentials():
    """Read node_id + device_secret from /share/cinexis. Returns (node_id, secret) or (None, None)."""
    try:
        with open(NODE_ID_FILE) as f:        node_id = f.read().strip()
        with open(DEVICE_SECRET_FILE) as f:  secret  = f.read().strip()
        if node_id and secret: return node_id, secret
    except Exception:
        pass
    return None, None

def load_customer_profile():
    """Return the saved customer profile dict, or None if onboarding incomplete."""
    try:
        with open(CUSTOMER_PROFILE_FILE) as f:
            return json.load(f)
    except Exception:
        return None

def save_customer_profile(data):
    os.makedirs(STORAGE_DIR, exist_ok=True)
    with open(CUSTOMER_PROFILE_FILE, "w") as f:
        json.dump(data, f, indent=2)

def cinexis_addon_call(method, path, payload=None):
    """Call /api/addon/* with node credentials auto-attached. Returns parsed JSON or {'ok':False,...}."""
    node_id, secret = get_node_credentials()
    if not node_id or not secret:
        return {"ok": False, "error": "no_node_credentials"}
    body = {"node_id": node_id, "device_secret": secret}
    if payload: body.update(payload)
    if method == "GET":
        qs = urllib.parse.urlencode(body)
        url = f"{CINEXIS_API}/api/addon{path}?{qs}"
        req = urllib.request.Request(url, method="GET")
    else:
        data = json.dumps(body).encode()
        url = f"{CINEXIS_API}/api/addon{path}"
        req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:    return json.loads(e.read().decode())
        except: return {"ok": False, "error": f"http_{e.code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def load_daily_config():
    try:
        with open(DAILY_FILE) as f: return json.load(f)
    except Exception: return {}

def save_daily_config(data):
    os.makedirs(STORAGE_DIR, exist_ok=True)
    with open(DAILY_FILE, "w") as f:
        json.dump(data, f, indent=2)

def load_rules():
    try:
        with open(RULES_FILE) as f:
            return json.load(f).get("rules", [])
    except Exception:
        return []

def save_rules(rules):
    os.makedirs(STORAGE_DIR, exist_ok=True)
    with open(RULES_FILE, "w") as f:
        json.dump({"rules": rules}, f, indent=2)

def load_telegram_config():
    """Return { bot_token, bot_username, default_chat_id } or empty dict."""
    try:
        with open(TELEGRAM_CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_telegram_config(data):
    os.makedirs(STORAGE_DIR, exist_ok=True)
    with open(TELEGRAM_CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)

def telegram_api(method, payload=None, bot_token=None):
    """Wrapper around api.telegram.org. Returns parsed JSON or {ok:False,...}."""
    cfg = load_telegram_config()
    token = bot_token or cfg.get("bot_token")
    if not token:
        return {"ok": False, "error": "no_bot_token"}
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        if payload:
            data = json.dumps(payload).encode()
            req = urllib.request.Request(url, data=data, method="POST",
                                          headers={"Content-Type": "application/json"})
        else:
            req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:    return json.loads(e.read().decode())
        except: return {"ok": False, "error": f"http_{e.code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def wa_service_call(method, path, payload=None):
    """Proxy a request to the local Baileys service (cinexis-wa.js on 127.0.0.1:18083).
    Returns parsed JSON or an {ok:False, error:...} stub on network failure.
    """
    url = WA_SERVICE_URL + path
    try:
        if method == "GET":
            req = urllib.request.Request(url, method="GET")
        else:
            data = json.dumps(payload or {}).encode()
            req = urllib.request.Request(url, data=data, method=method,
                                          headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:    return json.loads(e.read().decode())
        except: return {"ok": False, "error": f"http_{e.code}"}
    except Exception as e:
        return {"ok": False, "error": f"wa_service_unreachable: {e}"}

def cinexis_post(path, payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{CINEXIS_API}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # Parse JSON error body from API (e.g. 429 rate_limited, 400 invalid_otp)
        raw = e.read()
        try:
            return json.loads(raw)
        except Exception:
            raise Exception(f"HTTP {e.code}: {e.reason}")

# ── HTML helpers ──────────────────────────────────────────────────────────────
def page(title, body, base_path="/", extra_head=""):
    # HA ingress strips its prefix before forwarding, but the browser still
    # sees the full /api/hassio_ingress/TOKEN/ URL.  Setting <base> makes all
    # relative links (form actions, fetch paths) resolve correctly.
    base_tag = f'<base href="{base_path}">' if base_path != "/" else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — Cinexis</title>
{base_tag}
{extra_head}
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f1117;color:#e2e8f0;min-height:100vh;padding:0}}
.wrap{{max-width:960px;margin:0 auto;padding:20px 16px}}
h1{{font-size:1.4rem;font-weight:700;color:#fff;margin-bottom:4px}}
h2{{font-size:1.1rem;font-weight:600;color:#94a3b8;margin:24px 0 12px}}
.card{{background:#1e2333;border:1px solid #2d3748;border-radius:12px;padding:20px;margin-bottom:16px}}
.card h2{{margin-top:0}}
.badge{{display:inline-flex;align-items:center;gap:6px;padding:4px 12px;border-radius:20px;font-size:.8rem;font-weight:600}}
.badge-green{{background:#14532d;color:#4ade80;border:1px solid #166534}}
.badge-amber{{background:#451a03;color:#fb923c;border:1px solid #7c2d12}}
.badge-gray{{background:#1e293b;color:#94a3b8;border:1px solid #334155}}
label{{font-size:.85rem;color:#94a3b8;display:block;margin-bottom:4px;margin-top:12px}}
input[type=email],input[type=text]{{width:100%;padding:10px 12px;background:#0f1117;border:1px solid #334155;border-radius:8px;color:#e2e8f0;font-size:.9rem}}
input:focus{{outline:none;border-color:#6366f1}}
.btn{{padding:10px 20px;border:none;border-radius:8px;font-size:.9rem;font-weight:600;cursor:pointer;transition:background .15s}}
.btn-primary{{background:#6366f1;color:#fff}}
.btn-primary:hover{{background:#4f46e5}}
.btn-secondary{{background:#1e293b;color:#94a3b8;border:1px solid #334155}}
.btn-secondary:hover{{background:#334155;color:#e2e8f0}}
.btn-danger{{background:#7f1d1d;color:#fca5a5;border:1px solid #991b1b}}
.btn-danger:hover{{background:#991b1b}}
.btn-sm{{padding:4px 12px;font-size:.78rem}}
.msg{{padding:10px 14px;border-radius:8px;font-size:.85rem;margin-top:10px}}
.msg-ok{{background:#14532d;color:#4ade80;border:1px solid #166534}}
.msg-err{{background:#7f1d1d;color:#fca5a5;border:1px solid #991b1b}}
.msg-info{{background:#1e3a5f;color:#93c5fd;border:1px solid #1d4ed8}}
.tabs{{display:flex;gap:4px;margin-bottom:16px;border-bottom:1px solid #2d3748;padding-bottom:0}}
.tab{{padding:8px 16px;cursor:pointer;border-radius:8px 8px 0 0;font-size:.85rem;color:#94a3b8;background:transparent;border:none;transition:all .15s;position:relative;bottom:-1px}}
.tab.active{{background:#1e2333;color:#6366f1;border:1px solid #2d3748;border-bottom:1px solid #1e2333;font-weight:600}}
.tab:hover:not(.active){{color:#e2e8f0}}
.tab-panel{{display:none}}.tab-panel.active{{display:block}}
table{{width:100%;border-collapse:collapse;font-size:.83rem}}
th{{text-align:left;padding:8px 10px;color:#64748b;font-weight:600;border-bottom:1px solid #2d3748;white-space:nowrap}}
td{{padding:7px 10px;border-bottom:1px solid #1a2235;vertical-align:middle}}
tr:hover td{{background:#1a2235}}
.domain-row td{{background:#14181f;color:#94a3b8;font-size:.78rem;font-weight:600;padding:6px 10px}}
.toggle-group{{display:flex;gap:4px;align-items:center}}
input[type=checkbox]{{width:16px;height:16px;accent-color:#6366f1;cursor:pointer}}
.bulk-btns{{display:flex;gap:4px}}
.stat-bar{{display:flex;gap:16px;margin-bottom:12px;flex-wrap:wrap}}
.stat{{font-size:.8rem;color:#64748b}}.stat span{{color:#e2e8f0;font-weight:600}}
.filter-row{{display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap}}
select{{padding:6px 10px;background:#0f1117;border:1px solid #334155;border-radius:6px;color:#e2e8f0;font-size:.83rem}}
.saving{{opacity:.5;pointer-events:none}}
</style>
</head>
<body>
<div class="wrap">
<h1>⚡ Cinexis Remote Access</h1>
{body}
</div>
<script>
function showTab(id){{
  document.querySelectorAll('.tab,.tab-panel').forEach(el=>el.classList.remove('active'));
  document.querySelectorAll('[data-tab="'+id+'"]').forEach(el=>el.classList.add('active'));
}}
</script>
</body>
</html>"""

# ── License / OTP page ────────────────────────────────────────────────────────
def render_onboarding_wizard(base_path="/", error=""):
    """Single-page wizard shown when customer_profile.json is missing.

    Sections:
      A. Customer details      → POST {base}wizard/onboard
      B. Plan picker           → POST {base}wizard/subscribe  (returns short_url)
      C. Awaiting payment      → JS polls {base}wizard/status
      D. Notification opt-in   → POST {base}wizard/notif  (returns QR deep_link)
      E. Done                  → reload to dashboard

    All sections are in the DOM at once; client-side JS toggles visibility.
    Servers state via small JSON responses — no full page reloads.
    """
    err_html = f'<div class="alert err">{error}</div>' if error else ''
    return f"""
<div class="wizard">
  <div class="hero">
    <div class="logo">⚡</div>
    <h1>Welcome to Cinexis</h1>
    <p class="muted">Let's get your Home Assistant remote access set up — about 90 seconds.</p>
  </div>

  <div class="steps">
    <div class="step active" data-step="a"><span class="dot">1</span> Your details</div>
    <div class="step"        data-step="b"><span class="dot">2</span> Choose plan</div>
    <div class="step"        data-step="c"><span class="dot">3</span> Pay</div>
    <div class="step"        data-step="d"><span class="dot">4</span> Notifications</div>
  </div>

  {err_html}

  <!-- ── A. Customer details ─────────────────────────────────────────── -->
  <section id="sec-a" class="active">
    <h2>Tell us about you</h2>
    <p class="muted">Used for your account, invoices, and renewal reminders. You can change everything later.</p>
    <form id="form-a">
      <label>Full name *</label>      <input name="name"  required>
      <label>Email *</label>           <input name="email" type="email" required placeholder="you@example.com">
      <label>WhatsApp number</label>   <input name="phone" placeholder="+91 98765 43210">
      <label>City / Location</label>   <input name="location" placeholder="Mumbai, Maharashtra">
      <label>GSTIN (optional)</label>  <input name="gstin" placeholder="22AAAAA0000A1Z5">
      <label>Use case</label>
      <select name="use_case">
        <option value="home">Home</option>
        <option value="office">Office</option>
        <option value="showroom">Showroom / demo</option>
        <option value="rental">Rental property</option>
      </select>
      <button type="submit" class="btn">Continue →</button>
    </form>
  </section>

  <!-- ── B. Plan picker ──────────────────────────────────────────────── -->
  <section id="sec-b">
    <h2>Pick your plan</h2>
    <p class="muted">All plans include a 3-day full-features trial. Cancel anytime.</p>
    <div class="cycle">
      <label><input type="radio" name="billing" value="monthly" checked> Monthly</label>
      <label><input type="radio" name="billing" value="quarterly"> Quarterly <span class="save">-10%</span></label>
      <label><input type="radio" name="billing" value="halfyearly"> Half-yearly <span class="save">-15%</span></label>
      <label><input type="radio" name="billing" value="yearly"> Yearly <span class="save">-17%</span></label>
    </div>
    <div class="plans">
      <div class="plan" data-slug="ha-remote"><h3>Lite</h3><div class="price" data-m="299" data-q="799" data-h="1499" data-y="2990">₹299/mo</div><ul><li>HA remote access</li><li>Encrypted tunnel</li><li>Email support</li></ul><button class="btn ghost" data-pick="ha-remote">Choose Lite</button></div>
      <div class="plan" data-slug="smart"><h3>Smart</h3><div class="price" data-m="399" data-q="1077" data-h="2035" data-y="3830">₹399/mo</div><ul><li>Everything in Lite</li><li>WhatsApp notifications</li><li>Telegram notifications</li></ul><button class="btn ghost" data-pick="smart">Choose Smart</button></div>
      <div class="plan highlight" data-slug="ha-voice"><h3>Pro</h3><div class="price" data-m="599" data-q="1599" data-h="2999" data-y="5990">₹599/mo</div><ul><li>Everything in Smart</li><li>Alexa voice control</li><li>Priority WA support</li></ul><button class="btn" data-pick="ha-voice">Choose Pro</button></div>
      <div class="plan" data-slug="ultimate"><h3>Ultimate</h3><div class="price" data-m="799" data-q="2157" data-h="4075" data-y="7670">₹799/mo</div><ul><li>Everything in Pro</li><li>Google Home</li><li>Siri Shortcuts</li></ul><button class="btn ghost" data-pick="ultimate">Choose Ultimate</button></div>
    </div>
    <p class="muted small">Already paid your dealer / partner? <a href="#" id="offline-link">Skip Razorpay — admin will activate manually.</a></p>
  </section>

  <!-- ── C. Awaiting payment ─────────────────────────────────────────── -->
  <section id="sec-c">
    <h2>Almost there</h2>
    <p>Click the button below to complete payment on Razorpay. UPI AutoPay, cards, and netbanking all supported.</p>
    <a id="pay-link" href="#" target="_blank" class="btn big">Pay with UPI / Card / Netbanking ↗</a>
    <p class="muted small">After payment, this page auto-refreshes within 30 seconds.</p>
    <div id="poll-status" class="muted small" style="margin-top:14px"></div>
  </section>

  <!-- ── D. Notifications (coming in v1.11 — Baileys-based local WA Web) ── -->
  <section id="sec-d">
    <h2>Notifications</h2>
    <p>Configure your HA event → WhatsApp / Telegram notifications in the next addon update (v1.11).</p>
    <p class="muted">You'll be able to:</p>
    <ul class="muted">
      <li>Scan a QR with <strong>your own WhatsApp</strong> — messages go from <em>your</em> number, not ours</li>
      <li>Paste your <strong>own Telegram bot token</strong> for Telegram alerts</li>
      <li>Pick which HA entities trigger notifications (lights, doors, motion, automations, batteries…)</li>
      <li>Custom message templates per trigger</li>
      <li>Daily morning summary report</li>
      <li>Per-recipient routing (you, family group, security guard, etc.)</li>
    </ul>
    <p class="muted small">Account setup is now complete — your trial is active. Click below to open the dashboard.</p>
    <button id="finish" class="btn big">Open dashboard →</button>
  </section>
</div>

<style>
  .wizard {{ max-width: 720px; margin: 0 auto; padding: 24px 16px }}
  .hero {{ text-align: center; padding: 24px 0 }}
  .hero .logo {{ font-size: 2.6rem }}
  .hero h1 {{ font-size: 1.6rem; margin: 8px 0 4px }}
  .muted {{ color: var(--text3); font-size: .92rem }}
  .small {{ font-size: .82rem }}
  .steps {{ display: flex; gap: 8px; margin: 16px 0 24px; flex-wrap: wrap }}
  .step {{ flex: 1 1 22%; padding: 10px 12px; border-radius: 8px; background: var(--card); font-size: .82rem; opacity: .5 }}
  .step.active {{ opacity: 1; background: var(--accent2); color: #fff }}
  .step .dot {{ display: inline-block; width: 22px; height: 22px; line-height: 22px; text-align: center; background: rgba(0,0,0,.2); border-radius: 50%; margin-right: 6px; font-weight: 700 }}
  section {{ display: none; background: var(--card); border-radius: 14px; padding: 24px; margin-bottom: 16px }}
  section.active {{ display: block }}
  section h2 {{ margin: 0 0 6px; font-size: 1.2rem }}
  form label {{ display: block; font-size: .8rem; color: var(--text2); margin: 12px 0 4px }}
  form input, form select {{ width: 100%; padding: 10px 12px; background: var(--bg); border: 1px solid #2a2f3c; border-radius: 8px; color: var(--text); font-size: .95rem }}
  .btn {{ display: inline-block; margin-top: 16px; padding: 12px 22px; background: var(--accent2); color: #fff; border: 0; border-radius: 8px; font-weight: 600; cursor: pointer; text-decoration: none }}
  .btn.big {{ width: 100%; text-align: center; font-size: 1rem; padding: 14px }}
  .btn.ghost {{ background: transparent; border: 1px solid var(--accent2); color: var(--accent2) }}
  .btn.small {{ padding: 8px 14px; font-size: .85rem; margin-top: 8px }}
  .cycle {{ display: flex; gap: 14px; flex-wrap: wrap; margin: 14px 0 }}
  .cycle label {{ background: var(--bg); padding: 8px 12px; border-radius: 8px; cursor: pointer; font-size: .88rem }}
  .save {{ color: #16a34a; font-weight: 700; font-size: .76rem }}
  .plans {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 14px }}
  .plan {{ background: var(--bg); padding: 16px; border-radius: 12px; border: 1px solid #2a2f3c }}
  .plan.highlight {{ border-color: var(--accent2); box-shadow: 0 0 0 2px rgba(99,102,241,.2) }}
  .plan h3 {{ margin: 0 0 6px }}
  .plan .price {{ font-size: 1.4rem; font-weight: 800; margin-bottom: 10px }}
  .plan ul {{ padding-left: 18px; margin: 0 0 12px; font-size: .82rem }}
  .plan ul li {{ margin: 4px 0 }}
  .qr-area {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin: 16px 0 }}
  .qr-card {{ background: var(--bg); padding: 18px; border-radius: 12px; text-align: center }}
  .qr-placeholder {{ background: #0b0f17; min-height: 140px; display: flex; align-items: center; justify-content: center; border-radius: 8px; font-size: .82rem; color: var(--text3); padding: 10px; word-break: break-all }}
  .alert.err {{ background: #fef2f2; border: 1px solid #fecaca; color: #991b1b; padding: 10px 14px; border-radius: 8px; margin-bottom: 14px }}
</style>
<script>
const BASE = '{base_path}';
function $(s){{return document.querySelector(s)}}
function $$(s){{return document.querySelectorAll(s)}}
function showSec(letter){{
  $$('section').forEach(s=>s.classList.remove('active'));
  $('#sec-'+letter).classList.add('active');
  $$('.step').forEach((s,i)=>s.classList.toggle('active', 'abcd'.indexOf(s.dataset.step) <= 'abcd'.indexOf(letter)));
  window.scrollTo({{top:0,behavior:'smooth'}});
}}

// Step A — submit details
$('#form-a').onsubmit = async (e) => {{
  e.preventDefault();
  const fd = Object.fromEntries(new FormData(e.target));
  const btn = e.target.querySelector('button'); btn.disabled = true; btn.textContent = 'Saving…';
  const r = await fetch(BASE+'wizard/onboard', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify(fd)}}).then(r=>r.json()).catch(e=>({{ok:false,error:String(e)}}));
  if (r.ok) {{ showSec('b'); }} else {{ alert('Could not save: ' + (r.error || 'unknown')); btn.disabled = false; btn.textContent = 'Continue →'; }}
}};

// Step B — plan selection
function getBilling(){{ return document.querySelector('input[name="billing"]:checked').value; }}
function updatePrices(){{
  const b = getBilling(); const keys = {{monthly:'m', quarterly:'q', halfyearly:'h', yearly:'y'}};
  $$('.price').forEach(el => {{
    const v = el.dataset[keys[b]];
    el.textContent = '₹' + Number(v).toLocaleString('en-IN') + '/' + b.replace('ly','');
  }});
}}
$$('input[name="billing"]').forEach(r => r.addEventListener('change', updatePrices));
updatePrices();
$$('.plan button').forEach(btn => btn.onclick = async () => {{
  const slug = btn.dataset.pick; const billing = getBilling();
  btn.disabled = true; btn.textContent = 'Creating subscription…';
  const r = await fetch(BASE+'wizard/subscribe', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{plan_slug:slug, billing_period:billing}})}}).then(r=>r.json());
  if (r.ok && r.short_url) {{
    $('#pay-link').href = r.short_url;
    showSec('c');
    pollStatus();
  }} else {{
    alert('Could not start subscription: ' + (r.error || 'unknown') + '\\n\\nIf Razorpay is being slow, ask your dealer to mark you as paid offline from admin.cinexis.cloud.');
    btn.disabled = false; btn.textContent = 'Choose ' + btn.parentElement.querySelector('h3').textContent;
  }}
}});
$('#offline-link').onclick = (e) => {{
  e.preventDefault();
  alert('Got it. Your dealer can mark you as paid from admin.cinexis.cloud → Customers → your row → 💵 Mark Paid.\\n\\nMeanwhile, your 3-day trial of full features starts now.');
  showSec('d');
}};

// Step C — poll subscription status
let pollTimer = null;
async function pollStatus(){{
  $('#poll-status').textContent = '⏳ Watching for payment…';
  pollTimer = setInterval(async () => {{
    const r = await fetch(BASE+'wizard/status').then(r=>r.json()).catch(()=>null);
    if (r && r.license_status === 'active') {{
      clearInterval(pollTimer);
      $('#poll-status').textContent = '✅ Payment received! Setting up notifications…';
      setTimeout(()=>showSec('d'), 800);
    }}
  }}, 5000);
}}

// Step D — placeholder until v1.11 ships the Baileys-based WA Web + TG flow.
$('#finish').onclick = () => location.href = BASE;
</script>
"""

def render_ha_integration_section(base_path="/"):
    """
    'Use from Home Assistant' card — exposes the addon as REST commands so
    customers define triggers inside HA's automation editor (the same place
    they configure everything else) instead of duplicating logic in the
    addon's own rules screen.

    GreenAPI-style: addon = pipe, HA automations = brain.
    """
    return f"""
<div class="card" id="ha-int-card">
  <div class="card-header"><span class="card-icon">🏠</span>Use from Home Assistant automations</div>
  <p class="muted small">
    Paste the snippet below into your <code>configuration.yaml</code> and reload <em>YAML configuration → All YAML configuration</em>.
    You can then call <code>service: notify.cinexis_whatsapp</code> or
    <code>service: notify.cinexis_telegram</code> from any HA automation, script, or button.
  </p>

  <h4 style="margin-top:14px;font-size:.95rem">1. configuration.yaml</h4>
  <pre id="ha-snippet" style="background:var(--bg);padding:14px;border-radius:8px;font-size:.78rem;overflow-x:auto;line-height:1.55"># Cinexis Remote Access — WhatsApp sender.
# The addon publishes port 18083 to the HA host, so this URL is reachable
# from HA Core directly. The addon's WhatsApp session must already be
# paired (see the WhatsApp card on this page).
#
# For Telegram: don't use the addon. HA has a built-in `telegram_bot`
# integration that works directly with your BotFather token — set it up
# once and call `notify.telegram` from any automation. (Snippet below.)

rest_command:
  cinexis_whatsapp:
    url: "http://homeassistant.local.hass.io:18083/send/text"
    method: POST
    content_type: "application/json"
    payload: '{{{{ {{ "to": to, "text": message }} | to_json }}}}'

  cinexis_whatsapp_image:
    url: "http://homeassistant.local.hass.io:18083/send/image"
    method: POST
    content_type: "application/json"
    payload: '{{{{ {{ "to": to, "image_url": image_url, "caption": caption }} | to_json }}}}'

# ── Telegram (HA native — no addon involvement) ─────────────────────────
# Replace the values with your BotFather token + your chat_id.
telegram_bot:
  - platform: polling
    api_key: !secret telegram_bot_token
    allowed_chat_ids:
      - !secret telegram_chat_id

notify:
  - name: telegram
    platform: telegram
    chat_id: !secret telegram_chat_id

# ── Optional: wrap WhatsApp as a notify service too ─────────────────────
# Lets any automation use `service: notify.cinexis_whatsapp` like a regular
# notify integration.
notify:
  - name: cinexis_whatsapp
    platform: rest
    resource: "http://homeassistant.local.hass.io:18083/send/text"
    method: POST_JSON
    message_param_name: text
    target_param_name: to
</pre>
  <button class="btn btn-primary" onclick="copyHa('ha-snippet')" style="margin-top:6px">📋 Copy configuration.yaml snippet</button>

  <h4 style="margin-top:22px;font-size:.95rem">2. secrets.yaml (so the token never lives in plain configuration.yaml)</h4>
  <pre id="ha-secrets" style="background:var(--bg);padding:14px;border-radius:8px;font-size:.78rem;overflow-x:auto;line-height:1.55"># /config/secrets.yaml
telegram_bot_token: "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
telegram_chat_id:   123456789      # your personal chat — get from @userinfobot
</pre>
  <button class="btn btn-primary" onclick="copyHa('ha-secrets')" style="margin-top:6px">📋 Copy secrets.yaml</button>

  <h4 style="margin-top:22px;font-size:.95rem">3. Example automation — front door at night</h4>
  <pre id="ha-auto" style="background:var(--bg);padding:14px;border-radius:8px;font-size:.78rem;overflow-x:auto;line-height:1.55">- alias: Front door opens at night
  description: WhatsApp + Telegram the family when the door opens between 10pm and 6am
  triggers:
    - trigger: state
      entity_id: binary_sensor.front_door
      to: "on"
  conditions:
    - condition: time
      after: "22:00:00"
      before: "06:00:00"
  actions:
    # WhatsApp via Cinexis addon (your paired number)
    - service: rest_command.cinexis_whatsapp
      data:
        to: "919999000001"
        message: >-
          🚨 Front door opened at {{{{ now().strftime('%H:%M:%S') }}}}.
          Last person home: {{{{ states('person.someone') }}}}.
    # Telegram via HA's native integration (your own bot)
    - service: notify.telegram
      data:
        message: "🚨 Front door (night) — {{{{ now().strftime('%H:%M') }}}}"
</pre>
  <button class="btn btn-primary" onclick="copyHa('ha-auto')" style="margin-top:6px">📋 Copy example automation</button>

  <h4 style="margin-top:22px;font-size:.95rem">3. Or via the GUI automation editor</h4>
  <ol class="muted small" style="margin-top:8px">
    <li>HA → <strong>Settings → Automations &amp; Scenes → + Create Automation</strong></li>
    <li>Trigger: pick whatever (state change, time pattern, event…)</li>
    <li>Action: <strong>Call service → rest_command.cinexis_whatsapp</strong></li>
    <li>Data fields:
      <pre style="background:var(--bg);padding:10px;border-radius:6px;font-size:.75rem;margin-top:6px">to: "919999000001"
message: "Hello from HA 👋"</pre>
    </li>
  </ol>

  <p class="muted small" style="margin-top:16px;border-top:1px solid #2a2f3c;padding-top:14px">
    <strong>Why this is better than the addon's own rules editor:</strong> you keep all logic in HA's
    automation editor (same place you set everything else), can mix &amp; match WA / Telegram /
    email / mobile push in one automation, and benefit from HA's robust condition / trigger /
    template engine. The addon is just the WhatsApp + Telegram pipe.
  </p>
</div>
<script>
function copyHa(id) {{
  const text = document.getElementById(id).innerText;
  navigator.clipboard.writeText(text).then(() => {{
    const btn = event.target;
    const orig = btn.textContent;
    btn.textContent = '✅ Copied';
    setTimeout(() => btn.textContent = orig, 1500);
  }});
}}
</script>
"""

def render_daily_section(base_path="/"):
    """Daily summary report config — fires once per day at configured time.

    Owner picks: enable, time HH:MM, entities to include, recipients.
    cinexis-events.py daily_loop() checks every minute and fires when due.
    """
    cfg     = load_daily_config()
    enabled = "checked" if cfg.get("enabled") else ""
    tm      = cfg.get("time", "08:00")
    ents    = "\n".join(cfg.get("entities") or [])
    wa_r    = ", ".join(cfg.get("wa_recipients") or [])
    tg_r    = ", ".join(cfg.get("tg_chat_ids") or [])
    bat     = cfg.get("battery_low_pct") or 20
    return f"""
<div class="card" id="daily-card">
  <div class="card-header"><span class="card-icon">📊</span>Daily Summary Report</div>
  <p class="muted small">Once a day at the time you set, fire a recap of the previous 24 h — state changes per entity, low batteries flagged. Goes to the same WA / TG you configure here.</p>
  <form id="daily-form">
    <label style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
      <input type="checkbox" name="enabled" {enabled}> Enabled
    </label>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px">
      <div><label style="font-size:.8rem;color:var(--text2)">Send at (HH:MM, local time)</label><input name="time" type="time" value="{tm}" style="width:100%;padding:8px;background:var(--bg);border:1px solid #2a2f3c;border-radius:6px;color:var(--text)"></div>
      <div><label style="font-size:.8rem;color:var(--text2)">Low battery threshold (%)</label><input name="battery_low_pct" type="number" value="{bat}" min="1" max="100" style="width:100%;padding:8px;background:var(--bg);border:1px solid #2a2f3c;border-radius:6px;color:var(--text)"></div>
    </div>
    <label style="font-size:.8rem;color:var(--text2)">Entities to include (one per line) — try <code>binary_sensor.*</code>, <code>sensor.*_battery</code>, doors, motion, automations</label>
    <textarea name="entities" rows="6" placeholder="binary_sensor.front_door&#10;binary_sensor.kitchen_motion&#10;sensor.living_room_battery&#10;automation.morning_routine" style="width:100%;padding:8px;background:var(--bg);border:1px solid #2a2f3c;border-radius:6px;color:var(--text);font-family:monospace;font-size:.85rem;margin-bottom:10px">{esc_html(ents)}</textarea>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
      <div><label style="font-size:.8rem;color:var(--text2)">WhatsApp recipients</label><input name="wa_recipients" value="{esc_html(wa_r)}" placeholder="918792962291" style="width:100%;padding:8px;background:var(--bg);border:1px solid #2a2f3c;border-radius:6px;color:var(--text);margin-bottom:10px"></div>
      <div><label style="font-size:.8rem;color:var(--text2)">Telegram chat IDs</label><input name="tg_chat_ids" value="{esc_html(tg_r)}" placeholder="123456789" style="width:100%;padding:8px;background:var(--bg);border:1px solid #2a2f3c;border-radius:6px;color:var(--text);margin-bottom:10px"></div>
    </div>
    <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
      <button class="btn btn-primary" type="submit">Save</button>
      <button class="btn btn-ghost" type="button" onclick="dailyTest()">Send now (test)</button>
    </div>
    <div id="daily-result" class="muted small" style="margin-top:10px"></div>
  </form>
</div>
<script>
const DAILY_BASE = '{base_path}';
document.getElementById('daily-form').onsubmit = async (e) => {{
  e.preventDefault();
  const fd = Object.fromEntries(new FormData(e.target));
  const payload = {{
    enabled: !!fd.enabled,
    time:    fd.time || '08:00',
    battery_low_pct: parseInt(fd.battery_low_pct || '20', 10),
    entities: (fd.entities || '').split('\\n').map(s=>s.trim()).filter(Boolean),
    wa_recipients: (fd.wa_recipients||'').split(',').map(s=>s.trim()).filter(Boolean),
    tg_chat_ids:   (fd.tg_chat_ids||'').split(',').map(s=>s.trim()).filter(Boolean),
  }};
  const r = await fetch(DAILY_BASE + 'daily/save', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify(payload)}}).then(r=>r.json());
  const out = document.getElementById('daily-result');
  out.textContent = r.ok ? '✅ Saved' : '❌ ' + (r.error || 'unknown');
}};
async function dailyTest(){{
  const out = document.getElementById('daily-result');
  out.textContent = 'Sending test…';
  const r = await fetch(DAILY_BASE + 'daily/test', {{method:'POST'}}).then(r=>r.json());
  out.textContent = r.ok ? '✅ Test summary sent — check WhatsApp / Telegram' : '❌ ' + (r.error || 'unknown');
}}
</script>
"""

def render_rules_section(base_path="/"):
    """Notification rules CRUD. List + add/edit/delete via small JS.
    On save → POST /rules/save which writes notification_rules.json.
    cinexis-events.py reloads the file every 30s so changes take effect quickly.
    """
    rules = load_rules()
    rule_rows = ""
    for r in rules:
        trig = r.get("trigger") or {}
        cond = f"{trig.get('entity_id','—')}: {trig.get('from','any')} → {trig.get('to','any')}"
        chans = ", ".join(r.get("channels") or [])
        rule_rows += f"""
        <tr data-id="{r.get('id')}">
          <td>{esc_html(r.get('name','—'))}</td>
          <td><code style="font-size:.78rem">{esc_html(cond)}</code></td>
          <td>{esc_html(chans)}</td>
          <td>{'✅' if r.get('enabled', True) else '⏸'}</td>
          <td><button class="btn btn-ghost small" onclick="editRule('{r.get('id')}')">Edit</button>
              <button class="btn btn-ghost small" onclick="delRule('{r.get('id')}')" style="color:#ef4444">Delete</button></td>
        </tr>"""
    return f"""
<div class="card" id="rules-card">
  <div class="card-header"><span class="card-icon">⚡</span>Notification Rules</div>
  <p class="muted small">Pick HA entities → fire a message to WhatsApp / Telegram when they change. Rules apply within 30 s of saving.</p>
  <button class="btn btn-primary" onclick="newRule()" style="margin-bottom:14px">+ New rule</button>
  <table class="rules-table" style="width:100%;border-collapse:collapse;font-size:.88rem">
    <thead><tr><th align="left">Name</th><th align="left">Trigger</th><th align="left">Channels</th><th>On</th><th></th></tr></thead>
    <tbody id="rules-tbody">{rule_rows or '<tr><td colspan=5 class=muted style="padding:14px;text-align:center">No rules yet — click "+ New rule" to add one.</td></tr>'}</tbody>
  </table>
</div>

<div id="rule-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:1000;align-items:center;justify-content:center">
  <div style="background:var(--card);padding:24px;border-radius:14px;max-width:520px;width:90%;max-height:90vh;overflow:auto">
    <h2 id="rule-modal-title" style="margin:0 0 16px">New rule</h2>
    <form id="rule-form">
      <input type="hidden" name="id">
      <label>Name</label><input name="name" required placeholder="Front door at night" style="width:100%;padding:8px;background:var(--bg);border:1px solid #2a2f3c;border-radius:6px;color:var(--text);margin-bottom:10px">
      <label>Entity ID (HA)</label><input name="entity_id" required placeholder="binary_sensor.front_door" list="entity-list" style="width:100%;padding:8px;background:var(--bg);border:1px solid #2a2f3c;border-radius:6px;color:var(--text);font-family:monospace;font-size:.85rem;margin-bottom:10px">
      <datalist id="entity-list"></datalist>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
        <div><label>From state (optional)</label><input name="from" placeholder="off" style="width:100%;padding:8px;background:var(--bg);border:1px solid #2a2f3c;border-radius:6px;color:var(--text);margin-bottom:10px"></div>
        <div><label>To state (optional)</label><input name="to" placeholder="on" style="width:100%;padding:8px;background:var(--bg);border:1px solid #2a2f3c;border-radius:6px;color:var(--text);margin-bottom:10px"></div>
      </div>
      <label>Message template</label>
      <textarea name="message_template" rows="3" required style="width:100%;padding:8px;background:var(--bg);border:1px solid #2a2f3c;border-radius:6px;color:var(--text);font-family:monospace;font-size:.85rem;margin-bottom:6px" placeholder="🚨 {{{{name}}}} opened at {{{{time}}}}"></textarea>
      <div class="muted small" style="margin-bottom:10px">Vars: <code>{{{{entity}}}}</code>, <code>{{{{state}}}}</code>, <code>{{{{old_state}}}}</code>, <code>{{{{name}}}}</code>, <code>{{{{time}}}}</code>, <code>{{{{date}}}}</code>, <code>{{{{datetime}}}}</code>, <code>{{{{unit}}}}</code></div>
      <label>Channels</label>
      <div style="margin-bottom:10px">
        <label style="display:inline-block;margin-right:14px"><input type="checkbox" name="ch_wa"> 📱 WhatsApp</label>
        <label style="display:inline-block"><input type="checkbox" name="ch_tg"> 💬 Telegram</label>
      </div>
      <label>WhatsApp recipients (comma-separated, +91…)</label>
      <input name="wa_recipients" placeholder="918792962291, 911234567890" style="width:100%;padding:8px;background:var(--bg);border:1px solid #2a2f3c;border-radius:6px;color:var(--text);margin-bottom:10px">
      <label>Telegram chat IDs (comma-separated; blank = default)</label>
      <input name="tg_chat_ids" placeholder="123456789, -100456789012" style="width:100%;padding:8px;background:var(--bg);border:1px solid #2a2f3c;border-radius:6px;color:var(--text);margin-bottom:10px">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
        <div><label>Cooldown (seconds)</label><input name="cooldown_seconds" type="number" value="30" min="0" style="width:100%;padding:8px;background:var(--bg);border:1px solid #2a2f3c;border-radius:6px;color:var(--text)"></div>
        <div><label>Enabled</label><div style="padding:10px 0"><label><input type="checkbox" name="enabled" checked> Active</label></div></div>
      </div>
      <div style="display:flex;gap:8px;margin-top:16px;justify-content:flex-end">
        <button type="button" class="btn btn-ghost" onclick="closeModal()">Cancel</button>
        <button type="submit" class="btn btn-primary">Save rule</button>
      </div>
    </form>
  </div>
</div>

<script>
const RULES_BASE = '{base_path}';
let RULES_CACHE = {json.dumps(rules)};

async function loadEntities(){{
  try {{
    const r = await fetch(RULES_BASE + 'ha/entities').then(r=>r.json());
    const dl = document.getElementById('entity-list');
    dl.innerHTML = (r.entities || []).map(e => `<option value="${{e.entity_id}}">${{e.friendly_name||''}}</option>`).join('');
  }} catch(e) {{}}
}}
loadEntities();

function newRule(){{
  document.getElementById('rule-modal-title').textContent = 'New rule';
  const f = document.getElementById('rule-form');
  f.reset();
  f.elements.id.value = '';
  f.elements.enabled.checked = true;
  document.getElementById('rule-modal').style.display = 'flex';
}}
function editRule(id){{
  const r = RULES_CACHE.find(x => x.id === id);
  if (!r) return;
  document.getElementById('rule-modal-title').textContent = 'Edit rule';
  const f = document.getElementById('rule-form');
  f.elements.id.value = r.id;
  f.elements.name.value = r.name || '';
  f.elements.entity_id.value = (r.trigger||{{}}).entity_id || '';
  f.elements.from.value = (r.trigger||{{}}).from || '';
  f.elements.to.value = (r.trigger||{{}}).to || '';
  f.elements.message_template.value = r.message_template || '';
  f.elements.ch_wa.checked = (r.channels||[]).includes('whatsapp');
  f.elements.ch_tg.checked = (r.channels||[]).includes('telegram');
  f.elements.wa_recipients.value = (r.wa_recipients||[]).join(', ');
  f.elements.tg_chat_ids.value   = (r.tg_chat_ids||[]).join(', ');
  f.elements.cooldown_seconds.value = r.cooldown_seconds || 30;
  f.elements.enabled.checked = !!r.enabled;
  document.getElementById('rule-modal').style.display = 'flex';
}}
function closeModal(){{ document.getElementById('rule-modal').style.display = 'none'; }}
async function delRule(id){{
  if (!confirm('Delete this rule?')) return;
  await fetch(RULES_BASE + 'rules/delete', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{id}})}});
  location.reload();
}}
document.getElementById('rule-form').onsubmit = async (e) => {{
  e.preventDefault();
  const fd = new FormData(e.target);
  const channels = []; if (fd.get('ch_wa')) channels.push('whatsapp'); if (fd.get('ch_tg')) channels.push('telegram');
  const payload = {{
    id: fd.get('id') || ('rule-' + Math.random().toString(36).slice(2,9)),
    name: fd.get('name'),
    enabled: !!fd.get('enabled'),
    trigger: {{ type: 'state_change', entity_id: fd.get('entity_id'), from: fd.get('from')||undefined, to: fd.get('to')||undefined }},
    message_template: fd.get('message_template'),
    channels,
    wa_recipients: (fd.get('wa_recipients')||'').split(',').map(s=>s.trim()).filter(Boolean),
    tg_chat_ids: (fd.get('tg_chat_ids')||'').split(',').map(s=>s.trim()).filter(Boolean),
    cooldown_seconds: parseInt(fd.get('cooldown_seconds')||'0', 10),
  }};
  const r = await fetch(RULES_BASE + 'rules/save', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify(payload)}}).then(r=>r.json());
  if (r.ok) location.reload();
  else alert('Save failed: ' + (r.error || 'unknown'));
}};
</script>
"""

def esc_html(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

def render_telegram_section(base_path="/"):
    """Telegram setup panel. Owner pastes their own BotFather token + chat IDs."""
    cfg     = load_telegram_config()
    token   = cfg.get("bot_token", "")
    botname = cfg.get("bot_username", "")
    chatid  = cfg.get("default_chat_id", "")
    masked  = (token[:8] + "…" + token[-4:]) if token else ""
    status_html = (f'<div class="wa-status-row connected"><div class="dot"></div>'
                   f'<div><div style="font-weight:700">Configured: @{botname or "—"}</div>'
                   f'<div class="muted small">Token {masked}</div></div></div>') if token else \
                  '<div class="wa-status-row waiting"><div class="dot"></div><div>No bot token configured yet. Get one from <a href="https://t.me/BotFather" target="_blank">@BotFather</a> in Telegram.</div></div>'
    return f"""
<div class="card" id="tg-card">
  <div class="card-header"><span class="card-icon">💬</span>Telegram</div>
  <p class="muted small">Use <strong>your own Telegram bot</strong> to deliver HA notifications. Create one via <a href="https://t.me/BotFather" target="_blank">@BotFather</a>, paste the token below.</p>
  {status_html}
  <form id="tg-form" style="margin-top:14px">
    <label style="display:block;font-size:.8rem;color:var(--text2);margin-bottom:4px">Bot token</label>
    <input name="bot_token" placeholder="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11" value="{token}" style="width:100%;padding:9px 11px;background:var(--bg);border:1px solid #2a2f3c;border-radius:8px;color:var(--text);font-family:monospace;font-size:.85rem" />
    <label style="display:block;font-size:.8rem;color:var(--text2);margin:10px 0 4px">Default chat ID (your personal chat, or a group's chat_id)</label>
    <input name="default_chat_id" placeholder="123456789 or -100456789012" value="{chatid}" style="width:100%;padding:9px 11px;background:var(--bg);border:1px solid #2a2f3c;border-radius:8px;color:var(--text);font-family:monospace;font-size:.85rem" />
    <div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap">
      <button class="btn btn-primary" type="submit">Verify &amp; save</button>
      <button class="btn btn-ghost" type="button" onclick="tgTest()" {'disabled' if not token else ''}>Send test</button>
    </div>
    <div id="tg-result" class="muted small" style="margin-top:10px"></div>
  </form>
  <p class="muted small" style="margin-top:14px">📌 To get your chat_id: open <a href="https://t.me/userinfobot" target="_blank">@userinfobot</a> in Telegram, send /start. For a group, add the bot to the group and the chat_id will be negative.</p>
</div>
<script>
const TG_BASE = '{base_path}';
document.getElementById('tg-form').onsubmit = async (e) => {{
  e.preventDefault();
  const fd  = Object.fromEntries(new FormData(e.target));
  const out = document.getElementById('tg-result');
  out.textContent = 'Verifying token…';
  const r = await fetch(TG_BASE + 'tg/verify-and-save', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify(fd)}}).then(r=>r.json());
  if (r.ok) {{
    out.innerHTML = '✅ Saved. Bot: @' + r.bot_username;
    setTimeout(() => location.reload(), 1500);
  }} else {{
    out.textContent = '❌ ' + (r.error || 'unknown');
  }}
}};
async function tgTest(){{
  const out = document.getElementById('tg-result');
  out.textContent = 'Sending…';
  const r = await fetch(TG_BASE + 'tg/test', {{method:'POST'}}).then(r=>r.json());
  out.textContent = r.ok ? '✅ Test message sent to your default chat' : '❌ ' + (r.error || 'unknown');
}}
</script>
"""

def render_whatsapp_section(base_path="/"):
    """WhatsApp pairing/status panel. Polls /wa/status every 5s via JS.

    First-pair flow:
      1. Service starts → QR appears in /wa/qr — UI displays it
      2. Owner scans with their WhatsApp → /wa/status returns connected=true
      3. UI switches to "Connected as +XX..." + Send-test form + Logout

    The Node service auto-reconnects so we don't expose a "reconnect" button.
    """
    return f"""
<div class="card" id="wa-card">
  <div class="card-header"><span class="card-icon">📱</span>WhatsApp</div>
  <p class="muted small">Pair your <strong>own</strong> WhatsApp once — the addon will send your Home Assistant notifications from this number.</p>
  <div id="wa-body">
    <div class="muted small">⏳ Loading WhatsApp service status…</div>
  </div>
</div>
<style>
  #wa-qr-img {{ background:#fff; padding:10px; border-radius:10px; display:block; margin:14px auto; max-width: 280px; width:80% }}
  .wa-status-row {{ display:flex; gap:12px; align-items:center; padding:14px; background:var(--bg); border-radius:10px; margin:12px 0 }}
  .wa-status-row .dot {{ width:10px; height:10px; border-radius:50%; flex-shrink:0 }}
  .wa-status-row.connected .dot {{ background:#22c55e; box-shadow:0 0 0 3px rgba(34,197,94,.2) }}
  .wa-status-row.waiting   .dot {{ background:#eab308 }}
  .wa-status-row.error     .dot {{ background:#ef4444 }}
  .wa-test-form {{ display:flex; gap:8px; margin-top:12px; flex-wrap:wrap }}
  .wa-test-form input {{ flex:1 1 200px; padding:8px 10px; border:1px solid #2a2f3c; background:var(--bg); border-radius:8px; color:var(--text); font-size:.88rem }}
  .wa-test-form button {{ padding:8px 14px }}
</style>
<script>
const WA_BASE = '{base_path}';
async function waRefreshStatus(){{
  const body = document.getElementById('wa-body');
  try {{
    const s = await fetch(WA_BASE + 'wa/status').then(r => r.json());
    if (s.error && s.error.startsWith('wa_service_unreachable')) {{
      body.innerHTML = '<div class="wa-status-row error"><div class="dot"></div><div>WhatsApp service not running. Check the addon log.</div></div>';
      return;
    }}
    if (s.connected) {{
      body.innerHTML = `
        <div class="wa-status-row connected">
          <div class="dot"></div>
          <div>
            <div style="font-weight:700">Connected as +${{s.phone}}</div>
            <div class="muted small">Since ${{new Date(s.since).toLocaleString()}}</div>
          </div>
        </div>
        <form class="wa-test-form" onsubmit="return waSendTest(event)">
          <input name="to"   placeholder="Send test to (default: your own number)" />
          <input name="text" placeholder="Message text"  value="🧪 Cinexis test message" />
          <button class="btn btn-primary" type="submit">Send</button>
        </form>
        <div id="wa-test-result" class="muted small" style="margin-top:8px"></div>
        <button class="btn btn-ghost" style="margin-top:14px" onclick="waLogout()">Log out / re-pair</button>
      `;
      return;
    }}
    if (s.has_qr) {{
      const qr = await fetch(WA_BASE + 'wa/qr').then(r => r.json());
      if (qr.qr_data_url) {{
        body.innerHTML = `
          <div class="wa-status-row waiting"><div class="dot"></div><div>Waiting for QR scan — open WhatsApp → ⋮ → Linked devices → Link a device</div></div>
          <img id="wa-qr-img" src="${{qr.qr_data_url}}" alt="WhatsApp pairing QR" />
          <p class="muted small" style="text-align:center">QR refreshes automatically every few seconds.</p>
        `;
        return;
      }}
    }}
    body.innerHTML = '<div class="wa-status-row waiting"><div class="dot"></div><div>Starting up — a fresh QR will appear here in a few seconds.</div></div>';
  }} catch (e) {{
    body.innerHTML = '<div class="wa-status-row error"><div class="dot"></div><div>Could not reach the WhatsApp service: ' + e.message + '</div></div>';
  }}
}}
async function waSendTest(ev){{
  ev.preventDefault();
  const fd = Object.fromEntries(new FormData(ev.target));
  const out = document.getElementById('wa-test-result');
  out.textContent = 'Sending…';
  const r = await fetch(WA_BASE + 'wa/test', {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify(fd)}}).then(r=>r.json());
  out.textContent = r.ok ? `✅ Sent to ${{r.sent_to}}` : '❌ ' + (r.error || 'unknown');
  return false;
}}
async function waLogout(){{
  if (!confirm('Log out the linked WhatsApp? You will need to scan a new QR.')) return;
  await fetch(WA_BASE + 'wa/logout', {{method:'POST'}});
  setTimeout(waRefreshStatus, 1500);
}}
waRefreshStatus();
setInterval(waRefreshStatus, 5000);
</script>
"""

def render_license_section(msg="", msg_type=""):
    active = license_active()
    msg_html = f'<div class="msg msg-{msg_type}">{msg}</div>' if msg else ""

    if active:
        try:
            with open(LICENSE_KEY_FILE) as f:
                key = f.read().strip()
            key_display = key[:8] + "..." if len(key) > 8 else key
        except Exception:
            key_display = "loaded"
        return f"""
<div class="card">
  <h2>License</h2>
  <p style="margin-bottom:12px"><span class="badge badge-green">✅ Active</span></p>
  <p style="font-size:.83rem;color:#64748b">Key: <code style="color:#94a3b8">{key_display}</code></p>
  <p style="font-size:.83rem;color:#64748b;margin-top:8px">
    Alexa Smart Home is enabled. Say <em>"Alexa, discover devices"</em> to sync.
  </p>
  <form method="post" action="license/clear" style="margin-top:14px">
    <button type="submit" class="btn btn-danger btn-sm">Clear License (re-activate)</button>
  </form>
  {msg_html}
</div>"""

    return f"""
<div class="card">
  <h2>License Activation</h2>
  <p style="margin-bottom:12px"><span class="badge badge-amber">⚠️ Not Activated</span></p>
  <p style="font-size:.83rem;color:#64748b;margin-bottom:16px">
    Enter your email registered with <strong>cinexis.cloud</strong> to activate Alexa Smart Home.
  </p>

  <form method="post" action="license/send-otp" id="otpRequestForm">
    <label>Email (registered with cinexis.cloud)</label>
    <input type="email" name="email" id="emailInput" placeholder="you@example.com" required autocomplete="email">
    <div style="margin-top:12px;display:flex;gap:8px;align-items:center">
      <button type="submit" class="btn btn-primary">Send OTP</button>
      <span id="otpTimer" style="font-size:.8rem;color:#64748b"></span>
    </div>
  </form>

  <form method="post" action="license/verify-otp" style="margin-top:20px;padding-top:20px;border-top:1px solid #2d3748">
    <label>OTP (received via email)</label>
    <input type="text" name="otp" placeholder="123456" maxlength="6" pattern="[0-9]{{6}}" inputmode="numeric">
    <input type="hidden" name="email" id="verifyEmail">
    <div style="margin-top:12px">
      <button type="submit" class="btn btn-primary">Verify &amp; Activate</button>
    </div>
    <p style="font-size:.78rem;color:#475569;margin-top:8px">OTP is valid for 30 minutes.</p>
  </form>
  {msg_html}
</div>
<script>
var storedEmail = localStorage.getItem('cinexis_otp_email') || '';
if(storedEmail) {{
  document.getElementById('emailInput').value = storedEmail;
  document.getElementById('verifyEmail').value = storedEmail;
}}
document.getElementById('emailInput').addEventListener('input', function(){{
  localStorage.setItem('cinexis_otp_email', this.value);
  document.getElementById('verifyEmail').value = this.value;
}});
// Cooldown timer
var cdEnd = parseInt(localStorage.getItem('cinexis_otp_cd') || '0');
function tickTimer(){{
  var left = Math.max(0, Math.ceil((cdEnd - Date.now()) / 1000));
  var el = document.getElementById('otpTimer');
  if(left > 0) {{
    el.textContent = 'Wait ' + left + 's before resending';
    setTimeout(tickTimer, 1000);
  }} else {{
    el.textContent = '';
  }}
}}
tickTimer();
document.getElementById('otpRequestForm').addEventListener('submit', function(){{
  cdEnd = Date.now() + 180000;
  localStorage.setItem('cinexis_otp_cd', cdEnd);
}});
</script>"""

# ── Voice devices page ────────────────────────────────────────────────────────
def render_voice_section(msg="", msg_type="", active_platform="alexa"):
    states = ha_get_states()
    exclusions = load_exclusions()

    # Group by domain, filter to supported
    by_domain = {}
    for s in states:
        eid    = s["entity_id"]
        domain = eid.split(".")[0]
        if domain not in SUPPORTED_DOMAINS:
            continue
        attrs = s.get("attributes", {})
        name  = attrs.get("friendly_name") or eid.replace("_", " ").title()
        by_domain.setdefault(domain, []).append({"id": eid, "name": name})

    if not by_domain:
        no_devices = '<div class="msg msg-info">No supported HA entities found. Is HA running?</div>'
        return f'<div class="card"><h2>Voice Devices</h2>{no_devices}</div>'

    # Sort domains
    domain_order = ["light","switch","cover","climate","fan","media_player","input_boolean","script","scene"]
    sorted_domains = sorted(by_domain.keys(), key=lambda d: domain_order.index(d) if d in domain_order else 99)

    total = sum(len(v) for v in by_domain.values())
    msg_html = f'<div class="msg msg-{msg_type}">{msg}</div>' if msg else ""

    # Platform tab headers
    tab_html = '<div class="tabs">'
    for p in PLATFORMS:
        active_cls = " active" if p == active_platform else ""
        tab_html += f'<button class="tab{active_cls}" data-tab="{p}" onclick="showTab(\'{p}\')">{PLATFORM_LABELS[p]}</button>'
    tab_html += "</div>"

    # Build one table panel per platform
    panels = ""
    for plat in PLATFORMS:
        active_cls = " active" if plat == active_platform else ""
        enabled_count = sum(
            1 for dm in by_domain.values()
            for e in dm
            if not exclusions.get(e["id"], {}).get(plat, False)
        )
        rows = ""
        for domain in sorted_domains:
            entities = by_domain[domain]
            icon = DOMAIN_ICONS.get(domain, "▪️")
            domain_enabled = sum(1 for e in entities if not exclusions.get(e["id"], {}).get(plat, False))
            rows += f"""<tr class="domain-row">
  <td colspan="2">{icon} {domain.replace('_',' ').title()} ({domain_enabled}/{len(entities)})</td>
  <td><div class="bulk-btns">
    <button class="btn btn-secondary btn-sm" onclick="bulkToggle('{plat}','{domain}',true)">All</button>
    <button class="btn btn-secondary btn-sm" onclick="bulkToggle('{plat}','{domain}',false)">None</button>
  </div></td>
</tr>"""
            for e in sorted(entities, key=lambda x: x["name"].lower()):
                checked = "" if exclusions.get(e["id"], {}).get(plat, False) else "checked"
                rows += f"""<tr data-domain="{domain}">
  <td style="color:#cbd5e1">{e['name']}</td>
  <td style="color:#475569;font-size:.75rem">{e['id']}</td>
  <td><input type="checkbox" {checked} onchange="toggleDevice('{plat}','{e['id']}',this.checked)" title="Enable for {PLATFORM_LABELS[plat]}"></td>
</tr>"""

        panels += f"""<div class="tab-panel{active_cls}" data-tab="{plat}">
<div class="stat-bar">
  <div class="stat">Enabled for {PLATFORM_LABELS[plat]}: <span id="count-{plat}">{enabled_count}</span> / {total}</div>
</div>
<div style="display:flex;gap:8px;margin-bottom:12px">
  <button class="btn btn-secondary btn-sm" onclick="bulkToggle('{plat}',null,true)">Enable All</button>
  <button class="btn btn-secondary btn-sm" onclick="bulkToggle('{plat}',null,false)">Disable All</button>
</div>
<div style="overflow-x:auto">
<table>
<thead><tr><th>Device</th><th>Entity ID</th><th>{PLATFORM_LABELS[plat]}</th></tr></thead>
<tbody id="tbody-{plat}">{rows}</tbody>
</table>
</div>
</div>"""

    return f"""<div class="card">
<h2>Voice Device Management</h2>
<p style="font-size:.83rem;color:#64748b;margin-bottom:16px">
  Control which devices are exposed to each voice assistant.<br>
  After changing, say <em>"Alexa, discover devices"</em> (or equivalent) to sync.
</p>
{msg_html}
{tab_html}
{panels}
</div>
<script>
function toggleDevice(platform, entityId, enabled) {{
  fetch('voice/toggle', {{
    method: 'POST',
    headers: {{'Content-Type':'application/json'}},
    body: JSON.stringify({{platform, entity_id: entityId, enabled}})
  }}).then(r=>r.json()).then(d=>{{
    if(!d.ok) console.error('Toggle failed', d);
    updateCount(platform);
  }});
}}
function bulkToggle(platform, domain, enabled) {{
  var rows = document.querySelectorAll('#tbody-'+platform+' tr[data-domain]');
  rows.forEach(function(row){{
    if(domain && row.dataset.domain !== domain) return;
    var cb = row.querySelector('input[type=checkbox]');
    if(!cb) return;
    cb.checked = enabled;
    var eid = row.querySelector('td:nth-child(2)').textContent.trim();
    fetch('voice/toggle', {{
      method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{platform, entity_id: eid, enabled}})
    }});
  }});
  setTimeout(()=>updateCount(platform), 200);
}}
function updateCount(platform) {{
  var rows = document.querySelectorAll('#tbody-'+platform+' tr[data-domain]');
  var cnt = 0;
  rows.forEach(function(r){{
    var cb = r.querySelector('input[type=checkbox]');
    if(cb && cb.checked) cnt++;
  }});
  var el = document.getElementById('count-'+platform);
  if(el) el.textContent = cnt;
}}
</script>"""

# ── HTTP Handler ──────────────────────────────────────────────────────────────
class IngressHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send_html(self, code, html):
        body = html.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def ingress_base(self):
        """Return the HA ingress base path with trailing slash, e.g. /api/hassio_ingress/TOKEN/"""
        base = self.headers.get("X-Ingress-Path", "")
        if base and not base.endswith("/"):
            base += "/"
        return base or "/"

    def redirect_home(self):
        base = self.ingress_base()
        self.send_response(302)
        self.send_header("Location", base)
        self.end_headers()

    def read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length)

    def parse_form(self):
        raw = self.read_body().decode()
        return dict(urllib.parse.parse_qsl(raw))

    def do_GET(self):
        path = self.path.split("?")[0]
        base = self.ingress_base()

        if path in ("/", "/index.html"):
            # First-run experience: if customer hasn't completed the wizard,
            # show it instead of the legacy license/voice dashboard.
            # Show wizard only on a true fresh install: no customer profile
            # AND no cached license. Legacy customers (license-only, no profile)
            # skip the wizard and see the dashboard so the WhatsApp QR + new
            # cards are immediately reachable.
            has_profile = bool(load_customer_profile())
            has_license = os.path.exists(LICENSE_KEY_FILE) and os.path.getsize(LICENSE_KEY_FILE) > 0
            if not has_profile and not has_license:
                self.send_html(200, page("Welcome to Cinexis", render_onboarding_wizard(base_path=base), base_path=base))
                return
            lic   = render_license_section()
            voice = render_voice_section()
            wa    = render_whatsapp_section(base_path=base)
            tg    = render_telegram_section(base_path=base)
            ha    = render_ha_integration_section(base_path=base)
            daily = render_daily_section(base_path=base)
            rules = render_rules_section(base_path=base)
            # Order: License, WhatsApp QR (prominent), Telegram, HA integration
            # (the recommended path), then Daily Summary, then standalone Rules
            # (de-emphasised — HA automations are the right answer for triggers),
            # then Voice (Alexa) at the bottom.
            self.send_html(200, page("Cinexis Setup",
                lic + wa + tg + ha + daily + rules + voice,
                base_path=base))
        elif path == "/ha/entities":
            # Fetch the HA entity list so the rule editor can autocomplete.
            try:
                req = urllib.request.Request(
                    "http://supervisor/core/api/states",
                    headers={"Authorization": "Bearer " + get_supervisor_token()},
                )
                with urllib.request.urlopen(req, timeout=10) as r:
                    states = json.loads(r.read().decode())
                ents = [{"entity_id": s.get("entity_id"), "friendly_name": (s.get("attributes") or {}).get("friendly_name", "")} for s in states]
                self.send_json(200, {"entities": ents})
            except Exception as e:
                self.send_json(200, {"entities": [], "error": str(e)})
        elif path == "/wa/status":
            self.send_json(200, wa_service_call("GET", "/status"))
        elif path == "/wa/qr":
            self.send_json(200, wa_service_call("GET", "/qr"))
        elif path == "/wizard/status":
            # Addon-side proxy of /api/addon/status so the JS can poll over
            # ingress (cross-origin to cinexis.cloud would need CORS).
            self.send_json(200, cinexis_addon_call("GET", "/status"))
        elif path == "/health":
            self.send_json(200, {"ok": True})
        else:
            self.send_html(404, page("Not Found", "<p>Page not found.</p>", base_path=base))

    def do_POST(self):
        path = self.path.split("?")[0]
        base = self.ingress_base()

        # ── Onboarding wizard (first-run experience) ───────────────────────
        if path == "/wizard/onboard":
            try:
                payload = json.loads(self.read_body() or b"{}")
            except Exception:
                return self.send_json(400, {"ok": False, "error": "bad_json"})
            resp = cinexis_addon_call("POST", "/onboard", payload)
            if resp.get("ok"):
                # Persist locally so we don't re-show the wizard next boot
                save_customer_profile({
                    "customer_id": resp.get("customer_id"),
                    "name":        payload.get("name"),
                    "email":       payload.get("email"),
                    "phone":       payload.get("phone"),
                    "location":    payload.get("location"),
                    "gstin":       payload.get("gstin"),
                    "use_case":    payload.get("use_case"),
                    "onboarded_at": datetime.now(timezone.utc).isoformat(),
                })
            return self.send_json(200, resp)

        if path == "/wizard/subscribe":
            try:
                payload = json.loads(self.read_body() or b"{}")
            except Exception:
                return self.send_json(400, {"ok": False, "error": "bad_json"})
            resp = cinexis_addon_call("POST", "/subscribe", payload)
            if resp.get("ok"):
                # Stash subscription URL so we can show "renew" link later
                prof = load_customer_profile() or {}
                prof["subscription_id"]        = resp.get("subscription_id")
                prof["subscription_short_url"] = resp.get("short_url")
                prof["chosen_plan"]            = payload.get("plan_slug")
                prof["chosen_billing"]         = payload.get("billing_period")
                save_customer_profile(prof)
            return self.send_json(200, resp)

        # ── Daily summary config + test fire ──────────────────────────────
        if path == "/daily/save":
            try:    payload = json.loads(self.read_body() or b"{}")
            except: return self.send_json(400, {"ok": False, "error": "bad_json"})
            save_daily_config(payload)
            return self.send_json(200, {"ok": True})

        if path == "/daily/test":
            # Force a fresh summary by deleting the last-sent marker, then
            # poke the events service via SIGUSR1 — simpler: just run the
            # summary inline using the same helpers (won't conflict because
            # daily_loop checks last_sent_date which we'll update afterwards).
            cfg = load_daily_config()
            if not cfg.get("entities") or not (cfg.get("wa_recipients") or cfg.get("tg_chat_ids")):
                return self.send_json(400, {"ok": False, "error": "configure_entities_and_recipients_first"})
            # Build text inline (mirror of cinexis-events.py build_summary_text)
            from datetime import datetime as _dt
            text = f"📊 Cinexis daily summary — {_dt.now().strftime('%a %d %b %Y')} (TEST)\\n\\nThis is a test fire from the addon UI.\\n\\nIf you receive this, your daily summary is wired up. The real one will fire at {cfg.get('time','08:00')} every day.\\n\\n— Cinexis"
            sent = 0; errs = []
            for to in cfg.get("wa_recipients", []):
                r = wa_service_call("POST", "/send/text", {"to": to, "text": text})
                if r.get("ok"): sent += 1
                else: errs.append(f"WA→{to}: {r.get('error')}")
            for chat in cfg.get("tg_chat_ids", []):
                r = telegram_api("sendMessage", {"chat_id": chat, "text": text})
                if r.get("ok"): sent += 1
                else: errs.append(f"TG→{chat}: {r.get('description') or r.get('error')}")
            return self.send_json(200, {"ok": sent > 0, "sent": sent, "errors": errs})

        # ── Notification rules CRUD ───────────────────────────────────────
        if path == "/rules/save":
            try:    payload = json.loads(self.read_body() or b"{}")
            except: return self.send_json(400, {"ok": False, "error": "bad_json"})
            if not payload.get("id") or not payload.get("name") or not (payload.get("trigger") or {}).get("entity_id"):
                return self.send_json(400, {"ok": False, "error": "id_name_entity_required"})
            rules = load_rules()
            # Upsert
            idx = next((i for i, r in enumerate(rules) if r.get("id") == payload["id"]), -1)
            if idx >= 0: rules[idx] = payload
            else:        rules.append(payload)
            save_rules(rules)
            return self.send_json(200, {"ok": True, "count": len(rules)})

        if path == "/rules/delete":
            try:    payload = json.loads(self.read_body() or b"{}")
            except: return self.send_json(400, {"ok": False, "error": "bad_json"})
            if not payload.get("id"): return self.send_json(400, {"ok": False, "error": "id_required"})
            rules = load_rules()
            rules = [r for r in rules if r.get("id") != payload["id"]]
            save_rules(rules)
            return self.send_json(200, {"ok": True, "count": len(rules)})

        # ── Telegram bot config + send ────────────────────────────────────
        if path == "/tg/verify-and-save":
            try:    payload = json.loads(self.read_body() or b"{}")
            except: payload = {}
            token = (payload.get("bot_token") or "").strip()
            chat  = (payload.get("default_chat_id") or "").strip()
            if not token:
                return self.send_json(400, {"ok": False, "error": "bot_token required"})
            # Verify token via getMe
            res = telegram_api("getMe", bot_token=token)
            if not res.get("ok"):
                return self.send_json(400, {"ok": False, "error": res.get("description") or res.get("error") or "invalid_token"})
            bot = res.get("result") or {}
            save_telegram_config({
                "bot_token":       token,
                "bot_username":    bot.get("username"),
                "bot_id":          bot.get("id"),
                "default_chat_id": chat,
                "saved_at":        datetime.now(timezone.utc).isoformat(),
            })
            return self.send_json(200, {"ok": True, "bot_username": bot.get("username")})

        if path == "/tg/test":
            cfg = load_telegram_config()
            if not cfg.get("bot_token") or not cfg.get("default_chat_id"):
                return self.send_json(400, {"ok": False, "error": "token_or_chat_id_missing"})
            res = telegram_api("sendMessage", payload={
                "chat_id": cfg["default_chat_id"],
                "text":    f"🧪 Cinexis Telegram test at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} — your bot is wired up correctly.",
            })
            return self.send_json(200, {"ok": bool(res.get("ok")), "error": res.get("description") or res.get("error")})

        if path == "/tg/send":
            try:    payload = json.loads(self.read_body() or b"{}")
            except: payload = {}
            cfg = load_telegram_config()
            if not cfg.get("bot_token"):
                return self.send_json(400, {"ok": False, "error": "bot_not_configured"})
            chat = payload.get("chat_id") or cfg.get("default_chat_id")
            if not chat or not payload.get("text"):
                return self.send_json(400, {"ok": False, "error": "chat_id_and_text_required"})
            res = telegram_api("sendMessage", payload={"chat_id": chat, "text": payload["text"]})
            return self.send_json(200, {"ok": bool(res.get("ok")), "error": res.get("description") or res.get("error")})

        # ── WhatsApp service proxy (Node Baileys at 127.0.0.1:18083) ──────
        if path == "/wa/test":
            try:    payload = json.loads(self.read_body() or b"{}")
            except: payload = {}
            return self.send_json(200, wa_service_call("POST", "/test", payload))
        if path == "/wa/send/text":
            try:    payload = json.loads(self.read_body() or b"{}")
            except: payload = {}
            return self.send_json(200, wa_service_call("POST", "/send/text", payload))
        if path == "/wa/send/image":
            try:    payload = json.loads(self.read_body() or b"{}")
            except: payload = {}
            return self.send_json(200, wa_service_call("POST", "/send/image", payload))
        if path == "/wa/logout":
            return self.send_json(200, wa_service_call("POST", "/logout"))

        if path == "/license/send-otp":
            form = self.parse_form()
            email = (form.get("email") or "").strip()
            if not email:
                body = render_license_section("Please enter a valid email.", "err") + render_voice_section()
                self.send_html(400, page("Cinexis Setup", body, base_path=base))
                return
            try:
                resp = cinexis_post("/api/node/otp-request", {"email": email})
                if resp.get("ok"):
                    msg = f"OTP sent to <strong>{email}</strong>. Check your inbox and enter the code below."
                    mtype = "ok"
                else:
                    err = resp.get("error", "unknown")
                    if err == "please_wait":
                        wait = resp.get("wait_seconds", 60)
                        msg = f"Please wait {wait} seconds before requesting another OTP."
                    else:
                        msg = f"Could not send OTP: {err}"
                    mtype = "err"
            except Exception as e:
                msg = f"Network error: {e}"
                mtype = "err"
            body = render_license_section(msg, mtype) + render_voice_section()
            self.send_html(200, page("Cinexis Setup", body, base_path=base))

        elif path == "/license/verify-otp":
            form = self.parse_form()
            email = (form.get("email") or "").strip()
            otp   = (form.get("otp") or "").strip()
            if not email or not otp:
                body = render_license_section("Email and OTP are required.", "err") + render_voice_section()
                self.send_html(400, page("Cinexis Setup", body, base_path=base))
                return
            try:
                resp = cinexis_post("/api/node/otp-verify", {"email": email, "otp": otp})
                if resp.get("ok"):
                    key = resp.get("license_key", "")
                    os.makedirs(STORAGE_DIR, exist_ok=True)
                    with open(LICENSE_KEY_FILE, "w") as f:
                        f.write(key)
                    log(f"License activated for {email}")
                    body = render_license_section("License activated! Alexa Smart Home is now enabled.", "ok") + render_voice_section()
                    self.send_html(200, page("Cinexis Setup", body, base_path=base))
                else:
                    err = resp.get("error", "unknown")
                    msgs = {
                        "invalid_otp": "Invalid OTP. Please check and try again.",
                        "otp_expired": "OTP has expired. Click Send OTP to request a new one.",
                        "no_otp_found": "No OTP found for this email. Click Send OTP first.",
                        "too_many_attempts": "Too many failed attempts. Click Send OTP to request a new one.",
                        "no_license_found": "No active license found for this email. Visit cinexis.cloud to purchase.",
                    }
                    body = render_license_section(msgs.get(err, f"Verification failed: {err}"), "err") + render_voice_section()
                    self.send_html(400, page("Cinexis Setup", body, base_path=base))
            except Exception as e:
                body = render_license_section(f"Network error: {e}", "err") + render_voice_section()
                self.send_html(500, page("Cinexis Setup", body, base_path=base))

        elif path == "/license/clear":
            try:
                os.remove(LICENSE_KEY_FILE)
                log("License cleared via ingress UI")
            except FileNotFoundError:
                pass
            self.redirect_home()

        elif path == "/voice/toggle":
            try:
                body = json.loads(self.read_body())
                entity_id = body.get("entity_id", "")
                platform  = body.get("platform", "")
                enabled   = body.get("enabled", True)
                if not entity_id or platform not in PLATFORMS:
                    self.send_json(400, {"ok": False, "error": "invalid_params"})
                    return
                data = load_exclusions()
                if entity_id not in data:
                    data[entity_id] = {}
                # excluded=True means voice assistant can NOT see it
                data[entity_id][platform] = not enabled
                save_exclusions(data)
                self.send_json(200, {"ok": True})
            except Exception as e:
                self.send_json(500, {"ok": False, "error": str(e)})

        else:
            self.send_html(404, page("Not Found", "<p>Not found.</p>", base_path=base))


class ThreadedHTTPServer(http.server.ThreadingHTTPServer):
    pass


def main():
    token = get_supervisor_token()
    log(f"SUPERVISOR_TOKEN: {'set (' + str(len(token)) + ' chars)' if token else 'NOT SET — HA states will be empty'}")
    server = ThreadedHTTPServer(("0.0.0.0", PORT), IngressHandler)
    log(f"Ingress UI listening on port {PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
