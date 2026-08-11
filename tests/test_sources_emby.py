"""U1: EmbySource over the Emby REST API.

Emby is a near-variant of Jellyfin, but the deltas matter and are validated here
against a fake Emby REST surface via ``httpx.MockTransport`` (so the real
request-building — the ``/emby/`` path prefix on EVERY route, the
``X-Emby-Authorization`` sign-in header, and the ``X-Emby-Token`` request header
— is exercised, not just parsing). Covers: account sign-in yielding token+userId
with the password never stored (AE), library/artist/album/track parse with ms
durations and ProviderIds→entity-scoped match_ids, ChildCount→track_count,
pagination over StartIndex/Limit, header-borne stream auth with no token in the
URL (R25 — Emby uses ``X-Emby-Token``, no force-proxy), missing ProviderIds still
ingesting, the native_search+genres capability set with the rest degrading, a 401
raising the adapter auth error, and a divergent field shape failing loud.
"""

import json

import httpx
import pytest

from app.sources import emby as em
from app.sources.base import Capabilities, StreamTarget
from app.sources.emby import EmbySource

SERVER = "http://emby.local:8096"
UID = "u1"
TOKEN = "tok-abc"

# every Emby route lives under /emby (delta from Jellyfin's root paths).
PREFIX = "/emby"


def _json(payload, status=200):
    return httpx.Response(status, json=payload)


class FakeEmby:
    """A minimal in-memory Emby REST surface for httpx.MockTransport."""

    def __init__(self):
        self.last_auth = None       # X-Emby-Authorization header (sign-in)
        self.last_token = None      # X-Emby-Token header (authed requests)
        self.paths_seen = []        # every request path — assert the /emby prefix
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
        self.paths_seen.append(path)
        self.last_auth = request.headers.get("X-Emby-Authorization", "")
        self.last_token = request.headers.get("X-Emby-Token", "")

        # every route must be under /emby — strip it for routing, and if it is
        # missing route to a loud 404 so a dropped prefix can't silently pass.
        if not path.startswith(PREFIX + "/"):
            return _json({"error": "missing /emby prefix"}, status=404)
        rest = path[len(PREFIX):]

        if rest == "/Users/AuthenticateByName" and request.method == "POST":
            body = json.loads(request.content.decode())
            if body.get("Pw") == "bad":
                return _json({"error": "unauthorized"}, status=401)
            return _json({"AccessToken": TOKEN,
                          "User": {"Id": UID, "Name": body.get("Username")},
                          "ServerId": "srv1"})

        if rest == f"/Users/{UID}/Views":
            return _json(self.views)

        if rest == "/Artists/AlbumArtists":
            return _json(self._page(self.artists, params))

        if rest == "/MusicGenres":
            return _json({"Items": [{"Name": "Rock"}, {"Name": "Jazz"}]})

        if rest == "/Items":
            term = params.get("searchTerm")
            if term:
                return _json({"Items": self._search(term), "TotalRecordCount": 0})
            itypes = params.get("IncludeItemTypes", "")
            if "MusicAlbum" in itypes:
                return _json(self._page(self.albums, params))
            if "Audio" in itypes:
                return _json(self._page(self.tracks, params))
            return _json({"Items": [], "TotalRecordCount": 0})

        if rest.startswith(f"/Users/{UID}/Items/"):
            iid = rest.rsplit("/", 1)[-1]
            for coll in (self.tracks, self.albums, self.artists):
                for it in coll:
                    if it["Id"] == iid:
                        return _json(it)
            return _json({}, status=404)

        if "/Images/Primary" in rest:
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
    return FakeEmby()


def _source(fake, **kw):
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return EmbySource(server_url=SERVER, token=TOKEN, user_id=UID,
                      source_id="emby", server_name="Emby", device_id="dev1",
                      http=http, **kw)


# ── identity + capabilities ──────────────────────────────────────────────────

def test_identity_and_capabilities(fake):
    s = _source(fake)
    assert s.source_id == "emby"
    assert s.source_type == "emby"
    caps = s.capabilities
    assert isinstance(caps, Capabilities)
    assert caps.native_search and caps.genres
    assert not (caps.similarity or caps.popular or caps.styles)


