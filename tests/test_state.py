"""Tests for app.state startup reconnect behavior and advance-lock guard."""

import asyncio
import contextlib

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


async def test_registry_includes_local_source_when_configured():
    """U11: a stored local source builds a LocalSource into the registry (the
    additive wiring mirroring U10's Jellyfin path)."""
    import app.state as st
    import app.database as db
    st.invalidate_plex_client()
    with patch.object(db, "get_plex_servers", AsyncMock(return_value=[])), \
         patch.object(db, "get_plex_config", AsyncMock(return_value=None)), \
         patch.object(db, "get_jellyfin_sources", AsyncMock(return_value=[])), \
         patch.object(db, "get_local_sources", AsyncMock(return_value=[
             {"source_id": "local-1", "name": "Vinyl", "root_dir": "/music"}])):
        reg = await st.get_plex_client()
    st.invalidate_plex_client()
    assert reg is not None
    local = next(s for s in reg.sources if s.source_type == "local")
    assert local.source_id == "local-1"
    assert local.server_name == "Vinyl"


async def test_registry_none_when_no_sources_at_all():
    """AE6 additivity: the local loop is empty-safe — a zero-source install still
    returns None (no registry), exactly as before U11."""
    import app.state as st
    import app.database as db
    st.invalidate_plex_client()
    with patch.object(db, "get_plex_servers", AsyncMock(return_value=[])), \
         patch.object(db, "get_plex_config", AsyncMock(return_value=None)), \
         patch.object(db, "get_jellyfin_sources", AsyncMock(return_value=[])), \
         patch.object(db, "get_local_sources", AsyncMock(return_value=[])):
        reg = await st.get_plex_client()
    st.invalidate_plex_client()
    assert reg is None


async def test_startup_reconnect_calls_discover_then_set_device():
    """Without a cached address, falls through to discover_devices then set_device."""
    from app.state import _startup_reconnect
    import app.database as db
    backend = MagicMock()
    backend.discover_devices = AsyncMock()
    backend.set_device = AsyncMock()
    with patch.object(db, "get_setting", AsyncMock(return_value=None)):
        await _startup_reconnect(backend, "dev-123")
    backend.discover_devices.assert_awaited_once()
    backend.set_device.assert_awaited_once_with("dev-123")


async def test_startup_reconnect_swallows_discover_exception():
    from app.state import _startup_reconnect
    import app.database as db
    backend = MagicMock()
    backend.discover_devices = AsyncMock(side_effect=Exception("mDNS timeout"))
    backend.set_device = AsyncMock()
    with patch.object(db, "get_setting", AsyncMock(return_value=None)):
        await _startup_reconnect(backend, "dev-123")  # must not raise
    backend.set_device.assert_awaited_once_with("dev-123")  # set_device still runs


async def test_startup_reconnect_swallows_set_device_exception():
    from app.state import _startup_reconnect
    import app.database as db
    backend = MagicMock()
    backend.discover_devices = AsyncMock()
    backend.set_device = AsyncMock(side_effect=Exception("device not found"))
    with patch.object(db, "get_setting", AsyncMock(return_value=None)):
        await _startup_reconnect(backend, "dev-123")  # must not raise


# ── device address cache (R3–R7) ─────────────────────────────────────────────

async def test_startup_reconnect_cached_address_skips_discover():
    """R3/R4: when a cached address exists, set_device is called directly without discover."""
    import json
    import app.database as db
    from app.state import _startup_reconnect
    from app.output.chromecast import ChromecastBackend

    backend = ChromecastBackend()
    backend.set_device = AsyncMock()
    backend.discover_devices = AsyncMock()

    cached = json.dumps({"name": "Living Room", "host": "192.168.1.10", "port": 8009})
    with patch.object(db, "get_setting", AsyncMock(return_value=cached)), \
         patch("app.database.set_setting", AsyncMock()):
        await _startup_reconnect(backend, "dev-uuid-1")

    backend.set_device.assert_awaited_once_with("dev-uuid-1")
    backend.discover_devices.assert_not_awaited()


async def test_startup_reconnect_cached_address_populates_dbus_index():
    """R3: the cached address is loaded into _dbus_index so set_device can connect directly."""
    import json
    import app.database as db
    from app.state import _startup_reconnect
    from app.output.chromecast import ChromecastBackend

    backend = ChromecastBackend()
    backend.set_device = AsyncMock()

    cached = json.dumps({"name": "Kitchen Cast", "host": "10.0.0.5", "port": 8009})
    with patch.object(db, "get_setting", AsyncMock(return_value=cached)), \
         patch("app.database.set_setting", AsyncMock()):
        await _startup_reconnect(backend, "some-uuid")

    assert backend._dbus_index["some-uuid"] == ("Kitchen Cast", "10.0.0.5", 8009)


async def test_startup_reconnect_cached_address_airplay_populates_device_addr():
    """R3: for AirPlay the cached address populates the device-address cache
    that the subprocess backend's set_device() consults. The 4-tuple shape
    (name, host, port, txt) mirrors the discovery cache; an empty TXT dict
    on the cached path surfaces a re-pair event from the speaker rather than
    silently using stale TXT."""
    import json
    import app.database as db
    from app.state import _startup_reconnect
    from app.output.airplay import AirPlayBackend

    backend = AirPlayBackend()
    backend.set_device = AsyncMock()

    cached = json.dumps({"name": "Bedroom Speaker", "host": "192.168.1.20", "port": 7000})
    with patch.object(db, "get_setting", AsyncMock(return_value=cached)), \
         patch("app.database.set_setting", AsyncMock()):
        await _startup_reconnect(backend, "ap-host:7000")

    assert backend._device_addr["ap-host:7000"] == (
        "Bedroom Speaker", "192.168.1.20", 7000, {}
    )


async def test_startup_reconnect_cached_address_fails_falls_through_to_discover():
    """R6: if set_device fails on the cached address, discover_devices is called next."""
    import json
    import app.database as db
    from app.state import _startup_reconnect
    from app.output.chromecast import ChromecastBackend

    backend = ChromecastBackend()
    call_order = []
    backend.set_device = AsyncMock(side_effect=[Exception("stale"), None])
    backend.discover_devices = AsyncMock(side_effect=lambda: call_order.append("discover"))

    cached = json.dumps({"name": "TV", "host": "192.168.1.99", "port": 8009})
    with patch.object(db, "get_setting", AsyncMock(return_value=cached)), \
         patch("app.database.set_setting", AsyncMock()):
        await _startup_reconnect(backend, "some-uuid")

    assert call_order == ["discover"], "discover_devices must be called after cached address fails"
    assert backend.set_device.await_count == 2


async def test_startup_reconnect_r7_specific_notification_on_total_failure():
    """R7: when cached + discover both fail, notification says rescan not generic error."""
    import json
    import app.database as db
    from app.state import _startup_reconnect
    from app.output.chromecast import ChromecastBackend

    backend = ChromecastBackend()
    backend.set_device = AsyncMock(side_effect=Exception("not found"))
    backend.discover_devices = AsyncMock()

    cached = json.dumps({"name": "TV", "host": "10.0.0.1", "port": 8009})
    broadcast_events = []

    async def fake_broadcast(event):
        broadcast_events.append(event)

    with patch.object(db, "get_setting", AsyncMock(return_value=cached)), \
         patch("app.database.set_setting", AsyncMock()):
        from app.events import bus as _bus
        with patch.object(_bus.manager, "broadcast_to_admins", fake_broadcast):
            await _startup_reconnect(backend, "some-uuid")

    assert broadcast_events, "an admin notification must be sent"
    evt = broadcast_events[0]
    assert "rescan" in evt.device_name.lower()
    assert evt.backend_type == "error"


async def test_startup_reconnect_no_cache_does_not_set_dbus_index():
    """R5: without a cached address, _dbus_index is not modified before discover."""
    import app.database as db
    from app.state import _startup_reconnect
    from app.output.chromecast import ChromecastBackend

    backend = ChromecastBackend()
    backend.set_device = AsyncMock()
    backend.discover_devices = AsyncMock()

    with patch.object(db, "get_setting", AsyncMock(return_value=None)):
        await _startup_reconnect(backend, "some-uuid")

    assert "some-uuid" not in backend._dbus_index


# ── advance lock (#9 skip/EOS race) ──────────────────────────────────────────

async def test_do_advance_bails_when_advance_lock_held():
    """_do_advance must no-op if _advance_lock is already held (e.g. by skip)."""
    import app.state as st
    advance_ran = []

    async def fake_advance():
        advance_ran.append(True)
        return None

    with patch.object(st.queue_engine, "advance", side_effect=fake_advance):
        async with st._advance_lock:
            # Lock held — _do_advance must bail immediately
            await st._do_advance()

    assert advance_ran == [], "_do_advance should not run queue_engine.advance while lock is held"


async def test_do_advance_runs_when_lock_free():
    """_do_advance proceeds normally when _advance_lock is not held."""
    import app.state as st

    with patch.object(st.queue_engine, "advance", AsyncMock(return_value=None)), \
         patch.object(st, "get_plex_client", AsyncMock(return_value=None)):
        # Should not raise; returns immediately because advance() returns None
        await st._do_advance()


# ── shuffle provider wiring (#8) ──────────────────────────────────────────────

async def test_auto_fill_provider_is_wired_after_setup():
    """queue_engine._auto_fill_provider must be set by setup()."""
    import app.state as st
    import app.database as db

    # Reset the provider so we can test setup() wires it
    original = st.queue_engine._auto_fill_provider
    st.queue_engine._auto_fill_provider = None

    try:
        with patch.object(db, "get_setting", AsyncMock(return_value=None)), \
             patch.object(db, "load_queue", AsyncMock(return_value=[])), \
             patch.object(db, "load_history", AsyncMock(return_value=[])), \
             patch.object(db, "get_enabled_libraries", AsyncMock(return_value=[])):
            await st.setup()
        assert st.queue_engine._auto_fill_provider is not None, \
            "setup() must wire _auto_fill_provider on queue_engine"
    finally:
        st.queue_engine._auto_fill_provider = original


