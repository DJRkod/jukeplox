"""Tests for PlexPlayerBackend (2026-08-04-002 plan, U2) — the Companion
protocol client is fully faked with scripted timeline sequences.

Style mirrors tests/test_output_dlna.py: the real output-session supervisor
rides the ``fresh_supervisor`` fixture (fake timers + MagicMock record_play);
module-level ``notify_outage`` is patched where the outage signal itself is
the assertion target. FakePlayerClient replays a scripted list of
TimelineSnapshot objects / exceptions through ``poll_timeline`` so every
poll-loop scenario (confirm, outage, watchdog, teardown) is deterministic.
"""

import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.models import Track
from app.plex.companion import (
    CompanionParseError,
    CompanionRequestError,
    CompanionTargetMismatchError,
    CompanionUnreachableError,
    CompanionPlayer,
    PlayQueueItem,
    PlayQueueWindow,
    TimelineSnapshot,
)


def make_track(track_id="srv-A:42", duration_ms=0, title="Song") -> Track:
    """duration_ms defaults to 0 so play() does NOT arm the duration watchdog
    — tests that drive the poll loop under a patched asyncio.sleep would
    otherwise race an instantly-firing watchdog. Watchdog tests pass a real
    duration and manage the task explicitly."""
    return Track(id=track_id, title=title, artist="A", album="B",
                 duration_ms=duration_ms)


def snap(state=None, t=None, dur=None, qid=None, item_id=None, vol=None,
         cmd_id=None, rk=None) -> TimelineSnapshot:
    return TimelineSnapshot(
        state=state, time=t, duration=dur, play_queue_id=qid,
        play_queue_item_id=item_id, volume=vol, command_id=cmd_id,
        rating_key=rk,
    )


def win(qid=50, *items) -> PlayQueueWindow:
    """PlayQueueWindow from (item_id, rating_key) pairs, in queue order."""
    return PlayQueueWindow(
        play_queue_id=qid,
        items=tuple(PlayQueueItem(play_queue_item_id=i, rating_key=rk)
                    for i, rk in items),
    )


class FakePmsClient:
    """Scripted PmsCompanionClient stand-in for the U7 play-queue window ops
    (append / delete / window read). ``window`` is returned by every op;
    ``append_error`` / ``delete_error`` raise instead."""

    def __init__(self, window=None, append_error=None, delete_error=None):
        self.window = window
        self.append_error = append_error
        self.delete_error = delete_error
        self.calls = []
        self.closed = False

    async def append_to_play_queue(self, play_queue_id, uri, *,
                                   play_next=False):
        self.calls.append(("append", play_queue_id, uri, play_next))
        if self.append_error is not None:
            raise self.append_error
        return self.window or PlayQueueWindow()

    async def get_play_queue(self, play_queue_id, **kwargs):
        self.calls.append(("get", play_queue_id))
        return self.window or PlayQueueWindow()

    async def delete_play_queue_item(self, play_queue_id, item_id):
        self.calls.append(("delete", play_queue_id, item_id))
        if self.delete_error is not None:
            raise self.delete_error
        return self.window or PlayQueueWindow()

    def op_names(self):
        return [name for name, *_ in self.calls]

    async def aclose(self):
        self.closed = True


def _pms_factory_for(pms):
    async def factory(server_machine_id):
        return pms
    return factory


class FakePlayerClient:
    """Scripted CompanionPlayerClient stand-in.

    ``timeline_script`` items are consumed one per ``poll_timeline`` call:
    a TimelineSnapshot is returned, an Exception instance is raised.
    ``on_exhausted``: "raise" (default) fails loudly on a miscounted script;
    "block" parks forever like a real long-poll with no timeline ticks
    (play()-focused tests use it and cancel the poll at teardown)."""

    def __init__(self, timeline_script=None, on_exhausted="raise"):
        self._script = list(timeline_script or [])
        self._on_exhausted = on_exhausted
        self.calls = []
        self.closed = False
        self.command_error = None   # raised by create_play_queue when set
        self.stop_error = None      # raised by stop when set

    async def create_play_queue(self, **kwargs):
        self.calls.append(("create_play_queue", kwargs))
        if self.command_error is not None:
            raise self.command_error

    async def play(self):
        self.calls.append(("play", None))

    async def pause(self):
        self.calls.append(("pause", None))

    async def stop(self):
        self.calls.append(("stop", None))
        if self.stop_error is not None:
            raise self.stop_error

    async def seek_to(self, offset_ms):
        self.calls.append(("seek_to", offset_ms))

    async def set_parameters(self, **kwargs):
        self.calls.append(("set_parameters", kwargs))

    async def refresh_play_queue(self, play_queue_id):
        self.calls.append(("refresh_play_queue", play_queue_id))

    async def poll_timeline(self, wait=0):
        self.calls.append(("poll_timeline", wait))
        if not self._script:
            if self._on_exhausted == "block":
                await asyncio.Event().wait()  # a tickless long-poll
            raise AssertionError(
                "timeline script exhausted — script exactly the reads your "
                "scenario needs")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def command_names(self):
        return [name for name, _ in self.calls]

    async def aclose(self):
        self.closed = True


async def _default_rating_key_resolver(machine_id, key_part, track):
    return "999"


def _server_info(machine_id="srv-A"):
    from app.output.plexplayer import ServerInfo
    return ServerInfo(machine_id=machine_id, protocol="http",
                      address="192.168.1.10", port=32400)


async def _default_server_info_resolver(machine_id):
    return _server_info(machine_id)


def make_backend(script=None, advance_cb=None, client=None,
                 on_exhausted="raise", device_id="player-1", **overrides):
    """Backend + fake client bound directly into a _PlayerSession — the
    DLNA-test style of assigning private attrs instead of walking
    set_device (which persists settings). ``_device_id`` is deliberately
    left None so set_volume's persistence branch never touches the DB."""
    from app.output.plexplayer import PlexPlayerBackend, _PlayerSession
    fake = client or FakePlayerClient(script, on_exhausted=on_exhausted)
    backend = PlexPlayerBackend(
        advance_cb=advance_cb,
        rating_key_resolver=overrides.pop(
            "rating_key_resolver", _default_rating_key_resolver),
        server_info_resolver=overrides.pop(
            "server_info_resolver", _default_server_info_resolver),
        clients_source=overrides.pop("clients_source", None),
        client_factory=overrides.pop("client_factory", None),
        pms_factory=overrides.pop("pms_factory", None),
        is_current=overrides.pop("is_current", None),
    )
    backend._session = _PlayerSession(device_id=device_id, client=fake)
    return backend, fake


def _adopt(sess, *, qid=50, server="srv-A", cur_item=1):
    """Bind an adopted, owned play queue onto a test session (the state a
    confirmed dispatch reaches after its first timeline evidence)."""
    sess.current_queue_id = qid
    sess.owned_queue_ids = {qid}
    sess.server_machine_id = server
    sess.current_item_id = cur_item
    sess.awaiting_queue_adoption = False


def _arm(sess, *, qid=50, armed_item=2, rating_key="99", window=(1, 2),
         title="Next", duration_ms=0):
    """Install a delivered arm slot (what _arm_device_side records)."""
    track = Track(id=f"{sess.server_machine_id or 'srv-A'}:{rating_key}",
                  title=title, artist="A", album="B",
                  duration_ms=duration_ms)
    sess.armed_track = track
    sess.armed_item_id = armed_item
    sess.armed_rating_key = rating_key
    sess.armed_queue_id = qid
    sess.queue_window = tuple(window)
    return track


async def _teardown(backend):
    """End-of-test hygiene: cancel any poll/watchdog task the scenario left
    running (repo process-hygiene policy — no pending-task warnings)."""
    sess = backend._session
    if sess is not None:
        backend._cancel_poll(sess)
        backend._cancel_watchdog(sess)
    await asyncio.sleep(0)


# ── holder-key parsing ────────────────────────────────────────────────────────

def test_parse_holder_key_splits_machine_id_prefix():
    from app.output.plexplayer import parse_holder_key
    assert parse_holder_key("srv-A:42") == ("srv-A", "42")
    assert parse_holder_key("srv-B:/library/parts/1/2/f.flac") == (
        "srv-B", "/library/parts/1/2/f.flac")


def test_parse_holder_key_without_prefix_raises_track_error():
    from app.output.plexplayer import HolderResolutionError, parse_holder_key
    with pytest.raises(HolderResolutionError):
        parse_holder_key("no-colon-here")
    with pytest.raises(HolderResolutionError):
        parse_holder_key("")


# ── dispatch (play) ───────────────────────────────────────────────────────────

async def test_play_no_device_raises_device_not_ready():
    from app.output.base import DeviceNotReadyError
    from app.output.plexplayer import PlexPlayerBackend
    backend = PlexPlayerBackend()
    with pytest.raises(DeviceNotReadyError):
        await backend.play("http://ignored", make_track())


async def test_play_dispatches_create_play_queue_from_metadata_id():
    """Native single-Plex path: no dispatch holder was handed over, so the
    backend parses metadata.id ("{machine_id}:{ratingKey}") and sends the
    documented createPlayQueue param set with the OWNING server's
    coordinates from the fresh server_info_resolver call."""
    backend, fake = make_backend(on_exhausted="block")
    await backend.play("http://ignored", make_track("srv-A:42"))
    creates = [kw for name, kw in fake.calls if name == "create_play_queue"]
    assert len(creates) == 1
    kw = creates[0]
    assert kw["server_machine_id"] == "srv-A"
    assert kw["key"] == "/library/metadata/42"
    assert kw["server_protocol"] == "http"
    assert kw["server_address"] == "192.168.1.10"
    assert kw["server_port"] == 32400
    assert backend.is_playing is True
    await _teardown(backend)


