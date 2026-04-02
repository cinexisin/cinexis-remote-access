ARG BUILD_FROM
FROM $BUILD_FROM

# Install dependencies
RUN apk add --no-cache \
    curl \
    nginx \
    bash \
    openssl \
    jq

# Install frpc binary based on arch
ARG BUILD_ARCH
RUN ARCH="${BUILD_ARCH}" && \
    if [ "$ARCH" = "aarch64" ]; then FRP_ARCH="arm64"; \
    elif [ "$ARCH" = "armv7" ]; then FRP_ARCH="arm"; \
    else FRP_ARCH="amd64"; fi && \
    curl -fsSL "https://github.com/fatedier/frp/releases/download/v0.61.1/frp_0.61.1_linux_${FRP_ARCH}.tar.gz" \
      -o /tmp/frp.tar.gz && \
    tar -xzf /tmp/frp.tar.gz -C /tmp && \
    mv /tmp/frp_0.61.1_linux_${FRP_ARCH}/frpc /usr/local/bin/frpc && \
    chmod +x /usr/local/bin/frpc && \
    rm -rf /tmp/frp*

# Copy rootfs
COPY rootfs /

RUN chmod +x /usr/bin/cinexis-entrypoint.sh

CMD ["/usr/bin/cinexis-entrypoint.sh"]
