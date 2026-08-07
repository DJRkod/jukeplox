"""Tests for DlnaBackend — async-upnp-client is fully mocked."""

import asyncio
import logging
from datetime import timedelta
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call

from app.plex.models import Track


def make_track(container="flac") -> Track:
    t = Track(id="t1", title="Song", artist="A", album="B", duration_ms=180000,
              stream_key="/parts/1/f." + container)
    t.container = container
    return t


async def _dmr_set_transport_uri_signature(media_url: str, media_title: str, meta_data=None) -> None:
    """Mirror of `async_upnp_client.profiles.dlna.DmrDevice.async_set_transport_uri`.
    Wired in as the `side_effect` of the mocked method so a production-code
    call that passes the wrong arg count (e.g. four positionals) raises
    TypeError here, instead of being silently swallowed by a bare AsyncMock."""
    return None


async def _dmr_update_signature(do_ping: bool = True) -> None:
    """Mirror of `async_upnp_client.profiles.dlna.DmrDevice.async_update`.
    Production code calls `async_update()` to refresh state, then reads
    `transport_state` (a property). Wiring this as the `side_effect` of
    `async_update` means a production-code call that uses a non-existent
    method name (the prior `async_get_transport_info` bug — see
    docs/solutions/best-practices/mock-mask-third-party-signatures.md)
    fails the test rather than going green against a phantom method."""
    return None


async def _dmr_seek_rel_time_signature(time) -> None:
    """Mirror of `async_upnp_client.profiles.dlna.DmrDevice.async_seek_rel_time`.
    Library signature accepts a `timedelta`; enforce that here so any
    future bad call (e.g. passing raw ms as int) raises in tests rather
    than silently succeeding against a bare AsyncMock and only failing
    on real hardware. Same mock-mask discipline as
    docs/solutions/best-practices/mock-mask-third-party-signatures.md."""
    if not isinstance(time, timedelta):
        raise TypeError(
            f"async_seek_rel_time expects timedelta, got {type(time).__name__}"
        )
    return None


def make_dmr():
    dmr = MagicMock()
    dmr.async_set_transport_uri = AsyncMock(side_effect=_dmr_set_transport_uri_signature)
    dmr.async_play = AsyncMock()
    dmr.async_pause = AsyncMock()
    dmr.async_stop = AsyncMock()
    dmr.async_seek_rel_time = AsyncMock(side_effect=_dmr_seek_rel_time_signature)
    dmr.async_set_volume_level = AsyncMock()
    dmr.async_update = AsyncMock(side_effect=_dmr_update_signature)
    # transport_state is a real property on DmrDevice — tests that care
    # about EOS detection mutate it directly on this MagicMock or via the
    # async_update side_effect to simulate renderer state changes.
    dmr.transport_state = "PLAYING"
    dmr.async_subscribe_services = AsyncMock()
    # Per-action SOAP mocks reachable via _action("AVT", "<Name>"). The
    # DLNA backend bypasses the library's async_seek_rel_time / async_stop
    # helpers because both gate on a stale CurrentTransportActions cache
    # and silently no-op on Linkplay-family renderers; instead it calls
    # the raw Action object's async_call(...). Tests assert against these
    # per-action mocks rather than the bare async_seek_rel_time / async_stop
    # helpers so the bypass is pinned.
    dmr._seek_action = MagicMock()
    dmr._seek_action.async_call = AsyncMock()
    dmr._stop_action = MagicMock()
    dmr._stop_action.async_call = AsyncMock()
    # Plan U8 per-action mocks: SetNextAVTransportURI (arm + empty-URI
    # revoke) and GetPositionInfo (the CurrentTrackURI boundary watch) also
    # ride the direct _action path. The guarded library helper
    # async_set_next_transport_uri gates on the same stale
    # CurrentTransportActions cache as async_stop/async_seek_rel_time and
    # must NEVER be awaited — tests pin await_count == 0.
    dmr.async_set_next_transport_uri = AsyncMock()
    dmr._setnext_action = MagicMock()
    dmr._setnext_action.async_call = AsyncMock()
    dmr._position_action = MagicMock()
    dmr._position_action.async_call = AsyncMock(return_value={})

    def _action_side_effect(service, name):
        if service == "AVT" and name == "Seek":
            return dmr._seek_action
        if service == "AVT" and name == "Stop":
            return dmr._stop_action
        if service == "AVT" and name == "SetNextAVTransportURI":
            return dmr._setnext_action
        if service == "AVT" and name == "GetPositionInfo":
            return dmr._position_action
        return MagicMock()

    dmr._action = MagicMock(side_effect=_action_side_effect)
    return dmr


class _NoopSession:
    """Stand-in for aiohttp.ClientSession used by discover_devices,
    set_device, and probe_device.

    discover_devices opens the session via ``async with``. set_device
    and probe_device use it directly and call ``close()`` themselves.
    The class supports both shapes so a single class can stand in for
    every caller. ``closed`` flips to True on ``close()`` so tests can
    assert lifecycle correctness without requiring real aiohttp on a
    Windows test env that doesn't have the dependency installed."""

    def __init__(self):
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def close(self):
        self.closed = True


def _make_aiohttp_mock():
    """Build a faux `aiohttp` module exposing a `ClientSession` factory.
    DlnaBackend.set_device and probe_device both call
    `aiohttp.ClientSession()` directly, but aiohttp isn't installed on
    the Windows test env — so we patch the module-level symbol with a
    mock whose constructor returns a fresh `_NoopSession` per call."""
    aiohttp_mock = MagicMock()
    aiohttp_mock.ClientSession = MagicMock(side_effect=lambda: _NoopSession())
    return aiohttp_mock


@pytest.fixture
def dlna_mock():
    """Patch async-upnp-client so DlnaBackend can be imported anywhere.

    play() no longer uses a fixed sleep between SetAVTransportURI and
    Play — it calls async_update() instead (WMP-trace pattern). The mock
    DmrDevice's async_update is fast, so no patching needed for speed."""
    with patch("app.output.dlna._DLNA_AVAILABLE", True), \
         patch("app.output.dlna._create_discovery_session", lambda: _NoopSession()), \
         patch("app.output.dlna.aiohttp", _make_aiohttp_mock(), create=True):
        yield


# ── _didl_metadata helper ─────────────────────────────────────────────────────

def test_didl_metadata_structure():
    from app.output.dlna import _didl_metadata
    xml = _didl_metadata("My Song", "http://plex.local/file.flac", "audio/flac", 200000)
    assert "My Song" in xml
    assert "http://plex.local/file.flac" in xml
    assert "audio/flac" in xml
    assert "DIDL-Lite" in xml
    assert "musicTrack" in xml


def test_didl_duration_formatting():
    from app.output.dlna import _didl_metadata
    xml = _didl_metadata("T", "http://u", "audio/flac", 3661000)  # 1h 1m 1s
    assert "1:01:01" in xml


# ── mime type ─────────────────────────────────────────────────────────────────

def test_mime_type_from_container():
    from app.output.dlna import _mime_type
    assert _mime_type("flac", "http://url/f.flac?token=x") == "audio/flac"
    assert _mime_type("mp3", "http://url") == "audio/mpeg"
    assert _mime_type("aac", "http://url") == "audio/aac"


def test_mime_type_fallback_from_url():
    from app.output.dlna import _mime_type
    assert _mime_type(None, "http://url/track.mp3") == "audio/mpeg"
    assert _mime_type(None, "http://url/track.flac?token=xyz") == "audio/flac"


def test_mime_type_unknown_defaults_to_mpeg():
    from app.output.dlna import _mime_type
    assert _mime_type(None, "http://url/track.xyz") == "audio/mpeg"


def test_mime_type_proxied_ogg_advertises_flac():
    """/api/stream transcodes OGG→FLAC; the DIDL protocolInfo must declare the
    served type (audio/flac), not the source audio/ogg. DLNA renderers tolerate
    the old mismatch by sniffing, but Chromecast does not (2026-06-17) and the
    correct type is required regardless. Route on the part path (stream_key)."""
    from app.output.dlna import _mime_type
    url = "http://192.168.1.50/api/stream?key=k%2Ffile.ogg"
    assert _mime_type(None, url, "/library/parts/1/2/file.ogg") == "audio/flac"


def test_mime_type_direct_ogg_not_transcoded_keeps_ogg():
    """A direct Plex URL (no /api/stream proxy) is not transcoded — keep the
    native audio/ogg type."""
    from app.output.dlna import _mime_type
    url = "http://plex.local/library/parts/1/2/file.ogg?X-Plex-Token=t"
    assert _mime_type(None, url, "/library/parts/1/2/file.ogg") == "audio/ogg"


# ── discovery ─────────────────────────────────────────────────────────────────

async def test_discover_unavailable_returns_empty():
    with patch("app.output.dlna._DLNA_AVAILABLE", False):
        from app.output.dlna import DlnaBackend
        backend = DlnaBackend()
        assert await backend.discover_devices() == []


def _make_search(*responses):
    """Build a fake async_search that replays the given SSDP response dicts
    through the registered callback. Each response is a dict matching the
    headers shape async-upnp-client passes (USN, SERVER, LOCATION, ...)."""
    async def fake_search(callback, **kwargs):
        for r in responses:
            await callback(r)
    return fake_search


def _make_fetch(mapping):
    """Build a fake _fetch_device_description that returns the mapped result
    for each URL. Mapping values are either ``(deviceType, friendlyName)``
    tuples or ``None`` to simulate fetch/parse failure."""
    async def fake_fetch(session, url):
        return mapping.get(url)
    return fake_fetch


_RENDERER_V1 = "urn:schemas-upnp-org:device:MediaRenderer:1"
_RENDERER_V2 = "urn:schemas-upnp-org:device:MediaRenderer:2"
_SERVER = "urn:schemas-upnp-org:device:MediaServer:1"


async def test_discover_happy_path_single_renderer(dlna_mock):
    """AE1: One MediaRenderer with friendlyName → one OutputDevice keyed by
    the SSDP USN, labeled with the friendlyName, with the LOCATION cached
    in _device_locations so set_device can find it later."""
    from app.output.dlna import DlnaBackend

    url = "http://192.168.1.10/desc.xml"
    search = _make_search(
        {"USN": "uuid:device-1", "SERVER": "ignored", "LOCATION": url},
    )
    fetch = _make_fetch({url: (_RENDERER_V1, "Living Room Receiver")})

    with patch("app.output.dlna.async_search", search, create=True), \
         patch("app.output.dlna._fetch_device_description", fetch):
        backend = DlnaBackend()
        devices = await backend.discover_devices()

    assert len(devices) == 1
    assert devices[0].id == "uuid:device-1"
    assert devices[0].name == "Living Room Receiver"
    assert devices[0].backend_type == "dlna"
    assert backend._device_locations["uuid:device-1"] == url


async def test_discover_returns_renderers_in_ssdp_response_order(dlna_mock):
    """Three renderers should appear in the same order their SSDP responses
    arrived — preserves predictability for the picker UI."""
    from app.output.dlna import DlnaBackend

    urls = [
        "http://192.168.1.10/desc.xml",
        "http://192.168.1.20/desc.xml",
        "http://192.168.1.30/desc.xml",
    ]
    search = _make_search(
        {"USN": "uuid:1", "LOCATION": urls[0]},
        {"USN": "uuid:2", "LOCATION": urls[1]},
        {"USN": "uuid:3", "LOCATION": urls[2]},
    )
    fetch = _make_fetch({
        urls[0]: (_RENDERER_V1, "Alpha"),
        urls[1]: (_RENDERER_V1, "Beta"),
        urls[2]: (_RENDERER_V1, "Gamma"),
    })

    with patch("app.output.dlna.async_search", search, create=True), \
         patch("app.output.dlna._fetch_device_description", fetch):
        backend = DlnaBackend()
        devices = await backend.discover_devices()

    assert [d.name for d in devices] == ["Alpha", "Beta", "Gamma"]


async def test_discover_filters_non_renderer(dlna_mock):
    """A UPnP MediaServer that responds to our SSDP target is filtered out
    by the deviceType check on the description XML — the SSDP search target
    isn't authoritative, the deviceType is."""
    from app.output.dlna import DlnaBackend

    url = "http://192.168.1.40/desc.xml"
    search = _make_search({"USN": "uuid:server-1", "LOCATION": url})
    fetch = _make_fetch({url: (_SERVER, "NAS Media Server")})

    with patch("app.output.dlna.async_search", search, create=True), \
         patch("app.output.dlna._fetch_device_description", fetch):
        backend = DlnaBackend()
        devices = await backend.discover_devices()

    assert devices == []


async def test_discover_includes_newer_schema_version(dlna_mock):
    """MediaRenderer:2 (future schema bump) is accepted — the filter uses
    prefix match, not exact `:1` equality."""
    from app.output.dlna import DlnaBackend

    url = "http://192.168.1.10/desc.xml"
    search = _make_search({"USN": "uuid:future", "LOCATION": url})
    fetch = _make_fetch({url: (_RENDERER_V2, "Future Renderer")})

    with patch("app.output.dlna.async_search", search, create=True), \
         patch("app.output.dlna._fetch_device_description", fetch):
        backend = DlnaBackend()
        devices = await backend.discover_devices()

    assert len(devices) == 1
    assert devices[0].name == "Future Renderer"


async def test_discover_mixed_renderer_and_non_renderer(dlna_mock):
    """One MediaRenderer + one MediaServer in the same scan → only the
    renderer survives, and the surviving entry is correctly labeled."""
    from app.output.dlna import DlnaBackend

    url_r = "http://192.168.1.10/desc.xml"
    url_s = "http://192.168.1.40/desc.xml"
    search = _make_search(
        {"USN": "uuid:server", "LOCATION": url_s},
        {"USN": "uuid:renderer", "LOCATION": url_r},
    )
    fetch = _make_fetch({
        url_s: (_SERVER, "NAS"),
        url_r: (_RENDERER_V1, "Speaker"),
    })

    with patch("app.output.dlna.async_search", search, create=True), \
         patch("app.output.dlna._fetch_device_description", fetch):
        backend = DlnaBackend()
        devices = await backend.discover_devices()

    assert len(devices) == 1
    assert devices[0].name == "Speaker"
    assert devices[0].id == "uuid:renderer"


async def test_discover_skips_renderer_without_friendly_name(dlna_mock):
    """A MediaRenderer with no <friendlyName> (or an empty one) is skipped —
    a renderer with no label has nothing useful to show in the picker."""
    from app.output.dlna import DlnaBackend

    url = "http://192.168.1.10/desc.xml"
    search = _make_search({"USN": "uuid:nameless", "LOCATION": url})
    fetch = _make_fetch({url: (_RENDERER_V1, "")})

    with patch("app.output.dlna.async_search", search, create=True), \
         patch("app.output.dlna._fetch_device_description", fetch):
        backend = DlnaBackend()
        devices = await backend.discover_devices()

    assert devices == []


