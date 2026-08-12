"""Tests for the Radio Browser directory client (radio plan U1).

All HTTP is mocked via ``httpx.MockTransport`` and DNS via an injected fake
resolver/reverse — NOTHING hits the network. IPs use documentation ranges
(192.0.2.0/24, 198.51.100.0/24) and example hostnames only.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.radio.client import (
    RadioBrowserClient,
    RadioDirectoryUnavailable,
    Station,
    DISCOVERY_HOST,
)


# ── fake DNS ────────────────────────────────────────────────────────────────

# Documentation IP ranges (RFC 5737) mapped to example mirror hostnames.
_POOL = {
    "192.0.2.10": "de1.api.example-radio.invalid",
    "192.0.2.11": "nl1.api.example-radio.invalid",
    "198.51.100.20": "us1.api.example-radio.invalid",
}


def _fake_resolver(name):
    assert name == DISCOVERY_HOST
    return (DISCOVERY_HOST, [], list(_POOL.keys()))


def _fake_reverse(ip):
    return _POOL[ip]


def _empty_resolver(name):
    # Simulates a total discovery failure (DNS down).
    raise OSError("name resolution failed")


# ── fake transport ──────────────────────────────────────────────────────────

_STATION_JSON = [
    {
        "stationuuid": "uuid-jazz-1",
        "name": "Jazz FM",
        "url": "http://stream.example.invalid/jazz",
        "url_resolved": "http://cdn.example.invalid/jazz.mp3",
        "favicon": "http://example.invalid/jazz.png",
        "codec": "MP3",
        "bitrate": 128,
        "tags": "jazz,smooth",
        "countrycode": "US",
        "lastcheckok": 1,
    }
]


class _Recorder:
    """Records every request seen by the mock transport."""

    def __init__(self):
        self.requests: list[httpx.Request] = []


def _make_client(handler, resolver=_fake_resolver, reverse=_fake_reverse,
                 persist_load=None, persist_save=None):
    """Build a RadioBrowserClient over a MockTransport with injected DNS + no DB."""
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, timeout=5,
                             headers={"User-Agent": "Jukeplox/testsha"})

    async def _noop_save(hosts):
        if persist_save is not None:
            persist_save(hosts)

    async def _load():
        return persist_load() if persist_load is not None else []

    return RadioBrowserClient(
        http=http,
        resolver=resolver,
        reverse=reverse,
        persist_load=_load,
        persist_save=_noop_save,
    )


# ── Station parsing ───────────────────────────────────────────────────────────

def test_station_from_json_uses_stationuuid_and_resolved_url():
    s = Station.from_json(_STATION_JSON[0])
    assert s.stationuuid == "uuid-jazz-1"
    assert s.play_url == "http://cdn.example.invalid/jazz.mp3"  # url_resolved wins
    assert s.tags == ["jazz", "smooth"]
    assert s.bitrate == 128
    assert s.lastcheckok is True


def test_station_play_url_falls_back_to_url_when_resolved_empty():
    """Edge: url_resolved empty → falls back to url; stationuuid used as id."""
    item = dict(_STATION_JSON[0], url_resolved="", stationuuid="uuid-fallback")
    s = Station.from_json(item)
    assert s.url_resolved == ""
    assert s.play_url == "http://stream.example.invalid/jazz"  # falls back to url
    assert s.stationuuid == "uuid-fallback"
    assert RadioBrowserClient.resolve_play_url(s) == "http://stream.example.invalid/jazz"


# ── Happy: search-by-tag always sends hidebroken + limit ──────────────────────

async def test_search_by_tag_returns_parsed_stations_with_hidebroken_and_limit():
    rec = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        rec.requests.append(request)
        return httpx.Response(200, json=_STATION_JSON)

    client = _make_client(handler)
    stations = await client.search_stations(tag="jazz", limit=25)

    assert len(stations) == 1
    assert stations[0].stationuuid == "uuid-jazz-1"

    req = rec.requests[-1]
    assert "/json/stations/search" in req.url.path
    params = dict(req.url.params)
    assert params["hidebroken"] == "true"
    assert params["limit"] == "25"
    assert params["tag"] == "jazz"
    # UA is set on the client.
    assert req.headers["user-agent"] == "Jukeplox/testsha"
    await client.aclose()


async def test_topclick_and_bytagexact_always_carry_hidebroken_and_limit():
    rec = _Recorder()

    def handler(request: httpx.Request) -> httpx.Response:
        rec.requests.append(request)
        return httpx.Response(200, json=_STATION_JSON)

    client = _make_client(handler)
    await client.top_click(5)
    await client.stations_by_tag_exact("rock & roll", limit=7)

    top = rec.requests[0]
    assert top.url.path.endswith("/json/stations/topclick/5")
    assert dict(top.url.params)["hidebroken"] == "true"
    assert dict(top.url.params)["limit"] == "5"

    tag = rec.requests[1]
    # tag is URL-encoded in the path.
    assert "/json/stations/bytagexact/" in tag.url.path
    assert "rock" in tag.url.path
    assert dict(tag.url.params)["hidebroken"] == "true"
    await client.aclose()


# ── Happy: discovery randomizes the pool and picks a reachable host ───────────

async def test_discovery_resolves_reverses_and_randomizes(monkeypatch):
    # Force a deterministic shuffle so we can assert randomization was applied
    # (reverse) rather than insertion order.
    monkeypatch.setattr("app.radio.client.random.shuffle", lambda lst: lst.reverse())

    seen_hosts = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_hosts.append(request.url.host)
        return httpx.Response(200, json=_STATION_JSON)

    client = _make_client(handler)
    await client.search_stations(tag="jazz")

    hosts = client._hosts
    assert set(hosts) == set(_POOL.values())  # all reverse-resolved names
    # The chosen host is one of the pooled reverse names (reachable).
    assert seen_hosts[0] in _POOL.values()
    await client.aclose()


# ── Error: first mirror 5xx → rotate to next (failover). Covers AE6. ─────────

async def test_failover_first_mirror_5xx_rotates_and_succeeds():
    """Covers AE6: first mirror returns 503 → client rotates to the next host and
    the query succeeds."""
    calls = {"n": 0}
    hosts_hit = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts_hit.append(request.url.host)
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="unhealthy")
        return httpx.Response(200, json=_STATION_JSON)

    client = _make_client(handler)
    stations = await client.search_stations(tag="jazz")

    assert len(stations) == 1  # succeeded after failover
    assert calls["n"] == 2  # first failed, second succeeded
    assert hosts_hit[0] != hosts_hit[1]  # rotated to a different mirror
    await client.aclose()


async def test_failover_on_transport_timeout_rotates():
    """A connection/timeout error on the first mirror also rotates."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("timed out", request=request)
        return httpx.Response(200, json=_STATION_JSON)

    client = _make_client(handler)
    stations = await client.search_stations(tag="jazz")
    assert len(stations) == 1
    assert calls["n"] == 2
    await client.aclose()


