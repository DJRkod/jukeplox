"""Frontend regression checks born from the 2026-06-10 rail/desktop debug.

Three production bugs shipped because nothing enforced these invariants:

1. **Stale cache-busters** — static files were rewritten but kept their old
   `?v=N` in the templates, so deployed browsers ran mixed stale/new assets.
   The manual CACHE_MANIFEST that guarded this was retired 2026-06-16 after it
   rotted and shipped a stale admin.js (the Flood Control no-op); cache-busting
   is now build-derived (app/assets.py -> `asset_v`). The check below only
   asserts every /static/ ref is versioned; the exact-form guard lives in
   tests/test_static_discipline.py.
2. **Admin grid width blowout** — `#browse` sits in a `1fr` grid track;
   grid items default to `min-width: auto`, so one long nowrap
   `.list-title` (e.g. Fiona Apple's "When the Pawn…") forced the
   column ~390px past the viewport.
3. **Fixed admin list heights** — `max-height: 700px/480px` ignored the
   window, so panes overflowed short windows (and forced page-scroll
   rail traversal on phones).
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUEST_TEMPLATE = ROOT / "app/templates/guest/index.html"
ADMIN_TEMPLATE = ROOT / "app/templates/admin/dashboard.html"

# ── Check 1: every static ref carries a cache-buster ─────────────────────────
# Cache-busting is now automatic: app/assets.py derives a single token (the git
# SHA in an image build, else a content hash of the static files) and the
# templates write it as `?v={{ asset_v }}`. The old manual CACHE_MANIFEST
# (per-file version + sha256, kept in lockstep by hand) was retired 2026-06-16
# after it rotted and shipped a stale admin.js. Byte freshness is now guaranteed
# by the build, so the only remaining template invariant here is "no unversioned
# ref"; the exact `?v={{ asset_v }}` form is enforced in test_static_discipline.py.


def test_unversioned_static_refs_forbidden():
    """Every /static/ script or stylesheet reference must carry a ?v= —
    an unversioned reference can never be cache-busted at all."""
    bare = re.compile(r'(?:src|href)="(/static/[^"?]+)"')
    for template in (GUEST_TEMPLATE, ADMIN_TEMPLATE):
        source = template.read_text(encoding="utf-8")
        offenders = bare.findall(source)
        assert not offenders, (
            f"Unversioned /static/ reference(s) in {template.name}: {offenders}. "
            "Add ?v={{ asset_v }} so the build-derived cache-buster applies."
        )


# ── Check 2: admin grid item must be shrinkable ──────────────────────────────
# #browse lives in the desktop grid's 1fr track. Without min-width:0 the
# track's auto minimum is the widest nowrap .list-title, so one long album
# name ("When the Pawn…") blows the column past the viewport.


def test_admin_browse_grid_item_can_shrink():
    source = ADMIN_TEMPLATE.read_text(encoding="utf-8")
    rule = re.search(r"#pview-jukebox\s+#browse\s*\{([^}]*)\}", source)
    assert rule, "Expected a `#pview-jukebox #browse { … }` rule in dashboard.html"
    assert re.search(r"min-width:\s*0", rule.group(1)), (
        "`#pview-jukebox #browse` must declare `min-width: 0`. It occupies the "
        "desktop grid's 1fr track; grid items default to min-width:auto, so a "
        "single long nowrap .list-title forces the whole browse column wider "
        "than the viewport (the 2026-06-10 'vastly too wide' bug)."
    )


# ── Check 3: admin list heights must be viewport-relative ────────────────────


def test_admin_list_heights_are_viewport_relative():
    source = ADMIN_TEMPLATE.read_text(encoding="utf-8")
    offenders = []
    for m in re.finditer(
        r"#(artists-list|albums-list|search-results|years-list|genres-list)[^{]*\{([^}]*)\}",
        source,
    ):
        body = m.group(2)
        height = re.search(r"max-height:\s*([^;]+);", body)
        if height and "vh" not in height.group(1):
            offenders.append((m.group(1), height.group(1).strip()))
    assert not offenders, (
        f"Admin list container(s) with fixed (non-viewport) max-height: "
        f"{offenders}. Use a vh-based cap (e.g. max(320px, calc(100vh - "
        f"185px))) so panes track the window height instead of overflowing "
        f"short windows and forcing page-scroll on phones."
    )


# ── Check 4: rail reserves a content lane ────────────────────────────────────
# The rail previously sat at a hardcoded right:4px and overlapped the pane's
# native scrollbar and row chevrons by 26-36px. The activation code now
# publishes --rail-inset / --rail-lane on the host; rail.css must consume both.


def test_rail_css_positions_via_inset_var():
    rail_css = (ROOT / "static/browse/rail.css").read_text(encoding="utf-8")
    assert "var(--rail-inset" in rail_css, (
        ".alpha-rail must position via var(--rail-inset, …) so the rail "
        "clears the scroller's native scrollbar (2026-06-10 gutter fix)."
    )
    # 2026-06-24 browse-stall fix: the content lane is now direct inline padding
    # on the column, NOT an inherited --rail-lane custom property on the host —
    # that invalidated style for every cell (an O(N) recalc = the cold-render
    # stall, confirmed via tools/perf/browse-bench).
    assert "var(--rail-lane" not in rail_css, (
        "rail.css must not consume var(--rail-lane): an inherited host custom "
        "property forced an O(N) restyle of every cell. The lane is reserved as "
        "direct column padding-right in _railLaneRefresh."
    )


def test_rail_lane_reserved_without_host_custom_property():
    """Perf regression guard (2026-06-24 browse-stall): the rail gutter must not
    be published as a custom property on the host — custom properties inherit, so
    that restyles all N cells in one long task on the cold render. The lane is
    direct column padding-right; --rail-inset is set on the rail element itself."""
    browse_js = (ROOT / "static/browse/index.js").read_text(encoding="utf-8")
    assert "_railLaneRefresh" in browse_js, "the rail gutter refresher must exist."
    assert "--rail-lane" not in browse_js, (
        "no --rail-lane custom property: reserve the lane as direct column "
        "padding-right so only the column restyles, not every inheriting cell."
    )
    m = re.search(r"function _railLaneRefresh\(\)\s*\{(.*?)\n  \}", browse_js, re.S)
    assert m, "could not locate the _railLaneRefresh body"
    body = m.group(1)
    assert "host.style.setProperty" not in body, (
        "_railLaneRefresh must not set custom properties on the host (inherited → "
        "O(N) cell restyle); set --rail-inset on the rail element and the lane as "
        "padding-right on the column."
    )
    assert ".style.paddingRight" in body, (
        "the content lane must be reserved as direct column padding-right."
    )
    assert "_railSingleton.style.setProperty('--rail-inset'" in body, (
        "--rail-inset must be set on the rail element, not the host."
    )


# ── Sort-aware index rail (2026-06-20 plan) ───────────────────────────────────
# The rail re-indexes to match the active sort: letters (reversed for Z→A) for
# name sorts, an adaptive time ladder for Album year sorts, hidden for Most
# Played. These pins lock the resolver mapping, the adaptive cap, the generic
# bucket attribute, and single-source residence so the generalization can't
# silently regress or re-fork per page.


def test_rail_dimension_resolver_maps_sorts():
    browse = (ROOT / "static/browse/index.js").read_text(encoding="utf-8")
    assert "function resolveRailDimension(" in browse, (
        "static/browse/index.js must define resolveRailDimension(view, sort) — "
        "the single place that maps a sort to the rail's index dimension."
    )
    m = re.search(r"function resolveRailDimension\([^)]*\)\s*\{(.*?)\n  \}", browse, re.S)
    assert m, "resolveRailDimension body not found"
    body = m.group(1)
    assert "alpha_asc" in body and "alpha_desc" in body and "'letters'" in body, (
        "resolveRailDimension must map alpha_asc/alpha_desc to the letters dimension."
    )
    assert "year_asc" in body and "year_desc" in body and "'time'" in body and "'albums'" in body, (
        "resolveRailDimension must map Album year_asc/year_desc to the time dimension "
        "(Albums only — artists have no release year)."
    )
    assert "return null" in body, (
        "Most Played (popular) and unhandled sorts must hide the rail — "
        "resolveRailDimension returns null, which the renderers turn into _deactivateRail()."
    )
    # Both renderers must actually consume the resolver.
    assert "resolveRailDimension('artists'" in browse and "resolveRailDimension('albums'" in browse, (
        "renderArtistsItems and renderAlbumsItems must both resolve the rail dimension."
    )


def test_rail_adaptive_cap_is_25():
    browse = (ROOT / "static/browse/index.js").read_text(encoding="utf-8")
    assert re.search(r"RAIL_BUCKET_CAP\s*=\s*25\b", browse), (
        "The adaptive time-rail cap must be 25 (finest layout ≤ 25 buckets: "
        "years → 5-year bins → decades). Changing it changes everyday rail "
        "granularity — update the plan/brainstorm if intentional."
    )
    assert "function adaptiveTimeGranularity(" in browse and "function computeBuckets(" in browse, (
        "static/browse/index.js must define adaptiveTimeGranularity + computeBuckets."
    )


def test_rail_uses_generic_bucket_attribute():
    browse = (ROOT / "static/browse/index.js").read_text(encoding="utf-8")
    assert "data-bucket" in browse and "dataset.bucket" in browse, (
        "The rail must key on the generic data-bucket attribute so the same "
        "machinery serves letters and the time ladder."
    )
    # No silent half-rename: the old letter-only attribute is fully gone.
    assert "data-letter" not in browse and "dataset.letter" not in browse and "letterStart" not in browse, (
        "static/browse/index.js still references the old data-letter / letterStart "
        "attribute — the rail generalization must be a complete rename to data-bucket."
    )


def test_rail_dimension_logic_is_single_source():
    # Rail dimension logic lives in the shared module only — never forked into
    # the per-page apps (mirrors the static-discipline single-source rule).
    for rel in ("static/guest/app.js", "static/admin/app.js"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        for sym in ("resolveRailDimension", "computeBuckets", "adaptiveTimeGranularity", "RAIL_BUCKET_CAP"):
            assert sym not in src, (
                f"{rel} must not define/duplicate rail symbol {sym!r}; it belongs "
                "in static/browse/index.js (single source)."
            )


# ── Sort control: label normalization, de-duplication, opaque overlay ─────────
# The 2026-06-17 sort-labels-themed-dropdown plan replaced the native <select>
# sort rows (the byte-identical makeSortRow in BOTH shared.js and browse/index.js)
# with one shared themed popover (createSortControl), and normalized the year
# labels. These pins lock that in so a future edit can't silently regress the
# vocabulary, re-fork the builder, or drop the opaque-overlay treatment that
# keeps the popover legible on translucent-surface schemes.

SHARED_JS = ROOT / "static/shared.js"
BROWSE_JS = ROOT / "static/browse/index.js"
_SORT_JS_FILES = (SHARED_JS, BROWSE_JS)

_OLD_SORT_LABELS = ("Chronological", "Reverse Chronological", "Year ↓", "Year ↑")
_NEW_SORT_LABELS = ("Earliest → Latest", "Latest → Earliest")


def test_sort_year_labels_normalized():
    for path in _SORT_JS_FILES:
        src = path.read_text(encoding="utf-8")
        for old in _OLD_SORT_LABELS:
            assert old not in src, (
                f"Stale sort label {old!r} still in {path.name}. The year sorts "
                f"must read 'Earliest → Latest' / 'Latest → Earliest' everywhere "
                f"(2026-06-17 sort-labels plan R1/R2)."
            )
        for new in _NEW_SORT_LABELS:
            assert new in src, (
                f"Expected normalized sort label {new!r} in {path.name} — the "
                f"same sort must read the same way on every screen."
            )


def test_sort_control_is_single_source():
    shared = SHARED_JS.read_text(encoding="utf-8")
    browse = BROWSE_JS.read_text(encoding="utf-8")
    # The old native-<select> builder is gone from BOTH files (it was a
    # byte-identical fork — the exact duplication the shared-UI standard forbids).
    for path, src in ((SHARED_JS, shared), (BROWSE_JS, browse)):
        assert "function makeSortRow" not in src, (
            f"function makeSortRow still defined in {path.name}. The sort row is "
            f"now createSortControl in static/shared.js; delete the duplicate."
        )
    # The replacement lives in exactly one place (shared.js), called by browse.
    assert shared.count("function createSortControl") == 1, (
        "createSortControl must be defined exactly once, in static/shared.js."
    )
    assert "function createSortControl" not in browse, (
        "createSortControl must NOT be re-defined in static/browse/index.js — "
        "call the shared global instead (shared.js loads before browse/index.js)."
    )
    assert "createSortControl(" in browse, (
        "static/browse/index.js should call the shared createSortControl for its "
        "artists/albums sort rows."
    )


def test_sort_popover_uses_opaque_elev_base():
    shared = SHARED_JS.read_text(encoding="utf-8")
    # applyScheme must publish a mode-derived opaque base for floating overlays.
    assert "--elev-base" in shared, (
        "static/shared.js applyScheme must set --elev-base so the sort popover "
        "(and other floating overlays) have an opaque backing on translucent-"
        "surface schemes (Tubular Blue / Ladyland / Bloody Pink)."
    )
    # The popover CSS must actually consume it (opaque base + surface tint on top).
    assert "var(--elev-base" in shared, (
        "The sort popover CSS must composite over var(--elev-base, …) — without "
        "it the panel reuses the translucent --surface and reads the list "
        "beneath it on gradient/light schemes."
    )


def test_native_white_option_hack_removed():
    # The old makeSortRow forced every <option> to color:#111;background:#fff
    # (a white list ignoring the theme). That hack must be gone now that the
    # control is a themed popover.
    for path in _SORT_JS_FILES:
        src = path.read_text(encoding="utf-8")
        assert "color:#111;background:#fff" not in src, (
            f"The hardcoded white-option style is still in {path.name}. The "
            f"themed popover replaces it; remove the native-<select> remnant."
        )


# ── Surprise Me: single-source button + seed store (2026-06-17 plan U5) ───────
# The Surprise Me control is shared (per CLAUDE.md): the button factory + seed
# store live in static/shared.js, the button is rendered by the shared playback
# module, and the per-page files must not fork any of it.

PLAYBACK_JS = ROOT / "static/playback/index.js"
GUEST_APP = ROOT / "static/guest/app.js"
ADMIN_APP = ROOT / "static/admin/app.js"


def test_surprise_button_factory_is_single_source():
    shared = SHARED_JS.read_text(encoding="utf-8")
    assert shared.count("function createSurpriseButton") == 1, (
        "createSurpriseButton must be defined exactly once, in static/shared.js."
    )
    for path in (GUEST_APP, ADMIN_APP, BROWSE_JS, PLAYBACK_JS):
        src = path.read_text(encoding="utf-8")
        assert "function createSurpriseButton" not in src, (
            f"createSurpriseButton must NOT be defined in {path.name} — call the "
            f"shared factory in static/shared.js (shared.js loads first)."
        )


def test_surprise_seed_store_is_single_source():
    shared = SHARED_JS.read_text(encoding="utf-8")
    assert "function recordSurprisePick" in shared and "function getSurpriseSeed" in shared, (
        "The Surprise seed store (recordSurprisePick/getSurpriseSeed) lives in shared.js."
    )
    for path in (GUEST_APP, ADMIN_APP):
        src = path.read_text(encoding="utf-8")
        assert "function recordSurprisePick" not in src and "function getSurpriseSeed" not in src, (
            f"The Surprise seed store must not be re-defined in {path.name}."
        )


def test_browse_records_surprise_picks():
    browse = BROWSE_JS.read_text(encoding="utf-8")
    assert "recordSurprisePick(" in browse, (
        "static/browse/index.js must record each successful add into the Surprise "
        "seed so a later press is seeded from the browser's own picks."
    )


def test_playback_renders_surprise_button_and_posts_seed():
    pb = PLAYBACK_JS.read_text(encoding="utf-8")
    assert "createSurpriseButton(" in pb, (
        "static/playback/index.js must render the shared Surprise button in the "
        "Now-tab dock (placement C)."
    )
    assert "/api/queue/surprise" in pb, (
        "static/playback/index.js must POST the seed to /api/queue/surprise."
    )


def test_playback_renders_lyrics_contribute_link_in_reserved_slot():
    """Contribute prompt (2026-06-23 plan U4): the shared playback module renders a
    contribute link in the SAME reserved lyric slot (no UI reshape), opens it in a
    new tab, and shows it only on a confirmed miss the server flagged via
    r.contribute.url — mutually exclusive with the pill/instrumental tag."""
    pb = PLAYBACK_JS.read_text(encoding="utf-8")
    # The anchor lives in the slot scaffold and opens safely in a new tab (R5).
    assert "np-lyrics-contribute" in pb, (
        "static/playback/index.js must render a .np-lyrics-contribute link in the "
        "reserved lyric slot."
    )
    assert 'target="_blank"' in pb and "noopener" in pb, (
        "the contribute link must open in a new tab with rel=noopener (R5 — never "
        "navigate away from the jukebox)."
    )
    # Gated on the server-provided contribute.url, and exclusive with pill/tag.
    assert "r.contribute" in pb and "contribute.url" in pb, (
        "the contribute branch must render only when the server attaches "
        "r.contribute.url (a confirmed miss with the toggle on)."
    )
    assert "!hasLyrics" in pb and "!instrumental" in pb, (
        "the contribute link must be mutually exclusive with the pill and the "
        "instrumental tag (slot one-of invariant — no reshape)."
    )


def test_surprise_pick_is_retractable_via_receipt():
    """The Surprise press must hand its append receipt to the page (onQueued) so
    the queued track gets a remove (✕), exactly like a manual add — otherwise the
    guest can't remove a track their press queued (2026-06-17 fix)."""
    pb = PLAYBACK_JS.read_text(encoding="utf-8")
    assert "onQueued" in pb and "data.entry" in pb, (
        "static/playback/index.js _doSurprise must hand data.entry to cfg.onQueued; "
        "without it the surprise track has no receipt and shows no ✕."
    )
    guest = GUEST_APP.read_text(encoding="utf-8")
    assert guest.count("onQueued") >= 2, (
        "static/guest/app.js must pass onQueued to BOTH mountBrowser and "
        "mountPlayback so surprise picks persist a receipt like manual adds."
    )


