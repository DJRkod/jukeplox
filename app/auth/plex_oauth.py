"""Plex PIN flow bridge for dashboard Plex connection.

Wraps the plex.auth PIN flow used by the in-dashboard Plex connect feature.
"""

import logging

import httpx

from app import database, state
from app.plex import auth as plex_auth
from app.plex import health

_log = logging.getLogger(__name__)


async def start_flow(client_id: str | None = None) -> dict:
    """Begin the PIN flow. Returns pin info to send to the browser."""
    return await plex_auth.generate_pin(client_id)


async def complete_flow(pin_id: int, client_id: str) -> "health.ReconcileResult | None":
    """Poll once. Returns ``None`` while the PIN is unresolved; on resolution,
    the connect-origin reconcile result (``added`` / ``returned`` / ``vanished``
    / ``new_libraries`` / ``changed_tokens`` as machine ids and section keys —
    never a token). The caller is responsible for creating an admin session.

    Source-health plan U2 / KTD1: this is no longer a writer of the server
    table. The account token is stored (KTD3) and the discovered listing goes
    through ``health.reconcile(..., origin="connect")`` — the same routine the
    401 / startup / daily refreshes use — which clears tombstones, seeds a
    NEWLY added owned server's libraries, clears only its veto, and leaves a
    returned server's selection and veto alone (R12, R17).
    """
    token = await plex_auth.poll_pin(pin_id, client_id)
    if not token:
        return None

    try:
        await database.set_plex_account_token(token)
    except Exception as e:  # noqa: BLE001 — class only
        _log.warning("plex connect: account token not stored (%s); self-heal will "
                     "bootstrap from the owned server", type(e).__name__)

    result = health.ReconcileResult()
    try:
        # Pass the currently-persisted URLs so a re-auth probes each known
        # (possibly hand-pinned) URL first and never downgrades a working
        # server_url to a WAN hairpin (audit F9). Best-effort: an unreadable
        # table just means no preferences.
        try:
            known_urls = {s["machine_id"]: s["server_url"]
                          for s in await database.get_plex_servers(include_vanished=True)
                          if s.get("server_url")}
        except Exception:
            known_urls = {}
        servers = await plex_auth.discover_servers(token, client_id, known_urls=known_urls)
        if servers:
            # A 200 listing of zero servers writes nothing (guard kept).
            result = (await health.reconcile(servers, origin="connect")) or result
        else:
            # Fallback: discover single server and save legacy config
            server_url = await plex_auth.discover_server(token, client_id)
            await database.set_plex_config(server_url, token, client_id)
    except plex_auth.PlexAuthError:
        pass  # discovery failure doesn't block Plex connection
    except httpx.HTTPError as e:
        _log.warning("plex connect: discovery failed (%s)", type(e).__name__)
    state.invalidate_plex_client()
    return result
