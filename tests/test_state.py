"""Tests for app.state startup reconnect behavior and advance-lock guard."""

import asyncio
import contextlib

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.output import hold  # the hold flag's home since the session decomposition


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


async def test_startup_reconnect_cached_location_dlna_populates_device_locations():
    """Supervisor plan U3: DLNA joins the cached-address reconnect — the
    persisted output_addr carries the description LOCATION URL, seeded into
    _device_locations so set_device can attach with no discovery round."""
    import json
    import app.database as db
    from app.state import _startup_reconnect
    from app.output.dlna import DlnaBackend

    backend = DlnaBackend()
    backend.set_device = AsyncMock()
    backend.discover_devices = AsyncMock()

    cached = json.dumps({"location": "http://192.168.1.60:49152/desc.xml"})
    usn = "uuid:wiim-1::urn:schemas-upnp-org:device:MediaRenderer:1"
    with patch.object(db, "get_setting", AsyncMock(return_value=cached)), \
         patch("app.database.set_setting", AsyncMock()):
        await _startup_reconnect(backend, usn)

    assert backend._device_locations[usn] == "http://192.168.1.60:49152/desc.xml"
    backend.set_device.assert_awaited_once_with(usn)
    backend.discover_devices.assert_not_awaited()


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


# ── Gapless toggle live flag + arming generation (2026-07-11 plan U5) ─────────

@contextlib.contextmanager
def _gapless_reset():
    """Save/restore the module-level gapless flag + arming generation around a
    test (mirrors _ondeck_reset above)."""
    import app.state as st
    saved = (st._gapless_enabled, st._arming_gen)
    st._gapless_enabled, st._arming_gen = False, 0
    try:
        yield st
    finally:
        st._gapless_enabled, st._arming_gen = saved


def test_gapless_disabled_by_default():
    """Plan U5 (R10): the live flag defaults OFF — with the toggle off no
    playback path may behave differently, and the accessor is the only
    surface U6+ consult."""
    with _gapless_reset() as st:
        assert st.gapless_enabled() is False


def test_set_gapless_enabled_flips_flag_and_bumps_arming_gen():
    """A real flip live-applies (accessor reflects it immediately, no restart)
    AND bumps the arming generation — the U6 hook: an armed device-side next
    keyed to the old generation reads as stale and must be revoked."""
    with _gapless_reset() as st:
        gen0 = st.arming_gen()
        st.set_gapless_enabled(True)
        assert st.gapless_enabled() is True
        assert st.arming_gen() == gen0 + 1
        st.set_gapless_enabled(False)
        assert st.gapless_enabled() is False
        assert st.arming_gen() == gen0 + 2


def test_set_gapless_enabled_same_value_is_noop_for_arming_gen():
    """A same-value write is not a flip: no generation bump, so U6 never
    revokes/re-arms on a redundant settings save."""
    with _gapless_reset() as st:
        st.set_gapless_enabled(True)
        gen = st.arming_gen()
        st.set_gapless_enabled(True)
        assert st.arming_gen() == gen
        assert st.gapless_enabled() is True


# ── Effective-next prefetch + device-side arming (2026-07-11 plan U6) ─────────

def _arm_track(tid):
    from app.models import Track
    return Track(id=tid, title=f"T{tid}", artist="A", album="Al",
                 duration_ms=200000, stream_key=f"sk-{tid}")


class _FakeArmingBackend:
    """Gapless-capable fake for the U6 harness (U7/U8 implement the real
    ones): records the arm/revoke call sequence and mirrors the thread-safe-
    slot contract — arm_next stashes a single plain attribute a streaming
    thread could read without asyncio access."""

    def __init__(self):
        self.calls = []            # ("arm", url, track) / ("revoke",)
        self.armed = None          # the thread-safe slot: (url, track) | None
        self.is_playing = True

    async def arm_next(self, stream_url, track):
        self.calls.append(("arm", stream_url, track))
        self.armed = (stream_url, track)

    async def revoke_next(self):
        self.calls.append(("revoke",))
        self.armed = None

    def arm_count(self):
        return sum(1 for c in self.calls if c[0] == "arm")

    def revoke_count(self):
        return sum(1 for c in self.calls if c[0] == "revoke")


def _arm_engine(*, queue_tracks=(), current=None, playing=True, behavior=None):
    from types import SimpleNamespace
    from app.queue.models import QueueEndBehavior
    return SimpleNamespace(
        end_behavior=behavior or QueueEndBehavior.STOP,
        queue=[SimpleNamespace(track=t) for t in queue_tracks],
        state=SimpleNamespace(
            current=(SimpleNamespace(track=current)
                     if current is not None else None),
            is_playing=playing,
        ),
    )


@contextlib.contextmanager
def _arming_env(*, engine, backend=None, pending=False, gapless=True,
                closing=None):
    """The U6 test harness: reset the module arming/prefetch state, install a
    fake engine/router, and stub the warm (``url:<track.id>``) + the closing
    read. Yields ``(st, warm_mock)``; the router/closing stubs are reachable
    as ``st.output_router`` / ``st._closing_trigger_message``."""
    from types import SimpleNamespace
    import app.state as st
    saved = (st._armed_next_track, st._armed_next_url, st._armed_next_backend,
             st._armed_next_agen, st._next_warm_track, st._next_warm_url,
             st._arming_evaluating, st._arming_dirty,
             st._gapless_enabled, st._arming_gen)
    st._armed_next_track, st._armed_next_url = None, ""
    st._armed_next_backend, st._armed_next_agen = None, 0
    st._next_warm_track, st._next_warm_url = None, ""
    st._arming_evaluating, st._arming_dirty = False, False
    st._gapless_enabled, st._arming_gen = bool(gapless), 0
    warm = AsyncMock(side_effect=lambda t: f"url:{t.id}")
    router = SimpleNamespace(active=backend, has_pending=pending)
    try:
        with patch.object(st, "queue_engine", engine), \
             patch.object(st, "output_router", router), \
             patch.object(st, "_warm_next_transcode", warm), \
             patch.object(st, "_closing_trigger_message",
                          AsyncMock(return_value=closing)):
            yield st, warm
    finally:
        (st._armed_next_track, st._armed_next_url, st._armed_next_backend,
         st._armed_next_agen, st._next_warm_track, st._next_warm_url,
         st._arming_evaluating, st._arming_dirty,
         st._gapless_enabled, st._arming_gen) = saved


async def test_track_start_warms_and_arms_once():
    """A track starts → the effective next is warmed and (toggle on, capable
    backend) armed exactly once: the advance's queue_changed +
    now_playing_changed double event must not double-warm or double-arm."""
    t_next = _arm_track("next")
    be = _FakeArmingBackend()
    eng = _arm_engine(queue_tracks=[t_next], current=_arm_track("cur"))
    with _arming_env(engine=eng, backend=be) as (st, warm):
        await st._reconcile_armed_next()   # queue_changed
        await st._reconcile_armed_next()   # now_playing_changed
        assert warm.await_count == 1
        assert be.calls == [("arm", "url:next", t_next)]
        assert st.armed_next() == (t_next, "url:next")
        assert be.armed == ("url:next", t_next)   # the boundary-readable slot


async def test_prefetch_warms_with_gapless_off_but_never_arms():
    """R13: the prefetch runs regardless of the toggle — cache-warm only. With
    gapless OFF nothing goes device-side (toggle-off playback stays
    byte-identical; the warm is the one deliberate, transparent addition)."""
    t_next = _arm_track("next")
    be = _FakeArmingBackend()
    eng = _arm_engine(queue_tracks=[t_next], current=_arm_track("cur"))
    with _arming_env(engine=eng, backend=be, gapless=False) as (st, warm):
        await st._reconcile_armed_next()
        assert warm.await_count == 1
        assert be.calls == []
        assert st.armed_next() is None


async def test_backend_without_arm_next_prefetches_but_never_arms():
    """Duck-typed gate (hasattr, the register_resolved shape): an incapable
    backend still gets the R13 prefetch; AbstractOutputBackend is untouched,
    so nothing is ever armed on it."""
    t_next = _arm_track("next")
    be = MagicMock(spec=[])   # exposes NO arm_next
    eng = _arm_engine(queue_tracks=[t_next], current=_arm_track("cur"))
    with _arming_env(engine=eng, backend=be) as (st, warm):
        await st._reconcile_armed_next()
        assert warm.await_count == 1
        assert st.armed_next() is None


async def test_removed_armed_next_revokes_and_rearms():
    """AE6: a guest removes the armed next → revoke, then re-arm with the new
    queue front — the boundary would play the correct track."""
    t1, t2 = _arm_track("t1"), _arm_track("t2")
    be = _FakeArmingBackend()
    eng = _arm_engine(queue_tracks=[t1, t2], current=_arm_track("cur"))
    with _arming_env(engine=eng, backend=be) as (st, warm):
        await st._reconcile_armed_next()
        assert be.calls == [("arm", "url:t1", t1)]
        del eng.queue[0]                    # guest removes the armed next
        await st._reconcile_armed_next()    # queue_changed
        assert be.calls[1:] == [("revoke",), ("arm", "url:t2", t2)]
        assert st.armed_next() == (t2, "url:t2")
        assert be.armed == ("url:t2", t2)   # the device slot holds the right track


async def test_tail_append_no_revoke_churn():
    """A tail append leaves queue[0] unchanged → pure no-op: arm_next called
    once ever, revoke_next never, and the memo spares even the re-warm."""
    from types import SimpleNamespace
    t1 = _arm_track("t1")
    be = _FakeArmingBackend()
    eng = _arm_engine(queue_tracks=[t1], current=_arm_track("cur"))
    with _arming_env(engine=eng, backend=be) as (st, warm):
        await st._reconcile_armed_next()
        eng.queue.append(SimpleNamespace(track=_arm_track("t9")))
        await st._reconcile_armed_next()    # queue_changed (tail append)
        eng.queue.append(SimpleNamespace(track=_arm_track("t10")))
        await st._reconcile_armed_next()
        assert be.arm_count() == 1
        assert be.revoke_count() == 0
        assert warm.await_count == 1


async def test_append_to_empty_queue_preempts_armed_autofill_pick():
    """Empty queue + random mode: the on-deck pick is the armed effective
    next; a guest append preempts it → revoke + re-arm with the guest's
    track (the on-deck react drops the pick on the same event)."""
    from types import SimpleNamespace
    from app.queue.models import QueueEndBehavior
    pick, guest = _arm_track("pick"), _arm_track("guest")
    be = _FakeArmingBackend()
    eng = _arm_engine(queue_tracks=[], current=_arm_track("cur"),
                      behavior=QueueEndBehavior.FULL_RANDOM)
    with _ondeck_reset():
        with _arming_env(engine=eng, backend=be) as (st, warm):
            st._ondeck = pick
            await st._reconcile_armed_next()
            assert be.calls == [("arm", "url:pick", pick)]
            # Guest append: the queue gains a front; _ondeck_react invalidates
            # the pick on the same queue_changed — mirror both effects.
            eng.queue.append(SimpleNamespace(track=guest))
            st._ondeck = None
            await st._reconcile_armed_next()
            assert be.calls[1:] == [("revoke",), ("arm", "url:guest", guest)]
            assert st.armed_next() == (guest, "url:guest")


