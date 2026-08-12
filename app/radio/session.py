"""Radio session + non-destructive queue takeover/resume (radio plan U3).

A :class:`RadioSession` flips the single shared output into "playing a station"
by **explicitly** stopping current playback and holding the track queue, and
restores the queue on stop — WITHOUT mutating queue/history/EOS state beyond the
one ``hold_current`` + the resume dispatch (R8). This is the load-bearing
takeover; the naive "just ``hold_current`` and play" approach does NOT work (see
FE-1/FE-2 below), so this module implements the corrected design exactly.

Why the naive approach fails (the two corrections this class encodes)
--------------------------------------------------------------------

**FE-1 — takeover must suppress auto-start.** ``queue_engine.hold_current()``
re-front-inserts the current item and pauses the queue, but it does NOT set the
outage-hold flag (``app.output.hold._output_hold``). ``state._should_auto_start()``
returns True when ``not is_playing and bool(queue) and not output_hold_active()``
— all true right after a bare hold — and the ``queue_changed`` event that
``hold_current`` emits would immediately re-dispatch the held track to the very
output radio just took over. The takeover would race itself. So a radio session
sets a **radio-scoped suppression**: ``state.radio_active()`` (backed by this
session's :attr:`active` flag) GATES both ``_should_auto_start()`` and
``_do_advance()`` — mirroring exactly how ``output_hold_active()`` gates them —
keeping the paused queue inert for the station's lifetime. We deliberately do
NOT reuse ``_output_hold`` (that flag drives the outage reconnect machinery,
which radio must not arm).

**FE-2 — resume needs a NEW dispatch entry point.** No existing seam resumes a
bare-``hold_current`` paused queue: ``_do_advance()`` early-returns under a hold
and is EOS-driven (nothing fires it for a stopped radio), and
``session.manual_resume()`` returns False unless ``output_hold_active()``. So
stop/exit clears ``radio_active`` and then explicitly pops-and-plays the held
front at 0:00 through ``state._play_with_fallback`` (ADV-2) — so a held source
that died during a long radio session degrades to skip / outage-hold instead of
raising. Resume is **actor-independent** (any authorized stop triggers it).

Guarantees (R8)
---------------
- The queue is held exactly ONCE, at first takeover. An instant switch (R5)
  stops the current station stream and starts the new one WITHOUT re-holding.
- Radio never writes history for stations and never touches the held item's
  ``play_recorded`` mark (the resume honors it via ``_play_with_fallback`` so
  an already-counted play is not double-counted).
- All start/stop/switch mutations serialize under an ``asyncio.Lock`` so racing
  callers (two guests, or guest-STOP vs admin-switch) cannot double-hold or
  double-resume.

Seams for later units
---------------------
- **U4 (per-backend endless play):** :attr:`_play_url` is the "play this URL on
  the active backend as an endless stream" seam. For U3 it dispatches a
  pseudo-``Track`` (``duration_ms=0`` + a sentinel radio attribute) to the
  output router. U4 makes each backend detect that sentinel and suppress its
  advance path; the call here does not change.
- **U5 (Cast/DLNA transcode-proxy URL):** U3 hands the SSRF-validated final
  ``url_resolved`` straight to the backend. U5 will, for Cast/DLNA, substitute a
  capability-URL to ``/api/radio/stream/{session_id}`` — the swap happens inside
  the ``_play_url`` seam / its per-backend routing, not in this state machine.

Live title (radio plan U6)
--------------------------
The session is the single point where a station's live "now playing" title is
held and from which title changes are broadcast. Two sources feed it, per backend:

- **GStreamer-direct (FREE):** the Direct backend reads ``GST_TAG_TITLE`` off the
  playbin bus and calls its title hook; U7 wires that hook to :meth:`set_title`.
- **Cast/DLNA/AirPlay (bounded server-side ICY read):** those backends hand the
  audio to a device / transcode-proxy that does not surface the title, so the
  session runs a bounded, periodic best-effort ICY read against the ORIGINAL
  station URL (:func:`app.radio.icy.read_stream_title`) and feeds the result into
  :meth:`set_title`. The read is off the critical path — a failure just means
  station-name-only (AE5).

SEC-004: the ``StreamTitle`` is untrusted third-party text. It is sanitized to
plain text before it is stored here (the ICY reader sanitizes; the Direct hook
passes an already-sanitized value). :meth:`current_title` returns that plain
string — the WS sink (U7's ``RadioStateEvent.live_title``) carries it as a JSON
string the client MUST render via ``textContent`` (never ``innerHTML``); a
DIDL/Cast device-metadata sink XML-escapes it (see ``app.radio.icy``).

U7 API contract (read the current title + subscribe to changes):
    session.current_title() -> str | None       # snapshot for RadioStateEvent /
                                                 # now-playing hydration
    session.add_title_listener(cb)               # cb(title | None) on every change
    session.remove_title_listener(cb)            # (optional) unsubscribe
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from app.models import Track
from app.radio.client import Station, get_radio_client
from app.radio.icy import read_stream_title, sanitize_title
from app.radio.urlcheck import (
    RadioUrlBlocked,
    resolve_and_validate,
    validate_station_host,
)

_log = logging.getLogger("jukeplox.radio")


def _log_task_exc(task: asyncio.Task) -> None:
    """F10: done-callback logging a background task's exception (WARNING) unless
    cancelled — mirrors app/radio/stream.py + client.py. The title reader is
    fire-and-forget; without this a crash inside it would be silent."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _log.warning("radio: title-reader task raised: %s", exc, exc_info=exc)

