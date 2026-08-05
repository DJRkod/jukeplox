"""Plex provider — a thin ``MusicSource`` adapter over the existing ``PlexClient``.

The adapter delegates every method 1:1 to the wrapped client so behavior is
byte-identical to the pre-rebuild path (characterization-provable). Plex keeps
the full capability set; the multi-server routing that ``MultiPlexClient`` did
moves up to the registry (U3) — here one ``PlexSource`` wraps one ``PlexClient``
(one server), and its ``source_id`` is that server's ``machine_id``.
"""

from __future__ import annotations

from app.models import Album, Artist, Library, SearchResults, Track
from app.plex.client import PlexClient
from app.sources.base import Capabilities, MusicSource, StreamTarget

_PLEX_CAPS = Capabilities(
    native_search=True, similarity=True, popular=True, styles=True, genres=True,
)


class PlexSource(MusicSource):
    def __init__(self, client: PlexClient, source_id: str | None = None) -> None:
        self._client = client
        # Default to the wrapped server's machine_id so the source's key
        # namespace matches the compound keys PlexClient already mints.
        self._source_id = source_id or client.machine_id or "plex"

    # ── identity ──────────────────────────────────────────────────────────────

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def source_type(self) -> str:
        return "plex"

    @property
    def client(self) -> PlexClient:
        """The wrapped PlexClient — read-only. Plex Companion control
        (plexplayer backend wiring, 2026-08-04-002 plan U3) needs the raw
        server coordinates / token / controller id the client carries;
        everything else should keep to the MusicSource surface."""
        return self._client

    @property
    def capabilities(self) -> Capabilities:
        return _PLEX_CAPS

    # ── core: enumerate ───────────────────────────────────────────────────────

    async def get_libraries(self) -> list[Library]:
        return await self._client.get_libraries()

    async def get_artists(self, section_key: str) -> list[Artist]:
        return await self._client.get_artists(section_key)

    async def get_albums(
        self,
        section_key: str,
        artist_id: str | None = None,
        year: int | None = None,
        style: str | None = None,
    ) -> list[Album]:
        return await self._client.get_albums(
            section_key, artist_id=artist_id, year=year, style=style,
        )

    async def get_tracks(
        self,
        section_key: str,
        album_id: str | None = None,
        genre: str | None = None,
        year: int | None = None,
    ) -> list[Track]:
        return await self._client.get_tracks(
            section_key, album_id=album_id, genre=genre, year=year,
        )

    async def get_track(self, track_id: str) -> Track:
        return await self._client.get_track(track_id)

    async def get_album(self, album_id: str) -> Album:
        return await self._client.get_album(album_id)

    # ── core: search ──────────────────────────────────────────────────────────

    async def search(self, section_key: str, query: str) -> SearchResults:
        return await self._client.search(section_key, query)

    async def search_titles(
        self,
        section_key: str,
        query: str,
        types: tuple[str, ...] = ("track", "album"),
        start: int = 0,
        size: int = 30,
    ) -> SearchResults:
        return await self._client.search_titles(
            section_key, query, types=types, start=start, size=size,
        )

    # ── core: stream + art ────────────────────────────────────────────────────

    def resolve_stream(self, stream_key: str) -> StreamTarget:
        # Existing Plex behavior: a part-path URL with X-Plex-Token embedded.
        # The /api/stream proxy handles credential exposure posture as today.
        return StreamTarget(url=self._client.stream_url(stream_key))

    async def fetch_art(self, thumb_path: str, width: int | None = None) -> tuple[bytes, str]:
        return await self._client.fetch_art(thumb_path, width=width)

    # ── enrichments (Plex supports all) ────────────────────────────────────────

    async def get_genres(self, section_key: str) -> list[str]:
        return await self._client.get_genres(section_key)

    async def get_styles_with_counts(self, section_key: str) -> list[dict]:
        return await self._client.get_styles_with_counts(section_key)

    async def get_years(self, section_key: str) -> list[int]:
        return await self._client.get_years(section_key)

    async def get_sonic_nearest(
        self, track_id: str, limit: int = 10, max_distance: float = 0.35,
    ) -> list[Track]:
        return await self._client.get_sonic_nearest(
            track_id, limit=limit, max_distance=max_distance,
        )

    async def get_artist_similar_names(self, track_id: str) -> list[str]:
        return await self._client.get_artist_similar_names(track_id)

    async def get_artist_popular_tracks(self, artist_id: str) -> list[dict]:
        return await self._client.get_artist_popular_tracks(artist_id)

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def invalidate_cache(self) -> None:
        self._client.invalidate_cache()
