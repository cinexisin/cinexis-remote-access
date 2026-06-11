#!/usr/bin/env bash
set -euo pipefail

# ── Config ─────────────────────────────────────────────────────────────────────
API="${CINEXIS_API:-https://api1.cinexis.cloud}"
FRPS_HOST="${FRPS_HOST:-frp1.cinexis.cloud}"
FRPS_PORT="${FRPS_PORT:-7000}"
FRP_TOKEN="${FRP_TOKEN:-cinexis-frp-secret-2024}"
STORAGE_DIR="/share/cinexis"
NODE_ID_FILE="${STORAGE_DIR}/node_id"
SECRET_FILE="${STORAGE_DIR}/device_secret"
SHORT_ID_FILE="${STORAGE_DIR}/short_id"
FRPC_CONFIG="${STORAGE_DIR}/frpc.toml"
FRP_TOKEN_FILE="${STORAGE_DIR}/frp_token"   # per-node tunnel token from cloud
HEARTBEAT_INTERVAL=300
LOG_PREFIX="[Cinexis]"
NAME_PREFIX="${NAME_PREFIX:-}"
LICENSE_KEY_FILE="${STORAGE_DIR}/license_key"
LICENSE_KEY=""
SUBDOMAIN=""
NGINX_PID=""
FRPC_PID=""
ALEXA_PID=""
INGRESS_PID=""
CLEAN_SHUTDOWN=false
ALEXA_PORT=18081
INGRESS_PORT="${INGRESS_PORT:-18082}"
WA_PORT="${WA_PORT:-18083}"
WA_PID=""

# ── Alexa backend selection (from options.json) ───────────────────────────────
# alexa_backend: "self" (addon handles Alexa) or "bot" (proxy to cinexis-bot)
# bot_host:      "host:port" of the cinexis-bot when alexa_backend=bot
ALEXA_BACKEND=$(jq -r '.alexa_backend // "self"' /data/options.json 2>/dev/null || echo "self")
BOT_HOST=$(jq -r '.bot_host // ""' /data/options.json 2>/dev/null || echo "")
# Normalise bot_host: strip scheme if pasted as http://...
BOT_HOST="${BOT_HOST#http://}"
BOT_HOST="${BOT_HOST#https://}"
BOT_HOST="${BOT_HOST%/}"

log()  { echo "${LOG_PREFIX} $*"; }
warn() { echo "${LOG_PREFIX} ⚠️  $*"; }
err()  { echo "${LOG_PREFIX} ❌ $*"; }

# ── Storage ────────────────────────────────────────────────────────────────────
ensure_storage() {
    mkdir -p "${STORAGE_DIR}"
}

# ── Node identity ──────────────────────────────────────────────────────────────
# NOTE: Use `-s` (file exists AND non-empty) rather than `-f` (file exists).
# A truncated/zero-byte file from an interrupted previous write would otherwise
# be read as an empty string and cause the API to return 400 "missing fields".
ensure_node_id() {
    if [ ! -s "${NODE_ID_FILE}" ]; then
        local uuid
        uuid=$(cat /proc/sys/kernel/random/uuid 2>/dev/null || \
               openssl rand -hex 16 | sed 's/\(.\{8\}\)\(.\{4\}\)\(.\{4\}\)\(.\{4\}\)\(.\{12\}\)/\1-\2-\3-\4-\5/')
        if [ -z "${uuid}" ]; then err "Failed to generate node UUID — /proc and openssl both unavailable" >&2; exit 1; fi
        echo "${uuid}" > "${NODE_ID_FILE}"
    fi
    NODE_ID=$(cat "${NODE_ID_FILE}")
    if [ -z "${NODE_ID}" ]; then err "node_id file empty after write — check ${STORAGE_DIR} permissions" >&2; exit 1; fi
}

ensure_secret() {
    if [ ! -s "${SECRET_FILE}" ]; then
        openssl rand -hex 32 > "${SECRET_FILE}"
    fi
    DEVICE_SECRET=$(cat "${SECRET_FILE}")
    if [ -z "${DEVICE_SECRET}" ]; then err "device_secret file empty after write — check ${STORAGE_DIR} permissions" >&2; exit 1; fi
}

