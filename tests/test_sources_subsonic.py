"""U2: SubsonicSource over the OpenSubsonic / Subsonic REST API.

Tested against a fake Subsonic REST surface via ``httpx.MockTransport`` so the
real request-building (API-Key auth params, endpoint paths, id namespacing) is
exercised — not just parsing. Mirrors ``tests/test_sources_jellyfin.py``.

Covers the plan's U2 scenarios:
- browse artists→albums→tracks parse (songCount→track_count, duration→ms,
  {source_id}: namespaced ids);
- a second server-shaped (gonic-style) fixture parses with the SAME adapter;
- ``resolve_stream`` output carries NO api key/token/salt in the device-facing
  URL and the source flags ``url_borne_auth = True`` (AE4);
- capabilities advertise native_search + genres, enrichments empty (AE9/AE5);
- MBID present→entity-scoped match_ids (recording on the track), absent→empty;
- an unsafe-char server id is hashed to an ID-safe opaque id;
- empty-search quirk handled;
- a no-API-Key-extension server surfaces a clear error;
- getCoverArt→fetch_art bytes+content-type;
- a fetch_art/resolve_stream failure never emits the api key into logs.
"""

import hashlib
import re

import httpx
import pytest

from app.sources import subsonic as sub
from app.sources.base import Capabilities, StreamTarget
from app.sources.subsonic import SubsonicSource, SubsonicAuthError

SERVER = "http://navidrome.local:4533"
USER = "party"
API_KEY = "key-abc-secret"
PASSWORD = "s3cr3t-pw"  # token+salt mode account password (never transmitted)

# The registry accepts ids matching this shape after the {source_id}: prefix.
_ID_AFTER_PREFIX = re.compile(r"^[A-Za-z0-9_-]+(?::[A-Za-z0-9_-]+)?$")


def _sr(payload):
    """Wrap a body in a Subsonic ``{"subsonic-response": {...}}`` envelope."""
    return httpx.Response(
        200, json={"subsonic-response": {"status": "ok", "version": "1.16.1", **payload}}
    )


def _sr_failed(code, message):
    """A Subsonic ``failed`` envelope (HTTP 200, error code) — code 40 = wrong
    creds, which the adapter maps to SubsonicAuthError."""
    return httpx.Response(200, json={"subsonic-response": {
        "status": "failed", "version": "1.16.1",
        "error": {"code": code, "message": message}}})


