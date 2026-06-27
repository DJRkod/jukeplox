#!/usr/bin/env sh
# Jukeplox one-run installer.
#
# Detects the host (LAN IP, desktop-vs-headless audio, a free HTTP port, the mDNS
# path), asks only for the port, installs Docker if missing, then starts and
# health-checks the Jukeplox container with the correct flags and hands off to the
# browser. POSIX sh; runs on the host (not inside a container).
#
#   curl -fsSL https://raw.githubusercontent.com/DJRkod/jukeplox/main/install.sh | sh
#
# Run it as your normal desktop user. The script uses sudo only where it must
# (installing Docker, starting the daemon) and resolves your real user for the
# desktop-audio path even when invoked under sudo.

# NOTE: `set -eu` is enabled inside main(), not at file scope, so the script can be
# sourced for unit tests (bats sets JUKEPLOX_INSTALL_LIB=1) without aborting on the
# first non-zero return.

# ── configuration ─────────────────────────────────────────────────────────────
IMAGE_REPO="djrkod/jukeplox"
CONTAINER_NAME="jukeplox"
DATA_VOLUME="jukeplox-data"
DBUS_SOCKET="/run/dbus/system_bus_socket"
DEFAULT_PORT="80"
PORT_CANDIDATES="8096 8080 8686 9090 9595"
DOCKER_INSTALL_URL="https://get.docker.com"
DOCKER_DOCS_URL="https://docs.docker.com/engine/install/"

# Overridable for tests (stub the host).
: "${JUKEPLOX_RUNTIME_BASE:=/run/user}"
: "${JUKEPLOX_DEV_SND:=/dev/snd}"

# ── options (set by parse_args) ───────────────────────────────────────────────
OPT_PORT=""
OPT_VERSION="latest"
OPT_YES=0

# ── real-user / privilege state (set by resolve_real_user / preflight) ────────
REAL_USER=""
REAL_UID=""
DOCKER_SUDO=""   # "" or "sudo" — how to reach a usable docker daemon

# ── small helpers ─────────────────────────────────────────────────────────────
die() { printf 'Error: %s\n' "$1" >&2; exit 1; }
info() { printf '%s\n' "$1" >&2; }

usage() {
  cat <<EOF
Jukeplox installer

Usage: install.sh [options]

Options:
  --port <n>       HTTP port to use (skips the prompt)
  --version <tag>  Image tag to install (default: latest)
  --yes            Non-interactive: accept defaults and confirmations
  --help           Show this help

Run as your normal user; the script elevates with sudo only when required.
EOF
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --port) OPT_PORT="${2:-}"; shift 2 || die "--port needs a value" ;;
      --port=*) OPT_PORT="${1#*=}"; shift ;;
      --version) OPT_VERSION="${2:-}"; shift 2 || die "--version needs a value" ;;
      --version=*) OPT_VERSION="${1#*=}"; shift ;;
      --yes|-y) OPT_YES=1; shift ;;
      --help|-h) usage; exit 0 ;;
      *) usage >&2; die "unknown option: $1" ;;
    esac
  done
}

# Resolve the human running this, even under sudo, so the desktop-audio path uses
# the user's runtime socket and not root's.
resolve_real_user() {
  if [ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER}" != "root" ]; then
    REAL_USER="$SUDO_USER"
  else
    REAL_USER="$(id -un)"
  fi
  REAL_UID="$(id -u "$REAL_USER" 2>/dev/null || id -u)"
}

# Run a command as root only when not already root.
maybe_sudo() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo "$@"
  fi
}

# Yes/no prompt honoring --yes; reads from the terminal so it works under `curl | sh`.
confirm() {
  [ "$OPT_YES" -eq 1 ] && return 0
  printf '%s [Y/n] ' "$1" >&2
  if [ -r /dev/tty ]; then read -r _ans </dev/tty || _ans=""; else read -r _ans || _ans=""; fi
  case "$_ans" in n|N|no|NO|No) return 1 ;; *) return 0 ;; esac
}

# ── host detection ────────────────────────────────────────────────────────────
detect_lan_ip() {
  _ip=""
  if command -v ip >/dev/null 2>&1; then
    _ip=$(ip route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.][0-9.]*\).*/\1/p' | head -n1)
  fi
  if [ -z "$_ip" ] && command -v hostname >/dev/null 2>&1; then
    _ip=$(hostname -I 2>/dev/null | awk '{print $1}')
  fi
  [ -n "$_ip" ] && printf '%s\n' "$_ip" || printf '127.0.0.1\n'
}

