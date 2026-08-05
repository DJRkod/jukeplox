"""WebSocket connection manager and event broadcaster."""

import asyncio
import logging
from dataclasses import asdict
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

_log = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self) -> None:
        self._admin: set[WebSocket] = set()
        self._guest: set[WebSocket] = set()
        # Admin queue display settings (applied per broadcast)
        self.guest_n: int | None = None   # upcoming songs limit
        self.guest_m: int | None = None   # history songs limit

    def connect(self, ws: WebSocket, role: str) -> None:
        if role == "admin":
            self._admin.add(ws)
        else:
            self._guest.add(ws)

    def disconnect(self, ws: WebSocket, role: str) -> None:
        (self._admin if role == "admin" else self._guest).discard(ws)

    async def _send(self, ws: WebSocket, data: dict) -> bool:
        """Send JSON to a single WebSocket. Returns False if the connection is dead."""
        try:
            await ws.send_json(data)
            return True
        except (WebSocketDisconnect, RuntimeError):
            return False

    async def _broadcast(self, sockets: set[WebSocket], payload: dict) -> None:
        """Send payload to all sockets in parallel and prune dead connections."""
        snap = list(sockets)
        results = await asyncio.gather(
            *[self._send(ws, payload) for ws in snap],
            return_exceptions=True,
        )
        for ws, ok in zip(snap, results):
            if ok is not True:
                sockets.discard(ws)

    async def broadcast_to_admins(self, event: Any) -> None:
        payload = event.to_json() if hasattr(event, "to_json") else asdict(event)
        await self._broadcast(self._admin, payload)

    async def broadcast_to_guests(self, event: Any) -> None:
        if hasattr(event, "truncated"):
            payload = event.truncated(self.guest_n, self.guest_m).to_json()
        else:
            payload = event.to_json() if hasattr(event, "to_json") else asdict(event)
        await self._broadcast(self._guest, payload)

    async def broadcast_to_all(self, event: Any) -> None:
        await asyncio.gather(
            self.broadcast_to_admins(event),
            self.broadcast_to_guests(event),
        )

    @property
    def admin_count(self) -> int:
        return len(self._admin)

    @property
    def guest_count(self) -> int:
        return len(self._guest)


# Singleton — imported by routers
manager = ConnectionManager()


async def notify_admin_error(message: str) -> None:
    """Best-effort admin toast on the established error channel
    (``OutputChangedEvent`` with ``backend_type="error"`` → the admin WS
    handler toasts ``device_name``). The ONE helper for every "tell the
    admin something went wrong" emission (review fix S-2) — never raises
    into the caller: a broadcast failure is logged and dropped, because
    every call site treats the notice as advisory."""
    try:
        from app.events.types import OutputChangedEvent
        await manager.broadcast_to_admins(OutputChangedEvent(
            backend_type="error", device_name=message))
    except Exception:
        _log.warning("admin error notice broadcast failed: %r", message,
                     exc_info=True)
