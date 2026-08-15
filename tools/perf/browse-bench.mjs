// Zero-dependency headless-Chrome perf driver for the browse load/jump stall.
//
//   node tools/perf/browse-bench.mjs [count]
//
// Launches one-shot headless Chrome (node v24 global WebSocket speaks CDP — no
// npm installs), loads tools/perf/browse-bench.html (the real browse module +
// synthetic catalog), runs the self-driving bench, prints a timing + long-task
// report, then kills Chrome and removes its temp profile. Repeatable: run it,
// apply a fix, run it again to confirm the improvement.

import { spawn, execFileSync, spawnSync } from 'node:child_process';
import { mkdtempSync, rmSync, existsSync, readFileSync } from 'node:fs';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { join, extname, normalize } from 'node:path';

const COUNT = parseInt(process.argv[2] || '20000', 10);
const HEADED = process.argv.includes('headed');   // real compositor (for paint/scroll costs)
const IMAGES = process.argv.includes('images');   // serve real art so <img> load+decode is measured
const REAL = process.argv.includes('real');       // drive a live instance instead of the synthetic harness
// No baked-in default: this repo mirrors publicly, so a private LAN address
// here is both a leak and a footgun. `real` requires an explicit URL argument.
const REAL_URL = process.argv.find((a) => /^https?:\/\//.test(a)) || '';
if (REAL && !REAL_URL) {
  console.error('`real` needs an explicit target URL, e.g. real http://jukebox.local/');
  process.exit(2);
}

// Injected into a LIVE jukeplox page (real DOM/data/art). Opens Albums, waits for
// the list, then taps far rail letters — capturing main-thread long-tasks AND
// /api/art resource (network) timing per window, so a jump stall is attributable
// to decode/layout (long-tasks) vs art fetch burst (resource) vs neither (raster).
const REAL_DRIVER = `(async () => {
  const R = { mode:'real', longtasks:[], art:[], phases:{}, taps:[], notes:[] };
  try { new PerformanceObserver(l=>{for(const e of l.getEntries())R.longtasks.push({s:+e.startTime.toFixed(0),d:+e.duration.toFixed(0)});}).observe({type:'longtask',buffered:true}); } catch(_){ R.notes.push('no longtask obs'); }
  try { new PerformanceObserver(l=>{for(const e of l.getEntries())if((e.name||'').includes('/api/art'))R.art.push({s:+e.startTime.toFixed(0),dur:+e.duration.toFixed(0)});}).observe({type:'resource',buffered:true}); } catch(_){ R.notes.push('no resource obs'); }
  const sleep=ms=>new Promise(r=>setTimeout(r,ms)), raf=()=>new Promise(r=>requestAnimationFrame(()=>r()));
  const waitFor=async(p,ms)=>{const t=performance.now();while(performance.now()-t<ms){if(p())return true;await raf();}return false;};
  const lt=(a,b)=>{const x=R.longtasks.filter(e=>e.s>=a-1&&e.s<=b+1);return{count:x.length,totalMs:+x.reduce((s,e)=>s+e.d,0).toFixed(0),maxMs:x.reduce((m,e)=>Math.max(m,e.d),0)};};
  const ar=(a,b)=>{const x=R.art.filter(e=>e.s>=a-1&&e.s<=b+1);return{count:x.length,totalMs:+x.reduce((s,e)=>s+e.dur,0).toFixed(0),maxMs:x.reduce((m,e)=>Math.max(m,e.dur),0)};};
  await waitFor(()=>document.querySelector('.tab[data-view="albums-view"]')||document.querySelector('.tab[data-view="artists-view"]'),10000);
  const tab=document.querySelector('.tab[data-view="albums-view"]')||document.querySelector('.tab[data-view="artists-view"]');
  if(!tab){R.notes.push('no browse tab found');return R;}
  const view=tab.getAttribute('data-view'), itemsId=view==='albums-view'?'albums-items':'artists-items';
  const t0=performance.now(); tab.click();
  const ok=await waitFor(()=>{const c=document.querySelector('#'+itemsId+' .alpha-items-column');return c&&c.children.length>50;},60000);
  await sleep(900); const t1=performance.now();
  R.phases.firstNavigate={view,wallMs:+(t1-t0).toFixed(0),complete:ok,lt:lt(t0,t1),art:ar(t0,t1)};
  await waitFor(()=>{const c=document.querySelector('#'+itemsId+' .alpha-items-column');const rr=document.querySelector('.alpha-rail');return c&&rr&&rr.children.length>0;},10000);
  const col=document.querySelector('#'+itemsId+' .alpha-items-column');
  const scroller=(c)=>{let n=c&&c.parentElement;while(n&&n!==document.body){const o=getComputedStyle(n).overflowY;if(o==='auto'||o==='scroll')return n;n=n.parentElement;}return document.scrollingElement;};
  const sc=scroller(col);
  const esc=(k)=>(window.CSS&&CSS.escape)?CSS.escape(k):k;
  const rail=document.querySelector('.alpha-rail.visible')||document.querySelector('.alpha-rail');
  if(rail&&rail.children.length&&col&&sc){
    const b=[].filter.call(rail.children,x=>x.getAttribute('aria-disabled')!=='true');
    // A/B: is the cold-reveal cost driven by album-art <img> or by cell render?
    // Each jump targets a DISTINCT cold bucket (jumped from the top), so every
    // measurement is a first-reveal. Second pass hides art (display:none stops
    // lazy load + removes img layout) and jumps to OTHER cold buckets.
    const jumpCold=async(ix,tag)=>{
      const btn=b[ix]; if(!btn) return {tag,err:'no btn'};
      const key=btn.dataset.bucket;
      sc.scrollTop=0; await sleep(300);
      const t=col.querySelector('[data-bucket-start="'+esc(key)+'"]');
      if(!t) return {key,tag,err:'no target'};
      const r0=performance.now(); sc.scrollTop=t.offsetTop-(col.offsetTop||0); await sleep(600);
      return {key,tag,lt:lt(r0,performance.now()),art:ar(r0,performance.now())};
    };
    R.artOn=[]; R.artOff=[];
    for(const f of [0.4,0.7]) R.artOn.push(await jumpCold((b.length*f)|0,'art-on'));
    const st=document.createElement('style'); st.textContent='img.list-art{display:none!important}'; document.head.appendChild(st);
    await sleep(200);
    for(const f of [0.55,0.9]) R.artOff.push(await jumpCold((b.length*f)|0,'art-off'));
  } else R.notes.push('no rail/column/scroller');
  R.longtaskTotal=lt(0,performance.now()); R.artTotal=ar(0,performance.now());
  return R;
})()`;

// A real ~180x180 24-bit BMP, generated zero-dep. Served for every /api/art
// request; each row's unique art URL forces its own decode (as in production),
// so jump-into-view image decode cost is measured faithfully.
function makeBMP(w, h) {
  const rowSize = Math.floor((24 * w + 31) / 32) * 4;
  const dataSize = rowSize * h;
  const buf = Buffer.alloc(54 + dataSize);
  buf.write('BM', 0); buf.writeUInt32LE(54 + dataSize, 2); buf.writeUInt32LE(54, 10);
  buf.writeUInt32LE(40, 14); buf.writeInt32LE(w, 18); buf.writeInt32LE(h, 22);
  buf.writeUInt16LE(1, 26); buf.writeUInt16LE(24, 28); buf.writeUInt32LE(dataSize, 34);
  let o = 54;
  for (let y = 0; y < h; y++) { for (let x = 0; x < w; x++) { buf[o++] = (x * 2) & 255; buf[o++] = (y * 2) & 255; buf[o++] = (x + y) & 255; } o += rowSize - w * 3; }
  return buf;
}
const ART_BMP = makeBMP(180, 180);
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css', '.json': 'application/json' };
const CHROME_CANDIDATES = [
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
];
const CHROME = CHROME_CANDIDATES.find((p) => existsSync(p));
if (!CHROME) { console.error('No Chrome/Edge binary found.'); process.exit(2); }

const ROOT = process.cwd();
const harnessPath = join(ROOT, 'tools', 'perf', 'browse-bench.html');
if (!existsSync(harnessPath)) { console.error('Harness not found: ' + harnessPath); process.exit(2); }
const PORT = 9300 + Math.floor(Math.random() * 300);
const profileDir = mkdtempSync(join(tmpdir(), 'jp-bench-'));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// images mode needs a real origin so <img src="/api/art?…"> loads; file:// can't.
// A tiny static server over the repo root + an /api/art bitmap responder.
let server = null;
let HARNESS_URL = 'file:///' + harnessPath.replace(/\\/g, '/');
if (REAL) {
  HARNESS_URL = REAL_URL;            // drive the live instance directly
} else if (IMAGES) {
  const SRVPORT = 9700 + Math.floor(Math.random() * 200);
  server = createServer((req, res) => {
    const url = decodeURIComponent((req.url || '/').split('?')[0]);
    if (url.startsWith('/api/art')) { res.writeHead(200, { 'content-type': 'image/bmp', 'cache-control': 'no-store' }); res.end(ART_BMP); return; }
    const rel = normalize(url).replace(/^(\.\.[/\\])+/, '').replace(/^[/\\]+/, '');
    const file = join(ROOT, rel);
    if (!file.startsWith(ROOT) || !existsSync(file)) { res.writeHead(404); res.end('nf'); return; }
    res.writeHead(200, { 'content-type': MIME[extname(file)] || 'application/octet-stream' });
    res.end(readFileSync(file));
  }).listen(SRVPORT, '127.0.0.1');
  HARNESS_URL = `http://127.0.0.1:${SRVPORT}/tools/perf/browse-bench.html`;
}

const chrome = spawn(CHROME, [
  ...(HEADED ? [] : ['--headless=new', '--disable-gpu']),
  '--no-first-run', '--no-default-browser-check',
  '--disable-extensions', '--disable-background-timer-throttling',
  '--disable-renderer-backgrounding', '--disable-backgrounding-occluded-windows',
  '--disable-features=CalculateNativeWinOcclusion', '--window-size=1400,900',
  '--remote-allow-origins=*', '--user-data-dir=' + profileDir,
  '--remote-debugging-port=' + PORT, HARNESS_URL,
], { stdio: 'ignore' });

// Kill the WHOLE Chrome process tree, not just the spawned root. On Windows
// child.kill() only TerminateProcess()es the launcher PID, leaving the browser +
// renderer/gpu/crashpad/utility children orphaned — that leaked 19 trees (140
// chrome.exe) on 2026-06-24. taskkill /T /F kills the tree by PID; a sweep keyed
// on this run's unique profile tag is the bulletproof catch-all if Chrome
// relaunched under a PID we don't hold. See CLAUDE.md "Process Hygiene".
const PROFILE_TAG = profileDir.split(/[\\/]/).pop();   // e.g. jp-bench-AbC123 — unique per run
const syncSleep = (ms) => { try { Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms); } catch (_) {} };

