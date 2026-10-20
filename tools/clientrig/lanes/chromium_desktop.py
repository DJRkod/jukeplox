"""The Chromium desktop lane: a cheap pre-filter that knows what it cannot see.

This lane runs on every change and catches order-of-magnitude main-thread
regressions. It is deliberately not an authority on anything about a phone: the
project has a measured 5,939x gap between desktop Chrome and mobile Chrome on
one defect, so a green here says nothing about the device. It may suggest a
cause; it may never close a speed report.

Everything below was verified against the installed engine rather than taken
from documentation, because three of the assumptions turned out to matter:

* **A real tap produces interaction timing; a scripted click produces none.**
  Measured here: 18 entries from a dispatched touch, 0 from `element.click()`.
  Not a weaker signal -- no signal. This is why the lane resolves coordinates
  first and drives `touchscreen.tap(x, y)` rather than using a locator action.
* **Touch emulation overrides headless pointer forcing.** Headless Chromium is
  launched with flags that force fine-pointer/hover media, but with touch
  emulation on, `(pointer: coarse)` and `(hover: none)` both match. Verified, so
  pointer-media-dependent CSS behaves as it would on a phone.
* **Cross-origin isolation lifts the clock to 5 microseconds.** Served through
  the rig's own server with the isolation headers, the measured
  `performance.now()` step is 0.005ms rather than the coarse default. Without
  isolation the clock would swamp the effects being measured.

Two bounds, not one
-------------------
The timing half is bounded at 8ms because the Event Timing specification rounds
`duration` to 8ms -- that is independent of how good the clock is, so a fine
clock does not improve it. The quiescence half is bounded by a frame interval,
because "the screen stopped changing" is counted in frames. They are reported
separately for exactly this reason.

Actionability is a warm-up, so it is bypassed
---------------------------------------------
A locator action runs animation-frame stability polling, scrolls the element
into view, and moves the mouse before pressing. All three are interactions with
the page before the measured one, which is the instrument error that published
"the dropdown was never slow" against a real 41-53 second freeze. Resolve the
box, then drive the raw input device.
"""
from __future__ import annotations

LANE_NAME = "chromium-desktop"

OWNS_CLASSES = (
    "missing-repaint",      # report #3 -- fully DOM-observable, no device needed
    "click-retarget",       # the recorded credential defect for this lane
    "scroll-restoration",   # the red arm for report #2, with anchoring disabled
    "egress",               # the guest-reporting network boundary
)

# Declared blindness, carried as data on every result. A lane may not own a
# class it is blind to, and the honesty core refuses that combination.
BLIND_CLASSES = (
    "gpu-compositing",      # GPU work runs in a process CPU throttling misses
    "thermal",              # unmodelled; bites long interactions specifically
    "memory-bandwidth",     # the effect that turns O(n) layout superlinear
    "device-input-latency", # emulated input, not a digitiser
    "ios-behaviour",
)

# Entry types this lane's measurements actually depend on. Checked at startup:
# a lane that cannot observe what it claims refuses to run rather than
# reporting a confident zero.
REQUIRED_ENTRY_TYPES = ("event", "first-input", "long-animation-frame",
                        "longtask", "paint")

# The Event Timing specification rounds `duration` to 8ms. A better clock does
# not improve this, so it is a constant rather than a measurement.
EVENT_TIMING_ROUNDING_MS = 8.0


def profile_dir(profile_root: str, run_tag: str) -> str:
    """Where this run's browser profile lives.

    The run tag has to appear here, because this is what puts it on the
    browser's command line -- and the command line is the only thing the
    teardown sweep can match on. Playwright's default launch creates a
    randomly-named temp profile instead, which would leave the sweep with
    nothing to find and silently defeat the mitigation for this project's worst
    recurring incident.
    """
    return f"{profile_root.rstrip('/').rstrip(chr(92))}/{run_tag}"


def launch_kwargs(profile_root: str, run_tag: str, *, headless: bool = True,
                  viewport=(1400, 900), dpr: float = 1.0,
                  mobile_touch: bool = False) -> dict:
    """Arguments for a persistent context, tagged so teardown can find it.

    `channel="chromium"` selects the full browser. The default headless path
    uses a separate shell binary with a different renderer configuration, and
    this lane measures rendering.
    """
    kw = {
        "user_data_dir": profile_dir(profile_root, run_tag),
        "channel": "chromium",
        "headless": headless,
        "viewport": {"width": viewport[0], "height": viewport[1]},
        "device_scale_factor": dpr,
    }
    if mobile_touch:
        kw["has_touch"] = True
        kw["is_mobile"] = True
    return kw


# Measures the engine's own capabilities, including the clock step. Run before
# any measurement and after navigation, since isolation is per-document.
SELF_CHECK_JS = """() => {
  const steps = [];
  for (let trial = 0; trial < 12; trial++) {
    const t0 = performance.now();
    let t1 = t0, spins = 0;
    while (t1 === t0 && spins < 2000000) { t1 = performance.now(); spins++; }
    steps.push(t1 - t0);
  }
  steps.sort((a, b) => a - b);
  return {
    isolated: !!window.crossOriginIsolated,
    entry_types: (window.PerformanceObserver
                  && PerformanceObserver.supportedEntryTypes) || [],
    clock_step_ms: steps[Math.floor(steps.length / 2)],
    pointer_coarse: matchMedia('(pointer: coarse)').matches,
    hover_none: matchMedia('(hover: none)').matches,
    reduced_motion: matchMedia('(prefers-reduced-motion: reduce)').matches,
  };
}"""


