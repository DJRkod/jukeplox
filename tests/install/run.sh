#!/bin/sh
# tests/install/run.sh -- run the installer bats suite, bounded.
#
# Every run is capped twice. BATS_TEST_TIMEOUT bounds a test that hangs in
# process and names it; it kills direct children only, so a stand-in that
# backgrounds a child holding stdout outlives it. GNU timeout (-k) bounds the
# whole file for that shape and names the file. Either way a hang exits non-zero
# instead of sitting silently (the pytest side has had the same rule since
# 2026-06). The exit gate is the integer failure counter, never a summary token,
# so the text below can never drift away from the gate (lane-inside.sh, 2026-11).
# A file is also FAIL when bats ran fewer tests than it declares: a zero-test
# run exits 0 and would otherwise pass.
#
# Usage: tests/install/run.sh            (from the repo root or anywhere)
#   JP_BATS_TEST_TIMEOUT=60  seconds per test   (default 60)
#   JP_BATS_FILE_TIMEOUT=300 seconds per file   (default 300)
set -u

die() { printf 'run.sh: %s\n' "$*" >&2; exit 2; }

ROOT=$(cd "$(dirname "$0")/../.." && pwd) || die "cannot resolve the repo root"
cd "$ROOT" || die "cannot enter $ROOT"

# Fail closed: no cap means no run.
for tool in bats timeout shellcheck; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is required (see tests/install/README.md)"
done
# Bats implements BATS_TEST_TIMEOUT with pkill/ps and runs ZERO tests without them.
command -v pkill >/dev/null 2>&1 || command -v ps >/dev/null 2>&1 \
  || die "bats' per-test timeout needs pkill or ps (apt-get install procps)"
# BATS_TEST_TIMEOUT exists since bats-core 1.8.0; older bats ignores it silently.
bats_ver=$(bats --version 2>/dev/null | sed 's/^Bats //')
case "$bats_ver" in
  0.*|1.[0-7]|1.[0-7].*) die "bats >= 1.8.0 required for BATS_TEST_TIMEOUT (have ${bats_ver:-unknown})" ;;
esac

PER_TEST=${JP_BATS_TEST_TIMEOUT:-60}
PER_FILE=${JP_BATS_FILE_TIMEOUT:-300}

echo "== shellcheck install.sh install-native.sh"
shellcheck -s sh -S warning install.sh install-native.sh || die "shellcheck failed"

export BATS_TEST_TIMEOUT="$PER_TEST"
files=0; fail=0
for f in tests/install/*.bats; do
  files=$((files + 1))
  echo "== $f"
  declared=$(bats --count "$f" 2>/dev/null || echo 0)
  out="$(mktemp)"
  # No pipe here: `| tee` would make $? the pipe's last command, not bats/timeout
  # (the exact slip the pytest-timeout learning records). Capture, then print.
  timeout -k 10 "$PER_FILE" bats "$f" > "$out" 2>&1
  rc=$?
  cat "$out"
  ran=$(grep -cE '^(ok|not ok) ' "$out")
  rm -f "$out"
  if [ "$rc" -eq 0 ] && [ "$ran" -gt 0 ] && [ "$ran" -eq "$declared" ]; then
    printf 'PASS %s (%s tests)\n' "$f" "$ran"
  elif [ "$rc" -eq 0 ]; then
    fail=$((fail + 1)); printf 'FAIL %s (ran %s of %s declared tests)\n' "$f" "$ran" "$declared"
  elif [ "$rc" -eq 124 ]; then
    fail=$((fail + 1)); printf 'FAIL %s (hung: exceeded %ss)\n' "$f" "$PER_FILE"
  else
    fail=$((fail + 1)); printf 'FAIL %s (rc=%s)\n' "$f" "$rc"
  fi
done

echo "== $files files, $fail FAIL"
if [ "$fail" -ne 0 ]; then exit 1; fi
exit 0
