// Synthetic party: N headless guests behaving badly against the real guest UI.
//
// Every action goes through the DOM — real clicks, real handlers, real
// websocket updates — because the whole point is to exercise what a phone
// actually does, not what an API client does. Guests have NO transport
// controls (play/pause/skip/seek/volume are absent from the guest DOM
// entirely, not merely hidden), so transport abuse is driven separately.

import fs from 'node:fs';
import { launchBrowser, Page, teardown, sleep } from './cdp.mjs';

// No default. A soak points at a real deployment and the target must be stated
// explicitly — a baked-in address is both a footgun and, on this project, a
// leak of the validation rig's LAN address into a public repo.
const BASE = process.env.JP_BASE;
if (!BASE) {
  process.stderr.write('JP_BASE is required, e.g. JP_BASE=http://jukebox.local\n');
  process.exit(2);
}
const GUESTS = parseInt(process.env.JP_GUESTS || '4', 10);
const MINUTES = parseFloat(process.env.JP_MINUTES || '30');
const LOG = process.env.JP_LOG || 'party-actions.jsonl';

const endAt = Date.now() + MINUTES * 60_000;
const out = fs.createWriteStream(LOG, { flags: 'a' });
const rec = (o) => out.write(JSON.stringify({ t: Date.now(), ...o }) + '\n');
const rnd = (a) => a[Math.floor(Math.random() * a.length)];
const jitter = (lo, hi) => lo + Math.random() * (hi - lo);

const TERMS = ['a', 'e', 'the', 'love', 'you', 'man', 'day', 'night', 'time',
  'one', 'sun', 'black', 'blue', 'i', 'go', 'r', 'li', 'st', 'wa', 'mo'];

// ── page-side helpers, injected once per guest ───────────────────────────────
// Kept in the page so a click is a genuine user gesture on a live element.
const HELPERS = `
window.__jp = window.__jp || {
  vis(el) {
    if (!el) return false;
    const s = getComputedStyle(el);
    return s.display !== 'none' && s.visibility !== 'hidden' && el.offsetParent !== null;
  },
  tap(el) { if (!el) return false; el.click(); return true; },
  rows(sel) { return [...document.querySelectorAll(sel)].filter(r => window.__jp.vis(r)); },
  activeView() { const v = document.querySelector('.view.active'); return v ? v.id : null; },
  async waitFor(sel, ms) {
    const end = Date.now() + (ms || 6000);
    while (Date.now() < end) {
      if (window.__jp.rows(sel).length) return true;
      await new Promise(r => setTimeout(r, 150));
    }
    return false;
  },
  qlen() { return document.querySelectorAll('#queue-list .queue-item').length; },
  toast() { const t = document.querySelector('#toast'); return t ? t.textContent : ''; },
};
`;

