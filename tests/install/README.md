# install.sh tests

Unit tests for the host-side installer (`install.sh`). They source the script as a
library (it guards `main()` behind `JUKEPLOX_INSTALL_LIB`) and exercise its functions
with external commands (`docker`, `ss`, `ip`, `curl`, …) stubbed on a temporary
`PATH`. The error-prone logic — the detect→`docker run` flag mapping, the
desktop-vs-headless audio branch, and the re-run-keeps-the-volume path — is covered
directly.

These are **Linux/POSIX** tests. Run them on a Linux host or in CI.

## Requirements

- [`bats-core`](https://github.com/bats-core/bats-core)
- [`shellcheck`](https://www.shellcheck.net/)
- `python3` (one detection test fabricates a unix socket; that test self-skips if absent)

## Run

```sh
shellcheck install.sh
bats tests/install/
```

## Layout

- `helpers.bash` — shared setup: source-as-library, PATH stubbing, line assertions.
- `cli.bats` — flags, usage, real-user resolution (U1).
- `detection.bats` — LAN IP, audio mode, free port (U2).
- `docker.bats` — preflight, sudo routing, install (U3).
- `flags.bats` — the flag-assembly matrix and container recreate (U4).
- `verify.bats` — health check, summary labels, browser hand-off (U5).
