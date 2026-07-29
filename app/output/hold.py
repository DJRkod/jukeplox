"""Output hold (U2, R15/R18) — the outage-hold flag and its operations.

Decomposed out of ``app.output.session`` (2026-07-11 supervisor plan,
pre-Phase-B maintainability split) so the mutable hold state has exactly ONE
home: every reader — supervisor methods, retry-loop closures, the U2
classifier, ``state._should_auto_start`` / ``state._do_advance``, API
endpoints — goes through ``output_hold_active()`` / ``output_hold_reason()``
here, never a from-imported copy of the bool (a from-import would snapshot
the value at import time).

Module-level flag mirroring the ``_closing_active`` precedent in app.state:
True from a device-level failure until a resume. It gates
``state._should_auto_start`` and ``state._do_advance`` so a held queue
consumes ZERO items during an outage. Deliberately in-memory only (R18): the
hold does not survive a restart — the held ITEM does, as the persisted queue
front — so a restart lands idle-paused with the track next-up.

Import discipline: ``app.output.session`` imports this module at the top and
re-exports the public functions as a facade; this module late-imports
``app.output.session`` / ``app.output.session_events`` / ``app.state``
INSIDE functions (the repo's established convention), so no cycle can form.
"""

from __future__ import annotations

import logging
import time
from typing import Any

_log = logging.getLogger(__name__)

_output_hold: bool = False
_output_hold_reason: str = ""


def output_hold_active() -> bool:
    """True while a device-level outage holds the queue. Consulted by
    ``state._should_auto_start`` and ``state._do_advance``."""
    return _output_hold


def output_hold_reason() -> str:
    """The reason the current hold was entered ("" when not holding) — U3's
    reconnect loop and U4's observability surface consume this."""
    return _output_hold_reason


async def enter_output_hold(reason: str, *,
                            play_recorded: bool | None = None) -> None:
    """Device-level failure: pause and hold the interrupted track (U2).

    Sets the hold flag FIRST (the Closing Time discipline — the queue events
    the hold emits must already see auto-start gated), then re-front-inserts
    the current item marked ``play_recorded`` so a resume never re-counts
    (R19) and lands the queue paused so the guest UI reads coherently.
    Idempotent: a second signal while holding must not double-insert.

    ``play_recorded=None`` (the dispatch-failure path, where the supervisor
    never confirmed anything) falls back to the item's own persisted mark.

    U3: also opens the supervisor's reconnect context (``begin_outage``) —
    outage-entry capture happens BEFORE the queue mutations because
    ``hold_current`` flips ``is_paused`` True, so the R17 ``was_paused``
    answer only exists now; the last-known position (R7) is per-backend
    state the mutations don't touch, captured here for one obvious ordering.
    """
    global _output_hold, _output_hold_reason
    from app import state
    from app.output import session, session_events
    if _output_hold:
        return
    _output_hold = True
    _output_hold_reason = reason
    _log.warning(
        "Output session: OUTAGE HOLD entered (%s) — queue paused; no items "
        "will be consumed until resume", reason,
    )
    backend = state.output_router.active
    was_paused = state.queue_engine.state.is_paused
    if reason in ("dispatch_failed", "confirm_timeout"):
        # This dispatch never confirmed — the held item never played, so any
        # backend position residue (Cast _pos_snapshot_ms, DLNA _play_start)
        # belongs to the PREVIOUS track. Resume from the top.
        position_ms = 0
    else:
        position_ms = await _capture_position_ms(backend)
    current = state.queue_engine.state.current
    if current is not None:
        mark = play_recorded
        if mark is None:
            mark = bool(getattr(current, "play_recorded", False))
        await state.queue_engine.hold_current(play_recorded=mark)
    else:
        await state.queue_engine.set_paused(True)
    # Open the reconnect context + backoff retry loop (U3).
    session.get_supervisor().begin_outage(
        reason, backend=backend, was_paused=was_paused,
        position_ms=position_ms,
        held_track_id=(current.track.id if current is not None else ""))
    # Surface the hold everywhere (U4, R20): the admin socket gets the rich
    # OutputSessionEvent (reason/device/retry/window), guests the lean
    # paused-state equivalent. Emitted AFTER begin_outage so the payload
    # carries the armed retry schedule. Best-effort — never blocks the hold.
    await session_events.emit_session_event()


def clear_output_hold() -> None:
    """Exit the outage hold. Owners (U3): the resume orchestration (auto or
    manual dispatch), a confirmed start on any dispatch (manual skip / device
    switch reached audio), or restart (the flag is in-memory). Any exit also
    retires the reconnect machinery — no timer, listener or in-flight attempt
    may act for a finished outage."""
    global _output_hold, _output_hold_reason
    from app.output import session, session_events
    if not _output_hold:
        return
    _output_hold = False
    _output_hold_reason = ""
    try:
        session.get_supervisor().retire_outage()
    except Exception:
        _log.warning("Output session: outage retirement failed", exc_info=True)
    _log.info("Output session: outage hold cleared")
    # U4 (R20): every hold exit is observable — resume orchestration (auto or
    # manual), confirmed start on a manual skip/switch, queue-cleared landing.
    # Scheduled (sync context): the emitted payload reads the SETTLED state
    # the exit path sets right after this call (playing / idle / paused).
    session_events._schedule_emit()


async def _capture_position_ms(backend: Any) -> int:
    """Last-known playback position at outage entry (R7). Per-backend capture
    (plan U3): Cast's ``_pos_snapshot_ms`` survives the connection loss;
    DLNA's ``_play_start`` wall-clock estimate survives the ``is_playing``
    flip its outage reporters perform (``get_position()`` would read 0
    there); Direct/AirPlay answer through their own ``get_position``
    (pipeline query / monotonic anchor). Never raises; unknown → 0.

    U10 duck-typed override: a backend exposing ``capture_held_position_ms()``
    (sync, loop-side) is consulted FIRST — in Cast flow mode the raw
    ``_pos_snapshot_ms`` is device STREAM time, and the hook returns the
    TRACK-RELATIVE held position mapped through the stitch timeline instead.
    A hook returning None falls through to the normal reads (per-track mode
    stays byte-identical)."""
    if backend is None:
        return 0
    cap = getattr(backend, "capture_held_position_ms", None)
    if callable(cap):
        try:
            pos = cap()
        except Exception:
            _log.warning("Output session: held-position capture hook failed",
                         exc_info=True)
            pos = None
        if pos is not None:
            try:
                return max(0, int(pos))
            except (TypeError, ValueError):
                return 0
    snap = getattr(backend, "_pos_snapshot_ms", None)
    if snap is not None:
        try:
            return max(0, int(snap))
        except (TypeError, ValueError):
            return 0
    play_start = getattr(backend, "_play_start", None)
    if play_start is not None:
        try:
            if play_start > 0:
                return max(0, int((time.monotonic() - play_start) * 1000))
        except TypeError:
            pass
        return 0
    try:
        pos = await backend.get_position()
        return max(0, int(pos or 0))
    except Exception:
        return 0
