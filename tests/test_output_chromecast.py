"""Tests for ChromecastBackend — pychromecast and zeroconf are fully mocked."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call
from uuid import UUID

from app.plex.models import Track


def make_track(container="flac") -> Track:
    t = Track(id="t1", title="Song", artist="A", album="B", duration_ms=180000,
              stream_key="/parts/1/f." + container)
    t.container = container
    return t


def _make_cc(name="Living Room", uuid="abc-123"):
    cc = MagicMock()
    cc.uuid = uuid
    cc.name = name
    cc.media_controller = MagicMock()
    cc.media_controller.register_status_listener = MagicMock()
    cc.media_controller.play_media = MagicMock()
    cc.media_controller.block_until_active = MagicMock()
    cc.media_controller.pause = MagicMock()
    cc.media_controller.play = MagicMock()
    cc.media_controller.stop = MagicMock()
    cc.set_volume = MagicMock()
    cc.wait = MagicMock()
    return cc


def _make_cast_info(friendly_name="Living Room", uuid_str="abc-123", host="192.168.1.10", port=8009):
    info = MagicMock()
    info.friendly_name = friendly_name
    info.host = host
    info.port = port
    info.uuid = UUID(uuid_str) if len(uuid_str) == 36 else uuid_str
    return info


@pytest.fixture
def cast_mock():
    """Patch pychromecast, CastBrowser, SimpleCastListener, and zeroconf."""
    mock_pcc = MagicMock()
    mock_browser_cls = MagicMock()
    mock_listener_cls = MagicMock()
    mock_zconf_cls = MagicMock()
    with patch("app.output.chromecast._CAST_AVAILABLE", True), \
         patch("app.output.chromecast.pychromecast", mock_pcc, create=True), \
         patch("app.output.chromecast.CastBrowser", mock_browser_cls, create=True), \
         patch("app.output.chromecast.SimpleCastListener", mock_listener_cls, create=True), \
         patch("app.output.chromecast._zeroconf", MagicMock(Zeroconf=mock_zconf_cls), create=True):
        yield {
            "pcc": mock_pcc,
            "browser_cls": mock_browser_cls,
            "listener_cls": mock_listener_cls,
            "zconf_cls": mock_zconf_cls,
        }


# ── discovery ─────────────────────────────────────────────────────────────────

async def test_discover_starts_browser_and_returns_devices(cast_mock):
    from app.output.chromecast import ChromecastBackend
    backend = ChromecastBackend()

    # Simulate callbacks populating _cast_infos via the browser
    info1 = _make_cast_info("Kitchen", "00000000-0000-0000-0000-000000000001")
    info2 = _make_cast_info("Patio", "00000000-0000-0000-0000-000000000002")

    def fake_start_browser(self_=None):
        # Directly populate _cast_infos as the browser callbacks would
        backend._cast_infos["00000000-0000-0000-0000-000000000001"] = info1
        backend._cast_infos["00000000-0000-0000-0000-000000000002"] = info2
        backend._browser = MagicMock()
        backend._zconf = MagicMock()

    with patch.object(backend, "_start_browser", fake_start_browser), \
         patch.object(backend, "_wait_for_discovery", lambda: None):
        devices = await backend.discover_devices()

    assert len(devices) == 2
    names = {d.name for d in devices}
    assert "Kitchen" in names
    assert "Patio" in names
    assert all(d.backend_type == "chromecast" for d in devices)


async def test_discover_unavailable_returns_empty():
    with patch("app.output.chromecast._CAST_AVAILABLE", False):
        from app.output.chromecast import ChromecastBackend
        backend = ChromecastBackend()
        devices = await backend.discover_devices()
        assert devices == []


async def test_discover_browser_started_only_once(cast_mock):
    """A second discover_devices() call reuses the existing browser."""
    from app.output.chromecast import ChromecastBackend
    backend = ChromecastBackend()
    backend._browser = MagicMock()  # pre-populate so _start_browser won't be called
    backend._zconf = MagicMock()

    start_calls = []
    with patch.object(backend, "_start_browser", lambda: start_calls.append(1)), \
         patch.object(backend, "_wait_for_discovery", lambda: None):
        await backend.discover_devices()

    assert start_calls == [], "browser should not be restarted if already running"


async def test_discover_devices_added_during_wait_are_returned(cast_mock):
    """Devices announced by CastBrowser callbacks during the wait window appear in results.

    This is the regression test for the port-5353 contention fix: the wait must
    run in a thread pool (threading.Event.wait) so pychromecast's Zeroconf sockets
    are isolated from pyatv's asyncio-native sockets on the same port.
    """
    from app.output.chromecast import ChromecastBackend
    backend = ChromecastBackend()
    backend._browser = MagicMock()
    backend._zconf = MagicMock()
    info = _make_cast_info("JBL Charge", "00000000-0000-0000-0000-000000aabbcc")

    def fake_wait():
        uuid_obj = UUID("00000000-0000-0000-0000-000000aabbcc")
        backend._browser.devices = {uuid_obj: info}
        backend._on_cast_added(uuid_obj, "JBL Charge")

    with patch.object(backend, "_wait_for_discovery", fake_wait):
        devices = await backend.discover_devices()

    assert len(devices) == 1
    assert devices[0].name == "JBL Charge"
    assert devices[0].backend_type == "chromecast"


async def test_on_cast_added_populates_cast_infos(cast_mock):
    from app.output.chromecast import ChromecastBackend
    backend = ChromecastBackend()
    info = _make_cast_info("Bedroom", "00000000-0000-0000-0000-000000000003")
    uuid_obj = UUID("00000000-0000-0000-0000-000000000003")

    browser_mock = MagicMock()
    browser_mock.devices = {uuid_obj: info}
    backend._browser = browser_mock
    backend._zconf = MagicMock()

    backend._on_cast_added(uuid_obj, "Bedroom")
    assert "00000000-0000-0000-0000-000000000003" in backend._cast_infos


async def test_on_cast_removed_removes_from_cast_infos(cast_mock):
    from app.output.chromecast import ChromecastBackend
    backend = ChromecastBackend()
    info = _make_cast_info("Office", "00000000-0000-0000-0000-000000000004")
    uuid_obj = UUID("00000000-0000-0000-0000-000000000004")

    backend._cast_infos["00000000-0000-0000-0000-000000000004"] = info
    backend._on_cast_removed(uuid_obj, "Office", info)
    assert "00000000-0000-0000-0000-000000000004" not in backend._cast_infos


# ── continuous discovery subscription (U3) ────────────────────────────────────

GCAST = "_googlecast._tcp.local"


class _CastEvents:
    def __init__(self):
        self.events = []

    def __call__(self, kind, payload):
        self.events.append((kind, payload))

    def kinds(self, kind):
        return [p for k, p in self.events if k == kind]


async def _drain():
    for _ in range(4):
        await asyncio.sleep(0)


async def test_subscribe_discovery_unavailable_returns_none():
    from app.output.chromecast import ChromecastBackend
    with patch("app.output.chromecast._CAST_AVAILABLE", False):
        backend = ChromecastBackend()
        assert await backend.subscribe_discovery(_CastEvents()) is None


async def test_subscribe_discovery_starts_browser_and_emits_status_up(cast_mock):
    from app.output.chromecast import ChromecastBackend
    backend = ChromecastBackend()
    started = []

    def fake_start():
        backend._browser = MagicMock(devices={})
        started.append(1)

    cb = _CastEvents()
    with patch.object(backend, "_start_browser", fake_start):
        sub = await backend.subscribe_discovery(cb)
    await _drain()
    assert sub is not None
    assert started == [1]
    assert cb.kinds("status") == ["up"]
    assert backend._discovery_sub is sub


async def test_subscribe_discovery_replays_known_devices(cast_mock):
    from app.output.chromecast import ChromecastBackend
    backend = ChromecastBackend()
    info = _make_cast_info("JBL", "00000000-0000-0000-0000-000000000009",
                           host="192.168.1.9", port=8009)
    backend._cast_infos["00000000-0000-0000-0000-000000000009"] = info
    backend._browser = MagicMock(devices={})  # already started

    cb = _CastEvents()
    await backend.subscribe_discovery(cb)
    await _drain()
    news = cb.kinds("new")
    assert news == [("JBL", "192.168.1.9", 8009,
                     "00000000-0000-0000-0000-000000000009", {}, GCAST)]


async def test_cast_added_emits_new_and_keeps_cast_infos(cast_mock):
    from app.output.chromecast import ChromecastBackend
    backend = ChromecastBackend()
    info = _make_cast_info("Bedroom", "00000000-0000-0000-0000-000000000003",
                           host="192.168.1.3", port=8009)
    uuid_obj = UUID("00000000-0000-0000-0000-000000000003")
    backend._browser = MagicMock(devices={uuid_obj: info})

    cb = _CastEvents()
    await backend.subscribe_discovery(cb)
    cb.events.clear()  # drop the status/replay from subscribe
    backend._on_cast_added(uuid_obj, "Bedroom")
    await _drain()
    # _cast_infos populated for playback AND the watcher saw the arrival.
    assert "00000000-0000-0000-0000-000000000003" in backend._cast_infos
    assert cb.kinds("new") == [("Bedroom", "192.168.1.3", 8009,
                                "00000000-0000-0000-0000-000000000003", {}, GCAST)]


async def test_cast_removed_emits_remove_keyed_by_friendly_name(cast_mock):
    from app.output.chromecast import ChromecastBackend
    backend = ChromecastBackend()
    info = _make_cast_info("Office", "00000000-0000-0000-0000-000000000004")
    uuid_obj = UUID("00000000-0000-0000-0000-000000000004")
    backend._browser = MagicMock(devices={})
    backend._cast_infos["00000000-0000-0000-0000-000000000004"] = info

    cb = _CastEvents()
    await backend.subscribe_discovery(cb)
    cb.events.clear()
    backend._on_cast_removed(uuid_obj, "Office", info)
    await _drain()
    assert "00000000-0000-0000-0000-000000000004" not in backend._cast_infos
    assert cb.kinds("remove") == [("Office", GCAST)]


async def test_cast_callback_thread_cross_delivers_on_loop(cast_mock):
    """A CastBrowser callback firing off the asyncio loop still reaches
    on_event on the loop (thread-cross correctness)."""
    from app.output.chromecast import ChromecastBackend
    backend = ChromecastBackend()
    info = _make_cast_info("Patio", "00000000-0000-0000-0000-000000000005",
                           host="192.168.1.5", port=8009)
    uuid_obj = UUID("00000000-0000-0000-0000-000000000005")
    backend._browser = MagicMock(devices={uuid_obj: info})

    cb = _CastEvents()
    await backend.subscribe_discovery(cb)
    cb.events.clear()
    await asyncio.to_thread(backend._on_cast_added, uuid_obj, "Patio")
    await _drain()
    assert cb.kinds("new")[0][1] == "192.168.1.5"


async def test_unsubscribe_discovery_stops_events_keeps_browser(cast_mock):
    from app.output.chromecast import ChromecastBackend
    backend = ChromecastBackend()
    info = _make_cast_info("Den", "00000000-0000-0000-0000-000000000006",
                           host="192.168.1.6", port=8009)
    uuid_obj = UUID("00000000-0000-0000-0000-000000000006")
    backend._browser = MagicMock(devices={uuid_obj: info})

    cb = _CastEvents()
    sub = await backend.subscribe_discovery(cb)
    await _drain()  # let the subscribe-time status 'up' land
    await backend.unsubscribe_discovery(sub)
    assert backend._discovery_sub is None
    assert backend._browser is not None  # browser stays up for playback
    cb.events.clear()
    backend._on_cast_added(uuid_obj, "Den")
    await _drain()
    assert cb.events == []  # silenced after unsubscribe


def test_snapshot_devices_is_uuid_keyed(cast_mock):
    from app.output.chromecast import ChromecastBackend
    from app.output.base import OutputDevice
    backend = ChromecastBackend()
    info = _make_cast_info("Kitchen", "00000000-0000-0000-0000-000000000007")
    backend._cast_infos["00000000-0000-0000-0000-000000000007"] = info
    devices = backend.snapshot_devices()
    assert devices == [OutputDevice(
        id="00000000-0000-0000-0000-000000000007", name="Kitchen",
        backend_type="chromecast", id_format="uuid")]


# ── set_device ────────────────────────────────────────────────────────────────

async def test_set_device_uses_cached_cast_info(cast_mock):
    """set_device() uses get_chromecast_from_cast_info when cache is warm."""
    from app.output.chromecast import ChromecastBackend
    cc = _make_cc("Living Room", "abc-123")
    info = _make_cast_info("Living Room", "00000000-0000-0000-0000-000000000001")
    cast_mock["pcc"].get_chromecast_from_cast_info.return_value = cc

    backend = ChromecastBackend()
    backend._cast_infos["00000000-0000-0000-0000-000000000001"] = info
    backend._browser = MagicMock()
    backend._zconf = MagicMock()

    with patch("app.database.get_setting", AsyncMock(return_value=None)):
        await backend.set_device("00000000-0000-0000-0000-000000000001")

    cast_mock["pcc"].get_chromecast_from_cast_info.assert_called_once_with(info, backend._zconf)
    cast_mock["pcc"].get_chromecasts.assert_not_called()


async def test_set_device_fallback_to_scan_when_no_cache(cast_mock):
    """set_device() falls back to one-shot get_chromecasts when cache is empty."""
    from app.output.chromecast import ChromecastBackend
    cc = _make_cc("Living Room", "abc-123")
    cast_mock["pcc"].get_chromecasts.return_value = ([cc], MagicMock())

    backend = ChromecastBackend()
    # No _cast_infos, no _browser

    with patch("app.database.get_setting", AsyncMock(return_value=None)):
        await backend.set_device("abc-123")

    cast_mock["pcc"].get_chromecasts.assert_called_once()
    cast_mock["pcc"].get_chromecast_from_cast_info.assert_not_called()


async def test_sync_connect_in_degraded_mode_raises_without_scan(cast_mock):
    """In degraded mode (5353 unavailable), _sync_connect must not attempt the
    one-shot Zeroconf scan.

    When _mdns_port_unavailable is True and the device_id (a UUID saved from a
    prior session) is not in _dbus_index or _cast_infos, _sync_connect must
    raise RuntimeError without calling get_chromecasts — which would try to
    bind port 5353 and crash with EADDRINUSE.
    """
    import pytest
    from app.output.chromecast import ChromecastBackend

    backend = ChromecastBackend()
    # _dbus_index and _cast_infos are empty — UUID-keyed device not found

    with patch("app.state._mdns_port_unavailable", True):
        with pytest.raises(RuntimeError, match="in-process mDNS"):
            backend._sync_connect("308c00d1-117f-a74c-600c-b4c97d433fd4")

    cast_mock["pcc"].get_chromecasts.assert_not_called()


# ── play ──────────────────────────────────────────────────────────────────────

async def test_play_calls_play_media_flac(cast_mock):
    from app.output.chromecast import ChromecastBackend
    cc = _make_cc()
    backend = ChromecastBackend()
    backend._cast = cc
    await backend.play("http://plex.local/file.flac", make_track("flac"))
    cc.media_controller.play_media.assert_called_once()
    args = cc.media_controller.play_media.call_args
    assert args[0][0] == "http://plex.local/file.flac"
    assert args[0][1] == "audio/flac"


async def test_play_calls_play_media_mp3(cast_mock):
    from app.output.chromecast import ChromecastBackend
    cc = _make_cc()
    backend = ChromecastBackend()
    backend._cast = cc
    await backend.play("http://plex.local/file.mp3", make_track("mp3"))
    args = cc.media_controller.play_media.call_args
    assert args[0][1] == "audio/mpeg"


async def test_play_calls_play_media_aac(cast_mock):
    from app.output.chromecast import ChromecastBackend
    cc = _make_cc()
    backend = ChromecastBackend()
    backend._cast = cc
    await backend.play("http://plex.local/file.aac", make_track("aac"))
    args = cc.media_controller.play_media.call_args
    assert args[0][1] == "audio/aac"


async def test_play_proxied_ogg_advertises_flac(cast_mock):
    """Regression (2026-06-17): /api/stream transcodes OGG→FLAC, so the Cast
    receiver MUST be told audio/flac. Advertising the source type (audio/ogg)
    made the receiver init an Ogg pipeline, play ~1s of the FLAC bytes, then
    error and drop the control channel. Route on stream_key (the real part
    path) — production Tracks never set .container, so the old container-based
    guess silently fell through to mimetypes→audio/ogg."""
    from app.output.chromecast import ChromecastBackend
    cc = _make_cc()
    backend = ChromecastBackend()
    backend._cast = cc
    # A production-shaped Track: no .container attribute, real .ogg stream_key.
    track = Track(id="t", title="S", artist="A", album="B", duration_ms=1000,
                  stream_key="/library/parts/89497/1/file.ogg")
    await backend.play(
        "http://192.168.1.50/api/stream?key=k%3A%2Flibrary%2Fparts%2F89497%2F1%2Ffile.ogg",
        track,
    )
    args = cc.media_controller.play_media.call_args
    assert args[0][1] == "audio/flac"


async def test_play_direct_ogg_not_transcoded_keeps_ogg(cast_mock):
    """A direct Plex URL (no /api/stream proxy) is NOT transcoded — the device
    fetches raw Ogg from Plex, so keep the native audio/ogg type."""
    from app.output.chromecast import ChromecastBackend
    cc = _make_cc()
    backend = ChromecastBackend()
    backend._cast = cc
    track = Track(id="t", title="S", artist="A", album="B", duration_ms=1000,
                  stream_key="/library/parts/89497/1/file.ogg")
    await backend.play(
        "http://plex.local/library/parts/89497/1/file.ogg?X-Plex-Token=t", track,
    )
    args = cc.media_controller.play_media.call_args
    assert args[0][1] == "audio/ogg"


async def test_play_sets_is_playing(cast_mock):
    from app.output.chromecast import ChromecastBackend
    cc = _make_cc()
    backend = ChromecastBackend()
    backend._cast = cc
    await backend.play("http://url", make_track())
    assert backend.is_playing is True


async def test_play_no_device_raises(cast_mock):
    from app.output.chromecast import ChromecastBackend
    backend = ChromecastBackend()
    with pytest.raises(RuntimeError):
        await backend.play("http://url", make_track())


# ── pause / resume / stop ─────────────────────────────────────────────────────

async def test_pause_calls_media_controller(cast_mock):
    from app.output.chromecast import ChromecastBackend
    cc = _make_cc()
    backend = ChromecastBackend()
    backend._cast = cc
    await backend.play("http://url", make_track())
    await backend.pause()
    cc.media_controller.pause.assert_called_once()
    assert backend.is_playing is False


async def test_resume_calls_media_controller(cast_mock):
    from app.output.chromecast import ChromecastBackend
    cc = _make_cc()
    backend = ChromecastBackend()
    backend._cast = cc
    await backend.play("http://url", make_track())
    await backend.pause()
    await backend.resume()
    cc.media_controller.play.assert_called_once()
    assert backend.is_playing is True


async def test_stop_calls_media_controller(cast_mock):
    from app.output.chromecast import ChromecastBackend
    cc = _make_cc()
    backend = ChromecastBackend()
    backend._cast = cc
    await backend.play("http://url", make_track())
    await backend.stop()
    cc.media_controller.stop.assert_called_once()
    assert backend.is_playing is False


# ── volume ────────────────────────────────────────────────────────────────────

async def test_set_volume_delegates_to_cast(cast_mock):
    from app.output.chromecast import ChromecastBackend
    cc = _make_cc()
    backend = ChromecastBackend()
    backend._cast = cc
    await backend.set_volume(0.7)
    cc.set_volume.assert_called_once_with(0.7)
    assert await backend.get_volume() == pytest.approx(0.7)


async def test_volume_clamped(cast_mock):
    from app.output.chromecast import ChromecastBackend
    backend = ChromecastBackend()
    await backend.set_volume(1.5)
    assert await backend.get_volume() == 1.0
    await backend.set_volume(-0.3)
    assert await backend.get_volume() == 0.0


# ── EOS / advance ─────────────────────────────────────────────────────────────

async def test_eos_listener_fires_advance(cast_mock):
    from app.output.chromecast import ChromecastBackend, _AdvanceListener
    advance_called = []

    async def advance():
        advance_called.append(True)

    cc = _make_cc()
    backend = ChromecastBackend(advance_cb=advance)
    backend._cast = cc
    await backend.play("http://url", make_track())

    status = MagicMock()
    status.player_state = "IDLE"
    status.idle_reason = "FINISHED"
    listener = _AdvanceListener(backend)
    listener.new_media_status(status)

    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert advance_called


async def test_eos_listener_ignores_non_finished(cast_mock):
    from app.output.chromecast import ChromecastBackend, _AdvanceListener
    advance_called = []

    async def advance():
        advance_called.append(True)

    cc = _make_cc()
    backend = ChromecastBackend(advance_cb=advance)
    backend._cast = cc

    status = MagicMock()
    status.player_state = "PLAYING"
    status.idle_reason = None
    listener = _AdvanceListener(backend)
    listener.new_media_status(status)

    await asyncio.sleep(0)
    assert not advance_called


async def test_eos_listener_fires_advance_on_error(cast_mock):
    """A track ending with idle_reason=ERROR (the silent-stall culprit) must
    advance the queue, not freeze it. This is the core fix (U1)."""
    from app.output.chromecast import ChromecastBackend, _AdvanceListener
    advance_called = []

    async def advance():
        advance_called.append(True)

    backend = ChromecastBackend(advance_cb=advance)
    backend._is_playing = True
    backend._loop = asyncio.get_running_loop()

    status = MagicMock()
    status.player_state = "IDLE"
    status.idle_reason = "ERROR"
    status.current_time = None
    _AdvanceListener(backend).new_media_status(status)

    await asyncio.sleep(0.05)
    assert advance_called


async def test_eos_listener_error_logs_warning(cast_mock, caplog):
    """The ERROR advance path logs a WARNING so the stall is visible in logs."""
    import logging
    from app.output.chromecast import ChromecastBackend, _AdvanceListener

    async def advance():
        pass

    backend = ChromecastBackend(advance_cb=advance)
    backend._is_playing = True
    backend._loop = asyncio.get_running_loop()

    status = MagicMock()
    status.player_state = "IDLE"
    status.idle_reason = "ERROR"
    status.current_time = None
    with caplog.at_level(logging.WARNING, logger="app.output.chromecast"):
        _AdvanceListener(backend).new_media_status(status)
        await asyncio.sleep(0.05)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings
    assert any("ERROR" in r.getMessage() for r in warnings)


async def test_eos_listener_ignores_cancelled(cast_mock):
    """CANCELLED is self-induced (our own stop/skip) — must NOT advance."""
    from app.output.chromecast import ChromecastBackend, _AdvanceListener
    advance_called = []

    async def advance():
        advance_called.append(True)

    backend = ChromecastBackend(advance_cb=advance)
    backend._is_playing = True
    backend._loop = asyncio.get_running_loop()

    status = MagicMock()
    status.player_state = "IDLE"
    status.idle_reason = "CANCELLED"
    status.current_time = None
    _AdvanceListener(backend).new_media_status(status)

    await asyncio.sleep(0.05)
    assert not advance_called


async def test_eos_listener_ignores_interrupted(cast_mock):
    """INTERRUPTED is self-induced (our own next-track play) — must NOT advance."""
    from app.output.chromecast import ChromecastBackend, _AdvanceListener
    advance_called = []

    async def advance():
        advance_called.append(True)

    backend = ChromecastBackend(advance_cb=advance)
    backend._is_playing = True
    backend._loop = asyncio.get_running_loop()

    status = MagicMock()
    status.player_state = "IDLE"
    status.idle_reason = "INTERRUPTED"
    status.current_time = None
    _AdvanceListener(backend).new_media_status(status)

    await asyncio.sleep(0.05)
    assert not advance_called


async def test_eos_listener_ignores_error_before_play(cast_mock):
    """An ERROR while _is_playing is False (pre-play / post-stop) must NOT
    advance — a pre-play IDLE carries no terminal meaning."""
    from app.output.chromecast import ChromecastBackend, _AdvanceListener
    advance_called = []

    async def advance():
        advance_called.append(True)

    backend = ChromecastBackend(advance_cb=advance)
    backend._is_playing = False
    backend._loop = asyncio.get_running_loop()

    status = MagicMock()
    status.player_state = "IDLE"
    status.idle_reason = "ERROR"
    status.current_time = None
    _AdvanceListener(backend).new_media_status(status)

    await asyncio.sleep(0.05)
    assert not advance_called


# ── duration watchdog (U2) ────────────────────────────────────────────────────

async def test_watchdog_fires_advance_when_no_eos(cast_mock):
    """A hung stream that never reports any terminal status still advances:
    the watchdog fires after duration + grace."""
    from unittest.mock import patch as _patch
    from app.output.chromecast import ChromecastBackend
    advance_called = []

    async def advance():
        advance_called.append(True)

    backend = ChromecastBackend(advance_cb=advance)
    backend._is_playing = True
    backend._play_token = 7

    with _patch("app.output.chromecast.asyncio.sleep", AsyncMock(return_value=None)):
        await backend._watchdog(7, 1000)

    assert advance_called


async def test_watchdog_token_guard_prevents_stale_advance(cast_mock):
    """A watchdog from a superseded play() (token mismatch) must NOT advance —
    this is what stops a stale backstop from firing after a manual skip."""
    from unittest.mock import patch as _patch
    from app.output.chromecast import ChromecastBackend
    advance_called = []

    async def advance():
        advance_called.append(True)

    backend = ChromecastBackend(advance_cb=advance)
    backend._is_playing = True
    backend._play_token = 8  # newer than the watchdog's captured token

    with _patch("app.output.chromecast.asyncio.sleep", AsyncMock(return_value=None)):
        await backend._watchdog(7, 1000)  # stale token

    assert not advance_called


async def test_watchdog_not_playing_guard(cast_mock):
    """A watchdog that wakes after stop() (is_playing False) must NOT advance."""
    from unittest.mock import patch as _patch
    from app.output.chromecast import ChromecastBackend
    advance_called = []

    async def advance():
        advance_called.append(True)

    backend = ChromecastBackend(advance_cb=advance)
    backend._is_playing = False
    backend._play_token = 5

    with _patch("app.output.chromecast.asyncio.sleep", AsyncMock(return_value=None)):
        await backend._watchdog(5, 1000)

    assert not advance_called


async def test_play_arms_watchdog_with_duration(cast_mock):
    """play() with a known duration arms the watchdog backstop."""
    from app.output.chromecast import ChromecastBackend
    cc = _make_cc()
    backend = ChromecastBackend()
    backend._cast = cc
    await backend.play("http://url", make_track())  # duration_ms=180000
    assert backend._watchdog_task is not None
    backend._cancel_watchdog()  # cleanup the real sleeping task


async def test_play_no_watchdog_without_duration(cast_mock):
    """play() with unknown duration (live/streaming) arms no watchdog —
    there's nothing to time against."""
    from app.output.chromecast import ChromecastBackend
    cc = _make_cc()
    backend = ChromecastBackend()
    backend._cast = cc
    track = make_track()
    track.duration_ms = 0
    await backend.play("http://url", track)
    assert backend._watchdog_task is None


