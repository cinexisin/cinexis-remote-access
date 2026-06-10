# Per-node FRP tunnel token — deploy runbook

Replaces the single shared, grep-able FRP token (`cinexis-frp-secret-2024`,
baked into every published addon image) with per-node HMAC tokens, validated
by an frps server plugin. Closes subdomain-squatting: a leaked image token can
no longer claim another customer's `{sub}.ha1.cinexis.cloud`.

## What's already shipped (zero disruption — DONE)

1. **Cloud (`/opt/cinexis/api/server.js`)** issues a per-node token on
   `/p2p/register` + `/p2p/heartbeat`:
   `frp_token = HMAC-SHA256(<signing secret>, node_id)`.
   Secret self-bootstraps to `/opt/cinexis/data/frp_signing_secret` (0600).
2. **Addon (`cinexis-entrypoint.sh`, v1.18.0+)** fetches that token, stores it
   at `/share/cinexis/frp_token`, and writes it into `frpc.toml` as
   **metadata** (`[metadatas] node_id, node_token`) — NOT as the auth token.
   The auth token stays the legacy shared token, so **frps authenticates
   every client exactly as before**. The metadata is inert until the plugin
   below is in enforce mode. **No regression, no restart needed.**

## What remains — the frps plugin (DO IN A MAINTENANCE WINDOW)

The plugin (`frps-auth-plugin.js`) is built to **fail open** and ships in
**observe** mode (never rejects). Installing it requires one `frps` restart,
which briefly drops all live tunnels (frpc auto-reconnects in seconds). So do
it deliberately, verify reconnection, and only then think about enforce.

### Step 1 — copy + start the plugin (no frps change yet)
```bash
sudo mkdir -p /opt/cinexis/frps-plugin
sudo cp frps-auth-plugin.js /opt/cinexis/frps-plugin/
cd /opt/cinexis/frps-plugin
# better-sqlite3 is already on the box (cinexis-api uses it); symlink or install:
npm init -y && npm install better-sqlite3 || true
MODE=observe pm2 start frps-auth-plugin.js --name cinexis-frps-plugin
pm2 logs cinexis-frps-plugin --lines 5   # confirm "listening ... secret=loaded db=open"
```

### Step 2 — point frps at the plugin (the one restart)
Add to `/opt/cinexis/frps/frps.toml` (keep the existing `[auth]` token block —
the plugin is ADDITIONAL, auth stays token-method):
```toml
[[httpPlugins]]
name = "cinexis-auth"
addr = "127.0.0.1:7501"
path = "/handler"
ops = ["Login", "NewProxy"]
```
Also rotate the weak dashboard password while you're here:
```toml
[webServer]
addr = "127.0.0.1"
port = 7500
user = "admin"
password = "<NEW STRONG PASSWORD>"
```
Then:
```bash
sudo cp /opt/cinexis/frps/frps.toml /opt/cinexis/frps/frps.toml.bak-$(date +%F-%H%M)
# edit the file, then:
pm2 restart cinexis-frps
sleep 8
# VERIFY all tunnels reconnected (expect the same count as before):
curl -s -u admin:<NEW PASS> http://127.0.0.1:7500/api/proxy/http \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(sum(p['status']=='online' for p in d['proxies']),'online')"
```
**Rollback if anything's wrong:** `sudo cp frps.toml.bak-... frps.toml && pm2 restart cinexis-frps`.

### Step 3 — watch observe logs until the fleet has updated
```bash
pm2 logs cinexis-frps-plugin | grep -E "bad_node_token|subdomain_not_owned|legacy_no_meta"
```
- `legacy_no_meta` = an addon still on < v1.18.0 (allowed).
- `bad_node_token` / `subdomain_not_owned` while in observe = would-be rejects.
  Investigate before enforcing.
When you see only `ok` (and acceptable `legacy_no_meta`) for all real nodes,
proceed.

### Step 4 — flip to enforce (no frps restart needed — just the plugin)
```bash
pm2 restart cinexis-frps-plugin --update-env  # with:
MODE=enforce pm2 restart cinexis-frps-plugin
```
Now bad tokens + subdomain squatting are rejected; legacy (no-meta) clients
are still allowed.

### Step 5 — require metadata (full closure, after 100% fleet update)
Once every node runs v1.18.0+ (check the plugin logs show no `legacy_no_meta`
for active nodes), set `REQUIRE_META=1` and restart the plugin. Now even a
client with the leaked shared token but no valid per-node token is rejected —
the shared token alone is worthless. At that point you may also rotate the
shared `auth.token` in frps.toml (coordinate with a fleet push).

## Why it's safe to leave half-done
Steps already shipped are inert without the plugin. The plugin in observe mode
never rejects. Enforce only rejects forged tokens. Each stage is independently
reversible. Nothing here can break a live tunnel except a careless frps
restart — which is why Step 2 keeps a backup + verifies reconnection.