async def test_play_uses_dispatch_holder_key_and_consumes_it():
    """The state layer's per-attempt holder is the one dispatched (single
    selection authority): set_dispatch_holder wins over metadata.id, and the
    handed key is consumed one-shot — the next play() without a fresh
    handoff falls back to metadata.id again."""
    resolver_calls = []

    async def resolver(machine_id, key_part, track):
        resolver_calls.append((machine_id, key_part))
        return "777"

    backend, fake = make_backend(on_exhausted="block",
                                 rating_key_resolver=resolver)
    backend.set_dispatch_holder("srv-B:/library/parts/9/f.flac")
    await backend.play("http://ignored", make_track("srv-A:42"))
    kw = fake.calls[-1][1]
    assert kw["server_machine_id"] == "srv-B"
    assert kw["key"] == "/library/metadata/777"
    assert resolver_calls == [("srv-B", "/library/parts/9/f.flac")]
    # One-shot: consumed — the next dispatch is back on the native path.
    await backend.play("http://ignored", make_track("srv-A:42"))
    kw = [k for n, k in fake.calls if n == "create_play_queue"][-1]
    assert kw["server_machine_id"] == "srv-A"
    assert kw["key"] == "/library/metadata/42"
    await _teardown(backend)


async def test_play_rating_key_direct_when_not_part_path():
    """A holder key part that is not a part-path IS the rating key — the
    resolver must not be consulted (native shape)."""
    resolver = AsyncMock()
    backend, fake = make_backend(on_exhausted="block",
                                 rating_key_resolver=resolver)
    backend.set_dispatch_holder("srv-A:1234")
    await backend.play("http://ignored", make_track("srv-A:42"))
    resolver.assert_not_called()
    assert fake.calls[-1][1]["key"] == "/library/metadata/1234"
    await _teardown(backend)


async def test_play_resolver_failure_raises_track_error_without_dispatch():
    """Rating-key resolution failure is a TRACK-level typed error: the
    holder is consumed and _play_with_fallback continues — never an outage,
    and no command reaches the player."""
    from app.output.base import DeviceNotReadyError
    from app.output.plexplayer import HolderResolutionError

    async def resolver(machine_id, key_part, track):
        return None

    backend, fake = make_backend(rating_key_resolver=resolver)
    backend.set_dispatch_holder("srv-A:/library/parts/9/f.flac")
    with pytest.raises(HolderResolutionError) as exc_info:
        await backend.play("http://ignored", make_track())
    assert not isinstance(exc_info.value, DeviceNotReadyError)
    assert fake.calls == []
    # The handed holder was consumed even though the attempt failed.
    assert backend._dispatch_holder_key is None


async def test_play_unknown_server_raises_track_error():
    """server_info_resolver returning None (unknown/removed server) consumes
    the holder as a track-level failure — the fallback loop moves on."""
    from app.output.base import DeviceNotReadyError
    from app.output.plexplayer import PlexPlayerTrackError

    async def no_server(machine_id):
        return None

    backend, fake = make_backend(server_info_resolver=no_server)
    with pytest.raises(PlexPlayerTrackError) as exc_info:
        await backend.play("http://ignored", make_track("srv-gone:42"))
    assert not isinstance(exc_info.value, DeviceNotReadyError)
    assert fake.calls == []


async def test_play_player_unreachable_raises_device_lost():
    """createPlayQueue transport failure = the PLAYER is gone — typed
    device-level (DeviceLostError → supervisor outage hold, queue kept)."""
    from app.output.base import DeviceLostError
    backend, fake = make_backend()
    fake.command_error = CompanionUnreachableError("connect timeout")
    with pytest.raises(DeviceLostError):
        await backend.play("http://ignored", make_track())
    assert backend.is_playing is False


async def test_play_target_mismatch_raises_device_lost():
    """404 target-identifier mismatch = stale address for this player —
    device-level, same posture as unreachable."""
    from app.output.base import DeviceLostError
    backend, fake = make_backend()
    fake.command_error = CompanionTargetMismatchError("stale addr")
    with pytest.raises(DeviceLostError):
        await backend.play("http://ignored", make_track())


async def test_play_player_command_rejection_is_track_level():
    """A reachable player refusing the command (non-2xx) is a track/holder
    failure — the current attempt's server/uri may be bad; fallback owns it
    (never an outage while the player answers)."""
    from app.output.base import DeviceNotReadyError
    from app.output.plexplayer import PlexPlayerTrackError
    backend, fake = make_backend()
    fake.command_error = CompanionRequestError("HTTP 500", status_code=500)
    with pytest.raises(PlexPlayerTrackError) as exc_info:
        await backend.play("http://ignored", make_track())
    assert not isinstance(exc_info.value, DeviceNotReadyError)


async def test_play_captures_confirm_token(fresh_supervisor):
    """play() captures the supervisor's per-dispatch token at entry (DLNA
    dlna.py:727 pattern); an accepted createPlayQueue command alone must not
    count the play (control-plane success ≠ data plane)."""
    sup, timers, rec = fresh_supervisor
    backend, fake = make_backend(on_exhausted="block")
    token = sup.on_dispatched(make_track())
    await backend.play("http://ignored", make_track())
    assert backend._session.confirm_token == token
    rec.assert_not_called()
    await _teardown(backend)


async def test_play_cancels_previous_poll_and_watchdog():
    backend, fake = make_backend(on_exhausted="block")
    await backend.play("http://ignored", make_track(duration_ms=180000))
    first_poll = backend._session.poll_task
    first_watchdog = backend._session.watchdog_task
    assert first_watchdog is not None
    await backend.play("http://ignored", make_track(duration_ms=180000))
    await asyncio.sleep(0)
    assert first_poll.cancelled() or first_poll.done()
    assert first_watchdog.cancelled() or first_watchdog.done()
    await _teardown(backend)


# ── timeline poll loop: confirmed start ───────────────────────────────────────

async def test_poll_confirms_exactly_once_on_playing(fresh_supervisor):
    """First snapshot with state=playing confirms the dispatch — exactly
    once per dispatch; buffering before it is not evidence."""
    sup, timers, rec = fresh_supervisor
    backend, fake = make_backend(script=[
        snap("buffering"),          # pre-playback — no confirm
        snap("playing", t=0),       # confirmed start
        snap("playing", t=1500),    # already confirmed — no re-emit
        snap("stopped"),
        snap("stopped"),            # natural EOS — loop exits
    ], advance_cb=AsyncMock())
    sess = backend._session
    sess.confirm_token = sup.on_dispatched(make_track())
    backend._is_playing = True
    with patch("asyncio.sleep", AsyncMock()):
        await backend._poll_timeline(sess)
    rec.assert_called_once()
    assert sess.confirm_token is None   # one-shot


async def test_poll_buffering_flap_no_double_confirm_or_advance(fresh_supervisor):
    """playing → buffering → playing flaps neither re-confirm nor advance;
    the single advance is the real 2x-stopped EOS at the end."""
    sup, timers, rec = fresh_supervisor
    advance = AsyncMock()
    backend, fake = make_backend(script=[
        snap("playing", t=0),
        snap("buffering"),
        snap("playing", t=4000),
        snap("buffering"),
        snap("playing", t=9000),
        snap("stopped"),
        snap("stopped"),
    ], advance_cb=advance)
    sess = backend._session
    sess.confirm_token = sup.on_dispatched(make_track())
    backend._is_playing = True
    with patch("asyncio.sleep", AsyncMock()):
        await backend._poll_timeline(sess)
    rec.assert_called_once()
    assert advance.await_count == 1


async def test_poll_transient_single_stopped_does_not_advance():
    """A lone stopped read between playing reads (device hiccup) must not
    advance — 2 CONSECUTIVE stopped reads are required (Chromecast-stall /
    DLNA transient-STOPPED lesson)."""
    advance = AsyncMock()
    backend, fake = make_backend(script=[
        snap("playing", t=0),
        snap("stopped"),            # transient — ignored
        snap("playing", t=5000),    # counter resets
        snap("stopped"),
        snap("stopped"),            # real EOS
    ], advance_cb=advance)
    backend._is_playing = True
    with patch("asyncio.sleep", AsyncMock()):
        await backend._poll_timeline(backend._session)
    assert advance.await_count == 1


# ── timeline poll loop: natural EOS ───────────────────────────────────────────

async def test_poll_two_consecutive_stopped_advances_with_ref_cleared():
    """Natural stop: 2 consecutive stopped reads advance exactly once, and
    the poll-task ref is cleared BEFORE advance_cb is awaited so the
    downstream play() → _cancel_poll() can never cancel its own caller
    (dlna.py:922-933 self-cancel guard)."""
    backend, fake = make_backend(script=[
        snap("stopped"),
        snap("stopped"),
    ])
    sess = backend._session
    backend._is_playing = True
    sentinel_task = MagicMock()
    sentinel_task.done.return_value = False
    sess.poll_task = sentinel_task
    poll_task_at_advance = []

    async def advance():
        poll_task_at_advance.append(sess.poll_task)

    backend._advance_cb = advance
    with patch("asyncio.sleep", AsyncMock()):
        await backend._poll_timeline(sess)
    assert poll_task_at_advance == [None], (
        "poll-task ref must be cleared before advance_cb is awaited")
    sentinel_task.cancel.assert_not_called()
    assert backend.is_playing is False