# ── Error: discovery + mirrors fail → last-known-good → else raise. AE10. ────

async def test_discovery_hard_fail_falls_back_to_persisted(monkeypatch):
    """Covers AE10 (part 1): live discovery fails but a persisted last-known-good
    mirror list lets the client keep working."""
    monkeypatch.setattr("app.radio.client.random.shuffle", lambda lst: None)

    persisted = ["de1.api.example-radio.invalid"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_STATION_JSON)

    client = _make_client(handler, resolver=_empty_resolver,
                          persist_load=lambda: list(persisted))
    stations = await client.search_stations(tag="jazz")
    assert len(stations) == 1
    assert client._hosts == persisted
    await client.aclose()


async def test_discovery_hard_fail_no_persisted_raises():
    """Covers AE10 (part 2): live discovery fails AND nothing persisted → typed
    RadioDirectoryUnavailable."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_STATION_JSON)

    client = _make_client(handler, resolver=_empty_resolver,
                          persist_load=lambda: [])
    with pytest.raises(RadioDirectoryUnavailable):
        await client.search_stations(tag="jazz")
    await client.aclose()


async def test_successful_discovery_persists_last_known_good():
    """A successful discovery persists the reverse-resolved host list."""
    saved = {}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_STATION_JSON)

    client = _make_client(handler, persist_save=lambda hosts: saved.update({"h": hosts}))
    await client.search_stations(tag="jazz")
    assert set(saved["h"]) == set(_POOL.values())
    await client.aclose()


# ── Behavior: click report is fire-and-forget ────────────────────────────────

async def test_click_report_is_fire_and_forget_and_does_not_raise():
    """A failing/slow click must not raise to the caller and must not delay
    resolve. report_click returns immediately; resolve is pure/instant."""
    clicked = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "/json/url/" in request.url.path:
            clicked["n"] += 1
            return httpx.Response(500, text="boom")  # a failing click
        return httpx.Response(200, json=_STATION_JSON)

    client = _make_client(handler)
    # Prime discovery so the click task has a host without racing.
    await client.search_stations(tag="jazz")

    s = Station.from_json(_STATION_JSON[0])
    # resolve is synchronous/pure and never blocks on the click.
    assert client.resolve_play_url(s) == s.play_url
    # report_click returns without awaiting; it must not raise.
    client.report_click(s.stationuuid)
    # Let the background task run and swallow its failure.
    await asyncio.sleep(0)
    await asyncio.sleep(0.05)
    # The click was attempted (>=1; a 5xx rotates across mirrors) and the failure
    # was swallowed — report_click never raised to the caller.
    assert clicked["n"] >= 1
    await client.aclose()


async def test_click_report_empty_uuid_is_noop():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not fetch on empty uuid")

    client = _make_client(handler)
    client.report_click("")  # no crash, no request
    await asyncio.sleep(0)
    await client.aclose()


# ── Behavior: SWR cache ──────────────────────────────────────────────────────

async def test_swr_returns_stale_during_refresh():
    """An expired entry is returned immediately while a single-flight refresh runs
    in the background; the next call sees the refreshed value."""
    versions = [
        [{"name": "jazz", "stationcount": 10}],
        [{"name": "jazz", "stationcount": 99}],
    ]
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "/json/tags" in request.url.path:
            idx = min(calls["n"], len(versions) - 1)
            calls["n"] += 1
            return httpx.Response(200, json=versions[idx])
        return httpx.Response(200, json=_STATION_JSON)

    client = _make_client(handler)

    first = await client.get_tags()
    assert first[0]["stationcount"] == 10

    # Force expiry.
    client._cache["tags"].fetched_at -= 999

    # A second call returns the STALE value immediately and spawns a refresh.
    stale = await client.get_tags()
    assert stale[0]["stationcount"] == 10  # served stale

    # Let the background refresh complete.
    await client._refresh_tasks["tags"]

    fresh = await client.get_tags()
    assert fresh[0]["stationcount"] == 99  # refreshed
    await client.aclose()


async def test_swr_transient_failure_during_refresh_keeps_good_cached_list():
    """A transient failure during a background refresh does NOT evict the good
    cached list (never cache a transient failure as a definitive miss)."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "/json/tags" in request.url.path:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(200, json=[{"name": "jazz", "stationcount": 5}])
            # Every subsequent refresh fails on ALL mirror attempts.
            return httpx.Response(503, text="down")
        return httpx.Response(200, json=_STATION_JSON)

    client = _make_client(handler)
    good = await client.get_tags()
    assert good[0]["stationcount"] == 5

    client._cache["tags"].fetched_at -= 999
    stale = await client.get_tags()  # spawns a doomed refresh
    assert stale[0]["stationcount"] == 5

    await client._refresh_tasks["tags"]  # refresh fails internally, swallowed

    # The good list survived the failed refresh.
    assert client._cache["tags"].value[0]["stationcount"] == 5
    await client.aclose()


