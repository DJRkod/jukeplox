'use strict';

function _esc(s) {
  return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// ── Appearance: color schemes (2026-06-11 glow-up plan U2) ──────────────────
// Scheme ids are the cross-layer contract with SCHEME_IDS in
// app/api/guest.py — keep the id lists in lockstep. Each row carries the
// variable contract (origin R5): anchor (the named hex — fills, swatches,
// tints), ui (lifted tone so deep anchors stay legible as chrome on black),
// on (text color on accent surfaces), grad (optional gradient — Silver's
// metallic treatment), sax (optional CSS filter tinting the 🎷 glyphs;
// null = keep the gold sax, origin R6's degrade rule).
// Surface-aware schemes (2026-06-14 scheme-expansion): in addition to the
// accent contract (anchor/ui/on/grad/sax), a scheme MAY recolor the base via
// optional bg/surface/surface2/text/muted and declare mode 'light'. `bg` may
// be a gradient string (set as `background: var(--bg)`). Omitted surface vars
// fall back to the dark defaults in applyScheme, so accent-only schemes are
// unchanged and switching back from a background/light theme restores cleanly.
const APPEARANCE_SCHEMES = {
  'gold-rush':        { name: 'After the Gold Rush',    anchor: '#e5a00d', ui: '#e5a00d', on: '#111111', grad: null, sax: null },
  'king-crimson':     { name: 'King Crimson',           anchor: '#8f1117', ui: '#c41e25', on: '#ffffff', grad: null, sax: 'sepia(1) saturate(6) hue-rotate(-35deg) brightness(.8)' },
  'case-of-blue':     { name: 'A Case of Blue',         anchor: '#a2c5ef', ui: '#a2c5ef', on: '#021231', grad: null, sax: 'sepia(1) saturate(4) hue-rotate(185deg) brightness(.95)',
                        bg: '#011b5e', surface: '#0a2a78', surface2: '#143a8f', text: '#dce8fb', muted: '#8aa6d6' },
  'onion-green':      { name: 'Onion Green',            anchor: '#009d4c', ui: '#00b859', on: '#06130c', grad: null, sax: 'sepia(1) saturate(4) hue-rotate(85deg) brightness(.85)' },
  'ladyland-orange':  { name: 'Ladyland',               anchor: '#da5d3d', ui: '#f4cd4e', on: '#2a1410', grad: 'linear-gradient(90deg,#c32f35,#da5d3d,#f4cd4e)', sax: 'sepia(1) saturate(5) hue-rotate(-15deg)',
                        bg: 'linear-gradient(160deg,#271c20 0%,#c32f35 46%,#da5d3d 73%,#f4cd4e 100%)', surface: '#1c1310', surface2: '#261a16', text: '#fff4ea', muted: '#ecc8ad' },
  'chasing-rabbits':  { name: 'Chasing Rabbits',        anchor: '#f6ecf0', ui: '#f6ecf0', on: '#16070c', grad: null, sax: 'grayscale(.6) brightness(1.3)' },
  'sympathy-lime':    { name: 'Sympathy is a Lime',     anchor: '#8ACE00', ui: '#8ACE00', on: '#0c1206', grad: null, sax: 'sepia(1) saturate(4) hue-rotate(45deg) brightness(1.05)' },
  'pink-side':        { name: 'Theme From Big Pink',    anchor: '#ef6e85', ui: '#ef6e85', on: '#16070c', grad: null, sax: 'sepia(1) saturate(3) hue-rotate(-55deg) brightness(1.05)' },
  'silver-mountains': { name: 'Silver Mountains',       anchor: '#BCC6CC', ui: '#cdd4d9', on: '#101214', grad: 'linear-gradient(135deg,#e6e7e9,#bcc6cc 55%,#83878d)', sax: 'grayscale(1) brightness(1.15)' },
  'rainy-purple':     { name: 'Rainy Purple',           anchor: '#58156e', ui: '#a23bd6', on: '#ffffff', grad: null, sax: 'sepia(1) saturate(5) hue-rotate(225deg) brightness(.85)' },
  'dark-side':        { name: 'Dark Side',              anchor: '#e9e9ef', ui: '#e9e9ef', on: '#0a0a0c', grad: 'linear-gradient(90deg,#ff2d2d,#ff8a00,#ffe000,#27d07a,#2f8fff,#7a3cff)', sax: null,
                        bg: '#0a0a0c', surface: '#161619', surface2: '#202027', text: '#f4f4f6', muted: '#9a9aa2' },
  'bloody-pink':      { name: 'Bloody Pink',            anchor: '#ff6d97', ui: '#ff6d97', on: '#2a0a16', grad: 'linear-gradient(90deg,#bb305f,#ff6d97)', sax: 'sepia(1) saturate(4) hue-rotate(-50deg) brightness(1.05)',
                        bg: 'linear-gradient(160deg,#2a0a16 0%,#bb305f 55%,#ff6d97 100%)', surface: '#1a1216', surface2: '#251a20', text: '#fff0f5', muted: '#ffc9da' },
  'tubular-blue':     { name: 'Tubular Blue',           anchor: '#1a6fc0', ui: '#155a9e', on: '#ffffff', grad: 'linear-gradient(90deg,#1a6fc0,#4f9ee0)', sax: 'sepia(1) saturate(3) hue-rotate(170deg) brightness(.9)', mode: 'light',
                        bg: 'linear-gradient(160deg,#4f9ee0 0%,#c3dbe8 100%)', surface: 'rgba(255,255,255,.62)', surface2: 'rgba(255,255,255,.82)', text: '#0b2540', muted: '#0f2c49' },
  'peel-slowly':      { name: 'Peel Slowly',            anchor: '#fbce00', ui: '#8a6d00', on: '#1a1206', grad: 'linear-gradient(90deg,#fbce00 0 62%,#ecb69f 62% 82%,#d12d60 82% 100%)', sax: 'sepia(1) saturate(5) hue-rotate(-12deg) brightness(.9)', mode: 'light',
                        bg: '#e9e9e9', surface: '#ffffff', surface2: '#f1f1f1', text: '#0f0809', muted: '#6b5f60' },
  'inertia':          { name: 'Inertia',                anchor: '#010201', ui: '#010201', on: '#ffffff', grad: null, sax: 'grayscale(1)', mode: 'light',
                        bg: '#ffffff', surface: '#ffffff', surface2: '#f4f4f4', text: '#010201', muted: '#7a7a7a' },
  'medusa':           { name: 'Medusa',                 anchor: '#853881', ui: '#a24d9d', on: '#ffffff', grad: 'linear-gradient(90deg,#3a7354 0%,#3a7354 35%,#853881 50%,#853881 85%,#d21c18 100%)', sax: 'sepia(1) saturate(4) hue-rotate(230deg) brightness(.85)',
                        bg: '#0c0c0e', surface: '#171719', surface2: '#212125', text: '#f2f2f4', muted: '#9696a0' },
};

function applyScheme(id) {
  const s = APPEARANCE_SCHEMES[id];
  if (!s) return false;  // unknown id → defensive no-op, page keeps current vars
  const r = document.documentElement.style;
  r.setProperty('--accent', s.anchor);
  r.setProperty('--accent-ui', s.ui);
  r.setProperty('--on-accent', s.on);
  r.setProperty('--accent-grad', s.grad || s.anchor);
  r.setProperty('--accent-strong', `color-mix(in srgb, ${s.ui} 82%, #000)`);
  r.setProperty('--accent-dim', `color-mix(in srgb, ${s.anchor} 13%, transparent)`);
  r.setProperty('--sax-filter', s.sax || 'none');
  // Surface palette — set per scheme, else reset to the dark defaults so a
  // switch back from a background/light theme restores cleanly. `bg` may be a
  // gradient string (consumed by `background: var(--bg)`). data-mode lets CSS
  // target light themes for the chrome audit (light schemes invert text/bg).
  r.setProperty('--bg', s.bg || '#0f0f0f');
  r.setProperty('--surface', s.surface || '#1a1a1a');
  r.setProperty('--surface2', s.surface2 || '#242424');
  r.setProperty('--text', s.text || '#eee');
  r.setProperty('--muted', s.muted || '#888');
  // Chrome tokens (borders/dividers): TRANSLUCENT so they adapt to any base —
  // solid or gradient — instead of the old opaque #333/#1f1f1f that assumed a
  // dark base. Dark mode ≈ the old values over #0f0f0f; light mode flips to
  // translucent black so light themes show hairlines, not harsh dark lines.
  const _light = s.mode === 'light';
  r.setProperty('--border', _light ? 'rgba(0,0,0,.16)' : 'rgba(255,255,255,.14)');
  r.setProperty('--divider', _light ? 'rgba(0,0,0,.10)' : 'rgba(255,255,255,.08)');
  // Empty rating-pip tone (2026-06-26 ratings-and-tags plan U5/R17): flips like
  // --border so pips stay visible on light schemes (a hardcoded translucent
  // white vanishes on white). The fill uses --accent-ui; verified across all 16
  // schemes in docs/brainstorms/mockups/ratings-tags-allschemes.html.
  r.setProperty('--star-empty', _light ? 'rgba(0,0,0,.24)' : 'rgba(255,255,255,.18)');
  // Elevated-surface base: an OPAQUE backing for floating overlays (the sort
  // popover). Some schemes (Tubular Blue, Ladyland, Bloody Pink) make --surface
  // translucent so the gradient bg shows through page chrome; an overlay reusing
  // --surface directly would read the content beneath it. The translucent
  // --surface tint is composited OVER this opaque base, so the panel stays
  // tinted but legible. See docs/solutions/architecture-patterns/
  // surface-aware-theme-model-and-light-mode-chrome.md
  r.setProperty('--elev-base', _light ? '#ffffff' : '#16161b');
  document.documentElement.dataset.mode = s.mode || 'dark';
  return true;
}

// Early bootstrap (R8, flash minimization): apply the device's stored
// scheme at parse time — before any fetch, before the other modules load.
// Storage failures (private mode) fall through to the gold defaults.
try {
  const _bootScheme = localStorage.getItem('jp_scheme');
  if (_bootScheme) applyScheme(_bootScheme);
} catch (_) { /* defaults stand */ }

// ── Appearance: gear panel + per-device overrides (glow-up plan U5) ─────────
// The five live rail modes, in picker order. Glyphs are plain text (no
// LLM-generated imagery — origin rule); titles carry the full names.
const APPEARANCE_RAIL_MODES = [
  { id: 'vanilla',  name: 'Vanilla',  glyph: 'A' },
  { id: 'magnetic', name: 'Magnetic', glyph: 'aA' },
  { id: 'waveform', name: 'Waveform', glyph: '▂▅▃' },
  { id: 'loupe',    name: 'Loupe',    glyph: '⊙' },
  { id: 'vu',       name: 'VU Meter', glyph: '▮▮▯' },
];

// mountAppearance: the SHARED gear→panel component, mounted by BOTH pages
// (single-source per the project standard — never per-page duplicates).
// Owns the /api/appearance default fetch (the per-page /api/rail-mode
// pre-mount fetches are gone as of this unit). Resolution order per knob:
// device override (localStorage jp_scheme / jp_rail) → host default.
// opts.getHandle returns the page's mountBrowser handle (assigned async on
// the pages, hence a getter + null guards rather than a direct reference).
function mountAppearance(opts) {
  const getHandle = (opts && opts.getHandle) || (() => null);
  const defaults = {
    scheme: 'gold-rush', rail_mode: 'vanilla', view: 'list',
    // International rail (plan 004): install-wide, not a per-device override.
    rail_alpha_mode: 'english', rail_artist_threshold: 2, rail_album_threshold: 2,
    // Track ratings + tags (2026-06-26 plan U8): guest-visibility flags + the
    // five Browse-facet toggles. Install-wide (no per-device override), like the
    // rail-alpha defaults; pushed to the browse handle via setGuestVisibility.
    ratings_visible_to_guests: false, tags_visible_to_guests: false, browse_facets: null,
    // Rating display style (2026-06-27 plan U3): install-wide look for the 0–5
    // rating, applied as a :root[data-rating-style] attribute; CSS in rail.css
    // restyles every .trk-pip. Default stars (also the bare-CSS default).
    rating_style: 'stars',
  };

  const _read = (k) => { try { return localStorage.getItem(k); } catch (_) { return null; } };
  const _write = (k, v) => {
    try {
      if (v === null) localStorage.removeItem(k);
      else localStorage.setItem(k, v);
    } catch (_) { /* private mode: live switch still works, no persistence */ }
  };
  const _validRail = (m) => {
    if (m === 'density') return 'waveform';  // legacy read-edge mapping
    return APPEARANCE_RAIL_MODES.some(r => r.id === m) ? m : null;
  };
  // Tile-view U3: List/Tile is the third per-device appearance override.
  const _validView = (v) => (v === 'list' || v === 'tile') ? v : null;
  // International rail (plan 004): fold validated alpha-rail values from a source
  // (the /api/appearance fetch or an appearance_changed event) into defaults.
  // Install-wide, so no per-device override layer — defaults ARE the effective
  // values.
  const _applyAlphaDefaults = (src) => {
    if (!src) return;
    if (src.rail_alpha_mode === 'english' || src.rail_alpha_mode === 'international')
      defaults.rail_alpha_mode = src.rail_alpha_mode;
    if (typeof src.rail_artist_threshold === 'number' && src.rail_artist_threshold >= 1)
      defaults.rail_artist_threshold = src.rail_artist_threshold;
    if (typeof src.rail_album_threshold === 'number' && src.rail_album_threshold >= 1)
      defaults.rail_album_threshold = src.rail_album_threshold;
  };

  function _resolved() {
    const sOv = _read('jp_scheme');
    const rOv = _validRail(_read('jp_rail'));
    const vOv = _validView(_read('jp_view'));
    return {
      scheme: (sOv && APPEARANCE_SCHEMES[sOv]) ? sOv : defaults.scheme,
      rail: rOv || defaults.rail_mode,
      view: vOv || defaults.view,
      hasOverride: !!((sOv && APPEARANCE_SCHEMES[sOv]) || rOv || vOv),
    };
  }

  // Crossfade (2026-06-11 crossfade+build-tag reqs, Item 1): scheme changes
  // applied to a RUNNING page dissolve over 1.2s via the View Transitions
  // API (duration set by ::view-transition CSS in both templates). The
  // first _apply after mount stays instant (R2 — page-load application;
  // _lastScheme === null marks it), as do rail-only changes, unsupported
  // browsers, and prefers-reduced-motion (R5/R6: plain switch, no
  // second-tier animation).
  let _lastScheme = null;

  function _apply() {
    const r = _resolved();
    const doApply = () => {
      applyScheme(r.scheme);
      // Rating display style (plan U3): scheme-independent, install-wide. The
      // persistent `defaults` value means a scheme broadcast re-running doApply
      // re-applies the current style rather than clobbering it.
      document.documentElement.dataset.ratingStyle = defaults.rating_style;
      const h = getHandle();
      if (h && h.setRailMode) h.setRailMode(r.rail);
      if (h && h.setViewMode) h.setViewMode(r.view);
      // International rail is install-wide (no device override), so apply the
      // defaults directly; setRailAlpha is a no-op when nothing changed.
      if (h && h.setRailAlpha) h.setRailAlpha(
        defaults.rail_alpha_mode, defaults.rail_artist_threshold, defaults.rail_album_threshold);
      // Ratings/tags guest visibility + facet tab gating (plan U8). Install-wide,
      // so push defaults directly; the handle no-ops for the admin page.
      if (h && h.setGuestVisibility) h.setGuestVisibility(
        defaults.ratings_visible_to_guests, defaults.tags_visible_to_guests, defaults.browse_facets);
      _refreshPanel();
    };
    const reduced = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
    const animate = _lastScheme !== null && _lastScheme !== r.scheme
      && typeof document.startViewTransition === 'function' && !reduced;
    _lastScheme = r.scheme;
    if (animate) document.startViewTransition(doApply);
    else doApply();
  }

  async function _fetchDefaults() {
    try {
      const resp = await fetch('/api/appearance');
      if (resp.ok) {
        const data = await resp.json();
        if (data && APPEARANCE_SCHEMES[data.scheme]) defaults.scheme = data.scheme;
        const rm = _validRail(data && data.rail_mode);
        if (rm) defaults.rail_mode = rm;
        const vw = _validView(data && data.view);
        if (vw) defaults.view = vw;
        _applyAlphaDefaults(data);
        // Ratings/tags guest visibility + Browse facets (plan U8).
        if (typeof data.ratings_visible_to_guests === 'boolean')
          defaults.ratings_visible_to_guests = data.ratings_visible_to_guests;
        if (typeof data.tags_visible_to_guests === 'boolean')
          defaults.tags_visible_to_guests = data.tags_visible_to_guests;
        if (data.browse_facets && typeof data.browse_facets === 'object')
          defaults.browse_facets = data.browse_facets;
        // Rating display style (plan U3): validate against the known set.
        if (data.rating_style === 'stars' || data.rating_style === 'dots' || data.rating_style === 'bars')
          defaults.rating_style = data.rating_style;
      }
    } catch (_) { /* offline: built-in gold/vanilla defaults stand */ }
    _apply();
  }

  // ── DOM: gear FAB + panel (markup built here; CSS lives in both
  //    templates' style blocks — .appearance-gear / .appearance-panel) ──
  const gear = document.createElement('button');
  gear.type = 'button';
  gear.id = 'appearance-gear';
  gear.className = 'appearance-gear';
  gear.setAttribute('aria-label', 'Appearance settings');
  gear.setAttribute('aria-expanded', 'false');
  gear.textContent = '⚙︎';
  const panel = document.createElement('div');
  panel.id = 'appearance-panel';
  panel.className = 'appearance-panel';
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-label', 'Appearance');
  document.body.appendChild(gear);
  document.body.appendChild(panel);

  function _refreshPanel() {
    if (!panel.classList.contains('open')) return;
    const r = _resolved();
    panel.innerHTML = '';
    const h4s = document.createElement('h4');
    h4s.textContent = 'Color scheme';
    panel.appendChild(h4s);
    const grid = document.createElement('div');
    grid.className = 'ap-swatches';
    // Every scheme shows in stable insertion order; the active one is
    // highlighted in place (.on) rather than hidden. Picking a scheme no
    // longer reshuffles the grid (2026-06-14: 16 schemes made the old
    // hide-active reshuffle disorienting). Mirrors .ap-rail-icon.on and the
    // admin Setup picker's existing .ap-swatch.on highlight.
    Object.keys(APPEARANCE_SCHEMES).forEach(id => {
      const s = APPEARANCE_SCHEMES[id];
      const active = id === r.scheme;
      const sw = document.createElement('button');
      sw.type = 'button';
      sw.className = 'ap-swatch' + (active ? ' on' : '');
      sw.title = s.name;
      sw.setAttribute('aria-label', (active ? 'Current scheme: ' : 'Switch to ') + s.name);
      sw.style.background = s.grad || s.anchor;
      sw.addEventListener('click', () => { _write('jp_scheme', id); _apply(); });
      grid.appendChild(sw);
    });
    panel.appendChild(grid);
    const h4r = document.createElement('h4');
    h4r.textContent = 'Alphabet rail';
    panel.appendChild(h4r);
    const rails = document.createElement('div');
    rails.className = 'ap-rails';
    APPEARANCE_RAIL_MODES.forEach(m => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'ap-rail-icon' + (m.id === r.rail ? ' on' : '');
      b.title = m.name;
      b.setAttribute('aria-label', m.name + ' rail');
      b.textContent = m.glyph;
      b.addEventListener('click', () => { _write('jp_rail', m.id); _apply(); });
      rails.appendChild(b);
    });
    panel.appendChild(rails);
    // Tile-view U3: List/Tile view section. One global toggle; the active one
    // is highlighted in place like the rail icons. Plain-text glyphs only.
    const h4v = document.createElement('h4');
    h4v.textContent = 'View';
    panel.appendChild(h4v);
    const views = document.createElement('div');
    views.className = 'ap-views';
    [['list', 'List', '☰'], ['tile', 'Tiles', '▦']].forEach(([id, label, glyph]) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'ap-view-btn' + (id === r.view ? ' on' : '');
      b.title = label;
      b.setAttribute('aria-label', label + ' view');
      b.textContent = glyph + '  ' + label;
      b.addEventListener('click', () => { _write('jp_view', id); _apply(); });
      views.appendChild(b);
    });
    panel.appendChild(views);
    const reset = document.createElement('button');
    reset.type = 'button';
    reset.className = 'ap-hostdef';
    reset.textContent = 'Use host default';
    reset.disabled = !r.hasOverride;
    reset.addEventListener('click', () => {
      _write('jp_scheme', null);
      _write('jp_rail', null);
      _write('jp_view', null);
      _apply();
    });
    panel.appendChild(reset);
  }

  function _close() {
    panel.classList.remove('open');
    gear.setAttribute('aria-expanded', 'false');
    document.removeEventListener('click', _outside, true);
    document.removeEventListener('keydown', _esc_key);
  }
  function _outside(e) {
    if (!panel.contains(e.target) && e.target !== gear && e.target !== tabBtn) _close();
  }
  function _esc_key(e) { if (e.key === 'Escape') _close(); }
  function _toggle() {
    if (panel.classList.contains('open')) { _close(); return; }
    panel.classList.add('open');
    gear.setAttribute('aria-expanded', 'true');
    _refreshPanel();
    document.addEventListener('click', _outside, true);
    document.addEventListener('keydown', _esc_key);
  }
  gear.addEventListener('click', _toggle);

  // Docked tab-strip entry (gear-visibility reqs, A+C hybrid): on mobile
  // browse tabs the corner belongs to the rail, so the SAME panel opens
  // from a slim ⚙ pinned at the right end of the tab strip instead.
  // Both pages name their strip #tabs; visibility is CSS-gated per page
  // exactly like the FAB. No strip → no docked entry (defensive).
  const tabBtn = document.createElement('button');
  tabBtn.type = 'button';
  tabBtn.className = 'appearance-tab';
  tabBtn.title = 'Appearance';
  tabBtn.setAttribute('aria-label', 'Appearance settings');
  tabBtn.setAttribute('aria-haspopup', 'true');
  tabBtn.textContent = '⚙︎';
  tabBtn.addEventListener('click', _toggle);
  const _strip = document.querySelector('#tabs');
  if (_strip) _strip.appendChild(tabBtn);

  _fetchDefaults();

  return {
    // WS entry: AppearanceChangedEvent carries BOTH current defaults.
    // Each knob re-resolves independently — a device override keeps
    // winning; un-overridden knobs follow the new default live (AE3).
    onAppearanceChanged: (ev) => {
      if (ev && APPEARANCE_SCHEMES[ev.scheme]) defaults.scheme = ev.scheme;
      const rm = _validRail(ev && ev.rail_mode);
      if (rm) defaults.rail_mode = rm;
      const vw = _validView(ev && ev.view);
      if (vw) defaults.view = vw;
      _applyAlphaDefaults(ev);   // International rail follows the host live too.
      // Surprise Me is a server-side flag (not a per-device override), so it
      // applies to everyone the moment the host toggles it — show/hide the
      // already-rendered dock button live (code-review #6, 2026-06-18).
      if (ev && typeof ev.surprise_me_enabled === 'boolean') {
        const dock = document.querySelector('.jp-surprise-dock');
        if (dock) dock.hidden = !ev.surprise_me_enabled;
      }
      // Rating display style follows the host live too (2026-06-27): fold the
      // new value so _apply → doApply sets :root[data-rating-style] without a
      // reload. Install-wide, so no per-device override layer.
      if (ev && (ev.rating_style === 'stars' || ev.rating_style === 'dots' || ev.rating_style === 'bars'))
        defaults.rating_style = ev.rating_style;
      _apply();
    },
    // WS reconnect: a broadcast may have been missed while disconnected.
    onReconnect: _fetchDefaults,
  };
}

