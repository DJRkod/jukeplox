"""Tests for the lifted probe scheduler (2026-06-11 live-discovery plan U4).

The scheduling body is a verbatim lift from app/api/admin.py — its
behavior stays covered by tests/test_api_admin.py through the route.
Here we cover only what is NEW in the lift: the lazy app.state backends
mapping (KTD6) and the watcher-facing single-entry trigger
``probe_host`` the route never exercises.
"""

import asyncio
from unittest.mock import AsyncMock, patch

from app.output import probe_runner
from app.output.discovery import AggregatedDevice, ProtocolEntry


async def _drain():
    """Let the fire-and-forget probe tasks run to completion."""
    for _ in range(4):
        await asyncio.sleep(0)


# ── lazy backends mapping (KTD6) ──────────────────────────────────────────────

async def test_default_backends_built_lazily_from_app_state():
    """With no backends= passed, schedule_probes reads app.state at call
    time — the watcher never constructs the map."""
    with patch("app.state.direct_backend") as db, \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.dlna_backend") as lb, \
         patch("app.state.airplay_backend") as ab, \
         patch("app.output.probe_cache.set_verdict", AsyncMock()) as verdicts:
        ab.probe_device = AsyncMock(return_value=True)
        dev = AggregatedDevice(host="192.168.1.50", name="WiiM", protocols=[
            ProtocolEntry(backend="airplay", device_id="192.168.1.50:7000",
                          verified=None),
        ])
        await probe_runner.schedule_probes([dev])
        await _drain()

    ab.probe_device.assert_awaited_once_with("192.168.1.50:7000")
    verdicts.assert_awaited_once_with("192.168.1.50", "airplay", True)
    # The mapping carries all four backend names, straight from app.state.
    db.probe_device.assert_not_called()
    cb.probe_device.assert_not_called()
    lb.probe_device.assert_not_called()


async def test_default_backends_mapping_names():
    """The lazy map's keys mirror the route's hand-built dict exactly —
    a backend the route knows but the map misses would silently skip
    every watcher-triggered probe for it."""
    with patch("app.state.direct_backend"), patch("app.state.chromecast_backend"), \
         patch("app.state.dlna_backend"), patch("app.state.airplay_backend"):
        backends = probe_runner._default_backends()
    assert set(backends) == {"direct", "chromecast", "dlna", "airplay"}


# ── probe_host (the watcher's trigger) ────────────────────────────────────────

async def test_probe_host_probes_one_entry_and_writes_verdict():
    with patch("app.state.airplay_backend") as ab, \
         patch("app.output.probe_cache.set_verdict", AsyncMock()) as verdicts:
        ab.probe_device = AsyncMock(return_value=True)
        probe_runner.probe_host("192.168.1.50", "airplay", "192.168.1.50:7000")
        await _drain()

    ab.probe_device.assert_awaited_once_with("192.168.1.50:7000")
    verdicts.assert_awaited_once_with("192.168.1.50", "airplay", True)


async def test_probe_host_failure_writes_false_verdict_and_never_raises():
    """A failing probe is a Via-level verdict (False), not an exception —
    presence decisions stay with the watcher's registry."""
    with patch("app.state.airplay_backend") as ab, \
         patch("app.output.probe_cache.set_verdict", AsyncMock()) as verdicts:
        ab.probe_device = AsyncMock(side_effect=RuntimeError("unreachable"))
        probe_runner.probe_host("192.168.1.50", "airplay", "192.168.1.50:7000")
        await _drain()

    verdicts.assert_awaited_once_with("192.168.1.50", "airplay", False)
