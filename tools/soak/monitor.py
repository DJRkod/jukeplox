"""Soak observer: polls the running jukeplox and samples the container.

Writes one JSON object per sample to a JSONL so the run can be analysed after
the fact — and so a failure can be traced to WHEN it started rather than merely
observed at the end.

Everything it calls is a pure read. It deliberately never touches the admin
endpoints that mutate play counts, the queue, or playback, because those are the
very things it is trying to measure.
"""
import json
import os
import subprocess
import time

from jp_http import Client, minutes, require_base

BASE = require_base()
MINUTES = minutes()
OUT = os.environ.get("JP_MON", "monitor.jsonl")
ADMIN_PW = os.environ.get("JP_ADMIN_PW", "")
RIG = os.environ.get("JP_RIG", "")          # set to enable container sampling

_c = Client(BASE)
get = _c.get_json


def login():
    if not ADMIN_PW:
        return False
    return _c.login(ADMIN_PW)[0] == 200


def _scrub(text: str) -> str:
    """Remove anything secret-shaped before it can reach the log.

    Truncation is NOT redaction. `subprocess.TimeoutExpired.__str__` is
    "Command '<full argv>' timed out...", and the argv below carries the rig
    password and host-key fingerprint — so a generic `str(e)[:120]` wrote part
    of the password straight into monitor.jsonl, which analyse.py reads and
    operators attach to reports.
    """
    for var in ("JP_PW", "JP_HOSTKEY", "JP_ADMIN_PW", "JP_SSH"):
        secret = os.environ.get(var)
        if secret:
            text = text.replace(secret, f"<{var}>")
    return text


def container_sample():
    """RSS, threads, FDs and subprocess counts from /proc — the image has no ps."""
    if not RIG:
        return {}
    script = os.environ.get("JP_PROBE", "sh /root/jp-probe.sh")
    # The password is on argv, so it is visible in the local process table for
    # the life of the call. That is a known limitation of plink's -pw; prefer
    # key auth (-i) on any machine where the process table is not trusted.
    cmd = ["plink", "-batch", "-hostkey", os.environ["JP_HOSTKEY"],
           "-pw", os.environ["JP_PW"], os.environ["JP_SSH"], script]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    except subprocess.TimeoutExpired:
        # Fixed string: this exception stringifies the whole argv.
        return {"_proc_err": "probe timed out after 45s"}
    except FileNotFoundError:
        return {"_proc_err": "plink not found on PATH"}
    except Exception as e:
        return {"_proc_err": _scrub(f"{type(e).__name__}: {e}")[:120]}
    parts = r.stdout.strip().split()
    if len(parts) >= 5:
        return {"rss_kb": int(parts[0]), "threads": int(parts[1]),
                "fds": int(parts[2]), "children": int(parts[3]),
                "media_procs": int(parts[4])}
    return {"_proc_err": _scrub(r.stdout.strip() or r.stderr.strip())[:120]}


def main():
    end = time.time() + MINUTES * 60
    have_admin = login()
    out = open(OUT, "a", buffering=1)

    def emit(o):
        out.write(json.dumps({"t": time.time(), **o}) + "\n")

    baseline = get("/api/play-counts?type=track")
    base_counts = {}
    if isinstance(baseline, list):
        base_counts = {x.get("entity_id"): x.get("count", 0) for x in baseline}
    emit({"kind": "start", "admin": have_admin,
          "version": get("/api/version"),
          "baseline_tracks_with_plays": len(base_counts)})

    # Seed from the CURRENT track: without this the first poll counts as a
    # transition, so a flawless run reports increments/transitions = N/(N+1)
    # and analyse.py reads it as plays having been missed.
    last_track = (get("/api/now-playing") or {}).get("track_id")
    transitions = []
    n = 0
    while time.time() < end:
        n += 1
        pos = get("/api/playback/position")
        npw = get("/api/now-playing")
        q = get("/api/queue")
        sample = {
            "kind": "poll",
            "position_ms": pos.get("position_ms"),
            "duration_ms": pos.get("duration_ms"),
            "is_playing": pos.get("is_playing"),
            "is_paused": pos.get("is_paused"),
            "track_id": npw.get("track_id"),
            "title": npw.get("title"),
            "qlen": len(q.get("queue", [])) if isinstance(q.get("queue"), list) else None,
            "hlen": len(q.get("history", [])) if isinstance(q.get("history"), list) else None,
            "session": npw.get("output_session", {}),
        }
        tid = npw.get("track_id")
        if tid and tid != last_track:
            transitions.append({"t": time.time(), "track_id": tid,
                                "title": npw.get("title"),
                                "duration_ms": pos.get("duration_ms")})
            sample["transition"] = True
            last_track = tid
        if have_admin and n % 3 == 0:
            adm = get("/admin/playback/now-playing")
            if isinstance(adm.get("output_session"), dict):
                sample["admin_session"] = adm["output_session"]
        if n % 6 == 0:
            sample["container"] = container_sample()
        emit(sample)
        time.sleep(10)

    final = get("/api/play-counts?type=track")
    final_counts = {}
    if isinstance(final, list):
        final_counts = {x.get("entity_id"): x.get("count", 0) for x in final}
    deltas = {k: v - base_counts.get(k, 0) for k, v in final_counts.items()
              if v - base_counts.get(k, 0) != 0}
    emit({"kind": "end", "transitions": transitions,
          "play_count_deltas": deltas,
          "distinct_transitions": len({x["track_id"] for x in transitions}),
          "total_transitions": len(transitions)})
    out.close()


if __name__ == "__main__":
    main()