# Poll interval for the bounded server-side ICY title read (Cast/DLNA/AirPlay).
# A live title changes at song boundaries (minutes); ~20s is responsive enough
# without hammering the station (and each read is itself bounded + best-effort).
# Named constant per the plan (a periodic best-effort read, not a persistent
# second connection).
RADIO_TITLE_POLL_S = 20.0

# Sentinel attribute stamped on the pseudo-Track handed to the output backend so
# U4 can detect radio mode WITHOUT a play() signature change (SG-03). Paired with
# duration_ms=0. Kept here (not on the Track dataclass) so base.py stays untouched.
RADIO_TRACK_ATTR = "_radio_endless"


def make_radio_track(station: Station, final_url: str) -> Track:
    """Build the pseudo-``Track`` that carries a station to the output backend.

    ``duration_ms=0`` + the :data:`RADIO_TRACK_ATTR` sentinel is how a backend
    tells radio from a finite library track (SG-03) — U4 reads both to select
    its endless/EOS-suppressed path. ``id`` is the ``stationuuid`` so any
    downstream identity check is stable; ``stream_key`` carries the resolved URL
    for backends that inspect it. No album/artist identity — a station has none.
    """
    t = Track(
        id=station.stationuuid or "radio",
        title=station.name or "Radio",
        artist="",
        album="",
        duration_ms=0,
        thumb=station.favicon or None,
        stream_key=final_url,
    )
    setattr(t, RADIO_TRACK_ATTR, True)
    return t


def is_radio_track(track: object) -> bool:
    """True if ``track`` is a radio pseudo-Track (the U4 detection predicate)."""
    return bool(getattr(track, RADIO_TRACK_ATTR, False))


@dataclass
class _Active:
    """The currently-playing station state (present only while active)."""

    station: Station
    final_url: str


# Dependency seams. Defaulting to lazy ``app.state`` bindings keeps the session
# testable with zero DB / real-singleton coupling: a test injects plain
# coroutine mocks and asserts call ordering.
StopOutput = Callable[[], Awaitable[None]]
HoldQueue = Callable[[], Awaitable[object]]
HasQueueCurrent = Callable[[], bool]
PlayUrl = Callable[[Station, str], Awaitable[None]]
ResumeQueue = Callable[[], Awaitable[None]]
# F6: the "tracks were enqueued during radio, start them" seam. Fires the normal
# auto-start path (queue_changed → _trigger_auto_advance) so stop() with nothing
# held but a now-non-empty queue doesn't strand those tracks.
AutostartQueue = Callable[[], Awaitable[None]]
QueueNonEmpty = Callable[[], bool]


