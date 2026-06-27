"""Tests for the shared lyric cache + get_or_fetch helper (plan 2026-06-18-001 U1).

fetch_lyrics is patched (the cache calls it); these tests exercise the cache's
own logic: caching definitive answers, NOT caching transient failures, and the
in-flight dedup guard. asyncio_mode is auto, so `async def` tests run directly."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.lyrics import cache as lyrics_cache
from app.lyrics.client import LyricsFetchError

_LYRICS = {"available": True, "instrumental": False, "synced": [{"t_ms": 0, "line": "hi"}], "plain": None}
# The cache's transient-failure fallback shape — no_match=False (couldn't check),
# distinct from a definitive miss (no_match=True) returned by fetch_lyrics.
_MISS = {"available": False, "instrumental": False, "synced": None, "plain": None, "no_match": False}
_DEF_MISS = {"available": False, "instrumental": False, "synced": None, "plain": None, "no_match": True}


@pytest.fixture(autouse=True)
def _clear_cache():
    """The cache is module-level (shared across the process); clear it so tests
    don't bleed into each other."""
    lyrics_cache._CACHE.clear()
    lyrics_cache._INFLIGHT.clear()
    yield
    lyrics_cache._CACHE.clear()
    lyrics_cache._INFLIGHT.clear()


def test_cached_returns_none_on_miss():
    assert lyrics_cache.cached("nope") is None


async def test_get_or_fetch_caches_definitive_lyrics():
    fl = AsyncMock(return_value=dict(_LYRICS))
    with patch("app.lyrics.client.fetch_lyrics", fl):
        r1 = await lyrics_cache.get_or_fetch("t1", "A", "Song", "Alb", 200)
        r2 = await lyrics_cache.get_or_fetch("t1", "A", "Song", "Alb", 200)
    assert r1["available"] is True and r2["available"] is True
    assert fl.call_count == 1                      # second call is a cache hit
    assert lyrics_cache.cached("t1") is not None


async def test_get_or_fetch_caches_definitive_miss():
    """A genuine 'no match' (available:false, no_match:true) IS cached — it's a
    definitive answer, and the no_match marker is preserved through the cache."""
    fl = AsyncMock(return_value=dict(_DEF_MISS))
    with patch("app.lyrics.client.fetch_lyrics", fl):
        await lyrics_cache.get_or_fetch("t2", "A", "Song", "Alb", 200)
        await lyrics_cache.get_or_fetch("t2", "A", "Song", "Alb", 200)
    assert fl.call_count == 1                      # negative caching
    assert lyrics_cache.cached("t2") == _DEF_MISS


async def test_get_or_fetch_transient_failure_not_cached():
    """A LyricsFetchError (timeout/network/429/5xx) yields an UNCACHED miss, so a
    later call retries instead of being stuck on a permanent no-lyrics."""
    fl = AsyncMock(side_effect=LyricsFetchError("timeout"))
    with patch("app.lyrics.client.fetch_lyrics", fl):
        r1 = await lyrics_cache.get_or_fetch("t3", "A", "Song", "Alb", 200)
        r2 = await lyrics_cache.get_or_fetch("t3", "A", "Song", "Alb", 200)
    assert r1 == _MISS and r2 == _MISS
    assert fl.call_count == 2                      # NOT cached → re-fetched
    assert lyrics_cache.cached("t3") is None


async def test_get_or_fetch_dedupes_concurrent_calls():
    """Two concurrent lookups for the same track share one LRCLIB call (R4)."""
    release = asyncio.Event()

    async def slow_fetch(artist, title, album, duration_s):
        await release.wait()
        return dict(_LYRICS)

    fl = AsyncMock(side_effect=slow_fetch)
    with patch("app.lyrics.client.fetch_lyrics", fl):
        ta = asyncio.ensure_future(lyrics_cache.get_or_fetch("t4", "A", "S", "Al", 200))
        await asyncio.sleep(0.02)                  # let A register the in-flight task
        tb = asyncio.ensure_future(lyrics_cache.get_or_fetch("t4", "A", "S", "Al", 200))
        await asyncio.sleep(0.02)
        release.set()
        ra, rb = await asyncio.gather(ta, tb)
    assert ra["available"] is True and rb["available"] is True
    assert fl.call_count == 1                      # collapsed into one fetch
