"""U6: catalog scan — the pure ``build_catalog`` transform and the I/O
``scan_and_replace`` orchestration (crawl → dedup → atomic replace) including the
generalized "every section failed → don't wipe" guard.
"""

import pytest

from app import database
from app.catalog import scan, store
from app.config import Settings
from app.models import Album, Artist, Library, Track


# ── fixtures ─────────────────────────────────────────────────────────────────

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


def _artist(aid, title="Act"):
    return Artist(id=aid, title=title)


def _album(aid, title="Rec", artist="Act", tc=2, year=2020):
    return Album(id=aid, title=title, artist=artist, track_count=tc, year=year)


def _track(tid, album_id, title="Song", artist="Act", album="Rec", dur=180000,
           num=1, disc=1, stream_key=None, match_ids=None):
    return Track(id=tid, title=title, artist=artist, album=album, duration_ms=dur,
                 album_id=album_id, track_number=num, disc_number=disc,
                 stream_key=stream_key or f"{tid}", match_ids=match_ids or {})


class _FakeSource:
    """Minimal MusicSource stand-in for scan crawl tests."""

    def __init__(self, source_id, libs, artists=None, albums=None, tracks=None,
                 fail=False, source_type="plex"):
        self.source_id = source_id
        self.source_type = source_type
        self._libs = libs
        self._artists = artists or []
        self._albums = albums or []
        self._tracks = tracks or []
        self._fail = fail

    async def get_libraries(self):
        if self._fail:
            raise RuntimeError("source down")
        return self._libs

    async def get_artists(self, key):
        return self._artists

    async def get_albums(self, key):
        return self._albums

    async def get_tracks(self, key):
        return self._tracks


class _FakeRegistry:
    def __init__(self, sources):
        self.sources = sources


# ── build_catalog (pure) ─────────────────────────────────────────────────────

def test_build_catalog_single_source_links_tracks_to_album():
    built = scan.build_catalog([{
        "source_id": "m1", "priority": 0, "server_name": "Plex",
        "artists": [_artist("m1:ar")],
        "albums": [_album("m1:al")],
        "tracks": [_track("m1:t1", "m1:al", num=1), _track("m1:t2", "m1:al", num=2)],
    }])
    assert len(built["artists"]) == 1
    assert len(built["albums"]) == 1
    assert len(built["tracks"]) == 2
    album_ident = built["albums"][0]["identity"]
    assert all(t["album_identity"] == album_ident for t in built["tracks"])
    # one hold per track, carrying the resolvable stream key + source
    track_holds = [h for h in built["holds"] if h["entity_type"] == "track"]
    assert {h["provider_local_key"] for h in track_holds} == {"m1:t1", "m1:t2"}
    assert all(h["source_id"] == "m1" for h in track_holds)


def test_build_catalog_merges_same_album_across_sources_with_two_holds():
    # AE2-style by strict-name (matching track_count, no IDs): two sources, same
    # release → one album with two holds, one per source.
    common = dict(title="Rec", artist="Act", tc=2)
    built = scan.build_catalog([
        {"source_id": "m1", "priority": 0, "server_name": "Plex",
         "artists": [], "albums": [_album("m1:al", **common)], "tracks": []},
        {"source_id": "jelly", "priority": 1, "server_name": "Jelly",
         "artists": [], "albums": [_album("jelly:al", **common)], "tracks": []},
    ])
    assert len(built["albums"]) == 1
    album_holds = [h for h in built["holds"] if h["entity_type"] == "album"]
    assert {h["source_id"] for h in album_holds} == {"m1", "jelly"}


def test_build_catalog_merges_track_across_sources_within_tolerance():
    t1 = _track("m1:t", "m1:al", dur=180000, stream_key="m1:partA")
    t2 = _track("jelly:t", "jelly:al", dur=181000, stream_key="jelly:partB")  # +1s
    built = scan.build_catalog([
        {"source_id": "m1", "priority": 0, "server_name": "Plex",
         "artists": [], "albums": [_album("m1:al")], "tracks": [t1]},
        {"source_id": "jelly", "priority": 1, "server_name": "Jelly",
         "artists": [], "albums": [_album("jelly:al")], "tracks": [t2]},
    ])
    assert len(built["tracks"]) == 1
    holds = [h for h in built["holds"] if h["entity_type"] == "track"]
    assert {h["provider_local_key"] for h in holds} == {"m1:partA", "jelly:partB"}


