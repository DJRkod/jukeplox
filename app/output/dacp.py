"""DACP (Digital Audio Control Protocol) HTTP server.

AirPlay 2 receivers (JBL, WiiM, etc.) report hardware volume changes to the
sender by calling back to a DACP HTTP endpoint that the sender advertises
via mDNS during pair-verify. This module provides:

- A single process-wide HTTP server on an ephemeral port in the MA-canonical
  39831-49831 range, handling DACP volumeup / volumedown / setproperty
  requests.
- A stable 16-character uppercase hex DACP-ID persisted under the
  `output_dacp_id` setting so AP2 pair-verify state survives restarts.
- Per-session Active-Remote tokens that cliap2 echoes back to the speaker;
  the server matches incoming Active-Remote headers against registered
  sessions to reject spoofed callbacks.
- mDNS publication as `iTunes_Ctrl_<dacp_id>._dacp._tcp.local.` when a
  shared AsyncZeroconf is available (port 5353 case).

Speaker-side calls land here, get validated, and broadcast as
VolumeChangedEvent on the WebSocket bus — same shape as ChromecastBackend
and DlnaBackend external volume changes.

Reference: github.com/music-assistant/server providers/airplay/provider.py
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import socket
import urllib.parse
from typing import Any

_log = logging.getLogger(__name__)


# Port range chosen by Music Assistant. Speakers don't care what port the
# DACP server lives on — they read it from the mDNS TXT record cliap2
# advertised during pair-verify.
_DACP_PORT_MIN = 39831
_DACP_PORT_MAX = 49831
_DACP_DISCOVERY_TYPE = "_dacp._tcp.local."


def _bind_in_range(low: int, high: int) -> socket.socket:
    """Bind a socket in the given port range and return it open so the
    caller can hand it to asyncio.start_server with sock=. Keeping the
    socket bound through the handoff eliminates the TOCTOU window the
    previous probe-then-rebind approach left open (another process could
    grab the port between our close and start_server's bind)."""
    for port in range(low, high + 1):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            sock.close()
            continue
        return sock
    raise OSError(f"No free port in range {low}-{high} for DACP server")


async def _get_or_create_dacp_id() -> str:
    """Read the persisted DACP-ID, or generate and persist one on first call.

    Per MA's convention the DACP-ID is the first 16 characters of a stable
    server UUID, uppercased. Persisting it means AP2 pair-verify state on
    the receiver survives a Jukeplox restart so users don't have to re-pair
    after every container rebuild.
    """
    from app import database
    stored = await database.get_setting("output_dacp_id")
    if stored:
        return stored
    new_id = secrets.token_hex(8).upper()  # 16 hex chars
    await database.set_setting("output_dacp_id", new_id)
    return new_id


class DacpSession:
    """Bookkeeping for a single AirPlay playback session.

    cliap2 receives the Active-Remote token at session start and echoes it
    back to the speaker, which then includes it in callback headers. We
    register the token here so the server can validate incoming requests
    without trusting the source IP."""

    def __init__(self, dacp_id: str, active_remote_id: int) -> None:
        self.dacp_id = dacp_id
        self.active_remote_id = active_remote_id


class DacpServer:
    """Process-wide DACP HTTP server. Created once at app startup; the
    AirPlayBackend asks it for fresh Active-Remote ids per playback session.
    """

    def __init__(self) -> None:
        self.dacp_id: str | None = None
        self.port: int | None = None
        self._server: asyncio.AbstractServer | None = None
        self._sessions: dict[int, DacpSession] = {}
        self._mdns_info: Any = None
        # Keep a handle on the shared zeroconf so stop() can unregister
        # the mDNS service. Stale records linger until TTL otherwise.
        self._shared_aiozc: Any = None
        # Callback fired on valid volume callback; wired by state.py.
        # Signature: (backend, level: float) → coroutine
        self._on_volume_change: Any = None

    async def start(self, shared_aiozc: Any = None) -> None:
        """Bind an HTTP listener and register with mDNS.

        shared_aiozc is the app's existing AsyncZeroconf instance from
        state.setup(). If None (port 5353 collision case), the server still
        runs but speakers can't discover it; AE3 callbacks won't fire. That
        deferred D-Bus avahi publish path is tracked in the plan's deferred
        section."""
        self.dacp_id = await _get_or_create_dacp_id()
        bound_sock = _bind_in_range(_DACP_PORT_MIN, _DACP_PORT_MAX)
        self.port = bound_sock.getsockname()[1]
        # asyncio.start_server takes ownership of the bound socket; the
        # server's wait_closed() handles its close.
        self._server = await asyncio.start_server(
            self._handle_request, sock=bound_sock,
        )
        self._shared_aiozc = shared_aiozc
        _log.info("DACP: listening on 0.0.0.0:%d (dacp_id=%s)",
                  self.port, self.dacp_id)
        if shared_aiozc is not None:
            try:
                await self._register_mdns(shared_aiozc)
            except Exception:
                _log.warning(
                    "DACP: mDNS registration failed — speaker-initiated "
                    "volume changes won't surface",
                    exc_info=True,
                )
        else:
            _log.warning(
                "DACP: no shared zeroconf — speaker-initiated volume changes "
                "won't surface (D-Bus avahi publish path is deferred work)"
            )

    async def _register_mdns(self, shared_aiozc: Any) -> None:
        """Publish iTunes_Ctrl_<dacp_id>._dacp._tcp.local. so AirPlay
        receivers can resolve the DACP endpoint cliap2 advertised in the
        pair-verify exchange."""
        from zeroconf.asyncio import AsyncServiceInfo
        instance_name = f"iTunes_Ctrl_{self.dacp_id}.{_DACP_DISCOVERY_TYPE}"
        # The published address is the interface IP — fall back to the
        # detected hostname's IP. AsyncServiceInfo accepts packed bytes; we
        # leave addresses empty and let zeroconf pick interface addresses.
        self._mdns_info = AsyncServiceInfo(
            _DACP_DISCOVERY_TYPE,
            name=instance_name,
            port=self.port,
            properties={
                "DvNm": "Jukeplox",
                "RemV": "10000",
                "DvTy": "iTunes",
                "RemN": "Remote",
                "txtvers": "1",
                "Pass": "0",
            },
            server=f"{socket.gethostname()}.local.",
        )
        await shared_aiozc.async_register_service(self._mdns_info)

    async def stop(self) -> None:
        # Unregister mDNS first so speakers stop trying to call us during
        # the HTTP shutdown grace.
        if self._mdns_info is not None and self._shared_aiozc is not None:
            try:
                await self._shared_aiozc.async_unregister_service(self._mdns_info)
            except Exception:
                _log.warning("DACP: mDNS unregister failed", exc_info=True)
            self._mdns_info = None
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None

    def new_session(self) -> DacpSession:
        """Mint a fresh per-stream session. cliap2 is invoked with the
        returned Active-Remote id so the speaker's callbacks carry it as a
        verifiable token."""
        if self.dacp_id is None:
            raise RuntimeError("DacpServer not started")
        active_remote_id = secrets.randbits(32)
        session = DacpSession(self.dacp_id, active_remote_id)
        self._sessions[active_remote_id] = session
        return session

    def end_session(self, active_remote_id: int) -> None:
        """Drop the session — subsequent callbacks with this token will be
        rejected. Called by AirPlayBackend._teardown()."""
        self._sessions.pop(active_remote_id, None)

    # ── HTTP handling ────────────────────────────────────────────────────

    async def _handle_request(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Hand-rolled HTTP/1.0 request handler.

        DACP traffic is so simple (GET-only with one header that matters)
        that a 30-line parser is cheaper than pulling in aiohttp. Tolerant
        of malformed input — bad requests get HTTP 400 and the connection
        closes."""
        try:
            # 5s read timeout protects against slow-loris on the LAN.
            # AirPlay receivers complete their request line and headers
            # in well under 100ms in normal operation; 5s leaves ample
            # headroom and bounds the asyncio handler's lifetime.
            request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not request_line:
                return
            try:
                method, path, _ = request_line.decode("ascii").strip().split(" ", 2)
            except ValueError:
                await self._send_status(writer, 400, "Bad Request")
                return

            headers: dict[str, str] = {}
            # Cap header count so a malicious client can't blow memory by
            # streaming an unbounded prelude.
            header_budget = 64
            while header_budget > 0:
                header_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if not header_line or header_line in (b"\r\n", b"\n"):
                    break
                try:
                    name, _, value = header_line.decode("ascii").partition(":")
                    headers[name.strip().lower()] = value.strip()
                except UnicodeDecodeError:
                    continue
                header_budget -= 1

            await self._dispatch(method, path, headers, writer)
        except asyncio.TimeoutError:
            _log.warning("DACP: request read timed out (slow client)")
        except Exception:
            _log.warning("DACP: request handling crashed", exc_info=True)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _dispatch(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        writer: asyncio.StreamWriter,
    ) -> None:
        if method != "GET":
            await self._send_status(writer, 405, "Method Not Allowed")
            return

        # Active-Remote header is the per-session token cliap2 handed to the
        # speaker during pair-verify. Mismatch = rogue caller; reject.
        ar_header = headers.get("active-remote")
        session: DacpSession | None = None
        if ar_header:
            try:
                ar_int = int(ar_header)
                session = self._sessions.get(ar_int)
            except ValueError:
                session = None
        if session is None:
            await self._send_status(writer, 401, "Unauthorized")
            return

        parsed = urllib.parse.urlparse(path)
        query = urllib.parse.parse_qs(parsed.query)

        # /ctrl-int/1/setproperty?dmcp.device-volume=<float>
        if parsed.path == "/ctrl-int/1/setproperty":
            vol_str = (
                query.get("dmcp.device-volume", [None])[0]
                or query.get("dmcp.volume", [None])[0]
            )
            if vol_str is None:
                await self._send_status(writer, 400, "Bad Request")
                return
            try:
                level = float(vol_str)
            except ValueError:
                await self._send_status(writer, 400, "Bad Request")
                return
            level = max(0.0, min(1.0, level))
            await self._send_status(writer, 204, "No Content")
            await self._fire_volume_change(session, level)
            return

        # /ctrl-int/1/volumeup, /ctrl-int/1/volumedown — relative changes
        # without an explicit level. cliap2 receivers occasionally use these
        # for hardware button presses. We treat them as small steps; the
        # exact value isn't surfaced as a hardware-button-press semantic in
        # Jukeplox UI.
        if parsed.path in ("/ctrl-int/1/volumeup", "/ctrl-int/1/volumedown"):
            delta = 0.05 if parsed.path.endswith("up") else -0.05
            await self._send_status(writer, 204, "No Content")
            await self._fire_volume_change_relative(session, delta)
            return

        await self._send_status(writer, 404, "Not Found")

    async def _fire_volume_change(self, session: DacpSession, level: float) -> None:
        if self._on_volume_change is None:
            return
        try:
            await self._on_volume_change(session, level, absolute=True)
        except Exception:
            _log.warning("DACP: volume-change callback raised", exc_info=True)

    async def _fire_volume_change_relative(
        self, session: DacpSession, delta: float
    ) -> None:
        if self._on_volume_change is None:
            return
        try:
            await self._on_volume_change(session, delta, absolute=False)
        except Exception:
            _log.warning("DACP: relative volume callback raised", exc_info=True)

    async def _send_status(
        self, writer: asyncio.StreamWriter, code: int, reason: str
    ) -> None:
        try:
            writer.write(
                f"HTTP/1.0 {code} {reason}\r\n"
                f"Content-Length: 0\r\n"
                f"Connection: close\r\n"
                f"\r\n".encode("ascii")
            )
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
