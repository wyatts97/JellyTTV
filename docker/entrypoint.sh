#!/bin/sh
set -e

PUID=${PUID:-0}
PGID=${PGID:-0}

if [ "$PUID" != "0" ]; then
  if ! getent group jellyttv >/dev/null 2>&1; then
    groupadd -o -g "$PGID" jellyttv 2>/dev/null || addgroup -g "$PGID" jellyttv 2>/dev/null || true
  fi
  if ! getent passwd jellyttv >/dev/null 2>&1; then
    useradd -o -u "$PUID" -g "$PGID" -M -d /app -s /bin/sh jellyttv 2>/dev/null || true
  fi
  chown -R "$PUID:$PGID" /config 2>/dev/null || true
  chown "$PUID:$PGID" /media/twitch 2>/dev/null || true
  RUNNER="gosu $PUID:$PGID"
else
  RUNNER=""
fi

case "$1" in
  api)
    CMD="python -m uvicorn app.main:app --host 0.0.0.0 --port ${JELLYTTV_PORT:-8730} --proxy-headers --forwarded-allow-ips=*"
    ;;
  worker)
    CMD="python -m arq app.worker.settings.WorkerSettings"
    ;;
  *)
    CMD="$*"
    ;;
esac

# shellcheck disable=SC2086
exec $RUNNER $CMD
