// Chrome-over-CDP driver for the jukeplox party soak.
//
// Node has a global WebSocket, so this needs no dependencies — which matters,
// because the harness has to be trivially runnable and, above all, trivially
// KILLABLE. Orphaned headless Chrome is a recurring problem on this project
// (~120 processes on 2026-06-10, 140 on 2026-06-24), so every spawn is tracked
// and teardown runs on normal exit, on error, and on Ctrl-C.
//
// Teardown follows tools/perf/browse-bench.mjs, which is the reference
// implementation for this project's process-hygiene standard. Two details in it
// are load-bearing and were got wrong in this harness's first draft:
//
//   1. Kill the process TREE. On Windows, killing the spawned PID only
//      TerminateProcess()es the launcher, leaving the browser, renderer, gpu,
//      crashpad and utility children orphaned. `taskkill /T /F` takes the tree.
//   2. Match by command-line signature, never by process name ALONE. The
//      user's own Chrome can be ~30 processes and must never be touched, so
//      every sweep is keyed on this run's unique profile tag AND narrowed to
//      browser images — the same belt-and-braces pair browse-bench.mjs uses.

import { spawn, execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

// Unique per run, so a sweep can never match another run's browsers — let alone
// the user's own.
const RUN_TAG = `jp-soak-${process.pid}-${Date.now().toString(36)}`;
const PROFILE_ROOT = path.join(os.tmpdir(), RUN_TAG);

const CHROME = process.env.JP_CHROME || defaultChrome();

function defaultChrome() {
  const candidates = process.platform === 'win32'
    ? ['C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
       'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe']
    : ['/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser',
       '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'];
  for (const c of candidates) {
    try { if (fs.existsSync(c)) return c; } catch { /* keep looking */ }
  }
  return candidates[0];   // let the spawn fail with a useful path in the error
}

const spawned = [];
let tornDown = false;

const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const syncSleep = (ms) => {
  try { Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms); } catch { /* best effort */ }
};

/** Count processes still carrying this run's signature.
 *
 * Deliberately its own function so the teardown can VERIFY rather than assume.
 * The first draft of this harness verified with `wmic | grep`, which returns
 * nothing on a UTF-16 stream — it reported zero leftovers every time, including
 * when there were dozens. Get-CimInstance returns objects, so the count is real.
 */
export function strayCount() {
  if (process.platform !== 'win32') {
    try {
      const out = execFileSync('pgrep', ['-fc', RUN_TAG], { encoding: 'utf8' });
      return parseInt(out.trim(), 10) || 0;
    } catch { return 0; }
  }
  try {
    const out = execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command',
      `@(Get-CimInstance Win32_Process -Filter "Name='chrome.exe' OR Name='msedge.exe'" | ` +
      `Where-Object { $_.CommandLine -like '*${RUN_TAG}*' }).Count`,
    ], { encoding: 'utf8', timeout: 30000 });
    return parseInt(out.trim(), 10) || 0;
  } catch { return 0; }
}

function killTree() {
  if (process.platform === 'win32') {
    for (const s of spawned) {
      if (!s.proc || !s.proc.pid) continue;
      try {
        execFileSync('taskkill', ['/PID', String(s.proc.pid), '/T', '/F'], { stdio: 'ignore' });
      } catch { /* already gone */ }
    }
    // Catch-all for anything Chrome relaunched under a PID we never held.
    try {
      execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command',
        `Get-CimInstance Win32_Process -Filter "Name='chrome.exe' OR Name='msedge.exe'" | ` +
        `Where-Object { $_.CommandLine -like '*${RUN_TAG}*' } | ` +
        `ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }`,
      ], { stdio: 'ignore', timeout: 30000 });
    } catch { /* best effort */ }
  } else {
    for (const s of spawned) {
      try { s.proc.kill('SIGKILL'); } catch { /* already gone */ }
    }
    try { execFileSync('pkill', ['-9', '-f', RUN_TAG], { stdio: 'ignore' }); } catch { /* none left */ }
  }
}

