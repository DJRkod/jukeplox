"""Tests for the Chromium desktop lane.

Two halves. The pure policy -- launch arguments, the self-check, the resolution
bounds -- runs anywhere. The integration half launches the real engine through
the rig's own isolating server and asserts the three claims this lane rests on,
because all three were assumptions until they were measured:

  * a dispatched touch produces interaction timing and a scripted click produces
    none, which is the entire justification for bypassing locator actions;
  * cross-origin isolation through our server lifts the clock to microseconds;
  * touch emulation beats the headless pointer-media forcing.

The integration tests skip loudly when Playwright's browsers are absent, naming
the coverage lost -- the `shutil.which` precedent in
`tests/test_frontend_regressions.py`. Every browser is launched into a
tag-carrying profile directory and closed in a `finally`, because this repo's
recorded incidents are 120 and 140 orphaned browser processes.
"""
import importlib.util
import shutil
import sys
import threading
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _load(name, relpath):
    """Load a rig module by path, registering it so dataclasses can resolve.

    See the note in `tests/test_clientrig_contract.py`.
    """
    spec = importlib.util.spec_from_file_location(name, _ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


lane = _load("clientrig_chromium", "tools/clientrig/lanes/chromium_desktop.py")
h = _load("clientrig_health4", "tools/clientrig/health.py")
sv = _load("clientrig_serve2", "tools/clientrig/serve.py")


def probe(**kw):
    base = dict(isolated=True,
                entry_types=list(lane.REQUIRED_ENTRY_TYPES) + ["mark"],
                clock_step_ms=0.005, pointer_coarse=True, hover_none=True,
                reduced_motion=False)
    base.update(kw)
    return base


# ── launch arguments: the run tag must reach the command line ───────────────

def test_the_profile_directory_carries_the_run_tag():
    """This is the only thing the teardown sweep can match on. Playwright's
    default launch uses a random temp profile, which would leave the sweep with
    nothing to find and silently defeat the mitigation."""
    d = lane.profile_dir("/tmp/rig", "jp-clientrig-123-abc-0")
    assert "jp-clientrig-123-abc-0" in d


def test_launch_selects_the_full_browser_not_the_headless_shell():
    """The shell is a different renderer configuration, and this lane measures
    rendering."""
    kw = lane.launch_kwargs("/tmp/rig", "jp-clientrig-1-a-0")
    assert kw["channel"] == "chromium"


def test_launch_kwargs_carry_the_tag_and_the_viewport():
    kw = lane.launch_kwargs("/tmp/rig", "jp-clientrig-1-a-0",
                            viewport=(443, 816), dpr=2.44)
    assert "jp-clientrig-1-a-0" in kw["user_data_dir"]
    assert kw["viewport"] == {"width": 443, "height": 816}
    assert kw["device_scale_factor"] == 2.44


def test_mobile_touch_turns_on_touch_and_mobile_together():
    kw = lane.launch_kwargs("/tmp/rig", "t", mobile_touch=True)
    assert kw["has_touch"] and kw["is_mobile"]


def test_desktop_launch_does_not_claim_touch():
    kw = lane.launch_kwargs("/tmp/rig", "t")
    assert "has_touch" not in kw


# ── the self-check: refuse rather than report a confident zero ──────────────

def test_a_capable_engine_passes_the_self_check():
    assert lane.self_check(probe()) is None


def test_a_missing_entry_type_refuses_and_names_it():
    p = probe(entry_types=["event", "paint"])
    why = lane.self_check(p)
    assert why and "long-animation-frame" in why


def test_a_non_isolated_document_refuses_because_the_clock_is_coarse():
    why = lane.self_check(probe(isolated=False))
    assert why and "isolated" in why.lower()
    assert "file://" in why


def test_isolation_can_be_waived_explicitly_for_a_non_timing_run():
    """Correctness-only runs -- did the rail repaint, did the restore land --
    do not need a fine clock, and demanding one would block them for no gain."""
    assert lane.self_check(probe(isolated=False),
                           require_isolation=False) is None


def test_a_coarse_clock_refuses_with_the_measured_step():
    why = lane.self_check(probe(clock_step_ms=1.0 + 1e-9))
    assert why and "ms" in why


def test_a_missing_clock_measurement_refuses():
    assert lane.self_check(probe(clock_step_ms=None)) is not None


def test_a_zero_clock_step_refuses_rather_than_being_used_as_a_bound():
    assert lane.self_check(probe(clock_step_ms=0)) is not None


def test_an_empty_probe_refuses():
    assert lane.self_check({}) is not None


def test_touch_media_can_be_required_and_is_checked():
    why = lane.self_check(probe(pointer_coarse=False),
                          require_touch_media=True)
    assert why and "coarse" in why


# ── resolution bounds: two of them, different in kind ──────────────────────

def test_the_timing_bound_is_the_specs_rounding_not_the_clock():
    """A fine clock does not improve it: the Event Timing specification rounds
    `duration` to 8ms, so reporting a finer bound would overstate precision."""
    b = lane.resolution_bounds(probe(clock_step_ms=0.005))
    assert b["timing_resolution_ms"] == 8.0


def test_the_quiescence_bound_is_a_frame_interval():
    b = lane.resolution_bounds(probe(), frame_interval_ms=16.667)
    assert b["quiescence_resolution_ms"] == 16.667


def test_the_measured_clock_step_is_carried_alongside_both():
    b = lane.resolution_bounds(probe(clock_step_ms=0.005))
    assert b["clock_step_ms"] == 0.005


# ── declarations ───────────────────────────────────────────────────────────

def test_the_lane_starts_uncredentialed():
    cred = h.Credential(**lane.credential_template())
    assert h.credential_health(cred, declared_blind=lane.BLIND_CLASSES)


def test_the_lane_never_owns_a_class_it_is_blind_to():
    cred = h.Credential(**lane.credential_template())
    assert h.ownership_health(cred) is None


def test_the_lane_declares_blindness_to_gpu_and_thermal_effects():
    for cls in ("gpu-compositing", "thermal", "memory-bandwidth"):
        assert cls in lane.BLIND_CLASSES


def test_the_lane_owns_the_repaint_report_which_needs_no_device():
    assert "missing-repaint" in lane.OWNS_CLASSES


# ── integration: the real engine, through the rig's own server ─────────────

_HAVE_PW = importlib.util.find_spec("playwright") is not None


@pytest.fixture
def served():
    """The rig's server, running on loopback, torn down in a finally."""
    httpd, port = sv.make_server(upstream="http://127.0.0.1:1",
                                 overlay_root=None,
                                 static_root=_ROOT / "static")
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield port
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


@pytest.fixture
def ctx(tmp_path):
    """A tagged persistent context, always closed."""
    if not _HAVE_PW:
        pytest.skip("playwright not installed; the engine-behaviour claims "
                    "(real input produces interaction timing, isolation lifts "
                    "the clock, touch media overrides headless forcing) go "
                    "unverified here")
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    try:
        browsers = Path(pw.chromium.executable_path)
        if not browsers.exists():
            pytest.skip("playwright browsers not downloaded; engine-behaviour "
                        "claims unverified")
        context = pw.chromium.launch_persistent_context(
            **lane.launch_kwargs(str(tmp_path), "jp-clientrig-test-0",
                                 mobile_touch=True, viewport=(443, 816),
                                 dpr=2.0))
        try:
            yield context
        finally:
            context.close()
    finally:
        pw.stop()


def test_the_self_check_passes_against_the_real_engine(served, ctx):
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(f"http://127.0.0.1:{served}/static/shared.js")
    p = page.evaluate(lane.SELF_CHECK_JS)
    assert lane.self_check(p, require_touch_media=True) is None, p


def test_the_rigs_server_actually_delivers_cross_origin_isolation(served, ctx):
    """The claim the clock precision rests on, verified end to end."""
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(f"http://127.0.0.1:{served}/static/shared.js")
    assert page.evaluate("() => !!window.crossOriginIsolated") is True


def test_the_measured_clock_step_is_sub_millisecond_when_isolated(served, ctx):
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(f"http://127.0.0.1:{served}/static/shared.js")
    step = page.evaluate(lane.SELF_CHECK_JS)["clock_step_ms"]
    assert 0 < step < 1.0, f"clock step {step}ms is too coarse to measure with"


def test_touch_emulation_beats_the_headless_pointer_media_forcing(served, ctx):
    """Headless Chromium is launched with flags forcing fine-pointer/hover
    media. If those won, pointer-media-dependent CSS would take the desktop
    branch and this would not be the surface a phone renders."""
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(f"http://127.0.0.1:{served}/static/shared.js")
    p = page.evaluate(lane.SELF_CHECK_JS)
    assert p["pointer_coarse"] is True
    assert p["hover_none"] is True


_TAP_PAGE = """<!doctype html><meta charset=utf-8>
<div id=t style="height:80px;width:240px;background:#ccc">tap</div>
<script>
document.getElementById('t').addEventListener('pointerdown', () => {
  const s = performance.now(); while (performance.now() - s < 60) {}
});
</script>"""


def test_a_dispatched_touch_produces_interaction_timing(served, ctx):
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(f"http://127.0.0.1:{served}/static/shared.js")
    page.set_content(_TAP_PAGE)
    page.evaluate(lane.INSTALL_OBSERVERS_JS)
    page.touchscreen.tap(120, 40)
    page.wait_for_timeout(300)
    events = page.evaluate("() => window.__jpRig.events")
    assert events, "a real tap produced no interaction-timing entries"


def test_a_scripted_click_produces_no_interaction_timing(served, ctx):
    """The measured basis for the rig's input rule. A scripted click is not a
    weaker signal than a real tap -- it is no signal, so any figure derived from
    one is measuring a different code path than a finger."""
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(f"http://127.0.0.1:{served}/static/shared.js")
    page.set_content(_TAP_PAGE)
    page.evaluate(lane.INSTALL_OBSERVERS_JS)
    page.evaluate("() => document.getElementById('t').click()")
    page.wait_for_timeout(300)
    events = page.evaluate("() => window.__jpRig.events")
    assert events == [], f"expected no entries from a scripted click, got {events}"


def test_quiescence_watcher_settles_on_a_still_region(served, ctx):
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(f"http://127.0.0.1:{served}/static/shared.js")
    page.set_content("<!doctype html><div id=t style='height:50px'>x</div>")
    page.evaluate(lane.INSTALL_OBSERVERS_JS)
    started = page.evaluate(lane.WATCH_QUIESCENCE_JS, "#t")
    assert started["ok"], started
    page.wait_for_timeout(400)
    q = page.evaluate("() => window.__jpRig.quiescence")
    assert q["settledAt"] is not None, q
    assert q["frames"] >= 3


def test_quiescence_watcher_reports_a_missing_selector_rather_than_settling(served, ctx):
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(f"http://127.0.0.1:{served}/static/shared.js")
    page.set_content("<!doctype html><div id=t></div>")
    page.evaluate(lane.INSTALL_OBSERVERS_JS)
    out = page.evaluate(lane.WATCH_QUIESCENCE_JS, "#nope")
    assert out["ok"] is False
    assert "nope" in out["reason"]