class FakeSubsonic:
    """A minimal in-memory Subsonic REST surface for httpx.MockTransport.

    ``id_char`` lets a test inject an unsafe-char id (default clean); ``server``
    lets a second (gonic-style) fixture reshape response fields.
    """

    def __init__(self, *, has_api_key_ext=True, unsafe_ids=False,
                 known_apikey=None, known_password=None):
        self.has_api_key_ext = has_api_key_ext
        self.unsafe_ids = unsafe_ids
        # When a known credential is set, the fake ENFORCES it on every
        # authenticated request (wrong secret → failed code 40), instead of
        # accepting anything. This is the fix for the original ship's blind spot:
        # a credential-blind mock passes CI while a real wrong secret is rejected.
        # apiKey mode checks apiKey==known_apikey; token mode recomputes
        # md5(known_password + s) and compares t, proving the adapter's token
        # derivation is wire-correct. Both None → accept (back-compat).
        self.known_apikey = known_apikey
        self.known_password = known_password
        self.last_url = None  # full URL of the most recent request (for leak checks)

        aid = "ar:1" if unsafe_ids else "ar1"
        alid = "al:1" if unsafe_ids else "al1"
        tid = "tr:1" if unsafe_ids else "tr1"
        self._aid, self._alid, self._tid = aid, alid, tid

        self.artists = [
            {"id": aid, "name": "Artist One", "coverArt": aid, "albumCount": 2,
             "musicBrainzId": "mba-1"},
        ]
        self.albums = [
            {"id": alid, "name": "Album One", "artist": "Artist One",
             "artistId": aid, "year": 2001, "songCount": 2, "coverArt": alid,
             "duration": 500, "musicBrainzId": "mbal-1"},
        ]
        self.songs = [
            {"id": tid, "parent": alid, "title": "Song One", "album": "Album One",
             "artist": "Artist One", "albumId": alid, "artistId": aid,
             "duration": 180, "track": 1, "discNumber": 1, "year": 2001,
             "genre": "Rock", "coverArt": tid,
             # carries only the recording MBID on the track — the album mbid must
             # NOT ride the track entity (false-merge guard).
             "musicBrainzId": "mbtrack-1"},
            {"id": (tid + "b"), "parent": alid, "title": "Song Two",
             "album": "Album One", "artist": "Artist One", "albumId": alid,
             "artistId": aid, "duration": 200, "track": 2, "discNumber": 1,
             "year": 2001},  # no MBID → empty match_ids
        ]

    def _auth_ok(self, params) -> bool:
        """Enforce the configured credential (if any) — apiKey exact match, or
        token+salt md5 recomputation. No known credential → accept."""
        if self.known_apikey is not None:
            return params.get("apiKey") == self.known_apikey
        if self.known_password is not None:
            s, t = params.get("s"), params.get("t")
            if not s or not t:
                return False
            import hashlib
            return t == hashlib.md5(
                (self.known_password + s).encode("utf-8")).hexdigest()
        return True

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.last_url = str(request.url)
        path = request.url.path
        params = request.url.params
        name = path.rsplit("/", 1)[-1].replace(".view", "")

        if name == "getOpenSubsonicExtensions":
            # The unauthenticated capability endpoint — never credential-gated.
            exts = []
            if self.has_api_key_ext:
                exts = [{"name": "apiKeyAuthentication", "versions": [1]},
                        {"name": "transcodeOffset", "versions": [1]}]
            return _sr({"openSubsonicExtensions": exts})

        # Every other endpoint is authenticated: reject a wrong secret.
        if not self._auth_ok(params):
            return _sr_failed(40, "Wrong username or password.")

        if name == "ping":
            return _sr({})

        if name == "getArtists":
            return _sr({"artists": {"index": [
                {"name": "A", "artist": self.artists},
            ]}})

        if name == "getAlbumList2":
            return _sr({"albumList2": {"album": self.albums}})

        if name == "getAlbum":
            alid = params.get("id")
            alb = next((a for a in self.albums if a["id"] == alid), None)
            if not alb:
                return _sr({"album": {}})
            songs = [s for s in self.songs if s["albumId"] == alid]
            return _sr({"album": {**alb, "song": songs}})

        if name == "getArtist":
            aid = params.get("id")
            art = next((a for a in self.artists if a["id"] == aid), None)
            albs = [a for a in self.albums if a["artistId"] == aid]
            return _sr({"artist": {**(art or {}), "album": albs}})

        if name == "getSong":
            tid = params.get("id")
            song = next((s for s in self.songs if s["id"] == tid), None)
            return _sr({"song": song or {}})

        if name == "getGenres":
            return _sr({"genres": {"genre": [
                {"value": "Rock", "songCount": 10},
                {"value": "Jazz", "songCount": 5},
            ]}})

        if name == "search3":
            q = (params.get("query") or "").lower().strip()
            arts = [a for a in self.artists if q and q in a["name"].lower()]
            albs = [a for a in self.albums if q and q in a["name"].lower()]
            songs = [s for s in self.songs if q and q in s["title"].lower()]
            return _sr({"searchResult3": {
                "artist": arts, "album": albs, "song": songs,
            }})

        if name in ("getCoverArt", "stream"):
            return httpx.Response(200, content=b"BYTES",
                                  headers={"content-type": "image/jpeg"})

        return _sr({})


