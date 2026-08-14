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
        # Discovery surface: a superset of the connected ones — "c3" is visible
        # on the network but not yet trusted, which is exactly what the pairing
        # UI needs to offer and the zoning UI must not.
        self._discovered = [
            {"id": "c1", "name": "Kitchen", "paired": True, "connected": True,
             "url": "ws://192.0.2.11:8928/s"},
            {"id": "c2", "name": "Living", "paired": True, "connected": True,
             "url": "ws://192.0.2.12:8928/s"},
            {"id": "c3", "name": "Porch", "paired": False, "connected": False,
             "url": "ws://192.0.2.13:8928/s"},
        ]

    def add_event_listener(self, cb):
        self.listener = cb

    @property
    def api_path(self):
        return "/sendspin"

    def discovered_clients(self):
        return [dict(c) for c in self._discovered]

    def client_url(self, cid):
        for c in self._discovered:
            if c["id"] == cid:
                return c["url"]
        return ""

    async def connect_to_client(self, url):
        self.calls.append(("connect", url))

    async def disconnect_client(self, cid):
        self.calls.append(("disconnect", cid))

    async def unpair(self, cid):
        self.calls.append(("unpair", cid))

    async def revoke_unpaired(self, cid):
        self.calls.append(("untrust", cid))

    async def paired_clients(self):
        return [{"id": c["id"], "name": c["name"]} for c in self._clients]

    async def set_now_playing(self, **kw):
        self.calls.append(("np", kw))

    async def freeze_progress(self):
        self.calls.append(("freeze",))

    async def clear_now_playing(self):
        self.calls.append(("npclear",))

    async def set_album_artwork(self, data):
        self.calls.append(("art", data))

    async def configure_controls(self, *, seek_max_ms):
        self.calls.append(("controls", seek_max_ms))

    def set_transport_handler(self, cb):
        self.transport = cb

    # Mirrors the real adapter: a stream only exists once start_stream has run
    # AND at least one speaker is connected. `no_speakers` models the
    # nothing-paired case, where the real adapter cannot build a group at all.
    no_speakers = False

    stream_starts = 0

    async def start_stream(self):
        self.stream_started = True
        self.stream_starts += 1
        self._stream = not self.no_speakers

    def has_stream(self):
        # Mirrors the real adapter: a stream is only useful if somebody is
        # actually connected to hear it.
        return bool(getattr(self, "_stream", False)) and not self.no_speakers

    async def reconcile_stream(self):
        self.calls.append(("attach",))
        if self.no_speakers:
            self._stream = False          # room emptied → tear the stream down
        else:
            self._stream = True

    async def revoke_unpaired(self, cid):
        self.calls.append(("untrust", cid))

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


def _record_feed(feed, offset_ms):
    feed.start_offset_ms = offset_ms      # so tests can assert seek/resume
    return feed


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
        feed_factory=lambda s, h, off=0: _record_feed(next(feed_iter), off),
        host_resolver=lambda: host,
    )
    b._t_adapters, b._t_advance = adapters, advance
    return b


async def _wait_until(pred, timeout=1.0):
    async def _poll():
        while not pred():
            await asyncio.sleep(0)
    await asyncio.wait_for(_poll(), timeout)


def _slice_bytes():
    from app.output.sendspin import _FEED_SLICE_BYTES
    return _FEED_SLICE_BYTES


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


# ── discovery: service type + both spec directions (U1) ──────────────────────


class FakeAiozc:
    """Stands in for the ONE shared AsyncZeroconf."""

    def __init__(self):
        self.registered, self.unregistered = [], []

    async def async_register_service(self, info):
        self.registered.append(info)

    async def async_unregister_service(self, info):
        self.unregistered.append(info)


def _use_shared_aiozc(monkeypatch):
    from app import state
    zc = FakeAiozc()
    monkeypatch.setattr(state, "shared_aiozc", zc, raising=False)
    return zc


async def test_advertises_the_server_service_type_not_the_client_one(monkeypatch):
    """The spec assigns servers `_sendspin-server._tcp` and clients
    `_sendspin._tcp`. Advertising the client type (the original bug) makes
    jukeplox invisible to anything hunting for a Sendspin server."""
    zc = _use_shared_aiozc(monkeypatch)
    b = make_backend(monkeypatch)
    await b.enable()
    assert len(zc.registered) == 1
    info = zc.registered[0]
    assert info.type == "_sendspin-server._tcp.local."
    assert info.type != "_sendspin._tcp.local."
    assert info.port == 8927