function killChromeTree() {
  if (process.platform === 'win32') {
    if (chrome && chrome.pid) {
      try { execFileSync('taskkill', ['/PID', String(chrome.pid), '/T', '/F'], { stdio: 'ignore' }); } catch (_) {}
    }
    // Catch-all: kill any chrome/edge whose command line carries OUR profile tag.
    try {
      execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command',
        `Get-CimInstance Win32_Process -Filter "Name='chrome.exe' OR Name='msedge.exe'" | ` +
        `Where-Object { $_.CommandLine -like '*${PROFILE_TAG}*' } | ` +
        `ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }`,
      ], { stdio: 'ignore' });
    } catch (_) {}
  } else {
    try { chrome.kill('SIGKILL'); } catch (_) {}
    try { spawnSync('pkill', ['-9', '-f', PROFILE_TAG], { stdio: 'ignore' }); } catch (_) {}
  }
}

let cleanedUp = false;
function cleanup() {
  if (cleanedUp) return;          // idempotent: explicit calls + the 'exit' hook
  cleanedUp = true;
  killChromeTree();
  try { if (server) server.close(); } catch (_) {}
  // Chrome may briefly hold profile-dir locks right after the kill; retry the rm.
  for (let i = 0; i < 4; i++) {
    try { rmSync(profileDir, { recursive: true, force: true }); break; } catch (_) { syncSleep(150); }
  }
}
process.on('exit', cleanup);
const SIGCODE = { SIGINT: 130, SIGTERM: 143, SIGHUP: 129 };
for (const sig of ['SIGINT', 'SIGTERM', 'SIGHUP']) {
  process.on(sig, () => { cleanup(); process.exit(SIGCODE[sig]); });
}

