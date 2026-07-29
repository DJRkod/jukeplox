"""Thread-safe async queue engine.

Every mutating operation:
  1. acquires the asyncio Lock
  2. mutates in-memory state
  3. persists to SQLite (fire-and-forget)
  4. emits an event to all registered callbacks
  5. releases the lock
"""

import asyncio
import random
from collections import deque
from collections.abc import Callable, Coroutine
from typing import Any

from app import database
from app.models import Track
from app.queue.models import PlaybackState, QueueEndBehavior, QueueItem


_MAX_QUEUE_DEPTH = 500  # per-party sanity cap; prevents DB/WS storm from runaway appends


class QueueLockError(Exception):
    """Raised when a guest tries to append while queuing is locked."""


EventCallback = Callable[[str, Any], Coroutine[Any, Any, None]]


class QueueEngine:
    def __init__(self, history_max: int = 50):
        self._queue: list[QueueItem] = []
        self._history: deque[QueueItem] = deque(maxlen=history_max)
        self._current: QueueItem | None = None
        self._is_playing: bool = False
        self._is_paused: bool = False
        self._locked: bool = False
        self._end_behavior: QueueEndBehavior = QueueEndBehavior.STOP
        self._history_max = history_max
        self._lock = asyncio.Lock()
        self._callbacks: list[EventCallback] = []
        # Injected by the app to fetch an auto-fill track for the random
        # queue-end modes (POPULAR_RANDOM / FULL_RANDOM). Takes the active
        # behavior and returns a Track to enqueue, or None to stop.
        self._auto_fill_provider: (
            Callable[[QueueEndBehavior], Coroutine[Any, Any, Track | None]] | None
        ) = None

    # ── callbacks ─────────────────────────────────────────────────────────────

    def add_callback(self, cb: EventCallback) -> None:
        self._callbacks.append(cb)

    async def _emit(self, event: str, payload: Any = None) -> None:
        await asyncio.gather(*(cb(event, payload) for cb in self._callbacks),
                             return_exceptions=True)

    # ── persistence ───────────────────────────────────────────────────────────

    async def _persist(self) -> None:
        asyncio.create_task(database.save_queue([item.to_dict() for item in self._queue]))

    async def _persist_history(self) -> None:
        asyncio.create_task(database.save_history([item.to_dict() for item in self._history]))

    async def load_from_db(self) -> None:
        rows = await database.load_queue()
        self._queue = [QueueItem.from_dict(r) for r in rows]
        history_rows = await database.load_history()
        for r in history_rows:
            self._history.append(QueueItem.from_dict(r))

    # ── state snapshot ────────────────────────────────────────────────────────

    @property
    def state(self) -> PlaybackState:
        return PlaybackState(
            current=self._current,
            is_playing=self._is_playing,
            is_paused=self._is_paused,
        )

    @property
    def queue(self) -> list[QueueItem]:
        return list(self._queue)

    @property
    def history(self) -> list[QueueItem]:
        return list(self._history)

    @property
    def is_locked(self) -> bool:
        return self._locked

    @property
    def end_behavior(self) -> QueueEndBehavior:
        return self._end_behavior

    @end_behavior.setter
    def end_behavior(self, value: QueueEndBehavior) -> None:
        self._end_behavior = value

    @property
    def history_max(self) -> int:
        return self._history_max

    @history_max.setter
    def history_max(self, value: int) -> None:
        self._history_max = value
        self._history = deque(self._history, maxlen=value)

    # ── guest operations ──────────────────────────────────────────────────────

    def is_duplicate(self, track_id: str) -> bool:
        if self._current and self._current.track_id == track_id:
            return True
        return any(item.track_id == track_id for item in self._queue)

    async def append(self, track: Track, *, bypass_lock: bool = False) -> QueueItem:
        async with self._lock:
            if self._locked and not bypass_lock:
                raise QueueLockError("Guest queuing is currently locked")
            if len(self._queue) >= _MAX_QUEUE_DEPTH:
                raise QueueLockError("Queue is full")
            item = QueueItem(track=track)
            self._queue.append(item)
            await self._persist()
        await self._emit("queue_changed")
        return item

    async def append_many(
        self, tracks: list[Track], *, bypass_lock: bool = False
    ) -> list[QueueItem]:
        """Atomically append a batch of tracks to the queue.

        Takes the lock once and validates the WHOLE batch against
        `_MAX_QUEUE_DEPTH`. Either all tracks are appended or none — a
        batch that would overflow the cap raises `QueueLockError` before
        any in-memory mutation, so the queue is never left in a partial
        state (KTD: per-batch atomicity for the album branch — see #2 of
        the review-batch fix queue).

        Mirrors `append`'s lock and emit semantics for the cap/lock checks,
        but emits a single `queue_changed` event for the batch rather than
        one per track — keeps the WS firehose quiet for album appends.
        """
        if not tracks:
            return []
        async with self._lock:
            if self._locked and not bypass_lock:
                raise QueueLockError("Guest queuing is currently locked")
            if len(self._queue) + len(tracks) > _MAX_QUEUE_DEPTH:
                raise QueueLockError("Queue is full")
            items = [QueueItem(track=t) for t in tracks]
            self._queue.extend(items)
            await self._persist()
        await self._emit("queue_changed")
        return items

    async def remove_last_matching(self, track_id: str, added_at: str) -> bool:
        """Undo support (collected-library plan U5): remove the LAST queue
        entry matching BOTH receipt fields. Returns False when no entry
        matches — the caller treats that as success-no-op (the entry
        already advanced to playing or was removed). Emits queue_changed
        only when something was actually removed."""
        removed = False
        async with self._lock:
            for i in range(len(self._queue) - 1, -1, -1):
                item = self._queue[i]
                if item.track_id == track_id and item.added_at == added_at:
                    self._queue.pop(i)
                    removed = True
                    await self._persist()
                    break
        if removed:
            await self._emit("queue_changed")
        return removed

    async def remove_entries(self, entries: list[tuple[str, str]]) -> int:
        """Remove all upcoming queue entries matching the given receipts
        (remove-own-queued-tracks U3). Generalizes `remove_last_matching` to a
        batch: for each `(track_id, added_at)` receipt, removes the LAST
        matching upcoming entry. Returns the count actually removed.

        One lock, one persist, and a single `queue_changed` emit for the whole
        batch — an album-as-unit removal is one broadcast, not N. Receipts
        whose entries are already gone (played, removed, or garbage) match
        nothing and contribute 0 — quiet no-op, same contract as
        `remove_last_matching`. Iterates `self._queue` only, so the
        currently-playing track is never removable by a guest receipt."""
        removed = 0
        async with self._lock:
            for track_id, added_at in entries:
                for i in range(len(self._queue) - 1, -1, -1):
                    item = self._queue[i]
                    if item.track_id == track_id and item.added_at == added_at:
                        self._queue.pop(i)
                        removed += 1
                        break
            if removed:
                await self._persist()
        if removed:
            await self._emit("queue_changed")
        return removed

    # ── admin operations ──────────────────────────────────────────────────────

    async def remove(self, position: int) -> None:
        async with self._lock:
            if position < 0 or position >= len(self._queue):
                raise IndexError(f"Queue position {position} out of range")
            self._queue.pop(position)
            await self._persist()
        await self._emit("queue_changed")

    async def remove_history_entry(self, track_id: str, added_at: str) -> QueueItem | None:
        """Admin play-data curation (plan U3): remove ONE history entry matching
        BOTH ``(track_id, added_at)`` — the first match from the head (most-recent)
        — and return it, or ``None`` when nothing matches. ``added_at`` is not
        guaranteed unique across a batch-append/restore, so the head-first
        tie-break removes exactly one entry (the endpoint un-counts once). The
        returned item lets the caller read ``item.track.album`` /
        ``item.track.artist`` for the count roll-back. Persists and emits
        ``queue_changed`` so every screen's history strip repaints."""
        removed: QueueItem | None = None
        async with self._lock:
            remaining: list[QueueItem] = []
            for item in self._history:  # deque iterates head (newest) → tail (oldest)
                if removed is None and item.track_id == track_id and item.added_at == added_at:
                    removed = item
                    continue
                remaining.append(item)
            if removed is None:
                return None
            self._history = deque(remaining, maxlen=self._history_max)
            await self._persist_history()
        await self._emit("queue_changed")
        return removed

    async def move(self, from_pos: int, to_pos: int) -> None:
        async with self._lock:
            n = len(self._queue)
            if not (0 <= from_pos < n and 0 <= to_pos < n):
                raise IndexError("Queue position out of range")
            item = self._queue.pop(from_pos)
            self._queue.insert(to_pos, item)
            await self._persist()
        await self._emit("queue_changed")

    async def promote(self, position: int) -> None:
        """Move item at position to the front (plays next after current)."""
        async with self._lock:
            if position < 0 or position >= len(self._queue):
                raise IndexError(f"Queue position {position} out of range")
            item = self._queue.pop(position)
            self._queue.insert(0, item)
            await self._persist()
        await self._emit("queue_changed")

    async def clear(self) -> None:
        async with self._lock:
            self._queue.clear()
            await self._persist()
        await self._emit("queue_changed")

    async def lock(self) -> None:
        async with self._lock:
            self._locked = True
        await self._emit("lock_changed", True)

    async def unlock(self) -> None:
        async with self._lock:
            self._locked = False
        await self._emit("lock_changed", False)

    # ── playback ──────────────────────────────────────────────────────────────

    async def advance(self) -> QueueItem | None:
        """Called by the output backend when a track ends. Returns the next item or None."""
        async with self._lock:
            if self._current:
                self._history.appendleft(self._current)
                await self._persist_history()
            self._current = None
            self._is_playing = False
            self._is_paused = False

            if self._queue:
                self._current = self._queue.pop(0)
                self._is_playing = True
                await self._persist()
                result = self._current
            elif (self._end_behavior in (QueueEndBehavior.POPULAR_RANDOM,
                                         QueueEndBehavior.FULL_RANDOM)
                  and self._auto_fill_provider):
                track = await self._auto_fill_provider(self._end_behavior)
                if track:
                    self._current = QueueItem(track=track)
                    self._is_playing = True
                    await self._persist()
                    result = self._current
                else:
                    result = None
            else:
                result = None

        await self._emit("queue_changed")
        await self._emit("now_playing_changed")
        return result

    async def skip_back(self) -> QueueItem | None:
        """Replay the most recently played track (admin Skip Back).

        The interrupted current track (if any) returns to the FRONT of the
        queue so Skip Back → Skip Forward round-trips back to it. The
        front-insert deliberately bypasses `_MAX_QUEUE_DEPTH` — the cap
        guards guest appends, not internal ops (mirrors promote/move).
        No-op (returns None, emits nothing) when history is empty; callers
        treat None as "nothing to go back to".
        """
        async with self._lock:
            if not self._history:
                return None
            if self._current:
                self._queue.insert(0, self._current)
            self._current = self._history.popleft()
            self._is_playing = True
            self._is_paused = False
            await self._persist()
            await self._persist_history()
            result = self._current
        await self._emit("queue_changed")
        await self._emit("now_playing_changed")
        return result

    async def hold_current(self, *, play_recorded: bool = False) -> QueueItem | None:
        """Outage hold (2026-07-11 supervisor plan U2): a device-level failure
        interrupted the current track — re-front-insert it so it plays next at
        resume, marked ``play_recorded`` so an already-counted play is never
        counted twice (R19), and land the queue PAUSED so the guest UI reads
        coherently. The front-insert deliberately bypasses ``_MAX_QUEUE_DEPTH``
        (the cap guards guest appends, not internal ops — the ``skip_back``
        mechanic). Persisted, so a restart lands idle with the held item at
        the queue front and its mark intact (R18). No-op when nothing is
        current — repeated holds cannot double-insert."""
        async with self._lock:
            if self._current is None:
                return None
            item = self._current
            item.play_recorded = play_recorded
            self._queue.insert(0, item)
            self._current = None
            self._is_playing = False
            self._is_paused = True
            await self._persist()
            result = item
        await self._emit("queue_changed")
        await self._emit("now_playing_changed")
        await self._emit("playback_state_changed")
        return result

    async def skip_held_front(self) -> QueueItem | None:
        """Skip while outage-held (2026-07-11 supervisor plan U4, R17): the
        held item IS the queue front (``hold_current`` re-front-inserted it),
        so Skip retires it to history — exactly where ``advance()`` lands a
        playing track — and the next queued item becomes the new held front.
        A pure pointer move: nothing dispatches, ``current`` stays None
        (nothing is playing), the queue stays paused. The retired item keeps
        its ``play_recorded`` mark (it WAS counted if it played, and a
        skip-back must not re-mint that); the new front's own mark governs
        the eventual resume (an unplayed queued item is False → it counts
        when it finally plays). No-op returning None on an empty queue."""
        async with self._lock:
            if not self._queue:
                return None
            item = self._queue.pop(0)
            self._history.appendleft(item)
            await self._persist()
            await self._persist_history()
            result = item
        await self._emit("queue_changed")
        return result

    async def skip_back_held_front(self) -> QueueItem | None:
        """Skip Back while outage-held (U4, R17): front-insert the most
        recent history item so IT becomes the held front that plays at
        resume — the mirror of ``skip_held_front``, and the ``skip_back``
        front-insert mechanic (deliberately bypasses ``_MAX_QUEUE_DEPTH``;
        the cap guards guest appends, not internal ops). The item's
        ``play_recorded`` mark rides along: a held item skipped away and
        pulled back keeps its already-counted mark, while an organically
        played history item (mark False) re-counts at resume — matching
        live Skip Back semantics. No-op returning None on empty history."""
        async with self._lock:
            if not self._history:
                return None
            item = self._history.popleft()
            self._queue.insert(0, item)
            await self._persist()
            await self._persist_history()
            result = item
        await self._emit("queue_changed")
        return result

    async def set_playing(self, track: Track) -> None:
        """Directly set the current track (called when playback starts)."""
        async with self._lock:
            self._current = QueueItem(track=track)
            self._is_playing = True
            self._is_paused = False
        await self._emit("now_playing_changed")

    async def set_paused(self, paused: bool) -> None:
        async with self._lock:
            self._is_paused = paused
        await self._emit("playback_state_changed")

    async def set_stopped(self) -> None:
        async with self._lock:
            self._current = None
            self._is_playing = False
            self._is_paused = False
        await self._emit("now_playing_changed")

    async def close_out(self) -> None:
        """Closing Time freeze (2026-06-24 plan): the current track played to its
        natural end, so retire it to history WITHOUT popping the next item or
        auto-filling, and leave the queue intact for a later resume. Distinct
        from ``advance()`` (which continues to the next track / autofill) and
        from ``set_stopped()`` (which discards the played track without recording
        it in history). Emits the same events as ``advance()`` so clients refresh
        the now-playing / history strip."""
        async with self._lock:
            if self._current:
                self._history.appendleft(self._current)
                await self._persist_history()
            self._current = None
            self._is_playing = False
            self._is_paused = False
        await self._emit("queue_changed")
        await self._emit("now_playing_changed")
