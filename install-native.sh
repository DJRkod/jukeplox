#!/usr/bin/env sh
# Jukeplox native (non-Docker) installer for Debian 12 "bookworm".
#
# Turns a stock bookworm box — validated on 32-bit x86 (i386), where the Docker
# image cannot run — into a running Jukeplox service: apt packages, a compiled
# CPython 3.12 (bookworm ships 3.11 and no 3.12 exists for i686), a Rust
# toolchain for the three crates that have no 32-bit wheels, a venv with the
# checkout installed editably, a systemd unit under a dedicated user, and an
# env file whose build-badge lines name the checkout's sha.
#
#   # fresh box, as root:
#   curl -fsSL https://raw.githubusercontent.com/DJRkod/jukeplox/main/install-native.sh -o install-native.sh
#   sh install-native.sh
#
#   # update: re-run. The checkout fast-forwards, the interpreter and toolchain
#   # are reused, the venv is reinstalled, the badge lines are refreshed.
#
# POSIX sh; runs on the host. Same sourceable-library shape as install.sh: set -eu
# lives inside main() so the bats suite can source the file (JUKEPLOX_INSTALL_LIB=1)
# and exercise the functions with stubbed externals. Decision functions PRINT and
# never execute; the thin wrappers around them do the executing.
#
# Parity with the Docker image is enforced by tests/test_native_parity.py, which
# parses the package-set variables and the env template below against Dockerfile.

# ── pinned downloads ──────────────────────────────────────────────────────────
PY_VERSION="3.12.15"
PY_SHA256="de1a241a519e0a3374fea98988d0b52c886743f9d953f23be8269cc7b59c5fab"
PY_URL_BASE="${JUKEPLOX_PY_URL_BASE:-https://www.python.org/ftp/python}"

RUSTUP_VERSION="1.29.2"
RUSTUP_SHA256_i686="01c158bbee6bf70a57bd2642349b6f70e0be8b98b366fc4bbe38dfe7f82e1acb"
RUSTUP_SHA256_x86_64="5be191e8c70a5007074254cb74d78dbfdd25cd5a61c27de7b8e5410122ec24f4"
RUSTUP_SHA256_aarch64="a9c168dbea81a84671c6b7a5c1795cb0eb699020110d107ddfca427bd797e697"
RUSTUP_URL_BASE="${JUKEPLOX_RUSTUP_URL_BASE:-https://static.rust-lang.org/rustup/archive}"
# cryptography/bcrypt/orjson build with any recent stable; bookworm's 1.63 is too old.
RUST_MIN_MINOR="74"

PYGOBJECT_PIN="PyGObject<3.50"   # identical to the Dockerfile's pin (parity-tested)

# ── package sets (parity-tested against Dockerfile) ───────────────────────────
# Dockerfile runtime apt list MINUS the AirPlay support libraries.
RUNTIME_PKGS="
gstreamer1.0-tools
gstreamer1.0-plugins-base
gstreamer1.0-plugins-good
gstreamer1.0-plugins-ugly
gstreamer1.0-libav
gstreamer1.0-pulseaudio
gstreamer1.0-alsa
gir1.2-gstreamer-1.0
gir1.2-gst-plugins-base-1.0
libgirepository-1.0-1
ffmpeg
flac
snapserver
snapclient
"
# Needed only by the vendored cliap2/cliraop binaries, which exist for x86_64 and
# aarch64 only (public issue #90 tracks a 32-bit build). Installed on those two.
AIRPLAY_LIBS="
libplist3
libevent-2.1-7
libevent-pthreads-2.1-7
libcurl4
libconfuse2
"
# Dockerfile builder stage deps (first four) plus what compiling CPython and the
# no-wheel C extensions (uvloop, httptools, pillow, numpy, aiohttp, pycairo) need.
BUILD_PKGS="
libgirepository1.0-dev
libcairo2-dev
gcc
pkg-config
build-essential
libssl-dev
libffi-dev
libsqlite3-dev
zlib1g-dev
libbz2-dev
liblzma-dev
libreadline-dev
libncursesw5-dev
uuid-dev
libgdbm-dev
libgdbm-compat-dev
tk-dev
libzstd-dev
libjpeg62-turbo-dev
libfreetype6-dev
libuv1-dev
xz-utils
"
# Not part of the image's contract; needed by this script itself.
PREFLIGHT_PKGS="curl ca-certificates git"

# Every variable the env file knows. Superset of the Dockerfile's ENV block
# (parity-tested as containment): the rest are `-e` flags in Docker.
# shellcheck disable=SC2034  # read by tests/test_native_parity.py and the bats suite
ENV_TEMPLATE_VARS="DATA_DIR BIND_HOST PORT LOG_LEVEL STREAM_BASE_URL PULSE_SERVER JUKEPLOX_CLIAP2_BIN JUKEPLOX_CLIRAOP_BIN JUKEPLOX_SNAPSERVER_BIN JUKEPLOX_SNAPSERVER_CONF JUKEPLOX_GIT_SHA JUKEPLOX_BUILD_TIME JUKEPLOX_IMAGE_TAG"
BADGE_VARS="JUKEPLOX_GIT_SHA JUKEPLOX_BUILD_TIME JUKEPLOX_IMAGE_TAG"

