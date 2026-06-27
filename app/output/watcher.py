"""Live output-device registry + avahi subscription watcher.

2026-06-11 live-discovery plan U2 (KTD1/KTD3/KTD5/KTD9/KTD10): a singleton
service, started from the FastAPI lifespan after state init, that owns

- persistent in-process mDNS subscriptions (``_raop._tcp.local`` → airplay
  via app/output/mdns_zeroconf.py, ``_googlecast._tcp.local`` → chromecast
  via the CastBrowser bridge) on the shared AsyncZeroconf (2026-06-15 plan
  U2/U3; no avahi/D-Bus);
- the in-memory device registry — entries keyed ``(backend, device_id)``,
  retained when devices go offline (greyed out, never vanishing) until a
  forced Scan reconcile drops the still-absent ones (KTD3, origin R2/R6);
- grace timers (a device must be gone GRACE_S before it flips offline, so
  mDNS flaps emit nothing);
- the U3 DLNA sweep (jittered SWEEP_S discover_devices passes, KTD4) plus
  an opportunistic — never load-bearing — SSDP alive/byebye listener;
- the debounced ``devices_changed`` broadcast to ADMIN websockets (KTD5).

Nothing else mutates the registry. Start is fail-soft on every path: no
avahi → the watcher idles in degraded mode (``running`` is False, the
object stays queryable) and the legacy pull flow keeps working.

Clock, timers, subscribe/unsubscribe, backend lookup, snapshot builder,
broadcast sink and probe trigger are all constructor-injectable so tests
drive the full state machine without sleeping or touching D-Bus.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from app.events.types import DevicesChangedEvent
from app.output.base import OutputDevice

_log = logging.getLogger(__name__)

# Seconds a device must stay gone after ItemRemove before it flips offline.
# Some receivers churn their mDNS records on the way to renewing them; the
# grace window swallows those flaps (KTD3). Tune by feel on the live host.
GRACE_S = 15.0

# DLNA sweep period (U3, KTD4). DLNA has no push channel we can rely on
# (the SSDP NOTIFY listener below is opportunistic and often loses the
# 1900 bind to another UPnP stack on the host), so a periodic
# discover_devices() pass is the deterministic baseline. 75s balances
# freshness against M-SEARCH chatter on the LAN; each cycle is jittered
# ±20% so multiple jukeplox instances on one network don't synchronize
# their multicast bursts.
SWEEP_S = 75.0
_SWEEP_JITTER = (0.8, 1.2)

# SSDP multicast group — the opportunistic alive/byebye listener (KTD4)
# joins it at start; on any bind/join failure the watcher logs once and
# stays sweep-only (the 5353 lesson: a bind failure must never take the
# feature down).
_SSDP_ADDR = "239.255.255.250"
_SSDP_PORT = 1900

# Trailing-edge debounce for the devices_changed broadcast. A Chromecast
# announces several records at once; one frame per burst, not per record
# (KTD5).
DEBOUNCE_S = 0.75

# Auto-remove (2026-06-15 passive-discovery plan U4). Once an entry flips
# offline (grace expired), it is greyed-retained for a purge window, then
# evicted — so the menu self-maintains without a manual Scan (origin R6 →
# grey-then-auto-remove). Idle entries purge after PURGE_S; the ACTIVE output
# gets the longer ACTIVE_PURGE_S so a brief drop of the device you're playing
# to does not evict it mid-use (origin AE4). Exact values are feel-tunable on
# the live host; both are constructor-injectable for tests.
PURGE_S = 300.0           # 5 min — idle offline entry
ACTIVE_PURGE_S = 1800.0   # 30 min — active-output offline entry

# avahi service type → backend name. Subscriptions exist only for the two
# mDNS backends; DLNA joins the registry via the U3 sweep + SSDP listener,
# Direct never enters it (appended as a pseudo-device at aggregation time).
_SERVICE_BACKENDS: dict[str, str] = {
    "_raop._tcp.local": "airplay",
    "_googlecast._tcp.local": "chromecast",
}

# Shape-compatible with admin.py's _MdnsStatus map ("ok" | "unavailable"
# per backend) so U5 can source /output/active's mdns_status from here
# without translating.
_ALL_BACKENDS = ("direct", "airplay", "chromecast", "dlna")


@dataclass
class RegistryEntry:
    """One known output device, online or retained-offline (KTD3)."""
    device: OutputDevice
    online: bool = True
    last_seen: float = 0.0            # monotonic (watcher clock)
    offline_since: float | None = None  # wall clock, for "offline since" UI
    # Raw announced mDNS instance name — ItemRemove events key by this, not
    # by device_id, so the watcher must remember the mapping. Empty for
    # entries that arrived via reconcile() (one-shot discovers carry only
    # display names); such entries gain a name the next time avahi
    # announces them.
    avahi_name: str = ""


class DeviceWatcher:
    """Owns the live registry, avahi subscriptions and admin broadcasts.

    Public surface (U3 sweep / U4 probes / U5 snapshot+Scan build on this):

    - ``await start()`` / ``await stop()`` — lifespan hooks, fail-soft.
    - ``running`` — True while at least one avahi subscription is live.
    - ``registry`` — read-only-by-convention dict
      ``(backend, device_id) → RegistryEntry``; only this class mutates it.
    - ``mdns_status()`` — per-backend availability map (admin.py shape).
    - ``reconcile(found)`` — forced-Scan merge: upserts found devices,
      drops offline ghosts absent from the scan, returns whether anything
      changed (U5 calls this from the ``?force=1`` path).
    """

    def __init__(
        self,
        *,
        snapshot: Callable[[], Any] | None = None,
        broadcast: Callable[[DevicesChangedEvent], Awaitable[None]] | None = None,
        subscribe: Callable[..., Awaitable[Any]] | None = None,
        unsubscribe: Callable[[Any], Awaitable[None]] | None = None,
        backend_for: Callable[[str], Any] | None = None,
        probe: Callable[[str, str, str], None] | None = None,
        timer: Callable[[float, Callable[[], None]], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        grace_s: float = GRACE_S,
        debounce_s: float = DEBOUNCE_S,
        sweep_s: float = SWEEP_S,
        purge_s: float = PURGE_S,
        active_purge_s: float = ACTIVE_PURGE_S,
        active_key_for: Callable[[], tuple[str, str] | None] | None = None,
        rand: Callable[[float, float], float] = random.uniform,
        ssdp_listen: Callable[..., Awaitable[Any]] | None = None,
        dbus_available: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        # Injection points — every default reaches the real collaborators
        # lazily (function-level imports) so constructing a watcher in
        # tests never drags in D-Bus or the websocket manager.
        self._snapshot = snapshot or self._default_snapshot
        self._broadcast = broadcast or self._default_broadcast
        self._subscribe_fn = subscribe or self._default_subscribe
        self._unsubscribe_fn = unsubscribe or self._default_unsubscribe
        self._backend_for = backend_for or self._default_backend_for
        self._probe = probe or self._default_probe
        self._timer = timer or self._default_timer
        self._clock = clock
        self._wall_clock = wall_clock
        self._grace_s = grace_s
        self._debounce_s = debounce_s
        self._sweep_s = sweep_s
        self._purge_s = purge_s
        self._active_purge_s = active_purge_s
        self._active_key_for = active_key_for or self._default_active_key
        self._rand = rand
        self._ssdp_listen_fn = ssdp_listen or self._default_ssdp_listen
        self._dbus_available_fn = dbus_available or self._default_dbus_available

        self.registry: dict[tuple[str, str], RegistryEntry] = {}
        # (backend, avahi_name) → registry key. ItemRemove carries only the
        # announced instance name; this index translates it back to the
        # (backend, device_id) registry key.
        self._name_index: dict[tuple[str, str], tuple[str, str]] = {}
        self._grace_timers: dict[tuple[str, str], Any] = {}
        # Auto-remove purge timers (U4): armed when an entry flips offline,
        # fire to evict it. Torn down in stop() alongside _grace_timers (the
        # watcher's no-leak invariant, AE5).
        self._purge_timers: dict[tuple[str, str], Any] = {}
        self._debounce_handle: Any = None
        # In-flight broadcast tasks; tracked so stop() can drain them (AE5
        # — no leaked tasks) and so exceptions are logged, never swallowed.
        self._tasks: set[asyncio.Task] = set()
        self._handles: dict[str, Any] = {}  # backend → subscription handle
        self._mdns_status: dict[str, str] = {b: "ok" for b in _ALL_BACKENDS}
        # U3 DLNA state: the pending sweep timer handle, the SSDP NOTIFY
        # transport (None when the opportunistic listener didn't bind),
        # and the ids with an in-flight alive-verification (so a device
        # announcing alive three times in one burst — the SSDP norm —
        # fetches its description once, not three times).
        self._sweep_handle: Any = None
        self._ssdp_transport: Any = None
        self._ssdp_inflight: set[str] = set()
        # True when the in-process 5353 bind failed (a host avahi owns it)
        # AND a browsable D-Bus daemon is reachable: the watcher's live
        # Cast/AirPlay view is then the periodic D-Bus sweep, NOT a
        # subscription. running/mdns_status read this so they reflect the
        # sweep instead of the (absent) subscription handles. Set in start().
        self._mdns_sweep_active = False
        self._started = False
        self._stopped = False

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Open the avahi subscriptions. Fail-soft on every path.

        A backend whose subscribe() returns None (avahi absent, PyGObject
        missing, setup failure) is marked "unavailable" in mdns_status and
        the watcher simply has no live view for it — degraded mode, the
        legacy pull flow still works (origin R5).
        """
        if self._started:
            return
        self._started = True
        self._stopped = False
        for service_type, backend in _SERVICE_BACKENDS.items():
            handle = None
            try:
                handle = await self._subscribe_fn(
                    service_type, self._make_on_event(backend)
                )
            except Exception:
                # subscribe() is documented never-raises, but the watcher
                # must survive even a broken injection (fail-soft, KTD1).
                _log.warning("device watcher: subscribe(%s) failed",
                             service_type, exc_info=True)
            if handle is not None:
                self._handles[backend] = handle
            else:
                self._mdns_status[backend] = "unavailable"
        # Sweep-mode marker: when the in-process 5353 bind failed (a host
        # avahi owns it) there is no live Cast/AirPlay subscription — the
        # periodic D-Bus sweep below IS the live mDNS view. Mark it so
        # running/mdns_status reflect the sweep, but ONLY when a browsable
        # D-Bus daemon is actually reachable; otherwise discovery genuinely
        # is unavailable and the banner must keep saying so.
        from app import state
        if state.shared_aiozc is None:
            self._mdns_sweep_active = await self._dbus_available_fn()
            if self._mdns_sweep_active:
                # The sweep provides these even though subscribe() returned
                # no handle — clear the "unavailable" the loop just set.
                for b in ("airplay", "chromecast"):
                    self._mdns_status[b] = "ok"
        if not self._handles and not self._mdns_sweep_active:
            _log.warning(
                "device watcher: Cast/AirPlay discovery unavailable — "
                "running degraded (pull-only discovery)"
            )
        # The DLNA sweep (and the opportunistic SSDP listener) start whenever
        # the watcher started, full stop — DLNA needs no avahi, so even
        # avahi-degraded mode keeps a live DLNA view. In sweep mode the same
        # pass also discovers Cast/AirPlay over D-Bus. The first sweep fires
        # immediately (delay 0) so devices appear within one discover window
        # instead of after the first jittered SWEEP_S (~60-90s) — the "no
        # devices until a manual Scan" symptom; _run_sweep re-arms jittered.
        self._schedule_sweep(immediate=True)
        await self._start_ssdp_listener()

    async def stop(self) -> None:
        """Tear everything down: timers, debounce, tasks, subscriptions.

        Safe to call on a never-started or degraded watcher. After stop()
        no timer, task or subscription survives (AE5 — no leaks).
        """
        self._stopped = True
        self._started = False
        if self._debounce_handle is not None:
            self._debounce_handle.cancel()
            self._debounce_handle = None
        # U3 leak hygiene: cancel the pending sweep timer and close the
        # SSDP listener BEFORE draining tasks, so neither can spawn a new
        # sweep/verify task while we're gathering the old ones.
        if self._sweep_handle is not None:
            self._sweep_handle.cancel()
            self._sweep_handle = None
        if self._ssdp_transport is not None:
            try:
                self._ssdp_transport.close()
            except Exception:
                _log.debug("device watcher: SSDP transport close failed",
                           exc_info=True)
            self._ssdp_transport = None
        for handle in self._grace_timers.values():
            handle.cancel()
        self._grace_timers.clear()
        # U4: pending auto-remove timers must die with the watcher too.
        for handle in self._purge_timers.values():
            handle.cancel()
        self._purge_timers.clear()
        # Cancel in-flight broadcasts BEFORE unsubscribing so a snapshot
        # mid-build never observes half-torn-down state.
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._mdns_sweep_active = False
        handles = list(self._handles.values())
        self._handles.clear()
        for handle in handles:
            try:
                await self._unsubscribe_fn(handle)
            except Exception:
                _log.debug("device watcher: unsubscribe failed", exc_info=True)

    @property
    def running(self) -> bool:
        """True while a live Cast/AirPlay discovery view exists — either at
        least one in-process subscription handle, OR the D-Bus sweep is the
        live view (in-process 5353 bind failed but a browsable D-Bus daemon
        is reachable; see _mdns_sweep_active).

        False in fully-degraded mode (no subscriptions AND no reachable
        D-Bus) and after stop() — the route uses this to choose
        registry-snapshot vs legacy-pull serving.

        Note: `running` stays Cast/AirPlay-only ON PURPOSE. The DLNA sweep
        runs whenever the watcher started (even fully degraded), so on a
        host with no mDNS path the registry can hold live DLNA entries while
        `running` is False — but the legacy pull path the route falls back
        to runs the same one-shot discovers (DLNA included), so gating the
        whole snapshot-vs-pull choice here loses nothing and keeps the
        route's decision a single boolean.
        """
        return (bool(self._handles) or self._mdns_sweep_active) and not self._stopped

    # ── public queries / reconcile ────────────────────────────────────────────

    def mdns_status(self) -> dict[str, str]:
        """Per-backend availability map, admin.py `_MdnsStatus`-shaped, plus a
        ``discovery`` health key.

        'status' subscription events flip the mDNS backends; Direct and DLNA
        stay "ok" — the U3 sweep is always-on (no daemon that can be absent),
        so a failed discover pass is transient noise, not an outage worth a
        banner.

        ``discovery`` is the degraded-state signal: "ok" while a Cast/AirPlay
        source is live — EITHER an in-process subscription handle OR the
        avahi/D-Bus sweep (_mdns_sweep_active) — else "unavailable". The
        watcher reaches degraded-no-source only when the shared AsyncZeroconf
        could not bind 5353 AND no browsable D-Bus daemon is reachable. The
        admin banner maps "unavailable" to actionable guidance (host
        networking, or mount the D-Bus socket) instead of a silent empty
        menu. Returns a copy so callers can't mutate watcher state.
        """
        status = dict(self._mdns_status)
        status["discovery"] = (
            "ok" if (self._handles or self._mdns_sweep_active) else "unavailable")
        return status

    def reconcile(self, found: dict[str, list[OutputDevice]]) -> bool:
        """Merge a forced Scan's one-shot results into the registry (U5).

        Per backend present in *found* (absent backends are untouched, so
        a DLNA-only sweep can never evict mDNS entries — origin AE4):

        - every found device is upserted online (grace timer cancelled,
          offline_since cleared);
        - OFFLINE entries absent from the scan are dropped — the Scan
          reconcile is the only eviction path (origin R6). Online entries
          absent from the scan are KEPT: the live subscription says they
          exist, and one-shot windows routinely miss late announcers.

        Returns True when anything changed (and schedules the debounced
        broadcast so open admin pages converge without a reload).
        """
        changed = False
        now = self._clock()
        for backend, devices in found.items():
            found_ids: set[str] = set()
            for device in devices:
                found_ids.add(device.id)
                key = (backend, device.id)
                self._cancel_grace(key)
                self._cancel_purge(key)  # U4: re-seen → cancel pending eviction
                entry = self.registry.get(key)
                if entry is None:
                    self.registry[key] = RegistryEntry(
                        device=device, online=True, last_seen=now)
                    changed = True
                else:
                    if not entry.online or entry.device != device:
                        changed = True
                    entry.device = device
                    entry.online = True
                    entry.last_seen = now
                    entry.offline_since = None
            for key in [k for k in self.registry if k[0] == backend]:
                if key[1] in found_ids:
                    continue
                entry = self.registry[key]
                if entry.online:
                    continue  # live view trumps a missed one-shot window
                self._cancel_grace(key)
                self._cancel_purge(key)  # U4: evicting now — retire its timer
                del self.registry[key]
                self._drop_name_index_for(key)
                changed = True
        if changed:
            self._schedule_broadcast()
        return changed

    # ── subscription event handling ───────────────────────────────────────────

    def _make_on_event(self, backend: str) -> Callable[[str, Any], None]:
        """Bind *backend* into the source's on_event callback.

        'status' payloads carry no service type, so the backend must be
        captured per subscription. The callback runs on the asyncio loop
        (the in-process sources cross threads via call_soon_threadsafe); the
        blanket except keeps a malformed event from reaching the loop's
        exception handler (fail-soft).
        """
        def _on_event(kind: str, payload: Any) -> None:
            try:
                self._dispatch(backend, kind, payload)
            except Exception:
                _log.warning("device watcher: %s event for %s failed",
                             kind, backend, exc_info=True)
        return _on_event

    def _dispatch(self, backend: str, kind: str, payload: Any) -> None:
        if self._stopped:
            return  # late event from an in-flight resolve after stop()
        if kind == "new":
            self._on_new(backend, payload)
        elif kind == "remove":
            self._on_remove(backend, payload)
        elif kind == "status":
            self._on_status(backend, payload)

    def _on_new(self, backend: str, payload: Any) -> None:
        """Resolved arrival: upsert online, feed the backend cache (KTD9)."""
        name, host, port, uuid, txt, _service_type = payload
        device = self._register_with_backend(backend, name, host, port, uuid, txt)
        key = (backend, device.id)
        self._name_index[(backend, name)] = key
        self._apply_arrival(key, device, avahi_name=name, probe_host=host)

    def _on_remove(self, backend: str, payload: Any) -> None:
        """Announced departure: start the grace timer, flip later (KTD3)."""
        name, _service_type = payload
        key = self._name_index.get((backend, name))
        if key is None:
            return  # never saw (or already reconciled away) this name
        self._start_grace(key)

    # ── shared registry transitions ───────────────────────────────────────────
    # U3 extracted these from the avahi-only _on_new/_on_remove so every
    # live source — mDNS resolves, DLNA sweep hits, SSDP alive/byebye —
    # funnels through ONE transition pair and the grace/flap/probe
    # semantics cannot drift between paths (plan U3: one transition,
    # source-tagged by its arguments; sweep entries simply carry no
    # avahi_name).

    def _apply_arrival(
        self, key: tuple[str, str], device: OutputDevice,
        *, avahi_name: str = "", probe_host: str | None = None,
    ) -> None:
        """Online-upsert for a device a live source just saw.

        Reappearance within grace cancels the pending offline flip — a
        flap must change nothing, broadcast nothing and re-probe nothing
        (the entry never went offline).
        """
        self._cancel_grace(key)
        self._cancel_purge(key)  # U4: a returning device must not be purged
        entry = self.registry.get(key)
        if entry is None:
            self.registry[key] = RegistryEntry(
                device=device, online=True, last_seen=self._clock(),
                avahi_name=avahi_name)
            changed = arrived = True
        else:
            arrived = not entry.online  # offline→online return (grace expired)
            changed = arrived or entry.device != device
            entry.device = device
            entry.online = True
            entry.last_seen = self._clock()
            entry.offline_since = None
            if avahi_name:
                entry.avahi_name = avahi_name
        if changed:
            self._schedule_broadcast()
        if arrived and probe_host:
            # U4 probe-on-arrival (R4): new and returning devices verify
            # their Via options hands-off. Probes only write Via verdicts
            # (probe_cache) — a failure NEVER removes the registry entry —
            # and the trigger itself is fail-soft: a broken injection must
            # not take down event handling. probe_host can be None on the
            # DLNA paths (no cached LOCATION → nothing to probe against).
            try:
                self._probe(probe_host, key[0], key[1])
            except Exception:
                _log.warning("device watcher: probe trigger failed for %s/%s",
                             key[0], key[1], exc_info=True)

    def _start_grace(self, key: tuple[str, str]) -> None:
        """Arm the offline grace timer for *key* (KTD3). No-ops for
        unknown keys, already-offline entries (retained ghosts — only a
        forced Scan reconcile evicts) and keys already counting down
        (duplicate removes / consecutive sweep misses share one timer)."""
        entry = self.registry.get(key)
        if entry is None or not entry.online:
            return  # already offline — nothing to grace
        if key in self._grace_timers:
            return  # first timer already counting
        self._grace_timers[key] = self._timer(
            self._grace_s, lambda k=key: self._grace_expired(k))

    def _grace_expired(self, key: tuple[str, str]) -> None:
        """Grace window elapsed without reappearance → offline, RETAINED, and
        armed for auto-removal (U4)."""
        self._grace_timers.pop(key, None)
        entry = self.registry.get(key)
        if entry is None or not entry.online:
            return
        entry.online = False
        entry.offline_since = self._wall_clock()  # wall time: rendered in UI
        self._schedule_broadcast()
        self._arm_purge(key)

    def _arm_purge(self, key: tuple[str, str]) -> None:
        """Start the auto-remove timer for an entry that just went offline.

        The active output gets the longer ACTIVE_PURGE_S so a brief drop of
        the device you're playing to is not evicted mid-use before its window
        (AE4); idle entries get PURGE_S. One timer per key — a re-arm is a
        no-op so consecutive offline transitions can't stack timers."""
        if key in self._purge_timers:
            return
        ttl = self._active_purge_s if self._is_active_key(key) else self._purge_s
        self._purge_timers[key] = self._timer(
            ttl, lambda k=key: self._purge_expired(k))

    def _purge_expired(self, key: tuple[str, str]) -> None:
        """Purge window elapsed while still offline → evict (the eviction path
        a forced Scan reconcile() also uses). A device that came back online
        during the window is kept — the arrival cancels the timer, but guard
        here too against a late fire."""
        self._purge_timers.pop(key, None)
        entry = self.registry.get(key)
        if entry is None or entry.online:
            return
        self._cancel_grace(key)
        del self.registry[key]
        self._drop_name_index_for(key)
        self._schedule_broadcast()

    def _is_active_key(self, key: tuple[str, str]) -> bool:
        """True when *key* is the currently-active output. Fail-soft: a broken
        active_key_for must not stop the purge (it just uses the idle TTL)."""
        try:
            return self._active_key_for() == key
        except Exception:
            _log.warning("device watcher: active_key_for failed", exc_info=True)
            return False

    def _on_status(self, backend: str, payload: Any) -> None:
        """Source 'down'/'up' → mdns_status flip + broadcast.

        The DevicesChangedEvent carries mdns_status, so admin pages learn
        of a discovery outage (and its recovery) live — the same debounced
        frame the device list uses.
        """
        status = "ok" if payload == "up" else "unavailable"
        if self._mdns_status.get(backend) == status:
            return
        self._mdns_status[backend] = status
        self._schedule_broadcast()

    def _register_with_backend(
        self, backend: str, name: str, host: str, port: int,
        uuid: str | None, txt: dict[str, str],
    ) -> OutputDevice:
        """KTD9/KTD10: the backend's own hook builds the device AND feeds
        its address cache, so registry ids equal backend-cache ids by
        construction (chromecast: uuid when the TXT carried one, else
        host:port; airplay: always host:port).

        Backend missing (state not set up, or a test without that fake)
        → fall back to the same normalization without a cache write.
        """
        backend_obj = self._backend_for(backend)
        if backend_obj is not None and hasattr(backend_obj, "register_resolved"):
            return backend_obj.register_resolved(name, host, port, uuid, txt)
        if backend == "chromecast" and uuid:
            return OutputDevice(id=uuid, name=name, backend_type=backend,
                                id_format="uuid")
        return OutputDevice(id=f"{host}:{port}", name=name,
                            backend_type=backend, id_format="host_port")

    # ── DLNA sweep (U3, KTD4) ─────────────────────────────────────────────────

    def _schedule_sweep(self, *, immediate: bool = False) -> None:
        """Arm the next sweep, jittered ±20% around SWEEP_S (see the
        constant's comment for why). One handle at a time — the next
        cycle is armed only after the previous discover pass finishes,
        so a slow SSDP window can never stack concurrent sweeps.

        ``immediate`` arms with delay 0 for the first sweep at startup so
        discovery results appear within one discover window rather than
        after a full jittered cycle; subsequent re-arms (from _run_sweep)
        omit it and use the jittered cadence."""
        if self._stopped:
            return
        delay = 0.0 if immediate else self._rand(*_SWEEP_JITTER) * self._sweep_s
        self._sweep_handle = self._timer(delay, self._sweep_fired)

    def _sweep_fired(self) -> None:
        """Timer callback (sync, on the loop) → async sweep task. Rides
        the same _tasks set the broadcast tasks use so stop() drains it
        (no orphaned task, AE5)."""
        self._sweep_handle = None
        if self._stopped:
            return
        task = asyncio.ensure_future(self._run_sweep())
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)

    async def _run_sweep(self) -> None:
        try:
            await self._sweep_once()
        except asyncio.CancelledError:
            raise  # stop() is draining us — do NOT re-arm
        except Exception:
            # A failed pass leaves the registry untouched: existing entries
            # keep their state (no phantom grace timers from an errored
            # discover) and the next cycle retries. Per-backend discover
            # errors are already caught in _sweep_backend; this guards the
            # merge/scheduling tail.
            _log.warning("device watcher: sweep pass failed", exc_info=True)
        self._schedule_sweep()

    async def _sweep_once(self) -> None:
        """One discover pass merged into the registry.

        DLNA always sweeps (no avahi needed). When the watcher is in D-Bus
        sweep mode (_mdns_sweep_active: the in-process 5353 bind failed, so
        there is NO live Cast/AirPlay subscription — see start()), the same
        pass also one-shot discovers airplay and chromecast over avahi/D-Bus
        and merges them through the identical arrival/grace primitives. In
        the in-process path the live AsyncServiceBrowser/CastBrowser owns
        Cast/AirPlay, so the mDNS sweep is skipped (the marker is False).

        Deliberately NOT built on reconcile() — the Scan-reconcile contract
        is wrong for the sweep on both miss branches: reconcile DROPS
        offline entries absent from the scan (eviction belongs to the forced
        Scan alone, origin R6) and KEEPS online entries absent from it
        (right for a one-shot racing a live mDNS view, wrong for the sweep,
        which IS the live view here). The sweep instead reuses the two
        primitive transitions the mDNS paths use: _apply_arrival for hits
        (probe-on-arrival included), _start_grace for misses — sweep miss →
        grace → offline-retained, exactly like an mDNS ItemRemove. Each
        backend touches only its own ("<backend>", *) keys by construction.

        Offline-detection latency: a device gone right after a sweep is
        first MISSED by the next sweep up to 1.2×SWEEP_S = 90s later, then
        sits out GRACE_S = 15s of grace — worst case ~105s, typically
        ~SWEEP_S + GRACE_S ≈ 90s. For DLNA the opportunistic byebye listener
        shortens an announced departure to ~GRACE_S; the sweep ceiling
        covers silent deaths (power cut) and hosts where the 1900 bind
        failed. Cast/AirPlay in sweep mode have no byebye listener, so they
        sit at the sweep ceiling.
        """
        await self._sweep_backend("dlna")
        if self._mdns_sweep_active:
            # Sequential keeps the LAN multicast / D-Bus burst gentle, same
            # as admin's _forced_one_shots._mdns ordering.
            await self._sweep_backend("airplay")
            await self._sweep_backend("chromecast")

    async def _sweep_backend(self, backend_name: str) -> None:
        """Discover one backend and merge it. Per-backend try/except so a
        failure in one (e.g. a D-Bus hiccup on airplay) cannot starve the
        others this cycle — the next sweep retries. A raised discover leaves
        that backend's registry entries untouched (no phantom grace timers)."""
        backend = self._backend_for(backend_name)
        if backend is None:
            return  # state not set up (or a test without that fake)
        try:
            if backend_name == "chromecast":
                # Explicit D-Bus one-shot: discover_devices() would otherwise
                # branch on _mdns_port_unavailable and could start the
                # in-process CastBrowser (5s wait). We only reach here in
                # sweep mode, where D-Bus is the only path.
                found = await backend._dbus_discover()
            else:
                found = await backend.discover_devices()
        except Exception:
            _log.warning("device watcher: %s sweep discover failed",
                         backend_name, exc_info=True)
            return
        self._sweep_merge(backend_name, found)

    def _sweep_merge(self, backend_name: str, found: list[OutputDevice]) -> None:
        """Merge one backend's discover results into the registry via the
        shared arrival/grace primitives (see _sweep_once for why not
        reconcile). Touches only this backend's keys."""
        if self._stopped:
            return  # discover outlived stop(); leave the registry alone
        found_ids: set[str] = set()
        for device in found:
            found_ids.add(device.id)
            if backend_name == "dlna":
                probe_host = self._dlna_host(device.id)
            else:
                probe_host = self._mdns_host(backend_name, device.id)
            self._apply_arrival(
                (backend_name, device.id), device, probe_host=probe_host)
        for key in [k for k in self.registry
                    if k[0] == backend_name and k[1] not in found_ids]:
            self._start_grace(key)

    def _mdns_host(self, backend_name: str, device_id: str) -> str | None:
        """Probe host for a swept Cast/AirPlay entry, read from the backend's
        own address cache — the SAME cache set_device/_probe/admin's
        _host_for resolve through, so probe verdicts land under the key the
        aggregator reads. None when the cache doesn't know the device."""
        backend = self._backend_for(backend_name)
        if backend is None:
            return None
        if backend_name == "airplay":
            addr = getattr(backend, "_device_addr", {}).get(device_id)
            return addr[1] if addr else None  # (name, host, port, txt)
        if backend_name == "chromecast":
            addr = getattr(backend, "_dbus_index", {}).get(device_id)
            return addr[1] if addr else None  # (name, host, port)
        return None

    def _dlna_host(self, device_id: str, location: str = "") -> str | None:
        """Probe host for a DLNA entry: the LOCATION URL's hostname —
        same derivation admin.py's _host_for uses, so probe verdicts
        land under the key the aggregator reads. Falls back from an
        explicit *location* (SSDP alive header) to the backend's
        _device_locations cache; None when neither knows the device."""
        if not location:
            backend = self._backend_for("dlna")
            locations = getattr(backend, "_device_locations", None) or {}
            location = locations.get(device_id, "")
        if not location:
            return None
        try:
            return urlparse(location).hostname
        except ValueError:
            return None

    # ── opportunistic SSDP alive/byebye listener (U3, KTD4) ───────────────────

    async def _start_ssdp_listener(self) -> None:
        """ATTEMPT to join the SSDP multicast group. Never load-bearing:
        any bind/join failure (port 1900 owned by another UPnP stack is
        the common case) logs ONE info line and the watcher stays
        sweep-only — functionality is identical, only the offline-
        detection latency differs (see _sweep_once)."""
        try:
            self._ssdp_transport = await self._ssdp_listen_fn(self._on_ssdp_packet)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _log.info(
                "device watcher: SSDP NOTIFY listener unavailable (%s) — "
                "DLNA discovery stays sweep-only", e)

    def _on_ssdp_packet(self, data: bytes) -> None:
        """NOTIFY packet → registry transition. Runs on the loop via the
        datagram protocol; the blanket except keeps a malformed packet
        from reaching the loop's exception handler (fail-soft) — the
        listener must never raise out of its task."""
        try:
            self._handle_ssdp_packet(data)
        except Exception:
            _log.debug("device watcher: SSDP packet handling failed",
                       exc_info=True)

    def _handle_ssdp_packet(self, data: bytes) -> None:
        if self._stopped:
            return  # late datagram after stop()
        parsed = _parse_ssdp_notify(data)
        if parsed is None:
            return
        nts, nt, usn, location = parsed
        # Renderer filter on NT/USN. The authoritative check stays the
        # description XML (discover / describe_renderer); this only stops
        # us reacting to the firehose of non-renderer NOTIFYs.
        if "MediaRenderer" not in nt and "MediaRenderer" not in usn:
            return
        key = ("dlna", usn)
        if nts == "ssdp:byebye":
            # Announced departure — same grace path as an mDNS
            # ItemRemove. Unknown ids no-op inside _start_grace.
            self._start_grace(key)
            return
        if nts != "ssdp:alive":
            return
        entry = self.registry.get(key)
        if entry is not None:
            # Known device: alive is trustworthy enough to cancel a
            # pending grace timer / flip a retained ghost back online
            # (probe-on-arrival fires only on the offline→online edge,
            # inside _apply_arrival — an alive refresh re-probes nothing).
            self._apply_arrival(key, entry.device,
                                probe_host=self._dlna_host(usn, location))
            return
        # Unknown id: an alive packet is an unauthenticated hint, so the
        # device enters the registry only after the same description-XML
        # verification M-SEARCH responses get (KTD4) — a one-off targeted
        # fetch, not a full sweep.
        if not location.startswith(("http://", "https://")):
            return
        self._spawn_alive_verify(usn, location)

    def _spawn_alive_verify(self, usn: str, location: str) -> None:
        token = usn or location
        if token in self._ssdp_inflight:
            return  # burst dedupe: one fetch per announcement burst
        self._ssdp_inflight.add(token)
        task = asyncio.ensure_future(self._verify_alive(usn, location))
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)
        task.add_done_callback(
            lambda _t, tok=token: self._ssdp_inflight.discard(tok))

    async def _verify_alive(self, usn: str, location: str) -> None:
        """Targeted description-check for an alive from an unknown id,
        through the dlna backend's describe_renderer (which also feeds
        _device_locations, so the entry is immediately addressable)."""
        backend = self._backend_for("dlna")
        describe = getattr(backend, "describe_renderer", None)
        if describe is None:
            return  # backend absent or too old — next sweep will see it
        try:
            device = await describe(location, usn)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.debug("device watcher: alive verification failed for %s",
                       location, exc_info=True)
            return
        if device is None or self._stopped:
            return  # not a renderer / unreachable — ignore the hint
        self._apply_arrival(("dlna", device.id), device,
                            probe_host=self._dlna_host(device.id, location))

    # ── debounced broadcast ───────────────────────────────────────────────────

    def _schedule_broadcast(self) -> None:
        """(Re)arm the trailing-edge debounce — last change wins (KTD5)."""
        if self._stopped:
            return
        if self._debounce_handle is not None:
            self._debounce_handle.cancel()
        self._debounce_handle = self._timer(self._debounce_s, self._debounce_fired)

    def _debounce_fired(self) -> None:
        """Timer callback (sync, on the loop) → async broadcast task."""
        self._debounce_handle = None
        if self._stopped:
            return
        task = asyncio.ensure_future(self._emit_broadcast())
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception():
            _log.warning("device watcher: broadcast task failed: %s",
                         task.exception())

    async def _emit_broadcast(self) -> None:
        """Build the snapshot (sync or async builder) and push to admins."""
        try:
            devices = self._snapshot()
            if inspect.isawaitable(devices):
                devices = await devices
            event = DevicesChangedEvent(
                devices=list(devices), mdns_status=self.mdns_status())
            await self._broadcast(event)
        except asyncio.CancelledError:
            raise  # stop() is draining us — propagate, never swallow
        except Exception:
            # A failed frame is harmless: the payload is the full snapshot,
            # so the next broadcast (or the GET) self-heals.
            _log.warning("device watcher: devices_changed broadcast failed",
                         exc_info=True)

    # ── helpers ───────────────────────────────────────────────────────────────

    def _cancel_grace(self, key: tuple[str, str]) -> None:
        handle = self._grace_timers.pop(key, None)
        if handle is not None:
            handle.cancel()

    def _cancel_purge(self, key: tuple[str, str]) -> None:
        handle = self._purge_timers.pop(key, None)
        if handle is not None:
            handle.cancel()

    def _drop_name_index_for(self, key: tuple[str, str]) -> None:
        for name_key in [nk for nk, v in self._name_index.items() if v == key]:
            del self._name_index[name_key]

    # ── injectable defaults ───────────────────────────────────────────────────

    async def _default_snapshot(self) -> list[dict]:
        """The real devices payload (U5, KTD11): the broadcast body IS the
        GET /admin/output/devices ``devices`` list, built by the shared
        builder in app.output.discovery — one render path for push and
        pull (KTD5). Wired here (lazy default) rather than in main.py's
        start_watcher call so EVERY watcher gets production behavior with
        zero lifespan plumbing; the import is function-level and one-way
        (discovery never imports the watcher), so no cycle. Tests keep
        injecting their own builders through the constructor seam, and
        the injected ``backend_for`` doubles as the builder's backend
        lookup so faked backends resolve hosts without touching
        app.state."""
        from app.output.discovery import build_registry_snapshot
        payload, _aggregated = await build_registry_snapshot(
            self.registry, backend_for=self._backend_for)
        return payload

    async def _default_broadcast(self, event: DevicesChangedEvent) -> None:
        # ADMIN socket only (KTD5): the device picker is admin chrome, the
        # same routing OutputChangedEvent / VolumeChangedEvent use.
        from app.events.bus import manager
        await manager.broadcast_to_admins(event)

    async def _default_subscribe(self, service_type: str, on_event):
        """Route each service type to its in-process live source on the shared
        AsyncZeroconf: AirPlay (``_raop._tcp.local``) via app.output.
        mdns_zeroconf, Chromecast (``_googlecast._tcp.local``) via the
        persistent CastBrowser.

        Returns None when the shared AsyncZeroconf could not bind 5353 (a host
        avahi owns it, e.g. TrueNAS) — there is no in-process subscription
        then, and the watcher falls back to the periodic D-Bus sweep for live
        Cast/AirPlay (start() marks _mdns_sweep_active in that mode). The
        former avahi-over-D-Bus *subscription* was retired in 2026-06-16: its
        GLib cross-thread context handling delivered no live events, so the
        sweep replaced it (see app/output/mdns_dbus.py).
        """
        backend = _SERVICE_BACKENDS.get(service_type)
        if backend == "airplay":
            from app import state
            from app.output import mdns_zeroconf
            return await mdns_zeroconf.subscribe(
                service_type, on_event, state.shared_aiozc)
        if backend == "chromecast":
            from app import state
            cc = state.chromecast_backend
            if state.shared_aiozc is not None and cc is not None:
                return await cc.subscribe_discovery(on_event)
            return None  # no 5353 bind → sweep-mode (no live subscription)
        return None  # unknown service type — no source for it

    async def _default_unsubscribe(self, handle) -> None:
        """Release *handle* through whichever source created it (handle type
        is the discriminant — see _default_subscribe). Only the two in-process
        sources produce handles now; the D-Bus path has no subscription."""
        from app.output import mdns_zeroconf
        if isinstance(handle, mdns_zeroconf._ZeroconfSubscription):
            await mdns_zeroconf.unsubscribe(handle)
            return
        from app.output.chromecast import _CastSubscription
        if isinstance(handle, _CastSubscription):
            from app import state
            cc = state.chromecast_backend
            if cc is not None:
                await cc.unsubscribe_discovery(handle)

    def _default_active_key(self) -> tuple[str, str] | None:
        """The currently-active output as a ``(backend, device_id)`` key, or
        None. Lazy in-memory read of ``app.state.output_router.active`` + the
        backend's selected ``_device_id`` (mirrors ``_default_backend_for``'s
        lazy state read) — used only to grant the active output a longer purge
        window (U4 AE4). Sync + best-effort by design: the purge arming path
        is synchronous, so this cannot await a DB read."""
        from app import state
        backend = getattr(state.output_router, "active", None)
        if backend is None:
            return None
        name = None
        for candidate in ("chromecast", "airplay", "dlna", "direct"):
            if backend is getattr(state, f"{candidate}_backend", None):
                name = candidate
                break
        device_id = getattr(backend, "_device_id", None)
        if name is None or not device_id:
            return None
        return (name, device_id)

    def _default_backend_for(self, backend: str):
        from app import state
        return {
            # "direct" exists for the snapshot builder only (its pseudo-
            # device is appended at aggregation); Direct never enters the
            # registry and has no register_resolved hook.
            "direct": state.direct_backend,
            "chromecast": state.chromecast_backend,
            "airplay": state.airplay_backend,
            "dlna": state.dlna_backend,
        }.get(backend)

    async def _default_ssdp_listen(self, on_packet):
        """Join 239.255.255.250:1900 and deliver raw NOTIFY datagrams to
        *on_packet*. Raises on bind/join failure — _start_ssdp_listener
        catches and degrades to sweep-only (KTD4). SO_REUSEADDR (+ the
        SO_REUSEPORT attempt) lets us coexist with other SSDP stacks
        where the OS allows it; where it doesn't, the bind failure is
        exactly the fail-soft path."""
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except (AttributeError, OSError):
                pass  # platform without SO_REUSEPORT — fine
            sock.bind(("", _SSDP_PORT))
            mreq = socket.inet_aton(_SSDP_ADDR) + socket.inet_aton("0.0.0.0")
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except Exception:
            sock.close()  # don't leak the fd on the failure path
            raise
        loop = asyncio.get_running_loop()
        transport, _protocol = await loop.create_datagram_endpoint(
            lambda: _SsdpNotifyProtocol(on_packet), sock=sock)
        return transport

    @staticmethod
    def _default_probe(host: str, backend: str, device_id: str) -> None:
        # Shared with the admin route (U4/KTD6): one semaphore bounds
        # watcher- and route-triggered probes together.
        from app.output import probe_runner
        probe_runner.probe_host(host, backend, device_id)

    @staticmethod
    async def _default_dbus_available() -> bool:
        # Cheap "is a browsable mDNS daemon reachable over D-Bus" probe,
        # used by start() to decide the sweep-mode marker (see start()).
        from app.output import mdns_dbus
        return await mdns_dbus.dbus_discovery_available()

    @staticmethod
    def _default_timer(delay: float, callback: Callable[[], None]):
        # asyncio.TimerHandle satisfies the .cancel() contract the watcher
        # relies on; tests inject a registry-backed fake instead.
        return asyncio.get_running_loop().call_later(delay, callback)


