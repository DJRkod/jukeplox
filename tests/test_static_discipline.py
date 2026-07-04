"""Discipline check for the shared UI module structure (browse + playback).

Enforces the structural rules from
`docs/plans/2026-06-05-004-refactor-guest-admin-browse-unification-plan.md`
and `docs/plans/2026-06-09-001-feat-guest-admin-layout-transport-plan.md`:
browse/search/track-row/source-picker/album-tracks/artist-albums/queue-
append rendering lives in `static/browse/index.js` only, and Now Playing/
progress/queue-list/history-strip/micro-bar rendering lives in
`static/playback/index.js` only. The per-page files (`static/guest/app.js`,
`static/admin/app.js`) MUST NOT contain forked copies of those symbols,
and MUST NOT grow new top-level declarations outside their allowlists.

The check is intentionally narrow but covers the failure modes a future
LLM coding pass would likely hit when interpreting "unify" as "duplicate-
but-match":

1. Forbidden symbols (verbatim or renamed) in per-page files → fail.
2. New top-level declarations in per-page files outside the allowlist →
   fail (catches rename-based fork attempts where a future LLM gives the
   helper a different name to sidestep check 1).
3. Shared module missing → fail (the structural premise broken).
4. New `<script src="/static/...">` tags in either template outside the
   tiny allowlist → fail (catches "agent landed browse helpers in a new
   file" → adds a new <script> tag → sidesteps checks 1-2).

What the regex DOES catch: `function name()`, `async function name()`,
`const name =`, `let name =`, `var name =`, `class name {`. Verified
via the rename-attack injection tests below.

What it does NOT catch: deeply nested IIFEs, prototype assignments
(`Obj.prototype.foo = ...`). Documented residual; tighten if a real
fork attempt uses those patterns.

CLAUDE.md / AGENTS.md point at this file as the authoritative rule —
prose explains why; the test enforces what.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


# ── Allowlists ────────────────────────────────────────────────────────────────
# Per-page top-level declarations that are allowed to live in the per-page JS
# files (page chrome only; KTD9 of the unification plan). Anything not on the
# allowlist fails the check — including a future renamed copy of a forbidden
# symbol.

GUEST_ALLOWED = {
    # WebSocket lifecycle
    "ws", "wsBackoff", "connectWS",
    # Toast
    "toastEl", "toastTimer", "showToast",
    # API helper (page-chrome variant)
    "api",
    # Lock state
    "isLocked", "lockBanner", "setLocked",
    # Shared module mount handles
    "browseHandle", "playbackHandle",
    # Appearance engine mount handle (2026-06-11 glow-up U5 — wiring only;
    # the gear/panel/scheme logic itself lives in static/shared.js)
    "appearanceHandle",
    # Queue snapshot resync (initial load / WS reconnect / tab refocus)
    "refreshQueueState",
    # Tab navigation (page chrome; Now tab is not a browse view)
    "switchTab", "BROWSE_VIEWS",
    # Desktop two-pane seam (2026-06-09 rail plan U6)
    "desktopMq", "handleDesktopChange",
    # Own-track receipt store (remove-own-queued-tracks U4): browser-local
    # ownership for the in-queue remove (✕); onQueued is inline config.
    "queueReceipts",
    # Queue remove plan (unify-queue-remove): builds {singles, albums} for the
    # shared playback remove renderer (receipt-scoped; album-as-unit).
    "guestRemovePlan",
}

ADMIN_ALLOWED = {
    # WebSocket
    "ws", "wsBackoff", "connectWS",
    # Toast
    "toastEl", "toastTimer", "showToast",
    # API helper (admin variant: throws on error)
    "api",
    # Playback controls (chrome only — rendering lives in static/playback/)
    "btnPause",
    # Volume
    "volSlider", "volLabel", "volTimer", "_volUserLastSet",
    "loadVolume", "applyVolumeFromEvent",
    # Queue management (admin chrome — row actions over shared-module rows)
    "queuePlayNext", "queueRemove",
    # Queue remove plan (unify-queue-remove): builds {singles, albums} for the
    # shared playback remove renderer (all rows; albums grouped by album_id).
    "adminRemovePlan",
    # Drag-and-drop
    "dragSrc",
    "onDragStart", "onDragOver", "onDragLeave", "onDragEnd", "onDrop",
    # Lock toggle
    "lockToggle", "syncLock",
    # Device selector — cross-protocol output picker
    # (docs/plans/2026-06-08-002-feat-cross-protocol-output-picker-plan.md U6).
    "allDevices", "currentActive", "devicesByHost", "_devicesLoading",
    "_initialLoadDone", "PROTOCOL_LABELS", "VIA_DEFAULT_ORDER", "STUCK_PROBE_S",
    "loadDevices", "_updateBackendBanner",
    "updateDeviceSelect", "_appendDeviceOption",
    "updateConnectionSelect", "_setApplyDisabled", "updateConnectionHint",
    # AirPlay protocol indicator / fallback (U6 of feat-airplay-2-default-with-fallback)
    "_airplayProtocols", "btnNoAudio", "btnRetestAp2", "protocolLabel",
    "loadAirplayProtocols", "_syncAirPlayProtocolUI",
    # Live device discovery (2026-06-11 plan U6): shared GET/WS render path
    # + offline warning banner — admin-only output chrome.
    "applyDevicesPayload", "_updateDeviceOfflineBanner",
    # Chrome helpers (KTD9)
    "esc", "artImg", "formatDuration",
    # Page-level Jukebox/Setup tabs (hash-based; legacy anchors map to tabs)
    "LEGACY_ANCHOR_TABS", "applyPageTab", "_pageTabFromHash",
    # Shared playback module mount + transport chrome
    "playbackHandle", "btnPrev", "isPlaying", "_historyEmpty", "_syncPrevEnabled",
    "decorateQueueRow", "refreshQueueState",
    # Libraries / Plex / Settings (admin chrome)
    # Pattern Matching / Artist Exclusion editors (2026-06-10 pattern-rules
    # plan U4) — admin-only Setup chrome per the project standard.
    "patternRules", "exclusionNames", "editorsDirty",
    "renderPatternRules", "renderExclusions", "syncInertHint", "loadRuleEditors",
    "libraryList", "renderLibraryList", "loadLibraries", "adminRescan",
    "btnConnectPlex", "plexConnectSpinner", "plexConnectError",
    "plexPollInterval", "plexPollTimeout",
    # Multi-source Sources panel (plan U14): connect/remove/rescan/priority —
    # admin-only Setup chrome (no shared-module overlap; distinct from queue drag).
    "sourcesList", "jfConnectError", "SOURCE_TYPE_LABELS", "_currentSources",
    "renderSourcesList", "loadSources", "connectJellyfin", "connectLocal", "removeSource",
    "rescanSources", "moveSourcePriority",
    # Surprise Me capability-degradation note — toggled by source mix (plan U13).
    "syncSurpriseSourceNote",
    # Admin Sources scan-status badge (plan U15): scanning / scanned-empty.
    "renderSourceScanStatus",
    "loadSettings",
    # Surprise "Recent suggestions" readout — shared render path for the GET fetch
    # + the live surprise_recorded WS event (admin-only Setup chrome).
    "renderSurpriseRecent",
    # Recent Plays curation panel (2026-07-03 plan): admin-only Setup chrome to prune
    # recent plays. Fed by refreshQueueState + queue_changed history; renders its own
    # compact rows (a distinct management surface, NOT a shared-module fork — like the
    # Sources / Surprise-Recent panels), pages client-side over the ~50 live buffer,
    # and reuses POST /admin/history/remove-play.
    "_recentPlaysData", "_recentPlaysPage", "_recentPlaysGen", "_recentPlaysExpanded",
    "_playedAgo", "setRecentPlaysData", "renderRecentPlays", "toggleRecentPlays",
    "removeRecentPlay",
    # Default-scheme picker (glow-up U6 — Setup chrome over the shared
    # APPEARANCE_SCHEMES table; admin-only by design)
    "defaultScheme", "renderDefaultSchemePicker",
    # Shared module mount handle
    "browseHandle",
    # Appearance engine mount handle (2026-06-11 glow-up U5 — wiring only)
    "appearanceHandle",
}

# Forbidden symbols: forked copies of the shared module's responsibilities.
# Per R3 / R10 of the unification plan.
FORBIDDEN_SYMBOLS = {
    # Playback rendering (2026-06-09 layout plan U6): Now Playing, progress,
    # queue-list, and history-strip rendering live ONLY in
    # static/playback/index.js (mountPlayback). These are the per-page fork
    # names that existed before extraction — named entries because they are
    # camelCase and the underscore-prefix wildcard below cannot catch them.
    "mountPlayback",
    "renderNowPlaying", "renderQueue", "renderQueueStrip", "renderHistoryStrip",
    "renderProgress", "gRenderProgress",
    # Closing Time banner (2026-06-24 plan U5): the overlay render lives ONLY in
    # static/playback/index.js. Per-page files dispatch the closing_time event to
    # playbackHandle.applyClosingTime (a method call, not a declaration — not
    # extracted), so forbidding these names only fires on a per-page fork.
    "applyClosingTime", "_ensureClosingEl",
    "gStartTick", "gStopTick", "gStartSync", "gStopSync",
    "startProgressTick", "stopProgressTick", "startProgressSync", "stopProgressSync",
    "gFmtMs", "fmtMs",
    "gPosMs", "gDurMs", "gTickTimer", "gSyncTimer",
    "posMs", "durMs", "progressTickTimer", "progressSyncTimer", "isSeeking",
    "guestProgress", "guestPos", "guestDur",
    "progressBar", "progressPos", "progressDur",
    "npPlaceholder", "npArt", "npInfo", "npIdle", "npTitle", "npArtist", "npAlbum",
    "queueStrip", "historyStrip", "historyInner", "queueList", "currentQueue",
    "deduplicateTracks",
    "makeTrackRow", "makeTrackRowMulti",
    "showSourcePicker", "renderTracksDeduped",
    "addAlbum", "addTrack",
    "showAlbumTracks", "showArtistAlbums", "showYearAlbums",
    "groupAlbumsBySubtype", "hasMultipleSubtypes",
    "sortKey", "applySort",
    # Alphabet rail (U5-U8): exhaustive list of every top-level identifier the
    # rail introduces in static/browse/index.js. The U3 placeholder for
    # _railDragging was here; U9 absorbs it into this section + adds the rest.
    # The wildcard pattern FORBIDDEN_SYMBOL_PATTERNS below also catches any
    # _rail* / _alpha* / computeLetter* / cancelRail* helper added later
    # without a denylist update.
    "computeLetterIndex", "cancelRailDrag",
    "_railDragging", "_railBounds", "_letterHeight",
    "_alphaOverlay", "_alphaObserver", "_dragSupported",
    "_ensureOverlay", "_findScrollAncestor",
    "_attachAlphaObserver", "_wireRailInteractions", "_railPointerMove",
    # Plan 003 U3 additions: scroll-event + rAF highlight identifiers replace
    # the old IntersectionObserver approach. The _alpha* wildcard pattern
    # already catches future helpers; named denylist entries here surface
    # clearer error messages when a future LLM tries to fork one.
    "_alphaScrollHandler", "_alphaScrollPending",
    "_alphaScrollAncestor", "_alphaSortedOffsets", "_alphaActiveRail",
    "_ALPHA_HIGHLIGHT_THRESHOLD",
    # Plan 003 U4 additions: density mode rendering. _densityRefresh walks
    # rail children and applies log-scale bar widths from the letter counts
    # map. The _density* wildcard pattern (added to FORBIDDEN_SYMBOL_PATTERNS
    # below) catches future helpers without enumeration churn.
    "_densityRefresh",
    # Plan 003 U5 additions: magnetic mode rendering. _magneticUpdate
    # applies Gaussian-falloff scaling; _magneticReset clears inline styles
    # when the cursor leaves the rail. The _magnetic* wildcard pattern
    # catches future helpers. Constants are module-level consts so they
    # land in the wildcard sweep too.
    "_magneticUpdate", "_magneticReset",
    "_MAGNETIC_SIGMA", "_MAGNETIC_MAX_SCALE",
    # 2026-06-10 gutter fix: publishes --rail-inset/--rail-lane on the host.
    "_railLaneRefresh",
    # 2026-06-11 glow-up U4: waveform-row bar sizing (density's analogue).
    "_waveformRefresh",
    # 2026-06-10 genre-search plan U2: search-bound genre→album drill-in.
    "_searchStyleView", "_searchStyleReturnFilter",
    "_ensureSearchStyleView", "_showSearchStyleAlbums", "_styleAlbumRow",
    # 2026-06-10 pattern-rules plan U5: JS normalization twin.
    "_patternRulesCompiled", "_compilePatternRules", "_normalizeName",
    "_loadPatternRules",
    # 2026-06-10 most-played plan U2: leaderboard tab loader.
    "_mostPlayedLoaded", "loadMostPlayed",
    # 2026-06-11 appearance glow-up U2/U5: appearance engine internals live
    # ONLY in static/shared.js. Pages mount via mountAppearance and keep just
    # the handle; they never define or re-declare these.
    "APPEARANCE_SCHEMES", "applyScheme",
    "APPEARANCE_RAIL_MODES", "mountAppearance",
    # Plan 002 U4 additions: singleton mount + lifecycle + column-visibility
    # observer identifiers. _buildOrRefreshRail was removed in U2 (singleton
    # replaces per-column build); its denylist entry is gone too — the
    # function no longer exists in the shared module.
    "_railSingleton", "_activeColumn", "_activeLetterIndex",
    "_columnVisibilityObserver", "_ensureRailSingleton",
    "_activateRail", "_deactivateRail", "_watchColumnVisibility",
    "SUBTYPE_ORDER", "SUBTYPE_LABELS",
    "loadArtists", "loadAlbums", "loadYears", "loadGenres",
    "renderArtistsList", "renderArtistsItems",
    "renderAlbumsList", "renderAlbumsItems",
    "renderYearsList", "renderSearchResults",
    "showOverflowMenu", "hideOverflowMenu",
    "mountBrowser",
    # Past-tense admin-prefixed forms — explicit denylist so a future LLM
    # can't reintroduce them under the old names either.
    "adminAddTrack", "adminShowSourcePicker", "adminTrackRowMulti",
    "adminRenderTracksDeduped", "adminAlbumRow", "adminArtistRow",
    "adminShowAlbumTracks", "adminShowArtistAlbums",
    "adminDoSearch", "adminRenderResults",
    "adminLoadArtists", "adminLoadAlbums", "adminLoadYears", "adminLoadGenres",
    "adminRenderArtistsBrowse", "adminRenderArtistsItems",
    "adminRenderAlbumsBrowse", "adminRenderAlbumsItems",
    "adminRenderYearsBrowse", "adminShowYearAlbums",
}

# Allowlist of <script src> values in the per-page templates. New entries
# require explicit decision (catches "agent landed browse helpers in a new
# file" — the path of least resistance for sidestepping the symbol allowlist).
SCRIPT_SRC_ALLOWED = {
    "/static/shared.js",
    "/static/browse/index.js",
    "/static/playback/index.js",
    "/static/guest/app.js",
    "/static/admin/app.js",
}

# Allowlist of local <link rel="stylesheet"> href values in per-page templates.
# Parallel to SCRIPT_SRC_ALLOWED — closes the obvious CSS-side bypass where a
# future LLM might land rail styles in a new per-page stylesheet (e.g.,
# /static/admin/alpha-rail.css). External URLs (Google Fonts) are scoped out
# via startswith("/static/") in the check function.
STYLESHEET_LINK_ALLOWED = {
    "/static/browse/rail.css",
    # Shared queue remove (✕ chip + album bar) styling, linked by both the guest
    # and admin templates (unify-queue-remove U2) — one home for the affordance.
    "/static/playback/queue.css",
}

# Inline-style scan: rail-specific CSS selectors must NOT appear in templates'
# inline <style> blocks (which would re-fork the residence even though the
# new shared rail.css exists). Targeted rail-class scan only — full inline-CSS
# residence is documented as out of scope.
RAIL_CSS_CLASS_SELECTORS = (
    ".alpha-rail",
    ".list-with-rail",
    ".alpha-items-column",
    ".alpha-overlay",
    # Plan 003 U4/U5: mode-density / mode-magnetic rail variants.
    ".mode-density",
    ".density-row",
    ".mode-magnetic",
    ".magnetic-letter",
)


# ── Declaration extraction ────────────────────────────────────────────────────
# Match every top-level declaration form present in the codebase, plus the
# rename-attack shapes the discipline-layer must catch. See the module docstring
# for what is and isn't covered.

_DECL_PATTERNS = [
    # function name() / async function name()
    re.compile(r"^(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\(", re.MULTILINE),
    # const/let/var name = (assignment-style declarations)
    re.compile(r"^(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=", re.MULTILINE),
    # class name {
    re.compile(r"^class\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*[{(]", re.MULTILINE),
    # window.X = ... (un-anchored — catches both `window.X =` at line start AND
    # IIFE-wrapped `(function(){ window.X = ... })()` forms; the per-page
    # denylist scans only static/guest/app.js and static/admin/app.js, so the
    # shared module's legitimate `window.mountBrowser = mountBrowser` at
    # static/browse/index.js is exempt because the shared module isn't scanned.)
    re.compile(r"\bwindow\.([A-Za-z_$][A-Za-z0-9_$]*)\s*="),
    # export const/let/var name = ... (ESM-style exports)
    re.compile(r"^\s*export\s+(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=", re.MULTILINE),
]


# Object.assign(window, {key1: ..., key2: ...}) — secondary helper to extract
# top-level keys from the FIRST object argument's body. Multi-arg forms
# (Object.assign(window, {a:1}, {b:2})) catch the first object's keys; the
# second-object shape is documented as a residual in _RESIDUAL_ADJACENT_BYPASS_SHAPES.
_OBJECT_ASSIGN_WINDOW_PATTERN = re.compile(
    r"\bObject\.assign\s*\(\s*window\s*,\s*\{([^}]+)\}",
)
_OBJECT_LITERAL_KEY_PATTERN = re.compile(
    r"([A-Za-z_$][A-Za-z0-9_$]*)\s*:",
)


def _extract_top_level_names(source: str) -> set[str]:
    """Return the set of top-level declarations matching any of the supported forms.

    Top-level means at the start of a line (column 0) for anchored patterns, or
    anywhere on a line for window-assign/IIFE shapes (which can sit inside
    one-line IIFE wrappers). Indented declarations inside functions / blocks
    are still extracted for the un-anchored patterns — the per-page denylist
    catches them when they leak into per-page files.
    """
    names: set[str] = set()
    for pattern in _DECL_PATTERNS:
        for m in pattern.finditer(source):
            names.add(m.group(1))
    # Object.assign(window, {...}) — extract keys from the first object body.
    for m in _OBJECT_ASSIGN_WINDOW_PATTERN.finditer(source):
        body = m.group(1)
        for k in _OBJECT_LITERAL_KEY_PATTERN.finditer(body):
            names.add(k.group(1))
    return names


# ── Check 1: Forbidden symbols in per-page files ──────────────────────────────


def _check_no_forbidden_symbols(path: Path, page_label: str) -> None:
    source = path.read_text(encoding="utf-8")
    names = _extract_top_level_names(source)
    leaked = names & FORBIDDEN_SYMBOLS
    assert not leaked, (
        f"Forbidden browse-module symbols appeared in {page_label} ({path}): "
        f"{sorted(leaked)}. These must live in static/browse/index.js, not the "
        f"per-page file. Move the code into the shared module instead of "
        f"defining it here."
    )


def test_no_forbidden_symbols_in_guest_app_js():
    _check_no_forbidden_symbols(ROOT / "static/guest/app.js", "guest")


def test_no_forbidden_symbols_in_admin_app_js():
    _check_no_forbidden_symbols(ROOT / "static/admin/app.js", "admin")


# Wildcard residence rule (per U9 of the rail plan): any identifier matching
# this prefix regex in per-page files is also forbidden, even if it's not on
# the named FORBIDDEN_SYMBOLS list. Catches future helpers in the rail's
# vocabulary that the named list forgot to enumerate (e.g., `_railScrollRoot`,
# `_computeLetterFromY`, `cancelRailDragHard`). Per KTD 8, future alphabet-
# adjacent features (keyboard shortcuts, deep links) extend this pattern.
FORBIDDEN_SYMBOL_PATTERNS = [
    # Plan 003 U4/U5: extended with _density / _magnetic to protect future
    # density-mode and magnetic-mode helpers added in static/browse/index.js.
    re.compile(r"^(_rail|_alpha|_density|_magnetic|computeLetter|cancelRail)"),
]


def _check_no_forbidden_symbol_patterns(path: Path, page_label: str) -> None:
    source = path.read_text(encoding="utf-8")
    names = _extract_top_level_names(source)
    leaked = {n for n in names for pat in FORBIDDEN_SYMBOL_PATTERNS if pat.match(n)}
    assert not leaked, (
        f"Forbidden rail-vocabulary symbol(s) appeared in {page_label} "
        f"({path}): {sorted(leaked)}. Identifiers starting with _rail, "
        f"_alpha, _density, _magnetic, computeLetter, or cancelRail are "
        f"reserved for the shared browse module (static/browse/index.js). "
        f"Move the code into the shared module instead of defining it here."
    )


def test_no_forbidden_symbol_patterns_in_guest_app_js():
    _check_no_forbidden_symbol_patterns(ROOT / "static/guest/app.js", "guest")


def test_no_forbidden_symbol_patterns_in_admin_app_js():
    _check_no_forbidden_symbol_patterns(ROOT / "static/admin/app.js", "admin")


# ── Check 2: Per-page allowlist (rejects rename-based fork attempts) ──────────


def _check_allowlist(path: Path, allowed: set[str], page_label: str) -> None:
    source = path.read_text(encoding="utf-8")
    names = _extract_top_level_names(source)
    unexpected = names - allowed
    assert not unexpected, (
        f"Unexpected top-level declaration(s) in {page_label} ({path}): "
        f"{sorted(unexpected)}. The per-page file's allowlist (defined in "
        f"tests/test_static_discipline.py:GUEST_ALLOWED / ADMIN_ALLOWED) only "
        f"permits page-chrome symbols. If this is a legitimate new chrome "
        f"helper, add it to the allowlist with rationale. If it's browse / "
        f"search / queue logic, move it to static/browse/index.js."
    )


def test_guest_app_js_allowlist():
    _check_allowlist(ROOT / "static/guest/app.js", GUEST_ALLOWED, "guest")


def test_admin_app_js_allowlist():
    _check_allowlist(ROOT / "static/admin/app.js", ADMIN_ALLOWED, "admin")


# ── Check 3: Shared module present ────────────────────────────────────────────


def test_shared_browse_module_exists():
    path = ROOT / "static/browse/index.js"
    assert path.exists(), (
        "static/browse/index.js is missing. The unification plan moved all "
        "browse rendering into this module; without it the discipline check "
        "is meaningless."
    )
    source = path.read_text(encoding="utf-8")
    assert "function mountBrowser" in source, (
        "static/browse/index.js exists but does not define mountBrowser. "
        "The per-page files call mountBrowser(); the shared module must "
        "provide it."
    )


def test_shared_playback_module_exists():
    path = ROOT / "static/playback/index.js"
    assert path.exists(), (
        "static/playback/index.js is missing. The 2026-06-09 layout plan "
        "moved all Now Playing / progress / queue-list / history-strip / "
        "micro-bar rendering into this module; without it the playback "
        "discipline check is meaningless."
    )
    source = path.read_text(encoding="utf-8")
    assert "function mountPlayback" in source, (
        "static/playback/index.js exists but does not define mountPlayback. "
        "The per-page files call mountPlayback(); the shared module must "
        "provide it."
    )


def test_closing_banner_is_single_source():
    """Closing Time banner render lives ONLY in the shared playback module; the
    per-page files dispatch the event to it but must not render the banner."""
    shared = (ROOT / "static/playback/index.js").read_text(encoding="utf-8")
    assert "function applyClosingTime" in shared and "closing-time-overlay" in shared, (
        "static/playback/index.js must define applyClosingTime AND the banner DOM "
        "(.closing-time-overlay) — it's the single home for the shared banner."
    )
    for page in ("static/guest/app.js", "static/admin/app.js"):
        src = (ROOT / page).read_text(encoding="utf-8")
        assert "playbackHandle.applyClosingTime" in src, (
            f"{page} must dispatch the closing_time event to the shared handle."
        )
        assert "closing-time-overlay" not in src, (
            f"{page} must not render the banner itself — single-source in the "
            f"playback module (the discipline test forbids applyClosingTime there)."
        )


# ── Check 4: <script src> tag allowlist in HTML templates ─────────────────────


_SCRIPT_SRC_PATTERN = re.compile(
    # Match `<script ... src="…">` regardless of which other attributes (type,
    # async, defer, integrity, etc.) sit before the `src`. Catches the
    # rename-attack shape `<script type="module" src="…">` where a future LLM
    # could land a new helper bundle by giving it a non-trivial attribute set.
    r'<script\b[^>]*?\bsrc="([^"?]+)(?:\?[^"]*)?"',
    re.IGNORECASE,
)


def _check_template_scripts(path: Path, template_label: str) -> None:
    source = path.read_text(encoding="utf-8")
    refs = {m.group(1) for m in _SCRIPT_SRC_PATTERN.finditer(source)}
    static_refs = {r for r in refs if r.startswith("/static/")}
    unexpected = static_refs - SCRIPT_SRC_ALLOWED
    assert not unexpected, (
        f"Unexpected <script src> reference(s) in {template_label} ({path}): "
        f"{sorted(unexpected)}. The allowlist (SCRIPT_SRC_ALLOWED in "
        f"tests/test_static_discipline.py) only permits the five known "
        f"frontend bundles. A new <script> tag is a red flag — it might be a "
        f"future LLM landing browse or playback helpers in a new file to sidestep the "
        f"per-page symbol allowlist. If it's legitimate (a new app surface), "
        f"add it to SCRIPT_SRC_ALLOWED with rationale."
    )


def test_guest_template_script_allowlist():
    _check_template_scripts(ROOT / "app/templates/guest/index.html", "guest template")


def test_admin_template_script_allowlist():
    _check_template_scripts(ROOT / "app/templates/admin/dashboard.html", "admin template")


def test_admin_template_has_international_rail_controls():
    """Plan 004 U3: the International rail toggle + per-rail thresholds (with ⓘ
    hovers and the gray-out hint) live in the admin Setup form, and admin/app.js
    wires them through load + save + the alpha-mode gray-out handler — all in the
    existing per-page file (the script-allowlist test above guards against a new
    JS module)."""
    html = (ROOT / "app/templates/admin/dashboard.html").read_text(encoding="utf-8")
    for needle in (
        'name="rail-alpha-mode"', 'value="international"',
        'id="international-thresholds-row"',
        'id="rail-artist-threshold"', 'id="rail-album-threshold"',
        'international-thresholds-hint',
    ):
        assert needle in html, f"admin Setup form missing {needle!r}"
    js = (ROOT / "static/admin/app.js").read_text(encoding="utf-8")
    for needle in (
        "rail-alpha-mode",            # load + save + gray-out wiring
        "rail_alpha_mode",            # POST body key
        "rail_artist_threshold", "rail_album_threshold",
        "international-thresholds-row",  # gray-out toggle target
    ):
        assert needle in js, f"admin/app.js missing International rail wiring {needle!r}"


# ── Check 4b: static refs use the build-derived cache-buster, not a literal ──
# Cache-busting is automatic: app/assets.py derives a token (git SHA in an image
# build, else a content hash of the static files) and the templates write it as
# `?v={{ asset_v }}`. This guard fails if any /static/ ref reverts to a manual
# `?v=<number>` literal (or any other non-canonical buster) — the exact
# regression that shipped the Flood Control admin-toggle no-op: a changed app.js
# whose manual ?v= was never bumped, so warm-cache browsers ran the old file.
# A single global token also makes cross-template divergence impossible, so the
# prior per-template lockstep check is subsumed. This guards template *shape*,
# not byte freshness — byte freshness is now guaranteed by the build token.

_VERSIONED_STATIC_REF = re.compile(r'(?:src|href)="(/static/[^"?]+)\?v=([^"]*)"')
_CANONICAL_BUSTER = "{{ asset_v }}"
_CACHE_BUSTER_TEMPLATES = {
    "guest template": ROOT / "app/templates/guest/index.html",
    "admin template": ROOT / "app/templates/admin/dashboard.html",
}


def test_static_refs_use_build_derived_cache_buster():
    offenders = {}
    for label, path in _CACHE_BUSTER_TEMPLATES.items():
        source = path.read_text(encoding="utf-8")
        for asset, buster in _VERSIONED_STATIC_REF.findall(source):
            if buster != _CANONICAL_BUSTER:
                offenders[f"{label}: {asset}"] = f"?v={buster}"
    assert not offenders, (
        "Static asset ref(s) not using the build-derived cache-buster "
        "(expected exactly `?v={{ asset_v }}`): "
        f"{offenders}. A manual `?v=<number>` literal goes stale — a changed "
        "file whose buster wasn't bumped serves cached JS/CSS to returning "
        "browsers (the Flood Control no-op). Use `?v={{ asset_v }}` so the "
        "buster is derived from the build (see app/assets.py)."
    )


# ── Check 5: <link rel="stylesheet"> allowlist (CSS residence) ────────────────
# Mirrors check 4 for stylesheets. Catches "agent landed rail styles in a new
# per-page CSS file" — the path of least resistance for sidestepping the
# shared rail.css residence rule.

# Match <link ... rel="stylesheet" ... href="..."> AND <link ... href="..." ... rel="stylesheet">
# regardless of attribute order. Extracts the href value.
_STYLESHEET_LINK_REL_FIRST = re.compile(
    r'<link\b[^>]*?\brel="stylesheet"[^>]*?\bhref="([^"?]+)(?:\?[^"]*)?"',
    re.IGNORECASE,
)
_STYLESHEET_LINK_HREF_FIRST = re.compile(
    r'<link\b[^>]*?\bhref="([^"?]+)(?:\?[^"]*)?"[^>]*?\brel="stylesheet"',
    re.IGNORECASE,
)


def _extract_stylesheet_hrefs(source: str) -> set[str]:
    refs = set()
    for pattern in (_STYLESHEET_LINK_REL_FIRST, _STYLESHEET_LINK_HREF_FIRST):
        for m in pattern.finditer(source):
            refs.add(m.group(1))
    return refs


def _check_template_stylesheets(path: Path, template_label: str) -> None:
    source = path.read_text(encoding="utf-8")
    refs = _extract_stylesheet_hrefs(source)
    static_refs = {r for r in refs if r.startswith("/static/")}
    unexpected = static_refs - STYLESHEET_LINK_ALLOWED
    assert not unexpected, (
        f"Unexpected <link rel='stylesheet'> reference(s) in {template_label} "
        f"({path}): {sorted(unexpected)}. The allowlist (STYLESHEET_LINK_ALLOWED "
        f"in tests/test_static_discipline.py) only permits shared rail-module "
        f"stylesheets. A new local stylesheet tag is a red flag — it might be a "
        f"future LLM landing rail styles in a per-page file to sidestep the "
        f"shared-rail-CSS residence rule. If it's legitimate, add it to "
        f"STYLESHEET_LINK_ALLOWED with rationale."
    )


def test_guest_template_stylesheet_allowlist():
    _check_template_stylesheets(ROOT / "app/templates/guest/index.html", "guest template")


def test_admin_template_stylesheet_allowlist():
    _check_template_stylesheets(ROOT / "app/templates/admin/dashboard.html", "admin template")


# ── Check 6: rail-class inline-style scan ────────────────────────────────────
# Closes the obvious bypass to check 5: a future LLM blocked by the stylesheet
# allowlist could land rail rules in an inline <style> block instead. Targeted
# scan for rail-class selectors only — full inline-style residence enforcement
# is documented as out of scope.

_INLINE_STYLE_BLOCK_PATTERN = re.compile(
    r"<style\b[^>]*>(.*?)</style>",
    re.IGNORECASE | re.DOTALL,
)


def _check_no_rail_classes_in_inline_styles(path: Path, template_label: str) -> None:
    source = path.read_text(encoding="utf-8")
    for m in _INLINE_STYLE_BLOCK_PATTERN.finditer(source):
        block = m.group(1)
        leaked = [s for s in RAIL_CSS_CLASS_SELECTORS if s in block]
        assert not leaked, (
            f"Rail-specific CSS selector(s) found in inline <style> block of "
            f"{template_label} ({path}): {leaked}. Rail styles MUST live in "
            f"static/browse/rail.css per KTD 2 of the rail plan. Move the "
            f"rule(s) into the shared stylesheet instead of duplicating in "
            f"per-page templates."
        )


def test_guest_template_no_rail_inline_styles():
    _check_no_rail_classes_in_inline_styles(
        ROOT / "app/templates/guest/index.html", "guest template"
    )


def test_admin_template_no_rail_inline_styles():
    _check_no_rail_classes_in_inline_styles(
        ROOT / "app/templates/admin/dashboard.html", "admin template"
    )


# ── Rename-attack injection verification ──────────────────────────────────────
# Per the plan's "rename-attack injection" requirement. Confirms the expanded
# regex catches all three declaration shapes a future LLM might use to fork
# a helper under a renamed identifier.

_ATTACK_SHAPES = [
    ("function-form",   "function deduplicateTracks(tracks) { return tracks; }"),
    ("const-arrow",     "const deduplicateTracks = (tracks) => tracks;"),
    ("const-function",  "const deduplicateTracks = function (tracks) { return tracks; };"),
    ("class-form",      "class deduplicateTracks { run(t) { return t; } }"),
    # Rename-to-novel-name attack: the allowlist catches it via check 2
    # because the new name isn't on GUEST_ALLOWED / ADMIN_ALLOWED.
    ("rename-arrow",    "const _localDedup = (tracks) => tracks;"),
    ("rename-function", "function _localDedup(tracks) { return tracks; }"),
]


@pytest.mark.parametrize("shape_name,injection", _ATTACK_SHAPES)
def test_rename_attack_caught_by_extractor(shape_name, injection):
    """Confirms the regex set extracts the declaration name for each attack
    shape. The orchestrator-level check (1 or 2) then fires on the extracted
    name. This test does NOT inject into the real per-page files — it just
    verifies the regex catches the shape."""
    names = _extract_top_level_names(injection)
    expected_name = "deduplicateTracks" if "deduplicateTracks" in injection else "_localDedup"
    assert expected_name in names, (
        f"Shape '{shape_name}' was not caught by _extract_top_level_names. "
        f"Extracted: {sorted(names)}. The discipline check would miss this "
        f"declaration form — a future LLM could use this shape to sidestep "
        f"the forbidden-symbol denylist. Extend _DECL_PATTERNS in "
        f"tests/test_static_discipline.py to catch this shape."
    )


# ── Script-src rename-attack shapes ───────────────────────────────────────────
# Confirms _SCRIPT_SRC_PATTERN catches the attribute-order variants a future
# LLM might use to sneak a new bundle past the allowlist. The tag-shape and
# extracted-src pairs below should always succeed; if the extractor returns
# something else, the discipline test would miss the injection.

_SCRIPT_SRC_ATTACK_SHAPES = [
    (
        "module-then-src",
        '<script type="module" src="/static/sneaky/helpers.js"></script>',
        "/static/sneaky/helpers.js",
    ),
    (
        "defer-then-src",
        '<script defer src="/static/sneaky/late.js"></script>',
        "/static/sneaky/late.js",
    ),
]


@pytest.mark.parametrize("shape_name,tag,expected_src", _SCRIPT_SRC_ATTACK_SHAPES)
def test_script_src_pattern_catches_attribute_order_variants(shape_name, tag, expected_src):
    """A future LLM landing browse helpers in a new file via an attribute-rich
    `<script>` tag must still be caught by `_SCRIPT_SRC_PATTERN`. If this fails,
    the rename-attack succeeds because the allowlist never sees the new URL."""
    matches = _SCRIPT_SRC_PATTERN.findall(tag)
    assert matches == [expected_src], (
        f"Shape '{shape_name}' was not caught: got {matches}, want "
        f"['{expected_src}']. _SCRIPT_SRC_PATTERN needs to match this "
        f"attribute-order variant so the template-script-allowlist check "
        f"flags new bundles regardless of how the tag is written."
    )


def test_rename_attack_caught_by_allowlist():
    """If a future LLM injected `const _localDedup = (tracks) => tracks;`
    into static/admin/app.js, the allowlist check would fail because
    `_localDedup` isn't on ADMIN_ALLOWED. This test simulates the injection
    in a string (not the real file) and confirms the assertion message
    surfaces the renamed name."""
    injected_source = (ROOT / "static/admin/app.js").read_text(encoding="utf-8")
    injected_source += "\n\nconst _localDedup = (tracks) => tracks;\n"
    names = _extract_top_level_names(injected_source)
    unexpected = names - ADMIN_ALLOWED
    assert "_localDedup" in unexpected, (
        "Allowlist check did not flag the renamed helper. Either the regex "
        "missed the shape or ADMIN_ALLOWED contains _localDedup (which would "
        "be a serious bug — chrome helpers shouldn't have underscore-prefixed "
        "names that hint at private intent)."
    )


# ── Bypass-shape verification (now-closed via U1 of rail plan) ────────────────
# Verifies the four originally-documented bypass shapes are now caught:
#   - `window.X = function() {...}`         (prototype/global assign)
#   - `Object.assign(window, {X: ...})`     (bulk global assign)
#   - `export const X = ...`                (ESM export)
#   - `(function(){ window.X = ... })()`    (IIFE + window-assign)

_CLOSED_BYPASS_SHAPES = [
    ("window-assign",
     "window.deduplicateTracks = function(tracks) { return tracks; };"),
    ("object-assign-window",
     "Object.assign(window, { deduplicateTracks: function(t) { return t; } });"),
    ("esm-export-const",
     "export const deduplicateTracks = (tracks) => tracks;"),
    ("iife-window-assign",
     "(function(){ window.deduplicateTracks = function(t) { return t; }; })();"),
]


@pytest.mark.parametrize("shape_name,injection", _CLOSED_BYPASS_SHAPES)
def test_closed_bypass_shapes_are_caught(shape_name, injection):
    """The four shapes documented in the prior xfail-strict block are now
    extracted by _DECL_PATTERNS + _OBJECT_ASSIGN_WINDOW_PATTERN. If any future
    regression weakens the extractor to miss any of these shapes, this test
    fails loudly."""
    names = _extract_top_level_names(injection)
    assert "deduplicateTracks" in names, (
        f"Shape '{shape_name}' was not extracted. Extracted: {sorted(names)}. "
        f"_DECL_PATTERNS or the Object.assign helper has regressed."
    )


# ── Documented adjacent-attack-vector residual ────────────────────────────────
# U1 of the rail plan closed the four original bypass shapes. The next-tier
# bypass surface (adjacent attack space) remains a residual gap — these shapes
# are NOT caught by the current extractor, and a future tightening pass should
# either (a) add ~5 more regex patterns, or (b) adopt an AST-aware extractor
# (esprima or similar). The xfail-strict block records the gap explicitly.

_RESIDUAL_ADJACENT_BYPASS_SHAPES = [
    ("object-defineproperty-window",
     "Object.defineProperty(window, 'deduplicateTracks', { value: function(t){return t;} });"),
    ("reflect-set-window",
     "Reflect.set(window, 'deduplicateTracks', function(t){return t;});"),
    ("globalthis-assign",
     "globalThis.deduplicateTracks = function(t){return t;};"),
    ("multi-arg-object-assign",
     "Object.assign(window, {a:1}, { deduplicateTracks: function(t){return t;} });"),
    ("computed-key-object-assign",
     "var k = 'deduplicateTracks'; Object.assign(window, { [k]: function(t){return t;} });"),
]


@pytest.mark.parametrize("shape_name,injection", _RESIDUAL_ADJACENT_BYPASS_SHAPES)
@pytest.mark.xfail(strict=True, reason=(
    "Adjacent-attack-vector residual: extractor doesn't parse "
    "Object.defineProperty, Reflect.set, globalThis.X, multi-arg Object.assign "
    "(second object's keys), or computed-key Object.assign shapes. A future "
    "tightening pass should add ~5 regex patterns OR adopt an AST-aware "
    "extractor (esprima ~50KB pure-JS). When fixed, strict=True will flip "
    "xpass to fail loudly so this block can be deleted."
))
def test_adjacent_residual_bypass_shapes_are_documented_as_gaps(shape_name, injection):
    """Documents the next-tier bypass surface so a future LLM exploiting one
    of these shapes is caught at code-review time (the gap is visible) rather
    than slipping through silently."""
    names = _extract_top_level_names(injection)
    assert "deduplicateTracks" in names, (
        f"Shape '{shape_name}' was not extracted. Extracted: {sorted(names)}."
    )