# ── layout (overridable by flags) ─────────────────────────────────────────────
REPO_URL="${JUKEPLOX_REPO_URL:-https://github.com/DJRkod/jukeplox.git}"
REPO_BRANCH="${JUKEPLOX_REPO_BRANCH:-main}"
DEFAULT_PREFIX="/opt/jukeplox"
DEFAULT_DATA_DIR="/var/lib/jukeplox"
DEFAULT_SERVICE_USER="jukeplox"
ENV_DIR="${JUKEPLOX_ENV_DIR:-/etc/jukeplox}"
UNIT_PATH="${JUKEPLOX_UNIT_PATH:-/etc/systemd/system/jukeplox.service}"
CACHE_DIR="/var/cache/jukeplox"       # systemd CacheDirectory=jukeplox
MIN_FREE_MB=3072                      # CPython + Rust + venv builds under the prefix
MIN_RAM_MB=2048                       # Rust/numpy compiles below this tend to OOM
HEALTH_TRIES="${JUKEPLOX_HEALTH_TRIES:-60}"

# ── options (set by parse_args) ───────────────────────────────────────────────
OPT_SOURCE=""          # --source DIR: use an existing checkout as-is
OPT_PREFIX="$DEFAULT_PREFIX"
OPT_DATA_DIR="$DEFAULT_DATA_DIR"
OPT_PORT="80"
OPT_BIND_HOST=""       # empty → detected LAN address
OPT_RUN_AS="$DEFAULT_SERVICE_USER"
OPT_RUN_AS_SET=0         # 1 when --run-as was passed (a re-run otherwise keeps the unit's user)
OPT_OPTIMIZE=0
OPT_NO_SERVICE=0
OPT_YES=0

# ── small helpers ─────────────────────────────────────────────────────────────
die() { printf 'Error: %s\n' "$1" >&2; exit 1; }
# Build scratch dirs are registered here and removed on ANY exit (a failed CPython
# build otherwise leaves hundreds of MB under /tmp on a small 32-bit disk).
_TMPDIRS=""
# Sets SCRATCH (never call it inside $(...): the registry must reach the parent shell).
scratch_dir() { SCRATCH="$(mktemp -d)"; _TMPDIRS="$_TMPDIRS $SCRATCH"; }
# shellcheck disable=SC2086  # intentional word-split of the dir list
cleanup_scratch() { [ -n "$_TMPDIRS" ] && rm -rf $_TMPDIRS; return 0; }
info() { printf '%s\n' "$1" >&2; }
now_s() { date +%s; }
utc_now() { date -u +%Y-%m-%dT%H:%MZ; }

usage() {
  cat <<EOF
Jukeplox native installer (Debian 12)

Usage: sh install-native.sh [options]      (run as root)

Options:
  --source <dir>     Install from an existing checkout instead of cloning
  --prefix <dir>     Install root (default: ${DEFAULT_PREFIX})
  --data-dir <dir>   Database + secret key (default: ${DEFAULT_DATA_DIR})
  --port <n>         HTTP port (default: 80)
  --bind-host <ip>   Pin the listen address (default: all interfaces, 0.0.0.0, like the
                     Docker image). Pinning needs a static IP: a DHCP change would
                     leave the service unable to bind.
  --run-as <user>    Service account (default: ${DEFAULT_SERVICE_USER}, created).
                     Use your desktop user if you set PULSE_SERVER.
  --optimize         Build CPython with PGO+LTO (hours on old 32-bit hardware)
  --no-service       Stop after the venv; no user, no systemd (container lane)
  --yes              Non-interactive: accept confirmations
  --help             Show this help
EOF
}

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --source)     OPT_SOURCE="$2"; shift 2 ;;
      --source=*)   OPT_SOURCE="${1#*=}"; shift ;;
      --prefix)     OPT_PREFIX="$2"; shift 2 ;;
      --prefix=*)   OPT_PREFIX="${1#*=}"; shift ;;
      --data-dir)   OPT_DATA_DIR="$2"; shift 2 ;;
      --data-dir=*) OPT_DATA_DIR="${1#*=}"; shift ;;
      --port)       OPT_PORT="$2"; shift 2 ;;
      --port=*)     OPT_PORT="${1#*=}"; shift ;;
      --bind-host)  OPT_BIND_HOST="$2"; shift 2 ;;
      --bind-host=*) OPT_BIND_HOST="${1#*=}"; shift ;;
      --run-as)     OPT_RUN_AS="$2"; OPT_RUN_AS_SET=1; shift 2 ;;
      --run-as=*)   OPT_RUN_AS="${1#*=}"; OPT_RUN_AS_SET=1; shift ;;
      --optimize)   OPT_OPTIMIZE=1; shift ;;
      --no-service) OPT_NO_SERVICE=1; shift ;;
      --yes|-y)     OPT_YES=1; shift ;;
      --help|-h)    usage; exit 0 ;;
      *) usage >&2; die "unknown option: $1" ;;
    esac
  done
  case "$OPT_PORT" in ''|*[!0-9]*) die "--port must be a number" ;; esac
}