// ── Surprise Me (2026-06-17 plan U5) ────────────────────────────────────────
// Shared, single-source: the seed store (the browser's own picks) + the themed
// Now-dock button factory, used by browse/index.js (records picks on add) and
// playback/index.js (renders the button + reads the seed). One home for both
// guest and admin per the shared-UI standard; the option list / button honor
// the active color scheme via the accent vars.

const _SURPRISE_SEED_KEY = 'jp_surprise_seed';
const _SURPRISE_SEED_CAP = 30;

// Record a pick the browser just queued, so a later Surprise press can be
// seeded from "what I've been adding". Newest-first, deduped by id, capped.
function recordSurprisePick(track) {
  if (!track) return;
  const id = track.track_id || track.id;
  if (!id) return;
  try {
    let list;
    try { list = JSON.parse(localStorage.getItem(_SURPRISE_SEED_KEY) || '[]'); }
    catch { list = []; }
    if (!Array.isArray(list)) list = [];
    list = list.filter((p) => p && p.track_id !== id);
    list.unshift({ track_id: id, genre: track.genre || null, artist: track.artist || null });
    if (list.length > _SURPRISE_SEED_CAP) list = list.slice(0, _SURPRISE_SEED_CAP);
    localStorage.setItem(_SURPRISE_SEED_KEY, JSON.stringify(list));
  } catch { /* private mode / quota — seeding silently degrades to random */ }
}

