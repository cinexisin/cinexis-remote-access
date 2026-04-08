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
HEARTBEAT_INTERVAL=300
LOG_PREFIX="[Cinexis]"
NAME_PREFIX="${NAME_PREFIX:-}"
ADMIN_EMAIL="${ADMIN_EMAIL:-}"
ADMIN_OTP="${ADMIN_OTP:-}"
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

log()  { echo "${LOG_PREFIX} $*"; }
warn() { echo "${LOG_PREFIX} ⚠️  $*"; }
err()  { echo "${LOG_PREFIX} ❌ $*"; }

# ── Storage ────────────────────────────────────────────────────────────────────
ensure_storage() {
    mkdir -p "${STORAGE_DIR}"
}

# ── Node identity ──────────────────────────────────────────────────────────────
ensure_node_id() {
    if [ ! -f "${NODE_ID_FILE}" ]; then
        local uuid
        uuid=$(cat /proc/sys/kernel/random/uuid 2>/dev/null || \
               openssl rand -hex 16 | sed 's/\(.\{8\}\)\(.\{4\}\)\(.\{4\}\)\(.\{4\}\)\(.\{12\}\)/\1-\2-\3-\4-\5/')
        echo "${uuid}" > "${NODE_ID_FILE}"
    fi
    NODE_ID=$(cat "${NODE_ID_FILE}")
}

ensure_secret() {
    if [ ! -f "${SECRET_FILE}" ]; then
        openssl rand -hex 32 > "${SECRET_FILE}"
    fi
    DEVICE_SECRET=$(cat "${SECRET_FILE}")
}