async function guestLoop(idx) {
  const port = 9600 + idx;
  const b = await launchBrowser(idx, port);
  const p = await Page.attach(b.port);
  await p.goto(BASE + '/');
  await sleep(2500);
  await p.eval(HELPERS + 'return true;');
  rec({ guest: idx, act: 'joined' });

  const behaviours = [
    // ── the bread-and-butter: find something, add it ─────────────────────
    async function searchAndAdd() {
      const term = rnd(TERMS);
      await p.eval(`
        ${HELPERS}
        const tab = document.querySelector('#tabs .tab[data-view="search-view"]');
        window.__jp.tap(tab);
        const i = document.querySelector('#search-input');
        if (i) { i.value = ${JSON.stringify(term)}; i.dispatchEvent(new Event('input', {bubbles:true})); }
        return true;`);
      await sleep(600);                        // clear the 400ms debounce
      const r = await p.eval(`
        ${HELPERS}
        await window.__jp.waitFor('#search-results .list-item.track-row', 7000);
        const rows = window.__jp.rows('#search-results .list-item.track-row');
        if (!rows.length) return {added:false, rows:0};
        const row = rows[Math.floor(Math.random()*rows.length)];
        const id = row.dataset.trackId || null;
        window.__jp.tap(row);
        return {added:true, rows:rows.length, trackId:id};`);
      rec({ guest: idx, act: 'searchAndAdd', term, ...r });
    },

    // ── impatience: tap the same track three times fast ──────────────────
    // The UI is supposed to absorb this (a re-tap on an already-queued row is
    // a client-side no-op). If it does not, the queue gains duplicates.
    async function tripleTapAdd() {
      const term = rnd(TERMS);
      await p.eval(`
        ${HELPERS}
        window.__jp.tap(document.querySelector('#tabs .tab[data-view="search-view"]'));
        const i = document.querySelector('#search-input');
        if (i) { i.value = ${JSON.stringify(term)}; i.dispatchEvent(new Event('input', {bubbles:true})); }
        return true;`);
      await sleep(600);
      const r = await p.eval(`
        ${HELPERS}
        await window.__jp.waitFor('#search-results .list-item.track-row', 7000);
        const rows = window.__jp.rows('#search-results .list-item.track-row');
        if (!rows.length) return {taps:0};
        const row = rows[0];
        const before = window.__jp.qlen();
        row.click(); row.click(); row.click();
        return {taps:3, trackId: row.dataset.trackId||null, qBefore: before};`);
      await sleep(900);
      const after = await p.eval(`return document.querySelectorAll('#queue-list .queue-item').length;`);
      rec({ guest: idx, act: 'tripleTapAdd', term, ...r, qAfter: after });
    },

    // ── drill in like a real person browsing ─────────────────────────────
    async function drillArtistAlbum() {
      await p.eval(`${HELPERS} window.__jp.tap(document.querySelector('#tabs .tab[data-view="artists-view"]')); return true;`);
      const a = await p.eval(`
        ${HELPERS}
        await window.__jp.waitFor('#artists-list .list-item', 9000);
        const rows = window.__jp.rows('#artists-list .list-item');
        if (!rows.length) return {step:'artists', n:0};
        window.__jp.tap(rows[Math.floor(Math.random()*rows.length)]);
        return {step:'artists', n:rows.length};`);
      const b2 = await p.eval(`
        ${HELPERS}
        await window.__jp.waitFor('#artists-list .list-item', 8000);
        const rows = window.__jp.rows('#artists-list .list-item').filter(r => !r.classList.contains('all-songs-entry'));
        if (!rows.length) return {step:'albums', n:0};
        window.__jp.tap(rows[Math.floor(Math.random()*rows.length)]);
        return {step:'albums', n:rows.length};`);
      const c = await p.eval(`
        ${HELPERS}
        await window.__jp.waitFor('.list-item.track-row', 8000);
        const rows = window.__jp.rows('.list-item.track-row');
        if (!rows.length) return {added:false};
        window.__jp.tap(rows[Math.floor(Math.random()*rows.length)]);
        return {added:true, n:rows.length};`);
      rec({ guest: idx, act: 'drillArtistAlbum', a, b: b2, c });
    },

    // ── queue a whole album, which is the big blunt instrument ───────────
    async function queueWholeAlbum() {
      await p.eval(`${HELPERS} window.__jp.tap(document.querySelector('#tabs .tab[data-view="albums-view"]')); return true;`);
      const r = await p.eval(`
        ${HELPERS}
        await window.__jp.waitFor('#albums-list .kebab-btn, #albums-list .list-item, #albums-list .tile', 12000);
        const kebabs = window.__jp.rows('#albums-list .kebab-btn');
        if (!kebabs.length) return {opened:false};
        window.__jp.tap(kebabs[Math.floor(Math.random()*kebabs.length)]);
        return {opened:true, n:kebabs.length};`);
      await sleep(700);
      const q = await p.eval(`
        ${HELPERS}
        const btns = [...document.querySelectorAll('#queue-album-area button')];
        const rel = btns.find(b => /queue release/i.test(b.textContent));
        if (!rel) { const c = document.querySelector('#close-menu-btn'); if (c) c.click(); return {queued:false, btns: btns.map(b=>b.textContent.trim())}; }
        rel.click();
        return {queued:true};`);
      rec({ guest: idx, act: 'queueWholeAlbum', ...r, ...q });
      await sleep(600);
      await p.eval(`const c=document.querySelector('#close-menu-btn'); if(c&&getComputedStyle(c).display!=='none')c.click(); return true;`);
    },

    // ── take it back out again, sometimes twice ──────────────────────────
    async function removeOwn() {
      await p.eval(`${HELPERS} window.__jp.tap(document.querySelector('#tabs .tab[data-view="now-view"]')); return true;`);
      await sleep(800);
      const r = await p.eval(`
        ${HELPERS}
        const x = window.__jp.rows('#queue-list .qi-remove');
        const alb = window.__jp.rows('#queue-list .album-remove');
        if (x.length) { const b = x[Math.floor(Math.random()*x.length)]; b.click(); if (Math.random()<0.4) b.click(); return {removed:'single', n:x.length}; }
        if (alb.length) { alb[0].click(); return {removed:'album', n:alb.length}; }
        return {removed:'none'};`);
      rec({ guest: idx, act: 'removeOwn', ...r });
    },

    // ── the impatient shortcut ───────────────────────────────────────────
    async function surprise() {
      await p.eval(`${HELPERS} window.__jp.tap(document.querySelector('#tabs .tab[data-view="now-view"]')); return true;`);
      await sleep(600);
      const r = await p.eval(`
        ${HELPERS}
        const btn = document.querySelector('button.jp-surprise-dock')
                 || document.querySelector('.jp-surprise-dock button')
                 || document.querySelector('.jp-surprise-dock');
        if (!btn) return {fired:false};
        btn.click(); btn.click();            // double-tap: busy-lock should absorb
        return {fired:true};`);
      rec({ guest: idx, act: 'surprise', ...r });
    },

    // ── restless: hop tabs faster than anything can finish loading ───────
    async function tabHop() {
      const views = ['search-view', 'artists-view', 'albums-view', 'genres-view',
        'years-view', 'mostplayed-view', 'recentlyadded-view', 'now-view'];
      const seq = [rnd(views), rnd(views), rnd(views), rnd(views)];
      for (const v of seq) {
        await p.eval(`${HELPERS} const t=document.querySelector('#tabs .tab[data-view="${v}"]'); if(t&&window.__jp.vis(t)) t.click(); return true;`);
        await sleep(jitter(120, 400));       // far faster than a list can load
      }
      rec({ guest: idx, act: 'tabHop', seq });
    },

    // ── start something, then walk away from it ──────────────────────────
    async function searchThenAbandon() {
      const term = rnd(TERMS);
      await p.eval(`
        ${HELPERS}
        window.__jp.tap(document.querySelector('#tabs .tab[data-view="search-view"]'));
        const i = document.querySelector('#search-input');
        if (i) { i.value = ${JSON.stringify(term)}; i.dispatchEvent(new Event('input', {bubbles:true})); }
        return true;`);
      await sleep(jitter(80, 380));          // abandon DURING the debounce
      await p.eval(`${HELPERS} window.__jp.tap(document.querySelector('#tabs .tab[data-view="artists-view"]')); return true;`);
      rec({ guest: idx, act: 'searchThenAbandon', term });
    },

    // ── phone reload mid-flow ────────────────────────────────────────────
    async function reloadMidFlow() {
      await p.eval(`
        ${HELPERS}
        window.__jp.tap(document.querySelector('#tabs .tab[data-view="search-view"]'));
        const i = document.querySelector('#search-input');
        if (i) { i.value = 'the'; i.dispatchEvent(new Event('input', {bubbles:true})); }
        return true;`);
      await sleep(jitter(100, 500));
      await p.goto(BASE + '/');               // reload mid-request
      await sleep(2200);
      await p.eval(HELPERS + 'return true;');
      rec({ guest: idx, act: 'reloadMidFlow' });
    },

    // ── pocket, then a flurry ────────────────────────────────────────────
    async function idleThenBurst() {
      const idle = jitter(20000, 45000);
      rec({ guest: idx, act: 'idleStart', ms: Math.round(idle) });
      await sleep(idle);
      for (let i = 0; i < 4; i++) {
        await p.eval(`
          ${HELPERS}
          const tabs = window.__jp.rows('#tabs .tab');
          if (tabs.length) tabs[Math.floor(Math.random()*tabs.length)].click();
          return true;`);
        await sleep(jitter(150, 350));
      }
      rec({ guest: idx, act: 'idleBurstDone' });
    },

    // ── poke the things a guest is not supposed to be able to use ────────
    async function pokeForbidden() {
      const r = await p.eval(`
        ${HELPERS}
        window.__jp.tap(document.querySelector('#tabs .tab[data-view="now-view"]'));
        const bar = document.querySelector('.np-progress-bar');
        const out = {progressFound: !!bar, progressDisabled: bar ? bar.disabled : null, seekAttempted:false};
        if (bar) {
          bar.value = Math.floor(Math.random()*100);
          bar.dispatchEvent(new Event('input', {bubbles:true}));
          bar.dispatchEvent(new Event('change', {bubbles:true}));
          out.seekAttempted = true;
        }
        out.transportButtons = document.querySelectorAll('#btn-pause, #btn-skip, #vol-slider').length;
        return out;`);
      rec({ guest: idx, act: 'pokeForbidden', ...r });
    },
  ];

  // Weighted so adding music dominates, like a real party.
  const weighted = [
    ...Array(5).fill(behaviours[0]),   // searchAndAdd
    ...Array(2).fill(behaviours[1]),   // tripleTapAdd
    ...Array(3).fill(behaviours[2]),   // drillArtistAlbum
    ...Array(2).fill(behaviours[3]),   // queueWholeAlbum
    ...Array(3).fill(behaviours[4]),   // removeOwn
    ...Array(2).fill(behaviours[5]),   // surprise
    ...Array(3).fill(behaviours[6]),   // tabHop
    ...Array(2).fill(behaviours[7]),   // searchThenAbandon
    behaviours[8],                     // reloadMidFlow
    behaviours[9],                     // idleThenBurst
    behaviours[10],                    // pokeForbidden
  ];

  while (Date.now() < endAt) {
    const fn = rnd(weighted);
    try {
      await fn();
    } catch (e) {
      rec({ guest: idx, act: 'ERROR', behaviour: fn.name, err: String(e.message || e) });
      // A guest that breaks gets refreshed rather than dropping out of the party.
      try { await p.goto(BASE + '/'); await sleep(2000); await p.eval(HELPERS + 'return true;'); }
      catch { break; }
    }
    await sleep(jitter(700, 2600));
  }
  rec({ guest: idx, act: 'left' });
  p.close();
}

(async () => {
  rec({ act: 'partyStart', guests: GUESTS, minutes: MINUTES, base: BASE });
  // Stagger arrivals — a party fills up, it does not appear at once.
  const runners = [];
  for (let i = 0; i < GUESTS; i++) {
    runners.push((async () => {
      await sleep(i * jitter(3000, 7000));
      try { await guestLoop(i); }
      catch (e) { rec({ guest: i, act: 'FATAL', err: String(e.message || e) }); }
    })());
  }
  await Promise.all(runners);
  rec({ act: 'partyEnd' });
  out.end();
  await sleep(500);
  teardown('party complete');
  process.exit(0);          // the Chrome children would otherwise hold the loop
})();
