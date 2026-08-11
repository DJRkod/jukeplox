"""Radio Mode per-backend endless-mode EOS suppression + reconnect (plan U4).

Every backend gains an endless-mode path: a radio pseudo-Track (detected via the
SG-03 sentinel — ``is_radio_track`` / ``duration_ms=0``) plays an arbitrary
endless URL and NEVER fires ``advance_cb``; an upstream drop/idle RECONNECTS
(bounded, sustained-progress-aware) rather than advancing or going silent, and
after the cap surfaces a ``failed``/offline state.

Device-level runtime behavior is rig-validated (headless can't drive real
GStreamer / Cast / DLNA / AirPlay). These tests assert the LOGIC via the same
mock seams the existing per-backend tests use: advance suppressed, watchdog
disarmed, reconnect attempted, failed-state hook fired, and — critically — that
non-radio (finite) tracks are byte-for-byte unchanged (AE7).
"""

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.plex.models import Track
from app.radio.session import make_radio_track
from app.output import radio_endless


# ── shared helpers ────────────────────────────────────────────────────────────

def make_finite_track(tid="t1", container="flac") -> Track:
    t = Track(id=tid, title="Song", artist="A", album="B", duration_ms=180000,
              stream_key="/parts/1/f." + container)
    t.container = container
    return t


def make_radio() -> Track:
    """A radio pseudo-Track exactly as U3's session hands the router."""
    station = MagicMock()
    station.stationuuid = "st-1"
    station.name = "Jazz FM"
    station.favicon = None
    return make_radio_track(station, "http://radio.example/stream")


# ══════════════════════════════════════════════════════════════════════════════
# ReconnectPolicy (the shared bounded + sustained-progress-aware counter)
# ══════════════════════════════════════════════════════════════════════════════

def test_reconnect_policy_sustained_progress_resets_budget():
    """A connection that played past the sustained window resets the attempt
    budget on the next drop — a healthy station gets the full budget again."""
    clock = {"t": 0.0}
    pol = radio_endless.ReconnectPolicy(time_fn=lambda: clock["t"])
    pol.begin()
    # Burn a couple of quick drops (no sustained progress between them).
    assert pol.should_reconnect() is True   # attempt 1
    pol.mark_connected()
    assert pol.should_reconnect() is True    # attempt 2 (dropped immediately)
    assert pol.attempts == 2
    # Now a connection that SUSTAINS past the window.
    pol.mark_connected()
    clock["t"] += radio_endless.RADIO_SUSTAINED_PROGRESS_S + 1
    assert pol.should_reconnect() is True     # sustained → budget reset → attempt 1
    assert pol.attempts == 1


def test_reconnect_policy_dribble_still_trips_cap():
    """ADV-5: a station dribbling a few bytes per attempt (no sustained
    progress) burns the whole budget and trips the cap — never forever."""
    clock = {"t": 0.0}
    pol = radio_endless.ReconnectPolicy(time_fn=lambda: clock["t"])
    pol.begin()
    results = []
    for _ in range(radio_endless.RADIO_RECONNECT_MAX_ATTEMPTS + 2):
        # Each "connection" lives only a fraction of the sustained window.
        pol.mark_connected()
        clock["t"] += 1.0  # << RADIO_SUSTAINED_PROGRESS_S
        results.append(pol.should_reconnect())
    # Exactly MAX_ATTEMPTS Trues, then Falses (cap tripped).
    assert results[:radio_endless.RADIO_RECONNECT_MAX_ATTEMPTS] == \
        [True] * radio_endless.RADIO_RECONNECT_MAX_ATTEMPTS
    assert results[radio_endless.RADIO_RECONNECT_MAX_ATTEMPTS] is False


