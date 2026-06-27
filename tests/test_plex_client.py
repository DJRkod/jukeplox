"""Tests for the Plex API client and auth flow. All HTTP calls are mocked."""

import pytest

from app.plex.auth import PlexAuthError, discover_server, generate_pin, poll_pin
from app.plex.client import PlexClient
from app.plex.models import Album, Artist, Library, Track


# ── helpers ───────────────────────────────────────────────────────────────────

def make_client(**kwargs) -> PlexClient:
    return PlexClient(
        server_url=kwargs.get("server_url", "http://plex.local:32400"),
        token=kwargs.get("token", "tok123"),
        client_id=kwargs.get("client_id", "test-client"),
        max_concurrency=kwargs.get("max_concurrency"),
    )


# ── concurrency ceiling (U1) ──────────────────────────────────────────────────

async def test_get_concurrency_is_capped_per_client():
    """_get serializes through the per-client semaphore: with cap=4, no more
    than 4 underlying HTTP gets run at once even when 20 are fired. Covers AE1."""
    import asyncio
    from unittest.mock import MagicMock
    client = make_client(max_concurrency=4)
    in_flight = 0
    peak = 0

    async def fake_get(*args, **kwargs):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"MediaContainer": {}}
        resp.raise_for_status = MagicMock()
        return resp

    client._http.get = fake_get
    await asyncio.gather(*[client._get(f"/p{i}") for i in range(20)])
    assert 0 < peak <= 4


async def test_fetch_art_shares_the_same_cap():
    """fetch_art uses the same per-client semaphore as _get, so mixed data +
    art traffic is bounded by one ceiling. Covers AE5."""
    import asyncio
    from unittest.mock import MagicMock
    client = make_client(max_concurrency=3)
    in_flight = 0
    peak = 0

    async def fake_get(*args, **kwargs):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"MediaContainer": {}}
        resp.content = b"x"
        resp.headers = {"content-type": "image/jpeg"}
        resp.raise_for_status = MagicMock()
        return resp

    client._http.get = fake_get
    calls = ([client._get(f"/p{i}") for i in range(10)]
             + [client.fetch_art(f"/a{i}") for i in range(10)])
    await asyncio.gather(*calls)
    assert 0 < peak <= 3


async def test_fetch_art_width_uses_photo_transcoder():
    """fetch_art(width=N) requests Plex's photo transcoder with the bare art path
    as the `url` param, so a 48px row doesn't decode a full-size cover
    (2026-06-25 deep-jump reveal fix)."""
    from unittest.mock import MagicMock
    client = make_client()
    seen = {}

    async def fake_get(url, params=None, headers=None):
        seen["url"] = url
        seen["params"] = params or {}
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b"small"
        resp.headers = {"content-type": "image/jpeg"}
        resp.raise_for_status = MagicMock()
        return resp

    client._http.get = fake_get
    data, ct = await client.fetch_art("/library/metadata/5/thumb/9", width=144)
    assert data == b"small"
    assert seen["url"].endswith("/photo/:/transcode")
    assert seen["params"]["width"] == 144 and seen["params"]["height"] == 144
    assert seen["params"]["url"] == "/library/metadata/5/thumb/9"


async def test_fetch_art_width_falls_back_to_full_on_transcode_error():
    """A transcoder failure must never break art — fetch_art retries the full image."""
    from unittest.mock import MagicMock
    client = make_client()
    calls = []

    async def fake_get(url, params=None, headers=None):
        calls.append(url)
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b"full"
        resp.headers = {"content-type": "image/jpeg"}
        if url.endswith("/photo/:/transcode"):
            resp.raise_for_status = MagicMock(side_effect=Exception("no transcoder"))
        else:
            resp.raise_for_status = MagicMock()
        return resp

    client._http.get = fake_get
    data, ct = await client.fetch_art("/library/metadata/5/thumb/9", width=144)
    assert data == b"full"
    assert any(u.endswith("/photo/:/transcode") for u in calls)
    assert any(u.endswith("/library/metadata/5/thumb/9") for u in calls)


def test_default_concurrency_comes_from_settings():
    from app.config import settings
    client = make_client()
    assert client._sem._value == settings.plex_max_concurrency


def test_explicit_concurrency_overrides_default():
    client = make_client(max_concurrency=2)
    assert client._sem._value == 2


def test_each_client_has_an_independent_cap():
    """Per-server isolation (R3): one server's saturation can't consume another's budget."""
    assert make_client()._sem is not make_client()._sem


def _sections_response(*libs):
    return {"MediaContainer": {"Directory": list(libs)}}


def _metadata_response(*items):
    return {"MediaContainer": {"Metadata": list(items)}}


def _dir_response(*items):
    return {"MediaContainer": {"Directory": list(items)}}


# ── helper: _bare_rating_key ──────────────────────────────────────────────────

def test_bare_rating_key_returns_none_for_none():
    client = make_client()
    assert client._bare_rating_key(None) is None


def test_bare_rating_key_returns_empty_for_empty():
    client = make_client()
    assert client._bare_rating_key("") == ""


def test_bare_rating_key_returns_bare_unchanged():
    client = make_client()
    assert client._bare_rating_key("12345") == "12345"


def test_bare_rating_key_strips_same_server_prefix():
    client = make_client()
    client.machine_id = "machineA"
    assert client._bare_rating_key("machineA:12345") == "12345"


def test_bare_rating_key_strips_cross_server_prefix():
    """Cross-server case: prefix doesn't match the current client's machine_id."""
    client = make_client()
    client.machine_id = "machineA"
    assert client._bare_rating_key("machineB:12345") == "12345"


def test_bare_rating_key_splits_on_first_colon_only():
    """Defensive: unusual ids with multiple colons keep everything after the first."""
    client = make_client()
    assert client._bare_rating_key("machineA:deep:path") == "deep:path"


async def test_get_albums_cross_server_artist_id_still_strips_to_bare(respx_mock):
    """Regression: artist_id with a different machine_id prefix is reduced to a
    bare numeric ratingKey before hitting Plex (existing inline-strip behavior
    preserved by the helper refactor)."""
    import httpx
    route = respx_mock.get("http://plex.local:32400/library/sections/1/all").mock(
        return_value=httpx.Response(200, json={"MediaContainer": {"Metadata": [
            {"ratingKey": "20", "title": "X", "parentTitle": "Y",
             "parentRatingKey": "42", "year": 2020, "subtype": None},
        ]}})
    )
    client = make_client()
    client.machine_id = "machineA"
    # artist_id belongs to machineB (different server)
    albums = await client.get_albums("1", artist_id="machineB:42")
    assert len(albums) == 1
    assert route.called
    # Confirm Plex saw the bare numeric ratingKey, not the compound form
    assert route.calls[0].request.url.params.get("parentRatingKey") == "42"


