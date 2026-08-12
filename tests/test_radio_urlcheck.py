"""Tests for the radio stream-URL SSRF validator (radio plan U2 / R11).

NOTHING hits the network or real DNS: ``socket.getaddrinfo`` is monkeypatched
with a fake resolver (like ``_fake_getaddrinfo`` in ``tests/test_api_admin.py``),
and redirect chains are driven by an ``httpx.MockTransport``.

IP-range discipline / an important gotcha:

The security-correct radio SSRF policy blocks EVERY non-globally-routable
address (``ipaddress.is_private`` — which the stdlib treats as "not is_global").
Python's stdlib classifies the RFC 5737/3849 **documentation** ranges
(192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24, 2001:db8::/32) as ``is_private``
too — they are non-routable — so those ranges are (correctly) BLOCKED by this
validator and can NOT stand in for a "reachable public host" here. For the
"public" fixtures we therefore use well-known globally-routable anycast IPs
(``8.8.8.8``, ``1.1.1.1``, IPv6 ``2606:4700:4700::1111``). These are NOT IPs from
this environment (the hard constraint bars a real routable/LAN IP *from this
box* — e.g. the LAN 192.168.x — never a public anycast resolver). The real
special ranges (127.0.0.1, 169.254.1.1, 10.0.0.1, 192.168.1.1, ::1, fc00::1)
appear ONLY as the blocked inputs under test.
"""
from __future__ import annotations

import httpx
import pytest

from app.radio.urlcheck import (
    MAX_REDIRECT_HOPS,
    RadioUrlBlocked,
    resolve_and_validate,
    validate_station_host,
)

# ── globally-routable ("public") IPs — NOT from this environment ─────────────
_PUBLIC_V4 = "8.8.8.8"           # Google public DNS anycast
_PUBLIC_V4_B = "1.1.1.1"         # Cloudflare public DNS anycast
_PUBLIC_V4_C = "9.9.9.9"         # Quad9 public DNS anycast
_PUBLIC_V6 = "2606:4700:4700::1111"  # Cloudflare public DNS anycast (v6)

# ── real special-range IPs (BLOCKED inputs only) ─────────────────────────────
_LOOPBACK_V4 = "127.0.0.1"
_LINKLOCAL_V4 = "169.254.1.1"
_PRIVATE_10 = "10.0.0.1"
_PRIVATE_192 = "192.168.1.1"
_LOOPBACK_V6 = "::1"
_ULA_V6 = "fc00::1"


# ── fake DNS ─────────────────────────────────────────────────────────────────

def _fake_getaddrinfo(mapping):
    """Return a ``socket.getaddrinfo`` stand-in resolving hosts per ``mapping``.

    ``mapping`` maps a hostname -> a single IP string or a list of IP strings.
    A default key ``"*"`` applies to any unlisted host. A host mapped to None
    (or absent with no default) raises ``OSError`` (unresolvable).
    """
    def _resolver(host, port, *a, **kw):
        ips = mapping.get(host, mapping.get("*"))
        if ips is None:
            raise OSError(f"name resolution failed for {host!r}")
        if isinstance(ips, str):
            ips = [ips]
        infos = []
        for ip in ips:
            family = 10 if ":" in ip else 2  # AF_INET6 vs AF_INET
            infos.append((family, 1, 6, "", (ip, 0)))
        return infos
    return _resolver


def _patch_dns(monkeypatch, mapping):
    monkeypatch.setattr("socket.getaddrinfo", _fake_getaddrinfo(mapping))


def _allow_private(monkeypatch, value):
    # The whole point of U2: this flag must be IGNORED. We patch it True in the
    # block tests to prove it makes no difference.
    monkeypatch.setattr("app.config.settings.allow_private_sources", value,
                        raising=False)


# ── redirect transport helpers ───────────────────────────────────────────────

class _Recorder:
    def __init__(self):
        self.requests: list[httpx.Request] = []


def _redirect_client(handler):
    """An httpx client over a MockTransport with follow_redirects=False.

    Mirrors how ``resolve_and_validate`` must be called: a redirect-following
    client would defeat per-hop validation, so the injected client is explicitly
    non-following (and we assert that below).
    """
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport, follow_redirects=False,
                             timeout=5)


# ══ single-URL validator ══════════════════════════════════════════════════════

