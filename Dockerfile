ARG BUILD_FROM
FROM $BUILD_FROM

RUN apk add --no-cache python3 py3-flask py3-pip ffmpeg jq \
    && pip install --no-cache-dir --break-system-packages yt-dlp

WORKDIR /app
COPY server.py /app/server.py
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