async def test_advertisement_carries_the_websocket_path(monkeypatch):
    """Without the `path` TXT record a client has to guess the endpoint."""
    zc = _use_shared_aiozc(monkeypatch)
    b = make_backend(monkeypatch)
    await b.enable()
    props = zc.registered[0].properties
    assert props[b"path"] == b"/sendspin"
    assert props[b"name"] == b"jukeplox"


async def test_disable_unregisters_the_service(monkeypatch):
    zc = _use_shared_aiozc(monkeypatch)
    b = make_backend(monkeypatch)
    await b.enable()
    await b.disable()
    assert len(zc.unregistered) == 1


async def test_enable_succeeds_without_a_shared_zeroconf_stack(monkeypatch):
    """A D-Bus-only host has no shared stack — that costs mDNS advertisement,
    not the backend."""
    from app import state
    monkeypatch.setattr(state, "shared_aiozc", None, raising=False)
    b = make_backend(monkeypatch)
    await b.enable()
    assert b._connected


async def test_unadvertise_is_a_noop_when_registration_never_happened(monkeypatch):
    from app import state
    monkeypatch.setattr(state, "shared_aiozc", None, raising=False)
    b = make_backend(monkeypatch)
    await b.enable()
    await b.disable()  # must not raise despite nothing ever being registered
    assert not b._connected


async def test_discovered_speakers_include_visible_but_unpaired(monkeypatch):
    """The pairing surface is a superset of the zoning surface — you pair with
    something you can see but have not trusted yet."""
    b = make_backend(monkeypatch)
    await b.enable()
    found = await b.discovered_speakers()
    by_id = {s["id"]: s for s in found}
    assert set(by_id) == {"c1", "c2", "c3"}
    assert by_id["c3"]["paired"] is False and by_id["c3"]["connected"] is False
    # zoning still reports only the connected pair
    zones = await b.list_zones()
    assert {c["client_id"] for c in zones[0]["clients"]} == {"c1", "c2"}


async def test_discovered_speakers_empty_when_not_enabled(monkeypatch):
    b = make_backend(monkeypatch)
    assert await b.discovered_speakers() == []


async def test_connect_speaker_dials_out_to_an_advertised_client(monkeypatch):
    b = make_backend(monkeypatch)
    await b.enable()
    await b.connect_speaker("ws://192.0.2.13:8928/s")
    assert ("connect", "ws://192.0.2.13:8928/s") in b._t_adapters[0].calls


async def test_connect_speaker_refuses_when_disabled(monkeypatch):
    b = make_backend(monkeypatch)
    with pytest.raises(DeviceNotReadyError):
        await b.connect_speaker("ws://192.0.2.13:8928/s")


# ── now playing on speaker screens (U6) ───────────────────────────────────────


def _rich_track(thumb="/library/art/1", tid="t1"):
    return type("T", (), {
        "id": tid, "title": "Vanity Fair", "artist": "Mr. Bungle",
        "album": "California", "album_artist": "Mr. Bungle",
        "duration_ms": 215000, "thumb": thumb, "stream_key": "/p/1.flac",
    })()


def _fake_art(monkeypatch, data=b"JPEGBYTES", boom=False):
    class _Client:
        async def fetch_art(self, path, width=None):
            if boom:
                raise RuntimeError("art server down")
            return data, "image/jpeg"

    from app import state as st
    monkeypatch.setattr(st, "get_plex_client", AsyncMock(return_value=_Client()))


def _np_pushes(b):
    return [c[1] for c in b._t_adapters[0].calls if c[0] == "np"]


def _art_pushes(b):
    return [c[1] for c in b._t_adapters[0].calls if c[0] == "art"]


