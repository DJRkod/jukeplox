#!/bin/sh
# Continuous segmented capture of the analogue loopback, for the acoustic arm.
#
# Runs ON the rig. Everything comes from the environment — no host, no device
# index, no credential is committed here.
#
# Segments hourly so that a mid-run failure destroys at most one segment rather
# than the whole arm, and so feature extraction can run incrementally instead of
# waiting six hours for a single enormous file.
#
# FLAC, not WAV: a 12-hour party cannot be re-run, so keeping the evidence
# losslessly is worth the disk. Measured on this rig 2026-09-23 at ~342 MB/hour
# (48 kHz stereo), against ~1.04 GB/hour for raw 24-bit.
#
# Capture goes through the ALSA `default` PCM rather than `hw:`. The host's
# asound.conf wraps the card in `plug`, which converts the interface's
# S24_3LE-only format; addressing `hw:` directly forces the caller to match that
# format exactly and fails with "Sample format non available" otherwise. Never
# pin a card index — USB indexes shift across reboots and the soak's recovery
# loop restarts things.
set -eu

DEVICE="${JP_ALSA_CAPTURE:-default}"
RATE="${JP_ALSA_RATE:-48000}"
CHANNELS="${JP_ALSA_CHANNELS:-2}"
SEGMENT_SEC="${JP_SEGMENT_SEC:-3600}"
OUT_DIR="${JP_CAP_DIR:?JP_CAP_DIR is required}"
# Hours this capture is expected to run, used only for the disk pre-flight.
HOURS="${JP_CAP_HOURS:?JP_CAP_HOURS is required}"
# Measured rate plus headroom. Overridable because a different rate or a
# different interface changes it.
MB_PER_HOUR="${JP_CAP_MB_PER_HOUR:-400}"

mkdir -p "$OUT_DIR"

# Pre-flight the disk rather than discovering the problem at hour nine.
need_mb=$(( HOURS * MB_PER_HOUR ))
free_mb=$(df -Pm "$OUT_DIR" | awk 'NR==2 {print $4}')
if [ "$free_mb" -lt "$need_mb" ]; then
  echo "refusing to start: need ~${need_mb} MB for ${HOURS}h, only ${free_mb} MB free in ${OUT_DIR}" >&2
  exit 2
fi
# Pre-flight the DEVICE too. ALSA capture is single-consumer: a leftover
# recorder from a previous run makes this one fail at open with "Device or
# resource busy". Backgrounded, that failure is silent — on 2026-09-23 a
# four-minute capture produced no audio at all while the state sampler happily
# recorded 141 samples beside it, and only the recorder's own log revealed why.
# A 6-hour arm would fail the same way. Fail here, loudly, instead.
if ! arecord -D "$DEVICE" -f S24_3LE -c "$CHANNELS" -r "$RATE" -d 1 /dev/null >/dev/null 2>&1; then
  busy=$(arecord -D "$DEVICE" -f S24_3LE -c "$CHANNELS" -r "$RATE" -d 1 /dev/null 2>&1 | tail -1)
  echo "refusing to start: capture device ${DEVICE} is not usable: ${busy}" >&2
  echo "  (if this says 'busy', a previous recorder is still holding it)" >&2
  exit 3
fi

echo "capture: device=${DEVICE} rate=${RATE} segments=${SEGMENT_SEC}s dir=${OUT_DIR} (need ~${need_mb}MB, ${free_mb}MB free)"

# -strftime names segments by wall time, so ordering survives a restart mid-arm
# and a segment can be located from an incident timestamp without an index.
exec ffmpeg -hide_banner -loglevel warning -nostdin \
  -f alsa -sample_rate "$RATE" -channels "$CHANNELS" -i "$DEVICE" \
  -c:a flac \
  -f segment -segment_time "$SEGMENT_SEC" -segment_atclocktime 1 -strftime 1 \
  "${OUT_DIR}/cap-%Y%m%d-%H%M%S.flac"
