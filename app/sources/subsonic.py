"""OpenSubsonic provider — a ``MusicSource`` over the Subsonic/OpenSubsonic API
(plan U2, one adapter covering Navidrome / gonic / Ampache / Nextcloud Music /
LMS / Lyrion).

No ``py-opensonic`` dependency: the REST surface is small enough for direct
``httpx``. The shape mirrors ``app/sources/jellyfin.py`` — a per-server
concurrency semaphore, a TTL cache, small parse helpers, and the
``{source_id}:{native}`` key namespace via ``_make_id``/``_strip`` — so the
providers read alike.

Auth model (2026-08-11-003, token+salt fallback): a Subsonic connection stores a
**secret** whose meaning is set by the negotiated ``auth_mode``. When the server
advertises the OpenSubsonic *API-Key extension* (``getOpenSubsonicExtensions`` →
``apiKeyAuthentication``) the secret is an **API key** and requests carry
``apiKey=<key>`` (the original, preferred path). When the server lacks the
extension — Navidrome, gonic, and most self-hosted servers — the secret is the
account **password** and requests use the standard Subsonic **token+salt** auth:
``u`` + ``s=<fresh random salt>`` + ``t=md5(password+salt)``, a fresh salt per
request. The cleartext password is NEVER transmitted (no legacy ``p=``) and is
sealed at rest. Common params are always ``u`` / ``v`` / ``c`` / ``f=json`` plus
the mode-specific auth params. Capability detection at connect uses a dedicated
credential-free probe (``_probe_extensions_unauth``); the secret is then proven
by an authenticated ``validate_credentials`` call (NOT the no-network
``get_libraries``).

Credential-in-URL (R6): unlike Plex/Jellyfin (header auth), Subsonic auths via
query params, so a stream URL carries the key. ``url_borne_auth`` is ``True`` so
U5 force-proxies every Subsonic stream — the credentialed URL is fetched only
server-side and never reaches a Cast/DLNA device. **The upstream URL is never
logged** (it carries the key); log only the opaque stream key.

Identifiers (ID safety, load-bearing): the registry splits a key on the first
``:`` and enqueue validates ``^[A-Za-z0-9_-]+$``. Subsonic ids are opaque
server strings and *usually* colon-free, but the spec does not guarantee it, so
``_safe_id`` encodes any id with an unsafe char (or an over-long id) to an
opaque, ID-safe local id at the source boundary. The encoding is **stateless and
self-decodable**: an unsafe/long native id is urlsafe-base64'd (no padding) with
a short reserved ``_b64_`` prefix, so ``_strip``/``_decode_id`` recover the real
native id with NO map lookup — a hashed id therefore survives a registry rebuild
(``invalidate_plex_client``) or a process restart (P1). A safe, short native id
passes through unchanged. Because the recovery is stateless there is no in-process
reverse map to lose.

Durations: Subsonic ``duration`` is whole seconds → ``* 1000`` ms.

Capabilities: native search (``search3``) + genres only. Sonic similarity,
popular tracks, and Plex "styles" degrade to the base class's empty defaults.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.models import Album, Artist, Library, SearchResults, Track
from app.sources.base import Capabilities, MusicSource, StreamTarget

_log = logging.getLogger("jukeplox.subsonic")

CLIENT_NAME = "Jukeplox"
API_VERSION = "1.16.1"  # OpenSubsonic baseline (supports search3/getArtists/getAlbum)

_CACHE_TTL = 300  # seconds — mirrors JellyfinSource / PlexClient

_SUBSONIC_CAPS = Capabilities(native_search=True, genres=True)

_API_KEY_EXTENSION = "apikeyauthentication"

# The two auth modes a Subsonic source can use. ``apikey`` = the secret is an
# OpenSubsonic API key (apiKey= param); ``token`` = the secret is the account
# password, sent as u/s/t token+salt. Validated at construction/reconfigure so a
# typo ('Token', 'apiKey') fails loudly rather than silently emitting the wrong
# auth params at request time.
_VALID_AUTH_MODES = ("apikey", "token")

# Safety bounds on getAlbumList2 pagination so a server that never returns a short
# final page can't loop forever. 500-album pages × 400 pages = 200k albums, well
# past any real library; the total cap is a second belt.
_MAX_ALBUM_PAGES = 400
_MAX_ALBUMS_TOTAL = 200_000

# Only these MusicBrainz ids belong on their entity. Subsonic exposes a single
# ``musicBrainzId`` per item whose *meaning is the item's own type*: on an artist
# it's the artist mbid, on an album the release(-group) mbid, on a song the
# recording mbid. Scoping is load-bearing — mirror jellyfin's `_mb_match_ids`
# entity scoping so an album mbid can never ride a track and false-merge two
# tracks of one album (plan R8 — prefer a visible duplicate over a false merge).
_MB_ARTIST_SCHEME = "musicbrainzartist"
_MB_ALBUM_SCHEME = "musicbrainzalbum"
_MB_RECORDING_SCHEME = "musicbrainzrecording"

# The identity shape the registry/enqueue accept for the native part of a key.
_ID_SAFE = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"

# Reserved prefix for a base64url-encoded native id. It is itself id-safe (only
# ``_`` + ``[a-z0-9]``) and — because a real native id that HAPPENS to start with
# ``_b64_`` would already be unsafe/over-long-or-safe-and-passed-through — a
# pass-through id can never collide with it: a native id equal to its own encoded
# form is impossible (base64url of any non-empty input never reproduces the input
# verbatim with this prefix), and we only ever emit this prefix from _safe_id.
_B64_PREFIX = "_b64_"

# Longest native id we pass through verbatim; longer ids are base64-encoded so the
# ``{source_id}:{local}`` key stays bounded.
_MAX_PASSTHROUGH = 128


class SubsonicAuthError(Exception):
    """Raised when a Subsonic server rejects credentials or lacks API-Key auth.

    The connect handler (U4) rejects on this — it is the "no API-Key support"
    and "auth rejected" signal.
    """


def _safe_id(native_id: str) -> str:
    """Return an ID-safe, **self-decodable** token for a native server id.

    A clean id (matches ``^[A-Za-z0-9_-]+$``, ≤128 chars, and not colliding with
    the reserved ``_b64_`` prefix) is passed through untouched — no needless
    encoding, keeps ids human-legible where possible. Any id with an unsafe char
    (notably ``:``, which the registry splits on) or an over-long id is encoded as
    urlsafe-base64 (no padding) behind the reserved ``_b64_`` prefix. base64url is
    itself ID-safe (``[A-Za-z0-9_-]``) and the encoding is reversible with NO map
    lookup, so the real native id survives a registry rebuild / process restart
    (P1)."""
    if (
        native_id
        and len(native_id) <= _MAX_PASSTHROUGH
        and not native_id.startswith(_B64_PREFIX)
        and all(c in _ID_SAFE for c in native_id)
    ):
        return native_id
    encoded = base64.urlsafe_b64encode(native_id.encode("utf-8")).rstrip(b"=")
    return _B64_PREFIX + encoded.decode("ascii")


def _decode_id(local_id: str) -> str:
    """Recover the native server id from an ID-safe local id produced by
    :func:`_safe_id` — stateless inverse (no map). A ``_b64_``-prefixed id is
    base64url-decoded; anything else was a pass-through and is returned as-is."""
    if not local_id or not local_id.startswith(_B64_PREFIX):
        return local_id
    payload = local_id[len(_B64_PREFIX):]
    pad = "=" * (-len(payload) % 4)
    try:
        return base64.urlsafe_b64decode(payload + pad).decode("utf-8")
    except Exception:
        # A malformed token can't be decoded — return it verbatim so the caller
        # fails visibly upstream rather than silently addressing the wrong id.
        return local_id


@dataclass
class _CacheEntry:
    value: Any
    expires_at: float = field(default_factory=lambda: time.monotonic() + _CACHE_TTL)

    @property
    def valid(self) -> bool:
        return time.monotonic() < self.expires_at


class SubsonicSource(MusicSource):
    def __init__(
        self,
        server_url: str,
        api_key: str,
        username: str,
        source_id: str = "subsonic",
        server_name: str = "",
        http: httpx.AsyncClient | None = None,
        max_concurrency: int | None = None,
        auth_mode: str = "apikey",
    ) -> None:
        self.server_url = server_url.rstrip("/")
        # ``_secret`` is the API key (apikey mode) OR the account password
        # (token mode); ``_auth_mode`` decides how _common_params uses it. The
        # ``api_key`` parameter name is kept for back-compat — in apikey mode it
        # IS the api key; in token mode the caller passes the password here and
        # sets auth_mode="token" (or calls set_auth).
        self._secret = api_key
        if auth_mode not in _VALID_AUTH_MODES:
            raise ValueError(f"invalid auth_mode {auth_mode!r}; expected one of {_VALID_AUTH_MODES}")
        self._auth_mode = auth_mode
        self.username = username
        self._source_id = source_id or "subsonic"
        self.server_name = server_name
        self._cache: dict[str, _CacheEntry] = {}
        # No reverse map: _safe_id is self-decodable (base64url behind a reserved
        # prefix), so a hashed id resolves back to its native id statelessly and
        # survives a registry rebuild / restart (P1).
        if max_concurrency is None:
            from app.config import settings
            max_concurrency = settings.plex_max_concurrency
        self._sem = asyncio.Semaphore(max_concurrency)
        self._http = http or httpx.AsyncClient(
            timeout=15,
            http2=False,
            limits=httpx.Limits(
                max_connections=max_concurrency,
                max_keepalive_connections=max_concurrency,
            ),
        )

    # ── identity ──────────────────────────────────────────────────────────────

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def source_type(self) -> str:
        return "subsonic"

    @property
    def capabilities(self) -> Capabilities:
        return _SUBSONIC_CAPS

    @property
    def url_borne_auth(self) -> bool:
        # Subsonic auths via URL query params; U5 reads this to force every
        # stream through the /api/stream proxy so the key never reaches a device.
        return True

    # ── id namespace (stateless, self-decodable — no reverse map) ──────────────

    def _make_id(self, native_id: str) -> str:
        return f"{self._source_id}:{_safe_id(native_id)}"

    def _strip(self, key: str | None) -> str:
        """Return the native server id for a ``{source_id}:{local}`` key (or a
        bare key), statelessly reversing the id-safe encoding."""
        prefix = f"{self._source_id}:"
        local = key
        if key and key.startswith(prefix):
            local = key[len(prefix):]
        return _decode_id(local or "")

    # ── request plumbing ──────────────────────────────────────────────────────

    def _common_params(self) -> dict:
        base = {
            "u": self.username,
            "v": API_VERSION,
            "c": CLIENT_NAME,
            "f": "json",
        }
        if self._auth_mode == "token":
            # Standard Subsonic token+salt: fresh random salt per request,
            # t=md5(password+salt). The cleartext password is never sent (no
            # legacy p=). md5 is the wire protocol here, NOT a security
            # primitive — usedforsecurity=False documents that (and dodges FIPS
            # lint). token_hex(8) → 16 hex chars, well past the spec's 6-char
            # salt floor.
            salt = secrets.token_hex(8)
            token = hashlib.md5(
                (self._secret + salt).encode("utf-8"), usedforsecurity=False
            ).hexdigest()
            return {**base, "s": salt, "t": token}
        return {**base, "apiKey": self._secret}

    def set_auth(self, auth_mode: str, secret: str | None = None) -> None:
        """Reconfigure the auth mode (and optionally the secret) on an existing
        source. The connect route probes capability, then routes THIS one source
        into the detected mode via set_auth — avoiding a second SubsonicSource
        (each __init__ opens its own httpx client, which would leak past the
        route's single ``finally: aclose()``)."""
        if auth_mode not in _VALID_AUTH_MODES:
            raise ValueError(f"invalid auth_mode {auth_mode!r}; expected one of {_VALID_AUTH_MODES}")
        self._auth_mode = auth_mode
        if secret is not None:
            self._secret = secret

    def _endpoint(self, name: str) -> str:
        return f"{self.server_url}/rest/{name}.view"

    async def _call(self, name: str, params: dict | None = None) -> dict:
        """GET a Subsonic endpoint, returning the inner ``subsonic-response``.

        NEVER logs the request URL — it carries ``apiKey`` in the query string.
        A Subsonic ``failed`` status with an auth code (40/41/44) raises
        ``SubsonicAuthError``; other failures raise ``httpx.HTTPError`` shape via
        a generic error.
        """
        merged = {**self._common_params(), **(params or {})}
        async with self._sem:
            resp = await self._http.get(self._endpoint(name), params=merged)
        resp.raise_for_status()
        body = resp.json().get("subsonic-response", {})
        if body.get("status") == "failed":
            err = body.get("error", {}) or {}
            code = err.get("code")
            msg = err.get("message", "Subsonic request failed")
            if code in (40, 41, 44):  # wrong creds / token unsupported / bad api key
                raise SubsonicAuthError(msg)
            raise RuntimeError(f"Subsonic error {code}: {msg}")
        return body

    def _cached(self, key: str) -> Any | None:
        entry = self._cache.get(key)
        return entry.value if entry and entry.valid else None

    def _store(self, key: str, value: Any) -> None:
        self._cache[key] = _CacheEntry(value=value)

    # ── extension detection (capability probe, U2/U4) ─────────────────────────

    async def _probe_extensions_unauth(self) -> bool:
        """Credential-free capability probe (connect-time, pre-validation).

        Issues ``getOpenSubsonicExtensions`` with ONLY ``v``/``c``/``f`` — no
        ``apiKey``/``u``/``s``/``t``/``p``. The OpenSubsonic spec allows this
        endpoint unauthenticated so a client can discover apiKey support before
        choosing how to auth; calling it through ``_call``/``_common_params``
        (which inject the credential) would transmit the still-unvalidated secret,
        so this path deliberately bypasses them. Returns ``True`` iff
        ``apiKeyAuthentication`` is advertised. Raises on a hard error (non-2xx or
        a Subsonic ``failed`` status) so the connect route can apply its
        no-silent-downgrade policy."""
        params = {"v": API_VERSION, "c": CLIENT_NAME, "f": "json"}
        async with self._sem:
            resp = await self._http.get(
                self._endpoint("getOpenSubsonicExtensions"), params=params
            )
        resp.raise_for_status()
        body = resp.json().get("subsonic-response", {}) or {}
        if body.get("status") == "failed":
            raise RuntimeError("getOpenSubsonicExtensions returned failed")
        exts = body.get("openSubsonicExtensions", []) or []
        return any(str(e.get("name", "")).lower() == _API_KEY_EXTENSION for e in exts)

    async def validate_credentials(self) -> None:
        """Prove the secret with an AUTHENTICATED request in the current mode.

        Issues ``ping`` through ``_call`` (which carries the mode's auth params),
        so a Subsonic ``failed`` auth code (40/41/44) raises ``SubsonicAuthError``.
        This is the real connect-time secret check — ``get_libraries`` is a
        no-network synthetic and validates nothing."""
        await self._call("ping")

    # ── parse helpers ─────────────────────────────────────────────────────────

    def _art(self, cover_art: str | None) -> str | None:
        if cover_art:
            return self._make_id(cover_art)
        return None

    @staticmethod
    def _mb(item: dict, scheme: str) -> dict[str, str]:
        mbid = item.get("musicBrainzId")
        if mbid and str(mbid).strip():
            return {scheme: str(mbid).strip()}
        return {}

    def _parse_artist(self, item: dict) -> Artist:
        return Artist(
            id=self._make_id(item["id"]),
            title=item.get("name", ""),
            thumb=self._art(item.get("coverArt") or item.get("artistImageUrl")),
            release_count=item.get("albumCount"),
            match_ids=self._mb(item, _MB_ARTIST_SCHEME),
        )

    def _parse_album(self, item: dict) -> Album:
        return Album(
            id=self._make_id(item["id"]),
            title=item.get("name", ""),
            artist=item.get("artist", ""),
            year=item.get("year"),
            thumb=self._art(item.get("coverArt")),
            subtype=None,
            match_ids=self._mb(item, _MB_ALBUM_SCHEME),
            track_count=item.get("songCount"),
        )

    def _parse_track(self, item: dict) -> Track:
        native_id = item["id"]
        return Track(
            id=self._make_id(native_id),
            title=item.get("title", ""),
            artist=item.get("artist", ""),
            album=item.get("album", ""),
            duration_ms=int(item.get("duration") or 0) * 1000,
            genre=item.get("genre"),
            year=item.get("year"),
            thumb=self._art(item.get("coverArt")),
            stream_key=self._make_id(native_id),
            server_name=self.server_name,
            album_artist=item.get("artist") or None,
            album_id=self._make_id(item["albumId"]) if item.get("albumId") else None,
            disc_number=item.get("discNumber") or 1,
            track_number=item.get("track"),
            match_ids=self._mb(item, _MB_RECORDING_SCHEME),
        )

    # ── core: enumerate ───────────────────────────────────────────────────────

    async def get_libraries(self) -> list[Library]:
        # Subsonic has no per-library browse split for getArtists — expose one
        # synthetic music library so the registry/scan treat it uniformly.
        return [
            Library(
                key=self._make_id("root"),
                title=self.server_name or "Music",
                type="artist",
                server_name=self.server_name,
            )
        ]

    async def get_artists(self, section_key: str) -> list[Artist]:
        if (c := self._cached("artists")) is not None:
            return c
        body = await self._call("getArtists")
        artists: list[Artist] = []
        for idx in (body.get("artists", {}) or {}).get("index", []) or []:
            for a in idx.get("artist", []) or []:
                artists.append(self._parse_artist(a))
        self._store("artists", artists)
        return artists

    async def get_albums(
        self,
        section_key: str,
        artist_id: str | None = None,
        year: int | None = None,
        style: str | None = None,
    ) -> list[Album]:
        if artist_id:
            # Artist-scoped: getArtist returns the artist's albums directly.
            native = self._strip(artist_id)
            cache_key = f"albums:artist:{native}"
            if (c := self._cached(cache_key)) is not None:
                return c
            body = await self._call("getArtist", {"id": native})
            albums = [
                self._parse_album(a)
                for a in (body.get("artist", {}) or {}).get("album", []) or []
            ]
            self._store(cache_key, albums)
            return albums

        cache_key = f"albums:all:{year}"
        if (c := self._cached(cache_key)) is not None:
            return c
        # getAlbumList2 with type=alphabeticalByName; walk pages of size 500.
        # Safety bounds: a misbehaving server that returns a full page forever
        # (never a short final page) must not loop indefinitely — cap both the
        # page count and the total albums collected.
        albums: list[Album] = []
        offset = 0
        page = 500
        pages = 0
        for pages in range(1, _MAX_ALBUM_PAGES + 1):
            params: dict = {"type": "alphabeticalByName", "size": page, "offset": offset}
            if year:
                params = {"type": "byYear", "fromYear": year, "toYear": year,
                          "size": page, "offset": offset}
            body = await self._call("getAlbumList2", params)
            batch = (body.get("albumList2", {}) or {}).get("album", []) or []
            albums.extend(self._parse_album(a) for a in batch)
            if len(batch) < page or len(albums) >= _MAX_ALBUMS_TOTAL:
                break
            offset += len(batch)
        if pages >= _MAX_ALBUM_PAGES or len(albums) >= _MAX_ALBUMS_TOTAL:
            _log.warning(
                "getAlbumList2 hit the safety bound (%d pages / %d albums) — the "
                "server may be paginating incorrectly; album list truncated.",
                pages, len(albums),
            )
        self._store(cache_key, albums)
        return albums

    async def get_tracks(
        self,
        section_key: str,
        album_id: str | None = None,
        genre: str | None = None,
        year: int | None = None,
    ) -> list[Track]:
        if album_id:
            native = self._strip(album_id)
            cache_key = f"tracks:album:{native}"
            if (c := self._cached(cache_key)) is not None:
                return c
            body = await self._call("getAlbum", {"id": native})
            songs = (body.get("album", {}) or {}).get("song", []) or []
            tracks = [self._parse_track(s) for s in songs]
            tracks.sort(key=lambda t: (t.disc_number, t.track_number or 0))
            self._store(cache_key, tracks)
            return tracks

        # Section-wide tracks (scan path) — walk every album's songs. Cache the
        # album list read via get_albums.
        cache_key = f"tracks:section:{genre}:{year}"
        if (c := self._cached(cache_key)) is not None:
            return c
        albums = await self.get_albums(section_key, year=year)
        tracks: list[Track] = []
        for alb in albums:
            # One transient per-album fetch failure must not abort the whole
            # section (the catalog _safe wrapper would then drop every track).
            # Log at WARNING and skip the failed album.
            try:
                album_tracks = await self.get_tracks(section_key, album_id=alb.id)
            except Exception:
                _log.warning(
                    "getAlbum tracks failed for album %r during section scan — "
                    "skipping this album, keeping the rest of the section.",
                    alb.title, exc_info=True,
                )
                continue
            for t in album_tracks:
                if genre and (t.genre or "").lower() != genre.lower():
                    continue
                tracks.append(t)
        self._store(cache_key, tracks)
        return tracks

    async def get_track(self, track_id: str) -> Track:
        native = self._strip(track_id)
        body = await self._call("getSong", {"id": native})
        song = body.get("song") or {}
        if not song:
            raise KeyError(f"Track {track_id} not found")
        return self._parse_track(song)

    async def get_album(self, album_id: str) -> Album:
        native = self._strip(album_id)
        body = await self._call("getAlbum", {"id": native})
        album = body.get("album") or {}
        if not album:
            raise KeyError(f"Album {album_id} not found")
        return self._parse_album(album)

    # ── core: search ──────────────────────────────────────────────────────────

    async def search(self, section_key: str, query: str) -> SearchResults:
        # Empty-search quirk (Music Assistant gotcha): some servers reject a
        # blank query. Short-circuit so we neither crash nor fire a bad request.
        if not query or not query.strip():
            return SearchResults()
        body = await self._call(
            "search3",
            {"query": query, "artistCount": 40, "albumCount": 40, "songCount": 100},
        )
        res = body.get("searchResult3", {}) or {}
        tracks = [self._parse_track(s) for s in res.get("song", []) or []]
        albums = [self._parse_album(a) for a in res.get("album", []) or []]
        artists = [self._parse_artist(a) for a in res.get("artist", []) or []]
        return SearchResults(tracks=tracks, albums=albums, artists=artists)

    # ── core: stream + art ────────────────────────────────────────────────────

    def resolve_stream(self, stream_key: str) -> StreamTarget:
        # R6: the key rides the URL query (Subsonic has no header auth), so this
        # URL is credentialed. url_borne_auth=True → U5 force-proxies it; the
        # /api/stream proxy fetches this server-side and hands the device a
        # credential-free /api/stream URL. NEVER log this url.
        native = self._strip(stream_key)
        params = {**self._common_params(), "id": native}
        req = httpx.Request("GET", self._endpoint("stream"), params=params)
        return StreamTarget(url=str(req.url), headers=None)

    async def fetch_art(self, thumb_path: str, width: int | None = None) -> tuple[bytes, str]:
        native = self._strip(thumb_path)
        params: dict = {"id": native}
        if width:
            params["size"] = width
        # Reuse _call's semaphore + never-log-url guarantee; but _call parses
        # JSON, so fetch the binary directly under the same semaphore.
        merged = {**self._common_params(), **params}
        async with self._sem:
            resp = await self._http.get(self._endpoint("getCoverArt"), params=merged)
        resp.raise_for_status()
        return resp.content, resp.headers.get("content-type", "image/jpeg")

    # ── enrichments (Subsonic supports genres; the rest degrade via base) ──────

    async def get_genres(self, section_key: str) -> list[str]:
        if (c := self._cached("genres")) is not None:
            return c
        body = await self._call("getGenres")
        genres = [
            g.get("value", "")
            for g in (body.get("genres", {}) or {}).get("genre", []) or []
            if g.get("value")
        ]
        self._store("genres", genres)
        return genres

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def invalidate_cache(self) -> None:
        self._cache.clear()
