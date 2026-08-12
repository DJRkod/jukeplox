"""GStreamer direct-audio output backend.

Requires GStreamer + PyGObject installed in the container (via apt).
On the host machine this file can still be imported; GStreamer is lazy-loaded
only when a DirectAudioBackend is instantiated and play() is called.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Any

from app.output.base import AdvanceCallback, DeviceNotReadyError, OutputDevice
from app.output.radio_endless import (
    RadioFailedHook,
    RadioTitleHook,
    ReconnectPolicy,
    fire_radio_failed_hook,
    is_radio_track,
)
from app.radio.icy import sanitize_title
from app.models import Track

_log = logging.getLogger(__name__)

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


# ── GStreamer bus main loop (2026-07-29 fix U2) ───────────────────────────────
# add_signal_watch() attaches each pipeline's bus to GLib's DEFAULT MainContext,
# but nothing in this asyncio app iterates that context — so the bus message::*
# signals (confirmed-start, EOS advance, error classification, gapless
# STREAM_START) never fire on the Direct backend. Run ONE persistent
# GLib.MainLoop on the default context in a daemon thread to pump it.
#
# Started LAZILY on first play (NOT at import): app.state imports this module at
# import time, so an import-time start would spawn a live thread on any
# GStreamer-capable host merely by importing app.state, and — because the
# gst_mock test fixture patches _GST_AVAILABLE/Gst AFTER import — could not be
# suppressed in unit tests. _sync_play calls ensure_bus_mainloop() before
# add_signal_watch + set_state(PLAYING), so the loop is established before any
# bus message can be posted.
_bus_loop_started = False
_bus_loop_lock = threading.Lock()


def ensure_bus_mainloop() -> None:
    """Idempotently start the default-context GLib main loop on a daemon thread.

    No-op after the first successful start, and a no-op when GStreamer is
    unavailable. Bus callbacks fire on this GLib thread and marshal back to the
    asyncio loop via run_coroutine_threadsafe / session.notify_*_threadsafe, so
    pumping the bus from here is thread-safe."""
    global _bus_loop_started
    if not _GST_AVAILABLE:
        return
    with _bus_loop_lock:
        if _bus_loop_started:
            return
        loop = GLib.MainLoop()
        threading.Thread(
            target=loop.run, daemon=True, name="gst-bus-mainloop"
        ).start()
        _bus_loop_started = True
        _log.info("Started GStreamer bus main loop (default GLib context)")




class DirectAudioBackend:
    """Plays audio via a GStreamer playbin pipeline to a local audio device."""

    def __init__(self, advance_cb: AdvanceCallback | None = None):
        self._device_id: str = "default"
        self._volume: float = 0.5
        self._pipeline = None
        self._is_playing: bool = False
        self._advance_cb = advance_cb
        self._loop: asyncio.AbstractEventLoop | None = None
        # Output-session supervisor dispatch token (2026-07-11 plan U1),
        # captured at play() and cleared when the pipeline's first
        # PLAYING/ASYNC_DONE emits the confirmed-start signal.
        self._confirm_token: int | None = None
        # ── gapless (2026-07-11 plan U7) ──────────────────────────────────
        # U6 arm/revoke contract: the pre-armed next as ONE atomic tuple
        # (stream_url, track). Written on the loop (arm_next/revoke_next),
        # consumed on the GStreamer STREAMING thread by the about-to-finish
        # handler with a capture-to-local-then-null pop — zero asyncio access
        # from that thread. The transcode was warmed by U6's prefetch, so
        # the URI serves instantly when playbin pre-rolls it.
        self._armed_next: tuple[str, Track] | None = None
        # The (url, track) about-to-finish consumed, awaiting its
        # STREAM_START. Doubles as the initial-vs-boundary discriminator:
        # a fresh play()'s first STREAM_START pops None here (the dispatch
        # owns that start), only a consumed arm makes a stream-start a
        # gapless boundary. Cleared by teardown — a fresh dispatch owns the
        # boundary again.
        self._pending_boundary: tuple[str, Track] | None = None
        # Play-generation guard (the plan's stale-STREAM_START scenario):
        # bumped on every pipeline teardown (fresh dispatch or stop), and
        # compared in the loop-side marshaled callback — a boundary from a
        # torn-down/preempted pipeline must never double-advance past the
        # skip's new dispatch.
        self._play_gen: int = 0
        # ── radio endless mode (radio plan U4) ─────────────────────────────
        # Set True in play() when the metadata is a radio pseudo-Track
        # (is_radio_track / duration_ms=0 + sentinel; SG-03 — no play()
        # signature change). While True, BOTH bus advance authorities are
        # suppressed: _on_eos and _on_error (ADV-1) reconnect the live URL via
        # playbin instead of firing advance_cb. Cleared on every finite play().
        self._radio_mode: bool = False
        self._radio_url: str = ""
        # Bounded, sustained-progress-aware reconnect policy (R12/ADV-5).
        self._radio_reconnect = ReconnectPolicy()
        # U7 wires this to a RadioStateEvent "offline" — called (no args) once
        # the reconnect cap is exhausted so a dead station never reads as
        # indefinite silence. None until wired.
        self._radio_failed_hook: RadioFailedHook | None = None
        # ── radio live title (radio plan U6, FREE on Direct) ───────────────
        # GStreamer's playbin/souphttpsrc parse ICY in-band metadata for free
        # and post GST_MESSAGE_TAG on the bus carrying GST_TAG_TITLE — repeatedly
        # (once at start, then on each new StreamTitle). In radio mode we read
        # that tag (newest-wins) and hand it to the session via a callback so the
        # title flows to U7's RadioStateEvent broadcast. No manual ICY parsing on
        # Direct. Cleared on every new station / stop (a stale title must never
        # outlive its station). The title is UNTRUSTED (SEC-004): it is sanitized
        # in the callback before any sink; the WS sink carries it as a plain JSON
        # string the client renders via textContent (U7 contract).
        self._radio_title: str | None = None
        # Called (sanitized title | None) whenever a new StreamTitle arrives on
        # the bus in radio mode. None until U7/session wires it via
        # set_radio_title_hook. Best-effort — a raising hook never breaks the bus.
        self._radio_title_hook: RadioTitleHook | None = None

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

    def set_radio_failed_hook(self, hook: RadioFailedHook | None) -> None:
        """Register (or clear) the endless-mode failed/offline callback (U4).

        Called with no arguments after the reconnect cap is exhausted — U7 wires
        it to a ``RadioStateEvent`` so a dead station surfaces as "offline"
        rather than indefinite silence (R12)."""
        self._radio_failed_hook = hook

    def set_radio_title_hook(self, hook: RadioTitleHook | None) -> None:
        """Register (or clear) the endless-mode live-title callback (U6).

        Called with the newest SANITIZED live title (or ``None``) whenever a new
        ``StreamTitle`` arrives on the GStreamer bus in radio mode — U7 wires it
        to a ``RadioStateEvent`` so the "now playing" line follows the station's
        live title. FREE on Direct: GStreamer parses ICY metadata itself."""
        self._radio_title_hook = hook

    async def play(self, stream_url: str, metadata: Track) -> None:
        self._loop = asyncio.get_running_loop()
        # Radio endless mode (U4, SG-03): carried on the pseudo-Track, not a
        # play() signature change. Set BEFORE the executor spawns the pipeline
        # so the bus handlers (which can fire before run_in_executor returns)
        # already see the mode. A finite track clears it — the radio branch is
        # strictly additive and gated on this flag.
        self._radio_mode = is_radio_track(metadata)
        self._radio_url = stream_url if self._radio_mode else ""
        if self._radio_mode:
            self._radio_reconnect.begin()
            # Clear any prior station's live title (U6): a stale title must never
            # outlive its station. The bus will re-post the new station's title.
            # A reconnect of the SAME station also calls play() and clears here —
            # correct, the fresh pipeline re-emits the current title tag.
            self._set_radio_title(None)
        # Capture the supervisor's per-dispatch token BEFORE the executor
        # spawns the pipeline: the bus can report PLAYING before
        # run_in_executor returns (plan U1).
        from app.output import session
        self._confirm_token = session.get_supervisor().current_token()
        await self._loop.run_in_executor(None, self._sync_play, stream_url)

    def _sync_play(self, stream_url: str) -> None:
        if not _GST_AVAILABLE:
            # Typed device-level error (supervisor plan U2): a missing audio
            # stack must never drain the queue via holder fallback — closes
            # the plain-RuntimeError asymmetry with the Chromecast backend.
            raise DeviceNotReadyError("GStreamer is not available in this environment")
        # Ensure the default-context bus main loop is pumping before we attach a
        # signal watch below (fix U2) — otherwise message::* callbacks never fire.
        ensure_bus_mainloop()
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
            if sink is None:
                # Fail LOUD, not soft: a missing alsasink plugin must surface as
                # a clear device-level error, not fall through to playbin's
                # server-ranked default (which mis-routes / stalls) and then a
                # generic confirm_timeout that points the user at the device.
                raise DeviceNotReadyError(
                    "ALSA sink unavailable — is gstreamer1.0-alsa installed? "
                    f"(cannot open device {self._device_id!r})"
                )
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
            # fail LOUD (device-level error), never fall through to playbin's
            # default sink — autoaudiosink ranks pulsesink first and would
            # mis-route/stall with no pulse server, surfacing as a generic
            # confirm_timeout that wrongly points the user at the device.
            sink = Gst.ElementFactory.make("alsasink", "audio-sink")
            if sink is None:
                raise DeviceNotReadyError(
                    "ALSA sink unavailable — is gstreamer1.0-alsa installed?"
                )
            pipeline.set_property("audio-sink", sink)

        # Gapless (plan U7): about-to-finish is playbin's arm-consumption
        # point — it fires on a streaming thread shortly before the current
        # track drains, and setting the next uri IN-HANDLER is the entire
        # classic-playbin gapless mechanic (GStreamer 1.22; playbin3's rework
        # is 1.24+). Connected unconditionally: with the toggle off nothing
        # is ever armed, the handler no-ops, and behavior stays byte-
        # identical to the per-track path.
        pipeline.connect("about-to-finish", self._on_about_to_finish)

        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::eos", self._on_eos)
        bus.connect("message::error", self._on_error)
        # Radio live title (U6): playbin/souphttpsrc parse ICY in-band metadata
        # for free and post GST_MESSAGE_TAG carrying GST_TAG_TITLE. Connected
        # unconditionally (cheap); the handler no-ops outside radio mode so finite
        # playback is byte-identical.
        bus.connect("message::tag", self._on_tag)
        # Confirmed-start signals (2026-07-11 supervisor plan U1). The
        # pre-existing watch handled eos/error only — playback *start* had no
        # data-plane signal at all.
        bus.connect("message::state-changed", self._on_state_changed)
        bus.connect("message::async-done", self._on_async_done)
        # Gapless transition signal (plan U7): STREAM_START is the audible
        # boundary — position/duration queries flip to the new track there,
        # not at about-to-finish (protocol capability map).
        bus.connect("message::stream-start", self._on_stream_start)

        # Publish the pipeline BEFORE set_state: bus callbacks compare
        # message.src against self._pipeline (and _on_async_done gates on
        # _is_playing), so a confirmed-start racing the state change must
        # already see this pipeline as current.
        self._pipeline = pipeline
        self._is_playing = True
        pipeline.set_state(Gst.State.PLAYING)

    def _on_eos(self, bus, message) -> None:
        if self._radio_mode:
            # Endless mode (U4/AE4): a radio stream must never advance. A clean
            # EOS on a live stream means the upstream closed — reconnect the
            # same URL, never fire advance_cb. (souphttpsrc usually surfaces a
            # drop as an ERROR, not EOS, but a graceful close lands here.)
            _log.info("Radio: bus EOS on endless stream — reconnecting")
            self._radio_reconnect_on_loop()
            return
        if self._pending_boundary is not None:
            # Advance-authority table (plan U7, R16): with an armed next
            # consumed at about-to-finish, STREAM_START is the SOLE advance
            # authority — EOS is suppressed. Classic playbin doesn't post EOS
            # after an in-handler uri swap, so this only guards the
            # pathological case; without it a double authority could form.
            _log.info("Gapless: suppressing EOS — an armed boundary is pending")
            return
        self._is_playing = False
        if self._advance_cb and self._loop:
            asyncio.run_coroutine_threadsafe(self._advance_cb(), self._loop)

    @staticmethod
    def _is_sink_resource_error(message, err) -> bool:
        """Two-class split for a GStreamer bus ERROR (supervisor plan U2,
        R15 KTD): classify by ORIGINATING ELEMENT and error domain, never
        "device-level by definition". Our sink element is explicitly named
        "audio-sink" (and playbin's internal fallbacks carry "sink" in their
        names); RESOURCE is GLib quark ``gst-resource-error-quark``. Only
        sink + RESOURCE means the audio device died; a source/decode-chain
        error is the media's fault and keeps the advance-on-ERROR behavior."""
        src = getattr(message, "src", None)
        try:
            name = (src.get_name() or "") if src is not None else ""
        except Exception:
            name = ""
        domain = str(getattr(err, "domain", "") or "")
        return "sink" in name.lower() and "resource" in domain.lower()

    def _on_error(self, bus, message) -> None:
        err, debug = message.parse_error()
        self._is_playing = False
        # A bus ERROR ends any in-flight gapless boundary: the transition it
        # attributed will never complete audibly (e.g. the armed uri failed
        # to pre-roll), and a lingering pending slot would wrongly suppress a
        # subsequent EOS. The error paths below own what happens next (plan
        # U7 — the two-class split is UNCHANGED in gapless mode).
        self._pending_boundary = None
        if self._radio_mode and not self._is_sink_resource_error(message, err):
            # Endless mode (U4/ADV-1): a live radio drop surfaces HERE as a
            # souphttpsrc RESOURCE/source ERROR, not a clean EOS — and _on_error
            # would normally ADVANCE the (held) queue on the golden-path backend
            # (the party-stall posture). Radio suppresses that: reconnect the
            # same URL instead. A genuine local audio-sink RESOURCE failure
            # (device died) still falls through to the outage path below — a
            # dead sink is a device failure even for radio, and reconnecting the
            # source would not fix it.
            _log.warning("Radio: source ERROR on endless stream (%s) — "
                         "reconnecting, not advancing", err)
            self._radio_reconnect_on_loop()
            return
        if self._is_sink_resource_error(message, err):
            # Device-level: the local audio sink failed (R16 — report, never
            # advance). Same GLib-thread → loop hop as the EOS path.
            _log.warning(
                "GStreamer audio-sink RESOURCE error: %s — reporting "
                "outage-suspected (not advancing)", err,
            )
            from app.output import session
            session.notify_outage_threadsafe(self._loop, "sink_error")
            return
        # Track-level (source/decode chain): skip the dead media — the
        # advance-on-ERROR party-stall posture, preserved by R15.
        if self._advance_cb and self._loop:
            asyncio.run_coroutine_threadsafe(self._advance_cb(), self._loop)

    # ── radio live title from the bus (radio plan U6) ─────────────────────────

    def _on_tag(self, bus, message) -> None:
        """GStreamer bus TAG handler — reads the live ICY ``StreamTitle`` for free.

        Only acts in radio mode: playbin surfaces the station's in-band title as
        ``GST_TAG_TITLE`` on a ``GST_MESSAGE_TAG`` (posted once at start, then on
        each new StreamTitle). We read it newest-wins, sanitize it (SEC-004 —
        untrusted third-party text), and, when it actually changed, hand it to the
        session hook so it flows to U7's broadcast. Fires on a GLib bus thread;
        the hook must be thread-tolerant (U7 marshals to the loop). Never raises
        into the bus. Outside radio mode this is a no-op — finite tracks carry
        their own title and never drive the radio title line."""
        if not self._radio_mode:
            return
        try:
            taglist = message.parse_tag()
            ok, raw = taglist.get_string(Gst.TAG_TITLE)
        except Exception:
            return
        if not ok or not raw:
            return
        title = sanitize_title(raw)
        if title is None or title == self._radio_title:
            return  # empty/blank, or unchanged — no spurious re-broadcast
        self._set_radio_title(title)

    def _set_radio_title(self, title: str | None) -> None:
        """Store the current radio title (newest-wins) and notify the U6 hook.

        Idempotent for an unchanged value (a None-clear on an already-None title
        does not re-notify). The stored value is already sanitized. The hook is
        best-effort — a raising hook is swallowed so the bus thread never dies."""
        if title == self._radio_title:
            return
        self._radio_title = title
        hook = self._radio_title_hook
        if hook is None:
            return
        try:
            hook(title)
        except Exception:  # pragma: no cover - hook is best-effort
            _log.debug("Radio: title hook raised (ignored)", exc_info=True)

    def _on_state_changed(self, bus, message) -> None:
        """Confirmed-start (plan U1): the playbin itself reaching PLAYING means
        decoded audio is flowing to the sink — the data-plane start signal the
        play count keys on. state-changed fires for EVERY element in the bin;
        only the pipeline's own transition counts (message.src filter)."""
        if self._confirm_token is None or message.src is not self._pipeline:
            return
        try:
            _old, new, _pending = message.parse_state_changed()
        except Exception:
            return
        if new == Gst.State.PLAYING:
            self._emit_confirmed_start()

    def _on_async_done(self, bus, message) -> None:
        """ASYNC_DONE = preroll complete: the first buffers reached the audio
        sink. play() drives straight to PLAYING, so while _is_playing this is
        an equivalent (often earlier) confirmed-start signal; while paused
        (a future preroll-then-seek resume) it stays pre-playback (R15)."""
        if self._is_playing:
            self._emit_confirmed_start()

    def _emit_confirmed_start(self) -> None:
        """Marshal the confirmed-start token to the asyncio loop — GStreamer
        bus callbacks fire on GLib threads (same hop as _on_eos). One-shot:
        the token is cleared here so PAUSED→PLAYING resumes can't re-emit."""
        token = self._confirm_token
        if token is None:
            return
        self._confirm_token = None
        from app.output import session
        session.notify_confirmed_threadsafe(self._loop, token)

    # ── radio endless-mode reconnect (radio plan U4) ──────────────────────────

    def _radio_reconnect_on_loop(self) -> None:
        """Marshal a radio reconnect from the GLib bus thread to the asyncio
        loop (same hop as the EOS advance). No-op without a loop."""
        self._is_playing = False
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._radio_reconnect_run(), self._loop)

    async def _radio_reconnect_run(self) -> None:
        """Re-open the live station URL via a fresh playbin (R12/ADV-5).

        Bounded + sustained-progress-aware: only a connection that actually
        played for a while resets the attempt budget, so a station dribbling a
        few bytes per attempt still trips the cap. On exhaustion, surface the
        failed/offline state (U7 hook) instead of retrying forever.

        F5: an ITERATIVE loop, NO self-recursion (a per-attempt recursion grew
        the call stack unboundedly on a station that failed every attempt). The
        failed hook fires exactly once, when the cap is hit."""
        while True:
            if not self._radio_mode:
                return  # a stop / finite dispatch raced us — nothing to reconnect
            if not self._radio_reconnect.should_reconnect():
                _log.warning("Radio: reconnect cap reached — station offline")
                self._radio_notify_failed()
                return
            await asyncio.sleep(self._radio_reconnect.backoff_s())
            if not self._radio_mode:
                return  # stopped while we backed off
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, self._sync_play, self._radio_url)
                # The re-PLAY drove the pipeline; mark the fresh connection so its
                # lifetime is measured against the next drop (sustained-progress).
                self._radio_reconnect.mark_connected()
                _log.info("Radio: reconnected to endless stream (attempt %d)",
                          self._radio_reconnect.attempts)
                return
            except Exception:
                # A failed re-open is itself a drop; loop to consume the next
                # attempt (bounded by the policy) or surface offline at the top.
                _log.warning("Radio: reconnect attempt failed — retrying",
                             exc_info=True)

    def _radio_notify_failed(self) -> None:
        # F13: shared hook-fire + swallow.
        fire_radio_failed_hook(self._radio_failed_hook, _log, "Direct")

    # ── gapless: arm/consume/boundary (2026-07-11 plan U7) ────────────────────

    async def arm_next(self, stream_url: str, track: Track) -> None:
        """Device-side arming (U6 contract): stash the effective next as one
        atomic tuple for the about-to-finish handler. The orchestrator in
        app.state owns WHEN to arm/revoke (toggle, queue edits, Closing Time
        — R21's arm-time check happens there, so a consumed boundary can
        never cross the send-off track); this backend only holds the slot.

        Mixed caps (deferred decision, resolved): same-caps chains — decoded
        PCM at one sample rate — transition truly gapless; a caps change at
        the boundary degrades to a small renegotiation gap, which we accept
        rather than pinning a fixed-rate pipeline (GStreamer 1.22 capability
        map; plan risk table)."""
        self._armed_next = (stream_url, track)

    async def revoke_next(self) -> None:
        """Clear the armed slot (idempotent — U6 also revokes right after a
        boundary consumed the arm, which must no-op). A consumed arm cannot
        be un-consumed: playbin is already pre-rolling the uri, and the
        orchestrator's post-boundary reconcile owns the correction."""
        self._armed_next = None

    def _on_about_to_finish(self, playbin) -> None:
        """Playbin's arm-consumption point (plan U7). Fires on a GStreamer
        STREAMING THREAD: everything here is synchronous and non-blocking —
        no asyncio work, no pipeline state changes, no I/O (protocol
        capability map contract). Pop the armed slot capture-to-local-then-
        null; with a URI armed, setting the ``uri`` property in-handler IS
        the gapless mechanic (playbin pre-rolls the next source and chains
        it without draining the sink). Nothing armed → leave the playbin
        alone → natural EOS → today's gapped advance (both paths converge
        in _do_advance)."""
        armed, self._armed_next = self._armed_next, None
        if armed is None:
            return
        # Record the consumed arm BEFORE the uri swap so the coming
        # STREAM_START can always attribute the transition to it.
        self._pending_boundary = armed
        try:
            playbin.set_property("uri", armed[0])
        except Exception:
            # The swap never took — withdraw the pending boundary so the
            # coming EOS runs the normal gapped advance instead of being
            # suppressed as a boundary that will never happen.
            self._pending_boundary = None
            _log.warning("Gapless: setting the next uri at about-to-finish "
                         "failed — falling back to the gapped advance",
                         exc_info=True)

    def _on_stream_start(self, bus, message) -> None:
        """The audible-transition signal (plan U7): position/duration queries
        flip to the new track HERE, not at about-to-finish. GStreamer-thread
        side. The FIRST stream-start after a fresh play() is the dispatched
        track itself starting — its confirmed-start comes from the
        state-changed/async-done handlers, and ``_pending_boundary`` is None
        then, so popping the empty slot naturally tells the two apart. A
        populated slot means about-to-finish chained the armed next: marshal
        the boundary to the loop carrying the play-generation, compared in
        the marshaled callback (a skip's play()/stop() bumps it — the stale
        boundary of a torn-down pipeline must never double-advance)."""
        src = getattr(message, "src", None)
        if src is not None and src is not self._pipeline:
            return
        # Capture the generation BEFORE popping the pending slot: a teardown
        # interleaving between the two bumps the gen, and a stale boundary
        # marshaled with the FRESH generation would defeat the loop-side
        # double-advance guard.
        play_gen = self._play_gen
        consumed, self._pending_boundary = self._pending_boundary, None
        if consumed is None:
            return
        if self._loop is None:
            return
        _url, track = consumed
        asyncio.run_coroutine_threadsafe(
            self._gapless_boundary(play_gen, track), self._loop)

    async def _gapless_boundary(self, play_gen: int, track: Track) -> None:
        """Loop side of the boundary. The staleness compare happens HERE, on
        the event loop (the plan's generation guard): a skip or stop that
        tore the pipeline down bumped ``_play_gen``, so the boundary its
        pipeline emitted is stale and must not advance past the fresh
        dispatch. A live boundary hands off to the supervisor — STREAM_START
        drives BOTH the queue advance and the confirmed-start chokepoint in
        gapless mode (advance-authority table)."""
        if play_gen != self._play_gen:
            _log.info("Gapless: dropping stale STREAM_START boundary for %r",
                      getattr(track, "title", "?"))
            return
        from app.output import session
        await session.notify_gapless_boundary(track)

    async def probe_liveness(self) -> tuple[bool, str | None]:
        """R15 reachability probe: audio-sink element liveness (plan KTD).

        A zero-timeout ``get_state`` peek at the pipeline — FAILURE means the
        sink could not come up (device gone/busy), ASYNC maps to the
        supervisor's pre-playback extension state, anything else reports the
        pipeline's current state name. No pipeline (or no GStreamer) reads as
        unreachable. Never raises."""
        if not _GST_AVAILABLE:
            return (False, None)
        pipeline = self._pipeline
        if pipeline is None:
            return (False, None)
        try:
            ret, state, _pending = pipeline.get_state(0)
            if ret == Gst.StateChangeReturn.FAILURE:
                return (False, None)
            if ret == Gst.StateChangeReturn.ASYNC:
                return (True, "ASYNC")
            nick = getattr(state, "value_nick", None) or str(state)
            return (True, str(nick).upper())
        except Exception:
            return (False, None)

    def _teardown_pipeline(self) -> None:
        # Any teardown (fresh dispatch via _sync_play, or stop) invalidates
        # the gapless state (plan U7): bump the play-generation so a
        # marshaled STREAM_START from this pipeline drops as stale, and
        # clear the armed/pending slots — a fresh dispatch owns the boundary
        # again (U6 contract: play() clears the armed slot).
        self._play_gen += 1
        self._armed_next = None
        self._pending_boundary = None
        # Claim the pipeline into a local and null the field FIRST, so two
        # concurrent teardowns can't both pass the None-check and then race on
        # a half-torn-down pipeline. Radio's error->reconnect->teardown cycle
        # fires teardown from the GLib bus thread AND the asyncio reconnect loop
        # concurrently; without the claim, one thread nulls self._pipeline
        # between the other's check and its set_state() call (AttributeError:
        # 'NoneType' has no attribute 'set_state' — rig-caught on a radio drop).
        pipe = self._pipeline
        self._pipeline = None
        if pipe is not None:
            bus = pipe.get_bus()
            bus.remove_signal_watch()
            pipe.set_state(Gst.State.NULL)
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
        # End radio endless mode here (not in _teardown_pipeline, which
        # _sync_play also calls mid-reconnect): stop() is the real "station is
        # over" edge, so a reconnect scheduled just before it must not re-open a
        # stopped station.
        self._radio_mode = False
        self._radio_url = ""
        # Clear the live title (U6) so a stopped station's title can't linger.
        self._set_radio_title(None)
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

    async def resume_seek(self, position_ms: int) -> None:
        """Position-resume after an outage re-attach (supervisor plan U3, R7):
        the preroll-then-seek dance — uri already loaded by play(), then
        PAUSED → wait ASYNC_DONE → seek FLUSH|ACCURATE → PLAYING. Seeking a
        playbin before preroll completes fails (GStreamer gapless/seek
        contract, see the protocol capability map), so the plain seek() above
        is not reliable this early in a pipeline's life. Runs in the executor
        because the preroll wait blocks."""
        if not (self._pipeline and _GST_AVAILABLE):
            return
        await asyncio.get_running_loop().run_in_executor(
            None, self._sync_preroll_seek, max(0, position_ms)
        )

    def _sync_preroll_seek(self, position_ms: int) -> None:
        pipeline = self._pipeline
        if pipeline is None:
            return
        pipeline.set_state(Gst.State.PAUSED)
        # get_state blocks until the ASYNC state change completes (the
        # ASYNC_DONE the preroll dance waits on) or the bound expires — a
        # dead sink must not wedge the executor thread forever.
        pipeline.get_state(10 * Gst.SECOND)
        pipeline.seek_simple(
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
            position_ms * 1_000_000,  # ms → nanoseconds
        )
        pipeline.set_state(Gst.State.PLAYING)

    @property
    def is_playing(self) -> bool:
        return self._is_playing