async def test_toggle_flip_mid_track_revokes():
    """set_gapless_enabled(False) mid-track fires the reconcile itself (no
    waiting for the next queue event): arming_gen mismatch → revoke."""
    t1 = _arm_track("t1")
    be = _FakeArmingBackend()
    eng = _arm_engine(queue_tracks=[t1], current=_arm_track("cur"))
    with _arming_env(engine=eng, backend=be) as (st, warm):
        await st._reconcile_armed_next()
        assert be.arm_count() == 1
        st.set_gapless_enabled(False)   # bumps arming_gen + fires the trigger
        for _ in range(10):
            await asyncio.sleep(0)      # let the spawned eval loop drain
        assert be.revoke_count() == 1
        assert st.armed_next() is None


async def test_output_switch_pending_revokes_and_holds_off():
    """Flow Gap 9b: a pending backend switch revokes the armed next so the
    boundary returns to server control (the router's swap_pending owns the
    next track); nothing re-arms while the swap is pending."""
    t1 = _arm_track("t1")
    be = _FakeArmingBackend()
    eng = _arm_engine(queue_tracks=[t1], current=_arm_track("cur"))
    with _arming_env(engine=eng, backend=be) as (st, warm):
        await st._reconcile_armed_next()
        st.output_router.has_pending = True    # switch requested mid-track
        await st._reconcile_armed_next()       # the router-fired reconcile
        assert be.calls[-1] == ("revoke",)
        assert st.armed_next() is None
        await st._reconcile_armed_next()       # still pending → still nothing
        assert be.arm_count() == 1


async def test_backend_swap_revokes_on_owner_and_arms_on_new():
    """An immediate backend switch (nothing playing at the router) changes the
    active-backend arming input: the revoke goes to the backend that OWNS the
    arm; the re-arm lands on the new active backend."""
    t1 = _arm_track("t1")
    old, new = _FakeArmingBackend(), _FakeArmingBackend()
    eng = _arm_engine(queue_tracks=[t1], current=_arm_track("cur"))
    with _arming_env(engine=eng, backend=old) as (st, warm):
        await st._reconcile_armed_next()
        assert old.arm_count() == 1
        st.output_router.active = new
        await st._reconcile_armed_next()
        assert old.calls[-1] == ("revoke",)
        assert new.calls == [("arm", "url:t1", t1)]


async def test_router_set_backend_fires_arming_eval():
    """Both set_backend branches (immediate + deferred) call the state-level
    hook — the reconcile then reads has_pending / active as stale."""
    from app.output.router import OutputRouter
    import app.state as st
    r = OutputRouter()
    a = MagicMock(is_playing=True)
    b = MagicMock(is_playing=False)
    with patch.object(st, "trigger_arming_eval", MagicMock()) as trig:
        r.set_backend(a)     # immediate (no active yet)
        r.set_backend(b)     # deferred (active is playing)
    assert r.active is a and r.has_pending is True
    assert trig.call_count == 2


async def test_reconcile_gapless_off_nothing_armed_skips_closing_read():
    """Toggle-off cost bound (2026-07-12 review C11): with gapless OFF and
    nothing armed there is nothing to arm or revoke, so the reconcile skips
    the per-queue-event closing-config DB read entirely — while the R13
    prefetch warm still runs (cache-warm only, no dispatch behavior). With
    gapless on or a slot armed the read still gates want_next (the R21
    tests below pin that path)."""
    t_next = _arm_track("next")
    be = _FakeArmingBackend()
    eng = _arm_engine(queue_tracks=[t_next], current=_arm_track("cur"))
    with _arming_env(engine=eng, backend=be, gapless=False) as (st, warm):
        await st._reconcile_armed_next()    # a queue event's reconcile
        st._closing_trigger_message.assert_not_awaited()   # no config read
        assert warm.await_count == 1        # the R13 warm still ran
        assert be.calls == []
        assert st.armed_next() is None


async def test_closing_sendoff_playing_suppresses_arming():
    """R21 arm-time check: the current track is the configured send-off — the
    boundary freezes instead of advancing, so nothing is prefetched or armed
    past it."""
    t1 = _arm_track("t1")
    be = _FakeArmingBackend()
    eng = _arm_engine(queue_tracks=[t1], current=_arm_track("sendoff"))
    with _arming_env(engine=eng, backend=be, closing="last call!") as (st, warm):
        await st._reconcile_armed_next()
        assert be.calls == []
        assert warm.await_count == 0
        assert st.armed_next() is None


async def test_closing_trigger_becoming_current_revokes_armed_next():
    """A next armed while closing was quiet must not survive the current track
    BECOMING the send-off (config edit mid-track): the reconcile revokes."""
    t1 = _arm_track("t1")
    be = _FakeArmingBackend()
    eng = _arm_engine(queue_tracks=[t1], current=_arm_track("cur"))
    with _arming_env(engine=eng, backend=be) as (st, warm):
        await st._reconcile_armed_next()
        assert be.arm_count() == 1
        st._closing_trigger_message.return_value = "last call!"  # config edit
        await st._reconcile_armed_next()
        assert be.calls[-1] == ("revoke",)
        assert st.armed_next() is None


async def test_closing_config_edit_revokes_and_reevaluates():
    """Closing config edited with a next armed → revoke NOW (the armed
    decision was made under the old config), then a fresh evaluation is
    triggered — the settings POST calls notify_closing_config_changed."""
    t1 = _arm_track("t1")
    be = _FakeArmingBackend()
    eng = _arm_engine(queue_tracks=[t1], current=_arm_track("cur"))
    with _arming_env(engine=eng, backend=be) as (st, warm):
        await st._reconcile_armed_next()
        with patch.object(st, "trigger_arming_eval", MagicMock()) as trig:
            await st.notify_closing_config_changed()
        assert be.revoke_count() == 1
        assert st.armed_next() is None
        trig.assert_called_once()


async def test_warm_failure_no_arm_no_dead_end():
    """The warm fails (source down) → no arm, no exception: the boundary falls
    back to today's non-gapless advance. The failure is NOT memoized, so a
    recovered source warms and arms on the next event."""
    t1 = _arm_track("t1")
    be = _FakeArmingBackend()
    eng = _arm_engine(queue_tracks=[t1], current=_arm_track("cur"))
    with _arming_env(engine=eng, backend=be) as (st, warm):
        warm.side_effect = None
        warm.return_value = None            # source down
        await st._reconcile_armed_next()    # must not raise
        assert be.calls == []
        assert st.armed_next() is None
        assert st._next_warm_track is None  # failure not memoized
        warm.side_effect = lambda t: f"url:{t.id}"   # source recovers
        await st._reconcile_armed_next()
        assert be.calls == [("arm", "url:t1", t1)]


async def test_stale_warm_discarded_when_queue_moves_mid_warm():
    """Generation-guard contract (the _ondeck_gen shape): the queue changes
    while the warm awaits — the finishing warm discards its result and arms
    nothing; the re-triggered pass owns the new next."""
    from types import SimpleNamespace
    t1, t2 = _arm_track("t1"), _arm_track("t2")
    be = _FakeArmingBackend()
    eng = _arm_engine(queue_tracks=[t1], current=_arm_track("cur"))
    with _arming_env(engine=eng, backend=be) as (st, warm):
        async def _warm_and_mutate(track):
            eng.queue[0] = SimpleNamespace(track=t2)   # queue moved mid-warm
            return f"url:{track.id}"
        warm.side_effect = _warm_and_mutate
        await st._reconcile_armed_next()
        assert be.calls == []               # stale result discarded
        assert st.armed_next() is None
        warm.side_effect = lambda t: f"url:{t.id}"
        await st._reconcile_armed_next()    # the dirty re-loop's pass
        assert be.calls == [("arm", "url:t2", t2)]


async def test_stop_playback_revokes_armed_next():
    """Playback stopping (idle boundary) leaves nothing to arm past: the armed
    next is revoked rather than left device-visible."""
    t1 = _arm_track("t1")
    be = _FakeArmingBackend()
    eng = _arm_engine(queue_tracks=[t1], current=_arm_track("cur"))
    with _arming_env(engine=eng, backend=be) as (st, warm):
        await st._reconcile_armed_next()
        eng.state.current = None            # stopped
        eng.state.is_playing = False
        await st._reconcile_armed_next()
        assert be.calls[-1] == ("revoke",)
        assert st.armed_next() is None


async def test_remove_stranded_entries_revokes_armed_next():
    """2026-08-04-002 plan U7 (the U6 seam): a stranded-removal set that
    contains the ARMED next track revokes the arm slot on the owning
    backend BEFORE the entry drops — the device must never natively advance
    into a track the queue no longer holds."""
    from types import SimpleNamespace
    t1 = _arm_track("t1")
    be = _FakeArmingBackend()
    eng = _arm_engine(queue_tracks=[t1], current=_arm_track("cur"))
    eng.remove_entries = AsyncMock(return_value=1)
    with _arming_env(engine=eng, backend=be) as (st, warm):
        await st._reconcile_armed_next()
        assert be.calls == [("arm", "url:t1", t1)]
        stranded = [SimpleNamespace(track_id="t1", added_at=123.0)]
        with patch("app.output.hold._output_hold", False):
            removed = await st.remove_stranded_entries(stranded)
        assert removed == 1
        assert be.calls[-1] == ("revoke",)
        assert st.armed_next() is None
        eng.remove_entries.assert_awaited_once_with([("t1", 123.0)])


async def test_remove_stranded_entries_without_armed_track_no_revoke():
    """Removal sets that do NOT contain the armed next leave the arm slot
    untouched (no revoke churn on unrelated stranded removals)."""
    from types import SimpleNamespace
    t1 = _arm_track("t1")
    be = _FakeArmingBackend()
    eng = _arm_engine(queue_tracks=[t1], current=_arm_track("cur"))
    eng.remove_entries = AsyncMock(return_value=1)
    with _arming_env(engine=eng, backend=be) as (st, warm):
        await st._reconcile_armed_next()
        stranded = [SimpleNamespace(track_id="other", added_at=9.0)]
        with patch("app.output.hold._output_hold", False):
            await st.remove_stranded_entries(stranded)
        assert be.revoke_count() == 0
        assert st.armed_next() == (t1, "url:t1")


async def test_arm_next_raising_leaves_slot_empty():
    """A device that rejects the arm is a gapped boundary, not an error path:
    the slot stays empty and no exception propagates."""
    t1 = _arm_track("t1")
    be = _FakeArmingBackend()

    async def _boom(url, track):
        raise RuntimeError("device refused")

    be.arm_next = _boom
    eng = _arm_engine(queue_tracks=[t1], current=_arm_track("cur"))
    with _arming_env(engine=eng, backend=be) as (st, warm):
        await st._reconcile_armed_next()    # must not raise
        assert st.armed_next() is None