async def test_discover_skips_fetch_failure(dlna_mock):
    """A description fetch that fails (returns None) skips the device with
    no exception bubbling out of discover_devices."""
    from app.output.dlna import DlnaBackend

    url = "http://192.168.1.10/desc.xml"
    search = _make_search({"USN": "uuid:unreachable", "LOCATION": url})
    fetch = _make_fetch({url: None})

    with patch("app.output.dlna.async_search", search, create=True), \
         patch("app.output.dlna._fetch_device_description", fetch):
        backend = DlnaBackend()
        devices = await backend.discover_devices()

    assert devices == []


async def test_discover_empty_when_no_ssdp_responses(dlna_mock):
    """Edge case — SSDP returns zero responses. discover_devices returns
    an empty list, _device_locations is unchanged, no fetch is attempted.
    (AE2's picker-empty-state assertion is delegated to the unified picker
    behavior and not exercised by this backend test.)"""
    from app.output.dlna import DlnaBackend

    search = _make_search()  # no responses
    fetch = AsyncMock()  # should never be called

    with patch("app.output.dlna.async_search", search, create=True), \
         patch("app.output.dlna._fetch_device_description", fetch):
        backend = DlnaBackend()
        before = dict(backend._device_locations)
        devices = await backend.discover_devices()

    assert devices == []
    assert backend._device_locations == before
    fetch.assert_not_called()


async def test_discover_skips_response_without_location(dlna_mock):
    """An SSDP response with no LOCATION header has no description to fetch
    and is dropped before the fetch phase — preserves the existing guard."""
    from app.output.dlna import DlnaBackend

    search = _make_search({"USN": "uuid:no-location", "SERVER": "Mystery"})
    fetch = AsyncMock()  # should not be called

    with patch("app.output.dlna.async_search", search, create=True), \
         patch("app.output.dlna._fetch_device_description", fetch):
        backend = DlnaBackend()
        devices = await backend.discover_devices()

    assert devices == []
    fetch.assert_not_called()


async def test_discover_dedupes_by_location(dlna_mock):
    """A device that announces twice within the SSDP window (same LOCATION)
    only triggers one description fetch and appears once in the results."""
    from app.output.dlna import DlnaBackend

    url = "http://192.168.1.10/desc.xml"
    search = _make_search(
        {"USN": "uuid:dup-1", "LOCATION": url},
        {"USN": "uuid:dup-2", "LOCATION": url},  # same LOCATION
    )
    call_count = 0

    async def counting_fetch(session, fetched_url):
        nonlocal call_count
        call_count += 1
        return (_RENDERER_V1, "Loud Speaker")

    with patch("app.output.dlna.async_search", search, create=True), \
         patch("app.output.dlna._fetch_device_description", counting_fetch):
        backend = DlnaBackend()
        devices = await backend.discover_devices()

    assert call_count == 1
    assert len(devices) == 1


async def test_discover_ignores_non_http_location(dlna_mock):
    """A response carrying a non-HTTP LOCATION (e.g. some malformed
    advertisement) is rejected at the SSDP-response stage. Preserves the
    pre-existing safety check."""
    from app.output.dlna import DlnaBackend

    search = _make_search(
        {"USN": "uuid:weird", "LOCATION": "ftp://192.168.1.10/desc.xml"},
    )
    fetch = AsyncMock()

    with patch("app.output.dlna.async_search", search, create=True), \
         patch("app.output.dlna._fetch_device_description", fetch):
        backend = DlnaBackend()
        devices = await backend.discover_devices()

    assert devices == []
    fetch.assert_not_called()


# ── _fetch_device_description helper ──────────────────────────────────────────

_NS_DESCRIPTION_XML = (
    '<?xml version="1.0"?>'
    '<root xmlns="urn:schemas-upnp-org:device-1-0">'
    '<device>'
    '<deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>'
    '<friendlyName>Kitchen Speaker</friendlyName>'
    '<manufacturer>ACME</manufacturer>'
    '</device>'
    '</root>'
)

_UNNAMESPACED_XML = (
    '<?xml version="1.0"?>'
    '<root>'
    '<device>'
    '<deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>'
    '<friendlyName>Old Stack Renderer</friendlyName>'
    '</device>'
    '</root>'
)


class _FakeResponse:
    """Minimal async-context-manager mimicking an aiohttp ClientResponse."""

    def __init__(self, status: int, body: str = "") -> None:
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def text(self):
        return self._body


class _FakeSession:
    """Minimal session whose ``.get(url, timeout=...)`` returns a configured
    _FakeResponse — sufficient for exercising _fetch_device_description
    without involving real aiohttp networking."""

    def __init__(self, response_or_exception) -> None:
        self._target = response_or_exception

    def get(self, url, timeout=None):
        if isinstance(self._target, Exception):
            raise self._target
        return self._target


async def test_fetch_description_happy_path_namespaced(dlna_mock):
    from app.output.dlna import _fetch_device_description
    session = _FakeSession(_FakeResponse(200, _NS_DESCRIPTION_XML))
    result = await _fetch_device_description(session, "http://x/desc.xml")
    assert result == ("urn:schemas-upnp-org:device:MediaRenderer:1", "Kitchen Speaker")


async def test_fetch_description_tolerates_unnamespaced(dlna_mock):
    """Older or non-conformant DLNA stacks may serve descriptions without
    the canonical namespace declaration. The helper falls through to a
    bare-tag lookup so those still parse."""
    from app.output.dlna import _fetch_device_description
    session = _FakeSession(_FakeResponse(200, _UNNAMESPACED_XML))
    result = await _fetch_device_description(session, "http://x/desc.xml")
    assert result == ("urn:schemas-upnp-org:device:MediaRenderer:1", "Old Stack Renderer")


async def test_fetch_description_non_200_returns_none(dlna_mock):
    from app.output.dlna import _fetch_device_description
    session = _FakeSession(_FakeResponse(404, ""))
    assert await _fetch_device_description(session, "http://x/desc.xml") is None


async def test_fetch_description_timeout_returns_none(dlna_mock):
    from app.output.dlna import _fetch_device_description
    session = _FakeSession(asyncio.TimeoutError())
    assert await _fetch_device_description(session, "http://x/desc.xml") is None


async def test_fetch_description_transport_error_returns_none(dlna_mock):
    from app.output.dlna import _fetch_device_description
    session = _FakeSession(OSError("connection refused"))
    assert await _fetch_device_description(session, "http://x/desc.xml") is None


async def test_fetch_description_malformed_xml_returns_none(dlna_mock):
    from app.output.dlna import _fetch_device_description
    session = _FakeSession(_FakeResponse(200, "<not-xml at all"))
    assert await _fetch_device_description(session, "http://x/desc.xml") is None


async def test_fetch_description_missing_device_element_returns_none(dlna_mock):
    """A well-formed description that has no <device> root child is treated
    as malformed for our purposes — there's nothing to read."""
    from app.output.dlna import _fetch_device_description
    xml = '<root xmlns="urn:schemas-upnp-org:device-1-0"><other/></root>'
    session = _FakeSession(_FakeResponse(200, xml))
    assert await _fetch_device_description(session, "http://x/desc.xml") is None


async def test_fetch_description_missing_friendly_name_returns_empty_string(dlna_mock):
    """A <device> with <deviceType> but no <friendlyName> returns the type
    plus an empty string — the discover_devices caller filters empty names."""
    from app.output.dlna import _fetch_device_description
    xml = (
        '<root xmlns="urn:schemas-upnp-org:device-1-0">'
        '<device>'
        '<deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>'
        '</device>'
        '</root>'
    )
    session = _FakeSession(_FakeResponse(200, xml))
    result = await _fetch_device_description(session, "http://x/desc.xml")
    assert result == ("urn:schemas-upnp-org:device:MediaRenderer:1", "")


# ── describe_renderer (U3 live-discovery: SSDP-alive verification) ────────────

_DESC_URL = "http://192.168.1.77:49152/description.xml"
_DESC_USN = "uuid:wiim-1::urn:schemas-upnp-org:device:MediaRenderer:1"


async def test_describe_renderer_happy_path_caches_location(dlna_mock):
    """An alive's LOCATION that verifies as a renderer yields an
    OutputDevice keyed by the USN (discover_devices' id derivation) and
    feeds _device_locations so set_device/probes can address it."""
    from app.output.dlna import DlnaBackend
    fetch = _make_fetch({_DESC_URL: (_RENDERER_V1, "WiiM Pro")})
    with patch("app.output.dlna._fetch_device_description", fetch):
        backend = DlnaBackend()
        device = await backend.describe_renderer(_DESC_URL, _DESC_USN)
    assert device is not None
    assert device.id == _DESC_USN
    assert device.name == "WiiM Pro"
    assert device.backend_type == "dlna"
    assert backend._device_locations[_DESC_USN] == _DESC_URL


async def test_describe_renderer_without_usn_falls_back_to_location(dlna_mock):
    """Same fallback as discover_devices: no USN → the LOCATION is the id."""
    from app.output.dlna import DlnaBackend
    fetch = _make_fetch({_DESC_URL: (_RENDERER_V1, "WiiM Pro")})
    with patch("app.output.dlna._fetch_device_description", fetch):
        backend = DlnaBackend()
        device = await backend.describe_renderer(_DESC_URL)
    assert device.id == _DESC_URL
    assert backend._device_locations[_DESC_URL] == _DESC_URL


async def test_describe_renderer_rejects_non_renderer(dlna_mock):
    """A MediaServer's alive must not enter the registry — the deviceType
    check is the authoritative filter, exactly as in discover_devices."""
    from app.output.dlna import DlnaBackend
    fetch = _make_fetch({_DESC_URL: (_SERVER, "NAS")})
    with patch("app.output.dlna._fetch_device_description", fetch):
        backend = DlnaBackend()
        assert await backend.describe_renderer(_DESC_URL, _DESC_USN) is None
    assert backend._device_locations == {}


async def test_describe_renderer_rejects_nameless_renderer(dlna_mock):
    from app.output.dlna import DlnaBackend
    fetch = _make_fetch({_DESC_URL: (_RENDERER_V1, "")})
    with patch("app.output.dlna._fetch_device_description", fetch):
        backend = DlnaBackend()
        assert await backend.describe_renderer(_DESC_URL, _DESC_USN) is None
    assert backend._device_locations == {}


async def test_describe_renderer_fetch_failure_returns_none(dlna_mock):
    from app.output.dlna import DlnaBackend
    fetch = _make_fetch({})  # URL unknown → None, the fetch-failed shape
    with patch("app.output.dlna._fetch_device_description", fetch):
        backend = DlnaBackend()
        assert await backend.describe_renderer(_DESC_URL, _DESC_USN) is None


async def test_describe_renderer_unavailable_returns_none():
    with patch("app.output.dlna._DLNA_AVAILABLE", False):
        from app.output.dlna import DlnaBackend
        backend = DlnaBackend()
        assert await backend.describe_renderer(_DESC_URL, _DESC_USN) is None


# ── play ──────────────────────────────────────────────────────────────────────

async def test_play_calls_set_transport_uri_then_play(dlna_mock):
    """play() must call async_set_transport_uri with the real (url, title, didl)
    signature — 3 positional args. The library does NOT accept a separate
    mime arg; passing it raises TypeError. _dmr_set_transport_uri_signature
    enforces the real shape so a regression like that surfaces in unit tests."""
    from app.output.dlna import DlnaBackend
    dmr = make_dmr()
    backend = DlnaBackend()
    backend._dmr = dmr
    await backend.play("http://plex.local/file.flac", make_track("flac"))
    dmr.async_set_transport_uri.assert_awaited_once()
    args = dmr.async_set_transport_uri.call_args[0]
    assert len(args) == 3, (
        f"async_set_transport_uri should be called with 3 positional args "
        f"(url, title, didl); got {len(args)}: {args!r}"
    )
    assert args[0] == "http://plex.local/file.flac"
    assert args[1] == "Song"
    # Third arg is the DIDL XML — non-empty string, references the title.
    assert isinstance(args[2], str) and "Song" in args[2]
    dmr.async_play.assert_awaited_once()
    assert backend.is_playing is True


async def test_play_no_device_raises(dlna_mock):
    from app.output.dlna import DlnaBackend
    backend = DlnaBackend()
    with pytest.raises(RuntimeError):
        await backend.play("http://url", make_track())


async def test_play_cancels_previous_poll(dlna_mock):
    from app.output.dlna import DlnaBackend
    dmr = make_dmr()
    backend = DlnaBackend()
    backend._dmr = dmr
    await backend.play("http://url1", make_track())
    first_task = backend._poll_task
    await backend.play("http://url2", make_track())
    await asyncio.sleep(0)  # allow event loop to finalize cancellation
    assert first_task.cancelled() or first_task.done()


async def test_play_does_not_call_stop_before_set_transport_uri(dlna_mock):
    """play() must NOT call async_stop in the cold-start or track-advance
    sequence. STOPPED→STOPPED is not a defined transition per UPnP
    AVTransport:2 §2.5.1; the spec defines Stop only from PLAYING /
    PAUSED / RECORDING. Empirically, an unconditional Stop call before
    SetAVTransportURI was the cause of the post-EOS track-advance bug
    on WiiM Pro / JBL Charge 5 — the Stop disrupted the renderer's
    internal state and the subsequent SetAVTransportURI was silently
    rejected. This test pins the spec-aligned sequence (no Stop) so a
    future refactor cannot accidentally re-add it.

    The canonical sequence is SetAVTransportURI → 250ms → Play; verified
    against Home Assistant dlna_dmr, Cling reference tests, Sony's
    audio_control_api examples, upmpdcli's WMP trace, and BubbleUPnP."""
    from app.output.dlna import DlnaBackend
    dmr = make_dmr()
    backend = DlnaBackend()
    backend._dmr = dmr
    await backend.play("http://url", make_track())
    method_order = [
        c[0] for c in dmr.mock_calls
        if c[0] in ("async_stop", "async_set_transport_uri", "async_play")
    ]
    assert "async_stop" not in method_order, (
        f"play() must NOT invoke async_stop — see UPnP AVTransport:2 §2.5.1 "
        f"and the WiiM Pro track-advance regression. Observed: {method_order}"
    )
    assert method_order == ["async_set_transport_uri", "async_play"], (
        f"play() must invoke async_set_transport_uri then async_play "
        f"with no other SOAP actions; observed {method_order}"
    )