class GonicFakeSubsonic(FakeSubsonic):
    """A second-server-shaped surface: gonic returns getAlbumList2 albums that
    omit some optional fields Navidrome fills (no coverArt on some rows, integer
    ids as strings), to prove the SAME adapter code parses it unmodified."""

    def __init__(self, **kw):
        super().__init__(**kw)
        # gonic-style: numeric-string ids, no musicBrainzId, minimal fields.
        self.artists = [
            {"id": "10", "name": "Gonic Artist", "albumCount": 1},
        ]
        self.albums = [
            {"id": "20", "name": "Gonic Album", "artist": "Gonic Artist",
             "artistId": "10", "year": 1999, "songCount": 1},
        ]
        self.songs = [
            {"id": "30", "parent": "20", "title": "Gonic Song",
             "album": "Gonic Album", "artist": "Gonic Artist", "albumId": "20",
             "artistId": "10", "duration": 240, "track": 1},
        ]


def _source(fake, **kw):
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return SubsonicSource(server_url=SERVER, api_key=API_KEY, username=USER,
                          source_id="navi", server_name="Navi", http=http, **kw)


def _token_source(fake, **kw):
    """A token+salt-mode source: the secret is the account password."""
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return SubsonicSource(server_url=SERVER, api_key=PASSWORD, username=USER,
                          source_id="navi", server_name="Navi", http=http,
                          auth_mode="token", **kw)


@pytest.fixture
def fake():
    return FakeSubsonic()


# ── identity + capabilities ──────────────────────────────────────────────────

def test_identity_and_capabilities(fake):
    s = _source(fake)
    assert s.source_id == "navi"
    assert s.source_type == "subsonic"
    caps = s.capabilities
    assert isinstance(caps, Capabilities)
    assert caps.native_search and caps.genres
    assert not (caps.similarity or caps.popular or caps.styles)


def test_url_borne_auth_flag(fake):
    # U5 reads this to force-proxy the source; header-auth providers leave it False.
    s = _source(fake)
    assert s.url_borne_auth is True


# ── extension detection / auth ───────────────────────────────────────────────

def test_invalid_auth_mode_rejected_at_construction():
    # A typo'd mode must fail loudly, not silently emit the wrong auth params.
    http = httpx.AsyncClient(transport=httpx.MockTransport(FakeSubsonic().handler))
    with pytest.raises(ValueError):
        SubsonicSource(server_url=SERVER, api_key="k", username=USER, http=http,
                       auth_mode="Token")  # wrong case


def test_invalid_auth_mode_rejected_in_set_auth(fake):
    s = _source(fake)
    with pytest.raises(ValueError):
        s.set_auth("password")  # not a valid mode


async def test_requests_carry_api_key_not_password(fake):
    s = _source(fake)
    await s.get_genres("navi:lib")
    # API-Key auth: apiKey param present, NO password (p) or token/salt (t/s).
    assert "apiKey=key-abc-secret" in fake.last_url
    assert "&p=" not in fake.last_url and "?p=" not in fake.last_url
    assert "&t=" not in fake.last_url and "&s=" not in fake.last_url


# ── token+salt fallback (2026-08-11-003 U1) ──────────────────────────────────

def test_apikey_mode_common_params_unchanged(fake):
    # Regression (R6): apiKey mode emits apiKey and NONE of s/t/p.
    s = _source(fake)
    p = s._common_params()
    assert p["apiKey"] == API_KEY
    assert p["u"] == USER and p["f"] == "json"
    assert "s" not in p and "t" not in p and "p" not in p


def test_token_mode_common_params_shape_and_md5(fake):
    # R2: token mode emits u/s/t with t=md5(password+salt); no apiKey, no p.
    s = _token_source(fake)
    p = s._common_params()
    assert p["u"] == USER and p["f"] == "json"
    assert "apiKey" not in p and "p" not in p
    assert p["s"] and p["t"]
    expected = hashlib.md5((PASSWORD + p["s"]).encode("utf-8"),
                           usedforsecurity=False).hexdigest()
    assert p["t"] == expected


def test_token_mode_fresh_salt_per_call(fake):
    # R2: each request gets a fresh salt (and therefore a fresh token).
    s = _token_source(fake)
    a = s._common_params()
    b = s._common_params()
    assert a["s"] != b["s"]
    assert a["t"] != b["t"]