async def test_swr_generation_guard_drops_stale_refresh():
    """A refresh that began before invalidate_cache() must not resurrect the
    cleared cache."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"name": "x", "stationcount": 1}])

    client = _make_client(handler)
    await client.get_tags()
    client._cache["tags"].fetched_at -= 999

    # Spawn the refresh, then invalidate before it can write back.
    client._spawn_refresh("tags", lambda: client._request_json("json/tags"))
    client.invalidate_cache()
    await client._refresh_tasks["tags"]

    # The generation-guarded refresh dropped its result; cache stays cleared.
    assert "tags" not in client._cache
    await client.aclose()


# ── F10: completed refresh tasks are pruned; _log_task_exc surfaces exceptions ──


async def test_f10_completed_refresh_task_pruned_from_dict():
    """F10: a completed refresh task is removed from _refresh_tasks so the dict
    can't grow unbounded (one distinct key per never-cleared entry would leak)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"name": "x", "stationcount": 1}])

    client = _make_client(handler)
    await client.get_tags()
    client._cache["tags"].fetched_at -= 999      # force it stale

    client._spawn_refresh("tags", lambda: client._request_json("json/tags"))
    task = client._refresh_tasks["tags"]
    await task
    await asyncio.sleep(0)                        # let the done-callbacks run
    assert "tags" not in client._refresh_tasks, \
        "a completed refresh task must be pruned from _refresh_tasks (F10)"
    await client.aclose()


