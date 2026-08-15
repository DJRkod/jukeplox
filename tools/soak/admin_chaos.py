"""Admin-side transport chaos.

Guests have no transport controls at all, so nothing in the guest party
exercises pause/resume/skip/previous/seek/volume. Those all live in
app/playback_control.py and are shared by every backend, so this driver hammers
them deliberately — including the impatient patterns (double-skip,
skip-during-transition, pause/resume flapping) a host actually produces.
"""
import os
import random
import time

from jp_http import Client, Recorder, minutes, require_base

BASE = require_base()
MINUTES = minutes()
PW = os.environ.get("JP_ADMIN_PW", "")
LOG = os.environ.get("JP_ADMIN_LOG", "admin-actions.jsonl")

_c = Client(BASE)
call = _c.call
get = _c.get_json
# Opened in main(), not at import: a module-level Recorder creates its log file
# as an import side effect, which litters the tree and once got a stray empty
# JSONL committed.
rec = None


def main():
    global rec
    rec = Recorder(LOG)
    st, _ = _c.login(PW)
    rec(act="login", status=st)
    if st != 200:
        rec(act="ABORT", why="admin login failed")
        return

    end = time.time() + MINUTES * 60

    def skip():
        return call("POST", "/admin/playback/skip")

    def prev():
        return call("POST", "/admin/playback/previous")

    def pause():
        return call("POST", "/admin/playback/pause")

    def resume():
        return call("POST", "/admin/playback/resume")

    def volume():
        return call("POST", "/admin/playback/volume", {"level": round(random.uniform(0.2, 0.9), 2)})

    def seek():
        pos = get("/api/playback/position")
        dur = pos.get("duration_ms") or 0
        if not dur:
            return (None, "no duration")
        return call("POST", "/admin/playback/seek", {"position_ms": random.randint(0, max(1, dur - 2000))})

    behaviours = [
        ("skip", lambda: [skip()]),
        # impatient host: two skips back to back, faster than a track can settle
        ("doubleSkip", lambda: [skip(), skip()]),
        ("tripleSkip", lambda: [skip(), skip(), skip()]),
        ("pauseResume", lambda: [pause(), resume()]),
        # flap the transport
        ("pauseFlap", lambda: [pause(), resume(), pause(), resume()]),
        ("previous", lambda: [prev()]),
        ("volume", lambda: [volume()]),
        ("volumeSpam", lambda: [volume(), volume(), volume()]),
        ("seek", lambda: [seek()]),
        ("seekThenSkip", lambda: [seek(), skip()]),
        ("skipThenPause", lambda: [skip(), pause(), resume()]),
    ]

    while time.time() < end:
        name, fn = random.choice(behaviours)
        try:
            results = fn()
            codes = [r[0] for r in results if isinstance(r, tuple)]
            bodies = [r[1][:120] for r in results
                      if isinstance(r, tuple) and r[0] not in (200, 204, None)]
            rec(act=name, codes=codes, errors=bodies or None)
        except Exception as e:
            rec(act=name, fatal=str(e)[:200])
        time.sleep(random.uniform(8, 25))

    rec(act="adminChaosEnd")
    rec.close()


if __name__ == "__main__":
    main()