# ── get_albums(artist) fallback characterization (2026-06-21 plan U5) ────────
# The warm browse drill-in serves releases from the persistent index; this path
# only runs on a cold/missed index. These tests PIN its two load-bearing
# behaviors so a future switch to /children can't silently regress them.

async def test_get_albums_artist_filters_to_requested_parent_client_side(respx_mock):
    """Plex may ignore parentRatingKey and return the whole section; the client
    MUST filter to the requested artist's releases itself."""
    import httpx
    respx_mock.get("http://plex.local:32400/library/sections/1/all").mock(
        return_value=httpx.Response(200, json={"MediaContainer": {"Metadata": [
            {"ratingKey": "10", "title": "Mine A", "parentTitle": "Me",
             "parentRatingKey": "42", "year": 2001, "subtype": "album"},
            {"ratingKey": "11", "title": "Mine B", "parentTitle": "Me",
             "parentRatingKey": "42", "year": 2003, "subtype": "album"},
            {"ratingKey": "99", "title": "Someone Else", "parentTitle": "Other",
             "parentRatingKey": "77", "year": 1999, "subtype": "album"},
        ]}})
    )
    client = make_client()
    albums = await client.get_albums("1", artist_id="42")
    assert sorted(a.title for a in albums) == ["Mine A", "Mine B"]  # 77 excluded


async def test_get_albums_artist_preserves_non_primary_subtypes(respx_mock):
    """EPs / singles / live releases (non-primary subtypes) MUST survive — the
    reason this path uses section-all rather than /children."""
    import httpx
    respx_mock.get("http://plex.local:32400/library/sections/1/all").mock(
        return_value=httpx.Response(200, json={"MediaContainer": {"Metadata": [
            {"ratingKey": "10", "title": "LP", "parentTitle": "Me",
             "parentRatingKey": "42", "subtype": "album"},
            {"ratingKey": "11", "title": "The EP", "parentTitle": "Me",
             "parentRatingKey": "42", "subtype": "ep"},
            {"ratingKey": "12", "title": "A Single", "parentTitle": "Me",
             "parentRatingKey": "42", "subtype": "single"},
            {"ratingKey": "13", "title": "Live!", "parentTitle": "Me",
             "parentRatingKey": "42", "subtype": "live"},
        ]}})
    )
    client = make_client()
    albums = await client.get_albums("1", artist_id="42")
    assert {a.subtype for a in albums} == {"album", "ep", "single", "live"}
    assert len(albums) == 4


# ── auth: generate_pin ────────────────────────────────────────────────────────

async def test_generate_pin_returns_expected_keys(respx_mock):
    import respx, httpx
    respx_mock.post("https://plex.tv/api/v2/pins").mock(
        return_value=httpx.Response(201, json={"id": 42, "code": "ABCD"})
    )
    result = await generate_pin("my-client")
    assert result["id"] == 42
    assert result["code"] == "ABCD"
    assert result["client_id"] == "my-client"
    assert "app.plex.tv" in result["auth_url"]


async def test_generate_pin_uses_provided_client_id(respx_mock):
    import respx, httpx
    respx_mock.post("https://plex.tv/api/v2/pins").mock(
        return_value=httpx.Response(201, json={"id": 1, "code": "XY"})
    )
    result = await generate_pin("specific-id")
    assert result["client_id"] == "specific-id"


# ── auth: poll_pin ────────────────────────────────────────────────────────────

async def test_poll_pin_pending_returns_none(respx_mock):
    import httpx
    respx_mock.get("https://plex.tv/api/v2/pins/99").mock(
        return_value=httpx.Response(200, json={"authToken": None})
    )
    result = await poll_pin(99, "cid")
    assert result is None


async def test_poll_pin_resolved_returns_token(respx_mock):
    import httpx
    respx_mock.get("https://plex.tv/api/v2/pins/99").mock(
        return_value=httpx.Response(200, json={"authToken": "mytoken"})
    )
    result = await poll_pin(99, "cid")
    assert result == "mytoken"


# ── auth: discover_server ─────────────────────────────────────────────────────

async def test_discover_server_returns_local_connection(respx_mock):
    import httpx
    respx_mock.get("https://plex.tv/api/v2/resources").mock(
        return_value=httpx.Response(200, json=[{
            "provides": "server",
            "clientIdentifier": "abc123",
            "name": "My Server",
            "owned": True,
            "sourceTitle": "",
            "accessToken": "srv-token",
            "connections": [
                {"uri": "https://remote.example.com", "local": False},
                {"uri": "http://192.168.1.10:32400", "local": True},
            ]
        }])
    )
    # Local is reachable here → owned server keeps the fast local URI.
    respx_mock.get("http://192.168.1.10:32400/identity").mock(return_value=httpx.Response(200))
    url = await discover_server("tok", "cid")
    assert url == "http://192.168.1.10:32400"


async def test_discover_server_raises_when_none_found(respx_mock):
    import httpx
    respx_mock.get("https://plex.tv/api/v2/resources").mock(
        return_value=httpx.Response(200, json=[])
    )
    with pytest.raises(PlexAuthError):
        await discover_server("tok", "cid")


async def test_discover_owned_skips_unreachable_local_for_reachable_remote(respx_mock):
    """Owned server whose LOCAL connection is unreachable (NAT'd container /
    remote deploy) must save a reachable remote URL, not the dead local one —
    else its libraries never list. Reproduces the Docker-on-Windows
    'only shared libraries show, not my own' bug."""
    import httpx
    LOCAL = "https://172-16-1-2.hash.plex.direct:32400"
    REMOTE = "https://173-230-126-16.hash.plex.direct:32401"
    respx_mock.get("https://plex.tv/api/v2/resources").mock(
        return_value=httpx.Response(200, json=[{
            "provides": "server",
            "clientIdentifier": "abc123",
            "name": "My Server",
            "owned": True,
            "accessToken": "srv-token",
            "connections": [
                {"uri": LOCAL, "local": True},
                {"uri": REMOTE, "local": False},
            ],
        }])
    )
    # Local times out (unreachable from here); remote answers.
    respx_mock.get(f"{LOCAL}/identity").mock(side_effect=httpx.ConnectTimeout("unreachable"))
    respx_mock.get(f"{REMOTE}/identity").mock(return_value=httpx.Response(200))
    url = await discover_server("tok", "cid")
    assert url == REMOTE


