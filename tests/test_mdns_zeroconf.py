"""Tests for the in-process python-zeroconf mDNS source (2026-06-15
passive-discovery plan U1).

The zeroconf layer is faked: ``AsyncServiceBrowser`` / ``AsyncServiceInfo`` are
monkeypatched so the browser's state-change handler can be driven by hand and
``ServiceInfo`` resolves replay scripted address/port/TXT data. No real socket,
no real multicast. Covers AE1 (the arrival-path source) at the unit level;
end-to-end arrival/departure through the watcher registry lives in
test_device_watcher.py.
"""

import asyncio

import pytest

from app.output import mdns_zeroconf
from zeroconf import ServiceStateChange

RAOP = "_raop._tcp.local"
CAST = "_googlecast._tcp.local"


# ── fakes ──────────────────────────────────────────────────────────────────────

class FakeAiozc:
    """Shape the source needs from a shared AsyncZeroconf: a ``.zeroconf``."""

    def __init__(self):
        self.zeroconf = object()


class FakeInfo:
    """Stands in for AsyncServiceInfo; replays scripted resolve data keyed by
    the full instance name."""

    registry: dict[str, dict] = {}

    def __init__(self, type_, name):
        self.type_ = type_
        self.name = name
        self._data = FakeInfo.registry.get(name)
        self.port = self._data.get("port") if self._data else None

    async def async_request(self, zc, timeout, *a, **k):
        return bool(self._data and self._data.get("ok", True))

    @property
    def decoded_properties(self):
        return dict(self._data.get("txt", {})) if self._data else {}

    def parsed_addresses(self, version=None):
        return list(self._data.get("addresses", [])) if self._data else []


class FakeBrowser:
    """Stands in for AsyncServiceBrowser; ``fire`` drives the captured handler.

    If ``auto_script`` is set on the class, __init__ fires those events
    synchronously (used by the one-shot discover() test, which awaits a sleep
    rather than calling fire by hand)."""

    instances: list["FakeBrowser"] = []
    auto_script: list[tuple[str, ServiceStateChange]] = []

    def __init__(self, zc, type_, handlers=None, **k):
        self.zc = zc
        self.type_ = type_
        self.handlers = handlers or []
        self.cancelled = False
        FakeBrowser.instances.append(self)
        for name, change in FakeBrowser.auto_script:
            self.fire(name, change)

    async def async_cancel(self):
        self.cancelled = True

    def fire(self, name, state_change):
        # Invoke handlers exactly as python-zeroconf's AsyncServiceBrowser does —
        # with service_type as a KEYWORD arg. A handler whose parameter is misnamed
        # (e.g. service_type_) then raises TypeError here, which is the regression
        # that flooded the event loop and shipped undetected (ce-code-review
        # 2026-07-24). Positional firing would mask exactly that class of bug.
        for h in self.handlers:
            h(self.zc, service_type=self.type_, name=name, state_change=state_change)


@pytest.fixture(autouse=True)
def _patch_zeroconf(monkeypatch):
    FakeBrowser.instances = []
    FakeBrowser.auto_script = []
    FakeInfo.registry = {}
    monkeypatch.setattr(mdns_zeroconf, "AsyncServiceBrowser", FakeBrowser)
    monkeypatch.setattr(mdns_zeroconf, "AsyncServiceInfo", FakeInfo)
    monkeypatch.setattr(mdns_zeroconf, "_ZEROCONF_AVAILABLE", True)
    yield


class Collector:
    def __init__(self):
        self.events: list[tuple[str, tuple]] = []

    def __call__(self, kind, payload):
        self.events.append((kind, payload))

    def kinds(self, kind):
        return [p for k, p in self.events if k == kind]


async def _drain():
    """Let call_soon_threadsafe dispatch + the resolve task run + emit."""
    for _ in range(6):
        await asyncio.sleep(0)


# ── tests ───────────────────────────────────────────────────────────────────────

