"""Output-session supervisor — the confirmed-playback-start chokepoint.

2026-07-11 supervisor plan U1. Dispatching a track to a backend proves only
that the command was accepted; it is never proof of audio (see
docs/solutions/best-practices/control-plane-success-data-plane-silent.md).
This module owns the gap between the two: every play-start entry point
reports its dispatch here (``on_dispatched``), each backend reports the
first data-plane evidence of playback (``on_playback_confirmed``), and
``record_play`` fires exactly once per dispatched token from that
confirmation — never from dispatch.

A dispatch that is never confirmed within the deadline emits an
"outage suspected" signal to registered listeners, and every re-pointed
backend advance-authority path (Cast connection LOST / unreachable-watchdog,
DLNA 3x poll errors, Direct sink error, AirPlay crash — R16) reports here
via ``notify_outage``. The supervisor core still only notifies; deciding
what an outage means is the job of the U2 classifier (``classify_outage``),
which production wiring registers as an outage listener: device-level →
outage hold (pause + re-front-insert the interrupted item, R15/R18);
track-level (device demonstrably reachable) → today's skip behavior.
R15's third outcome guards slow starts: when the device is reachable AND
the transport reports a pre-playback state, the deadline is extended
exactly once before classifying, so a cold source buffer cannot cascade
healthy tracks into outage handling.

Backends must call in on the event loop — backend threads hop via
``call_soon_threadsafe`` / ``run_coroutine_threadsafe`` exactly as their
existing EOS paths do, so the supervisor itself needs no locking.

Decomposition (2026-07-11 supervisor plan, pre-Phase-B maintainability
split): the outage-hold flag and its operations live in ``app.output.hold``
(the ONLY home of the mutable hold state); the U4 observability surface
(snapshots + event emission) lives in ``app.output.session_events``. This
module keeps the supervisor/state machine, the U2 classifier, and the
module-singleton ``notify_*`` surface — and re-exports the moved PUBLIC
functions below so every production call site keeps importing
``from app.output import session`` and calling ``session.<name>`` unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Awaitable, Callable

from app.output import hold, session_events
# Facade re-exports (see the decomposition note above): FUNCTIONS only —
# never the mutable ``hold._output_hold`` flag itself (a from-import would
# snapshot the bool; readers go through ``hold.output_hold_active()``).
from app.output.hold import (  # noqa: F401
    clear_output_hold,
    enter_output_hold,
    output_hold_active,
    output_hold_reason,
)
from app.output.session_events import (  # noqa: F401
    _schedule_emit,
    emit_session_event,
    session_snapshot,
    session_snapshot_admin,
)

_log = logging.getLogger(__name__)

# Confirmation deadline: how long a dispatch may sit unconfirmed before the
# supervisor classifies it. Starts at 12s (deferred-to-implementation tuning,
# plan band ~10–15s); injectable per-instance for tests and future per-backend
# tuning.
CONFIRM_DEADLINE_S = 12.0

# The single bounded extension granted by R15's third outcome (device
# reachable + transport in a pre-playback state at deadline expiry).
CONFIRM_EXTENSION_S = 12.0

# U10 (Cast flow mode): how many superseded-but-unconfirmed flow-deferred
# dispatches the supervisor retains awaiting their device-time crossing. In
# practice at most a couple can be outstanding (run-ahead ÷ shortest track),
# and the backend's periodic status poll keeps crossings flowing, so the cap
# only bounds pathological churn — hitting it means a real count is being
# lost (eviction logs a WARNING; the evicted stub simply never counts).
_FLOW_DEFERRED_CAP = 256

# Transport states that count as "pre-playback" for the extension: the device
# is visibly working on our media, just not rendering yet.
#   Cast:   BUFFERING / LOADING
#   DLNA:   TRANSITIONING
#   Direct: ASYNC (pipeline mid async state change) / PAUSED (prerolling)
PRE_PLAYBACK_STATES = frozenset({
    "BUFFERING", "LOADING",
    "TRANSITIONING",
    "ASYNC", "PAUSED",
})

# Per-backend probe interface (U1 minimal; U2 wires real backend probes via
# ``app.state._output_probe``): an async callable returning
# (reachable, transport_state or None). Probe semantics per backend (plan KTD):
# Cast = socket/status liveness; DLNA = transport-info query; Direct =
# audio-sink element liveness; AirPlay = TCP reachability of the cached
# receiver address (cliap2 exposes no transport state).
ProbeFn = Callable[[], Awaitable[tuple[bool, str | None]]]

# Outage reasons whose REPORTER already established the device is gone — the
# classifier must not second-guess them with a probe:
#   connection_lost       Cast socket LOST mid-playback (unreachable by definition)
#   watchdog_unreachable  Cast duration-watchdog expiry + failed liveness probe
#   poll_errors           DLNA: 3x consecutive transport-poll errors ARE the probe
#   sink_error            Direct: sink/RESOURCE bus error — the local device died
# Every other reason ("confirm_timeout", "process_crash", …) is ambiguous and
# gets the classifier's reachability probe as the R15 tie-breaker.
DEVICE_LEVEL_REASONS = frozenset({
    "connection_lost",
    "watchdog_unreachable",
    "poll_errors",
    "sink_error",
})

# Flow-mode receiver transients that are RECOVERABLE, not track-level failures.
# A Cast flow receiver can report IDLE(ERROR) or stop pulling the stream
# (consumer-gone) mid-flow while its control socket stays UP — so the R15
# reachability probe reads "reachable" and the old code SKIPPED the (playing)
# track. A 3-hour hardware soak (2026-08-08) showed these are transient Linkplay
# hiccups (the same family as connection_lost, which the socket-drop path
# already recovers by hold + auto-resume-at-position), NOT bad tracks — a bad
# track fails at DECODE (FlowDecodeError → server-side _flow_skip), never here.
# So route these through the SAME hold + auto-resume path connection_lost uses;
# the R19 flap guard bounds a persistently-flapping receiver (holds for manual
# after FLAP_GUARD_N short-lived resumes) instead of skipping a live track.
FLOW_RECOVERABLE_REASONS = frozenset({
    "flow_receiver_idle",
    "flow_consumer_gone",
})

# Stuck-track backstop for the flow recovery path. Hold + auto-resume recovers a
# transient, but a receiver that keeps erroring at ~the SAME position (content
# the server decodes yet the receiver rejects, or a wedged media pipeline that
# won't consume the re-LOAD) would otherwise recover-loop forever — strictly
# worse than the old skip. So: after FLOW_RECOVER_STUCK_LIMIT recoveries on the
# same track without at least FLOW_RECOVER_PROGRESS_MS of forward progress, fall
# back to a track-level skip (the pre-fix backstop). A track whose held position
# ADVANCES between hiccups is a genuine transient and keeps recovering
# unbounded. NOTE: the R19 flap guard is adjacency-gated (only counts resumes
# that re-fail within FLAP_SHORT_LIVED_S), so it does NOT bound a slow-cadence
# receiver; this progress-aware cap is what actually bounds the flow path.
FLOW_RECOVER_STUCK_LIMIT = 3
FLOW_RECOVER_PROGRESS_MS = 10_000

# ── reconnect / auto-resume tuning (U3) ───────────────────────────────────────
# Backoff schedule for the direct-address retry loop: short fixed start,
# exponential growth, LAN-sender-norm cap (~300s). Discovery arrival and the
# Cast CONNECTED listener short-circuit whatever wait is pending.
RETRY_BACKOFF_START_S = 5.0
RETRY_BACKOFF_FACTOR = 2.0
RETRY_BACKOFF_CAP_S = 300.0

# Flap guard (R19): FLAP_GUARD_N short-lived auto-resumes within
# FLAP_GUARD_WINDOW_S → the next re-attach holds for MANUAL resume instead of
# auto-playing. "Short-lived" = the outage re-entered within
# FLAP_SHORT_LIVED_S of an auto-resume.
FLAP_GUARD_N = 3
FLAP_GUARD_WINDOW_S = 600.0
FLAP_SHORT_LIVED_S = 60.0

# ── session state machine (U3; plan diagram — U4 broadcasts these) ────────────
# was_paused is carried alongside (supervisor attribute), not a state of its
# own: OutagePaused with was_paused=True re-attaches into STATE_PAUSED.
STATE_IDLE = "idle"
STATE_PLAYING = "playing"
STATE_PAUSED = "paused"
STATE_OUTAGE_PAUSED = "outage_paused"
STATE_RECONNECTING = "reconnecting"
STATE_IDLE_PAUSED = "idle_paused"

# Serializes every set_device-bearing attach: the supervisor's re-attach
# seeding AND state.activate_backend's manual switch. set_device commits
# backend internals (_cast/_device_id) from an uncancellable executor thread
# on a shared singleton, so without ordering a stale attach can finish LAST
# and overwrite the freshly switched device's state. With the lock, the old
# attach either completes before the switch's attach starts, or acquires
# after it, observes the bumped attach-epoch, and never calls set_device.
_attach_serial = asyncio.Lock()

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _looks_like_uuid(value: str) -> bool:
    return bool(_UUID_RE.match(value or ""))


def _log_task_exc(task: asyncio.Task) -> None:
    if not task.cancelled():
        exc = task.exception()
        if exc:
            _log.error("Supervisor task raised: %s", exc, exc_info=exc)


def _spawn_supervised(coro: Any) -> None:
    """Fire-and-forget ``coro`` on the running loop with exception logging —
    the shared hop for every sync entry point (timer callbacks, listener
    notifications) whose work is async. Loop gone (shutdown) → drop the
    work; supervisor timers and triggers have nothing left to act on."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()  # not awaited by design — suppress the warning
        return
    task = loop.create_task(coro)
    task.add_done_callback(_log_task_exc)


