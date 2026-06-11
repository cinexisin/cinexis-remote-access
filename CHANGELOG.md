## [1.19.1] - 2026-06-11

### Changed — cleaner dashboard for non-technical users

- The "Use from Home Assistant automations" YAML card (rest_command /
  notify wrapper, payload template, secret) is now collapsed into an
  "⚙️ Advanced — raw configuration.yaml" expandable, clearly pointing
  users to the 🎨 Notification Designer instead. A normal user no longer
  sees a wall of YAML.

### Fixed — WhatsApp service self-recovery

- The most common cause of "WhatsApp service not running" is a corrupt
  auth state in /share/cinexis/wa-auth (which survives addon updates, so
  reinstalling doesn't clear it). The service now wipes that state after
  3 consecutive boot failures and starts fresh (showing a new QR) instead
  of crash-looping.
- The watchdog no longer gives up permanently after 5 crashes — it backs
  off to a retry every ~5 min, so a transient failure recovers on its own.
- The "service not running" card now explains it's starting/recovering
  (auto-retries), notes Telegram + the Designer still work meanwhile, and
  points to the Log / /diag for diagnosis if it persists.

## [1.19.0] - 2026-06-11

### Added — 🎨 Notification Designer (no-YAML visual builder)

The headline feature: build WhatsApp/Telegram notifications visually, with a
LIVE preview, and the addon writes the Home Assistant automation for you. No
more hand-editing automations.yaml.

- **Visual builder** in the addon dashboard: ① pick a trigger (any HA
  sensor/lock/person/cover/alarm device + target state), ② tick recipients
  from your book, ③ compose the message with insertable value-chips (time,
  date, device state, device name), ④ optionally attach a live camera
  snapshot.
- **Live preview** — "Send preview to me" renders your message through HA's
  template engine (so {{ now() }} etc. become real values) and sends it, with
  the actual camera frame, to a chosen recipient. You see the EXACT WhatsApp
  message before saving.
- **Save & activate** writes the HA automation via the core config API
  (classic schema — works on all HA versions). If HA won't allow the
  programmatic write, it falls back to handing you the ready YAML to paste.
- Lists your designer-created notifications with one-click delete; seeds the
  per-automation recipient map so /notify routes correctly.
- Gated by the ha_integration entitlement; one-time rest_command setup is
  detected and guided.

New ingress endpoints: GET /designer/entities, /designer/check,
/designer/list; POST /designer/preview, /designer/save, /designer/delete.
New HA core API helper (ha_api_call) for reading entities, rendering
templates, and writing automations.

## [1.18.0] - 2026-06-10

### Security — per-node FRP tunnel token (replaces the shared secret)

The FRP tunnel token (`cinexis-frp-secret-2024`) was hardcoded into every
published addon image and validated as a single shared secret by frps — any
customer could grep it from the image and claim another (offline) customer's
`{sub}.ha1.cinexis.cloud` subdomain. Closed with per-node tokens:

- The addon now fetches a per-node token from the cloud at registration
  (`frp_token = HMAC(server-secret, node_id)`), caches it, and writes it into
  `frpc.toml` as **metadata** (`[metadatas] node_id, node_token`) — NOT as the
  auth token. The auth token stays the legacy shared token, so frps
  authenticates every client exactly as before. **Zero regression, no
  tunnel disruption.**
- A new frps server plugin (`ops/frps-auth-plugin/`) validates the per-node
  token and enforces that a node can only claim its own subdomain. Built to
  fail open and ships in observe mode; deploy + cutover steps are in
  `ops/frps-auth-plugin/README.md`. Verified in enforce mode: forged tokens
  and subdomain squatting are rejected; valid + legacy clients pass.

Companion cloud (deployed): `/p2p/register` + `/p2p/heartbeat` now return the
per-node `frp_token`; signing secret self-bootstraps to a 0600 file.

## [1.17.0] - 2026-06-10

### Added — /notify abuse guards (protect your WhatsApp number)

A flapping HA sensor firing the notify automation hundreds of times could
get your personal WhatsApp number flagged or banned. The /notify endpoint
now has a circuit breaker:

- Global cap: 30 notify calls/minute across all automations.
- Fan-out cap: max 50 recipients per single call.
- Per-recipient cooldown: 10s minimum gap to the same person (drops dupes
  from a loop hammering one contact).

Returns HTTP 429 with a clear hint when tripped, so an automation loop
pauses instead of burning your number.

### Companion cloud (deployed)

- Circuit breaker on the SHARED Cinexis WABA (used by all products):
  manual + auto kill-switch, quality_rating=RED auto-pause, 60/min global
  cap, 20s per-recipient cooldown. One product's runaway loop can no
  longer ban WhatsApp alerts for every product.

## [1.16.0] - 2026-06-10

### Fixed — WhatsApp QR never appearing (root causes from a deep-diagnosis pass)

The QR was held hostage by license/cloud state and had no crash recovery.
Three confirmed root causes, all fixed:

- **WA service now starts before the approval gate.** Previously `start_wa`
  ran only AFTER cloud registration succeeded AND the p2p node was
  'active' — so during a cloud outage (registration retry loop blocks) or
  while pending approval (`wait_for_approval` loops forever) the WhatsApp
  service never launched and the QR never appeared. It now starts right
  after the ingress UI, before any cloud/approval gate. The owner's
  personal WhatsApp pairing has nothing to do with license state.
- **Crash supervision added.** None of the backgrounded services
  (ingress, WA, alexa) had restart-on-crash — one Baileys throw killed
  WhatsApp for the whole addon lifetime. A watchdog now respawns any dead
  child every 20s (capped at 5 to avoid crash-loops).
- **WA service self-heals instead of dying.** A network blip fetching the
  WA web version used to throw → `process.exit(1)` → no restart → QR dead
  forever. Now: pinned-version fallback, retry-on-boot-failure, catch on
  the reconnect timers, and process-level crash guards. `/status` now
  surfaces `last_error` + `reconnect_attempts`.

### Changed — `/notify` now reachable from HA + secured

- The Baileys service binds `0.0.0.0` so HA Core's `rest_command.cinexis_notify`
  can actually reach it over the published port (was 127.0.0.1 — the host
  port mapping could never connect). Mutating endpoints (`/notify`,
  `/send/*`, `/test`, `/logout`) are now guarded by a shared secret (= the
  node device secret); the ingress proxy and the generated HA snippet both
  attach it automatically.
- `/diag` now reports local WhatsApp service health (reachable / connected
  / has_qr / last_error) alongside the cloud status — one call tells you
  exactly why the QR isn't showing.

### Companion cloud fixes (deployed)

- Razorpay webhook signature now verifies (raw-body HMAC) so a paid
  customer is actually promoted to active.
- `/api/addon/plans` returns real prices + razorpay_plan_id (was querying
  dead column names → empty grid → couldn't subscribe).
- Lifetime/comp customers (active, no expiry) no longer misread as
  pending_payment. Added `ultimate` plan entitlements + `plan_pretty`.

## [1.15.0] - 2026-05-26

### Added — Recipients book + unified /notify service

Replaces the v1.12.0 "paste phone numbers into configuration.yaml" pattern
with a proper recipients book. HA automations reference contacts by name
(Dad / Mom / Family / On-call) and the addon owns the address-resolution.

- New **📇 Notification Recipients** card in the addon dashboard.
  Add WhatsApp numbers + Telegram chat IDs by friendly name; each row
  has Test / Enable / Remove buttons.
- Stored in `/share/cinexis/recipients.json` (shared between Python
  ingress and the Node WA service so both can read it).

- New **`POST /notify`** endpoint on the Baileys WA service
  (port 18083 — already exposed to the HA host). Body shape:
  ```json
  {
    "to": "Dad" | ["Dad","Mom"] | "all" | "all_whatsapp" | "all_telegram",
    "message": "...",
    "image_url": "...",
    "image_entity": "camera.front_door",
    "video_url": "...", "document_url": "...", "document_name": "...",
    "automation_id": "automation.front_door_at_night"
  }
  ```
  - **Fan-out**: `to` accepts a single name, an array, or special tokens
    (`all`, `all_whatsapp`, `all_telegram`). Resolves names → channel +
    address via the recipients book.
  - **Camera snapshots**: pass `image_entity: camera.x` and the addon
    snapshots HA's camera via the Supervisor API and attaches the JPEG
    on WhatsApp + Telegram in one call.
  - **Media**: `image_url`, `video_url`, `document_url` (with optional
    `document_name`) work on both channels.
  - **Per-automation defaults**: pass `automation_id` and leave `to`
    empty — the addon falls back to per-automation recipient lists
    stored in `/share/cinexis/automation_recipient_map.json`.
  - Returns per-recipient delivery status: `{ ok, sent: [...], failed: [...] }`.

### HA integration card — rewritten

Now shows ONE clean snippet: `rest_command.cinexis_notify` +
`notify.cinexis_addon` (so HA's GUI automation editor lists it as a
notify service). Three copy-paste examples: fan-out + camera snapshot,
all-recipients power-cut alert, Telegram-only maintenance reminder.

### Re-registration fix (cloud-side, deployed)

After an addon update, the customer no longer drops into
`pending_approval`. New cloud lookup priority in `/api/addon/status`
and `/api/addon/onboard`:

1. exact `ha_node_id`
2. 8-char prefix fallback (legacy Alexa-routing path)
3. `license_key` passed in the request (addon sends its cached key
   on every call) — auto-attaches the node to that customer
4. p2p.db node→license chain — finds the customer who owns the
   bot_licenses row for this node
5. email (for /onboard only) — auto-claims the node if the email is
   known and no other customer has it

Only when **all** miss does a brand-new node land in
`pending_approval`. The addon will recover its previous link
automatically on the very first /status call after the update.

## [1.14.0] - 2026-05-26

### Added — Subscription & Upgrade card (in-addon, no public URLs)

The Upgrade flow now lives entirely inside the addon UI. No more
`https://cinexis.cloud/upgrade?node_id=...` deep links — the node_id
stays inside the addon's authenticated ingress.

- **Always-visible "💎 Subscription & Billing" card** at the top of the
  dashboard. Shows current plan, billing period, status pill, expiry
  date, autopay state, and a legacy badge for grandfathered customers.
- **Upgrade / change plan** button opens an in-modal plan grid. Plans
  are loaded over the authenticated `/api/addon/plans` endpoint.
- Pick a plan → addon POSTs to `/api/addon/subscribe` over ingress;
  cloud creates a Razorpay subscription server-side and returns the
  Razorpay-hosted `short_url`. Addon opens THAT in a new tab — never
  a cinexis.cloud URL. UI then polls every 5s for the plan to flip
  active.
- **Cancel auto-pay** button when a subscription is active. Issues
  `cancel_at_cycle_end=1` so the customer keeps access through the
  paid period.
- **/diag endpoint** — `GET <ingress>/diag` returns the cloud's view
  of this node (plan, entitlements, status). Use it when a feature
  card doesn't appear to find out what the cloud thinks.

### Fixed

- Faster pending-approval refresh: dashboard now reloads every 15s
  while pending (down from 60s) so admin approval lands within ~15s.
- Status cache TTL drops to 10s while in pending_approval so a license
  assignment shows up quickly.
- Locked-card "Upgrade plan →" buttons now open the in-modal upgrade
  flow instead of a public cinexis.cloud page.
- /api/addon/status now auto-heals customers whose `ha_node_id` was
  stored as just the 8-char prefix (Alexa legacy path) — the cloud
  upgrades the row to the full UUID on first authenticated /status
  call, so the customer's dashboard stops flapping into
  pending_approval.

### Companion cloud changes (already deployed)

- New `POST /api/addon/subscribe/cancel` for autopay teardown.
- New `GET /api/addon/plans` (authenticated) for the in-modal grid.
- `POST /api/addon/subscribe` no longer flips the customer's plan
  on subscription creation — only Razorpay webhook (subscription.charged)
  promotes the plan. Stops "ghost upgrades" if the customer abandons
  the Razorpay checkout.
- Webhook reads the intended plan from Razorpay subscription notes
  and promotes both `plan` and `billing_period` on first successful
  charge.

## [1.13.0] - 2026-05-26

### Added — Pending-approval gate + license-tier feature gating

The cloud is now the source of truth for which feature cards a customer
can see. The addon UI calls `/api/addon/status` (cached 30s) which returns:

- `license_status`: `pending_approval`, `trial`, `active`, `pending_payment`,
  `expired`, etc.
- `entitlements`: a per-feature boolean map keyed by plan slug —
  `whatsapp`, `telegram`, `ha_integration`, `voice_alexa`, `voice_google`,
  `voice_siri`, `scenes`.
- `upgrade_url`: a deep link to https://cinexis.cloud/upgrade?node_id=… for
  customers who want to unlock more features.

What changes in the addon UI:

- **Pending-approval banner** — Fresh installs land in `pending_approval`
  until an admin assigns a license tier. The dashboard now shows a single
  '⏳ Waiting for admin approval' card and auto-refreshes every minute.
  No feature cards render until approval lands. Existing customers
  (created before 2026-05-26) were grandfathered server-side, so this
  only affects new installs.

- **Feature-locked cards** — Cards the current plan doesn't include
  (e.g. Voice on the Lite tier) are replaced by a greyed-out card with
  a 🔒 icon and an 'Upgrade plan →' button that opens the cloud upgrade
  page in a new tab.

- **Entitlements come from the cloud** — No more tier→feature logic
  baked into the addon. Plans can change features without an addon update.

### Companion changes on cinexis-cloud (already deployed)

- New status: `pending_approval`. New columns: `trial_starts_at`,
  `trial_ends_at`, `auto_approved_legacy`, `deleted_at`.
- New endpoints: `POST /admin/api/customers/:id/approve` (license_admin
  or owner role), `POST /admin/api/customers/:id/reject`,
  `POST /admin/api/customers/:id/restore`.
- Multi-admin RBAC: roles `owner` / `license_admin` / `viewer`.
- WhatsApp admin notifications (`new_registration`, `admin_approved`,
  `payment_success`, `payment_failed`, `trial_expiring_3d`,
  `subscription_halted`) with configurable recipients at
  `/admin/notifications`.
- Soft-delete for customers (preserves payment history).
- One-time grandfather migration so existing customers don't get bumped
  into pending_approval.

## [1.12.0] - 2026-05-25

### Added — HA-native automation integration

Pivots the notification flow to match GreenAPI: the addon is the WhatsApp
pipe, all the trigger logic lives in HA's automation editor. New dashboard
card '🏠 Use from Home Assistant automations' shows:

- A copy-paste configuration.yaml snippet that registers `rest_command.cinexis_whatsapp`
  (text + image variants) plus an optional `notify.cinexis_whatsapp` wrapper.
- A copy-paste secrets.yaml block for the Telegram bot token + chat_id.
- A working example automation: 'Front door opens at night → WhatsApp +
  Telegram the family group'.
- Three-step GUI alternative (Settings → Automations → Create →
  Action: Call service: rest_command.cinexis_whatsapp).

Telegram is now expected to use HA's built-in `telegram_bot` integration
(no addon involvement) — the addon-side Telegram card is kept for the
quick test flow, but the production path is HA-native `notify.telegram`.

### Added — addon port 18083 published to host

The Baileys WhatsApp service is now reachable from HA Core at
`http://homeassistant.local.hass.io:18083` for the REST commands above.
Port mapping is documented in addon config so the user can remap it if
18083 collides with anything else.

### Fixed — legacy customers stuck on onboarding wizard

Customers with an existing CNX-XXXXX license (no `customer_profile.json`
yet) were being shown the new onboarding wizard instead of the dashboard,
which meant the WhatsApp QR card was hidden. Now the wizard fires only
when BOTH `customer_profile.json` is missing AND no license is cached —
existing paying customers go straight to the full dashboard.

### Fixed — overly-broad bot UA blocklist
(Companion to the server-side fix.) The addon was banned by the
cinexis-cloud server's fail2ban jail because the bot-defense map
was matching `curl/`, `okhttp`, `Java/`, `python-requests`. Server-side
fixed independently — your IP and `223.185.0.0/16` + `27.61.0.0/16` are
now permanently whitelisted in the jail, and those legitimate client
libraries were removed from the bad-UA map. No addon change needed for
this one — documented here so the fix is traceable.

### Card order on the dashboard
1. 📱 WhatsApp (QR + status + test send)  ← most-used, top
2. 💬 Telegram (legacy in-addon — use HA's notify.telegram for production)
3. 🏠 Use from Home Assistant automations  ← NEW
4. 📊 Daily Summary Report
5. ⚡ Notification Rules (now de-emphasised — HA automations are the answer)
6. 🎤 Voice Devices (Alexa)

---

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
