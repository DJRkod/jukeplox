"""Tests for the output abstraction layer and DirectAudioBackend.
GStreamer is mocked throughout so tests run without hardware."""

import threading

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

from app.output.base import OutputDevice
from app.output.router import OutputRouter
from app.plex.models import Track


def make_track(tid="t1") -> Track:
    return Track(id=tid, title="Song", artist="A", album="B", duration_ms=180000,
                 stream_key="/parts/1/f.mp3")


# ── DirectAudioBackend (GStreamer mocked) ─────────────────────────────────────

@pytest.fixture
def gst_mock():
    """Patch away GStreamer so DirectAudioBackend can be instantiated anywhere."""
    mock_pipeline = MagicMock()
    mock_gst = MagicMock()
    mock_gst.ElementFactory.make.return_value = mock_pipeline
    mock_gst.State.PLAYING = "PLAYING"
    mock_gst.State.PAUSED = "PAUSED"
    mock_gst.State.NULL = "NULL"
    with patch("app.output.direct._GST_AVAILABLE", True):
        with patch("app.output.direct.Gst", mock_gst, create=True):
            # Stub the lazy bus-loop starter so play()/_sync_play never spawns a
            # real GLib.MainLoop thread under test (fix U2). An import-time start
            # could not be intercepted this way — the starter must be lazy.
            with patch("app.output.direct.ensure_bus_mainloop"):
                yield mock_gst, mock_pipeline


async def test_direct_play_sets_pipeline_playing(gst_mock):
    from app.output.direct import DirectAudioBackend
    mock_gst, mock_pipeline = gst_mock
    backend = DirectAudioBackend()
    await backend.play("http://plex.local/stream.flac", make_track())
    mock_pipeline.set_state.assert_called_with("PLAYING")


async def test_direct_pause(gst_mock):
    from app.output.direct import DirectAudioBackend
    mock_gst, mock_pipeline = gst_mock
    backend = DirectAudioBackend()
    await backend.play("http://plex.local/stream.flac", make_track())
    await backend.pause()
    mock_pipeline.set_state.assert_called_with("PAUSED")


async def test_direct_resume(gst_mock):
    from app.output.direct import DirectAudioBackend
    mock_gst, mock_pipeline = gst_mock
    backend = DirectAudioBackend()
    await backend.play("http://url", make_track())
    await backend.pause()
    await backend.resume()
    # Last call should be PLAYING
    mock_pipeline.set_state.assert_called_with("PLAYING")


async def test_direct_stop_sets_null(gst_mock):
    from app.output.direct import DirectAudioBackend
    mock_gst, mock_pipeline = gst_mock
    backend = DirectAudioBackend()
    await backend.play("http://url", make_track())
    await backend.stop()
    mock_pipeline.set_state.assert_called_with("NULL")


async def test_direct_resume_seek_prerolls_then_seeks_accurate(gst_mock):
    """Position-resume (supervisor plan U3, R7): the preroll-then-seek dance —
    PAUSED → wait ASYNC_DONE (blocking get_state) → seek FLUSH|ACCURATE →
    PLAYING. Seeking a playbin before preroll completes fails, so the order
    is the contract."""
    from app.output.direct import DirectAudioBackend
    mock_gst, mock_pipeline = gst_mock
    backend = DirectAudioBackend()
    await backend.play("http://url", make_track())
    mock_pipeline.reset_mock()

    await backend.resume_seek(30_000)

    names = [c[0] for c in mock_pipeline.mock_calls if c[0] != "__bool__"]
    assert names == ["set_state", "get_state", "seek_simple", "set_state"]
    assert mock_pipeline.set_state.call_args_list[0].args == ("PAUSED",)
    assert mock_pipeline.set_state.call_args_list[1].args == ("PLAYING",)
    seek_args = mock_pipeline.seek_simple.call_args.args
    assert seek_args[0] is mock_gst.Format.TIME
    assert seek_args[1] == (mock_gst.SeekFlags.FLUSH | mock_gst.SeekFlags.ACCURATE)
    assert seek_args[2] == 30_000 * 1_000_000  # ms → ns


async def test_direct_resume_seek_without_pipeline_is_noop(gst_mock):
    from app.output.direct import DirectAudioBackend
    mock_gst, mock_pipeline = gst_mock
    backend = DirectAudioBackend()
    await backend.resume_seek(30_000)  # never played — nothing to seek
    mock_pipeline.seek_simple.assert_not_called()


async def test_direct_set_volume(gst_mock):
    from app.output.direct import DirectAudioBackend
    mock_gst, mock_pipeline = gst_mock
    backend = DirectAudioBackend()
    await backend.play("http://url", make_track())
    await backend.set_volume(0.5)
    assert await backend.get_volume() == 0.5
    mock_pipeline.set_property.assert_any_call("volume", 0.5)


async def test_direct_volume_clamped(gst_mock):
    from app.output.direct import DirectAudioBackend
    backend = DirectAudioBackend()
    await backend.set_volume(2.0)
    assert await backend.get_volume() == 1.0
    await backend.set_volume(-1.0)
    assert await backend.get_volume() == 0.0