# ── mDNS status flags (U2) ───────────────────────────────────────────────────

def _mock_zeroconf_modules(side_effect=None):
    """Return a sys.modules patch dict with a fake zeroconf.asyncio module."""
    mock_az = MagicMock()
    mock_az.AsyncZeroconf = MagicMock(side_effect=side_effect) if side_effect else MagicMock()
    return {"zeroconf": MagicMock(), "zeroconf.asyncio": mock_az}


def _setup_stack(stack, db, zeroconf_side_effect=None):
    """Enter all patches needed to call setup() in tests."""
    stack.enter_context(patch.dict("sys.modules", _mock_zeroconf_modules(zeroconf_side_effect)))
    stack.enter_context(patch.object(db, "get_setting", AsyncMock(return_value=None)))
    stack.enter_context(patch.object(db, "load_queue", AsyncMock(return_value=[])))
    stack.enter_context(patch.object(db, "load_history", AsyncMock(return_value=[])))
    stack.enter_context(patch.object(db, "get_enabled_libraries", AsyncMock(return_value=[])))


async def test_mdns_port_unavailable_false_when_zeroconf_succeeds():
    """_mdns_port_unavailable stays False when AsyncZeroconf binds without error."""
    import app.state as st
    import app.database as db

    original = st._mdns_port_unavailable
    st._mdns_port_unavailable = False
    try:
        with contextlib.ExitStack() as stack:
            _setup_stack(stack, db)
            await st.setup()
        assert st._mdns_port_unavailable is False
    finally:
        st._mdns_port_unavailable = original


async def test_mdns_port_unavailable_set_on_eaddrinuse():
    """_mdns_port_unavailable becomes True when AsyncZeroconf raises EADDRINUSE."""
    import errno as _errno
    import app.state as st
    import app.database as db

    original = st._mdns_port_unavailable
    st._mdns_port_unavailable = False
    try:
        exc = OSError(_errno.EADDRINUSE, "Address already in use")
        exc.errno = _errno.EADDRINUSE
        with contextlib.ExitStack() as stack:
            _setup_stack(stack, db, zeroconf_side_effect=exc)
            await st.setup()
        assert st._mdns_port_unavailable is True
    finally:
        st._mdns_port_unavailable = original


async def test_mdns_port_unavailable_not_set_on_other_oserror():
    """_mdns_port_unavailable stays False for OSErrors other than EADDRINUSE."""
    import app.state as st
    import app.database as db

    original = st._mdns_port_unavailable
    st._mdns_port_unavailable = False
    try:
        exc = OSError(111, "Connection refused")
        exc.errno = 111
        with contextlib.ExitStack() as stack:
            _setup_stack(stack, db, zeroconf_side_effect=exc)
            await st.setup()
        assert st._mdns_port_unavailable is False
    finally:
        st._mdns_port_unavailable = original


# ── U5: shared AsyncZeroconf is the single in-process mDNS stack ──────────────

async def test_shared_aiozc_exposed_and_wired_to_cast_browser():
    """U5: setup() creates ONE shared AsyncZeroconf, exposes it module-level,
    and wires the same instance into the Chromecast browser. The AirPlay
    browser reads st.shared_aiozc directly through the watcher, so a single
    bind serves both."""
    import app.state as st
    import app.database as db

    original_flag = st._mdns_port_unavailable
    original_shared = st.shared_aiozc
    st._mdns_port_unavailable = False
    st.shared_aiozc = None
    try:
        with contextlib.ExitStack() as stack:
            _setup_stack(stack, db)
            await st.setup()
        assert st.shared_aiozc is not None
        # CastBrowser binds via the sync Zeroconf of the shared instance.
        assert st.chromecast_backend._shared_zconf is st.shared_aiozc.zeroconf
    finally:
        st._mdns_port_unavailable = original_flag
        st.shared_aiozc = original_shared


async def test_shared_aiozc_none_on_eaddrinuse_degraded():
    """U5: a failed 5353 bind leaves shared_aiozc None (the degraded state U6
    surfaces) — no crash, no D-Bus fallback wiring."""
    import errno as _errno
    import app.state as st
    import app.database as db

    original_flag = st._mdns_port_unavailable
    original_shared = st.shared_aiozc
    st._mdns_port_unavailable = False
    st.shared_aiozc = object()  # sentinel proves setup() overwrites it
    try:
        exc = OSError(_errno.EADDRINUSE, "Address already in use")
        exc.errno = _errno.EADDRINUSE
        with contextlib.ExitStack() as stack:
            _setup_stack(stack, db, zeroconf_side_effect=exc)
            await st.setup()
        assert st.shared_aiozc is None
        assert st._mdns_port_unavailable is True
    finally:
        st._mdns_port_unavailable = original_flag
        st.shared_aiozc = original_shared


# ── U5: activate_backend host persistence ────────────────────────────────────


async def test_activate_backend_writes_host_and_via_when_host_provided():
    """When `host` is passed, activate_backend persists output_host and
    device_via:{host} so GET /output/active can surface the persisted
    selection on the next page load — even after a server restart."""
    from app import state as st

    settings: dict[str, str] = {}

    async def fake_set(key, value):
        settings[key] = value

    with patch("app.state._get_backend", return_value=None), \
         patch("app.database.set_setting", side_effect=fake_set):
        await st.activate_backend(
            "chromecast", "uuid-1", host="192.168.1.50",
        )

    assert settings["output_backend_type"] == "chromecast"
    assert settings["output_device_id"] == "uuid-1"
    assert settings["output_host"] == "192.168.1.50"
    assert settings["device_via:192.168.1.50"] == "chromecast"


async def test_activate_backend_skips_host_writes_when_host_absent():
    """Backwards-compat: a legacy caller that doesn't pass `host` still
    works — output_host and device_via:* are not written, but the
    backend_type and device_id keys are."""
    from app import state as st

    settings: dict[str, str] = {}

    async def fake_set(key, value):
        settings[key] = value

    with patch("app.state._get_backend", return_value=None), \
         patch("app.database.set_setting", side_effect=fake_set):
        await st.activate_backend("direct", "default")

    assert settings["output_backend_type"] == "direct"
    assert settings["output_device_id"] == "default"
    assert "output_host" not in settings
    assert not any(k.startswith("device_via:") for k in settings)


async def test_activate_backend_via_preference_changes_per_device():
    """The same physical device can have its Via preference changed.
    Second activation overwrites the first under the same host key."""
    from app import state as st

    settings: dict[str, str] = {}

    async def fake_set(key, value):
        settings[key] = value

    with patch("app.state._get_backend", return_value=None), \
         patch("app.database.set_setting", side_effect=fake_set):
        await st.activate_backend("airplay", "id-1", host="192.168.1.50")
        assert settings["device_via:192.168.1.50"] == "airplay"
        await st.activate_backend("chromecast", "uuid-1", host="192.168.1.50")
        assert settings["device_via:192.168.1.50"] == "chromecast"


# ── credit cache refresh (2026-06-10 per-track credits plan U2) ──────────────

def _credit_track(artist, album_artist, album_id, album="Nuggets", **kw):
    from app.plex.models import Track
    return Track(id=kw.get("id", "t1"), title="Song", artist=artist, album=album,
                 duration_ms=1000, year=kw.get("year", 1972), thumb=kw.get("thumb"),
                 server_name="My Plex", album_artist=album_artist, album_id=album_id)


async def test_refresh_credit_cache_aggregates_act_release_rows():
    import app.state as state_mod
    import app.database as db
    tracks = [
        # Two tracks by the same act on the same album -> ONE row
        _credit_track("13th Floor Elevators", "Various Artists", "al-1"),
        _credit_track("13th Floor Elevators", "Various Artists", "al-1", id="t2"),
        # Same act, second album -> second row
        _credit_track("13th Floor Elevators", "Various Artists", "al-2", album="Texas Comp"),
        # Credit equals release artist (case-insensitive) -> excluded
        _credit_track("the beatles", "The Beatles", "al-3", album="Abbey Road"),
        # No album_id -> excluded
        _credit_track("The Seeds", "Various Artists", None),
    ]
    client = MagicMock()
    client.get_tracks = AsyncMock(return_value=tracks)
    with patch.object(state_mod, "get_plex_client", AsyncMock(return_value=client)), \
         patch.object(db, "get_enabled_libraries", AsyncMock(return_value=[{"section_key": "1"}])), \
         patch.object(db, "set_credit_cache", AsyncMock()) as set_mock:
        state_mod._credit_refresh_running = True
        await state_mod._refresh_credit_cache()
    assert state_mod._credit_refresh_running is False  # finally clears the flag
    rows = set_mock.await_args.args[0]
    keyed = {(r["name_lower"], r["album_id"]) for r in rows}
    assert keyed == {("13th floor elevators", "al-1"), ("13th floor elevators", "al-2")}
    by_album = {r["album_id"]: r for r in rows}
    assert by_album["al-1"]["album_artist"] == "Various Artists"
    assert by_album["al-1"]["album_title"] == "Nuggets"


async def test_refresh_credit_cache_all_libraries_failed_leaves_cache():
    import app.state as state_mod
    import app.database as db
    client = MagicMock()
    client.get_tracks = AsyncMock(side_effect=Exception("plex down"))
    with patch.object(state_mod, "get_plex_client", AsyncMock(return_value=client)), \
         patch.object(db, "get_enabled_libraries", AsyncMock(return_value=[{"section_key": "1"}, {"section_key": "2"}])), \
         patch.object(db, "set_credit_cache", AsyncMock()) as set_mock:
        state_mod._credit_refresh_running = True
        await state_mod._refresh_credit_cache()
    set_mock.assert_not_awaited()  # destructive replace must not run on a failed scan
    assert state_mod._credit_refresh_running is False


