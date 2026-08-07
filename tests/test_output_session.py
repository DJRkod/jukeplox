"""Tests for the output-session supervisor (2026-07-11 supervisor plan U1).

The confirmed-start chokepoint: play counts fire from a single confirmed-
playback-start signal, never from dispatch. Every timer here is FAKE (an
injected timer factory) — no real sleeps, per the repo's pytest-hang policy
(docs/solutions/workflow-issues/pytest-combined-run-hang-bound-with-gnu-timeout.md).
"""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

# The hold flag's home since the session decomposition — tests patch the
# OWNING module (production readers go through hold.output_hold_active()).
from app.output import hold
from app.output.session import (
    CONFIRM_DEADLINE_S,
    CONFIRM_EXTENSION_S,
    OutputSessionSupervisor,
)
from app.plex.models import Track

# The single fake supervisor-timer shape lives in conftest (2026-07-11 review
# consolidation) — same objects the fresh_supervisor fixture arms.
from tests.conftest import FakeTimerFactory


def make_track(tid="t1", title="Song", artist="A", album="B") -> Track:
    return Track(id=tid, title=title, artist=artist, album=album,
                 duration_ms=180000, stream_key="/parts/1/f.flac")


def make_supervisor(**kw):
    timers = FakeTimerFactory()
    rec = MagicMock()
    sup = OutputSessionSupervisor(record_play=rec, timer_factory=timers, **kw)
    return sup, timers, rec


async def _drain():
    """Settle the deadline task (+ optional probe await) — zero real delay."""
    for _ in range(8):
        await asyncio.sleep(0)


# ── confirmed-start chokepoint ────────────────────────────────────────────────

async def test_confirmed_start_records_play_exactly_once():
    """Happy path: dispatch → backend confirmation → record_play once with the
    dispatched track; the deadline timer is retired."""
    sup, timers, rec = make_supervisor()
    track = make_track()
    token = sup.on_dispatched(track)
    rec.assert_not_called()          # dispatch alone must never count
    sup.on_playback_confirmed(token)
    rec.assert_called_once()
    assert rec.call_args.args[0] is track
    assert timers.timers[0].cancelled


async def test_duplicate_confirmation_ignored():
    sup, timers, rec = make_supervisor()
    token = sup.on_dispatched(make_track())
    sup.on_playback_confirmed(token)
    sup.on_playback_confirmed(token)
    rec.assert_called_once()


async def test_rapid_double_dispatch_ignores_stale_token():
    """Skip during the confirmation window: only the SECOND track's
    confirmation counts; the superseded token is ignored and its deadline
    timer cancelled."""
    sup, timers, rec = make_supervisor()
    t1, t2 = make_track("t1"), make_track("t2")
    token1 = sup.on_dispatched(t1)
    token2 = sup.on_dispatched(t2)
    assert timers.timers[0].cancelled          # t1's deadline retired
    sup.on_playback_confirmed(token1)          # stale — ignored
    rec.assert_not_called()
    sup.on_playback_confirmed(token2)
    rec.assert_called_once()
    assert rec.call_args.args[0] is t2


async def test_late_deadline_callback_for_superseded_token_is_noop():
    """A deadline callback that slips past its cancel (call_later race shape)
    must be dropped by the token-staleness guard: no outage, no count."""
    sup, timers, rec = make_supervisor()
    outages = []
    sup.add_outage_listener(lambda *a: outages.append(a))
    sup.on_dispatched(make_track("t1"))
    sup.on_dispatched(make_track("t2"))
    timers.timers[0].cb()                      # late callback for stale token
    await _drain()
    assert outages == []
    rec.assert_not_called()


async def test_deadline_expiry_no_confirmation_emits_outage_and_no_count():
    """Error path: deadline expires with no signal → no count, outage-suspected
    emitted, and a LATE confirmation no longer counts."""
    sup, timers, rec = make_supervisor()
    outages = []
    sup.add_outage_listener(lambda token, track, reason: outages.append((token, track, reason)))
    track = make_track()
    token = sup.on_dispatched(track)
    timers.timers[0].fire()
    await _drain()
    rec.assert_not_called()
    assert outages == [(token, track, "confirm_timeout")]
    sup.on_playback_confirmed(token)           # too late — already classified
    rec.assert_not_called()


async def test_outage_emission_does_not_advance_or_skip():
    """The U1 hook only notifies (U2 consumes it) — the supervisor itself must
    not touch playback. Structural pin: emission reaches every listener even
    when one raises, and record_play stays untouched."""
    sup, timers, rec = make_supervisor()
    seen = []
    sup.add_outage_listener(MagicMock(side_effect=RuntimeError("listener bug")))
    sup.add_outage_listener(lambda *a: seen.append(a))
    sup.on_dispatched(make_track())
    timers.timers[0].fire()
    await _drain()
    assert len(seen) == 1
    rec.assert_not_called()


# ── R15 third outcome: bounded deadline extension ─────────────────────────────

async def test_reachable_preplayback_extends_deadline_once_then_confirms(caplog):
    """Cast BUFFERING on a reachable device at deadline → ONE bounded
    extension (INFO-logged), then a confirmation during the extension counts
    normally — no outage, no skip cascade."""
    sup, timers, rec = make_supervisor()
    outages = []
    sup.add_outage_listener(lambda *a: outages.append(a))
    probe = AsyncMock(return_value=(True, "BUFFERING"))
    token = sup.on_dispatched(make_track(), probe=probe)
    with caplog.at_level(logging.INFO, logger="app.output.session"):
        timers.timers[0].fire()
        await _drain()
    assert outages == []
    assert len(timers.timers) == 2             # the single extension armed
    assert timers.timers[1].delay == CONFIRM_EXTENSION_S
    assert any("extend" in r.getMessage().lower() for r in caplog.records)
    sup.on_playback_confirmed(token)
    rec.assert_called_once()
    assert timers.timers[1].cancelled


async def test_extension_happens_exactly_once_then_classifies():
    """Second expiry after the single extension → outage-suspected, no count,
    and the probe is not consulted again (extend EXACTLY once)."""
    sup, timers, rec = make_supervisor()
    outages = []
    sup.add_outage_listener(lambda *a: outages.append(a))
    probe = AsyncMock(return_value=(True, "BUFFERING"))
    sup.on_dispatched(make_track(), probe=probe)
    timers.timers[0].fire()
    await _drain()
    timers.timers[1].fire()
    await _drain()
    assert len(outages) == 1
    rec.assert_not_called()
    assert probe.await_count == 1
    assert len(timers.timers) == 2             # no third timer


async def test_unreachable_device_gets_no_extension():
    sup, timers, rec = make_supervisor()
    outages = []
    sup.add_outage_listener(lambda *a: outages.append(a))
    probe = AsyncMock(return_value=(False, "BUFFERING"))
    sup.on_dispatched(make_track(), probe=probe)
    timers.timers[0].fire()
    await _drain()
    assert len(outages) == 1
    assert len(timers.timers) == 1


async def test_non_preplayback_transport_state_gets_no_extension():
    sup, timers, rec = make_supervisor()
    outages = []
    sup.add_outage_listener(lambda *a: outages.append(a))
    probe = AsyncMock(return_value=(True, "IDLE"))
    sup.on_dispatched(make_track(), probe=probe)
    timers.timers[0].fire()
    await _drain()
    assert len(outages) == 1
    assert len(timers.timers) == 1


async def test_probe_exception_treated_as_unreachable():
    sup, timers, rec = make_supervisor()
    outages = []
    sup.add_outage_listener(lambda *a: outages.append(a))
    probe = AsyncMock(side_effect=RuntimeError("probe blew up"))
    sup.on_dispatched(make_track(), probe=probe)
    timers.timers[0].fire()
    await _drain()
    assert len(outages) == 1
    rec.assert_not_called()


async def test_supersede_during_probe_await_drops_stale_deadline():
    """A new dispatch landing while the stale deadline's probe is in flight
    must abort the stale path: no extension armed for it, no outage."""
    sup, timers, rec = make_supervisor()
    outages = []
    sup.add_outage_listener(lambda *a: outages.append(a))
    gate = asyncio.Event()

    async def probe():
        await gate.wait()
        return (True, "BUFFERING")

    sup.on_dispatched(make_track("t1"), probe=probe)
    timers.timers[0].fire()
    await _drain()                             # deadline task parked on gate
    token2 = sup.on_dispatched(make_track("t2"))
    gate.set()
    await _drain()
    assert outages == []
    assert len(timers.timers) == 2             # t1 deadline + t2 deadline only
    sup.on_playback_confirmed(token2)
    rec.assert_called_once()


# ── R19 groundwork: play_recorded mark ────────────────────────────────────────

async def test_play_recorded_mark_skips_counting():
    """A dispatch carrying the R19 mark (resume of an already-counted item)
    confirms without counting — the chokepoint is bypassed for the count only."""
    sup, timers, rec = make_supervisor()
    token = sup.on_dispatched(make_track(), play_recorded=True)
    sup.on_playback_confirmed(token)
    rec.assert_not_called()
    assert timers.timers[0].cancelled          # confirmation still retires the deadline


# ── dispatch withdrawal ───────────────────────────────────────────────────────

async def test_dispatch_failed_withdraws_pending_dispatch():
    """A failed dispatch (holder error before the backend accepted it) must not
    age into an outage emission or accept a late confirmation."""
    sup, timers, rec = make_supervisor()
    outages = []
    sup.add_outage_listener(lambda *a: outages.append(a))
    token = sup.on_dispatched(make_track())
    sup.on_dispatch_failed(token)
    assert timers.timers[0].cancelled
    assert sup.current_token() is None
    timers.timers[0].cb()                      # late callback → staleness guard
    await _drain()
    assert outages == []
    sup.on_playback_confirmed(token)
    rec.assert_not_called()


async def test_dispatch_failed_with_stale_token_is_noop():
    sup, timers, rec = make_supervisor()
    token1 = sup.on_dispatched(make_track("t1"))
    token2 = sup.on_dispatched(make_track("t2"))
    sup.on_dispatch_failed(token1)             # stale — must not kill t2
    assert sup.current_token() == token2
    sup.on_playback_confirmed(token2)
    rec.assert_called_once()


# ── token bookkeeping / configuration ─────────────────────────────────────────

async def test_current_token_reflects_pending_dispatch():
    sup, timers, rec = make_supervisor()
    assert sup.current_token() is None
    token = sup.on_dispatched(make_track())
    assert sup.current_token() == token
    # A confirmed dispatch stays current (late signals resolve against it)…
    sup.on_playback_confirmed(token)
    assert sup.current_token() == token
    # …but an outage-classified one is retired.
    token2 = sup.on_dispatched(make_track("t2"))
    timers.timers[-1].fire()
    await _drain()
    assert sup.current_token() is None
    assert token2 is not None


def test_confirm_deadline_default_is_twelve_seconds():
    # Deferred-to-implementation tuning starts at 12s (plan: ~10–15s band).
    assert CONFIRM_DEADLINE_S == 12.0


async def test_timer_armed_with_configured_deadline():
    sup, timers, rec = make_supervisor(deadline_s=7.5, extension_s=3.25)
    sup.on_dispatched(make_track(), probe=AsyncMock(return_value=(True, "BUFFERING")))
    assert timers.timers[0].delay == 7.5
    timers.timers[0].fire()
    await _drain()
    assert timers.timers[1].delay == 3.25


async def test_default_timer_delay_is_module_deadline():
    sup, timers, rec = make_supervisor()
    sup.on_dispatched(make_track())
    assert timers.timers[0].delay == CONFIRM_DEADLINE_S


# ── module singleton + backend-facing entry ───────────────────────────────────

async def test_get_supervisor_returns_lazy_singleton(monkeypatch):
    from app.output import session
    monkeypatch.setattr(session, "_supervisor", None)
    sup = session.get_supervisor()
    assert isinstance(sup, session.OutputSessionSupervisor)
    assert session.get_supervisor() is sup


async def test_notify_confirmed_routes_to_module_singleton(monkeypatch):
    from app.output import session
    sup, timers, rec = make_supervisor()
    monkeypatch.setattr(session, "_supervisor", sup)
    token = sup.on_dispatched(make_track())
    session.notify_confirmed(token)
    rec.assert_called_once()


async def test_default_record_play_resolves_app_state(monkeypatch):
    """Production wiring: the default record_play late-binds to
    app.state.record_play so test patches of that symbol keep working."""
    import app.state as st
    timers = FakeTimerFactory()
    sup = OutputSessionSupervisor(timer_factory=timers)
    rec = MagicMock()
    monkeypatch.setattr(st, "record_play", rec)
    track = make_track()
    token = sup.on_dispatched(track)
    sup.on_playback_confirmed(token)
    rec.assert_called_once_with(track)


# ── pre-seeded DB integration ─────────────────────────────────────────────────

async def _play_db(tmp_path, monkeypatch):
    import app.database as database
    from app.config import Settings
    s = Settings(data_dir=tmp_path, secret_key="test")
    monkeypatch.setattr(database, "settings", s)
    await database.init_db()
    return database