def test_build_catalog_ms_match_ids_drive_id_merge():
    a1 = _album("m1:al", title="X", artist="A", tc=99)   # mismatched count...
    a2 = _album("jelly:al", title="Y", artist="B", tc=1)  # ...and name, but same id
    a1.match_ids = {"mbid": "same"}
    a2.match_ids = {"mbid": "same"}
    built = scan.build_catalog([
        {"source_id": "m1", "priority": 0, "server_name": "", "artists": [], "albums": [a1], "tracks": []},
        {"source_id": "jelly", "priority": 1, "server_name": "", "artists": [], "albums": [a2], "tracks": []},
    ])
    assert len(built["albums"]) == 1  # ID-first overrides name/count mismatch
    assert ("album", "mbid:same", "mbid:same") in built["aliases"]


def test_build_catalog_drops_album_orphaned_by_track_dedup():
    # ce-debug 2026-06-29 (Bug C): tracks dedup across sources but their albums do
    # NOT (one source omits track_count, e.g. Plex leafCount=None vs local's known
    # count) → the merged track takes the representative source's album_identity,
    # orphaning the other source's album row with zero children. That empty,
    # duplicate album must be dropped (it showed as an empty FLAC album on jelly).
    common = dict(title="Song", artist="Act", album="Rec", dur=180000, num=1, disc=1)
    built = scan.build_catalog([
        {"source_id": "m1", "priority": 0, "server_name": "Plex", "artists": [],
         "albums": [_album("m1:al", tc=None)],
         "tracks": [_track("m1:t", "m1:al", stream_key="m1:p", **common)]},
        {"source_id": "local", "priority": 1, "server_name": "Home", "artists": [],
         "albums": [_album("local:al", tc=2)],
         "tracks": [_track("local:t", "local:al", stream_key="local:p", **common)]},
    ])
    assert len(built["tracks"]) == 1                 # tracks merged (duration within tol)
    assert len(built["albums"]) == 1                 # orphaned (trackless) album dropped
    kept = built["albums"][0]["identity"]
    assert built["tracks"][0]["album_identity"] == kept   # survivor is the linked album
    album_holds = [h for h in built["holds"] if h["entity_type"] == "album"]
    assert all(h["identity"] == kept for h in album_holds)  # no holds for a dropped album


# ── scan_and_replace (I/O + guard) ───────────────────────────────────────────

async def test_scan_and_replace_writes_catalog(db):
    src = _FakeSource(
        "m1", libs=[Library(key="m1:1", title="Music", type="artist", server_name="Plex")],
        artists=[_artist("m1:ar")], albums=[_album("m1:al")],
        tracks=[_track("m1:t1", "m1:al")],
    )
    ok = await scan.scan_and_replace(_FakeRegistry([src]))
    assert ok is True
    tracks = await store.get_all_tracks()
    assert len(tracks) == 1
    holds = await store.get_holds("track", tracks[0]["identity"])
    assert holds[0]["source_id"] == "m1"


async def test_scan_and_replace_no_sources_returns_false(db):
    assert await scan.scan_and_replace(_FakeRegistry([])) is False
    assert await scan.scan_and_replace(None) is False


async def test_scan_and_replace_all_failed_does_not_wipe(db):
    # Seed a good catalog, then a scan whose only source fails to enumerate must
    # leave the prior catalog intact (don't-wipe guard).
    await store.replace_catalog([], [_album_row()], [_track_row()], [])
    bad = _FakeSource("m1", libs=[], fail=True)
    ok = await scan.scan_and_replace(_FakeRegistry([bad]))
    assert ok is False
    assert len(await store.get_all_tracks()) == 1  # prior state retained


