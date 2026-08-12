"""Best-effort live-title (ICY) tests — radio plan U6.

All synthetic: a hand-built ICY byte stream fed through a mocked httpx transport
(no network), plus the Direct backend's bus-tag title path driven directly (the
gst_mock pattern from tests/test_output_direct.py). No real hosts — example /
documentation ranges only.

What U6 promises and these tests pin:
- the bounded reader extracts a ``StreamTitle`` and closes after ONE block;
- missing metadata (no ``icy-metaint`` / ``L=0``) → None, station-name-only (AE5);
- a lying/garbage server is BOUNDED (block <=4080, total-read cap, latin-1
  tolerated) and never hangs;
- the Direct backend surfaces the title from a bus TAG message, newest-wins;
- SEC-004: an untrusted ``StreamTitle`` with HTML/XML metacharacters is carried
  VERBATIM as a plain string for the WS sink (no HTML) and XML-escaped for the
  DIDL sink (both asserted; the client textContent contract for U7 is documented).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from unittest.mock import MagicMock, patch

from app.radio.icy import (
    parse_stream_title,
    read_stream_title,
    sanitize_title,
    xml_escape_title,
    _ICY_META_BLOCK_MAX,
    _ICY_TOTAL_READ_CAP,
)


# ── synthetic ICY stream builder ───────────────────────────────────────────────


def _meta_block(payload: bytes) -> bytes:
    """One ICY metadata block: a length byte L (in 16-byte units) + L*16 bytes,
    NUL-padded to the boundary (exactly what a real server sends)."""
    length = (len(payload) + 15) // 16
    padded = payload + b"\x00" * (length * 16 - len(payload))
    return bytes([length]) + padded


def _icy_bytes(metaint: int, payload: bytes, *, audio: bytes | None = None) -> bytes:
    """`metaint` audio bytes, then one metadata block for `payload`."""
    audio = audio if audio is not None else (b"\xff" * metaint)
    return audio + _meta_block(payload)


def _client_for(body: bytes, *, headers: dict | None = None) -> httpx.AsyncClient:
    """An httpx client whose single mocked response streams `body` with the
    given headers (default: a valid ``icy-metaint``)."""
    hdrs = {"icy-metaint": "16"} if headers is None else headers

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=hdrs, content=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


URL = "http://stream.example/radio"  # documentation host only


# ══════════════════════════════════════════════════════════════════════════════
# parse_stream_title — the pure, separately-unit-testable parser
# ══════════════════════════════════════════════════════════════════════════════


def test_parse_extracts_streamtitle():
    block = b"StreamTitle='Miles Davis - So What';StreamUrl='';\x00\x00"
    assert parse_stream_title(block) == "Miles Davis - So What"


def test_parse_no_streamtitle_returns_none():
    assert parse_stream_title(b"StreamUrl='http://x';\x00\x00") is None
    assert parse_stream_title(b"") is None
    assert parse_stream_title(b"\x00" * 16) is None


def test_parse_tolerates_undecodable_bytes_latin1():
    # 0x92 is a Windows-1252 curly apostrophe — invalid UTF-8, valid latin-1.
    block = b"StreamTitle='Sinead O\x92Connor';\x00\x00"
    out = parse_stream_title(block)
    assert out is not None and out.startswith("Sinead O")


def test_parse_missing_trailing_semicolon_still_matches():
    # Some servers omit the trailing ';' on the last field.
    assert parse_stream_title(b"StreamTitle='Lone Field'") == "Lone Field"


# ══════════════════════════════════════════════════════════════════════════════
# read_stream_title — the bounded async reader (mocked transport, no network)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_happy_reads_title_and_returns_it():
    """A synthetic ICY stream with icy-metaint + a StreamTitle → the title."""
    body = _icy_bytes(16, b"StreamTitle='Artist - Title';")
    async with _client_for(body) as client:
        title = await read_stream_title(URL, client=client)
    assert title == "Artist - Title"


@pytest.mark.asyncio
async def test_no_metaint_header_returns_none():
    """AE5: no icy-metaint ⇒ the server sends no metadata; don't parse."""
    body = b"\xff" * 64  # pure audio, no metadata framing
    async with _client_for(body, headers={}) as client:
        title = await read_stream_title(URL, client=client)
    assert title is None