# ══════════════════════════════════════════════════════════════════════════════
# Direct (GStreamer)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def gst_mock():
    mock_pipeline = MagicMock()
    mock_gst = MagicMock()
    mock_gst.ElementFactory.make.return_value = mock_pipeline
    mock_gst.State.PLAYING = "PLAYING"
    mock_gst.State.PAUSED = "PAUSED"
    mock_gst.State.NULL = "NULL"
    with patch("app.output.direct._GST_AVAILABLE", True):
        with patch("app.output.direct.Gst", mock_gst, create=True):
            with patch("app.output.direct.ensure_bus_mainloop"):
                yield mock_gst, mock_pipeline


def _direct_error_msg(src_name, domain):
    msg = MagicMock()
    src = MagicMock()
    src.get_name.return_value = src_name
    msg.src = src
    err = MagicMock()
    err.domain = domain
    msg.parse_error.return_value = (err, "debug")
    return msg


async def test_direct_radio_eos_does_not_advance(gst_mock):
    """AE4: a bus EOS on a radio endless stream reconnects, never advances."""
    from app.output.direct import DirectAudioBackend
    advance = []
    backend = DirectAudioBackend(advance_cb=lambda: advance.append(1))
    with patch.object(backend, "_radio_reconnect_on_loop") as recon:
        await backend.play("http://radio", make_radio())
        backend._on_eos(None, None)
        await asyncio.sleep(0)
    assert advance == []
    recon.assert_called_once()


async def test_direct_radio_source_error_reconnects_not_advance(gst_mock):
    """ADV-1: a souphttpsrc RESOURCE error in radio mode reconnects, never
    advances (the ERROR path is suppressed too, not just _on_eos)."""
    from app.output.direct import DirectAudioBackend
    advance = []
    backend = DirectAudioBackend(advance_cb=lambda: advance.append(1))
    with patch.object(backend, "_radio_reconnect_on_loop") as recon:
        await backend.play("http://radio", make_radio())
        backend._on_error(
            None, _direct_error_msg("souphttpsrc0", "gst-resource-error-quark"))
        await asyncio.sleep(0)
    assert advance == []
    recon.assert_called_once()


async def test_direct_radio_sink_error_still_outages_not_reconnect(gst_mock, fresh_supervisor):
    """A genuine local audio-sink RESOURCE failure is a device death even in
    radio mode — it still routes to outage, NOT the source reconnect."""
    from app.output.direct import DirectAudioBackend
    sup, timers, rec = fresh_supervisor
    outages = []
    sup.add_outage_listener(lambda token, track, reason: outages.append(reason))
    backend = DirectAudioBackend(advance_cb=lambda: None)
    with patch.object(backend, "_radio_reconnect_on_loop") as recon:
        await backend.play("http://radio", make_radio())
        backend._on_error(
            None, _direct_error_msg("audio-sink", "gst-resource-error-quark"))
        await asyncio.sleep(0)
    recon.assert_not_called()
    assert outages == ["sink_error"]


async def test_direct_radio_reconnect_bounded_then_failed(gst_mock):
    """AE9/ADV-5: repeated drops within the sustained window trip the cap and
    fire the failed-state hook rather than reconnecting forever."""
    from app.output.direct import DirectAudioBackend
    backend = DirectAudioBackend(advance_cb=lambda: None)
    failed = []
    backend.set_radio_failed_hook(lambda: failed.append(1))
    await backend.play("http://radio", make_radio())
    # No real sleeps; no sustained progress between attempts.
    with patch("asyncio.sleep", AsyncMock()), \
            patch.object(backend, "_sync_play"):
        # Drive reconnects directly. Each _radio_reconnect that succeeds does
        # NOT mark sustained (clock frozen), so the budget drains.
        for _ in range(radio_endless.RADIO_RECONNECT_MAX_ATTEMPTS + 1):
            await backend._radio_reconnect_run()
    assert failed == [1]


async def test_direct_finite_eos_still_advances(gst_mock):
    """AE7 non-regression: with radio mode OFF, a real EOS still advances."""
    from app.output.direct import DirectAudioBackend
    advance = []

    async def adv():
        advance.append(1)

    backend = DirectAudioBackend(advance_cb=adv)
    await backend.play("http://url", make_finite_track())
    assert backend._radio_mode is False
    backend._on_eos(None, None)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert advance == [1]