async def test_play_pushes_the_track_to_the_screens(monkeypatch):
    _fake_art(monkeypatch)
    b = make_backend(monkeypatch, feeds=[FakeFeed([b"x" * 100])])
    await b.enable()
    await b.play("ignored", _rich_track())
    pushes = _np_pushes(b)
    assert pushes and pushes[0]["title"] == "Vanity Fair"
    assert pushes[0]["artist"] == "Mr. Bungle"
    assert pushes[0]["album"] == "California"
    assert pushes[0]["duration_ms"] == 215000
    await b.stop()


async def test_progress_is_an_anchor_not_a_tick(monkeypatch):
    """One push per transition, never a stream of positions — the client
    extrapolates. A per-second tick would be needless traffic to a
    battery-powered speaker."""
    _fake_art(monkeypatch)
    b = make_backend(monkeypatch, feeds=[FakeFeed([b"x" * 100]) for _ in range(3)])
    await b.enable()
    await b.play("ignored", _rich_track())
    before = len(_np_pushes(b))
    await asyncio.sleep(0.05)
    assert len(_np_pushes(b)) == before   # nothing pushed while simply playing
    await b.stop()


async def test_resume_and_seek_reach_ffmpeg_not_just_the_progress_bar(monkeypatch):
    """The offset has to be passed to the decoder. Without it the audio restarts
    from 0:00 while the progress bar and the speaker screens read the offset and
    keep climbing — the track silently plays from the top."""
    _fake_art(monkeypatch)
    feeds = [FakeFeed([b"x" * 100]) for _ in range(4)]
    b = make_backend(monkeypatch, feeds=feeds)
    await b.enable()
    await b.play("ignored", _rich_track())
    assert feeds[0].start_offset_ms == 0
    await b.seek(90_000)
    assert feeds[1].start_offset_ms == 90_000, "seek decoded from the start"
    await b.stop()


async def test_an_unreadable_speaker_level_is_excluded_not_treated_as_zero(
        monkeypatch):
    """A failed volume read used to report 0.0, and that value is the INPUT to
    proportional group volume — so it got written straight back and genuinely
    silenced a working speaker."""
    b = make_backend(monkeypatch)
    await b.enable()
    a = b._t_adapters[0]
    a._clients = [{"id": "c1", "name": "K", "volume": None, "muted": False},
                  {"id": "c2", "name": "L", "volume": 0.8, "muted": False}]
    await b.set_group_volume("sendspin", 0.5)
    written = {c[1] for c in a.calls if c[0] == "cvol"}
    assert "c1" not in written, "an unknown level was written back to the speaker"
    assert "c2" in written


async def test_seek_reanchors_progress(monkeypatch):
    _fake_art(monkeypatch)
    b = make_backend(monkeypatch, feeds=[FakeFeed([b"x" * 100]) for _ in range(3)])
    await b.enable()
    await b.play("ignored", _rich_track())
    await b.seek(90_000)
    assert _np_pushes(b)[-1]["progress_ms"] == 90_000
    await b.stop()


async def test_pause_freezes_the_screens(monkeypatch):
    _fake_art(monkeypatch)
    b = make_backend(monkeypatch, feeds=[FakeFeed([b"x" * 100]) for _ in range(3)])
    await b.enable()
    await b.play("ignored", _rich_track())
    await b.pause()
    assert ("freeze",) in b._t_adapters[0].calls


async def test_stop_clears_the_screens(monkeypatch):
    _fake_art(monkeypatch)
    b = make_backend(monkeypatch, feeds=[FakeFeed([b"x" * 100])])
    await b.enable()
    await b.play("ignored", _rich_track())
    await b.stop()
    assert ("npclear",) in b._t_adapters[0].calls


async def test_album_art_reaches_the_screens(monkeypatch):
    _fake_art(monkeypatch, data=b"COVERBYTES")
    b = make_backend(monkeypatch, feeds=[FakeFeed([b"x" * 100])])
    await b.enable()
    await b.play("ignored", _rich_track())
    await _wait_until(lambda: _art_pushes(b))
    assert _art_pushes(b)[0] == b"COVERBYTES"
    await b.stop()


