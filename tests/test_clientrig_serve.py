"""Unit tests for the rig's delivery and harness server.

This one component does two jobs that both turned out to be missing from the
plan until a reviewer asked how a deliberately-broken build was supposed to
reach a phone over a cable:

  * **Delivery.** A canary is a patched copy of a file under `static/`, kept
    outside the worktree. The server overlays that copy on top of the real tree
    and proxies everything else to the running instance, so a desktop browser
    can point at it directly and the phone can reach it over `adb reverse`.
    Without this, no lane can be shown failing against a canary and the whole
    credentialing mechanism has nothing to stand on.
  * **Clock precision.** WebKit clamps `performance.now()` to whole
    milliseconds unless the page is cross-origin isolated, which swamps the
    effects being measured. Isolation needs two response headers, and it cannot
    be achieved over a `file://` URL at all -- which is how the existing perf
    bench loads its harness today.

The second-order trap, also tested: switching isolation on blocks the harness's
own subresources unless the same server marks them as same-origin resources. A
harness that loads isolated but renders no album art measures a different page.

No upstream default. The hygiene suite forbids defaulting a target address in
`tools/`, and a soak harness points at a real deployment -- so the target is
stated every time or the server refuses to start.
"""
import importlib.util
import json
import sys
import urllib.error
import urllib.request
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


sv = _load("clientrig_serve", "tools/clientrig/serve.py")


@pytest.fixture
def overlay(tmp_path):
    """A patched copy of one static file, the way a canary arrives on disk."""
    o = tmp_path / "overlay"
    (o / "browse").mkdir(parents=True)
    (o / "browse" / "index.js").write_text("// PATCHED BY CANARY\n",
                                           encoding="utf-8")
    return o


# ── routing: overlay wins, everything else proxies ──────────────────────────

def test_an_overlaid_file_is_served_from_the_overlay(overlay):
    r = sv.resolve(request_path="/static/browse/index.js",
                   static_root=_ROOT / "static", overlay_root=overlay)
    assert r.kind == "overlay"
    assert "PATCHED BY CANARY" in r.path.read_text(encoding="utf-8")


def test_a_static_file_absent_from_the_overlay_falls_through_to_the_real_tree(overlay):
    r = sv.resolve(request_path="/static/shared.js",
                   static_root=_ROOT / "static", overlay_root=overlay)
    assert r.kind == "static"
    assert r.path == (_ROOT / "static" / "shared.js")


def test_a_non_static_path_is_proxied(overlay):
    r = sv.resolve(request_path="/api/browse/albums",
                   static_root=_ROOT / "static", overlay_root=overlay)
    assert r.kind == "proxy"


def test_the_page_itself_is_proxied_so_the_real_template_renders(overlay):
    """Serving our own HTML would mean measuring a page the app never built."""
    r = sv.resolve(request_path="/", static_root=_ROOT / "static",
                   overlay_root=overlay)
    assert r.kind == "proxy"


def test_a_missing_static_file_is_not_silently_proxied(overlay):
    """A typo'd static path proxying upstream would return the app's 404 page
    with a 200-shaped body and look like a working asset."""
    r = sv.resolve(request_path="/static/does-not-exist.js",
                   static_root=_ROOT / "static", overlay_root=overlay)
    assert r.kind == "missing"


# ── traversal: the overlay must not become a file-read primitive ────────────

@pytest.mark.parametrize("attack", [
    "/static/../../../../etc/passwd",
    "/static/..%2f..%2fsecrets",
    "/static/browse/../../../app/database.py",
    "/static/browse/%2e%2e/%2e%2e/app/main.py",
])
def test_paths_escaping_the_roots_are_refused(attack, overlay):
    """Refused, not quietly rewritten.

    `normpath` would absorb each of these into a harmless lookup inside the
    root, which is safe but turns an escape attempt into an ordinary 404 --
    indistinguishable from a typo in a log. The resolver rejects the `..`
    segment before normalisation sees it.
    """
    r = sv.resolve(request_path=attack, static_root=_ROOT / "static",
                   overlay_root=overlay)
    assert r.kind == "refused", f"{attack} resolved to {r.kind}"


