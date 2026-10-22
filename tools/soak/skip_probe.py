"""Skip-to-audible probe — measure how long a listener keeps hearing the track
they just skipped away from, on the Cast gapless (flow) path.

Why this exists separately from ``boundary_probe.py``: that probe answers "is
THIS boundary clean", and its entire instrument reasons about **silent
intervals** — ``parse_silences`` -> ``boundary_silences`` ->
``post_boundary_total_s`` -> ``verdict``. During the window issue #68 is about
the speaker is not silent, it is playing the *rejected track*. Nothing in
``rig/features.py`` identifies content (its features are silences, levels,
per-channel RMS and duration), so that chain would correctly pin the transition
at the skip instant, then report approximately zero silence and score the defect
**clean** while it is in full effect. This is a different quantity, not a
different threshold.

The trigger, and why it is what it is:

    skip-to-audible = the first moment the DEVICE's own reported position maps
    to the track the listener asked for.

Two tempting alternatives are both wrong, and wrong in the direction that
invents an improvement:

- **Now Playing + a positive position.** Now Playing follows the boundary
  clock and names the requested track the instant the queue advances, which is
  precisely the stale window being measured. Meanwhile ``/api/playback/position``
  maps DEVICE time through the stitch, so while the device is still inside the
  old track it reports that old track's position — a positive number. The pair
  is therefore true at t+0 and the measurement reads ~0 on a broken build.
- **"Device time crosses the new track's boundary offset."** Works pre-fix, but
  a fresh session's stitch origin IS device zero, so post-fix the condition is
  satisfied by the first status after the LOAD and the metric silently becomes
  time-to-first-status. The before/after series would then compare a
  poll-observed quantity against a pushed one.

``heard_track_id`` from ``GET /admin/diagnostics/flow`` is the device's own
answer to "what am I playing", via ``FlowSession.track_at`` on the
device-reported stitch position. It is one rule that instantiates identically on
both arms, so a before/after series is comparable by construction.

Layering: pure policy first, every byte of I/O injected through
``boundary_probe.Deps`` (reused, not re-implemented, so the redaction and
cookie-session behaviour cannot drift between the two probes). ``RigState`` is
reused for the same reason — it refuses to run when a managed setting cannot be
snapshotted, which is the guard that stops a run leaving the rig auto-DJing.
The rig target and admin credential come from the environment only;
``tests/test_soak_harness_hygiene.py`` scans this tree for committed hosts and
secrets.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

_SOAK_DIR = os.path.dirname(os.path.abspath(__file__))
if _SOAK_DIR not in sys.path:
    sys.path.insert(0, _SOAK_DIR)

# Reused rather than copied: one definition of refusal, one of the achieved
# sampling rate, one of rig-state snapshot/restore, one HTTP client with
# password redaction already wired through it.
from boundary_probe import (  # noqa: E402
    Deps,
    ProbeRefusal,
    RigState,
    achieved_interval_ms,
)

# ── constants ────────────────────────────────────────────────────────────────

#: Poll period for the flow-diagnostics timeline. Far below FLOW_STATUS_POLL_S
#: (5.0s) because that cadence is an order of magnitude coarser than the effect.
#: HTTP-only on purpose: rig/state_sampler.py shells out to docker twice per
#: sample, which floors it near 1 Hz regardless of what is requested.
#:
#: Deliberately not faster. Each sample costs TWO requests (diagnostics plus
#: now-playing for the paused check), so 0.15s is ~13 req/s against an SBC that
#: is simultaneously decoding, encoding and polling Plex. The effect under test
#: is seconds wide and MAX_ACHIEVED_INTERVAL_MS allows 250ms, so a faster rate
#: buys no resolution and makes the harness a larger confound in its own
#: dataset. Collapsing the loop to one request is the next improvement here.
POLL_INTERVAL_S = 0.15

#: A measurement is not interpretable when the sampler ran coarser than this.
#: Deliberately a fraction of the smallest effect worth reporting rather than a
#: round number: the ACHIEVED interval bounds the resolution of every figure
#: derived from the timeline.
MAX_ACHIEVED_INTERVAL_MS = 250.0

#: How long to keep polling after a skip before giving up on the device ever
#: reporting the requested track. Generous against the observed 3-7s so a slow
#: run reads as a large number rather than a refusal.
WATCH_TIMEOUT_S = 40.0

#: Sentinels. A silent 0 reads exactly like a perfect fix, so every failure
#: mode gets its own encoding and the analysis step can tell them apart.
UNREACHED = -1      # device never reported the requested track
NO_DEVICE_TIME = -2  # no device report, so the lead would be a restated constant

#: Settings this probe mutates. Mirrors boundary_probe's split for the same
#: load-bearing reason: GET /admin/settings does not carry the output routing
#: keys, so snapshotting all of them from one endpoint silently yields None for
#: the routing three and a restore would leave the rig pointed somewhere else.
MANAGED_SETTINGS = ("gapless_enabled", "queue_end_behavior")
MANAGED_OUTPUT = ("backend_type", "device_id", "host")


# ── pure policy ──────────────────────────────────────────────────────────────


#: The device clock's own update interval is a SECOND, independent resolution
#: bound. The probe can poll HTTP at 5 Hz and still be reading a device clock
#: that only steps every 5 s (FLOW_STATUS_POLL_S). Measured on the rig
#: 2026-10-09: at the production cadence six skips all read 15.9 s +/- 0.2 s
#: while the buffered lead varied 22-40 s -- a quantised figure wearing a
#: precise achieved-interval number. Raise the device cadence for a run with
#: JP_FLOW_STATUS_POLL_S.
MAX_DEVICE_INTERVAL_MS = 1200.0

#: How stale the device clock may be and still support an extrapolated figure.
#: Generous, because extrapolation is arithmetically fine across a long gap of
#: CONTINUOUS playback -- the risk is a pause or re-buffer inside the gap, which
#: becomes likely rather than possible somewhere past a minute or two.
MAX_DEVICE_AGE_MS = 120_000


def achieved_device_interval_ms(rows: list[dict]) -> float | None:
    """Real device-status cadence, read from ``device_time_age_ms`` sawtooths.

    Deliberately NOT computed from ``device_stream_ms`` transitions. That field
    is now extrapolated from the last status (the backend mirrors
    ``get_position``), so it advances on every sample whether or not the device
    said anything -- a transition-based check on it cannot fail, which makes it
    worse than no check at all.

    ``device_time_age_ms`` is the honest signal: it climbs between statuses and
    DROPS when a fresh one lands. The gap between drops is the cadence the
    device is actually reporting at.

    None when no drop was observed at all -- meaning no status arrived during
    the whole window, so nothing here is bounded and the caller must refuse.
    """
    drops: list[float] = []
    prev_age = None
    prev_t = None
    for r in rows:
        age = r.get("device_time_age_ms")
        t = r.get("t")
        if age is None or t is None:
            continue
        if prev_age is not None and age < prev_age:
            if prev_t is not None:
                drops.append(t - prev_t)
            prev_t = t
        if prev_t is None:
            prev_t = t
        prev_age = age
    if not drops:
        return None
    drops.sort()
    mid = len(drops) // 2
    if len(drops) % 2:
        return float(drops[mid])
    return (drops[mid - 1] + drops[mid]) / 2.0


def skip_to_audible_ms(rows: list[dict], dispatch_t_ms: float,
                       requested_track_id) -> float:
    """Milliseconds from the accepted skip to the device hearing the request.

    ``rows`` is the flow-diagnostics timeline: each row carries ``t`` (ms),
    ``heard_track_id``, ``playing``, ``device_time_known`` and
    ``device_stream_ms``. The first row at or after dispatch where the device
    reports it is playing, has a real device clock, and its mapped position
    names the requested track, is the moment the listener hears the skip.

    Returns ``UNREACHED`` rather than a plausible number when that never
    happens — the caller records the sentinel instead of averaging a guess in.
    """
    if requested_track_id is None:
        raise ProbeRefusal("no requested track id — cannot tell which track "
                           "the device reaching counts as audible")
    for row in rows:
        if row.get("t") is None or row["t"] < dispatch_t_ms:
            continue
        if not row.get("playing"):
            continue
        if not row.get("device_time_known"):
            continue
        if not (row.get("device_stream_ms") or 0) > 0:
            continue
        if row.get("heard_track_id") == requested_track_id:
            return float(row["t"] - dispatch_t_ms)
    return float(UNREACHED)


def lead_at_dispatch_ms(snapshot: dict | None) -> int:
    """Total audio downstream of the splice at the moment of the skip.

    The encode clock minus the DEVICE-reported position. This is the whole
    server+device lead wherever it physically sits, which is what gate
    condition 3 needs — ``buffered_unsent_bytes`` alone sees only the Python
    out-buffer deque and misses the encoder's stdin pipe, ffmpeg's internal
    buffering and its stdout pipe.

    ``NO_DEVICE_TIME`` when the device has not reported: with no device clock
    ``held_offset_from_device_time`` falls back to "encode clock minus the
    run-ahead", which would make this figure a restatement of
    ``FLOW_RUN_AHEAD_S`` rather than a measurement of anything.
    """
    if not snapshot:
        raise ProbeRefusal("no flow session at dispatch — a per-track dispatch "
                           "is a different defect with its own signature and "
                           "must not be averaged into this one")
    if not snapshot.get("device_time_known"):
        return NO_DEVICE_TIME
    age = snapshot.get("device_time_age_ms")
    if age is not None and age > MAX_DEVICE_AGE_MS:
        # The backend extrapolates device position from the last status, which
        # is correct only while playback is uninterrupted. Across a gap this
        # long a pause or re-buffer is plausible, and extrapolation would
        # invent position rather than measure it.
        raise ProbeRefusal(
            f"device clock is {age} ms stale (limit {MAX_DEVICE_AGE_MS}) — "
            "extrapolating across a gap that long assumes uninterrupted "
            "playback, which is exactly what cannot be assumed here")
    lead = int(snapshot.get("total_lead_ms") or 0)
    if lead == 0:
        # held_offset_from_device_time clamps to [0, fed_ms], so an
        # extrapolation that overshot the encode clock lands here silently as a
        # zero lead. A live flow session cannot genuinely have the device level
        # with the encoder, so treat it as proof the extrapolation overshot.
        raise ProbeRefusal(
            "lead computed as 0 on a live session — the extrapolated device "
            "position reached the encode clock, so it overshot and was clamped")
    return lead


def split_at_dispatch(snapshot: dict | None) -> dict:
    """Which side is actually holding the audio downstream of the splice.

    ``total_lead_ms`` is the SUM of both shares, and reading the sum as either
    one gets the attribution exactly backwards -- which it did. Measured on the
    rig 2026-10-09: cutting ``FLOW_RUN_AHEAD_S`` from 10.0s to 2.0s moved
    skip-to-audible by only ~0.8s, because the encoder is paced by the device
    draining the stream (the out-buffer read 0 bytes at every dispatch) rather
    than by ``_pace``. So the receiver's own buffer is nearly all of it.

    ``server_lead_ms`` is ours and is bounded by the run-ahead.
    ``device_lag_ms`` is the receiver's and is bounded by nothing here -- it is
    the only part a re-LOAD can reach.
    """
    if not snapshot:
        return {"server_lead_ms": None, "device_lag_ms": None}
    return {
        "server_lead_ms": snapshot.get("server_lead_ms"),
        "device_lag_ms": snapshot.get("device_lag_ms"),
        "session_elapsed_ms": snapshot.get("session_elapsed_ms"),
    }


def out_buffer_note(snapshot: dict | None) -> dict:
    """The out-buffer figure, reported unconverted and labelled.

    Two independent reasons not to hand back milliseconds here: the deque holds
    ENCODED bytes while ``out_buffer_cap_bytes`` is computed at PCM rate, so the
    obvious division is wrong by the FLAC compression ratio; and the deque is
    only one of four places server-side audio sits. So this is a lower bound on
    a share, never the share itself.
    """
    if not snapshot:
        return {"out_buffer_bytes": None, "basis": "no flow session"}
    fmt = snapshot.get("encode_format")
    return {
        "out_buffer_bytes": snapshot.get("out_buffer_bytes_lower_bound"),
        "out_buffer_cap_bytes": snapshot.get("out_buffer_cap_bytes"),
        "encode_format": fmt,
        # Only a raw-PCM-behind-a-WAV-header session makes bytes->ms honest.
        "ms_conversion_valid": fmt == "wav",
        "basis": "lower bound on the server-held share only; encoded bytes "
                 "against a PCM-rate cap, and three further buffers "
                 "(encoder stdin, ffmpeg internal, encoder stdout) are not "
                 "visible to it",
    }


def modality(values: list[float]) -> dict:
    """Is this one population or two?

    Not a headline mean. The origin's "3-7s" is a perception range, and the
    plan's gate halts on a bimodal result because a cause-split aimed at the
    wrong mode is wasted work. Cheap two-cluster test: sort, find the largest
    internal gap, and call it bimodal when that gap dominates the spread. A
    full dip test would be more rigorous and is not warranted at these sample
    sizes — the point is to notice two clusters, not to publish a statistic.
    """
    vals = sorted(v for v in values if v >= 0)
    if len(vals) < 4:
        return {"n": len(vals), "bimodal": None,
                "why": "fewer than 4 usable samples"}
    spread = vals[-1] - vals[0]
    med = statistics.median(vals)
    gaps = [(vals[i + 1] - vals[i], i) for i in range(len(vals) - 1)]
    gap, idx = max(gaps)
    # A relative-only rule has no floor, so on a TIGHT distribution any slightly
    # uneven spacing trips it: 11718/11796/11882/11890/12016 was flagged bimodal
    # off a 126 ms gap inside a 298 ms spread, which is five samples of one
    # population. The gap must also be large against the MEDIAN before it can
    # mean two populations -- that keeps the rule scale-aware instead of
    # scale-blind, and it still catches the real outliers this run produced
    # (a 2,156 ms gap at ~11.9 s, and a 16,343 ms one).
    bimodal = bool(spread > 0
                   and gap > 0.4 * spread
                   and gap > 0.1 * med
                   and 0 < idx + 1 < len(vals))
    out = {
        "n": len(vals),
        "median_ms": statistics.median(vals),
        "min_ms": vals[0],
        "max_ms": vals[-1],
        "largest_gap_ms": gap,
        "spread_ms": spread,
        "bimodal": bimodal,
    }
    if bimodal:
        out["clusters"] = [vals[:idx + 1], vals[idx + 1:]]
    return out


def gate(skip_samples: list[float], server_leads: list[int],
         reload_ms: float | None) -> dict:
    """The three stop conditions from the plan. Any one halts before U3.

    Returns a decision, never a recommendation to proceed by default — an
    unmeasured input is a halt, not a pass, because a gate that cannot fail is
    not a gate.
    """
    usable = [v for v in skip_samples if v >= 0]
    if len(usable) < 4:
        return {"proceed": False, "reason": "insufficient_samples",
                "detail": f"{len(usable)} usable samples; need at least 4 "
                          "before any condition can be evaluated"}

    shape = modality(usable)
    baseline = shape["median_ms"]

    if shape.get("bimodal"):
        return {"proceed": False, "reason": "bimodal_distribution",
                "detail": "two clusters — the cause-split may be aiming at "
                          "the wrong mode",
                "shape": shape}

    # Evaluate the split FIRST and always report it. It was gated behind the
    # re-LOAD measurement, so an unmeasured re-LOAD hid the one answer the
    # condition exists to surface: on the rig 2026-10-09 the split came back
    # 99% server-held while the gate reported only "reload_cost_unmeasured".
    # A condition that cannot be reported until an unrelated input lands is a
    # condition that cannot do its job.
    # ``server_leads`` must be the SERVER share (encode clock minus session
    # elapsed), never total_lead_ms. The total is approximately the whole
    # skip-to-audible by construction -- they agreed within 1-3% on the rig --
    # so a ratio against the baseline would sit near 1.0 on every run and this
    # condition would always fire. A condition that always fires is as useless
    # as one that never does.
    real_leads = [v for v in server_leads if v >= 0]
    split = None
    if real_leads:
        lead_median = statistics.median(real_leads)
        split = {"server_lead_median_ms": lead_median,
                 "server_held_fraction": round(lead_median / baseline, 3)
                 if baseline else None}

    if reload_ms is None:
        return {"proceed": False, "reason": "reload_cost_unmeasured",
                "detail": "the mid-stream re-LOAD was not measured; the "
                          "499 ms per-track boundary figure is a different "
                          "operation and must not be substituted",
                "shape": shape, "split": split}

    if reload_ms > 0.5 * baseline:
        return {"proceed": False, "reason": "reload_cost_approaches_baseline",
                "detail": f"re-LOAD {reload_ms:.0f} ms against a "
                          f"{baseline:.0f} ms baseline",
                "shape": shape, "reload_ms": reload_ms}

    # Condition 3: who is actually holding the audio. Keyed on the SERVER
    # share only -- see the note above on why the total cannot serve here.
    if not real_leads:
        return {"proceed": False, "reason": "lead_unmeasured",
                "detail": "no dispatch reported a server lead, so the "
                          "server/device split is unknown",
                "shape": shape}
    lead_median = statistics.median(real_leads)
    if lead_median > 0.5 * baseline:
        return {"proceed": False, "reason": "server_held_dominant",
                "detail": f"median SERVER lead {lead_median:.0f} ms of a "
                          f"{baseline:.0f} ms baseline is ours to reclaim "
                          "— re-weigh lowering FLOW_RUN_AHEAD_S as the "
                          "PRIMARY fix before building the cause-split",
                "shape": shape, "split": split}

    return {"proceed": True, "reason": "all_conditions_clear",
            "shape": shape, "reload_ms": reload_ms,
            "lead_median_ms": lead_median, "split": split}


# ── injected I/O ─────────────────────────────────────────────────────────────


def flow_row(deps: Deps, clock=time.monotonic) -> dict:
    """One timeline sample: device-heard track plus the lead, on one clock.

    ``clock`` is injected for the same reason the flow engine injects its own:
    the achieved-interval refusal is a behaviour worth testing, and it cannot be
    exercised against a real monotonic clock.
    """
    row: dict = {"t": clock() * 1000.0}
    diag = deps._req("/admin/diagnostics/flow")
    snap = (diag or {}).get("flow") or {}
    np = deps.now_playing() or {}
    row.update({
        "gapless_flow_active": (diag or {}).get("gapless_flow_active"),
        "session": snap.get("session"),
        "heard_track_id": snap.get("heard_track_id"),
        "device_time_known": snap.get("device_time_known"),
        "device_stream_ms": snap.get("device_stream_ms"),
        # The honest staleness signal. device_stream_ms is extrapolated, so it
        # advances every sample whether or not the device reported; this is the
        # only field that reveals whether anything actually refreshed the clock.
        "device_time_age_ms": snap.get("device_time_age_ms"),
        "encode_clock_ms": snap.get("encode_clock_ms"),
        "total_lead_ms": snap.get("total_lead_ms"),
        # Now Playing is recorded but NEVER used as the trigger — it follows the
        # boundary clock. It is here so a reader can see the disagreement that
        # is the defect. Flat `track_id`, not a nested track object.
        "np_track_id": np.get("track_id"),
        # is_playing and is_paused are BOTH true on a paused instance (verified
        # against the rig: is_playing=True, is_paused=True, position frozen).
        # Treating is_playing alone as "audible" would let a paused device
        # satisfy the trigger, which is the whole thing this probe must not do.
        "playing": bool(np.get("is_playing")) and not bool(np.get("is_paused")),
    })
    return row


def watch_for_audible(deps: Deps, dispatch_t_ms: float, requested_track_id,
                      timeout_s: float = WATCH_TIMEOUT_S,
                      interval_s: float = POLL_INTERVAL_S,
                      sleep=time.sleep,
                      clock=time.monotonic) -> tuple[float, list[dict]]:
    """Poll until the device reports hearing the requested track, or give up."""
    rows: list[dict] = []
    deadline = clock() + timeout_s
    while clock() < deadline:
        rows.append(flow_row(deps, clock=clock))
        if skip_to_audible_ms(rows, dispatch_t_ms, requested_track_id) >= 0:
            break
        sleep(interval_s)
    achieved = achieved_interval_ms(rows)
    if achieved is None:
        raise ProbeRefusal("fewer than 3 samples — the timeline cannot bound "
                           "its own resolution")
    if achieved > MAX_ACHIEVED_INTERVAL_MS:
        raise ProbeRefusal(
            f"achieved sample interval {achieved:.0f} ms exceeds "
            f"{MAX_ACHIEVED_INTERVAL_MS:.0f} ms — too coarse to support a "
            "skip-to-audible claim (the REQUESTED interval is not evidence)")
    dev = achieved_device_interval_ms(rows)
    if dev is None:
        raise ProbeRefusal(
            "fewer than two device-clock updates observed — the device cadence "
            "cannot be bounded, and our own poll rate says nothing about it")
    if dev > MAX_DEVICE_INTERVAL_MS:
        raise ProbeRefusal(
            f"device clock advanced only every {dev:.0f} ms (limit "
            f"{MAX_DEVICE_INTERVAL_MS:.0f} ms) — skip-to-audible would be "
            "quantised to that step regardless of how fast we poll. Raise the "
            "cadence with JP_FLOW_STATUS_POLL_S for the measurement run")
    return achieved, rows, dev


def one_skip(deps: Deps, sleep=time.sleep, clock=time.monotonic) -> dict:
    """Measure a single skip end to end, refusing rather than guessing."""
    pre = deps._req("/admin/diagnostics/flow")
    if not (pre or {}).get("gapless_flow_active"):
        raise ProbeRefusal("gapless_flow_active is false — Cast degraded to "
                           "per-track dispatch, which has its own ~499 ms "
                           "signature and is not this defect")
    snap = (pre or {}).get("flow")
    lead = lead_at_dispatch_ms(snap)

    nxt = (deps._req("/api/queue") or {})
    items = nxt.get("queue") or []
    # Queue rows come from guest._track_dict, which emits `track_id` -- NOT
    # `id`, and not a nested track object. Verified against the rig: reading
    # `id` here silently yielded None on a fully populated queue, which the
    # probe then reported as "queue front is empty" on every single skip.
    requested = (items[0].get("track_id") or items[0].get("id")) if items else None
    if requested is None:
        raise ProbeRefusal("queue front is empty — a skip under a random "
                           "end-behaviour lands on an autofill pick the probe "
                           "cannot name in advance")

    t0 = clock() * 1000.0
    deps._req("/admin/playback/skip", {})
    achieved, rows, dev_interval = watch_for_audible(
        deps, t0, requested, sleep=sleep, clock=clock)

    return {
        "skip_to_audible_ms": skip_to_audible_ms(rows, t0, requested),
        "lead_at_dispatch_ms": lead,
        "split": split_at_dispatch(snap),
        "out_buffer": out_buffer_note(snap),
        "achieved_interval_ms": achieved,
        # The bound that actually limits the figure above it.
        "achieved_device_interval_ms": dev_interval,
        "requested_track_id": requested,
        "session_at_dispatch": (snap or {}).get("session"),
        "samples": len(rows),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--skips", type=int, default=12,
                    help="how many skips to measure (distribution, not a "
                         "headline — vary position/bitrate/queue depth across "
                         "runs)")
    ap.add_argument("--settle", type=float, default=8.0,
                    help="seconds of playback before each skip")
    ap.add_argument("--out", default=None, help="append JSONL results here")
    ap.add_argument("--reload-ms", type=float, default=None,
                    help="measured MID-STREAM re-LOAD cost, from the throwaway "
                         "probe. Not the 499 ms per-track boundary figure — "
                         "that is a different operation")
    args = ap.parse_args(argv)

    deps = Deps(base=os.environ.get("JP_BASE", ""))
    results: list[dict] = []
    try:
        with RigState(deps):
            deps.set_setting("gapless_enabled", True)
            for i in range(args.skips):
                time.sleep(args.settle)
                try:
                    r = one_skip(deps)
                except ProbeRefusal as exc:
                    r = {"refused": deps.redact(str(exc))}
                r["i"] = i
                results.append(r)
                print(json.dumps(r))
    except ProbeRefusal as exc:
        print(json.dumps({"refused": deps.redact(str(exc))}))
        return 3

    decision = gate([r.get("skip_to_audible_ms", UNREACHED) for r in results],
                    [(r.get("split") or {}).get("server_lead_ms")
                     if (r.get("split") or {}).get("server_lead_ms") is not None
                     else NO_DEVICE_TIME
                     for r in results],
                    args.reload_ms)
    report = {"results": results, "gate": decision}
    if args.out:
        with open(args.out, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(report) + "\n")
    print(json.dumps({"gate": decision}, indent=2))
    return 0 if decision.get("proceed") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