pulse_socket() { printf '%s/%s/pulse/native\n' "$JUKEPLOX_RUNTIME_BASE" "$REAL_UID"; }

# pulse  → a desktop sound server (PulseAudio/PipeWire) owns the card
# alsa   → headless: no sound server, ALSA is free
# none   → no audio device at all (cast-only host)
detect_audio() {
  if [ -S "$(pulse_socket)" ]; then
    printf 'pulse\n'
  elif [ -d "$JUKEPLOX_DEV_SND" ]; then
    printf 'alsa\n'
  else
    printf 'none\n'
  fi
}

# Return 0 when the TCP port is free to bind.
port_free() {
  _p="$1"
  if command -v ss >/dev/null 2>&1; then
    ! ss -ltnH 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]${_p}\$"
  elif command -v netstat >/dev/null 2>&1; then
    ! netstat -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]${_p}\$"
  elif command -v python3 >/dev/null 2>&1; then
    python3 - "$_p" <<'PY' 2>/dev/null
import socket, sys
s = socket.socket()
try:
    s.bind(("0.0.0.0", int(sys.argv[1]))); s.close()
except OSError:
    sys.exit(1)
PY
  else
    return 0  # can't tell; assume free
  fi
}

# Informational only: the dbus mount is added unconditionally (harmless when unused).
detect_mdns() {
  if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet avahi-daemon 2>/dev/null; then
    printf 'avahi\n'
  else
    printf 'in-process\n'
  fi
}

choose_port() {
  if [ -n "$OPT_PORT" ]; then printf '%s\n' "$OPT_PORT"; return 0; fi
  _default="$DEFAULT_PORT"
  if ! port_free "$DEFAULT_PORT"; then
    for _c in $PORT_CANDIDATES; do
      if port_free "$_c"; then _default="$_c"; break; fi
    done
  fi
  if [ "$OPT_YES" -eq 1 ]; then printf '%s\n' "$_default"; return 0; fi
  printf 'HTTP port [%s]: ' "$_default" >&2
  if [ -r /dev/tty ]; then read -r _p </dev/tty || _p=""; else read -r _p || _p=""; fi
  [ -n "$_p" ] && printf '%s\n' "$_p" || printf '%s\n' "$_default"
}

# ── docker preflight ──────────────────────────────────────────────────────────
# shellcheck disable=SC2086  # DOCKER_SUDO is intentionally word-split ("" or "sudo")
docker_q() { ${DOCKER_SUDO} docker "$@"; }

install_docker() {
  command -v curl >/dev/null 2>&1 || die "curl is required to install Docker. Install Docker manually: ${DOCKER_DOCS_URL}"
  info "Installing Docker via ${DOCKER_INSTALL_URL} ..."
  curl -fsSL "$DOCKER_INSTALL_URL" | maybe_sudo sh || die "Docker install failed. Install it manually: ${DOCKER_DOCS_URL}"
  maybe_sudo systemctl enable --now docker >/dev/null 2>&1 || true
}

preflight_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    confirm "Docker isn't installed. Install it now?" || die "Docker is required. Install it (${DOCKER_DOCS_URL}) and re-run."
    install_docker
  fi
  # Find a way to reach a running daemon: direct, then sudo, starting it if needed.
  if docker info >/dev/null 2>&1; then DOCKER_SUDO=""; return 0; fi
  if maybe_sudo docker info >/dev/null 2>&1; then DOCKER_SUDO="sudo"; return 0; fi
  maybe_sudo systemctl start docker >/dev/null 2>&1 || true
  if docker info >/dev/null 2>&1; then DOCKER_SUDO=""; return 0; fi
  if maybe_sudo docker info >/dev/null 2>&1; then DOCKER_SUDO="sudo"; return 0; fi
  die "Docker is installed but its daemon isn't reachable. Start it and re-run."
}