async def test_scan_and_replace_partial_failure_catalogs_good_source(db):
    good = _FakeSource(
        "m1", libs=[Library(key="m1:1", title="M", type="artist", server_name="Plex")],
        artists=[_artist("m1:ar")], albums=[_album("m1:al")], tracks=[_track("m1:t1", "m1:al")],
    )
    bad = _FakeSource("jelly", libs=[], fail=True)
    ok = await scan.scan_and_replace(_FakeRegistry([good, bad]))
    assert ok is True
    tracks = await store.get_all_tracks()
    assert len(tracks) == 1  # the good source is catalogued; the failed one contributes nothing


async def test_scan_and_replace_priority_follows_source_order(db):
    # Same track held by both sources; the holds carry each source's order as priority.
    t_plex = _track("m1:t", "m1:al", stream_key="m1:p")
    t_jelly = _track("jelly:t", "jelly:al", stream_key="jelly:p")
    s_plex = _FakeSource("m1", libs=[Library(key="m1:1", title="M", type="artist", server_name="P")],
                         artists=[], albums=[_album("m1:al")], tracks=[t_plex])
    s_jelly = _FakeSource("jelly", libs=[Library(key="jelly:1", title="M", type="artist", server_name="J")],
                          artists=[], albums=[_album("jelly:al")], tracks=[t_jelly])
    await scan.scan_and_replace(_FakeRegistry([s_plex, s_jelly]))
    tracks = await store.get_all_tracks()
    assert len(tracks) == 1
    holds = await store.get_holds("track", tracks[0]["identity"])
    assert [h["source_id"] for h in holds] == ["m1", "jelly"]  # priority order = source order
    assert [h["priority"] for h in holds] == [0, 1]


# ── enabled-library filter applies to every source type (ce-debug 2026-06-29) ──

async def test_scan_filters_non_plex_library_by_enabled_keys(db):
    """A Jellyfin library NOT in the enabled set must not be crawled — the
    enabled-library checkbox gates import for EVERY source type, not just Plex.
    Regression: scan filtered only Plex sources, so unchecked Jellyfin libraries
    were imported anyway."""
    plex = _FakeSource(
        "m1", source_type="plex",
        libs=[Library(key="m1:1", title="Music", type="artist", server_name="Plex")],
        artists=[_artist("m1:ar")], albums=[_album("m1:al")],
        tracks=[_track("m1:t1", "m1:al")],
    )
    jelly = _FakeSource(
        "jelly", source_type="jellyfin",
        libs=[Library(key="jelly:1", title="Music", type="artist", server_name="Den")],
        artists=[_artist("jelly:ar", title="JAct")],
        albums=[_album("jelly:al", artist="JAct")],
        tracks=[_track("jelly:t1", "jelly:al", artist="JAct", album="JRec")],
    )
    # Only the Plex library is enabled; the Jellyfin library is unchecked.
    ok = await scan.scan_and_replace(_FakeRegistry([plex, jelly]), {"m1:1"})
    assert ok is True
    tracks = await store.get_all_tracks()
    keys = {h["provider_local_key"]
            for t in tracks for h in await store.get_holds("track", t["identity"])}
    assert keys == {"m1:t1"}  # the unchecked Jellyfin library contributes nothing


async def test_scan_includes_enabled_non_plex_library(db):
    """A Jellyfin library that IS enabled crawls normally."""
    jelly = _FakeSource(
        "jelly", source_type="jellyfin",
        libs=[Library(key="jelly:1", title="Music", type="artist", server_name="Den")],
        artists=[_artist("jelly:ar")], albums=[_album("jelly:al")],
        tracks=[_track("jelly:t1", "jelly:al")],
    )
    ok = await scan.scan_and_replace(_FakeRegistry([jelly]), {"jelly:1"})
    assert ok is True
    assert len(await store.get_all_tracks()) == 1


# Row helpers for the don't-wipe seed (store-shaped dicts, not dataclasses).
def _album_row():
    return {"identity": "seed-al", "title": "Seed", "title_base": "seed",
            "artist": "Seed", "artist_base_key": "seed"}


def _track_row():
    return {"identity": "seed-t", "title": "Seed", "title_base": "seed",
            "artist": "Seed", "artist_base_key": "seed", "album_identity": "seed-al",
            "duration_ms": 1000}
