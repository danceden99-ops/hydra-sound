#!/usr/bin/env bash
set -euo pipefail
# Called by alsa_cdsp with: <format> <rate> <channels>
RAW_FMT="${1:-S32LE}"
RATE="${2:-44100}"
CHANNELS="${3:-2}"

case "$RAW_FMT" in
  S16LE|S16_LE) FMT="S16_LE" ;;
  S24LE|S24_LE) FMT="S24_4_RJ_LE" ;;
  S24_3LE|S24_3_LE) FMT="S24_3_LE" ;;
  S32LE|S32_LE) FMT="S32_LE" ;;
  FLOATLE|FLOAT32LE|F32_LE) FMT="F32_LE" ;;
  FLOAT64LE|F64_LE) FMT="F64_LE" ;;
  *) FMT="$RAW_FMT" ;;
esac

CFG_DIR="/etc/hydrasound"
if [[ -n "${HYDRA_CONFIG_PATH:-}" ]]; then
  CFG_DIR="$(dirname "$HYDRA_CONFIG_PATH")"
fi

TMPL="${CFG_DIR}/camilladsp_in.yml"
OUT="${CFG_DIR}/camilladsp.yml"

CHUNKSIZE=2048
if [[ "$RATE" -le 48000 ]]; then
  CHUNKSIZE=1024
elif [[ "$RATE" -gt 96000 ]]; then
  CHUNKSIZE=4096
fi

sed \
  -e "s/\\\$format\\\$/${FMT}/g" \
  -e "s/\\\$samplerate\\\$/${RATE}/g" \
  -e "s/\\\$channels\\\$/${CHANNELS}/g" \
  -e "s/\\\$chunksize\\\$/${CHUNKSIZE}/g" \
  "$TMPL" > "$OUT"

# Bypass Synchronous resampler when capture rate already matches target samplerate
if grep -qE "^  samplerate: ${RATE}$" "$OUT" && grep -qE "^  capture_samplerate: ${RATE}$" "$OUT"; then
  sed -i -e '/^  resampler:/{N;d;}' -e '/^  capture_samplerate:/d' "$OUT"
fi
