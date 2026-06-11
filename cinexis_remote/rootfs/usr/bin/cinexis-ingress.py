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
import time
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
RECIPIENTS_FILE       = f"{STORAGE_DIR}/recipients.json"
AUTOMATION_MAP_FILE   = f"{STORAGE_DIR}/automation_recipient_map.json"
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

def _read_cached_license_key():
    """The local license_key cache lets the cloud auto-relink this node to
    its customer even if the customer row's ha_node_id column drifted."""
    try:
        with open(LICENSE_KEY_FILE) as f:
            return f.read().strip() or None
    except (FileNotFoundError, OSError):
        return None

def cinexis_addon_call(method, path, payload=None):
    """Call /api/addon/* with node credentials auto-attached. Returns parsed JSON or {'ok':False,...}."""
    node_id, secret = get_node_credentials()
    if not node_id or not secret:
        return {"ok": False, "error": "no_node_credentials"}
    body = {"node_id": node_id, "device_secret": secret}
    # Send the cached license_key so the cloud can auto-relink if needed.
    # Safe to expose since cloud only uses it for lookup, not auth.
    lk = _read_cached_license_key()
    if lk:
        body["license_key"] = lk
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

# Cached license-status fetcher. Hits /api/addon/status at most once per 30s
# so dashboard renders are snappy even on flaky links. The cloud already
# returns plan + entitlements + license_status, so the dashboard doesn't
# need to do any tier→feature mapping locally.
_STATUS_CACHE = {"data": None, "ts": 0}
def get_addon_status(force_fresh=False):
    """Returns the latest /api/addon/status payload.
    Cached 10s while in pending_approval (so admin-side license assignment
    reflects within ~10s) and 30s otherwise. force_fresh bypasses the cache
    — used right after a subscribe call so the UI reflects the new state."""
    nowts = time.time()
    if not force_fresh and _STATUS_CACHE["data"]:
        prev = _STATUS_CACHE["data"]
        ttl = 10 if prev.get("license_status") == "pending_approval" else 30
        if (nowts - _STATUS_CACHE["ts"]) < ttl:
            return prev
    data = cinexis_addon_call("GET", "/status") or {}
    if data.get("ok"):
        _STATUS_CACHE["data"] = data
        _STATUS_CACHE["ts"]   = nowts
    return data or {}

def render_locked_card(title, feature_label):
    """Replacement card shown when the current plan doesn't include a feature.
    Greys it out and opens the in-addon upgrade modal (no public URL leaked)."""
    return f"""
<div class="card" style="opacity:.55;position:relative;border:1px dashed rgba(148,163,184,.25)">
  <div style="position:absolute;top:14px;right:14px;font-size:1.4rem">🔒</div>
  <h2 style="margin:0 0 6px">{title}</h2>
  <p style="color:#94a3b8;font-size:.85rem;margin:0 0 12px">
    <strong>{feature_label}</strong> isn't included in your current plan.
  </p>
  <button onclick="openUpgradeModal()"
     style="border:none;cursor:pointer;display:inline-block;padding:8px 16px;border-radius:8px;
            background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;
            font-weight:600;font-size:.85rem">
    Upgrade plan →
  </button>
</div>
"""

