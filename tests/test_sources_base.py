"""U2: the MusicSource interface — a minimal provider satisfies it, and the
capability-gated enrichments degrade to empty (never raise) by default."""

import pytest

from app.models import Album, Artist, Library, SearchResults, Track
from app.sources.base import Capabilities, MusicSource, StreamTarget


class MinimalSource(MusicSource):
    """Implements only the abstract core — no enrichments, no native search."""

    @property
    def source_id(self) -> str:
        return "min1"

    @property
    def source_type(self) -> str:
        return "minimal"

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities()  # all False

    async def get_libraries(self) -> list[Library]:
        return [Library(key="min1:L", title="Lib", type="artist")]

    async def get_artists(self, section_key):
        return []

    async def get_albums(self, section_key, artist_id=None, year=None, style=None):
        return []

    async def get_tracks(self, section_key, album_id=None, genre=None, year=None):
        return []

    async def get_track(self, track_id):
        return Track(id=track_id, title="t", artist="a", album="al", duration_ms=1)

    async def get_album(self, album_id):
        return Album(id=album_id, title="al", artist="a")

    async def search(self, section_key, query):
        return SearchResults()

    def resolve_stream(self, stream_key):
        return StreamTarget(path="/music/x.flac")

    async def fetch_art(self, thumb_path, width=None):
        return (b"", "image/jpeg")


def test_minimal_source_instantiates_and_reports_no_capabilities():
    s = MinimalSource()
    assert s.source_id == "min1"
    assert s.source_type == "minimal"
    caps = s.capabilities
    assert not (caps.native_search or caps.similarity or caps.popular or caps.styles or caps.genres)


@pytest.mark.asyncio
async def test_enrichment_defaults_degrade_to_empty():
    s = MinimalSource()
    assert await s.get_genres("min1:L") == []
    assert await s.get_styles_with_counts("min1:L") == []
    assert await s.get_years("min1:L") == []
    assert await s.get_sonic_nearest("min1:42") == []
    assert await s.get_artist_similar_names("min1:42") == []
    assert await s.get_artist_popular_tracks("min1:7") == []
    empty = await s.search_titles("min1:L", "q")
    assert empty.tracks == [] and empty.albums == [] and empty.artists == []


def test_invalidate_cache_is_a_noop_by_default():
    assert MinimalSource().invalidate_cache() is None


def test_resolve_stream_returns_a_stream_target():
    t = MinimalSource().resolve_stream("min1:abc")
    assert isinstance(t, StreamTarget)
    assert t.path == "/music/x.flac" and t.url is None
