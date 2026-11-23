"""Plex PIN-based OAuth flow.

Flow:
  1. generate_pin()  → {id, code, auth_url}  (show auth_url to admin)
  2. poll_pin(id)    → token | None          (poll until non-None, max 5 min)
  3. Store token via database.set_plex_config()
"""

import asyncio
import logging
import uuid

import httpx

_log = logging.getLogger(__name__)

PLEX_TV = "https://plex.tv/api/v2"
POLL_INTERVAL = 3       # seconds between polls
POLL_TIMEOUT = 300      # 5 minutes


class PlexAuthError(Exception):
    pass


def _default_client_id() -> str:
    return str(uuid.uuid4())


def _headers(client_id: str) -> dict:
    return {
        "X-Plex-Product": "Jukeplox",
        "X-Plex-Version": "1.0",
        "X-Plex-Platform": "Linux",
        "X-Plex-Client-Identifier": client_id,
        "Accept": "application/json",
    }


async def generate_pin(client_id: str | None = None) -> dict:
    """Request a new PIN from plex.tv. Returns {id, code, client_id, auth_url}."""
    cid = client_id or _default_client_id()
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(f"{PLEX_TV}/pins", headers=_headers(cid), params={"strong": "true"})
        resp.raise_for_status()
        data = resp.json()

    pin_id = data["id"]
    code = data["code"]
    auth_url = (
        f"https://app.plex.tv/auth#?"
        f"clientID={cid}&code={code}&context%5Bdevice%5D%5Bproduct%5D=Jukeplox"
    )
    return {"id": pin_id, "code": code, "client_id": cid, "auth_url": auth_url}


