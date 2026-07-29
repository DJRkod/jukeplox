"""U5: the track-grained catalog store.

Verifies the store persists artists/albums/tracks at track grain with stable
identities, reads them back grouped (artist→albums→tracks), atomic-replaces
content without leaving torn/leftover rows, orders holds by source priority,
and keeps the DURABLE alias table independent of content replacement. Also
asserts the catalog coexists with the existing ratings/play-count tables.
"""

import pytest

from app import database
from app.catalog import store
from app.config import Settings


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


# Small builders keep the row dicts readable; the store fills omitted keys None.
def _artist(identity, title="Act", base_key=None):
    return {"identity": identity, "title": title, "base_key": base_key or title.lower()}


def _album(identity, title="Rec", artist="Act", artist_base_key="act", **kw):
    return {"identity": identity, "title": title, "title_base": title.lower(),
            "artist": artist, "artist_base_key": artist_base_key, **kw}


def _track(identity, album_identity, title="Song", artist="Act", **kw):
    return {"identity": identity, "title": title, "title_base": title.lower(),
            "artist": artist, "artist_base_key": artist.lower(),
            "album_identity": album_identity, "duration_ms": 180000, **kw}


def _hold(identity, source_id, key, priority, entity_type="track", server_name=None):
    return {"entity_type": entity_type, "identity": identity, "source_id": source_id,
            "provider_local_key": key, "priority": priority, "server_name": server_name}


# ── happy path: write then read back grouped ─────────────────────────────────

async def test_write_then_read_back_grouped(db):
    await store.replace_catalog(
        artists=[_artist("ar1", "Act")],
        albums=[_album("al1", "Rec", "Act", "act")],
        tracks=[
            _track("t1", "al1", "Song A", track_number=1),
            _track("t2", "al1", "Song B", track_number=2),
        ],
        holds=[],
    )
    artists = await store.get_artists()
    assert [a["identity"] for a in artists] == ["ar1"]
    assert artists[0]["title"] == "Act"

    albums = await store.get_albums_for_artist("act")
    assert [a["identity"] for a in albums] == ["al1"]

    tracks = await store.get_tracks_for_album("al1")
    assert [t["identity"] for t in tracks] == ["t1", "t2"]
    assert tracks[0]["duration_ms"] == 180000


async def test_tracks_ordered_by_disc_then_track_number(db):
    await store.replace_catalog(
        artists=[], albums=[_album("al1")],
        tracks=[
            _track("d2t1", "al1", "D2T1", disc_number=2, track_number=1),
            _track("d1t2", "al1", "D1T2", disc_number=1, track_number=2),
            _track("d1t1", "al1", "D1T1", disc_number=1, track_number=1),
        ],
        holds=[],
    )
    tracks = await store.get_tracks_for_album("al1")
    assert [t["identity"] for t in tracks] == ["d1t1", "d1t2", "d2t1"]


async def test_get_by_identity_and_all_tracks(db):
    await store.replace_catalog(
        artists=[_artist("ar1")], albums=[_album("al1")],
        tracks=[_track("t1", "al1")], holds=[],
    )
    assert (await store.get_artist("ar1"))["identity"] == "ar1"
    assert (await store.get_album("al1"))["identity"] == "al1"
    assert (await store.get_track("t1"))["identity"] == "t1"
    assert await store.get_artist("nope") is None
    assert [t["identity"] for t in await store.get_all_tracks()] == ["t1"]


# ── empty / emptiness ────────────────────────────────────────────────────────

async def test_empty_catalog_reads_are_empty(db):
    assert await store.get_artists() == []
    assert await store.get_albums() == []
    assert await store.get_all_tracks() == []
    assert await store.get_tracks_for_album("whatever") == []
    assert await store.is_empty() is True


async def test_is_empty_false_once_tracks_present(db):
    await store.replace_catalog([], [_album("al1")], [_track("t1", "al1")], [])
    assert await store.is_empty() is False


# ── atomic replace fully swaps (no torn / leftover rows) ─────────────────────

async def test_replace_fully_swaps_content(db):
    await store.replace_catalog(
        [_artist("ar1")], [_album("al1")], [_track("t1", "al1")],
        [_hold("t1", "plex", "m1:1", 0)],
    )
    # A second replace with disjoint identities must leave NOTHING from the first.
    await store.replace_catalog(
        [_artist("ar2")], [_album("al2")], [_track("t2", "al2")],
        [_hold("t2", "jelly", "j1", 0)],
    )
    assert [a["identity"] for a in await store.get_artists()] == ["ar2"]
    assert [a["identity"] for a in await store.get_albums()] == ["al2"]
    assert [t["identity"] for t in await store.get_all_tracks()] == ["t2"]
    assert await store.get_track("t1") is None
    assert await store.get_holds("track", "t1") == []  # old holds gone too


