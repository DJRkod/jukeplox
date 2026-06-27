#!/usr/bin/env bats
# U2 — host detection (LAN IP, audio mode, free port).

load helpers

setup() { setup_install_lib; setup_stub_bin; }
teardown() { teardown_stub_bin; }

@test "U2: detect_lan_ip parses the src address from ip route" {
  make_stub ip 'echo "1.1.1.1 via 192.168.1.1 dev eth0 src 192.168.1.50 uid 1000"'
  run detect_lan_ip
  [ "$status" -eq 0 ]
  [ "$output" = "192.168.1.50" ]
}

@test "U2: detect_lan_ip falls back to hostname -I" {
  make_stub ip 'exit 1'
  make_stub hostname 'echo "192.168.5.5 10.0.0.1"'
  run detect_lan_ip
  [ "$output" = "192.168.5.5" ]
}

@test "U2 (AE2): detect_audio returns pulse when the user socket exists" {
  command -v python3 >/dev/null 2>&1 || skip "python3 needed to fabricate a unix socket"
  tmp="$(mktemp -d)"; mkdir -p "$tmp/1000/pulse"
  python3 - "$tmp/1000/pulse/native" <<'PY'
import socket, sys
socket.socket(socket.AF_UNIX).bind(sys.argv[1])
PY
  JUKEPLOX_RUNTIME_BASE="$tmp" REAL_UID=1000 run detect_audio
  [ "$output" = "pulse" ]
  rm -rf "$tmp"
}

@test "U2 (AE3): detect_audio returns alsa when no socket but a sound card exists" {
  tmp="$(mktemp -d)"; snd="$(mktemp -d)"
  JUKEPLOX_RUNTIME_BASE="$tmp" REAL_UID=1000 JUKEPLOX_DEV_SND="$snd" run detect_audio
  [ "$output" = "alsa" ]
  rm -rf "$tmp" "$snd"
}

@test "U2: detect_audio returns none when neither is present" {
  tmp="$(mktemp -d)"
  JUKEPLOX_RUNTIME_BASE="$tmp" REAL_UID=1000 JUKEPLOX_DEV_SND="$tmp/absent" run detect_audio
  [ "$output" = "none" ]
  rm -rf "$tmp"
}

@test "U2: port_free is true when ss shows nothing, false when listening" {
  make_stub ss 'echo ""'
  run port_free 80
  [ "$status" -eq 0 ]
  make_stub ss 'echo "LISTEN 0 128 0.0.0.0:80 0.0.0.0:*"'
  run port_free 80
  [ "$status" -ne 0 ]
}
