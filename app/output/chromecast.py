"""Chromecast (Google Cast) output backend via pychromecast.

Device discovery uses pychromecast's CastBrowser + SimpleCastListener for
persistent mDNS listening instead of a one-shot scan.  This avoids the
intermittent-miss problem with devices that announce late in the mDNS window.

# Persistent-listener approach patterned after Music Assistant (MIT License):
# https://github.com/music-assistant/server — providers/chromecast/provider.py
"""

from __future__ import annotations

import asyncio
import collections
import functools
import json
import mimetypes
import re
import threading
import time
from typing import Any, NamedTuple
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
from app.models import Track

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

# Cast stream_type for the flow-mode LOAD (2026-07-11 supervisor plan U10).
# BUFFERED-without-duration is the deliberate default: LIVE tells receivers to
# drop their timeline entirely, while BUFFERED with an unknown duration (the
# streamed-FLAC pipe never carries one) keeps transport behavior closest to
# per-track playback on the JBL strict-baseline receiver. The LIVE-vs-BUFFERED
# call is a DEFERRED HARDWARE DECISION (plan deferred-to-implementation) —
# this knob is the one place to flip if validation on the real receiver shows
# unknown-duration BUFFERED streams stalling.
FLOW_STREAM_TYPE = "BUFFERED"

# Flow-mode status-poll cadence (seconds). pychromecast 14 has no built-in
# media-status polling and flow mode disarms the duration watchdog, so the
# device-time count crossings depend entirely on receiver-initiated status
# pushes — which some receivers stop sending mid-stream. While pending counts
# exist, the backend nudges the receiver with update_status this often.
# Injectable per-instance for tests (``_flow_poll_interval_s``).
FLOW_STATUS_POLL_S = 5.0


class _PendingCount(NamedTuple):
    """One pending device-time count crossing (flow mode, U10): the
    boundary's stitch offset, the supervisor token to confirm when the
    DEVICE-reported position crosses it, and the crossed-into track's id
    (seek re-keying matches on it)."""
    offset_ms: int
    token: int
    track_id: str | None


def _flow_base_url() -> str | None:
    """Device-reachable absolute base for the flow route — the SAME base
    logic per-track dispatch uses (``state._stream_url_base``). None when
    neither STREAM_BASE_URL nor a specific BIND_HOST is configured: the
    per-track path has a source-direct fallback there, but a flow stream is
    served only by this server, so the caller degrades to per-track."""
    from app import state
    return state._stream_url_base() or None


def _log_flow_task_exc(task: Any) -> None:
    if not task.cancelled():
        exc = task.exception()
        if exc:
            _log.error("Cast flow task raised: %s", exc, exc_info=exc)


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
        # U10 mode fork: while a flow session is live the per-track idle-
        # reason matrix below carries no advance meaning (no track boundaries
        # exist device-side) — route to the flow handler and leave the
        # per-track body UNTOUCHED for toggle-off byte-identical behavior.
        if self._backend._in_flow_mode:
            self._backend._flow_media_status(status)
            return
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
        # Confirmed-start (2026-07-11 supervisor plan U1): the FIRST PLAYING
        # media status for the current play token is the data-plane proof the
        # receiver is actually rendering this dispatch — play counts key on
        # it, never on the LOAD command being accepted. One-shot per play:
        # the backend clears its token on emission, and a stale PLAYING from
        # a superseded dispatch names a token the supervisor ignores.
        if status.player_state == "PLAYING":
            self._backend._emit_confirmed_start()
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


