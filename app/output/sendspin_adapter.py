"""Version-pinned isolation layer for ``aiosendspin`` (2026-08-11 plan U7).

aiosendspin had ~9 breaking major releases in ~8 months, so EVERY reference to
the library lives in this one file behind a stable interface. A breaking bump is
then contained here — the Sendspin backend (``app/output/sendspin.py``) and its
tests target ``SendspinAdapter``'s methods, never raw library names.

The library import is function-local (dormancy, R16): importing this module pulls
in nothing, so ``app.output.sendspin`` stays importable — and the U1 zoning
contract test can introspect ``SendspinBackend`` — without ``aiosendspin`` / PyAV
installed. aiosendspin requires Python ≥3.12; the jukeplox image is 3.12, but the
local (3.11) test interpreter can't install it, so the local sendspin tests use a
fake adapter and the REAL API is exercised only by the in-image e2e (U10).

**Verified against the pinned aiosendspin 9.1.0 in-image (2026-08-12).** The real
9.1.0 API is roles-based and materially different from the Music-Assistant-shaped
guess the first draft assumed. What is aligned here (the load-bearing server +
feed data-path, which maps cleanly):

- Classes live at ``aiosendspin.server.server`` (NOT top-level ``aiosendspin``).
- ``SendspinServer(loop, identity, server_name, *, pairing_store)`` — needs an
  ``Identity`` (``aiosendspin.noise.keys.Identity.generate()``) and a
  ``ServerPairingStore`` (``aiosendspin.noise.trust_store``).
- ``server.start_server(port, host, *, discover_clients)`` / ``stop_server()``.
- Feed: ``group = SendspinGroup(server, *clients)`` → ``stream = group.start_stream()``
  (a ``PushStream``) → ``stream.prepare_audio(pcm, AudioFormat(48000,16,2))`` /
  ``stream.commit_audio()`` / ``stream.sleep_to_limit_buffer(max_buffer_us)``.
- Group volume/mute via the group's player role (``group.group_role``).

**SETTLE-TIME GAPS (need a design decision — see the backend's zoning/pairing
docstrings):** aiosendspin 9.1.0 has GROUP-level volume via the player role, not
the per-client model this backend's zoning assumes; and pairing is per-client and
``PairingAttempt``-based (a PIN-provider callback), not the PSK/PIN model the
admin pairing endpoints assume. Those two surfaces (per-client volume, pairing)
raise ``NotImplementedError`` with a pointer here until the model is aligned. The
server + feed path below is exercised by the in-image e2e.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

_log = logging.getLogger(__name__)

DEFAULT_PORT = 8927
SAMPLE_RATE = 48000
SAMPLE_BITS = 16
CHANNELS = 2
# Backpressure ceiling handed to PushStream.sleep_to_limit_buffer (microseconds).
_MAX_BUFFER_US = 500_000


async def _maybe_await(res: Any) -> Any:
    """aiosendspin mixes sync and coroutine returns across its surface; await
    only when the call actually returned a coroutine/awaitable."""
    if asyncio.iscoroutine(res) or hasattr(res, "__await__"):
        return await res
    return res


def _make_pairing_store():
    """A concrete in-memory ``ServerPairingStore`` — the 12 abstract methods
    aiosendspin 9.1.0 requires (it ships no concrete impl). VERIFIED on the arm64
    rig 2026-08-12: with this store, ``SendspinServer(...)`` constructs and
    ``start_server()`` binds 8927. Defined function-local so importing this module
    never imports aiosendspin (dormancy). NOTE: in-memory means pairings do not
    survive a restart — a persistent (``/data``-backed) store is the settle-time
    upgrade before Sendspin ships (the records are security-sensitive)."""
    from aiosendspin.server.server import ServerPairingStore

    class _InMemoryPairingStore(ServerPairingStore):
        def __init__(self):
            self._rec, self._staged, self._trusted = {}, {}, {}

        def store_record(self, record):
            self._rec[getattr(record, "client_id", None)] = record

        def record_by_client_id(self, client_id):
            return self._rec.get(client_id)

        def remove_record(self, client_id):
            self._rec.pop(client_id, None)

        def list_records(self):
            return list(self._rec.values())

        def stage_pairing_psk(self, client_id, staged):
            self._staged[client_id] = staged

        def staged_pairing_psk(self, client_id):
            return self._staged.get(client_id)

        def unstage_pairing_psk(self, client_id):
            self._staged.pop(client_id, None)

        def list_staged_pairing_psks(self):
            return list(self._staged.values())

        def add_trusted_unpaired(self, client):
            self._trusted[getattr(client, "client_id", None)] = client

        def trusted_unpaired(self, client_id):
            return self._trusted.get(client_id)

        def remove_trusted_unpaired(self, client_id):
            self._trusted.pop(client_id, None)

        def list_trusted_unpaired(self):
            return list(self._trusted.values())

    return _InMemoryPairingStore()


class SendspinAdapter:
    """Stable façade over one running ``SendspinServer`` instance (real 9.1.0)."""

    def __init__(self, server: Any, audio_format: Any) -> None:
        self._server = server
        self._audio_format = audio_format
        self._group: Any = None
        self._stream: Any = None

    # ── lifecycle ───────────────────────────────────────────────────────────

    @classmethod
    async def start(
        cls,
        *,
        host: str,
        port: int = DEFAULT_PORT,
        pairing_store_path: str,
        discover_clients: bool = True,
    ) -> "SendspinAdapter":
        """Construct + start an in-process SendspinServer bound to an EXPLICIT
        LAN ``host`` (never blind ``0.0.0.0``). ``allow_unencrypted`` stays
        default (False)."""
        from aiosendspin.server.server import SendspinServer
        from aiosendspin.server.push_stream import AudioFormat
        from aiosendspin.noise.keys import Identity

        loop = asyncio.get_running_loop()
        identity = Identity.generate()
        store = _make_pairing_store()
        # allow_unencrypted stays False (secure default). VERIFIED on the rig:
        # this constructs + start_server() binds 8927. Client onboarding still
        # needs the pairing protocol wired (see get_pairing_pin below).
        server = SendspinServer(loop, identity, "jukeplox", pairing_store=store)
        await _maybe_await(server.start_server(
            port=port, host=host, discover_clients=discover_clients))
        return cls(server, AudioFormat(SAMPLE_RATE, SAMPLE_BITS, CHANNELS))

    async def stop(self) -> None:
        if self._stream is not None:
            try:
                await _maybe_await(self._stream.stop())
            except Exception:
                pass
            self._stream = None
        await _maybe_await(self._server.stop_server())

    # ── events / clients ────────────────────────────────────────────────────

    def add_event_listener(self, cb: Callable[..., Any]) -> None:
        self._server.add_event_listener(cb)

    def clients(self) -> list[dict]:
        raw = getattr(self._server, "connected_clients", None) or []
        out = []
        for c in raw:
            out.append({
                "id": str(getattr(c, "id", None) or getattr(c, "client_id", "")),
                "name": str(getattr(c, "name", None) or "sendspin-client"),
                "volume": 0.0,   # per-client volume: settle-time (group-level only)
                "muted": False,
            })
        return out

    # ── feed (group push stream) ─────────────────────────────────────────────

    async def start_stream(self) -> None:
        from aiosendspin.server.server import SendspinGroup
        clients = list(getattr(self._server, "connected_clients", None) or [])
        self._group = SendspinGroup(self._server, *clients)
        self._stream = await _maybe_await(self._group.start_stream())

    async def prepare_audio(self, pcm: bytes) -> None:
        if self._stream is None:
            return
        await _maybe_await(self._stream.prepare_audio(pcm, self._audio_format))

    async def commit_audio(self) -> None:
        if self._stream is None:
            return
        await _maybe_await(self._stream.commit_audio())

    async def sleep_to_limit_buffer(self) -> None:
        if self._stream is None:
            return
        await _maybe_await(self._stream.sleep_to_limit_buffer(_MAX_BUFFER_US))

    # ── group volume/mute (player role) — group-level in 9.1.0 ──────────────

    def _group_role(self) -> Any:
        return getattr(self._group, "group_role", None) if self._group else None

    async def set_group_volume(self, level: float) -> None:
        role = self._group_role()
        if role is not None:
            await _maybe_await(role.set_group_volume(int(round(max(0.0, min(1.0, level)) * 100))))

    async def set_group_mute(self, muted: bool) -> None:
        role = self._group_role()
        if role is not None:
            await _maybe_await(role.set_group_muted(bool(muted)))

    # ── per-client volume + pairing — SETTLE-TIME (design mismatch) ──────────

    async def set_client_volume(self, client_id: str, level: float) -> None:
        # aiosendspin 9.1.0 exposes volume at the GROUP (player-role) level, not
        # per client. Aligning the zoning model is a settle-time design decision.
        raise NotImplementedError(
            "aiosendspin 9.1.0 has group-level volume only; per-client volume "
            "needs a zoning-model design decision (see sendspin_adapter docstring)")

    async def set_client_mute(self, client_id: str, muted: bool) -> None:
        raise NotImplementedError(
            "aiosendspin 9.1.0 has group-level mute only; per-client mute needs "
            "a zoning-model design decision (see sendspin_adapter docstring)")

    @property
    def pairing_key(self) -> str:
        return ""  # no long-term PSK in the 9.1.0 pairing model

    async def initiate_pairing(self) -> str:
        raise NotImplementedError(
            "aiosendspin 9.1.0 pairing is per-client + PairingAttempt/PIN-provider "
            "based; the admin PSK/PIN model needs alignment (settle-time)")

    async def rotate_pairing(self) -> str:
        raise NotImplementedError(
            "aiosendspin 9.1.0 has no long-term PSK to rotate; pairing-model "
            "alignment is settle-time")


async def default_adapter_factory(
    *, host: str, port: int, pairing_store_path: str,
) -> SendspinAdapter:
    return await SendspinAdapter.start(
        host=host, port=port, pairing_store_path=pairing_store_path)
