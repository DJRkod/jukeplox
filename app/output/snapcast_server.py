"""Embedded ``snapserver`` process supervisor (2026-08-11 plan U4).

The Snapcast backend (U5) can drive an EMBEDDED snapserver (the default) or an
EXTERNAL one (host/port). This module owns the embedded daemon's lifecycle only:
spawn it, gate on readiness, restart it if it dies unexpectedly, and — critically
— never leave it orphaned holding a host port after a failed start (under
``--network host`` an orphaned snapserver would hold 1704/1705/1780 and wedge
every subsequent start).

Load-bearing behaviors (the orphaned-port hold is the key failure mode, so this
unit is written test-first around it):

- **Readiness gate.** ``start()`` spawns snapserver and waits for its ``Version``
  banner on stdout plus a short grace, wrapped in ``wait_for(readiness_timeout)``.
  A timeout is a clean failure: the process is terminated + reaped and the runner
  cancelled BEFORE the error propagates, so no orphan survives.
- **Cancel-on-abort.** Any exception/cancel during start tears the process down
  in a finally — the port is always released on a failed start.
- **Restart on unexpected death.** Once ready, a background runner awaits the
  process; an exit that was NOT requested (crash / OOM-kill) schedules a restart
  after ``restart_delay_s``, but only after confirming the old pid is reaped and
  the ports are free (else the restart hits ``EADDRINUSE``). An intentional
  ``stop()`` sets a flag so the runner does NOT restart.
- **Loopback binding.** Control (1705) and HTTP/Snapweb (1780) bind ``127.0.0.1``;
  the snapclient stream port (1704) and the ffmpeg feed source are the only
  LAN/loopback-facing surfaces. (1780 has no auth — if the pinned snapserver
  rejects an http bind-host flag, U11 documents it as an open LAN surface.)
- **Config file.** snapserver silently ignores ``--stream.*`` args with no config
  file, so it is always launched with ``--config=<stub>`` (U3's stub conf).

Every blocking stdlib call on the start path (the port pre-check) runs in an
executor; seams (``spawn``, ``port_check``, ``sleep``) are injectable so tests
drive the whole lifecycle with a fake process and no real snapserver.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from typing import Any, Awaitable, Callable

_log = logging.getLogger(__name__)

# Debian's apt package installs snapserver to /usr/bin (NOT /usr/local/bin — that
# is where the vendored airplay binaries live). Override via JUKEPLOX_SNAPSERVER_BIN.
_SNAPSERVER_BIN_DEFAULT = "/usr/bin/snapserver"
_STUB_CONF_DEFAULT = "/app/config/snapserver.conf"

# Consecutive-restart cap: a permanently-broken snapserver (bad binary, repeated
# OOM, fatal config) must not respawn forever. After this many consecutive failed
# restarts the supervisor gives up (logs ERROR, stays down) rather than spawning
# orphans every restart_delay_s indefinitely.
_MAX_CONSECUTIVE_RESTARTS = 5

# Ports (embedded defaults). The feed source + control + http are loopback; only
# the snapclient stream port is LAN-facing.
DEFAULT_STREAM_PORT = 1704       # snapclient audio (LAN-facing, intentional)
DEFAULT_CONTROL_PORT = 1705      # JSON-RPC control (loopback)
DEFAULT_HTTP_PORT = 1780         # HTTP/Snapweb (loopback if supported; no auth)
DEFAULT_SOURCE_PORT = 4953       # ffmpeg → snapserver tcp feed (loopback)

DEFAULT_SAMPLEFORMAT = "48000:16:2"
DEFAULT_CODEC = "flac"
DEFAULT_IDLE_THRESHOLD_MS = 60000  # gapless-across-gaps: hold the stream open
DEFAULT_BUFFER_MS = 1000
DEFAULT_CHUNK_MS = 20

# The stdout banner snapserver prints once it is up. Kept configurable because
# the exact wording is a settle-at-runtime detail (plan Deferred to Impl).
DEFAULT_READY_MARKER = "(Snapserver) Version 0."
DEFAULT_READY_GRACE_S = 2.0
DEFAULT_READINESS_TIMEOUT_S = 30.0
DEFAULT_RESTART_DELAY_S = 5.0
_STOP_GRACE_S = 2.0


def snapserver_bin() -> str:
    """The snapserver binary path, overridable via ``JUKEPLOX_SNAPSERVER_BIN``
    for dev/test (mirrors airplay.py's ``JUKEPLOX_CLIAP2_BIN`` convention)."""
    return os.environ.get("JUKEPLOX_SNAPSERVER_BIN", _SNAPSERVER_BIN_DEFAULT)


class SnapserverStartError(RuntimeError):
    """Embedded snapserver failed to start (readiness timeout / port conflict /
    spawn error). The process is always reaped before this is raised."""


def build_snapserver_args(
    *,
    config_path: str,
    source_name: str,
    source_host: str = "127.0.0.1",
    source_port: int = DEFAULT_SOURCE_PORT,
    stream_port: int = DEFAULT_STREAM_PORT,
    control_port: int = DEFAULT_CONTROL_PORT,
    http_port: int = DEFAULT_HTTP_PORT,
    control_host: str = "127.0.0.1",
    http_host: str = "127.0.0.1",
    sampleformat: str = DEFAULT_SAMPLEFORMAT,
    codec: str = DEFAULT_CODEC,
    idle_threshold_ms: int = DEFAULT_IDLE_THRESHOLD_MS,
    buffer_ms: int = DEFAULT_BUFFER_MS,
    chunk_ms: int = DEFAULT_CHUNK_MS,
    binary: str | None = None,
) -> list[str]:
    """Build the snapserver CLI invocation. Pure function.

    All runtime config rides the CLI (the stub ``--config`` only anchors it). The
    tcp source LISTENS on ``source_host:source_port`` in server mode so the local
    ffmpeg feed connects and writes PCM; the exact flag spellings track snapcast
    0.27 and are verified at settle time (plan Deferred to Implementation)."""
    src = (
        f"tcp://{source_host}:{source_port}"
        f"?name={source_name}&mode=server&sampleformat={sampleformat}"
        f"&codec={codec}&idle_threshold={idle_threshold_ms}"
    )
    return [
        binary or snapserver_bin(),
        f"--config={config_path}",
        "--stream.source", src,
        "--stream.buffer", str(buffer_ms),
        "--stream.chunk_ms", str(chunk_ms),
        "--stream.codec", codec,
        "--stream.sampleformat", sampleformat,
        # snapclient audio port — LAN-facing by design.
        "--stream.port", str(stream_port),
        # JSON-RPC control — loopback only (no auth surface on the LAN).
        "--tcp.enabled", "true",
        "--tcp.port", str(control_port),
        "--tcp.bind_to_address", control_host,
        # HTTP/Snapweb — loopback if the pinned snapserver honors the flag
        # (1780 has no auth; U11 documents the exposure if it does not).
        "--http.enabled", "true",
        "--http.port", str(http_port),
        "--http.bind_to_address", http_host,
    ]


async def _default_spawn(args: list[str]) -> Any:
    return await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


def _port_free(host: str, port: int) -> bool:
    """True if ``host:port`` can be bound right now (a blocking stdlib call — run
    it in an executor off the loop). Used as the pre-launch conflict check and to
    confirm ports are released before a restart respawn."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


class SnapserverSupervisor:
    """Owns one embedded snapserver process. See the module docstring."""

    def __init__(
        self,
        *,
        source_name: str,
        config_path: str = _STUB_CONF_DEFAULT,
        stream_port: int = DEFAULT_STREAM_PORT,
        control_port: int = DEFAULT_CONTROL_PORT,
        http_port: int = DEFAULT_HTTP_PORT,
        source_host: str = "127.0.0.1",
        source_port: int = DEFAULT_SOURCE_PORT,
        control_host: str = "127.0.0.1",
        http_host: str = "127.0.0.1",
        readiness_timeout_s: float = DEFAULT_READINESS_TIMEOUT_S,
        ready_marker: str = DEFAULT_READY_MARKER,
        ready_grace_s: float = DEFAULT_READY_GRACE_S,
        restart_delay_s: float = DEFAULT_RESTART_DELAY_S,
        binary: str | None = None,
        # ── injectable seams (tests) ──────────────────────────────────────
        spawn: Callable[[list[str]], Awaitable[Any]] | None = None,
        port_check: Callable[[str, int], bool] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._source_name = source_name
        self._config_path = config_path
        self._stream_port = stream_port
        self._control_port = control_port
        self._http_port = http_port
        self._source_host = source_host
        self._source_port = source_port
        self._control_host = control_host
        self._http_host = http_host
        self._readiness_timeout_s = readiness_timeout_s
        self._ready_marker = ready_marker
        self._ready_grace_s = ready_grace_s
        self._restart_delay_s = restart_delay_s
        self._binary = binary
        self._spawn = spawn or _default_spawn
        self._port_check = port_check or _port_free
        self._sleep = sleep

        self._proc: Any = None
        self._stderr_task: asyncio.Task | None = None
        self._runner_task: asyncio.Task | None = None
        self._stop_called = False
        self._running = False

    # ── observability ───────────────────────────────────────────────────────

    @property
    def control_host(self) -> str:
        return self._control_host

    @property
    def control_port(self) -> int:
        return self._control_port

    @property
    def source_feed_url(self) -> str:
        """The ``tcp://`` URL the U5 feed ffmpeg writes PCM to (the address
        snapserver's tcp source listens on)."""
        return f"tcp://{self._source_host}:{self._source_port}"

    @property
    def is_running(self) -> bool:
        return self._running and self._proc is not None and self._proc.returncode is None

    def _args(self) -> list[str]:
        return build_snapserver_args(
            config_path=self._config_path,
            source_name=self._source_name,
            source_host=self._source_host,
            source_port=self._source_port,
            stream_port=self._stream_port,
            control_port=self._control_port,
            http_port=self._http_port,
            control_host=self._control_host,
            http_host=self._http_host,
            binary=self._binary,
        )

    # ── lifecycle ───────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Spawn snapserver, gate on readiness, and arm the restart runner.
        Raises ``SnapserverStartError`` on any failure — always leaving NO
        orphaned process/port behind. Idempotent while already running."""
        if self.is_running:
            return
        self._stop_called = False
        await self._preflight_ports()
        try:
            await asyncio.wait_for(self._spawn_and_wait_ready(),
                                   self._readiness_timeout_s)
        except asyncio.TimeoutError as exc:
            await self._teardown_process()
            raise SnapserverStartError(
                f"snapserver did not become ready within "
                f"{self._readiness_timeout_s:.0f}s") from exc
        except BaseException:
            # Spawn error, cancel, or any readiness failure — never leave an
            # orphan holding the host ports (the key failure mode).
            await self._teardown_process()
            raise
        self._running = True
        self._runner_task = asyncio.get_running_loop().create_task(self._run())

    async def _preflight_ports(self) -> None:
        """Fail fast with a CLEAR error if a required port is already held,
        rather than letting snapserver die with an opaque bind error. Runs the
        blocking bind-check off the loop."""
        loop = asyncio.get_running_loop()
        checks = [
            (self._control_host, self._control_port, "control"),
            (self._source_host, self._source_port, "feed source"),
            # stream_port/http bind to their own hosts; check stream on all-ifaces
            ("0.0.0.0", self._stream_port, "stream"),
        ]
        for host, port, label in checks:
            free = await loop.run_in_executor(None, self._port_check, host, port)
            if not free:
                raise SnapserverStartError(
                    f"snapserver {label} port {host}:{port} is already in use — "
                    f"another snapserver or process holds it")

    async def _spawn_and_wait_ready(self) -> None:
        self._proc = await self._spawn(self._args())
        if self._proc.stderr is not None:
            self._stderr_task = asyncio.create_task(
                self._drain(self._proc.stderr, "stderr"))
        await self._await_ready_marker()
        # Small grace after the banner so control/stream sockets are listening.
        await self._sleep(self._ready_grace_s)

    async def _await_ready_marker(self) -> None:
        """Read stdout until the readiness banner appears. An early process exit
        (stdout EOF) before the marker is a start failure, surfaced by the
        wait_for wrapper as the process is reaped in the finally."""
        stream = self._proc.stdout
        assert stream is not None
        while True:
            line_bytes = await stream.readline()
            if not line_bytes:
                # EOF: snapserver exited before signalling ready.
                rc = await self._proc.wait()
                raise SnapserverStartError(
                    f"snapserver exited ({rc}) before becoming ready")
            line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
            _log.debug("snapserver: %s", line)
            if self._ready_marker in line:
                _log.info("snapserver ready: %s", line)
                return

    async def _run(self) -> None:
        """Await the process; restart on an UNREQUESTED exit (crash/OOM). An
        intentional stop() sets _stop_called so this does not loop.

        A failed restart RETRIES (a transient bind race shouldn't be a permanent
        give-up), but only up to ``_MAX_CONSECUTIVE_RESTARTS`` — a permanently
        broken snapserver must not respawn orphans forever. A successful restart
        resets the budget."""
        consecutive_failures = 0
        while not self._stop_called:
            proc = self._proc
            if proc is None:
                return
            rc = await proc.wait()
            if self._stop_called:
                return
            self._running = False
            await self._reap_stderr()
            _log.warning("snapserver exited unexpectedly (rc=%s) — restarting in "
                         "%.0fs", rc, self._restart_delay_s)
            restarted = False
            while not self._stop_called and consecutive_failures < _MAX_CONSECUTIVE_RESTARTS:
                await self._sleep(self._restart_delay_s)
                if self._stop_called:
                    return
                # Confirm the ports actually released before respawn, else the
                # new snapserver hits EADDRINUSE (crash/OOM can leave a socket in
                # TIME_WAIT or a not-yet-reaped child holding it).
                try:
                    await self._await_ports_released()
                    await asyncio.wait_for(self._spawn_and_wait_ready(),
                                           self._readiness_timeout_s)
                    self._running = True
                    consecutive_failures = 0
                    restarted = True
                    _log.info("snapserver restarted")
                    break
                except BaseException:
                    await self._teardown_process()
                    self._proc = None
                    consecutive_failures += 1
                    _log.error("snapserver restart failed (%d/%d)",
                               consecutive_failures, _MAX_CONSECUTIVE_RESTARTS,
                               exc_info=True)
            if not restarted:
                _log.error("snapserver gave up after %d consecutive restart "
                           "failures — staying down", consecutive_failures)
                self._running = False
                return

    async def _await_ports_released(self, *, attempts: int = 20,
                                    interval_s: float = 0.25) -> None:
        loop = asyncio.get_running_loop()
        for _ in range(attempts):
            free = await loop.run_in_executor(
                None, self._port_check, self._control_host, self._control_port)
            if free:
                return
            await self._sleep(interval_s)
        _log.warning("snapserver: control port %s still held before restart — "
                     "proceeding (spawn may fail and retry)", self._control_port)

    async def stop(self) -> None:
        """Intentional shutdown: no restart. Terminates + reaps the process and
        cancels the runner. Idempotent."""
        self._stop_called = True
        self._running = False
        if self._runner_task is not None and not self._runner_task.done():
            self._runner_task.cancel()
            try:
                await self._runner_task
            except asyncio.CancelledError:
                pass
        self._runner_task = None
        await self._teardown_process()

    async def _teardown_process(self) -> None:
        """SIGTERM → grace → SIGKILL, then reap. Tolerates a None/exited proc."""
        proc = self._proc
        self._proc = None
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
            except (ProcessLookupError, OSError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=_STOP_GRACE_S)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                except (ProcessLookupError, OSError):
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=_STOP_GRACE_S)
                except asyncio.TimeoutError:
                    _log.error("snapserver ignored SIGKILL")
        await self._reap_stderr()

    async def _reap_stderr(self) -> None:
        if self._stderr_task is not None and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
        self._stderr_task = None

    async def _drain(self, stream: asyncio.StreamReader, label: str) -> None:
        try:
            while True:
                line = await stream.readline()
                if not line:
                    return
                _log.warning("snapserver %s: %s", label,
                             line.decode("utf-8", errors="replace").rstrip("\r\n"))
        except asyncio.CancelledError:
            return
        except Exception:
            return