def test_surprise_button_has_inflight_working_state():
    """The shared control defines a distinct in-flight working state — spinner +
    a SELF-CONTAINED spin keyframe (so it animates on the admin dock too, where
    the guest-only `sentSpin` doesn't exist) (2026-06-17 plan 004 U1)."""
    shared = SHARED_JS.read_text(encoding="utf-8")
    assert ".jp-surprise-dock.working" in shared, (
        "static/shared.js must define a .jp-surprise-dock.working in-flight state."
    )
    assert "@keyframes jpSurpriseSpin" in shared, (
        "static/shared.js must define a self-contained spin keyframe, not rely on "
        "the guest template's sentSpin (the button also renders on admin)."
    )
    assert "jp-surprise-spinner" in shared, (
        "createSurpriseButton must include a spinner element for the working state."
    )


def test_surprise_inflight_lifecycle_in_playback():
    """_doSurprise drives the working state and guards against double-submit
    (2026-06-17 plan 004 U2)."""
    pb = PLAYBACK_JS.read_text(encoding="utf-8")
    assert "classList.add('working')" in pb and "aria-busy" in pb, (
        "static/playback/index.js _doSurprise must enter the .working state + set "
        "aria-busy for the duration of the request."
    )
    assert "_surpriseBusy" in pb, (
        "_doSurprise must guard against double-submit while a press is in flight."
    )


def test_surprise_recent_readout_updates_live():
    """ce-debug fix: the Setup Recent-suggestions readout updates live via the
    surprise_recorded WS event, not only at Setup-open."""
    admin = ADMIN_APP.read_text(encoding="utf-8")
    assert "surprise_recorded" in admin and "renderSurpriseRecent(" in admin, (
        "static/admin/app.js must handle the surprise_recorded WS event and render "
        "the readout via the shared renderSurpriseRecent path."
    )


def test_surprise_anti_repeat_wiring():
    """Plan 005 U2: the browser sends recently-surprised ids as `exclude` and
    records each pick; the store is single-source in shared.js."""
    pb = PLAYBACK_JS.read_text(encoding="utf-8")
    assert "exclude" in pb and "recordSurprised(" in pb and "getRecentSurprised(" in pb, (
        "static/playback/index.js must send getRecentSurprised() as `exclude` and "
        "record each surprise pick via recordSurprised()."
    )
    shared = SHARED_JS.read_text(encoding="utf-8")
    assert "function recordSurprised" in shared and "function getRecentSurprised" in shared, (
        "The recently-surprised store must live single-source in static/shared.js."
    )


# ── Mobile default tab → Now (2026-06-17 plan 006) ───────────────────────────
# On guest mobile the page opens on Now, not Search. The HTML keeps search-view
# as the hardcoded active default for desktop safety: the docked Now pane is a
# sibling of #browse-container, so an inactive search-view would blank the
# desktop library pane. app.js re-points to Now at load only on mobile (R1/R2).


def test_guest_mobile_defaults_to_now_tab():
    guest = GUEST_APP.read_text(encoding="utf-8")
    assert re.search(r"!desktopMq\.matches[^\n]*switchTab\(['\"]now-view['\"]\)", guest), (
        "static/guest/app.js must default to the Now tab on mobile load — a "
        "switchTab('now-view') gated by !desktopMq.matches (2026-06-17 plan 006 U1/AE1)."
    )


def test_guest_desktop_default_safety_intact():
    guest = GUEST_APP.read_text(encoding="utf-8")
    template = GUEST_TEMPLATE.read_text(encoding="utf-8")
    assert "function handleDesktopChange" in guest and re.search(r"handleDesktopChange\(\);", guest), (
        "handleDesktopChange must remain and run at load so a desktop viewport "
        "lands on search-view (the docked-Now library pane must not be blank) — AE2."
    )
    assert re.search(r'class="tab active"[^>]*data-view="search-view"', template), (
        "The guest template must keep class=\"tab active\" on the search-view tab "
        "as the desktop-safe hardcoded default."
    )


def test_guest_tab_default_not_persisted():
    guest = GUEST_APP.read_text(encoding="utf-8")
    assert not re.search(r"(localStorage|sessionStorage)[^\n]*(tab|view)", guest, re.I), (
        "The mobile Now default must not introduce tab/view persistence (R4) — it "
        "is recomputed each load, not remembered."
    )


def test_browsehandle_declared_before_mobile_now_default():
    """Regression (2026-06-17): the load-time mobile default switchTab('now-view')
    must run only AFTER `let browseHandle` has been declared. switchTab reads
    browseHandle; if the default call precedes the declaration, browseHandle is in
    the Temporal Dead Zone and switchTab throws a ReferenceError at module load —
    which on mobile aborts the rest of guest/app.js (mountPlayback, mountBrowser,
    mountAppearance all never run), breaking the Surprise button, the nudge, the
    appearance gear, Search, and Browse. Desktop is unaffected (the call is gated
    !desktopMq.matches). TDZ is a runtime error, so the order pin is the only
    structural guard the (JS-runtime-less) test suite can offer."""
    guest = GUEST_APP.read_text(encoding="utf-8")
    decl = guest.find("let browseHandle")
    call = guest.find("switchTab('now-view')")
    assert decl != -1 and call != -1, (
        "expected both `let browseHandle` and switchTab('now-view') in guest/app.js"
    )
    assert decl < call, (
        "`let browseHandle` must be declared BEFORE the load-time mobile "
        "switchTab('now-view') default — otherwise switchTab reads browseHandle in "
        "the Temporal Dead Zone and throws at load, aborting all mobile init."
    )


# ── Idle "add music" nudge in the shared Now view (2026-06-17 plan 006 U2) ────
# The nudge is rendered by the shared playback module, gated on no-current-track
# AND empty-queue AND a top-level onFindMusic callback (guest → Search; admin
# none). It is a <button> (a11y), explicitly hidden when a track plays (not just
# "not emitted"), and hidden on desktop via CSS.

QUEUE_CSS = ROOT / "static/playback/queue.css"


def test_guest_wires_find_music_to_search_tab():
    guest = GUEST_APP.read_text(encoding="utf-8")
    assert re.search(r"onFindMusic:\s*\(\)\s*=>\s*switchTab\(['\"]search-view['\"]\)", guest), (
        "static/guest/app.js must pass a top-level onFindMusic callback into "
        "mountPlayback wired to switchTab('search-view') (2026-06-17 plan 006 U2/AE3)."
    )


def test_playback_renders_nudge_button_bound_to_callback():
    pb = PLAYBACK_JS.read_text(encoding="utf-8")
    # The nudge is a <button> (accessible affordance), not a div/span/a.
    assert re.search(r'<button[^>]*class="np-nudge"', pb), (
        "static/playback/index.js must render the idle nudge as a <button "
        'class="np-nudge"> (keyboard/screen-reader accessible), not a div/span/a.'
    )
    # Click is bound to the page-supplied callback.
    assert "cfg.onFindMusic" in pb, (
        "The nudge must invoke cfg.onFindMusic (the page-supplied 'add music' "
        "action) — guest routes it to Search; admin supplies none (R3)."
    )


def test_playback_nudge_gated_on_no_track_and_empty_queue():
    pb = PLAYBACK_JS.read_text(encoding="utf-8")
    # Gate references BOTH no-current-track and empty-queue (R5/AE3) and the
    # callback presence (R3).
    assert re.search(r"cfg\.onFindMusic\s*&&\s*!_hasTrack\s*&&\s*_queueLen\s*===\s*0", pb), (
        "The nudge gate must require onFindMusic AND no current track AND an empty "
        "queue — a queued-but-not-playing state must NOT show 'add music' (R5/AE3)."
    )


def test_playback_nudge_hidden_when_track_plays():
    pb = PLAYBACK_JS.read_text(encoding="utf-8")
    # applyNowPlaying must explicitly re-evaluate the nudge (the scaffold is built
    # once, so "not emitted on the playing branch" would leave a stale nudge).
    assert re.search(r"_hasTrack = true;\s*\n\s*_updateNudge\(\)", pb), (
        "applyNowPlaying must call _updateNudge() after setting _hasTrack so a "
        "track starting after an idle period hides the nudge (AE4) — the scaffold "
        "is built once, so explicit hide is required, not branch-skipping."
    )


def test_nudge_hidden_on_desktop_via_css():
    css = QUEUE_CSS.read_text(encoding="utf-8")
    assert re.search(r"@media \(min-width:\s*960px\)\s*\{[^}]*\.np-nudge[^}]*display:\s*none", css), (
        "static/playback/queue.css must hide .np-nudge at >=960px — on desktop the "
        "Now pane is docked beside a visible Search pane, so the nudge is redundant."
    )
    # The explicit [hidden] rule is required (author display:block out-ranks UA [hidden]).
    assert re.search(r"\.np-nudge\[hidden\]\s*\{\s*display:\s*none", css), (
        ".np-nudge[hidden] must force display:none — without it the toggle no-ops, "
        "since .np-nudge's own display:block out-ranks the UA [hidden] rule."
    )


# ── Genre/Year chip + "Show More" theme gloss (2026-06-17) ───────────────────
# Before: Show More had no CSS (raw browser button); genre/year chips were forked
# between templates — guest hovered to flat --accent, admin hovered only to
# --surface (no accent). Consolidated into the shared static/browse/rail.css with
# a gradient-gloss treatment (hover lights accent, :active fills --accent-grad,
# Show More is a gradient pill). These pins lock single-source + the themed look.

RAIL_CSS = ROOT / "static/browse/rail.css"


def test_show_more_button_is_themed():
    """2026-06-18 rework: Show More is now a quiet GHOST button (surface chip +
    accent border/text), NOT the prior full --accent-grad fill. Still themed via
    --accent-ui / --surface2 so a scheme switch re-colors it."""
    css = RAIL_CSS.read_text(encoding="utf-8")
    rule = re.search(r"\.show-more-btn\s*\{([^}]*)\}", css)
    assert rule, ".show-more-btn must be styled in the shared rail.css."
    body = rule.group(1)
    assert "--accent-grad" not in body, (
        ".show-more-btn must NOT fill with --accent-grad — the 2026-06-18 ghost "
        "rework dropped the loud gradient pill for a quiet surface chip."
    )
    assert re.search(r"background:\s*var\(--surface2", body) and re.search(r"border:[^;]*--accent-ui", body), (
        ".show-more-btn must be a ghost button: var(--surface2) background with an "
        "--accent-ui border + text (the calm secondary-action treatment)."
    )


def test_chip_states_single_source_and_glossed():
    css = RAIL_CSS.read_text(encoding="utf-8")
    assert ".genre-chip:hover" in css and ".year-chip:hover" in css, (
        "Genre/year chip hover states must live in the shared rail.css so guest "
        "and admin theme identically (single source)."
    )
    # The click (:active) state fills with the scheme gradient — the chosen gloss.
    assert re.search(r"\.genre-chip:active,\s*\.year-chip:active\s*\{[^}]*--accent-grad", css), (
        "Chip :active (clicked) must fill with --accent-grad (Variant B gloss)."
    )
    # Hover routes through --accent-ui (not a flat --accent swap).
    assert re.search(r"\.genre-chip:hover,\s*\.year-chip:hover\s*\{[^}]*--accent-ui", css), (
        "Chip hover must light --accent-ui per the themed treatment."
    )


def test_chip_css_not_forked_in_templates():
    guest = GUEST_TEMPLATE.read_text(encoding="utf-8")
    admin = ADMIN_TEMPLATE.read_text(encoding="utf-8")
    assert ".genre-chip {" not in guest and ".genre-chip:hover" not in guest, (
        "Guest template must not define .genre-chip rules — they live in the shared "
        "rail.css now (single source for both pages)."
    )
    assert ".admin-shell .genre-chip" not in admin and ".admin-shell .year-chip" not in admin, (
        "Admin template must not fork .genre-chip/.year-chip styling — the shared "
        "rail.css themes both pages (the old admin --surface-only hover is gone)."
    )


