"""U4 — embedded snapserver supervisor lifecycle.

Written around the orphaned-port-hold failure mode: a failed/aborted start must
never leave a snapserver holding host ports. A fake process + injected seams
drive every path with no real snapserver and no real sleeps.
"""

import asyncio

import pytest

from app.output.snapcast_server import (
    SnapserverStartError,
    SnapserverSupervisor,
    build_snapserver_args,
)


# ── fakes ─────────────────────────────────────────────────────────────────────


class _FakeStdout:
    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        await asyncio.Event().wait()  # process alive, no more output → hang


class _FakeProc:
    def __init__(self, stdout_lines, rc_on_term=0):
        self.stdout = _FakeStdout(stdout_lines)
        self.stderr = _FakeStdout([b""])  # immediate EOF → drain returns
        self.returncode = None
        self._exit = asyncio.Event()
        self._rc_on_term = rc_on_term
        self.terminated = False
        self.killed = False

    async def wait(self):
        if self.returncode is not None:
            return self.returncode
        await self._exit.wait()
        return self.returncode

    def signal_exit(self, rc):
        if self.returncode is None:
            self.returncode = rc
            self._exit.set()

    def terminate(self):
        self.terminated = True
        self.signal_exit(self._rc_on_term)

    def kill(self):
        self.killed = True
        self.signal_exit(-9)


_READY = [b"(Snapserver) Version 0.27.0\n"]


async def _instant_sleep(_s):
    return None


def _make(procs, *, port_check=None, **kw):
    """Supervisor whose spawn hands out ``procs`` in order."""
    it = iter(procs)
    spawned = []

    async def _spawn(_args):
        p = next(it)
        spawned.append(p)
        return p

    sup = SnapserverSupervisor(
        source_name="jukeplox",
        spawn=_spawn,
        port_check=port_check or (lambda h, p: True),
        sleep=_instant_sleep,
        readiness_timeout_s=0.5,
        **kw,
    )
    sup._spawned = spawned  # test handle
    return sup


async def _wait_until(pred, timeout=1.0):
    async def _poll():
        while not pred():
            await asyncio.sleep(0)
    await asyncio.wait_for(_poll(), timeout)


# ── args builder (pure) ───────────────────────────────────────────────────────


def test_build_args_binds_loopback_control_and_lan_stream():
    args = build_snapserver_args(config_path="/app/config/snapserver.conf",
                                 source_name="jukeplox")
    joined = " ".join(args)
    assert "--config=/app/config/snapserver.conf" in args
    # control + http bound loopback; stream port present (LAN-facing).
    assert "127.0.0.1" in joined
    assert "--tcp.port" in args and "1705" in args
    assert "--http.port" in args and "1780" in args
    assert "--stream.port" in args and "1704" in args
    # tcp:// source with the shared feed format + idle threshold + name.
    assert any(a.startswith("tcp://") and "sampleformat=48000:16:2" in a
               and "idle_threshold=60000" in a and "name=jukeplox" in a
               for a in args)


# ── happy path ────────────────────────────────────────────────────────────────


async def test_start_becomes_ready():
    sup = _make([_FakeProc(_READY)])
    await sup.start()
    assert sup.is_running
    assert sup.source_feed_url == "tcp://127.0.0.1:4953"
    assert sup.control_host == "127.0.0.1"
    await sup.stop()
    assert not sup.is_running


async def test_start_is_idempotent():
    sup = _make([_FakeProc(_READY)])
    await sup.start()
    await sup.start()  # already running → no second spawn
    assert len(sup._spawned) == 1
    await sup.stop()


# ── readiness failure → no orphan ─────────────────────────────────────────────


async def test_readiness_timeout_reaps_process_and_raises():
    proc = _FakeProc([b"loading config...\n"])  # marker never arrives → hangs
    sup = _make([proc])
    with pytest.raises(SnapserverStartError, match="ready"):
        await sup.start()
    # The orphaned-port-hold guard: the process was terminated + reaped.
    assert proc.terminated
    assert not sup.is_running


async def test_early_exit_before_ready_raises():
    proc = _FakeProc([b"binding failed\n", b""])  # EOF before marker
    proc.signal_exit(1)
    sup = _make([proc])
    with pytest.raises(SnapserverStartError):
        await sup.start()
    assert not sup.is_running


# ── port conflict pre-check ───────────────────────────────────────────────────


async def test_port_conflict_is_clear_error_no_spawn():
    def _busy_control(host, port):
        return port != 1705  # control port already held

    sup = _make([_FakeProc(_READY)], port_check=_busy_control)
    with pytest.raises(SnapserverStartError, match="in use"):
        await sup.start()
    assert sup._spawned == []  # never spawned snapserver at all


# ── restart semantics ─────────────────────────────────────────────────────────


async def test_unexpected_exit_restarts():
    p1, p2 = _FakeProc(_READY, rc_on_term=0), _FakeProc(_READY)
    sup = _make([p1, p2])
    await sup.start()
    assert sup.is_running
    p1.signal_exit(1)  # crash
    await _wait_until(lambda: len(sup._spawned) == 2)
    await _wait_until(lambda: sup.is_running)  # restarted on p2
    await sup.stop()


async def test_intentional_stop_does_not_restart():
    p1 = _FakeProc(_READY)
    sup = _make([p1, _FakeProc(_READY)])
    await sup.start()
    await sup.stop()
    # give the runner a chance to (wrongly) restart
    for _ in range(10):
        await asyncio.sleep(0)
    assert len(sup._spawned) == 1  # no restart after intentional stop
    assert p1.terminated


async def test_restart_retries_then_gives_up_after_cap():
    """A failed restart RETRIES (not give-up-after-one), but a permanently
    broken snapserver stops respawning after the consecutive-failure cap —
    no infinite orphan spawning."""
    from app.output.snapcast_server import _MAX_CONSECUTIVE_RESTARTS
    p1 = _FakeProc(_READY)
    # Each restart proc never emits the ready marker → readiness times out.
    failing = [_FakeProc([b"loading...\n"]) for _ in range(_MAX_CONSECUTIVE_RESTARTS)]
    sup = _make([p1] + failing)
    await sup.start()
    assert sup.is_running
    p1.signal_exit(1)  # crash → every restart attempt fails
    await _wait_until(
        lambda: not sup.is_running and len(sup._spawned) >= 1 + _MAX_CONSECUTIVE_RESTARTS,
        timeout=8.0)
    assert not sup.is_running  # gave up, not looping forever
    await sup.stop()


async def test_restart_waits_for_port_release():
    """Crash/OOM restart confirms the control port is released before respawn,
    so the new snapserver can't hit EADDRINUSE."""
    calls = {"n": 0}

    def _port_check(host, port):
        # feed/stream/control preflight all pass; during restart the control
        # port reads held for the first two checks, then frees.
        if port == 1705:
            calls["n"] += 1
            # first call = preflight (free). Later restart checks: held twice.
            if 2 <= calls["n"] <= 3:
                return False
        return True

    p1, p2 = _FakeProc(_READY), _FakeProc(_READY)
    sup = _make([p1, p2], port_check=_port_check)
    await sup.start()
    p1.signal_exit(137)  # OOM-kill
    await _wait_until(lambda: len(sup._spawned) == 2, timeout=2.0)
    # respawn only happened after the held-port checks returned free.
    assert calls["n"] >= 3
    await sup.stop()