# ══════════════════════════════════════════════════════════════════════════════
# Chromecast
# ══════════════════════════════════════════════════════════════════════════════

def _make_cc():
    cc = MagicMock()
    cc.media_controller = MagicMock()
    cc.media_controller.register_status_listener = MagicMock()
    cc.media_controller.play_media = MagicMock()
    cc.media_controller.block_until_active = MagicMock()
    cc.media_controller.stop = MagicMock()
    return cc


@pytest.fixture
def cast_mock():
    mock_pcc = MagicMock()
    with patch("app.output.chromecast._CAST_AVAILABLE", True), \
         patch("app.output.chromecast.pychromecast", mock_pcc, create=True):
        yield mock_pcc


def _cast_status(player_state, idle_reason=None, current_time=None):
    s = MagicMock()
    s.player_state = player_state
    s.idle_reason = idle_reason
    s.current_time = current_time
    return s


async def test_cast_radio_idle_error_does_not_advance(cast_mock):
    """AE4: Cast IDLE(ERROR) on a radio stream reconnects, never advances."""
    from app.output.chromecast import ChromecastBackend, _AdvanceListener
    advance = []
    backend = ChromecastBackend(advance_cb=lambda: advance.append(1))
    backend._cast = _make_cc()
    await backend.play("http://radio", make_radio())
    assert backend._radio_mode is True
    with patch.object(backend, "_radio_reconnect_schedule") as recon:
        backend._is_playing = True
        _AdvanceListener(backend).new_media_status(
            _cast_status("IDLE", "ERROR"))
        await asyncio.sleep(0)
    assert advance == []
    recon.assert_called_once()


async def test_cast_radio_bypasses_flow_when_gapless_on(cast_mock, monkeypatch):
    """FE-3: with gapless_enabled True, a radio track selects the radio path and
    does NOT enter _play_flow (the queue stitcher)."""
    from app.output import chromecast
    from app.output.chromecast import ChromecastBackend
    from app import state as st
    monkeypatch.setattr(st, "_gapless_enabled", True)
    backend = ChromecastBackend(advance_cb=lambda: None)
    backend._cast = _make_cc()
    flow_calls = []

    async def _flow(*a, **k):
        flow_calls.append(1)

    radio_calls = []
    orig_radio = backend._play_radio

    async def _radio(url, md):
        radio_calls.append(1)
        await orig_radio(url, md)

    with patch.object(backend, "_play_flow", _flow), \
            patch.object(backend, "_play_radio", _radio):
        await backend.play("http://radio", make_radio())
    assert flow_calls == []       # stitcher bypassed
    assert radio_calls == [1]     # radio path taken


async def test_cast_radio_no_watchdog_and_buffered(cast_mock):
    """Radio arms no duration watchdog and loads with BUFFERED."""
    from app.output import chromecast
    from app.output.chromecast import ChromecastBackend, FLOW_STREAM_TYPE
    backend = ChromecastBackend(advance_cb=lambda: None)
    cc = _make_cc()
    backend._cast = cc
    await backend.play("http://radio", make_radio())
    assert backend._watchdog_task is None
    assert backend._duration_ms == 0
    # BUFFERED stream_type passed to the LOAD.
    kwargs = cc.media_controller.play_media.call_args.kwargs
    assert kwargs.get("stream_type") == FLOW_STREAM_TYPE == "BUFFERED"


async def test_cast_radio_reconnect_bounded_then_failed(cast_mock):
    """AE9/ADV-5: repeated Cast drops trip the cap and fire the failed hook."""
    from app.output.chromecast import ChromecastBackend
    backend = ChromecastBackend(advance_cb=lambda: None)
    backend._cast = _make_cc()
    failed = []
    backend.set_radio_failed_hook(lambda: failed.append(1))
    await backend.play("http://radio", make_radio())
    with patch("asyncio.sleep", AsyncMock()), \
            patch.object(backend, "_sync_play_radio"):
        for _ in range(radio_endless.RADIO_RECONNECT_MAX_ATTEMPTS + 1):
            await backend._radio_reconnect_run()
    assert failed == [1]


