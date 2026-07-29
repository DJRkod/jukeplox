"""U10: JellyfinSource over the Jellyfin REST API.

Tested against a fake Jellyfin REST surface via ``httpx.MockTransport`` so the
real request-building (Authorization scheme, params, paths, pagination) is
exercised — not just parsing. Covers the plan's U10 scenarios: account sign-in
yielding token+userId (AE5), library/artist/album/track parse with ms durations
and ProviderIds→match_ids, pagination over StartIndex/Limit, header-borne stream
auth with no token in the URL (R25), missing ProviderIds still ingesting, and the
native_search+genres capability set.
"""

import json

import httpx
import pytest

from app.sources import jellyfin as jf
from app.sources.base import Capabilities, StreamTarget
from app.sources.jellyfin import JellyfinSource

SERVER = "http://jelly.local:8096"
UID = "u1"
TOKEN = "tok-abc"


def _json(payload, status=200):
    return httpx.Response(status, json=payload)


class FakeJellyfin:
    """A minimal in-memory Jellyfin REST surface for httpx.MockTransport."""

    def __init__(self):
        self.last_auth = None  # Authorization header of the most recent request
        self.views = {
            "Items": [
                {"Id": "lib-music", "Name": "Music", "CollectionType": "music"},
                {"Id": "lib-movies", "Name": "Movies", "CollectionType": "movies"},
            ]
        }
        self.artists = [
            {"Id": f"ar{i}", "Name": f"Artist {i}", "ImageTags": {"Primary": "x"},
             "ProviderIds": {"MusicBrainzArtist": f"mba{i}"}}
            for i in range(3)
        ]
        self.albums = [
            {"Id": f"al{i}", "Name": f"Album {i}", "AlbumArtist": "Artist 0",
             "ProductionYear": 2000 + i, "ChildCount": 10,
             "ImageTags": {"Primary": "x"},
             "ProviderIds": {"MusicBrainzAlbum": f"mbal{i}",
                             "MusicBrainzReleaseGroup": f"rg{i}"}}
            for i in range(3)
        ]
        self.tracks = [
            {"Id": "tr0", "Name": "Song Zero", "Artists": ["Artist 0"],
             "AlbumArtist": "Artist 0", "Album": "Album 0", "AlbumId": "al0",
             "RunTimeTicks": 1_800_000_000,  # 180 s → 180000 ms
             "IndexNumber": 1, "ParentIndexNumber": 1,
             "Genres": ["Rock"], "ProductionYear": 2000,
             "ImageTags": {"Primary": "x"},
             # carries BOTH a track id and an album id — only the track id must
             # land on the track entity, else two tracks of one album would
             # false-merge on the shared album mbid.
             "ProviderIds": {"MusicBrainzTrack": "mbt0", "MusicBrainzAlbum": "mbal0"}},
            {"Id": "tr1", "Name": "Song One", "Artists": ["Artist 0"],
             "AlbumArtist": "Artist 0", "Album": "Album 0", "AlbumId": "al0",
             "RunTimeTicks": 2_000_000_000,  # 200 s
             "IndexNumber": 2, "ParentIndexNumber": 1,
             "Genres": [], "ProductionYear": 2000,
             "ProviderIds": {}},  # missing MB ids → must still ingest
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = request.url.params
        self.last_auth = request.headers.get("Authorization", "")

        if path == "/Users/AuthenticateByName" and request.method == "POST":
            body = json.loads(request.content.decode())
            if body.get("Pw") == "bad":
                return _json({"error": "unauthorized"}, status=401)
            return _json({"AccessToken": TOKEN,
                          "User": {"Id": UID, "Name": body.get("Username")},
                          "ServerId": "srv1"})

        if path == f"/Users/{UID}/Views":
            return _json(self.views)

        if path == "/Artists/AlbumArtists":
            return _json(self._page(self.artists, params))

        if path == "/MusicGenres":
            return _json({"Items": [{"Name": "Rock"}, {"Name": "Jazz"}]})

        if path == "/Items":
            term = params.get("searchTerm")
            if term:
                return _json({"Items": self._search(term), "TotalRecordCount": 0})
            itypes = params.get("IncludeItemTypes", "")
            if "MusicAlbum" in itypes:
                return _json(self._page(self.albums, params))
            if "Audio" in itypes:
                return _json(self._page(self.tracks, params))
            return _json({"Items": [], "TotalRecordCount": 0})

        if path.startswith(f"/Users/{UID}/Items/"):
            iid = path.rsplit("/", 1)[-1]
            for coll in (self.tracks, self.albums, self.artists):
                for it in coll:
                    if it["Id"] == iid:
                        return _json(it)
            return _json({}, status=404)

        if "/Images/Primary" in path:
            return httpx.Response(200, content=b"IMG",
                                  headers={"content-type": "image/jpeg"})

        return _json({"Items": [], "TotalRecordCount": 0})

    def _search(self, term):
        term = term.lower()
        hits = []
        for t in self.tracks:
            if term in t["Name"].lower():
                hits.append({**t, "Type": "Audio"})
        for a in self.albums:
            if term in a["Name"].lower():
                hits.append({**a, "Type": "MusicAlbum"})
        for ar in self.artists:
            if term in ar["Name"].lower():
                hits.append({**ar, "Type": "MusicArtist"})
        return hits

    @staticmethod
    def _page(items, params):
        start = int(params.get("StartIndex", 0) or 0)
        limit = params.get("Limit")
        limit = int(limit) if limit is not None else len(items)
        return {"Items": items[start:start + limit], "TotalRecordCount": len(items)}


@pytest.fixture
def fake():
    return FakeJellyfin()


def _source(fake, **kw):
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return JellyfinSource(server_url=SERVER, token=TOKEN, user_id=UID,
                          source_id="jelly", server_name="Jelly", device_id="dev1",
                          http=http, **kw)


# ── identity + capabilities ──────────────────────────────────────────────────

def test_identity_and_capabilities(fake):
    s = _source(fake)
    assert s.source_id == "jelly"
    assert s.source_type == "jellyfin"
    caps = s.capabilities
    assert isinstance(caps, Capabilities)
    assert caps.native_search and caps.genres
    assert not (caps.similarity or caps.popular or caps.styles)


# ── account sign-in (token-only) ─────────────────────────────────────────────

async def test_authenticate_yields_token_and_userid(fake):
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    res = await jf.authenticate(SERVER, "admin", "secret", device_id="dev1", http=http)
    assert res["token"] == TOKEN
    assert res["user_id"] == UID
    # the pre-auth request used the MediaBrowser scheme and carried NO token
    assert fake.last_auth.startswith("MediaBrowser ")
    assert "Token=" not in fake.last_auth


async def test_authenticate_bad_credentials_raises(fake):
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    with pytest.raises(jf.JellyfinAuthError):
        await jf.authenticate(SERVER, "admin", "bad", device_id="dev1", http=http)


async def test_authenticated_requests_carry_the_token(fake):
    s = _source(fake)
    await s.get_libraries()
    assert 'Token="tok-abc"' in fake.last_auth


# ── browse parse ─────────────────────────────────────────────────────────────

async def test_get_libraries_filters_to_music_views(fake):
    s = _source(fake)
    libs = await s.get_libraries()
    assert [lib.key for lib in libs] == ["jelly:lib-music"]
    assert libs[0].type == "artist"          # normalized music-section type
    assert libs[0].server_name == "Jelly"


async def test_artists_albums_tracks_parse(fake):
    s = _source(fake)

    arts = await s.get_artists("jelly:lib-music")
    assert arts[0].id == "jelly:ar0"
    assert arts[0].title == "Artist 0"
    assert arts[0].thumb == "jelly:Items/ar0/Images/Primary"

    albs = await s.get_albums("jelly:lib-music")
    assert albs[0].id == "jelly:al0"
    assert albs[0].artist == "Artist 0"
    assert albs[0].year == 2000
    assert albs[0].track_count == 10
    assert albs[0].match_ids == {"musicbrainzalbum": "mbal0",
                                 "musicbrainzreleasegroup": "rg0"}

    trks = await s.get_tracks("jelly:lib-music")
    t0 = next(t for t in trks if t.id == "jelly:tr0")
    assert t0.duration_ms == 180000          # ticks ÷ 10,000 → ms
    assert t0.album_id == "jelly:al0"
    assert t0.disc_number == 1 and t0.track_number == 1
    assert t0.genre == "Rock"
    assert t0.stream_key == "jelly:tr0"
    # album-level mbid is scoped OUT of the track entity (false-merge guard)
    assert t0.match_ids == {"musicbrainztrack": "mbt0"}


async def test_track_with_missing_provider_ids_still_ingests(fake):
    s = _source(fake)
    trks = await s.get_tracks("jelly:lib-music")
    t1 = next(t for t in trks if t.id == "jelly:tr1")
    assert t1.match_ids == {}
    assert t1.title == "Song One" and t1.artist == "Artist 0"
    assert t1.duration_ms == 200000


async def test_pagination_returns_full_set(fake):
    # page_size 2 over 3 albums → two pages walked via StartIndex/Limit.
    s = _source(fake, page_size=2)
    albs = await s.get_albums("jelly:lib-music")
    assert [a.id for a in albs] == ["jelly:al0", "jelly:al1", "jelly:al2"]


async def test_get_albums_for_artist_scopes_request(fake):
    s = _source(fake)
    albs = await s.get_albums("jelly:lib-music", artist_id="jelly:ar0")
    assert [a.id for a in albs] == ["jelly:al0", "jelly:al1", "jelly:al2"]


async def test_get_track_and_album_single_item(fake):
    s = _source(fake)
    t = await s.get_track("jelly:tr0")
    assert t.title == "Song Zero" and t.duration_ms == 180000
    a = await s.get_album("jelly:al1")
    assert a.title == "Album 1" and a.year == 2001


async def test_genres(fake):
    s = _source(fake)
    assert await s.get_genres("jelly:lib-music") == ["Rock", "Jazz"]


# ── search ───────────────────────────────────────────────────────────────────

async def test_search_buckets_by_type(fake):
    s = _source(fake)
    res = await s.search("jelly:lib-music", "song")
    assert {t.id for t in res.tracks} == {"jelly:tr0", "jelly:tr1"}
    res2 = await s.search("jelly:lib-music", "album 1")
    assert [a.id for a in res2.albums] == ["jelly:al1"]
    res3 = await s.search("jelly:lib-music", "artist 2")
    assert [a.id for a in res3.artists] == ["jelly:ar2"]


# ── streaming (R25: token in the header, never the URL) ──────────────────────

def test_resolve_stream_puts_token_in_header_not_url(fake):
    s = _source(fake)
    target = s.resolve_stream("jelly:tr0")
    assert isinstance(target, StreamTarget)
    assert target.path is None
    assert target.url == f"{SERVER}/Audio/tr0/stream?static=true"
    # R25: no credential rides the URL to a LAN device
    assert "Token" not in target.url and "tok-abc" not in target.url
    assert 'Token="tok-abc"' in target.headers["Authorization"]
    assert target.headers["Authorization"].startswith("MediaBrowser ")


async def test_fetch_art(fake):
    s = _source(fake)
    arts = await s.get_artists("jelly:lib-music")
    data, ctype = await s.fetch_art(arts[0].thumb, width=48)
    assert data == b"IMG" and ctype == "image/jpeg"


# ── enrichments degrade (no similarity/popular/styles for Jellyfin) ──────────

async def test_unsupported_enrichments_degrade_to_empty(fake):
    s = _source(fake)
    assert await s.get_styles_with_counts("jelly:lib-music") == []
    assert await s.get_sonic_nearest("jelly:tr0") == []
    assert await s.get_artist_similar_names("jelly:tr0") == []
    assert await s.get_artist_popular_tracks("jelly:ar0") == []