async def test_stop_cancels_watchdog(cast_mock):
    """stop() cancels a pending watchdog so it can't force a stale advance."""
    from app.output.chromecast import ChromecastBackend
    cc = _make_cc()
    backend = ChromecastBackend()
    backend._cast = cc
    await backend.play("http://url", make_track())
    assert backend._watchdog_task is not None
    await backend.stop()
    assert backend._watchdog_task is None


async def test_pause_cancels_watchdog(cast_mock):
    """pause() cancels the watchdog (a paused track must not count down)."""
    from app.output.chromecast import ChromecastBackend
    cc = _make_cc()
    backend = ChromecastBackend()
    backend._cast = cc
    await backend.play("http://url", make_track())
    await backend.pause()
    assert backend._watchdog_task is None


async def test_clean_eos_cancels_watchdog(cast_mock):
    """A clean FINISHED end cancels the pending watchdog (no stale backstop),
    then advances."""
    from app.output.chromecast import ChromecastBackend
    advance_called = []

    async def advance():
        advance_called.append(True)

    cc = _make_cc()
    backend = ChromecastBackend(advance_cb=advance)
    backend._cast = cc
    await backend.play("http://url", make_track())
    wd = backend._watchdog_task
    assert wd is not None
    await backend._handle_eos("FINISHED")
    assert backend._watchdog_task is None
    # Let the cancellation propagate through the loop, then confirm retired.
    try:
        await wd
    except asyncio.CancelledError:
        pass
    assert wd.cancelled()
    assert advance_called


