"""U5 — Snapcast backend: enable/connect, feed, flow-mode advance/outage,
external SSRF fail-closed, control-RPC device enumeration, master fan-out.

All I/O is faked: a fake control server, a fake snapserver supervisor, and a fake
feed process — no `snapcast` lib, no real snapserver, no ffmpeg.
"""

import asyncio

import pytest
from unittest.mock import AsyncMock

from app.output import snapcast as snap_mod
from app.output.base import DeviceNotReadyError
from app.output.snapcast import SnapcastBackend


# ── fakes ─────────────────────────────────────────────────────────────────────


class FakeClient:
    def __init__(self, cid, name, volume=50, muted=False):
        self.identifier = cid
        self.friendly_name = name
        self.volume = volume
        self.muted = muted


class FakeGroup:
    def __init__(self, gid, clients, muted=False):
        self.identifier = gid
        self.clients = clients
        self.muted = muted


class FakeControl:
    def __init__(self, groups):
        self._groups = groups
        self.calls = []
        self.disconnected = False

    async def status(self):
        return {"groups": [g.identifier for g in self._groups]}

    def groups(self):
        return list(self._groups)

    def clients(self):
        out = []
        for g in self._groups:
            out.extend(g.clients)
        return out

    def client(self, cid):
        for c in self.clients():
            if c.identifier == cid:
                return c
        raise KeyError(cid)

    def group(self, gid):
        for g in self._groups:
            if g.identifier == gid:
                return g
        raise KeyError(gid)

    async def client_set_volume(self, cid, percent):
        self.calls.append(("cvol", cid, percent))
        self.client(cid).volume = percent

    async def client_set_muted(self, cid, muted):
        self.calls.append(("cmute", cid, muted))

    async def group_set_muted(self, gid, muted):
        self.calls.append(("gmute", gid, muted))

    async def group_set_clients(self, gid, cids):
        self.calls.append(("gclients", gid, list(cids)))

    def set_on_update(self, cb):
        self._cb = cb

    async def disconnect(self):
        self.disconnected = True


class FakeSupervisor:
    def __init__(self):
        self.started = False
        self.stopped = False
        self.control_host = "127.0.0.1"
        self.control_port = 1705

    @property
    def source_feed_url(self):
        return "tcp://127.0.0.1:4953"

    @property
    def is_running(self):
        return self.started and not self.stopped

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


class FakeFeed:
    def __init__(self):
        self.started = False
        self.closed = False
        self._exit = asyncio.Event()
        self._rc = 0

    async def start(self):
        self.started = True

    async def wait(self):
        await self._exit.wait()
        return self._rc

    def finish(self, rc):
        self._rc = rc
        self._exit.set()

    async def close(self):
        self.closed = True
        self._exit.set()


def _default_groups():
    return [FakeGroup("g1", [FakeClient("c1", "Kitchen", 40),
                             FakeClient("c2", "Living", 80)])]


def make_backend(monkeypatch, *, settings=None, control=None, feeds=None):
    settings = settings or {}

    async def _get_setting(key, default=None):
        return settings.get(key, default)

    monkeypatch.setattr("app.database.get_setting", _get_setting)
    monkeypatch.setattr(snap_mod, "_default_resolve_source",
                        AsyncMock(return_value=("/music/x.flac", {})))

    ctrl = control if control is not None else FakeControl(_default_groups())
    sup = FakeSupervisor()
    feed_iter = iter(feeds or [FakeFeed() for _ in range(10)])

    async def _control_factory(host, port):
        ctrl.host, ctrl.port = host, port
        return ctrl

    advance = AsyncMock()
    b = SnapcastBackend(
        advance_cb=advance,
        control_factory=_control_factory,
        supervisor_factory=lambda: sup,
        feed_factory=lambda s, h, *, sink: next(feed_iter),
    )
    b._t_ctrl, b._t_sup, b._t_advance = ctrl, sup, advance
    return b


async def _wait_until(pred, timeout=1.0):
    async def _poll():
        while not pred():
            await asyncio.sleep(0)
    await asyncio.wait_for(_poll(), timeout)


# ── enable / disable ──────────────────────────────────────────────────────────


