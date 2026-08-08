// Jukeplox shared browse module.
//
// One place for all browse/search/track-row/source-picker/album-tracks/
// artist-albums/queue-append rendering used by both guest and admin pages.
// Per the unification plan, the per-page JS files (static/guest/app.js,
// static/admin/app.js) MUST NOT contain rendering for these surfaces —
// see tests/test_static_discipline.py for the authoritative allowlist.
//
// Mount API:
//   mountBrowser(containerSelector, config) — returns a handle exposing
//   a couple of hooks (refreshNowPlaying not needed here; queue refresh
//   is driven by page WebSocket events). The browse module finds its
//   per-view child IDs inside the container and renders into them.
//
// Config:
//   authMode  — 'guest' | 'admin'. Drives queue endpoint selection
//               (/api/queue vs /admin/queue).
//   isLocked  — () => boolean. Read by add-track / add-album to decide
//               whether to skip the request and toast a lock warning.
//               Admin typically passes () => false (admin bypasses lock).
//   toast     — (msg) => void. Page's toast helper.

'use strict';

(function () {
  // Module-private state and helpers. Exposed only via mountBrowser at the end.

  let _config = null;

  // U7 state: rail drag-scrub bookkeeping. Declared early so U6's tap handler
  // can read _railDragging for its defensive bail-out (rail tap during drag).
  let _railDragging = false;
  let _railBounds = null;
  let _alphaOverlay = null;
  let _alphaObserver = null;
  // Plan 003 U3: scroll-event + rAF highlight state. Replaces the
  // IntersectionObserver approach which dropped intermediate letters on rapid
  // scroll (user-reported C→F hang). `_alphaScrollHandler` is the listener
  // attached to the scroll ancestor; module-scope so _deactivateRail can
  // removeEventListener it. `_alphaScrollPending` is the rAF dedupe flag.
  let _alphaScrollHandler = null;
  let _alphaScrollPending = false;
  let _alphaScrollAncestor = null;
  // U2 (2026-06-09 rail plan): condensation observer on the rail host;
  // created per activation, disconnected in _deactivateRail. The pending
  // flag rAF-debounces bursts (window drag-resize fires the observer every
  // layout frame); the generation counter invalidates callbacks queued
  // before a deactivation (mirrors the _alphaScrollPending discipline).
  let _railResizeObserver = null;
  let _condensationPending = false;
  let _condensationGen = 0;
  // Bucket-start marker ELEMENTS, cached per activation ([el, key] in DOM
  // order). Offsets are deliberately NOT cached — see _attachAlphaObserver.
  let _alphaMarkers = null;
  let _alphaActiveRail = null;
  // Last drag-scrubbed jump target; _settleJump converges onto it at release
  // (pointerup). Cleared by cancelRailDrag so render paths never settle onto
  // a detached row. _settleJumpGen invalidates an in-flight settle loop when
  // a newer jump starts (two loops must never fight over the scroller).
  let _railScrubTarget = null;
  let _settleJumpGen = 0;
  // Timestamp a user gesture interrupted an IN-FLIGHT rail jump settle. A tap
  // that lands while the jump is still converging hits a row that is still
  // moving under the finger — drilling in would open whatever happened to be at
  // that pixel mid-motion (the "wrong artist" — e.g. R.E.M. instead of the
  // aimed-at Rachel Goswell — 2026-08-08 debug). _tapInterruptedJump() lets the
  // nav entry points swallow that one drill; the user's next tap, on now-settled
  // content, opens the right item.
  let _settleAbortedAt = 0;
  const _JUMP_TAP_GUARD_MS = 250;
  const _nowMs = () => (typeof performance !== 'undefined' ? performance.now() : Date.now());
  function _tapInterruptedJump() {
    return (_nowMs() - _settleAbortedAt) < _JUMP_TAP_GUARD_MS;
  }
  // Plan 002 U2: singleton rail on document.body. Replaces per-column build.
  // _activeColumn is the .alpha-items-column the rail currently points at;
  // pointer handlers and the highlight observer read it at event time.
  let _railSingleton = null;
  let _activeColumn = null;
  let _activeBuckets = null;
  let _railSignature = null;
  // Plan 002 U3: observes the active column's visibility. When it leaves
  // viewport (page chrome scroll, tab switch via display:none), the rail
  // deactivates without requiring hooks in per-page JS.
  let _columnVisibilityObserver = null;
  const _dragSupported = !!(typeof window !== 'undefined'
    && window.PointerEvent
    && typeof Element !== 'undefined'
    && Element.prototype.setPointerCapture);

  // ── Endpoint switching ────────────────────────────────────────────────────

  function _queueEndpoint() {
    return _config.authMode === 'admin' ? '/admin/queue' : '/api/queue';
  }

  // ── HTTP helper (shared across both pages) ────────────────────────────────

  async function _api(method, path, body) {
    const resp = await fetch(path, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    // Error bodies parse too (plexplayer plan U5): 4xx rejections carry a
    // JSON `detail` callers branch on (409 output_source_lock vs the
    // flood-control duplicate). Empty/non-JSON bodies degrade to null,
    // exactly what error statuses returned before.
    let data = null;
    try { data = await resp.json(); } catch (_) { /* no JSON body */ }
    return [resp.status, data];
  }

  // ── HTML helpers ──────────────────────────────────────────────────────────

  function _esc(s) {
    return (s || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  // Request art at a size matched to the display slot, not the full cover. A
  // 150KB+ full cover decoded to paint a 48px row was the deep-jump reveal stall
  // (2026-06-25): content-visibility renders the destination band synchronously
  // on arrival, and oversized art dominated that cost. Width is source px (≈3×
  // the CSS slot for HiDPI); the server transcodes + caches per width.
  const _ART_W = { 'list-art': 144, 'tile-art': 360, 'ra-cover-img': 400 };
  function _artImg(thumb, cls) {
    if (!thumb) return `<div class="${cls}" style="display:flex;align-items:center;justify-content:center;font-size:1.2rem">🎷</div>`;
    // decoding="async": decode off the main thread; loading="lazy" defers
    // off-screen fetches; w= keeps the decoded bitmap small.
    const w = _ART_W[cls] || 256;
    return `<img class="${cls}" src="/api/art?path=${encodeURIComponent(thumb)}&w=${w}" alt="" loading="lazy" decoding="async">`;
  }

  // ── Tile view (2026-06-15 tile-view plan U2) ──────────────────────────────
  // One global List/Tile setting rides the appearance system; _config.viewMode
  // is pushed live by mountAppearance through the setViewMode handle (mirrors
  // setRailMode). Tiles are an art-forward alternative for artists + release
  // lists ONLY — tracks and the top-level genres view are never tiled. List
  // rendering paths are left untouched; tile mode is an added branch.
  function _viewIsTiles() { return !!(_config && _config.viewMode === 'tile'); }

  // Round-art artist tile: tap opens the artist (returnView routes back).
  function _artistTile(artist, returnView) {
    const tile = document.createElement('div');
    tile.className = 'tile is-artist';
    const rc = artist && artist.release_count;
    const sub = (typeof rc === 'number' && rc > 0) ? `${rc} release${rc === 1 ? '' : 's'}` : '';
    tile.innerHTML = `<div class="tile-art-wrap">${_artImg(artist.thumb, 'tile-art')}</div>`
      + `<div class="tile-title">${_esc(artist.title)}</div><div class="tile-sub">${sub}</div>`;
    tile.addEventListener('click', () => {
      if (_tapInterruptedJump()) return;   // tap landed mid-jump: don't open a moving tile
      showArtistAlbums(artist.id, artist.title, returnView || 'artists');
    });
    return tile;
  }

  // Square-art release tile: tap opens the release; corner ⋮ queues it (the
  // same sheet the list-row kebab opens). ctx mirrors the row builders:
  // { returnView, parentNav, currentArtistNorm }.
  // noClick: skip the built-in open handler so a caller (the style-album view
  // factory) can wire its own click while keeping the kebab + name-link.
  function _releaseTile(album, ctx, noClick) {
    ctx = ctx || {};
    const tile = document.createElement('div');
    tile.className = 'tile';
    const sub = album.artist ? `<span class="name-link nl-artist">${_esc(album.artist)}</span>` : '';
    tile.innerHTML = `<div class="tile-art-wrap">${_artImg(album.thumb, 'tile-art')}</div>`
      + `<div class="tile-title">${_esc(album.title)}</div><div class="tile-sub">${sub}</div>`;
    if (!noClick) tile.addEventListener('click', () => {
      if (_tapInterruptedJump()) return;   // tap landed mid-jump: don't open a moving tile
      showAlbumTracks(album.id, album.title, ctx.returnView, ctx.parentNav || null);
    });
    if (album.artist) _wireNameLinks(tile, { artist: album.artist, currentArtistNorm: ctx.currentArtistNorm });
    const k = _albumKebabBtn(album, ctx.currentArtistNorm);
    k.classList.add('tile-kebab');
    tile.firstChild.appendChild(k);   // into .tile-art-wrap
    return tile;
  }

  // Tile sibling of _styleAlbumRow for the style-album views (genres tab +
  // search genre drill). Click is wired by the factory (onAlbumClick), so the
  // tile itself is built no-click; the kebab stops propagation as usual.
  function _styleAlbumTile(album) {
    return _releaseTile(album, {}, true);
  }

  // Grid of release tiles for section-based contexts (artist drill) that have
  // no rail column of their own.
  function _releaseTileGrid(albums, ctx) {
    const grid = document.createElement('div');
    grid.className = 'tile-grid';
    albums.forEach(a => grid.appendChild(_releaseTile(a, ctx)));
    return grid;
  }

  function _formatDuration(ms) {
    const s = Math.floor(ms / 1000);
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  }

  // ── Subtype grouping ──────────────────────────────────────────────────────

  // appears_on last (2026-06-10 per-track credits plan U4): releases the
  // artist is credited on per-track, served by the backend with this
  // subtype; for a credit-only act it's the whole drill-in view.
  const SUBTYPE_ORDER = ['album', 'ep', 'single', 'live', 'compilation', 'appears_on'];
  const SUBTYPE_LABELS = { album: 'Albums', ep: 'EPs', single: 'Singles', live: 'Live', compilation: 'Compilations', appears_on: 'Appears On' };

  function groupAlbumsBySubtype(albums) {
    const groups = {};
    albums.forEach(a => {
      const st = (a.subtype || 'album').toLowerCase();
      (groups[st] = groups[st] || []).push(a);
    });
    const result = [];
    SUBTYPE_ORDER.forEach(k => { if (groups[k]) { result.push([k, groups[k]]); delete groups[k]; } });
    Object.entries(groups).forEach(([k, v]) => result.push([k, v]));
    return result;
  }

  function hasMultipleSubtypes(albums) {
    const types = new Set(albums.map(a => (a.subtype || 'album').toLowerCase()));
    return types.size > 1;
  }

  // ── Sorting ───────────────────────────────────────────────────────────────

  // ── Pattern rules (2026-06-10 pattern-rules plan U5) ─────────────────────
  // Mirrors app/normalize.py EXACTLY — the shared contract is the vector
  // list in tests/test_normalize.py; change semantics there and here in
  // lockstep. Comparison/sort/bucket only — display strings never change.

  let _patternRulesCompiled = [];

  function _compilePatternRules(rules) {
    const out = [];
    (rules || []).forEach(rule => {
      const members = (rule || []).filter(s => typeof s === 'string' && s.trim());
      if (members.length < 2) return;
      const canonical = members[0].toLowerCase();
      const others = members.slice(1).map(m => m.toLowerCase()).sort((a, b) => b.length - a.length);
      out.push([canonical, others]);
    });
    return out;
  }

  function _normalizeName(s) {
    let out = (s || '').toLowerCase();
    _patternRulesCompiled.forEach(([canonical, others]) => {
      others.forEach(m => {
        if (out.includes(m)) out = out.split(m).join(canonical);
      });
    });
    return out;
  }

  // Distinct track performers for a release, ordered by track count (desc), ties
  // broken by first appearance (2026-06-20 release-kebab-nav U1). Powers the
  // detail kebab's "Go to artist — [name]" entries; a compilation (>1 distinct)
  // gets several inline + a "More artists…" submenu. Empty names and the literal
  // "Various Artists" album label (not a navigable artist) are dropped.
  function _distinctPerformers(tracks) {
    const order = [];
    const counts = new Map();
    (tracks || []).forEach(t => {
      const name = ((t && t.artist) || '').trim();
      if (!name || name.toLowerCase() === 'various artists') return;
      if (!counts.has(name)) { counts.set(name, 0); order.push(name); }
      counts.set(name, counts.get(name) + 1);
    });
    // count desc, then first-appearance asc (order[] preserves arrival).
    return order.slice().sort((a, b) => counts.get(b) - counts.get(a)
      || order.indexOf(a) - order.indexOf(b));
  }

  async function _loadPatternRules() {
    try {
      const resp = await fetch('/api/pattern-rules');
      if (!resp.ok) return;
      const data = await resp.json();
      _patternRulesCompiled = _compilePatternRules(data.rules);
    } catch (_) { /* no rules is a valid state; sort falls back to plain */ }
  }

  function sortKey(name) {
    const s = (name || '').replace(/^(?:the |a |an )\b/i, '').trim();
    // Normalizing here covers BOTH applySort ordering and
    // computeBuckets rail bucketing (one hook, plan U5).
    return _normalizeName(s || (name || ''));
  }

  function applySort(items, sort, nameField) {
    const a = [...items];
    if (sort === 'alpha_asc') a.sort((x, y) => sortKey(x[nameField]).localeCompare(sortKey(y[nameField])));
    else if (sort === 'alpha_desc') a.sort((x, y) => sortKey(y[nameField]).localeCompare(sortKey(x[nameField])));
    else if (sort === 'year_desc') a.sort((x, y) => (y.year || 0) - (x.year || 0));
    else if (sort === 'year_asc') a.sort((x, y) => (x.year || 0) - (y.year || 0));
    // Artist sorts (plan 005): order by the artist field with album title as the
    // ascending tiebreak (both directions) so an artist's albums always read A→Z
    // by title. The title tiebreak never affects rail monotonicity — same-artist
    // items share a bucket. Uses sortKey so artist sorting agrees with the
    // artist-keyed rail (same normalization).
    else if (sort === 'artist_asc') a.sort((x, y) =>
      sortKey(x.artist).localeCompare(sortKey(y.artist)) ||
      sortKey(x.title).localeCompare(sortKey(y.title)));
    else if (sort === 'artist_desc') a.sort((x, y) =>
      sortKey(y.artist).localeCompare(sortKey(x.artist)) ||
      sortKey(x.title).localeCompare(sortKey(y.title)));
    return a;
  }

  // ── Alphabet index rail helpers ───────────────────────────────────────────

  // U7: cancel an in-flight rail drag. Called at the top of every render
  // path that rebuilds DOM containing the rail: renderArtistsItems,
  // renderAlbumsItems, showArtistAlbums, showAlbumTracks, showYearAlbums.
  // The rule is structural: any render that rebuilds the items-containing
  // DOM MUST call this first or risk leaking pointer capture + stale state.
  function cancelRailDrag() {
    if (!_railDragging) return;
    _railDragging = false;
    _railBounds = null;
    _railScrubTarget = null;
    if (_alphaOverlay) _alphaOverlay.style.opacity = '0';
  }

  function _ensureOverlay() {
    if (_alphaOverlay) return _alphaOverlay;
    _alphaOverlay = document.createElement('div');
    _alphaOverlay.className = 'alpha-overlay';
    document.body.appendChild(_alphaOverlay);
    return _alphaOverlay;
  }

  // Walk up from the .alpha-items-column to the first ancestor with
  // overflowY === 'auto' | 'scroll'. Resolves to #content on guest,
  // #artists-list / #albums-list on admin. Falls back to null (viewport).
  function _findScrollAncestor(el) {
    let cur = el && el.parentElement;
    while (cur && cur !== document.body) {
      const overflow = getComputedStyle(cur).overflowY;
      if (overflow === 'auto' || overflow === 'scroll') return cur;
      cur = cur.parentElement;
    }
    return null;
  }

  // U2: desktop layouts swap which element scrolls the library (guest:
  // #content on mobile, the library pane at >=960px). Crossing the
  // breakpoint while a rail-bearing view is active would otherwise leave
  // the highlight listener on a no-longer-scrolling element and the rail
  // parked in the wrong host — re-run activation to re-resolve both.
  // Lives in the shared module (not page scripts) per the discipline rule.
  if (typeof window !== 'undefined' && window.matchMedia) {
    const _railBp = window.matchMedia('(min-width: 960px)');
    const _railBpHandler = () => {
      // A crossing mid-scrub would re-parent the captured rail and
      // invalidate the cached drag bounds — end the drag first.
      if (_railDragging) cancelRailDrag();
      // The page's own breakpoint handler may fire first in the same MQL
      // batch and trigger a re-render that replaces the column (whose own
      // _activateRail already bound the fresh pane). Defer a frame and
      // re-check staleness so we never re-bind to a detached column.
      requestAnimationFrame(() => {
        if (_activeColumn && _activeBuckets && document.contains(_activeColumn)) {
          _activateRail(_activeColumn, _activeBuckets);
        }
      });
    };
    if (_railBp.addEventListener) _railBp.addEventListener('change', _railBpHandler);
    else if (_railBp.addListener) _railBp.addListener(_railBpHandler);
  }

  // 2026-06-09 rail plan U2: the rail's positioning host is the PARENT of
  // the scroll container — the rail must sibling the scroller, never live
  // inside it (an absolutely-positioned child of the scroller scrolls away
  // with the content). Guest: #content-frame wrapping #content. Admin:
  // #artists-view / #albums-view wrapping their max-height lists. The host
  // gets .rail-host (position: relative) so the absolute rail spans its box.
  function _ensureRailHost(column) {
    const scroller = _findScrollAncestor(column);
    if (!scroller || !scroller.parentElement || scroller.parentElement === document.body) {
      return null;
    }
    const host = scroller.parentElement;
    host.classList.add('rail-host');
    return host;
  }

  // U2: condensation — when the host gives the rail fewer than ~13px per
  // letter, alternating letters elide to dots (CSS-driven via .elide; the
  // active letter never elides). Scrub geometry is untouched: 27 buckets
  // divide the rail rect regardless of which labels are dotted.
  function _applyCondensation() {
    if (!_railSingleton) return;
    const n = _railSingleton.children.length;
    const per = n > 0 ? _railSingleton.clientHeight / n : 0;
    const condensed = per > 0 && per < 13;
    _railSingleton.classList.toggle('condensed', condensed);
    Array.from(_railSingleton.children).forEach((btn, i) => {
      btn.classList.toggle('elide', condensed && i % 2 === 1);
    });
  }

  function _scheduleCondensation() {
    if (_condensationPending) return;
    _condensationPending = true;
    const gen = _condensationGen;
    requestAnimationFrame(() => {
      _condensationPending = false;
      if (gen !== _condensationGen) return;  // deactivated since scheduling
      _applyCondensation();
    });
  }

  // 2026-06-10 gutter fix (2026-06-24 perf rework): position the rail clear of
  // the scroller's native scrollbar and reserve a right-hand lane on the items
  // column so row content (chevrons, add buttons) never runs under the rail.
  // --rail-inset (the rail's `right`) is set ON THE RAIL ELEMENT, and the lane
  // is set as direct padding-right ON THE COLUMN — NOT as custom properties on
  // the host. A host custom-property change invalidates style for every cell
  // that inherits it (an O(N) recalc that was the cold-render stall, confirmed
  // via tools/perf/browse-bench). Both values are mode-aware via live
  // offsetWidth; re-run on host resize since scrollbar presence flips when
  // content height crosses pane height.
  function _railLaneRefresh() {
    if (!_railSingleton || !_activeColumn) return;
    const host = _railSingleton.parentElement;
    if (!host || host === document.body) return;
    const scroller = _findScrollAncestor(_activeColumn);
    if (!scroller) return;
    const sbw = Math.max(0, scroller.offsetWidth - scroller.clientWidth);
    const inset = sbw + 6;
    // Set --rail-inset on the RAIL element itself, not the host. The rail reads
    // its own `right: var(--rail-inset)`; setting it on the host would invalidate
    // style for the host's whole subtree (the N cells inherit the custom property)
    // — the same O(N) restyle the lane var caused (2026-06-24 browse-stall fix).
    _railSingleton.style.setProperty('--rail-inset', inset + 'px');
    // Reserve the lane as direct padding-right ON THE COLUMN, not a custom
    // property on the host. A host custom-property change invalidates style for
    // every descendant that inherits it — an O(N) style recalc over all cells
    // that was the dominant cold-render stall (2026-06-24, confirmed by the
    // tools/perf bench: it owned the single ~200ms+ long task at 20k rows).
    // padding-right is not inherited, so only the column restyles; its cells
    // merely reflow, which content-visibility keeps cheap.
    const lane = inset + _railSingleton.offsetWidth + 8;
    const extra = _activeColumn.classList.contains('tile-grid') ? 14 : 0;
    _activeColumn.style.paddingRight = (lane + extra) + 'px';
  }

  // ── Sort-aware rail: dimension resolution + bucket computation ──────────────
  // (2026-06-20 sort-aware-index-rail plan). The rail re-indexes to match the
  // active sort: letters (reversed for Z→A) for name sorts, an adaptive time
  // ladder for Album year sorts, hidden for Most Played. computeBuckets
  // generalizes the fixed A–Z letter index to an ordered bucket list shared by
  // every rail mode and both pages. A "bucket" is { key, label, displayLabel,
  // anchor, firstIndex, count }; `keyForItem[idx]` is the bucket key for each
  // (already-sorted) item, used to mark data-bucket-start on the first cell of
  // each run.
  const RAIL_BUCKET_CAP = 25;  // finest TIME layout must stay at/under this many buckets
  const _RAIL_AZ = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'.split('');
  // Collation-derived non-Latin buckets (2026-06-22 plan 002). The fixed
  // '#'-pinned-to-A-end list was replaced by a leading catch-all (key '#',
  // symbols/digits that collate before A), the always-shown A–Z letters, and a
  // trailing region (non-Latin scripts that collate after Z). Catch-alls show
  // only when non-empty; ascending order is reversed wholesale for Z→A, which
  // flips both ends for free (position-based geometry).
  const LEADING_KEY = '#';
  const TRAILING_KEY = 'trailing';
  // Lowercase A–Z anchors in collation order. A first char's letter bucket is
  // decided by where it collates among these under the same default localeCompare
  // applySort orders by — so every Latin-extended letter (ƒ, ß, ø, é …) buckets
  // among the letters it sorts with, no finite fold-map required, and the rail
  // stays monotonic with the sort by construction. (sortKey lowercases first, so
  // the classified char is already lowercase.)
  const _RAIL_AZ_LOWER = 'abcdefghijklmnopqrstuvwxyz'.split('');
  // U2: the trailing region splits per Unicode script group only when worthwhile
  // — 2+ groups each clearing RAIL_TRAILING_MIN, and no more distinct groups than
  // RAIL_TRAILING_SCRIPT_CAP (else collapse to one bucket). This cap is distinct
  // from the TIME cap (RAIL_BUCKET_CAP). The group table is _RAIL_SCRIPT_GROUPS.
  const RAIL_TRAILING_SCRIPT_CAP = 8;
  const RAIL_TRAILING_MIN = 3;
  // Han + kana share one 'CJK' group (a Japanese act starting with kanji or kana
  // lands in the same bucket); Hangul stays its own (Korean is a distinct
  // alphabet). Order within the list is match-priority, not display order.
  const _RAIL_SCRIPT_GROUPS = [
    [/\p{Script=Han}/u, 'CJK'],
    [/\p{Script=Hiragana}/u, 'CJK'],
    [/\p{Script=Katakana}/u, 'CJK'],
    [/\p{Script=Hangul}/u, 'Hangul'],
    [/\p{Script=Greek}/u, 'Greek'],
    [/\p{Script=Cyrillic}/u, 'Cyrillic'],
    [/\p{Script=Arabic}/u, 'Arabic'],
    [/\p{Script=Hebrew}/u, 'Hebrew'],
    [/\p{Script=Thai}/u, 'Thai'],
    [/\p{Script=Devanagari}/u, 'Devanagari'],
  ];

  // Map (view, sort) → rail dimension, or null to hide the rail.
  // view: 'artists' | 'albums'. Time rail is Albums-only (artists have no year).
  function resolveRailDimension(view, sort) {
    if (sort === 'alpha_asc')  return { kind: 'letters', dir: 'asc' };
    if (sort === 'alpha_desc') return { kind: 'letters', dir: 'desc' };
    // Artist sorts (plan 005): a letter rail like the alpha sorts, keyed by the
    // artist field at the call site (nameField='artist'). View-independent.
    if (sort === 'artist_asc')  return { kind: 'letters', dir: 'asc' };
    if (sort === 'artist_desc') return { kind: 'letters', dir: 'desc' };
    if (view === 'albums' && sort === 'year_asc')  return { kind: 'time', dir: 'asc' };
    if (view === 'albums' && sort === 'year_desc') return { kind: 'time', dir: 'desc' };
    return null;  // 'popular' (both views) and anything else → no rail
  }

  // Finest time granularity whose bucket count stays ≤ RAIL_BUCKET_CAP:
  // years → 5-year bins → decades. Decades is the floor and a forward-looking
  // rung (only reached once the catalog spans ~125 years).
  function adaptiveTimeGranularity(years) {
    if (!years.length) return 'decades';
    const distinct = (span) => new Set(years.map(y => Math.floor(y / span))).size;
    if (distinct(1) <= RAIL_BUCKET_CAP) return 'years';
    if (distinct(5) <= RAIL_BUCKET_CAP) return 'lustrum';
    return 'decades';
  }

  // Short rail label + fuller overlay label for a time bucket's base year.
  function _timeLabels(base, gran) {
    if (gran === 'decades') return { label: String(base).slice(2) + 's', displayLabel: base + 's' };
    if (gran === 'lustrum') return { label: String(base).slice(2), displayLabel: base + '–' + (base + 4) };
    return { label: String(base).slice(2), displayLabel: String(base) };  // years
  }

  // International rail config for a list context, or null when the rail should use
  // the shipped English-friendly path (plan 004). The artists list uses the artist
  // threshold; the albums list uses the album threshold.
  function _alphaConfig(context) {
    if (!_config || _config.railAlphaMode !== 'international') return null;
    return {
      mode: 'international',
      threshold: context === 'albums' ? _config.railAlbumThreshold : _config.railArtistThreshold,
    };
  }

  // items MUST already be sorted in the dimension's order (applySort / leaderboard).
  // `alpha` (optional, 2026-06-22 plan 004): { mode, threshold } selecting the
  // letter-rail construction. mode 'international' rebuilds the rail from the
  // library's actual first characters; anything else (incl. omitted) keeps the
  // shipped English-friendly A–Z rail. Ignored for the time dimension.
  function computeBuckets(items, dim, nameField, alpha) {
    if (dim.kind === 'time') return _computeTimeBuckets(items, dim.dir);
    if (alpha && alpha.mode === 'international')
      return _computeInternationalBuckets(items, nameField, dim.dir, alpha.threshold);
    return _computeLetterBuckets(items, nameField, dim.dir);
  }

  // International alpha rail (2026-06-22 plan 004): instead of the fixed A–Z
  // scaffold, build the rail from the FIRST CHARACTERS actually present, in the
  // list's collation order. A first-char earns its own bucket only when its item
  // count meets `threshold` (uniform — Latin letters included); symbols/digits
  // stay grouped under the '#' leading catch-all, which is shown whenever
  // non-empty (never threshold-gated). Items are already dir-sorted, so the
  // first-appearance order IS the collation order for `dir` — no reversal (hence
  // no double-reverse), monotonic with the sort by construction. Suppressed-char
  // items fold into the most recent shown bucket's run (leading sub-threshold
  // items fold forward into the first shown bucket) so every item maps to a real
  // shown bucket and stays reachable by scrubbing.
  function _computeInternationalBuckets(items, nameField, dir, threshold) {
    const minCount = (typeof threshold === 'number' && threshold >= 1) ? threshold : 2;
    const firstChar = new Array(items.length);
    const counts = new Map();
    items.forEach((item, idx) => {
      const ch = [...sortKey(item[nameField] || '')][0] || '';
      const key = (!ch || ch.localeCompare('a') < 0) ? LEADING_KEY : ch;
      firstChar[idx] = key;
      counts.set(key, (counts.get(key) || 0) + 1);
    });
    const isShown = k => (k === LEADING_KEY)
      ? (counts.get(k) > 0)                       // '#' always shown when non-empty
      : ((counts.get(k) || 0) >= minCount);
    const order = [];
    const seen = new Set();
    firstChar.forEach(k => { if (isShown(k) && !seen.has(k)) { seen.add(k); order.push(k); } });
    const firstShown = order.length ? order[0] : null;
    const keyForItem = new Array(items.length);
    const firstIndex = new Map();
    let current = firstShown;
    items.forEach((item, idx) => {
      const k = firstChar[idx];
      if (isShown(k)) current = k;
      keyForItem[idx] = current;
      if (current != null && !firstIndex.has(current)) firstIndex.set(current, idx);
    });
    const buckets = order.map((key, i) => {
      const label = key === LEADING_KEY ? '#' : key.toUpperCase();
      return {
        key, label, displayLabel: label, anchor: i % 3 === 0,
        firstIndex: firstIndex.has(key) ? firstIndex.get(key) : -1,
        count: counts.get(key) || 0,
      };
    });
    return { buckets, keyForItem };
  }

  // Classify the first sortKey char of a name by collation position — not by a
  // fold-then-membership check. The char is already sortKey-normalized
  // (lowercased), so we compare with the same default localeCompare applySort
  // orders by:
  //   collates before 'a' → 'leading'  (symbols/digits, pinned to the A-end '#')
  //   collates after  'z' → 'trailing' (genuine non-Latin script, sorts past Z)
  //   otherwise           → 'letter', owning bucket = greatest A–Z anchor it
  //                         collates at/after (ƒ→F, é→E, ß→S, ø→O — no map).
  // Monotonic with the sort by construction; retires the finite-Latin-map class
  // that mis-bucketed unmapped non-decomposing letters (e.g. ƒ) to the trailing
  // end. A literal a–z first char short-circuits without any localeCompare call.
  function _railCharClass(ch) {
    if (!ch) return { cls: 'leading' };
    if (ch >= 'a' && ch <= 'z') return { cls: 'letter', letter: ch.toUpperCase() };
    if (ch.localeCompare('a') < 0) return { cls: 'leading' };
    if (ch.localeCompare('z') > 0) return { cls: 'trailing' };
    let lo = 0, hi = _RAIL_AZ_LOWER.length - 1, owner = 'a';
    while (lo <= hi) {                       // greatest anchor with ch >= anchor
      const mid = (lo + hi) >> 1;
      if (ch.localeCompare(_RAIL_AZ_LOWER[mid]) >= 0) { owner = _RAIL_AZ_LOWER[mid]; lo = mid + 1; }
      else hi = mid - 1;
    }
    return { cls: 'letter', letter: owner.toUpperCase() };
  }

  // Map a trailing (non-Latin) char to its script-group key for the U2 split.
  function _railScriptGroup(ch) {
    for (const [re, g] of _RAIL_SCRIPT_GROUPS) if (re.test(ch)) return g;
    return 'Other';
  }

  // Per-script-group rail glyph + overlay label (plan-002 U3, chosen against the
  // mockup). Single-glyph labels fit the existing letter-cell width, so no
  // dim-scripts CSS affordance is needed.
  const _RAIL_SCRIPT_LABELS = {
    CJK: { label: '字', displayLabel: 'CJK (Han / kana)' },
    Hangul: { label: '한', displayLabel: 'Hangul' },
    Greek: { label: 'Ω', displayLabel: 'Greek' },
    Cyrillic: { label: 'Я', displayLabel: 'Cyrillic' },
    Arabic: { label: 'ع', displayLabel: 'Arabic' },
    Hebrew: { label: 'א', displayLabel: 'Hebrew' },
    Thai: { label: 'ก', displayLabel: 'Thai' },
    Devanagari: { label: 'अ', displayLabel: 'Devanagari' },
    Other: { label: '⊙', displayLabel: 'Other scripts' },
  };

  // Rail label (short) + overlay label for a bucket key. The trailing glyphs are
  // placeholders finalized against a mockup in plan-002 U3.
  function _railBucketLabels(key) {
    if (key === LEADING_KEY) return { label: '#', displayLabel: '#' };
    if (key === TRAILING_KEY) return { label: '字', displayLabel: 'Other scripts' };
    if (key.startsWith(TRAILING_KEY + ':')) {
      const g = key.slice(TRAILING_KEY.length + 1);
      return _RAIL_SCRIPT_LABELS[g] || { label: '字', displayLabel: g };
    }
    return { label: key, displayLabel: key };  // A–Z
  }

  function _computeLetterBuckets(items, nameField, dir) {
    const firstIndex = new Map();
    const counts = new Map();
    const keyForItem = new Array(items.length);
    let hasLeading = false;
    // Trailing items are classified to a script group first; the final key
    // (single bucket vs per-script) is decided after the pass (U2).
    const trailingGroup = new Array(items.length);   // group key, or undefined
    const groupFirst = new Map();                    // group → first item index
    const groupCount = new Map();
    items.forEach((item, idx) => {
      const ch = [...sortKey(item[nameField] || '')][0] || '';
      const c = _railCharClass(ch);
      if (c.cls === 'letter') {
        keyForItem[idx] = c.letter;
        if (!firstIndex.has(c.letter)) firstIndex.set(c.letter, idx);
        counts.set(c.letter, (counts.get(c.letter) || 0) + 1);
      } else if (c.cls === 'leading') {
        keyForItem[idx] = LEADING_KEY;
        hasLeading = true;
        if (!firstIndex.has(LEADING_KEY)) firstIndex.set(LEADING_KEY, idx);
        counts.set(LEADING_KEY, (counts.get(LEADING_KEY) || 0) + 1);
      } else {
        const g = _railScriptGroup(ch);
        trailingGroup[idx] = g;
        if (!groupFirst.has(g)) groupFirst.set(g, idx);
        groupCount.set(g, (groupCount.get(g) || 0) + 1);
      }
    });
    // Trailing layout: split per-script only when 2+ groups clear the minimum
    // and the distinct-group count is within the cap; otherwise one bucket.
    const groups = [...groupFirst.keys()].sort((a, b) => groupFirst.get(a) - groupFirst.get(b));
    const qualifying = groups.filter(g => groupCount.get(g) >= RAIL_TRAILING_MIN).length;
    const split = qualifying >= 2 && groups.length <= RAIL_TRAILING_SCRIPT_CAP;
    const trailingKeyOf = g => (split ? TRAILING_KEY + ':' + g : TRAILING_KEY);
    items.forEach((item, idx) => {
      const g = trailingGroup[idx];
      if (g === undefined) return;
      const key = trailingKeyOf(g);
      keyForItem[idx] = key;
      if (!firstIndex.has(key)) firstIndex.set(key, idx);
      counts.set(key, (counts.get(key) || 0) + 1);
    });
    // The leading catch-all + A–Z are the canonical core (reversed wholesale for
    // desc). The trailing region collates AFTER Z, so it sits at the END in asc
    // and the START in desc. `groups` is already in the items' sort-direction
    // appearance order, so it must NOT be reversed again — reversing both the
    // core and the trailing groups would make Z→A non-monotonic.
    const core = [];
    if (hasLeading) core.push(LEADING_KEY);
    for (const L of _RAIL_AZ) core.push(L);
    const coreOrdered = dir === 'desc' ? core.reverse() : core;
    const trailingKeys = groups.length
      ? (split ? groups.map(trailingKeyOf) : [TRAILING_KEY])
      : [];
    const order = dir === 'desc'
      ? [...trailingKeys, ...coreOrdered]
      : [...coreOrdered, ...trailingKeys];
    // anchor every 3rd bucket in display order (preserves waveform sparse labels).
    const buckets = order.map((key, i) => {
      const { label, displayLabel } = _railBucketLabels(key);
      return {
        key, label, displayLabel, anchor: i % 3 === 0,
        firstIndex: firstIndex.has(key) ? firstIndex.get(key) : -1,
        count: counts.get(key) || 0,
      };
    });
    return { buckets, keyForItem };
  }

  function _computeTimeBuckets(items, dir) {
    const years = [];
    items.forEach(i => { if (typeof i.year === 'number' && i.year > 0) years.push(i.year); });
    const gran = adaptiveTimeGranularity(years);
    const span = gran === 'decades' ? 10 : gran === 'lustrum' ? 5 : 1;
    const baseOf = y => Math.floor(y / span) * span;
    const firstIndex = new Map();
    const counts = new Map();
    const keyForItem = new Array(items.length);
    let hasUnknown = false;
    items.forEach((item, idx) => {
      const y = item.year;
      const key = (typeof y === 'number' && y > 0) ? ('b' + baseOf(y)) : 'unknown';
      if (key === 'unknown') hasUnknown = true;
      keyForItem[idx] = key;
      if (!firstIndex.has(key)) firstIndex.set(key, idx);
      counts.set(key, (counts.get(key) || 0) + 1);
    });
    const asc = [];
    if (years.length) {
      const lo = baseOf(Math.min(...years)), hi = baseOf(Math.max(...years));
      for (let b = lo; b <= hi; b += span) {       // contiguous range, interior empties kept
        const key = 'b' + b;
        const { label, displayLabel } = _timeLabels(b, gran);
        asc.push({ key, label, displayLabel, anchor: b % 10 === 0,
          firstIndex: firstIndex.has(key) ? firstIndex.get(key) : -1, count: counts.get(key) || 0 });
      }
    }
    const buckets = dir === 'desc' ? asc.slice().reverse() : asc;
    if (hasUnknown) {                              // pin Unknown to the chronologically-oldest end
      const u = { key: 'unknown', label: '?', displayLabel: 'Unknown', anchor: true,
        firstIndex: firstIndex.get('unknown'), count: counts.get('unknown') };
      if (dir === 'desc') buckets.push(u); else buckets.unshift(u);
    }
    return { buckets, keyForItem };
  }

  // Mark the first cell of each bucket run so the rail can jump/highlight.
  // Generalizes _setLetterStart; items are in bucket order, so a key change
  // from the previous item marks a bucket start.
  function _setBucketStart(cell, keyForItem, idx) {
    if (idx === 0 || keyForItem[idx] !== keyForItem[idx - 1]) {
      cell.dataset.bucketStart = keyForItem[idx];
    }
  }

  // Plan 002 U2: build the singleton rail once on document.body. Idempotent —
  // subsequent calls return the same _railSingleton reference. The rail is
  // hidden by default (display:none); _activateRail / _deactivateRail toggle
  // the .visible class.
  // Children are (re)built per bucket set by _buildRailButtons; each mode
  // decorates the button differently (density rows, waveform/VU bars+segs,
  // loupe ticks) but every child carries dataset.bucket so highlight + click +
  // scrub stay mode-agnostic.
  function _ensureRailSingleton() {
    if (_railSingleton) return _railSingleton;
    // Sort-aware rail (2026-06-20): the container is built + wired once; its
    // CHILD buttons are (re)built per bucket set by _buildRailButtons (called
    // from _activateRail), so the rail can switch between the A–Z letter set, a
    // time ladder, or a reversed order with no teardown. Mode (vanilla /
    // magnetic / waveform / loupe / vu / legacy density) is baked at build time
    // and stored on dataset.mode; setRailMode nulls the singleton to swap it.
    const cfgMode = _config && _config.railMode;
    const KNOWN_MODES = ['vanilla', 'magnetic', 'density', 'waveform', 'loupe', 'vu'];
    const mode = KNOWN_MODES.indexOf(cfgMode) !== -1 ? cfgMode : 'vanilla';
    const rail = document.createElement('nav');
    rail.className = `alpha-rail mode-${mode}`;
    rail.dataset.mode = mode;
    rail.setAttribute('role', 'navigation');
    rail.setAttribute('aria-label', 'Jump to section');
    document.body.appendChild(rail);
    _wireRailInteractions(rail);
    _railSingleton = rail;
    _railSignature = null;
    return rail;
  }

  // Decorate one rail button for a bucket in the given mode. Single source for
  // all five modes so a bucket set renders identically across the letter and
  // time dimensions. data-bucket carries the jump key; data-label the overlay
  // text; bucket.anchor marks sparse-label buckets (every 3rd letter / decade
  // boundaries) for the waveform mode.
  function _decorateRailButton(btn, mode, bucket) {
    btn.type = 'button';
    btn.dataset.bucket = bucket.key;
    btn.dataset.label = bucket.displayLabel;
    btn.setAttribute('aria-label', `Jump to ${bucket.displayLabel}`);
    btn.textContent = bucket.label;  // vanilla IS the base; decorations overwrite
    if (mode === 'density') {
      btn.className = 'density-row';
      // span (not div) for the bar — buttons permit phrasing content only.
      btn.innerHTML = '<span class="count">0</span><span class="bar"></span><span class="label"></span>';
      const labelEl = btn.querySelector('.label');
      if (labelEl) labelEl.textContent = bucket.label;
    } else if (mode === 'magnetic') {
      btn.className = 'magnetic-letter';
    } else if (mode === 'waveform') {
      btn.className = 'wave-row' + (bucket.anchor ? ' anchor' : '');
      btn.innerHTML = '<span class="label"></span><span class="bar"></span>';
      const labelEl = btn.querySelector('.label');
      if (labelEl) labelEl.textContent = bucket.label;
    } else if (mode === 'vu') {
      btn.className = 'vu-row';
      btn.innerHTML = '<span class="label"></span><span class="seg"></span>';
      const labelEl = btn.querySelector('.label');
      if (labelEl) labelEl.textContent = bucket.label;
    } else if (mode === 'loupe') {
      btn.className = 'loupe-slot';
    }
  }

  // (Re)build the rail's child buttons for an ordered bucket list.
  function _buildRailButtons(rail, buckets, mode) {
    rail.textContent = '';
    buckets.forEach(bucket => {
      const btn = document.createElement('button');
      _decorateRailButton(btn, mode, bucket);
      rail.appendChild(btn);
    });
  }

  // Plan 002 U2: point the singleton rail at this column. Updates dim states,
  // clears stale aria-current, (re)attaches the highlight observer, and starts
  // observing column visibility for tab-switch detection. Called by render
  // functions when the active sort is alphabetical.
  // Plan 003 U4: letterIndex is now `{firstIndex: Map, counts: Map}`. Density
  // mode additionally walks .density-row children to set log-scale bar widths.
  function _activateRail(column, buckets) {
    const rail = _ensureRailSingleton();
    const mode = rail.dataset.mode || 'vanilla';
    // Rebuild the child buttons when the bucket set changes (sort flip,
    // granularity change, dimension switch) or when the singleton was just
    // recreated empty (post mode-switch); otherwise re-point the existing set.
    const sig = buckets.map(b => b.key).join(',');
    if (rail.children.length === 0 || _railSignature !== sig) {
      _buildRailButtons(rail, buckets, mode);
      _railSignature = sig;
    }
    _activeColumn = column;
    _activeBuckets = buckets;
    // Mark time-dimension rails so the stylesheet can give 2–3 char labels
    // (decades "60s", 2-digit years) room the single-letter widths don't.
    rail.classList.toggle('dim-time', buckets.some(b => b.key === 'unknown' || /^b\d/.test(b.key)));
    // U2: re-parent the singleton (and the scrub overlay) into the active
    // pane's positioning host BEFORE any rect-dependent work — the rail may
    // arrive detached or parked in a previous pane's host. Falls back to
    // body + viewport-fixed styling when no host resolves (defensive).
    const host = _ensureRailHost(column);
    const railParent = host || document.body;
    if (rail.parentElement !== railParent) railParent.appendChild(rail);
    rail.classList.toggle('no-host', !host);
    const overlay = _ensureOverlay();
    if (overlay.parentElement !== railParent) railParent.appendChild(overlay);
    overlay.classList.toggle('no-host', !host);
    // Center the scrub overlay on the SCROLLER's box, not the host's — on
    // the guest desktop two-pane the host (#content) also contains the
    // docked Now pane, and a host-centered overlay would sit over both
    // panes instead of the library being scrubbed (review fix, corroborated).
    const scroller = _findScrollAncestor(column);
    if (host && scroller) {
      overlay.style.left = (scroller.offsetLeft + scroller.clientWidth / 2) + 'px';
    } else {
      overlay.style.left = '';
    }
    // U2: observe the host for condensation; one observer per activation.
    if (_railResizeObserver) {
      try { _railResizeObserver.disconnect(); } catch (_) { /* noop */ }
      _railResizeObserver = null;
    }
    if (typeof ResizeObserver !== 'undefined') {
      _railResizeObserver = new ResizeObserver(() => {
        _scheduleCondensation();
        _railLaneRefresh();
      });
      _railResizeObserver.observe(railParent === document.body ? rail : railParent);
    }
    _scheduleCondensation();
    // Build firstIndex (present buckets only) + counts Maps from the bucket
    // list for the count-encoding refreshers and the dim/aria-disabled pass.
    const firstIndex = new Map();
    const counts = new Map();
    buckets.forEach(b => { counts.set(b.key, b.count); if (b.count > 0) firstIndex.set(b.key, b.firstIndex); });
    // Refresh dim/empty + count state per rail child.
    if (rail.classList.contains('mode-density')) {
      _densityRefresh(rail, firstIndex, counts);
    } else if (rail.classList.contains('mode-waveform')) {
      _waveformRefresh(rail, firstIndex, counts);
    } else {
      Array.from(rail.children).forEach(btn => {
        if (firstIndex.has(btn.dataset.bucket)) btn.removeAttribute('aria-disabled');
        else btn.setAttribute('aria-disabled', 'true');
      });
    }
    // Clear stale aria-current — observer reapplies on next scroll event.
    Array.from(rail.children).forEach(btn => btn.removeAttribute('aria-current'));
    // Highlight observer + column-visibility observer.
    _attachAlphaObserver(rail, column);
    _watchColumnVisibility(column);
    rail.classList.add('visible');
    // Lane vars need the rail's rendered width — measure AFTER .visible
    // flips display:none → flex (offsetWidth is 0 while hidden).
    _railLaneRefresh();
  }

  // Plan 003 U5: magnetic mode interaction. _magneticUpdate is invoked on
  // every pointer movement over the rail and walks every button to compute
  // a Gaussian-falloff scale + opacity. _magneticReset clears inline styles
  // when the cursor leaves the rail so transitions return buttons to base
  // state. Constants lifted verbatim from magnetic.html mockup.
  const _MAGNETIC_SIGMA = 50;
  const _MAGNETIC_MAX_SCALE = 2.2;

  function _magneticUpdate(mouseY) {
    if (_railDragging) return;
    if (!_railSingleton || !_railSingleton.classList.contains('mode-magnetic')) return;
    const children = Array.from(_railSingleton.children);
    const twoSigmaSq = 2 * _MAGNETIC_SIGMA * _MAGNETIC_SIGMA;
    children.forEach(btn => {
      const rect = btn.getBoundingClientRect();
      const btnY = rect.top + rect.height / 2;
      const dist = Math.abs(mouseY - btnY);
      const falloff = Math.exp(-(dist * dist) / twoSigmaSq);
      // U4 (2026-06-09 rail plan): write the falloff as a CSS custom
      // property; the stylesheet computes scale/opacity from it. Inline
      // transform/opacity writes outranked the [aria-current] accent rules
      // (the "highlight never transfers" defect). _MAGNETIC_MAX_SCALE
      // lives in the stylesheet calc as (max - 1) = 1.2.
      btn.style.setProperty('--falloff', falloff.toFixed(3));
    });
  }

  function _magneticReset() {
    if (!_railSingleton) return;
    Array.from(_railSingleton.children).forEach(btn => {
      btn.style.removeProperty('--falloff');
      // Legacy inline styles from the pre-U4 channel (harmless if absent).
      btn.style.transform = '';
      btn.style.opacity = '';
    });
    _railSingleton.classList.remove('engaged');
  }

  // Plan 003 U4 / 2026-06-09 rail plan U3: refresh density-row bar widths +
  // empty class. Sqrt-ratio scale (`max(3, round(sqrt(n / maxN) * 34))`)
  // replaces the original log scale, which compressed real library shapes
  // into near-identical bars (380-vs-180 rendered ~12% apart; sqrt makes it
  // ~45% — 34px vs ~23px — while a 5-count bucket stays visible at ~4px).
  // The 3px floor keeps single-entry letters discoverable.
  function _densityRefresh(rail, firstIndex, counts) {
    let maxCount = 0;
    counts.forEach(v => { if (v > maxCount) maxCount = v; });
    Array.from(rail.children).forEach(row => {
      const L = row.dataset.bucket;
      const n = counts.get(L) || 0;
      const isEmpty = !firstIndex.has(L);
      row.classList.toggle('empty', isEmpty);
      if (isEmpty) row.setAttribute('aria-disabled', 'true');
      else row.removeAttribute('aria-disabled');
      const countEl = row.querySelector('.count');
      if (countEl) countEl.textContent = String(n);
      const barEl = row.querySelector('.bar');
      if (barEl) {
        const w = (n > 0 && maxCount > 0)
          ? Math.max(3, Math.round(Math.sqrt(n / maxCount) * 34))
          : 0;
        barEl.style.width = w + 'px';
      }
    });
  }

  // Glow-up U4: waveform-row bar sizing. Same sqrt-ratio scale rationale as
  // _densityRefresh (real library shapes compress under log/linear); width
  // 3..25px against the 44px rail, height 2..6px gives the mirrored-bar
  // waveform silhouette from the locked mockup. Empty letters keep a 2px
  // stub (dimmed via .empty CSS) so the wave reads as continuous.
  function _waveformRefresh(rail, firstIndex, counts) {
    let maxCount = 0;
    counts.forEach(v => { if (v > maxCount) maxCount = v; });
    Array.from(rail.children).forEach(row => {
      const L = row.dataset.bucket;
      const n = counts.get(L) || 0;
      const isEmpty = !firstIndex.has(L);
      row.classList.toggle('empty', isEmpty);
      if (isEmpty) row.setAttribute('aria-disabled', 'true');
      else row.removeAttribute('aria-disabled');
      const barEl = row.querySelector('.bar');
      if (barEl) {
        const ratio = (n > 0 && maxCount > 0) ? Math.sqrt(n / maxCount) : 0;
        barEl.style.width = (n > 0 ? Math.max(3, Math.round(ratio * 25)) : 2) + 'px';
        barEl.style.height = Math.max(2, Math.round(2 + ratio * 4)) + 'px';
      }
    });
  }

  // Plan 002 U2: hide the rail and disconnect both observers. Called by
  // drill-in paths that destroy the active column's DOM (showArtistAlbums,
  // showAlbumTracks, showYearAlbums), by render functions when sort flips off
  // alpha, and by the column-visibility observer when the active column
  // leaves viewport.
  function _deactivateRail() {
    if (!_railSingleton) return;
    _railSingleton.classList.remove('visible');
    // Gutter fix: drop the lane vars so the pane's reserved right padding
    // collapses while no rail is shown (non-alpha sorts, drill-ins, tabs).
    _railSingleton.style.removeProperty('--rail-inset');
    // Drop the column's reserved lane padding (set directly in _railLaneRefresh)
    // so a column that persists past deactivation doesn't keep a stale gutter.
    if (_activeColumn) _activeColumn.style.paddingRight = '';
    if (_alphaObserver) {
      try { _alphaObserver.disconnect(); } catch (_) { /* noop */ }
      _alphaObserver = null;
    }
    // Plan 003 U3: detach the scroll-event highlight listener so it doesn't
    // fire against a stale rail/column after deactivation.
    if (_alphaScrollHandler) {
      const target = _alphaScrollAncestor || window;
      try { target.removeEventListener('scroll', _alphaScrollHandler); } catch (_) { /* noop */ }
      _alphaScrollHandler = null;
    }
    _alphaScrollPending = false;
    _alphaScrollAncestor = null;
    _alphaMarkers = null;
    _alphaActiveRail = null;
    if (_columnVisibilityObserver) {
      try { _columnVisibilityObserver.disconnect(); } catch (_) { /* noop */ }
      _columnVisibilityObserver = null;
    }
    // U2: detach the condensation observer alongside the other listeners;
    // bump the generation so any already-queued condensation rAF no-ops.
    if (_railResizeObserver) {
      try { _railResizeObserver.disconnect(); } catch (_) { /* noop */ }
      _railResizeObserver = null;
    }
    _condensationGen++;
    // U4: defensively clear any engaged magnetic flare on deactivation.
    _magneticReset();
    _activeColumn = null;
    _activeBuckets = null;
  }

  // Plan 002 U3: watch the active column's viewport intersection. When the
  // column leaves viewport (page chrome scroll, display:none via tab switch),
  // fire _deactivateRail. Single observer instance per activation, disconnected
  // on deactivate.
  function _watchColumnVisibility(column) {
    if (_columnVisibilityObserver) {
      try { _columnVisibilityObserver.disconnect(); } catch (_) { /* noop */ }
    }
    if (typeof IntersectionObserver === 'undefined') return;
    _columnVisibilityObserver = new IntersectionObserver((entries) => {
      entries.forEach(entry => {
        if (entry.target === _activeColumn && !entry.isIntersecting) {
          _deactivateRail();
        }
      });
    });
    _columnVisibilityObserver.observe(column);
  }

  // Plan 003 U3: scroll-event + rAF highlight. Replaces the previous
  // IntersectionObserver approach which silently dropped intermediate
  // letters on rapid scroll (entries fire when an element crosses the
  // rootMargin strip; multiple crossings within one frame collapse to a
  // single batched callback and the topmost-intersecting heuristic loses
  // information about the actual scroll position). The new approach reads
  // scrollTop directly every animation frame and binary-walks the cached
  // letter offsets — O(27) per scroll firing, deterministic, never skips.
  //
  // THRESHOLD: 10px below the scroll top is the activation line. Matches
  // the mockups (density.html / magnetic.html) and preserves the "active
  // = topmost visible bucket" feel of the prior rootMargin='-1px 0px -90% 0px'
  // observer config.
  const _ALPHA_HIGHLIGHT_THRESHOLD = 10;

  function _attachAlphaObserver(rail, column) {
    // Tear down any prior scroll listener before rewiring. Mirror the
    // _deactivateRail fallback (`|| window`) — a prior window-fallback
    // attach (no scroll ancestor resolved) must not leak its listener.
    if (_alphaScrollHandler) {
      const prevTarget = _alphaScrollAncestor || window;
      try { prevTarget.removeEventListener('scroll', _alphaScrollHandler); } catch (_) { /* noop */ }
    }
    // Legacy: a prior plan-002 IntersectionObserver may still be attached;
    // disconnect so we don't double-fire highlight updates.
    if (_alphaObserver) {
      try { _alphaObserver.disconnect(); } catch (_) { /* noop */ }
      _alphaObserver = null;
    }
    _alphaScrollPending = false;
    _alphaActiveRail = rail;
    _alphaScrollAncestor = _findScrollAncestor(column);
    // Cache the [data-bucket-start] marker ELEMENTS once at activation (the
    // DOM walk is the per-scroll cost worth avoiding). Their offsetTop is
    // deliberately read LIVE on every firing: under content-visibility render
    // containment (rail.css), a not-yet-rendered cell's offset contribution is
    // the contain-intrinsic-size ESTIMATE and shifts to the true value when
    // the cell first renders — an activation-time offset snapshot drifts by
    // thousands of px on a large library, highlighting a letter 2–3 behind
    // the visible content (2026-08-04 debug). Live reads share the current
    // frame's layout with scrollTop, so the comparison is always
    // self-consistent — ~27 offset reads per firing, no DOM walk.
    const markers = [];
    column.querySelectorAll('[data-bucket-start]').forEach(el => {
      markers.push([el, el.dataset.bucketStart]);
    });
    _alphaMarkers = markers;
    // The scroll handler. Module-scope reference so _deactivateRail can
    // removeEventListener it cleanly.
    _alphaScrollHandler = () => {
      if (_alphaScrollPending) return;
      _alphaScrollPending = true;
      requestAnimationFrame(() => {
        _alphaScrollPending = false;
        // _railDragging owns the indicator during scrub (U7 of plan 001).
        if (_railDragging) return;
        if (!_alphaActiveRail || !_alphaMarkers) return;
        const scrollRoot = _alphaScrollAncestor || document.scrollingElement;
        const scrollTop = (scrollRoot ? scrollRoot.scrollTop : 0) + _ALPHA_HIGHLIGHT_THRESHOLD;
        const colTop = column.offsetTop || 0;
        // Active = the marker with the greatest offset at/above the threshold
        // line; none above it (top of list) = the offset-lowest marker. A
        // max-scan rather than a sorted walk: offsets are live, so a frozen
        // sort order can't be assumed.
        let active = null, bestTop = -Infinity, firstKey = null, firstTop = Infinity;
        for (const [el, L] of _alphaMarkers) {
          const top = el.offsetTop - colTop;
          if (top < firstTop) { firstTop = top; firstKey = L; }
          if (top <= scrollTop && top > bestTop) { bestTop = top; active = L; }
        }
        if (active === null) active = firstKey;
        Array.from(_alphaActiveRail.children).forEach(child => {
          // Every mode's child carries data-bucket so the same lookup works.
          if (child.dataset && child.dataset.bucket !== undefined) {
            if (child.dataset.bucket === active) child.setAttribute('aria-current', 'true');
            else child.removeAttribute('aria-current');
          }
        });
      });
    };
    // Attach to the scroll ancestor (or window if we can't find one).
    const target = _alphaScrollAncestor || window;
    target.addEventListener('scroll', _alphaScrollHandler, { passive: true });
    // Prime the highlight once so the active letter is correct without
    // requiring the user to scroll.
    _alphaScrollHandler();
  }

  // Instant jump with convergence. Under content-visibility render containment
  // (rail.css), target.offsetTop is computed from contain-intrinsic-size
  // ESTIMATES for every not-yet-rendered cell above it; a single scrollTop
  // write therefore lands past the bucket start (~4 rows deep on a large
  // library — 2026-08-04 debug), because the browser realizes the destination
  // band at TRUE sizes right after the write and the content shifts. Re-read
  // and re-write across frames until the target's offset stabilizes. Bounded;
  // aborts on user input, when a newer jump takes over (generation), or when
  // a re-render detaches the target.
  function _settleJump(scrollRoot, col, target) {
    const gen = ++_settleJumpGen;   // a newer jump owns the scroller now
    // Abort on USER input only — scrollTop deltas can't distinguish the user
    // from Chrome's scroll anchoring, which adjusts scrollTop while the
    // destination band realizes (and would anchor the WRONG landing in place).
    // Listeners go on window: wheel/touch/pointer bubble there from any
    // scroller, and keydown never fires on an unfocused pane div.
    const evs = ['wheel', 'touchstart', 'pointerdown', 'keydown'];
    let aborted = false;
    const abort = (e) => {
      aborted = true;
      // Only a TAP (pointerdown/touchstart) arms the wrong-artist guard — a wheel
      // or key interrupt means the user took over scrolling, not selecting a row.
      const t = e && e.type;
      if (t === 'pointerdown' || t === 'touchstart') _settleAbortedAt = _nowMs();
    };
    evs.forEach(e => window.addEventListener(e, abort, { passive: true, once: true }));
    const cleanup = () => evs.forEach(e => window.removeEventListener(e, abort));
    // Watch up to a bounded window, but STOP once the landing has held flush for
    // a few consecutive frames. Exiting on a SINGLE agreeing read is unsafe: rAF
    // callbacks run before the frame's render step, so a late content-visibility
    // realization shift can land AFTER one tick that saw estimate-consistent
    // (matching) geometry. Requiring _STABLE_FRAMES consecutive flush frames
    // keeps that safety (any shift resets the counter) while ending the loop as
    // soon as content genuinely settles — instead of always running the full 20.
    // Each tick forces a synchronous getBoundingClientRect layout on the
    // content-visibility list (~15ms on a large library); holding for the full
    // window tripled the jump's main-thread cost (~160ms raw → ~500ms) and left
    // content micro-adjusting for ~333ms+ — the "chug", and the window in which a
    // fast follow-up tap lands on a row that has shifted (wrong-artist drill,
    // 2026-08-08 debug). Corrections are viewport-rect-based (target top vs
    // scroller top), exact in any offsetParent arrangement.
    let tries = 20;
    let stable = 0;
    const _STABLE_FRAMES = 3;
    const tick = () => {
      if (aborted || gen !== _settleJumpGen || !target.isConnected) { cleanup(); return; }
      const delta = target.getBoundingClientRect().top - scrollRoot.getBoundingClientRect().top;
      if (Math.abs(delta) > 1) { scrollRoot.scrollTop += delta; stable = 0; }
      else if (++stable >= _STABLE_FRAMES) { cleanup(); return; }
      if (--tries < 0) { cleanup(); return; }
      requestAnimationFrame(tick);
    };
    scrollRoot.scrollTop = target.offsetTop - ((col && col.offsetTop) || 0);
    requestAnimationFrame(tick);
  }

  // U6 + U7: wire tap-to-jump (and pointer drag in U7). One-shot wiring on
  // rail creation; rail elements are reused across re-renders so handlers
  // attached here survive sort changes. Resolves the .alpha-items-column
  // per-event since the rail's parent changes across renders of different
  // views (Artists vs Albums) — but rail elements themselves are scoped
  // per-column so this is just defensive.
  function _wireRailInteractions(rail) {
    rail.addEventListener('click', (e) => {
      // Every mode's child carries data-bucket; the selector accepts all.
      const btn = e.target.closest('[data-bucket]');
      if (!btn || btn === rail) return;
      if (btn.getAttribute('aria-disabled') === 'true') return;
      // Defensive: drag-in-progress means tap is a noisy edge case. Bail
      // (matches the U6 stale-handler discipline from finding #12).
      if (_railDragging) return;
      const key = btn.dataset.bucket;
      // U4 (2026-06-09 rail plan, R6/R9): move the accent highlight to the
      // tapped letter IMMEDIATELY — the scroll handler converges to the same
      // letter once the smooth scroll arrives, but waiting for it made taps
      // feel dead (reported on Magnetic; the shared handler fixes all modes).
      Array.from(rail.children).forEach(c => {
        if (c.dataset.bucket === key) c.setAttribute('aria-current', 'true');
        else c.removeAttribute('aria-current');
      });
      // The active column is module state, updated by _activateRail.
      const col = _activeColumn;
      const target = col && col.querySelector(`[data-bucket-start="${CSS.escape(key)}"]`);
      if (target) {
        // Instant jump (direct scrollTop, like the drag-scrub) — NOT smooth
        // scrollIntoView. Smooth animates *through* every band between here and
        // the target, forcing render of the whole traversal; on a large library
        // that's ~0.6–1s of main-thread long tasks per tap (confirmed against the
        // live instance via tools/perf/browse-bench, A/B smooth vs instant —
        // 2026-06-24). Instant renders only the destination region. _settleJump
        // converges the landing across frames (content-visibility estimates).
        const scrollRoot = _findScrollAncestor(col) || document.scrollingElement;
        if (scrollRoot) _settleJump(scrollRoot, col, target);
        else target.scrollIntoView({ block: 'start' });
      }
      // NOTE: deliberately no event.stopPropagation() — let any open overflow
      // menu close via the document-level outside-click listener.
    });

    // Plan 003 U5: magnetic mode interaction. Wired unconditionally — the
    // _magneticUpdate guard early-returns when mode-magnetic isn't on the
    // rail, so density mode pays a noop on each event but no rendering cost.
    // Mode is set at mount time per page load and never swaps mid-session.
    rail.addEventListener('mousemove', (e) => {
      if (!rail.classList.contains('mode-magnetic')) return;
      if (_railDragging) return;
      rail.classList.add('engaged');
      _magneticUpdate(e.clientY);
    });
    rail.addEventListener('mouseleave', () => {
      if (!rail.classList.contains('mode-magnetic')) return;
      _magneticReset();
    });
    rail.addEventListener('pointermove', (e) => {
      if (!rail.classList.contains('mode-magnetic')) return;
      if (e.pointerType !== 'touch') return;
      if (_railDragging) return;
      rail.classList.add('engaged');
      _magneticUpdate(e.clientY);
    });
    // U4 (2026-06-09 rail plan, R10): touch-safe flare reset. mouseleave
    // never fires for touch, so a tap/scrub used to strand the engaged
    // flare ("stuck" reports). pointerup/pointercancel/pointerleave cover
    // every exit path including interrupted gestures; wired unconditionally
    // (cheap no-op for non-magnetic modes via the class guard).
    const _magneticTouchReset = () => {
      if (rail.classList.contains('mode-magnetic')) _magneticReset();
    };
    rail.addEventListener('pointerup', _magneticTouchReset);
    rail.addEventListener('pointercancel', _magneticTouchReset);
    rail.addEventListener('pointerleave', _magneticTouchReset);

    // U7: pointer drag-scrub. Skip wiring if the browser lacks PointerEvent
    // or setPointerCapture — tap-to-jump still works (degraded fallback).
    if (!_dragSupported) return;

    rail.addEventListener('pointerdown', (e) => {
      // Close any visible transient UI that competes with the overlay.
      if (typeof hideOverflowMenu === 'function' && _overflowMenu && _overflowMenu.classList.contains('visible')) {
        hideOverflowMenu();
      }
      _railDragging = true;
      try { rail.setPointerCapture(e.pointerId); } catch (_) { /* noop */ }
      _railBounds = rail.getBoundingClientRect();
      const overlay = _ensureOverlay();
      // Glow-up U4: loupe mode restyles the SAME scrub overlay into the
      // thumb-following lens (one overlay system, per plan). CSS keys off
      // .lens (left:auto !important beats the activation-time inline left);
      // non-loupe modes clear the lens class + any inline top from a prior
      // loupe drag so the centered big-letter overlay returns intact.
      const isLoupe = rail.classList.contains('mode-loupe');
      overlay.classList.toggle('lens', isLoupe);
      if (!isLoupe) overlay.style.top = '';
      overlay.style.opacity = '1';
      _railPointerMove(e, rail);
    });
    rail.addEventListener('pointermove', (e) => {
      if (!_railDragging) return;
      _railPointerMove(e, rail);
    });
    rail.addEventListener('pointerup', () => {
      // Normal release: converge onto the last scrubbed bucket start (the
      // scrub's per-move scrollTop writes used estimate-based offsets — see
      // _settleJump). Interrupted gestures (pointercancel/lostpointercapture)
      // deliberately don't settle: don't move content under a lost pointer.
      const t = _railScrubTarget;
      cancelRailDrag();
      if (t && t.isConnected && _activeColumn) {
        const scrollRoot = _findScrollAncestor(_activeColumn) || document.scrollingElement;
        if (scrollRoot) _settleJump(scrollRoot, _activeColumn, t);
      }
    });
    rail.addEventListener('pointercancel', cancelRailDrag);
    rail.addEventListener('lostpointercapture', cancelRailDrag);
  }

  function _railPointerMove(e, rail) {
    if (!_railBounds) return;
    const children = rail.children;
    const n = children.length;
    if (!n) return;
    // Bucket-set-aware: divide the rail rect by the ACTUAL child count (was a
    // hardcoded 27), so the scrub maps onto whatever bucket set is live —
    // letters, a time ladder, or a reversed order.
    const cellH = _railBounds.height / n;
    const offset = Math.max(0, Math.min(_railBounds.height, e.clientY - _railBounds.top));
    const idx = Math.min(n - 1, Math.max(0, Math.floor(offset / cellH)));
    const btn = children[idx];
    const key = btn && btn.dataset.bucket;
    const label = (btn && btn.dataset.label) || '';
    // Column resolved from module state instead of DOM walk.
    const col = _activeColumn;
    const target = (col && key != null) ? col.querySelector(`[data-bucket-start="${CSS.escape(key)}"]`) : null;
    const scrollRoot = _findScrollAncestor(col) || document.scrollingElement;
    if (target && scrollRoot) {
      // Manual scrollTop (no smooth) — drag must feel real-time. Estimate
      // drift during the scrub is tolerable (the finger is still moving);
      // the pointerup wiring runs _settleJump on the LAST scrubbed target so
      // the release lands flush on the bucket start.
      const colTop = (col && col.offsetTop) || 0;
      scrollRoot.scrollTop = target.offsetTop - colTop;
      _railScrubTarget = target;
    }
    // The drag owns the indicator (the scroll handler defers via _railDragging),
    // so mirror the scrubbed bucket onto the rail — only for buckets with a jump
    // target (content present), matching the click handler's rule.
    if (target) {
      Array.from(children).forEach(c => {
        if (c === btn) c.setAttribute('aria-current', 'true');
        else c.removeAttribute('aria-current');
      });
    }
    if (_alphaOverlay) {
      if (_alphaOverlay.classList.contains('lens')) {
        // Loupe lens: big current label with ghosted neighbors, floating beside
        // the thumb. Labels are our own generated strings; _esc is belt-and-braces.
        const prev = idx > 0 ? (children[idx - 1].dataset.label || '') : '';
        const next = idx < n - 1 ? (children[idx + 1].dataset.label || '') : '';
        _alphaOverlay.innerHTML = '<span class="ghost">' + _esc(prev) + '</span><span class="big">' + _esc(label) + '</span><span class="ghost">' + _esc(next) + '</span>';
        const parent = _alphaOverlay.parentElement;
        const fixed = _alphaOverlay.classList.contains('no-host') || !parent || parent === document.body;
        let y = e.clientY;
        if (!fixed) y = e.clientY - parent.getBoundingClientRect().top;
        const maxY = (fixed ? window.innerHeight : parent.clientHeight) - 40;
        _alphaOverlay.style.top = Math.max(40, Math.min(maxY, y)) + 'px';
      } else {
        _alphaOverlay.textContent = label;
      }
    }
  }

  // ── Queue-append ──────────────────────────────────────────────────────────

  // Plex-player source lock (2026-08-04-002 plan U5). One client copy for
  // every rejection/guard path; the SERVER gate (409 output_source_lock on
  // both queue endpoints) is the enforcement — everything here is UX.
  const _SOURCE_LOCK_MSG = 'This output can only play Plex tracks';

  // Is queueing this track blocked by the active output's source lock? Live
  // read of the body-level render switch (set by the shared playback module
  // from the output_session channel) so guards built before an output
  // switch still decide correctly at press time. Tracks without the U4
  // plex_held flag (older payload shapes) fail open — the server decides.
  function _plexLocked(t) {
    return document.body.dataset.sourceLock === 'plex' && !!t && t.plex_held === false;
  }

  async function addTrack(trackId, title, track, sourceServerName, sourceLabel) {
    if (_config.isLocked && _config.isLocked()) { _config.toast('Queuing is paused by the host'); return; }
    // U5 click-guard: a dimmed row's tap toasts instead of POSTing (the
    // isLocked guard above is the precedent). Server gate still backstops
    // call sites that pass no track object.
    if (_plexLocked(track)) { _config.toast(_SOURCE_LOCK_MSG); return; }
    // Catalog "Play From Source…" (parity U3): re-POST the same catalog track_id
    // plus the chosen source's server_name. The server reorders the track's
    // holds so that source plays first (a preference — U9 play-time fallback
    // still applies). Absent on a plain tap = default highest-priority source.
    const body = { track_id: trackId };
    if (sourceServerName) body.source_server_name = sourceServerName;
    const [status, data] = await _api('POST', _queueEndpoint(), body);
    if (status === 423) { _config.toast('Queuing is paused by the host'); return; }
    // 409 disambiguation by detail (U5): output_source_lock (both endpoints —
    // the admin gets no bypass) means the selected output can't play this
    // track; the detail-less/duplicate 409 stays the Flood Control message
    // (2026-06-16 — guest /api/queue only, re-add of a playing/queued track).
    if (status === 409) {
      if (data && data.detail === 'output_source_lock') { _config.toast(_SOURCE_LOCK_MSG); return; }
      _config.toast("That track's already in the queue"); return;
    }
    if (status === 200) {
      if (data && data.warning === 'already_in_queue') _config.toast('Already in queue — added anyway');
      else if (sourceLabel) _config.toast(`Added from ${sourceLabel}`);
      else _config.toast('Added to queue!');
      // Hand the append receipt to the page (remove-own-queued-tracks U4).
      // The guest page persists it so it can show a remove (✕) on this entry
      // in the queue anytime before it plays; admin passes no onQueued and the
      // receipt is simply dropped (admin removes by position). Replaces the old
      // 5s undo snackbar — retraction now lives durably in the queue.
      if (data && data.entry && _config.onQueued) _config.onQueued(data.entry);
      // Surprise Me seed (2026-06-17 U5): remember this pick so a later
      // "Surprise Me" press can be seeded from what this browser has queued.
      if (track && typeof recordSurprisePick === 'function') recordSurprisePick(track);
    } else {
      _config.toast('Failed to add track');
    }
  }

  async function addAlbum(albumId, sourceServerName, sourceLabel) {
    if (_config.isLocked && _config.isLocked()) { _config.toast('Queuing is paused by the host'); return; }
    const body = { album_id: albumId };
    if (sourceServerName) body.source_server_name = sourceServerName;
    const [status, data] = await _api('POST', _queueEndpoint(), body);
    if (status === 423) { _config.toast('Queuing is paused by the host'); return; }
    // U5: an album with ZERO playable tracks on this output rejects with the
    // same shape as the per-track gate — same toast.
    if (status === 409 && data && data.detail === 'output_source_lock') {
      _config.toast(_SOURCE_LOCK_MSG); return;
    }
    if (status === 200) {
      // U5 partial add: the server enqueued only the playable subset and
      // reported the withheld count — surface both numbers.
      const filtered = (data && data.tracks_filtered) || 0;
      if (filtered > 0) {
        const total = data.tracks_added + filtered;
        _config.toast(`Added ${data.tracks_added} of ${total} — ${filtered} unavailable on this output`);
      }
      else if (sourceLabel) _config.toast(`Added from ${sourceLabel}: ${data.tracks_added} track(s)`);
      else _config.toast(`Added ${data.tracks_added} track(s) to queue!`);
      // Album batch receipt → the page stores it as one group so the whole
      // album can be removed as a unit (U4). Guest-only, as above.
      if (data && data.entries && data.entries.length && _config.onQueued) {
        _config.onQueued({ entries: data.entries });
      }
    } else {
      _config.toast('Failed to add album');
    }
  }

  // ── Track rendering ───────────────────────────────────────────────────────

  // Artist/album are PLAIN TEXT on queueable track rows (2026-06-14): the whole
  // row queues on tap, and inline name-links intercepted taps — badly on mobile
  // with long names. Navigation moved to the track kebab (Go to artist / Go to
  // album), which already carries it. Album rows / header / breadcrumb keep
  // their name-links (different renderers). See
  // docs/plans/2026-06-14-004-feat-track-row-suppress-inline-links-plan.md.
  function _trackSubHtml(t, dur) {
    return _esc(t.artist)
      + (t.album ? ' — ' + _esc(t.album) : '')
      + (dur ? ' · ' + dur : '');
  }

  function makeTrackRow(track, ctx, sources) {
    // Collected-library plan U4/U5 + 2026-06-14: the WHOLE row is the queue
    // button (everything except the kebab — the subtitle's artist/album are now
    // plain text, not links). The kebab carries navigation (Go to artist / Go
    // to album) and the Play From Source… escape hatch for duplicated tracks.
    const row = document.createElement('div');
    row.className = 'list-item track-row';
    row.dataset.trackId = track.track_id || track.id || '';
    // U5 source-lock gray-out: the row ALWAYS carries its playability class
    // (backend-independent, straight from the U4 plex_held flag); whether it
    // DIMS is decided purely by the body[data-source-lock="plex"] CSS switch,
    // so an output flip restyles live with no refetch. A missing flag (older
    // payload shapes) means no class — fail open, the server gate enforces.
    if (track.plex_held === false) row.classList.add('no-plex-hold');
    const dur = track.duration_ms ? _formatDuration(track.duration_ms) : '';
    row.innerHTML = `<div class="list-info"><div class="list-title">${_esc(track.title)}</div><div class="list-sub">${_trackSubHtml(track, dur)}</div></div><button class="kebab-btn" title="Track options" aria-haspopup="true">⋮</button>`;
    row.querySelector('.kebab-btn').addEventListener('click', (e) => {
      e.stopPropagation();
      _openSheet(_trackKebabItems(track, sources, ctx, row), e.currentTarget);
    });
    row.addEventListener('click', () => addTrack(track.track_id || track.id, track.title, track));
    _applyRatingTags(row, track);
    return row;
  }

  // ── Track ratings + tags (2026-06-26 plan U5; admin authoring added in U6) ──
  // Two global maps (track_id → stars / [tags]) fetched once at mount, mirroring
  // the _loadServerMeta / _loadPatternRules load-once idiom: rows rendered before
  // the maps land self-correct via _redecorateRatingTags on resolve. The server
  // GATES the guest responses (empty map = hidden, plan R8), so rendering needs
  // no client visibility branch — an empty map simply yields no pips/chips.
  let _ratingsMap = null;   // {track_id: stars}; null until loaded
  let _tagsMap = null;      // {track_id: [tags]}; null until loaded
  let _ratingTagPromise = null;

  function _loadRatingTagMaps() {
    if (_ratingTagPromise) return _ratingTagPromise;
    _ratingTagPromise = Promise.all([
      _api('GET', '/api/track-ratings'),
      _api('GET', '/api/track-tags'),
    ]).then(([[, ratings], [, tags]]) => {
      _ratingsMap = (ratings && typeof ratings === 'object') ? ratings : {};
      _tagsMap = (tags && typeof tags === 'object') ? tags : {};
      _redecorateRatingTags();
    }).catch(() => { _ratingsMap = _ratingsMap || {}; _tagsMap = _tagsMap || {}; });
    return _ratingTagPromise;
  }

  function _buildPips(stars) {
    const slot = document.createElement('span');
    slot.className = 'trk-rating';
    for (let i = 1; i <= 5; i++) {
      const pip = document.createElement('span');
      pip.className = 'trk-pip' + (i <= stars ? ' on' : '');
      pip.dataset.i = i;
      slot.appendChild(pip);
    }
    return slot;
  }

  function _buildTagChips(tags) {
    const box = document.createElement('div');
    box.className = 'trk-tags';
    tags.forEach(t => {
      const chip = document.createElement('span');
      chip.className = 'trk-tag';
      const tx = document.createElement('span');
      tx.className = 'trk-tag-tx';
      tx.textContent = t;          // R6: inert text, never innerHTML
      chip.appendChild(tx);
      box.appendChild(chip);
    });
    return box;
  }

  // Decorate one track row from the cached maps. Idempotent: clears prior slots
  // so the post-load redecoration (and post-edit refresh) can re-run on an
  // already-rendered row. Guests get a read-only view (pips only when rated,
  // tags only when present — blank otherwise, plan R16a). Admin (authMode
  // 'admin') gets editable pips (faint empty when unrated) + tag delete/add
  // (U6). Every authoring control stopPropagation()s so the row's
  // click-to-queue never fires.
  function _applyRatingTags(row, track) {
    const tid = (track && (track.track_id || track.id)) || row.dataset.trackId;
    if (!tid) return;
    const admin = !!(_config && _config.authMode === 'admin');

    // rating pips → trailing slot, before the kebab
    const oldR = row.querySelector(':scope > .trk-rating');
    if (oldR) oldR.remove();
    const stars = _ratingsMap ? _ratingsMap[tid] : undefined;
    const kebab = row.querySelector(':scope > .kebab-btn');
    if (admin) {
      row.insertBefore(_buildEditablePips(tid, stars || 0, row), kebab);
    } else if (stars != null) {
      row.insertBefore(_buildPips(stars), kebab);
    }

    // tag chips → under the subtitle, inside list-info
    const info = row.querySelector(':scope > .list-info');
    if (info) {
      const oldT = info.querySelector(':scope > .trk-tags');
      if (oldT) oldT.remove();
      const tags = _tagsMap ? _tagsMap[tid] : undefined;
      if (admin) {
        info.appendChild(_buildEditableTags(tid, tags || [], row));
      } else if (tags && tags.length) {
        info.appendChild(_buildTagChips(tags));
      }
    }
  }

  // ── Admin authoring (U6) ────────────────────────────────────────────────────
  async function _postRating(tid, stars) {
    const [status] = await _api('POST', '/admin/track-rating', { track_id: tid, stars });
    if (status >= 200 && status < 300) {
      _ratingsMap = _ratingsMap || {};
      if (stars > 0) _ratingsMap[tid] = stars; else delete _ratingsMap[tid];
      _highestRatedLoaded = false;  // leaderboard reorders on its next tab visit
      return true;
    }
    _config.toast('Could not save rating');
    return false;
  }

  async function _postTags(tid, tags) {
    const [status, data] = await _api('POST', '/admin/track-tags', { track_id: tid, tags });
    if (status >= 200 && status < 300) {
      const stored = (data && data.tags) || [];
      _tagsMap = _tagsMap || {};
      if (stored.length) _tagsMap[tid] = stored; else delete _tagsMap[tid];
      return true;
    }
    _config.toast('Could not save tags');
    return false;
  }

  function _buildEditablePips(tid, stars, row) {
    const slot = document.createElement('span');
    slot.className = 'trk-rating is-editable';
    const pips = [];
    for (let i = 1; i <= 5; i++) {
      const pip = document.createElement('span');
      pip.className = 'trk-pip' + (i <= stars ? ' on' : '');
      pip.dataset.i = i;
      pip.addEventListener('mouseenter', () =>
        pips.forEach((p, idx) => p.classList.toggle('hov', idx < i)));
      pip.addEventListener('click', async (e) => {
        e.stopPropagation();                       // never queue the track
        if (await _postRating(tid, i)) _applyRatingTags(row);
      });
      slot.appendChild(pip);
      pips.push(pip);
    }
    slot.addEventListener('mouseleave', () => pips.forEach(p => p.classList.remove('hov')));
    // Always render the clear button so its slot is reserved (plan U4/R9):
    // rating an unrated track — or clearing a rated one — no longer shifts the
    // row, because the ✕ footprint is present either way. is-hidden makes it
    // invisible + non-interactive (CSS) until there's a rating to clear.
    const clr = document.createElement('button');
    clr.type = 'button';
    clr.className = 'trk-clear' + (stars > 0 ? '' : ' is-hidden');
    clr.textContent = '✕';
    clr.title = 'Clear rating';
    clr.tabIndex = stars > 0 ? 0 : -1;
    clr.setAttribute('aria-hidden', stars > 0 ? 'false' : 'true');
    clr.addEventListener('click', async (e) => {
      e.stopPropagation();                          // never queue the track
      if (await _postRating(tid, 0)) _applyRatingTags(row);
    });
    slot.appendChild(clr);
    return slot;
  }

  function _buildEditableTags(tid, tags, row) {
    const box = document.createElement('div');
    box.className = 'trk-tags';
    tags.forEach(t => {
      const chip = document.createElement('span');
      chip.className = 'trk-tag';
      const tx = document.createElement('span');
      tx.className = 'trk-tag-tx';
      tx.textContent = t;                          // R6: inert
      chip.appendChild(tx);
      const del = document.createElement('button');
      del.type = 'button';
      del.className = 'trk-tag-del';
      del.textContent = '✕';
      del.title = 'Delete tag';
      del.addEventListener('click', async (e) => {
        e.stopPropagation();
        if (await _postTags(tid, tags.filter(x => x !== t))) _applyRatingTags(row);
      });
      chip.appendChild(del);
      box.appendChild(chip);
    });
    const add = document.createElement('button');
    add.type = 'button';
    add.className = 'trk-tag-add';
    add.textContent = '＋ tag';
    add.addEventListener('click', (e) => {
      e.stopPropagation();
      const inp = document.createElement('input');
      inp.type = 'text';
      inp.className = 'trk-tag-input';
      inp.maxLength = 40;
      inp.placeholder = 'new tag…';
      add.replaceWith(inp);
      inp.focus();
      inp.addEventListener('click', (ev) => ev.stopPropagation());
      let done = false;
      const commit = async () => {
        if (done) return;
        done = true;
        const v = inp.value.trim();
        if (v && await _postTags(tid, tags.concat([v]))) _applyRatingTags(row);
        else _applyRatingTags(row);                // empty/failed → just re-render
      };
      inp.addEventListener('keydown', (ev) => {
        ev.stopPropagation();
        if (ev.key === 'Enter') commit();
        else if (ev.key === 'Escape') { done = true; _applyRatingTags(row); }
      });
      inp.addEventListener('blur', commit);
    });
    box.appendChild(add);
    return box;
  }

  function _redecorateRatingTags() {
    document.querySelectorAll('.list-item.track-row[data-track-id]')
      .forEach(row => _applyRatingTags(row));
  }

  // Type-qualified source label for catalog "Play From Source…" entries (parity
  // U3): "Plex: Home" / "Jellyfin: Den" — disambiguates same-named servers that
  // a bare name would collide. Falls back to the bare name when no type is
  // present (native multi-Plex sources carry no source_type).
  const _SOURCE_TYPE_LABELS = { plex: 'Plex', jellyfin: 'Jellyfin', local: 'Local' };
  function _typeLabel(t) {
    return _SOURCE_TYPE_LABELS[t] || (t ? t.charAt(0).toUpperCase() + t.slice(1) : '');
  }
  function _sourceLabel(s) {
    const name = s.server_name || 'Server';
    const tl = _typeLabel(s.source_type);
    return tl ? `${tl}: ${name}` : name;
  }

  function _trackKebabItems(t, sources, ctx, row) {
    const items = [];
    const pane = _paneFor(row);
    const locked = _config.isLocked && _config.isLocked();
    if (t.artist) {
      items.push({ label: 'Go to artist', action: () => browseToArtist(t.artist, { pane }) });
    }
    if (t.album) {
      items.push({
        label: 'Go to album',
        // Self-link semantics (R1, refined in U6) + albumless/stale-meta
        // fallback contract: no id → entry disabled, never a dead drill.
        disabled: !t.album_id || !!(ctx && ctx.currentAlbumId && t.album_id === ctx.currentAlbumId),
        action: () => browseToAlbum(t.album_id, t.album, { pane }),
      });
    }
    // Per-source override. CATALOG mode (parity U3): the merged track carries its
    // own holds on `t.sources` = [{server_name, source_type}]; re-POST the single
    // catalog track_id plus the chosen source (a preference — U9 fallback still
    // applies), labelled by type. NATIVE mode: each `sources` entry carries its
    // own distinct per-server track (deduplicateTracks-built); enqueue that copy.
    // U5: while a Plex-player output dims this row, its queue actions in the
    // sheet disable too (the `disabled:` flag precedent); navigation (Go to
    // artist/album) and ratings stay live. Evaluated ONCE at sheet-build
    // time (S-6: the once-computed srcLocked serves the inner sheet entries
    // too — a locked outer entry never opens the inner sheet anyway, and
    // the addTrack press-time guard backstops any flip in between).
    const srcLocked = _plexLocked(t);
    const catalogSources = Array.isArray(t.sources) ? t.sources : null;
    if (catalogSources && catalogSources.length > 1) {
      items.push({
        label: 'Play from source…',
        action: () => _openSheet(catalogSources.map(s => ({
          label: _sourceLabel(s),
          disabled: locked || srcLocked,
          action: () => addTrack(t.track_id || t.id, t.title, t, s.server_name, _sourceLabel(s)),
        })), row.querySelector('.kebab-btn')),
        disabled: srcLocked,
      });
    } else if (sources && sources.length > 1) {
      items.push({
        label: 'Play from source…',
        action: () => _openSheet(sources.map(s => ({
          label: `Play from ${s.server_name || 'Server'}`,
          disabled: locked || srcLocked,
          action: () => addTrack(s.track.track_id || s.track.id, s.track.title, s.track),
        })), row.querySelector('.kebab-btn')),
        disabled: srcLocked,
      });
    }
    // Admin curation (plan U5, R4/R7/R8): remove this track from Most Played.
    // Scoped to the Most Played context (ctx.mostPlayed) so it never appears on
    // ordinary browse rows. Inline two-step confirm (a second sheet), never a
    // native window.confirm — it deletes the accumulated count.
    if (_config.authMode === 'admin' && ctx && ctx.mostPlayed) {
      items.push({
        label: 'Remove from Most Played',
        action: () => _openSheet([{
          label: `Confirm: remove “${t.title}” (clears its count)`,
          action: () => _removeFromMostPlayed(t, row),
        }], row.querySelector('.kebab-btn')),
      });
    }
    return items;
  }

  // ── Source priority + cross-server-only track dedup ──────────────────────
  // Collected-library plan U3. MIRRORED CONTRACT with _server_rank_key /
  // _srv_rank in app/api/guest.py (vector test:
  // tests/test_api_guest.py::test_server_rank_vectors): owned servers
  // first, known-unowned next, unknown-ownership next, names absent from
  // the meta last; alphabetical within each band. Change semantics there
  // and here in lockstep.

  let _serverMeta = null;   // [{name, owned|null}] — null until loaded

  async function _loadServerMeta() {
    try {
      const resp = await fetch('/api/servers');
      if (!resp.ok) return;
      _serverMeta = await resp.json();
    } catch (_) { /* no meta → alphabetical-only ranking still works */ }
  }

  function _rankServerNames(names) {
    const meta = new Map();
    (_serverMeta || []).forEach(s => meta.set(s.name, s.owned));
    const band = (n) => {
      if (!meta.has(n)) return 3;
      const o = meta.get(n);
      return o === true ? 0 : (o === false ? 1 : 2);
    };
    return [...names].sort((a, b) => {
      const d = band(a) - band(b);
      if (d) return d;
      return (a || '').toLowerCase().localeCompare((b || '').toLowerCase());
    });
  }

  // Multi-disc fix (2026-06-11): disc+track in the key keeps same-title
  // tracks on different discs as distinct rows IN ALBUM ORDER (identical
  // tracklists like 2-disc "Complete" editions used to regroup adjacently
  // — Intro, Intro, Believe, Believe). Cross-server copies of the SAME
  // disc+track still collapse. Fields absent (older payload shapes) →
  // defaults reproduce the old key exactly. Extracted so the search broad
  // tier (Tier 2) can dedup its rows against the already-rendered Tier-1
  // tracks using the EXACT same identity, not a divergent id heuristic.
  function _trackDedupKey(t) {
    return `${(t.title || '').toLowerCase()}|${(t.artist || '').toLowerCase()}|${(t.album || '').toLowerCase()}|${t.disc_number || 1}|${t.track_number != null ? t.track_number : ''}`;
  }

  function deduplicateTracks(tracks) {
    // R3: collapse ACROSS servers only — copies that repeat on a single
    // server all survive. Returns row descriptors: {track, sources} where
    // track is one of the PRIORITY server's copies and sources lists one
    // representative copy per server, priority-ordered (kebab's Play From
    // Source…). No auto source prompt anywhere (R6).
    const byKey = new Map();
    const orderKeys = [];
    tracks.forEach(t => {
      const key = _trackDedupKey(t);
      if (!byKey.has(key)) { byKey.set(key, new Map()); orderKeys.push(key); }
      const srv = t.server_name || '';
      const perSrv = byKey.get(key);
      if (!perSrv.has(srv)) perSrv.set(srv, []);
      perSrv.get(srv).push(t);
    });
    const rows = [];
    orderKeys.forEach(key => {
      const perSrv = byKey.get(key);
      const names = _rankServerNames([...perSrv.keys()]);
      const sources = names.map(n => ({ server_name: n, track: perSrv.get(n)[0] }));
      perSrv.get(names[0]).forEach(copy => rows.push({ track: copy, sources }));
    });
    return rows;
  }

  function makeTrackRowMulti(rowDesc, ctx) {
    // Collected-library plan U3: the row renders/queues the priority copy
    // outright; rowDesc.sources feeds the kebab's Play From Source… (U4).
    // The old per-row source picker is gone (R6 — no unsolicited prompts).
    return makeTrackRow(rowDesc.track, ctx, rowDesc.sources);
  }

  function renderTracksDeduped(tracks, el, ctx) {
    deduplicateTracks(tracks).forEach(rowDesc => el.appendChild(makeTrackRowMulti(rowDesc, ctx)));
  }

  // ── Overflow menu (album-level "Queue album" with source picker) ──────────

  let _overflowOverlay = null;
  let _overflowMenu = null;
  let _overflowTrigger = null;  // element that opened the menu — focus returns here on close

  // ── Parametric bottom sheet (collected-library plan U4) ──────────────────
  // ONE sheet for every contextual menu: track kebabs, release kebabs, the
  // album header ⋮. Reuses the template's #overflow-menu/#overflow-overlay
  // chrome; overlay tap, Escape, and focus-return wiring live in
  // _wireOverflowMenu exactly as before.

  function _openSheet(items, triggerEl) {
    if (!_overflowMenu || !items || !items.length) return;
    _overflowTrigger = triggerEl || null;
    const area = _overflowMenu.querySelector('#queue-album-area');
    if (!area) return;
    area.innerHTML = '';
    items.forEach(it => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.textContent = it.label;
      if (it.disabled) {
        btn.disabled = true;
        btn.classList.add('sheet-disabled');
      } else {
        btn.addEventListener('click', () => { hideOverflowMenu(); it.action(); });
      }
      area.appendChild(btn);
    });
    _overflowOverlay.classList.add('visible');
    _overflowMenu.classList.add('visible');
    const first = area.querySelector('button:not([disabled])');
    if (first) first.focus();
  }

  function _albumKebabBtn(album, currentArtistNorm) {
    const btn = document.createElement('button');
    btn.className = 'kebab-btn';
    btn.title = 'Release options';
    btn.setAttribute('aria-haspopup', 'true');
    btn.textContent = '⋮';
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      _openSheet(_albumKebabItems(album, btn, currentArtistNorm), btn);
    });
    return btn;
  }

  function _albumKebabItems(album, anchor, currentArtistNorm) {
    const locked = _config.isLocked && _config.isLocked();
    const sources = Array.isArray(album.sources) ? album.sources : [];
    const multi = sources.length > 1;
    // CATALOG mode (parity U3): catalog album sources carry a source_type. The
    // catalog enqueue addresses by catalog IDENTITY (album.id), NOT the provider
    // album id in sources[].album_id (that key belongs to a single backend and
    // the catalog branch can't resolve it). Native mode keeps addressing by the
    // chosen server's provider album id, as before.
    const isCatalog = sources.some(s => s.source_type);
    const items = [{
      label: 'Queue release',
      disabled: locked,
      // R4 (native): deduplicated releases queue the priority source's id+server;
      // absent/single sources (incl. appears_on dict rows, which carry no
      // sources at all) queue exactly as before. Catalog: queue the identity,
      // default source = highest priority (no explicit pick).
      action: () => (isCatalog
        ? addAlbum(album.id, null)
        : (multi
            ? addAlbum(sources[0].album_id, sources[0].server_name)
            : addAlbum(album.id, null))),
    }];
    if (album.artist) {
      items.push({
        label: 'Go to artist',
        // U6: disabled inside that artist's own drill (same test as the
        // greyed name link).
        disabled: !!(currentArtistNorm
          && _normalizeName(album.artist || '') === currentArtistNorm),
        action: () => browseToArtist(album.artist, { pane: _paneFor(anchor) }),
      });
    }
    if (multi) {
      items.push({
        label: 'Play from source…',
        action: () => _openSheet(sources.map(s => ({
          label: isCatalog ? _sourceLabel(s) : `Play from ${s.server_name || 'Server'}`,
          disabled: locked,
          // Catalog: identity + chosen source (server-side holds reorder).
          // Native: the chosen server's provider album id.
          action: () => (isCatalog
            ? addAlbum(album.id, s.server_name, _sourceLabel(s))
            : addAlbum(s.album_id, s.server_name)),
        })), anchor),
      });
    }
    return items;
  }

  // Detail kebab: distinct performers shown inline before spilling into a
  // "More artists…" submenu (2026-06-20 release-kebab-nav U2). Most releases are
  // 1; only big compilations exceed this.
  const KEBAB_ARTIST_INLINE_CAP = 6;
  // Per-page size of the "More artists…" sub-sheet (2026-06-20). The sheet has no
  // height cap / scroll, so a Nuggets-sized VA comp dumped into one sheet runs off
  // the page — paginate instead.
  const ARTIST_PAGE_SIZE = 7;

  // Paginate a list of sheet items into pages of `pageSize`; each non-final page
  // ends with a `label` entry that opens the next page in the same sheet, so an
  // arbitrarily long list never overflows the height-uncapped overflow-menu.
  // Returns the first page's items.
  function _paginateSheet(allItems, pageSize, label, triggerEl) {
    const pageAt = (start) => {
      const page = allItems.slice(start, start + pageSize);
      if (start + pageSize < allItems.length) {
        page.push({ label, action: () => _openSheet(pageAt(start + pageSize), triggerEl) });
      }
      return page;
    };
    return pageAt(0);
  }

  function showOverflowMenu(albumId, triggerEl) {
    // Album header ⋮, re-expressed in the sheet language (plan U4). Reads
    // sources / performers / year from the trigger button itself (per-button
    // capture — avoids the stale-data race across concurrent showAlbumTracks).
    const sources = (triggerEl && Array.isArray(triggerEl._sources)) ? triggerEl._sources : [];
    const artists = (triggerEl && Array.isArray(triggerEl._artists)) ? triggerEl._artists : [];
    const year = triggerEl && triggerEl._year;
    const locked = _config.isLocked && _config.isLocked();
    const ranked = sources.length ? _rankServerNames(sources) : [];
    const items = [{
      label: 'Queue release',
      disabled: locked,
      // R4: deduplicated releases queue from the priority server; a
      // single-source album passes no filter (identical behavior).
      action: () => addAlbum(albumId, ranked.length > 1 ? ranked[0] : null),
    }];
    // Navigation (U2): one "Go to artist — Name", or several inline + a
    // "More artists…" submenu for compilations; then "Go to year". Pane resolved
    // once so search-reached releases drill in place (matches the subtitle link).
    const pane = _paneFor(triggerEl);
    const artistItem = (name) => ({
      label: `Go to artist — ${name}`,
      action: () => browseToArtist(name, { pane }),
    });
    if (artists.length === 1) {
      items.push(artistItem(artists[0]));
    } else if (artists.length > 1) {
      if (artists.length <= KEBAB_ARTIST_INLINE_CAP) {
        artists.forEach(n => items.push(artistItem(n)));
      } else {
        artists.slice(0, KEBAB_ARTIST_INLINE_CAP).forEach(n => items.push(artistItem(n)));
        // Remaining performers paginate 7-per-page (recurring "More artists…")
        // so a Nuggets-sized comp never overflows the uncapped sheet.
        const rest = artists.slice(KEBAB_ARTIST_INLINE_CAP).map(artistItem);
        items.push({ label: 'More artists…', action: () => _openSheet(_paginateSheet(rest, ARTIST_PAGE_SIZE, 'More artists…', triggerEl), triggerEl) });
      }
    }
    if (year) {
      items.push({ label: `Go to year — ${year}`, action: () => browseToYear(year) });
    }
    if (ranked.length > 1) {
      items.push({
        label: 'Play from source…',
        action: () => _openSheet(ranked.map(n => ({
          label: `Play from ${n}`,
          disabled: locked,
          action: () => addAlbum(albumId, n),
        })), triggerEl),
      });
    }
    _openSheet(items, triggerEl);
  }

  function hideOverflowMenu() {
    _overflowOverlay.classList.remove('visible');
    _overflowMenu.classList.remove('visible');
    if (_overflowTrigger && typeof _overflowTrigger.focus === 'function') {
      _overflowTrigger.focus();
    }
    _overflowTrigger = null;
  }

  function _wireOverflowMenu() {
    _overflowOverlay = document.getElementById('overflow-overlay');
    _overflowMenu = document.getElementById('overflow-menu');
    if (!_overflowOverlay || !_overflowMenu) return;
    const closeBtn = document.getElementById('close-menu-btn');
    _overflowOverlay.addEventListener('click', hideOverflowMenu);
    if (closeBtn) closeBtn.addEventListener('click', hideOverflowMenu);
    // Item actions are wired directly per-button by _openSheet (plan U4) —
    // the old data-action delegation is gone with the picker era.
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && _overflowMenu.classList.contains('visible')) {
        hideOverflowMenu();
      }
    });
  }

  // ── Navigation context & state snapshots (2026-06-10 nav plan U2) ─────────
  // Honest back-out. Every drill carries a per-call nav descriptor
  // {el, origin, crumbs, reenter} built at drill time — per-pane closures,
  // because the search pane and a browse pane can hold independent drills
  // simultaneously, so module-global trail state would lie. Top-level list
  // views snapshot scroll position on drill-out and restore on return.
  // U3 renders the wayfinding bar from the same descriptor; until then the
  // legacy back-btn rows consume it (with honest labels/targets).

  const _navSnapshots = {};   // viewKey -> {scroll}

  function _scrollOwner(el) {
    if (!el) return null;
    const o = getComputedStyle(el).overflowY;
    if (o === 'auto' || o === 'scroll') return el;
    return _findScrollAncestor(el);
  }

  function _captureScroll(elId, key) {
    const el = document.getElementById(elId);
    const owner = _scrollOwner(el);
    _navSnapshots[key] = { scroll: owner ? owner.scrollTop : (window.scrollY || 0) };
  }

  function _restoreScroll(elId, key) {
    const snap = _navSnapshots[key];
    if (!snap) return;
    const el = document.getElementById(elId);
    const owner = _scrollOwner(el);
    // Shorter-than-before lists are safe: browsers clamp scrollTop.
    if (owner) owner.scrollTop = snap.scroll;
    else window.scrollTo(0, snap.scroll);
  }

  // Drills into the artists/albums panes bump these; the load* completions
  // re-check before rendering so a slow first fetch can't clobber a
  // jump-initiated drill view (plan U2 review fix — those completions sat
  // outside the generation system).
  let _artistsPaneEpoch = 0;
  let _albumsPaneEpoch = 0;

  async function _restoreArtistsView() {
    _artistAlbumsGen++; _albumTracksGen++; _artistsItemsGen++;
    // External jumps can land before the tab was ever visited — fall back
    // to a fresh load when there's no cached roster to re-render.
    if (!artistsData) { await loadArtists(); return; }
    await renderArtistsList();
    _restoreScroll('artists-list', 'artists');
  }

  async function _restoreAlbumsView() {
    _albumTracksGen++; _albumsItemsGen++;
    if (!albumsData) { await loadAlbums(); return; }
    await renderAlbumsList();
    _restoreScroll('albums-list', 'albums');
  }

  function _restoreSearchView() {
    _albumTracksGen++; _artistAlbumsGen++;
    if (lastSearchData) renderSearchResults(lastSearchData);
    else document.getElementById('search-results').innerHTML = '';
    _restoreScroll('search-results', 'search');
  }

  function _restoreYearsView() {
    _albumTracksGen++; _yearAlbumsGen++;
    // yearsData survives back-out now (U2): no refetch, position restored.
    if (!yearsData) { loadYears(); return; }
    renderYearsList();
    _restoreScroll('years-list', 'years');
  }

  function _restoreGenresView() {
    _albumTracksGen++;
    if (_genreBrowser && _genreBrowser.hasData()) _genreBrowser.restore();
    else loadGenres();
    _restoreScroll('genres-list', 'genres');
  }

  // ── Wayfinding bar (2026-06-10 nav plan U3; locked design: mockup B) ─────
  // One slim sticky row per drilled view: "‹ [origin] | crumb › crumb".
  // Origin = the surface the user actually came from (accent, tappable);
  // trail = drill path, current crumb bright and inert, earlier crumbs
  // tappable. On narrow widths an overflowing trail folds everything
  // before the current crumb into a tappable "…" that reveals it again.

  function _wayfindBar(origin, crumbs) {
    const bar = document.createElement('div');
    bar.className = 'wayfind-bar';
    const back = document.createElement('span');
    back.className = 'wf-origin';
    back.textContent = `‹ ${origin.label}`;
    back.addEventListener('click', origin.onTap);
    bar.appendChild(back);
    if (crumbs && crumbs.length) {
      const div = document.createElement('span');
      div.className = 'wf-div';
      div.textContent = '|';
      bar.appendChild(div);
      const trail = document.createElement('span');
      trail.className = 'wf-trail';
      const here = crumbs[crumbs.length - 1];
      const renderTrail = (collapsed) => {
        trail.innerHTML = '';
        const items = collapsed
          ? [{ label: '…', onTap: () => renderTrail(false), ellipsis: true }, here]
          : crumbs;
        items.forEach((c, i) => {
          if (i) {
            const sep = document.createElement('span');
            sep.className = 'wf-sep';
            sep.textContent = '›';
            trail.appendChild(sep);
          }
          const span = document.createElement('span');
          const isHere = c === here;
          span.className = 'wf-crumb' + (isHere ? ' wf-here' : '');
          span.textContent = c.label;
          if (c.onTap && !isHere) span.addEventListener('click', c.onTap);
          trail.appendChild(span);
        });
      };
      renderTrail(false);
      // Fold detection (2026-06-11 eager-collapse fix): look only at
      // NON-current crumbs, with real slack. The current crumb's own
      // ellipsization is no reason to fold — collapsing the crumbs before
      // it can't give it more room, it just erases short artist names
      // ("M83" → "…") for zero gained pixels. With .wf-trail at flex:1
      // the spans only ellipsize under a true width deficit, so a starved
      // earlier crumb is now an honest signal.
      if (crumbs.length >= 2) requestAnimationFrame(() => {
        if (!bar.isConnected) return;
        const starved = [...trail.querySelectorAll('.wf-crumb:not(.wf-here)')]
          .some(s => s.scrollWidth > s.clientWidth + 8);
        if (starved) renderTrail(true);
      });
      bar.appendChild(trail);
    }
    // Guest's #search-bar is itself sticky (top:0) in the same scroller
    // (plan U3 review fix): offset this bar below it so the two never
    // overlap while scrolling a drilled search view.
    requestAnimationFrame(() => {
      if (!bar.isConnected) return;
      const sb = document.getElementById('search-bar');
      if (sb && sb.offsetHeight && bar.closest('#search-view, #search-results')) {
        bar.style.top = sb.offsetHeight + 'px';
      }
    });
    return bar;
  }

  // ── Cross-surface navigation API (plan U2/U4; consumed by pages in U5) ───
  // Resolution order (credits-consistent): explicit id → roster match by
  // the normalize twin → credit: appearances probe → toast notice. The API
  // owns roster loading: a fresh page's first Now Playing tap must not
  // depend on the Artists tab having been visited.

  // Both pages carry the same shared tab strip; switching panes through
  // the page-owned tab buttons reuses each page's existing view wiring
  // (incl. guest's BROWSE_VIEWS gate) without new per-page code.
  function _showPane(viewId) {
    const btn = document.querySelector(`.tab[data-view="${viewId}"]`);
    if (btn) btn.click();
  }

  async function _resolveArtist(name) {
    if (!artistsData) {
      const [, data] = await _api('GET', '/api/browse/artists');
      if (data && data.length) artistsData = data;
    }
    const norm = _normalizeName(name || '');
    const match = (artistsData || []).find(a => _normalizeName(a.title) === norm);
    if (match) return { id: match.id, title: match.title };
    // Compilation-credited acts aren't in the roster (VA gate) — probe
    // appearances BEFORE drilling so a dead name gets a notice, never an
    // empty "No releases." view.
    const creditId = 'credit:' + encodeURIComponent(name || '');
    const [, albums] = await _api('GET', `/api/browse/artists/${encodeURIComponent(creditId)}/albums`);
    if (albums && albums.length) return { id: creditId, title: name };
    return null;
  }

  async function browseToArtist(name, opts = {}) {
    const extOrigin = opts.origin || null;
    if (opts.id) {
      _showPane('artists-view');
      showArtistAlbums(opts.id, name, 'artists', extOrigin);
      return true;
    }
    const hit = await _resolveArtist(name);
    if (!hit) { _config.toast('Not in your library'); return false; }
    if (opts.pane === 'search') {
      // Tap originated inside search results: drill in place (U4).
      showArtistAlbums(hit.id, hit.title, 'search');
    } else {
      _showPane('artists-view');
      showArtistAlbums(hit.id, hit.title, 'artists', extOrigin);
    }
    return true;
  }

  function browseToAlbum(albumId, title, opts = {}) {
    if (!albumId) { _config.toast('Not in your library'); return false; }
    if (opts.pane === 'search') {
      showAlbumTracks(albumId, title, 'search');
      return true;
    }
    _showPane('albums-view');
    const origin = opts.origin || { label: 'Albums', jump: _restoreAlbumsView };
    _albumsPaneEpoch++;
    _captureScroll('albums-list', 'albums');
    showAlbumTracks(albumId, title, null, {
      el: document.getElementById('albums-list'),
      origin,
      // External jumps show where in the app the user landed (R7).
      crumbs: opts.origin ? [{ label: 'Browse', jump: () => _restoreAlbumsView() }] : [],
      reenter: null,
    });
    return true;
  }

  // Year drill from a release subtitle (2026-06-20): a year is a top-level
  // browse axis, so the tap switches to the Years pane and opens that year's
  // releases. Await the years cache first so the tab's own loadYears() resolves
  // synchronously (cached branch) and can't clobber the drill with the chip
  // list — the same cold-jump race browseToArtist sidesteps via _resolveArtist's
  // prefetch. loadYears lacks an epoch guard, so the await is load-bearing.
  async function browseToYear(year) {
    if (!year) { return false; }
    if (!yearsData) { await loadYears(); }
    _showPane('years-view');
    showYearAlbums(year);
    return true;
  }

  // ── Name links in rows (2026-06-10 nav plan U4) ──────────────────────────
  // Artist/release text gets its own hit zone (R2: stopPropagation so the
  // row's own gestures keep working). Pane is decided at tap time from
  // where the row lives: search rows drill in place, everything else uses
  // the browse panes.

  function _paneFor(rowEl) {
    return (rowEl.closest && rowEl.closest('#search-results')) ? 'search' : 'browse';
  }

  function _wireNameLinks(row, info) {
    // Self-link greying (collected-library plan U6, R1): a link that would
    // lead to the view the user is already in renders muted with NO
    // listener — the tap falls through to the row's own action (open the
    // album / queue the track). "If they wanted to get where they already
    // were, they'd just stay put."
    const grey = (el) => {
      el.classList.remove('name-link');
      el.classList.add('name-link-self');
    };
    const a = row.querySelector('.nl-artist');
    if (a) {
      const selfArtist = !!(info.currentArtistNorm
        && _normalizeName(info.artist || '') === info.currentArtistNorm);
      if (selfArtist) grey(a);
      else a.addEventListener('click', (e) => {
        e.stopPropagation();
        browseToArtist(info.artist, { pane: _paneFor(row) });
      });
    }
    const al = row.querySelector('.nl-album');
    if (al) {
      const selfAlbum = !!(info.currentAlbumId && info.albumId === info.currentAlbumId);
      if (selfAlbum) grey(al);
      else al.addEventListener('click', (e) => {
        e.stopPropagation();
        browseToAlbum(info.albumId, info.album, { pane: _paneFor(row) });
      });
    }
  }

  // ── Chunked, non-blocking list render (2026-06-24 browse-load-freeze fix) ───
  // The cross-server browse index returns the FULL catalog instantly, so the old
  // single synchronous `forEach` over every artist/album blocked the main thread
  // for seconds on big libraries (frozen tab, dead spinner — masked before the
  // index when the wait was an async Plex fan-out). _renderCellsChunked builds
  // cells in time-budgeted slices, yielding to the browser between slices via
  // requestAnimationFrame so paint + input stay live. The first slice runs
  // synchronously for an instant first paint; the rest stream across frames.
  // Full DOM is kept (plan R12/R13 — no node-removing windowing), so the alpha
  // rail's per-cell anchors and scroll geometry are unaffected — onComplete fires
  // once every marker exists. Returns a Promise that resolves at completion so
  // `await render…()` callers (e.g. scroll restore on back-nav) still wait for the
  // full list. isStale() lets a superseding render (sort flip, drill-in, tab
  // switch) abort an in-flight build cleanly.
  const _RENDER_FRAME_BUDGET_MS = 10;

  function _renderCellsChunked(column, count, buildCell, isStale, onComplete) {
    return new Promise((resolve) => {
      let i = 0;
      const _now = () => (typeof performance !== 'undefined' ? performance.now() : Date.now());
      const pump = () => {
        const start = _now();
        const frag = document.createDocumentFragment();
        while (i < count) {
          frag.appendChild(buildCell(i));
          i++;
          // One cell is always built before the clock is read, so a slow single
          // build can never stall progress (and the loop can't spin doing zero).
          if (_now() - start >= _RENDER_FRAME_BUDGET_MS) break;
        }
        column.appendChild(frag);
      };
      const finish = () => { if (!isStale()) onComplete(); resolve(); };
      pump();                              // first slice synchronous → instant paint
      if (i >= count) { finish(); return; }
      const step = () => {
        if (isStale()) { resolve(); return; }   // a newer render claimed the pane
        pump();
        if (i < count) requestAnimationFrame(step);
        else finish();
      };
      requestAnimationFrame(step);
    });
  }
  if (typeof window !== 'undefined') window.__jpRenderCellsChunked = _renderCellsChunked;

  // One delegated click handler per top-level Artists/Albums list column, in
  // place of the ~2 listeners-per-row the inline builders used to attach (the
  // throughput half of the 2026-06-24 fix — N rows no longer cost 2N closures +
  // addEventListener calls). Each row stashes its data object on `_jpItem`; the
  // handler resolves the clicked affordance (kebab → sheet, artist name-link →
  // drill, row → open). Scoped to these two inline LIST builders only — the
  // shared drill-in / search / tile builders keep their own per-cell wiring, so
  // this doesn't touch other surfaces. Tiles keep their own handlers too. One
  // listener per column lifetime (each render rebuilds the column fresh).
  function _wireListDelegation(column, view) {
    column.addEventListener('click', (e) => {
      const row = e.target.closest('.list-item');
      if (!row || !column.contains(row)) return;
      const item = row._jpItem;
      if (!item) return;
      // A tap that interrupted an in-flight rail jump lands on a row still moving
      // under the finger — every affordance on it (open, kebab, name-link) would
      // act on the wrong item. Swallow this tap; the next one hits settled content.
      if (_tapInterruptedJump()) return;
      const kebab = e.target.closest('.kebab-btn');
      if (kebab) { _openSheet(_albumKebabItems(item, kebab, null), kebab); return; }
      const nameLink = e.target.closest('.nl-artist');
      if (nameLink) { browseToArtist(item.artist, { pane: _paneFor(row) }); return; }
      if (view === 'artists') showArtistAlbums(item.id, item.title);
      else showAlbumTracks(item.id, item.title, 'albums');
    });
  }

  // ── Artists view ──────────────────────────────────────────────────────────

  let artistsData = null;
  let artistsSort = 'alpha_asc';

  // Onboarding & scan empty states (plan U15/R19/R20). When a top-level browse
  // list comes back empty, the message depends on WHY: no sources connected at
  // all (ask the host to connect one), a first scan still building the library,
  // or a finished scan that found nothing — three distinct states, not one
  // generic "nothing here". Falls back to the plain "No X found." text if the
  // status probe fails, so an empty list is never a dead end.
  async function _renderBrowseEmptyState(el, noun) {
    let status = null;
    try { [, status] = await _api('GET', '/api/scan-status'); } catch (_) { /* fall through to plain */ }
    let msg;
    if (status && status.sources === 0) {
      msg = 'No music sources connected yet. Ask the host to connect one in Setup → Libraries.';
    } else if (status && status.scanning) {
      msg = 'Your library is being prepared… this can take a minute. Check back shortly.';
    } else if (status) {
      msg = 'No music found.';
    } else {
      msg = 'No ' + noun + ' found.';
    }
    el.innerHTML = '<div class="loading">' + _esc(msg) + '</div>';
  }

  async function loadArtists() {
    if (artistsData) { return renderArtistsList(); }
    const epoch = _artistsPaneEpoch;
    const el = document.getElementById('artists-list');
    el.innerHTML = '<div class="loading">Loading…</div>';
    const [, data] = await _api('GET', '/api/browse/artists');
    if (epoch !== _artistsPaneEpoch) return;   // a drill claimed the pane mid-fetch
    if (!data || !data.length) { return _renderBrowseEmptyState(el, 'artists'); }
    artistsData = data;
    return renderArtistsList();
  }

  function renderArtistsList() {
    const el = document.getElementById('artists-list');
    el.innerHTML = '';
    const sortRow = createSortControl({
      id: 'artists-sort',
      options: [['alpha_asc', 'A → Z'], ['alpha_desc', 'Z → A'], ['popular', 'Most Played']],
      value: artistsSort,
      onChange: async (v) => { artistsSort = v; await renderArtistsItems(); },
    });
    el.appendChild(sortRow);
    const itemsDiv = document.createElement('div');
    itemsDiv.id = 'artists-items';
    el.appendChild(itemsDiv);
    return renderArtistsItems();
  }

  let _artistsItemsGen = 0;

  async function renderArtistsItems() {
    if (_railDragging) cancelRailDrag();
    const el = document.getElementById('artists-items');
    if (!el || !artistsData) return;
    const gen = ++_artistsItemsGen;
    el.innerHTML = '';
    const wrapper = document.createElement('div');
    wrapper.className = 'list-with-rail';
    wrapper.dataset.sort = artistsSort;
    const column = document.createElement('div');
    column.className = 'alpha-items-column';
    wrapper.appendChild(column);
    el.appendChild(wrapper);
    let sorted;
    if (artistsSort === 'popular') {
      const [, counts] = await _api('GET', '/api/play-counts?type=artist');
      if (_artistsItemsGen !== gen) return;
      const countMap = counts ? Object.fromEntries(counts.map(r => [r.entity_id, r.count])) : {};
      // Most-played plan U2 (R4): prune 0-play entries — a leaderboard,
      // not a reordering of the whole library.
      sorted = artistsData
        .filter(a => (countMap[a.title] || 0) > 0)
        .sort((a, b) => (countMap[b.title] || 0) - (countMap[a.title] || 0));
      if (!sorted.length) {
        column.innerHTML = '<div class="loading">No plays yet.</div>';
        _deactivateRail();
        return;
      }
    } else {
      sorted = applySort(artistsData, artistsSort, 'title');
    }
    // Sort-aware rail: resolve the dimension for this sort (letters / time / none).
    const dim = resolveRailDimension('artists', artistsSort);
    const bucketData = dim ? computeBuckets(sorted, dim, 'title', _alphaConfig('artists')) : null;
    // Tile-view U2: same data, two layouts. The column gets the grid class in
    // tile mode; cells carry data-bucket-start either way so the rail still jumps.
    const tiles = _viewIsTiles();
    if (tiles) column.classList.add('tile-grid');
    else _wireListDelegation(column, 'artists');   // one listener for the whole list
    const buildCell = (idx) => {
      const artist = sorted[idx];
      let cell;
      if (tiles) {
        cell = _artistTile(artist, 'artists');
      } else {
        cell = document.createElement('div');
        cell.className = 'list-item';
        cell._jpItem = artist;   // delegated click reads this; no per-row listener
        cell.innerHTML = `${_artImg(artist.thumb, 'list-art')}<div class="list-info"><div class="list-title">${_esc(artist.title)}</div>${_artistReleasesSub(artist)}</div><span class="list-chevron">›</span>`;
      }
      if (bucketData) _setBucketStart(cell, bucketData.keyForItem, idx);
      return cell;
    };
    // Stream the cells in non-blocking slices; activate the rail once every
    // marker exists. The generation guard aborts a stale build when the sort
    // flips or a drill-in claims the pane mid-render.
    return _renderCellsChunked(column, sorted.length, buildCell,
      () => _artistsItemsGen !== gen,
      () => { if (bucketData) _activateRail(column, bucketData.buckets); else _deactivateRail(); });
  }

  // 2026-06-09 rail plan U5 (R15): "N releases" subtitle from Plex's
  // childCount. Absent/suppressed counts (multi-library dedupe, missing
  // field) omit the line entirely — never "0 releases".
  function _artistReleasesSub(artist) {
    const rc = artist && artist.release_count;
    return (typeof rc === 'number' && rc > 0)
      ? `<div class="list-sub">${rc} release${rc === 1 ? '' : 's'}</div>` : '';
  }

  let _artistAlbumsGen = 0;

  function showArtistAlbums(artistId, artistName, returnView = 'artists', extOrigin = null) {
    if (_railDragging) cancelRailDrag();
    // Plan 002 U2: drill-in destroys the active .alpha-items-column; deactivate
    // the rail before that DOM rebuild so it doesn't point at a detached column.
    _deactivateRail();
    const gen = ++_artistAlbumsGen;
    let el, origin;
    if (returnView === 'search') {
      el = document.getElementById('search-results');
      _captureScroll('search-results', 'search');
      origin = { label: 'Search', jump: _restoreSearchView };
    } else {
      el = document.getElementById('artists-list');
      _artistsPaneEpoch++;
      if (!extOrigin) _captureScroll('artists-list', 'artists');
      // External origin (Now Playing / Queue jump, plan U5) labels the back
      // link with the surface the user actually came from (R7).
      origin = extOrigin || { label: 'Artists', jump: _restoreArtistsView };
    }
    // Nav descriptor for this drill level (plan U2): album rows hand it to
    // showAlbumTracks so back from an album returns HERE (the artist's
    // albums), not the top-level list — the "‹ Artist" lie is gone (R5).
    // External jumps (R7) prepend a "Browse" root crumb so the trail shows
    // where in the app the user landed (locked desktop mock).
    const rootCrumbs = extOrigin
      ? [{ label: 'Browse', jump: () => _restoreArtistsView() }] : [];
    const nav = {
      el, origin,
      crumbs: [...rootCrumbs, { label: artistName }],
      reenter: () => showArtistAlbums(artistId, artistName, returnView, extOrigin),
      // U6: the drilled artist's normalized identity — rows in this view
      // grey their artist links / disable kebab Go-to-artist on match.
      artistNorm: _normalizeName(artistName || ''),
    };
    el.innerHTML = '<div class="loading">Loading…</div>';
    el.prepend(_wayfindBar(
      { label: origin.label, onTap: () => { _artistAlbumsGen++; origin.jump(); } },
      [...rootCrumbs.map(c => ({ label: c.label, onTap: () => { _artistAlbumsGen++; c.jump(); } })),
       { label: artistName }]
    ));
    // encodeURIComponent (plan U4, review fix): synthesized `credit:` ids
    // carry percent-encoded act names — raw interpolation breaks on
    // "AC/DC"-class names. Harmless for plain Plex ids.
    _api('GET', `/api/browse/artists/${encodeURIComponent(artistId)}/albums`).then(([, albums]) => {
      if (_artistAlbumsGen !== gen) return;
      const inner = el.querySelector('.loading');
      if (inner) inner.remove();
      if (!albums || !albums.length) { el.insertAdjacentHTML('beforeend', '<div class="loading">No releases.</div>'); return; }
      // All Songs entry (plan 007 U3): drills into the track-level view; sits
      // atop the releases. Back from there returns here via this nav descriptor.
      el.appendChild(_allSongsEntry(artistId, artistName, nav));
      const tctx = { returnView: null, parentNav: nav, currentArtistNorm: nav && nav.artistNorm };
      // Compute count-collision labels across this artist's full release set
      // (before any subtype split) so look-alike same-title editions are tagged.
      const countLabels = _albumCountLabels(albums);
      const appendGroup = (group) => {
        if (_viewIsTiles()) el.appendChild(_releaseTileGrid(group, tctx));
        else group.forEach(album => _appendAlbumRow(el, album, nav, countLabels));
      };
      if (hasMultipleSubtypes(albums)) {
        groupAlbumsBySubtype(albums).forEach(([st, group]) => {
          el.insertAdjacentHTML('beforeend', `<div class="section-header"><h3>${_esc(SUBTYPE_LABELS[st] || (st.charAt(0).toUpperCase() + st.slice(1) + 's'))}</h3></div>`);
          appendGroup(group);
        });
      } else {
        appendGroup(albums);
      }
    });
  }

  function _albumCountLabels(albums) {
    // Same-title plan U6 (R7/R8): flag albums sharing a normalized title+artist
    // with another album in this batch but differing in track_count, so the
    // look-alike rows can be told apart. Returns {albumId: track_count} for
    // flagged albums only — identical-count collisions (same-tracklist masters)
    // and non-colliding albums get nothing.
    const byKey = new Map();
    (albums || []).forEach(a => {
      const key = (a.title || '').toLowerCase().trim() + '|' + (a.artist || '').toLowerCase().trim();
      if (!byKey.has(key)) byKey.set(key, []);
      byKey.get(key).push(a);
    });
    const labels = {};
    byKey.forEach(group => {
      if (group.length < 2) return;
      if (new Set(group.map(a => a.track_count)).size < 2) return;  // all same count → nothing to show (R8)
      group.forEach(a => { if (a.track_count != null) labels[a.id] = a.track_count; });
    });
    return labels;
  }
  window.__jpAlbumCountLabels = _albumCountLabels;

  function _countLabelHtml(album, labels) {
    const n = labels && labels[album.id];
    return n != null ? ` · ${n} track${n === 1 ? '' : 's'}` : '';
  }

  function _appendAlbumRow(el, album, nav, labels) {
    const row = document.createElement('div');
    row.className = 'list-item';
    row.innerHTML = `${_artImg(album.thumb, 'list-art')}<div class="list-info"><div class="list-title">${_esc(album.title)}</div><div class="list-sub"><span class="name-link nl-artist">${_esc(album.artist)}</span>${album.year ? ' · ' + album.year : ''}${_countLabelHtml(album, labels)}</div></div>`;
    row.addEventListener('click', () => showAlbumTracks(album.id, album.title, null, nav));
    // U6 (AE1): inside this artist's own drill, their name greys — a tap
    // opens the album (the row action) instead of re-drilling.
    _wireNameLinks(row, { artist: album.artist, currentArtistNorm: nav && nav.artistNorm });
    row.appendChild(_albumKebabBtn(album, nav && nav.artistNorm));
    el.appendChild(row);
  }

  // ── Albums view ───────────────────────────────────────────────────────────

  let albumsData = null;
  let albumsSort = 'alpha_asc';

  async function loadAlbums() {
    if (albumsData) { return renderAlbumsList(); }
    const epoch = _albumsPaneEpoch;
    const el = document.getElementById('albums-list');
    el.innerHTML = '<div class="loading">Loading…</div>';
    const [, data] = await _api('GET', '/api/browse/albums');
    if (epoch !== _albumsPaneEpoch) return;   // a drill claimed the pane mid-fetch
    if (!data || !data.length) { return _renderBrowseEmptyState(el, 'albums'); }
    albumsData = data;
    return renderAlbumsList();
  }

  function renderAlbumsList() {
    const el = document.getElementById('albums-list');
    el.innerHTML = '';
    const sortRow = createSortControl({
      id: 'albums-sort',
      options: [['alpha_asc', 'A → Z'], ['alpha_desc', 'Z → A'], ['year_asc', 'Earliest → Latest'], ['year_desc', 'Latest → Earliest'], ['popular', 'Most Played']],
      value: albumsSort,
      onChange: async (v) => { albumsSort = v; await renderAlbumsItems(); },
    });
    el.appendChild(sortRow);
    const itemsDiv = document.createElement('div');
    itemsDiv.id = 'albums-items';
    el.appendChild(itemsDiv);
    return renderAlbumsItems();
  }

  let _albumsItemsGen = 0;

  async function renderAlbumsItems() {
    if (_railDragging) cancelRailDrag();
    const el = document.getElementById('albums-items');
    if (!el || !albumsData) return;
    const gen = ++_albumsItemsGen;
    el.innerHTML = '';
    const wrapper = document.createElement('div');
    wrapper.className = 'list-with-rail';
    wrapper.dataset.sort = albumsSort;
    const column = document.createElement('div');
    column.className = 'alpha-items-column';
    wrapper.appendChild(column);
    el.appendChild(wrapper);
    let sorted;
    if (albumsSort === 'popular') {
      const [, counts] = await _api('GET', '/api/play-counts?type=album');
      if (_albumsItemsGen !== gen) return;
      const countMap = counts ? Object.fromEntries(counts.map(r => [r.entity_id, r.count])) : {};
      // Most-played plan U2 (R4): prune 0-play entries (see artists).
      sorted = albumsData
        .filter(a => (countMap[a.title] || 0) > 0)
        .sort((a, b) => (countMap[b.title] || 0) - (countMap[a.title] || 0));
      if (!sorted.length) {
        column.innerHTML = '<div class="loading">No plays yet.</div>';
        _deactivateRail();
        return;
      }
    } else {
      sorted = applySort(albumsData, albumsSort, 'title');
    }
    const dim = resolveRailDimension('albums', albumsSort);
    const bucketData = dim ? computeBuckets(sorted, dim, 'title', _alphaConfig('albums')) : null;
    const tiles = _viewIsTiles();
    if (tiles) column.classList.add('tile-grid');
    else _wireListDelegation(column, 'albums');   // one listener for the whole list
    const countLabels = _albumCountLabels(sorted);
    const buildCell = (idx) => {
      const album = sorted[idx];
      let cell;
      if (tiles) {
        // Albums tab → tracks return to the albums view.
        cell = _releaseTile(album, { returnView: 'albums' });
      } else {
        cell = document.createElement('div');
        cell.className = 'list-item';
        cell._jpItem = album;   // delegated click reads this; no per-row listeners
        const subtypeNote = album.subtype && album.subtype !== 'album' ? ` · <span style="text-transform:capitalize;color:var(--muted)">${_esc(album.subtype)}</span>` : '';
        // The kebab is part of the row template (listener-less, identical markup
        // to _albumKebabBtn) — _wireListDelegation opens its sheet — so a large
        // list builds N rows with a single innerHTML parse and zero listeners.
        cell.innerHTML = `${_artImg(album.thumb, 'list-art')}<div class="list-info"><div class="list-title">${_esc(album.title)}</div><div class="list-sub"><span class="name-link nl-artist">${_esc(album.artist)}</span>${album.year ? ' · ' + album.year : ''}${subtypeNote}${_countLabelHtml(album, countLabels)}</div></div><button class="kebab-btn" title="Release options" aria-haspopup="true">⋮</button>`;
      }
      if (bucketData) _setBucketStart(cell, bucketData.keyForItem, idx);
      return cell;
    };
    // See renderArtistsItems: stream cells, activate the rail on completion.
    return _renderCellsChunked(column, sorted.length, buildCell,
      () => _albumsItemsGen !== gen,
      () => { if (bucketData) _activateRail(column, bucketData.buckets); else _deactivateRail(); });
  }

  let _albumTracksGen = 0;

  function showAlbumTracks(albumId, albumTitle, returnView, parentNav = null) {
    if (_railDragging) cancelRailDrag();
    // Plan 002 U2: see showArtistAlbums.
    _deactivateRail();
    const gen = ++_albumTracksGen;
    let el, backLabel, onBack;
    if (parentNav) {
      // Drill reached through an intermediate view (artist albums, genre
      // style, year) or a cross-surface jump: back returns to THAT level
      // via reenter — honest hierarchy (nav plan U2, R5) — or to the
      // origin when the jump landed directly on an album.
      el = parentNav.el;
      const prev = parentNav.crumbs[parentNav.crumbs.length - 1];
      if (prev && parentNav.reenter) {
        backLabel = prev.label;
        onBack = () => { _albumTracksGen++; parentNav.reenter(); };
      } else {
        backLabel = parentNav.origin.label;
        onBack = () => { _albumTracksGen++; parentNav.origin.jump(); };
      }
    } else if (returnView === 'albums') {
      el = document.getElementById('albums-list');
      _albumsPaneEpoch++;
      _captureScroll('albums-list', 'albums');
      backLabel = 'Albums';
      // _restoreAlbumsView bumps the album-tracks gen (in-flight tracks
      // fetch bails) and the albums-items gen (stale play-counts pass
      // bails), then restores sort + scroll.
      onBack = () => _restoreAlbumsView();
    } else if (returnView === 'search') {
      el = document.getElementById('search-results');
      _captureScroll('search-results', 'search');
      backLabel = 'Search';
      onBack = () => _restoreSearchView();
    } else if (returnView === 'years') {
      el = document.getElementById('years-list');
      backLabel = 'Years';
      onBack = () => _restoreYearsView();
    } else if (returnView === 'genres') {
      el = document.getElementById('genres-list');
      backLabel = 'Genres';
      onBack = () => _restoreGenresView();
    } else {
      // Legacy artists fallback (no parentNav supplied): top-level list
      // with state restore — never the old mislabeled "‹ Artist" jump.
      el = document.getElementById('artists-list');
      _artistsPaneEpoch++;
      backLabel = 'Artists';
      onBack = () => _restoreArtistsView();
    }
    // Wayfinding bar (plan U3) replaces the legacy back-btn row. backLabel
    // still drives the bar's previous-level crumb semantics below.
    let barOrigin, barCrumbs;
    if (parentNav) {
      barOrigin = { label: parentNav.origin.label, onTap: () => { _albumTracksGen++; parentNav.origin.jump(); } };
      barCrumbs = parentNav.crumbs.map((c, i) => ({
        label: c.label,
        onTap: (i === parentNav.crumbs.length - 1 && parentNav.reenter)
          ? () => { _albumTracksGen++; parentNav.reenter(); }
          : (c.jump ? () => { _albumTracksGen++; c.jump(); } : undefined),
      }));
      barCrumbs.push({ label: albumTitle });
    } else {
      barOrigin = { label: backLabel, onTap: onBack };
      barCrumbs = [{ label: albumTitle }];
    }
    // Release-art (2026-06-15 plan): the drill-in is a .release-view — a cover
    // .release-aside (cover + album title + artist·year + album-options kebab)
    // and a .release-main holding the tracklist. Layout (sticky aside on the
    // desktop pane / stacked hero on the phone column) is pure CSS. The album
    // title is known synchronously; the cover starts as the 🎷 placeholder and
    // the cover + "artist · year" fill in once the tracks fetch resolves.
    // 2026-06-11 kebab unification: the album-options button wears the SAME
    // .kebab-btn chrome as every release/track kebab; the menu is the sheet.
    el.innerHTML = `<div class="release-view">
        <div class="release-aside">
          <div class="ra-cover">${_artImg(null, 'ra-cover-img')}</div>
          <div class="ra-htitle"><div class="t">${_esc(albumTitle)}</div><div class="s"></div></div>
          <button class="kebab-btn" title="Album options" aria-haspopup="true">⋮</button>
        </div>
        <div class="release-main"><div class="loading">Loading…</div></div>
      </div>`;
    el.prepend(_wayfindBar(barOrigin, barCrumbs));
    const overflowBtn = el.querySelector('.release-aside .kebab-btn');
    // Start with empty sources captured on the button itself; disable until
    // the tracks fetch resolves. The handler reads sources from the button
    // (not module state) so concurrent `showAlbumTracks` calls can't bleed
    // into each other's overflow menus.
    overflowBtn._sources = [];
    overflowBtn.disabled = true;
    overflowBtn.addEventListener('click', () => showOverflowMenu(albumId, overflowBtn));

    _api('GET', `/api/browse/albums/${albumId}/tracks`).then(([, tracks]) => {
      if (_albumTracksGen !== gen) return;
      const main = el.querySelector('.release-main');
      const sources = [...new Set((tracks || []).map(t => t.server_name).filter(Boolean))];
      overflowBtn._sources = sources;
      // Disable when nothing to queue at all; otherwise enable so the user
      // can open the menu and pick (or take the single-source default).
      overflowBtn.disabled = !tracks || !tracks.length;
      // Release-art (2026-06-15 U2): fill the cover + "artist · year" from the
      // loaded tracks — the album cover rides on track.thumb (Plex parentThumb)
      // and year comes from the U1 serializer field. The album title was
      // already rendered synchronously; here the placeholder swaps to real art.
      const a0 = (tracks && tracks[0]) || null;
      // Detail-kebab navigation data (2026-06-20 U1): stash the ordered distinct
      // performers + release year on the trigger button — same per-button capture
      // idiom as _sources, so concurrent drills can't cross-contaminate menus.
      overflowBtn._artists = _distinctPerformers(tracks);
      overflowBtn._year = (a0 && a0.year) || null;
      if (a0) {
        if (a0.thumb) {
          const cover = el.querySelector('.ra-cover');
          if (cover) cover.innerHTML = _artImg(a0.thumb, 'ra-cover-img');
        }
        const sub = el.querySelector('.ra-htitle .s');
        if (sub) {
          // Release subtitle links (2026-06-20): the artist (the one tappable
          // artist instance — the tracklist's are plain text, U4) and the year
          // are accent name-links. Artist reuses the pane-aware shared wiring
          // (drills in place inside search, switches to Artists otherwise); the
          // year drills that year's releases. The "·" separator stays muted.
          const bits = [];
          if (a0.artist) bits.push(`<span class="name-link nl-artist">${_esc(a0.artist)}</span>`);
          if (a0.year)   bits.push(`<span class="name-link nl-year">${a0.year}</span>`);
          sub.innerHTML = bits.join(' · ');
          if (a0.artist) _wireNameLinks(sub, { artist: a0.artist });
          const yEl = sub.querySelector('.nl-year');
          if (yEl) yEl.addEventListener('click', (e) => { e.stopPropagation(); browseToYear(a0.year); });
        }
      }
      if (main) main.innerHTML = '';
      if (!tracks || !tracks.length) { if (main) main.insertAdjacentHTML('beforeend', '<div class="loading">No tracks.</div>'); return; }
      // Multi-disc fix (2026-06-11, reqs R3): partition by disc and render a
      // "Disc N" heading per group — but ONLY when the album actually spans
      // multiple discs; single-disc albums render exactly as before. Dedup
      // runs per disc, so cross-server copies of the same disc still pair.
      const discs = [...new Set(tracks.map(t => t.disc_number || 1))].sort((a, b) => a - b);
      if (discs.length > 1) {
        discs.forEach(d => {
          main.insertAdjacentHTML('beforeend', `<div class="section-header disc-header"><h3>Disc ${d}</h3></div>`);
          renderTracksDeduped(tracks.filter(t => (t.disc_number || 1) === d), main, { currentAlbumId: albumId });
        });
      } else {
        // ctx: album self-links inside this list are no-ops (U4).
        renderTracksDeduped(tracks, main, { currentAlbumId: albumId });
      }
    });
  }

  // ── Artist → All Songs (plan 2026-06-17-007) ──────────────────────────────
  // A track-level drill from the artist's Releases view. The server (U2) returns
  // every track across own + appears-on releases, enriched with kind / release /
  // release_year / plays / pop_rank; the client groups, de-dups, and sorts here,
  // reusing the themed sort control + the shared track-row renderer.

  const ALL_SONGS_ORDERS = [
    ['popular', 'Popular'], ['release', 'By Release'],
    ['az', 'Title A → Z'], ['za', 'Title Z → A'],
    ['earliest', 'Earliest → Latest'], ['latest', 'Latest → Earliest'],
    ['plays', 'Most Played'], ['rated', 'Highest Rated'],
  ];
  let _allSongsGen = 0;
  let _allSongsSort = null;   // remembered across the session (R12); not persisted
  const _songKey = (s) => (s || '').toLowerCase().replace(/\s+/g, ' ').trim();

  function _allSongsEntry(artistId, artistName, nav) {
    const row = document.createElement('div');
    row.className = 'list-item all-songs-entry';
    row.innerHTML = `<div class="list-info"><div class="list-title">♫ All Songs</div></div><span class="list-chevron">›</span>`;
    row.addEventListener('click', () => showAllSongs(artistId, artistName, nav));
    return row;
  }

  function showAllSongs(artistId, artistName, parentNav) {
    if (_railDragging) cancelRailDrag();
    _deactivateRail();
    const gen = ++_allSongsGen;
    const el = parentNav.el;
    // Back/crumbs: the last parent crumb (the artist) re-enters the Releases
    // view, so back from All Songs lands on the artist's Releases (AE5), not the
    // top-level Artists list. Mirrors showAlbumTracks's parentNav branch.
    const barOrigin = { label: parentNav.origin.label, onTap: () => { _allSongsGen++; parentNav.origin.jump(); } };
    const barCrumbs = parentNav.crumbs.map((c, i) => ({
      label: c.label,
      onTap: (i === parentNav.crumbs.length - 1 && parentNav.reenter)
        ? () => { _allSongsGen++; parentNav.reenter(); }
        : (c.jump ? () => { _allSongsGen++; c.jump(); } : undefined),
    }));
    barCrumbs.push({ label: 'All Songs' });
    el.innerHTML = '<div class="loading">Loading…</div>';
    el.prepend(_wayfindBar(barOrigin, barCrumbs));
    _api('GET', `/api/browse/artists/${encodeURIComponent(artistId)}/songs`).then(([, data]) => {
      if (_allSongsGen !== gen) return;
      const inner = el.querySelector('.loading'); if (inner) inner.remove();
      if (!data || !data.tracks || !data.tracks.length) {
        el.insertAdjacentHTML('beforeend', '<div class="loading">No songs.</div>');
        return;
      }
      _mountAllSongs(el, data);
    }).catch(() => {
      // A true network failure rejects _api (HTTP errors resolve to [status,null]
      // and are handled above). Without this the "Loading…" spinner strands
      // forever (code-review #9, 2026-06-18).
      if (_allSongsGen !== gen) return;
      el.innerHTML = '<div class="loading">Failed to load songs.</div>';
    });
  }

  function _mountAllSongs(el, data) {
    const popAvail = !!data.popular_available;
    // Default Popular when available, else By Release (R6); clamp render-only —
    // don't overwrite an explicit session choice held in _allSongsSort.
    let mode = _allSongsSort || (popAvail ? 'popular' : 'release');
    if (mode === 'popular' && !popAvail) mode = 'release';
    // Highest Rated sort is guest-gated on ratings visibility (plan R12); clamp
    // a stale session choice if it became unavailable.
    const ratingVis = _ratingVisibleToViewer();
    if (mode === 'rated' && !ratingVis) mode = popAvail ? 'popular' : 'release';
    // Popular shows but is unselectable when the artist has no popularity (R7).
    const options = ALL_SONGS_ORDERS
      .filter(([v]) => v !== 'rated' || ratingVis)
      .map(([v, l]) => (v === 'popular' && !popAvail) ? [v, l, true] : [v, l]);
    const host = document.createElement('div');
    el.appendChild(createSortControl({
      id: 'all-songs-sort',
      options,
      value: mode,
      onChange: (v) => { _allSongsSort = v; _renderAllSongs(host, data, v); },
    }));
    el.appendChild(host);
    _renderAllSongs(host, data, mode);
  }

  function _renderAllSongs(host, data, mode) {
    host.innerHTML = '';
    if (mode === 'release') _renderAllSongsByRelease(host, data);
    else _renderAllSongsFlat(host, data, mode);
  }

  function _renderAllSongsByRelease(host, data) {
    // Each release expanded to its tracks, dups preserved; own first then
    // appears-on (response order). Per-release dedup is the album multi-disc
    // dedup (renderTracksDeduped), exactly like the album drill-in.
    const byRel = new Map();
    data.tracks.forEach(t => {
      const k = t.album_id || t.release;
      if (!byRel.has(k)) byRel.set(k, []);
      byRel.get(k).push(t);
    });
    (data.releases || []).forEach(rel => {
      const tracks = byRel.get(rel.id) || byRel.get(rel.title);
      if (!tracks || !tracks.length) return;
      const meta = (rel.year ? ` · ${rel.year}` : '') + (rel.kind === 'appears' ? ' · appears on' : '');
      host.insertAdjacentHTML('beforeend', `<div class="section-header"><h3>${_esc(rel.title)}${_esc(meta)}</h3></div>`);
      renderTracksDeduped(tracks, host, { currentAlbumId: rel.id });
    });
  }

  function _renderAllSongsFlat(host, data, mode) {
    // Group by song ACROSS the own/appears boundary: a song is Own if any copy
    // sits on an own release (canonical = earliest own copy), else Appears-On
    // (canonical = earliest appears copy). One row per song; render Own then
    // Appears-On — so a hit on an album AND an appears-on comp shows once (R9/AE3).
    const songs = _groupSongs(data.tracks);
    let own = songs.filter(s => s.isOwn);
    let appears = songs.filter(s => !s.isOwn);
    _sortSongs(own, mode);
    _sortSongs(appears, mode);
    // Highest Rated (2026-06-27): this sort shows ONLY rated tracks — drop the
    // unrated entirely (not just last), with an empty state when none are rated.
    if (mode === 'rated') {
      own = own.filter(s => _songRating(s) >= 0);
      appears = appears.filter(s => _songRating(s) >= 0);
      if (!own.length && !appears.length) {
        host.insertAdjacentHTML('beforeend', '<div class="loading">No rated tracks.</div>');
        return;
      }
    }
    const section = (title, list) => {
      if (!list.length) return;
      host.insertAdjacentHTML('beforeend', `<div class="section-header"><h3>${_esc(title)}</h3></div>`);
      list.forEach(s => host.appendChild(makeTrackRow(s.canonical, {}, undefined)));
    };
    section('Songs', own);
    section('Appears On', appears);
  }

  function _groupSongs(tracks) {
    const yearOf = (t) => (t.release_year != null ? t.release_year : (t.year != null ? t.year : Infinity));
    const by = new Map();
    tracks.forEach(t => {
      const k = _songKey(t.title);
      let g = by.get(k);
      if (!g) { g = { copies: [], isOwn: false, plays: 0 }; by.set(k, g); }
      g.copies.push(t);
      g.plays += (t.plays || 0);
      if (t.kind === 'own') g.isOwn = true;
    });
    const out = [];
    by.forEach(g => {
      // Canonical = earliest OWN copy if any own copy exists, else earliest appears.
      const pool = g.isOwn ? g.copies.filter(c => c.kind === 'own') : g.copies;
      const canonical = pool.slice().sort((a, b) => yearOf(a) - yearOf(b))[0];
      const ranks = g.copies.map(c => c.pop_rank).filter(r => r != null);
      out.push({
        canonical, isOwn: g.isOwn, plays: g.plays,
        year: yearOf(canonical),
        pop_rank: ranks.length ? Math.min(...ranks) : null,
        title: canonical.title,
      });
    });
    return out;
  }

  // Local rating for a grouped song (via its canonical track), or -1 when
  // unrated. Shared by the Highest Rated sort and its unrated-filter (2026-06-27).
  const _songRating = (s) => {
    const id = s && s.canonical && (s.canonical.track_id || s.canonical.id);
    const v = _ratingsMap ? _ratingsMap[id] : undefined;
    return (v != null) ? v : -1;
  };

  function _sortSongs(songs, mode) {
    if (mode === 'popular') {
      songs.sort((a, b) => {
        if (a.pop_rank != null && b.pop_rank != null) return a.pop_rank - b.pop_rank;
        if (a.pop_rank != null) return -1;
        if (b.pop_rank != null) return 1;
        return a.title.localeCompare(b.title);
      });
    } else if (mode === 'az') {
      songs.sort((a, b) => a.title.localeCompare(b.title));
    } else if (mode === 'za') {
      songs.sort((a, b) => b.title.localeCompare(a.title));
    } else if (mode === 'earliest') {
      songs.sort((a, b) => a.year - b.year || a.title.localeCompare(b.title));
    } else if (mode === 'latest') {
      songs.sort((a, b) => b.year - a.year || a.title.localeCompare(b.title));
    } else if (mode === 'plays') {
      songs.sort((a, b) => b.plays - a.plays || a.title.localeCompare(b.title));
    } else if (mode === 'rated') {
      // Highest Rated (2026-06-27): rating desc, title tie. Unrated tracks are
      // filtered out by _renderAllSongsFlat before render, so they never appear.
      songs.sort((a, b) => _songRating(b) - _songRating(a) || a.title.localeCompare(b.title));
    }
  }

  // ── Genres & Years ────────────────────────────────────────────────────────

  let _genreBrowser = null;
  let _styleAlbumView = null;
  // Search-bound genre→album drill-in (2026-06-10 genre-search plan U2):
  // a SECOND createStyleAlbumView instance bound to #search-results, so
  // tapping a genre chip in search results drills in-place instead of
  // hijacking the Genres tab. Lazily created on first chip tap.
  let _searchStyleView = null;
  let _searchStyleReturnFilter = 'all';

  // One row renderer for BOTH style-album views (genres tab + search
  // drill-in) — identical presentation by construction.
  function _styleAlbumRow(album) {
    const row = document.createElement('div');
    row.className = 'list-item';
    row.innerHTML = `${_artImg(album.thumb, 'list-art')}<div class="list-info"><div class="list-title">${_esc(album.title)}</div><div class="list-sub"><span class="name-link nl-artist">${_esc(album.artist)}</span>${album.year ? ' · ' + album.year : ''}</div></div>`;
    _wireNameLinks(row, { artist: album.artist });
    row.appendChild(_albumKebabBtn(album));
    return row;
  }

  function _ensureSearchStyleView() {
    if (_searchStyleView) return;
    _searchStyleView = createStyleAlbumView({
      containerId: 'search-results',
      // Distinct from the genres tab instance's 'style-albums-sort' — both
      // views can exist in the DOM in the same session.
      sortSelectId: 'search-style-albums-sort',
      backBtnClass: 'back-btn',
      sectionHeadClass: 'section-header',
      loadingClass: 'loading',
      backLabel: '‹ Search',
      // Wayfinding bar (plan U3) replaces the default back-btn row.
      backElFn: (goBack, style) => _wayfindBar(
        { label: 'Search', onTap: goBack }, [{ label: style }]),
      onBack: () => {
        // Restore the filter tab that was active at chip-tap time so back
        // never lands on a filtered view that excludes genres (review
        // decision, corroborated).
        searchFilter = _searchStyleReturnFilter;
        document.querySelectorAll('.filter-tab').forEach(t =>
          t.classList.toggle('active', t.dataset.filter === searchFilter));
        if (lastSearchData) renderSearchResults(lastSearchData);
        else document.getElementById('search-results').innerHTML = '';
        _restoreScroll('search-results', 'search');
      },
      albumRowFn: _styleAlbumRow,
      // Tile-view U2: releases within a genre follow the global view setting.
      tileRowFn: _styleAlbumTile,
      isTilesFn: _viewIsTiles,
      // Nav plan U2: back from the album returns to THIS style view, not
      // the search results — honest hierarchy via the parentNav reenter.
      onAlbumClick: (album, style) => showAlbumTracks(album.id, album.title, null, {
        el: document.getElementById('search-results'),
        origin: { label: 'Search', jump: () => { _searchStyleView.cancel(); _restoreSearchView(); } },
        crumbs: [{ label: style }],
        reenter: () => _searchStyleView.show(style),
      }),
    });
  }

  function _showSearchStyleAlbums(style) {
    _ensureSearchStyleView();
    _searchStyleReturnFilter = searchFilter;
    _captureScroll('search-results', 'search');
    _searchStyleView.show(style);
  }

  function _ensureGenreBrowser() {
    if (_genreBrowser) return;
    _genreBrowser = createGenreBrowser({
      containerId: 'genres-list',
      chipClass: 'genre-chip',
      pageSize: 25,
      onSelect: style => {
        _captureScroll('genres-list', 'genres');
        _styleAlbumView.show(style);
      },
    });
    _styleAlbumView = createStyleAlbumView({
      containerId: 'genres-list',
      sortSelectId: 'style-albums-sort',
      backBtnClass: 'back-btn',
      sectionHeadClass: 'section-header',
      loadingClass: 'loading',
      // Wayfinding bar (plan U3) replaces the default back-btn row.
      backElFn: (goBack, style) => _wayfindBar(
        { label: 'Genres', onTap: goBack }, [{ label: style }]),
      // Nav plan U2: back keeps the genre cache AND the pagination state
      // ("Show More" presses survive the round trip), plus scroll.
      onBack: () => _restoreGenresView(),
      albumRowFn: _styleAlbumRow,
      // Tile-view U2: releases within a genre follow the global view setting.
      tileRowFn: _styleAlbumTile,
      isTilesFn: _viewIsTiles,
      // Back from an album returns to THIS style view (honest hierarchy).
      onAlbumClick: (album, style) => showAlbumTracks(album.id, album.title, null, {
        el: document.getElementById('genres-list'),
        origin: { label: 'Genres', jump: () => { _styleAlbumView.cancel(); _restoreGenresView(); } },
        crumbs: [{ label: style }],
        reenter: () => _styleAlbumView.show(style),
      }),
    });
  }

  async function loadGenres() {
    _ensureGenreBrowser();
    await _genreBrowser.load();
  }

  // ── Most Played (2026-06-10 most-played plan U2) ──────────────────────────

  let _mostPlayedLoaded = false;

  async function loadMostPlayed() {
    const el = document.getElementById('mostplayed-list');
    if (!el) return;
    if (_mostPlayedLoaded) return;  // cached per page load, like artists/albums
    el.innerHTML = '<div class="loading">Loading…</div>';
    const [, data] = await _api('GET', '/api/most-played');
    if (!data || !data.length) {
      // Not cached: a fresh install that starts playing can see the
      // leaderboard appear on the next tab visit (R5 empty state).
      el.innerHTML = '<div class="loading">No plays yet.</div>';
      return;
    }
    _mostPlayedLoaded = true;
    el.innerHTML = '';
    data.forEach(track => {
      const row = makeTrackRow(track, { mostPlayed: true });
      const count = document.createElement('span');
      count.className = 'mp-count';
      count.textContent = `${track.play_count}×`;
      row.insertBefore(count, row.firstChild);
      el.appendChild(row);
    });
  }

  // Admin curation (plan U5): remove a track from Most Played. Non-optimistic —
  // the row is removed only on a 2xx; on failure the row stays and a toast fires.
  // Resetting _mostPlayedLoaded re-fetches (and shows the empty state) next visit.
  async function _removeFromMostPlayed(t, row) {
    const tid = t.track_id || t.id;
    const [status] = await _api('POST', '/admin/most-played/remove', { track_id: tid });
    if (status >= 200 && status < 300) {
      if (row && row.parentNode) row.parentNode.removeChild(row);
      _mostPlayedLoaded = false;
      return true;
    }
    _config.toast('Could not remove from Most Played');
    return false;
  }

  // ── Highest Rated leaderboard (2026-06-26 ratings-and-tags plan U7) ─────────
  // Mirrors loadMostPlayed. No separate badge — makeTrackRow already renders the
  // rating pips from the global map (read-only for guests, editable for admin).
  // Reset by _postRating so a re-rate/clear reorders on the next tab visit.
  let _highestRatedLoaded = false;

  async function loadHighestRated() {
    const el = document.getElementById('highestrated-list');
    if (!el) return;
    if (_highestRatedLoaded) return;  // cached per page load, like Most Played
    _deactivateRail();                // flat rating-ranked list — no rail
    el.innerHTML = '<div class="loading">Loading…</div>';
    const [, data] = await _api('GET', '/api/highest-rated');
    if (!data || !data.length) {
      el.innerHTML = '<div class="loading">No ratings yet.</div>';
      return;
    }
    _highestRatedLoaded = true;
    el.innerHTML = '';
    data.forEach(track => el.appendChild(makeTrackRow(track)));
  }

  // ── Recently Added (plan 006) ─────────────────────────────────────────────

  let _recentlyAddedLoaded = false;

  async function loadRecentlyAdded() {
    const el = document.getElementById('recentlyadded-list');
    if (!el) return;
    if (_recentlyAddedLoaded) return;  // cached per page load, like the other panes
    _deactivateRail();                 // flat newest-first list — no rail
    el.innerHTML = '<div class="loading">Loading…</div>';
    const [, data] = await _api('GET', '/api/recently-added');
    if (!data || !data.length) {
      // Empty only when the library set is empty; an unpopulated index triggers
      // a background crawl server-side, so a later tab visit fills it (R10).
      el.innerHTML = '<div class="loading">Nothing added yet.</div>';
      return;
    }
    _recentlyAddedLoaded = true;
    el.innerHTML = '';
    // Nav descriptor (mirrors showArtistAlbums): _appendAlbumRow's row click
    // hands this to showAlbumTracks so the release renders into THIS list and
    // back returns here. Without it the drill-in falls through to the legacy
    // artists-list target and renders into the (hidden) Artists tab.
    const reload = () => { _recentlyAddedLoaded = false; loadRecentlyAdded(); };
    const nav = {
      el,
      origin: { label: 'Recently Added', jump: reload },
      crumbs: [],
      reenter: reload,
    };
    data.forEach(album => {
      _appendAlbumRow(el, album, nav);  // reuse the shared row + album drill-in
      const ago = _addedAgo(album.added_at);
      const sub = ago && el.lastElementChild && el.lastElementChild.querySelector('.list-sub');
      if (sub) {
        const span = document.createElement('span');
        span.className = 'ra-added';
        span.textContent = ' · ' + ago;
        sub.appendChild(span);
      }
    });
  }

  let yearsData = null;

  async function loadYears() {
    if (yearsData) { renderYearsList(); return; }
    const el = document.getElementById('years-list');
    el.innerHTML = '<div class="loading">Loading…</div>';
    const [, data] = await _api('GET', '/api/browse/years');
    if (!data || !data.length) { el.innerHTML = '<div class="loading">No years found.</div>'; return; }
    yearsData = data;
    renderYearsList();
  }

  function renderYearsList() {
    // The year CHIPS view has no rail; the year-albums drilldown (plan 005) now
    // activates one, so dropping back to the chips must deactivate it.
    if (_railDragging) cancelRailDrag();
    _deactivateRail();
    const el = document.getElementById('years-list');
    el.innerHTML = '<div style="padding:.75rem">' + yearsData.map(y => `<span class="year-chip" data-year="${y}">${y}</span>`).join('') + '</div>';
    el.querySelectorAll('.year-chip').forEach(chip => chip.addEventListener('click', () => showYearAlbums(parseInt(chip.dataset.year, 10))));
  }

  let _yearAlbumsGen = 0;
  let _yearAlbumsItemsGen = 0;
  // Year drilldown sort (plan 005): one session-shared choice across year
  // drilldowns (R4), Title A→Z default. The open year's albums are cached so a
  // sort change re-renders without refetching (R9).
  let yearAlbumsSort = 'alpha_asc';
  let _yearAlbumsData = null;
  let _yearAlbumsNav = null;

  function showYearAlbums(year) {
    if (_railDragging) cancelRailDrag();
    // Plan 002 U2: see showArtistAlbums.
    _deactivateRail();
    const gen = ++_yearAlbumsGen;
    _yearAlbumsData = null;
    const el = document.getElementById('years-list');
    _captureScroll('years-list', 'years');
    el.innerHTML = `<div class="section-header"><h3>${year}</h3></div><div class="loading">Loading…</div>`;
    // _restoreYearsView bumps the year-albums gen (in-flight fetch bails)
    // and keeps the years cache + scroll (nav plan U2).
    el.prepend(_wayfindBar(
      { label: 'Years', onTap: () => _restoreYearsView() },
      [{ label: String(year) }]
    ));
    // Nav descriptor: back from an album returns to THIS year's list (R5).
    const nav = {
      el,
      origin: { label: 'Years', jump: _restoreYearsView },
      crumbs: [{ label: String(year) }],
      reenter: () => showYearAlbums(year),
    };
    _yearAlbumsNav = nav;
    _api('GET', `/api/browse/years/${year}/albums`).then(([, albums]) => {
      if (_yearAlbumsGen !== gen) return;
      const inner = el.querySelector('.loading');
      if (inner) inner.remove();
      if (!albums || !albums.length) { el.insertAdjacentHTML('beforeend', '<div class="loading">No albums.</div>'); return; }
      _yearAlbumsData = albums;
      _renderYearAlbumsList(el);
    });
  }

  // Build the sort control + items container for the open year's albums, then
  // render the items. Mirrors renderAlbumsList; the sort onChange re-renders the
  // items only — no refetch (R1, R9).
  function _renderYearAlbumsList(el) {
    const sortRow = createSortControl({
      id: 'year-albums-sort',
      // No year sort: a single-year drilldown shares one year, so it is a no-op (R3).
      // Labels disambiguate the two alpha keys (R2).
      options: [['alpha_asc', 'Title A → Z'], ['alpha_desc', 'Title Z → A'],
                ['artist_asc', 'Artist A → Z'], ['artist_desc', 'Artist Z → A'],
                ['popular', 'Most Played']],
      value: yearAlbumsSort,
      onChange: async (v) => { yearAlbumsSort = v; await _renderYearAlbumsItems(); },
    });
    el.appendChild(sortRow);
    const itemsDiv = document.createElement('div');
    itemsDiv.id = 'year-albums-items';
    el.appendChild(itemsDiv);
    return _renderYearAlbumsItems();
  }

  async function _renderYearAlbumsItems() {
    if (_railDragging) cancelRailDrag();
    const el = document.getElementById('year-albums-items');
    if (!el || !_yearAlbumsData) return;
    const gen = ++_yearAlbumsItemsGen;
    const nav = _yearAlbumsNav;
    el.innerHTML = '';
    const wrapper = document.createElement('div');
    wrapper.className = 'list-with-rail';
    wrapper.dataset.sort = yearAlbumsSort;
    const column = document.createElement('div');
    column.className = 'alpha-items-column';
    wrapper.appendChild(column);
    el.appendChild(wrapper);
    let sorted;
    if (yearAlbumsSort === 'popular') {
      const [, counts] = await _api('GET', '/api/play-counts?type=album');
      // Bail if a later sort superseded this one, or the pane was backed out of.
      if (_yearAlbumsItemsGen !== gen || !document.body.contains(el)) return;
      const countMap = counts ? Object.fromEntries(counts.map(r => [r.entity_id, r.count])) : {};
      // Most-played: prune 0-play entries, order desc (see renderAlbumsItems, R10).
      sorted = _yearAlbumsData
        .filter(a => (countMap[a.title] || 0) > 0)
        .sort((a, b) => (countMap[b.title] || 0) - (countMap[a.title] || 0));
      if (!sorted.length) {
        column.innerHTML = '<div class="loading">No plays yet.</div>';
        _deactivateRail();
        return;
      }
    } else {
      sorted = applySort(_yearAlbumsData, yearAlbumsSort, 'title');
    }
    // Rail keyed by the displayed list's field: artist sorts index by artist
    // first-char, title sorts by title (R6). The list is always albums, so the
    // album threshold drives international suppression (R7).
    const nameField = yearAlbumsSort.startsWith('artist') ? 'artist' : 'title';
    const dim = resolveRailDimension('albums', yearAlbumsSort);
    const bucketData = dim ? computeBuckets(sorted, dim, nameField, _alphaConfig('albums')) : null;
    sorted.forEach((album, idx) => {
      const row = document.createElement('div');
      row.className = 'list-item';
      // Single-year view: year omitted from the subtitle (it is constant).
      row.innerHTML = `${_artImg(album.thumb, 'list-art')}<div class="list-info"><div class="list-title">${_esc(album.title)}</div><div class="list-sub"><span class="name-link nl-artist">${_esc(album.artist)}</span></div></div>`;
      row.addEventListener('click', () => showAlbumTracks(album.id, album.title, null, nav));
      _wireNameLinks(row, { artist: album.artist });
      row.appendChild(_albumKebabBtn(album));
      if (bucketData) _setBucketStart(row, bucketData.keyForItem, idx);
      column.appendChild(row);
    });
    if (bucketData) _activateRail(column, bucketData.buckets);
    else _deactivateRail();
  }

  // ── Search ────────────────────────────────────────────────────────────────

  let searchTimer;
  let searchAbortController = null;
  let lastSearchData = null;
  let lastSearchQuery = '';
  let searchFilter = 'all';
  // Per-filter-tab scroll memory within a single search (2026-07-01 ce-debug).
  // Keyed by filter name → scrollTop. Switching tabs rebuilds #search-results in
  // place; on guest the scroller is the ANCESTOR (#content), so without this an
  // unvisited tab inherited the prior tab's depth and opened mid-list. Reset on
  // each new query / clear so tabs never carry a prior search's position.
  let _searchTabScroll = {};
  // Tiered search (plan 2026-06-14-005). Tier 1 = the hub-search results
  // already on screen; Tier 2 = a broader literal title-substring pass
  // (/api/search/broad) auto-loaded a page at a time as the user scrolls to
  // the end, deduped against Tier 1 and prior broad pages. _broad holds the
  // live pagination cursor + IntersectionObserver for the current query.
  const BROAD_PAGE_SIZE = 30;
  // Hard ceiling on auto-paged broad requests. Far more than any guest
  // scrolls (25 × 30 = 750 results); exists only so a misbehaving PMS that
  // ignored X-Plex-Container-Start (returning rows forever) can't spin the
  // sentinel into an unbounded fetch loop.
  const BROAD_MAX_PAGES = 25;
  let _broad = null;

  function _wireSearch() {
    document.querySelectorAll('.filter-tab').forEach(tab => {
      tab.addEventListener('click', () => {
        const nextFilter = tab.dataset.filter;
        // Remember the OUTGOING tab's depth before the in-place rebuild, then
        // land the INCOMING tab at its own remembered depth — or the top if it
        // hasn't been scrolled yet. _scrollOwner resolves the real scroller on
        // both pages (guest: ancestor #content; admin: #search-results itself),
        // so the fix is uniform.
        const outOwner = _scrollOwner(document.getElementById('search-results'));
        if (outOwner) _searchTabScroll[searchFilter] = outOwner.scrollTop;
        document.querySelectorAll('.filter-tab').forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        searchFilter = nextFilter;
        if (lastSearchData) renderSearchResults(lastSearchData);
        const inOwner = _scrollOwner(document.getElementById('search-results'));
        if (inOwner) inOwner.scrollTop = _searchTabScroll[nextFilter] || 0;
      });
    });
    const input = document.getElementById('search-input');
    if (!input) return;
    // Cross-browser clear-× affordance (search-clear plan U1). The .search-field
    // wrapper + .search-clear button are per-template chrome; this wiring is a
    // no-op until they exist, so the behavior can ship ahead of the markup.
    const searchField = input.closest('.search-field');
    function syncSearchClear() {
      if (searchField) searchField.classList.toggle('has-text', input.value.length > 0);
    }
    input.addEventListener('input', (e) => {
      syncSearchClear();
      clearTimeout(searchTimer);
      const q = e.target.value.trim();
      if (!q) {
        // Input clear rebuilds the container outside renderSearchResults —
        // cancel any in-flight drill-in fetch so it can't append stale rows.
        if (_searchStyleView) _searchStyleView.cancel();
        _teardownBroadTier();
        document.getElementById('search-results').innerHTML = '';
        lastSearchData = null;
        lastSearchQuery = '';
        _searchTabScroll = {};
        if (searchAbortController) { searchAbortController.abort(); searchAbortController = null; }
        return;
      }
      searchTimer = setTimeout(() => doSearch(q), 400);
    });
    const searchClearBtn = searchField && searchField.querySelector('.search-clear');
    if (searchClearBtn) {
      searchClearBtn.addEventListener('click', () => {
        input.value = '';
        // Reuse the empty-input path (clears results + aborts) and refresh state.
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.focus();
      });
    }
    syncSearchClear();
  }

  async function doSearch(q) {
    if (searchAbortController) searchAbortController.abort();
    searchAbortController = new AbortController();
    // A new query starts every filter tab fresh at the top.
    _searchTabScroll = {};
    const el = document.getElementById('search-results');
    el.innerHTML = '<div class="loading">Searching…</div>';
    try {
      const resp = await fetch(`/api/search?q=${encodeURIComponent(q)}`, {
        signal: searchAbortController.signal,
      });
      if (!resp.ok) throw new Error('search failed');
      const data = await resp.json();
      lastSearchData = data;
      lastSearchQuery = q;
      renderSearchResults(data);
    } catch (e) {
      if (e.name === 'AbortError') return;
      el.innerHTML = '<div class="loading">Search failed.</div>';
    }
  }

  // Single source for a search album row (Tier-1 grouped albums AND Tier-2
  // broad matches). Mirrors the year-album / browse-album row exactly so the
  // broad tier appends rows indistinguishable from the hub-search ones.
  function _searchAlbumRow(album) {
    const row = document.createElement('div');
    row.className = 'list-item';
    row.innerHTML = `${_artImg(album.thumb, 'list-art')}<div class="list-info"><div class="list-title">${_esc(album.title)}</div><div class="list-sub"><span class="name-link nl-artist">${_esc(album.artist)}</span></div></div>`;
    row.addEventListener('click', () => showAlbumTracks(album.id, album.title, 'search'));
    _wireNameLinks(row, { artist: album.artist });
    row.appendChild(_albumKebabBtn(album));
    return row;
  }

  // Fresh-install audit F10: during the first catalog crawl the browse index
  // (artists/albums) fills in seconds while the track catalog takes ~30 min —
  // a guest's search showed albums but silently zero tracks, or a bare
  // "No results." when nothing matched yet. When the track section would be
  // empty, consult /api/scan-status BEFORE assembling the DOM (single render
  // pass — no async arrival, no reflow) and explain that indexing is running.
  let _searchRenderGen = 0;

  async function _searchScanStatus(data, f) {
    const tracksEmpty = (f === 'all' || f === 'tracks')
      && !(data.tracks && data.tracks.length);
    if (!tracksEmpty) return null;   // hint can't apply — skip the fetch
    try { const [, s] = await _api('GET', '/api/scan-status'); return s; }
    catch (_) { return null; }       // status probe down → today's behavior
  }

  async function renderSearchResults(data) {
    // Status fetch happens before any DOM work; the generation guard drops a
    // stale render if a newer query/tab-tap superseded us during the await.
    const gen = ++_searchRenderGen;
    const scanStatus = await _searchScanStatus(data, searchFilter);
    if (gen !== _searchRenderGen) return;
    // Single chokepoint for every #search-results rebuild (new query,
    // filter-tab tap, back-from-drill-in): bump the search drill-in's
    // generation FIRST so an in-flight style-albums fetch bails instead of
    // appending stale rows after this render. Typing or tab-tapping during
    // drill-in is the intended exit path (review fix, corroborated).
    if (_searchStyleView) _searchStyleView.cancel();
    // Any rebuild invalidates the prior broad-tier cursor/observer/sentinel
    // (they pointed into the now-cleared DOM). Tear down before wiping.
    _teardownBroadTier();
    const el = document.getElementById('search-results');
    el.innerHTML = '';
    const f = searchFilter;
    let any = false;
    const scanning = !!(scanStatus && scanStatus.scanning);

    if ((f === 'all' || f === 'artists') && data.artists && data.artists.length) {
      any = true;
      el.insertAdjacentHTML('beforeend', '<div class="section-header cat-head"><h3>Artists</h3></div>');
      if (_viewIsTiles()) {
        const grid = document.createElement('div');
        grid.className = 'tile-grid';
        data.artists.forEach(artist => grid.appendChild(_artistTile(artist, 'search')));
        el.appendChild(grid);
      } else {
        data.artists.forEach(artist => {
          const row = document.createElement('div');
          row.className = 'list-item';
          row.innerHTML = `${_artImg(artist.thumb, 'list-art')}<div class="list-info"><div class="list-title">${_esc(artist.title)}</div>${_artistReleasesSub(artist)}</div><span class="list-chevron">›</span>`;
          row.addEventListener('click', () => showArtistAlbums(artist.id, artist.title, 'search'));
          el.appendChild(row);
        });
      }
    }
    if ((f === 'all' || f === 'albums') && data.albums && data.albums.length) {
      any = true;
      groupAlbumsBySubtype(data.albums).forEach(([st, group]) => {
        const label = SUBTYPE_LABELS[st] || (st.charAt(0).toUpperCase() + st.slice(1) + 's');
        el.insertAdjacentHTML('beforeend', `<div class="section-header cat-head"><h3>${_esc(label)}</h3></div>`);
        if (_viewIsTiles()) el.appendChild(_releaseTileGrid(group, { returnView: 'search' }));
        else group.forEach(album => el.appendChild(_searchAlbumRow(album)));
      });
    }
    if ((f === 'all' || f === 'tracks') && data.tracks && data.tracks.length) {
      any = true;
      el.insertAdjacentHTML('beforeend', '<div class="section-header cat-head"><h3>Tracks</h3></div>');
      renderTracksDeduped(data.tracks, el);
    } else if ((f === 'all' || f === 'tracks') && scanning && any) {
      // Mixed results during the first crawl (audit F10): artists/albums hit
      // but the track catalog is still filling — say so in the Tracks slot
      // instead of silently omitting the section. Rendered in the same pass
      // as the results (status pre-fetched), so nothing reflows.
      el.insertAdjacentHTML('beforeend',
        '<div class="section-header cat-head"><h3>Tracks</h3></div>'
        + '<div class="loading">Tracks are still being indexed — check back shortly.</div>');
    }
    // Genres LAST in All per origin R2 (broadest, least-specific match
    // type). Chip cluster reuses the Genres tab's presentation.
    if ((f === 'all' || f === 'genres') && data.genres && data.genres.length) {
      any = true;
      el.insertAdjacentHTML('beforeend', '<div class="section-header cat-head"><h3>Genres</h3></div>');
      const chipWrap = document.createElement('div');
      chipWrap.style.cssText = 'padding:.75rem';
      data.genres.forEach(g => {
        const chip = document.createElement('span');
        chip.className = 'genre-chip';
        chip.dataset.genre = g.name;
        chip.textContent = g.name;
        chip.addEventListener('click', () => _showSearchStyleAlbums(g.name));
        chipWrap.appendChild(chip);
      });
      el.appendChild(chipWrap);
    }
    // Distinct empty state for the genres filter (review fix): a bare
    // empty div reads as stuck-loading to party guests.
    if (!any) {
      if (scanning && f !== 'genres') {
        // All-empty during the first crawl — the most common shape early on,
        // while the browse index itself is still building (audit F10): an
        // unexplained "No results." here looks like a broken jukebox.
        el.innerHTML = '<div class="loading">Your library is still being indexed — check back shortly.</div>';
      } else {
        el.innerHTML = `<div class="loading">${f === 'genres' ? 'No genres found.' : 'No results.'}</div>`;
      }
    } else {
      // Tier 2: with Tier-1 hits on screen to scroll past, arm the broad
      // pass (tracks/albums only; artist/genre filters opt out via
      // _broadTypesFor → no sentinel). R3: deliberate scroll-to-end
      // expansion, not auto-fire on incidental scroll.
      _setupBroadTier(data);
    }
  }

  // ── Tier 2: broad literal title-substring expansion ───────────────────────

  // track/album for 'all', the single type for a type-specific filter, and
  // null for artists/genres (no substring tier — those aren't title rows).
  function _broadTypesFor(filter) {
    if (filter === 'all') return 'track,album';
    if (filter === 'tracks') return 'track';
    if (filter === 'albums') return 'album';
    return null;
  }

  function _teardownBroadTier() {
    if (_broad) {
      if (_broad.observer) {
        try { _broad.observer.disconnect(); } catch (_) { /* noop */ }
      }
      // Abort the in-flight page too (2026-07-17 ce-debug): without this a
      // re-query per keystroke only orphaned the response client-side while
      // the server kept burning per-source semaphore slots on stale queries,
      // queueing the live pages the CURRENT query was about to ask for.
      if (_broad.abort) {
        try { _broad.abort.abort(); } catch (_) { /* noop */ }
      }
    }
    _broad = null;
  }

  function _setupBroadTier(data) {
    _teardownBroadTier();
    const types = _broadTypesFor(searchFilter);
    const q = lastSearchQuery;
    if (!types || !q) return;
    if (typeof IntersectionObserver === 'undefined') return;
    const el = document.getElementById('search-results');
    if (!el) return;
    // Seed the dedup set with every Tier-1 row already on screen so the
    // broad pass never re-shows a hub hit. Tracks keyed by the SAME identity
    // deduplicateTracks uses (collapses cross-server copies too); albums by id.
    const shown = new Set();
    (data.tracks || []).forEach(t => shown.add('t:' + _trackDedupKey(t)));
    (data.albums || []).forEach(a => shown.add('a:' + a.id));
    const sentinel = document.createElement('div');
    sentinel.className = 'search-sentinel';
    el.appendChild(sentinel);
    const b = {
      q, filter: searchFilter, types,
      page: 0, done: false, loading: false,
      // Consecutive auto-chained pages that appended nothing new (or failed).
      // Bounds the forced re-observe below: an all-duplicates run may chain
      // at most _BROAD_STALE_CHAIN pages before the tier waits for a real
      // scroll transition (2026-07-17 ce-debug — the unbounded chain serially
      // pulled up to 25 live-source pages per query on catalog installs
      // where every row deduped against Tier 1).
      stale: 0,
      shown, sentinel, headerDone: false, observer: null, abort: null,
    };
    // Root on the element that ACTUALLY scrolls #search-results: on guest
    // that's the ancestor (#content); on ADMIN #search-results is its OWN
    // overflow:auto box, which _findScrollAncestor skips (it starts at
    // el.parentElement) — rooted on a non-scroller, admin's sentinel stayed
    // clipped and the tier never fired there (2026-07-02 ce-debug, fix lost
    // in the 546cd36 revert; re-applied 2026-07-17). _scrollOwner returns
    // the element itself when it scrolls, else the ancestor.
    b.observer = new IntersectionObserver((entries) => {
      if (entries.some(e => e.isIntersecting)) _loadBroadPage();
    }, { root: _scrollOwner(el) || null, rootMargin: '200px' });
    b.observer.observe(sentinel);
    _broad = b;
  }

  // Ceiling on consecutive forced re-observes that yielded no new rows: one
  // all-duplicates page is tolerated (a dupes-run can sit between real
  // matches), the second consecutive one parks the tier until an actual
  // scroll transition re-enters the sentinel.
  const _BROAD_STALE_CHAIN = 2;

  async function _loadBroadPage() {
    const b = _broad;
    if (!b || b.loading || b.done) return;
    b.loading = true;
    b.sentinel.classList.add('loading');
    try {
      b.abort = new AbortController();
      const resp = await fetch(
        `/api/search/broad?q=${encodeURIComponent(b.q)}&types=${encodeURIComponent(b.types)}&page=${b.page}`,
        { signal: b.abort.signal }
      );
      if (!resp.ok) throw new Error('broad search failed');
      const data = await resp.json();
      // A newer query/filter swapped _broad while we awaited — drop this page.
      if (_broad !== b) return;
      const got = (data.tracks ? data.tracks.length : 0) + (data.albums ? data.albums.length : 0);
      const newAlbums = (data.albums || []).filter(a => {
        const k = 'a:' + a.id;
        if (b.shown.has(k)) return false;
        b.shown.add(k);
        return true;
      });
      const newTracks = (data.tracks || []).filter(t => {
        const k = 't:' + _trackDedupKey(t);
        if (b.shown.has(k)) return false;
        b.shown.add(k);
        return true;
      });
      const el = document.getElementById('search-results');
      if (el && (newAlbums.length || newTracks.length)) {
        if (!b.headerDone) {
          const head = document.createElement('div');
          head.className = 'more-head';
          head.innerHTML = `<h3>More matches</h3><span class="hint">titles containing “${_esc(b.q)}”</span>`;
          el.insertBefore(head, b.sentinel);
          b.headerDone = true;
        }
        // Albums above tracks, matching Tier-1 section order. In tile mode the
        // broad albums collect into one persistent grid (created before the
        // sentinel on first use) so paged-in tiles share the grid layout.
        if (newAlbums.length) {
          if (_viewIsTiles()) {
            if (!b.albumGrid) {
              b.albumGrid = document.createElement('div');
              b.albumGrid.className = 'tile-grid';
              el.insertBefore(b.albumGrid, b.sentinel);
            }
            newAlbums.forEach(a => b.albumGrid.appendChild(_releaseTile(a, { returnView: 'search' })));
          } else {
            newAlbums.forEach(a => el.insertBefore(_searchAlbumRow(a), b.sentinel));
          }
        }
        if (newTracks.length) {
          const wrap = document.createElement('div');
          renderTracksDeduped(newTracks, wrap);
          while (wrap.firstChild) el.insertBefore(wrap.firstChild, b.sentinel);
        }
      }
      // Stale-chain accounting (2026-07-17 ce-debug): a page that appended
      // nothing new (all rows deduped against Tier 1 / prior pages) still
      // advances the cursor, but only _BROAD_STALE_CHAIN of them may
      // auto-chain via the forced re-observe below.
      if (newAlbums.length || newTracks.length) b.stale = 0;
      else b.stale++;
      b.page++;
      // The server returned nothing for this page → the well is dry. (A page
      // can be all-duplicates — got>0 but zero new rows — so we keep paging
      // on got>0; X-Plex-Container-Start advances each page, so it terminates.)
      // BROAD_MAX_PAGES is the backstop if paging never empties.
      if (got === 0 || b.page >= BROAD_MAX_PAGES) {
        b.done = true;
        if (b.observer) { try { b.observer.disconnect(); } catch (_) { /* noop */ } }
      }
    } catch (e) {
      // A teardown-driven abort is not a failure — the tier is already gone.
      if (e && e.name === 'AbortError') return;
      // Any other failure counts toward the stale chain so a dead source
      // cannot spin the forced re-observe into an infinite retry loop
      // (2026-07-17 ce-debug); a later real scroll retries this same page.
      if (_broad === b) b.stale++;
    } finally {
      if (_broad === b) {
        b.loading = false;
        b.sentinel.classList.remove('loading');
        // IntersectionObserver fires only on transitions. If this page didn't
        // push the sentinel out of view (few/no new rows, short viewport),
        // re-observe to re-deliver the current intersection and pull the next
        // page — self-stops once content fills the viewport (sentinel scrolls
        // out), the tier is done, or the stale chain hit its ceiling.
        if (!b.done && b.observer && b.stale < _BROAD_STALE_CHAIN) {
          b.observer.unobserve(b.sentinel);
          b.observer.observe(b.sentinel);
        }
      }
    }
  }

  // ── Tab activation hooks (called by the page when tabs switch) ────────────

  // ── Browse facet visibility (2026-06-26 ratings-and-tags plan U8) ───────────
  // The five toggleable facets → their tab's data-view. Search/Artists/Albums/
  // Now are never toggleable. display:none removes a flex child with no gap (R14).
  const _FACET_VIEW = {
    genre: 'genres-view', years: 'years-view', mostplayed: 'mostplayed-view',
    recentlyadded: 'recentlyadded-view', highestrated: 'highestrated-view',
  };

  function _ratingVisibleToViewer() {
    return !!(_config && (_config.authMode === 'admin' || _config.ratingsVisible));
  }

  // Hide toggleable tabs the admin turned off — for GUESTS only; the admin
  // always sees every facet (R15). The Highest Rated tab is double-gated on
  // ratings visibility (R12). Underlying data is untouched — only the entry
  // point hides.
  function _applyTabVisibility() {
    const admin = !!(_config && _config.authMode === 'admin');
    const facets = (_config && _config.facets) || {};
    Object.keys(_FACET_VIEW).forEach(facet => {
      const tab = document.querySelector(`#tabs [data-view="${_FACET_VIEW[facet]}"]`);
      if (!tab) return;
      let show = admin ? true : (facets[facet] !== false);
      if (facet === 'highestrated' && !_ratingVisibleToViewer()) show = false;
      tab.style.display = show ? '' : 'none';
    });
  }

  function activateView(viewId) {
    if (viewId === 'artists-view') loadArtists();
    else if (viewId === 'albums-view') loadAlbums();
    else if (viewId === 'genres-view') loadGenres();
    else if (viewId === 'years-view') loadYears();
    else if (viewId === 'mostplayed-view') loadMostPlayed();
    else if (viewId === 'recentlyadded-view') loadRecentlyAdded();
    else if (viewId === 'highestrated-view') loadHighestRated();
  }

  // ── Mount entry point ─────────────────────────────────────────────────────

  function mountBrowser(containerSelector, config) {
    _config = Object.assign({
      authMode: 'guest',
      isLocked: () => false,
      toast: (msg) => console.log('[toast]', msg),
      // Tile-view U2: List by default; mountAppearance pushes the effective
      // view (device override → admin default) via setViewMode after fetch.
      viewMode: 'list',
      // International rail (plan 004): install-wide alpha-rail mode + per-rail
      // thresholds. mountAppearance pushes the effective values via setRailAlpha
      // after the /api/appearance fetch; defaults keep the shipped English rail.
      railAlphaMode: 'english',
      railArtistThreshold: 2,
      railAlbumThreshold: 2,
    }, config || {});
    // Container selector kept for symmetry / future use; the module relies on
    // per-view IDs (#search-results, #artists-list, etc.) being present inside.
    _wireOverflowMenu();
    _wireSearch();
    // Pattern rules load alongside the first list fetch; sort/bucketing
    // applies them no later than the next render (R6's next-load bar).
    _loadPatternRules();
    // Server ownership meta for source-priority ranking (U3) — same
    // load-once idiom; rows rendered before it lands rank alphabetically
    // and self-correct on the next render.
    _loadServerMeta();
    // Track ratings + tags (2026-06-26 plan U5): load-once like the meta above;
    // rows rendered before it lands self-correct via _redecorateRatingTags.
    _loadRatingTagMaps();
    return {
      activateView,
      // Guest visibility + facet tab gating (2026-06-26 plan U8). Pushed by
      // mountAppearance after the /api/appearance fetch; no-ops the hiding for
      // the admin page (admin keeps every facet). Drives both the tab bar and
      // the all-songs 'rated' sort option (read at render time).
      setGuestVisibility: (ratingsVisible, tagsVisible, facets) => {
        if (!_config) return;
        _config.ratingsVisible = !!ratingsVisible;
        _config.tagsVisible = !!tagsVisible;
        if (facets && typeof facets === 'object') _config.facets = facets;
        _applyTabVisibility();
      },
      // Cross-surface navigation (2026-06-10 nav plan U2/U5): pages route
      // name taps here. opts.origin = {label, jump} names the surface the
      // user came from; jump is the page's own return action.
      browseToArtist,
      browseToAlbum,
      // U5 (review fix): lets the admin page apply freshly saved rules to
      // the already-mounted browse view without a page reload.
      refreshPatternRules: async () => {
        await _loadPatternRules();
        if (artistsData) renderArtistsList();
        if (albumsData) renderAlbumsList();
      },
      // 2026-06-11 glow-up U3: switch the rail mode live, no reload. The
      // singleton bakes its mode at build time, so switching means a full
      // teardown in this exact order: abort any in-flight drag, deactivate
      // (hides rail, drops lane vars, disconnects all observers/listeners),
      // remove the stale nav node from whatever host it's parked in, null
      // the singleton so the next _ensureRailSingleton rebuilds under the
      // new mode, THEN flip config and re-render the cached lists (the
      // render path re-activates the rail when the active sort is alpha).
      setRailMode: (mode) => {
        // Five live modes; legacy 'density' maps to waveform (read-edge
        // rule, belt-and-suspenders — U1's API resolvers already map it).
        if (mode === 'density') mode = 'waveform';
        const VALID = ['vanilla', 'magnetic', 'waveform', 'loupe', 'vu'];
        if (VALID.indexOf(mode) === -1) return;
        if (mode === (_config && _config.railMode)) return;
        cancelRailDrag();
        _deactivateRail();
        if (_railSingleton) {
          _railSingleton.remove();
          _railSingleton = null;
        }
        _config.railMode = mode;
        if (artistsData) renderArtistsList();
        if (albumsData) renderAlbumsList();
      },
      // Tile-view U2: switch List/Tile live, no reload (mirrors setRailMode).
      // mountAppearance calls this from _apply with the effective view. Re-
      // renders the cached top-level lists and any active search; an open
      // style-album drill re-renders on its next show.
      setViewMode: (mode) => {
        if (mode !== 'list' && mode !== 'tile') return;
        if (mode === (_config && _config.viewMode)) return;
        _config.viewMode = mode;
        if (artistsData) renderArtistsList();
        if (albumsData) renderAlbumsList();
        if (lastSearchData) renderSearchResults(lastSearchData);
      },
      // International rail (plan 004): apply the install-wide alpha-rail mode +
      // thresholds, no reload. The bucket SET (not the rail's visual mode)
      // changes, so tear the rail down like setRailMode and re-render the cached
      // lists — the render path rebuilds the rail buttons under the new buckets.
      setRailAlpha: (mode, artistThreshold, albumThreshold) => {
        const m = (mode === 'international') ? 'international' : 'english';
        const at = (typeof artistThreshold === 'number' && artistThreshold >= 1) ? artistThreshold : 2;
        const bt = (typeof albumThreshold === 'number' && albumThreshold >= 1) ? albumThreshold : 2;
        if (_config && m === _config.railAlphaMode
            && at === _config.railArtistThreshold && bt === _config.railAlbumThreshold) return;
        cancelRailDrag();
        _deactivateRail();
        if (_railSingleton) { _railSingleton.remove(); _railSingleton = null; }
        _config.railAlphaMode = m;
        _config.railArtistThreshold = at;
        _config.railAlbumThreshold = bt;
        if (artistsData) renderArtistsList();
        if (albumsData) renderAlbumsList();
      },
    };
  }

  // Relative "added X ago" label for a Plex addedAt (epoch SECONDS). Pure and
  // deterministic — `now` (epoch seconds) is injectable for tests. Falsy input
  // → '' so the row simply omits the label. Plan 006 U4.
  function _addedAgo(addedAt, now) {
    if (!addedAt) return '';
    const nowS = (now == null) ? Math.floor(Date.now() / 1000) : now;
    const days = Math.floor(Math.max(0, nowS - addedAt) / 86400);
    if (days < 1) return 'today';
    if (days < 7) return days + 'd ago';
    if (days < 30) return Math.floor(days / 7) + 'w ago';
    if (days < 365) return Math.floor(days / 30) + 'mo ago';
    return Math.floor(days / 365) + 'y ago';
  }

  // Expose to the global scope so the per-page <script> tags can call it.
  window.mountBrowser = mountBrowser;
  // Test-only hook (no runtime caller): exposes the pure sort→dimension→bucket
  // pipeline so tests/test_frontend_regressions.py can verify rail bucketing in
  // a real JS engine. See plan 2026-06-22-002.
  window.__jpComputeBuckets = function (items, sort, nameField, alpha) {
    const dim = resolveRailDimension('artists', sort) || resolveRailDimension('albums', sort);
    const sorted = applySort(items, sort, nameField);
    const r = computeBuckets(sorted, dim, nameField, alpha);
    return { buckets: r.buckets, keyForItem: r.keyForItem, sorted: sorted.map(i => i[nameField]), items: sorted };
  };
  window.__jpAddedAgo = _addedAgo;
})();
