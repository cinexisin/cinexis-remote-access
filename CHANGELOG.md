# Changelog — Cinexis Remote Access

All notable changes to this add-on are documented here.
Versioning follows [Semantic Versioning](https://semver.org/).

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
