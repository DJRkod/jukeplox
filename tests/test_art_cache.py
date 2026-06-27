"""Tests for app.cache.art_cache.ArtCache — disk-backed, asyncio-aware."""

import asyncio
import json
from pathlib import Path

import pytest

from app.cache.art_cache import ArtCache, _hash


def _mk_cache(tmp_path: Path, size_mb: int = 1) -> ArtCache:
    """Helper: cache rooted under a pytest tmp_path with the given cap."""
    return ArtCache(data_dir=tmp_path / "art-cache", size_mb=size_mb)


# ── happy path / round trip ────────────────────────────────────────────────────

async def test_put_then_get_returns_bytes_and_content_type(tmp_path):
    """Covers AE1 — the core read/write round trip."""
    cache = _mk_cache(tmp_path)
    await cache.put("plex/path/A", b"image-bytes", "image/jpeg")
    result = await cache.get("plex/path/A")
    assert result == (b"image-bytes", "image/jpeg")


async def test_get_miss_returns_none(tmp_path):
    cache = _mk_cache(tmp_path)
    assert await cache.get("never-cached") is None


async def test_content_type_round_trip_png_not_defaulted_to_jpeg(tmp_path):
    cache = _mk_cache(tmp_path)
    await cache.put("path/png", b"abc", "image/png")
    bytes_, ct = await cache.get("path/png")
    assert ct == "image/png"


async def test_put_overwrites_existing_entry(tmp_path):
    cache = _mk_cache(tmp_path)
    await cache.put("p", b"old", "image/jpeg")
    await cache.put("p", b"new-larger", "image/png")
    bytes_, ct = await cache.get("p")
    assert bytes_ == b"new-larger"
    assert ct == "image/png"
    # Size accounting should reflect only the new entry, not double-count.
    assert cache.size_bytes == len(b"new-larger")


# ── eviction & LRU ordering ────────────────────────────────────────────────────

async def test_eviction_drops_oldest_when_cap_exceeded(tmp_path):
    """Cap of 100 bytes; two 60-byte entries can't both fit."""
    cache = ArtCache(data_dir=tmp_path / "art-cache", size_mb=0)
    # Force tiny cap by overriding internal byte-cap directly (size_mb=0
    # is the disabled path).
    cache._cap_bytes = 100
    await cache.put("a", b"x" * 60, "image/jpeg")
    await cache.put("b", b"y" * 60, "image/jpeg")
    assert await cache.get("a") is None  # evicted
    assert (await cache.get("b"))[0] == b"y" * 60


async def test_lru_ordering_moves_touched_entry_to_mru(tmp_path):
    """Touching 'a' before inserting 'c' should evict 'b' (now the LRU), not 'a'."""
    cache = ArtCache(data_dir=tmp_path / "art-cache", size_mb=0)
    cache._cap_bytes = 120
    await cache.put("a", b"x" * 50, "image/jpeg")
    await cache.put("b", b"y" * 50, "image/jpeg")
    # Touch 'a' → moves to MRU; 'b' is now the LRU.
    await cache.get("a")
    await cache.put("c", b"z" * 50, "image/jpeg")
    assert await cache.get("a") is not None
    assert await cache.get("b") is None
    assert await cache.get("c") is not None


async def test_put_zero_cap_is_disabled_path(tmp_path):
    """size_mb=0 means caching disabled — nothing written to disk, no index entry."""
    cache = _mk_cache(tmp_path, size_mb=0)
    await cache.put("p", b"abc", "image/jpeg")
    assert await cache.get("p") is None
    assert cache.size_bytes == 0
    # No directory created either (lazy mkdir is gated on actual writes).
    assert not (tmp_path / "art-cache").exists()


# ── persistence across instance restart ───────────────────────────────────────

async def test_persistence_across_instance_restart(tmp_path):
    """Drop the in-memory cache, instantiate a new one over the same dir,
    verify both entries are still reachable."""
    c1 = _mk_cache(tmp_path)
    await c1.put("a", b"alpha", "image/jpeg")
    await c1.put("b", b"beta-beta", "image/png")
    # Simulate process restart — new ArtCache over the same dir.
    c2 = _mk_cache(tmp_path)
    a = await c2.get("a")
    b = await c2.get("b")
    assert a == (b"alpha", "image/jpeg")
    assert b == (b"beta-beta", "image/png")
    assert c2.entry_count == 2


