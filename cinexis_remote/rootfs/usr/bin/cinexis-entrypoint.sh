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
SUBDOMAIN=""
NGINX_PID=""
FRPC_PID=""
CLEAN_SHUTDOWN=false

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

[[proxies]]
name = "${NODE_ID}"
type = "http"
localIP = "127.0.0.1"
localPort = 8099
customDomains = ["${SUBDOMAIN}.ha1.cinexis.cloud"]
FRPCEOF
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

# ── Cleanup ────────────────────────────────────────────────────────────────────
cleanup() {
    CLEAN_SHUTDOWN=true
    log "Shutting down..."
    kill_frpc
    [ -n "${NGINX_PID}" ] && kill "${NGINX_PID}" 2>/dev/null || true
    [ -n "${HEARTBEAT_PID:-}" ] && kill "${HEARTBEAT_PID}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ── Main ───────────────────────────────────────────────────────────────────────
main() {
    log "=========================================="
    log " Cinexis Remote Access v1.5.0"
    log "=========================================="

    ensure_storage
    ensure_node_id
    ensure_secret
    ensure_short_id
    get_ha_name

    log "Your URL: https://${SUBDOMAIN}.ha1.cinexis.cloud"

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

    start_nginx
    start_frpc

    log "Cinexis Remote Access is running."
    log "Access your HA at: https://${SUBDOMAIN}.ha1.cinexis.cloud"

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