# ── Artist → All Songs (2026-06-17 plan 007) ─────────────────────────────────
# The All Songs drill lives single-source in the shared browse module; these pins
# lock the seven orders, the song-level (cross own/appears) dedup, the Popular
# availability gate, the local-plays Most Played source, and dups-in-By-Release.


def test_all_songs_view_single_source_in_browse():
    browse = BROWSE_JS.read_text(encoding="utf-8")
    assert "function showAllSongs" in browse and "function _allSongsEntry" in browse, (
        "All Songs view + entry control live in the shared static/browse/index.js "
        "(so both guest and admin get it)."
    )
    assert re.search(r"/api/browse/artists/\$\{[^}]+\}/songs", browse), (
        "showAllSongs must fetch the /songs endpoint."
    )
    # Reuses the shared renderer + sort control, not a forked builder.
    assert "createSortControl(" in browse and "makeTrackRow(" in browse, (
        "All Songs must reuse createSortControl + the shared track-row renderer."
    )


def test_all_songs_offers_seven_orders():
    browse = BROWSE_JS.read_text(encoding="utf-8")
    for v in ("'popular'", "'release'", "'az'", "'za'", "'earliest'", "'latest'", "'plays'"):
        assert v in browse, f"All Songs sort order {v} missing from ALL_SONGS_ORDERS."


# ── Queue-end rework (2026-06-21 plan U5) ────────────────────────────────────


def test_queue_end_radios_replaced():
    """Stop / Popular Random / Full Random present; legacy Shuffle/Repeat gone."""
    html = ADMIN_TEMPLATE.read_text(encoding="utf-8")
    assert 'value="stop"' in html
    assert 'value="popular_random"' in html
    assert 'value="full_random"' in html
    assert 'value="shuffle"' not in html
    assert 'value="repeat"' not in html


def test_queue_end_has_info_tooltips_and_controls():
    """Both random modes carry an info (ⓘ) tooltip; the threshold field, its row,
    and the length-limit checkbox exist."""
    html = ADMIN_TEMPLATE.read_text(encoding="utf-8")
    assert html.count("&#9432;") >= 2  # ⓘ on Popular Random + Full Random
    assert 'title="When the queue runs dry' in html
    assert 'id="popular-random-threshold"' in html
    assert 'id="popular-threshold-row"' in html
    assert 'id="queue-end-length-limit"' in html


def test_admin_js_wires_queue_end_settings():
    """admin app.js loads/saves the queue-end settings."""
    js = ADMIN_APP.read_text(encoding="utf-8")
    assert "popular_random_threshold" in js
    assert "queue_end_length_limit" in js


def test_popular_threshold_never_dimmed_or_disabled():
    """Spec correction (2026-06-24): the popularity-threshold field is ALWAYS
    fully enabled — never greyed out or disabled based on the selected Queue-end
    mode (reverses the 2026-06-21 dim+disable treatment). The
    '(applies to Popular Random)' note stays as a plain, always-visible hint."""
    html = ADMIN_TEMPLATE.read_text(encoding="utf-8")
    js = ADMIN_APP.read_text(encoding="utf-8")
    # The scope note is still present (now a plain field-note, not a hover-only hint).
    assert "applies to Popular Random" in html
    # No mode-gated dim/disable wiring remains for the popular-threshold row.
    assert "popular-threshold-row" not in js, (
        "the popular-threshold row must no longer be dimmed/disabled from JS"
    )
    assert "#popular-threshold-row.is-inactive" not in html, (
        "the popular-threshold dim CSS must be removed"
    )
    assert "popular-threshold-hint" not in html, (
        "the hover-only hint span was replaced by an always-visible field-note"
    )


def test_info_circles_are_clickable_buttons():
    """2026-06-24: every ⓘ info circle is a real <button type="button"> (not a
    bare span nested in a label), and admin/app.js calls preventDefault on .info-i
    clicks so showing help never toggles the option it sits inside."""
    html = ADMIN_TEMPLATE.read_text(encoding="utf-8")
    js = ADMIN_APP.read_text(encoding="utf-8")
    # The ⓘ glyph now lives in buttons, never in spans.
    assert '<button type="button" class="info-i"' in html
    assert "&#9432;</span>" not in html, "info circles must not be plain spans anymore"
    assert html.count("&#9432;</button>") >= 2
    # The click handler guards against toggling the attached control.
    assert ".info-i" in js
    assert "preventDefault" in js


def test_limit_random_length_lives_in_surprise_me_below_filter():
    """2026-06-24: the 'Limit random length' checkbox moved out of Queue & playback
    into Surprise Me, positioned just below the 'Random length filter' row it
    refers to (so a top-down reader meets the filter before the toggle that scopes
    it)."""
    html = ADMIN_TEMPLATE.read_text(encoding="utf-8")
    filter_pos = html.index("random-min-seconds")
    toggle_pos = html.index('id="queue-end-length-limit"')
    surprise_pos = html.index(">Surprise Me<")
    browse_pos = html.index("Browse &amp; appearance")
    # The toggle sits after the length filter…
    assert filter_pos < toggle_pos, "Limit random length must follow Random length filter"
    # …and within the Surprise Me section (between its subhead and the next one).
    assert surprise_pos < toggle_pos < browse_pos, (
        "Limit random length must live in the Surprise Me section"
    )


def test_most_played_shows_in_guest_experience():
    """2026-06-24: the 'Most Played shows' leaderboard-size field moved from Queue
    & playback to the Guest experience section (it's a guest-facing display count)."""
    html = ADMIN_TEMPLATE.read_text(encoding="utf-8")
    guest_pos = html.index(">Guest experience<")
    mp_pos = html.index('id="most-played-display-limit"')
    assert mp_pos > guest_pos, "Most Played shows must live under Guest experience"


def test_guest_count_fields_use_inline_prose():
    """2026-06-24: 'Show N upcoming tracks to guests' became inline prose with the
    number as a field in the sentence — 'Show [field] upcoming tracks to guests' —
    so the count reads in context. Same for history."""
    html = ADMIN_TEMPLATE.read_text(encoding="utf-8")
    assert "upcoming tracks to guests." in html
    assert "history tracks to guests." in html
    # The inputs keep their stable ids (the API/JS contract is unchanged).
    assert 'id="guest-n"' in html
    assert 'id="guest-m"' in html
    # The old flat label form is gone.
    assert "Show N upcoming tracks to guests" not in html
    assert "Show M history tracks to guests" not in html


def test_lyrics_body_resets_scroll_to_top_on_render():
    """2026-06-21: a freshly-rendered lyrics body starts at the top — resets the
    body scrollTop and _activeLine — so a new track doesn't inherit the previous
    track's bottom scroll. Single-source in the shared playback module."""
    pb = PLAYBACK_JS.read_text(encoding="utf-8")
    start = pb.index("function _renderLyricsBody")
    end = pb.index("function _updateActiveLine", start)
    region = pb[start:end]
    assert "scrollTop = 0" in region, (
        "_renderLyricsBody must reset the lyrics body scroll to the top on render"
    )
    assert "_activeLine = -1" in region, (
        "_renderLyricsBody must reset _activeLine so the active-line auto-scroll "
        "re-applies after a rebuild (re-expand / late load settle on the active line)"
    )


# ── Search filter-tab scroll memory (2026-07-01 ce-debug) ────────────────────
# Switching filter tabs within a search rebuilt #search-results in place but
# never touched the scroller. On guest the scroller is the ANCESTOR (#content),
# not #search-results itself, so clearing the child's innerHTML left the
# ancestor's scrollTop untouched — an unvisited tab opened mid-list at the prior
# tab's depth. Fix: per-filter scroll memory keyed by filter name — capture the
# outgoing tab's depth before the rebuild, land the incoming tab at its
# remembered depth or the top (|| 0) if it was never scrolled, and reset on each
# new query. Same inherited-scroll family as the lyrics-body reset above; no JS
# runtime in this suite, so these are static-source pins.


def test_search_filter_tab_scroll_memory():
    browse = BROWSE_JS.read_text(encoding="utf-8")
    assert "_searchTabScroll" in browse, (
        "static/browse/index.js must keep a per-filter scroll store "
        "(_searchTabScroll) so search filter tabs don't share one scroll position."
    )
    # Isolate the filter-tab click wiring inside _wireSearch.
    start = browse.index("function _wireSearch")
    end = browse.index("async function doSearch", start)
    region = browse[start:end]
    # Capture the OUTGOING tab's depth before the in-place rebuild…
    assert re.search(r"_searchTabScroll\[[^\]]+\]\s*=\s*[^\n]*scrollTop", region), (
        "the filter-tab handler must capture the outgoing tab's scrollTop into "
        "_searchTabScroll before re-rendering."
    )
    # …and land the INCOMING tab at its remembered depth or the top (|| 0) — an
    # unvisited tab must start at the top, not inherit the prior tab's scroll.
    assert re.search(r"scrollTop\s*=\s*_searchTabScroll\[[^\]]+\]\s*\|\|\s*0", region), (
        "the filter-tab handler must restore _searchTabScroll[incoming] || 0 so "
        "a never-scrolled tab opens at the top (not the prior tab's depth)."
    )
    # Resolve the scroller generically so the fix works on guest (ancestor
    # #content) AND admin (#search-results is its own overflow:auto scroller).
    assert "_scrollOwner(" in region, (
        "the filter-tab handler must resolve the scroller via _scrollOwner so the "
        "fix covers both guest (ancestor scroller) and admin (self scroller)."
    )
    # A new query resets the per-tab memory — tabs don't inherit a prior search.
    ds_start = browse.index("async function doSearch")
    ds_end = browse.index("\n  function ", ds_start)
    ds_region = browse[ds_start:ds_end]
    assert re.search(r"_searchTabScroll\s*=\s*\{\}", ds_region), (
        "doSearch must reset _searchTabScroll = {} so a new query starts every "
        "filter tab fresh at the top."
    )


# ── Search filter-tab bar: single-source, opaque, docked (2026-07-02 ce-debug) ─
# The filter-tab bar was forked across both templates — guest wrapped input+tabs
# in a sticky #search-bar backed by var(--bg) (bleeds on translucent/gradient
# schemes); admin had NO wrapper and NO sticky, plus its own .admin-shell tab
# rules. It now lives single-source in rail.css as an opaque (var(--elev-base)),
# sticky, top-docked bar, with the identical #search-bar wrapper markup on both
# pages (CLAUDE.md shared-UI standard).


def test_search_filter_bar_single_source_and_docked():
    rail = RAIL_CSS.read_text(encoding="utf-8")
    guest = GUEST_TEMPLATE.read_text(encoding="utf-8")
    admin = ADMIN_TEMPLATE.read_text(encoding="utf-8")

    # Shared rule: a sticky bar docked to the top with an OPAQUE backing.
    m = re.search(r"#search-bar\s*\{([^}]*)\}", rail)
    assert m, "#search-bar must be styled in the shared static/browse/rail.css."
    body = m.group(1)
    assert "position: sticky" in body and "top: 0" in body, (
        "#search-bar must be a bar docked to the top (position: sticky; top: 0)."
    )
    assert "var(--elev-base" in body and "var(--bg)" not in body, (
        "#search-bar must back with the opaque var(--elev-base), NOT var(--bg) — "
        "var(--bg) is a gradient/translucent on some schemes and lets scrolled "
        "results bleed through the docked bar (the 2026-07-02 overlay bug)."
    )
    # The sticky bar must NOT be forced transparent by a .browse-surface override
    # (guest-only — only guest's #browse-container carries .browse-surface). That
    # let scrolled results bleed through the docked bar on guest while admin looked
    # fine (2026-07-02 ce-debug part 2). Only the NON-sticky .wayfind-bar may be
    # transparent.
    assert ".browse-surface #search-bar" not in rail, (
        "rail.css must not force .browse-surface #search-bar transparent — #search-bar "
        "is position:sticky, so a transparent backing lets scrolled results bleed "
        "through the docked bar on guest. Keep it opaque; only .wayfind-bar (non-sticky) "
        "stays transparent."
    )
    assert "#search-filter-tabs" in rail and ".filter-tab.active" in rail, (
        "#search-filter-tabs and .filter-tab(.active) must be single-source in rail.css."
    )
    # Neither template re-forks the bar or the tab styling.
    for name, src in (("guest", guest), ("admin", admin)):
        assert "#search-bar {" not in src, (
            f"{name} template must not re-style #search-bar — it lives in rail.css now."
        )
        assert "#search-filter-tabs {" not in src and ".filter-tab {" not in src, (
            f"{name} template must not fork #search-filter-tabs/.filter-tab styling."
        )
    assert ".admin-shell .filter-tab" not in admin and ".admin-shell #search-filter-tabs" not in admin, (
        "the admin template's forked .admin-shell filter-tab rules must be removed."
    )
    # Both pages mount the identical wrapper: #search-bar contains the tabs, above
    # #search-results.
    for name, src in (("guest", guest), ("admin", admin)):
        bar = src.find('id="search-bar"')
        assert bar != -1, f"{name} template must wrap search chrome in #search-bar."
        wrapper = src[bar:src.index('id="search-results"', bar)]
        assert 'id="search-filter-tabs"' in wrapper, (
            f"{name}: #search-bar must wrap #search-filter-tabs (identical markup both pages)."
        )


def test_all_songs_popular_gated_when_unavailable():
    browse = BROWSE_JS.read_text(encoding="utf-8")
    assert "popular_available" in browse, "All Songs must read popular_available."
    assert re.search(r"v === 'popular'\s*&&\s*!popAvail", browse), (
        "Popular must render as a disabled-but-visible sort option when the artist "
        "has no popularity data (R7)."
    )


def test_all_songs_groups_by_song_across_own_appears():
    browse = BROWSE_JS.read_text(encoding="utf-8")
    assert "function _groupSongs" in browse, "song-level grouping helper required."
    # A song is Own if ANY copy is own — the fix that makes a hit on a studio
    # album + an appears-on comp render exactly once in Own (R9/AE3).
    assert re.search(r"isOwn\s*=\s*true", browse) and "filter(s => s.isOwn)" in browse, (
        "Flat sorts must group by song across the own/appears boundary (not per "
        "release), so a straddling song appears once."
    )


def test_all_songs_most_played_uses_summed_local_plays():
    browse = BROWSE_JS.read_text(encoding="utf-8")
    assert re.search(r"mode === 'plays'", browse) and "b.plays - a.plays" in browse, (
        "Most Played orders by summed local play counts (not Plex viewCount)."
    )


def test_all_songs_by_release_keeps_dups_via_shared_renderer():
    browse = BROWSE_JS.read_text(encoding="utf-8")
    assert "_renderAllSongsByRelease" in browse and re.search(r"renderTracksDeduped\(tracks, host", browse), (
        "By Release renders each release's tracks via the shared dedup renderer, "
        "preserving duplicates across releases."
    )


# ── Now Playing → Lyrics (2026-06-17 plan 008) ───────────────────────────────
# The lyric panel lives single-source in the shared playback module so guest,
# admin, and the desktop docked pane all get it (CLAUDE.md shared-UI standard).
# These pins lock: the pill + inline expand/collapse, the available/instrumental
# gating, the race guard (track_id capture/compare — the only thing that catches
# a stale fetch after a rapid skip), the shared CSS residence, and the U4
# karaoke highlight riding the existing position tick (no second clock).


