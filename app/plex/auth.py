"""Plex PIN-based OAuth flow.

Flow:
  1. generate_pin()  → {id, code, auth_url}  (show auth_url to admin)
  2. poll_pin(id)    → token | None          (poll until non-None, max 5 min)
  3. Store token via database.set_plex_config()
"""

import asyncio
import uuid

import httpx

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


def _ordered_candidates(connections: list[dict], owned: bool) -> list[str]:
    """Connection URIs in the order to probe for reachability.

    Owned: local (fast on-LAN) → remote (public) → relay (always works, slower).
    Shared: remote → relay → local last (local = someone else's LAN). De-duped,
    order preserved.
    """
    local = [c for c in connections if c.get("local")]
    relay = [c for c in connections if c.get("relay") and not c.get("local")]
    remote = [c for c in connections if not c.get("local") and not c.get("relay")]
    ordered = (local + remote + relay) if owned else (remote + relay + local)
    seen: set[str] = set()
    out: list[str] = []
    for c in ordered:
        uri = c.get("uri")
        if uri and uri not in seen:
            seen.add(uri)
            out.append(uri)
    return out


async def _reachable_url(connections: list[dict], owned: bool, client: "httpx.AsyncClient") -> str | None:
    """Pick the first connection that actually answers, in preference order.

    Fixes the deploy-location assumption baked into `_best_url`: an owned server's
    LOCAL LAN URI is unreachable when Jukeplox runs off the server's LAN (NAT'd
    container, remote/cloud deploy), so its libraries never list while shared
    servers (which already avoid local URIs) work. Probing `/identity` (public,
    no auth) and taking the first reachable URI makes the saved URL correct
    regardless of where Jukeplox runs. Falls back to the static best pick so a
    server with no currently-reachable connection is still saved, not dropped —
    re-connecting re-discovers if the network later changes.
    """
    for uri in _ordered_candidates(connections, owned):
        try:
            await client.get(f"{uri}/identity", timeout=_PROBE_TIMEOUT)
            return uri  # any HTTP response means the connection is reachable
        except Exception:
            continue
    return _best_url(connections, owned=owned)


async def discover_servers(token: str, client_id: str) -> list[dict]:
    """Return all accessible Plex servers with per-server access tokens.

    Each dict: {machine_id, server_url, name, owner, token, client_id}
    """
    headers = {**_headers(client_id), "X-Plex-Token": token}
    servers = []
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            "https://plex.tv/api/v2/resources",
            params={"includeHttps": "1", "includeRelay": "1"},
            headers=headers,
        )
        resp.raise_for_status()
        resources = resp.json()

        try:
            user_resp = await client.get(f"{PLEX_TV}/user", headers=headers)
            user_resp.raise_for_status()
            user_data = user_resp.json()
            admin_username = user_data.get("title") or user_data.get("username") or ""
        except Exception:
            admin_username = ""

        # Probe connections for reachability INSIDE the client context (the
        # client must be live for _reachable_url's /identity probes).
        for resource in resources:
            if not resource.get("provides", "").startswith("server"):
                continue
            machine_id = resource.get("clientIdentifier", "")
            name = resource.get("name", "Plex Server")
            owned = resource.get("owned", True)
            source_title = resource.get("sourceTitle", "")
            owner = admin_username if owned else (source_title or name)
            access_token = resource.get("accessToken") or token
            url = await _reachable_url(resource.get("connections", []), bool(owned), client)
            if not url or not machine_id:
                continue
            servers.append({
                "machine_id": machine_id,
                "server_url": url,
                "name": name,
                "owner": owner,
                # Collected-library plan U1: the raw owned flag drives queue
                # source priority (owned servers win); persisted by
                # save_plex_servers, previously dropped here.
                "owned": bool(owned),
                "token": access_token,
                "client_id": client_id,
            })
    return servers


async def discover_server(token: str, client_id: str) -> str:
    """Return the best URL for the first owned server (backward-compat helper)."""
    servers = await discover_servers(token, client_id)
    if servers:
        return servers[0]["server_url"]
    raise PlexAuthError("No Plex server found for this account")