async def test_direct_uses_pulsesink_when_pulse_server_set(gst_mock, monkeypatch):
    """Linux host-audio passthrough (U6): PULSE_SERVER set + default device →
    route through pulsesink so the container plays on the host's speakers."""
    from app.output.direct import DirectAudioBackend
    mock_gst, mock_pipeline = gst_mock
    monkeypatch.setenv("PULSE_SERVER", "unix:/tmp/pulse/native")
    backend = DirectAudioBackend()  # device_id == "default"
    await backend.play("http://url", make_track())
    mock_gst.ElementFactory.make.assert_any_call("pulsesink", "audio-sink")


async def test_direct_default_device_uses_alsasink_without_pulse(gst_mock, monkeypatch):
    """The common case: no PULSE_SERVER + default device → play straight to the
    host ALSA card via alsasink (docker run --device /dev/snd). Deterministic —
    not delegated to playbin's pulse-first autoaudiosink."""
    from app.output.direct import DirectAudioBackend
    mock_gst, mock_pipeline = gst_mock
    monkeypatch.delenv("PULSE_SERVER", raising=False)
    backend = DirectAudioBackend()  # device_id == "default"
    await backend.play("http://url", make_track())
    mock_gst.ElementFactory.make.assert_any_call("alsasink", "audio-sink")


async def test_direct_no_pulsesink_when_pulse_server_unset(gst_mock, monkeypatch):
    """No PULSE_SERVER → never route through pulsesink (Pulse is opt-in only)."""
    from app.output.direct import DirectAudioBackend
    mock_gst, mock_pipeline = gst_mock
    monkeypatch.delenv("PULSE_SERVER", raising=False)
    backend = DirectAudioBackend()
    await backend.play("http://url", make_track())
    made = [c.args[0] for c in mock_gst.ElementFactory.make.call_args_list]
    assert "pulsesink" not in made


# ── U1: alsasink missing → fail LOUD, never fall through to a broken default ───

def _sink_make(pipeline, *, alsasink):
    """ElementFactory.make side_effect: playbin → pipeline mock, alsasink →
    caller-chosen value, everything else → a fresh mock."""
    def _make(name, _label=None):
        if name == "playbin":
            return pipeline
        if name == "alsasink":
            return alsasink
        return MagicMock()
    return _make


async def test_direct_default_device_alsasink_missing_raises_loud(gst_mock, monkeypatch):
    """Missing gstreamer1.0-alsa on the default path → DeviceNotReadyError, NOT a
    silent fall-through to playbin's default sink (which mis-routes / stalls and
    surfaces as a misleading confirm_timeout)."""
    from app.output.base import DeviceNotReadyError
    from app.output.direct import DirectAudioBackend
    mock_gst, mock_pipeline = gst_mock
    monkeypatch.delenv("PULSE_SERVER", raising=False)
    mock_gst.ElementFactory.make.side_effect = _sink_make(mock_pipeline, alsasink=None)
    backend = DirectAudioBackend()  # device_id == "default"
    with pytest.raises(DeviceNotReadyError):
        await backend.play("http://url", make_track())
    # Must NOT have fallen through to setting a default/auto sink.
    assert not any(
        c.args and c.args[0] == "audio-sink"
        for c in mock_pipeline.set_property.call_args_list
    )


async def test_direct_explicit_device_alsasink_missing_raises_loud(gst_mock):
    """Missing alsasink on an explicitly chosen device → DeviceNotReadyError, not
    an AttributeError on None.set_property (the previously unguarded path)."""
    from app.output.base import DeviceNotReadyError
    from app.output.direct import DirectAudioBackend
    mock_gst, mock_pipeline = gst_mock
    mock_gst.ElementFactory.make.side_effect = _sink_make(mock_pipeline, alsasink=None)
    backend = DirectAudioBackend()
    backend._device_id = "hw:CARD=Device"  # non-default → explicit sink path
    with pytest.raises(DeviceNotReadyError):
        await backend.play("http://url", make_track())


async def test_direct_default_device_alsasink_present_sets_sink(gst_mock, monkeypatch):
    """Regression: alsasink present → sink is set on the pipeline as before."""
    from app.output.direct import DirectAudioBackend
    mock_gst, mock_pipeline = gst_mock
    sink = MagicMock()
    monkeypatch.delenv("PULSE_SERVER", raising=False)
    mock_gst.ElementFactory.make.side_effect = _sink_make(mock_pipeline, alsasink=sink)
    backend = DirectAudioBackend()
    await backend.play("http://url", make_track())
    mock_pipeline.set_property.assert_any_call("audio-sink", sink)


# ── U2: default-context GLib bus main loop, started lazily ─────────────────────

async def test_ensure_bus_mainloop_starts_once_and_is_idempotent():
    """The bus main loop starts exactly one daemon thread, and repeat calls (a
    second play / new pipeline) never start a second one."""
    import app.output.direct as direct
    fake_glib = MagicMock()
    with patch.object(direct, "_GST_AVAILABLE", True), \
         patch.object(direct, "GLib", fake_glib, create=True), \
         patch.object(direct, "_bus_loop_started", False), \
         patch("app.output.direct.threading.Thread") as MockThread:
        direct.ensure_bus_mainloop()
        direct.ensure_bus_mainloop()
        assert MockThread.call_count == 1
        # daemon thread, named for the sweep-and-kill hygiene signature
        assert MockThread.call_args.kwargs.get("daemon") is True
        assert MockThread.call_args.kwargs.get("name") == "gst-bus-mainloop"
        MockThread.return_value.start.assert_called_once()