# ── Short ID — auto-generated once, permanent ──────────────────────────────────
ensure_short_id() {
    if [ ! -f "${SHORT_ID_FILE}" ]; then
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

# ── License sync via email + OTP ──────────────────────────────────────────────
sync_license() {
    # If we already have a cached license key, use it
    if [ -f "${LICENSE_KEY_FILE}" ]; then
        LICENSE_KEY=$(cat "${LICENSE_KEY_FILE}")
        log "✅ License loaded from cache (${LICENSE_KEY:0:8}...)"
        return 0
    fi

    # No email configured at all
    if [ -z "${ADMIN_EMAIL}" ]; then
        warn "No admin email configured — Alexa Smart Home will be disabled."
        warn "   Set your cinexis.cloud email in add-on configuration to enable Alexa."
        return 0
    fi

    # Email set but no OTP yet — request one
    if [ -z "${ADMIN_OTP}" ]; then
        log "Requesting license OTP for ${ADMIN_EMAIL}..."
        local resp
        resp=$(curl -sf --max-time 15 \
            -X POST "https://cinexis.cloud/api/node/otp-request" \
            -H "Content-Type: application/json" \
            -d "{\"email\":\"${ADMIN_EMAIL}\"}" 2>/dev/null) || {
            warn "Could not reach Cinexis Cloud to request OTP. Alexa disabled for now."
            return 0
        }
        local ok
        ok=$(echo "${resp}" | jq -r '.ok // false')
        if [ "${ok}" = "true" ]; then
            log "📧 OTP sent to ${ADMIN_EMAIL}."
            log "   ➡  Check your email, then enter the OTP in add-on configuration and restart."
        else
            local err_code
            err_code=$(echo "${resp}" | jq -r '.error // "unknown"')
            warn "OTP request failed: ${err_code}"
        fi
        return 0
    fi

    # Both email and OTP provided — verify and fetch license
    log "Verifying OTP for ${ADMIN_EMAIL}..."
    local resp
    resp=$(curl -sf --max-time 15 \
        -X POST "https://cinexis.cloud/api/node/otp-verify" \
        -H "Content-Type: application/json" \
        -d "{\"email\":\"${ADMIN_EMAIL}\",\"otp\":\"${ADMIN_OTP}\"}" 2>/dev/null) || {
        warn "Could not reach Cinexis Cloud to verify OTP."
        return 0
    }

    local ok
    ok=$(echo "${resp}" | jq -r '.ok // false')
    if [ "${ok}" = "true" ]; then
        LICENSE_KEY=$(echo "${resp}" | jq -r '.license_key // ""')
        local tier expires
        tier=$(echo "${resp}" | jq -r '.tier // "basic"')
        expires=$(echo "${resp}" | jq -r '.expires_at // "0"')
        echo "${LICENSE_KEY}" > "${LICENSE_KEY_FILE}"
        log "✅ License activated — tier=${tier}"
        [ "${expires}" != "0" ] && log "   Expires: $(date -d @${expires} '+%Y-%m-%d' 2>/dev/null || echo ${expires})"
        log "   You can now clear the OTP field in add-on configuration."
    else
        local err_code
        err_code=$(echo "${resp}" | jq -r '.error // "unknown"')
        warn "OTP verification failed: ${err_code}"
        if [ "${err_code}" = "otp_expired" ]; then
            warn "   OTP has expired. Clear the OTP field and restart to request a new one."
        fi
    fi
}

# ── Register with Cinexis API ──────────────────────────────────────────────────
register_node() {
    log "Registering with Cinexis Cloud..." >&2
    local response
    response=$(curl -sf --max-time 15 \
        -X POST "${API}/p2p/register" \
        -H "Content-Type: application/json" \
        -d "{\"node_id\":\"${NODE_ID}\",\"device_secret\":\"${DEVICE_SECRET}\",\"ha_name\":\"${HA_NAME}\",\"custom_name\":\"${SUBDOMAIN}\"}" \
        2>/dev/null) || { err "Failed to reach Cinexis API. Check internet connection." >&2; return 1; }

    local status
    status=$(echo "${response}" | jq -r '.status // "error"')
    log "Status: ${status}" >&2

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
    log "   Node ID:   ${NODE_ID}"
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
    cat > "${FRPC_CONFIG}" << FRPCEOF
serverAddr = "${FRPS_HOST}"
serverPort = ${FRPS_PORT}

[auth]
method = "token"
token = "${FRP_TOKEN}"

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
    INGRESS_PORT="${INGRESS_PORT}" python3 /usr/bin/cinexis-ingress.py &
    INGRESS_PID=$!
    sleep 1
    if kill -0 "${INGRESS_PID}" 2>/dev/null; then
        log "✅ Ingress UI running (pid ${INGRESS_PID}) — open addon UI tab in HA"
    else
        warn "Ingress UI failed to start — voice device management unavailable"
        INGRESS_PID=""
    fi
}

# ── Start Alexa handler ────────────────────────────────────────────────────────
start_alexa_handler() {
    if [ -z "${LICENSE_KEY}" ]; then
        warn "Alexa Smart Home is disabled — no active license."
        if [ -z "${ADMIN_EMAIL}" ]; then
            warn "   Set your cinexis.cloud email in add-on configuration to enable."
        fi
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
cleanup() {
    CLEAN_SHUTDOWN=true
    log "Shutting down..."
    kill_frpc
    [ -n "${NGINX_PID}" ]       && kill "${NGINX_PID}"      2>/dev/null || true
    [ -n "${HEARTBEAT_PID:-}" ] && kill "${HEARTBEAT_PID}"  2>/dev/null || true
    [ -n "${ALEXA_PID}" ]       && kill "${ALEXA_PID}"      2>/dev/null || true
    [ -n "${INGRESS_PID}" ]     && kill "${INGRESS_PID}"    2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ── Main ───────────────────────────────────────────────────────────────────────
main() {
    log "=========================================="
    log " Cinexis Remote Access v1.8.0"
    log " + Alexa Smart Home Integration"
    log " + Ingress Management UI"
    log "=========================================="

    ensure_storage

    # Start ingress UI immediately — HA checks ingress port on startup
    # Must be first so the web UI is available before any network calls
    start_ingress

    ensure_node_id
    ensure_secret
    ensure_short_id
    get_ha_name
    sync_license

    local status
    status=$(register_node) || {
        err "Registration failed. Retrying in 60s..."
        sleep 60
        exec /usr/bin/cinexis-entrypoint.sh
    }

    case "${status}" in
        pending)  wait_for_approval ;;
        blocked)  err "Node blocked. Contact support@cinexis.cloud"; exit 1 ;;
        expired)  err "License expired. Please renew."; exit 1 ;;
        active)   ;;
        *)        err "Unexpected status: ${status}"; sleep 30; exec /usr/bin/cinexis-entrypoint.sh ;;
    esac

    # Start Alexa handler (before FRP) so it's ready when tunnel connects
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

    wait "${FRPC_PID}" || true
    [ "${CLEAN_SHUTDOWN}" = "true" ] && exit 0
    err "frpc exited unexpectedly. Restarting in 30s..."
    kill "${HEARTBEAT_PID:-}" 2>/dev/null || true
    sleep 30
    exec /usr/bin/cinexis-entrypoint.sh
}

main "$@"
