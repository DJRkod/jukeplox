"""LRU-capped on-disk cache for Plex art proxy responses.

Plex art URLs embed a version stamp (``/library/metadata/<id>/thumb/<version>``),
so the URL itself acts as a content hash — entries can be treated as immutable
and stale ones are simply unreachable rather than wrong. The cache therefore
needs no invalidation logic; LRU eviction handles size management.

Public API:
- ``async get(path) -> (bytes, content_type) | None``
- ``async put(path, data, content_type) -> None``  (best-effort; write failures log WARNING but do not raise)
- ``enforce_cap_blocking() -> None``  (called at app startup; sync)

Disk layout (under ``data_dir``):
- Two-char sharded subdirectories: ``ab/abc123…``  (avoids "too many files
  per directory" on both ext4 and ZFS).
- Per entry: ``<basename>`` holds raw bytes; ``<basename>.meta`` holds one
  line of JSON with the content type. Two files share the same SHA-256
  hash of the original path (KTD1, KTD2).

Threading model: file I/O runs in the default thread-pool executor via
``loop.run_in_executor``. The in-memory LRU index is guarded by an
``asyncio.Lock`` so concurrent ``get``/``put`` calls don't corrupt the
OrderedDict (DI3).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)


@dataclass
class _Entry:
    """In-memory LRU index entry. Values match what os.stat() reports for
    the bytes file (not the .meta sidecar — meta is ~50 bytes, ignored in
    the size accounting because it's both noise and overhead-neutral)."""
    size: int
    content_type: str


class ArtCache:
    def __init__(self, data_dir: Path, size_mb: int) -> None:
        self._dir = Path(data_dir)
        self._cap_bytes = max(0, size_mb) * 1024 * 1024
        # OrderedDict: most-recently-used at the right end.
        self._index: "OrderedDict[str, _Entry]" = OrderedDict()
        self._size_bytes = 0
        self._lock = asyncio.Lock()
        # Defer index population to first access OR enforce_cap_blocking(),
        # whichever fires first — avoids touching disk at import time.
        self._loaded = False
        # Async loader coordination. The first get/put that touches an
        # unloaded cache kicks off `_load_index_from_disk` in an executor and
        # sets `_load_event` when the load finishes. Concurrent callers wait
        # on the event instead of all entering the per-request lock — this
        # keeps a slow cold load (large on-disk cache) from blocking every
        # request behind one lock holder.
        self._load_event: asyncio.Event | None = None

    # ── public API ────────────────────────────────────────────────────────────

    async def get(self, path: str) -> Optional[tuple[bytes, str]]:
        """Return cached ``(bytes, content_type)`` or ``None`` on miss.

        On hit, the entry is moved to the LRU's MRU position. Disk reads
        happen in a thread-pool executor so the asyncio loop is never blocked.
        """
        await self._ensure_loaded_async()
        key = _hash(path)
        async with self._lock:
            entry = self._index.get(key)
            if entry is None:
                return None
            # Move to MRU position before releasing the lock.
            self._index.move_to_end(key)
        # Read outside the lock — multiple concurrent reads of distinct keys
        # shouldn't serialize on the index lock.
        try:
            data = await asyncio.get_running_loop().run_in_executor(
                None, _read_bytes, self._bytes_path(key),
            )
        except FileNotFoundError:
            # Bytes file vanished between index lookup and read (e.g., manual
            # cleanup). Drop the index entry and report a miss.
            async with self._lock:
                ev = self._index.pop(key, None)
                if ev is not None:
                    self._size_bytes -= ev.size
            return None
        except Exception:
            _log.warning("art cache read failed for key=%s", key[:12], exc_info=True)
            return None
        return data, entry.content_type

    async def put(self, path: str, data: bytes, content_type: str) -> None:
        """Write the entry to disk and update the LRU index.

        Best-effort: filesystem errors (disk full, permission denied) log a
        WARNING but do not raise — caching is an optimization, not a
        correctness requirement (KTD5).
        """
        if self._cap_bytes == 0:
            return  # disabled
        await self._ensure_loaded_async()
        key = _hash(path)
        size = len(data)
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, _write_atomic, self._bytes_path(key), self._meta_path(key), data, content_type,
            )
        except Exception:
            _log.warning("art cache write failed for key=%s", key[:12], exc_info=True)
            return
        async with self._lock:
            # If we're overwriting an existing entry, subtract its old size first.
            old = self._index.pop(key, None)
            if old is not None:
                self._size_bytes -= old.size
            self._index[key] = _Entry(size=size, content_type=content_type)
            self._size_bytes += size
            await self._evict_to_cap()

    def enforce_cap_blocking(self) -> None:
        """Walk the cache dir, populate the in-memory index, and evict down
        to the cap. Called once at app startup, synchronously, before the
        asyncio loop is running.

        Wraps the heavy work in try/except so a corrupt cache directory
        cannot break startup (caching is non-critical).
        """
        try:
            self._load_index_from_disk()
            self._loaded = True
            self._evict_to_cap_sync()
        except Exception:
            _log.warning("art cache startup enforcement failed", exc_info=True)

    # Cap on per-pass evictions when running enforcement off the lifespan
    # path — protects against a single task pinning the executor when an
    # oversize cache holds tens of thousands of entries. The caller schedules
    # repeat passes until the size is under the cap.
    _ASYNC_EVICT_PER_PASS = 1000

    async def enforce_cap_async(self) -> None:
        """Async-friendly startup enforcement.

        Wraps `_load_index_from_disk` in `run_in_executor` so the disk scan
        doesn't block the asyncio loop, then evicts down to the cap in
        bounded passes (`_ASYNC_EVICT_PER_PASS` entries per pass) yielding
        back to the loop between passes — an oversize cache scanned at
        startup shouldn't monopolise the background task slot.

        Best-effort: any exception is logged and swallowed. Caching is
        non-critical; serving must continue even when the cache is broken.
        """
        try:
            if not self._loaded:
                try:
                    await asyncio.get_running_loop().run_in_executor(
                        None, self._load_index_from_disk,
                    )
                finally:
                    # Mark loaded + signal any concurrent `_ensure_loaded_async`
                    # waiters even on load failure so they don't hang.
                    self._loaded = True
                    if self._load_event is not None:
                        self._load_event.set()
            # Bounded eviction loop: do at most `_ASYNC_EVICT_PER_PASS`
            # evictions per pass under the lock, then yield. Keeps a huge
            # cache from holding the lock for the entire eviction.
            while True:
                async with self._lock:
                    removed = 0
                    while (
                        self._size_bytes > self._cap_bytes
                        and self._index
                        and removed < self._ASYNC_EVICT_PER_PASS
                    ):
                        key, entry = next(iter(self._index.items()))
                        self._index.pop(key, None)
                        self._size_bytes -= entry.size
                        await asyncio.get_running_loop().run_in_executor(
                            None, _unlink_pair,
                            self._bytes_path(key), self._meta_path(key),
                        )
                        removed += 1
                    over_cap = self._size_bytes > self._cap_bytes and bool(self._index)
                if not over_cap:
                    return
                # Yield to the loop between passes so other tasks make progress.
                await asyncio.sleep(0)
        except Exception:
            _log.warning("art cache async startup enforcement failed", exc_info=True)

    # ── internal helpers ──────────────────────────────────────────────────────

    def _bytes_path(self, key: str) -> Path:
        return self._dir / key[:2] / key

    def _meta_path(self, key: str) -> Path:
        return self._dir / key[:2] / f"{key}.meta"

    async def _ensure_loaded_async(self) -> None:
        """One-shot async index load coordinated via `_load_event`.

        Design:
        - First caller into an unloaded cache creates the Event (under the
          per-request lock so two callers can't both create one) and runs
          `_load_index_from_disk` via `run_in_executor` outside the lock —
          the disk scan must not block the event loop.
        - Subsequent callers find the Event already created and wait on it.
        - Once `_loaded` is True, the fast path (no-op) skips everything.
        """
        if self._loaded:
            return
        async with self._lock:
            if self._loaded:
                return
            if self._load_event is None:
                # First caller — own the load.
                self._load_event = asyncio.Event()
                owner = True
            else:
                owner = False
        if owner:
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, self._load_index_from_disk,
                )
            except Exception:
                _log.warning("art cache async index load failed", exc_info=True)
            finally:
                # Mark loaded and signal waiters even on failure — caching is
                # non-critical, and leaving callers blocked on a failed load
                # would freeze every art-proxy request behind a broken cache.
                self._loaded = True
                self._load_event.set()
        else:
            # Wait for the owning caller's load to finish.
            await self._load_event.wait()

    def _load_index_from_disk(self) -> None:
        """Scan data_dir for cache entries and populate the index in
        ctime order (treats startup-time ordering as a deterministic LRU
        approximation — no real access-time tracking survives restart).

        Side effect: orphan bytes files (bytes present, no .meta sidecar)
        are unlinked rather than silently skipped — otherwise they leak
        disk space forever because `_load_index_from_disk` is the only
        code path that scans the directory. Failures to unlink are
        non-fatal; caching is best-effort (KTD5)."""
        self._index.clear()
        self._size_bytes = 0
        if not self._dir.exists():
            return
        rows: list[tuple[float, str, _Entry]] = []
        orphans_removed = 0
        for shard in self._dir.iterdir():
            if not shard.is_dir() or len(shard.name) != 2:
                continue
            for entry_path in shard.iterdir():
                # Skip meta sidecars and partial-write temp files; only iterate
                # the bytes files. Meta is recovered alongside.
                name = entry_path.name
                if name.endswith(".meta") or name.endswith(".tmp"):
                    continue
                meta_path = entry_path.with_name(f"{name}.meta")
                if not meta_path.exists():
                    # Partial state per DI2 — bytes file without meta. The
                    # bytes are unreachable (no content_type → can't be
                    # served), so reclaim the disk space instead of leaking.
                    try:
                        entry_path.unlink()
                        orphans_removed += 1
                    except Exception:
                        _log.debug("art cache: failed to unlink orphan %s",
                                   entry_path, exc_info=True)
                    continue
                try:
                    size = entry_path.stat().st_size
                    content_type = _read_meta_content_type(meta_path)
                except Exception:
                    continue
                if content_type is None:
                    continue
                rows.append((entry_path.stat().st_mtime, name, _Entry(size=size, content_type=content_type)))
        # Insert in mtime order — oldest first → ends up at the LRU end (left).
        rows.sort(key=lambda r: r[0])
        for _, key, entry in rows:
            self._index[key] = entry
            self._size_bytes += entry.size
        if orphans_removed:
            _log.info("art cache: removed %d orphan bytes file(s) on index load",
                      orphans_removed)

    async def _evict_to_cap(self) -> None:
        """Async eviction loop. Caller must hold self._lock."""
        while self._size_bytes > self._cap_bytes and self._index:
            key, entry = next(iter(self._index.items()))  # LRU end
            self._index.pop(key, None)
            self._size_bytes -= entry.size
            # Best-effort file removal off the loop.
            await asyncio.get_running_loop().run_in_executor(
                None, _unlink_pair, self._bytes_path(key), self._meta_path(key),
            )

    def _evict_to_cap_sync(self) -> None:
        """Sync eviction for startup-time enforcement."""
        while self._size_bytes > self._cap_bytes and self._index:
            key, entry = next(iter(self._index.items()))
            self._index.pop(key, None)
            self._size_bytes -= entry.size
            _unlink_pair(self._bytes_path(key), self._meta_path(key))

    # ── observability (not strictly required, useful for ops) ─────────────────

    @property
    def size_bytes(self) -> int:
        return self._size_bytes

    @property
    def entry_count(self) -> int:
        return len(self._index)