async def test_refresh_credit_cache_partial_failure_still_writes():
    import app.state as state_mod
    import app.database as db
    client = MagicMock()
    client.get_tracks = AsyncMock(side_effect=[
        Exception("lib 1 down"),
        [_credit_track("The Seeds", "Various Artists", "al-9")],
    ])
    with patch.object(state_mod, "get_plex_client", AsyncMock(return_value=client)), \
         patch.object(db, "get_enabled_libraries", AsyncMock(return_value=[{"section_key": "1"}, {"section_key": "2"}])), \
         patch.object(db, "set_credit_cache", AsyncMock()) as set_mock:
        state_mod._credit_refresh_running = True
        await state_mod._refresh_credit_cache()
    rows = set_mock.await_args.args[0]
    assert [r["name"] for r in rows] == ["The Seeds"]


async def test_trigger_credit_refresh_single_flight():
    import app.state as state_mod
    calls = []

    async def fake_refresh():
        calls.append(1)

    with patch.object(state_mod, "_refresh_credit_cache", fake_refresh):
        state_mod._credit_refresh_running = False
        state_mod.trigger_credit_refresh()
        state_mod.trigger_credit_refresh()  # no-op while flag is set
        await asyncio.sleep(0.05)
    # fake doesn't clear the flag (the real refresh does, in finally) — reset.
    state_mod._credit_refresh_running = False
    assert calls == [1]


# ── cache freshness gate (gentle-on-plex U2/U3) ───────────────────────────────

async def test_cache_is_fresh_true_for_recent_timestamp():
    from datetime import datetime, timezone
    from app import state
    recent = datetime.now(timezone.utc).isoformat()
    with patch("app.database.get_setting", AsyncMock(return_value=recent)):
        assert await state.cache_is_fresh("k", 3600) is True


async def test_cache_is_fresh_false_for_old_timestamp():
    from datetime import datetime, timezone, timedelta
    from app import state
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with patch("app.database.get_setting", AsyncMock(return_value=old)):
        assert await state.cache_is_fresh("k", 3600) is False


async def test_cache_is_fresh_false_for_missing_timestamp():
    from app import state
    with patch("app.database.get_setting", AsyncMock(return_value=None)):
        assert await state.cache_is_fresh("k", 3600) is False


async def test_cache_is_fresh_false_for_malformed_timestamp():
    from app import state
    with patch("app.database.get_setting", AsyncMock(return_value="not-a-date")):
        assert await state.cache_is_fresh("k", 3600) is False


async def test_refresh_genre_cache_stamps_computed_at_on_success():
    from app import state
    client = MagicMock()
    client.get_styles_with_counts = AsyncMock(return_value=[{"name": "Rock", "count": 3}])
    stamped = {}

    async def fake_set_setting(key, value):
        stamped[key] = value

    with patch("app.state.get_plex_client", AsyncMock(return_value=client)), \
         patch("app.database.get_enabled_libraries", AsyncMock(return_value=[{"section_key": "1"}])), \
         patch("app.database.set_genre_cache", AsyncMock()), \
         patch("app.database.set_setting", fake_set_setting):
        await state._refresh_genre_cache()
    assert "genre_cache_computed_at" in stamped


async def test_refresh_credit_cache_stamps_on_success():
    from app import state
    track = MagicMock()
    track.artist = "Guest Act"
    track.album_artist = "Main Artist"
    track.album_id = "a1"
    track.album = "Alb"
    track.thumb = None
    track.year = 2020
    track.server_name = None
    client = MagicMock()
    client.get_tracks = AsyncMock(return_value=[track])
    stamped = {}

    async def fake_set_setting(key, value):
        stamped[key] = value

    with patch("app.state.get_plex_client", AsyncMock(return_value=client)), \
         patch("app.database.get_enabled_libraries", AsyncMock(return_value=[{"section_key": "1"}])), \
         patch("app.database.set_credit_cache", AsyncMock()), \
         patch("app.database.set_setting", fake_set_setting):
        await state._refresh_credit_cache()
    assert "credit_cache_computed_at" in stamped


async def test_refresh_credit_cache_does_not_stamp_when_all_libs_fail():
    """A fully-failed scan keeps the cache untouched AND unstamped, so it stays
    stale and retries on the next read instead of being marked fresh."""
    from app import state
    client = MagicMock()
    client.get_tracks = AsyncMock(side_effect=RuntimeError("boom"))
    stamped = {}

    async def fake_set_setting(key, value):
        stamped[key] = value

    with patch("app.state.get_plex_client", AsyncMock(return_value=client)), \
         patch("app.database.get_enabled_libraries", AsyncMock(return_value=[{"section_key": "1"}])), \
         patch("app.database.set_credit_cache", AsyncMock()) as scc, \
         patch("app.database.set_setting", fake_set_setting):
        await state._refresh_credit_cache()
    scc.assert_not_called()
    assert "credit_cache_computed_at" not in stamped


async def test_cache_is_fresh_false_for_naive_timestamp():
    """A tz-naive stamp (legacy/hand-edited) degrades to not-fresh, never raises."""
    from app import state
    with patch("app.database.get_setting", AsyncMock(return_value="2026-06-14T12:00:00")):
        assert await state.cache_is_fresh("k", 3600) is False


# ── Random-pick length band in the shuffle floor (2026-06-20 plan U3) ─────────

from app.plex.models import Track as _Track


class _ShufLib:
    def __init__(self, key): self.key = key


class _ShufEnt:
    def __init__(self, id): self.id = id


def _shuf_track(tid, dur_ms):
    return _Track(id=tid, title=f"T{tid}", artist="A", album="Al",
                  duration_ms=dur_ms, genre="Rock", album_id="al1")


def _shuf_client(tracks_return):
    """Fake Plex client for the shuffle traversal. ``tracks_return`` is either a
    fixed track list (every get_tracks returns it) or a list-of-lists used as a
    per-call side_effect (attempt 1 returns the first, attempt 2 the second, …)."""
    c = MagicMock()
    c.get_libraries = AsyncMock(return_value=[_ShufLib("1")])
    c.get_artists = AsyncMock(return_value=[_ShufEnt("a1")])
    c.get_albums = AsyncMock(return_value=[_ShufEnt("al1")])
    if tracks_return and isinstance(tracks_return[0], list):
        c.get_tracks = AsyncMock(side_effect=tracks_return)
    else:
        c.get_tracks = AsyncMock(return_value=tracks_return)
    return c


@contextlib.contextmanager
def _shuf_ctx(client, bounds):
    import app.state as st
    import app.database as db
    with patch.object(st, "get_plex_client", AsyncMock(return_value=client)), \
         patch.object(db, "get_enabled_libraries",
                      AsyncMock(return_value=[{"section_key": "1"}])), \
         patch.object(db, "get_random_length_bounds", AsyncMock(return_value=bounds)):
        yield st


async def test_shuffle_provider_no_bounds_single_traversal():
    """No band → one unfiltered traversal: unchanged behavior and call pattern."""
    client = _shuf_client([_shuf_track("t1", 5000)])
    with _shuf_ctx(client, (None, None)) as st:
        track = await st._shuffle_provider()
    assert track is not None and track.id == "t1"
    assert client.get_artists.await_count == 1  # exactly one traversal


async def test_shuffle_provider_returns_in_band_track():
    """A band filters the album's tracks to the in-band one."""
    client = _shuf_client([[
        _shuf_track("short", 3000),
        _shuf_track("good", 200000),
        _shuf_track("long", 2_000_000),
    ]])
    with _shuf_ctx(client, (30000, 600000)) as st:
        track = await st._shuffle_provider()
    assert track is not None and track.id == "good"


async def test_shuffle_provider_retries_past_out_of_band_album():
    """An album with only out-of-band tracks (band traversal → empty) is retried;
    a later attempt finds an in-band track."""
    client = _shuf_client([
        [_shuf_track("toolong", 9_000_000)],   # attempt 1: filtered to empty
        [_shuf_track("good", 200000)],         # attempt 2: in-band hit
    ])
    with _shuf_ctx(client, (30000, 600000)) as st:
        track = await st._shuffle_provider()
    assert track is not None and track.id == "good"
    assert client.get_tracks.await_count == 2


async def test_shuffle_provider_last_resort_returns_out_of_band():
    """When no in-band track exists after the retry budget, the final unfiltered
    traversal returns a track anyway (never dead-end)."""
    import app.state as st
    client = _shuf_client([_shuf_track("only_long", 9_000_000)])
    with _shuf_ctx(client, (30000, 600000)):
        with patch.object(st, "_SHUFFLE_BAND_TRIES", 3):
            track = await st._shuffle_provider()
    assert track is not None and track.id == "only_long"
    # 3 band attempts (each filtered to empty) + 1 final unfiltered pass.
    assert client.get_tracks.await_count == 4


async def test_shuffle_provider_empty_library_returns_none_with_band():
    """An empty enabled library returns None — not a band failure."""
    import app.state as st
    client = _shuf_client([])
    client.get_libraries = AsyncMock(return_value=[])
    with _shuf_ctx(client, (30000, 600000)):
        with patch.object(st, "_SHUFFLE_BAND_TRIES", 3):
            track = await st._shuffle_provider()
    assert track is None


# ── Auto-fill provider (queue-end modes; 2026-06-21 plan U3) ─────────────────

