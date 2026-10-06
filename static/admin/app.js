// Jukeplox Admin Dashboard — page chrome only.
//
// Browse / search / queue-append rendering lives in static/browse/index.js;
// Now Playing / progress / queue-list / history-strip rendering lives in
// static/playback/index.js (both shared with the guest page). See
// tests/test_static_discipline.py for the authoritative rule.
//
// This file owns: WebSocket lifecycle, page-level Jukebox/Setup tabs,
// transport buttons (Skip Back | Play/Pause | Skip Forward), volume,
// queue-row decoration (DnD + actions), and all Setup-tab chrome
// (output picker, libraries, Plex connect, settings, sign-out).

'use strict';

// ── Toast ─────────────────────────────────────────────────────────────────

const toastEl = document.getElementById('toast');
let toastTimer;
function showToast(msg) {
  toastEl.textContent = msg;
  toastEl.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove('show'), 3000);
}

// ── API helpers ────────────────────────────────────────────────────────────

async function api(method, path, body) {
  const resp = await fetch(path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!resp.ok) {
    // Attach status + parsed JSON detail so callers can branch on
    // structured errors (plexplayer plan U6: the output-switch confirm
    // rides a 409 whose detail is a DICT — reason/stranded_count — and
    // must be distinguishable from plain-string 409s). detail stays
    // undefined on a non-JSON body; every existing `catch {}` caller is
    // unaffected (still a thrown Error).
    let detail;
    try { detail = (await resp.json()).detail; } catch { /* non-JSON body */ }
    const err = new Error(`${method} ${path} → ${resp.status}`);
    err.status = resp.status;
    err.detail = detail;
    throw err;
  }
  return resp.json();
}

// ── Page-level tabs (Jukebox / Setup) ──────────────────────────────────────
// Tab state lives in the URL hash so a refresh keeps the active tab.
// Legacy sidebar anchors (#output etc.) map to their owning tab.

const LEGACY_ANCHOR_TABS = {
  '#now-playing': 'jukebox', '#queue': 'jukebox', '#browse': 'jukebox',
  '#output': 'setup', '#libraries': 'setup', '#settings': 'setup',
};

function applyPageTab(name) {
  const tab = (name === 'setup') ? 'setup' : 'jukebox';
  document.querySelectorAll('.ptab').forEach(b =>
    b.classList.toggle('active', b.dataset.ptab === tab));
  document.getElementById('pview-jukebox').classList.toggle('active', tab === 'jukebox');
  document.getElementById('pview-setup').classList.toggle('active', tab === 'setup');
}

function _pageTabFromHash() {
  const hash = location.hash || '';
  if (hash === '#setup') return 'setup';
  if (hash === '#jukebox') return 'jukebox';
  return LEGACY_ANCHOR_TABS[hash] || 'jukebox';
}

document.querySelectorAll('.ptab').forEach(btn => {
  btn.addEventListener('click', () => {
    // Dirty-state guard (pattern-rules plan U4, review decision; widened by
    // the 2026-10-05 save-model U4): leaving Setup with uncommitted edits
    // discards them silently, so ask first. The count is the same one the
    // unsaved-changes bar renders — the rule editors are folded into it — so
    // this is ONE guard reading one source, not two guards prompting twice on
    // a single click. A declined prompt returns without changing the hash, so
    // the page and every pending mark stay exactly as they were.
    const pending = window.__jpPendingCount || 0;
    if (pending > 0 && !confirm(
      pending === 1
        ? 'You have 1 unsaved change. Leave Setup and lose it?'
        : 'You have ' + pending + ' unsaved changes. Leave Setup and lose them?'
    )) return;
    location.hash = btn.dataset.ptab;
  });
});
// Tab close and reload (save-model U4). The .ptab guard only ever covered
// in-page navigation; there was no beforeunload listener anywhere in this file,
// so closing the tab on a page of unsaved edits lost them without a word. The
// message cannot be customised — browsers show their own generic prompt — and
// the listener must stay silent at a count of zero or it prompts on every exit.
window.addEventListener('beforeunload', (e) => {
  if (!(window.__jpPendingCount > 0)) return;
  e.preventDefault();
  e.returnValue = '';
  return '';
});

window.addEventListener('hashchange', () => applyPageTab(_pageTabFromHash()));
applyPageTab(_pageTabFromHash());

// ── Queue row decoration (admin extras over the shared module's rows) ─────
// The shared playback module renders pos/art/title/artist; admin prepends
// the drag handle, appends Play Next / Remove, and wires HTML5 DnD.

let dragSrc = null;

function onDragStart(e) {
  dragSrc = this;
  this.classList.add('dragging');
  e.dataTransfer.effectAllowed = 'move';
}
function onDragOver(e) { e.preventDefault(); this.classList.add('drag-over'); }
function onDragLeave() { this.classList.remove('drag-over'); }
function onDragEnd() { this.classList.remove('dragging'); document.querySelectorAll('.drag-over').forEach(el => el.classList.remove('drag-over')); }
async function onDrop(e) {
  e.preventDefault();
  this.classList.remove('drag-over');
  if (!dragSrc || dragSrc === this) return;
  const fromPos = parseInt(dragSrc.dataset.idx, 10);
  const toPos = parseInt(this.dataset.idx, 10);
  dragSrc = null;
  try { await api('POST', '/admin/queue/move', { from_position: fromPos, to_position: toPos }); }
  catch { showToast('Reorder failed'); }
}

async function queuePlayNext(pos) {
  try { await api('POST', `/admin/queue/${pos}/play-next`); }
  catch { showToast('Error promoting track'); }
}
async function queueRemove(pos) {
  try { await api('DELETE', `/admin/queue/${pos}`); }
  catch { showToast('Error removing track'); }
}

function decorateQueueRow(row, item, idx) {
  row.draggable = true;
  const handle = document.createElement('span');
  handle.className = 'drag-handle';
  handle.textContent = '⠿';
  row.prepend(handle);
  const actions = document.createElement('div');
  actions.className = 'qi-actions';
  // Play-Next stays here (admin-only); the remove ✕ now comes from the shared
  // remove renderer (adminRemovePlan → the unified .qi-remove chip), appended
  // into this same .qi-actions slot (unify-queue-remove U4).
  actions.innerHTML = `
    <button class="btn btn-icon btn-sm" data-action="next" title="Play Next">⤴</button>`;
  actions.querySelector('[data-action=next]').addEventListener('click', () => queuePlayNext(idx));
  row.appendChild(actions);
  row.addEventListener('dragstart', onDragStart);
  row.addEventListener('dragover', onDragOver);
  row.addEventListener('dragleave', onDragLeave);
  row.addEventListener('drop', onDrop);
  row.addEventListener('dragend', onDragEnd);
}

// Admin remove plan (unify-queue-remove U4): returns { singles, albums } for the
// shared playback remove renderer. Admin scope = EVERY row. Albums are grouped
// by consecutive same-album_id metadata (admin has no receipts): a run of >=2
// gets the full-height bar (removes the whole album in one call via the
// entry-based /api/queue/undo) AND keeps a per-row ✕ on each album row so an
// admin can still pluck one track. Non-album rows get a chip. Single-row removal
// stays on the position DELETE (queueRemove(idx) — admin renders the full,
// untruncated queue, so render index == queue position, as the existing
// Play-Next/Remove wiring already assumes). Drag + Play-Next come from
// decorateQueueRow.
function adminRemovePlan(items) {
  const removeEntries = async (entries) => {
    const [status] = await api('POST', '/api/queue/undo', { entries });
    if (status !== 200) showToast('Could not remove album');
    // success re-renders via the queue_changed broadcast
  };
  const singles = [];
  const albums = [];
  let i = 0;
  while (i < items.length) {
    const aid = items[i].album_id;
    let j = i + 1;
    if (aid) { while (j < items.length && items[j].album_id === aid) j++; }
    if (aid && j - i >= 2) {
      const idxs = [];
      const entries = [];
      for (let k = i; k < j; k++) {
        idxs.push(k);
        entries.push({ track_id: items[k].track_id, added_at: items[k].added_at });
      }
      const rowRemovers = idxs.map((idx) => () => queueRemove(idx));
      albums.push({ idxs, remove: () => removeEntries(entries), rowRemovers });
      i = j;
    } else {
      const idx = i;
      singles.push({ idx, remove: () => queueRemove(idx) });
      i++;
    }
  }
  return { singles, albums };
}

// ── Shared playback module ─────────────────────────────────────────────────

const btnPrev = document.getElementById('btn-prev');
const btnPause = document.getElementById('btn-pause');
let isPlaying = false;

const playbackHandle = mountPlayback({
  nowPlaying: {
    el: '#np-panel',
    progressEl: '#np-progress',
    onSeek: (ms) => api('POST', '/admin/playback/seek', { position_ms: ms }),
  },
  queue: { el: '#queue-list', decorateRow: decorateQueueRow, removePlan: adminRemovePlan },
  // History strip is READ-ONLY on both pages. The admin "remove this play"
  // affordance moved off the shared strip into the Setup → Recent Plays panel
  // (see renderRecentPlays below), so the strip passes no removePlan hook and
  // the shared module renders no ✕ for anyone.
  history: { el: '#history-strip' },
  toast: showToast,
  // Closing Time (2026-06-24): admin is a control surface — render the banner
  // NON-blocking (a click-through bar with Resume) so the host can keep queueing
  // and skip/resume to restart, instead of being locked out by the send-off.
  controlSurface: true,
  onClosingResume: () => api('POST', '/admin/playback/resume'),
  // Nav plan U5 wiring: name taps drive the shared browse pane (always
  // visible beside playback on the jukebox view); origin jump just brings
  // the playback panel back into view.
  onNameTap: (t) => {
    if (!browseHandle) return;
    const origin = {
      label: t.surface === 'queue' ? 'Queue' : 'Now Playing',
      jump: () => {
        const p = document.getElementById('np-panel');
        if (p) p.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      },
    };
    if (t.kind === 'artist') browseHandle.browseToArtist(t.name, { origin });
    else browseHandle.browseToAlbum(t.albumId, t.name, { origin });
  },
  onPlayState: (playing) => {
    isPlaying = playing;
    btnPause.textContent = playing ? '⏸' : '▶';
  },
  // Radio Mode now-playing (2026-08-11 plan U10): admin has full control. STOP
  // resumes the held queue (admin override, always permitted). "Browse stations"
  // activates the shared Radio browse view (start/switch lives there, U9).
  // authMode:'admin' → the module never dims the control-adjacent affordance.
  authMode: 'admin',
  onRadioStop: () => api('POST', '/admin/radio/stop'),
  onRadioBrowse: () => { if (browseHandle && browseHandle.activateView) browseHandle.activateView('radio-view'); },
  // Supervisor plan U4: every applyOutputSession hydration path (WS event,
  // snapshot re-pull, resume()'s /api/now-playing refetch) also refreshes the
  // admin-rich banner — renderOutputSessionBanner degrades to the generic
  // message when the lean payload omits device/attempt/retry detail.
  onOutputSession: renderOutputSessionBanner,
});

// Queue/history snapshot refetch — used on initial load, WS reconnect, and
// tab-refocus. WS gaps (locked phone, network blip) drop queue_changed
// events and the server sends no snapshot on reconnect; without a re-pull,
// the Skip Back button's disabled state and the queue list go stale until
// the next queue mutation. Mirrors the loadVolume() onopen-resync pattern.
async function refreshQueueState() {
  // Snapshot ordering guard (review fix JFR-1, mirroring the shared
  // module's resume()): a WS output_session push landing during the fetch
  // await bumps the generation — the older snapshot must not overwrite it.
  const osGen = playbackHandle.outputSessionGen();
  try {
    const state = await api('GET', '/admin/queue');
    playbackHandle.applyQueue(state.queue, state.history);
    // Hydrate/resync browse embers from the snapshot (load / ws.onopen /
    // visibilitychange) — fresh navigator + dropped-frame client both converge
    // (added-to-queue plan).
    if (browseHandle) browseHandle.applyQueue(state.queue);
    setRecentPlaysData(state.history);
    syncLock(state.is_locked);
    _historyEmpty = !(state.history && state.history.length);
    _syncPrevEnabled();
    // Output-session resync (supervisor plan U4): a WS gap can drop the
    // output_session delta, so every snapshot re-pull re-renders the outage
    // note + admin banner from the mirrored field (same shape as the event).
    if (state.output_session && osGen === playbackHandle.outputSessionGen()) {
      playbackHandle.applyOutputSession(state.output_session);
      renderOutputSessionBanner(state.output_session);
    }
    return state;
  } catch { return null; }
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden) playbackHandle.suspend();
  else { playbackHandle.resume(); refreshQueueState(); }
});

// ── Transport: Skip Back | Play/Pause | Skip Forward ───────────────────────

btnPrev.addEventListener('click', async () => {
  btnPrev.disabled = true;
  try { await api('POST', '/admin/playback/previous'); }
  catch { showToast('Skip Back failed'); }
  finally { _syncPrevEnabled(); }
});

// HTML `disabled` is driven by the latest queue_changed payload's history;
// the endpoint's 409 remains the race-condition safety net (history can
// empty between the last event and the press).
let _historyEmpty = true;
function _syncPrevEnabled() { btnPrev.disabled = _historyEmpty; }

btnPause.addEventListener('click', async () => {
  btnPause.disabled = true;
  try {
    // playback_state_changed will flow back through the WS and update the
    // module + glyph; the optimistic flip here just keeps the UI snappy.
    if (isPlaying) {
      await api('POST', '/admin/playback/pause');
      btnPause.textContent = '▶';
    } else {
      await api('POST', '/admin/playback/resume');
      btnPause.textContent = '⏸';
    }
  } catch { showToast('Playback error'); } finally { btnPause.disabled = false; }
});

document.getElementById('btn-skip').addEventListener('click', async () => {
  const btn = document.getElementById('btn-skip');
  btn.disabled = true;
  try { await api('POST', '/admin/playback/skip'); }
  catch { showToast('Skip failed'); }
  finally { btn.disabled = false; }
});

// ── Output-session outage banner (supervisor plan U4, R20) ─────────────────
// Admin-rich detail layered over the shared lean note (which lives in
// static/playback/index.js): device name, attempt count, next-retry delay,
// and the manual Resume affordance. ONE render path for both the
// output_session WS event and the /admin/queue snapshot's output_session
// field — the payloads share a shape, mirroring applyDevicesPayload. Kept
// deliberately minimal (static retry text, no ticking countdown) pending the
// interaction-state mockups pass the plan defers to.
function renderOutputSessionBanner(data) {
  const banner = document.getElementById('output-outage-banner');
  const msgEl = document.getElementById('output-outage-msg');
  const resumeBtn = document.getElementById('btn-outage-resume');
  if (!banner || !msgEl || !data) return;
  if (!data.held) {
    // A gapless degrade is not an outage: nothing is held and there is
    // nothing to resume, so the old `!data.held` early return hid it
    // completely. It still has to surface, because it is the one state where
    // the product is quietly not doing what the toggle says it is doing —
    // the operator turned gapless on and is hearing a gap at every track.
    // Same banner, Resume hidden (there is nothing for it to resume).
    if (data.gapless_degraded_reason) {
      msgEl.textContent = '⚠ Gapless is on but not running — '
        + data.gapless_degraded_reason;
      if (resumeBtn) resumeBtn.style.display = 'none';
      banner.style.display = 'flex';
      return;
    }
    banner.style.display = 'none';
    return;
  }
  if (resumeBtn) resumeBtn.style.display = '';
  const device = data.device_name || data.device_id || 'The output device';
  let msg;
  if (data.state === 'reconnecting') {
    msg = '⚠ “' + device + '” is offline — reconnecting (attempt '
      + (data.attempts || 1) + ')…';
  } else if (data.state === 'idle_paused'
      && data.idle_paused_reason === 'foreign_controller') {
    // Plex player foreign-controller yield (2026-08-04-002 plan U7): the
    // device is REACHABLE but another controller owns its queue — this is
    // a yield, not an outage, so the copy asks for a re-activate (the
    // banner's Resume button IS the re-activate) instead of implying a
    // reconnect is coming.
    msg = '⚠ Another Plex controller took the device — re-activate to '
      + 'resume jukeplox control.';
  } else if (data.state === 'idle_paused') {
    const why = {
      window_expired: 'the resume window expired',
      flap_guard: 'it kept dropping out (flap guard)',
      closing_time: 'Closing Time is active',
      track_identity: 'the held track changed identity',
      no_media_source: 'no media source is connected',
    }[data.idle_paused_reason] || 'auto-resume is off';
    msg = '⚠ “' + device + '” is back but playback stays paused — ' + why
      + '. Resume continues from where it stopped.';
  } else if (data.state === 'paused') {
    msg = '⚠ “' + device + '” reconnected paused (it was paused before the '
      + 'outage) — Resume continues playback.';
  } else {
    msg = '⚠ “' + device + '” is offline — playback paused, queue held';
    const bits = [];
    if (data.attempts) bits.push('attempt ' + data.attempts);
    if (data.next_retry_s) bits.push('retrying in ~' + Math.round(data.next_retry_s) + 's');
    if (bits.length) msg += ' (' + bits.join(', ') + ')';
    msg += '.';
  }
  msgEl.textContent = msg;
  banner.style.display = 'flex';   // inline styles lay out msg + Resume in a row
}

// Resume button: the R17 manual resume (POST /admin/playback/resume routes to
// the supervisor while held; 409 = device still unreachable, hold intact).
// Wired once here — renderOutputSessionBanner only mutates text/visibility.
document.getElementById('btn-outage-resume').addEventListener('click', async () => {
  const btn = document.getElementById('btn-outage-resume');
  btn.disabled = true;
  try { await api('POST', '/admin/playback/resume'); }
  catch { showToast('Device still unreachable — retrying'); }
  finally { btn.disabled = false; }
});

// ── Volume ─────────────────────────────────────────────────────────────────

const volSlider = document.getElementById('volume-slider');
const volLabel = document.getElementById('vol-label');
let volTimer;
let _volUserLastSet = 0;
volSlider.addEventListener('input', () => {
  volLabel.textContent = `${Math.round(volSlider.value * 100)}%`;
  volSlider.style.setProperty('--vol-fill', `${volSlider.value * 100}%`);  // gradient fill (2026-06-18)
  _volSyncButtons();
  // Stamp on raw input (not inside the debounce) so the echo-guard window
  // covers the user's whole drag, not just the post-debounce write.
  _volUserLastSet = Date.now();
  clearTimeout(volTimer);
  volTimer = setTimeout(async () => {
    try { await api('POST', '/admin/playback/volume', { level: parseFloat(volSlider.value) }); }
    catch { showToast('Volume error'); }
  }, 300);
});

