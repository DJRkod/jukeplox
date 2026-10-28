"""Package-anchored path resolution (native-install plan U1, R7).

The container runs from ``WORKDIR /app`` so a CWD-relative ``static`` and
``app/templates`` always resolved; a native install runs from wherever
systemd puts it. These tests pin that the app finds its own files regardless
of the process working directory, that ``app/paths.py`` is the single
anchor every path site shares, and that the Snapcast config path can be
overridden through the environment like the binaries already can.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _run_in_cwd(cwd: Path, code: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")
    return subprocess.run(
        [sys.executable, "-c", code], cwd=str(cwd), env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )


# ── import from another CWD (AE4) ─────────────────────────────────────────────


def test_app_imports_from_a_foreign_cwd(tmp_path):
    """Starlette's StaticFiles checks the directory at import time; a
    CWD-relative ``static`` makes ``import app.main`` raise from any other
    directory — which is exactly where a systemd unit may start it."""
    proc = _run_in_cwd(tmp_path, "import app.main; print('imported')")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "imported" in proc.stdout


def test_pages_and_static_serve_from_a_foreign_cwd(tmp_path, monkeypatch):
    """Covers AE4: templates and static assets resolve relative to the
    package, not the working directory."""
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.chdir(tmp_path)
    client = TestClient(app, raise_server_exceptions=True)
    assert client.get("/").status_code == 200  # guest template via TEMPLATES_DIR
    resp = client.get("/static/shared.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers.get("content-type", "")


# ── one anchor module ─────────────────────────────────────────────────────────


def test_paths_module_anchors_on_the_package_root():
    from app import paths

    assert paths.ROOT == ROOT
    assert paths.STATIC_DIR == ROOT / "static"
    assert paths.TEMPLATES_DIR == ROOT / "app" / "templates"
    assert paths.CONFIG_DIR == ROOT / "config"
    for d in (paths.STATIC_DIR, paths.TEMPLATES_DIR, paths.CONFIG_DIR):
        assert d.is_dir(), d


def test_assets_static_dir_is_the_shared_anchor():
    """The cache-buster fingerprint and the mount must read the same tree."""
    from app import assets, paths

    assert assets.STATIC_DIR == paths.STATIC_DIR


def test_app_package_lives_under_the_anchored_root():
    """KTD2 invariant: ``app`` must import from the checkout (the working
    directory copy in the image), never from site-packages — otherwise the
    anchor points at a tree with no ``static/``."""
    import app
    from app import paths

    assert Path(app.__file__).resolve().is_relative_to(paths.ROOT)


def test_paths_module_imports_nothing_from_app():
    """``app.assets`` imports ``paths``; a reverse edge would be a cycle."""
    src = (ROOT / "app" / "paths.py").read_text(encoding="utf-8")
    assert "from app" not in src and "import app" not in src


# ── Snapcast config path override (R12, KTD3) ─────────────────────────────────


def test_snapserver_conf_default_is_install_relative(monkeypatch):
    monkeypatch.delenv("JUKEPLOX_SNAPSERVER_CONF", raising=False)
    from app import paths
    from app.output.snapcast_server import SnapserverSupervisor, snapserver_conf

    assert snapserver_conf() == str(paths.CONFIG_DIR / "snapserver.conf")
    sup = SnapserverSupervisor(source_name="jukeplox")
    assert sup._config_path == str(paths.CONFIG_DIR / "snapserver.conf")


def test_snapserver_conf_env_override_applies_at_construction(monkeypatch):
    """The override is read when the supervisor is built, not when the
    module was imported — a module-level default would ignore it."""
    from app.output.snapcast_server import SnapserverSupervisor

    monkeypatch.setenv("JUKEPLOX_SNAPSERVER_CONF", "/etc/jukeplox/snapserver.conf")
    sup = SnapserverSupervisor(source_name="jukeplox")
    assert sup._config_path == "/etc/jukeplox/snapserver.conf"


def test_snapserver_conf_explicit_argument_still_wins(monkeypatch):
    from app.output.snapcast_server import SnapserverSupervisor

    monkeypatch.setenv("JUKEPLOX_SNAPSERVER_CONF", "/etc/jukeplox/snapserver.conf")
    sup = SnapserverSupervisor(source_name="jukeplox", config_path="/x/y.conf")
    assert sup._config_path == "/x/y.conf"


# ── native-aware wording (KTD13) ──────────────────────────────────────────────


def test_airplay_missing_binaries_message_names_issue_90():
    from app.output import airplay

    msg = airplay.missing_binaries_message()
    assert "#90" in msg
    assert "JUKEPLOX_CLIAP2_BIN" in msg


@pytest.mark.parametrize("needle", ["--network host", "mount /run/dbus/system_bus_socket"])
def test_discovery_banner_also_speaks_to_native_installs(needle):
    """The banner may keep its Docker advice, but it must no longer be the
    only advice: a native box needs 'start avahi', not 'mount a socket'."""
    js = (ROOT / "static" / "admin" / "app.js").read_text(encoding="utf-8")
    assert needle in js  # Docker guidance retained
    assert "avahi-daemon" in js and "native" in js.lower()


def test_build_info_docstring_no_longer_calls_unknown_the_native_signal():
    src = (ROOT / "app" / "_build_info.py").read_text(encoding="utf-8")
    assert "native" in src.lower()