// The browser's own picks, sent with a Surprise press. Empty (fresh visitor /
// cleared storage) → the server resolves to a random track.
function getSurpriseSeed() {
  try {
    const list = JSON.parse(localStorage.getItem(_SURPRISE_SEED_KEY) || '[]');
    return Array.isArray(list) ? list : [];
  } catch { return []; }
}

// Anti-repeat (2026-06-17 plan 005): a short, capped list of the track ids this
// browser was recently *surprised* with, sent as exclusions so remove + re-press
// (and back-to-back presses) won't return the same track. Distinct from the seed
// store; NOT cleared when a track is removed (that's the point — a removed track
// stays excluded for the window).
const _SURPRISE_RECENT_KEY = 'jp_surprise_recent';
const _SURPRISE_RECENT_CAP = 20;

function recordSurprised(trackId) {
  if (!trackId) return;
  try {
    let list;
    try { list = JSON.parse(localStorage.getItem(_SURPRISE_RECENT_KEY) || '[]'); }
    catch { list = []; }
    if (!Array.isArray(list)) list = [];
    list = list.filter((id) => id !== trackId);
    list.unshift(trackId);
    if (list.length > _SURPRISE_RECENT_CAP) list = list.slice(0, _SURPRISE_RECENT_CAP);
    localStorage.setItem(_SURPRISE_RECENT_KEY, JSON.stringify(list));
  } catch { /* private mode / quota — anti-repeat silently degrades */ }
}

