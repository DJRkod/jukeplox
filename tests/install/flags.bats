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

@test "U4: always includes data volume and dbus mount" {
  out="$(assemble_args none 10.0.0.2 80 1000 latest)"
  assert_line_eq "$out" "jukeplox-data:/data"
  assert_line_eq "$out" "/run/dbus/system_bus_socket:/run/dbus/system_bus_socket"
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