async def test_discover_owned_all_unreachable_still_saves_a_url(respx_mock):
    """If no connection answers, discovery must still save a URL (the static
    best pick) rather than dropping the server — it may become reachable later."""
    import httpx
    LOCAL = "http://192.168.1.10:32400"
    respx_mock.get("https://plex.tv/api/v2/resources").mock(
        return_value=httpx.Response(200, json=[{
            "provides": "server", "clientIdentifier": "abc123", "name": "My Server",
            "owned": True, "accessToken": "srv-token",
            "connections": [{"uri": LOCAL, "local": True}],
        }])
    )
    respx_mock.get(f"{LOCAL}/identity").mock(side_effect=httpx.ConnectError("down"))
    url = await discover_server("tok", "cid")
    assert url == LOCAL  # fallback to the static best pick; server not dropped


# ── client: libraries ─────────────────────────────────────────────────────────

async def test_get_libraries_filters_music_only(respx_mock):
    import httpx
    respx_mock.get("http://plex.local:32400/library/sections").mock(
        return_value=httpx.Response(200, json=_sections_response(
            {"key": "1", "title": "Music", "type": "artist"},
            {"key": "2", "title": "Movies", "type": "movie"},
            {"key": "3", "title": "More Music", "type": "artist"},
        ))
    )
    client = make_client()
    libs = await client.get_libraries()
    assert len(libs) == 2
    assert all(isinstance(lib, Library) for lib in libs)
    assert all(lib.type == "artist" for lib in libs)


async def test_get_libraries_cached_on_second_call(respx_mock):
    import httpx
    route = respx_mock.get("http://plex.local:32400/library/sections").mock(
        return_value=httpx.Response(200, json=_sections_response(
            {"key": "1", "title": "Music", "type": "artist"},
        ))
    )
    client = make_client()
    await client.get_libraries()
    await client.get_libraries()
    assert route.call_count == 1  # second call hits cache


# ── client: stream URL ────────────────────────────────────────────────────────

def test_stream_url_constructed_correctly():
    client = make_client(server_url="http://plex.local:32400", token="mytoken")
    url = client.stream_url("/library/parts/123/file.flac")
    assert url == "http://plex.local:32400/library/parts/123/file.flac?X-Plex-Token=mytoken"


def test_stream_url_strips_trailing_slash():
    client = make_client(server_url="http://plex.local:32400/", token="tok")
    url = client.stream_url("/library/parts/1/f.mp3")
    assert "//library" not in url


# ── client: artists ───────────────────────────────────────────────────────────

async def test_get_artists_returns_artist_objects(respx_mock):
    import httpx
    respx_mock.get("http://plex.local:32400/library/sections/1/all").mock(
        return_value=httpx.Response(200, json=_metadata_response(
            {"ratingKey": "10", "title": "The Beatles", "thumb": "/art/beatles"},
            {"ratingKey": "11", "title": "Led Zeppelin", "thumb": None},
        ))
    )
    client = make_client()
    artists = await client.get_artists("1")
    assert len(artists) == 2
    assert isinstance(artists[0], Artist)
    assert artists[0].id == "10"
    assert artists[0].title == "The Beatles"


# ── client: albums ────────────────────────────────────────────────────────────

async def test_get_albums_returns_album_objects(respx_mock):
    import httpx
    respx_mock.get("http://plex.local:32400/library/sections/1/all").mock(
        return_value=httpx.Response(200, json=_metadata_response(
            {"ratingKey": "20", "title": "Abbey Road", "parentTitle": "The Beatles", "year": 1969, "thumb": "/t"},
        ))
    )
    client = make_client()
    albums = await client.get_albums("1")
    assert len(albums) == 1
    assert isinstance(albums[0], Album)
    assert albums[0].year == 1969


async def test_get_albums_for_artist_returns_all_release_types(respx_mock):
    """section-all with parentRatingKey returns all release types; client-side filter applied."""
    import httpx

    route = respx_mock.get("http://plex.local:32400/library/sections/1/all").mock(
        return_value=httpx.Response(200, json={"MediaContainer": {"Metadata": [
            # main album
            {"ratingKey": "20", "title": "The Downward Spiral", "parentTitle": "NIN",
             "parentRatingKey": "10", "year": 1994, "subtype": None},
            # EP
            {"ratingKey": "21", "title": "Broken", "parentTitle": "NIN",
             "parentRatingKey": "10", "year": 1992, "subtype": "ep"},
            # single
            {"ratingKey": "22", "title": "Closer", "parentTitle": "NIN",
             "parentRatingKey": "10", "year": 1994, "subtype": "single"},
            # live
            {"ratingKey": "23", "title": "And All That Could Have Been", "parentTitle": "NIN",
             "parentRatingKey": "10", "year": 2002, "subtype": "live"},
            # album from a different artist — should be excluded by client-side filter
            {"ratingKey": "99", "title": "Other Album", "parentTitle": "Other",
             "parentRatingKey": "99", "year": 2000, "subtype": None},
        ]}})
    )
    client = make_client()
    albums = await client.get_albums("1", artist_id="10")
    assert len(albums) == 4
    subtypes = {a.subtype for a in albums}
    assert None in subtypes
    assert "single" in subtypes
    assert "ep" in subtypes
    assert "live" in subtypes
    assert route.called


# ── client: albums added_at (Recently Added plan U1) ─────────────────────────

async def test_get_albums_captures_added_at_section_path(respx_mock):
    """U1: get_albums lifts Plex addedAt (epoch seconds) onto Album.added_at on
    the section path — the one the browse-index crawl uses."""
    import httpx
    respx_mock.get("http://plex.local:32400/library/sections/1/all").mock(
        return_value=httpx.Response(200, json=_metadata_response(
            {"ratingKey": "20", "title": "Abbey Road", "parentTitle": "The Beatles",
             "year": 1969, "addedAt": 1700000000},
        ))
    )
    albums = await make_client().get_albums("1")
    assert albums[0].added_at == 1700000000


async def test_get_albums_captures_added_at_artist_path(respx_mock):
    """U1: the by-artist (drill-in fallback) path also carries added_at."""
    import httpx
    respx_mock.get("http://plex.local:32400/library/sections/1/all").mock(
        return_value=httpx.Response(200, json={"MediaContainer": {"Metadata": [
            {"ratingKey": "20", "title": "X", "parentTitle": "Y",
             "parentRatingKey": "42", "year": 2020, "addedAt": 1650000000},
        ]}})
    )
    albums = await make_client().get_albums("1", artist_id="42")
    assert albums[0].added_at == 1650000000


