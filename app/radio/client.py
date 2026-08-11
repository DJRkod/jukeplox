"""Radio Browser directory client (radio plan U1).

A standalone, anonymous client for the Radio Browser API (https://docs.radio-browser.info/):
discovers a healthy mirror at runtime (no hardcoded host), browses/searches stations,
resolves a station to its playable URL, and reports a play/click as a good-citizen
signal — with its OWN ``httpx.AsyncClient``, a dedicated ``asyncio.Semaphore``, an SWR
module cache, and last-known-good mirror persistence.

Design (mirrors existing repo conventions):
- **Own httpx client + semaphore** like ``app/sources/subsonic.py`` — isolated from the
  Plex/source pool so an untrusted, slow directory can never poison the shared pool.
- **Start-gate deadlines, never ``wait_for``-cancel** — every request carries a bounded
  ``httpx`` timeout; we never wrap a call in ``asyncio.wait_for(...)`` that cancels
  mid-flight (poisons the connection pool). A slow host fails via its own timeout and we
  rotate to the next.
- **SWR cache** like ``app/api/guest.py`` ``_ENABLED_LIBS_TTL`` — an expired entry is
  served immediately while a single-flight background refresh runs, generation-guarded so
  a stale refresh can't clobber newer data, and a transient refresh failure NEVER evicts a
  good cached list.
- **Discovery off the event loop** like ``app/api/admin.py`` ``_validate_source_url`` —
  ``socket.gethostbyname_ex`` / reverse-resolve run via ``run_in_executor``.

The client returns RAW directory data. Liveness-recheck / tag-filter POLICY (the curated
"popular" set) lives in the API layer (U7), NOT here — U9 treats the curated result as
opaque so "popular" can be re-tuned without touching the client.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from urllib.parse import quote

import httpx

from app import _build_info

_log = logging.getLogger("jukeplox.radio")


def _log_task_exc(task: "asyncio.Task") -> None:
    """F10: done-callback that logs a background task's exception (at WARNING)
    unless it was cancelled — mirrors ``app/radio/stream.py``'s task-exc logging.
    A bare ``lambda t: t.exception()`` retrieves-and-drops the exception silently;
    this surfaces it so a crashed fire-and-forget task isn't invisible."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _log.warning("radio: background task raised: %s", exc, exc_info=exc)


# The Radio Browser discovery A-pool. Resolving this name yields the current set of
# healthy mirrors; we randomize + fail over across them (never hardcode one host).
DISCOVERY_HOST = "all.api.radio-browser.info"

# SWR cache TTL for tags/countries/station lists — mirrors _ENABLED_LIBS_TTL (30s).
_CACHE_TTL = 30.0

# Per-request bounded timeout (start-gate deadline). A host that stalls past this raises
# via httpx and we rotate to the next mirror — we never wait_for-cancel mid-flight.
_REQUEST_TIMEOUT = 8.0

# Bound outbound fan-out with a dedicated semaphore (not httpx pool limits) so the cap is
# provable in transport-mocked tests.
_MAX_CONCURRENCY = 4

# Bound discovery/rotation attempts so a fully-dead pool fails fast rather than looping.
_MAX_MIRROR_ATTEMPTS = 3

# Every station query always carries these — hidebroken filters dead stations and an
# explicit limit bounds the response (Radio Browser default is unbounded).
_DEFAULT_LIMIT = 100

# HTTP status codes that mean "this mirror is unhealthy, try the next" rather than a
# definitive answer. 5xx and 429 (rate limit) rotate; a 4xx like 404 is a real answer.
_ROTATE_STATUSES = frozenset({429, 500, 502, 503, 504})


class RadioDirectoryUnavailable(Exception):
    """Raised when mirror discovery AND every fallback host fail with no last-known-good
    persisted — the whole Radio Browser directory is unreachable (R13). The API layer
    turns this into an explicit "radio directory unavailable" state."""


