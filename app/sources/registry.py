"""SourceRegistry — holds 0..N MusicSource providers and routes calls by id.

This replaces ``MultiPlexClient`` as the aggregate the app talks to. It exposes
the same method surface the app already calls on the client returned by
``state.get_plex_client()``, so call sites are unchanged (U3). Routing mirrors
the old ``MultiPlexClient._route`` with one deliberate difference (issue #44,
2026-11-21 health plan U5): a key ``{source_id}:{rest}`` dispatches to the source
whose ``source_id`` matches; a key whose prefix matches NO registered source
raises ``KeyError`` instead of falling back to the first source; only a
prefix-less legacy key falls back to the single Plex client. ``get_libraries``
aggregates across all sources; ``invalidate_cache`` fans out.

Provider-agnostic: a source is any ``MusicSource`` (Plex today; Jellyfin/local
register the same way), so no method here assumes Plex.
"""

from __future__ import annotations

import logging

import httpx

from app.models import Album, Artist, Library, SearchResults, Track
from app.sources.base import MusicSource

_log = logging.getLogger(__name__)


class SourceRegistry:
    def __init__(self, sources: list[MusicSource]) -> None:
        self._sources = sources
        self._by_id: dict[str, MusicSource] = {
            s.source_id: s for s in sources if s.source_id
        }

    # ── routing ───────────────────────────────────────────────────────────────

    def _route(self, key: str) -> MusicSource:
        """Return the source owning ``key`` (split on first ':').

        A prefix that names a registered source routes there. A prefix that
        names NO registered source raises ``KeyError(prefix)`` (issue #44 /
        KTD6): the old first-source fallback meant a stale queue entry or
        browse id for a server plex.tv stopped sharing would be fetched -- and
        played -- from the wrong server. ``KeyError`` because the existing 404
        mappers in the guest and admin APIs already catch it. Only a
        prefix-less legacy key (the single-server install whose machine_id is
        "") falls back to the first source."""
        if key and ":" in key:
            source_id, _ = key.split(":", 1)
            source = self._by_id.get(source_id)
            if source is not None:
                return source
            raise KeyError(source_id)
        return self._sources[0] if self._sources else _NoopSource()

    def source_type_for(self, key: str) -> str | None:
        """``source_type`` of the source whose id prefixes ``key``, or ``None``
        when the prefix is unknown or absent. Unlike ``_route`` this never falls
        back to the first source: the stream proxy uses it to decide whether an
        upstream 401 belongs to a Plex server (2026-11-21 health plan, U3), and
        a Jellyfin or Subsonic 401 must never be attributed to Plex."""
        if not key or ":" not in key:
            return None
        source = self._by_id.get(key.split(":", 1)[0])
        if source is None:
            return None
        return getattr(source, "source_type", None)

    @property
    def sources(self) -> list[MusicSource]:
        return list(self._sources)

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def invalidate_cache(self) -> None:
        for s in self._sources:
            s.invalidate_cache()

    # ── stream (U3: returns a URL string for call-site compatibility; U4 moves
    #    the proxy to resolve_stream's url-or-path StreamTarget) ────────────────

    def url_borne_auth_for(self, stream_key: str) -> bool:
        """Does the source owning ``stream_key`` carry its credential in the
        stream URL (U5)? Routes via ``_route`` and reads the owning source's
        ``url_borne_auth`` flag (default False for header-auth Plex/Jellyfin/
        Emby). ``_make_stream_url`` reads this to force-proxy URL-auth sources."""
        return bool(getattr(self._route(stream_key), "url_borne_auth", False))

    def stream_url(self, stream_key: str) -> str:
        """Raw device-facing upstream URL for a header-auth source.

        SECURITY (R6 / P0): a URL-auth source (Subsonic) carries its credential
        in the URL, so this accessor **refuses** it — returns ``""`` rather than
        let a credentialed upstream URL escape to a device. Such sources are
        force-proxied by ``_make_stream_url`` via ``/api/stream`` instead; a raw
        credentialed URL must never leave through this method on any config."""
        if self.url_borne_auth_for(stream_key):
            return ""
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
            except Exception as e:  # noqa: BLE001 — one dead source never breaks aggregation
                # Class and HTTP status only (2026-11-21 health plan, U3). Never
                # the exception text or the request URL: Subsonic carries its API
                # key in the query string and str(HTTPStatusError) embeds the URL.
                code = e.response.status_code if isinstance(e, httpx.HTTPStatusError) else None
                _log.warning(
                    "source %s: get_libraries failed (%s%s); its libraries are "
                    "skipped in this aggregation",
                    getattr(s, "source_id", "?"), type(e).__name__,
                    f", HTTP {code}" if code is not None else "")
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

    url_borne_auth = False

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
