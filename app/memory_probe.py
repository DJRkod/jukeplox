"""Admin-only memory attribution, built on ``tracemalloc``.

A long party soak showed RSS climbing steadily with no attribution — a measured
shape on a graph and nothing more. This turns that into a named allocation site:
start tracing, take a labelled baseline, run the load, take a second sample,
diff them.

Deliberately **off by default**. Tracing every allocation costs real CPU and
retains a frame record per live block, and the target hardware is a 2–4 GB
single-board machine — this is a diagnostic a maintainer switches on, not a
background service. ``tracemalloc`` is stdlib, so it needs no new dependency and
no special build; it can also be started against an already-running process,
which a profiler attached at launch cannot.

Snapshots live in process memory only. They are diagnostic state, not data, and
they disappear on restart — which is correct, since a restart also resets the
growth being measured.

Pure of FastAPI so it can be exercised directly; the routes in
``app.api.admin`` are thin wrappers over the functions here.
"""

from __future__ import annotations

import asyncio
import heapq
import logging
import threading
import time
import tracemalloc
from collections import OrderedDict
from typing import Any

_log = logging.getLogger(__name__)

# Frames per traceback. Deep enough to place an allocation inside our own call
# chain rather than at the library leaf that happened to allocate, shallow
# enough that the per-block overhead stays sane on a Pi.
DEFAULT_FRAMES = 12
MAX_FRAMES = 40

# Snapshots retain memory in proportion to the number of live allocation sites.
# A maintainer needs a baseline and a sample; the cap stops an interactive
# session from quietly becoming the leak it is investigating.
MAX_SNAPSHOTS = 8

DEFAULT_ENTRIES = 25
MAX_ENTRIES = 200

# Wall-clock ceiling on a tracing session. The caps above bound the diagnostic's
# own structures but not tracemalloc's per-live-block bookkeeping, which grows
# with the heap for as long as tracing is on — so an admin who starts the probe
# and forgets pays that cost until restart, on the machine whose memory was the
# problem in the first place. A session that outlives this is stopped for them.
MAX_TRACE_SECONDS = 30 * 60

_started_at: float = 0.0
# True only when THIS module called tracemalloc.start(). PYTHONTRACEMALLOC=1 or
# another component may already have it running, and stopping tracing we did not
# start would silently break whoever did.
_owned: bool = False
_autostop_task = None

# label -> (captured_at, tracemalloc.Snapshot), oldest first.
_snapshots: "OrderedDict[str, Any]" = OrderedDict()

# take() and diff() are dispatched to worker threads by the route layer (they
# are CPU-bound and would otherwise stall the audio stream on the event loop),
# so this state is genuinely concurrent — it is no longer protected by the
# single-threaded loop the way await-free loop-side mutators were.
_lock = threading.Lock()


class MemoryProbeError(Exception):
    """Raised for caller error (not tracing, unknown label, cap reached).

    ``kind`` lets the route layer pick a status code from the failure's meaning
    rather than from which route happened to raise: "not_found" for an unknown
    snapshot label (404, matching how the rest of the admin API reports unknown
    identifiers), "bad_request" for a malformed argument, "conflict" for a
    precondition the caller can fix by changing probe state.
    """

    def __init__(self, message: str, kind: str = "conflict"):
        super().__init__(message)
        self.kind = kind


def _drop_own_frames(snapshot):
    """Exclude tracemalloc's and this module's own allocations.

    Without this the probe shows up in its own results, which is noise at best
    and misleading at worst when the question is 'what is holding memory'.
    """
    return snapshot.filter_traces((
        tracemalloc.Filter(False, tracemalloc.__file__),
        tracemalloc.Filter(False, __file__),
    ))


def _snapshot_labels() -> list:
    with _lock:
        return list(_snapshots.keys())


def status() -> dict:
    """Current probe state. Safe to call whether or not tracing is on."""
    return {
        "tracing": tracemalloc.is_tracing(),
        "frames": tracemalloc.get_traceback_limit() if tracemalloc.is_tracing() else 0,
        "snapshots": _snapshot_labels(),
        "max_snapshots": MAX_SNAPSHOTS,
        "traced_kb": round(tracemalloc.get_traced_memory()[0] / 1024, 1)
        if tracemalloc.is_tracing() else 0.0,
        "peak_kb": round(tracemalloc.get_traced_memory()[1] / 1024, 1)
        if tracemalloc.is_tracing() else 0.0,
        "tracing_for_s": round(time.time() - _started_at, 1)
        if (tracemalloc.is_tracing() and _started_at) else 0.0,
        "auto_stop_after_s": MAX_TRACE_SECONDS,
        "probe_owns_tracing": _owned,
    }


def start(frames: int = DEFAULT_FRAMES) -> dict:
    """Begin tracing.

    Starting while already tracing is reported rather than silently honoured:
    restarting would discard the allocation history a baseline was taken
    against, which is exactly the measurement the caller is part-way through.
    """
    if frames < 1 or frames > MAX_FRAMES:
        raise MemoryProbeError(f"frames must be between 1 and {MAX_FRAMES}", "bad_request")
    global _started_at, _owned
    if tracemalloc.is_tracing():
        out = status()
        out["already_tracing"] = True
        return out
    tracemalloc.start(frames)
    _started_at = time.time()
    _owned = True
    _log.info("memory probe: tracing started (%d frames)", frames)
    out = status()
    out["already_tracing"] = False
    return out


