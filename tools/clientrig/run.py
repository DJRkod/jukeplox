"""The multi-lane runner: teardown that verifies, and state that unwinds.

This lives in the foundations unit rather than inside a lane, because all three
lanes need it and the alternative -- whichever lane gets built first owning it --
leaves the other two reinventing the two things this project has been bitten by.

Teardown
--------
Recorded history: roughly 120 orphaned Chrome processes on 2026-06-10, and 140
from leaked perf-benchmark runs on 2026-06-24. Three rules fall out of that, and
all three are encoded below.

  * **Match on our own signature, never on a process name.** The user's real
    Chrome can be around thirty processes; other agent instances run as
    `node.exe`; the desktop app runs as `msedgewebview2`. The run tag appears in
    the browser's own command line via its profile directory, which is why a
    lane must launch with an explicit profile path rather than letting the
    automation library pick a random one -- otherwise there is nothing to match
    and this sweep silently finds nothing.
  * **Exclude yourself when signalling.** The invoking process's command line
    contains the very tag it is searching for. `pkill -f <pattern>` matching the
    invoking shell has destroyed measurements in this repo.
  * **Verify the residue, do not assume the kill worked.** Count after killing,
    not before, and fail the run loudly on anything left -- both recorded
    incidents were discovered by the user rather than by the harness.

State
-----
On 2026-09-24 a harness arm left `queue_end_behavior` flipped and the rig
auto-played roughly 3,000 tracks over nine days and twenty-one hours,
contaminating play history and play counts. The process sweep that day was
completely clean, because a mutated setting has no PID to look for. The guard
below snapshots only the keys a lane declares it touches, restores in a
`finally` so an exception cannot skip the unwind, and runs **once around the
whole run** rather than per-arm -- a per-arm restore would fight the next arm,
which needs the mutation.

Everything here is pure policy with I/O injected through a `Deps` bundle, in the
`tools/soak/arm.py` style, so all of it is provable with no browser and no
device attached.
"""
from __future__ import annotations

import contextlib
import itertools
import os
import time
from dataclasses import dataclass, field

_TAG_PREFIX = "jp-clientrig-"

# Wall-clock milliseconds are not fine-grained enough on their own: two tags
# minted in the same millisecond collide, and two colliding tags mean two runs
# sweep each other's browsers -- the precise failure the tag exists to prevent.
_seq = itertools.count()


def make_run_tag() -> str:
    """A token unique to this run, used for profile paths and the kill sweep.

    The pid separates concurrent processes, the timestamp makes it readable in a
    process list, and the sequence number guarantees uniqueness within a process
    regardless of clock resolution.
    """
    return (f"{_TAG_PREFIX}{os.getpid()}-{int(time.time() * 1000):x}"
            f"-{next(_seq):x}")


def stray_pids(processes: list[dict], run_tag: str, own_pid: int) -> list[int]:
    """Processes carrying this run's tag, excluding the caller itself.

    Signature matching decides what to FIND; self-exclusion decides what to
    SIGNAL. Conflating the two is the recorded way to kill your own measurement.
    """
    return [p["pid"] for p in processes
            if run_tag in (p.get("cmdline") or "") and p["pid"] != own_pid]


@dataclass(frozen=True)
class TeardownOutcome:
    residue: int
    ok: bool
    reason: str = ""
    killed: tuple = ()


def teardown(deps, run_tag: str, own_pid: int, *,
             settle_s: float = 0.0) -> TeardownOutcome:
    """Kill this run's browsers, then prove none are left.

    Two passes with a settle between them: a browser tree can spawn a
    replacement child while the first pass is walking the list.
    """
    killed: list[int] = []
    for _ in range(2):
        pids = stray_pids(deps.list_processes(), run_tag, own_pid)
        if not pids:
            break
        for pid in pids:
            deps.kill(pid)
            killed.append(pid)
        if settle_s:
            time.sleep(settle_s)

    residue = len(stray_pids(deps.list_processes(), run_tag, own_pid))
    if residue:
        return TeardownOutcome(
            residue=residue, ok=False, killed=tuple(killed),
            reason=(f"{residue} process(es) still carry {run_tag} after two "
                    f"kill passes -- sweep them by hand before trusting the "
                    f"next run"))
    return TeardownOutcome(residue=0, ok=True, killed=tuple(killed))


@contextlib.contextmanager
def settings_guard(deps, keys):
    """Snapshot the declared settings and restore them whatever happens.

    Only keys the lane declared are touched, and only those that actually
    changed are written back -- an unchanged setting needs no write, and a
    gratuitous write is itself a mutation.
    """
    keys = tuple(keys or ())
    before = deps.get_settings()
    snapshot = {k: before.get(k) for k in keys}
    try:
        yield snapshot
    finally:
        after = deps.get_settings()
        drifted = {k: v for k, v in snapshot.items() if after.get(k) != v}
        if drifted:
            deps.set_settings(**drifted)


@dataclass
class RunOutcome:
    results: list = field(default_factory=list)
    lane_order: dict = field(default_factory=dict)
    teardown: TeardownOutcome | None = None
    run_tag: str = ""


def run_lanes(deps, lanes, touches, own_pid: int,
              run_tag: str | None = None) -> RunOutcome:
    """Run each lane in order, then unwind state and processes exactly once.

    A lane that raises is recorded as **broken** -- not as a pass, and not as a
    defect that was not observed. Those three outcomes look identical in a bare
    boolean, and collapsing them is the failure this rig exists to prevent.
    """
    tag = run_tag or make_run_tag()
    out = RunOutcome(run_tag=tag)
    out.lane_order = {lane: i for i, lane in enumerate(lanes)}

    with settings_guard(deps, touches):
        for lane in lanes:
            try:
                res = deps.run_lane(lane)
            except Exception as exc:                      # lane-local failure
                out.results.append({
                    "lane": lane, "ok": False, "error": str(exc),
                    "note": "lane broken -- not a pass, and not a clean miss",
                })
                continue
            if isinstance(res, dict):
                res.setdefault("lane", lane)
            out.results.append(res)

    out.teardown = teardown(deps, tag, own_pid)
    return out
