"""Static-asset cache-buster — a single build-derived token appended to every
`/static/` URL as `?v=<token>`, replacing the hand-maintained `?v=N` numbers
that repeatedly went stale (see
docs/solutions/ui-bugs/stale-cache-busters-mixed-assets-pane-blowouts.md and
docs/plans/2026-06-16-006-refactor-auto-cache-busting-plan.md).

Token source:
- **Production (image build):** the git SHA baked in by the Dockerfile
  (`app/_build_info.py` `GIT_SHA`). Every deploy carries a new SHA, so a deploy
  that ships changed bytes always ships a new buster — "bytes changed but buster
  didn't" cannot happen as long as the image is built with the `GIT_SHA`
  build-arg (the project Docker standard).
- **Dev (no image):** a content hash over the static files' bytes. A content
  hash (not size/mtime) busts on same-size edits and is immune to Docker `COPY`
  mtime normalization.

If `GIT_SHA` is the `"unknown"` sentinel but other build-info values are set,
the image was built without the `GIT_SHA` build-arg — that is a mis-built image,
so we log a WARNING and fall back to the content hash rather than freezing the
literal `"unknown"` as the buster.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from app import _build_info

_log = logging.getLogger(__name__)

# app/assets.py → parents[1] is the repo root, where `static/` lives (mirrors
# `app.mount("/static", StaticFiles(directory="static"))` under the image's
# WORKDIR /app). Anchored to the file, NOT the process CWD, so the dev
# fingerprint reads the real files regardless of where the app is launched.
STATIC_DIR = Path(__file__).resolve().parents[1] / "static"

# Fallback when the static dir can't be read at all — never raise at import.
_FALLBACK = "dev"


def static_fingerprint(static_dir: Path | None = None) -> str:
    """Short content hash over every file's relative path + bytes under the
    static dir. Stable within a process; changes whenever any static byte
    changes (including same-size edits)."""
    root = static_dir if static_dir is not None else STATIC_DIR
    if not root.is_dir():
        # rglob() on a missing path yields nothing (no error), which would hash
        # to the empty digest and masquerade as a real fingerprint — guard
        # explicitly so a missing static dir hits the fallback instead.
        _log.warning("asset fingerprint: %s is not a directory — using fallback buster", root)
        return _FALLBACK
    try:
        h = hashlib.sha256()
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            h.update(path.relative_to(root).as_posix().encode("utf-8"))
            h.update(b"\0")
            h.update(path.read_bytes())
        return h.hexdigest()[:12]
    except OSError:
        _log.warning("asset fingerprint: could not read %s — using fallback buster", root)
        return _FALLBACK


def compute_version() -> str:
    """Resolve the cache-buster token: git SHA in an image build, content hash
    otherwise. Warns when an image build is missing its GIT_SHA build-arg."""
    sha = _build_info.GIT_SHA
    if sha and sha != "unknown":
        return sha
    # GIT_SHA is the sentinel. If BUILD_TIME/IMAGE_TAG are set, this is an image
    # build that forgot --build-arg GIT_SHA — surface it loudly.
    if _build_info.BUILD_TIME != "unknown" or _build_info.IMAGE_TAG != "unknown":
        _log.warning(
            "JUKEPLOX_GIT_SHA is unset on an image build (BUILD_TIME/IMAGE_TAG are "
            "set) — asset cache-buster falls back to a content fingerprint. Pass "
            "--build-arg GIT_SHA=$(git rev-parse --short HEAD) to the image build."
        )
    return static_fingerprint()


# Computed once at import; reused for every render (never per-request).
ASSET_VERSION: str = compute_version()


def register(templates) -> None:
    """Install the `asset_v` global on a Jinja2Templates env so templates can
    write `?v={{ asset_v }}` on every static asset URL."""
    templates.env.globals["asset_v"] = ASSET_VERSION
