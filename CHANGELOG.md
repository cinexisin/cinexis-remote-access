# Changelog — Cinexis Remote Access

All notable changes to this add-on are documented here.
Versioning follows [Semantic Versioning](https://semver.org/).

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
