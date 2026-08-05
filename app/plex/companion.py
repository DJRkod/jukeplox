"""Plex Companion protocol client (2026-08-04-002 plan, U1).

Two self-contained HTTP surfaces, no jukeplox lifecycle coupling:

``CompanionPlayerClient``
    Talks plain HTTP directly to a Companion receiver (Caldera headless,
    Plexamp, ...) at ``http://{host}:{port}``: the modern player-side
    ``createPlayQueue`` dispatch plus transport commands and the
    ``timeline/poll`` observation channel. One instance per (controller,
    player) pair — it owns the monotonically increasing ``commandID``
    counter the protocol requires, so callers must build per-device
    instances (R10: no module-level singletons).

``PmsCompanionClient``
    The PMS-side helpers Companion control needs from the *server*:
    ``GET /clients`` enumeration (capability parsing — eligibility gates on
    ``playqueues-creation``) and play-queue window reads / append / remove
    for gapless arming.

Protocol references: plex-media-player wiki "Remote-control-API";
python-plexapi ``client.py``/``playqueue.py`` request shapes; the Plex
forum Caldera thread (elan-confirmed player-side ``createPlayQueue`` with
``source={server machineIdentifier}`` and NO ``providerIdentifier`` param —
legacy ``playMedia`` fails on Caldera trying to resolve the provider).

Parsing uses stdlib ``xml.etree.ElementTree`` only (the DLNA precedent).
Commands tolerate 200 responses with plain-text bodies ("OK") — the
Plexamp family does not return XML on success — by never parsing command
response bodies at all; only the timeline poll and PMS play-queue reads
parse XML.

Every failure is logged through :func:`_redact` (mirror of
``app.output.flow._redact``, duplicated here to keep app/plex free of an
output-layer import): URL query strings are credential-bearing (PMS URLs
ride ``?X-Plex-Token=…``) and are scrubbed before any log write.
"""

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.plex import auth as plex_auth

_log = logging.getLogger(__name__)

# The capability that gates v1 device eligibility (Key Technical Decisions):
# players lacking it would need the legacy playMedia path we deliberately
# don't ship.
PLAYQUEUES_CREATION = "playqueues-creation"

# House convention (see PlexClient / dead-plex-server lesson): a blackholed
# host must fail the CONNECT phase in seconds, not burn the read budget.
_TIMEOUT = httpx.Timeout(15, connect=5)
# timeline/poll?wait=1 is a long poll — the player holds the request until
# the next timeline tick. Give the read leg more headroom than a plain GET.
_LONG_POLL_READ = 35.0


# ── log redaction (mirror of app.output.flow._redact) ─────────────────────────
# Any URL query string is treated as credential-bearing (Plex rides
# ``?X-Plex-Token=…`` on server URLs) and scrubbed before logging.
_URL_QUERY_RE = re.compile(r"(https?://[^\s'\"?]+)\?[^\s'\"]*")


def _redact(text: Any) -> str:
    """Scrub credential-bearing URL query strings from text bound for the
    logs — httpx error text echoes full request URLs."""
    return _URL_QUERY_RE.sub(r"\1?<redacted>", str(text))


# ── typed errors ──────────────────────────────────────────────────────────────

class CompanionError(Exception):
    """Base for all Companion protocol failures."""
    retryable = False


class CompanionUnreachableError(CompanionError):
    """Connect/timeout/transport failure — the peer may come back; retryable."""
    retryable = True


class CompanionTargetMismatchError(CompanionError):
    """Player returned 404: the X-Plex-Target-Client-Identifier we sent does
    not match the device answering at this address (spec-mandated response).
    The registry's address for this machineIdentifier is stale."""


class CompanionParseError(CompanionError):
    """A response body that must be XML (timeline, play-queue window) wasn't."""


class CompanionRequestError(CompanionError):
    """Non-2xx response that isn't the typed 404 target mismatch."""

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


