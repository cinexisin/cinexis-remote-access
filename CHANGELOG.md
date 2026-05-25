# Changelog — Cinexis Remote Access

All notable changes to this add-on are documented here.
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [1.11.3] - 2026-05-25

### Fixed
- **"Addon seems not ready" error when cloud unreachable.** Previously when
  cloud registration failed (DNS/firewall/transit issue), the script did
   to retry, which replaced the bash process and killed
  every spawned child including the ingress UI. HA showed "addon not ready"
  intermittently because the UI was down for ~3 seconds every retry cycle.
  Now the registration retry loops in-place with exponential backoff (15s,
  30s, 60s, 120s, then capped at 300s) — the ingress UI, WhatsApp Web service
  and event listener stay alive throughout, so the customer can configure
  notifications even when the cloud side is unreachable.

---

## [1.11.2] - 2026-05-25

### Added — Daily summary report

Closes Phase 2 of v1.11. The addon now sends a once-a-day summary at a
configurable time (default 08:00 local) covering: state-change count per
tracked entity, currently-low batteries, current state of each entity.

- New dashboard card **"📊 Daily Summary Report"** — enable / time picker /
  battery threshold / multi-line entity list / WA + TG recipients /
  *Send now (test)* button.
- `cinexis-events.py` now spawns a `daily_loop` thread (1-minute cron
  resolution). Checks the configured time daily, builds the summary by
  hitting HA's `/api/history/period?filter_entity_id=…` for the last 24 h,
  fires via the local WA + Telegram senders. Idempotent — `daily_summary_last.json`
  stores the date already sent so we don't fire twice.
- Storage: `/share/cinexis/daily_summary.json`.

### Phase 2 complete
The addon notification stack started in v1.10 with the wrong cloud-relay
model and pivoted in v1.11 to GreenAPI-style local sending is now
end-to-end:
  - WhatsApp pairing via Baileys ✅
  - Telegram bot token setup ✅
  - Event-triggered rules (HA WebSocket) ✅
  - Daily summary report ✅

---

## [1.11.1] - 2026-05-25

### Added — Phase 2: Telegram + HA events + rules editor

The dashboard now has the full GreenAPI-style stack: scan WhatsApp, paste a
Telegram bot token, define rules like *"front door opens between 10 pm and
6 am → message my family group"*, and the new HA WebSocket listener fires
them within a second.

