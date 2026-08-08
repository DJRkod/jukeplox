"""Surprise Me resolver — degradation chain + source attribution (2026-06-17 plan U2).

The resolver is pure orchestration over an injected Plex client, queue, random
floor, exclusions, and enabled-library list — so these tests use fakes and never
touch Plex or the DB. They pin the mode x seed-state matrix from the plan:
Plex sonic -> Plex similar-artist -> genre/artist heuristic -> random floor, with
the floor always reachable so a press never dead-ends.
"""

from app.plex.models import Track
from app.queue.surprise import (
    resolve_surprise,
    _within_length,
    SOURCE_PLEX_SONIC,
    SOURCE_PLEX_SIMILAR,
    SOURCE_HEURISTIC,
    SOURCE_RANDOM,
)


def mk_track(tid, artist="Artist", genre="Rock", title=None, album_id=None,
             dur_ms=1000) -> Track:
    return Track(id=tid, title=title or f"T{tid}", artist=artist, album="Album",
                 duration_ms=dur_ms, genre=genre, album_id=album_id)


class FakeArtist:
    def __init__(self, id, title):
        self.id = id
        self.title = title


class FakeAlbum:
    def __init__(self, id):
        self.id = id


class _QI:
    def __init__(self, track):
        self.track = track


class _QState:
    def __init__(self, current):
        self.current = current


class FakeQueue:
    def __init__(self, dup_ids=(), queued=(), history=(), current=None):
        self._dups = set(dup_ids)
        # Mirror the engine's public accessors used by the diversity gate.
        self.queue = [_QI(t) for t in queued]
        self.history = [_QI(t) for t in history]
        self.state = _QState(_QI(current) if current is not None else None)

    def is_duplicate(self, track_id):
        return track_id in self._dups


class FakeClient:
    def __init__(self, *, sonic=None, similar_names=None, artists=None,
                 albums=None, album_tracks=None, genre_tracks=None):
        self._sonic = sonic or {}                # track_id -> [Track]
        self._similar_names = similar_names or {}  # track_id -> [name]
        self._artists = artists or {}            # section -> [FakeArtist]
        self._albums = albums or {}              # artist_id -> [FakeAlbum]
        self._album_tracks = album_tracks or {}  # album_id -> [Track]
        self._genre_tracks = genre_tracks or {}  # genre -> [Track]
        self.calls = []

    async def get_sonic_nearest(self, track_id, **kw):
        self.calls.append(("sonic", track_id))
        return list(self._sonic.get(track_id, []))

    async def get_artist_similar_names(self, track_id):
        self.calls.append(("similar_names", track_id))
        return list(self._similar_names.get(track_id, []))

    async def get_artists(self, section_key):
        self.calls.append(("get_artists", section_key))
        return list(self._artists.get(section_key, []))

    async def get_albums(self, section_key, artist_id=None, **kw):
        self.calls.append(("get_albums", section_key, artist_id))
        return list(self._albums.get(artist_id, []))

    async def get_tracks(self, section_key, album_id=None, genre=None, **kw):
        self.calls.append(("get_tracks", section_key, album_id, genre))
        if album_id is not None:
            return list(self._album_tracks.get(album_id, []))
        if genre is not None:
            return list(self._genre_tracks.get(genre, []))
        return []


def make_shuffle(track):
    async def _s():
        return track
    return _s


def make_excl(names):
    async def _e():
        return list(names)
    return _e


def make_libs(*sections):
    async def _l():
        return [{"section_key": s} for s in sections]
    return _l


def _kw(**over):
    """Default injected deps; override per test."""
    base = dict(
        client=FakeClient(),
        queue=FakeQueue(),
        shuffle_provider=make_shuffle(mk_track("rand", artist="Floor")),
        get_exclusions=make_excl([]),
        get_enabled_libraries=make_libs("1"),
    )
    base.update(over)
    return base


SEED = [{"track_id": "s1", "genre": "Rock", "artist": "Seed Act"}]


def _has(calls, kind):
    return any(c[0] == kind for c in calls)


# ── Auto mode: the full chain ──────────────────────────────────────────────────

async def test_auto_sonic_hit():
    """Covers AE1. Auto with a seed: a sonic hit wins."""
    fc = FakeClient(sonic={"s1": [mk_track("c1", artist="X")]})
    track, source = await resolve_surprise(SEED, "auto", **_kw(client=fc))
    assert source == SOURCE_PLEX_SONIC
    assert track.id == "c1"