# ── startup-time cap enforcement ──────────────────────────────────────────────

async def test_startup_enforcement_shrinks_oversized_cache(tmp_path):
    """Covers AE3 — pre-populate disk over cap, then enforce cap on startup."""
    # Phase 1: instantiate a generous cache and load it with two entries.
    big = _mk_cache(tmp_path)
    await big.put("old", b"x" * 60, "image/jpeg")
    await big.put("new", b"y" * 60, "image/jpeg")
    assert big.size_bytes == 120

    # Phase 2: open a fresh instance with a small cap and enforce.
    small = ArtCache(data_dir=tmp_path / "art-cache", size_mb=0)
    small._cap_bytes = 100  # tighter than current contents
    small.enforce_cap_blocking()

    # One of them must be gone; on-disk footprint must be ≤ cap.
    assert small.size_bytes <= 100
    surviving = (await small.get("old") is not None) + (await small.get("new") is not None)
    assert surviving == 1


async def test_startup_enforcement_no_op_when_under_cap(tmp_path):
    cache = _mk_cache(tmp_path)
    await cache.put("a", b"x" * 10, "image/jpeg")
    fresh = _mk_cache(tmp_path)
    fresh.enforce_cap_blocking()
    assert (await fresh.get("a"))[0] == b"x" * 10


async def test_startup_enforcement_on_missing_dir_is_noop(tmp_path):
    """Fresh install with no /data/art-cache/ yet → enforce_cap_blocking
    must not raise."""
    cache = _mk_cache(tmp_path)
    assert not (tmp_path / "art-cache").exists()
    cache.enforce_cap_blocking()  # must not raise
    assert cache.size_bytes == 0


# ── atomic write & partial-state recovery (DI2) ───────────────────────────────

async def test_partial_write_does_not_appear_as_cache_hit(tmp_path):
    """Bytes file present but no .meta sidecar → treated as a miss; the next
    put() overwrites cleanly. Mirrors DI2's interrupted-write recovery."""
    cache = _mk_cache(tmp_path)
    key = _hash("partial")
    bytes_path = cache._bytes_path(key)
    bytes_path.parent.mkdir(parents=True, exist_ok=True)
    bytes_path.write_bytes(b"orphaned")
    # No meta sidecar. Index rebuild should ignore this.
    fresh = _mk_cache(tmp_path)
    assert await fresh.get("partial") is None
    # And the next put cleanly overwrites.
    await fresh.put("partial", b"correct", "image/jpeg")
    bytes_, ct = await fresh.get("partial")
    assert bytes_ == b"correct"
    assert ct == "image/jpeg"


async def test_load_index_unlinks_orphan_bytes_files(tmp_path, caplog):
    """Orphan bytes (no .meta sidecar) on the disk → `_load_index_from_disk`
    unlinks them so the disk space is reclaimed and the count is logged.
    Without this, partial-write debris from a crashed put() would accumulate
    forever — `_load_index_from_disk` is the only code path that scans the
    directory, so silently-skipped orphans never get cleaned up."""
    import logging
    cache = _mk_cache(tmp_path)
    # Plant one legit pair first so the loader still does normal work.
    await cache.put("legit", b"abc", "image/jpeg")
    # Now plant two orphan bytes files under distinct shards. We do this
    # after the put so the put's `_ensure_loaded` call doesn't already
    # sweep them — the fresh cache below is what runs the orphan-cleanup
    # path under test.
    for path_key in ("orphan-A", "orphan-B"):
        key = _hash(path_key)
        bytes_path = cache._bytes_path(key)
        bytes_path.parent.mkdir(parents=True, exist_ok=True)
        bytes_path.write_bytes(b"orphaned")
        assert bytes_path.exists()

    # Force an index reload by instantiating a fresh cache over the same dir.
    fresh = _mk_cache(tmp_path)
    caplog.set_level(logging.INFO, logger="app.cache.art_cache")
    fresh.enforce_cap_blocking()

    # Orphans gone, legit entry intact.
    for path_key in ("orphan-A", "orphan-B"):
        assert not fresh._bytes_path(_hash(path_key)).exists()
    assert (await fresh.get("legit"))[0] == b"abc"
    # Cleanup counter surfaced.
    assert any("orphan" in rec.message.lower() and "2" in rec.message
               for rec in caplog.records), (
        f"Expected INFO log mentioning orphan count of 2; got: "
        f"{[rec.message for rec in caplog.records]}"
    )


