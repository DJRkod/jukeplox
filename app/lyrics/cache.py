"""Shared in-memory lyric cache (plan 2026-06-18-001 U1).

The one home for the lyric cache, used by BOTH the on-demand `/api/lyrics`
endpoint (`app/api/guest.py`) and the rolling-window prefetcher
(`app/lyrics/prefetch.py`, driven from `app/state.py`). It lives here — not in
the guest API module — because the prefetcher is driven from `app/state.py` and
`app/api/guest.py` already imports `state`; sharing the cache in place would be a
circular import. This module imports neither, so both can import it.

Cache semantics (the 2026-06-18 no-lyrics bug taught these): a DEFINITIVE answer
from LRCLIB — lyrics, instrumental, or a genuine "no match" (`available: false`)
— is cached. A TRANSIENT failure (timeout / network / 429 / 5xx, surfaced as
`LyricsFetchError`) is NOT cached, so a later play retries instead of being stuck
on one slow response. An in-flight guard collapses concurrent lookups for the
same track into a single LRCLIB call (a prefetch racing the on-demand endpoint,
or rapid queue reorders).
"""
from __future__ import annotations

import asyncio

_CACHE: dict[str, dict] = {}
_CACHE_CAP = 512
_INFLIGHT: dict[str, asyncio.Future] = {}

# The TRANSIENT-failure fallback (timeout / network / 429 / 5xx, or the endpoint's
# no-Plex / resolve-fail). no_match=False distinguishes "couldn't check right now"
# from a DEFINITIVE no-match (app/lyrics/client.py _MISS, no_match=True) — only the
# latter offers the "contribute lyrics" prompt (contribute-prompt plan 2026-06-23 U1).
MISS = {"available": False, "instrumental": False, "synced": None, "plain": None, "no_match": False}


def cached(track_id: str) -> dict | None:
    """Return the cached result for a track, or None on a cache miss."""
    return _CACHE.get(track_id)


def _cache_put(track_id: str, result: dict) -> None:
    if track_id in _CACHE:
        return
    if len(_CACHE) >= _CACHE_CAP:
        _CACHE.pop(next(iter(_CACHE)), None)  # FIFO eviction
    _CACHE[track_id] = result


async def _do_fetch(track_id: str, artist, title, album, duration_s) -> dict:
    # Late import keeps `app.lyrics.client.fetch_lyrics` patchable in tests and
    # avoids binding the name at module load. fetch_lyrics raises
    # LyricsFetchError on a transient failure (which propagates → the task fails
    # → nothing is cached); a definitive result (incl. an available:false miss)
    # is returned and cached here.
    from app.lyrics.client import fetch_lyrics
    result = await fetch_lyrics(artist, title, album, duration_s)
    _cache_put(track_id, result)
    return result


async def get_or_fetch(track_id: str, artist, title, album, duration_s) -> dict:
    """Return lyrics for a track from the cache, or fetch + cache them.

    Returns a result dict and never raises: a transient failure yields an
    UNCACHED ``MISS`` so the next lookup retries. Concurrent calls for the same
    ``track_id`` share one fetch (in-flight guard)."""
    hit = _CACHE.get(track_id)
    if hit is not None:
        return hit

    task = _INFLIGHT.get(track_id)
    if task is None:
        task = asyncio.ensure_future(
            _do_fetch(track_id, artist, title, album, duration_s)
        )
        _INFLIGHT[track_id] = task
        task.add_done_callback(lambda _t, k=track_id: _INFLIGHT.pop(k, None))

    try:
        # shield so a cancelled awaiter can't cancel the shared fetch task.
        return await asyncio.shield(task)
    except Exception:
        return dict(MISS)  # transient/any failure → uncached miss (task cached nothing)
