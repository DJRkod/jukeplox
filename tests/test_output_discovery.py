"""Tests for app.output.discovery — per-host aggregation of per-backend
discovery results.

The aggregator is the pure function that turns the per-backend lists the
admin discovery API gathers into the per-physical-device list the picker
consumes. Tests here exercise the algorithm in isolation — no
backend instances, no I/O.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.output.base import OutputDevice
from app.output.discovery import (
    AggregatedDevice,
    ProtocolEntry,
    aggregate_devices,
    build_devices_snapshot,
    build_registry_snapshot,
    host_for,
)
from app.output.probe_cache import Verdict


def _make_verdicts(items):
    """Helper: build the `{(host, backend): Verdict}` map the aggregator
    expects. items is a list of `(host, backend, ok)` tuples."""
    return {(h, b): Verdict(ok=ok, checked_at=1700000000.0) for h, b, ok in items}


def _make_host_for(mapping):
    """Helper: return a host_for callable that consults a `(d.id, backend)`
    → host dict. Tests use this to inject the per-backend extraction
    without instantiating real backends."""
    def host_for(d, backend):
        return mapping.get((d.id, backend))
    return host_for


# ── happy paths ──────────────────────────────────────────────────────────────


def test_aggregate_merges_three_protocols_at_same_host():
    """AE1 from the brainstorm: one physical device at 192.168.1.50
    reachable via AirPlay + Chromecast + DLNA → one AggregatedDevice
    labeled with the clean Chromecast/DLNA friendly name (AirPlay's
    post-strip name is overridden by higher-priority sources)."""
    per_backend = {
        "direct": [],
        "airplay": [OutputDevice(id="192.168.1.50:7000", name="WiiM Pro-E5F6",
                                 backend_type="airplay", id_format="host_port")],
        "chromecast": [OutputDevice(id="uuid:cc-1", name="WiiM Pro",
                                    backend_type="chromecast")],
        "dlna": [OutputDevice(id="uuid:dlna-1", name="WiiM Pro",
                              backend_type="dlna")],
    }
    host_for = _make_host_for({
        ("192.168.1.50:7000", "airplay"): "192.168.1.50",
        ("uuid:cc-1", "chromecast"): "192.168.1.50",
        ("uuid:dlna-1", "dlna"): "192.168.1.50",
    })
    verdicts = _make_verdicts([
        ("192.168.1.50", "airplay", True),
        ("192.168.1.50", "chromecast", True),
        ("192.168.1.50", "dlna", True),
    ])

    result = aggregate_devices(per_backend, host_for, verdicts)

    # No Direct in input → no Direct entry; just the one merged device.
    assert len(result) == 1
    dev = result[0]
    assert dev.host == "192.168.1.50"
    # DLNA/Chromecast friendly name wins over AirPlay's post-strip.
    assert dev.name == "WiiM Pro"
    assert {p.backend for p in dev.protocols} == {"airplay", "chromecast", "dlna"}
    assert all(p.verified is True for p in dev.protocols)


def test_aggregate_single_backend_device_passes_through():
    """A device only one backend can reach → one AggregatedDevice with
    one ProtocolEntry. No merge, no name priority drama."""
    per_backend = {
        "direct": [],
        "airplay": [],
        "chromecast": [OutputDevice(id="uuid:cc-1", name="Nest Mini",
                                    backend_type="chromecast")],
        "dlna": [],
    }
    host_for = _make_host_for({("uuid:cc-1", "chromecast"): "10.0.0.5"})
    verdicts = _make_verdicts([("10.0.0.5", "chromecast", True)])

    result = aggregate_devices(per_backend, host_for, verdicts)

    assert len(result) == 1
    assert result[0].host == "10.0.0.5"
    assert result[0].name == "Nest Mini"
    assert len(result[0].protocols) == 1
    assert result[0].protocols[0].backend == "chromecast"


def test_aggregate_passes_direct_through_bypassing_dedupe():
    """The Direct ("System Audio") pseudo-device is not network-bound and
    must always appear first, never merged with any host. Uses a sentinel
    host so the frontend can distinguish it."""
    per_backend = {
        "direct": [OutputDevice(id="default", name="System Audio",
                                backend_type="direct")],
        "airplay": [OutputDevice(id="192.168.1.50:7000", name="WiiM",
                                 backend_type="airplay", id_format="host_port")],
        "chromecast": [],
        "dlna": [],
    }
    host_for = _make_host_for({("192.168.1.50:7000", "airplay"): "192.168.1.50"})
    verdicts = _make_verdicts([("192.168.1.50", "airplay", True)])

    result = aggregate_devices(per_backend, host_for, verdicts)

    assert len(result) == 2
    assert result[0].host == "__direct__"
    assert result[0].name == "System Audio"
    assert result[0].protocols[0].backend == "direct"
    assert result[0].protocols[0].verified is True
    assert result[1].name == "WiiM"


def test_aggregate_sorts_direct_first_then_alphabetical_by_name():
    """System Audio first; remaining devices sorted alphabetically by
    name so the picker order is predictable across discovery cycles."""
    per_backend = {
        "direct": [OutputDevice(id="default", name="System Audio",
                                backend_type="direct")],
        "airplay": [
            OutputDevice(id="192.168.1.30:7000", name="Charlie",
                         backend_type="airplay", id_format="host_port"),
            OutputDevice(id="192.168.1.10:7000", name="Alpha",
                         backend_type="airplay", id_format="host_port"),
            OutputDevice(id="192.168.1.20:7000", name="Bravo",
                         backend_type="airplay", id_format="host_port"),
        ],
        "chromecast": [],
        "dlna": [],
    }
    host_for = _make_host_for({
        ("192.168.1.10:7000", "airplay"): "192.168.1.10",
        ("192.168.1.20:7000", "airplay"): "192.168.1.20",
        ("192.168.1.30:7000", "airplay"): "192.168.1.30",
    })

    result = aggregate_devices(per_backend, host_for, {})

    assert [d.name for d in result] == ["System Audio", "Alpha", "Bravo", "Charlie"]


# ── verdict states ──────────────────────────────────────────────────────────


def test_aggregate_marks_unprobed_entries_as_none():
    """A device discovered but not yet probed surfaces with verified=None
    (the "Checking…" signal). The aggregator does not filter — that's
    the picker's job; this keeps the function pure and lossless."""
    per_backend = {
        "direct": [],
        "airplay": [OutputDevice(id="192.168.1.50:7000", name="WiiM",
                                 backend_type="airplay", id_format="host_port")],
        "chromecast": [],
        "dlna": [],
    }
    host_for = _make_host_for({("192.168.1.50:7000", "airplay"): "192.168.1.50"})
    # No verdicts at all.
    result = aggregate_devices(per_backend, host_for, {})

    assert len(result) == 1
    assert result[0].protocols[0].verified is None


