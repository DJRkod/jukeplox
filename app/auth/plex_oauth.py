"""Plex PIN flow bridge for dashboard Plex connection.

Wraps the plex.auth PIN flow used by the in-dashboard Plex connect feature.
"""

from app import database, state
from app.plex import auth as plex_auth


async def start_flow(client_id: str | None = None) -> dict:
    """Begin the PIN flow. Returns pin info to send to the browser."""
    return await plex_auth.generate_pin(client_id)


async def complete_flow(pin_id: int, client_id: str) -> bool:
    """Poll once. Returns True if the PIN resolved and Plex is now connected.

    The caller is responsible for creating an admin session on True.
    """
    token = await plex_auth.poll_pin(pin_id, client_id)
    if not token:
        return False

    # Discover all accessible Plex servers and persist them
    try:
        # Pass the currently-persisted URLs so a re-auth probes each known
        # (possibly hand-pinned) URL first and never downgrades a working
        # server_url to a WAN hairpin (audit F9). Best-effort: an unreadable
        # table just means no preferences.
        try:
            known_urls = {s["machine_id"]: s["server_url"]
                          for s in await database.get_plex_servers()}
        except Exception:
            known_urls = {}
        servers = await plex_auth.discover_servers(token, client_id, known_urls=known_urls)
        if servers:
            await database.save_plex_servers(servers)
        else:
            # Fallback: discover single server and save legacy config
            server_url = await plex_auth.discover_server(token, client_id)
            await database.set_plex_config(server_url, token, client_id)
    except plex_auth.PlexAuthError:
        pass  # discovery failure doesn't block Plex connection
    state.invalidate_plex_client()
    return True