# Yes/no prompt honoring --yes; reads from the terminal so it works under `curl | sh`.
confirm() {
  [ "$OPT_YES" -eq 1 ] && return 0
  printf '%s [Y/n] ' "$1" >&2
  # 2>/dev/null FIRST: a failing /dev/tty open aborts before a later 2> applies (audit F8).
  if [ -r /dev/tty ]; then read -r _ans 2>/dev/null </dev/tty || _ans=""; else read -r _ans || _ans=""; fi
  case "$_ans" in n|N|no|NO|No) return 1 ;; *) return 0 ;; esac
}

# Derived layout paths; call after parse_args.
layout() {
  APP_DIR="${OPT_SOURCE:-$OPT_PREFIX/app}"
  PY_PREFIX="$OPT_PREFIX/python"
  PY_BIN="$PY_PREFIX/bin/python3.12"
  VENV_DIR="$OPT_PREFIX/venv"
  RUST_HOME="$OPT_PREFIX/rust"
  ENV_FILE="$ENV_DIR/env"
}

# ── preflight (pure checks print verdicts; wrappers act) ──────────────────────
detect_arch() { dpkg --print-architecture 2>/dev/null || uname -m; }

# dpkg arch → vendored-binary suffix, or empty when no AirPlay build exists.
airplay_suffix() {
  case "$1" in
    amd64|x86_64) printf 'x86_64\n' ;;
    arm64|aarch64) printf 'aarch64\n' ;;
    *) printf '\n' ;;
  esac
}

# dpkg arch → Rust target triple (empty = unsupported by the pinned rustup set).
rust_triple() {
  case "$1" in
    i386|i686) printf 'i686-unknown-linux-gnu\n' ;;
    amd64|x86_64) printf 'x86_64-unknown-linux-gnu\n' ;;
    arm64|aarch64) printf 'aarch64-unknown-linux-gnu\n' ;;
    *) printf '\n' ;;
  esac
}

rustup_sha256_for() {
  case "$1" in
    i386|i686) printf '%s\n' "$RUSTUP_SHA256_i686" ;;
    amd64|x86_64) printf '%s\n' "$RUSTUP_SHA256_x86_64" ;;
    arm64|aarch64) printf '%s\n' "$RUSTUP_SHA256_aarch64" ;;
    *) printf '\n' ;;
  esac
}

# Pure: the apt runtime set for an architecture, one package per line.
# shellcheck disable=SC2086  # the package-set variables are intentionally word-split
package_set() {
  printf '%s\n' $RUNTIME_PKGS
  if [ -n "$(airplay_suffix "$1")" ]; then printf '%s\n' $AIRPLAY_LIBS; fi
}

# Pure: "ok" | "warn <reason>" from an os-release file (never fatal: other Debian
# releases and derivatives are unvalidated, not refused).
os_verdict() {
  _f="${1:-/etc/os-release}"
  [ -r "$_f" ] || { printf 'warn no os-release file; assuming Debian-compatible\n'; return 0; }
  _id="$(sed -n 's/^ID=//p' "$_f" | tr -d '"')"
  _ver="$(sed -n 's/^VERSION_ID=//p' "$_f" | tr -d '"')"
  if [ "$_id" != "debian" ]; then printf 'warn %s is not Debian; only Debian 12 is validated\n' "${_id:-unknown}"; return 0; fi
  if [ "$_ver" != "12" ]; then printf 'warn Debian %s is not the validated release (12)\n' "${_ver:-?}"; return 0; fi
  printf 'ok\n'
}

preflight() {
  [ "$(id -u)" -eq 0 ] || die "run this installer as root (sudo sh install-native.sh)"
  command -v apt-get >/dev/null 2>&1 || die "apt-get not found; this installer targets Debian 12"
  _v="$(os_verdict /etc/os-release)"
  case "$_v" in
    warn*) info "Warning: ${_v#warn }" ;;
  esac
  ARCH="$(detect_arch)"
  info "Architecture: ${ARCH}  (uname: $(uname -m))"
  [ -n "$(rust_triple "$ARCH")" ] || die "no pinned Rust toolchain for architecture ${ARCH}"
  if [ -z "$(airplay_suffix "$ARCH")" ]; then
    info "Note: no AirPlay binaries exist for ${ARCH}; AirPlay output will be unavailable (issue #90)."
  fi
  apt-get update -qq >/dev/null || die "apt-get update failed"
  # shellcheck disable=SC2086
  apt-get install -y -qq --no-install-recommends $PREFLIGHT_PKGS >/dev/null || die "could not install ${PREFLIGHT_PKGS}"
  _free="$(free_mb_under "$OPT_PREFIX")"
  [ "$_free" -ge "$MIN_FREE_MB" ] || die "need ${MIN_FREE_MB} MB free under ${OPT_PREFIX}, have ${_free} MB"
  _ram="$(ram_mb)"
  if [ "$_ram" -lt "$MIN_RAM_MB" ]; then
    info "Warning: ${_ram} MB RAM. Compiling cryptography/numpy below ${MIN_RAM_MB} MB can be killed by the OOM killer; add a swap file first if the build dies."
  fi
}

