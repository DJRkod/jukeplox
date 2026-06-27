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
