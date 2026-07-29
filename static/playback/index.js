// Jukeplox Shared Playback Module
//
// The ONLY home for Now Playing / progress / queue-list / history-strip /
// micro-bar rendering, shared by the guest page (/) and the admin page
// (/admin). Both mount it via mountPlayback(config); per-page behavior
// (seek, drag-reorder, item actions, tap-to-expand) is injected through
// config callbacks — the module renders structure, pages layer behavior.
// See tests/test_static_discipline.py for the authoritative rule and
// docs/solutions/design-patterns/guest-playback-dock-collapse-treatments.md
// for the design record (Option B).
//
// Timer discipline: this module is the single owner of the 1s tick and the
// 5s position-sync interval. Every start clears the prior timer; idle and
// suspend() stop both. The micro-bar is always-on — a leak here runs for
// the life of the page (see docs/solutions/best-practices/
// async-review-patterns-fastapi-frontend.md).

'use strict';

// Pure active-synced-line picker (2026-06-17 plan 008 U4): the index of the
// last line whose timestamp has passed `posMs`, or -1 before the first line.
// Assumes `lines` is sorted ascending by t_ms (parse_lrc guarantees this).
// Module-level + exposed on window so it can be unit-tested in a Node vm
// harness without a DOM (the JS-runtime-test-gap learning from this session) —
// the highlight escapes text-pattern tests, so the math gets a real test.
function lyricActiveIndex(lines, posMs) {
  if (!lines || !lines.length) return -1;
  let idx = -1;
  for (let i = 0; i < lines.length; i++) {
    if (lines[i].t_ms <= posMs) idx = i; else break;
  }
  return idx;
}
window.__jpLyricActiveIndex = lyricActiveIndex;

