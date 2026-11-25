#!/usr/bin/env bash
set -euo pipefail
# Event hook invoked by pibuz --on-event
EVENT="${1:-${PIBUZ_EVENT:-}}"
case "$EVENT" in
  play|playing|start)
    curl -s -X POST http://127.0.0.1:8000/api/internal/event \
      -H "Content-Type: application/json" \
      -d '{"source":"qobuz","event":"play","incoming_format":"FLAC"}' >/dev/null || true
    ;;
  pause|paused)
    curl -s -X POST http://127.0.0.1:8000/api/internal/event \
      -H "Content-Type: application/json" \
      -d '{"source":"qobuz","event":"pause"}' >/dev/null || true
    ;;
  stop|stopped)
    curl -s -X POST http://127.0.0.1:8000/api/internal/event \
      -H "Content-Type: application/json" \
      -d '{"source":"qobuz","event":"stop"}' >/dev/null || true
    ;;
esac
