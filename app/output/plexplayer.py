"""Plex Companion player output backend (2026-08-04-002 plan, U2).

Drives Plex Companion receivers (Caldera headless, Plexamp, ...) through the
modern player-side ``createPlayQueue`` flow: one command to the player, the
player asks its OWNING server to build a play queue it then owns and
advances, and jukeplox observes a ``timeline/poll`` long-poll for confirmed
start, position, and terminal states. Protocol layer:
:mod:`app.plex.companion` (U1) — this module adds only jukeplox lifecycle.

Why ``stream_url`` is ignored
    Unlike every other backend, this device class plays *Plex library
    items*, not arbitrary stream URLs. ``play(stream_url, metadata)`` keeps
    the ``AbstractOutputBackend`` signature (the router/state contract for
    the existing backends is untouched), but the dispatch is derived from
    the holder key / ``metadata.id`` — never from the URL.

Holder handshake (single selection authority)
    The state layer owns holder selection (``_holder_keys`` ordering,
    same-server-first — U4); this backend NEVER re-picks. The dispatch path
    calls :meth:`PlexPlayerBackend.set_dispatch_holder` with the current
    attempt's holder key immediately before ``play()`` (U3 wires that call);
    ``play()`` consumes it one-shot. With no handed key (native single-Plex
    path) the backend falls back to parsing ``metadata.id``
    (``"{machine_id}:{ratingKey}"``). A catalog-path holder key carries a
    part-path (``"{machine_id}:/library/parts/..."``); the rating key is
    then recovered through the injected async ``rating_key_resolver`` —
    the backend deliberately imports no catalog/state modules. Resolution
    failure raises :class:`HolderResolutionError`, a plain-RuntimeError
    track-level failure: the holder is consumed and ``_play_with_fallback``
    continues — never an outage.

Injection points U3 wires at construction (tests inject fakes):
    ``rating_key_resolver(server_machine_id: str, holder_key_part: str,
    track: Track) -> str | None``
        async; resolves a part-path holder key to the bare rating key on
        that server (alias-table lookup via
        ``catalog.store.get_aliases_for_identity`` members with the
        matching ``{machine_id}:`` prefix). ``None`` = unresolvable.
    ``server_info_resolver(server_machine_id: str) -> ServerInfo | None``
        async; the dispatch-time server binding (protocol/address/port)
        for the createPlayQueue params — player auth rides the Companion
        client's header token, never a per-server field here (S-4).
        Resolved FRESH per dispatch — must not cache across
        ``state.invalidate_plex_client()``.
    ``clients_source() -> list[CompanionPlayer]``
        async; the merged ``GET /clients`` sweep across enabled Plex
        sources (U3 owns the per-server fan-out + registry write-through).
    ``client_factory(host: str, port: int, player_machine_id: str) ->
    CompanionPlayerClient``
        sync; builds the per-player Companion client (U3 supplies the
        controller identity + account/server token — only the
        registry/auth layer knows them). Required before ``set_device``.

Server-down leg, scoped per attempt (plan Open Questions / U2 approach)
    ``probe_liveness()`` targets the PLAYER (cheap ``timeline/poll?wait=0``),
    never the server. The failure classification then falls out of the
    existing supervisor machinery with no special code here:

    * player unreachable at dispatch → :class:`DeviceLostError` → outage
      hold (queue preserved);
    * createPlayQueue accepted but the current attempt's SERVER is dead →
      no timeline movement → the supervisor's confirm deadline expires, its
      probe reads the player fine (reachable, transport stopped ≠
      pre-playback) → track-level classification → holder consumed,
      fallback continues to the next holder/server. U3/U7 refine.

U7 — gapless arming, boundary reconciliation, conflict handling (plan
2026-08-04-002 U7). The player is the advance authority:

* :meth:`arm_next` / :meth:`revoke_next` implement the state orchestrator's
  duck-typed arming contract (``_reconcile_armed_next`` hasattr-gates on
  ``arm_next``). Arming = same-server-only PMS PUT-append (``play_next``)
  + a player ``refreshPlayQueue`` nudge; an unadopted queue stashes the arm
  and the poll loop delivers it on first adopted evidence (the DLNA
  deferred-send timing). Cross-server / unresolvable next → decline (plain
  return, the DLNA not-armed pattern) and the boundary falls back to the
  per-track EOS advance + fresh dispatch.
* Boundary detection = ``playQueueItemID`` edge in the timeline matching
  the armed item (:meth:`_observe_queue`), filtered by commandID staleness
  (echoes below the dispatch-time commandID predate this dispatch) and
  itemID ordering against the post-arm fetched queue window (reverse reads
  = stale replay, never a backward advance). Forward jumps within our
  queue reconcile one boundary per item through the supervisor chokepoint,
  capped at the window length — the same path reconciles a short poll
  partition where the player advanced on its own.
* Conflicts re-dispatch rather than fight the device: a revoked next that
  chains in anyway (stale watch) or a wrong next → stop-and-replay
  correction (dlna.py:1219-1240 shape). A sustained unowned playQueueID
  after confirmation (``_FOREIGN_READ_STRIKES`` consecutive reads, never
  inside the dispatch-transition window) yields to the foreign controller:
  hold with reason ``foreign_controller`` + admin notice, no dispatch loop.
  The owned playQueueID persists (``plexqueue:{device_id}``) so a backend
  restart re-adopts its own queue instead of declaring it foreign.
* Per-device behavioral gapless verdict (DLNA pattern,
  ``gapless_verdict:plexplayer:{device_id}``): a PUT-append the server
  REJECTS verdicts "unsupported" (per-track dispatch mode from then on);
  the first successfully detected armed boundary verdicts "supported".

R10: all session/poll state lives in per-device ``_PlayerSession`` structs
inside the backend instance — no module-level singletons.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from app.models import Track
from app.output.base import (
    AdvanceCallback,
    DeviceLostError,
    DeviceNotReadyError,
    OutputDevice,
    echo_guard_active,
)
from app.plex.companion import (
    CompanionError,
    CompanionParseError,
    CompanionPlayer,
    CompanionPlayerClient,
    CompanionRequestError,
    CompanionTargetMismatchError,
    CompanionUnreachableError,
    TimelineSnapshot,
    server_item_uri,
)

_log = logging.getLogger(__name__)

# Watchdog backstop grace past the track's expected end (Cast precedent,
# chromecast.py WATCHDOG_GRACE_S) — generous enough to absorb buffering.
WATCHDOG_GRACE_S = 30

# Consecutive unreachable timeline polls before the outage signal (the DLNA
# 3-strike convention, dlna.py:975-990): three failures ARE the probe.
_POLL_ERROR_STRIKES = 3

# Pacing floor for the poll loop. wait=1 long-polls until the player's next
# timeline tick, so a well-behaved player paces us; a player that answers
# instantly (or the wait=0 fallback) must not hot-loop.
_POLL_MIN_INTERVAL_S = 1.0

# Delay between failed polls — no point hammering a peer that just refused.
_POLL_ERROR_RETRY_S = 2.0

# Consecutive post-confirmation timeline reads naming an UNOWNED playQueueID
# before the foreign-controller yield (plan KTD: yield-and-notify, debounced
# across dispatch-transition and restart windows so routine re-dispatches
# never false-positive).
_FOREIGN_READ_STRIKES = 3


# ── typed track-level errors ──────────────────────────────────────────────────

class PlexPlayerTrackError(RuntimeError):
    """Track/holder-level dispatch failure: the holder is consumed and the
    state layer's fallback loop continues (``_play_with_fallback`` treats
    any non-DeviceNotReady exception this way). Deliberately NOT a
    ``DeviceNotReadyError`` subclass — these failures must never freeze the
    queue behind an outage hold while the player itself answers."""


class HolderResolutionError(PlexPlayerTrackError):
    """The dispatched holder key could not be resolved to a (server, rating
    key) pair — malformed key, or the alias lookup found no member for the
    server (e.g. mid-rescan). Track-level by contract (module docstring)."""


@dataclass(frozen=True)
class ServerInfo:
    """Dispatch-time server binding handed back by ``server_info_resolver``:
    everything the createPlayQueue param set needs about the OWNING server.
    The address must be reachable *by the player* (plex.direct off-LAN
    lesson) — the resolver owns that choice. No token field (S-4): player
    auth rides the Companion client's X-Plex-Token header."""
    machine_id: str
    protocol: str
    address: str
    port: int


def parse_holder_key(holder_key: str) -> tuple[str, str]:
    """Split ``"{machine_id}:{key}"`` into its parts. The key part is either
    a bare rating key (native path) or a part-path (catalog holder). A key
    without the machine-id prefix cannot name an owning server — that is a
    track-level failure, not a crash."""
    machine_id, sep, key_part = (holder_key or "").partition(":")
    if not sep or not machine_id or not key_part:
        raise HolderResolutionError(
            f"holder key {holder_key!r} lacks the '{{machine_id}}:' prefix")
    return machine_id, key_part


