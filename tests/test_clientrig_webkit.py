"""Tests for the WebKit lane, including the probe that settles another unit.

The behavioural anchoring test here is not really about this lane. It answers a
question the scroll-restoration fix depends on: does the shipped WebKit build
have the scroll-anchoring correction that masks the reported defect? If it does,
this lane cannot be that report's oracle and the red arm has to come from
elsewhere. Measured answer: it does.

The integration tests run the real engine. They skip loudly when the browser is
absent, naming what goes unverified, and every browser closes in a `finally`.
"""
import importlib.util
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


lane = _load("clientrig_webkit", "tools/clientrig/lanes/webkit_local.py")
h = _load("clientrig_health5", "tools/clientrig/health.py")
sv = _load("clientrig_serve3", "tools/clientrig/serve.py")


def probe(**kw):
    base = dict(isolated=True,
                entry_types=list(lane.REQUIRED_ENTRY_TYPES) + ["mark"],
                clock_step_ms=0.02)
    base.update(kw)
    return base


# ── what this lane is, and is not ──────────────────────────────────────────

def test_the_lane_is_blind_to_scroll_restoration_because_anchoring_masks_it():
    """The measured consequence, not a precaution: this build compensates for
    content inserted above the viewport, exactly as Chrome does, so the defect
    cannot appear here."""
    assert "scroll-restoration" in lane.BLIND_CLASSES


def test_the_lane_is_blind_to_everything_scroll_or_compositor_paced():
    for cls in ("scroll-settling", "momentum-scroll", "compositor-animation"):
        assert cls in lane.BLIND_CLASSES


def test_the_lane_is_blind_to_stall_attribution_because_loaf_is_absent():
    """It can sometimes see that a frame was slow; it can never say whether the
    time went to script or to style and layout."""
    assert "stall-attribution" in lane.BLIND_CLASSES
    assert "long-animation-frame" in lane.KNOWN_ABSENT_ENTRY_TYPES


def test_the_lane_never_claims_to_be_safari():
    assert "ios-behaviour" in lane.BLIND_CLASSES
    text = (_ROOT / "tools" / "clientrig" / "lanes"
            / "webkit_local.py").read_text(encoding="utf-8")
    assert "not Safari" in text


def test_the_lane_owns_only_correctness_classes():
    assert set(lane.OWNS_CLASSES) == {"webkit-layout", "webkit-cascade"}
    for owned in lane.OWNS_CLASSES:
        assert "settling" not in owned and "latency" not in owned


def test_the_lane_never_owns_a_class_it_is_blind_to():
    cred = h.Credential(**lane.credential_template())
    assert h.ownership_health(cred) is None


def test_the_lane_is_uncredentialed_and_names_the_missing_recorded_defect():
    """Honest state: no WebKit-expressible recorded defect has been identified
    yet, so the lane is not entitled to report. Crediting it against an authored
    injection instead would be the weaker claim the rig exists to avoid."""
    cred = h.Credential(**lane.credential_template())
    why = h.credential_health(cred, declared_blind=lane.BLIND_CLASSES)
    assert why
    assert "uncredentialed" in why.lower() or "recorded" in why.lower()


def test_the_required_entry_types_are_short_because_this_build_reports_few():
    """Demanding Chromium's set would make the lane refuse to run for
    capabilities it never claimed."""
    assert "long-animation-frame" not in lane.REQUIRED_ENTRY_TYPES
    assert "longtask" not in lane.REQUIRED_ENTRY_TYPES
    assert "event" in lane.REQUIRED_ENTRY_TYPES


# ── self-check ─────────────────────────────────────────────────────────────

def test_a_capable_build_passes():
    assert lane.self_check(probe()) is None


def test_a_clamped_clock_is_recognised_as_isolation_not_being_in_effect():
    why = lane.self_check(probe(clock_step_ms=1.0 + 1e-9))
    assert why and "clamped" in why


def test_a_non_isolated_document_refuses_and_says_why_it_matters_more_here():
    why = lane.self_check(probe(isolated=False))
    assert why and "whole millisecond" in why


def test_a_missing_entry_type_refuses_and_names_it():
    why = lane.self_check(probe(entry_types=["paint"]))
    assert why and "event" in why


# ── the anchoring verdict ──────────────────────────────────────────────────

def test_an_active_anchoring_result_declares_the_lane_cannot_be_the_oracle():
    active, note = lane.anchoring_verdict(
        {"probe_row_moved_px": 0, "scrollTop_compensated": 400})
    assert active
    assert "mask" in note.lower()


def test_an_absent_anchoring_result_would_restore_the_normal_path():
    """The branch that did not happen, kept because a future engine update
    could flip it and the plan should not have to be re-derived."""
    active, note = lane.anchoring_verdict(
        {"probe_row_moved_px": 400, "scrollTop_compensated": 0})
    assert not active
    assert "can reproduce" in note.lower()