def self_check(probe: dict, *, require_isolation: bool = True,
               require_touch_media: bool = False) -> str | None:
    """None when the engine can supply what this lane claims, else why not.

    Pure policy over the probe's output, so it is provable without a browser.
    """
    if not probe:
        return "self-check returned nothing -- the page did not evaluate"

    missing = [t for t in REQUIRED_ENTRY_TYPES
               if t not in (probe.get("entry_types") or ())]
    if missing:
        return (f"engine does not report {', '.join(missing)}; this lane's "
                f"measurements depend on them, so it refuses to run rather "
                f"than report a confident zero")

    if require_isolation and not probe.get("isolated"):
        return ("document is not cross-origin isolated, so the clock is "
                "coarsened far above the effects being measured. Serve the "
                "harness through the rig's server, which sets the isolation "
                "headers -- this cannot be achieved over a file:// URL")

    step = probe.get("clock_step_ms")
    if step is None:
        return "self-check did not measure a clock step"
    if step <= 0:
        return (f"measured clock step is {step}, which cannot be used as a "
                f"resolution bound")
    if step > 1.0:
        return (f"measured clock step is {step:.3f}ms -- coarser than a "
                f"millisecond, which swamps sub-frame effects. Confirm "
                f"isolation and that no fingerprinting defence is coarsening "
                f"the timer")

    if require_touch_media and not probe.get("pointer_coarse"):
        return ("touch emulation is on but (pointer: coarse) does not match, "
                "so pointer-media-dependent CSS is taking the desktop branch "
                "and this is not the surface a phone would render")

    return None


def resolution_bounds(probe: dict, frame_interval_ms: float = 16.667) -> dict:
    """The two bounds that belong on a reading from this lane.

    They differ in kind, which is why the contract keeps the halves apart: the
    timing bound comes from a specification's rounding and does not improve
    with a better clock, while the quiescence bound is a frame interval.
    """
    return {
        "timing_resolution_ms": EVENT_TIMING_ROUNDING_MS,
        "quiescence_resolution_ms": frame_interval_ms,
        "clock_step_ms": probe.get("clock_step_ms"),
    }


# Installs the observers BEFORE the measured interaction. Reading latency from
# how long the driver waited is not an option: its stability check is
# animation-frame based, so a frozen main thread arrives as a timeout, and with
# retries enabled that reads as flake which passes.
INSTALL_OBSERVERS_JS = """() => {
  window.__jpRig = { events: [], loaf: [], longtasks: [], frames: [] };
  const r = window.__jpRig;
  try {
    new PerformanceObserver((l) => {
      for (const e of l.getEntries()) {
        r.events.push({ name: e.name, start: e.startTime,
                        duration: e.duration,
                        processingStart: e.processingStart,
                        processingEnd: e.processingEnd,
                        interactionId: e.interactionId });
      }
    }).observe({ type: 'event', buffered: true, durationThreshold: 16 });
  } catch (_) { r.noEvent = true; }
  try {
    new PerformanceObserver((l) => {
      for (const e of l.getEntries()) {
        r.loaf.push({ start: e.startTime, duration: e.duration,
                      blockingDuration: e.blockingDuration,
                      renderStart: e.renderStart,
                      styleAndLayoutStart: e.styleAndLayoutStart });
      }
    }).observe({ type: 'long-animation-frame', buffered: true });
  } catch (_) { r.noLoaf = true; }
  try {
    new PerformanceObserver((l) => {
      for (const e of l.getEntries()) {
        r.longtasks.push({ start: e.startTime, duration: e.duration });
      }
    }).observe({ type: 'longtask', buffered: true });
  } catch (_) { r.noLongtask = true; }
  return true;
}"""

# Counts frames and reports when the region of interest last changed. Stillness
# is N consecutive frames with an unchanged bounding box, no running animations,
# and no mutation records -- never "the row count stopped changing", which goes
# flat between batches of a chunked render and reported done at a fraction of
# the content in the recorded incident.
WATCH_QUIESCENCE_JS = """(selector) => {
  const el = document.querySelector(selector);
  if (!el) return { ok: false, reason: 'selector matched nothing: ' + selector };
  const r = window.__jpRig;
  r.quiescence = { startedAt: performance.now(), settledAt: null, frames: 0 };
  let mutated = false;
  const mo = new MutationObserver(() => { mutated = true; });
  mo.observe(el, { childList: true, subtree: true, attributes: true });
  let last = JSON.stringify(el.getBoundingClientRect());
  let still = 0;
  const STILL_FRAMES = 3;
  function tick() {
    r.quiescence.frames++;
    const now = JSON.stringify(el.getBoundingClientRect());
    const running = (document.getAnimations ? document.getAnimations() : [])
      .filter((a) => a.playState === 'running').length;
    if (now === last && !mutated && running === 0) {
      still++;
      if (still >= STILL_FRAMES) {
        r.quiescence.settledAt = performance.now();
        mo.disconnect();
        return;
      }
    } else {
      still = 0;
      last = now;
      mutated = false;
    }
    if (r.quiescence.frames < 900) requestAnimationFrame(tick);
    else { r.quiescence.reason = 'never settled within 900 frames'; mo.disconnect(); }
  }
  requestAnimationFrame(tick);
  return { ok: true };
}"""


def credential_template(recorded_defect: str = "pointer-capture-retarget") -> dict:
    """An explicitly uncredentialed credential for this lane."""
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
