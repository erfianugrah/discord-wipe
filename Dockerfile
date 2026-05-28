# discord-wipe — long-running rolling-retention deleter.
#
# Built for servarr; runs as a daemon under compose.
# State and the official Discord export are mounted from the host;
# everything else is baked in.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    EXPORT_DIR=/data/export/Messages \
    STATE_PATH=/data/state/state.json \
    RETENTION_DAYS=7 \
    DELETE_DELAY=1.0 \
    SEARCH_DELAY=30.0 \
    INTERVAL_HOURS=24 \
    WATCH=1

# Single dep. Pin to a known-good range; requests is API-stable.
RUN pip install --no-cache-dir 'requests>=2.32,<3'

WORKDIR /app
COPY discord_wipe.py /app/discord_wipe.py

# Drop privileges. UID/GID match common Unraid `nobody` so bind-mounts
# from /mnt/user/appdata work without chown gymnastics.
ARG PUID=99
ARG PGID=100
RUN groupadd -g ${PGID} app 2>/dev/null || true \
    && useradd -u ${PUID} -g ${PGID} -M -d /app app 2>/dev/null || true \
    && mkdir -p /data/state /data/export \
    && chown -R ${PUID}:${PGID} /app /data

USER ${PUID}:${PGID}

ENTRYPOINT ["python", "/app/discord_wipe.py"]
CMD ["run"]
