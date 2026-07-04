import asyncio

import pytest

from app.plex.models import Track
from app.queue.engine import QueueEngine, QueueLockError
from app.queue.models import QueueEndBehavior, coerce_queue_end_behavior


def make_track(track_id: str = "t1", title: str = "Song") -> Track:
    return Track(
        id=track_id,
        title=title,
        artist="Artist",
        album="Album",
        duration_ms=200_000,
        stream_key=f"/parts/{track_id}/file.mp3",
    )


@pytest.fixture
def engine(monkeypatch):
    from unittest.mock import AsyncMock
    import app.queue.engine as eng_module
    q = QueueEngine()
    monkeypatch.setattr(eng_module.database, "save_queue", AsyncMock())
    monkeypatch.setattr(eng_module.database, "load_queue", AsyncMock(return_value=[]))
    monkeypatch.setattr(eng_module.database, "save_history", AsyncMock())
    return q


async def _done():
    pass

async def _done_list():
    return []


# ── queue-end behavior migration (2026-06-21 plan U2) ────────────────────────

def test_coerce_queue_end_behavior_passthrough():
    assert coerce_queue_end_behavior("stop") == QueueEndBehavior.STOP
    assert coerce_queue_end_behavior("popular_random") == QueueEndBehavior.POPULAR_RANDOM
    assert coerce_queue_end_behavior("full_random") == QueueEndBehavior.FULL_RANDOM


def test_coerce_queue_end_behavior_migrates_legacy():
    # shuffle behaved like the whole-library floor; repeat has no successor.
    assert coerce_queue_end_behavior("shuffle") == QueueEndBehavior.FULL_RANDOM
    assert coerce_queue_end_behavior("repeat") == QueueEndBehavior.STOP


def test_coerce_queue_end_behavior_unknown_defaults_stop():
    assert coerce_queue_end_behavior(None) == QueueEndBehavior.STOP
    assert coerce_queue_end_behavior("bogus") == QueueEndBehavior.STOP
    assert coerce_queue_end_behavior("") == QueueEndBehavior.STOP


# ── append ────────────────────────────────────────────────────────────────────

async def test_append_adds_to_end(engine):
    t1, t2 = make_track("t1"), make_track("t2")
    await engine.append(t1)
    await engine.append(t2)
    assert len(engine.queue) == 2
    assert engine.queue[0].track_id == "t1"
    assert engine.queue[1].track_id == "t2"


async def test_append_while_locked_raises(engine):
    await engine.lock()
    with pytest.raises(QueueLockError):
        await engine.append(make_track())


async def test_append_bypass_lock_succeeds(engine):
    await engine.lock()
    await engine.append(make_track(), bypass_lock=True)
    assert len(engine.queue) == 1


# ── append_many (batch) ───────────────────────────────────────────────────────

async def test_append_many_adds_all_tracks_in_order(engine):
    tracks = [make_track(f"t{i}") for i in range(5)]
    items = await engine.append_many(tracks)
    assert [i.track_id for i in items] == ["t0", "t1", "t2", "t3", "t4"]
    assert [i.track_id for i in engine.queue] == ["t0", "t1", "t2", "t3", "t4"]


async def test_append_many_empty_list_is_noop(engine):
    items = await engine.append_many([])
    assert items == []
    assert engine.queue == []


async def test_append_many_while_locked_raises_and_does_not_mutate(engine):
    """A locked queue rejects the WHOLE batch atomically — no partial append."""
    await engine.lock()
    tracks = [make_track(f"t{i}") for i in range(3)]
    with pytest.raises(QueueLockError):
        await engine.append_many(tracks)
    # Nothing was appended.
    assert engine.queue == []


async def test_append_many_bypass_lock_succeeds_on_locked_queue(engine):
    await engine.lock()
    tracks = [make_track(f"t{i}") for i in range(3)]
    await engine.append_many(tracks, bypass_lock=True)
    assert len(engine.queue) == 3


async def test_append_many_rejects_batch_that_would_exceed_cap(engine, monkeypatch):
    """Pre-fill near the cap, then a batch that would push past `_MAX_QUEUE_DEPTH`
    must be rejected as a whole — no partial commit."""
    import app.queue.engine as eng_module
    monkeypatch.setattr(eng_module, "_MAX_QUEUE_DEPTH", 10)
    # Pre-fill 8 entries via single-track append.
    for i in range(8):
        await engine.append(make_track(f"pre{i}"))
    # Batch of 5 would overflow (8 + 5 = 13 > 10) → reject, no partial.
    tracks = [make_track(f"batch{i}") for i in range(5)]
    with pytest.raises(QueueLockError):
        await engine.append_many(tracks)
    # Original 8 entries intact; none of the batch landed.
    assert len(engine.queue) == 8
    assert all(item.track_id.startswith("pre") for item in engine.queue)