async def test_trigger_arming_eval_coalesces_and_reruns():
    """Single-flight with the dirty re-loop: a trigger landing mid-reconcile
    runs exactly one more pass instead of stacking tasks."""
    import app.state as st
    calls = []

    async def _rec():
        calls.append(1)
        if len(calls) == 1:
            st.trigger_arming_eval()   # lands mid-run → dirty, no second task

    saved = (st._arming_evaluating, st._arming_dirty)
    st._arming_evaluating, st._arming_dirty = False, False
    try:
        with patch.object(st, "_reconcile_armed_next", _rec):
            st.trigger_arming_eval()
            for _ in range(10):
                await asyncio.sleep(0)
        assert calls == [1, 1]
        assert st._arming_evaluating is False
    finally:
        st._arming_evaluating, st._arming_dirty = saved


async def test_effective_next_prefers_queue_front_over_ondeck():
    from app.queue.models import QueueEndBehavior
    q0 = _arm_track("q0")
    eng = _arm_engine(queue_tracks=[q0], playing=False,
                      behavior=QueueEndBehavior.FULL_RANDOM)
    with _ondeck_reset() as st:
        st._ondeck = _arm_track("pick")
        with patch.object(st, "queue_engine", eng):
            assert st.effective_next_track() is q0


async def test_effective_next_ondeck_only_in_random_modes():
    """Empty queue: a random mode serves the on-deck pick; STOP means the
    boundary stops — no effective next, nothing to warm or arm."""
    from app.queue.models import QueueEndBehavior
    pick = _arm_track("pick")
    with _ondeck_reset() as st:
        st._ondeck = pick
        rand = _arm_engine(behavior=QueueEndBehavior.POPULAR_RANDOM)
        stop = _arm_engine(behavior=QueueEndBehavior.STOP)
        with patch.object(st, "queue_engine", rand):
            assert st.effective_next_track() is pick
        with patch.object(st, "queue_engine", stop):
            assert st.effective_next_track() is None


async def test_warm_ondeck_install_triggers_arming_eval():
    """The on-deck pick landing IS a new effective next (empty queue, random
    mode) — the install re-triggers the arming reconcile."""
    from app.queue.models import QueueEndBehavior
    picked = _shuf_track("warmed", 200000)
    with _ondeck_reset() as st:
        with patch.object(st, "_select_auto_fill_track",
                          AsyncMock(return_value=picked)), \
             patch.object(st, "schedule_prefetch", MagicMock()), \
             patch.object(st, "trigger_arming_eval", MagicMock()) as trig:
            await st._warm_ondeck(QueueEndBehavior.FULL_RANDOM, st._ondeck_gen)
        assert st._ondeck is picked
        trig.assert_called_once()


async def test_warm_ondeck_stale_install_does_not_trigger_arming_eval():
    """The on-deck generation guard keeps its contract: a stale warm neither
    installs nor pokes the arming orchestrator."""
    from app.queue.models import QueueEndBehavior
    picked = _shuf_track("stale", 200000)
    with _ondeck_reset() as st:
        stale_gen = st._ondeck_gen
        st._ondeck_gen = stale_gen + 1
        with patch.object(st, "_select_auto_fill_track",
                          AsyncMock(return_value=picked)), \
             patch.object(st, "schedule_prefetch", MagicMock()), \
             patch.object(st, "trigger_arming_eval", MagicMock()) as trig:
            await st._warm_ondeck(QueueEndBehavior.FULL_RANDOM, stale_gen)
        assert st._ondeck is None
        trig.assert_not_called()


# ── _warm_next_transcode: URL resolution + warm fetch (plan U6, R13) ──────────

def _warm_client(target):
    c = MagicMock()
    c.stream_url = lambda k: f"plex:{k}"
    c.resolve_stream = MagicMock(return_value=target)
    return c


async def test_warm_next_transcode_prewarms_ogg_through_stream_cache():
    """An Ogg-family next is pre-warmed through the stream proxy's EXISTING
    single-flight cache (never a parallel transcode path), keyed exactly as
    the boundary's GET will be, with the provider's auth headers."""
    import app.state as st
    from app.api import stream as stream_api
    from app.sources.base import StreamTarget
    track = _arm_track("t1")   # primary holder key sk-t1
    client = _warm_client(StreamTarget(url="http://src/f.ogg",
                                       headers={"X-Auth": "y"}))
    with patch.object(st, "get_plex_client", AsyncMock(return_value=client)), \
         patch.object(stream_api, "_get_or_transcode", AsyncMock()) as got:
        url = await st._warm_next_transcode(track)
    assert url == st._make_stream_url("sk-t1", client)
    got.assert_awaited_once_with("sk-t1", "http://src/f.ogg", {"X-Auth": "y"})


async def test_warm_next_transcode_local_and_passthrough_skip_transcode():
    """Local files and non-Ogg URLs need no server-side warm: URL resolution
    alone succeeds, and the transcode cache is never touched."""
    import app.state as st
    from app.api import stream as stream_api
    from app.sources.base import StreamTarget
    track = _arm_track("t1")
    for target in (StreamTarget(path="/music/a.flac"),
                   StreamTarget(url="http://src/f.flac")):
        client = _warm_client(target)
        with patch.object(st, "get_plex_client",
                          AsyncMock(return_value=client)), \
             patch.object(stream_api, "_get_or_transcode", AsyncMock()) as got:
            url = await st._warm_next_transcode(track)
        assert url == st._make_stream_url("sk-t1", client)
        got.assert_not_awaited()


async def test_warm_next_transcode_failure_paths_return_none():
    """Every failure degrades to None (no arm, boundary falls back): no
    client, no holders, empty resolution, and a transcode that raises."""
    import app.state as st
    from app.api import stream as stream_api
    from app.sources.base import StreamTarget
    track = _arm_track("t1")
    # no client
    with patch.object(st, "get_plex_client", AsyncMock(return_value=None)):
        assert await st._warm_next_transcode(track) is None
    # no holders
    bare = _arm_track("t2")
    bare.stream_key = ""
    client = _warm_client(StreamTarget(url="http://src/f.ogg"))
    with patch.object(st, "get_plex_client", AsyncMock(return_value=client)):
        assert await st._warm_next_transcode(bare) is None
    # empty resolution (neither path nor url)
    client = _warm_client(StreamTarget(url=""))
    with patch.object(st, "get_plex_client", AsyncMock(return_value=client)):
        assert await st._warm_next_transcode(track) is None
    # transcode raises (source down) — swallowed, never propagates
    client = _warm_client(StreamTarget(url="http://src/f.ogg"))
    with patch.object(st, "get_plex_client", AsyncMock(return_value=client)), \
         patch.object(stream_api, "_get_or_transcode",
                      AsyncMock(side_effect=RuntimeError("down"))):
        assert await st._warm_next_transcode(track) is None


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


async def test_refresh_browse_index_no_enabled_libs_clears_index():
    # All-off (nothing effectively enabled — all libraries disabled or all sources
    # vetoed): CLEAR the browse index rather than keep a stale one, and never crawl.
    # Previously this no-op'd and left the stale index serving vetoed content — the
    # P0 the doc review caught (Libraries-panel U2).
    import app.state as st
    client = MagicMock()
    client.get_libraries = AsyncMock(return_value=[_mk_lib("A:1", "ServerA")])
    client.get_artists = AsyncMock()
    client.get_albums = AsyncMock()
    set_idx = AsyncMock()
    with patch("app.state.get_plex_client", AsyncMock(return_value=client)), \
         patch("app.database.get_effective_enabled_libraries", AsyncMock(return_value=[])), \
         patch("app.database.set_browse_index", set_idx), \
         patch("app.state._rebuild_artist_grouping", AsyncMock()), \
         patch("app.state.stamp_cache", AsyncMock()):
        await st._refresh_browse_index()
    set_idx.assert_awaited_once_with([], [])   # index cleared
    client.get_libraries.assert_not_awaited()  # cleared before hitting the network
    client.get_artists.assert_not_awaited()    # no crawl


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


# ── queue_changed frame ordering (review fix JFR-2) ──────────────────────────

async def test_queue_changed_frames_arrive_in_mutation_order(monkeypatch):
    """Review fix JFR-2 (verified red before the fix): the annotate await
    between snapshot and broadcast let two overlapping queue mutations
    invert frames — the OLDER snapshot's frame broadcast LAST, so clients
    (which paint straight from the push, never a refetch) rendered a stale
    queue until the next mutation. Snapshot must be taken INSIDE the
    serialization lock: frames arrive in mutation order and the final
    frame carries both tracks."""
    import app.state as st
    from app.events import bus as _bus
    from app.plex.models import Track
    from app.queue.engine import QueueEngine
    qe = QueueEngine()
    monkeypatch.setattr(st, "queue_engine", qe)
    frames = []

    async def capture(ev):
        frames.append([q.track_id for q in ev.queue])

    gate = asyncio.Event()
    calls = {"n": 0}

    async def gated_annotate(rows, tracks=None):
        calls["n"] += 1
        if calls["n"] == 1:
            await gate.wait()      # first frame's annotate parks
        return rows

    def _t(tid):
        return Track(id=tid, title=tid, artist="A", album="B",
                     duration_ms=1000)

    with patch("app.api.guest._annotate_plex_held", gated_annotate), \
         patch.object(_bus.manager, "broadcast_to_all", capture), \
         patch("app.database.save_queue", AsyncMock()):
        await qe.append(_t("a"), bypass_lock=True)
        first = asyncio.create_task(st._broadcast_queue_changed())
        for _ in range(5):
            await asyncio.sleep(0)             # first is parked mid-annotate
        await qe.append(_t("b"), bypass_lock=True)
        second = asyncio.create_task(st._broadcast_queue_changed())
        for _ in range(5):
            await asyncio.sleep(0)
        gate.set()
        await first
        await second
    assert frames == [["a"], ["a", "b"]], (
        "frames must arrive in mutation order — the last frame on the wire "
        f"is the newest queue (got {frames})")


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


# ── Dispatch reporting to the output-session supervisor (2026-07-11 U1) ───────
# _play_with_fallback no longer counts at dispatch: it reports each dispatch to
# the supervisor, and record_play fires only from the confirmed-start
# chokepoint (tests/test_output_session.py owns the supervisor's own behavior).

def _u1_track(tid="x"):
    from app.models import Track
    return Track(id=tid, title="Song", artist="Act", album="Rec",
                 duration_ms=1000, stream_key="primary")


def _u1_client():
    c = MagicMock()
    c.stream_url = lambda k: f"url:{k}"
    return c


