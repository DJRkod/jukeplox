"""Tests for the live device watcher (2026-06-11 live-discovery plan U2).

Everything external is injected: mdns_dbus subscriptions are fakes whose
captured ``on_event`` callbacks we drive by hand, timers land in a registry
fake (nothing ever sleeps), the clock is stepped manually, and broadcasts
are captured into a list. The backend address-cache hooks (KTD9) run
against REAL ChromecastBackend / AirPlayBackend instances — their
constructors are IO-free — so the registry-id ↔ backend-cache-id equality
(KTD10) is asserted against the production code, not a mirror of it.
"""

import asyncio
import logging
import random

from app.output.airplay import AirPlayBackend
from app.output.base import OutputDevice
from app.output.chromecast import ChromecastBackend
from app.output.watcher import (
    ACTIVE_PURGE_S,
    DEBOUNCE_S,
    GRACE_S,
    PURGE_S,
    SWEEP_S,
    DeviceWatcher,
)

RAOP = "_raop._tcp.local"
CAST = "_googlecast._tcp.local"

# Realistic avahi shapes: RAOP names carry the MAC prefix cliap2 needs;
# Chromecast labels carry the dashed slug + 32-hex cast uuid, with the
# clean friendly name in the TXT fn record.
AIR_NAME = "AABBCCDDEEFF@WiiM Pro"
AIR_HOST, AIR_PORT = "192.168.1.20", 7000
AIR_TXT = {"et": "0,4", "features": "0x4A7FCA00,0xBC354BD0"}
CAST_NAME = "JBL-Charge-5-Wi-Fi-S-308c00d1117fa74c600cb4c97d433fd4"
CAST_HOST, CAST_PORT = "192.168.1.10", 8009
CAST_UUID = "308c00d1117fa74c600cb4c97d433fd4"
CAST_TXT = {"fn": "JBL Charge 5", "id": CAST_UUID}


def air_new(name=AIR_NAME, host=AIR_HOST, port=AIR_PORT, txt=None):
    return (name, host, port, None, dict(txt or AIR_TXT), RAOP)


def cast_new(name=CAST_NAME, host=CAST_HOST, port=CAST_PORT,
             uuid=CAST_UUID, txt=None):
    return (name, host, port, uuid, dict(txt or CAST_TXT), CAST)


# U3 DLNA shapes: registry ids are the SSDP USN (discover_devices' id
# derivation), probe hosts come from the LOCATION URL's hostname.
DLNA_USN = "uuid:wiim-1::urn:schemas-upnp-org:device:MediaRenderer:1"
DLNA_LOC = "http://192.168.1.77:49152/description.xml"
DLNA_HOST = "192.168.1.77"
MEDIA_RENDERER_NT = "urn:schemas-upnp-org:device:MediaRenderer:1"


def dlna_dev(uid=DLNA_USN, name="WiiM Pro DLNA"):
    return OutputDevice(id=uid, name=name, backend_type="dlna")