async def test_play_calls_async_update_between_set_uri_and_play(dlna_mock):
    """play() must call async_update() between SetAVTransportURI and
    Play, matching the Windows Media Player trace documented in
    upmpdcli issue 54.

    async_update internally fires GetTransportInfo +
    GetCurrentTransportActions + GetMediaInfo — the cluster of Get calls
    WMP issues to commit the new URI to the renderer's internal pipeline
    before triggering playback. A renderer that requires being queried
    (not just given time) between SetURI and Play needs this gate; a
    fixed sleep is not equivalent.

    Asserts the exact call order: SetAVTransportURI → async_update →
    Play, with no Stop and no extra SOAP actions interleaved."""
    from app.output.dlna import DlnaBackend
    dmr = make_dmr()
    backend = DlnaBackend()
    backend._dmr = dmr
    await backend.play("http://url", make_track())
    method_order = [
        c[0] for c in dmr.mock_calls
        if c[0] in ("async_stop", "async_set_transport_uri",
                    "async_update", "async_play")
    ]
    assert method_order == ["async_set_transport_uri", "async_update", "async_play"], (
        f"play() must invoke async_set_transport_uri → async_update → "
        f"async_play (no Stop, no extra calls); observed {method_order}"
    )


async def test_play_tolerates_async_update_raise(dlna_mock):
    """If async_update raises (JBL Charge 5 sends malformed DIDL in
    CurrentTrackMetaData, causing ParseError inside the library), play()
    must still proceed to async_play. The async_update is a hint to the
    renderer, not a hard prerequisite. Without this tolerance, every
    JBL track would silently fail to start.

    The Get-based SOAP roundtrips inside async_update usually complete
    before metadata parsing happens; the renderer still gets queried
    even when the library raises."""
    from app.output.dlna import DlnaBackend
    dmr = make_dmr()
    dmr.async_update = AsyncMock(side_effect=Exception("DIDL ParseError"))
    backend = DlnaBackend()
    backend._dmr = dmr
    await backend.play("http://url", make_track())
    dmr.async_set_transport_uri.assert_awaited_once()
    dmr.async_play.assert_awaited_once()
    assert backend.is_playing is True


# ── pause / resume / stop ─────────────────────────────────────────────────────

async def test_pause_sends_action(dlna_mock):
    from app.output.dlna import DlnaBackend
    dmr = make_dmr()
    backend = DlnaBackend()
    backend._dmr = dmr
    backend._is_playing = True
    await backend.pause()
    dmr.async_pause.assert_awaited_once()
    assert backend.is_playing is False


async def test_resume_sends_action(dlna_mock):
    from app.output.dlna import DlnaBackend
    dmr = make_dmr()
    backend = DlnaBackend()
    backend._dmr = dmr
    await backend.resume()
    dmr.async_play.assert_awaited_once()
    assert backend.is_playing is True


async def test_stop_sends_action_and_clears_poll(dlna_mock):
    """stop() must issue the AVT/Stop SOAP action directly via
    dmr._action("AVT", "Stop").async_call(InstanceID=0), bypassing the
    library's async_stop helper. The helper guards on
    _can_transport_action("stop") which checks the cached
    CurrentTransportActions state variable; that cache is populated from
    the STOPPED-state action set ({"play"} on Linkplay) at play() time
    and never refreshes, so the helper silently no-ops mid-playback.
    Symptom: skipping the final track does not stop the music."""
    from app.output.dlna import DlnaBackend
    dmr = make_dmr()
    backend = DlnaBackend()
    backend._dmr = dmr
    await backend.play("http://url", make_track())
    await backend.stop()
    # The library helper async_stop must NOT be invoked — it gates on the
    # stale CurrentTransportActions cache. The raw Action.async_call is
    # the bypass.
    assert dmr.async_stop.await_count == 0, (
        "stop() must NOT call the library's async_stop helper — the helper "
        "gates on a stale CurrentTransportActions cache and silently no-ops "
        "on Linkplay firmware"
    )
    dmr._stop_action.async_call.assert_awaited_once_with(InstanceID=0)
    assert backend.is_playing is False


async def test_stop_succeeds_when_current_transport_actions_cache_excludes_stop(dlna_mock):
    """Regression: when the renderer's cached CurrentTransportActions does
    NOT include "stop" (the bug condition that broke skip-on-empty-queue),
    stop() must still issue the raw AVT/Stop SOAP. The direct-SOAP fix
    bypasses _can_transport_action entirely. Without this bypass, the
    library helper async_stop returns silently and the renderer keeps
    streaming.

    Simulates the bug condition by giving the dmr mock a stale
    _current_transport_actions cache that excludes "stop". The library
    helper async_stop would short-circuit here; the raw _action path
    we now use does not check the cache at all."""
    from app.output.dlna import DlnaBackend
    dmr = make_dmr()
    # Simulate the stale-cache bug condition. The real library reads
    # this via the _current_transport_actions property; setting it as
    # an attribute on the MagicMock shadows the property for our test.
    dmr._current_transport_actions = {"play"}
    backend = DlnaBackend()
    backend._dmr = dmr
    backend._is_playing = True
    await backend.stop()
    dmr._stop_action.async_call.assert_awaited_once_with(InstanceID=0)
    assert backend.is_playing is False


# ── volume ────────────────────────────────────────────────────────────────────

async def test_set_volume_delegates(dlna_mock):
    from app.output.dlna import DlnaBackend
    dmr = make_dmr()
    backend = DlnaBackend()
    backend._dmr = dmr
    await backend.set_volume(0.6)
    dmr.async_set_volume_level.assert_awaited_once_with(0.6)
    assert await backend.get_volume() == pytest.approx(0.6)


async def test_volume_clamped(dlna_mock):
    from app.output.dlna import DlnaBackend
    backend = DlnaBackend()
    await backend.set_volume(2.0)
    assert await backend.get_volume() == 1.0
    await backend.set_volume(-1.0)
    assert await backend.get_volume() == 0.0


# ── seek ──────────────────────────────────────────────────────────────────────

async def test_seek_calls_avt_seek_action_directly(dlna_mock):
    """seek() must issue the AVT/Seek SOAP action directly via
    dmr._action("AVT", "Seek").async_call(InstanceID=0, Unit="REL_TIME",
    Target=<H:M:S>), bypassing the library's async_seek_rel_time helper.
    The helper guards on _can_transport_action("seek") which reads the
    cached CurrentTransportActions state variable; that cache is
    populated from the STOPPED-state action set ({"play"} on Linkplay)
    at play() time and never refreshes, so the helper silently no-ops
    mid-playback. Symptom: seek bar lands cosmetically but the renderer
    keeps playing from its original offset."""
    import time as _time
    from app.output.dlna import DlnaBackend
    dmr = make_dmr()
    backend = DlnaBackend()
    backend._dmr = dmr
    backend._is_playing = True
    backend._play_start = _time.monotonic() - 10
    await backend.seek(45_000)
    # The library helper async_seek_rel_time must NOT be invoked.
    assert dmr.async_seek_rel_time.await_count == 0, (
        "seek() must NOT call the library's async_seek_rel_time helper — "
        "it gates on a stale CurrentTransportActions cache and silently "
        "no-ops on Linkplay firmware"
    )
    # 45_000ms → time_to_str(timedelta(milliseconds=45000)) → "0:0:45"
    dmr._seek_action.async_call.assert_awaited_once_with(
        InstanceID=0, Unit="REL_TIME", Target="0:0:45",
    )


async def test_seek_succeeds_when_current_transport_actions_cache_excludes_seek(dlna_mock):
    """Regression: when the renderer's cached CurrentTransportActions does
    NOT include "seek" (the bug condition that broke the seek bar), seek()
    must still issue the raw AVT/Seek SOAP. The direct-SOAP fix bypasses
    _can_transport_action entirely. Without this bypass, the library
    helper async_seek_rel_time returns silently and the renderer keeps
    playing from its original offset."""
    import time as _time
    from app.output.dlna import DlnaBackend
    dmr = make_dmr()
    # Simulate the stale-cache bug condition.
    dmr._current_transport_actions = {"play"}
    backend = DlnaBackend()
    backend._dmr = dmr
    backend._is_playing = True
    backend._play_start = _time.monotonic() - 5
    await backend.seek(60_000)
    dmr._seek_action.async_call.assert_awaited_once_with(
        InstanceID=0, Unit="REL_TIME", Target="0:1:0",
    )
    # And _play_start IS re-anchored because no exception fired.
    pos = await backend.get_position()
    assert 59_000 <= pos <= 62_000, (
        f"_play_start must be re-anchored on successful seek; got {pos}ms"
    )


async def test_seek_reanchors_play_start(dlna_mock):
    """After a successful seek, get_position() must report ~the new
    offset rather than the pre-seek wall-clock position. Same re-anchor
    pattern as AirPlay (airplay.py:1117-1119): `_play_start =
    time.monotonic() - position_s` so subsequent wall-clock reads land
    at the new offset."""
    import time as _time
    from app.output.dlna import DlnaBackend
    dmr = make_dmr()
    backend = DlnaBackend()
    backend._dmr = dmr
    backend._is_playing = True
    backend._play_start = _time.monotonic() - 5  # pretend 5s into the track
    await backend.seek(90_000)
    pos = await backend.get_position()
    assert 89_000 <= pos <= 92_000, f"expected ~90000ms post-seek, got {pos}"


async def test_seek_no_op_when_dmr_unset(dlna_mock):
    """seek() on a backend with no _dmr (never connected, or post-stop()
    which clears _dmr) must not raise and must not touch any UPnP
    surface. The API endpoint forwards seek requests unconditionally,
    so backend must be defensive."""
    from app.output.dlna import DlnaBackend
    backend = DlnaBackend()
    assert backend._dmr is None
    await backend.seek(10_000)


async def test_seek_tolerates_renderer_failure(dlna_mock):
    """If the renderer rejects the Seek SOAP call (e.g.
    CurrentTransportActions does not include Seek for the current URI,
    or the renderer's MIME doesn't support REL_TIME seek), seek() must
    log and return — never raise into the API layer. _play_start must
    NOT be re-anchored on a failed seek, otherwise get_position() would
    lie about where the renderer actually is.

    Verifies both internal state (_play_start unchanged) AND the
    observable consequence (get_position() still reads the pre-seek
    elapsed time, not the seek target). The observable assertion is
    the load-bearing one: future refactors that re-anchor _play_start
    differently but still produce the right get_position() value pass;
    refactors that diverge get_position() from renderer reality fail."""
    import time as _time
    from app.output.dlna import DlnaBackend
    dmr = make_dmr()
    # Raise on the raw SOAP path — the library helper is bypassed.
    dmr._seek_action.async_call = AsyncMock(
        side_effect=Exception("transport not seekable")
    )
    backend = DlnaBackend()
    backend._dmr = dmr
    backend._is_playing = True
    original_start = _time.monotonic() - 5
    backend._play_start = original_start
    await backend.seek(60_000)  # must not raise
    assert backend._play_start == original_start, (
        "_play_start must NOT be re-anchored when the renderer rejected the seek"
    )
    # Observable consequence: get_position() reads the pre-seek elapsed
    # time (~5000ms), not the seek target (60_000ms).
    pos = await backend.get_position()
    assert 4_500 <= pos <= 5_500, (
        f"get_position() must reflect pre-seek elapsed time (~5000ms) "
        f"when seek failed; got {pos}ms"
    )


async def test_seek_zero_passes_through_to_renderer(dlna_mock):
    """seek(0) must dispatch Target="0:0:0", not silently clamp to a
    nonzero value. Seeking to the start of a track is a common user
    action (restart, replay)."""
    import time as _time
    from app.output.dlna import DlnaBackend
    dmr = make_dmr()
    backend = DlnaBackend()
    backend._dmr = dmr
    backend._is_playing = True
    backend._play_start = _time.monotonic() - 30
    await backend.seek(0)
    dmr._seek_action.async_call.assert_awaited_once_with(
        InstanceID=0, Unit="REL_TIME", Target="0:0:0",
    )


async def test_seek_negative_clamps_to_zero(dlna_mock):
    """seek() must clamp negative position_ms to 0 before dispatching.
    A negative Target would either raise or behave undefined on the
    renderer. The clamp guards the boundary; this test pins it so a
    refactor that drops the `max(0, position_ms)` guard fails."""
    import time as _time
    from app.output.dlna import DlnaBackend
    dmr = make_dmr()
    backend = DlnaBackend()
    backend._dmr = dmr
    backend._is_playing = True
    backend._play_start = _time.monotonic() - 30
    await backend.seek(-5_000)
    dmr._seek_action.async_call.assert_awaited_once_with(
        InstanceID=0, Unit="REL_TIME", Target="0:0:0",
    )


# ── EOS polling ───────────────────────────────────────────────────────────────

async def test_poll_eos_triggers_advance_on_two_consecutive_stopped(dlna_mock):
    """Real end-of-stream: the renderer reports STOPPED on every poll.
    advance fires after the second consecutive STOPPED — one poll's worth
    of latency over the bare-minimum 'fire on first STOPPED' design, but
    that's the cost of being immune to mid-stream transient STOPPED."""
    from app.output.dlna import DlnaBackend, TransportState
    advance_called = []

    async def advance():
        advance_called.append(True)

    dmr = make_dmr()

    async def _update(do_ping: bool = True) -> None:
        dmr.transport_state = TransportState.STOPPED

    dmr.async_update = AsyncMock(side_effect=_update)

    backend = DlnaBackend(advance_cb=advance)
    backend._dmr = dmr
    backend._is_playing = True

    with patch("asyncio.sleep", AsyncMock()):
        await backend._poll_eos()

    assert advance_called
    assert backend.is_playing is False


async def test_poll_eos_natural_advance_resets_play_start(dlna_mock):
    """F7: the natural-EOS advance (2x STOPPED) must drop the _play_start
    wall-clock anchor — otherwise a later position capture (outage entry for
    a track that never started) reads the FINISHED track's elapsed time as
    the new track's position."""
    import time as _time
    from app.output.dlna import DlnaBackend, TransportState

    async def advance():
        pass

    dmr = make_dmr()

    async def _update(do_ping: bool = True) -> None:
        dmr.transport_state = TransportState.STOPPED

    dmr.async_update = AsyncMock(side_effect=_update)

    backend = DlnaBackend(advance_cb=advance)
    backend._dmr = dmr
    backend._is_playing = True
    backend._play_start = _time.monotonic() - 200  # 200s into the track

    with patch("asyncio.sleep", AsyncMock()):
        await backend._poll_eos()

    assert backend._play_start == 0.0