@pytest.mark.parametrize(
    "blocked_ip",
    [_LOOPBACK_V4, _LINKLOCAL_V4, _PRIVATE_10, _PRIVATE_192, _LOOPBACK_V6, _ULA_V6],
    ids=["loopback-v4", "linklocal-v4", "private-10", "private-192",
         "loopback-v6", "ula-v6"],
)
async def test_blocked_ranges_rejected_even_with_allow_private(monkeypatch, blocked_ip):
    """AE12: loopback / link-local / private (v4+v6) are rejected for radio EVEN
    with allow_private_sources=True — the admin flag is not honored here."""
    _allow_private(monkeypatch, True)
    _patch_dns(monkeypatch, {"radio.example.invalid": blocked_ip})
    with pytest.raises(RadioUrlBlocked):
        await validate_station_host("http://radio.example.invalid/stream")


async def test_literal_blocked_ip_url_rejected(monkeypatch):
    """A literal blocked-IP URL (no DNS) is rejected too."""
    _allow_private(monkeypatch, True)
    # getaddrinfo should not even be consulted for a literal IP, but patch it to
    # a public value to prove the literal path is what blocks.
    _patch_dns(monkeypatch, {"*": _PUBLIC_V4})
    with pytest.raises(RadioUrlBlocked):
        await validate_station_host(f"http://{_PRIVATE_192}:8000/stream")


async def test_public_http_and_https_pass(monkeypatch):
    """Happy path: normal public http and https station URLs pass."""
    _patch_dns(monkeypatch, {
        "http-radio.example.invalid": _PUBLIC_V4,
        "https-radio.example.invalid": _PUBLIC_V4_B,
    })
    await validate_station_host("http://http-radio.example.invalid/stream")
    await validate_station_host("https://https-radio.example.invalid/stream")


async def test_public_ipv6_passes(monkeypatch):
    _patch_dns(monkeypatch, {"v6-radio.example.invalid": _PUBLIC_V6})
    await validate_station_host("http://v6-radio.example.invalid/stream")


async def test_unresolvable_host_fails_closed(monkeypatch):
    """An unresolvable host is rejected, not passed through (fail-closed)."""
    _patch_dns(monkeypatch, {})  # any host -> OSError
    with pytest.raises(RadioUrlBlocked):
        await validate_station_host("http://nope.example.invalid/stream")


async def test_multi_address_one_private_rejected(monkeypatch):
    """A host resolving to several addresses is rejected if ANY is private."""
    _allow_private(monkeypatch, True)
    _patch_dns(monkeypatch, {
        "mixed.example.invalid": [_PUBLIC_V4, _PRIVATE_10, _PUBLIC_V4_B],
    })
    with pytest.raises(RadioUrlBlocked):
        await validate_station_host("http://mixed.example.invalid/stream")


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "gopher://evil.example.invalid/",
    "ftp://evil.example.invalid/x",
    "ws://evil.example.invalid/x",
    "//evil.example.invalid/x",   # scheme-relative -> empty scheme
])
async def test_non_http_scheme_rejected(monkeypatch, url):
    """Any non-http(s) scheme is rejected."""
    _patch_dns(monkeypatch, {"*": _PUBLIC_V4})
    with pytest.raises(RadioUrlBlocked):
        await validate_station_host(url)


async def test_empty_host_rejected(monkeypatch):
    _patch_dns(monkeypatch, {"*": _PUBLIC_V4})
    with pytest.raises(RadioUrlBlocked):
        await validate_station_host("http:///just-a-path")


# ══ redirect follower (SEC-002) ═══════════════════════════════════════════════

async def test_public_no_redirect_returns_final(monkeypatch):
    """Happy: a public URL with no redirect resolves to itself."""
    _patch_dns(monkeypatch, {"radio.example.invalid": _PUBLIC_V4})

    def handler(request):
        return httpx.Response(200, text="ok")

    async with _redirect_client(handler) as client:
        final = await resolve_and_validate(
            "http://radio.example.invalid/stream", client=client)
    assert final == "http://radio.example.invalid/stream"


async def test_public_to_public_redirect_resolves_final(monkeypatch):
    """Happy: a public->public redirect chain resolves to the final public URL."""
    _patch_dns(monkeypatch, {
        "start.example.invalid": _PUBLIC_V4,
        "cdn.example.invalid": _PUBLIC_V4_B,
    })

    def handler(request):
        if request.url.host == "start.example.invalid":
            return httpx.Response(
                302, headers={"location": "http://cdn.example.invalid/final.mp3"})
        return httpx.Response(200, text="stream")

    async with _redirect_client(handler) as client:
        final = await resolve_and_validate(
            "http://start.example.invalid/stream", client=client)
    assert final == "http://cdn.example.invalid/final.mp3"


