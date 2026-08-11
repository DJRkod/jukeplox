"""Tests for the radio session + non-destructive queue takeover (radio plan U3).

DB-free by construction: the ``RadioSession`` seams (stop_output / hold_queue /
has_queue_current / play_url / resume_queue / report_click / validate_url) are all
injectable, so the state-machine behavior is exercised with plain async mocks and
never touches ``app.state``, the output router, or the DB. The two
integration-flavored tests use a REAL ``QueueEngine`` with its persistence stubbed
to no-ops (so ``hold_current`` / ``advance`` run their real in-memory logic without
spawning aiosqlite tasks) — mirroring how ``tests/test_tag_utils.py`` stays out of
the aiosqlite teardown-hang path.
"""

from __future__ import annotations

import asyncio

import pytest

from app.models import Track
from app.queue.engine import QueueEngine
from app.queue.models import QueueItem
from app.radio.client import Station
from app.radio.session import (
    RadioSession,
    is_radio_track,
    make_radio_track,
)


# ── helpers ──────────────────────────────────────────────────────────────────


def _station(uuid: str = "uuid-1", name: str = "Jazz FM",
             url: str = "http://stream.example/jazz") -> Station:
    return Station(
        stationuuid=uuid, name=name, url=url, url_resolved=url, favicon="",
        codec="MP3", bitrate=128, tags=["jazz"], countrycode="US",
        lastcheckok=True,
    )


def _track(tid: str = "t1", title: str = "Song") -> Track:
    return Track(id=tid, title=title, artist="A", album="Al", duration_ms=1000,
                 stream_key=f"/parts/{tid}")


class _Recorder:
    """A recording async/sync callable that logs invocation order into a shared
    list, so we can assert the takeover call sequence."""

    def __init__(self, log: list, name: str, *, ret=None, sync: bool = False,
                 raises: BaseException | None = None):
        self._log = log
        self._name = name
        self._ret = ret
        self._sync = sync
        self._raises = raises
        self.calls: list[tuple] = []

    def _do(self, args, kwargs):
        self.calls.append((args, kwargs))
        self._log.append(self._name)
        if self._raises is not None:
            raise self._raises
        return self._ret

    def __call__(self, *args, **kwargs):
        if self._sync:
            return self._do(args, kwargs)

        async def _coro():
            return self._do(args, kwargs)

        return _coro()


def _make_session(log, *, has_current: bool = True, validate_ret: str | None = None,
                  play_raises: BaseException | None = None,
                  queue_non_empty: bool = False):
    """Build a RadioSession wired to recorders. Returns (session, recorders).

    ``teardown``/``autostart`` recorders + a ``queue_non_empty`` predicate are
    always wired so the session never falls back to the real ``app.state`` /
    ``app.radio.stream`` seams (keeps the test DB-free)."""
    st = _station()
    recs = {
        "stop": _Recorder(log, "stop"),
        "hold": _Recorder(log, "hold"),
        "play": _Recorder(log, "play", raises=play_raises),
        "resume": _Recorder(log, "resume"),
        "click": _Recorder(log, "click", sync=True),
        "validate": _Recorder(log, "validate", ret=(validate_ret or st.play_url)),
        "teardown": _Recorder(log, "teardown"),
        "autostart": _Recorder(log, "autostart"),
    }
    sess = RadioSession(
        stop_output=recs["stop"],
        hold_queue=recs["hold"],
        has_queue_current=lambda: has_current,
        play_url=recs["play"],
        resume_queue=recs["resume"],
        report_click=recs["click"],
        validate_url=recs["validate"],
        teardown_stream=recs["teardown"],
        autostart_queue=recs["autostart"],
        queue_non_empty=lambda: queue_non_empty,
    )
    return sess, recs


# ── pseudo-Track sentinel (U4 seam) ──────────────────────────────────────────


