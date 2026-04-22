# Changelog — Cinexis Remote Access

All notable changes to this add-on are documented here.
Versioning follows [Semantic Versioning](https://semver.org/).

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