def test_lyrics_panel_single_source_in_playback():
    pb = PLAYBACK_JS.read_text(encoding="utf-8")
    # Pill + expand/collapse wired in the shared module (AE5/R5/R6).
    assert re.search(r'<button[^>]*class="np-lyrics-pill"', pb), (
        "static/playback/index.js must render the quiet '♪ Lyrics' pill as a "
        "<button class=\"np-lyrics-pill\"> (keyboard/screen-reader accessible)."
    )
    assert "_lyricsExpanded = true" in pb and "_lyricsExpanded = false" in pb, (
        "The pill must expand (_lyricsExpanded=true) and the collapse control "
        "must collapse (_lyricsExpanded=false) the inline panel (R6/AE5)."
    )
    # The fetch lives in the shared module; the lyric render must NOT be forked
    # into the per-page files (shared-UI standard).
    assert re.search(r"/api/lyrics\?track_id=", pb), (
        "static/playback/index.js must fetch GET /api/lyrics?track_id=… (R1/R2)."
    )
    for path in (GUEST_APP, ADMIN_APP):
        src = path.read_text(encoding="utf-8")
        assert "/api/lyrics" not in src and "np-lyrics" not in src, (
            f"Lyric fetch/render must not be forked into {path.name} — it lives "
            f"in the shared static/playback/index.js (CLAUDE.md shared-UI standard)."
        )


def test_lyrics_gated_on_available_and_instrumental():
    pb = PLAYBACK_JS.read_text(encoding="utf-8")
    # The render derives availability from the result and only shows the button
    # for real lyrics; a miss leaves the reserved slot empty (see reserved-slot test).
    assert re.search(r"r\s*&&\s*r\.available", pb) and "hasLyrics" in pb, (
        "The lyric render must derive `available` from the result and gate the "
        "button on hasLyrics (available && !instrumental) (R8/AE3)."
    )
    # AE4/R9: an instrumental result renders the static indicator tag.
    assert "instrumental" in pb and re.search(r'class="np-lyrics-tag"', pb), (
        "An instrumental result must render the '♪ Instrumental' indicator "
        "(.np-lyrics-tag), not the expandable button (R9/AE4)."
    )


def test_lyrics_slot_is_reserved_so_now_view_never_reflows():
    """2026-06-18 reserved-slot redesign (Option A), refined: the lyric slot holds
    a fixed height for the WHOLE time the now-view exists — including when nothing
    is playing (the slot is just empty). So the now-view never reflows mid-song
    (button/tag fades into held space) NOR on idle↔playing transitions. The wrapper
    must NOT be hidden on idle — only the button/tag/panel toggle inside it."""
    pb = PLAYBACK_JS.read_text(encoding="utf-8")
    assert "_lyrEls.root.hidden = false" in pb, (
        "_renderLyrics must keep the lyric wrapper shown (reserved slot) even when "
        "nothing is playing — only the inner button/tag/panel toggle."
    )
    assert not re.search(r"_lyrEls\.root\.hidden = true", pb), (
        "_renderLyrics must NOT hide the lyric wrapper on idle — hiding it collapses "
        "the reserved slot and reflows the now-view on idle↔playing transitions "
        "(the 2026-06-18 'reserve the space when nothing is playing' fix)."
    )
    css = QUEUE_CSS.read_text(encoding="utf-8")
    assert re.search(r"\.np-lyrics-slot\s*\{[^}]*min-height", css), (
        ".np-lyrics-slot must reserve a fixed min-height so searching / found / "
        "instrumental / miss / idle all occupy the same space (no reflow)."
    )


def test_lyrics_button_is_surprise_me_width_twin():
    """Design pick: the Lyrics button is a full-width inset bar matching the
    Surprise Me dock (width: 100% inside the inset slot)."""
    css = QUEUE_CSS.read_text(encoding="utf-8")
    assert re.search(r"\.np-lyrics-pill\s*\{[^}]*width:\s*100%", css), (
        "The Lyrics button must be a width-twin of the Surprise Me dock (full-"
        "width inset bar) per the 2026-06-18 design pick."
    )


def test_seek_bar_inset_matches_buttons():
    """2026-06-18 UI rework: the seek bar (#np-progress) is inset .75rem so it's
    flush with the Lyrics slot + Surprise Me dock, on both guest and admin."""
    for tmpl in (GUEST_TEMPLATE, ADMIN_TEMPLATE):
        src = tmpl.read_text(encoding="utf-8")
        rule = re.search(r"#np-progress\s*\{([^}]*)\}", src)
        assert rule, f"#np-progress rule missing in {tmpl.name}"
        body = rule.group(1)
        assert ".75rem" in body, (
            f"#np-progress in {tmpl.name} must be inset .75rem (flush with the "
            f"Lyrics slot + Surprise Me dock)."
        )
        assert not re.search(r"padding:\s*0\s+1rem", body), (
            f"#np-progress in {tmpl.name} must not keep the old 1rem inset."
        )


def test_volume_and_kebab_use_gradient():
    """2026-06-18 UI rework: the volume slider fill and kebab hover/active fill
    with --accent-grad (the seek bar + chips already do). Fills only — flat accent
    text/borders unchanged."""
    admin = ADMIN_TEMPLATE.read_text(encoding="utf-8")
    guest = GUEST_TEMPLATE.read_text(encoding="utf-8")
    assert re.search(r"#volume-slider::-webkit-slider-runnable-track\s*\{[^}]*--accent-grad", admin), (
        "The admin volume slider must use a --accent-grad fill track (custom range), "
        "not the flat native accent-color."
    )
    assert re.search(r"\.kebab-btn:active\s*\{[^}]*--accent-grad", guest), (
        "Guest .kebab-btn:active must fill with --accent-grad."
    )
    assert re.search(r"\.admin-shell \.kebab-btn:hover\s*\{[^}]*--accent-grad", admin), (
        "Admin .kebab-btn:hover must fill with --accent-grad."
    )


def test_release_subtitle_links_and_kebab_parity():
    """2026-06-20 release-view redesign: the artist + year under the cover are
    accent name-links (artist once, here; year drills its releases via
    browseToYear), and the release-aside kebab is full-art-width on BOTH
    breakpoints — mobile matches desktop's bar, not a small square."""
    browse = BROWSE_JS.read_text(encoding="utf-8")
    assert "nl-year" in browse and "nl-artist" in browse, (
        "showAlbumTracks must render the release subtitle artist + year as "
        ".nl-artist / .nl-year name-links."
    )
    assert re.search(r"async function browseToYear", browse), (
        "static/browse/index.js must define browseToYear (the year-drill helper)."
    )
    assert "browseToYear(" in browse, (
        "the release subtitle year link must call browseToYear."
    )
    for tmpl in (GUEST_TEMPLATE, ADMIN_TEMPLATE):
        src = tmpl.read_text(encoding="utf-8")
        # Accent-at-rest, scoped to the release subtitle (list rows stay muted).
        assert re.search(r"\.ra-htitle \.s \.name-link\s*\{[^}]*--accent", src), (
            f"{tmpl.name}: release subtitle .name-link must be accent-colored at rest."
        )
        # Kebab width == cover width at each breakpoint (the full-art-width bar).
        cover_mobile = re.search(r"\.ra-cover\s*\{[^}]*width:\s*(\d+)px", src)
        kebab_mobile = re.search(r"\.release-aside \.kebab-btn\s*\{[^}]*width:\s*(\d+)px", src)
        assert cover_mobile and kebab_mobile and cover_mobile.group(1) == kebab_mobile.group(1), (
            f"{tmpl.name}: mobile .release-aside .kebab-btn width must match the cover "
            "(standardize desktop's full-art-width bar onto mobile)."
        )
        cover_desktop = re.search(r"\.release-aside \.ra-cover\s*\{[^}]*width:\s*(\d+)px", src)
        kebab_desktop = re.findall(r"\.release-aside \.kebab-btn\s*\{[^}]*width:\s*(\d+)px", src)
        assert cover_desktop and len(kebab_desktop) >= 2 and kebab_desktop[-1] == cover_desktop.group(1), (
            f"{tmpl.name}: desktop .release-aside .kebab-btn width must match the desktop cover width."
        )


def test_release_kebab_navigation():
    """2026-06-20 release-kebab-nav: the detail-view header kebab gains Go to
    artist(s) + Go to year. Single artist = one entry; a compilation (>1 distinct
    track performer) lists several inline + a 'More artists…' submenu. Performers
    + year are captured per-trigger in showAlbumTracks and reuse the shipped
    browseToArtist / browseToYear helpers. Shared module = guest + admin parity."""
    browse = BROWSE_JS.read_text(encoding="utf-8")
    # The distinct-performers helper exists.
    assert re.search(r"function _distinctPerformers\s*\(", browse), (
        "static/browse/index.js must define _distinctPerformers (compilation "
        "performer list, ordered by track count)."
    )
    # showAlbumTracks captures performers + year on the trigger button.
    assert "overflowBtn._artists" in browse and "overflowBtn._year" in browse, (
        "showAlbumTracks must capture _artists + _year on the overflow trigger "
        "button (per-button capture idiom, like _sources)."
    )
    # The header-kebab builder wires the new entries.
    menu = re.search(r"function showOverflowMenu\b.*?\n  \}", browse, re.S)
    assert menu, "showOverflowMenu must exist."
    body = menu.group(0)
    assert "Go to artist" in body and "browseToArtist(" in body, (
        "showOverflowMenu must add 'Go to artist' entries wired to browseToArtist."
    )
    assert "Go to year" in body and "browseToYear(" in body, (
        "showOverflowMenu must add a 'Go to year' entry wired to browseToYear."
    )
    # Compilation overflow spills into a PAGINATED submenu (2026-06-20 follow-up:
    # the height-uncapped sheet ran off-page on Nuggets-sized comps) — the
    # "More artists…" path routes through _paginateSheet, not one flat sheet.
    assert "More artists" in body and "_paginateSheet(" in body, (
        "showOverflowMenu's 'More artists…' path must paginate via _paginateSheet "
        "so a large VA comp can't overflow the uncapped sheet."
    )
    assert re.search(r"function _paginateSheet\s*\(", browse), (
        "static/browse/index.js must define _paginateSheet (bounded-page sub-sheet)."
    )
    assert re.search(r"ARTIST_PAGE_SIZE\s*=\s*\d+", browse), (
        "static/browse/index.js must define an ARTIST_PAGE_SIZE for the paginated "
        "'More artists…' sub-sheet."
    )
    # The artist entry must carry the performer name (not a bare label).
    assert "Go to artist — ${" in browse, (
        "the detail-kebab artist entry must include the performer name "
        "('Go to artist — Name')."
    )


def test_overflow_sheet_caps_height_and_scrolls():
    """2026-06-20 defense-in-depth: the bottom/centered overflow-menu sheet had no
    height cap or scroll, so a long sheet (e.g. a big VA comp's performer list)
    ran off-page. Both templates' base .overflow-menu rule must cap height and
    scroll so any overlong sheet stays reachable."""
    for tmpl in (GUEST_TEMPLATE, ADMIN_TEMPLATE):
        src = tmpl.read_text(encoding="utf-8")
        rule = re.search(r"\.overflow-menu\s*\{([^}]*)\}", src)
        assert rule, f"{tmpl.name}: base .overflow-menu rule not found."
        body = rule.group(1)
        assert "max-height" in body and "overflow" in body, (
            f"{tmpl.name}: .overflow-menu must cap height + scroll (max-height + "
            "overflow-y) so a long sheet can't run off the page."
        )


def test_filter_tabs_accent_text_and_gradient_active():
    """2026-06-18 UI rework: unselected filter tabs use accent text (global, R4c);
    the active tab fills with --accent-grad (R3). Single-source in rail.css since
    the 2026-07-02 ce-debug docked-bar fix moved the forked template rules there."""
    rail = RAIL_CSS.read_text(encoding="utf-8")
    base = re.search(r"\.filter-tab\s*\{([^}]*)\}", rail)
    assert base and "--accent-ui" in base.group(1) and "var(--muted)" not in base.group(1), (
        "Unselected .filter-tab in rail.css must use --accent-ui text (not --muted) "
        "so it's legible on the gradient schemes (R4c, global)."
    )
    active = re.search(r"\.filter-tab\.active\s*\{([^}]*)\}", rail)
    assert active and "--accent-grad" in active.group(1), (
        ".filter-tab.active in rail.css must fill with --accent-grad (R3)."
    )


def test_translucent_schemes_opaque_and_readable():
    """2026-06-18 UI rework: Bloody Pink + Ladyland get opaque surfaces + lightened
    muted (and BP a diagonal gradient) so text is legible. Other schemes untouched.
    Note: #2a0a16/#2a1410 remain valid `on:` accent values — assert the muted/surface
    FIELDS specifically, not raw substring presence."""
    shared = SHARED_JS.read_text(encoding="utf-8")
    for key, dark_muted in (("bloody-pink", "#2a0a16"), ("ladyland-orange", "#2a1410")):
        entry = re.search(r"'%s':.*?\},\n" % re.escape(key), shared, re.S)
        assert entry, f"{key} scheme entry not found"
        body = entry.group(0)
        for field in ("surface", "surface2"):
            fm = re.search(rf"\b{field}:\s*'([^']+)'", body)
            assert fm and fm.group(1).startswith("#"), (
                f"{key} {field} must be an opaque hex color (not translucent rgba) so "
                f"text reads over the gradient (2026-06-18)."
            )
        mm = re.search(r"\bmuted:\s*'([^']+)'", body)
        assert mm and mm.group(1) != dark_muted, (
            f"{key} muted must be lightened from {dark_muted} — dark muted on a dark "
            f"surface is unreadable."
        )
    bp = re.search(r"'bloody-pink':.*?\},\n", shared, re.S).group(0)
    assert "160deg" in bp and "180deg" not in bp, (
        "Bloody Pink bg must be diagonal (160deg), not vertical (180deg)."
    )


def test_lyrics_fetch_has_trackid_race_guard():
    """The fetch captures the track_id it was issued for and the apply path
    compares it to the current _trackId before painting. _playing/_seeking stay
    true across a track change, so ONLY a track_id compare catches track A's
    fetch resolving after a rapid skip to B (realtime-ui-stale Rule 1)."""
    pb = PLAYBACK_JS.read_text(encoding="utf-8")
    assert re.search(r"async function _fetchLyrics\(trackId\)", pb), (
        "_fetchLyrics must capture the track_id it was issued for as a parameter."
    )
    # TWO guards: one after `fetch` and one after `resp.json()` — both are
    # suspension points (code-review #5, 2026-06-18). A single guard before the
    # json parse still lets a rapid skip during the parse paint stale lyrics.
    guards = len(re.findall(r"if \(trackId !== _trackId\) return", pb))
    assert guards >= 2, (
        f"_fetchLyrics must re-check trackId !== _trackId after BOTH awaits "
        f"(fetch AND resp.json()); found {guards} guard(s)."
    )


def test_all_songs_fetch_has_catch():
    """Code-review #9: showAllSongs must .catch() the fetch so a true network
    failure (which rejects _api) doesn't strand the 'Loading…' spinner forever."""
    browse = BROWSE_JS.read_text(encoding="utf-8")
    assert re.search(r"/songs[\s\S]{0,600}\}\)\.catch\(", browse), (
        "showAllSongs's /songs fetch must chain a .catch() that clears the spinner "
        "on network failure."
    )