# ── typed payloads ────────────────────────────────────────────────────────────

def _opt_int(value: Any) -> int | None:
    """Plex XML attributes as optional ints — absent/malformed → None,
    never a raise (idle/stopped timelines omit most fields)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class TimelineSnapshot:
    """One parsed ``timeline/poll`` response (the music timeline).

    Every field except ``continuing`` degrades to None when the attribute is
    absent — a stopped/idle player legitimately reports almost nothing."""
    state: str | None = None            # stopped|paused|playing|buffering|error
    time: int | None = None             # position, ms
    duration: int | None = None         # ms
    key: str | None = None
    rating_key: str | None = None
    container_key: str | None = None
    machine_identifier: str | None = None   # owning server
    address: str | None = None
    port: int | None = None
    volume: int | None = None           # 0-100
    play_queue_id: int | None = None
    play_queue_item_id: int | None = None
    play_queue_version: int | None = None
    command_id: int | None = None       # player's last-completed commandID echo
    continuing: bool = False            # stopped-between-items marker


@dataclass(frozen=True)
class CompanionPlayer:
    """One ``GET /clients`` entry."""
    name: str
    machine_identifier: str
    address: str
    port: int
    product: str
    protocol_capabilities: frozenset[str] = field(default_factory=frozenset)

    @property
    def supports_playqueues_creation(self) -> bool:
        """v1 eligibility gate: can this player create its own play queue?"""
        return PLAYQUEUES_CREATION in self.protocol_capabilities


@dataclass(frozen=True)
class PlayQueueItem:
    play_queue_item_id: int
    rating_key: str | None = None
    key: str | None = None
    title: str | None = None
    duration_ms: int | None = None


@dataclass(frozen=True)
class PlayQueueWindow:
    """A windowed ``GET/PUT/DELETE /playQueues/...`` response."""
    play_queue_id: int | None = None
    version: int | None = None
    selected_item_id: int | None = None
    selected_item_offset: int | None = None
    total_count: int | None = None
    source_uri: str | None = None
    items: tuple[PlayQueueItem, ...] = ()


# ── uri / coordinate helpers ──────────────────────────────────────────────────

def server_item_uri(server_machine_id: str, key: str) -> str:
    """The createPlayQueue uri form: the player asks the *server* to build the
    queue from a library item. ``key`` is the bare metadata key, e.g.
    ``/library/metadata/42``."""
    return f"server://{server_machine_id}/com.plexapp.plugins.library{key}"


def server_coordinates(server_url: str) -> tuple[str, str, int]:
    """Split a PMS base URL into the (protocol, address, port) triple the
    createPlayQueue params carry. Default ports follow the scheme."""
    parts = urlsplit(server_url)
    protocol = parts.scheme or "http"
    port = parts.port or (443 if protocol == "https" else 80)
    return protocol, parts.hostname or "", port


def _identity_headers(controller_id: str) -> dict:
    """The app's one product identity (X-Plex-Product/Version/Platform +
    client id), reused from app.plex.auth so it can never fork. The JSON
    Accept header is dropped: Companion peers speak XML (or plain text)."""
    headers = dict(plex_auth._headers(controller_id))
    headers.pop("Accept", None)
    return headers


# ── shared transport helper ───────────────────────────────────────────────────

async def _request(
    http: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    params: dict | None,
    headers: dict,
    timeout: httpx.Timeout | None = None,
    target_mismatch_404: bool = False,
) -> httpx.Response:
    """One HTTP exchange with the shared error contract: transport failures
    → retryable CompanionUnreachableError; 404 → typed target mismatch when
    talking to a player; other non-2xx → CompanionRequestError. Every failure
    path logs through _redact — never silently swallowed."""
    kwargs: dict = {"params": params, "headers": headers}
    if timeout is not None:
        kwargs["timeout"] = timeout
    try:
        resp = await http.request(method, url, **kwargs)
    except httpx.TransportError as exc:
        _log.warning("Companion %s %s failed: %s",
                     method, _redact(url), _redact(exc))
        raise CompanionUnreachableError(
            f"{method} {_redact(url)}: {_redact(exc)}") from exc
    if resp.status_code == 404 and target_mismatch_404:
        _log.warning("Companion %s %s: 404 target-identifier mismatch",
                     method, _redact(url))
        raise CompanionTargetMismatchError(
            f"player at {_redact(url)} rejected the target client identifier "
            "(404) — stale address for this machineIdentifier?")
    if resp.status_code >= 400:
        _log.warning("Companion %s %s: HTTP %s",
                     method, _redact(url), resp.status_code)
        raise CompanionRequestError(
            f"{method} {_redact(url)}: HTTP {resp.status_code}",
            status_code=resp.status_code)
    return resp


# ── XML parsers (stdlib ElementTree only) ─────────────────────────────────────

def parse_timeline(payload: bytes | str) -> TimelineSnapshot:
    """Parse a timeline/poll body into a TimelineSnapshot.

    Selects the ``type="music"`` timeline (falling back to the first Timeline
    element — some players omit type on the only line they send). Missing
    attributes parse to None; malformed XML raises CompanionParseError."""
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise CompanionParseError(f"malformed timeline XML: {exc}") from exc
    container_command_id = _opt_int(root.get("commandID"))
    timelines = root.findall("Timeline")
    tl = next((t for t in timelines if t.get("type") == "music"), None)
    if tl is None:
        tl = timelines[0] if timelines else None
    if tl is None:
        return TimelineSnapshot(command_id=container_command_id)
    return TimelineSnapshot(
        state=tl.get("state"),
        time=_opt_int(tl.get("time")),
        duration=_opt_int(tl.get("duration")),
        key=tl.get("key"),
        rating_key=tl.get("ratingKey"),
        container_key=tl.get("containerKey"),
        machine_identifier=tl.get("machineIdentifier"),
        address=tl.get("address"),
        port=_opt_int(tl.get("port")),
        volume=_opt_int(tl.get("volume")),
        play_queue_id=_opt_int(tl.get("playQueueID")),
        play_queue_item_id=_opt_int(tl.get("playQueueItemID")),
        play_queue_version=_opt_int(tl.get("playQueueVersion")),
        command_id=(container_command_id
                    if container_command_id is not None
                    else _opt_int(tl.get("commandID"))),
        continuing=tl.get("continuing") == "1",
    )


def parse_clients(payload: bytes | str) -> list[CompanionPlayer]:
    """Parse a PMS ``GET /clients`` body into CompanionPlayer entries.

    PMS emits ``<Server>`` children (historical tag name); ``<Player>`` is
    tolerated for the /resources shape. Entries without the addressing
    essentials (machineIdentifier, host, port) are skipped with a log line,
    never a raise."""
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise CompanionParseError(f"malformed /clients XML: {exc}") from exc
    players: list[CompanionPlayer] = []
    for el in root:
        if el.tag not in ("Server", "Player"):
            continue
        machine_id = el.get("machineIdentifier")
        address = el.get("host") or el.get("address")
        port = _opt_int(el.get("port"))
        if not machine_id or not address or port is None:
            _log.info("Companion /clients entry skipped (incomplete): %s",
                      _redact(dict(el.attrib)))
            continue
        caps = frozenset(
            c.strip()
            for c in (el.get("protocolCapabilities") or "").split(",")
            if c.strip()
        )
        players.append(CompanionPlayer(
            name=el.get("name") or el.get("title") or "",
            machine_identifier=machine_id,
            address=address,
            port=port,
            product=el.get("product") or "",
            protocol_capabilities=caps,
        ))
    return players


def parse_play_queue(payload: bytes | str) -> PlayQueueWindow:
    """Parse a ``/playQueues/...`` MediaContainer into a PlayQueueWindow.
    Items are any children carrying a playQueueItemID (Track elements for
    audio)."""
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise CompanionParseError(f"malformed playQueue XML: {exc}") from exc
    items = []
    for el in root:
        item_id = _opt_int(el.get("playQueueItemID"))
        if item_id is None:
            continue
        items.append(PlayQueueItem(
            play_queue_item_id=item_id,
            rating_key=el.get("ratingKey"),
            key=el.get("key"),
            title=el.get("title"),
            duration_ms=_opt_int(el.get("duration")),
        ))
    return PlayQueueWindow(
        play_queue_id=_opt_int(root.get("playQueueID")),
        version=_opt_int(root.get("playQueueVersion")),
        selected_item_id=_opt_int(root.get("playQueueSelectedItemID")),
        selected_item_offset=_opt_int(root.get("playQueueSelectedItemOffset")),
        total_count=_opt_int(root.get("playQueueTotalCount")),
        source_uri=root.get("playQueueSourceURI"),
        items=tuple(items),
    )


# ── player-side client ────────────────────────────────────────────────────────

class CompanionPlayerClient:
    """Direct HTTP command surface for one Companion receiver.

    One instance per (controller, player) pair: it owns the per-player
    monotonic commandID counter (incremented on *every* command including
    timeline polls, per spec). Command responses are never body-parsed, so
    the Plexamp-family plain-text "OK" 200s are tolerated by construction.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        target_machine_id: str,
        controller_id: str,
        token: str,
        device_name: str | None = None,
        http: httpx.AsyncClient | None = None,
    ):
        self.host = host
        self.port = port
        self.target_machine_id = target_machine_id
        self._base_url = f"http://{host}:{port}"
        identity = _identity_headers(controller_id)
        self._headers = {
            **identity,
            "X-Plex-Device-Name": device_name or identity["X-Plex-Product"],
            "X-Plex-Target-Client-Identifier": target_machine_id,
            "X-Plex-Token": token,
        }
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=_TIMEOUT, http2=False)
        self._command_id = 0

    @property
    def command_id(self) -> int:
        """Last commandID sent — U2's staleness filter compares timeline
        echoes against this."""
        return self._command_id

    def _next_command_id(self) -> int:
        self._command_id += 1
        return self._command_id

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _command(
        self,
        path: str,
        params: dict | None = None,
        *,
        timeout: httpx.Timeout | None = None,
    ) -> httpx.Response:
        query = dict(params or {})
        query["commandID"] = self._next_command_id()
        return await _request(
            self._http, "GET", f"{self._base_url}/player/{path}",
            params=query, headers=self._headers,
            timeout=timeout, target_mismatch_404=True,
        )

    # ── dispatch ──────────────────────────────────────────────────────────

    async def create_play_queue(
        self,
        *,
        server_machine_id: str,
        key: str,
        server_protocol: str,
        server_address: str,
        server_port: int,
    ) -> None:
        """The modern player-side dispatch: one command, the player asks the
        owning server to create a play queue it then owns and advances.

        Param set is the elan-confirmed Caldera/Plexamp shape: uri in
        server:// form, ``source={server machineIdentifier}``, coordinates of
        the OWNING server, and deliberately NO ``providerIdentifier`` param
        (Caldera tries to resolve it as a provider and fails)."""
        await self._command("playback/createPlayQueue", {
            "uri": server_item_uri(server_machine_id, key),
            "type": "audio",
            "shuffle": 0,
            "repeat": 0,
            "continuous": 0,
            "offset": 0,
            "protocol": server_protocol,
            "address": server_address,
            "port": server_port,
            "machineIdentifier": server_machine_id,
            "source": server_machine_id,
        })

    # ── transport controls (type=music: the Companion media-type argument) ─

    async def play(self) -> None:
        await self._command("playback/play", {"type": "music"})

    async def pause(self) -> None:
        await self._command("playback/pause", {"type": "music"})

    async def stop(self) -> None:
        await self._command("playback/stop", {"type": "music"})

    async def seek_to(self, offset_ms: int) -> None:
        await self._command("playback/seekTo",
                            {"offset": offset_ms, "type": "music"})

    async def set_parameters(self, *, volume: int | None = None) -> None:
        params: dict = {"type": "music"}
        if volume is not None:
            params["volume"] = volume
        await self._command("playback/setParameters", params)

    async def refresh_play_queue(self, play_queue_id: int) -> None:
        """Nudge the player to refetch its (server-side) play queue after a
        controller mutation — the gapless-arm second half."""
        await self._command("playback/refreshPlayQueue",
                            {"playQueueID": play_queue_id, "type": "music"})

    # ── observation ───────────────────────────────────────────────────────

    async def poll_timeline(self, wait: int = 0) -> TimelineSnapshot:
        """One timeline read. ``wait=1`` long-polls (player holds the request
        until its next timeline tick) with a widened read timeout."""
        timeout = httpx.Timeout(_LONG_POLL_READ, connect=5) if wait else None
        resp = await self._command("timeline/poll", {"wait": wait},
                                   timeout=timeout)
        return parse_timeline(resp.content)