async def test_confirmed_start_adds_one_play_to_preseeded_db(tmp_path, monkeypatch):
    """Integration: gating changes nothing about accumulated counts — one
    confirmed start adds exactly one play on top of pre-seeded rows, unrelated
    rows stay untouched, and curation's unrecord_play still reconciles."""
    import app.state as st
    database = await _play_db(tmp_path, monkeypatch)
    try:
        for _ in range(5):
            await database.increment_play_count("track", "t1")
        for _ in range(7):
            await database.increment_play_count("track", "t9")
        await database.increment_play_count("album", "B")
        await database.increment_play_count("artist", "A")

        timers = FakeTimerFactory()
        sup = OutputSessionSupervisor(timer_factory=timers)  # real record_play
        track = make_track("t1")
        token = sup.on_dispatched(track)
        sup.on_playback_confirmed(token)
        # record_play is fire-and-forget create_task — poll briefly.
        for _ in range(200):
            if await database.get_play_count("track", "t1") == 6:
                break
            await asyncio.sleep(0.01)
        assert await database.get_play_count("track", "t1") == 6
        assert await database.get_play_count("track", "t9") == 7   # untouched
        assert await database.get_play_count("album", "B") == 2
        assert await database.get_play_count("artist", "A") == 2

        await st.unrecord_play("t1", "B", "A")                     # still reconciles
        assert await database.get_play_count("track", "t1") == 5
    finally:
        await database.close_db()


async def test_deadline_expiry_writes_no_play_rows(tmp_path, monkeypatch):
    """AE1 (count side): a dead device — dispatch with no confirmation — leaves
    ZERO play-count rows for the dispatched track."""
    database = await _play_db(tmp_path, monkeypatch)
    try:
        timers = FakeTimerFactory()
        sup = OutputSessionSupervisor(timer_factory=timers)
        outages = []
        sup.add_outage_listener(lambda *a: outages.append(a))
        sup.on_dispatched(make_track("t1"))
        timers.timers[0].fire()
        await _drain()
        assert len(outages) == 1
        assert await database.get_play_count("track", "t1") == 0
        assert await database.get_all_play_counts("track") == []
    finally:
        await database.close_db()


# ── U2: failure classifier + output hold ──────────────────────────────────────
# The classifier is the production outage listener (registered by
# get_supervisor); tests attach it to the fixture supervisor explicitly and
# patch app.state's queue/skip/advance surfaces.

import contextlib
from unittest.mock import patch


def _wire_state(stack, qe=None, probe="unset"):
    """Patch app.state's surfaces the classifier/hold touch. Returns
    (queue_engine, emit_track_skipped, do_advance)."""
    import app.state as st
    from app.queue.engine import QueueEngine
    if qe is None:
        qe = QueueEngine()
    skipped = AsyncMock()
    advance = AsyncMock()
    stack.enter_context(patch.object(st, "queue_engine", qe))
    stack.enter_context(patch("app.queue.engine.database.save_queue", AsyncMock()))
    stack.enter_context(patch("app.queue.engine.database.save_history", AsyncMock()))
    stack.enter_context(patch.object(st, "_emit_track_skipped", skipped))
    stack.enter_context(patch.object(st, "_do_advance", advance))
    stack.enter_context(patch("app.events.bus.manager.broadcast_to_admins", AsyncMock()))
    if probe == "unset":
        stack.enter_context(patch.object(st, "_output_probe", lambda: None))
    else:
        stack.enter_context(patch.object(st, "_output_probe", lambda: probe))
    return qe, skipped, advance


async def _start_playing(qe, track):
    """Queue `track` and advance so it is the engine's playing current."""
    await qe.append(track)
    return await qe.advance()


async def test_device_level_reason_holds_counted_track(monkeypatch):
    """The origin party scenario's core (AE1 hold side): Cast reports
    connection LOST mid-track → hold entered, the interrupted item is
    front-inserted with play_recorded=True (its confirm already counted),
    ZERO queue items consumed, no TrackSkippedEvent, no advance."""
    from app.output import session
    sup, timers, rec = _fresh(monkeypatch)
    sup.add_outage_listener(session.classify_outage)
    with contextlib.ExitStack() as stack:
        qe, skipped, advance = _wire_state(stack)
        item = await _start_playing(qe, make_track("t1"))
        await qe.append(make_track("t2"))
        token = sup.on_dispatched(item.track)
        sup.on_playback_confirmed(token)         # play counted
        rec.assert_called_once()

        sup.on_outage_reported("connection_lost")
        await _drain()

        assert session.output_hold_active() is True
        assert session.output_hold_reason() == "connection_lost"
        assert [i.track_id for i in qe.queue] == ["t1", "t2"]
        assert qe.queue[0].play_recorded is True  # R19: never counted twice
        assert qe.state.current is None
        assert qe.state.is_paused is True
        skipped.assert_not_called()
        advance.assert_not_called()
        rec.assert_called_once()                  # still exactly one count


async def test_confirm_timeout_unreachable_holds_uncounted_track(monkeypatch):
    """Deadline expiry + classifier probe UNREACHABLE → device-level hold;
    the play never confirmed, so the held item stays play_recorded=False
    (it counts when it finally plays at resume)."""
    from app.output import session
    sup, timers, rec = _fresh(monkeypatch)
    sup.add_outage_listener(session.classify_outage)
    probe = AsyncMock(return_value=(False, None))
    with contextlib.ExitStack() as stack:
        qe, skipped, advance = _wire_state(stack, probe=probe)
        item = await _start_playing(qe, make_track("t1"))
        sup.on_dispatched(item.track)             # dispatch, never confirmed
        timers.timers[0].fire()                   # deadline → outage-suspected
        await _drain()

        assert session.output_hold_active() is True
        assert [i.track_id for i in qe.queue] == ["t1"]
        assert qe.queue[0].play_recorded is False
        skipped.assert_not_called()
        advance.assert_not_called()
        rec.assert_not_called()


async def test_confirm_timeout_reachable_is_track_level_skip(monkeypatch):
    """Deadline expiry + classifier probe REACHABLE → the track is the
    failure: TrackSkippedEvent + advance (today's skip behavior), no hold."""
    from app.output import session
    sup, timers, rec = _fresh(monkeypatch)
    sup.add_outage_listener(session.classify_outage)
    probe = AsyncMock(return_value=(True, "IDLE"))
    with contextlib.ExitStack() as stack:
        qe, skipped, advance = _wire_state(stack, probe=probe)
        item = await _start_playing(qe, make_track("t1"))
        sup.on_dispatched(item.track)
        timers.timers[0].fire()
        await _drain()

        assert session.output_hold_active() is False
        skipped.assert_awaited_once()
        assert skipped.await_args.args[0] is item.track
        advance.assert_awaited_once()


async def test_no_probe_available_defaults_track_level(monkeypatch):
    """No reachability probe on the active backend → no outage evidence:
    keep today's skip behavior (liveness beats an unprovable hold)."""
    from app.output import session
    sup, timers, rec = _fresh(monkeypatch)
    sup.add_outage_listener(session.classify_outage)
    with contextlib.ExitStack() as stack:
        qe, skipped, advance = _wire_state(stack, probe=None)
        item = await _start_playing(qe, make_track("t1"))
        sup.on_dispatched(item.track)
        timers.timers[0].fire()
        await _drain()

        assert session.output_hold_active() is False
        skipped.assert_awaited_once()
        advance.assert_awaited_once()


async def test_classifier_probe_exception_fails_closed_to_hold(monkeypatch):
    """After a reported outage signal, a probe that raises counts as
    unreachable (matches the U1 deadline posture) — protect the queue."""
    from app.output import session
    sup, timers, rec = _fresh(monkeypatch)
    sup.add_outage_listener(session.classify_outage)
    probe = AsyncMock(side_effect=RuntimeError("probe transport died"))
    with contextlib.ExitStack() as stack:
        qe, skipped, advance = _wire_state(stack, probe=probe)
        item = await _start_playing(qe, make_track("t1"))
        sup.on_dispatched(item.track)
        timers.timers[0].fire()
        await _drain()

        assert session.output_hold_active() is True
        skipped.assert_not_called()
        advance.assert_not_called()


async def test_stale_classification_dropped_when_newer_dispatch_live(monkeypatch):
    """R16 double-advance firewall: an outage emission whose classification
    task runs AFTER a newer dispatch landed (a backend's own advance racing
    the deadline) must act on nothing — no skip, no advance, no hold."""
    from app.output import session
    sup, timers, rec = _fresh(monkeypatch)
    sup.add_outage_listener(session.classify_outage)
    probe = AsyncMock(return_value=(True, "IDLE"))
    with contextlib.ExitStack() as stack:
        qe, skipped, advance = _wire_state(stack, probe=probe)
        item = await _start_playing(qe, make_track("t1"))
        sup.on_dispatched(item.track)
        sup.on_outage_reported("confirm_timeout")  # emits; classify task queued
        sup.on_dispatched(make_track("t2"))        # newer dispatch wins the race
        await _drain()

        assert session.output_hold_active() is False
        skipped.assert_not_called()
        advance.assert_not_called()
        probe.assert_not_called()                  # dropped at task entry


async def test_dispatch_landing_during_probe_drops_classification(monkeypatch):
    """Same firewall at the second checkpoint: the supersede lands while the
    classifier's reachability probe is awaiting."""
    from app.output import session
    sup, timers, rec = _fresh(monkeypatch)
    sup.add_outage_listener(session.classify_outage)

    async def _probe_with_race():
        sup.on_dispatched(make_track("t2"))        # supersede mid-probe
        return (True, "IDLE")

    probe = AsyncMock(side_effect=_probe_with_race)
    with contextlib.ExitStack() as stack:
        qe, skipped, advance = _wire_state(stack, probe=probe)
        item = await _start_playing(qe, make_track("t1"))
        sup.on_dispatched(item.track)
        timers.timers[0].fire()                    # deadline → emission
        await _drain()

        assert session.output_hold_active() is False
        skipped.assert_not_called()
        advance.assert_not_called()
        probe.assert_awaited_once()


async def test_repeated_outage_signals_do_not_double_insert(monkeypatch):
    """Idempotence (System-Wide Impact): a second device-level signal during
    an active hold must not front-insert again or re-classify."""
    from app.output import session
    sup, timers, rec = _fresh(monkeypatch)
    sup.add_outage_listener(session.classify_outage)
    with contextlib.ExitStack() as stack:
        qe, skipped, advance = _wire_state(stack)
        item = await _start_playing(qe, make_track("t1"))
        token = sup.on_dispatched(item.track)
        sup.on_playback_confirmed(token)

        sup.on_outage_reported("connection_lost")
        await _drain()
        sup.on_outage_reported("poll_errors")     # a second signal, same outage
        await _drain()

        assert [i.track_id for i in qe.queue] == ["t1"]  # exactly one insert
        assert session.output_hold_reason() == "connection_lost"


async def test_hold_with_idle_queue_just_pauses(monkeypatch):
    """A device-level signal with nothing current holds state coherently:
    flag set + paused, nothing to insert."""
    from app.output import session
    sup, timers, rec = _fresh(monkeypatch)
    sup.add_outage_listener(session.classify_outage)
    with contextlib.ExitStack() as stack:
        qe, skipped, advance = _wire_state(stack)
        sup.on_outage_reported("connection_lost")  # no dispatch, no current
        await _drain()

        assert session.output_hold_active() is True
        assert qe.queue == []
        assert qe.state.is_paused is True


async def test_confirmed_start_clears_hold(monkeypatch):
    """U2's minimal hold exit: a manual action (skip / device switch) whose
    dispatch CONFIRMS proves audio is flowing — the hold ends."""
    from app.output import session
    sup, timers, rec = _fresh(monkeypatch)
    monkeypatch.setattr(hold, "_output_hold", True)
    monkeypatch.setattr(hold, "_output_hold_reason", "connection_lost")
    token = sup.on_dispatched(make_track("t2"))
    assert session.output_hold_active() is True   # dispatch alone: still held
    sup.on_playback_confirmed(token)
    assert session.output_hold_active() is False
    rec.assert_called_once()


async def test_dispatch_play_recorded_semantics(monkeypatch):
    """The hold-mark source of truth: confirmed or mark-carrying dispatches
    read True; a pending unconfirmed one reads False; unknown tokens default
    True (R19 ranks never-count-twice above never-miss-one) — including after
    an outage emission retires the dispatch."""
    sup, timers, rec = _fresh(monkeypatch)
    token = sup.on_dispatched(make_track("t1"))
    assert sup.dispatch_play_recorded(token) is False   # pending, uncounted
    sup.on_playback_confirmed(token)
    assert sup.dispatch_play_recorded(token) is True    # counted

    token2 = sup.on_dispatched(make_track("t2"), play_recorded=True)
    assert sup.dispatch_play_recorded(token2) is True   # carried the R19 mark
    assert sup.dispatch_play_recorded(999) is True      # unknown → safe default

    # An outage emission retires the dispatch but its count-state stays
    # readable for the classifier's hold mark.
    token3 = sup.on_dispatched(make_track("t3"))
    sup.on_outage_reported("connection_lost")
    assert sup.dispatch_play_recorded(token3) is False


async def test_outage_report_with_no_dispatch_still_notifies(monkeypatch):
    """on_outage_reported with no live dispatch (deadline already retired it)
    still reaches listeners — token -1, track None."""
    sup, timers, rec = _fresh(monkeypatch)
    outages = []
    sup.add_outage_listener(lambda token, track, reason: outages.append((token, track, reason)))
    sup.on_outage_reported("poll_errors")
    assert outages == [(-1, None, "poll_errors")]