# ── failure visibility (U3) ──────────────────────────────────────────────────

async def test_handle_eos_error_broadcasts_admin_event(cast_mock):
    """A non-FINISHED end broadcasts an error OutputChangedEvent to admins so
    the operator sees the failure, then advances."""
    from unittest.mock import patch as _patch
    from app.output.chromecast import ChromecastBackend
    advance_called = []

    async def advance():
        advance_called.append(True)

    backend = ChromecastBackend(advance_cb=advance)
    broadcast = AsyncMock()
    with _patch("app.events.bus.manager.broadcast_to_admins", broadcast):
        await backend._handle_eos("ERROR")

    assert advance_called
    broadcast.assert_called_once()
    event = broadcast.call_args.args[0]
    assert getattr(event, "backend_type", None) == "error"


async def test_handle_eos_near_end_error_treated_as_clean(cast_mock):
    """A receiver ERROR at ~the real end of a seeked track is a mislabeled clean
    finish (no FLAC seektable → seek desync, 2026-06-17). It must advance WITHOUT
    toasting an admin failure."""
    from unittest.mock import patch as _patch
    from app.output.chromecast import ChromecastBackend, NEAR_END_GRACE_MS
    advance_called = []

    async def advance():
        advance_called.append(True)

    backend = ChromecastBackend(advance_cb=advance)
    backend._duration_ms = 200_000
    backend._pos_snapshot_ms = 200_000 - NEAR_END_GRACE_MS // 2  # within the grace
    broadcast = AsyncMock()
    with _patch("app.events.bus.manager.broadcast_to_admins", broadcast):
        await backend._handle_eos("ERROR")

    assert advance_called
    broadcast.assert_not_called()  # treated as a clean end — no failure toast


