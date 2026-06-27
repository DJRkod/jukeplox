"""Tests for the LRCLIB lyrics client + LRC parser (plan 2026-06-17-008 U1).
All HTTP is mocked via respx; the parser is pure."""
import httpx
import pytest

from app.lyrics.lrc import parse_lrc
from app.lyrics.client import fetch_lyrics, _match_norm, _TIMEOUT, LyricsFetchError


# ── LRC parser (pure) ─────────────────────────────────────────────────────────

def test_parse_lrc_synced_lines_ordered():
    lrc = "[00:05.00]first\n[00:12.50]second\n[00:01.00]intro"
    out = parse_lrc(lrc)
    assert [e["line"] for e in out] == ["intro", "first", "second"]  # sorted by time
    assert out[0]["t_ms"] == 1000 and out[1]["t_ms"] == 5000 and out[2]["t_ms"] == 12500


def test_parse_lrc_centiseconds_vs_milliseconds():
    # 2-digit frac = centiseconds (34 → 340ms); 3-digit = milliseconds (345 → 345ms)
    out = parse_lrc("[00:00.34]a\n[00:00.345]b")
    assert out[0]["t_ms"] == 340 and out[1]["t_ms"] == 345


def test_parse_lrc_skips_metadata_and_malformed():
    lrc = "[ar:The Band]\n[ti:Song]\nnot a timestamp\n[00:10.00]real line"
    out = parse_lrc(lrc)
    assert out == [{"t_ms": 10000, "line": "real line"}]


def test_parse_lrc_multiple_timestamps_one_line():
    out = parse_lrc("[00:01.00][00:31.00]chorus")
    assert out == [{"t_ms": 1000, "line": "chorus"}, {"t_ms": 31000, "line": "chorus"}]


def test_parse_lrc_empty_or_none():
    assert parse_lrc(None) == []
    assert parse_lrc("") == []


def test_match_norm_strips_accents_punctuation_case():
    assert _match_norm("Motörhead") == "motorhead"
    assert _match_norm("Don't Stop!") == "dontstop"
    assert _match_norm(None) == ""


# ── fetch_lyrics (respx) ──────────────────────────────────────────────────────

def _get_resp(json):
    return httpx.Response(200, json=json)


async def test_fetch_lyrics_get_synced(respx_mock):
    respx_mock.get("https://lrclib.net/api/get").mock(return_value=_get_resp({
        "trackName": "Song", "artistName": "Artist", "instrumental": False,
        "syncedLyrics": "[00:01.00]hello\n[00:05.00]world", "plainLyrics": "hello\nworld",
    }))
    r = await fetch_lyrics("Artist", "Song", "Album", 200)
    assert r["available"] is True and r["instrumental"] is False
    assert r["synced"] == [{"t_ms": 1000, "line": "hello"}, {"t_ms": 5000, "line": "world"}]


async def test_fetch_lyrics_get_plain_only(respx_mock):
    respx_mock.get("https://lrclib.net/api/get").mock(return_value=_get_resp({
        "instrumental": False, "syncedLyrics": None, "plainLyrics": "just words",
    }))
    r = await fetch_lyrics("Artist", "Song", "Album", 200)
    assert r["available"] is True and r["synced"] is None and r["plain"] == "just words"


async def test_fetch_lyrics_instrumental(respx_mock):
    respx_mock.get("https://lrclib.net/api/get").mock(return_value=_get_resp({
        "instrumental": True, "syncedLyrics": None, "plainLyrics": None,
    }))
    r = await fetch_lyrics("Artist", "Song", "Album", 200)
    assert r == {"available": True, "instrumental": True, "synced": None, "plain": None}


async def test_fetch_lyrics_search_fallback_requires_title_artist_match(respx_mock):
    """Covers R10: a search hit with matching duration but WRONG title is rejected."""
    respx_mock.get("https://lrclib.net/api/get").mock(return_value=httpx.Response(404))
    respx_mock.get("https://lrclib.net/api/search").mock(return_value=_get_resp([
        {"trackName": "Totally Different", "artistName": "Artist", "duration": 200,
         "plainLyrics": "wrong", "syncedLyrics": None},
    ]))
    r = await fetch_lyrics("Artist", "Song", "Album", 200)
    assert r["available"] is False  # duration matched but title didn't → no wrong-song lyrics


