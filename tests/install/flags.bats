#!/usr/bin/env bats
# U4 — flag assembly (the decision matrix) and container run.

load helpers

setup() { setup_install_lib; setup_stub_bin; }
teardown() { teardown_stub_bin; }

@test "U4 (AE2): desktop pulse wires PULSE_SERVER + socket, never /dev/snd" {
  out="$(assemble_args pulse 192.168.1.50 8096 1000 latest)"
  assert_line_eq "$out" "PULSE_SERVER=unix:/run/user/1000/pulse/native"
  assert_line_eq "$out" "/run/user/1000/pulse/native:/run/user/1000/pulse/native"
  refute_substr "$out" "/dev/snd"
}

@test "U4 (AE3): headless adds /dev/snd + group-add audio, no pulse" {
  out="$(assemble_args alsa 192.168.1.50 80 1000 latest)"
  assert_line_eq "$out" "/dev/snd"
  assert_line_eq "$out" "audio"
  refute_substr "$out" "PULSE_SERVER"
}

@test "U4: audio none is casting-only (no device, no socket), still host networking" {
  out="$(assemble_args none 10.0.0.2 80 1000 latest)"
  refute_substr "$out" "/dev/snd"
  refute_substr "$out" "PULSE_SERVER"
  assert_line_eq "$out" "--network"
  assert_line_eq "$out" "host"
}

@test "U4 (AE1): port 80 omits PORT and STREAM_BASE_URL" {
  out="$(assemble_args alsa 192.168.1.50 80 1000 latest)"
  refute_line_eq "$out" "PORT=80"
  refute_substr "$out" "STREAM_BASE_URL"
}

@test "U4 (AE1): non-80 port adds PORT and STREAM_BASE_URL" {
  out="$(assemble_args alsa 192.168.1.50 8096 1000 latest)"
  assert_line_eq "$out" "PORT=8096"
  assert_line_eq "$out" "STREAM_BASE_URL=http://192.168.1.50:8096"
}

@test "U4 (AE6): version pins the image tag, default is latest" {
  out="$(assemble_args alsa 1.2.3.4 80 1000 9.9.9)"
  assert_line_eq "$out" "djrkod/jukeplox:9.9.9"
  out2="$(assemble_args alsa 1.2.3.4 80 1000 latest)"
  assert_line_eq "$out2" "djrkod/jukeplox:latest"
}

@test "U4: always includes the data volume" {
  out="$(assemble_args none 10.0.0.2 80 1000 latest no)"
  assert_line_eq "$out" "jukeplox-data:/data"
}

# Fresh-install audit F5: the dbus mount is conditional on the host socket
# existing — mounting a nonexistent path made Docker create a stray root-owned
# directory at /run/dbus/system_bus_socket (which INSTALL.md itself warns
# about, and which breaks a later host dbus start).

@test "F5: dbus fact yes emits the socket mount" {
  out="$(assemble_args none 10.0.0.2 80 1000 latest yes)"
  assert_line_eq "$out" "/run/dbus/system_bus_socket:/run/dbus/system_bus_socket"
}

@test "F5: dbus fact no (and omitted) emits no dbus mount" {
  out="$(assemble_args none 10.0.0.2 80 1000 latest no)"
  refute_substr "$out" "system_bus_socket"
  out2="$(assemble_args none 10.0.0.2 80 1000 latest)"
  refute_substr "$out2" "system_bus_socket"
}

@test "F5: other args unchanged across dbus yes/no" {
  for d in yes no; do
    out="$(assemble_args alsa 192.168.1.50 8096 1000 latest "$d")"
    assert_line_eq "$out" "jukeplox-data:/data"
    assert_line_eq "$out" "BIND_HOST=192.168.1.50"
    assert_line_eq "$out" "PORT=8096"
    assert_line_eq "$out" "--device"
    assert_line_eq "$out" "djrkod/jukeplox:latest"
  done
}

@test "F5: detect_dbus reports the socket's existence" {
  # DBUS_SOCKET honors the JUKEPLOX_DBUS_SOCKET override at source time; point
  # it at paths we control. A plain file must NOT count — only a socket would,
  # and mktemp can't make one, so assert the no-socket verdicts.
  DBUS_SOCKET="$BATS_TEST_TMPDIR/absent_socket"
  run detect_dbus
  [ "$output" = "no" ]
  : > "$BATS_TEST_TMPDIR/plain_file"
  DBUS_SOCKET="$BATS_TEST_TMPDIR/plain_file"
  run detect_dbus
  [ "$output" = "no" ]
}

@test "F8: no-TTY prompt reads emit nothing on stderr" {
  # choose_port with a readable-but-unopenable tty is environment-specific;
  # assert the shape instead: the read's stderr redirect precedes the
  # /dev/tty redirect (redirections process left-to-right — a failing open
  # aborts before a later 2> takes effect).
  grep -Eq 'read -r _ans 2>/dev/null </dev/tty' "$PROJECT_ROOT/install.sh"
  grep -Eq 'read -r _p 2>/dev/null </dev/tty' "$PROJECT_ROOT/install.sh"
}

@test "U4 (AE5): run_container recreates an existing container, never removes the volume" {
  log="$(mktemp)"; rm -f "$log"
  make_stub docker 'case "$1" in
    ps) echo jukeplox ;;
    *) echo "docker $*" >> '"$log"' ;;
  esac'
  OPT_YES=1; DOCKER_SUDO=""
  run run_container alsa 192.168.1.50 80 1000 latest
  [ "$status" -eq 0 ]
  grep -q "stop jukeplox" "$log"
  grep -q "rm jukeplox" "$log"
  grep -q "pull djrkod/jukeplox:latest" "$log"
  grep -q "run -d" "$log"
  ! grep -q "volume rm" "$log"
  rm -f "$log"
}
