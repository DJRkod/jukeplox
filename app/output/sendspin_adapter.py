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

**Two earlier beliefs about 9.1.0 were wrong and are corrected here (2026-08-13):**

- *"Volume is group-level only."* It is not. Each client's player role carries
  ``set_player_volume`` / ``set_player_mute`` / ``set_static_delay``. The earlier
  reading found the group role first and stopped looking.
- *"The library ships no concrete pairing store."* It ships two, including a
  file-backed one with atomic writes. jukeplox subclasses that and replaces only
  its two I/O primitives so the payload lands sealed rather than readable.

Pairing itself is inverted from the original assumption: the SERVER initiates and
the OPERATOR enters a code read off the speaker. There is no long-term server PSK
to display or rotate.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

_log = logging.getLogger(__name__)

DEFAULT_PORT = 8927
SAMPLE_RATE = 48000
SAMPLE_BITS = 16
CHANNELS = 2
# Backpressure ceiling handed to PushStream.sleep_to_limit_buffer (microseconds).
_MAX_BUFFER_US = 500_000

# The spec defines TWO discovery directions and requires a SERVER to support
# BOTH (https://www.sendspin-audio.com/spec/):
#
#   client-initiated : the SERVER advertises `_sendspin-server._tcp` (port 8927)
#                      and the client dials in.
#   server-initiated : the CLIENT advertises `_sendspin._tcp` (port 8928) and
#                      the server discovers it and dials out.
#
# The two service types are NOT interchangeable. jukeplox previously advertised
# itself under the CLIENT type, so nothing looking for a Sendspin *server* could
# ever find it — the reason no real speaker has ever connected.
SERVER_SERVICE_TYPE = "_sendspin-server._tcp.local."
CLIENT_SERVICE_TYPE = "_sendspin._tcp.local."
CLIENT_DEFAULT_PORT = 8928

#: Test-harness seam ONLY — see ``SendspinAdapter.allow_unpaired``. Never set
#: this on a running server: it opens a transport-control path that requires no
#: pairing at all.
ALLOW_UNPAIRED_FOR_TESTS = False

#: Role family that renders audio. A client may have none (a display-only screen).
PLAYER_ROLE_FAMILY = "player"

#: What jukeplox actually services. A speaker with extra capabilities — lighting,
#: for instance — connects and works fully for everything listed here; the rest
#: stays idle rather than being advertised and then ignored. Lighting is tracked
#: separately and deliberately absent.
IMPLEMENTED_ROLE_FAMILIES = ("player", "metadata", "artwork", "controller")

#: Transport commands jukeplox actually services. Shuffle, repeat and switch are
#: absent on purpose: a party queue has no such modes, and the protocol drops any
#: command that is not advertised, so claiming them would just produce silence.
SUPPORTED_COMMANDS = ("play", "pause", "next", "previous", "seek",
                      "seek_relative", "volume", "mute")

#: The three pairing methods the spec defines. In ALL of them the SERVER is the
#: initiator and the OPERATOR reads a code off the speaker and enters it here —
#: the reverse of the PSK-display flow this backend originally assumed.
PAIR_TOKEN = "pairing_psk"    # a token supplied with the device, pasted in
PAIR_STATIC_PIN = "static_pin"    # a fixed code printed on or shipped with it
PAIR_DYNAMIC_PIN = "dynamic_pin"   # a one-time code the device displays
PAIRING_METHODS = (PAIR_TOKEN, PAIR_STATIC_PIN, PAIR_DYNAMIC_PIN)


async def _maybe_await(res: Any) -> Any:
    """aiosendspin mixes sync and coroutine returns across its surface; await
    only when the call actually returned a coroutine/awaitable."""
    if asyncio.iscoroutine(res) or hasattr(res, "__await__"):
        return await res
    return res


class SendspinPairingStoreError(RuntimeError):
    """The persisted pairing store exists but cannot be opened."""


#: Settings-KV key holding the SEALED pairing store payload.
PAIRING_STORE_SETTING_KEY = "sendspin_pairing_store"


# ── sealed persistence (pure of aiosendspin — locally testable) ──────────────
#
# aiosendspin ships a concrete ``FileServerPairingStore`` that persists atomically
# to a JSON file. We reuse its record model and its 12-method implementation, and
# swap ONLY its two I/O primitives so the payload lands sealed in the settings
# table instead of readable on disk — the records are Noise key material, and
# sealing is what jukeplox already does for every other stored credential.
#
# These two helpers hold the seal/JSON/corruption logic and import nothing from
# aiosendspin, so they are exercisable on the local 3.11 interpreter; only the
# thin binding below is in-image-only.


