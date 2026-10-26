#!/usr/bin/env bats
# Native installer (plan U2) — package sets, Python/Rust/venv build decisions, checksums.

load helpers

setup() { setup_native_lib; setup_stub_bin; }
teardown() { teardown_stub_bin; }

@test "U2: i386 package set has every runtime package and no AirPlay lib" {
  out="$(package_set i386)"
  for p in $RUNTIME_PKGS; do assert_line_eq "$out" "$p"; done
  for p in $AIRPLAY_LIBS; do refute_line_eq "$out" "$p"; done
}

@test "U2: amd64 package set adds the AirPlay libs and the x86_64 binary pair" {
  out="$(package_set amd64)"
  for p in $AIRPLAY_LIBS; do assert_line_eq "$out" "$p"; done
  pairs="$(airplay_binary_pairs amd64 /opt/jukeplox/app)"
  assert_line_eq "$pairs" "/opt/jukeplox/app/vendor/airplay/cliap2-linux-x86_64 /usr/local/bin/cliap2"
  assert_line_eq "$pairs" "/opt/jukeplox/app/vendor/airplay/cliraop-linux-x86_64 /usr/local/bin/cliraop"
  [ -z "$(airplay_binary_pairs i386 /opt/jukeplox/app)" ]
}

@test "U2: python_verdict skips an exact version match and builds otherwise" {
  [ "$(python_verdict "Python $PY_VERSION")" = "skip" ]
  [ "$(python_verdict "Python 3.12.14")" = "build" ]
  [ "$(python_verdict "")" = "build" ]
}

@test "U2: configure_flags — plain by default, PGO+LTO only with --optimize" {
  out="$(configure_flags 0)"
  refute_substr "$out" "--enable-optimizations"; refute_substr "$out" "--with-lto"
  assert_line_eq "$out" "--prefix=$PY_PREFIX"
  out2="$(configure_flags 1)"
  assert_line_eq "$out2" "--enable-optimizations"; assert_line_eq "$out2" "--with-lto"
}

@test "U2: checksum_verdict — pin match ok, anything else mismatch" {
  [ "$(checksum_verdict "$PY_SHA256  py.tgz" "$PY_SHA256")" = "ok" ]
  [ "$(checksum_verdict "deadbeef  py.tgz" "$PY_SHA256")" = "mismatch" ]
}

@test "U2: a tarball with the wrong checksum dies before extracting" {
  : > "$BATS_TEST_TMPDIR/bad.tgz"
  run verify_checksum "$BATS_TEST_TMPDIR/bad.tgz" "$PY_SHA256"
  [ "$status" -ne 0 ]
  [[ "$output" == *"checksum mismatch"* ]]
}

@test "U2: rustup-init with the wrong checksum dies before executing" {
  : > "$BATS_TEST_TMPDIR/rustup-init"
  run verify_checksum "$BATS_TEST_TMPDIR/rustup-init" "$(rustup_sha256_for i386)"
  [ "$status" -ne 0 ]
}

@test "U2: rustup pins exist for every supported arch and the version is pinned" {
  [ -n "$(rustup_sha256_for i386)" ]; [ -n "$(rustup_sha256_for amd64)" ]; [ -n "$(rustup_sha256_for arm64)" ]
  [ -z "$(rustup_sha256_for mips)" ]
  [[ "$RUSTUP_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]
  ! grep -q 'sh.rustup.rs' "$PROJECT_ROOT/install-native.sh"
}

@test "U2: cargo_verdict — old bookworm cargo installs, recent skips, absent installs" {
  [ "$(cargo_verdict "cargo 1.63.0")" = "install" ]
  [ "$(cargo_verdict "cargo 1.80.0 (abc 2024-01-01)")" = "skip" ]
  [ "$(cargo_verdict "")" = "install" ]
}

@test "U2: venv_verdict — create when absent, recreate on version change, reuse on match" {
  [ "$(venv_verdict "" 3.12.15)" = "create" ]
  [ "$(venv_verdict 3.12.14 3.12.15)" = "recreate" ]
  [ "$(venv_verdict 3.12.15 3.12.15)" = "reuse" ]
}

@test "U2: disable_distro_snapcast disables and masks both distro units" {
  log="$BATS_TEST_TMPDIR/sysctl.log"
  make_stub systemctl 'echo "systemctl $*" >> '"$log"
  disable_distro_snapcast
  grep -q "disable --now snapserver.service" "$log"
  grep -q "disable --now snapclient.service" "$log"
  grep -q "mask snapserver.service" "$log"
  grep -q "mask snapclient.service" "$log"
}

@test "U2: PyGObject pin matches the Dockerfile's" {
  grep -qF "\"$PYGOBJECT_PIN\"" "$PROJECT_ROOT/Dockerfile"
}