async def test_ensure_bus_mainloop_noop_without_gstreamer():
    """No GStreamer → the starter is inert (no thread), so importing on a
    GStreamer-less host never spawns a loop."""
    import app.output.direct as direct
    with patch.object(direct, "_GST_AVAILABLE", False), \
         patch.object(direct, "_bus_loop_started", False), \
         patch("app.output.direct.threading.Thread") as MockThread:
        direct.ensure_bus_mainloop()
        MockThread.assert_not_called()


async def test_direct_play_invokes_bus_mainloop_starter(gst_mock):
    """_sync_play establishes the bus loop (before add_signal_watch) — under the
    fixture the starter is stubbed, so no real thread spawns, but it IS called."""
    import app.output.direct as direct
    from app.output.direct import DirectAudioBackend
    backend = DirectAudioBackend()
    await backend.play("http://url", make_track())
    # gst_mock patches ensure_bus_mainloop to a MagicMock; assert it fired and
    # that no real bus-loop thread is alive.
    direct.ensure_bus_mainloop.assert_called()
    assert not any(t.name == "gst-bus-mainloop" for t in threading.enumerate())


async def test_eos_triggers_advance_callback(gst_mock):
    from app.output.direct import DirectAudioBackend
    advance_called = []

    async def advance():
        advance_called.append(True)

    mock_gst, mock_pipeline = gst_mock
    backend = DirectAudioBackend(advance_cb=advance)
    await backend.play("http://url", make_track())
    # Simulate EOS by calling the handler directly
    backend._on_eos(None, None)
    import asyncio
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert advance_called


# ── confirmed-start signal (2026-07-11 supervisor plan U1) ────────────────────

async def test_direct_connects_confirmed_start_bus_handlers(gst_mock):
    """play() wires the NEW state-changed/async-done handlers alongside the
    pre-existing eos/error watch."""
    from app.output.direct import DirectAudioBackend
    mock_gst, mock_pipeline = gst_mock
    backend = DirectAudioBackend()
    await backend.play("http://url", make_track())
    bus = mock_pipeline.get_bus()
    connected = [c.args[0] for c in bus.connect.call_args_list]
    assert "message::state-changed" in connected
    assert "message::async-done" in connected


async def test_direct_state_changed_to_playing_emits_confirmed_start(gst_mock, fresh_supervisor):
    """The mocked GStreamer bus shape: a state-changed message from the
    pipeline reaching PLAYING → confirmed-start → record_play once."""
    import asyncio
    from app.output.direct import DirectAudioBackend
    mock_gst, mock_pipeline = gst_mock
    sup, timers, rec = fresh_supervisor
    backend = DirectAudioBackend()
    token = sup.on_dispatched(make_track())    # state dispatches before backend.play
    await backend.play("http://url", make_track())
    assert backend._confirm_token == token
    rec.assert_not_called()                    # pipeline spawn alone must not count

    msg = MagicMock()
    msg.src = backend._pipeline                # the pipeline's own transition
    msg.parse_state_changed.return_value = ("READY", "PLAYING", "VOID_PENDING")
    backend._on_state_changed(None, msg)
    await asyncio.sleep(0)                     # call_soon_threadsafe hop
    rec.assert_called_once()
    assert backend._confirm_token is None      # one-shot


async def test_direct_child_element_state_change_ignored(gst_mock, fresh_supervisor):
    """state-changed fires for every element in the bin — only the pipeline's
    own PLAYING transition confirms."""
    import asyncio
    from app.output.direct import DirectAudioBackend
    mock_gst, mock_pipeline = gst_mock
    sup, timers, rec = fresh_supervisor
    backend = DirectAudioBackend()
    token = sup.on_dispatched(make_track())
    await backend.play("http://url", make_track())
    msg = MagicMock()
    msg.src = MagicMock()                      # some child element, not the bin
    msg.parse_state_changed.return_value = ("READY", "PLAYING", "VOID_PENDING")
    backend._on_state_changed(None, msg)
    await asyncio.sleep(0)
    rec.assert_not_called()
    assert backend._confirm_token == token


async def test_direct_non_playing_state_change_ignored(gst_mock, fresh_supervisor):
    import asyncio
    from app.output.direct import DirectAudioBackend
    mock_gst, mock_pipeline = gst_mock
    sup, timers, rec = fresh_supervisor
    backend = DirectAudioBackend()
    token = sup.on_dispatched(make_track())
    await backend.play("http://url", make_track())
    msg = MagicMock()
    msg.src = backend._pipeline
    msg.parse_state_changed.return_value = ("READY", "PAUSED", "PLAYING")
    backend._on_state_changed(None, msg)
    await asyncio.sleep(0)
    rec.assert_not_called()
    assert backend._confirm_token == token


async def test_direct_async_done_emits_confirmed_start_while_playing(gst_mock, fresh_supervisor):
    """ASYNC_DONE (preroll complete — buffers reached the sink) confirms while
    a play() is live; a second one is a no-op (token already cleared)."""
    import asyncio
    from app.output.direct import DirectAudioBackend
    mock_gst, mock_pipeline = gst_mock
    sup, timers, rec = fresh_supervisor
    backend = DirectAudioBackend()
    sup.on_dispatched(make_track())
    await backend.play("http://url", make_track())
    backend._on_async_done(None, MagicMock())
    backend._on_async_done(None, MagicMock())
    await asyncio.sleep(0)
    rec.assert_called_once()


# ── OutputRouter ──────────────────────────────────────────────────────────────

