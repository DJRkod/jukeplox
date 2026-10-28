# Native-install validation lanes

Validation for `install-native.sh` (the non-Docker Debian 12 install, validated
on 32-bit x86) without a 32-bit machine.

## Container lane (fast loop)

```sh
tools/native/validate-i386-container.sh
```

Starts a `linux/386` `debian:bookworm-slim` container — a true 32-bit userland
running natively on the amd64 kernel, under `linux32` so `uname -m` says `i686` —
and inside it:

1. runs the installer with `--source --no-service --yes` (AE1 at the venv level);
2. asserts the compiled Python 3.12 imports `gi`/`Gst`, `cryptography`, `bcrypt`,
   `orjson`, `numpy`, `av`, `PIL` and `app.main`;
3. proves bytecode is reused across starts (the same PYTHONPYCACHEPREFIX mechanism the unit uses, exercised here as root);
4. starts uvicorn from a directory that is not the checkout and fetches `/`, a
   static asset and `/api/version` (AE4);
5. synthesizes two sine tracks with ffmpeg, registers them as a Local source, selects
   the Direct backend and plays them to ALSA's software `null` PCM. The `null` PCM has
   no clock, so the proof is lifecycle, not pacing: both tracks reach the play history
   AND the app log shows two confirmed starts with no decode or skip errors (AE5);
6. runs the test suite per file under the compiled interpreter with the project's
   hard timeout (`JP_LANE_SKIP_SUITE=1` skips this).

`/opt/jukeplox` (interpreter, Rust toolchain, venv) lives in the Docker volume
`jukeplox-i386-opt`, so the first run pays the compiles and later runs skip them.
The container is removed on exit.

**What it cannot prove:** anything kernel-level, systemd, reboot survival, avahi
coexistence, or real sound hardware — the host kernel has no loopback or dummy
sound module. Those belong to the VM lane (a Hyper-V Debian i386 guest), whose runbook lives outside this repo's public tree.

Files: `validate-i386-container.sh` (host side), `lane-inside.sh` (runs in the
container), `lane-play.py` (the AE5 playback drive, uses only the public admin API).
