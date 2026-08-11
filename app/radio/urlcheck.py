"""Radio stream-URL SSRF validator (U2 / R11).

A resolved internet-radio station URL comes from Radio Browser — an anonymous,
open, community-edited directory. It is **untrusted third-party input**, so it
must be validated as an SSRF risk *before the jukebox process ever fetches it*.

This mirrors the shape of ``app/api/admin.py``'s ``_validate_source_url`` (off-loop
``getaddrinfo``, check every resolved address, fail closed on an unresolvable
host) but applies a **stricter radio policy**:

1. **Always block** loopback, link-local, **and private / unique-local ranges,
   reserved, multicast, and the unspecified address** — independent of
   ``settings.allow_private_sources``. The admin flag exists so a self-hosted
   server on the LAN can be added as a *deliberate* source; a station URL is an
   arbitrary public host chosen by strangers, so the LAN-first default is wrong
   here and is NOT honored.
2. **Reject any scheme that is not ``http`` or ``https``** (``file://``,
   ``gopher://``, etc.). Station streams legitimately use plain ``http`` (many
   ICY servers are http-only), so — unlike the Radio Browser *API* host, which
   U1 pins to https — we allow http **and** https here, but nothing else.
3. **Resolve the host off the event loop**; check *every* returned address; fail
   **closed** (raise ``RadioUrlBlocked``) if the host does not resolve.
4. **Follow redirects MANUALLY, re-validating every hop (SEC-002).** A
   pre-connect check of only the first host is insufficient: the downstream
   fetchers follow redirects natively — ``app/output/flow.py``'s ``_feed_source``
   uses ``follow_redirects=True``, and GStreamer ``souphttpsrc`` / ``ffmpeg -i``
   follow redirects themselves. So ``resolve_and_validate`` uses an httpx client
   configured ``follow_redirects=False``, validates each ``Location`` host
   against the block policy on every hop (bounded to ``MAX_REDIRECT_HOPS``), and
   returns only the **fully-resolved final URL**. Callers hand *that* URL to the
   backend / transcode-proxy — never the original.

**Residual (documented, same as the admin path):** a DNS-rebind TOCTOU window
remains — DNS could change between this check and the fetcher's own resolution,
because the fetcher re-resolves the hostname rather than dialing the pinned IP.
Closing it fully would require pinning the connection to the resolved IP (larger
surgery). The manual no-follow-redirect posture is the primary defense against
the redirect-based bypass; per-hop re-validation is what U2 adds over the admin
validator.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import httpx

__all__ = [
    "RadioUrlBlocked",
    "validate_station_host",
    "resolve_and_validate",
    "MAX_REDIRECT_HOPS",
]

# Bound on how many redirect hops we will follow+validate before giving up. A
# station that needs more than a handful of hops is either broken or hostile; an
# unbounded follow would let a redirect loop spin forever.
MAX_REDIRECT_HOPS = 5

# Allowed URL schemes for a station STREAM (the API host is https-only, enforced
# separately in U1). http is intentionally allowed — many ICY servers are
# http-only.
_ALLOWED_SCHEMES = frozenset({"http", "https"})


class RadioUrlBlocked(Exception):
    """A station URL was rejected by the radio SSRF policy.

    Raised for a disallowed scheme, an unresolvable host (fail-closed), any
    resolved address in a blocked range (loopback / link-local / private /
    reserved / multicast / unspecified), or a redirect chain that hops to a
    blocked host or exceeds ``MAX_REDIRECT_HOPS``.
    """


def _blocked_reason(ip: ipaddress._BaseAddress) -> str | None:
    """Return a human reason string if ``ip`` is in a blocked range, else None.

    Radio policy blocks EVERY non-global range — loopback, link-local, private
    (RFC-1918 + IPv6 ULA ``fc00::/7``), reserved, multicast, and the unspecified
    address (``0.0.0.0`` / ``::``) — flag-independent.
    """
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local"
    if ip.is_unspecified:
        return "unspecified"
    if ip.is_multicast:
        return "multicast"
    # is_private in the stdlib covers RFC-1918 (10/8, 172.16/12, 192.168/16),
    # IPv6 ULA (fc00::/7), and also loopback/link-local/unspecified — but we name
    # those separately above for a clearer message.
    if ip.is_private:
        return "private/internal"
    if ip.is_reserved:
        return "reserved"
    return None


def _parse_scheme_host(url: str) -> tuple[str, str]:
    """Return ``(scheme, host)`` for ``url``; raise ``RadioUrlBlocked`` if the
    scheme is not http(s) or the host is unparseable/empty."""
    try:
        parsed = urlparse(url or "")
    except Exception as exc:  # pragma: no cover - urlparse is very tolerant
        raise RadioUrlBlocked(f"Could not parse the station URL: {url!r}") from exc

    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise RadioUrlBlocked(
            f"Scheme {scheme or '(none)'!r} is not allowed for a radio stream "
            "(only http/https).")

    host = parsed.hostname or ""
    if not host:
        raise RadioUrlBlocked("Station URL has no host.")
    return scheme, host


async def _resolve_addrs(host: str) -> list[str]:
    """Resolve ``host`` to a list of IP strings, OFF the event loop.

    A literal-IP host returns just itself. Fails CLOSED: an unresolvable host
    raises ``RadioUrlBlocked`` rather than falling through.
    """
    try:
        ipaddress.ip_address(host)  # literal IP?
        return [host]
    except ValueError:
        pass

    try:
        loop = asyncio.get_running_loop()
        # getaddrinfo is blocking (DNS) — run it in the default executor so a
        # slow resolver can't stall the event loop.
        infos = await loop.run_in_executor(None, socket.getaddrinfo, host, None)
    except OSError as exc:
        raise RadioUrlBlocked(
            f"Could not resolve station host {host!r} (blocked, fail-closed)."
        ) from exc

    addrs = sorted({info[4][0] for info in infos})
    if not addrs:
        raise RadioUrlBlocked(
            f"Could not resolve station host {host!r} (blocked, fail-closed).")
    return addrs


async def validate_station_host(url: str) -> None:
    """Validate a SINGLE station URL's scheme + resolved host (no network fetch).

    Raises ``RadioUrlBlocked`` if the scheme is not http(s), the host does not
    resolve (fail-closed), or ANY resolved address is in a blocked range
    (loopback / link-local / private / reserved / multicast / unspecified) —
    regardless of ``settings.allow_private_sources``.

    This is the pure, no-fetch validator. It is used both standalone (e.g. by a
    caller that already has a redirect-resolved URL) and as the per-hop check
    inside ``resolve_and_validate``. It does NOT follow redirects — use
    ``resolve_and_validate`` for the full untrusted-fetch guard.
    """
    _scheme, host = _parse_scheme_host(url)
    addrs = await _resolve_addrs(host)
    for raw in addrs:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            # A getaddrinfo result we can't parse as an IP is not something we
            # can prove is safe — fail closed on it.
            raise RadioUrlBlocked(
                f"Station host {host!r} resolved to an unparseable address "
                f"{raw!r} (blocked, fail-closed).")
        reason = _blocked_reason(ip)
        if reason is not None:
            raise RadioUrlBlocked(
                f"Station host {host!r} resolves to a blocked address {raw} "
                f"({reason}). Radio URLs may not point at internal networks.")


async def resolve_and_validate(
    url: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> str:
    """Validate ``url`` AND every redirect hop, returning the final resolved URL.

    The load-bearing SSRF guard for radio (SEC-002). Because the downstream
    fetchers follow redirects natively, validating only the first host is
    insufficient — a public host could 302 to ``127.0.0.1``. This function:

    1. Validates the starting URL with :func:`validate_station_host`.
    2. Issues a request with ``follow_redirects=False`` and, for each 3xx
       ``Location``, re-validates the redirect target's host BEFORE following it,
       up to :data:`MAX_REDIRECT_HOPS` hops.
    3. Returns the fully-resolved FINAL URL (the first non-redirect response's
       URL) — the value the caller hands to the backend / transcode-proxy.

    Any hop that resolves to a blocked address, a disallowed scheme, or an
    unresolvable host raises ``RadioUrlBlocked`` and the whole chain is rejected.
    Exceeding ``MAX_REDIRECT_HOPS`` (including a redirect loop) is rejected.

    ``client`` is injectable so tests can pass an ``httpx.AsyncClient`` built on
    an ``httpx.MockTransport`` (no real network / DNS). When omitted, a
    short-lived client with ``follow_redirects=False`` is created and closed
    here. A caller-supplied client is NOT closed and MUST itself be configured
    ``follow_redirects=False`` — a redirect-following client would defeat the
    per-hop re-validation.
    """
    # Validate the entry point before any fetch.
    await validate_station_host(url)

    owns_client = client is None
    if client is None:
        # follow_redirects=False is the whole point — we follow manually so we
        # can re-validate each hop's host.
        client = httpx.AsyncClient(follow_redirects=False, timeout=10.0)

    current = url
    try:
        for _hop in range(MAX_REDIRECT_HOPS + 1):
            # A HEAD would be cheaper, but many ICY / streaming servers mishandle
            # HEAD (405 / hang) and some don't emit redirects for it. Use GET and
            # never read the body — httpx does not stream the body until awaited.
            resp = await client.request("GET", current)
            try:
                if not resp.is_redirect:
                    # Final hop. Its host was validated on the way in (either the
                    # entry validation or the previous hop's re-validation).
                    return str(resp.url)

                location = resp.headers.get("location")
                if not location:
                    # A 3xx with no Location — nothing to follow; treat the
                    # current (already-validated) URL as final.
                    return str(resp.url)

                # Resolve relative Locations against the current URL.
                next_url = urljoin(str(resp.url), location)
            finally:
                await resp.aclose()

            # RE-VALIDATE the redirect target BEFORE following it. This is the
            # SEC-002 guarantee: a public->private redirect is caught here.
            await validate_station_host(next_url)
            current = next_url

        # Fell out of the loop => too many hops (or a redirect loop).
        raise RadioUrlBlocked(
            f"Station URL exceeded {MAX_REDIRECT_HOPS} redirect hops "
            "(possible redirect loop); rejected.")
    except httpx.HTTPError as exc:
        # A transport-level failure while probing redirects is not proof of
        # safety — fail closed.
        raise RadioUrlBlocked(
            f"Could not resolve the station redirect chain: {exc}") from exc
    finally:
        if owns_client:
            await client.aclose()
