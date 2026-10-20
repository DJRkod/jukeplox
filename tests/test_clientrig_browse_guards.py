"""Behavioural guards for the browse reports, driven in the Chromium lane.

These are the assertions the existing frontend suite physically cannot make: it
runs JavaScript in bare Node, which has no layout engine, no animation frames
and no input pipeline, so geometry, visibility and scroll questions are
unanswerable there. Each report also carries a source-level pin in
`tests/test_frontend_regressions.py`; this file is the half that needs an engine.

Report #3 -- the rail style change that leaves no rail
------------------------------------------------------
Reproduced here before it was fixed, and the arm matters more than the
assertion. The style setter re-renders every cached list; the two renders race
on separate animation-frame pumps; whichever finishes LAST claims the rail.
Albums is far longer than Artists, so Albums reliably finishes last -- which
means the rail ends up hosted in the Albums pane and the defect appears when
**Artists** is the visible tab.

Measured on the unfixed build, 300 artists and 1500 albums:

    visible pane | rail host   | rail visible after style change
    Artists      | albums-view | false   <- the defect
    Albums       | albums-view | true    <- healthy

An earlier draft of this plan named the Albums-visible case as the primary
scenario. That arm passes against broken code, so the failing-first step would
have returned green and read as a refuted diagnosis.

Every arm runs in a fresh page, because order is a variable: sharing a page let
the second arm inherit the first's rail state, which made a healthy arm look
broken for the wrong reason.
"""
import importlib.util
import sys
import threading
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_HARNESS = _ROOT / "tools" / "clientrig" / "harness"


def _load(name, relpath):
    """Load a rig module by path, registering it so dataclasses can resolve.

    See the note in `tests/test_clientrig_contract.py`.
    """
    spec = importlib.util.spec_from_file_location(name, _ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sv = _load("clientrig_serve4", "tools/clientrig/serve.py")
lane = _load("clientrig_chromium2", "tools/clientrig/lanes/chromium_desktop.py")

_HAVE_PW = importlib.util.find_spec("playwright") is not None

# A 1x1 transparent PNG. Album rows request art; without a stub the rig's
# server proxies those to a dead upstream and the console fills with 502s that
# bury real page errors.
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082")


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
        pytest.skip("playwright not installed; the browse behavioural guards "
                    "(rail ownership after a style change, jump landing, "
                    "scroll restoration) go unverified -- the source-level pins "
                    "in tests/test_frontend_regressions.py still run")
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    try:
        if not Path(pw.chromium.executable_path).exists():
            pytest.skip("playwright browsers not downloaded; browse "
                        "behavioural guards unverified")
        root = tmp_path_factory.mktemp("rigprof")
        ctx = pw.chromium.launch_persistent_context(
            **lane.launch_kwargs(str(root), "jp-clientrig-guards-0",
                                 headless=True, viewport=(1400, 900)))
        try:
            yield ctx
        finally:
            ctx.close()
    finally:
        pw.stop()


def _rail_after_style_change(ctx, port, visible_pane, *, mode="magnetic",
                             setter="setRailMode", artists=300, albums=1500):
    """Fresh page: cache both lists, show one pane, change the style, report.

    Returns the discriminating state -- which pane is visible, where the rail is
    hosted, and whether it is visible there. A boolean "a rail exists somewhere"
    cannot express this defect, because the rail does exist; it is parked in the
    pane nobody can see.
    """
    page = ctx.new_page()
    try:
        page.route("**/api/art/**",
                   lambda route: route.fulfill(status=200, body=_PNG,
                                               content_type="image/png"))
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"http://127.0.0.1:{port}/harness/rail_repaint.html"
                  f"?artists={artists}&albums={albums}", wait_until="load")
        assert page.evaluate("async () => await window.__harness.mount()")
        cached = page.evaluate("async () => await window.__harness.cacheBothLists()")
        assert cached["ok"], f"harness failed to cache both lists: {cached}"

        page.evaluate("(id) => window.__harness.show(id)", visible_pane)
        page.evaluate("async () => await window.__harness.settle(30)")
        applied = page.evaluate(
            "([s, m]) => window.__harness[s](m)", [setter, mode])
        page.evaluate("async () => await window.__harness.settle(150)")
        state = page.evaluate("() => window.__railState()")
        state["_applied"] = applied
        state["_errors"] = errors[:5]
        return state
    finally:
        page.close()