# ── SSDP listener plumbing (module-level, watcher-agnostic) ───────────────────

class _SsdpNotifyProtocol(asyncio.DatagramProtocol):
    """Datagram protocol for the opportunistic listener: every received
    packet goes to the watcher's handler, which never raises (fail-soft
    — a malformed datagram must not reach the loop's exception handler).
    """

    def __init__(self, on_packet: Callable[[bytes], None]) -> None:
        self._on_packet = on_packet

    def datagram_received(self, data: bytes, addr) -> None:
        self._on_packet(data)

    def error_received(self, exc) -> None:
        # ICMP-level noise (port unreachable from a multicast peer) is
        # routine on a busy LAN — log at debug, keep listening.
        _log.debug("device watcher: SSDP socket error: %s", exc)


def _parse_ssdp_notify(data: bytes) -> tuple[str, str, str, str] | None:
    """Parse a NOTIFY datagram into (nts, nt, usn, location).

    Returns None for anything that is not a NOTIFY (M-SEARCH requests
    and 200-OK responses also land on the multicast group). Header names
    are case-insensitive per HTTP; NTS values are lowercased so callers
    compare against the literal `ssdp:alive` / `ssdp:byebye`.
    """
    try:
        text = data.decode("utf-8", "replace")
    except Exception:
        return None
    lines = text.split("\r\n")
    if not lines or not lines[0].upper().startswith("NOTIFY"):
        return None
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().upper()] = value.strip()
    return (
        headers.get("NTS", "").lower(),
        headers.get("NT", ""),
        headers.get("USN", ""),
        headers.get("LOCATION", ""),
    )


# ── module singleton ──────────────────────────────────────────────────────────
# Matches state.py's idiom: module-level slot + accessor; the lifespan owns
# creation and teardown, everything else only reads via get_watcher().

_watcher: DeviceWatcher | None = None


def get_watcher() -> DeviceWatcher | None:
    """The live watcher, or None before startup / after shutdown."""
    return _watcher


async def start_watcher(**kwargs) -> DeviceWatcher:
    """Create (if needed) and start the singleton. Called from lifespan.

    *kwargs* pass through to DeviceWatcher — production passes none; they
    exist so an integration test can run the real lifecycle with fakes.
    """
    global _watcher
    if _watcher is None:
        _watcher = DeviceWatcher(**kwargs)
    await _watcher.start()
    return _watcher


async def stop_watcher() -> None:
    """Stop and discard the singleton. Safe when never started."""
    global _watcher
    watcher, _watcher = _watcher, None
    if watcher is not None:
        await watcher.stop()
