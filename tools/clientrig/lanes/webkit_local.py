"""The WebKit lane: a correctness oracle that refuses to be read as Safari.

This lane exists because the project has no Apple hardware and never will in
this plan. It runs the real WebKit engine, which makes it genuinely useful for
layout, cascade and DOM behaviour. It is not Safari, and the gap is not a
caveat -- it is the lane's defining property, carried as data on every result.

What the probe actually found (2026-10-19, webkit-2370, UA Version/27.2)
-----------------------------------------------------------------------
Every line below is measured on the shipped build, not read from documentation.

* **Scroll anchoring is ACTIVE.** Inserting 400px above the viewport moved
  `scrollTop` by exactly 400 and left the probe row at the same screen position.
  This settles the question the plan flagged: the build is Safari-27-era, so it
  has the correction that *masks* the scroll-restoration report. The lane is
  therefore declared blind to that class, and the red arm for it comes from
  disabling anchoring on shipped code in the Chromium lane instead. This is the
  failure branch the plan predicted -- reached by measurement rather than
  discovered after building on the assumption.
* **Cross-origin isolation works, and it matters more here.** Served through the
  rig's own server the measured clock step is 20 microseconds; without isolation
  WebKit clamps to whole milliseconds, which is coarser than most of what this
  rig measures.
* **Event Timing is available.** So interaction timing is observable, coarse
  (the specification rounds to 8ms) but real.
* **Long Animation Frames and Long Tasks are absent.** Chromium-only. Anything
  that decomposes a slow frame into script versus style-and-layout is simply
  not available here, so the lane cannot attribute a stall even when it can see
  that one happened.
* **Device pixel ratio cannot be trusted.** Asked for 3.0, the page reported
  1.23 -- the host display's scaling leaking through. Any geometry expressed in
  device pixels is therefore suspect, so the lane records the *observed* ratio
  and declares the mismatch rather than assuming the requested value took.
* **`window.safari` and `GestureEvent` are injected stubs.** Code that
  feature-detects either takes the iOS branch while nothing behind it works.
  Worth knowing before trusting any UA-conditional path.

The WSL escape hatch: registered, not usable as-is
--------------------------------------------------
Playwright does ship an undocumented `webkit-wsl` channel that runs the Linux
build, where async scrolling and compositing are compiled in. Launching it here
got as far as invoking `wsl.exe -d playwright`, which means the channel is real
and wired up -- it then failed because that distribution does not exist. Making
it work means provisioning a 10GB Ubuntu WSL image. Recorded as available-with-
provisioning rather than unavailable, because the distinction matters if anyone
later needs scroll behaviour from a WebKit engine.
"""
from __future__ import annotations

LANE_NAME = "webkit-local"

# Correctness, not responsiveness. Layout, cascade and DOM behaviour are real
# WebKit here; nothing about timing on a phone is.
OWNS_CLASSES = (
    "webkit-layout",
    "webkit-cascade",
)

# Measured, not assumed. Each entry below has a probe result behind it.
BLIND_CLASSES = (
    # Anchoring is active in this build, so the defect is masked exactly as it
    # would be in Chrome. Measured: 400px inserted, 400px compensated.
    "scroll-restoration",
    # Compositing is disabled at launch on this port and async scrolling is not
    # compiled in, so any settling figure describes an implementation that
    # ships nowhere.
    "scroll-settling",
    "momentum-scroll",
    "compositor-animation",
    # Playwright disables the back/forward cache in every engine.
    "bfcache-restore",
    # Long Animation Frames and Long Tasks are absent, so a stall cannot be
    # attributed to script versus style-and-layout.
    "stall-attribution",
    # No Apple hardware is involved at any point.
    "ios-behaviour",
    # The host's display scaling overrode the requested ratio.
    "device-pixel-geometry",
)

# Entry types the lane depends on. Deliberately short: this build reports far
# fewer than Chromium, and demanding Chromium's set would make the lane refuse
# to run for capabilities it never claimed.
REQUIRED_ENTRY_TYPES = ("event", "first-input", "paint")

# Absent here, and the lane says so rather than reporting their absence as calm.
KNOWN_ABSENT_ENTRY_TYPES = ("long-animation-frame", "longtask", "layout-shift")