def render_subscription_card(status, base_path="/"):
    """Always-visible card showing current plan + Upgrade/Change/Cancel actions.
    All actions are in-addon — no public URL exposure."""
    plan       = status.get("plan") or "—"
    billing    = status.get("billing_period") or "monthly"
    expires_at = status.get("expires_at")
    trial_end  = status.get("trial_ends_at")
    auto_renew = status.get("auto_renew", False)
    legacy     = status.get("legacy", False)
    lic_status = status.get("license_status") or "—"

    plan_pretty = {
        "ha-remote": "Lite (HA Remote)",
        "ha-voice":  "Pro (HA + Voice)",
        "smart":     "Smart",
        "ultimate":  "Ultimate",
        "enterprise":"Enterprise",
    }.get(plan, plan or "—")

    exp_label = ""
    if trial_end:
        exp_label = f"Trial ends: " + datetime.fromtimestamp(trial_end, tz=timezone.utc).astimezone().strftime("%d %b %Y")
    elif expires_at:
        exp_label = f"Renews: " + datetime.fromtimestamp(expires_at, tz=timezone.utc).astimezone().strftime("%d %b %Y")

    status_pill_color = {
        "active":   "#22c55e",
        "trial":    "#3b82f6",
        "pending_payment": "#f59e0b",
        "expired":  "#ef4444",
    }.get(lic_status, "#94a3b8")

    legacy_badge = '<span style="background:#fbbf24;color:#0b0e14;font-size:.7rem;padding:2px 8px;border-radius:6px;font-weight:700;margin-left:8px">⚡ legacy</span>' if legacy else ''
    autopay_badge = ('<span style="color:#22c55e;font-size:.75rem;margin-left:8px">↻ Auto-pay on</span>' if auto_renew else
                     '<span style="color:#64748b;font-size:.75rem;margin-left:8px">Auto-pay off</span>')

    return f"""
<div class="card" id="sub-card">
  <div class="card-header"><span class="card-icon">💎</span>Subscription &amp; Billing</div>
  <div style="display:flex;flex-wrap:wrap;gap:24px;align-items:center;margin:14px 0 18px">
    <div>
      <div style="font-size:.75rem;color:#94a3b8;text-transform:uppercase;letter-spacing:.05em">Current plan</div>
      <div style="font-size:1.15rem;font-weight:700;margin-top:2px">{plan_pretty}{legacy_badge}</div>
      <div style="font-size:.75rem;color:#94a3b8;margin-top:2px">Billing: {billing}{autopay_badge}</div>
    </div>
    <div>
      <div style="font-size:.75rem;color:#94a3b8;text-transform:uppercase;letter-spacing:.05em">Status</div>
      <div style="margin-top:2px"><span style="display:inline-block;padding:3px 12px;border-radius:6px;background:{status_pill_color}22;color:{status_pill_color};font-weight:700;font-size:.85rem;text-transform:capitalize">{lic_status.replace('_',' ')}</span></div>
      <div style="font-size:.78rem;color:#94a3b8;margin-top:4px">{exp_label}</div>
    </div>
  </div>
  <div style="display:flex;gap:10px;flex-wrap:wrap">
    <button onclick="openUpgradeModal()" style="padding:9px 18px;border-radius:8px;border:none;cursor:pointer;background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;font-weight:600;font-size:.88rem">
      💎 Upgrade / change plan
    </button>
    {('<button onclick="cancelAutopay()" style="padding:9px 14px;border-radius:8px;border:1px solid #2a2f3c;background:transparent;color:#94a3b8;cursor:pointer;font-size:.85rem">Cancel auto-pay</button>' if auto_renew else '')}
  </div>
  <p style="color:#64748b;font-size:.72rem;margin-top:14px;margin-bottom:0">
    Payments are processed directly by Razorpay (PCI DSS Level 1). Cinexis never stores your card or UPI details.
  </p>
</div>

<!-- In-addon Upgrade Modal — plan grid, NO public links -->
<div id="upgrade-modal" style="display:none;position:fixed;inset:0;background:rgba(8,12,20,.75);z-index:1000;align-items:flex-start;justify-content:center;padding:30px 20px;overflow-y:auto">
  <div style="background:#0d1220;border:1px solid #1e2d45;border-radius:14px;max-width:760px;width:100%;padding:24px;color:#e8edf5">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
      <h2 style="margin:0;font-size:1.25rem">Pick a plan</h2>
      <button onclick="closeUpgradeModal()" style="background:transparent;border:none;color:#94a3b8;font-size:1.4rem;cursor:pointer">×</button>
    </div>
    <p style="color:#94a3b8;font-size:.85rem;margin:0 0 18px">Switch tiers anytime. Razorpay handles payment — Cinexis never sees your card or UPI details.</p>
    <div id="up-billing-row" style="display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap">
      <button data-period="monthly"    onclick="setBillingPeriod(this)" class="up-period up-active">Monthly</button>
      <button data-period="quarterly"  onclick="setBillingPeriod(this)" class="up-period">Quarterly</button>
      <button data-period="halfyearly" onclick="setBillingPeriod(this)" class="up-period">Half-yearly</button>
      <button data-period="yearly"     onclick="setBillingPeriod(this)" class="up-period">Yearly</button>
    </div>
    <div id="up-plans-grid">
      <div style="text-align:center;padding:30px;color:#64748b">⏳ Loading plans…</div>
    </div>
    <div id="up-result" style="margin-top:14px;font-size:.85rem"></div>
  </div>
</div>

<style>
  .up-period {{ padding:7px 14px;border-radius:8px;border:1px solid #1e2d45;background:transparent;color:#94a3b8;font-size:.82rem;cursor:pointer }}
  .up-period.up-active {{ background:#6366f1;border-color:#6366f1;color:#fff;font-weight:600 }}
  .up-plan-card {{ background:#111827;border:1px solid #1e2d45;border-radius:10px;padding:16px;cursor:pointer;transition:all .15s }}
  .up-plan-card:hover {{ border-color:#6366f1;transform:translateY(-1px) }}
  .up-plan-card.up-current {{ border:2px dashed #22c55e;cursor:default }}
  .up-plan-card.up-highlight {{ border-color:#8b5cf6 }}
</style>

<script>
const SUB_BASE = '{base_path}';
let __upBilling = 'monthly';
let __upPlansCache = null;

function openUpgradeModal() {{
  document.getElementById('upgrade-modal').style.display = 'flex';
  loadUpgradePlans();
}}
function closeUpgradeModal() {{
  document.getElementById('upgrade-modal').style.display = 'none';
  document.getElementById('up-result').textContent = '';
}}
function setBillingPeriod(btn) {{
  document.querySelectorAll('.up-period').forEach(b => b.classList.remove('up-active'));
  btn.classList.add('up-active');
  __upBilling = btn.dataset.period;
  if (__upPlansCache) renderUpgradePlans(__upPlansCache);
}}
async function loadUpgradePlans() {{
  if (__upPlansCache) {{ renderUpgradePlans(__upPlansCache); return; }}
  try {{
    const r = await fetch(SUB_BASE + 'plans').then(r => r.json());
    if (!r.ok || !r.plans) {{
      document.getElementById('up-plans-grid').innerHTML = '<div style="color:#ef4444;padding:20px">Could not load plans. Please retry.</div>';
      return;
    }}
    __upPlansCache = r.plans;
    renderUpgradePlans(__upPlansCache);
  }} catch(e) {{
    document.getElementById('up-plans-grid').innerHTML = '<div style="color:#ef4444;padding:20px">Network error loading plans.</div>';
  }}
}}
function renderUpgradePlans(plans) {{
  const currentPlan    = {json.dumps(plan)};
  const currentBilling = {json.dumps(billing)};
  const licStatus      = {json.dumps(lic_status)};
  const isPaid         = (licStatus === 'active');   // trial / pending_payment / expired are NOT paid → must allow payment
  const grid = document.getElementById('up-plans-grid');
  const cards = plans.map(p => {{
    const price = p.prices && p.prices[__upBilling];
    const isSamePlan = (p.slug === currentPlan);
    // Only the EXACT current PAID plan+period is a terminal "current" state.
    // On trial/pending/expired you can still pay to activate, and even when
    // active you can switch billing period on the same tier (e.g. monthly→yearly).
    const isExactCurrent = isPaid && isSamePlan && (__upBilling === currentBilling);
    const cls = 'up-plan-card' + (isExactCurrent ? ' up-current' : '') + (p.highlight ? ' up-highlight' : '');
    const priceHtml = price
      ? '<div style="margin:6px 0 4px"><span style="font-size:1.3rem;font-weight:700">₹' + price.amount_inr.toLocaleString('en-IN') + '</span><span style="font-size:.8rem;color:#94a3b8"> / ' + __upBilling + '</span></div>'
      : '<div style="color:#94a3b8;font-size:.85rem;margin:6px 0">Price unavailable for this billing period</div>';
    let label = 'Pick this plan';
    if (isSamePlan && !isPaid)      label = 'Activate this plan';                       // trial/expired → pay to start
    else if (isSamePlan && isPaid)  label = 'Switch to ' + __upBilling + ' billing';    // same tier, change period
    const btn = isExactCurrent
      ? '<div style="margin-top:10px;color:#22c55e;font-size:.82rem;font-weight:600">✓ Your current plan</div>'
      : (price
          ? '<button onclick="subscribeTo(\\''+p.slug+'\\')" style="margin-top:10px;width:100%;padding:9px;border:none;border-radius:7px;background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;font-weight:600;cursor:pointer">' + label + '</button>'
          : '<div style="margin-top:10px;color:#64748b;font-size:.78rem">Price unavailable for ' + __upBilling + ' billing</div>');
    const feats = (p.features || []).slice(0, 5).map(f => '<li style="font-size:.78rem;color:#94a3b8;margin:2px 0">• ' + f + '</li>').join('');
    return '<div class="'+cls+'"><div style="font-size:1.05rem;font-weight:700">'+p.name+'</div>'+priceHtml+'<ul style="margin:8px 0 0;padding:0;list-style:none">'+feats+'</ul>'+btn+'</div>';
  }});
  grid.style.display = 'grid';
  grid.style.gridTemplateColumns = 'repeat(auto-fit,minmax(220px,1fr))';
  grid.style.gap = '12px';
  grid.innerHTML = cards.join('');
}}
async function subscribeTo(planSlug) {{
  const out = document.getElementById('up-result');
  out.style.color = '#94a3b8';
  out.textContent = '⏳ Creating subscription with Razorpay…';
  try {{
    const r = await fetch(SUB_BASE + 'subscribe', {{
      method: 'POST',
      headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{ plan_slug: planSlug, billing_period: __upBilling }}),
    }}).then(r => r.json());
    if (r.ok && r.short_url) {{
      out.style.color = '#22c55e';
      out.innerHTML = '✅ Subscription ready. <a href="'+r.short_url+'" target="_blank" rel="noopener" style="color:#818cf8;text-decoration:underline">Open Razorpay to complete payment →</a><br><span style="color:#94a3b8;font-size:.78rem">Your plan activates the moment payment succeeds. This page refreshes automatically.</span>';
      // open Razorpay-hosted checkout — never our domain
      window.open(r.short_url, '_blank', 'noopener,noreferrer');
      // Poll for status flip every 5s
      setTimeout(function pollStatus() {{
        fetch(SUB_BASE + 'wizard/status').then(r => r.json()).then(s => {{
          if (s.plan === planSlug && s.license_status === 'active') {{ location.reload(); }}
          else setTimeout(pollStatus, 5000);
        }}).catch(() => setTimeout(pollStatus, 5000));
      }}, 5000);
    }} else {{
      out.style.color = '#ef4444';
      out.textContent = '❌ ' + (r.error || 'Could not create subscription');
    }}
  }} catch(e) {{
    out.style.color = '#ef4444';
    out.textContent = '❌ Network error: ' + e.message;
  }}
}}
async function cancelAutopay() {{
  if (!confirm('Cancel auto-renewal? You keep access until your current billing cycle ends — no immediate charge.')) return;
  const r = await fetch(SUB_BASE + 'subscribe/cancel', {{method:'POST'}}).then(r => r.json()).catch(() => ({{ok:false,error:'network'}}));
  alert(r.ok ? 'Auto-renewal cancelled. You keep access until ' + (r.expires_at_label || 'cycle end') + '.' : 'Could not cancel: ' + (r.error || 'unknown'));
  if (r.ok) setTimeout(() => location.reload(), 800);
}}
</script>
"""

