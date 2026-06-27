# Shared helpers for the install.sh bats suite.
#
# These tests source install.sh as a library (the script guards its main() call
# behind JUKEPLOX_INSTALL_LIB) and exercise its functions in isolation, stubbing
# external commands (docker, ss, ip, curl, …) on a temporary PATH.

setup_install_lib() {
  PROJECT_ROOT="$(cd "$(dirname "$BATS_TEST_FILENAME")/../.." && pwd)"
  # shellcheck disable=SC1091
  JUKEPLOX_INSTALL_LIB=1 . "$PROJECT_ROOT/install.sh"
}

# Prepend an empty stub dir to PATH. Pair with teardown_stub_bin.
setup_stub_bin() {
  STUB_BIN="$(mktemp -d)"
  PATH="$STUB_BIN:$PATH"
}

teardown_stub_bin() {
  [ -n "${STUB_BIN:-}" ] && rm -rf "$STUB_BIN"
  return 0
}

# make_stub NAME 'sh body' — install a fake executable on the stub PATH.
make_stub() {
  printf '#!/usr/bin/env sh\n%s\n' "$2" > "$STUB_BIN/$1"
  chmod +x "$STUB_BIN/$1"
}

# assert_line_eq HAYSTACK LINE — fail unless LINE appears as a whole line.
assert_line_eq() {
  printf '%s\n' "$1" | grep -qxF "$2" || { echo "expected line: $2" >&2; return 1; }
}

# refute_line_eq HAYSTACK LINE — fail if LINE appears as a whole line.
refute_line_eq() {
  if printf '%s\n' "$1" | grep -qxF "$2"; then echo "unexpected line: $2" >&2; return 1; fi
  return 0
}

# refute_substr HAYSTACK SUBSTR — fail if SUBSTR appears anywhere.
refute_substr() {
  if printf '%s\n' "$1" | grep -qF "$2"; then echo "unexpected substring: $2" >&2; return 1; fi
  return 0
}