# MB free on the filesystem that would hold $1 (walks up to an existing parent).
free_mb_under() {
  _d="$1"; while [ ! -d "$_d" ] && [ "$_d" != "/" ]; do _d="$(dirname "$_d")"; done
  df -Pm "$_d" | awk 'NR==2 {print $4}'
}
ram_mb() { awk '/MemTotal/ {printf "%d\n", $2/1024}' /proc/meminfo; }

# Pure: "free" | "busy" for a TCP port, via ss (falls back to free when ss is missing).
port_verdict() {
  command -v ss >/dev/null 2>&1 || { printf 'free\n'; return 0; }
  if ss -Hltn 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]$1\$"; then printf 'busy\n'; else printf 'free\n'; fi
}

# Pure: "local" | "foreign" for an address, given the host's address list ($2, newline-separated).
bind_host_verdict() {
  _ip="$1"; _addrs="$2"
  case "$_ip" in 0.0.0.0|::|'') printf 'local\n'; return 0 ;; esac
  if printf '%s\n' "$_addrs" | grep -qxF -- "$_ip"; then printf 'local\n'; else printf 'foreign\n'; fi
}

local_addrs() {
  if command -v ip >/dev/null 2>&1; then ip -o addr show 2>/dev/null | awk '{print $4}' | cut -d/ -f1; fi
  hostname -I 2>/dev/null | tr ' ' '\n'
}

detect_lan_ip() {
  _ip=""
  if command -v ip >/dev/null 2>&1; then
    _ip="$(ip route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n1)"
  fi
  [ -n "$_ip" ] || _ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  [ -n "$_ip" ] || _ip="0.0.0.0"
  printf '%s\n' "$_ip"
}

# ── system packages ───────────────────────────────────────────────────────────
install_packages() {
  info "Installing system packages ..."
  # shellcheck disable=SC2086,SC2046  # package lists are intentionally word-split
  apt-get install -y -qq --no-install-recommends $BUILD_PKGS $(package_set "$ARCH") >/dev/null \
    || die "apt-get install failed"
  disable_distro_snapcast
  install_airplay_binaries "$ARCH"
}

# Debian starts snapserver.service and snapclient.service the moment apt installs
# them. The distro snapserver would then hold 1704/1705/1780, which the embedded
# supervisor needs, and the distro snapclient would open the default ALSA device.
# The image never had this problem — it has no init system.
disable_distro_snapcast() {
  command -v systemctl >/dev/null 2>&1 || return 0
  for _u in snapserver.service snapclient.service; do
    systemctl disable --now "$_u" >/dev/null 2>&1 || true
    systemctl mask "$_u" >/dev/null 2>&1 || true
  done
}

# Pure: prints "src dst" pairs for the vendored AirPlay binaries, or nothing.
airplay_binary_pairs() {
  _sfx="$(airplay_suffix "$1")"; _dir="$2"
  [ -n "$_sfx" ] || return 0
  printf '%s %s\n' "$_dir/vendor/airplay/cliap2-linux-$_sfx" /usr/local/bin/cliap2
  printf '%s %s\n' "$_dir/vendor/airplay/cliraop-linux-$_sfx" /usr/local/bin/cliraop
}

install_airplay_binaries() {
  airplay_binary_pairs "$1" "$APP_DIR" | while read -r _src _dst; do
    [ -f "$_src" ] || { info "Warning: ${_src} missing; AirPlay unavailable"; continue; }
    install -m 0755 "$_src" "$_dst"
  done
}

# ── checkout ──────────────────────────────────────────────────────────────────
git_q() { git -c "safe.directory=$APP_DIR" -C "$APP_DIR" "$@"; }

# Pure: "clone" | "proceed" | "ff" | "stop <reason>" for the MANAGED clone.
#   $1 "absent"|"present"  $2 porcelain output  $3 "ancestor"|"diverged"
update_verdict() {
  case "$1" in absent) printf 'clone\n'; return 0 ;; esac
  if [ -n "$2" ]; then
    printf 'stop checkout has local changes:\n%s\n' "$2"; return 0
  fi
  case "$3" in
    ancestor) printf 'ff\n' ;;
    *) printf 'stop checkout has diverged from %s; resolve it by hand (the installer never resets)\n' "origin/$REPO_BRANCH" ;;
  esac
}

