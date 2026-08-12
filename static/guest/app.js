// Jukeplox Guest App — page chrome only.
//
// Browse / search / track-row / source-picker / album-tracks / artist-albums /
// queue-append rendering lives in static/browse/index.js (shared with admin).
// Now Playing / progress / queue-list / history-strip / micro-bar rendering
// lives in static/playback/index.js (shared with admin).
// See tests/test_static_discipline.py for the authoritative rule.
//
// This file owns: WebSocket lifecycle, lock state, tab navigation, and the
// mount calls that wire the two shared modules with guest-mode config.

'use strict';

// ── State ─────────────────────────────────────────────────────────────────

let isLocked = false;

// ── Toast ─────────────────────────────────────────────────────────────────

const toastEl = document.getElementById('toast');
let toastTimer;
function showToast(msg) {
  toastEl.textContent = msg;
  toastEl.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove('show'), 3000);
}

// ── API helper (used by page-chrome fetches; the shared modules have their own) ─

async function api(method, path, body) {
  const resp = await fetch(path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  return [resp.status, resp.ok ? await resp.json() : null];
}

// ── Own-track receipts (remove-own-queued-tracks U4) ───────────────────────
//
// Best-effort, browser-local ownership: the append receipt this browser
// received IS its proof it queued an entry. We persist receipts here and let
// the queue rows match against them to show a remove (✕) on the guest's own
// upcoming entries. Single adds store one receipt; album adds store a batch
// (removed as a unit). The server enforces nothing about identity — it just
// removes by receipt equality (see /api/queue/undo). Lost on cleared storage /
// other device / private window, which is acceptable for a retract affordance.
// A queue entry is keyed by (track_id, added_at); both arrive on queue items
// via the GET payload and the queue_changed WS payload.
const queueReceipts = {
  KEY: 'jukeplox.queueReceipts',
  _key(e) { return e.track_id + '\u0000' + e.added_at; },
  load() {
    try { return JSON.parse(localStorage.getItem(this.KEY)) || []; }
    catch { return []; }
  },
  _write(list) {
    try { localStorage.setItem(this.KEY, JSON.stringify(list)); }
    catch { /* private mode / quota — ownership silently degrades, no crash */ }
  },
  // receipt: {track_id, added_at} (single) OR {entries:[...]} (album batch).
  // Batch records carry a stable `id` (first entry's key) so the queue
  // decorator can group a contiguous run of the SAME album under one bar.
  save(receipt) {
    const list = this.load();
    if (receipt.entries && receipt.entries.length) {
      list.push({ id: this._key(receipt.entries[0]), entries: receipt.entries });
    } else if (receipt.track_id && receipt.added_at) {
      list.push({ track_id: receipt.track_id, added_at: receipt.added_at });
    }
    this._write(list);
  },
  // Removal descriptor for a queue row, or null when this browser doesn't own
  // it. Single → remove that entry; batch → remove the whole album as a unit
  // (`batchId` groups contiguous rows of the same add under one remove bar).
  ownedFor(item) {
    for (const r of this.load()) {
      if (r.entries) {
        if (r.entries.some(e => this._key(e) === this._key(item))) {
          return { kind: 'batch', batchId: r.id, body: { entries: r.entries } };
        }
      } else if (this._key(r) === this._key(item)) {
        return { kind: 'single', body: { track_id: r.track_id, added_at: r.added_at } };
      }
    }
    return null;
  },
  // Drop the receipt(s) addressing a just-removed entry/batch.
  forget(body) {
    let list = this.load();
    if (body.entries) {
      const gone = new Set(body.entries.map(e => this._key(e)));
      list = list.filter(r => !(r.entries && r.entries.some(e => gone.has(this._key(e)))));
    } else {
      list = list.filter(r => !(!r.entries && this._key(r) === this._key(body)));
    }
    this._write(list);
  },
  // Hygiene: drop receipts whose entries are no longer upcoming (played,
  // host-removed, self-removed) so storage stays bounded. Called on every
  // queue render.
  prune(queueItems) {
    const present = new Set((queueItems || []).map(i => this._key(i)));
    const list = this.load().map(r => {
      if (r.entries) {
        const kept = r.entries.filter(e => present.has(this._key(e)));
        return kept.length ? { id: r.id, entries: kept } : null;
      }
      return present.has(this._key(r)) ? r : null;
    }).filter(Boolean);
    this._write(list);
  },
};

// ── Durable surprise ownership tokens (remove-own-surprise-after-screen-off) ──
//
// Unlike the (track_id, added_at) receipt above — which is only learned from the
// append RESPONSE and is therefore lost when a phone sleeps during a slow
// Surprise Me — an ownership token is generated and persisted HERE, BEFORE the
// request goes out. The server stamps it on the queued row and echoes it on
// every queue payload (GET + queue_changed), so this browser can match its own
// row and keep the remove (✕) even if it never saw the response. A row is owned
// when its `owner_token` is one we reserved.
function _genOwnerToken() {
  // crypto.randomUUID is secure-context-only (absent on plain-http LAN installs,
  // e.g. http://<nas>:80); crypto.getRandomValues works everywhere. Fall back to
  // a time+random string only if crypto is entirely unavailable.
  try {
    const a = new Uint8Array(16);
    crypto.getRandomValues(a);
    return Array.from(a, (b) => b.toString(16).padStart(2, '0')).join('');
  } catch {
    return 'r' + Date.now().toString(16) + Math.random().toString(16).slice(2);
  }
}
const surpriseTokens = {
  KEY: 'jukeplox.surpriseTokens',
  // Grace window: keep a reserved-but-not-yet-echoed token this long so a
  // queue_changed / refresh that fires DURING the resolve (another guest's add,
  // a track advance, or the screen-on resync) can't prune it before its row
  // lands. Must exceed the resolve budget (~8s) plus wake slack — otherwise the
  // feature re-breaks in exactly its target window (the prune-before-land race).
  GRACE_MS: 30000,
  // Bound growth from presses that never produce a row (ok:false / 423 / a lost
  // request the server never processed): keep only the most-recent CAP tokens.
  CAP: 100,
  _raw() {
    try { return JSON.parse(localStorage.getItem(this.KEY)) || []; }
    catch { return []; }
  },
  // Normalize to [{t, ts}]; tolerate the legacy plain-string format (ts=0 → old,
  // so it survives while its row is present and is reaped once the row is gone).
  load() {
    return this._raw()
      .map((e) => (typeof e === 'string' ? { t: e, ts: 0 } : e))
      .filter((e) => e && e.t);
  },
  _write(list) {
    try { localStorage.setItem(this.KEY, JSON.stringify(list.slice(-this.CAP))); }
    catch { /* private mode / quota — ownership silently degrades, no crash */ }
  },
  // Reserve a new token, persist it (with a reserve timestamp) BEFORE the request.
  reserve() {
    const token = _genOwnerToken();
    const list = this.load();
    list.push({ t: token, ts: Date.now() });
    this._write(list);
    return token;
  },
  has(token) { return !!token && this.load().some((e) => e.t === token); },
  forget(token) { this._write(this.load().filter((e) => e.t !== token)); },
  // Hygiene: drop a token only when its row is absent AND it is older than the
  // grace window. A freshly reserved token whose row hasn't landed yet is within
  // grace → KEPT (fixes the prune-before-land race); a played/removed row's token
  // is absent + eventually old → reaped. Called on every queue render.
  prune(queueItems) {
    const present = new Set((queueItems || []).map((i) => i.owner_token).filter(Boolean));
    const now = Date.now();
    this._write(this.load().filter((e) => present.has(e.t) || (now - e.ts) < this.GRACE_MS));
  },
};

// Guest remove plan (unify-queue-remove U3): returns { singles, albums } for the
// shared playback renderer (static/playback/index.js _renderRemovePlan), which
// draws the ✕ chip + full-height album bar. Guest scope = the browser's OWN
// entries only (receipt). Single adds → a chip; an album the guest added → one
// bar over its contiguous run (bar only, no per-row ✕ — album-as-unit). Each
// remove() redeems the receipt via /api/queue/undo, drops it locally, and toasts;
// the server's queue_changed broadcast re-renders for every client.
function guestRemovePlan(items) {
  const remove = async (body, msg, token) => {
    const [status] = await api('POST', '/api/queue/undo', body);
    if (status === 200) {
      queueReceipts.forget(body);
      if (token) surpriseTokens.forget(token);
      showToast(msg);
    } else showToast('Could not remove');
  };
  const singles = [];
  const albums = [];
  let i = 0;
  while (i < items.length) {
    // Token-owned (surprise) rows first: ownership is proven by a pre-stored
    // owner_token the server echoes on the row, so it survives a lost response.
    // The removal body (track_id, added_at) is read straight off the queue row.
    const item = items[i];
    if (surpriseTokens.has(item.owner_token)) {
      const body = { track_id: item.track_id, added_at: item.added_at };
      const token = item.owner_token;
      singles.push({ idx: i, remove: () => remove(body, 'Removed from queue', token) });
      i++;
      continue;
    }
    const owned = queueReceipts.ownedFor(items[i]);
    if (!owned) { i++; continue; }
    if (owned.kind === 'single') {
      const body = owned.body;
      singles.push({ idx: i, remove: () => remove(body, 'Removed from queue') });
      i++;
      continue;
    }
    // batch: gather the contiguous run of the SAME album add.
    const batchId = owned.batchId;
    const body = owned.body;
    let j = i;
    while (j < items.length) {
      const o = queueReceipts.ownedFor(items[j]);
      if (!o || o.kind !== 'batch' || o.batchId !== batchId) break;
      j++;
    }
    const idxs = [];
    for (let k = i; k < j; k++) idxs.push(k);
    albums.push({ idxs, remove: () => remove(body, 'Removed your album') });  // bar only
    i = j;
  }
  return { singles, albums };
}

// ── Lock ──────────────────────────────────────────────────────────────────

const lockBanner = document.getElementById('lock-banner');
function setLocked(locked) {
  isLocked = locked;
  lockBanner.classList.toggle('visible', locked);
  // The shared module checks isLocked() at interaction time (row taps,
  // kebab items) — the old .add-btn disable loop went with the + buttons
  // (collected-library plan U4 review fix).
}

// ── Tabs ──────────────────────────────────────────────────────────────────

const BROWSE_VIEWS = new Set(['search-view', 'artists-view', 'albums-view', 'genres-view', 'years-view', 'mostplayed-view', 'recentlyadded-view', 'radio-view']);

// Shared-module handles, declared up-front (not at their mount site below) so
// switchTab — which reads browseHandle — is safe to call at load time. The mobile
// Now default further down calls switchTab before the mount block runs; declaring
// browseHandle here keeps it out of the Temporal Dead Zone (a ReferenceError there
// aborts all mobile init). The mount IIFE assigns these.
let browseHandle;
let appearanceHandle;

function switchTab(viewId) {
  document.querySelectorAll('.tab').forEach(t =>
    t.classList.toggle('active', t.dataset.view === viewId));
  document.querySelectorAll('.view').forEach(v =>
    v.classList.toggle('active', v.id === viewId));
  // Race-safe guard — browseHandle is assigned only after the pre-mount
  // rail-mode fetch resolves (~10-50ms typical). The Now tab is page
  // chrome, not a browse view; never forward it to the browse module.
  if (browseHandle && BROWSE_VIEWS.has(viewId)) browseHandle.activateView(viewId);
}

document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => switchTab(tab.dataset.view));
});