async def load_sealed_pairing_blob() -> dict | None:
    """The persisted payload, or ``None`` when nothing has ever been stored.

    Raises ``SendspinPairingStoreError`` when a row EXISTS but will not open.
    That distinction is load-bearing: ``get_sealed_setting`` deliberately
    degrades to ``""`` on a lost or rotated key rather than raising, so a corrupt
    store is otherwise indistinguishable from a fresh one — and silently
    starting empty presents to the user as every speaker forgetting its pairing
    at once, with nothing to explain it.
    """
    from app import database

    raw = await database.get_setting(PAIRING_STORE_SETTING_KEY)
    if not raw:
        return None  # genuinely nothing stored yet
    opened = await database.get_sealed_setting(PAIRING_STORE_SETTING_KEY)
    if not opened:
        raise SendspinPairingStoreError(
            "the stored Sendspin pairing store could not be opened — the "
            "credential seal key looks lost or rotated. Existing pairings are "
            "unreadable; re-pair the speakers or restore the previous key.")
    try:
        data = json.loads(opened)
    except ValueError as exc:
        raise SendspinPairingStoreError(
            "the stored Sendspin pairing store could not be opened — the "
            "decrypted payload is not valid JSON") from exc
    if not isinstance(data, dict):
        raise SendspinPairingStoreError(
            "the stored Sendspin pairing store could not be opened — the "
            f"decrypted payload is a {type(data).__name__}, not an object")
    return data


async def save_sealed_pairing_blob(payload: dict) -> None:
    """Seal and persist the payload. An empty payload clears the row."""
    from app import database

    if not payload:
        await database.set_sealed_setting(PAIRING_STORE_SETTING_KEY, None)
        return
    await database.set_sealed_setting(
        PAIRING_STORE_SETTING_KEY, json.dumps(payload, separators=(",", ":")))


async def _make_pairing_store():
    """A ``ServerPairingStore`` whose records survive a restart and are sealed at
    rest. Subclasses the library's file-backed store and overrides ONLY its two
    I/O primitives, so the record model, the staging semantics and the 12
    abstract methods all stay the library's problem rather than ours.

    Defined function-local so importing this module never imports aiosendspin."""
    from aiosendspin.noise.trust_store import (
        FileServerPairingStore,
        ServerPairingRecord,
        StagedPairingPsk,
        TrustedUnpairedClient,
    )

    class _SealedServerPairingStore(FileServerPairingStore):
        async def _load(self) -> None:
            data = await load_sealed_pairing_blob()
            if data is None:
                return
            self._records = {
                cid: ServerPairingRecord.from_dict(v)
                for cid, v in (data.get("records") or {}).items()
            }
            self._staged = {
                cid: StagedPairingPsk.from_dict(v)
                for cid, v in (data.get("staged_pairing_psks") or {}).items()
            }
            self._trusted = {
                cid: TrustedUnpairedClient.from_dict(v)
                for cid, v in (data.get("trusted_unpaired_clients") or {}).items()
            }

        async def _save(self) -> None:
            async with self._lock:
                await save_sealed_pairing_blob({
                    "records": {c: r.to_dict() for c, r in self._records.items()},
                    "staged_pairing_psks":
                        {c: p.to_dict() for c, p in self._staged.items()},
                    "trusted_unpaired_clients":
                        {c: t.to_dict() for c, t in self._trusted.items()},
                })

    # The inherited __init__ wants a path. Nothing is ever read from or written
    # to it — both I/O primitives are overridden above — but passing the real
    # data dir keeps the object well-formed and makes the intent legible if a
    # future version grows a third file touchpoint.
    from app import database
    from pathlib import Path
    store = _SealedServerPairingStore(
        Path(database.settings.data_dir) / "sendspin_pairing.unused")
    await store._load()
    return store


