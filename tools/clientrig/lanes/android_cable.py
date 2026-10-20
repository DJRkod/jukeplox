"""The Android cable lane: the rig's highest-authority measurement.

Why this lane leads
-------------------
It is the only one that measures the responsiveness definition literally. Input
is injected by the operating system above the browser; frame times come from the
display pipeline. Every desktop lane approximates at least one of those, and the
project has a measured 5,939x gap between desktop Chrome and mobile Chrome on a
single defect -- so a desktop green says nothing about a phone.

That is also why this lane is the final authority on any speed claim about a
phone, and why a desktop lane may suggest a cause but never close a speed
report.

What it cannot see
------------------
Declared up front and carried as data on every result, not buried in prose:
anything about iOS, anything about another manufacturer's Android, and anything
about a build other than the one on the device.

Credentialing needs the hardware
--------------------------------
This lane enters service only once it has been observed failing against the
recorded 40-pixel rail-jump defect and passing against the clean build, with
both observations and its sensitivity floor stored. Until then the honesty core
refuses to let it report anything -- an uncredentialed lane is not a quiet lane,
it is an unproven one.

The navigation precondition that is easy to miss
------------------------------------------------
Below 960 CSS pixels the guest page opens on the Now tab, so nothing
browse-related exists until Albums is tapped. A lane that skips that step
measures an empty surface and reports no rail -- which looks like a fast rail.
"""
from __future__ import annotations

LANE_NAME = "android-cable"

# The classes this lane is the authority for.
OWNS_CLASSES = (
    "jump-landing",      # report #1, and the recorded credential defect
    "input-stall",
    "layout-thrash",
)

# What it structurally cannot observe. A lane may not own a class it is blind
# to, and the honesty core enforces that.
BLIND_CLASSES = (
    "ios-behaviour",       # no Apple hardware is involved at any point
    "other-oem-android",   # one device profile, stamped on every result
    "cross-build",         # measures the build installed, nothing else
)

# The mobile breakpoint in the guest page. Below this the page opens on Now.
DESKTOP_BREAKPOINT_PX = 960


def credential_template(recorded_defect: str = "rail-jump-40px") -> dict:
    """An explicitly uncredentialed credential for this lane.

    Returned as a plain mapping so the caller builds the real `Credential` with
    the honesty core's own type. `observed_failing` and `observed_passing_clean`
    start false, which is what makes the lane unable to report until the device
    has actually been driven.
    """
    return {
        "lane": LANE_NAME,
        "blind_classes": BLIND_CLASSES,
        "owns_classes": OWNS_CLASSES,
        "sensitivity_floor_ms": None,
        "recorded_defect": recorded_defect,
        "observed_failing": False,
        "observed_passing_clean": False,
        "reissued_reason": None,
        "reissued_date": None,
    }


def navigation_plan(viewport_width_px: int) -> list:
    """The steps needed before anything browse-related exists on screen.

    Below the desktop breakpoint the guest page opens on Now, so Albums must be
    tapped first. Skipping this measures an empty surface, and "no rail" is
    indistinguishable from "a very fast rail" in the result.
    """
    steps = ["wait-for-page-ready"]
    if viewport_width_px < DESKTOP_BREAKPOINT_PX:
        steps.append("tap-albums-tab")
    steps.append("wait-for-rail-visible")
    return steps


def preflight(devices: list, serial: str | None, device_check) -> tuple:
    """`(None, "")` when the lane may run, else a sentinel and the reason.

    Takes `device_check` injected rather than importing it, so this stays
    provable without the adb module loaded -- the same seam the soak harness
    uses for its policy functions.
    """
    code, why = device_check(devices, serial=serial)
    if code is not None:
        return (code, f"{LANE_NAME} cannot run: {why}")
    return (None, "")