async def test_play_with_fallback_counts_only_at_confirmed_start(fresh_supervisor):
    """The natural-advance entry point: dispatch must NOT count; confirmation
    counts exactly once with the dispatched track."""
    import app.state as state
    from app.queue.models import QueueItem
    sup, timers, rec = fresh_supervisor
    item = QueueItem(track=_u1_track())
    play = AsyncMock(return_value=None)
    with patch.object(state, "output_router", MagicMock(play=play)):
        ok = await state._play_with_fallback(item, _u1_client())
    assert ok is True
    rec.assert_not_called()                      # no count at dispatch
    assert sup.current_token() is not None       # dispatch was reported
    sup.on_playback_confirmed(sup.current_token())
    rec.assert_called_once()
    assert rec.call_args.args[0] is item.track


async def test_play_with_fallback_failed_holder_withdraws_its_dispatch(fresh_supervisor):
    """Holder 1 fails, holder 2 plays: the failed attempt's token is withdrawn
    (its timer cancelled, a late deadline no-ops) and only the successful
    attempt's confirmation counts."""
    import app.state as state
    from app.queue.models import QueueItem
    sup, timers, rec = fresh_supervisor
    outages = []
    sup.add_outage_listener(lambda *a: outages.append(a))
    item = QueueItem(track=_u1_track())
    item.track.holds = [{"source_id": "m1", "key": "k1"},
                        {"source_id": "j", "key": "k2"}]
    play = AsyncMock(side_effect=[Exception("404"), None])
    with patch.object(state, "output_router", MagicMock(play=play)):
        ok = await state._play_with_fallback(item, _u1_client())
    assert ok is True
    assert len(timers.timers) == 2               # one dispatch per holder attempt
    assert timers.timers[0].cancelled            # failed attempt withdrawn
    timers.timers[0].cb()                        # late deadline → staleness guard
    await asyncio.sleep(0)
    assert outages == []
    sup.on_playback_confirmed(sup.current_token())
    rec.assert_called_once()


async def test_play_with_fallback_all_fail_leaves_no_pending_dispatch(fresh_supervisor):
    """Every holder fails → the item is the caller's to skip (track-level);
    no dispatch may linger to age into an outage emission."""
    import app.state as state
    from app.queue.models import QueueItem
    sup, timers, rec = fresh_supervisor
    item = QueueItem(track=_u1_track())
    play = AsyncMock(side_effect=Exception("gone"))
    with patch.object(state, "output_router", MagicMock(play=play)):
        ok = await state._play_with_fallback(item, _u1_client())
    assert ok is False
    assert sup.current_token() is None
    rec.assert_not_called()


async def test_play_with_fallback_device_not_ready_withdraws_dispatch(fresh_supervisor):
    """DeviceNotReadyError re-raises (halt the advance) — and the dispatch is
    withdrawn on the way out."""
    import app.state as state
    from app.output.base import DeviceNotReadyError
    from app.queue.models import QueueItem
    sup, timers, rec = fresh_supervisor
    item = QueueItem(track=_u1_track())
    play = AsyncMock(side_effect=DeviceNotReadyError())
    with patch.object(state, "output_router", MagicMock(play=play)):
        with pytest.raises(DeviceNotReadyError):
            await state._play_with_fallback(item, _u1_client())
    assert sup.current_token() is None
    rec.assert_not_called()


async def test_play_with_fallback_forwards_play_recorded_mark(fresh_supervisor):
    """R19 groundwork: an item carrying the play_recorded mark (a held item a
    resume path replays) confirms WITHOUT counting."""
    import app.state as state
    from app.queue.models import QueueItem
    sup, timers, rec = fresh_supervisor
    item = QueueItem(track=_u1_track())
    item.play_recorded = True
    play = AsyncMock(return_value=None)
    with patch.object(state, "output_router", MagicMock(play=play)):
        ok = await state._play_with_fallback(item, _u1_client())
    assert ok is True
    sup.on_playback_confirmed(sup.current_token())
    rec.assert_not_called()


# ── U2: device-level hold routing + holder tie-breaker (supervisor plan U2) ──

def _u2_router(play, probe=None):
    """An output_router stand-in: .play as given; .active carries
    probe_liveness only when a probe is supplied (no MagicMock auto-attr —
    _output_probe is hasattr-guarded)."""
    router = MagicMock()
    router.play = play
    active = MagicMock(spec=[])            # no probe_liveness attribute
    if probe is not None:
        active = MagicMock(spec=["probe_liveness"])
        active.probe_liveness = probe
    router.active = active
    return router


def _u2_track(tid):
    from app.models import Track
    return Track(id=tid, title=f"Song {tid}", artist="Act", album="Rec",
                 duration_ms=1000, stream_key=f"key-{tid}")


async def test_play_with_fallback_holder_failure_unreachable_raises_device_lost(fresh_supervisor):
    """R15 tie-breaker: first holder throws a transport error and the probe
    says UNREACHABLE — DeviceLostError, and the second holder is never
    consumed (the failure is the device's, not the track's)."""
    import app.state as state
    from app.output.base import DeviceLostError
    from app.queue.models import QueueItem
    sup, timers, rec = fresh_supervisor
    item = QueueItem(track=_u1_track())
    item.track.holds = [{"source_id": "m1", "key": "k1"},
                        {"source_id": "j", "key": "k2"}]
    play = AsyncMock(side_effect=Exception("connection reset"))
    probe = AsyncMock(return_value=(False, None))
    with patch.object(state, "output_router", _u2_router(play, probe)):
        with pytest.raises(DeviceLostError):
            await state._play_with_fallback(item, _u1_client())
    assert play.await_count == 1               # no second holder consumed
    probe.assert_awaited_once()
    rec.assert_not_called()


async def test_play_with_fallback_holder_failure_reachable_tries_next_holder(fresh_supervisor):
    """Probe says REACHABLE — the failure is the holder's; fallback continues
    exactly as today."""
    import app.state as state
    from app.queue.models import QueueItem
    sup, timers, rec = fresh_supervisor
    item = QueueItem(track=_u1_track())
    item.track.holds = [{"source_id": "m1", "key": "k1"},
                        {"source_id": "j", "key": "k2"}]
    play = AsyncMock(side_effect=[Exception("404"), None])
    probe = AsyncMock(return_value=(True, "PLAYING"))
    with patch.object(state, "output_router", _u2_router(play, probe)):
        ok = await state._play_with_fallback(item, _u1_client())
    assert ok is True
    assert play.await_count == 2


async def test_play_with_fallback_no_probe_keeps_todays_fallback(fresh_supervisor):
    """A backend without probe_liveness yields no tie-break evidence — all
    holders are tried and the item is the caller's to skip (today's path)."""
    import app.state as state
    from app.queue.models import QueueItem
    sup, timers, rec = fresh_supervisor
    item = QueueItem(track=_u1_track())
    item.track.holds = [{"source_id": "m1", "key": "k1"},
                        {"source_id": "j", "key": "k2"}]
    play = AsyncMock(side_effect=Exception("404"))
    with patch.object(state, "output_router", _u2_router(play)):
        ok = await state._play_with_fallback(item, _u1_client())
    assert ok is False
    assert play.await_count == 2


async def test_play_with_fallback_broken_probe_fails_open(fresh_supervisor):
    """A probe that itself raises is no evidence — don't hold on it (liveness:
    a broken probe must not freeze playback)."""
    import app.state as state
    from app.queue.models import QueueItem
    sup, timers, rec = fresh_supervisor
    item = QueueItem(track=_u1_track())
    item.track.holds = [{"source_id": "m1", "key": "k1"},
                        {"source_id": "j", "key": "k2"}]
    play = AsyncMock(side_effect=[Exception("404"), None])
    probe = AsyncMock(side_effect=RuntimeError("probe blew up"))
    with patch.object(state, "output_router", _u2_router(play, probe)):
        ok = await state._play_with_fallback(item, _u1_client())
    assert ok is True
    assert play.await_count == 2


async def test_play_with_fallback_probes_at_most_once_per_invocation(fresh_supervisor):
    """F9: N failing holders must not stack N reachability probes (each can
    block ~5s under _advance_lock — DLNA's SOAP timeout) — the first verdict
    is cached and reused; the fallback still consumes every holder under it."""
    import app.state as state
    from app.queue.models import QueueItem
    sup, timers, rec = fresh_supervisor
    item = QueueItem(track=_u1_track())
    item.track.holds = [{"source_id": "m1", "key": "k1"},
                        {"source_id": "j", "key": "k2"},
                        {"source_id": "l", "key": "k3"}]
    play = AsyncMock(side_effect=Exception("404"))
    probe = AsyncMock(return_value=(True, "PLAYING"))
    with patch.object(state, "output_router", _u2_router(play, probe)):
        ok = await state._play_with_fallback(item, _u1_client())
    assert ok is False
    assert play.await_count == 3                 # every holder still tried
    probe.assert_awaited_once()                  # ONE probe, verdict reused


async def test_play_with_fallback_consumes_play_recorded_mark(fresh_supervisor):
    """The R19 mark protects one pending play: a successful dispatch consumes
    it (the supervisor's dispatch captured it first), so a later organic
    replay counts again."""
    import app.state as state
    from app.queue.models import QueueItem
    sup, timers, rec = fresh_supervisor
    item = QueueItem(track=_u1_track(), play_recorded=True)
    play = AsyncMock(return_value=None)
    with patch.object(state, "output_router", _u2_router(play)):
        ok = await state._play_with_fallback(item, _u1_client())
    assert ok is True
    assert item.play_recorded is False           # consumed
    sup.on_playback_confirmed(sup.current_token())
    rec.assert_not_called()                      # dispatch still carried the mark


async def test_do_advance_device_failure_enters_hold_with_item_next_up(fresh_supervisor):
    """Idle-entry outage (plan scenario): a guest queues against a dead device
    — dispatch fails device-level — hold with that track next-up, ZERO items
    consumed, no TrackSkippedEvent, queue paused. The stranded-`current` bug
    path is gone."""
    import app.state as state
    from app.output import session
    from app.output.base import DeviceNotReadyError
    from app.queue.engine import QueueEngine
    sup, timers, rec = fresh_supervisor
    qe = QueueEngine()
    play = AsyncMock(side_effect=DeviceNotReadyError("no device"))
    skipped = AsyncMock()
    with patch.object(state, "queue_engine", qe), \
         patch("app.queue.engine.database.save_queue", AsyncMock()), \
         patch("app.queue.engine.database.save_history", AsyncMock()), \
         patch.object(state, "output_router", _u2_router(play)), \
         patch.object(state, "get_plex_client", AsyncMock(return_value=_u1_client())), \
         patch.object(state, "_emit_track_skipped", skipped), \
         patch("app.events.bus.manager.broadcast_to_admins", AsyncMock()):
        await qe.append(_u2_track("t1"))
        await qe.append(_u2_track("t2"))
        await state._do_advance()

        assert session.output_hold_active() is True
        assert session.output_hold_reason() == "dispatch_failed"
        assert [i.track_id for i in qe.queue] == ["t1", "t2"]  # nothing consumed
        assert qe.queue[0].play_recorded is False              # never counted
        assert qe.state.current is None
        assert qe.state.is_paused is True
        skipped.assert_not_called()
        rec.assert_not_called()