class RadioSession:
    """State machine for the non-destructive radio takeover of the shared output.

    States: **idle** (``active`` False, no station) → **playing** (a station is
    on, the queue is held) → **idle** again after stop. A switch stays in
    *playing* (new station, no re-hold).

    Injectable seams (all default to ``app.state`` via lazy import):

    - ``stop_output``   → ``output_router.stop()`` (immediate; NOT ``set_backend``).
    - ``hold_queue``    → ``queue_engine.hold_current()`` (once, at takeover).
    - ``has_queue_current`` → whether a track is currently playing (so an empty
      queue holds nothing).
    - ``play_url``      → the U4/U5 "play this URL on the active backend as
      endless" seam.
    - ``resume_queue``  → the FE-2 resume dispatch (pop-and-play the held front
      via ``_play_with_fallback``).
    """

    def __init__(
        self,
        *,
        stop_output: StopOutput | None = None,
        hold_queue: HoldQueue | None = None,
        has_queue_current: HasQueueCurrent | None = None,
        play_url: PlayUrl | None = None,
        resume_queue: ResumeQueue | None = None,
        autostart_queue: AutostartQueue | None = None,
        queue_non_empty: QueueNonEmpty | None = None,
        report_click: Callable[[str], None] | None = None,
        validate_url: Callable[[str], Awaitable[str]] | None = None,
        teardown_stream: Callable[[], Awaitable[None]] | None = None,
        read_title: Callable[[str], Awaitable[str | None]] | None = None,
    ) -> None:
        self._stop_output = stop_output or _default_stop_output
        self._hold_queue = hold_queue or _default_hold_queue
        self._has_queue_current = has_queue_current or _default_has_queue_current
        self._play_url = play_url or _default_play_url
        self._resume_queue = resume_queue or _default_resume_queue
        self._autostart_queue = autostart_queue or _default_autostart_queue
        self._queue_non_empty = queue_non_empty or _default_queue_non_empty
        self._report_click = report_click or _default_report_click
        self._validate_url = validate_url or resolve_and_validate
        # U5 teardown seam: invalidate the Cast/DLNA transcode-proxy capability id
        # (and reap its ffmpeg) on stop so a stale device can't re-bind onto a new
        # station. No-op when no proxy session exists (GStreamer-direct/AirPlay).
        # A SWITCH does NOT call this — start_radio_stream (minted by the Cast/DLNA
        # backend) supersedes+reaps the prior proxy itself, keeping the id fresh.
        self._teardown_stream = teardown_stream or _default_teardown_stream

        # State machine, guarded by _lock. `_active` is None <=> idle.
        self._active: _Active | None = None
        # Current playback status for U7's RadioStateEvent + now-playing snapshot
        # ``radio`` block. One of "idle" | "connecting" | "playing" | "failed".
        # Idle whenever `_active` is None; a start goes connecting → playing; a
        # backend failed-hook / proxy consumer-gone flips it to "failed" WITHOUT
        # clearing `_active` (the station is still selected, just offline — R12).
        self._status: str = "idle"
        # U7 subscribes here for "state changed" broadcasts (RadioStateEvent on
        # every transition). Same best-effort contract as the title listeners.
        self._state_listeners: list[Callable[[], None]] = []
        # `_held` records whether THIS session performed the one hold_current, so
        # a switch never re-holds and a stop with an empty-queue takeover doesn't
        # try to resume a queue it never held.
        self._held: bool = False
        self._lock = asyncio.Lock()
        # F8: start/stop generation counter. A start captures the gen BEFORE the
        # outside-lock validate; if a stop() bumps it during that window, the
        # start aborts after re-acquiring the lock (the stop must win, else a
        # station plays after an explicit stop). Bumped on every stop().
        self._gen: int = 0

        # ── live title (U6) ──────────────────────────────────────────────────
        # The current sanitized live title (None = station-name-only). Fed by the
        # Direct bus-tag hook (FREE) or the periodic ICY reader (Cast/DLNA/AirPlay).
        self._title: str | None = None
        # U7 subscribes here for "title changed" broadcasts (RadioStateEvent).
        self._title_listeners: list[Callable[[str | None], None]] = []
        # The bounded periodic ICY reader task (Cast/DLNA/AirPlay only) + the URL
        # it reads. Direct never spawns it (GStreamer surfaces the title for free).
        self._title_reader_task: asyncio.Task | None = None
        # Injectable ICY-read seam for tests (defaults to the real bounded reader).
        self._read_title = read_title or _default_read_title

    # ── predicates (read outside the lock — a bare bool read is atomic) ─────────

    def is_active(self) -> bool:
        """True while a station is playing (the queue is held). Backs
        ``state.radio_active()`` which gates auto-start / advance (FE-1)."""
        return self._active is not None

    @property
    def station(self) -> Station | None:
        a = self._active
        return a.station if a is not None else None

    @property
    def final_url(self) -> str | None:
        a = self._active
        return a.final_url if a is not None else None

    def status(self) -> str:
        """Current playback status for U7 (``idle``/``connecting``/``playing``/
        ``failed``). ``idle`` whenever no station is selected."""
        return self._status if self._active is not None else "idle"

    # ── state transitions (U7) — the RadioStateEvent broadcast seam ─────────────

    def add_state_listener(self, cb: Callable[[], None]) -> None:
        """Subscribe to state transitions for U7's ``RadioStateEvent`` broadcast.

        ``cb()`` (no args) fires AFTER each transition (start → connecting →
        playing, stop → idle, failed) so the listener reads the current
        ``station``/``status``/``current_title`` snapshot itself. Best-effort — a
        raising listener is swallowed. Keeping U3's state machine intact: this is
        an additive observer, not a rewrite (the plan's preferred shape)."""
        if cb not in self._state_listeners:
            self._state_listeners.append(cb)

    def remove_state_listener(self, cb: Callable[[], None]) -> None:
        self._state_listeners = [c for c in self._state_listeners if c is not cb]

    def _notify_state(self) -> None:
        """Fire the state listeners (best-effort). Called on every transition."""
        for cb in list(self._state_listeners):
            try:
                cb()
            except Exception:  # pragma: no cover - listeners are best-effort
                _log.debug("radio: state listener raised (ignored)", exc_info=True)

    def mark_failed(self) -> bool:
        """Flip the active station to the ``failed`` (offline) status (R12).

        Called by U7's backend failed-hook / proxy consumer-gone wiring once the
        per-backend reconnect cap is exhausted. Does NOT clear ``_active`` — the
        station stays selected (a stopped-vs-offline distinction the UI needs);
        an explicit stop or a switch is what returns to idle/playing. A no-op when
        idle (the RadioStateEvent must not fire when radio is inactive).

        Returns True iff it actually transitioned (and therefore fired the state
        listeners → a broadcast); False on the idempotent no-op (already failed /
        idle). The caller (F3 ``_on_radio_failed``) uses this to avoid a double
        broadcast on the normal edge while still broadcasting the offline edge when
        mark_failed no-ops (already failed)."""
        if self._active is None or self._status == "failed":
            return False
        self._status = "failed"
        self._notify_state()
        return True

    # ── live title (U6) — the API U7 reads/subscribes to ────────────────────────

    def current_title(self) -> str | None:
        """The current sanitized live title, or ``None`` (station-name-only).

        The U7 snapshot/hydration read: a fresh or WS-gap client converges by
        pulling this into the now-playing ``radio`` block. Plain text (SEC-004) —
        rendered via ``textContent`` on the client, XML-escaped for a DIDL/Cast
        sink."""
        return self._title

    def add_title_listener(self, cb: Callable[[str | None], None]) -> None:
        """Subscribe to "title changed" for U7's ``RadioStateEvent`` broadcast.

        ``cb(title | None)`` fires on every distinct title change (a new
        StreamTitle, or a clear on station switch/stop). The value is the same
        plain-text string :meth:`current_title` returns. Listeners are best-effort
        — a raising listener is swallowed so one bad subscriber can't break the
        title flow."""
        if cb not in self._title_listeners:
            self._title_listeners.append(cb)

    def remove_title_listener(self, cb: Callable[[str | None], None]) -> None:
        self._title_listeners = [c for c in self._title_listeners if c is not cb]

    def set_title(self, title: str | None) -> None:
        """Record a new live title (newest-wins) and notify listeners on change.

        Called by the Direct bus-tag hook (FREE) and by the periodic ICY reader
        (Cast/DLNA/AirPlay). ``title`` is expected already-sanitized; it is
        re-sanitized here defensively so this is the single SEC-004 boundary no
        matter the source. A no-op when the (sanitized) value is unchanged — no
        spurious broadcast. Thread-tolerant caller note: the Direct hook fires on
        a GLib bus thread; U7's listener is responsible for marshaling to the loop
        (this method itself only mutates a field + calls plain listeners)."""
        sanitized = sanitize_title(title) if title is not None else None
        if sanitized == self._title:
            return
        self._title = sanitized
        for cb in list(self._title_listeners):
            try:
                cb(sanitized)
            except Exception:  # pragma: no cover - listeners are best-effort
                _log.debug("radio: title listener raised (ignored)",
                           exc_info=True)

    def _start_title_reader(self, station: Station) -> None:
        """Start the bounded periodic ICY title reader for ``station`` (U6).

        Cast/DLNA/AirPlay only: those backends do not surface the live title, so
        the jukebox reads it itself against the ORIGINAL station URL (NOT the
        transcode-proxy URL — the proxy re-encodes to mp3 and strips ICY). A
        periodic best-effort read (:data:`RADIO_TITLE_POLL_S`), NOT a persistent
        second connection. Direct skips this (its bus tag is free). Always cancels
        any prior reader first so a switch never stacks readers."""
        self._stop_title_reader()
        if not _active_backend_uses_icy_reader():
            return  # Direct: title comes from the bus tag hook, not a read
        # F1 (SSRF): read the ALREADY-SSRF-VALIDATED final_url (set at start via
        # resolve_and_validate), NOT the raw directory-supplied station.play_url —
        # the reader must never fetch an unvalidated URL. The loop additionally
        # re-validates the host before each read (defense-in-depth vs DNS change).
        active = self._active
        url = active.final_url if active is not None else ""
        if not url:
            return
        self._title_reader_task = asyncio.get_running_loop().create_task(
            self._title_reader_loop(url))
        self._title_reader_task.add_done_callback(_log_task_exc)

    def _stop_title_reader(self) -> None:
        task = self._title_reader_task
        self._title_reader_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _title_reader_loop(self, url: str) -> None:
        """Bounded, periodic best-effort ICY read → :meth:`set_title`.

        Off the critical path (R6): each read is itself bounded and non-raising;
        a failure just leaves the last title (or None). Runs until cancelled by a
        station switch/stop. An immediate first read makes the title appear
        promptly, then it polls at :data:`RADIO_TITLE_POLL_S`."""
        try:
            while True:
                try:
                    # F1 (defense-in-depth vs DNS rebind): re-validate the host
                    # right before each read and SKIP the read on RadioUrlBlocked
                    # — never fetch a URL that now resolves to an internal host.
                    await validate_station_host(url)
                    title = await self._read_title(url)
                    self.set_title(title)
                except asyncio.CancelledError:
                    raise
                except RadioUrlBlocked:
                    _log.debug("radio: title read URL blocked by SSRF policy "
                               "(skipped)", exc_info=True)
                except Exception:  # pragma: no cover - reader is best-effort
                    _log.debug("radio: periodic title read failed (ignored)",
                               exc_info=True)
                await asyncio.sleep(RADIO_TITLE_POLL_S)
        except asyncio.CancelledError:
            return

    # ── start / switch (F1, R5) ────────────────────────────────────────────────

    async def start(self, station: Station) -> None:
        """Start (or instant-switch to) ``station`` on the shared output.

        First takeover: validate+resolve the final URL (U2) → ``output_router.stop()``
        → ``queue_engine.hold_current()`` (only when a track is playing) → mark
        radio active → begin endless playback of the final URL. An instant switch
        (already active) stops the current station stream and starts the new one
        WITHOUT re-holding (R5). The click report is fire-and-forget at start.

        Serialized under the lock so racing start/switch can't double-hold.
        """
        # Resolve + SSRF-validate OUTSIDE the lock: it may block on DNS/redirects
        # and must not stall a concurrent stop. hold happens under the lock below.
        # F8: capture the generation BEFORE the outside-lock validate so a stop()
        # completing during that window is not silently lost (it bumps the gen).
        gen_at_entry = self._gen
        final_url = await self._validate_url(station.play_url)

        async with self._lock:
            # F8: if a stop() bumped the generation while we validated outside the
            # lock, that stop must win — abort the start (don't hold/play). Without
            # this, a station would play AFTER an explicit stop (the stop's clear
            # ran, then our start re-activates it).
            if self._gen != gen_at_entry:
                _log.info("radio: start aborted — a stop raced the URL validate "
                          "(gen %d → %d)", gen_at_entry, self._gen)
                return

            first_takeover = self._active is None

            # Stop the current output stream immediately (a switch stops the old
            # station; a first takeover stops the track that was playing). We use
            # router.stop() — NOT set_backend, whose deferred next-track swap
            # would let the held track keep playing until it ended.
            await self._stop_output()

            if first_takeover:
                # Hold the queue exactly ONCE, and only if a track is actually
                # playing — an empty-queue takeover holds nothing but still plays
                # the station (R8 edge). Marking radio active BEFORE hold_current
                # means the queue_changed the hold emits already sees
                # radio_active() True, so _should_auto_start() is gated (FE-1) —
                # mirrors enter_output_hold setting its flag before mutating.
                self._active = _Active(station=station, final_url=final_url)
                if self._has_queue_current():
                    await self._hold_queue()
                    self._held = True
            else:
                # Instant switch (R5): no re-hold, just swap the station.
                self._active = _Active(station=station, final_url=final_url)

            # U7: connecting → playing. We flip to "connecting" the moment the new
            # station is selected (a switch re-enters connecting, clearing a prior
            # "failed"), then to "playing" once _play_url has been dispatched.
            self._status = "connecting"
            self._notify_state()

            # Begin endless playback of the validated final URL (U4/U5 seam).
            await self._play_url(station, final_url)

            # U7: dispatched → playing. (Device-level "actually producing audio"
            # is not observable headless; the backend failed-hook demotes to
            # "failed" if the endless stream never establishes / later drops.)
            self._status = "playing"
            self._notify_state()

            # Live title (U6): clear the prior station's title (a switch must not
            # briefly show the old station's now-playing) and (re)start the bounded
            # periodic ICY reader for the NEW station's original URL. On Direct the
            # reader is skipped — its bus-tag hook feeds set_title for free. The
            # title clear notifies U7's listeners so the UI drops to name-only
            # until the new title arrives.
            self.set_title(None)
            self._start_title_reader(station)

        # Good-citizen click report — fire-and-forget, outside the lock, never
        # blocks or fails the start (U1 already makes report_click non-raising).
        try:
            self._report_click(station.stationuuid)
        except Exception:  # pragma: no cover - report_click is itself best-effort
            _log.debug("radio: click report dispatch failed (ignored)",
                       exc_info=True)

    # ── stop / exit (F2, R8) ────────────────────────────────────────────────────

    async def stop(self) -> None:
        """Stop the station and restore the track queue (actor-independent).

        Clears ``radio_active`` → stops the station stream → resumes the held
        queue via the FE-2 dispatch (pop-and-play the held front at 0:00,
        honoring ``play_recorded`` and routing through ``_play_with_fallback`` so
        a since-died source degrades to skip/outage-hold rather than raising).
        No-op when not active. Serialized under the lock so two stops (or a
        stop racing a switch) can't double-resume.
        """
        async with self._lock:
            # F8: bump the generation on EVERY stop (even an already-idle one) so a
            # start currently validating outside the lock aborts — an explicit stop
            # must always win over a start that raced its URL validate.
            self._gen += 1

            if self._active is None:
                return  # already idle — a concurrent stop won the race

            need_resume = self._held

            # Clear active FIRST so the resume dispatch's queue_changed no longer
            # sees radio_active() True (the FE-1 gate lifts exactly here, letting
            # the queue play again). Mirrors clear_output_hold ordering.
            self._active = None
            self._held = False
            self._status = "idle"
            # U7: notify the idle transition (station() now None, status() idle)
            # so the RadioStateEvent + now-playing snapshot converge to "nothing
            # on radio". Fired before the resume dispatch below so the UI drops
            # the radio surface as the queue resumes.
            self._notify_state()

            # Live title (U6): stop the periodic ICY reader and clear the title so
            # a stopped station's now-playing can't linger (notifies U7's
            # listeners → the UI drops the radio title line).
            self._stop_title_reader()
            self.set_title(None)

            # Stop the station output stream.
            await self._stop_output()

            # Invalidate + reap the Cast/DLNA transcode-proxy (U5). Idempotent /
            # no-op for GStreamer-direct & AirPlay (they never mint a proxy).
            await self._teardown_stream()

            # Resume the track queue only if WE held it. An empty-queue takeover
            # held nothing → returning to idle is already correct.
            if need_resume:
                await self._resume_queue()
            elif self._queue_non_empty():
                # F6 (stranded-queue edge): we held nothing (empty-queue takeover),
                # but tracks were ENQUEUED while the station played. Without this
                # the queue would sit inert (no resume dispatch, no queue_changed)
                # and those tracks would strand. radio_active() has already lifted
                # (cleared above), so fire the normal auto-start path so they play.
                await self._autostart_queue()