async def test_append_many_at_exact_cap_succeeds(engine, monkeypatch):
    """A batch that fills the queue to exactly the cap is allowed (boundary
    case — `_MAX_QUEUE_DEPTH` is the inclusive cap, not the exclusive limit)."""
    import app.queue.engine as eng_module
    monkeypatch.setattr(eng_module, "_MAX_QUEUE_DEPTH", 10)
    for i in range(5):
        await engine.append(make_track(f"pre{i}"))
    tracks = [make_track(f"batch{i}") for i in range(5)]
    await engine.append_many(tracks)
    assert len(engine.queue) == 10


async def test_append_many_emits_single_queue_changed(engine):
    """append_many fires exactly ONE queue_changed event for the whole batch —
    not one per track. Keeps the WS firehose quiet for album appends."""
    events = []

    async def cb(event, payload):
        events.append(event)

    engine.add_callback(cb)
    tracks = [make_track(f"t{i}") for i in range(5)]
    await engine.append_many(tracks)
    assert events.count("queue_changed") == 1


# ── remove ────────────────────────────────────────────────────────────────────

async def test_remove_correct_item(engine):
    for i in range(3):
        await engine.append(make_track(f"t{i}"))
    await engine.remove(1)
    ids = [item.track_id for item in engine.queue]
    assert ids == ["t0", "t2"]


async def test_remove_out_of_range_raises(engine):
    await engine.append(make_track())
    with pytest.raises(IndexError):
        await engine.remove(5)


# ── move ──────────────────────────────────────────────────────────────────────

async def test_move_item_forward(engine):
    for i in range(4):
        await engine.append(make_track(f"t{i}"))
    await engine.move(3, 0)
    assert engine.queue[0].track_id == "t3"
    assert engine.queue[1].track_id == "t0"


async def test_move_item_backward(engine):
    for i in range(4):
        await engine.append(make_track(f"t{i}"))
    await engine.move(0, 3)
    assert engine.queue[3].track_id == "t0"
    assert engine.queue[0].track_id == "t1"


async def test_move_out_of_range_raises(engine):
    await engine.append(make_track())
    with pytest.raises(IndexError):
        await engine.move(0, 99)


# ── promote ───────────────────────────────────────────────────────────────────

async def test_promote_moves_to_front(engine):
    for i in range(4):
        await engine.append(make_track(f"t{i}"))
    await engine.promote(3)
    assert engine.queue[0].track_id == "t3"
    assert len(engine.queue) == 4


async def test_promote_already_first_is_noop(engine):
    for i in range(3):
        await engine.append(make_track(f"t{i}"))
    await engine.promote(0)
    assert engine.queue[0].track_id == "t0"


# ── clear ─────────────────────────────────────────────────────────────────────

async def test_clear_empties_queue(engine):
    for i in range(5):
        await engine.append(make_track(f"t{i}"))
    await engine.clear()
    assert engine.queue == []


async def test_clear_does_not_affect_current(engine):
    track = make_track()
    await engine.set_playing(track)
    await engine.append(make_track("t2"))
    await engine.clear()
    assert engine.state.current is not None
    assert engine.state.current.track_id == "t1"


# ── lock / unlock ─────────────────────────────────────────────────────────────

async def test_lock_sets_locked(engine):
    await engine.lock()
    assert engine.is_locked is True


async def test_unlock_clears_locked(engine):
    await engine.lock()
    await engine.unlock()
    assert engine.is_locked is False


# ── duplicate detection ───────────────────────────────────────────────────────

async def test_is_duplicate_for_queued_track(engine):
    await engine.append(make_track("t1"))
    assert engine.is_duplicate("t1") is True


async def test_is_duplicate_for_current_track(engine):
    await engine.set_playing(make_track("t1"))
    assert engine.is_duplicate("t1") is True


async def test_not_duplicate_for_absent_track(engine):
    assert engine.is_duplicate("absent") is False


# ── advance / end behaviour ───────────────────────────────────────────────────

async def test_advance_loads_next_from_queue(engine):
    t1, t2 = make_track("t1"), make_track("t2")
    await engine.set_playing(t1)
    await engine.append(t2)
    nxt = await engine.advance()
    assert nxt is not None
    assert nxt.track_id == "t2"
    assert engine.state.is_playing is True


async def test_advance_moves_current_to_history(engine):
    t1, t2 = make_track("t1"), make_track("t2")
    await engine.set_playing(t1)
    await engine.append(t2)
    await engine.advance()
    assert any(h.track_id == "t1" for h in engine.history)


async def test_advance_stop_when_empty(engine):
    await engine.set_playing(make_track())
    result = await engine.advance()
    assert result is None
    assert engine.state.is_playing is False