def test_quadruple_dot_is_a_literal_name_and_resolves_missing_not_refused():
    """A negative control for a bypass that does not apply here.

    `....//` defeats servers that *strip* `../` from a path, because stripping
    leaves `../`. This resolver rejects `..` segments outright instead of
    stripping them, so four dots are simply a directory name -- and one that
    does not exist. "Missing" is the correct answer, and asserting "refused"
    here would be a guard demanding the wrong behaviour.
    """
    r = sv.resolve(request_path="/static//....//app/main.py",
                   static_root=_ROOT / "static", overlay_root=None)
    assert r.kind == "missing"


def test_a_legitimate_nested_static_path_is_not_refused(overlay):
    r = sv.resolve(request_path="/static/browse/rail.css",
                   static_root=_ROOT / "static", overlay_root=overlay)
    assert r.kind == "static"


# ── isolation headers ───────────────────────────────────────────────────────

def test_the_harness_is_served_cross_origin_isolated():
    hdrs = sv.headers_for("/static/browse/index.js")
    assert hdrs["Cross-Origin-Opener-Policy"] == "same-origin"
    assert hdrs["Cross-Origin-Embedder-Policy"] == "require-corp"


def test_subresources_carry_a_resource_policy_so_isolation_does_not_block_them():
    """The second-order trap: require-corp blocks the harness's own art unless
    the same server marks it same-origin. An isolated page that renders no art
    is not the page under test."""
    hdrs = sv.headers_for("/api/art/1234")
    assert hdrs["Cross-Origin-Resource-Policy"] == "same-origin"


def test_every_response_carries_the_resource_policy():
    for path in ("/static/shared.js", "/api/art/1", "/"):
        assert "Cross-Origin-Resource-Policy" in sv.headers_for(path)


def test_static_responses_are_not_cached_so_a_repatch_is_never_stale():
    """A canary swapped between arms must not be served from cache -- that would
    silently measure the previous build."""
    hdrs = sv.headers_for("/static/browse/index.js")
    assert "no-store" in hdrs.get("Cache-Control", "")


# ── the upstream target is never defaulted ─────────────────────────────────

def test_the_server_refuses_to_start_without_an_explicit_upstream():
    with pytest.raises(ValueError, match="upstream|target"):
        sv.make_server(upstream="", overlay_root=None, static_root=_ROOT / "static")


def test_the_module_contains_no_hardcoded_address():
    """Mirrors the hygiene suite's rule, asserted here too because this is the
    one rig file whose whole job is talking to a host."""
    import re
    text = (_ROOT / "tools" / "clientrig" / "serve.py").read_text(encoding="utf-8")

    # By shape, not by this installation's actual values: naming them here would
    # publish them, and a probe for one known host misses every other one.
    private_ip = re.compile(
        r"\b(?:192\.168|10|172\.(?:1[6-9]|2\d|3[01]))\."
        r"\d{1,3}\.\d{1,3}\b")
    assert not private_ip.search(text), (
        f"private address in serve.py: {private_ip.search(text).group(0)!r}")

    local_host = re.compile(r"\b[a-z0-9-]+\.(?:lan|local|home|internal)\b", re.I)
    assert not local_host.search(text), (
        f"local hostname in serve.py: {local_host.search(text).group(0)!r}")


# ── end to end on a loopback port, torn down in a finally ──────────────────

def test_a_running_server_serves_the_overlay_with_isolation_headers(overlay):
    """One real request through the actual handler.

    Bound to loopback on an ephemeral port and shut down in a `finally`, because
    this repo's recorded incidents are 120 and 140 orphaned processes.
    """
    httpd, port = sv.make_server(upstream="http://127.0.0.1:1",
                                 overlay_root=overlay,
                                 static_root=_ROOT / "static")
    import threading
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/static/browse/index.js",
                timeout=10) as resp:
            body = resp.read().decode("utf-8")
            assert "PATCHED BY CANARY" in body
            assert resp.headers["Cross-Origin-Opener-Policy"] == "same-origin"
            assert resp.headers["Cross-Origin-Embedder-Policy"] == "require-corp"
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


def test_a_running_server_refuses_a_traversal_attempt(overlay):
    httpd, port = sv.make_server(upstream="http://127.0.0.1:1",
                                 overlay_root=overlay,
                                 static_root=_ROOT / "static")
    import threading
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/static/../../app/main.py", timeout=10)
        assert exc.value.code in (400, 403, 404)
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)