async def test_cast_finite_idle_finished_still_advances(cast_mock):
    """AE7 non-regression: with radio mode OFF a finite IDLE(FINISHED) still
    advances, and the duration watchdog is still armed."""
    from app.output.chromecast import ChromecastBackend, _AdvanceListener
    advance = []

    async def adv():
        advance.append(1)

    backend = ChromecastBackend(advance_cb=adv)
    backend._cast = _make_cc()
    await backend.play("http://url", make_finite_track())
    assert backend._radio_mode is False
    assert backend._watchdog_task is not None       # watchdog armed for a finite track
    backend._is_playing = True
    _AdvanceListener(backend).new_media_status(_cast_status("IDLE", "FINISHED"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert advance == [1]
    backend._cancel_watchdog()


# ══════════════════════════════════════════════════════════════════════════════
# DLNA
# ══════════════════════════════════════════════════════════════════════════════

def _make_dmr():
    dmr = MagicMock()
    dmr.async_set_transport_uri = AsyncMock()
    dmr.async_play = AsyncMock()
    dmr.async_update = AsyncMock()
    dmr.transport_state = "PLAYING"
    dmr._action = MagicMock(return_value=MagicMock(async_call=AsyncMock(return_value={})))
    return dmr


@pytest.fixture
def dlna_mock():
    with patch("app.output.dlna._DLNA_AVAILABLE", True):
        yield


def test_dlna_radio_didl_omits_duration(dlna_mock):
    """A radio DIDL carries no real <res duration> (endless stream)."""
    from app.output.dlna import DlnaBackend
    backend = DlnaBackend()
    didl = backend._track_didl("http://radio", make_radio())
    assert "duration=" not in didl
    # A finite track still carries its duration.
    finite = backend._track_didl("http://url", make_finite_track())
    assert 'duration="' in finite


def test_f14_dlna_radio_didl_uses_transcode_content_type_not_url(dlna_mock):
    """F14: for a radio stream served via the transcode-proxy, the DIDL mime is
    the transcode OUTPUT content-type (_radio_content_type), NOT derived from the
    extension-less proxy URL (which would coincidentally be audio/mpeg only while
    the format is mp3). Prove it by forcing an aac output type."""
    from app.output.dlna import DlnaBackend
    backend = DlnaBackend()
    # Simulate the proxy having minted an aac session (radio_proxy_url would set
    # this from radio_output_content_type on the play path).
    backend._radio_content_type = "audio/aac"
    # An extension-less proxy-style URL — a URL-derived mime would be audio/mpeg.
    didl = backend._track_didl("http://host/api/radio/stream/tok123", make_radio())
    assert "audio/aac" in didl, "radio DIDL must advertise the transcode output type"
    assert "audio/mpeg" not in didl

    # Fallback path (no proxy content-type) still derives from the URL as before.
    backend._radio_content_type = ""
    didl2 = backend._track_didl("http://host/stream.mp3", make_radio())
    assert "audio/mpeg" in didl2


def test_f13_fire_radio_failed_hook_swallows_and_noops():
    """F13: the shared fire_radio_failed_hook fires the hook, swallows any
    exception, and no-ops on a None hook."""
    from app.output.radio_endless import fire_radio_failed_hook
    import logging
    log = logging.getLogger("test.radio")

    calls = []
    fire_radio_failed_hook(lambda: calls.append(1), log, "Direct")
    assert calls == [1]

    # A raising hook is swallowed (no exception propagates).
    def _raise():
        raise RuntimeError("hook boom")
    fire_radio_failed_hook(_raise, log, "Cast")     # must not raise

    # A None hook is a clean no-op.
    fire_radio_failed_hook(None, log, "DLNA")


async def test_dlna_radio_two_stopped_reconnects_not_advance(dlna_mock):
    """AE4: 2×STOPPED on a radio stream reconnects (re-SetURI+Play), no advance."""
    from app.output.dlna import DlnaBackend, TransportState
    advance = []

    async def adv():
        advance.append(1)

    dmr = _make_dmr()

    async def _update(do_ping: bool = True):
        dmr.transport_state = TransportState.STOPPED

    dmr.async_update = AsyncMock(side_effect=_update)

    backend = DlnaBackend(advance_cb=adv)
    backend._dmr = dmr
    backend._is_playing = True
    backend._radio_mode = True
    backend._radio_url = "http://radio"
    backend._radio_metadata = make_radio()
    backend._radio_reconnect.begin()

    # After a couple reconnects, flip the policy to trip so the poll terminates.
    calls = {"n": 0}
    real_should = backend._radio_reconnect.should_reconnect

    def _should():
        calls["n"] += 1
        return calls["n"] <= 1   # allow one reconnect, then trip the cap

    backend._radio_reconnect.should_reconnect = _should  # type: ignore

    with patch("asyncio.sleep", AsyncMock()):
        await backend._poll_eos()

    assert advance == []                       # never advanced
    assert dmr.async_set_transport_uri.await_count >= 1   # reconnected at least once


async def test_dlna_radio_reconnect_bounded_then_failed(dlna_mock):
    """AE9/ADV-5: the reconnect helper trips the cap and fires the failed hook."""
    from app.output.dlna import DlnaBackend
    backend = DlnaBackend(advance_cb=lambda: None)
    backend._dmr = _make_dmr()
    backend._radio_mode = True
    backend._radio_url = "http://radio"
    backend._radio_metadata = make_radio()
    backend._radio_reconnect.begin()
    failed = []
    backend.set_radio_failed_hook(lambda: failed.append(1))
    offline = None
    for _ in range(radio_endless.RADIO_RECONNECT_MAX_ATTEMPTS + 1):
        offline = await backend._radio_reconnect_poll()
    assert offline is True
    assert failed == [1]


async def test_dlna_finite_two_stopped_still_advances(dlna_mock):
    """AE7 non-regression: with radio mode OFF, 2×STOPPED still advances."""
    from app.output.dlna import DlnaBackend, TransportState
    advance = []

    async def adv():
        advance.append(1)

    dmr = _make_dmr()

    async def _update(do_ping: bool = True):
        dmr.transport_state = TransportState.STOPPED

    dmr.async_update = AsyncMock(side_effect=_update)
    backend = DlnaBackend(advance_cb=adv)
    backend._dmr = dmr
    backend._is_playing = True
    assert backend._radio_mode is False
    with patch("asyncio.sleep", AsyncMock()):
        await backend._poll_eos()
    assert advance == [1]


# ══════════════════════════════════════════════════════════════════════════════
# AirPlay
# ══════════════════════════════════════════════════════════════════════════════

class _ExitingProc:
    def __init__(self, returncode):
        self.returncode = returncode
        self.stderr = None
        self._wait_event = asyncio.Event()

    def signal_exit(self):
        self._wait_event.set()

    async def wait(self):
        await self._wait_event.wait()
        return self.returncode


async def test_airplay_radio_end_of_input_reconnects_not_advance():
    """AE4: cliraop end-of-input (clean rc=0) in radio mode restarts ffmpeg,
    never advances."""
    from app.output.airplay import AirPlayBackend
    advance = []

    async def adv():
        advance.append(1)

    backend = AirPlayBackend(advance_cb=adv)
    backend._is_playing = True
    backend._radio_mode = True
    backend._radio_url = "http://radio"
    backend._radio_metadata = make_radio()
    backend._exit_handled = False

    proc = _ExitingProc(returncode=0)
    backend._cliap2_proc = proc

    with patch.object(backend, "_radio_restart", AsyncMock()) as restart:
        proc.signal_exit()
        await backend._process_watcher_body(proc)

    assert advance == []
    restart.assert_awaited_once()


async def test_airplay_radio_nonzero_exit_reconnects_not_outage(fresh_supervisor):
    """AE9: a non-zero exit in radio mode is a drop → reconnect, NOT the outage
    classifier."""
    from app.output.airplay import AirPlayBackend
    sup, timers, rec = fresh_supervisor
    outages = []
    sup.add_outage_listener(lambda token, track, reason: outages.append(reason))
    backend = AirPlayBackend(advance_cb=lambda: None)
    backend._is_playing = True
    backend._radio_mode = True
    backend._radio_url = "http://radio"
    backend._radio_metadata = make_radio()
    backend._exit_handled = False
    proc = _ExitingProc(returncode=1)
    backend._cliap2_proc = proc
    with patch.object(backend, "_radio_restart", AsyncMock()) as restart:
        proc.signal_exit()
        await backend._process_watcher_body(proc)
    assert outages == []
    restart.assert_awaited_once()


async def test_airplay_radio_reconnect_bounded_then_failed():
    """AE9/ADV-5: repeated end-of-input trips the cap and fires the failed hook
    instead of restarting forever."""
    from app.output.airplay import AirPlayBackend
    backend = AirPlayBackend(advance_cb=lambda: None)
    backend._radio_mode = True
    backend._radio_url = "http://radio"
    backend._radio_metadata = make_radio()
    backend._radio_reconnect.begin()
    failed = []
    backend.set_radio_failed_hook(lambda: failed.append(1))

    # Stub teardown + play so no real subprocesses spawn. play() succeeds each
    # time (no re-drop chaining), so every _radio_restart consumes exactly one
    # bounded attempt; the clock never advances past the sustained window, so
    # the budget drains and the cap trips.
    with patch.object(backend, "_teardown", AsyncMock()), \
            patch.object(backend, "play", AsyncMock()), \
            patch("asyncio.sleep", AsyncMock()):
        # MAX successful restarts, then the (MAX+1)th trips the cap → offline.
        for _ in range(radio_endless.RADIO_RECONNECT_MAX_ATTEMPTS):
            await backend._radio_restart(returncode=1)
            assert failed == []   # still reconnecting
        await backend._radio_restart(returncode=1)   # cap hit
    assert failed == [1]


async def test_airplay_finite_clean_exit_still_advances(fresh_supervisor, monkeypatch, tmp_path):
    """AE7 non-regression: with radio mode OFF, a clean exit still advances."""
    from app.output.airplay import AirPlayBackend
    advance = []

    async def adv():
        advance.append(1)

    backend = AirPlayBackend(advance_cb=adv)
    backend._is_playing = True
    backend._exit_handled = False
    assert backend._radio_mode is False
    proc = _ExitingProc(returncode=0)
    backend._cliap2_proc = proc
    with patch.object(backend, "_teardown", AsyncMock()):
        proc.signal_exit()
        await backend._process_watcher_body(proc)
    assert advance == [1]


# ══════════════════════════════════════════════════════════════════════════════
# F5 — iterative (non-recursive) reconnect: a play() that fails EVERY attempt
# terminates bounded (no runaway / no stack growth) and fires the hook ONCE.
# ══════════════════════════════════════════════════════════════════════════════


async def test_f5_direct_all_attempts_fail_bounded_and_hook_once(gst_mock):
    """Direct: a single _radio_reconnect_run() whose _sync_play raises on EVERY
    attempt must loop (not recurse), consume the bounded budget, and fire the
    failed hook exactly once — no runaway."""
    from app.output.direct import DirectAudioBackend
    backend = DirectAudioBackend(advance_cb=lambda: None)
    failed = []
    backend.set_radio_failed_hook(lambda: failed.append(1))
    await backend.play("http://radio", make_radio())

    attempts = {"n": 0}

    def _always_fail(url):
        attempts["n"] += 1
        raise RuntimeError("station down")

    with patch("asyncio.sleep", AsyncMock()), \
            patch.object(backend, "_sync_play", side_effect=_always_fail):
        # ONE call — the loop internally consumes the whole budget.
        await backend._radio_reconnect_run()

    assert failed == [1], "the failed hook fires exactly once when the cap is hit"
    # Bounded: attempts never exceed the cap (no runaway loop).
    assert attempts["n"] <= radio_endless.RADIO_RECONNECT_MAX_ATTEMPTS


async def test_f5_cast_all_attempts_fail_bounded_and_hook_once(cast_mock):
    """Cast: same — the reconnect loop fails every re-LOAD, caps out, fires once."""
    from app.output.chromecast import ChromecastBackend
    backend = ChromecastBackend(advance_cb=lambda: None)
    backend._cast = _make_cc()
    failed = []
    backend.set_radio_failed_hook(lambda: failed.append(1))
    await backend.play("http://radio", make_radio())

    attempts = {"n": 0}

    def _always_fail(*a, **k):
        attempts["n"] += 1
        raise RuntimeError("cast LOAD failed")

    with patch("asyncio.sleep", AsyncMock()), \
            patch.object(backend, "_sync_play_radio", side_effect=_always_fail):
        await backend._radio_reconnect_run()

    assert failed == [1]
    assert attempts["n"] <= radio_endless.RADIO_RECONNECT_MAX_ATTEMPTS


async def test_f5_airplay_all_attempts_fail_bounded_and_hook_once():
    """AirPlay: a play() that raises on every reconnect attempt must NOT recurse
    (the old code re-armed begin() mid-recovery → potential infinite recursion).
    The loop caps out and fires the failed hook exactly once; play() is never
    called more than the bounded budget."""
    from app.output.airplay import AirPlayBackend
    backend = AirPlayBackend(advance_cb=lambda: None)
    backend._radio_mode = True
    backend._radio_url = "http://radio"
    backend._radio_metadata = make_radio()
    backend._radio_reconnect.begin()
    failed = []
    backend.set_radio_failed_hook(lambda: failed.append(1))

    play_calls = {"n": 0}

    async def _always_fail(url, metadata):
        play_calls["n"] += 1
        raise RuntimeError("airplay restart failed")

    with patch.object(backend, "_teardown", AsyncMock()), \
            patch.object(backend, "play", side_effect=_always_fail), \
            patch("asyncio.sleep", AsyncMock()):
        # ONE call — the loop consumes the whole budget internally, no recursion.
        await backend._radio_restart(returncode=1)

    assert failed == [1], "failed hook fires exactly once at the cap"
    assert play_calls["n"] <= radio_endless.RADIO_RECONNECT_MAX_ATTEMPTS
    # The reconnect episode flag is cleared once the episode ends (no leak).
    assert backend._radio_reconnecting is False


async def test_f5_dlna_all_attempts_fail_bounded_and_hook_once(dlna_mock):
    """DLNA: a re-issue that raises every attempt loops (not recurses), caps out,
    fires the failed hook once, and returns True (end the poll)."""
    from app.output.dlna import DlnaBackend
    backend = DlnaBackend(advance_cb=lambda: None)
    backend._radio_mode = True
    backend._radio_url = "http://radio"
    backend._radio_metadata = make_radio()
    backend._radio_reconnect.begin()
    failed = []
    backend.set_radio_failed_hook(lambda: failed.append(1))

    dmr = _make_dmr()
    dmr.async_set_transport_uri = AsyncMock(side_effect=RuntimeError("SOAP down"))
    dmr.async_play = AsyncMock()
    backend._dmr = dmr

    with patch("asyncio.sleep", AsyncMock()):
        result = await backend._radio_reconnect_poll()

    assert result is True, "cap exhausted → end the poll (station offline)"
    assert failed == [1]
    assert dmr.async_set_transport_uri.await_count \
        <= radio_endless.RADIO_RECONNECT_MAX_ATTEMPTS