def test_the_recorded_probe_result_says_anchoring_was_active():
    assert lane.PROBED["anchoring"] == "active"
    assert lane.PROBED["loaf"] is False
    assert lane.PROBED["event_timing"] is True


# ── the pixel-ratio mismatch ───────────────────────────────────────────────

def test_a_matching_pixel_ratio_produces_no_note():
    assert lane.dpr_note(requested=2.0, observed=2.0) is None


def test_a_mismatched_pixel_ratio_is_reported_with_both_numbers():
    note = lane.dpr_note(requested=3.0, observed=1.23)
    assert note and "3.0" in note and "1.23" in note
    assert "suspect" in note.lower()


# ── the WSL channel: registered but unprovisioned ─────────────────────────

def test_a_successful_launch_reports_available():
    assert lane.wsl_status(None) == "available"


def test_the_measured_failure_is_classified_as_needing_provisioning():
    """The distinction matters: a channel that does not exist is a dead end,
    while one that needs a distribution is a decision about whether 10GB buys
    enough."""
    status = lane.wsl_status(
        "Error: BrowserType.launch: Failed to launch the browser process.\n"
        "<launching> C:\\WINDOWS\\System32\\wsl.exe -d playwright -u pwuser")
    assert status.startswith("available-with-provisioning")
    assert "10GB" in status or "10gb" in status.lower()


def test_an_unrelated_failure_is_not_laundered_into_provisioning():
    status = lane.wsl_status("Error: executable doesn't exist at /nope")
    assert status.startswith("unavailable")


# ── integration: the real engine ──────────────────────────────────────────

_HAVE_PW = importlib.util.find_spec("playwright") is not None

_ANCHOR_JS = """() => {
  document.body.innerHTML =
    '<div id=sc style="height:300px;overflow-y:auto"><div id=top></div>' +
    Array.from({length: 60}, (_, i) =>
      '<div class=row style="height:40px">row ' + i + '</div>').join('') +
    '</div>';
  const sc = document.getElementById('sc');
  sc.scrollTop = 800;
  void sc.scrollHeight;
  const before = sc.scrollTop;
  const row = sc.querySelectorAll('.row')[20];
  const beforeTop = row.getBoundingClientRect().top;
  const ins = document.createElement('div');
  ins.style.height = '400px';
  document.getElementById('top').appendChild(ins);
  void sc.scrollHeight;
  return {
    scrollTop_compensated: sc.scrollTop - before,
    probe_row_moved_px: row.getBoundingClientRect().top - beforeTop,
  };
}"""


@pytest.fixture
def served():
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
def wk_page():
    if not _HAVE_PW:
        pytest.skip("playwright not installed; the WebKit anchoring verdict "
                    "that the scroll-restore fix depends on goes unverified")
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    try:
        if not Path(pw.webkit.executable_path).exists():
            pytest.skip("webkit browser not downloaded; anchoring verdict "
                        "unverified")
        launch, ctx_kw = lane.launch_kwargs()
        browser = pw.webkit.launch(**launch)
        try:
            page = browser.new_page(**ctx_kw)
            yield page
        finally:
            browser.close()
    finally:
        pw.stop()


def test_the_real_build_has_anchoring_active(served, wk_page):
    """The measurement that settles report #2's oracle.

    If this ever fails, the engine changed and the scroll-restoration report
    gains a WebKit oracle -- which would be good news, and the plan's other
    branch is already written for it.
    """
    wk_page.goto(f"http://127.0.0.1:{served}/static/shared.js")
    wk_page.set_content("<!doctype html><title>anchor</title>")
    result = wk_page.evaluate(_ANCHOR_JS)
    active, note = lane.anchoring_verdict(result)
    assert active, f"anchoring now appears ABSENT: {result} -- {note}"


def test_the_real_build_passes_the_lanes_self_check(served, wk_page):
    wk_page.goto(f"http://127.0.0.1:{served}/static/shared.js")
    p = wk_page.evaluate("""() => {
      const steps = [];
      for (let t = 0; t < 12; t++) {
        const a = performance.now(); let b = a, s = 0;
        while (b === a && s < 2000000) { b = performance.now(); s++; }
        steps.push(b - a);
      }
      steps.sort((x, y) => x - y);
      return { isolated: !!window.crossOriginIsolated,
               entry_types: PerformanceObserver.supportedEntryTypes || [],
               clock_step_ms: steps[6] };
    }""")
    assert lane.self_check(p) is None, p


def test_the_real_build_confirms_loaf_is_absent(served, wk_page):
    """Blindness declared from a measurement, not a precaution."""
    wk_page.goto(f"http://127.0.0.1:{served}/static/shared.js")
    types = wk_page.evaluate(
        "() => PerformanceObserver.supportedEntryTypes || []")
    assert "long-animation-frame" not in types
    assert "event" in types, "Event Timing should be present on Safari 26.2+"