# ── PMS-side helpers ──────────────────────────────────────────────────────────

class PmsCompanionClient:
    """The server-side reads/mutations Companion control needs from a PMS:
    player enumeration (``/clients``) and play-queue window ops for gapless
    arming. Deliberately separate from PlexClient — these endpoints speak
    XML and carry no library caching concerns."""

    def __init__(
        self,
        server_url: str,
        token: str,
        client_id: str,
        *,
        http: httpx.AsyncClient | None = None,
    ):
        self.server_url = server_url.rstrip("/")
        self._headers = {
            **_identity_headers(client_id),
            "X-Plex-Token": token,
        }
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=_TIMEOUT, http2=False)

    @classmethod
    def from_plex_client(cls, plex_client) -> "PmsCompanionClient":
        """Build from an existing app.plex.client.PlexClient — the server
        coordinates/token/client-id it already exposes are all we need."""
        return cls(plex_client.server_url, plex_client.token,
                   plex_client.client_id)

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _pms(self, method: str, path: str,
                   params: dict | None = None) -> httpx.Response:
        return await _request(
            self._http, method, f"{self.server_url}{path}",
            params=params, headers=self._headers,
        )

    async def get_clients(self) -> list[CompanionPlayer]:
        resp = await self._pms("GET", "/clients")
        return parse_clients(resp.content)

    async def get_play_queue(
        self,
        play_queue_id: int,
        *,
        window: int = 50,
    ) -> PlayQueueWindow:
        params: dict = {
            "window": window,
            "own": 0,
            "includeBefore": 1,
            "includeAfter": 1,
        }
        resp = await self._pms("GET", f"/playQueues/{play_queue_id}", params)
        return parse_play_queue(resp.content)

    async def append_to_play_queue(
        self,
        play_queue_id: int,
        uri: str,
        *,
        play_next: bool = False,
    ) -> PlayQueueWindow:
        """PUT-append an item (uri in :func:`server_item_uri` form — the
        same shape the createPlayQueue dispatch uses; section uuids for the
        plexapi ``library://`` form are unobtainable through app/plex) to
        the queue's Up Next region; ``play_next=True`` fronts the region."""
        params: dict = {"uri": uri}
        if play_next:
            params["next"] = 1
        resp = await self._pms("PUT", f"/playQueues/{play_queue_id}", params)
        return parse_play_queue(resp.content)

    async def delete_play_queue_item(
        self,
        play_queue_id: int,
        item_id: int,
    ) -> PlayQueueWindow:
        resp = await self._pms(
            "DELETE", f"/playQueues/{play_queue_id}/items/{item_id}")
        return parse_play_queue(resp.content)