# ── timeline poll loop: self-induced stop ─────────────────────────────────────

async def test_poll_self_induced_stop_never_advances():
    """Our own stop()/teardown sets the self-stopped flag — a stopped read
    after it must NOT advance (terminal-state matrix: self-induced ≠
    natural)."""
    advance = AsyncMock()
    backend, fake = make_backend(script=[
        snap("stopped"),
        snap("stopped"),
        snap("stopped"),
    ], advance_cb=advance)
    sess = backend._session
    sess.self_stopped = True
    backend._is_playing = True
    with patch("asyncio.sleep", AsyncMock()):
        await backend._poll_timeline(sess)
    advance.assert_not_called()
    assert sess.poll_task is None


async def test_stop_then_poll_task_no_advance():
    """Integration shape: stop() cancels the running poll task FIRST, so a
    stopped timeline queued behind it can never fire an advance."""
    advance = AsyncMock()
    backend, fake = make_backend(script=[
        snap("stopped"),   # verify read consumed by stop()
        snap("stopped"),
        snap("stopped"),
    ], advance_cb=advance)
    sess = backend._session
    backend._is_playing = True
    sess.poll_task = asyncio.create_task(backend._poll_timeline(sess))
    await backend.stop()
    await asyncio.sleep(0)
    advance.assert_not_called()
    assert sess.self_stopped is True


# ── timeline poll loop: outage (AE6) ──────────────────────────────────────────

async def test_poll_three_unreachable_reports_outage_no_advance():
    """AE6: player unreachable for 3 consecutive polls mid-track →
    notify_outage("poll_errors"); the queue is preserved (no advance), the
    adopted play-queue bookkeeping survives for reconnect reconciliation."""
    advance = AsyncMock()
    backend, fake = make_backend(script=[
        snap("playing", t=0, qid=7),
        CompanionUnreachableError("gone 1"),
        CompanionUnreachableError("gone 2"),
        CompanionUnreachableError("gone 3"),
    ], advance_cb=advance)
    sess = backend._session
    sess.awaiting_queue_adoption = True
    backend._is_playing = True
    with patch("asyncio.sleep", AsyncMock()), \
         patch("app.output.session.notify_outage") as notify:
        await backend._poll_timeline(sess)
    notify.assert_called_once_with("poll_errors")
    advance.assert_not_called()
    assert backend.is_playing is False
    assert sess.poll_task is None
    assert sess.current_queue_id == 7   # queue bookkeeping preserved


async def test_poll_error_count_resets_on_successful_read():
    """Two failures, a good read, then three failures: the outage fires only
    at the LATER 3-consecutive run — a flaky link never accumulates."""
    advance = AsyncMock()
    backend, fake = make_backend(script=[
        CompanionUnreachableError("1"),
        CompanionUnreachableError("2"),
        snap("playing", t=1000),
        CompanionUnreachableError("3"),
        CompanionUnreachableError("4"),
        CompanionUnreachableError("5"),
    ], advance_cb=advance)
    backend._is_playing = True
    with patch("asyncio.sleep", AsyncMock()), \
         patch("app.output.session.notify_outage") as notify:
        await backend._poll_timeline(backend._session)
    notify.assert_called_once_with("poll_errors")
    advance.assert_not_called()


async def test_poll_parse_error_falls_back_to_wait0_and_does_not_count():
    """A long-poll body that won't parse retries the SAME tick with wait=0
    (the plan's fallback) and never counts toward the outage budget — the
    player is reachable, the body is just garbled."""
    backend, fake = make_backend(script=[
        CompanionParseError("garbled long-poll"),   # wait=1 read
        snap("playing", t=0),                        # wait=0 fallback read
        snap("stopped"),
        snap("stopped"),
    ], advance_cb=AsyncMock())
    backend._is_playing = True
    with patch("asyncio.sleep", AsyncMock()), \
         patch("app.output.session.notify_outage") as notify:
        await backend._poll_timeline(backend._session)
    notify.assert_not_called()
    waits = [w for name, w in fake.calls if name == "poll_timeline"]
    assert waits[:2] == [1, 0], (
        f"expected wait=1 then wait=0 fallback, got {waits}")


async def test_poll_persistent_parse_errors_never_outage():
    backend, fake = make_backend(script=[
        CompanionParseError("a"), CompanionParseError("a0"),
        CompanionParseError("b"), CompanionParseError("b0"),
        CompanionParseError("c"), CompanionParseError("c0"),
        snap("stopped"),
        snap("stopped"),
    ], advance_cb=AsyncMock())
    backend._is_playing = True
    with patch("asyncio.sleep", AsyncMock()), \
         patch("app.output.session.notify_outage") as notify:
        await backend._poll_timeline(backend._session)
    notify.assert_not_called()


async def test_poll_timeline_gone_is_not_evidence():
    """A snapshot with no state (idle/empty timeline) is UNKNOWN — it must
    not advance, and resets the consecutive-stopped counter (strictly
    consecutive evidence only; the watchdog owns a permanently-gone
    timeline). No confirm token outstanding: a CONFIRMED dispatch's counter
    behavior is what's under test (the pre-confirmation window is a no-op
    by the PLX-4 guard, pinned separately below)."""
    advance = AsyncMock()
    backend, fake = make_backend(script=[
        snap(None),          # timeline-gone: not evidence
        snap("stopped"),
        snap(None),          # breaks the consecutive run
        snap("stopped"),
        snap("stopped"),     # real EOS
    ], advance_cb=advance)
    sess = backend._session
    backend._is_playing = True
    with patch("asyncio.sleep", AsyncMock()):
        await backend._poll_timeline(sess)
    assert advance.await_count == 1


async def test_pre_confirmation_stopped_reads_never_advance(fresh_supervisor):
    """Review fix PLX-4: while the confirm token is outstanding (dispatch
    accepted but playback never started — dead server, slow queue load),
    'stopped' reads are steady pre-playback state, NOT terminal evidence.
    No advance may fire — the supervisor's confirm deadline + probe own
    that window (a burned queue would pop entries as finished with the
    other holders never tried)."""
    sup, timers, rec = fresh_supervisor
    advance = AsyncMock()
    backend, fake = make_backend(script=[
        snap("stopped"),
        snap("stopped"),
        snap("stopped"),
        snap("stopped"),
        CompanionUnreachableError("end 1"),
        CompanionUnreachableError("end 2"),
        CompanionUnreachableError("end 3"),   # bounded loop exit for the test
    ], advance_cb=advance)
    sess = backend._session
    sess.confirm_token = sup.on_dispatched(make_track())
    sess.awaiting_queue_adoption = True       # the real play() posture
    backend._is_playing = True
    with patch("asyncio.sleep", AsyncMock()), \
         patch("app.output.session.notify_outage"):
        await backend._poll_timeline(sess)
    advance.assert_not_called()
    rec.assert_not_called()


async def test_confirm_deadline_path_untouched_by_preconfirm_guard(fresh_supervisor):
    """PLX-4 companion: the guard only suppresses TERMINAL counting — a
    later real start still confirms once and a genuine post-confirmation
    EOS still advances exactly once."""
    sup, timers, rec = fresh_supervisor
    advance = AsyncMock()
    backend, fake = make_backend(script=[
        snap("stopped"),                     # pre-playback stopped: inert
        snap("stopped"),
        snap("playing", t=0, qid=50),        # adoption + confirmed start
        snap("stopped"),
        snap("stopped"),                     # real EOS after confirmation
    ], advance_cb=advance)
    sess = backend._session
    sess.confirm_token = sup.on_dispatched(make_track())
    sess.awaiting_queue_adoption = True
    backend._is_playing = True
    with patch("asyncio.sleep", AsyncMock()):
        await backend._poll_timeline(sess)
    rec.assert_called_once()
    assert advance.await_count == 1


async def test_confirm_requires_adopted_queue_evidence(fresh_supervisor):
    """Review fix PLX-5: a 'playing' tick that arrives BEFORE the dispatched
    queue was adopted is the OLD track still sounding (dispatch over an
    actively-playing track) — it must not consume the confirm token. The
    confirm fires on the first playing tick whose queue evidence was
    adopted as ours."""
    sup, timers, rec = fresh_supervisor
    backend, fake = make_backend(script=[
        snap("playing", t=150000, qid=40),   # OLD queue still playing
        snap("playing", t=151000, qid=40),   # still the old track
        snap("playing", t=0, qid=51),        # NEW queue adopted → confirm
        snap("stopped"),
        snap("stopped"),
    ], advance_cb=AsyncMock())
    sess = backend._session
    sess.current_queue_id = 40               # our previous dispatch's queue
    sess.owned_queue_ids = {40}
    sess.awaiting_queue_adoption = True      # fresh dispatch in flight
    sess.confirm_token = sup.on_dispatched(make_track())
    backend._is_playing = True
    confirmed_at = []
    real_confirm = MagicMock(
        side_effect=lambda tok: confirmed_at.append(sess.current_queue_id))
    with patch("asyncio.sleep", AsyncMock()), \
         patch("app.output.session.notify_confirmed", real_confirm):
        await backend._poll_timeline(sess)
    real_confirm.assert_called_once()
    assert confirmed_at == [51], (
        "confirm must fire only after the NEW queue's evidence was adopted")


