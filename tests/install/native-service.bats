#!/usr/bin/env bats
# Native installer (plan U3) — env file rendering/preservation, unit rendering, service install order.

load helpers

setup() { setup_native_lib; setup_stub_bin; }
teardown() { teardown_stub_bin; }

@test "U3: fresh render_env names every template variable and carries the port" {
  out="$(render_env "" /var/lib/jukeplox 192.168.1.50 8096 /opt/jukeplox/app abc1234 2026-10-24T00:00Z 192.168.1.50)"
  for v in $ENV_TEMPLATE_VARS; do printf '%s\n' "$out" | grep -Eq "^#?${v}=" || { echo "missing $v" >&2; return 1; }; done
  assert_line_eq "$out" "PORT=8096"
  assert_line_eq "$out" "BIND_HOST=192.168.1.50"
  assert_line_eq "$out" "STREAM_BASE_URL=http://192.168.1.50:8096"
  assert_line_eq "$out" "#JUKEPLOX_SNAPSERVER_CONF=/opt/jukeplox/app/config/snapserver.conf"
  refute_line_eq "$out" "JUKEPLOX_SNAPSERVER_CONF=/opt/jukeplox/app/config/snapserver.conf"
}

@test "U3: fresh render_env on port 80 leaves STREAM_BASE_URL unset" {
  out="$(render_env "" /var/lib/jukeplox 192.168.1.50 80 /opt/jukeplox/app abc1234 t 192.168.1.50)"
  refute_line_eq "$out" "STREAM_BASE_URL=http://192.168.1.50:80"
}

@test "U3: no --bind-host binds all interfaces; STREAM_BASE_URL uses the LAN address on a non-80 port" {
  out="$(render_env "" /d "" 8096 /a sha t 192.168.1.50)"
  assert_line_eq "$out" "BIND_HOST=0.0.0.0"
  assert_line_eq "$out" "STREAM_BASE_URL=http://192.168.1.50:8096"
}

@test "U3: wildcard bind with no LAN address never writes http://0.0.0.0" {
  out="$(render_env "" /d "" 8096 /a sha t 0.0.0.0)"
  refute_substr "$out" "http://0.0.0.0"
  assert_line_eq "$out" "#STREAM_BASE_URL="
  out2="$(render_env "" /d 0.0.0.0 8096 /a sha t "")"
  refute_substr "$out2" "http://0.0.0.0"
}

@test "U3: env_value returns the last uncommented assignment; unit_user reads User=" {
  env="$(printf '#PORT=1\nPORT=80\nPORT=8096\nBIND_HOST=10.0.0.5\n')"
  [ "$(env_value PORT "$env")" = "8096" ]
  [ "$(env_value BIND_HOST "$env")" = "10.0.0.5" ]
  [ -z "$(env_value DATA_DIR "$env")" ]
  unit="$(render_unit media /a /v /e)"
  [ "$(unit_user "$unit")" = "media" ]
}

@test "U3 (review #3): a re-run derives the probe port from the preserved env, not the flag default" {
  existing="$(printf 'DATA_DIR=/var/lib/jukeplox\nBIND_HOST=0.0.0.0\nPORT=8096\nJUKEPLOX_GIT_SHA=old\n')"
  out="$(render_env "$existing" /var/lib/jukeplox "" 80 /a new t 192.168.1.50)"
  [ "$(env_value PORT "$out")" = "8096" ]
  [ "$(env_value BIND_HOST "$out")" = "0.0.0.0" ]
}

@test "U3 (review #5): scratch_dir registers in the parent shell so cleanup_scratch removes it" {
  _TMPDIRS=""
  scratch_dir; d="$SCRATCH"
  [ -d "$d" ]
  [[ "$_TMPDIRS" == *"$d"* ]]
  cleanup_scratch
  [ ! -d "$d" ]
}

@test "U3: badge lines carry the stubbed sha, time and native tag" {
  out="$(render_env "" /d 10.0.0.2 80 /a deadbee 2026-10-24T01:02Z)"
  assert_line_eq "$out" "JUKEPLOX_GIT_SHA=deadbee"
  assert_line_eq "$out" "JUKEPLOX_BUILD_TIME=2026-10-24T01:02Z"
  assert_line_eq "$out" "JUKEPLOX_IMAGE_TAG=native"
}