# ── default dependency bindings (lazy ``app.state`` access, no import cycle) ──────
#
# These live at module scope (not as methods) so a test can construct a
# RadioSession with pure mocks and never touch app.state / the DB. The real
# process singleton (state.radio_session) wires the real bindings via these.


async def _default_stop_output() -> None:
    from app import state
    await state.output_router.stop()


def _default_has_queue_current() -> bool:
    from app import state
    return state.queue_engine.state.current is not None


def _default_queue_non_empty() -> bool:
    """F6: True when the track queue has any items (current or pending) — used to
    detect tracks enqueued DURING radio that a bare-hold stop would strand."""
    from app import state
    qe = state.queue_engine
    return qe.state.current is not None or bool(qe.queue)


async def _default_autostart_queue() -> None:
    """F6: fire the normal auto-start path for tracks enqueued while radio played.

    ``radio_active()`` has already lifted by the time stop() calls this, so
    ``_trigger_auto_advance`` pops the queue front into ``current`` and plays it
    (its own guard no-ops if something is already playing)."""
    from app import state
    await state._trigger_auto_advance()


async def _default_hold_queue() -> None:
    """Hold the currently-playing track, PRESERVING its ``play_recorded`` mark.

    ``queue_engine.hold_current(play_recorded=...)`` OVERWRITES the held item's
    mark with the argument (default False). Passing False would make an
    already-counted track re-count when radio stops and it replays — violating
    R8 ("the held item's ``play_recorded`` is untouched"). So we forward the
    current item's own mark, exactly like ``enter_output_hold`` does.
    """
    from app import state
    current = state.queue_engine.state.current
    mark = bool(getattr(current, "play_recorded", False)) if current is not None else False
    await state.queue_engine.hold_current(play_recorded=mark)