# ── flag assembly (the decision matrix) ───────────────────────────────────────
# Pure: prints the `docker run` arguments, one per line, from detected facts.
#   $1 audio (pulse|alsa|none)  $2 lan_ip  $3 port  $4 uid  $5 version
assemble_args() {
  _audio="$1"; _ip="$2"; _port="$3"; _uid="$4"; _ver="$5"
  printf '%s\n' --name "$CONTAINER_NAME" --network host --restart unless-stopped
  printf '%s\n' -v "${DATA_VOLUME}:/data"
  printf '%s\n' -v "${DBUS_SOCKET}:${DBUS_SOCKET}"
  printf '%s\n' -e "BIND_HOST=${_ip}"
  if [ "$_port" != "80" ]; then
    printf '%s\n' -e "PORT=${_port}"
    printf '%s\n' -e "STREAM_BASE_URL=http://${_ip}:${_port}"
  fi
  case "$_audio" in
    pulse)
      _sock="${JUKEPLOX_RUNTIME_BASE}/${_uid}/pulse/native"
      printf '%s\n' -e "PULSE_SERVER=unix:${_sock}"
      printf '%s\n' -v "${_sock}:${_sock}"
      ;;
    alsa)
      printf '%s\n' --device /dev/snd --group-add audio
      ;;
    none) : ;;
  esac
  printf '%s\n' "${IMAGE_REPO}:${_ver}"
}

container_exists() {
  docker_q ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$CONTAINER_NAME"
}

run_container() {
  _audio="$1"; _ip="$2"; _port="$3"; _uid="$4"; _ver="$5"
  if container_exists; then
    confirm "A '${CONTAINER_NAME}' container already exists. Replace it (your data is kept)?" \
      || die "Leaving the existing container in place."
    docker_q stop "$CONTAINER_NAME" >/dev/null 2>&1 || true
    docker_q rm "$CONTAINER_NAME" >/dev/null 2>&1 || true
    # NOTE: the ${DATA_VOLUME} volume is intentionally never removed.
  fi
  info "Pulling ${IMAGE_REPO}:${_ver} ..."
  docker_q pull "${IMAGE_REPO}:${_ver}" || die "Could not pull ${IMAGE_REPO}:${_ver}. Check the image is public on Docker Hub, or you may have hit Docker Hub's anonymous pull rate limit -- run 'docker login' or wait a few minutes, then retry."
  _oldifs=$IFS
  IFS='
'
  # shellcheck disable=SC2046
  set -- $(assemble_args "$_audio" "$_ip" "$_port" "$_uid" "$_ver")
  IFS=$_oldifs
  docker_q run -d "$@" >/dev/null || die "Failed to start the container."
}

# ── verify + handoff ──────────────────────────────────────────────────────────
wait_healthy() {
  _url="$1"; _tries="${2:-30}"
  command -v curl >/dev/null 2>&1 || { sleep 3; return 0; }  # no curl: best-effort
  while [ "$_tries" -gt 0 ]; do
    if curl -fsS "${_url}/health" 2>/dev/null | grep -q '"status"'; then return 0; fi
    _tries=$((_tries - 1))
    sleep 1
  done
  return 1
}

audio_label() {
  case "$1" in
    pulse) printf 'desktop speakers (PulseAudio/PipeWire)\n' ;;
    alsa)  printf 'attached speakers (ALSA)\n' ;;
    *)     printf 'network speakers only (no local audio)\n' ;;
  esac
}

print_summary() {
  _audio="$1"; _ip="$2"; _port="$3"; _mdns="$4"
  info ""
  info "Jukeplox is running:"
  info "  Audio      : $(audio_label "$_audio")"
  info "  Discovery  : ${_mdns} (Chromecast / AirPlay / DLNA)"
  info "  Address    : http://${_ip}:${_port}"
}

maybe_open_browser() {
  _url="$1"
  [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] || return 0
  command -v xdg-open >/dev/null 2>&1 || return 0
  confirm "Open ${_url} in your browser?" || return 0
  xdg-open "$_url" >/dev/null 2>&1 || true
}

# ── entry point ───────────────────────────────────────────────────────────────
main() {
  set -eu
  parse_args "$@"
  resolve_real_user
  preflight_docker

  _ip="$(detect_lan_ip)"
  _audio="$(detect_audio)"
  _mdns="$(detect_mdns)"
  _port="$(choose_port)"

  run_container "$_audio" "$_ip" "$_port" "$REAL_UID" "$OPT_VERSION"

  _url="http://${_ip}:${_port}"
  if wait_healthy "$_url"; then
    print_summary "$_audio" "$_ip" "$_port" "$_mdns"
    info ""
    info "Finish setup in your browser:"
    info "  ${_url}/admin   (set a password, connect Plex, pick a speaker)"
    maybe_open_browser "${_url}/admin"
  else
    die "Container started but never became healthy. Check: docker logs ${CONTAINER_NAME}"
  fi
}

if [ -z "${JUKEPLOX_INSTALL_LIB:-}" ]; then
  main "$@"
fi
