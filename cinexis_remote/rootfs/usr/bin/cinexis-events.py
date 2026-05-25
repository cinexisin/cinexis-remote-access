#!/usr/bin/env python3
"""
Cinexis Events — HA WebSocket event listener.

Subscribes to Home Assistant's state_changed event bus (via the Supervisor
WebSocket API) and matches incoming events against the rules in
/share/cinexis/notification_rules.json. On match, renders the rule's
message template and sends via the addon-local WhatsApp (cinexis-wa.js
on :18083) and/or Telegram (via cinexis-ingress.py's /tg/send proxy on
:18082).

Rules file is re-read every 30 seconds, so edits in the addon UI take
effect within half a minute without restarting this process.

Per-rule cooldown ensures one chatty entity (motion sensor flapping)
doesn't blast 100 messages.
"""

import json
import os
import re
import time
import threading
import urllib.request
import urllib.error
from datetime import datetime, timezone

try:
    import websocket  # websocket-client package
except ImportError:
    print("[CINEXIS-EVENTS] websocket-client not installed; exiting")
    raise SystemExit(0)

# ── Config ────────────────────────────────────────────────────────────────────
STORAGE_DIR        = "/share/cinexis"
RULES_FILE         = f"{STORAGE_DIR}/notification_rules.json"
LAST_FIRED_FILE    = f"{STORAGE_DIR}/notification_rules_last_fired.json"
HA_WS_URL          = "ws://supervisor/core/api/websocket"
WA_SERVICE_URL     = "http://127.0.0.1:18083"
INGRESS_URL        = "http://127.0.0.1:18082"   # for Telegram proxy
RULES_RELOAD_SEC   = 30
HEARTBEAT_SEC      = 30

def log(msg):  print(f"[CINEXIS-EVENTS] {msg}", flush=True)
def warn(msg): print(f"[CINEXIS-EVENTS] ⚠️  {msg}", flush=True)
def err(msg):  print(f"[CINEXIS-EVENTS] ❌ {msg}", flush=True)

def get_supervisor_token():
    """Same fallback chain as the other Cinexis Python services."""
    for v in ("SUPERVISOR_TOKEN", "HASSIO_TOKEN"):
        t = os.environ.get(v, "")
        if t: return t
    for name in ("SUPERVISOR_TOKEN", "HASSIO_TOKEN"):
        try:
            with open(f"/var/run/s6/container_environment/{name}") as f:
                t = f.read().strip()
                if t: return t
        except Exception:
            pass
    return ""

# ── Rules ─────────────────────────────────────────────────────────────────────
class RuleStore:
    def __init__(self):
        self._rules = []
        self._last_fired = {}
        self._lock = threading.Lock()
        self.reload()

    def reload(self):
        try:
            with open(RULES_FILE) as f:
                data = json.load(f)
            rules = data.get("rules", [])
        except FileNotFoundError:
            rules = []
        except Exception as e:
            warn(f"rules file unreadable: {e}")
            rules = []
        try:
            with open(LAST_FIRED_FILE) as f:
                last_fired = json.load(f)
        except Exception:
            last_fired = {}
        with self._lock:
            self._rules = rules
            self._last_fired = last_fired
        log(f"rules reloaded: {len(rules)} active rule(s)")

    def matching(self, event_type, data):
        with self._lock:
            return [r for r in self._rules if r.get("enabled", True) and self._matches(r, event_type, data)]

    def _matches(self, rule, event_type, data):
        trig = rule.get("trigger") or {}
        if trig.get("type") == "state_change" and event_type == "state_changed":
            wanted_entity = trig.get("entity_id", "")
            if not wanted_entity: return False
            if data.get("entity_id") != wanted_entity: return False
            ns = (data.get("new_state") or {}).get("state")
            os_ = (data.get("old_state") or {}).get("state")
            if trig.get("to")   and ns != trig["to"]:   return False
            if trig.get("from") and os_ != trig["from"]: return False
            return True
        return False

    def mark_fired(self, rule_id):
        now = int(time.time())
        with self._lock:
            self._last_fired[rule_id] = now
        try:
            with open(LAST_FIRED_FILE, "w") as f:
                json.dump(self._last_fired, f)
        except Exception as e:
            warn(f"could not persist last_fired: {e}")

    def in_cooldown(self, rule):
        cd = int(rule.get("cooldown_seconds") or 0)
        if cd <= 0: return False
        with self._lock:
            last = self._last_fired.get(rule.get("id", ""))
        if not last: return False
        return (int(time.time()) - last) < cd

RULES = RuleStore()

def rules_reload_loop():
    while True:
        time.sleep(RULES_RELOAD_SEC)
        try: RULES.reload()
        except Exception as e: warn(f"reload failed: {e}")