# ── owned play-queue adoption (U7 seam) ───────────────────────────────────────

async def test_queue_adoption_from_first_post_dispatch_evidence():
    """The authoritative playQueueID is the queue observed after OUR
    dispatch: current + previous retained, older retired on each adoption
    (U7's foreign-verdict window builds on exactly this set)."""
    backend, fake = make_backend()
    sess = backend._session
    sess.awaiting_queue_adoption = True
    backend._adopt_queue_evidence(sess, snap("playing", qid=101))
    assert sess.current_queue_id == 101
    assert sess.owned_queue_ids == {101}
    # Re-dispatch → new queue observed → previous retained, older retired.
    sess.awaiting_queue_adoption = True
    backend._adopt_queue_evidence(sess, snap("playing", qid=202))
    assert sess.current_queue_id == 202
    assert sess.owned_queue_ids == {202, 101}
    sess.awaiting_queue_adoption = True
    backend._adopt_queue_evidence(sess, snap("playing", qid=303))
    assert sess.owned_queue_ids == {303, 202}


async def test_queue_evidence_without_dispatch_window_not_adopted():
    """A playQueueID observed OUTSIDE our dispatch-adoption window is never
    adopted as ours (player-echoed IDs alone are not ownership; the foreign
    verdict itself is U7)."""
    backend, fake = make_backend()
    sess = backend._session
    sess.awaiting_queue_adoption = False
    sess.current_queue_id = 101
    sess.owned_queue_ids = {101}
    backend._adopt_queue_evidence(sess, snap("playing", qid=666))
    assert sess.current_queue_id == 101
    assert sess.owned_queue_ids == {101}


# ── duration watchdog backstop ────────────────────────────────────────────────

async def test_watchdog_single_advance_when_reachable():
    """duration+grace expiry with no EOS/boundary ever surfacing: probe the
    player; reachable → exactly one advance, with the watchdog's own task
    ref cleared BEFORE advance_cb (self-cancel trap)."""
    backend, fake = make_backend(script=[
        snap("playing", t=170000),   # probe_liveness read: reachable
    ])
    sess = backend._session
    backend._is_playing = True
    sess.play_gen = 5
    sentinel = MagicMock()
    sentinel.done.return_value = False
    sess.watchdog_task = sentinel
    ref_at_advance = []

    async def advance():
        ref_at_advance.append(sess.watchdog_task)

    backend._advance_cb = advance
    with patch("asyncio.sleep", AsyncMock()):
        await backend._watchdog(sess, 5, 180000)
    assert ref_at_advance == [None]
    sentinel.cancel.assert_not_called()
    assert backend.is_playing is False


async def test_watchdog_stale_token_no_op():
    """A newer play() bumped the generation — the stale watchdog must do
    nothing (no probe, no advance, no outage)."""
    advance = AsyncMock()
    backend, fake = make_backend(script=[], advance_cb=advance)
    sess = backend._session
    backend._is_playing = True
    sess.play_gen = 6                      # newer dispatch
    with patch("asyncio.sleep", AsyncMock()), \
         patch("app.output.session.notify_outage") as notify:
        await backend._watchdog(sess, 5, 180000)   # armed for gen 5
    advance.assert_not_called()
    notify.assert_not_called()
    assert fake.calls == []                # not even a probe read
    assert backend.is_playing is True


async def test_watchdog_not_playing_no_op():
    advance = AsyncMock()
    backend, fake = make_backend(script=[], advance_cb=advance)
    sess = backend._session
    backend._is_playing = False
    sess.play_gen = 5
    with patch("asyncio.sleep", AsyncMock()):
        await backend._watchdog(sess, 5, 180000)
    advance.assert_not_called()
    assert fake.calls == []


async def test_watchdog_unreachable_reports_outage_not_advance():
    advance = AsyncMock()
    backend, fake = make_backend(script=[
        CompanionUnreachableError("probe failed"),
    ], advance_cb=advance)
    sess = backend._session
    backend._is_playing = True
    sess.play_gen = 5
    with patch("asyncio.sleep", AsyncMock()), \
         patch("app.output.session.notify_outage") as notify:
        await backend._watchdog(sess, 5, 180000)
    notify.assert_called_once_with("watchdog_unreachable")
    advance.assert_not_called()


async def test_play_arms_watchdog_and_stop_cancels_it():
    backend, fake = make_backend(
        script=[snap("stopped")],   # stop()'s verify read
        on_exhausted="block")
    await backend.play("http://ignored", make_track(duration_ms=180000))
    sess = backend._session
    assert sess.watchdog_task is not None
    watchdog = sess.watchdog_task
    await backend.stop()
    await asyncio.sleep(0)
    assert sess.watchdog_task is None
    assert watchdog.cancelled() or watchdog.done()
    await _teardown(backend)


async def test_play_without_duration_does_not_arm_watchdog():
    backend, fake = make_backend(on_exhausted="block")
    await backend.play("http://ignored", make_track(duration_ms=0))
    assert backend._session.watchdog_task is None
    await _teardown(backend)


# ── teardown on switch-away (stop) ────────────────────────────────────────────

async def test_stop_sends_command_and_verifies_clean():
    backend, fake = make_backend(script=[
        snap("stopped"),   # verify read: player really stopped
    ])
    backend._is_playing = True
    await backend.stop()
    assert "stop" in fake.command_names()
    assert backend.last_teardown_warning is None
    assert backend.is_playing is False


async def test_stop_verify_still_playing_sets_warning():
    """The player answered the stop but the verification read still shows
    playing — log + expose the warning (U6/U7 wire the admin notice); the
    backend still detaches cleanly."""
    backend, fake = make_backend(script=[
        snap("playing", t=42000),   # verify read: still playing!
    ])
    backend._is_playing = True
    await backend.stop()
    assert backend.last_teardown_warning is not None
    assert backend.is_playing is False


async def test_stop_command_failure_sets_warning_and_detaches():
    backend, fake = make_backend()
    fake.stop_error = CompanionUnreachableError("player gone")
    backend._is_playing = True
    await backend.stop()   # must not raise
    assert backend.last_teardown_warning is not None
    assert backend.is_playing is False
    assert backend._session is not None   # non-destructive (DLNA posture)


async def test_stop_verify_read_failure_sets_warning():
    backend, fake = make_backend(script=[
        CompanionUnreachableError("verify failed"),
    ])
    backend._is_playing = True
    await backend.stop()
    assert backend.last_teardown_warning is not None


async def test_play_clears_teardown_warning():
    backend, fake = make_backend(on_exhausted="block")
    backend.last_teardown_warning = "stale"
    await backend.play("http://ignored", make_track())
    assert backend.last_teardown_warning is None
    await _teardown(backend)


async def test_stop_without_session_is_noop():
    from app.output.plexplayer import PlexPlayerBackend
    backend = PlexPlayerBackend()
    await backend.stop()
    assert backend.is_playing is False


# ── controls ──────────────────────────────────────────────────────────────────

async def test_pause_and_resume_send_commands():
    backend, fake = make_backend()
    backend._is_playing = True
    await backend.pause()
    assert ("pause", None) in fake.calls
    assert backend.is_playing is False
    await backend.resume()
    assert ("play", None) in fake.calls
    assert backend.is_playing is True


async def test_seek_sends_seek_to_and_reanchors_position():
    backend, fake = make_backend()
    sess = backend._session
    sess.last_state = "playing"
    sess.last_time_ms = 5000
    sess.last_at = time.monotonic()
    await backend.seek(90000)
    assert ("seek_to", 90000) in fake.calls
    pos = await backend.get_position()
    assert 89000 <= pos <= 92000


async def test_seek_failure_does_not_reanchor():
    backend, fake = make_backend()

    async def failing_seek(offset_ms):
        raise CompanionUnreachableError("gone")

    fake.seek_to = failing_seek
    sess = backend._session
    sess.last_state = "paused"
    sess.last_time_ms = 5000
    sess.last_at = time.monotonic()
    await backend.seek(90000)   # must not raise (API-surface posture)
    assert (await backend.get_position()) == 5000


async def test_seek_negative_clamps_to_zero():
    backend, fake = make_backend()
    await backend.seek(-4000)
    assert ("seek_to", 0) in fake.calls


async def test_set_volume_sends_percent_and_stamps_echo_guard():
    from app.output.base import echo_guard_active
    backend, fake = make_backend()
    await backend.set_volume(0.6)
    assert ("set_parameters", {"volume": 60}) in fake.calls
    assert await backend.get_volume() == pytest.approx(0.6)
    assert echo_guard_active(backend._vol_last_set)


async def test_volume_clamped():
    backend, fake = make_backend()
    await backend.set_volume(2.0)
    assert await backend.get_volume() == 1.0
    await backend.set_volume(-1.0)
    assert await backend.get_volume() == 0.0


async def test_timeline_volume_echo_suppressed_within_guard_window():
    """A timeline volume within the echo-guard window of our own write is
    the device's confirmation echo — it must not clobber the level."""
    backend, fake = make_backend()
    await backend.set_volume(0.6)
    backend._note_snapshot(backend._session, snap("playing", t=0, vol=40))
    assert await backend.get_volume() == pytest.approx(0.6)