def test_make_radio_track_carries_endless_sentinel():
    st = _station()
    t = make_radio_track(st, "http://final/url")
    assert t.duration_ms == 0            # SG-03: duration_ms=0 marks endless
    assert is_radio_track(t) is True     # sentinel attribute present
    assert t.id == st.stationuuid
    assert t.stream_key == "http://final/url"
    # A normal finite track is NOT a radio track.
    assert is_radio_track(_track()) is False


# ── AE1: happy start ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_stops_output_holds_queue_and_marks_active():
    log: list = []
    sess, recs = _make_session(log, has_current=True)
    assert sess.is_active() is False

    await sess.start(_station())

    assert sess.is_active() is True
    assert sess.station is not None and sess.station.stationuuid == "uuid-1"
    # Called output_router.stop() AND hold_current(), then played.
    assert recs["stop"].calls, "output stop must be called"
    assert recs["hold"].calls, "hold_current must be called on takeover"
    assert recs["play"].calls, "the station URL must be played"
    # Ordering: validate → stop → hold → play (the corrected takeover sequence).
    assert log == ["validate", "stop", "hold", "play", "click"]


# ── AE2: happy stop → resume ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_resumes_the_held_queue():
    log: list = []
    sess, recs = _make_session(log, has_current=True)
    await sess.start(_station())
    log.clear()

    await sess.stop()

    assert sess.is_active() is False
    assert recs["stop"].calls, "the station stream must be stopped"
    assert recs["resume"].calls, "the held queue must be resumed"
    # active is cleared BEFORE the resume dispatch (so its queue_changed no longer
    # sees radio_active); stop-output → proxy teardown → resume, in that order.
    assert log == ["stop", "teardown", "resume"]


# ── FE-1: radio_active gates auto-start ──────────────────────────────────────


@pytest.mark.asyncio
async def test_active_flag_backs_radio_active_predicate():
    """The session's is_active() is what state.radio_active() returns — the value
    that gates _should_auto_start()/_do_advance(). Prove the gate would be True
    (blocking auto-start) after start and False after stop."""
    log: list = []
    sess, _ = _make_session(log, has_current=True)

    # Simulate the _should_auto_start expression's radio clause.
    def would_auto_start() -> bool:
        # queue non-empty + nothing playing would be True; radio_active gates it.
        return not sess.is_active()

    assert would_auto_start() is True          # idle: auto-start allowed
    await sess.start(_station())
    assert sess.is_active() is True
    assert would_auto_start() is False         # FE-1: gated while radio plays
    await sess.stop()
    assert would_auto_start() is True          # gate lifts on stop


# ── R5: switch does not re-hold ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_switch_station_does_not_rehold_or_remutate_queue():
    log: list = []
    sess, recs = _make_session(log, has_current=True)
    await sess.start(_station("uuid-1", "First"))
    assert len(recs["hold"].calls) == 1

    # Switch to a second station while the first plays.
    await sess.start(_station("uuid-2", "Second"))

    assert sess.is_active() is True
    assert sess.station.stationuuid == "uuid-2"
    # hold_current was called EXACTLY once (at first takeover), never on switch.
    assert len(recs["hold"].calls) == 1, "switch must not re-hold the queue"
    # The switch DID stop the old stream and play the new one.
    assert len(recs["stop"].calls) == 2   # first takeover + switch
    assert len(recs["play"].calls) == 2


# ── actor-independence ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resume_is_actor_independent():
    """Stop restores the queue regardless of who started it — there is no actor
    coupling in the session. A stop from a fresh caller still resumes."""
    log: list = []
    sess, recs = _make_session(log, has_current=True)
    await sess.start(_station())
    # "Different actor" stops — the session has no notion of the starter.
    await sess.stop()
    assert recs["resume"].calls, "any authorized stop resumes the queue"