def render_pending_banner():
    """Shown when license_status='pending_approval' — replaces the whole dashboard."""
    return """
<div class="card" style="text-align:center;padding:48px 24px">
  <div style="font-size:3rem;margin-bottom:12px">⏳</div>
  <h2 style="margin:0 0 8px">Waiting for admin approval</h2>
  <p style="color:#94a3b8;max-width:480px;margin:0 auto 16px;line-height:1.5">
    Your registration was received. Our team is reviewing your account
    and will activate your trial within 24 hours. You'll get a WhatsApp
    message the moment it's ready.
  </p>
  <p style="color:#64748b;font-size:.8rem">
    Nothing for you to do here — this page refreshes every 15 seconds.
  </p>
</div>
<script>setTimeout(function(){ location.reload(); }, 15000);</script>
"""

def load_recipients():
    """Recipients book — list of { id, name, channel, address, enabled }.
    channel ∈ {'whatsapp','telegram'}. address = E.164 phone for WA, chat_id
    string for Telegram. Used by /notify to fan-out HA notifications without
    leaking individual phone numbers into HA's configuration.yaml."""
    try:
        with open(RECIPIENTS_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def save_recipients(recipients):
    os.makedirs(STORAGE_DIR, exist_ok=True)
    with open(RECIPIENTS_FILE, "w") as f:
        json.dump(recipients, f, indent=2)

def load_automation_map():
    """Per-automation default recipient picks. Keyed by HA automation
    entity_id (e.g. 'automation.front_door_at_night'), value is a list
    of recipient names. The rest_command body in HA passes the automation
    entity_id and /notify resolves recipients from this map if `to` is
    omitted."""
    try:
        with open(AUTOMATION_MAP_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_automation_map(m):
    os.makedirs(STORAGE_DIR, exist_ok=True)
    with open(AUTOMATION_MAP_FILE, "w") as f:
        json.dump(m, f, indent=2)

# ── Home Assistant core API helper ────────────────────────────────────────────
def ha_api_call(method, path, payload=None, timeout=12):
    """Call HA's core REST API via the Supervisor proxy. Returns
    (ok, parsed_or_text). Used by the Notification Designer to read the entity
    list, render message templates, and write automations."""
    token = get_supervisor_token()
    if not token:
        return False, {"error": "no_supervisor_token"}
    url = "http://supervisor/core/api" + path
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            try:    return True, json.loads(raw)
            except Exception: return True, raw
    except urllib.error.HTTPError as e:
        body = ""
        try: body = e.read().decode()
        except Exception: pass
        return False, {"error": f"http_{e.code}", "body": body[:300]}
    except Exception as e:
        return False, {"error": str(e)}

DESIGNER_FILE = f"{STORAGE_DIR}/designer_automations.json"

def load_designer_automations():
    """Automations created by the Notification Designer — list of
    { id, name, entity, to_state, recipients[], message, image_entity }."""
    try:
        with open(DESIGNER_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def save_designer_automations(items):
    os.makedirs(STORAGE_DIR, exist_ok=True)
    with open(DESIGNER_FILE, "w") as f:
        json.dump(items, f, indent=2)

def render_designer_section(base_path="/"):
    """Notification Designer — a no-YAML visual builder. Pick a trigger, pick
    recipients, compose the message, attach a camera, see a LIVE WhatsApp
    preview (real values + real snapshot), and save → writes the HA automation
    under the hood. The 10x feature."""
    recipients = [r for r in load_recipients() if r.get("enabled", True)]
    rec_json = json.dumps([{"name": r.get("name",""), "channel": r.get("channel","whatsapp")} for r in recipients])
    has_recipients = len(recipients) > 0

    tmpl = """
<div class="card" id="designer-card">
  <div class="card-header"><span class="card-icon">🎨</span>Notification Designer <span style="font-size:.66rem;background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;padding:2px 8px;border-radius:6px;margin-left:8px;vertical-align:middle">no YAML</span></div>
  <p class="muted small">Build a notification visually — pick when it fires, who it goes to, what it says, and see the real WhatsApp message <strong>before</strong> you save. We write the Home Assistant automation for you.</p>

  __NORECIP__

  <div id="dz-builder" style="__BUILDERHIDE__">
    <!-- ① Trigger -->
    <div class="dz-step">
      <div class="dz-step-label">① When this happens</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        <select id="dz-entity" class="dz-input" style="flex:2 1 240px"><option value="">⏳ loading devices…</option></select>
        <span class="muted small">becomes</span>
        <select id="dz-state" class="dz-input" style="flex:1 1 120px"><option value="">—</option></select>
      </div>
    </div>

    <!-- ② Recipients -->
    <div class="dz-step">
      <div class="dz-step-label">② Notify these people</div>
      <div id="dz-recipients" style="display:flex;gap:8px;flex-wrap:wrap"></div>
    </div>

    <!-- ③ Message -->
    <div class="dz-step">
      <div class="dz-step-label">③ Message</div>
      <textarea id="dz-message" class="dz-input" rows="2" style="width:100%;resize:vertical" placeholder="🚨 Front door opened at {{ now().strftime('%I:%M %p') }}"></textarea>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px">
        <button type="button" class="dz-chip" data-token="{{ now().strftime('%I:%M %p') }}">🕐 Time</button>
        <button type="button" class="dz-chip" data-token="{{ now().strftime('%d %b') }}">📅 Date</button>
        <button type="button" class="dz-chip" data-token="{{ states('ENTITY') }}">📊 Its state</button>
        <button type="button" class="dz-chip" data-token="{{ state_attr('ENTITY','friendly_name') }}">🏠 Device name</button>
      </div>
    </div>

    <!-- ④ Camera -->
    <div class="dz-step">
      <div class="dz-step-label">④ Attach a camera snapshot <span class="muted small">(optional)</span></div>
      <select id="dz-camera" class="dz-input" style="max-width:320px"><option value="">— no snapshot —</option></select>
    </div>

    <!-- Live preview -->
    <div class="dz-step" style="background:var(--bg);border:1px dashed #2a2f3c;border-radius:10px;padding:12px 14px">
      <div class="dz-step-label">🔍 Live preview — see the real message on WhatsApp</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        <span class="muted small">send a preview to</span>
        <select id="dz-preview-to" class="dz-input" style="flex:1 1 150px"></select>
        <button type="button" class="btn btn-ghost btn-sm" id="dz-preview-btn" onclick="dzPreview()">📤 Send preview</button>
      </div>
      <div id="dz-preview-result" class="muted small" style="margin-top:8px"></div>
    </div>

    <div style="display:flex;gap:10px;align-items:center;margin-top:6px;flex-wrap:wrap">
      <input id="dz-name" class="dz-input" placeholder="Name this notification (e.g. Front door at night)" style="flex:1 1 240px">
      <button type="button" class="btn btn-primary" onclick="dzSave()">💾 Save &amp; activate</button>
    </div>
    <div id="dz-save-result" style="margin-top:8px;font-size:.85rem"></div>
    <div id="dz-rc-warn" class="muted small" style="margin-top:6px;display:none;color:#fbbf24"></div>
  </div>

  <!-- Existing -->
  <div id="dz-existing" style="margin-top:16px"></div>
</div>

<style>
  .dz-step{margin:14px 0}
  .dz-step-label{font-size:.8rem;font-weight:700;color:var(--text2);margin-bottom:7px}
  .dz-input{background:var(--bg);border:1px solid #2a2f3c;border-radius:8px;color:var(--text);padding:9px 11px;font-size:.86rem;font-family:inherit;outline:none}
  .dz-input:focus{border-color:#6366f1}
  .dz-chip{background:var(--surface2,#162032);border:1px solid #2a2f3c;border-radius:7px;color:var(--text2);font-size:.76rem;padding:5px 10px;cursor:pointer}
  .dz-chip:hover{border-color:#6366f1;color:var(--text)}
  .dz-rec{display:inline-flex;align-items:center;gap:6px;padding:6px 12px;border-radius:8px;border:1px solid #2a2f3c;cursor:pointer;font-size:.83rem;user-select:none}
  .dz-rec.on{background:rgba(99,102,241,.14);border-color:#6366f1;color:#c7d2fe}
  .dz-auto{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 12px;background:var(--bg);border:1px solid #1e2d45;border-radius:9px;margin-bottom:8px}
</style>

<script>
const DZ_BASE = '__BASE__';
const DZ_RECIPIENTS = __RECIPIENTS__;
let DZ_ENT = {};   // entity_id -> {target_states, friendly}

function dzEsc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

async function dzInit(){
  // Recipients (checkboxes + preview dropdown)
  const recWrap = document.getElementById('dz-recipients');
  const prevSel = document.getElementById('dz-preview-to');
  recWrap.innerHTML = DZ_RECIPIENTS.map(r =>
    '<label class="dz-rec"><input type="checkbox" class="dz-rcheck" value="'+dzEsc(r.name)+'" style="display:none" onchange="this.closest(\\'label\\').classList.toggle(\\'on\\',this.checked)">'+
    (r.channel==='telegram'?'💬':'📱')+' '+dzEsc(r.name)+'</label>').join('');
  prevSel.innerHTML = DZ_RECIPIENTS.map(r=>'<option value="'+dzEsc(r.name)+'">'+dzEsc(r.name)+'</option>').join('');

  // Entities + cameras
  try{
    const d = await fetch(DZ_BASE+'designer/entities').then(r=>r.json());
    if(d.ok){
      const sel=document.getElementById('dz-entity'); sel.innerHTML='<option value="">— pick a device —</option>';
      const DOMNAMES={binary_sensor:'Sensors',person:'People',device_tracker:'Devices',lock:'Locks',cover:'Covers/Blinds',switch:'Switches',input_boolean:'Toggles',alarm_control_panel:'Alarm',sun:'Sun',climate:'Climate'};
      Object.keys(d.groups||{}).sort().forEach(dom=>{
        const og=document.createElement('optgroup'); og.label=DOMNAMES[dom]||dom;
        d.groups[dom].forEach(e=>{ DZ_ENT[e.entity_id]=e;
          const o=document.createElement('option'); o.value=e.entity_id; o.textContent=e.friendly_name+'  ('+e.state+')'; og.appendChild(o); });
        sel.appendChild(og);
      });
      const cam=document.getElementById('dz-camera');
      (d.cameras||[]).forEach(c=>{const o=document.createElement('option');o.value=c.entity_id;o.textContent=c.friendly_name;cam.appendChild(o);});
    } else {
      document.getElementById('dz-entity').innerHTML='<option value="">⚠ Can\\'t reach Home Assistant</option>';
    }
  }catch(e){ document.getElementById('dz-entity').innerHTML='<option value="">⚠ error loading devices</option>'; }

  // rest_command readiness
  try{ const c=await fetch(DZ_BASE+'designer/check').then(r=>r.json());
    if(c.ok && !c.rest_command_ready){ const w=document.getElementById('dz-rc-warn'); w.style.display='block';
      w.innerHTML='⚠ One-time setup needed: paste the <strong>rest_command.cinexis_notify</strong> snippet from the “Use from Home Assistant” card below into your configuration.yaml &amp; restart HA. Until then, saving will give you the automation YAML to paste manually.'; }
  }catch(e){}

  dzLoadExisting();
}

document.getElementById('dz-entity')?.addEventListener('change',function(){
  const e=DZ_ENT[this.value]; const st=document.getElementById('dz-state');
  st.innerHTML = e ? e.target_states.map(s=>'<option value="'+s+'">'+s+'</option>').join('') : '<option value="">—</option>';
});
document.querySelectorAll('.dz-chip').forEach(b=>b.addEventListener('click',function(){
  const ent=document.getElementById('dz-entity').value||'YOUR_DEVICE';
  const ta=document.getElementById('dz-message');
  ta.value += (ta.value && !ta.value.endsWith(' ')?' ':'') + this.dataset.token.replace(/ENTITY/g,ent);
  ta.focus();
}));

function dzSelectedRecipients(){return [...document.querySelectorAll('.dz-rcheck:checked')].map(c=>c.value);}

async function dzPreview(){
  const out=document.getElementById('dz-preview-result'); const btn=document.getElementById('dz-preview-btn');
  const msg=document.getElementById('dz-message').value.trim();
  const to=document.getElementById('dz-preview-to').value;
  const cam=document.getElementById('dz-camera').value;
  if(!msg){out.style.color='#ef4444';out.textContent='Write a message first.';return;}
  btn.disabled=true; out.style.color='var(--text3)'; out.textContent='Sending preview…';
  try{
    const r=await fetch(DZ_BASE+'designer/preview',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:msg,to:to,image_entity:cam})}).then(r=>r.json());
    if(r.ok){out.style.color='#22c55e';out.innerHTML='✅ Preview sent to '+dzEsc(to)+' — check WhatsApp.'+(r.rendered?'<br><span style="color:#94a3b8">Rendered: “'+dzEsc(r.rendered)+'”</span>':'');}
    else{out.style.color='#ef4444';out.textContent='❌ '+(r.error||'failed (is WhatsApp paired?)');}
  }catch(e){out.style.color='#ef4444';out.textContent='❌ '+e.message;}
  btn.disabled=false;
}

async function dzSave(){
  const out=document.getElementById('dz-save-result');
  const name=document.getElementById('dz-name').value.trim();
  const entity=document.getElementById('dz-entity').value;
  const to_state=document.getElementById('dz-state').value;
  const recipients=dzSelectedRecipients();
  const message=document.getElementById('dz-message').value.trim();
  const image_entity=document.getElementById('dz-camera').value;
  if(!name||!entity||!to_state||!recipients.length||!message){
    out.style.color='#ef4444';out.textContent='❌ Fill in: a name, the trigger device + state, at least one recipient, and a message.';return;}
  out.style.color='var(--text3)';out.textContent='Saving…';
  try{
    const r=await fetch(DZ_BASE+'designer/save',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name,entity,to_state,recipients,message,image_entity})}).then(r=>r.json());
    if(r.ok && r.ha_written){out.style.color='#22c55e';out.textContent='✅ Saved & active! It will fire automatically from now on.';dzLoadExisting();document.getElementById('dz-name').value='';}
    else if(r.ok && r.fallback_yaml){out.style.color='#fbbf24';
      out.innerHTML='⚠ Saved, but HA wouldn\\'t let us write the automation automatically. Paste this into your automations and reload:<pre style="background:var(--bg);padding:10px;border-radius:8px;margin-top:8px;font-size:.74rem;overflow-x:auto;white-space:pre-wrap">'+dzEsc(r.fallback_yaml)+'</pre>';dzLoadExisting();}
    else{out.style.color='#ef4444';out.textContent='❌ '+(r.error||'failed');}
  }catch(e){out.style.color='#ef4444';out.textContent='❌ '+e.message;}
}

async function dzLoadExisting(){
  try{
    const d=await fetch(DZ_BASE+'designer/list').then(r=>r.json());
    const wrap=document.getElementById('dz-existing');
    if(!d.automations||!d.automations.length){wrap.innerHTML='';return;}
    wrap.innerHTML='<div class="dz-step-label" style="margin-top:4px">Your notifications ('+d.automations.length+')</div>'+
      d.automations.map(a=>'<div class="dz-auto"><div><strong>'+dzEsc(a.name)+'</strong>'+
        (a.ha_written?'':' <span style="color:#fbbf24;font-size:.7rem">(manual)</span>')+
        '<div class="muted small">'+dzEsc(a.entity)+' → '+dzEsc(a.to_state)+' · to '+dzEsc((a.recipients||[]).join(', '))+(a.image_entity?' · 📷':'')+'</div></div>'+
        '<button class="btn btn-danger btn-sm" onclick="dzDelete(\\''+dzEsc(a.id)+'\\')">🗑</button></div>').join('');
  }catch(e){}
}
async function dzDelete(id){
  if(!confirm('Delete this notification?'))return;
  await fetch(DZ_BASE+'designer/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
  dzLoadExisting();
}
dzInit();
</script>
"""
    norecip = ('<div class="wa-status-row waiting" style="margin:10px 0"><div class="dot"></div>'
               '<div>Add people in the <strong>Notification Recipients</strong> card first, then design notifications for them here.</div></div>') if not has_recipients else ''
    return (tmpl
            .replace("__BASE__", base_path)
            .replace("__RECIPIENTS__", rec_json)
            .replace("__NORECIP__", norecip)
            .replace("__BUILDERHIDE__", "" if has_recipients else "opacity:.45;pointer-events:none"))

def render_recipients_section(base_path="/"):
    """Recipients book — customer manages WA + Telegram contacts here,
    then HA automations reference them by name (no phone numbers in
    configuration.yaml)."""
    recipients = load_recipients()
    rows = ""
    if recipients:
        for r in recipients:
            channel_icon = "📱" if r.get("channel") == "whatsapp" else "💬"
            enabled = r.get("enabled", True)
            enabled_pill = ('<span style="background:#22c55e22;color:#22c55e;padding:2px 8px;border-radius:6px;font-size:.72rem;font-weight:600">enabled</span>'
                            if enabled else
                            '<span style="background:#64748b22;color:#94a3b8;padding:2px 8px;border-radius:6px;font-size:.72rem">disabled</span>')
            rows += f"""
<tr id="r-{esc_html(r.get('id',''))}">
  <td style="padding:8px 4px">{channel_icon} <strong>{esc_html(r.get('name',''))}</strong></td>
  <td style="padding:8px 4px;color:#94a3b8;font-family:monospace;font-size:.85rem">{esc_html(r.get('address',''))}</td>
  <td style="padding:8px 4px">{enabled_pill}</td>
  <td style="padding:8px 4px;text-align:right">
    <button class="btn btn-ghost btn-sm" onclick="testRecipient('{esc_html(r.get('id',''))}')">📨 Test</button>
    <button class="btn btn-ghost btn-sm" onclick="toggleRecipient('{esc_html(r.get('id',''))}')">{('Disable' if enabled else 'Enable')}</button>
    <button class="btn btn-danger btn-sm" onclick="removeRecipient('{esc_html(r.get('id',''))}')">🗑</button>
  </td>
</tr>"""
    else:
        rows = '<tr><td colspan="4" style="text-align:center;color:#64748b;padding:30px;font-size:.85rem">No recipients yet. Add Dad / Mom / Family group below so HA automations can reference them by name.</td></tr>'

    return f"""
<div class="card" id="recipients-card">
  <div class="card-header"><span class="card-icon">📇</span>Notification Recipients</div>
  <p class="muted small" style="margin-bottom:14px">
    Add WhatsApp numbers and Telegram chat IDs by friendly name. HA automations reference them as
    <code style="background:#1a1f2e;padding:2px 6px;border-radius:4px">to: ["Dad","Mom"]</code> — no phone numbers in your configuration.yaml.
  </p>
  <div style="overflow-x:auto">
    <table style="width:100%;border-collapse:collapse">
      <thead><tr style="border-bottom:1px solid #1e2d45;color:#94a3b8;font-size:.75rem;text-transform:uppercase;letter-spacing:.05em">
        <th style="text-align:left;padding:8px 4px">Name</th>
        <th style="text-align:left;padding:8px 4px">Address</th>
        <th style="text-align:left;padding:8px 4px">State</th>
        <th style="text-align:right;padding:8px 4px">Actions</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>

  <details style="margin-top:14px">
    <summary style="cursor:pointer;color:#818cf8;font-weight:600;font-size:.88rem">+ Add a recipient</summary>
    <div style="background:#0d1220;border:1px solid #1e2d45;border-radius:10px;padding:14px;margin-top:10px">
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">
        <div style="flex:1 1 130px"><label style="display:block;font-size:.75rem;color:#94a3b8;margin-bottom:3px">Name</label>
          <input id="r-name" placeholder="e.g. Dad" style="width:100%;padding:8px;border:1px solid #2a2f3c;background:var(--bg);border-radius:6px;color:#e8edf5"/>
        </div>
        <div style="flex:0 0 130px"><label style="display:block;font-size:.75rem;color:#94a3b8;margin-bottom:3px">Channel</label>
          <select id="r-channel" style="width:100%;padding:8px;border:1px solid #2a2f3c;background:var(--bg);border-radius:6px;color:#e8edf5">
            <option value="whatsapp">📱 WhatsApp</option>
            <option value="telegram">💬 Telegram</option>
          </select>
        </div>
        <div style="flex:1 1 190px"><label style="display:block;font-size:.75rem;color:#94a3b8;margin-bottom:3px">Address (phone or chat_id)</label>
          <input id="r-addr" placeholder="919876543210 or -1001234567" style="width:100%;padding:8px;border:1px solid #2a2f3c;background:var(--bg);border-radius:6px;color:#e8edf5;font-family:monospace"/>
        </div>
      </div>
      <button onclick="addRecipient()" class="btn btn-primary btn-sm">Add recipient</button>
      <div id="r-result" style="margin-top:8px;font-size:.82rem"></div>
    </div>
  </details>
</div>

<script>
const REC_BASE = '{base_path}';
async function addRecipient() {{
  const name    = document.getElementById('r-name').value.trim();
  const channel = document.getElementById('r-channel').value;
  const addr    = document.getElementById('r-addr').value.trim();
  const out     = document.getElementById('r-result');
  if (!name || !addr) {{ out.style.color = '#ef4444'; out.textContent = 'Name and address are both required'; return; }}
  out.style.color = '#94a3b8';
  out.textContent = 'Saving…';
  const r = await fetch(REC_BASE + 'recipients/add', {{ method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{ name, channel, address: addr }}) }}).then(r=>r.json());
  if (r.ok) {{ location.reload(); }}
  else {{ out.style.color = '#ef4444'; out.textContent = '❌ ' + (r.error || 'failed'); }}
}}
async function removeRecipient(id) {{
  if (!confirm('Remove this recipient? HA automations using their name will fail to deliver.')) return;
  await fetch(REC_BASE + 'recipients/' + encodeURIComponent(id) + '/remove', {{ method:'POST' }});
  document.getElementById('r-' + id)?.remove();
}}
async function toggleRecipient(id) {{
  await fetch(REC_BASE + 'recipients/' + encodeURIComponent(id) + '/toggle', {{ method:'POST' }});
  location.reload();
}}
async function testRecipient(id) {{
  const r = await fetch(REC_BASE + 'recipients/' + encodeURIComponent(id) + '/test', {{ method:'POST' }}).then(r=>r.json()).catch(()=>({{ok:false,error:'network'}}));
  alert(r.ok ? '✅ Test sent. Check the recipient\\'s phone.' : '❌ ' + (r.error || 'failed'));
}}
</script>
"""

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

def tg_send_test(chat_id):
    """Send a one-shot Telegram test message to a chat_id."""
    text = f"🧪 Cinexis test message at {datetime.now(timezone.utc).astimezone().strftime('%I:%M %p %d %b')} — your addon's Telegram bot can reach this chat."
    r = telegram_api("sendMessage", {"chat_id": chat_id, "text": text})
    return {"ok": bool(r.get("ok")), "error": r.get("description") or r.get("error")} if not r.get("ok") else {"ok": True}

def wa_service_call(method, path, payload=None):
    """Proxy a request to the local Baileys service (cinexis-wa.js on 127.0.0.1:18083).
    Returns parsed JSON or an {ok:False, error:...} stub on network failure.
    The WA service now binds 0.0.0.0 and guards mutating endpoints with a
    shared secret (= the node device_secret) — attach it so our proxy calls
    aren't rejected.
    """
    url = WA_SERVICE_URL + path
    headers = {"Content-Type": "application/json"}
    try:
        _, secret = get_node_credentials()
        if secret:
            headers["x-cinexis-secret"] = secret
    except Exception:
        pass
    try:
        if method == "GET":
            req = urllib.request.Request(url, method="GET", headers=headers)
        else:
            data = json.dumps(payload or {}).encode()
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
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
    'Use from Home Assistant' card — one clean rest_command + a notify
    wrapper. HA automations reference recipients by NAME (set in the
    Recipients card above). No phone numbers in configuration.yaml.
    """
    # The WA service now requires a shared secret (= this node's device_secret)
    # on /notify. Template it into the generated snippet so the customer's
    # copy-paste works out of the box. It's their own secret in their own
    # config — never leaves their HA.
    try:
        _, _secret = get_node_credentials()
    except Exception:
        _secret = ""
    secret_line = f"      'secret':        '{_secret}',\n" if _secret else ""
    return f"""
<details class="card" id="ha-int-card">
  <summary style="cursor:pointer;padding:16px 18px;font-weight:700;font-size:.95rem;list-style:none">
    ⚙️ Advanced — raw configuration.yaml (for power users)
    <div style="font-weight:400;font-size:.78rem;color:var(--text3);margin-top:4px">
      You don't need this. Build notifications with the 🎨 <strong>Notification Designer</strong> above —
      no YAML. This is only here if you want to call the addon directly from your own automations.
    </div>
  </summary>
  <div style="padding:0 18px 18px">
  <p class="muted small">
    One <code>rest_command.cinexis_notify</code> + a <code>notify.cinexis_addon</code> wrapper. HA automations
    pass <strong>recipient names</strong> (defined in the Recipients card above) — never phone numbers in YAML.
    Supports text, camera snapshots, images, videos, documents, and fan-out to multiple recipients in one call.
  </p>

  <h4 style="margin-top:14px;font-size:.95rem">1. configuration.yaml</h4>
  <pre id="ha-snippet" style="background:var(--bg);padding:14px;border-radius:8px;font-size:.78rem;overflow-x:auto;line-height:1.55"># Cinexis Remote Access — single rest_command + notify wrapper.
# Recipient names live in the addon's Recipients book; HA automations
# just reference them. The 'secret' below authenticates HA to your addon's
# WhatsApp service — it's your node's device secret, keep configuration.yaml
# private (use !secret if you prefer).

rest_command:
  cinexis_notify:
    url: "http://homeassistant.local.hass.io:18083/notify"
    method: POST
    content_type: "application/json"
    # Required: to + message. Optional: image_url, image_entity (HA
    # camera entity to snapshot), video_url, document_url, document_name,
    # automation_id (lets per-automation defaults from the addon UI win
    # when `to` is omitted).
    payload: >-
      {{{{ {{
        'to':            to            | default(['all']),
        'message':       message       | default(''),
        'image_url':     image_url     | default(none),
        'image_entity':  image_entity  | default(none),
        'video_url':     video_url     | default(none),
        'document_url':  document_url  | default(none),
        'document_name': document_name | default(none),
        'automation_id': automation_id | default(none),
{secret_line}      }} | to_json }}}}

# Optional: expose it as a regular notify service so the GUI automation
# editor lists it as `notify.cinexis_addon`. The ?secret=... authenticates
# HA to your addon (same device secret as above).
notify:
  - name: cinexis_addon
    platform: rest
    resource: "http://homeassistant.local.hass.io:18083/notify?secret={_secret}"
    method: POST_JSON
    message_param_name: message
    target_param_name: to
    # Optional data the action editor can pass through:
    # data:
    #   image_entity: camera.front_door
</pre>
  <button class="btn btn-primary" onclick="copyHa('ha-snippet')" style="margin-top:6px">📋 Copy configuration.yaml snippet</button>

  <h4 style="margin-top:22px;font-size:.95rem">2. Examples</h4>

  <p class="muted small" style="margin-top:14px"><strong>A) Front door opens at night — fan-out + camera snapshot</strong></p>
  <pre id="ha-ex1" style="background:var(--bg);padding:14px;border-radius:8px;font-size:.78rem;overflow-x:auto;line-height:1.55">- alias: Front door at night
  triggers:
    - trigger: state
      entity_id: binary_sensor.front_door
      to: "on"
  conditions:
    - condition: time
      after: "22:00:00"
      before: "06:00:00"
  actions:
    - service: rest_command.cinexis_notify
      data:
        to: ["Dad", "Mom"]                # recipient names from the addon book
        message: "🚨 Front door opened at {{{{ now().strftime('%H:%M:%S') }}}}"
        image_entity: camera.front_door    # addon snapshots this and attaches
        automation_id: automation.front_door_at_night</pre>
  <button class="btn btn-primary btn-sm" onclick="copyHa('ha-ex1')" style="margin-top:6px">📋 Copy</button>

  <p class="muted small" style="margin-top:18px"><strong>B) Boundary alert — ALL recipients, no media</strong></p>
  <pre id="ha-ex2" style="background:var(--bg);padding:14px;border-radius:8px;font-size:.78rem;overflow-x:auto;line-height:1.55">- service: rest_command.cinexis_notify
  data:
    to: "all"             # everyone enabled in the Recipients book
    message: "⚡ Power outage detected. Generator started automatically."</pre>
  <button class="btn btn-primary btn-sm" onclick="copyHa('ha-ex2')" style="margin-top:6px">📋 Copy</button>

  <p class="muted small" style="margin-top:18px"><strong>C) Maintenance reminder — only Telegram users</strong></p>
  <pre id="ha-ex3" style="background:var(--bg);padding:14px;border-radius:8px;font-size:.78rem;overflow-x:auto;line-height:1.55">- service: rest_command.cinexis_notify
  data:
    to: "all_telegram"
    message: "🔧 Filter due for replacement (last changed 90 days ago)"
    document_url: "https://your-cdn/maintenance-log.pdf"
    document_name: "maintenance-log.pdf"</pre>
  <button class="btn btn-primary btn-sm" onclick="copyHa('ha-ex3')" style="margin-top:6px">📋 Copy</button>

  <h4 style="margin-top:22px;font-size:.95rem">3. Or via the GUI automation editor</h4>
  <ol class="muted small" style="margin-top:8px;line-height:1.7">
    <li>HA → <strong>Settings → Automations &amp; Scenes → + Create Automation</strong></li>
    <li>Pick your trigger (state change, time pattern, event…)</li>
    <li>Action: <strong>Call service → notify.cinexis_addon</strong></li>
    <li>Set <code>message</code>, set <code>target</code> = recipient names (or "all"). Optionally add
        <code>image_entity</code> in the data section to attach a live camera snapshot.</li>
  </ol>

  <p class="muted small" style="margin-top:16px;border-top:1px solid #2a2f3c;padding-top:14px">
    <strong>Tip:</strong> set per-automation defaults so each automation routes to the right people
    automatically — pass <code>automation_id</code> in the call and the addon will fall back to the
    recipient list saved for that automation_id (in /share/cinexis/automation_recipient_map.json).
    HA's <code>{{{{ trigger.id }}}}</code> or <code>automation.&lt;name&gt;</code> works.
  </p>
  </div>