async def test_timeline_volume_external_change_applies_after_guard():
    backend, fake = make_backend()
    backend._volume = 0.6
    backend._vol_last_set = 0.0   # far outside the guard window
    backend._note_snapshot(backend._session, snap("playing", t=0, vol=40))
    assert await backend.get_volume() == pytest.approx(0.4)
    await asyncio.sleep(0)   # let the best-effort broadcast task settle


async def test_get_position_interpolates_only_while_playing():
    backend, fake = make_backend()
    sess = backend._session
    sess.last_state = "playing"
    sess.last_time_ms = 10000
    sess.last_at = time.monotonic() - 2.0
    pos = await backend.get_position()
    assert 11500 <= pos <= 13500   # 10000 + ~2000 interpolated
    sess.last_state = "paused"
    assert await backend.get_position() == 10000


async def test_get_position_no_session_or_no_snapshot_reads_zero():
    from app.output.plexplayer import PlexPlayerBackend
    backend = PlexPlayerBackend()
    assert await backend.get_position() == 0
    backend2, _ = make_backend()
    assert await backend2.get_position() == 0


# ── probes ────────────────────────────────────────────────────────────────────

async def test_probe_liveness_reads_timeline():
    backend, fake = make_backend(script=[snap("buffering")])
    assert await backend.probe_liveness() == (True, "buffering")
    assert fake.calls[-1] == ("poll_timeline", 0)   # cheap wait=0 read


async def test_probe_liveness_unreachable_and_unbound():
    from app.output.plexplayer import PlexPlayerBackend
    backend, fake = make_backend(script=[CompanionUnreachableError("x")])
    assert await backend.probe_liveness() == (False, None)
    assert await PlexPlayerBackend().probe_liveness() == (False, None)


# ── discovery / device binding ────────────────────────────────────────────────

def _player(mid, name="Caldera", caps=("playqueues-creation", "timeline"),
            address="192.168.1.30", port=32500):
    return CompanionPlayer(
        name=name, machine_identifier=mid, address=address, port=port,
        product="Caldera", protocol_capabilities=frozenset(caps))


async def test_discover_devices_filters_capability_and_dedupes():
    async def clients():
        return [
            _player("p1", name="Living Room"),
            _player("p1", name="Living Room"),          # seen via 2nd server
            _player("p2", name="TV App", caps=("timeline",)),  # ineligible
        ]

    backend, fake = make_backend(clients_source=clients)
    devices = await backend.discover_devices()
    assert len(devices) == 1
    d = devices[0]
    assert d.id == "p1"
    assert d.name == "Living Room"
    assert d.backend_type == "plexplayer"
    assert d.id_format == "uuid"
    assert backend._device_addresses["p1"]["host"] == "192.168.1.30"
    assert backend._device_addresses["p1"]["port"] == 32500


async def test_discover_devices_without_source_returns_empty():
    from app.output.plexplayer import PlexPlayerBackend
    backend = PlexPlayerBackend()
    assert await backend.discover_devices() == []


async def test_discover_devices_source_failure_returns_empty():
    async def broken():
        raise RuntimeError("sweep failed")

    backend, fake = make_backend(clients_source=broken)
    assert await backend.discover_devices() == []


async def test_set_device_builds_client_restores_volume_persists_addr():
    from app.output.plexplayer import PlexPlayerBackend
    built = []

    def factory(host, port, device_id):
        built.append((host, port, device_id))
        return FakePlayerClient(on_exhausted="block")

    backend = PlexPlayerBackend(client_factory=factory)
    backend._device_addresses["p1"] = {
        "host": "192.168.1.30", "port": 32500, "name": "Living Room"}
    get_setting = AsyncMock(side_effect=lambda key, default=None:
                            "0.7" if key.startswith("vol:") else None)
    set_setting = AsyncMock()
    with patch("app.database.get_setting", get_setting), \
         patch("app.database.set_setting", set_setting):
        await backend.set_device("p1")
    assert built == [("192.168.1.30", 32500, "p1")]
    assert backend._device_id == "p1"
    assert backend._session is not None
    assert await backend.get_volume() == pytest.approx(0.7)
    get_setting.assert_any_call("vol:plexplayer:p1")
    persisted = [c for c in set_setting.call_args_list
                 if c.args[0] == "output_addr:p1"]
    assert persisted, "output_addr:{device_id} must be persisted"
    import json as _json
    payload = _json.loads(persisted[0].args[1])
    assert payload["host"] == "192.168.1.30"
    assert payload["port"] == 32500


async def test_set_device_reads_persisted_address_when_cache_cold():
    """Restart re-bind: no discovery has run, the persisted
    output_addr:{device_id} JSON seeds the address."""
    import json as _json
    from app.output.plexplayer import PlexPlayerBackend
    backend = PlexPlayerBackend(
        client_factory=lambda h, p, d: FakePlayerClient(on_exhausted="block"))
    addr_json = _json.dumps(
        {"host": "192.168.1.31", "port": 32500, "name": "Caldera"})
    get_setting = AsyncMock(side_effect=lambda key, default=None:
                            addr_json if key.startswith("output_addr:") else None)
    with patch("app.database.get_setting", get_setting), \
         patch("app.database.set_setting", AsyncMock()):
        await backend.set_device("p9")
    assert backend._session is not None
    assert backend._device_addresses["p9"]["host"] == "192.168.1.31"


async def test_set_device_unknown_address_raises_device_not_ready():
    from app.output.base import DeviceNotReadyError
    from app.output.plexplayer import PlexPlayerBackend
    backend = PlexPlayerBackend(
        client_factory=lambda h, p, d: FakePlayerClient())
    with patch("app.database.get_setting", AsyncMock(return_value=None)):
        with pytest.raises(DeviceNotReadyError):
            await backend.set_device("nowhere")


async def test_set_device_tears_down_prior_session():
    from app.output.plexplayer import PlexPlayerBackend
    prior_backend, prior_client = make_backend(on_exhausted="block")
    backend = prior_backend
    backend._client_factory = (
        lambda h, p, d: FakePlayerClient(on_exhausted="block"))
    await backend.play("http://ignored", make_track())
    prior_poll = backend._session.poll_task
    backend._device_addresses["p2"] = {
        "host": "192.168.1.32", "port": 32500, "name": "Other"}
    with patch("app.database.get_setting", AsyncMock(return_value=None)), \
         patch("app.database.set_setting", AsyncMock()):
        await backend.set_device("p2")
    await asyncio.sleep(0)
    assert prior_poll.cancelled() or prior_poll.done()
    assert prior_client.closed is True
    assert backend._session.client is not prior_client
    await _teardown(backend)


# ── protocol conformance ──────────────────────────────────────────────────────

def test_backend_satisfies_output_protocol():
    from app.output.base import AbstractOutputBackend
    from app.output.plexplayer import PlexPlayerBackend
    assert isinstance(PlexPlayerBackend(), AbstractOutputBackend)


# ── U3 seams: device_host accessor + raising sweep ────────────────────────────

async def test_device_host_reads_address_cache():
    """device_host is the public read over _device_addresses the
    aggregator's host_for and the watcher's sweep-merge use (U3)."""
    async def clients():
        return [_player("p1")]

    backend, fake = make_backend(clients_source=clients)
    assert backend.device_host("p1") is None      # nothing discovered yet
    await backend.discover_devices()
    assert backend.device_host("p1") == "192.168.1.30"
    assert backend.device_host("ghost") is None


async def test_sweep_devices_raises_on_source_failure():
    """sweep_devices propagates a total clients-source failure ("no scan
    data" — the watcher leaves its registry untouched), while
    discover_devices keeps the fail-soft [] for the legacy pull path."""
    async def broken():
        raise RuntimeError("all Plex servers unreachable")

    backend, fake = make_backend(clients_source=broken)
    with pytest.raises(RuntimeError):
        await backend.sweep_devices()
    assert await backend.discover_devices() == []


async def test_sweep_devices_without_source_returns_empty():
    from app.output.plexplayer import PlexPlayerBackend
    assert await PlexPlayerBackend().sweep_devices() == []


# ══ U7: gapless arming ════════════════════════════════════════════════════════

async def test_arm_next_appends_play_next_nudges_and_records_slot():
    """Same-server arm on an adopted queue: PMS PUT-append with
    play_next=True and the server:// uri form (section uuids are not
    exposed anywhere — the documented U1 decision), player refreshPlayQueue
    nudge, armed item identified from the window, one-shot slot recorded,
    PMS client closed."""
    pms = FakePmsClient(window=win(50, (1, "42"), (2, "999")))
    backend, fake = make_backend(pms_factory=_pms_factory_for(pms))
    sess = backend._session
    _adopt(sess)
    backend._is_playing = True
    track = make_track("srv-A:999", title="Next")
    await backend.arm_next("http://ignored", track)
    assert pms.calls[0][:2] == ("append", 50)
    _, _, uri, play_next = pms.calls[0]
    assert uri == ("server://srv-A/com.plexapp.plugins.library"
                   "/library/metadata/999")
    assert play_next is True
    assert ("refresh_play_queue", 50) in fake.calls
    assert sess.armed_track is track
    assert sess.armed_item_id == 2
    assert sess.armed_rating_key == "999"
    assert sess.queue_window == (1, 2)
    assert pms.closed is True


