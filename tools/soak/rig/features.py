"""Turn captured audio segments into a compact feature timeline.

Runs ON the rig, next to the segments. Emits one JSON object per segment plus
one per silent interval, with ABSOLUTE timestamps, so later analysis joins
against the state timeline without ever re-reading gigabytes of audio.

Thresholds are the ones already proven on this rig rather than invented ones:
`silencedetect=noise=-55dB:d=0.4`. Measured previously, an unarmed fallback
boundary showed 9.2 s of silence and an armed gapless boundary showed zero
silence at or above 0.4 s
(docs/solutions/workflow-issues/autonomous-hardware-validation-loopback-rig.md).

The -55 dB figure sits deliberately between the two measured points on this
hardware — a -90.3 dB silence floor and roughly -21 dB of live playback — so it
is not a guess and not a knife-edge.

Segment start times come from the filename, which `record.sh` writes with
`-strftime`. They are LOCAL time, converted here on the same host that wrote
them, so the conversion is correct whatever the machine's zone.
"""
import argparse
import glob
import io
import json
import os
import re
import subprocess
import sys
import time

NOISE_DB = os.environ.get("JP_SILENCE_NOISE_DB", "-55")
MIN_SILENCE_S = os.environ.get("JP_SILENCE_MIN_S", "0.4")

_NAME = re.compile(r"cap-(\d{8})-(\d{6})\.(?:flac|wav)$")
_START = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_END = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")
_LEVEL = re.compile(r"(mean|max)_volume:\s*(-?\d+(?:\.\d+)?) dB")
_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def segment_start_ms(name: str, mktime=time.mktime) -> int | None:
    """Epoch ms for a segment filename, or None when it does not match.

    `mktime` is injectable so the conversion can be tested without depending on
    the test machine's timezone.
    """
    m = _NAME.search(os.path.basename(name))
    if not m:
        return None
    stamp = time.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    return int(mktime(stamp) * 1000)


def parse_silences(text: str, segment_duration_s: float | None = None) -> list[dict]:
    """Silent intervals from ffmpeg silencedetect stderr, in segment-relative seconds.

    A silence that runs to the end of the segment has a `silence_start` with no
    matching `silence_end`. That is not malformed output — it is the common case
    at a boundary that straddles a segment edge — so it is closed at the segment
    duration when one is known, and reported as open-ended otherwise. Dropping it
    would lose exactly the silences most likely to matter.
    """
    starts = [float(x) for x in _START.findall(text)]
    ends = [float(x) for x in _END.findall(text)]
    out: list[dict] = []
    for i, s in enumerate(starts):
        if i < len(ends):
            e = ends[i]
            out.append({"start_s": s, "end_s": e, "dur_s": round(e - s, 3),
                        "open_ended": False})
        elif segment_duration_s is not None:
            out.append({"start_s": s, "end_s": segment_duration_s,
                        "dur_s": round(segment_duration_s - s, 3),
                        "open_ended": True})
        else:
            out.append({"start_s": s, "end_s": None, "dur_s": None,
                        "open_ended": True})
    return out


def parse_levels(text: str) -> dict:
    return {k: float(v) for k, v in _LEVEL.findall(text)}


def parse_channel_rms(text: str) -> list[float]:
    """Per-channel RMS in dB, in channel order, from ffmpeg astats output.

    Whole-mix level cannot see one channel dying: a stereo mix with the right
    channel silent still reads as perfectly healthy audio. This is what makes
    "a channel dropped" detectable at all.
    """
    out: list[float] = []
    current: float | None = None
    for line in text.splitlines():
        if "Channel:" in line:
            if current is not None:
                out.append(current)
            current = None
        elif "RMS level dB:" in line and current is None:
            try:
                current = float(line.split("RMS level dB:")[1].strip())
            except ValueError:
                current = None
    if current is not None:
        out.append(current)
    return out


def parse_duration_s(text: str) -> float | None:
    m = _DURATION.search(text)
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def analyse(path: str) -> dict:
    """Run ffmpeg once over a segment and return its parsed features."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", path,
         "-af", f"silencedetect=noise={NOISE_DB}dB:d={MIN_SILENCE_S},"
                f"volumedetect,astats=measure_perchannel=RMS_level:measure_overall=none",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    dur = parse_duration_s(proc.stderr)
    sils = parse_silences(proc.stderr, dur)
    for sl in sils:
        sl["level_db"] = _level_in(path, sl["start_s"], sl.get("dur_s"))
    return {
        "duration_s": dur,
        "levels": parse_levels(proc.stderr),
        "channel_rms": parse_channel_rms(proc.stderr),
        "silences": sils,
    }


def _level_in(path: str, start_s: float, dur_s: float | None) -> float | None:
    """Mean level inside one interval, so quiet audio can be told from silence."""
    if not dur_s or dur_s <= 0:
        return None
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-ss", str(start_s),
         "-t", str(dur_s), "-i", path, "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    lv = parse_levels(p.stderr)
    return lv.get("mean")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cap_dir")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    out_path = args.out or os.path.join(args.cap_dir, "features.jsonl")
    segs = sorted(glob.glob(os.path.join(args.cap_dir, "cap-*.flac")) +
                  glob.glob(os.path.join(args.cap_dir, "cap-*.wav")))
    if not segs:
        print(f"no segments in {args.cap_dir}", file=sys.stderr)
        return 2

    n_sil = 0
    with io.open(out_path, "w", encoding="utf-8") as fh:
        for seg in segs:
            t0 = segment_start_ms(seg)
            f = analyse(seg)
            fh.write(json.dumps({
                "kind": "segment", "file": os.path.basename(seg),
                "t0": t0, "duration_s": f["duration_s"], "levels": f["levels"],
                "channel_rms": f["channel_rms"],
            }) + "\n")
            for s in f["silences"]:
                n_sil += 1
                fh.write(json.dumps({
                    "kind": "silence", "file": os.path.basename(seg),
                    # Absolute epoch ms is what the join needs; segment-relative
                    # seconds are kept so a finding can be located inside the file.
                    "t_start": (t0 + int(s["start_s"] * 1000)) if t0 else None,
                    "t_end": (t0 + int(s["end_s"] * 1000))
                             if (t0 and s["end_s"] is not None) else None,
                    "start_s": s["start_s"], "dur_s": s["dur_s"],
                    "open_ended": s["open_ended"],
                    # The measured level INSIDE the silence. silencedetect
                    # triggers below -55 dB, which real music dips under while
                    # still sounding; only a level at the noise floor is true
                    # silence. Without this a 12-hour run over a real library
                    # produced 17 false positives for every genuine one.
                    "level_db": s.get("level_db"),
                }) + "\n")
    print(f"{len(segs)} segment(s), {n_sil} silent interval(s) -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