async def test_handle_eos_midtrack_error_still_broadcasts(cast_mock):
    """An ERROR well before the end is a genuine failure — still toast + advance,
    so the near-end grace doesn't mask real mid-track failures."""
    from unittest.mock import patch as _patch
    from app.output.chromecast import ChromecastBackend
    advance_called = []

    async def advance():
        advance_called.append(True)

    backend = ChromecastBackend(advance_cb=advance)
    backend._duration_ms = 200_000
    backend._pos_snapshot_ms = 30_000  # far from the end
    broadcast = AsyncMock()
    with _patch("app.events.bus.manager.broadcast_to_admins", broadcast):
        await backend._handle_eos("ERROR")

    assert advance_called
    broadcast.assert_called_once()


async def test_seek_reanchors_and_rearms_watchdog(cast_mock):
    """Seeking re-anchors position and re-arms the watchdog for the post-seek
    remainder (skipping ahead ends the track earlier than the play()-time
    deadline) — the 2026-06-17 FLAC-seek belt-and-suspenders."""
    from app.output.chromecast import ChromecastBackend
    cc = _make_cc()
    backend = ChromecastBackend()
    backend._cast = cc
    await backend.play("http://url", make_track())  # duration_ms=180000, playing
    first_wd = backend._watchdog_task
    first_token = backend._play_token

    await backend.seek(120_000)

    assert backend._pos_snapshot_ms == 120_000      # re-anchored
    assert backend._play_token == first_token + 1   # watchdog re-armed (new token)
    assert backend._watchdog_task is not first_wd
    cc.media_controller.seek.assert_called_once()
    backend._cancel_watchdog()


