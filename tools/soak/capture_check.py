"""Prove the analogue loopback records while playback is live.

Runs ON the rig (it needs `arecord` and `ffmpeg`). Everything comes from the
environment — no host, no device index, no credential is committed here.

Why this exists as its own check rather than a line in a runbook: an acoustic
soak rests entirely on the assumption that a capture can run at the same time as
playback. ALSA `hw:` is single-consumer per direction, and nothing had ever
proven the two directions coexist on this interface. A 12-hour run is an
expensive place to discover they don't.

It is also the positive control for the acoustic arm: it proves the capture path
can see audio at all. It does NOT prove the detector can say *no* — a silent
capture is equally consistent with a wrong backend, an unplugged cable, and a
real defect. Separating those needs the device's own session state, not dB.
See docs/solutions/developer-experience/2026-09-05-acoustic-arm-must-assert-the-session.md

Measured on this hardware 2026-09-23:
  silence floor        mean -90.3 dB   max -76.3 dB
  live Direct playback mean -22.0 dB   max  -7.6 dB

Usage:
  JP_ALSA_CAPTURE=default python3 capture_check.py          # assert audio present
  JP_ALSA_CAPTURE=default python3 capture_check.py --floor  # measure the silence floor
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile

# The interface accepts ONE format. Asking for the obvious S16_LE fails with
# "Sample format non available", which reads like a broken device rather than a
# wrong argument.
FORMAT = os.environ.get("JP_ALSA_FORMAT", "S24_3LE")
RATE = os.environ.get("JP_ALSA_RATE", "48000")
CHANNELS = os.environ.get("JP_ALSA_CHANNELS", "2")
# "default" resolves through the host's asound.conf. Override per rig; never
# hard-code a card index — USB indexes shift across reboots and the soak's
# recovery loop restarts things.
DEVICE = os.environ.get("JP_ALSA_CAPTURE", "default")
# A capture whose mean sits above this is carrying audio. Chosen to sit far from
# both measured points above, so it cannot be tripped by a slightly noisy floor
# and cannot be missed by a quiet passage.
THRESHOLD_DB = float(os.environ.get("JP_AUDIO_THRESHOLD_DB", "-70"))

_LEVEL = re.compile(r"(mean|max)_volume:\s*(-?\d+(?:\.\d+)?) dB")


def capture(seconds: float, path: str) -> None:
    # arecord's -d takes whole seconds; "5.0" is rejected as an invalid duration.
    subprocess.run(
        ["arecord", "-D", DEVICE, "-f", FORMAT, "-c", CHANNELS, "-r", RATE,
         "-d", str(max(1, round(seconds))), path],
        check=True, capture_output=True,
    )


def levels(path: str) -> dict:
    """mean/max dBFS via ffmpeg volumedetect. Values are negative; 0 is full scale."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", path,
         "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    return {k: float(v) for k, v in _LEVEL.findall(proc.stderr)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--floor", action="store_true",
                    help="measure the silence floor instead of asserting audio")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as d:
        wav = os.path.join(d, "capture.wav")
        try:
            capture(args.seconds, wav)
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or b"").decode(errors="replace").strip()
            # Report the device error rather than returning a silent buffer that
            # downstream analysis would read as a dropout.
            print(f"CAPTURE FAILED on device {DEVICE!r}: {err}", file=sys.stderr)
            return 2
        lv = levels(wav)

    if not lv:
        print("could not read levels from the capture", file=sys.stderr)
        return 2

    mean, mx = lv.get("mean", 0.0), lv.get("max", 0.0)
    print(f"device={DEVICE} format={FORMAT} rate={RATE} "
          f"mean={mean:.1f} dB max={mx:.1f} dB")

    if args.floor:
        print(f"FLOOR: mean {mean:.1f} dB, max {mx:.1f} dB")
        return 0

    if mean > THRESHOLD_DB:
        print(f"AUDIO PRESENT (mean {mean:.1f} dB > {THRESHOLD_DB:.0f} dB)")
        return 0
    print(f"SILENT (mean {mean:.1f} dB <= {THRESHOLD_DB:.0f} dB). This alone does "
          f"NOT mean playback is broken — confirm which backend owns the device "
          f"and whether that device reports an active session.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
