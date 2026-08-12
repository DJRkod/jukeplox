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
_MDNS_SERVICE_TYPE = "_sendspin._tcp.local."


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

    def _make_feed(self, source: str, headers: dict | None) -> PcmFeed:
        return PcmFeed(source, headers, sink="pipe:1", consume_stdout=True,
                       realtime=False, label="sendspin-feed")

    def _pairing_store_path(self) -> str:
        from app import database
        from pathlib import Path
        return str(Path(database.settings.data_dir) / "sendspin_pairing.json")

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
                host=host, port=self._port,
                pairing_store_path=self._pairing_store_path())
            self._adapter.add_event_listener(self._on_event)
            await self._advertise_mdns(host)
            self._connected = True
            # Persist the current PSK SEALED (Fernet) for admin-display continuity
            # across restarts — never plaintext at rest (U2/R24). Best-effort.
            try:
                from app import database
                key = self._adapter.pairing_key
                if key:
                    await database.set_sealed_setting("sendspin_pairing_psk", key)
            except Exception:
                _log.debug("sendspin: sealing pairing key failed", exc_info=True)
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
            info = ServiceInfo(
                _MDNS_SERVICE_TYPE,
                f"jukeplox.{_MDNS_SERVICE_TYPE}",
                addresses=[socket.inet_aton(host)],
                port=self._port,
                properties={b"src": b"jukeplox"},
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
        await self._adapter.start_stream()
        feed = self._feed_factory(source, headers)
        self._feed = feed
        await feed.start()
        self._playback_started_at = time.monotonic() - (start_offset_ms / 1000)
        self._paused_at = None
        self._is_playing = True
        # Confirmed-start proxy: the feed is up and about to push (a 0-client
        # server is audibly silent but not an outage — R15).
        self._confirm_started()
        self._feed_task = asyncio.get_running_loop().create_task(
            self._feed_loop(gen, feed))

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
                await self._adapter.prepare_audio(chunk)
                await self._adapter.commit_audio()
                # BOUND: never push faster than the client buffer drains. The
                # wait_for ceiling converts a library that never releases (e.g.
                # on zero clients) into a bounded, recoverable outage instead of
                # an unbounded hang.
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

    async def seek(self, position_ms: int) -> None:
        if self._current_track is None:
            return
        resolved = await _default_resolve_source(self._current_track)
        if resolved is None:
            return
        source, headers = resolved
        await self._teardown_feed(keep_playing_flag=True)
        await self._spawn_feed(source, headers, max(0, position_ms))

    async def _teardown_feed(self, *, keep_playing_flag: bool = False) -> None:
        self._feed_gen += 1
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
        clients = self._adapter.clients()
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
        # Sendspin's single in-process group of all connected clients.
        return [{
            "group_id": "sendspin",
            "muted": False,
            "clients": [
                {"client_id": c["id"], "name": c["name"],
                 "volume": c["volume"], "muted": c["muted"]}
                for c in self._adapter.clients()
            ],
        }]

    async def set_client_volume(self, client_id: str, level: float) -> None:
        self._stamp_volume_write()
        await self._adapter.set_client_volume(client_id, max(0.0, min(1.0, level)))

    async def set_client_mute(self, client_id: str, muted: bool) -> None:
        await self._adapter.set_client_mute(client_id, muted)

    async def set_group_mute(self, group_id: str, muted: bool) -> None:
        for c in self._adapter.clients():
            try:
                await self._adapter.set_client_mute(c["id"], muted)
            except Exception:
                _log.warning("sendspin: group-mute fan-out failed for a client",
                             exc_info=True)

    async def set_group_volume(self, group_id: str, level: float) -> None:
        clients = self._adapter.clients()
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

    # ── pairing (U8) ─────────────────────────────────────────────────────────

    async def get_pairing_pin(self) -> str:
        """A fresh short-lived pairing PIN/token — the PREFERRED flow over
        displaying the raw long-term PSK. Admin-only (the caller gates)."""
        if self._adapter is None:
            raise DeviceNotReadyError("Sendspin is not enabled")
        return await self._adapter.initiate_pairing()

    def pairing_key(self) -> str:
        """The long-term PSK, plaintext — served ONLY to an authenticated admin
        (the endpoint gates + never logs it, never sends it sealed)."""
        return self._adapter.pairing_key if self._adapter is not None else ""

    async def rotate_pairing(self) -> None:
        """Regenerate the long-term PSK (disconnects all paired clients) and
        persist it SEALED (Fernet) — never plaintext at rest, never logged."""
        if self._adapter is None:
            raise DeviceNotReadyError("Sendspin is not enabled")
        new_key = await self._adapter.rotate_pairing()
        from app import database
        await database.set_sealed_setting("sendspin_pairing_psk", new_key)
