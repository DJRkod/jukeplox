"""Tests for the build-derived static-asset cache-buster (app/assets.py).

Covers the 2026-06-16 auto-cache-busting plan U1: token source selection
(git SHA in an image build, content hash in dev), the mis-built-image warning,
content-hash correctness (busts same-size edits), and Jinja-global registration.
"""

import logging

from fastapi.templating import Jinja2Templates

from app import assets


def test_version_is_git_sha_when_known(monkeypatch):
    monkeypatch.setattr(assets._build_info, "GIT_SHA", "abc1234")
    assert assets.compute_version() == "abc1234"


def test_version_falls_back_to_content_hash_when_sha_unknown(monkeypatch):
    monkeypatch.setattr(assets._build_info, "GIT_SHA", "unknown")
    monkeypatch.setattr(assets._build_info, "BUILD_TIME", "unknown")
    monkeypatch.setattr(assets._build_info, "IMAGE_TAG", "unknown")
    token = assets.compute_version()
    assert token and token != "unknown"


def test_content_hash_changes_on_same_size_edit(tmp_path):
    """The dev-correctness guarantee: a same-length byte change still moves the
    token — which a (size, mtime) fingerprint would miss."""
    f = tmp_path / "app.js"
    f.write_text("AAAA")
    first = assets.static_fingerprint(tmp_path)
    f.write_text("BBBB")  # same length, different bytes
    second = assets.static_fingerprint(tmp_path)
    assert first != second


def test_content_hash_stable_without_changes(tmp_path):
    (tmp_path / "app.js").write_text("console.log(1)")
    assert assets.static_fingerprint(tmp_path) == assets.static_fingerprint(tmp_path)


def test_missing_static_dir_falls_back_not_raises(tmp_path):
    assert assets.static_fingerprint(tmp_path / "does-not-exist") == assets._FALLBACK


def test_warns_when_image_build_missing_sha(monkeypatch, caplog):
    monkeypatch.setattr(assets._build_info, "GIT_SHA", "unknown")
    monkeypatch.setattr(assets._build_info, "BUILD_TIME", "2026-06-16T00:00Z")
    monkeypatch.setattr(assets._build_info, "IMAGE_TAG", "latest")
    with caplog.at_level(logging.WARNING, logger="app.assets"):
        assets.compute_version()
    assert any("GIT_SHA" in r.message for r in caplog.records)


def test_no_warning_in_pure_dev(monkeypatch, caplog):
    monkeypatch.setattr(assets._build_info, "GIT_SHA", "unknown")
    monkeypatch.setattr(assets._build_info, "BUILD_TIME", "unknown")
    monkeypatch.setattr(assets._build_info, "IMAGE_TAG", "unknown")
    with caplog.at_level(logging.WARNING, logger="app.assets"):
        assets.compute_version()
    assert not any("GIT_SHA" in r.message for r in caplog.records)


def test_register_installs_asset_v_global():
    templates = Jinja2Templates(directory="app/templates")
    assert "asset_v" not in templates.env.globals
    assets.register(templates)
    assert templates.env.globals["asset_v"] == assets.ASSET_VERSION


# ── HTML responses must revalidate (2026-07-17 ce-debug) ──────────────────────
# The ?v=<sha> cache-buster only works if the HTML that references it is
# fresh. The template routes shipped with NO cache headers, so browsers
# (phones especially) could keep serving a stale page — and with it a stale
# JS bundle — across deploys: a guest device kept running the pre-fix broad
# search cascade after the fixed image shipped, and one such device's
# cascade drove live Tier-1 searches to 40+ seconds for every guest.
# Cache-Control: no-cache forces revalidation on every load, so a plain
# reload always picks up the new asset_v.


def _html_no_cache(path):
    from fastapi.testclient import TestClient
    from app.main import app
    resp = TestClient(app, raise_server_exceptions=True).get(path)
    assert resp.status_code == 200, path
    assert "text/html" in resp.headers.get("content-type", ""), path
    assert resp.headers.get("cache-control") == "no-cache", (
        f"{path} must send Cache-Control: no-cache — a cached page pins a "
        f"stale JS bundle across deploys (the ?v= buster can't fire)."
    )


def test_guest_index_html_is_no_cache():
    _html_no_cache("/")


def test_admin_login_html_is_no_cache():
    from unittest.mock import AsyncMock, patch
    with patch("app.database.get_setting", AsyncMock(return_value="1")):
        _html_no_cache("/admin/login")


def test_admin_dashboard_html_is_no_cache():
    # The authenticated dashboard route also received Cache-Control: no-cache
    # in the same 2026-07-17 pass; pin it directly (the redirect-to-login path
    # would otherwise mask a regression, since the login page is no-cache too).
    from unittest.mock import AsyncMock, patch
    from fastapi.testclient import TestClient
    from app.main import app
    c = TestClient(app, raise_server_exceptions=True)
    c.cookies.set("jukeplox_session", "x")
    with patch("app.auth.session.validate_session", AsyncMock(return_value=True)):
        resp = c.get("/admin", follow_redirects=False)
    assert resp.status_code == 200, "authed /admin must render, not redirect to login"
    assert "text/html" in resp.headers.get("content-type", ""), "/admin"
    assert resp.headers.get("cache-control") == "no-cache", (
        "/admin dashboard must send Cache-Control: no-cache — a cached page pins "
        "a stale JS bundle across deploys (the ?v= buster can't fire)."
    )