def test_surprise_dock_reacts_to_live_appearance_toggle():
    """Code-review #6: the surprise dock is created always (hidden when disabled),
    and the shared appearance handler toggles it on a live surprise_me_enabled
    change — so an admin toggling it mid-session reaches connected clients without
    a reload."""
    shared = SHARED_JS.read_text(encoding="utf-8")
    assert re.search(r"surprise_me_enabled[\s\S]{0,200}\.jp-surprise-dock", shared), (
        "shared.js onAppearanceChanged must toggle .jp-surprise-dock visibility "
        "from ev.surprise_me_enabled."
    )
    pb = PLAYBACK_JS.read_text(encoding="utf-8")
    assert "btn.hidden = !enabled" in pb, (
        "playback/index.js must create the surprise button always and hide it when "
        "disabled (so a live toggle is a pure show/hide)."
    )


def test_lyrics_css_in_shared_queue_css_no_new_file():
    css = QUEUE_CSS.read_text(encoding="utf-8")
    assert ".np-lyrics-pill" in css and ".np-lyrics-panel" in css and ".np-lyric-line" in css, (
        "Lyric pill/panel/line styles must live in the shared static/playback/"
        "queue.css (linked by both templates) — not a new per-page stylesheet."
    )


def test_lyrics_synced_highlight_rides_position_tick():
    """U4: the active-line highlight is driven by the existing position repaint
    path (_renderProgress), NOT a new interval, and lyric state resets on track
    change. The pure picker is unit-tested separately (vm smoke test below)."""
    pb = PLAYBACK_JS.read_text(encoding="utf-8")
    # The highlight updates from the shared repaint path, not a dedicated timer.
    assert re.search(r"function _renderProgress[\s\S]{0,700}_updateActiveLine\(\)", pb), (
        "_updateActiveLine must be called from _renderProgress (the existing "
        "tick/sync/seek repaint path) so the karaoke highlight rides one clock."
    )
    # No lyric-specific interval: the only setInterval owners stay the tick+sync.
    assert pb.count("setInterval") == 2, (
        "Lyrics must not introduce a new setInterval — the highlight rides the "
        "existing 1s tick / 5s sync (the module's only two timers)."
    )
    # Active-line toggling + the pure picker.
    assert "lyricActiveIndex(" in pb and "classList.toggle('active'" in pb, (
        "The synced render must compute the active line via lyricActiveIndex and "
        "toggle the .active highlight class."
    )
    # State resets on track change (refetch path).
    assert re.search(r"_resetLyrics\(id\)", pb) and re.search(r"function _resetLyrics", pb), (
        "applyNowPlaying's track-change branch must call _resetLyrics(id) to clear "
        "+ refetch lyrics so a new track never shows the prior track's words."
    )


def test_lyric_active_index_picker_node_vm_smoke():
    """JS-runtime smoke test (the recurring escape this session): exercise the
    pure active-line picker in a real JS engine over a sample LRC + positions.
    Text-pattern pins can't prove the math; this can. Skips if node is absent."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH; karaoke-index math covered only structurally")
    src_path = ROOT / "static/playback/index.js"
    harness = (
        "const fs=require('fs'),vm=require('vm');"
        f"const src=fs.readFileSync({json.dumps(str(src_path))},'utf8');"
        "const ctx={window:{}};vm.createContext(ctx);vm.runInContext(src,ctx);"
        "const f=ctx.window.__jpLyricActiveIndex;"
        "const L=[{t_ms:1000},{t_ms:5000},{t_ms:12500}];"
        "const got=[f(L,0),f(L,1000),f(L,4999),f(L,5000),f(L,99999),f([],3000),f(null,3000)];"
        "const want=[-1,0,0,1,2,-1,-1];"
        "if(JSON.stringify(got)!==JSON.stringify(want)){"
        "console.error('got',got,'want',want);process.exit(1);}"
        "process.exit(0);"
    )
    result = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, (
        f"lyricActiveIndex picked the wrong line in a real JS engine:\n{result.stderr}"
    )


# ── Random-pick length filter admin control (2026-06-20 plan U4) ──────────────


def test_random_length_filter_control_present():
    """The Setup panel exposes the two length-band inputs with the fixed copy,
    and admin/app.js loads + saves both (always sending, empty → 0 = off)."""
    template = ADMIN_TEMPLATE.read_text(encoding="utf-8")
    assert 'id="random-min-seconds"' in template and 'id="random-max-seconds"' in template, (
        "dashboard.html must render the random-min-seconds and random-max-seconds inputs."
    )
    for phrase in ("Exclude tracks shorter than", "or longer than",
                   "seconds from random choices"):
        assert phrase in template, f"dashboard.html must carry the fixed copy: {phrase!r}"

    admin = ADMIN_APP.read_text(encoding="utf-8")
    assert "random_min_seconds" in admin and "random_max_seconds" in admin, (
        "static/admin/app.js must load and save both random length bounds."
    )
    # Save path must always send both as integers with empty → 0 so clearing a box
    # turns the bound off (deliberately not the guest-n/m 'empty = unchanged').
    assert re.search(r"random_min_seconds\s*=\s*parseInt\([^)]*\)\s*\|\|\s*0", admin), (
        "app.js must send random_min_seconds as parseInt(...) || 0 (empty → off)."
    )
    assert re.search(r"random_max_seconds\s*=\s*parseInt\([^)]*\)\s*\|\|\s*0", admin), (
        "app.js must send random_max_seconds as parseInt(...) || 0 (empty → off)."
    )


# ── Browse grid render containment (2026-06-21 browse-index plan U7) ──────────

def test_browse_grid_has_render_containment():
    """Large Artists/Albums grids must use content-visibility containment so
    off-screen cells skip layout/paint. It MUST keep the cells in the DOM (no
    JS windowing / display:none) — the alpha rail's per-cell anchors and the
    cross-server dedup need the full ordered set."""
    rail_css = (ROOT / "static/browse/rail.css").read_text(encoding="utf-8")
    assert "content-visibility: auto" in rail_css, (
        "rail.css must apply content-visibility:auto to browse cells (plan U7)."
    )
    assert "contain-intrinsic-size" in rail_css, (
        "rail.css must set contain-intrinsic-size so the scrollbar stays sized."
    )
    # Both view modes are covered: tiles and list rows.
    assert ".tile-grid > .tile" in rail_css
    assert ".alpha-items-column > .list-item" in rail_css


def test_browse_containment_lives_in_shared_css_only():
    """R13: the containment rule is single-source in the shared browse
    stylesheet — not forked into a per-page template's inline <style>."""
    for tmpl in (GUEST_TEMPLATE, ADMIN_TEMPLATE):
        src = tmpl.read_text(encoding="utf-8")
        assert "content-visibility" not in src, (
            f"{tmpl.name} must not carry its own content-visibility rule — it "
            f"belongs in the shared static/browse/rail.css (plan U7/R13)."
        )


# ── Collation-derived non-Latin rail buckets (2026-06-22 plan U1/U2) ──────────
# The rail no longer dumps every non-A-Z name into a single '#' pinned to the
# A-end. A rule-independent first-char classifier splits into a leading catch-all
# (symbols/digits, sort before A), the A-Z letters, and a trailing region
# (non-Latin scripts, sort after Z). With no JS test runner, the math is proven
# by running the REAL computeBuckets pipeline in node (precedent:
# test_lyric_active_index_picker_node_vm_smoke) plus structural pins.

_RAIL_NODE_HARNESS = (
    "const fs=require('fs'),vm=require('vm');"
    f"const src=fs.readFileSync({json.dumps(str(ROOT / 'static/browse/index.js'))},'utf8');"
    "const ctx={window:{}};vm.createContext(ctx);vm.runInContext(src,ctx);"
    "const f=ctx.window.__jpComputeBuckets;"
    "if(typeof f!=='function'){console.error('no __jpComputeBuckets hook');process.exit(2);}"
    "const titles=JSON.parse(process.argv[1]);"
    "const latin=JSON.parse(process.argv[2]);"
    "const mk=ts=>ts.map(t=>({title:t}));"
    "const asc=f(mk(titles),'alpha_asc','title');"
    "const desc=f(mk(titles),'alpha_desc','title');"
    "const keyByTitle={};asc.sorted.forEach((t,i)=>{keyByTitle[t]=asc.keyForItem[i];});"
    "const descKeyByTitle={};desc.sorted.forEach((t,i)=>{descKeyByTitle[t]=desc.keyForItem[i];});"
    "const countByKey={};asc.buckets.forEach(b=>{countByKey[b.key]=b.count;});"
    "const out={ascKeys:asc.buckets.map(b=>b.key),descKeys:desc.buckets.map(b=>b.key),"
    "ascSorted:asc.sorted,descSorted:desc.sorted,keyByTitle,descKeyByTitle,countByKey,"
    "latinKeys:f(mk(latin),'alpha_asc','title').buckets.map(b=>b.key)};"
    "console.log(JSON.stringify(out));process.exit(0);"
)


def _run_rail_buckets(titles, latin):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH; rail bucket math covered only structurally")
    result = subprocess.run(
        [node, "-e", _RAIL_NODE_HARNESS, json.dumps(titles), json.dumps(latin)],
        capture_output=True, encoding="utf-8", errors="replace", timeout=30,
    )
    assert result.returncode == 0, (
        f"rail node harness failed (rc={result.returncode}):\n"
        f"STDOUT:{result.stdout}\nSTDERR:{result.stderr}"
    )
    return json.loads(result.stdout)


_RAIL_LETTERS_AZ = list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")


def test_rail_leading_trailing_buckets_node_vm():
    """Covers AE1/AE2/AE5: leading + trailing classification, the asc→desc
    end-flip, monotonicity with the sort, and hidden empty catch-alls — run
    against the real computeBuckets in a JS engine (rules unloaded, so the
    classifier is exercised rule-independently)."""
    titles = ["!!!", "100 gecs", "ABBA", "Beyoncé", "Étienne", "Ångström",
              "Øystein Sevåg", "ßruno", "µ-Ziq", "大沢伸一", "Ωμέγα", "Яндекс", "Zhu"]
    latin = ["ABBA", "Beck", "Zappa"]
    data = _run_rail_buckets(titles, latin)

    # AE2 / R2 / R3: collation order [leading, A..Z, trailing]; desc exact reverse.
    assert data["ascKeys"] == ["#"] + _RAIL_LETTERS_AZ + ["trailing"], data["ascKeys"]
    assert data["descKeys"] == list(reversed(["#"] + _RAIL_LETTERS_AZ + ["trailing"]))

    # R2: classification. Accented Latin folds to its letter even with no rules;
    # non-decomposable Latin (ø, ß) uses the supplementary base map; genuine
    # non-Latin scripts go trailing.
    kbt = data["keyByTitle"]
    assert kbt["!!!"] == "#" and kbt["100 gecs"] == "#"
    assert kbt["Beyoncé"] == "B" and kbt["Étienne"] == "E" and kbt["Ångström"] == "A"
    assert kbt["Øystein Sevåg"] == "O" and kbt["ßruno"] == "S"
    assert kbt["Zhu"] == "Z"
    for t in ("µ-Ziq", "大沢伸一", "Ωμέγα", "Яндекс"):
        assert kbt[t] == "trailing", (t, kbt[t])

    # R7: every item lands in a bucket that exists in the rail (reachable).
    assert set(kbt.values()) <= set(data["ascKeys"])

    # R1: rail monotonic with the sort — the bucket index is non-decreasing
    # across the sorted item list (a stale/mis-placed catch-all would regress).
    idx = {k: i for i, k in enumerate(data["ascKeys"])}
    seq = [idx[kbt[t]] for t in data["ascSorted"]]
    assert seq == sorted(seq), (data["ascSorted"], seq)

    # R6 / AE5: empty catch-alls hidden — a pure-Latin set shows only A-Z.
    assert data["latinKeys"] == _RAIL_LETTERS_AZ, data["latinKeys"]


def test_rail_collation_buckets_structural():
    """Structural pins for the collation-derived bucketing (plan U1)."""
    browse = (ROOT / "static/browse/index.js").read_text(encoding="utf-8")
    assert "window.__jpComputeBuckets" in browse, (
        "static/browse/index.js must expose the window.__jpComputeBuckets test "
        "hook so the pure bucketing pipeline is verifiable in node."
    )
    assert "function _railCharClass" in browse, (
        "the rule-independent first-char classifier _railCharClass must exist."
    )
    assert "/[A-Z]/.test(ch) ? ch : '#'" not in browse, (
        "_computeLetterBuckets still uses the old single-'#' fallback; non-Latin "
        "names must split into leading + trailing buckets (plan U1)."
    )
    assert "_RAIL_LATIN_BASE" not in browse, (
        "the finite Latin base-letter map must be removed; letter classification "
        "is now comparator-derived against the A-Z anchors (plan 003 U1)."
    )


def test_rail_latin_extended_letters_bucket_among_az_node_vm():
    """Covers AE1/AE3: Latin-extended letters that neither NFD-decompose nor
    appear in the old supplementary map (ƒ U+0192, ŧ U+0167) must bucket under
    the A-Z letter they collate among (ƒIN→F, ŧrip→T), not the trailing 'Other
    scripts' region pinned past Z. Regression for the ƒIN-by-John-Talbot
    backward-jump bug. The non-decomposing letters the map used to cover (ø, ß)
    must stay correct after the map is removed, and genuine non-Latin scripts
    must still trail."""
    titles = ["Fugazi", "ƒIN", "Talk Talk", "ŧrip", "Øystein Sevåg", "ßruno",
              "µ-Ziq", "大沢伸一", "Zelda"]
    data = _run_rail_buckets(titles, ["Beck"])
    kbt = data["keyByTitle"]
    # The bug: these collate among the Latin letters, not after Z.
    assert kbt["ƒIN"] == "F", kbt["ƒIN"]
    assert kbt["ŧrip"] == "T", kbt["ŧrip"]
    # Map removal must not regress the non-decomposing letters it used to cover.
    assert kbt["Øystein Sevåg"] == "O" and kbt["ßruno"] == "S"
    # Genuine non-Latin scripts still trail (each below the per-group minimum,
    # so they collapse into the single 'trailing' bucket).
    assert kbt["µ-Ziq"] == "trailing" and kbt["大沢伸一"] == "trailing"
    # R7: every item lands in a bucket that exists in the rail (reachable).
    assert set(kbt.values()) <= set(data["ascKeys"])
    # R9: monotonic with the sort in BOTH directions — a ƒ-class item placed in
    # the trailing region would jump backward in asc (and the symmetric way in
    # desc), so assert non-decreasing bucket index across each sorted list.
    aidx = {k: i for i, k in enumerate(data["ascKeys"])}
    aseq = [aidx[kbt[t]] for t in data["ascSorted"]]
    assert aseq == sorted(aseq), ("asc", data["ascSorted"], aseq)
    didx = {k: i for i, k in enumerate(data["descKeys"])}
    dkbt = data["descKeyByTitle"]
    dseq = [didx[dkbt[t]] for t in data["descSorted"]]
    assert dseq == sorted(dseq), ("desc", data["descSorted"], dseq)


