#!/usr/bin/env python3
"""
Cinexis Remote Access — Alexa Smart Home Handler
Handles Alexa Smart Home API v3 directives via Home Assistant Supervisor API.
Listens on port 18081 — FRP tunnels it as {subdomain}bot.ha1.cinexis.cloud

Auth: validates X-Cinexis-Secret header (timing-safe) against device_secret file.
HA API: http://supervisor/core/api/* using SUPERVISOR_TOKEN env var.
"""

import json
import os
import uuid
import hmac
import http.server
import urllib.request
import urllib.error
import threading
import sys
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────
PORT          = int(os.environ.get("ALEXA_HANDLER_PORT", "18081"))
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
HA_BASE       = "http://supervisor/core"
SECRET_PATHS  = ["/share/cinexis/device_secret", "/data/device_secret"]
DISCOVERY_CAP = 300

SUPPORTED_DOMAINS = {
    "light", "switch", "cover", "climate", "fan",
    "scene", "script", "media_player", "input_boolean"
}
DOMAIN_PRIORITY = {
    "light": 1, "switch": 2, "cover": 3, "climate": 4, "fan": 5,
    "media_player": 6, "input_boolean": 7, "script": 8, "scene": 9
}
DOMAIN_CATEGORY = {
    "light": "LIGHT", "switch": "SWITCH", "cover": "INTERIOR_BLIND",
    "climate": "THERMOSTAT", "fan": "FAN", "scene": "SCENE_TRIGGER",
    "script": "SCENE_TRIGGER", "media_player": "SPEAKER", "input_boolean": "SWITCH"
}

# ── Logging ───────────────────────────────────────────────────────────────────
def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[cinexis-alexa] {ts} {msg}", flush=True)

# ── Device secret ─────────────────────────────────────────────────────────────
def get_device_secret():
    for path in SECRET_PATHS:
        try:
            with open(path) as f:
                return f.read().strip()
        except Exception:
            pass
    return ""

# ── HA REST helpers ───────────────────────────────────────────────────────────
def ha_get(path):
    req = urllib.request.Request(
        f"{HA_BASE}{path}",
        headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())