async def test_fetch_lyrics_search_picks_closest_duration_match(respx_mock):
    respx_mock.get("https://lrclib.net/api/get").mock(return_value=httpx.Response(404))
    respx_mock.get("https://lrclib.net/api/search").mock(return_value=_get_resp([
        {"trackName": "Song", "artistName": "Artist", "duration": 198, "plainLyrics": "near", "syncedLyrics": None},
        {"trackName": "Song", "artistName": "Artist", "duration": 200, "plainLyrics": "exact", "syncedLyrics": None},
        {"trackName": "Song", "artistName": "Artist", "duration": 240, "plainLyrics": "toofar", "syncedLyrics": None},
    ]))
    r = await fetch_lyrics("Artist", "Song", "Album", 200)
    assert r["available"] is True and r["plain"] == "exact"


async def test_fetch_lyrics_no_match_returns_miss(respx_mock):
    """A definitive 'LRCLIB has nothing' carries no_match=True so the endpoint can
    tell a confirmed miss apart from a transient failure (contribute-prompt plan U1)."""
    respx_mock.get("https://lrclib.net/api/get").mock(return_value=httpx.Response(404))
    respx_mock.get("https://lrclib.net/api/search").mock(return_value=_get_resp([]))
    r = await fetch_lyrics("Artist", "Song", "Album", 200)
    assert r == {"available": False, "instrumental": False, "synced": None,
                 "plain": None, "no_match": True}


async def test_fetch_lyrics_empty_record_is_confirmed_miss(respx_mock):
    """A record with neither synced nor plain lyrics is a confirmed no-usable-lyrics
    (no_match=True), not a transient failure."""
    respx_mock.get("https://lrclib.net/api/get").mock(return_value=_get_resp({
        "instrumental": False, "syncedLyrics": None, "plainLyrics": None,
    }))
    r = await fetch_lyrics("Artist", "Song", "Album", 200)
    assert r["available"] is False and r["no_match"] is True


async def test_fetch_lyrics_present_lyrics_not_flagged_no_match(respx_mock):
    """A real lyrics hit is never flagged as a no-match."""
    respx_mock.get("https://lrclib.net/api/get").mock(return_value=_get_resp({
        "instrumental": False, "syncedLyrics": None, "plainLyrics": "words",
    }))
    r = await fetch_lyrics("Artist", "Song", "Album", 200)
    assert r["available"] is True and r.get("no_match") is not True


async def test_fetch_lyrics_timeout_raises_transient(respx_mock):
    """A timeout is TRANSIENT — fetch_lyrics raises LyricsFetchError so the
    endpoint can return an uncached miss (a slow response must not be cached as
    a permanent no-lyrics — the 2026-06-18 bug)."""
    respx_mock.get("https://lrclib.net/api/get").mock(side_effect=httpx.TimeoutException("slow"))
    with pytest.raises(LyricsFetchError):
        await fetch_lyrics("Artist", "Song", "Album", 200)


async def test_fetch_lyrics_rate_limited_raises_transient(respx_mock):
    """429 (and 5xx) are transient — raise, don't cache."""
    respx_mock.get("https://lrclib.net/api/get").mock(return_value=httpx.Response(429))
    with pytest.raises(LyricsFetchError):
        await fetch_lyrics("Artist", "Song", "Album", 200)


async def test_fetch_lyrics_server_error_raises_transient(respx_mock):
    """A 5xx on /api/get is transient (LRCLIB hiccup), not a definitive miss."""
    respx_mock.get("https://lrclib.net/api/get").mock(return_value=httpx.Response(503))
    with pytest.raises(LyricsFetchError):
        await fetch_lyrics("Artist", "Song", "Album", 200)


def test_timeout_accommodates_real_lrclib_latency():
    """Regression pin for the 2026-06-18 no-lyrics bug: LRCLIB's real-world
    latency runs ~6s on a cold connection, so the original 3s timeout failed
    soft on every lookup. The timeout must stay generous enough to actually get
    an answer (the fetch is async, so this never stalls the now-view)."""
    assert _TIMEOUT >= 8, (
        f"_TIMEOUT={_TIMEOUT} is too short for LRCLIB's ~6s real latency; lookups "
        f"will time out and every track will silently show no lyrics."
    )