async def test_outage_report_retires_dispatch_and_cancels_timer(monkeypatch):
    """A reported outage retires the live dispatch exactly like a deadline
    emission: its timer dies and a late confirmation cannot count."""
    sup, timers, rec = _fresh(monkeypatch)
    outages = []
    sup.add_outage_listener(lambda *a: outages.append(a))
    token = sup.on_dispatched(make_track("t1"))
    sup.on_outage_reported("connection_lost")
    assert len(outages) == 1
    assert timers.timers[0].cancelled
    assert sup.current_token() is None
    sup.on_playback_confirmed(token)              # too late — retired
    rec.assert_not_called()


async def test_get_supervisor_registers_classifier(monkeypatch):
    """Production wiring: the lazy singleton carries the U2 classifier as an
    outage listener."""
    from app.output import session
    monkeypatch.setattr(session, "_supervisor", None)
    sup = session.get_supervisor()
    assert session.classify_outage in sup._outage_listeners


def _fresh(monkeypatch):
    """A fresh supervisor installed as the module singleton with the hold
    flag reset — the classifier resolves the singleton via get_supervisor()."""
    from app.output import session
    timers = FakeTimerFactory()
    rec = MagicMock()
    sup = OutputSessionSupervisor(record_play=rec, timer_factory=timers)
    monkeypatch.setattr(session, "_supervisor", sup)
    monkeypatch.setattr(hold, "_output_hold", False)
    monkeypatch.setattr(hold, "_output_hold_reason", "")
    return sup, timers, rec


# ── U3: reconnect loop and auto-resume ────────────────────────────────────────
# Every trigger, clock and window read is injected: FakeTimerFactory drives the
# backoff schedule, FakeClock drives the resume window and flap guard, and the
# window/identity/track hooks are AsyncMocks — zero real sleeps.

from types import SimpleNamespace

from app.output.base import DeviceNotReadyError
from app.output.session import (
    RETRY_BACKOFF_CAP_S,
    RETRY_BACKOFF_START_S,
    STATE_IDLE_PAUSED,
    STATE_OUTAGE_PAUSED,
    STATE_PAUSED,
    STATE_PLAYING,
)


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, s: float) -> None:
        self.now += s


class FakeResumeBackend:
    """Duck-typed backend for the resume tests: attach via set_device,
    position via get_position (the Direct/AirPlay capture path), seek and
    volume as AsyncMocks. Plain class — no MagicMock auto-attrs, so the
    supervisor's hasattr guards behave exactly as against real backends."""

    def __init__(self, device_id="dev-1", position_ms=42_000):
        self._device_id = device_id
        self._volume = 0.5
        self._position_ms = position_ms
        self.set_device = AsyncMock()
        self.seek = AsyncMock()
        self.set_volume = AsyncMock()

    async def get_position(self) -> int:
        return self._position_ms

    @property
    def is_playing(self) -> bool:
        return False


class NoSeekBackend:
    """AE4 shape: a backend with no seek/resume_seek at all."""

    def __init__(self, device_id="dev-1"):
        self._device_id = device_id
        self._volume = 0.5
        self.set_device = AsyncMock()

    async def get_position(self) -> int:
        return 42_000

    @property
    def is_playing(self) -> bool:
        return False


def _fresh_u3(monkeypatch, **kw):
    """Fresh supervisor with every U3 seam injected, installed as the module
    singleton (enter_output_hold / clear_output_hold resolve it there)."""
    from app.output import session
    timers = FakeTimerFactory()
    clock = FakeClock()
    rec = MagicMock()
    sup = OutputSessionSupervisor(
        record_play=rec, timer_factory=timers, clock=clock,
        window_minutes=kw.pop("window_minutes", AsyncMock(return_value=60)),
        identity_check=kw.pop("identity_check", AsyncMock(return_value=True)),
        held_track_check=kw.pop("held_track_check",
                                AsyncMock(return_value=True)),
        **kw,
    )
    monkeypatch.setattr(session, "_supervisor", sup)
    monkeypatch.setattr(hold, "_output_hold", False)
    monkeypatch.setattr(hold, "_output_hold_reason", "")
    return sup, timers, rec, clock


def _wire_resume(stack, backend, qe=None):
    """Patch app.state's resume surfaces: real QueueEngine, fake router
    carrying `backend` as active, stubbed stream URL/client/broadcast."""
    import app.state as st
    from app.queue.engine import QueueEngine
    if qe is None:
        qe = QueueEngine()
    router = SimpleNamespace(active=backend, play=AsyncMock(),
                             stop=AsyncMock(), pause=AsyncMock(),
                             resume=AsyncMock(),
                             # PLX-1: dispatch_play deposits the holder key
                             # on the router's EFFECTIVE backend.
                             effective_backend=lambda: backend)
    stack.enter_context(patch.object(st, "queue_engine", qe))
    stack.enter_context(patch("app.queue.engine.database.save_queue", AsyncMock()))
    stack.enter_context(patch("app.queue.engine.database.save_history", AsyncMock()))
    stack.enter_context(patch.object(st, "output_router", router))
    stack.enter_context(patch.object(
        st, "get_plex_client", AsyncMock(return_value=MagicMock())))
    stack.enter_context(patch.object(
        st, "_make_stream_url", lambda key, client: f"http://stream/{key}"))
    stack.enter_context(patch.object(st, "_emit_track_skipped", AsyncMock()))
    stack.enter_context(patch.object(st, "_do_advance", AsyncMock()))
    stack.enter_context(patch.object(st, "_closing_active", False))
    stack.enter_context(patch("app.events.bus.manager.broadcast_to_admins",
                              AsyncMock()))
    return qe, router


def _last_timer(timers):
    return timers.timers[-1]


async def test_ae2_reattach_at_backoff_tick3_seeks_and_never_recounts(monkeypatch):
    """AE2: device returns at backoff tick 3 → re-attach via set_device, the
    held item re-dispatches carrying its R19 mark, seeks to the held
    position, and the play is NOT counted twice. Backoff grows 5→10→20."""
    import app.state as st
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(monkeypatch)
    backend = FakeResumeBackend()
    backend.set_device = AsyncMock(side_effect=[RuntimeError("down"),
                                                RuntimeError("down"), None])
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        stack.enter_context(patch.object(st, "dlna_backend", backend))
        item = await _start_playing(qe, make_track("t1"))
        token = sup.on_dispatched(item.track)
        sup.on_playback_confirmed(token)         # the play counted once
        rec.assert_called_once()

        await session.enter_output_hold("connection_lost", play_recorded=True)
        assert session.output_hold_active() is True
        assert qe.queue[0].play_recorded is True
        assert _last_timer(timers).delay == RETRY_BACKOFF_START_S

        _last_timer(timers).fire()               # tick 1 → fail
        await _drain()
        assert _last_timer(timers).delay == 10.0
        router.play.assert_not_awaited()
        _last_timer(timers).fire()               # tick 2 → fail
        await _drain()
        assert _last_timer(timers).delay == 20.0
        _last_timer(timers).fire()               # tick 3 → device is back
        await _drain()

        router.play.assert_awaited_once()        # held item re-dispatched
        backend.seek.assert_awaited_once_with(42_000)
        assert session.output_hold_active() is False
        assert sup.session_state == STATE_PLAYING
        # R19: the resume dispatch carried the mark — confirming it must not
        # re-count, and the mark was consumed for future organic replays.
        sup.on_playback_confirmed(sup.current_token())
        rec.assert_called_once()
        assert qe.state.current.play_recorded is False


async def test_ae3_window_expired_lands_idle_paused_manual_resume_recovers(monkeypatch):
    """AE3: device returns after the window → attached but IdlePaused (no
    audio); manual resume plays from the held position."""
    import app.state as st
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(
        monkeypatch, window_minutes=AsyncMock(return_value=60))
    backend = FakeResumeBackend()
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        stack.enter_context(patch.object(st, "dlna_backend", backend))
        await _start_playing(qe, make_track("t1"))
        await session.enter_output_hold("connection_lost")

        clock.advance(61 * 60)                   # past the 60-minute window
        _last_timer(timers).fire()               # attach succeeds…
        await _drain()

        router.play.assert_not_awaited()         # …but NO audio after expiry
        assert sup.session_state == STATE_IDLE_PAUSED
        assert sup.idle_paused_reason == "window_expired"
        assert session.output_hold_active() is True

        ok = await sup.manual_resume()           # manual resume recovers
        assert ok is True
        router.play.assert_awaited_once()
        backend.seek.assert_awaited_once_with(42_000)
        assert session.output_hold_active() is False
        assert sup.session_state == STATE_PLAYING


async def test_ae4_backend_without_seek_restarts_from_top(monkeypatch):
    """AE4: a backend with no reliable seek support restarts the held track
    from the top — resume still lands, nothing raises."""
    import app.state as st
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(monkeypatch)
    backend = NoSeekBackend()
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        stack.enter_context(patch.object(st, "dlna_backend", backend))
        await _start_playing(qe, make_track("t1"))
        await session.enter_output_hold("connection_lost")

        _last_timer(timers).fire()
        await _drain()

        router.play.assert_awaited_once()        # restart-from-top happened
        assert sup.session_state == STATE_PLAYING
        assert session.output_hold_active() is False


async def test_window_expiry_races_inflight_attempt_no_audio(monkeypatch):
    """The window expires WHILE an attach attempt blocks: the single
    authoritative check at audio-start (under _advance_lock) decides — no
    audio plays after expiry."""
    import app.state as st
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(monkeypatch)
    backend = FakeResumeBackend()
    gate = asyncio.Event()

    async def slow_set_device(device_id):
        await gate.wait()

    backend.set_device = slow_set_device
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        stack.enter_context(patch.object(st, "dlna_backend", backend))
        await _start_playing(qe, make_track("t1"))
        await session.enter_output_hold("connection_lost")

        _last_timer(timers).fire()               # attempt parks on the gate
        await _drain()
        clock.advance(2 * 3600)                  # window expires mid-attach
        gate.set()                               # attach finally returns OK
        await _drain()

        router.play.assert_not_awaited()
        assert sup.session_state == STATE_IDLE_PAUSED
        assert sup.idle_paused_reason == "window_expired"
        assert session.output_hold_active() is True


async def test_was_paused_session_reattaches_silent(monkeypatch):
    """R17: a session deliberately paused before the outage re-attaches
    PAUSED and silent — never auto-plays; the held position applies at the
    eventual manual resume."""
    import app.state as st
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(monkeypatch)
    backend = FakeResumeBackend()
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        stack.enter_context(patch.object(st, "dlna_backend", backend))
        await _start_playing(qe, make_track("t1"))
        await qe.set_paused(True)                # user paused before the outage
        await session.enter_output_hold("connection_lost")

        _last_timer(timers).fire()               # device returns
        await _drain()

        router.play.assert_not_awaited()         # attached, SILENT
        assert sup.session_state == STATE_PAUSED
        assert sup.was_paused is True
        assert session.output_hold_active() is True

        ok = await sup.manual_resume()           # position applies now
        assert ok is True
        router.play.assert_awaited_once()
        backend.seek.assert_awaited_once_with(42_000)


async def test_skip_during_reconnecting_resumes_new_front_at_zero(monkeypatch):
    """A skip during the outage bumps _advance_gen: the resume targets the
    CURRENT queue front from 0:00 — the captured position belonged to a
    superseded track."""
    import app.state as st
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(monkeypatch)
    backend = FakeResumeBackend()
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        stack.enter_context(patch.object(st, "dlna_backend", backend))
        await _start_playing(qe, make_track("t1"))
        await qe.append(make_track("t2"))
        await session.enter_output_hold("connection_lost")

        st._advance_gen += 1                     # admin skip while held (U4
        await qe.advance()                       # moves the held pointer)

        _last_timer(timers).fire()
        await _drain()

        router.play.assert_awaited_once()
        backend.seek.assert_not_awaited()        # 0:00 — no seek
        assert sup.session_state == STATE_PLAYING


async def test_device_switch_during_hold_cancels_retry_loop_atomically(monkeypatch):
    """R17: a manual switch retires the old device's retry loop atomically —
    a late backoff tick must not re-attach the old device. The hold itself
    clears only after the switch succeeds (activate_backend's job)."""
    import app.state as st
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(monkeypatch)
    backend = FakeResumeBackend()
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        stack.enter_context(patch.object(st, "dlna_backend", backend))
        await _start_playing(qe, make_track("t1"))
        await session.enter_output_hold("connection_lost")
        retry = _last_timer(timers)
        epoch_before = sup.attach_epoch

        session.notify_manual_switch()

        assert sup.attach_epoch == epoch_before + 1
        assert sup._outage is None
        assert retry.cancelled is True
        retry.cb()                               # late tick racing the cancel
        await _drain()
        backend.set_device.assert_not_awaited()  # no late re-attach
        assert session.output_hold_active() is True  # cleared by the switch,
        session.clear_output_hold()                  # not by the cancel
        assert session.output_hold_active() is False