async def test_get_albums_added_at_absent_is_none(respx_mock):
    """U1: a payload missing addedAt degrades to None, never a crash."""
    import httpx
    respx_mock.get("http://plex.local:32400/library/sections/1/all").mock(
        return_value=httpx.Response(200, json=_metadata_response(
            {"ratingKey": "20", "title": "Abbey Road", "parentTitle": "The Beatles"},
        ))
    )
    albums = await make_client().get_albums("1")
    assert albums[0].added_at is None


# ── client: albums track_count (same-title release plan U1) ──────────────────

async def test_get_albums_captures_track_count_section_path(respx_mock):
    """U1: get_albums lifts Plex leafCount (total tracks) onto Album.track_count
    on the section path — the cheap content signal the grouping/fold layer uses."""
    import httpx
    respx_mock.get("http://plex.local:32400/library/sections/1/all").mock(
        return_value=httpx.Response(200, json=_metadata_response(
            {"ratingKey": "20", "title": "Loveless", "parentTitle": "My Bloody Valentine",
             "year": 1991, "leafCount": 11},
        ))
    )
    albums = await make_client().get_albums("1")
    assert albums[0].track_count == 11


async def test_get_albums_captures_track_count_artist_path(respx_mock):
    """U1: the by-artist (drill-in fallback) path also carries track_count."""
    import httpx
    respx_mock.get("http://plex.local:32400/library/sections/1/all").mock(
        return_value=httpx.Response(200, json={"MediaContainer": {"Metadata": [
            {"ratingKey": "20", "title": "X", "parentTitle": "Y",
             "parentRatingKey": "42", "leafCount": 7},
        ]}})
    )
    albums = await make_client().get_albums("1", artist_id="42")
    assert albums[0].track_count == 7


async def test_get_albums_track_count_childcount_fallback(respx_mock):
    """U1: when leafCount is absent, childCount stands in as the track count."""
    import httpx
    respx_mock.get("http://plex.local:32400/library/sections/1/all").mock(
        return_value=httpx.Response(200, json=_metadata_response(
            {"ratingKey": "20", "title": "Loveless", "parentTitle": "My Bloody Valentine",
             "childCount": 11},
        ))
    )
    albums = await make_client().get_albums("1")
    assert albums[0].track_count == 11


async def test_get_albums_track_count_absent_is_none(respx_mock):
    """U1: a payload with neither leafCount nor childCount degrades to None."""
    import httpx
    respx_mock.get("http://plex.local:32400/library/sections/1/all").mock(
        return_value=httpx.Response(200, json=_metadata_response(
            {"ratingKey": "20", "title": "Loveless", "parentTitle": "My Bloody Valentine"},
        ))
    )
    albums = await make_client().get_albums("1")
    assert albums[0].track_count is None


# ── client: tracks / stream key ───────────────────────────────────────────────

def _track_item(rating_key="30", title="Come Together", part_key="/library/parts/30/file.flac",
                grandparent_title="The Beatles", parent_title="Abbey Road"):
    return {
        "ratingKey": rating_key,
        "title": title,
        "grandparentTitle": grandparent_title,
        "parentTitle": parent_title,
        "duration": 259000,
        "Genre": [{"tag": "Rock"}],
        "year": 1969,
        "thumb": "/thumb/30",
        "Media": [{"Part": [{"key": part_key}]}],
    }


async def test_get_tracks_returns_track_objects(respx_mock):
    import httpx
    respx_mock.get("http://plex.local:32400/library/sections/1/all").mock(
        return_value=httpx.Response(200, json=_metadata_response(_track_item()))
    )
    client = make_client()
    tracks = await client.get_tracks("1")
    assert len(tracks) == 1
    assert isinstance(tracks[0], Track)
    assert tracks[0].stream_key == "/library/parts/30/file.flac"
    assert tracks[0].genre == "Rock"


async def test_album_children_sorted_disc_then_track(respx_mock):
    """Multi-disc reqs R1/R5 (2026-06-11): parentIndex=disc, index=track;
    the album-children branch sorts (disc, track) — identical-tracklist
    2-disc editions must come back D1:1..N then D2:1..N even when the
    upstream payload arrives scrambled. Missing fields default to disc 1
    / track 0 (sort head) without raising."""
    import httpx
    items = []
    # Scrambled: D2-Intro, D1-Believe, D1-Intro, no-fields straggler.
    for rk, title, disc, idx in (
        ("41", "Intro", 2, 1), ("42", "Believe", 1, 2),
        ("43", "Intro", 1, 1), ("44", "Hidden", None, None),
    ):
        it = _track_item(rating_key=rk, title=title)
        it["type"] = "track"
        if disc is not None:
            it["parentIndex"] = disc
        if idx is not None:
            it["index"] = idx
        items.append(it)
    respx_mock.get("http://plex.local:32400/library/metadata/9/children").mock(
        return_value=httpx.Response(200, json=_metadata_response(*items))
    )
    client = make_client()
    tracks = await client.get_tracks("1", album_id="9")
    assert [(t.disc_number, t.track_number, t.title) for t in tracks] == [
        (1, None, "Hidden"),   # missing fields → disc 1, sorts at track-0 head
        (1, 1, "Intro"),
        (1, 2, "Believe"),
        (2, 1, "Intro"),
    ]


async def test_track_per_track_credit_wins_over_release_artist(respx_mock):
    """2026-06-10 plan U1 / AE1, AE6: originalTitle (the per-track credited
    act on compilations) beats grandparentTitle ("Various Artists")."""
    import httpx
    item = _track_item(grandparent_title="Various Artists")
    item["originalTitle"] = "13th Floor Elevators"
    item["parentRatingKey"] = "77"
    respx_mock.get("http://plex.local:32400/library/sections/1/all").mock(
        return_value=httpx.Response(200, json=_metadata_response(item))
    )
    client = make_client()
    tracks = await client.get_tracks("1")
    assert tracks[0].artist == "13th Floor Elevators"
    assert tracks[0].album_artist == "Various Artists"
    assert tracks[0].album_id is not None and "77" in tracks[0].album_id


async def test_track_release_artist_when_no_per_track_credit(respx_mock):
    """AE7: normal album track — no originalTitle — behaves exactly as today."""
    import httpx
    respx_mock.get("http://plex.local:32400/library/sections/1/all").mock(
        return_value=httpx.Response(200, json=_metadata_response(_track_item()))
    )
    client = make_client()
    tracks = await client.get_tracks("1")
    assert tracks[0].artist == "The Beatles"
    assert tracks[0].album_artist == "The Beatles"