async def test_auto_sonic_empty_falls_to_similar_artist():
    fc = FakeClient(
        sonic={"s1": []},
        similar_names={"s1": ["Sim Act"]},
        artists={"1": [FakeArtist("a1", "Sim Act")]},
        albums={"a1": [FakeAlbum("al1")]},
        album_tracks={"al1": [mk_track("c2", artist="Sim Act")]},
    )
    track, source = await resolve_surprise(SEED, "auto", **_kw(client=fc))
    assert source == SOURCE_PLEX_SIMILAR
    assert track.id == "c2"


async def test_auto_sonic_and_similar_empty_falls_to_heuristic():
    """Covers AE2. No sonic/similar data → genre heuristic."""
    fc = FakeClient(genre_tracks={"Rock": [mk_track("c3", artist="Y")]})
    track, source = await resolve_surprise(SEED, "auto", **_kw(client=fc))
    assert source == SOURCE_HEURISTIC
    assert track.id == "c3"


async def test_auto_all_empty_falls_to_random_floor():
    fc = FakeClient()  # nothing resolves
    floor = mk_track("rand", artist="Floor")
    track, source = await resolve_surprise(
        SEED, "auto", **_kw(client=fc, shuffle_provider=make_shuffle(floor)))
    assert source == SOURCE_RANDOM


async def test_heuristic_failsoft_when_library_raises():
    """Code-review #4: a library raising during the genre scan is swallowed
    per-library (like sonic/similar), so surprise degrades to the random floor
    instead of 500-ing the public endpoint."""
    class BoomClient(FakeClient):
        async def get_tracks(self, section_key, album_id=None, genre=None, **kw):
            raise RuntimeError("library unavailable")
    floor = mk_track("rand", artist="Floor")
    track, source = await resolve_surprise(
        SEED, "auto", **_kw(client=BoomClient(), shuffle_provider=make_shuffle(floor)))
    assert source == SOURCE_RANDOM   # heuristic raised → swallowed → random floor
    assert track.id == "rand"
    assert track.id == "rand"


# ── Resolution latency budget ───────────────────────────────────────────────────

async def test_resolve_budget_skips_later_sources_after_overrun():
    """Between-source deadline: a source that OVERRUNS the budget without a hit
    causes the remaining smart sources to be skipped (via the _remaining()<=0
    pre-check) — the chain falls to the fast random floor instead of piling on
    more slow Plex work. Note: the budget gates STARTING a source; it does not
    cancel one mid-flight (see the no-cancellation test below)."""
    import asyncio
    class SlowEmptySonic(FakeClient):
        async def get_sonic_nearest(self, track_id, **kw):
            self.calls.append(("sonic", track_id))
            await asyncio.sleep(0.3)   # slow, but no candidates → consumes budget
            return []
    fc = SlowEmptySonic(
        similar_names={"s1": ["Sim"]}, artists={"1": [FakeArtist("a1", "Sim")]},
        albums={"a1": [FakeAlbum("al1")]}, album_tracks={"al1": [mk_track("c2")]},
        genre_tracks={"Rock": [mk_track("c3")]},
    )
    floor = mk_track("rand", artist="Floor")
    track, source = await resolve_surprise(
        SEED, "auto", **_kw(client=fc, shuffle_provider=make_shuffle(floor)),
        resolve_budget_s=0.1)
    assert source == SOURCE_RANDOM and track.id == "rand"
    # similar + heuristic skipped once the budget was spent — never attempted.
    assert not _has(fc.calls, "similar_names")
    assert not _has(fc.calls, "get_artists")
    assert not _has(fc.calls, "get_tracks")


async def test_resolve_no_midsource_cancellation_started_source_completes():
    """A smart source that has STARTED within budget runs to completion and its
    hit is used — the resolver no longer cancels mid-flight. (Mid-source
    cancellation poisoned the shared Plex httpx pool and broke the subsequent
    floor on real hardware — a ~23s press that queued nothing.)"""
    import asyncio
    class SlowHitSonic(FakeClient):
        async def get_sonic_nearest(self, track_id, **kw):
            await asyncio.sleep(0.3)
            return [mk_track("c1", artist="X")]
    track, source = await resolve_surprise(
        SEED, "auto", **_kw(client=SlowHitSonic()), resolve_budget_s=0.1)
    # Not cancelled at the 0.1s budget — sonic completed and its hit is used.
    assert source == SOURCE_PLEX_SONIC and track.id == "c1"


async def test_floor_tolerates_transient_draw_error():
    """Never-dead-end: a transient error on one floor draw (e.g. a Plex
    ReadTimeout, or the shared pool briefly degraded) must not abandon the floor
    and queue nothing — it retries and still returns a track when content exists."""
    calls = {"n": 0}
    async def flaky_floor():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient plex read timeout")
        return mk_track("rand", artist="Floor")
    track, source = await resolve_surprise(
        [], "random", **_kw(shuffle_provider=flaky_floor))
    assert source == SOURCE_RANDOM and track.id == "rand"
    assert calls["n"] == 2   # retried after the transient error