# ── Template rendering ───────────────────────────────────────────────────────
def render_template(tpl, event_data):
    """Tiny mustache-style {{ var }} substitution. Available vars:
       {{entity}}, {{state}}, {{old_state}}, {{name}} (friendly), {{now}}, {{date}}, {{time}}.
    """
    new_state = event_data.get("new_state") or {}
    old_state = event_data.get("old_state") or {}
    attrs     = new_state.get("attributes") or {}
    now_ist   = datetime.now().astimezone()  # local TZ inside container; sysadmin sets TZ via HA
    vars_ = {
        "entity":    event_data.get("entity_id", ""),
        "state":     new_state.get("state", ""),
        "old_state": old_state.get("state", ""),
        "name":      attrs.get("friendly_name") or event_data.get("entity_id", ""),
        "unit":      attrs.get("unit_of_measurement", ""),
        "now":       now_ist.strftime("%H:%M:%S"),
        "date":      now_ist.strftime("%Y-%m-%d"),
        "time":      now_ist.strftime("%H:%M"),
        "datetime":  now_ist.strftime("%Y-%m-%d %H:%M:%S"),
    }
    def sub(m):
        key = m.group(1).strip()
        return str(vars_.get(key, m.group(0)))
    return re.sub(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}", sub, tpl)

# ── Notification senders ─────────────────────────────────────────────────────
def _http_post_json(url, payload):
    try:
        data = json.dumps(payload).encode()
        req  = urllib.request.Request(url, data=data, method="POST",
                                       headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"ok": False, "error": str(e)}

def send_whatsapp(to, text):
    if not to: return {"ok": False, "error": "empty_to"}
    return _http_post_json(f"{WA_SERVICE_URL}/send/text", {"to": to, "text": text})

def send_telegram(chat_id, text):
    if not chat_id: return {"ok": False, "error": "empty_chat_id"}
    return _http_post_json(f"{INGRESS_URL}/tg/send", {"chat_id": chat_id, "text": text})

def fire_rule(rule, event_data):
    if RULES.in_cooldown(rule):
        log(f"rule {rule.get('id')} '{rule.get('name')}' in cooldown — skipping")
        return
    tpl  = rule.get("message_template") or "{{entity}} → {{state}}"
    text = render_template(tpl, event_data)
    channels = rule.get("channels") or []
    if "whatsapp" in channels:
        for to in rule.get("wa_recipients") or []:
            r = send_whatsapp(to, text)
            log(f"WA → {to}: {'ok' if r.get('ok') else 'fail:' + str(r.get('error'))}")
    if "telegram" in channels:
        for chat in rule.get("tg_chat_ids") or []:
            r = send_telegram(chat, text)
            log(f"TG → {chat}: {'ok' if r.get('ok') else 'fail:' + str(r.get('error'))}")
    RULES.mark_fired(rule.get("id", ""))

# ── HA WebSocket client ──────────────────────────────────────────────────────
def ws_loop():
    msg_id = 1
    while True:
        token = get_supervisor_token()
        if not token:
            err("SUPERVISOR_TOKEN unavailable; retrying in 15s")
            time.sleep(15)
            continue
        try:
            ws = websocket.create_connection(HA_WS_URL, timeout=10)
            # 1. Auth handshake
            hello = json.loads(ws.recv())
            if hello.get("type") != "auth_required":
                warn(f"unexpected first message: {hello}")
            ws.send(json.dumps({"type": "auth", "access_token": token}))
            resp = json.loads(ws.recv())
            if resp.get("type") != "auth_ok":
                err(f"auth failed: {resp}")
                ws.close()
                time.sleep(15)
                continue
            log("HA WebSocket authenticated")

            # 2. Subscribe to state_changed events
            sub_id = msg_id; msg_id += 1
            ws.send(json.dumps({"id": sub_id, "type": "subscribe_events", "event_type": "state_changed"}))
            ws.recv()  # ack
            log("subscribed to state_changed")

            # 3. Receive loop
            last_pong = time.time()
            while True:
                try:
                    raw = ws.recv()
                except Exception:
                    raise
                if not raw: continue
                msg = json.loads(raw)
                if msg.get("type") != "event": continue
                evt   = msg.get("event") or {}
                etype = evt.get("event_type")
                data  = evt.get("data") or {}
                matches = RULES.matching(etype, data)
                for rule in matches:
                    try: fire_rule(rule, data)
                    except Exception as e: warn(f"fire_rule failed: {e}")

                # Lightweight heartbeat to keep connection
                now_t = time.time()
                if now_t - last_pong > HEARTBEAT_SEC:
                    ping_id = msg_id; msg_id += 1
                    try:
                        ws.send(json.dumps({"id": ping_id, "type": "ping"}))
                        last_pong = now_t
                    except Exception:
                        raise

        except Exception as e:
            warn(f"WS connection error: {e} — reconnecting in 10s")
            try: ws.close()
            except Exception: pass
            time.sleep(10)

def main():
    log("starting Cinexis HA event listener")
    if not os.path.exists(STORAGE_DIR):
        os.makedirs(STORAGE_DIR, exist_ok=True)
    threading.Thread(target=rules_reload_loop, daemon=True).start()
    ws_loop()

if __name__ == "__main__":
    main()