function getRecentSurprised() {
  try {
    const list = JSON.parse(localStorage.getItem(_SURPRISE_RECENT_KEY) || '[]');
    return Array.isArray(list) ? list : [];
  } catch { return []; }
}

function _ensureSurpriseStyles() {
  if (document.getElementById('jp-surprise-styles')) return;
  const style = document.createElement('style');
  style.id = 'jp-surprise-styles';
  // Themed-action lineage (.kebab-btn / .filter-tab): accent-dim chip at rest,
  // escalating to the full accent fill on press; legible across all schemes.
  style.textContent = `
    .jp-surprise-dock{width:calc(100% - 1.5rem);margin:.55rem .75rem .2rem;padding:.72rem 1rem;border-radius:10px;font:inherit;font-size:.92rem;font-weight:700;letter-spacing:.01em;display:flex;align-items:center;justify-content:center;gap:.4rem;cursor:pointer;border:1px solid var(--border);background:var(--accent-dim);color:var(--accent-ui,var(--accent));transition:background .14s ease,color .14s ease,transform .12s ease;}
    .jp-surprise-dock:hover{background:color-mix(in srgb,var(--accent) 28%,transparent);}
    .jp-surprise-dock:active{background:var(--accent);color:var(--on-accent);transform:scale(.985);}
    .jp-surprise-dock:focus-visible{outline:2px solid var(--accent-ui,var(--accent));outline-offset:2px;}
    .jp-surprise-dock[disabled]{opacity:.6;cursor:default;}
    .jp-surprise-spark{display:inline-block;animation:jpSurprisePulse 2.6s ease-in-out infinite;}
    @keyframes jpSurprisePulse{0%,100%{transform:scale(1);opacity:1;}50%{transform:scale(1.18);opacity:.8;}}
    /* In-flight working state (2026-06-17 plan 004): spinner replaces the spark,
       chip stays full-strength (NOT the dimmed [disabled] look). The spin
       keyframe is self-contained here so it animates on the admin dock too
       (the template's sentSpin is guest-only). */
    .jp-surprise-spinner{display:none;width:1.05em;height:1.05em;border:2px solid var(--border);border-top-color:var(--accent-ui,var(--accent));border-radius:50%;flex-shrink:0;}
    .jp-surprise-dock.working{cursor:default;}
    .jp-surprise-dock.working:hover{background:var(--accent-dim);}
    .jp-surprise-dock.working .jp-surprise-spark{display:none;}
    .jp-surprise-dock.working .jp-surprise-spinner{display:inline-block;animation:jpSurpriseSpin .7s linear infinite;}
    @keyframes jpSurpriseSpin{to{transform:rotate(360deg);}}
    @media (prefers-reduced-motion: reduce){.jp-surprise-spinner,.jp-surprise-spark{animation:none !important;}}`;
  document.head.appendChild(style);
}