async def test_arm_next_declines_cross_server_then_eos_fresh_dispatch():
    """Cross-server next (no rating key on the dispatch's server) → arming
    DECLINED: nothing goes device-side, and the boundary falls back to the
    per-track EOS advance (fresh dispatch via advance_cb) — never a
    gapless boundary."""
    async def no_match(machine_id, key_part, track):
        return None

    advance = AsyncMock()
    pms = FakePmsClient()
    backend, fake = make_backend(
        script=[snap("stopped"), snap("stopped")],
        advance_cb=advance, rating_key_resolver=no_match,
        pms_factory=_pms_factory_for(pms))
    sess = backend._session
    _adopt(sess)
    backend._is_playing = True
    await backend.arm_next("http://ignored", make_track("srv-B:7"))
    assert pms.calls == []
    assert sess.armed_track is None and sess.pending_arm is None
    boundary = AsyncMock()
    with patch("asyncio.sleep", AsyncMock()), \
         patch("app.output.session.notify_gapless_boundary", boundary):
        await backend._poll_timeline(sess)
    advance.assert_awaited_once()
    boundary.assert_not_called()


async def test_arm_next_stashes_until_adoption_then_delivers():
    """Arm requested before the dispatched queue is adopted → stashed; the
    poll loop delivers the append on the first adopted evidence (the DLNA
    deferred-send timing — never append into a queue we don't own)."""
    pms = FakePmsClient(window=win(50, (1, "42"), (2, "999")))
    advance = AsyncMock()
    backend, fake = make_backend(
        script=[
            snap("playing", t=0, qid=50, item_id=1),   # adoption + delivery
            snap("stopped"),
            snap("stopped"),                           # new EOS converges
        ],
        advance_cb=advance, pms_factory=_pms_factory_for(pms))
    sess = backend._session
    sess.awaiting_queue_adoption = True
    sess.server_machine_id = "srv-A"
    backend._is_playing = True
    await backend.arm_next("http://ignored", make_track("srv-A:999"))
    assert sess.pending_arm is not None
    assert pms.calls == []          # nothing device-side yet
    with patch("asyncio.sleep", AsyncMock()):
        await backend._poll_timeline(sess)
    assert pms.op_names()[0] == "append"
    assert sess.pending_arm is None
    # The stopped pair after delivery is the gapped fallback: arm discarded,
    # exactly one advance.
    advance.assert_awaited_once()
    assert sess.armed_track is None


async def test_arm_next_rejected_append_verdicts_unsupported_per_device():
    """A PMS that REJECTS the PUT-append to the player-owned queue is
    behavioral evidence: verdict 'unsupported' recorded + persisted, and
    arming stops for that device (per-track dispatch mode)."""
    pms = FakePmsClient(
        append_error=CompanionRequestError("HTTP 403", status_code=403))
    backend, fake = make_backend(pms_factory=_pms_factory_for(pms))
    backend._device_id = "player-1"
    sess = backend._session
    _adopt(sess)
    backend._is_playing = True
    set_verdict = AsyncMock()
    with patch("app.database.set_gapless_verdict", set_verdict):
        await backend.arm_next("http://ignored", make_track("srv-A:999"))
    assert sess.armed_track is None
    assert backend._gapless_verdicts.get("player-1") == "unsupported"
    set_verdict.assert_awaited_once_with("plexplayer", "player-1",
                                         "unsupported")
    # Second arm never reaches the PMS again — the cached verdict gates it.
    await backend.arm_next("http://ignored", make_track("srv-A:1000"))
    assert pms.op_names() == ["append"]


async def test_arm_next_transport_failure_declines_without_verdict():
    """An unreachable PMS at arm time is NOT capability evidence: decline
    this boundary, no verdict — the next reconcile may arm again."""
    pms = FakePmsClient(append_error=CompanionUnreachableError("down"))
    backend, fake = make_backend(pms_factory=_pms_factory_for(pms))
    backend._device_id = "player-1"
    sess = backend._session
    _adopt(sess)
    backend._is_playing = True
    await backend.arm_next("http://ignored", make_track("srv-A:999"))
    assert sess.armed_track is None
    assert backend._gapless_verdicts == {}
    await backend.arm_next("http://ignored", make_track("srv-A:999"))
    assert pms.op_names() == ["append", "append"]   # re-attempted


async def test_arm_next_requires_playing_session():
    pms = FakePmsClient()
    backend, fake = make_backend(pms_factory=_pms_factory_for(pms))
    _adopt(backend._session)
    backend._is_playing = False
    await backend.arm_next("http://ignored", make_track("srv-A:999"))
    assert pms.calls == []
    assert backend._session.armed_track is None


# ══ U7: boundary watch ════════════════════════════════════════════════════════

async def test_boundary_itemid_edge_one_boundary_no_dispatch_verdict_supported():
    """AE5 (backend half): the playQueueItemID edge onto the armed item →
    EXACTLY one notify_gapless_boundary with the armed track, no dispatch
    at the boundary (no createPlayQueue), one-shot arm discard (a repeat
    read of the same item fires nothing), and the first successful armed
    boundary verdicts the device 'supported'."""
    advance = AsyncMock()
    backend, fake = make_backend(script=[
        snap("playing", t=170000, qid=50, item_id=1),
        snap("playing", t=500, qid=50, item_id=2, rk="99"),   # the edge
        snap("playing", t=2500, qid=50, item_id=2, rk="99"),  # same item — no-op
        snap("stopped"),
        snap("stopped"),          # the NEW track's own natural EOS
    ], advance_cb=advance)
    backend._device_id = "player-1"
    sess = backend._session
    _adopt(sess)
    armed = _arm(sess)
    backend._is_playing = True
    boundary = AsyncMock()
    set_verdict = AsyncMock()
    with patch("asyncio.sleep", AsyncMock()), \
         patch("app.output.session.notify_gapless_boundary", boundary), \
         patch("app.database.set_gapless_verdict", set_verdict):
        await backend._poll_timeline(sess)
    boundary.assert_awaited_once_with(armed)
    assert not [c for c in fake.calls if c[0] == "create_play_queue"]
    assert sess.armed_track is None            # one-shot discard
    assert sess.current_item_id == 2
    assert backend._gapless_verdicts.get("player-1") == "supported"
    advance.assert_awaited_once()              # the new track's EOS only


async def test_eos_with_armed_slot_discards_and_advances_once():
    """The dlna.py:886-912 shape: the player STOPPED despite an armed next —
    the un-fired boundary's slot is discarded BEFORE the EOS fallback, so
    there is exactly ONE advance (the EOS one) and no boundary; no verdict
    (a stop is not append-capability evidence)."""
    advance = AsyncMock()
    backend, fake = make_backend(script=[
        snap("stopped"),
        snap("stopped"),
    ], advance_cb=advance)
    backend._device_id = "player-1"
    sess = backend._session
    _adopt(sess)
    _arm(sess)
    backend._is_playing = True
    boundary = AsyncMock()
    set_verdict = AsyncMock()
    with patch("asyncio.sleep", AsyncMock()), \
         patch("app.output.session.notify_gapless_boundary", boundary), \
         patch("app.database.set_gapless_verdict", set_verdict):
        await backend._poll_timeline(sess)
    advance.assert_awaited_once()
    boundary.assert_not_called()
    assert sess.armed_track is None
    set_verdict.assert_not_called()


async def test_stale_itemid_replay_never_reverses():
    """A timeline replaying an EARLIER itemID (reconnect buffers) must not
    advance backward: the window ordering reads it as behind the current
    item — ignored."""
    backend, fake = make_backend(script=[
        snap("playing", t=100, qid=50, item_id=1),   # behind current (=2)
        snap("stopped"),
        snap("stopped"),
    ], advance_cb=AsyncMock())
    sess = backend._session
    _adopt(sess, cur_item=2)
    sess.queue_window = (1, 2)
    backend._is_playing = True
    boundary = AsyncMock()
    with patch("asyncio.sleep", AsyncMock()), \
         patch("app.output.session.notify_gapless_boundary", boundary):
        await backend._poll_timeline(sess)
    boundary.assert_not_called()
    assert sess.current_item_id == 2


async def test_stale_command_echo_ignored_for_boundary():
    """commandID staleness filter: a snapshot whose commandID echo predates
    the dispatch is a replay — its itemID edge is not evidence; the same
    edge with a fresh commandID fires."""
    backend, fake = make_backend(script=[
        snap("playing", t=100, qid=50, item_id=2, rk="99", cmd_id=5),
        snap("playing", t=200, qid=50, item_id=2, rk="99", cmd_id=12),
        snap("stopped"),
        snap("stopped"),
    ], advance_cb=AsyncMock())
    sess = backend._session
    _adopt(sess)
    armed = _arm(sess)
    sess.dispatch_command_id = 10
    backend._is_playing = True
    boundary = AsyncMock()
    with patch("asyncio.sleep", AsyncMock()), \
         patch("app.output.session.notify_gapless_boundary", boundary):
        await backend._poll_timeline(sess)
    boundary.assert_awaited_once_with(armed)


def test_stale_command_echo_not_adopted():
    """The adoption window applies the same staleness floor: a replayed
    pre-dispatch snapshot must not be adopted as the new queue."""
    backend, fake = make_backend()
    sess = backend._session
    sess.awaiting_queue_adoption = True
    sess.dispatch_command_id = 10
    backend._adopt_queue_evidence(sess, snap("playing", qid=666, cmd_id=5))
    assert sess.current_queue_id is None
    assert sess.awaiting_queue_adoption is True
    backend._adopt_queue_evidence(sess, snap("playing", qid=101, cmd_id=11))
    assert sess.current_queue_id == 101