async def _default_play_url(station: Station, final_url: str) -> None:
    """U4/U5 seam: play ``final_url`` on the active backend as an endless stream.

    U3 minimal implementation: dispatch the radio pseudo-``Track`` to the output
    router's active backend. U4 teaches each backend to detect the sentinel
    (``is_radio_track`` / ``duration_ms=0``) and suppress its EOS/advance path;
    U5 substitutes a Cast/DLNA transcode-proxy capability URL for ``final_url``
    on those two backends. The state-machine call above does not change.
    """
    from app import state
    track = make_radio_track(station, final_url)
    # Deliberately NOT via dispatch_play(): dispatch_play reports to the
    # output-session SUPERVISOR (confirmed-start / outage classification / play
    # counting), none of which applies to an endless station (R8: no history/EOS
    # writes). Go straight to the router; U4 owns per-backend endless behavior.
    await state.output_router.play(final_url, track)


async def _default_resume_queue() -> None:
    """FE-2 resume dispatch: pop-and-play the held front at 0:00.

    Routes through ``state._resume_radio_hold`` (added in state.py), which uses
    ``queue_engine.advance()`` to pop the held front into ``current`` and then
    ``_play_with_fallback`` (ADV-2) so a source that died during a long station
    session degrades to skip / outage-hold instead of raising."""
    from app import state
    await state._resume_radio_hold()