// Build the themed "Surprise Me" button (placement C — Now-tab dock). onClick
// receives the button element so the caller can disable it during the request.
function createSurpriseButton({ label = 'Surprise Me', onClick } = {}) {
  _ensureSurpriseStyles();
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'jp-surprise-dock';
  btn.setAttribute('aria-label', label);
  btn.innerHTML = `<span class="jp-surprise-spark" aria-hidden="true">✨</span>`
    + `<span class="jp-surprise-spinner" aria-hidden="true"></span>`
    + `<span class="jp-surprise-label">${_esc(label)}</span>`;
  if (onClick) btn.addEventListener('click', () => onClick(btn));
  return btn;
}

// createSortControl — the SHARED themed sort dropdown for every browse/search
// sort row (artists, albums, genre→album). Replaces the old native-<select>
// makeSortRow with a custom listbox so the option list honors the active color
// scheme (native <option> lists can't be themed cross-browser). One home for
// both guest and admin per the shared-UI standard.
//
//   createSortControl({ id, options:[[value,label],…], value, onChange }) → rowEl
//
// Behaves like a <select> for pointer + keyboard (Enter/Space/Arrows/Escape),
// closes on outside-tap, clamps to the viewport, and keeps DOM focus on the
// trigger via aria-activedescendant (accessible listbox pattern). The popover
// surface is opaque (--elev-base) so it stays legible on translucent schemes.
function _ensureSortControlStyles() {
  if (document.getElementById('jp-sortctl-styles')) return;
  const style = document.createElement('style');
  style.id = 'jp-sortctl-styles';
  style.textContent = `
    .jp-sortctl-wrap{position:relative;display:inline-block}
    .jp-sortctl-trigger{display:inline-flex;align-items:center;gap:.4rem;font-size:.78rem;
      padding:.3rem .55rem;background:var(--surface2);border:1px solid var(--border);
      color:var(--text);border-radius:6px;cursor:pointer;line-height:1.2}
    .jp-sortctl-trigger:focus-visible{outline:2px solid var(--accent-ui);outline-offset:1px}
    .jp-sortctl-caret{color:var(--muted);font-size:.6rem}
    .jp-sortctl-pop{position:absolute;top:calc(100% + 4px);left:0;z-index:50;min-width:11rem;
      padding:4px;border:1px solid var(--border);border-radius:10px;
      box-shadow:0 10px 30px rgba(0,0,0,.4);max-height:60vh;overflow-y:auto;
      /* opaque base + translucent surface tint on top → legible on
         translucent-surface schemes (Tubular Blue/Ladyland/Bloody Pink). */
      background-color:var(--elev-base,#16161b);
      background-image:linear-gradient(var(--surface),var(--surface))}
    .jp-sortctl-pop[hidden]{display:none}
    .jp-sortctl-pop.jp-rt{left:auto;right:0}
    .jp-sortctl-item{display:flex;align-items:center;justify-content:space-between;gap:.6rem;
      padding:.5rem .55rem;font-size:.82rem;color:var(--text);border-radius:7px;cursor:pointer;
      white-space:nowrap}
    .jp-sortctl-item:hover,.jp-sortctl-item.jp-active{background:var(--surface2)}
    .jp-sortctl-item[aria-selected="true"]{background:var(--accent-dim);color:var(--accent-ui);font-weight:600}
    .jp-sortctl-check{opacity:0;color:var(--accent-ui);font-size:.8rem}
    .jp-sortctl-item[aria-selected="true"] .jp-sortctl-check{opacity:1}
    .jp-sortctl-item[aria-disabled="true"]{opacity:.45;cursor:not-allowed}
    .jp-sortctl-item[aria-disabled="true"]:hover{background:transparent}`;
  document.head.appendChild(style);
}