def test_rail_adaptive_per_script_trailing_node_vm():
    """Covers AE3/AE4/AE6: the trailing region splits per-script only when 2+
    script groups each clear the per-group minimum; a small/single-script tail
    stays one bucket; too many distinct groups collapse back to one. Run against
    the real adaptive logic in node."""
    # AE4 — multi-script tail (3 each) splits into per-script buckets, ordered by
    # appearance (= collation: Greek < Cyrillic < Han).
    multi = ["Ωμέγα", "Άλφα", "Βήτα", "Яндекс", "Борис", "Вера",
             "大沢伸一", "北京", "上海"]
    data = _run_rail_buckets(multi, ["Beck"])
    trailing = [k for k in data["ascKeys"] if k.startswith("trailing")]
    # Han + kana fold to one 'CJK' group (U3 grouping); Hangul would be separate.
    assert trailing == ["trailing:Greek", "trailing:Cyrillic", "trailing:CJK"], trailing
    kbt = data["keyByTitle"]
    assert kbt["Ωμέγα"] == "trailing:Greek"
    assert kbt["Яндекс"] == "trailing:Cyrillic"
    assert kbt["大沢伸一"] == "trailing:CJK"
    # Monotonic with the sort across the split buckets — BOTH directions. The
    # trailing region must collate after Z in asc and before Z in desc; an
    # accidental double-reverse passes asc but breaks desc, so assert both.
    idx = {k: i for i, k in enumerate(data["ascKeys"])}
    seq = [idx[kbt[t]] for t in data["ascSorted"]]
    assert seq == sorted(seq), ("asc", data["ascSorted"], seq)
    didx = {k: i for i, k in enumerate(data["descKeys"])}
    dkbt = data["descKeyByTitle"]
    dseq = [didx[dkbt[t]] for t in data["descSorted"]]
    assert dseq == sorted(dseq), ("desc", data["descSorted"], dseq)
    # Histogram parity: per-script counts sum to the full non-Latin tail (none lost).
    assert sum(data["countByKey"][k] for k in trailing) == len(multi)

    # AE3 — the reporter's small mixed tail (µ-Ziq + one CJK act) stays a SINGLE
    # trailing bucket, not a per-script split (each group below the minimum).
    small = _run_rail_buckets(["µ-Ziq", "大沢伸一", "Beck"], ["Beck"])
    small_trailing = [k for k in small["ascKeys"] if k.startswith("trailing")]
    assert small_trailing == ["trailing"], small_trailing
    assert small["keyByTitle"]["µ-Ziq"] == "trailing"
    assert small["keyByTitle"]["大沢伸一"] == "trailing"

    # AE6 — more distinct script groups than the cap collapses to one bucket even
    # though two groups qualify (Greek, Cyrillic ×3). Nine distinct groups under
    # the U3 grouping: Greek, Cyrillic, CJK, Hangul, Arabic, Hebrew, Thai,
    # Devanagari, Other(µ).
    over = ["Ωμέγα", "Άλφα", "Βήτα", "Яндекс", "Борис", "Вера",
            "大", "の", "한", "ع", "א", "ก", "अ", "µ-Ziq"]
    over_data = _run_rail_buckets(over, ["Beck"])
    over_trailing = [k for k in over_data["ascKeys"] if k.startswith("trailing")]
    assert over_trailing == ["trailing"], over_trailing


def test_rail_trailing_script_cap_is_distinct_from_time_cap():
    """The trailing-script split cap is its own constant — the pinned time cap
    (RAIL_BUCKET_CAP = 25) must not be repurposed for it."""
    browse = (ROOT / "static/browse/index.js").read_text(encoding="utf-8")
    assert re.search(r"RAIL_TRAILING_SCRIPT_CAP\s*=\s*\d+", browse), (
        "U2 must define RAIL_TRAILING_SCRIPT_CAP (bounds the per-script split)."
    )
    assert "\\p{Script" in browse, (
        "per-script detection must use Unicode \\p{Script=…} property escapes."
    )
    assert "function _railScriptGroup" in browse, (
        "the per-script grouping helper _railScriptGroup must exist."
    )


# ── International rail (2026-06-22 plan 004 U4) ───────────────────────────────

_RAIL_INTL_HARNESS = (
    "const fs=require('fs'),vm=require('vm');"
    f"const src=fs.readFileSync({json.dumps(str(ROOT / 'static/browse/index.js'))},'utf8');"
    "const ctx={window:{}};vm.createContext(ctx);vm.runInContext(src,ctx);"
    "const f=ctx.window.__jpComputeBuckets;"
    "if(typeof f!=='function'){console.error('no __jpComputeBuckets hook');process.exit(2);}"
    "const titles=JSON.parse(process.argv[1]);"
    "const threshold=JSON.parse(process.argv[2]);"
    "const mk=ts=>ts.map(t=>({title:t}));"
    "const alpha={mode:'international',threshold};"
    "const asc=f(mk(titles),'alpha_asc','title',alpha);"
    "const desc=f(mk(titles),'alpha_desc','title',alpha);"
    "const keyByTitle={};asc.sorted.forEach((t,i)=>{keyByTitle[t]=asc.keyForItem[i];});"
    "const descKeyByTitle={};desc.sorted.forEach((t,i)=>{descKeyByTitle[t]=desc.keyForItem[i];});"
    "const countByKey={};asc.buckets.forEach(b=>{countByKey[b.key]=b.count;});"
    "const out={ascKeys:asc.buckets.map(b=>b.key),descKeys:desc.buckets.map(b=>b.key),"
    "ascSorted:asc.sorted,descSorted:desc.sorted,keyByTitle,descKeyByTitle,countByKey};"
    "console.log(JSON.stringify(out));process.exit(0);"
)


def _run_rail_intl(titles, threshold):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH; international rail math covered structurally")
    result = subprocess.run(
        [node, "-e", _RAIL_INTL_HARNESS, json.dumps(titles), json.dumps(threshold)],
        capture_output=True, encoding="utf-8", errors="replace", timeout=30,
    )
    assert result.returncode == 0, (
        f"intl rail harness failed (rc={result.returncode}):\n"
        f"STDOUT:{result.stdout}\nSTDERR:{result.stderr}"
    )
    return json.loads(result.stdout)


def _assert_intl_monotonic(data):
    """The bucket index must be non-decreasing across the sorted list in BOTH
    directions, and descKeys must be the exact reverse of ascKeys (data-derived,
    single reversal — guards against the double-reverse class)."""
    aidx = {k: i for i, k in enumerate(data["ascKeys"])}
    aseq = [aidx[data["keyByTitle"][t]] for t in data["ascSorted"]]
    assert aseq == sorted(aseq), ("asc not monotonic", data["ascSorted"], aseq)
    didx = {k: i for i, k in enumerate(data["descKeys"])}
    dseq = [didx[data["descKeyByTitle"][t]] for t in data["descSorted"]]
    assert dseq == sorted(dseq), ("desc not monotonic", data["descSorted"], dseq)
    assert data["descKeys"] == list(reversed(data["ascKeys"])), (
        data["ascKeys"], data["descKeys"])


def test_rail_international_per_character_buckets_node_vm():
    """Covers AE2: International mode builds a bucket per first character that meets
    the threshold — incl. per-CJK-ideograph buckets, not one collapsed script
    bucket. 大/北 have 2 artists each (kept at threshold 2); 上 has 1 (suppressed
    but its item folds into a shown neighbour and stays in the list)."""
    titles = ["大壹", "大貳", "北壹", "北貳", "上海"]
    data = _run_rail_intl(titles, 2)
    assert set(data["ascKeys"]) == {"大", "北"}, data["ascKeys"]
    kbt = data["keyByTitle"]
    assert kbt["大壹"] == "大" and kbt["北壹"] == "北"
    assert kbt["上海"] in set(data["ascKeys"])        # suppressed → folded, reachable
    assert "上海" in data["ascSorted"]                 # still present in the list
    _assert_intl_monotonic(data)


def test_rail_international_suppresses_sparse_latin_node_vm():
    """Covers AE3: a lone Latin letter is suppressed at threshold 2; its item folds
    into the preceding shown bucket and remains in the list (reachable by scroll).
    Keys are lowercase (sortKey lowercases first)."""
    titles = ["Aaa", "Aab", "Quux", "Zaa", "Zab"]      # a:2, q:1, z:2
    data = _run_rail_intl(titles, 2)
    assert set(data["ascKeys"]) == {"a", "z"}, data["ascKeys"]
    kbt = data["keyByTitle"]
    assert kbt["Quux"] == "a"                           # folds into preceding shown bucket
    assert "Quux" in data["ascSorted"]
    _assert_intl_monotonic(data)


def test_rail_international_groups_symbols_under_hash_node_vm():
    """Covers AE4: symbols/digits collapse into a single '#' bucket (not per-char),
    and '#' shows whenever non-empty even below threshold (it is not gated)."""
    titles = ["!!!", "Aaa", "Aab"]                      # '#':1 (below threshold), a:2
    data = _run_rail_intl(titles, 2)
    assert data["ascKeys"] == ["#", "a"], data["ascKeys"]
    assert "!" not in data["ascKeys"] and "1" not in data["ascKeys"]
    assert data["keyByTitle"]["!!!"] == "#"
    _assert_intl_monotonic(data)


def test_rail_international_threshold_one_keeps_every_first_char_node_vm():
    """At threshold 1 nothing is suppressed — every present first character gets a
    bucket."""
    titles = ["Aaa", "Quux", "大海"]
    data = _run_rail_intl(titles, 1)
    assert set(data["ascKeys"]) == {"a", "q", "大"}, data["ascKeys"]
    _assert_intl_monotonic(data)


def test_rail_international_monotonic_mixed_scripts_node_vm():
    """The keystone (R5/R9): over a mixed symbols + Latin + Cyrillic + CJK library
    the rail stays monotonic with the sort in BOTH directions."""
    titles = ["!!!", "Aaa", "Aab", "Mmm", "Mno", "Яша", "Яна", "大壹", "大貳"]
    data = _run_rail_intl(titles, 2)
    _assert_intl_monotonic(data)


# ── Artist sort + artist-keyed rail (2026-06-22 plan 005 U1) ──────────────────
# The Year drilldown can sort/index by ARTIST, not just album title. The artist
# sort orders by the artist field with album title as the ascending tiebreak; the
# rail keys off the artist field via nameField='artist'. Proven against the real
# applySort + resolveRailDimension + computeBuckets pipeline in node. The hook
# returns `items` (the full sorted objects) so tests can read both title+artist.

_RAIL_ARTIST_HARNESS = (
    "const fs=require('fs'),vm=require('vm');"
    f"const src=fs.readFileSync({json.dumps(str(ROOT / 'static/browse/index.js'))},'utf8');"
    "const ctx={window:{}};vm.createContext(ctx);vm.runInContext(src,ctx);"
    "const f=ctx.window.__jpComputeBuckets;"
    "if(typeof f!=='function'){console.error('no __jpComputeBuckets hook');process.exit(2);}"
    "const rows=JSON.parse(process.argv[1]);"          # [[title, artist], ...]
    "const intl=JSON.parse(process.argv[2]);"          # null | threshold int
    "const mk=rs=>rs.map(r=>({title:r[0],artist:r[1]}));"
    "const alpha=intl===null?undefined:{mode:'international',threshold:intl};"
    "const asc=f(mk(rows),'artist_asc','artist',alpha);"
    "const desc=f(mk(rows),'artist_desc','artist',alpha);"
    "if(!asc.items||!desc.items){console.error('hook missing items[]');process.exit(3);}"
    "const keyByTitle={};asc.items.forEach((it,i)=>{keyByTitle[it.title]=asc.keyForItem[i];});"
    "const descKeyByTitle={};desc.items.forEach((it,i)=>{descKeyByTitle[it.title]=desc.keyForItem[i];});"
    "const out={ascKeys:asc.buckets.map(b=>b.key),descKeys:desc.buckets.map(b=>b.key),"
    "ascTitles:asc.items.map(it=>it.title),descTitles:desc.items.map(it=>it.title),"
    "ascArtists:asc.items.map(it=>it.artist),descArtists:desc.items.map(it=>it.artist),"
    "keyByTitle,descKeyByTitle};"
    "console.log(JSON.stringify(out));process.exit(0);"
)


def _run_rail_artist(rows, intl=None):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH; artist rail math covered structurally")
    result = subprocess.run(
        [node, "-e", _RAIL_ARTIST_HARNESS, json.dumps(rows), json.dumps(intl)],
        capture_output=True, encoding="utf-8", errors="replace", timeout=30,
    )
    assert result.returncode == 0, (
        f"artist rail harness failed (rc={result.returncode}):\n"
        f"STDOUT:{result.stdout}\nSTDERR:{result.stderr}"
    )
    return json.loads(result.stdout)


def _assert_artist_monotonic(data):
    """Bucket index non-decreasing across the sorted list in BOTH directions, and
    descKeys is the exact reverse of ascKeys — same guard as the intl rail."""
    aidx = {k: i for i, k in enumerate(data["ascKeys"])}
    aseq = [aidx[data["keyByTitle"][t]] for t in data["ascTitles"]]
    assert aseq == sorted(aseq), ("asc not monotonic", data["ascTitles"], aseq)
    didx = {k: i for i, k in enumerate(data["descKeys"])}
    dseq = [didx[data["descKeyByTitle"][t]] for t in data["descTitles"]]
    assert dseq == sorted(dseq), ("desc not monotonic", data["descTitles"], dseq)
    assert data["descKeys"] == list(reversed(data["ascKeys"])), (
        data["ascKeys"], data["descKeys"])


def test_artist_sort_orders_by_artist_node_vm():
    """Covers AE2 / R5: artist_asc orders albums by their artist (not title)."""
    rows = [["Zoo", "ABBA"], ["Apple", "Beck"]]
    data = _run_rail_artist(rows)
    assert data["ascArtists"] == ["ABBA", "Beck"], data["ascArtists"]
    # Title order is irrelevant to artist sort — "Zoo" (ABBA) precedes "Apple" (Beck).
    assert data["ascTitles"] == ["Zoo", "Apple"], data["ascTitles"]


def test_artist_sort_title_tiebreak_node_vm():
    """R5 / Decision 2: within one artist, albums read title-ascending in BOTH
    directions (artist_desc flips only the artist comparison)."""
    rows = [["Bee", "X"], ["Ant", "X"], ["Cat", "X"]]
    data = _run_rail_artist(rows)
    assert data["ascTitles"] == ["Ant", "Bee", "Cat"], data["ascTitles"]
    assert data["descTitles"] == ["Ant", "Bee", "Cat"], data["descTitles"]


def test_artist_sort_desc_reverses_artist_node_vm():
    """R5: artist_desc reverses the artist ordering (Z→A by artist)."""
    rows = [["t1", "ABBA"], ["t2", "Beck"], ["t3", "Cher"]]
    data = _run_rail_artist(rows)
    assert data["ascArtists"] == ["ABBA", "Beck", "Cher"]
    assert data["descArtists"] == ["Cher", "Beck", "ABBA"], data["descArtists"]