@contextlib.contextmanager
def _auto_ctx(client, *, length_limit, bounds=(30000, 600000)):
    """Context for _auto_fill_provider: a floor client, the queue-end
    length-limit checkbox state, and the stored band (applied only when on)."""
    import app.state as st
    import app.database as db
    with patch.object(st, "get_plex_client", AsyncMock(return_value=client)), \
         patch.object(db, "get_enabled_libraries",
                      AsyncMock(return_value=[{"section_key": "1"}])), \
         patch.object(db, "get_random_length_bounds", AsyncMock(return_value=bounds)), \
         patch.object(db, "get_queue_end_length_limit",
                      AsyncMock(return_value=length_limit)):
        yield st


async def test_auto_fill_full_random_ignores_band_when_checkbox_off():
    """Full Random with the length-limit checkbox OFF plays any length, even
    when a band is stored (AE4)."""
    from app.queue.models import QueueEndBehavior
    client = _shuf_client([_shuf_track("only_long", 9_000_000)])
    with _auto_ctx(client, length_limit=False) as st:
        track = await st._auto_fill_provider(QueueEndBehavior.FULL_RANDOM)
    assert track is not None and track.id == "only_long"
    assert client.get_artists.await_count == 1  # single unfiltered traversal


async def test_auto_fill_full_random_applies_band_when_checkbox_on():
    """Full Random with the checkbox ON honors the band (AE5)."""
    from app.queue.models import QueueEndBehavior
    client = _shuf_client([[
        _shuf_track("short", 3000),
        _shuf_track("good", 200000),
    ]])
    with _auto_ctx(client, length_limit=True) as st:
        track = await st._auto_fill_provider(QueueEndBehavior.FULL_RANDOM)
    assert track is not None and track.id == "good"


async def test_auto_fill_popular_empty_pool_falls_back_to_full_random():
    """Popular Random with no qualifying popular track falls back to the
    whole-library floor (never dead-end; AE2)."""
    from app.queue.models import QueueEndBehavior
    import app.state as st
    client = _shuf_client([_shuf_track("floor", 200000)])
    with _auto_ctx(client, length_limit=False):
        with patch.object(st, "_popular_provider", AsyncMock(return_value=None)):
            track = await st._auto_fill_provider(QueueEndBehavior.POPULAR_RANDOM)
    assert track is not None and track.id == "floor"


# ── Popular Random provider (2026-06-21 plan U4) ─────────────────────────────

def _pop_rows(*pairs):
    """Build get_top_played_tracks-shaped rows from (track_id, count) pairs."""
    return [{"track_id": tid, "count": c, "metadata": None} for tid, c in pairs]


@contextlib.contextmanager
def _pop_ctx(*, rows, threshold, resolve):
    """Context for _popular_provider.

    rows: get_top_played_tracks return value.
    resolve: dict track_id -> Track | Exception (Exception → get_track raises;
    a missing key resolves to None)."""
    import app.state as st
    import app.database as db
    client = MagicMock()

    async def _get_track(tid):
        val = resolve.get(tid)
        if isinstance(val, Exception):
            raise val
        return val

    client.get_track = AsyncMock(side_effect=_get_track)
    with patch.object(st, "get_plex_client", AsyncMock(return_value=client)), \
         patch.object(db, "get_popular_random_threshold",
                      AsyncMock(return_value=threshold)), \
         patch.object(db, "get_top_played_tracks", AsyncMock(return_value=rows)):
        yield st, client


async def test_popular_provider_returns_qualifying_track():
    """AE1: a popular track meeting the threshold resolves and is returned;
    a below-threshold track is excluded."""
    rows = _pop_rows(("hit", 5), ("low", 1))
    resolve = {"hit": _shuf_track("hit", 200000)}
    with _pop_ctx(rows=rows, threshold=2, resolve=resolve) as (st, _client):
        track = await st._popular_provider((None, None))
    assert track is not None and track.id == "hit"


async def test_popular_provider_empty_pool_returns_none():
    """AE2: no track meets the threshold → None, without resolving anything."""
    rows = _pop_rows(("low", 1))
    with _pop_ctx(rows=rows, threshold=2, resolve={}) as (st, client):
        track = await st._popular_provider((None, None))
    assert track is None
    client.get_track.assert_not_awaited()


async def test_popular_provider_skips_unresolvable_ids():
    """An unresolvable popular id is skipped; a resolvable one is returned."""
    rows = _pop_rows(("gone", 9), ("good", 8))
    resolve = {"gone": RuntimeError("deleted"), "good": _shuf_track("good", 200000)}
    with _pop_ctx(rows=rows, threshold=2, resolve=resolve) as (st, _client):
        track = await st._popular_provider((None, None))
    assert track is not None and track.id == "good"


async def test_popular_provider_all_unresolvable_returns_none():
    """Every candidate unresolvable → None (caller falls back to Full Random)."""
    rows = _pop_rows(("a", 9), ("b", 8))
    resolve = {"a": RuntimeError("x"), "b": None}
    with _pop_ctx(rows=rows, threshold=2, resolve=resolve) as (st, _client):
        track = await st._popular_provider((None, None))
    assert track is None


async def test_popular_provider_queries_unbounded_pool():
    """The Popular Random pool is gated by the play-count threshold alone — it
    queries get_top_played_tracks with no display cap (limit=None), so a
    qualifying track outside the leaderboard's top N is still eligible."""
    from app import database as _db
    rows = _pop_rows(("hit", 5))
    resolve = {"hit": _shuf_track("hit", 200000)}
    with _pop_ctx(rows=rows, threshold=2, resolve=resolve) as (st, _client):
        await st._popular_provider((None, None))
        _db.get_top_played_tracks.assert_awaited_once_with(None)


async def test_popular_provider_applies_band():
    """With a band in effect, an out-of-band popular track is excluded and an
    in-band one is returned."""
    rows = _pop_rows(("toolong", 9), ("good", 8))
    resolve = {"toolong": _shuf_track("toolong", 9_000_000),
               "good": _shuf_track("good", 200000)}
    with _pop_ctx(rows=rows, threshold=2, resolve=resolve) as (st, _client):
        track = await st._popular_provider((30000, 600000))
    assert track is not None and track.id == "good"


async def test_surprise_floor_is_band_aware_end_to_end():
    """Integration: when Surprise Me's smart sources are all band-filtered out,
    resolve_surprise falls through to its DEFAULT floor — the real band-aware
    _shuffle_provider — so the press still honors the band end-to-end. The unit
    tests above mock the floor directly; this one omits shuffle_provider so the
    real floor backs the press, guarding the composition."""
    import app.state as st
    import app.database as db
    from app.queue.surprise import resolve_surprise, SOURCE_RANDOM

    band = (30000, 600000)

    # Smart-source client: its only sonic candidate is out-of-band, so
    # resolve_surprise's acceptable() filters it; similar/heuristic yield nothing.
    smart = MagicMock()
    smart.get_sonic_nearest = AsyncMock(return_value=[_shuf_track("toolong", 9_000_000)])
    smart.get_artist_similar_names = AsyncMock(return_value=[])
    smart.get_artists = AsyncMock(return_value=[])
    smart.get_albums = AsyncMock(return_value=[])
    smart.get_tracks = AsyncMock(return_value=[])

    # Floor client (returned by get_plex_client) holds an in-band track.
    floor_client = _shuf_client([_shuf_track("good", 200000)])

    class _Q:
        queue: list = []
        history: list = []
        class _S:
            current = None
        state = _S()
        def is_duplicate(self, tid):
            return False

    async def _no_excl():
        return []

    async def _one_lib():
        return [{"section_key": "1"}]

    seed = [{"track_id": "s1", "genre": "Rock", "artist": "Seed"}]
    with patch.object(st, "get_plex_client", AsyncMock(return_value=floor_client)), \
         patch.object(db, "get_enabled_libraries", AsyncMock(side_effect=_one_lib)), \
         patch.object(db, "get_random_length_bounds", AsyncMock(return_value=band)):
        track, source = await resolve_surprise(
            seed, "plex", client=smart, queue=_Q(), diversity="off",
            length_bounds=band, get_exclusions=_no_excl, get_enabled_libraries=_one_lib,
            # shuffle_provider OMITTED → defaults to the real app.state._shuffle_provider
        )
    assert source == SOURCE_RANDOM
    assert track is not None and track.id == "good"


# ── Random auto-fill pre-buffer / on-deck (2026-06-21 plan U1–U3) ────────────

@contextlib.contextmanager
def _ondeck_reset():
    """Save/restore the module-level on-deck state around a test."""
    import app.state as st
    saved = (st._ondeck, st._ondeck_warming, st._ondeck_gen)
    st._ondeck, st._ondeck_warming, st._ondeck_gen = None, False, 0
    try:
        yield st
    finally:
        st._ondeck, st._ondeck_warming, st._ondeck_gen = saved


async def test_auto_fill_serves_ondeck_without_selecting():
    """AE1: a buffered on-deck track is returned instantly; the slow synchronous
    selection is not called, and the slot is cleared."""
    from app.queue.models import QueueEndBehavior
    with _ondeck_reset() as st:
        st._ondeck = _shuf_track("ondeck", 200000)
        with patch.object(st, "_select_auto_fill_track", AsyncMock()) as sel:
            track = await st._auto_fill_provider(QueueEndBehavior.FULL_RANDOM)
        assert track is not None and track.id == "ondeck"
        sel.assert_not_awaited()
        assert st._ondeck is None


async def test_auto_fill_falls_back_to_selection_when_no_ondeck():
    """AE2: with no buffer, _auto_fill_provider runs the synchronous selection."""
    from app.queue.models import QueueEndBehavior
    picked = _shuf_track("selected", 200000)
    with _ondeck_reset() as st:
        with patch.object(st, "_select_auto_fill_track",
                          AsyncMock(return_value=picked)) as sel:
            track = await st._auto_fill_provider(QueueEndBehavior.FULL_RANDOM)
        assert track is not None and track.id == "selected"
        sel.assert_awaited_once()