async def test_resolve_budget_generous_still_uses_smart_source():
    """The budget only bites on a stall: a fast smart source under budget still
    wins (no behavior change on a healthy server)."""
    fc = FakeClient(sonic={"s1": [mk_track("c1", artist="X")]})
    track, source = await resolve_surprise(
        SEED, "auto", **_kw(client=fc), resolve_budget_s=8.0)
    assert source == SOURCE_PLEX_SONIC and track.id == "c1"


# ── No-seed rule (R4) ──────────────────────────────────────────────────────────

async def test_no_seed_goes_random_and_skips_smart_sources():
    """Covers AE3. A fresh browser (no picks) → random, no Plex/heuristic work."""
    fc = FakeClient(genre_tracks={"Rock": [mk_track("c3")]})
    track, source = await resolve_surprise([], "auto", **_kw(client=fc))
    assert source == SOURCE_RANDOM
    assert not _has(fc.calls, "sonic")
    assert not _has(fc.calls, "similar_names")
    assert not _has(fc.calls, "get_tracks")


# ── Heuristic-only mode ────────────────────────────────────────────────────────

async def test_heuristic_only_never_calls_plex():
    """Covers AE4. Heuristic-only with a seed → heuristic, zero Plex similarity calls."""
    fc = FakeClient(genre_tracks={"Rock": [mk_track("c3")]})
    track, source = await resolve_surprise(SEED, "heuristic", **_kw(client=fc))
    assert source == SOURCE_HEURISTIC
    assert not _has(fc.calls, "sonic")
    assert not _has(fc.calls, "similar_names")


async def test_heuristic_only_no_seed_goes_random():
    """Covers AE4. Heuristic-only with no seed → random (no genre to seed from)."""
    fc = FakeClient()
    track, source = await resolve_surprise([], "heuristic", **_kw(client=fc))
    assert source == SOURCE_RANDOM


# ── Plex-only mode ──────────────────────────────────────────────────────────────

async def test_plex_only_degrades_straight_to_random_skipping_heuristic():
    fc = FakeClient(genre_tracks={"Rock": [mk_track("c3")]})  # heuristic data present...
    track, source = await resolve_surprise(SEED, "plex", **_kw(client=fc))
    assert source == SOURCE_RANDOM          # ...but plex-only never runs the heuristic
    assert not any(c[0] == "get_tracks" and c[3] is not None for c in fc.calls)


# ── Random mode ─────────────────────────────────────────────────────────────────

async def test_random_mode_ignores_seed_and_makes_no_plex_calls():
    fc = FakeClient(sonic={"s1": [mk_track("c1")]})
    track, source = await resolve_surprise(SEED, "random", **_kw(client=fc))
    assert source == SOURCE_RANDOM
    assert fc.calls == []


async def test_unknown_mode_treated_as_auto():
    fc = FakeClient(sonic={"s1": [mk_track("c1")]})
    track, source = await resolve_surprise(SEED, "bogus", **_kw(client=fc))
    assert source == SOURCE_PLEX_SONIC


# ── Exclusions + de-dup (R9) ────────────────────────────────────────────────────

async def test_excluded_artist_candidate_is_filtered():
    fc = FakeClient(sonic={"s1": [mk_track("bad", artist="Banned"),
                                   mk_track("ok", artist="Fine")]})
    track, source = await resolve_surprise(
        SEED, "auto", **_kw(client=fc, get_exclusions=make_excl(["Banned"])))
    assert source == SOURCE_PLEX_SONIC
    assert track.id == "ok"


async def test_seed_artist_is_excluded_from_results():
    """Don't suggest the same artist the user just queued."""
    fc = FakeClient(sonic={"s1": [mk_track("same", artist="Seed Act")]})
    track, source = await resolve_surprise(SEED, "auto", **_kw(client=fc))
    assert source == SOURCE_RANDOM   # the only sonic candidate was a seed artist


async def test_seed_track_id_not_re_suggested():
    fc = FakeClient(sonic={"s1": [mk_track("s1", artist="Other")]})
    track, source = await resolve_surprise(SEED, "auto", **_kw(client=fc))
    assert source == SOURCE_RANDOM   # the sonic candidate was the seed track itself


async def test_queue_duplicate_candidate_is_filtered():
    fc = FakeClient(sonic={"s1": [mk_track("dup", artist="Other")]})
    track, source = await resolve_surprise(
        SEED, "auto", **_kw(client=fc, queue=FakeQueue(dup_ids=["dup"])))
    assert source == SOURCE_RANDOM


# ── Empty library ───────────────────────────────────────────────────────────────

