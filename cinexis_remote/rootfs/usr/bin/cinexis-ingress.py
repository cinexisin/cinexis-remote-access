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
STORAGE_DIR       = "/share/cinexis"
LICENSE_KEY_FILE  = f"{STORAGE_DIR}/license_key"
EXCLUSIONS_FILE   = f"{STORAGE_DIR}/voice_exclusions.json"
SUPERVISOR_TOKEN  = os.environ.get("SUPERVISOR_TOKEN", "")
HA_BASE           = "http://supervisor/core"
CINEXIS_API       = "https://cinexis.cloud"

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
    if not SUPERVISOR_TOKEN:
        return []
    try:
        req = urllib.request.Request(
            f"{HA_BASE}/api/states",
            headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log(f"HA states fetch failed: {e}")
        return []

def cinexis_post(path, payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{CINEXIS_API}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())

# ── HTML helpers ──────────────────────────────────────────────────────────────
def page(title, body, extra_head=""):
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — Cinexis</title>
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
  <form method="post" action="/license/clear" style="margin-top:14px">
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

  <form method="post" action="/license/send-otp" id="otpRequestForm">
    <label>Email (registered with cinexis.cloud)</label>
    <input type="email" name="email" id="emailInput" placeholder="you@example.com" required autocomplete="email">
    <div style="margin-top:12px;display:flex;gap:8px;align-items:center">
      <button type="submit" class="btn btn-primary">Send OTP</button>
      <span id="otpTimer" style="font-size:.8rem;color:#64748b"></span>
    </div>
  </form>

  <form method="post" action="/license/verify-otp" style="margin-top:20px;padding-top:20px;border-top:1px solid #2d3748">
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
  fetch('/voice/toggle', {{
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
    fetch('/voice/toggle', {{
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

    def redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length)

    def parse_form(self):
        raw = self.read_body().decode()
        return dict(urllib.parse.parse_qsl(raw))

    def do_GET(self):
        path = self.path.split("?")[0]

        if path in ("/", "/index.html"):
            lic = render_license_section()
            voice = render_voice_section()
            self.send_html(200, page("Cinexis Setup", lic + voice))
        elif path == "/health":
            self.send_json(200, {"ok": True})
        else:
            self.send_html(404, page("Not Found", "<p>Page not found.</p>"))

    def do_POST(self):
        path = self.path.split("?")[0]
        ct = self.headers.get("Content-Type", "")

        if path == "/license/send-otp":
            form = self.parse_form()
            email = (form.get("email") or "").strip()
            if not email:
                body = render_license_section("Please enter a valid email.", "err") + render_voice_section()
                self.send_html(400, page("Cinexis Setup", body))
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
            self.send_html(200, page("Cinexis Setup", body))

        elif path == "/license/verify-otp":
            form = self.parse_form()
            email = (form.get("email") or "").strip()
            otp   = (form.get("otp") or "").strip()
            if not email or not otp:
                body = render_license_section("Email and OTP are required.", "err") + render_voice_section()
                self.send_html(400, page("Cinexis Setup", body))
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
                    self.send_html(200, page("Cinexis Setup", body))
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
                    self.send_html(400, page("Cinexis Setup", body))
            except Exception as e:
                body = render_license_section(f"Network error: {e}", "err") + render_voice_section()
                self.send_html(500, page("Cinexis Setup", body))

        elif path == "/license/clear":
            try:
                os.remove(LICENSE_KEY_FILE)
                log("License cleared via ingress UI")
            except FileNotFoundError:
                pass
            self.redirect("/")

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
            self.send_html(404, page("Not Found", "<p>Not found.</p>"))


class ThreadedHTTPServer(http.server.ThreadingHTTPServer):
    pass


def main():
    if not SUPERVISOR_TOKEN:
        log("WARNING: SUPERVISOR_TOKEN not set — HA device list will be empty")
    server = ThreadedHTTPServer(("0.0.0.0", PORT), IngressHandler)
    log(f"Ingress UI listening on port {PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
