#!/bin/sh
set -eu

BOT_API_PID=""

cleanup() {
  if [ -n "$BOT_API_PID" ]; then
    kill "$BOT_API_PID" 2>/dev/null || true
    wait "$BOT_API_PID" 2>/dev/null || true
  fi
}

trap cleanup EXIT INT TERM

LOCAL_ENABLED="$(printf '%s' "${BOT_API_LOCAL:-}" | tr '[:upper:]' '[:lower:]')"
if [ "$LOCAL_ENABLED" = "1" ] || [ "$LOCAL_ENABLED" = "true" ] || [ "$LOCAL_ENABLED" = "yes" ]; then
  if [ -z "${TELEGRAM_API_ID:-}" ] || [ -z "${TELEGRAM_API_HASH:-}" ]; then
    echo "BOT_API_LOCAL enabled but TELEGRAM_API_ID / TELEGRAM_API_HASH missing" >&2
    exit 1
  fi

  export TELEGRAM_LOCAL=1
  export TELEGRAM_HTTP_PORT=8081
  export TELEGRAM_HTTP_IP_ADDRESS=127.0.0.1
  export BOT_API_BASE_URL="http://127.0.0.1:8081"

  /docker-entrypoint.sh &
  BOT_API_PID=$!

  echo "Waiting for Local Telegram Bot API..."
  READY=0
  for _ in $(seq 1 90); do
    if nc -z 127.0.0.1 8081 >/dev/null 2>&1; then
      READY=1
      break
    fi
    if ! kill -0 "$BOT_API_PID" 2>/dev/null; then
      echo "Local Telegram Bot API exited during startup" >&2
      exit 1
    fi
    sleep 1
  done
  if [ "$READY" != "1" ]; then
    echo "Local Telegram Bot API did not become ready" >&2
    exit 1
  fi
  echo "Local Telegram Bot API ready"
fi

python3 -m bot &
APP_PID=$!
wait "$APP_PID"
STATUS=$?
cleanup
exit "$STATUS"