async def test_poll_eos_ignores_transient_stopped(dlna_mock):
    """Regression: WiiM Pro / JBL DLNA renderers transiently report
    CurrentTransportState=STOPPED while still streaming audio. A single
    STOPPED followed by a return to PLAYING must NOT fire advance — that
    bug surfaced as 'track disappears from Now Playing at ~13s while
    audio keeps playing' on real hardware.

    Sequence: STOPPED, PLAYING, PLAYING, STOPPED, PLAYING — the two
    isolated STOPPED states should be ignored. We then return STOPPED
    twice in a row so the test terminates instead of polling forever."""
    from app.output.dlna import DlnaBackend, TransportState
    advance_called = []

    async def advance():
        advance_called.append(True)

    states = iter([
        TransportState.STOPPED,   # transient — ignored
        TransportState.PLAYING,   # back to playing — counter resets
        TransportState.PLAYING,
        TransportState.STOPPED,   # second transient — ignored
        TransportState.PLAYING,   # counter resets again
        TransportState.STOPPED,   # real EOS start
        TransportState.STOPPED,   # confirmed — fire
    ])
    dmr = make_dmr()

    async def _update(do_ping: bool = True) -> None:
        dmr.transport_state = next(states)

    dmr.async_update = AsyncMock(side_effect=_update)

    backend = DlnaBackend(advance_cb=advance)
    backend._dmr = dmr
    backend._is_playing = True

    with patch("asyncio.sleep", AsyncMock()):
        await backend._poll_eos()

    # advance fires exactly once — at the two consecutive STOPPED at the end.
    assert advance_called == [True]
    # The 4th poll into the iteration was the second transient STOPPED;
    # if the bug regresses, advance fires there and the call count is 2+.
    assert dmr.async_update.await_count == 7


async def test_poll_eos_advance_cb_can_call_play_without_self_cancellation(dlna_mock):
    """Regression: _poll_eos awaits advance_cb directly. advance_cb is wired
    to _do_advance which calls DlnaBackend.play(). play() calls _cancel_poll()
    which calls self._poll_task.cancel() — but self._poll_task IS the
    currently-running poll task. The cancel hits at the next await inside
    play() (async_set_transport_uri), CancelledError propagates, the new
    track URI never reaches the renderer.

    Symptom on hardware: 'initial track plays accurately, but advancing
    does not function.' Logs show `play() entry` for the next track but
    no `play() set_transport_uri+async_play succeeded` follow-up — exactly
    the self-cancellation footprint.

    AirPlay handled this same pattern at airplay.py:1320 — clear the task
    ref BEFORE awaiting advance_cb so _cancel_poll has nothing to cancel.

    This test wires an advance_cb that calls backend.play() with a new URL
    and asserts async_set_transport_uri received that URL. If the self-
    cancel returns, set_transport_uri's last call will still be the
    original URL (or won't be called at all)."""
    from app.output.dlna import DlnaBackend, TransportState
    advance_calls = []

    dmr = make_dmr()

    async def _update(do_ping: bool = True) -> None:
        dmr.transport_state = TransportState.STOPPED

    dmr.async_update = AsyncMock(side_effect=_update)

    backend = DlnaBackend()
    backend._dmr = dmr
    backend._is_playing = True

    # The production sequence we're guarding against:
    #   _poll_eos task → await advance_cb() → _do_advance → backend.play()
    #     → _cancel_poll() → self._poll_task.cancel() → CancelledError
    #     fires at the next real await inside play() (async_set_transport_uri)
    # The structural fix mirrors AirPlay's at airplay.py:1331: clear the
    # task ref BEFORE awaiting advance_cb so _cancel_poll has nothing
    # matching to cancel. We assert that structural invariant directly:
    # when advance_cb is entered, self._poll_task must already be None.
    sentinel_task = MagicMock()  # stand-in for the production task ref
    sentinel_task.done.return_value = False
    backend._poll_task = sentinel_task

    poll_task_at_advance_entry = []

    async def advance():
        poll_task_at_advance_entry.append(backend._poll_task)
        advance_calls.append(True)

    backend._advance_cb = advance

    with patch("asyncio.sleep", AsyncMock()):
        await backend._poll_eos()

    assert advance_calls, "advance_cb must fire after two consecutive STOPPED"
    assert poll_task_at_advance_entry == [None], (
        "Before awaiting advance_cb, _poll_eos must clear self._poll_task so "
        "the downstream play() → _cancel_poll() doesn't cancel its own caller. "
        f"Got self._poll_task={poll_task_at_advance_entry[0]!r} at advance_cb entry. "
        "See app/output/airplay.py:1320-1331 for the same fix on the AirPlay path."
    )
    # And the sentinel task must NOT have been canceled — proves
    # _poll_task was cleared before _cancel_poll could touch it.
    sentinel_task.cancel.assert_not_called()


async def test_poll_eos_uses_async_update_not_get_transport_info(dlna_mock):
    """Regression for the production bug where _poll_eos called
    `async_get_transport_info`, a method that does not exist on
    async_upnp_client.profiles.dlna.DmrDevice. The real API is:
        await dmr.async_update()
        state = dmr.transport_state  # property returning TransportState

    Production logs showed every poll raised AttributeError; after 3
    errors the EOS path fired advance_cb at ~15s wall clock, surfacing
    as 'tracks disappear from Now Playing at ~13s'.

    This test wires `async_update` as the supplier (used by production
    code) and intentionally leaves `async_get_transport_info` absent so
    a regression that re-introduces the old method name explodes loudly.
    """
    from app.output.dlna import DlnaBackend, TransportState
    advance_called = []

    async def advance():
        advance_called.append(True)

    dmr = make_dmr()

    async def _update(do_ping: bool = True) -> None:
        dmr.transport_state = TransportState.STOPPED

    dmr.async_update = AsyncMock(side_effect=_update)
    # Intentionally NOT setting dmr.async_get_transport_info — any
    # production code that still calls it will AttributeError on a real
    # MagicMock-with-spec? Actually plain MagicMock auto-creates the
    # attribute, so instead assert that production code calls async_update.
    backend = DlnaBackend(advance_cb=advance)
    backend._dmr = dmr
    backend._is_playing = True

    with patch("asyncio.sleep", AsyncMock()):
        await backend._poll_eos()

    assert advance_called == [True]
    assert dmr.async_update.await_count >= 2, (
        "_poll_eos must call async_update() to refresh transport_state, "
        "not async_get_transport_info() (which does not exist on DmrDevice)"
    )


async def test_poll_eos_parse_error_never_false_advances(dlna_mock):
    """Regression (U1): a renderer that returns malformed DIDL track
    metadata on every poll makes async_update raise ParseError every time.
    The OLD generic `except Exception` counted each ParseError toward the
    3-strike budget and fired advance_cb at ~15s while audio was still
    playing — exactly the 'error fires a lot, including while Chromecast is
    playing' symptom the user reported.

    The narrow ParseError catch must swallow the parse failure WITHOUT
    counting it (the renderer is reachable — this is bad metadata, not a
    connectivity failure) and fall through to read transport_state. Since
    transport_state stays PLAYING here, advance must NEVER fire."""
    import xml.etree.ElementTree as ET
    from app.output.dlna import DlnaBackend, TransportState
    advance_called = []

    async def advance():
        advance_called.append(True)

    dmr = make_dmr()
    dmr.transport_state = TransportState.PLAYING

    async def _update(do_ping: bool = True) -> None:
        # GetTransportInfo leg already refreshed transport_state (PLAYING);
        # the GetPositionInfo metadata leg then blows up on unbound prefix.
        raise ET.ParseError("unbound prefix: line 7, column 0")

    dmr.async_update = AsyncMock(side_effect=_update)

    backend = DlnaBackend(advance_cb=advance)
    backend._dmr = dmr
    backend._is_playing = True

    # The loop only exits on advance (which must not happen) or error — so
    # break out externally after a fixed number of polls and assert no
    # advance fired. Six polls is well past the old 3-strike threshold.
    class _Stop(Exception):
        pass

    n = {"count": 0}

    async def _sleep(*_a, **_k):
        n["count"] += 1
        if n["count"] > 6:
            raise _Stop

    with patch("asyncio.sleep", AsyncMock(side_effect=_sleep)):
        with pytest.raises(_Stop):
            await backend._poll_eos()

    assert advance_called == [], (
        "ParseError on every poll must NOT fire advance_cb — a persistently "
        "malformed renderer is reachable, not failed. If this regresses, the "
        "ParseError is leaking into the 3-strike error path again."
    )
    assert dmr.async_update.await_count == 6
    assert backend.is_playing is True


async def test_poll_eos_parse_error_still_detects_eos(dlna_mock):
    """U1: the ParseError catch must fall through to the transport_state
    read so EOS detection still works on a renderer that sends malformed
    metadata. async_update refreshes transport_state to STOPPED (the
    GetTransportInfo leg) and THEN raises ParseError (the GetPositionInfo
    metadata leg). advance must still fire after two consecutive STOPPED."""
    import xml.etree.ElementTree as ET
    from app.output.dlna import DlnaBackend, TransportState
    advance_called = []

    async def advance():
        advance_called.append(True)

    dmr = make_dmr()

    async def _update(do_ping: bool = True) -> None:
        dmr.transport_state = TransportState.STOPPED
        raise ET.ParseError("unbound prefix: line 7, column 0")

    dmr.async_update = AsyncMock(side_effect=_update)

    backend = DlnaBackend(advance_cb=advance)
    backend._dmr = dmr
    backend._is_playing = True

    with patch("asyncio.sleep", AsyncMock()):
        await backend._poll_eos()

    assert advance_called == [True]
    assert backend.is_playing is False


async def test_poll_eos_three_connectivity_errors_report_outage_not_advance(
    dlna_mock, fresh_supervisor
):
    """U1 kept the narrow ParseError catch from swallowing real connectivity
    failures — those still count toward the 3-strike budget. Supervisor plan
    U2 (R16) re-points what the third strike DOES: three consecutive failed
    transport polls are the reachability probe failing, so the poll reports
    outage-suspected ("poll_errors", device-level → hold) instead of the old
    force-advance that consumed queue items during an outage."""
    from app.output.dlna import DlnaBackend
    sup, timers, rec = fresh_supervisor
    outages = []
    sup.add_outage_listener(lambda token, track, reason: outages.append(reason))
    advance_called = []

    async def advance():
        advance_called.append(True)

    dmr = make_dmr()

    async def _update(do_ping: bool = True) -> None:
        raise ConnectionResetError("renderer dropped the connection")

    dmr.async_update = AsyncMock(side_effect=_update)

    backend = DlnaBackend(advance_cb=advance)
    backend._dmr = dmr
    backend._is_playing = True

    with patch("asyncio.sleep", AsyncMock()):
        await backend._poll_eos()

    assert advance_called == []                # was: force-advance
    assert outages == ["poll_errors"]          # now: outage-suspected → hold
    assert dmr.async_update.await_count == 3   # the 3-strike budget unchanged
    assert backend.is_playing is False
    assert backend._poll_task is None          # loop exited cleanly


async def test_poll_eos_steady_polls_log_at_debug_not_info(dlna_mock, caplog):
    """Log hygiene (2026-06-16): the per-poll EOS status line spammed INFO
    every 5s for an entire track. It must log at DEBUG so a playing track
    emits only transitions (play start, first non-STOPPED, advance) at INFO,
    not one line every poll."""
    from app.output.dlna import DlnaBackend, TransportState

    async def advance():
        pass

    states = iter([
        TransportState.PLAYING, TransportState.PLAYING, TransportState.PLAYING,
        TransportState.STOPPED, TransportState.STOPPED,  # two STOPPED ends the loop
    ])
    dmr = make_dmr()

    async def _update(do_ping: bool = True) -> None:
        dmr.transport_state = next(states)

    dmr.async_update = AsyncMock(side_effect=_update)
    backend = DlnaBackend(advance_cb=advance)
    backend._dmr = dmr
    backend._is_playing = True

    with caplog.at_level(logging.DEBUG, logger="app.output.dlna"):
        with patch("asyncio.sleep", AsyncMock()):
            await backend._poll_eos()

    # The recurring per-poll status line must NOT be INFO.
    info_status = [r for r in caplog.records
                   if r.levelno == logging.INFO and "will fire on >=2" in r.getMessage()]
    assert info_status == [], (
        "per-poll EOS status must log at DEBUG, not INFO — it spammed one line "
        f"every 5s during playback. Got {len(info_status)} INFO line(s)."
    )
    # ...but it must still be emitted at DEBUG (demoted, not deleted).
    debug_status = [r for r in caplog.records
                    if r.levelno == logging.DEBUG and "will fire on >=2" in r.getMessage()]
    assert debug_status, "per-poll EOS status should still be logged at DEBUG"


# ── _on_dlna_event GENA volume callback (U3) ───────────────────────────────────

def _mk_sv(name: str, value):
    sv = MagicMock()
    sv.name = name
    sv.value = value
    return sv


def _mk_service(service_id: str):
    svc = MagicMock()
    svc.service_id = service_id
    return svc


async def test_on_dlna_event_volume_change_broadcasts(dlna_mock):
    """RenderingControl event with Volume=42 → state=0.42 and broadcast.

    Covers AE2 — external volume change reflected in admin client.
    """
    from app.output.dlna import DlnaBackend
    from app.events.bus import manager
    backend = DlnaBackend()
    svc = _mk_service("urn:upnp-org:serviceId:RenderingControl")
    sv = _mk_sv("Volume", 42)

    with patch.object(manager, "broadcast_to_admins", AsyncMock()) as broadcast:
        backend._on_dlna_event(svc, [sv])
        # _on_dlna_event schedules the broadcast via create_task; yield once.
        await asyncio.sleep(0)

    assert backend._volume == pytest.approx(0.42)
    broadcast.assert_awaited_once()
    sent = broadcast.await_args.args[0]
    assert sent.type == "volume_changed"
    assert sent.level == pytest.approx(0.42)


async def test_on_dlna_event_ignores_avtransport(dlna_mock):
    """AVTransport events must not trigger volume broadcasts.

    Scope boundary (plan): DLNA EOS replacement via AVTransport::LastChange
    is a follow-up; for now, only RenderingControl events are handled here.
    """
    from app.output.dlna import DlnaBackend
    from app.events.bus import manager
    backend = DlnaBackend()
    backend._volume = 0.5
    svc = _mk_service("urn:upnp-org:serviceId:AVTransport")
    sv = _mk_sv("TransportState", "PLAYING")

    with patch.object(manager, "broadcast_to_admins", AsyncMock()) as broadcast:
        backend._on_dlna_event(svc, [sv])
        await asyncio.sleep(0)

    assert backend._volume == 0.5  # unchanged
    broadcast.assert_not_called()


