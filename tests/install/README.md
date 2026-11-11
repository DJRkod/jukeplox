# Installer tests

Unit tests for the two host-side installers. `install.sh` (Docker install) and
`install-native.sh` (native Debian install) are sourced as libraries (both guard
`main()` behind `JUKEPLOX_INSTALL_LIB`) and their functions are exercised with
external commands (`docker`, `ss`, `ip`, `curl`, `systemctl`, `useradd`, …)
stubbed on a temporary `PATH`. The error-prone logic — the detect→`docker run`
flag mapping, the desktop-vs-headless audio branch, the re-run-keeps-the-volume
path, the native layout record and service unit — is covered directly.

These are **Linux/POSIX** tests. Run them on a Linux host, or anywhere through
the Docker image below.

## Run

Always through the runner. It shellchecks both installers, bounds every test
(`BATS_TEST_TIMEOUT`, default 60 s) and every file (GNU `timeout -k`, default
300 s), prints one `PASS`/`FAIL` line per file, and exits non-zero when any
file fails, hangs, or runs fewer tests than it declares. The per-test cap
names a test that hangs in process; it kills direct children only, so a stand-in
that backgrounds a child holding stdout is ended by the file cap instead, which
names the file. The runner refuses to run uncapped: `timeout`, `pkill`/`ps`
(procps) and bats 1.8.0+ must be present, because older bats ignores
`BATS_TEST_TIMEOUT` and bats runs zero tests under it without procps.

On a Linux host, as a normal user:

```sh
tests/install/run.sh
```

Anywhere with Docker (the image is Debian 12 with bats, shellcheck, procps):

```sh
docker build -t jp-bats:latest -f tests/install/Dockerfile tests/install
docker run --rm -v "$PWD:/code" jp-bats:latest
```

Git Bash on Windows rewrites `/code`-style paths; prefix the run with
`MSYS_NO_PATHCONV=1` and mount with `-v "$(cygpath -w "$PWD"):/code"`.
The image runs as root, so `maybe_sudo` paths call the command directly; on a
host run as a normal user the `sudo` stand-ins cover them.

To iterate on one file or case, call bats directly inside the same cap:

```sh
timeout 300 bats --filter 'install_docker' tests/install/docker.bats
```

Timeouts: `JP_BATS_TEST_TIMEOUT` and `JP_BATS_FILE_TIMEOUT` (seconds) override
the runner's defaults.

## Requirements

- [`bats-core`](https://github.com/bats-core/bats-core) 1.8.0 or newer (`BATS_TEST_TIMEOUT`)
- [`shellcheck`](https://www.shellcheck.net/)
- GNU `coreutils` `timeout` and `procps` (`pkill`/`ps`)
- `python3` (one detection test fabricates a unix socket; that test self-skips if absent)

## Layout

- `run.sh` — the bounded runner described above.
- `Dockerfile` — the test image recipe (`jp-bats`).
- `helpers.bash` — shared setup: source-as-library for both installers, `PATH`
  stubbing (`make_stub` writes stand-ins under an absolute `#!/bin/sh` so a test
  can fake any command, including a shell, without recursion), line assertions.
- `harness.bats` — regression guards for the stub harness itself (absolute shebang, faking `sh` does not recurse).
- `cli.bats` — `install.sh` flags, usage, real-user resolution (U1).
- `detection.bats` — LAN IP, audio mode, free port (U2).
- `docker.bats` — preflight, sudo routing, Docker install (U3).
- `flags.bats` — the flag-assembly matrix and container recreate (U4).
- `verify.bats` — health check, summary labels, browser hand-off (U5).
- `native-cli.bats` — `install-native.sh` flags, layout record, re-run rules.
- `native-packages.bats` — apt/pip/snapcast steps and bounded downloads.
- `native-service.bats` — service user, data dir ownership, unit, health wait.
