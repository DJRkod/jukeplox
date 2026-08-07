"""U4 observability: session snapshots + event emission (R20).

Decomposed out of ``app.output.session`` (2026-07-11 supervisor plan,
pre-Phase-B maintainability split) — the observability surface the state
machine reports through, kept apart from the machine itself.

The WS/GET resync contract (docs/solutions/best-practices/
realtime-ui-stale-async-and-reconnect-resync.md): every OutputSessionEvent
delta is refetchable — the SAME dict shapes ride the broadcasts and the
now-playing GET snapshots (`output_session` field), so a client that missed
the delta converges from the snapshot.

Import discipline: ``app.output.session`` imports this module at the top and
re-exports the public functions as a facade; this module imports only
``app.output.hold`` at the top (hold never imports this module at the top)
and late-imports ``app.output.session`` INSIDE functions (the repo's
established convention), so no cycle can form.
"""

from __future__ import annotations

import logging

from app.output import hold

_log = logging.getLogger(__name__)


def session_snapshot() -> dict:
    """Lean session-state snapshot — the guest-facing truth (R20): the
    supervisor state plus whether an outage hold freezes the queue, plus
    whether a Cast gapless flow session is live (U10 — whether playback is
    riding the stitched flow stream right now; the registry's liveness
    check, never the session id, which is the flow route's capability
    credential). The same fields lead the admin snapshot, so guest and
    admin can never disagree about the state itself.

    ``source_lock`` (2026-08-04-002 plexplayer plan U4): "plex" while the
    PERSISTED selected backend is plexplayer — ``state.output_requires_plex``
    is the one gate truth (loud warning there: never the router's
    deferred-swap state) — else None. Guests key the U5 gray-out body
    attribute off this field."""
    from app import state
    from app.output import session
    from app.output.flow import current_flow_session
    sup = session.get_supervisor()
    return {
        "state": sup.session_state,
        "held": hold.output_hold_active(),
        "gapless_flow_active": current_flow_session() is not None,
        "source_lock": "plex" if state.output_requires_plex() else None,
    }


async def session_snapshot_admin() -> dict:
    """Admin-rich session snapshot (R20): the lean truth plus outage detail —
    reason, device, attempt count, next-retry countdown, resume-window
    remaining, was_paused, flap/idle-paused reasons. Rides both the admin
    OutputSessionEvent broadcast and the admin now-playing/queue GETs (one
    payload shape, one client render path). Never raises; fields whose
    source is unavailable degrade to None."""
    from app.output import session
    sup = session.get_supervisor()
    snap = session_snapshot()
    info = sup.outage_info() or {}
    window_remaining_s = None
    if info:
        # Window remaining is advisory display data (the authoritative check
        # stays at audio-start, U3) — a failed read degrades to None.
        try:
            window_min = int(await sup._window_minutes())
            elapsed = max(0.0, sup._clock() - float(info["entered_at"]))
            window_remaining_s = max(0, int(window_min * 60 - elapsed))
        except Exception:
            window_remaining_s = None
    ot = sup._outage
    device_name = None
    if ot is not None and ot.backend is not None:
        device_name = getattr(ot.backend, "_resolved_name", None)
    snap.update({
        "reason": hold.output_hold_reason() or info.get("reason") or None,
        "backend_type": info.get("backend_type") or None,
        "device_id": info.get("device_id") or None,
        "device_name": (str(device_name) if device_name else None)
                       or info.get("device_id") or None,
        "attempts": info.get("attempts"),
        "next_retry_s": info.get("next_delay_s"),
        "window_remaining_s": window_remaining_s,
        "was_paused": sup.was_paused,
        "flap_tripped": info.get("flap_tripped"),
        "idle_paused_reason": sup.idle_paused_reason or None,
    })
    return snap


async def emit_session_event() -> None:
    """Broadcast the CURRENT session state (U4, R20): admin-rich to the admin
    socket, guest-lean to guests — the TrackSkippedEvent dual-broadcast
    pattern. Emission points (kept sane — never per-timer-tick): hold
    entered, reconnect attempt started, attempt failed back to outage_paused,
    paused/idle_paused landings, and hold cleared / resumed. Best-effort: a
    broadcast failure never blocks the state machine."""
    try:
        from app.events.bus import manager
        from app.events.types import OutputSessionEvent
        admin = await session_snapshot_admin()
        lean = session_snapshot()
        await manager.broadcast_to_admins(OutputSessionEvent(**admin))
        await manager.broadcast_to_guests(OutputSessionEvent(**lean))
    except Exception:
        _log.warning("Output session: state broadcast failed", exc_info=True)


def _schedule_emit() -> None:
    """Fire-and-forget ``emit_session_event`` for SYNC call sites
    (``clear_output_hold``, ``_land_idle_paused``): the task reads the
    settled state when it runs, so a clear-then-transition sequence emits
    the final truth, not the intermediate one."""
    from app.output import session
    session._spawn_supervised(emit_session_event())