</details>
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
      body.innerHTML = '<div class="wa-status-row error"><div class="dot"></div><div>'+
        '<div style="font-weight:700">WhatsApp service is starting / recovering…</div>'+
        '<div class="muted small" style="margin-top:4px">This can take up to a minute after an addon update or restart — it retries automatically. '+
        'Telegram and the Notification Designer work meanwhile. If it stays here for several minutes, open the addon\\'s <strong>Log</strong> tab '+
        '(or append <code>/diag</code> to this URL) and share the WhatsApp lines so we can pinpoint it, then restart the addon.</div>'+
        '</div></div>';
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

            # Pull license + entitlements from the cloud. The cloud is the
            # source of truth for what's unlocked; we never decide locally.
            status = get_addon_status()
            lic_status   = status.get("license_status")
            entitlements = status.get("entitlements") or {}

            # Pending-approval gate — show the waiting banner and nothing else.
            if lic_status == "pending_approval":
                self.send_html(200, page("Cinexis — Pending approval",
                    render_pending_banner(), base_path=base))
                return

            # Always-visible Subscription / Billing card (current plan,
            # Upgrade button, Cancel autopay). All actions are in-addon —
            # no public cinexis.cloud URLs exposed.
            sub   = render_subscription_card(status, base_path=base)
            lic   = render_license_section()
            recs  = render_recipients_section(base_path=base)

            # Feature cards gated by entitlements. Each card either renders
            # in full or is replaced by a locked-card that opens the in-addon
            # upgrade modal (no public URL).
            wa    = render_whatsapp_section(base_path=base) if entitlements.get("whatsapp", True) \
                    else render_locked_card("📱 WhatsApp", "WhatsApp send + receive")
            tg    = render_telegram_section(base_path=base) if entitlements.get("telegram", True) \
                    else render_locked_card("💬 Telegram", "Telegram bot pairing")
            # Notification Designer — the no-YAML visual builder. Gated by the
            # same ha_integration entitlement (it writes HA automations).
            designer = render_designer_section(base_path=base) if entitlements.get("ha_integration", True) \
                    else render_locked_card("🎨 Notification Designer", "Visual no-YAML notification builder")
            ha    = render_ha_integration_section(base_path=base) if entitlements.get("ha_integration", True) \
                    else render_locked_card("🏠 Home Assistant automations", "REST commands & notify services")
            daily = render_daily_section(base_path=base)   # always available
            rules = render_rules_section(base_path=base)   # de-emphasised, always shown
            # Voice card: lite plan loses Alexa+Google+Siri entirely.
            voice_any = (entitlements.get("voice_alexa") or entitlements.get("voice_google") or entitlements.get("voice_siri"))
            voice = render_voice_section() if voice_any \
                    else render_locked_card("🎙️ Voice control (Alexa / Google / Siri)", "Voice control on Pro plan and up")

            # Order: Subscription, License, WhatsApp QR, Telegram, Recipients,
            # then the Notification Designer (the star — build alerts visually),
            # the HA integration snippet (advanced / fallback), Daily, Rules,
            # Voice at the bottom.
            self.send_html(200, page("Cinexis Setup",
                sub + lic + wa + tg + recs + designer + ha + daily + rules + voice,
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
        elif path == "/designer/entities":
            # Triggerable entities grouped by domain (the useful ones for
            # notifications) + a separate camera list for snapshots. Each entity
            # carries its current state + suggested target states.
            ok, states = ha_api_call("GET", "/states", timeout=10)
            if not ok or not isinstance(states, list):
                self.send_json(200, {"ok": False, "error": (states or {}).get("error", "ha_unreachable")})
                return
            # Domains worth triggering on, with their common target states.
            TRIGGER_DOMAINS = {
                "binary_sensor": ["on", "off"], "person": ["home", "not_home"],
                "device_tracker": ["home", "not_home"], "lock": ["locked", "unlocked"],
                "cover": ["open", "closed"], "door": ["open", "closed"],
                "switch": ["on", "off"], "input_boolean": ["on", "off"],
                "alarm_control_panel": ["armed_away", "armed_home", "disarmed", "triggered"],
                "sun": ["above_horizon", "below_horizon"], "climate": ["heat", "cool", "off"],
            }
            groups = {}
            cameras = []
            for s in states:
                eid = s.get("entity_id", "")
                dom = eid.split(".")[0] if "." in eid else ""
                fn = (s.get("attributes") or {}).get("friendly_name", "") or eid
                cur = s.get("state", "")
                if dom == "camera":
                    cameras.append({"entity_id": eid, "friendly_name": fn})
                    continue
                if dom in TRIGGER_DOMAINS:
                    groups.setdefault(dom, []).append({
                        "entity_id": eid, "friendly_name": fn, "state": cur,
                        "target_states": TRIGGER_DOMAINS[dom],
                    })
            self.send_json(200, {"ok": True, "groups": groups, "cameras": cameras})
        elif path == "/designer/check":
            # Is rest_command.cinexis_notify wired up in HA yet? The designer's
            # saved automations call it, so we guide the one-time setup if not.
            ok, services = ha_api_call("GET", "/services", timeout=8)
            has_rc = False
            if ok and isinstance(services, list):
                for grp in services:
                    if grp.get("domain") == "rest_command" and "cinexis_notify" in (grp.get("services") or {}):
                        has_rc = True; break
            self.send_json(200, {"ok": True, "rest_command_ready": has_rc})
        elif path == "/designer/list":
            self.send_json(200, {"ok": True, "automations": load_designer_automations()})
        elif path == "/wa/status":
            self.send_json(200, wa_service_call("GET", "/status"))
        elif path == "/wa/qr":
            self.send_json(200, wa_service_call("GET", "/qr"))
        elif path == "/wizard/status":
            # Addon-side proxy of /api/addon/status so the JS can poll over
            # ingress (cross-origin to cinexis.cloud would need CORS).
            self.send_json(200, cinexis_addon_call("GET", "/status"))
        elif path == "/plans":
            # Proxy /api/addon/plans — authenticated by node creds server-side.
            # Keeps the customer's request inside the addon's iframe.
            self.send_json(200, cinexis_addon_call("GET", "/plans"))
        elif path == "/diag":
            # Diagnostics — what does the cloud think of THIS addon's node,
            # AND is the local WhatsApp service alive? Useful when a customer
            # reports "WhatsApp QR not showing".
            node_id, _ = get_node_credentials()
            status = cinexis_addon_call("GET", "/status")
            wa = wa_service_call("GET", "/status")
            wa_health = {
                "reachable":   not (isinstance(wa, dict) and str(wa.get("error","")).startswith("wa_service_unreachable")),
                "connected":   bool(wa.get("connected")) if isinstance(wa, dict) else False,
                "has_qr":      bool(wa.get("has_qr")) if isinstance(wa, dict) else False,
                "phone":       wa.get("phone") if isinstance(wa, dict) else None,
                "last_error":  wa.get("last_error") if isinstance(wa, dict) else None,
                "reconnect_attempts": wa.get("reconnect_attempts") if isinstance(wa, dict) else None,
            }
            self.send_json(200, {
                "ok": True,
                "addon_version": "1.16.0",
                "node_id": node_id,
                "cloud_status": status,
                "wa_service": wa_health,
            })
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

        elif path == "/subscribe":
            # Server-side proxy of /api/addon/subscribe — addon's JS hits us
            # over ingress (no CORS), we forward with node creds. Returns the
            # Razorpay-hosted short_url; addon UI opens THAT in a new tab so
            # the customer never lands on a cinexis.cloud public page.
            try:
                body = json.loads(self.read_body() or "{}")
                plan_slug      = body.get("plan_slug")
                billing_period = body.get("billing_period", "monthly")
                if not plan_slug:
                    self.send_json(400, {"ok": False, "error": "plan_slug_required"})
                    return
                # Invalidate status cache so the post-subscribe poll sees fresh data.
                _STATUS_CACHE["data"] = None
                result = cinexis_addon_call("POST", "/subscribe", {
                    "plan_slug":      plan_slug,
                    "billing_period": billing_period,
                })
                self.send_json(200, result)
            except Exception as e:
                self.send_json(500, {"ok": False, "error": str(e)})

        elif path == "/recipients/add":
            try:
                body = json.loads(self.read_body() or "{}")
                name    = (body.get("name") or "").strip()[:60]
                channel = (body.get("channel") or "whatsapp").strip().lower()
                address = (body.get("address") or "").strip()
                if not name or not address or channel not in ("whatsapp", "telegram"):
                    self.send_json(400, {"ok": False, "error": "name_address_and_valid_channel_required"})
                    return
                recipients = load_recipients()
                # Reject duplicate names (case-insensitive) — names are the
                # primary key from HA's perspective.
                if any(r.get("name", "").lower() == name.lower() for r in recipients):
                    self.send_json(409, {"ok": False, "error": "name_already_exists"})
                    return
                import uuid
                rec = {
                    "id":      uuid.uuid4().hex[:12],
                    "name":    name,
                    "channel": channel,
                    "address": address,
                    "enabled": True,
                    "created_at": int(time.time()),
                }
                recipients.append(rec)
                save_recipients(recipients)
                self.send_json(200, {"ok": True, "recipient": rec})
            except Exception as e:
                self.send_json(500, {"ok": False, "error": str(e)})

        elif path.startswith("/recipients/") and path.endswith("/toggle"):
            rid = path[len("/recipients/"):-len("/toggle")]
            recipients = load_recipients()
            found = False
            for r in recipients:
                if r.get("id") == rid:
                    r["enabled"] = not r.get("enabled", True)
                    found = True
                    break
            if not found:
                self.send_json(404, {"ok": False, "error": "not_found"})
                return
            save_recipients(recipients)
            self.send_json(200, {"ok": True})

        elif path.startswith("/recipients/") and path.endswith("/remove"):
            rid = path[len("/recipients/"):-len("/remove")]
            recipients = [r for r in load_recipients() if r.get("id") != rid]
            save_recipients(recipients)
            self.send_json(200, {"ok": True})

        elif path.startswith("/recipients/") and path.endswith("/test"):
            rid = path[len("/recipients/"):-len("/test")]
            rec = next((r for r in load_recipients() if r.get("id") == rid), None)
            if not rec:
                self.send_json(404, {"ok": False, "error": "not_found"})
                return
            try:
                if rec.get("channel") == "telegram":
                    result = tg_send_test(rec.get("address"))
                else:
                    result = wa_service_call("POST", "/test", {"to": rec.get("address")})
                self.send_json(200, result if result.get("ok") else {"ok": False, "error": result.get("error", "send_failed")})
            except Exception as e:
                self.send_json(500, {"ok": False, "error": str(e)})

        elif path == "/subscribe/cancel":
            # Cancel auto-renewal on the customer's Razorpay subscription.
            # No payload required — cloud resolves the sub by node_id.
            try:
                _STATUS_CACHE["data"] = None
                result = cinexis_addon_call("POST", "/subscribe/cancel", {})
                self.send_json(200, result)
            except Exception as e:
                self.send_json(500, {"ok": False, "error": str(e)})

        elif path == "/designer/preview":
            # The killer feature: render the composed message through HA's
            # template engine (so {{ now() }} etc. become real values), then
            # send it — with the live camera snapshot — to ONE chosen recipient
            # so the customer sees the EXACT WhatsApp message before saving.
            try:
                body = json.loads(self.read_body() or "{}")
                message  = (body.get("message") or "").strip()
                to_name  = (body.get("to") or "").strip()
                image_entity = (body.get("image_entity") or "").strip() or None
                if not to_name:
                    self.send_json(400, {"ok": False, "error": "pick a recipient to preview to"})
                    return
                # Render templates via HA (best-effort — falls back to raw text).
                rendered = message
                if "{{" in message:
                    ok, out = ha_api_call("POST", "/template", {"template": message}, timeout=8)
                    if ok and isinstance(out, str):
                        rendered = out
                payload = {"to": to_name, "message": "🔔 PREVIEW — " + (rendered or "(no message)")}
                if image_entity:
                    payload["image_entity"] = image_entity
                result = wa_service_call("POST", "/notify", payload)
                self.send_json(200, {"ok": bool(result.get("ok")), "rendered": rendered,
                                     "sent": result.get("sent"), "failed": result.get("failed"),
                                     "error": result.get("error")})
            except Exception as e:
                self.send_json(500, {"ok": False, "error": str(e)})

        elif path == "/designer/save":
            # Build + persist a notification: write the HA automation via the
            # core API, store our own copy for the editor, and seed the
            # per-automation recipient map. Falls back to returning the YAML
            # for manual paste if the HA API write isn't permitted.
            try:
                import uuid, re as _re
                body = json.loads(self.read_body() or "{}")
                name     = (body.get("name") or "").strip()[:80]
                entity   = (body.get("entity") or "").strip()
                to_state = (body.get("to_state") or "").strip()
                recipients = body.get("recipients") or []
                message  = (body.get("message") or "").strip()
                image_entity = (body.get("image_entity") or "").strip() or None
                if not name or not entity or not to_state or not recipients or not message:
                    self.send_json(400, {"ok": False, "error": "name, trigger, recipients and message are all required"})
                    return
                slug = "cinexis_" + _re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:40]
                auto_eid = "automation." + slug
                data = {"to": recipients, "message": message, "automation_id": auto_eid}
                if image_entity:
                    data["image_entity"] = image_entity
                # Classic automation schema (trigger/condition/action +
                # platform/service) — accepted by ALL Home Assistant versions,
                # unlike the newer triggers/actions keys.
                automation = {
                    "alias": "Cinexis · " + name,
                    "description": "Created with the Cinexis Notification Designer",
                    "mode": "single",
                    "trigger": [{"platform": "state", "entity_id": entity, "to": to_state}],
                    "condition": [],
                    "action": [{"service": "rest_command.cinexis_notify", "data": data}],
                }
                # Write to HA via the config automation API.
                ok, out = ha_api_call("POST", f"/config/automation/config/{slug}", automation)
                # Persist our own record either way.
                items = [a for a in load_designer_automations() if a.get("id") != slug]
                items.append({"id": slug, "entity_id": auto_eid, "name": name, "entity": entity,
                              "to_state": to_state, "recipients": recipients, "message": message,
                              "image_entity": image_entity, "ha_written": bool(ok)})
                save_designer_automations(items)
                # Seed per-automation recipient defaults.
                amap = load_automation_map(); amap[auto_eid] = recipients; save_automation_map(amap)
                if ok:
                    self.send_json(200, {"ok": True, "automation_id": auto_eid, "ha_written": True})
                else:
                    # Fall back: hand the customer the YAML to paste.
                    yaml_txt = (
                        f"- alias: \"Cinexis · {name}\"\n"
                        f"  trigger:\n    - platform: state\n      entity_id: {entity}\n      to: \"{to_state}\"\n"
                        f"  action:\n    - service: rest_command.cinexis_notify\n      data:\n"
                        f"        to: {json.dumps(recipients)}\n        message: \"{message}\"\n"
                        + (f"        image_entity: {image_entity}\n" if image_entity else "")
                    )
                    self.send_json(200, {"ok": True, "ha_written": False,
                                         "fallback_yaml": yaml_txt,
                                         "reason": (out or {}).get("error", "ha_write_failed")})
            except Exception as e:
                self.send_json(500, {"ok": False, "error": str(e)})

        elif path == "/designer/delete":
            try:
                body = json.loads(self.read_body() or "{}")
                aid = (body.get("id") or "").strip()
                items = load_designer_automations()
                target = next((a for a in items if a.get("id") == aid), None)
                if target:
                    # Remove from HA too (best-effort).
                    ha_api_call("DELETE", f"/config/automation/config/{aid}")
                    items = [a for a in items if a.get("id") != aid]
                    save_designer_automations(items)
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
