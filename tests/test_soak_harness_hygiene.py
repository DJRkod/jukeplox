"""Committed tooling must carry no target host and no credential.

Porting a harness from a scratch copy is exactly when a rig address or a
password gets left behind, and this repo mirrors to a public GitHub remote —
so the mistake is not recoverable by deleting the file later. Everything the
harness needs comes from the environment; this test is the standing check that
it stayed that way.

**The positive controls below are the load-bearing part.** The first version of
this file only asserted that the (already clean) harness files did not match,
which a regex matching *nothing* would also satisfy — and one did: `\\b` does not
match after an underscore, so `ADMIN_PW = "..."` sailed straight through while
`password = "..."` was caught. Those are the exact names the harness uses. A
detector with no known-bad fixture is not a detector, it is a witness that
always says yes (see docs/solutions/workflow-issues/
isolated-repro-harness-false-witness.md).

The harness itself is not unit-tested: it drives a live deployment through a
real browser, and asserting anything about it without one would assert nothing.
Its correctness is established by running it (see tools/soak/README.md).
"""

import io
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HARNESS_DIR = REPO / "tools" / "soak"
# The whole of tools/ is scanned, not just tools/soak/. browse-bench.mjs sat in
# mirrored code with a hard-coded 192.168.x default for months precisely because
# the guard was scoped to one subdirectory.
TOOLS_DIR = REPO / "tools"

# Binary/vendored things a text scan cannot meaningfully read. Everything else
# under tools/soak/ is scanned — an allowlist of extensions would silently skip
# the .json/.env/.ps1 files a leaked secret is most likely to land in.
_SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip",
                  ".gz", ".woff", ".woff2", ".ttf", ".jsonl"}
_SKIP_DIRS = {"node_modules", "__pycache__", ".git"}

# Address literals that pin the harness to somebody's actual network.
# NB: every fixture below is synthetic. Real rig addresses and fingerprints are
# denylisted for the public mirror, and a test fixture is still a committed
# string — this file would have shipped the rig's host key otherwise.
# Documentation ranges (192.0.2.x, 198.51.100.x, 203.0.113.x) stay allowed for
# examples; 127.x and localhost are fine as they are not anyone's LAN.
_HOST_LITERAL = re.compile(
    r"\b(?:"
    r"192\.168\.\d{1,3}\.\d{1,3}"                      # RFC1918
    r"|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"                  # RFC1918
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"    # RFC1918
    r"|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}"  # CGNAT/Tailscale
    r"|169\.254\.\d{1,3}\.\d{1,3}"                     # link-local
    r")\b"
)

# A credential written into the file rather than read from the environment.
# The prefix group is what the original `\b` got wrong: secret names in this
# harness are all prefixed (ADMIN_PW, JP_PW, JP_HOSTKEY), and a word boundary
# never fires after an underscore.
_ASSIGNED_SECRET = re.compile(
    r"""(?ix)
    (?:^|[^A-Za-z0-9])
    (?:[A-Za-z0-9]*[_-])?
    (?:password|passwd|pwd|pw|pass|token|secret|credential|apikey|api_key
       |hostkey|privkey|private_key)
    \s*[:=]\s*
    (?P<q>['"])(?P<val>[^'"]{3,})(?P=q)
    """
)

# Secret shapes that need no assignment to be a leak.
_BARE_SECRET = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|ssh-(?:rsa|ed25519|dss) AAAA[0-9A-Za-z+/]{20,}"
    r"|SHA256:[0-9A-Za-z+/]{43}"
    r"|://[^/\s:@]+:[^/\s:@]+@"                        # user:pass@host in a URL
)


def _is_env_read(value: str) -> bool:
    """Reading the secret from the environment is the whole point."""
    v = value.strip()
    return (
        v.startswith("JP_")
        or v.startswith("$")
        or v.startswith("%")
        or v in {"", "...", "Content-Type", "application/json"}
    )


def _files_under(root: Path):
    assert root.is_dir(), f"missing {root}"
    out = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        if p.suffix.lower() in _SKIP_SUFFIXES:
            continue
        out.append(p)
    return sorted(out)


def _harness_files():
    return _files_under(HARNESS_DIR)