async def test_meta_without_bytes_is_treated_as_miss(tmp_path):
    """Inverse partial state: meta present, bytes missing."""
    cache = _mk_cache(tmp_path)
    key = _hash("meta-only")
    meta_path = cache._meta_path(key)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps({"content_type": "image/jpeg"}))
    fresh = _mk_cache(tmp_path)
    assert await fresh.get("meta-only") is None


async def test_get_handles_bytes_file_vanishing_between_lookup_and_read(tmp_path):
    """If something deletes the bytes file between the index lookup and the
    read (e.g., manual cleanup), get() recovers gracefully and the index
    entry is dropped."""
    cache = _mk_cache(tmp_path)
    await cache.put("p", b"abc", "image/jpeg")
    # Manually delete the bytes file.
    bytes_path = cache._bytes_path(_hash("p"))
    bytes_path.unlink()
    # Index still has the entry; get() handles the FileNotFoundError.
    assert await cache.get("p") is None
    # Entry was pruned from the index by the recovery path.
    assert _hash("p") not in cache._index


# ── concurrency ────────────────────────────────────────────────────────────────

async def test_concurrent_puts_on_distinct_keys_both_succeed(tmp_path):
    cache = _mk_cache(tmp_path)
    await asyncio.gather(
        cache.put("k1", b"alpha", "image/jpeg"),
        cache.put("k2", b"beta", "image/png"),
    )
    a = await cache.get("k1")
    b = await cache.get("k2")
    assert a == (b"alpha", "image/jpeg")
    assert b == (b"beta", "image/png")
    assert cache.size_bytes == len(b"alpha") + len(b"beta")


async def test_concurrent_put_put_on_same_key(tmp_path):
    """Two concurrent puts for the same key must not corrupt the final entry.

    Before the unique-tmp-filename fix, both writers used the same
    deterministic `<name>.tmp` path, so writer A's `os.replace` could race
    with writer B opening the tmp file for write — producing a torn final
    file or a stray exception out of the finally-block unlink.

    After the fix, each writer's tmp filename embeds pid + a short uuid,
    so they cannot collide. The final file should equal one of the two
    payloads (whichever os.replace landed last); no torn read, no partial
    bytes.
    """
    cache = _mk_cache(tmp_path)
    payload_a = b"a" * 4096
    payload_b = b"b" * 4096

    async def writer(payload, content_type):
        await cache.put("collide", payload, content_type)

    # Many iterations: the race window per put is tiny, so a single
    # gather() rarely hits it. Loop until we either prove safety or
    # blow up — under deterministic-tmp pre-fix code, this loop produces
    # an OSError/torn bytes within tens of iterations on most filesystems.
    for _ in range(40):
        await asyncio.gather(
            writer(payload_a, "image/jpeg"),
            writer(payload_b, "image/png"),
        )
        result = await cache.get("collide")
        assert result is not None
        data, ct = result
        # Final bytes equal one of the two complete payloads — never a mix.
        assert data in (payload_a, payload_b)
        # Content-type matches the winning payload.
        assert ct in ("image/jpeg", "image/png")
        # Size accounting matches what's on disk (no double-counting).
        assert cache.size_bytes == len(data)


async def test_concurrent_put_and_get_on_same_key_no_partial(tmp_path):
    """Concurrent put + get on the same key: get either returns None
    (write hasn't completed) or the full bytes (write completed atomically),
    never a partial read."""
    cache = _mk_cache(tmp_path)
    data = b"x" * 1024

    async def writer():
        await cache.put("p", data, "image/jpeg")

    async def reader():
        return await cache.get("p")

    # Run several iterations to make a partial-read regression visible.
    for _ in range(20):
        await asyncio.gather(writer(), reader(), reader())
        result = await cache.get("p")
        assert result is not None
        assert result[0] == data  # never partial


# ── write failure non-fatal (KTD5) ────────────────────────────────────────────

async def test_put_swallows_write_failure(tmp_path, monkeypatch):
    """A disk-full or permission-denied write must not raise out of put()."""
    cache = _mk_cache(tmp_path)
    from app.cache import art_cache as mod

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(mod, "_write_atomic", boom)
    # Must not raise.
    await cache.put("p", b"data", "image/jpeg")
    # And the entry should NOT be in the index (the write failed).
    assert await cache.get("p") is None
    assert _hash("p") not in cache._index
