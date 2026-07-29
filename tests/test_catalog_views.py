"""U8: catalog-backed browse/search (the universal floor) + the routing gate.

The catalog view builders return one merged, source-invisible entry per release
with holds-derived sources/stream keys; ``_catalog_active`` routes a single
native source (Plex) to its native pipeline and everything else to the catalog.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app import database
from app.api import guest
from app.catalog import store, views
from app.config import Settings
from app.sources.base import Capabilities


@pytest.fixture
def tmp_settings(tmp_path, monkeypatch):
    s = Settings(data_dir=tmp_path, secret_key="test")
    monkeypatch.setattr(database, "settings", s)
    return s


@pytest.fixture
async def db(tmp_settings):
    await database.init_db()
    yield tmp_settings
    await database.close_db()


async def _seed_two_source_album(db):
    """One artist, one album held by two sources, two tracks (each two holds)."""
    await store.replace_catalog(
        artists=[{"identity": "ar", "title": "Act", "base_key": "act"}],
        albums=[{"identity": "al", "title": "Rec", "title_base": "rec", "artist": "Act",
                 "artist_base_key": "act", "year": 2020, "track_count": 2}],
        tracks=[
            {"identity": "t1", "title": "Song One", "title_base": "song one", "artist": "Act",
             "artist_base_key": "act", "album": "Rec", "album_identity": "al",
             "duration_ms": 180000, "disc_number": 1, "track_number": 1},
            {"identity": "t2", "title": "Song Two", "title_base": "song two", "artist": "Act",
             "artist_base_key": "act", "album": "Rec", "album_identity": "al",
             "duration_ms": 200000, "disc_number": 1, "track_number": 2},
        ],
        holds=[
            {"entity_type": "album", "identity": "al", "source_id": "m1", "provider_local_key": "m1:al", "priority": 0, "server_name": "Plex"},
            {"entity_type": "album", "identity": "al", "source_id": "jelly", "provider_local_key": "jelly:al", "priority": 1, "server_name": "Jelly"},
            {"entity_type": "track", "identity": "t1", "source_id": "m1", "provider_local_key": "m1:p1", "priority": 0, "server_name": "Plex"},
            {"entity_type": "track", "identity": "t1", "source_id": "jelly", "provider_local_key": "jelly:p1", "priority": 1, "server_name": "Jelly"},
            {"entity_type": "track", "identity": "t2", "source_id": "m1", "provider_local_key": "m1:p2", "priority": 0, "server_name": "Plex"},
        ],
    )


# ── view builders ────────────────────────────────────────────────────────────

async def test_artists_and_albums_merged(db):
    await _seed_two_source_album(db)
    arts = await views.artists()
    assert [(a.id, a.title) for a in arts] == [("ar", "Act")]

    albs = await views.albums()
    assert len(albs) == 1  # one merged entry, not one per source
    assert albs[0].id == "al"
    # holds → priority-ordered sources for drill-in routing
    assert [s["server_name"] for s in albs[0].sources] == ["Plex", "Jelly"]
    assert [s["album_id"] for s in albs[0].sources] == ["m1:al", "jelly:al"]


async def test_album_tracks_carry_primary_hold_stream_key(db):
    await _seed_two_source_album(db)
    tracks = await views.album_tracks("al")
    assert [t.id for t in tracks] == ["t1", "t2"]
    # highest-priority hold supplies the resolvable stream key + server name
    assert tracks[0].stream_key == "m1:p1"
    assert tracks[0].server_name == "Plex"
    assert tracks[0].album_id == "al"  # links back to the merged album identity


async def test_artist_albums_by_base_key(db):
    await _seed_two_source_album(db)
    albs = await views.artist_albums("ar")
    assert [a.id for a in albs] == ["al"]


async def test_artist_songs_floor(db):
    await _seed_two_source_album(db)
    songs = await views.artist_songs("ar")
    assert {t.id for t in songs} == {"t1", "t2"}


async def test_artist_songs_includes_featured_tracks_via_albums(db):
    # ce-debug 2026-06-29 (Bug A): a track whose per-track artist differs from the
    # release artist ("Act feat. Guest") must still appear in the artist's All
    # Songs — it's on the artist's own release. The old by-track-artist_base_key
    # filter dropped it (and emptied All Songs entirely for a Various-Artists or
    # all-featured artist). All Songs now gathers via the artist's albums.
    await store.replace_catalog(
        artists=[{"identity": "ar", "title": "Act", "base_key": "act"}],
        albums=[{"identity": "al", "title": "Rec", "title_base": "rec", "artist": "Act",
                 "artist_base_key": "act", "year": 2020, "track_count": 2}],
        tracks=[
            {"identity": "t1", "title": "Solo", "title_base": "solo", "artist": "Act",
             "artist_base_key": "act", "album": "Rec", "album_identity": "al",
             "duration_ms": 180000, "disc_number": 1, "track_number": 1},
            {"identity": "t2", "title": "Collab", "title_base": "collab",
             "artist": "Act feat. Guest", "artist_base_key": "act feat. guest",
             "album": "Rec", "album_identity": "al",
             "duration_ms": 200000, "disc_number": 1, "track_number": 2},
        ],
        holds=[
            {"entity_type": "track", "identity": "t1", "source_id": "m1", "provider_local_key": "m1:p1", "priority": 0, "server_name": "Plex"},
            {"entity_type": "track", "identity": "t2", "source_id": "m1", "provider_local_key": "m1:p2", "priority": 0, "server_name": "Plex"},
        ],
    )
    songs = await views.artist_songs("ar")
    assert {t.id for t in songs} == {"t1", "t2"}  # feat track included via the album


async def test_search_matches_across_entities(db):
    await _seed_two_source_album(db)
    res = await views.search("song one")
    assert [t.id for t in res["tracks"]] == ["t1"]
    res2 = await views.search("rec")
    assert [a.id for a in res2["albums"]] == ["al"]
    res3 = await views.search("act")
    assert [a.id for a in res3["artists"]] == ["ar"]
    assert await views.search("   ") == {"tracks": [], "albums": [], "artists": [], "genres": []}


# ── routing gate ─────────────────────────────────────────────────────────────

class _FakeSource:
    def __init__(self, native=True, source_type="plex"):
        self.capabilities = Capabilities(native_search=native)
        self.source_type = source_type


class _FakeRegistry:
    def __init__(self, sources):
        self.sources = sources


async def _gate_with(reg):
    with patch("app.state.get_plex_client", AsyncMock(return_value=reg)):
        return await guest._catalog_active()


async def test_gate_single_plex_source_is_native():
    assert await _gate_with(_FakeRegistry([_FakeSource(source_type="plex")])) is False


async def test_gate_multiple_plex_servers_stay_native():
    # Regression: one PlexSource PER server, so a multi-server Plex install has
    # len(sources) > 1 — but it is still all-Plex and the native pipeline merges
    # the servers (AE6). It must NOT route to the catalog (the jelly bug).
    assert await _gate_with(_FakeRegistry([
        _FakeSource(source_type="plex"), _FakeSource(source_type="plex"),
    ])) is False


async def test_gate_mixed_types_uses_catalog():
    assert await _gate_with(_FakeRegistry([
        _FakeSource(source_type="plex"), _FakeSource(native=True, source_type="jellyfin"),
    ])) is True


async def test_gate_single_non_plex_uses_catalog():
    assert await _gate_with(_FakeRegistry([_FakeSource(native=False, source_type="local")])) is True


async def test_gate_none_or_nonregistry_is_native():
    assert await _gate_with(None) is False
    from unittest.mock import MagicMock
    assert await _gate_with(MagicMock()) is False  # .sources is a Mock, not a list


# ── endpoint routes through the gate to the catalog ──────────────────────────

async def test_browse_artist_songs_endpoint_returns_dict_payload(db):
    # ce-debug 2026-06-29 round 2 (Bug A, real cause): the frontend All-Songs view
    # reads data.tracks / data.releases / data.popular_available. The catalog
    # branch returned a BARE LIST, so data.tracks was undefined → "No songs."
    # (The native branch returns this dict shape; the catalog branch must match.)
    await _seed_two_source_album(db)
    with patch("app.state.get_plex_client", AsyncMock(return_value=_FakeRegistry([
            _FakeSource(source_type="plex"), _FakeSource(source_type="jellyfin")]))):
        data = await guest.browse_artist_songs("ar")
    assert isinstance(data, dict)
    assert {t["track_id"] for t in data["tracks"]} == {"t1", "t2"}
    assert [r["id"] for r in data["releases"]] == ["al"]
    assert data["popular_available"] is False
    assert all(t["kind"] == "own" and "release" in t for t in data["tracks"])


async def test_browse_artists_endpoint_uses_catalog_when_active(db):
    await _seed_two_source_album(db)
    with patch("app.state.get_plex_client",
               AsyncMock(return_value=_FakeRegistry([
                   _FakeSource(source_type="plex"), _FakeSource(source_type="jellyfin")]))):
        arts = await guest.browse_artists()
    assert [a.id for a in arts] == ["ar"]


# ── per-source holds serialization for the picker (parity U2) ────────────────

class _SrcStub:
    def __init__(self, source_id, source_type):
        self.source_id = source_id
        self.source_type = source_type


def _typed_registry():
    # Maps the seed's hold source_ids to their types so labels are type-qualified.
    return _FakeRegistry([_SrcStub("m1", "plex"), _SrcStub("jelly", "jellyfin")])


async def test_track_attaches_per_source_holds_with_type(db):
    await _seed_two_source_album(db)
    with patch("app.state.get_plex_client", AsyncMock(return_value=_typed_registry())):
        tracks = await views.album_tracks("al")
    t1 = next(t for t in tracks if t.id == "t1")
    # Two holders, priority-ordered (primary first), each carrying server_name,
    # source_type, and the resolvable key.
    assert [(h["server_name"], h["source_type"], h["key"]) for h in t1.holds] == [
        ("Plex", "plex", "m1:p1"), ("Jelly", "jellyfin", "jelly:p1")]
    # Single-holder track → one hold; the picker is suppressed downstream.
    t2 = next(t for t in tracks if t.id == "t2")
    assert [(h["server_name"], h["source_type"]) for h in t2.holds] == [("Plex", "plex")]


async def test_track_dict_emits_sources_for_multi_holder(db):
    await _seed_two_source_album(db)
    with patch("app.state.get_plex_client", AsyncMock(return_value=_typed_registry())):
        tracks = await views.album_tracks("al")
    d1 = guest._track_dict(next(t for t in tracks if t.id == "t1"))
    assert d1["sources"] == [{"server_name": "Plex", "source_type": "plex"},
                             {"server_name": "Jelly", "source_type": "jellyfin"}]
    # Single holder → no `sources` (frontend hides "Play From Source…").
    d2 = guest._track_dict(next(t for t in tracks if t.id == "t2"))
    assert "sources" not in d2


async def test_album_sources_carry_source_type(db):
    await _seed_two_source_album(db)
    with patch("app.state.get_plex_client", AsyncMock(return_value=_typed_registry())):
        albs = await views.albums()
    # Album picker labels are type-qualified too (AE1 lists "Plex"/"Jellyfin").
    assert [(s["server_name"], s["source_type"]) for s in albs[0].sources] == [
        ("Plex", "plex"), ("Jelly", "jellyfin")]


# ── source-neutral genres (U13/R16) ──────────────────────────────────────────

async def _seed_genre_catalog(db):
    """Two albums across two genres — Rock (2 tracks, mixed case) + Pop (1) —
    plus one untagged track that must produce no genre row."""
    await store.replace_catalog(
        artists=[{"identity": "ar", "title": "Act", "base_key": "act"}],
        albums=[
            {"identity": "alr", "title": "RockRec", "title_base": "rockrec", "artist": "Act",
             "artist_base_key": "act", "year": 2020, "track_count": 2},
            {"identity": "alp", "title": "PopRec", "title_base": "poprec", "artist": "Act",
             "artist_base_key": "act", "year": 2021, "track_count": 1},
        ],
        tracks=[
            {"identity": "t1", "title": "R1", "title_base": "r1", "artist": "Act",
             "artist_base_key": "act", "album": "RockRec", "album_identity": "alr",
             "genre": "Rock", "duration_ms": 180000, "disc_number": 1, "track_number": 1},
            {"identity": "t2", "title": "R2", "title_base": "r2", "artist": "Act",
             "artist_base_key": "act", "album": "RockRec", "album_identity": "alr",
             "genre": "rock", "duration_ms": 200000, "disc_number": 1, "track_number": 2},
            {"identity": "t3", "title": "P1", "title_base": "p1", "artist": "Act",
             "artist_base_key": "act", "album": "PopRec", "album_identity": "alp",
             "genre": "Pop", "duration_ms": 150000, "disc_number": 1, "track_number": 1},
            {"identity": "t4", "title": "U1", "title_base": "u1", "artist": "Act",
             "artist_base_key": "act", "album": "PopRec", "album_identity": "alp",
             "genre": None, "duration_ms": 150000, "disc_number": 1, "track_number": 2},
        ],
        holds=[
            {"entity_type": "album", "identity": "alr", "source_id": "jelly", "provider_local_key": "jelly:alr", "priority": 0, "server_name": "Jelly"},
            {"entity_type": "album", "identity": "alp", "source_id": "jelly", "provider_local_key": "jelly:alp", "priority": 0, "server_name": "Jelly"},
        ],
    )


async def test_genres_counts_from_track_tags(db):
    await _seed_genre_catalog(db)
    # Rock (2, case-insensitive merge, first spelling wins) ahead of Pop (1);
    # the untagged track contributes nothing.
    assert await views.genres() == [{"name": "Rock", "count": 2}, {"name": "Pop", "count": 1}]


async def test_genres_empty_when_no_tags(db):
    await store.replace_catalog(
        artists=[], albums=[],
        tracks=[{"identity": "t1", "title": "X", "title_base": "x", "artist": "A",
                 "artist_base_key": "a", "album": "Al", "album_identity": None,
                 "duration_ms": 1000}],
        holds=[],
    )
    assert await views.genres() == []  # R16: degrade to empty, never error


async def test_genre_albums_returns_albums_for_style(db):
    await _seed_genre_catalog(db)
    with patch("app.state.get_plex_client", AsyncMock(return_value=_FakeRegistry([_SrcStub("jelly", "jellyfin")]))):
        albums = await views.genre_albums("rock")  # case-insensitive match
    assert [a.id for a in albums] == ["alr"]
    # Album objects carry holds-derived sources so the shared renderer drills in.
    assert albums[0].sources and albums[0].sources[0]["album_id"] == "jelly:alr"


async def test_genre_albums_unknown_style_empty(db):
    await _seed_genre_catalog(db)
    assert await views.genre_albums("jazz") == []
    assert await views.genre_albums("  ") == []