async def test_token_mode_requests_carry_token_salt_not_password(fake):
    s = _token_source(fake)
    await s.get_genres("navi:lib")
    assert "&t=" in fake.last_url and "&s=" in fake.last_url
    assert f"u={USER}" in fake.last_url
    assert "apiKey=" not in fake.last_url
    assert "&p=" not in fake.last_url and "?p=" not in fake.last_url
    # the cleartext password must never appear anywhere in the URL
    assert PASSWORD not in fake.last_url


def test_url_borne_auth_true_in_token_mode(fake):
    # AE4/R5: force-proxy applies to token+salt too (the URL carries u/t/s).
    assert _token_source(fake).url_borne_auth is True


async def test_resolve_stream_token_mode_no_cleartext_password(fake):
    s = _token_source(fake)
    tgt = s.resolve_stream("navi:tr1")
    assert isinstance(tgt, StreamTarget)
    assert "t=" in tgt.url and "s=" in tgt.url
    assert "apiKey=" not in tgt.url and "&p=" not in tgt.url
    assert PASSWORD not in tgt.url


async def test_set_auth_reconfigures_mode(fake):
    # U4 relies on flipping one source's mode without a second httpx client.
    s = _source(fake)  # starts apikey
    assert "apiKey" in s._common_params()
    s.set_auth("token", secret=PASSWORD)
    p = s._common_params()
    assert "apiKey" not in p and p["s"] and p["t"]
    assert p["t"] == hashlib.md5((PASSWORD + p["s"]).encode("utf-8"),
                                 usedforsecurity=False).hexdigest()


async def test_probe_extensions_unauth_carries_no_credentials(fake):
    # security-lens/adversarial P1: the detection probe must not transmit the
    # unvalidated secret — only v/c/f on the wire.
    s = _source(fake)
    present = await s._probe_extensions_unauth()
    assert present is True
    assert "apiKey=" not in fake.last_url
    assert f"u={USER}" not in fake.last_url
    assert "&t=" not in fake.last_url and "&s=" not in fake.last_url
    assert "&p=" not in fake.last_url and "?p=" not in fake.last_url


async def test_probe_extensions_unauth_absent_returns_false():
    fake = FakeSubsonic(has_api_key_ext=False)
    s = _source(fake)
    assert await s._probe_extensions_unauth() is False


async def test_probe_extensions_unauth_raises_on_failed_status():
    def handler(request):
        return httpx.Response(200, json={"subsonic-response": {
            "status": "failed", "error": {"code": 0, "message": "nope"}}})
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    s = SubsonicSource(server_url=SERVER, api_key=API_KEY, username=USER,
                       source_id="navi", http=http)
    with pytest.raises(RuntimeError):
        await s._probe_extensions_unauth()


async def test_validate_credentials_ok(fake):
    # ping succeeds → no raise, in both modes.
    await _source(fake).validate_credentials()
    await _token_source(fake).validate_credentials()


async def test_validate_credentials_raises_on_auth_failure():
    # A server that rejects the secret returns a Subsonic failed code 40 →
    # SubsonicAuthError (the real connect-time secret check; get_libraries would
    # not have caught this).
    def handler(request):
        if request.url.path.endswith("ping.view"):
            return httpx.Response(200, json={"subsonic-response": {
                "status": "failed", "error": {"code": 40, "message": "wrong"}}})
        return _sr({})
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    s = SubsonicSource(server_url=SERVER, api_key="badpw", username=USER,
                       source_id="navi", http=http, auth_mode="token")
    with pytest.raises(SubsonicAuthError):
        await s.validate_credentials()


# ── U6: both-mode credential ENFORCEMENT (closes the original mock-blind gap) ──
# The prior FakeSubsonic accepted any credential, so a green suite proved nothing
# about wire-correctness. These use a credential-enforcing fake in BOTH modes.