// Desktop two-pane (2026-06-09 rail plan U6): at >=960px the Now panel is
// permanently docked and its tab is hidden — if the window crosses into
// desktop while the Now tab is active, land on Search so the library pane
// isn't blank. The only stateful seam in the resize path; everything else
// is the media query above.
const desktopMq = window.matchMedia('(min-width: 960px)');
function handleDesktopChange() {
  if (!desktopMq.matches) return;
  const nowTab = document.querySelector('.tab[data-view="now-view"]');
  if (nowTab && nowTab.classList.contains('active')) switchTab('search-view');
}
if (desktopMq.addEventListener) desktopMq.addEventListener('change', handleDesktopChange);
else if (desktopMq.addListener) desktopMq.addListener(handleDesktopChange);
handleDesktopChange();

// Mobile default tab → Now (2026-06-17 plan 006 U1). The HTML ships search-view
// active so desktop is correct out of the gate (the docked Now pane is a sibling
// of #browse-container — an inactive search-view would blank the desktop library
// pane). On a phone-width viewport there's no docked Now, so open on Now instead.
// Recomputed every load (no persistence, R4); the desktopMq 'change' handler
// above moves off Now on a mobile→desktop resize.
if (!desktopMq.matches) switchTab('now-view');

// ── Shared playback module ─────────────────────────────────────────────────

