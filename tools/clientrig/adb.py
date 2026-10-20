"""Device access for the Android cable lane: input the OS injects, frames the display presented.

This is the only part of the rig that measures the responsiveness definition
literally. Everything on the desktop side approximates at least one half of it:

  * **Input** comes from `adb shell input tap`, which goes through Android's
    InputManager -- above the browser entirely. A scripted click inside the page
    is not a weaker version of this; by specification it produces no interaction
    timing entry at all, and it skips hit testing and queueing behind a busy main
    thread. Switching to real input is what made a 48-second frame appear at all
    in the October investigation.
  * **Frames** come from `dumpsys SurfaceFlinger --latency`, which reports when
    the compositor actually presented each frame to the display. In-page
    measurement can tell you when script finished and when a paint was
    requested; only this says when the screen changed.

Parsing is where the care goes
------------------------------
SurfaceFlinger marks frames it has no data for with the maximum 64-bit integer,
and sometimes with zeros. Read either as a timestamp and you get a duration of
roughly 292 years, or a frame presented at the epoch. Averaged into a settle
time, both produce a confident wrong answer that looks extremely stable -- which
is the exact failure shape this project has already paid for. Unusable rows are
discarded and the reason is reported; an unusable dump yields a sentinel, never
a zero.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Sentinels are shared with the measurement contract rather than redefined, so a
# reading that did not happen is distinguishable the same way everywhere.
UNREACHED = -1
NO_DEVICE = -2
NO_READING = -3

# What this lane reports as its input kind. The honesty core accepts it because
# the operating system, not the page, delivers the event.
INPUT_KIND = "os-injected-tap"

_INT64_MAX = 9223372036854775807

# Device states adb reports that mean "attached but not usable", with the action
# that fixes each one -- a bare "no device" sends the operator looking at cables
# when the real problem is an unaccepted prompt on screen.
_UNUSABLE_STATES = {
    "unauthorized": ("accept the USB debugging prompt on the device screen; "
                     "adb is connected but not authorised"),
    "offline": ("device reports offline -- unplug and replug, or restart adb; "
                "a sleeping device also reports this"),
    "no permissions": "adb lacks permission to claim the device",
    "bootloader": "device is in the bootloader, not booted into Android",
    "recovery": "device is in recovery, not booted into Android",
    "sideload": "device is in sideload mode",
    "unknown": "device state is unknown to adb",
}


@dataclass(frozen=True)
class SfFrame:
    desired_ns: int
    present_ns: int
    ready_ns: int


@dataclass(frozen=True)
class SfLatency:
    """A parsed `dumpsys SurfaceFlinger --latency` dump.

    `reason` is empty when the dump was usable, and carries why not otherwise --
    the same None-or-reason shape the rest of the rig uses, so an unusable dump
    cannot be mistaken for a quiet screen.
    """

    refresh_period_ns: int = 0
    frames: tuple = ()
    reason: str = ""

    @property
    def present_ns(self) -> list:
        return [f.present_ns for f in self.frames]


def parse_sf_latency(text: str) -> SfLatency:
    """Parse a latency dump, discarding rows that carry no real timestamp."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return SfLatency(reason="empty dump -- no output from dumpsys")

    try:
        refresh = int(lines[0])
    except ValueError:
        return SfLatency(
            reason=(f"first line is not a refresh period: {lines[0][:60]!r}. "
                    f"This is not a SurfaceFlinger latency dump"))
    if refresh <= 0:
        return SfLatency(
            reason=f"refresh period reported as {refresh}, which cannot be used")

    frames, pending, malformed = [], 0, 0
    for ln in lines[1:]:
        parts = ln.split()
        if len(parts) != 3:
            malformed += 1
            continue
        try:
            desired, present, ready = (int(p) for p in parts)
        except ValueError:
            malformed += 1
            continue
        if _INT64_MAX in (desired, present, ready) or present <= 0:
            pending += 1
            continue
        frames.append(SfFrame(desired, present, ready))

    if not frames:
        return SfLatency(
            refresh_period_ns=refresh,
            reason=(f"no usable frames in the dump: {pending} pending "
                    f"(marked with INT64_MAX or zero) and {malformed} "
                    f"malformed row(s). A pending row read as a timestamp "
                    f"would be a duration of roughly 292 years"))

    frames.sort(key=lambda f: f.present_ns)
    return SfLatency(refresh_period_ns=refresh, frames=tuple(frames))


def present_intervals_ms(latency: SfLatency) -> list:
    """Gaps between consecutive presented frames, in milliseconds.

    A single frame has no interval, and the empty list says so rather than
    implying a gap of zero.
    """
    ns = latency.present_ns
    return [(b - a) / 1e6 for a, b in zip(ns, ns[1:])]