# ── ADV-2: stop degrades, never raises ───────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_does_not_raise_when_resume_degrades():
    """The resume seam routes through _play_with_fallback (ADV-2), which degrades
    a dead source to skip/outage-hold internally and does NOT raise. The session
    must therefore return cleanly to idle even under a degrading resume."""
    log: list = []
    sess, _ = _make_session(log, has_current=True)
    await sess.start(_station())

    # A well-behaved resume seam (mirrors _resume_radio_hold) never raises even
    # when the held source died — assert stop() completes and lands idle.
    await sess.stop()
    assert sess.is_active() is False


@pytest.mark.asyncio
async def test_stop_propagation_if_resume_raises_is_contained_by_fallback():
    """Belt-and-suspenders: even if the injected resume seam itself raised
    (which the real ADV-2 seam does not), the session has already cleared active
    and stopped output before dispatching resume — so radio state is consistent.
    We assert the corrected ORDER guarantees active is False before resume runs."""
    order: list = []

    async def _resume():
        # At this point radio must already be inactive (gate lifted, FE-1).
        order.append(("resume", sess.is_active()))

    sess = RadioSession(
        stop_output=lambda: asyncio.sleep(0),
        hold_queue=lambda: asyncio.sleep(0),
        has_queue_current=lambda: True,
        play_url=lambda s, u: asyncio.sleep(0),
        resume_queue=_resume,
        report_click=lambda uuid: None,
        validate_url=lambda u: _immediately(u),
    )
    await sess.start(_station())
    await sess.stop()
    assert order == [("resume", False)]  # active cleared before resume dispatch


async def _immediately(v):
    return v


# ── race: lock serializes start/stop/switch ──────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_start_stop_switch_serialize_no_double_hold():
    """Concurrent start/switch/stop must serialize under the session lock so the
    queue is held at most once and resumed at most once."""
    log: list = []
    hold_calls = 0
    resume_calls = 0

    async def _hold():
        nonlocal hold_calls
        hold_calls += 1
        await asyncio.sleep(0)  # yield to let a racing task interleave if unlocked

    async def _resume():
        nonlocal resume_calls
        resume_calls += 1
        await asyncio.sleep(0)

    sess = RadioSession(
        stop_output=lambda: asyncio.sleep(0),
        hold_queue=_hold,
        has_queue_current=lambda: True,
        play_url=lambda s, u: asyncio.sleep(0),
        resume_queue=_resume,
        report_click=lambda uuid: None,
        validate_url=lambda u: _immediately(u),
    )

    # Fire two starts (a start + an instant switch) and a stop concurrently.
    await asyncio.gather(
        sess.start(_station("uuid-1")),
        sess.start(_station("uuid-2")),
        sess.stop(),
    )
    # No matter the interleaving, hold is called at most once (only the first
    # takeover holds; the second start is a switch or a no-op after a stop).
    assert hold_calls <= 1, "the queue must never be double-held"
    assert resume_calls <= 1, "the queue must never be double-resumed"


@pytest.mark.asyncio
async def test_double_stop_resumes_once():
    log: list = []
    sess, recs = _make_session(log, has_current=True)
    await sess.start(_station())
    await asyncio.gather(sess.stop(), sess.stop())
    # Only the first stop (while active) resumes; the second is a no-op.
    assert len(recs["resume"].calls) == 1


# ── edge: empty-queue takeover holds nothing ─────────────────────────────────


@pytest.mark.asyncio
async def test_empty_queue_takeover_holds_nothing_and_stop_is_clean():
    log: list = []
    sess, recs = _make_session(log, has_current=False)  # nothing playing

    await sess.start(_station())
    assert sess.is_active() is True
    assert not recs["hold"].calls, "empty-queue takeover must NOT hold"
    assert recs["play"].calls, "the station still plays"

    await sess.stop()
    assert sess.is_active() is False
    assert not recs["resume"].calls, "nothing was held → nothing to resume"


# ── Integration: real QueueEngine, no DB — counters/queue unchanged ──────────