async def test_track_empty_per_track_credit_falls_back(respx_mock):
    """Value-aware fallback: empty-string originalTitle must not blank the
    artist (dict-key fallback would)."""
    import httpx
    item = _track_item()
    item["originalTitle"] = ""
    respx_mock.get("http://plex.local:32400/library/sections/1/all").mock(
        return_value=httpx.Response(200, json=_metadata_response(item))
    )
    client = make_client()
    tracks = await client.get_tracks("1")
    assert tracks[0].artist == "The Beatles"


async def test_track_no_artist_fields_yields_empty(respx_mock):
    import httpx
    item = _track_item()
    del item["grandparentTitle"]
    respx_mock.get("http://plex.local:32400/library/sections/1/all").mock(
        return_value=httpx.Response(200, json=_metadata_response(item))
    )
    client = make_client()
    tracks = await client.get_tracks("1")
    assert tracks[0].artist == ""
    assert tracks[0].album_id is None  # no parentRatingKey in fixture


async def test_get_track_by_id(respx_mock):
    import httpx
    respx_mock.get("http://plex.local:32400/library/metadata/30").mock(
        return_value=httpx.Response(200, json=_metadata_response(_track_item()))
    )
    client = make_client()
    track = await client.get_track("30")
    assert track.title == "Come Together"


async def test_get_track_missing_raises(respx_mock):
    import httpx
    respx_mock.get("http://plex.local:32400/library/metadata/99").mock(
        return_value=httpx.Response(200, json={"MediaContainer": {"Metadata": []}})
    )
    client = make_client()
    with pytest.raises(KeyError):
        await client.get_track("99")


# ── client: get_album single-fetch ────────────────────────────────────────────

async def test_get_album_reads_parentTitle_for_artist(respx_mock):
    """The album metadata endpoint exposes the album's artist as parentTitle
    (not grandparentTitle — that's the track-level convention). Confirms the
    parser doesn't accidentally read the track-shaped field."""
    import httpx
    respx_mock.get("http://plex.local:32400/library/metadata/20").mock(
        return_value=httpx.Response(200, json=_metadata_response({
            "ratingKey": "20",
            "title": "Abbey Road",
            "parentTitle": "The Beatles",
            "grandparentTitle": "should-not-be-used-for-album-artist",
            "year": 1969,
            "thumb": "/thumb/20",
            "subtype": None,
        }))
    )
    album = await make_client().get_album("20")
    assert isinstance(album, Album)
    assert album.title == "Abbey Road"
    assert album.artist == "The Beatles"
    assert album.year == 1969


async def test_get_album_missing_raises(respx_mock):
    import httpx
    respx_mock.get("http://plex.local:32400/library/metadata/99").mock(
        return_value=httpx.Response(200, json={"MediaContainer": {"Metadata": []}})
    )
    with pytest.raises(KeyError):
        await make_client().get_album("99")


async def test_get_album_strips_cross_server_compound_id(respx_mock):
    """Cross-server album_id (machineB:42 from a machineA client) is reduced
    to bare ratingKey 42 before hitting Plex (via _bare_rating_key)."""
    import httpx
    route = respx_mock.get("http://plex.local:32400/library/metadata/42").mock(
        return_value=httpx.Response(200, json=_metadata_response({
            "ratingKey": "42",
            "title": "Album",
            "parentTitle": "Artist",
        }))
    )
    client = make_client()
    client.machine_id = "machineA"
    album = await client.get_album("machineB:42")
    assert album.title == "Album"
    assert route.called


# ── client: genres / years ────────────────────────────────────────────────────

async def test_get_genres(respx_mock):
    import httpx
    respx_mock.get("http://plex.local:32400/library/sections/1/genre").mock(
        return_value=httpx.Response(200, json=_dir_response(
            {"title": "Rock"}, {"title": "Jazz"}
        ))
    )
    genres = await make_client().get_genres("1")
    assert genres == ["Rock", "Jazz"]


async def test_get_years(respx_mock):
    import httpx
    respx_mock.get("http://plex.local:32400/library/sections/1/year").mock(
        return_value=httpx.Response(200, json=_dir_response(
            {"title": "1969"}, {"title": "1971"}, {"title": "unknown"}
        ))
    )
    years = await make_client().get_years("1")
    assert years == [1969, 1971]


# ── client: styles ────────────────────────────────────────────────────────────

def _container_response(total_size: int) -> dict:
    return {"MediaContainer": {"totalSize": total_size, "size": 0, "offset": 0}}


async def test_get_styles_with_counts_sorted_zeros_excluded(respx_mock):
    import httpx
    import respx
    respx_mock.get("http://plex.local:32400/library/sections/1/style").mock(
        return_value=httpx.Response(200, json=_dir_response(
            {"key": "10", "title": "Indie Rock"},
            {"key": "20", "title": "Classical"},
            {"key": "30", "title": "Synth-pop"},
        ))
    )
    # Indie Rock: 50, Classical: 0, Synth-pop: 20
    def count_side_effect(request):
        params = dict(request.url.params)
        counts = {"10": 50, "20": 0, "30": 20}
        return httpx.Response(200, json=_container_response(counts.get(params.get("style", ""), 0)))

    respx_mock.get("http://plex.local:32400/library/sections/1/all").mock(side_effect=count_side_effect)

    result = await make_client().get_styles_with_counts("1")
    assert result == [{"name": "Indie Rock", "count": 50}, {"name": "Synth-pop", "count": 20}]


async def test_get_styles_with_counts_all_zero_returns_empty(respx_mock):
    import httpx
    respx_mock.get("http://plex.local:32400/library/sections/1/style").mock(
        return_value=httpx.Response(200, json=_dir_response({"key": "10", "title": "Jazz"}))
    )
    respx_mock.get("http://plex.local:32400/library/sections/1/all").mock(
        return_value=httpx.Response(200, json=_container_response(0))
    )
    result = await make_client().get_styles_with_counts("1")
    assert result == []


async def test_get_styles_with_counts_cached_on_second_call(respx_mock):
    import httpx
    style_route = respx_mock.get("http://plex.local:32400/library/sections/1/style").mock(
        return_value=httpx.Response(200, json=_dir_response({"key": "10", "title": "Rock"}))
    )
    respx_mock.get("http://plex.local:32400/library/sections/1/all").mock(
        return_value=httpx.Response(200, json=_container_response(5))
    )
    client = make_client()
    await client.get_styles_with_counts("1")
    await client.get_styles_with_counts("1")
    assert style_route.call_count == 1  # second call hits cache