async def test_replace_rolls_back_on_error_leaving_prior_state(db, monkeypatch):
    await store.replace_catalog([_artist("ar1")], [_album("al1")], [_track("t1", "al1")], [])

    # Simulate a write failure mid-transaction (after the DELETEs have run, on
    # the first bulk insert). A genuine error — not an OR IGNORE-swallowed
    # constraint — so the except/rollback path must fire and undo EVERYTHING
    # since BEGIN, including the DELETEs that already emptied the tables.
    async def boom(*a, **k):
        raise RuntimeError("simulated write failure")
    monkeypatch.setattr(database._conn(), "executemany", boom)

    with pytest.raises(RuntimeError):
        await store.replace_catalog([_artist("ar2")], [_album("al2")],
                                    [_track("t2", "al2")], [])

    monkeypatch.undo()  # restore executemany before reading back
    assert [a["identity"] for a in await store.get_artists()] == ["ar1"]
    assert (await store.get_track("t1"))["identity"] == "t1"
    assert await store.get_track("t2") is None


# ── holds: priority ordering + per-source lookup ─────────────────────────────

async def test_holds_returned_in_priority_order(db):
    await store.replace_catalog(
        [], [], [_track("t1", "al1")],
        holds=[
            _hold("t1", "local", "/music/x.flac", 2, server_name="Disk"),
            _hold("t1", "plex", "m1:1", 0, server_name="Plex"),
            _hold("t1", "jelly", "j1", 1, server_name="Jelly"),
        ],
    )
    holds = await store.get_holds("track", "t1")
    assert [h["source_id"] for h in holds] == ["plex", "jelly", "local"]
    assert holds[0]["provider_local_key"] == "m1:1"
    assert holds[0]["server_name"] == "Plex"


async def test_identities_held_by_source(db):
    await store.replace_catalog(
        [], [], [_track("t1", "al1"), _track("t2", "al1")],
        holds=[
            _hold("t1", "plex", "m1:1", 0),
            _hold("t2", "plex", "m1:2", 0),
            _hold("t2", "jelly", "j2", 1),
        ],
    )
    assert sorted(await store.get_identities_held_by_source("plex")) == ["t1", "t2"]
    assert await store.get_identities_held_by_source("jelly") == ["t2"]
    assert await store.get_identities_held_by_source("gone") == []


# ── durable alias table is independent of content replacement ────────────────

async def test_alias_find_register_and_no_clobber(db):
    assert await store.find_identity("track", "mbid:abc") is None
    await store.register_alias("track", "mbid:abc", "ident-1")
    assert await store.find_identity("track", "mbid:abc") == "ident-1"
    # INSERT OR IGNORE: re-registering the same key never re-points it.
    await store.register_alias("track", "mbid:abc", "ident-2")
    assert await store.find_identity("track", "mbid:abc") == "ident-1"


async def test_alias_scoped_by_entity_type(db):
    await store.register_alias("track", "shared:1", "track-ident")
    await store.register_alias("album", "shared:1", "album-ident")
    assert await store.find_identity("track", "shared:1") == "track-ident"
    assert await store.find_identity("album", "shared:1") == "album-ident"


async def test_aliases_survive_content_replace(db):
    await store.register_alias("track", "mbid:abc", "ident-1")
    await store.register_alias("track", "hash:xyz", "ident-1")
    # A full content replace (what a rescan does) must NOT wipe identity aliases.
    await store.replace_catalog([_artist("ar1")], [_album("al1")], [_track("t1", "al1")], [])
    assert await store.find_identity("track", "mbid:abc") == "ident-1"
    assert await store.get_aliases_for_identity("track", "ident-1") == ["hash:xyz", "mbid:abc"]


# ── coexistence with the existing ratings / play-count tables ────────────────

async def test_coexists_with_ratings_and_play_counts(db):
    # Identity-keyed ratings/counts (the U7 target) live alongside the catalog.
    await store.replace_catalog([], [_album("al1")], [_track("ident-1", "al1")], [])
    await database.set_rating("ident-1", 5)
    await database.increment_play_count("track", "ident-1")
    assert await database.get_rating("ident-1") == 5
    assert await database.get_play_count("track", "ident-1") == 1
    # Catalog still intact after writing to the sibling tables.
    assert (await store.get_track("ident-1"))["identity"] == "ident-1"