def _tool_files():
    return _files_under(TOOLS_DIR)


def _scan(text: str) -> list[str]:
    """Every violation in *text*, as human-readable strings."""
    hits = [f"host literal: {m}" for m in _HOST_LITERAL.findall(text)]
    hits += [f"bare secret: {m.group(0)[:40]}" for m in _BARE_SECRET.finditer(text)]
    for m in _ASSIGNED_SECRET.finditer(text):
        if not _is_env_read(m.group("val")):
            hits.append(f"assigned secret: {m.group(0).strip()[:60]}")
    return hits


# ── Positive controls: prove the detector detects ─────────────────────────────

_KNOWN_BAD = [
    # The exact names this harness uses — all missed by the original `\b` form.
    'ADMIN_PW = "hunter2xx"',
    'JP_PW = "hunter2xx"',
    'JP_HOSTKEY = "AAAAB3NzaC1yc2EAAAADAQABAAABgQCsyntheticexampl"',
    'admin_password = "hunter2xx"',
    'ADMIN_TOKEN = "abc123def456"',
    'my_secret = "abc123def456"',
    "pwd = 'abc123def456'",
    'PASS = "abc123def456"',
    # Unprefixed forms.
    'password = "hunter2xx"',
    'token: "abc123def456"',
    # Address literals.
    'BASE = "http://192.168.1.77"',
    'BASE = "http://10.1.2.3"',
    'BASE = "http://172.16.9.9"',
    'BASE = "http://100.101.102.103"',
    # Assignment-free shapes.
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKxxxxxxxxxxxxxxxxxxxx",
    "url = 'https://admin:hunter2@jukebox.local'",
]

_KNOWN_GOOD = [
    'BASE = os.environ.get("JP_BASE", "")',
    "const BASE = process.env.JP_BASE;",
    'password = os.environ["JP_ADMIN_PW"]',
    '"-pw", os.environ["JP_PW"]',
    'BASE = "http://127.0.0.1:8000"',
    'BASE = "http://localhost"',
    'BASE = "http://jukebox.local"',
    'BASE = "http://192.0.2.10"',          # documentation range
    'headers={"Content-Type": "application/json"}',
]


@pytest.mark.parametrize("sample", _KNOWN_BAD)
def test_detector_catches_known_bad(sample):
    """If this fails, the guard below is decorative."""
    assert _scan(sample), f"detector missed a real leak shape: {sample!r}"


@pytest.mark.parametrize("sample", _KNOWN_GOOD)
def test_detector_allows_known_good(sample):
    assert not _scan(sample), f"detector false-positives on: {sample!r}"


# ── The guard itself ──────────────────────────────────────────────────────────

def test_harness_files_are_present():
    names = {p.name for p in _harness_files()}
    # The capability is only preserved if every piece of it landed.
    assert {"README.md", "cdp.mjs", "party.mjs", "jp_http.py", "monitor.py",
            "admin_chaos.py", "admin_toggles.py", "analyse.py"} <= names


@pytest.mark.parametrize("path", _tool_files(), ids=lambda p: p.name)
def test_committed_tooling_carries_no_host_or_credential(path):
    hits = _scan(io.open(path, encoding="utf-8", errors="replace").read())
    assert not hits, f"{path.name} would leak on the public mirror: {hits}"


@pytest.mark.parametrize(
    "path",
    [p for p in _tool_files() if p.suffix in {".py", ".mjs", ".js"}],
    ids=lambda p: p.name,
)
def test_harness_requires_its_target_from_the_environment(path):
    """No driver may fall back to a default target.

    Covers both the Python `os.environ.get("JP_BASE", "http://...")` shape and
    the JS `process.env.JP_BASE || 'http://...'` shape.
    """
    text = io.open(path, encoding="utf-8", errors="replace").read()
    if "JP_BASE" not in text:
        pytest.skip("not a driver that talks to the instance")
    py_default = re.search(r"""JP_BASE["']\s*,\s*["']\s*http""", text)
    js_default = re.search(r"""JP_BASE\s*\|\|\s*["']\s*http""", text)
    assert not py_default, f"{path.name} defaults JP_BASE (python form)"
    assert not js_default, f"{path.name} defaults JP_BASE (js form)"