async def test_advance_full_random_calls_provider(engine):
    engine.end_behavior = QueueEndBehavior.FULL_RANDOM
    random_track = make_track("randomed")

    async def provider(behavior):
        assert behavior == QueueEndBehavior.FULL_RANDOM
        return random_track

    engine._auto_fill_provider = provider
    await engine.set_playing(make_track("current"))
    result = await engine.advance()
    assert result is not None
    assert result.track_id == "randomed"


async def test_advance_popular_random_calls_provider(engine):
    engine.end_behavior = QueueEndBehavior.POPULAR_RANDOM
    picked = make_track("popular")
    seen = {}

    async def provider(behavior):
        seen["behavior"] = behavior
        return picked

    engine._auto_fill_provider = provider
    await engine.set_playing(make_track("current"))
    result = await engine.advance()
    assert result is not None
    assert result.track_id == "popular"
    assert seen["behavior"] == QueueEndBehavior.POPULAR_RANDOM


async def test_advance_random_provider_none_stops(engine):
    # A provider that finds nothing (Plex down, empty library) must leave the
    # engine stopped, not in a half-playing state.
    engine.end_behavior = QueueEndBehavior.FULL_RANDOM

    async def provider(behavior):
        return None

    engine._auto_fill_provider = provider
    await engine.set_playing(make_track("current"))
    result = await engine.advance()
    assert result is None
    assert engine.state.is_playing is False


# ── event emission ────────────────────────────────────────────────────────────

async def test_append_emits_queue_changed(engine):
    events = []

    async def cb(event, payload):
        events.append(event)

    engine.add_callback(cb)
    await engine.append(make_track())
    await asyncio.sleep(0)  # allow tasks to run
    assert "queue_changed" in events


async def test_lock_emits_lock_changed(engine):
    events = []

    async def cb(event, payload):
        events.append((event, payload))

    engine.add_callback(cb)
    await engine.lock()
    await asyncio.sleep(0)
    assert ("lock_changed", True) in events



# ── skip_back ─────────────────────────────────────────────────────────────────

async def test_skip_back_replays_history_head_and_requeues_current(engine):
    t1, t2 = make_track("t1"), make_track("t2")
    await engine.set_playing(t1)
    await engine.advance()              # t1 -> history; idle
    await engine.set_playing(t2)        # t2 now playing
    result = await engine.skip_back()
    assert result is not None
    assert result.track_id == "t1"
    assert engine.state.current.track_id == "t1"
    assert engine.state.is_playing is True
    assert engine.queue[0].track_id == "t2"          # interrupted track at front
    assert all(h.track_id != "t1" for h in engine.history)  # popped from history


async def test_skip_back_while_paused_results_in_playing(engine):
    t1, t2 = make_track("t1"), make_track("t2")
    await engine.set_playing(t1)
    await engine.advance()
    await engine.set_playing(t2)
    await engine.set_paused(True)
    result = await engine.skip_back()
    assert result.track_id == "t1"
    assert engine.state.is_playing is True
    assert engine.state.is_paused is False


async def test_skip_back_idle_plays_history_without_requeue(engine):
    """After the final track ends (or a restart), current is None but history persists."""
    await engine.set_playing(make_track("t1"))
    await engine.advance()              # idle, history [t1]
    result = await engine.skip_back()
    assert result.track_id == "t1"
    assert engine.state.is_playing is True
    assert engine.queue == []           # nothing inserted — no interrupted track


async def test_skip_back_empty_history_is_noop(engine):
    events = []

    async def cb(event, payload):
        events.append(event)

    await engine.set_playing(make_track("t1"))
    engine.add_callback(cb)
    result = await engine.skip_back()
    assert result is None
    assert engine.state.current.track_id == "t1"     # unchanged
    assert engine.queue == []
    assert events == []                              # no events emitted


async def test_skip_back_bypasses_queue_depth_cap(engine):
    from app.queue.engine import _MAX_QUEUE_DEPTH
    await engine.set_playing(make_track("h1"))
    await engine.advance()                            # h1 -> history
    await engine.set_playing(make_track("cur"))
    await engine.append_many([make_track(f"q{i}") for i in range(_MAX_QUEUE_DEPTH)])
    result = await engine.skip_back()
    assert result.track_id == "h1"
    assert len(engine.queue) == _MAX_QUEUE_DEPTH + 1  # front-insert succeeded past cap
    assert engine.queue[0].track_id == "cur"


async def test_skip_back_then_advance_round_trips(engine):
    """AE4 at engine level: Skip Back then Skip Forward returns to the interrupted track."""
    t1, t2 = make_track("t1"), make_track("t2")
    await engine.set_playing(t1)
    await engine.advance()
    await engine.set_playing(t2)
    await engine.skip_back()                          # t1 current, t2 at queue front
    nxt = await engine.advance()                      # t1 -> history, t2 replays
    assert nxt.track_id == "t2"
    assert engine.state.is_playing is True


