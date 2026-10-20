// Guest-side experience reporting: the device matrix you will never own.
//
// Four reports arrived about the interface feeling slow or wrong, across phones
// and browsers nobody here has. Two of them turned out not to reproduce on any
// engine available locally, and one of those is very likely specific to iPhone
// behaviour that cannot be emulated. This module is the answer to that: let the
// guest's own browser measure what it actually experienced and tell the jukebox.
//
// NOTHING LEAVES THE INSTALLATION.
//
// There is one destination, it is a relative path, and a relative path cannot
// address another host. That property is enforced by a running test that drives
// this flow in a real engine, intercepts every request across the full page
// lifecycle including the flush, and fails on anything that is not this origin.
// A deliberately-leaking fixture must make that test fail, so the guard itself
// is credentialed rather than merely green. The source scan for absolute
// addresses is a secondary check, because it cannot see a destination built by
// concatenation or handed to a websocket.
//
// WHAT IS COLLECTED, AND WHY SO LITTLE
//
// A view name, never a location. For a music library the location IS personal
// data: it names the artist or album someone was looking at. Everything else is
// bucketed -- engine family and major version, a viewport band, a rounded pixel
// ratio, coarse device hints. No identifiers of any kind, and time only to the
// minute, because a finer stamp is a correlation handle across records.
//
// The population here is a house party, not the open internet, so a handful of
// bucketed attributes can still narrow to one phone. That is why there is no
// identifier to join them on, why the feature is off until an operator turns it
// on, and why there is a view that shows exactly what is stored.
//
// SAMPLING
//
// Tail-first: every interaction past the threshold is reported with its
// attribution, plus a small random share of good ones. Without that share there
// is a count and no distribution -- a number with nothing to compare it to,
// which this project has already been caught by.
//
// THE CAPABILITY GAP IS DECLARED, NOT INFERRED
//
// Interaction timing works in every current engine. The deeper diagnostics that
// decompose a slow frame into script versus style-and-layout are Chromium-only,
// and measured absent in WebKit. A record that simply omits them looks exactly
// like a record from an engine that had nothing to report, so each record says
// which capabilities its engine had.

