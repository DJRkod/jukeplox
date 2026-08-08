"""Tests for ChromecastBackend — pychromecast and zeroconf are fully mocked."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call
from uuid import UUID

from app.output import hold  # the hold flag's home since the session decomposition
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


async def test_play_proxied_flac_advertises_flac(cast_mock):
    """Proxy URL (/api/stream?key=...) hides the extension in the query string,
    so the Cast LOAD content-type must be resolved from stream_key (the real part
    path), not the proxy URL. Regression (2026-08-03 ce-debug: JBL Charge 5 no
    audio): _content_type did split('?')[0] on the proxy URL, lost the .flac,
    and — since .flac is absent from Python's mimetypes — fell back to audio/mpeg,
    so the receiver rejected the FLAC and no media session formed. A production
    Track has no .container, so the container branch never rescued it."""
    from app.output.chromecast import ChromecastBackend
    cc = _make_cc()
    backend = ChromecastBackend()
    backend._cast = cc
    # Production-shaped Track: NO .container attribute, real .flac stream_key.
    track = Track(id="t", title="S", artist="A", album="B", duration_ms=40000,
                  stream_key="local-x:Test Tone Trio/Alpha Sessions/02 Mid E.flac")
    await backend.play(
        "http://192.168.1.50/api/stream?key=local-x%3ATest%20Tone%20Trio%2F"
        "Alpha%20Sessions%2F02%20Mid%20E.flac",
        track,
    )
    args = cc.media_controller.play_media.call_args
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


# ── confirmed-start signal (2026-07-11 supervisor plan U1) ────────────────────

async def test_first_playing_status_emits_confirmed_start(cast_mock, fresh_supervisor):
    """play() captures the supervisor's per-dispatch token; the FIRST PLAYING
    media status confirms it → record_play fires exactly once via the
    chokepoint. LOAD acceptance alone (play() returning) must not count."""
    from app.output.chromecast import ChromecastBackend, _AdvanceListener
    sup, timers, rec = fresh_supervisor
    cc = _make_cc()
    backend = ChromecastBackend()
    backend._cast = cc
    token = sup.on_dispatched(make_track())    # state dispatches before backend.play
    await backend.play("http://url", make_track())
    assert backend._confirm_token == token
    rec.assert_not_called()                    # command accepted ≠ audio

    status = MagicMock()
    status.player_state = "PLAYING"
    status.idle_reason = None
    status.current_time = 1.0
    _AdvanceListener(backend).new_media_status(status)
    await asyncio.sleep(0)                     # call_soon_threadsafe hop
    rec.assert_called_once()
    assert backend._confirm_token is None      # one-shot
    backend._cancel_watchdog()


async def test_repeat_playing_status_confirms_only_once(cast_mock, fresh_supervisor):
    from app.output.chromecast import ChromecastBackend, _AdvanceListener
    sup, timers, rec = fresh_supervisor
    backend = ChromecastBackend()
    backend._loop = asyncio.get_running_loop()
    backend._confirm_token = sup.on_dispatched(make_track())
    status = MagicMock()
    status.player_state = "PLAYING"
    status.idle_reason = None
    status.current_time = 1.0
    listener = _AdvanceListener(backend)
    listener.new_media_status(status)
    listener.new_media_status(status)
    await asyncio.sleep(0)
    rec.assert_called_once()


async def test_buffering_status_does_not_confirm(cast_mock, fresh_supervisor):
    """BUFFERING is pre-playback (R15's extension state), not confirmation."""
    from app.output.chromecast import ChromecastBackend, _AdvanceListener
    sup, timers, rec = fresh_supervisor
    backend = ChromecastBackend()
    backend._loop = asyncio.get_running_loop()
    token = sup.on_dispatched(make_track())
    backend._confirm_token = token
    status = MagicMock()
    status.player_state = "BUFFERING"
    status.idle_reason = None
    status.current_time = 0.0
    _AdvanceListener(backend).new_media_status(status)
    await asyncio.sleep(0)
    rec.assert_not_called()
    assert backend._confirm_token == token     # still awaiting confirmation


async def test_stale_playing_status_confirms_nothing_after_supersede(cast_mock, fresh_supervisor):
    """A late PLAYING for a superseded dispatch names a stale token — the
    supervisor ignores it (skip-during-confirmation-window shape)."""
    from app.output.chromecast import ChromecastBackend, _AdvanceListener
    sup, timers, rec = fresh_supervisor
    backend = ChromecastBackend()
    backend._loop = asyncio.get_running_loop()
    backend._confirm_token = sup.on_dispatched(make_track())
    sup.on_dispatched(make_track())            # a newer dispatch supersedes
    status = MagicMock()
    status.player_state = "PLAYING"
    status.idle_reason = None
    status.current_time = 1.0
    _AdvanceListener(backend).new_media_status(status)
    await asyncio.sleep(0)
    rec.assert_not_called()


# ── duration watchdog (U2) ────────────────────────────────────────────────────

async def test_watchdog_fires_advance_when_no_eos(cast_mock):
    """A hung stream that never reports any terminal status still advances
    WHEN THE DEVICE IS REACHABLE: the watchdog fires after duration + grace
    and the U2 probe confirms the receiver is alive (today's behavior)."""
    from unittest.mock import patch as _patch
    from app.output.chromecast import ChromecastBackend
    advance_called = []

    async def advance():
        advance_called.append(True)

    backend = ChromecastBackend(advance_cb=advance)
    backend._is_playing = True
    backend._play_token = 7
    backend._cast = _make_cc()                 # socket_client.is_connected truthy

    with _patch("app.output.chromecast.asyncio.sleep", AsyncMock(return_value=None)):
        await backend._watchdog(7, 1000)

    assert advance_called


async def test_watchdog_unreachable_device_reports_outage_not_advance(
    cast_mock, fresh_supervisor
):
    """U2's R15/R16 fork: watchdog expiry with the device UNREACHABLE reports
    outage-suspected to the supervisor — the queue holds, no advance, no
    consumed item (the origin party incident's exact mechanism)."""
    from unittest.mock import patch as _patch
    from app.output.chromecast import ChromecastBackend
    sup, timers, rec = fresh_supervisor
    outages = []
    sup.add_outage_listener(lambda token, track, reason: outages.append(reason))
    advance_called = []

    async def advance():
        advance_called.append(True)

    backend = ChromecastBackend(advance_cb=advance)
    backend._is_playing = True
    backend._play_token = 7
    backend._cast = None                       # probe → unreachable

    with _patch("app.output.chromecast.asyncio.sleep", AsyncMock(return_value=None)):
        await backend._watchdog(7, 1000)

    assert not advance_called
    assert outages == ["watchdog_unreachable"]
    assert backend._is_playing is False


async def test_watchdog_superseded_during_probe_is_noop(cast_mock):
    """A play() that lands while the watchdog's probe is in flight bumps the
    token — the stale watchdog must neither advance nor report."""
    from unittest.mock import patch as _patch
    from app.output.chromecast import ChromecastBackend
    advance_called = []

    async def advance():
        advance_called.append(True)

    backend = ChromecastBackend(advance_cb=advance)
    backend._is_playing = True
    backend._play_token = 7

    async def probe_and_supersede():
        backend._play_token = 8                # a newer play() superseded us
        return (True, "PLAYING")

    backend.probe_liveness = probe_and_supersede
    with _patch("app.output.chromecast.asyncio.sleep", AsyncMock(return_value=None)):
        await backend._watchdog(7, 1000)

    assert not advance_called
    assert backend._is_playing is True         # the new play() owns the flag


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
    found = [("Living Room TV", "192.168.1.40", 8009, uuid_str, {"fn": "Living Room TV"})]

    with patch("app.state._mdns_port_unavailable", True), \
         patch("app.output.mdns_dbus.discover", AsyncMock(return_value=found)):
        devices = await backend.discover_devices()

    assert [d.id for d in devices] == [uuid_str]
    assert devices[0].name == "Living Room TV"
    assert devices[0].id_format == "uuid"
    # Address cache populated so connect-by-IP (get_chromecast_from_host) works.
    assert backend._dbus_index[uuid_str] == ("Living Room TV", "192.168.1.40", 8009)
    assert backend._dbus_index["192.168.1.40:8009"] == ("Living Room TV", "192.168.1.40", 8009)


async def test_dbus_discover_host_port_fallback_and_socket_absent(cast_mock):
    """No id= TXT → host:port id; mdns_dbus.discover None (socket absent) → []."""
    from app.output.chromecast import ChromecastBackend

    backend = ChromecastBackend()
    found = [("Bedroom", "192.168.1.41", 8009, None, {})]
    with patch("app.output.mdns_dbus.discover", AsyncMock(return_value=found)):
        devices = await backend._dbus_discover()
    assert devices[0].id == "192.168.1.41:8009"
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
    cast_mock["pcc"].Chromecast.return_value = cc

    backend = ChromecastBackend()
    backend._dbus_index[uuid_str] = ("Living Room TV", "192.168.1.10", 8009)

    with patch("app.state._mdns_port_unavailable", True):
        result = backend._sync_connect(uuid_str)

    cast_mock["pcc"].get_chromecasts.assert_not_called()
    cast_mock["pcc"].Chromecast.assert_called_once()
    _ci = cast_mock["pcc"].models.CastInfo.call_args
    assert _ci.args[4] == "192.168.1.10" and _ci.args[5] == 8009
    # cast_type forced to CHROMECAST, not auto-detected 'audio' (2026-08-04 fix)
    assert _ci.args[6] == cast_mock["pcc"].const.CAST_TYPE_CHROMECAST
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
    cast_mock["pcc"].Chromecast.return_value = cc

    backend = ChromecastBackend()
    backend._dbus_index[uuid_str] = ("Living Room TV", "192.168.1.10", 8009)

    result = backend._sync_connect(uuid_str)

    cast_mock["pcc"].Chromecast.assert_called_once()
    _ci = cast_mock["pcc"].models.CastInfo.call_args
    from uuid import UUID
    # UUID parsed from device_id; cast_type forced to CHROMECAST (2026-08-04 fix)
    assert _ci.args[1] == UUID(uuid_str)
    assert _ci.args[6] == cast_mock["pcc"].const.CAST_TYPE_CHROMECAST
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
    cast_mock["pcc"].Chromecast.return_value = cc

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
    cast_mock["pcc"].Chromecast.return_value = cc

    backend = ChromecastBackend()
    backend._dbus_index["192.168.1.10:8009"] = ("Living Room", "192.168.1.10", 8009)

    result = backend._sync_connect("192.168.1.10:8009")

    cast_mock["pcc"].Chromecast.assert_called_once()
    _ci = cast_mock["pcc"].models.CastInfo.call_args
    # legacy host:port device_id -> uuid None; cast_type forced to CHROMECAST
    assert _ci.args[1] is None
    assert _ci.args[4] == "192.168.1.10" and _ci.args[5] == 8009
    assert _ci.args[6] == cast_mock["pcc"].const.CAST_TYPE_CHROMECAST
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
    cast_mock["pcc"].Chromecast.return_value = cc

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
    cast_mock["pcc"].Chromecast.return_value = cc

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


# ── connection LOST + reachability probe (2026-07-11 supervisor plan U2) ─────

async def test_connection_lost_while_playing_reports_outage(cast_mock, fresh_supervisor):
    """Cast socket LOST mid-track is device-level by definition: report
    outage-suspected (R16 re-point), never advance — the classifier's hold
    is what keeps the party queue intact through a 5-minute outage."""
    from app.output.chromecast import ChromecastBackend, _ConnectionListener
    import asyncio as _asyncio
    sup, timers, rec = fresh_supervisor
    outages = []
    sup.add_outage_listener(lambda token, track, reason: outages.append(reason))
    advance_called = []

    async def advance():
        advance_called.append(True)

    backend = ChromecastBackend(advance_cb=advance)
    cc = _make_cc()
    backend._cast = cc
    backend._is_playing = True
    backend._loop = _asyncio.get_running_loop()

    status = MagicMock()
    status.status = "LOST"
    _ConnectionListener(backend, cc).new_connection_status(status)
    await _asyncio.sleep(0)                    # call_soon_threadsafe hop

    assert outages == ["connection_lost"]
    assert not advance_called
    assert backend._is_playing is False


async def test_connection_lost_while_idle_is_noop(cast_mock, fresh_supervisor):
    """A LOST while nothing is playing interrupts nothing — no outage report
    (U3's reconnect loop owns idle losses)."""
    from app.output.chromecast import ChromecastBackend, _ConnectionListener
    import asyncio as _asyncio
    sup, timers, rec = fresh_supervisor
    outages = []
    sup.add_outage_listener(lambda *a: outages.append(a))

    backend = ChromecastBackend()
    cc = _make_cc()
    backend._cast = cc
    backend._is_playing = False
    backend._loop = _asyncio.get_running_loop()

    status = MagicMock()
    status.status = "LOST"
    _ConnectionListener(backend, cc).new_connection_status(status)
    await _asyncio.sleep(0)
    assert outages == []


async def test_connection_lost_while_user_paused_reports_outage(
        cast_mock, fresh_supervisor, monkeypatch):
    """F11: a Cast powered off while USER-PAUSED still interrupts a live
    session — pause() flipped _is_playing False, but a current+paused queue
    means the plan's Paused→OutagePaused edge must fire, not vanish behind
    the playing gate."""
    from app.output.chromecast import ChromecastBackend, _ConnectionListener
    import asyncio as _asyncio
    import app.state as st
    sup, timers, rec = fresh_supervisor
    outages = []
    sup.add_outage_listener(lambda token, track, reason: outages.append(reason))

    fake_qe = MagicMock()
    fake_qe.state.current = object()             # a live (paused) track
    fake_qe.state.is_paused = True
    monkeypatch.setattr(st, "queue_engine", fake_qe)

    backend = ChromecastBackend()
    cc = _make_cc()
    backend._cast = cc
    backend._is_playing = False                  # pause() already flipped it
    backend._loop = _asyncio.get_running_loop()

    status = MagicMock()
    status.status = "LOST"
    _ConnectionListener(backend, cc).new_connection_status(status)
    await _asyncio.sleep(0)

    assert outages == ["connection_lost"]


async def test_connection_lost_no_current_track_is_noop(
        cast_mock, fresh_supervisor, monkeypatch):
    """F11 guard: plain idle (no current track) stays a no-op — nothing is
    being interrupted, whatever the paused flag says."""
    from app.output.chromecast import ChromecastBackend, _ConnectionListener
    import asyncio as _asyncio
    import app.state as st
    sup, timers, rec = fresh_supervisor
    outages = []
    sup.add_outage_listener(lambda *a: outages.append(a))

    fake_qe = MagicMock()
    fake_qe.state.current = None
    fake_qe.state.is_paused = True
    monkeypatch.setattr(st, "queue_engine", fake_qe)

    backend = ChromecastBackend()
    backend._cast = _make_cc()
    backend._is_playing = False
    backend._loop = _asyncio.get_running_loop()

    status = MagicMock()
    status.status = "LOST"
    _ConnectionListener(backend, backend._cast).new_connection_status(status)
    await _asyncio.sleep(0)
    assert outages == []


async def test_connection_lost_after_own_stop_race_still_noop(
        cast_mock, fresh_supervisor, monkeypatch):
    """F11 guard: a LOST racing our own stop/skip (current set, NOT paused,
    _is_playing already False) reports nothing — the playing dispatch owns
    its own signals."""
    from app.output.chromecast import ChromecastBackend, _ConnectionListener
    import asyncio as _asyncio
    import app.state as st
    sup, timers, rec = fresh_supervisor
    outages = []
    sup.add_outage_listener(lambda *a: outages.append(a))

    fake_qe = MagicMock()
    fake_qe.state.current = object()
    fake_qe.state.is_paused = False              # live skip, not user pause
    monkeypatch.setattr(st, "queue_engine", fake_qe)

    backend = ChromecastBackend()
    backend._cast = _make_cc()
    backend._is_playing = False
    backend._loop = _asyncio.get_running_loop()

    status = MagicMock()
    status.status = "LOST"
    _ConnectionListener(backend, backend._cast).new_connection_status(status)
    await _asyncio.sleep(0)
    assert outages == []


async def test_resume_swallows_media_controller_error(cast_mock):
    """F11 (adjacent hardening): resume() gets the same guard as
    pause()/stop() — a gone media session must not raise into the endpoint."""
    from app.output.chromecast import ChromecastBackend
    backend = ChromecastBackend()
    cc = _make_cc()
    cc.media_controller.play.side_effect = RuntimeError("no active session")
    backend._cast = cc
    await backend.resume()                       # must not raise
    assert backend._is_playing is True


async def test_connection_lost_from_stale_cast_ignored(cast_mock, fresh_supervisor):
    """A listener surviving on a superseded cast object (set_device switched
    devices) must not report an outage for the new device."""
    from app.output.chromecast import ChromecastBackend, _ConnectionListener
    import asyncio as _asyncio
    sup, timers, rec = fresh_supervisor
    outages = []
    sup.add_outage_listener(lambda *a: outages.append(a))

    backend = ChromecastBackend()
    old_cc = _make_cc("Old", "abc-001")
    backend._cast = _make_cc("New", "abc-002")   # a different, current cast
    backend._is_playing = True
    backend._loop = _asyncio.get_running_loop()

    status = MagicMock()
    status.status = "LOST"
    _ConnectionListener(backend, old_cc).new_connection_status(status)
    await _asyncio.sleep(0)
    assert outages == []
    assert backend._is_playing is True


async def test_connection_non_lost_statuses_ignored(cast_mock, fresh_supervisor):
    """CONNECTING/CONNECTED/DISCONNECTED are not outage signals in U2 (U3
    wires CONNECTED for re-attach)."""
    from app.output.chromecast import ChromecastBackend, _ConnectionListener
    import asyncio as _asyncio
    sup, timers, rec = fresh_supervisor
    outages = []
    sup.add_outage_listener(lambda *a: outages.append(a))

    backend = ChromecastBackend()
    cc = _make_cc()
    backend._cast = cc
    backend._is_playing = True
    backend._loop = _asyncio.get_running_loop()

    for st in ("CONNECTING", "CONNECTED", "DISCONNECTED", "FAILED"):
        status = MagicMock()
        status.status = st
        _ConnectionListener(backend, cc).new_connection_status(status)
    await _asyncio.sleep(0)
    assert outages == []


# ── connection CONNECTED → supervisor re-attach trigger (plan U3) ─────────────

async def test_connection_restored_during_hold_triggers_reattach(
        cast_mock, fresh_supervisor, monkeypatch):
    """CONNECTED while an outage hold is active is the socket client's own
    re-attach signal (LOST→CONNECTED destroyed the media session): route it
    to the supervisor's single-flight entry."""
    from app.output import session
    from app.output.chromecast import ChromecastBackend, _ConnectionListener
    import asyncio as _asyncio
    monkeypatch.setattr(hold, "_output_hold", True)
    triggers = []
    monkeypatch.setattr(session, "notify_reconnect_trigger",
                        lambda t: triggers.append(t))

    backend = ChromecastBackend()
    cc = _make_cc()
    backend._cast = cc
    backend._loop = _asyncio.get_running_loop()

    status = MagicMock()
    status.status = "CONNECTED"
    _ConnectionListener(backend, cc).new_connection_status(status)
    await _asyncio.sleep(0)
    assert triggers == ["cast_connected"]


async def test_connection_restored_without_hold_is_noop(
        cast_mock, fresh_supervisor, monkeypatch):
    """CONNECTED with nothing held is the initial connect / a routine blip —
    it must not reach the supervisor's re-attach entry."""
    from app.output import session
    from app.output.chromecast import ChromecastBackend, _ConnectionListener
    import asyncio as _asyncio
    monkeypatch.setattr(hold, "_output_hold", False)
    triggers = []
    monkeypatch.setattr(session, "notify_reconnect_trigger",
                        lambda t: triggers.append(t))

    backend = ChromecastBackend()
    cc = _make_cc()
    backend._cast = cc
    backend._loop = _asyncio.get_running_loop()

    status = MagicMock()
    status.status = "CONNECTED"
    _ConnectionListener(backend, cc).new_connection_status(status)
    await _asyncio.sleep(0)
    assert triggers == []


async def test_connection_restored_from_stale_cast_ignored(
        cast_mock, fresh_supervisor, monkeypatch):
    """A CONNECTED from a superseded cast object (manual switch happened)
    must not trigger a re-attach for the new device."""
    from app.output import session
    from app.output.chromecast import ChromecastBackend, _ConnectionListener
    import asyncio as _asyncio
    monkeypatch.setattr(hold, "_output_hold", True)
    triggers = []
    monkeypatch.setattr(session, "notify_reconnect_trigger",
                        lambda t: triggers.append(t))

    backend = ChromecastBackend()
    old_cc = _make_cc("Old", "abc-001")
    backend._cast = _make_cc("New", "abc-002")
    backend._loop = _asyncio.get_running_loop()

    status = MagicMock()
    status.status = "CONNECTED"
    _ConnectionListener(backend, old_cc).new_connection_status(status)
    await _asyncio.sleep(0)
    assert triggers == []


async def test_set_device_registers_connection_listener(cast_mock):
    """set_device wires the U2 connection listener on the freshly connected
    cast so a later LOST reaches the supervisor."""
    from app.output.chromecast import ChromecastBackend, _ConnectionListener
    cc = _make_cc("Device", "abc-001")
    info = _make_cast_info("Device", "00000000-0000-0000-0000-000000000001")
    cast_mock["pcc"].get_chromecast_from_cast_info.return_value = cc

    backend = ChromecastBackend()
    backend._cast_infos["00000000-0000-0000-0000-000000000001"] = info
    backend._browser = MagicMock()
    backend._zconf = MagicMock()

    with patch("app.database.get_setting", AsyncMock(return_value=None)), \
         patch("app.database.set_setting", AsyncMock()):
        await backend.set_device("00000000-0000-0000-0000-000000000001")

    cc.register_connection_listener.assert_called_once()
    listener = cc.register_connection_listener.call_args.args[0]
    assert isinstance(listener, _ConnectionListener)


async def test_probe_liveness_reachable_with_connected_socket(cast_mock):
    """Cast probe semantics (plan KTD): socket/status liveness — connected
    socket_client → reachable, with the receiver's player_state."""
    from app.output.chromecast import ChromecastBackend
    backend = ChromecastBackend()
    cc = _make_cc()
    cc.socket_client.is_connected = True
    cc.media_controller.status.player_state = "BUFFERING"
    backend._cast = cc
    assert await backend.probe_liveness() == (True, "BUFFERING")


async def test_probe_liveness_unreachable_when_socket_down(cast_mock):
    from app.output.chromecast import ChromecastBackend
    backend = ChromecastBackend()
    cc = _make_cc()
    cc.socket_client.is_connected = False
    backend._cast = cc
    reachable, _state = await backend.probe_liveness()
    assert reachable is False


async def test_probe_liveness_no_cast_is_unreachable(cast_mock):
    from app.output.chromecast import ChromecastBackend
    backend = ChromecastBackend()
    assert await backend.probe_liveness() == (False, None)


# ── flow mode (2026-07-11 supervisor plan U10) ────────────────────────────────
# Gapless-on Cast plays ONE server-stitched flow URL: boundaries are the
# advance authority (queue/Now Playing follow the ENCODE clock), while play
# counts key on DEVICE-reported current_time crossing the boundary offsets.
# The FakeFlowSession below is the exact engine surface the backend consumes;
# test_flow_three_tracks_* runs the REAL U9 engine end-to-end.

import contextlib
import inspect
from types import SimpleNamespace

from app.output.flow import FlowBoundary


def _ftrack(tid: str) -> Track:
    t = Track(id=tid, title=f"Song {tid}", artist="A", album="B",
              duration_ms=180000, stream_key=f"/parts/{tid}/f.flac")
    t.container = "flac"
    return t


def _pstatus(player_state, idle_reason=None, current_time=None):
    s = MagicMock()
    s.player_state = player_state
    s.idle_reason = idle_reason
    s.current_time = current_time
    return s


class FakeFlowSession:
    """Duck-typed FlowSession: the exact surface ChromecastBackend consumes
    (listeners, reposition, pause/resume/close, the stitch-timeline mapping),
    with test-driven boundary emission. Offsets are plain milliseconds."""

    def __init__(self, first, start_offset_ms=0, session_id="flowtest"):
        self.first_track = first
        self.start_offset_ms = start_offset_ms
        self.session_id = session_id
        self.closed = False
        self.ended = False
        self.paused = False
        self.pause_calls = 0
        self.resume_calls = 0
        self.repositions = []
        self.boundary_listeners = []
        self.skip_listeners = []
        self.gone_listeners = []
        self.ended_listeners = []
        self.position_ms = 10 ** 9          # encode clock far ahead
        self.boundaries_ms = [(0, first)]   # (offset_ms, track), stitch order

    @property
    def url_path(self):
        return f"/api/stream/flow/{self.session_id}"

    @property
    def content_type(self):
        return "audio/flac"

    def add_boundary_listener(self, cb):
        self.boundary_listeners.append(cb)

    def add_skip_listener(self, cb):
        self.skip_listeners.append(cb)

    def add_consumer_gone_listener(self, cb):
        self.gone_listeners.append(cb)

    def add_ended_listener(self, cb):
        self.ended_listeners.append(cb)

    def pause(self):
        self.pause_calls += 1
        self.paused = True

    def resume(self):
        self.resume_calls += 1
        self.paused = False

    async def close(self):
        self.closed = True

    async def reposition(self, track, offset_ms=0):
        if self.closed or self.ended:
            return False
        self.repositions.append((track, offset_ms))
        return True

    # ── stitch-timeline mapping (the U10 surface) ──
    def track_at(self, ms):
        found = None
        for off, t in self.boundaries_ms:
            if off <= ms:
                found = t
        return found

    def offset_of(self, track):
        out = None
        for off, t in self.boundaries_ms:
            if getattr(t, "id", None) == getattr(track, "id", None):
                out = off
        return out

    def held_offset_from_device_time(self, device_time_s):
        if device_time_s is None:
            return max(0, self.position_ms - 10_000)
        return max(0, min(int(device_time_s * 1000), self.position_ms))

    # ── test driver ──
    async def emit_boundary(self, track, offset_ms, reposition=False):
        self.boundaries_ms.append((offset_ms, track))
        ev = FlowBoundary(track=track,
                          offset_samples=offset_ms * 44100 // 1000,
                          offset_ms=offset_ms, reposition=reposition)
        for cb in list(self.boundary_listeners):
            res = cb(ev)
            if inspect.isawaitable(res):
                await res


@pytest.fixture
def flow_env(cast_mock, monkeypatch):
    """Cast flow-mode harness: gapless ON, a device-reachable stream base,
    and flow.create_flow_session building FakeFlowSessions registered in the
    module registry (so current_flow_session() agrees with the backend)."""
    import app.state as st
    from app.config import settings
    from app.output import flow
    monkeypatch.setattr(st, "_gapless_enabled", True)
    monkeypatch.setattr(settings, "stream_base_url", "http://192.168.1.70")
    created = []

    def fake_create(first, *, start_offset_ms=0, **kw):
        s = FakeFlowSession(first, start_offset_ms=start_offset_ms,
                            session_id=f"fs{len(created) + 1}")
        old = flow._current_session
        flow._current_session = s
        if old is not None and not old.closed:
            old.closed = True  # the registry supersedes (sync for tests)
        created.append(s)
        return s

    monkeypatch.setattr(flow, "create_flow_session", fake_create)
    monkeypatch.setattr(flow, "_current_session", None)
    yield SimpleNamespace(created=created)
    flow._current_session = None


# The canonical queue-wiring helper lives in tests/conftest.py (shared with
# test_output_direct.py and test_output_dlna.py).
from tests.conftest import wire_queue as _wire_flow_queue


async def _flow_play(sup, backend, track, url="http://x/api/stream?key=k",
                     play_recorded=False):
    """Dispatch `track` the way dispatch_play does (token first, then play)."""
    token = sup.on_dispatched(track, play_recorded=play_recorded)
    await backend.play(url, track)
    return token


async def test_flow_load_once_stream_type_and_no_watchdog(flow_env, fresh_supervisor):
    """Gapless on → ONE LOAD of the flow URL (base composed like per-track
    dispatch), BUFFERED-without-duration stream type (the documented knob;
    hardware-confirmed 2026-08-08 — real Linkplay receivers play the open-ended
    chunked FLAC cleanly under BUFFERED), and NO per-track duration watchdog
    (flow liveness = stream consumption + connection status)."""
    from app.output.chromecast import ChromecastBackend
    sup, timers, rec = fresh_supervisor
    cc = _make_cc()
    backend = ChromecastBackend()
    backend._cast = cc
    t1 = _ftrack("t1")

    await _flow_play(sup, backend, t1)

    cc.media_controller.play_media.assert_called_once()
    args = cc.media_controller.play_media.call_args
    assert args[0][0] == "http://192.168.1.70/api/stream/flow/fs1"
    assert args[0][1] == "audio/flac"
    assert args[1]["stream_type"] == "BUFFERED"
    assert backend._watchdog_task is None
    assert backend._duration_ms == 0
    assert backend.is_playing is True
    assert backend._flow_session is flow_env.created[0]
    assert flow_env.created[0].start_offset_ms == 0


async def test_flow_first_playing_confirms_first_track(flow_env, fresh_supervisor):
    """The FIRST PLAYING status confirms the flow's first track through the
    normal U1 token — device time 0 IS the first track's boundary offset."""
    from app.output.chromecast import ChromecastBackend
    sup, timers, rec = fresh_supervisor
    backend = ChromecastBackend()
    backend._cast = _make_cc()
    t1 = _ftrack("t1")
    await _flow_play(sup, backend, t1)
    rec.assert_not_called()                    # dispatch is never proof of audio

    backend._listener.new_media_status(_pstatus("PLAYING", current_time=0.4))
    await _drain()
    rec.assert_called_once()
    assert rec.call_args.args[0].id == "t1"

    backend._listener.new_media_status(_pstatus("PLAYING", current_time=1.4))
    await _drain()
    rec.assert_called_once()                   # one-shot


async def test_flow_boundary_advances_now_counts_at_device_crossing(
        flow_env, fresh_supervisor):
    """The two-phase split (AE5 + counting): a natural boundary advances the
    queue on the ENCODE clock with NO per-track LOAD (the gap source is
    gone), but the play count fires only when DEVICE-reported current_time
    crosses the boundary offset."""
    from app.output.chromecast import ChromecastBackend
    sup, timers, rec = fresh_supervisor
    cc = _make_cc()
    backend = ChromecastBackend()
    backend._cast = cc
    t1, t2 = _ftrack("t1"), _ftrack("t2")
    with contextlib.ExitStack() as stack:
        qe = _wire_flow_queue(stack)
        await qe.append(t1)
        await qe.append(t2)
        item1 = await qe.advance()
        await _flow_play(sup, backend, item1.track)
        backend._listener.new_media_status(_pstatus("PLAYING", current_time=0.5))
        await _drain()
        rec.assert_called_once()               # t1 confirmed

        sess = flow_env.created[0]
        await sess.emit_boundary(t2, 180_000)  # encode clock crosses into t2

        assert qe.state.current.track_id == "t2"           # advance NOW
        assert [i.track_id for i in qe.history] == ["t1"]
        cc.media_controller.play_media.assert_called_once()  # AE5: no re-LOAD
        assert rec.call_count == 1             # NOT counted at encode time

        # Device still inside t1 → no count yet.
        backend._listener.new_media_status(
            _pstatus("PLAYING", current_time=170.0))
        await _drain()
        assert rec.call_count == 1
        # Device crosses t2's offset → the count fires exactly once.
        backend._listener.new_media_status(
            _pstatus("PLAYING", current_time=180.3))
        await _drain()
        assert rec.call_count == 2
        assert rec.call_args.args[0].id == "t2"
        backend._listener.new_media_status(
            _pstatus("PLAYING", current_time=181.0))
        await _drain()
        assert rec.call_count == 2             # one-shot


async def test_flow_skip_repositions_media_session_unchanged(
        flow_env, fresh_supervisor):
    """Skip in flow mode = stitcher reposition: NO LOAD, the dispatch's
    confirm deadline is deferred (the device-buffer lag would misfire it),
    and the count keys on the device crossing the reposition boundary."""
    from app.output.chromecast import ChromecastBackend
    sup, timers, rec = fresh_supervisor
    cc = _make_cc()
    backend = ChromecastBackend()
    backend._cast = cc
    t1, t2 = _ftrack("t1"), _ftrack("t2")
    await _flow_play(sup, backend, t1)
    backend._listener.new_media_status(_pstatus("PLAYING", current_time=0.5))
    await _drain()
    rec.assert_called_once()
    sess = flow_env.created[0]

    token2 = await _flow_play(sup, backend, t2, url="http://x/2")

    assert sess.repositions == [(t2, 0)]
    cc.media_controller.play_media.assert_called_once()    # media session kept
    assert len(flow_env.created) == 1                      # no second stitcher
    assert timers.timers[-1].cancelled                     # deadline deferred
    assert sup._current.deferred is True
    assert backend._confirm_token is None    # a routine PLAYING can't confirm
    assert sess.resume_calls == 0            # non-paused reposition: no resume
    cc.media_controller.play.assert_not_called()

    await sess.emit_boundary(t2, 42_000, reposition=True)
    assert list(backend._flow_pending_counts) == [(42_000, token2, "t2")]

    backend._listener.new_media_status(_pstatus("PLAYING", current_time=42.5))
    await _drain()
    assert rec.call_count == 2
    assert rec.call_args.args[0].id == "t2"


async def test_flow_skip_before_crossing_cancels_pending_count(
        flow_env, fresh_supervisor):
    """No play is EVER counted for audio the listener never heard: a skip
    past an uncrossed boundary cancels its pending count — even though the
    buffered residue still plays for a moment."""
    from app.output.chromecast import ChromecastBackend
    sup, timers, rec = fresh_supervisor
    backend = ChromecastBackend()
    backend._cast = _make_cc()
    t1, t2, t3 = _ftrack("t1"), _ftrack("t2"), _ftrack("t3")
    with contextlib.ExitStack() as stack:
        qe = _wire_flow_queue(stack)
        await qe.append(t1)
        await qe.append(t2)
        item1 = await qe.advance()
        await _flow_play(sup, backend, item1.track)
        backend._listener.new_media_status(_pstatus("PLAYING", current_time=0.5))
        await _drain()
        rec.assert_called_once()               # t1
        sess = flow_env.created[0]

        await sess.emit_boundary(t2, 100_000)  # t2 pending, uncrossed
        assert rec.call_count == 1

        # Skip to t3 BEFORE the device reaches t2's offset.
        await _flow_play(sup, backend, t3, url="http://x/3")
        assert list(backend._flow_pending_counts) == []    # t2 cancelled
        await sess.emit_boundary(t3, 130_000, reposition=True)

        # Device now crosses BOTH offsets: only t3 counts.
        backend._listener.new_media_status(
            _pstatus("PLAYING", current_time=140.0))
        await _drain()
        assert rec.call_count == 2
        assert rec.call_args.args[0].id == "t3"


async def test_flow_toggle_off_teardown_at_next_boundary(
        flow_env, fresh_supervisor, monkeypatch):
    """Toggle off mid-flow: the current track finishes IN flow mode (nothing
    torn down mid-track, no stop()); at the NEXT boundary the session tears
    down and the boundary's track goes to the per-track dispatch — single
    owner (_do_advance) both advances and dispatches, so the advance
    authority reverts atomically with no double-advance."""
    import app.state as st
    from app.output.chromecast import ChromecastBackend
    sup, timers, rec = fresh_supervisor
    advance_calls = []

    async def advance():
        advance_calls.append(True)

    cc = _make_cc()
    backend = ChromecastBackend(advance_cb=advance)
    backend._cast = cc
    t1, t2 = _ftrack("t1"), _ftrack("t2")
    await _flow_play(sup, backend, t1)
    backend._listener.new_media_status(_pstatus("PLAYING", current_time=0.5))
    await _drain()
    sess = flow_env.created[0]

    monkeypatch.setattr(st, "_gapless_enabled", False)
    # Mid-track: the flow keeps playing — nothing happens until the boundary.
    assert backend._flow_session is sess and not sess.closed
    cc.media_controller.stop.assert_not_called()
    assert advance_calls == []

    await sess.emit_boundary(t2, 180_000)      # the teardown boundary

    assert backend._flow_session is None       # detached AT the boundary
    await _drain()                             # decoupled advance + close land
    assert advance_calls == [True]             # exactly one advance owner
    assert sess.closed is True
    cc.media_controller.stop.assert_not_called()  # no audible interruption
    assert rec.call_count == 1                 # boundary listener counted nothing


async def test_flow_toggle_off_advance_survives_pump_cancellation(
        flow_env, fresh_supervisor, monkeypatch):
    """The toggle-off handoff dispatch must not ride the pump task:
    FlowSession.close() (spawned in the same branch) cancels the pump, and
    an advance awaited FROM the pump would be cancelled mid-dispatch — the
    per-track handoff would never fire. The advance runs on its OWN task."""
    import app.state as st
    from app.output.chromecast import ChromecastBackend
    sup, timers, rec = fresh_supervisor
    started, finished = [], []

    async def advance():
        started.append(True)
        for _ in range(5):                     # a real dispatch suspends
            await asyncio.sleep(0)
        finished.append(True)

    backend = ChromecastBackend(advance_cb=advance)
    backend._cast = _make_cc()
    t1, t2 = _ftrack("t1"), _ftrack("t2")
    await _flow_play(sup, backend, t1)
    sess = flow_env.created[0]

    pump_holder = {}

    async def cancelling_close():              # the real close cancels the pump
        sess.closed = True
        t = pump_holder.get("task")
        if t is not None and not t.done():
            t.cancel()

    sess.close = cancelling_close
    monkeypatch.setattr(st, "_gapless_enabled", False)

    async def pump():
        await sess.emit_boundary(t2, 180_000)  # the toggle-off boundary

    pump_holder["task"] = asyncio.get_running_loop().create_task(pump())
    for _ in range(30):
        await asyncio.sleep(0)

    assert started == [True]
    assert finished == [True]                  # completed despite the close
    assert sess.closed is True
    assert backend._flow_session is None


async def test_flow_skip_while_paused_resumes_stitcher_and_receiver(
        flow_env, fresh_supervisor):
    """Skip while flow-paused: the reposition must auto-play like a
    per-track skip does — unfreeze the stitcher's pacing clock AND issue
    the receiver play; otherwise the device stays silent while the UI
    reads playing."""
    from app.output.chromecast import ChromecastBackend
    sup, timers, rec = fresh_supervisor
    cc = _make_cc()
    backend = ChromecastBackend()
    backend._cast = cc
    t1, t2 = _ftrack("t1"), _ftrack("t2")
    await _flow_play(sup, backend, t1)
    sess = flow_env.created[0]

    await backend.pause()
    assert sess.paused is True
    cc.media_controller.play.assert_not_called()

    await _flow_play(sup, backend, t2, url="http://x/2")   # skip while paused

    assert sess.repositions == [(t2, 0)]
    assert sess.resume_calls == 1              # stitcher clock unfrozen
    assert sess.paused is False
    cc.media_controller.play.assert_called_once()          # receiver resumed
    assert backend.is_playing is True


async def test_flow_status_poll_nudges_receiver_and_stops_on_detach(
        flow_env, fresh_supervisor):
    """Counts must not starve on receiver silence: pychromecast 14 has no
    built-in media-status polling and flow mode disarms the watchdog, so
    while pending device-time counts exist a loop-side poll nudges
    update_status; it stops with the session (detach cancels it)."""
    from app.output.chromecast import ChromecastBackend
    sup, timers, rec = fresh_supervisor
    cc = _make_cc()
    calls = []
    cc.media_controller.update_status = lambda: calls.append(True)
    backend = ChromecastBackend()
    backend._flow_poll_interval_s = 0          # injectable: no real sleeps
    backend._cast = cc
    t1, t2 = _ftrack("t1"), _ftrack("t2")
    with contextlib.ExitStack() as stack:
        qe = _wire_flow_queue(stack)
        await qe.append(t1)
        await qe.append(t2)
        item1 = await qe.advance()
        await _flow_play(sup, backend, item1.track)
        assert backend._flow_poll_timer is None     # ledger empty — no poll

        sess = flow_env.created[0]
        await sess.emit_boundary(t2, 180_000)       # uncrossed pending count
        assert backend._flow_poll_timer is not None  # poll armed

        async def _until(cond):
            while not cond():
                await asyncio.sleep(0)

        await asyncio.wait_for(_until(lambda: len(calls) >= 2), timeout=5)

        backend._detach_flow()                      # teardown cancels the poll
        assert backend._flow_poll_timer is None
        await _drain()                              # settle any in-flight tick
        n = len(calls)
        for _ in range(20):
            await asyncio.sleep(0)
        assert len(calls) == n                      # poll stopped with the session
        assert backend._flow_poll_timer is None     # a stale tick can't re-arm


async def test_flow_first_track_decode_fail_never_counts_it(
        flow_env, fresh_supervisor):
    """A fresh flow session whose FIRST track fails to decode must not count
    it at the first PLAYING (the audio that starts is track 2): the pending
    LOAD confirmation is withdrawn and the next boundary owns the audible
    track's advance/count (unheard-audio invariant)."""
    import app.state as st
    from app.output.chromecast import ChromecastBackend
    sup, timers, rec = fresh_supervisor
    backend = ChromecastBackend()
    backend._cast = _make_cc()
    t1, t2 = _ftrack("t1"), _ftrack("t2")
    with contextlib.ExitStack() as stack:
        qe = _wire_flow_queue(stack)
        skipped = AsyncMock()
        stack.enter_context(patch.object(st, "_emit_track_skipped", skipped))
        await qe.append(t1)
        await qe.append(t2)
        item1 = await qe.advance()
        await _flow_play(sup, backend, item1.track)
        sess = flow_env.created[0]

        for cb in sess.skip_listeners:              # t1 fails to decode
            await cb(t1, "decode failed")
        assert backend._confirm_token is None       # confirmation withdrawn
        skipped.assert_awaited_once()

        backend._listener.new_media_status(         # first PLAYING = t2's audio
            _pstatus("PLAYING", current_time=0.1))
        await _drain()
        rec.assert_not_called()                     # t1 never counted

        await sess.emit_boundary(t2, 0)             # t2 contributes from offset 0
        assert qe.state.current.track_id == "t2"
        backend._listener.new_media_status(
            _pstatus("PLAYING", current_time=0.3))
        await _drain()
        rec.assert_called_once()                    # t2 counts exactly once
        assert rec.call_args.args[0].id == "t2"


async def test_flow_natural_end_stale_session_hop_noops(
        flow_env, fresh_supervisor):
    """A natural-end hop that lands AFTER a fresh session replaced the one
    it was captured for must no-op — never fire the NEW session's ledger or
    tear it down (the session identity guard)."""
    from app.output.chromecast import ChromecastBackend
    sup, timers, rec = fresh_supervisor
    advance_calls = []

    async def advance():
        advance_calls.append(True)

    backend = ChromecastBackend(advance_cb=advance)
    backend._cast = _make_cc()
    t1 = _ftrack("t1")
    await _flow_play(sup, backend, t1)
    live = flow_env.created[0]
    from app.output.chromecast import _PendingCount
    with backend._flow_lock:
        backend._flow_pending_counts.append(_PendingCount(1000, 41, "t1"))

    stale = FakeFlowSession(t1, session_id="stale")
    stale.ended = True
    await backend._flow_natural_end(stale)     # hop landed after a fresh LOAD

    assert backend._flow_session is live       # nothing torn down
    assert list(backend._flow_pending_counts) == [(1000, 41, "t1")]  # ledger intact
    assert advance_calls == []
    rec.assert_not_called()


async def test_flow_idle_error_with_ended_session_is_natural_end(
        flow_env, fresh_supervisor):
    """IDLE(ERROR) with the session ended IS the final EOS: strict receivers
    can't distinguish a clean chunked-stream close from a drop, and the
    session is over either way — the remaining crossings fire and the flow
    converges through the advance path, never the outage route (which would
    discard the final counts)."""
    import app.state as st
    from app.output.chromecast import ChromecastBackend
    sup, timers, rec = fresh_supervisor
    outages = []
    sup.add_outage_listener(lambda token, track, reason: outages.append(reason))
    advance_calls = []

    async def advance():
        advance_calls.append(True)

    backend = ChromecastBackend(advance_cb=advance)
    backend._cast = _make_cc()
    t1, t2 = _ftrack("t1"), _ftrack("t2")
    with contextlib.ExitStack() as stack:
        qe = _wire_flow_queue(stack)
        await qe.append(t1)
        await qe.append(t2)
        item1 = await qe.advance()
        await _flow_play(sup, backend, item1.track)
        backend._listener.new_media_status(_pstatus("PLAYING", current_time=0.5))
        await _drain()
        sess = flow_env.created[0]
        await sess.emit_boundary(t2, 8_000)    # short final track, uncrossed
        sess.ended = True                      # queue exhausted server-side

        backend._listener.new_media_status(
            _pstatus("IDLE", idle_reason="ERROR"))
        await _drain()

        assert outages == []                   # never routed as an outage
        assert rec.call_count == 2             # the final crossing WAS heard
        assert rec.call_args.args[0].id == "t2"
        assert advance_calls == [True]         # converged via the advance path
        assert backend._flow_session is None
        assert sess.closed is True


async def test_flow_seek_repositions_current_and_maps_track_position(
        flow_env, fresh_supervisor, monkeypatch):
    """Seek within the current track = reposition(current, offset): no
    receiver seek, no snapshot re-anchor (device stream time is continuous);
    the reposition boundary re-keys the decode start offset so get_position
    maps stitch position → TRACK position."""
    import time as _t
    import app.state as st
    from app.output.chromecast import ChromecastBackend
    sup, timers, rec = fresh_supervisor
    cc = _make_cc()
    backend = ChromecastBackend()
    backend._cast = cc
    t1 = _ftrack("t1")
    await _flow_play(sup, backend, t1)
    sess = flow_env.created[0]
    fake_qe = MagicMock()
    fake_qe.state.current = SimpleNamespace(track=t1, play_recorded=False)
    monkeypatch.setattr(st, "queue_engine", fake_qe)

    await backend.seek(30_000)

    assert sess.repositions == [(t1, 30_000)]
    cc.media_controller.seek.assert_not_called()

    await sess.emit_boundary(t1, 55_000, reposition=True)
    assert backend._flow_track_starts == {55_000: 30_000}

    backend._pos_snapshot_ms = 56_000          # device stream time 56s
    backend._pos_snapshot_at = _t.monotonic()
    backend._is_playing = False                # freeze the elapsed estimate
    assert await backend.get_position() == 31_000   # 56s - 55s + 30s start


async def test_flow_get_position_is_track_relative(flow_env, fresh_supervisor):
    """Now Playing progress shows TRACK position, not stream position."""
    import time as _t
    from app.output.chromecast import ChromecastBackend
    sup, timers, rec = fresh_supervisor
    backend = ChromecastBackend()
    backend._cast = _make_cc()
    t1, t2 = _ftrack("t1"), _ftrack("t2")
    await _flow_play(sup, backend, t1)
    sess = flow_env.created[0]
    sess.boundaries_ms.append((90_000, t2))
    backend._is_playing = False

    backend._pos_snapshot_ms = 95_000          # device inside t2
    backend._pos_snapshot_at = _t.monotonic()
    assert await backend.get_position() == 5_000

    backend._pos_snapshot_ms = 85_000          # device still inside t1
    backend._pos_snapshot_at = _t.monotonic()
    assert await backend.get_position() == 85_000


async def test_flow_pause_resume_freeze_encode_clock_with_receiver(
        flow_env, fresh_supervisor):
    """Native pause/resume stay in sync with the stitcher's pacing clock —
    otherwise the encode side keeps leading through a pause and the resume
    skips ahead by the buffered audio."""
    from app.output.chromecast import ChromecastBackend
    sup, timers, rec = fresh_supervisor
    cc = _make_cc()
    backend = ChromecastBackend()
    backend._cast = cc
    await _flow_play(sup, backend, _ftrack("t1"))
    sess = flow_env.created[0]

    await backend.pause()
    cc.media_controller.pause.assert_called_once()
    assert sess.pause_calls == 1
    assert backend.is_playing is False

    await backend.resume()
    cc.media_controller.play.assert_called_once()
    assert sess.resume_calls == 1
    assert backend.is_playing is True


async def test_flow_connection_lost_captures_mapped_offset_and_tears_down(
        flow_env, fresh_supervisor, monkeypatch):
    """Connection LOST mid-flow: the held offset comes from DEVICE-reported
    position mapped through the stitch timeline (track-relative), captured
    BEFORE the session tears down; the outage routes as connection_lost."""
    import time as _t
    import asyncio as _asyncio
    import app.state as st
    from app.output.chromecast import ChromecastBackend, _ConnectionListener
    sup, timers, rec = fresh_supervisor
    outages = []
    sup.add_outage_listener(lambda token, track, reason: outages.append(reason))
    cc = _make_cc()
    backend = ChromecastBackend()
    backend._cast = cc
    backend._loop = _asyncio.get_running_loop()
    t1, t2 = _ftrack("t1"), _ftrack("t2")
    await _flow_play(sup, backend, t1)
    sess = flow_env.created[0]
    sess.boundaries_ms.append((90_000, t2))
    fake_qe = MagicMock()
    fake_qe.state.current = SimpleNamespace(track=t2, play_recorded=False)
    monkeypatch.setattr(st, "queue_engine", fake_qe)
    backend._pos_snapshot_ms = 95_000          # device 5s into t2
    backend._pos_snapshot_at = _t.monotonic()

    status = MagicMock()
    status.status = "LOST"
    _ConnectionListener(backend, cc).new_connection_status(status)
    await _drain()

    assert outages == ["connection_lost"]
    assert backend._flow_session is None
    assert sess.closed is True                 # never two stitchers
    # The hold hook consumes the stash exactly once (track-relative 5s).
    assert backend.capture_held_position_ms() == 5_000
    assert backend.capture_held_position_ms() is None   # per-track fallthrough


async def test_flow_capture_device_behind_boundary_clock_holds_at_zero(
        flow_env, fresh_supervisor, monkeypatch):
    """The boundary clock leads the audio: when the device is still inside a
    track Now Playing already advanced past, the held front never audibly
    started — hold it at 0:00 (its pending count was cancelled, so the
    resume still counts it exactly once, R19)."""
    import time as _t
    import app.state as st
    from app.output.chromecast import ChromecastBackend
    sup, timers, rec = fresh_supervisor
    backend = ChromecastBackend()
    backend._cast = _make_cc()
    t1, t2 = _ftrack("t1"), _ftrack("t2")
    await _flow_play(sup, backend, t1)
    sess = flow_env.created[0]
    sess.boundaries_ms.append((90_000, t2))
    fake_qe = MagicMock()
    fake_qe.state.current = SimpleNamespace(track=t2, play_recorded=False)
    monkeypatch.setattr(st, "queue_engine", fake_qe)
    backend._pos_snapshot_ms = 85_000          # device still inside t1
    backend._pos_snapshot_at = _t.monotonic()

    assert backend.capture_held_position_ms() == 0


async def test_flow_resume_recreates_session_at_held_offset_no_recount(
        flow_env, fresh_supervisor):
    """Outage resume in flow mode: play() consumes the supervisor-primed held
    offset into create_flow_session(start_offset_ms=…) + re-LOADs the flow
    URL; resume_seek is a NO-OP (position-resume fully server-controlled);
    the R19 mark keeps the play uncounted; position maps with the start
    offset folded in."""
    import time as _t
    from app.output.chromecast import ChromecastBackend
    sup, timers, rec = fresh_supervisor
    cc = _make_cc()
    backend = ChromecastBackend()
    backend._cast = cc
    t2 = _ftrack("t2")

    backend.prime_resume_offset(5_000)         # the supervisor's held position
    await _flow_play(sup, backend, t2, play_recorded=True)

    sess = flow_env.created[0]
    assert sess.start_offset_ms == 5_000
    cc.media_controller.play_media.assert_called_once()   # the re-LOAD
    assert backend._flow_track_starts == {0: 5_000}

    await backend.resume_seek(5_000)           # supervisor's seek → no-op
    assert sess.repositions == []
    cc.media_controller.seek.assert_not_called()

    backend._listener.new_media_status(_pstatus("PLAYING", current_time=0.2))
    await _drain()
    rec.assert_not_called()                    # R19: never counted twice

    backend._pos_snapshot_ms = 3_000
    backend._pos_snapshot_at = _t.monotonic()
    backend._is_playing = False
    assert await backend.get_position() == 8_000   # 3s device + 5s start


@pytest.mark.parametrize("idle_reason", ["FINISHED", "ERROR"])
async def test_flow_receiver_idle_routes_outage_not_advance(
        flow_env, fresh_supervisor, monkeypatch, idle_reason):
    """Per-track terminal states are SUPPRESSED as advance authority in flow
    mode (no track boundaries exist device-side): a terminal IDLE mid-flow is
    a receiver hiccup → outage-suspected, never advance. The classifier then
    RECOVERS it (hold + auto-resume at position) rather than skipping — see
    test_classify_flow_receiver_transient_recovers_not_skips in test_output_session."""
    import app.state as st
    from app.output.chromecast import ChromecastBackend
    sup, timers, rec = fresh_supervisor
    outages = []
    sup.add_outage_listener(lambda token, track, reason: outages.append(reason))
    advance_calls = []

    async def advance():
        advance_calls.append(True)

    backend = ChromecastBackend(advance_cb=advance)
    backend._cast = _make_cc()
    t1 = _ftrack("t1")
    await _flow_play(sup, backend, t1)
    sess = flow_env.created[0]
    fake_qe = MagicMock()
    fake_qe.state.current = SimpleNamespace(track=t1, play_recorded=False)
    monkeypatch.setattr(st, "queue_engine", fake_qe)

    backend._listener.new_media_status(_pstatus("IDLE", idle_reason=idle_reason))
    await _drain()

    assert outages == ["flow_receiver_idle"]
    assert advance_calls == []
    assert backend._flow_session is None
    assert sess.closed is True
    assert backend.is_playing is False


async def test_flow_idle_cancelled_interrupted_still_ignored(
        flow_env, fresh_supervisor):
    """Self-induced idle reasons stay non-signals in flow mode too."""
    from app.output.chromecast import ChromecastBackend
    sup, timers, rec = fresh_supervisor
    outages = []
    sup.add_outage_listener(lambda *a: outages.append(a))
    backend = ChromecastBackend()
    backend._cast = _make_cc()
    await _flow_play(sup, backend, _ftrack("t1"))

    for reason in ("CANCELLED", "INTERRUPTED"):
        backend._listener.new_media_status(_pstatus("IDLE", idle_reason=reason))
    await _drain()
    assert outages == []
    assert backend._flow_session is not None   # flow untouched


async def test_flow_consumer_gone_reports_ambiguous_outage(
        flow_env, fresh_supervisor, monkeypatch):
    """Consumer-gone grace expiry → outage-SUSPECTED with a deliberately
    ambiguous reason (the socket may still be CONNECTED — the classifier's
    probe is the R15 tie-breaker); the dead session tears down so a resume
    or skip LOADs fresh. A stale session's callback is ignored (= the
    re-bind-within-grace case never fires the listener at all)."""
    import app.state as st
    from app.output.chromecast import ChromecastBackend
    sup, timers, rec = fresh_supervisor
    outages = []
    sup.add_outage_listener(lambda token, track, reason: outages.append(reason))
    backend = ChromecastBackend()
    backend._cast = _make_cc()
    t1 = _ftrack("t1")
    await _flow_play(sup, backend, t1)
    sess = flow_env.created[0]
    fake_qe = MagicMock()
    fake_qe.state.current = SimpleNamespace(track=t1, play_recorded=False)
    monkeypatch.setattr(st, "queue_engine", fake_qe)

    stale = FakeFlowSession(t1, session_id="stale")
    backend._flow_consumer_gone(stale)         # not the live session → no-op
    await _drain()
    assert outages == []

    backend._flow_consumer_gone(sess)
    await _drain()
    assert outages == ["flow_consumer_gone"]
    assert backend._flow_session is None
    assert sess.closed is True


async def test_flow_ended_waits_for_receiver_then_converges_idle(
        flow_env, fresh_supervisor, monkeypatch):
    """Natural exhaustion: the encode-side ended event tears nothing down
    (the receiver is still draining the audible tail — an uncrossed final
    boundary must still count); the receiver's IDLE(FINISHED) is the flow's
    real final EOS — it fires the remaining crossings and converges through
    the same advance path a final per-track EOS takes."""
    import app.state as st
    from app.output.chromecast import ChromecastBackend
    sup, timers, rec = fresh_supervisor
    advance_calls = []

    async def advance():
        advance_calls.append(True)

    backend = ChromecastBackend(advance_cb=advance)
    backend._cast = _make_cc()
    t1, t2 = _ftrack("t1"), _ftrack("t2")
    with contextlib.ExitStack() as stack:
        qe = _wire_flow_queue(stack)
        await qe.append(t1)
        await qe.append(t2)
        item1 = await qe.advance()
        await _flow_play(sup, backend, item1.track)
        backend._listener.new_media_status(_pstatus("PLAYING", current_time=0.5))
        await _drain()
        sess = flow_env.created[0]
        await sess.emit_boundary(t2, 8_000)    # short final track, uncrossed

        sess.ended = True                      # queue exhausted server-side
        for cb in sess.ended_listeners:
            await cb()
        assert backend._flow_session is sess   # nothing torn down yet
        assert sess.closed is False            # the tail keeps draining
        assert advance_calls == []

        # The receiver drains the stream to its end.
        backend._listener.new_media_status(
            _pstatus("IDLE", idle_reason="FINISHED"))
        await _drain()

        assert rec.call_count == 2             # the final crossing WAS heard
        assert rec.call_args.args[0].id == "t2"
        assert advance_calls == [True]         # converge via the advance path
        assert backend._flow_session is None
        assert backend.is_playing is False


async def test_flow_stop_closes_session(flow_env, fresh_supervisor):
    from app.output.chromecast import ChromecastBackend
    sup, timers, rec = fresh_supervisor
    cc = _make_cc()
    backend = ChromecastBackend()
    backend._cast = cc
    await _flow_play(sup, backend, _ftrack("t1"))
    sess = flow_env.created[0]

    await backend.stop()

    assert sess.closed is True
    assert backend._flow_session is None
    cc.media_controller.stop.assert_called_once()
    assert backend.is_playing is False


async def test_flow_set_device_closes_session_immediately(flow_env, fresh_supervisor):
    """Device switch = immediate flow teardown (the stitcher belongs to the
    OLD device's media session) — the same-backend device change never calls
    stop(), so set_device owns it."""
    from app.output.chromecast import ChromecastBackend
    sup, timers, rec = fresh_supervisor
    cc = _make_cc("Living Room", "abc-123")
    info = _make_cast_info("Living Room", "00000000-0000-0000-0000-000000000001")
    backend = ChromecastBackend()
    backend._cast = cc
    backend._cast_infos["00000000-0000-0000-0000-000000000001"] = info
    backend._browser = MagicMock()
    backend._zconf = MagicMock()
    await _flow_play(sup, backend, _ftrack("t1"))
    sess = flow_env.created[0]

    import app.output.chromecast as cc_mod
    with patch.object(cc_mod.pychromecast, "get_chromecast_from_cast_info",
                      return_value=_make_cc("Living Room", "abc-999")), \
         patch("app.database.get_setting", AsyncMock(return_value=None)), \
         patch("app.database.set_setting", AsyncMock()):
        await backend.set_device("00000000-0000-0000-0000-000000000001")

    assert sess.closed is True
    assert backend._flow_session is None


async def test_flow_skip_listener_pops_failed_front_and_emits_skip(
        flow_env, fresh_supervisor):
    """The engine awaits the skip listener BEFORE re-resolving the lookahead:
    the failed queue front must be popped (or the spin guard ends the flow),
    and the R22 TrackSkippedEvent fires."""
    import app.state as st
    from app.output.chromecast import ChromecastBackend
    sup, timers, rec = fresh_supervisor
    backend = ChromecastBackend()
    backend._cast = _make_cc()
    t1, t2 = _ftrack("t1"), _ftrack("t2")
    with contextlib.ExitStack() as stack:
        qe = _wire_flow_queue(stack)
        skipped = AsyncMock()
        stack.enter_context(patch.object(st, "_emit_track_skipped", skipped))
        await qe.append(t1)
        await qe.append(t2)
        item1 = await qe.advance()
        await _flow_play(sup, backend, item1.track)
        sess = flow_env.created[0]

        for cb in sess.skip_listeners:
            await cb(t2, "decode failed")

        assert [i.track_id for i in qe.queue] == []   # front popped
        skipped.assert_awaited_once()
        assert skipped.await_args.args[0] is t2


async def test_flow_degrades_to_per_track_without_stream_base(
        cast_mock, fresh_supervisor, monkeypatch):
    """Gapless on but no STREAM_BASE_URL/BIND_HOST → no device-reachable flow
    URL exists: degrade to per-track dispatch (today's behavior) instead of
    dead air."""
    import app.state as st
    from app.config import settings
    from app.output.chromecast import ChromecastBackend
    sup, timers, rec = fresh_supervisor
    monkeypatch.setattr(st, "_gapless_enabled", True)
    monkeypatch.setattr(settings, "stream_base_url", "")
    monkeypatch.setattr(settings, "bind_host", "0.0.0.0")
    cc = _make_cc()
    backend = ChromecastBackend()
    backend._cast = cc
    t1 = _ftrack("t1")

    await _flow_play(sup, backend, t1, url="http://plex.local/file.flac")

    assert backend._flow_session is None
    args = cc.media_controller.play_media.call_args
    assert args[0][0] == "http://plex.local/file.flac"   # per-track LOAD
    assert "stream_type" not in args[1]
    assert backend._watchdog_task is not None            # per-track watchdog


async def test_backend_exposes_no_arm_next(cast_mock):
    """The U6 arming orchestrator gates on hasattr(backend, "arm_next") —
    Cast flow mode has no device-side arming (the stitcher's lookahead owns
    the next track), so the orchestrator must naturally no-op for Cast."""
    from app.output.chromecast import ChromecastBackend
    backend = ChromecastBackend()
    assert not hasattr(backend, "arm_next")
    assert not hasattr(backend, "revoke_next")


async def test_flow_three_tracks_real_engine_single_load_one_count_each(
        cast_mock, fresh_supervisor, monkeypatch):
    """Integration on the REAL U9 engine (fixture PCM, unbounded run-ahead):
    gapless on + Cast active → ONE flow LOAD; three queue tracks play with
    boundary-driven advances on the ENCODE clock and exactly ONE play count
    each, fired at DEVICE-time crossings; the receiver's IDLE(FINISHED) after
    the finalized stream converges through the advance path."""
    import app.state as st
    from app.config import settings
    from app.output import flow
    from app.output.chromecast import ChromecastBackend
    from tests.test_flow_stream import (FakePCMDecoder, PassthroughEncoder,
                                        make_pcm)
    sup, timers, rec = fresh_supervisor
    monkeypatch.setattr(st, "_gapless_enabled", True)
    monkeypatch.setattr(settings, "stream_base_url", "http://192.168.1.70")
    monkeypatch.setattr(flow, "_current_session", None)

    pcm = {"t1": make_pcm(44100, 1), "t2": make_pcm(44100, 2),
           "t3": make_pcm(22050, 3)}          # 1s + 1s + 0.5s of stitch audio
    real_create = flow.create_flow_session

    def fake_create(first, *, start_offset_ms=0, **kw):
        async def resolver(track):
            return (track.id, {})

        async def next_fn(prev):
            return st.effective_next_track()  # real lookahead, no DB reads

        return real_create(
            first, start_offset_ms=start_offset_ms,
            decoder_factory=lambda src, hdrs, off: FakePCMDecoder(
                pcm[src], offset_ms=off),
            encoder_factory=lambda fmt, sr, ch: PassthroughEncoder(),
            source_resolver=resolver, next_track_fn=next_fn,
            run_ahead_s=1e9)

    monkeypatch.setattr(flow, "create_flow_session", fake_create)
    advance_calls = []

    async def advance():
        advance_calls.append(True)

    cc = _make_cc()
    backend = ChromecastBackend(advance_cb=advance)
    backend._cast = cc
    t1, t2, t3 = _ftrack("t1"), _ftrack("t2"), _ftrack("t3")
    with contextlib.ExitStack() as stack:
        qe = _wire_flow_queue(stack)
        await qe.append(t1)
        await qe.append(t2)
        await qe.append(t3)
        item1 = await qe.advance()
        await _flow_play(sup, backend, item1.track)
        sess = flow.current_flow_session()
        cc.media_controller.play_media.assert_called_once()

        backend._listener.new_media_status(_pstatus("PLAYING", current_time=0.1))
        for _ in range(400):                   # let the pump stitch everything
            await asyncio.sleep(0)
            if sess.ended:
                break
        assert sess.ended
        assert sess.offset_of(t2) == 1000
        assert sess.offset_of(t3) == 2000
        # Boundary-clock advances happened with exactly ONE LOAD (AE5: the
        # album-track boundary emits no per-track LOAD — the gap source is gone).
        assert qe.state.current.track_id == "t3"
        assert [i.track_id for i in qe.history] == ["t2", "t1"]
        cc.media_controller.play_media.assert_called_once()
        await _drain()
        rec.assert_called_once()               # only t1 confirmed so far
        assert rec.call_args.args[0].id == "t1"

        # Device-time crossings: t2 counts at 1.0s, t3 at 2.0s — never before.
        backend._listener.new_media_status(_pstatus("PLAYING", current_time=0.9))
        await _drain()
        assert rec.call_count == 1
        backend._listener.new_media_status(_pstatus("PLAYING", current_time=1.2))
        await _drain()
        assert rec.call_count == 2
        assert rec.call_args.args[0].id == "t2"
        backend._listener.new_media_status(_pstatus("PLAYING", current_time=2.1))
        await _drain()
        assert rec.call_count == 3
        assert rec.call_args.args[0].id == "t3"

        # The receiver drains the finalized stream → the flow's final EOS.
        backend._listener.new_media_status(
            _pstatus("IDLE", idle_reason="FINISHED"))
        await _drain()
        assert advance_calls == [True]
        assert backend._flow_session is None
        assert rec.call_count == 3             # one count each, nothing extra


async def test_flow_toggle_off_real_engine_clean_teardown(
        cast_mock, fresh_supervisor, monkeypatch, caplog):
    """Toggle-off against the REAL engine: the boundary listener detaches
    synchronously and closes out-of-band, so the pump winds down cleanly
    (no 'encoder died' ERROR from tripping over its own teardown) and the
    per-track dispatch fires exactly once, at the boundary."""
    import logging as _logging
    import app.state as st
    from app.config import settings
    from app.output import flow
    from app.output.chromecast import ChromecastBackend
    from tests.test_flow_stream import (FakePCMDecoder, PassthroughEncoder,
                                        make_pcm)
    sup, timers, rec = fresh_supervisor
    monkeypatch.setattr(st, "_gapless_enabled", True)
    monkeypatch.setattr(settings, "stream_base_url", "http://192.168.1.70")
    monkeypatch.setattr(flow, "_current_session", None)

    pcm = {"t1": make_pcm(44100, 1), "t2": make_pcm(44100, 2)}
    decs = {}
    real_create = flow.create_flow_session

    def fake_create(first, *, start_offset_ms=0, **kw):
        def dec_factory(src, hdrs, off):
            d = FakePCMDecoder(pcm[src], offset_ms=off,
                               block_after=(88_200 if src == "t1" else None))
            decs[src] = d
            return d

        async def resolver(track):
            return (track.id, {})

        async def next_fn(prev):
            return st.effective_next_track()

        return real_create(
            first, start_offset_ms=start_offset_ms,
            decoder_factory=dec_factory,
            encoder_factory=lambda fmt, sr, ch: PassthroughEncoder(),
            source_resolver=resolver, next_track_fn=next_fn,
            run_ahead_s=1e9)

    monkeypatch.setattr(flow, "create_flow_session", fake_create)
    advance_calls = []

    async def advance():
        advance_calls.append(True)

    backend = ChromecastBackend(advance_cb=advance)
    backend._cast = _make_cc()
    t1, t2 = _ftrack("t1"), _ftrack("t2")
    with contextlib.ExitStack() as stack:
        qe = _wire_flow_queue(stack)
        await qe.append(t1)
        await qe.append(t2)
        item1 = await qe.advance()
        await _flow_play(sup, backend, item1.track)
        sess = flow.current_flow_session()
        for _ in range(200):                   # pump stalls mid-t1 (blocked)
            await asyncio.sleep(0)
            if decs.get("t1") is not None and decs["t1"].blocked.is_set():
                break
        assert decs["t1"].blocked.is_set()

        monkeypatch.setattr(st, "_gapless_enabled", False)  # mid-track flip
        assert advance_calls == []             # nothing until the boundary
        assert backend._flow_session is sess

        with caplog.at_level(_logging.ERROR, logger="app.output.flow"):
            decs["t1"].release()               # t1 finishes → t2 boundary
            for _ in range(400):
                await asyncio.sleep(0)
                if advance_calls and sess.closed:
                    break

        assert advance_calls == [True]         # dispatch exactly at the boundary
        assert backend._flow_session is None
        assert sess.closed is True
        assert [r for r in caplog.records if r.levelno >= _logging.ERROR] == []


# ── FakeFlowSession ↔ FlowSession attribute parity (2026-07-12 review C10) ────

def test_fake_flow_session_covers_backend_consumed_flow_surface():
    """Every public method/property the BACKEND calls on a flow session must
    exist on BOTH FakeFlowSession and the real FlowSession — an engine method
    the backend starts consuming that is silently missing from the fake must
    fail HERE at test time, never ship green against a stale fake. The
    consumed surface is scraped from app/output/chromecast.py's source
    (``sess.<attr>``, ``._flow_session.<attr>`` and ``getattr(sess, "<attr>",
    …)`` usages), so a new call site extends the checked set automatically.

    Documented finding: ``FlowSession.set_device_epoch_offset`` is NOT part
    of this surface — the backend never calls it on the resume path (an
    outage resume creates a FRESH session via
    ``create_flow_session(start_offset_ms=…)``, whose stitch origin is the
    device's new time zero, so no epoch rebase is needed); only
    tests/test_flow_stream.py exercises it directly."""
    import re
    from app.output import chromecast as cc_mod
    from app.output.flow import FlowSession

    src = inspect.getsource(cc_mod)
    consumed = set(re.findall(r"\bsess\.(\w+)", src))
    consumed |= set(re.findall(r"\b_flow_session\.(\w+)", src))
    consumed |= set(re.findall(r"getattr\(sess,\s*[\"'](\w+)[\"']", src))
    assert consumed, "no flow-session usages found — the source scrape went stale"

    fake = FakeFlowSession(_ftrack("t1"))
    missing_on_fake = sorted(a for a in consumed if not hasattr(fake, a))
    # FlowSession is checked at class level (methods + properties both live
    # there); it is never instantiated here — its ctor builds a real encoder.
    missing_on_real = sorted(a for a in consumed if not hasattr(FlowSession, a))
    assert missing_on_fake == [], (
        f"FakeFlowSession lacks backend-consumed attrs: {missing_on_fake}")
    assert missing_on_real == [], (
        f"FlowSession lacks backend-consumed attrs: {missing_on_real}")