async def test_backoff_success_and_connected_race_single_flight(monkeypatch):
    """A backoff attempt in flight and a Cast CONNECTED trigger racing it →
    exactly one attach runs and exactly one resume dispatches; the loser
    no-ops."""
    import app.state as st
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(monkeypatch)
    backend = FakeResumeBackend()
    gate = asyncio.Event()
    attaches = []

    async def slow_set_device(device_id):
        attaches.append(device_id)
        await gate.wait()

    backend.set_device = slow_set_device
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        stack.enter_context(patch.object(st, "dlna_backend", backend))
        await _start_playing(qe, make_track("t1"))
        await session.enter_output_hold("connection_lost")

        _last_timer(timers).fire()               # backoff attempt in flight
        await _drain()
        session.notify_reconnect_trigger("cast_connected")  # racing trigger
        await _drain()
        gate.set()
        await _drain()

        assert attaches == ["dev-1"]             # ONE attach attempt
        router.play.assert_awaited_once()        # ONE resume dispatched
        assert session.output_hold_active() is False


async def test_manual_switch_while_attach_blocked_discards_stale_result(monkeypatch):
    """A manual switch while an attach blocks in the executor: the post-return
    epoch check discards the stale result — no cache write-back, no resume,
    no state commit for the old device."""
    import app.state as st
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(monkeypatch)
    backend = FakeResumeBackend()
    backend._resolved_host = "10.0.0.9"          # write-back inputs present…
    backend._resolved_port = 8009
    backend._resolved_name = "Old Device"
    backend.register_resolved = MagicMock()
    gate = asyncio.Event()

    async def slow_set_device(device_id):
        await gate.wait()

    backend.set_device = slow_set_device
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        stack.enter_context(patch.object(st, "dlna_backend", backend))
        await _start_playing(qe, make_track("t1"))
        await session.enter_output_hold("connection_lost")

        _last_timer(timers).fire()               # attach blocks on the gate
        await _drain()
        session.notify_manual_switch()           # epoch bump mid-attach
        gate.set()                               # stale attach returns "OK"
        await _drain()

        backend.register_resolved.assert_not_called()  # …but never written
        router.play.assert_not_awaited()
        assert sup.session_state != STATE_PLAYING


async def test_successful_reattach_writes_back_resolved_address(monkeypatch):
    """Write-back positive path (KTD): a successful re-attach whose backend
    carries the resolved-address fields registers them through
    register_resolved so the watcher registry and backend caches converge —
    resolved name/host/port, uuid None for a non-UUID device id, empty TXT."""
    import app.state as st
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(monkeypatch)
    backend = FakeResumeBackend()
    backend._resolved_host = "10.0.0.9"
    backend._resolved_port = 8009
    backend._resolved_name = "Living Room"
    backend.register_resolved = MagicMock()
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        stack.enter_context(patch.object(st, "dlna_backend", backend))
        await _start_playing(qe, make_track("t1"))
        await session.enter_output_hold("connection_lost")

        _last_timer(timers).fire()               # backoff tick → attach OK
        await _drain()

        router.play.assert_awaited_once()        # the re-attach resumed
        backend.register_resolved.assert_called_once_with(
            "Living Room", "10.0.0.9", 8009, None, {})


async def test_flap_guard_three_short_lived_resumes_hold_for_manual(monkeypatch):
    """Flap guard: three auto-resumes each followed by an outage within the
    short-lived threshold → the next re-attach lands IdlePaused with the
    distinct flap_guard reason instead of auto-playing."""
    import app.state as st
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(monkeypatch)
    backend = FakeResumeBackend()
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        stack.enter_context(patch.object(st, "dlna_backend", backend))
        await _start_playing(qe, make_track("t1"))
        for i in range(3):
            await session.enter_output_hold("connection_lost")
            _last_timer(timers).fire()           # immediate re-attach…
            await _drain()
            assert router.play.await_count == i + 1  # …auto-resumed
            assert session.output_hold_active() is False
            clock.advance(10.0)                  # dies again 10s later

        await session.enter_output_hold("connection_lost")  # 4th outage
        _last_timer(timers).fire()
        await _drain()

        assert router.play.await_count == 3      # NO 4th auto-resume
        assert sup.session_state == STATE_IDLE_PAUSED
        assert sup.idle_paused_reason == "flap_guard"
        assert session.output_hold_active() is True
        ok = await sup.manual_resume()           # manual recovers
        assert ok is True
        assert router.play.await_count == 4


async def test_dhcp_wrong_device_identity_check_keeps_retrying(monkeypatch):
    """DHCP reuse: the cached address answers but the identity check says it
    is a DIFFERENT device → treated as a failed attempt, backoff re-armed,
    no resume."""
    import app.state as st
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(
        monkeypatch, identity_check=AsyncMock(return_value=False))
    backend = FakeResumeBackend()
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        stack.enter_context(patch.object(st, "dlna_backend", backend))
        await _start_playing(qe, make_track("t1"))
        await session.enter_output_hold("connection_lost")

        _last_timer(timers).fire()               # set_device "succeeds"
        await _drain()

        router.play.assert_not_awaited()
        assert sup.session_state == STATE_OUTAGE_PAUSED
        assert session.output_hold_active() is True
        assert _last_timer(timers).delay == 10.0  # retry continues (grown)


# ── _default_identity_check: the production DHCP-reuse guard ─────────────────
# Evidence-based and fail-open: only a verifiable identity MISMATCH blocks the
# resume; absent evidence (no _cast/_dmr, non-UUID/USN ids, other backends)
# reads True.

async def test_identity_check_chromecast_uuid_match():
    from app.output.session import _default_identity_check
    device_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeffff"
    backend = SimpleNamespace(_cast=SimpleNamespace(uuid=device_id))
    assert await _default_identity_check(backend, "chromecast", device_id) is True