# ── Short ID — auto-generated once, permanent ──────────────────────────────────
ensure_short_id() {
    if [ ! -s "${SHORT_ID_FILE}" ]; then
        # 8 random lowercase alphanumeric chars — unguessable, permanent
        openssl rand -hex 4 > "${SHORT_ID_FILE}"
    fi
    local short_id
    short_id=$(cat "${SHORT_ID_FILE}")

    # Build subdomain: optional prefix + short id
    if [ -n "${NAME_PREFIX}" ]; then
        # Sanitise prefix: lowercase, letters/numbers only, max 15 chars
        local clean_prefix
        clean_prefix=$(echo "${NAME_PREFIX}" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9' | cut -c1-15)
        if [ -n "${clean_prefix}" ]; then
            SUBDOMAIN="${clean_prefix}-${short_id}"
        else
            SUBDOMAIN="${short_id}"
        fi
    else
        SUBDOMAIN="${short_id}"
    fi
}

# ── Get HA name ────────────────────────────────────────────────────────────────
get_ha_name() {
    HA_NAME=$(curl -sf --max-time 5 \
        -H "Authorization: Bearer ${SUPERVISOR_TOKEN:-}" \
        "http://supervisor/core/api/config" 2>/dev/null | \
        jq -r '.location_name // "Home Assistant"' 2>/dev/null || echo "Home Assistant")
}

# ── License sync — reads from cache only (OTP flow handled by ingress UI) ──────
sync_license() {
    if [ -f "${LICENSE_KEY_FILE}" ]; then
        LICENSE_KEY=$(cat "${LICENSE_KEY_FILE}")
        log "✅ License loaded from cache (${LICENSE_KEY:0:8}...)"
    else
        warn "No license cached — open the Cinexis panel in HA sidebar to activate Alexa."
    fi
}

# ── Register with Cinexis API ──────────────────────────────────────────────────
register_node() {
    log "Registering with Cinexis Cloud..." >&2
    local response http_code curl_exit
    # Capture the HTTP status separately so we can give a useful error message:
    #   - exit 6/7/28/35 → genuine network/DNS/TLS/timeout problem
    #   - 4xx HTTP code → server reached but rejected the payload
    #   - 5xx HTTP code → server is up but errored
    response=$(curl -s --max-time 15 -w "\n%{http_code}" \
        -X POST "${API}/p2p/register" \
        -H "Content-Type: application/json" \
        -d "{\"node_id\":\"${NODE_ID}\",\"device_secret\":\"${DEVICE_SECRET}\",\"ha_name\":\"${HA_NAME}\",\"custom_name\":\"${SUBDOMAIN}\"}" \
        2>/dev/null) || curl_exit=$?
    if [ -n "${curl_exit:-}" ]; then
        err "Could not reach ${API} (curl exit ${curl_exit}). Check DNS / TLS / firewall." >&2
        return 1
    fi
    http_code="${response##*$'\n'}"
    response="${response%$'\n'*}"
    if [ "${http_code}" != "200" ]; then
        err "Cinexis API rejected the request (HTTP ${http_code}): ${response}" >&2
        if [ "${http_code}" = "400" ]; then
            err "Hint: a previous run may have left an empty file in ${STORAGE_DIR}. Try: rm ${NODE_ID_FILE} ${SECRET_FILE} && restart the addon." >&2
        fi
        return 1
    fi

    local status
    status=$(echo "${response}" | jq -r '.status // "error"')
    log "Status: ${status}" >&2

    # Capture the per-node FRP token (if the cloud issued one). Persisted so
    # write_frpc_config can embed it as metadata. Older cloud versions omit
    # the field → we simply keep no token and frpc still works on the legacy
    # shared auth token.
    local node_token
    node_token=$(echo "${response}" | jq -r '.frp_token // empty')
    if [ -n "${node_token}" ]; then
        printf '%s' "${node_token}" > "${FRP_TOKEN_FILE}" 2>/dev/null && chmod 600 "${FRP_TOKEN_FILE}" 2>/dev/null || true
    fi

    # Also register with cinexis.cloud for Alexa routing (non-blocking — best effort)
    # This links SUBDOMAIN (= ha_node_id) to the customer's license for Alexa directive routing.
    local alexa_payload
    alexa_payload="{\"node_id\":\"${NODE_ID}\",\"ha_node_id\":\"${SUBDOMAIN}\",\"device_secret\":\"${DEVICE_SECRET}\",\"ha_name\":\"${HA_NAME}\""
    [ -n "${LICENSE_KEY}" ] && alexa_payload="${alexa_payload},\"license_key\":\"${LICENSE_KEY}\""
    alexa_payload="${alexa_payload}}"
    curl -sf --max-time 10 -X POST "https://cinexis.cloud/api/node/register" \
        -H "Content-Type: application/json" -d "${alexa_payload}" > /dev/null 2>&1 || true

    echo "${status}"
}

# ── Heartbeat ──────────────────────────────────────────────────────────────────
send_heartbeat() {
    local response action status
    response=$(curl -sf --max-time 15 \
        -X POST "${API}/p2p/heartbeat" \
        -H "Content-Type: application/json" \
        -d "{\"node_id\":\"${NODE_ID}\",\"device_secret\":\"${DEVICE_SECRET}\"}" \
        2>/dev/null) || { warn "Heartbeat failed — network issue?"; return 1; }

    action=$(echo "${response}" | jq -r '.action // "continue"')
    status=$(echo "${response}" | jq -r '.status // "unknown"')
    log "Heartbeat: status=${status}"

    if [ "${action}" = "stop" ]; then
        err "License ${status}. Stopping tunnel."
        kill_frpc
        # Also kill Alexa handler — no valid license means no voice control
        if [ -n "${ALEXA_PID}" ] && kill -0 "${ALEXA_PID}" 2>/dev/null; then
            warn "Stopping Alexa handler (license invalid)."
            kill "${ALEXA_PID}" 2>/dev/null || true
            ALEXA_PID=""
        fi
        # Clear cached license so ingress UI shows re-activation form
        rm -f "${LICENSE_KEY_FILE}"
        return 2
    fi
    return 0
}

# ── Wait for approval ──────────────────────────────────────────────────────────
wait_for_approval() {
    log "⏳ Pending approval by Cinexis admin..."
    # Only show a short prefix — the full node_id is an installation identifier
    # and addon logs are often pasted into support chats / public forums.
    log "   Node ID:   ${NODE_ID:0:8}… (full id is in your Cinexis dashboard)"
    log "   HA Name:   ${HA_NAME}"
    log "   Subdomain: ${SUBDOMAIN}.ha1.cinexis.cloud"
    local attempt=0
    while true; do
        sleep 30
        attempt=$((attempt + 1))
        local response status
        response=$(curl -sf --max-time 15 \
            -X POST "${API}/p2p/heartbeat" \
            -H "Content-Type: application/json" \
            -d "{\"node_id\":\"${NODE_ID}\",\"device_secret\":\"${DEVICE_SECRET}\"}" \
            2>/dev/null) || { warn "Heartbeat failed, retrying..."; continue; }

        status=$(echo "${response}" | jq -r '.status // "pending"')
        log "Waiting for approval... (check #${attempt})"

        if [ "${status}" = "active" ]; then
            log "✅ Approved! Starting tunnel..."
            return 0
        elif [ "${status}" = "blocked" ]; then
            err "Node blocked. Contact support@cinexis.cloud"
            exit 1
        fi
    done
}

# ── Write frpc config ──────────────────────────────────────────────────────────
write_frpc_config() {
    # Per-node tunnel token (HMAC issued by the cloud at registration). Sent as
    # frpc METADATA — NOT as the auth token — so frps's existing token-method
    # auth keeps working unchanged (zero regression). A future frps login plugin
    # validates node_token == HMAC(secret, node_id) and that this node owns the
    # subdomain, closing the shared-token squatting hole. Until that plugin is
    # in enforce mode, this metadata is simply ignored by frps.
    local node_token=""
    [ -s "${FRP_TOKEN_FILE}" ] && node_token=$(cat "${FRP_TOKEN_FILE}" 2>/dev/null | tr -d '[:space:]')
    cat > "${FRPC_CONFIG}" << FRPCEOF
serverAddr = "${FRPS_HOST}"
serverPort = ${FRPS_PORT}

[auth]
method = "token"
token = "${FRP_TOKEN}"

[metadatas]
node_id = "${NODE_ID}"
node_token = "${node_token}"

[log]
level = "info"

# HA remote access tunnel — https://${SUBDOMAIN}.ha1.cinexis.cloud
[[proxies]]
name = "${NODE_ID}"
type = "http"
localIP = "127.0.0.1"
localPort = 8099
customDomains = ["${SUBDOMAIN}.ha1.cinexis.cloud"]

# Alexa Smart Home tunnel — https://${SUBDOMAIN}alexa.ha1.cinexis.cloud
# cinexis.cloud routes Alexa directives here via X-Cinexis-Secret
[[proxies]]
name = "${NODE_ID}-alexa"
type = "http"
localIP = "127.0.0.1"
localPort = ${ALEXA_PORT}
customDomains = ["${SUBDOMAIN}alexa.ha1.cinexis.cloud"]
FRPCEOF
    log "HA URL   : https://${SUBDOMAIN}.ha1.cinexis.cloud"
    log "Alexa URL: https://${SUBDOMAIN}alexa.ha1.cinexis.cloud"
}

# ── Process management ─────────────────────────────────────────────────────────
kill_frpc() {
    if [ -n "${FRPC_PID}" ] && kill -0 "${FRPC_PID}" 2>/dev/null; then
        kill "${FRPC_PID}" 2>/dev/null || true
        FRPC_PID=""
    fi
}

start_nginx() {
    log "Starting nginx proxy..."
    nginx -g "daemon off;" &
    NGINX_PID=$!
    sleep 1
    kill -0 "${NGINX_PID}" 2>/dev/null || { err "nginx failed to start"; exit 1; }
}

start_frpc() {
    write_frpc_config
    log "Connecting tunnel to ${FRPS_HOST}:${FRPS_PORT}..."
    frpc -c "${FRPC_CONFIG}" &
    FRPC_PID=$!
    sleep 2
    kill -0 "${FRPC_PID}" 2>/dev/null || { err "frpc failed to start"; return 1; }
    log "✅ Tunnel established!"
    log "🌐 Your HA URL: https://${SUBDOMAIN}.ha1.cinexis.cloud"
}

# ── Heartbeat loop ─────────────────────────────────────────────────────────────
heartbeat_loop() {
    while true; do
        sleep "${HEARTBEAT_INTERVAL}"
        send_heartbeat || { [ $? -eq 2 ] && exit 1; }
    done
}

# ── Start ingress UI ───────────────────────────────────────────────────────────
start_ingress() {
    log "Starting ingress UI on port ${INGRESS_PORT}..."
    # Pass WA_SERVICE_URL so the ingress proxy always points at the right
    # WhatsApp service port even if WA_PORT is overridden in config.
    INGRESS_PORT="${INGRESS_PORT}" \
    WA_SERVICE_URL="http://127.0.0.1:${WA_PORT}" \
        python3 /usr/bin/cinexis-ingress.py &
    INGRESS_PID=$!
    sleep 1
    if kill -0 "${INGRESS_PID}" 2>/dev/null; then
        log "✅ Ingress UI running (pid ${INGRESS_PID}) — open addon UI tab in HA"
    else
        warn "Ingress UI failed to start — voice device management unavailable"
        INGRESS_PID=""
    fi
}

# ── WhatsApp Web service (Baileys) ─────────────────────────────────────────────
# Hosts the owner's own personal WhatsApp Web session. Started once at boot;
# the Node process auto-reconnects with backoff if WhatsApp drops the link.
# Auth lives in /share/cinexis/wa-auth so a pairing survives addon updates.
start_wa() {
    if [ ! -f /usr/lib/cinexis-wa/cinexis-wa.js ]; then
        warn "cinexis-wa.js not installed — WhatsApp notifications disabled"
        return
    fi
    log "Starting WhatsApp Web service on port ${WA_PORT}..."
    cd /usr/lib/cinexis-wa
    # tee to both the persistent log AND container stdout so Baileys crashes
    # are visible in HA's addon "Log" tab (not just the hidden share file).
    WA_PORT="${WA_PORT}" WA_AUTH_DIR="${STORAGE_DIR}/wa-auth" \
    WA_SHARED_SECRET="${DEVICE_SECRET:-}" \
        node cinexis-wa.js 2>&1 | while IFS= read -r line; do
            echo "[wa] ${line}"
            echo "${line}" >> "${STORAGE_DIR}/cinexis-wa.log"
        done &
    WA_PID=$!
    cd - > /dev/null
    sleep 2
    if kill -0 "${WA_PID}" 2>/dev/null; then
        log "✅ WhatsApp Web service running (pid ${WA_PID}) — open addon UI to scan QR"
    else
        warn "WhatsApp Web service failed to start — check ${STORAGE_DIR}/cinexis-wa.log"
        WA_PID=""
    fi
}

# ── Alexa backend: write conditional nginx proxy config ──────────────────────
# When alexa_backend=bot, nginx (on port 18081) forwards /voice/alexa/internal
# to the cinexis-bot at BOT_HOST. The bot's alexa_device_secret must match
# this addon's device_secret (see addon README for the copy-paste step).
write_alexa_proxy_config() {
    local target="/etc/nginx/http.d/cinexis-alexa-proxy.conf"
    if [ "${ALEXA_BACKEND}" = "bot" ] && [ -n "${BOT_HOST}" ]; then
        cat > "${target}" << NGINXEOF
# Generated by cinexis-entrypoint.sh — alexa_backend=bot
# Forwards Alexa directives to cinexis-bot at ${BOT_HOST}
server {
    listen ${ALEXA_PORT};
    server_name _;

    location /voice/alexa/internal {
        proxy_pass http://${BOT_HOST}/voice/alexa/internal;
        proxy_http_version 1.1;
        proxy_set_header Host              \$http_host;
        proxy_set_header X-Cinexis-Secret  \$http_x_cinexis_secret;
        proxy_set_header Content-Type      \$http_content_type;
        proxy_read_timeout 10s;
        proxy_buffering    off;
    }
    location / {
        return 404;
    }
}
NGINXEOF
        log "Alexa proxy: forwarding :${ALEXA_PORT} → http://${BOT_HOST}/voice/alexa/internal"
    else
        # Remove any stale proxy config from previous 'bot' runs
        rm -f "${target}"
    fi
}

# ── Start Alexa handler ────────────────────────────────────────────────────────
start_alexa_handler() {
    if [ -z "${LICENSE_KEY}" ]; then
        warn "Alexa Smart Home is disabled — no active license."
        warn "   Open the Cinexis panel in the HA sidebar to activate."
        ALEXA_PID=""
        return 0
    fi
    if [ "${ALEXA_BACKEND}" = "bot" ]; then
        if [ -z "${BOT_HOST}" ]; then
            err "alexa_backend=bot but bot_host is empty. Set bot_host in addon config (e.g. 192.168.1.50:3000)."
            ALEXA_PID=""
            return 0
        fi
        log "✅ Alexa backend: bot (delegating to ${BOT_HOST} via nginx proxy)"
        log "   Ensure cinexis-bot's alexa_device_secret matches this addon's device_secret"
        ALEXA_PID=""
        return 0
    fi
    log "Starting Alexa Smart Home handler on port ${ALEXA_PORT}..."
    ALEXA_HANDLER_PORT="${ALEXA_PORT}" python3 /usr/bin/cinexis-alexa.py &
    ALEXA_PID=$!
    sleep 1
    if kill -0 "${ALEXA_PID}" 2>/dev/null; then
        log "✅ Alexa handler running (pid ${ALEXA_PID})"
        log "   Say 'Alexa, discover devices' after linking your account"
    else
        warn "Alexa handler failed to start — voice control will not work"
        ALEXA_PID=""
    fi
}

# ── Cleanup ────────────────────────────────────────────────────────────────────
# ── Service watchdog ─────────────────────────────────────────────────────────
# None of the backgrounded services (ingress, WA, alexa) had restart-on-crash.
# If any died — a Baileys throw, a python exception — it stayed dead for the
# whole addon lifetime and the customer lost that feature silently. This loop
# checks every 20s and respawns a dead child, capped at 5 respawns each to
# avoid a crash-loop hammering CPU. frpc keeps its own exec-restart via main().
WATCHDOG_PID=""
declare -A CRASHES
service_watchdog() {
    while true; do
        sleep 20
        [ "${CLEAN_SHUTDOWN}" = "true" ] && return 0

        # Ingress UI (always expected to run)
        if [ -n "${INGRESS_PID}" ] && ! kill -0 "${INGRESS_PID}" 2>/dev/null; then
            CRASHES[ingress]=$(( ${CRASHES[ingress]:-0} + 1 ))
            if [ "${CRASHES[ingress]}" -le 5 ]; then
                warn "Ingress UI died — respawning (#${CRASHES[ingress]})"
                start_ingress
            fi
        fi

        # WhatsApp Web service (only if installed). Never give up permanently —
        # after 5 fast respawns, back off to one retry per ~5 min (every 15th
        # 20s tick) so a transient cause (corrupt auth, brief resource crunch)
        # still recovers on its own instead of leaving WhatsApp dead forever.
        if [ -f /usr/lib/cinexis-wa/cinexis-wa.js ]; then
            if [ -n "${WA_PID}" ] && ! kill -0 "${WA_PID}" 2>/dev/null; then
                CRASHES[wa]=$(( ${CRASHES[wa]:-0} + 1 ))
                if [ "${CRASHES[wa]}" -le 5 ]; then
                    warn "WhatsApp service died — respawning (#${CRASHES[wa]})"
                    start_wa
                else
                    WA_BACKOFF=$(( ${WA_BACKOFF:-0} + 1 ))
                    if [ "${CRASHES[wa]}" -eq 6 ]; then
                        warn "WhatsApp service crashed 5× — slowing retries to every ~5 min. Check the addon Log tab for the cause."
                    fi
                    if [ "${WA_BACKOFF}" -ge 15 ]; then
                        WA_BACKOFF=0
                        warn "WhatsApp service slow-retry — respawning"
                        start_wa
                    fi
                fi
            fi
        fi

        # Alexa handler (only when self-hosted backend + licensed)
        if [ "${ALEXA_BACKEND}" != "bot" ] && [ -n "${LICENSE_KEY}" ]; then
            if [ -n "${ALEXA_PID}" ] && ! kill -0 "${ALEXA_PID}" 2>/dev/null; then
                CRASHES[alexa]=$(( ${CRASHES[alexa]:-0} + 1 ))
                if [ "${CRASHES[alexa]}" -le 5 ]; then
                    warn "Alexa handler died — respawning (#${CRASHES[alexa]})"
                    start_alexa_handler
                fi
            fi
        fi
    done
}

cleanup() {
    CLEAN_SHUTDOWN=true
    log "Shutting down..."
    kill_frpc
    [ -n "${WATCHDOG_PID}" ]    && kill "${WATCHDOG_PID}"   2>/dev/null || true
    [ -n "${NGINX_PID}" ]       && kill "${NGINX_PID}"      2>/dev/null || true
    [ -n "${HEARTBEAT_PID:-}" ] && kill "${HEARTBEAT_PID}"  2>/dev/null || true
    [ -n "${ALEXA_PID}" ]       && kill "${ALEXA_PID}"      2>/dev/null || true
    [ -n "${INGRESS_PID}" ]     && kill "${INGRESS_PID}"    2>/dev/null || true
    [ -n "${WA_PID}" ]          && kill "${WA_PID}"         2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ── Main ───────────────────────────────────────────────────────────────────────
main() {
    log "=========================================="
    log " Cinexis Remote Access v1.19.4"
    log " + Alexa Smart Home Integration"
    log " + Ingress Management UI"
    log "=========================================="

    ensure_storage

    # Start ingress UI immediately — HA checks ingress port on startup
    # Must be first so the web UI is available before any network calls
    start_ingress

    # ── Start the WhatsApp Web service NOW, before any cloud/approval gate ──
    # The owner's personal WhatsApp pairing (the QR) has NOTHING to do with
    # license approval or cloud reachability. Previously start_wa ran only
    # AFTER register_node succeeded AND the node was p2p-'active' — so during
    # a cloud outage (registration loop blocks) or while pending approval
    # (wait_for_approval loops forever) the WA service never launched and the
    # QR never appeared. Starting it here makes the QR available the instant
    # the addon boots, in every state. (Alexa / nginx / frpc legitimately
    # need registration, so they stay gated below.)
    start_wa

    ensure_node_id
    ensure_secret
    ensure_short_id
    get_ha_name
    sync_license

    # ── Registration retry loop (in-place) ───────────────────────────────
    # PREVIOUSLY this did `exec /usr/bin/cinexis-entrypoint.sh` on failure,
    # which replaced the bash process and killed every spawned child —
    # including the ingress UI. HA's "addon ready?" probe then failed and
    # the customer saw "addon seems not ready" in their browser.
    #
    # Now we loop in-place with exponential backoff so the ingress / WA /
    # events services stay alive across cloud-registration retries. Customer
    # can still open the addon UI and configure WhatsApp + notification rules
    # even when the cloud is unreachable.
    local status retry_count=0 backoff=15
    while ! status=$(register_node); do
        retry_count=$((retry_count + 1))
        # Cap backoff at 5 min — we want fast retry when the network blip
        # is short, but don't hammer the cloud during a real outage.
        backoff=$(( retry_count < 5 ? 15 * (1 << (retry_count - 1)) : 300 ))
        if [ "$backoff" -gt 300 ]; then backoff=300; fi
        warn "Registration retry #$retry_count in ${backoff}s. The addon UI stays open the whole time — open it to configure WhatsApp / Telegram / rules even now."
        sleep "$backoff"
    done
    if [ "$retry_count" -gt 0 ]; then
        log "Registration succeeded after $retry_count retry(ies)."
    fi

    case "${status}" in
        pending)  wait_for_approval ;;
        blocked)  err "Node blocked. Contact support@cinexis.cloud"; exit 1 ;;
        expired)  err "License expired. Please renew."; exit 1 ;;
        active)   ;;
        *)        err "Unexpected status: ${status}"; sleep 30; exec /usr/bin/cinexis-entrypoint.sh ;;
    esac

    # Start Alexa handler (before FRP) so it's ready when tunnel connects.
    # (start_wa already ran near the top of main(), before the approval gate.)
    write_alexa_proxy_config
    start_alexa_handler

    start_nginx
    start_frpc

    log "Cinexis Remote Access is running."
    log "🏠 HA access : https://${SUBDOMAIN}.ha1.cinexis.cloud"
    if [ -n "${LICENSE_KEY}" ]; then
        log "🔊 Alexa     : link at cinexis.cloud — Alexa node ID: ${SUBDOMAIN}"
    else
        log "🔒 Alexa     : disabled — enter your cinexis.cloud email in configuration"
    fi

    heartbeat_loop &
    HEARTBEAT_PID=$!

    # Respawn any backgrounded child (ingress/WA/alexa) that dies.
    service_watchdog &
    WATCHDOG_PID=$!

    wait "${FRPC_PID}" || true
    [ "${CLEAN_SHUTDOWN}" = "true" ] && exit 0
    err "frpc exited unexpectedly. Restarting in 30s..."
    kill "${HEARTBEAT_PID:-}" 2>/dev/null || true
    sleep 30
    exec /usr/bin/cinexis-entrypoint.sh
}

main "$@"
