#!/usr/bin/env bats
# Native installer (plan U2) — CLI flags, preflight verdicts, checkout update decision.

load helpers

setup() { setup_native_lib; setup_stub_bin; }
teardown() { teardown_stub_bin; }

@test "U2: --help prints usage and exits 0" {
  run sh "$PROJECT_ROOT/install-native.sh" --help
  [ "$status" -eq 0 ]
  [[ "$output" == *"Usage: sh install-native.sh"* ]]
}

@test "U2: unknown flag exits non-zero" {
  run sh "$PROJECT_ROOT/install-native.sh" --bogus
  [ "$status" -ne 0 ]
}

@test "U2: parse_args populates every flag, both forms" {
  parse_args --source /src --prefix /opt/x --data-dir /var/x --port 8096 --bind-host 10.0.0.5 --run-as media --optimize --no-service --yes
  [ "$OPT_SOURCE" = "/src" ]; [ "$OPT_PREFIX" = "/opt/x" ]; [ "$OPT_DATA_DIR" = "/var/x" ]
  [ "$OPT_PORT" = "8096" ]; [ "$OPT_BIND_HOST" = "10.0.0.5" ]; [ "$OPT_RUN_AS" = "media" ]
  [ "$OPT_OPTIMIZE" -eq 1 ]; [ "$OPT_NO_SERVICE" -eq 1 ]; [ "$OPT_YES" -eq 1 ]
  parse_args --port=70 --run-as=svc
  [ "$OPT_PORT" = "70" ]; [ "$OPT_RUN_AS" = "svc" ]
}

@test "U2: --yes skips the confirmation prompt (no read, default taken)" {
  OPT_YES=1
  run confirm "Proceed?"
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}

@test "U2: layout derives paths from the prefix, --source overrides the app dir" {
  parse_args --prefix /opt/j; layout
  [ "$APP_DIR" = "/opt/j/app" ]; [ "$VENV_DIR" = "/opt/j/venv" ]; [ "$PY_BIN" = "/opt/j/python/bin/python3.12" ]
  parse_args --source /src; layout
  [ "$APP_DIR" = "/src" ]
}

@test "R7 (review #6): a flagless re-run adopts the --source and --prefix recorded in the env file" {
  ENV_DIR="$BATS_TEST_TMPDIR/etc"; mkdir -p "$ENV_DIR"
  printf 'PORT=80\nJUKEPLOX_INSTALL_PREFIX=/srv/jp\nJUKEPLOX_INSTALL_SOURCE=/home/me/jukeplox\n' > "$ENV_DIR/env"
  parse_args; layout
  [ "$APP_DIR" = "/home/me/jukeplox" ]
  [ "$VENV_DIR" = "/srv/jp/venv" ]
  [ "$OPT_PREFIX" = "/srv/jp" ]
  [ "$OPT_SOURCE" = "/home/me/jukeplox" ]
}

@test "R7: an explicit flag wins over the recorded layout and the mismatch is announced" {
  ENV_DIR="$BATS_TEST_TMPDIR/etc"; mkdir -p "$ENV_DIR"
  printf 'JUKEPLOX_INSTALL_PREFIX=/srv/jp\nJUKEPLOX_INSTALL_SOURCE=\n' > "$ENV_DIR/env"
  parse_args --prefix /other; layout 2> "$BATS_TEST_TMPDIR/err"
  [ "$APP_DIR" = "/other/app" ]
  [ "$VENV_DIR" = "/other/venv" ]
  grep -q "/srv/jp" "$BATS_TEST_TMPDIR/err"
}

@test "R7 (review): --source= clears a recorded source and announces the switch to a managed clone" {
  ENV_DIR="$BATS_TEST_TMPDIR/etc"; mkdir -p "$ENV_DIR"
  printf 'JUKEPLOX_INSTALL_PREFIX=/srv/jp\nJUKEPLOX_INSTALL_SOURCE=/home/me/jukeplox\n' > "$ENV_DIR/env"
  parse_args --source=; layout 2> "$BATS_TEST_TMPDIR/err"
  [ "$APP_DIR" = "/srv/jp/app" ]
  [ -z "$OPT_SOURCE" ]
  grep -q "managed clone" "$BATS_TEST_TMPDIR/err"
}

@test "R7 (review): an install that predates the layout record is read back from its unit" {
  ENV_DIR="$BATS_TEST_TMPDIR/etc"; mkdir -p "$ENV_DIR"
  printf 'PORT=80\nJUKEPLOX_GIT_SHA=old0000\n' > "$ENV_DIR/env"   # written by the previous installer: no JUKEPLOX_INSTALL_* lines
  UNIT_PATH="$BATS_TEST_TMPDIR/jukeplox.service"
  render_unit jukeplox /home/me/jukeplox /srv/jp/venv "$ENV_DIR/env" > "$UNIT_PATH"
  parse_args; layout
  [ "$APP_DIR" = "/home/me/jukeplox" ]
  [ "$VENV_DIR" = "/srv/jp/venv" ]
  [ "$OPT_PREFIX" = "/srv/jp" ]
  render_unit jukeplox /opt/jukeplox/app /opt/jukeplox/venv "$ENV_DIR/env" > "$UNIT_PATH"
  OPT_SOURCE=""; OPT_SOURCE_SET=0; OPT_PREFIX="$DEFAULT_PREFIX"; OPT_PREFIX_SET=0; layout
  [ "$APP_DIR" = "/opt/jukeplox/app" ]   # a managed clone under the prefix is not a --source
  [ -z "$OPT_SOURCE" ]
}

