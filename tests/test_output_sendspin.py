"""U7 — Sendspin backend: enable/bind, bounded push-feed loop, flow-mode
advance/outage, dormancy, master fan-out, experimental label.

Faked adapter + feed — no aiosendspin, no PyAV, no ffmpeg.
"""

import asyncio
import sys

import pytest
from unittest.mock import AsyncMock

from app.output import sendspin as ss_mod
from app.output.base import DeviceNotReadyError
from app.output.multiroom import FeedStalled
from app.output.sendspin import SendspinBackend


class FakeAdapter:
    def __init__(self):
        self.calls = []
        self.stopped = False
        self.stream_started = False
        self.listener = None
        self._clients = [
            {"id": "c1", "name": "Kitchen", "volume": 0.4, "muted": False},
            {"id": "c2", "name": "Living", "volume": 0.8, "muted": False},
        ]

    def add_event_listener(self, cb):
        self.listener = cb

    async def start_stream(self):
        self.stream_started = True

    async def prepare_audio(self, pcm):
        self.calls.append(("prep", len(pcm)))

    async def commit_audio(self):
        self.calls.append(("commit",))

    async def sleep_to_limit_buffer(self):
        self.calls.append(("sleep",))

    def clients(self):
        return [dict(c) for c in self._clients]

    async def set_client_volume(self, cid, level):
        self.calls.append(("cvol", cid, level))

    async def set_client_mute(self, cid, muted):
        self.calls.append(("cmute", cid, muted))

    async def stop(self):
        self.stopped = True


class FakeFeed:
    def __init__(self, chunks, stall=False):
        self.chunks, self.stall = list(chunks), stall
        self.started = self.closed = False

    async def start(self):
        self.started = True

    async def read(self, n):
        if self.stall:
            raise FeedStalled("no first byte")
        return self.chunks.pop(0) if self.chunks else b""

    async def close(self):
        self.closed = True


def make_backend(monkeypatch, *, host="192.0.2.10", feeds=None, no_source=False):
    adapters = []

    async def _adapter_factory(**kw):
        a = FakeAdapter()
        a.kw = kw
        adapters.append(a)
        return a

    monkeypatch.setattr(
        ss_mod, "_default_resolve_source",
        AsyncMock(return_value=None if no_source else ("/music/x.flac", {})))

    feed_iter = iter(feeds or [FakeFeed([b"x" * 100]) for _ in range(10)])
    advance = AsyncMock()
    b = SendspinBackend(
        advance_cb=advance,
        adapter_factory=_adapter_factory,
        feed_factory=lambda s, h: next(feed_iter),
        host_resolver=lambda: host,
    )
    b._t_adapters, b._t_advance = adapters, advance
    return b


async def _wait_until(pred, timeout=1.0):
    async def _poll():
        while not pred():
            await asyncio.sleep(0)
    await asyncio.wait_for(_poll(), timeout)


def _track(tid="t1"):
    return type("T", (), {"id": tid, "title": "Song", "stream_key": "/p/1.flac"})()


# ── dormancy ──────────────────────────────────────────────────────────────────


def test_importing_sendspin_does_not_import_aiosendspin():
    assert "aiosendspin" not in sys.modules  # heavy lib stays behind enable


# ── enable / disable ──────────────────────────────────────────────────────────


async def test_enable_binds_explicit_lan_host(monkeypatch):
    b = make_backend(monkeypatch, host="192.0.2.10")
    await b.enable()
    assert b._connected
    assert b._t_adapters[0].kw["host"] == "192.0.2.10"  # explicit, not 0.0.0.0
    assert b._t_adapters[0].kw["port"] == 8927


async def test_enable_refuses_wildcard_host(monkeypatch):
    b = make_backend(monkeypatch, host="0.0.0.0")
    with pytest.raises(DeviceNotReadyError):
        await b.enable()
    assert not b._connected


async def test_enable_disable_enable_releases_and_rebinds(monkeypatch):
    b = make_backend(monkeypatch)
    await b.enable()
    a1 = b._t_adapters[0]
    await b.disable()
    assert a1.stopped  # 8927 released
    await b.enable()
    assert len(b._t_adapters) == 2 and b._connected  # fresh listener rebinds


async def test_enable_fail_closed_tears_down(monkeypatch):
    b = make_backend(monkeypatch)

    async def _boom(**kw):
        raise RuntimeError("bind failed")

    b._adapter_factory = _boom
    with pytest.raises(RuntimeError, match="bind failed"):
        await b.enable()
    assert not b._connected


# ── bounded push-feed loop + flow-mode advance ────────────────────────────────


async def test_feed_loop_pushes_bounded_then_advances(monkeypatch):
    feed = FakeFeed([b"x" * 100])  # one slice, then EOF
    b = make_backend(monkeypatch, feeds=[feed])
    await b.enable()
    await b.play("ignored", _track())
    await _wait_until(lambda: b._t_advance.await_count == 1)
    a = b._t_adapters[0]
    assert a.stream_started
    # bounded: every slice is followed by the backpressure yield (no unbounded
    # spin) — prepare + commit + sleep each fired for the single slice.
    kinds = [c[0] for c in a.calls]
    assert kinds.count("prep") == 1 and kinds.count("sleep") == 1
    assert not b.is_playing


async def test_feed_stall_holds_outage(monkeypatch):
    outages = []
    monkeypatch.setattr("app.output.session.notify_outage",
                        lambda r: outages.append(r))
    stalling = FakeFeed([], stall=True)
    b = make_backend(monkeypatch, feeds=[stalling])
    await b.enable()
    await b.play("ignored", _track())
    await _wait_until(lambda: outages)
    assert outages == ["sendspin_feed_stalled"]  # held, not a silent dead-stall
    assert not b.is_playing
    await b.stop()


async def test_play_confirms_start_with_supervisor(monkeypatch):
    confirmed = []
    monkeypatch.setattr("app.output.session.notify_confirmed",
                        lambda tok: confirmed.append(tok))

    class _Sup:
        def current_token(self):
            return 7

    monkeypatch.setattr("app.output.session.get_supervisor", lambda: _Sup())
    b = make_backend(monkeypatch, feeds=[FakeFeed([b"x" * 100])])
    await b.enable()
    await b.play("ignored", _track())
    assert confirmed == [7]
    await b.stop()


# ── master volume fan-out + enumeration + experimental ────────────────────────


async def test_master_volume_fans_out(monkeypatch):
    b = make_backend(monkeypatch)
    await b.enable()
    await b.set_volume(0.5)
    cvol = [c for c in b._t_adapters[0].calls if c[0] == "cvol"]
    assert {c[1] for c in cvol} == {"c1", "c2"}


async def test_discover_devices_lists_clients(monkeypatch):
    b = make_backend(monkeypatch)
    await b.enable()
    devs = await b.discover_devices()
    assert {d.id for d in devs} == {"c1", "c2"}
    assert all(d.backend_type == "sendspin" for d in devs)


async def test_zero_clients_is_empty(monkeypatch):
    b = make_backend(monkeypatch)
    await b.enable()
    b._t_adapters[0]._clients = []
    assert await b.discover_devices() == []


def test_experimental_flag():
    assert SendspinBackend.experimental is True
