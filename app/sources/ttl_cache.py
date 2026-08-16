"""Bounded TTL cache shared by the source clients.

All three source clients (Plex, Jellyfin, Subsonic) carried a byte-identical
copy of a TTL cache whose entries were never evicted. `valid` made an expired
entry unreadable, but it stayed in the dict holding its parsed Tracks, Albums,
and the JSON payloads behind them — expiry made the memory *useless* without
making it *free*.

That is a leak rather than a bounded cost because the keys are per-artist and
per-album (``albums:<lib>:<artist>``, ``tracks:<lib>:<album>``). Every distinct
drill-in on a large library adds an entry that nothing ever removes: a re-read
would return None and a re-write would replace it, but the realistic pattern is
a key written once and never touched again.

Measured on the rig with a 103k-track library (issue #37): 187 MB retained
across 11 minutes of party load, about 1 GB/hour, with cyclic garbage collected
at both ends of the measurement so it was genuine retention and not uncollected
churn. On a 2-4 GB single-board machine that exhausts the box inside an evening.

Two bounds, because either alone is insufficient. Expiry frees on read, which
covers keys that are read again. A size cap covers the ones that never are.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

# Five minutes. Long enough that a guest browsing a library does not re-hit the
# source for every tap, short enough that a rescan's changes surface promptly.
DEFAULT_TTL = 300

# Entries, not bytes. A party browses tens of artists and albums, not hundreds,
# so this is comfortably above real use while keeping worst-case retention to
# something a small box can hold. Bytes would be the truer bound but would mean
# sizing arbitrary parsed objects on every write.
DEFAULT_MAX_ENTRIES = 256


@dataclass
class _Entry:
    value: Any
    expires_at: float

    @property
    def valid(self) -> bool:
        return time.monotonic() < self.expires_at


class TTLCache:
    """A small LRU cache whose entries also expire.

    Not thread-safe: every caller lives on the asyncio loop and neither method
    awaits, so each is atomic with respect to the others.
    """

    def __init__(self, ttl: float = DEFAULT_TTL,
                 max_entries: int = DEFAULT_MAX_ENTRIES):
        self._ttl = ttl
        self._max = max(1, int(max_entries))
        self._entries: "OrderedDict[str, _Entry]" = OrderedDict()

    def get(self, key: str) -> Any | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if not entry.valid:
            # Drop it. Returning None while keeping the value is the bug this
            # class exists to fix.
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return entry.value

    def set(self, key: str, value: Any) -> None:
        self._entries[key] = _Entry(value, time.monotonic() + self._ttl)
        self._entries.move_to_end(key)
        self._prune()

    def clear(self) -> None:
        self._entries.clear()

    def _prune(self) -> None:
        if len(self._entries) <= self._max:
            return
        # Expired entries first — they are worthless, so evicting them costs
        # nothing in hit rate. Only then fall back to dropping live ones.
        for key in [k for k, e in self._entries.items() if not e.valid]:
            del self._entries[key]
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)

    def __len__(self) -> int:
        return len(self._entries)
