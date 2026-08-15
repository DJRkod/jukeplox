"""Admin settings churn during live playback.

The first soak hammered transport but never touched configuration. Real hosts
fiddle: they flip flood control when the queue fills with repeats, hide a tab,
lock the queue when someone takes the piss. Each of those changes state that
guests are actively reading — several broadcast to every connected browser —
while audio is playing.

Deliberately avoids the genuinely destructive endpoints (rescan, source removal,
password change, history/most-played mutation): those would wreck the run's own
measurements rather than test anything.
"""
import os
import random
import time

from jp_http import Client, Recorder, minutes, require_base

BASE = require_base()
MINUTES = minutes()
PW = os.environ.get("JP_ADMIN_PW", "")
LOG = os.environ.get("JP_TOGGLE_LOG", "toggle-actions.jsonl")

_c = Client(BASE)
call = _c.call
get = _c.get_json
# Opened in main(), not at import: a module-level Recorder creates its log file
# as an import side effect, which litters the tree and once got a stray empty
# JSONL committed.
rec = None


def setting(**kw):
    return call("POST", "/admin/settings", kw)


def main():
    global rec
    rec = Recorder(LOG)
    st, _ = _c.login(PW)
    rec(act="login", status=st)
    if st != 200:
        rec(act="ABORT", why="login failed")
        return

    before = get("/admin/settings")
    rec(act="settings_before", keys=len(before) if isinstance(before, dict) else None)

    end = time.time() + MINUTES * 60

    toggles = [
        # Flood control is the interesting one: with it ON, a guest re-adding a
        # queued track should 409. Guests are triple-tapping throughout, so this
        # exercises whether concurrent duplicate POSTs race the check.
        ("floodOn", lambda: setting(flood_control=True)),
        ("floodOff", lambda: setting(flood_control=False)),
        # Live-applied and documented as bumping the arming generation — the
        # most mechanically risky flip in the list.
        ("gaplessOn", lambda: setting(gapless_enabled=True)),
        ("gaplessOff", lambda: setting(gapless_enabled=False)),
        # These broadcast appearance_changed to every connected guest mid-browse.
        ("facetGenre", lambda: setting(facet_genre=random.choice([True, False]))),
        ("facetYears", lambda: setting(facet_years=random.choice([True, False]))),
        ("facetMostPlayed", lambda: setting(facet_mostplayed=random.choice([True, False]))),
        ("facetRadio", lambda: setting(facet_radio=random.choice([True, False]))),
        ("railMode", lambda: setting(rail_mode=random.choice(
            ["vanilla", "magnetic", "waveform", "loupe", "vu"]))),
        ("scheme", lambda: setting(default_scheme=random.choice(
            ["gold-rush", "king-crimson", "case-of-blue", "pink-side"]))),
        ("defaultView", lambda: setting(default_view=random.choice(["list", "tile"]))),
        ("ratingStyle", lambda: setting(rating_style=random.choice(
            ["stars", "dots", "bars"]))),
        # Guest-visibility flags the guest UI reads live.
        ("ratingsVisible", lambda: setting(ratings_visible_to_guests=random.choice([True, False]))),
        ("tagsVisible", lambda: setting(tags_visible_to_guests=random.choice([True, False]))),
        ("guestRadio", lambda: setting(guest_radio_control=random.choice([True, False]))),
        # Surprise Me is being pressed by guests throughout — flip it under them.
        ("surpriseOff", lambda: setting(surprise_me_enabled=False)),
        ("surpriseOn", lambda: setting(surprise_me_enabled=True)),
        ("surpriseMode", lambda: setting(surprise_me_source_mode=random.choice(
            ["auto", "heuristic", "random"]))),
        ("surpriseDiversity", lambda: setting(surprise_me_diversity=random.choice(
            ["off", "album", "artist"]))),
        # Changes what happens when the queue empties — guests are draining it.
        ("queueEnd", lambda: setting(queue_end_behavior=random.choice(
            ["stop", "popular_random", "full_random"]))),
        ("lengthBand", lambda: setting(random_min_seconds=random.choice([0, 60]),
                                       random_max_seconds=random.choice([0, 600]))),
        # Lock the queue out from under guests who are mid-add, then release.
        ("queueLock", lambda: call("POST", "/admin/queue/lock")),
        ("queueUnlock", lambda: call("POST", "/admin/queue/unlock")),
    ]

    # Weight the mechanically risky ones up; cosmetic ones still fire, less often.
    weighted = (
        [t for t in toggles if t[0] in ("floodOn", "floodOff")] * 3 +
        [t for t in toggles if t[0] in ("gaplessOn", "gaplessOff")] * 3 +
        [t for t in toggles if t[0] in ("queueLock", "queueUnlock")] * 2 +
        [t for t in toggles if t[0].startswith("surprise")] * 2 +
        [t for t in toggles if t[0].startswith("facet")] * 2 +
        toggles
    )

    n = 0
    while time.time() < end:
        n += 1
        name, fn = random.choice(weighted)
        pos_before = get("/api/playback/position")
        code, body = fn()
        time.sleep(1.5)
        pos_after = get("/api/playback/position")
        npw = get("/api/now-playing")
        entry = {
            "act": name, "code": code,
            "playing_before": pos_before.get("is_playing"),
            "playing_after": pos_after.get("is_playing"),
            "session": (npw.get("output_session") or {}).get("state"),
        }
        if code not in (200, 204):
            entry["body"] = body[:250]
        # Did a config flip stop the music?
        if pos_before.get("is_playing") and not pos_after.get("is_playing"):
            entry["PLAYBACK_STOPPED"] = True
        rec(**entry)
        time.sleep(random.uniform(6, 16))

    # Leave the box in a sane state for whoever looks next.
    setting(flood_control=False, surprise_me_enabled=True,
            facet_genre=True, facet_years=True, facet_mostplayed=True,
            ratings_visible_to_guests=False, tags_visible_to_guests=False,
            guest_radio_control=False, facet_radio=False,
            queue_end_behavior="stop")
    call("POST", "/admin/queue/unlock")
    rec(act="toggleChaosEnd", flips=n)
    rec.close()


if __name__ == "__main__":
    main()
