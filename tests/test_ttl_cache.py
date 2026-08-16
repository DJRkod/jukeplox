"""The source clients' response cache must be bounded and must actually free.

Issue #37. All three source clients carried a byte-identical TTL cache whose
entries were never evicted: `valid` made an expired entry unreadable but left it
in the dict holding its parsed Tracks, Albums and the JSON payloads behind them.
Cache keys are per-artist and per-album, so every distinct drill-in on a large
library added a permanent entry.

Measured on the rig with a 103k-track library: 187 MB retained across 11 minutes
of party load — about 1 GB/hour — with cyclic garbage collected at both ends, so
it was genuine retention rather than uncollected churn.
"""

import time

import pytest

from app.sources.ttl_cache import TTLCache


def test_returns_what_was_stored():
    c = TTLCache()
    c.set("k", [1, 2, 3])
    assert c.get("k") == [1, 2, 3]


def test_missing_key_is_none():
    assert TTLCache().get("nope") is None


def test_expired_entry_is_dropped_not_merely_ignored():
    """The defect. Reading an expired entry must FREE it — returning None while
    keeping the value is what leaked."""
    c = TTLCache(ttl=0.01)
    c.set("k", "payload")
    assert len(c) == 1
    time.sleep(0.02)

    assert c.get("k") is None
    assert len(c) == 0, "expired entry still held after being read"


def test_expired_entries_do_not_accumulate_when_never_read_again():
    """Keys are per-artist and per-album, so the realistic pattern is many keys
    written once and never read again. Nothing would ever trigger their
    removal, which is why a size bound is needed as well as expiry."""
    c = TTLCache(ttl=0.01, max_entries=16)
    for i in range(500):
        c.set(f"albums:lib:{i}", f"payload-{i}")
    assert len(c) <= 16, f"unbounded growth: {len(c)} entries"


def test_bounded_by_max_entries_even_when_all_are_live():
    c = TTLCache(ttl=3600, max_entries=10)
    for i in range(100):
        c.set(f"k{i}", i)
    assert len(c) == 10


def test_eviction_is_least_recently_used():
    c = TTLCache(ttl=3600, max_entries=3)
    c.set("a", 1)
    c.set("b", 2)
    c.set("c", 3)
    assert c.get("a") == 1          # 'a' is now the most recently used
    c.set("d", 4)                   # evicts the LRU, which is 'b'

    assert c.get("b") is None
    assert c.get("a") == 1
    assert c.get("c") == 3
    assert c.get("d") == 4


def test_overwriting_a_key_does_not_grow_the_cache():
    c = TTLCache(ttl=3600, max_entries=10)
    for _ in range(50):
        c.set("same", "value")
    assert len(c) == 1


def test_clear_empties_everything():
    c = TTLCache()
    c.set("a", 1)
    c.set("b", 2)
    c.clear()
    assert len(c) == 0
    assert c.get("a") is None


def test_a_refreshed_key_is_live_again():
    c = TTLCache(ttl=0.01)
    c.set("k", "old")
    time.sleep(0.02)
    assert c.get("k") is None
    c.set("k", "new")
    assert c.get("k") == "new"


@pytest.mark.parametrize("module,attr", [
    ("app.plex.client", "PlexClient"),
    ("app.sources.jellyfin", "JellyfinSource"),
    ("app.sources.subsonic", "SubsonicSource"),
])
def test_every_source_client_uses_the_bounded_cache(module, attr):
    """All three copied the unbounded version; none may keep a plain dict."""
    import importlib

    mod = importlib.import_module(module)
    assert hasattr(mod, attr), f"{module}.{attr} moved — update this test"
    src = open(mod.__file__, encoding="utf-8").read()
    assert "TTLCache(" in src, f"{module} does not use the bounded cache"
    assert "dict[str, _CacheEntry]" not in src, \
        f"{module} still declares the unbounded cache dict"
