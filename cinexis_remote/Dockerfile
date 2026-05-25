ARG BUILD_FROM
FROM $BUILD_FROM

# ── Base packages ─────────────────────────────────────────────────────────────
# nodejs + npm for the Baileys-based WhatsApp Web service (cinexis-wa.js).
# Alpine ships node 20 in v3.20+.
RUN apk add --no-cache \
    bash \
    curl \
    jq \
    nginx \
    nodejs \
    npm \
    openssl \
    python3 \
    py3-pip

# Python WebSocket client for cinexis-events.py (HA event listener)
RUN pip3 install --no-cache-dir --break-system-packages \
    'websocket-client==1.8.0'

# ── frpc binary ───────────────────────────────────────────────────────────────
ARG BUILD_ARCH
RUN ARCH="${BUILD_ARCH}" && \
    if   [ "$ARCH" = "aarch64" ]; then FRP_ARCH="arm64"; \
    elif [ "$ARCH" = "armv7" ];   then FRP_ARCH="arm"; \
    else                                FRP_ARCH="amd64"; fi && \
    curl -fsSL "https://github.com/fatedier/frp/releases/download/v0.61.1/frp_0.61.1_linux_${FRP_ARCH}.tar.gz" \
      -o /tmp/frp.tar.gz && \
    tar -xzf /tmp/frp.tar.gz -C /tmp && \
    mv /tmp/frp_0.61.1_linux_${FRP_ARCH}/frpc /usr/local/bin/frpc && \
    chmod +x /usr/local/bin/frpc && \
    rm -rf /tmp/frp*

# ── Copy addon rootfs ─────────────────────────────────────────────────────────
COPY rootfs /

# ── Install WhatsApp service deps (Baileys + express + qrcode + pino) ────────
# Baileys pulls in libsignal/native crypto — needs python3 + make + g++ on
# Alpine for the gyp build. Strip the build chain after install to keep the
# image small.
RUN apk add --no-cache --virtual .build-deps git make g++ python3-dev \
 && cd /usr/lib/cinexis-wa \
 && npm install --omit=dev --no-audit --no-fund \
 && apk del .build-deps \
 && rm -rf /root/.npm /tmp/*

# ── Permissions ───────────────────────────────────────────────────────────────
RUN chmod +x /usr/bin/cinexis-entrypoint.sh \
              /usr/bin/cinexis-alexa.py \
              /usr/bin/cinexis-ingress.py \
              /usr/bin/cinexis-events.py

CMD ["/usr/bin/cinexis-entrypoint.sh"]
