"""Async Plex Media Server client.

Provides library enumeration, track/album/artist metadata, stream URL
construction, and an album-art proxy helper.  Plex is treated as a read-only
metadata and media source; all playback happens elsewhere.

Compound key format
-------------------
When a machine_id is set on PlexClient, all returned resource identifiers
(section keys, rating keys, thumb/stream paths) are prefixed with
"{machine_id}:" so they remain globally unique across multiple servers.
Methods that accept these identifiers strip the prefix before calling the
Plex API.  MultiPlexClient routes each call to the correct sub-client by
splitting on the first ":".
"""

import asyncio
import json
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.plex.models import Album, Artist, Library, SearchResults, Track

_TYPO_TABLE = str.maketrans({
    '‘': "'",  # left single quotation mark
    '’': "'",  # right single quotation mark / typographic apostrophe
    '‚': "'",  # single low-9 quotation mark
    '‛': "'",  # single high-reversed-9 quotation mark
    '“': '"',  # left double quotation mark
    '”': '"',  # right double quotation mark
    '„': '"',  # double low-9 quotation mark
    '—': '-',  # em dash
    '–': '-',  # en dash
    '…': '...',  # horizontal ellipsis
    '‒': '-',  # figure dash
    '―': '-',  # horizontal bar
})


def _normalize_text(s: str) -> str:
    """Fold typographic punctuation to ASCII equivalents for comparison only."""
    return unicodedata.normalize('NFC', s).translate(_TYPO_TABLE).lower()


def browse_base_key(s: str | None) -> str:
    """Rule-INDEPENDENT base normalization for the browse-index cross-server
    merge key (2026-06-21 plan U1/U2): typographic fold + case + strip, with NO
    pattern rules. It is a refinement of the rule-aware app.normalize.normalize —
    two strings equal under this key are always equal under the rule-aware norm —
    so the index can group by base key while the rule layer is applied at request
    time (plan R11: pattern-rule edits never require an index rebuild)."""
    return _normalize_text(s or "").strip()


