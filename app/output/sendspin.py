"""Sendspin output backend (2026-08-11 plan U7/U8) — EXPERIMENTAL.

A server-fed ``AbstractOutputBackend`` on the U1 ``MultiroomBackendBase``, mirror
of Snapcast but with an IN-PROCESS server: jukeplox runs an ``aiosendspin``
``SendspinServer`` on an explicit LAN host (never blind ``0.0.0.0``), advertises
it via the shared ``AsyncZeroconf``, and pushes the single queue's PCM into it
with a ``prepare_audio``/``commit_audio`` loop paced by ``sleep_to_limit_buffer``
backpressure (the bound that makes an endless-stream hang impossible — the
radio-mode P0 class).

All ``aiosendspin`` surface is isolated behind ``sendspin_adapter.SendspinAdapter``
(pinned exact version). The feed is ffmpeg → PCM pipe → Python → adapter push;
unlike Snapcast there is no ``tcp://`` sink and no device-reachable
``STREAM_BASE_URL`` (clients connect to the in-process server directly). Track
boundaries + advance are the queue clock (U1): clean feed EOF advances, mid-track
feed death holds (bounded outage). Labelled EXPERIMENTAL; dormant unless enabled
(PyAV/aiosendspin imported only in the enable path, R16)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from app.output.base import DeviceNotReadyError, OutputDevice
from app.output.flow import _default_resolve_source
from app.output.multiroom import (
    FEED_BYTES_PER_SECOND,
    FeedStalled,
    MultiroomBackendBase,
    PcmFeed,
)
from app.output import sendspin_adapter
from app.models import Track

_log = logging.getLogger(__name__)

# ~100 ms PCM slices (Music Assistant's Sendspin feed granularity).
_FEED_SLICE_BYTES = int(FEED_BYTES_PER_SECOND * 0.1)
# Ceiling on the library backpressure wait. If sleep_to_limit_buffer doesn't
# unblock within this (e.g. the pinned aiosendspin doesn't release on zero
# clients), the feed is treated as stalled → outage-hold, never an unbounded
# hang (the belt to the first-byte watchdog's suspenders).
_BUFFER_WAIT_TIMEOUT_S = 5.0
# Cover art is bound for a speaker's screen, not a wall display — a full-size
# image would be wasted bytes over the wire and wasted decode on an ESP32.
_ART_WIDTH = 640
# Ceiling on any speaker-bound write that sits on the playback dispatch path.
_SPEAKER_WRITE_TIMEOUT_S = 5.0
# The SERVER service type. Advertising the client type here (as this backend
# originally did) makes jukeplox invisible to anything hunting for a Sendspin
# server — see the discovery note in sendspin_adapter.
_MDNS_SERVICE_TYPE = sendspin_adapter.SERVER_SERVICE_TYPE


class SendspinBackend(MultiroomBackendBase):
    #: Surfaced to the admin UI (U9): Sendspin is a technical preview.
    experimental = True

    def __init__(
        self,
        advance_cb: Callable[[], Awaitable[Any]] | None = None,
        *,
        adapter_factory: Callable[..., Awaitable[Any]] | None = None,
        feed_factory: Callable[..., PcmFeed] | None = None,
        host_resolver: Callable[[], str] | None = None,
        port: int = sendspin_adapter.DEFAULT_PORT,
    ) -> None:
        super().__init__(advance_cb=advance_cb)
        self._adapter_factory = adapter_factory or sendspin_adapter.default_adapter_factory
        self._feed_factory = feed_factory or self._make_feed
        self._host_resolver = host_resolver or self._resolve_lan_host
        self._port = port

        self._adapter: Any = None
        self._connected = False
        self._advertised = False
        self._bind_host = ""

        self._feed: PcmFeed | None = None
        self._feed_task: asyncio.Task | None = None
        self._current_track: Track | None = None
        self._feed_gen = 0
        self._playback_started_at: float | None = None
        self._paused_at: float | None = None
        self._zones_changed_hook: Callable[[], Any] | None = None
        self._art_task: asyncio.Task | None = None
        self._resync_task: asyncio.Task | None = None

    # ── helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_lan_host() -> str:
        """An explicit routable LAN IP — NEVER ``0.0.0.0`` under host networking.
        Reuses the app's existing LAN detection."""
        from app import state
        try:
            return state._detect_primary_lan_ip()
        except Exception:
            # No route yet — fall back to the configured bind host if it is a
            # specific address; a blind 0.0.0.0 is refused by the enable path.
            from app.config import settings
            bh = settings.bind_host
            return bh if bh and bh != "0.0.0.0" else ""

    def _make_feed(self, source: str, headers: dict | None,
                   start_offset_ms: int = 0) -> PcmFeed:
        # The offset MUST reach ffmpeg (-ss). Without it, resume-after-pause and
        # seek both restart the track from 0:00 while the progress clock and the
        # speaker screens read the offset and keep climbing.
        return PcmFeed(source, headers, sink="pipe:1", consume_stdout=True,
                       realtime=False, label="sendspin-feed",
                       start_offset_ms=max(0, int(start_offset_ms)))

    def set_zones_changed_hook(self, cb: Callable[[], Any] | None) -> None:
        self._zones_changed_hook = cb

    # ── enable / disable (U2 activation contract) ───────────────────────────

    async def enable(self) -> None:
        if self._connected:
            return  # already running — a double enable must not rebind 8927
        host = self._host_resolver()
        if not host or host == "0.0.0.0":
            raise DeviceNotReadyError(
                "cannot resolve a routable LAN address for the Sendspin server — "
                "set BIND_HOST/STREAM_BASE_URL to this server's LAN IP")
        self._bind_host = host
        try:
            self._adapter = await self._adapter_factory(
                host=host, port=self._port)
            self._adapter.add_event_listener(self._on_event)
            self._adapter.set_transport_handler(self.handle_transport)
            await self._advertise_mdns(host)
            self._connected = True
            # No long-term server PSK is persisted here. This protocol has no
            # such thing: trust is per-speaker pairing records, and those are
            # sealed by the pairing store itself.
        except BaseException:
            await self._safe_teardown()
            raise

    async def disable(self) -> None:
        await self.stop()
        await self._safe_teardown()

    async def _safe_teardown(self) -> None:
        self._connected = False
        await self._unadvertise_mdns()
        if self._adapter is not None:
            try:
                await self._adapter.stop()  # releases the 8927 listener
            except Exception:
                _log.warning("sendspin: server stop failed", exc_info=True)
            self._adapter = None

    def _on_event(self, *args: Any) -> None:
        """A Sendspin client/volume/group event. Echo-guarded like Snapcast so a
        server-initiated volume write doesn't bounce back to admin sliders."""
        # A speaker may have just arrived, so bring it up to date. Advertising
        # controls at enable() cannot work — there is no group until a client
        # connects — and the protocol drops any command it was not told about,
        # so without this an idle speaker's Play button does nothing at all.
        self._spawn_client_resync()
        if self._echo_guard_active():
            return
        hook = self._zones_changed_hook
        if hook is None:
            return
        try:
            res = hook()
            if asyncio.iscoroutine(res):
                asyncio.get_running_loop().create_task(res)
        except RuntimeError:
            pass
        except Exception:
            _log.warning("sendspin: zones-changed hook failed", exc_info=True)

    # ── mDNS advertisement (shared AsyncZeroconf) ───────────────────────────

    async def _advertise_mdns(self, host: str) -> None:
        """Advertise the in-process server via the ONE shared AsyncZeroconf
        (the DACP publish precedent) — never a second Zeroconf bind. Best-effort:
        no shared stack (bind failed / D-Bus-only host) just means no mDNS ad."""
        from app import state
        aiozc = getattr(state, "shared_aiozc", None)
        if aiozc is None:
            return
        try:
            import socket
            from zeroconf import ServiceInfo
            # TXT `path` is how a client learns the WebSocket endpoint without
            # guessing; `name` is the human label it shows. Both are spec TXT keys.
            path = self._adapter.api_path if self._adapter is not None else "/"
            info = ServiceInfo(
                _MDNS_SERVICE_TYPE,
                f"jukeplox.{_MDNS_SERVICE_TYPE}",
                addresses=[socket.inet_aton(host)],
                port=self._port,
                properties={
                    b"src": b"jukeplox",
                    b"path": path.encode(),
                    b"name": b"jukeplox",
                },
            )
            await aiozc.async_register_service(info)
            self._mdns_info = info
            self._advertised = True
        except Exception:
            _log.warning("sendspin: mDNS advertise failed (non-fatal)", exc_info=True)

    async def _unadvertise_mdns(self) -> None:
        if not self._advertised:
            return
        self._advertised = False
        from app import state
        aiozc = getattr(state, "shared_aiozc", None)
        info = getattr(self, "_mdns_info", None)
        if aiozc is not None and info is not None:
            try:
                await aiozc.async_unregister_service(info)
            except Exception:
                _log.debug("sendspin: mDNS unregister failed", exc_info=True)

    # ── playback ─────────────────────────────────────────────────────────────

    async def play(self, stream_url: str, metadata: Track, *,
                   start_offset_ms: int = 0) -> None:
        if not self._connected or self._adapter is None:
            raise DeviceNotReadyError("Sendspin backend is not enabled/connected")
        self._capture_confirm_token()
        await self._teardown_feed()
        resolved = await _default_resolve_source(metadata)
        if resolved is None:
            raise DeviceNotReadyError("could not resolve the track source")
        source, headers = resolved
        self._current_track = metadata
        await self._spawn_feed(source, headers, start_offset_ms)

    async def _spawn_feed(self, source: str, headers: dict | None,
                          start_offset_ms: int) -> None:
        self._feed_gen += 1
        gen = self._feed_gen
        # Bounded: this runs on the dispatch path while state._advance_lock is
        # held, so a wedged speaker here would freeze the whole queue engine —
        # not just Sendspin. On timeout we continue with no stream; the feed
        # paces itself and reconciles when the speaker recovers.
        try:
            await asyncio.wait_for(self._adapter.start_stream(),
                                   _SPEAKER_WRITE_TIMEOUT_S)
        except asyncio.TimeoutError:
            _log.warning("sendspin: opening the speaker stream timed out — "
                         "continuing unpushed until it recovers")
        feed = self._feed_factory(source, headers, start_offset_ms)
        self._feed = feed
        await feed.start()
        self._playback_started_at = time.monotonic() - (start_offset_ms / 1000)
        self._paused_at = None
        self._is_playing = True
        # Confirmed-start proxy: the feed is up and about to push (a 0-client
        # server is audibly silent but not an outage — R15).
        self._confirm_started()
        # Start the reader FIRST. Everything below talks to speakers over the
        # network, and this runs on the dispatch path that router.play() awaits
        # — a wedged write to one speaker would otherwise hang every subsequent
        # track dispatch app-wide (natural advance, Skip, Previous, seek), with
        # confirmation already fired so the supervisor's deadline cannot save us.
        self._feed_task = asyncio.get_running_loop().create_task(
            self._feed_loop(gen, feed))
        if self._current_track is not None:
            self._spawn_artwork(self._current_track, gen)
            # Bounded for the same reason: a slow speaker delays its own screen,
            # never the music.
            try:
                await asyncio.wait_for(
                    self._push_track_state(self._current_track,
                                           start_offset_ms, gen),
                    _SPEAKER_WRITE_TIMEOUT_S)
            except asyncio.TimeoutError:
                _log.warning("sendspin: speaker state push timed out — audio "
                             "continues, screens may lag")

    async def _push_track_state(self, track: Track, offset_ms: int,
                                gen: int) -> None:
        # Generation-guarded like the artwork task: the reader task is already
        # running by now, so a slow push here could otherwise land after the
        # track changed — un-freezing a paused screen, or re-arming the previous
        # track's seek ceiling.
        if gen != self._feed_gen:
            return
        await self._push_now_playing(track, offset_ms)
        if gen != self._feed_gen:
            return
        # Seek is DISCARDED by the protocol unless a ceiling is set, so the
        # ceiling is refreshed per track (the command set itself is advertised
        # at enable, so an idle speaker can start playback).
        try:
            await self._adapter.configure_controls(
                seek_max_ms=int(getattr(track, "duration_ms", 0) or 0) or None)
        except Exception:
            _log.warning("sendspin: advertising controls failed", exc_info=True)

    async def _feed_loop(self, gen: int, feed: PcmFeed) -> None:
        """Bounded push loop: read a PCM slice → prepare/commit → yield to
        ``sleep_to_limit_buffer`` (the backpressure that BOUNDS the loop). A
        clean EOF advances on a FRESH task (never inline — inline re-enters
        play() and cancels this task); a stall/death holds via the supervisor's
        outage path (never a detached ``raise`` — that exception is unobserved)."""
        try:
            while True:
                try:
                    chunk = await feed.read(_FEED_SLICE_BYTES)
                except FeedStalled:
                    self._hold_outage("sendspin_feed_stalled", gen)
                    return
                if gen != self._feed_gen or not self._is_playing:
                    return  # superseded / stopped
                if not chunk:
                    # Clean EOF at track end → advance (the queue is the clock).
                    self._is_playing = False
                    self._spawn_advance()
                    return
                if not self._adapter.has_stream():
                    # NO SPEAKERS. The feed is built without real-time throttling
                    # because sleep_to_limit_buffer normally paces it — so with
                    # no push sink there is nothing holding this loop back, and
                    # it would read-and-discard a whole track in seconds, advance,
                    # and drain the entire queue while recording every track as
                    # played. Pace it against the wall clock instead: silent, but
                    # still playing at the speed of sound.
                    await asyncio.sleep(len(chunk) / FEED_BYTES_PER_SECOND)
                    # Keep the stream in step with the room: attach when a
                    # speaker arrives mid-track, tear down when the last leaves.
                    await self._adapter.reconcile_stream()
                    continue
                await self._adapter.prepare_audio(chunk)
                await self._adapter.commit_audio()
                # BOUND: never push faster than the client buffer drains. The
                # wait_for ceiling converts a library that never releases into a
                # bounded, recoverable outage instead of an unbounded hang.
                try:
                    await asyncio.wait_for(self._adapter.sleep_to_limit_buffer(),
                                           _BUFFER_WAIT_TIMEOUT_S)
                except asyncio.TimeoutError:
                    self._hold_outage("sendspin_buffer_stall", gen)
                    return
        except asyncio.CancelledError:
            return
        except Exception:
            _log.warning("sendspin: feed loop crashed", exc_info=True)
            self._hold_outage("sendspin_feed_error", gen)

    def _spawn_client_resync(self) -> None:
        """Bring a newly-connected speaker up to date: advertise the commands it
        may send, and re-send what is playing so its screen is not blank."""
        if self._adapter is None or self._resync_task is not None:
            return

        async def _run() -> None:
            try:
                track = self._current_track
                await self._adapter.configure_controls(
                    seek_max_ms=(int(getattr(track, "duration_ms", 0) or 0) or None)
                    if track is not None else None)
                if track is not None:
                    await self._push_now_playing(track, self.get_position_sync())
            except Exception:
                _log.debug("sendspin: resyncing a new speaker failed",
                           exc_info=True)
            finally:
                self._resync_task = None

        try:
            self._resync_task = asyncio.get_running_loop().create_task(_run())
        except RuntimeError:
            self._resync_task = None

    # ── now playing: title, artist, album and cover on speaker screens ───────

    async def _push_now_playing(self, track: Track, progress_ms: int) -> None:
        """An anchor, not a tick — the client extrapolates progress from here."""
        if self._adapter is None:
            return
        try:
            await self._adapter.set_now_playing(
                title=getattr(track, "title", "") or "",
                artist=getattr(track, "artist", "") or "",
                album=getattr(track, "album", "") or "",
                album_artist=getattr(track, "album_artist", "") or "",
                duration_ms=int(getattr(track, "duration_ms", 0) or 0),
                progress_ms=max(0, int(progress_ms)),
            )
        except Exception:
            _log.warning("sendspin: pushing now-playing failed", exc_info=True)

    def _spawn_artwork(self, track: Track, gen: int) -> None:
        """Fetch and push cover art OFF the playback path.

        Two things this must never do: delay audio while an image downloads, and
        paint a cover for a track we have already moved past. A track with no
        art clears the screen rather than leaving the previous cover up."""
        async def _run() -> None:
            data: bytes | None = None
            thumb = getattr(track, "thumb", None)
            if thumb:
                try:
                    from app import state
                    client = await state.get_plex_client()
                    if client is not None:
                        data, _ctype = await client.fetch_art(thumb, width=_ART_WIDTH)
                except Exception:
                    _log.debug("sendspin: album art fetch failed", exc_info=True)
                    data = None
            if gen != self._feed_gen or self._adapter is None:
                return  # superseded — never paint a stale cover
            try:
                await self._adapter.set_album_artwork(data)
            except Exception:
                _log.warning("sendspin: pushing album art failed", exc_info=True)

        self._art_task = asyncio.get_running_loop().create_task(_run())

    def _cancel_artwork(self) -> None:
        task, self._art_task = self._art_task, None
        if task is not None and not task.done():
            task.cancel()

    def _hold_outage(self, reason: str, gen: int) -> None:
        """Route a mid-track feed failure to the supervisor's outage-hold (hold,
        don't drain, don't dead-stall). No-op if superseded."""
        if gen != self._feed_gen:
            return
        self._is_playing = False
        _log.warning("sendspin: feed failed mid-track (%s) — holding (outage)",
                     reason)
        self._notify_feed_outage(reason)

    async def pause(self) -> None:
        if self._paused_at is None and self._is_playing:
            self._paused_at = time.monotonic()
        await self._teardown_feed(keep_playing_flag=True)
        # Stop the screens counting up. The track stays on display.
        if self._adapter is not None:
            try:
                await self._adapter.freeze_progress()
            except Exception:
                _log.warning("sendspin: freezing progress failed", exc_info=True)

    async def resume(self) -> None:
        if self._current_track is None or self._paused_at is None:
            return
        resolved = await _default_resolve_source(self._current_track)
        if resolved is None:
            return
        source, headers = resolved
        held_ms = self.get_position_sync()
        self._paused_at = None
        await self._spawn_feed(source, headers, held_ms)

    async def stop(self) -> None:
        self._is_playing = False
        self._current_track = None
        self._paused_at = None
        await self._teardown_feed()
        if self._adapter is not None:
            try:
                await self._adapter.clear_now_playing()  # blanks the screens too
            except Exception:
                _log.warning("sendspin: clearing now-playing failed", exc_info=True)

    async def seek(self, position_ms: int) -> None:
        if self._current_track is None:
            return
        if self._paused_at is not None:
            # Seeking while PAUSED must not start playing. Re-anchor the
            # position and leave the feed down, or the backend would run while
            # the queue engine still believes it is paused.
            target = max(0, position_ms)
            self._playback_started_at = time.monotonic() - (target / 1000)
            self._paused_at = time.monotonic()
            await self._push_now_playing(self._current_track, target)
            if self._adapter is not None:
                try:
                    await self._adapter.freeze_progress()
                except Exception:
                    _log.debug("sendspin: re-freezing after a paused seek failed",
                               exc_info=True)
            return
        resolved = await _default_resolve_source(self._current_track)
        if resolved is None:
            return
        source, headers = resolved
        await self._teardown_feed(keep_playing_flag=True)
        await self._spawn_feed(source, headers, max(0, position_ms))

    async def _teardown_feed(self, *, keep_playing_flag: bool = False) -> None:
        self._feed_gen += 1
        self._cancel_artwork()
        if not keep_playing_flag:
            self._is_playing = False
        task, feed = self._feed_task, self._feed
        self._feed_task, self._feed = None, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if feed is not None:
            try:
                await feed.close()
            except Exception:
                _log.warning("sendspin: feed close failed", exc_info=True)

    # ── position / volume ────────────────────────────────────────────────────

    def get_position_sync(self) -> int:
        if self._playback_started_at is None:
            return 0
        end = self._paused_at if self._paused_at is not None else time.monotonic()
        return max(0, int((end - self._playback_started_at) * 1000))

    async def get_position(self) -> int:
        return self.get_position_sync()

    async def set_volume(self, level: float) -> None:
        level = max(0.0, min(1.0, level))
        self._volume = level
        if not self._connected or self._adapter is None:
            return
        # Speakers whose level could not be read are EXCLUDED, not treated as
        # zero: feeding an unknown in as 0.0 makes redistribute preserve that
        # zero and write it back, silencing a speaker that was playing fine.
        clients = [c for c in self._adapter.clients() if c["volume"] is not None]
        if not clients:
            return
        current = [c["volume"] for c in clients]
        targets = self.redistribute(current, level, lo=0.0, hi=1.0)
        self._stamp_volume_write()
        for c, tgt in zip(clients, targets):
            try:
                await self._adapter.set_client_volume(c["id"], tgt)
            except Exception:
                _log.warning("sendspin: client volume set failed", exc_info=True)

    # ── device enumeration ────────────────────────────────────────────────────

    async def discover_devices(self) -> list[OutputDevice]:
        if not self._connected or self._adapter is None:
            return []
        return [
            OutputDevice(id=c["id"], name=c["name"], backend_type="sendspin",
                         id_format="uuid")
            for c in self._adapter.clients()
        ]

    async def set_device(self, device_id: str) -> None:
        return None  # server-fed: no per-device selection (zoning owns targeting)

    # ── transport commands FROM a speaker ────────────────────────────────────

    async def handle_transport(self, action: str, value: Any = None) -> None:
        """Apply a command a paired speaker sent.

        Two deliberate choices, both easy to mistake for oversights:

        1. This routes into the SAME functions the web UI's transport calls, not
           a parallel implementation. Skip in particular carries careful
           outage-hold behaviour; a second copy would drift from it.
        2. The guest permission toggles are NOT consulted. Pairing IS the
           authorisation for a Sendspin speaker — a deliberate product decision,
           accepted knowing a paired kitchen speaker therefore outranks a guest
           holding a phone. Do not "fix" this by adding a guest check.
        """
        if not self._connected:
            return
        from app import playback_control
        try:
            if action == "play":
                await playback_control.playback_resume()
            elif action == "pause":
                await playback_control.playback_pause()
            elif action == "next":
                await playback_control.playback_skip()
            elif action == "previous":
                await playback_control.playback_previous()
            elif action in ("seek", "seek_relative"):
                await self._handle_seek(action, value)
            elif action == "volume":
                # This arrives from the CONTROLLER role, which is group-scoped
                # in the protocol — so it maps to master volume, which then
                # fans out proportionally, rather than to the sending speaker
                # alone. Someone turning the knob on one speaker is asking the
                # room to get louder, not just that box.
                self._stamp_volume_write()   # don't let the fan-out echo back
                await playback_control.playback_volume(
                    max(0.0, min(1.0, float(value or 0) / 100.0)))
            elif action == "mute":
                self._stamp_volume_write()
                await self.set_group_mute("sendspin", bool(value))
        except Exception:
            _log.warning("sendspin: transport command %r failed", action,
                         exc_info=True)

    async def _handle_seek(self, action: str, value: Any) -> None:
        if self._current_track is None:
            return  # nothing playing — a stale command must not resurrect a track
        target = int(value or 0)
        if action == "seek_relative":
            target = self.get_position_sync() + target
        duration = int(getattr(self._current_track, "duration_ms", 0) or 0)
        target = max(0, target)
        if duration:
            target = min(target, duration)
        from app import playback_control
        await playback_control.playback_seek(target)

    # ── discovery (both spec directions) ─────────────────────────────────────

    async def discovered_speakers(self) -> list[dict]:
        """Every speaker the server knows about, paired or not — the PAIRING
        surface. Distinct from ``discover_devices``/``list_zones``, which only
        report connected clients (the zoning surface): you pair with something
        visible-but-untrusted, and you zone something already connected."""
        if not self._connected or self._adapter is None:
            return []
        return self._adapter.discovered_clients()

    async def connect_speaker(self, url: str) -> None:
        """Dial OUT to a speaker that advertised itself. The library's own
        discovery does this automatically; this is the manual escape hatch for a
        speaker mDNS never surfaced (different subnet, flaky multicast).

        The URL is operator-supplied, so it goes through the same fail-closed
        host policy every other outbound target in this codebase does."""
        if not self._connected or self._adapter is None:
            raise DeviceNotReadyError("Sendspin is not enabled")
        await self._validate_speaker_url(url)
        await self._adapter.connect_to_client(url)

    @staticmethod
    async def _validate_speaker_url(url: str) -> None:
        """Scheme + host check for an operator-supplied speaker URL.

        Reuses Snapcast's backend-local validator rather than adding a third
        copy of the policy: loopback and link-local are refused outright, and
        private ranges unless ALLOW_PRIVATE_SOURCES is set. Living in the output
        layer keeps the backend clear of app/api."""
        from urllib.parse import urlparse
        from app.output.snapcast import _validate_external_host

        try:
            parsed = urlparse(url or "")
        except Exception as exc:
            raise ValueError("that speaker address is not a valid URL") from exc
        if parsed.scheme not in ("ws", "wss"):
            raise ValueError("a speaker address must start with ws:// or wss://")
        if not parsed.hostname:
            raise ValueError("that speaker address has no host")
        try:
            await _validate_external_host(parsed.hostname)
        except DeviceNotReadyError as exc:
            # Surface as a 400 to the operator rather than a device-state error.
            raise ValueError(str(exc)) from exc

    # ── zoning contract (U1) — pairing/zoning hardened in U8 ────────────────

    def supports_zoning(self) -> bool:
        return self._connected

    def can_manage_topology(self) -> bool:
        # In-process server we own → full management (U8 refines the group model
        # to whatever the pinned aiosendspin exposes).
        return True

    async def list_zones(self) -> list[dict]:
        if not self._connected or self._adapter is None:
            return []
        # Sendspin's single in-process group of all connected clients. The group
        # reads as muted only when every speaker in it is.
        clients = self._adapter.clients()
        return [{
            "group_id": "sendspin",
            "name": "Sendspin",
            "muted": bool(clients) and all(c.get("muted") for c in clients),
            "clients": [
                {"client_id": c["id"], "name": c["name"],
                 "volume": c["volume"] if c["volume"] is not None else 0.0,
                 "muted": c["muted"],
                 "delay_ms": c.get("delay_ms", 0)}
                for c in clients
            ],
        }]

    async def set_client_volume(self, client_id: str, level: float) -> None:
        self._stamp_volume_write()
        await self._adapter.set_client_volume(client_id, max(0.0, min(1.0, level)))

    async def set_client_mute(self, client_id: str, muted: bool) -> None:
        await self._adapter.set_client_mute(client_id, muted)

    async def set_client_delay(self, client_id: str, delay_ms: int) -> None:
        """Per-room latency trim. Deliberately OUTSIDE the shared zoning
        contract for now: adding it there would oblige Snapcast to implement it
        too, and that call is better made once this surface has settled."""
        if self._adapter is None:
            raise DeviceNotReadyError("Sendspin is not enabled")
        await self._adapter.set_client_delay(client_id, max(0, int(delay_ms)))

    async def set_group_mute(self, group_id: str, muted: bool) -> None:
        for c in self._adapter.clients():
            try:
                await self._adapter.set_client_mute(c["id"], muted)
            except Exception:
                _log.warning("sendspin: group-mute fan-out failed for a client",
                             exc_info=True)

    async def set_group_volume(self, group_id: str, level: float) -> None:
        clients = [c for c in self._adapter.clients() if c["volume"] is not None]
        if not clients:
            return
        current = [c["volume"] for c in clients]
        targets = self.redistribute(current, max(0.0, min(1.0, level)), lo=0.0, hi=1.0)
        self._stamp_volume_write()
        for c, tgt in zip(clients, targets):
            try:
                await self._adapter.set_client_volume(c["id"], tgt)
            except Exception:
                _log.warning("sendspin: group-volume fan-out failed for a client",
                             exc_info=True)

    async def assign_client_to_group(self, client_id: str, group_id: str) -> None:
        # Sendspin exposes a single in-process group in this scope; explicit
        # multi-group topology is a follow-up (see plan Scope Boundaries).
        return None

    # ── pairing ──────────────────────────────────────────────────────────────
    #
    # Pairing is the ONLY authority boundary for a Sendspin speaker: a paired
    # speaker gets full transport control (see handle_command). That makes this
    # surface — and especially unpair — the security boundary, not a convenience.

    async def pair_speaker(self, *, method: str, code: str,
                           client_id: str = "") -> str:
        """Pair with one speaker using the code read off that speaker."""
        if not self._connected or self._adapter is None:
            raise DeviceNotReadyError("Sendspin is not enabled")
        return await self._adapter.begin_pairing(
            method=method, code=code, client_id=client_id)

    async def cancel_pairing(self, client_id: str) -> None:
        """Abandon an attempt early. Also withdraws any standing
        trusted-unpaired grant, so a cancelled pairing leaves nothing behind."""
        if self._adapter is None:
            return
        await self._adapter.end_pairing(client_id)
        await self._adapter.revoke_unpaired(client_id)

    async def paired_speakers(self) -> list[dict]:
        if not self._connected or self._adapter is None:
            return []
        return await self._adapter.paired_clients()

    async def unpair_speaker(self, client_id: str) -> None:
        """Revoke a speaker: drops its stored pairing AND its live session."""
        if not self._connected or self._adapter is None:
            raise DeviceNotReadyError("Sendspin is not enabled")
        await self._adapter.unpair(client_id)