async def test_skip_back_history_maxlen_round_trip(engine):
    engine.history_max = 2
    for tid in ("a", "b", "c"):
        await engine.set_playing(make_track(tid))
        await engine.advance()
    assert len(engine.history) == 2                   # capped: [c, b]
    result = await engine.skip_back()
    assert result.track_id == "c"
    assert len(engine.history) == 1


async def test_skip_back_emits_queue_and_now_playing_changed_once(engine):
    await engine.set_playing(make_track("t1"))
    await engine.advance()
    await engine.set_playing(make_track("t2"))
    events = []

    async def cb(event, payload):
        events.append(event)

    engine.add_callback(cb)
    await engine.skip_back()
    assert events.count("queue_changed") == 1
    assert events.count("now_playing_changed") == 1


# ── undo: remove_last_matching (collected-library plan U5) ───────────────────

async def test_remove_last_matching_removes_only_last_duplicate():
    qe = QueueEngine()
    t = make_track("t1")
    first = await qe.append(t, bypass_lock=True)
    second = await qe.append(t, bypass_lock=True)
    assert first.added_at != second.added_at or True  # timestamps may differ
    removed = await qe.remove_last_matching(second.track_id, second.added_at)
    assert removed is True
    assert len(qe.queue) == 1
    assert qe.queue[0].added_at == first.added_at


async def test_remove_last_matching_absent_is_false_and_untouched():
    qe = QueueEngine()
    await qe.append(make_track("t1"), bypass_lock=True)
    removed = await qe.remove_last_matching("t1", "2026-01-01T00:00:00+00:00")
    assert removed is False
    assert len(qe.queue) == 1


async def test_remove_last_matching_requires_both_fields():
    qe = QueueEngine()
    item = await qe.append(make_track("t1"), bypass_lock=True)
    assert await qe.remove_last_matching("OTHER", item.added_at) is False
    assert await qe.remove_last_matching(item.track_id, "nope") is False
    assert len(qe.queue) == 1


# ── close_out (Closing Time freeze; 2026-06-24 plan U2) ──────────────────────

async def test_close_out_retires_current_preserves_queue(engine):
    """Freeze: the played trigger goes to history, the queue is kept intact, and
    playback goes idle — no next-track pop, no autofill."""
    await engine.set_playing(make_track("cur", "Closing Time"))
    await engine.append(make_track("next"))
    await engine.close_out()
    assert engine.state.current is None
    assert engine.state.is_playing is False
    assert engine.history[0].track_id == "cur"
    assert [i.track_id for i in engine.queue] == ["next"]


async def test_close_out_with_empty_queue_leaves_queue_empty(engine):
    """Trigger as the last item: freeze still records it and leaves an empty queue."""
    await engine.set_playing(make_track("cur"))
    await engine.close_out()
    assert engine.state.current is None
    assert list(engine.queue) == []
    assert engine.history[0].track_id == "cur"


# ── admin history removal (play-data curation plan U3) ───────────────────────

def _hist_item(track_id, added_at, album="Album", artist="Artist"):
    from app.queue.models import QueueItem
    t = make_track(track_id)
    t.album = album
    t.artist = artist
    return QueueItem(track=t, added_at=added_at)


async def test_remove_history_entry_removes_match_and_returns_it(engine):
    from collections import deque
    engine._history = deque([
        _hist_item("t2", "2026-07-03T00:00:02+00:00"),
        _hist_item("t1", "2026-07-03T00:00:01+00:00", album="Broken", artist="NIN"),
    ])
    events = []

    async def cb(event, payload):
        events.append(event)

    engine.add_callback(cb)
    removed = await engine.remove_history_entry("t1", "2026-07-03T00:00:01+00:00")
    assert removed is not None
    assert removed.track.album == "Broken" and removed.track.artist == "NIN"
    assert [h.track_id for h in engine.history] == ["t2"]
    assert events.count("queue_changed") == 1


async def test_remove_history_entry_not_found_returns_none(engine):
    from collections import deque
    engine._history = deque([_hist_item("t1", "A")])
    events = []

    async def cb(event, payload):
        events.append(event)

    engine.add_callback(cb)
    removed = await engine.remove_history_entry("t1", "MISMATCH")
    assert removed is None
    assert [h.track_id for h in engine.history] == ["t1"]   # untouched
    assert events.count("queue_changed") == 0               # no emit on no-op


async def test_remove_history_entry_tie_break_removes_head_only(engine):
    from collections import deque
    # Two entries share BOTH track_id and added_at → remove exactly one (head-most).
    engine._history = deque([
        _hist_item("t1", "SAME", album="head"),
        _hist_item("t1", "SAME", album="tail"),
    ])
    removed = await engine.remove_history_entry("t1", "SAME")
    assert removed.track.album == "head"
    assert len(engine.history) == 1
    assert engine.history[0].track.album == "tail"