@test "U3 (AE3): re-run preserves user edits, user-added variables and comments; rewrites only badge lines" {
  existing="$(printf '# my note\nBIND_HOST=192.168.1.77\nPORT=80\nMY_EXTRA=1\nJUKEPLOX_GIT_SHA=old0000\nJUKEPLOX_BUILD_TIME=then\nJUKEPLOX_IMAGE_TAG=native\n')"
  out="$(render_env "$existing" /d 10.0.0.2 80 /a new1111 now)"
  assert_line_eq "$out" "# my note"
  assert_line_eq "$out" "BIND_HOST=192.168.1.77"   # user's value kept, not the detected one
  assert_line_eq "$out" "MY_EXTRA=1"
  assert_line_eq "$out" "JUKEPLOX_GIT_SHA=new1111"
  refute_line_eq "$out" "JUKEPLOX_GIT_SHA=old0000"
  [ "$(printf '%s\n' "$out" | grep -c '^JUKEPLOX_IMAGE_TAG=')" -eq 1 ]
}

@test "U3: render_unit carries the hardening and lifecycle directives, execs uvicorn with no shell" {
  out="$(render_unit jukeplox /opt/jukeplox/app /opt/jukeplox/venv /etc/jukeplox/env)"
  for line in "Type=exec" "User=jukeplox" "Restart=on-failure" "RestartSec=5" "StartLimitIntervalSec=0" \
              "AmbientCapabilities=CAP_NET_BIND_SERVICE" "SupplementaryGroups=audio" "CacheDirectory=jukeplox" "UMask=0077" \
              "EnvironmentFile=/etc/jukeplox/env" "WorkingDirectory=/opt/jukeplox/app" \
              "After=network-online.target avahi-daemon.service"; do
    assert_line_eq "$out" "$line"
  done
  printf '%s\n' "$out" | grep -q '^ExecStart=/opt/jukeplox/venv/bin/uvicorn app.main:app'
  refute_substr "$out" "ExecStart=/bin/sh"
  if printf '%s
' "$out" | grep -q '^PrivateDevices='; then return 1; fi
}

@test "U3: --run-as substitutes the user in the unit" {
  out="$(render_unit media /a /v /e)"
  assert_line_eq "$out" "User=media"
  assert_line_eq "$out" "Group=media"
}

@test "U3: ensure_dirs creates the data dir 0750 owned by the user and the env dir 0750 root:user" {
  log="$BATS_TEST_TMPDIR/install.log"
  make_stub install 'echo "install $*" >> '"$log"
  make_stub chmod 'echo "chmod $*" >> '"$log"
  ENV_DIR=/etc/jukeplox
  ensure_dirs jukeplox /var/lib/jukeplox
  grep -q "install -d -m 0750 -o jukeplox -g jukeplox /var/lib/jukeplox" "$log"
  grep -q "install -d -m 0750 -o root -g jukeplox /etc/jukeplox" "$log"
}

@test "U3: write_env installs the file 0640 root:user and backs up the previous one" {
  log="$BATS_TEST_TMPDIR/install.log"
  ENV_DIR="$BATS_TEST_TMPDIR/etc"; ENV_FILE="$ENV_DIR/env"; mkdir -p "$ENV_DIR"; echo old > "$ENV_FILE"
  make_stub install 'echo "install $*" >> '"$log"
  write_env jukeplox "NEW=1"
  grep -q "install -m 0640 -o root -g jukeplox $ENV_FILE $ENV_FILE.bak" "$log"
  grep -q "install -m 0640 -o root -g jukeplox $ENV_FILE.tmp $ENV_FILE" "$log"
}

