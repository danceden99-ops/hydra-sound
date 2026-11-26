#!/usr/bin/env bash
set -euo pipefail
# Event hook invoked by shairport-sync sessioncontrol
ACTION="${1:-stop}"
if [[ "$ACTION" == "play" ]]; then
  curl -s -X POST http://127.0.0.1:8000/api/internal/event \
    -H "Content-Type: application/json" \
    -d '{"source":"airplay","event":"play","incoming_format":"ALAC (Lossless)","incoming_rate":44100,"incoming_bit_depth":16}' >/dev/null || true
else
  curl -s -X POST http://127.0.0.1:8000/api/internal/event \
    -H "Content-Type: application/json" \
    -d '{"source":"airplay","event":"stop"}' >/dev/null || true
fi
