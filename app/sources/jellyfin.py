"""Jellyfin provider — a ``MusicSource`` over the Jellyfin REST API (plan U10).

No SDK dependency: the REST surface is small enough for direct ``httpx`` (origin
research — the maintained Python SDKs are archived/incomplete). The shape mirrors
``app/plex/client.py`` — a per-server concurrency semaphore, a TTL cache, and
small parse helpers — so the two providers read alike.

Auth model (R5/R24): a Jellyfin connection is an account sign-in.
``authenticate`` POSTs to ``/Users/AuthenticateByName`` and returns the
``AccessToken`` + ``UserId``; the **password is never persisted** — only the
token + userId are stored (``app/database.jellyfin_sources``). Every request
carries the ``Authorization: MediaBrowser …`` header with a stable DeviceId; the
token rides that header, never a URL query param, so when the stream proxy hands
a URL to a Cast/DLNA device no credential leaks (R25 — see ``resolve_stream``).

Identifiers follow the registry's ``{source_id}:{native_id}`` namespace (U3).
Jellyfin item ids are GUIDs with no ``:`` so the first-colon split routes cleanly.

Durations: Jellyfin ``RunTimeTicks`` are 100-ns ticks (10,000,000 per second), so
``ticks // 10_000`` is milliseconds.

Capabilities: native search + genres only. Sonic similarity, popular tracks, and
Plex "styles" are Plex-specific and degrade to the base class's empty defaults.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.models import Album, Artist, Library, SearchResults, Track
from app.sources.base import Capabilities, MusicSource, StreamTarget

CLIENT_NAME = "Jukeplox"
CLIENT_VERSION = "1.0"

_CACHE_TTL = 300  # seconds — mirrors PlexClient

_JELLYFIN_CAPS = Capabilities(native_search=True, genres=True)

# Which MusicBrainz provider ids belong on which entity. Scoping is load-bearing:
# a track item's ProviderIds also carries its album's mbid, and if that landed on
# the track entity two tracks of one album would share an external id and
# false-merge into one (plan R10 — prefer a visible duplicate over a false merge).
_MB_ALBUM = {"musicbrainzalbum", "musicbrainzreleasegroup"}
_MB_TRACK = {"musicbrainztrack", "musicbrainzrecording"}
_MB_ARTIST = {"musicbrainzartist"}


class JellyfinAuthError(Exception):
    """Raised when Jellyfin rejects credentials or a stored token (401)."""


def new_device_id() -> str:
    """A stable per-connection DeviceId to mint at connect time and persist."""
    return uuid.uuid4().hex


def _auth_header(device_id: str, token: str | None = None) -> str:
    """The ``Authorization: MediaBrowser …`` value. Token appended only when
    present (auth requests are pre-token)."""
    parts = [
        f'Client="{CLIENT_NAME}"',
        f'Device="{CLIENT_NAME}"',
        f'DeviceId="{device_id}"',
        f'Version="{CLIENT_VERSION}"',
    ]
    if token:
        parts.append(f'Token="{token}"')
    return "MediaBrowser " + ", ".join(parts)


def _mb_match_ids(provider_ids: dict | None, allowed: set[str]) -> dict[str, str]:
    """Lowercased ``{scheme: value}`` for the MusicBrainz ids in ``allowed``.

    ``external_keys`` (merge) lowercases too, but normalizing here keeps the
    stored ``match_ids`` consistent across providers."""
    out: dict[str, str] = {}
    for k, v in (provider_ids or {}).items():
        kl = str(k).lower()
        if kl in allowed and v and str(v).strip():
            out[kl] = str(v).strip()
    return out


@dataclass
class _CacheEntry:
    value: Any
    expires_at: float = field(default_factory=lambda: time.monotonic() + _CACHE_TTL)

    @property
    def valid(self) -> bool:
        return time.monotonic() < self.expires_at


async def authenticate(
    server_url: str,
    username: str,
    password: str,
    *,
    device_id: str,
    http: httpx.AsyncClient | None = None,
) -> dict:
    """Sign in to Jellyfin and return ``{"token", "user_id", "server_id"}``.

    The password is sent in the request body and **never returned or stored** —
    the caller persists only the token + userId. ``server_id`` (may be ``""`` if
    the server omits it) lets the caller mint a connection id stable across
    reconnects. Raises ``JellyfinAuthError`` on a 401 (bad credentials) or a
    response missing the token/userId.
    """
    server_url = server_url.rstrip("/")
    own = http is None
    client = http or httpx.AsyncClient(timeout=15)
    try:
        resp = await client.post(
            f"{server_url}/Users/AuthenticateByName",
            json={"Username": username, "Pw": password},
            headers={
                "Authorization": _auth_header(device_id),
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        if resp.status_code == 401:
            raise JellyfinAuthError("Jellyfin rejected the username/password")
        resp.raise_for_status()
        data = resp.json()
        token = data.get("AccessToken")
        user_id = (data.get("User") or {}).get("Id")
        if not token or not user_id:
            raise JellyfinAuthError("Jellyfin auth response missing token/userId")
        return {"token": token, "user_id": user_id, "server_id": data.get("ServerId", "")}
    finally:
        if own:
            await client.aclose()


class JellyfinSource(MusicSource):
    def __init__(
        self,
        server_url: str,
        token: str,
        user_id: str,
        source_id: str = "jellyfin",
        server_name: str = "",
        device_id: str = "",
        http: httpx.AsyncClient | None = None,
        max_concurrency: int | None = None,
        page_size: int = 100,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.token = token
        self.user_id = user_id
        self._source_id = source_id or "jellyfin"
        self.server_name = server_name
        self.device_id = device_id or new_device_id()
        self._page_size = page_size
        self._cache: dict[str, _CacheEntry] = {}
        if max_concurrency is None:
            from app.config import settings
            max_concurrency = settings.plex_max_concurrency
        self._sem = asyncio.Semaphore(max_concurrency)
        self._http = http or httpx.AsyncClient(
            timeout=15,
            http2=False,
            limits=httpx.Limits(
                max_connections=max_concurrency,
                max_keepalive_connections=max_concurrency,
            ),
        )

    # ── identity ──────────────────────────────────────────────────────────────

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def source_type(self) -> str:
        return "jellyfin"

    @property
    def capabilities(self) -> Capabilities:
        return _JELLYFIN_CAPS

    # ── internal helpers ──────────────────────────────────────────────────────

    def _headers(self) -> dict:
        return {
            "Authorization": _auth_header(self.device_id, self.token),
            "Accept": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self.server_url}{path}"

    async def _get(self, path: str, params: dict | None = None) -> dict:
        async with self._sem:
            resp = await self._http.get(
                self._url(path), headers=self._headers(), params=params or {}
            )
        if resp.status_code == 401:
            raise JellyfinAuthError("Jellyfin token rejected (401)")
        resp.raise_for_status()
        return resp.json()

    async def _paged(self, path: str, params: dict) -> list[dict]:
        """Walk ``/Items``-style endpoints over ``StartIndex``/``Limit`` until the
        reported ``TotalRecordCount`` is reached (or a short/empty page lands)."""
        out: list[dict] = []
        start = 0
        while True:
            page = {**params, "StartIndex": start, "Limit": self._page_size}
            data = await self._get(path, page)
            batch = data.get("Items", []) or []
            out.extend(batch)
            start += len(batch)
            if not batch:
                break
            total = data.get("TotalRecordCount")
            if total is not None and start >= int(total):
                break
            if len(batch) < self._page_size:
                break
        return out

    def _cached(self, key: str) -> Any | None:
        entry = self._cache.get(key)
        return entry.value if entry and entry.valid else None

    def _store(self, key: str, value: Any) -> None:
        self._cache[key] = _CacheEntry(value=value)

    # ── key namespace ─────────────────────────────────────────────────────────

    def _make_id(self, native_id: str) -> str:
        return f"{self._source_id}:{native_id}"

    def _strip(self, key: str | None) -> str | None:
        prefix = f"{self._source_id}:"
        if key and key.startswith(prefix):
            return key[len(prefix):]
        return key

    def _art(self, item: dict) -> str | None:
        if (item.get("ImageTags") or {}).get("Primary"):
            return self._make_id(f"Items/{item['Id']}/Images/Primary")
        return None

    @staticmethod
    def _album_artist(item: dict) -> str:
        if item.get("AlbumArtist"):
            return item["AlbumArtist"]
        aa = item.get("AlbumArtists") or []
        return aa[0].get("Name", "") if aa else ""

    def _parse_album(self, item: dict) -> Album:
        return Album(
            id=self._make_id(item["Id"]),
            title=item.get("Name", ""),
            artist=self._album_artist(item),
            year=item.get("ProductionYear"),
            thumb=self._art(item),
            subtype=None,
            match_ids=_mb_match_ids(item.get("ProviderIds"), _MB_ALBUM),
            track_count=item.get("ChildCount"),
        )

    def _parse_track(self, item: dict) -> Track:
        arts = item.get("Artists") or []
        artist = arts[0] if arts else (item.get("AlbumArtist") or "")
        genres = item.get("Genres") or []
        return Track(
            id=self._make_id(item["Id"]),
            title=item.get("Name", ""),
            artist=artist,
            album=item.get("Album", ""),
            duration_ms=int(item.get("RunTimeTicks") or 0) // 10_000,
            genre=genres[0] if genres else None,
            year=item.get("ProductionYear"),
            thumb=self._art(item),
            stream_key=self._make_id(item["Id"]),
            server_name=self.server_name,
            album_artist=item.get("AlbumArtist") or None,
            album_id=self._make_id(item["AlbumId"]) if item.get("AlbumId") else None,
            disc_number=item.get("ParentIndexNumber") or 1,
            track_number=item.get("IndexNumber"),
            match_ids=_mb_match_ids(item.get("ProviderIds"), _MB_TRACK),
        )

    # ── core: enumerate ───────────────────────────────────────────────────────

    async def get_libraries(self) -> list[Library]:
        if (c := self._cached("libraries")) is not None:
            return c
        data = await self._get(f"/Users/{self.user_id}/Views")
        libs = [
            Library(
                key=self._make_id(it["Id"]),
                title=it.get("Name", ""),
                type="artist",  # normalize music sections to the browse "artist" type
                server_name=self.server_name,
            )
            for it in data.get("Items", [])
            if it.get("CollectionType") == "music"
        ]
        self._store("libraries", libs)
        return libs

    async def get_artists(self, section_key: str) -> list[Artist]:
        bare = self._strip(section_key) or section_key
        cache_key = f"artists:{bare}"
        if (c := self._cached(cache_key)) is not None:
            return c
        items = await self._paged(
            "/Artists/AlbumArtists", {"userId": self.user_id, "parentId": bare}
        )
        artists = [
            Artist(
                id=self._make_id(it["Id"]),
                title=it.get("Name", ""),
                thumb=self._art(it),
                release_count=None,
            )
            for it in items
        ]
        self._store(cache_key, artists)
        return artists

    async def get_albums(
        self,
        section_key: str,
        artist_id: str | None = None,
        year: int | None = None,
        style: str | None = None,
    ) -> list[Album]:
        bare = self._strip(section_key) or section_key
        bare_artist = self._strip(artist_id) if artist_id else None
        cache_key = f"albums:{bare}:{bare_artist}:{year}"
        if (c := self._cached(cache_key)) is not None:
            return c
        params: dict = {
            "userId": self.user_id,
            "Recursive": "true",
            "IncludeItemTypes": "MusicAlbum",
            "Fields": "ProviderIds",
            "SortBy": "ProductionYear,SortName",
        }
        if bare_artist:
            # Artist scoping is library-wide via AlbumArtistIds, not ParentId.
            params["AlbumArtistIds"] = bare_artist
        else:
            params["ParentId"] = bare
        if year:
            params["Years"] = str(year)
        albums = [self._parse_album(it) for it in await self._paged("/Items", params)]
        self._store(cache_key, albums)
        return albums

    async def get_tracks(
        self,
        section_key: str,
        album_id: str | None = None,
        genre: str | None = None,
        year: int | None = None,
    ) -> list[Track]:
        bare = self._strip(section_key) or section_key
        bare_album = self._strip(album_id) if album_id else None
        cache_key = f"tracks:{bare}:{bare_album}:{genre}:{year}"
        if (c := self._cached(cache_key)) is not None:
            return c
        params: dict = {
            "userId": self.user_id,
            "Recursive": "true",
            "IncludeItemTypes": "Audio",
            "Fields": "ProviderIds,MediaSources",
        }
        params["ParentId"] = bare_album or bare
        if genre:
            params["Genres"] = genre
        if year:
            params["Years"] = str(year)
        tracks = [self._parse_track(it) for it in await self._paged("/Items", params)]
        if bare_album:
            tracks.sort(key=lambda t: (t.disc_number, t.track_number or 0))
        self._store(cache_key, tracks)
        return tracks

    async def get_track(self, track_id: str) -> Track:
        bare = self._strip(track_id) or track_id
        item = await self._get(f"/Users/{self.user_id}/Items/{bare}")
        if not item:
            raise KeyError(f"Track {track_id} not found")
        return self._parse_track(item)

    async def get_album(self, album_id: str) -> Album:
        bare = self._strip(album_id) or album_id
        item = await self._get(f"/Users/{self.user_id}/Items/{bare}")
        if not item:
            raise KeyError(f"Album {album_id} not found")
        return self._parse_album(item)

    # ── core: search ──────────────────────────────────────────────────────────

    async def search(self, section_key: str, query: str) -> SearchResults:
        bare = self._strip(section_key) or section_key
        items = await self._paged(
            "/Items",
            {
                "userId": self.user_id,
                "searchTerm": query,
                "Recursive": "true",
                "IncludeItemTypes": "Audio,MusicAlbum,MusicArtist",
                "Fields": "ProviderIds",
                "ParentId": bare,
            },
        )
        tracks, albums, artists = [], [], []
        for it in items:
            t = it.get("Type")
            if t == "Audio":
                tracks.append(self._parse_track(it))
            elif t == "MusicAlbum":
                albums.append(self._parse_album(it))
            elif t == "MusicArtist":
                artists.append(
                    Artist(id=self._make_id(it["Id"]), title=it.get("Name", ""),
                           thumb=self._art(it))
                )
        return SearchResults(tracks=tracks, albums=albums, artists=artists)

    # ── core: stream + art ────────────────────────────────────────────────────

    def resolve_stream(self, stream_key: str) -> StreamTarget:
        # R25: the token rides the Authorization header (injected server-side by
        # the /api/stream proxy), never the URL — so no credential reaches a
        # Cast/DLNA device that gets handed the proxied URL.
        bare = self._strip(stream_key) or stream_key
        url = f"{self.server_url}/Audio/{bare}/stream?static=true"
        return StreamTarget(
            url=url, headers={"Authorization": _auth_header(self.device_id, self.token)}
        )

    async def fetch_art(self, thumb_path: str, width: int | None = None) -> tuple[bytes, str]:
        bare = self._strip(thumb_path) or thumb_path
        params: dict = {}
        if width:
            params["maxWidth"] = width
        async with self._sem:
            resp = await self._http.get(
                self._url(f"/{bare}"), params=params, headers=self._headers()
            )
        resp.raise_for_status()
        return resp.content, resp.headers.get("content-type", "image/jpeg")

    # ── enrichments (Jellyfin supports genres; the rest degrade via base) ──────

    async def get_genres(self, section_key: str) -> list[str]:
        bare = self._strip(section_key) or section_key
        data = await self._get(
            "/MusicGenres", {"userId": self.user_id, "parentId": bare}
        )
        return [g.get("Name", "") for g in data.get("Items", []) if g.get("Name")]

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def invalidate_cache(self) -> None:
        self._cache.clear()