async def test_handle_eos_watchdog_broadcasts_admin_event(cast_mock):
    """The watchdog-driven advance also surfaces an admin error event."""
    from unittest.mock import patch as _patch
    from app.output.chromecast import ChromecastBackend

    async def advance():
        pass

    backend = ChromecastBackend(advance_cb=advance)
    broadcast = AsyncMock()
    with _patch("app.events.bus.manager.broadcast_to_admins", broadcast):
        await backend._handle_eos("watchdog")

    broadcast.assert_called_once()
    assert broadcast.call_args.args[0].backend_type == "error"


async def test_handle_eos_finished_broadcasts_nothing(cast_mock):
    """A clean end is quiet — no error toast for the normal path."""
    from unittest.mock import patch as _patch
    from app.output.chromecast import ChromecastBackend
    advance_called = []

    async def advance():
        advance_called.append(True)

    backend = ChromecastBackend(advance_cb=advance)
    broadcast = AsyncMock()
    with _patch("app.events.bus.manager.broadcast_to_admins", broadcast):
        await backend._handle_eos("FINISHED")

    assert advance_called
    broadcast.assert_not_called()


async def test_handle_eos_broadcast_failure_still_advances(cast_mock):
    """A broadcast failure is swallowed and must not block the advance."""
    from unittest.mock import patch as _patch
    from app.output.chromecast import ChromecastBackend
    advance_called = []

    async def advance():
        advance_called.append(True)

    backend = ChromecastBackend(advance_cb=advance)
    broadcast = AsyncMock(side_effect=RuntimeError("ws down"))
    with _patch("app.events.bus.manager.broadcast_to_admins", broadcast):
        await backend._handle_eos("ERROR")

    assert advance_called


# ── ghost listener cleanup ────────────────────────────────────────────────────

async def test_set_device_unregisters_old_listener(cast_mock):
    """set_device must call unregister_status_listener on the old cast's media_controller."""
    from app.output.chromecast import ChromecastBackend, _AdvanceListener

    cc1 = _make_cc("Old Device", "abc-001")
    cc1.media_controller.unregister_status_listener = MagicMock()
    cc2 = _make_cc("New Device", "abc-002")

    info2 = _make_cast_info("New Device", "00000000-0000-0000-0000-000000000002")
    cast_mock["pcc"].get_chromecast_from_cast_info.return_value = cc2

    backend = ChromecastBackend()
    backend._cast = cc1
    old_listener = _AdvanceListener(backend)
    backend._listener = old_listener
    backend._cast_infos["00000000-0000-0000-0000-000000000002"] = info2
    backend._browser = MagicMock()
    backend._zconf = MagicMock()

    with patch("app.database.get_setting", AsyncMock(return_value=None)):
        await backend.set_device("00000000-0000-0000-0000-000000000002")

    cc1.media_controller.unregister_status_listener.assert_called_once_with(old_listener)


async def test_set_device_no_old_listener_no_unregister(cast_mock):
    """set_device with no prior listener must not call unregister."""
    from app.output.chromecast import ChromecastBackend
    cc = _make_cc("Device", "abc-001")
    info = _make_cast_info("Device", "00000000-0000-0000-0000-000000000001")
    cast_mock["pcc"].get_chromecast_from_cast_info.return_value = cc

    backend = ChromecastBackend()
    backend._cast_infos["00000000-0000-0000-0000-000000000001"] = info
    backend._browser = MagicMock()
    backend._zconf = MagicMock()

    with patch("app.database.get_setting", AsyncMock(return_value=None)):
        await backend.set_device("00000000-0000-0000-0000-000000000001")

    cc.media_controller.unregister_status_listener.assert_not_called()


# ── shared Zeroconf (port 5353 fix) ──────────────────────────────────────────