def _default_report_click(stationuuid: str) -> None:
    get_radio_client().report_click(stationuuid)


async def _default_read_title(url: str) -> str | None:
    """U6 default: one bounded, best-effort ICY ``StreamTitle`` read against the
    ORIGINAL station URL (never the transcode-proxy URL — the proxy re-encodes to
    mp3 and strips ICY). Non-raising by contract; None = no title."""
    return await read_stream_title(url)


def _active_backend_uses_icy_reader() -> bool:
    """True when the active output backend needs the server-side ICY read (U6).

    GStreamer-direct surfaces the live title for free via its bus tag, so it does
    NOT need the periodic read; Cast/DLNA/AirPlay hand the audio to a device /
    transcode-proxy that does not surface the title, so they do. Detected by
    backend type (kept here, not in the state machine, so the session stays
    testable via the injected ``read_title`` seam). Unknown/no backend ⇒ False
    (no read — a station-name-only fallback is always safe)."""
    try:
        from app import state
        from app.output.direct import DirectAudioBackend
    except Exception:
        return False
    active = getattr(state.output_router, "active", None)
    if active is None:
        return False
    return not isinstance(active, DirectAudioBackend)


async def _default_teardown_stream() -> None:
    """U5 default: close+unregister the Cast/DLNA transcode-proxy session so its
    capability id is invalidated (a stale device 404s) and its ffmpeg is reaped.
    Idempotent and safe when no proxy session exists (the direct/AirPlay case)."""
    from app.radio import stream as radio_stream
    await radio_stream.close_radio_stream()