async def test_get_styles_with_counts_count_error_treated_as_zero(respx_mock):
    import httpx
    respx_mock.get("http://plex.local:32400/library/sections/1/style").mock(
        return_value=httpx.Response(200, json=_dir_response(
            {"key": "10", "title": "Good"}, {"key": "20", "title": "Bad"},
        ))
    )
    def flaky(request):
        params = dict(request.url.params)
        if params.get("style") == "20":
            raise httpx.ConnectError("timeout")
        return httpx.Response(200, json=_container_response(10))

    respx_mock.get("http://plex.local:32400/library/sections/1/all").mock(side_effect=flaky)
    result = await make_client().get_styles_with_counts("1")
    assert result == [{"name": "Good", "count": 10}]


async def test_get_albums_with_style_passes_style_param(respx_mock):
    import httpx
    route = respx_mock.get("http://plex.local:32400/library/sections/1/all").mock(
        return_value=httpx.Response(200, json=_metadata_response(
            {"ratingKey": "99", "title": "OK Computer", "parentTitle": "Radiohead", "year": 1997},
        ))
    )
    client = make_client()
    albums = await client.get_albums("1", style="Indie Rock")
    assert len(albums) == 1
    assert albums[0].title == "OK Computer"
    assert route.calls[0].request.url.params["style"] == "Indie Rock"


# ── client: search (hub search; 2026-06-10 hub-search plan) ──────────────────
# Fixture shapes mirror live PMS captures recorded in
# docs/plans/2026-06-10-005-fix-plex-hub-search-switch-plan.md: hubs carry
# type/size/Metadata; music items carry librarySectionID (int).


def _hub(hub_type, *items, size=None):
    hub = {"type": hub_type, "size": len(items) if size is None else size}
    if items:
        hub["Metadata"] = list(items)
    return hub


def _hub_response(*hubs):
    return {"MediaContainer": {"Hub": list(hubs)}}


def _in_section(item, section_id=1):
    return {**item, "librarySectionID": section_id}


def _artist_item(rating_key="70", title="Motörhead"):
    return {"ratingKey": rating_key, "title": title, "thumb": f"/thumb/{rating_key}"}


def _album_item(rating_key="80", title="Ace of Spades", parent_title="Motörhead"):
    return {"ratingKey": rating_key, "title": title, "parentTitle": parent_title,
            "year": 1980, "thumb": f"/thumb/{rating_key}"}


async def test_search_hub_returns_grouped_results(respx_mock):
    """R1: an ASCII query is sent verbatim ONCE; hub search owns the
    diacritic folding (live-verified) and the accented display titles
    survive parsing."""
    import httpx
    route = respx_mock.get("http://plex.local:32400/hubs/search").mock(
        return_value=httpx.Response(200, json=_hub_response(
            _hub("artist", _in_section(_artist_item())),
            _hub("album", _in_section(_album_item())),
            _hub("track", _in_section(_track_item(
                title="Ace of Spades", grandparent_title="Motörhead",
                parent_title="Ace of Spades"))),
        ))
    )
    results = await make_client().search("1", "Motorhead")
    assert [a.title for a in results.artists] == ["Motörhead"]
    assert [a.title for a in results.albums] == ["Ace of Spades"]
    assert [t.title for t in results.tracks] == ["Ace of Spades"]
    # ONE hub call replaces the legacy 3-calls-per-query (and per-token) fan-out
    assert route.call_count == 1


async def test_search_hub_request_carries_limit_and_query(respx_mock):
    import httpx
    route = respx_mock.get("http://plex.local:32400/hubs/search").mock(
        return_value=httpx.Response(200, json=_hub_response())
    )
    await make_client().search("1", "Motorhead")
    params = dict(route.calls[0].request.url.params)
    assert params["limit"] == "30"
    assert params["query"] == "motorhead"  # _normalize_text lowers/folds typographics


async def test_search_track_fields_complete_for_queueing(respx_mock):
    """R2: hub track items carry Media/Part, duration, parentRatingKey —
    the queue path needs stream_key/duration_ms/album_id intact."""
    import httpx
    item = _in_section({**_track_item(), "parentRatingKey": "93574"})
    respx_mock.get("http://plex.local:32400/hubs/search").mock(
        return_value=httpx.Response(200, json=_hub_response(_hub("track", item)))
    )
    results = await make_client().search("1", "come together")
    t = results.tracks[0]
    assert t.stream_key == "/library/parts/30/file.flac"
    assert t.duration_ms == 259000
    assert t.album_id == "93574"
    assert t.thumb == "/thumb/30"


# ── Tier 2: literal per-section title search (search_titles) ──────────────────

async def test_search_titles_surfaces_non_leading_substring_track(respx_mock):
    """Tier 2: the literal per-section endpoint returns a title-substring match
    owned by an unrelated artist — the exact case hub search (Tier 1) omits."""
    import httpx
    item = _track_item(rating_key="501", title="All Sparks (Cicada Remix)",
                       grandparent_title="Editors", parent_title="The Back Room")
    respx_mock.get("http://plex.local:32400/library/sections/1/search").mock(
        return_value=httpx.Response(200, json=_metadata_response(item))
    )
    results = await make_client().search_titles("1", "Cicada", types=("track",))
    assert [t.title for t in results.tracks] == ["All Sparks (Cicada Remix)"]
    assert results.tracks[0].artist == "Editors"
    assert results.albums == []


async def test_search_titles_request_carries_type_query_and_paging(respx_mock):
    import httpx
    route = respx_mock.get("http://plex.local:32400/library/sections/1/search").mock(
        return_value=httpx.Response(200, json=_metadata_response())
    )
    await make_client().search_titles("1", "Cicada", types=("track",), start=30, size=30)
    params = dict(route.calls[0].request.url.params)
    assert params["type"] == "10"                 # Plex libtype 10 = track
    assert params["query"] == "cicada"            # _normalize_text lowercases
    assert params["X-Plex-Container-Start"] == "30"
    assert params["X-Plex-Container-Size"] == "30"


async def test_search_titles_albums_use_album_libtype(respx_mock):
    import httpx
    route = respx_mock.get("http://plex.local:32400/library/sections/1/search").mock(
        return_value=httpx.Response(200, json=_metadata_response(
            _album_item(rating_key="88", title="Cicada EP", parent_title="Cicada")))
    )
    results = await make_client().search_titles("1", "Cicada", types=("album",))
    assert [a.title for a in results.albums] == ["Cicada EP"]
    assert results.tracks == []
    assert dict(route.calls[0].request.url.params)["type"] == "9"  # Plex libtype 9 = album