def make_mock_backend(playing=False):
    backend = MagicMock()
    backend.play = AsyncMock()
    backend.pause = AsyncMock()
    backend.resume = AsyncMock()
    backend.stop = AsyncMock()
    backend.set_volume = AsyncMock()
    backend.get_volume = AsyncMock(return_value=0.8)
    backend.discover_devices = AsyncMock(return_value=[])
    backend.set_device = AsyncMock()
    type(backend).is_playing = PropertyMock(return_value=playing)
    return backend


async def test_router_delegates_play():
    router = OutputRouter()
    backend = make_mock_backend()
    router.set_backend(backend)
    await router.play("http://url", make_track())
    backend.play.assert_awaited_once()


async def test_router_no_backend_raises():
    router = OutputRouter()
    with pytest.raises(RuntimeError):
        await router.play("http://url", make_track())


async def test_router_pending_swap_on_play():
    router = OutputRouter()
    old = make_mock_backend(playing=True)
    new = make_mock_backend()
    router.set_backend(old)
    router.set_backend(new)
    assert router.has_pending
    # Play triggers swap
    await router.play("http://url", make_track())
    assert not router.has_pending
    assert router.active is new


async def test_router_immediate_swap_when_idle():
    router = OutputRouter()
    old = make_mock_backend(playing=False)
    router.set_backend(old)
    new = make_mock_backend()
    router.set_backend(new)
    assert not router.has_pending
    assert router.active is new
    # Review fix PLX-2: the immediate branch STOPS the outgoing backend
    # (idle includes paused — a retired backend must not keep poll loops /
    # sessions alive). Scheduled as a task; drain it.
    await asyncio.sleep(0)
    old.stop.assert_awaited_once()


async def test_router_immediate_swap_same_backend_not_stopped():
    """Re-selecting the SAME backend (device change on one backend) must not
    stop it — set_device owns its own session teardown."""
    router = OutputRouter()
    backend = make_mock_backend(playing=False)
    router.set_backend(backend)
    router.set_backend(backend)
    await asyncio.sleep(0)
    backend.stop.assert_not_awaited()


async def test_router_set_volume_delegates():
    router = OutputRouter()
    router.set_backend(make_mock_backend())
    await router.set_volume(0.6)
    router.active.set_volume.assert_awaited_with(0.6)


# ── bus-ERROR classification + sink probe (2026-07-11 supervisor plan U2) ─────
# R15 KTD: a GStreamer bus ERROR is classified by ORIGINATING ELEMENT and
# domain — sink/RESOURCE = device-level (outage-suspected, R16), source/decode
# chain = track-level (advance-on-ERROR, the party-stall posture, preserved).

def _error_msg(src_name, domain):
    msg = MagicMock()
    src = MagicMock()
    src.get_name.return_value = src_name
    msg.src = src
    err = MagicMock()
    err.domain = domain
    msg.parse_error.return_value = (err, "debug info")
    return msg