def test_aggregate_carries_false_verdicts_through_unchanged():
    """ok=False stays as verified=False on the ProtocolEntry. The picker
    will filter these from the Via dropdown; the aggregator preserves
    them so failure observability isn't lost."""
    per_backend = {
        "direct": [],
        "airplay": [],
        "chromecast": [OutputDevice(id="uuid:cc-1", name="Nest",
                                    backend_type="chromecast")],
        "dlna": [],
    }
    host_for = _make_host_for({("uuid:cc-1", "chromecast"): "10.0.0.5"})
    verdicts = _make_verdicts([("10.0.0.5", "chromecast", False)])

    result = aggregate_devices(per_backend, host_for, verdicts)

    assert len(result) == 1
    assert result[0].protocols[0].verified is False


def test_aggregate_exposes_checked_at_when_verdict_present():
    """checked_at is surfaced on the ProtocolEntry so the frontend can
    detect stuck probes — an entry older than 45s with verified=None
    gets rendered as 'Could not verify' in the picker."""
    per_backend = {
        "direct": [],
        "airplay": [],
        "chromecast": [OutputDevice(id="uuid:cc-1", name="Nest",
                                    backend_type="chromecast")],
        "dlna": [],
    }
    host_for = _make_host_for({("uuid:cc-1", "chromecast"): "10.0.0.5"})
    verdicts = {("10.0.0.5", "chromecast"): Verdict(ok=True, checked_at=1717891234.5)}

    result = aggregate_devices(per_backend, host_for, verdicts)

    assert result[0].protocols[0].checked_at == 1717891234.5