async function loadVolume() {
  // Skip if user moved the slider less than 4 seconds ago
  if (Date.now() - _volUserLastSet < 4000) return;
  try {
    const data = await api('GET', '/admin/playback/volume');
    const level = data.level ?? 0.5;
    volSlider.value = level;
    volLabel.textContent = `${Math.round(level * 100)}%`;
    volSlider.style.setProperty('--vol-fill', `${level * 100}%`);
    _volSyncButtons();
  } catch {}
}

// Client-side echo guard for device-pushed VolumeChangedEvent (KTD3).
// Server-side _vol_last_set suppresses most echoes; this second window
// closes slow-network and out-of-order-event cases without a tombstone.
function applyVolumeFromEvent(level) {
  if (Date.now() - _volUserLastSet < 2000) return;
  if (!Number.isFinite(level)) return;  // rejects NaN, Infinity, non-numbers
  const clamped = Math.max(0, Math.min(1, level));
  volSlider.value = clamped;
  volLabel.textContent = `${Math.round(clamped * 100)}%`;
  volSlider.style.setProperty('--vol-fill', `${clamped * 100}%`);
  _volSyncButtons();
}

// ±5% nudge buttons (2026-08-04 volume rework U2). Snap to the nearest
// multiple of 5 in the pressed direction (47% → 50% up, 45% down); math in
// the integer-percent domain so float artifacts can't miss the grid. The
// write path is REUSED, not duplicated: set the slider value and dispatch a
// synthetic 'input' so the existing listener does label/fill/echo-stamp/
// debounced POST — rapid taps coalesce into one device write. Buttons carry
// the disabled attribute at the bounds (mockup pick); synced everywhere the
// slider value changes outside the input listener.
function _volSyncButtons() {
  const p = Math.round(volSlider.value * 100);
  document.getElementById('vol-down').disabled = p <= 0;
  document.getElementById('vol-up').disabled = p >= 100;
}
function _volSnap(dir) {
  const p = Math.round(volSlider.value * 100);
  // Branchless "nearest multiple of 5 in the pressed direction": off-grid
  // snaps to it, on-grid moves a full step (floor(45/5)+1 = 50, etc.).
  const next = (dir > 0 ? Math.floor(p / 5) + 1 : Math.ceil(p / 5) - 1) * 5;
  volSlider.value = Math.min(100, Math.max(0, next)) / 100;
  volSlider.dispatchEvent(new Event('input'));  // reuse the full slider pipeline
}
document.getElementById('vol-down').addEventListener('click', () => _volSnap(-1));
document.getElementById('vol-up').addEventListener('click', () => _volSnap(1));
_volSyncButtons();

// ── Lock toggle ────────────────────────────────────────────────────────────

const lockToggle = document.getElementById('lock-toggle');
lockToggle.addEventListener('change', async () => {
  lockToggle.disabled = true;
  try {
    await api('POST', lockToggle.checked ? '/admin/queue/lock' : '/admin/queue/unlock');
  } catch { showToast('Lock error'); lockToggle.checked = !lockToggle.checked; } finally { lockToggle.disabled = false; }
});

function syncLock(isLocked) {
  lockToggle.checked = !!isLocked;
}

// ── Flood Control (instant toggle) ───────────────────────────────────────────
// Mirrors the lock-toggle: POSTs the new state to /admin/settings immediately
// on change (not via Save Settings), reverting the checkbox if the write fails.
// Referenced inline (no top-level const) to stay inside the static-discipline
// allowlist; the handler reads the element from the event target.
document.getElementById('flood-toggle').addEventListener('change', async (e) => {
  const el = e.currentTarget;
  el.disabled = true;
  try {
    await api('POST', '/admin/settings', { flood_control: el.checked });
    showToast(el.checked ? 'Flood Control on' : 'Flood Control off');
  } catch { showToast('Flood Control error'); el.checked = !el.checked; } finally { el.disabled = false; }
});

document.getElementById('btn-clear').addEventListener('click', async () => {
  if (!confirm('Clear the entire queue?')) return;
  try { await api('POST', '/admin/queue/clear', { confirmed: true }); }
  catch { showToast('Error clearing queue'); }
});

// ── WebSocket ──────────────────────────────────────────────────────────────

let ws;
let wsBackoff = 1000;