@test "R7 (review): an adopted source that is no longer a checkout dies naming the record, not a flag" {
  ENV_DIR="$BATS_TEST_TMPDIR/etc"; mkdir -p "$ENV_DIR"
  printf 'JUKEPLOX_INSTALL_PREFIX=/opt/jukeplox\nJUKEPLOX_INSTALL_SOURCE=%s/gone\n' "$BATS_TEST_TMPDIR" > "$ENV_DIR/env"
  parse_args; layout
  run ensure_checkout
  [ "$status" -ne 0 ]
  [[ "$output" == *"recorded source checkout"* ]]
  [[ "$output" == *"--source="* ]]
}

@test "R7: no env file and no flags is the default layout, silently" {
  ENV_DIR="$BATS_TEST_TMPDIR/etc"
  parse_args; layout 2> "$BATS_TEST_TMPDIR/err"
  [ "$APP_DIR" = "/opt/jukeplox/app" ]
  [ ! -s "$BATS_TEST_TMPDIR/err" ]
}

@test "U2: os_verdict warns (not dies) on Debian 13 and non-Debian, ok on 12" {
  printf 'ID=debian\nVERSION_ID="12"\n' > "$BATS_TEST_TMPDIR/os12"
  printf 'ID=debian\nVERSION_ID="13"\n' > "$BATS_TEST_TMPDIR/os13"
  printf 'ID=ubuntu\nVERSION_ID="24.04"\n' > "$BATS_TEST_TMPDIR/osu"
  [ "$(os_verdict "$BATS_TEST_TMPDIR/os12")" = "ok" ]
  [[ "$(os_verdict "$BATS_TEST_TMPDIR/os13")" == warn* ]]
  [[ "$(os_verdict "$BATS_TEST_TMPDIR/osu")" == warn* ]]
}

@test "U2 (AE8): preflight does not refuse amd64 — the arch mapping covers it" {
  [ -n "$(rust_triple amd64)" ]
  [ -n "$(airplay_suffix amd64)" ]
  [ "$(rust_triple i386)" = "i686-unknown-linux-gnu" ]
  [ -z "$(airplay_suffix i386)" ]
  [ -z "$(rust_triple mips)" ]
}

@test "U2: port_verdict reports busy from ss and free otherwise" {
  make_stub ss 'echo "LISTEN 0 128 0.0.0.0:80 0.0.0.0:*"'
  [ "$(port_verdict 80)" = "busy" ]
  [ "$(port_verdict 8096)" = "free" ]
}

@test "U2: bind_host_verdict accepts wildcard and local addresses, rejects foreign" {
  addrs="$(printf '127.0.0.1\n192.168.1.50\n')"
  [ "$(bind_host_verdict 0.0.0.0 "$addrs")" = "local" ]
  [ "$(bind_host_verdict 192.168.1.50 "$addrs")" = "local" ]
  [ "$(bind_host_verdict 10.9.9.9 "$addrs")" = "foreign" ]
}

@test "U2: update_verdict — clone when absent, ff when clean ancestor" {
  [ "$(update_verdict absent "" "")" = "clone" ]
  [ "$(update_verdict present "" ancestor)" = "ff" ]
}

@test "U2: update_verdict stops on a dirty managed clone and names the file" {
  out="$(update_verdict present " M config/snapserver.conf" ancestor)"
  [[ "$out" == stop* ]]
  [[ "$out" == *"config/snapserver.conf"* ]]
}

@test "U2: update_verdict stops on a diverged managed clone, never resets" {
  out="$(update_verdict present "" diverged)"
  [[ "$out" == stop* ]]
  ! grep -q 'reset --hard' "$PROJECT_ROOT/install-native.sh"
}

@test "U2: --source performs no fetch/ff/dirty check — ensure_checkout only validates the tree" {
  mkdir -p "$BATS_TEST_TMPDIR/src/app"
  make_stub git 'echo "GIT $*" >&2; exit 1'   # any git call would fail loudly
  parse_args --source "$BATS_TEST_TMPDIR/src"; layout
  run ensure_checkout
  [ "$status" -eq 0 ]
  [[ "$output" != *"GIT "* ]]
}

@test "U2: --source on a non-checkout dies" {
  mkdir -p "$BATS_TEST_TMPDIR/empty"
  parse_args --source "$BATS_TEST_TMPDIR/empty"; layout
  run ensure_checkout
  [ "$status" -ne 0 ]
}

@test "F8: no-TTY prompt read has stderr redirect before /dev/tty" {
  grep -Eq 'read -r _ans 2>/dev/null </dev/tty' "$PROJECT_ROOT/install-native.sh"
}