window.mountPlayback = function mountPlayback(config) {
  const cfg = config || {};

  // ── private helpers ───────────────────────────────────────────────────
  const _esc = (s) => (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const _el = (sel) => (sel ? document.querySelector(sel) : null);
  const _art = (path) => `/api/art?path=${encodeURIComponent(path)}`;
  const _toast = cfg.toast || (() => {});

  function _fmtMs(ms) {
    const s = Math.floor((ms || 0) / 1000);
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  }

  // ── region elements ───────────────────────────────────────────────────
  // Now Playing panel (full card — guest "Now" tab / admin Jukebox view)
  const np = cfg.nowPlaying || {};
  const npRoot = _el(np.el);
  // Progress (range input + times row) lives inside the NP panel
  const progressRoot = _el(np.progressEl);
  // Queue vertical list
  const q = cfg.queue || {};
  const queueRoot = _el(q.el);
  // History strip
  const h = cfg.history || {};
  const historyRoot = _el(h.el);
  // Guest micro-bar (null on admin)
  const mb = cfg.microBar || null;
  const mbRoot = mb ? _el(mb.el) : null;

  // ── playback state ────────────────────────────────────────────────────
  let _posMs = 0;
  let _durMs = 0;
  let _playing = false;   // effective: is_playing && !is_paused
  let _hasTrack = false;
  let _queueLen = 0;      // latest queue length — gates the idle "add music" nudge
  let _trackId = null;    // last applied track — position resets on change
  let _tickTimer = null;
  let _syncTimer = null;
  let _seeking = false;   // suppresses repaints while the user drags
  // ── lyrics state (2026-06-17 plan 008) ───────────────────────────────
  let _lyrics = null;         // last fetched result {available, instrumental, synced, plain}
  let _lyricsTrackId = null;  // the track_id _lyrics belongs to (race guard)
  let _lyricsExpanded = false; // user-toggled open state; persists across tracks
  let _activeLine = -1;       // U4: currently highlighted synced-line index

  // ── scaffold rendering (one-time DOM) ─────────────────────────────────

  if (npRoot) {
    npRoot.innerHTML = `
      <div class="np-row">
        <div class="np-placeholder">🎷</div>
        <img class="np-art hidden" alt="">
        <div class="np-text">
          <div class="np-title">Jukeplox</div>
          <div class="np-artist">Nothing playing</div>
        </div>
      </div>
      <div class="np-output-note" hidden></div>
      <div class="np-lyrics" hidden>
        <div class="np-lyrics-slot">
          <button type="button" class="np-lyrics-pill" aria-expanded="false" hidden>♪ Lyrics</button>
          <span class="np-lyrics-tag" hidden>♪ Instrumental</span>
          <a class="np-lyrics-contribute" target="_blank" rel="noopener noreferrer" hidden></a>
        </div>
        <div class="np-lyrics-panel" hidden>
          <div class="np-lyrics-head">
            <span class="np-lyrics-title">♪ Lyrics</span>
            <button type="button" class="np-lyrics-collapse" aria-label="Hide lyrics">✕</button>
          </div>
          <div class="np-lyrics-body"></div>
        </div>
      </div>
      <button type="button" class="np-nudge" hidden>Find something to play</button>`;
  }

  if (progressRoot) {
    progressRoot.innerHTML = `
      <input type="range" class="np-progress-bar" min="0" max="1000" value="0" step="1"
             ${np.onSeek ? '' : 'disabled'}>
      <div class="np-times"><span class="np-pos">0:00</span><span class="np-dur">0:00</span></div>`;
  }

  if (mbRoot) {
    mbRoot.innerHTML = `
      <div class="mb-art-placeholder">🎷</div>
      <img class="mb-art hidden" alt="">
      <div class="mb-text">
        <span class="mb-title">Jukeplox</span>
        <span class="mb-artist">Nothing playing</span>
      </div>
      <span class="mb-time">0:00</span>
      <div class="mb-track"></div>
      <div class="mb-progress" style="width:0%"></div>`;
    if (mb.onTap) mbRoot.addEventListener('click', mb.onTap);
  }

  const _npEls = npRoot ? {
    placeholder: npRoot.querySelector('.np-placeholder'),
    art: npRoot.querySelector('.np-art'),
    title: npRoot.querySelector('.np-title'),
    artist: npRoot.querySelector('.np-artist'),
    nudge: npRoot.querySelector('.np-nudge'),
    outputNote: npRoot.querySelector('.np-output-note'),
  } : null;

  // ── lyrics panel (2026-06-17 plan 008 U3/U4) ──────────────────────────
  // The quiet "♪ Lyrics" pill expands inline to a panel; instrumental tracks
  // show a static "♪ Instrumental" tag (no expand); a no-match track shows
  // nothing. Lives in the shared module so guest + admin + the desktop docked
  // pane all get it by construction (CLAUDE.md shared-UI standard). The fetch
  // is keyed on track_id only — the server resolves artist/title/album/duration
  // (no cache-poisoning vector) — and a race guard (track_id capture/compare)
  // prevents a slow fetch for track A painting over track B after a rapid skip.
  const _lyrEls = npRoot ? {
    root: npRoot.querySelector('.np-lyrics'),
    slot: npRoot.querySelector('.np-lyrics-slot'),
    pill: npRoot.querySelector('.np-lyrics-pill'),
    tag: npRoot.querySelector('.np-lyrics-tag'),
    contribute: npRoot.querySelector('.np-lyrics-contribute'),
    panel: npRoot.querySelector('.np-lyrics-panel'),
    body: npRoot.querySelector('.np-lyrics-body'),
  } : null;

  if (_lyrEls && _lyrEls.pill) {
    _lyrEls.pill.addEventListener('click', () => { _lyricsExpanded = true; _renderLyrics(); });
  }
  if (_lyrEls && _lyrEls.panel) {
    const collapse = _lyrEls.panel.querySelector('.np-lyrics-collapse');
    if (collapse) collapse.addEventListener('click', () => { _lyricsExpanded = false; _renderLyrics(); });
  }

  // Idle "add music" nudge (2026-06-17 plan 006 U2). Shown ONLY when there's no
  // current track AND the queue is empty AND the page wired cfg.onFindMusic —
  // guest does (→ Search), admin doesn't, so it never appears on admin (R3). The
  // scaffold is built once and the idle/playing paths only mutate textContent, so
  // visibility is toggled explicitly via _updateNudge() from _setIdle (show),
  // applyNowPlaying (hide), and applyQueue (re-evaluate on queue change) — "not
  // emitted on the playing branch" would otherwise leave a stale nudge under live
  // playback (AE4). Desktop hides it via CSS (queue.css @media >=960px).
  if (_npEls && _npEls.nudge && cfg.onFindMusic) {
    _npEls.nudge.addEventListener('click', () => cfg.onFindMusic());
  }
  function _updateNudge() {
    if (!_npEls || !_npEls.nudge) return;
    _npEls.nudge.hidden = !(cfg.onFindMusic && !_hasTrack && _queueLen === 0);
  }

  const _pEls = progressRoot ? {
    bar: progressRoot.querySelector('.np-progress-bar'),
    pos: progressRoot.querySelector('.np-pos'),
    dur: progressRoot.querySelector('.np-dur'),
  } : null;

  const _mbEls = mbRoot ? {
    placeholder: mbRoot.querySelector('.mb-art-placeholder'),
    art: mbRoot.querySelector('.mb-art'),
    title: mbRoot.querySelector('.mb-title'),
    artist: mbRoot.querySelector('.mb-artist'),
    time: mbRoot.querySelector('.mb-time'),
    fill: mbRoot.querySelector('.mb-progress'),
  } : null;

  // Publish the played fraction as a CSS var so the gradient progress fill
  // (a var(--accent-grad) track with a --np-fill-driven dim over the unplayed
  // remainder) tracks position on both the tick path and live drag. The bar's
  // native value is 0–1000, so percent = value / 10.
  const _syncFill = () => {
    if (_pEls) _pEls.bar.style.setProperty('--np-fill', (_pEls.bar.value / 10) + '%');
  };

  // ── seek (module-owned end to end; no external suppression contract) ──

  if (_pEls && np.onSeek) {
    _pEls.bar.addEventListener('mousedown', () => { _seeking = true; });
    _pEls.bar.addEventListener('touchstart', () => { _seeking = true; }, { passive: true });
    document.addEventListener('pointerup', () => { _seeking = false; });
    _pEls.bar.addEventListener('input', () => {
      _posMs = _durMs > 0 ? Math.round((_pEls.bar.value / 1000) * _durMs) : 0;
      _pEls.pos.textContent = _fmtMs(_posMs);
      _syncFill();
    });
    _pEls.bar.addEventListener('change', async () => {
      try { await np.onSeek(_posMs); }
      catch { _toast('Seek failed'); }
      finally { _seeking = false; }
    });
  }

  // ── progress rendering + timers ───────────────────────────────────────

  function _renderProgress() {
    if (_pEls) {
      if (!_seeking) {
        _pEls.bar.value = _durMs > 0 ? Math.round((_posMs / _durMs) * 1000) : 0;
        _pEls.pos.textContent = _fmtMs(_posMs);
        _syncFill();
      }
      _pEls.dur.textContent = _fmtMs(_durMs);
    }
    if (_mbEls) {
      _mbEls.time.textContent = _fmtMs(_posMs);
      _mbEls.fill.style.width = _durMs > 0 ? `${Math.min(100, (_posMs / _durMs) * 100)}%` : '0%';
    }
    _updateActiveLine();   // U4: ride the same repaint path as the progress bar
  }

  function _stopTick() { clearInterval(_tickTimer); _tickTimer = null; }
  function _stopSync() { clearInterval(_syncTimer); _syncTimer = null; }

  function _startTick() {
    _stopTick();
    if (!_playing) return;
    _tickTimer = setInterval(() => {
      if (_seeking) return;
      _posMs = Math.min(_posMs + 1000, _durMs || 1e9);
      _renderProgress();
    }, 1000);
  }

  function _startSync() {
    _stopSync();
    if (!_playing) return;
    _syncTimer = setInterval(async () => {
      try {
        const resp = await fetch('/api/playback/position');
        if (!resp.ok) return;
        const d = await resp.json();
        // Re-check both flags AFTER the await: clearInterval() cannot abort
        // an in-flight fetch, so a response landing after _setIdle() (track
        // ended mid-poll) must not repaint stale position onto the idle UI.
        if (_seeking || !_playing) return;
        _posMs = d.position_ms ?? _posMs;
        _durMs = d.duration_ms ?? _durMs;
        _renderProgress();
      } catch {}
    }, 5000);
  }

  function _applyPlayingFlag(playing) {
    _playing = !!playing;
    if (_playing) { _startTick(); _startSync(); }
    else { _stopTick(); _stopSync(); }
    if (cfg.onPlayState) cfg.onPlayState(_playing, _hasTrack);
  }

  // ── now playing ───────────────────────────────────────────────────────

  function _setIdle() {
    _hasTrack = false;
    _trackId = null;
    _posMs = 0; _durMs = 0;
    if (_npEls) {
      _npEls.placeholder.style.display = '';
      _npEls.art.classList.add('hidden');
      _npEls.title.textContent = 'Jukeplox';
      _npEls.artist.textContent = 'Nothing playing';
    }
    if (_mbEls) {
      _mbEls.placeholder.style.display = '';
      _mbEls.art.classList.add('hidden');
      _mbEls.title.textContent = 'Jukeplox';
      _mbEls.artist.textContent = 'Nothing playing';
    }
    _applyPlayingFlag(false);
    _renderProgress();
    _updateNudge();
    _resetLyrics(null);   // nothing playing → hide the lyric panel (no refetch)
  }

  // ── name links (2026-06-10 nav plan U5) ──────────────────────────────
  // Artist/album text becomes tappable when the page wires cfg.onNameTap
  // (payload {kind, name, albumId, surface}); without it the line renders
  // as plain text exactly as before. The sub line is built from spans —
  // it was a single combined textContent write, which can't carry two
  // independent hit zones (R2).

  function _setNameLine(el, parts, surface) {
    if (!cfg.onNameTap) {
      el.textContent = parts.map(p => p && p.text).filter(Boolean).join(' — ');
      return;
    }
    el.innerHTML = '';
    let first = true;
    parts.forEach(p => {
      if (!p || !p.text) return;
      if (!first) el.appendChild(document.createTextNode(' — '));
      first = false;
      const s = document.createElement('span');
      s.className = 'name-link';
      s.textContent = p.text;
      s.addEventListener('click', (e) => {
        e.stopPropagation();
        cfg.onNameTap({ kind: p.kind, name: p.text, albumId: p.albumId, surface });
      });
      el.appendChild(s);
    });
  }

  function applyNowPlaying(data) {
    if (!data || !data.title) { _setIdle(); return; }
    _hasTrack = true;
    _updateNudge();   // a track is current → hide the idle nudge (AE4)
    const subParts = [
      data.artist ? { kind: 'artist', text: data.artist } : null,
      data.album ? { kind: 'album', text: data.album, albumId: data.album_id } : null,
    ];
    if (_npEls) {
      _npEls.title.textContent = data.title;
      _setNameLine(_npEls.artist, subParts, 'now');
      if (data.thumb) {
        _npEls.art.src = _art(data.thumb);
        _npEls.art.classList.remove('hidden');
        _npEls.placeholder.style.display = 'none';
      } else {
        _npEls.art.classList.add('hidden');
        _npEls.placeholder.style.display = '';
      }
    }
    if (_mbEls) {
      _mbEls.title.textContent = data.title;
      _setNameLine(_mbEls.artist, [subParts[0]], 'micro');
      if (data.thumb) {
        _mbEls.art.src = _art(data.thumb);
        _mbEls.art.classList.remove('hidden');
        _mbEls.placeholder.style.display = 'none';
      } else {
        _mbEls.art.classList.add('hidden');
        _mbEls.placeholder.style.display = '';
      }
    }
    // Reset progress on track change; preserve across pause/resume events.
    // Track identity is the primary signal — duration equality alone misses
    // same-duration transitions (deterministic in REPEAT with one track,
    // where the same track replays and the bar would stick at the end).
    const id = data.track_id ?? null;
    if (id !== null && id !== _trackId) {
      _trackId = id;
      if (data.duration_ms != null) _durMs = data.duration_ms;
      _posMs = 0;
      _resetLyrics(id);   // track changed → clear + refetch lyrics (U3 race guard)
    } else if (data.duration_ms != null && data.duration_ms !== _durMs) {
      _durMs = data.duration_ms;
      _posMs = 0;
    }
    _applyPlayingFlag(data.is_playing && !data.is_paused);
    _renderProgress();
  }

  // playback_state_changed carries only the flags — freeze/start timers
  // without touching track metadata or resetting position.
  function applyPlaybackState(data) {
    if (!_hasTrack) return;
    _applyPlayingFlag(data && data.is_playing && !data.is_paused);
    _renderProgress();
  }

  // ── lyrics (2026-06-17 plan 008) ──────────────────────────────────────

  // Called from applyNowPlaying's track-change branch and _setIdle. Clears
  // any prior lyrics, repaints (hides the panel until the new result lands),
  // and — for a real track — fetches the new track's lyrics.
  function _resetLyrics(trackId) {
    _lyrics = null;
    _lyricsTrackId = null;
    _activeLine = -1;
    _renderLyrics();
    if (trackId) _fetchLyrics(trackId);
  }

  async function _fetchLyrics(trackId) {
    try {
      const resp = await fetch(`/api/lyrics?track_id=${encodeURIComponent(trackId)}`);
      // Race guard: on a rapid skip, track A's fetch can resolve after the view
      // switched to B. _playing/_seeking stay true across a track change, so
      // only a track_id compare (not those flags) catches the stale fetch.
      if (trackId !== _trackId) return;
      // Resolve EVERY outcome (incl. a network failure) to a result so the
      // reserved slot leaves the "searching" state — searching forever would
      // strand the slot, and an empty-but-reserved miss is the intended end
      // state (no reflow). The endpoint is fail-soft 200, so !ok is rare.
      const result = resp.ok ? await resp.json() : { available: false, instrumental: false, synced: null, plain: null };
      // Re-check AFTER the resp.json() await: that is a second suspension point,
      // and a rapid skip during the body parse could switch the view to B. Without
      // this, track A's result would paint its pill under track B until B's own
      // fetch resolves (code-review #5, 2026-06-18).
      if (trackId !== _trackId) return;
      _lyrics = result;
      _lyricsTrackId = trackId;
      _renderLyrics();
    } catch {
      if (trackId !== _trackId) return;
      _lyrics = { available: false, instrumental: false, synced: null, plain: null };
      _lyricsTrackId = trackId;
      _renderLyrics();
    }
  }

  // Reserved-slot model (2026-06-18): once a track is playing the lyric slot
  // holds a fixed height for the life of the track, so the now-view NEVER
  // reflows mid-song when lyrics resolve ~7s in. Within that reserved slot we
  // show exactly one of: nothing (searching / miss — empty but still reserved),
  // the "♪ Instrumental" tag, or the "♪ Lyrics" button. The expanded panel is
  // the one user-initiated exception that grows the layout (a tap, so expected):
  // expanding hides the reserved slot and shows the panel in its place.
  function _renderLyrics() {
    if (!_lyrEls || !_lyrEls.root) return;
    // The wrapper stays shown for the WHOLE life of the now-view — including when
    // nothing is playing (the slot is just empty). Reserving the space on idle too
    // means the layout never reflows on idle↔playing transitions, not just
    // mid-song. Only the inner button/tag/panel toggle by lyric outcome.
    _lyrEls.root.hidden = false;

    const r = _lyrics;
    const available = !!(r && r.available);
    const instrumental = available && !!r.instrumental;
    const hasLyrics = available && !r.instrumental;
    const expanded = hasLyrics && _lyricsExpanded;
    // Contribute prompt (2026-06-23): the server attaches r.contribute.url ONLY on
    // a confirmed no-match with the admin toggle on (available is false here, so it
    // never collides with the pill/tag). A quiet link in the otherwise-empty slot.
    const contribute = !hasLyrics && !instrumental && !!(r && r.contribute && r.contribute.url);

    // Expanded → hide the reserved slot, show the panel (user-initiated reflow).
    _lyrEls.root.classList.toggle('is-expanded', expanded);
    _lyrEls.slot.hidden = expanded;
    _lyrEls.panel.hidden = !expanded;

    // Within the collapsed reserved slot: button XOR instrumental tag XOR
    // contribute link XOR empty.
    _lyrEls.pill.hidden = expanded || !hasLyrics;
    _lyrEls.tag.hidden = !instrumental;
    if (hasLyrics) _lyrEls.pill.setAttribute('aria-expanded', String(expanded));
    if (_lyrEls.contribute) {
      _lyrEls.contribute.hidden = !contribute;
      if (contribute) {
        _lyrEls.contribute.href = r.contribute.url;
        _lyrEls.contribute.textContent = 'No lyrics found — contribute some?';
      }
    }

    if (expanded) {
      _renderLyricsBody();
      _updateActiveLine();                // U4: light the current line immediately
    }
  }

  // Build the panel body: synced → one element per line (highlightable in U4);
  // plain → a single scrollable text block (R4/AE2).
  function _renderLyricsBody() {
    if (!_lyrEls || !_lyrEls.body) return;
    const r = _lyrics;
    if (r && r.synced && r.synced.length) {
      _lyrEls.body.className = 'np-lyrics-body np-lyrics-synced';
      _lyrEls.body.innerHTML = '';
      r.synced.forEach((ln, i) => {
        const div = document.createElement('div');
        div.className = 'np-lyric-line';
        div.dataset.idx = i;
        div.textContent = ln.line || ' ';   // keep blank lines as spacing
        _lyrEls.body.appendChild(div);
      });
    } else {
      _lyrEls.body.className = 'np-lyrics-body np-lyrics-plain';
      _lyrEls.body.textContent = (r && r.plain) || '';
    }
    // A freshly-rendered body starts at the TOP (2026-06-21): a new track must not
    // inherit the previous track's bottom scroll, and setting scrollTop directly
    // also cancels any in-flight smooth-scroll from the prior track's last line.
    // Resetting _activeLine forces the next _updateActiveLine to re-center on the
    // current line IF one is already active (re-expand mid-song / late lyric load);
    // before the first synced timestamp there is no active line, so it stays at top.
    _lyrEls.body.scrollTop = 0;
    _activeLine = -1;
  }

  // U4: highlight + auto-scroll the active synced line from the shared position.
  // Driven by _renderProgress (the existing tick/sync/seek repaint path) — no
  // second clock. Gated on the rendered lyrics belonging to the current track
  // (the race guard) so a mid-fetch skip can't highlight against B's position.
  function _updateActiveLine() {
    if (!_lyrEls || !_lyrEls.body || !_lyricsExpanded) return;
    const r = _lyrics;
    if (!r || !r.synced || !r.synced.length || _lyricsTrackId !== _trackId) return;
    const idx = lyricActiveIndex(r.synced, _posMs);
    if (idx === _activeLine) return;      // only repaint/scroll on a line change
    _activeLine = idx;
    const lineEls = _lyrEls.body.querySelectorAll('.np-lyric-line');
    lineEls.forEach((el, i) => el.classList.toggle('active', i === idx));
    const active = idx >= 0 ? lineEls[idx] : null;
    if (active) {
      // Scroll only the panel body (not the page): center the active line.
      const body = _lyrEls.body;
      const top = active.offsetTop - body.clientHeight / 2 + active.clientHeight / 2;
      body.scrollTo({ top, behavior: 'smooth' });
    }
  }

  // ── queue (vertical list) ─────────────────────────────────────────────

  // Shared remove renderer (unify-queue-remove U2). Both pages supply a
  // removePlan(items) → { singles:[{idx, remove}], albums:[{idxs, remove,
  // rowRemovers?}] }. We render a ✕ chip on each single row and one full-height
  // bar over each album run (plus an optional per-row ✕ when rowRemovers is
  // given), wiring each control's click to the plan's page-specific remove().
  // The plan owns ownership, grouping, the API call and the toast; this owns
  // the DOM + the (shared) CSS classes. `rows` is a static snapshot so moving
  // album rows into groups left-to-right keeps later indices valid.
  function _ensureActions(row) {
    let a = row.querySelector('.qi-actions');
    if (!a) { a = document.createElement('div'); a.className = 'qi-actions'; row.appendChild(a); }
    return a;
  }
  function _removeChip(label, onClick) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'qi-remove';
    btn.setAttribute('aria-label', label);
    btn.textContent = '✕';
    btn.addEventListener('click', (e) => { e.stopPropagation(); onClick(); });
    return btn;
  }
  function _renderRemovePlan(root, items, planFn) {
    const plan = planFn(items);
    if (!plan) return;
    const rows = Array.from(root.children);
    (plan.singles || []).forEach(({ idx, remove }) => {
      const row = rows[idx];
      if (row && remove) _ensureActions(row).appendChild(_removeChip('Remove from queue', remove));
    });
    (plan.albums || []).forEach(({ idxs, remove, rowRemovers }) => {
      const runRows = (idxs || []).map((i) => rows[i]).filter(Boolean);
      if (!runRows.length) return;
      const group = document.createElement('div');
      group.className = 'album-group';
      runRows[0].parentNode.insertBefore(group, runRows[0]);
      runRows.forEach((r, k) => {
        r.classList.add('album');
        group.appendChild(r);
        if (rowRemovers && rowRemovers[k]) {
          _ensureActions(r).appendChild(_removeChip('Remove this track', rowRemovers[k]));
        }
      });
      if (remove) {
        const bar = document.createElement('button');
        bar.type = 'button';
        bar.className = 'album-remove';
        bar.setAttribute('aria-label', 'Remove the whole album');
        bar.textContent = '✕';
        bar.addEventListener('click', (e) => { e.stopPropagation(); remove(); });
        group.appendChild(bar);
      }
    });
  }

  function applyQueue(queue, history) {
    _queueLen = (queue || []).length;   // gate the idle nudge: empty queue only
    _updateNudge();
    if (queueRoot) {
      const items = queue || [];
      if (!items.length) {
        queueRoot.innerHTML = '<p class="pb-empty">Queue is empty</p>';
      } else {
        queueRoot.innerHTML = '';
        items.forEach((item, idx) => {
          const row = document.createElement('div');
          row.className = 'queue-item';
          row.dataset.idx = idx;
          row.innerHTML = `
            <span class="qi-pos">${idx + 1}</span>
            ${item.thumb
              ? `<img class="qi-art" src="${_art(item.thumb)}" alt="">`
              : '<div class="qi-art qi-art-placeholder">🎷</div>'}
            <div class="qi-info">
              <div class="qi-title">${_esc(item.title)}</div>
              <div class="qi-artist">${_esc(item.artist)}</div>
            </div>`;
          // Nav plan U5: the artist line is its own hit zone; the rest of
          // the row keeps its gestures (R2 — stopPropagation in the tap).
          if (cfg.onNameTap && item.artist) {
            const a = row.querySelector('.qi-artist');
            a.classList.add('name-link');
            a.addEventListener('click', (e) => {
              e.stopPropagation();
              cfg.onNameTap({ kind: 'artist', name: item.artist, surface: 'queue' });
            });
          }
          // Admin injects drag handle, action buttons, and DnD wiring here
          // (per-row); guest defers to decorateQueue below.
          if (q.decorateRow) q.decorateRow(row, item, idx);
          queueRoot.appendChild(row);
        });
        // Post-render hook over the fully-built list: lets a page decorate
        // across rows, not just within one (legacy guest seam, retained for
        // transition).
        if (q.decorateQueue) q.decorateQueue(queueRoot, items);
        // Shared remove renderer (unify-queue-remove U2): both pages pass a
        // removePlan(items) → {singles, albums}; the module renders the ✕ chip
        // + full-height album bar identically. Ownership/grouping/remove-action
        // live in the per-page plan; rendering + CSS live here.
        if (q.removePlan) _renderRemovePlan(queueRoot, items, q.removePlan);
      }
    }
    if (historyRoot) {
      const items = history || [];
      if (!items.length) {
        historyRoot.innerHTML = '<span class="pb-empty">No history yet</span>';
      } else {
        historyRoot.innerHTML = '';
        items.forEach(item => {
          const div = document.createElement('div');
          div.className = 'qs-item qs-history';
          div.innerHTML = (item.thumb
            ? `<img class="qs-art" src="${_art(item.thumb)}" alt="">`
            : '<div class="qs-art qs-art-placeholder">🎷</div>')
            + `<div class="qs-title">${_esc(item.title)}</div>`;
          historyRoot.appendChild(div);
        });
        // Admin curation (plan U6): the page supplies removePlan(items) → per-entry
        // remove(); the module renders the shared ✕ chip (always-visible via CSS).
        // Guest passes no removePlan → no ✕, so the affordance is admin-only.
        if (h.removePlan) {
          const plan = h.removePlan(items) || [];
          const rows = Array.from(historyRoot.children);
          plan.forEach(({ idx, remove }) => {
            const row = rows[idx];
            if (row && remove) _ensureActions(row).appendChild(_removeChip('Remove this play', remove));
          });
        }
      }
    }
  }

  // ── visibility lifecycle ──────────────────────────────────────────────
  // suspend() parks the timers while the tab is hidden; resume() refetches
  // authoritative state (track may have changed while hidden) and restarts.

  function suspend() { _stopTick(); _stopSync(); }

  async function resume() {
    try {
      const resp = await fetch('/api/now-playing');
      if (!resp.ok) {
        // Transient server error is UNKNOWN state, not idle — keep the last
        // known UI and restart timers if we believed we were playing, rather
        // than blanking live playback to "Nothing playing" on a 5xx blip.
        _applyPlayingFlag(_playing);
        return;
      }
      const data = await resp.json();
      // Closing Time (U5): render the banner from the snapshot so a client that
      // loads/reconnects DURING a freeze shows it (single-source — both pages get
      // it through this shared resume()). Runs in both the playing and idle
      // branches because the freeze clears `current`.
      applyClosingTime(data);
      // Output-session state (supervisor plan U4): render the outage note from
      // the snapshot so a client that missed the output_session delta (locked
      // phone, WS gap) converges on refetch — the WS/GET resync contract. Runs
      // in both branches because the hold clears `current`.
      if (data && data.output_session) applyOutputSession(data.output_session);
      if (data && data.title) {
        applyNowPlaying(data);
        const p = await fetch('/api/playback/position');
        if (p.ok) {
          const d = await p.json();
          _posMs = d.position_ms ?? _posMs;
          _durMs = d.duration_ms ?? _durMs;
          _renderProgress();
        }
        return;
      }
      _setIdle();   // authoritative: server says nothing is current
    } catch {
      // Network failure: state unknown — keep last known UI, restart timers
      // if playing so the display doesn't freeze until the next WS event.
      _applyPlayingFlag(_playing);
    }
  }

  // ── Surprise Me (2026-06-17 plan U5) — shared Now-dock button, placement C ──
  // Rendered once into the dock just above the queue list, on both guest and
  // admin (single-source). Visibility is gated on the public enabled flag; the
  // press posts the browser's own seed and lets the WS queue broadcast update
  // the list. Failures stay quiet for guests (per "never show no surprises") —
  // the source attribution + logs carry the signal for operators.
  // In-flight working state (2026-06-17 plan 004): show a distinct "working"
  // state for the whole request so a slow press doesn't read as hung. Uses the
  // .working class (full-strength chip + spinner), NOT disabled (which dims). A
  // re-entry guard prevents double-submit; a minimum visible duration avoids a
  // sub-perceptual flash on a fast resolve.
  let _surpriseBusy = false;
  const _SURPRISE_MIN_MS = 300;
  async function _doSurprise(btn) {
    if (_surpriseBusy) return;   // no double-submit while a press is in flight
    _surpriseBusy = true;
    const labelEl = btn ? btn.querySelector('.jp-surprise-label') : null;
    if (btn) { btn.classList.add('working'); btn.setAttribute('aria-busy', 'true'); }
    if (labelEl) labelEl.textContent = 'Finding a track…';
    const started = Date.now();
    try {
      const seed = (typeof getSurpriseSeed === 'function') ? getSurpriseSeed() : [];
      // Anti-repeat (plan 005): send the browser's recently-surprised ids so the
      // server won't re-suggest them (remove + re-press won't repeat).
      const exclude = (typeof getRecentSurprised === 'function') ? getRecentSurprised() : [];
      const resp = await fetch('/api/queue/surprise', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ picks: seed, exclude }),
      });
      if (resp.status === 423) { _toast('Queuing is paused by the host'); return; }
      if (resp.ok) {
        const data = await resp.json().catch(() => null);
        if (data && data.ok) {
          _toast('Surprise added to queue!');
          // Remember this pick so it won't be re-suggested on the next press —
          // recorded at queue time, independent of later removal (plan 005).
          if (data.entry && data.entry.track_id && typeof recordSurprised === 'function') {
            recordSurprised(data.entry.track_id);
          }
          // Hand the append receipt to the page so the surprise track gets a
          // remove (✕) in the queue, same as a manual add. cfg.onQueued saves
          // the receipt and refreshes (also closes the paint-before-save race).
          if (data.entry && cfg.onQueued) cfg.onQueued(data.entry);
        }
        // ok:false (no library content) → stay silent; never surface a failure.
      }
    } catch { /* network/server error → silent for guests */ }
    finally {
      const elapsed = Date.now() - started;
      if (elapsed < _SURPRISE_MIN_MS) {
        await new Promise((r) => setTimeout(r, _SURPRISE_MIN_MS - elapsed));
      }
      if (btn) { btn.classList.remove('working'); btn.removeAttribute('aria-busy'); }
      if (labelEl) labelEl.textContent = 'Surprise Me';
      _surpriseBusy = false;
    }
  }

  if (queueRoot && typeof createSurpriseButton === 'function') {
    (async () => {
      let enabled = true;
      try {
        const r = await fetch('/api/appearance');
        if (r.ok) { const a = await r.json(); enabled = a.surprise_me_enabled !== false; }
      } catch { /* default visible if the flag can't be read */ }
      if (document.querySelector('.jp-surprise-dock')) return;   // dedup only
      // Always create the button; hide it when disabled rather than skipping
      // creation, so an admin toggling surprise on/off mid-session is a pure
      // show/hide on a live appearance_changed event (code-review #6).
      const btn = createSurpriseButton({ onClick: _doSurprise });
      btn.hidden = !enabled;
      queueRoot.parentNode.insertBefore(btn, queueRoot);
    })();
  }

  // ── Closing Time banner (2026-06-24 plan U5) ──────────────────────────
  // Single-source so it appears identically on guest + admin: a fixed overlay
  // injected into <body> on first activation, shown while the closing freeze is
  // active and hidden on clear. Driven by the `closing_time` WS event and by the
  // closing_active/closing_message fields on the initial now-playing snapshot
  // (so a guest who joins after the freeze still sees it).
  let _closingEl = null;
  function _ensureClosingEl() {
    if (_closingEl) return _closingEl;
    // On a control surface (admin) the banner is NON-blocking: a click-through
    // bar with a Resume button, so the admin can still queue / skip / resume.
    // On guests it's the full-screen blocking send-off (cleared when the admin
    // acts). Same element + event path; the `is-control` modifier flips the CSS.
    const ctrl = !!cfg.controlSurface;
    const el = document.createElement('div');
    el.className = 'closing-time-overlay' + (ctrl ? ' is-control' : '');
    el.hidden = true;
    el.innerHTML = `
      <div class="closing-time-card" role="status" aria-live="polite">
        <div class="closing-time-emoji" aria-hidden="true">🌙</div>
        <div class="closing-time-msg"></div>
        <div class="closing-time-sub">— last call —</div>
        ${ctrl ? '<button type="button" class="closing-time-resume">Resume the night</button>' : ''}
      </div>`;
    if (ctrl) {
      const btn = el.querySelector('.closing-time-resume');
      if (btn) btn.addEventListener('click', () => { if (cfg.onClosingResume) cfg.onClosingResume(); });
    }
    document.body.appendChild(el);
    _closingEl = el;
    return el;
  }
  function applyClosingTime(data) {
    // Accepts both shapes: the WS `closing_time` event ({active, message}) and
    // the now-playing snapshot ({closing_active, closing_message}).
    const active = !!(data && (data.closing_active ?? data.active));
    const message = (data && (data.closing_message ?? data.message)) || '';
    const el = _ensureClosingEl();
    if (active) {
      el.querySelector('.closing-time-msg').textContent = message;
      el.hidden = false;
      document.body.classList.add('closing-time-on');
    } else {
      el.hidden = true;
      document.body.classList.remove('closing-time-on');
    }
  }

  // ── Output-session (outage) state (2026-07-11 supervisor plan U4) ─────
  // Single-source lean presentation for guest + admin: while a device-level
  // outage holds the queue, a quiet "Paused — output offline" note shows in
  // the Now Playing panel (the hold clears `current`, so the panel itself
  // reads idle). Driven by the `output_session` WS event and by the mirrored
  // `output_session` snapshot field on the now-playing GET (resume() below),
  // so a client that missed the delta converges on refetch — the same
  // event + snapshot-hydration pattern as the Closing Time banner. Accepts
  // ONE shape for both paths: {state, held, ...}. Admin-only banner DETAIL
  // (device/retry/resume button) is page chrome layered via cfg.onOutputSession.
  // Deliberately minimal pending the interaction-state mockups pass (the plan
  // defers that UX): a state note, no elaborate treatment.
  function applyOutputSession(data) {
    if (!data) return;
    const held = !!data.held;
    if (_npEls && _npEls.outputNote) {
      if (held) {
        _npEls.outputNote.textContent = data.state === 'reconnecting'
          ? 'Paused — reconnecting to the speaker…'
          : 'Paused — output offline';
        _npEls.outputNote.hidden = false;
      } else {
        _npEls.outputNote.hidden = true;
      }
    }
    if (cfg.onOutputSession) cfg.onOutputSession(data);
  }

  // ── Skip notification (plan U16 / R22) ───────────────────────────────
  // Single-source so it appears identically on guest + admin: a transient,
  // auto-dismissing toast shown when a queued track is skipped because every
  // holder failed to stream. Rapid successive skips REPLACE rather than stack
  // (one element, restarted timer) so a cascade of dead holders can't pile up.
  const _SKIP_NOTE_MS = 4500;
  let _skipEl = null;
  let _skipTimer = null;
  function _ensureSkipEl() {
    if (_skipEl) return _skipEl;
    const el = document.createElement('div');
    el.className = 'skip-note-overlay';
    el.hidden = true;
    el.innerHTML =
      '<div class="skip-note-card" role="status" aria-live="polite">' +
      '<span class="skip-note-icon" aria-hidden="true">⏭</span>' +
      '<span class="skip-note-msg"></span></div>';
    document.body.appendChild(el);
    _skipEl = el;
    return el;
  }
  function showSkipped(data) {
    const title = (data && data.track_title) || '';
    // Admins additionally see which sources were tried (diagnostic). The guest
    // broadcast omits sources_tried (null), so this degrades to title-only.
    const tried = data && Array.isArray(data.sources_tried) ? data.sources_tried : null;
    let msg = title ? ('Skipped “' + title + '” — unavailable') : 'Skipped an unavailable track';
    if (tried && tried.length) msg += ' (tried: ' + tried.join(', ') + ')';
    const el = _ensureSkipEl();
    el.querySelector('.skip-note-msg').textContent = msg;   // R6: inert text, never innerHTML
    el.hidden = false;
    // Reflow so the enter transition runs even when replacing a visible toast.
    void el.offsetWidth;
    el.classList.add('is-shown');
    if (_skipTimer) clearTimeout(_skipTimer);
    _skipTimer = setTimeout(() => {
      el.classList.remove('is-shown');
      el.hidden = true;
      _skipTimer = null;
    }, _SKIP_NOTE_MS);
  }

  // Initial paint: idle until the first payload arrives (the "Now" tab dot
  // and micro-bar must not claim playback before state is known).
  _setIdle();

  return { applyNowPlaying, applyPlaybackState, applyQueue, applyClosingTime,
           applyOutputSession, showSkipped, setIdle: _setIdle, suspend, resume };
};