ensure_checkout() {
  if [ -n "$OPT_SOURCE" ]; then
    # --source is used as-is: no clone, fetch, fast-forward, dirty or divergence
    # check. Both validation lanes run from a mounted or copied tree.
    [ -d "$OPT_SOURCE/app" ] || die "--source ${OPT_SOURCE} does not look like a Jukeplox checkout"
    info "Using source checkout ${OPT_SOURCE} as-is"
    return 0
  fi
  if [ ! -d "$APP_DIR/.git" ]; then
    _state=absent; _porc=""; _anc=""
  else
    git_q fetch -q origin "$REPO_BRANCH" || die "git fetch failed"
    _state=present
    _porc="$(git_q status --porcelain)"
    if git_q merge-base --is-ancestor HEAD "origin/$REPO_BRANCH"; then _anc=ancestor; else _anc=diverged; fi
  fi
  _v="$(update_verdict "$_state" "$_porc" "$_anc")"
  case "$_v" in
    clone)
      info "Cloning ${REPO_URL} (${REPO_BRANCH}) into ${APP_DIR} ..."
      install -d -m 0755 "$(dirname "$APP_DIR")"
      git clone -q --branch "$REPO_BRANCH" "$REPO_URL" "$APP_DIR" || die "git clone failed" ;;
    ff)
      info "Updating checkout ..."
      git_q merge -q --ff-only "origin/$REPO_BRANCH" || die "fast-forward failed" ;;
    stop*) die "${_v#stop }" ;;
  esac
}

badge_sha() { git_q rev-parse --short HEAD 2>/dev/null; }

# ── CPython ───────────────────────────────────────────────────────────────────
# Pure: "skip" | "build" from the version string an existing interpreter reports.
python_verdict() {
  case "$1" in
    "Python $PY_VERSION") printf 'skip\n' ;;
    *) printf 'build\n' ;;
  esac
}

# Pure: configure flags for the build.
configure_flags() {
  printf '%s\n' "--prefix=$PY_PREFIX" --without-ensurepip
  if [ "${1:-0}" -eq 1 ]; then printf '%s\n' --enable-optimizations --with-lto; fi
}

# Pure: "ok" | "mismatch" comparing a sha256sum line's hash to the pin.
checksum_verdict() {
  _actual="$(printf '%s' "$1" | cut -d' ' -f1)"
  if [ "$_actual" = "$2" ]; then printf 'ok\n'; else printf 'mismatch\n'; fi
}

verify_checksum() {   # $1 file  $2 expected
  _v="$(checksum_verdict "$(sha256sum "$1")" "$2")"
  [ "$_v" = "ok" ] || die "checksum mismatch for $(basename "$1") — refusing to continue"
}

ensure_python() {
  _have="$("$PY_BIN" --version 2>/dev/null || true)"
  if [ "$(python_verdict "$_have")" = "skip" ]; then info "Python ${PY_VERSION} present; skipping build"; return 0; fi
  _t0="$(now_s)"
  scratch_dir; _tmp="$SCRATCH"
  info "Downloading Python ${PY_VERSION} ..."
  curl -fsSL "${PY_URL_BASE}/${PY_VERSION}/Python-${PY_VERSION}.tgz" -o "$_tmp/py.tgz" || die "Python download failed"
  verify_checksum "$_tmp/py.tgz" "$PY_SHA256"
  tar -xzf "$_tmp/py.tgz" -C "$_tmp"
  info "Building Python ${PY_VERSION} (plain configure$([ "$OPT_OPTIMIZE" -eq 1 ] && printf ' + PGO/LTO'))... this is the slow step"
  _oldifs=$IFS; IFS='
'
  # shellcheck disable=SC2046
  set -- $(configure_flags "$OPT_OPTIMIZE")
  IFS=$_oldifs
  ( cd "$_tmp/Python-${PY_VERSION}" && ./configure "$@" >/dev/null && make -j"$(nproc)" >/dev/null && make altinstall >/dev/null ) \
    || die "Python build failed (log above). Try again without --optimize, or add swap."
  rm -rf "$_tmp"
  info "Python built in $(( $(now_s) - _t0 ))s"
}

# ── Rust toolchain ────────────────────────────────────────────────────────────
# Pure: "install" | "skip" from `cargo --version` output ("" when absent).
cargo_verdict() {
  _minor="$(printf '%s' "$1" | sed -n 's/^cargo 1\.\([0-9][0-9]*\).*/\1/p')"
  if [ -n "$_minor" ] && [ "$_minor" -ge "$RUST_MIN_MINOR" ]; then printf 'skip\n'; else printf 'install\n'; fi
}

rust_env() { RUSTUP_HOME="$RUST_HOME"; CARGO_HOME="$RUST_HOME"; export RUSTUP_HOME CARGO_HOME; PATH="$RUST_HOME/bin:$PATH"; export PATH; }

# Pure: "yes" when this architecture needs a Rust toolchain. Only i686 lacks wheels for
# cryptography, bcrypt and orjson; x86_64 and aarch64 install them as wheels.
needs_rust() { case "$1" in i386|i686) printf 'yes\n' ;; *) printf 'no\n' ;; esac; }