const playbackHandle = mountPlayback({
  microBar: { el: '#micro-bar', onTap: () => switchTab('now-view') },
  nowPlaying: { el: '#np-panel', progressEl: '#np-progress' },   // no onSeek — guests can't seek
  // Remove (✕) on the guest's OWN upcoming entries only, via the shared
  // remove renderer: single → chip, owned album → one full-height bar
  // (album-as-unit, no per-row ✕). The plan supplies ownership + remove
  // actions; the shared module draws it (unify-queue-remove U3).
  queue: { el: '#queue-list', removePlan: guestRemovePlan },
  history: { el: '#history-strip' },
  toast: showToast,
  // Surprise ownership is proven by a durable owner_token reserved BEFORE the
  // press (see reserveSurpriseToken) — not by the response receipt, which is
  // lost when the phone sleeps mid-resolve. So the happy path here just needs to
  // re-render; the token (already persisted) makes the row removable regardless.
  reserveSurpriseToken: () => surpriseTokens.reserve(),
  onQueued: () => { refreshQueueState(); },
  // Nav plan U5 wiring: name taps route to the shared browse module's
  // navigation API; the origin link returns here via the tab strip.
  onNameTap: (t) => {
    if (!browseHandle) return;
    const origin = {
      label: t.surface === 'queue' ? 'Queue' : 'Now Playing',
      jump: () => switchTab('now-view'),
    };
    if (t.kind === 'artist') browseHandle.browseToArtist(t.name, { origin });
    else browseHandle.browseToAlbum(t.albumId, t.name, { origin });
  },
  onPlayState: (playing) =>
    document.getElementById('now-dot').classList.toggle('on', playing),
  // Radio Mode now-playing (2026-08-11 plan U10): guest STOP is ALWAYS allowed
  // (R9) — POST /api/radio/stop, which resumes the held queue; the WS radio_state
  // broadcast repaints the widget. "Browse stations" routes to the Radio tab (a
  // read-only browse action always allowed); start/switch there is server-gated.
  // authMode:'guest' → the module dims the control-adjacent affordance when
  // guest_radio_control is off (cosmetic; the server enforces).
  authMode: 'guest',
  onRadioStop: () => api('POST', '/api/radio/stop'),
  onRadioBrowse: () => switchTab('radio-view'),
  // Idle "add music" nudge (2026-06-17 plan 006 U2): when the mobile Now tab is
  // idle (nothing playing, empty queue) the shared module shows a nudge that
  // routes here → the Search tab. Top-level config (like onNameTap/onPlayState)
  // so the module's _setIdle can reach it; admin passes none, so no nudge there.
  onFindMusic: () => switchTab('search-view'),
});