async def test_a_track_without_art_clears_rather_than_keeping_the_last_cover(
        monkeypatch):
    """AE3. The stale-cover failure is the one a manual check misses: the
    previous album stays on screen and looks correct."""
    _fake_art(monkeypatch, data=b"COVERBYTES")
    b = make_backend(monkeypatch, feeds=[FakeFeed([b"x" * 100]) for _ in range(3)])
    await b.enable()
    await b.play("ignored", _rich_track(thumb="/library/art/1"))
    await _wait_until(lambda: _art_pushes(b))
    await b.play("ignored", _rich_track(thumb=None, tid="t2"))
    await _wait_until(lambda: len(_art_pushes(b)) >= 2)
    assert _art_pushes(b)[-1] is None      # cleared, not left showing the old one
    await b.stop()


async def test_a_failed_art_fetch_clears_art_and_leaves_audio_alone(monkeypatch):
    _fake_art(monkeypatch, boom=True)
    b = make_backend(monkeypatch, feeds=[FakeFeed([b"x" * 100])])
    await b.enable()
    await b.play("ignored", _rich_track())
    await _wait_until(lambda: _art_pushes(b))
    assert _art_pushes(b)[-1] is None
    # audio still advanced normally despite the art failure
    await _wait_until(lambda: b._t_advance.await_count == 1)


async def test_only_implemented_capabilities_are_advertised(monkeypatch):
    """AE5. A lighting-capable speaker connects, plays, shows now-playing — and
    its lights stay idle because jukeplox never claims to drive them."""
    from app.output import sendspin_adapter as ssa
    families = ssa.IMPLEMENTED_ROLE_FAMILIES
    assert set(families) == {"player", "metadata", "artwork", "controller"}
    assert "visualizer" not in families and "color" not in families


# ── transport control from a paired speaker (U7) ──────────────────────────────


def _capture_transport(monkeypatch):
    """Patch the SHARED transport service — the same functions the web UI's
    routes now delegate to, so this asserts one implementation, not a copy."""
    fired = []
    from app import playback_control

    for name in ("playback_resume", "playback_pause", "playback_skip",
                 "playback_previous"):
        monkeypatch.setattr(playback_control, name,
                            AsyncMock(side_effect=lambda n=name: fired.append((n, None))))
    monkeypatch.setattr(playback_control, "playback_seek",
                        AsyncMock(side_effect=lambda pos: fired.append(
                            ("playback_seek", pos))))
    monkeypatch.setattr(playback_control, "playback_volume",
                        AsyncMock(side_effect=lambda level: fired.append(
                            ("playback_volume", level))))
    return fired


async def test_speaker_commands_reach_the_shared_transport(monkeypatch):
    fired = _capture_transport(monkeypatch)
    b = make_backend(monkeypatch)
    await b.enable()
    for action in ("play", "pause", "next", "previous"):
        await b.handle_transport(action)
    assert [f[0] for f in fired] == [
        "playback_resume", "playback_pause", "playback_skip", "playback_previous"]


async def test_a_speaker_command_is_not_gated_by_the_guest_toggles(monkeypatch):
    """AE4. Pairing IS the authorisation — a deliberate decision, not an
    oversight. With every guest control switched off, a paired speaker's next
    still advances the queue."""
    from app import database
    monkeypatch.setattr(database, "get_setting",
                        AsyncMock(return_value="0"))  # all guest controls off
    fired = _capture_transport(monkeypatch)
    b = make_backend(monkeypatch)
    await b.enable()
    await b.handle_transport("next")
    assert [f[0] for f in fired] == ["playback_skip"]


async def test_the_transport_handler_is_registered_on_enable(monkeypatch):
    b = make_backend(monkeypatch)
    await b.enable()
    assert b._t_adapters[0].transport == b.handle_transport


async def test_seek_ceiling_is_advertised_per_track(monkeypatch):
    """Seek is DISCARDED by the protocol with no ceiling set, so it must be
    refreshed for each track rather than once at enable."""
    _fake_art(monkeypatch)
    b = make_backend(monkeypatch, feeds=[FakeFeed([b"x" * 100])])
    await b.enable()
    await b.play("ignored", _rich_track())
    assert ("controls", 215000) in b._t_adapters[0].calls
    await b.stop()


async def test_seek_clamps_to_the_track_length(monkeypatch):
    _fake_art(monkeypatch)
    fired = _capture_transport(monkeypatch)
    b = make_backend(monkeypatch, feeds=[FakeFeed([b"x" * 100]) for _ in range(3)])
    await b.enable()
    await b.play("ignored", _rich_track())
    await b.handle_transport("seek", 999_999_999)
    assert ("playback_seek", 215000) in fired
    await b.stop()