@dataclass
class _PlayerSession:
    """Per-device session state (R10 — one struct per bound player, no
    module-level singletons). Everything the poll loop, watchdog, and the
    future U7 reconciler need about the CURRENT binding/dispatch."""
    device_id: str                      # player machineIdentifier
    client: Any                         # CompanionPlayerClient (or test fake)
    name: str = ""
    poll_task: asyncio.Task | None = None
    watchdog_task: asyncio.Task | None = None
    # Supervisor dispatch token captured at play() entry (dlna.py:727);
    # cleared one-shot when the first playing snapshot confirms the start.
    confirm_token: int | None = None
    # Bumped on every play(); the watchdog captures it so a backstop armed
    # for a superseded dispatch no-ops (stale-token guard).
    play_gen: int = 0
    # Set by our own stop()/teardown BEFORE the stop command goes out: any
    # later stopped read is self-induced and must never advance.
    self_stopped: bool = False
    # ── owned play-queue bookkeeping (U7 extends this for the foreign
    # verdict) ────────────────────────────────────────────────────────────
    # True between our createPlayQueue dispatch and the first timeline
    # evidence of the queue it created; only evidence inside this window is
    # adopted as OURS (player-echoed IDs alone are never ownership).
    awaiting_queue_adoption: bool = False
    current_queue_id: int | None = None
    # Current + previous owned playQueueIDs; the previous is retired on the
    # first evidence of the next queue (plan KTD: foreign-controller policy).
    owned_queue_ids: set[int] = field(default_factory=set)
    # Owning server of the current dispatch — U7's same-server arming check
    # and the per-attempt server probe leg key off this.
    server_machine_id: str | None = None
    # ── U7: gapless arm slot + boundary bookkeeping ───────────────────────
    # Stashed arm awaiting queue adoption: (track, rating_key). arm_next
    # stashes when the dispatched queue isn't adopted yet; the poll loop
    # delivers on the first adopted evidence (DLNA deferred-send timing).
    pending_arm: tuple | None = None
    armed_track: Track | None = None        # delivered arm (device-side next)
    armed_item_id: int | None = None        # its playQueueItemID, if parsed
    armed_rating_key: str | None = None     # identity fallback for matching
    armed_queue_id: int | None = None       # the queue it was appended into
    # Ordered playQueueItemIDs fetched after arming — the itemID-ordering
    # reference (reverse read = stale replay) and the forward-reconcile cap.
    queue_window: tuple = ()
    # Revoked-but-maybe-still-device-side watch (the DLNA stale-next
    # posture): a boundary into it is stop-and-replay corrected.
    stale_item_id: int | None = None
    stale_rating_key: str | None = None
    # Review fix PLX-7: revoke epoch. Bumped by revoke_next and _discard_arm;
    # an in-flight arm delivery captures it before its awaits and, when the
    # epoch moved underneath it, abandons the arm (best-effort DELETE of the
    # appended item + stale watch) instead of installing armed state a
    # concurrent revoke can no longer see.
    revoke_epoch: int = 0
    # Last consumed playQueueItemID — the boundary edge reference.
    current_item_id: int | None = None
    # commandID captured at dispatch: timeline echoes below it predate this
    # dispatch (staleness filter for adoption + boundary evidence).
    dispatch_command_id: int | None = None
    # Consecutive post-confirmation reads naming an unowned playQueueID.
    foreign_reads: int = 0
    # ── position bookkeeping (latest timeline snapshot + monotonic anchor;
    # get_position interpolates between polls like dlna/chromecast anchor
    # their wall clocks) ──────────────────────────────────────────────────
    last_state: str | None = None
    last_time_ms: int | None = None
    last_duration_ms: int | None = None
    last_at: float = 0.0