def test_artist_rail_keys_off_artist_node_vm():
    """Covers AE2 / R6: the rail buckets by the ARTIST first character, not the
    album title's. English-friendly mode shows the fixed A–Z scaffold, so the
    keystone is the per-item keys: "Zoo"/ABBA buckets under A (NOT title Z);
    "Apple"/Beck and "Mango"/Beck under B."""
    rows = [["Zoo", "ABBA"], ["Apple", "Beck"], ["Mango", "Beck"]]
    data = _run_rail_artist(rows)
    assert data["ascKeys"] == _RAIL_LETTERS_AZ, data["ascKeys"]   # fixed A–Z scaffold
    kbt = data["keyByTitle"]
    assert kbt["Zoo"] == "A"            # artist ABBA, NOT title Z
    assert kbt["Apple"] == "B" and kbt["Mango"] == "B"
    _assert_artist_monotonic(data)


def test_artist_rail_international_node_vm():
    """Covers AE5 / R7: International mode builds the artist rail from the artist
    first characters present, suppressing those below threshold and folding their
    items into a shown neighbour (still reachable). 大 has 2 artists, 上 has 1."""
    rows = [["a1", "大壹"], ["a2", "大貳"], ["a3", "北京"], ["a4", "北方"], ["a5", "上海"]]
    data = _run_rail_artist(rows, intl=2)
    assert set(data["ascKeys"]) == {"大", "北"}, data["ascKeys"]
    kbt = data["keyByTitle"]
    assert kbt["a1"] == "大" and kbt["a3"] == "北"
    assert kbt["a5"] in set(data["ascKeys"])     # suppressed → folded, reachable
    assert "a5" in data["ascTitles"]             # still present in the list
    _assert_artist_monotonic(data)


def test_year_drilldown_sort_wiring_structural():
    """Plan 005 U2: the Year drilldown gains a sort control + scrubber rail.
    Structural pins on the year-pane render block — the DOM render isn't unit
    tested (no pane renders in a test runner today), and the behavioral
    correctness (sort order, artist-keyed buckets, monotonicity, intl
    suppression) is covered by the artist/intl node-vm tests above since the year
    pane reuses the same shared primitives."""
    browse = (ROOT / "static/browse/index.js").read_text(encoding="utf-8")
    start = browse.index("function showYearAlbums(year)")
    end = browse.index("// ── Search", start)
    block = browse[start:end]

    # R1 / R2: themed sort control with the five labelled options.
    assert "createSortControl(" in block and "'year-albums-sort'" in block
    for tok in ("'alpha_asc', 'Title A → Z'", "'alpha_desc', 'Title Z → A'",
                "'artist_asc', 'Artist A → Z'", "'artist_desc', 'Artist Z → A'",
                "'popular', 'Most Played'"):
        assert tok in block, tok
    # R3: no year sort in the year drilldown (it would be a no-op in one year).
    assert "year_asc" not in block and "year_desc" not in block, \
        "year-sort must be excluded from the Year drilldown"
    # R6: rail wiring follows the sort.
    for tok in ("list-with-rail", "computeBuckets(", "_setBucketStart(",
                "_activateRail(", "_deactivateRail("):
        assert tok in block, tok
    # R6: rail keyed by artist for artist sorts, else title.
    assert "startsWith('artist')" in block and "'artist' : 'title'" in block
    # R7: international suppression uses the album threshold.
    assert "_alphaConfig('albums')" in block
    # R10: Most Played via album play-counts with a 0-play prune.
    assert "/api/play-counts?type=album" in block
    assert "countMap[a.title] || 0) > 0" in block
    # R9: the sort onChange re-renders items only — the year albums are fetched
    # exactly once (in the load path), never on a sort change.
    assert "_renderYearAlbumsItems()" in block
    assert block.count("/api/browse/years/") == 1, \
        "year albums must be fetched once (load only), not re-fetched on sort"


# ── Recently Added: relative add-date formatter (plan 006 U4) ─────────────────

_ADDED_AGO_HARNESS = (
    "const fs=require('fs'),vm=require('vm');"
    f"const src=fs.readFileSync({json.dumps(str(ROOT / 'static/browse/index.js'))},'utf8');"
    "const ctx={window:{}};vm.createContext(ctx);vm.runInContext(src,ctx);"
    "const f=ctx.window.__jpAddedAgo;"
    "if(typeof f!=='function'){console.error('no __jpAddedAgo hook');process.exit(2);}"
    "const now=1000000000,day=86400;"
    "const cases=[[now,now],[now-1*day,now],[now-6*day,now],[now-7*day,now],"
    "[now-21*day,now],[now-30*day,now],[now-365*day,now],[now-730*day,now],"
    "[null,now],[0,now]];"
    "console.log(JSON.stringify(cases.map(c=>f(c[0],c[1]))));process.exit(0);"
)


def test_added_ago_relative_buckets_node_vm():
    """Plan 006 U4: the relative add-date formatter buckets at the
    today/Nd/Nw/Nmo/Ny boundaries deterministically (injected `now`); null/0
    yield an empty label. Run against the real function in a JS engine."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH; added-ago formatter covered only structurally")
    result = subprocess.run(
        [node, "-e", _ADDED_AGO_HARNESS],
        capture_output=True, encoding="utf-8", errors="replace", timeout=30,
    )
    assert result.returncode == 0, (
        f"added-ago harness failed (rc={result.returncode}):\n"
        f"STDOUT:{result.stdout}\nSTDERR:{result.stderr}"
    )
    got = json.loads(result.stdout)
    assert got == ["today", "1d ago", "6d ago", "1w ago", "3w ago",
                   "1mo ago", "1y ago", "2y ago", "", ""]


def test_added_ago_hook_exposed():
    """Structural: the formatter hook is exposed for the node-vm test."""
    browse = (ROOT / "static/browse/index.js").read_text(encoding="utf-8")
    assert "window.__jpAddedAgo" in browse, (
        "static/browse/index.js must expose window.__jpAddedAgo so the relative "
        "add-date formatter is verifiable in node."
    )


# ── Recently Added: tab view + wiring (plan 006 U5) ──────────────────────────

def test_recently_added_loader_wiring():
    """The loader exists, is hooked into the tab-activation switch, reuses the
    shared album row + relative label, clears the rail, and has an empty state."""
    browse = (ROOT / "static/browse/index.js").read_text(encoding="utf-8")
    assert "function loadRecentlyAdded" in browse
    assert "recentlyadded-view" in browse and "loadRecentlyAdded()" in browse
    assert "_appendAlbumRow" in browse
    assert "_addedAgo" in browse
    assert "_deactivateRail()" in browse
    assert "/api/recently-added" in browse
    assert "Nothing added yet." in browse


def test_recently_added_tab_present_on_both_pages():
    """Shared-UI standard: the tab + its view container land on Guest AND Admin."""
    guest = (ROOT / "app/templates/guest/index.html").read_text(encoding="utf-8")
    admin = (ROOT / "app/templates/admin/dashboard.html").read_text(encoding="utf-8")
    for tpl, label in ((guest, "guest"), (admin, "admin")):
        assert 'data-view="recentlyadded-view"' in tpl, f"{label} missing Recently Added tab"
        assert 'id="recentlyadded-list"' in tpl, f"{label} missing recentlyadded-list container"


def test_recently_added_registered_in_guest_browse_views():
    """The guest view-switch gate (BROWSE_VIEWS) must include the new view or the
    tab is inert (admin forwards unconditionally; guest gates)."""
    guest_app = (ROOT / "static/guest/app.js").read_text(encoding="utf-8")
    m = re.search(r"BROWSE_VIEWS = new Set\(\[([^\]]*)\]\)", guest_app)
    assert m and "recentlyadded-view" in m.group(1)


def test_recently_added_rows_navigate_via_nav_descriptor():
    """Regression (ce-debug): Recently Added rows must hand _appendAlbumRow a nav
    descriptor so the album drill-in renders into THIS list and back returns
    here. Passing null routes showAlbumTracks to the legacy hidden artists-list
    target, so the row looks unclickable. Model: showArtistAlbums."""
    browse = (ROOT / "static/browse/index.js").read_text(encoding="utf-8")
    start = browse.index("async function loadRecentlyAdded")
    end = browse.index("let yearsData", start)
    block = browse[start:end]
    assert "_appendAlbumRow(el, album, nav)" in block, \
        "Recently Added must pass a nav descriptor to _appendAlbumRow"
    assert "_appendAlbumRow(el, album, null)" not in block, \
        "passing null nav misroutes the album drill-in to the hidden artists-list"
    assert "Recently Added" in block and "reenter" in block, \
        "the nav must carry a Recently Added origin + reenter so back returns here"


# ── Same-title disambiguator: track-count labels on colliding rows (U6) ───────
# Look-alike album rows (same title+artist, e.g. remasters/regional editions
# Plex filed under one title) carry a "N tracks" label ONLY when their track
# counts differ, so the user can tell them apart. Identical-count collisions and
# lone albums stay unlabeled. The pure batch helper is verified in node.

_COUNT_LABEL_HARNESS = (
    "const fs=require('fs'),vm=require('vm');"
    f"const src=fs.readFileSync({json.dumps(str(ROOT / 'static/browse/index.js'))},'utf8');"
    "const ctx={window:{}};vm.createContext(ctx);vm.runInContext(src,ctx);"
    "const f=ctx.window.__jpAlbumCountLabels;"
    "if(typeof f!=='function'){console.error('no __jpAlbumCountLabels hook');process.exit(2);}"
    "console.log(JSON.stringify(f(JSON.parse(process.argv[1]))));process.exit(0);"
)


def _run_count_labels(albums):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH; count-label math covered only structurally")
    result = subprocess.run(
        [node, "-e", _COUNT_LABEL_HARNESS, json.dumps(albums)],
        capture_output=True, encoding="utf-8", errors="replace", timeout=30,
    )
    assert result.returncode == 0, (
        f"count-label node harness failed (rc={result.returncode}):\n"
        f"STDOUT:{result.stdout}\nSTDERR:{result.stderr}"
    )
    return json.loads(result.stdout)


def test_album_count_labels_flags_only_differing_count_collisions_node_vm():
    """Covers AE5. Same title+artist with DIFFERENT counts → both flagged with
    their counts; same-count collisions (identical-tracklist masters) and lone
    albums → nothing to show."""
    albums = [
        {"id": "A:US", "title": "Further Down the Spiral",
         "artist": "Nine Inch Nails", "track_count": 13},
        {"id": "A:JP", "title": "Further Down the Spiral",
         "artist": "Nine Inch Nails", "track_count": 15},
        {"id": "A:L1", "title": "Loveless", "artist": "My Bloody Valentine", "track_count": 11},
        {"id": "A:L2", "title": "Loveless", "artist": "My Bloody Valentine", "track_count": 11},
        {"id": "A:OK", "title": "OK Computer", "artist": "Radiohead", "track_count": 12},
    ]
    labels = _run_count_labels(albums)
    # FDS split by count; same-count Loveless pair and the lone OK Computer unlabeled.
    assert labels == {"A:US": 13, "A:JP": 15}


def test_album_count_labels_structural():
    """The hook exists and BOTH list renderers consult it — the user hit the
    collision on the Albums tab AND in an artist's releases."""
    browse = (ROOT / "static/browse/index.js").read_text(encoding="utf-8")
    assert "window.__jpAlbumCountLabels" in browse, (
        "static/browse/index.js must expose window.__jpAlbumCountLabels so the "
        "collision disambiguator is verifiable in node."
    )
    assert browse.count("_albumCountLabels(") >= 3, (
        "_albumCountLabels must be defined AND consulted by both album list "
        "renderers (renderAlbumsItems for the Albums tab and the artist-releases "
        "render that feeds _appendAlbumRow)."
    )


# ── Closing Time admin form (2026-06-24 plan U4) ─────────────────────────────

def test_admin_closing_time_controls_present_and_wired():
    """The Setup form exposes the four Closing Time controls, and admin/app.js
    references all four settings keys (hydrate in loadSettings + save handler)."""
    tmpl = ADMIN_TEMPLATE.read_text(encoding="utf-8")
    for el_id in ("closing-time-enabled", "closing-time-title",
                  "closing-time-artist", "closing-time-message"):
        assert f'id="{el_id}"' in tmpl, f"dashboard.html missing #{el_id}"
    app_js = (ROOT / "static/admin/app.js").read_text(encoding="utf-8")
    for key in ("closing_time_enabled", "closing_time_title",
                "closing_time_artist", "closing_time_message"):
        # Each key appears at least twice: hydrate (s.<key>) and save (body.<key>).
        assert app_js.count(key) >= 2, f"admin/app.js must hydrate AND save {key}"


def test_closing_banner_non_blocking_on_admin():
    """Lockout fix (2026-06-24): the admin banner must NOT trap interaction. Admin
    mounts playback as a control surface; the shared module renders the is-control
    variant + Resume button; the CSS makes that variant click-through so the host
    can keep queueing and skip/resume to restart."""
    admin = (ROOT / "static/admin/app.js").read_text(encoding="utf-8")
    assert "controlSurface: true" in admin, "admin must mount playback as a control surface"
    assert "onClosingResume" in admin, "admin must wire the Resume action"
    shared = (ROOT / "static/playback/index.js").read_text(encoding="utf-8")
    assert "is-control" in shared and "closing-time-resume" in shared, (
        "shared module must render the non-blocking control variant + Resume button"
    )
    css = (ROOT / "static/playback/queue.css").read_text(encoding="utf-8")
    m = re.search(r"\.closing-time-overlay\.is-control\s*\{([^}]*)\}", css)
    assert m and "pointer-events: none" in m.group(1), (
        "the admin (is-control) banner container must be click-through "
        "(pointer-events: none) so it doesn't lock the host out"
    )


# ── Browse-load freeze (2026-06-24 ce-debug) ─────────────────────────────────
# Since the cross-server browse-index caching refactor, the warm index returns the
# FULL catalog instantly, so the old single synchronous forEach over every
# artist/album blocked the main thread for seconds on big libraries (frozen tab).
# The render now streams cells in rAF-yielded slices and the top-level rows use one
# delegated click listener instead of ~2 per row. Structural pins + a node VM run
# of the real _renderCellsChunked (precedent: the rail bucket node harnesses).


def test_browse_large_list_render_is_chunked_structural():
    browse = (ROOT / "static/browse/index.js").read_text(encoding="utf-8")
    assert "function _renderCellsChunked(" in browse, (
        "the non-blocking chunked renderer must exist (browse-load-freeze fix)."
    )
    assert "window.__jpRenderCellsChunked" in browse, (
        "expose the __jpRenderCellsChunked test hook so the chunking is node-verifiable."
    )
    # Both top-level list views must stream through the chunked renderer rather
    # than a synchronous forEach — a sync build over the full cross-server catalog
    # is the freeze.
    assert browse.count("_renderCellsChunked(column, sorted.length") == 2, (
        "both renderArtistsItems and renderAlbumsItems must route through "
        "_renderCellsChunked; a synchronous forEach over the full list reintroduces "
        "the multi-second main-thread freeze."
    )
    assert "requestAnimationFrame(step)" in browse, (
        "the chunk loop must yield to the browser between slices via rAF."
    )
    # The chunk loop honours each view's generation guard so a superseded build aborts.
    assert "_artistsItemsGen !== gen" in browse and "_albumsItemsGen !== gen" in browse, (
        "the chunked build must abort when a newer render claims the pane."
    )