function createSortControl({ id, options, value, onChange }) {
  _ensureSortControlStyles();
  const opts = options || [];
  let current = (value != null && opts.some(([v]) => v === value)) ? value : (opts[0] && opts[0][0]);
  let activeIdx = Math.max(0, opts.findIndex(([v]) => v === current));

  const row = document.createElement('div');
  row.style.cssText = 'display:flex;align-items:center;gap:.5rem;padding:.5rem .75rem .25rem';

  const lbl = document.createElement('span');
  lbl.style.cssText = 'font-size:.78rem;color:var(--muted)';
  lbl.textContent = 'Sort:';

  const wrap = document.createElement('div');
  wrap.className = 'jp-sortctl-wrap';

  const popId = id + '-pop';
  const labelFor = (v) => { const o = opts.find(([ov]) => ov === v); return o ? o[1] : ''; };

  const trigger = document.createElement('button');
  trigger.type = 'button';
  trigger.id = id;                       // keep the caller's id on the focusable control
  trigger.className = 'jp-sortctl-trigger';
  trigger.setAttribute('aria-haspopup', 'listbox');
  trigger.setAttribute('aria-expanded', 'false');
  trigger.setAttribute('aria-controls', popId);
  trigger.setAttribute('aria-label', 'Sort order');
  trigger.innerHTML = `<span class="jp-sortctl-label">${_esc(labelFor(current))}</span>` +
                      `<span class="jp-sortctl-caret" aria-hidden="true">▾</span>`;

  const pop = document.createElement('div');
  pop.className = 'jp-sortctl-pop';
  pop.id = popId;
  pop.setAttribute('role', 'listbox');
  pop.hidden = true;

  const itemEls = opts.map(([v, l, disabled], i) => {
    const it = document.createElement('div');
    it.className = 'jp-sortctl-item';
    it.id = id + '-opt-' + i;
    it.setAttribute('role', 'option');
    it.setAttribute('aria-selected', v === current ? 'true' : 'false');
    // Optional 3rd tuple element disables an option (visible but unselectable),
    // e.g. Popular when an artist has no Plex popularity data (All Songs R7).
    if (disabled) it.setAttribute('aria-disabled', 'true');
    it.dataset.value = v;
    it.innerHTML = `<span>${_esc(l)}</span><span class="jp-sortctl-check" aria-hidden="true">✓</span>`;
    it.addEventListener('click', () => select(i));
    it.addEventListener('mousemove', () => setActive(i));
    pop.appendChild(it);
    return it;
  });

  function setActive(i) {
    if (i < 0 || i >= itemEls.length) return;
    if (itemEls[activeIdx]) itemEls[activeIdx].classList.remove('jp-active');
    activeIdx = i;
    itemEls[activeIdx].classList.add('jp-active');
    trigger.setAttribute('aria-activedescendant', itemEls[activeIdx].id);
    itemEls[activeIdx].scrollIntoView({ block: 'nearest' });
  }

  function isOpen() { return !pop.hidden; }

  function open() {
    if (isOpen()) return;
    pop.hidden = false;
    pop.classList.remove('jp-rt');
    trigger.setAttribute('aria-expanded', 'true');
    setActive(Math.max(0, opts.findIndex(([v]) => v === current)));
    // Viewport clamp (R9): if the panel overflows the right edge, right-anchor it.
    if (pop.getBoundingClientRect().right > window.innerWidth - 8) pop.classList.add('jp-rt');
    document.addEventListener('click', onDocClick, true);
  }

  function close() {
    if (!isOpen()) return;
    pop.hidden = true;
    trigger.setAttribute('aria-expanded', 'false');
    document.removeEventListener('click', onDocClick, true);
  }

  function select(i) {
    const [v, l, disabled] = opts[i];
    if (disabled) return;   // visible-but-unselectable option (All Songs R7)
    current = v;
    itemEls.forEach((it, j) => it.setAttribute('aria-selected', j === i ? 'true' : 'false'));
    trigger.querySelector('.jp-sortctl-label').textContent = l;
    close();
    trigger.focus();
    if (onChange) onChange(v);
  }

  function onDocClick(e) { if (!wrap.contains(e.target)) close(); }

  trigger.addEventListener('click', (e) => { e.stopPropagation(); isOpen() ? close() : open(); });

  trigger.addEventListener('keydown', (e) => {
    if (!isOpen()) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp' || e.key === 'Enter' || e.key === ' ') {
        e.preventDefault(); open();
      }
      return;
    }
    if (e.key === 'ArrowDown') { e.preventDefault(); setActive(Math.min(activeIdx + 1, itemEls.length - 1)); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActive(Math.max(activeIdx - 1, 0)); }
    else if (e.key === 'Home') { e.preventDefault(); setActive(0); }
    else if (e.key === 'End') { e.preventDefault(); setActive(itemEls.length - 1); }
    else if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); select(activeIdx); }
    else if (e.key === 'Escape') { e.preventDefault(); close(); }
    else if (e.key === 'Tab') { close(); }
  });

  wrap.appendChild(trigger);
  wrap.appendChild(pop);
  row.appendChild(lbl);
  row.appendChild(wrap);
  return row;
}

