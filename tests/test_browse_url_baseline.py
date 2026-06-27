"""Characterization test: the extracted browse URL set must match the
baseline saved in `tests/fixtures/characterization/urls-baseline.txt`.

`scripts/extract_browse_urls.py` walks the four frontend JS files and
returns the set of `/api/`, `/admin/`, and `/static/` URL literals it
finds. The baseline was captured before the guest/admin browse
unification refactor — any change to the URL set means the frontend's
API surface from the browser's POV has shifted, which a refactor (vs a
feature) should not do.

Failure modes this test surfaces:
- Refactor accidentally drops a route call (`/api/browse/years` missing
  → baseline has it, extracted doesn't → diff shows removal).
- Refactor accidentally adds a new route call without intending to
  (e.g., a copy/paste introduces a spurious `/api/foo` literal → diff
  shows addition).

When you intentionally change the API surface (real feature work, not
refactoring), update the baseline file via:

    python scripts/extract_browse_urls.py > tests/fixtures/characterization/urls-baseline.txt

Include the diff in the commit so the URL-surface change is visible at
review time.
"""

from pathlib import Path

from scripts.extract_browse_urls import extract_urls

ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "tests/fixtures/characterization/urls-baseline.txt"


def _load_baseline() -> set[str]:
    """Read the baseline file; one URL per line, blank lines ignored."""
    text = BASELINE_PATH.read_text(encoding="utf-8")
    return {line.strip() for line in text.splitlines() if line.strip()}


def test_extracted_browse_urls_match_baseline():
    """Extracted URL set equals the saved baseline.

    On failure, the assertion message prints the added/removed URLs so
    the diff is immediately visible without rerunning extraction by hand.
    """
    extracted = set(extract_urls())
    baseline = _load_baseline()

    added = sorted(extracted - baseline)
    removed = sorted(baseline - extracted)

    if added or removed:
        lines = ["Browse URL set drifted from the saved baseline."]
        if added:
            lines.append("Added (in extracted, not in baseline):")
            lines.extend(f"  + {u}" for u in added)
        if removed:
            lines.append("Removed (in baseline, not in extracted):")
            lines.extend(f"  - {u}" for u in removed)
        lines.append(
            "If the change is intentional, refresh the baseline:\n"
            "  python scripts/extract_browse_urls.py > "
            "tests/fixtures/characterization/urls-baseline.txt"
        )
        assert False, "\n".join(lines)