def ha_post_service(domain, service, entity_id, extra=None):
    data = {"entity_id": entity_id}
    if extra:
        data.update(extra)
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        f"{HA_BASE}/api/services/{domain}/{service}",
        data=body,
        headers={
            "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
            "Content-Type": "application/json"
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except Exception as e:
        log(f"HA service call failed: {domain}.{service} on {entity_id}: {e}")
        return False

def ha_get_state(entity_id):
    try:
        return ha_get(f"/api/states/{entity_id}")
    except Exception:
        return None

# ── Alexa response builders ───────────────────────────────────────────────────
def new_msg_id():
    return str(uuid.uuid4())

def alexa_response(directive, properties=None):
    return {
        "event": {
            "header": {
                "namespace": "Alexa",
                "name": "Response",
                "messageId": new_msg_id(),
                "correlationToken": directive.get("header", {}).get("correlationToken"),
                "payloadVersion": "3"
            },
            "endpoint": {"endpointId": directive.get("endpoint", {}).get("endpointId", "")},
            "payload": {}
        },
        "context": {"properties": properties or []}
    }

def alexa_error(directive, error_type, message):
    return {
        "event": {
            "header": {
                "namespace": "Alexa",
                "name": "ErrorResponse",
                "messageId": new_msg_id(),
                "correlationToken": directive.get("header", {}).get("correlationToken"),
                "payloadVersion": "3"
            },
            "endpoint": {"endpointId": directive.get("endpoint", {}).get("endpointId", "")},
            "payload": {"type": error_type, "message": message}
        }
    }

def cap(iface, props=None, proactively=True):
    c = {"type": "AlexaInterface", "interface": iface, "version": "3"}
    if props is not None:
        c["properties"] = {
            "supported": [{"name": p} for p in props],
            "proactivelyReported": proactively,
            "retrievable": True
        }
    return c

def caps_for_domain(domain, attributes):
    base = [cap("Alexa")]
    if domain == "light":
        cs = [cap("Alexa.PowerController", ["powerState"])]
        if attributes.get("brightness") is not None:
            cs.append(cap("Alexa.BrightnessController", ["brightness"]))
        if attributes.get("color_temp") is not None:
            cs.append(cap("Alexa.ColorTemperatureController", ["colorTemperatureInKelvin"]))
        if attributes.get("rgb_color") is not None:
            cs.append(cap("Alexa.ColorController", ["color"]))
        return base + cs
    elif domain in ("switch", "input_boolean", "fan"):
        return base + [cap("Alexa.PowerController", ["powerState"])]
    elif domain == "cover":
        return base + [
            cap("Alexa.PowerController", ["powerState"]),
            cap("Alexa.PercentageController", ["percentage"]),
        ]
    elif domain == "climate":
        return base + [
            cap("Alexa.ThermostatController", ["targetSetpoint", "thermostatMode"]),
            cap("Alexa.TemperatureSensor", ["temperature"]),
            cap("Alexa.PowerController", ["powerState"]),
        ]
    elif domain in ("scene", "script"):
        return [cap("Alexa"), cap("Alexa.SceneController", [], proactively=False)]
    elif domain == "media_player":
        return base + [
            cap("Alexa.PowerController", ["powerState"]),
            cap("Alexa.Speaker", ["volume", "muted"]),
            cap("Alexa.PlaybackController", []),
        ]
    else:
        return base + [cap("Alexa.PowerController", ["powerState"])]

# ── State → Alexa properties ──────────────────────────────────────────────────
def state_to_properties(state):
    domain    = state["entity_id"].split(".")[0]
    s         = state.get("state", "off")
    attrs     = state.get("attributes", {})
    now_iso   = datetime.now(timezone.utc).isoformat()
    props     = []

    power = "ON" if s in ("on", "open", "playing", "heat", "cool", "fan_only", "auto", "dry") else "OFF"
    props.append({
        "namespace": "Alexa.PowerController", "name": "powerState",
        "value": power, "timeOfSample": now_iso, "uncertaintyInMilliseconds": 500
    })

    if domain == "light" and attrs.get("brightness") is not None:
        pct = round((attrs["brightness"] / 255) * 100)
        props.append({
            "namespace": "Alexa.BrightnessController", "name": "brightness",
            "value": pct, "timeOfSample": now_iso, "uncertaintyInMilliseconds": 500
        })

    if domain == "cover" and attrs.get("current_position") is not None:
        props.append({
            "namespace": "Alexa.PercentageController", "name": "percentage",
            "value": attrs["current_position"], "timeOfSample": now_iso, "uncertaintyInMilliseconds": 500
        })

    if domain == "climate":
        temp = attrs.get("temperature") or attrs.get("current_temperature")
        if temp is not None:
            props.append({
                "namespace": "Alexa.ThermostatController", "name": "targetSetpoint",
                "value": {"value": temp, "scale": "CELSIUS"}, "timeOfSample": now_iso, "uncertaintyInMilliseconds": 500
            })
        mode_map = {"heat": "HEAT", "cool": "COOL", "auto": "AUTO", "off": "OFF", "fan_only": "OFF", "dry": "OFF"}
        props.append({
            "namespace": "Alexa.ThermostatController", "name": "thermostatMode",
            "value": mode_map.get(s, "OFF"), "timeOfSample": now_iso, "uncertaintyInMilliseconds": 500
        })
        cur_temp = attrs.get("current_temperature")
        if cur_temp is not None:
            props.append({
                "namespace": "Alexa.TemperatureSensor", "name": "temperature",
                "value": {"value": cur_temp, "scale": "CELSIUS"}, "timeOfSample": now_iso, "uncertaintyInMilliseconds": 500
            })

    return props

# ── Directive handlers ────────────────────────────────────────────────────────
def handle_authorization(directive):
    return {"event": {"header": {"namespace": "Alexa.Authorization", "name": "AcceptGrant.Response",
                                  "messageId": new_msg_id(), "payloadVersion": "3"}, "payload": {}}}

def handle_discovery():
    try:
        states = ha_get("/api/states")
    except Exception as e:
        log(f"Discovery: failed to fetch HA states: {e}")
        return {"event": {"header": {"namespace": "Alexa.Discovery", "name": "Discover.Response",
                                      "messageId": new_msg_id(), "payloadVersion": "3"},
                           "payload": {"endpoints": []}}}

    endpoints = []
    for state in states:
        entity_id = state["entity_id"]
        domain    = entity_id.split(".")[0]
        if domain not in SUPPORTED_DOMAINS:
            continue
        attrs     = state.get("attributes", {})
        name      = attrs.get("friendly_name") or entity_id.replace("_", " ")
        if not name.strip():
            continue
        endpoints.append({
            "endpointId":        entity_id,
            "manufacturerName":  "Home Assistant",
            "friendlyName":      name,
            "description":       f"{domain} via Cinexis",
            "displayCategories": [DOMAIN_CATEGORY.get(domain, "OTHER")],
            "capabilities":      caps_for_domain(domain, attrs),
            "cookie":            {"domain": domain},
            "_priority":         DOMAIN_PRIORITY.get(domain, 10)
        })

    endpoints.sort(key=lambda e: (e["_priority"], e["friendlyName"].lower()))
    capped = endpoints[:DISCOVERY_CAP]
    if len(endpoints) > DISCOVERY_CAP:
        log(f"Discovery: {len(endpoints)} endpoints found — capped to {DISCOVERY_CAP}")
    for e in capped:
        del e["_priority"]

    log(f"Discovery: exposing {len(capped)}/{len(endpoints)} endpoints")
    return {"event": {"header": {"namespace": "Alexa.Discovery", "name": "Discover.Response",
                                  "messageId": new_msg_id(), "payloadVersion": "3"},
                       "payload": {"endpoints": capped}}}

def handle_report_state(directive):
    entity_id = directive.get("endpoint", {}).get("endpointId", "")
    state = ha_get_state(entity_id)
    if not state:
        return alexa_error(directive, "NO_SUCH_ENDPOINT", f"Unknown entity: {entity_id}")
    props = state_to_properties(state)
    return {
        "event": {"header": {"namespace": "Alexa", "name": "StateReport", "messageId": new_msg_id(),
                              "correlationToken": directive.get("header", {}).get("correlationToken"),
                              "payloadVersion": "3"},
                   "endpoint": {"endpointId": entity_id}, "payload": {}},
        "context": {"properties": props}
    }

def handle_power(directive, name):
    entity_id = directive.get("endpoint", {}).get("endpointId", "")
    domain    = entity_id.split(".")[0]
    turn_on   = (name == "TurnOn")

    if domain == "cover":
        svc = "open_cover" if turn_on else "close_cover"
        ha_post_service("cover", svc, entity_id)
    elif domain in ("scene", "script") and turn_on:
        ha_post_service(domain, "turn_on", entity_id)
    elif turn_on:
        ha_post_service(domain, "turn_on", entity_id)
    else:
        ha_post_service(domain, "turn_off", entity_id)

    state = ha_get_state(entity_id)
    return alexa_response(directive, state_to_properties(state) if state else [])

def handle_brightness(directive, name):
    entity_id = directive.get("endpoint", {}).get("endpointId", "")
    payload   = directive.get("payload", {})
    if name == "SetBrightness":
        pct = payload.get("brightness", 100)
        ha_post_service("light", "turn_on", entity_id, {"brightness_pct": pct})
    elif name == "AdjustBrightness":
        delta = payload.get("brightnessDelta", 0)
        state = ha_get_state(entity_id)
        cur   = round(((state or {}).get("attributes", {}).get("brightness", 128) / 255) * 100) if state else 50
        new_pct = max(0, min(100, cur + delta))
        ha_post_service("light", "turn_on", entity_id, {"brightness_pct": new_pct})
    state = ha_get_state(entity_id)
    return alexa_response(directive, state_to_properties(state) if state else [])

def handle_color_temp(directive):
    entity_id = directive.get("endpoint", {}).get("endpointId", "")
    kelvin    = directive.get("payload", {}).get("colorTemperatureInKelvin", 4000)
    ha_post_service("light", "turn_on", entity_id, {"kelvin": kelvin})
    state = ha_get_state(entity_id)
    return alexa_response(directive, state_to_properties(state) if state else [])

def handle_thermostat(directive, name):
    entity_id = directive.get("endpoint", {}).get("endpointId", "")
    payload   = directive.get("payload", {})
    if name == "SetTargetTemperature":
        sp = payload.get("targetSetpoint", {})
        temp = sp.get("value")
        if temp is not None:
            ha_post_service("climate", "set_temperature", entity_id, {"temperature": temp})
    elif name == "SetThermostatMode":
        alexa_mode = payload.get("thermostatMode", {}).get("value", "OFF")
        mode_map   = {"HEAT": "heat", "COOL": "cool", "AUTO": "auto", "OFF": "off"}
        ha_mode = mode_map.get(alexa_mode, "off")
        ha_post_service("climate", "set_hvac_mode", entity_id, {"hvac_mode": ha_mode})
    state = ha_get_state(entity_id)
    return alexa_response(directive, state_to_properties(state) if state else [])

def handle_percentage(directive, name):
    entity_id = directive.get("endpoint", {}).get("endpointId", "")
    payload   = directive.get("payload", {})
    if name == "SetPercentage":
        pct = payload.get("percentage", 50)
        ha_post_service("cover", "set_cover_position", entity_id, {"position": pct})
    elif name == "AdjustPercentage":
        delta = payload.get("percentageDelta", 0)
        state = ha_get_state(entity_id)
        cur   = (state or {}).get("attributes", {}).get("current_position", 50) if state else 50
        new_pos = max(0, min(100, cur + delta))
        ha_post_service("cover", "set_cover_position", entity_id, {"position": new_pos})
    state = ha_get_state(entity_id)
    return alexa_response(directive, state_to_properties(state) if state else [])

def handle_speaker(directive, name):
    entity_id = directive.get("endpoint", {}).get("endpointId", "")
    payload   = directive.get("payload", {})
    if name == "SetVolume":
        vol = payload.get("volume", 50)
        ha_post_service("media_player", "volume_set", entity_id, {"volume_level": vol / 100})
    elif name == "AdjustVolume":
        delta = payload.get("volume", 0)
        state = ha_get_state(entity_id)
        cur   = (state or {}).get("attributes", {}).get("volume_level", 0.5) if state else 0.5
        new_vol = max(0.0, min(1.0, cur + delta / 100))
        ha_post_service("media_player", "volume_set", entity_id, {"volume_level": new_vol})
    elif name == "SetMute":
        muted = payload.get("mute", False)
        ha_post_service("media_player", "volume_mute", entity_id, {"is_volume_muted": muted})
    state = ha_get_state(entity_id)
    return alexa_response(directive, state_to_properties(state) if state else [])

def handle_playback(directive, name):
    entity_id = directive.get("endpoint", {}).get("endpointId", "")
    svc_map   = {"Play": "media_play", "Pause": "media_pause", "Stop": "media_stop",
                 "Next": "media_next_track", "Previous": "media_previous_track"}
    svc = svc_map.get(name)
    if svc:
        ha_post_service("media_player", svc, entity_id)
    state = ha_get_state(entity_id)
    return alexa_response(directive, state_to_properties(state) if state else [])

# ── Main directive dispatcher ─────────────────────────────────────────────────
def dispatch(directive_wrapper):
    d   = directive_wrapper.get("directive", {})
    hdr = d.get("header", {})
    ns  = hdr.get("namespace", "")
    nm  = hdr.get("name", "")
    log(f"Directive: {ns}.{nm}")

    if ns == "Alexa.Authorization":
        return handle_authorization(d)
    if ns == "Alexa.Discovery":
        return handle_discovery()
    if ns == "Alexa" and nm == "ReportState":
        return handle_report_state(d)
    if ns == "Alexa.PowerController":
        return handle_power(d, nm)
    if ns == "Alexa.BrightnessController":
        return handle_brightness(d, nm)
    if ns == "Alexa.ColorTemperatureController":
        return handle_color_temp(d)
    if ns == "Alexa.ThermostatController":
        return handle_thermostat(d, nm)
    if ns == "Alexa.PercentageController":
        return handle_percentage(d, nm)
    if ns == "Alexa.Speaker":
        return handle_speaker(d, nm)
    if ns == "Alexa.PlaybackController":
        return handle_playback(d, nm)

    return alexa_error(d, "INVALID_DIRECTIVE", f"Unsupported: {ns}.{nm}")

# ── HTTP handler ──────────────────────────────────────────────────────────────
class AlexaHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress default HTTP logs (we log manually)

    def send_json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/voice/alexa/internal":
            self.send_json(404, {"error": "not_found"})
            return

        # Validate X-Cinexis-Secret (timing-safe)
        secret = get_device_secret()
        provided = self.headers.get("X-Cinexis-Secret", "")
        if not secret or not hmac.compare_digest(secret.encode(), provided.encode()):
            log("Rejected: invalid X-Cinexis-Secret")
            self.send_json(403, {"error": "forbidden"})
            return

        # Read body
        try:
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length))
        except Exception as e:
            self.send_json(400, {"error": "bad_request", "detail": str(e)})
            return

        # Dispatch
        try:
            result = dispatch(body)
        except Exception as e:
            log(f"Directive dispatch error: {e}")
            result = {
                "event": {"header": {"namespace": "Alexa", "name": "ErrorResponse",
                                      "messageId": new_msg_id(), "payloadVersion": "3"},
                           "payload": {"type": "INTERNAL_ERROR", "message": str(e)}}
            }

        self.send_json(200, result)

    def do_GET(self):
        if self.path == "/health":
            self.send_json(200, {"ok": True, "service": "cinexis-alexa"})
        else:
            self.send_json(404, {"error": "not_found"})


class ThreadedHTTPServer(http.server.ThreadingHTTPServer):
    pass


def main():
    if not SUPERVISOR_TOKEN:
        log("WARNING: SUPERVISOR_TOKEN not set — HA API calls will fail")
    secret = get_device_secret()
    if not secret:
        log("WARNING: device_secret not found — all requests will be rejected")

    server = ThreadedHTTPServer(("127.0.0.1", PORT), AlexaHandler)
    log(f"Alexa handler listening on port {PORT}")
    log(f"Endpoint: POST /voice/alexa/internal")
    server.serve_forever()


if __name__ == "__main__":
    main()