async def test_on_dlna_event_rendering_control_without_volume(dlna_mock):
    """RenderingControl event lacking a Volume state variable → no broadcast."""
    from app.output.dlna import DlnaBackend
    from app.events.bus import manager
    backend = DlnaBackend()
    backend._volume = 0.5
    svc = _mk_service("urn:upnp-org:serviceId:RenderingControl")
    sv = _mk_sv("Mute", False)  # not Volume

    with patch.object(manager, "broadcast_to_admins", AsyncMock()) as broadcast:
        backend._on_dlna_event(svc, [sv])
        await asyncio.sleep(0)

    assert backend._volume == 0.5
    broadcast.assert_not_called()


async def test_on_dlna_event_echo_guard_suppresses_within_2s(dlna_mock):
    """A NOTIFY that arrives within 2s of set_volume() is suppressed.

    Mirrors the Chromecast _vol_last_set guard. Without this guard,
    every server-initiated volume write would echo back to the admin
    client via GENA, causing slider snap-back during user drags.
    """
    import time
    from app.output.dlna import DlnaBackend
    from app.events.bus import manager
    backend = DlnaBackend()
    backend._volume = 0.5
    backend._vol_last_set = time.monotonic()  # just set, within 2s window
    svc = _mk_service("urn:upnp-org:serviceId:RenderingControl")
    sv = _mk_sv("Volume", 60)

    with patch.object(manager, "broadcast_to_admins", AsyncMock()) as broadcast:
        backend._on_dlna_event(svc, [sv])
        await asyncio.sleep(0)

    assert backend._volume == 0.5  # state unchanged
    broadcast.assert_not_called()


async def test_on_dlna_event_echo_guard_expires_after_2s(dlna_mock):
    """Same scenario with the guard window expired → broadcast fires."""
    import time
    from app.output.dlna import DlnaBackend
    from app.events.bus import manager
    backend = DlnaBackend()
    backend._volume = 0.5
    backend._vol_last_set = time.monotonic() - 3.0  # outside the window
    svc = _mk_service("urn:upnp-org:serviceId:RenderingControl")
    sv = _mk_sv("Volume", 60)

    with patch.object(manager, "broadcast_to_admins", AsyncMock()) as broadcast:
        backend._on_dlna_event(svc, [sv])
        await asyncio.sleep(0)

    assert backend._volume == pytest.approx(0.6)
    broadcast.assert_awaited_once()


async def test_set_volume_stamps_echo_guard(dlna_mock):
    """set_volume must set _vol_last_set so a follow-up NOTIFY is suppressed."""
    import time
    from app.output.dlna import DlnaBackend
    dmr = make_dmr()
    backend = DlnaBackend()
    backend._dmr = dmr

    before = time.monotonic()
    await backend.set_volume(0.7)
    after = time.monotonic()

    assert before <= backend._vol_last_set <= after


async def test_stop_is_non_destructive(dlna_mock):
    """stop() must NOT clear _dmr, the notify_server, or the
    aiohttp session. Earlier behavior tore all three down, which
    broke the skip flow at admin.py:playback_skip (router.stop() →
    router.play() raised RuntimeError because _dmr was None). The
    cleanup is now handled at the only place that needs it:
    set_device() releases the prior resources before binding a new
    renderer (see test_set_device_tears_down_previous_notify_server).

    Asserts post-stop() invariants:
    - AVT/Stop SOAP was sent (renderer told to stop) via the direct
      _action path — NOT via the library's async_stop helper
    - _dmr is still the same object (NOT None)
    - notify_server is still the same object
    - dlna_session is still the same object
    - is_playing is False
    """
    from app.output.dlna import DlnaBackend
    dmr = make_dmr()
    backend = DlnaBackend()
    backend._dmr = dmr
    notify = MagicMock()
    notify.async_stop_server = AsyncMock()
    backend._notify_server = notify
    session = _NoopSession()
    backend._dlna_session = session
    backend._is_playing = True

    await backend.stop()

    dmr._stop_action.async_call.assert_awaited_once_with(InstanceID=0)
    assert backend._dmr is dmr, "stop() must NOT clear _dmr"
    assert backend._notify_server is notify, (
        "stop() must NOT tear down notify_server"
    )
    assert backend._dlna_session is session, (
        "stop() must NOT close dlna_session"
    )
    notify.async_stop_server.assert_not_awaited()
    assert not session.closed
    assert backend.is_playing is False


async def test_set_device_tears_down_previous_notify_server(dlna_mock):
    """Calling set_device twice must stop the first NotifyServer before starting the second.

    Without this guard, a per-backend AiohttpNotifyServer leaks one bound socket
    per device selection. Plan U3 lists this as an explicit test scenario.
    """
    from app.output.dlna import DlnaBackend
    backend = DlnaBackend()
    backend._device_locations["dev-1"] = "http://192.168.1.50:8000/desc.xml"
    backend._device_locations["dev-2"] = "http://192.168.1.51:8000/desc.xml"

    # First set_device populates _notify_server (via the real instantiate path
    # but with the upstream factory and notify-server stubbed).
    first_notify = MagicMock()
    first_notify.async_start_server = AsyncMock()
    first_notify.async_stop_server = AsyncMock()
    first_notify.event_handler = MagicMock()
    second_notify = MagicMock()
    second_notify.async_start_server = AsyncMock()
    second_notify.async_stop_server = AsyncMock()
    second_notify.event_handler = MagicMock()
    notify_factory = MagicMock(side_effect=[first_notify, second_notify])

    dmr = make_dmr()
    dmr_factory = MagicMock(return_value=dmr)

    fake_upnp_factory = MagicMock()
    fake_upnp_factory.async_create_device = AsyncMock(return_value=MagicMock())

    with patch("app.output.dlna.AiohttpSessionRequester", MagicMock(), create=True), \
         patch("app.output.dlna.UpnpFactory", MagicMock(return_value=fake_upnp_factory), create=True), \
         patch("app.output.dlna.AiohttpNotifyServer", notify_factory, create=True), \
         patch("app.output.dlna.DmrDevice", dmr_factory, create=True), \
         patch("app.database.get_setting", AsyncMock(return_value=None)):
        await backend.set_device("dev-1")
        assert backend._notify_server is first_notify

        await backend.set_device("dev-2")

    # Previous notify server stopped before the new one was started
    first_notify.async_stop_server.assert_awaited_once()
    second_notify.async_start_server.assert_awaited_once()
    assert backend._notify_server is second_notify
    # DmrDevice received the new notify server's event_handler — not None
    second_dmr_call = dmr_factory.call_args_list[-1]
    assert second_dmr_call.kwargs.get("event_handler") is second_notify.event_handler
    # on_event was wired so GENA NOTIFYs reach _on_dlna_event. Bound methods
    # compare equal but not identical (new instance each access), so use ==.
    assert dmr.on_event == backend._on_dlna_event


async def test_set_device_persists_location_as_output_addr(dlna_mock):
    """Supervisor plan U3: a successful set_device persists the renderer's
    LOCATION under output_addr:{device_id} so the startup reconnect and the
    outage retry loop can re-attach with no discovery round (extends the
    Cast/AirPlay cached-address mechanic to DLNA)."""
    import json as _json
    from app.output.dlna import DlnaBackend
    backend = DlnaBackend()
    location = "http://192.168.1.50:8000/desc.xml"
    backend._device_locations["dev-1"] = location

    notify = MagicMock()
    notify.async_start_server = AsyncMock()
    notify.async_stop_server = AsyncMock()
    notify.event_handler = MagicMock()
    dmr = make_dmr()
    fake_upnp_factory = MagicMock()
    fake_upnp_factory.async_create_device = AsyncMock(return_value=MagicMock())
    set_setting = AsyncMock()

    with patch("app.output.dlna.AiohttpSessionRequester", MagicMock(), create=True), \
         patch("app.output.dlna.UpnpFactory", MagicMock(return_value=fake_upnp_factory), create=True), \
         patch("app.output.dlna.AiohttpNotifyServer", MagicMock(return_value=notify), create=True), \
         patch("app.output.dlna.DmrDevice", MagicMock(return_value=dmr), create=True), \
         patch("app.database.get_setting", AsyncMock(return_value=None)), \
         patch("app.database.set_setting", set_setting):
        await backend.set_device("dev-1")

    set_setting.assert_awaited_once_with(
        "output_addr:dev-1", _json.dumps({"location": location}))


async def test_set_device_subscribe_failure_persists_no_output_addr(dlna_mock):
    """A failed set_device must NOT persist an address — the renderer never
    attached, so the cached-address reconnect would seed a dead LOCATION."""
    from app.output.dlna import DlnaBackend
    backend = DlnaBackend()
    backend._device_locations["dev-1"] = "http://192.168.1.50:8000/desc.xml"

    notify = MagicMock()
    notify.async_start_server = AsyncMock()
    notify.async_stop_server = AsyncMock()
    notify.event_handler = MagicMock()
    dmr = MagicMock()
    dmr.async_subscribe_services = AsyncMock(side_effect=RuntimeError("boom"))
    fake_upnp_factory = MagicMock()
    fake_upnp_factory.async_create_device = AsyncMock(return_value=MagicMock())
    set_setting = AsyncMock()

    with patch("app.output.dlna.AiohttpSessionRequester", MagicMock(), create=True), \
         patch("app.output.dlna.UpnpFactory", MagicMock(return_value=fake_upnp_factory), create=True), \
         patch("app.output.dlna.AiohttpNotifyServer", MagicMock(return_value=notify), create=True), \
         patch("app.output.dlna.DmrDevice", MagicMock(return_value=dmr), create=True), \
         patch("app.database.get_setting", AsyncMock(return_value=None)), \
         patch("app.database.set_setting", set_setting):
        with pytest.raises(RuntimeError, match="boom"):
            await backend.set_device("dev-1")

    set_setting.assert_not_awaited()


async def test_set_device_cleans_up_notify_server_on_subscribe_failure(dlna_mock):
    """If async_subscribe_services raises, the started NotifyServer must be torn down.

    Otherwise the bound aiohttp socket leaks with no DMR subscribed, and play()
    raises 'no device selected' until the user re-runs set_device.
    """
    from app.output.dlna import DlnaBackend
    backend = DlnaBackend()
    backend._device_locations["dev-1"] = "http://192.168.1.50:8000/desc.xml"

    notify = MagicMock()
    notify.async_start_server = AsyncMock()
    notify.async_stop_server = AsyncMock()
    notify.event_handler = MagicMock()

    dmr = MagicMock()
    dmr.async_subscribe_services = AsyncMock(side_effect=RuntimeError("subscribe failed"))

    fake_upnp_factory = MagicMock()
    fake_upnp_factory.async_create_device = AsyncMock(return_value=MagicMock())

    with patch("app.output.dlna.AiohttpSessionRequester", MagicMock(), create=True), \
         patch("app.output.dlna.UpnpFactory", MagicMock(return_value=fake_upnp_factory), create=True), \
         patch("app.output.dlna.AiohttpNotifyServer", MagicMock(return_value=notify), create=True), \
         patch("app.output.dlna.DmrDevice", MagicMock(return_value=dmr), create=True), \
         patch("app.database.get_setting", AsyncMock(return_value=None)):
        with pytest.raises(RuntimeError, match="subscribe failed"):
            await backend.set_device("dev-1")

    notify.async_stop_server.assert_awaited_once()
    assert backend._notify_server is None
    assert backend._dmr is None


# ── U2: probe_device picker-facing wrapper ───────────────────────────────────


def _make_upnp_device(service_keys):
    """Construct a stand-in for the UpnpDevice returned by
    UpnpFactory.async_create_device. ``service_keys`` is the list of
    service identifiers the device exposes as keys in `UpnpDevice.services`.

    Real async_upnp_client populates `services` as
    ``{service.service_type: service}`` — e.g.,
    ``urn:schemas-upnp-org:service:AVTransport:1``. Tests should pass
    real-shaped `service_type` keys (with `:1` / `:2` suffix), not the
    older `serviceId` shape — otherwise the mock can mask production
    bugs in the substring match."""
    dev = MagicMock()
    dev.services = {sid: MagicMock(service_id=sid) for sid in service_keys}
    return dev


async def test_probe_device_returns_true_for_realistic_avtransport_service_type(dlna_mock):
    """Regression for the production bug where the substring match was
    `endswith('AVTransport')`. Real DLNA renderers advertise the service
    under `urn:schemas-upnp-org:service:AVTransport:1` (note the `:1`
    suffix). `endswith('AVTransport')` fails against that shape, the
    probe returns False, the picker drops DLNA — matching the live
    symptom on a WiiM Pro and JBL Charge 5.

    Substring match (`'AVTransport' in service_key`) handles the
    canonical `:1`, the newer `:2`, AND legacy `serviceId`-shaped keys."""
    from app.output.dlna import DlnaBackend

    upnp_dev = _make_upnp_device([
        "urn:schemas-upnp-org:service:RenderingControl:1",
        "urn:schemas-upnp-org:service:AVTransport:1",
        "urn:schemas-upnp-org:service:ConnectionManager:1",
    ])

    factory = MagicMock()
    factory.async_create_device = AsyncMock(return_value=upnp_dev)

    backend = DlnaBackend()
    backend._device_locations["dev-1"] = "http://192.168.1.50:8000/desc.xml"

    with patch("app.output.dlna.AiohttpSessionRequester", _RequesterSpy, create=True), \
         patch("app.output.dlna.UpnpFactory", MagicMock(return_value=factory), create=True), \
         patch("app.output.dlna.aiohttp.ClientSession", side_effect=lambda: _NoopSession()):
        result = await backend.probe_device("dev-1")

    assert result is True, (
        "probe_device must recognise the canonical "
        "urn:schemas-upnp-org:service:AVTransport:1 service key. "
        "If this fails the check is using endswith('AVTransport') which only "
        "matches the legacy serviceId shape, not the service_type shape that "
        "UpnpDevice.services is actually keyed by."
    )


async def test_probe_device_returns_true_for_avtransport_version_2(dlna_mock):
    """AVTransport version 2 (newer schema) is also accepted — substring
    match handles forward-compat without needing per-version explicit cases."""
    from app.output.dlna import DlnaBackend

    upnp_dev = _make_upnp_device([
        "urn:schemas-upnp-org:service:AVTransport:2",
    ])
    factory = MagicMock()
    factory.async_create_device = AsyncMock(return_value=upnp_dev)

    backend = DlnaBackend()
    backend._device_locations["dev-1"] = "http://192.168.1.50:8000/desc.xml"

    with patch("app.output.dlna.AiohttpSessionRequester", _RequesterSpy, create=True), \
         patch("app.output.dlna.UpnpFactory", MagicMock(return_value=factory), create=True), \
         patch("app.output.dlna.aiohttp.ClientSession", side_effect=lambda: _NoopSession()):
        assert await backend.probe_device("dev-1") is True