async def test_unavailable_guard_returns_none(monkeypatch):
    monkeypatch.setattr(mdns_zeroconf, "_ZEROCONF_AVAILABLE", False)
    cb = Collector()
    assert await mdns_zeroconf.subscribe(RAOP, cb, FakeAiozc()) is None
    assert cb.events == []  # no crash, no status event


async def test_no_shared_aiozc_returns_none():
    cb = Collector()
    assert await mdns_zeroconf.subscribe(RAOP, cb, None) is None
    assert mdns_zeroconf.subscriptions_supported(None) is False


async def test_subscribe_emits_status_up_and_one_browser():
    cb = Collector()
    sub = await mdns_zeroconf.subscribe(RAOP, cb, FakeAiozc())
    assert sub is not None
    assert cb.kinds("status") == ["up"]
    assert len(FakeBrowser.instances) == 1
    assert FakeBrowser.instances[0].type_ == RAOP + "."  # FQDN trailing dot


async def test_add_resolves_and_emits_new_tuple():
    FakeInfo.registry["WiiM._raop._tcp.local."] = {
        "addresses": ["192.168.1.20"], "port": 7000,
        "txt": {"et": "0,4", "features": "0x4A7FCA00,0xBC354BD0"},
    }
    cb = Collector()
    sub = await mdns_zeroconf.subscribe(RAOP, cb, FakeAiozc())
    FakeBrowser.instances[0].fire("WiiM._raop._tcp.local.", ServiceStateChange.Added)
    await _drain()
    news = cb.kinds("new")
    assert len(news) == 1
    name, host, port, uuid, txt, service_type = news[0]
    assert name == "WiiM"                       # instance label, suffix stripped
    assert host == "192.168.1.20"
    assert port == 7000
    assert uuid is None                         # raop has no id= TXT
    assert txt == {"et": "0,4", "features": "0x4A7FCA00,0xBC354BD0"}
    assert service_type == RAOP                 # original (no trailing dot)


async def test_cast_id_txt_surfaces_as_uuid():
    uid = "308c00d1117fa74c600cb4c97d433fd4"
    FakeInfo.registry["JBL._googlecast._tcp.local."] = {
        "addresses": ["192.168.1.10"], "port": 8009,
        "txt": {"fn": "JBL Charge 5", "id": uid},
    }
    cb = Collector()
    await mdns_zeroconf.subscribe(CAST, cb, FakeAiozc())
    FakeBrowser.instances[0].fire("JBL._googlecast._tcp.local.", ServiceStateChange.Added)
    await _drain()
    assert cb.kinds("new")[0][3] == uid


async def test_malformed_id_txt_yields_none_uuid():
    FakeInfo.registry["Bad._googlecast._tcp.local."] = {
        "addresses": ["192.168.1.11"], "port": 8009, "txt": {"id": "not-a-uuid"},
    }
    cb = Collector()
    await mdns_zeroconf.subscribe(CAST, cb, FakeAiozc())
    FakeBrowser.instances[0].fire("Bad._googlecast._tcp.local.", ServiceStateChange.Added)
    await _drain()
    assert cb.kinds("new")[0][3] is None


async def test_remove_emits_remove_payload():
    cb = Collector()
    await mdns_zeroconf.subscribe(RAOP, cb, FakeAiozc())
    FakeBrowser.instances[0].fire("WiiM._raop._tcp.local.", ServiceStateChange.Removed)
    await _drain()
    assert cb.kinds("remove") == [("WiiM", RAOP)]


async def test_unresolvable_address_emits_nothing():
    FakeInfo.registry["Ghost._raop._tcp.local."] = {
        "addresses": [], "port": 7000, "ok": True,
    }
    cb = Collector()
    await mdns_zeroconf.subscribe(RAOP, cb, FakeAiozc())
    FakeBrowser.instances[0].fire("Ghost._raop._tcp.local.", ServiceStateChange.Added)
    await _drain()
    assert cb.kinds("new") == []


