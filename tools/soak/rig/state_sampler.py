"""Rig-local playback-state timeline, sampled on the same clock as the audio.

Runs ON the rig, alongside `record.sh`. Everything comes from the environment —
no host, no credential is committed here.

**Why this runs on the rig rather than reusing the observer.** `monitor.py`
samples from the driving machine, and its timestamps are that machine's. Joining
an audio timeline recorded on the rig against a state timeline recorded
elsewhere means joining across two unsynchronised clocks, and the resulting skew
would corrupt every gap measurement silently — a boundary gap is tens of
milliseconds, and clock drift between two boxes is not. The observer keeps its
job (60-second container trends, where skew is irrelevant); this keeps the one
job that needs a shared clock.

Every sample also carries the container's `/dev/snd` descriptor count, because
that is the signal that something is actually holding the sound card. It is
deliberately not `media_procs`: the Direct backend's GStreamer pipeline runs
in-process and never appears as an ffmpeg/gst subprocess, so the probe's media
count sits at 0 while audio plays perfectly. Recording both on the audio clock
is what lets the classifier separate "the app believes it is playing but nothing
holds the card" from "the pipeline is live but silent" — two very different
faults that look identical from dB alone.
"""
import io
import json
import os
import subprocess
import sys
import time
import urllib.request

BASE = os.environ.get("JP_BASE")
if not BASE:
    # No default, deliberately: a soak points at a real deployment and the
    # target is stated every time (see tools/soak/README.md).
    sys.stderr.write("JP_BASE is required, e.g. JP_BASE=http://jukebox.local\n")
    raise SystemExit(2)

OUT = os.environ.get("JP_STATE_LOG", "state.jsonl")
INTERVAL = float(os.environ.get("JP_STATE_INTERVAL", "1.0"))
MINUTES = float(os.environ.get("JP_MINUTES", "30"))
CONTAINER = os.environ.get("JP_CONTAINER", "jukeplox")


def _get(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def now_playing() -> dict:
    return _get("/api/now-playing")


def position() -> dict:
    """Device-side playback position.

    A separate endpoint from now-playing, which carries no position field at all.
    This one asks the output router for the backend's own position.

    Recorded so the classifier can detect a position RESET, which is one of the
    two signals that marks a track boundary (the other being a track_id change).
    It is deliberately NOT a liveness signal: an earlier version of this comment
    claimed that a position which ADVANCES between samples proves the device is
    really playing, and that was retracted. Position advances through quiet music
    exactly as it does through loud music, and at 1 Hz the observed "advance" is
    just the sampling interval read back as evidence. Liveness comes from the
    /dev/snd descriptor count below; whether a silence is real comes from the
    level measured inside it. See
    docs/solutions/best-practices/acoustic-gap-vs-fadeout-quiet-audio-detection.md
    """
    return _get("/api/playback/position")


def snd_fds() -> int:
    """How many /dev/snd descriptors the container holds.

    The liveness signal for a Direct-backend arm. Returns -1 when the probe
    itself failed, which must stay distinguishable from a genuine 0 — a probe
    that silently reports zero looks exactly like an idle card.
    """
    try:
        cid = subprocess.run(["docker", "ps", "-qf", f"name={CONTAINER}"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        if not cid:
            return -1
        out = subprocess.run(
            ["docker", "exec", cid, "sh", "-c",
             "ls -l /proc/*/fd 2>/dev/null | grep -c snd"],
            capture_output=True, text=True, timeout=10).stdout.strip()
        return int(out or -1)
    except Exception:
        return -1


def main() -> int:
    end_at = time.time() + MINUTES * 60
    # Line-buffered and flushed per sample: a killed sampler must not lose the
    # tail, which is exactly the part an incident investigation wants.
    with io.open(OUT, "a", encoding="utf-8") as fh:
        while time.time() < end_at:
            t0 = time.time()
            rec = {"t": int(t0 * 1000)}
            try:
                d = now_playing()
                sess = d.get("output_session") or {}
                rec.update({
                    "track_id": d.get("track_id"),
                    "title": d.get("title"),
                    "is_playing": d.get("is_playing"),
                    "is_paused": d.get("is_paused"),
                    "session_state": sess.get("state"),
                    "gapless_flow_active": sess.get("gapless_flow_active"),
                    "held": sess.get("held"),
                })
            except Exception as exc:
                rec["err"] = type(exc).__name__
            try:
                p = position()
                # Recorded separately from is_playing on purpose: the app can
                # report playing while the device has no session at all (#57).
                # Used for boundary detection via a position RESET, not as a
                # liveness discriminator — see position() above for why the
                # "it advanced, so it is playing" reading was retracted.
                rec["position_ms"] = p.get("position_ms")
                rec["duration_ms"] = p.get("duration_ms")
            except Exception as exc:
                rec["pos_err"] = type(exc).__name__
            rec["snd_fds"] = snd_fds()
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            slept = time.time() - t0
            if slept < INTERVAL:
                time.sleep(INTERVAL - slept)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