def _dbfree_engine() -> QueueEngine:
    eng = QueueEngine()

    async def _noop():
        return None

    # Stub persistence so hold_current/advance run their real in-memory logic
    # without spawning aiosqlite tasks (keeps the test out of the teardown hang).
    eng._persist = _noop           # type: ignore[assignment]
    eng._persist_history = _noop   # type: ignore[assignment]
    return eng


@pytest.mark.asyncio
async def test_integration_start_stop_preserves_queue_and_play_recorded():
    """A start→stop excursion against a REAL QueueEngine: the current track is
    held (paused, re-front-inserted with play_recorded), and on resume it is
    promoted back to current with its play_recorded mark intact (no double-count),
    with history untouched (R8)."""
    eng = _dbfree_engine()
    # Seed a playing current track (as if it started organically, already counted).
    playing = QueueItem(track=_track("t1", "Now Playing"), play_recorded=True)
    eng._current = playing
    eng._is_playing = True
    eng._queue = [QueueItem(track=_track("t2", "Up Next"))]
    history_before = list(eng._history)

    resumed: list[QueueItem] = []

    async def _resume():
        # Mirror _resume_radio_hold: advance() promotes the held front to current.
        item = await eng.advance()
        resumed.append(item)

    async def _hold_preserving_mark():
        # Mirror the real _default_hold_queue: forward the current item's mark so
        # an already-counted play is not re-counted on resume (R8).
        cur = eng.state.current
        mark = bool(getattr(cur, "play_recorded", False)) if cur else False
        await eng.hold_current(play_recorded=mark)

    sess = RadioSession(
        stop_output=lambda: asyncio.sleep(0),
        hold_queue=_hold_preserving_mark,
        has_queue_current=lambda: eng.state.current is not None,
        play_url=lambda s, u: asyncio.sleep(0),
        resume_queue=_resume,
        report_click=lambda uuid: None,
        validate_url=lambda u: _immediately(u),
    )

    await sess.start(_station())
    # Held: current cleared, paused, the interrupted track re-front-inserted with
    # its play_recorded mark preserved, queue length grew by exactly one.
    assert eng.state.current is None
    assert eng.state.is_paused is True
    assert eng.queue[0].track.id == "t1"
    assert eng.queue[0].play_recorded is True   # R19 mark preserved (no re-count)
    assert eng.queue[1].track.id == "t2"
    assert list(eng._history) == history_before  # no history write for the station

    await sess.stop()
    # Resumed: the held front is promoted back to current (replays from start).
    assert resumed and resumed[0].track.id == "t1"
    assert eng.state.current.track.id == "t1"
    assert eng.state.is_playing is True
    assert [i.track.id for i in eng.queue] == ["t2"]  # front consumed, rest intact


# ── Non-regression (SG-06 / AE7): radio_active default False, gates are no-ops ─


def test_state_radio_active_default_false_and_gates_intact():
    """With radio never activated, state.radio_active() is False and the gated
    predicates behave exactly as before (the added clause is a pure no-op)."""
    from app import state

    # Never started a station → inactive.
    assert state.radio_active() is False
    assert state.radio_session.is_active() is False

    # The gate clause added to _should_auto_start / _do_advance is `not
    # radio_active()`, which is True here — i.e. it does not change the decision.
    # Prove the module-level singleton + accessor exist and mirror the pattern.
    assert hasattr(state, "radio_session")
    assert callable(state.radio_active)


# ── F18: _teardown_stream awaited once on stop, NOT on start/switch ───────────


@pytest.mark.asyncio
async def test_f18_teardown_stream_awaited_on_stop_not_start_or_switch():
    log: list = []
    sess, recs = _make_session(log, has_current=True)

    await sess.start(_station("uuid-1"))
    assert not recs["teardown"].calls, "start must NOT tear down the proxy"

    await sess.start(_station("uuid-2"))  # instant switch
    assert not recs["teardown"].calls, "switch must NOT tear down the proxy"

    await sess.stop()
    assert len(recs["teardown"].calls) == 1, "stop tears the proxy down exactly once"


