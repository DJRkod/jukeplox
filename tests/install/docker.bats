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

@test "U3 (AE4): install_docker fetches the convenience script" {
  marker="$(mktemp)"; rm -f "$marker"
  make_stub curl "touch '$marker'; echo ''"
  make_stub sudo 'exec "$@"'
  make_stub sh 'cat >/dev/null'
  make_stub systemctl 'exit 0'
  run install_docker
  [ "$status" -eq 0 ]
  [ -f "$marker" ]
  rm -f "$marker"
}

@test "U3: preflight passes when docker info succeeds (no sudo)" {
  make_stub docker 'case "$1" in info) exit 0 ;; *) exit 0 ;; esac'
  run preflight_docker
  [ "$status" -eq 0 ]
}