async def test_identity_check_chromecast_uuid_mismatch():
    from app.output.session import _default_identity_check
    backend = SimpleNamespace(_cast=SimpleNamespace(
        uuid="11111111-2222-3333-4444-555555555555"))
    assert await _default_identity_check(
        backend, "chromecast", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeffff") is False


async def test_identity_check_dlna_usn_prefix_match():
    from app.output.session import _default_identity_check
    backend = SimpleNamespace(_dmr=SimpleNamespace(
        device=SimpleNamespace(udn="uuid:abc")))
    assert await _default_identity_check(
        backend, "dlna",
        "uuid:abc::urn:schemas-upnp-org:device:MediaRenderer:1") is True


async def test_identity_check_dlna_udn_mismatch():
    from app.output.session import _default_identity_check
    backend = SimpleNamespace(_dmr=SimpleNamespace(
        device=SimpleNamespace(udn="uuid:xyz")))
    assert await _default_identity_check(
        backend, "dlna",
        "uuid:abc::urn:schemas-upnp-org:device:MediaRenderer:1") is False


async def test_identity_check_no_identity_fields_fails_open():
    from app.output.session import _default_identity_check
    bare = SimpleNamespace()                     # no _cast / _dmr at all
    assert await _default_identity_check(bare, "chromecast", "dev-1") is True
    assert await _default_identity_check(bare, "dlna", "uuid:abc::urn:x") is True
    assert await _default_identity_check(bare, "airplay", "dev-1") is True


async def test_closing_time_active_blocks_auto_resume(monkeypatch):
    """R21: Closing Time active at re-attach → device attached, no
    auto-resume; the landing is IdlePaused with the closing_time reason."""
    import app.state as st
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(monkeypatch)
    backend = FakeResumeBackend()
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        stack.enter_context(patch.object(st, "dlna_backend", backend))
        stack.enter_context(patch.object(st, "_closing_active", True))
        await _start_playing(qe, make_track("t1"))
        await session.enter_output_hold("connection_lost")

        _last_timer(timers).fire()
        await _drain()

        router.play.assert_not_awaited()
        assert sup.session_state == STATE_IDLE_PAUSED
        assert sup.idle_paused_reason == "closing_time"
        assert session.output_hold_active() is True


async def test_discovery_arrival_short_circuits_backoff_immediately(monkeypatch):
    """Discovery arrival for the held device triggers an immediate attempt —
    a pending (up to 300s) backoff wait is cancelled, the watcher listener
    is deregistered once attached."""
    import app.state as st
    from app.output import session, watcher as watcher_mod
    sup, timers, rec, clock = _fresh_u3(monkeypatch)

    class FakeWatcher:
        def __init__(self):
            self.listeners = {}

        def add_arrival_listener(self, backend, device_id, cb):
            self.listeners[(backend, device_id)] = cb

        def remove_arrival_listener(self, backend, device_id, cb):
            self.listeners.pop((backend, device_id), None)

    fw = FakeWatcher()
    monkeypatch.setattr(watcher_mod, "_watcher", fw)
    backend = FakeResumeBackend()
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        stack.enter_context(patch.object(st, "dlna_backend", backend))
        await _start_playing(qe, make_track("t1"))
        await session.enter_output_hold("connection_lost")

        retry = _last_timer(timers)
        assert retry.delay == RETRY_BACKOFF_START_S
        cb = fw.listeners[("dlna", "dev-1")]     # registered at outage entry
        cb()                                     # device announces itself
        await _drain()

        assert retry.cancelled is True           # backoff wait short-circuited
        router.play.assert_awaited_once()
        assert session.output_hold_active() is False
        assert ("dlna", "dev-1") not in fw.listeners  # deregistered


async def test_resume_dispatch_device_lost_reholds_and_rearms(monkeypatch):
    """The device dies again between attach and audio: the popped item is
    re-held with its R19 mark intact, the retry loop re-arms, and the SAME
    outage entry keeps counting toward the window (per-outage-entry, R8)."""
    import app.state as st
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(monkeypatch)
    backend = FakeResumeBackend()
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        stack.enter_context(patch.object(st, "dlna_backend", backend))
        router.play.side_effect = DeviceNotReadyError("gone again")
        await _start_playing(qe, make_track("t1"))
        entered = clock.now
        await session.enter_output_hold("connection_lost", play_recorded=True)

        _last_timer(timers).fire()               # attach OK, dispatch dies
        await _drain()

        assert session.output_hold_active() is True
        assert [i.track_id for i in qe.queue] == ["t1"]  # re-held
        assert qe.queue[0].play_recorded is True         # mark intact
        assert sup.session_state == STATE_OUTAGE_PAUSED
        assert sup._outage is not None
        assert sup._outage.entered_at == entered         # same window
        assert _last_timer(timers).delay == 10.0         # retry re-armed


async def test_manual_resume_unreachable_device_returns_false(monkeypatch):
    """Manual resume while the device is still gone: the attach fails, the
    hold stays, and the caller gets False (the endpoint 409s)."""
    import app.state as st
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(monkeypatch)
    backend = FakeResumeBackend()
    backend.set_device = AsyncMock(side_effect=RuntimeError("still down"))
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        stack.enter_context(patch.object(st, "dlna_backend", backend))
        await _start_playing(qe, make_track("t1"))
        await session.enter_output_hold("connection_lost")

        ok = await sup.manual_resume()

        assert ok is False
        router.play.assert_not_awaited()
        assert session.output_hold_active() is True


async def test_manual_resume_without_context_releases_hold_and_advances(monkeypatch):
    """A hold with no reconnect context (no addressable device at outage
    entry): manual resume releases the hold and hands the queue front to the
    advance authority."""
    import app.state as st
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(monkeypatch)
    monkeypatch.setattr(hold, "_output_hold", True)
    monkeypatch.setattr(hold, "_output_hold_reason", "connection_lost")
    advance = AsyncMock()
    with patch.object(st, "_do_advance", advance):
        ok = await sup.manual_resume()
    assert ok is True
    assert session.output_hold_active() is False
    advance.assert_awaited_once()


async def test_queue_cleared_during_outage_resume_lands_idle(monkeypatch):
    """Queue cleared while held: the resume finds nothing to play — the hold
    releases and the session lands idle (no orphan resume later)."""
    import app.state as st
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(monkeypatch)
    backend = FakeResumeBackend()
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        stack.enter_context(patch.object(st, "dlna_backend", backend))
        await _start_playing(qe, make_track("t1"))
        await session.enter_output_hold("connection_lost")
        await qe.clear()                         # admin clears the queue

        _last_timer(timers).fire()
        await _drain()

        router.play.assert_not_awaited()
        assert session.output_hold_active() is False
        assert sup.session_state == session.STATE_IDLE


async def test_held_track_identity_invalid_lands_idle_paused(monkeypatch):
    """A rescan re-minted ids mid-outage: the held track fails validation →
    IdlePaused with the track_identity reason (no blind dispatch of a stale
    id); manual resume still recovers (holder fallback self-corrects)."""
    import app.state as st
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(
        monkeypatch, held_track_check=AsyncMock(return_value=False))
    backend = FakeResumeBackend()
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        stack.enter_context(patch.object(st, "dlna_backend", backend))
        await _start_playing(qe, make_track("t1"))
        await session.enter_output_hold("connection_lost")

        _last_timer(timers).fire()
        await _drain()

        router.play.assert_not_awaited()
        assert sup.session_state == STATE_IDLE_PAUSED
        assert sup.idle_paused_reason == "track_identity"
        assert session.output_hold_active() is True

        ok = await sup.manual_resume()           # manual bypasses the check
        assert ok is True
        router.play.assert_awaited_once()


async def test_no_media_source_at_resume_lands_idle_paused(monkeypatch):
    """Attach succeeds but no media source is connected (get_plex_client
    None): the resume declines with the no_media_source IdlePaused landing
    and the hold stays — the held queue is not consumed."""
    import app.state as st
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(monkeypatch)
    backend = FakeResumeBackend()
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        stack.enter_context(patch.object(st, "dlna_backend", backend))
        stack.enter_context(patch.object(
            st, "get_plex_client", AsyncMock(return_value=None)))
        await _start_playing(qe, make_track("t1"))
        await session.enter_output_hold("connection_lost")

        _last_timer(timers).fire()               # attach OK…
        await _drain()

        router.play.assert_not_awaited()         # …but no audio dispatched
        assert sup.session_state == STATE_IDLE_PAUSED
        assert sup.idle_paused_reason == "no_media_source"
        assert session.output_hold_active() is True


async def test_set_held_volume_persists_and_applies_at_reattach(monkeypatch):
    """R17: a volume change during the hold is accepted + persisted and
    applied to the device at re-attach, before any audio starts."""
    import app.state as st
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(monkeypatch)
    backend = FakeResumeBackend()
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        stack.enter_context(patch.object(st, "dlna_backend", backend))
        set_setting = AsyncMock()
        stack.enter_context(patch("app.database.set_setting", set_setting))
        await _start_playing(qe, make_track("t1"))
        await session.enter_output_hold("connection_lost")

        await session.set_held_volume(0.7)
        assert sup._outage.pending_volume == 0.7
        assert backend._volume == 0.7
        set_setting.assert_awaited_once_with("vol:dlna:dev-1", "0.7")

        _last_timer(timers).fire()               # re-attach
        await _drain()
        backend.set_volume.assert_awaited_once_with(0.7)  # applied pre-audio
        router.play.assert_awaited_once()


async def test_track_level_resume_failure_skips_and_advances(monkeypatch):
    """All holders fail on the re-attached (live) device: today's track-level
    skip — TrackSkippedEvent + advance — never a dead-end."""
    import app.state as st
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(monkeypatch)
    backend = FakeResumeBackend()
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        stack.enter_context(patch.object(st, "dlna_backend", backend))
        router.play.side_effect = Exception("404")
        skipped = AsyncMock()
        advance = AsyncMock()
        stack.enter_context(patch.object(st, "_emit_track_skipped", skipped))
        stack.enter_context(patch.object(st, "_do_advance", advance))
        item = await _start_playing(qe, make_track("t1"))
        await session.enter_output_hold("connection_lost")

        _last_timer(timers).fire()
        await _drain()

        assert session.output_hold_active() is False
        skipped.assert_awaited_once()
        assert skipped.await_args.args[0] is item.track
        advance.assert_awaited_once()


async def test_cast_backoff_only_probes_while_socket_client_alive(monkeypatch):
    """Chromecast: while the cast object (socket client) is alive, a backoff
    tick only probes — no set_device rebuild; a probe reading connected IS
    the attach and the resume proceeds."""
    import app.state as st
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(monkeypatch)
    backend = FakeResumeBackend()
    backend._cast = object()                     # live socket client object
    backend._pos_snapshot_ms = 33_000            # Cast position capture path
    backend.probe_liveness = AsyncMock(side_effect=[(False, None),
                                                    (True, "PLAYING")])
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        stack.enter_context(patch.object(st, "chromecast_backend", backend))
        await _start_playing(qe, make_track("t1"))
        await session.enter_output_hold("connection_lost")

        _last_timer(timers).fire()               # probe says still gone
        await _drain()
        backend.set_device.assert_not_awaited()  # probe only, no rebuild
        assert session.output_hold_active() is True

        _last_timer(timers).fire()               # probe: reconnected
        await _drain()
        backend.set_device.assert_not_awaited()
        router.play.assert_awaited_once()        # rebuild-by-re-LOAD + resume
        backend.seek.assert_awaited_once_with(33_000)
        assert session.output_hold_active() is False


async def test_seed_and_set_device_seeds_cache_from_persisted_address(monkeypatch):
    """The generalized _startup_reconnect mechanic: a backoff attach seeds the
    backend's address cache from the persisted output_addr:{device_id} blob
    BEFORE set_device is awaited — and via setdefault, so a FRESHER entry a
    discovery arrival registered mid-outage is never clobbered by the stale
    persisted one."""
    import app.state as st
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(monkeypatch)
    backend = FakeResumeBackend()
    backend._dbus_index = {}                     # chromecast cache shape
    seen_at_set_device = []

    async def set_device(device_id):
        seen_at_set_device.append(dict(backend._dbus_index))
        if len(seen_at_set_device) == 1:
            raise RuntimeError("still down")     # attempt 1 fails post-seed

    backend.set_device = set_device
    get_setting = AsyncMock(
        return_value='{"host": "192.168.1.50", "port": 8009, "name": "X"}')
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        stack.enter_context(patch.object(st, "chromecast_backend", backend))
        stack.enter_context(patch("app.database.get_setting", get_setting))
        await _start_playing(qe, make_track("t1"))
        await session.enter_output_hold("connection_lost")

        _last_timer(timers).fire()               # attempt 1: seed, then fail
        await _drain()
        get_setting.assert_awaited_with("output_addr:dev-1")
        # The cache was seeded BEFORE set_device saw it.
        assert seen_at_set_device[0]["dev-1"] == ("X", "192.168.1.50", 8009)

        # A discovery arrival registers a FRESHER address mid-outage…
        backend._dbus_index["dev-1"] = ("X", "192.168.1.99", 8009)
        _last_timer(timers).fire()               # attempt 2: attach OK
        await _drain()
        # …and setdefault leaves it alone — the stale persisted blob lost.
        assert seen_at_set_device[1]["dev-1"] == ("X", "192.168.1.99", 8009)
        router.play.assert_awaited_once()


async def test_get_resume_window_minutes_accessor(tmp_path, monkeypatch):
    """R8 accessor: default 60, stored value honored, floor clamps to 1,
    junk falls back to the default."""
    database = await _play_db(tmp_path, monkeypatch)
    try:
        assert await database.get_resume_window_minutes() == 60
        await database.set_setting("resume_window_minutes", "15")
        assert await database.get_resume_window_minutes() == 15
        await database.set_setting("resume_window_minutes", "0")
        assert await database.get_resume_window_minutes() == 1
        await database.set_setting("resume_window_minutes", "junk")
        assert await database.get_resume_window_minutes() == 60
    finally:
        await database.close_db()


async def test_all_device_level_reasons_hold_without_probing(monkeypatch):
    """Every pre-classified device-level reason (its reporter already
    established unreachability) holds WITHOUT consulting the probe — the
    classifier must not second-guess a Cast watchdog probe or DLNA's three
    failed transport polls."""
    from app.output import session
    for reason in sorted(session.DEVICE_LEVEL_REASONS):
        sup, timers, rec = _fresh(monkeypatch)
        sup.add_outage_listener(session.classify_outage)
        probe = AsyncMock(return_value=(True, "PLAYING"))  # would say reachable
        with contextlib.ExitStack() as stack:
            qe, skipped, advance = _wire_state(stack, probe=probe)
            item = await _start_playing(qe, make_track("t1"))
            token = sup.on_dispatched(item.track)
            sup.on_playback_confirmed(token)
            sup.on_outage_reported(reason)
            await _drain()

            assert session.output_hold_active() is True, reason
            probe.assert_not_awaited()
            skipped.assert_not_called()
            advance.assert_not_called()

# ── U4: outage-state observability (R20) ──────────────────────────────────────
# OutputSessionEvent emissions + the session_snapshot / session_snapshot_admin
# GET mirrors: admin-rich and guest-lean payloads share the same state truth,
# and every WS delta is refetchable via the snapshot (resync contract).

from app.events.types import OutputSessionEvent
from app.output.session import STATE_IDLE, STATE_RECONNECTING


def _capture_broadcasts(stack):
    """Patch BOTH broadcast channels and return the mocks (entered after
    _wire_resume, so these supersede its blanket admin patch)."""
    admins = AsyncMock()
    guests = AsyncMock()
    stack.enter_context(patch("app.events.bus.manager.broadcast_to_admins", admins))
    stack.enter_context(patch("app.events.bus.manager.broadcast_to_guests", guests))
    return admins, guests


def _session_events(mock):
    return [c.args[0] for c in mock.await_args_list
            if isinstance(c.args[0], OutputSessionEvent)]


async def test_hold_entered_emits_admin_rich_and_guest_lean(monkeypatch):
    """U4 scenario (a), event half: hold entered -> the admin event carries
    reason + device + retry countdown + window remaining; the guest event is
    LEAN (same state truth, no outage detail) — the TrackSkippedEvent
    dual-broadcast pattern. This replaces U2's placeholder OutputChangedEvent
    toast; no output_changed error frame is broadcast anymore."""
    import app.state as st
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(
        monkeypatch, window_minutes=AsyncMock(return_value=60))
    backend = FakeResumeBackend()
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        admins, guests = _capture_broadcasts(stack)
        stack.enter_context(patch.object(st, "dlna_backend", backend))
        await _start_playing(qe, make_track("t1"))

        await session.enter_output_hold("connection_lost")

        aev, gev = _session_events(admins), _session_events(guests)
        assert len(aev) == 1 and len(gev) == 1
        a, g = aev[0], gev[0]
        assert a.type == "output_session"
        assert a.state == STATE_OUTAGE_PAUSED and a.held is True
        assert a.reason == "connection_lost"
        assert a.backend_type == "dlna" and a.device_id == "dev-1"
        assert a.attempts == 0
        assert a.next_retry_s == RETRY_BACKOFF_START_S
        assert a.window_remaining_s == 60 * 60
        assert a.was_paused is False and a.flap_tripped is False
        # Guest lean: SAME state truth, admin detail withheld (None).
        assert g.state == STATE_OUTAGE_PAUSED and g.held is True
        assert g.reason is None and g.device_id is None
        assert g.attempts is None and g.next_retry_s is None
        assert g.window_remaining_s is None and g.was_paused is None
        # The U2 placeholder toast is gone — no output_changed error frame.
        assert not [c for c in admins.await_args_list
                    if getattr(c.args[0], "type", "") == "output_changed"]


async def test_snapshots_mirror_event_fields_admin_rich_guest_lean(monkeypatch):
    """U4 scenario (a), GET half: both snapshots carry the SAME state truth;
    the admin snapshot adds the outage detail and its window countdown
    tracks the clock (refetchable per the WS/GET resync contract)."""
    import app.state as st
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(
        monkeypatch, window_minutes=AsyncMock(return_value=60))
    backend = FakeResumeBackend()
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        stack.enter_context(patch.object(st, "dlna_backend", backend))
        await _start_playing(qe, make_track("t1"))
        await session.enter_output_hold("connection_lost")

        lean = session.session_snapshot()
        assert lean == {"state": STATE_OUTAGE_PAUSED, "held": True,
                        "gapless_flow_active": False, "source_lock": None}

        adm = await session.session_snapshot_admin()
        assert adm["state"] == STATE_OUTAGE_PAUSED and adm["held"] is True
        assert adm["reason"] == "connection_lost"
        assert adm["backend_type"] == "dlna" and adm["device_id"] == "dev-1"
        assert adm["attempts"] == 0
        assert adm["next_retry_s"] == RETRY_BACKOFF_START_S
        assert adm["window_remaining_s"] == 3600
        clock.advance(600)
        adm2 = await session.session_snapshot_admin()
        assert adm2["window_remaining_s"] == 3000   # counts down with the clock


async def test_snapshot_admin_never_raises_without_outage_context():
    """The admin snapshot degrades to None detail (never raises) when there is
    no outage context — GET endpoints depend on it being safe."""
    from app.output import session
    adm = await session.session_snapshot_admin()
    assert adm["state"] in (STATE_IDLE, STATE_PLAYING, STATE_OUTAGE_PAUSED,
                            STATE_PAUSED, STATE_IDLE_PAUSED, STATE_RECONNECTING)
    for key in ("reason", "backend_type", "device_id", "attempts",
                "next_retry_s", "window_remaining_s", "flap_tripped"):
        assert key in adm


async def test_snapshots_carry_gapless_flow_active(monkeypatch):
    """U10 observability (2026-07-12 review C13): both snapshots carry
    ``gapless_flow_active`` — False with no live flow session, True while
    the flow registry holds an open one (the liveness check only; the
    session id is the flow route's capability credential and never rides
    a snapshot)."""
    from app.output import flow, session
    monkeypatch.setattr(flow, "_current_session", None)
    assert session.session_snapshot()["gapless_flow_active"] is False
    adm = await session.session_snapshot_admin()
    assert adm["gapless_flow_active"] is False

    monkeypatch.setattr(flow, "_current_session",
                        SimpleNamespace(closed=False))
    assert session.session_snapshot()["gapless_flow_active"] is True
    adm = await session.session_snapshot_admin()
    assert adm["gapless_flow_active"] is True
    # A closed leftover in the registry is NOT a live flow.
    monkeypatch.setattr(flow, "_current_session",
                        SimpleNamespace(closed=True))
    assert session.session_snapshot()["gapless_flow_active"] is False


async def test_reconnect_attempt_progress_events(monkeypatch):
    """U4 emission cadence: one event entering RECONNECTING per attempt and
    one landing back in OUTAGE_PAUSED per failure (with the bumped attempt
    count + next backoff delay) — never per-timer-tick spam. Resume then
    emits the settled playing/not-held truth."""
    import app.state as st
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(monkeypatch)
    backend = FakeResumeBackend()
    backend.set_device = AsyncMock(side_effect=[RuntimeError("down"), None])
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        admins, guests = _capture_broadcasts(stack)
        stack.enter_context(patch.object(st, "dlna_backend", backend))
        await _start_playing(qe, make_track("t1"))
        await session.enter_output_hold("connection_lost")

        _last_timer(timers).fire()               # attempt 1 -> fail
        await _drain()
        _last_timer(timers).fire()               # attempt 2 -> attach + resume
        await _drain()

        states = [e.state for e in _session_events(admins)]
        assert states == [STATE_OUTAGE_PAUSED,                      # hold entered
                          STATE_RECONNECTING, STATE_OUTAGE_PAUSED,  # attempt 1
                          STATE_RECONNECTING, STATE_PLAYING]        # attempt 2
        failed = _session_events(admins)[2]
        assert failed.attempts == 1 and failed.next_retry_s == 10.0
        resumed = _session_events(admins)[-1]
        assert resumed.held is False
        # Guests heard the same lean state sequence — never a divergent truth.
        assert [e.state for e in _session_events(guests)] == states
        assert all(e.reason is None for e in _session_events(guests))


async def test_idle_paused_landing_event_carries_reason(monkeypatch):
    """U4: the IdlePaused landing (window expired here) emits its reason so
    the admin banner can say WHY auto-resume declined; held stays True."""
    import app.state as st
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(
        monkeypatch, window_minutes=AsyncMock(return_value=60))
    backend = FakeResumeBackend()
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        admins, guests = _capture_broadcasts(stack)
        stack.enter_context(patch.object(st, "dlna_backend", backend))
        await _start_playing(qe, make_track("t1"))
        await session.enter_output_hold("connection_lost")

        clock.advance(61 * 60)                   # past the window
        _last_timer(timers).fire()               # attach OK, no audio
        await _drain()

        last = _session_events(admins)[-1]
        assert last.state == STATE_IDLE_PAUSED and last.held is True
        assert last.idle_paused_reason == "window_expired"
        assert last.window_remaining_s == 0      # clamped, never negative
        g = _session_events(guests)[-1]
        assert g.state == STATE_IDLE_PAUSED and g.held is True


async def test_was_paused_reattach_emits_paused_state(monkeypatch):
    """U4: the R17 paused re-attach (silent, no audio) is observable — state
    'paused' with held True (manual resume still owns the recovery)."""
    import app.state as st
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(monkeypatch)
    backend = FakeResumeBackend()
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        admins, guests = _capture_broadcasts(stack)
        stack.enter_context(patch.object(st, "dlna_backend", backend))
        await _start_playing(qe, make_track("t1"))
        await qe.set_paused(True)                # deliberately paused first
        await session.enter_output_hold("connection_lost")

        _last_timer(timers).fire()               # attach OK -> paused landing
        await _drain()

        last = _session_events(admins)[-1]
        assert last.state == STATE_PAUSED and last.held is True
        assert last.was_paused is True
        router.play.assert_not_awaited()


async def test_hold_cleared_emits_settled_state(monkeypatch):
    """U4: ANY hold exit emits the settled truth — here a confirmed start on
    a manual-skip dispatch (audio proof) lands held=False / playing."""
    import app.state as st
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(monkeypatch)
    backend = FakeResumeBackend()
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        admins, guests = _capture_broadcasts(stack)
        stack.enter_context(patch.object(st, "dlna_backend", backend))
        await _start_playing(qe, make_track("t1"))
        await session.enter_output_hold("connection_lost")

        token = sup.on_dispatched(make_track("t2"))   # manual skip dispatched
        sup.on_playback_confirmed(token)              # audio -> hold exits
        await _drain()

        last = _session_events(admins)[-1]
        assert last.state == STATE_PLAYING and last.held is False
        g = _session_events(guests)[-1]
        assert g.state == STATE_PLAYING and g.held is False


async def test_queue_cleared_during_outage_reattach_emits_idle(monkeypatch):
    """U4 scenario (c), observability half: queue cleared mid-outage -> the
    eventual re-attach lands IDLE (U3 owns the landing) and the emission
    reports it — no orphan 'resumed' event for a dropped track."""
    import app.state as st
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(monkeypatch)
    backend = FakeResumeBackend()
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        admins, guests = _capture_broadcasts(stack)
        stack.enter_context(patch.object(st, "dlna_backend", backend))
        await _start_playing(qe, make_track("t1"))
        await session.enter_output_hold("connection_lost")

        await qe.clear()                         # queue (incl. held item) gone
        _last_timer(timers).fire()               # device returns
        await _drain()

        router.play.assert_not_awaited()
        last = _session_events(admins)[-1]
        assert last.state == STATE_IDLE and last.held is False


# ── 2026-07-11 review fixes (F1/F3/F5/F7/F8/F12/F15/F16/F17) ──────────────────

async def test_user_pause_during_confirmation_window_suspends_deadline(monkeypatch):
    """F1: a user pause during the confirmation window must not force-skip
    the track (PAUSED is PRE_PLAYBACK — the old path granted one extension,
    then classified reachable → track-level skip). The deadline SUSPENDS:
    every expiry re-arms without consuming the single R15 extension, and the
    confirmation counts normally at user resume."""
    sup, timers, rec = _fresh(monkeypatch)
    outages = []
    sup.add_outage_listener(lambda *a: outages.append(a))
    with contextlib.ExitStack() as stack:
        qe, skipped, advance = _wire_state(stack)
        item = await _start_playing(qe, make_track("t1"))
        token = sup.on_dispatched(item.track)
        await qe.set_paused(True)                # user pauses mid-window
        for _ in range(3):
            timers.timers[-1].fire()             # deadline keeps expiring
            await _drain()
        assert outages == []                     # never classified
        rec.assert_not_called()
        skipped.assert_not_called()
        assert len(timers.timers) == 4           # original + 3 suspensions…
        assert all(t.delay == CONFIRM_DEADLINE_S for t in timers.timers)
        assert sup._current is not None
        assert sup._current.extended is False    # …extension NOT consumed
        await qe.set_paused(False)               # user resumes
        sup.on_playback_confirmed(token)         # confirmation fires normally
        rec.assert_called_once()


async def test_removed_held_front_resumes_new_front_at_zero_by_track_id(monkeypatch):
    """F5 (track-id belt): removing the HELD front during the outage — here
    directly on the engine, no gen bump — must not seek the removed track's
    position into the new front: the held track id captured at outage entry
    mismatches, so the resume plays the new front from 0:00. (The endpoint
    gen-bump suspenders are pinned in the API test modules.)"""
    import app.state as st
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(monkeypatch)
    backend = FakeResumeBackend()                # 42s position captured
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        stack.enter_context(patch.object(st, "dlna_backend", backend))
        await _start_playing(qe, make_track("t1"))
        await qe.append(make_track("t2"))
        await session.enter_output_hold("connection_lost")
        assert sup._outage.held_track_id == "t1"

        await qe.remove(0)                       # held front t1 removed

        _last_timer(timers).fire()               # device returns → auto-resume
        await _drain()

        router.play.assert_awaited_once()
        assert qe.state.current.track_id == "t2" # new front dispatched…
        backend.seek.assert_not_awaited()        # …from 0:00, no stale seek
        assert sup.session_state == STATE_PLAYING


async def test_never_started_hold_reasons_capture_zero_position(monkeypatch):
    """F7: dispatch_failed / confirm_timeout holds mean the held item never
    played — a previous track's position residue (Cast _pos_snapshot_ms /
    DLNA _play_start) must not become the held position. Device-loss reasons
    still capture normally."""
    import app.state as st
    from app.output import session
    for reason, expected in (("dispatch_failed", 0), ("confirm_timeout", 0),
                             ("connection_lost", 90_000)):
        sup, timers, rec, clock = _fresh_u3(monkeypatch)
        backend = FakeResumeBackend()
        backend._pos_snapshot_ms = 90_000        # previous track's residue
        with contextlib.ExitStack() as stack:
            qe, router = _wire_resume(stack, backend)
            stack.enter_context(patch.object(st, "dlna_backend", backend))
            await _start_playing(qe, make_track("t1"))
            await session.enter_output_hold(reason)
            assert sup._outage.held_position_ms == expected, reason
            session.clear_output_hold()


async def test_resume_seek_runs_outside_advance_lock(monkeypatch):
    """F8: the resume seek (Direct: up to 10s in an executor; Cast: an
    unbounded network roundtrip) must run AFTER _advance_lock is released —
    it may not stall every lock waiter."""
    import app.state as st
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(monkeypatch)
    backend = FakeResumeBackend()
    locked_during_seek = []

    async def seek(position_ms):
        locked_during_seek.append((st._advance_lock.locked(), position_ms))

    backend.seek = seek
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        stack.enter_context(patch.object(st, "dlna_backend", backend))
        await _start_playing(qe, make_track("t1"))
        await session.enter_output_hold("connection_lost")

        _last_timer(timers).fire()               # device returns → resume
        await _drain()

        router.play.assert_awaited_once()
        assert locked_during_seek == [(False, 42_000)]
        assert sup.session_state == STATE_PLAYING
        assert session.output_hold_active() is False


async def test_pause_intent_during_hold_reattaches_paused_silent(monkeypatch):
    """F12: pause pressed during the hold records the R17 intent on the
    supervisor AND the outage context — the eventual auto re-attach lands
    PAUSED and silent instead of auto-playing into a deliberately paused
    session."""
    import app.state as st
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(monkeypatch)
    backend = FakeResumeBackend()
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        stack.enter_context(patch.object(st, "dlna_backend", backend))
        await _start_playing(qe, make_track("t1"))
        await session.enter_output_hold("connection_lost")  # playing at entry
        assert sup._outage.was_paused is False

        sup.set_held_paused_intent()             # admin presses Pause while held
        assert sup.was_paused is True
        assert sup._outage.was_paused is True

        _last_timer(timers).fire()               # device returns
        await _drain()

        router.play.assert_not_awaited()         # NO auto-play
        assert sup.session_state == STATE_PAUSED
        assert session.output_hold_active() is True


async def test_manual_resume_queue_empty_landing_returns_true(monkeypatch):
    """F15: a manual resume that finds the queue cleared RESOLVES the press —
    hold released, session idle — and reports success. (The old False made
    the endpoint 409 'device unreachable' for a resolved landing.)"""
    import app.state as st
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(monkeypatch)
    backend = FakeResumeBackend()
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        stack.enter_context(patch.object(st, "dlna_backend", backend))
        await _start_playing(qe, make_track("t1"))
        await session.enter_output_hold("connection_lost")
        await qe.clear()                         # admin clears while held

        ok = await sup.manual_resume()

        assert ok is True                        # resolved, not "unreachable"
        assert session.output_hold_active() is False
        assert sup.session_state == session.STATE_IDLE
        router.play.assert_not_awaited()


async def test_manual_resume_track_level_skip_landing_returns_true(monkeypatch):
    """F15: a manual resume whose dispatch fails track-level (live device,
    dead holders) RESOLVES via today's skip path — hold cleared, advance owns
    what plays next — and reports success, not 'unreachable'."""
    import app.state as st
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(monkeypatch)
    backend = FakeResumeBackend()
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        stack.enter_context(patch.object(st, "dlna_backend", backend))
        router.play.side_effect = Exception("404")
        skipped = AsyncMock()
        advance = AsyncMock()
        stack.enter_context(patch.object(st, "_emit_track_skipped", skipped))
        stack.enter_context(patch.object(st, "_do_advance", advance))
        await _start_playing(qe, make_track("t1"))
        await session.enter_output_hold("connection_lost")

        ok = await sup.manual_resume()

        assert ok is True                        # resolved, not "unreachable"
        assert session.output_hold_active() is False
        skipped.assert_awaited_once()
        advance.assert_awaited_once()


async def test_held_volume_after_idle_paused_landing_applies_at_manual_resume(monkeypatch):
    """F17: volume accepted AFTER an IdlePaused landing (device attached, no
    audio) must still land on the device at manual resume, BEFORE dispatch —
    the attached+manual fast path used to skip the pending-volume apply."""
    import app.state as st
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(
        monkeypatch, window_minutes=AsyncMock(return_value=60))
    backend = FakeResumeBackend()
    order = []
    backend.set_volume = AsyncMock(
        side_effect=lambda level: order.append(("volume", level)))
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        router.play.side_effect = lambda *a, **k: order.append(("play",))
        stack.enter_context(patch.object(st, "dlna_backend", backend))
        stack.enter_context(patch("app.database.set_setting", AsyncMock()))
        await _start_playing(qe, make_track("t1"))
        await session.enter_output_hold("connection_lost")

        clock.advance(61 * 60)                   # window expires
        _last_timer(timers).fire()               # attach OK → IdlePaused
        await _drain()
        assert sup.session_state == STATE_IDLE_PAUSED
        backend.set_volume.assert_not_awaited()  # nothing pending yet

        await session.set_held_volume(0.3)       # volume set while landed
        ok = await sup.manual_resume()

        assert ok is True
        assert order == [("volume", 0.3), ("play",)]  # applied pre-dispatch
        assert session.output_hold_active() is False


async def test_attach_queued_behind_switch_aborts_without_set_device(monkeypatch):
    """F3: a retry attach queued on the attach-serial lock behind a manual
    switch must observe the bumped epoch after acquiring and abort WITHOUT
    calling set_device — otherwise its uncancellable executor connect could
    finish last and commit the OLD device's internals over the new one's."""
    import app.state as st
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(monkeypatch)
    monkeypatch.setattr(session, "_attach_serial", asyncio.Lock())
    backend = FakeResumeBackend()
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        stack.enter_context(patch.object(st, "dlna_backend", backend))
        await _start_playing(qe, make_track("t1"))
        await session.enter_output_hold("connection_lost")

        await session._attach_serial.acquire()   # a manual switch owns the seam
        try:
            _last_timer(timers).fire()           # backoff attach → parks on lock
            await _drain()
            backend.set_device.assert_not_awaited()  # queued, not attaching
            session.notify_manual_switch()       # the switch bumps the epoch…
        finally:
            session._attach_serial.release()     # …and finishes its set_device
        await _drain()

        backend.set_device.assert_not_awaited()  # stale epoch → never attached
        router.play.assert_not_awaited()
        assert session.output_hold_active() is True  # switch owns the clear


# ── U4 WS-miss-then-snapshot-resync integration (plan requirement) ────────────
# End-to-end over the app's REAL /admin/ws route (app/api/admin.py:
# admin_websocket → events.bus manager) and the REAL admin now-playing GET —
# no mocks on either path. Uses conftest's `client`/`mock_state` TestClient
# fixtures (cookie auth, wired QueueEngine) plus this file's U3 harness.


async def test_ws_miss_then_snapshot_resync_output_session(
        client, mock_state, monkeypatch):
    """The resync contract (U4, R20), both halves:

    (1) an admin WS client connected BEFORE the outage receives the
        output_session event ``enter_output_hold`` broadcasts through the
        real route, and the admin now-playing GET carries the SAME state
        truth field-for-field (every WS delta is refetchable);
    (2) a SECOND client connecting AFTER the hold entered gets NO event
        (the delta is gone) but converges to the same truth from the GET
        snapshot alone.
    """
    import app.state as st
    from app.events.bus import manager
    from app.output import session

    # Loop hygiene: TestClient runs the app in its own portal loop while this
    # test drives enter_output_hold on the pytest loop. Neither path should
    # contend these module-level locks, but a contended acquire would bind
    # them to one loop for good (3.11 asyncio.Lock is loop-bound) and poison
    # later tests — give them loop-fresh instances for the duration.
    monkeypatch.setattr(session, "_attach_serial", asyncio.Lock())
    monkeypatch.setattr(st, "_advance_lock", asyncio.Lock())

    sup, timers, rec, clock = _fresh_u3(monkeypatch)
    qe, or_ = mock_state
    backend = FakeResumeBackend()
    or_.active = backend                    # enter_output_hold reads .active
    try:
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(st, "dlna_backend", backend))
            await _start_playing(qe, make_track("t1"))

            # (1) delta half: connected BEFORE the outage → hears the event.
            with client.websocket_connect("/admin/ws") as ws1:
                assert manager.admin_count == 1
                await session.enter_output_hold("connection_lost")
                # Emission is awaited inside enter_output_hold: the frame is
                # either buffered in ws1's test session or the manager pruned
                # the socket as dead — a still-registered socket bounds the
                # otherwise-blocking receive below (no timed receive exists
                # on the sync TestClient websocket API).
                assert manager.admin_count == 1

                event = ws1.receive_json()
                assert event["type"] == "output_session"
                assert event["state"] == STATE_OUTAGE_PAUSED
                assert event["held"] is True
                assert event["reason"] == "connection_lost"
                assert event["backend_type"] == "dlna"
                assert event["device_id"] == "dev-1"

                # Resync contract: the GET snapshot mirrors EVERY event field
                # (the event is exactly the admin snapshot plus ``type``).
                snap = client.get(
                    "/admin/playback/now-playing").json()["output_session"]
                truth = {k: v for k, v in event.items() if k != "type"}
                assert snap == truth

            # (2) missed-delta half: connects AFTER the hold entered.
            with client.websocket_connect("/admin/ws") as ws2:
                assert manager.admin_count == 1  # ws1 deregistered, ws2 live
                # Nothing was broadcast since ws2 connected, so its receive
                # buffer is empty — the bounded no-event probe (the accept
                # frame was consumed by the connect handshake).
                assert ws2._send_rx.statistics().current_buffer_used == 0
                snap2 = client.get(
                    "/admin/playback/now-playing").json()["output_session"]
                assert snap2 == truth            # converged with no delta
    finally:
        # The route's finally deregisters each socket on close; belt-and-
        # braces so a mid-test failure can never leak a dead socket into
        # later tests through the module-singleton manager.
        manager._admin.clear()
        manager._guest.clear()


