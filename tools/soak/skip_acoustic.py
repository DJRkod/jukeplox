"""Acoustic skip-to-audible probe for the Chromecast gapless (flow) path.

Runs ON the rig. Needs ``arecord``, ``ffmpeg``, and HTTP reach to the app.

WHY THIS EXISTS AND WHY THE EARLIER PROBE WAS RETIRED
-----------------------------------------------------
``skip_probe.py`` measures skip-to-audible from the app's own device-position
field. That field only refreshes when the Cast receiver reports in -- roughly
once a second, on the receiver's schedule -- so polling faster returns the same
stale value more often. It is adequate to establish "10-18 seconds" and useless
for steering a fix toward 2.5 s, because its resolution is the same order as the
improvement being chased.

This probe measures the sound instead. The WiiM Pro's line-out is cabled into
the Alesis capture input, so the audio actually leaving the Cast target is
recordable (see
docs/solutions/workflow-issues/autonomous-hardware-validation-loopback-rig.md).

THE TRIGGER IS A CHANGE OF CONTENT, NOT A SILENCE
-------------------------------------------------
A flow reposition produces no silence -- the stream is continuous by
construction, which is the entire point of flow mode. So ``silencedetect`` (the
existing acoustic harness) has nothing to fire on. Instead the queue is filled
with generated single-frequency marker tracks, cycled so consecutive tracks
always differ, and the event is "the dominant frequency changed from the track
being rejected to the track being skipped to".

Marker frequencies all live inside ONE octave. That is not cosmetic: if any
marker were an integer multiple of another, harmonic distortion in the DAC/ADC
chain could register as the wrong track.

THE TIMEBASE, AND WHICH WAY ITS ERROR POINTS
--------------------------------------------
Dispatch time is host-monotonic. Audible time has to come from the same clock or
the difference is meaningless, so capture is read incrementally in one process
and every read is stamped with the same ``time.monotonic()`` that stamps the
skip. A sample's host time is interpolated LOCALLY between the two marks
bracketing its byte offset, which is exact regardless of any long-term drift
between the interface's 48 kHz and the host clock.

Two residual errors, both bounded and both stated rather than calibrated away:

  * The ALSA capture buffer. If this process reads late, bytes sit in the kernel
    and get stamped later than they were captured, so the measured latency comes
    out TOO LARGE. Bounded by the buffer size (default 2048 frames = 43 ms) and
    conservative in direction -- it can never manufacture an improvement.
  * Window granularity. The transition is bracketed between the last window
    that is still the old tone and the first that is the new one, so it is
    bounded by window + hop (default 43 + 11 ms).

Total under 100 ms, biased toward overstating. That is what makes a 250 ms
claim supportable.

REFUSAL DISCIPLINE
------------------
Follows ``boundary_probe.py``: raise ``ProbeRefusal``, never return an
optimistic number. A silent 0 reads exactly like a perfect fix. Every failure
gets its own negative sentinel. It refuses when the dispatch path was not flow,
when arecord reported an overrun (dropped samples break the byte-to-time map),
when the mark timeline deviates from its own fit by more than the error budget,
and when any managed setting could not be snapshotted.

Usage (on the rig):
  JP_BASE=http://127.0.0.1 JP_ADMIN_PW=... python3 skip_acoustic.py --skips 10
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import signal
import subprocess
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from boundary_probe import Deps, ProbeRefusal, RigState  # noqa: E402

# ---- capture format --------------------------------------------------------
# The interface accepts ONE format; asking for S16_LE fails with "Sample format
# non available", which reads like a broken device rather than a wrong argument.
CAP_DEVICE = os.environ.get("JP_ALSA_CAPTURE", "default")
CAP_FORMAT = "S24_3LE"
CAP_BYTES_PER_SAMPLE = 3
CAP_CHANNELS = int(os.environ.get("JP_ALSA_CHANNELS", "2"))
CAP_RATE = int(os.environ.get("JP_ALSA_RATE", "48000"))
#: Small on purpose -- this is the upper bound on how late a sample can be
#: stamped, and therefore on how much the probe can overstate a latency.
CAP_PERIOD = int(os.environ.get("JP_ALSA_PERIOD", "512"))
CAP_BUFFER = int(os.environ.get("JP_ALSA_BUFFER", "2048"))
BYTES_PER_FRAME = CAP_BYTES_PER_SAMPLE * CAP_CHANNELS

# ---- detector --------------------------------------------------------------
#: Decimation before analysis. There is no numpy on the rig, so the detector
#: runs in pure Python; dropping to 6 kHz makes that cheap. Averaging groups of
#: 8 is a crude low-pass whose first null is at 6 kHz -- fine for tones that all
#: sit below 1.2 kHz.
DECIMATE = 8
ANALYSIS_RATE = CAP_RATE // DECIMATE          # 6000 Hz
WINDOW = int(os.environ.get("JP_WINDOW", "256"))   # 42.7 ms @ 6 kHz
HOP = int(os.environ.get("JP_HOP", "64"))          # 10.7 ms
#: One octave (700-1400), so no marker is a harmonic of another. Spacing is
#: >= 120 Hz against a 23 Hz bin width, so a slightly-down neighbour cannot win.
#:
#: The last entry is reserved as the SECOND-HALF tone of every marker track.
#: Seek needs that: a constant-tone track cannot show a jump within itself, so
#: every marker is <first-half tone> then SECOND_HALF_TONE, and a seek from the
#: front half into the back half becomes an audible, expected change. It also
#: keeps the three events distinguishable --
#:   seek      : first_half_tone -> 1320
#:   boundary  : 1320 -> the NEXT track's first-half tone
#:   skip      : whatever is playing -> the destination's first-half tone
#: which is only unambiguous because 1320 is never a track's first half.
MARKER_TONES = (700, 820, 960, 1120, 1320)
SECOND_HALF_TONE = 1320
FIRST_HALF_TONES = (700, 820, 960, 1120)
#: A window must clear this to be called at all. Measured silence floor on this
#: rig is -90 dB mean; live playback is about -22 dB.
WINDOW_FLOOR_DB = float(os.environ.get("JP_WINDOW_FLOOR_DB", "-55"))
#: The winning tone must beat the runner-up by this ratio, else the window is
#: ambiguous and takes no part in the decision.
DOMINANCE_RATIO = float(os.environ.get("JP_DOMINANCE", "4.0"))
#: Consecutive agreeing windows required before a label is believed. One window
#: can be fooled by a transient; three spanning ~64 ms cannot.
SUSTAIN = int(os.environ.get("JP_SUSTAIN", "3"))

#: How much two adjacent runs may overlap and still count as consecutive.
#: Exactly the detector's window span: overlapping windows mean a seamless join
#: can place the new tone's first window before the old tone's last one ends.
_RUN_OVERLAP_MS = (WINDOW + HOP) * 1000.0 / ANALYSIS_RATE

#: Error budget for the mark timeline, in ms. Deviation beyond this from the
#: timeline's own linear fit means the byte-to-time map is not trustworthy.
MARK_FIT_TOLERANCE_MS = float(os.environ.get("JP_MARK_TOL_MS", "120"))

# ---- sentinels: every failure shape gets its own, so none reads as success --
UNREACHED = -1        # the new tone never became audible inside the watch
NOT_FLOW = -2         # dispatch did not take the flow path
AMBIGUOUS = -3        # tones could not be told apart around the dispatch


# ---- pure analysis ---------------------------------------------------------

def tone_cycle(count, tones=FIRST_HALF_TONES):
    """Frequencies for ``count`` marker tracks, cycled so neighbours differ.

    A 4-cycle means a skip of 1, 2 or 3 tracks all land on a different tone, so
    a multi-skip can be measured without the detector confusing the destination
    for the origin.
    """
    if count <= 0:
        raise ValueError("count must be positive")
    return [tones[i % len(tones)] for i in range(count)]


def decimate_mono(raw, factor=DECIMATE):
    """S24_3LE interleaved stereo -> decimated mono floats in [-1, 1].

    Averaging rather than naive subsampling: a bare stride would alias anything
    above the new Nyquist straight down onto the marker band.
    """
    step = BYTES_PER_FRAME
    nframes = len(raw) // step
    out = []
    acc = 0.0
    n = 0
    for f in range(nframes):
        base = f * step
        total = 0
        for c in range(CAP_CHANNELS):
            o = base + c * CAP_BYTES_PER_SAMPLE
            v = raw[o] | (raw[o + 1] << 8) | (raw[o + 2] << 16)
            if v & 0x800000:
                v -= 0x1000000
            total += v
        acc += total / (CAP_CHANNELS * 8388608.0)
        n += 1
        if n == factor:
            out.append(acc / factor)
            acc = 0.0
            n = 0
    return out


def goertzel_power(samples, freq, rate=ANALYSIS_RATE):
    """Single-bin power at ``freq``.

    Cheaper than an FFT when only a few bins matter, which is the whole reason
    the detector targets known tones rather than finding a spectral peak.
    """
    n = len(samples)
    if n == 0:
        return 0.0
    k = int(0.5 + (n * freq) / rate)
    w = (2.0 * math.pi * k) / n
    coeff = 2.0 * math.cos(w)
    s_prev = 0.0
    s_prev2 = 0.0
    for x in samples:
        s = coeff * s_prev - s_prev2 + x
        s_prev2 = s_prev
        s_prev = s
    return (s_prev2 * s_prev2 + s_prev * s_prev
            - coeff * s_prev * s_prev2) / (n * n)


def rms_db(samples):
    if not samples:
        return -120.0
    acc = 0.0
    for x in samples:
        acc += x * x
    r = math.sqrt(acc / len(samples))
    return 20.0 * math.log10(r) if r > 1e-9 else -120.0


def classify_window(samples, tones=MARKER_TONES, rate=ANALYSIS_RATE,
                    floor_db=WINDOW_FLOOR_DB, dominance=DOMINANCE_RATIO):
    """Which marker tone owns this window, or None.

    ``floor_db`` is explicit rather than defaulted-to-None: an earlier acoustic
    harness shipped a classifier whose floor defaulted to None with no caller
    supplying one, so its quiet-audio guard could never fire.
    """
    if rms_db(samples) < floor_db:
        return None
    powers = sorted(((goertzel_power(samples, f, rate), f) for f in tones),
                    reverse=True)
    best, best_f = powers[0]
    runner = powers[1][0] if len(powers) > 1 else 0.0
    if best <= 0.0:
        return None
    if runner > 0.0 and best / runner < dominance:
        return None
    return best_f


def label_windows(samples, rate=ANALYSIS_RATE, window=WINDOW, hop=HOP,
                  tones=MARKER_TONES, floor_db=WINDOW_FLOOR_DB,
                  dominance=DOMINANCE_RATIO):
    """[(start_sample, end_sample, tone_or_None), ...] over the whole buffer."""
    out = []
    i = 0
    n = len(samples)
    while i + window <= n:
        out.append((i, i + window,
                    classify_window(samples[i:i + window], tones, rate,
                                    floor_db, dominance)))
        i += hop
    return out


def sustained_runs(labels, sustain=SUSTAIN):
    """Collapse windows into runs of >= ``sustain`` agreeing non-None labels.

    Returns [(tone, first_start_sample, last_end_sample), ...]. Windows that are
    None, or part of too short a run, are dropped rather than guessed at.
    """
    runs = []
    cur_tone = None
    cur = []
    for start, end, tone in labels:
        if tone is not None and tone == cur_tone:
            cur.append((start, end))
            continue
        if cur_tone is not None and len(cur) >= sustain:
            runs.append((cur_tone, cur[0][0], cur[-1][1]))
        cur_tone = tone
        cur = [(start, end)] if tone is not None else []
    if cur_tone is not None and len(cur) >= sustain:
        runs.append((cur_tone, cur[0][0], cur[-1][1]))
    return runs


def mark_time_ms(byte_offset, marks):
    """Host-monotonic ms for the sample at ``byte_offset``, by LOCAL interpolation.

    ``marks`` is [(bytes_total_after_read, t_ms), ...] ascending. Local
    interpolation is used deliberately: it is exact between two marks whatever
    the long-run drift between the interface clock and the host clock, where a
    single global origin plus a nominal rate is not.
    """
    if len(marks) < 2:
        raise ProbeRefusal("capture produced fewer than two timeline marks - "
                           "no byte-to-time mapping is possible")
    if byte_offset <= marks[0][0]:
        raise ProbeRefusal("byte offset precedes the first timeline mark")
    if byte_offset > marks[-1][0]:
        raise ProbeRefusal("byte offset is past the end of the capture")
    lo = 0
    hi = len(marks) - 1
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if marks[mid][0] < byte_offset:
            lo = mid
        else:
            hi = mid
    b0, t0 = marks[lo]
    b1, t1 = marks[hi]
    if b1 == b0:
        return float(t1)
    return t0 + (byte_offset - b0) * (t1 - t0) / float(b1 - b0)


def check_mark_timeline(marks, rate=CAP_RATE, bytes_per_frame=BYTES_PER_FRAME,
                        tolerance_ms=MARK_FIT_TOLERANCE_MS):
    """Least-squares the marks and refuse if any deviates beyond the budget.

    The fit absorbs a constant clock-rate offset between the interface and the
    host (tens of ppm, harmless). What it cannot absorb is a stall or a drop,
    and those are exactly what would silently shift every reported latency.
    """
    if len(marks) < 4:
        raise ProbeRefusal("too few timeline marks to validate the capture")
    xs = [m[0] / float(bytes_per_frame) / rate * 1000.0 for m in marks]
    ys = [float(m[1]) for m in marks]
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        raise ProbeRefusal("capture timeline has no span")
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    intercept = my - slope * mx
    worst = 0.0
    for x, y in zip(xs, ys):
        worst = max(worst, abs(y - (slope * x + intercept)))
    info = {"marks": n, "fit_slope": round(slope, 6),
            "worst_deviation_ms": round(worst, 1),
            "span_s": round((xs[-1] - xs[0]) / 1000.0, 1)}
    if worst > tolerance_ms:
        raise ProbeRefusal(
            "capture timeline deviates %.0f ms from its own fit (budget "
            "%.0f ms) - the byte-to-time map cannot support a 250 ms claim; "
            "suspect an ALSA stall or a slow reader" % (worst, tolerance_ms))
    if not 0.98 <= slope <= 1.02:
        raise ProbeRefusal(
            "capture clock runs at %.4fx the assumed %d Hz - byte offsets do "
            "not map to real time" % (slope, rate))
    return info


def skip_to_audible_ms(runs, marks, dispatch_ms, from_tone, to_tone,
                       decimate=DECIMATE, bytes_per_frame=BYTES_PER_FRAME):
    """Locate the from_tone -> to_tone transition and difference it against dispatch.

    Returns (value_or_sentinel, detail). The value is the START of the first
    sustained run of ``to_tone`` beginning at or after dispatch, converted to
    host time -- the earliest instant the new track is provably audible.
    """
    if from_tone == to_tone:
        raise ProbeRefusal("origin and destination markers are the same tone - "
                           "the queue cycle is wrong, not the measurement")
    for tone, start, _end in runs:
        if tone != to_tone:
            continue
        try:
            t = mark_time_ms(start * decimate * bytes_per_frame, marks)
        except ProbeRefusal:
            continue
        if t >= dispatch_ms:
            return (t - dispatch_ms,
                    {"audible_at_ms": round(t, 1),
                     "dispatch_ms": round(dispatch_ms, 1)})
    return (UNREACHED, {"dispatch_ms": round(dispatch_ms, 1)})


def summarise(values, label, target_ms=2500.0):
    """Distribution, not a headline. Modality included because a bimodal
    result means two mechanisms, and a median across both describes neither."""
    usable = sorted(v for v in values if v is not None and v >= 0)
    out = {"arm": label, "n": len(usable), "n_refused": len(values) - len(usable),
           "n_rows": len(values)}
    if not usable:
        return out
    out.update({
        "median_ms": round(statistics.median(usable), 1),
        "min_ms": round(usable[0], 1),
        "max_ms": round(usable[-1], 1),
        "spread_ms": round(usable[-1] - usable[0], 1),
        "samples_ms": [round(v, 1) for v in usable],
        "meets_target": statistics.median(usable) < target_ms,
        "target_ms": target_ms,
    })
    if len(usable) >= 4:
        # Split at the WIDEST gap between neighbours, not at the median. A
        # median split puts the median itself in the lower cluster, which makes
        # that cluster look as wide as the whole distribution and hides exactly
        # the split it was supposed to find.
        gap, idx = max((usable[i + 1] - usable[i], i)
                       for i in range(len(usable) - 1))
        low = usable[:idx + 1]
        high = usable[idx + 1:]
        inner = max(max(low) - min(low), max(high) - min(high), 1.0)
        out["largest_internal_gap_ms"] = round(gap, 1)
        # Both sides need at least two members: one far value is an outlier,
        # not a second mechanism, and calling it a mode would overstate what
        # the data shows.
        out["possibly_bimodal"] = (len(low) >= 2 and len(high) >= 2
                                   and gap > 2 * inner)
    return out


# ---- capture ---------------------------------------------------------------

class Capture:
    """arecord into a raw file, with every read stamped on the host clock.

    The marks are the whole point. ``arecord -d N file.wav`` would be simpler
    and would give no way to say when any particular sample was captured, which
    is the only thing that makes a dispatch-to-audible difference meaningful.

    THE READER RUNS IN ITS OWN PROCESS, and that is not an optimisation.
    It was a thread first. The detector is pure Python (there is no numpy on the
    rig) and chews a whole analysis window at a time, so it holds the
    interpreter lock in long stretches; the reader fell behind, and the stamps
    it eventually wrote were minutes away from when the audio was captured. The
    timeline guard caught it -- one arm refused with 45,281 ms of deviation
    against a 120 ms budget -- but a guard catching it every time is not a
    working instrument. A separate process cannot be starved by analysis at all.

    The helper writes two files: the raw audio, and a text sidecar of
    ``<bytes_total> <host_ms>`` lines. The parent reads the sidecar whenever it
    needs the timeline, which is why ``refresh_marks()`` exists.
    """

    def __init__(self, raw_path, stderr_path, marks_path=None):
        self.raw_path = raw_path
        self.stderr_path = stderr_path
        self.marks_path = marks_path or (raw_path + ".marks")
        self.marks = []
        self.proc = None

    def __enter__(self):
        for p in (self.raw_path, self.marks_path, self.stderr_path):
            try:
                os.unlink(p)
            except OSError:
                pass
        self.proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--capture-helper",
             "--raw", self.raw_path, "--marks", self.marks_path,
             "--cap-stderr", self.stderr_path])
        # Do not return until audio is genuinely flowing. Returning early makes
        # the first skip of a run measure arecord startup instead of the app.
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if len(self.refresh_marks()) >= 4:
                return self
            time.sleep(0.1)
        self.__exit__(None, None, None)
        raise ProbeRefusal(
            "capture produced no audio within 15 s on device %r - if %s says "
            "busy, a previous recorder still holds it"
            % (CAP_DEVICE, self.stderr_path))

    def __exit__(self, exc_type, exc, tb):
        if self.proc is not None and self.proc.poll() is None:
            try:
                # SIGINT, not SIGTERM: the helper closes its files on
                # KeyboardInterrupt, and a truncated sidecar would strand the
                # last window of every measurement.
                self.proc.send_signal(signal.SIGINT)
                self.proc.wait(timeout=8)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        return False

    def refresh_marks(self):
        """Re-read the sidecar. Cheap, and the only way the parent sees time."""
        marks = []
        try:
            with open(self.marks_path, "r") as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) != 2:
                        continue  # a half-written final line, not an error
                    try:
                        marks.append((int(parts[0]), float(parts[1])))
                    except ValueError:
                        continue
        except OSError:
            pass
        self.marks = marks
        return marks

    def overruns(self):
        try:
            with open(self.stderr_path, "rb") as fh:
                return fh.read().decode("utf-8", "replace")
        except Exception:
            return ""

    def byte_at_time(self, t_ms, clamp_to_end=False):
        """First captured byte offset stamped at or after ``t_ms``.

        ``clamp_to_end`` returns the end of the capture when the requested time
        is past it. Use it for the END of an analysis window: a window that
        closes at "now" routinely lands a few milliseconds past the last mark,
        and refusing the whole measurement for that is a refusal about the
        instrument rather than about the system. Never clamp the START -- that
        would silently analyse a different window than the one asked for.
        """
        for total, t in self.marks:
            if t >= t_ms:
                return total
        if clamp_to_end and self.marks:
            return self.marks[-1][0]
        return None

    def read_range(self, start_byte, end_byte):
        start = max(0, start_byte - (start_byte % BYTES_PER_FRAME))
        with open(self.raw_path, "rb") as fh:
            fh.seek(start)
            return start, fh.read(max(0, end_byte - start))


def capture_helper(raw_path, marks_path, stderr_path):
    """The capture process: arecord -> raw file + timestamp sidecar. Nothing else.

    Deliberately does no analysis, no HTTP and no parsing. Every line of work
    added here is work that can delay a stamp, and a late stamp is a wrong
    measurement.
    """
    args = ["arecord", "-D", CAP_DEVICE, "-f", CAP_FORMAT,
            "-c", str(CAP_CHANNELS), "-r", str(CAP_RATE),
            "--period-size=%d" % CAP_PERIOD,
            "--buffer-size=%d" % CAP_BUFFER,
            # NOT -q: quiet suppresses the "overrun!!!" warnings, and those
            # warnings are the only evidence that samples were dropped. An
            # overrun check reading a silenced stream is a guard that cannot
            # fire.
            "-t", "raw", "-"]
    chunk = BYTES_PER_FRAME * CAP_PERIOD
    total = 0
    errfile = open(stderr_path, "wb")
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=errfile)
    try:
        with open(raw_path, "wb") as raw, open(marks_path, "w") as marks:
            while True:
                buf = proc.stdout.read(chunk)
                if not buf:
                    break
                # Stamped AFTER the read returns, so the stamp is never earlier
                # than the samples it covers. Any error from a late read makes
                # the measured latency larger, never smaller.
                t = time.monotonic() * 1000.0
                total += len(buf)
                raw.write(buf)
                raw.flush()
                marks.write("%d %.3f\n" % (total, t))
                marks.flush()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            errfile.close()
        except Exception:
            pass
    return 0


# ---- marker tracks ---------------------------------------------------------

MARKER_ALBUM = "JP Marker Tones"
MARKER_ARTIST = "JP Probe"
MARKER_TITLE_FMT = "Marker %02d %dHz"


def marker_filename(index, freq):
    # "seg" not "tone": the content changed shape when seek measurement was
    # added, and generate_markers skips files that already exist. Reusing the
    # old name would silently measure single-tone leftovers.
    return "%02d-seg%04d.flac" % (index + 1, freq)


def generate_markers(deps, out_dir, count, seconds):
    """Write ``count`` single-tone FLACs, cycled across MARKER_TONES.

    FLAC because flow decodes whatever the source gives it and FLAC is what the
    rest of the library is; a tone is trivially compressible so the files are
    tiny. Tagged as one album with sequential track numbers so queue order is
    deterministic rather than inherited from directory listing order.
    """
    os.makedirs(out_dir, exist_ok=True)
    plan = []
    for i, freq in enumerate(tone_cycle(count)):
        path = os.path.join(out_dir, marker_filename(i, freq))
        plan.append({"index": i, "freq": freq, "path": path,
                     "title": MARKER_TITLE_FMT % (i + 1, freq)})
        if os.path.exists(path):
            continue
        half = max(1, seconds // 2)
        rc, _out, err = deps.run([
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            # Two segments: the track's own tone, then the shared second-half
            # tone. A seek from the front half into the back half is then an
            # audible change with a known destination, which a constant-tone
            # track cannot provide.
            "-f", "lavfi", "-i",
            "sine=frequency=%d:sample_rate=44100:duration=%d" % (freq, half),
            "-f", "lavfi", "-i",
            "sine=frequency=%d:sample_rate=44100:duration=%d"
            % (SECOND_HALF_TONE, half),
            "-filter_complex",
            "[0:a][1:a]concat=n=2:v=0:a=1,volume=-10dB",
            "-metadata", "title=%s" % (MARKER_TITLE_FMT % (i + 1, freq)),
            "-metadata", "artist=%s" % MARKER_ARTIST,
            "-metadata", "albumartist=%s" % MARKER_ARTIST,
            "-metadata", "album=%s" % MARKER_ALBUM,
            "-metadata", "tracknumber=%d" % (i + 1),
            "-c:a", "flac", path,
        ], timeout=120)
        if rc != 0:
            raise ProbeRefusal("marker generation failed (rc %d): %s"
                               % (rc, err[-200:]))
    return plan


def find_marker_tracks(deps, plan):
    """Resolve the generated markers to catalog track ids, in plan order.

    One search per exact title rather than one album listing. ``/api/browse/
    albums`` takes no limit and returns the whole library (4,385 albums on this
    rig), and the queue order has to come from the plan anyway -- matching each
    title individually is both cheaper and immune to any ordering the catalog
    happens to apply.
    """
    by_title = {}
    # One search to find the album, then one listing for all of its tracks.
    # Search costs ~7 s per query on this rig, so doing it per marker turned a
    # 16-track plan into two minutes of setup per arm.
    first = deps._req("/api/search?q=" + urllib.parse.quote(plan[0]["title"]))
    rows = first if isinstance(first, list) else (first or {}).get("tracks", [])
    album_id = None
    for t in rows:
        if str(t.get("title") or "") == plan[0]["title"]:
            album_id = t.get("album_id")
            by_title[plan[0]["title"]] = t.get("id") or t.get("track_id")
            break
    if album_id:
        listing = deps._req("/api/browse/albums/%s/tracks"
                            % urllib.parse.quote(str(album_id)))
        trows = (listing if isinstance(listing, list)
                 else (listing or {}).get("tracks", []))
        for t in trows:
            title = str(t.get("title") or "")
            tid = t.get("id") or t.get("track_id")
            if title and tid:
                by_title[title] = tid

    out = []
    missing = []
    for item in plan:
        tid = by_title.get(item["title"])
        if not tid:
            missing.append(item["title"])
            continue
        out.append(dict(item, track_id=tid))
    if missing:
        raise ProbeRefusal(
            "%d of %d markers are not in the catalog yet (first: %r) - the "
            "local source was registered but the scan has not finished"
            % (len(missing), len(plan), missing[0]))
    return out


# ---- rig arrangement -------------------------------------------------------

def pick_protocol(deps, backend, name_prefix="WiiM Pro"):
    """``(device_id, host, claimed_gapless)`` for one protocol of one device.

    Returns the protocol's advertised gapless capability alongside the routing,
    because the claim is part of what is being tested: a renderer that
    advertises gapless and ignores it is the documented failure mode for DLNA
    on Linkplay hardware, and the audio is the only thing that can settle it.
    """
    for dev in deps._req("/admin/output/devices").get("devices", []):
        if not str(dev.get("name", "")).startswith(name_prefix):
            continue
        if not dev.get("online"):
            continue
        for p in dev.get("protocols", []):
            if p.get("backend") != backend:
                continue
            if p.get("verified") is not True:
                raise ProbeRefusal(
                    "%s offers %s but it is not verified (%s) - measuring an "
                    "unverified protocol measures the probe's patience"
                    % (name_prefix, backend, p.get("gapless")))
            return p["device_id"], dev.get("host"), p.get("gapless")
    raise ProbeRefusal("no online %s offering the %s backend"
                       % (name_prefix, backend))


def wait_for_markers(deps, plan, timeout_s=2400.0):
    # Generous because registering a source triggers a FULL catalog refresh,
    # and on this rig that crawls ~103k tracks. A container restart forces the
    # same rebuild, so the first arm after a redeploy pays the whole cost.
    # /admin/scan-status reports scanning=true throughout.
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        try:
            return find_marker_tracks(deps, plan)
        except ProbeRefusal as exc:
            last = str(exc)
            time.sleep(5.0)
    raise ProbeRefusal("markers never appeared in the catalog: %s" % last)


def arrange_queue(deps, tracks):
    deps._req("/admin/queue/clear", {"confirmed": True})
    for t in tracks:
        deps._req("/admin/queue", {"track_id": t["track_id"]})
    rows = deps._req("/api/queue")
    qrows = rows if isinstance(rows, list) else rows.get("queue", [])
    # Appending to an empty queue while the player is idle AUTO-STARTS
    # playback, which moves the first marker out of the queue and into
    # now-playing. So one missing row is expected; two is a real problem.
    np = deps.now_playing() or {}
    playing_marker = any(t["title"] == str(np.get("title") or "")
                         for t in tracks)
    accounted = len(qrows) + (1 if playing_marker else 0)
    if accounted < len(tracks):
        raise ProbeRefusal(
            "queue accounts for %d of %d markers (%d queued, now-playing %s) - "
            "refusing to measure against a queue that is not the one planned"
            % (accounted, len(tracks), len(qrows),
               "is a marker" if playing_marker else "is not"))


def tone_for_now_playing(deps, tracks):
    """The tone of the track the APP believes is current.

    In flow mode this flips the instant the queue advances, which is the start
    of the stale window, not its end - so it names the DESTINATION of a skip,
    which is exactly what the detector needs to look for.
    """
    np = deps.now_playing() or {}
    # /api/now-playing is FLAT -- title and track_id sit at the top level, not
    # under a "track" object. Guessing the nested shape is how an earlier probe
    # spent a rig session reporting that the queue front was empty.
    title = str(np.get("title") or "")
    for t in tracks:
        if t["title"] == title:
            return t["freq"], title
    return None, title


def gapless_path_active(deps, backend):
    """Is the dispatch about to be measured the GAPLESS one for this backend?

    Cast has a session object to interrogate, so the check is real. The other
    backends have none -- gapless there is a property of how the next track is
    armed (GStreamer about-to-finish, DLNA SetNextAVTransportURI), with nothing
    exposed to ask. So the check degrades to "the backend we asked for is the
    one playing", and the honest verdict comes from the audio instead. That
    asymmetry is reported in the arm summary rather than smoothed over, because
    a weaker check that looks identical to a strong one is how a measurement
    overstates itself.
    """
    if backend == "chromecast":
        return flow_is_active(deps)
    act = deps._req("/admin/output/active") or {}
    return act.get("backend_type") == backend


def flow_is_active(deps):
    """True when the Cast gapless (flow) session owns playback.

    The flag lives under ``output_session``, NOT at the top level of
    /api/now-playing. Read from the top level it is always falsey, which would
    make every skip in a run refuse as "not flow" and look like a rig fault.
    """
    np = deps.now_playing() or {}
    sess = np.get("output_session") or {}
    return bool(sess.get("gapless_flow_active"))


# ---- one skip --------------------------------------------------------------

def one_skip(deps, cap, tracks, watch_s, pre_roll_s=2.0):
    """Dispatch one skip and measure when the destination tone becomes audible."""
    backend = deps._backend_under_test
    if not gapless_path_active(deps, backend):
        return {"skip_to_audible_ms": NOT_FLOW,
                "note": "the gapless path for %s was not active" % backend}

    pos = deps.position() or {}
    if not (pos.get("is_playing") and not pos.get("is_paused")):
        raise ProbeRefusal("not playing at dispatch")

    t_dispatch = time.monotonic() * 1000.0
    deps._req("/admin/playback/skip", {})
    dest_tone, dest_title = tone_for_now_playing(deps, tracks)
    if dest_tone is None:
        return {"skip_to_audible_ms": AMBIGUOUS,
                "note": "now-playing reported %r, which is not a marker" % dest_title}

    # Let the watch window fill before analysing. Nothing is read from the
    # capture until it exists, so this is a plain wait rather than a poll.
    time.sleep(watch_s)

    cap.refresh_marks()
    start_byte = cap.byte_at_time(t_dispatch - pre_roll_s * 1000.0)
    end_byte = cap.byte_at_time(t_dispatch + watch_s * 1000.0,
                                clamp_to_end=True)
    if start_byte is None or end_byte is None or end_byte <= start_byte:
        raise ProbeRefusal("capture does not cover the dispatch window")

    base, raw = cap.read_range(start_byte, end_byte)
    samples = decimate_mono(raw)
    labels = label_windows(samples)
    runs = sustained_runs(labels)
    # Shift sample indices into whole-capture byte offsets before timing them.
    shifted = [(tone, s + base // (DECIMATE * BYTES_PER_FRAME),
                e + base // (DECIMATE * BYTES_PER_FRAME)) for tone, s, e in runs]

    origin = None
    for tone, s, _e in shifted:
        t = mark_time_ms(s * DECIMATE * BYTES_PER_FRAME, cap.marks)
        if t < t_dispatch:
            origin = tone
    if origin is None:
        return {"skip_to_audible_ms": AMBIGUOUS,
                "dest_tone": dest_tone,
                "note": "no tone was identifiable before dispatch - the line "
                        "may be silent or the markers may not be playing"}
    if origin == dest_tone:
        return {"skip_to_audible_ms": AMBIGUOUS,
                "dest_tone": dest_tone, "origin_tone": origin,
                "note": "origin and destination share a tone - the queue cycle "
                        "is wrong, not the measurement"}

    value, detail = skip_to_audible_ms(shifted, cap.marks, t_dispatch,
                                       origin, dest_tone)
    out = {"skip_to_audible_ms": (round(value, 1) if value >= 0 else value),
           "origin_tone": origin, "dest_tone": dest_tone,
           "dest_title": dest_title,
           "pos_at_dispatch_ms": pos.get("position_ms"),
           "runs": [(t, round(mark_time_ms(s * DECIMATE * BYTES_PER_FRAME,
                                           cap.marks) - t_dispatch, 1))
                    for t, s, _e in shifted],
           }
    out.update(detail)
    return out



def one_seek(deps, cap, tracks, watch_s, pre_roll_s=2.0):
    """Dispatch a seek from the front half of a marker into its back half.

    Seek is the sibling nobody measures. On the Cast gapless path it rides the
    SAME stitcher reposition as skip, so if skip waits out the device buffer,
    so does seek -- but that was inference until this existed.
    """
    backend = deps._backend_under_test
    if not gapless_path_active(deps, backend):
        return {"seek_to_audible_ms": NOT_FLOW,
                "note": "the gapless path for %s was not active" % backend}
    pos = deps.position() or {}
    dur = pos.get("duration_ms") or 0
    cur = pos.get("position_ms") or 0
    if dur <= 0:
        raise ProbeRefusal("no duration reported - cannot aim a seek")
    half = dur // 2
    if cur >= half - 3000:
        raise ProbeRefusal(
            "already at or past the halfway point (%d of %d ms) - a seek into "
            "the back half would not change the tone" % (cur, dur))
    origin, _title = tone_for_now_playing(deps, tracks)
    target_ms = half + min(8000, max(2000, (dur - half) // 4))

    t_dispatch = time.monotonic() * 1000.0
    deps._req("/admin/playback/seek", {"position_ms": int(target_ms)})
    time.sleep(watch_s)

    cap.refresh_marks()
    start_byte = cap.byte_at_time(t_dispatch - pre_roll_s * 1000.0)
    end_byte = cap.byte_at_time(t_dispatch + watch_s * 1000.0, clamp_to_end=True)
    if start_byte is None or end_byte is None or end_byte <= start_byte:
        raise ProbeRefusal("capture does not cover the seek window")
    base, raw = cap.read_range(start_byte, end_byte)
    shift = base // (DECIMATE * BYTES_PER_FRAME)
    runs = [(tone, s + shift, e + shift) for tone, s, e in
            sustained_runs(label_windows(decimate_mono(raw)))]

    value, detail = skip_to_audible_ms(runs, cap.marks, t_dispatch,
                                      origin if origin != SECOND_HALF_TONE
                                      else FIRST_HALF_TONES[0],
                                      SECOND_HALF_TONE)
    out = {"seek_to_audible_ms": (round(value, 1) if value >= 0 else value),
           "origin_tone": origin, "target_ms": int(target_ms),
           "pos_at_dispatch_ms": cur, "duration_ms": dur}
    out.update(detail)
    return out

def one_natural_boundary(deps, cap, tracks, watch_s, approach_s=14.0,
                         pre_roll_s=2.0):
    """Measure an UNDISTURBED track join: no skip, no seek, nothing dispatched.

    This replaces a seek-to-near-the-end method that was measuring its own
    interference. On DLNA the next track is armed with a one-shot
    SetNextAVTransportURI right after PLAYING, and a seek re-issues the
    transport URI -- so seeking 6 s before the end destroys the arming and the
    join is then a cold start. It measured 7.9 s that way, which says nothing
    about DLNA's gapless and everything about the probe.

    So: wait for the track to approach its own end, then just listen. Valid for
    every backend because it dispatches nothing at all.
    """
    backend = deps._backend_under_test
    if not gapless_path_active(deps, backend):
        return {"boundary_gap_ms": NOT_FLOW,
                "note": "the gapless path for %s was not active" % backend}

    origin_tone, origin_title = tone_for_now_playing(deps, tracks)
    if origin_tone is None:
        return {"boundary_gap_ms": AMBIGUOUS,
                "note": "now-playing reported %r, which is not a marker"
                        % origin_title}

    # Wait until the track is within approach_s of its end, polling only the
    # read-only position endpoint.
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        pos = deps.position() or {}
        dur = pos.get("duration_ms") or 0
        cur = pos.get("position_ms") or 0
        if dur > 0 and cur >= dur - approach_s * 1000:
            break
        if not (pos.get("is_playing") and not pos.get("is_paused")):
            raise ProbeRefusal("playback stopped while waiting for a boundary")
        time.sleep(2.0)
    else:
        raise ProbeRefusal("no track approached its end within 600 s")

    t_mark = time.monotonic() * 1000.0
    time.sleep(approach_s + watch_s)

    cap.refresh_marks()
    start_byte = cap.byte_at_time(t_mark - pre_roll_s * 1000.0)
    end_byte = cap.byte_at_time(t_mark + (approach_s + watch_s) * 1000.0,
                                clamp_to_end=True)
    if start_byte is None or end_byte is None or end_byte <= start_byte:
        raise ProbeRefusal("capture does not cover the boundary window")
    base, raw = cap.read_range(start_byte, end_byte)
    shift = base // (DECIMATE * BYTES_PER_FRAME)
    runs = [(tone, s2 + shift, e2 + shift) for tone, s2, e2 in
            sustained_runs(label_windows(decimate_mono(raw)))]

    dest_tone, dest_title = tone_for_now_playing(deps, tracks)
    gap, detail = boundary_gap_ms(runs, cap.marks, SECOND_HALF_TONE,
                                  dest_tone if dest_tone else -1, t_mark)
    out = {"boundary_gap_ms": (round(gap, 1) if gap >= 0 else gap),
           "origin_tone": origin_tone, "dest_tone": dest_tone,
           "dest_title": dest_title, "method": "undisturbed",
           "runs": [(t, round(mark_time_ms(s2 * DECIMATE * BYTES_PER_FRAME,
                                           cap.marks) - t_mark, 1),
                     round(mark_time_ms(e2 * DECIMATE * BYTES_PER_FRAME,
                                        cap.marks) - t_mark, 1))
                    for t, s2, e2 in runs]}
    out.update(detail)
    return out

def one_steady(deps, cap, tracks, watch_s, pre_roll_s=0.5):
    """Listen to UNDISTURBED playback and report the largest hole in it.

    This is the direct test of the cost of a shallower device buffer. Anchoring
    the pacing budget to the device means the device holds less unplayed audio,
    which is the whole point for skips and also exactly what turns a decode
    stall or a network hiccup into something audible. No skip, no seek, no
    dispatch of any kind: a steady tone should come back as one unbroken run,
    and anything else is the regression.
    """
    backend = deps._backend_under_test
    if not gapless_path_active(deps, backend):
        return {"max_hole_ms": NOT_FLOW,
                "note": "the gapless path for %s was not active" % backend}
    t0 = time.monotonic() * 1000.0
    time.sleep(watch_s)
    cap.refresh_marks()
    start_byte = cap.byte_at_time(t0 + pre_roll_s * 1000.0)
    end_byte = cap.byte_at_time(t0 + watch_s * 1000.0,
                                clamp_to_end=True)
    if start_byte is None or end_byte is None or end_byte <= start_byte:
        raise ProbeRefusal("capture does not cover the steady window")
    base, raw = cap.read_range(start_byte, end_byte)
    shift = base // (DECIMATE * BYTES_PER_FRAME)
    runs = [(tone, s + shift, e + shift) for tone, s, e in
            sustained_runs(label_windows(decimate_mono(raw)))]
    if not runs:
        return {"max_hole_ms": AMBIGUOUS,
                "note": "no marker tone identifiable at all in the window"}

    holes = []
    prev_end = None
    prev_tone = None
    for tone, s, e in runs:
        t_start = mark_time_ms(s * DECIMATE * BYTES_PER_FRAME, cap.marks)
        t_end = mark_time_ms(e * DECIMATE * BYTES_PER_FRAME, cap.marks)
        if prev_end is not None:
            # A tone CHANGE here is a real track boundary and belongs to
            # boundary mode, not to this one. Only same-tone gaps are holes.
            holes.append({"ms": round(t_start - prev_end, 1),
                          "same_tone": tone == prev_tone,
                          "at_ms": round(prev_end - t0, 1)})
        prev_end, prev_tone = t_end, tone
    same = [h["ms"] for h in holes if h["same_tone"]]
    return {"max_hole_ms": (round(max(same), 1) if same else 0.0),
            "holes": holes, "runs": len(runs),
            "window_s": watch_s}

# ---- boundary gaps: the regression guard for the skip fix ------------------
#
# Every plausible skip fix touches the stitcher that makes ordinary track
# boundaries seamless, so a skip arm on its own cannot say whether a change was
# worth making. A faster skip bought with a hole at every album transition is
# not a fix.
#
# A seek WITHIN a marker does not change its tone, so the only tone change in
# the window after a seek-to-near-the-end is the real automatic boundary. That
# makes the boundary reachable in ~10 s instead of waiting out a 150 s track.


def boundary_gap_ms(runs, marks, from_tone, to_tone, after_ms,
                    decimate=DECIMATE, bytes_per_frame=BYTES_PER_FRAME):
    """Silence between the end of ``from_tone`` and the start of ``to_tone``.

    Returns (gap_ms_or_sentinel, detail). Zero means sample-exact as far as
    this instrument can tell; the resolution floor is one window plus one hop,
    because a window straddling the join is ambiguous by construction and the
    detector refuses to label it rather than guessing which side it belongs to.
    """
    if from_tone == to_tone:
        raise ProbeRefusal("boundary origin and destination share a tone - the "
                           "marker cycle is wrong, not the measurement")

    def t_of(sample):
        return mark_time_ms(sample * decimate * bytes_per_frame, marks)

    end_of_a = None
    for tone, start, end in runs:
        try:
            t_start = t_of(start)
            t_end = t_of(end)
        except ProbeRefusal:
            continue
        if tone == from_tone and t_end >= after_ms:
            end_of_a = t_end
        elif (tone == to_tone and end_of_a is not None
                and t_start >= end_of_a - _RUN_OVERLAP_MS):
            # The tolerance is not slack, it is the detector's own geometry.
            # Analysis windows overlap by window - hop, so at a SEAMLESS join
            # the first window owned by the new tone can begin slightly before
            # the last window owned by the old one ends. Requiring
            # t_start >= end_of_a therefore rejected perfect boundaries: Cast
            # flow measured -10, -21 and -21 ms of overlap and every one came
            # back UNREACHED, i.e. the guard was hiding the success it existed
            # to confirm. Overlap beyond one window is not geometry and still
            # fails to match.
            return (max(0.0, t_start - end_of_a),
                    {"a_ended_ms": round(end_of_a, 1),
                     "b_started_ms": round(t_start, 1),
                     "overlap_ms": round(max(0.0, end_of_a - t_start), 1)})
    return (UNREACHED, {"a_ended_ms": (round(end_of_a, 1) if end_of_a else None)})


def one_boundary(deps, cap, tracks, watch_s, lead_in_s=6.0, pre_roll_s=2.0):
    """Seek to just before the end of the current marker and measure the join."""
    backend = deps._backend_under_test
    if not gapless_path_active(deps, backend):
        return {"boundary_gap_ms": NOT_FLOW,
                "note": "the gapless path for %s was not active" % backend}
    pos = deps.position() or {}
    dur = pos.get("duration_ms") or 0
    if dur <= int(lead_in_s * 1000) + 2000:
        raise ProbeRefusal("current track is too short (%s ms) to seek near "
                           "its end" % dur)

    origin_tone, origin_title = tone_for_now_playing(deps, tracks)
    if origin_tone is None:
        return {"boundary_gap_ms": AMBIGUOUS,
                "note": "now-playing reported %r, which is not a marker"
                        % origin_title}

    t_seek = time.monotonic() * 1000.0
    deps._req("/admin/playback/seek",
              {"position_ms": int(dur - lead_in_s * 1000)})
    time.sleep(watch_s)

    cap.refresh_marks()
    start_byte = cap.byte_at_time(t_seek - pre_roll_s * 1000.0)
    end_byte = cap.byte_at_time(t_seek + watch_s * 1000.0,
                                clamp_to_end=True)
    if start_byte is None or end_byte is None or end_byte <= start_byte:
        raise ProbeRefusal("capture does not cover the boundary window")

    base, raw = cap.read_range(start_byte, end_byte)
    shift = base // (DECIMATE * BYTES_PER_FRAME)
    runs = [(tone, s + shift, e + shift) for tone, s, e in
            sustained_runs(label_windows(decimate_mono(raw)))]

    dest_tone, dest_title = tone_for_now_playing(deps, tracks)
    if dest_tone is None or dest_tone == origin_tone:
        # The boundary did not happen inside the window, or the queue advanced
        # somewhere unexpected. Either way there is nothing to measure.
        return {"boundary_gap_ms": UNREACHED, "origin_tone": origin_tone,
                "note": "no marker-to-marker advance observed in the window"}

    # A join is always <second-half tone> -> <next track's first-half tone>,
    # because every marker ends on the shared second-half tone. Using the
    # now-playing tone as the origin would name the track's FRONT half, which
    # stopped sounding minutes earlier.
    gap, detail = boundary_gap_ms(runs, cap.marks, SECOND_HALF_TONE, dest_tone,
                                  t_seek)
    out = {"boundary_gap_ms": (round(gap, 1) if gap >= 0 else gap),
           "origin_tone": origin_tone, "dest_tone": dest_tone,
           "dest_title": dest_title,
           "runs": [(t, round(mark_time_ms(s2 * DECIMATE * BYTES_PER_FRAME,
                                           cap.marks) - t_seek, 1),
                     round(mark_time_ms(e2 * DECIMATE * BYTES_PER_FRAME,
                                        cap.marks) - t_seek, 1))
                    for t, s2, e2 in runs],
           "expected_from_tone": SECOND_HALF_TONE}
    out.update(detail)
    return out



def first_audible_after(cap, t_from_ms, max_window_s=45.0):
    """(ms_to_first_sustained_tone, detail) measured from ``t_from_ms``.

    Used once per arm to price the ONLY remaining lever on #68. Anchoring the
    pacing budget took skip-to-audible from ~11.9 s to ~5.2 s and then stopped
    mattering -- lead 3.0 and lead 1.0 land within 0.26 s of each other -- so
    what is left is audio the receiver already holds and will not give back.
    Discarding it means replacing the Cast media session rather than
    repositioning within it, and the cost of doing that is exactly this
    number: a fresh flow session and LOAD on an already-connected receiver,
    through to first audible sound.
    """
    cap.refresh_marks()
    # The number only means anything if the line was SILENT beforehand. An arm
    # that inherits playback from the previous arm finds a tone immediately and
    # reports a few milliseconds, which would read as "replacing the session is
    # free" -- the most expensive wrong answer available here. Measured on the
    # rig: 1.4 ms and 4.4 ms on two such arms, against 8,656 ms on the one arm
    # that genuinely started from silence.
    quiet_from = cap.byte_at_time(t_from_ms - 1500.0)
    if quiet_from is not None:
        q_base, q_raw = cap.read_range(quiet_from, cap.byte_at_time(
            t_from_ms, clamp_to_end=True) or quiet_from)
        if any(t is not None for _s, _e, t
               in label_windows(decimate_mono(q_raw))):
            return (AMBIGUOUS,
                    {"note": "audio was already playing before the cold start "
                             "- this arm cannot price a fresh session"})
    start_byte = cap.byte_at_time(t_from_ms)
    end_byte = cap.byte_at_time(t_from_ms + max_window_s * 1000.0,
                                clamp_to_end=True)
    if start_byte is None or end_byte is None or end_byte <= start_byte:
        return (UNREACHED, {"note": "capture does not cover the startup window"})
    base, raw = cap.read_range(start_byte, end_byte)
    shift = base // (DECIMATE * BYTES_PER_FRAME)
    runs = sustained_runs(label_windows(decimate_mono(raw)))
    for tone, sample, _end in runs:
        t = mark_time_ms((sample + shift) * DECIMATE * BYTES_PER_FRAME,
                         cap.marks)
        if t >= t_from_ms:
            return (t - t_from_ms, {"tone": tone})
    return (UNREACHED, {"note": "no marker tone became audible in %.0f s"
                                % max_window_s})


# ---- runner ----------------------------------------------------------------

def run(deps, args):
    marker_dir_host = args.marker_dir_host
    marker_dir_app = args.marker_dir_app
    plan = generate_markers(deps, marker_dir_host, args.tracks, args.track_seconds)
    dev_id, host, claimed_gapless = pick_protocol(
        deps, args.backend, args.device_prefix)
    # Stashed on deps so the per-event helpers can ask which gapless path they
    # are supposed to be measuring without threading it through every call.
    deps._backend_under_test = args.backend

    source_id = None
    results = []
    # Defaulted so a refusal that unwinds before the capture opens cannot turn
    # the summary into a NameError and lose the rows already collected.
    cold_ms, cold_detail = UNREACHED, {"note": "arm ended before cold start"}
    timeline = None
    try:
        reg = deps._req("/admin/sources/local",
                        {"root_dir": marker_dir_app, "name": "JP Markers"})
        source_id = reg.get("source_id")
        deps._req("/admin/sources/rescan", {})
        tracks = wait_for_markers(deps, plan)

        with RigState(deps):
            deps.set_setting("gapless_enabled", True)
            # stop, not any auto-fill mode: an auto-DJ behind the probe would
            # synthesise tracks outside the queue and the tone cycle would stop
            # meaning anything.
            deps.set_setting("queue_end_behavior", "stop")
            deps.set_output(args.backend, dev_id, host)
            time.sleep(4.0)
            with Capture(args.raw, args.cap_stderr, args.marks) as cap:
                # Timestamped before the first append, because that append is
                # what creates the flow session and issues the LOAD.
                t_cold = time.monotonic() * 1000.0
                arrange_queue(deps, tracks)
                try:
                    deps._req("/admin/playback/resume", {})
                except ProbeRefusal:
                    pass
                time.sleep(1.0)
                # Resume alone does not reliably start a freshly filled queue
                # -- there is nothing to resume. One skip pulls the front track
                # into playback. It costs a marker, which is why the plan has
                # spares.
                deps._req("/admin/playback/skip", {})
                if not _wait_playing(deps, args.settle_s + 45.0):
                    raise ProbeRefusal("playback never reached a measurable position")
                # Cast only: flow does not exist the instant playback starts,
                # and a skip dispatched before it does is a legitimate refusal
                # that still costs a sample. The other backends have no session
                # object to wait for -- see gapless_path_active.
                if args.backend == "chromecast" and not _wait_flow(deps, 60.0):
                    raise ProbeRefusal(
                        "no flow session after 60 s of playback - gapless is on "
                        "and the backend is chromecast, so this is a real "
                        "defect rather than a measurement problem")
                cold_ms, cold_detail = first_audible_after(cap, t_cold)

                for i in range(args.skips):
                    # Vary where in the track the skip lands: position at skip
                    # is a plausible driver of the lead and holding it constant
                    # would hide that.
                    time.sleep(args.settle_s + (i % 4) * 5.0)
                    try:
                        mode = args.mode
                        if mode == "mixed":
                            # One capture answers all three questions for a
                            # protocol, which matters when each arm costs a
                            # catalog crawl and a device handover.
                            mode = ("skip", "seek", "boundary")[i % 3]
                            row = {"cycle_mode": mode}
                        else:
                            row = {}
                        if mode == "boundary":
                            # Undisturbed by default. The seek-to-the-end
                            # variant perturbs DLNA's one-shot next-URI arming
                            # and measured its own interference.
                            fn = (one_boundary if args.boundary_by_seek
                                  else one_natural_boundary)
                            row.update(fn(deps, cap, tracks, args.watch_s))
                        elif mode == "steady":
                            row.update(one_steady(deps, cap, tracks,
                                                  args.watch_s))
                        elif mode == "seek":
                            row.update(one_seek(deps, cap, tracks,
                                                args.watch_s))
                        else:
                            row.update(one_skip(deps, cap, tracks,
                                                args.watch_s))
                    except ProbeRefusal as exc:
                        # Keep cycle_mode: replacing the row wholesale dropped
                        # it, and the mixed summary then reported n_refused 0
                        # next to nine refusals. A refusal that disappears from
                        # the count is the same failure as a silent zero.
                        row = {"refused": deps.redact(str(exc)),
                               "cycle_mode": row.get("cycle_mode")}
                    row["i"] = i
                    results.append(row)
                    print(json.dumps(row))
                    sys.stdout.flush()

                timeline = check_mark_timeline(cap.refresh_marks())
                err = cap.overruns()
                if "overrun" in err.lower():
                    raise ProbeRefusal(
                        "arecord reported an overrun - dropped samples break "
                        "the byte-to-time map, so every figure in this run is "
                        "suspect: " + err.strip()[-200:])
    finally:
        if source_id and not args.keep_source:
            try:
                deps._req("/admin/sources/local/%s" % source_id, method="DELETE")
            except Exception as exc:
                print("MARKER SOURCE NOT REMOVED: %s" % deps.redact(str(exc)))
        if not args.keep_raw:
            for p in (args.raw, args.cap_stderr, args.marks):
                try:
                    os.unlink(p)
                except Exception:
                    pass

    keys = {"boundary": "boundary_gap_ms", "steady": "max_hole_ms",
            "seek": "seek_to_audible_ms"}
    if args.mode == "mixed":
        summary = {"arm": args.label, "mode": "mixed",
                   "backend": args.backend,
                   "claimed_gapless": claimed_gapless}
        for sub, key in (("skip", "skip_to_audible_ms"),
                         ("seek", "seek_to_audible_ms"),
                         ("boundary", "boundary_gap_ms")):
            rows = [r for r in results if r.get("cycle_mode") == sub]
            summary[sub] = summarise([r.get(key) for r in rows],
                                     "%s/%s" % (args.label, sub),
                                     args.target_ms)
    else:
        key = keys.get(args.mode, "skip_to_audible_ms")
        summary = summarise([r.get(key) for r in results],
                            args.label, args.target_ms)
        summary["mode"] = args.mode
        summary["measured"] = key
    summary["backend"] = args.backend
    summary["claimed_gapless"] = claimed_gapless
    # Named so a reader cannot mistake the weak check for the strong one.
    summary["gapless_path_check"] = (
        "flow session interrogated" if args.backend == "chromecast"
        else "active-backend only (no session object exists for this backend)")
    # The price of the only remaining lever, recorded on every arm so it gets a
    # distribution rather than a single reading.
    summary["cold_start_to_audible_ms"] = (round(cold_ms, 1) if cold_ms >= 0
                                           else cold_ms)
    summary["cold_start_detail"] = cold_detail
    summary["timeline"] = timeline
    summary["accuracy_note"] = (
        "window %d + hop %d samples at %d Hz = %.0f ms, plus up to %.0f ms of "
        "ALSA buffer; all of it biased toward overstating the latency"
        % (WINDOW, HOP, ANALYSIS_RATE, (WINDOW + HOP) * 1000.0 / ANALYSIS_RATE,
           CAP_BUFFER * 1000.0 / CAP_RATE))
    print("ACOUSTIC RESULT " + json.dumps(summary))
    return summary


def _wait_flow(deps, timeout_s):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if flow_is_active(deps):
            return True
        time.sleep(1.0)
    return False


def _wait_playing(deps, timeout_s):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        p = deps.position() or {}
        if (p.get("is_playing") and not p.get("is_paused")
                and (p.get("position_ms") or 0) > 2000):
            return True
        time.sleep(1.0)
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--backend", default=os.environ.get("ARM_BACKEND", "chromecast"),
                    choices=("chromecast", "dlna", "airplay", "direct"),
                    help="which output protocol to route through for this arm. "
                         "Gapless is implemented differently per backend "
                         "(Cast: server-stitched flow; Direct: GStreamer "
                         "about-to-finish; DLNA: SetNextAVTransportURI; "
                         "AirPlay: per-track, i.e. none), so the numbers are "
                         "not comparable across backends without saying which.")
    ap.add_argument("--device-prefix", default=os.environ.get("ARM_DEVICE", "WiiM Pro"),
                    help="device name prefix to route to")
    ap.add_argument("--mode", choices=("skip", "seek", "boundary", "steady",
                                       "mixed"),
                    default="skip",
                    help="skip = dispatch-to-audible after an explicit skip. "
                         "boundary = silence at an ORDINARY automatic track "
                         "join, the regression guard: every plausible skip fix "
                         "touches the stitcher that makes those seamless.")
    ap.add_argument("--skips", type=int, default=10,
                    help="iterations (skips, or boundaries in boundary mode)")
    ap.add_argument("--tracks", type=int, default=16)
    ap.add_argument("--track-seconds", type=int, default=150)
    ap.add_argument("--settle-s", type=float, default=12.0)
    ap.add_argument("--watch-s", type=float, default=30.0)
    ap.add_argument("--target-ms", type=float, default=2500.0,
                    help="pass bar for the median. 2500 for skip mode (the "
                         "user's stated target); pass something like 150 in "
                         "boundary mode, just above the detector floor of one "
                         "window plus one hop.")
    ap.add_argument("--label", default=os.environ.get("ARM_LABEL", "?"))
    ap.add_argument("--marker-dir-host",
                    default=os.environ.get("JP_MARKER_DIR_HOST", "/tmp/jp-markers"))
    ap.add_argument("--marker-dir-app",
                    default=os.environ.get("JP_MARKER_DIR_APP", "/tmp/jp-markers"))
    ap.add_argument("--raw", default="/tmp/jp-skip-capture.raw")
    ap.add_argument("--cap-stderr", default="/tmp/jp-skip-capture.err")
    ap.add_argument("--boundary-by-seek", action="store_true",
                    help="reach the track join by seeking near the end instead "
                         "of waiting for it. Faster, and INVALID on DLNA: a "
                         "seek re-issues the transport URI and destroys the "
                         "one-shot SetNext arming, so the join becomes a cold "
                         "start (measured 7.9 s that way).")
    ap.add_argument("--keep-raw", action="store_true")
    ap.add_argument("--keep-source", action="store_true",
                    help="leave the marker local-files source registered on "
                         "exit. The catalog crawl that makes the markers "
                         "visible costs minutes, so an iterative sweep of "
                         "arms should pay it once -- but the default removes "
                         "it, because a source left behind is persistent "
                         "state with no PID to sweep for.")
    ap.add_argument("--capture-helper", action="store_true",
                    help="internal: run ONLY the capture loop (see Capture)")
    ap.add_argument("--marks", default="/tmp/jp-skip-capture.marks")
    args = ap.parse_args(argv)

    if args.capture_helper:
        return capture_helper(args.raw, args.marks, args.cap_stderr)

    deps = Deps(base=os.environ.get("JP_BASE", ""))
    try:
        deps.login()
        run(deps, args)
    except ProbeRefusal as exc:
        print("REFUSED: " + deps.redact(str(exc)), file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
