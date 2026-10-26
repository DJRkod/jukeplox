#!/usr/bin/env sh
# Container validation lane for the native (non-Docker) install — plan U5.
#
# Proves, on the dev machine, that install-native.sh takes a 32-bit Debian 12
# userland from nothing to a working venv, that the compiled 3.12 runs the per-file
# suite, and that the app serves pages and plays audio from a non-checkout working
# directory. It runs a linux/386 debian:bookworm-slim container NATIVELY (an amd64
# kernel executes 32-bit x86 without emulation) under `linux32`, so `uname -m`
# reports i686 the way a real 32-bit box does.
#
# What it cannot prove, by construction: anything kernel-level (the container runs
# on the host's 64-bit kernel), systemd, reboot survival, avahi, and real sound
# hardware — the host kernel has no loopback/dummy sound module, so playback goes
# to ALSA's software `null` PCM. The VM lane (a Hyper-V Debian i386 guest) covers those.
#
#   tools/native/validate-i386-container.sh            # full run
#   JP_LANE_SKIP_SUITE=1 tools/native/validate-i386-container.sh   # install + AE checks only
#
# The /opt/jukeplox tree (compiled Python, Rust toolchain, venv) lives in a named
# Docker volume so the first run pays the compile and later runs skip it.
# Self-terminating: the container is removed on exit, success or failure.

set -eu

IMAGE="debian:bookworm-slim"
PLATFORM="linux/386"
VOLUME="${JP_LANE_VOLUME:-jukeplox-i386-opt}"
NAME="jp-i386-lane-$$"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

# Git Bash on Windows rewrites /paths in arguments; docker needs them verbatim.
export MSYS_NO_PATHCONV=1
host_path() { if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s\n' "$1"; fi; }

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM

docker volume inspect "$VOLUME" >/dev/null 2>&1 || docker volume create "$VOLUME" >/dev/null

echo "== lane: ${PLATFORM} ${IMAGE}, volume ${VOLUME}, source $(host_path "$ROOT")"
docker run --name "$NAME" --platform "$PLATFORM" \
  -v "${VOLUME}:/opt/jukeplox" \
  -v "$(host_path "$ROOT"):/src:ro" \
  -e JP_LANE_SKIP_SUITE="${JP_LANE_SKIP_SUITE:-0}" \
  "$IMAGE" linux32 sh /src/tools/native/lane-inside.sh
