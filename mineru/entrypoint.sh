#!/usr/bin/env bash
set -Eeuo pipefail

mode="${MINERU_SERVICE:-${1:-api}}"
if [[ $# -gt 0 ]]; then
  shift
fi

mkdir -p /parsed/_api_tasks /parsed/cache /parsed/raw /parsed/documents /logs

case "${mode}" in
  api)
    mineru-api --host 0.0.0.0 --port 8001 >>/logs/mineru-upstream.log 2>&1 &
    upstream_pid=$!

    python -m uvicorn app:app \
      --host 0.0.0.0 \
      --port 8000 \
      --workers 1 \
      --no-access-log &
    proxy_pid=$!

    terminate() {
      trap - TERM INT
      kill -TERM "${proxy_pid}" 2>/dev/null || true
      kill -TERM "${upstream_pid}" 2>/dev/null || true
      wait "${proxy_pid}" 2>/dev/null || true
      wait "${upstream_pid}" 2>/dev/null || true
    }
    trap 'terminate; exit 143' TERM INT

    # Exit if either half of the service fails; Compose will restart both.
    set +e
    wait -n "${upstream_pid}" "${proxy_pid}"
    status=$?
    set -e
    terminate
    exit "${status}"
    ;;
  gradio)
    exec mineru-gradio \
      --server-name 0.0.0.0 \
      --server-port 7860 \
      --api-url "${MINERU_PUBLIC_API_URL:-http://mineru:8001}" \
      --enable-api true \
      "$@"
    ;;
  tools)
    exec "$@"
    ;;
  *)
    exec "${mode}" "$@"
    ;;
esac