def test_aggregate_unprobed_entry_has_none_checked_at():
    """An unprobed entry's checked_at is None so the frontend can
    distinguish 'no verdict yet' from 'verdict with timestamp'."""
    per_backend = {
        "direct": [],
        "airplay": [OutputDevice(id="192.168.1.50:7000", name="WiiM",
                                 backend_type="airplay", id_format="host_port")],
        "chromecast": [],
        "dlna": [],
    }
    host_for = _make_host_for({("192.168.1.50:7000", "airplay"): "192.168.1.50"})

    result = aggregate_devices(per_backend, host_for, {})

    assert result[0].protocols[0].checked_at is None


# ── name priority ────────────────────────────────────────────────────────────


def test_aggregate_airplay_arrives_first_dlna_overrides_name():
    """AirPlay processed before DLNA/Chromecast (alphabetical) → bucket
    seeded with the AirPlay name, then replaced when the DLNA entry
    lands. Pins the rank-based priority rule."""
    per_backend = {
        "direct": [],
        "airplay": [OutputDevice(id="192.168.1.50:7000", name="WiiM Pro-E5F6",
                                 backend_type="airplay", id_format="host_port")],
        "chromecast": [],
        "dlna": [OutputDevice(id="uuid:dlna-1", name="WiiM Pro",
                              backend_type="dlna")],
    }
    host_for = _make_host_for({
        ("192.168.1.50:7000", "airplay"): "192.168.1.50",
        ("uuid:dlna-1", "dlna"): "192.168.1.50",
    })

    result = aggregate_devices(per_backend, host_for, {})

    assert result[0].name == "WiiM Pro"  # DLNA wins over AirPlay


def test_aggregate_chromecast_does_not_override_dlna_name():
    """Chromecast and DLNA are equal priority — neither replaces the
    other. First-arrival wins among equal-rank backends so a device
    with both protocols keeps a stable name across discovery cycles."""
    per_backend = {
        "direct": [],
        "airplay": [],
        "chromecast": [OutputDevice(id="uuid:cc-1", name="Cast Name",
                                    backend_type="chromecast")],
        "dlna": [OutputDevice(id="uuid:dlna-1", name="DLNA Name",
                              backend_type="dlna")],
    }
    host_for = _make_host_for({
        ("uuid:cc-1", "chromecast"): "192.168.1.50",
        ("uuid:dlna-1", "dlna"): "192.168.1.50",
    })

    result = aggregate_devices(per_backend, host_for, {})

    # Walk order is alphabetical (chromecast before dlna), so Chromecast
    # seeds the bucket and DLNA (equal rank) does not replace.
    assert result[0].name == "Cast Name"


# ── edge cases ──────────────────────────────────────────────────────────────


def test_aggregate_drops_device_when_host_for_returns_none():
    """host_for returns None for a device (e.g., a DLNA LOCATION URL
    with no hostname or a backend that doesn't carry the device in its
    address cache) → the device is dropped, others unaffected."""
    per_backend = {
        "direct": [],
        "airplay": [],
        "chromecast": [
            OutputDevice(id="uuid:cc-1", name="Reachable", backend_type="chromecast"),
            OutputDevice(id="uuid:cc-orphan", name="Orphan", backend_type="chromecast"),
        ],
        "dlna": [],
    }
    host_for = _make_host_for({
        ("uuid:cc-1", "chromecast"): "10.0.0.5",
        # uuid:cc-orphan deliberately absent.
    })

    result = aggregate_devices(per_backend, host_for, {})

    assert len(result) == 1
    assert result[0].name == "Reachable"


