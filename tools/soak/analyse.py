"""Turn the soak's four JSONL streams into a verdict."""
import io
import json
import os
from collections import Counter


def load(path):
    rows = []
    try:
        for line in io.open(path, encoding="utf-8"):
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    return rows


party = load(os.environ.get("JP_LOG", "party-actions.jsonl"))
mon = load(os.environ.get("JP_MON", "monitor.jsonl"))
adm = load(os.environ.get("JP_ADMIN_LOG", "admin-actions.jsonl"))
# The settings-churn driver wrote this every run and nothing ever read it.
tog = load(os.environ.get("JP_TOGGLE_LOG", "toggle-actions.jsonl"))

print("=" * 66)
print("GUEST ACTIVITY")
print("=" * 66)
acts = Counter(r.get("act") for r in party)
for k, v in sorted(acts.items(), key=lambda x: -x[1]):
    print(f"  {v:5d}  {k}")
errs = [r for r in party if r.get("act") in ("ERROR", "FATAL")]
print(f"\n  guest errors: {len(errs)}")
for e in errs[:12]:
    print(f"    - {e.get('behaviour')}: {str(e.get('err'))[:110]}")

adds = [r for r in party if r.get("act") == "searchAndAdd" and r.get("added")]
drills = [r for r in party if r.get("act") == "drillArtistAlbum"
          and (r.get("c") or {}).get("added")]
albums = [r for r in party if r.get("act") == "queueWholeAlbum" and r.get("queued")]
print(f"\n  tracks added via search : {len(adds)}")
print(f"  tracks added via drill  : {len(drills)}")
print(f"  whole albums queued     : {len(albums)}")

# triple-tap duplicate evidence
tt = [r for r in party if r.get("act") == "tripleTapAdd" and r.get("taps") == 3
      and r.get("qBefore") is not None and r.get("qAfter") is not None]
dupes = [r for r in tt if (r["qAfter"] - r["qBefore"]) > 1]
print(f"\n  triple-taps observed    : {len(tt)}")
print(f"  of those, queue grew >1 : {len(dupes)}  (duplicate adds)")

print()
print("=" * 66)
print("ADMIN TRANSPORT CHAOS")
print("=" * 66)
aacts = Counter(r.get("act") for r in adm)
for k, v in sorted(aacts.items(), key=lambda x: -x[1]):
    print(f"  {v:5d}  {k}")
bad = [r for r in adm if r.get("errors")]
codes = Counter()
for r in adm:
    for c in (r.get("codes") or []):
        codes[c] += 1
print(f"\n  HTTP codes: {dict(codes)}")
print(f"  calls with error bodies: {len(bad)}")
for b in bad[:10]:
    print(f"    - {b.get('act')}: {str(b.get('errors'))[:150]}")

print()
print("=" * 66)
print("ADMIN SETTINGS CHURN")
print("=" * 66)
if not tog:
    print("  (no toggle log - admin_toggles.py did not run)")
else:
    tacts = Counter(r.get("act") for r in tog)
    for k, v in sorted(tacts.items(), key=lambda x: -x[1]):
        print(f"  {v:5d}  {k}")
    bad_t = [r for r in tog if r.get("code") not in (200, 204, None)]
    print(f"\n  flips rejected: {len(bad_t)}")
    for b in bad_t[:8]:
        print(f"    - {b.get('act')} -> {b.get('code')}: {str(b.get('body'))[:110]}")
    stopped = [r for r in tog if r.get("PLAYBACK_STOPPED")]
    print(f"  flips that STOPPED playback: {len(stopped)}"
          f"{'  <-- investigate' if stopped else ''}")
    for sflip in stopped[:8]:
        print(f"    - {sflip.get('act')}")

print()
print("=" * 66)
print("PLAYBACK INTEGRITY")
print("=" * 66)
polls = [r for r in mon if r.get("kind") == "poll"]
end = [r for r in mon if r.get("kind") == "end"]
start = [r for r in mon if r.get("kind") == "start"]
if start:
    print(f"  version under test: {start[0].get('version')}")
print(f"  polls: {len(polls)}")

trans = [r for r in polls if r.get("transition")]
print(f"  track transitions observed: {len(trans)}")