async def test_public_redirect_to_loopback_rejected(monkeypatch):
    """SEC-002: a public URL that 302s to loopback is rejected — the hop is
    re-validated and the loopback target never gets fetched."""
    _patch_dns(monkeypatch, {
        "start.example.invalid": _PUBLIC_V4,
        # the redirect target is a literal loopback IP host
    })
    rec = _Recorder()

    def handler(request):
        rec.requests.append(request)
        if request.url.host == "start.example.invalid":
            return httpx.Response(
                302, headers={"location": f"http://{_LOOPBACK_V4}/admin"})
        # Should NEVER be reached for the loopback target.
        raise AssertionError(f"blocked target was fetched: {request.url}")

    async with _redirect_client(handler) as client:
        with pytest.raises(RadioUrlBlocked):
            await resolve_and_validate(
                "http://start.example.invalid/stream", client=client)
    # The loopback hop was validated (and rejected) BEFORE any fetch of it.
    assert all(r.url.host != _LOOPBACK_V4 for r in rec.requests)


async def test_resolve_and_validate_uses_streaming_not_eager_read(monkeypatch):
    """resolve_and_validate must probe with client.stream() (returns on headers,
    body never read), NOT client.request()/get() which reads the body eagerly
    and blocks forever on a live, endless radio stream — a healthy stream keeps
    sending, so even the read timeout never fires (rig-caught P0, 2026-08-11).
    A fake client records which method the resolver used."""
    _patch_dns(monkeypatch, {"stream.example.invalid": _PUBLIC_V4})
    calls = {"stream": 0, "eager": 0}

    class _FakeStreamResp:
        url = httpx.URL("http://stream.example.invalid/live")
        is_redirect = False
        headers: dict = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _FakeClient:
        def stream(self, method, url):
            calls["stream"] += 1
            return _FakeStreamResp()

        async def request(self, *a, **k):  # the eager path that hangs on a stream
            calls["eager"] += 1
            raise AssertionError("resolve_and_validate used eager request()")

        async def get(self, *a, **k):
            calls["eager"] += 1
            raise AssertionError("resolve_and_validate used eager get()")

    out = await resolve_and_validate(
        "http://stream.example.invalid/live", client=_FakeClient())
    assert out == "http://stream.example.invalid/live"
    assert calls["stream"] == 1, "must probe the hop with a streaming GET"
    assert calls["eager"] == 0, "must never eagerly read the body"


async def test_public_redirect_to_private_via_dns_rejected(monkeypatch):
    """SEC-002 variant: the redirect Location is a hostname that RESOLVES to a
    private IP -> still rejected (host, not just literal-IP, is re-validated)."""
    _allow_private(monkeypatch, True)
    _patch_dns(monkeypatch, {
        "start.example.invalid": _PUBLIC_V4,
        "internal.example.invalid": _PRIVATE_192,
    })

    def handler(request):
        if request.url.host == "start.example.invalid":
            return httpx.Response(
                301, headers={"location": "http://internal.example.invalid/x"})
        raise AssertionError("private target was fetched")

    async with _redirect_client(handler) as client:
        with pytest.raises(RadioUrlBlocked):
            await resolve_and_validate(
                "http://start.example.invalid/stream", client=client)


async def test_uses_follow_redirects_false(monkeypatch):
    """The follower must NOT rely on the client auto-following: with a
    non-following client it still walks the chain (proving manual following),
    and it sees each hop as a distinct request."""
    _patch_dns(monkeypatch, {
        "a.example.invalid": _PUBLIC_V4,
        "b.example.invalid": _PUBLIC_V4_B,
        "c.example.invalid": _PUBLIC_V4_C,
    })
    seen_hosts = []

    def handler(request):
        seen_hosts.append(request.url.host)
        if request.url.host == "a.example.invalid":
            return httpx.Response(302, headers={"location": "http://b.example.invalid/2"})
        if request.url.host == "b.example.invalid":
            return httpx.Response(302, headers={"location": "http://c.example.invalid/3"})
        return httpx.Response(200, text="final")

    async with _redirect_client(handler) as client:
        # sanity: the injected client is genuinely non-following
        assert client.follow_redirects is False
        final = await resolve_and_validate(
            "http://a.example.invalid/1", client=client)
    assert final == "http://c.example.invalid/3"
    # Each hop was a separate request the follower made itself.
    assert seen_hosts == ["a.example.invalid", "b.example.invalid", "c.example.invalid"]


async def test_relative_redirect_location_resolved(monkeypatch):
    """A relative Location header is resolved against the current URL and
    re-validated on the same (public) host."""
    _patch_dns(monkeypatch, {"radio.example.invalid": _PUBLIC_V4})

    def handler(request):
        if request.url.path == "/stream":
            return httpx.Response(302, headers={"location": "/live.mp3"})
        return httpx.Response(200, text="ok")

    async with _redirect_client(handler) as client:
        final = await resolve_and_validate(
            "http://radio.example.invalid/stream", client=client)
    assert final == "http://radio.example.invalid/live.mp3"