class _RequesterSpy:
    """Spy that enforces the real AiohttpSessionRequester signature so a
    production-code call that drops the `session` argument raises TypeError
    here too. Mirrors async_upnp_client's actual constructor."""

    instances: list = []

    def __init__(self, session, with_sleep=False, timeout=5, http_headers=None):
        self.session = session
        self.closed = False
        type(self).instances.append(self)

    async def close(self):
        self.closed = True


class _SessionSpy:
    """Stand-in for aiohttp.ClientSession that records close() and exposes
    a real flag the test can assert on. Used because aiohttp isn't always
    installed in the local Windows test env (see existing _NoopSession
    fixture at the top of this file)."""

    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


async def test_probe_device_passes_session_to_requester(dlna_mock):
    """Regression for the production bug where probe_device called
    `AiohttpSessionRequester()` without a session, raising TypeError on
    every DLNA probe and silently flipping the verdict to ok=False.

    Patches `aiohttp` at the dlna module level so we can run on a host
    where aiohttp isn't installed. The spy enforces the real
    AiohttpSessionRequester signature — a production call that drops
    the `session` argument raises TypeError before the production code's
    own try/except catches it, propagating to probe_device's outer except
    and returning False.

    Asserts the session is the one we patched in (proves probe_device
    opens its own session and threads it through) and that the session
    is closed when probe_device exits — preventing the session leak that
    motivated the original 'finally close' design."""
    from app.output.dlna import DlnaBackend

    upnp_dev = _make_upnp_device(["urn:upnp-org:serviceId:AVTransport"])
    factory = MagicMock()
    factory.async_create_device = AsyncMock(return_value=upnp_dev)

    _RequesterSpy.instances.clear()
    session_spy = _SessionSpy()
    aiohttp_module = MagicMock(ClientSession=MagicMock(return_value=session_spy))

    backend = DlnaBackend()
    backend._device_locations["dev-1"] = "http://192.168.1.50:8000/desc.xml"

    with patch("app.output.dlna.aiohttp", aiohttp_module, create=True), \
         patch("app.output.dlna.AiohttpSessionRequester", _RequesterSpy, create=True), \
         patch("app.output.dlna.UpnpFactory", MagicMock(return_value=factory), create=True):
        result = await backend.probe_device("dev-1")

    assert result is True
    assert len(_RequesterSpy.instances) == 1
    spy = _RequesterSpy.instances[0]
    # The session passed into AiohttpSessionRequester must be the one we
    # opened — not None, not a default — proving probe_device threaded
    # the lifecycle correctly.
    assert spy.session is session_spy
    # The finally block must close the session before the call returns,
    # otherwise repeated probes leak sockets.
    assert session_spy.closed is True


async def test_probe_device_returns_true_when_avtransport_present(dlna_mock):
    """Happy path: factory returns a device exposing an AVTransport service
    → probe_device returns True. The probe-local aiohttp ClientSession is
    closed in the finally block so no session leaks across probes."""
    from app.output.dlna import DlnaBackend

    upnp_dev = _make_upnp_device([
        "urn:upnp-org:serviceId:RenderingControl",
        "urn:upnp-org:serviceId:AVTransport",
        "urn:upnp-org:serviceId:ConnectionManager",
    ])

    factory = MagicMock()
    factory.async_create_device = AsyncMock(return_value=upnp_dev)

    backend = DlnaBackend()
    backend._device_locations["dev-1"] = "http://192.168.1.50:8000/desc.xml"

    session_holder = []

    def _record_session():
        s = _NoopSession()
        session_holder.append(s)
        return s

    with patch("app.output.dlna.AiohttpSessionRequester", _RequesterSpy, create=True), \
         patch("app.output.dlna.UpnpFactory", MagicMock(return_value=factory), create=True), \
         patch("app.output.dlna.aiohttp.ClientSession", side_effect=_record_session):
        result = await backend.probe_device("dev-1")

    assert result is True
    factory.async_create_device.assert_awaited_once_with("http://192.168.1.50:8000/desc.xml")
    assert len(session_holder) == 1
    assert session_holder[0].closed is True


async def test_probe_device_returns_false_when_avtransport_absent(dlna_mock):
    """A device that's a UPnP MediaServer or similar (no AVTransport in its
    service list) is not a renderer we can stream to. Probe returns False
    even though async_create_device succeeded."""
    from app.output.dlna import DlnaBackend

    upnp_dev = _make_upnp_device([
        "urn:upnp-org:serviceId:ContentDirectory",
        "urn:upnp-org:serviceId:ConnectionManager",
    ])

    factory = MagicMock()
    factory.async_create_device = AsyncMock(return_value=upnp_dev)

    backend = DlnaBackend()
    backend._device_locations["dev-1"] = "http://192.168.1.40:8000/desc.xml"

    session_holder = []

    def _record_session():
        s = _NoopSession()
        session_holder.append(s)
        return s

    with patch("app.output.dlna.AiohttpSessionRequester", _RequesterSpy, create=True), \
         patch("app.output.dlna.UpnpFactory", MagicMock(return_value=factory), create=True), \
         patch("app.output.dlna.aiohttp.ClientSession", side_effect=_record_session):
        result = await backend.probe_device("dev-1")

    assert result is False
    assert session_holder[0].closed is True


async def test_probe_device_returns_false_on_factory_timeout(dlna_mock):
    """async_create_device timing out → False with no exception leaked.
    The probe-local session is still closed so a slow renderer does not
    accumulate session leaks across probes."""
    from app.output.dlna import DlnaBackend

    factory = MagicMock()

    async def slow_create(location):
        await asyncio.sleep(10)  # well past the configured probe timeout
        return _make_upnp_device(["AVTransport"])

    factory.async_create_device = slow_create

    backend = DlnaBackend()
    backend._device_locations["dev-1"] = "http://192.168.1.50:8000/desc.xml"

    session_holder = []

    def _record_session():
        s = _NoopSession()
        session_holder.append(s)
        return s

    with patch("app.output.dlna.AiohttpSessionRequester", _RequesterSpy, create=True), \
         patch("app.output.dlna.UpnpFactory", MagicMock(return_value=factory), create=True), \
         patch("app.output.dlna.aiohttp.ClientSession", side_effect=_record_session), \
         patch("app.output.dlna._DLNA_PROBE_TIMEOUT_S", 0.05):  # tighten for test speed
        result = await backend.probe_device("dev-1")

    assert result is False
    assert session_holder[0].closed is True


async def test_probe_device_returns_false_on_factory_exception(dlna_mock):
    """async_create_device raising (network error, malformed XML, etc.) →
    False; the session is still closed in the finally block."""
    from app.output.dlna import DlnaBackend

    factory = MagicMock()
    factory.async_create_device = AsyncMock(side_effect=RuntimeError("conn refused"))

    backend = DlnaBackend()
    backend._device_locations["dev-1"] = "http://192.168.1.50:8000/desc.xml"

    session_holder = []

    def _record_session():
        s = _NoopSession()
        session_holder.append(s)
        return s

    with patch("app.output.dlna.AiohttpSessionRequester", _RequesterSpy, create=True), \
         patch("app.output.dlna.UpnpFactory", MagicMock(return_value=factory), create=True), \
         patch("app.output.dlna.aiohttp.ClientSession", side_effect=_record_session):
        result = await backend.probe_device("dev-1")

    assert result is False
    assert session_holder[0].closed is True


async def test_probe_device_returns_false_for_unknown_device_id(dlna_mock):
    """device_id not in _device_locations → False without constructing a
    requester. Avoids the LOCATION-URL-of-None path that would otherwise
    blow up in UpnpFactory."""
    from app.output.dlna import DlnaBackend

    backend = DlnaBackend()
    # _device_locations empty.

    sentinel_requester = MagicMock()
    sentinel_requester.close = AsyncMock()
    requester_cls = MagicMock(return_value=sentinel_requester)

    with patch("app.output.dlna.AiohttpSessionRequester", requester_cls, create=True):
        assert await backend.probe_device("unknown") is False

    # No requester constructed because the early-return short-circuited.
    requester_cls.assert_not_called()


async def test_probe_device_returns_false_when_unavailable():
    """When async-upnp-client is not importable (_DLNA_AVAILABLE False),
    probes return False without raising."""
    from app.output.dlna import DlnaBackend

    with patch("app.output.dlna._DLNA_AVAILABLE", False):
        backend = DlnaBackend()
        backend._device_locations["dev-1"] = "http://192.168.1.50:8000/desc.xml"
        assert await backend.probe_device("dev-1") is False


# ── confirmed-start signal (2026-07-11 supervisor plan U1) ────────────────────

async def test_play_captures_confirm_token(dlna_mock, fresh_supervisor):
    """play() captures the supervisor's per-dispatch token; accepted SOAP calls
    alone (SetAVTransportURI/Play returning) must not count the play."""
    from app.output.dlna import DlnaBackend
    sup, timers, rec = fresh_supervisor
    dmr = make_dmr()
    backend = DlnaBackend()
    backend._dmr = dmr
    token = sup.on_dispatched(make_track())    # state dispatches before backend.play
    await backend.play("http://url", make_track())
    assert backend._confirm_token == token
    rec.assert_not_called()                    # command accepted ≠ audio
    backend._cancel_poll()


async def test_first_non_stopped_poll_emits_confirmed_start(dlna_mock, fresh_supervisor):
    """The first poll reading the transport OUT of STOPPED confirms the
    dispatch — once. The run ends with two consecutive STOPPED (EOS) so the
    poll terminates; the count stays at exactly one."""
    from app.output.dlna import DlnaBackend, TransportState
    sup, timers, rec = fresh_supervisor
    states = iter([
        TransportState.STOPPED,        # renderer not started yet — no confirm
        TransportState.TRANSITIONING,  # first non-STOPPED → confirmed-start
        TransportState.PLAYING,        # already confirmed — no re-emit
        TransportState.STOPPED,        # EOS begins
        TransportState.STOPPED,        # confirmed EOS — poll exits
    ])
    dmr = make_dmr()

    async def _update(do_ping: bool = True) -> None:
        dmr.transport_state = next(states)

    dmr.async_update = AsyncMock(side_effect=_update)
    backend = DlnaBackend(advance_cb=AsyncMock())
    backend._dmr = dmr
    backend._is_playing = True
    backend._confirm_token = sup.on_dispatched(make_track())

    with patch("asyncio.sleep", AsyncMock()):
        await backend._poll_eos()

    rec.assert_called_once()
    assert backend._confirm_token is None      # one-shot


async def test_stopped_polls_never_confirm(dlna_mock, fresh_supervisor):
    """A renderer that never leaves STOPPED (the silent SetAVTransportURI
    rejection) must NOT confirm — this is exactly the phantom play the
    chokepoint exists to stop counting."""
    from app.output.dlna import DlnaBackend, TransportState
    sup, timers, rec = fresh_supervisor
    dmr = make_dmr()

    async def _update(do_ping: bool = True) -> None:
        dmr.transport_state = TransportState.STOPPED

    dmr.async_update = AsyncMock(side_effect=_update)
    backend = DlnaBackend(advance_cb=AsyncMock())
    backend._dmr = dmr
    backend._is_playing = True
    token = sup.on_dispatched(make_track())
    backend._confirm_token = token

    with patch("asyncio.sleep", AsyncMock()):
        await backend._poll_eos()              # exits via 2×STOPPED EOS

    rec.assert_not_called()
    assert backend._confirm_token == token     # still unconfirmed


async def test_none_transport_state_does_not_confirm(dlna_mock, fresh_supervisor):
    """A None transport read is 'unknown', not evidence of playback."""
    from app.output.dlna import DlnaBackend, TransportState
    sup, timers, rec = fresh_supervisor
    states = iter([
        None,                          # unknown — must not confirm
        TransportState.STOPPED,
        TransportState.STOPPED,        # EOS — poll exits
    ])
    dmr = make_dmr()

    async def _update(do_ping: bool = True) -> None:
        dmr.transport_state = next(states)

    dmr.async_update = AsyncMock(side_effect=_update)
    backend = DlnaBackend(advance_cb=AsyncMock())
    backend._dmr = dmr
    backend._is_playing = True
    token = sup.on_dispatched(make_track())
    backend._confirm_token = token

    with patch("asyncio.sleep", AsyncMock()):
        await backend._poll_eos()

    rec.assert_not_called()


# ── device-level typing + reachability probe (2026-07-11 supervisor plan U2) ──

async def test_play_no_device_raises_device_not_ready(dlna_mock):
    """The plain-RuntimeError asymmetry is closed (U2): "no device selected"
    is a typed device-level error so the holder-fallback loop can never drain
    the queue over it. Still a RuntimeError subclass, so every existing
    `except RuntimeError` call site keeps working."""
    from app.output.base import DeviceNotReadyError
    from app.output.dlna import DlnaBackend
    backend = DlnaBackend()
    with pytest.raises(DeviceNotReadyError):
        await backend.play("http://url", make_track())


async def test_probe_liveness_queries_transport_info_via_direct_action(dlna_mock):
    """DLNA probe semantics (plan KTD): one GetTransportInfo SOAP roundtrip
    via the direct _action path (never a guarded library helper — the
    Linkplay stale-action-cache convention)."""
    from app.output.dlna import DlnaBackend
    dmr = make_dmr()
    gti_action = MagicMock()
    gti_action.async_call = AsyncMock(
        return_value={"CurrentTransportState": "PLAYING"})

    def _action(service, name):
        if service == "AVT" and name == "GetTransportInfo":
            return gti_action
        return MagicMock()

    dmr._action = MagicMock(side_effect=_action)
    backend = DlnaBackend()
    backend._dmr = dmr

    assert await backend.probe_liveness() == (True, "PLAYING")
    gti_action.async_call.assert_awaited_once_with(InstanceID=0)


async def test_probe_liveness_soap_failure_reads_unreachable(dlna_mock):
    from app.output.dlna import DlnaBackend
    dmr = make_dmr()
    gti_action = MagicMock()
    gti_action.async_call = AsyncMock(side_effect=ConnectionResetError("gone"))
    dmr._action = MagicMock(return_value=gti_action)
    backend = DlnaBackend()
    backend._dmr = dmr
    assert await backend.probe_liveness() == (False, None)


async def test_probe_liveness_no_device_reads_unreachable(dlna_mock):
    from app.output.dlna import DlnaBackend
    backend = DlnaBackend()
    assert await backend.probe_liveness() == (False, None)


async def test_probe_liveness_missing_action_reads_unreachable(dlna_mock):
    from app.output.dlna import DlnaBackend
    dmr = make_dmr()
    dmr._action = MagicMock(return_value=None)
    backend = DlnaBackend()
    backend._dmr = dmr
    assert await backend.probe_liveness() == (False, None)