class PlexPlayerBackend:
    """AbstractOutputBackend implementation for Plex Companion receivers.

    Mirrors the DlnaBackend lifecycle: typed device errors, confirm-token
    capture at ``play()``, poll-loop terminal-state matrix with the
    self-cancellation guard, 3-strike outage, volume echo guard, and
    ``vol:plexplayer:{device_id}`` / ``output_addr:{device_id}``
    persistence. See the module docstring for the U3 injection points and
    the U7 seams."""

    def __init__(
        self,
        advance_cb: AdvanceCallback | None = None,
        *,
        rating_key_resolver: Callable[
            [str, str, Track], Awaitable[str | None]] | None = None,
        server_info_resolver: Callable[
            [str], Awaitable[ServerInfo | None]] | None = None,
        clients_source: Callable[
            [], Awaitable[list[CompanionPlayer]]] | None = None,
        client_factory: Callable[
            [str, int, str], CompanionPlayerClient] | None = None,
        pms_factory: Callable[[str], Awaitable[Any]] | None = None,
        is_current: Callable[[], bool] | None = None,
    ) -> None:
        self._advance_cb = advance_cb
        self._rating_key_resolver = rating_key_resolver
        self._server_info_resolver = server_info_resolver
        self._clients_source = clients_source
        self._client_factory = client_factory
        # Review fix PLX-2: "am I still the router's active-or-pending
        # backend?" — consulted before any notify_outage so a RETIRED
        # backend (switched away while paused/idle) can never corrupt the
        # NEW backend's session with a phantom outage. Default True keeps
        # unit tests and degraded wiring byte-identical.
        self._is_current = is_current or (lambda: True)
        # U7 injection point: async ``pms_factory(server_machine_id) ->
        # PmsCompanionClient | None`` — a FRESH per-call PMS client for the
        # play-queue window ops (append / delete / window read); the backend
        # closes it after each op. None → that server has no enabled source.
        self._pms_factory = pms_factory
        # Per-device behavioral gapless verdicts (DLNA pattern): in-memory
        # mirror of gapless_verdict:plexplayer:{device_id}, loaded at
        # set_device; "unsupported" disables arming for that device.
        self._gapless_verdicts: dict[str, str] = {}
        self._session: _PlayerSession | None = None
        self._device_id: str | None = None
        self._is_playing: bool = False
        self._volume: float = 0.5
        # Echo-guard stamp (app.output.base.ECHO_GUARD_WINDOW): timeline
        # volume echoes within the window of our own write are suppressed.
        self._vol_last_set: float = 0.0
        # Discovery/persistence address cache: device_id → {host, port, name}.
        self._device_addresses: dict[str, dict] = {}
        # The state layer's per-attempt holder key (set_dispatch_holder),
        # consumed one-shot by play().
        self._dispatch_holder_key: str | None = None
        # Teardown-verification warning surface (U2: log + attribute; the
        # U6/U7 admin-notice event wires onto this).
        self.last_teardown_warning: str | None = None

    # ── holder handshake ──────────────────────────────────────────────────

    def set_dispatch_holder(self, holder_key: str | None) -> None:
        """The state layer hands the CURRENT attempt's holder key here
        immediately before ``play()`` (U3 wiring; module docstring). The
        backend never re-picks holders — this is the single selection
        authority's drop-off point. Consumed one-shot by the next play()."""
        self._dispatch_holder_key = holder_key

    def last_dispatch_server(self) -> str | None:
        """The owning server machine_id of the bound player's LAST dispatch,
        or None when unbound / nothing dispatched yet. Sync read for the
        state layer's same-server-first holder ordering (U4, R9) — the
        ordering input only; the backend still never picks holders."""
        sess = self._session
        return sess.server_machine_id if sess is not None else None

    # ── discovery / binding ───────────────────────────────────────────────

    async def discover_devices(self) -> list[OutputDevice]:
        """Fail-soft discover for the legacy pull path and startup
        reconnect: a clients-source failure logs and returns ``[]`` (a GET
        must not 500 on a dead server). The watcher sweep calls
        :meth:`sweep_devices` instead, where a TOTAL ``/clients`` failure
        must stay distinguishable from "no players" (U3)."""
        try:
            return await self.sweep_devices()
        except Exception:
            _log.warning("PlexPlayer discovery: clients source failed",
                         exc_info=True)
            return []

    async def sweep_devices(self) -> list[OutputDevice]:
        """RAISING discover (U3 watcher-sweep contract): Companion players
        from the injected ``/clients`` sweep, filtered to the
        ``playqueues-creation`` capability (v1 eligibility gate) and deduped
        by player machineIdentifier (one entry however many servers see the
        device). A raised clients-source failure means "no scan data" — the
        watcher leaves its registry untouched rather than grace-flipping
        every known player over a server outage (push-discovery
        write-through lesson). No sweep source wired (pre-U3) → empty
        list."""
        if self._clients_source is None:
            return []
        players = await self._clients_source()
        devices: list[OutputDevice] = []
        seen: set[str] = set()
        for p in players:
            mid = p.machine_identifier
            if not p.supports_playqueues_creation:
                _log.info(
                    "PlexPlayer discovery: %r (%s) lacks the "
                    "playqueues-creation capability — ineligible (v1 gate)",
                    p.name, mid)
                continue
            if mid in seen:
                continue
            seen.add(mid)
            self._device_addresses[mid] = {
                "host": p.address, "port": p.port,
                "name": p.name or p.product or mid,
            }
            devices.append(OutputDevice(
                id=mid,
                name=p.name or p.product or mid,
                backend_type="plexplayer",
                id_format="uuid",
            ))
        return devices

    def device_host(self, device_id: str) -> str | None:
        """The cached host for a discovered/bound player, or None when the
        address cache doesn't know it — the public read the aggregator's
        ``host_for`` and the watcher's sweep-merge use as the per-host
        dedupe/probe key (U3; mirrors DLNA's ``_device_locations`` read)."""
        addr = self._device_addresses.get(device_id)
        return str(addr["host"]) if addr and addr.get("host") else None

    async def set_device(self, device_id: str) -> None:
        """Bind a player: tear down any prior session (poll/watchdog
        cancelled, client closed), build the per-player Companion client,
        restore the persisted volume, and persist ``output_addr:{device_id}``
        for the mDNS-free restart re-bind (supervisor U3 mechanic)."""
        prior = self._session
        if prior is not None:
            prior.self_stopped = True
            self._cancel_poll(prior)
            self._cancel_watchdog(prior)
            try:
                await prior.client.aclose()
            except Exception:
                _log.debug("PlexPlayer set_device: prior client close failed",
                           exc_info=True)
        self._session = None
        self._is_playing = False

        from app import database
        addr = self._device_addresses.get(device_id)
        if addr is None:
            # Restart re-bind path: no discovery yet — seed from the
            # persisted address (same posture as _startup_reconnect).
            raw = await database.get_setting(f"output_addr:{device_id}")
            if raw:
                try:
                    data = json.loads(raw)
                    if data.get("host") and data.get("port"):
                        addr = {
                            "host": str(data["host"]),
                            "port": int(data["port"]),
                            "name": str(data.get("name") or device_id),
                        }
                        self._device_addresses[device_id] = addr
                except Exception:
                    _log.warning(
                        "PlexPlayer set_device: malformed persisted address "
                        "for %r", device_id, exc_info=True)
        if addr is None:
            raise DeviceNotReadyError(
                f"no known address for Plex player {device_id!r} — "
                "rescan devices")
        if self._client_factory is None:
            raise DeviceNotReadyError(
                "plexplayer client factory not wired (U3 injection point)")

        client = self._client_factory(addr["host"], int(addr["port"]),
                                      device_id)
        self._session = _PlayerSession(
            device_id=device_id, client=client,
            name=str(addr.get("name") or device_id))
        self._device_id = device_id

        stored = await database.get_setting(f"vol:plexplayer:{device_id}")
        self._volume = float(stored) if stored else 0.5

        # U7: per-device behavioral gapless verdict (DLNA set_device pattern,
        # dlna.py:598) — "unsupported" disables arming for this device.
        try:
            verdict = await database.get_gapless_verdict("plexplayer",
                                                         device_id)
            if verdict:
                self._gapless_verdicts[device_id] = verdict
        except Exception:
            _log.debug("PlexPlayer set_device: gapless-verdict read failed",
                       exc_info=True)

        # U7: restart re-adoption — seed the owned playQueueID persisted at
        # the last adoption so a timeline still playing OUR pre-restart
        # queue reads as ours, never as a foreign controller.
        try:
            raw_q = await database.get_setting(f"plexqueue:{device_id}")
        except Exception:
            raw_q = None
        if raw_q:
            try:
                data = json.loads(raw_q)
                qid = int(data["queue_id"])
                self._session.current_queue_id = qid
                self._session.owned_queue_ids = {qid}
                self._session.server_machine_id = (
                    str(data.get("server") or "") or None)
                _log.info("PlexPlayer set_device: re-adopted persisted "
                          "playQueueID %s for %r", qid, device_id)
            except Exception:
                _log.debug("PlexPlayer set_device: malformed persisted "
                           "plexqueue for %r", device_id, exc_info=True)

        # Best-effort — persistence must never fail the device selection.
        try:
            await database.set_setting(
                f"output_addr:{device_id}",
                json.dumps({
                    "host": addr["host"],
                    "port": int(addr["port"]),
                    "name": addr.get("name") or device_id,
                    "machine_identifier": device_id,
                }))
        except Exception:
            _log.debug("PlexPlayer set_device: output_addr persist failed",
                       exc_info=True)

    # ── dispatch ──────────────────────────────────────────────────────────

    async def play(self, stream_url: str, metadata: Track) -> None:
        """Dispatch via player-side createPlayQueue. ``stream_url`` is
        deliberately unused (module docstring) — the dispatch derives from
        the handed holder key / ``metadata.id``. Error contract:

        * no bound player → :class:`DeviceNotReadyError` (queue not drained)
        * player transport failure / 404 target mismatch →
          :class:`DeviceLostError` (outage hold)
        * holder/server resolution failure, player command refusal →
          :class:`PlexPlayerTrackError` (holder consumed, fallback continues)
        """
        # Consume the handed holder even on failure paths — a stale key
        # must never leak into a later, unrelated dispatch.
        holder_key = self._dispatch_holder_key
        self._dispatch_holder_key = None

        sess = self._session
        if sess is None:
            raise DeviceNotReadyError("no Plex player selected")
        _log.info("PlexPlayer play() entry: title=%r holder=%r device=%s",
                  metadata.title, holder_key, sess.device_id)
        self._cancel_poll(sess)
        self._cancel_watchdog(sess)
        sess.play_gen += 1
        gen = sess.play_gen
        sess.self_stopped = False
        # U7: a fresh createPlayQueue supersedes the whole device queue —
        # discard the arm slot + stale watch FIRST (skip-during-armed-window
        # discipline: the racing native advance is already dead via the
        # cancelled poll task + advance_gen supersession; a later
        # orchestrator revoke_next finds the slot empty and no-ops).
        self._discard_arm(sess)
        sess.foreign_reads = 0
        sess.current_item_id = None
        sess.dispatch_command_id = None
        self.last_teardown_warning = None
        # Capture the supervisor's per-dispatch token at entry (dlna.py:727);
        # the poll loop's first playing snapshot emits confirmed-start.
        from app.output import session as output_session
        sess.confirm_token = output_session.get_supervisor().current_token()

        if holder_key is None:
            holder_key = metadata.id   # native single-Plex path
        machine_id, key_part = parse_holder_key(holder_key)
        rating_key = await self._resolve_rating_key(
            machine_id, key_part, metadata)
        info = await self._resolve_server(machine_id)

        try:
            await sess.client.create_play_queue(
                server_machine_id=machine_id,
                key=f"/library/metadata/{rating_key}",
                server_protocol=info.protocol,
                server_address=info.address,
                server_port=info.port,
            )
        except (CompanionUnreachableError, CompanionTargetMismatchError) as exc:
            # The PLAYER did not answer (or a stale address answered): a
            # device-level failure — the supervisor holds the queue.
            raise DeviceLostError(
                f"Plex player {sess.device_id!r} unreachable at dispatch: "
                f"{exc}") from exc
        except CompanionError as exc:
            # The player answered but refused this dispatch — the current
            # attempt's uri/server may be bad. Track-level: fallback owns it.
            raise PlexPlayerTrackError(
                f"Plex player refused createPlayQueue for "
                f"{metadata.title!r}: {exc}") from exc

        # createPlayQueue returns nothing usable — the authoritative
        # playQueueID is adopted from the first POST-dispatch timeline
        # evidence (_adopt_queue_evidence); player-echoed IDs outside this
        # window are never adopted as ours. The dispatch-time commandID is
        # the staleness floor: timeline echoes below it predate this
        # dispatch (U7 replay filter).
        sess.dispatch_command_id = getattr(sess.client, "command_id", None)
        sess.awaiting_queue_adoption = True
        sess.server_machine_id = machine_id
        sess.last_state = None
        sess.last_time_ms = 0
        sess.last_duration_ms = int(metadata.duration_ms or 0)
        sess.last_at = time.monotonic()
        self._is_playing = True
        sess.poll_task = asyncio.create_task(self._poll_timeline(sess))
        duration_ms = int(metadata.duration_ms or 0)
        if duration_ms > 0:
            sess.watchdog_task = asyncio.create_task(
                self._watchdog(sess, gen, duration_ms))
        _log.info("PlexPlayer play(): createPlayQueue accepted "
                  "(server=%s ratingKey=%s) — timeline poll started",
                  machine_id, rating_key)

    async def _resolve_rating_key(self, machine_id: str, key_part: str,
                                  metadata: Track) -> str:
        """Rating-key recovery (module docstring): a non-path key part IS
        the rating key; a part-path goes through the injected resolver.
        Failure → track-level typed error, never a crash."""
        if not key_part.startswith("/"):
            return key_part
        if self._rating_key_resolver is None:
            raise HolderResolutionError(
                "catalog-path holder key but no rating_key_resolver wired "
                "(U3 injection point)")
        try:
            rating_key = await self._rating_key_resolver(
                machine_id, key_part, metadata)
        except Exception as exc:
            raise HolderResolutionError(
                f"rating-key resolution raised for {key_part!r} on "
                f"{machine_id}: {exc}") from exc
        if not rating_key:
            raise HolderResolutionError(
                f"no rating key for holder {key_part!r} on server "
                f"{machine_id} (alias lookup empty)")
        return str(rating_key)

    async def _resolve_server(self, machine_id: str) -> ServerInfo:
        """Fresh per-dispatch server binding (no caching — see the
        invalidate_plex_client note in the module docstring)."""
        if self._server_info_resolver is None:
            raise PlexPlayerTrackError(
                "no server_info_resolver wired (U3 injection point)")
        try:
            info = await self._server_info_resolver(machine_id)
        except Exception as exc:
            raise PlexPlayerTrackError(
                f"server lookup raised for {machine_id}: {exc}") from exc
        if info is None:
            raise PlexPlayerTrackError(
                f"no enabled Plex server for machine id {machine_id} — "
                "holder unplayable on this backend")
        return info

    # ── timeline poll loop ────────────────────────────────────────────────

    async def _read_timeline(self, sess: _PlayerSession) -> TimelineSnapshot:
        """One observation read: wait=1 long poll, with a same-tick wait=0
        retry when the long-poll body would not parse (plan: wait=0
        fallback on parse issues)."""
        try:
            return await sess.client.poll_timeline(wait=1)
        except CompanionParseError:
            _log.warning("PlexPlayer poll: long-poll body unparseable — "
                         "retrying with wait=0")
            return await sess.client.poll_timeline(wait=0)

    async def _poll_timeline(self, sess: _PlayerSession) -> None:
        """The observation loop — terminal-state matrix per the Chromecast
        stall lesson (chromecast-queue-stalls-silently doc):

        * ``playing``      → confirmed start (one-shot per dispatch)
        * ``buffering``/``paused`` → not terminal, resets the stopped run
        * ``stopped``/``error``    → self-induced (our stop flag): exit, NO
          advance; natural: 2 CONSECUTIVE reads → exactly one advance
        * state ``None`` (timeline-gone/idle) → unknown, not evidence —
          resets the consecutive run; the duration watchdog owns a
          permanently-gone timeline
        * 3 consecutive unreachable polls → ``notify_outage("poll_errors")``
          (DLNA 975-990 pattern; parse/refusal errors never count — a
          reachable player with a garbled body is not an outage)

        U7 extends the terminal/queue handling — kept in the separated
        :meth:`_register_terminal_read` / :meth:`_eos_advance` /
        :meth:`_adopt_queue_evidence` methods so the boundary watch can
        slot in without rewriting the loop."""
        error_count = 0
        terminal_count = 0
        poll_idx = 0
        while True:
            started = time.monotonic()
            poll_idx += 1
            try:
                tl = await self._read_timeline(sess)
            except CompanionUnreachableError as exc:
                error_count += 1
                _log.warning(
                    "PlexPlayer poll #%d: unreachable (%s); error_count=%d "
                    "(will report outage on >=%d)",
                    poll_idx, exc, error_count, _POLL_ERROR_STRIKES)
                if error_count >= _POLL_ERROR_STRIKES:
                    # Three consecutive failed polls ARE the reachability
                    # probe failing — the player is gone, not this track.
                    self._is_playing = False
                    sess.poll_task = None
                    if not self._is_current():
                        # Review fix PLX-2: this backend was switched away
                        # from — an outage signal now would land in the NEW
                        # backend's session (phantom outage). Just exit.
                        _log.warning(
                            "PlexPlayer poll: %d consecutive errors on a "
                            "RETIRED backend — suppressing outage signal",
                            _POLL_ERROR_STRIKES)
                        return
                    _log.warning("PlexPlayer poll: %d consecutive errors — "
                                 "reporting outage-suspected",
                                 _POLL_ERROR_STRIKES)
                    from app.output import session as output_session
                    output_session.notify_outage("poll_errors")
                    return
                await asyncio.sleep(_POLL_ERROR_RETRY_S)
                continue
            except CompanionError as exc:
                # Parse/request failure from a reachable peer: log, never
                # count toward the outage budget (DLNA ParseError lesson).
                _log.warning("PlexPlayer poll #%d: read failed without "
                             "transport error (%s) — not counting toward "
                             "outage", poll_idx, exc)
                await asyncio.sleep(_POLL_ERROR_RETRY_S)
                continue
            error_count = 0
            self._note_snapshot(sess, tl)
            # U7: deliver a stashed arm once our dispatched queue is adopted
            # (the DLNA deferred-send timing — arm as early as possible, but
            # never into a queue we don't own yet).
            if (sess.pending_arm is not None
                    and sess.current_queue_id is not None
                    and not sess.awaiting_queue_adoption):
                await self._deliver_pending_arm(sess)
            # U7: boundary / foreign-controller observation.
            outcome = await self._observe_queue(sess, tl)
            if outcome == "yielded":
                # Foreign controller owns the device — the hold is entered;
                # stop dispatching (no advance, no outage).
                sess.poll_task = None
                return
            if outcome == "corrected":
                # Wrong-next / revoked-next chained in: stop-and-replay the
                # correct queue front via the NORMAL dispatch path
                # (dlna.py:1219-1240 shape). Same self-cancel guard as
                # _eos_advance: clear the task ref BEFORE awaiting
                # advance_cb.
                self._is_playing = False
                self._cancel_watchdog(sess)
                sess.poll_task = None
                if self._advance_cb:
                    await self._advance_cb()
                return
            if outcome == "boundary":
                # The device chained tracks: any in-progress stopped run
                # belonged to the transition, not to the new track.
                terminal_count = 0
            state = tl.state
            if state == "playing":
                terminal_count = 0
                # Confirmed start: the first playing snapshot OF OUR QUEUE is
                # the data-plane evidence (advancing/nonzero time is implied
                # by the state; command 200s prove nothing). One-shot.
                # Review fix PLX-5: gated on adopted evidence — a playing
                # tick that arrives before _adopt_queue_evidence saw our
                # dispatched queue is the OLD track still sounding (the
                # deferred-swap / actively-playing-old-track window), never
                # this dispatch's confirmation.
                if (sess.confirm_token is not None
                        and not sess.awaiting_queue_adoption):
                    token, sess.confirm_token = sess.confirm_token, None
                    from app.output import session as output_session
                    output_session.notify_confirmed(token)
                # Review fix PLX-6: an EXTERNAL resume (another Plex app)
                # after our own pause() — a non-stale playing read naming
                # the owned queue while we believe we're paused. Restore
                # the playing flag (or the terminal-read matrix ignores the
                # track's end forever and the queue stalls) and re-arm the
                # watchdog for the remaining duration.
                if (not self._is_playing and not sess.self_stopped
                        and not self._is_stale_echo(sess, tl)
                        and tl.play_queue_id is not None
                        and tl.play_queue_id in sess.owned_queue_ids):
                    _log.info("PlexPlayer poll: external resume detected "
                              "(owned queue playing while paused) — "
                              "restoring playing state")
                    self._is_playing = True
                    self._cancel_watchdog(sess)
                    remaining_ms = max(
                        0, (sess.last_duration_ms or 0)
                        - self._interpolated_position(sess))
                    if remaining_ms > 0:
                        sess.watchdog_task = asyncio.create_task(
                            self._watchdog(sess, sess.play_gen, remaining_ms))
            elif state in ("buffering", "paused"):
                terminal_count = 0
            elif state in ("stopped", "error"):
                outcome, terminal_count = self._register_terminal_read(
                    sess, state, terminal_count)
                if outcome == "self_stopped":
                    sess.poll_task = None
                    return
                if outcome == "advance":
                    await self._eos_advance(sess)
                    return
            else:
                # Timeline-gone / no state: unknown, not evidence either
                # way. Strictly-consecutive terminal evidence only.
                terminal_count = 0
                _log.debug("PlexPlayer poll #%d: no timeline state "
                           "(idle/gone) — not evidence", poll_idx)
            elapsed = time.monotonic() - started
            if elapsed < _POLL_MIN_INTERVAL_S:
                await asyncio.sleep(_POLL_MIN_INTERVAL_S - elapsed)

    def _register_terminal_read(
        self, sess: _PlayerSession, state: str, terminal_count: int,
    ) -> tuple[str, int]:
        """One terminal (stopped/error) timeline read. Returns
        ``(outcome, terminal_count)`` where outcome is ``"self_stopped"``
        (our own stop — exit with NO advance), ``"advance"`` (2 consecutive
        natural terminal reads — the caller advances once), or ``"none"``.
        Separated from the loop so U7's boundary/arm-slot logic can wrap it
        (arm-slot discard BEFORE any EOS fallback, etc.)."""
        if sess.self_stopped:
            _log.info("PlexPlayer poll: %s read after our own stop — "
                      "self-induced, no advance", state)
            return ("self_stopped", 0)
        if sess.confirm_token is not None:
            # Review fix PLX-4: pre-confirmation window. A dispatch that
            # never starts (dead server, slow queue load) legitimately reads
            # "stopped" until the player begins — counting those ticks would
            # fire an EOS advance and pop the entry as finished (queue
            # burned, other holders never tried). The supervisor's confirm
            # deadline + probe own this window; terminal evidence only
            # counts once the start was confirmed.
            return ("none", 0)
        if not self._is_playing:
            # Nothing of ours is (supposed to be) playing — an idle player
            # reporting stopped is steady state, not an EOS.
            return ("none", 0)
        terminal_count += 1
        if state == "error":
            _log.warning("PlexPlayer poll: player reports error state "
                         "(%d consecutive terminal reads)", terminal_count)
        if terminal_count >= 2:
            return ("advance", terminal_count)
        return ("none", terminal_count)

    async def _eos_advance(self, sess: _PlayerSession) -> None:
        """Natural end-of-stream advance (exactly once per dispatch).
        Clears the poll-task ref BEFORE awaiting advance_cb: advance_cb →
        _do_advance → play() → _cancel_poll() would otherwise cancel the
        very task running this method and the next dispatch would die at
        its first await (dlna.py:922-933; airplay.py:1331).

        U7 one-shot discipline (the dlna.py:886-912 shape): an armed next
        the player STOPPED into instead of chaining is discarded BEFORE the
        EOS fallback advance, so the un-fired boundary can never also fire —
        exactly ONE advance (this one). The queue never advanced for the
        armed slot, so this EOS advance IS the fresh dispatch of the
        expected next. No verdict: only an append rejection or a detected
        armed boundary is capability evidence (plan U7)."""
        if sess.armed_track is not None or sess.pending_arm is not None:
            _log.warning("PlexPlayer gapless: player stopped despite an "
                         "armed next — gapped fallback advance (arm slot "
                         "discarded, no verdict)")
            self._discard_arm(sess)
        _log.info("PlexPlayer poll: confirmed terminal state after 2 "
                  "consecutive reads — firing advance_cb")
        self._is_playing = False
        self._cancel_watchdog(sess)
        sess.poll_task = None
        if self._advance_cb:
            await self._advance_cb()

    def _note_snapshot(self, sess: _PlayerSession,
                       tl: TimelineSnapshot) -> None:
        """Record a timeline snapshot: position anchor (get_position
        interpolates from it), owned-queue adoption, and the timeline
        volume echo (suppressed inside the echo-guard window of our own
        set_volume write, like every backend's device-event path)."""
        if tl.state is not None:
            sess.last_state = tl.state
            if tl.time is not None:
                sess.last_time_ms = tl.time
            if tl.duration is not None:
                sess.last_duration_ms = tl.duration
            sess.last_at = time.monotonic()
        self._adopt_queue_evidence(sess, tl)
        if tl.volume is not None and not echo_guard_active(self._vol_last_set):
            level = max(0.0, min(1.0, tl.volume / 100.0))
            if abs(level - self._volume) >= 0.01:
                self._volume = level
                _log.debug("PlexPlayer external volume change: %.2f", level)
                self._broadcast_volume(level)

    def _broadcast_volume(self, level: float) -> None:
        """Best-effort admin broadcast of an external volume change
        (mirrors dlna._on_dlna_event's fire-and-forget hop)."""
        try:
            from app.events.bus import manager
            from app.events.types import VolumeChangedEvent
            asyncio.get_running_loop().create_task(
                manager.broadcast_to_admins(VolumeChangedEvent(level=level)))
        except Exception:
            _log.debug("PlexPlayer: volume broadcast failed", exc_info=True)

    def _adopt_queue_evidence(self, sess: _PlayerSession,
                              tl: TimelineSnapshot) -> None:
        """Owned play-queue bookkeeping (U7 seam — see the module
        docstring). Only evidence inside our post-dispatch adoption window
        becomes OURS; the previous owned queue is retained until the new
        one shows, then older IDs retire (current + previous, exactly the
        debounce set the U7 foreign verdict needs)."""
        qid = tl.play_queue_id
        if qid is None or qid == sess.current_queue_id:
            return
        if sess.awaiting_queue_adoption:
            if self._is_stale_echo(sess, tl):
                # A replayed pre-dispatch snapshot inside the adoption
                # window must not be adopted as the NEW queue (U7 staleness
                # filter — the commandID echo predates our dispatch).
                _log.debug("PlexPlayer: stale timeline echo (commandID %s < "
                           "dispatch %s) — not adopting playQueueID %s",
                           tl.command_id, sess.dispatch_command_id, qid)
                return
            prev = sess.current_queue_id
            sess.current_queue_id = qid
            sess.owned_queue_ids = {q for q in (qid, prev) if q is not None}
            sess.awaiting_queue_adoption = False
            # The item anchor belonged to the retiring queue — the boundary
            # watch re-adopts it from the new queue's first evidence (U7).
            sess.current_item_id = None
            _log.info("PlexPlayer: adopted playQueueID %s (owned=%s)",
                      qid, sorted(sess.owned_queue_ids))
            # Persist the owned queue so a backend restart re-adopts it
            # instead of declaring its own pre-restart queue foreign (U7).
            self._persist_owned_queue(sess)
            return
        # Not in a dispatch window: an unexpected queue id — the U7 foreign
        # verdict (_observe_queue) debounces and decides; adoption only
        # happens inside the window.
        _log.debug("PlexPlayer: unowned playQueueID %s observed (owned=%s)",
                   qid, sorted(sess.owned_queue_ids))

    def _persist_owned_queue(self, sess: _PlayerSession) -> None:
        """Fire-and-forget persistence of the adopted playQueueID
        (``plexqueue:{device_id}``) — restart re-adoption seed (U7). Only
        when a real device binding exists (tests bind sessions directly)."""
        if not self._device_id:
            return
        payload = json.dumps({"queue_id": sess.current_queue_id,
                              "server": sess.server_machine_id or ""})
        key = f"plexqueue:{sess.device_id}"

        async def _write() -> None:
            from app import database
            try:
                await database.set_setting(key, payload)
            except Exception:
                _log.debug("PlexPlayer: plexqueue persist failed",
                           exc_info=True)

        try:
            asyncio.get_running_loop().create_task(_write())
        except RuntimeError:
            pass  # no loop (sync test path) — persistence is best-effort

    @staticmethod
    def _is_stale_echo(sess: _PlayerSession, tl: TimelineSnapshot) -> bool:
        """True when the snapshot's commandID echo predates the current
        dispatch — a replayed pre-dispatch timeline (reconnect buffers, slow
        proxies). Absent commandIDs are inert (no evidence either way)."""
        return (tl.command_id is not None
                and sess.dispatch_command_id is not None
                and tl.command_id < sess.dispatch_command_id)

    # ── U7: gapless arming (state orchestrator duck-typed contract) ───────

    async def arm_next(self, stream_url: str, track: Track) -> None:
        """Device-side arming (``state._reconcile_armed_next`` hasattr-gated
        contract). ``stream_url`` is deliberately unused — the arm is a
        same-server PMS play-queue append, not a URL hand-off (module
        docstring). Gates, in order (a decline is a plain return — the DLNA
        not-armed pattern: the orchestrator slot fills, the device holds
        nothing, and the boundary falls back to per-track EOS + fresh
        dispatch):

        - bound + playing session only;
        - cached behavioral verdict "unsupported" → never arm this device;
        - the next track must resolve to a rating key on the CURRENT
          dispatch's server (same-server only — a play queue cannot span
          servers; cross-server/unresolvable → decline);
        - queue not yet adopted → stash; the poll loop delivers on first
          adopted evidence (never append into a queue we don't own).

        The orchestrator's R21 closing freeze covers this backend for free:
        the reconcile never calls arm_next past the send-off track, and a
        pre-armed next is revoked through ``revoke_next`` when the closing
        input flips."""
        sess = self._session
        if sess is None or not self._is_playing:
            return
        if (self._gapless_verdicts.get(self._device_id or "", "unverified")
                == "unsupported"):
            _log.debug("PlexPlayer gapless: arming disabled by cached "
                       "verdict for %r", self._device_id)
            return
        server = sess.server_machine_id
        if not server:
            return
        gen = sess.play_gen
        epoch = sess.revoke_epoch   # PLX-7: captured before ANY await
        rating_key = await self._arm_rating_key(server, track)
        if sess is not self._session or sess.play_gen != gen:
            return  # superseded while resolving
        if sess.revoke_epoch != epoch:
            return  # PLX-7: a revoke raced the resolver await — don't arm
        if not rating_key:
            _log.info(
                "PlexPlayer gapless: next %r has no copy on server %s "
                "(cross-server or unresolvable) — not arming; the boundary "
                "falls back to EOS + fresh dispatch",
                getattr(track, "title", "?"), server)
            return
        sess.pending_arm = None
        if sess.current_queue_id is None or sess.awaiting_queue_adoption:
            sess.pending_arm = (track, str(rating_key))
            _log.debug("PlexPlayer gapless: queue not adopted yet — arm for "
                       "%r stashed", getattr(track, "title", "?"))
            return
        await self._arm_device_side(sess, track, str(rating_key),
                                    epoch=epoch)

    async def _arm_rating_key(self, server_machine_id: str,
                              track: Track) -> str | None:
        """The next track's rating key ON THE CURRENT DISPATCH'S SERVER, or
        None (→ decline arming). Rides the same injected resolver as
        dispatch; the production resolver keys on track identity + the
        server prefix, so the key-part argument is advisory and empty here
        (there is no dispatched holder at arm time). Without a resolver
        (tests / degraded wiring) the native ``{machine_id}:{ratingKey}``
        id shape is parsed directly."""
        if self._rating_key_resolver is not None:
            try:
                rk = await self._rating_key_resolver(server_machine_id, "",
                                                     track)
            except Exception:
                _log.warning("PlexPlayer gapless: rating-key resolution "
                             "raised at arm time", exc_info=True)
                return None
            return str(rk) if rk else None
        tid = str(getattr(track, "id", "") or "")
        prefix = f"{server_machine_id}:"
        if tid.startswith(prefix) and not tid[len(prefix):].startswith("/"):
            return tid[len(prefix):]
        return None

    async def _deliver_pending_arm(self, sess: _PlayerSession) -> None:
        """Deliver a stashed arm once the dispatched queue is adopted (poll
        loop call site). One-shot: the stash clears whether or not the
        device-side arm lands."""
        pending, sess.pending_arm = sess.pending_arm, None
        if pending is None:
            return
        track, rating_key = pending
        await self._arm_device_side(sess, track, rating_key)

    async def _arm_device_side(self, sess: _PlayerSession, track: Track,
                               rating_key: str,
                               epoch: int | None = None) -> None:
        """The device-side arm: PMS PUT-append (``play_next=True``) into the
        owned queue + a player ``refreshPlayQueue`` nudge, then a window
        fetch so the boundary watch knows the item ordering. Every await is
        followed by a play-generation staleness check (a superseded dispatch
        abandons its queue — an append that landed there is dead weight,
        never armed state).

        Review fix PLX-7: ``epoch`` is the revoke epoch captured before the
        caller's first await (default: captured at entry here). If a
        ``revoke_next`` bumps it while the append/window awaits are in
        flight, the delivery must NOT install armed state the revoke could
        no longer see — the appended item is abandoned (best-effort DELETE
        + refresh nudge) and left on the stale watch, so a player that
        chains into it anyway gets the stop-and-replay correction."""
        gen = sess.play_gen
        if epoch is None:
            epoch = sess.revoke_epoch
        qid = sess.current_queue_id
        server = sess.server_machine_id
        if qid is None or server is None:
            return
        if self._pms_factory is None:
            _log.debug("PlexPlayer gapless: no pms_factory wired — not "
                       "arming (U7 injection point)")
            return
        try:
            pms = await self._pms_factory(server)
        except Exception:
            _log.warning("PlexPlayer gapless: PMS client build failed for "
                         "%s — not arming", server, exc_info=True)
            return
        if pms is None:
            _log.info("PlexPlayer gapless: no enabled source for server %s "
                      "— not arming", server)
            return
        window = None
        try:
            if sess.play_gen != gen or not self._is_playing:
                return  # superseded while the client was built
            # URI-form decision (U1 open question, resolved here): plexapi
            # appends with library://{section.uuid}/item{key}, but section
            # uuids are exposed NOWHERE in app/plex (neither /clients nor
            # the library enumeration carries them), so the
            # server://{machineIdentifier}/com.plexapp.plugins.library{key}
            # form is primary — the same uri shape the createPlayQueue
            # dispatch already uses. HARDWARE-VALIDATION checklist item:
            # PUT-append with a server:// uri accepted by a real PMS. A
            # rejection lands as CompanionRequestError below and verdicts
            # this device per-track — graceful degradation either way.
            uri = server_item_uri(server, f"/library/metadata/{rating_key}")
            try:
                window = await pms.append_to_play_queue(qid, uri,
                                                        play_next=True)
            except CompanionRequestError as exc:
                # The server REFUSED the append to the player-owned queue —
                # behavioral evidence this device cannot be armed under our
                # controller identity (plan risk table): per-track dispatch
                # mode from here on.
                _log.warning("PlexPlayer gapless: PUT-append rejected (%s) "
                             "— verdicting unsupported for %r", exc,
                             sess.device_id)
                await self._decide_gapless_verdict("unsupported")
                return
            except CompanionError as exc:
                _log.warning("PlexPlayer gapless: append failed (%s) — not "
                             "armed for this boundary (no verdict: a "
                             "transport failure is not capability evidence)",
                             exc)
                return
            if window is None or not window.items:
                # PUT responses can come back unwindowed — fetch the
                # ordering explicitly (the boundary watch needs it for
                # itemID monotonicity + the forward-reconcile cap).
                try:
                    window = await pms.get_play_queue(qid)
                except CompanionError:
                    _log.warning("PlexPlayer gapless: post-arm window fetch "
                                 "failed — arming by rating-key identity "
                                 "only", exc_info=True)
                    window = None
        finally:
            try:
                await pms.aclose()
            except Exception:
                pass
        if sess.play_gen != gen or not self._is_playing:
            return  # superseded mid-append: the item landed in an abandoned queue
        if sess.revoke_epoch != epoch:
            # PLX-7: a revoke raced this delivery — the append may have
            # landed device-side, but it must never become armed state.
            await self._abandon_raced_arm(sess, qid, rating_key, window)
            return
        try:
            await sess.client.refresh_play_queue(qid)
        except CompanionError:
            _log.warning("PlexPlayer gapless: refreshPlayQueue nudge failed "
                         "— the player may not fetch the appended next; the "
                         "EOS fallback owns a missed boundary", exc_info=True)
        item_id = self._find_appended_item(window, rating_key,
                                           sess.current_item_id)
        sess.armed_track = track
        sess.armed_item_id = item_id
        sess.armed_rating_key = str(rating_key)
        sess.armed_queue_id = qid
        sess.queue_window = (
            tuple(i.play_queue_item_id for i in window.items)
            if window is not None else ())
        # A delivered arm overwrites any stale leftover from an earlier
        # revoke (the DLNA one-next-slot posture).
        sess.stale_item_id = None
        sess.stale_rating_key = None
        _log.info("PlexPlayer gapless: armed next %r device-side "
                  "(queue %s item %s)", getattr(track, "title", "?"),
                  qid, item_id)

    async def _abandon_raced_arm(self, sess: _PlayerSession, qid: int,
                                 rating_key: str, window) -> None:
        """PLX-7 abandonment half: an arm delivery lost the race to a
        revoke. Best-effort DELETE of the just-appended item + a refresh
        nudge so the player drops it; either way the revoked identity goes
        on the stale watch — a player that chains into it anyway gets the
        stop-and-replay correction (the revoke_next posture)."""
        item_id = self._find_appended_item(window, rating_key,
                                           sess.current_item_id)
        sess.stale_item_id = item_id
        sess.stale_rating_key = str(rating_key)
        _log.info("PlexPlayer gapless: revoke raced the arm delivery — "
                  "abandoning appended item %s (stale watch armed)", item_id)
        server = sess.server_machine_id
        if item_id is not None and server and self._pms_factory is not None:
            try:
                pms = await self._pms_factory(server)
            except Exception:
                pms = None
            if pms is not None:
                try:
                    await pms.delete_play_queue_item(qid, item_id)
                except CompanionError:
                    _log.warning("PlexPlayer gapless: abandon DELETE failed "
                                 "— stale watch owns the correction",
                                 exc_info=True)
                finally:
                    try:
                        await pms.aclose()
                    except Exception:
                        pass
        try:
            await sess.client.refresh_play_queue(qid)
        except CompanionError:
            _log.warning("PlexPlayer gapless: abandon nudge failed",
                         exc_info=True)

    @staticmethod
    def _find_appended_item(window, rating_key: str,
                            current_item_id: int | None) -> int | None:
        """The playQueueItemID of the item we just appended: the rating-key
        match positioned AFTER the current item (play_next fronts the Up
        Next region); with no position anchor, the last match (the same
        track queued twice earlier in the window must not shadow the fresh
        append). None → boundary matching degrades to rating-key identity."""
        if window is None or not window.items:
            return None
        matches = [i for i in window.items
                   if str(i.rating_key or "") == str(rating_key)]
        if not matches:
            return None
        if current_item_id is not None:
            ids = [i.play_queue_item_id for i in window.items]
            if current_item_id in ids:
                cur_idx = ids.index(current_item_id)
                after = [m for m in matches
                         if ids.index(m.play_queue_item_id) > cur_idx]
                if after:
                    return after[0].play_queue_item_id
        return matches[-1].play_queue_item_id

    async def revoke_next(self) -> None:
        """Revoke the armed next (idempotent — the orchestrator also revokes
        right after a boundary consumed the arm, which must no-op). An
        undelivered stash simply clears. A DELIVERED arm is DELETEd from the
        PMS queue and the player nudged; the revoked identity stays on the
        stale watch EITHER WAY (the DLNA revoke posture): if the player
        chains into it anyway — delete failed, nudge missed, or the player
        pre-fetched the old window — the boundary watch stop-and-replay
        corrects. That correction IS the plan's revoke-failure ladder:
        wrong-next context replays the correct queue front via the normal
        dispatch path; closing-time context lands in ``_do_advance``, whose
        closing check freezes instead of dispatching — the forced stop at
        the boundary."""
        sess = self._session
        if sess is None:
            return
        # PLX-7: invalidate any IN-FLIGHT arm delivery before anything else —
        # a delivery past its awaits must abandon rather than install.
        sess.revoke_epoch += 1
        sess.pending_arm = None
        armed_track = sess.armed_track
        item_id = sess.armed_item_id
        rating_key = sess.armed_rating_key
        qid = sess.armed_queue_id
        server = sess.server_machine_id
        if armed_track is None:
            return
        self._discard_arm(sess, clear_stale=False)
        sess.stale_item_id = item_id
        sess.stale_rating_key = rating_key
        _log.info("PlexPlayer gapless: revoking armed next %r (queue %s "
                  "item %s)", getattr(armed_track, "title", "?"), qid,
                  item_id)
        if (item_id is None or qid is None or server is None
                or self._pms_factory is None):
            _log.warning("PlexPlayer gapless: revoke has no addressable "
                         "item — stale watch armed (a boundary into it is "
                         "stop-and-replay corrected)")
            return
        try:
            pms = await self._pms_factory(server)
        except Exception:
            pms = None
        if pms is None:
            _log.warning("PlexPlayer gapless: revoke could not reach PMS — "
                         "stale watch stays armed")
            return
        try:
            await pms.delete_play_queue_item(qid, item_id)
        except CompanionError:
            _log.warning("PlexPlayer gapless: DELETE of armed item %s "
                         "failed — stale watch stays armed (stop-and-replay "
                         "on chain-in)", item_id, exc_info=True)
        finally:
            try:
                await pms.aclose()
            except Exception:
                pass
        try:
            await sess.client.refresh_play_queue(qid)
        except CompanionError:
            _log.warning("PlexPlayer gapless: revoke nudge failed",
                         exc_info=True)

    def _discard_arm(self, sess: _PlayerSession, *,
                     clear_stale: bool = True) -> None:
        """Drop every arm bookkeeping slot (fresh dispatch, EOS fallback,
        boundary consume, correction, foreign yield). Verdicts are NOT
        touched (the DLNA ``_discard_arm_state`` contract). Bumps the
        revoke epoch (PLX-7) so an in-flight arm delivery for the discarded
        slot abandons instead of resurrecting armed state."""
        sess.revoke_epoch += 1
        sess.pending_arm = None
        sess.armed_track = None
        sess.armed_item_id = None
        sess.armed_rating_key = None
        sess.armed_queue_id = None
        sess.queue_window = ()
        if clear_stale:
            sess.stale_item_id = None
            sess.stale_rating_key = None

    async def _decide_gapless_verdict(self, verdict: str) -> None:
        """Cache + persist the device's behavioral verdict (DLNA
        ``_decide_gapless_verdict`` mirror): the FIRST evidence on an
        unverified device decides; an already-decided device keeps its
        verdict (re-verification = clearing the persisted
        ``gapless_verdict:plexplayer:{device_id}`` setting). The in-memory
        write lands BEFORE any await so a racing reconcile already sees the
        arming gate decided; persistence + the picker-chip refresh are
        best-effort."""
        device_id = self._device_id or ""
        if not device_id:
            return
        if self._gapless_verdicts.get(device_id, "unverified") != "unverified":
            return
        self._gapless_verdicts[device_id] = verdict
        _log.info("PlexPlayer gapless: behavioral verdict for %r = %s",
                  device_id, verdict)
        from app import database
        try:
            await database.set_gapless_verdict("plexplayer", device_id,
                                               verdict)
        except Exception:
            _log.warning("PlexPlayer gapless: verdict persist failed",
                         exc_info=True)
        try:
            from app.output import watcher as watcher_mod
            w = watcher_mod.get_watcher()
            if w is not None and hasattr(w, "_schedule_broadcast"):
                w._schedule_broadcast()
        except Exception:
            _log.debug("PlexPlayer gapless: devices_changed refresh failed",
                       exc_info=True)

    # ── U7: boundary watch + foreign-controller verdict ───────────────────

    async def _observe_queue(self, sess: _PlayerSession,
                             tl: TimelineSnapshot) -> str:
        """One queue-observation tick on a successful timeline read. Returns
        ``"none"`` (keep polling), ``"boundary"`` (≥1 native advance was
        reconciled through the supervisor chokepoint — the SAME poll keeps
        running for the new track), ``"corrected"`` (a wrong/revoked next
        chained in — the caller stop-and-replays), or ``"yielded"`` (a
        foreign controller took the device — the hold is entered; the
        caller exits the loop without dispatching)."""
        qid = tl.play_queue_id
        if qid is None:
            return "none"   # idle/gone timeline: not evidence either way
        if not self._is_playing or sess.self_stopped:
            return "none"
        if self._is_stale_echo(sess, tl):
            _log.debug("PlexPlayer: stale timeline echo (commandID %s < "
                       "dispatch %s) — ignoring queue evidence",
                       tl.command_id, sess.dispatch_command_id)
            return "none"
        if qid not in sess.owned_queue_ids:
            # Dispatch-transition suppression: between our dispatch and its
            # first confirmation/adoption an unknown id is routine churn
            # (the player tearing down the old queue), never evidence.
            if sess.confirm_token is not None or sess.awaiting_queue_adoption:
                return "none"
            sess.foreign_reads += 1
            _log.warning("PlexPlayer: timeline names unowned playQueueID %s "
                         "(owned=%s) — foreign read %d/%d",
                         qid, sorted(sess.owned_queue_ids),
                         sess.foreign_reads, _FOREIGN_READ_STRIKES)
            if sess.foreign_reads >= _FOREIGN_READ_STRIKES:
                await self._yield_to_foreign(sess)
                return "yielded"
            return "none"
        sess.foreign_reads = 0
        if qid != sess.current_queue_id:
            return "none"   # echo of the retiring previous queue
        item = tl.play_queue_item_id
        if item is None:
            return "none"
        if sess.current_item_id is None:
            sess.current_item_id = item
            return "none"
        if item == sess.current_item_id:
            return "none"
        # ── the item edge ─────────────────────────────────────────────────
        if ((sess.stale_item_id is not None and item == sess.stale_item_id)
                or (sess.stale_item_id is None and sess.stale_rating_key
                    and str(tl.rating_key or "") == sess.stale_rating_key)):
            # The DEFINED revoke-failure fallback: the player chained into
            # the REVOKED next anyway (dlna.py:1219-1230).
            _log.warning("PlexPlayer gapless: player chained into the "
                         "REVOKED item %s — stop-and-replay correction",
                         item)
            self._discard_arm(sess)
            await self._send_corrective_stop(sess)
            return "corrected"
        armed = sess.armed_track
        if armed is not None and self._matches_armed(sess, tl, item):
            # The audible gapless transition happened and is detectable —
            # the behavioral criterion for "supported" (capability-map doc).
            _log.info("PlexPlayer gapless: playQueueItemID edge onto the "
                      "armed item %s — boundary detected", item)
            self._discard_arm(sess)   # one-shot BEFORE anything can await
            sess.current_item_id = item
            self._rearm_watchdog(sess, armed)
            await self._decide_gapless_verdict("supported")
            from app.output import session as output_session
            await output_session.notify_gapless_boundary(armed)
            return "boundary"
        window = sess.queue_window
        if window and item in window and sess.current_item_id in window:
            delta = window.index(item) - window.index(sess.current_item_id)
            if delta <= 0:
                # Reverse read: a replayed stale itemID after reconnect —
                # NEVER a backward advance (plan U7).
                _log.debug("PlexPlayer: stale itemID %s replayed (behind "
                           "current %s) — ignored", item,
                           sess.current_item_id)
                return "none"
            return await self._reconcile_forward(sess, item, delta)
        if armed is not None:
            # Armed, but the edge went somewhere we cannot attribute — a
            # wrong track is audible; correct it, never play it silently
            # (dlna.py:1231-1240).
            _log.warning("PlexPlayer gapless: playQueueItemID changed to "
                         "unexpected %s (armed item %s) — stop-and-replay "
                         "correction", item, sess.armed_item_id)
            self._discard_arm(sess)
            await self._send_corrective_stop(sess)
            return "corrected"
        # Not armed, no window ordering: an unattributable edge — not
        # evidence; EOS/watchdog converge (the DLNA unattributed-read
        # posture).
        _log.debug("PlexPlayer: unattributed playQueueItemID edge %s → %s",
                   sess.current_item_id, item)
        return "none"

    def _matches_armed(self, sess: _PlayerSession, tl: TimelineSnapshot,
                       item: int) -> bool:
        """Whether the observed item IS the armed one: by playQueueItemID
        when the append response exposed it, else by rating-key identity."""
        if sess.armed_item_id is not None:
            return item == sess.armed_item_id
        rk = sess.armed_rating_key
        return bool(rk and str(tl.rating_key or "") == str(rk))

    async def _reconcile_forward(self, sess: _PlayerSession,
                                 target_item: int, delta: int) -> str:
        """Forward jump WITHIN our queue (unexpected multi-item skip, or a
        short poll partition the player played through): accept the player
        as advance authority and reconcile one boundary per item through
        the supervisor chokepoint — counts stay 1:1 — capped at the fetched
        window length. The first step consumes the armed track; further
        steps take the live queue front (the same item the boundary advance
        is about to pop). Never backward (the caller filtered delta<=0)."""
        armed_track = sess.armed_track
        steps = min(delta, len(sess.queue_window))
        self._discard_arm(sess)
        _log.warning("PlexPlayer: player is %d item(s) ahead in our queue — "
                     "reconciling %d boundary(ies) forward", delta, steps)
        from app.output import session as output_session
        last_track = None
        for _ in range(steps):
            if armed_track is not None:
                track, armed_track = armed_track, None
            else:
                from app import state
                q = getattr(state.queue_engine, "queue", None) or []
                track = q[0].track if q else None
            if track is None:
                _log.warning("PlexPlayer: queue exhausted during forward "
                             "reconciliation — stopping early")
                break
            last_track = track
            await output_session.notify_gapless_boundary(track)
        sess.current_item_id = target_item
        if last_track is not None:
            self._rearm_watchdog(sess, last_track)
        return "boundary"

    async def _send_corrective_stop(self, sess: _PlayerSession) -> None:
        """The stop half of the stop-and-replay correction — best-effort
        (dlna._send_corrective_stop): the replay's fresh createPlayQueue
        supersedes the wrong track even when the stop is refused. NOT
        self.stop(): the self-stopped flag must stay clear — the caller
        immediately re-dispatches."""
        try:
            await sess.client.stop()
        except Exception:
            _log.warning("PlexPlayer gapless: corrective stop failed",
                         exc_info=True)

    def _rearm_watchdog(self, sess: _PlayerSession, track: Track) -> None:
        """A consumed boundary starts a new track WITHOUT a play(), but the
        duration watchdog armed at dispatch is still scoped to the OUTGOING
        track — left alone it would expire ~grace after the boundary and
        force a spurious advance (double-advance). Cancel + re-arm for the
        new track's duration under the SAME play generation."""
        self._cancel_watchdog(sess)
        duration_ms = int(getattr(track, "duration_ms", 0) or 0)
        if duration_ms > 0:
            sess.watchdog_task = asyncio.create_task(
                self._watchdog(sess, sess.play_gen, duration_ms))

    async def _yield_to_foreign(self, sess: _PlayerSession) -> None:
        """Foreign-controller yield (plan KTD: yield-and-notify): another
        controller owns the device's queue — stop dispatching (no advance,
        no outage-retry loop), enter the ``foreign_controller`` hold
        (idle-paused: only an admin re-activate recovers), and surface the
        admin notice on the established toast channel."""
        _log.warning("PlexPlayer: %d consecutive timeline reads named a "
                     "play queue we don't own — another Plex controller "
                     "took %r; yielding", sess.foreign_reads,
                     sess.name or sess.device_id)
        self._discard_arm(sess)
        self._is_playing = False
        self._cancel_watchdog(sess)
        from app.output import session as output_session
        try:
            await output_session.hold_foreign_controller()
        except Exception:
            _log.warning("PlexPlayer: foreign-controller hold entry failed",
                         exc_info=True)
        # Admin notice — the U6 vehicle, via the shared best-effort helper.
        from app.events.bus import notify_admin_error
        await notify_admin_error(
            "Another Plex controller took "
            f"“{sess.name or 'the Plex player'}” — "
            "jukeplox yielded; re-activate to resume")

    # ── duration watchdog backstop ────────────────────────────────────────

    async def _watchdog(self, sess: _PlayerSession, gen: int,
                        duration_ms: int) -> None:
        """Backstop for a dispatch whose EOS/boundary never surfaces
        (timeline gone, player wedged): sleep duration + grace, then — the
        Cast U2 fork — probe the PLAYER: reachable → exactly one forced
        advance; unreachable → outage-suspected (queue held, not consumed).
        A newer play() generation or stopped playback makes this a no-op;
        play()/stop() cancel it outright."""
        try:
            await asyncio.sleep(duration_ms / 1000 + WATCHDOG_GRACE_S)
        except asyncio.CancelledError:
            return
        if sess.play_gen != gen or not self._is_playing or sess.self_stopped:
            return
        reachable, _state = await self.probe_liveness()
        if sess.play_gen != gen or not self._is_playing:
            return  # superseded while the probe ran
        self._is_playing = False
        # Clear our own ref BEFORE any advance so a downstream
        # _cancel_watchdog can't cancel the task running it (self-cancel
        # trap — chromecast.py:1743).
        sess.watchdog_task = None
        self._cancel_poll(sess)
        if not reachable:
            if not self._is_current():
                # Review fix PLX-2: retired backend — never signal an outage
                # into the new backend's session.
                _log.warning("PlexPlayer watchdog: player unreachable but "
                             "backend is RETIRED — suppressing outage signal")
                return
            _log.warning("PlexPlayer watchdog: no terminal timeline and "
                         "player unreachable — reporting outage-suspected")
            from app.output import session as output_session
            output_session.notify_outage("watchdog_unreachable")
            return
        _log.warning("PlexPlayer watchdog: duration+grace expired with no "
                     "EOS — forcing a single advance")
        # U7 one-shot discipline: a still-armed slot dies with this forced
        # advance — the boundary it was waiting for never surfaced.
        self._discard_arm(sess)
        if self._advance_cb:
            await self._advance_cb()

    # ── task hygiene ──────────────────────────────────────────────────────

    def _cancel_poll(self, sess: _PlayerSession) -> None:
        if sess.poll_task and not sess.poll_task.done():
            sess.poll_task.cancel()
        sess.poll_task = None

    def _cancel_watchdog(self, sess: _PlayerSession) -> None:
        if sess.watchdog_task and not sess.watchdog_task.done():
            sess.watchdog_task.cancel()
        sess.watchdog_task = None

    # ── probes ────────────────────────────────────────────────────────────

    async def probe_liveness(self) -> tuple[bool, str | None]:
        """R15 reachability probe: one cheap ``timeline/poll?wait=0`` to the
        PLAYER (never the server — the server-down leg is track-level; see
        the module docstring). Never raises. The supervisor uppercases the
        returned transport state, so plex's "buffering"/"paused" map onto
        its PRE_PLAYBACK_STATES."""
        sess = self._session
        if sess is None:
            return (False, None)
        try:
            tl = await sess.client.poll_timeline(wait=0)
        except Exception:
            _log.debug("PlexPlayer probe_liveness failed", exc_info=True)
            return (False, None)
        return (True, tl.state)

    async def probe_device(self, device_id: str) -> bool:
        """Picker-facing probe (never raises): the bound device rides
        probe_liveness; an unbound one gets a transient client to its
        cached address for a single wait=0 read."""
        sess = self._session
        if sess is not None and sess.device_id == device_id:
            ok, _state = await self.probe_liveness()
            return ok
        addr = self._device_addresses.get(device_id)
        if addr is None or self._client_factory is None:
            return False
        try:
            client = self._client_factory(addr["host"], int(addr["port"]),
                                          device_id)
        except Exception:
            _log.warning("PlexPlayer probe_device: client build failed for "
                         "%r", device_id, exc_info=True)
            return False
        try:
            await client.poll_timeline(wait=0)
            return True
        except Exception:
            _log.debug("PlexPlayer probe_device: %r unreachable", device_id,
                       exc_info=True)
            return False
        finally:
            try:
                await client.aclose()
            except Exception:
                pass

    # ── transport controls ────────────────────────────────────────────────

    async def pause(self) -> None:
        sess = self._session
        if sess is None:
            return
        await sess.client.pause()
        # Freeze the position anchor at the pause point; the next timeline
        # snapshot re-syncs it (echo verification rides the running poll).
        sess.last_time_ms = self._interpolated_position(sess)
        sess.last_state = "paused"
        sess.last_at = time.monotonic()
        self._is_playing = False

    async def resume(self) -> None:
        sess = self._session
        if sess is None:
            return
        await sess.client.play()
        sess.last_state = "playing"
        sess.last_at = time.monotonic()
        self._is_playing = True

    async def stop(self) -> None:
        """Teardown on switch-away / queue end. Order matters (plan U2):
        1. flag self-induced + cancel the poll loop FIRST (no late stopped
           read can ever advance), 2. send the stop command, 3. verify via
           one timeline read. A failed stop or verification logs a clear
           warning and exposes ``last_teardown_warning`` ("player may still
           be playing — stop it from a Plex app"); U6/U7 wire the admin
           notice event. Non-destructive like DLNA stop(): the session and
           client stay bound so a subsequent play() reuses them."""
        self.last_teardown_warning = None
        sess = self._session
        self._is_playing = False
        if sess is None:
            return
        sess.self_stopped = True
        self._cancel_poll(sess)
        self._cancel_watchdog(sess)
        warning: str | None = None
        try:
            await sess.client.stop()
        except CompanionError as exc:
            warning = f"stop command failed: {exc}"
        if warning is None:
            try:
                tl = await sess.client.poll_timeline(wait=0)
                self._note_snapshot(sess, tl)
                if tl.state == "playing":
                    warning = "player still reports playing after stop"
            except CompanionError as exc:
                warning = f"stop verification read failed: {exc}"
        if warning is not None:
            # U2 surface: log + attribute; the admin-notice event pathway
            # (U6/U7) reads this instead of pretending success.
            self.last_teardown_warning = warning
            _log.warning(
                "PlexPlayer teardown: %s — the player may still be playing; "
                "stop it from a Plex app", warning)

    async def seek(self, position_ms: int) -> None:
        """seekTo, re-anchoring the local position only on success (the
        DLNA posture: a failed seek must not make get_position lie). Never
        raises into the API layer."""
        sess = self._session
        if sess is None:
            return
        position_ms = max(0, int(position_ms))
        try:
            await sess.client.seek_to(position_ms)
        except CompanionError:
            _log.warning("PlexPlayer seek to %dms failed", position_ms,
                         exc_info=True)
            return
        sess.last_time_ms = position_ms
        sess.last_at = time.monotonic()

    # ── volume / position ─────────────────────────────────────────────────

    async def set_volume(self, level: float) -> None:
        self._volume = max(0.0, min(1.0, level))
        # Stamp the echo-guard window BEFORE the device write so the
        # timeline's confirmation echo is suppressed (base.py contract).
        self._vol_last_set = time.monotonic()
        sess = self._session
        if sess is not None:
            await sess.client.set_parameters(
                volume=int(round(self._volume * 100)))
        if self._device_id:
            from app import database
            await database.set_setting(
                f"vol:plexplayer:{self._device_id}", str(self._volume))

    async def get_volume(self) -> float:
        return self._volume

    async def get_position(self) -> int:
        sess = self._session
        if sess is None:
            return 0
        return self._interpolated_position(sess)

    def _interpolated_position(self, sess: _PlayerSession) -> int:
        """Latest timeline position, interpolated on the monotonic clock
        while playing (timeline ticks are ~1s apart; the progress bar wants
        smooth reads — the dlna/chromecast wall-clock-anchor pattern)."""
        if sess.last_time_ms is None:
            return 0
        pos = sess.last_time_ms
        if sess.last_state == "playing":
            pos += int((time.monotonic() - sess.last_at) * 1000)
        return max(0, pos)

    @property
    def is_playing(self) -> bool:
        return self._is_playing