def stop() -> dict:
    """Release every retained snapshot, and stop tracing if we started it.

    Idempotent. Tracing the probe did not start (PYTHONTRACEMALLOC, or another
    component) is left running: snapshots are ours to drop, the global tracer is
    not ours to claim.
    """
    global _started_at, _owned
    with _lock:
        _snapshots.clear()
    was = tracemalloc.is_tracing()
    stopped = False
    if was and _owned:
        tracemalloc.stop()
        stopped = True
        _log.info("memory probe: tracing stopped, snapshots cleared")
    elif was:
        _log.info("memory probe: snapshots cleared; tracing left running "
                  "(not started by the probe)")
    if stopped:
        _started_at, _owned = 0.0, False
    out = status()
    out["was_tracing"] = was
    out["stopped_tracing"] = stopped
    return out


async def arm_autostop() -> None:
    """(Re)arm the wall-clock ceiling on the current tracing session."""
    global _autostop_task
    cancel_autostop()
    _autostop_task = asyncio.create_task(_autostop())


def cancel_autostop() -> None:
    global _autostop_task
    task, _autostop_task = _autostop_task, None
    if task is not None and not task.done():
        task.cancel()


async def _autostop() -> None:
    try:
        await asyncio.sleep(MAX_TRACE_SECONDS)
    except asyncio.CancelledError:
        return
    if tracemalloc.is_tracing() and _owned:
        _log.warning("memory probe: auto-stopping after %ds — a tracing session "
                     "left running costs memory on the box it is diagnosing",
                     MAX_TRACE_SECONDS)
        stop()


def take(label: str, replace: bool = False) -> dict:
    """Capture a labelled snapshot.

    Reusing a label is refused unless *replace* is set. A snapshot can take
    seconds on a large heap, which is long enough to trip a client timeout — and
    a blind retry that silently overwrote the baseline would leave the caller
    diffing a sample against itself and concluding, wrongly, that nothing grew.
    """
    label = (label or "").strip()
    if not label:
        raise MemoryProbeError("label must not be empty", "bad_request")
    if not tracemalloc.is_tracing():
        raise MemoryProbeError("not tracing — start the probe first")
    with _lock:
        if label in _snapshots and not replace:
            raise MemoryProbeError(
                f"snapshot {label!r} already exists; pass replace=true to overwrite")
        if label not in _snapshots and len(_snapshots) >= MAX_SNAPSHOTS:
            raise MemoryProbeError(
                f"snapshot limit reached ({MAX_SNAPSHOTS}); stop the probe to clear")

    snap = _drop_own_frames(tracemalloc.take_snapshot())
    current, peak = tracemalloc.get_traced_memory()
    with _lock:
        _snapshots[label] = (time.time(), snap)
        _snapshots.move_to_end(label)
        labels = list(_snapshots.keys())
    _log.info("memory probe: snapshot %r at %.1f KB traced", label, current / 1024)
    return {
        "label": label,
        "traced_kb": round(current / 1024, 1),
        "peak_kb": round(peak / 1024, 1),
        "snapshots": labels,
    }


def diff(before: str, after: str, limit: int = DEFAULT_ENTRIES) -> dict:
    """Rank the allocation sites that grew between two snapshots.

    ``limit`` is clamped rather than honoured blindly — a pathological
    allocation profile has tens of thousands of distinct sites and returning all
    of them would make the diagnostic a denial of service against the box it is
    diagnosing.
    """
    with _lock:
        if len(_snapshots) < 2:
            raise MemoryProbeError("need at least two snapshots to diff")
        for label in (before, after):
            if label not in _snapshots:
                raise MemoryProbeError(f"unknown snapshot {label!r}", "not_found")
        if before == after:
            raise MemoryProbeError(
                "before and after must be different snapshots", "bad_request")
        before_at, before_snap = _snapshots[before]
        after_at, after_snap = _snapshots[after]

    limit = max(1, min(int(limit), MAX_ENTRIES))
    stats = after_snap.compare_to(before_snap, "traceback")
    # compare_to already sorts, but by ABSOLUTE difference — the biggest
    # shrink outranks a smaller growth. We want growth first. nlargest rather
    # than a second full sort: on a process with tens of thousands of live
    # allocation sites the sort dominates, and `limit` is at most 200.
    top = heapq.nlargest(limit, stats, key=lambda s: s.size_diff)

    entries = []
    for stat in top:
        entries.append({
            "size_diff_kb": round(stat.size_diff / 1024, 1),
            "size_kb": round(stat.size / 1024, 1),
            "count_diff": stat.count_diff,
            "count": stat.count,
            "traceback": [str(frame) for frame in stat.traceback],
        })

    return {
        "before": before,
        "after": after,
        # Epochs let a caller see the window each side actually covers, so a
        # retried or stale snapshot is visible rather than silently assumed.
        "before_at": round(before_at, 3),
        "after_at": round(after_at, 3),
        "elapsed_s": round(after_at - before_at, 3),
        "total_diff_kb": round(sum(s.size_diff for s in stats) / 1024, 1),
        "sites": len(stats),
        "truncated": len(stats) > limit,
        "entries": entries,
    }