async def test_enable_embedded_starts_supervisor_and_connects(monkeypatch):
    b = make_backend(monkeypatch)
    await b.enable()
    assert b._t_sup.started
    assert b._connected
    assert b._t_ctrl.host == "127.0.0.1" and b._t_ctrl.port == 1705
    await b.disable()
    assert b._t_sup.stopped and b._t_ctrl.disconnected


async def test_enable_failure_tears_down(monkeypatch):
    b = make_backend(monkeypatch)

    async def _boom(host, port):
        raise RuntimeError("connect failed")

    b._control_factory = _boom
    with pytest.raises(RuntimeError, match="connect failed"):
        await b.enable()
    assert not b._connected
    assert b._t_sup.stopped  # supervisor torn down on failed enable


# ── external SSRF fail-closed ─────────────────────────────────────────────────


async def test_external_loopback_rejected_fail_closed(monkeypatch):
    b = make_backend(monkeypatch, settings={
        "snapcast_mode": "external",
        "snapcast_external_host": "127.0.0.1",
        "snapcast_external_port": "1705",
    })
    with pytest.raises(DeviceNotReadyError, match="loopback|link-local"):
        await b.enable()
    assert not b._connected


async def test_external_unresolvable_rejected_fail_closed(monkeypatch):
    b = make_backend(monkeypatch, settings={
        "snapcast_mode": "external",
        "snapcast_external_host": "no.such.host.invalid.example",
    })
    with pytest.raises(DeviceNotReadyError):
        await b.enable()
    assert not b._connected


async def test_external_private_ip_rejected_when_flag_off(monkeypatch):
    from app.config import settings as _settings
    monkeypatch.setattr(_settings, "allow_private_sources", False)
    b = make_backend(monkeypatch, settings={
        "snapcast_mode": "external",
        "snapcast_external_host": "192.168.1.50",  # RFC-1918 literal
    })
    with pytest.raises(DeviceNotReadyError, match="private"):
        await b.enable()
    assert not b._connected


async def test_external_private_ip_allowed_when_flag_on(monkeypatch):
    from app.config import settings as _settings
    monkeypatch.setattr(_settings, "allow_private_sources", True)
    b = make_backend(monkeypatch, settings={
        "snapcast_mode": "external",
        "snapcast_external_host": "192.168.1.50",
        "snapcast_external_feed_url": "tcp://192.168.1.50:4953",
    })
    await b.enable()  # LAN-first default lets a private host through
    assert b._connected
    await b.disable()


# ── playback: flow-mode advance vs outage ─────────────────────────────────────


def _track(tid="t1"):
    return type("T", (), {"id": tid, "title": "Song", "stream_key": "/p/1.flac"})()


async def test_play_spawns_feed_then_clean_eof_advances(monkeypatch):
    feed = FakeFeed()
    b = make_backend(monkeypatch, feeds=[feed])
    await b.enable()
    await b.play("ignored", _track())
    assert feed.started and b.is_playing
    feed.finish(0)  # clean EOF at track end
    await _wait_until(lambda: b._t_advance.await_count == 1)
    assert not b.is_playing


async def test_play_midtrack_death_holds_outage(monkeypatch):
    outages = []
    monkeypatch.setattr("app.output.session.notify_outage",
                        lambda r: outages.append(r))
    f1 = FakeFeed()
    b = make_backend(monkeypatch, feeds=[f1])
    await b.enable()
    await b.play("ignored", _track())
    f1.finish(1)  # crash mid-track (rc != 0)
    await _wait_until(lambda: outages)
    assert outages == ["snapcast_feed_failed"]  # held via supervisor, not drained
    b._t_advance.assert_not_awaited()            # NOT an advance
    assert not b.is_playing
    await b.stop()


