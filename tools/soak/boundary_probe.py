"""Track-boundary gap probe — measure the silence a listener actually hears at
a gapless transition, against what the source files themselves produce.

Why this exists separately from the 12-hour soak harness: the soak arm answers
"did anything break over hours", and its instrument is tuned for that. This one
answers "is THIS boundary clean, to within 100 ms", which is a different
measurement with three prerequisites the soak path does not meet:

- ``JP_SILENCE_MIN_S`` defaults to 0.4 in ``rig/features.py``, and that value is
  ffmpeg's silencedetect ``d=`` parameter — a *minimum duration*, not a
  threshold. At the default, a 300 ms gap is not "measured and passed", it is
  never emitted as an interval at all, so a gapping build and a fixed build both
  report zero silences. Every probe invocation must lower it
  (``SILENCE_MIN_S``) or the whole chain is blind at the magnitude under test.
- The boundary timestamp must come from a timeline sampled well below the gap of
  interest. ``rig/state_sampler.py`` shells out to ``docker`` twice per sample,
  which floors it near 1 Hz regardless of the requested interval, so this module
  carries its own HTTP-only poller and records the *achieved* interval rather
  than the requested one.
- ``classify()``'s quiet-audio filter is disabled when ``floor_db`` is None, and
  it is the thing that took a measured 17-in-18 false-positive rate to zero.
  ``verdict()`` refuses rather than guessing when the floor is unknown.

Layering: pure policy functions first, all I/O injected through ``Deps``, same
shape as ``tools/soak/arm.py``. The rig target and admin credential come from
the environment only — ``tests/test_soak_harness_hygiene.py`` scans this whole
tree for committed hosts and secrets.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

# Reuse the soak harness's silencedetect/volumedetect parsers rather than
# carrying a second copy. These encode subtle agreed behaviour — notably that
# a silence still open at end-of-file is closed at the known duration rather
# than dropped — and two copies of that would let the soak path and this probe
# disagree about the same audio. Same single-sourcing reason state.py gives for
# _stream_url_base. Path-loaded because tools/soak has no package __init__
# (the convention analyse.py already follows for `verdict`).
_SOAK_DIR = os.path.dirname(os.path.abspath(__file__))
if _SOAK_DIR not in sys.path:
    sys.path.insert(0, _SOAK_DIR)
from rig import features as _features  # noqa: E402

# ── constants ────────────────────────────────────────────────────────────────

#: silencedetect minimum duration for probe runs. The 0.4 default in
#: rig/features.py cannot see the gaps this probe exists to measure.
SILENCE_MIN_S = "0.02"

#: silencedetect trigger level. A trigger, never a verdict — the level INSIDE
#: each flagged interval is re-measured against the floor before it counts.
NOISE_DB = "-55"

#: A flagged interval whose own level sits more than this above the measured
#: noise floor is quiet audio, not silence (rig/classify.py uses the same
#: margin; keep them in step).
QUIET_AUDIO_MARGIN_DB = 8.0

#: Origin's fixed pass bar: the acoustic boundary must sit within this of the
#: source control. Deliberately NOT derived from the observed defect — a bar
#: chosen after the fact is fittable to whatever a candidate happens to achieve.
PASS_TOLERANCE_MS = 100

#: How far from the boundary a silence may start and still be attributed to it.
BOUNDARY_WINDOW_MS = 3000

#: Settings the probe mutates, split by the endpoint that actually owns them.
#: This split is load-bearing: GET /admin/settings does NOT return the output
#: routing keys (they live on GET /admin/output/active), so snapshotting all
#: five from one endpoint silently yields None for the output three — and a
#: restore that skips None would leave the rig pointed at whatever device the
#: probe last selected. That is the exact failure this class exists to prevent.
MANAGED_SETTINGS = ("gapless_enabled", "queue_end_behavior")
MANAGED_OUTPUT = ("backend_type", "device_id", "host")

class ProbeRefusal(Exception):
    """The probe declined to produce a verdict.

    Distinct from "measured zero gap" on purpose: a refusal means the
    instrument could not be trusted for this run (no floor, sampler too slow,
    wrong device session). Collapsing the two is how an insensitive instrument
    reports a clean pass.
    """


# ── pure policy ──────────────────────────────────────────────────────────────


#: Silent intervals from silencedetect stderr, and mean/max from volumedetect.
#: Shared with the soak harness so both paths read the same audio identically.
parse_silences = _features.parse_silences
parse_levels = _features.parse_levels


def boundary_from_poll(rows: list[dict]) -> dict | None:
    """Epoch-ms of the track transition in a poller timeline.

    Two detectors, mirroring rig/classify.py: a ``track_id`` change between two
    known, different tracks, and a position reset (a queue can legitimately hold
    the same track twice). Returns the first transition found, with the
    bracketing sample times so the caller can see how tightly it is pinned.
    """
    prev = None
    prev_t = None
    prev_pos = None
    for row in rows:
        if row.get("err"):
            continue
        tid = row.get("track_id")
        pos = row.get("position_ms")
        if prev is not None and tid is not None and tid != prev:
            return {"t": row["t"], "from": prev, "to": tid, "via": "track_id",
                    "prev_t": prev_t}
        if (prev is not None and tid is not None and tid == prev
                and prev_pos is not None and pos is not None
                and prev_pos - pos > 10000 and pos < 5000
                and row.get("is_playing")):
            return {"t": row["t"], "from": prev, "to": tid, "via": "position_reset",
                    "prev_t": prev_t}
        if tid is not None:
            prev, prev_t, prev_pos = tid, row["t"], pos
    return None


def achieved_interval_ms(rows: list[dict]) -> float | None:
    """Median inter-sample gap actually achieved by the poller.

    The requested interval is not evidence. A sampler that shells out per
    sample silently runs an order of magnitude slower than asked, and a
    boundary split derived from it carries that error invisibly.
    """
    ts = [r["t"] for r in rows if "t" in r]
    if len(ts) < 3:
        return None
    deltas = sorted(ts[i + 1] - ts[i] for i in range(len(ts) - 1))
    mid = len(deltas) // 2
    if len(deltas) % 2:
        return float(deltas[mid])
    return (deltas[mid - 1] + deltas[mid]) / 2.0


def split_at_boundary(start_ms: int, end_ms: int, boundary_ms: int) -> tuple[float, float]:
    """Split one silence into (pre, post) seconds about the transition.

    Load-bearing: silence BEFORE the transition belongs to the outgoing track —
    its fade-out — and silence AFTER it is the player's responsibility. A
    5.582 s "gapless failure" in this repo's history was 5.449 s of fade-out and
    0.133 s of real gap. Only the post side is a verdict on our code.
    """
    pre = max(0.0, min(boundary_ms, end_ms) - start_ms) / 1000.0
    post = max(0.0, end_ms - max(boundary_ms, start_ms)) / 1000.0
    return round(pre, 4), round(post, 4)


def boundary_silences(silences: list[dict], cap_t0_ms: int, boundary_ms: int,
                      floor_db: float | None,
                      window_ms: int = BOUNDARY_WINDOW_MS) -> list[dict]:
    """Silences attributable to the boundary, pre/post split and floor-filtered.

    ``floor_db`` is required. With it None the quiet-audio filter is off and
    quiet music reads as silence — this is the 17-in-18 false-positive case, so
    it raises rather than returning an optimistic answer.
    """
    if floor_db is None:
        raise ProbeRefusal("no measured noise floor — refusing to classify "
                           "(the quiet-audio filter would be disabled)")
    out = []
    for s in silences:
        if s.get("end_s") is None:
            # A silence still running at end-of-capture cannot be split about
            # the boundary. Skipping it would report 0 ms — a capture that
            # stopped INSIDE the gap would pass.
            raise ProbeRefusal(
                "a silence runs to the end of the capture and cannot be split "
                "about the boundary; re-capture with more tail")
        a = cap_t0_ms + int(s["start_s"] * 1000)
        b = cap_t0_ms + int(s["end_s"] * 1000)
        if b < boundary_ms - window_ms or a > boundary_ms + window_ms:
            continue
        lvl = s.get("level_db")
        if lvl is not None and lvl > floor_db + QUIET_AUDIO_MARGIN_DB:
            out.append({**s, "label": "quiet_audio", "pre_s": 0.0, "post_s": 0.0,
                        "why": f"measured {lvl:.1f} dB over a {floor_db:.1f} dB floor"})
            continue
        pre, post = split_at_boundary(a, b, boundary_ms)
        out.append({**s, "label": "boundary_gap", "pre_s": pre, "post_s": post})
    return out


def post_boundary_total_s(labelled: list[dict]) -> float:
    """Total post-transition silence — the single number the bar is applied to."""
    return round(sum(x["post_s"] for x in labelled if x["label"] == "boundary_gap"), 4)


def gain_to_match_db(measured_mean_db: float, target_mean_db: float) -> float:
    """dB gain that brings a digital stitch to the acoustic arm's level.

    Without this the two arms run the quiet-audio filter at different
    signal-to-floor ratios: the decoded stitch sits near full scale while the
    loopback capture sits tens of dB down, so identical material is filtered in
    one arm and counted in the other, and the arms stop being comparable in the
    one quantity being compared.
    """
    return round(target_mean_db - measured_mean_db, 3)


def verdict(acoustic_post_s: float, control_post_s: float,
            tolerance_ms: int = PASS_TOLERANCE_MS) -> dict:
    """Pass when the acoustic boundary is within tolerance of the source control.

    The control is the physical floor: the fix is never required to beat what
    the source files themselves produce, and cannot be credited for beating it.
    """
    delta_ms = round((acoustic_post_s - control_post_s) * 1000.0, 1)
    return {"pass": abs(delta_ms) <= tolerance_ms,
            "delta_ms": delta_ms,
            "acoustic_post_s": acoustic_post_s,
            "control_post_s": control_post_s,
            "tolerance_ms": tolerance_ms}


def control_recovered(expected_ms: int, measured_post_s: float,
                      tolerance_ms: int = PASS_TOLERANCE_MS) -> bool:
    """Did a graduated positive control come back at the magnitude injected?

    A control the instrument cannot recover means the instrument is blind at
    that magnitude; a later clean reading from it proves nothing. One-second
    controls only ever prove one-second sensitivity.

    The error bar is capped at half the control's own magnitude. Using the flat
    pass tolerance here would let a 100 ms control "recover" at a measured
    0 ms — the instrument detecting *nothing* would certify itself as
    sensitive, which is the exact vacuous pass this check exists to prevent.
    """
    allowed = min(float(tolerance_ms), expected_ms / 2.0)
    return abs(measured_post_s * 1000.0 - expected_ms) <= allowed


def series_pass(results: list[dict], need: int = 3, *,
                control_recovered: bool | None = None) -> dict:
    """Terminate on ``need`` CONSECUTIVE passes, with a proven instrument.

    Two guards against mining a pass out of a mostly-refusing run:

    - An aborted iteration resets the streak. "Three consecutive clean runs"
      has to mean consecutive; letting aborts bridge the gap lets an
      instrument that usually refuses assemble three scattered passes into
      false confidence.
    - A pass cannot be declared at all unless a positive control was recovered
      in the same session. An absence assertion from an instrument never shown
      to detect the thing is not evidence.
    """
    streak = 0
    for r in results:
        if r.get("aborted"):
            streak = 0
            continue
        streak = streak + 1 if r.get("pass") else 0
        if streak >= need:
            if control_recovered is not True:
                return {"done": False, "verdict": "unverified_instrument",
                        "streak": streak,
                        "why": "no positive control was recovered this session"}
            return {"done": True, "verdict": "pass", "streak": streak}
    failures = sum(1 for r in results if not r.get("aborted") and not r.get("pass"))
    return {"done": False, "verdict": "pending", "streak": streak,
            "failures": failures}


# ── injected I/O ─────────────────────────────────────────────────────────────


@dataclass
class Deps:
    """All network and subprocess I/O, injected so policy stays testable.

    The admin password is read from the environment and never stored on the
    instance in a form that reaches a record: ``redact()`` is applied to every
    string this module emits, because a generic exception stringifier in this
    repo's ``monitor.py`` once wrote part of the admin password into a JSONL
    file that the analysis step then read.
    """

    #: Empty is legal for analysis-only use (ffmpeg, no HTTP). Any network
    #: call demands a real target, and it always comes from the environment —
    #: never a default literal, which the harness hygiene test enforces.
    base: str = ""
    admin_pw: str = field(default_factory=lambda: os.environ.get("JP_ADMIN_PW", ""))
    _http: object = None
    _logged_in: bool = False

    def redact(self, text: str) -> str:
        out = str(text)
        if self.admin_pw:
            out = out.replace(self.admin_pw, "***")
            out = out.replace(urllib.parse.quote(self.admin_pw), "***")
        return out

    def _opener(self):
        """A cookie-retaining opener, so a login actually sticks.

        The admin routes are session-cookie gated; without a jar every /admin
        call after login would 401 again.
        """
        if self._http is None:
            jar = http.cookiejar.CookieJar()
            self._http = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(jar))
        return self._http

    def login(self) -> None:
        """Authenticate once; subsequent /admin calls ride the cookie."""
        if not self.admin_pw:
            raise ProbeRefusal(
                "JP_ADMIN_PW is unset — the probe cannot reach the admin "
                "endpoints it needs to snapshot and restore rig state")
        self._req("/admin/auth/login/local", {"password": self.admin_pw},
                  _skip_login=True)
        self._logged_in = True

    def _req(self, path: str, payload=None, method=None, *, _skip_login=False):
        if not self.base:
            raise ProbeRefusal("no rig target configured — set JP_BASE")
        if path.startswith("/admin") and not self._logged_in and not _skip_login:
            self.login()
        url = self.base + path
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data,
                                     method=method or ("POST" if data else "GET"))
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with self._opener().open(req, timeout=20) as r:
                body = r.read().decode()
        except Exception as exc:
            raise ProbeRefusal(self.redact(f"{path} failed: {exc}")) from None
        try:
            return json.loads(body)
        except ValueError:
            # Redacted too: a future authenticated call that echoes its
            # request would otherwise defeat redact() through this path.
            return {"raw": self.redact(body[:400])}

    def now_playing(self):
        return self._req("/api/now-playing")

    def position(self):
        return self._req("/api/playback/position")

    def get_settings(self) -> dict:
        return self._req("/admin/settings")

    def set_setting(self, key: str, value) -> None:
        self._req("/admin/settings", {key: value})

    def get_output(self) -> dict:
        """Active output routing. A SEPARATE endpoint from /admin/settings —
        the routing keys are not in the settings payload."""
        return self._req("/admin/output/active")

    def set_output(self, backend_type: str, device_id: str, host: str | None) -> None:
        self._req("/admin/output/active",
                  {"backend_type": backend_type, "device_id": device_id, "host": host})

    def run(self, args: list[str], timeout: int = 180):
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, self.redact(p.stdout), self.redact(p.stderr)


def analyse_file(deps: Deps, path: str, min_silence_s: str = SILENCE_MIN_S,
                 noise_db: str = NOISE_DB) -> dict:
    """One ffmpeg pass: silencedetect at the probe's minimum, plus levels.

    ``min_silence_s`` defaults to this module's 0.02, NOT features.py's 0.4 —
    see the module docstring for why that difference is the whole point.
    """
    rc, _out, err = deps.run([
        "ffmpeg", "-nostdin", "-hide_banner", "-i", path,
        "-af", f"silencedetect=noise={noise_db}dB:d={min_silence_s},volumedetect",
        "-f", "null", "-",
    ])
    if rc != 0:
        # Silences are parsed out of stderr, so a failed ffmpeg yields an
        # empty list — which reads downstream as a perfectly clean boundary.
        # A broken measurement must refuse, never pass.
        raise ProbeRefusal(f"ffmpeg analysis failed (rc {rc}): {err[-200:]}")
    levels = parse_levels(err)
    dur = None
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    sil = parse_silences(err, dur)
    for s in sil:
        if s.get("end_s") is not None and s["dur_s"] and s["dur_s"] > 0:
            s["level_db"] = level_in(deps, path, s["start_s"], s["dur_s"])
    return {"duration_s": dur, "levels": levels, "silences": sil,
            "min_silence_s": min_silence_s, "noise_db": noise_db}


def level_in(deps: Deps, path: str, start_s: float, dur_s: float) -> float | None:
    """Mean level INSIDE one flagged interval.

    silencedetect says "quieter than the trigger for long enough"; it does not
    say how quiet. This is the measurement that separates a real dropout from
    quiet music, and without it the detector had a 17-in-18 false-positive rate
    on real material.
    """
    rc, _out, err = deps.run([
        "ffmpeg", "-nostdin", "-hide_banner", "-ss", f"{start_s:.4f}",
        "-t", f"{dur_s:.4f}", "-i", path, "-af", "volumedetect", "-f", "null", "-",
    ], timeout=120)
    if rc != 0:
        # Returning None here would be read as "level unmeasurable", and an
        # unmeasurable level is counted as a real gap — so a crashed ffmpeg
        # would INVENT gaps. Refuse instead.
        raise ProbeRefusal(
            f"level measurement failed (rc {rc}) at {start_s:.3f}s: {err[-200:]}")
    mean = parse_levels(err).get("mean")
    if mean is None:
        raise ProbeRefusal(f"no level reading inside the interval at {start_s:.3f}s")
    return mean


def measure_floor(deps: Deps, seconds: int = 6) -> float:
    """Measured silence floor for THIS hardware, this run.

    Judged over the floor, never as an absolute dBFS number — the same reading
    means different things on different capture chains.
    """
    path = "/tmp/jp-floor.wav"
    rc, _o, err = deps.run(["arecord", "-D", os.environ.get("JP_ALSA_CAPTURE", "default"),
                            "-f", "S24_3LE", "-r", "48000", "-c", "2",
                            "-d", str(int(seconds)), path])
    if rc != 0:
        raise ProbeRefusal(f"floor capture failed (rc {rc}): {err[-200:]}")
    rc, _o, err = deps.run(["ffmpeg", "-nostdin", "-hide_banner", "-i", path,
                            "-af", "volumedetect", "-f", "null", "-"])
    mean = parse_levels(err).get("mean")
    if mean is None:
        raise ProbeRefusal("floor capture produced no level reading")
    return mean


# ── rig state: snapshot and restore ──────────────────────────────────────────


class RigState:
    """Snapshot the settings the probe mutates, and put them back.

    The guarantee is "restored to the value the API reported at snapshot time",
    not "restored to absent": ``/admin/settings`` and ``/admin/output/active``
    both resolve unset rows to defaults and there is no delete-setting
    endpoint, so absent and default-valued are indistinguishable over HTTP.

    This exists because process hygiene is only half the job. A flipped setting
    has no PID, no parent and no command line to sweep for — it survives the
    script exiting, the session closing, and a container restart. One left set
    on this rig auto-played ~3,000 tracks over 9 days 21 hours with a
    completely clean process sweep.
    """

    def __init__(self, deps: Deps):
        self.deps = deps
        self.snapshot: dict = {}
        self.output_snapshot: dict = {}
        self.restored: dict = {}
        self.restore_errors: list[str] = []

    def __enter__(self):
        settings = self.deps.get_settings()
        self.snapshot = {k: settings.get(k) for k in MANAGED_SETTINGS}
        output = self.deps.get_output()
        self.output_snapshot = {k: output.get(k) for k in MANAGED_OUTPUT}
        missing = [k for k in MANAGED_SETTINGS if k not in settings]
        missing += [f"output.{k}" for k in MANAGED_OUTPUT if k not in output]
        if missing:
            # Refuse rather than run: a key the snapshot could not read is a
            # key the restore cannot put back, and discovering that after the
            # run is how a setting gets left flipped.
            raise ProbeRefusal(
                "cannot snapshot " + ", ".join(missing) + " — refusing to "
                "mutate state the probe could not record")
        return self

    def __exit__(self, exc_type, exc, tb):
        # Every key is attempted even when one raises — a partial restore that
        # stops at the first error is how the worst of these leaks survive.
        for key, value in self.snapshot.items():
            if value is None:
                continue
            try:
                self.deps.set_setting(key, value)
                self.restored[key] = value
            except Exception as err:
                self.restore_errors.append(self.deps.redact(f"{key}: {err}"))
        # Output routing restores as ONE call — backend, device and host are a
        # tuple the endpoint applies together; writing them piecemeal would
        # briefly point the router at a device that was never selected.
        if self.output_snapshot.get("backend_type"):
            try:
                self.deps.set_output(self.output_snapshot["backend_type"],
                                     self.output_snapshot.get("device_id") or "default",
                                     self.output_snapshot.get("host"))
                self.restored["output"] = dict(self.output_snapshot)
            except Exception as err:
                self.restore_errors.append(self.deps.redact(f"output: {err}"))
        if self.restore_errors:
            # Logged here, not left for a caller to remember to inspect: an
            # unrestored setting that nobody notices is the whole failure mode
            # this class exists for.
            print("BOUNDARY_PROBE: RIG STATE NOT FULLY RESTORED: "
                  + "; ".join(self.restore_errors))
        return False  # never swallow the original exception


# ── analysis entrypoint ──────────────────────────────────────────────────────


def poll_timeline(deps: Deps, out_path: str, seconds: float,
                  interval_s: float = 0.02, stop=None) -> float:
    """Write a boundary-detection timeline, and return the ACHIEVED interval.

    Ships with the probe rather than being left to the caller, because the
    sample rate is not a detail — it is the error bar on every number the
    probe produces, and a boundary pinned to ±111 ms cannot support a 100 ms
    bar however carefully the audio is measured.

    One HTTP call per sample, deliberately. ``/api/now-playing`` carries the
    track id, which is all ``boundary_from_poll``'s primary detector needs;
    adding the position endpoint for the position-reset detector halves the
    rate and so doubles the error bar. Position is sampled on a slow
    sub-cadence instead, which is all that detector needs to spot a reset.
    """
    import time
    rows: list[dict] = []
    deadline = time.time() + seconds
    pos, pos_at = None, 0.0
    with open(out_path, "w") as f:
        while time.time() < deadline and (stop is None or not stop.is_set()):
            t = time.time()
            try:
                np = deps.now_playing()
                if t - pos_at > 1.0:
                    pos = deps.position().get("position_ms")
                    pos_at = t
                row = {"t": int(t * 1000), "track_id": np.get("track_id"),
                       "title": np.get("title"), "position_ms": pos,
                       "is_playing": True,
                       "flow": (np.get("output_session") or {}).get(
                           "gapless_flow_active")}
            except Exception as exc:
                row = {"t": int(t * 1000), "err": deps.redact(exc)[:80]}
            rows.append(row)
            print(json.dumps(row), file=f)
            f.flush()
            time.sleep(interval_s)
    return achieved_interval_ms(rows) or float("inf")


def analyse_boundary(deps: Deps, cap_path: str, cap_t0_ms: int,
                     poll_rows: list[dict], floor_db: float | None,
                     requested_interval_ms: float = 100.0) -> dict:
    """Full boundary verdict for one capture + its poller timeline.

    Refuses rather than guessing on every input it cannot trust: no boundary in
    the timeline, no floor, or a sampler that did not achieve the rate it was
    asked for. A number derived from a coarser timeline than claimed is worse
    than no number, because it looks like evidence.
    """
    achieved = achieved_interval_ms(poll_rows)
    if achieved is None:
        raise ProbeRefusal("poller produced too few samples to establish a rate")
    if achieved > requested_interval_ms * 3:
        raise ProbeRefusal(
            f"sampler achieved {achieved:.0f} ms against a requested "
            f"{requested_interval_ms:.0f} ms — the boundary split would carry "
            "that error invisibly")
    boundary = boundary_from_poll(poll_rows)
    if boundary is None:
        raise ProbeRefusal("no track transition found in the poller timeline")
    analysis = analyse_file(deps, cap_path)
    # The boundary must actually fall INSIDE the capture. A wrong cap_t0_ms —
    # a clock skew, a mis-passed flag, a capture that started late — puts it
    # outside, nothing matches the window, and the result is a confident
    # 0 ms PASS measured from audio that never contained the transition.
    dur_ms = int((analysis.get("duration_s") or 0) * 1000)
    if not (cap_t0_ms <= boundary["t"] <= cap_t0_ms + dur_ms):
        raise ProbeRefusal(
            f"the transition at {boundary['t']} falls outside the capture "
            f"[{cap_t0_ms}, {cap_t0_ms + dur_ms}] — this capture does not "
            "contain the boundary")
    pinned = boundary["t"] - (boundary.get("prev_t") or boundary["t"])
    if pinned > PASS_TOLERANCE_MS:
        raise ProbeRefusal(
            f"the transition is pinned only to within {pinned} ms, which is "
            f"coarser than the {PASS_TOLERANCE_MS} ms bar it would be judged "
            "against")
    labelled = boundary_silences(analysis["silences"], cap_t0_ms, boundary["t"],
                                 floor_db)
    return {
        "boundary": boundary,
        "boundary_pinned_ms": pinned,
        "achieved_interval_ms": achieved,
        "floor_db": floor_db,
        "min_silence_s": analysis["min_silence_s"],
        "capture_levels": analysis["levels"],
        "silences": analysis["silences"],
        "labelled": labelled,
        "post_boundary_s": post_boundary_total_s(labelled),
    }


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Measure one track-boundary gap.")
    ap.add_argument("--capture", required=True)
    ap.add_argument("--poll", required=True)
    ap.add_argument("--cap-t0-ms", type=int, required=True)
    ap.add_argument("--floor-db", type=float, default=None)
    ap.add_argument("--measure-floor", action="store_true")
    ap.add_argument("--requested-interval-ms", type=float, default=100.0)
    ap.add_argument("--control-ms", type=int, default=None,
                    help="expected gap for a positive-control run")
    args = ap.parse_args(argv)

    deps = Deps(base=os.environ.get("JP_BASE", ""))
    rows = [json.loads(l) for l in open(args.poll) if l.strip()]
    floor = args.floor_db
    try:
        if args.measure_floor and floor is None:
            floor = measure_floor(deps)
        out = analyse_boundary(deps, args.capture, args.cap_t0_ms, rows, floor,
                               args.requested_interval_ms)
    except ProbeRefusal as exc:
        print(json.dumps({"refused": str(exc)}, indent=2))
        return 3
    if args.control_ms is not None:
        out["control_recovered"] = control_recovered(args.control_ms,
                                                     out["post_boundary_s"])
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