async def test_search_filters_to_requested_section_and_music_hubs(respx_mock):
    """R3: hub search is server-wide and server-side scoping params leak
    (live finding 2) — items outside the requested section are dropped and
    non-music hubs are ignored entirely."""
    import httpx
    respx_mock.get("http://plex.local:32400/hubs/search").mock(
        return_value=httpx.Response(200, json=_hub_response(
            _hub("artist",
                 _in_section(_artist_item(rating_key="70"), section_id=3),
                 _in_section(_artist_item(rating_key="71", title="Other Lib Act"), section_id=1)),
            _hub("movie", _in_section({"ratingKey": "99", "title": "Zappa"}, section_id=3)),
        ))
    )
    results = await make_client().search("3", "Motorhead")
    assert [a.title for a in results.artists] == ["Motörhead"]
    assert results.tracks == [] and results.albums == []


async def test_search_item_without_library_section_excluded(respx_mock):
    """An item that cannot be proven to belong to the enabled library is
    dropped rather than leaked across libraries."""
    import httpx
    respx_mock.get("http://plex.local:32400/hubs/search").mock(
        return_value=httpx.Response(200, json=_hub_response(
            _hub("artist", _artist_item()),  # no librarySectionID
        ))
    )
    results = await make_client().search("1", "Motorhead")
    assert results.artists == []


async def test_search_compilation_credit_wins_in_hub_results(respx_mock):
    """R5: originalTitle (credited act) beats grandparentTitle ("Various
    Artists") for tracks surfaced via hub search too."""
    import httpx
    item = _in_section({**_track_item(grandparent_title="Various Artists"),
                        "originalTitle": "13th Floor Elevators"})
    respx_mock.get("http://plex.local:32400/hubs/search").mock(
        return_value=httpx.Response(200, json=_hub_response(_hub("track", item)))
    )
    results = await make_client().search("1", "elevators")
    assert results.tracks[0].artist == "13th Floor Elevators"
    assert results.tracks[0].album_artist == "Various Artists"


async def test_search_multiword_is_single_call_with_native_matching(respx_mock):
    """R6 (replaces the legacy per-token stitching tests): hub search
    handles cross-field partials natively (live-verified: "Prince Alp" →
    "Alphabet St."). Duplicate editions with distinct ratingKeys all come
    back — downstream _by_id/dedup own collapsing."""
    import httpx
    route = respx_mock.get("http://plex.local:32400/hubs/search").mock(
        return_value=httpx.Response(200, json=_hub_response(
            _hub("track",
                 _in_section(_track_item(rating_key="55", title="Alphabet St.",
                                         grandparent_title="Prince", parent_title="Lovesexy")),
                 _in_section(_track_item(rating_key="56", title="Alphabet St.",
                                         grandparent_title="Prince", parent_title="Hits"))),
        ))
    )
    results = await make_client().search("1", "Prince Alp")
    assert [t.title for t in results.tracks] == ["Alphabet St.", "Alphabet St."]
    assert results.tracks[0].artist == "Prince"
    assert route.call_count == 1  # no per-token fan-out


async def test_search_no_hub_key_returns_empty(respx_mock):
    import httpx
    respx_mock.get("http://plex.local:32400/hubs/search").mock(
        return_value=httpx.Response(200, json={"MediaContainer": {}})
    )
    results = await make_client().search("1", "anything")
    assert results.tracks == [] and results.albums == [] and results.artists == []


async def test_search_size_zero_hub_skipped(respx_mock):
    import httpx
    respx_mock.get("http://plex.local:32400/hubs/search").mock(
        return_value=httpx.Response(200, json=_hub_response(
            _hub("artist", size=0),  # size 0, no Metadata key — live shape
            _hub("track", _in_section(_track_item())),
        ))
    )
    results = await make_client().search("1", "come")
    assert results.artists == []
    assert len(results.tracks) == 1


async def test_search_401_raises_plex_auth_error(respx_mock):
    import httpx
    respx_mock.get("http://plex.local:32400/hubs/search").mock(
        return_value=httpx.Response(401)
    )
    with pytest.raises(PlexAuthError):
        await make_client().search("1", "Motorhead")


# ── client: 401 raises PlexAuthError ─────────────────────────────────────────

async def test_401_raises_plex_auth_error(respx_mock):
    import httpx
    respx_mock.get("http://plex.local:32400/library/sections").mock(
        return_value=httpx.Response(401)
    )
    with pytest.raises(PlexAuthError):
        await make_client().get_libraries()


# ── client: invalidate_cache ──────────────────────────────────────────────────

async def test_invalidate_cache_forces_refetch(respx_mock):
    import httpx
    route = respx_mock.get("http://plex.local:32400/library/sections").mock(
        return_value=httpx.Response(200, json=_sections_response(
            {"key": "1", "title": "Music", "type": "artist"},
        ))
    )
    client = make_client()
    await client.get_libraries()
    client.invalidate_cache()
    await client.get_libraries()
    assert route.call_count == 2


# ── client: artist release counts (2026-06-09 rail plan U5 / R15) ─────────────

async def test_get_artists_maps_child_count(respx_mock):
    import httpx
    respx_mock.get("http://plex.local:32400/library/sections/1/all").mock(
        return_value=httpx.Response(200, json=_metadata_response(
            {"ratingKey": "10", "title": "The Beatles", "childCount": 13},
            {"ratingKey": "11", "title": "One Hit Wonder", "childCount": 1},
        ))
    )
    client = make_client()
    artists = await client.get_artists("1")
    assert artists[0].release_count == 13
    assert artists[1].release_count == 1


async def test_get_artists_child_count_absent_is_none(respx_mock):
    import httpx
    respx_mock.get("http://plex.local:32400/library/sections/1/all").mock(
        return_value=httpx.Response(200, json=_metadata_response(
            {"ratingKey": "10", "title": "Mystery Artist"},
        ))
    )
    client = make_client()
    artists = await client.get_artists("1")
    assert artists[0].release_count is None


async def test_get_artists_child_count_malformed_is_none(respx_mock):
    """Malformed or non-positive childCount degrades to no-count, never a crash
    or a '0 releases' display."""
    import httpx
    respx_mock.get("http://plex.local:32400/library/sections/1/all").mock(
        return_value=httpx.Response(200, json=_metadata_response(
            {"ratingKey": "10", "title": "Weird Server", "childCount": "not-a-number"},
            {"ratingKey": "11", "title": "Zero Albums", "childCount": 0},
        ))
    )
    client = make_client()
    artists = await client.get_artists("1")
    assert artists[0].release_count is None
    assert artists[1].release_count is None


# ── client: similarity (Surprise Me, 2026-06-17 plan U1) ──────────────────────