def test_url_borne_auth_is_false(fake):
    # Emby is header-auth (X-Emby-Token) — it must NOT be force-proxied like a
    # URL-auth source. Symmetric to test_sources_subsonic.py::test_url_borne_auth_flag.
    assert _source(fake).url_borne_auth is False


# ── account sign-in (token-only; password never stored) ──────────────────────

async def test_authenticate_yields_token_and_userid(fake):
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    res = await em.authenticate(SERVER, "admin", "secret", device_id="dev1", http=http)
    assert res["token"] == TOKEN
    assert res["user_id"] == UID
    # sign-in used the X-Emby-Authorization header form and carried NO token
    assert 'MediaBrowser' in fake.last_auth
    assert "Token=" not in fake.last_auth
    # returned mapping retains only the token + userId (+server id) — never the
    # password, in any casing.
    assert set(res) <= {"token", "user_id", "server_id"}
    assert "secret" not in json.dumps(res)


async def test_authenticate_bad_credentials_raises(fake):
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    with pytest.raises(em.EmbyAuthError):
        await em.authenticate(SERVER, "admin", "bad", device_id="dev1", http=http)


async def test_authenticated_requests_carry_the_token_header(fake):
    s = _source(fake)
    await s.get_libraries()
    # delta from Jellyfin: token rides X-Emby-Token, not the Authorization value.
    assert fake.last_token == TOKEN


# ── /emby prefix on every route (a load-bearing Emby delta) ──────────────────

async def test_every_route_is_under_emby_prefix(fake):
    s = _source(fake)
    await s.get_libraries()
    await s.get_artists("emby:lib-music")
    await s.get_albums("emby:lib-music")
    await s.get_tracks("emby:lib-music")
    await s.get_track("emby:tr0")
    await s.get_album("emby:al0")
    await s.search("emby:lib-music", "song")
    await s.get_genres("emby:lib-music")
    assert fake.paths_seen  # sanity
    assert all(p.startswith(PREFIX + "/") for p in fake.paths_seen), fake.paths_seen


# ── browse parse ─────────────────────────────────────────────────────────────

async def test_get_libraries_filters_to_music_views(fake):
    s = _source(fake)
    libs = await s.get_libraries()
    assert [lib.key for lib in libs] == ["emby:lib-music"]
    assert libs[0].type == "artist"          # normalized music-section type
    assert libs[0].server_name == "Emby"


async def test_artists_albums_tracks_parse(fake):
    s = _source(fake)

    arts = await s.get_artists("emby:lib-music")
    assert arts[0].id == "emby:ar0"
    assert arts[0].title == "Artist 0"
    assert arts[0].thumb == "emby:Items/ar0/Images/Primary"

    albs = await s.get_albums("emby:lib-music")
    assert albs[0].id == "emby:al0"
    assert albs[0].artist == "Artist 0"
    assert albs[0].year == 2000
    assert albs[0].track_count == 10        # ChildCount → track_count
    assert albs[0].match_ids == {"musicbrainzalbum": "mbal0",
                                 "musicbrainzreleasegroup": "rg0"}

    trks = await s.get_tracks("emby:lib-music")
    t0 = next(t for t in trks if t.id == "emby:tr0")
    assert t0.duration_ms == 180000          # ticks ÷ 10,000 → ms
    assert t0.album_id == "emby:al0"
    assert t0.disc_number == 1 and t0.track_number == 1
    assert t0.genre == "Rock"
    assert t0.stream_key == "emby:tr0"
    # album-level mbid is scoped OUT of the track entity (false-merge guard)
    assert t0.match_ids == {"musicbrainztrack": "mbt0"}


async def test_track_with_missing_provider_ids_still_ingests(fake):
    s = _source(fake)
    trks = await s.get_tracks("emby:lib-music")
    t1 = next(t for t in trks if t.id == "emby:tr1")
    assert t1.match_ids == {}
    assert t1.title == "Song One" and t1.artist == "Artist 0"
    assert t1.duration_ms == 200000


