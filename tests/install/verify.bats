#!/usr/bin/env bats
# U5 — health check, summary labels, and browser hand-off.

load helpers

setup() { setup_install_lib; setup_stub_bin; }
teardown() { teardown_stub_bin; }

@test "U5: wait_healthy returns 0 when /health reports a status" {
  make_stub curl 'echo "{\"status\":\"ok\"}"'
  run wait_healthy http://x 3
  [ "$status" -eq 0 ]
}

@test "U5: wait_healthy fails when never healthy" {
  make_stub curl 'exit 1'
  run wait_healthy http://x 2
  [ "$status" -ne 0 ]
}

@test "U5: audio_label maps each mode" {
  [ "$(audio_label pulse)" = "desktop speakers (PulseAudio/PipeWire)" ]
  [ "$(audio_label alsa)" = "attached speakers (ALSA)" ]
  [ "$(audio_label none)" = "network speakers only (no local audio)" ]
}

@test "U5: maybe_open_browser does nothing without a display" {
  make_stub xdg-open 'echo opened'
  DISPLAY=""; WAYLAND_DISPLAY=""
  run maybe_open_browser http://x/admin
  [ "$status" -eq 0 ]
  [ -z "$output" ]
}