export function teardown(reason = 'exit') {
  if (tornDown) return;
  tornDown = true;
  process.stderr.write(`\n[teardown] ${reason}: killing ${spawned.length} browser tree(s)\n`);
  killTree();

  // Verify rather than assume, and retry once if anything survived.
  let stray = strayCount();
  if (stray > 0) {
    syncSleep(500);
    killTree();
    stray = strayCount();
  }
  process.stderr.write(stray === 0
    ? '[teardown] verified: no processes left carrying this run\'s signature\n'
    : `[teardown] WARNING: ${stray} process(es) still carry ${RUN_TAG} — kill them manually\n`);

  // Chrome can hold profile-dir locks briefly after the kill.
  for (let i = 0; i < 4; i++) {
    try { fs.rmSync(PROFILE_ROOT, { recursive: true, force: true }); break; }
    catch { syncSleep(150); }
  }
}

process.on('exit', () => teardown('exit'));
process.on('SIGINT', () => { teardown('SIGINT'); process.exit(130); });
process.on('SIGTERM', () => { teardown('SIGTERM'); process.exit(143); });
process.on('uncaughtException', (e) => {
  process.stderr.write(`[fatal] ${(e && e.stack) || e}\n`);
  teardown('uncaughtException');
  process.exit(1);
});

export async function launchBrowser(idx, port) {
  const profile = path.join(PROFILE_ROOT, `guest-${idx}`);
  fs.mkdirSync(profile, { recursive: true });
  const proc = spawn(CHROME, [
    '--headless=new',
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profile}`,
    '--no-first-run', '--no-default-browser-check',
    '--disable-gpu', '--disable-dev-shm-usage', '--disable-extensions',
    '--disable-backgrounding-occluded-windows',
    '--disable-renderer-backgrounding',
    '--disable-background-timer-throttling',   // a pocketed guest still acts
    '--window-size=430,900',                   // phone-ish, so mobile layout runs
    'about:blank',
  ], { stdio: 'ignore', detached: false });
  spawned.push({ proc, port, profile });

  for (let i = 0; i < 60; i++) {
    try {
      const r = await fetch(`http://127.0.0.1:${port}/json/version`);
      if (r.ok) return { proc, port, profile };
    } catch { /* not up yet */ }
    await sleep(500);
  }
  throw new Error(`browser ${idx} never exposed CDP on ${port}`);
}

/** One CDP session against a page target. */
export class Page {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); }

  static async attach(port) {
    const list = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    let target = list.find(t => t.type === 'page');
    if (!target) target = await (await fetch(`http://127.0.0.1:${port}/json/new`)).json();

    const ws = new WebSocket(target.webSocketDebuggerUrl);
    const page = new Page(ws);
    await new Promise((res, rej) => {
      ws.onopen = res;
      ws.onerror = () => rej(new Error('cdp socket failed'));
      setTimeout(() => rej(new Error('cdp socket timeout')), 20000);
    });
    ws.onmessage = (ev) => {
      let msg; try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.id && page.pending.has(msg.id)) {
        const { resolve, reject } = page.pending.get(msg.id);
        page.pending.delete(msg.id);
        msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
      }
    };
    return page;
  }

  send(method, params = {}) {
    const id = ++this.id;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`cdp timeout: ${method}`));
        }
      }, 30000);
    });
  }

  async goto(url) {
    await this.send('Page.enable');
    await this.send('Page.navigate', { url });
    await sleep(1200);
  }

  /** Run JS in the page and return a JSON-able value.
   *
   * This is how the harness clicks: real DOM elements, real event handlers,
   * real websocket updates — not API calls pretending to be a user.
   */
  async eval(expr) {
    const r = await this.send('Runtime.evaluate', {
      expression: `(async () => { ${expr} })()`,
      awaitPromise: true, returnByValue: true,
    });
    if (r.exceptionDetails) {
      throw new Error(r.exceptionDetails.exception?.description || 'page error');
    }
    return r.result?.value;
  }

  close() { try { this.ws.close(); } catch { /* already closed */ } }
}

export { sleep, spawned, PROFILE_ROOT, RUN_TAG };
