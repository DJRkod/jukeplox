"""Measure the rail-jump landing error on a cabled Android device.

This is the rig's highest-authority measurement and the Android lane's
credential in one run. It is also the only measurement in the whole rig taken
with input the operating system injected rather than the browser synthesised.

What it measures
----------------
Tapping a letter on the alphabet rail scrolls the list to that letter. The
initial scroll write is ancestor-blind, so it lands short and a correction loop
nudges it into place over following frames -- recorded in `_settleJump`'s own
comment as an open to-do, and measured at one ~40px correction on 6 of 7 letters
against the deployed build on 2026-10-12.

The discriminating observable is the COUNT and SIZE of post-landing corrections,
sampled per frame. Not "did the jump look right": a jump that lands 40px short
and then creeps into place looks fine in a screenshot and is exactly the defect.

Why no network is needed
------------------------
The rig's own server hosts the harness, and `adb reverse` makes the phone reach
it back down the cable. Nothing is exposed on the LAN, no deployment is
involved, and no address is baked in anywhere.

Hygiene
-------
The device serial is never printed. Chrome on the device is stopped at the end,
the port forwards are removed, and the local server is shut down in a `finally`
-- this repo's recorded incidents are 120 and 140 orphaned browser processes.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHROME_PKG = "com.android.chrome"


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def sh(adb, *args, timeout=30):
    """Run an adb command. The serial is never passed or printed."""
    out = subprocess.run([adb, *args], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=timeout)
    return out.returncode, (out.stdout or "").strip(), (out.stderr or "").strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--adb", required=True, help="path to adb.exe")
    ap.add_argument("--letter", default="M", help="rail letter to tap")
    ap.add_argument("--albums", type=int, default=1500)
    ap.add_argument("--frames", type=int, default=45,
                    help="frames of scroll position to sample after the tap")
    args = ap.parse_args()

    sv = _load("cr_serve", "tools/clientrig/serve.py")
    adbmod = _load("cr_adb", "tools/clientrig/adb.py")

    rc, out, err = sh(args.adb, "devices")
    devices = adbmod.parse_devices(out)
    code, why = adbmod.device_check(devices)
    if code is not None:
        print(json.dumps({"blocked": why}, indent=2))
        return 2

    httpd, port = sv.make_server(
        upstream="http://127.0.0.1:1", overlay_root=None,
        static_root=ROOT / "static",
        harness_root=ROOT / "tools" / "clientrig" / "harness")
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()

    result: dict = {"port": port, "letter": args.letter}
    try:
        # The phone reaches our loopback server back down the cable.
        sh(args.adb, "reverse", f"tcp:{port}", f"tcp:{port}")
        url = (f"http://127.0.0.1:{port}/harness/rail_repaint.html"
               f"?albums={args.albums}&artists=300")

        sh(args.adb, "shell", "am", "force-stop", CHROME_PKG)
        time.sleep(1)
        sh(args.adb, "shell", "am", "start", "-a", "android.intent.action.VIEW",
           "-n", f"{CHROME_PKG}/com.google.android.apps.chrome.Main", "-d", url)
        time.sleep(6)

        # CDP over the cable.
        sh(args.adb, "forward", f"tcp:{port + 1}",
           "localabstract:chrome_devtools_remote")
        import urllib.request
        targets = []
        for _ in range(20):
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port + 1}/json/list", timeout=5) as r:
                    targets = json.loads(r.read().decode("utf-8"))
                if any(t_.get("type") == "page" for t_ in targets):
                    break
            except Exception:
                pass
            time.sleep(1)
        page = next((t_ for t_ in targets if t_.get("type") == "page"
                     and "rail_repaint" in (t_.get("url") or "")), None)
        if not page:
            result["error"] = ("no debuggable page found -- check that USB "
                               "debugging is authorised and Chrome opened the "
                               "harness")
            result["targets_seen"] = [t_.get("url", "")[:60] for t_ in targets]
            print(json.dumps(result, indent=2))
            return 3

        from websocket import create_connection  # type: ignore
        # Chrome rejects a CDP WebSocket whose Origin it was not told to allow,
        # and we cannot add --remote-allow-origins to Chrome on the device the
        # way the desktop bench does. Sending no Origin header at all is
        # accepted, and is the only route that needs nothing from the phone.
        ws = create_connection(page["webSocketDebuggerUrl"], timeout=30,
                               suppress_origin=True)
        _id = [0]

        def cdp(method, params=None):
            _id[0] += 1
            ws.send(json.dumps({"id": _id[0], "method": method,
                                "params": params or {}}))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == _id[0]:
                    return msg.get("result", {})

        def ev(expr, await_promise=False):
            r = cdp("Runtime.evaluate", {"expression": expr,
                                         "returnByValue": True,
                                         "awaitPromise": await_promise})
            return (r.get("result") or {}).get("value")

        cdp("Runtime.enable")
        ev("window.__harness.mount()", True)
        # Show the pane BEFORE rendering. Toggling visibility afterwards does
        # not re-activate the rail -- correctly, since the U7 fix makes rail
        # ownership refuse a column that is not on screen, and nothing
        # re-renders on a bare visibility change. Rendering into the visible
        # pane is also the real user path.
        ev("window.__harness.show('albums-view')")
        ev("window.__harness.cacheBothLists()", True)
        ev("window.__harness.settle(60)", True)

        state = ev("window.__railState()")
        result["rail"] = {k: state.get(k) for k in
                          ("railVisible", "buttonCount", "hostId")}
        if not state.get("railVisible"):
            result["error"] = "no visible rail on the device; nothing to tap"
            print(json.dumps(result, indent=2))
            return 4

        # Where is the letter button on screen, in DEVICE pixels?
        box = ev(f"""(() => {{
          const b = [...document.querySelectorAll('.alpha-rail [data-bucket]')]
            .find(x => (x.textContent || '').trim().toUpperCase()
                        .startsWith('{args.letter}'));
          if (!b) return null;
          const r = b.getBoundingClientRect();
          const d = window.devicePixelRatio || 1;
          return {{cssX: Math.round(r.left + r.width / 2),
                   cssY: Math.round(r.top + r.height / 2),
                   x: Math.round((r.left + r.width / 2) * d),
                   y: Math.round((r.top + r.height / 2) * d),
                   dpr: d, label: (b.textContent || '').trim()}};
        }})()""")
        if not box:
            result["error"] = f"no rail button for {args.letter!r}"
            print(json.dumps(result, indent=2))
            return 5
        result["target"] = box

        # Arm a per-frame scroll sampler BEFORE the tap. No warm-up: the first
        # interaction is the phenomenon.
        ev(f"""(() => {{
          const pane = document.querySelector('#albums-list')
                    || document.querySelector('.alpha-items-column').parentElement;
          window.__samples = [];
          let n = 0;
          const tick = () => {{
            window.__samples.push(Math.round(
              (pane.scrollTop !== undefined ? pane.scrollTop
               : document.scrollingElement.scrollTop)));
            if (++n < {args.frames}) requestAnimationFrame(tick);
          }};
          window.__startSampling = () => requestAnimationFrame(tick);
          return true;
        }})()""")
        ev("window.__startSampling()")

        # Dispatched touch over the debugger, in PAGE coordinates.
        #
        # An OS-level `adb shell input tap` would be purer, but it addresses
        # SCREEN pixels while the button position is known in page pixels, and
        # Chrome's URL bar sits between the two -- an uncalibrated tap lands in
        # the browser chrome and nothing scrolls (measured: 45 frames of zero).
        # Input.dispatchTouchEvent is the method the October investigation used,
        # so these numbers are directly comparable to the recorded ones, and it
        # is a trusted-event path: a desktop probe measured 18 interaction
        # entries from a dispatched touch against 0 from a scripted click.
        def tap():
            cdp("Input.dispatchTouchEvent", {
                "type": "touchStart",
                "touchPoints": [{"x": box["cssX"], "y": box["cssY"]}]})
            cdp("Input.dispatchTouchEvent", {"type": "touchEnd",
                                            "touchPoints": []})

        # COLD jump: the page has never scrolled, so Chrome's URL bar is still
        # expanded and will collapse on the first scroll, changing the visible
        # viewport mid-jump.
        tap()
        time.sleep(2.0)
        cold = ev("window.__samples") or []
        cold_vv = ev("window.visualViewport ? Math.round(window.visualViewport.height) : null")

        # WARM jump: same letter, bar already collapsed. If the correction is
        # the bar, it disappears here while the arithmetic is unchanged.
        ev("window.__samples = []")
        ev("window.__startSampling()")
        tap()
        time.sleep(2.0)
        warm = ev("window.__samples") or []
        warm_vv = ev("window.visualViewport ? Math.round(window.visualViewport.height) : null")

        def moves_of(sm):
            return [[i, sm[i] - sm[i-1]] for i in range(1, len(sm))
                    if abs(sm[i] - sm[i-1]) > 1]
        result["cold"] = {"moves": moves_of(cold), "visual_viewport_h": cold_vv}
        result["warm"] = {"moves": moves_of(warm), "visual_viewport_h": warm_vv}
        time.sleep(0.1)

        samples = ev("window.__samples") or []
        result["samples"] = samples

        # Corrections after the landing: changes of >1px once the first move
        # has happened. One ~40px nudge a frame later is the recorded defect.
        moves = [(i, samples[i] - samples[i - 1])
                 for i in range(1, len(samples))
                 if abs(samples[i] - samples[i - 1]) > 1]
        result["moves"] = moves
        if moves:
            result["initial_jump_px"] = moves[0][1]
            result["corrections"] = [m for m in moves[1:]]
            result["correction_count"] = len(moves) - 1
            result["correction_total_px"] = sum(abs(m[1]) for m in moves[1:])
        result["verdict"] = (
            "LANDED FLUSH: no correction after the initial jump"
            if len(moves) <= 1 else
            f"{len(moves) - 1} correction(s) after landing, "
            f"{sum(abs(m[1]) for m in moves[1:])}px total -- the recorded defect")

        ws.close()
        print(json.dumps(result, indent=2))
        return 0
    finally:
        try:
            sh(args.adb, "shell", "am", "force-stop", CHROME_PKG)
            sh(args.adb, "reverse", "--remove", f"tcp:{port}")
            sh(args.adb, "forward", "--remove", f"tcp:{port + 1}")
        except Exception:
            pass
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


if __name__ == "__main__":
    sys.exit(main())