ensure_rust() {
  if [ "$(needs_rust "$ARCH")" != "yes" ]; then info "Rust toolchain not needed on ${ARCH} (wheels available); skipping"; return 0; fi
  rust_env
  if [ "$(cargo_verdict "$(cargo --version 2>/dev/null || true)")" = "skip" ]; then info "Rust toolchain present; skipping"; return 0; fi
  _t0="$(now_s)"; _triple="$(rust_triple "$ARCH")"; _sha="$(rustup_sha256_for "$ARCH")"
  scratch_dir; _tmp="$SCRATCH"
  info "Downloading rustup ${RUSTUP_VERSION} (${_triple}) ..."
  curl -fsSL "${RUSTUP_URL_BASE}/${RUSTUP_VERSION}/${_triple}/rustup-init" -o "$_tmp/rustup-init" || die "rustup download failed"
  verify_checksum "$_tmp/rustup-init" "$_sha"
  chmod 0755 "$_tmp/rustup-init"
  "$_tmp/rustup-init" -y -q --no-modify-path --profile minimal --default-toolchain stable >/dev/null || die "rustup install failed"
  rm -rf "$_tmp"
  info "Rust toolchain installed in $(( $(now_s) - _t0 ))s"
}

# ── venv ──────────────────────────────────────────────────────────────────────
# Pure: "create" | "recreate" | "reuse" from the venv's recorded version ("" = no venv)
# and the prefix interpreter's version.
venv_verdict() {
  [ -n "$1" ] || { printf 'create\n'; return 0; }
  if [ "$1" = "$2" ]; then printf 'reuse\n'; else printf 'recreate\n'; fi
}

venv_recorded_version() {
  [ -f "$VENV_DIR/pyvenv.cfg" ] || return 0
  sed -n 's/^version *= *//p' "$VENV_DIR/pyvenv.cfg" | tr -d ' '
}

ensure_venv() {
  _py_ver="$("$PY_BIN" -c 'import platform; print(platform.python_version())')"
  _fresh_venv=0
  case "$(venv_verdict "$(venv_recorded_version)" "$_py_ver")" in
    create)   info "Creating venv ..."; "$PY_BIN" -m venv "$VENV_DIR"; _fresh_venv=1 ;;
    recreate) info "Interpreter changed; recreating venv ..."; rm -rf "$VENV_DIR"; "$PY_BIN" -m venv "$VENV_DIR"; _fresh_venv=1 ;;
    reuse)    : ;;
  esac
  _t0="$(now_s)"
  _pip="$VENV_DIR/bin/pip"
  if [ ! -x "$_pip" ]; then
    if ! "$VENV_DIR/bin/python" -m ensurepip --upgrade >/dev/null 2>&1; then
      scratch_dir; _gp="$SCRATCH/get-pip.py"
      curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "$_gp" || die "pip bootstrap download failed (ensurepip unavailable and get-pip.py unreachable)"
      "$VENV_DIR/bin/python" "$_gp" >/dev/null || die "pip bootstrap failed"
    fi
    _fresh_venv=1
  fi
  # A pip upgrade is one network round-trip per re-run; only a fresh venv needs it.
  [ "$_fresh_venv" -eq 1 ] && "$_pip" install -q --upgrade pip >/dev/null
  info "Installing PyGObject and the project (compiling the no-wheel packages; slow on 32-bit) ..."
  [ "$(needs_rust "$ARCH")" = "yes" ] && rust_env
  # --prefer-binary: take an older wheel over a newer sdist. On i686 that is the
  # difference between av 15.0.0 (wheel) and av 19 (source, needs FFmpeg 7).
  "$_pip" install -q --prefer-binary "$PYGOBJECT_PIN" || die "PyGObject build failed"
  "$_pip" install -q --prefer-binary -e "$APP_DIR" || die "project install failed"
  info "Dependencies installed in $(( $(now_s) - _t0 ))s"
}

# ── env file + unit (pure renderers) ──────────────────────────────────────────
# Pure: prints the env file.
#   $1 existing file contents ("" on first install)  $2 data_dir
#   $3 bind_host ("" = wildcard 0.0.0.0, the image's default; an explicit --bind-host pins it)
#   $4 port  $5 app_dir  $6 sha  $7 build_time  $8 detected LAN address (for STREAM_BASE_URL)
render_env() {
  _existing="$1"; _data="$2"; _bind="${3:-0.0.0.0}"; _port="$4"; _app="$5"; _sha="$6"; _time="$7"; _lan="${8:-}"
  if [ -n "$_existing" ]; then
    # Keep every line that is not one of ours; rewrite only the badge trio.
    printf '%s\n' "$_existing" | grep -Ev "^($(printf '%s' "$BADGE_VARS" | tr ' ' '|'))="
  else
    cat <<EOF
# Jukeplox native install — the one supported configuration surface.
# Same names as the Docker image's -e flags. Edit, then: systemctl restart jukeplox
DATA_DIR=${_data}
BIND_HOST=${_bind}
PORT=${_port}
LOG_LEVEL=info
EOF
    # Cast/DLNA need an absolute stream URL only on a non-80 port, and only a real
    # address works there (never 0.0.0.0). A pinned address here goes stale if DHCP
    # renumbers the box — the comment says so.
    _stream_host="$_lan"; case "$_bind" in 0.0.0.0|::|"") : ;; *) _stream_host="$_bind" ;; esac
    case "$_stream_host" in 0.0.0.0|::|"") _stream_host="" ;; esac
    if [ "$_port" != "80" ] && [ -n "$_stream_host" ]; then
      printf '# Pinned at install time; update it if this machine changes address.