async def test_do_advance_bails_while_hold_active(fresh_supervisor, monkeypatch):
    """The hold flag gates _do_advance (R15): a stale EOS advance racing the
    hold consumes nothing."""
    import app.state as state
    from app.output import session
    monkeypatch.setattr(hold, "_output_hold", True)
    advance = AsyncMock()
    with patch.object(state.queue_engine, "advance", advance):
        await state._do_advance()
    advance.assert_not_called()


async def test_do_advance_device_lost_from_tie_breaker_enters_hold(fresh_supervisor):
    """DeviceLostError raised by the holder tie-breaker routes to the same
    hold as DeviceNotReadyError (it subclasses it)."""
    import app.state as state
    from app.output import session
    from app.queue.engine import QueueEngine
    sup, timers, rec = fresh_supervisor
    qe = QueueEngine()
    play = AsyncMock(side_effect=Exception("transport error"))
    probe = AsyncMock(return_value=(False, None))
    skipped = AsyncMock()
    with patch.object(state, "queue_engine", qe), \
         patch("app.queue.engine.database.save_queue", AsyncMock()), \
         patch("app.queue.engine.database.save_history", AsyncMock()), \
         patch.object(state, "output_router", _u2_router(play, probe)), \
         patch.object(state, "get_plex_client", AsyncMock(return_value=_u1_client())), \
         patch.object(state, "_emit_track_skipped", skipped), \
         patch("app.events.bus.manager.broadcast_to_admins", AsyncMock()):
        await qe.append(_u2_track("t1"))
        await qe.append(_u2_track("t2"))
        await state._do_advance()

        assert session.output_hold_active() is True
        assert [i.track_id for i in qe.queue] == ["t1", "t2"]
        skipped.assert_not_called()


async def test_do_advance_all_dead_holders_reachable_device_skips_as_today(fresh_supervisor):
    """Plan scenario: a track with all-dead holders on a REACHABLE device is
    skipped exactly as today — TrackSkippedEvent, queue advances to the next
    item, no hold."""
    import app.state as state
    from app.output import session
    from app.queue.engine import QueueEngine
    sup, timers, rec = fresh_supervisor
    qe = QueueEngine()
    # t1's holder 404s (device reachable); t2 then plays fine.
    play = AsyncMock(side_effect=[Exception("404"), None])
    probe = AsyncMock(return_value=(True, "PLAYING"))
    skipped = AsyncMock()
    with patch.object(state, "queue_engine", qe), \
         patch("app.queue.engine.database.save_queue", AsyncMock()), \
         patch("app.queue.engine.database.save_history", AsyncMock()), \
         patch.object(state, "output_router", _u2_router(play, probe)), \
         patch.object(state, "get_plex_client", AsyncMock(return_value=_u1_client())), \
         patch.object(state, "_emit_track_skipped", skipped):
        await qe.append(_u2_track("t1"))
        await qe.append(_u2_track("t2"))
        await state._do_advance()

        assert session.output_hold_active() is False
        skipped.assert_awaited_once()            # t1 announced as skipped
        assert qe.state.current is not None
        assert qe.state.current.track_id == "t2" # queue advanced past the dead track
        assert qe.queue == []


def test_should_auto_start_gated_by_output_hold(monkeypatch):
    """queue_changed during a hold must not auto-start (it would re-dispatch
    the held item straight into the dead device)."""
    import app.state as st
    from app.output import session
    fake_qe = MagicMock()
    fake_qe.state.is_playing = False
    fake_qe.queue = [MagicMock()]
    monkeypatch.setattr(st, "queue_engine", fake_qe)
    monkeypatch.setattr(st, "_auto_advance_pending", False)
    monkeypatch.setattr(st, "_closing_active", False)
    monkeypatch.setattr(hold, "_output_hold", False)
    assert st._should_auto_start() is True
    monkeypatch.setattr(hold, "_output_hold", True)
    assert st._should_auto_start() is False


async def test_dispatch_play_wires_backend_probe_into_deadline(fresh_supervisor):
    """The U1 deadline extension gets the real backend probe: deadline expiry
    consults the active backend's probe_liveness, and a reachable
    pre-playback device earns the single extension."""
    import app.state as state
    sup, timers, rec = fresh_supervisor
    play = AsyncMock(return_value=None)
    probe = AsyncMock(return_value=(True, "BUFFERING"))
    with patch.object(state, "output_router", _u2_router(play, probe)):
        await state.dispatch_play("http://url", _u1_track())
    assert len(timers.timers) == 1
    timers.timers[0].fire()                      # fire the deadline
    for _ in range(8):
        await asyncio.sleep(0)
    probe.assert_awaited_once()                  # the backend probe was consulted
    assert len(timers.timers) == 2               # extension armed


async def test_output_probe_none_without_backend_or_method():
    import app.state as state
    router = MagicMock()
    router.active = None
    with patch.object(state, "output_router", router):
        assert state._output_probe() is None
    router.active = MagicMock(spec=[])           # backend without probe_liveness
    with patch.object(state, "output_router", router):
        assert state._output_probe() is None


async def test_activate_backend_during_hold_clears_hold(fresh_supervisor, monkeypatch):
    """R17's U2 slice: a manual device/backend switch during an outage hold
    is a manual resume — the hold clears after a successful switch so the
    auto-start can dispatch the held item onto the new device."""
    import app.state as state
    from app.output import session
    sup, timers, rec = fresh_supervisor
    monkeypatch.setattr(hold, "_output_hold", True)
    monkeypatch.setattr(hold, "_output_hold_reason", "connection_lost")
    monkeypatch.setattr(state, "_auto_advance_pending", False)
    backend = MagicMock()
    backend.set_device = AsyncMock()
    fake_qe = MagicMock()
    fake_qe.state.is_playing = False
    fake_qe.queue = [MagicMock()]
    trigger = AsyncMock()
    with patch.object(state, "_get_backend", lambda t: backend), \
         patch.object(state, "output_router", MagicMock()), \
         patch.object(state, "queue_engine", fake_qe), \
         patch.object(state, "_trigger_auto_advance", trigger), \
         patch("app.database.set_setting", AsyncMock()):
        await state.activate_backend("chromecast", "dev-1")
        assert session.output_hold_active() is False
        await asyncio.sleep(0)                    # drain the auto-start task
        trigger.assert_awaited_once()


async def test_activate_backend_failed_switch_keeps_hold(fresh_supervisor, monkeypatch):
    """A switch whose set_device fails must NOT clear the hold — the outage
    is still real and nothing new can play."""
    import app.state as state
    from app.output import session
    sup, timers, rec = fresh_supervisor
    monkeypatch.setattr(hold, "_output_hold", True)
    monkeypatch.setattr(hold, "_output_hold_reason", "connection_lost")
    backend = MagicMock()
    backend.set_device = AsyncMock(side_effect=RuntimeError("unreachable"))
    with patch.object(state, "_get_backend", lambda t: backend), \
         patch.object(state, "output_router", MagicMock()), \
         patch("app.database.set_setting", AsyncMock()):
        with pytest.raises(RuntimeError):
            await state.activate_backend("chromecast", "dev-1")
        assert session.output_hold_active() is True


# ── U3: switch-as-resume cancellation half ────────────────────────────────────

async def test_activate_backend_bumps_epoch_and_retires_retry_loop(
        fresh_supervisor, monkeypatch):
    """R17 (U3): a manual switch cancels the old device's retry loop
    ATOMICALLY before any await — epoch bumped, outage context retired, its
    backoff timer dead — even while the hold stays until the switch lands."""
    import app.state as state
    from app.output import session
    sup, timers, rec = fresh_supervisor
    # Open a real outage context with a retry loop on a fake dlna device.
    held_backend = MagicMock(spec=["_device_id", "set_device", "get_position"])
    held_backend._device_id = "dev-old"
    held_backend.get_position = AsyncMock(return_value=1000)
    fake_qe = MagicMock()
    fake_qe.state.is_paused = False
    fake_qe.state.current = None
    fake_qe.set_paused = AsyncMock()
    router = MagicMock()
    router.active = held_backend
    with patch.object(state, "queue_engine", fake_qe), \
         patch.object(state, "output_router", router), \
         patch.object(state, "dlna_backend", held_backend), \
         patch.object(state, "_auto_advance_pending", True), \
         patch("app.events.bus.manager.broadcast_to_admins", AsyncMock()):
        await session.enter_output_hold("connection_lost")
        ot = sup._outage
        assert ot is not None and ot.timer is not None
        retry_timer = ot.timer
        epoch_before = sup.attach_epoch

        new_backend = MagicMock()
        new_backend.set_device = AsyncMock()
        with patch.object(state, "_get_backend", lambda t: new_backend), \
             patch("app.database.set_setting", AsyncMock()):
            await state.activate_backend("chromecast", "dev-new")

        assert sup.attach_epoch == epoch_before + 1
        assert sup._outage is None                    # retired atomically
        assert any(t is retry_timer and t.cancelled
                   for t in timers.timers)            # backoff timer dead
        assert session.output_hold_active() is False  # cleared post-switch


async def test_activate_backend_failed_switch_reopens_reconnect_loop(
        fresh_supervisor, monkeypatch):
    """F6: a FAILED device switch during a hold must not kill auto-reconnect
    permanently — notify_manual_switch retired the outage context up front,
    so the set_device-failure path re-opens it: fresh retry loop armed for
    the ORIGINAL device, original entered_at preserved (the resume window
    keeps counting from the original failure, R8), hold intact."""
    import app.state as state
    from app.output import session
    from app.output.session import RETRY_BACKOFF_START_S
    sup, timers, rec = fresh_supervisor
    held_backend = MagicMock(spec=["_device_id", "set_device", "get_position"])
    held_backend._device_id = "dev-old"
    held_backend.get_position = AsyncMock(return_value=1000)
    fake_qe = MagicMock()
    fake_qe.state.is_paused = False
    fake_qe.state.current = None
    fake_qe.set_paused = AsyncMock()
    router = MagicMock()
    router.active = held_backend
    with patch.object(state, "queue_engine", fake_qe), \
         patch.object(state, "output_router", router), \
         patch.object(state, "dlna_backend", held_backend), \
         patch("app.events.bus.manager.broadcast_to_admins", AsyncMock()), \
         patch("app.events.bus.manager.broadcast_to_guests", AsyncMock()):
        await session.enter_output_hold("connection_lost")
        entered = sup._outage.entered_at
        held_position = sup._outage.held_position_ms

        new_backend = MagicMock()
        new_backend.set_device = AsyncMock(side_effect=RuntimeError("unreachable"))
        with patch.object(state, "_get_backend", lambda t: new_backend), \
             patch("app.database.set_setting", AsyncMock()):
            with pytest.raises(RuntimeError):
                await state.activate_backend("chromecast", "dev-new")

        assert session.output_hold_active() is True   # hold survived
        ot = sup._outage
        assert ot is not None                         # context REOPENED
        assert ot.device_id == "dev-old"              # the original device
        assert ot.entered_at == entered               # same window (R8)
        assert ot.held_position_ms == held_position
        assert sup.session_state == session.STATE_OUTAGE_PAUSED
        assert ot.timer is not None                   # retry loop re-armed
        handle = timers.timers[-1]
        assert handle is ot.timer and not handle.cancelled
        assert handle.delay == RETRY_BACKOFF_START_S  # fresh backoff
        for _ in range(4):
            await asyncio.sleep(0)                    # settle _schedule_emit