# What the probe measured on 2026-10-19, kept so a future run can tell a changed
# engine from a changed result.
PROBED = {
    "build": "webkit-2370",
    "ua_version": "27.2",
    "anchoring": "active",
    "isolated_clock_step_ms": 0.02,
    "event_timing": True,
    "loaf": False,
    "longtask": False,
    "probed_on": "2026-10-19",
}


def launch_kwargs(*, headless: bool = True, viewport=(402, 681),
                  dpr: float = 3.0, mobile_touch: bool = True) -> dict:
    """Context arguments for the WebKit lane.

    `dpr` is what we ask for; the lane records what the page reports, because
    the two were measured to disagree.
    """
    kw = {
        "viewport": {"width": viewport[0], "height": viewport[1]},
        "device_scale_factor": dpr,
    }
    if mobile_touch:
        kw["has_touch"] = True
        kw["is_mobile"] = True
    return {"headless": headless}, kw


def self_check(probe: dict, *, require_isolation: bool = True) -> str | None:
    """None when this build can supply what the lane claims, else why not."""
    if not probe:
        return "self-check returned nothing -- the page did not evaluate"

    missing = [t for t in REQUIRED_ENTRY_TYPES
               if t not in (probe.get("entry_types") or ())]
    if missing:
        return (f"this WebKit build does not report {', '.join(missing)}; the "
                f"lane's measurements depend on them")

    if require_isolation and not probe.get("isolated"):
        return ("document is not cross-origin isolated, so WebKit clamps the "
                "clock to whole milliseconds -- coarser than most of what this "
                "rig measures. Serve through the rig's server")

    step = probe.get("clock_step_ms")
    if step is None or step <= 0:
        return f"unusable clock step: {step!r}"
    if step > 1.0:
        return (f"measured clock step is {step:.3f}ms, which is the clamped "
                f"value -- isolation is not in effect even though it was "
                f"reported")

    return None


def anchoring_verdict(probe_result: dict) -> tuple:
    """`(is_active, note)` from the behavioural anchoring probe.

    Feature detection is not enough here: supporting the CSS property says what
    the engine parses, not what it does. The probe inserts content above the
    viewport and watches whether the visible row stays put.
    """
    moved = abs(probe_result.get("probe_row_moved_px", 0))
    compensated = probe_result.get("scrollTop_compensated", 0)
    if moved < 20 and compensated > 0:
        return (True,
                f"anchoring active: scrollTop compensated by {compensated}px "
                f"and the probe row moved {moved}px. This build therefore MASKS "
                f"the scroll-restoration defect exactly as Chrome does, so the "
                f"lane cannot be its oracle")
    return (False,
            f"anchoring absent: the probe row moved {moved}px and scrollTop "
            f"compensated by {compensated}px. This build CAN reproduce the "
            f"scroll-restoration defect, so the normal failing-first path "
            f"applies to it")


def dpr_note(requested: float, observed: float) -> str | None:
    """None when the requested pixel ratio took, else what actually happened.

    Measured to disagree on this port: 3.0 requested, 1.23 observed, which is
    the host display's scaling. Geometry in device pixels is unsafe when they
    differ, so the lane reports the mismatch rather than silently using either.
    """
    if observed and abs(observed - requested) < 0.01:
        return None
    return (f"device pixel ratio was requested as {requested} but the page "
            f"reports {observed} -- the host display's scaling is overriding "
            f"it on this port. Any geometry expressed in device pixels is "
            f"suspect; use CSS pixels or record both")


def wsl_status(launch_error: str | None) -> str:
    """Classify the undocumented WSL channel's availability.

    The distinction is worth keeping: a channel that does not exist is a dead
    end, while one that exists and needs a distribution provisioned is a
    decision about whether 10GB is worth scroll behaviour from WebKit.
    """
    if launch_error is None:
        return "available"
    err = launch_error.lower()
    if "wsl.exe" in err or "-d playwright" in err:
        return ("available-with-provisioning: the channel is registered and "
                "invoked wsl.exe, but the 'playwright' WSL distribution does "
                "not exist. Creating it is a ~10GB Ubuntu image")
    return f"unavailable: {launch_error}"


def credential_template(recorded_defect: str | None = None) -> dict:
    """An uncredentialed credential for this lane.

    `recorded_defect` is left None deliberately until a WebKit-expressible
    recorded defect is identified. The honesty core refuses a credential with
    no recorded defect, which is the correct state: this lane is not yet
    entitled to report, and saying so is better than crediting it against an
    authored injection.
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
