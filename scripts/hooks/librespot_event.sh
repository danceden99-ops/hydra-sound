#!/usr/bin/env bash
set -euo pipefail
# Event hook invoked by librespot --onevent
EVENT="${PLAYER_EVENT:-}"
case "$EVENT" in
  started|playing)
    curl -s -X POST http://127.0.0.1:8000/api/internal/event \
      -H "Content-Type: application/json" \
      -d "{\"source\":\"spotify\",\"event\":\"play\",\"incoming_format\":\"Ogg Vorbis\",\"incoming_rate\":44100,\"incoming_bit_depth\":16}" >/dev/null || true
    ;;
  paused)
    curl -s -X POST http://127.0.0.1:8000/api/internal/event \
      -H "Content-Type: application/json" \
      -d "{\"source\":\"spotify\",\"event\":\"pause\"}" >/dev/null || true
    ;;
  stopped)
    curl -s -X POST http://127.0.0.1:8000/api/internal/event \
      -H "Content-Type: application/json" \
      -d "{\"source\":\"spotify\",\"event\":\"stop\"}" >/dev/null || true
    ;;
esac
