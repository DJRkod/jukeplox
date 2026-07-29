"""The ``MusicSource`` provider interface and capability flags.

Every source (Plex/Jellyfin/local) implements this contract. The *core* methods
(enumerate, search, resolve-stream, art) are abstract — every provider must
supply them. The *enrichment* methods (sonic similarity, popular tracks, Plex
styles) are capability-gated: the base class provides degrade-gracefully
defaults (empty results), and a provider only overrides them when its
``capabilities`` advertise support. This is how a Plex-connected install keeps
full feature parity while a Jellyfin/local source degrades without erroring
(plan R15).

Identifiers are opaque to callers and namespaced by the registry: a source's
own keys (section/rating/part) are prefixed with ``{source_id}:`` *outside* this
class (registry layer, U3/U4). Within a provider, keys are the provider's native
form.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.models import Album, Artist, Library, SearchResults, Track


@dataclass(frozen=True)
class Capabilities:
    """What optional features a source supports beyond the core contract.

    Core browse/search/stream/art is assumed for every source and is not a
    capability flag. These gate the *enrichments* that only some backends offer.
    """

    native_search: bool = False   # source has its own relevance-ranked search (e.g. Plex hub search)
    similarity: bool = False      # sonic / similar-artist data (Plex sonic analysis)
    popular: bool = False         # per-artist popular tracks (Plex online metadata)
    styles: bool = False          # sub-genre "styles" with counts (Plex)
    genres: bool = False          # genre listing


# Degrade-gracefully default for capability-gated enrichments. A source whose
# capabilities do not advertise the feature returns "nothing" rather than raising,
# so callers can treat absence uniformly (plan R15: "the enrichment is absent,
# never an error").


class MusicSource(ABC):
    """One connected media source behind a uniform interface."""

    # ── identity ──────────────────────────────────────────────────────────────

    @property
    @abstractmethod
    def source_id(self) -> str:
        """Stable, unique id for this source within the registry (key namespace)."""

    @property
    @abstractmethod
    def source_type(self) -> str:
        """``"plex"`` | ``"jellyfin"`` | ``"local"`` — for display/routing only."""

    @property
    @abstractmethod
    def capabilities(self) -> Capabilities:
        ...

    # ── core: enumerate ───────────────────────────────────────────────────────

    @abstractmethod
    async def get_libraries(self) -> list[Library]:
        ...

    @abstractmethod
    async def get_artists(self, section_key: str) -> list[Artist]:
        ...

    @abstractmethod
    async def get_albums(
        self,
        section_key: str,
        artist_id: str | None = None,
        year: int | None = None,
        style: str | None = None,
    ) -> list[Album]:
        ...

    @abstractmethod
    async def get_tracks(
        self,
        section_key: str,
        album_id: str | None = None,
        genre: str | None = None,
        year: int | None = None,
    ) -> list[Track]:
        ...

    @abstractmethod
    async def get_track(self, track_id: str) -> Track:
        ...

    @abstractmethod
    async def get_album(self, album_id: str) -> Album:
        ...

    # ── core: search ──────────────────────────────────────────────────────────

    @abstractmethod
    async def search(self, section_key: str, query: str) -> SearchResults:
        ...

    async def search_titles(
        self,
        section_key: str,
        query: str,
        types: tuple[str, ...] = ("track", "album"),
        start: int = 0,
        size: int = 30,
    ) -> SearchResults:
        """Broad literal title search. Default: no extra results beyond ``search``.

        Plex overrides this with its per-section literal endpoint; sources without
        a second search tier inherit the empty default."""
        return SearchResults()

    # ── core: stream + art ────────────────────────────────────────────────────

    @abstractmethod
    def resolve_stream(self, stream_key: str) -> StreamTarget:
        """Resolve a provider-native stream key to a playable target.

        Returns a :class:`StreamTarget` that is either a remote URL (proxied via
        httpx) or a local filesystem path (served via a range-aware FileResponse).
        The owning provider enforces its own containment (local path check)."""

    @abstractmethod
    async def fetch_art(self, thumb_path: str, width: int | None = None) -> tuple[bytes, str]:
        """Return ``(image_bytes, content_type)`` for an art path owned by this source."""

    # ── enrichments (capability-gated; degrade-gracefully defaults) ────────────

    async def get_genres(self, section_key: str) -> list[str]:
        return []

    async def get_styles_with_counts(self, section_key: str) -> list[dict]:
        return []

    async def get_years(self, section_key: str) -> list[int]:
        return []

    async def get_sonic_nearest(
        self, track_id: str, limit: int = 10, max_distance: float = 0.35,
    ) -> list[Track]:
        return []

    async def get_artist_similar_names(self, track_id: str) -> list[str]:
        return []

    async def get_artist_popular_tracks(self, artist_id: str) -> list[dict]:
        return []

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def invalidate_cache(self) -> None:
        """Drop any per-source caches. No-op by default."""
        return None


@dataclass(frozen=True)
class StreamTarget:
    """A resolved, playable stream location.

    Exactly one of ``url`` / ``path`` is set. ``url`` → proxy fetches it over
    HTTP (Plex/Jellyfin); ``path`` → a local filesystem path served via a
    range-aware FileResponse (local files). ``headers`` carries any auth the
    proxy must inject server-side (e.g. Jellyfin's MediaBrowser token) so no
    credential rides the URL to LAN devices (plan R25).
    """

    url: str | None = None
    path: str | None = None
    headers: dict[str, str] | None = None
