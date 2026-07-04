"""DLNA MediaRenderer output backend via async-upnp-client."""

from __future__ import annotations

import asyncio
import html
import time
import xml.etree.ElementTree as ET
from xml.parsers.expat import ExpatError
from datetime import timedelta
from typing import Any

from app.output.base import AdvanceCallback, OutputDevice
from app.models import Track

_DLNA_AVAILABLE = False

import logging
_log = logging.getLogger(__name__)

try:
    import aiohttp
    from async_upnp_client.aiohttp import AiohttpNotifyServer, AiohttpSessionRequester
    from async_upnp_client.client_factory import UpnpFactory
    from async_upnp_client.profiles.dlna import DmrDevice, TransportState
    from async_upnp_client.search import async_search
    _DLNA_AVAILABLE = True
except Exception as _e:
    _log.warning("async_upnp_client unavailable — DLNA discovery disabled: %s", _e)
    # Provide a stand-in so tests can still import TransportState from this
    # module on environments without async_upnp_client installed (the
    # production runtime always has the dep — this is a Windows-test guard).
    class TransportState:  # type: ignore[no-redef]
        STOPPED = "STOPPED"
        PLAYING = "PLAYING"
        PAUSED_PLAYBACK = "PAUSED_PLAYBACK"
        TRANSITIONING = "TRANSITIONING"
        NO_MEDIA_PRESENT = "NO_MEDIA_PRESENT"


_MEDIA_RENDERER_ST = "urn:schemas-upnp-org:device:MediaRenderer:1"

# Authoritative renderer check happens on the device-description XML's
# <deviceType>. SSDP's search-target filter is a hint, not a guarantee —
# many non-renderer UPnP services respond to broader patterns. Match by
# prefix so future schema versions (`:2`, `:3`) still pass.
_MEDIA_RENDERER_PREFIX = "urn:schemas-upnp-org:device:MediaRenderer:"

# Per-fetch timeout for the device-description GET. set_device uses 15s
# for the heavier UpnpFactory.async_create_device path (which walks SOAP
# service URLs); discovery does a plain GET on the same URL, so a tighter
# 5s window keeps a single slow renderer from dragging the whole scan.
# Matches the avahi browse window used elsewhere on the same network.
_DESCRIPTION_FETCH_TIMEOUT_S = 5.0

# Per-probe timeout for the lightweight DLNA verification path. The probe
# constructs a UpnpDevice from the cached LOCATION URL and inspects its
# service list — the same first stage set_device walks, just without the
# SUBSCRIBE chain. Bounded to 5s so a slow renderer can't stall the
# picker's "Checking…" window indefinitely.
_DLNA_PROBE_TIMEOUT_S = 5.0

# UPnP device-description root namespace. We tolerate both namespaced
# (well-formed) and unnamespaced (older or non-conformant) descriptions
# in `_fetch_device_description` by trying both lookups.
_UPNP_DEVICE_NS = "{urn:schemas-upnp-org:device-1-0}"

# The canonical UPnP AVTransport sequence: per UPnP AVTransport:2 §2.5.1,
# SetAVTransportURI does NOT change transport state in any state other
# than NO_MEDIA_PRESENT — a renderer already in STOPPED (post-EOS) stays
# STOPPED, the URI loads, and Play transitions STOPPED→PLAYING. STOPPED
# →STOPPED is not a defined transition, so no Stop call belongs in the
# sequence. Earlier iterations added a Stop call to "clear post-EOS
# state"; empirically on WiiM Pro / JBL Charge 5 (Linkplay firmware)
# that Stop induced the silent SetAVTransportURI rejection it was meant
# to prevent. BubbleUPnP famously removed its Stop call for the same
# reason.
#
# Between SetAVTransportURI and Play, the canonical Windows Media Player
# trace (documented in upmpdcli issue 54) issues GetTransportInfo +
# GetCurrentTransportActions + GetMediaInfo. async_upnp_client's
# DmrDevice.async_update() calls exactly that cluster internally. The
# working hypothesis (as of build 5cf4a0b-dirty + this change): post-EOS
# Linkplay renderers require being *queried* between SetURI and Play to
# commit the new URI to the internal pipeline — not given a fixed sleep.
# We use async_update() as the gate; if this hypothesis is wrong, the
# diagnostic state snapshots logged at each play() checkpoint will tell
# us why.


