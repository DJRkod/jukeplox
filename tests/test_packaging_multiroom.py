"""U3 — packaging gates for the baked-in server-fed deps.

Two tiers:
- Repo-inspectable gates (always run in the dev env): the exact-pin declarations
  in pyproject.toml, the Dockerfile apt install + version-range build gate, and
  the stub snapserver.conf. These catch a dropped pin / removed gate in CI on any
  platform without needing the image.
- In-image gates (skip when the binary/libs are absent, i.e. the Windows dev env):
  ``import snapcast`` / ``import aiosendspin`` succeed, and ``snapserver --version``
  resolves inside [0.27, 0.30). R16's "deps baked in" is only PROVEN when these
  pass inside the built image (a bare skip does not count).
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# ── repo-inspectable gates (always run) ──────────────────────────────────────


def test_pyproject_exact_pins_multiroom_libs():
    txt = _read("pyproject.toml")
    # Exact pins ("==") — NOT ">=" — per the plan (aiosendspin churn / snapcast
    # version-sensitive control surface).
    assert re.search(r'"snapcast==\d', txt), "snapcast must be exact-pinned"
    assert re.search(r'"aiosendspin\[server\]==\d', txt), \
        "aiosendspin[server] must be exact-pinned"


def test_dockerfile_installs_snapserver_and_snapclient():
    df = _read("Dockerfile")
    # Both the server and the software test receiver are baked in.
    assert re.search(r"^\s*snapserver\s*\\?\s*$", df, re.M), "snapserver apt line missing"
    assert re.search(r"^\s*snapclient\s*\\?\s*$", df, re.M), "snapclient apt line missing"


def test_dockerfile_has_snapserver_version_gate():
    df = _read("Dockerfile")
    assert "snapserver --version" in df, "build-time version gate missing"
    # The gate must fail the build on 0.30.x and below the floor (bookworm=0.26).
    assert "0.26" in df and "0.30" in df, "version-range bounds missing from the gate"


def test_dockerfile_ships_stub_conf():
    df = _read("Dockerfile")
    assert re.search(r"COPY\s+config/\s+\./config/", df), "config/ COPY missing"


def test_stub_snapserver_conf_present_and_empty_sections():
    conf = _read("config/snapserver.conf")
    # Only empty section headers — all real config is CLI-supplied (U4).
    assert "[stream]" in conf
    # No actual directive lines (key = value) leaked into the stub — every
    # non-comment, non-blank line must be a bare [section] header.
    for line in conf.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        assert s.startswith("[") and s.endswith("]"), \
            f"stub conf must contain only section headers, got: {line!r}"


def test_third_party_licenses_attribute_multiroom():
    lic = _read("THIRD_PARTY_LICENSES")
    assert "snapserver" in lic and "GPL-3.0-or-later" in lic
    assert "aggregation" in lic.lower()
    assert "aiosendspin" in lic and "Apache-2.0" in lic
    assert "PyAV" in lic


# ── in-image gates (skip when the binary/libs are absent) ────────────────────


def test_snapcast_lib_importable():
    pytest.importorskip("snapcast", reason="snapcast lib only present in the built image")


def test_aiosendspin_lib_importable():
    pytest.importorskip("aiosendspin", reason="aiosendspin only present in the built image")


def test_snapserver_version_in_range():
    binpath = shutil.which("snapserver")
    if not binpath:
        pytest.skip("snapserver not installed in this env (in-image gate)")
    out = subprocess.run([binpath, "--version"], capture_output=True, text=True)
    blob = (out.stdout or "") + (out.stderr or "")
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", blob)
    assert m, f"could not parse snapserver version from: {blob!r}"
    major, minor = int(m.group(1)), int(m.group(2))
    assert major == 0 and 26 <= minor < 30, f"snapserver {m.group(0)} outside [0.26, 0.30)"