def test_aggregate_dedupes_same_backend_twice_at_same_host():
    """Rare multi-NIC variant: two entries from the same backend resolve
    to the same host. Keep the first; drop the rest so the Via dropdown
    doesn't show 'AirPlay' twice for the same device."""
    per_backend = {
        "direct": [],
        "airplay": [
            OutputDevice(id="192.168.1.50:7000", name="WiiM A",
                         backend_type="airplay", id_format="host_port"),
            OutputDevice(id="192.168.1.50:7001", name="WiiM B",
                         backend_type="airplay", id_format="host_port"),
        ],
        "chromecast": [],
        "dlna": [],
    }
    host_for = _make_host_for({
        ("192.168.1.50:7000", "airplay"): "192.168.1.50",
        ("192.168.1.50:7001", "airplay"): "192.168.1.50",
    })

    result = aggregate_devices(per_backend, host_for, {})

    assert len(result) == 1
    # Exactly one AirPlay ProtocolEntry; the first one wins.
    airplay_entries = [p for p in result[0].protocols if p.backend == "airplay"]
    assert len(airplay_entries) == 1
    assert airplay_entries[0].device_id == "192.168.1.50:7000"


def test_aggregate_empty_input_returns_empty_list():
    """All backends returned zero devices and Direct is also empty →
    empty result (not None, not an exception)."""
    per_backend = {"direct": [], "airplay": [], "chromecast": [], "dlna": []}
    host_for = _make_host_for({})

    assert aggregate_devices(per_backend, host_for, {}) == []


# ── availability (U5, KTD8) ──────────────────────────────────────────────────


def _wiim_two_protocols():
    """One physical device reachable via airplay + dlna, for the
    availability-OR vectors."""
    per_backend = {
        "airplay": [OutputDevice(id="192.168.1.50:7000", name="WiiM",
                                 backend_type="airplay", id_format="host_port")],
        "dlna": [OutputDevice(id="uuid:dlna-1", name="WiiM",
                              backend_type="dlna")],
    }
    hf = _make_host_for({
        ("192.168.1.50:7000", "airplay"): "192.168.1.50",
        ("uuid:dlna-1", "dlna"): "192.168.1.50",
    })
    return per_backend, hf


def test_aggregate_without_availability_marks_everything_online():
    """No availability map (the legacy pull path) → every device is
    online with no offline_since — today's semantics, byte-for-byte."""
    per_backend, hf = _wiim_two_protocols()
    result = aggregate_devices(per_backend, hf, {})

    assert result[0].online is True
    assert result[0].offline_since is None


def test_aggregate_all_entries_offline_flags_device_with_latest_stamp():
    """Every contributing registry entry offline → device offline, and
    offline_since is the LATEST flip (the moment the device became
    unreachable on its last protocol)."""
    per_backend, hf = _wiim_two_protocols()
    availability = {
        ("airplay", "192.168.1.50:7000"): (False, 1000.0),
        ("dlna", "uuid:dlna-1"): (False, 1200.0),
    }
    result = aggregate_devices(per_backend, hf, {}, availability=availability)

    assert result[0].online is False
    assert result[0].offline_since == 1200.0
    # Offline devices are RETAINED in the output — present-with-flag, the
    # picker greys them out instead of vanishing (origin R2).
    assert {p.backend for p in result[0].protocols} == {"airplay", "dlna"}


def test_aggregate_any_online_entry_keeps_device_online():
    """One protocol still online → the physical device is online and
    carries no offline_since, regardless of arrival order."""
    per_backend, hf = _wiim_two_protocols()
    availability = {
        ("airplay", "192.168.1.50:7000"): (False, 1000.0),
        ("dlna", "uuid:dlna-1"): (True, None),
    }
    result = aggregate_devices(per_backend, hf, {}, availability=availability)

    assert result[0].online is True
    assert result[0].offline_since is None