async def test_auto_fill_consume_bumps_generation():
    """Consuming the on-deck slot bumps _ondeck_gen so an in-flight warm can't
    reinstall the consumed generation."""
    from app.queue.models import QueueEndBehavior
    with _ondeck_reset() as st:
        st._ondeck = _shuf_track("x", 200000)
        gen0 = st._ondeck_gen
        await st._auto_fill_provider(QueueEndBehavior.FULL_RANDOM)
        assert st._ondeck_gen == gen0 + 1


def _fake_engine(*, behavior, playing, queue):
    from types import SimpleNamespace
    return SimpleNamespace(end_behavior=behavior,
                           state=SimpleNamespace(is_playing=playing),
                           queue=queue)


async def test_invalidate_ondeck_clears_and_bumps_gen():
    with _ondeck_reset() as st:
        st._ondeck = _shuf_track("x", 200000)
        gen0 = st._ondeck_gen
        await st.invalidate_ondeck()
        assert st._ondeck is None
        assert st._ondeck_gen == gen0 + 1


async def test_warm_installs_ondeck_and_precaches_lyrics():
    """AE1/AE5: a successful warm installs the slot and warms its lyrics."""
    from app.queue.models import QueueEndBehavior
    picked = _shuf_track("warmed", 200000)
    with _ondeck_reset() as st:
        with patch.object(st, "_select_auto_fill_track", AsyncMock(return_value=picked)), \
             patch.object(st, "schedule_prefetch", MagicMock()) as pf:
            await st._warm_ondeck(QueueEndBehavior.FULL_RANDOM, st._ondeck_gen)
        assert st._ondeck is not None and st._ondeck.id == "warmed"
        pf.assert_called_once()
        assert pf.call_args.args[0][0].id == "warmed"   # the track passed to prefetch


async def test_warm_discards_result_on_generation_change():
    """The load-bearing race: a warm that finishes after an invalidation (gen
    bumped) must NOT install its stale pick."""
    from app.queue.models import QueueEndBehavior
    picked = _shuf_track("stale", 200000)
    with _ondeck_reset() as st:
        stale_gen = st._ondeck_gen
        st._ondeck_gen = stale_gen + 1   # an invalidation happened during the slow select
        with patch.object(st, "_select_auto_fill_track", AsyncMock(return_value=picked)), \
             patch.object(st, "schedule_prefetch", MagicMock()) as pf:
            await st._warm_ondeck(QueueEndBehavior.FULL_RANDOM, stale_gen)
        assert st._ondeck is None
        pf.assert_not_called()


async def test_warm_no_install_when_selection_empty():
    from app.queue.models import QueueEndBehavior
    with _ondeck_reset() as st:
        with patch.object(st, "_select_auto_fill_track", AsyncMock(return_value=None)), \
             patch.object(st, "schedule_prefetch", MagicMock()) as pf:
            await st._warm_ondeck(QueueEndBehavior.FULL_RANDOM, st._ondeck_gen)
        assert st._ondeck is None
        pf.assert_not_called()


async def test_trigger_warm_dispatches_when_conditions_met():
    from app.queue.models import QueueEndBehavior
    with _ondeck_reset() as st:
        fake = _fake_engine(behavior=QueueEndBehavior.FULL_RANDOM, playing=True, queue=[])
        with patch.object(st, "queue_engine", fake), \
             patch.object(st, "_warm_ondeck", AsyncMock()) as warm:
            st.trigger_ondeck_warm()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        warm.assert_awaited_once()


async def test_trigger_warm_noop_conditions():
    """R9 (Stop) + not-playing + non-empty queue each suppress the warm."""
    from app.queue.models import QueueEndBehavior
    cases = [
        _fake_engine(behavior=QueueEndBehavior.STOP, playing=True, queue=[]),
        _fake_engine(behavior=QueueEndBehavior.FULL_RANDOM, playing=False, queue=[]),
        _fake_engine(behavior=QueueEndBehavior.FULL_RANDOM, playing=True, queue=[1]),
    ]
    for fake in cases:
        with _ondeck_reset() as st:
            with patch.object(st, "queue_engine", fake), \
                 patch.object(st, "_warm_ondeck", AsyncMock()) as warm:
                st.trigger_ondeck_warm()
                await asyncio.sleep(0)
            warm.assert_not_awaited()


async def test_trigger_warm_single_flight():
    from app.queue.models import QueueEndBehavior
    with _ondeck_reset() as st:
        st._ondeck_warming = True   # a warm is already in flight
        fake = _fake_engine(behavior=QueueEndBehavior.FULL_RANDOM, playing=True, queue=[])
        with patch.object(st, "queue_engine", fake), \
             patch.object(st, "_warm_ondeck", AsyncMock()) as warm:
            st.trigger_ondeck_warm()
            await asyncio.sleep(0)
        warm.assert_not_awaited()


async def test_trigger_warm_noop_when_slot_already_set():
    from app.queue.models import QueueEndBehavior
    with _ondeck_reset() as st:
        st._ondeck = _shuf_track("already", 200000)
        fake = _fake_engine(behavior=QueueEndBehavior.FULL_RANDOM, playing=True, queue=[])
        with patch.object(st, "queue_engine", fake), \
             patch.object(st, "_warm_ondeck", AsyncMock()) as warm:
            st.trigger_ondeck_warm()
            await asyncio.sleep(0)
        warm.assert_not_awaited()


async def test_ondeck_react_now_playing_warms():
    """now_playing_changed → warm the next pick."""
    with _ondeck_reset() as st:
        with patch.object(st, "trigger_ondeck_warm", MagicMock()) as warm, \
             patch.object(st, "invalidate_ondeck", AsyncMock()) as inv:
            await st._ondeck_react("now_playing_changed")
        warm.assert_called_once()
        inv.assert_not_awaited()


async def test_ondeck_react_queue_nonempty_invalidates():
    """AE3: a user queue-add (non-empty queue) discards the buffered pick."""
    with _ondeck_reset() as st:
        fake = _fake_engine(behavior=None, playing=True, queue=[1])
        with patch.object(st, "queue_engine", fake), \
             patch.object(st, "trigger_ondeck_warm", MagicMock()) as warm, \
             patch.object(st, "invalidate_ondeck", AsyncMock()) as inv:
            await st._ondeck_react("queue_changed")
        inv.assert_awaited_once()
        warm.assert_not_called()


async def test_ondeck_react_queue_empty_warms():
    """AE3: once the tail is empty again, re-warm."""
    with _ondeck_reset() as st:
        fake = _fake_engine(behavior=None, playing=True, queue=[])
        with patch.object(st, "queue_engine", fake), \
             patch.object(st, "trigger_ondeck_warm", MagicMock()) as warm, \
             patch.object(st, "invalidate_ondeck", AsyncMock()) as inv:
            await st._ondeck_react("queue_changed")
        warm.assert_called_once()
        inv.assert_not_awaited()


# ── browse index refresh (2026-06-21 browse-index plan U2) ───────────────────

def _mk_artist(id, title, **kw):
    from app.plex.models import Artist
    return Artist(id=id, title=title, thumb=kw.get("thumb"),
                  release_count=kw.get("release_count"))


def _mk_album(id, title, artist, **kw):
    from app.plex.models import Album
    return Album(id=id, title=title, artist=artist, year=kw.get("year"),
                 thumb=kw.get("thumb"), subtype=kw.get("subtype"),
                 added_at=kw.get("added_at"), track_count=kw.get("track_count"))


def _mk_lib(key, server_name):
    from app.plex.models import Library
    return Library(key=key, title=f"Lib {key}", type="artist", server_name=server_name)


def test_build_browse_index_rows_happy():
    from app.state import _build_browse_index_rows
    libs_data = [
        ("ServerA", "A:1",
         [_mk_artist("A:1", "Radiohead", release_count=9)],
         [_mk_album("A:10", "Kid A", "Radiohead", year=2000)]),
    ]
    arts, albs = _build_browse_index_rows(libs_data)
    assert arts[0] == {
        "artist_id": "A:1", "title": "Radiohead", "base_key": "radiohead",
        "thumb": None, "release_count": 9, "server_name": "ServerA",
        "section_key": "A:1",
    }
    assert albs[0]["title_base"] == "kid a"
    assert albs[0]["artist_base_key"] == "radiohead"  # album.artist == release artist
    assert albs[0]["server_name"] == "ServerA"


def test_build_browse_index_rows_carries_added_at():
    """Recently Added plan U2: the builder copies Album.added_at into the album
    row dict (and tolerates None)."""
    from app.state import _build_browse_index_rows
    libs_data = [
        ("S", "S:1", [],
         [_mk_album("S:9", "Kid A", "Radiohead", added_at=1700000000),
          _mk_album("S:10", "No Date", "Radiohead")]),
    ]
    _, albs = _build_browse_index_rows(libs_data)
    by_id = {a["album_id"]: a for a in albs}
    assert by_id["S:9"]["added_at"] == 1700000000
    assert by_id["S:10"]["added_at"] is None


def test_build_browse_index_rows_carries_track_count():
    """Same-title plan U2: the builder copies Album.track_count into the album
    row dict (and tolerates None)."""
    from app.state import _build_browse_index_rows
    libs_data = [
        ("S", "S:1", [],
         [_mk_album("S:9", "Loveless", "My Bloody Valentine", track_count=11),
          _mk_album("S:10", "No Count", "X")]),
    ]
    _, albs = _build_browse_index_rows(libs_data)
    by_id = {a["album_id"]: a for a in albs}
    assert by_id["S:9"]["track_count"] == 11
    assert by_id["S:10"]["track_count"] is None


