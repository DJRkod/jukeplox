"""SourceRegistry — holds 0..N MusicSource providers and routes calls by id.

This replaces ``MultiPlexClient`` as the aggregate the app talks to. It exposes
the same method surface the app already calls on the client returned by
``state.get_plex_client()``, so call sites are unchanged (U3). Routing mirrors
``MultiPlexClient._route``: a key ``{source_id}:{rest}`` dispatches to the source
whose ``source_id`` matches; unknown/prefixless keys fall back to the first
source. ``get_libraries`` aggregates across all sources; ``invalidate_cache``
fans out.

Provider-agnostic: a source is any ``MusicSource`` (Plex today; Jellyfin/local
register the same way), so no method here assumes Plex.
"""

from __future__ import annotations

from app.models import Album, Artist, Library, SearchResults, Track
from app.sources.base import MusicSource


class SourceRegistry:
    def __init__(self, sources: list[MusicSource]) -> None:
        self._sources = sources
        self._by_id: dict[str, MusicSource] = {
            s.source_id: s for s in sources if s.source_id
        }

    # ── routing ───────────────────────────────────────────────────────────────

    def _route(self, key: str) -> MusicSource:
        """Return the source owning ``key`` (split on first ':'); fall back to the
        first source for prefixless or unknown-id keys (mirrors MultiPlexClient)."""
        if key and ":" in key:
            source_id, _ = key.split(":", 1)
            source = self._by_id.get(source_id)
            if source:
                return source
        return self._sources[0] if self._sources else _NoopSource()

    @property
    def sources(self) -> list[MusicSource]:
        return list(self._sources)

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def invalidate_cache(self) -> None:
        for s in self._sources:
            s.invalidate_cache()

    # ── stream (U3: returns a URL string for call-site compatibility; U4 moves
    #    the proxy to resolve_stream's url-or-path StreamTarget) ────────────────

    def stream_url(self, stream_key: str) -> str:
        return self._route(stream_key).resolve_stream(stream_key).url or ""

    def resolve_stream(self, stream_key: str):
        """Resolve to a StreamTarget (url-or-path + optional headers) via the
        owning provider — the /api/stream proxy uses this to branch delivery (U4)."""
        return self._route(stream_key).resolve_stream(stream_key)

    # ── enumerate ─────────────────────────────────────────────────────────────

    async def get_libraries(self) -> list[Library]:
        result: list[Library] = []
        for s in self._sources:
            try:
                libs = await s.get_libraries()
                # Stamp the owning source's type so the admin Libraries list can
                # render a per-source-type indicator (a Jellyfin "Music" vs a
                # Plex "Music"). This is the only aggregation point that knows
                # which source each library came from.
                for lib in libs:
                    lib.source_type = s.source_type
                result.extend(libs)
            except Exception:
                pass
        return result

    async def get_artists(self, section_key: str) -> list[Artist]:
        return await self._route(section_key).get_artists(section_key)

    async def get_albums(
        self,
        section_key: str,
        artist_id: str | None = None,
        year: int | None = None,
        style: str | None = None,
    ) -> list[Album]:
        return await self._route(section_key).get_albums(
            section_key, artist_id=artist_id, year=year, style=style,
        )

    async def get_tracks(
        self,
        section_key: str,
        album_id: str | None = None,
        genre: str | None = None,
        year: int | None = None,
    ) -> list[Track]:
        return await self._route(section_key).get_tracks(
            section_key, album_id=album_id, genre=genre, year=year,
        )

    async def get_track(self, track_id: str) -> Track:
        return await self._route(track_id).get_track(track_id)

    async def get_album(self, album_id: str) -> Album:
        return await self._route(album_id).get_album(album_id)

    # ── search ────────────────────────────────────────────────────────────────

    async def search(self, section_key: str, query: str) -> SearchResults:
        return await self._route(section_key).search(section_key, query)

    async def search_titles(
        self,
        section_key: str,
        query: str,
        types: tuple[str, ...] = ("track", "album"),
        start: int = 0,
        size: int = 30,
    ) -> SearchResults:
        return await self._route(section_key).search_titles(
            section_key, query, types=types, start=start, size=size,
        )

    # ── enrichments (route; degrade per-source via the base defaults) ──────────

    async def get_genres(self, section_key: str) -> list[str]:
        return await self._route(section_key).get_genres(section_key)

    async def get_styles_with_counts(self, section_key: str) -> list[dict]:
        return await self._route(section_key).get_styles_with_counts(section_key)

    async def get_years(self, section_key: str) -> list[int]:
        return await self._route(section_key).get_years(section_key)

    async def get_album_track_counts(self, section_key: str) -> dict[str, int]:
        return await self._route(section_key).get_album_track_counts(section_key)

    async def get_sonic_nearest(
        self, track_id: str, limit: int = 10, max_distance: float = 0.35,
    ) -> list[Track]:
        return await self._route(track_id).get_sonic_nearest(
            track_id, limit=limit, max_distance=max_distance,
        )

    async def get_artist_similar_names(self, track_id: str) -> list[str]:
        return await self._route(track_id).get_artist_similar_names(track_id)

    async def get_artist_popular_tracks(self, artist_id: str) -> list[dict]:
        return await self._route(artist_id).get_artist_popular_tracks(artist_id)

    # ── art ───────────────────────────────────────────────────────────────────

    async def fetch_art(self, thumb_path: str, width: int | None = None) -> tuple[bytes, str]:
        return await self._route(thumb_path).fetch_art(thumb_path, width=width)


class _NoopSource:
    """Returned by routing when the registry somehow holds no sources. Mirrors
    the old _NoopClient: enrichments degrade to empty, hard reads raise."""

    def _raise(self):
        raise RuntimeError("No media source available")

    def resolve_stream(self, key):
        from app.sources.base import StreamTarget
        return StreamTarget(url="")

    async def get_artists(self, *a, **kw): self._raise()
    async def get_albums(self, *a, **kw): self._raise()
    async def get_tracks(self, *a, **kw): self._raise()
    async def get_track(self, *a, **kw): self._raise()
    async def get_album(self, *a, **kw): self._raise()
    async def search(self, *a, **kw): self._raise()
    async def search_titles(self, *a, **kw): self._raise()
    async def fetch_art(self, *a, **kw): self._raise()
    async def get_genres(self, *a, **kw): return []
    async def get_styles_with_counts(self, *a, **kw): return []
    async def get_years(self, *a, **kw): return []
    async def get_album_track_counts(self, *a, **kw): return {}
    async def get_sonic_nearest(self, *a, **kw): return []
    async def get_artist_similar_names(self, *a, **kw): return []
    async def get_artist_popular_tracks(self, *a, **kw): return []