def test_aggregate_direct_always_online_even_with_availability_map():
    """The Direct pseudo-device never enters the registry; with an
    availability map present it stays online unconditionally (KTD8)."""
    per_backend = {
        "direct": [OutputDevice(id="default", name="System Audio",
                                backend_type="direct")],
        "airplay": [OutputDevice(id="192.168.1.50:7000", name="WiiM",
                                 backend_type="airplay", id_format="host_port")],
    }
    hf = _make_host_for({("192.168.1.50:7000", "airplay"): "192.168.1.50"})
    availability = {("airplay", "192.168.1.50:7000"): (False, 1000.0)}
    result = aggregate_devices(per_backend, hf, {}, availability=availability)

    assert result[0].host == "__direct__"
    assert result[0].online is True
    assert result[0].offline_since is None
    assert result[1].online is False


def test_aggregate_device_missing_from_availability_counts_online():
    """A device the map doesn't know (e.g. found by a forced one-shot a
    beat before the registry upsert lands) defaults to online — absence
    of evidence is not an outage."""
    per_backend, hf = _wiim_two_protocols()
    availability = {("airplay", "192.168.1.50:7000"): (False, 1000.0)}
    result = aggregate_devices(per_backend, hf, {}, availability=availability)

    assert result[0].online is True


# ── host_for (lifted from admin.py — U5, KTD11) ──────────────────────────────


def _fake_backends(airplay=None, chromecast=None, dlna=None):
    """backend_for callable over stub instances shaped like the real
    backends' address caches."""
    return {"airplay": airplay, "chromecast": chromecast, "dlna": dlna}.get


def _stub_chromecast(dbus_index=None, cast_infos=None):
    return SimpleNamespace(
        _discover_lock=threading.Lock(),
        _dbus_index=dbus_index or {},
        _cast_infos=cast_infos or {},
    )


def test_host_for_airplay_reads_device_addr():
    backend = SimpleNamespace(_device_addr={
        "192.168.1.20:7000": ("AA@JBL", "192.168.1.20", 7000, {}),
    })
    d = OutputDevice(id="192.168.1.20:7000", name="JBL",
                     backend_type="airplay", id_format="host_port")
    assert host_for(d, "airplay", _fake_backends(airplay=backend)) == "192.168.1.20"


def test_host_for_chromecast_prefers_dbus_index_then_cast_infos():
    d = OutputDevice(id="uuid:cc-1", name="Nest", backend_type="chromecast")
    via_dbus = _stub_chromecast(dbus_index={"uuid:cc-1": ("Nest", "10.0.0.5", 8009)})
    assert host_for(d, "chromecast", _fake_backends(chromecast=via_dbus)) == "10.0.0.5"

    via_browser = _stub_chromecast(
        cast_infos={"uuid:cc-1": SimpleNamespace(host="10.0.0.6")})
    assert host_for(d, "chromecast", _fake_backends(chromecast=via_browser)) == "10.0.0.6"


def test_host_for_dlna_parses_location_url():
    backend = SimpleNamespace(_device_locations={
        "uuid:dlna-1": "http://192.168.1.77:49152/description.xml",
    })
    d = OutputDevice(id="uuid:dlna-1", name="WiiM", backend_type="dlna")
    assert host_for(d, "dlna", _fake_backends(dlna=backend)) == "192.168.1.77"


def test_host_for_unknown_device_or_backend_returns_none():
    """Missing backend instance, device absent from every cache, and an
    unknown backend name all resolve to None — the aggregator drops
    those rather than bucketing under a junk key."""
    d = OutputDevice(id="ghost", name="Ghost", backend_type="airplay",
                     id_format="host_port")
    assert host_for(d, "airplay", _fake_backends()) is None
    assert host_for(
        d, "airplay",
        _fake_backends(airplay=SimpleNamespace(_device_addr={}))) is None
    assert host_for(d, "bluetooth", _fake_backends()) is None


# ── build_devices_snapshot / build_registry_snapshot (U5, KTD11) ─────────────