async def test_empty_library_returns_none():
    """Floor itself returns nothing (no enabled content) → (None, None)."""
    fc = FakeClient()
    track, source = await resolve_surprise(
        SEED, "auto", **_kw(client=fc, shuffle_provider=make_shuffle(None)))
    assert track is None


# ── Diversity gate + randomized selection (2026-06-17 plan 003 U1) ─────────────

async def test_album_mode_gates_already_queued_album():
    """Covers AE1. A candidate from an album already in the queue is rejected;
    a different-album candidate is chosen."""
    fc = FakeClient(sonic={"s1": [
        mk_track("c1", artist="B", album_id="alb1"),   # same album as queued q1
        mk_track("c2", artist="C", album_id="alb2"),
    ]})
    q = FakeQueue(queued=[mk_track("q1", artist="A", album_id="alb1")])
    track, source = await resolve_surprise(
        SEED, "auto", diversity="album", **_kw(client=fc, queue=q))
    assert source == SOURCE_PLEX_SONIC
    assert track.id == "c2"


async def test_artist_mode_gates_artist_in_history():
    """Covers AE2. A candidate by an artist already in recent history is rejected."""
    fc = FakeClient(sonic={"s1": [
        mk_track("c1", artist="Repeat", album_id="x"),
        mk_track("c2", artist="Fresh", album_id="y"),
    ]})
    q = FakeQueue(history=[mk_track("h1", artist="Repeat")])
    track, source = await resolve_surprise(
        SEED, "auto", diversity="artist", **_kw(client=fc, queue=q))
    assert track.id == "c2"


async def test_off_mode_does_not_gate():
    """Covers AE3. With diversity off, a same-album candidate is allowed (legacy)."""
    fc = FakeClient(sonic={"s1": [mk_track("c1", artist="A", album_id="alb1")]})
    q = FakeQueue(queued=[mk_track("q1", artist="A", album_id="alb1")])
    track, source = await resolve_surprise(
        SEED, "auto", diversity="off", **_kw(client=fc, queue=q))
    assert source == SOURCE_PLEX_SONIC
    assert track.id == "c1"


async def test_gate_includes_now_playing():
    """The now-playing track counts toward the represented window."""
    fc = FakeClient(sonic={"s1": [
        mk_track("c1", artist="NowArt"), mk_track("c2", artist="Other"),
    ]})
    q = FakeQueue(current=mk_track("np", artist="NowArt"))
    track, source = await resolve_surprise(
        SEED, "auto", diversity="artist", **_kw(client=fc, queue=q))
    assert track.id == "c2"


async def test_artist_gate_exhausted_degrades_to_floor():
    """Covers AE4. When the gate filters every smart candidate, the ungated
    random floor still returns a track (never dead-ends)."""
    fc = FakeClient(sonic={"s1": [mk_track("c1", artist="Gated")]})
    q = FakeQueue(history=[mk_track("h", artist="Gated")])
    floor = mk_track("rand", artist="Gated")  # floor is NOT gated
    track, source = await resolve_surprise(
        SEED, "auto", diversity="artist",
        **_kw(client=fc, queue=q, shuffle_provider=make_shuffle(floor)))
    assert source == SOURCE_RANDOM
    assert track.id == "rand"


async def test_randomized_selection_uses_choice():
    """Covers AE5. Among multiple acceptable candidates, selection goes through
    random.choice (not deterministic first-pick)."""
    from unittest.mock import patch
    fc = FakeClient(sonic={"s1": [mk_track("c1"), mk_track("c2")]})
    with patch("app.queue.surprise.random.choice", lambda seq: seq[-1]):
        track, source = await resolve_surprise(SEED, "auto", **_kw(client=fc))
    assert source == SOURCE_PLEX_SONIC
    assert track.id == "c2"


async def test_album_mode_candidate_without_album_id_not_gated():
    """A candidate with no album_id can't be album-matched → not gated by album."""
    fc = FakeClient(sonic={"s1": [mk_track("c1", artist="Z", album_id=None)]})
    q = FakeQueue(queued=[mk_track("q1", artist="A", album_id="alb1")])
    track, source = await resolve_surprise(
        SEED, "auto", diversity="album", **_kw(client=fc, queue=q))
    assert track.id == "c1"


# ── Plex-similarity randomness (2026-06-17 plan 005 U1) ───────────────────────

