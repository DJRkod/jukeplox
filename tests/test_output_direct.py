"""Tests for the output abstraction layer and DirectAudioBackend.
GStreamer is mocked throughout so tests run without hardware."""

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


async def test_router_set_volume_delegates():
    router = OutputRouter()
    router.set_backend(make_mock_backend())
    await router.set_volume(0.6)
    router.active.set_volume.assert_awaited_with(0.6)
