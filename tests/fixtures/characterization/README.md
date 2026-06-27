# Characterization Artifacts

This directory holds a URL-set baseline for characterizing the browse/queue
JS surface across refactors. Used by U2 and U3 of
`docs/plans/2026-06-05-004-refactor-guest-admin-browse-unification-plan.md`.

## Why URL extraction instead of HAR captures

The original plan called for full HAR + DOM captures from a browser, which
turned out to be the wrong mechanism:

- A real browser HAR of the Jukeplox UI is **~425 MB / ~1,800 entries**
  because album art (`/api/art`) dominates the entry list and "Save all as
  HAR with content" embeds base64 response bodies.
- Diffing two 425 MB HARs would surface image-cache reshuffling and timing
  jitter, drowning the structural API-contract shifts the refactor might
  introduce.
- Manual capture introduces user error (the smoke sequence varies between
  captures, response bodies differ even when the API contract doesn't).

What actually needs verifying after a JS refactor is the **set of URLs the
frontend calls** — same endpoints, same methods, same parameter shapes. The
backend tests (`tests/test_api_*`) already verify endpoint behavior; the
characterization needs to confirm the frontend still uses the same set.

`scripts/extract_browse_urls.py` does this with a ~30-line regex over the
JS sources. Pre/post diffing the output catches "the JS stopped calling
endpoint X" or "the JS started calling new endpoint Y" without browser
overhead.

## Files

- `urls-baseline.txt` — URL set extracted from the JS as of U1 of plan 004
  (the pre-U2 baseline). Refreshed when the plan reaches the post-U3
  characterization step.

## Refresh procedure

Before a structural JS refactor that touches `static/{guest,admin}/app.js`,
`static/shared.js`, or `static/browse/`:

```bash
python scripts/extract_browse_urls.py > tests/fixtures/characterization/urls-baseline.txt
git diff tests/fixtures/characterization/urls-baseline.txt
```

If the diff is empty, the refactor preserved the URL set. If non-empty,
inspect every added/removed URL — each one is either an intentional change
(commit the new baseline) or a regression (fix the JS).

## What this DOES NOT catch

- **Visual regression.** A refactor that breaks the CSS or DOM rendering
  produces the same URL set but a broken UI. Caught by post-deploy smoke,
  not by this baseline.
- **Request-body shape drift.** If the JS starts POSTing
  `{album_id, source: "Host"}` instead of `{album_id, source_server_name: "Host"}`,
  the URL is the same and this baseline doesn't flag it. Caught by the
  backend's Pydantic validation rejecting the wrong-shape body.
- **Dynamic URLs.** The regex only catches literal URL prefixes. If the
  JS starts building URLs via string concatenation (`'/api/' + verb + '/...'`),
  the dynamic URL won't appear in the baseline. Verified during planning
  that Jukeplox's JS uses literal prefixes only; deviate from that and the
  baseline loses coverage.

## When the script's pattern needs to evolve

The regex is intentionally minimal. Extend it when:

- The frontend adds a new endpoint family beyond `/api/`, `/admin/`, `/static/`
- The frontend starts building URLs in a way the regex doesn't match (e.g.,
  template literals with non-literal prefixes)

The script lives at `scripts/extract_browse_urls.py` — single source of
truth for the extraction logic.