// Queue/history snapshot refetch — used on initial load, WS reconnect, and
// tab-refocus. WS gaps (locked phone, network blip) drop queue_changed
// events and the server sends no snapshot on reconnect, so any resync path
// must re-pull the queue, not just now-playing/position.
async function refreshQueueState() {
  const [, queue] = await api('GET', '/api/queue');
  if (queue) {
    queueReceipts.prune(queue.queue);  // bound storage; drop receipts for gone entries
    surpriseTokens.prune(queue.queue); // and the durable ownership tokens
    playbackHandle.applyQueue(queue.queue, queue.history);
    // Hydrate/resync browse embers from the snapshot (load / ws.onopen /
    // visibilitychange), so a fresh navigator and a dropped-frame client both
    // converge (added-to-queue plan).
    if (browseHandle) browseHandle.applyQueue(queue.queue);
    setLocked(queue.is_locked);
  }
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden) playbackHandle.suspend();
  else { playbackHandle.resume(); refreshQueueState(); }
});

// ── WebSocket ──────────────────────────────────────────────────────────────

let ws;
let wsBackoff = 1000;

function connectWS() {
  if (ws) { ws.onclose = null; ws.close(); }
  ws = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws`);
  ws.onopen = () => {
    wsBackoff = 1000;
    // Events broadcast while the socket was down are gone — re-pull the
    // queue snapshot so strips/lists don't stay stale until the next event.
    // resume() refetches now-playing (whose output_session field carries the
    // outage note), mirroring the visibilitychange handler above — a guest
    // reconnecting mid-outage converges instead of waiting for a delta.
    playbackHandle.resume();
    refreshQueueState();
    // Glow-up U5: appearance defaults may also have changed while down.
    if (appearanceHandle) appearanceHandle.onReconnect();
  };
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'now_playing_changed') playbackHandle.applyNowPlaying(msg);
    else if (msg.type === 'playback_state_changed') playbackHandle.applyPlaybackState(msg);
    else if (msg.type === 'radio_state') {
      // Radio Mode (2026-08-11 plan U9): repaint the Radio-tab active-dot +
      // station-card indicators live. Null-guard mirrors the browse embers —
      // a broadcast can race first paint before the browse module mounts.
      if (browseHandle && browseHandle.applyRadioState) browseHandle.applyRadioState(msg);
      // Radio now-playing surface (plan U10): the same event drives the shared
      // playback widget (station/state/live-title/queue-paused notice + STOP).
      // Distinct name from browse's applyRadioState (FE6 — no cross-module
      // collision on the shared handle).
      playbackHandle.applyRadioNowPlaying(msg);
    }
    else if (msg.type === 'closing_time') playbackHandle.applyClosingTime(msg);
    else if (msg.type === 'output_session') playbackHandle.applyOutputSession(msg);
    else if (msg.type === 'track_skipped') playbackHandle.showSkipped(msg);
    else if (msg.type === 'queue_changed') {
      queueReceipts.prune(msg.queue);
      surpriseTokens.prune(msg.queue);
      playbackHandle.applyQueue(msg.queue, msg.history);
      // Feed browse queue membership so track-row embers update live for every
      // client (added-to-queue plan). Null-guard: browse mounts in an IIFE and a
      // broadcast can race first paint (the appearanceHandle guard precedent).
      if (browseHandle) browseHandle.applyQueue(msg.queue);
      setLocked(msg.is_locked);
    } else if (msg.type === 'lock_changed') {
      setLocked(msg.is_locked);
    } else if (msg.type === 'appearance_changed') {
      // Null-guard: the engine mounts in an IIFE below; a broadcast can
      // race the first paint (the switchTab guard precedent).
      if (appearanceHandle) appearanceHandle.onAppearanceChanged(msg);
    }
  };
  ws.onclose = () => {
    wsBackoff = Math.min(wsBackoff * 2, 30000);
    setTimeout(connectWS, wsBackoff);
  };
}

connectWS();

// ── Mount shared browse module ─────────────────────────────────────────────
//
// Glow-up U5: the appearance engine (shared.js) owns the /api/appearance
// fetch and applies the resolved rail mode via the browse handle — the
// old per-page /api/rail-mode pre-mount fetch is gone. The rail starts
// vanilla and switches live once defaults/overrides resolve (do not gate
// the UI on settings).

(() => {
  browseHandle = mountBrowser('#browse-container', {
    authMode: 'guest',
    isLocked: () => isLocked,
    toast: showToast,
    // Persist the append receipt so the queued entry gets a remove (✕) in the
    // queue, then re-render. The re-render is REQUIRED: the server broadcasts
    // queue_changed before the POST response returns, so the WS-driven render
    // can paint the new row before the receipt is saved — leaving it without a
    // ✕ until the next render. refreshQueueState after the (synchronous) save
    // guarantees a render with the receipt present (race fix).
    onQueued: (receipt) => { queueReceipts.save(receipt); refreshQueueState(); },
  });
  appearanceHandle = mountAppearance({ getHandle: () => browseHandle, getPlaybackHandle: () => playbackHandle });
})();

// ── Initial load ──────────────────────────────────────────────────────────

(async () => {
  await playbackHandle.resume();   // fetches now-playing + position, paints panel + micro-bar
  await refreshQueueState();
})();