async function getPageTarget() {
  for (let i = 0; i < 100; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${PORT}/json/list`);
      const list = await r.json();
      const t = list.find((x) => x.type === 'page' && x.webSocketDebuggerUrl);
      if (t) return t;
    } catch (_) { /* not up yet */ }
    await sleep(100);
  }
  throw new Error('Chrome DevTools page target never appeared');
}

function connect(wsUrl) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    let id = 0;
    const pending = new Map();
    const console_ = [];
    ws.onopen = () => resolve({ ws, send, console_ });
    ws.onerror = (e) => reject(new Error('ws error: ' + (e && e.message)));
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.id && pending.has(m.id)) {
        const { res, rej } = pending.get(m.id); pending.delete(m.id);
        m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result);
      } else if (m.method === 'Runtime.consoleAPICalled') {
        const txt = (m.params.args || []).map((a) => (a.value !== undefined ? a.value : (a.description || ''))).join(' ');
        console_.push(txt);
      } else if (m.method === 'Runtime.exceptionThrown') {
        const d = m.params.exceptionDetails || {};
        console_.push('[EXCEPTION] ' + ((d.exception && d.exception.description) || d.text || JSON.stringify(d)));
      }
    };
    function send(method, params = {}) {
      return new Promise((res, rej) => { const mid = ++id; pending.set(mid, { res, rej }); ws.send(JSON.stringify({ id: mid, method, params })); });
    }
  });
}

function fmt(n) { return (typeof n === 'number' ? n.toFixed(1) : String(n)) + 'ms'; }

function report(r, consoleLines) {
  const line = (s) => process.stdout.write(s + '\n');
  line('');
  line('═══ browse perf bench  (count=' + r.count + ', engine=' + CHROME.split('\\').pop() + ') ═══');
  if (r.rafProbe) line('rafProbe: ticks=' + r.rafProbe.ticks + ' in ' + fmt(r.rafProbe.ms) + ', visibility=' + r.rafProbe.visibility);
  if (r.error) line('HARNESS ERROR: ' + r.error);
  if (r.errors && r.errors.length) r.errors.forEach((e) => line('  JS ' + e));
  Object.entries(r.phases || {}).forEach(([k, p]) => { if (p.diag) line('  diag[' + k + ']: ' + JSON.stringify(p.diag)); });
  if (r.notes && r.notes.length) line('notes: ' + r.notes.join('; '));
  const ph = (name, label) => {
    const p = r.phases && r.phases[name];
    if (!p) { line('  ' + label.padEnd(26) + ' (missing)'); return; }
    const lt = p.lt || {};
    line('  ' + label.padEnd(26) + ' wall=' + fmt(p.wallMs).padEnd(10)
      + ' longtasks=' + (lt.count ?? '?') + ' (' + fmt(lt.totalMs || 0) + ', max ' + fmt(lt.maxMs || 0) + ')'
      + (p.complete === false ? '  [INCOMPLETE]' : ''));
  };
  line('── first navigate (the cold-open stall) ──');
  ph('artists_first_navigate', 'Artists');
  ph('albums_first_navigate', 'Albums');
  line('── jumping through the library ──');
  (r.taps || []).forEach((t, i) => {
    const lt = t.lt || {};
    line('  tap → ' + String(t.key).padEnd(6) + ' sync=' + fmt(t.syncMs).padEnd(9)
      + ' longtasks=' + lt.count + ' (' + fmt(lt.totalMs || 0) + ', max ' + fmt(lt.maxMs || 0) + ')');
  });
  if (r.drag) { const lt = r.drag.lt || {}; line('  drag-scrub'.padEnd(15) + ' wall=' + fmt(r.drag.wallMs).padEnd(9) + ' longtasks=' + lt.count + ' (' + fmt(lt.totalMs || 0) + ', max ' + fmt(lt.maxMs || 0) + ')'); }
  if (r.longtaskTotal) line('── total long-task time across run: ' + fmt(r.longtaskTotal.totalMs) + ' over ' + r.longtaskTotal.count + ' tasks ──');
  const prof = (r.prof || []);
  if (prof.length) { line('── in-module [browse-prof] timings ──'); prof.forEach((s) => line('  ' + s.replace('[browse-prof] ', ''))); }
  const exc = (consoleLines || []).filter((s) => s.indexOf('[EXCEPTION]') === 0);
  if (exc.length) { line('── page exceptions ──'); exc.forEach((s) => line('  ' + s)); }
  line('');
}

function reportReal(r, consoleLines) {
  const line = (s) => process.stdout.write(s + '\n');
  const win = (o) => o ? ('longtasks=' + o.lt.count + ' (' + fmt(o.lt.totalMs) + ', max ' + fmt(o.lt.maxMs) + ')   /api/art=' + o.art.count + ' (' + fmt(o.art.totalMs) + ', max ' + fmt(o.art.maxMs) + ')') : '(none)';
  line('');
  line('═══ browse perf bench — LIVE instance (' + REAL_URL + ') ═══');
  if (!r) { line('no result'); return; }
  if (r.notes && r.notes.length) line('notes: ' + r.notes.join('; '));
  const fn = r.phases && r.phases.firstNavigate;
  if (fn) line('first navigate (' + fn.view + ', wall=' + fmt(fn.wallMs) + (fn.complete ? '' : ' INCOMPLETE') + '): ' + win(fn));
  const jrow = (t) => t.err ? ('  jump → ' + String(t.key || '?').padEnd(14) + ' ' + t.err)
    : ('  jump → ' + String(t.key).padEnd(14) + ' ' + win(t));
  line('── cold deep jumps: ART ON ──');
  (r.artOn || []).forEach((t) => line(jrow(t)));
  line('── cold deep jumps: ART HIDDEN (display:none) ──');
  (r.artOff || []).forEach((t) => line(jrow(t)));
  if (r.longtaskTotal) line('totals: longtasks ' + fmt(r.longtaskTotal.totalMs) + '/' + r.longtaskTotal.count + ', /api/art ' + fmt(r.artTotal.totalMs) + '/' + r.artTotal.count);
  const exc = (consoleLines || []).filter((s) => s.indexOf('[EXCEPTION]') === 0);
  if (exc.length) { line('── page exceptions ──'); exc.forEach((s) => line('  ' + s)); }
  line('');
}

(async () => {
  const watchdog = setTimeout(() => { console.error('Timed out.'); cleanup(); process.exit(1); }, 120000);
  try {
    const target = await getPageTarget();
    const { send, console_ } = await connect(target.webSocketDebuggerUrl);
    await send('Runtime.enable');
    await send('Page.enable');
    if (!REAL) {
      let ready = false;
      for (let i = 0; i < 200 && !ready; i++) {
        const r = await send('Runtime.evaluate', { expression: '!!(window.__bench && window.__bench.run)', returnByValue: true });
        ready = !!r.result.value;
        if (!ready) await sleep(100);
      }
      if (!ready) throw new Error('harness window.__bench.run never became ready');
    }
    let result;
    if (REAL) {
      for (let i = 0; i < 200; i++) { const rs = await send('Runtime.evaluate', { expression: 'document.readyState', returnByValue: true }); if (rs.result.value === 'complete') break; await sleep(100); }
      await sleep(600);
      const r = await send('Runtime.evaluate', { expression: REAL_DRIVER, awaitPromise: true, returnByValue: true });
      result = r.result.value;
      reportReal(result, console_);
    } else {
      const r = await send('Runtime.evaluate', { expression: `window.__bench.run({count:${COUNT}, withImages:${IMAGES}})`, awaitPromise: true, returnByValue: true });
      report(r.result.value, console_);
    }
    clearTimeout(watchdog);
    cleanup();
    process.exit(0);
  } catch (e) {
    clearTimeout(watchdog);
    console.error('bench failed: ' + ((e && e.stack) || e));
    cleanup();
    process.exit(1);
  }
})();