async def test_redirect_loop_bounded_and_rejected(monkeypatch):
    """A redirect loop / excessive hops is bounded by MAX_REDIRECT_HOPS and
    rejected rather than spinning forever."""
    _patch_dns(monkeypatch, {"loop.example.invalid": _PUBLIC_V4})
    hops = []

    def handler(request):
        hops.append(request.url)
        # Always redirect back to the same public host -> infinite loop if unbounded.
        return httpx.Response(302, headers={"location": "http://loop.example.invalid/next"})

    async with _redirect_client(handler) as client:
        with pytest.raises(RadioUrlBlocked):
            await resolve_and_validate(
                "http://loop.example.invalid/start", client=client)
    # Bounded: no more than MAX_REDIRECT_HOPS + 1 requests were made.
    assert len(hops) <= MAX_REDIRECT_HOPS + 1


async def test_resolve_and_validate_rejects_blocked_entry_before_fetch(monkeypatch):
    """resolve_and_validate validates the ENTRY url before any fetch: a blocked
    start URL never touches the transport."""
    _allow_private(monkeypatch, True)
    _patch_dns(monkeypatch, {"radio.example.invalid": _LOOPBACK_V4})

    def handler(request):
        raise AssertionError("blocked entry URL was fetched")

    async with _redirect_client(handler) as client:
        with pytest.raises(RadioUrlBlocked):
            await resolve_and_validate(
                "http://radio.example.invalid/stream", client=client)


async def test_default_client_used_when_none(monkeypatch):
    """When no client is injected, resolve_and_validate still rejects a blocked
    entry URL without ever needing the network (validation precedes any fetch)."""
    _allow_private(monkeypatch, True)
    _patch_dns(monkeypatch, {"radio.example.invalid": _PRIVATE_10})
    with pytest.raises(RadioUrlBlocked):
        # No client -> it would build its own, but the entry validation rejects
        # first, so no socket is opened.
        await resolve_and_validate("http://radio.example.invalid/stream")


# ══ F17 — unspecified / multicast / reserved ranges are all blocked ═══════════

# Real special-range IPs used ONLY as blocked inputs (never from this box).
_UNSPEC_V4 = "0.0.0.0"
_UNSPEC_V6 = "::"
_MULTICAST_V4 = "224.0.0.1"
_MULTICAST_V6 = "ff02::1"
_RESERVED_V4 = "240.0.0.1"        # 240/4 reserved (RFC 1112 §4)


@pytest.mark.parametrize(
    "blocked_ip",
    [_UNSPEC_V4, _UNSPEC_V6, _MULTICAST_V4, _MULTICAST_V6, _RESERVED_V4],
    ids=["unspecified-v4", "unspecified-v6", "multicast-v4", "multicast-v6",
         "reserved-v4"],
)
async def test_f17_unspecified_multicast_reserved_blocked(monkeypatch, blocked_ip):
    """F17: the unspecified address (0.0.0.0 / ::), multicast (224.0.0.1 /
    ff02::1) and reserved (240.0.0.1) ranges are all blocked for radio — even
    with allow_private_sources=True."""
    _allow_private(monkeypatch, True)
    _patch_dns(monkeypatch, {"radio.example.invalid": blocked_ip})
    with pytest.raises(RadioUrlBlocked):
        await validate_station_host("http://radio.example.invalid/stream")


async def test_f17_redirect_location_non_http_scheme_rejected_never_fetched(
        monkeypatch):
    """F17: a 302 whose Location is a non-http scheme (file:///…) is rejected as a
    disallowed scheme on re-validation — and the target is NEVER fetched."""
    _patch_dns(monkeypatch, {"start.example.invalid": _PUBLIC_V4})
    rec = _Recorder()

    def handler(request):
        rec.requests.append(request)
        if request.url.scheme in ("http", "https") \
                and request.url.host == "start.example.invalid":
            return httpx.Response(
                302, headers={"location": "file:///etc/passwd"})
        raise AssertionError(f"non-http redirect target was fetched: {request.url}")

    async with _redirect_client(handler) as client:
        with pytest.raises(RadioUrlBlocked):
            await resolve_and_validate(
                "http://start.example.invalid/stream", client=client)
    # Only the initial public GET happened; the file:// target was never fetched.
    assert all(r.url.scheme in ("http", "https") for r in rec.requests)