- **Telegram setup** card on the dashboard. Paste a [@BotFather](https://t.me/BotFather)
  token + default chat_id, hit *Verify & save* (calls Telegram `getMe` to
  validate), then *Send test*. Token stored in `/share/cinexis/telegram_config.json`.
- **Notification Rules** card with full CRUD. Each rule: name, HA entity id
  (autocompleted from a fetched `/api/states`), optional from/to state
  filter, message template with mustache-style placeholders
  (`{{name}}`, `{{state}}`, `{{old_state}}`, `{{time}}`, `{{date}}`, `{{datetime}}`,
  `{{unit}}`), channels (WhatsApp / Telegram / both), per-channel recipients,
  cooldown seconds, enabled toggle. Stored in
  `/share/cinexis/notification_rules.json`.
- **`cinexis-events.py`** — new Python service that connects to HA's
  WebSocket API via `SUPERVISOR_TOKEN`, subscribes to `state_changed`, and
  fires matching rules. Reloads rules from disk every 30 s — UI edits take
  effect quickly. Per-rule cooldown stored in
  `notification_rules_last_fired.json` so a chatty motion sensor doesn't
  blast 100 messages. Auto-reconnects to HA on disconnect with a 10 s
  backoff.
- **Dockerfile**: `py3-pip` + `websocket-client==1.8.0` added.

### Coming next (v1.11.2)
- Daily morning summary report (configurable HH:MM)
- Per-entity batch notifications (group door-open events from same hour)
- Quick-action templates: "Front door alert", "Battery low", "AC reminder"

---

## [1.11.0] - 2026-05-25

### Added — Phase 1: WhatsApp Web (Baileys) pairing

This release introduces the **GreenAPI-style** notification stack: the owner
pairs their own personal WhatsApp once via the addon UI, and from then on
the addon sends Home Assistant event notifications **from the owner's
WhatsApp number** to whoever they configure. Nothing routes through
cinexis.cloud — all local.

- New Node.js 20 service `/usr/lib/cinexis-wa/cinexis-wa.js` that wraps
  [@whiskeysockets/baileys](https://github.com/WhiskeySockets/Baileys).
  Auth state persisted to `/share/cinexis/wa-auth/` (survives addon
  updates). Auto-reconnect with exponential backoff. HTTP control surface
  on `127.0.0.1:18083` — only the addon's Python ingress talks to it.
- HTTP API: `GET /status`, `GET /qr`, `POST /send/text`, `POST /send/image`,
  `POST /test`, `POST /logout`.
- Addon UI now has a **WhatsApp** card on the dashboard. While unpaired
  it shows a QR; after pairing it shows "Connected as +XX…" plus a
  test-send form. Logout button wipes auth and re-renders the QR.
- New `/wa/*` proxy routes in `cinexis-ingress.py` so the browser can
  talk to the local Node service through the HA ingress without exposing
  port 18083 outside the container.
- `Dockerfile` now installs nodejs + npm and `npm install --omit=dev`s
  the WA service at build time. ~30 MB image growth.

### Removed
- The old wizard step D "Get WhatsApp / Telegram link" — that flow routed
  every notification through *our* central Cinexis WhatsApp Business
  number, which was the wrong architecture for an addon meant to be
  used like GreenAPI. The new flow uses the owner's own WA via Baileys.
- Cloud-side `/api/addon/notif/link-token`,
  `/api/addon/notif/redeem-telegram`, and the inbound `LINK-XXXX`
  webhook handler.

### Coming in subsequent commits (still v1.11.0-dev)
- Telegram setup (owner pastes their own bot token in the addon UI)
- HA WebSocket event listener service (`cinexis-events.py`)
- Notification rules CRUD editor (entity / state / cooldown / template / recipients)
- Daily morning summary report

---

## [1.10.0] - 2026-05-24

### Added
- **Customer onboarding wizard** — first-run experience in the ingress UI. On
  fresh install the addon shows a 4-step wizard instead of the legacy
  license-OTP screen:
    1. Customer details (name / email / WhatsApp / location / GSTIN / use case)
    2. Plan picker (Lite / Smart / Pro / Ultimate × Monthly / Quarterly /
       Half-yearly / Yearly) — UPI AutoPay supported on the hosted checkout
    3. Razorpay payment page in a new tab with auto-poll
    4. WhatsApp / Telegram QR opt-in for event notifications
- New `cinexis_addon_call()` helper that automatically attaches the addon's
  node credentials to every call to `cinexis.cloud/api/addon/*`.
- `/share/cinexis/customer_profile.json` — written after the wizard
  completes; the addon shows the legacy dashboard on subsequent boots.
- Three new ingress endpoints (`/wizard/onboard`, `/wizard/subscribe`,
  `/wizard/notif`) plus `/wizard/status` for the JS poller — all of them
  proxy to the matching `cinexis.cloud/api/addon/*` endpoint.

### Notes
- No breaking change for existing customers — anyone already activated has
  a license cache and never sees the wizard.
- Offline-paid customers can skip Razorpay from step 3 — dealer activates
  them manually from admin.cinexis.cloud.

---

## [1.9.1] - 2026-04-27

### Fixed
- **Registration failed forever after a corrupted node-id file.** `ensure_node_id`,
  `ensure_secret`, and `ensure_short_id` checked `[ ! -f file ]` (file exists)
  rather than `[ ! -s file ]` (file exists AND is non-empty). A truncated
  zero-byte file from an interrupted previous write was therefore read as an
  empty string, causing the API to return HTTP 400 `{"error":"missing fields"}`.
  Now zero-byte files trigger regeneration; explicit empty-string guards exit
  with a clear error if generation itself fails.
- **Misleading "Failed to reach Cinexis API" message.** `register_node` used
  `curl -sf`, which reports HTTP 4xx/5xx as a generic network failure. It now
  uses `curl -s -w "%{http_code}"` and surfaces the real status: a network
  problem (DNS/TLS/timeout) is reported separately from a server-side rejection,
  and a 400 prints a hint to delete `/share/cinexis/node_id` and
  `/share/cinexis/device_secret` and retry.
- **Port 18082 stayed bound after registration retries.** The retry path
  re-execs `cinexis-entrypoint.sh` without `cleanup` running (because `exec`
  replaces the current process). The Python ingress UI subprocess was therefore
  inherited by HA's init, kept holding port 18082, and the next boot crashed
  with `OSError: [Errno 98] Address in use`. The retry path now explicitly
  kills `${INGRESS_PID}` and `${ALEXA_PID}` before `sleep 60 && exec`.

### Recovery for installs hit by the bug pre-1.9.1
Delete the corrupted state files and restart the addon — the script will
regenerate them on next boot:
```bash
rm -f /share/cinexis/node_id /share/cinexis/device_secret
# then: HA → Settings → Add-ons → Cinexis Remote Access → Restart
```

---

## [1.9.0] - 2026-04-22

### Added
- **Alexa backend selector** — new `alexa_backend` config option lets the
  customer choose which local service handles Alexa Smart Home directives:
  - `self` (default) — this addon handles Alexa via HA Supervisor API
  - `bot` — addon proxies `/voice/alexa/internal` to the `cinexis-bot` at
    `bot_host` (e.g. `192.168.1.50:3000`). The addon does not start its own
    Alexa handler in this mode; an nginx proxy on port 18081 forwards all
    directives to the bot instead.
- New `bot_host` config option (required when `alexa_backend=bot`).

### Notes
- When using `alexa_backend=bot`, the bot's `alexa_device_secret` must match
  this addon's registered `device_secret`. Copy from the Cinexis sidebar in
  HA, paste into the bot's Settings → Alexa Smart Home → "Override device
  secret" field.
- No cloud-side changes — the FRP tunnel URL stays the same; only the local
  listener changes.

---

## [1.0.3] - 2026-04-02

### Fixed
- Removed custom AppArmor profile — was blocking s6-overlay-suexec and causing startup failure
- Using Docker default AppArmor profile (apparmor: true) which still gives security rating 6
- Replace `exec "$0"` with full script path to avoid resolving to `/init` under s6

---

## [1.0.2] - 2026-04-02

### Fixed
- AppArmor profile now allows s6/init system paths — no more `/init: Permission denied` on shutdown
- Clean HA shutdown no longer triggers unexpected restart loop
- Heartbeat process properly killed on shutdown

---

## [1.0.1] - 2026-04-02

### Security
- Added AppArmor profile — security rating increased to 6/6
- Restricted file system access to only required paths
- Scoped network permissions

### Fixed
- Log output no longer leaks into status variable during registration
- Registration status check now works correctly for pending/blocked/expired nodes

---

## [1.0.0] - 2026-04-02

### Added
- Initial release of Cinexis Remote Access add-on
- Secure reverse tunnel via FRP to Cinexis Cloud
- Automatic node registration with `api1.cinexis.cloud`
- nginx proxy for Home Assistant with WebSocket support
- Heartbeat loop — tunnel stops if license is blocked or expired
- Supports amd64, aarch64, armv7 architectures
- Your HA accessible at `https://{node-id}.ha1.cinexis.cloud`