@test "U3: install_service on a fresh box — useradd, dirs (chown via install), daemon-reload, enable --now, in order" {
  log="$BATS_TEST_TMPDIR/calls.log"
  make_stub id 'exit 1'                                  # user absent
  make_stub runuser 'exit 0'
  make_stub useradd 'echo "useradd $*" >> '"$log"
  make_stub install 'echo "install $*" >> '"$log"
  make_stub chmod 'true'
  make_stub systemctl 'echo "systemctl $*" >> '"$log"
  ENV_DIR="$BATS_TEST_TMPDIR/etc"; ENV_FILE="$ENV_DIR/env"; UNIT_PATH="$BATS_TEST_TMPDIR/jukeplox.service"; mkdir -p "$ENV_DIR"
  install_service jukeplox /var/lib/jukeplox /opt/jukeplox/app "[Unit]" "X=1"
  awk 'NR==1' "$log" | grep -q '^useradd'
  grep -n "install -d -m 0750 -o jukeplox" "$log" | head -1 | cut -d: -f1 | { read -r n; [ "$n" -lt "$(grep -n 'systemctl daemon-reload' "$log" | cut -d: -f1)" ]; }
  grep -q "systemctl enable --now jukeplox" "$log"
  if grep -q "systemctl restart" "$log"; then return 1; fi
  [ "$(cat "$UNIT_PATH")" = "[Unit]" ]
}

@test "U3 (VM lane check 1): app_access_verdict asks the service user, not root" {
  make_stub runuser 'echo "runuser $*" > '"$BATS_TEST_TMPDIR/ru"'; exit 0'
  [ "$(app_access_verdict jukeplox /opt/jukeplox/app)" = "ok" ]
  grep -q -- '-u jukeplox' "$BATS_TEST_TMPDIR/ru"
  grep -q "/opt/jukeplox/app/app/main.py" "$BATS_TEST_TMPDIR/ru"
  make_stub runuser 'exit 1'
  [ "$(app_access_verdict jukeplox /root/jukeplox-src)" = "denied" ]
}

@test "U3 (VM lane check 1): install_service refuses an unreadable checkout before touching systemd" {
  log="$BATS_TEST_TMPDIR/calls.log"
  make_stub id 'exit 0'
  make_stub runuser 'exit 1'
  make_stub install 'true'
  make_stub systemctl 'echo "systemctl $*" >> '"$log"
  ENV_DIR="$BATS_TEST_TMPDIR/etc"; ENV_FILE="$ENV_DIR/env"; UNIT_PATH="$BATS_TEST_TMPDIR/jukeplox.service"; mkdir -p "$ENV_DIR"
  run install_service jukeplox /var/lib/jukeplox /root/jukeplox-src "[Unit]" "X=1"
  [ "$status" -ne 0 ]
  [[ "$output" == *"cannot read the checkout at /root/jukeplox-src"* ]]
  [[ "$output" == *"chmod o+rx"* ]]
  [ ! -f "$log" ]
  [ ! -f "$UNIT_PATH" ]
}

@test "U3: install_service on a re-run — no useradd, restart instead of enable" {
  log="$BATS_TEST_TMPDIR/calls.log"
  make_stub id 'exit 0'                                  # user present
  make_stub runuser 'exit 0'
  make_stub useradd 'echo "useradd $*" >> '"$log"
  make_stub install 'true'
  make_stub chmod 'true'
  make_stub systemctl 'echo "systemctl $*" >> '"$log"'; case "$1" in is-enabled) exit 0;; esac'
  ENV_DIR="$BATS_TEST_TMPDIR/etc"; ENV_FILE="$ENV_DIR/env"; UNIT_PATH="$BATS_TEST_TMPDIR/jukeplox.service"; mkdir -p "$ENV_DIR"
  install_service jukeplox /var/lib/jukeplox /opt/jukeplox/app "[Unit]" "X=1"
  if grep -q "^useradd" "$log"; then return 1; fi
  grep -q "systemctl restart jukeplox" "$log"
}

@test "U3: wait_version returns the reported sha, fails when never healthy" {
  make_stub curl 'echo "{\"git_sha\": \"abc1234\", \"build_time\": \"t\", \"image_tag\": \"native\"}"'
  [ "$(wait_version http://x 2)" = "abc1234" ]
  make_stub curl 'exit 1'
  run wait_version http://x 1
  [ "$status" -ne 0 ]
}
