ARG BUILD_FROM
FROM $BUILD_FROM
ARG BUILD_ARCH

RUN apk add --no-cache python3 py3-flask py3-pip ffmpeg jq curl ca-certificates \
    && pip install --no-cache-dir --break-system-packages yt-dlp

# cloudflared nur für die optionale Funktion thumb_tunnel (Vorschaubilder per HTTPS). Schlägt der
# Download fehl, wird das Add-on trotzdem gebaut, nur thumb_tunnel funktioniert dann nicht.
RUN case "${BUILD_ARCH}" in \
        amd64) CF=cloudflared-linux-amd64 ;; \
        aarch64) CF=cloudflared-linux-arm64 ;; \
        *) CF="" ;; \
    esac; \
    if [ -n "$CF" ]; then \
        curl -fsSL -o /usr/local/bin/cloudflared \
            "https://github.com/cloudflare/cloudflared/releases/latest/download/$CF" \
        && chmod +x /usr/local/bin/cloudflared \
        || echo "cloudflared nicht installiert (thumb_tunnel nicht verfügbar)"; \
    fi

WORKDIR /app
COPY server.py /app/server.py
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