async def test_sink_resource_error_reports_outage_not_advance(gst_mock, fresh_supervisor):
    """audio-sink + gst-resource-error-quark = the local device died →
    outage-suspected, never advance."""
    import asyncio
    from app.output.direct import DirectAudioBackend
    sup, timers, rec = fresh_supervisor
    outages = []
    sup.add_outage_listener(lambda token, track, reason: outages.append(reason))
    advance_called = []

    async def advance():
        advance_called.append(True)

    backend = DirectAudioBackend(advance_cb=advance)
    await backend.play("http://url", make_track())
    backend._on_error(None, _error_msg("audio-sink", "gst-resource-error-quark"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert outages == ["sink_error"]
    assert not advance_called
    assert backend.is_playing is False


async def test_source_error_still_advances(gst_mock, fresh_supervisor):
    """A decode/source-chain ERROR is the media's fault — keeps today's
    advance so one bad track can't freeze the queue (the party-stall fix)."""
    import asyncio
    from app.output.direct import DirectAudioBackend
    sup, timers, rec = fresh_supervisor
    outages = []
    sup.add_outage_listener(lambda *a: outages.append(a))
    advance_called = []

    async def advance():
        advance_called.append(True)

    backend = DirectAudioBackend(advance_cb=advance)
    await backend.play("http://url", make_track())
    backend._on_error(None, _error_msg("souphttpsrc0", "gst-resource-error-quark"))
    await asyncio.sleep(0.02)
    assert advance_called
    assert outages == []


async def test_sink_nonresource_error_still_advances(gst_mock, fresh_supervisor):
    """Sink-originated but non-RESOURCE (e.g. a stream/format error surfacing
    at the sink) is NOT device-level by definition — advance."""
    import asyncio
    from app.output.direct import DirectAudioBackend
    sup, timers, rec = fresh_supervisor
    outages = []
    sup.add_outage_listener(lambda *a: outages.append(a))
    advance_called = []

    async def advance():
        advance_called.append(True)

    backend = DirectAudioBackend(advance_cb=advance)
    await backend.play("http://url", make_track())
    backend._on_error(None, _error_msg("audio-sink", "gst-stream-error-quark"))
    await asyncio.sleep(0.02)
    assert advance_called
    assert outages == []


async def test_play_without_gstreamer_raises_device_not_ready():
    """The plain-RuntimeError asymmetry is closed (U2): a missing audio stack
    is a typed device-level error, so holder fallback can't drain the queue."""
    from app.output.base import DeviceNotReadyError
    from app.output.direct import DirectAudioBackend
    with patch("app.output.direct._GST_AVAILABLE", False):
        backend = DirectAudioBackend()
        with pytest.raises(DeviceNotReadyError):
            await backend.play("http://url", make_track())


async def test_probe_liveness_no_pipeline_is_unreachable(gst_mock):
    from app.output.direct import DirectAudioBackend
    backend = DirectAudioBackend()
    assert await backend.probe_liveness() == (False, None)


async def test_probe_liveness_reports_pipeline_state(gst_mock):
    """Direct probe semantics (plan KTD): audio-sink element liveness via a
    zero-timeout get_state peek."""
    from app.output.direct import DirectAudioBackend
    mock_gst, mock_pipeline = gst_mock
    mock_gst.StateChangeReturn.FAILURE = "FAILURE"
    mock_gst.StateChangeReturn.ASYNC = "ASYNC"
    backend = DirectAudioBackend()
    await backend.play("http://url", make_track())

    state = MagicMock()
    state.value_nick = "playing"
    mock_pipeline.get_state.return_value = ("SUCCESS", state, None)
    assert await backend.probe_liveness() == (True, "PLAYING")

    mock_pipeline.get_state.return_value = ("ASYNC", state, None)
    assert await backend.probe_liveness() == (True, "ASYNC")

    mock_pipeline.get_state.return_value = ("FAILURE", state, None)
    assert await backend.probe_liveness() == (False, None)


# ── gapless: arm/consume/boundary (2026-07-11 supervisor plan U7) ─────────────
# The Direct gapless advance-authority row: STREAM_START advances AND counts
# in gapless mode; EOS is suppressed EXCEPT when about-to-finish armed nothing;
# source/decode ERRORs stay track-level and sink/RESOURCE stays outage (the
# two-class split earlier in this file, unchanged). With the toggle off nothing
# is ever armed, every handler no-ops, and the per-track tests above ARE the
# byte-identical assertion.

import asyncio
import contextlib


async def _drain():
    """Settle marshaled boundary tasks — zero real delay."""
    for _ in range(12):
        await asyncio.sleep(0)


# The canonical queue-wiring helper lives in tests/conftest.py (shared with
# test_output_dlna.py and test_output_chromecast.py).
from tests.conftest import wire_queue as _wire_queue


async def test_direct_arm_next_stashes_atomic_slot_and_revoke_clears(gst_mock):
    """U6 backend contract: arm_next stashes one atomic (url, track) tuple;
    revoke_next nulls it and is idempotent (U6 also revokes right after a
    boundary consumed the arm — that must no-op)."""
    from app.output.direct import DirectAudioBackend
    backend = DirectAudioBackend()
    t2 = make_track("t2")
    await backend.arm_next("http://next", t2)
    assert backend._armed_next == ("http://next", t2)
    await backend.revoke_next()
    assert backend._armed_next is None
    await backend.revoke_next()
    assert backend._armed_next is None


async def test_direct_play_clears_armed_slot_and_pending_boundary(gst_mock):
    """A fresh dispatch owns the boundary again (U6 contract): play() clears
    the armed slot AND a consumed-but-unstarted boundary, and bumps the
    play-generation so a stale STREAM_START can't ride into the new track."""
    from app.output.direct import DirectAudioBackend
    backend = DirectAudioBackend()
    await backend.play("http://url1", make_track("t1"))
    await backend.arm_next("http://url2", make_track("t2"))
    backend._on_about_to_finish(backend._pipeline)
    gen = backend._play_gen
    await backend.play("http://url3", make_track("t3"))
    assert backend._armed_next is None
    assert backend._pending_boundary is None
    assert backend._play_gen > gen


async def test_direct_play_connects_gapless_signals(gst_mock):
    """play() wires about-to-finish (arm consumption, on the playbin) and
    message::stream-start (the audible-transition signal, on the bus)."""
    from app.output.direct import DirectAudioBackend
    mock_gst, mock_pipeline = gst_mock
    backend = DirectAudioBackend()
    await backend.play("http://url", make_track())
    pipe_signals = [c.args[0] for c in mock_pipeline.connect.call_args_list]
    assert "about-to-finish" in pipe_signals
    bus = mock_pipeline.get_bus()
    connected = [c.args[0] for c in bus.connect.call_args_list]
    assert "message::stream-start" in connected


async def test_about_to_finish_swaps_uri_in_handler_and_consumes_slot(gst_mock):
    """The entire gapless mechanic (capability-map contract): the streaming-
    thread handler pops the armed slot capture-to-local-then-null and sets
    the uri property SYNCHRONOUSLY in-handler — no pipeline state changes,
    no seeks — then records the consumed (url, track) for the loop side."""
    from app.output.direct import DirectAudioBackend
    mock_gst, mock_pipeline = gst_mock
    backend = DirectAudioBackend()
    t2 = make_track("t2")
    await backend.play("http://url1", make_track("t1"))
    await backend.arm_next("http://url2", t2)
    mock_pipeline.reset_mock()

    backend._on_about_to_finish(backend._pipeline)

    mock_pipeline.set_property.assert_called_once_with("uri", "http://url2")
    mock_pipeline.set_state.assert_not_called()
    mock_pipeline.seek_simple.assert_not_called()
    assert backend._armed_next is None
    assert backend._pending_boundary == ("http://url2", t2)


async def test_nothing_armed_at_about_to_finish_eos_gapped_advance(gst_mock):
    """Nothing armed → the handler leaves the playbin alone → natural EOS →
    today's gapped advance fires exactly once (not zero, not two)."""
    from app.output.direct import DirectAudioBackend
    mock_gst, mock_pipeline = gst_mock
    advance_called = []

    async def advance():
        advance_called.append(True)

    backend = DirectAudioBackend(advance_cb=advance)
    await backend.play("http://url1", make_track("t1"))
    mock_pipeline.reset_mock()

    backend._on_about_to_finish(backend._pipeline)
    mock_pipeline.set_property.assert_not_called()
    assert backend._pending_boundary is None

    backend._on_eos(None, None)
    await _drain()
    assert advance_called == [True]


async def test_eos_suppressed_when_armed_boundary_pending(gst_mock):
    """Advance-authority table: with an armed next consumed, STREAM_START is
    the SOLE authority — an EOS must not gap-advance under the chain."""
    from app.output.direct import DirectAudioBackend
    advance_called = []

    async def advance():
        advance_called.append(True)

    backend = DirectAudioBackend(advance_cb=advance)
    await backend.play("http://url1", make_track("t1"))
    await backend.arm_next("http://url2", make_track("t2"))
    backend._on_about_to_finish(backend._pipeline)

    backend._on_eos(None, None)
    await _drain()
    assert advance_called == []
    assert backend.is_playing is True  # audio continues through the chain


async def test_stream_start_boundary_advances_queue_and_counts_new_track(gst_mock, fresh_supervisor):
    """The U7 happy path: armed next + about-to-finish + STREAM_START → the
    queue moves the consumed track to current (old current to history)
    exactly as a normal advance would — WITHOUT any dispatch — and the new
    track's play counts exactly once via the U1 chokepoint."""
    from app.output.direct import DirectAudioBackend
    sup, timers, rec = fresh_supervisor
    backend = DirectAudioBackend()
    t1, t2 = make_track("t1"), make_track("t2")
    with contextlib.ExitStack() as stack:
        qe = _wire_queue(stack)
        await qe.append(t1)
        item1 = await qe.advance()               # t1 is the playing current
        token = sup.on_dispatched(item1.track)
        await backend.play("http://url1", t1)
        sup.on_playback_confirmed(token)         # t1's own confirmed start
        rec.assert_called_once()
        await qe.append(t2)

        await backend.arm_next("http://url2", t2)
        backend._on_about_to_finish(backend._pipeline)

        msg = MagicMock()
        msg.src = backend._pipeline
        backend._on_stream_start(None, msg)
        await _drain()

        assert qe.state.current.track_id == "t2"    # advanced, no dispatch
        assert [i.track_id for i in qe.history] == ["t1"]
        assert qe.queue == []
        assert rec.call_count == 2                  # t2 counted exactly once
        assert rec.call_args.args[0].id == "t2"
        assert backend._pending_boundary is None
        assert sup.current_token() is not None      # bookkeeping names t2


async def test_boundary_respects_play_recorded_mark(gst_mock, fresh_supervisor):
    """R19 at the boundary: an already-counted front item (held-item mark)
    consumed by a gapless boundary is NOT re-counted, and the mark is
    consumed so a later organic replay counts again (the
    _play_with_fallback posture)."""
    from app.output.direct import DirectAudioBackend
    sup, timers, rec = fresh_supervisor
    backend = DirectAudioBackend()
    t1, t2 = make_track("t1"), make_track("t2")
    with contextlib.ExitStack() as stack:
        qe = _wire_queue(stack)
        await qe.append(t1)
        await qe.advance()
        await backend.play("http://url1", t1)
        item2 = await qe.append(t2)
        item2.play_recorded = True               # counted before a hold (R19)

        await backend.arm_next("http://url2", t2)
        backend._on_about_to_finish(backend._pipeline)
        msg = MagicMock()
        msg.src = backend._pipeline
        backend._on_stream_start(None, msg)
        await _drain()

        assert qe.state.current.track_id == "t2"
        rec.assert_not_called()                  # never counted twice
        assert qe.state.current.play_recorded is False  # mark consumed


async def test_skip_during_gapless_drops_stale_boundary(gst_mock, fresh_supervisor):
    """The plan's stale-STREAM_START scenario: a skip's play() preempts the
    pipeline between the boundary signal and its loop-side callback — the
    play-generation compare IN THE MARSHALED CALLBACK drops it, so the dead
    pipeline's boundary advances nothing (no double-advance past the skip)."""
    from app.output.direct import DirectAudioBackend
    sup, timers, rec = fresh_supervisor
    backend = DirectAudioBackend()
    t1, t2, t3 = make_track("t1"), make_track("t2"), make_track("t3")
    with contextlib.ExitStack() as stack:
        qe = _wire_queue(stack)
        await qe.append(t1)
        await qe.advance()                       # t1 current
        await backend.play("http://url1", t1)
        await qe.append(t2)
        await backend.arm_next("http://url2", t2)
        backend._on_about_to_finish(backend._pipeline)
        stale_gen = backend._play_gen            # captured at STREAM_START time

        # The skip's fresh dispatch lands first: teardown + rebuild.
        await backend.play("http://url3", t3)

        await backend._gapless_boundary(stale_gen, t2)  # the marshaled callback
        assert qe.state.current.track_id == "t1"    # stale boundary dropped
        assert [i.track_id for i in qe.queue] == ["t2"]
        rec.assert_not_called()


async def test_stream_start_captures_gen_before_boundary_pop(gst_mock, fresh_supervisor):
    """Order pin: _on_stream_start must capture the play-generation BEFORE
    popping the pending boundary — a teardown interleaving between the two
    bumps the gen, and a stale boundary marshaled with the FRESH generation
    would defeat the loop-side double-advance guard. The instrumented pop
    bumps the gen at exactly the pop instant (the interleaved teardown); the
    marshaled boundary must carry the PRE-pop generation and drop as stale."""
    from app.output.direct import DirectAudioBackend
    sup, timers, rec = fresh_supervisor
    marshaled = []

    class InstrumentedBackend(DirectAudioBackend):
        # A class-level property intercepts the instance slot: any read of a
        # POPULATED _pending_boundary (the pop) simulates a teardown bumping
        # _play_gen at exactly that instant.
        @property
        def _pending_boundary(self):
            val = self.__dict__.get("_pending_boundary_slot")
            if val is not None:
                self._play_gen += 1            # the interleaved teardown
            return val

        @_pending_boundary.setter
        def _pending_boundary(self, value):
            self.__dict__["_pending_boundary_slot"] = value

        async def _gapless_boundary(self, play_gen, track):
            marshaled.append((play_gen, self._play_gen))
            await super()._gapless_boundary(play_gen, track)

    backend = InstrumentedBackend()
    t1, t2 = make_track("t1"), make_track("t2")
    with contextlib.ExitStack() as stack:
        qe = _wire_queue(stack)
        await qe.append(t1)
        await qe.advance()                       # t1 current
        await backend.play("http://url1", t1)
        await qe.append(t2)
        await backend.arm_next("http://url2", t2)
        backend._on_about_to_finish(backend._pipeline)
        gen_before = backend._play_gen

        msg = MagicMock()
        msg.src = backend._pipeline
        backend._on_stream_start(None, msg)
        await _drain()

        assert marshaled == [(gen_before, gen_before + 1)]  # pre-pop gen carried
        assert qe.state.current.track_id == "t1"            # stale → dropped
        assert [i.track_id for i in qe.queue] == ["t2"]
        rec.assert_not_called()


async def test_sink_error_mid_gapless_reports_outage_not_advance(gst_mock, fresh_supervisor):
    """The two-class ERROR split is UNCHANGED in gapless mode: sink/RESOURCE
    with a next armed → outage-suspected, never advance."""
    from app.output.direct import DirectAudioBackend
    sup, timers, rec = fresh_supervisor
    outages = []
    sup.add_outage_listener(lambda token, track, reason: outages.append(reason))
    advance_called = []

    async def advance():
        advance_called.append(True)

    backend = DirectAudioBackend(advance_cb=advance)
    await backend.play("http://url1", make_track("t1"))
    await backend.arm_next("http://url2", make_track("t2"))
    backend._on_error(None, _error_msg("audio-sink", "gst-resource-error-quark"))
    await _drain()
    assert outages == ["sink_error"]
    assert not advance_called


async def test_source_error_after_consumed_boundary_advances_and_clears_pending(gst_mock, fresh_supervisor):
    """A source/decode ERROR mid-gapless (e.g. the armed uri failed to
    pre-roll) keeps the track-level advance AND withdraws the pending
    boundary, so the EOS suppression can never wedge the pipeline's
    endgame."""
    from app.output.direct import DirectAudioBackend
    sup, timers, rec = fresh_supervisor
    advance_called = []

    async def advance():
        advance_called.append(True)

    backend = DirectAudioBackend(advance_cb=advance)
    await backend.play("http://url1", make_track("t1"))
    await backend.arm_next("http://url2", make_track("t2"))
    backend._on_about_to_finish(backend._pipeline)
    assert backend._pending_boundary is not None

    backend._on_error(None, _error_msg("souphttpsrc1", "gst-resource-error-quark"))
    await _drain()
    assert advance_called == [True]
    assert backend._pending_boundary is None


async def test_position_between_about_to_finish_and_stream_start_is_old_track(gst_mock):
    """Capability-map fact pinned for resume bookkeeping: between
    about-to-finish and STREAM_START the pipeline's position query still
    reports the OLD track — get_position stays a raw pipeline passthrough
    (hold._capture_position_ms reads it at outage entry), and the
    arm-consumption handler must not disturb it (no state changes, no
    seeks)."""
    from app.output.direct import DirectAudioBackend
    mock_gst, mock_pipeline = gst_mock
    backend = DirectAudioBackend()
    await backend.play("http://url1", make_track("t1"))
    await backend.arm_next("http://url2", make_track("t2"))
    mock_pipeline.query_position.return_value = (True, 170_500 * 1_000_000)
    mock_pipeline.reset_mock()  # keeps return_value; clears call records

    backend._on_about_to_finish(backend._pipeline)

    assert await backend.get_position() == 170_500  # still the old track
    mock_pipeline.set_state.assert_not_called()
    mock_pipeline.seek_simple.assert_not_called()


async def test_first_stream_start_after_play_is_not_a_boundary(gst_mock, fresh_supervisor):
    """The FIRST stream-start after a fresh play() is the dispatched track
    itself starting — never a boundary advance (the dispatch owns its
    confirmed-start via the state-changed/async-done handlers). Also the
    toggle-off shape: nothing armed means every stream-start no-ops."""
    from app.output.direct import DirectAudioBackend
    sup, timers, rec = fresh_supervisor
    backend = DirectAudioBackend()
    t1 = make_track("t1")
    with contextlib.ExitStack() as stack:
        qe = _wire_queue(stack)
        await qe.append(t1)
        await qe.advance()
        await backend.play("http://url1", t1)
        msg = MagicMock()
        msg.src = backend._pipeline
        backend._on_stream_start(None, msg)
        await _drain()
        assert qe.state.current.track_id == "t1"  # no advance
        assert qe.history == []
        rec.assert_not_called()


# ── Pipeline teardown under concurrent callers (#35) ──────────────────────────
#
# Teardown is reachable from the GLib bus thread and the asyncio loop at the
# same time — radio's error->reconnect->teardown cycle does exactly that. The
# pipeline handoff was already guarded for that reason, but the bus watch beside
# it was not, and detaching the same watch twice underflows GStreamer's watch
# refcount and emits a GLib warning from whichever caller loses.


class _GatedBackend:
    """Mixin that widens the teardown claim's read->write window.

    The real window is a couple of bytecodes wide. Left to chance, a
    concurrent-teardown test passes against a broken implementation on almost
    every run, so the interleave is forced: the first read of `_pipeline` on
    each thread parks at a barrier, guaranteeing both callers have read before
    either writes. A serialized (correctly locked) implementation never gets
    two threads to the barrier at once, so it trips the timeout instead — which
    is itself the proof that the claim held.
    """

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)

    def _install_gate(self, timeout=0.3):
        self._gate = threading.Barrier(2, timeout=timeout)
        self._tls = threading.local()

    @property
    def _pipeline(self):
        v = getattr(self, "_pipe_slot", None)
        if v is not None and not getattr(self._tls, "gated", False):
            self._tls.gated = True
            try:
                self._gate.wait()
            except threading.BrokenBarrierError:
                pass  # serialized — the other caller never reached the read
        return v

    @_pipeline.setter
    def _pipeline(self, v):
        self._pipe_slot = v


def _gated_backend(gst_mock):
    from app.output.direct import DirectAudioBackend

    class _Backend(_GatedBackend, DirectAudioBackend):
        pass

    b = _Backend.__new__(_Backend)
    b._install_gate()
    DirectAudioBackend.__init__(b)
    return b


def test_teardown_detaches_watch_once_and_releases_pipeline(gst_mock):
    """Happy path: a normal teardown detaches the bus watch exactly once and
    drives the pipeline to NULL."""
    from app.output.direct import DirectAudioBackend

    mock_gst, mock_pipeline = gst_mock
    backend = DirectAudioBackend()
    backend._pipeline = mock_pipeline

    backend._teardown_pipeline()

    bus = mock_pipeline.get_bus.return_value
    assert bus.remove_signal_watch.call_count == 1
    mock_pipeline.set_state.assert_called_with("NULL")
    assert backend._pipeline is None
    assert backend._is_playing is False


def test_teardown_with_no_pipeline_is_a_noop(gst_mock):
    """Edge: tearing down when nothing is live must not raise."""
    from app.output.direct import DirectAudioBackend

    backend = DirectAudioBackend()
    backend._teardown_pipeline()          # no exception
    assert backend._pipeline is None


def test_concurrent_teardowns_detach_the_watch_exactly_once(gst_mock):
    """Edge: two callers tearing down at the same instant must produce exactly
    one detach and one release, and neither may raise. A second
    remove_signal_watch() on the same bus is the GLib warning that becomes an
    abort under a fatal-warnings build."""
    mock_gst, mock_pipeline = gst_mock
    backend = _gated_backend(gst_mock)
    backend._pipeline = mock_pipeline

    errors = []

    def tear():
        try:
            backend._teardown_pipeline()
        except Exception as exc:      # noqa: BLE001 — the loser must not raise
            errors.append(exc)

    threads = [threading.Thread(target=tear) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    bus = mock_pipeline.get_bus.return_value
    assert not errors, f"a concurrent teardown raised: {errors}"
    assert bus.remove_signal_watch.call_count == 1, (
        f"bus watch detached {bus.remove_signal_watch.call_count} times")
    assert mock_pipeline.set_state.call_count == 1
    assert backend._pipeline is None


def test_teardown_releases_pipeline_even_if_detach_fails(gst_mock):
    """Error path: a failure detaching the watch must not strand a live
    pipeline — releasing it is the more important half of teardown."""
    from app.output.direct import DirectAudioBackend

    mock_gst, mock_pipeline = gst_mock
    mock_pipeline.get_bus.return_value.remove_signal_watch.side_effect = \
        RuntimeError("bus already disposed")

    backend = DirectAudioBackend()
    backend._pipeline = mock_pipeline
    backend._teardown_pipeline()          # must not raise

    mock_pipeline.set_state.assert_called_with("NULL")
    assert backend._pipeline is None


async def test_play_stop_play_cycle_still_works(gst_mock):
    """Integration: the guard must not break ordinary operation."""
    from app.output.direct import DirectAudioBackend

    mock_gst, mock_pipeline = gst_mock
    backend = DirectAudioBackend()

    await backend.play("http://plex.local/a.flac", make_track("t1"))
    assert backend._is_playing
    await backend.stop()
    assert backend._pipeline is None
    await backend.play("http://plex.local/b.flac", make_track("t2"))
    assert backend._is_playing
    mock_pipeline.set_state.assert_called_with("PLAYING")