STREAM_BASE_URL=http://%s:%s
' "$_stream_host" "$_port"
    else
      printf '#STREAM_BASE_URL=\n'
    fi
    cat <<EOF
# PulseAudio/PipeWire output needs the service to run as that desktop user
# (install with --run-as <user>); ALSA needs nothing here.
#PULSE_SERVER=unix:/run/user/1000/pulse/native
#JUKEPLOX_CLIAP2_BIN=/usr/local/bin/cliap2
#JUKEPLOX_CLIRAOP_BIN=/usr/local/bin/cliraop
#JUKEPLOX_SNAPSERVER_BIN=/usr/bin/snapserver
# Default is the checkout's config/snapserver.conf (follows the checkout if it moves).
#JUKEPLOX_SNAPSERVER_CONF=${_app}/config/snapserver.conf
EOF
  fi
  cat <<EOF
JUKEPLOX_GIT_SHA=${_sha}
JUKEPLOX_BUILD_TIME=${_time}
JUKEPLOX_IMAGE_TAG=native
EOF
}

# Pure: the effective value of NAME in env-file CONTENT (last uncommented assignment), or "".
env_value() { printf '%s\n' "$2" | sed -n "s/^$1=//p" | tail -n1; }

# Pure: the User= of a rendered unit, or "".
unit_user() { printf '%s\n' "$1" | sed -n 's/^User=//p' | head -n1; }

# Pure: prints the systemd unit.  $1 user  $2 app_dir  $3 venv  $4 env_file
render_unit() {
  _user="$1"; _app="$2"; _venv="$3"; _envf="$4"
  cat <<EOF
[Unit]
Description=Jukeplox party jukebox
After=network-online.target avahi-daemon.service
Wants=network-online.target avahi-daemon.service
StartLimitIntervalSec=0

[Service]
Type=exec
User=${_user}
Group=${_user}
SupplementaryGroups=audio
WorkingDirectory=${_app}
EnvironmentFile=${_envf}
# Writable cache outside the data dir: GStreamer registry + bytecode for a root-owned checkout.
CacheDirectory=jukeplox
Environment=XDG_CACHE_HOME=${CACHE_DIR} PYTHONPYCACHEPREFIX=${CACHE_DIR}/pycache
# Port 80 without root. No device-isolation directive: the Direct backend opens /dev/snd.
AmbientCapabilities=CAP_NET_BIND_SERVICE
# The data dir is 0750, but the files it writes (secret.key, jukeplox.db) should be private too.
UMask=0077
ExecStart=${_venv}/bin/uvicorn app.main:app --host \${BIND_HOST} --port \${PORT}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
}

# ── service install ───────────────────────────────────────────────────────────
ensure_user() {
  id -u "$1" >/dev/null 2>&1 && return 0
  useradd --system --home-dir "$2" --no-create-home --shell /usr/sbin/nologin --user-group "$1" \
    || die "could not create user $1"
}

# The unit chdirs into the checkout and imports it as that user. A checkout under a
# 0700 home (/root/jukeplox-src) fails systemd's CHDIR step with a bare "Permission
# denied" crash-loop, so refuse up front with the fix in the message (VM lane, check 1).
app_access_verdict() {   # $1 user  $2 app_dir -> ok | denied
  if runuser -u "$1" -- sh -c "test -x '$2' && test -x '$2/app' && test -r '$2/app/main.py'" 2>/dev/null; then
    printf 'ok
'
  else
    printf 'denied
'
  fi
}

ensure_dirs() {   # $1 user  $2 data_dir
  # install -d applies the mode to an existing directory too, so a re-run re-tightens it.
  install -d -m 0750 -o "$1" -g "$1" "$2"
  install -d -m 0750 -o root -g "$1" "$ENV_DIR"
}

write_env() {   # $1 user  $2 rendered contents
  if [ -f "$ENV_FILE" ]; then install -m 0640 -o root -g "$1" "$ENV_FILE" "$ENV_FILE.bak"; fi
  printf '%s\n' "$2" > "$ENV_FILE.tmp"
  install -m 0640 -o root -g "$1" "$ENV_FILE.tmp" "$ENV_FILE"
  rm -f "$ENV_FILE.tmp"
}

install_service() {   # $1 user  $2 data_dir  $3 app_dir  $4 unit contents  $5 env contents
  _fresh=0; id -u "$1" >/dev/null 2>&1 || _fresh=1
  ensure_user "$1" "$2"
  [ "$(app_access_verdict "$1" "$3")" = "ok" ]     || die "user $1 cannot read the checkout at $3 (is it under a private home directory?); move it to ${DEFAULT_PREFIX}/app or make every directory above it world-traversable (chmod o+rx) and re-run"
  ensure_dirs "$1" "$2"
  write_env "$1" "$5"
  printf '%s\n' "$4" > "$UNIT_PATH"
  systemctl daemon-reload
  if [ "$_fresh" -eq 1 ] || ! systemctl is-enabled --quiet jukeplox 2>/dev/null; then
    systemctl enable --now jukeplox
  else
    systemctl restart jukeplox
  fi
}