async def test_build_devices_snapshot_serializes_payload_and_returns_aggregated():
    """The builder loads verdicts, aggregates through the lifted host_for
    and returns (payload, aggregated): JSON-ready dicts in the exact GET
    shape — host/name/online/offline_since/protocols — plus the
    AggregatedDevice list the route feeds to schedule_probes."""
    airplay = SimpleNamespace(_device_addr={
        "192.168.1.50:7000": ("AA@WiiM", "192.168.1.50", 7000, {}),
    })
    per_backend = {
        "airplay": [OutputDevice(id="192.168.1.50:7000", name="WiiM",
                                 backend_type="airplay", id_format="host_port")],
    }
    verdicts = {("192.168.1.50", "airplay"): Verdict(ok=True, checked_at=42.0)}

    with patch("app.output.probe_cache.fetch_all",
               AsyncMock(return_value=verdicts)):
        payload, aggregated = await build_devices_snapshot(
            per_backend, backend_for=_fake_backends(airplay=airplay))

    assert payload == [{
        "host": "192.168.1.50",
        "name": "WiiM",
        "online": True,
        "offline_since": None,
        "protocols": [{
            "backend": "airplay",
            "device_id": "192.168.1.50:7000",
            "verified": True,
            "checked_at": 42.0,
        }],
    }]
    assert len(aggregated) == 1
    assert isinstance(aggregated[0], AggregatedDevice)
    assert aggregated[0].host == "192.168.1.50"


async def test_build_devices_snapshot_survives_probe_cache_failure():
    """A verdict-cache error degrades to an unverified aggregation —
    exactly the route's old fail-soft — never an exception."""
    airplay = SimpleNamespace(_device_addr={
        "192.168.1.50:7000": ("AA@WiiM", "192.168.1.50", 7000, {}),
    })
    per_backend = {
        "airplay": [OutputDevice(id="192.168.1.50:7000", name="WiiM",
                                 backend_type="airplay", id_format="host_port")],
    }
    with patch("app.output.probe_cache.fetch_all",
               AsyncMock(side_effect=RuntimeError("db down"))):
        payload, _ = await build_devices_snapshot(
            per_backend, backend_for=_fake_backends(airplay=airplay))

    assert payload[0]["protocols"][0]["verified"] is None


async def test_build_registry_snapshot_carries_flags_and_appends_direct():
    """Registry entries flow through with their online/offline_since
    flags; the Direct pseudo-device is appended via its backend's local
    discover, always online — the payload the watcher broadcasts IS the
    GET body (KTD5)."""
    airplay = SimpleNamespace(_device_addr={
        "192.168.1.50:7000": ("AA@WiiM", "192.168.1.50", 7000, {}),
    })
    direct = SimpleNamespace(discover_devices=AsyncMock(return_value=[
        OutputDevice(id="default", name="System Audio", backend_type="direct"),
    ]))
    backend_for = {"airplay": airplay, "direct": direct}.get
    registry = {
        ("airplay", "192.168.1.50:7000"): SimpleNamespace(
            device=OutputDevice(id="192.168.1.50:7000", name="WiiM",
                                backend_type="airplay", id_format="host_port"),
            online=False, offline_since=1234.5),
    }

    with patch("app.output.probe_cache.fetch_all", AsyncMock(return_value={})):
        payload, _ = await build_registry_snapshot(registry, backend_for=backend_for)

    by_host = {d["host"]: d for d in payload}
    assert by_host["__direct__"]["online"] is True
    assert by_host["192.168.1.50"]["online"] is False
    assert by_host["192.168.1.50"]["offline_since"] == 1234.5


async def test_build_registry_snapshot_direct_failure_is_fail_soft():
    """Direct discover raising costs only the Direct entry — the rest of
    the snapshot still builds (a broadcast must never die on GStreamer)."""
    direct = SimpleNamespace(
        discover_devices=AsyncMock(side_effect=RuntimeError("gst exploded")))
    with patch("app.output.probe_cache.fetch_all", AsyncMock(return_value={})):
        payload, _ = await build_registry_snapshot(
            {}, backend_for={"direct": direct}.get)

    assert payload == []