async def test_start_browser_uses_shared_zconf_when_set(cast_mock):
    """When _shared_zconf is injected, _start_browser uses it instead of creating a new one."""
    from app.output.chromecast import ChromecastBackend
    backend = ChromecastBackend()
    shared_zc = MagicMock()
    backend._shared_zconf = shared_zc

    with patch.object(backend, "_wait_for_discovery", lambda: None):
        await backend.discover_devices()

    # The shared Zeroconf should be stored without being owned
    assert backend._zconf is shared_zc
    assert backend._zconf_owned is False
    # The module-level Zeroconf class must not have been called
    cast_mock["zconf_cls"].assert_not_called()


async def test_close_does_not_close_shared_zconf(cast_mock):
    """close() must not close an externally provided Zeroconf instance."""
    from app.output.chromecast import ChromecastBackend
    backend = ChromecastBackend()
    shared_zc = MagicMock()
    backend._zconf = shared_zc
    backend._zconf_owned = False

    backend.close()

    shared_zc.close.assert_not_called()


async def test_close_closes_owned_zconf(cast_mock):
    """close() closes the Zeroconf instance when we created it ourselves."""
    from app.output.chromecast import ChromecastBackend
    backend = ChromecastBackend()
    owned_zc = MagicMock()
    backend._zconf = owned_zc
    backend._zconf_owned = True
    backend._browser = MagicMock()

    backend.close()

    owned_zc.close.assert_called_once()


# ── avahi/D-Bus fallback discovery + address cache + name cleaning ────────────

async def test_discover_devices_falls_back_to_dbus_when_port_unavailable(cast_mock):
    """2026-06-16 fix: a failed 5353 bind routes discover_devices to the
    avahi/D-Bus fallback (uuid-keyed), not an empty list."""
    from app.output.chromecast import ChromecastBackend

    backend = ChromecastBackend()
    uuid_str = "308c00d1-117f-a74c-600c-b4c97d433fd4"
    found = [("Living Room TV", "192.168.0.40", 8009, uuid_str, {"fn": "Living Room TV"})]

    with patch("app.state._mdns_port_unavailable", True), \
         patch("app.output.mdns_dbus.discover", AsyncMock(return_value=found)):
        devices = await backend.discover_devices()

    assert [d.id for d in devices] == [uuid_str]
    assert devices[0].name == "Living Room TV"
    assert devices[0].id_format == "uuid"
    # Address cache populated so connect-by-IP (get_chromecast_from_host) works.
    assert backend._dbus_index[uuid_str] == ("Living Room TV", "192.168.0.40", 8009)
    assert backend._dbus_index["192.168.0.40:8009"] == ("Living Room TV", "192.168.0.40", 8009)


async def test_dbus_discover_host_port_fallback_and_socket_absent(cast_mock):
    """No id= TXT → host:port id; mdns_dbus.discover None (socket absent) → []."""
    from app.output.chromecast import ChromecastBackend

    backend = ChromecastBackend()
    found = [("Bedroom", "192.168.0.41", 8009, None, {})]
    with patch("app.output.mdns_dbus.discover", AsyncMock(return_value=found)):
        devices = await backend._dbus_discover()
    assert devices[0].id == "192.168.0.41:8009"
    assert devices[0].id_format == "host_port"

    with patch("app.output.mdns_dbus.discover", AsyncMock(return_value=None)):
        assert await backend._dbus_discover() == []


def test_clean_chromecast_name_prefers_fn_txt():
    """Name cleaning still preferred by register_resolved: the `fn` TXT wins
    over the dashed avahi label-with-uuid-suffix."""
    from app.output.chromecast import _clean_chromecast_dbus_name
    label = "JBL-Charge-5-Wi-Fi-S-308c00d1117fa74c600cb4c97d433fd4"
    txt = {"fn": "JBL Charge 5 Wi-Fi SE", "id": "308c00d1117fa74c600cb4c97d433fd4"}
    assert _clean_chromecast_dbus_name(label, txt) == "JBL Charge 5 Wi-Fi SE"


def test_clean_chromecast_name_strips_uuid_suffix_when_no_fn():
    """No `fn`: strip the trailing `-<32-hex>` cast-UUID suffix and turn the
    avahi-encoded dashes back into spaces."""
    from app.output.chromecast import _clean_chromecast_dbus_name
    label = "SHIELD-Android-TV-90c8f7182aee6ae5fdc4fe04d2f6c776"
    assert _clean_chromecast_dbus_name(label, {}) == "SHIELD Android TV"


def test_clean_chromecast_name_passthrough_without_suffix():
    """An already-clean label (no canonical UUID suffix) flows through
    unchanged."""
    from app.output.chromecast import _clean_chromecast_dbus_name
    assert _clean_chromecast_dbus_name("Office Speaker", {}) == "Office Speaker"


async def test_sync_connect_uuid_in_degraded_mode_does_not_scan(cast_mock):
    """_sync_connect connects via the address cache (_dbus_index) even when
    _mdns_port_unavailable is True — no EADDRINUSE-prone one-shot scan."""
    from app.output.chromecast import ChromecastBackend

    uuid_str = "308c00d1-117f-a74c-600c-b4c97d433fd4"
    cc = _make_cc("Living Room TV")
    cc.wait.return_value = None
    cast_mock["pcc"].get_chromecast_from_host.return_value = cc

    backend = ChromecastBackend()
    backend._dbus_index[uuid_str] = ("Living Room TV", "192.168.1.10", 8009)

    with patch("app.state._mdns_port_unavailable", True):
        result = backend._sync_connect(uuid_str)

    cast_mock["pcc"].get_chromecasts.assert_not_called()
    cast_mock["pcc"].get_chromecast_from_host.assert_called_once_with(
        ("192.168.1.10", 8009, None, "Living Room TV", None)
    )
    assert result is cc


async def test_sync_connect_uses_dbus_index(cast_mock):
    """_sync_connect connects via _dbus_index when device_id is a UUID.

    This is the zero-touch reconnect path: a device selected as UUID is found
    in the address cache populated by register_resolved on live arrivals.
    """
    from app.output.chromecast import ChromecastBackend

    uuid_str = "308c00d1-117f-a74c-600c-b4c97d433fd4"
    cc = _make_cc("Living Room TV")
    cc.wait.return_value = None  # pychromecast 14 success
    cast_mock["pcc"].get_chromecast_from_host.return_value = cc

    backend = ChromecastBackend()
    backend._dbus_index[uuid_str] = ("Living Room TV", "192.168.1.10", 8009)

    result = backend._sync_connect(uuid_str)

    cast_mock["pcc"].get_chromecast_from_host.assert_called_once_with(
        ("192.168.1.10", 8009, None, "Living Room TV", None)
    )
    assert result is cc


async def test_sync_connect_dbus_index_raises_on_timeout(cast_mock):
    """_sync_connect raises RuntimeError when an _dbus_index-mapped connect times out."""
    import pytest
    from app.output.chromecast import ChromecastBackend

    class _FakeTimeout(Exception):
        pass

    uuid_str = "308c00d1-117f-a74c-600c-b4c97d433fd4"
    cc = _make_cc("Living Room TV")
    cc.wait.side_effect = _FakeTimeout("timed out")
    cast_mock["pcc"].get_chromecast_from_host.return_value = cc

    backend = ChromecastBackend()
    backend._dbus_index[uuid_str] = ("Living Room TV", "192.168.1.10", 8009)

    with patch("app.output.chromecast._RequestTimeout", _FakeTimeout):
        with pytest.raises(RuntimeError, match="did not connect"):
            backend._sync_connect(uuid_str)


