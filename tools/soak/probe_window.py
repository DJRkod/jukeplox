"""Scheduled load peaks, and the latency contract a probe reports against.

Runs on the driving machine. Everything comes from the environment — no host, no
credential is committed here.

A peak is a bounded window in which guest concurrency and request rate are
raised above the arm's baseline. They are SCHEDULED rather than left to chance so
that a probe's findings, the guest action log and the resource trend can all be
read against the same timestamps — "it felt slow" is worth little if nobody can
say what the system was doing at that moment.

The probe itself is an agent: it exercises the surfaces below, reads the measured
latencies, and judges whether the experience is degraded. That judgement is the
part a scripted guest cannot supply. Everything here exists to make the
judgement attributable rather than anecdotal.
"""
import argparse
import io
import json
import os
import sys
import time
import urllib.request

# Surfaces a guest actually waits on. Deliberately the read paths a phone hits,
# not admin endpoints — the question is what a party guest experiences.
DEFAULT_SURFACES = [
    ("browse_artists", "/api/browse/artists"),
    ("browse_albums", "/api/browse/albums"),
    ("search", "/api/search?q=love"),
    ("now_playing", "/api/now-playing"),
    ("queue", "/api/queue"),
]


def peak_schedule(arm_minutes: float, n_peaks: int, peak_minutes: float,
                  lead_in_minutes: float = 5.0) -> list[dict]:
    """Evenly spaced peak windows that fit inside the arm.

    A peak scheduled near the end would be cut off by the arm boundary, and a
    truncated peak is not a peak — its probe would measure a system that is
    already winding down. Windows are placed so the last one closes before the
    arm does, and an arm too short to hold the request gets fewer peaks rather
    than overlapping ones.
    """
    if n_peaks <= 0 or peak_minutes <= 0 or arm_minutes <= 0:
        return []
    usable = arm_minutes - lead_in_minutes - peak_minutes
    if usable <= 0:
        # Room for at most one peak, and only if the arm is longer than the
        # lead-in plus the peak itself.
        if arm_minutes >= lead_in_minutes + peak_minutes:
            return [{"start_min": lead_in_minutes,
                     "end_min": lead_in_minutes + peak_minutes}]
        return []
    out = []
    for i in range(n_peaks):
        start = lead_in_minutes + (usable * i / max(1, n_peaks - 1)
                                   if n_peaks > 1 else usable / 2)
        end = start + peak_minutes
        if end > arm_minutes:
            break
        if out and start < out[-1]["end_min"]:
            continue  # never overlap a previous window
        out.append({"start_min": round(start, 3), "end_min": round(end, 3)})
    return out


def measure(base: str, surfaces=None, timeout: float = 30.0) -> list[dict]:
    """Time each surface once. Failures are recorded, never omitted.

    A surface that timed out is the most interesting result a probe can get, so
    it must appear in the output rather than silently shrinking the sample.
    """
    out = []
    for name, path in (surfaces or DEFAULT_SURFACES):
        t0 = time.time()
        rec = {"surface": name, "t": int(t0 * 1000)}
        try:
            req = urllib.request.Request(f"{base.rstrip('/')}{path}")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                n = len(r.read())
            rec["ms"] = round((time.time() - t0) * 1000, 1)
            rec["bytes"] = n
        except Exception as exc:
            rec["ms"] = round((time.time() - t0) * 1000, 1)
            rec["err"] = type(exc).__name__
        out.append(rec)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--minutes", type=float,
                    default=float(os.environ.get("JP_MINUTES", "30")))
    ap.add_argument("--peaks", type=int,
                    default=int(os.environ.get("JP_PEAKS", "2")))
    ap.add_argument("--peak-minutes", type=float,
                    default=float(os.environ.get("JP_PEAK_MINUTES", "4")))
    ap.add_argument("--print-schedule", action="store_true")
    ap.add_argument("--measure", action="store_true",
                    help="time the guest surfaces once and exit")
    args = ap.parse_args()

    base = os.environ.get("JP_BASE")
    out_path = os.environ.get("JP_PROBE_LOG", "probe.jsonl")

    schedule = peak_schedule(args.minutes, args.peaks, args.peak_minutes)
    if args.print_schedule:
        print(json.dumps(schedule, indent=1))
        return 0

    if not base:
        sys.stderr.write("JP_BASE is required, e.g. JP_BASE=http://jukebox.local\n")
        return 2

    if args.measure:
        rows = measure(base)
        for r in rows:
            print(json.dumps(r))
        return 0

    # Emit peak markers on schedule so the probe, the guest log and the trend
    # data all share timestamps.
    start = time.time()
    with io.open(out_path, "a", encoding="utf-8") as fh:
        for w in schedule:
            for label, minute in (("peak_open", w["start_min"]),
                                  ("peak_close", w["end_min"])):
                delay = start + minute * 60 - time.time()
                if delay > 0:
                    time.sleep(delay)
                fh.write(json.dumps({"t": int(time.time() * 1000),
                                     "event": label,
                                     "window": w}) + "\n")
                fh.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
