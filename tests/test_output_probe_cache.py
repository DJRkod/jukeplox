"""Tests for app.output.probe_cache — per-(host, backend) verdict storage.

The verdict cache rides on the existing settings table behind the
``device_protocol:`` prefix. Each entry is JSON-encoded:
``{"ok": true|false, "checked_at": <float>}``. This module owns the
prefix discipline so callers don't reach into settings keys directly.
"""

from __future__ import annotations

import json
import time

import pytest

from app import database
from app.config import Settings


@pytest.fixture
def tmp_settings(tmp_path, monkeypatch):
    s = Settings(data_dir=tmp_path, secret_key="test")
    monkeypatch.setattr(database, "settings", s)
    return s


@pytest.fixture
async def db(tmp_settings):
    await database.init_db()
    return tmp_settings


# ── round-trip ───────────────────────────────────────────────────────────────


async def test_set_then_get_returns_verdict(db):
    """set_verdict → get_verdict returns a Verdict carrying both fields.
    checked_at is set to roughly time.time() at write — assert within
    a generous window so the test isn't clock-flaky."""
    from app.output.probe_cache import set_verdict, get_verdict

    before = time.time()
    await set_verdict("192.168.1.50", "chromecast", True)
    after = time.time()

    v = await get_verdict("192.168.1.50", "chromecast")
    assert v is not None
    assert v.ok is True
    assert before - 0.01 <= v.checked_at <= after + 0.01


async def test_get_verdict_returns_none_for_missing_key(db):
    """No verdict stored → get_verdict returns None so the aggregator can
    surface 'Checking…' rather than guessing a default."""
    from app.output.probe_cache import get_verdict
    assert await get_verdict("10.0.0.1", "airplay") is None


async def test_set_verdict_overwrites_existing(db):
    """A second set_verdict for the same (host, backend) replaces the
    prior verdict — rescan + re-probe must not stack stale entries."""
    from app.output.probe_cache import set_verdict, get_verdict

    await set_verdict("192.168.1.10", "dlna", True)
    await set_verdict("192.168.1.10", "dlna", False)

    v = await get_verdict("192.168.1.10", "dlna")
    assert v is not None
    assert v.ok is False


async def test_set_verdict_with_false(db):
    """ok=False stores a fail verdict that the picker can filter out so
    broken protocols don't reach the Via dropdown."""
    from app.output.probe_cache import set_verdict, get_verdict

    await set_verdict("192.168.1.50", "dlna", False)
    v = await get_verdict("192.168.1.50", "dlna")
    assert v is not None
    assert v.ok is False


# ── corruption tolerance ─────────────────────────────────────────────────────


async def test_invalid_json_in_storage_returns_none(db):
    """A row written outside this module (or corrupted on disk) parses to
    None rather than raising — the caller's behavior matches the missing
    case, which is the safe direction (re-probe rather than crash)."""
    from app.output.probe_cache import get_verdict

    await database.set_setting("device_protocol:192.168.1.50:airplay", "{not-json")
    assert await get_verdict("192.168.1.50", "airplay") is None


async def test_missing_fields_in_json_returns_none(db):
    """A row with valid JSON but missing required fields also returns
    None — same safe-direction reasoning as the invalid-JSON case."""
    from app.output.probe_cache import get_verdict

    # Missing 'ok' field
    await database.set_setting(
        "device_protocol:192.168.1.50:airplay", json.dumps({"checked_at": 1.0})
    )
    assert await get_verdict("192.168.1.50", "airplay") is None


# ── IPv6 host edge case ──────────────────────────────────────────────────────


async def test_ipv6_host_with_colons_roundtrips(db):
    """IPv6 hosts contain colons (`fe80::1`). The cache key shape is
    `device_protocol:{host}:{backend}` — to disambiguate, we split on the
    LAST colon when reading back. Pin that behavior with a test so a
    future refactor that switches to first-colon split fails loudly."""
    from app.output.probe_cache import set_verdict, get_verdict

    await set_verdict("fe80::1", "chromecast", True)

    v = await get_verdict("fe80::1", "chromecast")
    assert v is not None
    assert v.ok is True


# ── prefix isolation: clear_verdicts_for_host ───────────────────────────────


async def test_clear_verdicts_for_host_removes_only_that_host(db):
    """clear_verdicts_for_host('A') removes A's verdicts across every
    backend but leaves B's entries intact. Powers the per-host reprobe
    flow (deferred to follow-up plan) and the bust=true equivalent."""
    from app.output.probe_cache import set_verdict, get_verdict, clear_verdicts_for_host

    await set_verdict("192.168.1.50", "airplay", True)
    await set_verdict("192.168.1.50", "chromecast", True)
    await set_verdict("192.168.1.51", "airplay", True)

    await clear_verdicts_for_host("192.168.1.50")

    assert await get_verdict("192.168.1.50", "airplay") is None
    assert await get_verdict("192.168.1.50", "chromecast") is None
    # Unrelated host preserved.
    assert (await get_verdict("192.168.1.51", "airplay")).ok is True


async def test_clear_verdicts_for_host_no_matches_is_noop(db):
    """clear_verdicts_for_host on a host with no entries is a no-op —
    callers shouldn't have to check existence first."""
    from app.output.probe_cache import set_verdict, get_verdict, clear_verdicts_for_host

    await set_verdict("192.168.1.50", "airplay", True)
    await clear_verdicts_for_host("10.0.0.99")  # never had any verdicts
    # Existing entry still there.
    assert (await get_verdict("192.168.1.50", "airplay")).ok is True


# ── prefix isolation: clear_all_verdicts ────────────────────────────────────


async def test_clear_all_verdicts_removes_only_device_protocol_entries(db):
    """clear_all_verdicts removes every device_protocol:* key but leaves
    every other setting (volumes, AirPlay AP1/AP2 verdicts, output_*)
    untouched. This is the bust=true path's cache-clear step."""
    from app.output.probe_cache import set_verdict, clear_all_verdicts

    # Verdicts that should be cleared.
    await set_verdict("192.168.1.50", "airplay", True)
    await set_verdict("192.168.1.51", "chromecast", False)

    # Unrelated settings that must survive.
    await database.set_setting("vol:airplay:192.168.1.50:7000", "0.5")
    await database.set_setting("airplay:protocol:192.168.1.50:7000", "ap2")
    await database.set_setting("output_backend_type", "airplay")
    await database.set_setting("output_device_id", "192.168.1.50:7000")

    await clear_all_verdicts()

    # device_protocol:* gone.
    assert await database.get_setting("device_protocol:192.168.1.50:airplay") is None
    assert await database.get_setting("device_protocol:192.168.1.51:chromecast") is None
    # Everything else preserved.
    assert await database.get_setting("vol:airplay:192.168.1.50:7000") == "0.5"
    assert await database.get_setting("airplay:protocol:192.168.1.50:7000") == "ap2"
    assert await database.get_setting("output_backend_type") == "airplay"
    assert await database.get_setting("output_device_id") == "192.168.1.50:7000"


async def test_clear_all_verdicts_on_empty_cache_is_noop(db):
    """Calling clear_all_verdicts when no verdicts exist is harmless and
    must not touch unrelated settings."""
    from app.output.probe_cache import clear_all_verdicts

    await database.set_setting("output_backend_type", "direct")
    await clear_all_verdicts()
    assert await database.get_setting("output_backend_type") == "direct"