async def test_pagination_returns_full_set(fake):
    # page_size 2 over 3 albums → two pages walked via StartIndex/Limit.
    s = _source(fake, page_size=2)
    albs = await s.get_albums("emby:lib-music")
    assert [a.id for a in albs] == ["emby:al0", "emby:al1", "emby:al2"]


async def test_get_albums_for_artist_scopes_request(fake):
    s = _source(fake)
    albs = await s.get_albums("emby:lib-music", artist_id="emby:ar0")
    assert [a.id for a in albs] == ["emby:al0", "emby:al1", "emby:al2"]


async def test_get_track_and_album_single_item(fake):
    s = _source(fake)
    t = await s.get_track("emby:tr0")
    assert t.title == "Song Zero" and t.duration_ms == 180000
    a = await s.get_album("emby:al1")
    assert a.title == "Album 1" and a.year == 2001


async def test_genres(fake):
    s = _source(fake)
    assert await s.get_genres("emby:lib-music") == ["Rock", "Jazz"]


# ── search ───────────────────────────────────────────────────────────────────

async def test_search_buckets_by_type(fake):
    s = _source(fake)
    res = await s.search("emby:lib-music", "song")
    assert {t.id for t in res.tracks} == {"emby:tr0", "emby:tr1"}
    res2 = await s.search("emby:lib-music", "album 1")
    assert [a.id for a in res2.albums] == ["emby:al1"]
    res3 = await s.search("emby:lib-music", "artist 2")
    assert [a.id for a in res3.artists] == ["emby:ar2"]


# ── streaming (R25: token in the header, never the URL) ──────────────────────

def test_resolve_stream_puts_token_in_header_not_url(fake):
    s = _source(fake)
    target = s.resolve_stream("emby:tr0")
    assert isinstance(target, StreamTarget)
    assert target.path is None
    # credential-free stream url, under the /emby prefix
    assert target.url == f"{SERVER}{PREFIX}/Audio/tr0/stream?static=true"
    # R25: no credential rides the URL to a LAN device
    assert "Token" not in target.url and "tok-abc" not in target.url
    # header-auth: X-Emby-Token carries the token (no MediaBrowser Authorization)
    assert target.headers.get("X-Emby-Token") == "tok-abc"


async def test_fetch_art(fake):
    s = _source(fake)
    arts = await s.get_artists("emby:lib-music")
    data, ctype = await s.fetch_art(arts[0].thumb, width=48)
    assert data == b"IMG" and ctype == "image/jpeg"


# ── auth error on a rejected token (401 on a normal request) ─────────────────

async def test_token_rejected_raises_auth_error():
    def reject(request):
        return _json({"error": "unauthorized"}, status=401)
    http = httpx.AsyncClient(transport=httpx.MockTransport(reject))
    s = EmbySource(server_url=SERVER, token="stale", user_id=UID,
                   source_id="emby", device_id="dev1", http=http)
    with pytest.raises(em.EmbyAuthError):
        await s.get_libraries()


# ── divergent field shape fails loud (no silent degrade) ─────────────────────

async def test_divergent_track_shape_fails_loud():
    # An Emby item that omits the required ``Id`` must not silently parse into a
    # malformed Track — it should raise so the divergence is visible, not
    # ingested as junk.
    def handler(request):
        rest = request.url.path[len(PREFIX):]
        if rest == "/Items":
            return _json({"Items": [{"Name": "No Id Here", "RunTimeTicks": 1}],
                          "TotalRecordCount": 1})
        return _json({"Items": [], "TotalRecordCount": 0})
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    s = EmbySource(server_url=SERVER, token=TOKEN, user_id=UID,
                   source_id="emby", device_id="dev1", http=http)
    with pytest.raises(KeyError):
        await s.get_tracks("emby:lib-music")


# ── enrichments degrade (no similarity/popular/styles for Emby) ──────────────

async def test_unsupported_enrichments_degrade_to_empty(fake):
    s = _source(fake)
    assert await s.get_styles_with_counts("emby:lib-music") == []
    assert await s.get_sonic_nearest("emby:tr0") == []
    assert await s.get_artist_similar_names("emby:tr0") == []
    assert await s.get_artist_popular_tracks("emby:ar0") == []