# ── U10: Cast flow-mode boundary — two-phase advance/count split ──────────────
# The flow advance authority (advance-authority table, Chromecast flow row):
# queue/Now Playing advance on the ENCODE clock (notify_flow_boundary), while
# the play COUNT fires through the U1 chokepoint only at the DEVICE-time
# crossing (notify_confirmed with the returned token, no deadline timer).


async def test_flow_boundary_advances_without_count_until_crossing(monkeypatch):
    """notify_flow_boundary advances the queue NOW and returns an unconfirmed
    token with NO deadline armed; the count fires only when the backend
    reports the device-time crossing through the chokepoint."""
    from app.output import session
    sup, timers, rec = _fresh(monkeypatch)
    with contextlib.ExitStack() as stack:
        qe, skipped, advance = _wire_state(stack)
        item1 = await _start_playing(qe, make_track("t1"))
        t2 = make_track("t2")
        await qe.append(t2)
        token1 = sup.on_dispatched(item1.track)
        sup.on_playback_confirmed(token1)
        rec.assert_called_once()
        n_timers = len(timers.timers)

        token2 = await session.notify_flow_boundary(t2)

        assert isinstance(token2, int)
        assert qe.state.current.track_id == "t2"      # boundary-clock advance
        assert [i.track_id for i in qe.history] == ["t1"]
        assert rec.call_count == 1                    # encode time is not heard
        assert len(timers.timers) == n_timers         # NO deadline armed
        assert sup._current.token == token2 and sup._current.deferred

        session.notify_confirmed(token2)              # device-time crossing
        assert rec.call_count == 2
        assert rec.call_args.args[0].id == "t2"
        session.notify_confirmed(token2)              # one-shot
        assert rec.call_count == 2