// createGenreBrowser — shared genre list fetch, cache, render, and paginate.
// Used by both guest and admin pages so the /api/browse/genres response shape
// is parsed in exactly one place.
//
// options:
//   containerId   — ID of the host <div>
//   chipClass     — CSS class applied to each chip <span>
//   onSelect      — callback(genreName) invoked on chip click
//   pageSize      — chips per page; Infinity shows all (default: Infinity)
//   loadingClass  — CSS class for loading/error messages (default: 'loading')
//   showMoreClass — CSS class for the Show More button (default: 'show-more-btn')
//   wrapperStyle  — inline style on the chip wrapper div (default: 'padding:.75rem')
function createGenreBrowser({ containerId, chipClass, onSelect, pageSize, loadingClass, showMoreClass, wrapperStyle }) {
  pageSize      = (pageSize != null) ? pageSize      : Infinity;
  loadingClass  = loadingClass       || 'loading';
  showMoreClass = showMoreClass      || 'show-more-btn';
  wrapperStyle  = wrapperStyle       || 'padding:.75rem';

  let data  = null;
  let shown = 0;

  function _el() { return document.getElementById(containerId); }

  async function load() {
    if (data) { _render(); return; }
    _el().innerHTML = `<div class="${loadingClass}">Loading…</div>`;
    try {
      const resp = await fetch('/api/browse/genres');
      if (!resp.ok) throw new Error(resp.status);
      const json = await resp.json();
      if (!json || !json.length) {
        _el().innerHTML = `<div class="${loadingClass}">No genres found.</div>`;
        return;
      }
      data = json;
      _render();
    } catch {
      _el().innerHTML = `<div class="${loadingClass}">Failed to load genres.</div>`;
    }
  }

  function _render(targetShown) {
    // targetShown (2026-06-10 nav plan U2): re-render preserving a prior
    // pagination depth so backing out of a drill doesn't undo the user's
    // "Show More" presses. Default = first page (original behavior).
    shown = pageSize === Infinity
      ? data.length
      : Math.min(targetShown != null ? targetShown : pageSize, data.length);
    const chipsId = containerId + '-chips';
    const chips = data.slice(0, shown).map(g =>
      `<span class="${chipClass}" data-genre="${_esc(g.name)}">${_esc(g.name)}</span>`
    ).join('');
    _el().innerHTML = `<div id="${chipsId}" style="${wrapperStyle}">${chips}</div>`;
    _el().querySelectorAll('.' + chipClass).forEach(chip =>
      chip.addEventListener('click', () => onSelect(chip.dataset.genre))
    );
    if (data.length > shown) {
      const btn = document.createElement('button');
      btn.className = showMoreClass;
      btn.textContent = 'Show More';
      btn.addEventListener('click', _showMore);
      _el().appendChild(btn);
    }
  }

  function _showMore() {
    const chipsEl = document.getElementById(containerId + '-chips');
    const next = data.slice(shown, shown + pageSize);
    next.forEach(g => {
      const span = document.createElement('span');
      span.className = chipClass;
      span.dataset.genre = g.name;
      span.textContent = g.name;
      span.addEventListener('click', () => onSelect(g.name));
      chipsEl.appendChild(span);
    });
    shown += next.length;
    if (shown >= data.length) {
      _el().querySelectorAll('button.' + showMoreClass).forEach(b => b.remove());
    }
  }

  function reset() { data = null; }
  function hasData() { return data !== null; }

  // Re-render from cache at the CURRENT pagination depth (nav plan U2) —
  // the back-from-drill path; falls back to a fresh load without data.
  function restore() {
    if (!data) { load(); return; }
    _render(shown);
  }

  return { load, reset, hasData, restore };
}