async def test_relative_seek_is_applied_from_the_current_position(monkeypatch):
    _fake_art(monkeypatch)
    fired = _capture_transport(monkeypatch)
    b = make_backend(monkeypatch, feeds=[FakeFeed([b"x" * 100]) for _ in range(3)])
    await b.enable()
    await b.play("ignored", _rich_track())
    await b.handle_transport("seek_relative", -30_000)
    seeks = [f for f in fired if f[0] == "playback_seek"]
    assert seeks and seeks[-1][1] >= 0        # clamped, never negative
    await b.stop()


async def test_a_command_with_nothing_playing_does_not_resurrect_a_track(
        monkeypatch):
    fired = _capture_transport(monkeypatch)
    b = make_backend(monkeypatch)
    await b.enable()
    await b.handle_transport("seek", 5000)
    assert not [f for f in fired if f[0] == "playback_seek"]


async def test_a_command_while_disabled_is_ignored(monkeypatch):
    fired = _capture_transport(monkeypatch)
    b = make_backend(monkeypatch)
    await b.handle_transport("next")      # never enabled
    assert not fired


async def test_a_failing_transport_call_does_not_escape(monkeypatch):
    """A speaker's bad command must not take the backend down with it."""
    from app import playback_control
    monkeypatch.setattr(playback_control, "playback_skip",
                        AsyncMock(side_effect=RuntimeError("queue exploded")))
    b = make_backend(monkeypatch)
    await b.enable()
    await b.handle_transport("next")      # must not raise
    assert b._connected


async def test_speaker_volume_is_scaled_from_protocol_percent(monkeypatch):
    fired = _capture_transport(monkeypatch)
    b = make_backend(monkeypatch)
    await b.enable()
    await b.handle_transport("volume", 40)
    assert ("playback_volume", 0.4) in fired


# ── the REAL session supervisor (not a stubbed advance callback) ──────────────
#
# The Snapcast build shipped four P0s because no test wired a real
# OutputSessionSupervisor: every unit mocked the collaborator at its own seam,
# so nobody noticed the integration was simply absent. See
# docs/solutions/architecture-patterns/enforce-cross-unit-contracts-with-a-
# real-collaborator-test.md. These drive the real object.


def _real_supervisor(monkeypatch):
    from unittest.mock import MagicMock
    from app.output import session as sess
    from app.output.session import OutputSessionSupervisor
    from tests.conftest import FakeTimerFactory

    timers, rec = FakeTimerFactory(), MagicMock()
    sup = OutputSessionSupervisor(record_play=rec, timer_factory=timers)
    monkeypatch.setattr(sess, "_supervisor", sup, raising=False)
    monkeypatch.setattr(sess, "get_supervisor", lambda: sup)
    return sup, timers, rec


async def test_play_confirms_against_a_real_supervisor(monkeypatch):
    """A healthy start must satisfy the real confirmed-start chokepoint. When
    it does not, the supervisor's deadline fires and classifies working
    playback as an outage — one of the four P0s from the Snapcast build."""
    _fake_art(monkeypatch)
    sup, timers, _ = _real_supervisor(monkeypatch)
    b = make_backend(monkeypatch, feeds=[FakeFeed([b"x" * 100])])
    await b.enable()
    sup.on_dispatched(_rich_track(), play_recorded=False)
    token = sup.current_token()
    await b.play("ignored", _rich_track())
    for _ in range(8):
        await asyncio.sleep(0)
    assert sup.current_token() == token
    # The confirm landed, so firing the deadline must NOT raise an outage.
    outages = []
    sup.add_outage_listener(lambda *a, **k: outages.append(a))
    for t in list(timers.timers):
        t.cb()                       # fire the confirmed-start deadline
    for _ in range(8):
        await asyncio.sleep(0)
    assert not outages, "a healthy start was classified as an outage"
    await b.stop()