async def test_sync_connect_uses_dbus_device(cast_mock):
    """_sync_connect uses get_chromecast_from_host when device_id is in _dbus_index."""
    from app.output.chromecast import ChromecastBackend

    cc = _make_cc("Living Room")
    cc.wait.return_value = True
    cast_mock["pcc"].get_chromecast_from_host.return_value = cc

    backend = ChromecastBackend()
    backend._dbus_index["192.168.1.10:8009"] = ("Living Room", "192.168.1.10", 8009)

    result = backend._sync_connect("192.168.1.10:8009")

    cast_mock["pcc"].get_chromecast_from_host.assert_called_once_with(
        ("192.168.1.10", 8009, None, "Living Room", None)
    )
    assert result is cc


async def test_sync_connect_dbus_raises_on_timeout(cast_mock):
    """_sync_connect raises RuntimeError when pychromecast 14 raises RequestTimeout."""
    import pytest
    from unittest.mock import patch
    from app.output.chromecast import ChromecastBackend

    class _FakeTimeout(Exception):
        pass

    cc = _make_cc("Living Room")
    cc.wait.side_effect = _FakeTimeout("timed out")
    cast_mock["pcc"].get_chromecast_from_host.return_value = cc

    backend = ChromecastBackend()
    backend._dbus_index["192.168.1.10:8009"] = ("Living Room", "192.168.1.10", 8009)

    with patch("app.output.chromecast._RequestTimeout", _FakeTimeout):
        with pytest.raises(RuntimeError, match="did not connect"):
            backend._sync_connect("192.168.1.10:8009")


async def test_sync_connect_dbus_succeeds_when_wait_returns_none(cast_mock):
    """wait() returning None is pychromecast 14 success — _sync_connect must NOT raise."""
    from app.output.chromecast import ChromecastBackend

    cc = _make_cc("Living Room")
    cc.wait.return_value = None  # pychromecast >= 14: None means connected OK
    cast_mock["pcc"].get_chromecast_from_host.return_value = cc

    backend = ChromecastBackend()
    backend._dbus_index["192.168.1.10:8009"] = ("Living Room", "192.168.1.10", 8009)

    result = backend._sync_connect("192.168.1.10:8009")
    assert result is cc  # should succeed, not raise


# ── address cache persistence (R1, R2) ───────────────────────────────────────

async def test_set_device_persists_address_via_dbus_path(cast_mock):
    """R1: after connecting via D-Bus (_dbus_index), the resolved host:port is cached in DB."""
    import json
    from app.output.chromecast import ChromecastBackend

    cc = _make_cc("Living Room TV")
    cc.wait.return_value = None
    cast_mock["pcc"].get_chromecast_from_host.return_value = cc

    backend = ChromecastBackend()
    uuid_str = "308c00d1-117f-a74c-600c-b4c97d433fd4"
    backend._dbus_index[uuid_str] = ("Living Room TV", "192.168.1.10", 8009)

    saved = {}

    async def fake_set_setting(key, value):
        saved[key] = value

    with patch("app.database.get_setting", AsyncMock(return_value=None)), \
         patch("app.database.set_setting", fake_set_setting):
        await backend.set_device(uuid_str)

    addr_key = f"output_addr:{uuid_str}"
    assert addr_key in saved, "output_addr must be persisted after successful D-Bus connection"
    addr = json.loads(saved[addr_key])
    assert addr["host"] == "192.168.1.10"
    assert addr["port"] == 8009
    assert addr["name"] == "Living Room TV"


async def test_set_device_persists_address_via_cast_info_path(cast_mock):
    """R1: after connecting via CastInfo browser cache, the resolved host:port is cached in DB."""
    import json
    from app.output.chromecast import ChromecastBackend

    cc = _make_cc("Kitchen Speaker")
    cc.wait.return_value = None
    cast_mock["pcc"].get_chromecast_from_cast_info.return_value = cc

    info = _make_cast_info("Kitchen Speaker", "00000000-0000-0000-0000-000000000001",
                           host="192.168.1.25", port=8009)
    backend = ChromecastBackend()
    backend._cast_infos["00000000-0000-0000-0000-000000000001"] = info
    backend._browser = MagicMock()
    backend._zconf = MagicMock()

    saved = {}

    async def fake_set_setting(key, value):
        saved[key] = value

    with patch("app.database.get_setting", AsyncMock(return_value=None)), \
         patch("app.database.set_setting", fake_set_setting):
        await backend.set_device("00000000-0000-0000-0000-000000000001")

    addr_key = "output_addr:00000000-0000-0000-0000-000000000001"
    assert addr_key in saved
    addr = json.loads(saved[addr_key])
    assert addr["host"] == "192.168.1.25"
    assert addr["port"] == 8009


async def test_set_device_updates_cache_on_reconnect(cast_mock):
    """R2: reconnecting to the same device after an IP change updates the cached address."""
    import json
    from app.output.chromecast import ChromecastBackend

    uuid_str = "308c00d1-117f-a74c-600c-b4c97d433fd4"
    cc = _make_cc("TV")
    cc.wait.return_value = None
    cast_mock["pcc"].get_chromecast_from_host.return_value = cc

    backend = ChromecastBackend()
    # First connection: old IP
    backend._dbus_index[uuid_str] = ("TV", "192.168.1.10", 8009)
    saved = {}

    async def fake_set_setting(key, value):
        saved[key] = value

    with patch("app.database.get_setting", AsyncMock(return_value=None)), \
         patch("app.database.set_setting", fake_set_setting):
        await backend.set_device(uuid_str)

    addr_first = json.loads(saved[f"output_addr:{uuid_str}"])
    assert addr_first["host"] == "192.168.1.10"

    # Second connection: new IP (after DHCP renewal)
    backend._dbus_index[uuid_str] = ("TV", "192.168.1.55", 8009)
    saved.clear()
    cast_mock["pcc"].get_chromecast_from_host.return_value = _make_cc("TV")
    cast_mock["pcc"].get_chromecast_from_host.return_value.wait.return_value = None

    with patch("app.database.get_setting", AsyncMock(return_value=None)), \
         patch("app.database.set_setting", fake_set_setting):
        await backend.set_device(uuid_str)

    addr_second = json.loads(saved[f"output_addr:{uuid_str}"])
    assert addr_second["host"] == "192.168.1.55", "cache must be updated with new IP on reconnect"


# ── _VolumeListener volume_changed broadcast (U2) ──────────────────────────────

class _StatusStub:
    """Minimal duck-type for pychromecast's CastStatus carrying volume_level."""

    def __init__(self, volume_level):
        self.volume_level = volume_level


def test_volume_listener_ignores_none_status():
    """Defensive: a None status (some pychromecast edge cases) must not raise."""
    from app.output.chromecast import _VolumeListener, ChromecastBackend
    backend = ChromecastBackend()
    listener = _VolumeListener(backend)
    listener.new_cast_status(None)  # must not raise
    # Volume unchanged from default
    assert backend._volume == 0.5


def test_volume_listener_ignores_status_without_volume_level():
    """A status whose volume_level is None or missing must not raise or broadcast."""
    from app.output.chromecast import _VolumeListener, ChromecastBackend
    backend = ChromecastBackend()
    backend._loop = MagicMock()  # would broadcast if not for the None gate
    listener = _VolumeListener(backend)
    listener.new_cast_status(_StatusStub(volume_level=None))  # ValueError → early return
    # No broadcast attempt because we never got past the float() conversion
    backend._loop.call_soon_threadsafe.assert_not_called()


