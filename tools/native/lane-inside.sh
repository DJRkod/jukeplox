#!/bin/sh
# Runs INSIDE the linux/386 container started by validate-i386-container.sh.
# Everything here is a 32-bit Debian 12 userland; /src is the repo (read-only),
# /opt/jukeplox is the cached install volume.
set -eu

t0=$(date +%s)
say() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
step() { _n=$1; shift; _s=$(date +%s); say "START $_n"; if "$@"; then say "OK    $_n ($(( $(date +%s) - _s ))s)"; else say "FAIL  $_n ($(( $(date +%s) - _s ))s)"; return 1; fi; }
RESULTS=""
record() { RESULTS="${RESULTS}$1\n"; }

say "arch=$(dpkg --print-architecture) uname=$(uname -m) nproc=$(nproc)"
export DEBIAN_FRONTEND=noninteractive
# No apt-get update here: the installer must do it itself (a fresh box has no lists).

# ── 1. installer (AE1 at the venv level; --no-service: no systemd in a container) ──
# Copy the read-only mount so `pip install -e` can write its egg-link metadata.
rm -rf /opt/jukeplox/src && cp -r /src /opt/jukeplox/src
step installer sh /opt/jukeplox/src/install-native.sh --source /opt/jukeplox/src --no-service --yes \
  && record "AE1 installer: PASS" || { record "AE1 installer: FAIL"; printf "$RESULTS"; exit 1; }

PY=/opt/jukeplox/venv/bin/python
"$PY" -c 'import sys,platform; assert sys.version_info[:2]==(3,12), sys.version; print("python", platform.python_version(), platform.machine(), "32-bit" if sys.maxsize < 2**32 else "64-bit")'
step imports "$PY" -c 'import gi; gi.require_version("Gst","1.0"); from gi.repository import Gst, GLib, Gio; import cryptography, bcrypt, orjson, numpy, av, PIL, aiosendspin, snapcast, zeroconf, pychromecast; import app.main; print("imports ok")' \
  && record "imports (gi/Gst, cryptography, bcrypt, orjson, numpy, av, PIL, app.main): PASS" || record "imports: FAIL"

# ── 2. bytecode cache reused across starts (KTD6) ─────────────────────────────
export PYTHONPYCACHEPREFIX=/tmp/pycache
"$PY" -c 'import app.main' >/dev/null 2>&1 || true
n1=$(find /tmp/pycache -name '*.pyc' | wc -l)
"$PY" -X importtime -c 'import app.main' 2>/tmp/importtime.log || true
n2=$(find /tmp/pycache -name '*.pyc' | wc -l)
[ "$n1" -gt 0 ] && [ "$n1" -eq "$n2" ] && record "pycache reused on second start (${n1} .pyc, no recompile): PASS" || record "pycache reuse: FAIL (${n1} -> ${n2})"

# ── 3. serve from a foreign CWD (AE4) ─────────────────────────────────────────
mkdir -p /tmp/jpdata /tmp/elsewhere
# ALSA software null PCM: no /dev/snd in this container, no sound module in the host kernel.
cat > /etc/asound.conf <<'EOF'
pcm.!default { type null }
EOF
( cd /tmp/elsewhere && DATA_DIR=/tmp/jpdata BIND_HOST=127.0.0.1 PORT=8099 JUKEPLOX_GIT_SHA=lane JUKEPLOX_BUILD_TIME=now JUKEPLOX_IMAGE_TAG=native \
    /opt/jukeplox/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8099 >/tmp/uvicorn.log 2>&1 & echo $! > /tmp/uvicorn.pid )
for i in $(seq 1 30); do curl -fsS http://127.0.0.1:8099/api/version >/dev/null 2>&1 && break; sleep 1; done
ae4=PASS
for p in / /static/shared.js /api/version; do
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:8099$p"); [ "$code" = "200" ] || { ae4="FAIL ($p -> $code)"; }
done
curl -fsS http://127.0.0.1:8099/api/version | grep -q '"image_tag": *"native"' || ae4="FAIL (badge)"
record "AE4 serve from foreign CWD (/, /static/shared.js, /api/version, native badge): $ae4"

# ── 4. Direct-backend playback to the null PCM (AE5) ──────────────────────────
# A synthesized sine track, registered as a Local source, scanned, queued, played.
# Two tracks: the ALSA null PCM has no clock, so a track drains instantly; the proof
# here is confirmed-start + EOS advance into the second track (both land in history).
mkdir -p /tmp/music
ffmpeg -loglevel error -y -f lavfi -i "sine=frequency=440:duration=20" -ar 44100 -ac 2 /tmp/music/a-sine.flac
ffmpeg -loglevel error -y -f lavfi -i "sine=frequency=660:duration=20" -ar 44100 -ac 2 /tmp/music/b-sine.flac
ls -l /tmp/music
# Positive evidence is required on top of the drive's history check: the queue also
# appends to history on the error-advance path, so two undecodable tracks would
# otherwise pass. Two confirmed starts and no decode/skip errors in the app log.
if "$PY" /opt/jukeplox/src/tools/native/lane-play.py --base http://127.0.0.1:8099 --music /tmp/music; then
  starts=$(grep -c "confirmed start for" /tmp/uvicorn.log || true)
  errs=$(grep -cE "track_skipped|GStreamer ERROR|GST_MESSAGE_ERROR|DeviceNotReady|confirm_timeout" /tmp/uvicorn.log || true)
  if [ "$starts" -ge 2 ] && [ "$errs" -eq 0 ]; then ae5="PASS (${starts} confirmed starts, 0 errors)"; else ae5="FAIL (confirmed starts=${starts}, errors=${errs})"; fi
else
  ae5=FAIL
fi
record "AE5 Direct backend to ALSA null PCM (confirmed start + EOS advance; pacing is the VM lane's job): $ae5"
kill "$(cat /tmp/uvicorn.pid)" 2>/dev/null || true
mkdir -p /opt/jukeplox/lane-logs && cp /tmp/uvicorn.log /opt/jukeplox/lane-logs/uvicorn.log
[ "$ae5" = PASS ] || { say "uvicorn log tail (AE5 failed):"; grep -iE "error|warn|direct|alsa|gst|play|queue" /tmp/uvicorn.log | tail -40 | cut -c1-240; }

# ── 5. per-file suite under the compiled interpreter ──────────────────────────
if [ "${JP_LANE_SKIP_SUITE:-0}" != "1" ]; then
  cd /opt/jukeplox/src
  pass=0; fail=0; failed=""
  LOGS=/opt/jukeplox/lane-logs; rm -rf "$LOGS"; mkdir -p "$LOGS"
  for f in tests/test_*.py; do
    name=$(basename "$f" .py)
    if timeout 400 "$PY" -m pytest "$f" -q -p no:cacheprovider --no-header >"$LOGS/$name.log" 2>&1; then
      pass=$((pass+1)); rm -f "$LOGS/$name.log"
    else
      rc=$?; fail=$((fail+1)); failed="$failed $name(rc=$rc)"
      say "FAIL $name: $(grep -E '^(FAILED|ERROR) ' "$LOGS/$name.log" | cut -c1-120 | head -5 | tr '
' ';')"
    fi
  done
  record "suite: ${pass} files passed, ${fail} failed${failed:+:$failed} (logs kept in volume: /opt/jukeplox/lane-logs)"
fi

say "DONE in $(( $(date +%s) - t0 ))s"
echo "===== lane summary ====="
printf '%b' "$RESULTS"
case "$RESULTS" in *FAIL*) exit 1 ;; esac