def _dmr_state_snapshot(dmr: Any) -> dict[str, str]:
    """Capture renderer state into a dict for compact log lines used at
    play() checkpoints. Each field is best-effort: attribute access on
    async_upnp_client's DmrDevice can raise on intermittent renderers
    or post-error states. Long URIs are truncated for log readability.

    Returns a dict mapping attribute name → printable value (string).
    All exceptions are captured as `<error:ExceptionType>` so the log
    line itself never raises.
    """
    out: dict[str, str] = {}
    for attr in ("transport_state", "av_transport_uri",
                 "av_transport_uri_metadata", "current_transport_actions"):
        try:
            v = getattr(dmr, attr, None)
            if v is None:
                out[attr] = "None"
            else:
                s = getattr(v, "name", str(v))
                # Truncate long URIs and DIDL strings.
                if len(s) > 80:
                    s = s[:60] + "..." + s[-15:]
                out[attr] = s
        except Exception as e:
            out[attr] = f"<error:{type(e).__name__}>"
    return out


def _format_snap(snap: dict[str, str]) -> str:
    """Render a snapshot dict as `k=v k=v k=v` for log lines."""
    return " ".join(f"{k}={v}" for k, v in snap.items())

_CONTAINER_MIME: dict[str, str] = {
    "flac": "audio/flac",
    "mp3": "audio/mpeg",
    "aac": "audio/aac",
    "ogg": "audio/ogg",
    "wav": "audio/wav",
    "alac": "audio/mp4",
}


def _mime_type(container: str | None, stream_url: str, part_path: str = "") -> str:
    if container and container in _CONTAINER_MIME:
        native = _CONTAINER_MIME[container]
    else:
        ext = stream_url.rsplit(".", 1)[-1].lower().split("?")[0]
        native = _CONTAINER_MIME.get(ext, "audio/mpeg")
    # /api/stream transcodes OGG-family sources to FLAC: declare the served type
    # in the DIDL protocolInfo, not the source type. DLNA renderers tolerate the
    # mismatch by sniffing, but the correct type is required for strict receivers
    # (Chromecast) and is simply correct (2026-06-17). part_path (the Plex part
    # path) is the authoritative extension; production Tracks never set .container.
    from app.transcode import device_stream_content_type
    return device_stream_content_type(stream_url, part_path, native)


def _didl_metadata(title: str, stream_url: str, mime: str, duration_ms: int) -> str:
    """Build a minimal DIDL-Lite XML string for SetAVTransportURI."""
    duration_s = duration_ms // 1000
    h = duration_s // 3600
    m = (duration_s % 3600) // 60
    s = duration_s % 60
    dur_str = f"{h}:{m:02d}:{s:02d}"
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        f'<item id="1" parentID="0" restricted="1">'
        f"<dc:title>{html.escape(title)}</dc:title>"
        f"<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
        f'<res duration="{dur_str}" protocolInfo="http-get:*:{mime}:*">'
        f"{stream_url}</res>"
        "</item>"
        "</DIDL-Lite>"
    )


def _create_discovery_session():
    """Construct the aiohttp ClientSession used for description fetches.

    Lifted into its own function so tests can patch this single symbol
    instead of the broader ``aiohttp`` namespace. In production it returns
    a real ClientSession; tests substitute a fake whose ``get(url)``
    returns an async context manager.
    """
    return aiohttp.ClientSession()


async def _fetch_description_body(session: Any, url: str) -> tuple[str, Any]:
    """Inner fetch — separated so :func:`_fetch_device_description` can wrap
    it in a single :func:`asyncio.wait_for` for total-time bounding.

    Returns ``("ok", xml_text)`` on success or ``("status_error", code)``
    when the server responded with a non-200 status. Raises on transport
    errors so the wrapper's ``except`` clause logs the reason.
    """
    async with session.get(url) as resp:
        if resp.status != 200:
            return ("status_error", resp.status)
        return ("ok", await resp.text())


