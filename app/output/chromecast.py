"""Chromecast (Google Cast) output backend via pychromecast.

Device discovery uses pychromecast's CastBrowser + SimpleCastListener for
persistent mDNS listening instead of a one-shot scan.  This avoids the
intermittent-miss problem with devices that announce late in the mDNS window.

# Persistent-listener approach patterned after Music Assistant (MIT License):
# https://github.com/music-assistant/server — providers/chromecast/provider.py
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import re
import threading
import time
from typing import Any
from uuid import UUID

# Avahi advertises Chromecast service instances as
# `<dashed-friendly-slug>-<32-hex-cast-uuid>` (e.g.
# `JBL-Charge-5-Wi-Fi-S-308c00d1117fa74c600cb4c97d433fd4`). The 32-hex
# segment is the cast UUID; we strip it as the fallback when the TXT
# record doesn't carry a clean `fn` (friendly name) value.
_AVAHI_CAST_UUID_SUFFIX_RE = re.compile(r"-[0-9A-Fa-f]{32}$")


def _clean_chromecast_dbus_name(avahi_label: str, txt: dict[str, str]) -> str:
    """Resolve the display name from a D-Bus Chromecast advertisement.

    Prefers the `fn` TXT record (user-set friendly name; matches what
    CastInfo.friendly_name produces on the live-browser path). Falls back
    to stripping the trailing `-<32-hex>` cast UUID from the avahi label
    and replacing the remaining dashes with spaces — the inverse of the
    avahi encoding that turned the original spaces into dashes.

    When neither path applies (no `fn`, no UUID suffix) the avahi label
    passes through unchanged.
    """
    fn = (txt or {}).get("fn", "").strip()
    if fn:
        return fn
    stripped = _AVAHI_CAST_UUID_SUFFIX_RE.sub("", avahi_label)
    if stripped == avahi_label:
        return avahi_label
    return stripped.replace("-", " ")

from app.output.base import AdvanceCallback, DeviceNotReadyError, OutputDevice
from app.plex.models import Track

_CAST_AVAILABLE = False
# pychromecast >= 14 changed wait() to return None on success and raise RequestTimeout
# on timeout (previously returned True/False). _RequestTimeout is patched in tests.
_RequestTimeout = Exception

import logging
_log = logging.getLogger(__name__)

try:
    import pychromecast
    from pychromecast.error import RequestTimeout as _RequestTimeout
    from pychromecast.controllers.media import MediaStatusListener
    from pychromecast.discovery import CastBrowser, SimpleCastListener
    import zeroconf as _zeroconf
    _CAST_AVAILABLE = True
except Exception as _e:
    _log.warning("pychromecast unavailable — Chromecast discovery disabled: %s", _e)


_CONTAINER_MIME: dict[str, str] = {
    "flac": "audio/flac",
    "mp3": "audio/mpeg",
    "aac": "audio/aac",
    "ogg": "audio/ogg",
    "opus": "audio/ogg; codecs=opus",
    "alac": "audio/mp4",
    "wav": "audio/wav",
}

# Backstop for a hung stream that never reports ANY terminal status: if the
# receiver hasn't ended the track within its duration + this grace, the
# watchdog forces an advance. Generous enough to absorb normal buffering and
# slow (e.g. Plex relay) streams; tunable in one place.
WATCHDOG_GRACE_S = 30

# Belt-and-suspenders for the 2026-06-17 FLAC-seek bug: a constrained Cast
# receiver can report idle_reason=ERROR at the true end of a track that was
# *seeked* (its seek desyncs without a FLAC seektable). When the reported
# position is within this much of the known duration, treat the ERROR as a
# clean finish — log it calmly and skip the admin failure toast. The real fix
# is the FLAC seektable (app/api/stream.py); this just keeps a slipped-through
# case from looking like (and being announced as) a hard failure.
NEAR_END_GRACE_MS = 12_000


def _content_type(stream_url: str, container: str | None, part_path: str = "") -> str:
    if container and container in _CONTAINER_MIME:
        native = _CONTAINER_MIME[container]
    else:
        path = stream_url.split("?")[0]
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext in _CONTAINER_MIME:
            native = _CONTAINER_MIME[ext]
        else:
            guessed, _ = mimetypes.guess_type(stream_url)
            native = guessed or "audio/mpeg"
    # /api/stream transcodes OGG-family sources to FLAC, so advertise the served
    # type — not the source type — or the receiver mis-inits its decode pipeline
    # (2026-06-17). part_path (the Plex part path, e.g. .../file.ogg) is the
    # authoritative extension; production Tracks never set .container.
    from app.transcode import device_stream_content_type
    return device_stream_content_type(stream_url, part_path, native)


class _AdvanceListener:
    """Fires advance_cb when Cast reports the track finished."""

    def __init__(self, backend: "ChromecastBackend") -> None:
        self._backend = backend

    def new_media_status(self, status: Any) -> None:
        # current_time/duration are logged so an end-of-track ERROR can be told
        # apart from a clean finish: if current_time stalls well short of (or
        # past) the receiver's perceived duration before ERROR, the receiver is
        # mis-estimating the FLAC length (the 2026-06-17 "plays then waits then
        # ERRORs" gap). DEBUG-only — enable with LOG_LEVEL=debug to capture.
        _log.debug(
            "Cast status update: player_state=%s idle_reason=%s "
            "current_time=%s duration=%s",
            status.player_state, status.idle_reason,
            getattr(status, "current_time", None), getattr(status, "duration", None),
        )
        if status.player_state in ("PLAYING", "BUFFERING") and status.current_time is not None:
            self._backend._pos_snapshot_ms = int(status.current_time * 1000)
            self._backend._pos_snapshot_at = time.monotonic()
        # Advance only on a TERMINAL idle reason for a track we believe is
        # playing. FINISHED = natural end; ERROR = the receiver failed to
        # load/decode/stream the media (the silent-stall culprit — skip the
        # dead track rather than freeze the queue). CANCELLED and INTERRUPTED
        # are self-induced by our own stop()/skip/next-track play() and must
        # NOT advance — doing so would double-advance. A pre-play IDLE carries
        # no terminal meaning, hence the _is_playing gate.
        if (self._backend._is_playing
                and status.player_state == "IDLE"
                and status.idle_reason in ("FINISHED", "ERROR")):
            self._backend._on_eos(status.idle_reason)


class _VolumeListener:
    """Updates backend._volume when the device changes volume externally."""

    def __init__(self, backend: "ChromecastBackend") -> None:
        self._backend = backend

    def new_cast_status(self, status: Any) -> None:
        if status is None:
            return
        try:
            vol = float(status.volume_level)
        except (AttributeError, TypeError, ValueError):
            return
        vol = max(0.0, min(1.0, vol))  # mirrors DLNA/AirPlay clamping
        from app.output.base import echo_guard_active
        if not echo_guard_active(self._backend._vol_last_set):
            self._backend._volume = vol
            _log.debug("Cast external volume change: %.2f", vol)
            # pychromecast invokes this listener on its internal thread, not
            # the asyncio loop.  Hop back to the loop to broadcast — mirrors
            # the EOS pattern in _on_eos. Loop may be None if a volume event
            # arrives before play() has been called; skip the broadcast in
            # that case (state still updated above for the next read).
            loop = self._backend._loop
            if loop is not None:
                from app.events.bus import manager
                from app.events.types import VolumeChangedEvent
                fut = asyncio.run_coroutine_threadsafe(
                    manager.broadcast_to_admins(VolumeChangedEvent(level=vol)),
                    loop,
                )
                # Discarded futures swallow exceptions silently on GC. Attach
                # a done-callback that logs at WARNING so a broadcast failure
                # surfaces in normal logs instead of vanishing.
                def _log_broadcast_exc(f):
                    if not f.cancelled() and f.exception():
                        _log.warning("Cast volume broadcast failed: %s", f.exception())
                fut.add_done_callback(_log_broadcast_exc)


# Watcher service-type tag for Cast events (matches watcher._SERVICE_BACKENDS).
_GOOGLECAST_SERVICE = "_googlecast._tcp.local"


class _CastSubscription:
    """Handle bridging the persistent CastBrowser to the device watcher's
    ``subscribe(service_type, on_event)`` contract (2026-06-15 plan U3).

    CastBrowser callbacks fire on pychromecast's discovery thread; ``emit``
    crosses back to the asyncio loop captured at subscribe time via
    ``call_soon_threadsafe`` (the proven mdns_dbus shape). The ``active``
    guard silences events after unsubscribe.
    """

    def __init__(self, on_event, loop) -> None:
        self.on_event = on_event
        self.loop = loop
        self.active = True

    def emit(self, kind: str, payload) -> None:
        if not self.active:
            return
        try:
            self.loop.call_soon_threadsafe(self.on_event, kind, payload)
        except RuntimeError:
            pass  # asyncio loop already closed — nowhere to deliver


class ChromecastBackend:
    """Plays audio via pychromecast by handing Plex stream URLs to the Cast device.

    Discovery uses CastBrowser (persistent mDNS listener) patterned after
    Music Assistant — github.com/music-assistant/server (MIT License).
    """

    def __init__(self, advance_cb: AdvanceCallback | None = None) -> None:
        self._advance_cb = advance_cb
        self._cast: Any = None
        self._volume: float = 0.5
        self._vol_last_set: float = 0.0
        self._device_id: str | None = None
        self._is_playing: bool = False
        self._listener: _AdvanceListener | None = None
        self._vol_listener: _VolumeListener | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pos_snapshot_ms: int = 0
        self._pos_snapshot_at: float = 0.0
        # Duration watchdog backstop (U2). _play_token is bumped on every
        # play() so a watchdog from a superseded track no-ops instead of
        # force-advancing the wrong one.
        self._watchdog_task: asyncio.Task | None = None
        self._play_token: int = 0
        # Track duration (ms) of the current track, captured at play(); used to
        # re-arm the watchdog after a seek and to classify a near-end ERROR as a
        # clean finish (2026-06-17 FLAC-seek belt-and-suspenders).
        self._duration_ms: int = 0

        # Persistent CastBrowser state.  Callbacks fire from pychromecast's
        # internal thread, so all access to _cast_infos is protected by _discover_lock.
        self._browser: Any = None           # CastBrowser instance
        self._zconf: Any = None             # active Zeroconf (may be external or owned)
        self._zconf_owned: bool = False     # True only when we created our own Zeroconf
        self._shared_zconf: Any = None      # injected by state.py; shared with AirPlay to avoid double UDP 5353 bind
        self._cast_infos: dict[str, Any] = {}  # str(uuid) → CastInfo
        # U3: the watcher's continuous subscription to CastBrowser events. The
        # _on_cast_added/_on_cast_removed callbacks push 'new'/'remove' here
        # (in addition to maintaining _cast_infos for playback).
        self._discovery_sub: "_CastSubscription | None" = None
        # Address cache: uuid or "{host}:{port}" → (name, host, port). Fed by
        # register_resolved on every live arrival (in-process or avahi/D-Bus);
        # read by set_device/_sync_connect/probe_device. Connection is by IP, so
        # this is all the avahi/D-Bus fallback needs.
        self._dbus_index: dict[str, tuple[str, str, int]] = {}
        self._discover_lock = threading.Lock()
        self._dbus_discover_lock = asyncio.Lock()  # serializes D-Bus one-shots
        # Set in _sync_connect; read by set_device to persist the resolved address (R1/R2).
        self._resolved_host: str | None = None
        self._resolved_port: int | None = None
        self._resolved_name: str | None = None

    # ── device discovery ──────────────────────────────────────────────────────

    async def discover_devices(self) -> list[OutputDevice]:
        if not _CAST_AVAILABLE:
            return []
        from app import state
        # When the shared 5353 bind failed (a host avahi owns the port), the
        # in-process CastBrowser can't run — fall back to discovering via the
        # host avahi over D-Bus (2026-06-16 cross-host fix). Connection is by
        # IP, so no 5353 bind is needed on this path.
        if state._mdns_port_unavailable:
            return await self._dbus_discover()
        if self._browser is None:
            await asyncio.get_running_loop().run_in_executor(None, self._start_browser)
        # Block in the thread pool (not the asyncio event loop) so pychromecast's
        # Zeroconf thread sockets on UDP 5353 stay decoupled from the rest of
        # the asyncio reactor.
        _log.info("Chromecast: waiting 5s for mDNS announcements")
        await asyncio.get_running_loop().run_in_executor(None, self._wait_for_discovery)
        with self._discover_lock:
            devices = [
                OutputDevice(id=uid, name=info.friendly_name, backend_type="chromecast")
                for uid, info in self._cast_infos.items()
            ]
        _log.info("Chromecast: found %d device(s): %s", len(devices), [d.name for d in devices])
        return devices

    def _wait_for_discovery(self) -> None:
        threading.Event().wait(5)

    async def _dbus_discover(self) -> list[OutputDevice]:
        """Avahi-over-D-Bus fallback discovery (2026-06-16 cross-host fix),
        used when the in-process 5353 bind failed (a host avahi owns the port).

        Populates ``_dbus_index`` so ``set_device``/``_sync_connect`` (which
        connect by IP via ``get_chromecast_from_host``) and ``probe_device``
        resolve the same devices — no in-process zeroconf socket needed.
        Keys by uuid when the TXT carried an ``id=`` entry, else ``host:port``
        — the SAME keying the live D-Bus subscription produces, so a Scan
        racing a live arrival reconciles to one entry. Merges into the cache
        (never clears) so retained/live entries keep their addresses."""
        from app.output import mdns_dbus
        async with self._dbus_discover_lock:
            found = await mdns_dbus.discover("_googlecast._tcp.local")
            if found is None:
                return []  # D-Bus socket absent — nothing to report
            fresh: dict[str, tuple[str, str, int]] = {}
            results: list[OutputDevice] = []
            seen_ids: set[str] = set()
            for name, host, port, uuid, txt in found:
                display_name = _clean_chromecast_dbus_name(name, txt)
                fresh[f"{host}:{port}"] = (display_name, host, port)
                if uuid:
                    fresh[uuid] = (display_name, host, port)
                    device_id, id_fmt = uuid, "uuid"
                else:
                    device_id, id_fmt = f"{host}:{port}", "host_port"
                if device_id not in seen_ids:
                    seen_ids.add(device_id)
                    results.append(OutputDevice(
                        id=device_id, name=display_name,
                        backend_type="chromecast", id_format=id_fmt))
            with self._discover_lock:
                self._dbus_index.update(fresh)
            _log.debug("Chromecast: avahi/D-Bus fallback found %d device(s): %s",
                       len(results), [r.name for r in results])
            return results

    def register_resolved(
        self,
        name: str,
        host: str,
        port: int,
        uuid: str | None,
        txt: dict[str, str],
    ) -> OutputDevice:
        """Feed one avahi-resolved advertisement into the D-Bus address cache.

        Called by the device watcher (2026-06-11 live-discovery plan U2,
        KTD9) on every subscription arrival. Everything downstream —
        ``probe_device``, ``set_device``/``_sync_connect``, admin's
        ``_host_for`` — resolves addresses through ``_dbus_index``; a
        subscription arrival that skipped this cache would be visible in
        the registry yet unusable for playback.

        Writes both the ``host:port`` key and, when present, the uuid key,
        under the ``_discover_lock`` (pychromecast threads read the index in
        ``probe_device``). Never clears anything — Scan-retained offline
        devices keep their addresses.

        Returns the OutputDevice keyed by uuid when the arrival carried one,
        else ``host:port`` (KTD10 normalization), so the watcher's registry
        ids always equal this backend's cache ids.
        """
        display_name = _clean_chromecast_dbus_name(name, txt or {})
        with self._discover_lock:
            self._dbus_index[f"{host}:{port}"] = (display_name, host, port)
            if uuid:
                self._dbus_index[uuid] = (display_name, host, port)
        if uuid:
            return OutputDevice(id=uuid, name=display_name,
                                backend_type="chromecast", id_format="uuid")
        return OutputDevice(id=f"{host}:{port}", name=display_name,
                            backend_type="chromecast", id_format="host_port")

    async def probe_device(self, device_id: str) -> bool:
        """Picker-facing probe: True if Chromecast is verified to work on
        the device addressed by *device_id*.

        Looks up the device in the existing caches (``_dbus_index`` for
        the D-Bus discovery path, ``_cast_infos`` for the live-browser
        path), runs the blocking ``pychromecast.get_chromecast_from_host``
        + ``cast.wait(timeout=10)`` calls in a thread-pool executor (the
        same shape ``_sync_connect`` uses), and treats "wait() returned
        without raising ``_RequestTimeout``" as the success criterion.

        Per pychromecast >= 14, ``wait()`` returns ``None`` on success
        and raises ``_RequestTimeout`` on timeout — we do NOT check
        ``cast.status``, which is populated by background listeners and
        is the wrong success signal.

        ``cast.disconnect()`` is called in a finally block on every path
        so the Zeroconf-backed connection (and any background socket the
        cast object holds) is never leaked, even on the timeout path.

        Never raises. Unknown device_id, pychromecast unavailable, and
        every failure path return False with a WARNING log.
        """
        if not _CAST_AVAILABLE:
            return False
        # Resolve host:port:name from whichever cache carries the device.
        with self._discover_lock:
            dbus_info = self._dbus_index.get(device_id)
            cast_info = self._cast_infos.get(device_id)
        if dbus_info is not None:
            name, host, port = dbus_info
        elif cast_info is not None:
            name = cast_info.friendly_name
            host = cast_info.host
            port = cast_info.port
        else:
            return False

        def _probe_sync() -> bool:
            cc = None
            try:
                cc = pychromecast.get_chromecast_from_host(
                    (host, port, None, name, None)
                )
                cc.wait(timeout=10)
                return True
            except _RequestTimeout:
                _log.warning(
                    "Chromecast probe_device: %r did not connect within 10s",
                    device_id,
                )
                return False
            except Exception:
                _log.warning(
                    "Chromecast probe_device failed for %r",
                    device_id, exc_info=True,
                )
                return False
            finally:
                if cc is not None:
                    try:
                        cc.disconnect()
                    except Exception:
                        # Disconnect itself failing is non-fatal — the OS
                        # will reclaim the socket when the probe-local
                        # cast object is GC'd shortly.
                        pass

        try:
            return await asyncio.get_running_loop().run_in_executor(None, _probe_sync)
        except Exception:
            _log.warning(
                "Chromecast probe_device executor failed for %r",
                device_id, exc_info=True,
            )
            return False

    def _start_browser(self) -> None:
        if self._shared_zconf is not None:
            zconf = self._shared_zconf
            self._zconf_owned = False
        else:
            zconf = _zeroconf.Zeroconf()
            self._zconf_owned = True
        try:
            browser = CastBrowser(
                SimpleCastListener(
                    add_callback=self._on_cast_added,
                    remove_callback=self._on_cast_removed,
                    update_callback=self._on_cast_added,
                ),
                zconf,
            )
            browser.start_discovery()
        except Exception:
            if self._zconf_owned:
                try:
                    zconf.close()
                except Exception:
                    pass
            raise
        self._zconf = zconf
        self._browser = browser
        _log.info("Chromecast: CastBrowser started")

    def _on_cast_added(self, uuid: UUID, name: str) -> None:
        # Called from pychromecast's discovery thread — must be thread-safe.
        with self._discover_lock:
            if not self._browser:
                return
            info = self._browser.devices.get(uuid)
            if info:
                self._cast_infos[str(uuid)] = info
        # U3: also feed the watcher's continuous subscription (outside the
        # lock — emit only schedules onto the asyncio loop). _cast_infos stays
        # populated for the playback connection either way.
        if info is not None:
            sub = self._discovery_sub
            if sub is not None:
                sub.emit("new", self._cast_event_payload(str(uuid), info))

    def _on_cast_removed(self, uuid: UUID, name: str, cast_info: Any) -> None:
        with self._discover_lock:
            self._cast_infos.pop(str(uuid), None)
        sub = self._discovery_sub
        if sub is not None:
            # Keyed by the friendly_name we emitted on 'new' so the watcher's
            # name index resolves back to the registry key (origin AE1).
            friendly = getattr(cast_info, "friendly_name", None) or name
            sub.emit("remove", (friendly, _GOOGLECAST_SERVICE))

    @staticmethod
    def _cast_event_payload(uid: str, info: Any) -> tuple:
        """Build the watcher 'new' tuple from a CastInfo. ``uid`` is the
        ``_cast_infos`` key (``str(uuid)``), so the registry key the watcher
        derives via register_resolved equals the cache key by construction —
        and matches what the U7 Scan snapshot uses (uuid-key consistency)."""
        return (info.friendly_name, info.host, info.port, uid, {}, _GOOGLECAST_SERVICE)

    async def subscribe_discovery(self, on_event) -> "_CastSubscription | None":
        """Make the persistent CastBrowser the watcher's continuous Cast
        source (plan U3). Starts the browser (idempotent) and registers
        *on_event*; thereafter _on_cast_added/_on_cast_removed push 'new'/
        'remove' to the watcher AND keep _cast_infos populated for playback.

        Replays already-discovered devices so a browser started earlier (e.g.
        by a Scan) is not invisible to a fresh subscription. Returns None when
        pychromecast/zeroconf is unavailable so the watcher marks Chromecast
        degraded (U6). Never raises."""
        if not _CAST_AVAILABLE:
            return None
        loop = asyncio.get_running_loop()
        try:
            if self._browser is None:
                await loop.run_in_executor(None, self._start_browser)
        except Exception:
            _log.warning("Chromecast: CastBrowser start failed for discovery "
                         "subscription", exc_info=True)
            return None
        sub = _CastSubscription(on_event, loop)
        self._discovery_sub = sub
        with self._discover_lock:
            snapshot = list(self._cast_infos.items())
        for uid, info in snapshot:
            sub.emit("new", self._cast_event_payload(uid, info))
        sub.emit("status", "up")
        return sub

    async def unsubscribe_discovery(self, handle: "_CastSubscription | None") -> None:
        """Stop delivering CastBrowser events to the watcher. The browser
        keeps running — _cast_infos must stay populated for the playback
        connection even across a watcher restart. Never raises."""
        if handle is not None:
            handle.active = False
        if self._discovery_sub is handle:
            self._discovery_sub = None

    def snapshot_devices(self) -> list[OutputDevice]:
        """Current live CastBrowser snapshot as uuid-keyed OutputDevices.

        The U7 Scan reconciles from THIS (never _dbus_discover): both the live
        subscription and the snapshot key by ``str(uuid)``, so a Scan racing a
        live arrival of the same device produces one registry entry, not two."""
        with self._discover_lock:
            return [
                OutputDevice(id=uid, name=info.friendly_name,
                             backend_type="chromecast", id_format="uuid")
                for uid, info in self._cast_infos.items()
            ]

    def close(self) -> None:
        """Stop the CastBrowser and release the Zeroconf instance if owned."""
        with self._discover_lock:
            if self._browser:
                try:
                    self._browser.stop_discovery()
                except Exception:
                    pass
                self._browser = None
            if self._zconf and self._zconf_owned:
                try:
                    self._zconf.close()
                except Exception:
                    pass
                self._zconf = None

    # ── set_device ────────────────────────────────────────────────────────────

    async def set_device(self, device_id: str) -> None:
        if not _CAST_AVAILABLE:
            return
        from app import database
        stored = await database.get_setting(f"vol:chromecast:{device_id}")
        fallback = float(stored) if stored else 0.5

        old_listener = self._listener
        old_vol_listener = self._vol_listener
        old_cast = self._cast

        cast = await asyncio.get_running_loop().run_in_executor(
            None, self._sync_connect, device_id
        )
        self._cast = cast
        self._device_id = device_id
        self._listener = None

        # R1/R2: persist the resolved address so startup reconnect can bypass mDNS.
        if self._resolved_host and self._resolved_port is not None:
            try:
                from app import database
                await database.set_setting(
                    f"output_addr:{device_id}",
                    json.dumps({
                        "name": self._resolved_name or device_id,
                        "host": str(self._resolved_host),
                        "port": int(self._resolved_port),
                    }),
                )
            except Exception:
                pass

        try:
            vol = cast.status.volume_level if cast.status else None
            self._volume = float(vol) if vol is not None else fallback
        except Exception:
            self._volume = fallback

        self._vol_listener = _VolumeListener(self)
        try:
            cast.register_status_listener(self._vol_listener)
        except Exception:
            pass

        if old_cast:
            _lst = old_listener
            _vlst = old_vol_listener
            _cc = old_cast

            def _teardown():
                if _lst:
                    try:
                        _cc.media_controller.unregister_status_listener(_lst)
                    except Exception:
                        pass
                if _vlst:
                    try:
                        _cc.unregister_status_listener(_vlst)
                    except Exception:
                        pass
                try:
                    _cc.disconnect()
                except Exception:
                    pass

            try:
                await asyncio.get_running_loop().run_in_executor(None, _teardown)
            except Exception:
                pass

    def _sync_connect(self, device_id: str) -> Any:
        # D-Bus path — device_id is "{host}:{port}" (legacy) or UUID (preferred).
        with self._discover_lock:
            dbus_info = self._dbus_index.get(device_id)
        if dbus_info is not None:
            name, host, port = dbus_info
            _log.info("Chromecast: connecting to D-Bus device %r (%s:%d)", device_id, host, port)
            cc = pychromecast.get_chromecast_from_host((host, port, None, name, None))
            try:
                cc.wait(timeout=10)
            except _RequestTimeout:
                raise RuntimeError(
                    f"Chromecast D-Bus device {device_id!r} did not connect within 10s "
                    f"(if its IP changed, rescan in output settings)"
                )
            self._resolved_name, self._resolved_host, self._resolved_port = name, host, port
            return cc

        # Fast path: use cached CastInfo from persistent browser (no new scan).
        with self._discover_lock:
            cast_info = self._cast_infos.get(device_id)

        if cast_info is not None and self._zconf is not None:
            _log.info("Chromecast: connecting to %r via cached CastInfo", device_id)
            cc = pychromecast.get_chromecast_from_cast_info(cast_info, self._zconf)
            try:
                cc.wait(timeout=10)
            except _RequestTimeout:
                raise RuntimeError(f"Chromecast device {device_id!r} did not connect within 10s")
            self._resolved_name = cast_info.friendly_name
            self._resolved_host = cast_info.host
            self._resolved_port = cast_info.port
            return cc

        # Fallback: one-shot scan (browser not yet started or device not seen).
        # Skip when mDNS port 5353 is unavailable — creating a bare Zeroconf()
        # would fail with EADDRINUSE (a host avahi already holds the port and
        # we are NOT on host networking). Without in-process discovery there is
        # nothing to connect to; the operator must run with --network host
        # (U6 banner) and reselect.
        from app import state
        if state._mdns_port_unavailable:
            raise RuntimeError(
                f"Chromecast device {device_id!r} unreachable: in-process mDNS "
                f"is unavailable (run the container with --network host, then "
                f"reselect the device in output settings)"
            )
        _log.warning("Chromecast: no cached info for %r — falling back to one-shot scan", device_id)
        chromecasts, browser = pychromecast.get_chromecasts(tries=1, retry_wait=0, timeout=8)
        target = next((cc for cc in chromecasts if str(cc.uuid) == device_id), None)
        if target:
            try:
                target.wait(timeout=10)
            except _RequestTimeout:
                target = None
        pychromecast.discovery.stop_discovery(browser)
        if not target:
            raise RuntimeError(f"Chromecast device {device_id!r} not found")
        self._resolved_name = target.name
        self._resolved_host = target.host
        self._resolved_port = target.port
        return target

    # ── playback ──────────────────────────────────────────────────────────────

    async def play(self, stream_url: str, metadata: Track) -> None:
        if not _CAST_AVAILABLE or self._cast is None:
            raise DeviceNotReadyError("Chromecast not available or no device selected")
        self._loop = asyncio.get_running_loop()
        content_type = _content_type(
            stream_url,
            getattr(metadata, "container", None),
            getattr(metadata, "stream_key", "") or "",
        )
        await self._loop.run_in_executor(
            None, self._sync_play, stream_url, content_type, metadata
        )
        # Arm the duration watchdog backstop (U2). _sync_play has succeeded
        # and flipped _is_playing True by here (it raises on failure). Skip
        # when duration is unknown — there's nothing to time against.
        self._cancel_watchdog()
        self._play_token += 1
        duration_ms = getattr(metadata, "duration_ms", 0) or 0
        self._duration_ms = duration_ms
        if duration_ms > 0:
            self._watchdog_task = asyncio.create_task(
                self._watchdog(self._play_token, duration_ms)
            )

    def _sync_play(self, stream_url: str, content_type: str, metadata: Track) -> None:
        self._pos_snapshot_ms = 0
        self._pos_snapshot_at = 0.0
        try:
            mc = self._cast.media_controller
            _log.debug("Cast _sync_play: url=%s content_type=%s", stream_url, content_type)
            if self._listener is None:
                self._listener = _AdvanceListener(self)
                mc.register_status_listener(self._listener)
            mc.play_media(
                stream_url,
                content_type,
                title=metadata.title,
                thumb=getattr(metadata, "thumb", None),
                current_time=0,
                autoplay=True,
            )
            mc.block_until_active(timeout=10)
            _log.debug("Cast _sync_play: block_until_active returned, player_state=%s",
                       mc.status.player_state if mc.status else "unknown")
            self._is_playing = True
        except Exception as exc:
            self._is_playing = False
            raise RuntimeError(f"Cast play failed: {exc}") from exc

    async def pause(self) -> None:
        # A paused track must not count down toward the watchdog deadline.
        self._cancel_watchdog()
        if self._cast and _CAST_AVAILABLE:
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, self._cast.media_controller.pause
                )
            except Exception:
                _log.debug("Cast pause() failed (no active session?)", exc_info=True)
        self._is_playing = False

    async def resume(self) -> None:
        if self._cast and _CAST_AVAILABLE:
            await asyncio.get_running_loop().run_in_executor(
                None, self._cast.media_controller.play
            )
            self._is_playing = True

    async def stop(self) -> None:
        self._cancel_watchdog()
        if self._cast and _CAST_AVAILABLE:
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, self._cast.media_controller.stop
                )
            except Exception:
                _log.debug("Cast stop() failed (no active session?)", exc_info=True)
        self._is_playing = False

    async def set_volume(self, level: float) -> None:
        self._volume = max(0.0, min(1.0, level))
        self._vol_last_set = time.monotonic()
        if self._cast and _CAST_AVAILABLE:
            cast = self._cast
            vol = self._volume
            await asyncio.get_running_loop().run_in_executor(None, cast.set_volume, vol)
        if self._device_id:
            from app import database
            await database.set_setting(f"vol:chromecast:{self._device_id}", str(self._volume))

    async def get_volume(self) -> float:
        return self._volume

    # ── EOS ───────────────────────────────────────────────────────────────────

    def _on_eos(self, reason: str = "FINISHED") -> None:
        """End-of-stream entry point. Called from the Cast status thread
        (via _AdvanceListener) and from the duration watchdog. Flips
        _is_playing and hops onto the asyncio loop to run _handle_eos, the
        single funnel where logging, failure broadcast, and advance happen.
        """
        self._is_playing = False
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._handle_eos(reason), self._loop)

    async def _handle_eos(self, reason: str) -> None:
        """Log the end-of-stream reason, then advance the queue. Runs on the
        asyncio loop. `reason` is FINISHED for a clean end, ERROR for a
        receiver failure, or "watchdog" for the hung-stream backstop.

        Cancels any pending watchdog first so a clean/error EOS retires the
        backstop. The watchdog path clears its OWN task ref before awaiting
        this, so this cancel can never cancel the task currently running it
        (the self-cancel trap — see _watchdog)."""
        self._cancel_watchdog()
        # A receiver ERROR at ~the real end of a *seeked* track is a clean finish
        # the device mislabeled (no FLAC seektable → seek desync; 2026-06-17).
        # Treat it as a normal end so it neither logs as a failure nor toasts.
        near_end = (
            reason == "ERROR"
            and self._duration_ms > 0
            and self._pos_snapshot_ms >= self._duration_ms - NEAR_END_GRACE_MS
        )
        if reason == "FINISHED" or near_end:
            _log.info(
                "Cast track ended (reason=%s%s) — advancing",
                reason, ", at end" if near_end else "",
            )
        else:
            _log.warning(
                "Cast playback ended abnormally (reason=%s) — advancing to next track",
                reason,
            )
            # Surface the failure to the admin UI (mirrors the AirPlay crash
            # broadcast in airplay.py). Function-local imports keep the output
            # layer free of an app.events import at module load.
            try:
                from app.events.bus import manager
                from app.events.types import OutputChangedEvent
                await manager.broadcast_to_admins(OutputChangedEvent(
                    backend_type="error",
                    device_name="Chromecast playback failed; advancing to next track",
                ))
            except Exception:
                _log.warning("Cast: failure-event broadcast failed", exc_info=True)
        if self._advance_cb:
            await self._advance_cb()

    async def _watchdog(self, token: int, duration_ms: int) -> None:
        """Backstop for a receiver that ends a track without emitting any
        terminal status (a hung stream the EOS listener never hears about).
        Sleeps the track's duration + grace, then forces an advance — unless
        a newer play() superseded this token or playback already stopped."""
        try:
            await asyncio.sleep(duration_ms / 1000 + WATCHDOG_GRACE_S)
        except asyncio.CancelledError:
            return
        if self._play_token != token or not self._is_playing:
            return  # superseded by a newer play(), or already stopped/ended
        self._is_playing = False
        # Clear our own ref BEFORE _handle_eos so its _cancel_watchdog() can't
        # cancel this very task mid-advance (the DLNA/AirPlay self-cancel trap).
        self._watchdog_task = None
        await self._handle_eos("watchdog")

    def _cancel_watchdog(self) -> None:
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        self._watchdog_task = None

    async def get_position(self) -> int:
        if self._is_playing and self._pos_snapshot_at > 0:
            elapsed = int((time.monotonic() - self._pos_snapshot_at) * 1000)
            return self._pos_snapshot_ms + elapsed
        return self._pos_snapshot_ms

    async def seek(self, position_ms: int) -> None:
        if not (self._cast and _CAST_AVAILABLE):
            return
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, self._cast.media_controller.seek, position_ms / 1000.0
            )
        except Exception:
            _log.warning("Cast seek to %dms failed", position_ms, exc_info=True)
            return
        # Re-anchor position and re-arm the watchdog for the post-seek remainder:
        # skipping ahead ends the track earlier than the play()-time deadline, so
        # without this the hung-stream backstop would fire far too late (the
        # 2026-06-17 FLAC-seek belt-and-suspenders).
        position_ms = max(0, position_ms)
        self._pos_snapshot_ms = position_ms
        self._pos_snapshot_at = time.monotonic()
        if self._is_playing and self._duration_ms > 0:
            remaining_ms = max(0, self._duration_ms - position_ms)
            self._cancel_watchdog()
            self._play_token += 1
            self._watchdog_task = asyncio.create_task(
                self._watchdog(self._play_token, remaining_ms)
            )

    @property
    def is_playing(self) -> bool:
        return self._is_playing