async def test_apikey_mode_correct_key_accepted_by_enforcing_server():
    fake = FakeSubsonic(known_apikey=API_KEY)
    s = _source(fake)  # apikey mode, api_key == API_KEY
    await s.validate_credentials()                 # correct key → no raise
    assert "Rock" in await s.get_genres("navi:lib")


async def test_apikey_mode_wrong_key_rejected_by_enforcing_server():
    fake = FakeSubsonic(known_apikey="the-right-key")
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    s = SubsonicSource(server_url=SERVER, api_key="WRONG-key", username=USER,
                       source_id="navi", http=http)  # apikey (default)
    with pytest.raises(SubsonicAuthError):
        await s.validate_credentials()


async def test_token_mode_correct_password_accepted_by_enforcing_server():
    # Proves t=md5(password+salt) is WIRE-correct: the fake recomputes it
    # server-side from the known password and only accepts a matching token.
    fake = FakeSubsonic(has_api_key_ext=False, known_password=PASSWORD)
    s = _token_source(fake)  # password == PASSWORD
    await s.validate_credentials()                 # correct password → no raise
    assert "Rock" in await s.get_genres("navi:lib")


async def test_token_mode_wrong_password_rejected_by_enforcing_server():
    fake = FakeSubsonic(has_api_key_ext=False, known_password="correct-pw")
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    s = SubsonicSource(server_url=SERVER, api_key="WRONG-pw", username=USER,
                       source_id="navi", http=http, auth_mode="token")
    with pytest.raises(SubsonicAuthError):
        await s.validate_credentials()


# ── browse parse ─────────────────────────────────────────────────────────────

async def test_get_libraries_single_synthetic_library(fake):
    # Subsonic has no per-library concept for getArtists; one synthetic library.
    s = _source(fake)
    libs = await s.get_libraries()
    assert len(libs) == 1
    assert libs[0].type == "artist"
    assert libs[0].server_name == "Navi"
    assert libs[0].key.startswith("navi:")


async def test_artists_albums_tracks_parse(fake):
    s = _source(fake)

    arts = await s.get_artists("navi:root")
    assert arts[0].id == "navi:ar1"
    assert arts[0].title == "Artist One"
    assert arts[0].match_ids == {"musicbrainzartist": "mba-1"}

    albs = await s.get_albums("navi:root")
    a0 = albs[0]
    assert a0.id == "navi:al1"
    assert a0.artist == "Artist One"
    assert a0.year == 2001
    assert a0.track_count == 2               # songCount → track_count
    assert a0.match_ids == {"musicbrainzalbum": "mbal-1"}

    trks = await s.get_tracks("navi:root", album_id="navi:al1")
    t0 = next(t for t in trks if t.id == "navi:tr1")
    assert t0.duration_ms == 180000          # seconds → ms
    assert t0.album_id == "navi:al1"         # equals owning album .id
    assert t0.disc_number == 1 and t0.track_number == 1
    assert t0.genre == "Rock"
    assert t0.stream_key == "navi:tr1"
    # recording MBID on the track; album mbid scoped OUT (false-merge guard)
    assert t0.match_ids == {"musicbrainzrecording": "mbtrack-1"}


async def test_track_without_mbid_ingests_empty_match_ids(fake):
    s = _source(fake)
    trks = await s.get_tracks("navi:root", album_id="navi:al1")
    t1 = next(t for t in trks if t.id == "navi:tr1b")
    assert t1.match_ids == {}
    assert t1.title == "Song Two"
    assert t1.duration_ms == 200000


async def test_album_id_equals_owning_album_id(fake):
    s = _source(fake)
    albs = await s.get_albums("navi:root")
    trks = await s.get_tracks("navi:root", album_id=albs[0].id)
    for t in trks:
        assert t.album_id == albs[0].id


async def test_get_track_and_album_single_item(fake):
    s = _source(fake)
    t = await s.get_track("navi:tr1")
    assert t.title == "Song One" and t.duration_ms == 180000
    a = await s.get_album("navi:al1")
    assert a.title == "Album One" and a.year == 2001 and a.track_count == 2