# ── module-level helpers (kept outside the class so they're trivially mockable) ──

def _hash(path: str) -> str:
    """SHA-256 hex of the validated Plex path (KTD1)."""
    return hashlib.sha256(path.encode("utf-8")).hexdigest()


def _read_bytes(path: Path) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _read_meta_content_type(meta_path: Path) -> Optional[str]:
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.loads(f.read()).get("content_type")
    except Exception:
        return None


def _write_atomic(bytes_path: Path, meta_path: Path, data: bytes, content_type: str) -> None:
    """Write both files atomically via tmp+rename (KTD5; DI2).

    Order: write meta first, then bytes. On crash between writes, the bytes
    file is absent → next get() returns None. Either both present or one
    rebuilt by the next put().

    Tmp filenames embed pid + a short uuid suffix so concurrent puts on the
    same key (two requests racing to warm the same path) don't trample each
    other's in-flight tmp files — the deterministic `<name>.tmp` shape
    pre-fix was a write/write-on-same-key hazard: writer A's `os.replace`
    of the tmp file could collide with writer B opening that same tmp name
    for write, producing a torn final file or a stray exception in the
    finally-block unlink.
    """
    bytes_path.parent.mkdir(parents=True, exist_ok=True)
    suffix = f"{os.getpid()}.{uuid.uuid4().hex[:8]}"
    meta_tmp = meta_path.with_name(f"{meta_path.name}.{suffix}.tmp")
    bytes_tmp = bytes_path.with_name(f"{bytes_path.name}.{suffix}.tmp")
    try:
        with open(meta_tmp, "w", encoding="utf-8") as f:
            json.dump({"content_type": content_type}, f)
        os.replace(meta_tmp, meta_path)
        with open(bytes_tmp, "wb") as f:
            f.write(data)
        os.replace(bytes_tmp, bytes_path)
    finally:
        # Clean up any leftover tmp files from a partial failure.
        for p in (meta_tmp, bytes_tmp):
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass


def _unlink_pair(bytes_path: Path, meta_path: Path) -> None:
    for p in (bytes_path, meta_path):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            _log.debug("art cache: failed to unlink %s", p, exc_info=True)
