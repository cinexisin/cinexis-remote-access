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
FRPC_CONFIG="${STORAGE_DIR}/frpc.toml"
HEARTBEAT_INTERVAL=300  # 5 minutes
LOG_PREFIX="[Cinexis]"
CUSTOM_NAME="${CUSTOM_NAME:-}"  # Set via add-on options (e.g. "stargate42")
ASSIGNED_NAME=""               # Confirmed subdomain returned by API after registration

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
        log "Generated new node ID: ${uuid}"
    fi
    NODE_ID=$(cat "${NODE_ID_FILE}")
}

ensure_secret() {
    if [ ! -f "${SECRET_FILE}" ]; then
        local secret
        secret=$(openssl rand -hex 32)
        echo "${secret}" > "${SECRET_FILE}"
    fi
    DEVICE_SECRET=$(cat "${SECRET_FILE}")
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
    log "Registering node ${NODE_ID} (${HA_NAME}) with Cinexis Cloud..." >&2
    local body="{\"node_id\":\"${NODE_ID}\",\"device_secret\":\"${DEVICE_SECRET}\",\"ha_name\":\"${HA_NAME}\""
    [ -n "${CUSTOM_NAME}" ] && body="${body},\"custom_name\":\"${CUSTOM_NAME}\""
    body="${body}}"

    local response
    response=$(curl -sf --max-time 15 \
        -X POST "${API}/p2p/register" \
        -H "Content-Type: application/json" \
        -d "${body}" \
        2>/dev/null) || { err "Failed to reach Cinexis API. Check internet connection." >&2; return 1; }

    local status
    status=$(echo "${response}" | jq -r '.status // "error"')
    ASSIGNED_NAME=$(echo "${response}" | jq -r '.custom_name // ""')
    log "Registration status: ${status}" >&2
    [ -n "${ASSIGNED_NAME}" ] && log "Your subdomain: ${ASSIGNED_NAME}.ha1.cinexis.cloud" >&2
    echo "${status}"
}

# ── Heartbeat ──────────────────────────────────────────────────────────────────
send_heartbeat() {
    local response action
    response=$(curl -sf --max-time 15 \
        -X POST "${API}/p2p/heartbeat" \
        -H "Content-Type: application/json" \
        -d "{\"node_id\":\"${NODE_ID}\",\"device_secret\":\"${DEVICE_SECRET}\"}" \
        2>/dev/null) || { warn "Heartbeat failed — network issue?"; return 1; }

    action=$(echo "${response}" | jq -r '.action // "continue"')
    local status
    status=$(echo "${response}" | jq -r '.status // "unknown"')
    log "Heartbeat: status=${status}, action=${action}"

    if [ "${action}" = "stop" ]; then
        err "License ${status}. Stopping tunnel."
        kill_frpc
        return 2
    fi
    return 0
}

# ── Wait for approval ──────────────────────────────────────────────────────────
wait_for_approval() {
    log "Node is pending approval. Waiting for Cinexis admin to approve..."
    log "Your node ID: ${NODE_ID}"
    log "Your HA name: ${HA_NAME}"
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
        log "Waiting for approval... status=${status} (check #${attempt})"

        if [ "${status}" = "active" ]; then
            log "✅ Node approved! Starting tunnel..."
            return 0
        elif [ "${status}" = "blocked" ]; then
            err "Node has been blocked. Contact support."
            exit 1
        fi
    done
}

# ── Write frpc config ──────────────────────────────────────────────────────────
write_frpc_config() {
    # Use custom name if assigned, else first 8 chars of node_id
    if [ -n "${ASSIGNED_NAME}" ]; then
        SUBDOMAIN="${ASSIGNED_NAME}"
    else
        SUBDOMAIN="${NODE_ID:0:8}"
    fi
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
    log "frpc config written — subdomain: ${SUBDOMAIN}"
}

# ── Kill frpc ──────────────────────────────────────────────────────────────────
FRPC_PID=""
kill_frpc() {
    if [ -n "${FRPC_PID}" ] && kill -0 "${FRPC_PID}" 2>/dev/null; then
        kill "${FRPC_PID}" 2>/dev/null || true
        FRPC_PID=""
    fi
}

# ── Start nginx ────────────────────────────────────────────────────────────────
start_nginx() {
    log "Starting nginx HA proxy..."
    nginx -g "daemon off;" &
    NGINX_PID=$!
    sleep 1
    if ! kill -0 "${NGINX_PID}" 2>/dev/null; then
        err "nginx failed to start"
        exit 1
    fi
    log "nginx started (PID ${NGINX_PID})"
}

# ── Start frpc ─────────────────────────────────────────────────────────────────
start_frpc() {
    write_frpc_config
    log "Starting frpc tunnel to ${FRPS_HOST}:${FRPS_PORT}..."
    frpc -c "${FRPC_CONFIG}" &
    FRPC_PID=$!
    sleep 2
    if ! kill -0 "${FRPC_PID}" 2>/dev/null; then
        err "frpc failed to start"
        return 1
    fi
    log "✅ Tunnel established!"
    log "🌐 Your HA URL: https://${SUBDOMAIN}.ha1.cinexis.cloud"
}

# ── Heartbeat loop ─────────────────────────────────────────────────────────────
heartbeat_loop() {
    while true; do
        sleep "${HEARTBEAT_INTERVAL}"
        send_heartbeat || {
            local rc=$?
            if [ $rc -eq 2 ]; then
                # License revoked — stop
                exit 1
            fi
            # Network issue — continue and retry
        }
    done
}

# ── Cleanup on exit ────────────────────────────────────────────────────────────
CLEAN_SHUTDOWN=false
cleanup() {
    CLEAN_SHUTDOWN=true
    log "Shutting down..."
    kill_frpc
    [ -n "${NGINX_PID:-}" ] && kill "${NGINX_PID}" 2>/dev/null || true
    [ -n "${HEARTBEAT_PID:-}" ] && kill "${HEARTBEAT_PID}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ── Main ───────────────────────────────────────────────────────────────────────
main() {
    log "=========================================="
    log " Cinexis Remote Access v1.0.0"
    log "=========================================="

    ensure_storage
    ensure_node_id
    ensure_secret
    get_ha_name

    local status
    status=$(register_node) || { err "Registration failed. Retrying in 60s..."; sleep 60; exec /usr/bin/cinexis-entrypoint.sh; }

    if [ "${status}" = "pending" ]; then
        wait_for_approval
    elif [ "${status}" = "blocked" ]; then
        err "This node is blocked. Contact Cinexis support."
        exit 1
    elif [ "${status}" = "expired" ]; then
        err "License expired. Please renew your Cinexis subscription."
        exit 1
    fi

    start_nginx
    start_frpc

    log "Cinexis Remote Access is running."
    log "Your HA is accessible at: https://${SUBDOMAIN}.ha1.cinexis.cloud"

    # Start heartbeat in background
    heartbeat_loop &
    HEARTBEAT_PID=$!

    # Wait for frpc to exit
    wait "${FRPC_PID}" || true
    [ "${CLEAN_SHUTDOWN}" = "true" ] && exit 0
    err "frpc exited unexpectedly. Restarting in 30s..."
    kill "${HEARTBEAT_PID:-}" 2>/dev/null || true
    sleep 30
    exec /usr/bin/cinexis-entrypoint.sh
}

main "$@"