async def test_genres(fake):
    s = _source(fake)
    assert await s.get_genres("navi:root") == ["Rock", "Jazz"]


# ── second-server shape (gonic) parses with the SAME adapter (AE3) ───────────

async def test_second_server_shape_parses_unmodified():
    gonic = GonicFakeSubsonic()
    http = httpx.AsyncClient(transport=httpx.MockTransport(gonic.handler))
    s = SubsonicSource(server_url=SERVER, api_key=API_KEY, username=USER,
                       source_id="gonic", server_name="Gonic", http=http)
    arts = await s.get_artists("gonic:root")
    assert arts[0].id == "gonic:10" and arts[0].title == "Gonic Artist"
    albs = await s.get_albums("gonic:root")
    assert albs[0].id == "gonic:20" and albs[0].track_count == 1
    trks = await s.get_tracks("gonic:root", album_id="gonic:20")
    assert trks[0].id == "gonic:30" and trks[0].duration_ms == 240000
    assert trks[0].match_ids == {}           # gonic emits no MBIDs


# ── search ───────────────────────────────────────────────────────────────────

async def test_search_buckets_by_type(fake):
    s = _source(fake)
    res = await s.search("navi:root", "song")
    assert {t.id for t in res.tracks} == {"navi:tr1", "navi:tr1b"}
    res2 = await s.search("navi:root", "album one")
    assert [a.id for a in res2.albums] == ["navi:al1"]
    res3 = await s.search("navi:root", "artist one")
    assert [a.id for a in res3.artists] == ["navi:ar1"]


async def test_empty_search_query_handled(fake):
    # Some servers reject an empty query — the adapter must not crash and must
    # not fire the upstream request with a blank query (quirk guard).
    s = _source(fake)
    res = await s.search("navi:root", "")
    assert res.tracks == [] and res.albums == [] and res.artists == []


# ── ID safety (unsafe char → opaque hashed id) ───────────────────────────────

async def test_unsafe_server_id_is_hashed_to_id_safe_opaque_id():
    fake = FakeSubsonic(unsafe_ids=True)  # server emits "ar:1" / "al:1" / "tr:1"
    s = _source(fake)

    arts = await s.get_artists("navi:root")
    albs = await s.get_albums("navi:root")

    # The colon-bearing native id must NOT appear raw in the identity — the
    # registry would split on it. It's hashed to an ID-safe opaque local id.
    assert ":1" not in arts[0].id.split(":", 1)[1]
    assert _ID_AFTER_PREFIX.match(arts[0].id)
    assert _ID_AFTER_PREFIX.match(albs[0].id)
    assert len(arts[0].id) <= 128

    # And the album we can still fetch its tracks by the opaque id (the native
    # locator was preserved internally).
    trks = await s.get_tracks("navi:root", album_id=albs[0].id)
    assert trks, "opaque album id must still resolve tracks"
    for t in trks:
        assert _ID_AFTER_PREFIX.match(t.id)
        assert t.album_id == albs[0].id
        # the stream_key routes back to the real native id, but is still ID-safe
        assert _ID_AFTER_PREFIX.match(t.stream_key)


def test_hash_is_stable_and_id_safe():
    # deterministic + colon-free + within the length bound
    h1 = sub._safe_id("weird:id/with?chars")
    h2 = sub._safe_id("weird:id/with?chars")
    assert h1 == h2
    assert re.match(r"^[A-Za-z0-9_-]+$", h1)
    # a clean id is passed through untouched (no needless hashing)
    assert sub._safe_id("clean-id_123") == "clean-id_123"


def test_safe_id_is_self_decodable_stateless():
    # P1: an unsafe/over-long id encodes to an ID-safe token that decodes back to
    # the native id with NO map (survives a registry rebuild / restart).
    for native in ("weird:id/with?chars", "x" * 300, "has spaces & symbols=!"):
        local = sub._safe_id(native)
        assert re.match(r"^[A-Za-z0-9_-]+$", local), local
        assert sub._decode_id(local) == native
    # a clean pass-through id decodes to itself
    assert sub._decode_id("clean-id_123") == "clean-id_123"