async def test_similar_artist_picks_random_track_not_first():
    """Covers AE1. The similar-artist path picks a RANDOM track from the chosen
    artist (patched random → last), not deterministically the first. The album
    sample is 1 (latency trim 2026-08-08), so this exercises random-pick WITHIN
    the sampled album rather than across albums."""
    from unittest.mock import patch
    fc = FakeClient(
        sonic={"s1": []},
        similar_names={"s1": ["Sim Act"]},
        artists={"1": [FakeArtist("a1", "Sim Act")]},
        albums={"a1": [FakeAlbum("al1")]},
        album_tracks={
            "al1": [mk_track("t1", artist="Sim Act"), mk_track("t2", artist="Sim Act"),
                    mk_track("t3", artist="Sim Act")],
        },
    )
    with patch("app.queue.surprise.random.choice", lambda seq: seq[-1]):
        track, source = await resolve_surprise(SEED, "auto", **_kw(client=fc))
    assert source == SOURCE_PLEX_SIMILAR
    # random.choice patched to last → t3, proving it is not pinned to the first track
    assert track.id == "t3"


async def test_similar_artist_single_track_returns_it():
    """An artist with one acceptable track returns it (no-op random)."""
    fc = FakeClient(
        similar_names={"s1": ["Solo"]},
        artists={"1": [FakeArtist("a1", "Solo")]},
        albums={"a1": [FakeAlbum("al1")]},
        album_tracks={"al1": [mk_track("only", artist="Solo")]},
    )
    track, source = await resolve_surprise(SEED, "auto", **_kw(client=fc))
    assert source == SOURCE_PLEX_SIMILAR and track.id == "only"


async def test_sonic_pool_is_widened():
    """try_sonic requests a wider neighbor pool so random.choice has more to draw
    from (2026-06-17 plan 005 U1)."""
    captured = []
    fc = FakeClient(sonic={"s1": [mk_track("c1")]})
    orig = fc.get_sonic_nearest
    async def spy(track_id, **kw):
        captured.append(kw)
        return await orig(track_id, **kw)
    fc.get_sonic_nearest = spy
    await resolve_surprise(SEED, "auto", **_kw(client=fc))
    assert captured and captured[0].get("limit", 10) >= 20


# ── Per-browser anti-repeat exclusions (2026-06-17 plan 005 U2) ───────────────

async def test_exclude_ids_filters_smart_sources():
    """Covers AE2/AE3. A candidate whose id is in exclude_ids is rejected across
    smart sources; an alternative is chosen."""
    fc = FakeClient(sonic={"s1": [mk_track("recent", artist="X"),
                                   mk_track("fresh", artist="Y")]})
    track, source = await resolve_surprise(
        SEED, "auto", exclude_ids={"recent"}, **_kw(client=fc))
    assert source == SOURCE_PLEX_SONIC and track.id == "fresh"


async def test_exclude_ids_exhausted_falls_to_floor():
    """Covers AE4. When exclude_ids removes all smart candidates, the floor (which
    is NOT excluded) still returns a track — never dead-ends."""
    fc = FakeClient(sonic={"s1": [mk_track("recent", artist="X")]})
    floor = mk_track("recent", artist="X")  # floor ignores exclude_ids
    track, source = await resolve_surprise(
        SEED, "auto", exclude_ids={"recent"},
        **_kw(client=fc, shuffle_provider=make_shuffle(floor)))
    assert source == SOURCE_RANDOM and track.id == "recent"


async def test_exclude_ids_default_none_is_noop():
    """No exclude_ids → behaves as before."""
    fc = FakeClient(sonic={"s1": [mk_track("c1")]})
    track, source = await resolve_surprise(SEED, "auto", **_kw(client=fc))
    assert source == SOURCE_PLEX_SONIC and track.id == "c1"


# ── Speedup: parallel fan-out (2026-06-17 ce-debug) ───────────────────────────

async def test_similar_fanout_runs_concurrently():
    """The similar fallback issues its Plex calls concurrently (bounded by the
    client semaphore in prod), not one-at-a-time — the latency fix."""
    import asyncio as _aio

    class ConcClient:
        def __init__(self):
            self.inflight = 0
            self.peak = 0
        async def _t(self):
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
            await _aio.sleep(0.005)
            self.inflight -= 1
        async def get_sonic_nearest(self, tid, **kw):
            await self._t()
            return []
        async def get_artist_similar_names(self, tid):
            await self._t()
            return [f"Sim{i}" for i in range(6)]
        async def get_artists(self, sk):
            await self._t()
            return [FakeArtist(f"a{i}", f"Sim{i}") for i in range(6)]
        async def get_albums(self, sk, artist_id=None, **kw):
            await self._t()
            return [FakeAlbum(f"{artist_id}-b{j}") for j in range(2)]
        async def get_tracks(self, sk, album_id=None, genre=None, **kw):
            await self._t()
            return [mk_track(f"{album_id}-t", artist="X")] if album_id else []

    c = ConcClient()
    track, source = await resolve_surprise(SEED, "auto", **_kw(client=c))
    assert source == SOURCE_PLEX_SIMILAR
    assert c.peak > 1, "similar fan-out must run concurrently (sequential → peak==1)"


