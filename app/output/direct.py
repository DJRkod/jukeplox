"""GStreamer direct-audio output backend.

Requires GStreamer + PyGObject installed in the container (via apt).
On the host machine this file can still be imported; GStreamer is lazy-loaded
only when a DirectAudioBackend is instantiated and play() is called.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from app.output.base import AdvanceCallback, OutputDevice
from app.plex.models import Track

_GST_AVAILABLE = False

try:
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst, GLib

    if not Gst.is_initialized():
        Gst.init(None)
    _GST_AVAILABLE = True
except Exception:
    pass




class DirectAudioBackend:
    """Plays audio via a GStreamer playbin pipeline to a local audio device."""

    def __init__(self, advance_cb: AdvanceCallback | None = None):
        self._device_id: str = "default"
        self._volume: float = 0.5
        self._pipeline = None
        self._is_playing: bool = False
        self._advance_cb = advance_cb
        self._loop: asyncio.AbstractEventLoop | None = None

    # ── device enumeration ────────────────────────────────────────────────────

    async def discover_devices(self) -> list[OutputDevice]:
        if not _GST_AVAILABLE:
            return [OutputDevice(id="default", name="Default Audio Device", backend_type="direct")]
        return await asyncio.get_running_loop().run_in_executor(None, self._sync_discover)

    def _sync_discover(self) -> list[OutputDevice]:
        monitor = Gst.DeviceMonitor.new()
        monitor.add_filter("Audio/Sink", None)
        monitor.start()
        devices = monitor.get_devices()
        monitor.stop()
        result = [OutputDevice(id="default", name="Default Audio Device", backend_type="direct")]
        for dev in devices:
            props = dev.get_properties()
            dev_id = props.get_string("device.string") or props.get_string("api.alsa.path") or dev.get_display_name()
            result.append(OutputDevice(id=dev_id, name=dev.get_display_name(), backend_type="direct"))
        return result

    async def set_device(self, device_id: str) -> None:
        from app import database
        stored = await database.get_setting(f"vol:direct:{device_id}")
        self._volume = float(stored) if stored else 0.5
        self._device_id = device_id

    # ── playback ──────────────────────────────────────────────────────────────

    async def play(self, stream_url: str, metadata: Track) -> None:
        self._loop = asyncio.get_running_loop()
        await self._loop.run_in_executor(None, self._sync_play, stream_url)

    def _sync_play(self, stream_url: str) -> None:
        if not _GST_AVAILABLE:
            raise RuntimeError("GStreamer is not available in this environment")
        self._teardown_pipeline()
        pipeline = Gst.ElementFactory.make("playbin", "jukeplox-player")
        pipeline.set_property("uri", stream_url)
        pipeline.set_property("volume", self._volume)

        # Sink selection. On Linux the sound card is a kernel ALSA device;
        # PulseAudio/PipeWire are optional userspace servers on top. The
        # common, research-backed default for headless/NAS containers is to
        # play DIRECTLY to ALSA (`docker run --device /dev/snd`) — no sound
        # server needed. Pulse is the opt-in only when the host already runs a
        # server that owns the card (desktop), where direct ALSA would hit a
        # "device busy" conflict. We pick the sink explicitly rather than let
        # playbin's autoaudiosink decide, because autoaudiosink ranks pulsesink
        # first and would mis-route or stall when no pulse server is reachable.
        if self._device_id and self._device_id != "default":
            # An explicitly chosen ALSA device.
            sink = Gst.ElementFactory.make("alsasink", "audio-sink")
            sink.set_property("device", self._device_id)
            pipeline.set_property("audio-sink", sink)
        elif os.environ.get("PULSE_SERVER"):
            # Opt-in: host already runs PulseAudio/PipeWire and owns the card.
            # Mount the pulse socket + set PULSE_SERVER and we cooperate with
            # the host server instead of fighting it for exclusive ALSA access.
            # pulsesink reads PULSE_SERVER from the env. Missing plugin → fall
            # through to playbin's default sink (fail-soft).
            sink = Gst.ElementFactory.make("pulsesink", "audio-sink")
            if sink is not None:
                pipeline.set_property("audio-sink", sink)
        else:
            # Default: play straight to the host's ALSA card. With
            # `--device /dev/snd` this is all that's needed — no sound server.
            # Explicit alsasink keeps selection deterministic. Missing plugin →
            # leave playbin's default sink (fail-soft).
            sink = Gst.ElementFactory.make("alsasink", "audio-sink")
            if sink is not None:
                pipeline.set_property("audio-sink", sink)

        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::eos", self._on_eos)
        bus.connect("message::error", self._on_error)

        pipeline.set_state(Gst.State.PLAYING)
        self._pipeline = pipeline
        self._is_playing = True

    def _on_eos(self, bus, message) -> None:
        self._is_playing = False
        if self._advance_cb and self._loop:
            asyncio.run_coroutine_threadsafe(self._advance_cb(), self._loop)

    def _on_error(self, bus, message) -> None:
        err, debug = message.parse_error()
        self._is_playing = False
        if self._advance_cb and self._loop:
            asyncio.run_coroutine_threadsafe(self._advance_cb(), self._loop)

    def _teardown_pipeline(self) -> None:
        if self._pipeline:
            bus = self._pipeline.get_bus()
            bus.remove_signal_watch()
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None
        self._is_playing = False

    async def pause(self) -> None:
        if self._pipeline and _GST_AVAILABLE:
            self._pipeline.set_state(Gst.State.PAUSED)
            self._is_playing = False

    async def resume(self) -> None:
        if self._pipeline and _GST_AVAILABLE:
            self._pipeline.set_state(Gst.State.PLAYING)
            self._is_playing = True

    async def stop(self) -> None:
        await asyncio.get_running_loop().run_in_executor(None, self._teardown_pipeline)

    async def set_volume(self, level: float) -> None:
        self._volume = max(0.0, min(1.0, level))
        if self._pipeline:
            self._pipeline.set_property("volume", self._volume)
        from app import database
        await database.set_setting(f"vol:direct:{self._device_id}", str(self._volume))

    async def get_volume(self) -> float:
        return self._volume

    async def get_position(self) -> int:
        if self._pipeline and _GST_AVAILABLE:
            ok, pos = self._pipeline.query_position(Gst.Format.TIME)
            if ok and pos >= 0:
                return pos // 1_000_000  # nanoseconds → ms
        return 0

    async def seek(self, position_ms: int) -> None:
        if self._pipeline and _GST_AVAILABLE:
            self._pipeline.seek_simple(
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                position_ms * 1_000_000,  # ms → nanoseconds
            )

    @property
    def is_playing(self) -> bool:
        return self._is_playing