def test_volume_listener_echo_guard_suppresses_within_2s():
    """Server-initiated writes set _vol_last_set; events within 2s are dropped.

    Mirrors the existing state-update guard at chromecast.py:94 — the new
    broadcast call lives inside the same guarded branch so server echoes
    don't reach admin clients either.
    """
    import time
    from app.output.chromecast import _VolumeListener, ChromecastBackend
    backend = ChromecastBackend()
    backend._volume = 0.5
    backend._vol_last_set = time.monotonic()  # just set
    backend._loop = MagicMock()
    listener = _VolumeListener(backend)
    listener.new_cast_status(_StatusStub(volume_level=0.6))
    assert backend._volume == 0.5  # unchanged
    backend._loop.call_soon_threadsafe.assert_not_called()


def test_volume_listener_broadcasts_when_guard_expired():
    """After the 2s window expires, an external volume change updates state and broadcasts.

    Covers AE1 — admin client sees volume changes the device announces.
    """
    import time
    from unittest.mock import patch as _patch
    from app.output.chromecast import _VolumeListener, ChromecastBackend
    backend = ChromecastBackend()
    backend._volume = 0.5
    backend._vol_last_set = time.monotonic() - 3.0  # outside the 2s window
    backend._loop = MagicMock()
    listener = _VolumeListener(backend)

    # Patch run_coroutine_threadsafe so we don't need a real loop; capture call args.
    with _patch("app.output.chromecast.asyncio.run_coroutine_threadsafe") as rcs:
        listener.new_cast_status(_StatusStub(volume_level=0.6))

    assert backend._volume == 0.6  # state updated
    rcs.assert_called_once()
    # First positional arg is the coroutine; second is the loop we set above.
    coro_arg, loop_arg = rcs.call_args.args
    assert loop_arg is backend._loop
    # The coroutine should still be awaitable (don't actually await it; just ensure
    # it was produced). Close it so the test doesn't leak a never-awaited warning.
    coro_arg.close()


def test_volume_listener_skips_broadcast_when_loop_unset():
    """If play() has not been called yet, _loop is None — broadcast is skipped.

    State still updates (so a later read returns the right value) but the
    cross-thread hop is gated on having a loop to hop to.
    """
    import time
    from unittest.mock import patch as _patch
    from app.output.chromecast import _VolumeListener, ChromecastBackend
    backend = ChromecastBackend()
    backend._volume = 0.5
    backend._vol_last_set = time.monotonic() - 3.0
    backend._loop = None  # play() hasn't run yet
    listener = _VolumeListener(backend)

    with _patch("app.output.chromecast.asyncio.run_coroutine_threadsafe") as rcs:
        listener.new_cast_status(_StatusStub(volume_level=0.7))

    assert backend._volume == 0.7  # state still updated
    rcs.assert_not_called()


# ── U2: probe_device picker-facing wrapper ───────────────────────────────────


class _RequestTimeoutStub(Exception):
    """Stand-in for pychromecast.error.RequestTimeout. The probe wrapper
    catches whatever `_RequestTimeout` symbol the module currently binds,
    so we patch the module-level symbol AND raise this stub from wait()."""


async def test_probe_device_returns_true_on_successful_wait(cast_mock):
    """Happy path: get_chromecast_from_host returns a cast; wait() returns
    without raising → probe_device returns True. disconnect() is called
    in the finally block so the Zeroconf-backed connection is not leaked."""
    from app.output.chromecast import ChromecastBackend

    cc = _make_cc()
    # wait() returns None on success in pychromecast >= 14; no raise.
    cc.wait.return_value = None
    cast_mock["pcc"].get_chromecast_from_host.return_value = cc

    backend = ChromecastBackend()
    backend._dbus_index["uuid-1"] = ("WiiM Pro", "192.168.1.50", 8009)

    result = await backend.probe_device("uuid-1")

    assert result is True
    cast_mock["pcc"].get_chromecast_from_host.assert_called_once()
    cc.disconnect.assert_called_once()


async def test_probe_device_returns_false_on_wait_timeout(cast_mock):
    """wait() raising _RequestTimeout → False; disconnect() still called
    from the finally block. Mirrors the existing _sync_connect pattern."""
    from app.output import chromecast as cc_mod
    from app.output.chromecast import ChromecastBackend

    cc = _make_cc()
    cc.wait.side_effect = _RequestTimeoutStub("timed out")
    cast_mock["pcc"].get_chromecast_from_host.return_value = cc

    backend = ChromecastBackend()
    backend._dbus_index["uuid-1"] = ("WiiM Pro", "192.168.1.50", 8009)

    with patch.object(cc_mod, "_RequestTimeout", _RequestTimeoutStub):
        result = await backend.probe_device("uuid-1")

    assert result is False
    cc.disconnect.assert_called_once()


async def test_probe_device_returns_false_on_get_chromecast_exception(cast_mock):
    """get_chromecast_from_host raising at construct time → False with no
    leaked exception. The probe is the boundary; the picker downstream is
    allowed to assume probe_device never raises."""
    from app.output.chromecast import ChromecastBackend

    cast_mock["pcc"].get_chromecast_from_host.side_effect = RuntimeError("network glitch")

    backend = ChromecastBackend()
    backend._dbus_index["uuid-1"] = ("WiiM Pro", "192.168.1.50", 8009)

    assert await backend.probe_device("uuid-1") is False


async def test_probe_device_returns_false_for_unknown_device_id(cast_mock):
    """device_id not in _dbus_index AND not in _cast_infos → return False
    without attempting a connect. Avoids the EADDRINUSE / 5353-rebind path
    that _sync_connect would otherwise try."""
    from app.output.chromecast import ChromecastBackend

    backend = ChromecastBackend()
    # Both indexes empty.

    assert await backend.probe_device("unknown-uuid") is False
    cast_mock["pcc"].get_chromecast_from_host.assert_not_called()


async def test_probe_device_uses_cast_info_when_dbus_index_empty(cast_mock):
    """When _dbus_index doesn't carry the device but _cast_infos does, the
    probe still works — uses the friendly_name/host/port off CastInfo so a
    device discovered through the persistent browser is reachable."""
    from app.output.chromecast import ChromecastBackend

    cc = _make_cc()
    cc.wait.return_value = None
    cast_mock["pcc"].get_chromecast_from_host.return_value = cc

    backend = ChromecastBackend()
    info = _make_cast_info(
        friendly_name="WiiM Pro",
        uuid_str="uuid-2",
        host="192.168.1.50",
        port=8009,
    )
    backend._cast_infos["uuid-2"] = info

    assert await backend.probe_device("uuid-2") is True
    cc.disconnect.assert_called_once()


async def test_probe_device_returns_false_when_unavailable():
    """When pychromecast is not importable (_CAST_AVAILABLE False), probes
    return False without raising. The picker filters those entries from
    the Via dropdown."""
    with patch("app.output.chromecast._CAST_AVAILABLE", False):
        from app.output.chromecast import ChromecastBackend
        backend = ChromecastBackend()
        assert await backend.probe_device("any-id") is False