async def _fetch_device_description(
    session: Any,
    url: str,
) -> tuple[str, str] | None:
    """GET the UPnP device description at ``url`` and return
    ``(deviceType, friendlyName)`` from the root ``<device>`` element.

    Returns ``None`` on any failure path — HTTP timeout, non-200 response,
    transport error, unparseable XML, or missing ``<device>`` root. Each
    failure logs a warning so production discovery surfaces the reason.

    Tolerates both namespaced (``urn:schemas-upnp-org:device-1-0``) and
    unnamespaced descriptions; older or non-conformant stacks sometimes
    serve the latter. Returned strings may be empty (``""``) if a child
    element is absent or has no text; the caller filters those.
    """
    try:
        result = await asyncio.wait_for(
            _fetch_description_body(session, url),
            timeout=_DESCRIPTION_FETCH_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        _log.warning("DLNA: description fetch timed out for %s", url)
        return None
    except Exception as e:  # aiohttp.ClientError, OSError, etc.
        _log.warning("DLNA: description fetch failed for %s: %s", url, e)
        return None

    kind, payload = result
    if kind == "status_error":
        _log.warning("DLNA: description fetch %s returned HTTP %d", url, payload)
        return None
    xml_text = payload

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        _log.warning("DLNA: malformed description XML at %s: %s", url, e)
        return None

    device_el = root.find(f"{_UPNP_DEVICE_NS}device")
    if device_el is None:
        device_el = root.find("device")
    if device_el is None:
        _log.warning("DLNA: description at %s has no <device> element", url)
        return None

    def _child_text(tag: str) -> str:
        el = device_el.find(f"{_UPNP_DEVICE_NS}{tag}")
        if el is None:
            el = device_el.find(tag)
        return (el.text or "").strip() if el is not None and el.text else ""

    return _child_text("deviceType"), _child_text("friendlyName")


class DlnaBackend:
    """Controls a DLNA MediaRenderer device via UPnP AVTransport."""

    def __init__(self, advance_cb: AdvanceCallback | None = None) -> None:
        self._advance_cb = advance_cb
        self._dmr: Any = None
        self._requester: Any = None
        # aiohttp.ClientSession backing self._requester. async_upnp_client's
        # AiohttpSessionRequester requires a session passed in and does not
        # own its lifecycle — we open one in set_device and close it on the
        # next set_device or stop.
        self._dlna_session: Any = None
        self._notify_server: Any = None   # AiohttpNotifyServer, per-backend (KTD2)
        self._volume: float = 0.5
        # Server-side echo guard — set by set_volume(); _on_dlna_event suppresses
        # GENA events arriving within 2s so server-initiated writes don't echo
        # back to the admin client. Mirrors ChromecastBackend._vol_last_set.
        self._vol_last_set: float = 0.0
        self._device_id: str | None = None
        self._is_playing: bool = False
        self._play_start: float = 0.0
        self._poll_task: asyncio.Task | None = None
        # Map device_id → location URL (populated during discovery)
        self._device_locations: dict[str, str] = {}

    # ── device discovery ──────────────────────────────────────────────────────

    async def discover_devices(self) -> list[OutputDevice]:
        """Discover DLNA MediaRenderers on the local network.

        Two-stage pipeline:
          1. SSDP M-SEARCH collects candidate responses. The search target
             is only a hint at the protocol level — many non-renderer UPnP
             services respond regardless — so candidates are stashed for
             a second-stage authoritative check.
          2. The device-description XML at each candidate's LOCATION URL
             is fetched concurrently. `<deviceType>` is checked against
             `urn:schemas-upnp-org:device:MediaRenderer:` (prefix match
             accepts future schema versions), and `<friendlyName>` becomes
             the human-readable label that flows into the admin picker.

        Failures at the fetch/parse stage skip the device with a logged
        reason; they never raise. The returned list is the surviving set,
        in SSDP-response order.
        """
        if not _DLNA_AVAILABLE:
            return []
        _log.debug("DLNA: starting SSDP search (8s timeout)")
        candidates: list[tuple[str, str, str]] = []  # (usn, server, location)
        seen_locations: set[str] = set()
        raw_count = 0

        async def _on_response(headers: Any) -> None:
            nonlocal raw_count
            raw_count += 1
            usn = headers.get("USN", "")
            server = headers.get("SERVER", "")
            location = headers.get("LOCATION", "")
            if not location:
                return
            if not location.startswith(("http://", "https://")):
                _log.warning("DLNA: ignoring device with non-HTTP LOCATION %r", location[:80])
                return
            # A device responding multiple times within the SSDP window
            # would otherwise trigger duplicate description fetches.
            if location in seen_locations:
                return
            seen_locations.add(location)
            candidates.append((usn, server, location))

        await async_search(_on_response, search_target=_MEDIA_RENDERER_ST, timeout=8)

        if not candidates:
            _log.debug("DLNA: search complete, found 0 renderer(s) (raw=%d)", raw_count)
            return []

        # One shared ClientSession for the whole gather phase. A new
        # session per fetch is valid but wasteful — connection pooling
        # is the point of the session abstraction. _create_discovery_session
        # is the patchable seam tests substitute for a no-op session.
        async with _create_discovery_session() as session:
            descriptions = await asyncio.gather(
                *(_fetch_device_description(session, loc) for _, _, loc in candidates),
                return_exceptions=False,
            )

        results: list[OutputDevice] = []
        skipped_non_renderer = 0
        skipped_no_friendly = 0
        skipped_fetch_failed = 0

        for (usn, _server, location), desc in zip(candidates, descriptions):
            if desc is None:
                skipped_fetch_failed += 1
                continue
            device_type, friendly_name = desc
            if not device_type.startswith(_MEDIA_RENDERER_PREFIX):
                _log.info(
                    "DLNA: skipping non-renderer at %s (deviceType=%s)",
                    location, device_type or "<empty>",
                )
                skipped_non_renderer += 1
                continue
            if not friendly_name:
                _log.warning(
                    "DLNA: skipping renderer at %s — missing or empty <friendlyName>",
                    location,
                )
                skipped_no_friendly += 1
                continue
            device_id = usn or location
            self._device_locations[device_id] = location
            results.append(OutputDevice(id=device_id, name=friendly_name, backend_type="dlna"))

        _log.debug(
            "DLNA: search complete, found %d renderer(s): %s "
            "(raw=%d, skipped non-renderer=%d, no friendlyName=%d, fetch failed=%d)",
            len(results), [d.name for d in results],
            raw_count, skipped_non_renderer, skipped_no_friendly, skipped_fetch_failed,
        )
        return results

    async def describe_renderer(
        self, location: str, usn: str = "",
    ) -> OutputDevice | None:
        """One-off targeted verification of a single LOCATION URL (the
        live-discovery watcher's U3/KTD4 hook).

        The watcher's opportunistic SSDP listener calls this when an
        ``ssdp:alive`` arrives for a device id it doesn't know: alive
        packets are unauthenticated hints, so the device may only enter
        the registry after the SAME description-XML check
        :meth:`discover_devices` applies to M-SEARCH responses
        (``deviceType`` prefix match + non-empty ``friendlyName``).
        Reuses that method's id derivation (USN, falling back to the
        LOCATION URL) and feeds ``_device_locations`` so set_device and
        probes can address the renderer immediately.

        Returns ``None`` on every failure path (library unavailable,
        fetch/parse failure, non-renderer, nameless renderer); transport
        errors are already absorbed and logged by
        :func:`_fetch_device_description`.
        """
        if not _DLNA_AVAILABLE:
            return None
        async with _create_discovery_session() as session:
            desc = await _fetch_device_description(session, location)
        if desc is None:
            return None
        device_type, friendly_name = desc
        if not device_type.startswith(_MEDIA_RENDERER_PREFIX):
            _log.info("DLNA: alive at %s is not a renderer (deviceType=%s)",
                      location, device_type or "<empty>")
            return None
        if not friendly_name:
            _log.warning(
                "DLNA: alive renderer at %s has no <friendlyName> — skipped",
                location,
            )
            return None
        device_id = usn or location
        self._device_locations[device_id] = location
        return OutputDevice(id=device_id, name=friendly_name,
                            backend_type="dlna")

    async def probe_device(self, device_id: str) -> bool:
        """Picker-facing probe: True if DLNA is verified to work on the
        device addressed by *device_id*.

        Walks the same first stage as :meth:`set_device` — constructs a
        UpnpDevice from the cached LOCATION URL — but stops short of
        starting the NotifyServer or running the SUBSCRIBE chain. Success
        is "the created device exposes a service whose ID ends with
        ``AVTransport``"; that's the playback control surface, and its
        presence is the cheapest reliable signal that the renderer will
        accept ``SetAVTransportURI`` at play time.

        A probe-local :class:`AiohttpSessionRequester` is used (not
        ``self._requester``, which may not exist on a backend instance
        that has never had ``set_device`` called) and closed in a finally
        block so the underlying aiohttp session does not leak across
        probe cycles.

        Never raises. Unknown device_id, async-upnp-client unavailable,
        factory timeout, factory exception, and "no AVTransport in the
        service list" all return False with a WARNING log.
        """
        if not _DLNA_AVAILABLE:
            return False
        location = self._device_locations.get(device_id)
        if not location:
            return False
        # Open a probe-local aiohttp.ClientSession. AiohttpSessionRequester
        # requires the session as a constructor argument (it does NOT own
        # the lifecycle), so we close the session ourselves in the finally
        # block — closing only the requester here leaks the socket.
        session = aiohttp.ClientSession()
        try:
            requester = AiohttpSessionRequester(session=session)
            factory = UpnpFactory(requester)
            try:
                upnp_device = await asyncio.wait_for(
                    factory.async_create_device(location),
                    timeout=_DLNA_PROBE_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                _log.warning(
                    "DLNA probe_device: timed out resolving %s for %r",
                    location, device_id,
                )
                return False
            except Exception:
                _log.warning(
                    "DLNA probe_device: factory failed for %r at %s",
                    device_id, location, exc_info=True,
                )
                return False
            # UpnpDevice.services is keyed by service_type — e.g.,
            # `urn:schemas-upnp-org:service:AVTransport:1`. We do a substring
            # match (not endswith) because the canonical service_type carries
            # a trailing schema-version suffix (`:1`, `:2`), and substring
            # match also handles the legacy serviceId shape
            # (`urn:upnp-org:serviceId:AVTransport`) any older device might
            # surface. An earlier `endswith('AVTransport')` check missed
            # real-world renderers entirely.
            services = getattr(upnp_device, "services", {}) or {}
            for service_key in services:
                if "AVTransport" in str(service_key):
                    return True
            _log.warning(
                "DLNA probe_device: no AVTransport service on %r (services=%s)",
                device_id, list(services),
            )
            return False
        finally:
            try:
                await session.close()
            except Exception:
                # Session close failure is non-fatal — the OS reclaims the
                # socket when the local reference drops.
                pass

    async def set_device(self, device_id: str) -> None:
        if not _DLNA_AVAILABLE:
            return
        self._cancel_poll()
        if self._dmr:
            try:
                await self._dmr.async_unsubscribe_services()
            except Exception:
                pass
        if self._notify_server:
            try:
                await self._notify_server.async_stop_server()
            except Exception:
                pass
            self._notify_server = None
        if self._requester:
            try:
                await self._requester.close()
            except Exception:
                pass
        # Close the prior aiohttp.ClientSession before opening a new one.
        # AiohttpSessionRequester doesn't own its session — we do — so a
        # missed close here leaks an aiohttp connection pool per set_device
        # call.
        if self._dlna_session is not None:
            try:
                await self._dlna_session.close()
            except Exception:
                pass
            self._dlna_session = None
        # Restore persisted volume before connecting
        from app import database
        stored = await database.get_setting(f"vol:dlna:{device_id}")
        self._volume = float(stored) if stored else 0.5
        self._device_id = device_id

        location = self._device_locations.get(device_id, device_id)
        self._dlna_session = aiohttp.ClientSession()
        self._requester = AiohttpSessionRequester(session=self._dlna_session)
        factory = UpnpFactory(self._requester)
        # Bound timeout on the device-description fetch and GENA SUBSCRIBE so
        # a slow renderer cannot block this task for the aiohttp 5-minute default.
        upnp_device = await asyncio.wait_for(
            factory.async_create_device(location), timeout=15.0,
        )

        # GENA event channel: per-backend NotifyServer (KTD2). Under host
        # networking (TrueNAS), binding 0.0.0.0:0 lets the OS pick a free port
        # and the library derives the callback URL from the bound socket.
        # AiohttpNotifyServer creates its own UpnpEventHandler internally,
        # exposed via .event_handler — pass that to DmrDevice so GENA NOTIFY
        # POSTs from the renderer reach our _on_dlna_event callback.
        # Symmetric error handling: if anything between async_start_server and
        # async_subscribe_services raises, tear down the notify server so its
        # bound socket is not left orphaned with self._dmr still None.
        self._notify_server = AiohttpNotifyServer(
            self._requester, source=("0.0.0.0", 0),
        )
        try:
            await self._notify_server.async_start_server()
            self._dmr = DmrDevice(upnp_device, event_handler=self._notify_server.event_handler)
            self._dmr.on_event = self._on_dlna_event
            await asyncio.wait_for(self._dmr.async_subscribe_services(), timeout=15.0)
        except Exception:
            try:
                await self._notify_server.async_stop_server()
            except Exception:
                pass
            self._notify_server = None
            self._dmr = None
            raise

    def _on_dlna_event(self, service: Any, state_variables: Any) -> None:
        """GENA NOTIFY callback — fires when subscribed services push state changes.

        Volume updates arrive on RenderingControl::LastChange (via LastChange
        parsing inside async_upnp_client which exposes the decoded child
        state variables). Per KTD1 we only forward the level. Other events
        (AVTransport::CurrentTransportState, etc.) are ignored — out of scope
        for this unit (see Deferred to Follow-Up Work in the plan).
        """
        # service may be UpnpService or None depending on library version; guard.
        service_id = getattr(service, "service_id", "") or ""
        if not service_id.endswith("RenderingControl"):
            return
        # state_variables is a sequence; find the Volume one if present.
        vol_var = None
        for sv in state_variables:
            if getattr(sv, "name", "") == "Volume":
                vol_var = sv
                break
        if vol_var is None:
            return
        try:
            vol_int = int(vol_var.value)
        except (TypeError, ValueError):
            return
        # Server-side echo guard: drop events within ECHO_GUARD_WINDOW of our own write.
        from app.output.base import echo_guard_active
        if echo_guard_active(self._vol_last_set):
            return
        self._volume = max(0.0, min(1.0, vol_int / 100.0))
        _log.debug("DLNA external volume change: %.2f", self._volume)
        # Broadcast on the asyncio loop. _on_dlna_event runs inside the
        # aiohttp request handler for the NOTIFY POST, so we already have a
        # running loop — fire-and-forget via create_task to keep the
        # response path non-blocking.
        from app.events.bus import manager
        from app.events.types import VolumeChangedEvent
        try:
            asyncio.get_running_loop().create_task(
                manager.broadcast_to_admins(VolumeChangedEvent(level=self._volume))
            )
        except RuntimeError:
            # Not in an asyncio context — should not happen on the NOTIFY path,
            # but skip gracefully if it ever does.
            pass

    # ── playback ──────────────────────────────────────────────────────────────

    async def play(self, stream_url: str, metadata: Track) -> None:
        _log.info(
            "DLNA play() entry: title=%r url=%s _dmr=%s _device_id=%s",
            metadata.title, stream_url, self._dmr is not None, self._device_id,
        )
        if not _DLNA_AVAILABLE or self._dmr is None:
            _log.warning(
                "DLNA play() raising RuntimeError: _DLNA_AVAILABLE=%s _dmr=%s",
                _DLNA_AVAILABLE, self._dmr is not None,
            )
            raise RuntimeError("DLNA not available or no device selected")
        self._cancel_poll()
        mime = _mime_type(
            getattr(metadata, "container", None),
            stream_url,
            getattr(metadata, "stream_key", "") or "",
        )
        # Canonical sequence: SetAVTransportURI → async_update (WMP-trace
        # pattern: GetTransportInfo + GetCurrentTransportActions +
        # GetMediaInfo) → Play. See the module-level commentary above the
        # _dmr_state_snapshot helper for the full rationale.
        #
        # The 4 DEBUG log lines below capture renderer state at every
        # checkpoint so the post-EOS track-advance bug (renderer silently
        # rejecting SetAVTransportURI on Linkplay firmware) can be
        # diagnosed conclusively when DEBUG logging is enabled: did the URI
        # actually load? did transport_state ever leave STOPPED? is Play
        # even in current_transport_actions? At INFO they were per-track
        # noise; the play() entry + success markers below stay at INFO.
        #
        # async_set_transport_uri signature is (media_url, media_title, meta_data=None).
        # `mime` is used inside _didl_metadata to build the DIDL; the library only
        # accepts the pre-built DIDL string as the third arg. Passing mime
        # separately raises TypeError.
        _log.debug("DLNA play() pre-SetURI: %s", _format_snap(_dmr_state_snapshot(self._dmr)))
        didl = _didl_metadata(metadata.title, stream_url, mime, metadata.duration_ms)
        await self._dmr.async_set_transport_uri(stream_url, metadata.title, didl)
        _log.debug("DLNA play() post-SetURI: %s", _format_snap(_dmr_state_snapshot(self._dmr)))
        # WMP-pattern gate between SetURI and Play. Tolerated to raise:
        # JBL Charge 5 sends malformed DIDL in CurrentTrackMetaData which
        # causes async_update's internal didl_lite.from_xml_string to
        # raise ParseError. We log and proceed — the SOAP roundtrips that
        # ARE part of async_update (GetTransportInfo, GetMediaInfo,
        # GetCurrentTransportActions) usually complete before the metadata
        # parse fails, so the renderer still gets queried even on partial
        # failure.
        try:
            await self._dmr.async_update()
        except Exception:
            _log.warning("DLNA play() async_update between SetURI and Play raised", exc_info=True)
        _log.debug("DLNA play() post-Update: %s", _format_snap(_dmr_state_snapshot(self._dmr)))
        await self._dmr.async_play()
        _log.debug("DLNA play() post-Play: %s", _format_snap(_dmr_state_snapshot(self._dmr)))
        self._is_playing = True
        self._play_start = time.monotonic()
        self._poll_task = asyncio.create_task(self._poll_eos())
        _log.info("DLNA play() set_transport_uri+async_play succeeded; EOS poll started")

    async def _poll_eos(self) -> None:
        # Require two consecutive STOPPED polls before firing advance. Real
        # DLNA renderers (notably WiiM Pro and similar embedded firmwares)
        # transiently report CurrentTransportState=STOPPED mid-stream while
        # audio is still playing — accepting the first STOPPED as end-of-
        # stream caused the queue to advance ~13s into every track. A real
        # EOS holds STOPPED across consecutive polls; transient STOPPED
        # flips back to PLAYING by the next 5s tick.
        #
        # Verbose logging is INFO-level on purpose: the 13s-disappear bug
        # is hard to reproduce in unit tests, so the production logs are
        # the diagnostic surface. Filter on "DLNA poll" to see the timeline.
        error_count = 0
        stopped_count = 0
        poll_idx = 0
        # One-shot diagnostic: log the poll# at which the renderer first
        # reports anything other than STOPPED. When the skip cascade fires
        # without this log appearing, the renderer never left STOPPED and
        # the bug is upstream of EOS detection — point the next fix at the
        # play() SOAP sequence, not the poll loop.
        first_non_stopped_logged = False
        while True:
            await asyncio.sleep(5)
            poll_idx += 1
            try:
                # Refresh device state from the renderer; transport_state is
                # then a fresh read off the cached AVTransport state vars.
                # The previous implementation called async_get_transport_info
                # — a method that does NOT exist on DmrDevice. Every poll
                # raised AttributeError and the 3-strike error path fired
                # advance_cb at ~15s wall clock (the user-visible ~13s).
                try:
                    await self._dmr.async_update()
                except (ET.ParseError, ExpatError):
                    # Some renderers (observed on WiiM Pro / JBL Charge 5)
                    # return DIDL-Lite track metadata with an unbound XML
                    # namespace prefix. async_upnp_client surfaces this as a
                    # ParseError from async_update's GetPositionInfo leg. The
                    # AVTransport transport_state was already refreshed by the
                    # earlier GetTransportInfo leg before the metadata parse
                    # blew up, so deliberately swallow this: log concisely (no
                    # traceback flood), do NOT count it toward the 3-strike
                    # error budget — the renderer is reachable, this is
                    # malformed metadata, not a connectivity failure — and
                    # fall through to read transport_state below so EOS
                    # detection still runs. The two-consecutive-STOPPED guard
                    # protects against acting on a stale read. error_count is
                    # reset to 0 below (a parse-only poll proves reachability),
                    # which also prevents a persistently-malformed renderer
                    # from ever false-advancing via the error path.
                    _log.debug(
                        "DLNA poll #%d: ignoring malformed track metadata (ParseError); using refreshed transport state",
                        poll_idx,
                    )
                state = self._dmr.transport_state
                # transport_state is a TransportState enum; .name gives the
                # spec string ("STOPPED", "PLAYING", etc.). Compare via name
                # so the TestState stand-in (str enum) works in unit tests.
                state_name = getattr(state, "name", str(state)) if state else "None"
                # Also log av_transport_uri on each poll so the post-EOS
                # track-advance bug (renderer silently reverting to the
                # previous URI after our SetAVTransportURI) is visible in
                # production logs. Truncated for readability.
                _uri = getattr(self._dmr, "av_transport_uri", None)
                _uri_str = (
                    "None" if _uri is None
                    else (_uri[:60] + "..." + _uri[-15:] if len(str(_uri)) > 80 else str(_uri))
                )
                _log.debug(
                    "DLNA poll #%d: state=%s av_transport_uri=%s is_playing=%s stopped_count=%d (will fire on >=2)",
                    poll_idx, state_name, _uri_str, self._is_playing, stopped_count,
                )
                if not first_non_stopped_logged and state_name != "STOPPED":
                    _log.info(
                        "DLNA poll: first non-STOPPED observed at poll #%d (state=%s)",
                        poll_idx, state_name,
                    )
                    first_non_stopped_logged = True
                error_count = 0
                if state_name == "STOPPED" and self._is_playing:
                    stopped_count += 1
                    if stopped_count >= 2:
                        _log.info(
                            "DLNA poll: confirmed STOPPED after %d consecutive polls — firing advance_cb",
                            stopped_count,
                        )
                        self._is_playing = False
                        # Clear our own task ref BEFORE awaiting advance_cb.
                        # advance_cb → _do_advance → DlnaBackend.play() →
                        # _cancel_poll() would otherwise call .cancel() on
                        # this very task (which IS the poll task). The
                        # CancelledError lands at the next real await inside
                        # play() (async_set_transport_uri) so the new track
                        # URI never reaches the renderer. Symptom on
                        # hardware: "initial track plays accurately, but
                        # advancing does not function." See airplay.py:1331
                        # for the same fix on the AirPlay watcher path.
                        self._poll_task = None
                        if self._advance_cb:
                            await self._advance_cb()
                        break
                else:
                    if stopped_count > 0:
                        _log.info(
                            "DLNA poll: state=%s reset stopped_count from %d to 0",
                            state_name, stopped_count,
                        )
                    stopped_count = 0
            except Exception as e:
                error_count += 1
                _log.warning(
                    "DLNA poll #%d: async_update raised %s; error_count=%d (will fire on >=3)",
                    poll_idx, type(e).__name__, error_count, exc_info=True,
                )
                if error_count >= 3:
                    _log.warning(
                        "DLNA poll: 3 consecutive errors — firing advance_cb"
                    )
                    self._is_playing = False
                    # Same self-cancel guard as the STOPPED path above —
                    # advance_cb → play() → _cancel_poll() would otherwise
                    # cancel the currently-running poll task.
                    self._poll_task = None
                    if self._advance_cb:
                        await self._advance_cb()
                    break

    def _cancel_poll(self) -> None:
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
        self._poll_task = None

    async def pause(self) -> None:
        if self._dmr and _DLNA_AVAILABLE:
            await self._dmr.async_pause()
            self._is_playing = False
            self._play_start = 0.0

    async def resume(self) -> None:
        if self._dmr and _DLNA_AVAILABLE:
            await self._dmr.async_play()
            self._is_playing = True

    async def stop(self) -> None:
        """Send AVT::Stop and cancel the EOS poll. **Non-destructive**:
        does NOT clear `_dmr`, the notify_server, the GENA subscription,
        or the aiohttp session — the connection stays alive so a
        subsequent `play()` can re-use it immediately.

        Earlier behavior cleared `_dmr=None`, which broke the skip flow
        at `admin.py:playback_skip` (router.stop() → router.play() raised
        RuntimeError because `_dmr` was None). The destructive cleanup
        is now handled at the only place it's actually needed:
        `set_device()` releases the prior `_dmr` / notify_server /
        session before binding a new renderer (see `set_device()` lines
        421-441). Backend switches via `OutputRouter.swap_pending()` go
        through `set_device()` on the new active backend; the old DLNA
        backend's resources idle until its next `set_device()` call.

        Bypasses the library's ``DmrDevice.async_stop`` helper. The
        helper guards on ``_can_transport_action("stop")`` which reads
        the cached ``CurrentTransportActions`` state variable. That
        cache is populated from the renderer's STOPPED-state action set
        (``{"play"}`` on Linkplay firmware) by ``async_update()`` inside
        ``play()`` — at which point ``__did_first_update`` flips True
        and the poll loop's subsequent ``async_update()`` calls skip
        ``GetCurrentTransportActions`` entirely (the library only polls
        that group when ``not is_subscribed or not __did_first_update``).
        The cache then stays stale at ``{"play"}`` forever, and
        ``async_stop`` silently returns mid-playback. We call the AVT/
        Stop SOAP action directly via ``_dmr._action("AVT", "Stop")``
        to bypass that gate. Symptom this fixes: skipping the last
        track in the queue did not stop the music.
        """
        _log.info(
            "DLNA stop() entry: _is_playing=%s _dmr=%s",
            self._is_playing, self._dmr is not None,
        )
        self._cancel_poll()
        if self._dmr and _DLNA_AVAILABLE:
            try:
                action = self._dmr._action("AVT", "Stop")
                if action is not None:
                    await action.async_call(InstanceID=0)
                else:
                    _log.warning("DLNA stop(): AVT/Stop action missing on device")
            except Exception:
                _log.warning("DLNA stop(): Stop SOAP raised", exc_info=True)
        self._is_playing = False
        self._play_start = 0.0

    async def set_volume(self, level: float) -> None:
        self._volume = max(0.0, min(1.0, level))
        # Stamp echo-guard window BEFORE the device write so any GENA NOTIFY
        # the renderer fires in response is suppressed by _on_dlna_event.
        self._vol_last_set = time.monotonic()
        if self._dmr and _DLNA_AVAILABLE:
            await self._dmr.async_set_volume_level(self._volume)
        if self._device_id:
            from app import database
            await database.set_setting(f"vol:dlna:{self._device_id}", str(self._volume))

    async def get_volume(self) -> float:
        return self._volume

    async def get_position(self) -> int:
        if self._is_playing and self._play_start > 0:
            return int((time.monotonic() - self._play_start) * 1000)
        return 0

    async def seek(self, position_ms: int) -> None:
        """Scrub to *position_ms* via UPnP AVTransport Seek(Unit=REL_TIME).

        Mirrors the Chromecast/AirPlay/Direct seek contracts: tell the
        renderer to move, then re-anchor the local wall-clock so
        get_position() reads the new offset. Re-anchor pattern matches
        airplay.py:1117-1119.

        Renderer-level Seek can legitimately fail (the renderer's
        transport rejects the SOAP); we log and return rather than raise
        so the admin API surface stays clean. The re-anchor only happens
        on the success path — otherwise get_position() would lie about
        the renderer's actual location.

        Bypasses the library's ``DmrDevice.async_seek_rel_time`` helper
        for the same reason ``stop()`` bypasses ``async_stop`` — see the
        ``stop()`` docstring above. The helper's
        ``_can_transport_action("seek")`` gate reads a cached
        ``CurrentTransportActions`` set that, on Linkplay-family
        renderers, never refreshes after ``play()``'s ``async_update()``
        call locks ``__did_first_update = True``. With the cache stuck
        at ``{"play"}`` the helper silently returns, ``_play_start`` is
        re-anchored anyway (no exception fired), and the user sees the
        seek bar land cosmetically while audio keeps playing from the
        original offset. Calling the AVT/Seek SOAP action directly via
        ``_dmr._action("AVT", "Seek")`` bypasses the gate.
        """
        if not _DLNA_AVAILABLE or self._dmr is None:
            return
        position_ms = max(0, position_ms)
        try:
            action = self._dmr._action("AVT", "Seek")
            if action is None:
                _log.warning("DLNA seek: AVT/Seek action missing on device")
                return
            from async_upnp_client.utils import time_to_str
            target = time_to_str(timedelta(milliseconds=position_ms))
            await action.async_call(InstanceID=0, Unit="REL_TIME", Target=target)
        except Exception:
            _log.warning("DLNA seek to %dms failed", position_ms, exc_info=True)
            return
        self._play_start = time.monotonic() - (position_ms / 1000.0)

    @property
    def is_playing(self) -> bool:
        return self._is_playing