def test_build_browse_index_rows_artist_album_keys_align():
    """Drill-in correctness: an album's artist_base_key equals the artist row's
    base_key for the same act, so get_browse_albums_for_artist resolves it
    (whitespace + case folded identically on both sides)."""
    from app.state import _build_browse_index_rows
    libs_data = [
        ("S", "S:1", [_mk_artist("S:1", "  Sigur Rós ")],
         [_mk_album("S:9", "( )", "  Sigur Rós ")]),
    ]
    arts, albs = _build_browse_index_rows(libs_data)
    assert arts[0]["base_key"] == albs[0]["artist_base_key"]


async def _run_refresh(libs, artists_by_key, albums_by_key):
    """Drive _refresh_browse_index against a fake client; return (set_idx, stamp)."""
    import app.state as st
    client = MagicMock()
    client.get_libraries = AsyncMock(return_value=libs)

    async def _ga(key):
        v = artists_by_key[key]
        if isinstance(v, Exception):
            raise v
        return v

    async def _gal(key):
        v = albums_by_key[key]
        if isinstance(v, Exception):
            raise v
        return v

    client.get_artists = AsyncMock(side_effect=_ga)
    client.get_albums = AsyncMock(side_effect=_gal)
    set_idx = AsyncMock()
    stamp = AsyncMock()
    enabled = [{"section_key": l.key} for l in libs]
    with patch("app.state.get_plex_client", AsyncMock(return_value=client)), \
         patch("app.database.get_enabled_libraries", AsyncMock(return_value=enabled)), \
         patch("app.database.set_browse_index", set_idx), \
         patch("app.state.stamp_cache", stamp):
        await st._refresh_browse_index()
    return set_idx, stamp


async def test_refresh_browse_index_happy_bulk_crawl():
    # Covers AE3 (index populated from bulk per-section calls).
    libs = [_mk_lib("A:1", "ServerA"), _mk_lib("B:1", "ServerB")]
    set_idx, stamp = await _run_refresh(
        libs,
        {"A:1": [_mk_artist("A:1", "Radiohead")], "B:1": [_mk_artist("B:2", "Björk")]},
        {"A:1": [_mk_album("A:10", "Kid A", "Radiohead")],
         "B:1": [_mk_album("B:20", "Homogenic", "Björk")]},
    )
    set_idx.assert_awaited_once()
    artist_rows, album_rows = set_idx.await_args.args
    assert {r["artist_id"] for r in artist_rows} == {"A:1", "B:2"}
    assert {r["album_id"] for r in album_rows} == {"A:10", "B:20"}
    assert {r["server_name"] for r in album_rows} == {"ServerA", "ServerB"}
    stamp.assert_awaited_once_with("browse_index_computed_at")


async def test_refresh_browse_index_all_failed_does_not_wipe():
    err = RuntimeError("plex down")
    libs = [_mk_lib("A:1", "ServerA")]
    set_idx, stamp = await _run_refresh(libs, {"A:1": err}, {"A:1": err})
    set_idx.assert_not_awaited()   # good index left intact
    stamp.assert_not_awaited()


async def test_refresh_browse_index_all_albums_fail_does_not_install():
    # Review finding #1: asymmetric outage — artist lists return but every album
    # query fails. Must NOT install an artists-only index (which would stamp
    # fresh and make drill-ins show "no releases" with no live fallback for 6h).
    err = RuntimeError("album query timeout")
    libs = [_mk_lib("A:1", "ServerA"), _mk_lib("B:1", "ServerB")]
    set_idx, stamp = await _run_refresh(
        libs,
        {"A:1": [_mk_artist("A:1", "Radiohead")], "B:1": [_mk_artist("B:2", "Björk")]},
        {"A:1": err, "B:1": err},
    )
    set_idx.assert_not_awaited()
    stamp.assert_not_awaited()


async def test_refresh_browse_index_all_artists_fail_does_not_install():
    # The mirror case: every artist query fails but albums return. Also blocked.
    err = RuntimeError("artist query timeout")
    libs = [_mk_lib("A:1", "ServerA")]
    set_idx, stamp = await _run_refresh(
        libs, {"A:1": err}, {"A:1": [_mk_album("A:10", "Kid A", "Radiohead")]}
    )
    set_idx.assert_not_awaited()
    stamp.assert_not_awaited()


async def test_refresh_browse_index_partial_failure_indexes_good_sections():
    err = RuntimeError("section B blip")
    libs = [_mk_lib("A:1", "ServerA"), _mk_lib("B:1", "ServerB")]
    set_idx, stamp = await _run_refresh(
        libs,
        {"A:1": [_mk_artist("A:1", "Radiohead")], "B:1": err},
        {"A:1": [_mk_album("A:10", "Kid A", "Radiohead")],
         "B:1": [_mk_album("B:20", "Post", "Björk")]},
    )
    set_idx.assert_awaited_once()
    artist_rows, album_rows = set_idx.await_args.args
    # B's artists failed (omitted) but B's albums succeeded (kept).
    assert {r["artist_id"] for r in artist_rows} == {"A:1"}
    assert {r["album_id"] for r in album_rows} == {"A:10", "B:20"}
    stamp.assert_awaited_once()


async def test_refresh_browse_index_no_enabled_libs_noops():
    import app.state as st
    client = MagicMock()
    client.get_libraries = AsyncMock(return_value=[_mk_lib("A:1", "ServerA")])
    client.get_artists = AsyncMock()
    client.get_albums = AsyncMock()
    set_idx = AsyncMock()
    with patch("app.state.get_plex_client", AsyncMock(return_value=client)), \
         patch("app.database.get_enabled_libraries", AsyncMock(return_value=[])), \
         patch("app.database.set_browse_index", set_idx), \
         patch("app.state.stamp_cache", AsyncMock()):
        await st._refresh_browse_index()
    set_idx.assert_not_awaited()
    client.get_artists.assert_not_awaited()


def test_trigger_browse_index_single_flight_noops_when_running():
    import app.state as st
    st._browse_index_refresh_running = True
    try:
        with patch("asyncio.create_task") as ct:
            st.trigger_browse_index_refresh()
        ct.assert_not_called()
    finally:
        st._browse_index_refresh_running = False


def test_trigger_browse_index_starts_when_idle():
    import app.state as st
    st._browse_index_refresh_running = False

    def _fake_ct(coro):
        coro.close()  # prevent 'coroutine never awaited' warning
        return MagicMock()

    with patch("asyncio.create_task", side_effect=_fake_ct):
        st.trigger_browse_index_refresh()
    assert st._browse_index_refresh_running is True
    st._browse_index_refresh_running = False


# ── artist grouping map (2026-06-22 rule-grouping plan U1) ───────────────────

def test_build_artist_grouping_merges_diacritic_siblings():
    from app.state import _build_artist_grouping
    from app.normalize import compile_rules, DEFAULT_PATTERN_RULES
    compiled = compile_rules(DEFAULT_PATTERN_RULES)
    rows = [
        {"title": "Beyoncé", "base_key": "beyoncé"},
        {"title": "Beyonce", "base_key": "beyonce"},
        {"title": "Radiohead", "base_key": "radiohead"},
    ]
    m = _build_artist_grouping(rows, compiled)
    # default é→e rule unions the two spellings under one rule-norm
    assert m["beyonce"] == {"beyoncé", "beyonce"}
    assert m["radiohead"] == {"radiohead"}


def test_build_artist_grouping_empty_rules_no_merge():
    from app.state import _build_artist_grouping
    rows = [{"title": "Beyoncé", "base_key": "beyoncé"},
            {"title": "Beyonce", "base_key": "beyonce"}]
    m = _build_artist_grouping(rows, [])
    # empty rules → normalize is plain lowercase; no diacritic folding, no merge
    assert m["beyoncé"] == {"beyoncé"}
    assert m["beyonce"] == {"beyonce"}


def test_rules_sig_stable_and_changes():
    from app.state import rules_sig
    from app.normalize import compile_rules
    a = compile_rules([["x", "y"]])
    b = compile_rules([["x", "y"]])
    c = compile_rules([["x", "z"]])
    assert rules_sig(a) == rules_sig(b)
    assert rules_sig(a) != rules_sig(c)


def test_get_artist_grouping_signature_guard():
    import app.state as st
    st._swap_artist_grouping((3, "rulesA"), {"a": {"a"}})
    try:
        assert st.get_artist_grouping((3, "rulesA")) == {"a": {"a"}}
        assert st.get_artist_grouping((4, "rulesA")) is None   # index gen differs
        assert st.get_artist_grouping((3, "rulesB")) is None   # rules differ
    finally:
        st._artist_grouping = None


def test_swap_artist_grouping_is_atomic_non_mutating():
    import app.state as st
    try:
        st._swap_artist_grouping((1, "r"), {"a": {"a"}})
        old = st.get_artist_grouping((1, "r"))
        st._swap_artist_grouping((2, "r"), {"b": {"b"}})
        assert old == {"a": {"a"}}                              # old ref untouched
        assert st.get_artist_grouping((2, "r")) == {"b": {"b"}}
    finally:
        st._artist_grouping = None


def test_browse_index_gen_reads_global():
    import app.state as st
    st._browse_index_gen = 7
    try:
        assert st.browse_index_gen() == 7
    finally:
        st._browse_index_gen = 0


# ── grouping rebuild hooks (2026-06-22 rule-grouping plan U2) ────────────────