# advance-rate check: how long each track was actually on air
if len(trans) > 1:
    gaps = []
    for a, b in zip(trans, trans[1:]):
        gaps.append(b["t"] - a["t"])
    gaps.sort()
    print(f"  seconds between transitions: min {gaps[0]:.1f} "
          f"median {gaps[len(gaps)//2]:.1f} max {gaps[-1]:.1f}")
    # A transition faster than ~2s with no admin skip near it is suspicious.
    fast = [g for g in gaps if g < 2.0]
    print(f"  transitions <2s apart: {len(fast)}"
          f"{'  <-- possible fast-drain' if len(fast) > 3 else ''}")

# position monotonicity within a track
viol = 0
prev = None
for r in polls:
    cur = (r.get("track_id"), r.get("position_ms"))
    if prev and prev[0] == cur[0] and cur[1] is not None and prev[1] is not None:
        if cur[1] + 1500 < prev[1]:      # tolerate a seek/jitter
            viol += 1
    prev = cur
print(f"  backwards position jumps (same track): {viol} "
      f"(seeks are expected — admin chaos seeks deliberately)")

overrun = [r for r in polls if r.get("position_ms") and r.get("duration_ms")
           and r["position_ms"] > r["duration_ms"] + 2000]
print(f"  position beyond duration: {len(overrun)}")

# outage / session states seen
states = Counter()
reasons = Counter()
for r in polls:
    s = r.get("session") or {}
    if s.get("state"):
        states[s["state"]] += 1
    a = r.get("admin_session") or {}
    if a.get("reason"):
        reasons[a["reason"]] += 1
print(f"  session states: {dict(states)}")
if reasons:
    print(f"  outage reasons: {dict(reasons)}   <-- investigate")

print()
print("=" * 66)
print("PLAY RECORDING (exactly-once)")
print("=" * 66)
if end:
    e = end[0]
    deltas = e.get("play_count_deltas", {})
    tot_trans = e.get("total_transitions", 0)
    distinct = e.get("distinct_transitions", 0)
    print(f"  transitions logged   : {tot_trans} ({distinct} distinct tracks)")
    print(f"  tracks with new plays: {len(deltas)}")
    over = {k: v for k, v in deltas.items() if v > 1}
    print(f"  counted MORE than once: {len(over)}")
    for k, v in list(over.items())[:8]:
        print(f"    {v}x  {k}")
    total_delta = sum(deltas.values())
    print(f"  total play increments: {total_delta}")
    if tot_trans:
        print(f"  ratio increments/transitions: {total_delta/tot_trans:.2f} "
              f"(1.00 is ideal; <1 means plays were missed)")
else:
    print("  (monitor did not finish — no end record)")

print()
print("=" * 66)
print("RESOURCE TREND (leak check)")
print("=" * 66)
rss_grew = False
cs = [(r["t"], r["container"]) for r in polls
      if isinstance(r.get("container"), dict) and "rss_kb" in r["container"]]
if len(cs) >= 2:
    first, last = cs[0][1], cs[-1][1]
    span = (cs[-1][0] - cs[0][0]) / 60
    print(f"  window: {span:.1f} min, {len(cs)} samples")
    for k in ("rss_kb", "threads", "fds", "children", "media_procs"):
        a, b = first.get(k), last.get(k)
        vals = [c[1].get(k) for c in cs if c[1].get(k) is not None]
        if a is None or b is None:
            continue
        peak = max(vals) if vals else b
        d = b - a
        flag = ""
        if k == "rss_kb" and d > 60000:
            flag = "  <-- RSS grew >60MB"
            rss_grew = True
        if k in ("fds", "threads", "children", "media_procs") and d > 8:
            flag = f"  <-- {k} grew by {d}"
        print(f"  {k:12s} start {a:>8}  end {b:>8}  peak {peak:>8}  delta {d:+}{flag}")
else:
    errs = [r["container"] for r in polls if isinstance(r.get("container"), dict)
            and "_proc_err" in r["container"]]
    print(f"  no container samples ({len(errs)} errors)")
    if errs:
        print(f"    e.g. {errs[0]['_proc_err'][:150]}")

if rss_grew:
    print()
    print("  This section shows the SHAPE of the growth, not its cause. To name")
    print("  the allocation sites, bracket the next run with the admin probe:")
    print("    POST /admin/diagnostics/memory/start")
    print("    POST /admin/diagnostics/memory/snapshot   {\"label\": \"base\"}")
    print("    ... run the soak ...")
    print("    POST /admin/diagnostics/memory/snapshot   {\"label\": \"after\"}")
    print("    GET  /admin/diagnostics/memory/diff?before=base&after=after")
    print("  Then POST /admin/diagnostics/memory/stop - tracing is not free.")