// createStyleAlbumView — genre→album drill-down, shared between guest and admin.
//
// options:
//   containerId      — ID of the host <div> (same div as the genre browser)
//   sortSelectId     — unique DOM ID for the sort <select> (avoid conflicts)
//   backBtnClass     — CSS class for the back button element
//   sectionHeadClass — CSS class for the section heading element
//   loadingClass     — CSS class for loading/error messages (default: 'loading')
//   sortOptions      — [[value, label], …] (default: title/artist/year)
//   onBack           — () => void, called when back is clicked
//   backLabel        — back button text (default: '‹ Genres')
//   backElFn         — optional (goBack, styleName) => HTMLElement; when
//                      provided it REPLACES the default back-btn row (the
//                      wayfinding-bar adoption seam, 2026-06-10 nav plan
//                      U3). goBack bumps the view's generation then calls
//                      onBack — custom elements must use it, not onBack.
//   albumRowFn       — (album) => HTMLElement, renders one album row
//   onAlbumClick     — (album, styleName) => void, called on row click
//
// Returned handle: { show, cancel }. cancel() bumps the generation counter
// (same mechanism as the back button) so an in-flight albums fetch bails
// out instead of appending stale rows into a container that some OTHER
// code path has since re-rendered. Callers that rebuild the container
// outside this view's own back path (e.g., the search renderer on a new
// query / input clear / filter-tab tap) MUST call cancel() first.
function createStyleAlbumView({
  containerId, sortSelectId, backBtnClass, sectionHeadClass, loadingClass,
  sortOptions, onBack, backLabel, backElFn, albumRowFn, onAlbumClick,
  tileRowFn, isTilesFn,
}) {
  sortOptions  = sortOptions  || [
    ['title',       'Title A → Z'],
    ['title_desc',  'Title Z → A'],
    ['artist',      'Artist A → Z'],
    ['artist_desc', 'Artist Z → A'],
    ['year_asc',    'Earliest → Latest'],
    ['year_desc',   'Latest → Earliest'],
  ];
  loadingClass = loadingClass || 'loading';

  let _gen = 0;

  function show(styleName) {
    const gen = ++_gen;
    const el = document.getElementById(containerId);

    // Bump the style-album gen on back so the in-flight per-style albums
    // fetch for THIS view bails out instead of populating the genres list
    // we're re-rendering.
    const goBack = () => { _gen++; onBack(); };
    let backEl;
    if (backElFn) {
      backEl = backElFn(goBack, styleName);
    } else {
      backEl = document.createElement('div');
      backEl.className = backBtnClass;
      backEl.textContent = backLabel || '‹ Genres';
      backEl.addEventListener('click', goBack);
    }

    const headEl = document.createElement('div');
    headEl.className = sectionHeadClass;
    headEl.textContent = styleName;

    const loadEl = document.createElement('div');
    loadEl.className = loadingClass;
    loadEl.textContent = 'Loading…';

    el.innerHTML = '';
    el.appendChild(backEl);
    el.appendChild(headEl);
    el.appendChild(loadEl);

    fetch(`/api/browse/genres/albums?style=${encodeURIComponent(styleName)}`)
      .then(r => r.ok ? r.json() : Promise.reject(r.status))
      .then(albums => {
        if (_gen !== gen) return;
        loadEl.remove();
        if (!albums || !albums.length) {
          el.insertAdjacentHTML('beforeend', `<div class="${loadingClass}">No albums.</div>`);
          return;
        }
        const rowsEl = document.createElement('div');
        let currentSort = 'title';
        const render = () => {
          rowsEl.innerHTML = '';
          const sorted =
            currentSort === 'title_desc'  ? [...albums].sort((a, b) => (b.title  || '').localeCompare(a.title  || '')) :
            currentSort === 'artist'      ? [...albums].sort((a, b) => (a.artist || '').localeCompare(b.artist || '')) :
            currentSort === 'artist_desc' ? [...albums].sort((a, b) => (b.artist || '').localeCompare(a.artist || '')) :
            currentSort === 'year_asc'    ? [...albums].sort((a, b) => (a.year   || 0) - (b.year   || 0)) :
            currentSort === 'year_desc'   ? [...albums].sort((a, b) => (b.year   || 0) - (a.year   || 0)) :
                                            [...albums].sort((a, b) => (a.title  || '').localeCompare(b.title  || ''));
          // Tile-view (2026-06-15 U2): releases within a genre follow the
          // global view setting. In tile mode the rows container becomes a
          // grid and cells come from tileRowFn; the click is still wired here.
          const tiles = !!(isTilesFn && isTilesFn() && tileRowFn);
          rowsEl.className = tiles ? 'tile-grid' : '';
          sorted.forEach(album => {
            const cell = tiles ? tileRowFn(album) : albumRowFn(album);
            cell.addEventListener('click', () => onAlbumClick(album, styleName));
            rowsEl.appendChild(cell);
          });
        };
        const sortRow = createSortControl({
          id: sortSelectId,
          options: sortOptions,
          value: currentSort,
          onChange: (v) => { currentSort = v; render(); },
        });
        el.appendChild(sortRow);
        el.appendChild(rowsEl);
        render();
      })
      .catch(() => {
        if (_gen !== gen) return;
        loadEl.remove();
        el.insertAdjacentHTML('beforeend', `<div class="${loadingClass}">Failed to load albums.</div>`);
      });
  }

  return { show, cancel: () => { _gen++; } };
}