@dataclass
class Station:
    """A parsed Radio Browser station.

    ``stationuuid`` is the stable id (use it, NOT the deprecated integer ``id``).
    ``play_url`` is the playable URL: ``url_resolved`` when present, else ``url``.
    """

    stationuuid: str
    name: str
    url: str
    url_resolved: str
    favicon: str
    codec: str
    bitrate: int
    tags: list[str]
    countrycode: str
    lastcheckok: bool

    @property
    def play_url(self) -> str:
        """The directly-playable URL — ``url_resolved`` (fall back to ``url``)."""
        return self.url_resolved or self.url

    @classmethod
    def from_json(cls, item: dict[str, Any]) -> "Station":
        raw_tags = item.get("tags") or ""
        if isinstance(raw_tags, list):
            tags = [str(t).strip() for t in raw_tags if str(t).strip()]
        else:
            tags = [t.strip() for t in str(raw_tags).split(",") if t.strip()]
        try:
            bitrate = int(item.get("bitrate") or 0)
        except (ValueError, TypeError):
            bitrate = 0
        return cls(
            stationuuid=str(item.get("stationuuid") or ""),
            name=str(item.get("name") or "").strip(),
            url=str(item.get("url") or ""),
            url_resolved=str(item.get("url_resolved") or ""),
            favicon=str(item.get("favicon") or ""),
            codec=str(item.get("codec") or ""),
            bitrate=bitrate,
            tags=tags,
            countrycode=str(item.get("countrycode") or ""),
            lastcheckok=bool(item.get("lastcheckok")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "stationuuid": self.stationuuid,
            "name": self.name,
            "url": self.url,
            "url_resolved": self.url_resolved,
            "play_url": self.play_url,
            "favicon": self.favicon,
            "codec": self.codec,
            "bitrate": self.bitrate,
            "tags": self.tags,
            "countrycode": self.countrycode,
            "lastcheckok": self.lastcheckok,
        }


@dataclass
class _CacheEntry:
    value: Any
    fetched_at: float = field(default_factory=time.monotonic)
    refreshing: bool = False

    @property
    def fresh(self) -> bool:
        return (time.monotonic() - self.fetched_at) < _CACHE_TTL


# Type of the off-loop resolver: name -> (canonical_host, aliaslist, ipaddrlist).
Resolver = Callable[[str], tuple[str, list[str], list[str]]]


def _json_loads(body: bytes) -> Any:
    return json.loads(body)


def _default_resolver(name: str) -> tuple[str, list[str], list[str]]:
    return socket.gethostbyname_ex(name)


def _default_reverse(ip: str) -> str:
    return socket.gethostbyaddr(ip)[0]


class RadioBrowserClient:
    """Anonymous Radio Browser directory client — see module docstring.

    Injectable seams (for tests, no network): ``http`` (an ``httpx.AsyncClient``, e.g.
    one built on ``httpx.MockTransport``), ``resolver`` (``gethostbyname_ex`` shape),
    ``reverse`` (IP -> hostname), and ``persist_load``/``persist_save`` (last-known-good
    mirror persistence; default to the ``app.database`` helpers).
    """

    def __init__(
        self,
        *,
        http: httpx.AsyncClient | None = None,
        resolver: Resolver | None = None,
        reverse: Callable[[str], str] | None = None,
        persist_load: Callable[[], Awaitable[list[str]]] | None = None,
        persist_save: Callable[[list[str]], Awaitable[None]] | None = None,
        max_concurrency: int = _MAX_CONCURRENCY,
    ) -> None:
        git_sha = getattr(_build_info, "GIT_SHA", "unknown")
        self._http = http or httpx.AsyncClient(
            timeout=_REQUEST_TIMEOUT,
            http2=False,
            follow_redirects=True,
            headers={"User-Agent": f"Jukeplox/{git_sha}"},
            limits=httpx.Limits(
                max_connections=max_concurrency,
                max_keepalive_connections=max_concurrency,
            ),
        )
        self._resolver = resolver or _default_resolver
        self._reverse = reverse or _default_reverse
        self._persist_load = persist_load or self._db_load
        self._persist_save = persist_save or self._db_save
        self._sem = asyncio.Semaphore(max_concurrency)

        # Discovered pool of mirror hostnames (randomized), and the currently-chosen
        # healthy host cached for the session. Guarded by _discovery_lock so concurrent
        # first-callers discover once.
        self._hosts: list[str] = []
        self._current_host: str | None = None
        self._discovery_lock = asyncio.Lock()

        # SWR cache + single-flight refresh tasks + a generation counter. A refresh that
        # began before an invalidate must not write its now-stale result back.
        self._cache: dict[str, _CacheEntry] = {}
        self._refresh_tasks: dict[str, asyncio.Task] = {}
        self._gen: int = 0

    # ── persistence defaults (last-known-good mirrors, R13) ────────────────────

    @staticmethod
    async def _db_load() -> list[str]:
        from app import database
        return await database.get_radio_last_known_mirrors()

    @staticmethod
    async def _db_save(hosts: list[str]) -> None:
        from app import database
        await database.set_radio_last_known_mirrors(hosts)

    # ── discovery ──────────────────────────────────────────────────────────────

    async def _discover_hosts(self) -> list[str]:
        """Resolve the A-pool OFF the event loop, reverse-resolve each IP to its hostname
        (for TLS/SNI), randomize, and return the host list. Persists the result as
        last-known-good. On hard failure, falls back to the persisted list. Raises
        RadioDirectoryUnavailable when live discovery fails AND nothing is persisted."""
        loop = asyncio.get_running_loop()
        hosts: list[str] = []
        try:
            _canonical, _aliases, ips = await loop.run_in_executor(
                None, self._resolver, DISCOVERY_HOST
            )
            for ip in ips:
                try:
                    host = await loop.run_in_executor(None, self._reverse, ip)
                    if host:
                        hosts.append(host)
                except OSError:
                    # A reverse lookup miss for one IP shouldn't fail discovery; the raw
                    # IP is unusable for TLS/SNI, so we simply skip it.
                    _log.debug("radio: reverse-resolve failed for %s", ip)
        except OSError:
            _log.warning("radio: live mirror discovery failed", exc_info=True)

        # Dedup preserving nothing (we randomize anyway).
        hosts = list({h for h in hosts if h})
        if hosts:
            random.shuffle(hosts)
            self._hosts = hosts
            # Persist last-known-good (fire in the caller's context — it's a cheap
            # settings write and must complete so a later cold start can fall back).
            try:
                await self._persist_save(hosts)
            except Exception:
                _log.warning("radio: persisting last-known-good mirrors failed",
                             exc_info=True)
            return hosts

        # Live discovery yielded nothing — fall back to last-known-good.
        try:
            persisted = await self._persist_load()
        except Exception:
            _log.warning("radio: loading last-known-good mirrors failed", exc_info=True)
            persisted = []
        if persisted:
            _log.warning("radio: using %d persisted last-known-good mirror(s)",
                         len(persisted))
            fallback = list(persisted)
            random.shuffle(fallback)
            self._hosts = fallback
            return fallback

        raise RadioDirectoryUnavailable(
            "Could not discover any Radio Browser mirror and no last-known-good "
            "mirror list is persisted."
        )

    async def _ensure_hosts(self) -> list[str]:
        if self._hosts:
            return self._hosts
        async with self._discovery_lock:
            if self._hosts:  # another caller discovered while we waited
                return self._hosts
            return await self._discover_hosts()

    def _rotate(self) -> None:
        """Drop the current (failed) host and pick the next from the pool."""
        if self._current_host and self._current_host in self._hosts:
            self._hosts.remove(self._current_host)
        self._current_host = self._hosts[0] if self._hosts else None

    # ── request plumbing (failover + start-gate deadline) ──────────────────────

    async def _request_json(self, path: str, params: dict | None = None) -> Any:
        """GET ``https://<mirror>/<path>`` as JSON, rotating mirrors on failure.

        Bounded by _MAX_MIRROR_ATTEMPTS. Each attempt uses a fresh healthy host; a 5xx /
        429 / timeout / transport error rotates to the next. Raises the last error (or
        RadioDirectoryUnavailable if no host could ever be established)."""
        await self._ensure_hosts()
        if not self._current_host:
            self._current_host = self._hosts[0] if self._hosts else None
        if not self._current_host:
            raise RadioDirectoryUnavailable("No Radio Browser mirror available.")

        last_err: Exception | None = None
        for _ in range(_MAX_MIRROR_ATTEMPTS):
            host = self._current_host
            if not host:
                break
            url = f"https://{host}/{path.lstrip('/')}"
            try:
                async with self._sem:
                    resp = await self._http.get(url, params=params)
                if resp.status_code in _ROTATE_STATUSES:
                    _log.warning("radio: mirror %s returned %s — rotating",
                                 host, resp.status_code)
                    self._rotate()
                    last_err = httpx.HTTPStatusError(
                        f"{resp.status_code}", request=resp.request, response=resp)
                    continue
                resp.raise_for_status()
                # Parse the JSON body off the event loop — Radio Browser station
                # lists can be large, and json.loads on the loop thread would block
                # it. (resp.json() would parse inline; we parse resp.content in the
                # executor instead.)
                body = resp.content
                if len(body) > 64 * 1024:
                    return await asyncio.get_running_loop().run_in_executor(
                        None, _json_loads, body)
                return _json_loads(body)
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                _log.warning("radio: request to %s failed (%s) — rotating", host, exc)
                last_err = exc
                self._rotate()
                continue

        if last_err is not None:
            raise last_err
        raise RadioDirectoryUnavailable("Every Radio Browser mirror failed.")

    @staticmethod
    def _station_params(extra: dict | None = None, limit: int = _DEFAULT_LIMIT) -> dict:
        """Merge caller params with the ALWAYS-ON hidebroken + explicit limit."""
        params: dict[str, Any] = {"hidebroken": "true", "limit": limit}
        if extra:
            params.update({k: v for k, v in extra.items() if v is not None})
        return params

    async def _fetch_stations(self, path: str, extra: dict | None = None,
                              limit: int = _DEFAULT_LIMIT) -> list[Station]:
        data = await self._request_json(path, self._station_params(extra, limit))
        if not isinstance(data, list):
            return []
        return [Station.from_json(item) for item in data if isinstance(item, dict)]

    # ── browse / search (R2) ────────────────────────────────────────────────────

    async def search_stations(
        self,
        *,
        name: str | None = None,
        tag: str | None = None,
        tag_list: str | None = None,
        country: str | None = None,
        countrycode: str | None = None,
        codec: str | None = None,
        order: str | None = None,
        reverse: bool | None = None,
        offset: int | None = None,
        limit: int = _DEFAULT_LIMIT,
    ) -> list[Station]:
        """``/json/stations/search`` — the general faceted search. hidebroken + limit are
        always applied."""
        extra: dict[str, Any] = {
            "name": name,
            "tag": tag,
            "tagList": tag_list,
            "country": country,
            "countrycode": countrycode,
            "codec": codec,
            "order": order,
            "offset": offset,
        }
        if reverse is not None:
            extra["reverse"] = "true" if reverse else "false"
        return await self._fetch_stations("json/stations/search", extra, limit)

    async def stations_by_tag_exact(self, tag: str, *, limit: int = _DEFAULT_LIMIT) -> list[Station]:
        """``/json/stations/bytagexact/{tag}``."""
        return await self._fetch_stations(
            f"json/stations/bytagexact/{quote(tag, safe='')}", limit=limit)

    async def stations_by_countrycode_exact(self, cc: str, *, limit: int = _DEFAULT_LIMIT) -> list[Station]:
        """``/json/stations/bycountrycodeexact/{cc}``."""
        return await self._fetch_stations(
            f"json/stations/bycountrycodeexact/{quote(cc, safe='')}", limit=limit)

    async def top_click(self, n: int = _DEFAULT_LIMIT) -> list[Station]:
        """``/json/stations/topclick/{n}`` — most-clicked stations (RAW; the
        liveness-recheck / tag-filter policy for the curated "popular" set lives in the
        API layer, U7)."""
        return await self._fetch_stations(f"json/stations/topclick/{int(n)}", limit=n)

    # ── curated-landing raw data (R3) — SWR-cached ─────────────────────────────

    async def get_tags(self) -> list[dict[str, Any]]:
        """``/json/tags`` (with counts) — RAW, SWR-cached. Genre quick-picks are derived
        from this in the API layer (U7)."""
        return await self._cached("tags", lambda: self._request_json("json/tags"))

    async def get_countries(self) -> list[dict[str, Any]]:
        """``/json/countries`` — RAW, SWR-cached."""
        return await self._cached("countries", lambda: self._request_json("json/countries"))

    async def get_top_click_cached(self, n: int = _DEFAULT_LIMIT) -> list[Station]:
        """SWR-cached ``topclick`` for the curated landing "popular" set (RAW stations;
        the liveness-recheck lives in U7)."""
        return await self._cached(f"topclick:{n}", lambda: self.top_click(n))

    # ── resolve + click report (R4) ────────────────────────────────────────────

    @staticmethod
    def resolve_play_url(station: Station) -> str:
        """Return the directly-playable URL for a station — ``url_resolved`` (fall back to
        ``url``). Pure; SSRF validation is U2's responsibility before the server fetches."""
        return station.play_url

    def report_click(self, stationuuid: str) -> None:
        """Fire-and-forget good-citizen click report to ``GET /json/url/{stationuuid}``.

        Spawns a background task; a failing or slow click MUST NOT raise to the caller or
        delay playback start. Never awaited by the resolve path."""
        if not stationuuid:
            return

        async def _do() -> None:
            try:
                await self._request_json(f"json/url/{stationuuid}")
            except Exception:
                _log.debug("radio: click report for %s failed (ignored)",
                           stationuuid, exc_info=True)

        try:
            task = asyncio.ensure_future(_do())
            task.add_done_callback(_log_task_exc)
        except RuntimeError:
            # No running loop (shouldn't happen in the app) — silently drop; the click is
            # best-effort and must never surface.
            _log.debug("radio: no event loop for click report %s", stationuuid)

    # ── SWR cache (generation-guarded, transient-safe) ─────────────────────────

    async def _cached(self, key: str, fetch: Callable[[], Awaitable[Any]]) -> Any:
        """Stale-while-revalidate cache. A fresh entry is returned directly. An EXPIRED
        entry is ALSO returned directly while a single-flight background refresh runs. The
        very first (no entry) call blocks. A transient refresh failure keeps the stale
        entry; a generation bump (invalidate) drops an in-flight refresh's result."""
        entry = self._cache.get(key)
        if entry is not None:
            if not entry.fresh:
                self._spawn_refresh(key, fetch)
            return entry.value
        # Cold: block on the first fetch. Do NOT cache a failure as a definitive miss —
        # let the exception propagate so the caller retries.
        value = await fetch()
        self._cache[key] = _CacheEntry(value=value)
        return value

    def _spawn_refresh(self, key: str, fetch: Callable[[], Awaitable[Any]]) -> None:
        existing = self._refresh_tasks.get(key)
        if existing is not None and not existing.done():
            return  # single-flight

        gen = self._gen

        async def _refresh() -> None:
            try:
                value = await fetch()
            except Exception:
                # Never cache a transient failure as a definitive empty/miss — the good
                # stale entry survives.
                _log.warning("radio: SWR refresh of %s failed — keeping stale", key,
                             exc_info=True)
                return
            if gen != self._gen:
                # An invalidate landed while we fetched — this result is stale relative to
                # the new generation; drop it rather than clobbering newer data.
                return
            self._cache[key] = _CacheEntry(value=value)

        task = asyncio.ensure_future(_refresh())
        self._refresh_tasks[key] = task
        task.add_done_callback(_log_task_exc)
        # F10: prune completed refresh tasks so _refresh_tasks can't grow unbounded
        # (a distinct key per never-cleared entry would leak). Pop only if this is
        # still the entry for `key` (a newer single-flight refresh may have replaced
        # it).
        task.add_done_callback(
            lambda t, k=key: self._refresh_tasks.pop(k, None)
            if self._refresh_tasks.get(k) is t else None)

    def invalidate_cache(self) -> None:
        """Drop all cached lists and bump the generation so any in-flight refresh's result
        is discarded rather than resurrecting the cleared cache."""
        self._gen += 1
        self._cache.clear()

    async def aclose(self) -> None:
        await self._http.aclose()


# ── module singleton ───────────────────────────────────────────────────────────

_client: RadioBrowserClient | None = None


def get_radio_client() -> RadioBrowserClient:
    """Return the process-wide Radio Browser client (lazy singleton)."""
    global _client
    if _client is None:
        _client = RadioBrowserClient()
    return _client
