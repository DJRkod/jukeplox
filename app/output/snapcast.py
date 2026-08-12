"""Snapcast output backend (2026-08-11 plan U5/U6).

A server-fed ``AbstractOutputBackend`` on the U1 ``MultiroomBackendBase``:

- **Embedded** (default): drives the U4 ``SnapserverSupervisor`` and connects the
  JSON-RPC control client to it on loopback.
- **External** (optional): connects control to a operator-supplied host/port,
  validated fail-closed against the SSRF policy; never manages topology there.

The feed is a per-track ``ffmpeg -re`` writing 48000:16:2 PCM straight to the
snapserver ``tcp://`` source (U1 ``PcmFeed`` tcp sink) — Python never sees the
bytes. Track boundaries and advance are driven by jukeplox's queue+supervisor
(the queue is the clock; there is no stitched server byte-clock): a clean feed
EOF advances, a mid-track feed death holds and auto-restarts (outage), bounded
(U1 ``classify_feed_exit``). Snapclients pull audio FROM snapserver, so — unlike
Cast flow — no device-reachable ``STREAM_BASE_URL`` proxy is needed; the feed
ffmpeg reads the source directly (creds ride the httpx request / local path via
``flow._default_resolve_source``, never the argv).

The ``snapcast`` control library is imported ONLY inside the enable path
(dormancy, R16) — importing this module pulls in nothing heavy, so the U1
zoning-contract test can introspect ``SnapcastBackend`` without the lib
installed. All library surface is isolated behind ``_SnapcastControl`` +
injectable factories so a lib change is contained and tests use fakes.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
from typing import Any, Awaitable, Callable

from app.output.base import DeviceNotReadyError, OutputDevice
from app.output.flow import _default_resolve_source
from app.output.multiroom import MultiroomBackendBase, PcmFeed
from app.output.snapcast_server import SnapserverSupervisor
from app.models import Track

_log = logging.getLogger(__name__)

# Independent bound on the control connect so a firewalled/slow EXTERNAL
# snapserver can't hang the FastAPI worker running the enable toggle (the
# embedded path already has the supervisor's 30s readiness gate in front).
_CONNECT_TIMEOUT_S = 10.0


# ── control adapter (isolates the `snapcast` PyPI lib) ───────────────────────


class _SnapcastControl:
    """Thin async adapter over Home Assistant's ``snapcast`` control library.
    The library import is function-local (dormancy). Every method the backend
    needs is exposed here so the lib surface is one file wide."""

    def __init__(self, server: Any) -> None:
        self._server = server

    @classmethod
    async def connect(cls, host: str, port: int) -> "_SnapcastControl":
        import snapcast.control  # function-local: never imported until enabled
        loop = asyncio.get_running_loop()
        server = await snapcast.control.create_server(loop, host, port, reconnect=True)
        return cls(server)

    async def status(self) -> dict:
        """Refresh + return the raw server status tree (Server.GetStatus)."""
        return await self._server.status()

    def groups(self) -> list:
        return list(getattr(self._server, "groups", []) or [])

    def clients(self) -> list:
        return list(getattr(self._server, "clients", []) or [])

    async def client_set_volume(self, client_id: str, percent: int) -> None:
        # Preserve the client's current mute state — a volume change must not
        # silently un-mute a muted client.
        muted = bool(getattr(self._server.client(client_id), "muted", False))
        await self._server.client_volume(
            client_id, {"percent": int(percent), "muted": muted})

    async def client_set_muted(self, client_id: str, muted: bool) -> None:
        client = self._server.client(client_id)
        await client.set_muted(bool(muted))

    async def group_set_muted(self, group_id: str, muted: bool) -> None:
        group = self._server.group(group_id)
        await group.set_muted(bool(muted))

    async def group_set_clients(self, group_id: str, client_ids: list[str]) -> None:
        # Server-level Group.SetClients (verified against snapcast 2.3.8 — the
        # Snapgroup object has no set_clients; group_clients is the RPC wrapper).
        await self._server.group_clients(group_id, client_ids)

    async def group_set_name(self, group_id: str, name: str) -> None:
        group = self._server.group(group_id)
        await group.set_name(name)

    def set_on_update(self, cb: Callable[[], Any]) -> None:
        setter = getattr(self._server, "set_on_update_callback", None)
        if callable(setter):
            setter(cb)

    async def disconnect(self) -> None:
        stopper = getattr(self._server, "stop", None)
        if callable(stopper):
            res = stopper()
            if asyncio.iscoroutine(res):
                await res


async def _default_control_factory(host: str, port: int) -> _SnapcastControl:
    return await _SnapcastControl.connect(host, port)


# ── SSRF guard for the external host (backend-local, layering-safe) ──────────


async def _validate_external_host(host: str) -> None:
    """Resolve ``host`` off the loop and reject loopback/link-local always, and
    RFC-1918 private/unique-local unless ``ALLOW_PRIVATE_SOURCES``. Fails CLOSED:
    an unresolvable host raises. Mirrors admin.py's ``_validate_source_url`` but
    lives here so the backend never imports ``app/api/*`` and the check also
    covers the boot-from-persisted-config path (a required contract, not a
    code-review catch)."""
    from app.config import settings
    if not host:
        raise DeviceNotReadyError("no Snapcast host configured")
    addrs: list[str] = []
    try:
        ipaddress.ip_address(host)
        addrs = [host]
    except ValueError:
        try:
            loop = asyncio.get_running_loop()
            infos = await loop.run_in_executor(None, socket.getaddrinfo, host, None)
            addrs = sorted({info[4][0] for info in infos})
        except OSError as exc:
            raise DeviceNotReadyError(
                f"could not resolve Snapcast host {host!r} — refusing to connect "
                "(SSRF fail-closed)") from exc
    if not addrs:
        raise DeviceNotReadyError(f"could not resolve Snapcast host {host!r}")
    allow_private = settings.allow_private_sources
    for raw in addrs:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_link_local:
            raise DeviceNotReadyError(
                f"Snapcast host {host!r} resolves to a loopback/link-local "
                "address — blocked (cannot be overridden)")
        if not allow_private and ip.is_private:
            raise DeviceNotReadyError(
                f"Snapcast host {host!r} is on a private network — blocked unless "
                "ALLOW_PRIVATE_SOURCES=true")


# ── backend ──────────────────────────────────────────────────────────────────


class SnapcastBackend(MultiroomBackendBase):
    def __init__(
        self,
        advance_cb: Callable[[], Awaitable[Any]] | None = None,
        *,
        control_factory: Callable[[str, int], Awaitable[Any]] | None = None,
        supervisor_factory: Callable[[], SnapserverSupervisor] | None = None,
        feed_factory: Callable[..., PcmFeed] | None = None,
    ) -> None:
        super().__init__(advance_cb=advance_cb)
        self._control_factory = control_factory or _default_control_factory
        self._supervisor_factory = supervisor_factory or self._make_supervisor
        self._feed_factory = feed_factory or self._make_feed

        self._mode = "embedded"          # "embedded" | "external"
        self._external_host = ""
        self._external_port = 1705
        self._external_feed_url = ""     # tcp:// the external server's source listens on

        self._supervisor: SnapserverSupervisor | None = None
        self._control: Any = None
        self._connected = False

        # per-selection feed state
        self._feed: PcmFeed | None = None
        self._feed_task: asyncio.Task | None = None
        self._current_track: Track | None = None
        self._feed_gen = 0
        self._playback_started_at: float | None = None
        self._paused_at: float | None = None

        # U6: a zones-changed broadcast hook (U9 attaches the WS/event push). The
        # backend stays decoupled from the events bus — it only fires the hook.
        self._zones_changed_hook: Callable[[], Any] | None = None

    # ── config load (from settings) ─────────────────────────────────────────

    async def _load_config(self) -> None:
        from app import database
        mode = await database.get_setting("snapcast_mode")
        self._mode = mode if mode in ("embedded", "external") else "embedded"
        self._external_host = (await database.get_setting("snapcast_external_host")) or ""
        port = await database.get_setting("snapcast_external_port")
        try:
            self._external_port = int(port) if port else 1705
        except ValueError:
            self._external_port = 1705
        self._external_feed_url = (
            await database.get_setting("snapcast_external_feed_url")) or ""

    def _make_supervisor(self) -> SnapserverSupervisor:
        return SnapserverSupervisor(source_name="jukeplox")

    def _make_feed(self, source: str, headers: dict | None, *, sink: str) -> PcmFeed:
        return PcmFeed(source, headers, sink=sink, consume_stdout=False,
                       realtime=True, label="snapcast-feed")

    # ── enable / disable (U2 activation contract) ───────────────────────────

    async def enable(self) -> None:
        """Start the embedded snapserver (or validate+connect an external one)
        and open the control connection. Fails closed: any error tears down
        partial state and re-raises so the U2/U9 toggle latches 'failed'."""
        if self._connected:
            return  # already running — a double enable must not double-start
                    # the supervisor (EADDRINUSE) or the control connection.
        await self._load_config()
        try:
            if self._mode == "embedded":
                self._supervisor = self._supervisor_factory()
                await self._supervisor.start()
                host, port = self._supervisor.control_host, self._supervisor.control_port
            else:
                await _validate_external_host(self._external_host)
                host, port = self._external_host, self._external_port
            # Bound the control connect independently of the supervisor's
            # readiness gate — a firewalled/slow EXTERNAL snapserver must not
            # hang the worker running the enable toggle.
            self._control = await asyncio.wait_for(
                self._control_factory(host, port), timeout=_CONNECT_TIMEOUT_S)
            self._control.set_on_update(self._on_control_update)
            await self._control.status()  # prime the tree
            self._connected = True
        except BaseException:
            await self._safe_teardown()
            raise

    async def disable(self) -> None:
        await self.stop()
        await self._safe_teardown()

    async def _safe_teardown(self) -> None:
        self._connected = False
        if self._control is not None:
            try:
                await self._control.disconnect()
            except Exception:
                _log.warning("snapcast: control disconnect failed", exc_info=True)
            self._control = None
        if self._supervisor is not None:
            try:
                await self._supervisor.stop()
            except Exception:
                _log.warning("snapcast: supervisor stop failed", exc_info=True)
            self._supervisor = None

    def set_zones_changed_hook(self, cb: Callable[[], Any] | None) -> None:
        """Register a callback fired when the zone tree changes (Server.OnUpdate
        or a granular client/group event). U9 attaches the WS broadcast."""
        self._zones_changed_hook = cb

    def _on_control_update(self, *args: Any) -> None:
        """Snapserver pushed a Server.OnUpdate / granular event. Echo-guard: a
        change we just initiated (within the volume echo window) is our own write
        confirming — suppress it so admin sliders don't snap back. Otherwise fire
        the zones-changed hook so U9 rebroadcasts the refreshed tree."""
        if self._echo_guard_active():
            return
        hook = self._zones_changed_hook
        if hook is None:
            return
        try:
            res = hook()
            if asyncio.iscoroutine(res):
                loop = asyncio.get_running_loop()
                loop.create_task(res)
        except RuntimeError:
            pass  # no running loop
        except Exception:
            _log.warning("snapcast: zones-changed hook failed", exc_info=True)

    # ── feed target ─────────────────────────────────────────────────────────

    def _feed_sink(self) -> str:
        if self._mode == "embedded" and self._supervisor is not None:
            return self._supervisor.source_feed_url
        # External: the operator-configured tcp source URL on their snapserver.
        return self._external_feed_url

    # ── playback (AbstractOutputBackend) ────────────────────────────────────

    async def play(self, stream_url: str, metadata: Track, *,
                   start_offset_ms: int = 0) -> None:
        if not self._connected:
            raise DeviceNotReadyError("Snapcast backend is not enabled/connected")
        # Grab the confirmation token BEFORE any await so a slow resolve can't
        # let a later dispatch's token overwrite it.
        self._capture_confirm_token()
        sink = self._feed_sink()
        if not sink:
            # External without a configured feed endpoint can't be fed.
            raise DeviceNotReadyError(
                "no Snapcast feed endpoint — configure the external server's tcp "
                "source URL, or use the embedded server")
        await self._teardown_feed()
        resolved = await _default_resolve_source(metadata)
        if resolved is None:
            # Unresolvable source — let the caller skip (return without playing).
            _log.warning("snapcast: could not resolve source for %r",
                         getattr(metadata, "title", "?"))
            raise DeviceNotReadyError("could not resolve the track source")
        source, headers = resolved
        self._current_track = metadata
        await self._spawn_feed(source, headers, sink, start_offset_ms)

    async def _spawn_feed(self, source: str, headers: dict | None, sink: str,
                          start_offset_ms: int) -> None:
        self._feed_gen += 1
        gen = self._feed_gen
        feed = self._feed_factory(source, headers, sink=sink)
        self._feed = feed
        await feed.start()
        self._playback_started_at = time.monotonic() - (start_offset_ms / 1000)
        self._paused_at = None
        self._is_playing = True
        # Confirmed-start proxy: the feed is up (ffmpeg spawned, writing to the
        # snapserver source). A 0-client backend is audibly silent but NOT an
        # outage (R15) — confirming here satisfies the supervisor's deadline so
        # healthy playback isn't misclassified.
        self._confirm_started()
        self._feed_task = asyncio.get_running_loop().create_task(
            self._watch_feed(gen, feed))

    async def _watch_feed(self, gen: int, feed: PcmFeed) -> None:
        """Own the feed's exit. Runs on the loop (a background task). A clean EOF
        at track end advances the queue on a FRESH task (never inline — that
        would re-enter play() → cancel this very task); a mid-track feed death
        holds via the supervisor's outage path (never a detached ``raise`` — that
        exception is unobserved, so the hold would never fire)."""
        try:
            rc = await feed.wait()
        except asyncio.CancelledError:
            return
        if gen != self._feed_gen or not self._is_playing:
            return  # superseded / stopped
        self._is_playing = False
        # A finite track's ffmpeg exits 0 at source EOF = the track ended.
        if self.classify_feed_exit(rc, expected_end=(rc == 0)) == "advance":
            self._spawn_advance()
        else:
            _log.warning("snapcast: feed died mid-track (rc=%s) — holding "
                         "(outage), not draining the queue", rc)
            self._notify_feed_outage("snapcast_feed_failed")

    async def pause(self) -> None:
        if self._paused_at is None and self._is_playing:
            self._paused_at = time.monotonic()
        await self._teardown_feed(keep_playing_flag=True)

    async def resume(self) -> None:
        if self._current_track is None or self._paused_at is None:
            return
        sink = self._feed_sink()
        resolved = await _default_resolve_source(self._current_track)
        if resolved is None:
            return
        source, headers = resolved
        held_ms = self.get_position_sync()
        self._paused_at = None
        await self._spawn_feed(source, headers, sink, held_ms)

    async def stop(self) -> None:
        self._is_playing = False
        self._current_track = None
        self._paused_at = None
        await self._teardown_feed()

    async def _teardown_feed(self, *, keep_playing_flag: bool = False) -> None:
        self._feed_gen += 1  # invalidate the in-flight watcher
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
                _log.warning("snapcast: feed close failed", exc_info=True)

    # ── position / volume ────────────────────────────────────────────────────

    def get_position_sync(self) -> int:
        if self._playback_started_at is None:
            return 0
        end = self._paused_at if self._paused_at is not None else time.monotonic()
        return max(0, int((end - self._playback_started_at) * 1000))

    async def get_position(self) -> int:
        return self.get_position_sync()

    async def seek(self, position_ms: int) -> None:
        if self._current_track is None:
            return
        sink = self._feed_sink()
        resolved = await _default_resolve_source(self._current_track)
        if resolved is None:
            return
        source, headers = resolved
        await self._teardown_feed(keep_playing_flag=True)
        await self._spawn_feed(source, headers, sink, max(0, position_ms))

    async def set_volume(self, level: float) -> None:
        """Master volume → per-client proportional redistribution across ALL
        connected clients (there is no wire Group.SetVolume — it's a client
        fan-out). Stamps the echo guard before writing."""
        level = max(0.0, min(1.0, level))
        self._volume = level
        if not self._connected or self._control is None:
            return
        clients = self._control.clients()
        if not clients:
            return
        current = [self._client_volume(c) / 100.0 for c in clients]
        targets = self.redistribute(current, level, lo=0.0, hi=1.0)
        self._stamp_volume_write()
        for c, tgt in zip(clients, targets):
            try:
                await self._control.client_set_volume(
                    self._client_id(c), int(round(tgt * 100)))
            except Exception:
                _log.warning("snapcast: client volume set failed", exc_info=True)

    # ── device enumeration (control-RPC, NOT mDNS) ──────────────────────────

    async def discover_devices(self) -> list[OutputDevice]:
        """Connected snapclients, from the control tree — NOT mDNS. Surfaced for
        the zoning UI (U9), not the device picker (server-fed clients are
        zone-managed). Returns [] when disabled/zero-clients (R15: not an error)."""
        if not self._connected or self._control is None:
            return []
        # Read the cached control tree — `set_on_update` (registered in enable())
        # keeps it fresh on every server-pushed event, so no per-call status()
        # RPC is needed (this runs on every admin integrations poll).
        return [
            OutputDevice(id=self._client_id(c), name=self._client_name(c),
                         backend_type="snapcast", id_format="uuid")
            for c in self._control.clients()
        ]

    async def set_device(self, device_id: str) -> None:
        # Server-fed: "device" selection is the backend itself; per-client
        # targeting is zoning (U6), not a set_device. No-op by contract.
        return None

    # ── zoning contract (U1) — full behavior hardened in U6 ─────────────────

    def supports_zoning(self) -> bool:
        return self._connected

    def can_manage_topology(self) -> bool:
        """Embedded server → full group management; external → read + assign
        between existing groups only (never destroy an operator's topology)."""
        return self._mode == "embedded"

    async def list_zones(self) -> list[dict]:
        if not self._connected or self._control is None:
            return []
        # Cached tree (kept fresh by set_on_update) — no per-call status() RPC.
        zones = []
        for g in self._control.groups():
            zones.append({
                "group_id": self._group_id(g),
                "muted": bool(getattr(g, "muted", False)),
                "clients": [
                    {
                        "client_id": self._client_id(c),
                        "name": self._client_name(c),
                        "volume": self._client_volume(c) / 100.0,
                        "muted": bool(getattr(c, "muted", False)),
                    }
                    for c in getattr(g, "clients", []) or []
                ],
            })
        return zones

    async def set_client_volume(self, client_id: str, level: float) -> None:
        self._stamp_volume_write()
        await self._control.client_set_volume(
            client_id, int(round(max(0.0, min(1.0, level)) * 100)))

    async def set_client_mute(self, client_id: str, muted: bool) -> None:
        await self._control.client_set_muted(client_id, muted)

    async def set_group_mute(self, group_id: str, muted: bool) -> None:
        await self._control.group_set_muted(group_id, muted)

    async def set_group_volume(self, group_id: str, level: float) -> None:
        """Group volume = proportional client fan-out (no wire Group.SetVolume)."""
        group = self._find_group(group_id)
        if group is None:
            return
        clients = list(getattr(group, "clients", []) or [])
        if not clients:
            return
        current = [self._client_volume(c) / 100.0 for c in clients]
        targets = self.redistribute(current, max(0.0, min(1.0, level)), lo=0.0, hi=1.0)
        self._stamp_volume_write()
        for c, tgt in zip(clients, targets):
            await self._control.client_set_volume(
                self._client_id(c), int(round(tgt * 100)))

    async def assign_client_to_group(self, client_id: str, group_id: str) -> None:
        """Move a client into a group (Group.SetClients). Allowed on both
        embedded and external (assign-between-existing is non-destructive)."""
        group = self._find_group(group_id)
        if group is None:
            return
        existing = [self._client_id(c) for c in getattr(group, "clients", []) or []]
        if client_id not in existing:
            existing.append(client_id)
        await self._control.group_set_clients(group_id, existing)

    # ── topology management (embedded ONLY — never on an external server) ────

    def _require_topology(self) -> None:
        if not self.can_manage_topology():
            raise PermissionError(
                "topology changes (create/delete/rename) are not permitted on an "
                "external Snapcast server — read + assign-between-existing only")

    async def rename_group(self, group_id: str, name: str) -> None:
        self._require_topology()
        await self._control.group_set_name(group_id, name)

    async def dissolve_group(self, group_id: str) -> None:
        """Delete a group by moving its clients out (snapserver has no explicit
        'delete group' — an empty group disappears). Embedded only."""
        self._require_topology()
        await self._control.group_set_clients(group_id, [])

    # ── control-tree accessors (tolerant of dict/attr shapes) ───────────────

    def _find_group(self, group_id: str) -> Any:
        for g in self._control.groups():
            if self._group_id(g) == group_id:
                return g
        return None

    @staticmethod
    def _client_id(c: Any) -> str:
        return str(getattr(c, "identifier", None) or getattr(c, "id", ""))

    @staticmethod
    def _client_name(c: Any) -> str:
        return str(getattr(c, "friendly_name", None) or getattr(c, "name", "")
                   or "snapclient")

    @staticmethod
    def _client_volume(c: Any) -> int:
        return int(getattr(c, "volume", 0) or 0)

    @staticmethod
    def _group_id(g: Any) -> str:
        return str(getattr(g, "identifier", None) or getattr(g, "id", ""))