def _default_timer_factory(delay_s: float, cb: Callable[[], None]):
    """``loop.call_later`` behind an injectable seam so tests drive deadlines
    with fake timers instead of real sleeps (repo pytest-hang policy). The
    returned handle only needs ``.cancel()``."""
    return asyncio.get_running_loop().call_later(delay_s, cb)


def _default_record_play(track: Any) -> None:
    # Late import + attribute lookup so the state<->output import cycle never
    # forms and existing test patches of app.state.record_play keep working.
    from app import state
    state.record_play(track)


class _Dispatch:
    """One dispatched play awaiting its confirmed-start signal.

    ``deferred`` (U10 Cast flow mode): confirmation is keyed to the DEVICE-
    reported position crossing the track's stitch-timeline boundary offset —
    no deadline timer runs (device buffer lag lawfully exceeds any fixed
    deadline; the flow session's connection/consumer liveness owns failure
    detection), and a superseding boundary dispatch parks this one in the
    supervisor's deferred stash so its own crossing can still count it."""

    __slots__ = ("token", "track", "play_recorded", "probe",
                 "confirmed", "extended", "timer", "deferred")

    def __init__(self, token: int, track: Any, play_recorded: bool,
                 probe: ProbeFn | None) -> None:
        self.token = token
        self.track = track
        self.play_recorded = play_recorded
        self.probe = probe
        self.confirmed = False
        self.extended = False
        self.timer: Any = None
        self.deferred = False


class _Outage:
    """Reconnect context for ONE outage hold (U3): everything captured at
    entry that the retry loop and the resume orchestration need. Created by
    ``begin_outage`` (once per hold — the hold flag's idempotence guarantees
    it); retired by ``clear_output_hold`` or a manual device switch."""

    __slots__ = ("reason", "backend", "backend_type", "device_id",
                 "was_paused", "held_position_ms", "held_gen", "held_track_id",
                 "entered_at", "delay_s", "timer", "attempts",
                 "attempt_inflight", "attached", "flap_tripped",
                 "pending_volume", "arrival_cb", "retired")

    def __init__(self, reason: str) -> None:
        self.reason = reason
        self.backend: Any = None
        self.backend_type = ""
        self.device_id = ""
        self.was_paused = False
        self.held_position_ms = 0
        self.held_gen = 0
        self.held_track_id = ""
        self.entered_at = 0.0
        self.delay_s = RETRY_BACKOFF_START_S
        self.timer: Any = None
        self.attempts = 0
        self.attempt_inflight = False
        self.attached = False
        self.flap_tripped = False
        self.pending_volume: float | None = None
        self.arrival_cb: Any = None
        self.retired = False

    def cancel_timer(self) -> None:
        t, self.timer = self.timer, None
        if t is not None:
            try:
                t.cancel()
            except Exception:
                pass


