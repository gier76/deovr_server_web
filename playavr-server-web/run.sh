#!/usr/bin/with-contenv bashio

VIDEO_DIR="$(bashio::config 'video_dir')"
mkdir -p "${VIDEO_DIR}" /data/cache

# Optionen, die nicht über bashio::config laufen (optionale Felder, Listen)
SOURCES_JSON="$(jq -c '.sources // []' /data/options.json)"
PUBLIC_URL="$(jq -r '.public_url // ""' /data/options.json)"
UPDATE_YTDLP="$(jq -r 'if .update_ytdlp == false then "false" else "true" end' /data/options.json)"

# yt-dlp beim Start aktualisieren: Seiten ändern sich oft, alte Versionen brechen
if [ "${UPDATE_YTDLP}" = "true" ]; then
    bashio::log.info "Prüfe yt-dlp-Update ..."
    if timeout 120 pip install --no-cache-dir --break-system-packages -U yt-dlp >/tmp/pip.log 2>&1; then
        bashio::log.info "yt-dlp Version: $(yt-dlp --version)"
    else
        bashio::log.warning "yt-dlp-Update fehlgeschlagen, nutze die vorhandene Version"
    fi
fi

export VIDEO_DIR
export SOURCES_JSON
export PUBLIC_URL
export CACHE_DIR=/data/cache
export PORT=8000

bashio::log.info "Videoordner: ${VIDEO_DIR}"
bashio::log.info "Webquellen:  $(echo "${SOURCES_JSON}" | jq 'length')"
if [ -n "${PUBLIC_URL}" ]; then
    bashio::log.info "Basis-URL:   ${PUBLIC_URL}"
fi
bashio::log.info "Playa-URL:   http://<HA-IP>:8000  (DeoVR: /deovr, Diagnose: /debug)"

exec python3 /app/server.py