class SendspinAdapter:
    """Stable façade over one running ``SendspinServer`` instance (real 9.1.0)."""

    def __init__(self, server: Any, audio_format: Any) -> None:
        self._server = server
        self._audio_format = audio_format
        self._group: Any = None
        self._stream: Any = None
        self._pairing_timers: dict[str, asyncio.Task] = {}
        self._transport_cb: Callable[[str, Any], Any] | None = None
        self._ctrl_map: tuple | None = None
        self._transport_tasks: set[asyncio.Task] = set()

    # ── lifecycle ───────────────────────────────────────────────────────────

    @classmethod
    async def start(
        cls,
        *,
        host: str,
        port: int = DEFAULT_PORT,
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
        store = await _make_pairing_store()
        # allow_unencrypted stays False (secure default). VERIFIED on the rig:
        # this constructs + start_server() binds 8927. Client onboarding still
        # needs the pairing protocol wired (see get_pairing_pin below).
        server = SendspinServer(loop, identity, "jukeplox", pairing_store=store)
        await _maybe_await(server.start_server(
            port=port, host=host, discover_clients=discover_clients))
        return cls(server, AudioFormat(SAMPLE_RATE, SAMPLE_BITS, CHANNELS))

    async def stop(self) -> None:
        for cid in list(self._pairing_timers):
            self._cancel_pairing_timer(cid)
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

    @property
    def api_path(self) -> str:
        """The WebSocket endpoint clients must dial. The spec carries this in
        the mDNS TXT record as ``path``; without it a client has to guess."""
        return str(getattr(self._server, "API_PATH", "") or "/")

    @staticmethod
    def _client_id(c: Any) -> str:
        return str(getattr(c, "client_id", None) or getattr(c, "id", "") or "")

    @staticmethod
    def _client_name(c: Any) -> str:
        return str(getattr(c, "name", None) or "sendspin-client")

    def _player_role(self, client: Any) -> Any:
        """The client's player role, or None for a device that renders no audio
        (a display-only screen is a legitimate Sendspin client)."""
        try:
            roles = client.roles_by_family(PLAYER_ROLE_FAMILY) or []
        except Exception:
            return None
        return roles[0] if roles else None

    def _role_for_id(self, client_id: str) -> Any:
        for c in getattr(self._server, "connected_clients", None) or []:
            if self._client_id(c) == client_id:
                return self._player_role(c)
        return None

    def clients(self) -> list[dict]:
        """CONNECTED clients only — the zoning surface, with REAL readback.

        This previously reported a constant 0.0 volume for every client. That is
        not merely a missing feature: group volume scales clients proportionally
        from their current levels, so a constant zero silently corrupts the
        arithmetic for the whole group."""
        raw = getattr(self._server, "connected_clients", None) or []
        out = []
        for c in raw:
            # Guard PER SPEAKER. This backs list_zones, discover_devices and
            # every group-volume calculation, and list_zones is served straight
            # out of an admin route — so one speaker vanishing mid-call must not
            # 500 the zoning panel and take master volume down with it.
            try:
                role = self._player_role(c)
                vol = role.get_player_volume() if role is not None else None
                muted = role.get_player_muted() if role is not None else None
                delay = int(role.get_static_delay_ms() or 0) if role is not None else 0
            except Exception:
                _log.debug("sendspin: reading speaker state failed", exc_info=True)
                vol, muted, delay = None, False, 0
            out.append({
                "id": self._client_id(c),
                "name": self._client_name(c),
                # The protocol carries volume as 0..100; jukeplox works in 0..1.
                # An UNKNOWN level stays None rather than becoming 0.0: this
                # dict is the input to proportional group volume, not just a
                # render, so a failed read that reported 0.0 would be written
                # straight back and genuinely silence the speaker — the exact
                # corruption the real readback was added to fix.
                "volume": (vol / 100.0) if vol is not None else None,
                "muted": bool(muted),
                "delay_ms": delay,
            })
        return out

    def discovered_clients(self) -> list[dict]:
        """EVERY client the server knows about — discovered-but-unpaired
        included. This is the pairing surface, and it is deliberately distinct
        from ``clients()``: you pair with something you can see but have not yet
        trusted, and you zone something already connected."""
        raw = getattr(self._server, "clients", None) or []
        out = []
        for c in raw:
            cid = self._client_id(c)
            if not cid:
                continue
            out.append({
                "id": cid,
                "name": self._client_name(c),
                "paired": bool(getattr(c, "is_paired", False)),
                "connected": bool(getattr(c, "is_connected", False)),
                "url": self.client_url(cid),
            })
        return out

    def client_url(self, client_id: str) -> str:
        try:
            return str(self._server.get_client_url(client_id) or "")
        except Exception:
            return ""

    async def connect_to_client(self, url: str) -> None:
        """Dial OUT to a client that advertised itself (server-initiated mode)."""
        await _maybe_await(self._server.connect_to_client(url))

    async def disconnect_client(self, client_id: str) -> None:
        """Drop the live session for a speaker.

        Closes the connection directly rather than going through the URL path:
        a client URL is only recorded for speakers WE dialled out to, so a
        speaker that connected inbound has none, and a URL-only teardown would
        silently no-op on exactly the sessions we most need to cut."""
        try:
            client = self._server.get_client(client_id)
        except Exception:
            client = None
        conn = getattr(client, "connection", None) if client is not None else None
        if conn is not None:
            try:
                await _maybe_await(conn.disconnect(retry_connection=False))
                return
            except TypeError:
                try:
                    await _maybe_await(conn.disconnect())
                    return
                except Exception:
                    _log.debug("sendspin: direct disconnect failed", exc_info=True)
            except Exception:
                _log.debug("sendspin: direct disconnect failed", exc_info=True)
        url = self.client_url(client_id)
        if url:
            try:
                await _maybe_await(self._server.disconnect_from_client(url))
            except Exception:
                _log.debug("sendspin: url disconnect failed", exc_info=True)

    def _drop_from_group(self, client_id: str) -> None:
        group = self._group
        if group is None:
            return
        try:
            members = list(group.clients or [])
            if len(members) <= 1 and any(
                    self._client_id(c) == client_id for c in members):
                # The library asserts a group holds at least one client, so the
                # last one is dropped by discarding the group, not by removing
                # from it.
                self._group = None
                return
            for c in members:
                if self._client_id(c) == client_id:
                    group.remove_client(c)
        except Exception:
            _log.debug("sendspin: removing a client from the group failed",
                       exc_info=True)

    # ── feed (group push stream) ─────────────────────────────────────────────

    def _ensure_group(self) -> Any:
        """One LONG-LIVED group, reconciled as speakers come and go.

        This used to build a brand-new group for every track. Group-level state
        — volume, mute, what is on the screens — lives on the group, so a fresh
        one each track would silently reset all of it at every boundary and
        re-form the sync set mid-queue."""
        connected = list(getattr(self._server, "connected_clients", None) or [])
        if not connected:
            # DROP the group rather than returning the existing one. Returning a
            # stale group here is what let the zero-speaker queue-drain P0 come
            # back after its first fix: once any speaker had ever connected,
            # self._group stayed non-None forever, so a later start_stream on an
            # empty room still produced a stream, has_stream() went true with
            # nobody listening, and the feed left its wall-clock pacing branch.
            # There is no group state worth preserving with zero clients.
            self._group = None
            return None
        if self._group is None:
            from aiosendspin.server.server import SendspinGroup
            self._group = SendspinGroup(self._server, *connected)
            return self._group
        try:
            members = list(self._group.clients or [])
        except Exception:
            # Membership unreadable — do NOT fall back to "assume empty", which
            # would re-add every connected speaker on every track.
            _log.debug("sendspin: group membership unreadable; skipping "
                       "reconciliation this pass", exc_info=True)
            return self._group
        present = {self._client_id(c) for c in members}
        live = {self._client_id(c) for c in connected}
        for c in connected:
            if self._client_id(c) not in present:
                try:
                    self._group.add_client(c)
                except Exception:
                    _log.warning("sendspin: adding a client to the group failed",
                                 exc_info=True)
        # Reconcile BOTH directions. Add-only leaves a departed speaker in the
        # group, and a reconnecting one then looks already-present to the guard
        # above — so it is skipped and stays silent for the rest of the session.
        for c in members:
            if self._client_id(c) not in live:
                try:
                    self._group.remove_client(c)
                except Exception:
                    _log.debug("sendspin: removing a departed client failed",
                               exc_info=True)
        return self._group

    def _group_role_for(self, family: str) -> Any:
        """``group_role`` is a METHOD keyed by role family, not a property."""
        group = self._group
        if group is None:
            return None
        try:
            return group.group_role(family)
        except Exception:
            return None

    def has_stream(self) -> bool:
        """Whether audio pushed now would actually reach anybody.

        Deliberately checks for a live speaker as well as a stream. Tying this
        to reality rather than to "did we once call start_stream" is what keeps
        the feed's wall-clock pacing engaged when the room empties out."""
        if self._stream is None:
            return False
        return bool(getattr(self._server, "connected_clients", None))

    async def _stop_stream(self) -> None:
        """Drop the current push stream. Called before starting another: the
        group is long-lived now, so without this every track, seek and resume
        would stack a new stream on it and leak the previous one."""
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            await _maybe_await(stream.stop())
        except Exception:
            _log.debug("sendspin: stopping the previous stream failed",
                       exc_info=True)

    async def start_stream(self) -> None:
        await self._stop_stream()
        group = self._ensure_group()
        if group is None:
            # No speakers yet. The caller keeps feeding and paces itself — a
            # zero-client server is audibly silent but NOT an outage.
            return
        self._stream = await _maybe_await(group.start_stream())

    async def reconcile_stream(self) -> None:
        """Keep the push stream in step with who is actually connected.

        Called from the feed loop, so it must handle BOTH directions: attach
        mid-track when a speaker arrives, and tear down when the room empties —
        otherwise a stale stream keeps the loop out of its pacing branch."""
        connected = bool(getattr(self._server, "connected_clients", None))
        if not connected:
            if self._stream is not None:
                await self._stop_stream()
            return
        if self._stream is not None:
            return
        group = self._ensure_group()
        if group is None:
            return
        stream = None
        try:
            stream = await _maybe_await(group.start_stream())
        except Exception:
            _log.debug("sendspin: mid-track stream attach failed", exc_info=True)
        finally:
            # Assigned in `finally` because CancelledError is a BaseException:
            # an `except Exception` would let a cancellation drop an already
            # started stream on the floor — leaking exactly what _stop_stream
            # exists to prevent.
            if stream is not None:
                self._stream = stream

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

    # ── now playing: what the screens show ───────────────────────────────────
    #
    # Progress travels as an ANCHOR, not a tick: the role carries position,
    # duration and playback speed together with an explicit freeze, so the
    # client extrapolates the moving part itself. Pushing a position every
    # second would be needless traffic to battery-powered devices.

    async def set_now_playing(self, *, title: str = "", artist: str = "",
                              album: str = "", album_artist: str = "",
                              duration_ms: int = 0, progress_ms: int = 0) -> None:
        self._ensure_group()
        role = self._group_role_for("metadata")
        if role is None:
            return
        await _maybe_await(role.update(
            title=title or None,
            artist=artist or None,
            album=album or None,
            album_artist=album_artist or None,
            track_duration=int(duration_ms) or None,
            track_progress=int(progress_ms),
            playback_speed=1,
        ))

    async def freeze_progress(self) -> None:
        role = self._group_role_for("metadata")
        if role is not None:
            await _maybe_await(role.freeze_progress())

    async def clear_now_playing(self) -> None:
        role = self._group_role_for("metadata")
        if role is not None:
            await _maybe_await(role.clear())
        await self.set_album_artwork(None)

    async def set_album_artwork(self, image_bytes: bytes | None) -> None:
        """Push cover art to screens, or clear it when there is none.

        Bytes in, decoding here: the role wants a decoded image, and keeping
        Pillow behind this façade means the backend never imports it."""
        self._ensure_group()
        role = self._group_role_for("artwork")
        if role is None:
            return
        image = None
        if image_bytes:
            import io
            # Imported outside the decode guard so a MISSING Pillow logs as a
            # missing dependency rather than masquerading as a corrupt image.
            from PIL import Image
            try:
                image = Image.open(io.BytesIO(image_bytes))
                image.load()
            except Exception:
                _log.warning("sendspin: could not decode album art", exc_info=True)
                image = None
        await _maybe_await(role.set_album_artwork(image))

    def implemented_role_families(self) -> tuple[str, ...]:
        return IMPLEMENTED_ROLE_FAMILIES

    # ── transport commands coming FROM a speaker ─────────────────────────────
    #
    # The library validates an inbound command against the supported list,
    # handles volume and mute itself, and emits an event for the application.
    # Two consequences worth knowing: a command absent from the supported list
    # is dropped with a warning, and SEEK is discarded entirely unless a seek
    # ceiling has been set for the current track.

    async def configure_controls(self, *, seek_max_ms: int | None) -> None:
        from aiosendspin.server.roles.controller.group import MediaCommand
        self._ensure_group()
        role = self._group_role_for("controller")
        if role is None:
            return
        await _maybe_await(role.set_supported_commands(
            [MediaCommand(c) for c in SUPPORTED_COMMANDS]))
        await _maybe_await(role.set_seek_max_ms(seek_max_ms))

    def _controller_map(self) -> tuple:
        """(event class, action, payload attribute) — built once, on demand."""
        if self._ctrl_map is None:
            from aiosendspin.server.roles.controller import events as E
            self._ctrl_map = (
                (E.ControllerPlayEvent, "play", None),
                (E.ControllerPauseEvent, "pause", None),
                (E.ControllerNextEvent, "next", None),
                (E.ControllerPreviousEvent, "previous", None),
                (E.ControllerSeekEvent, "seek", "position_ms"),
                (E.ControllerSeekRelativeEvent, "seek_relative", "offset_ms"),
                (E.ControllerVolumeEvent, "volume", "volume"),
                (E.ControllerMuteEvent, "mute", "muted"),
            )
        return self._ctrl_map

    def set_transport_handler(self, cb: Callable[[str, Any], Any]) -> None:
        """Register ``cb(action, value)``. The backend never sees library types."""
        self._transport_cb = cb
        self._server.add_event_listener(self._on_transport_event)

    def _on_transport_event(self, event: Any = None, *_rest: Any) -> None:
        cb = self._transport_cb
        if cb is None or event is None:
            return
        for cls, action, attr in self._controller_map():
            if isinstance(event, cls):
                value = getattr(event, attr, None) if attr else None
                try:
                    res = cb(action, value)
                except Exception:
                    _log.warning("sendspin: transport handler failed",
                                 exc_info=True)
                    return
                if asyncio.iscoroutine(res):
                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        _log.debug("sendspin: no running loop for a transport "
                                   "command; dropping it")
                        res.close()
                        return
                    # Hold a reference. A bare create_task() is only weakly held
                    # by the loop, so a command can be garbage-collected
                    # mid-flight — a skip that simply never happens.
                    task = loop.create_task(res)
                    self._transport_tasks.add(task)
                    task.add_done_callback(self._transport_tasks.discard)
                return

    # ── per-client volume + pairing — SETTLE-TIME (design mismatch) ──────────

    # ── per-client volume / mute / delay (player role) ───────────────────────
    #
    # These are genuinely per-client in 9.1.0. The earlier belief that the
    # library offered group-level control only came from finding the group role
    # first and not looking further.
    #
    # NOTE on group volume: the library also has a real group-level volume, but
    # jukeplox deliberately does NOT use it. Snapcast's group slider scales its
    # clients PROPORTIONALLY, preserving the relative balance between rooms; a
    # single group level would flatten that. Parity with Snapcast is a stated
    # requirement, so group volume stays a proportional fan-out over these
    # per-client writes.

    async def set_client_volume(self, client_id: str, level: float) -> None:
        role = self._role_for_id(client_id)
        if role is None:
            return
        pct = int(round(max(0.0, min(1.0, level)) * 100))
        await _maybe_await(role.set_player_volume(pct))

    async def set_client_mute(self, client_id: str, muted: bool) -> None:
        role = self._role_for_id(client_id)
        if role is None:
            return
        await _maybe_await(role.set_player_mute(bool(muted)))

    async def set_client_delay(self, client_id: str, delay_ms: int) -> None:
        """Per-room latency trim. A speaker further from the listener, or behind
        a slower DAC, needs its own offset to stay in time with the others."""
        role = self._role_for_id(client_id)
        if role is None:
            return
        await _maybe_await(role.set_static_delay(int(delay_ms)))

    # ── pairing ──────────────────────────────────────────────────────────────
    #
    # In every method the spec defines, the SERVER initiates and the OPERATOR
    # reads a code off the speaker and enters it here. jukeplox never displays a
    # code for you to type into the speaker — that flow does not exist in this
    # protocol, and the original admin screen had it backwards.

    @staticmethod
    def plan_pairing(*, method: str, code: str, client_id: str,
                     discovered: list[dict]) -> tuple[str, str, str]:
        """Decide how to reach a speaker, or explain why we cannot.

        Pure of aiosendspin so the rules that reject an operator's input — and
        the choice between talking over an existing connection and dialling out
        — are testable on the local interpreter rather than only in-image.

        Returns ``(route, target, url)`` where route is ``"initiate"`` or
        ``"dial"``. Raises ``ValueError`` with operator-facing wording."""
        code = (code or "").strip()
        if method not in PAIRING_METHODS:
            raise ValueError(f"unknown pairing method {method!r}")
        if not code:
            raise ValueError("a pairing code is required")
        target = (client_id or "").strip()
        if method != PAIR_TOKEN and not target:
            raise ValueError("choose a speaker to pair with")

        entry = next((c for c in discovered if c["id"] == target), None)
        # The Noise handshake runs over a LIVE connection, which leaves two
        # routes and no third:
        #
        #   already connected → register the attempt on that connection.
        #   advertised itself → dial out WITH the attempt attached. This is the
        #     normal route for a brand-new speaker: an unknown client dialling
        #     IN is refused before pairing can even begin, so onboarding
        #     generally happens server-initiated.
        if entry is not None and entry["connected"]:
            return ("initiate", target, "")
        if entry is not None and entry["url"]:
            return ("dial", target, entry["url"])
        raise ValueError(
            "that speaker is not reachable — switch it on and wait for it "
            "to appear on the network, then pair")

    async def begin_pairing(self, *, method: str, code: str,
                            client_id: str = "",
                            timeout_s: float = 120.0) -> str:
        """Register a pairing intent and return the speaker id it targets.

        A pairing token carries the speaker's identity itself, so no speaker
        needs choosing; the two PIN methods pair with a speaker you select."""
        from aiosendspin.noise.pairing import PairingAttempt
        from aiosendspin.noise.pairing_token import decode_token
        from aiosendspin.noise.trust_store import PairMethod, is_valid_static_pin

        code = (code or "").strip()
        if method == PAIR_TOKEN and code:
            # The token names its own speaker, so decoding replaces the
            # operator's selection.
            try:
                tok = decode_token(code)
            except Exception as exc:
                raise ValueError("that pairing token could not be read") from exc
            client_id, token_psk = tok.client_id, tok.pairing_psk
        else:
            token_psk = None
        if method == PAIR_STATIC_PIN and code and not is_valid_static_pin(code):
            raise ValueError("a fixed device code is exactly 8 digits")

        route, target, url = self.plan_pairing(
            method=method, code=code, client_id=client_id,
            discovered=self.discovered_clients())

        if token_psk is not None:
            attempt = PairingAttempt(method=PairMethod.PAIRING_PSK,
                                     pairing_psk=token_psk)
        else:
            async def _pin() -> str:
                return code

            attempt = PairingAttempt(method=PairMethod(method), pin_provider=_pin)

        if route == "initiate":
            await _maybe_await(self._server.initiate_pairing(target, attempt))
        else:
            await _maybe_await(self._server.connect_to_client(
                url, pairing_attempt=attempt))

        self._arm_pairing_timeout(target, timeout_s)
        return target

    async def allow_unpaired(self, client_id: str) -> None:
        """Let one named, not-yet-trusted speaker complete a connection.

        **Refused in production.** A trusted-unpaired grant is not a lesser
        form of pairing: the controller role carries no pairing requirement, so
        a trusted-unpaired speaker gets FULL transport control with no pairing
        at all. The grant also persists, is invisible to the paired list, and
        survives unpair — a second authorisation path that cannot be seen or
        revoked from the admin panel.

        It exists solely so the in-image end-to-end test can connect a client
        without completing a cryptographic handshake, and the test opts in
        explicitly. Enabling it on a running server would put an
        unauthenticated transport path on the LAN."""
        if not ALLOW_UNPAIRED_FOR_TESTS:
            raise RuntimeError(
                "trusted-unpaired access grants full transport control without "
                "pairing and is refused outside the test harness")
        await _maybe_await(self._server.trust_unpaired(client_id))

    async def revoke_unpaired(self, client_id: str) -> None:
        """Withdraw a trusted-unpaired grant. Safe to call unconditionally —
        revocation is never gated, only granting is."""
        try:
            await _maybe_await(self._server.untrust_unpaired(client_id))
        except Exception:
            _log.debug("sendspin: no trusted-unpaired grant to revoke",
                       exc_info=True)

    def _arm_pairing_timeout(self, client_id: str, timeout_s: float) -> None:
        """A speaker that never completes must not leave an attempt pending
        forever — that would block the operator from retrying."""
        self._cancel_pairing_timer(client_id)

        async def _expire() -> None:
            try:
                await asyncio.sleep(timeout_s)
            except asyncio.CancelledError:
                return
            # Pairing may have SUCCEEDED while this timer ran. Tearing down a
            # working speaker two minutes after it paired would look like a
            # random mid-track dropout, so check before reaping. (Previously
            # masked by the self-cancel bug above — these two only make sense
            # fixed together.)
            try:
                if any(p["id"] == client_id for p in await self.paired_clients()):
                    _log.debug("sendspin: %s paired before the window closed",
                               client_id)
                    return
            except Exception:
                _log.debug("sendspin: could not confirm pairing state",
                           exc_info=True)
            _log.info("sendspin: pairing attempt for %s timed out", client_id)
            try:
                await self.end_pairing(client_id)
                await self.revoke_unpaired(client_id)
            except Exception:
                _log.debug("sendspin: expiring pairing attempt failed",
                           exc_info=True)

        self._pairing_timers[client_id] = (
            asyncio.get_running_loop().create_task(_expire()))

    def _cancel_pairing_timer(self, client_id: str) -> None:
        t = self._pairing_timers.pop(client_id, None)
        if t is None or t.done():
            return
        # NEVER cancel the task we are running inside. The expiry task calls
        # end_pairing, which calls this — cancelling itself here kills the
        # cleanup at its first suspension point, and because CancelledError is
        # a BaseException the expiry task's own `except Exception` cannot even
        # log it. The one cleanup path in the pairing system would silently
        # never complete.
        if t is asyncio.current_task():
            return
        t.cancel()

    async def end_pairing(self, client_id: str) -> None:
        self._cancel_pairing_timer(client_id)
        await _maybe_await(self._server.end_pairing(client_id))

    async def paired_clients(self) -> list[dict]:
        """Speakers with a stored pairing record — the revocation surface.

        The store's accessors are async, so this must be awaited; calling it
        synchronously silently yields a coroutine rather than records."""
        store = getattr(self._server, "pairing_store", None)
        if store is None:
            return []
        names = {c["id"]: c["name"] for c in self.discovered_clients()}
        out = []
        for rec in (await _maybe_await(store.list_records()) or []):
            cid = str(getattr(rec, "client_id", "") or "")
            if not cid:
                continue
            out.append({"id": cid, "name": names.get(cid, "sendspin-client")})
        return out

    async def unpair(self, client_id: str) -> None:
        """Revoke a speaker: drop its stored record AND end its live session.

        STORE FIRST, deliberately. The library's ``unpair`` resolves a live
        connection before it removes the record, so it raises for a speaker that
        is currently offline — which is precisely the case revocation exists
        for: a speaker that has been taken, lost, or handed on. Leaving the
        record behind would hand full transport control back the moment it
        reappeared. Removing the record ourselves first makes revocation
        unconditional; the connection teardown is then best-effort."""
        store = getattr(self._server, "pairing_store", None)
        if store is None:
            # Reporting success here would tell the operator a speaker was
            # revoked when its key is still on disk. Revocation must never
            # silently no-op.
            raise RuntimeError(
                "no pairing store available — the speaker was NOT revoked")
        # Deliberately unguarded: a failure to remove the record means
        # revocation did not happen, and the caller must hear about it.
        await _maybe_await(store.remove_record(client_id))

        # End any PENDING pairing attempt, and do it through end_pairing rather
        # than just cancelling the timer. An attempt is precisely what lets a
        # client complete a handshake, so leaving one registered — while
        # cancelling the task that would have reaped it — would let the speaker
        # we just revoked re-pair itself with the code it already holds.
        try:
            await self.end_pairing(client_id)
        except Exception:
            _log.warning("sendspin: could not end a pending pairing attempt "
                         "for a revoked speaker", exc_info=True)
        # Also drop any standing trusted-unpaired grant — otherwise a revoked
        # speaker could still reconnect through that separate door.
        await self.revoke_unpaired(client_id)
        try:
            await _maybe_await(self._server.unpair(client_id))
        except Exception:
            # Offline speaker (no live connection to notify) — the record is
            # already gone, which is the part that matters.
            _log.debug("sendspin: no live session to notify on unpair",
                       exc_info=True)
        await self.disconnect_client(client_id)
        self._drop_from_group(client_id)

        # Confirm rather than assume. This is the only security control a
        # Sendspin speaker has, and a revocation that quietly did nothing while
        # the UI says "unpaired" is worse than one that fails loudly.
        if any(p["id"] == client_id for p in await self.paired_clients()):
            raise RuntimeError(
                f"revocation did not take effect for {client_id} — its pairing "
                "record is still present")


async def default_adapter_factory(*, host: str, port: int) -> SendspinAdapter:
    return await SendspinAdapter.start(host=host, port=port)