# ── gapless SetNext arming + boundary (2026-07-11 supervisor plan U8) ─────────
# The DLNA gapless advance-authority row: CurrentTrackURI change to the
# EXPECTED armed next while PLAYING advances (no dispatch); STOPPED-anyway =
# the gapped fallback (the existing 2xSTOPPED EOS advance, exactly once);
# poll errors → the existing notify_outage path, unchanged. Every U8 SOAP
# rides the direct _action path — the guarded library helper
# async_set_next_transport_uri is pinned never-awaited (stale-action-cache
# convention, same as async_stop/async_seek_rel_time).

import contextlib
import time as _time_mod


def _track(tid: str) -> Track:
    t = Track(id=tid, title=f"Song {tid}", artist="A", album="B",
              duration_ms=180000, stream_key=f"/parts/{tid}/f.flac")
    t.container = "flac"
    return t


class _StopPoll(Exception):
    """Bounded exit for _poll_eos runs that would otherwise loop forever."""


def _tick_limit(n: int):
    """asyncio.sleep replacement that raises _StopPoll after n poll ticks —
    the existing parse-error test's bounded-loop pattern."""
    count = {"n": 0}

    async def _sleep(*_a, **_k):
        count["n"] += 1
        if count["n"] > n:
            raise _StopPoll

    return AsyncMock(side_effect=_sleep)


# The canonical queue-wiring helper lives in tests/conftest.py (shared with
# test_output_direct.py and test_output_chromecast.py).
from tests.conftest import wire_queue as _wire_queue


def _playing_backend(dmr, advance_cb=None, current_uri="http://url1"):
    """A DlnaBackend mid-playback on device dev-1 with the arm-timing gate
    already satisfied (first PLAYING poll seen) — the state every boundary
    scenario starts from."""
    from app.output.dlna import DlnaBackend
    backend = DlnaBackend(advance_cb=advance_cb)
    backend._dmr = dmr
    backend._device_id = "dev-1"
    backend._is_playing = True
    backend._playing_confirmed = True
    backend._current_uri = current_uri
    backend._current_duration_ms = 180_000
    backend._play_start = _time_mod.monotonic()
    return backend


async def test_arm_next_defers_setnext_until_first_playing_poll(dlna_mock):
    """Arm-timing (capability map: Linkplay refuses SetNext near track end —
    deliver right after PLAYING): an arm stashed before the renderer's first
    PLAYING poll sends NOTHING; the first PLAYING read delivers it with the
    same DIDL shape play() builds. The guarded helper is never awaited."""
    from app.output.dlna import DlnaBackend, TransportState
    dmr = make_dmr()
    states = iter([
        TransportState.TRANSITIONING,  # pre-playback — no SetNext yet
        TransportState.PLAYING,        # first PLAYING → deliver the arm
        TransportState.PLAYING,
    ])

    async def _update(do_ping: bool = True) -> None:
        dmr.transport_state = next(states)

    dmr.async_update = AsyncMock(side_effect=_update)
    backend = DlnaBackend(advance_cb=AsyncMock())
    backend._dmr = dmr
    backend._device_id = "dev-1"
    backend._is_playing = True
    backend._current_uri = "http://url1"
    backend._current_duration_ms = 180_000
    backend._play_start = _time_mod.monotonic()
    t2 = _track("t2")

    await backend.arm_next("http://url2", t2)
    dmr._setnext_action.async_call.assert_not_awaited()      # stashed only
    assert backend._armed_next == ("http://url2", t2)

    with patch("asyncio.sleep", _tick_limit(3)):
        with pytest.raises(_StopPoll):
            await backend._poll_eos()

    dmr._setnext_action.async_call.assert_awaited_once()
    kwargs = dmr._setnext_action.async_call.await_args.kwargs
    assert kwargs["InstanceID"] == 0
    assert kwargs["NextURI"] == "http://url2"
    assert "DIDL-Lite" in kwargs["NextURIMetaData"]          # play()'s DIDL shape
    assert "Song t2" in kwargs["NextURIMetaData"]
    assert backend._setnext_sent is True
    assert dmr.async_set_next_transport_uri.await_count == 0


async def test_gapless_boundary_uri_change_advances_counts_and_rearms(
    dlna_mock, fresh_supervisor
):
    """U8 happy path (the advance-authority row): arm after PLAYING →
    SetNext delivered via direct _action; CurrentTrackURI flips to the
    expected next while PLAYING → notify_gapless_boundary advances the queue
    WITHOUT any SetAVTransportURI reload and counts the new track exactly
    once; the verdict flips supported (from unverified) and persists; the
    slot is consumed so the next arm re-primes immediately (one-shot NextURI
    per the capability map)."""
    from app.output.dlna import TransportState
    sup, timers, rec = fresh_supervisor
    dmr = make_dmr()
    dmr.transport_state = TransportState.PLAYING
    uris = iter(["http://url1", "http://url2"])
    dmr._position_action.async_call = AsyncMock(
        side_effect=lambda **kw: {"TrackURI": next(uris)})
    t1, t2 = _track("t1"), _track("t2")

    with contextlib.ExitStack() as stack:
        qe = _wire_queue(stack)
        set_setting = stack.enter_context(
            patch("app.database.set_setting", AsyncMock()))
        await qe.append(t1)
        item1 = await qe.advance()                 # t1 is the playing current
        token = sup.on_dispatched(item1.track)
        backend = _playing_backend(dmr, advance_cb=AsyncMock())
        sup.on_playback_confirmed(token)           # t1's own confirmed start
        rec.assert_called_once()
        await qe.append(t2)

        await backend.arm_next("http://url2", t2)  # already PLAYING → sent now
        dmr._setnext_action.async_call.assert_awaited_once()

        with patch("asyncio.sleep", _tick_limit(2)):
            with pytest.raises(_StopPoll):
                await backend._poll_eos()

        assert qe.state.current.track_id == "t2"   # advanced — no dispatch
        assert [i.track_id for i in qe.history] == ["t1"]
        assert rec.call_count == 2                 # t2 counted exactly once
        assert rec.call_args.args[0].id == "t2"
        dmr.async_set_transport_uri.assert_not_awaited()  # no reload
        backend._advance_cb.assert_not_awaited()   # boundary ≠ EOS advance
        assert backend._gapless_verdicts["dev-1"] == "supported"
        set_setting.assert_awaited_once_with(
            "gapless_verdict:dlna:dev-1", "supported")
        # Slot consumed → re-arm capability: the next arm delivers at once.
        assert backend._armed_next is None and backend._setnext_sent is False
        await backend.arm_next("http://url3", _track("t3"))
        assert dmr._setnext_action.async_call.await_count == 2
        assert dmr.async_set_next_transport_uri.await_count == 0


async def test_stopped_anyway_despite_setnext_gapped_fallback_once_unsupported(
    dlna_mock, fresh_supervisor
):
    """Renderer goes STOPPED despite an ACCEPTED SetNext: exactly ONE gapped
    advance fires — the existing 2xSTOPPED EOS path re-plays the expected
    next via the normal dispatch (the queue never advanced for the un-fired
    boundary) — never a phantom boundary on top; the armed slot is discarded
    and (the stop landing near the expected track end — a failed armed
    boundary, not an external stop) the unverified device verdicts
    unsupported."""
    from app.output.dlna import TransportState
    sup, timers, rec = fresh_supervisor
    advance_called = []

    async def advance():
        advance_called.append(True)

    dmr = make_dmr()

    async def _update(do_ping: bool = True) -> None:
        dmr.transport_state = TransportState.STOPPED

    dmr.async_update = AsyncMock(side_effect=_update)
    backend = _playing_backend(dmr, advance_cb=advance)
    # 170s into the 180s track: inside the end margin — this STOPPED is a
    # boundary-shaped stop, so it IS behavioral evidence (verdict written).
    backend._play_start = _time_mod.monotonic() - 170
    t2 = _track("t2")
    backend._armed_next = ("http://url2", t2)
    backend._setnext_sent = True
    backend._sent_next_url = "http://url2"

    notify = AsyncMock()
    set_setting = AsyncMock()
    with patch("app.output.session.notify_gapless_boundary", notify), \
         patch("app.database.set_setting", set_setting), \
         patch("asyncio.sleep", AsyncMock()):
        await backend._poll_eos()

    assert advance_called == [True]        # one advance — not zero, not two
    notify.assert_not_awaited()            # no phantom boundary advance
    assert backend._gapless_verdicts["dev-1"] == "unsupported"
    set_setting.assert_awaited_once_with(
        "gapless_verdict:dlna:dev-1", "unsupported")
    assert backend._armed_next is None and backend._setnext_sent is False
    assert backend.is_playing is False


async def test_stopped_mid_track_while_armed_advances_without_verdict(
    dlna_mock, fresh_supervisor
):
    """External-stop guard (review C3): 2xSTOPPED with an ACCEPTED SetNext
    but far from the expected track end is an EXTERNAL stop (the user
    stopped the renderer from its own app), not behavioral evidence — the
    gapped fallback advance still fires exactly once, but NO verdict is
    written: the device stays unverified and a real armed boundary can
    still decide it later."""
    from app.output.dlna import TransportState
    sup, timers, rec = fresh_supervisor
    advance_called = []

    async def advance():
        advance_called.append(True)

    dmr = make_dmr()

    async def _update(do_ping: bool = True) -> None:
        dmr.transport_state = TransportState.STOPPED

    dmr.async_update = AsyncMock(side_effect=_update)
    # _playing_backend anchors _play_start at NOW → 0s into a 180s track,
    # well outside the end margin.
    backend = _playing_backend(dmr, advance_cb=advance)
    t2 = _track("t2")
    backend._armed_next = ("http://url2", t2)
    backend._setnext_sent = True
    backend._sent_next_url = "http://url2"

    notify = AsyncMock()
    set_setting = AsyncMock()
    with patch("app.output.session.notify_gapless_boundary", notify), \
         patch("app.database.set_setting", set_setting), \
         patch("asyncio.sleep", AsyncMock()):
        await backend._poll_eos()

    assert advance_called == [True]        # the gapped advance is ungated
    notify.assert_not_awaited()            # no phantom boundary advance
    assert backend._gapless_verdicts.get("dev-1") is None   # verdict untouched
    set_setting.assert_not_awaited()
    assert backend._armed_next is None and backend._setnext_sent is False
    assert backend.is_playing is False


async def test_arm_same_uri_as_current_never_sends_no_verdict_normal_eos(
    dlna_mock, fresh_supervisor
):
    """Duplicate-track arm, immediate-send path (review C1): the same track
    queued twice makes the armed NextURI equal the CURRENT URI, so the
    boundary's URI-flip detection could never fire — arming device-side
    would only buy the late window expiry plus a false permanent
    'unsupported'. The arm is discarded with NO SetNext SOAP and NO verdict
    (same-URI transitions are inherently undetectable), and the track's own
    EOS advances normally (per-track fallback)."""
    from app.output.dlna import TransportState
    sup, timers, rec = fresh_supervisor
    advance_called = []

    async def advance():
        advance_called.append(True)

    dmr = make_dmr()

    async def _update(do_ping: bool = True) -> None:
        dmr.transport_state = TransportState.STOPPED

    dmr.async_update = AsyncMock(side_effect=_update)
    backend = _playing_backend(dmr, advance_cb=advance,
                               current_uri="http://url1")
    set_setting = AsyncMock()
    with patch("app.database.set_setting", set_setting):
        await backend.arm_next("http://url1", _track("t1"))  # duplicate track

    dmr._setnext_action.async_call.assert_not_awaited()   # no SetNext SOAP
    assert backend._armed_next is None and backend._setnext_sent is False
    assert backend._gapless_verdicts.get("dev-1") is None  # no verdict write
    set_setting.assert_not_awaited()

    with patch("asyncio.sleep", AsyncMock()):
        await backend._poll_eos()
    assert advance_called == [True]                        # normal EOS advance


async def test_arm_same_uri_deferred_send_discards_at_first_playing(dlna_mock):
    """Duplicate-track arm, deferred-send path (review C1): an arm stashed
    BEFORE the renderer's first PLAYING poll is discarded — never sent — at
    delivery time when its URI equals the current track's; no verdict, and
    the boundary watch never starts (no GetPositionInfo reads)."""
    from app.output.dlna import DlnaBackend, TransportState
    dmr = make_dmr()
    states = iter([
        TransportState.TRANSITIONING,  # pre-playback — arm stays stashed
        TransportState.PLAYING,        # first PLAYING → delivery time
        TransportState.PLAYING,
    ])

    async def _update(do_ping: bool = True) -> None:
        dmr.transport_state = next(states)

    dmr.async_update = AsyncMock(side_effect=_update)
    backend = DlnaBackend(advance_cb=AsyncMock())
    backend._dmr = dmr
    backend._device_id = "dev-1"
    backend._is_playing = True
    backend._current_uri = "http://url1"
    backend._current_duration_ms = 180_000
    backend._play_start = _time_mod.monotonic()
    t1_again = _track("t1")

    set_setting = AsyncMock()
    with patch("app.database.set_setting", set_setting):
        await backend.arm_next("http://url1", t1_again)
        assert backend._armed_next == ("http://url1", t1_again)  # stashed

        with patch("asyncio.sleep", _tick_limit(3)):
            with pytest.raises(_StopPoll):
                await backend._poll_eos()

    dmr._setnext_action.async_call.assert_not_awaited()   # discarded, not sent
    dmr._position_action.async_call.assert_not_awaited()  # no boundary watch
    assert backend._armed_next is None and backend._setnext_sent is False
    assert backend._gapless_verdicts.get("dev-1") is None
    set_setting.assert_not_awaited()


async def test_setnext_failure_keeps_stale_watch_and_corrects_chained_next(
    dlna_mock,
):
    """SetNext failure ≠ SetNext not applied (review C2): a SOAP
    failure/timeout may still have been APPLIED by the renderer (a response
    lost on the wire is indistinguishable from a refusal). The failed URL
    goes on the stale watch instead of vanishing — so when the renderer
    chains into it anyway, the existing wrong-URI correction fires
    (corrective Stop + exactly one replay advance) instead of the delivered
    next playing completely unwatched (frozen Now Playing + double play)."""
    from app.output.dlna import TransportState
    advance_called = []

    async def advance():
        advance_called.append(True)

    dmr = make_dmr()
    dmr._setnext_action.async_call = AsyncMock(
        side_effect=Exception("timeout: response lost on the wire"))
    backend = _playing_backend(dmr, advance_cb=advance)
    set_setting = AsyncMock()
    with patch("app.database.set_setting", set_setting):
        await backend.arm_next("http://url2", _track("t2"))  # SOAP raises

    assert backend._stale_next_uri == "http://url2"          # watched anyway
    assert backend._armed_next is None and backend._setnext_sent is False
    set_setting.assert_not_awaited()                         # no verdict

    # The renderer applied the SetNext after all: CurrentTrackURI flips to
    # the failed URL while PLAYING → the existing correction machinery.
    dmr.transport_state = TransportState.PLAYING
    dmr._position_action.async_call = AsyncMock(
        return_value={"TrackURI": "http://url2"})
    notify = AsyncMock()
    with patch("app.output.session.notify_gapless_boundary", notify), \
         patch("asyncio.sleep", AsyncMock()):
        await backend._poll_eos()

    dmr._stop_action.async_call.assert_awaited_once_with(InstanceID=0)
    assert advance_called == [True]        # stop-and-replay, exactly once
    notify.assert_not_awaited()            # never counted as a boundary
    assert backend._stale_next_uri is None
    assert backend.is_playing is False