async def test_flow_boundary_emits_queue_and_now_playing_events(monkeypatch):
    """The static/UI half of U10: the boundary advance drives the SAME
    queue_changed / now_playing_changed events the frontend already renders
    from — no new frontend surface is needed for flow-mode Now Playing."""
    from app.output import session
    sup, timers, rec = _fresh(monkeypatch)
    with contextlib.ExitStack() as stack:
        qe, skipped, advance = _wire_state(stack)
        events = []

        async def _cb(event, payload=None):
            events.append(event)

        await _start_playing(qe, make_track("t1"))
        t2 = make_track("t2")
        await qe.append(t2)
        qe.add_callback(_cb)

        await session.notify_flow_boundary(t2)

        assert "queue_changed" in events
        assert "now_playing_changed" in events


async def test_flow_boundary_superseded_dispatch_counts_at_its_own_crossing(
        monkeypatch):
    """Tracks shorter than the device lag: boundary N+1 supersedes boundary
    N's dispatch BEFORE the device crossed N — the parked (flow-deferred)
    dispatch still counts when ITS crossing arrives, in order, one-shot."""
    from app.output import session
    sup, timers, rec = _fresh(monkeypatch)
    with contextlib.ExitStack() as stack:
        qe, skipped, advance = _wire_state(stack)
        item1 = await _start_playing(qe, make_track("t1"))
        t2, t3 = make_track("t2"), make_track("t3")
        await qe.append(t2)
        await qe.append(t3)
        token1 = sup.on_dispatched(item1.track)
        sup.on_playback_confirmed(token1)

        token2 = await session.notify_flow_boundary(t2)
        token3 = await session.notify_flow_boundary(t3)   # supersedes token2
        assert rec.call_count == 1

        session.notify_confirmed(token2)   # t2's own crossing still counts
        assert rec.call_count == 2
        assert rec.call_args.args[0].id == "t2"
        session.notify_confirmed(token2)   # one-shot
        assert rec.call_count == 2
        session.notify_confirmed(token3)
        assert rec.call_count == 3
        assert rec.call_args.args[0].id == "t3"


async def test_flow_boundary_respects_play_recorded_mark(monkeypatch):
    """R19 through the flow path: a front item carrying the play_recorded
    mark advances at the boundary and its crossing does NOT re-count; the
    mark is consumed for future organic replays."""
    from app.output import session
    sup, timers, rec = _fresh(monkeypatch)
    with contextlib.ExitStack() as stack:
        qe, skipped, advance = _wire_state(stack)
        await _start_playing(qe, make_track("t1"))
        t2 = make_track("t2")
        await qe.append(t2)
        qe.queue[0].play_recorded = True

        token2 = await session.notify_flow_boundary(t2)
        session.notify_confirmed(token2)

        rec.assert_not_called()
        assert qe.state.current.play_recorded is False    # mark consumed


async def test_flow_boundary_hold_or_skip_owns_transition(monkeypatch):
    """R16 firewall on the flow row: a hold freezes the queue (None, no
    advance); a skip supersedes — it bumps _advance_gen BEFORE taking
    _advance_lock, so a boundary parked on the lock drops at the gen
    re-check once the skip releases (the boundary WAITS for the lock, it
    never bails on locked() — see the initial-LOAD test below)."""
    import app.state as st
    from app.output import session
    sup, timers, rec = _fresh(monkeypatch)
    monkeypatch.setattr(st, "_advance_lock", asyncio.Lock())
    with contextlib.ExitStack() as stack:
        qe, skipped, advance = _wire_state(stack)
        await _start_playing(qe, make_track("t1"))
        t2 = make_track("t2")
        await qe.append(t2)

        monkeypatch.setattr(hold, "_output_hold", True)
        assert await session.notify_flow_boundary(t2) is None
        monkeypatch.setattr(hold, "_output_hold", False)

        await st._advance_lock.acquire()                  # a skip in flight
        task = asyncio.create_task(session.notify_flow_boundary(t2))
        await _drain()
        assert not task.done()                            # parked on the lock
        st._advance_gen += 1                              # the skip's bump
        st._advance_lock.release()
        assert await task is None                         # dropped at re-check

        assert qe.state.current.track_id == "t1"          # nothing advanced
        assert [i.track_id for i in qe.queue] == ["t2"]
        rec.assert_not_called()


async def test_flow_boundary_waits_for_initial_load_lock_then_advances(
        monkeypatch):
    """A boundary firing while the INITIAL flow LOAD still holds
    _advance_lock (first track shorter than the run-ahead: the pump starts
    before run_in_executor returns) must NOT be dropped — it waits for the
    lock and advances + issues its token once the dispatch releases (gen
    unchanged); dropping it was a permanent off-by-one + lost count."""
    import app.state as st
    from app.output import session
    sup, timers, rec = _fresh(monkeypatch)
    monkeypatch.setattr(st, "_advance_lock", asyncio.Lock())
    with contextlib.ExitStack() as stack:
        qe, skipped, advance = _wire_state(stack)
        item1 = await _start_playing(qe, make_track("t1"))
        t2 = make_track("t2")
        await qe.append(t2)
        token1 = sup.on_dispatched(item1.track)   # the LOAD dispatch (deadline)

        await st._advance_lock.acquire()          # the dispatch holds the lock
        task = asyncio.create_task(session.notify_flow_boundary(t2))
        await _drain()
        assert not task.done()                    # delayed, NOT dropped
        assert qe.state.current.track_id == "t1"
        st._advance_lock.release()                # the LOAD completes

        token2 = await task
        assert isinstance(token2, int)
        assert qe.state.current.track_id == "t2"  # advance landed after release
        assert [i.track_id for i in qe.history] == ["t1"]
        rec.assert_not_called()                   # counts stay device-time gated

        session.notify_confirmed(token1)          # first PLAYING (parked prev)
        assert rec.call_count == 1
        assert rec.call_args.args[0].id == "t1"
        session.notify_confirmed(token2)          # t2's own crossing
        assert rec.call_count == 2
        assert rec.call_args.args[0].id == "t2"


async def test_flow_boundary_parked_on_lock_respects_hold_entry(monkeypatch):
    """The R15 companion to awaiting the lock: a hold entered while the
    boundary is parked must keep the queue frozen — the in-lock hold
    re-check drops the advance."""
    import app.state as st
    from app.output import session
    sup, timers, rec = _fresh(monkeypatch)
    monkeypatch.setattr(st, "_advance_lock", asyncio.Lock())
    with contextlib.ExitStack() as stack:
        qe, skipped, advance = _wire_state(stack)
        await _start_playing(qe, make_track("t1"))
        t2 = make_track("t2")
        await qe.append(t2)

        await st._advance_lock.acquire()
        task = asyncio.create_task(session.notify_flow_boundary(t2))
        await _drain()
        assert not task.done()                    # parked on the lock
        monkeypatch.setattr(hold, "_output_hold", True)   # outage lands now
        st._advance_lock.release()

        assert await task is None                 # frozen queue stays frozen
        assert qe.state.current.track_id == "t1"
        assert [i.track_id for i in qe.queue] == ["t2"]
        rec.assert_not_called()