async def test_refresh_browse_index_rebuilds_grouping_and_bumps_gen():
    import app.state as st
    from app.normalize import compile_rules
    st._browse_index_gen = 0
    st._artist_grouping = None
    libs = [_mk_lib("A:1", "ServerA")]
    client = MagicMock()
    client.get_libraries = AsyncMock(return_value=libs)
    client.get_artists = AsyncMock(return_value=[_mk_artist("A:1", "Radiohead")])
    client.get_albums = AsyncMock(return_value=[_mk_album("A:10", "Kid A", "Radiohead")])
    roster = [{"title": "Radiohead", "base_key": "radiohead"}]
    try:
        with patch("app.state.get_plex_client", AsyncMock(return_value=client)), \
             patch("app.database.get_enabled_libraries", AsyncMock(return_value=[{"section_key": "A:1"}])), \
             patch("app.database.set_browse_index", AsyncMock()), \
             patch("app.state.stamp_cache", AsyncMock()), \
             patch("app.database.get_browse_artists", AsyncMock(return_value=roster)), \
             patch("app.database.get_pattern_rules", AsyncMock(return_value=[])):
            await st._refresh_browse_index()
        assert st.browse_index_gen() == 1   # bumped on roster replace
        sig = (1, st.rules_sig(compile_rules([])))
        assert st.get_artist_grouping(sig) == {"radiohead": {"radiohead"}}
    finally:
        st._browse_index_gen = 0
        st._artist_grouping = None


async def test_rebuild_artist_grouping_uses_db_not_plex():
    import app.state as st
    from app.normalize import compile_rules
    st._browse_index_gen = 2
    st._artist_grouping = None
    roster = [{"title": "Björk", "base_key": "björk"}]
    try:
        with patch("app.database.get_browse_artists", AsyncMock(return_value=roster)) as ga, \
             patch("app.database.get_pattern_rules", AsyncMock(return_value=[])):
            await st._rebuild_artist_grouping()
        sig = (2, st.rules_sig(compile_rules([])))   # gen unchanged (rule-save path)
        assert st.get_artist_grouping(sig) == {"björk": {"björk"}}
        ga.assert_awaited_once()
    finally:
        st._browse_index_gen = 0
        st._artist_grouping = None


def test_trigger_artist_grouping_rebuild_single_flight():
    import app.state as st
    st._grouping_rebuild_running = True
    try:
        with patch("asyncio.create_task") as ct:
            st.trigger_artist_grouping_rebuild()
        ct.assert_not_called()
    finally:
        st._grouping_rebuild_running = False


# ── Closing Time mode (2026-06-24 plan U2) ───────────────────────────────────

def _ct_item(title="Closing Time", artist="Semisonic"):
    from app.queue.models import QueueItem
    from app.plex.models import Track
    return QueueItem(track=Track(id="ct", title=title, artist=artist,
                                 album="Feeling Strangely Fine", duration_ms=200_000))


@contextlib.contextmanager
def _closing_ctx(enabled, *, title="Closing Time", artist="Semisonic", message="msg"):
    """Patch the closing config + capture the broadcast (manager is imported
    locally in app.state, so patch the bus singleton — see line ~148)."""
    import app.database as db
    from app.events import bus as _bus
    bc = AsyncMock()
    with patch.object(db, "get_closing_time_config",
                      AsyncMock(return_value=(enabled, title, artist, message))), \
         patch.object(_bus.manager, "broadcast_to_all", bc):
        yield bc


async def test_closing_freezes_instead_of_advancing(monkeypatch):
    """Enabled + trigger just ended → close_out (freeze), no advance, banner broadcast, flag set."""
    import app.state as st
    monkeypatch.setattr(st, "_closing_active", False)
    monkeypatch.setattr(st.queue_engine, "_current", _ct_item())
    with _closing_ctx(True, message="Last call") as bc:
        with patch.object(st.queue_engine, "advance", AsyncMock()) as adv, \
             patch.object(st.queue_engine, "close_out", AsyncMock()) as co:
            await st._do_advance()
    adv.assert_not_awaited()
    co.assert_awaited_once()
    assert st._closing_active is True
    ev = bc.await_args.args[0]
    assert ev.type == "closing_time" and ev.active is True and ev.message == "Last call"


async def test_closing_non_match_advances_normally(monkeypatch):
    import app.state as st
    monkeypatch.setattr(st, "_closing_active", False)
    monkeypatch.setattr(st.queue_engine, "_current", _ct_item(title="Other Song"))
    with _closing_ctx(True) as bc:
        with patch.object(st.queue_engine, "advance", AsyncMock(return_value=None)) as adv, \
             patch.object(st.queue_engine, "close_out", AsyncMock()) as co, \
             patch.object(st, "get_plex_client", AsyncMock(return_value=None)):
            await st._do_advance()
    adv.assert_awaited_once()
    co.assert_not_awaited()
    assert st._closing_active is False
    bc.assert_not_awaited()


async def test_closing_disabled_advances_normally(monkeypatch):
    import app.state as st
    monkeypatch.setattr(st, "_closing_active", False)
    monkeypatch.setattr(st.queue_engine, "_current", _ct_item())  # title would match
    with _closing_ctx(False) as bc:
        with patch.object(st.queue_engine, "advance", AsyncMock(return_value=None)) as adv, \
             patch.object(st.queue_engine, "close_out", AsyncMock()) as co, \
             patch.object(st, "get_plex_client", AsyncMock(return_value=None)):
            await st._do_advance()
    adv.assert_awaited_once()
    co.assert_not_awaited()
    bc.assert_not_awaited()


async def test_closing_match_is_case_and_space_insensitive(monkeypatch):
    import app.state as st
    monkeypatch.setattr(st, "_closing_active", False)
    monkeypatch.setattr(st.queue_engine, "_current",
                        _ct_item(title="  closing   TIME ", artist="SEMISONIC"))
    with _closing_ctx(True, title="Closing Time", artist="Semisonic"):
        with patch.object(st.queue_engine, "advance", AsyncMock()) as adv, \
             patch.object(st.queue_engine, "close_out", AsyncMock()):
            await st._do_advance()
    adv.assert_not_awaited()
    assert st._closing_active is True


def test_should_auto_start_suppressed_while_closing(monkeypatch):
    """The idle auto-start gate must not fire while a Closing Time freeze is active."""
    import app.state as st
    from app.queue.models import QueueItem
    from app.plex.models import Track
    monkeypatch.setattr(st, "_auto_advance_pending", False)
    monkeypatch.setattr(st.queue_engine, "_current", None)
    monkeypatch.setattr(st.queue_engine, "_is_playing", False)
    monkeypatch.setattr(st.queue_engine, "_queue",
                        [QueueItem(track=Track(id="n", title="N", artist="A",
                                               album="Al", duration_ms=1000))])
    monkeypatch.setattr(st, "_closing_active", False)
    assert st._should_auto_start() is True
    monkeypatch.setattr(st, "_closing_active", True)
    assert st._should_auto_start() is False


async def test_clear_closing_resets_and_broadcasts(monkeypatch):
    """clear_closing drops the flag + message and tells every client to hide the banner."""
    import app.state as st
    from app.events import bus as _bus
    monkeypatch.setattr(st, "_closing_active", True)
    monkeypatch.setattr(st, "_closing_message", "Last call")
    bc = AsyncMock()
    with patch.object(_bus.manager, "broadcast_to_all", bc):
        await st.clear_closing()
    assert st._closing_active is False
    assert st._closing_message == ""
    ev = bc.await_args.args[0]
    assert ev.type == "closing_time" and ev.active is False


async def test_clear_closing_noop_when_inactive(monkeypatch):
    """No freeze active → clear_closing broadcasts nothing (no spurious banner churn)."""
    import app.state as st
    from app.events import bus as _bus
    monkeypatch.setattr(st, "_closing_active", False)
    bc = AsyncMock()
    with patch.object(_bus.manager, "broadcast_to_all", bc):
        await st.clear_closing()
    bc.assert_not_awaited()


# ── U13: source-neutral random floor + catalog-active predicate ───────────────

async def test_catalog_active_true_with_non_plex_source():
    import app.state as st
    reg = MagicMock()
    reg.sources = [MagicMock(source_type="plex"), MagicMock(source_type="jellyfin")]
    with patch.object(st, "get_plex_client", AsyncMock(return_value=reg)):
        assert await st.catalog_active() is True


async def test_catalog_active_false_for_plex_only_multiserver():
    # AE6: one PlexSource PER server, so an all-Plex install has >1 source but
    # stays native — gated on TYPE, not count.
    import app.state as st
    reg = MagicMock()
    reg.sources = [MagicMock(source_type="plex"), MagicMock(source_type="plex")]
    with patch.object(st, "get_plex_client", AsyncMock(return_value=reg)):
        assert await st.catalog_active() is False


async def test_catalog_active_false_for_none_or_nonregistry():
    import app.state as st
    with patch.object(st, "get_plex_client", AsyncMock(return_value=None)):
        assert await st.catalog_active() is False
    # A bare MagicMock's .sources is a Mock, not a list → native.
    with patch.object(st, "get_plex_client", AsyncMock(return_value=MagicMock())):
        assert await st.catalog_active() is False


async def _seed_shuffle_catalog(tmp_path, monkeypatch, tracks):
    import app.database as database
    from app.catalog import store
    from app.config import Settings
    s = Settings(data_dir=tmp_path, secret_key="test")
    monkeypatch.setattr(database, "settings", s)
    await database.init_db()
    holds = [{"entity_type": "track", "identity": t["identity"], "source_id": "jelly",
              "provider_local_key": f"jelly:{t['identity']}", "priority": 0,
              "server_name": "Jelly"} for t in tracks]
    await store.replace_catalog(artists=[], albums=[], tracks=tracks, holds=holds)