async def test_stale_uri_window_expiry_late_boundary_and_unsupported(
    dlna_mock, fresh_supervisor
):
    """Audibly-gapless-but-URI-never-updates (the WiiM stale-metadata mode):
    the bounded window past the expected track end expires with the
    transport still PLAYING and CurrentTrackURI unchanged → treated as a
    LATE boundary — counted once, queue advanced, so Now Playing never
    freezes on the old track — and the verdict is unsupported (DETECTABILITY
    criterion: audio quality alone is not support). Arming is disabled for
    the device thereafter (per-track path)."""
    from app.output.dlna import TransportState
    sup, timers, rec = fresh_supervisor
    dmr = make_dmr()
    dmr.transport_state = TransportState.PLAYING
    dmr._position_action.async_call = AsyncMock(
        return_value={"TrackURI": "http://url1"})   # never flips
    t1, t2 = _track("t1"), _track("t2")

    with contextlib.ExitStack() as stack:
        qe = _wire_queue(stack)
        set_setting = stack.enter_context(
            patch("app.database.set_setting", AsyncMock()))
        await qe.append(t1)
        item1 = await qe.advance()
        sup.on_playback_confirmed(sup.on_dispatched(item1.track))
        await qe.append(t2)
        backend = _playing_backend(dmr, advance_cb=AsyncMock())
        backend._play_start = _time_mod.monotonic() - 500  # 180s track long over
        backend._armed_next = ("http://url2", t2)
        backend._setnext_sent = True
        backend._sent_next_url = "http://url2"

        with patch("asyncio.sleep", _tick_limit(2)):
            with pytest.raises(_StopPoll):
                await backend._poll_eos()

        assert qe.state.current.track_id == "t2"   # late boundary — no freeze
        assert rec.call_count == 2                 # counted exactly once
        backend._advance_cb.assert_not_awaited()   # one advance, via boundary
        assert backend._gapless_verdicts["dev-1"] == "unsupported"
        set_setting.assert_awaited_once_with(
            "gapless_verdict:dlna:dev-1", "unsupported")
        # Subsequent boundaries are per-track: arming is disabled now.
        await backend.arm_next("http://url3", _track("t3"))
        assert backend._armed_next is None
        assert dmr._setnext_action.async_call.await_count == 0


async def test_scpd_without_setnext_never_arms_and_caches_unsupported(dlna_mock):
    """SCPD lacks SetNextAVTransportURI → never armed, per-track path, and
    the capability is cached unsupported IMMEDIATELY (a static gate needs no
    behavioral probe — the renderer cannot chain, full stop)."""
    from app.output.dlna import DlnaBackend
    dmr = make_dmr()
    orig = dmr._action.side_effect

    def _action(service, name):
        if service == "AVT" and name == "SetNextAVTransportURI":
            return None                            # action absent from SCPD
        return orig(service, name)

    dmr._action = MagicMock(side_effect=_action)
    backend = DlnaBackend()
    backend._dmr = dmr
    backend._device_id = "dev-1"
    backend._playing_confirmed = True
    set_setting = AsyncMock()
    with patch("app.database.set_setting", set_setting):
        await backend.arm_next("http://url2", _track("t2"))
    assert backend._armed_next is None
    dmr._setnext_action.async_call.assert_not_awaited()
    assert backend._gapless_verdicts["dev-1"] == "unsupported"
    set_setting.assert_awaited_once_with(
        "gapless_verdict:dlna:dev-1", "unsupported")


async def test_setnext_refusal_discards_arm_without_verdict_change(dlna_mock):
    """Linkplay refuses SetNext sent near track end and on <40s tracks: the
    refusal discards the arm — the boundary is per-track EOS naturally, no
    armed state ever formed — with NO verdict change: a refusal is not
    evidence of missing support; only a FAILED ARMED BOUNDARY decides."""
    from app.output.dlna import DlnaBackend
    dmr = make_dmr()
    dmr._setnext_action.async_call = AsyncMock(
        side_effect=Exception("712: refused near track end"))
    backend = DlnaBackend()
    backend._dmr = dmr
    backend._device_id = "dev-1"
    backend._playing_confirmed = True
    set_setting = AsyncMock()
    with patch("app.database.set_setting", set_setting):
        await backend.arm_next("http://url2", _track("t2"))  # must not raise
    assert backend._armed_next is None and backend._setnext_sent is False
    set_setting.assert_not_awaited()
    assert backend._gapless_verdicts.get("dev-1") is None    # still unverified


async def test_arm_next_noop_when_cached_verdict_unsupported(dlna_mock):
    """"unsupported" disables arming: the verdict is per DEVICE while
    hasattr(backend, "arm_next") is per class, so the backend itself
    enforces the gate — no stash, no SOAP, per-track path."""
    from app.output.dlna import DlnaBackend
    dmr = make_dmr()
    backend = DlnaBackend()
    backend._dmr = dmr
    backend._device_id = "dev-1"
    backend._playing_confirmed = True
    backend._gapless_verdicts["dev-1"] = "unsupported"
    await backend.arm_next("http://url2", _track("t2"))
    assert backend._armed_next is None
    dmr._setnext_action.async_call.assert_not_awaited()


async def test_revoke_sends_empty_next_uri_and_keeps_stale_watch(dlna_mock):
    """revoke_next on a DELIVERED arm issues the empty-NextURI revoke via
    direct _action — and keeps the delivered URI on the stale watch either
    way, because Linkplay firmware variously errors OR silently ignores the
    empty-URI revoke and a SOAP success cannot tell honored from ignored.
    Idempotent: a second revoke is a pure no-op."""
    from app.output.dlna import DlnaBackend
    dmr = make_dmr()
    backend = DlnaBackend()
    backend._dmr = dmr
    backend._device_id = "dev-1"
    backend._playing_confirmed = True
    await backend.arm_next("http://url2", _track("t2"))      # delivered
    dmr._setnext_action.async_call.assert_awaited_once()

    await backend.revoke_next()
    assert dmr._setnext_action.async_call.await_count == 2
    kwargs = dmr._setnext_action.async_call.await_args.kwargs
    assert kwargs["NextURI"] == "" and kwargs["NextURIMetaData"] == ""
    assert backend._armed_next is None and backend._setnext_sent is False
    assert backend._stale_next_uri == "http://url2"          # watched

    await backend.revoke_next()                              # idempotent
    assert dmr._setnext_action.async_call.await_count == 2
    assert dmr.async_set_next_transport_uri.await_count == 0


async def test_revoke_error_keeps_stale_watch_without_raising(dlna_mock):
    """The renderer ERRORS on the empty-URI revoke (firmware varies): the
    slot still clears server-side, nothing raises, and the delivered URI
    stays on the stale watch for the correction path."""
    from app.output.dlna import DlnaBackend
    dmr = make_dmr()
    backend = DlnaBackend()
    backend._dmr = dmr
    backend._device_id = "dev-1"
    backend._playing_confirmed = True
    await backend.arm_next("http://url2", _track("t2"))
    dmr._setnext_action.async_call = AsyncMock(
        side_effect=Exception("718: invalid instance / revoke unsupported"))
    await backend.revoke_next()                              # must not raise
    assert backend._stale_next_uri == "http://url2"
    assert backend._armed_next is None and backend._setnext_sent is False


async def test_stale_next_boundary_stop_and_replays_correct_front(dlna_mock):
    """The DEFINED revoke-failure fallback: the renderer chained into the
    REVOKED next anyway. The boundary watch sees the WRONG CurrentTrackURI
    change → corrective Stop + exactly one normal advance (the replay of the
    correct queue front via the normal dispatch path) — never a silent wrong
    track, and never a boundary count for the wrong track."""
    from app.output.dlna import TransportState
    advance_called = []

    async def advance():
        advance_called.append(True)

    dmr = make_dmr()
    dmr.transport_state = TransportState.PLAYING
    dmr._position_action.async_call = AsyncMock(
        return_value={"TrackURI": "http://revoked"})
    backend = _playing_backend(dmr, advance_cb=advance)
    backend._stale_next_uri = "http://revoked"   # a failed/ignored revoke
    notify = AsyncMock()
    with patch("app.output.session.notify_gapless_boundary", notify), \
         patch("asyncio.sleep", AsyncMock()):
        await backend._poll_eos()

    dmr._stop_action.async_call.assert_awaited_once_with(InstanceID=0)
    assert advance_called == [True]        # the replay — exactly one advance
    notify.assert_not_awaited()            # the wrong track is never counted
    assert backend._stale_next_uri is None
    assert backend.is_playing is False


async def test_gapless_soap_bypasses_guarded_helpers_with_stale_action_cache(dlna_mock):
    """Regression pin (stale-CurrentTransportActions doc): with the cache
    stuck at {"play"} — the Linkplay bug condition — every U8 SOAP (SetNext
    arm, boundary GetPositionInfo read, empty-URI revoke) still fires via
    direct _action, and the guarded async_set_next_transport_uri helper is
    never awaited."""
    from app.output.dlna import TransportState
    dmr = make_dmr()
    dmr._current_transport_actions = {"play"}    # the stale-cache condition
    dmr.transport_state = TransportState.PLAYING
    dmr._position_action.async_call = AsyncMock(
        return_value={"TrackURI": "http://url1"})
    backend = _playing_backend(dmr, advance_cb=AsyncMock())

    await backend.arm_next("http://url2", _track("t2"))
    dmr._setnext_action.async_call.assert_awaited_once()

    with patch("asyncio.sleep", _tick_limit(1)):
        with pytest.raises(_StopPoll):
            await backend._poll_eos()
    dmr._position_action.async_call.assert_awaited_once_with(InstanceID=0)

    await backend.revoke_next()
    assert dmr._setnext_action.async_call.await_count == 2

    assert dmr.async_set_next_transport_uri.await_count == 0


async def test_play_clears_armed_state_and_arm_timing_gate(dlna_mock, fresh_supervisor):
    """U6 contract: play() clears the slot — a fresh dispatch owns the
    boundary. The arm-timing gate resets too, so the NEW track's arm waits
    for ITS first PLAYING poll, and the boundary baseline re-anchors."""
    from app.output.dlna import DlnaBackend
    dmr = make_dmr()
    backend = DlnaBackend()
    backend._dmr = dmr
    backend._device_id = "dev-1"
    backend._playing_confirmed = True
    await backend.arm_next("http://url2", _track("t2"))
    backend._stale_next_uri = "http://old"
    await backend.play("http://url3", _track("t3"))
    backend._cancel_poll()
    assert backend._armed_next is None
    assert backend._setnext_sent is False
    assert backend._stale_next_uri is None
    assert backend._playing_confirmed is False
    assert backend._current_uri == "http://url3"


async def test_verdict_first_boundary_decides_and_sticks(dlna_mock):
    """First-armed-boundary-decides: an already-decided device keeps its
    verdict — later outcomes never overwrite (re-verification = clearing the
    persisted gapless_verdict:dlna:{device_id} setting)."""
    from app.output.dlna import DlnaBackend
    backend = DlnaBackend()
    backend._device_id = "dev-1"
    set_setting = AsyncMock()
    with patch("app.database.set_setting", set_setting):
        await backend._decide_gapless_verdict("supported")
        await backend._decide_gapless_verdict("unsupported")
    assert backend._gapless_verdicts["dev-1"] == "supported"
    set_setting.assert_awaited_once_with(
        "gapless_verdict:dlna:dev-1", "supported")


async def test_verdict_flip_schedules_devices_changed_broadcast(dlna_mock, monkeypatch):
    """A decided verdict refreshes the picker chip by reusing the watcher's
    debounced devices_changed broadcast (the snapshot builder bulk-reads the
    persisted verdicts, so the scheduled frame carries the flip)."""
    import app.output.watcher as watcher_module
    from app.output.dlna import DlnaBackend
    fake_watcher = MagicMock()
    monkeypatch.setattr(watcher_module, "_watcher", fake_watcher)
    dmr = make_dmr()

    def _action(service, name):
        if service == "AVT" and name == "SetNextAVTransportURI":
            return None                            # static gate → verdict
        return MagicMock()

    dmr._action = MagicMock(side_effect=_action)
    backend = DlnaBackend()
    backend._dmr = dmr
    backend._device_id = "dev-1"
    with patch("app.database.set_setting", AsyncMock()):
        await backend.arm_next("http://url2", _track("t2"))
    fake_watcher._schedule_broadcast.assert_called_once()


async def test_set_device_hydrates_gapless_verdict(dlna_mock):
    """set_device hydrates the per-device verdict cache from the persisted
    gapless_verdict:dlna:{device_id} setting so the arming gate reads
    memory, never the DB, on the playback path (plan U8)."""
    from app.output.dlna import DlnaBackend
    backend = DlnaBackend()
    backend._device_locations["dev-1"] = "http://192.168.1.50:8000/desc.xml"

    notify = MagicMock()
    notify.async_start_server = AsyncMock()
    notify.async_stop_server = AsyncMock()
    notify.event_handler = MagicMock()
    dmr = make_dmr()
    fake_upnp_factory = MagicMock()
    fake_upnp_factory.async_create_device = AsyncMock(return_value=MagicMock())

    async def _get_setting(key, default=None):
        if key == "gapless_verdict:dlna:dev-1":
            return "unsupported"
        return None

    with patch("app.output.dlna.AiohttpSessionRequester", MagicMock(), create=True), \
         patch("app.output.dlna.UpnpFactory", MagicMock(return_value=fake_upnp_factory), create=True), \
         patch("app.output.dlna.AiohttpNotifyServer", MagicMock(return_value=notify), create=True), \
         patch("app.output.dlna.DmrDevice", MagicMock(return_value=dmr), create=True), \
         patch("app.database.get_setting", AsyncMock(side_effect=_get_setting)), \
         patch("app.database.set_setting", AsyncMock()):
        await backend.set_device("dev-1")

    assert backend._gapless_verdicts["dev-1"] == "unsupported"
