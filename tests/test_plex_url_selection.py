"""Plex discovery URL selection (fresh-install audit F9, 2026-08-06).

First direct unit coverage for _best_url / _ordered_candidates /
_reachable_url — previously only ever mocked at the discover_servers level.

Live characterization (rig, 2026-08-06) that shaped these rules: the NAS —
a bridge-networked TrueNAS app — advertises ONLY its container IP
(172.16.x.x) as the local connection; the real LAN address appears nowhere
in the plex.tv payload, so every client-side local candidate is dead and
discovery lands on the WAN hairpin. Hence: a previously-persisted URL is
probed FIRST (re-auth never downgrades a working URL), raw-http candidates
rescue the DNS-rebind population (HTTPS-first, API-usable bar), and the
bridged-PMS population is fixed server-side via Plex's Custom server access
URLs (documented in the install docs).

These tests live in their own file: tests/test_auth.py hangs at run time on
the Windows dev box (pre-existing environment issue; it runs on Linux).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.plex.auth import _best_url, _ordered_candidates, _reachable_url

# Synthetic connection entries (fixture convention: 192.168.1.x, never real
# deploy IPs). Shapes mirror plex.tv/api/v2/resources connections.
LOCAL = {"uri": "https://192-168-1-50.hash.plex.direct:32400",
         "address": "192.168.1.50", "port": 32400, "local": True, "relay": False}
REMOTE = {"uri": "https://203-0-113-9.hash.plex.direct:32401",
          "address": "203.0.113.9", "port": 32401, "local": False, "relay": False}
RELAY = {"uri": "https://relay-host.hash.plex.direct:8443",
         "address": "198.51.100.7", "port": 8443, "local": False, "relay": True}
CONNS = [LOCAL, REMOTE, RELAY]

RAW_URI = "http://192.168.1.50:32400"
PINNED = "http://192.168.1.50:32400"  # a hand-pinned, previously-working URL


def _client(responses):
    """Fake httpx client: responses maps probed base URI → status int or
    Exception; unlisted URIs raise (unreachable)."""
    async def _get(url, timeout=None):
        base = url.rsplit("/identity", 1)[0]
        v = responses.get(base)
        if v is None:
            raise ConnectionError(f"unreachable: {base}")
        if isinstance(v, Exception):
            raise v
        resp = MagicMock()
        resp.status_code = v
        return resp
    c = MagicMock()
    c.get = AsyncMock(side_effect=_get)
    return c


# ── _ordered_candidates ──────────────────────────────────────────────────────

def test_owned_order_https_first_raw_before_relay():
    out = _ordered_candidates(CONNS, owned=True)
    uris = [u for u, _ in out]
    # local https → remote https → raw http (synthesized) → relay
    assert uris == [LOCAL["uri"], REMOTE["uri"], RAW_URI, RELAY["uri"]]
    # Only the synthesized candidate is flagged raw.
    assert [(u, r) for u, r in out if r] == [(RAW_URI, True)]


def test_owned_preferred_url_probes_first():
    out = _ordered_candidates(CONNS, owned=True, preferred="http://192.168.1.77:32400")
    assert out[0] == ("http://192.168.1.77:32400", False)


def test_owned_preferred_dedupes_against_synthesized_raw():
    out = _ordered_candidates(CONNS, owned=True, preferred=PINNED)
    uris = [u for u, _ in out]
    assert uris[0] == PINNED
    assert uris.count(PINNED) == 1  # raw synthesis of the same URI de-duped


def test_shared_order_unchanged_no_raw_synthesis():
    out = _ordered_candidates(CONNS, owned=False)
    uris = [u for u, _ in out]
    # remote → relay → local last; someone else's LAN gets no raw candidates
    assert uris == [REMOTE["uri"], RELAY["uri"], LOCAL["uri"]]
    assert all(not raw for _, raw in out)


# ── _reachable_url ───────────────────────────────────────────────────────────

async def test_local_https_answering_wins():
    client = _client({LOCAL["uri"]: 200, REMOTE["uri"]: 200, RAW_URI: 200})
    assert await _reachable_url(CONNS, True, client) == LOCAL["uri"]


async def test_local_https_never_loses_to_raw_http():
    # Security review: the plaintext path must never win while HTTPS works.
    client = _client({LOCAL["uri"]: 401, RAW_URI: 200})
    assert await _reachable_url(CONNS, True, client) == LOCAL["uri"]


async def test_dns_rebind_shape_raw_http_rescues():
    # local plex.direct dead (rebind protection), remote dead, raw answers.
    client = _client({RAW_URI: 200})
    assert await _reachable_url(CONNS, True, client) == RAW_URI


async def test_raw_http_needs_api_usable_response():
    # 'Secure connections: Required' servers answer HTTP probes with an error;
    # persisting the raw URL would be worse than the hairpin — skip to relay.
    client = _client({RAW_URI: 426, RELAY["uri"]: 200})
    assert await _reachable_url(CONNS, True, client) == RELAY["uri"]


async def test_preferred_known_url_wins_over_fresh_local():
    # Re-auth stability: the persisted (hand-pinned) URL answers → kept, even
    # though the payload's local candidate also answers.
    client = _client({PINNED: 200, LOCAL["uri"]: 200})
    conns = [dict(LOCAL, address="172.16.5.9",
                  uri="https://172-16-5-9.hash.plex.direct:32400"), REMOTE, RELAY]
    assert await _reachable_url(conns, True, client, preferred=PINNED) == PINNED


async def test_nothing_answers_is_a_typed_none_not_a_silent_pick():
    # Source-health plan U2: when no candidate answers the probe says so
    # (None) instead of handing back the static best pick as if it were
    # reachable. discover_servers turns that into an "unreachable" outcome
    # with the saved URL kept (below); the static pick is only for a server
    # that was never saved.
    client = _client({})
    assert await _reachable_url(CONNS, True, client) is None
    assert await _reachable_url(CONNS, False, client) is None


async def test_shared_preferred_known_url_probes_first_and_wins():
    # A shared server's pinned URL is probed first too (known_urls is passed
    # for owned and shared alike).
    out = _ordered_candidates(CONNS, owned=False, preferred=PINNED)
    assert out[0] == (PINNED, False)
    client = _client({PINNED: 200, REMOTE["uri"]: 200})
    assert await _reachable_url(CONNS, False, client, preferred=PINNED) == PINNED


# ── discover_servers: the typed outcome ──────────────────────────────────────

PLEXTV = "https://plex.tv/api/v2/resources"
PLEXTV_USER = "https://plex.tv/api/v2/user"


def _resource(mid, conns, owned=True):
    return {"provides": "server", "clientIdentifier": mid, "name": mid, "owned": owned,
            "accessToken": f"tok-{mid}", "connections": conns}


def _plextv(respx_mock, *resources):
    import httpx
    respx_mock.get(PLEXTV).mock(return_value=httpx.Response(200, json=list(resources)))
    respx_mock.get(PLEXTV_USER).mock(return_value=httpx.Response(200, json={"title": "me"}))


async def test_daily_refresh_keeps_the_pinned_owned_url_when_it_still_answers(respx_mock):
    import httpx
    from app.plex.auth import discover_servers
    _plextv(respx_mock, _resource("A", CONNS))
    for uri in (PINNED, LOCAL["uri"], REMOTE["uri"], RAW_URI, RELAY["uri"]):
        respx_mock.get(f"{uri}/identity").mock(return_value=httpx.Response(200))
    out = await discover_servers("acct", "cid", known_urls={"A": PINNED})
    assert out[0]["server_url"] == PINNED
    assert out[0]["probe"] == "reachable"


async def test_refresh_falls_back_through_best_url_order_when_the_pinned_url_fails(respx_mock):
    import httpx
    from app.plex.auth import discover_servers
    pinned = "http://192.168.1.77:32400"
    _plextv(respx_mock, _resource("A", CONNS))
    respx_mock.get(f"{pinned}/identity").mock(side_effect=httpx.ConnectError("down"))
    respx_mock.get(f"{LOCAL['uri']}/identity").mock(side_effect=httpx.ConnectError("down"))
    respx_mock.get(f"{REMOTE['uri']}/identity").mock(return_value=httpx.Response(200))
    out = await discover_servers("acct", "cid", known_urls={"A": pinned})
    assert out[0]["server_url"] == REMOTE["uri"]
    assert out[0]["probe"] == "reachable"


async def test_nothing_answers_keeps_saved_url_with_unreachable_outcome(respx_mock):
    import httpx
    from app.plex.auth import discover_servers
    pinned = "http://192.168.1.77:32400"
    _plextv(respx_mock, _resource("A", CONNS), _resource("S", [REMOTE], owned=False))
    # Registered AFTER the plex.tv routes: respx matches in order, so this
    # catch-all only swallows the /identity probes.
    respx_mock.route(host__regex=r".*").mock(side_effect=httpx.ConnectError("down"))
    out = {s["machine_id"]: s for s in await discover_servers(
        "acct", "cid", known_urls={"A": pinned, "S": "https://s.example.invalid:32400"})}
    assert out["A"] == dict(out["A"], server_url=pinned, probe="unreachable")
    assert out["S"]["server_url"] == "https://s.example.invalid:32400"
    assert out["S"]["probe"] == "unreachable"


async def test_nothing_answers_and_never_saved_falls_back_to_static_pick(respx_mock):
    import httpx
    from app.plex.auth import discover_servers
    _plextv(respx_mock, _resource("A", CONNS))
    respx_mock.route(host__regex=r".*").mock(side_effect=httpx.ConnectError("down"))
    out = await discover_servers("acct", "cid")
    assert out[0]["server_url"] == _best_url(CONNS, owned=True)
    assert out[0]["probe"] == "unreachable"


async def test_empty_connections_resource_is_listed_not_dropped(respx_mock):
    from app.plex.auth import discover_servers
    _plextv(respx_mock, _resource("A", []))
    out = await discover_servers("acct", "cid", known_urls={"A": PINNED})
    assert out == [dict(out[0], server_url=PINNED, probe="unreachable")]
    out = await discover_servers("acct", "cid")
    assert out[0]["server_url"] == "" and out[0]["probe"] == "unreachable"


# ── the characterized bridged-PMS shape (documented limit) ───────────────────

async def test_bridged_pms_lands_on_wan_hairpin_not_relay():
    # Only local connection is the container IP (unreachable); the WAN remote
    # answers via hairpin NAT. Discovery must pick the remote — not the relay,
    # and must not invent an unreachable raw candidate as the answer.
    bridged_local = {"uri": "https://172-16-5-9.hash.plex.direct:32400",
                     "address": "172.16.5.9", "port": 32400,
                     "local": True, "relay": False}
    conns = [bridged_local, REMOTE, RELAY]
    client = _client({REMOTE["uri"]: 200, RELAY["uri"]: 200})
    assert await _reachable_url(conns, True, client) == REMOTE["uri"]
