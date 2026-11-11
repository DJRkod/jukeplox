#!/usr/bin/env bats
# The stub harness itself: regression guards for tests/install/helpers.bash.

load helpers

setup() { setup_stub_bin; }
teardown() { teardown_stub_bin; }

@test "make_stub writes an absolute /bin/sh shebang" {
  make_stub probe 'exit 0'
  [ "$(head -n1 "$STUB_BIN/probe")" = "#!/bin/sh" ]
}

@test "faking sh runs the stand-in once and never recurses" {
  # Before 2026-11 every stand-in started with `#!/usr/bin/env sh`; with STUB_BIN
  # first on PATH, a test that faked `sh` made every stand-in re-exec itself
  # forever and the file never finished. This case hangs if that comes back;
  # the runner's caps then name this file.
  log="$BATS_TEST_TMPDIR/sh.log"
  make_stub sh 'echo "sh $*" >> '"'$log'"'; exit 0'
  make_stub wrap 'exec "$@"'
  run wrap sh -c true
  [ "$status" -eq 0 ]
  [ "$(wc -l < "$log")" -eq 1 ]
}