async def test_advance_reentry_into_play_does_not_deadlock(monkeypatch):
    """The P0 the review caught: advance_cb re-enters play() (the production
    _do_advance shape). With advance scheduled on a fresh task the feed task
    returns first, so play()->_teardown_feed cancels the OLD (done) task, never
    the one it runs inside. Inline advance would deadlock here."""
    f1, f2 = FakeFeed(), FakeFeed()
    b = make_backend(monkeypatch, feeds=[f1, f2])
    await b.enable()
    done = asyncio.Event()
    plays = {"n": 0}

    async def _advance():
        plays["n"] += 1
        if plays["n"] == 1:
            await b.play("ignored", _track("t2"))  # re-enter play() from advance
        else:
            done.set()

    b._advance_cb = _advance
    await b.play("ignored", _track("t1"))
    f1.finish(0)                                    # clean EOF -> advance -> play(t2)
    await _wait_until(lambda: f2.started, timeout=2.0)   # play(t2) ran, no deadlock
    f2.finish(0)                                    # -> advance again -> done
    await asyncio.wait_for(done.wait(), timeout=2.0)
    await b.stop()


async def test_play_confirms_start_with_supervisor(monkeypatch):
    """Healthy server-fed playback must notify_confirmed or the 12s deadline
    misclassifies it as an outage (the confirm-timeout P0)."""
    confirmed = []
    monkeypatch.setattr("app.output.session.notify_confirmed",
                        lambda tok: confirmed.append(tok))

    class _Sup:
        def current_token(self):
            return 4242
    monkeypatch.setattr("app.output.session.get_supervisor", lambda: _Sup())

    b = make_backend(monkeypatch, feeds=[FakeFeed()])
    await b.enable()
    await b.play("ignored", _track())
    assert confirmed == [4242]  # confirmed once, with the dispatch's token
    await b.stop()


async def test_play_unresolvable_source_raises(monkeypatch):
    b = make_backend(monkeypatch)
    monkeypatch.setattr(snap_mod, "_default_resolve_source",
                        AsyncMock(return_value=None))
    await b.enable()
    with pytest.raises(DeviceNotReadyError):
        await b.play("ignored", _track())


# ── zero-clients (R15/AE2) ────────────────────────────────────────────────────


async def test_zero_clients_discover_is_empty_not_error(monkeypatch):
    b = make_backend(monkeypatch, control=FakeControl([]))
    await b.enable()
    assert await b.discover_devices() == []


async def test_play_proceeds_with_zero_clients(monkeypatch):
    feed = FakeFeed()
    b = make_backend(monkeypatch, control=FakeControl([]), feeds=[feed])
    await b.enable()
    await b.play("ignored", _track())
    assert feed.started and b.is_playing  # audio runs even with no clients
    await b.stop()


# ── device enumeration via control-RPC ────────────────────────────────────────


async def test_discover_devices_lists_snapclients(monkeypatch):
    b = make_backend(monkeypatch)
    await b.enable()
    devs = await b.discover_devices()
    assert {d.id for d in devs} == {"c1", "c2"}
    assert all(d.backend_type == "snapcast" for d in devs)


# ── master volume fan-out + zoning ────────────────────────────────────────────


async def test_master_volume_fans_out_to_clients(monkeypatch):
    b = make_backend(monkeypatch)
    await b.enable()
    await b.set_volume(0.5)
    cvol_calls = [c for c in b._t_ctrl.calls if c[0] == "cvol"]
    assert {c[1] for c in cvol_calls} == {"c1", "c2"}  # both clients written


async def test_can_manage_topology_embedded_vs_external(monkeypatch):
    emb = make_backend(monkeypatch)
    await emb.enable()
    assert emb.can_manage_topology() is True
    ext = make_backend(monkeypatch, settings={
        "snapcast_mode": "external",
        "snapcast_external_host": "8.8.8.8",  # public literal, passes SSRF
        "snapcast_external_feed_url": "tcp://8.8.8.8:4953",
    })
    await ext.enable()
    assert ext.can_manage_topology() is False


async def test_assign_client_to_group(monkeypatch):
    b = make_backend(monkeypatch)
    await b.enable()
    await b.assign_client_to_group("c2", "g1")
    gclient_calls = [c for c in b._t_ctrl.calls if c[0] == "gclients"]
    assert gclient_calls and "c2" in gclient_calls[-1][2]


async def test_set_group_volume_is_client_fanout(monkeypatch):
    b = make_backend(monkeypatch)
    await b.enable()
    await b.set_group_volume("g1", 0.6)
    cvol = [c for c in b._t_ctrl.calls if c[0] == "cvol"]
    assert {c[1] for c in cvol} == {"c1", "c2"}