def test_browse_art_requests_sized_thumbnails():
    """Browse art must request a width-sized thumbnail (w=) + async decode, not the
    full cover — decoding a ~150KB cover to paint a 48px row was the deep-jump
    content-visibility reveal stall (2026-06-25, confirmed via live A/B in
    tools/perf/browse-bench: hiding art dropped the worst reveal task ~600ms→~70ms)."""
    browse = (ROOT / "static/browse/index.js").read_text(encoding="utf-8")
    assert "/api/art?path=" in browse
    assert "&w=" in browse, "art URLs must carry a width hint (&w=) for resized thumbnails"
    assert 'decoding="async"' in browse, "art must decode off the main thread"


def test_rail_tap_jump_is_instant_not_smooth():
    """The rail tap must jump instantly (direct scrollTop, like the drag-scrub),
    not via scrollIntoView({behavior:'smooth'}). Smooth animates *through* every
    band between origin and target, forcing render of the whole traversal — ~0.6–1s
    of main-thread long tasks per tap on a large library (confirmed live via
    tools/perf/browse-bench, A/B smooth vs instant; 2026-06-24 browse-stall)."""
    browse = (ROOT / "static/browse/index.js").read_text(encoding="utf-8")
    assert "behavior: 'smooth'" not in browse and 'behavior: "smooth"' not in browse, (
        "no smooth-scroll jumps in the browse module: the rail tap must set "
        "scrollTop directly (instant) so it renders only the destination, not "
        "every band in the traversal."
    )
    # The tap handler resolves the scroll container and sets scrollTop on it.
    assert "scrollRoot.scrollTop = target.offsetTop" in browse, (
        "rail tap-to-jump must position via direct scrollTop on the scroll ancestor."
    )


def test_rail_geometry_is_containment_safe_structural():
    """Rail geometry must not trust one-shot offset reads under
    content-visibility render containment (2026-08-04 rail-tracking debug).

    Off-screen cells occupy their contain-intrinsic-size ESTIMATE (rail.css)
    until first rendered, so:
    - a single scrollTop write from target.offsetTop lands past the bucket
      start (~4 rows deep on a large library) once the destination band
      realizes at true sizes — jumps must go through _settleJump, which
      re-corrects across frames until the target is flush;
    - an activation-time snapshot of marker offsets drifts by thousands of px
      as cells realize while scrolling (highlight lagged 2-3 letters) — the
      highlight handler must cache marker ELEMENTS (_alphaMarkers) and read
      offsetTop live each firing.
    Behavioral proof: tools/perf/browse-bench.html driven headless (drift
    -266px -> -0.1px with the fix)."""
    browse = (ROOT / "static/browse/index.js").read_text(encoding="utf-8")
    assert "function _settleJump" in browse, (
        "rail jumps need the _settleJump convergence loop: one scrollTop write "
        "lands mid-letter under content-visibility size estimates."
    )
    call_sites = browse.count("_settleJump(scrollRoot")
    assert call_sites >= 2, (
        f"_settleJump must run on BOTH jump paths (tap handler + drag release); "
        f"found {call_sites} call site(s)."
    )
    assert "_alphaMarkers" in browse, (
        "the highlight handler must keep the marker-element cache (_alphaMarkers) "
        "and read offsets live per firing."
    )
    assert "offsets.push([el.offsetTop" not in browse and "_alphaSortedOffsets" not in browse, (
        "no frozen offset snapshots: an activation-time [offsetTop, key] table "
        "goes stale as content-visibility cells realize their true size."
    )


def test_browse_top_level_rows_use_event_delegation_structural():
    browse = (ROOT / "static/browse/index.js").read_text(encoding="utf-8")
    assert "function _wireListDelegation(" in browse, (
        "top-level Artists/Albums lists must delegate clicks (throughput half of "
        "the browse-load-freeze fix)."
    )
    assert "_jpItem" in browse, (
        "delegated rows must stash their item object on _jpItem for the handler."
    )
    # The inline list builders must NOT attach a per-row click listener anymore;
    # ~2N listeners + closures were a large share of the build cost. (Drill-in /
    # year / search builders use `row.addEventListener`, not `cell.`.)
    assert "cell.addEventListener('click'" not in browse, (
        "the inline Artists/Albums row builders must be listener-less (delegated); "
        "a per-row click listener reintroduces the 2N-listener build cost."
    )


_CHUNK_NODE_HARNESS = (
    "const fs=require('fs'),vm=require('vm');"
    f"const src=fs.readFileSync({json.dumps(str(ROOT / 'static/browse/index.js'))},'utf8');"
    # Each performance.now() call advances 100ms (> the 10ms budget), so exactly
    # one cell is built per slice — deterministic proof of yielding + completeness.
    "let clock=0;const rafQ=[];"
    "const mkFrag=()=>({items:[],appendChild(x){this.items.push(x);}});"
    "const ctx={window:{},performance:{now:()=>{clock+=100;return clock;}},"
    "requestAnimationFrame:(cb)=>{rafQ.push(cb);},"
    "document:{createDocumentFragment:mkFrag}};"
    "vm.createContext(ctx);vm.runInContext(src,ctx);"
    "const f=ctx.window.__jpRenderCellsChunked;"
    "if(typeof f!=='function'){console.error('no __jpRenderCellsChunked hook');process.exit(2);}"
    "const drain=()=>{let n=0;while(rafQ.length){rafQ.shift()();if(++n>100000)break;}return n;};"
    # Case A: full build streams to completion across frames; onComplete once.
    "const builtA=[];const colA={total:0,appendChild(fr){this.total+=fr.items.length;}};let doneA=0;"
    "f(colA,5,(i)=>{builtA.push(i);return{i};},()=>false,()=>{doneA++;});"
    "const framesA=drain();"
    # Case B: a stale guard mid-build aborts and suppresses onComplete.
    "const builtB=[];const colB={total:0,appendChild(fr){this.total+=fr.items.length;}};let doneB=0;"
    "f(colB,5,(i)=>{builtB.push(i);return{i};},()=>builtB.length>=3,()=>{doneB++;});"
    "drain();"
    "console.log(JSON.stringify({a:{built:builtA,total:colA.total,done:doneA,frames:framesA},"
    "b:{built:builtB,total:colB.total,done:doneB}}));process.exit(0);"
)


def test_browse_chunked_render_completes_and_aborts_node_vm():
    """Run the REAL _renderCellsChunked in a JS engine: it must build every cell,
    yield across frames (not block), fire onComplete exactly once — and abort
    cleanly (no onComplete) when a superseding render flips the stale guard."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH; chunked render covered only structurally")
    result = subprocess.run(
        [node, "-e", _CHUNK_NODE_HARNESS],
        capture_output=True, encoding="utf-8", errors="replace", timeout=30,
    )
    assert result.returncode == 0, (
        f"chunk node harness failed (rc={result.returncode}):\n"
        f"STDOUT:{result.stdout}\nSTDERR:{result.stderr}"
    )
    data = json.loads(result.stdout)
    a = data["a"]
    assert a["built"] == [0, 1, 2, 3, 4], a["built"]   # every cell built, in order
    assert a["total"] == 5                              # all appended to the column
    assert a["done"] == 1                               # onComplete fired exactly once
    assert a["frames"] >= 2, "render must yield across rAF frames, not block in one pass"
    b = data["b"]
    assert b["built"] == [0, 1, 2], b["built"]          # stale guard stopped the build
    assert b["done"] == 0                               # and suppressed onComplete


# ── U15: onboarding & scan empty states (shared module + admin badge) ─────────

def test_browse_empty_state_picks_source_aware_message():
    """The shared browse module must distinguish the three R19/R20 empty states
    (zero-source, scan-in-progress, scanned-empty) off /api/scan-status, not a
    single generic 'nothing here'. Lives in the shared module (discipline)."""
    js = (ROOT / "static/browse/index.js").read_text(encoding="utf-8")
    assert "_renderBrowseEmptyState" in js, "the shared empty-state helper must exist"
    assert "/api/scan-status" in js, "empty state must consult the scan-status surface"
    # The three distinct states are present.
    assert "No music sources connected" in js
    assert "being prepared" in js
    assert "No music found." in js
    # Wired into BOTH top-level browse lists (artists + albums).
    assert js.count("_renderBrowseEmptyState(el,") >= 2


def test_admin_scan_status_badge_wired():
    """The admin Sources panel renders a scan-status badge (scanning /
    scanned-empty) from /admin/scan-status — in static/admin/app.js (admin
    chrome) with its target element in the dashboard template."""
    js = (ROOT / "static/admin/app.js").read_text(encoding="utf-8")
    assert "renderSourceScanStatus" in js
    assert "/admin/scan-status" in js
    assert "renderSourceScanStatus()" in js  # called from loadSources
    html = ADMIN_TEMPLATE.read_text(encoding="utf-8")
    assert 'id="sources-scan-status"' in html


def test_skip_notification_lives_in_shared_playback_module():
    """R22/U16: the skip toast is a single-source shared-playback feature — both
    pages dispatch the track_skipped WS event to the shared module's showSkipped,
    never a per-page renderer (shared-module discipline)."""
    js = (ROOT / "static/playback/index.js").read_text(encoding="utf-8")
    assert "function showSkipped" in js
    assert "skip-note" in js                       # the overlay element/class
    assert "showSkipped," in js                    # exported on the handle
    # Both per-page WS handlers route track_skipped to the shared module.
    for page in ("static/guest/app.js", "static/admin/app.js"):
        src = (ROOT / page).read_text(encoding="utf-8")
        assert "track_skipped" in src and "playbackHandle.showSkipped" in src, page
    # The toast has CSS.
    css = (ROOT / "static/playback/queue.css").read_text(encoding="utf-8")
    assert ".skip-note-overlay" in css


# ── Play-data curation UI (2026-07-03 plan U5/U6) ────────────────────────────

BROWSE_JS = ROOT / "static/browse/index.js"
PLAYBACK_JS = ROOT / "static/playback/index.js"


def test_most_played_admin_remove_item_is_source_pinned():
    """U5 (R4/R7/R8): an admin-only 'Remove from Most Played' kebab item, scoped
    to the Most Played context, with an inline two-step confirm (never native
    confirm) and a non-optimistic POST — all in the shared browse module."""
    js = BROWSE_JS.read_text(encoding="utf-8")
    assert "Remove from Most Played" in js
    assert "ctx.mostPlayed" in js                 # scoped to the Most Played list only
    assert "authMode === 'admin'" in js           # admin-gated on the client
    assert "mostPlayed: true" in js               # loadMostPlayed threads the ctx
    assert "/admin/most-played/remove" in js      # POSTs the admin endpoint
    assert "Confirm: remove" in js                # inline two-step confirm
    assert "window.confirm(" not in js            # never native confirm (blocks the loop)


def test_history_admin_remove_play_is_source_pinned():
    """Play-data curation relocation (2026-07-03 plan, R1/R5/R7): the admin
    "remove this play" affordance moved OFF the shared history strip into the
    Setup -> Recent Plays panel. The strip is now read-only for everyone; the
    panel (admin Setup chrome) owns the POST /admin/history/remove-play call and
    reads each play's added_at, and the strip-scoped chip CSS is gone."""
    admin = (ROOT / "static/admin/app.js").read_text(encoding="utf-8")
    css = (ROOT / "static/playback/queue.css").read_text(encoding="utf-8")
    dashboard = (ROOT / "app/templates/admin/dashboard.html").read_text(encoding="utf-8")
    # Strip is read-only: the admin mountPlayback history config carries only `el`,
    # with no removePlan hook (the guest strip never passed one either).
    assert "history: { el: '#history-strip' }" in admin
    # The Recent Plays panel is admin-only Setup chrome that owns the removal.
    assert 'id="recent-plays"' in dashboard
    assert "renderRecentPlays" in admin
    assert "/admin/history/remove-play" in admin
    assert "added_at" in admin
    # The strip-scoped chip CSS was removed (base .qi-remove stays for queue-remove).
    assert ".qs-history .qi-remove" not in css


# ── Broad search tier: observer root, abort, stale-chain bound (2026-07-17) ────
# ce-debug of the guest/admin search bifurcation. Three load-bearing behaviors
# in static/browse/index.js's Tier-2 machinery, each pinned against the exact
# regression that caused (or hid) the "guest search is much slower" report:
#   1. The sentinel observer roots on _scrollOwner(el) — the element that
#      ACTUALLY scrolls #search-results (guest: ancestor #content; admin:
#      #search-results' own overflow box, which _findScrollAncestor skips).
#      With the wrong root the tier silently never fired on admin (fixed
#      2026-07-02 in 20b7520, lost in the 546cd36 revert, re-applied here).
#   2. _teardownBroadTier aborts the in-flight page and the fetch carries the
#      signal, so per-keystroke re-queries stop consuming server capacity.
#   3. The forced re-observe self-chain is bounded by _BROAD_STALE_CHAIN so
#      all-duplicates pages (catalog installs: every broad row dedups against
#      Tier 1) and failing sources cannot spin an unbounded serial cascade.


def _broad_tier_source():
    browse = BROWSE_JS.read_text(encoding="utf-8")
    st = browse.index("function _teardownBroadTier")
    en = browse.index("// ── Tab activation hooks", st)
    return browse[st:en]


def test_broad_observer_uses_scroll_owner_root():
    src = _broad_tier_source()
    assert "_scrollOwner(el)" in src, (
        "_setupBroadTier's sentinel observer must root on _scrollOwner(el) so the "
        "broad tier auto-loads on admin, where #search-results is its own scroller "
        "(_findScrollAncestor starts at parentElement and skips it)."
    )
    assert "root: _findScrollAncestor(el)" not in src, (
        "Rooting on _findScrollAncestor(el) leaves admin's sentinel clipped inside "
        "its own scroll box — the tier never fires there (the 546cd36 regression)."
    )


def test_broad_teardown_aborts_inflight_page():
    src = _broad_tier_source()
    teardown = src[:src.index("function _setupBroadTier")]
    assert "_broad.abort" in teardown and ".abort()" in teardown, (
        "_teardownBroadTier must abort the in-flight broad fetch — a stale "
        "keystroke's page otherwise keeps burning per-source semaphore slots."
    )
    assert "signal: b.abort.signal" in src, (
        "The broad-page fetch must carry the tier's AbortController signal."
    )
    assert "AbortError" in src, (
        "The page loader must swallow its own teardown abort (not count it as a "
        "stale/failed page)."
    )


def test_broad_stale_chain_is_bounded():
    src = _broad_tier_source()
    assert "_BROAD_STALE_CHAIN" in src, (
        "The forced re-observe self-chain must be bounded: all-duplicates pages "
        "chained up to BROAD_MAX_PAGES serial live-source calls per query on "
        "catalog installs (2026-07-17 ce-debug)."
    )
    assert "b.stale < _BROAD_STALE_CHAIN" in src, (
        "The finally-block re-observe must gate on the stale counter."
    )
    # Failures count toward the chain so a dead source can't retry-loop forever.
    assert "if (_broad === b) b.stale++;" in src
