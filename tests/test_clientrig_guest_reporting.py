"""Guards for guest-side experience reporting.

The egress guard comes first, deliberately. "Nothing leaves the installation" is
the claim this entire feature rests on, and the plan's own review found the
original enforcement too weak: a text scan for absolute URLs in one file cannot
see a URL built by concatenation, one made with `new URL()`, a protocol-relative
address, a websocket, or any of it once the code moves into a shared module --
which this repo's structural rules actively encourage.

So the real guard is a running network-boundary test: drive the reporting flow in
a real engine, intercept every request across the full page lifecycle including
the flush that happens when the page is hidden, and fail on any request to a host
other than the installation. The text scan stays as a cheap secondary.

The guard is itself credentialed. A deliberately-egressing fixture must make it
fail -- without that, a passing egress test would only prove that nothing
happened to leave, not that the guard could see it if it did.
"""
import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_HARNESS = _ROOT / "tools" / "clientrig" / "harness"
_MODULE = _ROOT / "static" / "clientreport.js"


def _load(name, relpath):
    """Load a rig module by path, registering it so dataclasses can resolve."""
    spec = importlib.util.spec_from_file_location(name, _ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sv = _load("clientrig_serve5", "tools/clientrig/serve.py")
lane = _load("clientrig_chromium3", "tools/clientrig/lanes/chromium_desktop.py")

_HAVE_PW = importlib.util.find_spec("playwright") is not None


# ── the cheap secondary: no absolute address in the module source ───────────

def test_the_reporting_module_exists():
    assert _MODULE.exists(), (
        "static/clientreport.js is missing; the egress guarantee has nothing "
        "to enforce")


def test_the_module_source_carries_no_absolute_address():
    """A secondary check, explicitly not the guarantee.

    It cannot see a URL built by concatenation, one made with `new URL()`, a
    protocol-relative address, or a websocket. The running boundary test below
    is the actual enforcement.
    """
    src = _MODULE.read_text(encoding="utf-8")
    for probe in ("http://", "https://", "ws://", "wss://", "//"):
        if probe == "//":
            # Allow comment lines, forbid protocol-relative URLs in strings.
            bad = [ln for ln in src.splitlines()
                   if "'//" in ln or '"//' in ln or "`//" in ln]
            assert not bad, f"protocol-relative URL literal: {bad[:2]}"
            continue
        assert probe not in src, f"{probe!r} appears in the reporting module"


def test_the_endpoint_path_is_relative():
    """A relative path cannot leave the origin. An absolute one can."""
    src = _MODULE.read_text(encoding="utf-8")
    assert "/api/client-report" in src
    assert "://" not in src


# ── the real guard: a running network boundary ─────────────────────────────

@pytest.fixture(scope="module")
def served():
    httpd, port = sv.make_server(upstream="http://127.0.0.1:1",
                                 overlay_root=None,
                                 static_root=_ROOT / "static",
                                 harness_root=_HARNESS)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield port
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


@pytest.fixture(scope="module")
def browser_ctx(tmp_path_factory):
    if not _HAVE_PW:
        pytest.skip("playwright not installed; the egress guarantee that this "
                    "whole feature rests on goes unverified -- only the source "
                    "scan runs")
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    try:
        if not Path(pw.chromium.executable_path).exists():
            pytest.skip("playwright browsers not downloaded; egress guarantee "
                        "unverified")
        root = tmp_path_factory.mktemp("rigreport")
        ctx = pw.chromium.launch_persistent_context(
            **lane.launch_kwargs(str(root), "jp-clientrig-report-0",
                                 headless=True, viewport=(443, 816),
                                 mobile_touch=True, dpr=2.0))
        try:
            yield ctx
        finally:
            ctx.close()
    finally:
        pw.stop()


def _run_reporting_session(ctx, port, *, canary=False):
    """Drive the reporting flow and return every request the page attempted.

    Covers the full lifecycle including the hidden-page flush, because that is
    when the batch is sent and therefore exactly when an egress would happen.
    """
    page = ctx.new_page()
    attempted = []
    page.on("request", lambda r: attempted.append(r.url))
    try:
        qs = "?canary=egress" if canary else ""
        page.goto(f"http://127.0.0.1:{port}/harness/client_report.html{qs}",
                  wait_until="load")
        page.evaluate("async () => await window.__report.enable()")
        page.evaluate("async () => await window.__report.interact()")
        page.evaluate("() => window.__report.recordRestore(true)")
        # The flush path: hiding the page is what sends the batch.
        page.evaluate("""() => {
          Object.defineProperty(document, 'visibilityState',
            { value: 'hidden', configurable: true });
          document.dispatchEvent(new Event('visibilitychange'));
        }""")
        page.wait_for_timeout(600)
        return attempted, page.evaluate("() => window.__report.state()")
    finally:
        page.close()


def _foreign(urls, port):
    ok = (f"http://127.0.0.1:{port}", "about:", "data:", "blob:")
    return [u for u in urls if not any(u.startswith(p) for p in ok)]


def test_no_request_leaves_the_installation(served, browser_ctx):
    """THE GUARANTEE. Every request across the lifecycle stays on this origin."""
    attempted, state = _run_reporting_session(browser_ctx, served)
    foreign = _foreign(attempted, served)
    assert not foreign, (
        f"guest reporting attempted {len(foreign)} request(s) off the "
        f"installation: {foreign[:4]}")
    assert state["flushed"], (
        "nothing was flushed, so this run does not establish that the flush "
        "path stays local -- it establishes that no flush happened")


def test_the_egress_canary_makes_the_guard_fail(served, browser_ctx):
    """The guard's own credential.

    Without this, a passing egress test proves only that nothing tried to
    leave -- not that the guard would notice if something did.
    """
    attempted, _ = _run_reporting_session(browser_ctx, served, canary=True)
    foreign = _foreign(attempted, served)
    assert foreign, (
        "the deliberately-egressing fixture produced no foreign request, so "
        "the egress guard is not credentialed and its green means nothing")


def test_nothing_is_collected_while_the_feature_is_off(served, browser_ctx):
    """Off by default, and off means off: no batch, no flush, no request."""
    page = browser_ctx.new_page()
    attempted = []
    page.on("request", lambda r: attempted.append(r.url))
    try:
        page.goto(f"http://127.0.0.1:{served}/harness/client_report.html",
                  wait_until="load")
        # Deliberately NOT enabling.
        page.evaluate("async () => await window.__report.interact()")
        page.evaluate("""() => {
          Object.defineProperty(document, 'visibilityState',
            { value: 'hidden', configurable: true });
          document.dispatchEvent(new Event('visibilitychange'));
        }""")
        page.wait_for_timeout(500)
        state = page.evaluate("() => window.__report.state()")
        assert state["queued"] == 0, state
        assert not state["flushed"], state
        posts = [u for u in attempted if "client-report" in u]
        assert not posts, f"reported while disabled: {posts}"
    finally:
        page.close()


# ── what is collected, and what is not ────────────────────────────────────

def test_a_record_carries_a_view_name_and_never_a_url(served, browser_ctx):
    """For a music library the URL IS personal data -- it names what someone
    was looking at."""
    _, state = _run_reporting_session(browser_ctx, served)
    rec = state["sample"]
    assert rec, state
    assert rec.get("view"), "no view name on the record"
    blob = json.dumps(rec)
    assert "http" not in blob, f"a URL reached the record: {rec}"
    assert "/api/" not in blob
    assert "?" not in blob


def test_a_record_carries_no_identifier_and_no_fine_timestamp(served, browser_ctx):
    _, state = _run_reporting_session(browser_ctx, served)
    rec = state["sample"]
    blob = json.dumps(rec).lower()
    for banned in ("session", "token", "uuid", "serial", "cookie"):
        assert banned not in blob, f"{banned!r} in the record: {rec}"
    # Minute resolution at most: a finer stamp is a correlation handle.
    assert rec.get("minute") is not None
    assert "ms_epoch" not in rec and "timestamp" not in rec


def test_the_engine_is_recorded_coarsely_not_as_a_user_agent_string(
        served, browser_ctx):
    _, state = _run_reporting_session(browser_ctx, served)
    rec = state["sample"]
    assert rec.get("engine"), rec
    assert "Mozilla" not in json.dumps(rec), (
        "a full user-agent string is a fingerprint; record a family and a "
        "major version")


def test_the_restore_correctness_flag_is_carried(served, browser_ctx):
    """The route to closing report #2 from a real iPhone.

    A single boolean per back-out -- did the remembered album land back where it
    was. Correctness, not timing, and the only mechanism in the plan that can
    confirm that fix on a surface nobody here owns.
    """
    _, state = _run_reporting_session(browser_ctx, served)
    outcomes = state.get("restoreOutcomes")
    assert outcomes, f"no restore outcome recorded: {state}"
    assert outcomes[0]["ok"] is True


def test_the_engine_capability_gap_is_declared_not_inferred(served, browser_ctx):
    """Thinner data from an iPhone must be distinguishable from no data.

    Long Animation Frames are Chromium-only -- measured absent in the WebKit
    build this rig ships against. A record that simply omits them looks
    identical to a record from a browser that had nothing to report.
    """
    _, state = _run_reporting_session(browser_ctx, served)
    rec = state["sample"]
    assert "caps" in rec, rec
    assert "loaf" in rec["caps"], (
        "the record does not say whether this engine can report Long Animation "
        "Frames, so an absence cannot be told from a silence")
