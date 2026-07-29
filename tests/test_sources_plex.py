"""U2: PlexSource adapts PlexClient 1:1 (byte-equivalent delegation) and reports
the full Plex capability set. Characterization: the adapter must not alter the
wrapped client's return values or call arguments."""

from unittest.mock import AsyncMock, MagicMock

from app.models import Album, Artist, Library, SearchResults, Track
from app.sources.base import StreamTarget
from app.sources.plex import PlexSource


def _fake_client():
    c = MagicMock()
    c.machine_id = "m1"
    c.get_libraries = AsyncMock(return_value=[Library(key="m1:1", title="L", type="artist")])
    c.get_artists = AsyncMock(return_value=[Artist(id="m1:7", title="Act")])
    c.get_albums = AsyncMock(return_value=[Album(id="m1:9", title="Rec", artist="Act")])
    c.get_tracks = AsyncMock(return_value=[Track(id="m1:10", title="Song", artist="Act", album="Rec", duration_ms=1000)])
    c.get_track = AsyncMock(return_value=Track(id="m1:10", title="Song", artist="Act", album="Rec", duration_ms=1000))
    c.get_album = AsyncMock(return_value=Album(id="m1:9", title="Rec", artist="Act"))
    c.search = AsyncMock(return_value=SearchResults(tracks=[Track(id="m1:10", title="Song", artist="Act", album="Rec", duration_ms=1)]))
    c.search_titles = AsyncMock(return_value=SearchResults())
    c.get_genres = AsyncMock(return_value=["Rock"])
    c.get_styles_with_counts = AsyncMock(return_value=[{"name": "Doom", "count": 3}])
    c.get_years = AsyncMock(return_value=[1986])
    c.get_sonic_nearest = AsyncMock(return_value=[Track(id="m1:11", title="N", artist="Act", album="Rec", duration_ms=1)])
    c.get_artist_similar_names = AsyncMock(return_value=["Other Act"])
    c.get_artist_popular_tracks = AsyncMock(return_value=[{"title": "Hit", "rating_key": "m1:12"}])
    c.fetch_art = AsyncMock(return_value=(b"img", "image/jpeg"))
    c.stream_url = MagicMock(return_value="http://plex/library/parts/1/f.flac?X-Plex-Token=tok")
    c.invalidate_cache = MagicMock()
    return c


def test_identity_and_capabilities():
    s = PlexSource(_fake_client())
    assert s.source_id == "m1"
    assert s.source_type == "plex"
    caps = s.capabilities
    assert caps.native_search and caps.similarity and caps.popular and caps.styles and caps.genres


def test_source_id_override():
    assert PlexSource(_fake_client(), source_id="custom").source_id == "custom"


async def test_enumerate_delegates_unchanged():
    c = _fake_client()
    s = PlexSource(c)
    assert (await s.get_libraries())[0].key == "m1:1"
    assert (await s.get_artists("m1:1"))[0].id == "m1:7"
    await s.get_albums("m1:1", artist_id="m1:7", year=1986, style="Doom")
    c.get_albums.assert_awaited_with("m1:1", artist_id="m1:7", year=1986, style="Doom")
    await s.get_tracks("m1:1", album_id="m1:9", genre="Rock", year=1986)
    c.get_tracks.assert_awaited_with("m1:1", album_id="m1:9", genre="Rock", year=1986)
    assert (await s.get_track("m1:10")).title == "Song"
    assert (await s.get_album("m1:9")).title == "Rec"


async def test_search_delegates():
    s = PlexSource(_fake_client())
    res = await s.search("m1:1", "song")
    assert res.tracks[0].id == "m1:10"


async def test_enrichments_delegate_for_plex():
    s = PlexSource(_fake_client())
    assert await s.get_genres("m1:1") == ["Rock"]
    assert (await s.get_styles_with_counts("m1:1"))[0]["name"] == "Doom"
    assert await s.get_years("m1:1") == [1986]
    assert (await s.get_sonic_nearest("m1:10"))[0].id == "m1:11"
    assert await s.get_artist_similar_names("m1:10") == ["Other Act"]
    assert (await s.get_artist_popular_tracks("m1:7"))[0]["title"] == "Hit"


async def test_fetch_art_delegates():
    s = PlexSource(_fake_client())
    assert await s.fetch_art("m1:/thumb", width=48) == (b"img", "image/jpeg")


def test_resolve_stream_wraps_client_stream_url():
    c = _fake_client()
    t = PlexSource(c).resolve_stream("m1:/library/parts/1/f.flac")
    assert isinstance(t, StreamTarget)
    assert t.url == "http://plex/library/parts/1/f.flac?X-Plex-Token=tok"
    assert t.path is None
    c.stream_url.assert_called_once_with("m1:/library/parts/1/f.flac")


def test_invalidate_cache_delegates():
    c = _fake_client()
    PlexSource(c).invalidate_cache()
    c.invalidate_cache.assert_called_once()
