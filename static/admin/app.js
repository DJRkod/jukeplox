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
  if (!resp.ok) throw new Error(`${method} ${path} → ${resp.status}`);
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
    // Dirty-state guard (pattern-rules plan U4, review decision): the rule
    // editors mutate local state only until Save — leaving the Setup tab
    // with unsaved edits silently discards them, so ask first.
    if (editorsDirty && !confirm('Unsaved rule changes will be lost. Leave Setup?')) return;
    location.hash = btn.dataset.ptab;
  });
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
});

// Queue/history snapshot refetch — used on initial load, WS reconnect, and
// tab-refocus. WS gaps (locked phone, network blip) drop queue_changed
// events and the server sends no snapshot on reconnect; without a re-pull,
// the Skip Back button's disabled state and the queue list go stale until
// the next queue mutation. Mirrors the loadVolume() onopen-resync pattern.
async function refreshQueueState() {
  try {
    const state = await api('GET', '/admin/queue');
    playbackHandle.applyQueue(state.queue, state.history);
    setRecentPlaysData(state.history);
    syncLock(state.is_locked);
    _historyEmpty = !(state.history && state.history.length);
    _syncPrevEnabled();
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

// ── Volume ─────────────────────────────────────────────────────────────────

const volSlider = document.getElementById('volume-slider');
const volLabel = document.getElementById('vol-label');
let volTimer;
let _volUserLastSet = 0;
volSlider.addEventListener('input', () => {
  volLabel.textContent = `${Math.round(volSlider.value * 100)}%`;
  volSlider.style.setProperty('--vol-fill', `${volSlider.value * 100}%`);  // gradient fill (2026-06-18)
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
}

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
    else if (msg.type === 'closing_time') playbackHandle.applyClosingTime(msg);
    else if (msg.type === 'track_skipped') playbackHandle.showSkipped(msg);
    else if (msg.type === 'queue_changed') {
      playbackHandle.applyQueue(msg.queue, msg.history);
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
      protocols: [{ backend: 'direct', device_id: 'default', verified: true, checked_at: null }],
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

document.getElementById('btn-apply-output').addEventListener('click', async () => {
  const sel = document.getElementById('connection-select');
  const opt = sel.selectedOptions[0];
  if (!opt || opt.disabled) { showToast('Select a verified protocol'); return; }
  const backend_type = opt.dataset.backend;
  const device_id = opt.dataset.deviceId;
  const host = document.getElementById('device-select').value;
  const body = { backend_type, device_id };
  if (host && host !== '__direct__') body.host = host;
  try {
    await api('POST', '/admin/output/active', body);
    currentActive = { backend_type, device_id, host: body.host || null, via: backend_type };
    updateDeviceSelect();
    _syncAirPlayProtocolUI();
    showToast('Output updated');
  } catch (err) {
    // HTTP 409 from the server signals a stale-verdict failure on Apply
    // — re-fetch with a bust to refresh the cache and re-probe.
    if (err && err.status === 409) {
      showToast((err.detail || 'Protocol check is stale') + ' — rechecking');
      loadDevices(true);
    } else {
      showToast('Failed to update output');
    }
  }
});

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
  appearanceHandle = mountAppearance({ getHandle: () => browseHandle });
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
const SOURCE_TYPE_LABELS = { plex: 'Plex', jellyfin: 'Jellyfin', local: 'Local' };
let _currentSources = [];

function renderSourcesList(sources) {
  _currentSources = sources.slice();
  if (!sources.length) {
    sourcesList.innerHTML = '<p style="color:#555;font-size:.85rem">No sources connected yet.</p>';
    return;
  }
  sourcesList.innerHTML = '';
  // Secondary-button treatment for the inline controls (matches the dark chrome).
  const sbtn = 'class="btn btn-sm" style="background:var(--surface2);color:var(--text);border:1px solid var(--border);min-width:32px"';
  sources.forEach((src, i) => {
    const row = document.createElement('div');
    row.className = 'lib-item';
    row.style.justifyContent = 'space-between';
    const label = SOURCE_TYPE_LABELS[src.type] || src.type;
    const up = i > 0
      ? `<button ${sbtn} data-dir="up" data-id="${esc(src.source_id)}" title="Higher priority">▲</button>` : '';
    const down = i < sources.length - 1
      ? `<button ${sbtn} data-dir="down" data-id="${esc(src.source_id)}" title="Lower priority">▼</button>` : '';
    // Plex disconnect stays in the Plex flow; non-Plex sources (Jellyfin, local)
    // are removable here, routed to their own endpoint by src.type (U11/U14).
    const remove = src.type !== 'plex'
      ? `<button class="btn btn-sm" data-remove="${esc(src.source_id)}" style="background:var(--surface2);color:var(--danger);border:1px solid var(--border)">Remove</button>` : '';
    row.innerHTML =
      `<span><span style="color:#888;font-size:.75em;text-transform:uppercase;letter-spacing:.04em;margin-right:.5rem">${esc(label)}</span>${esc(src.name)}</span>` +
      `<span style="display:flex;gap:.35rem">${up}${down}${remove}</span>`;
    row.querySelectorAll('[data-dir]').forEach(b =>
      b.addEventListener('click', () => moveSourcePriority(b.dataset.id, b.dataset.dir)));
    const rm = row.querySelector('[data-remove]');
    if (rm) rm.addEventListener('click', () => removeSource(src.type, rm.dataset.remove));
    sourcesList.appendChild(row);
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
    renderSourcesList(sources);
  } catch {
    sourcesList.innerHTML = '<p style="color:#f87171;font-size:.85rem">Could not load sources.</p>';
  }
  renderSourceScanStatus();
}

// U15 admin scan-status badge: surfaces the catalog scan state under the Sources
// list — "Scanning…" while a crawl runs, and a distinct "no music found" when a
// finished scan returned nothing (vs the zero-source "No sources connected"
// which the sources list itself shows). Hidden when the library is populated.
async function renderSourceScanStatus() {
  const el = document.getElementById('sources-scan-status');
  if (!el) return;
  let s;
  try { s = await api('GET', '/admin/scan-status'); } catch { el.style.display = 'none'; return; }
  let msg = '';
  if (s.scanning) {
    msg = 'Scanning sources… the library will populate as it runs.';
  } else if (s.sources > 0 && s.scanned && s.empty) {
    msg = 'Scan complete, but no music was found — the connected sources returned nothing.';
  }
  el.textContent = msg;
  el.style.display = msg ? '' : 'none';
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
  try { await api('POST', '/admin/sources/rescan'); showToast('Rescanning all sources…'); }
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
  renderSourcesList(ids.map(id => byId[id]));  // optimistic reorder
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

// ── Libraries ──────────────────────────────────────────────────────────────

const libraryList = document.getElementById('library-list');

function renderLibraryList(libs) {
  if (!libs.length) { libraryList.innerHTML = '<p style="color:#555;font-size:.85rem">No Plex libraries found.</p>'; return; }
  libraryList.innerHTML = '';
  libs.forEach(lib => {
    const row = document.createElement('div');
    row.className = 'lib-item';
    const id = `lib-${lib.key}`;
    // Per-source-type indicator (ce-debug 2026-06-29): "(Plex — owner)" /
    // "(Jellyfin — server)" so same-named libraries across sources are
    // distinguishable. Disambiguator keeps the existing inner value (Plex owner
    // username), falling back to server_name (Jellyfin carries no owner).
    const typeLabels = { plex: 'Plex', jellyfin: 'Jellyfin', local: 'Local' };
    const tl = typeLabels[lib.source_type] || (lib.source_type
      ? lib.source_type.charAt(0).toUpperCase() + lib.source_type.slice(1) : '');
    const disambig = lib.owner || lib.server_name || '';
    const inner = [tl, disambig].filter(Boolean).join(' — ');
    const ownerTag = inner ? ` <span style="color:#666;font-size:.8em">(${esc(inner)})</span>` : '';
    const encodedKey = encodeURIComponent(lib.key);
    row.innerHTML = `<input type="checkbox" id="${id}" data-key="${encodedKey}" ${lib.enabled ? 'checked' : ''}><label for="${id}">${esc(lib.title)}${ownerTag}</label>`;
    row.querySelector('input').addEventListener('change', async function() {
      const path = this.checked ? `/admin/plex/libraries/${this.dataset.key}/enable` : `/admin/plex/libraries/${this.dataset.key}/disable`;
      try { await api('POST', path); }
      catch { showToast('Failed to update library'); this.checked = !this.checked; }
    });
    libraryList.appendChild(row);
  });
}

async function loadLibraries() {
  try {
    const libs = await api('GET', '/admin/plex/libraries');
    renderLibraryList(libs);
  } catch { libraryList.innerHTML = '<p style="color:#f87171;font-size:.85rem">Plex not connected.</p>'; }
  // Browse-index plan U6: surface index freshness next to the Rescan button.
  // Inlined (not a new top-level helper) to respect the per-page allowlist.
  try {
    const s = await api('GET', '/admin/plex/index-status');
    const el = document.getElementById('index-status');
    if (el) el.textContent = s.building ? 'Indexing…'
      : (s.computed_at ? 'Indexed ' + new Date(s.computed_at).toLocaleString() : 'Not yet indexed');
  } catch {}
}

async function adminRescan() {
  const btn = document.getElementById('btn-rescan-libs');
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = 'Rescanning…';
  try {
    const libs = await api('POST', '/admin/plex/rescan');
    renderLibraryList(libs);
    // Rescan just triggered a background browse-index rebuild (plan U6).
    const el = document.getElementById('index-status');
    if (el) el.textContent = 'Indexing…';
    showToast('Libraries refreshed');
  } catch { showToast('Rescan failed'); }
  finally { btn.disabled = false; btn.textContent = 'Rescan Libraries'; }
}

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
    ['genre', 'years', 'mostplayed', 'recentlyadded', 'highestrated'].forEach(f => {
      const el = document.getElementById('facet-' + f);
      if (el) el.checked = s['facet_' + f] !== false;
    });
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
  const surpriseEnabledEl = document.getElementById('surprise-enabled');
  const surpriseModeEl = document.getElementById('surprise-source-mode');
  const surpriseDivEl = document.getElementById('surprise-diversity');
  if (surpriseEnabledEl) body.surprise_me_enabled = surpriseEnabledEl.checked;
  if (surpriseModeEl) body.surprise_me_source_mode = surpriseModeEl.value;
  if (surpriseDivEl) body.surprise_me_diversity = surpriseDivEl.value;
  const lyricsContributeEl = document.getElementById('lyrics-contribute-enabled');
  if (lyricsContributeEl) body.lyrics_contribute_enabled = lyricsContributeEl.checked;
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
  // Track ratings + tags (2026-06-26 plan U9): two visibility flags + five facet
  // toggles, all booleans.
  const rvgEl = document.getElementById('ratings-visible-to-guests');
  if (rvgEl) body.ratings_visible_to_guests = rvgEl.checked;
  const tvgEl = document.getElementById('tags-visible-to-guests');
  if (tvgEl) body.tags_visible_to_guests = tvgEl.checked;
  ['genre', 'years', 'mostplayed', 'recentlyadded', 'highestrated'].forEach(f => {
    const el = document.getElementById('facet-' + f);
    if (el) body['facet_' + f] = el.checked;
  });
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
  const qeLimitEl = document.getElementById('queue-end-length-limit');
  if (qeLimitEl) body.queue_end_length_limit = qeLimitEl.checked;
  // International rail (plan 004): alpha-mode + the two thresholds (empty/0 →
  // default 2, mirroring the popular-threshold field).
  const alphaModeEl = document.querySelector('[name=rail-alpha-mode]:checked');
  if (alphaModeEl) body.rail_alpha_mode = alphaModeEl.value;
  const artistThreshEl = document.getElementById('rail-artist-threshold');
  if (artistThreshEl) body.rail_artist_threshold = parseInt(artistThreshEl.value, 10) || 2;
  const albumThreshEl = document.getElementById('rail-album-threshold');
  if (albumThreshEl) body.rail_album_threshold = parseInt(albumThreshEl.value, 10) || 2;
  try { await api('POST', '/admin/settings', body); showToast('Settings saved'); }
  catch { showToast('Failed to save settings'); }
});

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

(async () => {
  const state = await refreshQueueState();
  if (state && state.current) {
    playbackHandle.applyNowPlaying({ ...state.current, is_playing: state.is_playing, is_paused: state.is_paused });
  }
  await playbackHandle.resume();   // authoritative position + np refresh
  loadDevices();
  loadSources();
  loadLibraries();
  loadSettings();
  loadRuleEditors();
  loadVolume();
})();