def _parse_child_count(value) -> int | None:
    """Plex's childCount as a non-negative int, or None when absent/unusable.

    A missing or malformed value degrades to "no count shown" (2026-06-09
    rail plan R15) — never an exception, never a 0 display.
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None

# Plex media type constants
TYPE_ARTIST = 8
TYPE_ALBUM = 9
TYPE_TRACK = 10

_CACHE_TTL = 300  # 5 minutes

# Per-hub result cap for /hubs/search (`limit` applies per hub). Results are
# relevance-ranked, so truncation keeps the best matches. The legacy section
# endpoint returned full pages; this makes list depth a deliberate choice.
# (A 2026-06-14 experiment confirmed raising this does NOT surface non-leading
# title-substring matches — those are absent from hub ranking, not truncated;
# the broad literal Tier 2 covers them instead. Cap kept at the baseline 30.)
_SEARCH_HUB_LIMIT = 30


@dataclass
class _CacheEntry:
    value: Any
    expires_at: float = field(default_factory=lambda: time.monotonic() + _CACHE_TTL)

    @property
    def valid(self) -> bool:
        return time.monotonic() < self.expires_at


class PlexClient:
    def __init__(
        self,
        server_url: str,
        token: str,
        client_id: str,
        machine_id: str = "",
        server_name: str = "",
        owner: str = "",
        max_concurrency: int | None = None,
    ):
        self.server_url = server_url.rstrip("/")
        self.token = token
        self.client_id = client_id
        self.machine_id = machine_id
        self.server_name = server_name
        self.owner = owner
        self._cache: dict[str, _CacheEntry] = {}
        # Per-server concurrency ceiling (gentle-on-Plex, 2026-06-14 plan U1).
        # The semaphore is the ENFORCING cap — it lives in our code, so it is
        # deterministically testable and never lets more than `cap` requests
        # into the pool at once (which also means the pool never queues, so no
        # PoolTimeout tuning). The httpx Limits below are a belt-and-suspenders
        # pool cap for any direct httpx use. Both default to settings.
        # plex_max_concurrency. Rationale + the "why not httpx-limits-only"
        # decision: docs/solutions/best-practices/
        # concurrency-cap-semaphore-not-httpx-pool-limits.md
        if max_concurrency is None:
            from app.config import settings
            max_concurrency = settings.plex_max_concurrency
        self._sem = asyncio.Semaphore(max_concurrency)
        # connect=5 split (2026-07-17 ce-debug): a blackholed server (SYN
        # never answered — dead box, moved IP, filtered port) must fail the
        # CONNECT phase in seconds, not burn the full 15s read budget. The
        # read budget stays 15s for slow-but-alive PMS responses.
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(15, connect=5),
            http2=False,
            limits=httpx.Limits(
                max_connections=max_concurrency,
                max_keepalive_connections=max_concurrency,
            ),
        )

    # ── internal helpers ──────────────────────────────────────────────────────

    def _headers(self) -> dict:
        return {
            "X-Plex-Token": self.token,
            "X-Plex-Client-Identifier": self.client_id,
            "Accept": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self.server_url}{path}"

    @staticmethod
    def _check_status(resp) -> None:
        """Shared status contract for _get and _get_raw so the two paths can't
        drift: a 401 becomes a typed PlexAuthError (drives the auth re-prompt),
        any other 4xx/5xx raises via raise_for_status."""
        if resp.status_code == 401:
            from app.plex.auth import PlexAuthError
            raise PlexAuthError("Plex token rejected (401)")
        resp.raise_for_status()

    async def _get(self, path: str, params: dict | None = None) -> dict:
        async with self._sem:
            resp = await self._http.get(
                self._url(path), headers=self._headers(), params=params or {}
            )
        self._check_status(resp)
        return resp.json()

    async def _get_raw(self, path: str, params: dict | None = None) -> bytes:
        """Fetch a response body as raw bytes, same status contract as _get but
        WITHOUT decoding JSON on the event loop (fix U3). Whole-library callers
        hand the bytes to run_in_executor so the (large) json.loads + object
        build runs off-loop. The semaphore wraps only the I/O; the executor parse
        happens after it releases, so the cap governs sockets, not thread time."""
        async with self._sem:
            resp = await self._http.get(
                self._url(path), headers=self._headers(), params=params or {}
            )
        self._check_status(resp)
        return resp.content

    def _cached(self, key: str) -> Any | None:
        entry = self._cache.get(key)
        return entry.value if entry and entry.valid else None

    def _store(self, key: str, value: Any) -> None:
        self._cache[key] = _CacheEntry(value=value)

    def invalidate_cache(self) -> None:
        self._cache.clear()

    # ── compound key helpers ──────────────────────────────────────────────────

    def _prefix(self, path: str | None) -> str | None:
        if not path or not self.machine_id:
            return path
        return f"{self.machine_id}:{path}"

    def _strip(self, key: str | None) -> str | None:
        if not key or not self.machine_id:
            return key
        prefix = f"{self.machine_id}:"
        return key[len(prefix):] if key.startswith(prefix) else key

    def _bare_rating_key(self, key: str | None) -> str | None:
        # Returns the substring after the first ":" so cross-server compound
        # ids ("machineB:42") become bare numeric ratingKeys Plex accepts.
        # _strip handles same-server; this handles whatever _strip left behind.
        if not key or ":" not in key:
            return key
        return key.split(":", 1)[1]

    def _make_id(self, rating_key: str) -> str:
        return f"{self.machine_id}:{rating_key}" if self.machine_id else rating_key

    # ── stream URL ────────────────────────────────────────────────────────────

    def stream_url(self, stream_key: str) -> str:
        """Build the full HTTP URL to stream a media part."""
        part_key = self._strip(stream_key) or stream_key
        return f"{self.server_url}{part_key}?X-Plex-Token={self.token}"

    # ── libraries ─────────────────────────────────────────────────────────────

    async def get_libraries(self) -> list[Library]:
        cached = self._cached("libraries")
        if cached is not None:
            return cached
        data = await self._get("/library/sections")
        libs = [
            Library(
                key=self._make_id(sec["key"]),
                title=sec["title"],
                type=sec["type"],
                owner=self.owner,
                server_name=self.server_name,
            )
            for sec in data.get("MediaContainer", {}).get("Directory", [])
            if sec.get("type") == "artist"
        ]
        self._store("libraries", libs)
        return libs

    # ── artists ───────────────────────────────────────────────────────────────

    async def get_artists(self, section_key: str) -> list[Artist]:
        bare_key = self._strip(section_key) or section_key
        cache_key = f"artists:{bare_key}"
        cached = self._cached(cache_key)
        if cached is not None:
            return cached
        raw = await self._get_raw(
            f"/library/sections/{bare_key}/all", params={"type": TYPE_ARTIST}
        )
        artists = await asyncio.get_running_loop().run_in_executor(
            None, self._parse_artists, raw
        )
        self._store(cache_key, artists)
        return artists

    # ── albums ────────────────────────────────────────────────────────────────

    async def get_albums(
        self,
        section_key: str,
        artist_id: str | None = None,
        year: int | None = None,
        style: str | None = None,
    ) -> list[Album]:
        bare_key = self._strip(section_key) or section_key
        bare_artist = self._bare_rating_key(self._strip(artist_id))

        if bare_artist:
            cache_key = f"albums:{bare_key}:{bare_artist}"
            cached = self._cached(cache_key)
            if cached is not None:
                return cached
            # Use section-all with parentRatingKey to capture all release types
            # (EPs, singles, live, etc.). /children only returns primary albums
            # in Plex Music Agent v2. Filter client-side because Plex may ignore
            # the server-side parentRatingKey parameter.
            # NOTE (2026-06-21 browse-index plan U5): this is now the FALLBACK
            # path. The warm browse drill-in serves an artist's releases from the
            # persistent index (app/state.py crawl + app/api/guest.py U4), so the
            # whole-section transfer here only happens on a cold/missed index.
            # The client-side filter + all-subtype capture below are pinned by
            # tests so a future "optimization" to /children can't silently drop
            # EPs/singles/live.
            data = await self._get(
                f"/library/sections/{bare_key}/all",
                params={"type": TYPE_ALBUM, "parentRatingKey": bare_artist},
            )
            all_items = data.get("MediaContainer", {}).get("Metadata", [])
            items = [
                item for item in all_items
                if str(item.get("parentRatingKey", "")) == bare_artist
            ]
            albums = [
                Album(
                    id=self._make_id(item["ratingKey"]),
                    title=item["title"],
                    artist=item.get("parentTitle", ""),
                    year=item.get("year"),
                    thumb=self._prefix(item.get("thumb")),
                    subtype=item.get("subtype"),
                    added_at=item.get("addedAt"),
                    track_count=_parse_child_count(item.get("leafCount") or item.get("childCount")),
                )
                for item in items
            ]
            albums.sort(key=lambda a: a.year or 0)
            self._store(cache_key, albums)
            return albums

        params: dict = {"type": TYPE_ALBUM}
        if style:
            params["style"] = style
            cache_key = f"albums:{bare_key}:style:{style}"
        elif year:
            params["year"] = str(year)
            cache_key = f"albums:{bare_key}:year:{year}"
        else:
            cache_key = f"albums:{bare_key}:all"
        cached = self._cached(cache_key)
        if cached is not None:
            return cached
        raw = await self._get_raw(f"/library/sections/{bare_key}/all", params=params)
        albums = await asyncio.get_running_loop().run_in_executor(
            None, self._parse_albums, raw
        )
        self._store(cache_key, albums)
        return albums

    # ── tracks ────────────────────────────────────────────────────────────────

    def _parse_track(self, item: dict) -> Track:
        part_key = ""
        media_list = item.get("Media", [])
        if media_list:
            parts = media_list[0].get("Part", [])
            if parts:
                part_key = parts[0].get("key", "")
        return Track(
            id=self._make_id(item["ratingKey"]),
            title=item["title"],
            # Per-track credit wins (2026-06-10 plan U1): on compilations Plex
            # carries the credited act in originalTitle and "Various Artists"
            # in grandparentTitle. Value-aware `or`, not dict-key fallback —
            # an empty originalTitle must still yield the release artist.
            artist=item.get("originalTitle") or item.get("grandparentTitle", ""),
            album=item.get("parentTitle", ""),
            duration_ms=item.get("duration", 0),
            genre=item.get("Genre", [{}])[0].get("tag") if item.get("Genre") else None,
            year=item.get("year") or item.get("parentYear"),
            thumb=self._prefix(item.get("thumb") or item.get("parentThumb")),
            stream_key=self._prefix(part_key) or part_key,
            server_name=self.server_name,
            album_artist=item.get("grandparentTitle", ""),
            album_id=self._make_id(item["parentRatingKey"]) if item.get("parentRatingKey") else None,
            # Multi-disc ordering (2026-06-11): parentIndex = disc, index =
            # track number. `or 1` (not dict default) — Plex can send null.
            disc_number=item.get("parentIndex") or 1,
            track_number=item.get("index"),
        )

    # ── off-loop whole-library parsers (2026-07-29 fix U3) ────────────────────
    # Run on a run_in_executor worker thread: json.loads of a whole-library
    # payload plus building thousands of dataclasses is CPU-bound work that would
    # otherwise freeze the event loop (starving transport controls / position
    # polls) for tens of seconds on a weak host. These mirror the previous inline
    # comprehensions exactly — pure relocation of where the CPU runs.

    def _parse_tracks(self, raw: bytes) -> list[Track]:
        data = json.loads(raw)
        return [
            self._parse_track(item)
            for item in data.get("MediaContainer", {}).get("Metadata", [])
        ]

    def _parse_artists(self, raw: bytes) -> list[Artist]:
        data = json.loads(raw)
        return [
            Artist(
                id=self._make_id(item["ratingKey"]),
                title=item["title"],
                thumb=self._prefix(item.get("thumb")),
                release_count=_parse_child_count(item.get("childCount")),
            )
            for item in data.get("MediaContainer", {}).get("Metadata", [])
        ]

    def _parse_albums(self, raw: bytes) -> list[Album]:
        data = json.loads(raw)
        return [
            Album(
                id=self._make_id(item["ratingKey"]),
                title=item["title"],
                artist=item.get("parentTitle", ""),
                year=item.get("year"),
                thumb=self._prefix(item.get("thumb")),
                subtype=item.get("subtype"),
                added_at=item.get("addedAt"),
                track_count=_parse_child_count(item.get("leafCount") or item.get("childCount")),
            )
            for item in data.get("MediaContainer", {}).get("Metadata", [])
        ]

    async def get_tracks(
        self,
        section_key: str,
        album_id: str | None = None,
        genre: str | None = None,
        year: int | None = None,
    ) -> list[Track]:
        bare_key = self._strip(section_key) or section_key
        bare_album = self._strip(album_id)

        if bare_album:
            # Children endpoint is reliable; section-all with parentRatingKey is not.
            cache_key = f"tracks:{bare_key}:{bare_album}"
            cached = self._cached(cache_key)
            if cached is not None:
                return cached
            data = await self._get(f"/library/metadata/{bare_album}/children")
            tracks = [
                self._parse_track(item)
                for item in data.get("MediaContainer", {}).get("Metadata", [])
                if item.get("type") == "track"
            ]
            # Determinism guarantee (multi-disc reqs R5): Plex's children
            # order is already disc-then-track, but don't trust upstream —
            # albums with per-disc numbering restart NEED the disc key.
            tracks.sort(key=lambda t: (t.disc_number, t.track_number if t.track_number is not None else 0))
            self._store(cache_key, tracks)
            return tracks

        params: dict = {"type": TYPE_TRACK}
        cache_key = f"tracks:{bare_key}"
        if genre:
            params["genre"] = genre
            cache_key += f":genre:{genre}"
        if year:
            params["year"] = str(year)
            cache_key += f":year:{year}"
        cached = self._cached(cache_key)
        if cached is not None:
            return cached
        raw = await self._get_raw(f"/library/sections/{bare_key}/all", params=params)
        tracks = await asyncio.get_running_loop().run_in_executor(
            None, self._parse_tracks, raw
        )
        self._store(cache_key, tracks)
        return tracks

    async def get_track(self, track_id: str) -> Track:
        bare_id = self._strip(track_id) or track_id
        data = await self._get(f"/library/metadata/{bare_id}")
        items = data.get("MediaContainer", {}).get("Metadata", [])
        if not items:
            raise KeyError(f"Track {track_id} not found")
        return self._parse_track(items[0])

    async def get_album(self, album_id: str) -> Album:
        # For an album metadata item, parentTitle is the artist (unlike tracks
        # where grandparentTitle holds the artist because the parent is the
        # album). Mirrors the Album parsing pattern in get_albums.
        bare_id = self._bare_rating_key(self._strip(album_id)) or album_id
        data = await self._get(f"/library/metadata/{bare_id}")
        items = data.get("MediaContainer", {}).get("Metadata", [])
        if not items:
            raise KeyError(f"Album {album_id} not found")
        item = items[0]
        return Album(
            id=self._make_id(item["ratingKey"]),
            title=item["title"],
            artist=item.get("parentTitle", ""),
            year=item.get("year"),
            thumb=self._prefix(item.get("thumb")),
            subtype=item.get("subtype"),
            track_count=_parse_child_count(item.get("leafCount") or item.get("childCount")),
        )

    # ── similarity (Surprise Me) ──────────────────────────────────────────────

    async def get_sonic_nearest(
        self, track_id: str, limit: int = 10, max_distance: float = 0.35,
    ) -> list[Track]:
        """Sonically-similar tracks to ``track_id`` via Plex sonic analysis.

        Requires the server to have run sonic analysis (a Plex Pass feature); when
        that data is absent Plex returns an empty MediaContainer. Fails safe to
        ``[]`` on any error so the Surprise Me chain degrades to the next source.
        """
        bare = self._bare_rating_key(self._strip(track_id)) or track_id
        try:
            data = await self._get(
                f"/library/metadata/{bare}/nearest",
                params={"limit": limit, "maxDistance": max_distance},
            )
        except Exception:
            return []
        # /nearest returns only tracks; parse all items (don't depend on a
        # per-item `type` field, which the section-all track parser also ignores).
        return [
            self._parse_track(item)
            for item in data.get("MediaContainer", {}).get("Metadata", [])
        ]

    async def get_artist_similar_names(self, track_id: str) -> list[str]:
        """Names of artists Plex considers similar to ``track_id``'s artist.

        Sourced from the artist's online-metadata ``<Similar>`` tags (no Plex Pass
        needed); the resolver maps these names onto locally-present artists. Two
        cheap metadata reads (track → grandparentRatingKey → artist). Fails safe
        to ``[]``.
        """
        bare = self._bare_rating_key(self._strip(track_id)) or track_id
        try:
            tdata = await self._get(f"/library/metadata/{bare}")
            titems = tdata.get("MediaContainer", {}).get("Metadata", [])
            artist_key = titems[0].get("grandparentRatingKey") if titems else None
            if not artist_key:
                return []
            adata = await self._get(f"/library/metadata/{artist_key}")
            aitems = adata.get("MediaContainer", {}).get("Metadata", [])
            if not aitems:
                return []
            return [s.get("tag") for s in aitems[0].get("Similar", []) if s.get("tag")]
        except Exception:
            return []

    async def get_artist_popular_tracks(self, artist_id: str) -> list[dict]:
        """Popular tracks for an artist, from Plex's online metadata.

        Plex surfaces per-artist popularity only when the artist is matched to
        its online music database; ``includePopularLeaves`` adds the popular
        track leaves to the artist-metadata read. The leaves are ONLINE-metadata
        items, so their rating keys live in a different keyspace than local
        library track ids — callers match by normalized title, using the rating
        key only as an opportunistic bonus. Returns an ordered list of
        ``{"title": str, "rating_key": str | None}`` (rank = list order), or
        ``[]`` when the artist has no popularity data. Fails safe to ``[]``.

        NOTE: the exact location of the popular leaves in the response varies by
        PMS version (a ``Hub`` of tracks vs a ``popularLeaves`` container); the
        extractor below tolerates the common shapes and must be confirmed against
        a live server (see the plan's Deferred-to-Implementation note).
        """
        bare = self._bare_rating_key(self._strip(artist_id)) or artist_id
        try:
            data = await self._get(
                f"/library/metadata/{bare}",
                params={"includePopularLeaves": 1},
            )
        except Exception:
            return []
        out: list[dict] = []
        for it in self._extract_popular_leaves(data.get("MediaContainer", {})):
            title = it.get("title")
            if not title:
                continue
            rk = it.get("ratingKey")
            out.append({"title": title, "rating_key": str(rk) if rk is not None else None})
        return out

    @staticmethod
    def _extract_popular_leaves(mc: dict) -> list[dict]:
        """Locate popular track leaves in an artist-metadata response.

        Tolerates the shapes PMS uses across versions: a ``popularLeaves``
        container (dict with ``Metadata`` or a bare list) on the artist item, or
        a ``Hub`` of track items at the artist or MediaContainer level. Keeps
        only track-typed leaves (or untyped, when ``type`` is absent)."""
        candidates: list[dict] = []
        meta = mc.get("Metadata", [])
        if meta:
            m0 = meta[0]
            for key in ("popularLeaves", "PopularLeaves"):
                v = m0.get(key)
                if isinstance(v, dict):
                    candidates += v.get("Metadata", []) or []
                elif isinstance(v, list):
                    candidates += v
            for hub in m0.get("Hub", []) or []:
                candidates += hub.get("Metadata", []) or []
        for hub in mc.get("Hub", []) or []:
            candidates += hub.get("Metadata", []) or []
        return [c for c in candidates if c.get("type") in (None, "track")]

    # ── genres / styles / years ───────────────────────────────────────────────

    async def get_genres(self, section_key: str) -> list[str]:
        bare_key = self._strip(section_key) or section_key
        data = await self._get(f"/library/sections/{bare_key}/genre")
        return [
            item["title"]
            for item in data.get("MediaContainer", {}).get("Directory", [])
        ]

    async def get_styles_with_counts(self, section_key: str) -> list[dict]:
        """Return [{name, count}] sorted desc by album count, zeros excluded.

        Uses X-Plex-Container-Size=0 per-style requests to get counts without
        fetching full album payloads. Result is cached for the process lifetime.
        """
        bare_key = self._strip(section_key) or section_key
        cache_key = f"style_counts:{bare_key}"
        cached = self._cached(cache_key)
        if cached is not None:
            return cached

        data = await self._get(f"/library/sections/{bare_key}/style")
        styles = data.get("MediaContainer", {}).get("Directory", [])

        async def _count(style_key: str) -> int:
            try:
                resp = await self._get(
                    f"/library/sections/{bare_key}/all",
                    params={
                        "type": TYPE_ALBUM,
                        "style": style_key,
                        "X-Plex-Container-Size": 0,
                        "X-Plex-Container-Start": 0,
                    },
                )
                return int(resp.get("MediaContainer", {}).get("totalSize", 0))
            except Exception:
                return 0

        counts = await asyncio.gather(*[_count(s["key"]) for s in styles])
        result = [
            {"name": s["title"], "count": n}
            for s, n in zip(styles, counts)
            if n > 0
        ]
        result.sort(key=lambda x: x["count"], reverse=True)
        self._store(cache_key, result)
        return result

    async def get_years(self, section_key: str) -> list[int]:
        bare_key = self._strip(section_key) or section_key
        data = await self._get(f"/library/sections/{bare_key}/year")
        return [
            int(item["title"])
            for item in data.get("MediaContainer", {}).get("Directory", [])
            if item["title"].isdigit()
        ]

    # ── search ────────────────────────────────────────────────────────────────

    async def search(self, section_key: str, query: str) -> SearchResults:
        """Hub search (/hubs/search) — the engine the official Plex apps use.

        The legacy per-section /search endpoint matched typed text literally
        against displayed titles, so "Motorhead" never found "Motörhead";
        hub search folds diacritics, ranks by relevance, and handles
        multi-word and cross-field partial queries natively (all verified
        against a live PMS, 2026-06-10 hub-search plan). Server-side scoping
        params (sectionId/searchTypes) leak other sections on real servers,
        so scoping is client-side: every music hub item carries
        librarySectionID, and anything not provably in the requested section
        is dropped.
        """
        bare_key = self._strip(section_key) or section_key
        data = await self._get(
            "/hubs/search",
            params={"query": _normalize_text(query), "limit": _SEARCH_HUB_LIMIT},
        )
        buckets: dict[str, list] = {"track": [], "album": [], "artist": []}
        for hub in data.get("MediaContainer", {}).get("Hub", []):
            items = buckets.get(hub.get("type"))
            if items is None:
                continue  # movie/show/actor/... hubs — not ours
            for item in hub.get("Metadata", []):
                # librarySectionID is an int in hub payloads; section keys
                # are strings — compare as strings (same treatment as other
                # numeric Plex keys, e.g. parentRatingKey).
                if str(item.get("librarySectionID", "")) != bare_key:
                    continue
                items.append(item)
        return SearchResults(
            tracks=[self._parse_track(i) for i in buckets["track"]],
            albums=[
                Album(
                    id=self._make_id(i["ratingKey"]),
                    title=i["title"],
                    artist=i.get("parentTitle", ""),
                    year=i.get("year"),
                    thumb=self._prefix(i.get("thumb")),
                    subtype=i.get("subtype"),
                )
                for i in buckets["album"]
            ],
            artists=[
                Artist(
                    id=self._make_id(i["ratingKey"]),
                    title=i["title"],
                    thumb=self._prefix(i.get("thumb")),
                )
                for i in buckets["artist"]
            ],
        )

    # ── broad title search (Tier 2: literal per-section, paginated) ─────────────

    async def search_titles(
        self,
        section_key: str,
        query: str,
        types: tuple[str, ...] = ("track", "album"),
        start: int = 0,
        size: int = 30,
    ) -> SearchResults:
        """Literal per-section title search — the on-demand broad Tier 2 pass.

        Hub search (Tier 1) ranks top hits and folds diacritics but never returns
        non-leading title substrings (e.g. "…(Cicada Remix)"). This per-section
        endpoint matches titles literally, so it surfaces those — at the cost of
        being diacritic-blind (accent-folding stays Tier 1's job). Paginated via
        X-Plex-Container-Start/Size so the frontend can load more on scroll. Goes
        through _get, so it rides the per-server concurrency semaphore (R7).
        """
        bare_key = self._strip(section_key) or section_key
        q = _normalize_text(query)

        async def _page(libtype: int) -> list[dict]:
            data = await self._get(
                f"/library/sections/{bare_key}/search",
                params={
                    "type": libtype,
                    "query": q,
                    "X-Plex-Container-Start": start,
                    "X-Plex-Container-Size": size,
                },
            )
            return data.get("MediaContainer", {}).get("Metadata", [])

        tracks: list[Track] = []
        albums: list[Album] = []
        if "track" in types:
            tracks = [self._parse_track(i) for i in await _page(TYPE_TRACK)]
        if "album" in types:
            albums = [
                Album(
                    id=self._make_id(i["ratingKey"]),
                    title=i["title"],
                    artist=i.get("parentTitle", ""),
                    year=i.get("year"),
                    thumb=self._prefix(i.get("thumb")),
                    subtype=i.get("subtype"),
                )
                for i in await _page(TYPE_ALBUM)
            ]
        return SearchResults(tracks=tracks, albums=albums, artists=[])

    # ── album art proxy helper ─────────────────────────────────────────────────

    async def fetch_art(self, thumb_path: str, width: int | None = None) -> tuple[bytes, str]:
        """Fetch album art bytes and content-type from Plex. Used by the art proxy route.

        With `width`, request a resized thumbnail via Plex's photo transcoder. A
        full-size cover (often 150KB+) decoded to paint a 48px row was the
        deep-jump reveal stall (2026-06-25 — full-DOM + content-visibility renders
        the destination band synchronously on arrival). Resize is best-effort:
        any transcoder failure falls back to the full image so art never breaks."""
        bare_path = self._strip(thumb_path) or thumb_path
        if width:
            try:
                async with self._sem:
                    resp = await self._http.get(
                        self._url("/photo/:/transcode"),
                        params={"width": width, "height": width, "minSize": 1,
                                "upscale": 0, "url": bare_path},
                        headers=self._headers(),
                    )
                resp.raise_for_status()
                return resp.content, resp.headers.get("content-type", "image/jpeg")
            except Exception:
                import logging
                logging.getLogger(__name__).warning(
                    "art transcode (w=%s) failed for %s; serving full size",
                    width, bare_path[:80], exc_info=True)
                # fall through to the full-size fetch below
        async with self._sem:
            resp = await self._http.get(self._url(bare_path), headers=self._headers())
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "image/jpeg")
        return resp.content, content_type


class MultiPlexClient:
    """Routes Plex API calls across multiple servers using compound keys."""

    def __init__(self, clients: list[PlexClient]) -> None:
        self._clients = clients
        self._by_machine: dict[str, PlexClient] = {
            c.machine_id: c for c in clients if c.machine_id
        }

    def _route(self, compound_key: str) -> tuple[str, PlexClient]:
        """Return (bare_key, client) by splitting on first ':'."""
        if ":" in compound_key:
            machine_id, bare = compound_key.split(":", 1)
            client = self._by_machine.get(machine_id)
            if client:
                return bare, client
            # Unknown machine_id: strip prefix and fall back to primary with bare key
            if self._clients:
                return bare, self._clients[0]
            return bare, _NoopClient()
        return compound_key, self._clients[0] if self._clients else _NoopClient()

    def _client_for_machine(self, machine_id: str) -> PlexClient | None:
        return self._by_machine.get(machine_id)

    def invalidate_cache(self) -> None:
        for c in self._clients:
            c.invalidate_cache()

    def stream_url(self, stream_key: str) -> str:
        _, client = self._route(stream_key)
        return client.stream_url(stream_key)

    async def get_libraries(self) -> list[Library]:
        """Union of every server's libraries — CONCURRENT and LOUD.

        2026-07-17 ce-debug: the previous sequential loop with a silent
        `except: pass` let one blackholed server tax every caller the full
        per-request timeout AND silently drop that server's libraries — the
        root of the "guest search always takes 17s" bug, invisible through
        four debugging rounds precisely because nothing logged. Legs now run
        in parallel (cost = slowest leg, not the sum) and a failed leg WARNs
        with the server name and elapsed time; healthy legs always land."""
        import logging

        async def _one(c) -> list[Library]:
            t0 = time.monotonic()
            try:
                return await c.get_libraries()
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    "get_libraries failed for server %r after %.1fs: %s",
                    getattr(c, "server_name", "?") or "?",
                    time.monotonic() - t0, exc,
                )
                return []
        per_server = await asyncio.gather(*[_one(c) for c in self._clients])
        return [lib for libs in per_server for lib in libs]

    async def get_artists(self, section_key: str) -> list[Artist]:
        _, client = self._route(section_key)
        return await client.get_artists(section_key)

    async def get_albums(
        self,
        section_key: str,
        artist_id: str | None = None,
        year: int | None = None,
        style: str | None = None,
    ) -> list[Album]:
        _, client = self._route(section_key)
        return await client.get_albums(section_key, artist_id=artist_id, year=year, style=style)

    async def get_tracks(
        self,
        section_key: str,
        album_id: str | None = None,
        genre: str | None = None,
        year: int | None = None,
    ) -> list[Track]:
        _, client = self._route(section_key)
        return await client.get_tracks(section_key, album_id=album_id, genre=genre, year=year)

    async def get_track(self, track_id: str) -> Track:
        _, client = self._route(track_id)
        return await client.get_track(track_id)

    async def get_sonic_nearest(
        self, track_id: str, limit: int = 10, max_distance: float = 0.35,
    ) -> list[Track]:
        _, client = self._route(track_id)
        return await client.get_sonic_nearest(track_id, limit=limit, max_distance=max_distance)

    async def get_artist_similar_names(self, track_id: str) -> list[str]:
        _, client = self._route(track_id)
        return await client.get_artist_similar_names(track_id)

    async def get_artist_popular_tracks(self, artist_id: str) -> list[dict]:
        _, client = self._route(artist_id)
        return await client.get_artist_popular_tracks(artist_id)

    async def get_album(self, album_id: str) -> Album:
        _, client = self._route(album_id)
        return await client.get_album(album_id)

    async def get_genres(self, section_key: str) -> list[str]:
        _, client = self._route(section_key)
        return await client.get_genres(section_key)

    async def get_styles_with_counts(self, section_key: str) -> list[dict]:
        _, client = self._route(section_key)
        return await client.get_styles_with_counts(section_key)

    async def get_years(self, section_key: str) -> list[int]:
        _, client = self._route(section_key)
        return await client.get_years(section_key)

    async def search(self, section_key: str, query: str) -> SearchResults:
        _, client = self._route(section_key)
        return await client.search(section_key, query)

    async def search_titles(
        self,
        section_key: str,
        query: str,
        types: tuple[str, ...] = ("track", "album"),
        start: int = 0,
        size: int = 30,
    ) -> SearchResults:
        _, client = self._route(section_key)
        return await client.search_titles(section_key, query, types=types, start=start, size=size)

    async def fetch_art(self, thumb_path: str, width: int | None = None) -> tuple[bytes, str]:
        _, client = self._route(thumb_path)
        return await client.fetch_art(thumb_path, width=width)


class _NoopClient:
    """Placeholder returned when no real client is available."""
    def _raise(self) -> None:
        raise RuntimeError("No Plex client available")

    def stream_url(self, key: str) -> str: self._raise()  # type: ignore[return-value]
    async def get_artists(self, *a, **kw): self._raise()
    async def get_albums(self, *a, **kw): self._raise()
    async def get_tracks(self, *a, **kw): self._raise()
    async def search(self, *a, **kw): self._raise()
    async def search_titles(self, *a, **kw): self._raise()
    async def fetch_art(self, *a, **kw): self._raise()
    async def get_track(self, *a, **kw): self._raise()
    async def get_album(self, *a, **kw): self._raise()
    async def get_play_counts(self, *a, **kw): self._raise()
    async def get_sonic_nearest(self, *a, **kw): return []
    async def get_artist_similar_names(self, *a, **kw): return []
    async def get_artist_popular_tracks(self, *a, **kw): return []