async def test_flow_first_load_dispatch_parked_by_boundary_counts_once(
        monkeypatch):
    """The flow session's FIRST dispatch (deadline armed) superseded by the
    first deadline=False boundary before its PLAYING confirmation is PARKED,
    not dropped — the first PLAYING still counts the first track exactly
    once, via the flow-deferred path."""
    from app.output import session
    sup, timers, rec = _fresh(monkeypatch)
    with contextlib.ExitStack() as stack:
        qe, skipped, advance = _wire_state(stack)
        item1 = await _start_playing(qe, make_track("t1"))
        t2 = make_track("t2")
        await qe.append(t2)
        token1 = sup.on_dispatched(item1.track)   # LOAD dispatch, deadline armed
        assert not timers.timers[0].cancelled

        token2 = await session.notify_flow_boundary(t2)   # supersedes pre-PLAYING
        assert timers.timers[0].cancelled         # parked: deadline retired
        assert token1 in sup._flow_deferred
        rec.assert_not_called()

        session.notify_confirmed(token1)          # the first PLAYING arrives
        rec.assert_called_once()
        assert rec.call_args.args[0].id == "t1"
        session.notify_confirmed(token1)          # one-shot
        rec.assert_called_once()
        session.notify_confirmed(token2)          # t2's own crossing still counts
        assert rec.call_count == 2


async def test_flow_deferred_cap_eviction_warns_and_noops(caplog):
    """The deferred stash is capped generously (256) and eviction is
    PATHOLOGICAL — it must WARN (a real count is being lost), and a late
    crossing for an evicted token must no-op harmlessly."""
    from app.output import session
    sup, timers, rec = make_supervisor()
    first = sup.on_dispatched(make_track("t0"), deadline=False)
    with caplog.at_level(logging.WARNING, logger="app.output.session"):
        for i in range(1, session._FLOW_DEFERRED_CAP + 1):
            sup.on_dispatched(make_track(f"t{i}"), deadline=False)
        assert not [r for r in caplog.records
                    if "flow-deferred cap" in r.getMessage()]  # at cap: quiet
        sup.on_dispatched(make_track("tlast"), deadline=False)  # one past it
    assert len(sup._flow_deferred) == session._FLOW_DEFERRED_CAP
    assert first not in sup._flow_deferred                     # oldest evicted
    warns = [r for r in caplog.records
             if "flow-deferred cap" in r.getMessage()]
    assert len(warns) == 1
    sup.on_playback_confirmed(first)         # late crossing for the evicted
    rec.assert_not_called()                  # harmless no-op, nothing counted


async def test_defer_confirmation_cancels_deadline_then_confirm_counts():
    """A dispatch_play that lands as a stitcher reposition defers: the 12s
    deadline is cancelled (the device-buffer lag would misfire it) and the
    later crossing confirms/counts through the normal chokepoint."""
    sup, timers, rec = make_supervisor()
    token = sup.on_dispatched(make_track("t1"))
    assert not timers.timers[0].cancelled

    sup.defer_confirmation(token)
    assert timers.timers[0].cancelled
    assert sup._current.deferred is True

    sup.on_playback_confirmed(token)
    rec.assert_called_once()
    sup.defer_confirmation(token)      # confirmed → no-op, no exception
    sup.defer_confirmation(999)        # stale → no-op


async def test_flow_outage_reasons_are_classifier_ambiguous():
    """Consumer-gone / receiver-IDLE mid-flow have NOT established the device
    is gone (the Cast socket may still be CONNECTED) — they must route
    through the classifier's reachability probe, never the device-level
    fast path (advance-authority table: outage-SUSPECTED → classifier)."""
    from app.output.session import DEVICE_LEVEL_REASONS
    assert "flow_consumer_gone" not in DEVICE_LEVEL_REASONS
    assert "flow_receiver_idle" not in DEVICE_LEVEL_REASONS


async def test_flow_consumer_gone_unreachable_holds_reachable_skips(monkeypatch):
    """The classifier verdicts for the flow's ambiguous reasons: unreachable
    device means outage hold; reachable device means the stream/track is the
    failure — today's skip behavior."""
    from app.output import session
    # Unreachable → hold.
    sup, timers, rec = _fresh(monkeypatch)
    sup.add_outage_listener(session.classify_outage)
    probe = AsyncMock(return_value=(False, None))
    with contextlib.ExitStack() as stack:
        qe, skipped, advance = _wire_state(stack, probe=probe)
        item = await _start_playing(qe, make_track("t1"))
        sup.on_dispatched(item.track)
        sup.on_outage_reported("flow_consumer_gone")
        await _drain()
        assert session.output_hold_active() is True
        skipped.assert_not_called()
        advance.assert_not_called()

    # Reachable → track-level skip (fresh supervisor + hold flag).
    sup, timers, rec = _fresh(monkeypatch)
    sup.add_outage_listener(session.classify_outage)
    probe = AsyncMock(return_value=(True, "PLAYING"))
    with contextlib.ExitStack() as stack:
        qe, skipped, advance = _wire_state(stack, probe=probe)
        item = await _start_playing(qe, make_track("t1"))
        sup.on_dispatched(item.track)
        sup.on_outage_reported("flow_receiver_idle")
        await _drain()
        assert session.output_hold_active() is False
        skipped.assert_awaited_once()
        advance.assert_awaited_once()


async def test_capture_position_hook_overrides_snapshot():
    """hold._capture_position_ms consults the duck-typed U10 hook FIRST: a
    value wins over _pos_snapshot_ms (flow mode maps device stream time to
    TRACK time); None or a raising hook falls through to the normal reads
    (per-track mode byte-identical)."""

    class HookBackend:
        _pos_snapshot_ms = 99_000

        def capture_held_position_ms(self):
            return 4_321

    class NoneHookBackend:
        _pos_snapshot_ms = 99_000

        def capture_held_position_ms(self):
            return None

    class RaisingHookBackend:
        _pos_snapshot_ms = 99_000

        def capture_held_position_ms(self):
            raise RuntimeError("boom")

    assert await hold._capture_position_ms(HookBackend()) == 4_321
    assert await hold._capture_position_ms(NoneHookBackend()) == 99_000
    assert await hold._capture_position_ms(RaisingHookBackend()) == 99_000


async def test_resume_primes_flow_offset_and_device_seek_noops(monkeypatch):
    """Flow-mode outage resume (R7): the supervisor primes the held position
    into the backend (play() feeds it to create_flow_session) and the
    resume_seek hook NO-OPs — position-resume is fully server-controlled,
    no device seek; the play is not recounted (R19)."""
    import app.state as st
    from app.output import session

    class FakeFlowResumeBackend(FakeResumeBackend):
        def __init__(self):
            super().__init__()
            self.primed = []
            self._flow_session = object()      # flow mode active

        def capture_held_position_ms(self):
            return 5_000                       # device-time mapped held offset

        def prime_resume_offset(self, position_ms):
            self.primed.append(position_ms)

        async def resume_seek(self, position_ms):
            if self._flow_session is not None:
                return                         # server-controlled resume
            await self.seek(position_ms)

    sup, timers, rec, clock = _fresh_u3(monkeypatch)
    backend = FakeFlowResumeBackend()
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        stack.enter_context(patch.object(st, "chromecast_backend", backend))
        item = await _start_playing(qe, make_track("t1"))
        token = sup.on_dispatched(item.track)
        sup.on_playback_confirmed(token)       # counted once
        rec.assert_called_once()

        await session.enter_output_hold("connection_lost", play_recorded=True)
        assert session.output_hold_active() is True
        assert sup._outage.held_position_ms == 5_000   # via the U10 hook

        _last_timer(timers).fire()             # backoff tick → attach + resume
        await _drain()

        router.play.assert_awaited_once()      # re-dispatch (play re-LOADs)
        assert backend.primed == [5_000]       # held offset primed, not sought
        backend.seek.assert_not_awaited()      # NO device seek in flow mode
        assert session.output_hold_active() is False
        assert sup.session_state == STATE_PLAYING
        # R19: confirming the resume dispatch must not re-count.
        sup.on_playback_confirmed(sup.current_token())
        rec.assert_called_once()


async def test_late_deadline_callback_after_defer_is_noop():
    """The defer/deadline race: a deadline callback already in flight when
    defer_confirmation cancelled the timer must not classify the deferred
    dispatch — its confirmation lawfully outlives any deadline."""
    sup, timers, rec = make_supervisor()
    outages = []
    sup.add_outage_listener(lambda *a: outages.append(a))
    token = sup.on_dispatched(make_track("t1"))
    sup.defer_confirmation(token)

    timers.timers[0].cb()              # late callback racing its cancel
    await _drain()

    assert outages == []
    sup.on_playback_confirmed(token)   # the crossing still counts
    rec.assert_called_once()


# ── U7 (2026-08-04-002 plexplayer plan): boundary producer + foreign hold ─────

async def test_gapless_boundary_from_plexplayer_producer_counts_once(
        monkeypatch):
    """AE5 (supervisor half): the plexplayer timeline watch reports the
    itemID-edge boundary through notify_gapless_boundary — one advance, one
    count for the chained track, NO dispatch at the boundary (the fourth
    producer rides the same chokepoint as Direct/DLNA)."""
    from app.output import session
    sup, timers, rec = _fresh(monkeypatch)
    with contextlib.ExitStack() as stack:
        qe, skipped, advance = _wire_state(stack)
        item1 = await _start_playing(qe, make_track("t1"))
        t2 = make_track("t2")
        await qe.append(t2)
        token1 = sup.on_dispatched(item1.track)
        sup.on_playback_confirmed(token1)
        rec.assert_called_once()                 # first track counted

        await session.notify_gapless_boundary(t2)

        assert qe.state.current.track_id == "t2"     # queue advanced
        assert [i.track_id for i in qe.history] == ["t1"]
        assert rec.call_count == 2                   # exactly one count each
        assert rec.call_args.args[0] is t2
        advance.assert_not_called()                  # no dispatch at boundary
        skipped.assert_not_called()


async def test_hold_foreign_controller_enters_idle_paused_hold(monkeypatch):
    """Foreign-controller yield: the standard hold (queue frozen, R19 mark
    intact) but NO reconnect machinery — the device is reachable, so
    auto-reattach would fight the other controller. Lands IDLE_PAUSED with
    reason 'foreign_controller', serialized on the admin output_session
    payload (the banner switch input)."""
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(monkeypatch)
    backend = FakeResumeBackend()
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        item = await _start_playing(qe, make_track("t1"))
        token = sup.on_dispatched(item.track)
        sup.on_playback_confirmed(token)         # play counted
        rec.assert_called_once()

        await session.hold_foreign_controller()

        assert session.output_hold_active() is True
        assert session.output_hold_reason() == "foreign_controller"
        assert sup.session_state == STATE_IDLE_PAUSED
        assert sup.idle_paused_reason == "foreign_controller"
        # R19: the confirmed play re-fronts already counted.
        assert qe.queue[0].play_recorded is True
        # No reconnect loop may run: every armed timer is cancelled and the
        # context reads attached (manual resume goes straight to dispatch).
        assert all(t.cancelled for t in timers.timers)
        ot = sup._outage
        assert ot is not None and ot.attached is True
        admin = await session.session_snapshot_admin()
        assert admin["held"] is True
        assert admin["state"] == "idle_paused"
        assert admin["idle_paused_reason"] == "foreign_controller"
        assert admin["reason"] == "foreign_controller"


async def test_hold_foreign_controller_backoff_never_redispatches(monkeypatch):
    """Even a timer that slipped through must not re-dispatch while the
    foreign hold stands: the retry timer is cancelled at yield time and no
    new one is armed — router.play stays untouched."""
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(monkeypatch)
    backend = FakeResumeBackend()
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        await _start_playing(qe, make_track("t1"))

        await session.hold_foreign_controller()

        for t in list(timers.timers):
            t.cb()                   # late callbacks racing their cancels
        await _drain()
        router.play.assert_not_awaited()
        assert session.output_hold_active() is True
        assert sup.session_state == STATE_IDLE_PAUSED


async def test_hold_foreign_controller_manual_resume_reactivates(monkeypatch):
    """The banner's Resume press IS the re-activate: manual resume bypasses
    the auto gates, dispatches the held front fresh (taking the device
    back), clears the hold, and never re-counts the held play."""
    from app.output import session
    sup, timers, rec, clock = _fresh_u3(monkeypatch)
    backend = FakeResumeBackend()
    with contextlib.ExitStack() as stack:
        qe, router = _wire_resume(stack, backend)
        item = await _start_playing(qe, make_track("t1"))
        token = sup.on_dispatched(item.track)
        sup.on_playback_confirmed(token)
        rec.assert_called_once()

        await session.hold_foreign_controller()
        ok = await sup.manual_resume()

        assert ok is True
        router.play.assert_awaited_once()        # fresh dispatch = takeover
        assert session.output_hold_active() is False
        assert sup.session_state == STATE_PLAYING
        assert sup.idle_paused_reason == ""
        # R19: confirming the resume dispatch must not re-count.
        sup.on_playback_confirmed(sup.current_token())
        rec.assert_called_once()