def quiescence_ms(latency: SfLatency, input_ns: int,
                  idle_gap_frames: int = 3) -> float:
    """Milliseconds from an input event until the compositor stopped presenting.

    "Stopped" means a gap of at least `idle_gap_frames` refresh periods between
    presented frames. Returns the last frame before that gap.

    Two things deliberately do not return a number:

    * **No frame after the input** -- `NO_READING`. Either the dump predates the
      interaction or nothing was presented, and zero would read as instant.
    * **No gap at all** -- `UNREACHED`. The screen was still painting when the
      dump ended, so it has not settled. Reporting the last frame would
      understate the settle time by however much came after the dump.
    """
    if latency.reason or not latency.frames:
        return NO_READING

    after = [p for p in latency.present_ns if p >= input_ns]
    if not after:
        return NO_READING

    threshold = idle_gap_frames * latency.refresh_period_ns
    for a, b in zip(after, after[1:]):
        if b - a >= threshold:
            return (a - input_ns) / 1e6
    return UNREACHED


def parse_devices(text: str) -> list:
    """`adb devices` output as a list of `(serial, state)`."""
    out = []
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln or ln.lower().startswith("list of devices"):
            continue
        if ln.startswith("*"):               # adb daemon chatter
            continue
        parts = ln.split()
        if len(parts) >= 2:
            out.append((parts[0], " ".join(parts[1:])))
    return out


def device_check(devices: list, serial: str | None = None):
    """`(None, "")` when exactly one usable device is addressable, else why not.

    Returning a sentinel plus a reason rather than raising keeps this usable as
    a lane precondition: a missing phone is a recorded "no reading", not a
    crash, and not a pass.
    """
    if not devices:
        return (NO_DEVICE,
                "no device attached -- connect the phone over USB and confirm "
                "with `adb devices`")

    if serial:
        match = [(s, st) for s, st in devices if s == serial]
        if not match:
            attached = ", ".join(s for s, _ in devices) or "none"
            return (NO_DEVICE,
                    f"requested serial {serial} is not attached "
                    f"(attached: {attached})")
        s, state = match[0]
        if state != "device":
            return (NO_DEVICE,
                    f"{s} is in state {state!r}: "
                    f"{_UNUSABLE_STATES.get(state, 'not usable')}")
        return (None, "")

    usable = [(s, st) for s, st in devices if st == "device"]
    if not usable:
        s, state = devices[0]
        return (NO_DEVICE,
                f"{s} is in state {state!r}: "
                f"{_UNUSABLE_STATES.get(state, 'not usable')}")
    if len(usable) > 1:
        names = ", ".join(s for s, _ in usable)
        return (NO_DEVICE,
                f"{len(usable)} usable devices attached ({names}) -- pass an "
                f"explicit serial. Guessing would attribute the result to the "
                f"wrong hardware, and every result is stamped with its device "
                f"profile")
    return (None, "")


def _base_argv(serial: str) -> list:
    return ["adb", "-s", serial, "shell", "input"]


def tap_argv(serial: str, x: int, y: int) -> list:
    """A real tap, injected by the OS through InputManager."""
    return _base_argv(serial) + ["tap", str(int(x)), str(int(y))]


def swipe_argv(serial: str, x1: int, y1: int, x2: int, y2: int,
               duration_ms: int) -> list:
    """A swipe with an explicit duration, so the gesture is frame-paced.

    Duration matters: an instantaneous swipe is a fling, which exercises
    different gesture arbitration than a controlled drag.
    """
    return _base_argv(serial) + [
        "swipe", str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)),
        str(int(duration_ms))]


def sf_latency_argv(serial: str, layer: str) -> list:
    """The dump that carries real presentation timestamps."""
    return ["adb", "-s", serial, "shell", "dumpsys", "SurfaceFlinger",
            "--latency", layer]


def gfxinfo_argv(serial: str, package: str) -> list:
    """Per-frame stats from the app's own render pipeline, as a cross-check.

    Kept alongside the SurfaceFlinger dump rather than instead of it: when two
    instruments disagree about whether a frame was presented, that disagreement
    is itself the finding.
    """
    return ["adb", "-s", serial, "shell", "dumpsys", "gfxinfo", package,
            "framestats"]


def forward_argv(serial: str, port: int) -> list:
    """Forward a local port to the browser's debugging socket on the device."""
    return ["adb", "-s", serial, "forward", f"tcp:{int(port)}",
            "localabstract:chrome_devtools_remote"]


def reverse_argv(serial: str, port: int) -> list:
    """Make a local server reachable from the device.

    This is how the phone reaches the rig's canary-delivery server without the
    port being exposed on the network.
    """
    return ["adb", "-s", serial, "reverse", f"tcp:{int(port)}",
            f"tcp:{int(port)}"]