async def test_forward_jump_reconciles_one_boundary_per_item_capped():
    """Outage-reconnect / jump-ahead reconciliation: a short poll partition
    the player played through (two items ahead on recovery) → two boundary
    reconciliations through the chokepoint — first the armed track, then
    the live queue front — never backward, capped at the window length."""
    from types import SimpleNamespace
    t3 = Track(id="srv-A:77", title="Third", artist="A", album="B",
               duration_ms=0)
    advance = AsyncMock()
    backend, fake = make_backend(script=[
        CompanionUnreachableError("blip 1"),
        CompanionUnreachableError("blip 2"),
        snap("playing", t=100, qid=50, item_id=3, rk="77"),  # two ahead
        snap("stopped"),
        snap("stopped"),
    ], advance_cb=advance)
    sess = backend._session
    _adopt(sess)
    armed = _arm(sess, window=(1, 2, 3))
    backend._is_playing = True
    boundary = AsyncMock()
    fake_state = SimpleNamespace(
        queue_engine=SimpleNamespace(queue=[SimpleNamespace(track=t3)]))
    with patch("asyncio.sleep", AsyncMock()), \
         patch("app.output.session.notify_gapless_boundary", boundary), \
         patch("app.state.queue_engine", fake_state.queue_engine), \
         patch("app.output.session.notify_outage") as notify:
        await backend._poll_timeline(sess)
    assert [c.args[0] for c in boundary.await_args_list] == [armed, t3]
    assert sess.current_item_id == 3
    notify.assert_not_called()     # the partition recovered — no outage
    advance.assert_awaited_once()  # the final track's own EOS


# ══ U7: revoke + stale-watch correction ═══════════════════════════════════════

async def test_revoke_deletes_item_nudges_and_arms_stale_watch():
    pms = FakePmsClient()
    backend, fake = make_backend(pms_factory=_pms_factory_for(pms))
    sess = backend._session
    _adopt(sess)
    _arm(sess)
    await backend.revoke_next()
    assert ("delete", 50, 2) in pms.calls
    assert ("refresh_play_queue", 50) in fake.calls
    assert sess.armed_track is None
    assert sess.stale_item_id == 2          # DLNA posture: watched either way
    # Idempotent: the post-boundary orchestrator churn must no-op.
    await backend.revoke_next()
    assert pms.op_names().count("delete") == 1


async def test_revoke_failure_keeps_stale_watch_then_corrects_on_chain_in():
    """The DEFINED revoke-failure fallback (plan U7 / closing-time ladder):
    DELETE fails → the revoked item stays on the stale watch; the player
    chaining into it anyway → corrective stop + exactly one advance_cb
    (stop-and-replay — at closing time _do_advance's own check freezes
    instead of dispatching, i.e. the forced stop at the boundary)."""
    pms = FakePmsClient(delete_error=CompanionRequestError("HTTP 500",
                                                           status_code=500))
    advance = AsyncMock()
    backend, fake = make_backend(
        script=[snap("playing", t=100, qid=50, item_id=2, rk="99")],
        advance_cb=advance, pms_factory=_pms_factory_for(pms))
    sess = backend._session
    _adopt(sess)
    _arm(sess)
    await backend.revoke_next()
    assert sess.stale_item_id == 2
    backend._is_playing = True
    boundary = AsyncMock()
    with patch("asyncio.sleep", AsyncMock()), \
         patch("app.output.session.notify_gapless_boundary", boundary):
        await backend._poll_timeline(sess)
    assert ("stop", None) in fake.calls     # corrective stop
    advance.assert_awaited_once()           # the fresh re-dispatch path
    boundary.assert_not_called()
    assert sess.poll_task is None


async def test_wrong_next_chained_in_stop_and_replay():
    """Armed, but the edge lands on an item we cannot attribute (foreign
    mutation of our queue / wrong next): never play it silently —
    stop-and-replay correction (dlna.py:1231-1240 shape)."""
    advance = AsyncMock()
    backend, fake = make_backend(
        script=[snap("playing", t=100, qid=50, item_id=9, rk="888")],
        advance_cb=advance)
    sess = backend._session
    _adopt(sess)
    _arm(sess)   # armed item is 2; 9 is outside the window
    backend._is_playing = True
    boundary = AsyncMock()
    with patch("asyncio.sleep", AsyncMock()), \
         patch("app.output.session.notify_gapless_boundary", boundary):
        await backend._poll_timeline(sess)
    assert ("stop", None) in fake.calls
    advance.assert_awaited_once()
    boundary.assert_not_called()
    assert sess.armed_track is None


# ══ U7: skip race ═════════════════════════════════════════════════════════════

async def test_play_discards_arm_slot_and_revoke_noops_after():
    """Admin skip during the armed window: the fresh play() supersedes the
    whole device queue — arm slot + stale watch discarded at entry, the
    racing poll task cancelled, and the orchestrator's follow-up
    revoke_next finds nothing to delete (no stray PMS call)."""
    pms = FakePmsClient()
    backend, fake = make_backend(on_exhausted="block",
                                 pms_factory=_pms_factory_for(pms))
    sess = backend._session
    _adopt(sess)
    _arm(sess)
    sess.stale_item_id = 7
    sess.foreign_reads = 2
    await backend.play("http://ignored", make_track("srv-A:42"))
    assert sess.armed_track is None
    assert sess.pending_arm is None
    assert sess.stale_item_id is None
    assert sess.foreign_reads == 0
    assert sess.current_item_id is None
    await backend.revoke_next()
    assert pms.calls == []           # nothing armed — nothing deleted
    await _teardown(backend)


# ══ U7: foreign controller ════════════════════════════════════════════════════

async def test_sustained_foreign_queue_yields_hold_and_notice():
    """Three consecutive post-confirmation reads naming an unowned
    playQueueID → yield: foreign_controller hold entered, admin notice on
    the OutputChangedEvent 'error' channel, dispatch loop stopped — no
    advance, no outage."""
    advance = AsyncMock()
    backend, fake = make_backend(script=[
        snap("playing", t=100, qid=666, item_id=9),
        snap("playing", t=200, qid=666, item_id=9),
        snap("playing", t=300, qid=666, item_id=9),
    ], advance_cb=advance)
    sess = backend._session
    _adopt(sess)
    backend._is_playing = True
    hold_cb = AsyncMock()
    notice = AsyncMock()
    with patch("asyncio.sleep", AsyncMock()), \
         patch("app.output.session.hold_foreign_controller", hold_cb), \
         patch("app.events.bus.manager.broadcast_to_admins", notice), \
         patch("app.output.session.notify_outage") as outage:
        await backend._poll_timeline(sess)
    hold_cb.assert_awaited_once()
    assert notice.await_count == 1
    evt = notice.await_args.args[0]
    assert evt.backend_type == "error"
    assert "Plex controller" in evt.device_name
    advance.assert_not_called()
    outage.assert_not_called()
    assert backend.is_playing is False
    assert sess.poll_task is None


async def test_foreign_count_resets_on_owned_read():
    """Two foreign reads, then our own queue again → the debounce counter
    resets; no yield (a transient scanner/echo never takes the device)."""
    backend, fake = make_backend(script=[
        snap("playing", t=100, qid=666, item_id=9),
        snap("playing", t=200, qid=666, item_id=9),
        snap("playing", t=300, qid=50, item_id=1),   # ours — reset
        snap("stopped"),
        snap("stopped"),
    ], advance_cb=AsyncMock())
    sess = backend._session
    _adopt(sess)
    backend._is_playing = True
    hold_cb = AsyncMock()
    with patch("asyncio.sleep", AsyncMock()), \
         patch("app.output.session.hold_foreign_controller", hold_cb):
        await backend._poll_timeline(sess)
    hold_cb.assert_not_called()
    assert sess.foreign_reads == 0


async def test_dispatch_transition_window_is_not_foreign():
    """Stale old-queue timeline right after a re-dispatch (adoption window
    open, old owned qid still echoing) is routine churn — never a foreign
    read; the new queue is adopted when its evidence lands."""
    backend, fake = make_backend(script=[
        snap("playing", t=100, qid=100, item_id=5),   # old OWNED queue echo
        snap("playing", t=200, qid=100, item_id=5),
        snap("playing", t=0, qid=200, item_id=1),     # new queue → adopted
        snap("stopped"),
        snap("stopped"),
    ], advance_cb=AsyncMock())
    sess = backend._session
    sess.current_queue_id = 100
    sess.owned_queue_ids = {100}
    sess.awaiting_queue_adoption = True               # re-dispatch window
    backend._is_playing = True
    hold_cb = AsyncMock()
    with patch("asyncio.sleep", AsyncMock()), \
         patch("app.output.session.hold_foreign_controller", hold_cb):
        await backend._poll_timeline(sess)
    hold_cb.assert_not_called()
    assert sess.foreign_reads == 0
    assert sess.current_queue_id == 200
    assert sess.owned_queue_ids == {200, 100}