# ── plexplayer backend wiring (2026-08-04-002 plan U3) ───────────────────────

def _plex_source(machine_id="srv-A", server_url="http://192.168.1.10:32400",
                 token="tok-A", client_id="cid-1"):
    from app.plex.client import PlexClient
    from app.sources.plex import PlexSource
    return PlexSource(PlexClient(
        server_url=server_url, token=token, client_id=client_id,
        machine_id=machine_id))


def _registry(*sources):
    from app.sources.registry import SourceRegistry
    return SourceRegistry(list(sources))


def _companion_player(mid, name="Caldera",
                      caps=("playqueues-creation", "timeline")):
    from app.plex.companion import CompanionPlayer
    return CompanionPlayer(
        name=name, machine_identifier=mid, address="192.168.1.88",
        port=32500, product="Caldera",
        protocol_capabilities=frozenset(caps))


class _FakePms:
    def __init__(self, players=None, error=None):
        self._players = list(players or [])
        self._error = error
        self.closed = False

    async def get_clients(self):
        if self._error is not None:
            raise self._error
        return list(self._players)

    async def aclose(self):
        self.closed = True


def _pms_cls(fakes_by_machine_id):
    class _FakePmsCls:
        @staticmethod
        def from_plex_client(plex_client):
            return fakes_by_machine_id[plex_client.machine_id]
    return _FakePmsCls


async def test_plexplayer_registered_in_backend_maps():
    """plexplayer resolves through _get_backend and _backend_type_of — the
    supervisor's reconnect mechanics and activate_backend key off these."""
    import app.state as st
    fake = object()
    with patch("app.state.plexplayer_backend", fake):
        assert st._get_backend("plexplayer") is fake
        assert st._backend_type_of(fake) == "plexplayer"


async def test_plexplayer_clients_source_merges_and_dedupes_by_machine_id():
    """Two servers, concurrent legs, one player visible via both → one
    merged entry (machineIdentifier dedupe, first server wins); every PMS
    client is closed after its leg."""
    import app.state as st
    src_a = _plex_source("srv-A")
    src_b = _plex_source("srv-B", server_url="http://192.168.1.11:32400",
                         token="tok-B")
    fakes = {
        "srv-A": _FakePms([_companion_player("p1", "Living Room")]),
        "srv-B": _FakePms([_companion_player("p1", "Living Room"),
                           _companion_player("p2", "Den")]),
    }
    with patch("app.state.get_plex_client",
               AsyncMock(return_value=_registry(src_a, src_b))), \
         patch("app.database.get_disabled_sources", AsyncMock(return_value=[])), \
         patch("app.plex.companion.PmsCompanionClient", _pms_cls(fakes)):
        merged = await st._plexplayer_clients_source()
    assert [p.machine_identifier for p in merged] == ["p1", "p2"]
    assert all(f.closed for f in fakes.values())


async def test_plexplayer_clients_source_partial_failure_returns_survivors(caplog):
    """Dead-server lesson: one leg failing is LOGGED and skipped — the
    healthy server's players still merge (never raise on partial data)."""
    import logging
    import app.state as st
    src_a = _plex_source("srv-A")
    src_b = _plex_source("srv-B", server_url="http://192.168.1.11:32400")
    fakes = {
        "srv-A": _FakePms(error=RuntimeError("connect timeout")),
        "srv-B": _FakePms([_companion_player("p2", "Den")]),
    }
    with patch("app.state.get_plex_client",
               AsyncMock(return_value=_registry(src_a, src_b))), \
         patch("app.database.get_disabled_sources", AsyncMock(return_value=[])), \
         patch("app.plex.companion.PmsCompanionClient", _pms_cls(fakes)):
        with caplog.at_level(logging.WARNING, logger="app.state"):
            merged = await st._plexplayer_clients_source()
    assert [p.machine_identifier for p in merged] == ["p2"]
    assert any("srv-A" in r.message for r in caplog.records)


async def test_plexplayer_clients_source_all_legs_failed_raises():
    """EVERY leg failing = "no scan data" → raise (the watcher's
    sweep_devices contract: registry untouched, never a grace/eviction
    pass over a server outage)."""
    import app.state as st
    src_a = _plex_source("srv-A")
    fakes = {"srv-A": _FakePms(error=RuntimeError("dead"))}
    with patch("app.state.get_plex_client",
               AsyncMock(return_value=_registry(src_a))), \
         patch("app.database.get_disabled_sources", AsyncMock(return_value=[])), \
         patch("app.plex.companion.PmsCompanionClient", _pms_cls(fakes)):
        with pytest.raises(RuntimeError):
            await st._plexplayer_clients_source()


async def test_plexplayer_clients_source_no_sources_returns_empty():
    import app.state as st
    with patch("app.state.get_plex_client", AsyncMock(return_value=None)):
        assert await st._plexplayer_clients_source() == []


async def test_plexplayer_clients_source_skips_vetoed_sources():
    """A disabled_sources-vetoed server contributes no leg at all (the
    Libraries-panel whole-source veto reaches Companion discovery too)."""
    import app.state as st
    src_a = _plex_source("srv-A")
    src_b = _plex_source("srv-B", server_url="http://192.168.1.11:32400")
    fakes = {"srv-A": _FakePms([_companion_player("p1")])}
    # srv-B deliberately absent from fakes: a leg for it would KeyError.
    with patch("app.state.get_plex_client",
               AsyncMock(return_value=_registry(src_a, src_b))), \
         patch("app.database.get_disabled_sources",
               AsyncMock(return_value=["srv-B"])), \
         patch("app.plex.companion.PmsCompanionClient", _pms_cls(fakes)):
        merged = await st._plexplayer_clients_source()
    assert [p.machine_identifier for p in merged] == ["p1"]


async def test_plexplayer_server_info_resolver_builds_from_registry():
    """server_info_resolver reads the CURRENT registry per call: the source
    whose source_id == the holder's machine id yields protocol/address/port
    (companion.server_coordinates over the reachability-probed server_url);
    unknown machine id → None. No token field (S-4): player auth rides the
    Companion client's header token."""
    import app.state as st
    src = _plex_source("srv-A", server_url="https://10.0.0.9:32400",
                       token="tok-A")
    with patch("app.state.get_plex_client",
               AsyncMock(return_value=_registry(src))), \
         patch("app.database.get_disabled_sources", AsyncMock(return_value=[])):
        info = await st._plexplayer_server_info_resolver("srv-A")
        missing = await st._plexplayer_server_info_resolver("srv-nope")
    assert info is not None
    assert (info.protocol, info.address, info.port) == ("https", "10.0.0.9", 32400)
    assert info.machine_id == "srv-A"
    assert not hasattr(info, "token")
    assert missing is None


async def test_plexplayer_server_info_resolver_excludes_vetoed_source():
    import app.state as st
    src = _plex_source("srv-A")
    with patch("app.state.get_plex_client",
               AsyncMock(return_value=_registry(src))), \
         patch("app.database.get_disabled_sources",
               AsyncMock(return_value=["srv-A"])):
        assert await st._plexplayer_server_info_resolver("srv-A") is None


async def test_plexplayer_rating_key_resolver_via_alias_table():
    """Catalog path: track id → identity → alias members; the member with
    the requested server's machine-id prefix yields the bare rating key
    (the compound local_key shape PlexClient._make_id mints). External ids
    and other servers' members are skipped."""
    import app.state as st
    from app.models import Track
    track = Track(id="mbid:abc", title="Song", artist="A", album="B",
                  duration_ms=1000)
    with patch("app.catalog.identity.identity_for_track_id",
               AsyncMock(return_value="mbid:abc")), \
         patch("app.catalog.store.get_aliases_for_identity",
               AsyncMock(return_value=[
                   "mbid:abc", "srv-A:42", "srv-B:77"])):
        assert await st._plexplayer_rating_key_resolver(
            "srv-B", "/library/parts/9/f.flac", track) == "77"
        assert await st._plexplayer_rating_key_resolver(
            "srv-A", "/library/parts/9/f.flac", track) == "42"


async def test_plexplayer_rating_key_resolver_native_path_and_no_match():
    """No catalog identity: the track id itself is prefix-matched (native
    single-Plex path). No member for the server → None (the backend turns
    that into a track-level HolderResolutionError)."""
    import app.state as st
    from app.models import Track
    track = Track(id="srv-A:42", title="Song", artist="A", album="B",
                  duration_ms=1000)
    with patch("app.catalog.identity.identity_for_track_id",
               AsyncMock(return_value=None)):
        assert await st._plexplayer_rating_key_resolver(
            "srv-A", "/library/parts/9/f.flac", track) == "42"
        assert await st._plexplayer_rating_key_resolver(
            "srv-B", "/library/parts/9/f.flac", track) is None


def test_plexplayer_rating_key_member_shapes():
    """Member-shape filter: only the bare ratingKey (_make_id shape) is
    metadata-shaped; ANY path (part-paths — and /library/metadata/, which
    no producer emits; S-6 dropped the dead tolerance) and empties are
    not."""
    import app.state as st
    assert st._plexplayer_rating_key_from_member("42") == "42"
    assert st._plexplayer_rating_key_from_member("/library/metadata/42") is None
    assert st._plexplayer_rating_key_from_member("/library/parts/1/2/f.flac") is None
    assert st._plexplayer_rating_key_from_member("") is None


async def test_plexplayer_client_factory_uses_primary_source_identity():
    """The sync factory builds a CompanionPlayerClient with the app's
    controller id and the PRIMARY (first) Plex source's token — per-server
    token selection at dispatch is server_info_resolver's job."""
    import app.state as st
    src = _plex_source("srv-A", token="tok-A", client_id="cid-1")
    with patch("app.state._plex_client", _registry(src)):
        client = st._plexplayer_client_factory("192.168.1.88", 32500,
                                               "caldera-1")
    try:
        assert client.host == "192.168.1.88"
        assert client.port == 32500
        assert client.target_machine_id == "caldera-1"
        assert client._headers["X-Plex-Token"] == "tok-A"
        assert client._headers["X-Plex-Client-Identifier"] == "cid-1"
        assert client._headers["X-Plex-Target-Client-Identifier"] == "caldera-1"
    finally:
        await client.aclose()


async def test_plexplayer_client_factory_without_sources_raises_not_ready():
    import app.state as st
    from app.output.base import DeviceNotReadyError
    with patch("app.state._plex_client", None):
        with pytest.raises(DeviceNotReadyError):
            st._plexplayer_client_factory("192.168.1.88", 32500, "caldera-1")