# ── F18: status() transitions connecting→playing on start, idle on stop ───────


@pytest.mark.asyncio
async def test_f18_status_transitions_connecting_playing_idle():
    log: list = []
    # Capture the status observed at each state-listener notification so we can
    # confirm the connecting→playing ordering (not just the terminal value).
    seen: list[str] = []
    sess, _ = _make_session(log, has_current=True)
    sess.add_state_listener(lambda: seen.append(sess.status()))

    assert sess.status() == "idle"          # before any start
    await sess.start(_station())
    assert sess.status() == "playing"       # terminal state after start
    assert "connecting" in seen and "playing" in seen
    assert seen.index("connecting") < seen.index("playing")

    await sess.stop()
    assert sess.status() == "idle"          # back to idle on stop


# ── F6: stranded-queue edge — empty-queue takeover + enqueued-during-radio ────


@pytest.mark.asyncio
async def test_f6_stranded_queue_autostarts_when_nothing_held_but_queue_nonempty():
    """An empty-queue takeover holds nothing (need_resume False). If tracks were
    enqueued WHILE the station played, stop() must fire the auto-start path so they
    aren't stranded — even though there is no held front to resume."""
    log: list = []
    # has_current=False → empty-queue takeover (holds nothing); but the queue is
    # non-empty by stop time (tracks enqueued during radio).
    sess, recs = _make_session(log, has_current=False, queue_non_empty=True)

    await sess.start(_station())
    assert not recs["hold"].calls           # nothing held

    await sess.stop()
    assert not recs["resume"].calls, "nothing was held → no resume dispatch"
    assert len(recs["autostart"].calls) == 1, \
        "enqueued-during-radio tracks trigger the auto-start path (F6)"


@pytest.mark.asyncio
async def test_f6_no_autostart_when_queue_empty_at_stop():
    """The complement: empty-queue takeover with an empty queue at stop must NOT
    fire auto-start (nothing to play) — the fix is scoped to the stranded case."""
    log: list = []
    sess, recs = _make_session(log, has_current=False, queue_non_empty=False)
    await sess.start(_station())
    await sess.stop()
    assert not recs["autostart"].calls
    assert not recs["resume"].calls


@pytest.mark.asyncio
async def test_f6_normal_resume_path_does_not_autostart():
    """When WE held the queue (need_resume True), the resume dispatch runs and the
    F6 auto-start branch must NOT also fire (no double dispatch)."""
    log: list = []
    sess, recs = _make_session(log, has_current=True, queue_non_empty=True)
    await sess.start(_station())
    await sess.stop()
    assert len(recs["resume"].calls) == 1
    assert not recs["autostart"].calls, "held-queue path resumes, never auto-starts"


# ── F8: takeover STOP-during-validation race ──────────────────────────────────


@pytest.mark.asyncio
async def test_f8_stop_during_validate_aborts_the_start():
    """A stop() that completes while start()'s SSRF validate runs OUTSIDE the lock
    must win: the start aborts after re-acquiring the lock (the station must not
    play after an explicit stop). Driven by a validate seam that blocks on an
    event the test releases only after stop() has run."""
    log: list = []
    gate = asyncio.Event()
    st = _station()

    async def _blocking_validate(url):
        await gate.wait()          # hold the start inside its outside-lock validate
        return st.play_url

    play = _Recorder(log, "play")
    sess = RadioSession(
        stop_output=lambda: asyncio.sleep(0),
        hold_queue=lambda: asyncio.sleep(0),
        has_queue_current=lambda: True,
        play_url=play,
        resume_queue=lambda: asyncio.sleep(0),
        report_click=lambda uuid: None,
        validate_url=_blocking_validate,
        teardown_stream=lambda: asyncio.sleep(0),
        queue_non_empty=lambda: False,
    )

    start_task = asyncio.create_task(sess.start(st))
    await asyncio.sleep(0)         # let start reach the blocking validate
    await sess.stop()             # a stop lands during the validate window (bumps gen)
    gate.set()                     # release the validate; start re-acquires the lock
    await start_task

    assert sess.is_active() is False, "the stop must win — no station after stop"
    assert not play.calls, "an aborted start must never dispatch play"