async def test_a_mid_track_feed_death_reaches_the_real_supervisor(monkeypatch):
    """The failure must arrive as a NOTIFIED outage, not a raise from a
    detached task — an unobserved exception means the hold never runs and the
    queue silently drains."""
    _fake_art(monkeypatch)
    sup, _timers, _ = _real_supervisor(monkeypatch)
    seen = []
    sup.add_outage_listener(lambda *a, **k: seen.append(a))
    b = make_backend(monkeypatch, feeds=[FakeFeed([], stall=True)])
    await b.enable()
    sup.on_dispatched(_rich_track(), play_recorded=False)
    await b.play("ignored", _rich_track())
    await _wait_until(lambda: seen, 2.0)
    assert seen, "the feed death never reached the supervisor"
    await b.stop()


# ── revocation, mute, and repeated tracks ─────────────────────────────────────


async def test_unpair_while_playing_stops_audio_and_refuses_commands(monkeypatch):
    """AE2. Pairing is the only authority boundary, and handle_transport has no
    per-sender identity — so the whole guarantee rests on the session being
    severed. Assert it end to end rather than trusting that."""
    _fake_art(monkeypatch)
    fired = _capture_transport(monkeypatch)
    b = make_backend(monkeypatch, feeds=[FakeFeed([b"x" * 100]) for _ in range(3)])
    await b.enable()
    a = b._t_adapters[0]
    await b.play("ignored", _rich_track())
    await b.unpair_speaker("c1")
    assert ("unpair", "c1") in a.calls          # record dropped AND session cut
    await b.disable()
    await b.handle_transport("next")            # a command after revocation
    assert not [f for f in fired if f[0] == "playback_skip"]


async def test_speaker_mute_reaches_every_speaker(monkeypatch):
    b = make_backend(monkeypatch)
    await b.enable()
    await b.handle_transport("mute", True)
    muted = [c for c in b._t_adapters[0].calls if c[0] == "cmute"]
    assert {c[1] for c in muted} == {"c1", "c2"}
    assert all(c[2] is True for c in muted)


async def test_group_mute_fans_out_to_every_speaker(monkeypatch):
    b = make_backend(monkeypatch)
    await b.enable()
    await b.set_group_mute("sendspin", True)
    assert {c[1] for c in b._t_adapters[0].calls if c[0] == "cmute"} == {"c1", "c2"}


async def test_a_second_track_replaces_the_stream_rather_than_stacking_one(
        monkeypatch):
    """The group is long-lived now, so every track calls start_stream on the
    same group. Without stopping the previous one they accumulate."""
    _fake_art(monkeypatch)
    b = make_backend(monkeypatch, feeds=[FakeFeed([b"x" * 100]) for _ in range(4)])
    await b.enable()
    await b.play("ignored", _rich_track())
    await b.play("ignored", _rich_track(tid="t2"))
    assert b._t_adapters[0].stream_starts <= 2
    await b.stop()


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


async def test_zero_speakers_does_not_drain_the_queue(monkeypatch):
    """With nothing connected there is no push sink, and the feed is built
    without real-time throttling because the push backpressure normally paces
    it. Unpaced, the loop read-and-discards a whole track in milliseconds,
    advances, and eats the entire queue — recording every track as played,
    since confirmation has already fired. It must run at wall-clock speed
    instead: silent, but still playing."""
    # ~1.2s of audio in slices; unpaced this finishes instantly.
    slices = [b"x" * _slice_bytes() for _ in range(12)]
    b = make_backend(monkeypatch, feeds=[FakeFeed(slices)])
    await b.enable()
    b._t_adapters[0].no_speakers = True
    await b.play("ignored", _track())
    await asyncio.sleep(0.25)
    assert b._t_advance.await_count == 0, (
        "the queue advanced while no speakers were connected — the feed is "
        "running unpaced and will drain the whole queue")
    assert b.is_playing
    await b.stop()


async def test_a_speaker_connecting_mid_track_starts_hearing_it(monkeypatch):
    b = make_backend(monkeypatch, feeds=[FakeFeed([b"x" * _slice_bytes()
                                                   for _ in range(12)])])
    await b.enable()
    a = b._t_adapters[0]
    a.no_speakers = True
    await b.play("ignored", _track())
    await asyncio.sleep(0.05)
    a.no_speakers = False                      # speaker powers on mid-track
    await _wait_until(lambda: a.has_stream(), 2.0)
    assert ("attach",) in a.calls
    await b.stop()


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