async def test_hashed_id_resolves_after_restart_with_no_map():
    # P1 regression: a FRESH SubsonicSource (empty state — simulates a registry
    # rebuild / process restart, so no in-process reverse map exists) must still
    # resolve/stream a previously-hashed id back to the correct native ?id=.
    fake = FakeSubsonic(unsafe_ids=True)  # native ids carry ":" → hashed

    # Source A builds the identity (as a scan would).
    a = _source(fake)
    albs = await a.get_albums("navi:root")
    hashed_album_id = albs[0].id          # e.g. "navi:_b64_..."
    trks = await a.get_tracks("navi:root", album_id=hashed_album_id)
    hashed_stream_key = trks[0].stream_key

    # Source B is brand new (empty _cache, no reverse map) — the id was produced
    # by a *different* instance, so a map-based design would fail here.
    b = _source(FakeSubsonic(unsafe_ids=True))

    # resolve_stream recovers the real native id (fake._tid, e.g. "tr:1") — the
    # decoded ?id= value must equal the native id even though no map exists.
    target = b.resolve_stream(hashed_stream_key)
    parsed = httpx.URL(target.url)
    assert parsed.params.get("id") == fake._tid
    # and _strip recovers the native id from the hashed identity statelessly.
    assert b._strip(hashed_stream_key) == fake._tid
    # fetching the album's tracks by the hashed id addresses the right album.
    trks_b = await b.get_tracks("navi:root", album_id=hashed_album_id)
    assert trks_b, "a hashed album id must resolve on a fresh source (no map)"


# ── streaming (R6: credential in the server-side URL only, never device-facing) ──

def test_resolve_stream_has_no_credential_in_device_url(fake):
    s = _source(fake)
    target = s.resolve_stream("navi:tr1")
    assert isinstance(target, StreamTarget)
    assert target.path is None
    assert target.url  # the server-side-fetched URL DOES carry the key
    # R6/AE4: the key/salt/token must not ride a device-facing URL. This URL is
    # only fetched server-side by the proxy; U5 force-proxies via url_borne_auth
    # so a device never receives it. But belt-and-suspenders: the source flags it.
    assert s.url_borne_auth is True
    # No auth in the headers dict either (Subsonic auths via query params).
    assert not (target.headers or {})


async def test_fetch_art(fake):
    s = _source(fake)
    arts = await s.get_artists("navi:root")
    data, ctype = await s.fetch_art(arts[0].thumb, width=64)
    assert data == b"BYTES" and ctype == "image/jpeg"


# ── never log the api key ────────────────────────────────────────────────────

async def test_fetch_art_failure_does_not_log_api_key(caplog):
    import logging

    class Boom:
        def handler(self, request):
            raise httpx.ConnectError("down", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(Boom().handler))
    s = SubsonicSource(server_url=SERVER, api_key=API_KEY, username=USER,
                       source_id="navi", server_name="Navi", http=http)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(httpx.HTTPError):
            await s.fetch_art("navi:al1", width=64)
    assert API_KEY not in caplog.text


def test_resolve_stream_never_logs_api_key(caplog):
    import logging
    s = _source(FakeSubsonic())
    with caplog.at_level(logging.DEBUG):
        s.resolve_stream("navi:tr1")
    assert API_KEY not in caplog.text


# ── enrichments degrade to empty ─────────────────────────────────────────────

async def test_unsupported_enrichments_degrade_to_empty(fake):
    s = _source(fake)
    assert await s.get_styles_with_counts("navi:root") == []
    assert await s.get_sonic_nearest("navi:tr1") == []
    assert await s.get_artist_similar_names("navi:tr1") == []
    assert await s.get_artist_popular_tracks("navi:ar1") == []
    assert await s.get_years("navi:root") == []
    assert await s.get_album_track_counts("navi:root") == {}