async def poll_pin(pin_id: int, client_id: str) -> str | None:
    """Poll plex.tv for PIN resolution. Returns auth token when ready, None if still pending."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{PLEX_TV}/pins/{pin_id}",
            headers=_headers(client_id),
        )
        resp.raise_for_status()
        data = resp.json()
    return data.get("authToken") or None


async def await_pin(pin_id: int, client_id: str) -> str:
    """Block until the PIN is authorised. Raises PlexAuthError on timeout."""
    elapsed = 0
    while elapsed < POLL_TIMEOUT:
        token = await poll_pin(pin_id, client_id)
        if token:
            return token
        await asyncio.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL
    raise PlexAuthError("PIN authorisation timed out")


def _best_url(connections: list[dict], owned: bool = True) -> str | None:
    if owned:
        # Own server: prefer local LAN for lowest latency
        local = [c for c in connections if c.get("local")]
        non_relay = [c for c in connections if not c.get("relay")]
        chosen = local or non_relay or connections
    else:
        # Shared server: local IPs are on someone else's LAN and unreachable
        non_local = [c for c in connections if not c.get("local")]
        non_relay = [c for c in non_local if not c.get("relay")]
        chosen = non_relay or non_local or connections
    return chosen[0]["uri"] if chosen else None


# Per-connection reachability probe timeout. Short so the one-time discovery
# stays snappy even when a preferred (e.g. local) connection is a dead end.
_PROBE_TIMEOUT = 2.5


def _ordered_candidates(
    connections: list[dict], owned: bool, preferred: str | None = None
) -> list[tuple[str, bool]]:
    """(uri, is_raw_http) pairs in the order to probe for reachability.

    Both: the preferred (previously-working persisted) URL first — stability
    wins; a re-auth or daily refresh must never downgrade a good URL to a WAN
    hairpin, and a shared server's pinned URL is as worth keeping as an owned
    one's (source-health plan U2). Owned, then: local https →
    remote https → RAW http://{address}:{port} synthesized from local entries
    (rescues routers whose DNS-rebind protection kills plex.direct hostnames;
    plaintext-token path, so strictly after every HTTPS candidate; audit F9)
    → relay last. Shared: remote → relay → local last (local = someone else's
    LAN; no raw synthesis — those addresses are never ours to reach). De-duped,
    order preserved.

    Known limit (characterized on the rig, 2026-08-06): a bridge-networked PMS
    (e.g. a TrueNAS app) may advertise ONLY its container IP as the local
    connection — the real LAN address appears nowhere in the payload, so no
    client-side candidate can reach it and discovery lands on the WAN hairpin.
    The fix for that population is server-side (Plex's "Custom server access
    URLs"), documented in the install docs.
    """
    local = [c for c in connections if c.get("local")]
    relay = [c for c in connections if c.get("relay") and not c.get("local")]
    remote = [c for c in connections if not c.get("local") and not c.get("relay")]
    ordered: list[tuple[dict, bool]]
    if owned:
        raw = [
            {"uri": f"http://{c['address']}:{c['port']}"}
            for c in local
            if c.get("address") and c.get("port")
        ]
        ordered = ([({"uri": preferred}, False)] if preferred else []) \
            + [(c, False) for c in local + remote] \
            + [(c, True) for c in raw] \
            + [(c, False) for c in relay]
    else:
        ordered = ([({"uri": preferred}, False)] if preferred else []) \
            + [(c, False) for c in remote + relay + local]
    seen: set[str] = set()
    out: list[tuple[str, bool]] = []
    for c, is_raw in ordered:
        uri = c.get("uri")
        if uri and uri not in seen:
            seen.add(uri)
            out.append((uri, is_raw))
    return out


async def _reachable_url(
    connections: list[dict],
    owned: bool,
    client: "httpx.AsyncClient",
    preferred: str | None = None,
) -> str | None:
    """Pick the first connection that actually answers, in preference order.

    Fixes the deploy-location assumption baked into `_best_url`: an owned server's
    LOCAL LAN URI is unreachable when Jukeplox runs off the server's LAN (NAT'd
    container, remote/cloud deploy), so its libraries never list while shared
    servers (which already avoid local URIs) work. Probing `/identity` (public,
    no auth) and taking the first reachable URI makes the saved URL correct
    regardless of where Jukeplox runs. Returns ``None`` when NO candidate
    answers — the caller (``discover_servers``) then keeps the saved URL, or the
    static best pick for a never-saved server, and carries a typed
    ``unreachable`` probe outcome so the row is marked rather than dropped
    (source-health plan U2; before that the fallback was silent and a server
    nobody could reach looked identical to a healthy one).

    Raw-http candidates (synthesized; audit F9) carry a stricter bar: the
    /identity probe must return a NON-error response before the plaintext form
    is persisted — servers set to "Secure connections: Required" answer HTTP
    probes while refusing HTTP API calls, and persisting such a URL would be
    strictly worse than the hairpin it replaces. Every candidate verdict is
    logged at INFO (loud probes — the dead-server stall lesson).
    """
    for uri, is_raw in _ordered_candidates(connections, owned, preferred=preferred):
        try:
            resp = await client.get(f"{uri}/identity", timeout=_PROBE_TIMEOUT)
            if is_raw and resp.status_code >= 400:
                _log.info("plex url probe: %s answered HTTP %s but raw-http needs "
                          "a non-error /identity — skipping", uri, resp.status_code)
                continue
            _log.info("plex url probe: %s reachable (HTTP %s) — selected",
                      uri, resp.status_code)
            return uri  # reachable (and API-usable where required)
        except Exception as e:
            _log.info("plex url probe: %s unreachable (%s: %s)",
                      uri, type(e).__name__, e)
            continue
    _log.info("plex url probe: no candidate answered")
    return None


async def verify_account_token(
    token: str, client_id: str, client: "httpx.AsyncClient | None" = None,
) -> tuple[str, str]:
    """Ask plex.tv whether ``token`` is a valid ACCOUNT token (``/api/v2/user``).

    Returns ``(verdict, username)`` with a hard verdict: ``"ok"`` (200, username
    is the account title), ``"rejected"`` (401/403 — plex.tv does not know this
    token as an account), or ``"error"`` (anything else: 5xx, 429, connection
    error, timeout — no verdict). The health module's bootstrap (KTD3) adopts the
    owned server's saved token as the account token only on ``"ok"``. Never
    raises; never logs the token.
    """
    headers = {**_headers(client_id), "X-Plex-Token": token}

    async def _call(c: "httpx.AsyncClient") -> tuple[str, str]:
        try:
            resp = await c.get(f"{PLEX_TV}/user", headers=headers, timeout=10)
        except Exception as e:  # noqa: BLE001 — class only, never text (may carry a URL)
            _log.info("plex account check failed: %s", type(e).__name__)
            return "error", ""
        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                return "ok", ""
            return "ok", str(data.get("title") or data.get("username") or "")
        if resp.status_code in (401, 403):
            return "rejected", ""
        _log.info("plex account check: HTTP %s", resp.status_code)
        return "error", ""

    if client is not None:
        return await _call(client)
    async with httpx.AsyncClient(timeout=10) as c:
        return await _call(c)


# Per-server authenticated listing budget (plan constraint: probes use
# ``httpx.Timeout(15, connect=5)`` and run concurrently, so a sweep over N
# servers finishes in one budget, not N).
SECTIONS_TIMEOUT = httpx.Timeout(15, connect=5)


async def list_sections(url: str, token: str) -> dict:
    """One authenticated ``GET {url}/library/sections`` with the listed token.

    Returns ``{"status", "sections", "status_code"}`` where ``status`` is the
    server-health verdict: ``"usable"`` (200; ``sections`` holds
    ``{key, title, type}`` for every Directory entry), ``"token_rejected"``
    (401/403), or ``"unreachable"`` (connection error, timeout, or any other
    non-200 — the server did not serve us). This is the ONLY call that can
    produce ``usable``: the unauthenticated ``/identity`` probe proves an
    address answers, not that the token is accepted. Never raises; logs the
    exception class and status code only, never text or the URL.
    """
    headers = {"X-Plex-Token": token, "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=SECTIONS_TIMEOUT) as client:
            resp = await client.get(f"{url.rstrip('/')}/library/sections", headers=headers)
    except Exception as e:  # noqa: BLE001 — class only (httpx text embeds the URL)
        _log.info("plex sections probe failed: %s", type(e).__name__)
        return {"status": "unreachable", "sections": [], "status_code": None}
    code = resp.status_code
    if code == 200:
        try:
            raw = resp.json().get("MediaContainer", {}).get("Directory", [])
        except (ValueError, AttributeError):
            raw = []
        sections = [
            {"key": str(sec.get("key")), "title": str(sec.get("title") or ""),
             "type": str(sec.get("type") or "")}
            for sec in raw if sec.get("key") is not None
        ]
        return {"status": "usable", "sections": sections, "status_code": 200}
    if code in (401, 403):
        return {"status": "token_rejected", "sections": [], "status_code": code}
    _log.info("plex sections probe: HTTP %s", code)
    return {"status": "unreachable", "sections": [], "status_code": code}


def _provides_server(resource: dict) -> bool:
    provides = str(resource.get("provides") or "")
    return any(p.strip() == "server" for p in provides.split(","))


async def discover_servers(
    token: str, client_id: str, known_urls: dict[str, str] | None = None
) -> list[dict]:
    """Return EVERY server-providing plex.tv resource that carries a client
    identifier, with a typed probe outcome.

    Each dict: ``{machine_id, server_url, name, owner, owned, token, client_id,
    probe}`` where ``probe`` is ``"reachable"`` (an address answered
    ``/identity`` and ``server_url`` is that address) or ``"unreachable"`` (no
    candidate answered — ``server_url`` is the saved URL from ``known_urls`` when
    there is one, else the static best pick, else ``""``). A resource is never
    dropped for having no usable address: presence in this list is what the
    health module uses to decide which saved rows plex.tv still lists
    (source-health plan U2 / KTD2), so dropping one would mark a live server
    ``no_longer_shared``.

    ``known_urls`` maps machine_id → the currently-persisted server_url; it is
    probed FIRST for owned and shared servers alike, so a refresh keeps a
    working (possibly hand-pinned) URL instead of downgrading it (audit F9).

    Raises ``httpx.HTTPStatusError`` when plex.tv answers non-2xx to the
    resources call (the caller maps 401/403 to ``account_rejected`` and
    anything else to "no change + cooldown") and ``httpx.HTTPError`` when it
    cannot be reached.
    """
    headers = {**_headers(client_id), "X-Plex-Token": token}
    servers = []
    async with httpx.AsyncClient(timeout=10) as client:
        # The listing and the account check are independent plex.tv calls.
        resp, (verdict, admin_username) = await asyncio.gather(
            client.get(
                f"{PLEX_TV}/resources",
                params={"includeHttps": "1", "includeRelay": "1"},
                headers=headers,
            ),
            verify_account_token(token, client_id, client=client),
        )
        resp.raise_for_status()
        resources = resp.json()
        if verdict != "ok":
            admin_username = ""

        # Probe connections for reachability INSIDE the client context (the
        # client must be live for _reachable_url's /identity probes). One
        # probe per resource, concurrently; the result keeps listing order.
        entries = []
        for resource in resources:
            if not _provides_server(resource):
                continue
            machine_id = resource.get("clientIdentifier", "")
            if not machine_id:
                continue
            owned = resource.get("owned", True)
            connections = resource.get("connections") or []
            saved = (known_urls or {}).get(machine_id) or None
            entries.append((resource, machine_id, owned, connections, saved))
        probes = await asyncio.gather(
            *(_reachable_url(conns, bool(owned), client, preferred=saved)
              for _, _, owned, conns, saved in entries))
        for (resource, machine_id, owned, connections, saved), reachable in zip(entries, probes):
            name = resource.get("name", "Plex Server")
            source_title = resource.get("sourceTitle", "")
            owner = admin_username if owned else (source_title or name)
            access_token = resource.get("accessToken") or token
            url = reachable or saved or _best_url(connections, owned=bool(owned)) or ""
            servers.append({
                "machine_id": machine_id,
                "server_url": url,
                "name": name,
                "owner": owner,
                # Collected-library plan U1: the raw owned flag drives queue
                # source priority (owned servers win).
                "owned": bool(owned),
                "token": access_token,
                "client_id": client_id,
                "probe": "reachable" if reachable else "unreachable",
            })
    return servers


async def discover_server(token: str, client_id: str) -> str:
    """Return the best URL for the first owned server (backward-compat helper)."""
    servers = await discover_servers(token, client_id)
    for s in servers:
        if s["server_url"]:
            return s["server_url"]
    raise PlexAuthError("No Plex server found for this account")
