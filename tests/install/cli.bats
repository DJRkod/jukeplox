#!/usr/bin/env bats
# U1 — CLI flags, usage, and real-user resolution.

load helpers

setup() { setup_install_lib; }

@test "U1: --help prints usage and exits 0" {
  run sh "$PROJECT_ROOT/install.sh" --help
  [ "$status" -eq 0 ]
  [[ "$output" == *"Usage: install.sh"* ]]
}

@test "U1: parse_args populates port, version, and yes" {
  OPT_PORT=""; OPT_VERSION="latest"; OPT_YES=0
  parse_args --port 8096 --version 1.2.0 --yes
  [ "$OPT_PORT" = "8096" ]
  [ "$OPT_VERSION" = "1.2.0" ]
  [ "$OPT_YES" -eq 1 ]
}

@test "U1: parse_args accepts --flag=value form" {
  OPT_PORT=""; OPT_VERSION="latest"
  parse_args --port=70 --version=2.0.0
  [ "$OPT_PORT" = "70" ]
  [ "$OPT_VERSION" = "2.0.0" ]
}

@test "U1: unknown flag exits non-zero" {
  run sh "$PROJECT_ROOT/install.sh" --bogus
  [ "$status" -ne 0 ]
}

@test "U1: resolve_real_user honors SUDO_USER" {
  cur="$(id -un)"
  SUDO_USER="$cur" resolve_real_user
  [ "$REAL_USER" = "$cur" ]
  [ -n "$REAL_UID" ]
}
