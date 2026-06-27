"""LRCLIB lyrics client (Now Playing → Lyrics, plan 2026-06-17-008 U1).

Fetches lyrics from LRCLIB (lrclib.net) — free, no auth, time-synced LRC with
plain fallback. `fetch_lyrics` returns a result dict for a *definitive* answer
(including a definitive "LRCLIB has no match" → available=False), and raises
`LyricsFetchError` for a *transient* failure (timeout / network / 429 / 5xx /
malformed response). The caller (the /api/lyrics endpoint) turns either into a
silent no-lyrics UI, but only caches the definitive answer — a transient slow
response must not be cached as a permanent miss (the 2026-06-18 no-lyrics bug,
where a 3s timeout made every lookup fail soft *and* get cached). The
/api/search fallback requires a normalized title+artist match (not duration
alone) before accepting a result — the wrong-song guard (R10).
"""
import asyncio
import re
import unicodedata

import httpx

from app.lyrics.lrc import parse_lrc

_BASE = "https://lrclib.net"
# LRCLIB's real-world latency runs ~6s on a cold connection (DNS + TLS + server
# processing); the original 3s timed out on *every* lookup and failed soft to
# "no lyrics" — no track ever showed a lyric pill (2026-06-18 bug). Still well
# under the Plex client's 15s, and the fetch is fully async (it never blocks the
# now-view; the pill just appears a few seconds in), so a generous bound is safe.
_TIMEOUT = 10.0
_DUR_TOL = 2            # seconds; LRCLIB's own /api/get gate is also ±2s

# A DEFINITIVE "LRCLIB has no usable lyrics for this track" — no_match=True marks
# it as a confirmed miss (distinct from the cache/endpoint transient MISS, which is
# no_match=False). The /api/lyrics endpoint uses this to decide whether to offer the
# "contribute lyrics" prompt (contribute-prompt plan, 2026-06-23 U1).
_MISS = {"available": False, "instrumental": False, "synced": None, "plain": None, "no_match": True}


class LyricsFetchError(Exception):
    """A TRANSIENT failure reaching LRCLIB (timeout / network / 429 / 5xx /
    malformed response) — distinct from a definitive "LRCLIB has no match".
    The endpoint catches this and returns an UNCACHED miss so a later play
    retries, instead of caching one slow response as a permanent no-lyrics."""


def _match_norm(s: str | None) -> str:
    """Aggressive comparison key for title/artist matching: drop accents +
    punctuation + case + spacing so 'Motörhead' == 'motorhead' and
    "Don't" == 'dont'. Comparison-only, never displayed."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _to_result(rec: dict) -> dict:
    if rec.get("instrumental"):
        return {"available": True, "instrumental": True, "synced": None, "plain": None}
    synced = parse_lrc(rec.get("syncedLyrics")) or None
    plain = rec.get("plainLyrics") or None
    if not synced and not plain:
        return dict(_MISS)
    return {"available": True, "instrumental": False, "synced": synced, "plain": plain}


async def _try_get(http: httpx.AsyncClient, artist, title, album, duration_s) -> dict | None:
    params = {"artist_name": artist or "", "track_name": title or "", "album_name": album or ""}
    if duration_s:
        params["duration"] = int(round(duration_s))
    r = await http.get(f"{_BASE}/api/get", params=params)
    if r.status_code == 200:
        return r.json()
    if r.status_code == 404:
        return None                         # no exact match → caller tries search
    raise LyricsFetchError(f"/api/get {r.status_code}")   # 429 / 5xx → transient


async def _try_search(http: httpx.AsyncClient, artist, title, duration_s) -> dict | None:
    r = await http.get(f"{_BASE}/api/search",
                       params={"track_name": title or "", "artist_name": artist or ""})
    if r.status_code != 200:
        raise LyricsFetchError(f"/api/search {r.status_code}")  # 429 / 5xx → transient
    nt, na = _match_norm(title), _match_norm(artist)
    best, best_delta = None, None
    for rec in (r.json() or []):
        # Wrong-song guard: require a normalized title AND artist match.
        if _match_norm(rec.get("trackName")) != nt or _match_norm(rec.get("artistName")) != na:
            continue
        d = rec.get("duration")
        if duration_s and d is not None:
            delta = abs(d - duration_s)
            if delta > _DUR_TOL:
                continue
        else:
            delta = 0
        if best_delta is None or delta < best_delta:
            best, best_delta = rec, delta
    return best


_client: httpx.AsyncClient | None = None
_client_loop: asyncio.AbstractEventLoop | None = None


def _shared_client() -> httpx.AsyncClient:
    """One reusable AsyncClient (connection pool) so the prefetcher's sequential
    lookups don't each pay a fresh TLS handshake (code-review #7). Recreated if
    the running event loop changed — keeps test isolation across per-test loops;
    in production there's a single uvicorn loop so it's a process singleton."""
    global _client, _client_loop
    loop = asyncio.get_running_loop()
    if _client is None or _client.is_closed or _client_loop is not loop:
        _client = httpx.AsyncClient(timeout=_TIMEOUT)
        _client_loop = loop
    return _client


async def fetch_lyrics(artist: str | None, title: str | None,
                       album: str | None, duration_s: float | None) -> dict:
    """Look up lyrics for a track. Returns
    ``{available, instrumental, synced: [{t_ms,line}]|None, plain: str|None}``
    for a *definitive* answer — including a definitive "no match" (available
    False), which the caller may safely cache.

    Raises ``LyricsFetchError`` on a *transient* failure (timeout / network /
    429 / 5xx / malformed response) so the caller can return a miss WITHOUT
    caching it — a slow or flaky response must not poison a track permanently."""
    try:
        http = _shared_client()
        rec = await _try_get(http, artist, title, album, duration_s)
        if rec is None:
            rec = await _try_search(http, artist, title, duration_s)
    except (httpx.HTTPError, ValueError) as e:   # timeout/connect/read, or bad JSON
        raise LyricsFetchError(str(e)) from e
    return _to_result(rec) if rec is not None else dict(_MISS)