async def test_setup_constructs_plexplayer_backend_with_wiring():
    """The fifth backend carries advance_cb plus the four U2 injection
    points wired to the state-layer resolvers; the source of setup() pins
    that this exact construction runs at startup (running full setup would
    drag in GStreamer/DACP/zeroconf)."""
    import inspect
    import app.state as st
    from app.output.plexplayer import PlexPlayerBackend
    backend = PlexPlayerBackend(
        advance_cb=st._do_advance,
        rating_key_resolver=st._plexplayer_rating_key_resolver,
        server_info_resolver=st._plexplayer_server_info_resolver,
        clients_source=st._plexplayer_clients_source,
        client_factory=st._plexplayer_client_factory,
    )
    assert backend._advance_cb is st._do_advance
    assert backend._rating_key_resolver is st._plexplayer_rating_key_resolver
    assert backend._server_info_resolver is st._plexplayer_server_info_resolver
    assert backend._clients_source is st._plexplayer_clients_source
    assert backend._client_factory is st._plexplayer_client_factory
    src = inspect.getsource(st.setup)
    assert "plexplayer_backend = PlexPlayerBackend(" in src
    assert "rating_key_resolver=_plexplayer_rating_key_resolver" in src
    assert "client_factory=_plexplayer_client_factory" in src
    # And the startup-reconnect set includes plexplayer (background
    # reconnect path, not the direct set_device branch).
    assert '"plexplayer"' in inspect.getsource(st.setup)


async def test_activate_backend_plexplayer_builds_registry_and_persists():
    """activate_backend('plexplayer', …) ensures the source registry is
    built BEFORE set_device (the sync Companion client factory reads the
    module global) and persists the standard output_* selection keys."""
    import app.state as st
    order = []
    settings = {}

    async def fake_set(key, value):
        settings[key] = value

    async def fake_registry():
        order.append("registry")
        return object()

    backend = MagicMock()

    async def fake_set_device(device_id):
        order.append(("set_device", device_id))

    backend.set_device = fake_set_device
    with patch("app.state._get_backend", return_value=backend), \
         patch("app.state.get_plex_client", side_effect=fake_registry), \
         patch("app.database.set_setting", side_effect=fake_set):
        await st.activate_backend("plexplayer", "caldera-1")

    assert order == ["registry", ("set_device", "caldera-1")]
    assert settings["output_backend_type"] == "plexplayer"
    assert settings["output_device_id"] == "caldera-1"


async def test_startup_reconnect_plexplayer_rebinds_from_persisted_address():
    """Restart re-bind without a fresh sweep (plan U3 integration): the
    persisted output_addr:{device_id} JSON is enough — the backend's
    set_device reads it itself when its address cache is cold, and
    _startup_reconnect pre-builds the source registry for the sync client
    factory. discover_devices is never called."""
    import json
    import app.database as db
    from app.state import _startup_reconnect
    from app.output.plexplayer import PlexPlayerBackend

    built = []

    def factory(host, port, device_id):
        built.append((host, port, device_id))
        client = MagicMock()
        client.aclose = AsyncMock()
        return client

    backend = PlexPlayerBackend(client_factory=factory)
    backend.discover_devices = AsyncMock()
    cached = json.dumps(
        {"host": "192.168.1.88", "port": 32500, "name": "Caldera"})
    registry_build = AsyncMock(return_value=None)

    async def get_setting(key, default=None):
        return cached if key.startswith("output_addr:") else None

    with patch.object(db, "get_setting", side_effect=get_setting), \
         patch("app.database.set_setting", AsyncMock()), \
         patch("app.state.get_plex_client", registry_build):
        await _startup_reconnect(backend, "caldera-1")

    registry_build.assert_awaited()
    backend.discover_devices.assert_not_awaited()
    assert built == [("192.168.1.88", 32500, "caldera-1")]
    assert backend._device_id == "caldera-1"
    assert backend._session is not None
    assert backend.device_host("caldera-1") == "192.168.1.88"


# ── dispatch holder handshake (2026-08-04-002 plan U3) ───────────────────────

async def test_dispatch_play_hands_holder_key_to_capable_backend(fresh_supervisor):
    """dispatch_play hands the CURRENT attempt's holder key to a backend
    exposing set_dispatch_holder — unconditionally, so an explicit None
    clears any stale key; the backend consumes it one-shot."""
    import app.state as st
    from app.models import Track
    track = Track(id="srv-A:42", title="Song", artist="A", album="B",
                  duration_ms=1000)
    handed = []
    backend = MagicMock()
    backend.set_dispatch_holder = handed.append
    router = MagicMock()
    router.active = backend
    router.effective_backend.return_value = backend   # no pending switch
    router.play = AsyncMock()
    with patch("app.state.output_router", router):
        await st.dispatch_play("http://x", track,
                               holder_key="srv-A:/library/parts/9/f.flac")
        await st.dispatch_play("http://x", track)
    assert handed == ["srv-A:/library/parts/9/f.flac", None]
    assert router.play.await_count == 2


async def test_dispatch_play_deposits_holder_on_pending_backend(fresh_supervisor):
    """Review fix PLX-1: under a DEFERRED backend switch the holder key must
    land on the PENDING backend — play() runs swap_pending() first, so the
    pending backend is the one that consumes the key. Depositing on
    router.active handed it to the outgoing backend (first boundary dispatch
    degraded to metadata.id parsing / a stale key leaked into a later
    unrelated dispatch)."""
    import app.state as st
    from app.models import Track
    from app.output.router import OutputRouter
    track = Track(id="srv-A:42", title="Song", artist="A", album="B",
                  duration_ms=1000)
    old = MagicMock()
    old.is_playing = True                    # forces the deferred branch
    old.play = AsyncMock()
    old.stop = AsyncMock()
    old_handed = []
    old.set_dispatch_holder = old_handed.append
    handed = []
    new = MagicMock()
    new.play = AsyncMock()
    new.set_dispatch_holder = handed.append
    router = OutputRouter()
    router.set_backend(old)
    router.set_backend(new)                  # deferred: old is playing
    assert router.has_pending is True
    with patch("app.state.output_router", router):
        await st.dispatch_play("http://x", track, holder_key="srv-A:42")
    assert handed == ["srv-A:42"]            # the PENDING backend got the key
    # The outgoing backend never received this dispatch's key; the swap
    # cleared any stale one it held (an explicit None at retire time).
    assert "srv-A:42" not in old_handed
    new.play.assert_awaited_once()
    await asyncio.sleep(0)                   # drain the arming-eval task


async def test_dispatch_play_without_hook_is_untouched(fresh_supervisor):
    """A backend without set_dispatch_holder (all four existing ones) is
    byte-identical — no attribute poke, dispatch proceeds."""
    from types import SimpleNamespace
    import app.state as st
    from app.models import Track
    track = Track(id="t1", title="Song", artist="A", album="B",
                  duration_ms=1000)
    backend = SimpleNamespace()   # no set_dispatch_holder, no probe_liveness
    router = MagicMock()
    router.active = backend
    router.effective_backend = lambda: backend
    router.play = AsyncMock()
    with patch("app.state.output_router", router):
        await st.dispatch_play("http://x", track, holder_key="srv-A:1")
    router.play.assert_awaited_once()


# ── U4 (2026-08-04-002): gate truth + R9 holder filter/ordering ──────────────

class _RegSrc:
    def __init__(self, sid, stype="plex"):
        self.source_id = sid
        self.source_type = stype


class _Reg:
    def __init__(self, sources):
        self.sources = sources


class _FakePlexPlayerBackend:
    """last_dispatch_server is the only surface _holder_keys reads."""
    def __init__(self, server=None):
        self._server = server

    def last_dispatch_server(self):
        return self._server


def _plex_env(monkeypatch, *, selected="plexplayer", sources=None,
              disabled=(), bound=None):
    """Install the sync mirrors _holder_keys reads: persisted-selection
    mirror, registry module global, disabled-sources mirror, and the
    plexplayer backend singleton (its bound-server accessor)."""
    import app.state as st
    monkeypatch.setattr(st, "_selected_output_backend", selected)
    monkeypatch.setattr(st, "_plex_client", _Reg(sources if sources is not None
                                                 else [_RegSrc("m1"), _RegSrc("m2"),
                                                       _RegSrc("jelly", "jellyfin")]))
    monkeypatch.setattr(st, "_disabled_sources_sync", set(disabled))
    monkeypatch.setattr(st, "plexplayer_backend", _FakePlexPlayerBackend(bound))
    return st


def _mixed_holds_track(**kw):
    from app.models import Track
    return Track(id="x", title="Song", artist="Act", album="Rec",
                 duration_ms=1000,
                 stream_key=kw.get("stream_key", "jelly:k0"),
                 holds=kw.get("holds", [
                     {"source_id": "jelly", "key": "jelly:k1"},
                     {"source_id": "m1", "key": "m1:k1"},
                     {"source_id": "m2", "key": "m2:k1"},
                 ]))


def test_output_requires_plex_reads_persisted_mirror(monkeypatch):
    """The gate keys off the PERSISTED selection mirror — deliberately NOT
    output_router.active (deferred swap) — so it flips the moment
    activate_backend commits, backend attach state notwithstanding."""
    import app.state as st
    monkeypatch.setattr(st, "_selected_output_backend", "direct")
    assert st.output_requires_plex() is False
    monkeypatch.setattr(st, "_selected_output_backend", "plexplayer")
    assert st.output_requires_plex() is True


def test_holder_keys_byte_identical_on_other_backends(monkeypatch):
    """Explicit non-plexplayer guard: with any other persisted backend the
    full mixed holder list comes back in snapshot order — even with a
    registry, a disabled set, and a bound plexplayer backend installed."""
    st = _plex_env(monkeypatch, selected="chromecast",
                   disabled={"m1"}, bound="m2")
    t = _mixed_holds_track()
    assert st._holder_keys(t) == ["jelly:k1", "m1:k1", "m2:k1"]
    assert st._holder_keys(_mixed_holds_track(holds=[], stream_key="p")) == ["p"]
    assert st._holder_keys(_mixed_holds_track(holds=[], stream_key="")) == []


def test_holder_keys_plexplayer_filters_to_enabled_plex_sources(monkeypatch):
    """R9: under plexplayer only holders from enabled Plex sources survive;
    non-Plex holders are skipped entirely. No binding → priority order."""
    st = _plex_env(monkeypatch)
    assert st._holder_keys(_mixed_holds_track()) == ["m1:k1", "m2:k1"]


def test_holder_keys_plexplayer_same_server_first(monkeypatch):
    """Same-server-first: the bound player's last-dispatch server leads;
    the rest keep their priority order (stable partition)."""
    st = _plex_env(monkeypatch, bound="m2")
    assert st._holder_keys(_mixed_holds_track()) == ["m2:k1", "m1:k1"]


def test_holder_keys_plexplayer_disabled_source_excluded(monkeypatch):
    st = _plex_env(monkeypatch, disabled={"m2"})
    assert st._holder_keys(_mixed_holds_track()) == ["m1:k1"]