async def test_failed_request_emits_nothing():
    FakeInfo.registry["Slow._raop._tcp.local."] = {
        "addresses": ["192.168.1.30"], "port": 7000, "ok": False,
    }
    cb = Collector()
    await mdns_zeroconf.subscribe(RAOP, cb, FakeAiozc())
    FakeBrowser.instances[0].fire("Slow._raop._tcp.local.", ServiceStateChange.Added)
    await _drain()
    assert cb.kinds("new") == []


async def test_long_strings_are_length_bounded():
    long_name = "X" * 1000
    full = f"{long_name}._raop._tcp.local."
    FakeInfo.registry[full] = {
        "addresses": ["192.168.1.40"], "port": 7000,
        "txt": {"k" * 1000: "v" * 1000},
    }
    cb = Collector()
    await mdns_zeroconf.subscribe(RAOP, cb, FakeAiozc())
    FakeBrowser.instances[0].fire(full, ServiceStateChange.Added)
    await _drain()
    name, host, port, uuid, txt, _ = cb.kinds("new")[0]
    assert len(name) <= mdns_zeroconf._MAX_STR
    for k, v in txt.items():
        assert len(k) <= mdns_zeroconf._MAX_STR
        assert len(v) <= mdns_zeroconf._MAX_STR


async def test_unsubscribe_stops_events_and_releases_browser():
    FakeInfo.registry["WiiM._raop._tcp.local."] = {
        "addresses": ["192.168.1.20"], "port": 7000, "txt": {},
    }
    cb = Collector()
    sub = await mdns_zeroconf.subscribe(RAOP, cb, FakeAiozc())
    browser = FakeBrowser.instances[0]
    await mdns_zeroconf.unsubscribe(sub)
    assert browser.cancelled is True
    assert sub.active is False
    assert sub._resolve_tasks == set()  # no leaked resolve tasks
    # Events after unsubscribe are silenced.
    before = len(cb.events)
    browser.fire("WiiM._raop._tcp.local.", ServiceStateChange.Added)
    await _drain()
    assert len(cb.events) == before


async def test_unsubscribe_none_is_safe():
    await mdns_zeroconf.unsubscribe(None)  # no raise


async def test_off_loop_delivery_reaches_on_event():
    """A handler firing on a non-loop thread still delivers on the loop
    (thread-cross correctness via call_soon_threadsafe)."""
    FakeInfo.registry["WiiM._raop._tcp.local."] = {
        "addresses": ["192.168.1.20"], "port": 7000, "txt": {},
    }
    cb = Collector()
    await mdns_zeroconf.subscribe(RAOP, cb, FakeAiozc())
    browser = FakeBrowser.instances[0]
    # Fire from a worker thread, not the asyncio loop.
    await asyncio.to_thread(browser.fire, "WiiM._raop._tcp.local.", ServiceStateChange.Added)
    await _drain()
    assert cb.kinds("new")[0][1] == "192.168.1.20"


async def test_discover_one_shot_returns_resolved_tuples():
    FakeInfo.registry["WiiM._raop._tcp.local."] = {
        "addresses": ["192.168.1.20"], "port": 7000, "txt": {"et": "0,4"},
    }
    FakeBrowser.auto_script = [("WiiM._raop._tcp.local.", ServiceStateChange.Added)]
    found = await mdns_zeroconf.discover(RAOP, FakeAiozc(), timeout=0.01)
    assert found == [("WiiM", "192.168.1.20", 7000, None, {"et": "0,4"})]
    assert FakeBrowser.instances[0].cancelled is True  # one-shot tears down


async def test_discover_unavailable_returns_none(monkeypatch):
    monkeypatch.setattr(mdns_zeroconf, "_ZEROCONF_AVAILABLE", False)
    assert await mdns_zeroconf.discover(RAOP, FakeAiozc(), timeout=0.01) is None