async def test_unconfirmed_dispatch_suppresses_foreign_verdict():
    """Between dispatch and first confirmation an unknown playQueueID is not
    evidence (suppression window): no foreign counting while the confirm
    token is outstanding."""
    backend, fake = make_backend(script=[
        snap("buffering", qid=666, item_id=9),
        snap("buffering", qid=666, item_id=9),
        snap("buffering", qid=666, item_id=9),
        CompanionUnreachableError("end 1"),
        CompanionUnreachableError("end 2"),
        CompanionUnreachableError("end 3"),
    ], advance_cb=AsyncMock())
    sess = backend._session
    _adopt(sess)
    sess.confirm_token = 12345          # dispatch not yet confirmed
    backend._is_playing = True
    hold_cb = AsyncMock()
    with patch("asyncio.sleep", AsyncMock()), \
         patch("app.output.session.hold_foreign_controller", hold_cb), \
         patch("app.output.session.notify_outage"):
        await backend._poll_timeline(sess)
    hold_cb.assert_not_called()
    assert sess.foreign_reads == 0


async def test_restart_readopts_persisted_queue_not_foreign():
    """Backend restart re-adoption: set_device seeds the persisted owned
    playQueueID (plexqueue:{device_id}), so a timeline still playing OUR
    pre-restart queue reads as ours — never a foreign controller."""
    import json as _json
    from app.output.plexplayer import PlexPlayerBackend
    backend = PlexPlayerBackend(
        client_factory=lambda h, p, d: FakePlayerClient(on_exhausted="block"))
    backend._device_addresses["p1"] = {
        "host": "192.168.1.30", "port": 32500, "name": "Caldera"}

    def settings(key, default=None):
        if key == "plexqueue:p1":
            return _json.dumps({"queue_id": 77, "server": "srv-A"})
        return None

    get_setting = AsyncMock(side_effect=settings)
    with patch("app.database.get_setting", get_setting), \
         patch("app.database.set_setting", AsyncMock()):
        await backend.set_device("p1")
    sess = backend._session
    assert sess.current_queue_id == 77
    assert sess.owned_queue_ids == {77}
    assert sess.server_machine_id == "srv-A"
    backend._is_playing = True
    out = await backend._observe_queue(
        sess, snap("playing", t=100, qid=77, item_id=4))
    assert out == "none"
    assert sess.foreign_reads == 0
    assert sess.current_item_id == 4    # position anchor adopted, no verdict


async def test_adoption_persists_owned_queue_when_device_bound():
    """Adoption on a bound device fire-and-forgets the plexqueue setting
    (the restart re-adoption seed)."""
    backend, fake = make_backend()
    backend._device_id = "player-1"
    sess = backend._session
    sess.awaiting_queue_adoption = True
    sess.server_machine_id = "srv-A"
    set_setting = AsyncMock()
    with patch("app.database.set_setting", set_setting):
        backend._adopt_queue_evidence(sess, snap("playing", qid=101))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    assert sess.current_queue_id == 101
    calls = [c for c in set_setting.await_args_list
             if c.args[0] == "plexqueue:player-1"]
    assert calls, "adopted playQueueID must persist for restart re-adoption"
    import json as _json
    payload = _json.loads(calls[0].args[1])
    assert payload["queue_id"] == 101
    assert payload["server"] == "srv-A"


# ══ review fixes: PLX-2 / PLX-6 / PLX-7 ══════════════════════════════════════

async def test_retired_backend_suppresses_outage_signals():
    """Review fix PLX-2 (guard layer): a backend that is no longer the
    router's active-or-pending backend must NOT fire notify_outage — the
    signal would land in the NEW backend's session as a phantom outage.
    The 3-strike exit goes silent instead."""
    backend, fake = make_backend(script=[
        CompanionUnreachableError("1"),
        CompanionUnreachableError("2"),
        CompanionUnreachableError("3"),
    ], is_current=lambda: False)
    backend._is_playing = True
    with patch("asyncio.sleep", AsyncMock()), \
         patch("app.output.session.notify_outage") as notify:
        await backend._poll_timeline(backend._session)
    notify.assert_not_called()
    assert backend.is_playing is False


async def test_switch_away_while_paused_stops_retired_plexplayer():
    """Review fix PLX-2 (router layer): set_backend's IMMEDIATE branch fires
    for a PAUSED backend (not is_playing) and must stop the outgoing one —
    poll task cancelled, stop command sent, self_stopped flagged — so the
    retired instance can't keep a session/poll alive past the switch."""
    from app.output.router import OutputRouter
    backend, fake = make_backend(script=[snap("stopped")])   # stop-verify read
    sess = backend._session
    parked = asyncio.create_task(asyncio.Event().wait())     # a live poll task
    sess.poll_task = parked
    backend._is_playing = False                              # paused mid-track
    router = OutputRouter()
    router._active = backend
    new = MagicMock(is_playing=False)
    with patch("app.state.trigger_arming_eval", MagicMock()):
        router.set_backend(new)
    for _ in range(10):
        await asyncio.sleep(0)
    assert router.active is new
    assert parked.cancelled()
    assert sess.poll_task is None
    assert sess.self_stopped is True
    assert "stop" in fake.command_names()


async def test_external_resume_after_pause_still_advances_at_track_end():
    """Review fix PLX-6: our pause(), then an EXTERNAL resume from another
    Plex app. Non-stale playing reads naming the owned queue restore the
    playing flag, so the track's real end (2x stopped) still advances —
    the queue must not stall behind a resume we didn't issue."""
    advance = AsyncMock()
    backend, fake = make_backend(script=[
        snap("playing", t=120000, qid=50),   # external resume tick
        snap("playing", t=121000, qid=50),
        snap("stopped"),
        snap("stopped"),                     # track end → advance
    ], advance_cb=advance)
    sess = backend._session
    _adopt(sess)
    sess.last_state = "playing"
    sess.last_time_ms = 100000
    backend._is_playing = True
    await backend.pause()
    assert backend.is_playing is False
    with patch("asyncio.sleep", AsyncMock()):
        await backend._poll_timeline(sess)
    advance.assert_awaited_once()
    assert backend.is_playing is False       # ended after the restored state


async def test_external_resume_ignores_unowned_queue():
    """PLX-6 companion: a playing tick naming a queue we don't own is a
    foreign controller's business (the foreign verdict owns it), never a
    resume of OUR paused track."""
    backend, fake = make_backend(script=[
        snap("playing", t=5000, qid=666),
        CompanionUnreachableError("end 1"),
        CompanionUnreachableError("end 2"),
        CompanionUnreachableError("end 3"),
    ])
    sess = backend._session
    _adopt(sess)
    backend._is_playing = True
    await backend.pause()
    with patch("asyncio.sleep", AsyncMock()), \
         patch("app.output.session.notify_outage"), \
         patch("app.output.session.hold_foreign_controller", AsyncMock()):
        await backend._poll_timeline(sess)
    assert backend.is_playing is False       # never restored for a foreign queue


async def test_revoke_racing_arm_delivery_abandons_install():
    """Review fix PLX-7: revoke_next interleaving with an IN-FLIGHT arm
    delivery (mid-append await) must win — no armed state installs, the
    appended item is best-effort DELETEd, and its identity stays on the
    stale watch (a chain-in anyway gets the stop-and-replay correction)."""
    gate = asyncio.Event()

    class _BlockingPms(FakePmsClient):
        async def append_to_play_queue(self, play_queue_id, uri, *,
                                       play_next=False):
            self.calls.append(("append", play_queue_id, uri, play_next))
            await gate.wait()
            return self.window

    pms = _BlockingPms(window=win(50, (1, "42"), (2, "999")))
    backend, fake = make_backend(pms_factory=_pms_factory_for(pms))
    sess = backend._session
    _adopt(sess)
    backend._is_playing = True
    arm_task = asyncio.create_task(
        backend.arm_next("http://ignored", make_track("srv-A:999")))
    for _ in range(20):
        await asyncio.sleep(0)
        if pms.op_names():
            break
    assert pms.op_names() == ["append"]      # delivery parked mid-await
    await backend.revoke_next()              # the interleaved revoke
    gate.set()
    await arm_task
    assert sess.armed_track is None, "no armed state may install past a revoke"
    assert sess.armed_item_id is None
    assert sess.pending_arm is None
    assert "delete" in pms.op_names()        # appended item abandoned
    assert sess.stale_item_id == 2           # stale watch armed
    assert sess.stale_rating_key == "999"


# ══ U7: teardown-warning notice (router switch-away wiring) ═══════════════════

async def test_swap_pending_broadcasts_teardown_warning_notice():
    """Switch-away teardown the backend could not verify → the U6 admin
    notice vehicle fires from router.swap_pending (the one switch-away stop
    owner) — copy pinned; no warning → no notice."""
    from types import SimpleNamespace
    from app.output.router import OutputRouter

    class _Old:
        def __init__(self, warning):
            self.stop = AsyncMock()
            self.last_teardown_warning = warning
            self.is_playing = True

    new = SimpleNamespace(is_playing=False)
    notice = AsyncMock()
    with patch("app.state.trigger_arming_eval", MagicMock()), \
         patch("app.events.bus.manager.broadcast_to_admins", notice):
        router = OutputRouter()
        old = _Old("player still reports playing after stop")
        router.set_backend(old)
        router._pending = new
        await router.swap_pending()
        old.stop.assert_awaited_once()
        notice.assert_awaited_once()
        evt = notice.await_args.args[0]
        assert evt.backend_type == "error"
        assert evt.device_name == ("Plex player may still be playing — "
                                   "stop it from a Plex app")

        # Clean teardown → silent.
        notice.reset_mock()
        router2 = OutputRouter()
        clean = _Old(None)
        router2.set_backend(clean)
        router2._pending = new
        await router2.swap_pending()
        notice.assert_not_called()
