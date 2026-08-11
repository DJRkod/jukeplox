"""Emby provider — a ``MusicSource`` over the Emby REST API (plan U1).

Emby forked from the same lineage as Jellyfin, so the ``/Items`` browse surface,
``RunTimeTicks`` durations, ``ProviderIds`` shape, and ``ChildCount`` are
identical — this adapter subclasses :class:`~app.sources.jellyfin.JellyfinSource`
and reuses its paging (``_paged``), TTL cache, per-instance client + semaphore,
and all parse helpers unchanged. Only the Emby-specific deltas are overridden
here (validated in ``tests/test_sources_emby.py`` — they are broader than a
prefix swap):

* **``/emby/`` path prefix on EVERY route** — ``/Users/{id}/Items``, ``/Items``,
  ``/Audio/{id}/stream``, etc. Applied once in :meth:`EmbySource._url` so every
  ``_get``/``_paged``/``fetch_art`` call routes through it.
* **Sign-in** (:func:`authenticate`) uses the ``X-Emby-Authorization`` *request
  header* form of AuthenticateByName. The password is sent in the body and
  **never returned or stored** — the caller persists only token + userId.
* **Request auth** rides the ``X-Emby-Token`` header (not Jellyfin's
  ``Authorization: MediaBrowser …``).
* **Streaming** (:meth:`resolve_stream`) returns a credential-free stream URL
  with the token in an ``X-Emby-Token`` header — header-auth, so no force-proxy
  is needed and no credential reaches a Cast/DLNA device that gets the URL (R25).

``source_type = "emby"``. Capabilities: native search + genres; sonic
similarity, popular tracks, and Plex "styles" degrade to the base class defaults.
"""

from __future__ import annotations

import httpx

from app.sources.base import Capabilities, StreamTarget
from app.sources.jellyfin import (
    CLIENT_NAME,
    CLIENT_VERSION,
    JellyfinSource,
    new_device_id,
)

_EMBY_PREFIX = "/emby"

_EMBY_CAPS = Capabilities(native_search=True, genres=True)


class EmbyAuthError(Exception):
    """Raised when Emby rejects credentials or a stored token (401)."""


def _emby_auth_value(device_id: str, token: str | None = None) -> str:
    """The ``X-Emby-Authorization`` header value used at sign-in time.

    Same MediaBrowser token scheme Emby's own clients send; the token is only
    appended when present (the AuthenticateByName request is pre-token)."""
    parts = [
        f'Client="{CLIENT_NAME}"',
        f'Device="{CLIENT_NAME}"',
        f'DeviceId="{device_id}"',
        f'Version="{CLIENT_VERSION}"',
    ]
    if token:
        parts.append(f'Token="{token}"')
    return "MediaBrowser " + ", ".join(parts)


async def authenticate(
    server_url: str,
    username: str,
    password: str,
    *,
    device_id: str,
    http: httpx.AsyncClient | None = None,
) -> dict:
    """Sign in to Emby and return ``{"token", "user_id", "server_id"}``.

    Uses Emby's ``X-Emby-Authorization`` request-header form of
    ``/emby/Users/AuthenticateByName``. The password is sent in the request body
    and **never returned or stored** — the caller persists only the token +
    userId. Raises :class:`EmbyAuthError` on a 401 or a response missing the
    token/userId.
    """
    server_url = server_url.rstrip("/")
    own = http is None
    client = http or httpx.AsyncClient(timeout=15)
    try:
        resp = await client.post(
            f"{server_url}{_EMBY_PREFIX}/Users/AuthenticateByName",
            json={"Username": username, "Pw": password},
            headers={
                "X-Emby-Authorization": _emby_auth_value(device_id),
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        if resp.status_code == 401:
            raise EmbyAuthError("Emby rejected the username/password")
        resp.raise_for_status()
        data = resp.json()
        token = data.get("AccessToken")
        user_id = (data.get("User") or {}).get("Id")
        if not token or not user_id:
            raise EmbyAuthError("Emby auth response missing token/userId")
        return {"token": token, "user_id": user_id, "server_id": data.get("ServerId", "")}
    finally:
        if own:
            await client.aclose()


class EmbySource(JellyfinSource):
    """Emby music source — a JellyfinSource with the Emby-specific deltas."""

    @property
    def source_type(self) -> str:
        return "emby"

    @property
    def capabilities(self) -> Capabilities:
        return _EMBY_CAPS

    # ── request shaping (the load-bearing Emby deltas) ─────────────────────────

    def _url(self, path: str) -> str:
        # Every Emby route is under /emby — apply it once here so all inherited
        # _get/_paged/fetch_art calls route through the prefix.
        return f"{self.server_url}{_EMBY_PREFIX}{path}"

    def _headers(self) -> dict:
        # Token rides X-Emby-Token, not Jellyfin's Authorization: MediaBrowser.
        return {"X-Emby-Token": self.token, "Accept": "application/json"}

    async def _get(self, path: str, params: dict | None = None) -> dict:
        async with self._sem:
            resp = await self._http.get(
                self._url(path), headers=self._headers(), params=params or {}
            )
        if resp.status_code == 401:
            raise EmbyAuthError("Emby token rejected (401)")
        resp.raise_for_status()
        return resp.json()

    # ── streaming (R25: token in an X-Emby-Token header, never the URL) ─────────

    def resolve_stream(self, stream_key: str) -> StreamTarget:
        bare = self._strip(stream_key) or stream_key
        url = f"{self.server_url}{_EMBY_PREFIX}/Audio/{bare}/stream?static=true"
        return StreamTarget(url=url, headers={"X-Emby-Token": self.token})


__all__ = ["EmbySource", "EmbyAuthError", "authenticate", "new_device_id"]