async def test_shuffle_provider_routes_to_catalog_when_active(tmp_path, monkeypatch):
    """The whole-library random floor draws from the catalog (carrying holds for
    play-time fallback) once a non-Plex source is connected — the Plex traversal
    can't see Jellyfin/local tracks (U13)."""
    import app.state as st
    import app.database as database
    await _seed_shuffle_catalog(tmp_path, monkeypatch, [
        {"identity": "t1", "title": "Only", "title_base": "only", "artist": "A",
         "artist_base_key": "a", "album": "Al", "album_identity": None, "duration_ms": 180000}])
    try:
        with patch.object(st, "catalog_active", AsyncMock(return_value=True)):
            track = await st._shuffle_provider()
        assert track is not None
        assert track.id == "t1"
        assert track.stream_key == "jelly:t1"
        # Carries the priority-ordered holds so _holder_keys / fallback works.
        assert [h["key"] for h in track.holds] == ["jelly:t1"]
    finally:
        await database.close_db()


async def test_catalog_shuffle_band_filters_then_never_dead_ends(tmp_path, monkeypatch):
    import app.state as st
    import app.database as database
    await _seed_shuffle_catalog(tmp_path, monkeypatch, [
        {"identity": "short", "title": "S", "title_base": "s", "artist": "A",
         "artist_base_key": "a", "album": "Al", "album_identity": None, "duration_ms": 60000},
        {"identity": "long", "title": "L", "title_base": "l", "artist": "A",
         "artist_base_key": "a", "album": "Al", "album_identity": None, "duration_ms": 600000}])
    try:
        # In-band [0,120s] → only the short track qualifies (deterministic).
        t = await st._catalog_shuffle(0, 120000)
        assert t.id == "short"
        # Nothing fits [700s, ∞) → never dead-end: an unfiltered pick still returns.
        t2 = await st._catalog_shuffle(700000, None)
        assert t2 is not None and t2.id in {"short", "long"}
    finally:
        await database.close_db()


async def test_catalog_shuffle_empty_catalog_returns_none(tmp_path, monkeypatch):
    import app.state as st
    import app.database as database
    await _seed_shuffle_catalog(tmp_path, monkeypatch, [])
    try:
        assert await st._catalog_shuffle(None, None) is None
    finally:
        await database.close_db()


# ── U15: scan-status snapshot (onboarding + scan states) ──────────────────────

async def _u15_status_db(tmp_path, monkeypatch, scanning):
    import app.state as st
    import app.database as database
    from app.config import Settings
    s = Settings(data_dir=tmp_path, secret_key="test")
    monkeypatch.setattr(database, "settings", s)
    monkeypatch.setattr(st, "_catalog_refresh_running", scanning)
    await database.init_db()
    return st, database


async def test_scan_status_zero_sources(tmp_path, monkeypatch):
    st, database = await _u15_status_db(tmp_path, monkeypatch, scanning=False)
    try:
        with patch.object(st, "get_plex_client", AsyncMock(return_value=None)):
            status = await st.scan_status()
        assert status == {"sources": 0, "scanning": False, "scanned": False, "empty": True}
    finally:
        await database.close_db()


async def test_scan_status_first_scan_building(tmp_path, monkeypatch):
    st, database = await _u15_status_db(tmp_path, monkeypatch, scanning=True)
    try:
        reg = MagicMock(); reg.sources = [MagicMock(source_type="jellyfin")]
        with patch.object(st, "get_plex_client", AsyncMock(return_value=reg)):
            status = await st.scan_status()
        # First scan: a source connected, crawl in flight, nothing stamped/stored.
        assert status == {"sources": 1, "scanning": True, "scanned": False, "empty": True}
    finally:
        await database.close_db()


async def test_scan_status_scanned_with_content(tmp_path, monkeypatch):
    st, database = await _u15_status_db(tmp_path, monkeypatch, scanning=False)
    from app.catalog import store
    try:
        await store.replace_catalog(
            artists=[], albums=[],
            tracks=[{"identity": "t1", "title": "X", "title_base": "x", "artist": "A",
                     "artist_base_key": "a", "album": "Al", "album_identity": None,
                     "duration_ms": 1000}],
            holds=[])
        await database.set_setting("catalog_computed_at", "2026-06-30T00:00:00+00:00")
        reg = MagicMock(); reg.sources = [MagicMock(source_type="jellyfin")]
        with patch.object(st, "get_plex_client", AsyncMock(return_value=reg)):
            status = await st.scan_status()
        assert status == {"sources": 1, "scanning": False, "scanned": True, "empty": False}
    finally:
        await database.close_db()


async def test_scan_status_scanned_but_empty(tmp_path, monkeypatch):
    st, database = await _u15_status_db(tmp_path, monkeypatch, scanning=False)
    try:
        await database.set_setting("catalog_computed_at", "2026-06-30T00:00:00+00:00")
        reg = MagicMock(); reg.sources = [MagicMock(source_type="local")]
        with patch.object(st, "get_plex_client", AsyncMock(return_value=reg)):
            status = await st.scan_status()
        # Distinct from zero-source: a finished scan that found nothing.
        assert status == {"sources": 1, "scanning": False, "scanned": True, "empty": True}
    finally:
        await database.close_db()


# ── Play-data curation: unrecord_play / purge_play_track (plan U2) ────────────

async def _curation_db(tmp_path, monkeypatch):
    import app.database as database
    from app.config import Settings
    s = Settings(data_dir=tmp_path, secret_key="test")
    monkeypatch.setattr(database, "settings", s)
    await database.init_db()
    return database


async def test_unrecord_play_decrements_all_three_counts(tmp_path, monkeypatch):
    """AE1: removing one play rolls back the track, album, and artist counts."""
    import app.state as st
    database = await _curation_db(tmp_path, monkeypatch)
    try:
        for _ in range(5):
            await database.increment_play_count("track", "t1")
        for _ in range(8):
            await database.increment_play_count("album", "Broken")
        for _ in range(12):
            await database.increment_play_count("artist", "NIN")
        await st.unrecord_play("t1", "Broken", "NIN")
        assert await database.get_play_count("track", "t1") == 4
        assert await database.get_play_count("album", "Broken") == 7
        assert await database.get_play_count("artist", "NIN") == 11
    finally:
        await database.close_db()


async def test_unrecord_play_floors_at_zero(tmp_path, monkeypatch):
    """AE2: nothing seeded → no count goes negative, call still succeeds."""
    import app.state as st
    database = await _curation_db(tmp_path, monkeypatch)
    try:
        await st.unrecord_play("t1", "Broken", "NIN")
        assert await database.get_play_count("track", "t1") == 0
        assert await database.get_play_count("album", "Broken") == 0
        assert await database.get_play_count("artist", "NIN") == 0
    finally:
        await database.close_db()


async def test_unrecord_play_skips_empty_album_artist(tmp_path, monkeypatch):
    """A track with an empty album/artist never created an ("album","") row, so
    the inverse must not touch one (mirrors record_play's truthiness guards)."""
    import app.state as st
    database = await _curation_db(tmp_path, monkeypatch)
    try:
        await database.increment_play_count("track", "t1")
        await st.unrecord_play("t1", "", None)
        assert await database.get_play_count("track", "t1") == 0
        assert await database.get_all_play_counts("album") == []
        assert await database.get_all_play_counts("artist") == []
    finally:
        await database.close_db()


async def test_unrecord_play_consolidates_before_decrement(tmp_path, monkeypatch):
    """Identity row = 0 but a stale sibling holds the real 5 → consolidate to 5,
    then decrement to 4 (not 0). Prevents the over-correction the naive
    decrement-then-delete would cause."""
    import app.state as st
    from app.catalog import store
    database = await _curation_db(tmp_path, monkeypatch)
    try:
        await store.register_alias("track", "I", "I")   # identity self-alias
        await store.register_alias("track", "S", "I")   # stale sibling → identity
        await database.set_play_count("track", "S", 5)   # real total on the stale key
        await st.unrecord_play("I", None, None)
        assert await database.get_play_count("track", "I") == 4
        assert await database.get_play_count("track", "S") == 0   # sibling swept
    finally:
        await database.close_db()


async def test_purge_play_track_is_track_scoped(tmp_path, monkeypatch):
    """AE4: removing a track from Most Played deletes its count + meta and leaves
    the shared album/artist name-keyed counts untouched."""
    import app.state as st
    database = await _curation_db(tmp_path, monkeypatch)
    try:
        await database.increment_play_count("track", "t1")
        await database.set_play_track_meta("t1", {"title": "X", "artist": "A"})
        await database.increment_play_count("album", "Broken")
        await database.increment_play_count("artist", "NIN")
        await st.purge_play_track("t1")
        assert await database.get_play_count("track", "t1") == 0
        assert await database.get_all_play_track_meta() == {}
        assert await database.get_play_count("album", "Broken") == 1
        assert await database.get_play_count("artist", "NIN") == 1
    finally:
        await database.close_db()


async def test_purge_play_track_survives_rescan_reconcile(tmp_path, monkeypatch):
    """AE3 durability: seed a live identity + a stale sibling (real durable state,
    not a fresh DB), purge, then a follow-up reconcile does NOT restore the count
    and the track is off the leaderboard."""
    import app.state as st
    from app.catalog import store, migrate
    database = await _curation_db(tmp_path, monkeypatch)
    try:
        await store.replace_catalog(artists=[], albums=[], tracks=[
            {"identity": "I", "title": "Wish", "title_base": "wish",
             "artist": "NIN", "artist_base_key": "nin"}], holds=[])
        await store.register_alias("track", "I", "I")
        await store.register_alias("track", "S", "I")
        await database.set_play_count("track", "I", 13)
        await database.set_play_count("track", "S", 13)
        await database.set_play_track_meta("I", {"title": "Wish"})
        await database.set_play_track_meta("S", {"title": "Wish"})
        await st.purge_play_track("I")
        assert await database.get_play_count("track", "I") == 0
        assert await database.get_play_count("track", "S") == 0
        await migrate.migrate_metadata()   # a subsequent Rescan
        assert await database.get_play_count("track", "I") == 0
        assert [r for r in await database.get_top_played_tracks(None)
                if r["track_id"] in ("I", "S")] == []
    finally:
        await database.close_db()
