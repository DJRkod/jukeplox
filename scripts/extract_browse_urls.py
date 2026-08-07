#!/usr/bin/env python3
"""Extract URL literals from the frontend JS to characterize the API surface
the browse/queue UI hits.

Used as a lightweight characterization mechanism for refactors of
static/guest/app.js, static/admin/app.js, static/shared.js, and the
shared static/browse/ module: run before the refactor → save output as
baseline → run after → diff. Any change to the URL set is a signal the
refactor altered the API contract from the frontend's POV.

The pattern is intentionally simple: matches single/double/backtick-quoted
literals beginning with /api/, /admin/, or /static/. Does NOT catch URLs
built at runtime via string concatenation or template variable
interpolation (e.g., `/api/browse/albums/${id}/tracks` works because the
literal prefix is captured up to ${; `/api/browse/' + 'albums'` does not).
For Jukeplox's current frontend, every API URL is a literal prefix —
verified manually during the unification plan's research phase.

Usage:
    python3 scripts/extract_browse_urls.py > tests/fixtures/characterization/urls-baseline.txt
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS_FILES = [
    ROOT / "static/guest/app.js",
    ROOT / "static/admin/app.js",
    ROOT / "static/shared.js",
    ROOT / "static/browse/index.js",  # may not exist pre-U2
    ROOT / "static/playback/index.js",  # shared playback module (2026-06-09 layout plan)
]

# Capture a quoted literal that starts with /api/, /admin/, or /static/, up to
# the next quote, query-string marker, whitespace, or template-expr boundary.
URL_PATTERN = re.compile(r'''["'`](/(?:api|admin|static)/[^"'`?\s${}]+)''')


def extract_urls() -> list[str]:
    urls: set[str] = set()
    for path in JS_FILES:
        if not path.exists():
            continue
        for m in URL_PATTERN.finditer(path.read_text(encoding="utf-8")):
            urls.add(m.group(1))
    return sorted(urls)


if __name__ == "__main__":
    for url in extract_urls():
        print(url)