async def test_get_sonic_nearest_returns_tracks(respx_mock):
    """Sonic /nearest parses the returned tracks (Covers AE1)."""
    import httpx
    route = respx_mock.get("http://plex.local:32400/library/metadata/30/nearest").mock(
        return_value=httpx.Response(200, json=_metadata_response(
            _track_item(rating_key="31", title="Weird Fishes"),
            _track_item(rating_key="32", title="Reckoner"),
        ))
    )
    tracks = await make_client().get_sonic_nearest("30")
    assert [t.title for t in tracks] == ["Weird Fishes", "Reckoner"]
    assert all(isinstance(t, Track) for t in tracks)
    # request carried the similarity params
    params = dict(route.calls[0].request.url.params)
    assert params["limit"] == "10"
    assert params["maxDistance"] == "0.35"


async def test_get_sonic_nearest_empty_when_no_sonic_data(respx_mock):
    """No sonic analysis on the server → empty MediaContainer → [] (Covers AE2)."""
    import httpx
    respx_mock.get("http://plex.local:32400/library/metadata/30/nearest").mock(
        return_value=httpx.Response(200, json={"MediaContainer": {}})
    )
    assert await make_client().get_sonic_nearest("30") == []


async def test_get_sonic_nearest_fails_safe_on_error(respx_mock):
    """A server error must not raise — the chain degrades to the next source."""
    import httpx
    respx_mock.get("http://plex.local:32400/library/metadata/30/nearest").mock(
        return_value=httpx.Response(500)
    )
    assert await make_client().get_sonic_nearest("30") == []


async def test_get_sonic_nearest_strips_cross_server_compound_id(respx_mock):
    """Cross-server track id (machineB:30) is reduced to a bare ratingKey."""
    import httpx
    route = respx_mock.get("http://plex.local:32400/library/metadata/30/nearest").mock(
        return_value=httpx.Response(200, json=_metadata_response(_track_item()))
    )
    client = make_client()
    client.machine_id = "machineA"
    tracks = await client.get_sonic_nearest("machineB:30")
    assert len(tracks) == 1
    assert route.called


async def test_get_artist_similar_names_returns_tag_names(respx_mock):
    """track → grandparentRatingKey → artist <Similar> tags → names."""
    import httpx
    respx_mock.get("http://plex.local:32400/library/metadata/30").mock(
        return_value=httpx.Response(200, json=_metadata_response(
            {"ratingKey": "30", "title": "Idioteque", "grandparentRatingKey": "10"}
        ))
    )
    respx_mock.get("http://plex.local:32400/library/metadata/10").mock(
        return_value=httpx.Response(200, json=_metadata_response(
            {"ratingKey": "10", "title": "Radiohead",
             "Similar": [{"tag": "Muse"}, {"tag": "Thom Yorke"}, {"id": 9}]}
        ))
    )
    names = await make_client().get_artist_similar_names("30")
    assert names == ["Muse", "Thom Yorke"]  # the tagless entry is skipped


async def test_get_artist_similar_names_no_artist_key_returns_empty(respx_mock):
    """A track with no grandparentRatingKey yields [] without a second call."""
    import httpx
    respx_mock.get("http://plex.local:32400/library/metadata/30").mock(
        return_value=httpx.Response(200, json=_metadata_response(
            {"ratingKey": "30", "title": "Orphan Track"}
        ))
    )
    assert await make_client().get_artist_similar_names("30") == []


async def test_get_artist_similar_names_no_similar_tags_returns_empty(respx_mock):
    """An artist with no <Similar> tags yields []."""
    import httpx
    respx_mock.get("http://plex.local:32400/library/metadata/30").mock(
        return_value=httpx.Response(200, json=_metadata_response(
            {"ratingKey": "30", "title": "T", "grandparentRatingKey": "10"}
        ))
    )
    respx_mock.get("http://plex.local:32400/library/metadata/10").mock(
        return_value=httpx.Response(200, json=_metadata_response(
            {"ratingKey": "10", "title": "Obscure Act"}
        ))
    )
    assert await make_client().get_artist_similar_names("30") == []


async def test_get_artist_similar_names_fails_safe_on_error(respx_mock):
    import httpx
    respx_mock.get("http://plex.local:32400/library/metadata/30").mock(
        return_value=httpx.Response(500)
    )
    assert await make_client().get_artist_similar_names("30") == []


# ── get_artist_popular_tracks (All Songs plan U1) ───────────────────────────────
# includePopularLeaves surfaces the artist's online-metadata popular tracks. The
# exact leaf location varies by PMS version, so the extractor tolerates a Hub of
# tracks AND a popularLeaves container; callers match by title (online keyspace
# differs from local ids), so each entry carries its title + optional rating key.

async def test_get_artist_popular_tracks_returns_ranked_leaves(respx_mock):
    """Hub-of-tracks shape → ordered {title, rating_key} entries (rank = order)."""
    import httpx
    respx_mock.get("http://plex.local:32400/library/metadata/10").mock(
        return_value=httpx.Response(200, json=_metadata_response({
            "ratingKey": "10", "title": "Radiohead", "type": "artist",
            "Hub": [{"type": "track", "Metadata": [
                {"ratingKey": "501", "title": "Creep", "type": "track"},
                {"ratingKey": "502", "title": "Karma Police", "type": "track"},
            ]}],
        }))
    )
    assert await make_client().get_artist_popular_tracks("10") == [
        {"title": "Creep", "rating_key": "501"},
        {"title": "Karma Police", "rating_key": "502"},
    ]


async def test_get_artist_popular_tracks_popularleaves_container_shape(respx_mock):
    """popularLeaves-container shape; a leaf without a rating key still yields a title entry."""
    import httpx
    respx_mock.get("http://plex.local:32400/library/metadata/10").mock(
        return_value=httpx.Response(200, json=_metadata_response({
            "ratingKey": "10", "title": "Radiohead",
            "popularLeaves": {"Metadata": [{"title": "No Surprises"}]},
        }))
    )
    assert await make_client().get_artist_popular_tracks("10") == [
        {"title": "No Surprises", "rating_key": None},
    ]


async def test_get_artist_popular_tracks_none_when_unmatched(respx_mock):
    """An artist with no popular leaves (unmatched) → []."""
    import httpx
    respx_mock.get("http://plex.local:32400/library/metadata/10").mock(
        return_value=httpx.Response(200, json=_metadata_response(
            {"ratingKey": "10", "title": "Obscure Act"}))
    )
    assert await make_client().get_artist_popular_tracks("10") == []


async def test_get_artist_popular_tracks_fails_safe_on_error(respx_mock):
    import httpx
    respx_mock.get("http://plex.local:32400/library/metadata/10").mock(
        return_value=httpx.Response(500)
    )
    assert await make_client().get_artist_popular_tracks("10") == []