# ── Source fail-soft: Plex slowness degrades, never 500s (2026-06-18 ce-debug) ─
# A slow Plex server raised httpx.ReadTimeout inside _artist_random_track's
# get_albums; the unguarded asyncio.gather re-raised it through try_similar and
# the chain loop → 500, and the chain never reached heuristic/random. Two layers
# now prevent that: return_exceptions in the sonic/similar fan-outs (partial
# slowness still queues) + a chain-level guard (any source raising degrades).

async def test_similar_artist_failsoft_when_get_albums_raises():
    """The reported bug: a Plex timeout in _artist_random_track (get_albums) must
    not 500. return_exceptions in the similar fan-out swallows it, the source
    yields nothing, and the chain degrades to the heuristic."""
    class BoomAlbums(FakeClient):
        async def get_albums(self, section_key, artist_id=None, **kw):
            raise RuntimeError("plex read timeout")
    fc = BoomAlbums(
        sonic={"s1": []},
        similar_names={"s1": ["Sim Act"]},
        artists={"1": [FakeArtist("a1", "Sim Act")]},
        genre_tracks={"Rock": [mk_track("c3", artist="Y")]},
    )
    track, source = await resolve_surprise(SEED, "auto", **_kw(client=fc))
    assert source == SOURCE_HEURISTIC
    assert track.id == "c3"


async def test_similar_artist_partial_failure_still_queues():
    """One matched artist's get_albums times out; the other resolves. The source
    still returns a track from the survivor (return_exceptions) rather than
    aborting — the 'no longer queues any tracks' half of the bug."""
    class PartialBoom(FakeClient):
        async def get_albums(self, section_key, artist_id=None, **kw):
            if artist_id == "a1":
                raise RuntimeError("plex read timeout")
            return await super().get_albums(section_key, artist_id=artist_id, **kw)
    fc = PartialBoom(
        sonic={"s1": []},
        similar_names={"s1": ["Bad Act", "Good Act"]},
        artists={"1": [FakeArtist("a1", "Bad Act"), FakeArtist("a2", "Good Act")]},
        albums={"a2": [FakeAlbum("al2")]},
        album_tracks={"al2": [mk_track("good", artist="Good Act")]},
    )
    track, source = await resolve_surprise(SEED, "auto", **_kw(client=fc))
    assert source == SOURCE_PLEX_SIMILAR
    assert track.id == "good"


async def test_sonic_partial_failure_still_queues():
    """One seed's /nearest lookup times out; the other resolves. try_sonic still
    builds candidates from the survivor (return_exceptions)."""
    class PartialSonic(FakeClient):
        async def get_sonic_nearest(self, track_id, **kw):
            if track_id == "bad":
                raise RuntimeError("plex read timeout")
            return await super().get_sonic_nearest(track_id, **kw)
    seed = [{"track_id": "bad", "genre": "Rock", "artist": "A"},
            {"track_id": "good", "genre": "Rock", "artist": "A"}]
    fc = PartialSonic(sonic={"good": [mk_track("c1", artist="X")]})
    track, source = await resolve_surprise(seed, "auto", **_kw(client=fc))
    assert source == SOURCE_PLEX_SONIC
    assert track.id == "c1"


async def test_chain_level_failsoft_when_source_raises_unexpectedly():
    """Belt-and-suspenders: a failure outside the per-call guards (here the
    enabled-libraries lookup raising) is caught at the chain loop, so the press
    still degrades to the random floor instead of 500-ing."""
    async def boom_libs():
        raise RuntimeError("db unavailable")
    fc = FakeClient(sonic={"s1": []})
    floor = mk_track("rand", artist="Floor")
    track, source = await resolve_surprise(
        SEED, "auto",
        **_kw(client=fc, get_enabled_libraries=boom_libs,
              shuffle_provider=make_shuffle(floor)))
    assert source == SOURCE_RANDOM
    assert track.id == "rand"


# ── Random-pick length band (2026-06-20 plan U2) ──────────────────────────────


class _Dur:
    """Minimal duration-only stand-in for the predicate's duck typing."""
    def __init__(self, dur_ms):
        self.duration_ms = dur_ms


def test_within_length_no_bounds_passes():
    assert _within_length(_Dur(1), None, None) is True
    assert _within_length(_Dur(10_000_000), None, None) is True


def test_within_length_excludes_too_short_and_too_long():
    assert _within_length(_Dur(3000), 30000, 600000) is False        # 3s < 30s
    assert _within_length(_Dur(2_000_000), 30000, 600000) is False   # 2000s > 600s