def test_f10_log_task_exc_surfaces_and_ignores_cancel():
    """The F10 helper logs a task's exception unless cancelled; it never raises."""
    from app.radio.client import _log_task_exc

    class _T:
        def __init__(self, cancelled, exc):
            self._cancelled = cancelled
            self._exc = exc

        def cancelled(self):
            return self._cancelled

        def exception(self):
            return self._exc

    # A cancelled task: no exception() lookup, no raise.
    _log_task_exc(_T(True, None))
    # A clean task: exception() is None → nothing logged, no raise.
    _log_task_exc(_T(False, None))
    # A crashed task: the exception is surfaced (logged) without raising.
    _log_task_exc(_T(False, RuntimeError("boom")))


# ── Behavior: semaphore bounds concurrency ────────────────────────────────────

async def test_semaphore_bounds_concurrency():
    """Concurrent outbound calls never exceed the semaphore limit."""
    max_conc = {"seen": 0}
    current = {"n": 0}
    gate = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        current["n"] += 1
        max_conc["seen"] = max(max_conc["seen"], current["n"])
        # Hold each request until enough have piled up (or a short timeout) so we
        # actually observe concurrency, then release.
        try:
            await asyncio.wait_for(gate.wait(), timeout=0.3)
        except asyncio.TimeoutError:
            pass
        current["n"] -= 1
        return httpx.Response(200, json=_STATION_JSON)

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, timeout=5)

    async def _load():
        return []

    async def _save(hosts):
        pass

    client = RadioBrowserClient(
        http=http, resolver=_fake_resolver, reverse=_fake_reverse,
        persist_load=_load, persist_save=_save, max_concurrency=2,
    )
    # Prime discovery so all the concurrent calls share one established host.
    await client.search_stations(tag="warm")

    async def one(i):
        return await client.search_stations(tag=f"t{i}")

    tasks = [asyncio.create_task(one(i)) for i in range(6)]
    await asyncio.sleep(0.35)
    gate.set()
    await asyncio.gather(*tasks)

    assert max_conc["seen"] <= 2  # semaphore held the line at 2
    await client.aclose()