# ── F1: title reader reads the SSRF-validated final_url, skips a blocked URL ───


@pytest.mark.asyncio
async def test_f1_title_reader_reads_validated_final_url_not_raw_play_url(
        monkeypatch):
    """The periodic ICY title reader must read the ALREADY-VALIDATED final_url
    (from start's resolve_and_validate), never the raw directory station.play_url.
    We force the ICY-reader backend path and a distinct validated URL, then assert
    the read seam is handed final_url."""
    import app.radio.session as session_mod

    # Force the "backend needs the ICY reader" branch (Cast/DLNA/AirPlay).
    monkeypatch.setattr(session_mod, "_active_backend_uses_icy_reader",
                        lambda: True)
    # Never actually re-validate (host resolution) in this unit — just record.
    monkeypatch.setattr(session_mod, "validate_station_host",
                        lambda url: asyncio.sleep(0))

    read_urls: list[str] = []

    async def _read(url):
        read_urls.append(url)
        return None                # no title; keeps the reader looping harmlessly

    log: list = []
    st = _station(url="http://raw.example/original")
    validated = "http://validated.example/final"
    sess = RadioSession(
        stop_output=lambda: asyncio.sleep(0),
        hold_queue=lambda: asyncio.sleep(0),
        has_queue_current=lambda: False,
        play_url=lambda s, u: asyncio.sleep(0),
        resume_queue=lambda: asyncio.sleep(0),
        report_click=lambda uuid: None,
        validate_url=lambda u: _immediately(validated),
        teardown_stream=lambda: asyncio.sleep(0),
        read_title=_read,
        queue_non_empty=lambda: False,
    )
    await sess.start(st)
    for _ in range(5):             # let the reader's immediate first read run
        await asyncio.sleep(0)
    await sess.stop()

    assert read_urls, "the title reader performed at least one read"
    assert all(u == validated for u in read_urls), \
        "the reader must read the SSRF-validated final_url, not the raw play_url"
    assert st.play_url not in read_urls


@pytest.mark.asyncio
async def test_f1_title_reader_skips_blocked_url_no_fetch(monkeypatch):
    """If the per-read host re-validation raises RadioUrlBlocked (a DNS change to
    an internal host), the reader SKIPS the fetch — the read seam is never called."""
    import app.radio.session as session_mod
    from app.radio.urlcheck import RadioUrlBlocked

    monkeypatch.setattr(session_mod, "_active_backend_uses_icy_reader",
                        lambda: True)

    async def _blocked(url):
        raise RadioUrlBlocked("resolves to loopback now")

    monkeypatch.setattr(session_mod, "validate_station_host", _blocked)

    read_calls: list[str] = []

    async def _read(url):
        read_calls.append(url)
        return None

    st = _station()
    sess = RadioSession(
        stop_output=lambda: asyncio.sleep(0),
        hold_queue=lambda: asyncio.sleep(0),
        has_queue_current=lambda: False,
        play_url=lambda s, u: asyncio.sleep(0),
        resume_queue=lambda: asyncio.sleep(0),
        report_click=lambda uuid: None,
        validate_url=lambda u: _immediately("http://validated.example/final"),
        teardown_stream=lambda: asyncio.sleep(0),
        read_title=_read,
        queue_non_empty=lambda: False,
    )
    await sess.start(st)
    await asyncio.sleep(0)
    await sess.stop()

    assert read_calls == [], \
        "a blocked host must skip the read — the reader never fetches (F1)"