class _ConnectionListener:
    """Reports Cast socket transitions to the output-session supervisor.

    LOST (2026-07-11 supervisor plan U2, R16): a connection loss mid-playback
    is a device-level failure by definition — the queue must hold, never
    advance. CONNECTED (plan U3): while an outage hold is active, the socket
    client's own auto-reconnect succeeding is THE re-attach trigger —
    LOST→CONNECTED destroys the media session, so the supervisor rebuilds
    (re-LOAD) and resumes.

    pychromecast invokes this on its socket-client thread; the handlers hop
    to the asyncio loop exactly like _on_eos. Bound to the cast object it was
    registered on so a listener surviving on a superseded cast (set_device
    keeps no unregister API for connection listeners) can never report a
    spurious outage — or trigger a stale re-attach — for the new device."""

    def __init__(self, backend: "ChromecastBackend", cast: Any) -> None:
        self._backend = backend
        self._cast = cast

    def new_connection_status(self, status: Any) -> None:
        st = getattr(status, "status", None)
        if st not in ("LOST", "CONNECTED"):
            return  # CONNECTING/DISCONNECTED/FAILED — nothing to route
        backend = self._backend
        if backend._cast is not self._cast:
            return  # stale listener from a superseded set_device
        loop = backend._loop
        if loop is None:
            return
        handler = (backend._on_connection_lost if st == "LOST"
                   else backend._on_connection_restored)
        try:
            loop.call_soon_threadsafe(handler)
        except RuntimeError:
            pass  # asyncio loop already closed — nowhere to deliver


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
        # Output-session supervisor dispatch token (plan U1), captured at
        # play() and cleared when the first PLAYING status emits the
        # confirmed-start signal. None = nothing awaiting confirmation.
        self._confirm_token: int | None = None

        # ── Cast flow mode (2026-07-11 supervisor plan U10) ────────────────
        # The live FlowSession while gapless flow playback is active; None =
        # per-track mode (every per-track code path checks nothing else, so
        # toggle-off behavior is byte-identical by construction).
        self._flow_session: Any = None
        # Pending device-time count crossings (_PendingCount entries) in
        # stitch order. Appended loop-side (boundary listener), consumed
        # front-first on the CAST SOCKET THREAD (media-status handler) —
        # hence the deque and the threading.Lock (the only flow state both
        # threads write).
        self._flow_lock = threading.Lock()
        self._flow_pending_counts: collections.deque[_PendingCount] = (
            collections.deque())
        # Boundary offset_ms → decode start offset (ms into the track) for
        # boundaries that did not start at the track's top: the session's
        # first entry on an outage resume (start_offset_ms) and seek
        # repositions. Needed to map stitch position → TRACK position.
        self._flow_track_starts: dict[int, int] = {}
        # One-shot context for the NEXT reposition boundary: (supervisor
        # token | None, decode start ms). A skip dispatch sets (token, 0);
        # a seek sets (None, target_ms). Consumed by the boundary listener.
        self._flow_pending_repos: tuple[int | None, int] | None = None
        # Held position primed by the supervisor before a resume dispatch
        # (prime_resume_offset); consumed one-shot at play().
        self._flow_resume_offset_ms: int = 0
        # Eagerly-captured track-relative held position at a flow outage —
        # taken BEFORE the session is torn down, consumed one-shot by
        # capture_held_position_ms when the classifier enters the hold.
        self._flow_held_capture_ms: int | None = None
        # Periodic status poll (loop-side): a call_later timer chain live
        # while pending counts exist so device-time crossings can't starve
        # on a receiver that stops pushing status (see FLOW_STATUS_POLL_S).
        # None interval → the module constant; tests inject per-instance.
        self._flow_poll_timer: Any = None
        self._flow_poll_interval_s: float | None = None

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
        # U10: a device switch tears the flow session down IMMEDIATELY — the
        # stitcher and its capability URL belong to the OLD device's media
        # session (router.stop() covers cross-backend switches; this covers
        # the same-backend device change, which never calls stop()).
        await self._flow_teardown()
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

        # U2: route a mid-playback socket loss to the supervisor's outage
        # classifier (device-level → hold). Registered per cast object; the
        # listener self-guards against superseded casts.
        try:
            cast.register_connection_listener(_ConnectionListener(self, cast))
        except Exception:
            _log.debug("Cast connection-listener registration failed",
                       exc_info=True)

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
        # U10 mode switch: gapless on → FLOW MODE (one server-stitched stream
        # LOADed once; boundaries are the advance authority). The held resume
        # offset (prime_resume_offset) and any outage capture stash are
        # consumed/cleared by THIS dispatch whatever path it takes.
        resume_offset_ms = self._flow_resume_offset_ms
        self._flow_resume_offset_ms = 0
        self._flow_held_capture_ms = None
        from app import state as app_state
        if app_state.gapless_enabled():
            base = _flow_base_url()
            if base is not None:
                await self._play_flow(metadata, base, resume_offset_ms)
                return
            _log.warning(
                "Cast gapless flow mode needs STREAM_BASE_URL or a specific "
                "BIND_HOST to build a device-reachable flow URL — falling "
                "back to per-track playback")
        # Per-track dispatch while a flow session lingers (toggle just went
        # off, or the flow degraded): the stitcher must not keep emitting
        # boundaries against a replaced media session. No-op in plain
        # per-track operation (nothing to tear down).
        await self._flow_teardown()
        # Capture the supervisor's per-dispatch token BEFORE _sync_play: the
        # Cast status thread can report PLAYING while block_until_active is
        # still blocking, and a token captured after the executor call would
        # miss that first status (plan U1).
        from app.output import session
        self._confirm_token = session.get_supervisor().current_token()
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

    # ── flow mode (2026-07-11 supervisor plan U10) ────────────────────────────
    # Thread map: everything here runs ON THE EVENT LOOP (the flow engine's
    # pump tasks and grace timer fire there, and play/seek/stop are loop-side
    # already) EXCEPT _flow_media_status + _flow_fire_crossings, which run on
    # pychromecast's CAST SOCKET THREAD and marshal via the *_threadsafe
    # entries exactly like _on_eos/_emit_confirmed_start.

    @property
    def _in_flow_mode(self) -> bool:
        """True while a flow session is attached — EXACTLY the
        ``self._flow_session is not None`` null-check every mode fork uses
        (a lingering not-yet-closed session still counts as flow mode; the
        sites that care about ``closed``/``ended`` bind the session object
        and check those themselves)."""
        return self._flow_session is not None

    async def _play_flow(self, metadata: Track, base_url: str,
                         start_offset_ms: int) -> None:
        """FLOW MODE dispatch. Two shapes:

        - A live flow session already LOADed on this device → the dispatch is
          a skip/skip-back/new-front: REPOSITION the stitcher (media session
          unchanged, NO re-LOAD — queue/Now Playing already updated at the
          dispatching endpoint; audio jumps after the device buffer drains).
          The confirm/count defers to the device-time crossing of the
          reposition boundary (plan: counts key on DEVICE-reported time).
        - No live session (initial dispatch, outage resume, ended/superseded
          session) → create a fresh FlowSession — ``start_offset_ms`` carries
          the supervisor-primed held position on a resume (R7: position-
          resume fully server-controlled, no device seek) — and LOAD the flow
          URL once. The first PLAYING status confirms via the normal U1
          token (dispatch_play issued it; captured in _confirm_token)."""
        from app.output import flow, session as output_session
        supervisor = output_session.get_supervisor()
        token = supervisor.current_token()
        sess = self._flow_session
        if (sess is not None and not sess.closed and not sess.ended
                and sess is flow.current_flow_session()):
            if await sess.reposition(metadata, 0):
                # The PLAYING-status confirm path must stay quiet — the
                # media session is unchanged, so a routine status would
                # confirm before the audio transition is heard.
                self._confirm_token = None
                if token is not None:
                    supervisor.defer_confirmation(token)
                with self._flow_lock:
                    # A skip cancels every uncrossed pending count: the
                    # listener never hears past the jump (the few buffered
                    # tail seconds are not a play).
                    self._flow_pending_counts.clear()
                self._flow_pending_repos = (token, 0)
                if sess.paused:
                    # Skip while flow-paused: a per-track skip auto-plays,
                    # so the reposition must too — unfreeze the stitcher's
                    # pacing clock AND issue the receiver play (mirrors
                    # resume()); without both, the device stays silent
                    # while the UI reads playing.
                    sess.resume()
                    try:
                        await asyncio.get_running_loop().run_in_executor(
                            None, self._cast.media_controller.play
                        )
                    except Exception:
                        _log.debug("Cast flow resume-on-skip failed (no "
                                   "active session?)", exc_info=True)
                self._is_playing = True
                return
            # The session ended/closed under us — fall through to a fresh one.
        await self._flow_teardown()
        sess = flow.create_flow_session(metadata,
                                        start_offset_ms=start_offset_ms)
        sess.add_boundary_listener(functools.partial(self._flow_boundary, sess))
        sess.add_skip_listener(functools.partial(self._flow_skip, sess))
        sess.add_consumer_gone_listener(
            functools.partial(self._flow_consumer_gone, sess))
        sess.add_ended_listener(functools.partial(self._flow_ended, sess))
        self._flow_session = sess
        self._flow_pending_repos = None
        self._flow_track_starts = (
            {0: start_offset_ms} if start_offset_ms > 0 else {})
        with self._flow_lock:
            self._flow_pending_counts.clear()
        self._confirm_token = token  # first PLAYING confirms the first track
        url = base_url.rstrip("/") + sess.url_path
        try:
            await self._loop.run_in_executor(
                None, self._sync_play_flow, url, sess.content_type, metadata)
        except Exception:
            await self._flow_teardown()
            raise
        # No duration watchdog in flow mode: there is no per-track duration —
        # liveness is stream consumption (the session's consumer-gone grace)
        # plus connection status. Bump the play token so any armed per-track
        # watchdog from a previous dispatch goes stale.
        self._cancel_watchdog()
        self._play_token += 1
        self._duration_ms = 0

    def _sync_play_flow(self, stream_url: str, content_type: str,
                        metadata: Track) -> None:
        """The ONE flow LOAD (executor thread, mirrors _sync_play)."""
        self._pos_snapshot_ms = 0
        self._pos_snapshot_at = 0.0
        try:
            mc = self._cast.media_controller
            _log.info("Cast flow LOAD: url=%s content_type=%s stream_type=%s",
                      stream_url, content_type, FLOW_STREAM_TYPE)
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
                stream_type=FLOW_STREAM_TYPE,
            )
            mc.block_until_active(timeout=10)
            self._is_playing = True
        except Exception as exc:
            self._is_playing = False
            raise RuntimeError(f"Cast flow play failed: {exc}") from exc

    def _detach_flow(self) -> Any | None:
        """Drop every reference/ledger for the current flow session (sync,
        loop-side) and return it. The outage capture stash deliberately
        SURVIVES detach — the classifier reads it after the teardown."""
        sess, self._flow_session = self._flow_session, None
        self._flow_pending_repos = None
        self._flow_track_starts = {}
        timer, self._flow_poll_timer = self._flow_poll_timer, None
        if timer is not None:
            timer.cancel()
        with self._flow_lock:
            self._flow_pending_counts.clear()
        return sess

    async def _flow_teardown(self) -> None:
        """Detach + close the flow session (idempotent; no-op in per-track
        mode). Used by: toggle-off at a boundary, device switch (set_device),
        stop(), a failed flow LOAD, and a per-track dispatch superseding a
        lingering flow."""
        sess = self._detach_flow()
        if sess is not None and not sess.closed:
            await sess.close()

    def _spawn_flow_teardown(self) -> None:
        """Sync detach + fire-and-forget close (loop-side): the outage paths
        must not await the subprocess teardown before reporting."""
        sess = self._detach_flow()
        if sess is None or sess.closed:
            return
        task = asyncio.get_running_loop().create_task(sess.close())
        task.add_done_callback(_log_flow_task_exc)

    async def _flow_boundary(self, sess: Any, event: Any) -> None:
        """Flow boundary listener — runs on the EVENT LOOP, awaited by the
        pump (boundary ordering is strict). Natural boundaries are the flow
        advance authority; reposition boundaries only settle the pending
        count ledger (the skip/seek endpoint already owns the queue)."""
        if sess is not self._flow_session:
            return  # a superseded session's pump draining — stale
        from app import state as app_state
        from app.output import session as output_session
        if event.reposition:
            pending, self._flow_pending_repos = self._flow_pending_repos, None
            tid = getattr(event.track, "id", None)
            with self._flow_lock:
                survivors: collections.deque[_PendingCount] = collections.deque()
                if pending is not None:
                    token, start_ms = pending
                    if token is not None:
                        # Skip dispatch: its count keys on the device crossing
                        # THIS boundary's offset.
                        survivors.append(
                            _PendingCount(event.offset_ms, token, tid))
                    else:
                        # Seek within the current track: re-key its still-
                        # uncrossed count (if any) to the new offset — the
                        # sought audio is where the listener will first hear
                        # it. Everything else is cancelled (jumped past).
                        for pc in self._flow_pending_counts:
                            if pc.track_id == tid:
                                survivors.append(_PendingCount(
                                    event.offset_ms, pc.token, pc.track_id))
                                break
                    if start_ms > 0:
                        self._flow_track_starts[event.offset_ms] = start_ms
                self._flow_pending_counts = survivors
            if survivors:
                self._ensure_flow_poll()
            return
        if not app_state.gapless_enabled():
            # Toggle-off teardown at the NEXT boundary (plan U10): the
            # current track finished in flow mode; hand this boundary's track
            # to the per-track dispatch. SINGLE OWNER: _do_advance (the
            # advance_cb) both pops the queue front (== event.track — the
            # lookahead resolved it from the same front) and dispatches it,
            # so this listener advances NOTHING — no double-advance, and the
            # advance authority reverts atomically with the teardown. The
            # close is out-of-band (detach is sync): we run INSIDE the pump's
            # boundary await, and an in-listener close would make the pump
            # trip over its own closed encoder.
            _log.info("Cast flow: gapless toggled off — tearing down at the "
                      "%r boundary; per-track playback resumes",
                      getattr(event.track, "title", "?"))
            self._spawn_flow_teardown()
            if self._advance_cb:
                # Schedule the handoff dispatch on its OWN task: this
                # listener runs on the pump task, and the teardown's close()
                # cancels the pump — an advance awaited from here would be
                # cancelled mid-dispatch and the per-track handoff would
                # never fire.
                task = asyncio.get_running_loop().create_task(
                    self._advance_cb())
                task.add_done_callback(_log_flow_task_exc)
            return
        token = await output_session.notify_flow_boundary(event.track)
        if token is None:
            return  # a skip/hold owned the transition — nothing advanced
        with self._flow_lock:
            self._flow_pending_counts.append(_PendingCount(
                event.offset_ms, token, getattr(event.track, "id", None)))
        self._ensure_flow_poll()

    async def _flow_skip(self, sess: Any, track: Any, reason: str) -> None:
        """Flow server-side skip listener (loop): a track's source failed to
        resolve/decode. MUST complete before returning — the engine awaits it
        before re-resolving the lookahead, and a failed item left at the
        queue front trips the engine's spin guard and ends the flow."""
        if sess is not self._flow_session:
            return
        from app import state as app_state
        tid = getattr(track, "id", None)
        cur = app_state.queue_engine.state.current
        if (self._confirm_token is not None and cur is not None
                and getattr(cur.track, "id", None) == tid):
            # A fresh session's FIRST track failed to decode before any
            # PLAYING status arrived: the audio that eventually starts is
            # the NEXT track, so the pending LOAD confirmation must not
            # count this one (unheard-audio invariant). Withdraw the
            # dispatch — the next boundary owns the audible track's
            # advance/count.
            from app.output import session as output_session
            token, self._confirm_token = self._confirm_token, None
            output_session.get_supervisor().on_dispatch_failed(token)
        q = app_state.queue_engine.queue
        if q and getattr(q[0].track, "id", None) == tid:
            await app_state.queue_engine.remove(0)
        else:
            # The failed track may be the on-deck autofill pick — drop it so
            # the lookahead resolves a fresh pick instead of spinning (the
            # id-check + invalidate are atomic inside state; no-op when the
            # slot holds something else).
            await app_state.invalidate_ondeck_if_track(tid)
        await app_state._emit_track_skipped(track)

    def _flow_consumer_gone(self, sess: Any) -> None:
        """Consumer-gone grace expiry (loop — the session's timer): the
        receiver stopped pulling the stream and never re-requested within the
        grace. Route outage-SUSPECTED — deliberately NOT a DEVICE_LEVEL
        reason: with the Cast socket possibly still CONNECTED nothing has
        established the device is gone (the advance-authority table routes
        'stream consumer gone' to the classifier, whose reachability probe is
        the R15 tie-breaker: unreachable ⇒ hold, reachable ⇒ the stream/track
        is the failure ⇒ today's skip)."""
        if sess is not self._flow_session:
            return
        _log.warning("Cast flow: stream consumer gone beyond the grace — "
                     "reporting outage-suspected")
        self._flow_suspect_outage("flow_consumer_gone")

    async def _flow_ended(self, sess: Any) -> None:
        """Natural queue exhaustion (loop — the pump's finalize). ENCODE-side
        only: the consumer is still draining the audible tail (up to the
        run-ahead + device buffer), which may include boundaries the device
        has not crossed yet — so nothing is torn down and no advance fires
        here. The receiver's IDLE(FINISHED) after the tail drains is the
        flow's real 'final EOS' (_flow_natural_end): it fires the remaining
        crossings and converges through the advance path."""
        if sess is not self._flow_session:
            return
        _log.info("Cast flow: queue exhausted — stream finalized; waiting "
                  "for the receiver to drain the tail")

    async def _flow_natural_end(self, sess: Any) -> None:
        """The flow's final EOS (loop; hopped from the status thread on a
        terminal IDLE with the session ended): the device played the stream
        to the end, so every remaining pending crossing WAS audibly heard —
        fire them in stitch order, close the finalized session, and converge
        through the same advance path a final per-track EOS takes (advance()
        on the empty queue retires current → idle; a random end-behavior
        autofill dispatches a fresh flow). ``sess`` is the session captured
        on the status thread — a hop landing after a fresh session replaced
        it must no-op, never fire the NEW session's ledger. A replayed
        crossing whose token the supervisor's deferred stash evicted is a
        harmless no-op (eviction warns and is pathological)."""
        if sess is not self._flow_session:
            return
        with self._flow_lock:
            remaining = [pc.token for pc in self._flow_pending_counts]
            self._flow_pending_counts.clear()
        from app.output import session
        for token in remaining:
            session.notify_confirmed(token)
        await self._flow_teardown()
        self._is_playing = False
        _log.info("Cast flow: receiver finished the stream — converging "
                  "through the advance path")
        if self._advance_cb:
            await self._advance_cb()

    def _flow_suspect_outage(self, reason: str) -> None:
        """Loop-side flow outage funnel (consumer-gone; receiver IDLE mid-
        flow). Ordering is load-bearing: capture the held position FIRST
        (the classifier's enter_output_hold reads it via
        capture_held_position_ms AFTER this returns), then tear the dead
        session down (its media/stream is unusable — resume or skip LOADs
        fresh; also: never two stitchers), then report."""
        if not self._in_flow_mode:
            return
        self._flow_held_capture_ms = self._flow_capture_position_ms()
        self._is_playing = False
        self._spawn_flow_teardown()
        from app.output import session
        session.notify_outage(reason)

    def _flow_media_status(self, status: Any) -> None:
        """Flow-mode media-status handler — CAST SOCKET THREAD (hops to the
        loop like _on_eos). Replaces the per-track idle-reason matrix while a
        flow session is live: per-track terminal states are SUPPRESSED as
        advance authority (no track boundaries exist device-side — advance-
        authority table, Chromecast flow row); a terminal IDLE mid-flow is a
        receiver hiccup routed to outage-suspected instead."""
        _log.debug(
            "Cast flow status: player_state=%s idle_reason=%s current_time=%s",
            status.player_state, status.idle_reason,
            getattr(status, "current_time", None),
        )
        ct = getattr(status, "current_time", None)
        if status.player_state in ("PLAYING", "BUFFERING") and ct is not None:
            # RAW device stream time (the flow LOAD's own timeline) — the
            # track-relative mapping happens at the loop-side consumers
            # (get_position / capture_held_position_ms).
            self._pos_snapshot_ms = int(ct * 1000)
            self._pos_snapshot_at = time.monotonic()
        if status.player_state == "PLAYING":
            # First PLAYING = the flow LOAD's confirmed start (U1 token from
            # dispatch_play; one-shot). Every PLAYING also advances the
            # device-time crossing ledger — PLAYING is the "audio heard"
            # proof the counts key on.
            self._emit_confirmed_start()
            if ct is not None:
                self._flow_fire_crossings(int(ct * 1000))
        if (self._is_playing
                and status.player_state == "IDLE"
                and status.idle_reason in ("FINISHED", "ERROR")):
            self._is_playing = False
            loop = self._loop
            sess = self._flow_session
            if sess is not None and getattr(sess, "ended", False):
                # The stream finalized server-side (queue exhausted) and the
                # receiver just drained it to the end — the flow's real
                # 'final EOS', not a hiccup. ERROR routes here too: a strict
                # receiver can't distinguish the clean chunked-stream close
                # from a drop, the session is over either way (nothing is
                # masked), and routing it to outage would DISCARD the
                # remaining final counts.
                if loop is not None:
                    try:
                        asyncio.run_coroutine_threadsafe(
                            self._flow_natural_end(sess), loop)
                    except RuntimeError:
                        pass  # asyncio loop already closed
                return
            _log.warning(
                "Cast flow: receiver went IDLE (%s) mid-flow — routing "
                "outage-suspected, not advance (no track boundaries exist "
                "device-side)", status.idle_reason,
            )
            if loop is not None:
                try:
                    loop.call_soon_threadsafe(
                        self._flow_suspect_outage, "flow_receiver_idle")
                except RuntimeError:
                    pass  # asyncio loop already closed — nowhere to deliver

    def _flow_fire_crossings(self, device_stream_ms: int) -> None:
        """Device-time count crossings (CAST SOCKET THREAD): fire the U1
        chokepoint confirm for every pending boundary whose stitch offset the
        DEVICE-reported position has crossed — one-shot each, in stitch
        order. Every LOAD starts a fresh session whose stitch origin is the
        device's time zero, so device stream ms compares directly against
        boundary offsets. Tokens marshal to the loop via the same threadsafe
        hop as the confirmed-start path."""
        fired: list[int] = []
        with self._flow_lock:
            while (self._flow_pending_counts
                   and self._flow_pending_counts[0].offset_ms
                   <= device_stream_ms):
                fired.append(self._flow_pending_counts.popleft().token)
        if not fired:
            return
        from app.output import session
        for token in fired:
            session.notify_confirmed_threadsafe(self._loop, token)

    def _ensure_flow_poll(self) -> None:
        """Arm the periodic status poll (loop-side): pending device-time
        counts must not starve on a receiver that stops pushing media status
        (pychromecast 14 has no built-in polling and flow mode disarms the
        duration watchdog). A call_later timer chain rather than a sleeping
        task: the idle wait is a plain TimerHandle, so nothing pends across
        loop shutdown. Idempotent — one timer at a time; the chain stops on
        its own once the ledger drains (a later boundary re-arms it) and
        _detach_flow cancels it with the session's lifecycle."""
        if not self._in_flow_mode or self._flow_poll_timer is not None:
            return
        interval = self._flow_poll_interval_s
        self._flow_poll_timer = asyncio.get_running_loop().call_later(
            FLOW_STATUS_POLL_S if interval is None else interval,
            self._flow_poll_fire)

    def _flow_poll_fire(self) -> None:
        """Poll timer expiry (sync, loop): with the session live and counts
        still pending, spawn the one-shot update_status poll (the executor
        hop is async work). Ledger drained → the chain simply stops."""
        self._flow_poll_timer = None
        if not self._in_flow_mode:
            return
        with self._flow_lock:
            pending = bool(self._flow_pending_counts)
        if not pending:
            return
        task = asyncio.get_running_loop().create_task(self._flow_poll_once())
        task.add_done_callback(_log_flow_task_exc)

    async def _flow_poll_once(self) -> None:
        """One poll tick (loop task): ask the receiver for a media status via
        the media controller's update_status (executor — it writes to the
        Cast socket), then re-arm the chain. Errors are swallowed:
        connection loss is handled by the connection listener."""
        cast = self._cast
        if not self._in_flow_mode or cast is None:
            return
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, cast.media_controller.update_status)
        except Exception:
            _log.debug("Cast flow status poll failed", exc_info=True)
        finally:
            self._ensure_flow_poll()

    def _flow_track_position_ms(self, sess: Any,
                                device_time_s: float | None) -> int:
        """Map a device stream time to the TRACK-relative position (loop):
        stitch offset via the session's device-time mapping, minus the heard
        track's boundary offset, plus that boundary's decode start offset
        (resume/seek boundaries don't start at the track's top)."""
        stitch_ms = sess.held_offset_from_device_time(device_time_s)
        heard = sess.track_at(stitch_ms)
        off = sess.offset_of(heard) if heard is not None else None
        if off is None:
            return 0
        return max(0, stitch_ms - off + self._flow_track_starts.get(off, 0))

    def _flow_capture_position_ms(self) -> int:
        """The flow-mode held position (loop): DEVICE-reported position
        mapped through the stitch timeline (R7) — never the encode clock,
        which leads heard audio by the run-ahead. When the device is still
        inside a track the boundary clock already advanced past (Now Playing
        leads the audio), the held queue front never audibly started — hold
        it at 0:00 (and its uncrossed pending count was cancelled, so the
        resume play still counts exactly once, R19)."""
        sess = self._flow_session
        if sess is None:
            return 0
        device_s = ((self._pos_snapshot_ms / 1000.0)
                    if self._pos_snapshot_at > 0 else None)
        stitch_ms = sess.held_offset_from_device_time(device_s)
        heard = sess.track_at(stitch_ms)
        if heard is None:
            return 0
        from app import state as app_state
        current = app_state.queue_engine.state.current
        if (current is None
                or getattr(current.track, "id", None)
                != getattr(heard, "id", None)):
            return 0
        off = sess.offset_of(heard)
        if off is None:
            return 0
        return max(0, stitch_ms - off + self._flow_track_starts.get(off, 0))

    def capture_held_position_ms(self) -> int | None:
        """U10 hold hook (duck-typed; hold._capture_position_ms consults it
        FIRST). Flow mode: the track-relative held position — either the
        stash an outage path captured before tearing the session down, or a
        live mapping. Per-track mode returns None so the hold's normal
        ``_pos_snapshot_ms`` read applies unchanged (byte-identical)."""
        stash, self._flow_held_capture_ms = self._flow_held_capture_ms, None
        if stash is not None:
            return stash
        if not self._in_flow_mode:
            return None
        return self._flow_capture_position_ms()

    def prime_resume_offset(self, position_ms: int) -> None:
        """Supervisor resume hook (U10, R7): the held position for the NEXT
        dispatch. play() consumes it one-shot — in flow mode it feeds
        ``create_flow_session(start_offset_ms=…)`` (position-resume fully
        server-controlled); the per-track path ignores it and the
        supervisor's normal resume seek applies."""
        self._flow_resume_offset_ms = max(0, int(position_ms or 0))

    async def resume_seek(self, position_ms: int) -> None:
        """Supervisor position-resume hook (R7). Flow mode: NO-OP — play()
        already created the session at the primed held offset; a device seek
        would fight the stitch timeline. Per-track mode: exactly the seek the
        supervisor fell through to before this method existed."""
        if self._in_flow_mode:
            return
        await self.seek(position_ms)

    async def pause(self) -> None:
        # A paused track must not count down toward the watchdog deadline.
        self._cancel_watchdog()
        # U10: freeze the flow encode clock in step with the receiver pause —
        # otherwise the stitcher keeps leading and the run-ahead budget turns
        # the pause into a post-resume skip-ahead of buffered audio.
        sess = self._flow_session
        if sess is not None:
            sess.pause()
        if self._cast and _CAST_AVAILABLE:
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, self._cast.media_controller.pause
                )
            except Exception:
                _log.debug("Cast pause() failed (no active session?)", exc_info=True)
        self._is_playing = False

    async def resume(self) -> None:
        # U10: unfreeze the flow encode clock symmetrically with pause().
        sess = self._flow_session
        if sess is not None:
            sess.resume()
        if self._cast and _CAST_AVAILABLE:
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, self._cast.media_controller.play
                )
            except Exception:
                _log.debug("Cast resume() failed (no active session?)",
                           exc_info=True)
            self._is_playing = True

    async def stop(self) -> None:
        self._cancel_watchdog()
        # U10: stop ends the flow playback outright — close the stitcher
        # (device switch routes here via router.swap_pending/activate paths;
        # set_device has its own teardown for the same-backend switch).
        await self._flow_teardown()
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

    # ── confirmed start (2026-07-11 supervisor plan U1) ───────────────────────

    def _emit_confirmed_start(self) -> None:
        """Report the current dispatch's confirmed start to the supervisor.

        Called from the Cast status thread (via _AdvanceListener) on the first
        PLAYING media status for the current play; hops to the asyncio loop
        exactly like _on_eos. One-shot: the token is cleared here so repeat
        PLAYING statuses (buffer wobble, seek) can't re-emit."""
        token = self._confirm_token
        if token is None:
            return
        self._confirm_token = None
        from app.output import session
        session.notify_confirmed_threadsafe(self._loop, token)

    # ── reachability probe + connection loss (2026-07-11 supervisor plan U2) ──

    async def probe_liveness(self) -> tuple[bool, str | None]:
        """R15 reachability probe: socket/status liveness (plan KTD).

        Non-blocking attribute reads only — pychromecast's HeartbeatController
        keeps ``socket_client.is_connected`` current, so no network roundtrip
        is needed (or wanted: the probe is the tie-breaker on paths where the
        device may be gone). Returns ``(reachable, player_state | None)``;
        never raises."""
        cast = self._cast
        if not _CAST_AVAILABLE or cast is None:
            return (False, None)
        try:
            sc = getattr(cast, "socket_client", None)
            reachable = bool(sc is not None and getattr(sc, "is_connected", False))
            status = getattr(cast.media_controller, "status", None)
            state = getattr(status, "player_state", None) if status is not None else None
            return (reachable, str(state) if state else None)
        except Exception:
            _log.debug("Cast probe_liveness failed", exc_info=True)
            return (False, None)

    def _on_connection_lost(self) -> None:
        """Loop-side handler for a LOST socket (via _ConnectionListener): a
        device-level failure by definition (R15) — report outage-suspected to
        the supervisor instead of ever advancing (R16). The watchdog is
        retired so it cannot fire a second signal for the same outage."""
        if not self._is_playing:
            # pause() flips _is_playing False, so a Cast powered off while
            # USER-PAUSED lands here with a live session still interrupted —
            # the Paused→OutagePaused edge (was_paused rides the hold). Only
            # a plain-idle loss (no current track) is a true no-op.
            from app import state
            qs = state.queue_engine.state
            if qs.current is None or not qs.is_paused:
                return  # raced our own stop/skip — nothing is being interrupted
        self._is_playing = False
        self._cancel_watchdog()
        if self._in_flow_mode:
            # U10: capture the held offset from the last DEVICE-reported
            # position mapped through the stitch timeline BEFORE tearing the
            # session down (the classifier's hold reads the stash via
            # capture_held_position_ms after this returns; never two
            # stitchers across the outage).
            self._flow_held_capture_ms = self._flow_capture_position_ms()
            self._spawn_flow_teardown()
        _log.warning("Cast connection LOST mid-playback — reporting outage-suspected")
        from app.output import session
        session.notify_outage("connection_lost")

    def _on_connection_restored(self) -> None:
        """Loop-side CONNECTED handler (supervisor plan U3). Only meaningful
        while an outage hold is active: the socket client reconnected on its
        own (its 5s auto-retry — the sole re-attach trigger while it lives),
        but LOST→CONNECTED destroyed the media session, so the supervisor
        must rebuild (re-LOAD) and resume. Outside a hold this is the initial
        connect / a routine blip with nothing held — no-op."""
        from app.output import session
        if not session.output_hold_active():
            return
        _log.info("Cast connection restored — triggering supervisor re-attach")
        session.notify_reconnect_trigger("cast_connected")

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
        Sleeps the track's duration + grace, then — U2's R15/R16 fork —
        probes reachability: a reachable device gets today's forced advance
        (a hung stream, not an outage); an unreachable one reports
        outage-suspected so the queue holds instead of being consumed.
        A newer play() token or stopped playback makes this a no-op."""
        try:
            await asyncio.sleep(duration_ms / 1000 + WATCHDOG_GRACE_S)
        except asyncio.CancelledError:
            return
        if self._play_token != token or not self._is_playing:
            return  # superseded by a newer play(), or already stopped/ended
        reachable, _state = await self.probe_liveness()
        if self._play_token != token or not self._is_playing:
            return  # superseded while the probe ran
        self._is_playing = False
        # Clear our own ref BEFORE _handle_eos so its _cancel_watchdog() can't
        # cancel this very task mid-advance (the DLNA/AirPlay self-cancel trap).
        self._watchdog_task = None
        if not reachable:
            _log.warning(
                "Cast watchdog: no terminal status and device unreachable — "
                "reporting outage-suspected (not advancing)",
            )
            from app.output import session
            session.notify_outage("watchdog_unreachable")
            return
        await self._handle_eos("watchdog")

    def _cancel_watchdog(self) -> None:
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        self._watchdog_task = None

    async def get_position(self) -> int:
        # U10 flow mode: _pos_snapshot_ms is device STREAM time — Now Playing
        # progress must show TRACK position, so map it through the stitch
        # timeline (boundary ledger + decode start offsets).
        sess = self._flow_session
        if sess is not None:
            if self._pos_snapshot_at > 0:
                dev_ms = self._pos_snapshot_ms
                if self._is_playing:
                    dev_ms += int(
                        (time.monotonic() - self._pos_snapshot_at) * 1000)
                device_s: float | None = dev_ms / 1000.0
            else:
                device_s = None
            return self._flow_track_position_ms(sess, device_s)
        if self._is_playing and self._pos_snapshot_at > 0:
            elapsed = int((time.monotonic() - self._pos_snapshot_at) * 1000)
            return self._pos_snapshot_ms + elapsed
        return self._pos_snapshot_ms

    async def seek(self, position_ms: int) -> None:
        # U10 flow mode: seek is a stitcher reposition within the CURRENT
        # track — the media session is untouched (no receiver seek; the
        # audio jumps once the device buffer drains, the accepted flow lag)
        # and the device stream clock keeps running, so the position
        # snapshot is NOT re-anchored. The reposition boundary re-keys the
        # track's decode start offset (and any uncrossed pending count).
        sess = self._flow_session
        if sess is not None:
            from app import state as app_state
            current = app_state.queue_engine.state.current
            if current is None:
                return
            position_ms = max(0, position_ms)
            self._flow_pending_repos = (None, position_ms)
            if not await sess.reposition(current.track, position_ms):
                self._flow_pending_repos = None
            return
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