# ── report #3: the rail must belong to the pane the user can see ───────────

def test_rail_survives_a_style_change_with_artists_visible(served, browser_ctx):
    """THE RED ARM. Artists is the shorter list, so Albums finishes last and
    claims the rail -- parking it in the hidden pane.

    Measured on the unfixed build: rail hosted in albums-view, not visible.
    """
    s = _rail_after_style_change(browser_ctx, served, "artists-view")
    assert not s["_errors"], s["_errors"]
    assert s["railExists"], "no rail was built at all -- harness problem"
    assert s["railMode"] == "magnetic", s
    assert s["railInVisiblePane"], (
        f"the rail is hosted in {s['hostId']!r} while the visible pane is "
        f"{s['visiblePaneId']!r} -- it belongs to whichever render finished "
        f"last, not to the pane the user is looking at")
    assert s["railVisible"], (
        f"the style change left no visible rail: {s}. This is report #3 -- the "
        f"user sees no rail until a tab switch re-renders the visible list")


def test_rail_survives_a_style_change_with_albums_visible(served, browser_ctx):
    """The non-regression arm, and the one that already passed.

    Keeping it is the point: it is why the original scenario choice would have
    produced a green failing-first step.
    """
    s = _rail_after_style_change(browser_ctx, served, "albums-view")
    assert s["railExists"], s
    assert s["railMode"] == "magnetic", s
    assert s["railInVisiblePane"], s
    assert s["railVisible"], s


def test_rail_survives_a_view_mode_change_with_artists_visible(served, browser_ctx):
    """Same seam, different control. The view toggle runs the identical
    back-to-back double render, so a fix confined to the style setter leaves the
    defect reachable from here."""
    s = _rail_after_style_change(browser_ctx, served, "artists-view",
                                 mode="tile", setter="setViewMode")
    assert s["_applied"], "setViewMode was not available on the handle"
    assert s["railExists"], s
    assert s["railInVisiblePane"], (
        f"rail hosted in {s['hostId']!r}, visible pane {s['visiblePaneId']!r} "
        f"-- the view toggle reaches the same ownership seam")
    assert s["railVisible"], s


def test_rail_survives_a_rail_alpha_change_with_artists_visible(served, browser_ctx):
    """Third control reaching the same seam. Skips rather than fails if the
    handle does not expose it, so the guard cannot silently pass by calling
    nothing."""
    s = _rail_after_style_change(browser_ctx, served, "artists-view",
                                 mode="international", setter="setRailAlpha")
    if not s["_applied"]:
        pytest.skip("setRailAlpha is not exposed on the mount handle; the "
                    "international-rail control is not covered by this guard")
    assert s["railExists"], s
    assert s["railInVisiblePane"], s
    assert s["railVisible"], s


def test_a_style_change_with_only_one_list_cached_still_applies(served, browser_ctx):
    """The single-render path must not regress.

    Also the arm that would pass against broken code, which is why it cannot be
    the failing-first scenario.
    """
    page = browser_ctx.new_page()
    try:
        page.route("**/api/art/**",
                   lambda route: route.fulfill(status=200, body=_PNG,
                                               content_type="image/png"))
        page.goto(f"http://127.0.0.1:{served}/harness/rail_repaint.html"
                  f"?artists=300&albums=1500", wait_until="load")
        assert page.evaluate("async () => await window.__harness.mount()")
        # Only Albums is cached: one render, no race.
        page.evaluate("async () => { await window.__harness.handle"
                      ".activateView('albums-view'); }")
        assert page.evaluate(
            "async () => await window.__harness.waitForColumn('#albums-list')")
        page.evaluate("(id) => window.__harness.show(id)", "albums-view")
        page.evaluate("() => window.__harness.setRailMode('waveform')")
        page.evaluate("async () => await window.__harness.settle(150)")
        s = page.evaluate("() => window.__railState()")
        assert s["railVisible"], s
        assert s["railMode"] == "waveform", s
    finally:
        page.close()