wait_version() {   # $1 url  $2 tries → prints the reported git_sha, or fails
  _tries="$2"
  while [ "$_tries" -gt 0 ]; do
    _json="$(curl -fsS --max-time 3 "$1/api/version" 2>/dev/null || true)"
    if [ -n "$_json" ]; then printf '%s\n' "$_json" | sed -n 's/.*"git_sha": *"\([^"]*\)".*/\1/p'; return 0; fi
    _tries=$((_tries - 1)); sleep 1
  done
  return 1
}

# ── entry point ───────────────────────────────────────────────────────────────
main() {
  set -eu
  trap cleanup_scratch EXIT
  parse_args "$@"
  layout
  preflight
  ensure_checkout
  install_packages
  ensure_python
  ensure_rust
  ensure_venv

  _sha="$(badge_sha)"
  [ -n "$_sha" ] || die "cannot read the checkout's git sha (is ${APP_DIR} a git checkout with git installed?); refusing to write an unknown badge"

  if [ "$OPT_NO_SERVICE" -eq 1 ]; then
    info ""
    info "Installed without a service (--no-service). Run by hand from ${APP_DIR}:"
    info "  JUKEPLOX_GIT_SHA=${_sha} JUKEPLOX_BUILD_TIME=$(utc_now) JUKEPLOX_IMAGE_TAG=native DATA_DIR=${OPT_DATA_DIR} \\"
    info "    ${VENV_DIR}/bin/uvicorn app.main:app --host 0.0.0.0 --port ${OPT_PORT}"
    return 0
  fi

  _lan="$(detect_lan_ip)"
  if [ -n "$OPT_BIND_HOST" ]; then
    [ "$(bind_host_verdict "$OPT_BIND_HOST" "$(local_addrs)")" = "local" ] || die "--bind-host ${OPT_BIND_HOST} is not an address on this machine"
  fi
  if [ ! -f "$ENV_FILE" ] && [ "$(port_verdict "$OPT_PORT")" = "busy" ]; then
    die "port ${OPT_PORT} is already in use; pass --port <n> (Cast/DLNA then use STREAM_BASE_URL automatically)"
  fi

  # A re-run honours what the box already has: the env file's values win over flag
  # defaults, and the unit's User= wins unless --run-as was passed explicitly.
  _existing=""; [ -f "$ENV_FILE" ] && _existing="$(cat "$ENV_FILE")"
  _env="$(render_env "$_existing" "$OPT_DATA_DIR" "$OPT_BIND_HOST" "$OPT_PORT" "$APP_DIR" "$_sha" "$(utc_now)" "$_lan")"
  _eff_port="$(env_value PORT "$_env")"; _eff_port="${_eff_port:-$OPT_PORT}"
  _eff_bind="$(env_value BIND_HOST "$_env")"; _eff_bind="${_eff_bind:-0.0.0.0}"
  _eff_data="$(env_value DATA_DIR "$_env")"; _eff_data="${_eff_data:-$OPT_DATA_DIR}"
  _user="$OPT_RUN_AS"
  if [ "$OPT_RUN_AS_SET" -eq 0 ] && [ -f "$UNIT_PATH" ]; then
    _prev="$(unit_user "$(cat "$UNIT_PATH")")"; [ -n "$_prev" ] && _user="$_prev"
  fi
  info "Listening on ${_eff_bind}:${_eff_port} as ${_user}; LAN address ${_lan}"
  _unit="$(render_unit "$_user" "$APP_DIR" "$VENV_DIR" "$ENV_FILE")"
  install_service "$_user" "$_eff_data" "$APP_DIR" "$_unit" "$_env"

  # Probe where the service actually listens; show the address people will type.
  case "$_eff_bind" in 0.0.0.0|::) _probe="127.0.0.1"; _show="$_lan" ;; *) _probe="$_eff_bind"; _show="$_eff_bind" ;; esac
  _url="http://${_probe}:${_eff_port}"
  if _got="$(wait_version "$_url" "$HEALTH_TRIES")"; then
    info ""
    info "Jukeplox is running (build ${_got}, native):"
    info "  Admin   : http://${_show}:${_eff_port}/admin   (set a password, connect a source, pick a speaker)"
    info "  Config  : ${ENV_FILE}      Data: ${_eff_data}      Logs: journalctl -u jukeplox -f"
    info "  Update  : cd ${APP_DIR} && git pull && sh install-native.sh"
  else
    info "Service did not answer at ${_url}/api/version. Last log lines:"
    journalctl -u jukeplox -n 30 --no-pager >&2 || true
    die "install finished but the service is not healthy"
  fi
}

if [ -z "${JUKEPLOX_INSTALL_LIB:-}" ]; then
  main "$@"
fi
