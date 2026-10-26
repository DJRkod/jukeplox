"""AE5 drive for the container lane: play a local track through the Direct backend
and prove the pipeline reaches playing state and advances position.

Runs with the installed venv's interpreter against a uvicorn started by
lane-inside.sh. Uses only the public admin API, the same calls the Admin page
makes: first-run setup (or login), connect a Local source, wait for the scan,
select the Direct backend, append a track, then poll the position endpoint.

Exit 0 on success; non-zero with a one-line reason otherwise.
"""
from __future__ import annotations

import argparse
import secrets
import sys
import time
from pathlib import Path

import httpx


def fail(msg: str) -> None:
    print(f"lane-play: FAIL: {msg}")
    sys.exit(1)


def expect_ok(r: httpx.Response, label: str) -> None:
    if r.status_code != 200:
        fail(f"{label} {r.status_code} {r.text[:200]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--music", required=True, help="directory holding the synthesized track")
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--password-file", default=None,
                    help="VM lane only: read the admin password from this file (create it with a fresh "
                         "random one if absent) so later checks can log in again; never a literal")
    ap.add_argument("--keep-queue", action="store_true", help="leave the queue playing (EOS-advance checks)")
    args = ap.parse_args()

    c = httpx.Client(base_url=args.base, timeout=20.0)

    # ── auth: first-run setup on a fresh data dir (the lane always starts one).
    # The admin password is generated per run and never written anywhere.
    pw = None
    if args.password_file:
        pf = Path(args.password_file)
        if pf.exists():
            pw = pf.read_text(encoding="utf-8").strip()
        else:
            pw = secrets.token_urlsafe(18)
            pf.touch(mode=0o600, exist_ok=False)  # mode first, then the secret
            pf.write_text(pw, encoding="utf-8")
    pw = pw or secrets.token_urlsafe(18)
    r = c.post("/admin/auth/setup", json={"password": pw})
    if r.status_code == 403:
        r = c.post("/admin/auth/login/local", json={"password": pw})
        expect_ok(r, "login (setup already complete; stored password rejected)")
    expect_ok(r, "auth")

    # ── source: the synthesized track's directory ─────────────────────────────
    r = c.post("/admin/sources/local", json={"root_dir": args.music, "name": "lane"})
    expect_ok(r, "sources/local")

    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        st = c.get("/admin/scan-status").json()
        if st.get("scanned") and not st.get("scanning"):
            break
        time.sleep(1)
    else:
        fail(f"scan never finished: {st}")

    # ── find the track through the browse API ────────────────────────────────
    # scan-status can report "scanned" for the pre-scan (empty) catalog, and the
    # browse answer is a prepared snapshot rebuilt asynchronously — poll the list.
    items = []
    while time.monotonic() < deadline and not items:
        albums = c.get("/api/browse/albums").json()
        items = albums if isinstance(albums, list) else albums.get("albums") or albums.get("items") or []
        items or time.sleep(1)
    items or fail(f"no albums after scan: {str(albums)[:200]}")
    album_id = items[0]["id"]
    tracks = c.get(f"/api/browse/albums/{album_id}/tracks").json()
    tracks = tracks if isinstance(tracks, list) else tracks.get("tracks") or []
    tracks or fail("album has no tracks")
    track_ids = [t.get("track_id") or t.get("id") for t in tracks]
    all(track_ids) or fail(f"track without id: {tracks}")
    track_id = track_ids[0]

    # ── output: Direct backend, default ALSA device (the lane's null PCM) ────
    r = c.post("/admin/output/active", json={"backend_type": "direct", "device_id": "default"})
    expect_ok(r, "output/active")

    # ── queue: appending to an idle queue auto-starts playback ───────────────
    for tid in track_ids:
        r = c.post("/admin/queue", json={"track_id": tid})
        expect_ok(r, "queue")

    # ── proof. Two acceptable forms:
    #  (a) pacing: is_playing with position advancing over a few seconds — a real or
    #      loopback card (VM lane);
    #  (b) lifecycle: every queued track reaches the play history, which requires a
    #      confirmed start and an EOS-driven advance — the ALSA `null` PCM has no
    #      clock, so on the container lane tracks drain in milliseconds and only (b)
    #      can be observed.
    first = None
    p: dict = {}
    while time.monotonic() < deadline:
        if len(track_ids) > 1:  # lifecycle proof needs two tracks; skip the fetch otherwise
            hist = c.get("/api/queue").json().get("history") or []
            played = {h.get("track_id") for h in hist}
        else:
            played = set()
        if len(track_ids) > 1 and all(t in played for t in track_ids):
            print(f"lane-play: PASS: all {len(track_ids)} tracks reached play history "
                  f"(confirmed start + EOS advance), tracks {track_ids}")
            return
        p = c.get("/api/playback/position").json()
        if p.get("is_playing") and p.get("position_ms", 0) > 0:
            if first is None:
                first = (time.monotonic(), p["position_ms"])
            elif time.monotonic() - first[0] >= 4 and p["position_ms"] > first[1] + 2000:
                print(f"lane-play: PASS: playing, position {first[1]} -> {p['position_ms']} ms "
                      f"over {time.monotonic() - first[0]:.1f}s (track {track_id})")
                if not args.keep_queue:
                    c.post("/admin/queue/clear")
                return
        time.sleep(1)
    fail(f"pipeline did not reach playing with advancing position: last={p}")


if __name__ == "__main__":
    main()
