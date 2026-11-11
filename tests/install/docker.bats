#!/usr/bin/env bats
# U3 — Docker preflight, sudo routing, and install.

load helpers

setup() { setup_install_lib; setup_stub_bin; OPT_YES=1; }
teardown() { teardown_stub_bin; }

@test "U3: docker_q routes through sudo when DOCKER_SUDO=sudo" {
  make_stub sudo 'echo "SUDO $*"'
  make_stub docker 'echo "DOCKER $*"'
  DOCKER_SUDO="sudo"
  run docker_q ps
  [ "$status" -eq 0 ]
  [ "$output" = "SUDO docker ps" ]
}

@test "U3: docker_q runs docker directly when DOCKER_SUDO is empty" {
  make_stub docker 'echo "DOCKER $*"'
  DOCKER_SUDO=""
  run docker_q ps
  [ "$output" = "DOCKER ps" ]
}

@test "U3 (AE4): install_docker fetches the convenience script and enables docker" {
  marker="$BATS_TEST_TMPDIR/fetched"; log="$BATS_TEST_TMPDIR/calls.log"
  # The fake curl records the fetch and hands back a harmless script body that
  # the REAL shell runs through install_docker's pipe. `sh` itself is never
  # faked: with STUB_BIN first on PATH that recursed forever (fixed 2026-11).
  make_stub curl "touch '$marker'; echo 'exit 0'"
  make_stub sudo 'exec "$@"'          # inert as root (the image); real sudo otherwise
  make_stub systemctl 'echo "systemctl $*" >> '"'$log'"
  run install_docker
  [ "$status" -eq 0 ]
  [ -f "$marker" ]
  grep -q "systemctl enable --now docker" "$log"
}

@test "U3 (AE4): install_docker really runs the fetched script (a failing script fails the install)" {
  make_stub curl "echo 'exit 7'"
  make_stub sudo 'exec "$@"'
  make_stub systemctl 'exit 0'
  run install_docker
  [ "$status" -ne 0 ]
  [[ "$output" == *"Docker install failed"* ]]
}

@test "U3: preflight passes when docker info succeeds (no sudo)" {
  make_stub docker 'case "$1" in info) exit 0 ;; *) exit 0 ;; esac'
  run preflight_docker
  [ "$status" -eq 0 ]
}