@pytest.mark.asyncio
async def test_length_byte_zero_returns_none():
    """L==0 (no metadata this cycle, the common case) → None cleanly."""
    body = b"\xff" * 16 + b"\x00"  # metaint audio, then L=0
    async with _client_for(body) as client:
        title = await read_stream_title(URL, client=client)
    assert title is None


@pytest.mark.asyncio
async def test_oversized_length_byte_is_bounded():
    """A garbage/oversized length byte can never demand more than 4080 bytes:
    the reader reads at most _ICY_META_BLOCK_MAX and stops (never hangs). Here L
    is the max (255) but the body only carries a small real block after it — the
    reader must not wedge waiting for 4080 bytes that never come; it returns None
    (truncated block) rather than hanging."""
    # metaint audio, then L=255 (claims 4080 bytes) but only a few real bytes.
    body = b"\xff" * 16 + bytes([255]) + b"StreamTitle='x';"
    async with _client_for(body) as client:
        title = await asyncio.wait_for(read_stream_title(URL, client=client), 5)
    assert title is None
    assert _ICY_META_BLOCK_MAX == 4080


@pytest.mark.asyncio
async def test_lying_server_hits_total_cap_returns_none_no_hang():
    """A server that lies about icy-metaint (streams forever without ever
    reaching the metadata point) must trip the total-read cap and return None,
    not hang. We advertise a metaint larger than the cap-worth of bytes the
    (endless) transport would feed."""

    def handler(request: httpx.Request) -> httpx.Response:
        # icy-metaint far beyond the total cap: the reader can never discard
        # enough audio to reach a length byte, so it must give up at the cap.
        big = _ICY_TOTAL_READ_CAP * 4
        body = b"\x00" * (_ICY_TOTAL_READ_CAP + 4096)  # < metaint, endless-ish
        return httpx.Response(200, headers={"icy-metaint": str(big)}, content=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        title = await asyncio.wait_for(read_stream_title(URL, client=client), 5)
    assert title is None


@pytest.mark.asyncio
async def test_read_is_non_raising_on_transport_error():
    """Best-effort by contract: a transport failure (e.g. an ICY 200 OK server
    h11 would reject) degrades to None, never raises to the caller."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        title = await read_stream_title(URL, client=client)
    assert title is None


@pytest.mark.asyncio
async def test_reader_discards_exactly_metaint_audio_then_reads_block():
    """The block is located AFTER exactly `metaint` audio bytes (a title placed
    at the right offset is found; the same bytes earlier would be audio)."""
    metaint = 200
    body = _icy_bytes(metaint, b"StreamTitle='Deep Cut';")
    async with _client_for(body, headers={"icy-metaint": str(metaint)}) as client:
        title = await read_stream_title(URL, client=client)
    assert title == "Deep Cut"


# ══════════════════════════════════════════════════════════════════════════════
# sanitize_title / xml_escape_title (SEC-004 helpers)
# ══════════════════════════════════════════════════════════════════════════════


def test_sanitize_strips_control_chars_and_collapses_ws():
    assert sanitize_title("  Artist\r\n-\tTitle  ") == "Artist - Title"
    assert sanitize_title("") is None
    assert sanitize_title("   ") is None
    assert sanitize_title(None) is None


def test_sanitize_bounds_length():
    out = sanitize_title("x" * 5000)
    assert out is not None and len(out) <= 400


# ══════════════════════════════════════════════════════════════════════════════
# Direct backend: bus TAG title, newest-wins (gst_mock pattern)
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def gst_mock():
    mock_pipeline = MagicMock()
    mock_gst = MagicMock()
    mock_gst.ElementFactory.make.return_value = mock_pipeline
    mock_gst.State.PLAYING = "PLAYING"
    mock_gst.State.PAUSED = "PAUSED"
    mock_gst.State.NULL = "NULL"
    mock_gst.TAG_TITLE = "title"
    with patch("app.output.direct._GST_AVAILABLE", True):
        with patch("app.output.direct.Gst", mock_gst, create=True):
            with patch("app.output.direct.ensure_bus_mainloop"):
                yield mock_gst, mock_pipeline


def _radio_track():
    from app.radio.session import make_radio_track

    station = MagicMock()
    station.stationuuid = "st-1"
    station.name = "Jazz FM"
    station.favicon = None
    return make_radio_track(station, URL)


def _tag_msg(pipeline, title: str, *, ok: bool = True):
    """A GST_MESSAGE_TAG whose taglist yields `title` for GST_TAG_TITLE."""
    msg = MagicMock()
    msg.src = pipeline
    taglist = MagicMock()
    taglist.get_string.return_value = (ok, title)
    msg.parse_tag.return_value = taglist
    return msg


@pytest.mark.asyncio
async def test_direct_radio_title_from_bus_tag_newest_wins(gst_mock):
    """The Direct title comes from a bus TAG message and updates on a new
    StreamTitle (newest-wins), notifying the U6 hook each change."""
    from app.output.direct import DirectAudioBackend

    mock_gst, mock_pipeline = gst_mock
    seen: list = []
    backend = DirectAudioBackend()
    backend.set_radio_title_hook(lambda t: seen.append(t))
    await backend.play(URL, _radio_track())

    backend._on_tag(None, _tag_msg(backend._pipeline, "First Song"))
    assert backend._radio_title == "First Song"
    backend._on_tag(None, _tag_msg(backend._pipeline, "Second Song"))
    assert backend._radio_title == "Second Song"

    # A duplicate tag does not re-notify (no spurious broadcast).
    backend._on_tag(None, _tag_msg(backend._pipeline, "Second Song"))

    assert seen == ["First Song", "Second Song"]


@pytest.mark.asyncio
async def test_direct_tag_ignored_when_not_radio(gst_mock):
    """A finite track never drives the radio title line — _on_tag no-ops."""
    from app.output.direct import DirectAudioBackend
    from app.plex.models import Track

    finite = Track(id="t1", title="Song", artist="A", album="B",
                   duration_ms=180000, stream_key="/parts/1/f.mp3")
    backend = DirectAudioBackend()
    seen: list = []
    backend.set_radio_title_hook(lambda t: seen.append(t))
    await backend.play(URL, finite)
    backend._on_tag(None, _tag_msg(backend._pipeline, "Should Be Ignored"))
    assert backend._radio_title is None
    assert seen == []


@pytest.mark.asyncio
async def test_direct_play_new_station_clears_prior_title(gst_mock):
    """A new station clears the prior title (a stale title must not outlive its
    station) and notifies the hook with None."""
    from app.output.direct import DirectAudioBackend

    backend = DirectAudioBackend()
    seen: list = []
    backend.set_radio_title_hook(lambda t: seen.append(t))
    await backend.play(URL, _radio_track())
    backend._on_tag(None, _tag_msg(backend._pipeline, "Now Playing"))
    assert backend._radio_title == "Now Playing"

    # Starting another station clears it.
    await backend.play("http://stream.example/other", _radio_track())
    assert backend._radio_title is None
    assert seen[-1] is None


# ══════════════════════════════════════════════════════════════════════════════
# SEC-004: an HTML/XML-metacharacter StreamTitle at each sink
# ══════════════════════════════════════════════════════════════════════════════

# The malicious/untrusted title used across both sinks.
_EVIL = "<script>alert(1)</script> & \"quote\""


def test_sec004_ws_sink_carries_plain_string_no_html():
    """WS sink (U7 RadioStateEvent.live_title): the value is carried VERBATIM as
    a plain string — NO HTML entities are introduced. The client renders it via
    element.textContent (never innerHTML), so the string is inert. This test
    documents + enforces that contract for U7's client implementer."""
    # The reader/sanitizer keeps the metacharacters literal (only control chars /
    # whitespace are normalized) — nothing is HTML-escaped on the WS path.
    out = sanitize_title(_EVIL)
    assert out == _EVIL  # verbatim: '<', '&', '"' all present, no entities
    assert "&lt;" not in out and "&amp;" not in out


def test_sec004_ws_sink_via_reader_and_session_is_plain():
    """End-to-end WS path: the bounded reader returns the plain string, and the
    session's set_title/current_title carry it unescaped to U7's broadcast."""
    from app.radio.session import RadioSession

    block = f"StreamTitle='{_EVIL}';".encode("latin-1")
    assert parse_stream_title(block) == _EVIL  # reader keeps it literal

    sess = RadioSession()
    broadcast: list = []
    sess.add_title_listener(lambda t: broadcast.append(t))
    sess.set_title(_EVIL)
    assert sess.current_title() == _EVIL           # plain string for the WS sink
    assert broadcast == [_EVIL]                     # broadcast verbatim, no HTML


def test_sec004_didl_sink_xml_escapes():
    """DIDL sink (Cast/DLNA device metadata): the SAME untrusted title is
    XML-escaped before embedding in the DIDL body, so '<', '&', '"' can't break
    the XML or inject markup."""
    from app.output.dlna import _didl_metadata

    didl = _didl_metadata(_EVIL, "http://x/y", "audio/mpeg", 0, omit_duration=True)
    assert "<script>" not in didl                   # raw tag never embedded
    assert "&lt;script&gt;" in didl                 # escaped instead
    assert "&amp;" in didl and "&quot;" in didl

    # The shared helper escapes identically (used by the Cast metadata path).
    esc = xml_escape_title(_EVIL)
    assert "<script>" not in esc and "&lt;script&gt;" in esc


# ══════════════════════════════════════════════════════════════════════════════
# F1 — the bounded title read must NOT follow redirects (an SSRF vector) and must
# redact the URL in logs.
# ══════════════════════════════════════════════════════════════════════════════


async def test_f1_read_does_not_follow_redirect_during_read(monkeypatch):
    """A redirect during the title read must NOT be followed (SSRF vector). With a
    default (own) client, read_stream_title builds it follow_redirects=False, so a
    302 to an internal host is returned as a redirect (no icy-metaint) → None, and
    the redirect target is NEVER fetched."""
    import app.radio.icy as icy_mod

    fetched_hosts: list[str] = []
    built_clients: list[httpx.AsyncClient] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched_hosts.append(request.url.host)
        if request.url.host == "public.example":
            # A redirect to an internal host — must NOT be followed.
            return httpx.Response(302, headers={"location": "http://127.0.0.1/x"})
        raise AssertionError(f"redirect target was fetched: {request.url}")

    real_ctor = httpx.AsyncClient

    def _spy_ctor(*args, **kwargs):
        # Assert the reader builds its own client non-following (F1).
        assert kwargs.get("follow_redirects") is False, \
            "the title reader must build its client follow_redirects=False"
        c = real_ctor(transport=httpx.MockTransport(handler),
                      follow_redirects=kwargs.get("follow_redirects", True))
        built_clients.append(c)
        return c

    monkeypatch.setattr(icy_mod.httpx, "AsyncClient", _spy_ctor)

    title = await read_stream_title("http://public.example/stream")  # own client
    assert title is None                       # a 302 has no icy-metaint → no title
    assert fetched_hosts == ["public.example"], \
        "the redirect target (127.0.0.1) must never be fetched"


def test_f1_redact_url_logs_host_only():
    """The URL redactor keeps only scheme://host (path/query may carry a token)."""
    from app.radio.icy import _redact_url

    assert _redact_url("http://stream.example:8000/live?auth=SECRET&t=9") \
        == "http://stream.example:8000"
    assert _redact_url("https://host.example/path") == "https://host.example"
    assert _redact_url("not a url") == "<redacted>"