def test_within_length_inclusive_boundary():
    # Copy: exclude *shorter than* min / *longer than* max → exactly min/max kept.
    assert _within_length(_Dur(30000), 30000, 600000) is True
    assert _within_length(_Dur(600000), 30000, 600000) is True
    assert _within_length(_Dur(200000), 30000, 600000) is True


def test_within_length_unknown_duration_passes():
    assert _within_length(_Dur(0), 30000, 600000) is True
    assert _within_length(_Dur(None), 30000, 600000) is True


def test_within_length_min_only_and_max_only():
    assert _within_length(_Dur(3000), 30000, None) is False
    assert _within_length(_Dur(900000), 30000, None) is True
    assert _within_length(_Dur(900000), None, 600000) is False
    assert _within_length(_Dur(3000), None, 600000) is True


async def test_resolve_surprise_filters_out_of_band_smart_source():
    """A band drops out-of-band sonic candidates; an in-band one is chosen."""
    short = mk_track("short", artist="X", dur_ms=3000)
    good = mk_track("good", artist="X", dur_ms=200000)
    long_ = mk_track("long", artist="X", dur_ms=2_000_000)
    fc = FakeClient(sonic={"s1": [short, good, long_]})
    track, source = await resolve_surprise(
        SEED, "plex", **_kw(client=fc), length_bounds=(30000, 600000))
    assert source == SOURCE_PLEX_SONIC
    assert track.id == "good"


async def test_resolve_surprise_no_bounds_unchanged():
    """Default length_bounds (None, None) selects exactly as before (regression)."""
    fc = FakeClient(sonic={"s1": [mk_track("c1", artist="X", dur_ms=5)]})
    track, source = await resolve_surprise(SEED, "plex", **_kw(client=fc))
    assert source == SOURCE_PLEX_SONIC
    assert track.id == "c1"  # a tiny track is still chosen when no band is set


async def test_resolve_surprise_band_excludes_all_in_source_falls_through():
    """When every candidate in a smart source is out-of-band, the chain degrades
    to the random floor (which resolve_surprise does not band-filter — U3's real
    _shuffle_provider owns the floor's own filtering + never-dead-end fallback)."""
    fc = FakeClient(sonic={"s1": [mk_track("toolong", artist="X", dur_ms=9_000_000)]})
    floor = mk_track("rand", artist="Floor")
    track, source = await resolve_surprise(
        SEED, "plex",
        **_kw(client=fc, shuffle_provider=make_shuffle(floor)),
        length_bounds=(30000, 600000))
    assert source == SOURCE_RANDOM
    assert track.id == "rand"


# ── Plexplayer source-lock gate (2026-08-04-002 plan U8, R11) ─────────────────
# The acceptable() closure gates the smart sources; the floor is constrained at
# its call site with a bounded re-roll and a give-up (the CONSCIOUS inversion of
# never-dead-end for a hard playability constraint). lock getter None ⇒ inert.

from app.queue.surprise import _PLEX_LOCK_FLOOR_TRIES  # noqa: E402


def plexable(t, sid="m1"):
    """Stamp *t* with one enabled-Plex-shaped hold."""
    t.holds = [{"source_id": sid, "key": f"{sid}:{t.id}"}]
    return t


def make_lock(ids):
    async def _l():
        return ids
    return _l


def make_notify():
    calls = []

    async def _n():
        calls.append(1)
    _n.calls = calls
    return _n


def make_shuffle_seq(tracks):
    """Floor returning a scripted sequence; repeats the last entry forever."""
    seq = list(tracks)
    calls = []

    async def _s():
        calls.append(1)
        return seq.pop(0) if len(seq) > 1 else seq[0]
    _s.calls = calls
    return _s


async def test_lock_smart_source_filters_unplayable_candidates():
    """Mixed sonic candidates + lock active: only the enabled-Plex-held one
    can win; a Jellyfin-held and a hold-less candidate are both rejected
    (fail closed) inside acceptable()."""
    jelly = mk_track("cj", artist="X")
    jelly.holds = [{"source_id": "jelly", "key": "jelly:cj"}]
    bare = mk_track("cb", artist="X")          # no holds at all
    good = plexable(mk_track("cp", artist="X"))
    fc = FakeClient(sonic={"s1": [jelly, bare, good]})
    track, source = await resolve_surprise(
        SEED, "plex", **_kw(client=fc),
        get_plex_lock_ids=make_lock({"m1"}), notify_lock_giveup=make_notify())
    assert source == SOURCE_PLEX_SONIC
    assert track.id == "cp"