(function () {
  'use strict';

  var ENDPOINT = '/api/client-report';
  var CONFIG = '/api/client-report/config';

  // Interaction timing rounds its own duration to 8ms and its observer floors
  // at 16ms, so nothing finer is reportable however good the clock is.
  var SLOW_MS = 200;          // tail: always report at or above this
  var GOOD_SAMPLE_RATE = 0.05; // and this share of the rest, for the baseline
  var MAX_BATCH = 40;          // the server caps too; this avoids building more

  var enabled = false;
  var queue = [];
  var restoreOutcomes = [];
  var flushed = false;
  var lastRecord = null;
  var caps = { event: false, loaf: false, longtask: false };
  var view = 'unknown';

  function engineInfo() {
    // A family and a major version, from the structured API where it exists.
    // Never the full agent string: that is a fingerprint, and it carries build
    // numbers and device models nobody here needs.
    var family = 'unknown';
    var major = null;
    try {
      var d = navigator.userAgentData;
      if (d && d.brands && d.brands.length) {
        for (var i = 0; i < d.brands.length; i++) {
          var b = d.brands[i].brand || '';
          if (/Not.?A.?Brand/i.test(b)) continue;
          if (/Chromium|Chrome|Edge/i.test(b)) { family = 'chromium'; }
          else if (/Firefox/i.test(b)) { family = 'gecko'; }
          else if (/Safari|WebKit/i.test(b)) { family = 'webkit'; }
          if (family !== 'unknown') {
            major = parseInt(d.brands[i].version, 10) || null;
            break;
          }
        }
      }
    } catch (_) { /* structured hints unavailable */ }
    if (family === 'unknown') {
      // Feature probes, not string parsing. Each is a capability difference
      // this rig has actually measured between the engines.
      try {
        var types = (window.PerformanceObserver
                     && PerformanceObserver.supportedEntryTypes) || [];
        if (types.indexOf('long-animation-frame') !== -1) family = 'chromium';
        else if (types.indexOf('layout-shift') !== -1) family = 'chromium';
        else if ('GestureEvent' in window) family = 'webkit';
        else if ('mozInnerScreenX' in window) family = 'gecko';
      } catch (_) { /* leave unknown */ }
    }
    return { engine: family, engineMajor: major };
  }

  function viewportBand(w) {
    if (w < 480) return 'xs';
    if (w < 768) return 'sm';
    if (w < 1100) return 'md';
    return 'lg';
  }

  function minuteStamp() {
    // Minute resolution. Anything finer lets two records be correlated into a
    // session, which is precisely what not carrying an identifier avoids.
    var d = new Date();
    d.setSeconds(0, 0);
    return d.toISOString().slice(0, 16);
  }

  function baseRecord() {
    var e = engineInfo();
    return {
      view: view,
      engine: e.engine,
      engineMajor: e.engineMajor,
      band: viewportBand(window.innerWidth || 0),
      dpr: Math.round((window.devicePixelRatio || 1) * 10) / 10,
      cores: navigator.hardwareConcurrency || null,
      minute: minuteStamp(),
      caps: { event: caps.event, loaf: caps.loaf, longtask: caps.longtask }
    };
  }

  function enqueue(rec) {
    if (!enabled) return;
    if (queue.length >= MAX_BATCH) return;
    queue.push(rec);
    lastRecord = rec;
  }

  function reportInteraction(entry) {
    var slow = entry.duration >= SLOW_MS;
    if (!slow && Math.random() > GOOD_SAMPLE_RATE) return;
    var rec = baseRecord();
    rec.kind = 'interaction';
    rec.name = entry.name;
    rec.durationMs = Math.round(entry.duration);
    rec.slow = slow;
    if (entry.target) rec.target = String(entry.target).slice(0, 40);
    enqueue(rec);
  }

  function observe() {
    var types = [];
    try {
      types = (window.PerformanceObserver
               && PerformanceObserver.supportedEntryTypes) || [];
    } catch (_) { types = []; }
    caps.event = types.indexOf('event') !== -1;
    caps.loaf = types.indexOf('long-animation-frame') !== -1;
    caps.longtask = types.indexOf('longtask') !== -1;

    if (caps.event) {
      try {
        new PerformanceObserver(function (list) {
          var es = list.getEntries();
          for (var i = 0; i < es.length; i++) reportInteraction(es[i]);
        }).observe({ type: 'event', buffered: true, durationThreshold: 16 });
      } catch (_) { caps.event = false; }
    }
    if (caps.loaf) {
      try {
        new PerformanceObserver(function (list) {
          var es = list.getEntries();
          for (var i = 0; i < es.length; i++) {
            if (es[i].blockingDuration < 50) continue;
            var rec = baseRecord();
            rec.kind = 'slowFrame';
            rec.blockingMs = Math.round(es[i].blockingDuration);
            enqueue(rec);
          }
        }).observe({ type: 'long-animation-frame', buffered: true });
      } catch (_) { caps.loaf = false; }
    }
  }

  function flush() {
    if (!enabled || !queue.length) return false;
    var body = JSON.stringify({ records: queue.slice(0, MAX_BATCH) });
    queue = [];
    flushed = true;
    try {
      if (navigator.sendBeacon) {
        navigator.sendBeacon(ENDPOINT, new Blob([body],
          { type: 'application/json' }));
        return true;
      }
    } catch (_) { /* fall through */ }
    try {
      fetch(ENDPOINT, { method: 'POST', body: body, keepalive: true,
                        headers: { 'Content-Type': 'application/json' } });
    } catch (_) { /* observability must never break the page */ }
    return true;
  }

  // The route to settling the Safari back-out report: one boolean per return,
  // saying whether the remembered album landed back where it was. Correctness,
  // not timing.
  function recordRestore(ok, detail) {
    var rec = baseRecord();
    rec.kind = 'restore';
    rec.ok = !!ok;
    if (detail) rec.detail = String(detail).slice(0, 40);
    restoreOutcomes.push({ ok: !!ok, minute: rec.minute });
    enqueue(rec);
    return rec;
  }

  function setView(name) {
    view = String(name || 'unknown').slice(0, 32);
  }

  async function start() {
    // Off until the installation says otherwise. A failure to read the setting
    // leaves it off: the safe direction for something that observes guests.
    try {
      var res = await fetch(CONFIG, { method: 'GET' });
      if (!res.ok) return false;
      var cfg = await res.json();
      enabled = !!(cfg && cfg.enabled);
    } catch (_) {
      enabled = false;
      return false;
    }
    if (!enabled) return false;
    observe();
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'hidden') flush();
    });
    return true;
  }

  window.jpClientReport = {
    start: start,
    setView: setView,
    recordRestore: recordRestore,
    flush: flush,
    isEnabled: function () { return enabled; },
    // Test surface. No runtime caller; the guards read it to assert what was
    // collected rather than inspecting the network payload.
    __state: function () {
      return {
        enabled: enabled,
        queued: queue.length,
        flushed: flushed,
        sample: lastRecord,
        restoreOutcomes: restoreOutcomes.slice(0, 5),
        caps: caps
      };
    }
  };
})();