def test_holder_keys_plexplayer_stream_key_prefix_attribution(monkeypatch):
    """The holds-less fallback path attributes the bare stream_key via its
    compound prefix (Plex source_id == server machine_id): a Plex-prefixed
    key survives, a non-Plex one yields an empty eligible list."""
    st = _plex_env(monkeypatch)
    assert st._holder_keys(
        _mixed_holds_track(holds=[], stream_key="m1:p9")) == ["m1:p9"]
    assert st._holder_keys(
        _mixed_holds_track(holds=[], stream_key="jelly:p9")) == []
    assert st._holder_keys(
        _mixed_holds_track(holds=[], stream_key="bare-key")) == []


async def test_play_with_fallback_dispatches_plex_holder_under_plexplayer(
        monkeypatch, fresh_supervisor):
    """AE4 dispatch half: a Jellyfin-primary track with a Plex holder plays
    THROUGH the Plex holder on plexplayer — the filtered/ordered key is what
    dispatch streams AND what the holder handshake hands the backend."""
    from app.queue.models import QueueItem
    st = _plex_env(monkeypatch)
    item = QueueItem(track=_mixed_holds_track())
    play = AsyncMock()
    router = MagicMock(play=play)
    # PLX-1: the holder handshake targets the router's EFFECTIVE backend
    # (pending-or-active); with no pending switch that IS the active one.
    router.effective_backend.return_value = router.active
    client = MagicMock()
    client.stream_url = lambda k: f"url:{k}"
    with patch.object(st, "output_router", router), \
         patch.object(st, "record_play", MagicMock()):
        ok = await st._play_with_fallback(item, client)
    assert ok is True
    play.assert_awaited_once()
    assert play.await_args.args[0] == "url:m1:k1"          # Plex holder streamed
    router.active.set_dispatch_holder.assert_called_once_with("m1:k1")


async def test_activate_backend_broadcasts_source_lock_from_persisted_truth(
        monkeypatch, fresh_supervisor):
    """Mid-play switch contract (plan U4): committing plexplayer broadcasts
    OutputSessionEvent with source_lock "plex" IMMEDIATELY — from the
    persisted-selection mirror — while output_router.active still points at
    the old backend (the deferred-swap window). Switching back broadcasts
    None. Snapshot GETs and the push share session_snapshot(), so shape
    agreement is structural."""
    import app.state as st
    from app.output import session_events
    monkeypatch.setattr(st, "_selected_output_backend", "direct")
    guests, admins = AsyncMock(), AsyncMock()
    prev_active = st.output_router.active
    with patch("app.state._get_backend", return_value=None), \
         patch("app.database.set_setting", AsyncMock()), \
         patch("app.state.get_plex_client", AsyncMock(return_value=object())), \
         patch("app.events.bus.manager.broadcast_to_admins", admins), \
         patch("app.events.bus.manager.broadcast_to_guests", guests):
        await st.activate_backend("plexplayer", "default")
        assert st.output_requires_plex() is True
        # Router untouched (deferred truth ≠ gate truth): _get_backend was
        # None, so the old active backend still owns the current track.
        assert st.output_router.active is prev_active
        ev = guests.await_args.args[0]
        assert ev.type == "output_session" and ev.source_lock == "plex"
        assert admins.await_args.args[0].source_lock == "plex"
        # Push payload and snapshot GET agree (one render path).
        assert session_events.session_snapshot()["source_lock"] == "plex"

        await st.activate_backend("direct", "default")
        assert st.output_requires_plex() is False
        assert guests.await_args.args[0].source_lock is None


# ── U8 autofill plexplayer source-lock gate (2026-08-04-002 plan U8, R11) ────
# The playability check wraps _auto_fill_provider (post-selection, around the
# selection call) — never _shuffle_provider, which other callers rely on
# unfenced. Gate input resolves ONCE per cycle; give-up = no pick + one
# debounced admin notice.


def _held_track(tid, sid):
    t = _shuf_track(tid, 200000)
    t.holds = [{"source_id": sid, "key": f"{sid}:{tid}"}]
    return t


def _lock(monkeypatch_target, ids):
    return patch.object(monkeypatch_target, "plex_lock_enabled_ids",
                        AsyncMock(return_value=ids))


async def test_autofill_lock_rerolls_unplayable_selection():
    """An unplayable selection is re-rolled; the first Plex-playable pick is
    returned and no notice fires."""
    from app.queue.models import QueueEndBehavior
    with _ondeck_reset() as st:
        sel = AsyncMock(side_effect=[_held_track("bad", "jelly"),
                                     _held_track("good", "m1")])
        notify = AsyncMock()
        with _lock(st, {"m1"}), \
             patch.object(st, "_select_auto_fill_track", sel), \
             patch.object(st, "notify_plex_lock_giveup", notify):
            track = await st._auto_fill_provider(QueueEndBehavior.FULL_RANDOM)
        assert track is not None and track.id == "good"
        assert sel.await_count == 2
        notify.assert_not_awaited()


async def test_autofill_lock_bounded_giveup_notifies():
    """Zero Plex-playable candidates: bounded re-rolls (no infinite loop),
    no pick for the cycle, the give-up notice fires."""
    from app.queue.models import QueueEndBehavior
    with _ondeck_reset() as st:
        sel = AsyncMock(return_value=_held_track("bad", "jelly"))
        notify = AsyncMock()
        with _lock(st, {"m1"}), \
             patch.object(st, "_select_auto_fill_track", sel), \
             patch.object(st, "notify_plex_lock_giveup", notify):
            track = await st._auto_fill_provider(QueueEndBehavior.FULL_RANDOM)
        assert track is None
        assert sel.await_count == st._AUTOFILL_PLEX_LOCK_TRIES
        notify.assert_awaited_once()


async def test_autofill_lock_empty_library_quiet():
    """A None selection (library genuinely empty) is the pre-existing no-pick
    condition — quiet, not a lock give-up."""
    from app.queue.models import QueueEndBehavior
    with _ondeck_reset() as st:
        notify = AsyncMock()
        with _lock(st, {"m1"}), \
             patch.object(st, "_select_auto_fill_track", AsyncMock(return_value=None)), \
             patch.object(st, "notify_plex_lock_giveup", notify):
            track = await st._auto_fill_provider(QueueEndBehavior.FULL_RANDOM)
        assert track is None
        notify.assert_not_awaited()


async def test_autofill_lock_discards_unplayable_ondeck_and_rerolls():
    """A buffered on-deck pick that pre-dates the lock (no enabled-Plex holder)
    is discarded — slot cleared, gen bumped — and the cycle re-rolls to a
    playable selection."""
    from app.queue.models import QueueEndBehavior
    with _ondeck_reset() as st:
        st._ondeck = _held_track("stale", "jelly")
        gen0 = st._ondeck_gen
        sel = AsyncMock(return_value=_held_track("fresh", "m1"))
        with _lock(st, {"m1"}), \
             patch.object(st, "_select_auto_fill_track", sel), \
             patch.object(st, "notify_plex_lock_giveup", AsyncMock()):
            track = await st._auto_fill_provider(QueueEndBehavior.FULL_RANDOM)
        assert track is not None and track.id == "fresh"
        assert st._ondeck is None
        assert st._ondeck_gen == gen0 + 1


async def test_autofill_lock_serves_playable_ondeck_instantly():
    """A Plex-playable buffered pick is served without re-selection (the
    pre-buffer contract survives the gate)."""
    from app.queue.models import QueueEndBehavior
    with _ondeck_reset() as st:
        st._ondeck = _held_track("ondeck", "m1")
        sel = AsyncMock()
        with _lock(st, {"m1"}), \
             patch.object(st, "_select_auto_fill_track", sel):
            track = await st._auto_fill_provider(QueueEndBehavior.FULL_RANDOM)
        assert track is not None and track.id == "ondeck"
        sel.assert_not_awaited()


async def test_autofill_inert_other_backend_byte_identical():
    """Filter inert (gate input None — non-plexplayer selection): a hold-less
    pick returns on the first selection, no playability machinery, no notice."""
    from app.queue.models import QueueEndBehavior
    with _ondeck_reset() as st:
        sel = AsyncMock(return_value=_shuf_track("bare", 200000))  # no holds
        notify = AsyncMock()
        with _lock(st, None), \
             patch.object(st, "_select_auto_fill_track", sel), \
             patch.object(st, "notify_plex_lock_giveup", notify):
            track = await st._auto_fill_provider(QueueEndBehavior.FULL_RANDOM)
        assert track is not None and track.id == "bare"
        assert sel.await_count == 1
        notify.assert_not_awaited()


async def test_plex_lock_enabled_ids_gate_conditions(monkeypatch):
    """None off-plexplayer; None on the native all-Plex path; the enabled-ids
    set (even empty) while plexplayer is selected with the catalog active."""
    import app.state as st
    monkeypatch.setattr(st, "_selected_output_backend", "direct")
    assert await st.plex_lock_enabled_ids() is None
    monkeypatch.setattr(st, "_selected_output_backend", "plexplayer")
    with patch.object(st, "catalog_active", AsyncMock(return_value=False)):
        assert await st.plex_lock_enabled_ids() is None
    with patch.object(st, "catalog_active", AsyncMock(return_value=True)), \
         patch.object(st, "plex_enabled_source_ids",
                      AsyncMock(return_value={"m1"})):
        assert await st.plex_lock_enabled_ids() == {"m1"}
    with patch.object(st, "catalog_active", AsyncMock(return_value=True)), \
         patch.object(st, "plex_enabled_source_ids",
                      AsyncMock(return_value=set())):
        assert await st.plex_lock_enabled_ids() == set()


async def test_notify_plex_lock_giveup_debounces_per_session(monkeypatch):
    """The U6 notice vehicle fires ONCE per lock session (flag debounce):
    a second give-up stays silent until the flag resets."""
    import app.state as st
    monkeypatch.setattr(st, "_plex_lock_notice_sent", False)
    admins = AsyncMock()
    with patch("app.events.bus.manager.broadcast_to_admins", admins):
        await st.notify_plex_lock_giveup()
        await st.notify_plex_lock_giveup()
    admins.assert_awaited_once()
    ev = admins.await_args.args[0]
    assert ev.backend_type == "error"
    assert "no Plex-playable" in ev.device_name


async def test_activate_backend_resets_lock_notice_debounce(
        monkeypatch, fresh_supervisor):
    """Committing a selection starts a fresh lock session: the one-shot
    give-up notice is re-armed by activate_backend."""
    import app.state as st
    monkeypatch.setattr(st, "_selected_output_backend", "direct")
    monkeypatch.setattr(st, "_plex_lock_notice_sent", True)
    with patch("app.state._get_backend", return_value=None), \
         patch("app.database.set_setting", AsyncMock()), \
         patch("app.state.get_plex_client", AsyncMock(return_value=object())), \
         patch("app.events.bus.manager.broadcast_to_admins", AsyncMock()), \
         patch("app.events.bus.manager.broadcast_to_guests", AsyncMock()):
        await st.activate_backend("plexplayer", "default")
    assert st._plex_lock_notice_sent is False