def test_the_rail_carries_real_buckets_so_the_guard_cannot_pass_on_an_empty_rail(
        served, browser_ctx):
    """A rail with no buttons would satisfy a presence check while being
    useless. The harness builds 26 letter groups, so the guard asserts the
    count as well as the visibility."""
    s = _rail_after_style_change(browser_ctx, served, "albums-view")
    assert s["buttonCount"] >= 20, (
        f"only {s['buttonCount']} rail buttons -- a nearly-empty rail would "
        f"pass a visibility check while telling you nothing")

# ── report #2: come back to the album you opened ───────────────────────────
#
# Report #2 does NOT reproduce on a desktop engine. Measured 2026-10-19 across
# list mode, tile mode, a narrow viewport and late-arriving art, with the
# browser's scroll-anchoring correction both on and off: every arm restored the
# exact row at the exact sub-row offset, and scrollHeight was identical before
# and after in all of them. That is the reason -- the list is awaited to
# completion before the scroll write, so no height ever differs and anchoring
# has nothing to correct.
#
# The remaining candidates are iOS-only and unreachable here: Safari's own
# history restoration competing with the app's, the URL bar collapsing and
# changing the visible height, and momentum scrolling still gliding when the tap
# lands, so the recorded offset is one the list was merely passing through.
#
# The fix is justified without a reproduction because it is correct on its own
# terms: you opened an album, you should come back to it. The guards below prove
# the anchored restore works, and that it survives the class of change a
# remembered offset cannot.


def _restore_page(ctx, port, albums=1500):
    page = ctx.new_page()
    page.route("**/api/art/**",
               lambda route: route.fulfill(status=200, body=_PNG,
                                           content_type="image/png"))
    page.goto(f"http://127.0.0.1:{port}/harness/scroll_restore.html"
              f"?albums={albums}", wait_until="load")
    page.evaluate("async () => await window.__harness.mount()")
    assert page.evaluate("async () => await window.__harness.loadAlbums()")
    page.evaluate("async () => await window.__harness.settle(50)")
    return page


def test_backing_out_returns_to_the_album_you_opened(served, browser_ctx):
    """The behaviour as a user would state it: you opened an album, you come
    back to it, at the same place on screen."""
    page = _restore_page(browser_ctx, served)
    try:
        page.evaluate("() => window.__harness.scrollTo(24000)")
        page.evaluate("async () => await window.__harness.settle(20)")
        before = page.evaluate("() => window.__probe()")
        d = page.evaluate(
            "async () => await window.__harness.drillByClickingRowAt(200)")
        assert d["ok"], d
        page.evaluate("async () => await window.__harness.settle(30)")
        b = page.evaluate("async () => await window.__harness.backOut()")
        assert b["ok"], b
        page.evaluate("async () => await window.__harness.settle(120)")
        after = page.evaluate("() => window.__probe()")
        assert after["rowAtProbe"]["title"] == before["rowAtProbe"]["title"], (
            f"opened {before['rowAtProbe']} and came back to "
            f"{after['rowAtProbe']}")
        assert abs(after["rowAtProbe"]["delta"]
                   - before["rowAtProbe"]["delta"]) <= 2, (
            "same row but a different sub-row offset -- the restore is "
            "approximate where it should be exact")
    finally:
        page.close()


# NOT EXERCISED HERE, deliberately and explicitly.
#
# The robustness claim behind the anchored restore -- that it survives the list
# changing while the user is away -- cannot be tested in this harness. The fetch
# mock serialises to JSON, so the module holds a deep copy of the catalog and
# mutating it after load has no effect: measured at catalog length 1900 against
# 1500 rendered rows with scrollHeight unchanged.
#
# Three guards previously stood here (content growing above, its negative
# control, and a missing-anchor fallback). All three were passing because
# nothing happened, and the negative control correctly reported that the
# scenario did not discriminate. They are removed rather than left green,
# because a guard that passes vacuously is worse than no guard -- it gets cited
# as evidence later.
#
# What IS covered: the anchored restore returns the user to the album they
# opened, at the same place on screen (above), and the shape of the capture and
# restore is pinned in tests/test_frontend_regressions.py. What is NOT covered:
# behaviour when the list reorders, shrinks, or grows between capture and
# restore -- which is the class every remaining candidate mechanism for report
# #2 belongs to. Closing that needs either a module seam for evicting the cached
# list, or the real iOS surface the report came from.