function connectWS() {
  if (ws) { ws.onclose = null; ws.close(); }
  ws = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/admin/ws`);
  ws.onopen = () => {
    wsBackoff = 1000;
    // Re-sync slider on (re)connect — device may have changed volume while
    // the WS was down. loadVolume's own _volUserLastSet guard protects an
    // active drag, so this is safe to call unconditionally.
    if (typeof loadVolume === 'function') loadVolume();
    // Re-hydrate the AirPlay protocol cache on every WS connect so events
    // broadcast while the socket was down don't leave the UI stale.
    // Mirrors the loadVolume() resync pattern above.
    if (typeof loadAirplayProtocols === 'function') loadAirplayProtocols();
    // Same reasoning for the queue snapshot — missed queue_changed events
    // would leave the queue list and Skip Back disabled-state stale.
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
      // station-card indicators live. Null-guard: browse mounts in an IIFE and
      // a broadcast can race first paint.
      if (browseHandle && browseHandle.applyRadioState) browseHandle.applyRadioState(msg);
      // Radio now-playing surface (plan U10): the same event drives the shared
      // playback widget (station/state/live-title/queue-paused notice + STOP).
      // Distinct name from browse's applyRadioState (FE6 — no cross-module
      // collision on the shared handle).
      playbackHandle.applyRadioNowPlaying(msg);
    }
    else if (msg.type === 'closing_time') playbackHandle.applyClosingTime(msg);
    else if (msg.type === 'output_session') {
      // Supervisor plan U4: shared lean note via the playback module, then the
      // admin-rich banner detail (device / attempts / retry / Resume) — the
      // event body is the same shape as the /admin/queue snapshot field, so
      // push and pull share one render path (the applyDevicesPayload pattern).
      playbackHandle.applyOutputSession(msg);
      renderOutputSessionBanner(msg);
    }
    else if (msg.type === 'track_skipped') playbackHandle.showSkipped(msg);
    else if (msg.type === 'queue_changed') {
      playbackHandle.applyQueue(msg.queue, msg.history);
      // Feed browse queue membership so track-row embers update live (added-to-
      // queue plan). Null-guard: browse mounts in an IIFE; a broadcast can race
      // first paint.
      if (browseHandle) browseHandle.applyQueue(msg.queue);
      setRecentPlaysData(msg.history);
      syncLock(msg.is_locked);
      _historyEmpty = !(msg.history && msg.history.length);
      _syncPrevEnabled();
    }
    else if (msg.type === 'lock_changed') syncLock(msg.is_locked);
    else if (msg.type === 'output_changed') {
      // backend_type 'error' is a playback-failure signal (Chromecast EOS
      // error / watchdog, AirPlay crash) — surface the message so a silent
      // auto-skip is visible to the host. device_name carries the text.
      if (msg.backend_type === 'error' && msg.device_name) showToast(msg.device_name);
      loadVolume();
    }
    else if (msg.type === 'volume_changed') applyVolumeFromEvent(msg.level);
    else if (msg.type === 'airplay_protocol_changed') {
      _airplayProtocols[msg.device_id] = msg.protocol;
      _syncAirPlayProtocolUI();
    }
    else if (msg.type === 'appearance_changed') {
      // Null-guard: the engine mounts in an IIFE below; a broadcast can
      // race the first paint (the switchTab guard precedent).
      if (appearanceHandle) appearanceHandle.onAppearanceChanged(msg);
    }
    else if (msg.type === 'devices_changed') {
      // Live discovery (plan U6): the watcher pushed a fresh snapshot —
      // same shape as the GET body, same render path (KTD5).
      applyDevicesPayload(msg.devices, msg.mdns_status);
    }
    else if (msg.type === 'surprise_recorded') {
      // A guest's Surprise press updated the attribution — refresh the Setup
      // "Recent suggestions" readout live (same {recent, tally} shape as the GET).
      renderSurpriseRecent(msg);
    }
  };
  ws.onclose = () => {
    wsBackoff = Math.min(wsBackoff * 2, 30000);
    setTimeout(connectWS, wsBackoff);
  };
}

connectWS();

// ── Output config ──────────────────────────────────────────────────────────

// Cross-protocol output picker. The admin discovery API returns one entry
// per physical device with a verified-protocol set attached. The picker
// renders one device row plus a "Via" dropdown that lists only protocols
// proven to work on that device. See
// docs/plans/2026-06-08-002-feat-cross-protocol-output-picker-plan.md
// for the full spec.

const PROTOCOL_LABELS = {
  direct: 'Direct (System Audio)',
  airplay: 'AirPlay',
  chromecast: 'Chromecast',
  dlna: 'DLNA',
  // Server-fed multi-room backends (2026-08-11 plan U5/U7). Whole-backend
  // selections managed via the Admin → Setup "Optional integrations" panel +
  // zoning UI (U9), not per-device Via picker options — labelled here but
  // deliberately absent from VIA_DEFAULT_ORDER.
  snapcast: 'Snapcast',
  sendspin: 'Sendspin',
};
// Default Via priority when a device has multiple verified protocols and
// no per-device preference is stored. AirPlay first reflects its lower
// control-channel latency on most modern receivers.
const VIA_DEFAULT_ORDER = ['airplay', 'chromecast', 'dlna'];
// Stuck-probe window. A verified=null entry whose checked_at is older
// than this is rendered as "Could not verify" instead of "Checking…".
// The backend's per-probe timeouts are all <= 10s; 45s leaves comfortable
// headroom for the scheduling semaphore + clock skew.
const STUCK_PROBE_S = 45;

let allDevices = [];           // Flat list of AggregatedDevice (post-U4).
let devicesByHost = {};        // {host: AggregatedDevice} for fast lookup.
let currentActive = { backend_type: 'direct', device_id: 'default', host: null, via: null };
let _devicesLoading = false;
let _initialLoadDone = false;  // false until first loadDevices() resolves.

async function loadDevices(bust = false) {
  if (_devicesLoading) return;
  _devicesLoading = true;
  let devicesResp, active;
  try {
    const url = '/admin/output/devices' + (bust ? '?bust=1' : '');
    [devicesResp, active] = await Promise.all([
      api('GET', url),
      api('GET', '/admin/output/active'),
    ]);
  } catch {
    showToast('Device scan failed — check network and server logs');
    return;
  } finally {
    _devicesLoading = false;
    _initialLoadDone = true;
  }
  currentActive = active;
  applyDevicesPayload(devicesResp && devicesResp.devices, devicesResp && devicesResp.mdns_status);
}

// Live-discovery render path (discovery plan U6): ONE function renders a
// devices payload whether it arrived via GET or via a devices_changed WS
// frame — the event body is exactly the GET response body (KTD5), so the
// picker can never drift between pull and push. currentActive is NOT in
// the event; it only changes through output_changed → loadDevices.
function applyDevicesPayload(devices, mdnsStatus) {
  allDevices = Array.isArray(devices) ? devices : [];
  devicesByHost = {};
  for (const d of allDevices) devicesByHost[d.host] = d;
  _updateBackendBanner(mdnsStatus);
  _initialLoadDone = true;
  updateDeviceSelect();
  _updateDeviceOfflineBanner();
  _syncAirPlayProtocolUI();
}

// Offline warnings (plan R3/D5 — mark, never auto-switch). Two cases share
// one banner, active-output takes precedence over picker-selection:
// the admin is told, and chooses; Jukeplox never changes outputs itself.
function _updateDeviceOfflineBanner() {
  const banner = document.getElementById('device-offline-banner');
  if (!banner) return;
  const activeDev = currentActive && currentActive.host ? devicesByHost[currentActive.host] : null;
  const sel = document.getElementById('device-select');
  const selectedDev = sel && sel.value ? devicesByHost[sel.value] : null;
  if (activeDev && activeDev.online === false) {
    banner.textContent = '⚠ Active output “' + (activeDev.name || currentActive.host) +
      '” appears offline — playback may fail until it returns or you choose another output.';
    banner.style.display = '';
  } else if (selectedDev && selectedDev.online === false) {
    banner.textContent = '⚠ “' + (selectedDev.name || selectedDev.host) +
      '” appears offline — you can still select it, but it may fail until it returns.';
    banner.style.display = '';
  } else {
    banner.style.display = 'none';
  }
}

function _updateBackendBanner(status) {
  const banner = document.getElementById('mdns-unavailable-banner');
  const msgEl = document.getElementById('mdns-banner-msg');
  if (!banner || !msgEl) return;
  if (!status || typeof status !== 'object') {
    banner.style.display = 'none';
    return;
  }
  // discovery === 'unavailable' is the root cause (no Cast/AirPlay source
  // established — neither the in-process mDNS bind nor the avahi/D-Bus
  // fallback) and has specific fixes, so it takes precedence over the
  // per-backend "scan unavailable" note.
  if (status.discovery === 'unavailable') {
    msgEl.textContent = 'Chromecast & AirPlay discovery is unavailable — Jukeplox can’t '
      + 'reach the network for mDNS. Run the container with --network host so it can '
      + 'bind mDNS (UDP 5353); if a host avahi/Bonjour already owns 5353 (e.g. TrueNAS), '
      + 'also mount /run/dbus/system_bus_socket so Jukeplox can use it. Then Retry '
      + 'Discovery. (DLNA and System Audio are unaffected.)';
    banner.style.display = '';
    return;
  }
  const failures = [];
  for (const [backend, state] of Object.entries(status)) {
    // 'direct' has no network surface; 'discovery' is handled above.
    if (state === 'unavailable' && backend !== 'direct' && backend !== 'discovery') {
      failures.push(PROTOCOL_LABELS[backend] || backend);
    }
  }
  if (failures.length === 0) {
    banner.style.display = 'none';
    return;
  }
  let label;
  if (failures.length === 1) {
    label = failures[0] + ' scan unavailable — ' + failures[0] + ' devices won’t appear.';
  } else if (failures.length === 2) {
    label = failures[0] + ' and ' + failures[1] + ' scans unavailable — those devices won’t appear.';
  } else {
    const head = failures.slice(0, -1).join(', ');
    label = head + ', and ' + failures[failures.length - 1] + ' scans unavailable — those devices won’t appear.';
  }
  msgEl.textContent = label;
  banner.style.display = '';
}

function updateDeviceSelect() {
  const sel = document.getElementById('device-select');
  sel.innerHTML = '';

  // Initial-page-load state: before the first loadDevices completes,
  // show a non-interactive placeholder so the operator never sees a
  // blank or stale-HTML picker.
  if (!_initialLoadDone) {
    const loading = document.createElement('option');
    loading.disabled = true;
    loading.textContent = 'Loading devices…';
    sel.appendChild(loading);
    _setApplyDisabled(true, 'Loading devices…');
    document.getElementById('connection-select').innerHTML = '';
    return;
  }

  // Direct ("System Audio") is always first, always selectable. If the
  // backend never sent a Direct entry (degraded payload), inject one.
  let directDev = devicesByHost['__direct__'];
  if (!directDev) {
    directDev = {
      host: '__direct__', name: 'System Audio',
      // gapless mirrors the server snapshot's Direct capability (plan U5).
      protocols: [{ backend: 'direct', device_id: 'default', verified: true, checked_at: null, gapless: 'supported' }],
    };
  }
  _appendDeviceOption(sel, directDev);

  const networkDevices = allDevices.filter(d => d.host !== '__direct__');
  for (const dev of networkDevices) _appendDeviceOption(sel, dev);

  if (networkDevices.length === 0) {
    const hint = document.createElement('option');
    hint.disabled = true;
    hint.textContent = 'No devices found — use Scan or Retry Discovery to recheck';
    sel.appendChild(hint);
  }

  // Pre-select the persisted host when known; otherwise leave the
  // dropdown on the Direct (top) entry.
  const persistedHost = currentActive && currentActive.host;
  if (persistedHost && devicesByHost[persistedHost]) {
    sel.value = persistedHost;
  } else if (currentActive && currentActive.backend_type === 'direct') {
    sel.value = '__direct__';
  }

  updateConnectionSelect();
}

function _appendDeviceOption(sel, dev) {
  const opt = document.createElement('option');
  opt.value = dev.host;
  let label = dev.host === '__direct__' ? 'System Audio (Direct)' : dev.name;
  if (currentActive && currentActive.host === dev.host) label += ' (Default)';
  // Offline marking (discovery plan KTD8): greyed + tagged, still
  // selectable — the banner carries the warning when picked.
  if (dev.online === false) {
    label += ' — offline';
    opt.style.color = '#777';
  }
  opt.textContent = label;
  sel.appendChild(opt);
}

function updateConnectionSelect() {
  const sel = document.getElementById('connection-select');
  sel.innerHTML = '';
  const host = document.getElementById('device-select').value;
  const dev = devicesByHost[host];
  if (!dev) {
    _setApplyDisabled(true, 'Select a device');
    updateConnectionHint();
    return;
  }
  const now = Date.now() / 1000;
  const entries = (dev.protocols || []).map(p => {
    const stuck = (
      p.verified === null
      && p.checked_at !== null && p.checked_at !== undefined
      && (now - p.checked_at) > STUCK_PROBE_S
    );
    return { ...p, stuck };
  });

  // Drop failed protocols entirely; keep verified + in-flight + stuck.
  const visible = entries.filter(p => p.verified !== false);
  const verifiedEntries = visible.filter(p => p.verified === true);

  if (visible.length === 0) {
    const opt = document.createElement('option');
    opt.disabled = true;
    opt.textContent = '—';
    sel.appendChild(opt);
    _setApplyDisabled(true, 'No working protocols found for this device — try Scan or pick a different device');
    updateConnectionHint();
    return;
  }

  for (const p of visible) {
    const opt = document.createElement('option');
    opt.dataset.backend = p.backend;
    opt.dataset.deviceId = p.device_id;
    if (p.verified === true) {
      opt.textContent = PROTOCOL_LABELS[p.backend] || p.backend;
    } else if (p.stuck) {
      opt.textContent = (PROTOCOL_LABELS[p.backend] || p.backend) + ' (Could not verify)';
      opt.disabled = true;
    } else {
      opt.textContent = (PROTOCOL_LABELS[p.backend] || p.backend) + ' (Checking…)';
      opt.disabled = true;
    }
    // Gapless capability chip (2026-07-11 supervisor plan U5): a minimal text
    // badge from the snapshot's per-protocol field — supported / unsupported /
    // unverified. <option> rows are text-only, so the "chip" is a label suffix
    // (same idiom as the "(Checking…)" state above); the deliberate visual
    // pass is deferred to a mockups round. Absent field (degraded payload) →
    // no suffix, exactly today's label.
    if (p.gapless) opt.textContent += ' · gapless: ' + p.gapless;
    sel.appendChild(opt);
  }

  // Pre-select: persisted Via if verified; else priority-order first
  // verified; else the first verified entry; else nothing-selectable.
  let preferred = null;
  if (currentActive && currentActive.via) {
    preferred = verifiedEntries.find(p => p.backend === currentActive.via);
  }
  if (!preferred) {
    for (const b of VIA_DEFAULT_ORDER) {
      preferred = verifiedEntries.find(p => p.backend === b);
      if (preferred) break;
    }
  }
  if (!preferred && verifiedEntries.length > 0) preferred = verifiedEntries[0];

  if (preferred) {
    for (const opt of sel.options) {
      if (opt.dataset.backend === preferred.backend) {
        opt.selected = true;
        break;
      }
    }
    _setApplyDisabled(false, '');
  } else {
    _setApplyDisabled(true, 'Waiting for protocol check to complete…');
  }

  updateConnectionHint();
}

function _setApplyDisabled(disabled, title) {
  const btn = document.getElementById('btn-apply-output');
  if (!btn) return;
  btn.disabled = !!disabled;
  if (title) btn.title = title; else btn.removeAttribute('title');
}

function updateConnectionHint() {
  const sel = document.getElementById('connection-select');
  const hintEl = document.getElementById('output-device-hint');
  if (!hintEl) return;
  const opt = sel.selectedOptions[0];
  const hint = opt && opt.dataset.hint;
  if (hint) {
    hintEl.textContent = hint;
    hintEl.style.display = '';
  } else {
    hintEl.textContent = '';
    hintEl.style.display = 'none';
  }
}

document.getElementById('device-select').addEventListener('change', updateConnectionSelect);
document.getElementById('device-select').addEventListener('change', _updateDeviceOfflineBanner);
document.getElementById('connection-select').addEventListener('change', updateConnectionHint);
document.getElementById('btn-scan-output').addEventListener('click', () => loadDevices(true));

document.getElementById('btn-retry-discovery').addEventListener('click', async () => {
  const btn = document.getElementById('btn-retry-discovery');
  btn.disabled = true;
  btn.textContent = 'Scanning…';
  await loadDevices(true);
  btn.disabled = false;
  btn.textContent = 'Retry Discovery';
});

document.getElementById('btn-dismiss-mdns-banner').addEventListener('click', () => {
  const banner = document.getElementById('mdns-unavailable-banner');
  if (banner) banner.style.display = 'none';
});

// In-flight guard for the Apply click (review fix JFR-3, the house
// _surpriseBusy pattern): a double-click raced two POSTs — two structured
// 409s meant two stacked confirm overlays (both could then Confirm).
let _applyOutputBusy = false;
document.getElementById('btn-apply-output').addEventListener('click', async () => {
  if (_applyOutputBusy) return;
  const sel = document.getElementById('connection-select');
  const opt = sel.selectedOptions[0];
  if (!opt || opt.disabled) { showToast('Select a verified protocol'); return; }
  _applyOutputBusy = true;
  const backend_type = opt.dataset.backend;
  const device_id = opt.dataset.deviceId;
  const host = document.getElementById('device-select').value;
  const body = { backend_type, device_id };
  if (host && host !== '__direct__') body.host = host;
  // Success bookkeeping, shared by the direct path and the confirmed
  // resend inside the stranded dialog (plexplayer plan U6).
  const applySwitched = () => {
    currentActive = { backend_type, device_id, host: body.host || null, via: backend_type };
    updateDeviceSelect();
    _syncAirPlayProtocolUI();
  };
  try {
    await api('POST', '/admin/output/active', body);
    applySwitched();
    showToast('Output updated');
  } catch (err) {
    const d = err && err.detail;
    if (d && typeof d === 'object' && d.reason === 'output_switch_confirm') {
      // Plexplayer plan U6 two-phase switch: the server warned that queued
      // tracks would be stranded — hand off to the confirm dialog, which
      // resends the same body with confirmed:true. No state changed yet.
      showOutputSwitchConfirm(d.stranded_count, body, applySwitched);
    } else if (err && err.status === 409) {
      // A plain-string HTTP 409 signals a stale-verdict failure on Apply
      // — re-fetch with a bust to refresh the cache and re-probe. (The
      // structured switch-confirm 409 branched above; any other dict
      // detail falls back to the generic copy.)
      showToast(((typeof d === 'string' && d) || 'Protocol check is stale') + ' — rechecking');
      loadDevices(true);
    } else {
      showToast('Failed to update output');
    }
  } finally {
    _applyOutputBusy = false;
  }
});

// ── Stranded-tracks switch confirm dialog (2026-08-04-002 plexplayer U6) ───
// Admin-only chrome (never the shared module): shown ONLY on the structured
// 409 from POST /admin/output/active. A native confirm() can't express the
// in-flight loading state the two-phase flow needs, so this is a minimal
// overlay dialog. Blocking-overlay lesson honored: the dialog's own
// Confirm/Cancel sit inside the card ON TOP of the backdrop (never covered
// by it), the state is purely client-side (a refresh recovers), and it is
// deliberately NOT dismissable mid-flight — the confirmed POST must resolve
// or fail before the dialog can go away, so the admin always sees the
// outcome toast (actual removed count, or failure with the queue untouched).
function showOutputSwitchConfirm(strandedCount, requestBody, onSwitched) {
  // Singleton overlay (review fix JFR-3): any earlier confirm dialog is
  // superseded — the LAST intent wins, so two dialogs can never stack and
  // both be confirmed.
  document.querySelectorAll('.switch-confirm-overlay')
    .forEach((el) => el.remove());
  const n = strandedCount || 0;
  const overlay = document.createElement('div');
  overlay.className = 'switch-confirm-overlay';
  overlay.innerHTML = `
    <div class="switch-confirm-card">
      <p>${n} queued track${n === 1 ? '' : 's'} can't play on this device and will be removed.</p>
      <div class="switch-confirm-actions">
        <button type="button" class="btn btn-sm" data-act="cancel">Cancel</button>
        <button type="button" class="btn btn-primary btn-sm" data-act="confirm">Confirm</button>
      </div>
    </div>`;
  const confirmBtn = overlay.querySelector('[data-act="confirm"]');
  const cancelBtn = overlay.querySelector('[data-act="cancel"]');
  let inFlight = false;
  // Cancel / backdrop click → plain dismiss, nothing sent (AE2: reject is a
  // no-op — queue and output untouched). Both refuse mid-flight.
  cancelBtn.addEventListener('click', () => { if (!inFlight) overlay.remove(); });
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay && !inFlight) overlay.remove();
  });
  confirmBtn.addEventListener('click', async () => {
    if (inFlight) return;
    inFlight = true;
    confirmBtn.disabled = true;
    cancelBtn.disabled = true;
    confirmBtn.textContent = 'Switching…';
    try {
      const res = await api('POST', '/admin/output/active',
        Object.assign({}, requestBody, { confirmed: true }));
      overlay.remove();
      onSwitched();
      // Server recomputed the stranded set at confirm time — toast the
      // ACTUAL removed count, never the warned one (race-safe).
      const removed = (res && res.removed_count) || 0;
      showToast(`Removed ${removed} unplayable track${removed === 1 ? '' : 's'}`);
    } catch {
      inFlight = false;
      confirmBtn.disabled = false;
      cancelBtn.disabled = false;
      confirmBtn.textContent = 'Confirm';
      showToast('Could not switch output — queue was not changed');
    }
  });
  document.body.appendChild(overlay);
}

// ── AirPlay protocol indicator + No-audio + Re-test ───────────────────────
// Cache of per-device cliap2-vs-cliraop verdicts. Hydrated on page load
// from /admin/output/airplay-protocols; updated reactively via the
// `airplay_protocol_changed` WebSocket event when the discovery probe,
// the No-audio button, or the Re-test button persists a new verdict.

let _airplayProtocols = {};
const btnNoAudio = document.getElementById('btn-no-audio');
const btnRetestAp2 = document.getElementById('btn-retest-ap2');
const protocolLabel = document.getElementById('protocol-label');

async function loadAirplayProtocols() {
  // Merge into the existing object rather than replacing it. The WS
  // handler writes point-updates via _airplayProtocols[device_id] = ...
  // during the fetch await. A wholesale assignment would silently drop
  // those WS-delivered writes — Object.assign keeps both sources coherent.
  try {
    const fresh = await api('GET', '/admin/output/airplay-protocols');
    Object.assign(_airplayProtocols, fresh);
  } catch { /* leave existing cache in place on fetch failure */ }
  _syncAirPlayProtocolUI();
}

function _syncAirPlayProtocolUI() {
  const isAirPlay = currentActive.backend_type === 'airplay';
  const deviceId = currentActive.device_id;
  const protocol = isAirPlay ? _airplayProtocols[deviceId] : undefined;

  // Protocol label: visible during AirPlay sessions when we know the verdict.
  if (isAirPlay && protocol) {
    protocolLabel.textContent = protocol === 'ap2' ? 'AirPlay 2' : 'AirPlay 1';
    protocolLabel.style.display = '';
  } else {
    protocolLabel.style.display = 'none';
  }

  // No-audio button: visible during any active AirPlay session. Hidden for
  // other backends because the silent-fail recovery is AirPlay-specific.
  btnNoAudio.style.display = isAirPlay ? '' : 'none';

  // Re-test button: visible when the selected device is the active AirPlay
  // device AND it has a cached `ap1` verdict. Re-test only makes sense for
  // devices we previously marked AP1-only — a firmware update could have
  // fixed cliap2 support since the probe last ran.
  btnRetestAp2.style.display = (isAirPlay && protocol === 'ap1') ? '' : 'none';
}

btnNoAudio.addEventListener('click', async () => {
  btnNoAudio.disabled = true;
  try {
    await api('POST', '/admin/playback/no-audio');
    showToast('Falling back to AirPlay 1');
  } catch { showToast('No-audio fallback failed'); }
  finally { btnNoAudio.disabled = false; }
});

btnRetestAp2.addEventListener('click', async () => {
  const deviceId = currentActive.device_id;
  if (!deviceId) return;
  btnRetestAp2.disabled = true;
  const originalText = btnRetestAp2.textContent;
  btnRetestAp2.textContent = 'Probing…';
  try {
    const result = await api('POST', `/admin/output/devices/${encodeURIComponent(deviceId)}/retest-ap2`);
    _airplayProtocols[deviceId] = result.protocol;
    _syncAirPlayProtocolUI();
    showToast(result.protocol === 'ap2' ? 'AP2 now working — switched back' : 'Still AP1-only after re-test');
  } catch { showToast('Re-test failed'); }
  finally {
    // Only restore button state if the active device hasn't changed during
    // the probe. If a concurrent WS event or device switch hid the button,
    // _syncAirPlayProtocolUI() already owns the visible state and our
    // restore would overwrite that with stale text/enabled values.
    if (currentActive.device_id === deviceId) {
      btnRetestAp2.disabled = false;
      btnRetestAp2.textContent = originalText;
    }
  }
});

// Initial hydration.
loadAirplayProtocols();

// ── Chrome helpers (used by library list, etc.) ───────────────────────────
// Per KTD9 of the unification plan: esc, artImg, formatDuration stay
// duplicated on both per-page files because they're page-chrome with no
// shared-module consumer. The shared browse module uses private _esc,
// _artImg, _formatDuration internally to avoid name conflicts.

function esc(s) { return (s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function artImg(thumb, cls) {
  if (!thumb) return `<div class="${cls}" style="display:flex;align-items:center;justify-content:center;font-size:1.2rem">🎷</div>`;
  return `<img class="${cls}" src="/api/art?path=${encodeURIComponent(thumb)}" alt="" loading="lazy">`;
}

function formatDuration(ms) {
  const s = Math.floor(ms / 1000);
  return `${Math.floor(s/60)}:${String(s%60).padStart(2,'0')}`;
}

// ── Browse / search / queue-append: mount shared module ───────────────────
// All browse/search/track-row/source-picker/album-tracks/artist-albums/
// queue-append rendering lives in static/browse/index.js. See
// tests/test_static_discipline.py for the authoritative allowlist.
//
// Glow-up U5: the appearance engine (shared.js) owns the /api/appearance
// fetch and applies the resolved rail mode via the browse handle — the
// old per-page /api/rail-mode pre-mount fetch is gone. Overrides are
// per-device: an admin restyling their own session never touches the
// Setup defaults (those save through /admin/settings as before).

let browseHandle;
let appearanceHandle;
(() => {
  browseHandle = mountBrowser('#browse-container', {
    authMode: 'admin',
    isLocked: () => false,  // admin bypasses the guest lock by design
    toast: showToast,
  });
  appearanceHandle = mountAppearance({ getHandle: () => browseHandle, getPlaybackHandle: () => playbackHandle });
})();

// ── Tabs (browse mode switching) ───────────────────────────────────────────
// Adopts guest's .tab / .view / .view.active convention per U3 of the
// unification plan (replaces admin's prior .a-mode-tab + #admin-pane-*
// inline-display toggling). Scoped to #browse — the page-level Jukebox/
// Setup tabs use the separate .ptab class.

document.querySelectorAll('#browse .tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('#browse .tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('#browse .view').forEach(v => v.classList.remove('active'));
    tab.classList.add('active');
    const view = document.getElementById(tab.dataset.view);
    if (view) view.classList.add('active');
    // U2 of plan 003: browseHandle may be undefined for the first ~10-50ms
    // while the pre-mount rail-mode fetch resolves. Race-safe early return —
    // the user simply has to retap; in practice the fetch finishes faster
    // than any user reaction time.
    if (browseHandle) browseHandle.activateView(tab.dataset.view);
  });
});

// ── Sources (multi-source connect / remove / rescan / priority, plan U14) ────

const sourcesList = document.getElementById('sources-list');
const jfConnectError = document.getElementById('jf-connect-error');
const SOURCE_TYPE_LABELS = { plex: 'Plex', jellyfin: 'Jellyfin', subsonic: 'Subsonic', emby: 'Emby', local: 'Local' };
let _currentSources = [];
let _currentLibs = [];               // last /admin/plex/libraries payload (grouped by source)
const _openDrills = new Set();       // source_ids whose "Edit libraries…" panel is expanded
const _togglingSources = new Set();  // source_ids with an in-flight switch POST (no double-toggle)
let _loadSourcesGen = 0;             // monotonic guard: a slower loadSources() must not overwrite a fresher render

// One source-grouped list: each source is a row carrying a whole-source on/off
// switch, priority arrows, (non-Plex) Remove, and an "Edit libraries…" drill-in
// that reveals nested per-library checkboxes (rendered for every source with
// libraries — single-library sources included, fresh-install audit F1).
// Source-off greys AND disables those checkboxes (source-off wins).
// Grouping replaces the old "(Type — owner)" disambiguation tags (Libraries-panel).
function renderSourcesList(sources, libs) {
  _currentSources = sources.slice();
  if (libs !== undefined) _currentLibs = libs;
  if (!sources.length) {
    sourcesList.innerHTML =
      '<p style="color:#555;font-size:.85rem">No sources connected — use the buttons below to add one.</p>';
    syncSurpriseSourceNote();
    return;
  }

  // Group libraries under their owning source (explicit source_id, else key prefix).
  const bySource = {};
  (_currentLibs || []).forEach(l => {
    const key = String(l.key || '');
    const sid = (l.source_id !== undefined && l.source_id !== null && l.source_id !== '')
      ? l.source_id : (key.includes(':') ? key.split(':', 1)[0] : '');
    (bySource[sid] = bySource[sid] || []).push(l);
  });

  // Inner helpers kept local so the per-page top-level surface (discipline
  // allowlist) stays unchanged.
  const cssSafe = (s) => 'x' + String(s).replace(/[^a-zA-Z0-9_-]/g, '_');
  const arrowBtn = (dir, sid) => {
    const b = document.createElement('button');
    b.className = 'icbtn';
    b.textContent = dir === 'up' ? '▲' : '▼';
    b.title = dir === 'up' ? 'Higher priority' : 'Lower priority';
    b.addEventListener('click', () => moveSourcePriority(sid, dir));
    return b;
  };
  const showRowError = (block, msg) => {
    let e = block.querySelector('.src-err');
    if (!e) { e = document.createElement('div'); e.className = 'src-err'; block.appendChild(e); }
    e.textContent = msg;
  };
  const clearRowError = (block) => { const e = block.querySelector('.src-err'); if (e) e.remove(); };

  sourcesList.innerHTML = '';
  sources.forEach((src, i) => {
    const sid = src.source_id;
    const srcLibs = bySource[sid] || [];
    let on = src.enabled !== false;

    const block = document.createElement('div');
    block.className = 'src-block';
    const row = document.createElement('div');
    row.className = 'src-row';

    const nameWrap = document.createElement('span');
    nameWrap.className = 'src-name';
    nameWrap.innerHTML =
      `<span class="type-tag">${esc(SOURCE_TYPE_LABELS[src.type] || src.type || '')}</span>${esc(src.name)}`;

    const meta = document.createElement('span');
    meta.className = 'src-meta';
    const setMeta = () => {
      // State-aware for every source size: the old single-library "1 library" label
      // hid a disabled library behind healthy-looking text (fresh-install audit F1).
      const n = srcLibs.filter(l => l.enabled).length;
      meta.textContent = !srcLibs.length ? '' : `${n} of ${srcLibs.length} on`;
    };

    // Nested per-library checkboxes (source-off greys + disables them).
    const children = document.createElement('div');
    children.className = 'lib-children';
    children.id = 'libs-' + cssSafe(sid);
    children.hidden = !_openDrills.has(sid);
    srcLibs.forEach(lib => {
      const label = document.createElement('label');
      label.className = 'lib-child' + (on ? '' : ' greyed');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = !!lib.enabled;
      cb.disabled = !on;
      cb.addEventListener('change', async () => {
        // Don't race the whole-source switch: if it's off or mid-flight, this
        // per-library write has no catalog effect (source-off wins) — revert and bail.
        if (!on || _togglingSources.has(sid)) { cb.checked = !cb.checked; return; }
        const path = `/admin/plex/libraries/${encodeURIComponent(lib.key)}/`
          + (cb.checked ? 'enable' : 'disable');
        try { await api('POST', path); lib.enabled = cb.checked; setMeta(); }
        catch { showToast('Failed to update library'); cb.checked = !cb.checked; }
      });
      label.appendChild(cb);
      label.appendChild(document.createTextNode(' ' + (lib.title || '')));
      children.appendChild(label);
    });

    // "Edit libraries…" drill-in — for every source with libraries. Gating this on
    // more-than-one library left a single-library Plex source's checkbox unreachable
    // (no expander, nothing seeded on connect → permanently empty catalog on fresh
    // installs; fresh-install audit F1, 2026-08-06).
    if (srcLibs.length > 0) {
      const drill = document.createElement('button');
      drill.type = 'button';
      drill.className = 'drill';
      drill.setAttribute('aria-controls', children.id);
      const syncDrill = () => {
        const open = _openDrills.has(sid);
        drill.textContent = open ? 'Hide libraries' : 'Edit libraries…';
        drill.setAttribute('aria-expanded', open ? 'true' : 'false');
        children.hidden = !open;
      };
      drill.addEventListener('click', () => {
        if (_openDrills.has(sid)) _openDrills.delete(sid); else _openDrills.add(sid);
        syncDrill();
      });
      syncDrill();
      nameWrap.appendChild(document.createTextNode(' '));
      nameWrap.appendChild(drill);
    }
    nameWrap.appendChild(meta);
    setMeta();

    const controls = document.createElement('span');
    controls.className = 'src-controls';
    if (i > 0) controls.appendChild(arrowBtn('up', sid));
    if (i < sources.length - 1) controls.appendChild(arrowBtn('down', sid));
    // Plex disconnect stays in the Plex flow; only non-Plex sources are removable here.
    if (src.type !== 'plex') {
      const rm = document.createElement('button');
      rm.className = 'icbtn rm';
      rm.textContent = 'Remove';
      rm.addEventListener('click', () => removeSource(src.type, sid));
      controls.appendChild(rm);
    }

    // Whole-source on/off switch.
    const sw = document.createElement('label');
    sw.className = 'switch';
    const swInput = document.createElement('input');
    swInput.type = 'checkbox';
    swInput.checked = on;
    swInput.setAttribute('aria-label', 'Enable ' + (src.name || 'source'));
    const slider = document.createElement('span');
    slider.className = 'slider';
    sw.appendChild(swInput);
    sw.appendChild(slider);
    const applyGrey = () => {
      children.querySelectorAll('.lib-child').forEach(lc => {
        lc.classList.toggle('greyed', !on);
        const cb = lc.querySelector('input'); if (cb) cb.disabled = !on;
      });
    };
    swInput.addEventListener('change', async () => {
      if (_togglingSources.has(sid)) { swInput.checked = !swInput.checked; return; }  // no double-toggle
      const want = swInput.checked;
      _togglingSources.add(sid);
      swInput.disabled = true;
      try {
        await api('POST', `/admin/sources/${encodeURIComponent(sid)}/` + (want ? 'enable' : 'disable'));
        on = want; src.enabled = want; applyGrey(); clearRowError(block);
      } catch {
        swInput.checked = !want;   // revert
        showRowError(block, `Could not ${want ? 'enable' : 'disable'} ${src.name} — try again.`);
      } finally {
        _togglingSources.delete(sid); swInput.disabled = false;
      }
    });

    row.appendChild(nameWrap);
    row.appendChild(controls);
    row.appendChild(sw);
    block.appendChild(row);
    if (srcLibs.length > 0) block.appendChild(children);
    sourcesList.appendChild(block);
  });
  syncSurpriseSourceNote();
}

// U13 capability-degradation note: when a non-Plex source is connected, Surprise
// Me falls back to whole-library random (the Plex similarity options don't apply
// to a mixed/Jellyfin/local library). Surface that in the Surprise Me settings.
function syncSurpriseSourceNote() {
  const note = document.getElementById('surprise-source-note');
  if (!note) return;
  const mixed = _currentSources.some(s => s.type && s.type !== 'plex');
  note.style.display = mixed ? '' : 'none';
}

async function loadSources() {
  const gen = ++_loadSourcesGen;   // stale-overwrite guard (5 callers x 3 serial fetches)
  try {
    const data = await api('GET', '/admin/sources');
    let sources = data.sources || [];
    // Render in saved-priority order (highest first); unknown ids fall after.
    try {
      const pr = await api('GET', '/admin/sources/priority');
      const order = pr.order || [];
      if (order.length) {
        sources = sources.slice().sort((a, b) => {
          const ia = order.indexOf(a.source_id), ib = order.indexOf(b.source_id);
          return (ia === -1 ? 1e9 : ia) - (ib === -1 ? 1e9 : ib);
        });
      }
    } catch {}
    let libs = [];
    try { libs = await api('GET', '/admin/plex/libraries'); } catch {}
    if (gen !== _loadSourcesGen) return;   // a newer loadSources() started — drop this stale result
    renderSourcesList(sources, libs);
  } catch {
    sourcesList.innerHTML = '<p style="color:#f87171;font-size:.85rem">Could not load sources.</p>';
  }
  // Scan-status badge + browse-index freshness line. Both are driven by
  // renderSourceScanStatus (which self-polls while a scan/index build is in
  // flight), so an in-progress scan on load animates to its final state without
  // a page reload.
  renderSourceScanStatus();
}

// U15 admin scan-status badge: surfaces the catalog scan state under the Sources
// list — "Scanning…" while a crawl runs, and a distinct "no music found" when a
// finished scan returned nothing (vs the zero-source "No sources connected"
// which the sources list itself shows). Hidden when the library is populated.
// Live-poll timer id for the scan-status / index readouts (admin-only Setup
// chrome). Set while a scan or index build is in flight, cleared when idle.
let _jpScanPoll = null;

async function renderSourceScanStatus() {
  const el = document.getElementById('sources-scan-status');
  if (!el) return;
  // Supersede any pending live-poll tick — this call refreshes the readouts now.
  if (_jpScanPoll) { clearTimeout(_jpScanPoll); _jpScanPoll = null; }
  let s;
  try { s = await api('GET', '/admin/scan-status'); } catch { el.style.display = 'none'; return; }
  let msg = '';
  if (s.scanning) {
    // Precedence: a refresh in flight beats a standing failure — the running
    // retry is the more useful signal; the failure re-shows if it fails again.
    msg = 'Scanning sources… the library will populate as it runs.';
  } else if (s.refresh_failed) {
    msg = 'The last library refresh failed — check the server logs, then Rescan.';
  } else if (s.grouping_degraded) {
    // Lower severity than refresh_failed on purpose (#61): the index is fine,
    // the album track counts behind same-title grouping are not. Saying "refresh
    // failed" here would send an admin hunting for missing music that is present.
    const libs = s.grouping_degraded_libraries || [];
    msg = 'Album track counts are unavailable for ' + (libs.length ? libs.join(', ') : 'a library')
        + ' — albums held on more than one source may appear twice until the next'
        + ' successful scan. The library itself is complete.';
  } else if (s.sources > 0 && s.scanned && s.empty) {
    msg = 'Scan complete, but no music was found — the connected sources returned nothing.';
  }
  el.textContent = msg;
  el.style.display = msg ? '' : 'none';
  // Browse-index freshness line ("Indexing…" → "Indexed <time>"). 503s on a
  // non-Plex install, which is fine — the line just stays blank. `building`
  // catches the NATIVE browse-index rebuild that scan-status.scanning (catalog
  // only) doesn't, so the live poll below covers both install types.
  let building = false;
  try {
    const idx = await api('GET', '/admin/plex/index-status');
    const iel = document.getElementById('index-status');
    if (iel) iel.textContent = idx.building ? 'Indexing…'
      : (idx.computed_at ? 'Indexed ' + new Date(idx.computed_at).toLocaleString() : 'Not yet indexed');
    building = !!idx.building;
  } catch {}
  // Live update: while a scan or index build is in flight, re-check shortly so
  // the readouts settle to their final state on a live page (no reload). Stops
  // polling once idle — never a perpetual timer.
  if (s.scanning || building) {
    _jpScanPoll = setTimeout(renderSourceScanStatus, 2500);
  }
}

async function connectJellyfin() {
  const btn = document.getElementById('btn-connect-jellyfin');
  const url = document.getElementById('jf-url').value.trim();
  const user = document.getElementById('jf-user').value.trim();
  const pass = document.getElementById('jf-pass').value;
  const name = document.getElementById('jf-name').value.trim();
  jfConnectError.style.display = 'none';
  if (!url || !user) {
    jfConnectError.textContent = 'Server URL and username are required.';
    jfConnectError.style.display = '';
    return;
  }
  btn.disabled = true; btn.textContent = 'Connecting…';
  try {
    const resp = await fetch('/admin/sources/jellyfin', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ server_url: url, username: user, password: pass, name }),
    });
    if (!resp.ok) {
      let cat = 'unreachable', msg = '';
      try { const e = await resp.json(); if (e.detail) { cat = e.detail.category || cat; msg = e.detail.message || ''; } } catch {}
      jfConnectError.textContent = cat === 'auth_rejected'
        ? 'Jellyfin rejected the username/password.'
        : (cat === 'unreachable' ? 'Could not reach the Jellyfin server. Check the URL.'
                                 : (msg || 'Could not connect.'));
      jfConnectError.style.display = '';
      return;
    }
    ['jf-url', 'jf-user', 'jf-pass', 'jf-name'].forEach(id => { document.getElementById(id).value = ''; });
    document.getElementById('jf-connect-form').style.display = 'none';
    showToast('Jellyfin connected — scanning…');
    loadSources();
  } catch {
    jfConnectError.textContent = 'Could not reach the Jellyfin server. Check the URL.';
    jfConnectError.style.display = '';
  } finally { btn.disabled = false; btn.textContent = 'Connect'; }
}

async function connectLocal() {
  // Mirrors connectJellyfin: a directory path (read-only, no credential). The
  // error element is looked up inline (no module-level const) to keep the
  // per-page top-level surface unchanged for the discipline allowlist.
  const btn = document.getElementById('btn-connect-local');
  const err = document.getElementById('local-connect-error');
  const dir = document.getElementById('local-dir').value.trim();
  const name = document.getElementById('local-name').value.trim();
  err.style.display = 'none';
  if (!dir) {
    err.textContent = 'A folder path is required.';
    err.style.display = '';
    return;
  }
  btn.disabled = true; btn.textContent = 'Connecting…';
  try {
    const resp = await fetch('/admin/sources/local', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ root_dir: dir, name }),
    });
    if (!resp.ok) {
      let msg = 'Could not connect to that folder.';
      try { const e = await resp.json(); if (e.detail && e.detail.message) msg = e.detail.message; } catch {}
      err.textContent = msg;
      err.style.display = '';
      return;
    }
    ['local-dir', 'local-name'].forEach(id => { document.getElementById(id).value = ''; });
    document.getElementById('local-connect-form').style.display = 'none';
    showToast('Local folder connected — scanning…');
    loadSources();
  } catch {
    err.textContent = 'Could not connect to that folder.';
    err.style.display = '';
  } finally { btn.disabled = false; btn.textContent = 'Connect'; }
}

async function connectSubsonic() {
  // ONE credential field auto-routed by the server's capability (2026-08-11-003):
  // an API key where the server advertises apiKeyAuthentication, else the account
  // password (token+salt). Error + note elements are looked up inline (no
  // module-level const) to keep the per-page top-level surface minimal for the
  // discipline allowlist.
  const btn = document.getElementById('btn-connect-subsonic');
  const err = document.getElementById('subsonic-connect-error');
  const note = document.getElementById('subsonic-connect-note');
  const url = document.getElementById('subsonic-url').value.trim();
  const key = document.getElementById('subsonic-key').value;
  const user = document.getElementById('subsonic-user').value.trim();
  const name = document.getElementById('subsonic-name').value.trim();
  err.style.display = 'none';
  note.style.display = 'none';
  if (!url || !key || !user) {
    err.textContent = 'Server URL, API key or password, and username are required.';
    err.style.display = '';
    return;
  }
  btn.disabled = true; btn.textContent = 'Connecting…';
  try {
    const resp = await fetch('/admin/sources/subsonic', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ server_url: url, secret: key, username: user, name }),
    });
    if (!resp.ok) {
      let cat = 'unreachable', msg = '';
      try { const e = await resp.json(); if (e.detail) { cat = e.detail.category || cat; msg = e.detail.message || ''; } } catch {}
      err.textContent =
        cat === 'auth_rejected' ? (msg || 'The server rejected the API key or password.')
        : cat === 'unreachable' ? 'Could not reach the Subsonic server. Check the URL.'
        : cat === 'duplicate' ? 'A source at this URL is already configured.'
        : cat === 'blocked_private' ? (msg || 'That address is blocked. Set ALLOW_PRIVATE_SOURCES to connect a private-range server.')
        : (msg || 'Could not connect.');
      err.style.display = '';
      return;
    }
    let base = null, warning = null, authMode = null;
    try { const ok = await resp.json(); base = ok.resolved_stream_base; warning = ok.warning; authMode = ok.auth_mode; } catch {}
    ['subsonic-url', 'subsonic-key', 'subsonic-user', 'subsonic-name'].forEach(id => { document.getElementById(id).value = ''; });
    document.getElementById('subsonic-connect-form').style.display = 'none';
    // The zero-music-libraries case saves the source (200) and returns a warning
    // string — surface it on success (there is no no_music_libraries error).
    if (warning) { err.textContent = warning; err.style.display = ''; }
    // Surface the resolved proxy base (U5/U7): a wrong LAN-IP auto-detection is
    // visible before a silent no-audio cast. Null base => detection failed, so
    // show the actionable STREAM_BASE_URL guidance instead of a base.
    note.textContent = base
      ? 'Streaming via ' + base + ' — set STREAM_BASE_URL to override if a Cast/DLNA device can\'t reach it.'
      : 'Could not auto-detect a device-reachable streaming address — set STREAM_BASE_URL to this server\'s LAN address or Cast/DLNA playback may be silent.';
    note.style.display = '';
    // Tell the admin which auth was negotiated (apiKey vs token+salt password).
    const via = authMode === 'token' ? ' via username/password'
      : authMode === 'apikey' ? ' via API key' : '';
    showToast('Subsonic connected' + via + ' — scanning…');
    loadSources();
  } catch {
    err.textContent = 'Could not reach the Subsonic server. Check the URL.';
    err.style.display = '';
  } finally { btn.disabled = false; btn.textContent = 'Connect'; }
}

async function connectEmby() {
  // Mirrors connectJellyfin: sign-in exchanges username/password for a token
  // (R5 — the password is discarded server-side). Header-auth source, so no
  // resolved-base surface is needed (that is Subsonic-only, per U5).
  const btn = document.getElementById('btn-connect-emby');
  const err = document.getElementById('emby-connect-error');
  const url = document.getElementById('emby-url').value.trim();
  const user = document.getElementById('emby-user').value.trim();
  const pass = document.getElementById('emby-pass').value;
  const name = document.getElementById('emby-name').value.trim();
  err.style.display = 'none';
  if (!url || !user) {
    err.textContent = 'Server URL and username are required.';
    err.style.display = '';
    return;
  }
  btn.disabled = true; btn.textContent = 'Connecting…';
  try {
    const resp = await fetch('/admin/sources/emby', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ server_url: url, username: user, password: pass, name }),
    });
    if (!resp.ok) {
      let cat = 'unreachable', msg = '';
      try { const e = await resp.json(); if (e.detail) { cat = e.detail.category || cat; msg = e.detail.message || ''; } } catch {}
      err.textContent =
        cat === 'auth_rejected' ? 'Emby rejected the username/password.'
        : cat === 'unreachable' ? 'Could not reach the Emby server. Check the URL.'
        : cat === 'duplicate' ? 'A source at this URL is already configured.'
        : cat === 'blocked_private' ? (msg || 'That address is blocked. Set ALLOW_PRIVATE_SOURCES to connect a private-range server.')
        : (msg || 'Could not connect.');
      err.style.display = '';
      return;
    }
    let warning = null;
    try { const ok = await resp.json(); warning = ok.warning; } catch {}
    ['emby-url', 'emby-user', 'emby-pass', 'emby-name'].forEach(id => { document.getElementById(id).value = ''; });
    document.getElementById('emby-connect-form').style.display = 'none';
    // The zero-music-libraries case saves the source (200) and returns a warning
    // string — surface it on success (there is no no_music_libraries error).
    if (warning) { err.textContent = warning; err.style.display = ''; }
    showToast('Emby connected — scanning…');
    loadSources();
  } catch {
    err.textContent = 'Could not reach the Emby server. Check the URL.';
    err.style.display = '';
  } finally { btn.disabled = false; btn.textContent = 'Connect'; }
}

async function removeSource(type, sourceId) {
  if (!confirm('Remove this source? Its tracks leave the library.')) return;
  try {
    await api('DELETE', `/admin/sources/${type}/${encodeURIComponent(sourceId)}`);
    showToast('Source removed');
    loadSources();
  } catch { showToast('Failed to remove source'); }
}

async function rescanSources() {
  const btn = document.getElementById('btn-rescan-sources');
  if (btn) { btn.disabled = true; btn.textContent = 'Rescanning…'; }
  try {
    await api('POST', '/admin/sources/rescan'); showToast('Rescanning all sources…');
    renderSourceScanStatus();  // reflect "Scanning…" now + start the live poll
  }
  catch { showToast('Rescan failed'); }
  finally { if (btn) { btn.disabled = false; btn.textContent = 'Rescan Sources'; } }
}

function moveSourcePriority(sourceId, dir) {
  const byId = {};
  _currentSources.forEach(s => { byId[s.source_id] = s; });
  const ids = _currentSources.map(s => s.source_id);
  const i = ids.indexOf(sourceId);
  if (i === -1) return;
  const j = dir === 'up' ? i - 1 : i + 1;
  if (j < 0 || j >= ids.length) return;
  ids.splice(j, 0, ids.splice(i, 1)[0]);
  if (_togglingSources.size) {
    // A source switch is mid-flight; an optimistic re-render would rebuild that row
    // from a stale enabled value. Persist priority and reload once it settles.
    api('POST', '/admin/sources/priority', { order: ids })
      .then(() => loadSources())
      .catch(() => { showToast('Failed to save priority'); loadSources(); });
    return;
  }
  renderSourcesList(ids.map(id => byId[id]), _currentLibs);  // optimistic reorder
  api('POST', '/admin/sources/priority', { order: ids })
    .then(() => showToast('Priority updated'))
    .catch(() => { showToast('Failed to save priority'); loadSources(); });
}

// "Connect Jellyfin" reveals the inline form (mirrors the Plex connect button);
// Cancel hides it. Submit ("Connect") + Rescan wire to the module functions.
document.getElementById('btn-connect-jellyfin-toggle').addEventListener('click', () => {
  const form = document.getElementById('jf-connect-form');
  const open = form.style.display !== 'none';
  form.style.display = open ? 'none' : '';
  jfConnectError.style.display = 'none';
  if (!open) document.getElementById('jf-url').focus();
});
document.getElementById('btn-cancel-jellyfin').addEventListener('click', () => {
  document.getElementById('jf-connect-form').style.display = 'none';
  jfConnectError.style.display = 'none';
});
document.getElementById('btn-connect-jellyfin').addEventListener('click', connectJellyfin);
document.getElementById('btn-rescan-sources').addEventListener('click', rescanSources);

// "Connect Local Folder" reveals its inline form; Cancel hides it; Connect submits.
document.getElementById('btn-connect-local-toggle').addEventListener('click', () => {
  const form = document.getElementById('local-connect-form');
  const open = form.style.display !== 'none';
  form.style.display = open ? 'none' : '';
  document.getElementById('local-connect-error').style.display = 'none';
  if (!open) document.getElementById('local-dir').focus();
});
document.getElementById('btn-cancel-local').addEventListener('click', () => {
  document.getElementById('local-connect-form').style.display = 'none';
  document.getElementById('local-connect-error').style.display = 'none';
});
document.getElementById('btn-connect-local').addEventListener('click', connectLocal);

// "Connect Subsonic" reveals its inline form; Cancel hides it (clearing the
// error + resolved-base note); Connect submits. Inline expression-statement
// wiring keeps the per-page top-level surface minimal (U7).
document.getElementById('btn-connect-subsonic-toggle').addEventListener('click', () => {
  const form = document.getElementById('subsonic-connect-form');
  const open = form.style.display !== 'none';
  form.style.display = open ? 'none' : '';
  document.getElementById('subsonic-connect-error').style.display = 'none';
  document.getElementById('subsonic-connect-note').style.display = 'none';
  if (!open) document.getElementById('subsonic-url').focus();
});
document.getElementById('btn-cancel-subsonic').addEventListener('click', () => {
  document.getElementById('subsonic-connect-form').style.display = 'none';
  document.getElementById('subsonic-connect-error').style.display = 'none';
  document.getElementById('subsonic-connect-note').style.display = 'none';
});
document.getElementById('btn-connect-subsonic').addEventListener('click', connectSubsonic);

// "Connect Emby" reveals its inline form; Cancel hides it; Connect submits.
document.getElementById('btn-connect-emby-toggle').addEventListener('click', () => {
  const form = document.getElementById('emby-connect-form');
  const open = form.style.display !== 'none';
  form.style.display = open ? 'none' : '';
  document.getElementById('emby-connect-error').style.display = 'none';
  if (!open) document.getElementById('emby-url').focus();
});
document.getElementById('btn-cancel-emby').addEventListener('click', () => {
  document.getElementById('emby-connect-form').style.display = 'none';
  document.getElementById('emby-connect-error').style.display = 'none';
});
document.getElementById('btn-connect-emby').addEventListener('click', connectEmby);

// ── Libraries ──────────────────────────────────────────────────────────────

// The flat "Plex libraries" checkbox list and its "(Type — owner)" disambiguation
// tags are gone — libraries now render nested under their source (renderSourcesList
// above), so the grouping carries source identity for free (Libraries-panel).

// Compat alias: the unified loader fetches sources + libraries and renders the one
// source-grouped list. Existing call sites (init) keep working.
async function loadLibraries() { return loadSources(); }

// ── Plex connect ───────────────────────────────────────────────────────────

const btnConnectPlex = document.getElementById('btn-connect-plex');
const plexConnectSpinner = document.getElementById('plex-connect-spinner');
const plexConnectError = document.getElementById('plex-connect-error');

let plexPollInterval = null;
let plexPollTimeout = null;

btnConnectPlex.addEventListener('click', async () => {
  clearInterval(plexPollInterval);
  clearTimeout(plexPollTimeout);
  plexPollInterval = null;
  plexPollTimeout = null;

  btnConnectPlex.disabled = true;
  plexConnectError.style.display = 'none';
  let data;
  try {
    const resp = await fetch('/admin/plex/connect/pin');
    if (!resp.ok) throw new Error();
    data = await resp.json();
  } catch {
    plexConnectError.textContent = 'Could not start Plex connection.';
    plexConnectError.style.display = '';
    btnConnectPlex.disabled = false;
    return;
  }
  window.open(data.auth_url, '_blank');
  plexConnectSpinner.style.display = '';

  plexPollInterval = setInterval(async () => {
    try {
      const r = await fetch(`/admin/plex/connect/poll/${data.id}?client_id=${data.client_id}`);
      const result = await r.json();
      if (result.resolved) {
        clearInterval(plexPollInterval);
        clearTimeout(plexPollTimeout);
        plexPollInterval = null;
        plexPollTimeout = null;
        plexConnectSpinner.style.display = 'none';
        btnConnectPlex.disabled = false;
        showToast('Plex connected');
        loadLibraries();
      }
    } catch {}
  }, 3000);

  plexPollTimeout = setTimeout(() => {
    clearInterval(plexPollInterval);
    plexPollInterval = null;
    plexPollTimeout = null;
    plexConnectSpinner.style.display = 'none';
    btnConnectPlex.disabled = false;
    plexConnectError.textContent = 'Plex authorization timed out.';
    plexConnectError.style.display = '';
  }, 120000);
});

// ── Settings ───────────────────────────────────────────────────────────────

// Glow-up U6: the install-wide default scheme, edited via a swatch grid
// built from the shared APPEARANCE_SCHEMES table (no bespoke control).
// Unlike the gear panel, ALL ten schemes show here — the selected default
// carries the .on ring. Selection is local until Save Settings POSTs.
let defaultScheme = 'gold-rush';

function renderDefaultSchemePicker() {
  const host = document.getElementById('default-scheme-picker');
  if (!host) return;
  host.innerHTML = '';
  Object.keys(APPEARANCE_SCHEMES).forEach(id => {
    const s = APPEARANCE_SCHEMES[id];
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'ap-swatch' + (id === defaultScheme ? ' on' : '');
    b.dataset.scheme = id;   // save-model U3 reads the selection off the DOM
    b.title = s.name;
    b.setAttribute('aria-label', 'Default scheme: ' + s.name);
    b.style.background = s.grad || s.anchor;
    b.addEventListener('click', () => { defaultScheme = id; renderDefaultSchemePicker(); });
    host.appendChild(b);
  });
}

// Render the Surprise "Recent suggestions" readout. Shared by the GET fetch at
// Setup-open and the live surprise_recorded WS event — both pass {recent, tally}.
function renderSurpriseRecent(data) {
  const el = document.getElementById('surprise-recent');
  if (!el) return;
  const tally = (data && data.tally) || {};
  const parts = Object.keys(tally).map(k => `${k} ×${tally[k]}`);
  el.textContent = parts.length ? parts.join(' · ') : 'No suggestions yet';
}

// ── Recent Plays curation panel (2026-07-03 plan; Direction A) ───────────────
// Admin-only Setup chrome to prune recent plays. Fed by the SAME history array
// that drives the shared read-only strip — captured from refreshQueueState() and
// the queue_changed WS event via setRecentPlaysData(). Pages 10-at-a-time client-
// side over the ~50-entry live buffer the /admin/queue payload already carries;
// removal reuses POST /admin/history/remove-play (the shipped inverse chokepoint).
// Row rendering lives here (not the shared playback module) because this is a
// distinct admin management surface, like renderSourcesList / renderSurpriseRecent.
let _recentPlaysData = [];
let _recentPlaysPage = 0;
let _recentPlaysGen = 0;
let _recentPlaysExpanded = false;

function _playedAgo(iso) {
  const t = Date.parse(iso);
  if (!t) return '';
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 45) return 'just now';
  const m = Math.round(s / 60);
  if (m < 60) return m + ' min ago';
  const h = Math.round(m / 60);
  if (h < 24) return h + (h === 1 ? ' hr ago' : ' hrs ago');
  const d = Math.round(h / 24);
  return d + (d === 1 ? ' day ago' : ' days ago');
}

// Store the latest history and refresh the panel. The count badge updates even
// when collapsed (it sits on the always-visible header); rows render only when
// expanded. Bumping the generation lets an in-flight removal detect a newer
// snapshot and skip its optimistic paint.
function setRecentPlaysData(history) {
  _recentPlaysData = Array.isArray(history) ? history : [];
  _recentPlaysGen++;
  renderRecentPlays();
}

function renderRecentPlays() {
  const badge = document.getElementById('recent-plays-count');
  const list = document.getElementById('recent-plays-list');
  const pager = document.getElementById('recent-plays-pager');
  if (!badge || !list || !pager) return;
  const n = _recentPlaysData.length;
  badge.textContent = n + (n === 1 ? ' play' : ' plays');
  if (!_recentPlaysExpanded) return;  // build rows only when the panel is open
  if (n === 0) {
    list.innerHTML = '<div class="rp-empty">No plays recorded yet.</div>';
    pager.innerHTML = '';
    return;
  }
  const perPage = 10;
  const maxPage = Math.max(0, Math.ceil(n / perPage) - 1);
  if (_recentPlaysPage > maxPage) _recentPlaysPage = maxPage;  // clamp after a shrink
  const start = _recentPlaysPage * perPage;
  const rows = _recentPlaysData.slice(start, start + perPage);
  list.innerHTML = rows.map((it) =>
    '<div class="rp-row">'
    + artImg(it.thumb, 'rp-art')
    + `<div class="rp-meta"><div class="rp-t">${esc(it.title)}</div>`
    + `<div class="rp-s">${esc(it.artist)}</div></div>`
    + `<div class="rp-when">${esc(_playedAgo(it.added_at))}</div>`
    + '<button type="button" class="rp-x" title="Remove this play">✕</button>'
    + '</div>').join('');
  list.querySelectorAll('.rp-x').forEach((btn, i) => {
    const it = rows[i];
    btn.setAttribute('aria-label', 'Remove ' + (it.title || 'this play') + ' from history');
    btn.addEventListener('click', () => removeRecentPlay(it.track_id || it.id, it.added_at));
  });
  pager.innerHTML =
    `<button type="button" class="rp-nav" aria-label="Newer page"${_recentPlaysPage === 0 ? ' disabled' : ''}>‹ Newer</button>`
    + `<span class="rp-lbl">Page ${_recentPlaysPage + 1} of ${maxPage + 1}</span>`
    + `<button type="button" class="rp-nav" aria-label="Older page"${_recentPlaysPage >= maxPage ? ' disabled' : ''}>Older ›</button>`;
  const [prev, next] = pager.querySelectorAll('.rp-nav');
  if (prev) prev.addEventListener('click', () => { if (_recentPlaysPage > 0) { _recentPlaysPage--; _recentPlaysGen++; renderRecentPlays(); } });
  if (next) next.addEventListener('click', () => { if (_recentPlaysPage < maxPage) { _recentPlaysPage++; _recentPlaysGen++; renderRecentPlays(); } });
}

function toggleRecentPlays() {
  const sec = document.getElementById('recent-plays');
  if (!sec) return;
  _recentPlaysExpanded = sec.classList.toggle('rp-collapsed') === false;
  const head = document.getElementById('recent-plays-head');
  if (head) head.setAttribute('aria-expanded', String(_recentPlaysExpanded));
  _recentPlaysGen++;
  renderRecentPlays();
}

// Remove one play. Raw fetch (admin api() throws on non-2xx and can't surface a
// 404): 2xx → optimistic filter + repaint (the queue_changed broadcast re-confirms;
// per the realtime-ui learning, write-then-render beats waiting on the event);
// 404 (already gone) → refetch the snapshot; any other status → toast. No confirm.
async function removeRecentPlay(trackId, addedAt) {
  const gen = _recentPlaysGen;
  let resp;
  try {
    resp = await fetch('/admin/history/remove-play', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ track_id: trackId, added_at: addedAt }),
    });
  } catch { showToast('Could not remove play'); return; }
  if (resp.status === 404) { refreshQueueState(); return; }
  if (!resp.ok) { showToast('Could not remove play'); return; }
  if (gen !== _recentPlaysGen) return;  // a newer snapshot already superseded us
  _recentPlaysData = _recentPlaysData.filter(
    (x) => !((x.track_id || x.id) === trackId && x.added_at === addedAt));
  renderRecentPlays();
}

// Wire the collapse toggle (the header is a <button> in the admin template, so
// Enter/Space fire click natively; this script runs at end of body).
(() => {
  const head = document.getElementById('recent-plays-head');
  if (head) head.addEventListener('click', toggleRecentPlays);
})();

async function loadSettings() {
  try {
    const s = await api('GET', '/admin/settings');
    document.querySelectorAll('[name=end-behavior]').forEach(r => { r.checked = r.value === (s.queue_end_behavior || 'stop'); });
    // The API maps legacy stored 'density' to waveform at the read edge
    // (U1), so the five-mode radio always finds a match.
    document.querySelectorAll('[name=rail-mode]').forEach(r => { r.checked = r.value === (s.rail_mode || 'vanilla'); });
    // Tile-view U4: install-wide default browse/search view.
    document.querySelectorAll('[name=default-view]').forEach(r => { r.checked = r.value === (s.default_view || 'list'); });
    defaultScheme = s.default_scheme || 'gold-rush';
    renderDefaultSchemePicker();
    // Rating display style (2026-06-27 plan U2): hydrate the radio; default stars.
    document.querySelectorAll('[name=rating-style]').forEach(r => { r.checked = r.value === (s.rating_style || 'stars'); });
    if (s.queue_display_n != null) document.getElementById('guest-n').value = s.queue_display_n;
    if (s.queue_display_m != null) document.getElementById('guest-m').value = s.queue_display_m;
    // Flood Control (2026-06-16): instant toggle, hydrated from the stored
    // flag. It lives by the Lock toggle, not the batched Save Settings form.
    const floodToggle = document.getElementById('flood-toggle');
    if (floodToggle) floodToggle.checked = !!s.flood_control;
    // Surprise Me (2026-06-17): master on/off (default on) + source mode.
    const surpriseEnabled = document.getElementById('surprise-enabled');
    if (surpriseEnabled) surpriseEnabled.checked = s.surprise_me_enabled !== false;
    // Lyrics contribute prompt (2026-06-23): default-on, hydrated like surprise
    // (unset/true → checked). Saved via the batched Save Settings POST below.
    const lyricsContribute = document.getElementById('lyrics-contribute-enabled');
    if (lyricsContribute) lyricsContribute.checked = s.lyrics_contribute_enabled !== false;
    const surpriseMode = document.getElementById('surprise-source-mode');
    if (surpriseMode && s.surprise_me_source_mode) surpriseMode.value = s.surprise_me_source_mode;
    const surpriseDiv = document.getElementById('surprise-diversity');
    if (surpriseDiv && s.surprise_me_diversity) surpriseDiv.value = s.surprise_me_diversity;
    // Random-pick length band (2026-06-20 plan U4): 0 = bound off, shown as an
    // empty box so the admin sees "no limit" rather than a literal 0.
    const randMin = document.getElementById('random-min-seconds');
    if (randMin) randMin.value = s.random_min_seconds || '';
    const randMax = document.getElementById('random-max-seconds');
    if (randMax) randMax.value = s.random_max_seconds || '';
    // Queue-end rework (plan U5): Popular Random threshold (empty box shows the
    // default 2 via the placeholder) + the opt-in length-limit checkbox. The
    // threshold field stays fully enabled regardless of the selected mode
    // (2026-06-24) — no dim/disable treatment.
    const popThresh = document.getElementById('popular-random-threshold');
    if (popThresh) popThresh.value = s.popular_random_threshold || '';
    // Most Played leaderboard size (2026-06-23) — empty box shows default 100
    // via the placeholder.
    const mpLimit = document.getElementById('most-played-display-limit');
    if (mpLimit) mpLimit.value = s.most_played_display_limit || '';
    // Track ratings + tags (2026-06-26 plan U9): visibility flags default OFF
    // (hydrate true only on an explicit true); facet flags default ON (!== false).
    const rvg = document.getElementById('ratings-visible-to-guests');
    if (rvg) rvg.checked = s.ratings_visible_to_guests === true;
    const tvg = document.getElementById('tags-visible-to-guests');
    if (tvg) tvg.checked = s.tags_visible_to_guests === true;
    // Radio Mode (2026-08-11 plan U8, R9): guest radio-control flag defaults OFF
    // (hydrate true only on an explicit true).
    const grc = document.getElementById('guest-radio-control');
    if (grc) grc.checked = s.guest_radio_control === true;
    ['genre', 'years', 'mostplayed', 'recentlyadded', 'highestrated'].forEach(f => {
      const el = document.getElementById('facet-' + f);
      if (el) el.checked = s['facet_' + f] !== false;
    });
    // Radio TAB visibility (2026-08-12): default OFF (opt-in), so hydrate checked
    // only on an explicit true — unlike the five facets above (default ON).
    const fRadio = document.getElementById('facet-radio');
    if (fRadio) fRadio.checked = s.facet_radio === true;
    // Closing Time (2026-06-24): hydrate toggle + trigger song/message. The GET
    // returns the resolved Semisonic defaults when unset, so the boxes show the
    // current values; clearing a box persists "" (and that trigger never fires).
    const ctEnabled = document.getElementById('closing-time-enabled');
    if (ctEnabled) ctEnabled.checked = !!s.closing_time_enabled;
    const ctTitle = document.getElementById('closing-time-title');
    if (ctTitle) ctTitle.value = s.closing_time_title || '';
    const ctArtist = document.getElementById('closing-time-artist');
    if (ctArtist) ctArtist.value = s.closing_time_artist || '';
    const ctMessage = document.getElementById('closing-time-message');
    if (ctMessage) ctMessage.value = s.closing_time_message || '';
    const qeLimit = document.getElementById('queue-end-length-limit');
    if (qeLimit) qeLimit.checked = !!s.queue_end_length_limit;
    // Gapless playback toggle (2026-07-11 supervisor plan U5): default OFF —
    // hydrate checked only on an explicit true (mirrors ratings-visible).
    const gaplessEnabled = document.getElementById('gapless-enabled');
    if (gaplessEnabled) gaplessEnabled.checked = s.gapless_enabled === true;
    // Auto-resume window (plan U5): the GET resolves the default (60), so the
    // box always shows the live value; the >= 1 floor is enforced server-side.
    const resumeWindow = document.getElementById('resume-window-minutes');
    if (resumeWindow) resumeWindow.value = s.resume_window_minutes || '';
    // Volume bar orientation (2026-08-04 volume rework U3): hydrate the radio
    // AND apply the render switch — the data-attribute is the single CSS hook
    // (body[data-vol-orient]); JS never branches on orientation.
    const volOrient = s.volume_orientation || 'horizontal';
    document.querySelectorAll('[name=volume-orientation]').forEach(r => { r.checked = r.value === volOrient; });
    document.body.dataset.volOrient = volOrient;
    // International rail (2026-06-22 plan 004): alpha-mode radios + the two
    // first-character thresholds (empty box → default 2 via placeholder). The
    // threshold row is ALWAYS visible — dimmed + disabled when International isn't
    // the selected alpha mode.
    document.querySelectorAll('[name=rail-alpha-mode]').forEach(r => { r.checked = r.value === (s.rail_alpha_mode || 'english'); });
    const artistThresh = document.getElementById('rail-artist-threshold');
    if (artistThresh) artistThresh.value = s.rail_artist_threshold || '';
    const albumThresh = document.getElementById('rail-album-threshold');
    if (albumThresh) albumThresh.value = s.rail_album_threshold || '';
    const intlActive = (s.rail_alpha_mode === 'international');
    const intlRow = document.getElementById('international-thresholds-row');
    if (intlRow) intlRow.classList.toggle('is-inactive', !intlActive);
    if (artistThresh) artistThresh.disabled = !intlActive;
    if (albumThresh) albumThresh.disabled = !intlActive;
    // Source-attribution readout (U6): which method actually produced recent
    // suggestions. Seeded here at Setup-open; kept live by the surprise_recorded
    // WS event (renderSurpriseRecent is the shared render path).
    try { renderSurpriseRecent(await api('GET', '/admin/surprise/recent')); }
    catch { const el = document.getElementById('surprise-recent'); if (el) el.textContent = '—'; }
    // Save-model U3: the pending baseline is this response, captured AFTER the
    // full hydrate and INSIDE this try. A throw mid-hydrate leaves the form
    // half-populated, and a baseline taken outside would record defaults for
    // the fields that never got written. An event rather than a shared global:
    // the bookkeeping lives in an IIFE, so nothing new reaches top level.
    document.dispatchEvent(new CustomEvent('jp:settings-hydrated', { detail: s }));
  } catch {}
}

document.getElementById('btn-save-settings').addEventListener('click', async () => {
  const endBehavior = document.querySelector('[name=end-behavior]:checked').value;
  const railMode = document.querySelector('[name=rail-mode]:checked').value;
  const defaultView = document.querySelector('[name=default-view]:checked').value;
  // Rating display style (2026-06-27 plan U2): install-wide look, sent with the
  // other appearance defaults.
  const ratingStyle = document.querySelector('[name=rating-style]:checked').value;
  const n = document.getElementById('guest-n').value;
  const m = document.getElementById('guest-m').value;
  const body = { queue_end_behavior: endBehavior, rail_mode: railMode, default_scheme: defaultScheme, default_view: defaultView, rating_style: ratingStyle };
  if (n !== '') body.queue_display_n = parseInt(n, 10);
  if (m !== '') body.queue_display_m = parseInt(m, 10);
  // Surprise Me: only the enable flag is batched — it is in the appearance
  // broadcast. Its source-mode and diversity selects commit on change.
  const surpriseEnabledEl = document.getElementById('surprise-enabled');
  if (surpriseEnabledEl) body.surprise_me_enabled = surpriseEnabledEl.checked;
  // Random-pick length band (plan U4): always send both as integers, empty → 0,
  // so clearing a box turns that bound off (diverges from guest-n/m's "empty =
  // unchanged" on purpose — 0 is the canonical off sentinel here).
  const randMinEl = document.getElementById('random-min-seconds');
  const randMaxEl = document.getElementById('random-max-seconds');
  if (randMinEl) body.random_min_seconds = parseInt(randMinEl.value, 10) || 0;
  if (randMaxEl) body.random_max_seconds = parseInt(randMaxEl.value, 10) || 0;
  // Queue-end rework (plan U5): threshold (empty/0 → default 2) + length-limit
  // checkbox for the queue-end random modes.
  const popThreshEl = document.getElementById('popular-random-threshold');
  if (popThreshEl) body.popular_random_threshold = parseInt(popThreshEl.value, 10) || 2;
  // Most Played leaderboard size (empty/0 → default 100). Display-only.
  const mpLimitEl = document.getElementById('most-played-display-limit');
  if (mpLimitEl) body.most_played_display_limit = parseInt(mpLimitEl.value, 10) || 100;
  // Closing Time (2026-06-24): toggle + trigger song/message. Strings persist
  // as typed (server trims); a blank trigger simply never fires.
  const ctEnabledEl = document.getElementById('closing-time-enabled');
  if (ctEnabledEl) body.closing_time_enabled = ctEnabledEl.checked;
  const ctTitleEl = document.getElementById('closing-time-title');
  if (ctTitleEl) body.closing_time_title = ctTitleEl.value;
  const ctArtistEl = document.getElementById('closing-time-artist');
  if (ctArtistEl) body.closing_time_artist = ctArtistEl.value;
  const ctMessageEl = document.getElementById('closing-time-message');
  if (ctMessageEl) body.closing_time_message = ctMessageEl.value;
  // Auto-resume window (2026-07-11 supervisor plan U5). Empty/0 → default 60
  // (mirrors most-played-display-limit's shape). Gapless left this batch on
  // 2026-10-05: it lives in Output now and commits the moment it is toggled, so
  // this collector must NOT send gapless_enabled — a stale read here would
  // silently revert a toggle made after the page loaded.
  const resumeWindowEl = document.getElementById('resume-window-minutes');
  if (resumeWindowEl) body.resume_window_minutes = parseInt(resumeWindowEl.value, 10) || 60;
  // International rail (plan 004): alpha-mode + the two thresholds (empty/0 →
  // default 2, mirroring the popular-threshold field).
  const alphaModeEl = document.querySelector('[name=rail-alpha-mode]:checked');
  if (alphaModeEl) body.rail_alpha_mode = alphaModeEl.value;
  const artistThreshEl = document.getElementById('rail-artist-threshold');
  if (artistThreshEl) body.rail_artist_threshold = parseInt(artistThreshEl.value, 10) || 2;
  const albumThreshEl = document.getElementById('rail-album-threshold');
  if (albumThreshEl) body.rail_album_threshold = parseInt(albumThreshEl.value, 10) || 2;
  try {
    await api('POST', '/admin/settings', body);
    showToast('Settings saved');
    // Rebase the pending baseline by re-reading, so the marks clear against
    // what the server actually stored rather than against the form. Only on
    // success: a rejected Save applies nothing and must leave every mark up.
    loadSettings();
  }
  catch (err) {
    // The server validates everything before persisting, so a 4xx applied
    // NOTHING — every mark stays up, and the one useful thing to add is which
    // field it refused. api() already attaches status and detail; the old bare
    // catch threw both away, leaving a retry that fails identically.
    const d = err && err.detail;
    let field = null, msg = null;
    if (typeof d === 'string') {
      msg = d;
    } else if (Array.isArray(d)) {
      // FastAPI validation shape: loc is ['body', '<field>'].
      const first = d[0] || {};
      const loc = first.loc || [];
      field = loc[loc.length - 1] || null;
      msg = first.msg || null;
    } else if (d && typeof d === 'object') {
      field = d.field || null;
      msg = d.message || d.reason || null;
    }
    showToast(field
      ? 'Save rejected — ' + field + ': ' + (msg || 'invalid value')
      : 'Failed to save settings' + (msg ? ' — ' + msg : ''));
    document.dispatchEvent(new CustomEvent('jp:settings-rejected', {
      detail: { field: field, message: msg },
    }));
  }
});

// ── Commit-on-change (2026-10-05 settings save-model U2) ───────────────────
// Fifteen controls write themselves the moment they change, so no Save press
// stands between the operator and the effect. A control is admitted only if all
// three hold: its value is final the instant it changes; committing it does not
// change what the box does on its own; and its key is not in the server's
// appearance-broadcast set. `end-behavior` fails the second (a stray click would
// start an auto-DJ) and the six appearance keys fail the third (each commit
// would repaint every guest device mid-comparison), so all seven stay on Save.
//
// Every admitted key was removed from the Save collector in the same change.
// Leaving one would let a Save press re-send the value the form held at page
// load, silently reverting a commit made since — or made in another tab.
//
// IIFE expression statement: inner declarations are indented, so this adds no
// top-level name (static discipline), matching the ⓘ-popover block below.
(function () {
  // [element id, settings key, name used in a failure toast]
  const CHECKBOXES = [
    ['gapless-enabled', 'gapless_enabled', 'Gapless playback'],
    ['queue-end-length-limit', 'queue_end_length_limit', 'Queue-end length limit'],
    ['lyrics-contribute-enabled', 'lyrics_contribute_enabled', 'Lyrics contribution'],
    ['ratings-visible-to-guests', 'ratings_visible_to_guests', 'Guest rating visibility'],
    ['tags-visible-to-guests', 'tags_visible_to_guests', 'Guest tag visibility'],
    ['guest-radio-control', 'guest_radio_control', 'Guest radio control'],
    ['facet-genre', 'facet_genre', 'Genre tab'],
    ['facet-years', 'facet_years', 'Years tab'],
    ['facet-mostplayed', 'facet_mostplayed', 'Most Played tab'],
    ['facet-recentlyadded', 'facet_recentlyadded', 'Recently Added tab'],
    ['facet-highestrated', 'facet_highestrated', 'Highest Rated tab'],
    ['facet-radio', 'facet_radio', 'Radio tab'],
  ];
  const SELECTS = [
    ['surprise-source-mode', 'surprise_me_source_mode', 'Surprise Me source'],
    ['surprise-diversity', 'surprise_me_diversity', 'Surprise Me diversity'],
  ];
  // [radio group name, settings key, name used in a failure toast]
  const RADIO_GROUPS = [
    ['volume-orientation', 'volume_orientation', 'Volume bar orientation'],
  ];

  // showToast is a singleton with one shared timer, so a burst of commits would
  // clobber each other and leave only the last one visible. Successes inside
  // this window collapse into one message naming the count. One constant, so
  // the behaviour is a single decision rather than a per-call-site habit.
  const COALESCE_MS = 1200;
  let okCount = 0;
  let okTimer = null;

  function flushOk() {
    okTimer = null;
    if (!okCount) return;
    showToast(okCount === 1 ? 'Setting saved' : okCount + ' settings saved');
    okCount = 0;
  }

  function noteOk() {
    okCount += 1;
    if (okTimer) clearTimeout(okTimer);
    okTimer = setTimeout(flushOk, COALESCE_MS);
  }

  // A failure is never absorbed into a coalesced success: it cancels the pending
  // count and names the control instead. Reporting "3 settings saved" when one
  // of the three was rejected is the one outcome coalescing must not produce.
  function noteFailure(label, err) {
    if (okTimer) { clearTimeout(okTimer); okTimer = null; }
    okCount = 0;
    const d = err && err.detail;
    showToast(label + ': ' + ((typeof d === 'string' && d) || 'could not save'));
  }

  // Reverting a rejected commit needs the value the control held BEFORE the
  // change, and `change` arrives after the DOM has already moved. The snapshot
  // is taken on focus — a pointer or keyboard interaction always focuses a
  // control before it can change it — and refreshed after each success, so a
  // second edit reverts to what the server actually holds. A checkbox needs no
  // snapshot: its previous state is the inverse of its current one.
  const prior = new Map();

  // The flood-control sequence, once: lock, POST exactly one key, confirm, and
  // on failure put the control back. The revert is the load-bearing half —
  // without it the form shows a value the server refused.
  async function commit(els, key, label, read, revert, onOk) {
    els.forEach(c => { c.disabled = true; });
    try {
      const value = read();
      await api('POST', '/admin/settings', { [key]: value });
      if (onOk) onOk(value);
      noteOk();
    } catch (err) {
      revert();
      noteFailure(label, err);
    } finally {
      els.forEach(c => { c.disabled = false; });
    }
  }

  // Mark the row as instant-committing so the static-cue pass can tag it
  // without a second copy of the admitted list. This block owns that list.
  function markInstant(el) {
    const row = el.closest('.panel-row');
    if (row) row.dataset.saveMode = 'instant';
  }

  CHECKBOXES.forEach(([id, key, label]) => {
    document.querySelectorAll('#' + id).forEach(el => {
      markInstant(el);
      el.addEventListener('change', () => commit(
        [el], key, label,
        () => el.checked,
        () => { el.checked = !el.checked; },
      ));
    });
  });

  SELECTS.forEach(([id, key, label]) => {
    document.querySelectorAll('#' + id).forEach(el => {
      markInstant(el);
      el.addEventListener('focus', () => { if (!el.disabled) prior.set(el, el.value); });
      el.addEventListener('change', () => commit(
        [el], key, label,
        () => el.value,
        () => { if (prior.has(el)) el.value = prior.get(el); },
        (v) => prior.set(el, v),
      ));
    });
  });

  RADIO_GROUPS.forEach(([name, key, label]) => {
    const group = Array.from(document.querySelectorAll('[name=' + name + ']'));
    if (!group.length) return;
    markInstant(group[0]);
    const checkedValue = () => {
      const sel = group.find(r => r.checked);
      return sel ? sel.value : null;
    };
    group.forEach(r => {
      // Snapshot on focus: the click that focuses a radio sets `checked` in the
      // click default action, which runs after focus — so the group still holds
      // the outgoing selection here.
      r.addEventListener('focus', () => {
        const v = checkedValue();
        if (v !== null) prior.set(group, v);
      });
      r.addEventListener('change', () => {
        if (!r.checked) return;
        commit(
          group, key, label,
          () => r.value,
          () => {
            const was = prior.get(group);
            const back = group.find(x => x.value === was);
            if (back) back.checked = true;
          },
          (v) => {
            prior.set(group, v);
            // volume_orientation's local apply moves with the key rather than
            // being stranded on a Save path that no longer sends it.
            if (key === 'volume_orientation') document.body.dataset.volOrient = v;
          },
        );
      });
    });
  });
})();

// Popular-threshold field (2026-06-24): intentionally NOT mode-gated — it stays
// fully enabled regardless of the selected Queue-end mode (reverses the earlier
// dim+disable treatment). The '(applies to Popular Random)' note clarifies scope.

// International rail (plan 004): always-visible dim+disable pattern — the
// thresholds row is active only when the International alpha mode is selected.
// Expression statement (no new top-level declaration → respects static discipline).
document.querySelectorAll('[name=rail-alpha-mode]').forEach(r => r.addEventListener('change', () => {
  const sel = document.querySelector('[name=rail-alpha-mode]:checked');
  const active = !!sel && sel.value === 'international';
  const row = document.getElementById('international-thresholds-row');
  const artistInp = document.getElementById('rail-artist-threshold');
  const albumInp = document.getElementById('rail-album-threshold');
  if (row) row.classList.toggle('is-inactive', !active);
  if (artistInp) artistInp.disabled = !active;
  if (albumInp) albumInp.disabled = !active;
}));

// ── Pending-change state (2026-10-05 settings save-model U3) ──────────────
// Twenty Settings controls still need an explicit Save — see the admission
// rules above for why each is excluded. This makes that legible: a static cue
// on every row saying which kind it is, a marker on the rows actually holding
// an uncommitted edit, and one count in a bar that exists only when something
// is pending.
//
// The baseline is the GET /admin/settings response, never the DOM. Hydration
// defaults are deliberately inconsistent — `!== false` for default-on flags,
// `!!` for default-off — and numerics hydrate `|| ''`, so a stored 0 renders as
// an empty box. Comparing DOM strings against those would report changes nobody
// made. Normalisation on both sides mirrors the Save collector's own coercion,
// so a box that hydrated empty and was typed back to its stored value reads
// clean.
//
// One IIFE, indented declarations: no new top-level name (static discipline).
(function () {
  // num: the default the collector substitutes for an empty box.
  // blankKeeps: empty means "leave the stored value alone" (guest-n/m only).
  // boolOn: hydrates checked unless explicitly false, rather than the reverse.
  const FIELDS = [
    { id: 'popular-random-threshold',  key: 'popular_random_threshold',  num: 2 },
    { id: 'resume-window-minutes',     key: 'resume_window_minutes',     num: 60 },
    { id: 'random-min-seconds',        key: 'random_min_seconds',        num: 0 },
    { id: 'random-max-seconds',        key: 'random_max_seconds',        num: 0 },
    { id: 'rail-artist-threshold',     key: 'rail_artist_threshold',     num: 2 },
    { id: 'rail-album-threshold',      key: 'rail_album_threshold',      num: 2 },
    { id: 'most-played-display-limit', key: 'most_played_display_limit', num: 100 },
    { id: 'guest-n', key: 'queue_display_n', num: 0, blankKeeps: true },
    { id: 'guest-m', key: 'queue_display_m', num: 0, blankKeeps: true },
    { id: 'closing-time-enabled', key: 'closing_time_enabled', bool: true },
    { id: 'surprise-enabled',     key: 'surprise_me_enabled',  bool: true, boolOn: true },
    { id: 'closing-time-title',   key: 'closing_time_title',   text: true },
    { id: 'closing-time-artist',  key: 'closing_time_artist',  text: true },
    { id: 'closing-time-message', key: 'closing_time_message', text: true },
    { radio: 'end-behavior',    key: 'queue_end_behavior', fallback: 'stop' },
    { radio: 'rail-mode',       key: 'rail_mode',          fallback: 'vanilla' },
    { radio: 'rail-alpha-mode', key: 'rail_alpha_mode',    fallback: 'english' },
    { radio: 'default-view',    key: 'default_view',       fallback: 'list' },
    { radio: 'rating-style',    key: 'rating_style',       fallback: 'stars' },
    { scheme: true,             key: 'default_scheme',     fallback: 'gold-rush' },
  ];

  // A mode plus the parameters it governs, or a cross-validated pair: marked
  // together and counted once, because committing half of one is not a thing
  // the operator asked for.
  const GROUPS = [
    ['closing_time_enabled', 'closing_time_title', 'closing_time_artist', 'closing_time_message'],
    ['random_min_seconds', 'random_max_seconds'],
    ['rail_alpha_mode', 'rail_artist_threshold', 'rail_album_threshold'],
    ['queue_end_behavior', 'popular_random_threshold'],
  ];

  // The Pattern Matching and Artist Exclusion editors keep their own dirty flag
  // and their own endpoints. They are counted here so the bar tells the truth
  // about everything outstanding, and its Save drives their Save buttons too.
  const EDITORS = '__editors';

  let base = null;

  const byKey = {};
  FIELDS.forEach(f => { byKey[f.key] = f; });

  function fieldEl(f) {
    if (f.radio) return document.querySelector('[name=' + f.radio + ']');
    if (f.scheme) return document.getElementById('default-scheme-picker');
    return document.getElementById(f.id);
  }

  function rowFor(f) {
    const el = fieldEl(f);
    return el ? el.closest('.panel-row') : null;
  }

  function baseValue(f) {
    const v = base ? base[f.key] : undefined;
    if (f.radio || f.scheme) return (v === undefined || v === null) ? f.fallback : v;
    if (f.bool) return f.boolOn ? v !== false : !!v;
    if (f.text) return (v === undefined || v === null) ? '' : String(v);
    return parseInt(v, 10) || f.num;
  }

  function nowValue(f) {
    if (f.radio) {
      const sel = document.querySelector('[name=' + f.radio + ']:checked');
      return sel ? sel.value : f.fallback;
    }
    if (f.scheme) {
      const on = document.querySelector('#default-scheme-picker .ap-swatch.on');
      return on ? on.dataset.scheme : f.fallback;
    }
    const el = document.getElementById(f.id);
    if (!el) return baseValue(f);
    if (f.bool) return el.checked;
    if (f.text) return el.value;
    if (el.value === '') return f.blankKeeps ? baseValue(f) : f.num;
    return parseInt(el.value, 10) || f.num;
  }

  // Pure, and exposed for the node harness. Given the group definitions, a
  // field-to-row lookup and the dirty field keys, return the rows to mark and
  // the number of changes to report.
  //
  // The dedupe is the whole point. All four Closing Time fields live in ONE
  // .panel-row, so mapping a group's fields to rows yields that row four times;
  // a loop that adds it on the first field and clears it on the rest reports a
  // real pending change as zero. The mockup did exactly that, and showed
  // "1 Unsaved Change" while two rows were visibly marked. Ungrouped fields
  // coalesce by row for the same reason — guest-n and guest-m share one row and
  // are one change, without needing an entry in GROUPS.
  function pendingUnits(groups, rowOf, dirty) {
    const grouped = {};
    const units = [];
    groups.forEach(g => { g.forEach(k => { grouped[k] = 1; }); units.push(g); });
    const byRow = {};
    const rowOrder = [];
    Object.keys(rowOf).forEach(k => {
      if (grouped[k]) return;
      if (rowOf[k] === null || rowOf[k] === undefined) return;
      const r = String(rowOf[k]);
      if (!byRow[r]) { byRow[r] = []; rowOrder.push(r); }
      byRow[r].push(k);
    });
    rowOrder.forEach(r => units.push(byRow[r]));

    const rows = [];
    let count = 0;
    units.forEach(ks => {
      if (!ks.some(k => dirty.indexOf(k) !== -1)) return;
      count += 1;
      ks.forEach(k => {
        const r = rowOf[k];
        if (r !== null && r !== undefined && rows.indexOf(r) === -1) rows.push(r);
      });
    });
    return { count: count, rows: rows };
  }
  window.__jpPendingUnits = pendingUnits;

  // ── static cue ───────────────────────────────────────────────────────────
  // Runs once the rows exist. The instant rows were stamped by the
  // commit-on-change block, so the admitted list is not duplicated here.
  function tagRows() {
    document.querySelectorAll('#settings .panel-row, #output .panel-row').forEach(row => {
      if (row.dataset.saveTagged) return;
      const instant = row.dataset.saveMode === 'instant';
      const deferred = FIELDS.some(f => rowFor(f) === row);
      if (!instant && !deferred) return;
      row.dataset.saveTagged = '1';
      const lead = row.querySelector('label');
      if (lead) {
        const tag = document.createElement('span');
        tag.className = 'save-tag ' + (instant ? 'is-instant' : 'is-deferred');
        tag.textContent = instant ? 'instant' : 'needs save';
        lead.appendChild(tag);
      }
      if (!instant) {
        // The reserved slot, present from load and merely revealed when the row
        // goes pending — never inserted on change, which would reflow the row.
        const slot = document.createElement('span');
        slot.className = 'row-status';
        slot.textContent = 'unsaved';
        row.appendChild(slot);
      }
    });
  }

  // ── pending computation ──────────────────────────────────────────────────
  const barEl = () => document.getElementById('pending-bar');
  const countEl = () => document.getElementById('pending-count');
  let marked = [];

  function editorsRow() {
    const btn = document.getElementById('btn-save-pattern-rules');
    return btn ? btn.closest('.panel-row') : null;
  }

  function recompute() {
    if (!base) return;
    const rowOf = {};
    const rowList = [];
    const idOf = (el) => {
      if (!el) return null;
      let i = rowList.indexOf(el);
      if (i === -1) { i = rowList.length; rowList.push(el); }
      return i;
    };
    const dirty = [];
    FIELDS.forEach(f => {
      rowOf[f.key] = idOf(rowFor(f));
      if (nowValue(f) !== baseValue(f)) dirty.push(f.key);
    });
    rowOf[EDITORS] = idOf(editorsRow());
    if (typeof editorsDirty !== 'undefined' && editorsDirty) dirty.push(EDITORS);

    const out = pendingUnits(GROUPS, rowOf, dirty);

    marked.forEach(r => r.classList.remove('is-pending'));
    marked = out.rows.map(i => rowList[i]).filter(Boolean);
    marked.forEach(r => r.classList.add('is-pending'));
    // Editing a refused row clears its rejection — the operator has answered
    // the complaint, and leaving the danger treatment up would describe the
    // value they just replaced.
    marked.forEach(r => {
      if (!r.classList.contains('is-rejected')) return;
      r.classList.remove('is-rejected');
      const det = r.querySelector('.reject-detail');
      if (det) det.remove();
    });

    const bar = barEl(), cnt = countEl();
    if (!bar || !cnt) return;
    cnt.innerHTML = out.count === 1
      ? '<b>1</b> Unsaved Change'
      : '<b>' + out.count + '</b> Unsaved Changes';
    bar.classList.toggle('show', out.count > 0);
    window.__jpPendingCount = out.count;
  }

  // One recompute per interaction rather than a listener per control: the
  // comparison is twenty values, and it also catches the rule editors, whose
  // dirty flag is set from a dozen places.
  document.addEventListener('input', recompute, true);
  document.addEventListener('change', recompute, true);
  document.addEventListener('click', () => setTimeout(recompute, 0), true);

  // A rejected Save names one field; mark that row so the operator can see
  // which of several pending rows the server refused, without a console.
  document.addEventListener('jp:settings-rejected', (e) => {
    const d = e.detail || {};
    document.querySelectorAll('.panel-row.is-rejected').forEach(r => {
      r.classList.remove('is-rejected');
      const old = r.querySelector('.reject-detail');
      if (old) old.remove();
    });
    const f = byKey[d.field];
    const row = f ? rowFor(f) : null;
    if (!row) return;
    row.classList.add('is-rejected');
    if (d.message && !row.querySelector('.reject-detail')) {
      const det = document.createElement('span');
      det.className = 'reject-detail';
      det.textContent = d.message;
      row.appendChild(det);
    }
  });

  // The cue is static information about which controls need Save, so it must
  // not wait on the settings GET: if that request fails the operator still has
  // a form in front of them, and losing the cue there is losing it exactly
  // when the page is least trustworthy. Idempotent, so the hydrate can re-run
  // it after a re-render without doubling the tags.
  tagRows();

  document.addEventListener('jp:settings-hydrated', (e) => {
    base = e.detail || {};
    tagRows();
    recompute();
  });

  document.addEventListener('click', (e) => {
    if (e.target.closest('#pending-save')) {
      // Drive the existing Save rather than duplicating the collector, and the
      // editors' own Saves too — a bar reporting three changes must save three.
      const btn = document.getElementById('btn-save-settings');
      if (btn) btn.click();
      if (typeof editorsDirty !== 'undefined' && editorsDirty) {
        const pr = document.getElementById('btn-save-pattern-rules');
        const ex = document.getElementById('btn-save-exclusions');
        if (pr) pr.click();
        if (ex) ex.click();
      }
    } else if (e.target.closest('#pending-cancel')) {
      // Re-reading is the restore: it rewrites every field from the server and
      // re-publishes the baseline, so there is one definition of "original".
      loadSettings();
      loadRuleEditors();
    }
  });
})();

// Info-circle popovers (2026-06-24): clicking an ⓘ shows its help text in a small
// popover and NEVER toggles the radio/checkbox it sits inside (preventDefault on
// the bubbling event cancels the <label>'s forward-to-control default; the button
// is also interactive content). Reaches touch users, who get no hover tooltip.
// IIFE expression statement — inner declarations are indented, so no new
// top-level name (respects the static-discipline allowlist).
(function () {
  let pop = null, openFor = null;
  function closeInfo() { if (pop) { pop.remove(); pop = null; openFor = null; } }
  document.addEventListener('click', (e) => {
    const icon = e.target.closest('.info-i');
    if (!icon) { closeInfo(); return; }
    e.preventDefault();
    e.stopPropagation();
    if (openFor === icon) { closeInfo(); return; }   // second click on same ⓘ closes
    closeInfo();
    const text = icon.getAttribute('title') || '';
    if (!text) return;
    pop = document.createElement('div');
    pop.className = 'info-pop';
    pop.textContent = text;
    document.body.appendChild(pop);
    openFor = icon;
    const r = icon.getBoundingClientRect();
    const left = Math.max(8, Math.min(r.left, window.innerWidth - pop.offsetWidth - 8));
    pop.style.left = left + 'px';
    pop.style.top = (r.bottom + 6) + 'px';
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeInfo(); });
  window.addEventListener('scroll', closeInfo, true);
})();

// ── Logout ─────────────────────────────────────────────────────────────────

// ── Pattern Matching + Artist Exclusion editors (pattern-rules plan U4) ────
// Local-state model (review decision): rows mutate `patternRules` /
// `exclusionNames` only; nothing POSTs until the section's Save. Navigating
// away discards (guarded by the .ptab confirm above) — that is the undo.

let patternRules = [];
let exclusionNames = [];
let editorsDirty = false;

function renderPatternRules() {
  const host = document.getElementById('pattern-rule-list');
  host.innerHTML = '';
  if (!patternRules.length) {
    host.innerHTML = '<div class="rule-empty-state">No rules yet — Add Rule to define equivalent strings (e.g. &amp; and "and").</div>';
    return;
  }
  patternRules.forEach((rule, ri) => {
    const row = document.createElement('div');
    row.className = 'rule-row';
    rule.forEach((val, si) => {
      const input = document.createElement('input');
      input.type = 'text';
      input.value = val;
      input.addEventListener('input', () => {
        patternRules[ri][si] = input.value;
        editorsDirty = true;
        syncInertHint(row, patternRules[ri]);
      });
      row.appendChild(input);
    });
    const addStr = document.createElement('button');
    addStr.className = 'rule-mini-btn';
    addStr.textContent = 'add string';
    addStr.addEventListener('click', () => { patternRules[ri].push(''); editorsDirty = true; renderPatternRules(); });
    row.appendChild(addStr);
    if (rule.length >= 3) {
      // Removes the right-most field regardless of content (review decision:
      // backspace mental model, no confirm).
      const rmStr = document.createElement('button');
      rmStr.className = 'rule-mini-btn';
      rmStr.textContent = 'remove string';
      rmStr.addEventListener('click', () => { patternRules[ri].pop(); editorsDirty = true; renderPatternRules(); });
      row.appendChild(rmStr);
    }
    const rm = document.createElement('button');
    rm.className = 'rule-remove';
    rm.title = 'Remove rule';
    rm.textContent = '✕';
    rm.addEventListener('click', () => { patternRules.splice(ri, 1); editorsDirty = true; renderPatternRules(); });
    row.appendChild(rm);
    syncInertHint(row, rule);
    host.appendChild(row);
  });
}

function syncInertHint(row, rule) {
  const existing = row.querySelector('.rule-inert-hint');
  const inert = rule.filter(s => s && s.trim()).length < 2;
  if (inert && !existing) {
    const hint = document.createElement('div');
    hint.className = 'rule-inert-hint';
    hint.textContent = 'Needs at least 2 filled strings to take effect';
    row.appendChild(hint);
  } else if (!inert && existing) {
    existing.remove();
  }
}

function renderExclusions() {
  const host = document.getElementById('exclusion-list');
  host.innerHTML = '';
  if (!exclusionNames.length) {
    host.innerHTML = '<div class="rule-empty-state">No exclusions — Add Rule to hide an artist from the browse list.</div>';
    return;
  }
  exclusionNames.forEach((val, i) => {
    const row = document.createElement('div');
    row.className = 'rule-row';
    const input = document.createElement('input');
    input.type = 'text';
    input.value = val;
    input.addEventListener('input', () => { exclusionNames[i] = input.value; editorsDirty = true; });
    row.appendChild(input);
    const rm = document.createElement('button');
    rm.className = 'rule-remove';
    rm.title = 'Remove exclusion';
    rm.textContent = '✕';
    rm.addEventListener('click', () => { exclusionNames.splice(i, 1); editorsDirty = true; renderExclusions(); });
    row.appendChild(rm);
    host.appendChild(row);
  });
}

async function loadRuleEditors() {
  try {
    const r = await api('GET', '/admin/pattern-rules');
    patternRules = r.rules || [];
    const e = await api('GET', '/admin/artist-exclusions');
    exclusionNames = e.names || [];
    editorsDirty = false;
    renderPatternRules();
    renderExclusions();
  } catch {}
}

document.getElementById('btn-add-pattern-rule').addEventListener('click', () => {
  patternRules.push(['', '']);  // origin R2: a new rule starts with two fields
  editorsDirty = true;
  renderPatternRules();
});

document.getElementById('btn-add-exclusion').addEventListener('click', () => {
  exclusionNames.push('');
  editorsDirty = true;
  renderExclusions();
});

document.getElementById('btn-save-pattern-rules').addEventListener('click', async () => {
  try {
    await api('POST', '/admin/pattern-rules', { rules: patternRules });
    const active = patternRules.filter(r => r.filter(s => s && s.trim()).length >= 2).length;
    showToast(`Rules saved — ${active} active`);
    editorsDirty = false;
    // Stale-view fix (review decision): the mounted browse module compiled
    // rules at mount; refresh it in place so this page's Artists view
    // reflects the save without a reload.
    if (browseHandle && browseHandle.refreshPatternRules) browseHandle.refreshPatternRules();
    loadRuleEditors();  // GET re-render; failure here is display-only
  } catch {
    showToast('Failed to save rules');
  }
});

document.getElementById('btn-save-exclusions').addEventListener('click', async () => {
  // Trim and silently drop empty/whitespace entries (review decision —
  // standard text-input behavior).
  const names = exclusionNames.map(s => (s || '').trim()).filter(Boolean);
  try {
    await api('POST', '/admin/artist-exclusions', { names });
    showToast('Exclusions saved');
    editorsDirty = false;
    loadRuleEditors();
  } catch {
    showToast('Failed to save exclusions');
  }
});

document.getElementById('logout-link').addEventListener('click', async (e) => {
  e.preventDefault();
  await api('POST', '/admin/auth/logout');
  window.location.href = '/admin/login';
});

// ── Initial load ───────────────────────────────────────────────────────────

// ── Optional integrations: server-fed multi-room backends (plan U9) ──────────
// Admin-only Setup chrome (guests never load this template). The zone sliders
// reuse the master volume's --vol-fill visual treatment but POST to their own
// per-client/per-group endpoints — a distinct admin surface (like the Sources
// panel), NOT a fork of the shared playback/browse modules. Enable is the
// explicit opt-in; Sendspin carries an "Experimental" badge (no separate modal).
let _integrationsData = null;
let _zoneVolTimers = {};

async function loadIntegrations() {
  try {
    _integrationsData = await api('GET', '/admin/output/integrations');
  } catch (e) {
    _integrationsData = null;
  }
  renderMultiroomPanel();
}

function renderMultiroomPanel() {
  const host = document.getElementById('multiroom-panel');
  if (!host) return;
  if (!_integrationsData) {
    host.innerHTML = '<p style="color:#a55;font-size:.85rem">Could not load integrations.</p>';
    return;
  }
  host.innerHTML = '';
  for (const backend of ['snapcast', 'sendspin']) {
    const info = _integrationsData[backend] || {};
    const card = document.createElement('div');
    card.className = 'panel-row';
    card.style.cssText = 'display:block;border:1px solid var(--border);border-radius:8px;padding:.75rem 1rem;margin-bottom:.6rem';
    const label = PROTOCOL_LABELS[backend] || backend;
    const badge = info.experimental
      ? ' <span title="Technical preview — protocol may change" style="font-size:.7rem;background:#4a3a1a;color:#e8c877;border-radius:4px;padding:.1rem .35rem;vertical-align:middle">Experimental</span>'
      : '';
    const clientNote = info.connected
      ? (info.client_count > 0
          ? `${info.client_count} client${info.client_count === 1 ? '' : 's'} connected`
          : 'No clients connected — audio is running but not audible')
      : 'Off';
    card.innerHTML =
      `<div style="display:flex;align-items:center;gap:.6rem;justify-content:space-between">
         <div><strong>${esc(label)}</strong>${badge}
           <div style="font-size:.8rem;color:#999" data-role="status">${esc(clientNote)}</div></div>
         <label style="display:flex;align-items:center;gap:.4rem;font-size:.85rem">
           <input type="checkbox" data-backend="${backend}" ${info.enabled ? 'checked' : ''}> Enable</label>
       </div>
       <div data-role="failure" style="display:none;color:#e88;font-size:.8rem;margin-top:.4rem"></div>
       <div data-role="config"></div>
       <div data-role="zones" style="margin-top:.6rem"></div>
       <div data-role="pairing" style="margin-top:.6rem"></div>`;
    const toggle = card.querySelector('input[type=checkbox]');
    if (toggle) toggle.addEventListener('change', () => toggleIntegration(backend, toggle, card));
    host.appendChild(card);
    // The external-server form is available for Snapcast whether enabled or not
    // (saving it applies the config and enables the backend).
    if (backend === 'snapcast') renderExternalConfig(card);
    if (info.enabled && info.connected) {
      loadZones(backend, card);
      if (backend === 'sendspin') renderPairingPanel(card);
    }
  }
}

async function toggleIntegration(backend, toggle, card) {
  const fail = card.querySelector('[data-role="failure"]');
  fail.style.display = 'none';
  toggle.disabled = true;
  const status = card.querySelector('[data-role="status"]');
  if (toggle.checked) status.textContent = 'Enabling…';
  try {
    const r = await fetch(`/admin/output/integrations/${backend}/toggle`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: toggle.checked }),
    });
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      const msg = (body.detail && body.detail.message) || 'Failed to change state';
      // Latch to "failed" with the toggle reflecting the true (unchanged) state,
      // never a silent revert.
      toggle.checked = !toggle.checked;
      fail.textContent = msg;
      fail.style.display = 'block';
      return;
    }
    await loadIntegrations();
  } catch (e) {
    toggle.checked = !toggle.checked;
    fail.textContent = 'Network error';
    fail.style.display = 'block';
  } finally {
    toggle.disabled = false;
  }
}

function zonePost(path, body) {
  return fetch('/admin/output/' + path, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
}

async function loadZones(backend, card) {
  const zonesEl = card.querySelector('[data-role="zones"]');
  zonesEl.innerHTML = '<span style="font-size:.8rem;color:#777">Loading zones…</span>';
  try {
    const data = await api('GET', `/admin/output/${backend}/zones`);
    renderZoneTree(backend, zonesEl, data.zones || [], !!data.can_manage_topology);
  } catch (e) {
    zonesEl.innerHTML = '<span style="font-size:.8rem;color:#a55">Zones unavailable</span>';
  }
}

function renderZoneTree(backend, zonesEl, zones, canManageTopology) {
  zonesEl.innerHTML = '';
  if (!zones.length) {
    zonesEl.innerHTML = '<span style="font-size:.8rem;color:#777">No clients connected.</span>';
    return;
  }
  const groupIds = zones.map(g => g.group_id);
  for (const group of zones) {
    const gwrap = document.createElement('div');
    gwrap.style.cssText = 'border-left:2px solid var(--border);padding-left:.6rem;margin-bottom:.5rem';
    gwrap.appendChild(renderGroupHeader(backend, group, canManageTopology));
    const clients = document.createElement('div');
    clients.style.cssText = 'max-height:12rem;overflow-y:auto';
    for (const client of group.clients || []) {
      clients.appendChild(renderZoneSlider(backend, group.group_id, client, groupIds));
    }
    gwrap.appendChild(clients);
    zonesEl.appendChild(gwrap);
  }
}

function renderGroupHeader(backend, group, canManageTopology) {
  const hdr = document.createElement('div');
  hdr.style.cssText = 'display:flex;align-items:center;gap:.4rem;margin:.35rem 0;font-size:.8rem;flex-wrap:wrap';
  const label = document.createElement('strong');
  label.textContent = group.name || 'Group';
  label.style.color = '#ddd';
  const gname = group.name || 'Group';
  // Group volume — the mean of members, applied as a proportional fan-out.
  const gvol = document.createElement('input');
  gvol.type = 'range'; gvol.min = '0'; gvol.max = '100';
  const members = group.clients || [];
  const mean = members.length
    ? members.reduce((a, c) => a + (c.volume || 0), 0) / members.length : 0;
  gvol.value = String(Math.round(mean * 100));
  gvol.style.setProperty('--vol-fill', gvol.value + '%');
  gvol.setAttribute('aria-label', gname + ' volume');
  gvol.addEventListener('input', () => {
    gvol.style.setProperty('--vol-fill', gvol.value + '%');
    const key = 'g:' + backend + ':' + group.group_id;
    clearTimeout(_zoneVolTimers[key]);
    _zoneVolTimers[key] = setTimeout(() => {
      zonePost(`${backend}/group/${encodeURIComponent(group.group_id)}/volume`,
               { level: Number(gvol.value) / 100 }).catch(() => {});
    }, 150);
  });
  const gmuteLbl = document.createElement('label');
  gmuteLbl.style.cssText = 'display:flex;align-items:center;gap:.2rem;color:#999';
  const gmute = document.createElement('input');
  gmute.type = 'checkbox'; gmute.checked = !!group.muted;
  gmute.setAttribute('aria-label', gname + ' mute');
  gmute.addEventListener('change', () =>
    zonePost(`${backend}/group/${encodeURIComponent(group.group_id)}/mute`,
             { muted: gmute.checked }).catch(() => {}));
  gmuteLbl.appendChild(gmute); gmuteLbl.appendChild(document.createTextNode('mute'));
  hdr.appendChild(label); hdr.appendChild(gvol); hdr.appendChild(gmuteLbl);
  // Topology controls — embedded Snapcast only (external is read + assign-only).
  if (canManageTopology && backend === 'snapcast') {
    const rename = document.createElement('button');
    rename.className = 'btn btn-sm'; rename.textContent = 'Rename';
    rename.style.cssText = 'background:transparent;color:var(--muted);border:1px solid var(--border)';
    rename.addEventListener('click', async () => {
      const name = prompt('New group name:', group.name || '');
      if (name == null) return;
      await zonePost(`snapcast/group/${encodeURIComponent(group.group_id)}/rename`, { name });
      showToast('Group renamed');
    });
    const dissolve = document.createElement('button');
    dissolve.className = 'btn btn-sm'; dissolve.textContent = 'Dissolve';
    dissolve.style.cssText = 'background:transparent;color:#c77;border:1px solid var(--border)';
    dissolve.addEventListener('click', async () => {
      if (!confirm('Dissolve this group? Its clients become ungrouped.')) return;
      await fetch(`/admin/output/snapcast/group/${encodeURIComponent(group.group_id)}`,
                  { method: 'DELETE' });
      showToast('Group dissolved');
    });
    hdr.appendChild(rename); hdr.appendChild(dissolve);
  }
  return hdr;
}

function renderZoneSlider(backend, groupId, client, groupIds) {
  const row = document.createElement('div');
  row.style.cssText = 'display:flex;align-items:center;gap:.5rem;margin:.25rem 0;flex-wrap:wrap';
  const name = document.createElement('span');
  name.textContent = client.name;
  name.style.cssText = 'font-size:.8rem;min-width:8rem;color:#ccc';
  const slider = document.createElement('input');
  slider.type = 'range';
  slider.min = '0'; slider.max = '100';
  slider.value = String(Math.round((client.volume || 0) * 100));
  // Reuse the master slider's --vol-fill visual treatment.
  slider.style.setProperty('--vol-fill', slider.value + '%');
  slider.setAttribute('aria-label', `${client.name} volume`);
  slider.addEventListener('input', () => {
    slider.style.setProperty('--vol-fill', slider.value + '%');
    const key = backend + ':' + client.client_id;
    clearTimeout(_zoneVolTimers[key]);
    _zoneVolTimers[key] = setTimeout(() => {
      zonePost(`${backend}/client/${encodeURIComponent(client.client_id)}/volume`,
               { level: Number(slider.value) / 100 }).catch(() => {});
    }, 150);
  });
  row.appendChild(name);
  row.appendChild(slider);
  // Per-client mute.
  const muteLbl = document.createElement('label');
  muteLbl.style.cssText = 'display:flex;align-items:center;gap:.2rem;font-size:.75rem;color:#999';
  const mute = document.createElement('input');
  mute.type = 'checkbox'; mute.checked = !!client.muted;
  mute.setAttribute('aria-label', `${client.name} mute`);
  mute.addEventListener('change', () =>
    zonePost(`${backend}/client/${encodeURIComponent(client.client_id)}/mute`,
             { muted: mute.checked }).catch(() => {}));
  muteLbl.appendChild(mute); muteLbl.appendChild(document.createTextNode('mute'));
  row.appendChild(muteLbl);
  // Per-room delay trim, shown when the backend reports one. Gated on the
  // capability rather than the backend name, so anything that grows a delay
  // readback later picks the control up without a UI change.
  if (client.delay_ms !== undefined && client.delay_ms !== null) {
    const delayLbl = document.createElement('label');
    delayLbl.style.cssText = 'display:flex;align-items:center;gap:.2rem;font-size:.75rem;color:#999';
    const delay = document.createElement('input');
    delay.type = 'number'; delay.min = '0'; delay.max = '2000'; delay.step = '10';
    delay.value = String(client.delay_ms || 0);
    delay.style.cssText = 'width:4.5rem;font-size:.75rem;padding:.15rem .3rem;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:4px';
    delay.setAttribute('aria-label', `${client.name} delay in milliseconds`);
    // Debounced like the sibling volume slider — a number input fires on every
    // arrow-key step, and each one is a network write to a speaker.
    delay.addEventListener('input', () => {
      const key = backend + ':delay:' + client.client_id;
      clearTimeout(_zoneVolTimers[key]);
      _zoneVolTimers[key] = setTimeout(() => {
        zonePost(`${backend}/client/${encodeURIComponent(client.client_id)}/delay`,
                 { delay_ms: Math.max(0, Number(delay.value) || 0) }).catch(() => {});
      }, 300);
    });
    delayLbl.appendChild(delay);
    delayLbl.appendChild(document.createTextNode('ms delay'));
    row.appendChild(delayLbl);
  }
  // Assign to another existing group (Snapcast only — non-destructive).
  if (backend === 'snapcast' && groupIds && groupIds.length > 1) {
    const sel = document.createElement('select');
    sel.setAttribute('aria-label', `${client.name} group`);
    sel.style.cssText = 'font-size:.75rem';
    for (const gid of groupIds) {
      const opt = document.createElement('option');
      opt.value = gid; opt.textContent = gid === groupId ? '(here)' : gid;
      if (gid === groupId) opt.selected = true;
      sel.appendChild(opt);
    }
    sel.addEventListener('change', async () => {
      await zonePost(`snapcast/group/${encodeURIComponent(sel.value)}/assign`,
                     { client_id: client.client_id });
      showToast('Client moved');
    });
    row.appendChild(sel);
  }
  return row;
}

function renderExternalConfig(card) {
  const el = card.querySelector('[data-role="config"]');
  if (!el) return;
  const inp = 'padding:.3rem;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:4px';
  el.innerHTML =
    `<details style="margin-top:.4rem"><summary style="cursor:pointer;font-size:.8rem;color:#aaa">Configure server…</summary>
       <div style="display:flex;flex-direction:column;gap:.35rem;margin-top:.4rem;font-size:.8rem;max-width:22rem">
         <label><input type="radio" name="snap-mode" value="embedded" checked> Embedded (built-in server)</label>
         <label><input type="radio" name="snap-mode" value="external"> External server</label>
         <input data-role="ext-host" placeholder="external host / IP" style="${inp}">
         <input data-role="ext-port" placeholder="control port (default 1705)" style="${inp}">
         <input data-role="ext-feed" placeholder="tcp feed url e.g. tcp://host:4953" style="${inp}">
         <button class="btn btn-primary btn-sm" data-role="save-config">Save + apply</button>
       </div></details>`;
  el.querySelector('[data-role="save-config"]').addEventListener('click', async () => {
    const mode = el.querySelector('input[name="snap-mode"]:checked').value;
    const body = {
      mode,
      host: el.querySelector('[data-role="ext-host"]').value || null,
      port: Number(el.querySelector('[data-role="ext-port"]').value) || 1705,
      feed_url: el.querySelector('[data-role="ext-feed"]').value || null,
    };
    try {
      const r = await zonePost('snapcast/connect', body);
      if (r.ok) { showToast('Snapcast configured'); loadIntegrations(); }
      else {
        const b = await r.json().catch(() => ({}));
        showToast((b.detail && (b.detail.message || b.detail)) || 'Config failed');
      }
    } catch (e) { showToast('Config failed'); }
  });
}

// Pairing runs the way the protocol defines it: you read a code off the speaker
// and enter it here. Jukeplox never displays a code for you to type into the
// speaker — the previous panel had that backwards. The method list and its
// rules come from the API rather than being duplicated here, so a browser and
// an API client cannot disagree about what pairing accepts.
let _pairingGen = 0;

async function renderPairingPanel(card) {
  const el = card.querySelector('[data-role="pairing"]');
  // Monotonic guard: pair/unpair each trigger a refresh, so two renders can be
  // in flight at once and an older response must not paint over a newer one.
  const gen = ++_pairingGen;
  let data;
  try {
    data = await api('GET', '/admin/output/sendspin/speakers');
  } catch (e) {
    if (gen !== _pairingGen) return;
    el.innerHTML = '<div style="font-size:.8rem;color:#e88">Could not load speakers</div>';
    return;
  }
  if (gen !== _pairingGen) return;   // superseded while fetching
  const unpaired = (data.discovered || []).filter(s => !s.paired);
  const paired = data.paired || [];

  const opts = unpaired.map(s =>
    `<option value="${esc(s.id)}">${esc(s.name)}</option>`).join('');
  const methods = data.methods || [];
  const methodOpts = methods.map(m =>
    `<option value="${esc(m.id)}" data-needs-target="${m.requires_client_id ? '1' : ''}"
      >${esc(m.label)}</option>`).join('');
  const codeHint = m => (methods.find(x => x.id === m) || {}).code_format || '';

  // The paired list is always visible while the backend is enabled. Pairing is
  // the ONLY authority boundary a Sendspin speaker has — a paired speaker can
  // skip and pause — so this list is the security surface, not a nicety.
  const pairedRows = paired.length
    ? paired.map(s =>
        `<div style="display:flex;align-items:center;justify-content:space-between;padding:.3rem 0;border-bottom:1px solid var(--divider)">
           <span>${esc(s.name)}</span>
           <button class="btn btn-sm" data-unpair="${esc(s.id)}"
             style="background:transparent;color:var(--muted);border:1px solid var(--border)">Unpair</button>
         </div>`).join('')
    : '<div style="font-size:.8rem;color:#999">No speakers paired yet</div>';

  el.innerHTML =
    `<div style="font-size:.7rem;letter-spacing:.05em;text-transform:uppercase;color:#999;margin-bottom:.3rem">Paired speakers</div>
     ${pairedRows}
     <div style="font-size:.7rem;letter-spacing:.05em;text-transform:uppercase;color:#999;margin:.7rem 0 .3rem">Pair a speaker</div>
     <div style="display:flex;flex-wrap:wrap;gap:.4rem;align-items:center">
       <select data-role="pair-method" style="font-size:.85rem">${methodOpts}</select>
       <select data-role="pair-target" style="font-size:.85rem">
         ${opts || '<option value="">No new speakers found</option>'}
       </select>
       <input data-role="pair-code" placeholder="Code from the speaker"
         style="font-size:.85rem;padding:.3rem .5rem;border:1px solid var(--border);border-radius:6px;background:var(--surface);color:var(--text)">
       <button class="btn btn-sm" data-role="pair-go">Pair</button>
     </div>
     <div data-role="pair-error" style="display:none;color:#e88;font-size:.8rem;margin-top:.4rem"></div>`;

  const methodSel = el.querySelector('[data-role="pair-method"]');
  const targetSel = el.querySelector('[data-role="pair-target"]');
  const codeEl = el.querySelector('[data-role="pair-code"]');
  const errEl = el.querySelector('[data-role="pair-error"]');
  // Whether a speaker must be chosen is the API's call, not a hardcoded rule
  // here — a pairing token, for instance, names its own speaker.
  const syncTarget = () => {
    const opt = methodSel.selectedOptions[0];
    targetSel.style.display = opt && opt.dataset.needsTarget ? '' : 'none';
    codeEl.placeholder = codeHint(methodSel.value) || 'Code from the speaker';
  };
  methodSel.addEventListener('change', syncTarget);
  syncTarget();

  const pairBtn = el.querySelector('[data-role="pair-go"]');
  pairBtn.addEventListener('click', async () => {
    if (pairBtn.disabled) return;        // no double-submit
    errEl.style.display = 'none';
    pairBtn.disabled = true;
    const body = {
      method: methodSel.value,
      code: codeEl.value.trim(),
      client_id: targetSel.style.display === 'none' ? '' : targetSel.value,
    };
    try {
      await api('POST', '/admin/output/sendspin/pair', body);
      // The server has REGISTERED the attempt; the handshake completes
      // asynchronously. Saying "paired" here would send the operator off to
      // pair again and cancel the attempt already running.
      showToast('Pairing started — watch the paired list');
      renderPairingPanel(card);
      loadZones('sendspin', card);       // a newly paired speaker gets controls
    } catch (e) {
      errEl.textContent = e.detail || 'Pairing failed';
      errEl.style.display = '';          // leave the code field usable for a retry
      pairBtn.disabled = false;
    }
  });

  el.querySelectorAll('[data-unpair]').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (btn.disabled) return;
      const id = btn.getAttribute('data-unpair');
      if (!confirm('Unpair this speaker? It stops playing immediately and loses control.')) return;
      btn.disabled = true;
      try {
        await api('POST', `/admin/output/sendspin/client/${encodeURIComponent(id)}/unpair`);
        showToast('Speaker unpaired');
        renderPairingPanel(card);
        loadZones('sendspin', card);
      } catch (e) {
        showToast('Unpair failed');
        btn.disabled = false;
      }
    });
  });
}

(async () => {
  const state = await refreshQueueState();
  if (state && state.current) {
    playbackHandle.applyNowPlaying({ ...state.current, is_playing: state.is_playing, is_paused: state.is_paused });
  }
  await playbackHandle.resume();   // authoritative position + np refresh
  loadDevices();
  loadSources();   // unified loader: sources + libraries -> one source-grouped list
  loadSettings();
  loadRuleEditors();
  loadVolume();
  loadIntegrations();
})();