async def test_lock_native_candidate_attributes_via_compound_id():
    """Review fix PLX-8: native smart-source candidates (sonic/similar/
    heuristic on an all-Plex install) carry NO holds — their compound id
    "{machine_id}:{ratingKey}" attributes them to the owning server, so a
    candidate from an ENABLED server passes instead of failing closed and
    starving the smart chain. A foreign-server id and an unattributable
    bare id still fail closed."""
    native_bad = mk_track("m9:44", artist="X")     # disabled/unknown server
    bare = mk_track("cb", artist="X")              # no holds, no prefix
    native_good = mk_track("m1:101", artist="X")   # enabled server
    fc = FakeClient(sonic={"s1": [native_bad, bare, native_good]})
    track, source = await resolve_surprise(
        SEED, "plex", **_kw(client=fc),
        get_plex_lock_ids=make_lock({"m1"}), notify_lock_giveup=make_notify())
    assert source == SOURCE_PLEX_SONIC
    assert track.id == "m1:101"


async def test_lock_floor_rerolls_to_playable():
    """The floor call site re-rolls an unplayable pick and returns the first
    Plex-playable one."""
    bad = mk_track("bad", artist="F")
    bad.holds = [{"source_id": "jelly", "key": "jelly:bad"}]
    good = plexable(mk_track("good", artist="F"))
    shuffle = make_shuffle_seq([bad, good])
    track, source = await resolve_surprise(
        SEED, "random", **_kw(client=FakeClient(), shuffle_provider=shuffle),
        get_plex_lock_ids=make_lock({"m1"}), notify_lock_giveup=make_notify())
    assert source == SOURCE_RANDOM
    assert track.id == "good"
    assert len(shuffle.calls) == 2


async def test_lock_floor_gives_up_bounded_and_notifies_once():
    """Zero Plex-playable candidates: the floor stops at the bounded cap
    (no infinite loop), returns no pick, and emits the give-up notice once."""
    bad = mk_track("bad", artist="F")
    bad.holds = [{"source_id": "jelly", "key": "jelly:bad"}]
    shuffle = make_shuffle_seq([bad])
    notify = make_notify()
    track, source = await resolve_surprise(
        SEED, "random", **_kw(client=FakeClient(), shuffle_provider=shuffle),
        get_plex_lock_ids=make_lock({"m1"}), notify_lock_giveup=notify)
    assert (track, source) == (None, None)
    assert len(shuffle.calls) == _PLEX_LOCK_FLOOR_TRIES
    assert len(notify.calls) == 1


async def test_lock_floor_empty_ids_set_rejects_everything():
    """Every-Plex-source-vetoed edge: an EMPTY (non-None) id set keeps the gate
    active and no holder can qualify — give-up, not inert."""
    held = plexable(mk_track("held", artist="F"))
    notify = make_notify()
    track, source = await resolve_surprise(
        SEED, "random",
        **_kw(client=FakeClient(), shuffle_provider=make_shuffle(held)),
        get_plex_lock_ids=make_lock(set()), notify_lock_giveup=notify)
    assert (track, source) == (None, None)
    assert len(notify.calls) == 1


async def test_lock_floor_empty_library_stays_quiet():
    """An empty floor (None on the first draw) is the PRE-EXISTING no-pick
    condition, not a lock give-up — no notice."""
    notify = make_notify()
    track, source = await resolve_surprise(
        SEED, "random",
        **_kw(client=FakeClient(), shuffle_provider=make_shuffle(None)),
        get_plex_lock_ids=make_lock({"m1"}), notify_lock_giveup=notify)
    assert (track, source) == (None, None)
    assert notify.calls == []


async def test_lock_inert_other_backend_byte_identical():
    """Filter inert (getter returns None): a hold-less floor pick is returned
    on the FIRST draw — no re-roll, no playability read, no notice — exactly
    the pre-U8 behavior."""
    bare = mk_track("bare", artist="F")        # no holds; would fail the gate
    shuffle = make_shuffle_seq([bare])
    notify = make_notify()
    track, source = await resolve_surprise(
        SEED, "random", **_kw(client=FakeClient(), shuffle_provider=shuffle),
        get_plex_lock_ids=make_lock(None), notify_lock_giveup=notify)
    assert source == SOURCE_RANDOM
    assert track.id == "bare"
    assert len(shuffle.calls) == 1
    assert notify.calls == []


async def test_lock_default_getter_is_inert_off_plexplayer():
    """Composition guard: with NO injected lock getter the default
    (app.state.plex_lock_enabled_ids) reads the persisted-selection mirror —
    'direct' ⇒ inert, hold-less picks flow exactly as before."""
    import app.state as st
    from unittest.mock import patch
    bare = mk_track("bare", artist="F")
    with patch.object(st, "_selected_output_backend", "direct"):
        track, source = await resolve_surprise(
            SEED, "random",
            **_kw(client=FakeClient(), shuffle_provider=make_shuffle(bare)))
    assert source == SOURCE_RANDOM and track.id == "bare"