class OutputSessionSupervisor:
    """Signal intake + confirmed-start chokepoint (the U1 skeleton of the
    session state machine; U2/U3 add classification, hold, and reconnect)."""

    def __init__(
        self,
        *,
        record_play: Callable[[Any], None] | None = None,
        deadline_s: float = CONFIRM_DEADLINE_S,
        extension_s: float = CONFIRM_EXTENSION_S,
        timer_factory: Callable[[float, Callable[[], None]], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        window_minutes: Callable[[], Awaitable[int]] | None = None,
        identity_check: Callable[[Any, str, str], Awaitable[bool]] | None = None,
        held_track_check: Callable[[Any], Awaitable[bool]] | None = None,
    ) -> None:
        self._record_play = record_play or _default_record_play
        self._deadline_s = deadline_s
        self._extension_s = extension_s
        self._timer_factory = timer_factory or _default_timer_factory
        self._token = 0
        self._current: _Dispatch | None = None
        # The most recent dispatch an outage emission retired (U2): the
        # classifier consults it AFTER emission to stamp the held item's
        # play_recorded mark (see dispatch_play_recorded).
        self._retired: _Dispatch | None = None
        # U10 (Cast flow mode): flow-deferred dispatches superseded by a
        # LATER boundary while still awaiting their own device-time crossing
        # (token → _Dispatch, insertion-ordered, capped). Unlike per-track
        # supersession, a flow boundary superseding a dispatch does NOT mean
        # its audio was cut — tracks stream sequentially, so each deferred
        # dispatch keeps its right to count when ITS offset is crossed.
        self._flow_deferred: dict[int, _Dispatch] = {}
        self._outage_listeners: list[Callable[[int, Any, str], None]] = []
        # ── U3: reconnect loop + session state machine ────────────────────
        # All timers/clocks injectable — tests drive backoff, the resume
        # window and the flap guard on fake clocks, never real sleeps.
        self._clock = clock
        self._window_minutes = window_minutes or _default_window_minutes
        self._identity_check = identity_check or _default_identity_check
        self._held_track_check = held_track_check or _default_held_track_check
        # Explicit session state (plan diagram; U4 broadcasts it). Owned
        # transitions: confirmed start → PLAYING; begin_outage →
        # OUTAGE_PAUSED (was_paused carried); attempts → RECONNECTING;
        # landings → PAUSED / IDLE_PAUSED / PLAYING. idle_paused_reason names
        # WHY an attached device is not auto-playing (window_expired /
        # flap_guard / closing_time / track_identity / no_media_source).
        self.session_state: str = STATE_IDLE
        self.was_paused: bool = False
        self.idle_paused_reason: str = ""
        # Attach-epoch: bumped on every manual switch/backend change. An
        # executor-bound attach cannot be cancelled, so each attempt captures
        # this BEFORE attaching and re-validates it after every await — the
        # only protection against a stale attach committing state.
        self.attach_epoch: int = 0
        self._outage: _Outage | None = None
        self._flap_stamps: list[float] = []
        self._last_auto_resume_at: float | None = None
        # Flow-recovery stuck-track guard (see FLOW_RECOVER_STUCK_LIMIT): tracks
        # consecutive flow recoveries on the same track WITHOUT forward progress.
        self._flow_recover_track_id: Any = None
        self._flow_recover_pos_ms: int = 0
        self._flow_recover_count: int = 0

    def flow_recovery_allowed(self, track_id: Any, pos_ms: int) -> bool:
        """Stuck-track backstop for the flow recovery path (FLOW_RECOVERABLE_
        REASONS). Return True to recover (hold + auto-resume), False to fall
        back to a track-level skip. A track that keeps hiccuping WITHOUT forward
        progress (same track, position not advanced by >= FLOW_RECOVER_
        PROGRESS_MS) is capped at FLOW_RECOVER_STUCK_LIMIT recoveries, then
        skipped. Forward progress (or a new track) refreshes the budget, so a
        genuine transient on a normally-advancing track recovers unbounded.
        Degrades safely: if position is unavailable (reads ~0 each time) this
        collapses to a plain 3-strikes-then-skip on the same track."""
        if (track_id != self._flow_recover_track_id
                or pos_ms - self._flow_recover_pos_ms >= FLOW_RECOVER_PROGRESS_MS):
            self._flow_recover_track_id = track_id
            self._flow_recover_pos_ms = pos_ms
            self._flow_recover_count = 1
            return True
        self._flow_recover_count += 1
        if pos_ms > self._flow_recover_pos_ms:
            self._flow_recover_pos_ms = pos_ms
        return self._flow_recover_count <= FLOW_RECOVER_STUCK_LIMIT

    # ── outage-suspected emission hook (consumed by U2) ────────────────────

    def add_outage_listener(self, cb: Callable[[int, Any, str], None]) -> None:
        """Register ``cb(token, track, reason)`` for outage-suspected
        emissions. The supervisor only notifies — it never advances or skips
        anything itself; deciding what an outage means is the listener's job."""
        self._outage_listeners.append(cb)

    # ── signal intake ──────────────────────────────────────────────────────

    def current_token(self) -> int | None:
        """Token of the current dispatch (confirmed or pending), or None.
        Backends capture this at their play() entry so their confirmation
        signal names the dispatch it belongs to."""
        d = self._current
        return d.token if d is not None else None

    def on_dispatched(self, track: Any, *, play_recorded: bool = False,
                      probe: ProbeFn | None = None,
                      deadline: bool = True) -> int:
        """A play-start entry point handed ``track`` to the active backend.

        Issues and returns the per-dispatch token, superseding any prior
        dispatch (its stale token is ignored from now on) and arming the
        confirmation deadline. ``play_recorded`` is the R19 mark: a resume
        path replaying an already-counted item sets it so confirmation skips
        counting. ``probe`` is the minimal R15 tie-breaker used only at
        deadline expiry (see ``ProbeFn``).

        ``deadline=False`` (U10 Cast flow boundaries): the dispatch is
        flow-DEFERRED — no confirmation timer runs and the confirm fires when
        the backend maps the DEVICE-reported position across the boundary's
        stitch offset (see ``_Dispatch.deferred``)."""
        self._cancel_timer()
        self._token += 1
        prev = self._current
        d = _Dispatch(self._token, track, play_recorded, probe)
        self._current = d
        if (prev is not None and not prev.confirmed
                and (prev.deferred or not deadline)):
            # A flow boundary superseding a still-uncrossed dispatch: park
            # it — its own device-time crossing may still count it. Covers
            # both the deferred-by-construction boundary dispatches and the
            # flow session's initial LOAD dispatch (deadline armed) when the
            # first boundary lands before its PLAYING confirmation — its
            # deadline timer was already cancelled above. A parked entry
            # that never confirms lingers harmlessly: tokens are never
            # reused, the backend's teardown clears its crossing ledger (so
            # no stale confirm can name it), and the cap bounds memory.
            prev.deferred = True
            self._flow_deferred[prev.token] = prev
            while len(self._flow_deferred) > _FLOW_DEFERRED_CAP:
                evicted = self._flow_deferred.pop(
                    next(iter(self._flow_deferred)))
                _log.warning(
                    "Output session: flow-deferred cap (%d) evicted %r — "
                    "its device-time crossing can no longer count it",
                    _FLOW_DEFERRED_CAP, _title(evicted.track))
        if deadline:
            d.timer = self._timer_factory(
                self._deadline_s, lambda: self._deadline_fired(d.token))
        else:
            d.deferred = True
        return d.token

    def defer_confirmation(self, token: int) -> None:
        """Convert the current dispatch to flow-deferred confirmation (U10).

        The Cast backend calls this when a normal ``dispatch_play`` lands as
        a stitcher REPOSITION (skip/skip-back while a flow session is live):
        the media session is untouched, so no fresh PLAYING status is coming,
        and the audible transition lags by the run-ahead + device buffer —
        the standard deadline would misclassify that lag as an outage. The
        deadline timer is cancelled; the confirm/count fires at the device-
        time crossing of the reposition boundary. Stale/confirmed tokens are
        ignored."""
        d = self._current
        if d is None or d.token != token or d.confirmed:
            return
        self._cancel_timer()
        d.deferred = True

    def on_dispatch_failed(self, token: int) -> None:
        """The dispatch never reached the device (holder/backend raised before
        playback could start). Withdraw it so it cannot age into a spurious
        outage emission — the dispatching path owns that failure today."""
        d = self._current
        if d is None or d.token != token:
            return
        self._cancel_timer()
        self._current = None

    def on_playback_confirmed(self, token: int) -> None:
        """A backend reported the first data-plane evidence of playback for
        ``token``. Counts the play exactly once per dispatched token; stale
        tokens (superseded, withdrawn, or already classified) are ignored —
        except a flow-DEFERRED dispatch parked by a later boundary (U10),
        whose own device-time crossing still counts it."""
        d = self._current
        if d is None or d.token != token or d.confirmed:
            self._confirm_deferred_crossing(token)
            return
        d.confirmed = True
        self._cancel_timer()
        # Confirmed audio is proof the output is alive again — a live outage
        # hold ends here (clearing also retires the U3 reconnect machinery;
        # the resume orchestration usually cleared it at dispatch already).
        hold.clear_output_hold()
        # U3 state machine: confirmed audio means Playing, whatever came
        # before — this is the diagram's only entry into the Playing state.
        self.session_state = STATE_PLAYING
        self.was_paused = False
        if d.play_recorded:
            _log.info("Output session: confirmed start for %r (play already "
                      "recorded — not re-counting)", _title(d.track))
            return
        _log.info("Output session: confirmed start for %r — recording play",
                  _title(d.track))
        try:
            self._record_play(d.track)
        except Exception:
            _log.warning("record_play failed for %r", _title(d.track),
                         exc_info=True)

    def _confirm_deferred_crossing(self, token: int) -> None:
        """A flow-deferred dispatch superseded by a later boundary reached its
        own device-time crossing (U10): the audio WAS heard, so count it —
        through the same R19 mark semantics — WITHOUT touching the session
        state machine or the hold (the live dispatch owns those). Unknown
        tokens are the normal stale-signal no-op."""
        d = self._flow_deferred.pop(token, None)
        if d is None or d.confirmed:
            return
        d.confirmed = True
        if d.play_recorded:
            _log.info("Output session: device-time crossing for %r (play "
                      "already recorded — not re-counting)", _title(d.track))
            return
        _log.info("Output session: device-time crossing confirmed %r — "
                  "recording play", _title(d.track))
        try:
            self._record_play(d.track)
        except Exception:
            _log.warning("record_play failed for %r", _title(d.track),
                         exc_info=True)

    def on_outage_reported(self, reason: str) -> None:
        """A backend advance-authority path re-pointed here (U2, R16): Cast
        connection LOST / watchdog-with-unreachable-device, DLNA 3x poll
        errors, Direct sink error, AirPlay crash. Emits outage-suspected for
        the current dispatch (retiring it — a late confirmation must not
        count); with no live dispatch (e.g. the deadline already retired it)
        the listeners still hear the signal so the classifier can hold
        whatever the queue is playing."""
        d = self._current
        if d is not None:
            self._cancel_timer()
            self._emit_outage_suspected(d, reason=reason)
            return
        _log.warning("Output session: outage suspected (%s) — no live dispatch",
                     reason)
        for cb in list(self._outage_listeners):
            try:
                cb(-1, None, reason)
            except Exception:
                _log.warning("Output session: outage listener raised",
                             exc_info=True)

    def dispatch_play_recorded(self, token: int) -> bool:
        """Whether the play for dispatch ``token`` has already been counted
        (confirmed here, or dispatched carrying the R19 mark). U2's hold
        mechanic stamps the held item's ``play_recorded`` mark with this so a
        resume never re-counts. Unknown tokens default True — R19 ranks
        never-count-twice above never-miss-one."""
        for d in (self._current, self._retired):
            if d is not None and d.token == token:
                return d.play_recorded or d.confirmed
        return True

    # ── gapless boundary (U7: Direct STREAM_START advance authority) ───────

    async def on_gapless_boundary(self, track: Any) -> None:
        """A gapless backend reported the audible transition to a consumed
        armed track — the advance-authority table's gapless rows (R16):
        Direct's STREAM_START after about-to-finish swapped the uri (U7),
        and DLNA's CurrentTrackURI flip to the expected armed next while the
        transport stays PLAYING (U8 — including its late-boundary correction
        for renderers whose URI reporting goes stale).

        At a gapless boundary there is NO dispatch — the device chained into
        the next track on its own — so this does two things:

        1. ADVANCES the queue exactly as ``state._do_advance`` would, minus
           ``_play_with_fallback``: ``queue_engine.advance()`` pops the
           consumed track to current (the outgoing one to history) and its
           events update Now Playing and re-run the arming reconcile (which
           revokes the consumed slot and arms the FOLLOWING track).
        2. Reports the audible track through the U1 chokepoint as a dispatch
           WITHOUT ``router.play``, confirmed immediately. Same token model
           as every other play-start: ``record_play`` fires exactly once,
           the popped item's ``play_recorded`` mark is respected (R19), the
           supervisor's ``current_token()`` names the audibly playing track
           for any later outage classification, and signals for the OLD
           track go stale by token. A parallel "boundary-confirmed" entry
           would duplicate the chokepoint's count/hold/state logic and give
           outage classification a dispatch record that lies about what is
           audible — one chokepoint, one bookkeeping path.

        Concurrency: skip bumps ``_advance_gen`` BEFORE taking
        ``_advance_lock``, so a boundary that lost to a skip drops at the
        gen re-check after acquiring the lock — the boundary WAITS for the
        lock rather than bailing (an in-flight dispatch holding it merely
        delays the boundary; the backend's play-generation guard already
        dropped boundaries from torn-down pipelines). Hold active → the
        queue is frozen (R15).
        Closing Time needs no check here: U6's arm-time check (R21) never
        arms past the send-off track, so a consumed boundary cannot cross
        it — an unarmed send-off ends in EOS, and _do_advance freezes there.
        """
        await self._boundary_advance(track, confirm_now=True)

    async def on_flow_boundary(self, track: Any) -> int | None:
        """Cast flow-mode boundary (U10): the server stitcher's ENCODE clock
        crossed into ``track`` — the flow advance authority (R16's
        Chromecast-flow row). The two-phase counting split:

        1. NOW (boundary clock): advance the queue exactly like
           ``on_gapless_boundary`` — Now Playing and the queue follow the
           encode clock (device lag accepted, the Music Assistant precedent)
           — and issue the track's dispatch token through the U1 chokepoint.
        2. LATER (device clock): the confirm — and with it ``record_play`` —
           fires only when the backend maps the DEVICE-reported position
           across this boundary's stitch offset (``notify_confirmed`` with
           the returned token). No deadline timer runs (``deadline=False``):
           the device buffer lag lawfully exceeds any fixed deadline, and
           flow liveness is owned by connection status + stream consumption.

        Returns the UNCONFIRMED token for the backend's pending-count ledger,
        or None when a skip/hold owns the transition (no advance happened —
        register nothing). No play is EVER counted for audio the listener
        never heard: an uncrossed boundary the backend cancels (skip past it,
        outage teardown) simply never confirms."""
        return await self._boundary_advance(track, confirm_now=False)

    async def _boundary_advance(self, track: Any, *,
                                confirm_now: bool) -> int | None:
        """Shared boundary-advance mechanics for the gapless (U7/U8) and flow
        (U10) advance authorities — single-sourced so the guard ordering and
        R19 mark handling cannot drift between them. ``confirm_now`` is the
        only divergence: per-track gapless boundaries ARE the audible
        transition (confirm immediately); flow boundaries are encode-side
        (dispatch deferred to the device-time crossing)."""
        from app import state
        if hold.output_hold_active():
            return None
        captured_gen = state.advance_gen()
        # AWAIT the lock — never bail on locked(): the initial flow LOAD
        # lawfully holds _advance_lock past the first boundary when the
        # first track is shorter than the run-ahead (the pump starts before
        # run_in_executor returns), and bailing there permanently dropped
        # the advance AND its count. Awaiting is safe: boundaries run on the
        # pump task (flow) or a marshaled loop task (gapless), never on the
        # lock holder. A skip still supersedes — it bumps _advance_gen
        # BEFORE taking the lock, so the re-check below drops any boundary
        # that captured the pre-skip generation; a hold entered while the
        # boundary was parked keeps the queue frozen (R15).
        async with state.advance_lock():
            if state.advance_gen() != captured_gen or hold.output_hold_active():
                return None
            next_item = await state.queue_engine.advance()
            play_recorded = False
            if next_item is None:
                # The queue emptied between arm-consumption and the audible
                # boundary (a revoke that landed after playbin pre-rolled).
                # The track IS audibly playing — count it; the queue reads
                # idle and the track's own EOS converges in _do_advance.
                _log.warning(
                    "Gapless: boundary for %r but the queue produced no item "
                    "— counting the audible track; queue reads idle",
                    _title(track),
                )
            elif (next_item.track is not track
                    and getattr(next_item.track, "id", None)
                    != getattr(track, "id", None)):
                # The front changed after the arm was consumed (too late to
                # revoke device-side). Audible reality wins the play count;
                # the popped item stands as current and the next boundary or
                # EOS self-corrects. Rare by construction — U6 revokes on
                # every queue_changed.
                _log.warning(
                    "Gapless: boundary track %r does not match the queue "
                    "front %r — counting the audible track",
                    _title(track), _title(next_item.track),
                )
            else:
                # The R19 mark protected THIS pending play; consume it so a
                # later organic replay counts again (the _play_with_fallback
                # posture).
                play_recorded = bool(getattr(next_item, "play_recorded",
                                             False))
                next_item.play_recorded = False
            token = self.on_dispatched(track, play_recorded=play_recorded,
                                       probe=state._output_probe(),
                                       deadline=confirm_now)
            if confirm_now:
                self.on_playback_confirmed(token)
            return token

    # ── confirmation deadline ──────────────────────────────────────────────

    def _cancel_timer(self) -> None:
        d = self._current
        if d is not None and d.timer is not None:
            try:
                d.timer.cancel()
            except Exception:
                pass
            d.timer = None

    def _deadline_fired(self, token: int) -> None:
        # Timer callbacks are sync; the R15 probe is async — hop into a task.
        _spawn_supervised(self._deadline_expired(token))

    async def _deadline_expired(self, token: int) -> None:
        from app import state
        d = self._current
        if d is None or d.token != token or d.confirmed or d.deferred:
            # Superseded / withdrawn / confirmed — stale deadline. Deferred
            # (U10): defer_confirmation cancelled the timer, but a callback
            # already in flight when the cancel landed must not classify a
            # dispatch whose confirmation lawfully outlives any deadline.
            return
        if state.queue_engine.state.is_paused:
            # User paused during the confirmation window: no confirmation can
            # arrive while the transport is paused, and classifying now would
            # force-skip a healthy track (PAUSED is a PRE_PLAYBACK state — one
            # extension, then track-level skip). A hold can't be active here:
            # its emission would have retired this dispatch. Suspend — re-arm
            # WITHOUT consuming the single R15 extension; confirmation fires
            # normally at user resume.
            _log.info("Output session: confirmation deadline suspended — "
                      "session paused")
            d.timer = self._timer_factory(
                self._deadline_s, lambda: self._deadline_fired(token))
            return
        if not d.extended and d.probe is not None:
            # R15 third outcome: reachable device + pre-playback transport →
            # extend exactly once before classifying (a cold source buffer
            # must not skip-cascade healthy tracks).
            try:
                reachable, transport_state = await d.probe()
            except Exception:
                _log.warning("Output session: confirmation probe failed for %r",
                             _title(d.track), exc_info=True)
                reachable, transport_state = False, None
            if self._current is not d or d.confirmed:
                return  # superseded/confirmed while the probe was in flight
            if reachable and (transport_state or "").upper() in PRE_PLAYBACK_STATES:
                d.extended = True
                _log.info(
                    "Output session: extending confirmation deadline once for "
                    "%r (+%.1fs) — device reachable, transport pre-playback (%s)",
                    _title(d.track), self._extension_s, transport_state,
                )
                d.timer = self._timer_factory(
                    self._extension_s, lambda: self._deadline_fired(token))
                return
        self._emit_outage_suspected(d, reason="confirm_timeout")

    def _emit_outage_suspected(self, d: _Dispatch, *, reason: str) -> None:
        # Retire the dispatch first: a late confirmation must not count a play
        # the deadline already classified as unheard. The retired slot keeps
        # the count-state readable for the classifier (dispatch_play_recorded).
        self._retired = d
        self._current = None
        if d.confirmed:
            _log.warning("Output session: outage suspected (%s) for %r",
                         reason, _title(d.track))
        else:
            _log.warning(
                "Output session: outage suspected (%s) — no confirmed start "
                "for %r; play not counted", reason, _title(d.track),
            )
        for cb in list(self._outage_listeners):
            try:
                cb(d.token, d.track, reason)
            except Exception:
                _log.warning("Output session: outage listener raised",
                             exc_info=True)

    # ── reconnect loop + auto-resume (U3: R6–R9, R17, R19, R21) ───────────────

    def begin_outage(self, reason: str, *, backend: Any,
                     was_paused: bool, position_ms: int,
                     held_track_id: str = "") -> None:
        """Open the reconnect context for a fresh outage hold (called by
        ``enter_output_hold``).

        Captures everything the resume needs at ENTRY time: the failed
        backend/device, the last-known position (R7), ``_advance_gen`` (a
        skip bumps it and the resume re-targets the new queue front at 0:00),
        the held track's id (a queue remove/promote during the hold changes
        the front — the resume must not seek the OLD position into it),
        ``was_paused`` (R17), and the entry clock for the per-outage resume
        window (R8). Then arms the backoff retry loop and the watcher's
        discovery-arrival fast path."""
        from app import state
        self.retire_outage()  # a stale context must never leak its timer here
        ot = _Outage(reason)
        ot.backend = backend
        ot.backend_type = state._backend_type_of(backend) or ""
        ot.device_id = str(getattr(backend, "_device_id", None) or "")
        ot.was_paused = bool(was_paused)
        ot.held_position_ms = max(0, int(position_ms or 0))
        ot.held_gen = state.advance_gen()
        ot.held_track_id = held_track_id
        ot.entered_at = self._clock()
        self._outage = ot
        self.session_state = STATE_OUTAGE_PAUSED
        self.was_paused = ot.was_paused
        # Flap guard (R19): an outage re-entered shortly after an auto-resume
        # marks that resume short-lived; N of those inside the window trips
        # the guard and this outage re-attaches for MANUAL resume only.
        now = ot.entered_at
        if (self._last_auto_resume_at is not None
                and now - self._last_auto_resume_at <= FLAP_SHORT_LIVED_S):
            self._flap_stamps = [t for t in self._flap_stamps
                                 if now - t <= FLAP_GUARD_WINDOW_S]
            self._flap_stamps.append(now)
            if len(self._flap_stamps) >= FLAP_GUARD_N:
                ot.flap_tripped = True
                _log.warning(
                    "Output session: flap guard tripped (%d short-lived "
                    "auto-resumes within %.0f min) — next re-attach holds "
                    "for manual resume", len(self._flap_stamps),
                    FLAP_GUARD_WINDOW_S / 60,
                )
        if ot.backend is None or not ot.device_id:
            _log.info("Output session: outage has no addressable device — "
                      "manual resume or a device switch recovers")
            return
        self._watch_arrival(ot)
        self._arm_retry(ot, ot.delay_s)

    def retire_outage(self) -> None:
        """Cancel the reconnect machinery for the current outage — pending
        backoff timer, discovery-arrival listener, single-flight context.
        Idempotent. Called by ``clear_output_hold`` (any hold exit) and by
        ``on_manual_switch`` (R17: no late re-attach of the old device)."""
        ot, self._outage = self._outage, None
        if ot is None:
            return
        ot.retired = True
        ot.cancel_timer()
        self._unwatch_arrival(ot)
        self.idle_paused_reason = ""

    def on_manual_switch(self) -> None:
        """R17 switch-as-resume, the cancellation half: bump the attach-epoch
        so an in-flight executor attach discards its result, and retire the
        old device's retry loop atomically. The HOLD is not cleared here —
        ``state.activate_backend`` clears it only after the new device
        attached, so a failed switch leaves the queue protected."""
        self.attach_epoch += 1
        self.retire_outage()

    def peek_outage(self) -> _Outage | None:
        """The live outage context, or None. Read-only: ``activate_backend``
        snapshots it BEFORE ``notify_manual_switch`` retires it, so a FAILED
        switch can hand it back to ``reopen_outage``."""
        return self._outage

    def reopen_outage(self, prev: _Outage) -> None:
        """Re-open the reconnect loop after a FAILED manual switch during a
        hold: ``notify_manual_switch`` retired the outage context up front,
        and a switch whose ``set_device`` then failed left the hold active
        with NO retry loop — auto-reconnect would be dead for good. Builds a
        FRESH context from the retired one, keeping the original
        ``entered_at`` (the resume window keeps counting from the original
        failure — per-outage-entry, R8), and re-arms the retry loop and the
        arrival watch (fresh backoff from the start delay)."""
        if not hold.output_hold_active():
            return
        self.retire_outage()  # never leak a live context's timer
        ot = _Outage(prev.reason)
        ot.backend = prev.backend
        ot.backend_type = prev.backend_type
        ot.device_id = prev.device_id
        ot.was_paused = prev.was_paused
        ot.held_position_ms = prev.held_position_ms
        ot.held_gen = prev.held_gen
        ot.held_track_id = prev.held_track_id
        ot.pending_volume = prev.pending_volume
        ot.entered_at = prev.entered_at
        self._outage = ot
        self.session_state = STATE_OUTAGE_PAUSED
        self.was_paused = ot.was_paused
        _log.warning("Output session: device switch failed while held — "
                     "re-opening the reconnect loop for %r", ot.device_id)
        if ot.backend is None or not ot.device_id:
            return
        self._watch_arrival(ot)
        self._arm_retry(ot, ot.delay_s)
        session_events._schedule_emit()

    def set_held_paused_intent(self) -> None:
        """A pause pressed during a hold is an INTENT, not a device write
        (the output is gone — a live write would raise): record it so the
        eventual re-attach lands PAUSED and silent instead of auto-playing
        (R17's was_paused edge)."""
        self.was_paused = True
        ot = self._outage
        if ot is not None:
            ot.was_paused = True

    def outage_info(self) -> dict | None:
        """Live reconnect snapshot for observability (U4's event/GET payloads
        source from this). None when no outage context exists."""
        ot = self._outage
        if ot is None:
            return None
        return {
            "reason": ot.reason,
            "backend_type": ot.backend_type,
            "device_id": ot.device_id,
            "entered_at": ot.entered_at,
            "attempts": ot.attempts,
            "next_delay_s": ot.delay_s,
            "attached": ot.attached,
            "was_paused": ot.was_paused,
            "flap_tripped": ot.flap_tripped,
            "held_position_ms": ot.held_position_ms,
        }

    async def manual_resume(self) -> bool:
        """R17 manual resume (the Play press while held): works in
        OutagePaused (attach now, then play), Paused-after-reattach and
        IdlePaused (window expired / flap guard) — plays from the held
        position, bypassing the auto-resume gates. Returns True when the
        press RESOLVED the hold (audio dispatched, or the hold released into
        idle/advance); False when the device is still unreachable or a
        concurrent attempt holds the single-flight slot — the endpoint reads
        the outage context to tell those apart."""
        if not hold.output_hold_active():
            return False
        ot = self._outage
        if ot is None:
            # Hold without a reconnect context (no addressable device at
            # outage entry): release the hold and let the advance authority
            # dispatch the held front — its play_recorded mark still guards
            # the count (R19).
            from app import state
            hold.clear_output_hold()
            self.session_state = STATE_IDLE
            await state._do_advance()
            return True
        return await self._attempt_reattach(ot, "manual", manual=True)

    # ── retry loop plumbing ────────────────────────────────────────────────

    def _arm_retry(self, ot: _Outage, delay_s: float) -> None:
        """Arm the next backoff tick (5s → exponential → ~300s cap). Timers go
        through the injectable factory so tests drive the schedule with fake
        timers — never real sleeps (repo pytest-hang policy)."""
        if ot.retired:
            return
        ot.cancel_timer()

        def _fire() -> None:
            ot.timer = None
            if ot.retired or not hold.output_hold_active():
                return
            _spawn_supervised(self._attempt_reattach(ot, "backoff"))

        ot.timer = self._timer_factory(delay_s, _fire)

    def _watch_arrival(self, ot: _Outage) -> None:
        """Discovery-arrival fast path: the watcher invokes a per-device
        callback after feeding the backend cache (register_resolved), and the
        callback short-circuits whatever backoff wait is pending. hasattr-
        guarded like the watcher→backend chain — an absent watcher just means
        backoff-only reconnects."""
        if ot.arrival_cb is not None or not ot.backend_type or not ot.device_id:
            return
        from app.output import watcher as watcher_mod
        w = watcher_mod.get_watcher()
        if w is None or not hasattr(w, "add_arrival_listener"):
            return

        def _cb() -> None:
            if ot.retired or not hold.output_hold_active():
                return
            _spawn_supervised(self._attempt_reattach(ot, "discovery"))

        try:
            w.add_arrival_listener(ot.backend_type, ot.device_id, _cb)
        except Exception:
            _log.warning("Output session: arrival-listener registration "
                         "failed", exc_info=True)
            return
        ot.arrival_cb = _cb

    def _unwatch_arrival(self, ot: _Outage) -> None:
        cb, ot.arrival_cb = ot.arrival_cb, None
        if cb is None:
            return
        try:
            from app.output import watcher as watcher_mod
            w = watcher_mod.get_watcher()
            if w is not None and hasattr(w, "remove_arrival_listener"):
                w.remove_arrival_listener(ot.backend_type, ot.device_id, cb)
        except Exception:
            _log.debug("Output session: arrival-listener removal failed",
                       exc_info=True)

    async def _attempt_reattach(self, ot: _Outage, trigger: str,
                                *, manual: bool = False) -> bool:
        """The SINGLE re-attach entry point (single-flight per outage): every
        trigger — backoff tick, Cast ConnectionStatusListener CONNECTED,
        discovery arrival, manual resume — funnels through here; while one
        attempt runs, concurrent triggers no-op (the loser returns False).

        The attach-epoch is captured before the (uncancellable) executor-bound
        attach and re-validated after every await before anything commits — a
        manual switch mid-attach makes the result stale, and a stale attach
        must write no caches, register nothing and resume nothing. Returns
        True only when playback audio was dispatched."""
        if ot.retired or self._outage is not ot or not hold.output_hold_active():
            return False
        if ot.attached:
            # Already attached (IdlePaused / Paused landing): only a manual
            # resume moves forward from here. Volume accepted since the
            # landing applies now — this path never revisits
            # _complete_reattach.
            if manual:
                await self._apply_pending_volume(ot)
                return await self._resume_from_hold(ot, manual=True)
            return False
        if ot.attempt_inflight:
            _log.debug("Output session: re-attach trigger %r lost the "
                       "single-flight race", trigger)
            return False
        ot.attempt_inflight = True
        ot.cancel_timer()  # discovery/manual short-circuit the backoff wait
        ot.attempts += 1
        self.session_state = STATE_RECONNECTING
        # U4 (R20): one event per attempt START — never per timer tick — so
        # the admin banner shows live attempt progress.
        await session_events.emit_session_event()
        epoch = self.attach_epoch
        try:
            ok = await self._try_attach(ot, trigger, epoch)
            if ot.retired or self._outage is not ot or epoch != self.attach_epoch:
                _log.info("Output session: discarding stale attach result for "
                          "%r (superseded by a manual switch)", ot.device_id)
                return False
            if ok:
                # DHCP safety: the cached address answered — but is it still
                # the same device? A mismatch keeps retrying.
                try:
                    identity_ok = await self._identity_check(
                        ot.backend, ot.backend_type, ot.device_id)
                except Exception:
                    _log.warning("Output session: identity check raised",
                                 exc_info=True)
                    identity_ok = False
                if (ot.retired or self._outage is not ot
                        or epoch != self.attach_epoch):
                    return False
                if not identity_ok:
                    _log.warning(
                        "Output session: device at the cached address is not "
                        "%r (DHCP reuse?) — retrying", ot.device_id)
                    ok = False
            if not ok:
                self.session_state = STATE_OUTAGE_PAUSED
                ot.delay_s = min(ot.delay_s * RETRY_BACKOFF_FACTOR,
                                 RETRY_BACKOFF_CAP_S)
                self._arm_retry(ot, ot.delay_s)
                # U4 (R20): the attempt landed back in outage_paused — the
                # event carries the bumped attempt count + next backoff delay.
                await session_events.emit_session_event()
                return False
            ot.attached = True
            self._write_back_address(ot)
            return await self._complete_reattach(ot, manual=manual)
        finally:
            ot.attempt_inflight = False

    async def _try_attach(self, ot: _Outage, trigger: str, epoch: int) -> bool:
        """One attach attempt, per-backend mechanics (never raises):

        - Chromecast: while the socket client object is alive it owns
          reconnection (5s auto-retry + the CONNECTED listener) — a backoff
          tick only probes, and a probe reading connected IS the attach. The
          ``set_device`` rebuild is reserved for a dead socket client or a
          discovery/manual trigger (fresh address in the cache; set_device
          explicitly disconnects the superseded cast object).
        - Direct: local — the resume dispatch itself is the attach.
        - DLNA / AirPlay / anything else: the ``_startup_reconnect`` mechanic —
          seed the backend cache from ``output_addr:{device_id}`` then
          ``set_device()``.
        """
        backend = ot.backend
        if backend is None or not ot.device_id:
            return False
        if ot.backend_type == "chromecast":
            probe = getattr(backend, "probe_liveness", None)
            if callable(probe):
                try:
                    reachable, _transport = await probe()
                except Exception:
                    reachable = False
                if reachable:
                    return True
            if getattr(backend, "_cast", None) is not None and trigger == "backoff":
                return False  # live socket client owns re-attach; probe only
            return await self._seed_and_set_device(ot, epoch)
        if ot.backend_type == "direct":
            return True
        return await self._seed_and_set_device(ot, epoch)

    async def _seed_and_set_device(self, ot: _Outage, epoch: int) -> bool:
        """The generalized ``_startup_reconnect`` mechanic: seed the backend's
        address cache from the persisted ``output_addr:{device_id}`` setting
        (setdefault — a fresher discovery-registered address always wins),
        then ``set_device()``. No discovery fall-through: live discovery is
        continuous via the watcher, which triggers its own attempt.

        Runs under ``_attach_serial`` (shared with ``activate_backend``'s
        manual-switch set_device), re-validating ``epoch`` — the attempt's
        capture — after acquiring: an attach queued behind a manual switch
        must abort WITHOUT calling set_device, or its executor connect could
        finish last and overwrite the new device's backend internals."""
        from app import database
        async with _attach_serial:
            if epoch != self.attach_epoch or ot.retired:
                _log.info("Output session: attach for %r superseded by a "
                          "manual switch — not attaching", ot.device_id)
                return False
            try:
                addr_raw = await database.get_setting(
                    f"output_addr:{ot.device_id}")
            except Exception:
                addr_raw = None
            if addr_raw:
                try:
                    _seed_backend_cache(ot.backend, ot.backend_type,
                                        ot.device_id, json.loads(addr_raw))
                except Exception:
                    _log.debug("Output session: address-cache seed failed",
                               exc_info=True)
            try:
                await ot.backend.set_device(ot.device_id)
                return True
            except Exception as exc:
                _log.info("Output session: re-attach attempt %d for %r "
                          "failed: %s", ot.attempts, ot.device_id, exc)
                return False

    def _write_back_address(self, ot: _Outage) -> None:
        """Registry/cache convergence on success (KTD): a set_device that
        resolved a live address writes it back through ``register_resolved``
        so the watcher registry and backend caches agree — never a
        supervisor-owned address store. hasattr-guarded; only backends that
        expose both the resolved-address fields and the hook participate."""
        backend = ot.backend
        host = getattr(backend, "_resolved_host", None)
        port = getattr(backend, "_resolved_port", None)
        register = getattr(backend, "register_resolved", None)
        if not host or port is None or not callable(register):
            return
        name = getattr(backend, "_resolved_name", None) or ot.device_id
        uuid = ot.device_id if _looks_like_uuid(ot.device_id) else None
        try:
            register(str(name), str(host), int(port), uuid, {})
        except Exception:
            _log.debug("Output session: register_resolved write-back failed",
                       exc_info=True)

    # ── resume orchestration ───────────────────────────────────────────────

    async def _complete_reattach(self, ot: _Outage, *, manual: bool) -> bool:
        """Device attached — decide what happens next. The retry loop and the
        discovery listener retire here (the device is back); the audio-start
        decision itself (window, dispatch, seek) happens in
        ``_resume_from_hold`` under ``_advance_lock``."""
        from app import state
        ot.cancel_timer()
        self._unwatch_arrival(ot)
        await self._apply_pending_volume(ot)
        if not manual:
            if ot.was_paused:
                # R17: a session deliberately paused before the outage
                # re-attaches PAUSED and silent — never auto-plays; the held
                # position applies at the eventual manual resume.
                self.session_state = STATE_PAUSED
                _log.info("Output session: %r re-attached paused (was_paused)"
                          " — waiting for manual resume", ot.device_id)
                await session_events.emit_session_event()  # U4: paused landing observable
                return False
            if ot.flap_tripped:
                self._land_idle_paused(ot, "flap_guard")
                return False
            if state._closing_active:
                # R21: Closing Time integrity — no auto-resume while active.
                self._land_idle_paused(ot, "closing_time")
                return False
        return await self._resume_from_hold(ot, manual=manual)

    async def _apply_pending_volume(self, ot: _Outage) -> None:
        """R17: volume accepted during the hold is applied at re-attach,
        BEFORE any audio starts. Cleared once applied so a later landing on
        the same outage doesn't re-write it; a failed write keeps it pending
        for the next landing."""
        if ot.pending_volume is None:
            return
        try:
            await ot.backend.set_volume(ot.pending_volume)
        except Exception:
            _log.warning("Output session: held volume apply failed",
                         exc_info=True)
        else:
            ot.pending_volume = None

    def _land_idle_paused(self, ot: _Outage, reason: str) -> None:
        """Attached but not auto-playing: the IdlePaused landing (window
        expired, flap guard, Closing Time, identity drift). The hold stays —
        the queue remains protected — and a manual resume recovers, playing
        from the held position."""
        self.session_state = STATE_IDLE_PAUSED
        self.idle_paused_reason = reason
        _log.warning("Output session: %r re-attached but NOT auto-resuming "
                     "(%s) — manual resume recovers", ot.device_id, reason)
        # U4 (R20): the idle_paused landing carries its reason to the admin
        # banner (sync context — the task reads the settled state).
        session_events._schedule_emit()

    async def _resume_from_hold(self, ot: _Outage, *, manual: bool) -> bool:
        """Dispatch the held queue front and seek to the held position.

        Runs under ``state._advance_lock`` — the resume-window check (R8)
        happens HERE, at the moment audio would start, never on a separate
        expiry timer, so a window expiry racing an in-flight attempt has a
        single authority. An ``_advance_gen`` bump since outage entry (a
        skip) re-targets the resume at the CURRENT queue front from 0:00.
        Manual resumes bypass the window/flap/closing gates (AE3: expiry only
        blocks the AUTO path). Returns True when the resume RESOLVED the
        hold — audio dispatched, or the hold released into idle/advance
        (queue-cleared and track-level-skip landings); False when the device
        is still the blocker."""
        from app import state
        from app.output.base import DeviceNotReadyError
        played = False
        skipped_track: Any = None
        seek_backend: Any = None
        async with state.advance_lock():
            if ot.retired or self._outage is not ot or not hold.output_hold_active():
                return False
            if not manual:
                try:
                    window_min = int(await self._window_minutes())
                except Exception:
                    _log.warning("Output session: resume-window read failed",
                                 exc_info=True)
                    window_min = 60
                if ot.retired or self._outage is not ot:
                    return False
                if self._clock() - ot.entered_at > window_min * 60.0:
                    # R8/AE3: window expired — device stays attached, silent.
                    self._land_idle_paused(ot, "window_expired")
                    return False
            position_ms = ot.held_position_ms
            if state.advance_gen() != ot.held_gen:
                # A skip during the outage moved the held pointer — the
                # captured position belongs to a superseded track (R17).
                position_ms = 0
            queue = state.queue_engine.queue
            if not queue:
                # Queue cleared during the outage — nothing to resume. The
                # hold is RESOLVED (released, idle): report success so a
                # manual press doesn't 409 as "unreachable".
                _log.info("Output session: queue was cleared during the "
                          "outage — nothing to resume; hold released, "
                          "landing idle")
                hold.clear_output_hold()
                self.session_state = STATE_IDLE
                return True
            if not manual:
                # Rescan mid-outage can re-mint catalog ids (durable-derived-
                # mappings doc): don't blind-fire a stale identity. Manual
                # resume skips this — holder fallback self-corrects there.
                try:
                    valid = await self._held_track_check(queue[0].track)
                except Exception:
                    valid = True
                if ot.retired or self._outage is not ot:
                    return False
                if not valid:
                    self._land_idle_paused(ot, "track_identity")
                    return False
            client = await state.get_plex_client()
            if ot.retired or self._outage is not ot:
                return False
            if client is None:
                self._land_idle_paused(ot, "no_media_source")
                return False
            next_item = await state.queue_engine.advance()
            if next_item is None:
                _log.info("Output session: advance produced no item to "
                          "resume — nothing to play; hold released, "
                          "landing idle")
                hold.clear_output_hold()
                self.session_state = STATE_IDLE
                return True
            if ot.held_track_id and next_item.track.id != ot.held_track_id:
                # The held front changed under the hold (queue remove /
                # promote / receipt-undo) — the captured position belongs to
                # the displaced track; play the new front from the top.
                position_ms = 0
            if not manual:
                # Flap-guard bookkeeping: this is the moment an auto-resume
                # happened; a quick outage re-entry marks it short-lived.
                self._last_auto_resume_at = self._clock()
            # U10 (R7): a flow-capable backend takes the held position as a
            # SERVER-side start offset — play() feeds it to
            # ``create_flow_session(start_offset_ms=…)`` instead of a device
            # seek (its ``resume_seek`` no-ops in flow mode; position-resume
            # is fully server-controlled). Primed with the ADJUSTED position
            # (gen-bump / front-change zeroing above); the per-track path
            # consumes and ignores it, keeping the seek below.
            prime = getattr(ot.backend, "prime_resume_offset", None)
            if callable(prime):
                try:
                    prime(position_ms)
                except Exception:
                    _log.debug("Output session: resume-offset prime failed",
                               exc_info=True)
            try:
                played = await state._play_with_fallback(next_item, client)
            except DeviceNotReadyError:
                # The device died again between attach and audio: re-hold the
                # item (its R19 mark is intact — the dispatch never
                # succeeded) and hand the outage back to the retry loop. Same
                # outage entry — the resume window keeps counting from the
                # original failure (per-outage-entry, R8).
                _log.warning("Output session: resume dispatch failed device-"
                             "level — re-holding %r", _title(next_item.track))
                await state.queue_engine.hold_current(
                    play_recorded=bool(getattr(next_item, "play_recorded",
                                               False)))
                ot.attached = False
                self.session_state = STATE_OUTAGE_PAUSED
                ot.delay_s = min(ot.delay_s * RETRY_BACKOFF_FACTOR,
                                 RETRY_BACKOFF_CAP_S)
                self._watch_arrival(ot)
                self._arm_retry(ot, ot.delay_s)
                # U4: back to outage_paused — scheduled (lock held here; the
                # _land_idle_paused / clear_output_hold pattern).
                session_events._schedule_emit()
                return False
            if not played:
                # Track-level: every holder failed on a live device — today's
                # skip path (R15's other half); advance owns what plays next,
                # AFTER the lock drops.
                hold.clear_output_hold()
                skipped_track = next_item.track
            else:
                hold.clear_output_hold()
                self.session_state = STATE_PLAYING
                self.was_paused = False
                if position_ms > 0:
                    # Seek AFTER the lock drops: Direct blocks up to 10s in
                    # an executor and Cast is an unbounded network roundtrip
                    # — neither may stall every _advance_lock waiter.
                    seek_backend = ot.backend
        if skipped_track is not None:
            # The hold RESOLVED into today's skip behavior — success, not
            # "device unreachable".
            await state._emit_track_skipped(skipped_track)
            await state._do_advance()
            return True
        if seek_backend is not None:
            await self._resume_seek(seek_backend, position_ms)
        if played:
            _log.info(
                "Output session: resumed %r on %r at %dms (%s)",
                _title(next_item.track), ot.device_id, position_ms,
                "manual" if manual else "auto",
            )
        return played

    async def _resume_seek(self, backend: Any, position_ms: int) -> None:
        """Per-backend position-resume (R7): Cast seek after the re-LOAD
        (re-arms the watchdog; ``resumeState: PLAYBACK_START`` is fine here —
        this session is meant to play), DLNA direct-``_action`` Seek REL_TIME
        (its seek() already bypasses the guarded helper), AirPlay
        ``start_offset_ms`` respawn (its seek() IS the respawn), Direct
        preroll-then-seek via ``resume_seek()``. A backend exposing neither
        hook has no reliable seek → restart-from-top (AE4)."""
        fn = getattr(backend, "resume_seek", None)
        if not callable(fn):
            fn = getattr(backend, "seek", None)
        if not callable(fn):
            _log.info("Output session: backend has no seek support — "
                      "resuming from the top (AE4)")
            return
        try:
            await fn(position_ms)
        except Exception:
            _log.warning("Output session: resume seek to %dms failed — "
                         "playing from the top", position_ms, exc_info=True)


def _title(track: Any) -> str:
    return getattr(track, "title", None) or getattr(track, "id", "") or "?"


# ── U3 module surface: reconnect triggers, held volume, default hooks ─────────

def notify_manual_switch() -> None:
    """``state.activate_backend`` calls this FIRST on every manual device or
    backend switch (R17): bump the attach-epoch so an in-flight executor
    attach discards its result, and retire the old device's retry loop
    atomically — no late re-attach. The hold itself is cleared by
    activate_backend only after the new device attached."""
    get_supervisor().on_manual_switch()


def notify_reconnect_trigger(trigger: str) -> None:
    """Loop-side re-attach trigger for backend connection listeners (U3): the
    Cast ConnectionStatusListener's CONNECTED lands here (LOST→CONNECTED
    destroyed the media session, so the re-attach rebuilds + resumes).
    Funnels into the single-flight entry; no-op without an active outage."""
    sup = get_supervisor()
    ot = sup._outage
    if ot is None or not hold.output_hold_active():
        return
    _spawn_supervised(sup._attempt_reattach(ot, trigger))


async def hold_foreign_controller() -> None:
    """Foreign-controller yield (2026-08-04-002 plexplayer plan U7): another
    Plex controller took the device's play queue, so jukeplox stops
    dispatching until the admin re-activates.

    Mechanically this is the standard outage hold (queue frozen, the
    current item re-front-inserted with its R19 mark — a resume never
    re-counts a confirmed play) — but the device is REACHABLE, so the
    reconnect machinery must NOT run: an auto re-attach would re-dispatch
    and fight the other controller for the queue. The retry timer and
    arrival watch retire immediately, the outage context is marked
    ``attached`` (a manual resume goes straight to the dispatch, which IS
    the re-activate), and the session lands IDLE_PAUSED with reason
    ``foreign_controller`` — the reason rides ``idle_paused_reason`` into
    the admin ``output_session`` payload, where
    ``renderOutputSessionBanner`` (static/admin/app.js) shows the dedicated
    banner copy. MUST run on the event loop (backend poll tasks do)."""
    sup = get_supervisor()
    token = sup.current_token()
    play_recorded = (sup.dispatch_play_recorded(token)
                     if token is not None else None)
    await hold.enter_output_hold("foreign_controller",
                                 play_recorded=play_recorded)
    if not hold.output_hold_active():
        return  # hold entry did not take — nothing to land
    ot = sup._outage
    if ot is not None:
        ot.cancel_timer()
        sup._unwatch_arrival(ot)
        ot.attached = True  # reachable device: manual resume dispatches directly
    sup.session_state = STATE_IDLE_PAUSED
    sup.idle_paused_reason = "foreign_controller"
    await session_events.emit_session_event()


async def set_held_volume(level: float) -> None:
    """R17: a volume change during a hold is accepted + persisted + applied at
    re-attach before audio — never a live device write to a dead output (the
    write would raise and 500 the endpoint)."""
    from app import state
    level = max(0.0, min(1.0, float(level)))
    sup = get_supervisor()
    ot = sup._outage
    if ot is not None:
        ot.pending_volume = level
    backend = state.output_router.active
    if backend is None:
        return
    try:
        backend._volume = level  # in-memory level for get_volume reads
    except Exception:
        pass
    backend_type = state._backend_type_of(backend) or (
        ot.backend_type if ot is not None else "")
    device_id = getattr(backend, "_device_id", None)
    if backend_type and device_id:
        from app import database
        try:
            await database.set_setting(f"vol:{backend_type}:{device_id}",
                                       str(level))
        except Exception:
            _log.warning("Output session: held volume persist failed",
                         exc_info=True)


def _seed_backend_cache(backend: Any, backend_type: str, device_id: str,
                        addr: dict) -> None:
    """The ``_startup_reconnect`` seeding mechanic, per backend cache. Uses
    ``setdefault`` so a FRESH address a discovery arrival just registered is
    never clobbered by the stale persisted one."""
    name = addr.get("name", device_id)
    if backend_type == "chromecast" and hasattr(backend, "_dbus_index"):
        backend._dbus_index.setdefault(
            device_id, (name, addr["host"], int(addr["port"])))
    elif backend_type == "airplay" and hasattr(backend, "_device_addr"):
        # Empty TXT on the cached path — same posture as _startup_reconnect
        # (a stale pairing surfaces a re-pair event instead of failing mute).
        backend._device_addr.setdefault(
            device_id, (name, addr["host"], int(addr["port"]), {}))
    elif backend_type == "dlna" and hasattr(backend, "_device_locations"):
        if addr.get("location"):
            backend._device_locations.setdefault(device_id, addr["location"])


async def _default_window_minutes() -> int:
    from app import database
    return await database.get_resume_window_minutes()


async def _default_identity_check(backend: Any, backend_type: str,
                                  device_id: str) -> bool:
    """DHCP-reuse guard (plan risk table): after connecting via a cached
    address, confirm the endpoint is the SAME device before resuming; a
    mismatch keeps the retry loop going. Evidence-based and fail-open — a
    backend that exposes no identity yields True (absence of evidence is not
    a mismatch; blocking on an unverifiable identity would strand recovery
    on backends that simply can't answer, e.g. AirPlay's cliap2)."""
    try:
        if backend_type == "chromecast":
            cast = getattr(backend, "_cast", None)
            uuid = str(getattr(cast, "uuid", "") or "")
            if uuid and _looks_like_uuid(device_id):
                return uuid == device_id
            return True
        if backend_type == "dlna":
            dmr = getattr(backend, "_dmr", None)
            udn = str(getattr(getattr(dmr, "device", None), "udn", "") or "")
            # DLNA device ids are USNs ("uuid:<device>::urn:...") when SSDP
            # supplied one; only that shape is verifiable against the UDN.
            if udn and device_id.startswith("uuid:"):
                return device_id.startswith(udn)
            return True
        return True
    except Exception:
        return True


async def _default_held_track_check(track: Any) -> bool:
    """Resume identity validation (durable-derived-mappings doc): a rescan
    mid-outage can re-mint catalog ids. With a catalog floor populated, the
    held id must still resolve to a live identity; an empty catalog (native
    Plex install pre-scan) offers no evidence and passes. Never raises —
    validation failure must not strand the resume."""
    try:
        from app.catalog import identity as cat_identity
        from app.catalog import store
        if await store.is_empty():
            return True
        return (await cat_identity.identity_for_track_id(track.id)) is not None
    except Exception:
        return True


# ── failure classifier (U2, R15/R16) ──────────────────────────────────────────

def classify_outage(token: int, track: Any, reason: str) -> None:
    """The production outage listener: two-class failure classification.

    Sync (listener signature) — hops into a task because the tie-breaker
    probe is async. Registered on the module singleton by ``get_supervisor``;
    tests attach it to their own supervisors explicitly."""
    _spawn_supervised(_classify_outage(token, track, reason))


async def _classify_outage(token: int, track: Any, reason: str) -> None:
    from app import state
    if hold.output_hold_active():
        return  # already holding — repeated signals are the same outage
    if get_supervisor().current_token() is not None:
        # Every emission retires its dispatch before listeners fire, so a
        # live dispatch here is NEWER than the one this classification
        # speaks for: playback already moved on (e.g. a backend's own
        # advance raced the confirmation deadline). Acting now — especially
        # the track-level advance — would consume a healthy item (R15); the
        # live dispatch's own confirm/deadline owns the decision.
        _log.info("Output session: dropping stale outage classification "
                  "(%s) — a newer dispatch is live", reason)
        return
    if reason in DEVICE_LEVEL_REASONS:
        await hold.enter_output_hold(
            reason, play_recorded=get_supervisor().dispatch_play_recorded(token))
        return
    if reason in FLOW_RECOVERABLE_REASONS:
        # A reachable flow receiver hiccup is usually a transient (hardware-
        # evidenced), not a bad track — recover it like connection_lost (hold +
        # auto-resume at position), NOT a track-level skip. BUT a track that
        # keeps failing at the same position (receiver-rejected content, wedged
        # media pipeline) must not recover-loop forever — the progress-aware
        # cap falls back to the old skip after FLOW_RECOVER_STUCK_LIMIT
        # no-progress recoveries. (The R19 flap guard alone does NOT bound this
        # — it only counts sub-FLAP_SHORT_LIVED_S re-fails; see the cap comment.)
        pos_ms = await hold._capture_position_ms(state.output_router.active)
        track_id = getattr(track, "id", None)
        if get_supervisor().flow_recovery_allowed(track_id, pos_ms):
            await hold.enter_output_hold(
                reason,
                play_recorded=get_supervisor().dispatch_play_recorded(token))
            return
        _log.warning(
            "Output session: %s recovered %d× for %r without progress "
            "(~%dms) — treating the track as the failure and skipping",
            reason, FLOW_RECOVER_STUCK_LIMIT,
            _title(track) if track is not None else "?", pos_ms,
        )
        await _track_level_skip(track, reason)
        return
    # Ambiguous reason → reachability probe is the R15 tie-breaker. A probe
    # that raises counts as unreachable (matches the U1 deadline-extension
    # posture): after a reported outage signal, fail toward protecting the
    # queue rather than consuming it.
    probe = state._output_probe()
    if probe is not None:
        try:
            reachable, _transport = await probe()
        except Exception:
            _log.warning("Output session: classification probe failed",
                         exc_info=True)
            reachable = False
        if hold.output_hold_active():
            return  # a device-level report landed while the probe ran
        if get_supervisor().current_token() is not None:
            _log.info("Output session: dropping stale outage classification "
                      "(%s) — a newer dispatch landed during the probe", reason)
            return
        if not reachable:
            await hold.enter_output_hold(
                reason,
                play_recorded=get_supervisor().dispatch_play_recorded(token))
            return
    else:
        _log.warning(
            "Output session: no reachability probe for the active backend — "
            "classifying %s as track-level (today's skip behavior)", reason,
        )
    await _track_level_skip(track, reason)


async def _track_level_skip(track: Any, reason: str) -> None:
    """Track-level handoff: the device is demonstrably reachable, so the
    dispatched track is the failure — keep today's skip behavior (R15's
    other half: one unplayable track must not freeze playback)."""
    from app import state
    _log.warning(
        "Output session: %s classified track-level for %r (device reachable) "
        "— skipping", reason, _title(track) if track is not None else "?",
    )
    if track is not None:
        await state._emit_track_skipped(track)
    await state._do_advance()


# ── module singleton ──────────────────────────────────────────────────────────

_supervisor: OutputSessionSupervisor | None = None


def get_supervisor() -> OutputSessionSupervisor:
    """The process-wide supervisor, created lazily (mirrors the app-singleton
    convention in app.state; tests install their own via ``_supervisor``).
    Production wiring registers the U2 classifier here; a test-installed
    supervisor attaches it explicitly when classification is under test."""
    global _supervisor
    if _supervisor is None:
        _supervisor = OutputSessionSupervisor()
        _supervisor.add_outage_listener(classify_outage)
    return _supervisor


def notify_confirmed(token: int) -> None:
    """Backend-facing confirmed-start entry. MUST run on the event loop —
    backend threads marshal here via ``call_soon_threadsafe`` (the same hop
    their EOS paths already make)."""
    get_supervisor().on_playback_confirmed(token)


def notify_outage(reason: str) -> None:
    """Backend-facing outage-suspected entry (U2) for the re-pointed
    advance-authority paths. MUST run on the event loop — backend threads
    marshal here via ``call_soon_threadsafe`` exactly like notify_confirmed."""
    get_supervisor().on_outage_reported(reason)


async def notify_gapless_boundary(track: Any) -> None:
    """Backend-facing gapless-boundary entry (U7/U8): the audible transition
    to a consumed armed ``track`` (Direct STREAM_START; DLNA CurrentTrackURI
    boundary). MUST run on the event loop — Direct marshals from its
    GStreamer thread via ``run_coroutine_threadsafe`` (its EOS hop) and
    applies its own play-generation staleness guard BEFORE calling here;
    DLNA's EOS poll already runs on the loop and calls in directly."""
    await get_supervisor().on_gapless_boundary(track)


async def notify_flow_boundary(track: Any) -> int | None:
    """Backend-facing Cast flow-mode boundary entry (U10): the server
    stitcher's encode clock crossed into ``track``. Advances the queue + Now
    Playing NOW (the flow advance authority, R16) and returns the UNCONFIRMED
    dispatch token — the play count fires later, through the U1 chokepoint
    (``notify_confirmed``), when the backend maps the DEVICE-reported position
    across this boundary's stitch offset; never here on the encode clock.
    None = a skip/hold owns the transition (nothing advanced — register no
    pending count). MUST run on the event loop — the flow pump's boundary
    listeners already do (U9 awaits them on its own loop tasks)."""
    return await get_supervisor().on_flow_boundary(track)


def notify_confirmed_threadsafe(loop: Any, token: int) -> None:
    """Thread-side confirmed-start entry: marshal ``notify_confirmed`` onto
    ``loop`` — the shared hop for backends whose confirmation signal fires on
    a foreign thread (Cast status thread, GStreamer GLib bus), exactly like
    their EOS paths. A missing or already-closed loop drops the signal —
    there is nowhere left to deliver it."""
    if loop is None:
        return
    try:
        loop.call_soon_threadsafe(notify_confirmed, token)
    except RuntimeError:
        pass  # asyncio loop already closed — nowhere to deliver


def notify_outage_threadsafe(loop: Any, reason: str) -> None:
    """Thread-side outage-suspected entry: marshal ``notify_outage`` onto
    ``loop`` — the shared hop for backends whose outage signal fires on a
    foreign thread (Cast status thread, GStreamer GLib bus), exactly like
    their EOS paths. A missing or already-closed loop drops the signal —
    there is nowhere left to deliver it."""
    if loop is None:
        return
    try:
        loop.call_soon_threadsafe(notify_outage, reason)
    except RuntimeError:
        pass  # asyncio loop already closed — nowhere to deliver