def ssdp_notify(nts, usn=DLNA_USN, nt=MEDIA_RENDERER_NT, location=None):
    """Raw NOTIFY datagram in the shape renderers actually multicast."""
    lines = [
        "NOTIFY * HTTP/1.1",
        "HOST: 239.255.255.250:1900",
        f"NT: {nt}",
        f"NTS: {nts}",
        f"USN: {usn}",
    ]
    if location:
        lines.append(f"LOCATION: {location}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


# ── fakes ─────────────────────────────────────────────────────────────────────

class FakeTimer:
    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.cancelled = False
        self.fired = False

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if self.cancelled or self.fired:
            return
        self.fired = True
        self.callback()


class FakeTimers:
    """Injected timer factory + registry: the orphan-timer assertions in
    the churn/stop tests read ``active`` — a timer neither fired nor
    cancelled is a leak."""

    def __init__(self):
        self.created: list[FakeTimer] = []

    def schedule(self, delay, callback) -> FakeTimer:
        t = FakeTimer(delay, callback)
        self.created.append(t)
        return t

    @property
    def active(self) -> list[FakeTimer]:
        return [t for t in self.created if not t.cancelled and not t.fired]

    def fire(self, delay):
        # Snapshot first: firing a grace timer schedules the debounce
        # timer, which must not fire in the same pass.
        for t in list(self.active):
            if t.delay == delay:
                t.fire()


class FakeMdns:
    """Stands in for the injected subscribe/unsubscribe seam (the in-process
    mdns_zeroconf / CastBrowser sources); captures on_event."""

    def __init__(self, supported=True):
        self.supported = supported
        self.on_events: dict[str, object] = {}  # service_type → callback
        self.handles: list[object] = []
        self.unsubscribed: list[object] = []

    async def subscribe(self, service_type, on_event):
        if not self.supported:
            return None  # mirrors the real API: None == avahi unavailable
        self.on_events[service_type] = on_event
        handle = object()
        self.handles.append(handle)
        return handle

    async def unsubscribe(self, handle):
        self.unsubscribed.append(handle)


class FakeDlnaBackend:
    """Stands in for DlnaBackend on the watcher's injection seams:
    discover_devices replays scripted results (the U3 sweep), and
    describe_renderer is the SSDP-alive targeted-verification hook.
    _device_locations mirrors the real cache shape — the watcher derives
    probe hosts from it via urlparse."""

    def __init__(self):
        self.results: list[OutputDevice] = []
        self.locations: dict[str, str] = {}   # id → LOCATION, fed on discover
        self.error: Exception | None = None
        self.discover_calls = 0
        self.block: asyncio.Event | None = None  # set → discover hangs
        self.describe_calls: list[tuple[str, str]] = []
        self.describe_result: OutputDevice | None = None
        self._device_locations: dict[str, str] = {}

    async def discover_devices(self):
        self.discover_calls += 1
        if self.block is not None:
            await self.block.wait()
        if self.error is not None:
            raise self.error
        self._device_locations.update(self.locations)
        return list(self.results)

    async def describe_renderer(self, location, usn=""):
        self.describe_calls.append((location, usn))
        if self.describe_result is not None:
            self._device_locations[self.describe_result.id] = location
        return self.describe_result


class FakeSsdpTransport:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeSsdp:
    """Stands in for the watcher's ssdp_listen injection: captures the
    packet callback (notify() feeds raw datagrams through it) or raises
    at bind time when fail=True (the 1900-owned-by-another-stack case)."""

    def __init__(self, fail=False):
        self.fail = fail
        self.on_packet = None
        self.transport = FakeSsdpTransport()

    async def listen(self, on_packet):
        if self.fail:
            raise OSError(98, "address already in use")
        self.on_packet = on_packet
        return self.transport

    def notify(self, data: bytes):
        assert self.on_packet is not None, "listener never bound"
        self.on_packet(data)


class Clock:
    def __init__(self):
        self.mono = 1000.0
        self.wall = 1_770_000_000.0


class Harness:
    """One fully-faked watcher; ``emit`` drives captured U1 callbacks."""

    def __init__(self, supported=True, snapshot=None, probe=None,
                 rand=None, ssdp_fail=False, active_key_for=None,
                 dbus_available=False):
        self.timers = FakeTimers()
        self.mdns = FakeMdns(supported)
        self._dbus_available = dbus_available
        self.clock = Clock()
        self.broadcasts: list = []
        # U4 probe trigger captures — (host, backend, device_id) per call.
        self.probes: list[tuple[str, str, str]] = []
        self.cast_backend = ChromecastBackend()
        self.airplay_backend = AirPlayBackend()
        self.dlna_backend = FakeDlnaBackend()
        self.ssdp = FakeSsdp(fail=ssdp_fail)

        async def _capture(event):
            self.broadcasts.append(event)

        def _capture_probe(host, backend, device_id):
            self.probes.append((host, backend, device_id))

        async def _dbus_avail():
            return self._dbus_available

        self.watcher = DeviceWatcher(
            snapshot=snapshot,
            broadcast=_capture,
            subscribe=self.mdns.subscribe,
            unsubscribe=self.mdns.unsubscribe,
            backend_for={"chromecast": self.cast_backend,
                         "airplay": self.airplay_backend,
                         "dlna": self.dlna_backend}.get,
            probe=probe or _capture_probe,
            timer=self.timers.schedule,
            clock=lambda: self.clock.mono,
            wall_clock=lambda: self.clock.wall,
            # Jitter pinned to 1.0 by default so the sweep timer's delay
            # is exactly SWEEP_S and FakeTimers.fire(SWEEP_S) drives it;
            # jitter tests inject their own rand.
            rand=rand or (lambda lo, hi: 1.0),
            ssdp_listen=self.ssdp.listen,
            # U4 auto-remove: default to "no active device" so purge tests use
            # the idle PURGE_S window; tests asserting AE4 inject their own.
            active_key_for=active_key_for or (lambda: None),
            dbus_available=_dbus_avail,
        )

    async def start(self):
        await self.watcher.start()
        # Drain the immediate startup sweep (delay 0) so tests begin in steady
        # state with only the periodic jittered timer armed (delay SWEEP_S
        # under the pinned rand) — the invariant the sweep/jitter/pending
        # assertions rely on. The startup sweep runs with default-empty
        # discover results (tests script results AFTER start()), so its only
        # footprint is one discover_call, reset here; an empty pass touches
        # neither registry, probes nor broadcasts.
        for t in list(self.timers.active):
            if t.callback == self.watcher._sweep_fired and t.delay == 0.0:
                t.fire()
                break
        for _ in range(4):
            await asyncio.sleep(0)
        self.dlna_backend.discover_calls = 0
        return self

    def emit(self, service_type, kind, payload):
        self.mdns.on_events[service_type](kind, payload)

    @property
    def pending(self):
        """Active timers EXCLUDING the always-armed U3 sweep — the mDNS
        assertions predate the sweep and care only about grace/debounce
        state (the sweep timer is live from start() to stop() by design)."""
        return [t for t in self.timers.active if t.delay != SWEEP_S]

    async def fire_debounce(self):
        """Fire the pending debounce timer and drain the broadcast task."""
        self.timers.fire(DEBOUNCE_S)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    async def run_sweep(self):
        """Fire the pending sweep timer and drain the discover task."""
        self.timers.fire(SWEEP_S)
        for _ in range(4):
            await asyncio.sleep(0)


# ── arrival ───────────────────────────────────────────────────────────────────

async def test_arrival_marks_online_and_emits_one_debounced_broadcast():
    h = await Harness().start()
    assert h.watcher.running
    h.emit(RAOP, "new", air_new())
    h.emit(CAST, "new", cast_new())

    air_key = ("airplay", f"{AIR_HOST}:{AIR_PORT}")
    cast_key = ("chromecast", CAST_UUID)
    assert h.watcher.registry[air_key].online is True
    assert h.watcher.registry[cast_key].online is True
    assert h.watcher.registry[air_key].offline_since is None

    # Two arrivals → two debounce schedules, but trailing-edge means the
    # first was cancelled: exactly ONE timer is live (ignoring the sweep),
    # and firing it emits exactly ONE devices_changed frame (KTD5).
    assert len(h.pending) == 1
    await h.fire_debounce()
    assert len(h.broadcasts) == 1
    event = h.broadcasts[0]
    assert event.type == "devices_changed"
    assert event.mdns_status["airplay"] == "ok"
    # The default snapshot IS the GET /admin/output/devices payload now
    # (U5 wiring, KTD11/KTD5): per-host records with online flags, hosts
    # resolved through the backend caches register_resolved fed (KTD9).
    by_host = {d["host"]: d for d in event.devices}
    assert by_host[AIR_HOST]["online"] is True
    assert by_host[AIR_HOST]["protocols"][0]["device_id"] == f"{AIR_HOST}:{AIR_PORT}"
    assert by_host[CAST_HOST]["protocols"][0]["backend"] == "chromecast"
    assert by_host[CAST_HOST]["protocols"][0]["device_id"] == CAST_UUID


async def test_arrival_feeds_backend_address_caches_ktd9():
    """KTD9: arrivals must land in the SAME structures _dbus_discover
    writes, or the device is registry-visible yet unplayable."""
    h = await Harness().start()
    h.emit(RAOP, "new", air_new())
    h.emit(CAST, "new", cast_new())

    # AirPlay: raw avahi name retained (cliap2 needs the MAC prefix).
    assert h.airplay_backend._device_addr[f"{AIR_HOST}:{AIR_PORT}"] == (
        AIR_NAME, AIR_HOST, AIR_PORT, AIR_TXT)
    # Chromecast: both the host:port key and the uuid key, display name
    # resolved from the TXT fn record — exactly what _dbus_discover writes.
    assert h.cast_backend._dbus_index[CAST_UUID] == (
        "JBL Charge 5", CAST_HOST, CAST_PORT)
    assert h.cast_backend._dbus_index[f"{CAST_HOST}:{CAST_PORT}"] == (
        "JBL Charge 5", CAST_HOST, CAST_PORT)


async def test_injected_snapshot_builds_the_payload():
    """The watcher broadcasts whatever the injected builder returns —
    U5 swaps in the real aggregator through this seam. Async builders
    are supported because the real one reads the probe cache."""
    async def snapshot():
        return [{"sentinel": True}]

    h = await Harness(snapshot=snapshot).start()
    h.emit(RAOP, "new", air_new())
    await h.fire_debounce()
    assert h.broadcasts[0].devices == [{"sentinel": True}]


# ── remove / grace / flap ─────────────────────────────────────────────────────

async def test_remove_then_grace_expiry_flips_offline_and_retains_entry():
    h = await Harness().start()
    h.emit(RAOP, "new", air_new())
    await h.fire_debounce()
    h.broadcasts.clear()

    h.emit(RAOP, "remove", (AIR_NAME, RAOP))
    key = ("airplay", f"{AIR_HOST}:{AIR_PORT}")
    # Remove alone changes nothing observable: still online, only the
    # grace timer is pending — no debounce scheduled yet.
    assert h.watcher.registry[key].online is True
    assert [t.delay for t in h.pending] == [GRACE_S]

    h.clock.wall += GRACE_S
    h.timers.fire(GRACE_S)
    entry = h.watcher.registry[key]
    assert entry.online is False
    assert entry.offline_since == h.clock.wall
    assert key in h.watcher.registry  # retained until Scan reconcile (R2)

    await h.fire_debounce()
    assert len(h.broadcasts) == 1
    by_host = {d["host"]: d for d in h.broadcasts[0].devices}
    assert by_host[AIR_HOST]["online"] is False
    assert by_host[AIR_HOST]["offline_since"] == h.clock.wall


async def test_flap_within_grace_emits_nothing_and_stays_online():
    h = await Harness().start()
    h.emit(RAOP, "new", air_new())
    await h.fire_debounce()
    h.broadcasts.clear()

    h.emit(RAOP, "remove", (AIR_NAME, RAOP))
    h.emit(RAOP, "new", air_new())  # reappears inside the grace window

    key = ("airplay", f"{AIR_HOST}:{AIR_PORT}")
    assert h.watcher.registry[key].online is True
    assert h.watcher.registry[key].offline_since is None
    # Grace timer cancelled, no debounce armed, no frame emitted.
    assert h.pending == []
    await h.fire_debounce()  # no-op: nothing to fire
    assert h.broadcasts == []


async def test_remove_for_unknown_name_is_ignored():
    h = await Harness().start()
    h.emit(CAST, "remove", ("never-seen-device", CAST))
    assert h.pending == []
    assert h.watcher.registry == {}


# ── source swap (U2): AirPlay rides in-process zeroconf, not avahi/D-Bus ───────

async def test_default_subscribe_routes_airplay_to_zeroconf(monkeypatch):
    """U2: the watcher's default subscribe browses _raop via the in-process
    zeroconf source with the shared AsyncZeroconf (no avahi/D-Bus)."""
    import app.state as st
    from app.output import mdns_zeroconf

    watcher = DeviceWatcher()
    sentinel_aiozc = object()
    monkeypatch.setattr(st, "shared_aiozc", sentinel_aiozc)

    zc_calls = []

    async def fake_zc_sub(service_type, on_event, aiozc):
        zc_calls.append((service_type, aiozc))
        return "zc-handle"

    monkeypatch.setattr(mdns_zeroconf, "subscribe", fake_zc_sub)

    handle = await watcher._default_subscribe(RAOP, lambda *a: None)
    assert handle == "zc-handle"
    assert zc_calls == [(RAOP, sentinel_aiozc)]


async def test_default_unsubscribe_routes_zeroconf_handle(monkeypatch):
    """U2: a zeroconf subscription handle is released through mdns_zeroconf."""
    from app.output import mdns_zeroconf

    watcher = DeviceWatcher()
    sub = mdns_zeroconf._ZeroconfSubscription(
        RAOP, lambda *a: None, None, asyncio.get_running_loop())

    zc_unsub = []

    async def fake_zc_unsub(h):
        zc_unsub.append(h)

    monkeypatch.setattr(mdns_zeroconf, "unsubscribe", fake_zc_unsub)

    await watcher._default_unsubscribe(sub)
    assert zc_unsub == [sub]


async def test_default_subscribe_routes_chromecast_to_castbrowser(monkeypatch):
    """With the shared AsyncZeroconf bound (5353 available), the watcher's
    default subscribe drives the persistent CastBrowser (cc.subscribe_discovery)
    for _googlecast — no avahi/D-Bus."""
    import app.state as st

    watcher = DeviceWatcher()
    monkeypatch.setattr(st, "shared_aiozc", object())  # in-process 5353 bound

    class FakeCast:
        def __init__(self):
            self.calls = []

        async def subscribe_discovery(self, on_event):
            self.calls.append(on_event)
            return "cast-handle"

    fake_cc = FakeCast()
    monkeypatch.setattr(st, "chromecast_backend", fake_cc)

    cb = lambda *a: None
    handle = await watcher._default_subscribe(CAST, cb)
    assert handle == "cast-handle"
    assert fake_cc.calls == [cb]


async def test_default_unsubscribe_routes_cast_handle(monkeypatch):
    """U3: a _CastSubscription handle is released through the chromecast
    backend."""
    import app.state as st
    from app.output.chromecast import _CastSubscription

    watcher = DeviceWatcher()
    sub = _CastSubscription(lambda *a: None, asyncio.get_running_loop())

    released = []

    class FakeCast:
        async def unsubscribe_discovery(self, h):
            released.append(h)

    monkeypatch.setattr(st, "chromecast_backend", FakeCast())

    await watcher._default_unsubscribe(sub)
    assert released == [sub]


async def test_unknown_service_type_subscribe_returns_none():
    """An unknown service type maps to no backend, so neither the in-process
    nor the D-Bus fallback applies — returns None (degraded), no crash."""
    watcher = DeviceWatcher()
    assert await watcher._default_subscribe("_unknown._tcp.local", lambda *a: None) is None


async def test_default_subscribe_airplay_returns_none_without_5353(monkeypatch):
    """2026-06-16 (U3): the avahi-over-D-Bus *subscription* was retired (its
    GLib cross-thread context handling delivered no live events). With
    shared_aiozc None there is no in-process subscription, so _default_subscribe
    returns None — the periodic D-Bus sweep is the live Cast/AirPlay view."""
    import app.state as st
    from app.output import mdns_zeroconf

    watcher = DeviceWatcher()
    monkeypatch.setattr(st, "shared_aiozc", None)  # 5353 bind failed

    async def zc_none(service_type, on_event, aiozc):
        return None  # in-process unavailable

    monkeypatch.setattr(mdns_zeroconf, "subscribe", zc_none)

    assert await watcher._default_subscribe(RAOP, lambda *a: None) is None


async def test_default_subscribe_chromecast_returns_none_without_5353(monkeypatch):
    """U3: with shared_aiozc None the CastBrowser can't bind 5353; there is no
    D-Bus subscription fallback anymore, so subscribe returns None (sweep mode)
    and the CastBrowser is never started."""
    import app.state as st

    watcher = DeviceWatcher()
    monkeypatch.setattr(st, "shared_aiozc", None)

    class FakeCast:
        def __init__(self):
            self.subscribe_calls = 0

        async def subscribe_discovery(self, on_event):
            self.subscribe_calls += 1
            return "cast-handle"

    fake_cc = FakeCast()
    monkeypatch.setattr(st, "chromecast_backend", fake_cc)

    assert await watcher._default_subscribe(CAST, lambda *a: None) is None
    assert fake_cc.subscribe_calls == 0  # CastBrowser not started without 5353


async def test_default_unsubscribe_ignores_unknown_handle():
    """U3: only the two in-process sources produce handles now (the D-Bus path
    has no subscription). An unrecognized handle is a no-op — never raises."""
    watcher = DeviceWatcher()
    await watcher._default_unsubscribe(object())  # no raise, no routing


async def test_zeroconf_raop_arrival_yields_selectable_airplay_device():
    """U2 (AE1): a _raop arrival delivered on the watcher's contract reaches
    the registry and feeds the AirPlay address cache, so the device is
    selectable for playback."""
    h = await Harness().start()
    h.emit(RAOP, "new", air_new())
    key = ("airplay", f"{AIR_HOST}:{AIR_PORT}")
    assert h.watcher.registry[key].online is True
    # register_resolved consumed the zeroconf payload: address cache populated
    # with the RAW avahi name (cliap2 needs the MAC prefix).
    assert h.airplay_backend._device_addr[f"{AIR_HOST}:{AIR_PORT}"] == (
        AIR_NAME, AIR_HOST, AIR_PORT, AIR_TXT)
    # Departure greys it via the grace path, exactly as before.
    h.emit(RAOP, "remove", (AIR_NAME, RAOP))
    h.timers.fire(GRACE_S)
    assert h.watcher.registry[key].online is False


# ── auto-remove purge (U4) ────────────────────────────────────────────────────

async def test_offline_then_purge_evicts_with_broadcast_on_both_transitions():
    """AE2: offline → greyed (broadcast) → auto-removed after the purge TTL
    (broadcast). No Scan needed to clear the dead entry."""
    h = await Harness().start()
    h.emit(RAOP, "new", air_new())
    await h.fire_debounce()
    h.broadcasts.clear()
    key = ("airplay", f"{AIR_HOST}:{AIR_PORT}")

    # Departure → grace → offline-retained (first transition).
    h.emit(RAOP, "remove", (AIR_NAME, RAOP))
    h.clock.wall += GRACE_S
    h.timers.fire(GRACE_S)
    assert h.watcher.registry[key].online is False
    await h.fire_debounce()
    assert len(h.broadcasts) == 1
    assert h.broadcasts[0].devices[0]["online"] is False
    h.broadcasts.clear()

    # Purge timer is armed at the idle TTL (no active device in this Harness).
    assert [t.delay for t in h.pending] == [PURGE_S]

    # Purge fires → entry evicted (second transition), name index dropped.
    h.timers.fire(PURGE_S)
    assert key not in h.watcher.registry
    assert h.watcher._name_index == {}
    await h.fire_debounce()
    assert len(h.broadcasts) == 1
    assert h.broadcasts[0].devices == []


async def test_reappearance_within_purge_window_cancels_eviction():
    """AE3-adjacent: a device that returns while greyed cancels the pending
    purge — it stays in the menu, no churn."""
    h = await Harness().start()
    h.emit(RAOP, "new", air_new())
    await h.fire_debounce()
    key = ("airplay", f"{AIR_HOST}:{AIR_PORT}")

    h.emit(RAOP, "remove", (AIR_NAME, RAOP))
    h.timers.fire(GRACE_S)  # → offline, purge armed
    await h.fire_debounce()  # drain the offline broadcast's debounce timer
    assert [t.delay for t in h.pending] == [PURGE_S]

    # Comes back before the purge fires.
    h.emit(RAOP, "new", air_new())
    assert h.watcher.registry[key].online is True
    assert h.watcher._purge_timers == {}        # purge cancelled
    await h.fire_debounce()                       # drain the return broadcast
    assert h.pending == []                        # nothing left pending
    # Firing the (now-cancelled) purge timer does nothing.
    h.timers.fire(PURGE_S)
    assert key in h.watcher.registry


async def test_flap_within_grace_arms_no_purge():
    """AE3: a flap inside the grace window never flips offline, so no purge
    timer is ever armed."""
    h = await Harness().start()
    h.emit(RAOP, "new", air_new())
    await h.fire_debounce()

    h.emit(RAOP, "remove", (AIR_NAME, RAOP))
    h.emit(RAOP, "new", air_new())  # reappears inside grace
    assert h.watcher._grace_timers == {}
    assert h.watcher._purge_timers == {}
    assert h.pending == []


async def test_active_output_gets_longer_purge_window():
    """AE4: the active output is armed with ACTIVE_PURGE_S, not the idle
    PURGE_S, so a brief drop of the device you're playing to is retained
    longer and not evicted mid-use before its window."""
    active = ("airplay", f"{AIR_HOST}:{AIR_PORT}")
    h = await Harness(active_key_for=lambda: active).start()
    h.emit(RAOP, "new", air_new())          # the active device
    h.emit(CAST, "new", cast_new())         # an idle device
    await h.fire_debounce()

    # Both go offline.
    h.emit(RAOP, "remove", (AIR_NAME, RAOP))
    h.emit(CAST, "remove", (CAST_NAME, CAST))
    h.timers.fire(GRACE_S)
    await h.fire_debounce()  # drain the offline broadcast's debounce timer

    delays = sorted(t.delay for t in h.pending)
    assert delays == [PURGE_S, ACTIVE_PURGE_S]  # idle cast + active airplay
    # The idle PURGE_S fires first; the active entry must survive it.
    h.timers.fire(PURGE_S)
    assert active in h.watcher.registry
    assert ("chromecast", CAST_UUID) not in h.watcher.registry
    # The active entry only goes once its longer window elapses.
    h.timers.fire(ACTIVE_PURGE_S)
    assert active not in h.watcher.registry


async def test_purge_skipped_if_back_online_before_fire():
    """A late purge fire on an entry that returned online is a no-op (guard
    against a timer that escaped cancellation)."""
    h = await Harness().start()
    h.emit(RAOP, "new", air_new())
    await h.fire_debounce()
    key = ("airplay", f"{AIR_HOST}:{AIR_PORT}")

    h.emit(RAOP, "remove", (AIR_NAME, RAOP))
    h.timers.fire(GRACE_S)
    purge = [t for t in h.pending if t.delay == PURGE_S][0]
    # Force online without going through _apply_arrival's cancel (simulate a
    # timer that slipped past cancellation), then fire it directly.
    h.watcher.registry[key].online = True
    purge.callback()
    assert key in h.watcher.registry  # online entry never evicted by purge


async def test_stop_cancels_pending_purge_timers():
    """AE5 no-leak: stop() cancels armed purge timers too — nothing survives."""
    h = await Harness().start()
    h.emit(RAOP, "new", air_new())
    await h.fire_debounce()
    h.emit(RAOP, "remove", (AIR_NAME, RAOP))
    h.timers.fire(GRACE_S)
    assert h.watcher._purge_timers != {}

    await h.watcher.stop()
    assert h.watcher._purge_timers == {}
    assert h.timers.active == []


# ── probe-on-arrival (U4) ─────────────────────────────────────────────────────

async def test_arrival_triggers_one_probe_per_backend_entry():
    """A NEW device probes exactly once per arriving backend entry, with
    the resolved host (R4 — Via options verify hands-off)."""
    h = await Harness().start()
    h.emit(RAOP, "new", air_new())
    h.emit(CAST, "new", cast_new())
    assert h.probes == [
        (AIR_HOST, "airplay", f"{AIR_HOST}:{AIR_PORT}"),
        (CAST_HOST, "chromecast", CAST_UUID),
    ]
    # Re-announcement while online is neither new nor returning — the
    # record refresh must not re-probe.
    h.emit(RAOP, "new", air_new())
    assert len(h.probes) == 2


async def test_flap_within_grace_does_not_reprobe():
    """The entry never went offline, so the reappearance is not a return
    — no probe, exactly as no broadcast (KTD3 grace semantics)."""
    h = await Harness().start()
    h.emit(RAOP, "new", air_new())
    h.probes.clear()

    h.emit(RAOP, "remove", (AIR_NAME, RAOP))
    h.emit(RAOP, "new", air_new())  # reappears inside the grace window
    assert h.probes == []


async def test_offline_to_online_return_probes_again():
    h = await Harness().start()
    h.emit(RAOP, "new", air_new())
    h.probes.clear()

    h.emit(RAOP, "remove", (AIR_NAME, RAOP))
    h.timers.fire(GRACE_S)  # grace expires → offline (entry retained)
    key = ("airplay", f"{AIR_HOST}:{AIR_PORT}")
    assert h.watcher.registry[key].online is False

    h.emit(RAOP, "new", air_new())  # returns after the grace expiry
    assert h.probes == [(AIR_HOST, "airplay", f"{AIR_HOST}:{AIR_PORT}")]
    assert h.watcher.registry[key].online is True


async def test_probe_trigger_failure_leaves_entry_present():
    """Probe verdicts are Via-level: even a probe trigger that RAISES
    must not disturb the registry upsert or the broadcast."""
    def broken_probe(host, backend, device_id):
        raise RuntimeError("probe machinery on fire")

    h = await Harness(probe=broken_probe).start()
    h.emit(RAOP, "new", air_new())
    key = ("airplay", f"{AIR_HOST}:{AIR_PORT}")
    assert h.watcher.registry[key].online is True
    await h.fire_debounce()
    assert len(h.broadcasts) == 1  # broadcast survived the broken trigger


# ── mdns status ───────────────────────────────────────────────────────────────

async def test_status_down_up_flips_watcher_mdns_status():
    h = await Harness().start()
    assert h.watcher.mdns_status() == {
        "direct": "ok", "airplay": "ok", "chromecast": "ok", "dlna": "ok",
        "discovery": "ok"}  # U6: live subscriptions established

    h.emit(CAST, "status", "down")
    assert h.watcher.mdns_status()["chromecast"] == "unavailable"
    assert h.watcher.mdns_status()["airplay"] == "ok"
    # The flip rides the same debounced frame so admin pages see the
    # outage live (the event payload carries mdns_status).
    await h.fire_debounce()
    assert h.broadcasts[-1].mdns_status["chromecast"] == "unavailable"

    h.emit(CAST, "status", "up")
    assert h.watcher.mdns_status()["chromecast"] == "ok"


async def test_degraded_mode_when_avahi_unavailable():
    h = await Harness(supported=False).start()
    assert h.watcher.running is False
    # No live view for the mDNS backends — admin's banner shape.
    assert h.watcher.mdns_status()["airplay"] == "unavailable"
    assert h.watcher.mdns_status()["chromecast"] == "unavailable"
    # U6: with no in-process source established, the discovery-health signal
    # flips to unavailable so the admin banner shows host-networking guidance.
    assert h.watcher.mdns_status()["discovery"] == "unavailable"
    await h.watcher.stop()  # degraded stop must be a clean no-op


async def test_discovery_ok_when_a_source_established():
    """U6: at least one live subscription handle → in_process 'ok'."""
    h = await Harness().start()
    assert h.watcher.mdns_status()["discovery"] == "ok"


# ── stop ──────────────────────────────────────────────────────────────────────

async def test_stop_cancels_all_timers_tasks_and_subscriptions():
    h = await Harness().start()
    h.emit(RAOP, "new", air_new())
    h.emit(CAST, "new", cast_new())
    h.emit(RAOP, "remove", (AIR_NAME, RAOP))  # pending grace + debounce

    assert h.timers.active != []
    await h.watcher.stop()

    assert h.timers.active == []  # injected registry: zero orphaned timers
    assert h.watcher._tasks == set()
    assert h.watcher.running is False
    assert sorted(map(id, h.mdns.unsubscribed)) == sorted(map(id, h.mdns.handles))
    # Late events from in-flight resolves after stop() are dropped.
    h.emit(RAOP, "new", air_new(host="192.168.1.99"))
    assert ("airplay", "192.168.1.99:7000") not in h.watcher.registry
    assert h.timers.active == []


# ── churn (AE5) ───────────────────────────────────────────────────────────────

async def test_churn_keeps_registry_bounded_and_leaks_no_timers():
    h = await Harness().start()
    devices = [
        (f"AABBCCDDEE0{i}@Speaker {i}", f"192.168.1.{30 + i}", 7000)
        for i in range(3)
    ]
    for _cycle in range(50):
        for name, host, port in devices:
            h.emit(RAOP, "new", air_new(name=name, host=host, port=port))
        for name, _host, _port in devices:
            h.emit(RAOP, "remove", (name, RAOP))
    # End on arrivals so every grace timer must have been cancelled.
    for name, host, port in devices:
        h.emit(RAOP, "new", air_new(name=name, host=host, port=port))

    assert len(h.watcher.registry) == 3
    assert all(e.online for e in h.watcher.registry.values())
    assert h.watcher._grace_timers == {}
    # Only the trailing debounce may be live; nothing else may linger
    # (the sweep timer is excluded — it is armed from start to stop).
    assert [t.delay for t in h.pending] in ([], [DEBOUNCE_S])
    assert len(h.airplay_backend._device_addr) == 3
    await h.watcher.stop()
    assert h.timers.active == []


# ── reconcile (U5 contract) ───────────────────────────────────────────────────

def _cast_dev(uid, name):
    return OutputDevice(id=uid, name=name, backend_type="chromecast",
                        id_format="uuid")


async def test_reconcile_drops_absent_ghosts_keeps_reappeared_upserts_new():
    h = await Harness().start()
    ghost_uuid = "deadbeefdeadbeefdeadbeefdeadbeef"
    back_uuid = "feedfacefeedfacefeedfacefeedface"
    h.emit(CAST, "new", cast_new())  # stays online
    h.emit(CAST, "new", cast_new(name="Ghost-Cast", host="192.168.1.40",
                                 uuid=ghost_uuid, txt={"fn": "Ghost"}))
    h.emit(CAST, "new", cast_new(name="Back-Cast", host="192.168.1.41",
                                 uuid=back_uuid, txt={"fn": "Back"}))
    h.emit(RAOP, "new", air_new())  # airplay ghost — must survive untouched
    # Flip the two casts and the airplay entry offline via grace expiry.
    for name in ("Ghost-Cast", "Back-Cast"):
        h.emit(CAST, "remove", (name, CAST))
    h.emit(RAOP, "remove", (AIR_NAME, RAOP))
    h.timers.fire(GRACE_S)
    await h.fire_debounce()
    h.broadcasts.clear()

    live = h.watcher.registry[("chromecast", CAST_UUID)].device
    reappeared = h.watcher.registry[("chromecast", back_uuid)].device
    brand_new = _cast_dev("0123456789abcdef0123456789abcdef", "Brand New")
    changed = h.watcher.reconcile(
        {"chromecast": [live, reappeared, brand_new]})

    assert changed is True
    reg = h.watcher.registry
    assert ("chromecast", ghost_uuid) not in reg          # dropped (R6)
    assert reg[("chromecast", back_uuid)].online is True  # reappeared
    assert reg[("chromecast", back_uuid)].offline_since is None
    assert reg[("chromecast", brand_new.id)].online is True  # upserted
    # Foreign-backend ghost untouched: reconcile only acts on the
    # backends present in `found` (a DLNA-only sweep can never evict
    # mDNS entries — origin AE4).
    air_entry = reg[("airplay", f"{AIR_HOST}:{AIR_PORT}")]
    assert air_entry.online is False

    # Idempotence: the same scan again changes nothing.
    assert h.watcher.reconcile(
        {"chromecast": [live, reappeared, brand_new]}) is False


async def test_reconcile_keeps_online_entries_missed_by_the_scan():
    """An online entry absent from a one-shot window is NOT dropped —
    the live subscription outranks a scan that simply missed a late
    announcer; only OFFLINE ghosts are evicted."""
    h = await Harness().start()
    h.emit(CAST, "new", cast_new())
    changed = h.watcher.reconcile({"chromecast": []})
    assert changed is False
    assert ("chromecast", CAST_UUID) in h.watcher.registry


# ── register_resolved hooks (KTD9, real backends) ─────────────────────────────

def test_chromecast_register_resolved_matches_dbus_discover_shape():
    backend = ChromecastBackend()
    # Pre-seed: the hook must never clear existing entries (Scan-retained
    # offline devices keep their addresses).
    backend._dbus_index["10.0.0.5:8009"] = ("Old Device", "10.0.0.5", 8009)

    dev = backend.register_resolved(
        CAST_NAME, CAST_HOST, CAST_PORT, CAST_UUID, CAST_TXT)
    assert dev == OutputDevice(id=CAST_UUID, name="JBL Charge 5",
                               backend_type="chromecast", id_format="uuid")
    assert backend._dbus_index[CAST_UUID] == ("JBL Charge 5", CAST_HOST, CAST_PORT)
    assert backend._dbus_index[f"{CAST_HOST}:{CAST_PORT}"] == (
        "JBL Charge 5", CAST_HOST, CAST_PORT)
    assert backend._dbus_index["10.0.0.5:8009"] == ("Old Device", "10.0.0.5", 8009)


def test_chromecast_register_resolved_host_port_fallback_without_uuid():
    backend = ChromecastBackend()
    dev = backend.register_resolved(
        "Bare-Cast", "192.168.1.50", 8009, None, {})
    assert dev.id == "192.168.1.50:8009"
    assert dev.id_format == "host_port"
    assert backend._dbus_index == {
        "192.168.1.50:8009": ("Bare-Cast", "192.168.1.50", 8009)}


def test_airplay_register_resolved_matches_discover_shape():
    backend = AirPlayBackend()
    backend._device_addr["10.0.0.9:7000"] = ("OLD@Old", "10.0.0.9", 7000, {})

    dev = backend.register_resolved(AIR_NAME, AIR_HOST, AIR_PORT, None, AIR_TXT)
    # Display name stripped of the MAC prefix; id always host:port.
    assert dev == OutputDevice(id=f"{AIR_HOST}:{AIR_PORT}", name="WiiM Pro",
                               backend_type="airplay", id_format="host_port",
                               hint=None)
    # Cache keeps the RAW avahi name (cliap2's _ensure_deviceid needs it)
    # and never clears existing entries.
    assert backend._device_addr[f"{AIR_HOST}:{AIR_PORT}"] == (
        AIR_NAME, AIR_HOST, AIR_PORT, AIR_TXT)
    assert backend._device_addr["10.0.0.9:7000"] == ("OLD@Old", "10.0.0.9", 7000, {})


# ── DLNA sweep (U3, KTD4) ─────────────────────────────────────────────────────

async def test_sweep_arrival_upserts_online_probes_and_reschedules():
    """One sweep cycle: the found renderer lands online in the registry,
    probe-on-arrival fires with the LOCATION host (same derivation as
    admin's _host_for), the change broadcasts, and the next sweep is
    armed."""
    h = await Harness().start()
    h.dlna_backend.results = [dlna_dev()]
    h.dlna_backend.locations = {DLNA_USN: DLNA_LOC}
    await h.run_sweep()

    key = ("dlna", DLNA_USN)
    assert h.watcher.registry[key].online is True
    assert h.probes == [(DLNA_HOST, "dlna", DLNA_USN)]
    await h.fire_debounce()
    by_host = {d["host"]: d for d in h.broadcasts[-1].devices}
    assert by_host[DLNA_HOST]["protocols"][0]["backend"] == "dlna"
    assert by_host[DLNA_HOST]["protocols"][0]["device_id"] == DLNA_USN
    # Next sweep armed (one live sweep timer, no leftover grace).
    assert [t.delay for t in h.timers.active if t.delay == SWEEP_S] == [SWEEP_S]
    assert h.watcher._grace_timers == {}


async def test_sweep_reannounce_neither_reprobes_nor_rebroadcasts():
    """A second sweep seeing the identical device is a record refresh:
    no probe (still online → not an arrival edge) and no frame."""
    h = await Harness().start()
    h.dlna_backend.results = [dlna_dev()]
    h.dlna_backend.locations = {DLNA_USN: DLNA_LOC}
    await h.run_sweep()
    await h.fire_debounce()
    h.broadcasts.clear()
    h.probes.clear()

    await h.run_sweep()
    assert h.probes == []
    await h.fire_debounce()
    assert h.broadcasts == []
    assert h.dlna_backend.discover_calls == 2


async def test_sweep_miss_starts_grace_then_offline_retained():
    """The U3 contract: sweep miss → grace → offline-RETAINED, exactly
    like an mDNS ItemRemove. (Worst-case latency for a silent death is
    therefore ~1.2*SWEEP_S + GRACE_S; the byebye listener only shortens
    it when bound.)"""
    h = await Harness().start()
    h.dlna_backend.results = [dlna_dev()]
    h.dlna_backend.locations = {DLNA_USN: DLNA_LOC}
    await h.run_sweep()
    await h.fire_debounce()
    h.broadcasts.clear()

    h.dlna_backend.results = []  # renderer unplugged
    await h.run_sweep()
    key = ("dlna", DLNA_USN)
    # Miss alone changes nothing observable — only the grace timer runs.
    assert h.watcher.registry[key].online is True
    assert [t.delay for t in h.pending] == [GRACE_S]

    h.clock.wall += GRACE_S
    h.timers.fire(GRACE_S)
    entry = h.watcher.registry[key]
    assert entry.online is False
    assert entry.offline_since == h.clock.wall
    assert key in h.watcher.registry  # retained until Scan reconcile (R2)

    # A further miss must NOT drop the offline ghost (that would be the
    # reconcile() Scan contract — eviction belongs to the forced Scan
    # alone) and must not arm a second grace timer.
    await h.run_sweep()
    assert key in h.watcher.registry
    assert h.watcher._grace_timers == {}


async def test_sweep_return_after_offline_probes_again():
    h = await Harness().start()
    h.dlna_backend.results = [dlna_dev()]
    h.dlna_backend.locations = {DLNA_USN: DLNA_LOC}
    await h.run_sweep()
    h.probes.clear()

    h.dlna_backend.results = []
    await h.run_sweep()
    h.timers.fire(GRACE_S)
    assert h.watcher.registry[("dlna", DLNA_USN)].online is False

    h.dlna_backend.results = [dlna_dev()]
    await h.run_sweep()
    assert h.watcher.registry[("dlna", DLNA_USN)].online is True
    assert h.probes == [(DLNA_HOST, "dlna", DLNA_USN)]


async def test_sweep_never_touches_non_dlna_entries():
    """An empty DLNA sweep must not grace, drop or otherwise disturb
    mDNS-backend entries — online or retained-offline (origin AE4)."""
    h = await Harness().start()
    h.emit(RAOP, "new", air_new())          # online airplay
    h.emit(CAST, "new", cast_new())         # cast → flipped offline below
    h.emit(CAST, "remove", (CAST_NAME, CAST))
    h.timers.fire(GRACE_S)
    await h.fire_debounce()
    h.broadcasts.clear()

    await h.run_sweep()  # discover returns nothing

    assert h.watcher.registry[("airplay", f"{AIR_HOST}:{AIR_PORT}")].online is True
    assert h.watcher.registry[("chromecast", CAST_UUID)].online is False
    assert ("chromecast", CAST_UUID) in h.watcher.registry
    # The empty sweep armed no grace for any backend; the only pending timer
    # is the cast's auto-remove purge (U4), armed when it went offline above.
    assert h.watcher._grace_timers == {}
    assert [t.delay for t in h.pending] == [PURGE_S]
    await h.fire_debounce()
    assert h.broadcasts == []       # and nothing was broadcast


async def test_sweep_jitter_bounds_and_args():
    """The scheduled delay is rand(0.8, 1.2) * SWEEP_S — assert both the
    bounds passed to the injected randomness and the resulting delay."""
    calls = []

    def rec(lo, hi):
        calls.append((lo, hi))
        return hi  # worst-case jitter

    h = await Harness(rand=rec).start()
    assert calls == [(0.8, 1.2)]
    [sweep] = h.timers.active  # only the sweep is armed right after start
    assert sweep.delay == 1.2 * SWEEP_S


async def test_sweep_jitter_with_default_randomness_stays_in_bounds():
    """Production default (random.uniform) — every cycle lands within
    [0.8, 1.2] * SWEEP_S."""
    h = await Harness(rand=random.uniform).start()
    [sweep] = h.timers.active
    assert 0.8 * SWEEP_S <= sweep.delay <= 1.2 * SWEEP_S


async def test_sweep_error_logs_reschedules_and_leaves_registry_alone(caplog):
    h = await Harness().start()
    h.dlna_backend.results = [dlna_dev()]
    h.dlna_backend.locations = {DLNA_USN: DLNA_LOC}
    await h.run_sweep()

    h.dlna_backend.error = RuntimeError("SSDP socket exploded")
    with caplog.at_level(logging.WARNING, logger="app.output.watcher"):
        await h.run_sweep()
    # Per-backend isolation (U2): the discover error is caught in
    # _sweep_backend and named by backend, so one backend's failure cannot
    # starve the others this cycle.
    assert any("dlna sweep discover failed" in r.message for r in caplog.records)
    # Registry untouched: still online, no phantom grace timer from the
    # errored pass (an error is not a miss).
    assert h.watcher.registry[("dlna", DLNA_USN)].online is True
    assert h.watcher._grace_timers == {}
    # Next sweep still scheduled and functional once the error clears.
    h.dlna_backend.error = None
    await h.run_sweep()
    assert h.dlna_backend.discover_calls == 3


async def test_stop_cancels_sweep_timer_and_inflight_sweep_task():
    h = await Harness().start()
    h.dlna_backend.block = asyncio.Event()  # discover hangs forever
    h.timers.fire(SWEEP_S)
    await asyncio.sleep(0)  # sweep task is now parked on the event
    assert h.watcher._tasks != set()

    await h.watcher.stop()
    assert h.watcher._tasks == set()   # drained, not orphaned (AE5)
    assert h.timers.active == []       # sweep handle cancelled too
    # And the cancelled task must not have re-armed a next sweep.
    await asyncio.sleep(0)
    assert h.timers.active == []


async def test_sweep_runs_in_avahi_degraded_mode():
    """DLNA needs no avahi: the sweep starts even when every mDNS
    subscription failed, while `running` stays avahi-only (U5 gates the
    snapshot route on it — see the property docstring)."""
    h = await Harness(supported=False).start()
    assert h.watcher.running is False
    h.dlna_backend.results = [dlna_dev()]
    h.dlna_backend.locations = {DLNA_USN: DLNA_LOC}
    await h.run_sweep()
    assert h.watcher.registry[("dlna", DLNA_USN)].online is True
    await h.watcher.stop()
    assert h.timers.active == []


# ── D-Bus mDNS sweep (U2) ─────────────────────────────────────────────────────
# When the in-process 5353 bind fails (a host avahi owns it) there is no live
# Cast/AirPlay subscription — the periodic sweep one-shot discovers them over
# avahi/D-Bus and feeds the same arrival/grace primitives. Gated on the
# sweep-mode marker (shared_aiozc None AND a browsable D-Bus daemon reachable).

def _areturn(value):
    """Build an async stand-in returning *value* (avoids a mock import)."""
    async def _f(*_a, **_k):
        return value
    return _f


def _air_dev():
    return OutputDevice(id=f"{AIR_HOST}:{AIR_PORT}", name="WiiM Pro",
                        backend_type="airplay", id_format="host_port")


def _cast_dev_uuid():
    return OutputDevice(id=CAST_UUID, name="JBL Charge 5",
                        backend_type="chromecast", id_format="uuid")


async def test_mdns_sweep_discovers_cast_and_airplay_in_sweep_mode(monkeypatch):
    """The core 2026-06-16 fix: in D-Bus sweep mode the periodic sweep
    one-shot discovers airplay (discover_devices) and chromecast
    (_dbus_discover), upserts them online, and fires probe-on-arrival with
    the host read from each backend's own address cache — devices appear
    hands-off, no manual Scan."""
    import app.state as st
    monkeypatch.setattr(st, "shared_aiozc", None)  # 5353 bind failed
    h = Harness(dbus_available=True)
    # Startup immediate sweep must be inert — empty discovers before scripting.
    h.airplay_backend.discover_devices = _areturn([])
    h.cast_backend._dbus_discover = _areturn([])
    await h.start()
    assert h.watcher._mdns_sweep_active is True

    # Script the avahi/D-Bus one-shots + seed the address caches the watcher
    # derives probe hosts from (what the real discovers populate).
    h.airplay_backend.discover_devices = _areturn([_air_dev()])
    h.cast_backend._dbus_discover = _areturn([_cast_dev_uuid()])
    h.airplay_backend._device_addr[f"{AIR_HOST}:{AIR_PORT}"] = (
        AIR_NAME, AIR_HOST, AIR_PORT, AIR_TXT)
    h.cast_backend._dbus_index[CAST_UUID] = ("JBL Charge 5", CAST_HOST, CAST_PORT)

    await h.run_sweep()

    air_key = ("airplay", f"{AIR_HOST}:{AIR_PORT}")
    cast_key = ("chromecast", CAST_UUID)
    assert h.watcher.registry[air_key].online is True
    assert h.watcher.registry[cast_key].online is True
    # Probe-on-arrival fired for each, host from the backend cache.
    assert (AIR_HOST, "airplay", f"{AIR_HOST}:{AIR_PORT}") in h.probes
    assert (CAST_HOST, "chromecast", CAST_UUID) in h.probes
    await h.fire_debounce()
    by_host = {d["host"]: d for d in h.broadcasts[-1].devices}
    assert by_host[AIR_HOST]["online"] is True
    assert by_host[CAST_HOST]["online"] is True


async def test_mdns_sweep_skipped_in_inprocess_mode(monkeypatch):
    """In-process mode (shared_aiozc bound) → the live AsyncServiceBrowser /
    CastBrowser owns Cast/AirPlay, so the sweep must NOT also discover them
    (no marker, no wasted D-Bus browse). Only DLNA is swept."""
    import app.state as st
    monkeypatch.setattr(st, "shared_aiozc", object())  # 5353 bound
    h = Harness()  # dbus_available irrelevant — marker block skipped
    air_calls = []
    cast_calls = []

    async def _air():
        air_calls.append(1)
        return []

    async def _cast():
        cast_calls.append(1)
        return []

    h.airplay_backend.discover_devices = _air
    h.cast_backend._dbus_discover = _cast
    await h.start()
    assert h.watcher._mdns_sweep_active is False

    h.dlna_backend.results = [dlna_dev()]
    h.dlna_backend.locations = {DLNA_USN: DLNA_LOC}
    await h.run_sweep()

    assert h.watcher.registry[("dlna", DLNA_USN)].online is True
    assert air_calls == []   # never swept — live subscription owns it
    assert cast_calls == []


async def test_mdns_sweep_miss_starts_grace_offline_retained(monkeypatch):
    """A swept Cast/AirPlay entry missed by a later sweep follows the same
    contract as DLNA / an mDNS ItemRemove: grace → offline-RETAINED (only a
    forced Scan reconcile evicts)."""
    import app.state as st
    monkeypatch.setattr(st, "shared_aiozc", None)
    h = Harness(dbus_available=True)
    h.airplay_backend.discover_devices = _areturn([])
    h.cast_backend._dbus_discover = _areturn([])
    await h.start()

    h.cast_backend._dbus_discover = _areturn([_cast_dev_uuid()])
    h.cast_backend._dbus_index[CAST_UUID] = ("JBL Charge 5", CAST_HOST, CAST_PORT)
    await h.run_sweep()
    await h.fire_debounce()
    cast_key = ("chromecast", CAST_UUID)
    assert h.watcher.registry[cast_key].online is True

    # Renderer gone: next sweep misses it → grace timer, still online.
    h.cast_backend._dbus_discover = _areturn([])
    await h.run_sweep()
    assert h.watcher.registry[cast_key].online is True
    assert [t.delay for t in h.pending] == [GRACE_S]

    h.clock.wall += GRACE_S
    h.timers.fire(GRACE_S)
    assert h.watcher.registry[cast_key].online is False
    assert cast_key in h.watcher.registry  # retained


async def test_sweep_mode_marker_makes_running_and_discovery_ok(monkeypatch):
    """With no subscription handles (in-process unavailable) but a reachable
    D-Bus daemon, the sweep IS the live mDNS view: running is True and
    mdns_status reports discovery + per-backend Cast/AirPlay as 'ok' so the
    admin banner stays hidden and the route serves the registry snapshot."""
    import app.state as st
    monkeypatch.setattr(st, "shared_aiozc", None)
    h = await Harness(supported=False, dbus_available=True).start()
    assert h.watcher._mdns_sweep_active is True
    assert h.watcher.running is True
    status = h.watcher.mdns_status()
    assert status["discovery"] == "ok"
    assert status["airplay"] == "ok"
    assert status["chromecast"] == "ok"
    await h.watcher.stop()
    # Marker clears on stop — running goes False.
    assert h.watcher.running is False


async def test_no_mdns_path_keeps_running_false_and_discovery_unavailable(monkeypatch):
    """shared_aiozc None AND no reachable D-Bus daemon: discovery is
    genuinely unavailable. running stays False and the banner signal fires —
    the route falls back to the legacy pull flow."""
    import app.state as st
    monkeypatch.setattr(st, "shared_aiozc", None)
    h = await Harness(supported=False, dbus_available=False).start()
    assert h.watcher._mdns_sweep_active is False
    assert h.watcher.running is False
    assert h.watcher.mdns_status()["discovery"] == "unavailable"


# ── opportunistic SSDP listener (U3, KTD4) ────────────────────────────────────

async def test_ssdp_bind_failure_is_invisible_logged_once(caplog):
    """The 5353 lesson: a 1900 bind failure must cost nothing but one
    info line — start() succeeds, mDNS events flow, the sweep covers
    DLNA."""
    with caplog.at_level(logging.INFO, logger="app.output.watcher"):
        h = await Harness(ssdp_fail=True).start()
    hits = [r for r in caplog.records if "sweep-only" in r.message]
    assert len(hits) == 1
    assert hits[0].levelno == logging.INFO

    assert h.watcher.running is True  # avahi side unaffected
    h.dlna_backend.results = [dlna_dev()]
    h.dlna_backend.locations = {DLNA_USN: DLNA_LOC}
    await h.run_sweep()
    assert h.watcher.registry[("dlna", DLNA_USN)].online is True
    await h.watcher.stop()


async def test_ssdp_byebye_for_known_entry_starts_grace():
    h = await Harness().start()
    h.dlna_backend.results = [dlna_dev()]
    h.dlna_backend.locations = {DLNA_USN: DLNA_LOC}
    await h.run_sweep()
    await h.fire_debounce()

    h.ssdp.notify(ssdp_notify("ssdp:byebye"))
    key = ("dlna", DLNA_USN)
    assert h.watcher.registry[key].online is True  # grace, not instant
    assert [t.delay for t in h.pending] == [GRACE_S]

    h.timers.fire(GRACE_S)
    assert h.watcher.registry[key].online is False
    assert key in h.watcher.registry  # offline-retained, like ItemRemove


async def test_ssdp_alive_within_grace_cancels_timer_flap_semantics():
    """byebye then alive inside the grace window is a flap: timer
    cancelled, entry never went offline, no re-probe, no frame."""
    h = await Harness().start()
    h.dlna_backend.results = [dlna_dev()]
    h.dlna_backend.locations = {DLNA_USN: DLNA_LOC}
    await h.run_sweep()
    await h.fire_debounce()
    h.broadcasts.clear()
    h.probes.clear()

    h.ssdp.notify(ssdp_notify("ssdp:byebye"))
    assert [t.delay for t in h.pending] == [GRACE_S]
    h.ssdp.notify(ssdp_notify("ssdp:alive", location=DLNA_LOC))

    key = ("dlna", DLNA_USN)
    assert h.watcher.registry[key].online is True
    assert h.pending == []          # grace cancelled, no debounce armed
    assert h.probes == []           # not an offline→online edge
    await h.fire_debounce()
    assert h.broadcasts == []
    # The alive for a KNOWN id must not trigger a description fetch.
    assert h.dlna_backend.describe_calls == []


async def test_ssdp_byebye_for_unknown_usn_is_ignored():
    h = await Harness().start()
    h.ssdp.notify(ssdp_notify("ssdp:byebye", usn="uuid:never-seen::" + MEDIA_RENDERER_NT))
    assert h.pending == []
    assert h.watcher.registry == {}


async def test_ssdp_alive_for_unknown_id_verifies_via_description_check():
    """alive from an unknown id only enters the registry AFTER the
    backend's description-XML verification (KTD4: alive is a hint, the
    XML check is authoritative) — and a 3-packet announcement burst
    fetches once."""
    h = await Harness().start()
    h.dlna_backend.describe_result = dlna_dev()
    pkt = ssdp_notify("ssdp:alive", location=DLNA_LOC)
    h.ssdp.notify(pkt)
    h.ssdp.notify(pkt)  # SSDP norm: alive repeats within the burst
    h.ssdp.notify(pkt)
    for _ in range(4):
        await asyncio.sleep(0)  # drain the verification task

    assert h.dlna_backend.describe_calls == [(DLNA_LOC, DLNA_USN)]
    key = ("dlna", DLNA_USN)
    assert h.watcher.registry[key].online is True
    assert h.probes == [(DLNA_HOST, "dlna", DLNA_USN)]


async def test_ssdp_alive_verification_rejecting_keeps_registry_clean():
    """describe_renderer returning None (non-renderer / unreachable)
    must leave no trace — the hint was wrong."""
    h = await Harness().start()
    h.dlna_backend.describe_result = None
    h.ssdp.notify(ssdp_notify("ssdp:alive", location=DLNA_LOC))
    for _ in range(4):
        await asyncio.sleep(0)
    assert h.dlna_backend.describe_calls == [(DLNA_LOC, DLNA_USN)]
    assert h.watcher.registry == {}
    assert h.probes == []


async def test_ssdp_non_renderer_and_malformed_packets_are_ignored():
    h = await Harness().start()
    # Non-renderer NOTIFY (a media server's alive) — filtered on NT/USN.
    h.ssdp.notify(ssdp_notify(
        "ssdp:alive", usn="uuid:nas::urn:schemas-upnp-org:device:MediaServer:1",
        nt="urn:schemas-upnp-org:device:MediaServer:1", location=DLNA_LOC))
    # M-SEARCH request and raw garbage — neither is a NOTIFY.
    h.ssdp.notify(b"M-SEARCH * HTTP/1.1\r\nMAN: \"ssdp:discover\"\r\n\r\n")
    h.ssdp.notify(b"\x00\xff garbage \xfe")
    await asyncio.sleep(0)
    assert h.watcher.registry == {}
    assert h.dlna_backend.describe_calls == []


async def test_stop_closes_ssdp_transport_and_drops_late_packets():
    h = await Harness().start()
    await h.watcher.stop()
    assert h.ssdp.transport.closed is True
    # A datagram already in flight when stop() ran must be dropped.
    h.ssdp.notify(ssdp_notify("ssdp:alive", location=DLNA_LOC))
    await asyncio.sleep(0)
    assert h.watcher.registry == {}
    assert h.timers.active == []
