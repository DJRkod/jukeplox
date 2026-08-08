"""Surprise Me — suggestion resolver (2026-06-17 plan U2).

Resolves ONE track to enqueue, seeded from the pressing browser's own picks,
walking an elegant degradation chain so a press never dead-ends:

    Plex sonic similarity → Plex metadata similar-artist → genre/artist
    heuristic → pure-random floor.

The admin source mode selects which sources are in play *above the universal
random floor*; every mode degrades down to it. A press with no seed resolves to
random in every mode. The function is pure orchestration over injected
dependencies (Plex client, queue, random floor, exclusions, enabled libraries),
which keeps it unit-testable without Plex or the DB.
"""

import asyncio
import logging
import random
import time
from collections import deque

from app.catalog.views import holds_plex_held

_log = logging.getLogger(__name__)

# Bounded re-roll budget for the random floor while the plexplayer source lock
# is active (2026-08-04-002 plan U8): the floor draws whole-catalog randoms, so
# each try is an independent shot at a Plex-playable pick; past the budget the
# resolve gives up (see the CONSCIOUS INVERSION note at the floor call site).
_PLEX_LOCK_FLOOR_TRIES = 8

# Never-dead-end floor resilience (2026-08-08 latency pass): a transient error on
# a single floor draw (e.g. a Plex ReadTimeout, or the shared httpx pool briefly
# degraded after a slow smart source) must NOT abandon the floor and queue nothing
# while the catalog has content — retry a few draws before giving up.
_FLOOR_ERROR_RETRIES = 3

SOURCE_PLEX_SONIC = "plex_sonic"
SOURCE_PLEX_SIMILAR = "plex_similar_artist"
SOURCE_HEURISTIC = "heuristic"
SOURCE_RANDOM = "random"

# Which sources are enabled, in priority order, per admin source mode. Random is
# the universal floor and terminates every chain, so a press always resolves.
_CHAINS = {
    "auto": ("sonic", "similar", "heuristic", "random"),
    "plex": ("sonic", "similar", "random"),
    "heuristic": ("heuristic", "random"),
    "random": ("random",),
}


def _norm(s: str | None) -> str:
    return (s or "").strip().lower()


def _plex_lock_ok(t, lock_ids) -> bool:
    """Per-candidate plexplayer source-lock check (review fix PLX-8).

    ``lock_ids is None`` ⇒ gate inert. Otherwise a candidate qualifies when
    its loaded holds name an enabled Plex source (the catalog-floor shape) —
    OR, for hold-less candidates, when its compound id attributes to one:
    the smart sources (sonic/similar/heuristic) return NATIVE tracks whose
    ids are ``"{machine_id}:{ratingKey}"`` and which carry no holds, so the
    holds-only check failed every native candidate closed and starved the
    smart chain down to the floor. Fails closed only when NEITHER holds nor
    the id prefix attribute the candidate (the same compound-key
    attribution ``state._holder_keys`` uses for bare stream keys)."""
    if lock_ids is None:
        return True
    if holds_plex_held(getattr(t, "holds", None), lock_ids):
        return True
    tid = str(getattr(t, "id", "") or "")
    machine_id, sep, _rest = tid.partition(":")
    return bool(sep and machine_id in lock_ids)


def _within_length(t, min_ms: int | None, max_ms: int | None) -> bool:
    """True when track ``t``'s duration falls within the [min_ms, max_ms] band.

    Random-pick length filter (2026-06-20 plan U2). The boundary is INCLUSIVE —
    the admin copy excludes tracks *shorter than* min / *longer than* max, so a
    track exactly at min or max is kept. A missing/zero duration passes (we never
    silently drop a track whose length we can't read). Both bounds ``None`` →
    always True (filter off; zero overhead on the default path).
    """
    if min_ms is None and max_ms is None:
        return True
    dur = getattr(t, "duration_ms", 0) or 0
    if not dur:
        return True
    if min_ms is not None and dur < min_ms:
        return False
    if max_ms is not None and dur > max_ms:
        return False
    return True


def _diversity_index(queue, seed, diversity):
    """(represented_artists, represented_albums) for the diversity gate.

    The recent window = now-playing + upcoming queue + in-memory play history
    (read defensively so a minimal fake queue yields empty sets) + the seed's
    own artists. Returns empty sets when diversity is off.
    """
    artists: set[str] = set()
    albums: set[str] = set()
    if diversity == "off":
        return artists, albums
    items = list(getattr(queue, "queue", []) or []) + list(getattr(queue, "history", []) or [])
    state = getattr(queue, "state", None)
    current = getattr(state, "current", None) if state is not None else None
    if current is not None:
        items.append(current)
    for it in items:
        tr = getattr(it, "track", None)
        if tr is None:
            continue
        artists.add(_norm(getattr(tr, "artist", "")))
        alb = getattr(tr, "album_id", None)
        if alb:
            albums.add(alb)
    for p in seed:
        if p.get("artist"):
            artists.add(_norm(p["artist"]))
    return artists, albums


# ── source attribution (dev observability, plan U6) ──────────────────────────
# A process-local ring of the most recent resolved sources. The enqueue endpoint
# records into it; the admin readout reads it. Resets on restart (acceptable for
# diagnosis — persistence is deferred). Guests never see this.
_RECENT_SOURCES: deque = deque(maxlen=20)


def record_source(source: str) -> None:
    _RECENT_SOURCES.append({"source": source, "ts": time.time()})


def recent_sources() -> list[dict]:
    return list(_RECENT_SOURCES)


def recent_source_tally() -> dict[str, int]:
    tally: dict[str, int] = {}
    for entry in _RECENT_SOURCES:
        tally[entry["source"]] = tally.get(entry["source"], 0) + 1
    return tally


async def resolve_surprise(
    seed,
    mode,
    *,
    client,
    queue,
    diversity: str = "off",
    exclude_ids=None,
    length_bounds: tuple[int | None, int | None] = (None, None),
    shuffle_provider=None,
    get_exclusions=None,
    get_enabled_libraries=None,
    sonic_seed_tries: int = 2,
    similar_name_tries: int = 6,
    get_plex_lock_ids=None,
    notify_lock_giveup=None,
    resolve_budget_s: float = 8.0,
):
    """Return ``(track, source_label)`` for one Surprise pick.

    ``seed`` is the browser's own picks: ``[{track_id, genre, artist}]``.
    ``mode`` is one of ``auto`` / ``plex`` / ``heuristic`` / ``random`` (unknown
    → ``auto``). Returns ``(None, None)`` only when even the random floor finds
    nothing (no enabled library content). Dependencies default to the real
    implementations but are injectable for testing.
    """
    seed = seed or []
    chain = _CHAINS.get(mode, _CHAINS["auto"])

    if shuffle_provider is None:
        from app.state import _shuffle_provider as shuffle_provider  # noqa: F811
    if get_exclusions is None:
        from app.database import get_artist_exclusions as get_exclusions  # noqa: F811
    if get_enabled_libraries is None:
        # Effective = enabled minus vetoed sources, so Surprise's native similar
        # path can't pick a whole-source-OFF library (Libraries-panel U2).
        from app.database import get_effective_enabled_libraries as get_enabled_libraries  # noqa: F811
    if get_plex_lock_ids is None:
        from app.state import plex_lock_enabled_ids as get_plex_lock_ids  # noqa: F811
    if notify_lock_giveup is None:
        from app.state import notify_plex_lock_giveup as notify_lock_giveup  # noqa: F811

    # Wall-clock budget clock — started HERE, before the first await, so the
    # pre-resolve prefetches (get_exclusions, get_plex_lock_ids) count against the
    # budget too. Under DB contention those reads can stall; starting the clock at
    # entry keeps the total press latency honestly bounded rather than letting an
    # unbudgeted prefetch erode the smart-source window silently. See the chain
    # loop below for how the budget gates the smart sources.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, resolve_budget_s)

    def _remaining() -> float:
        return deadline - loop.time()

    # Anti-repeat (plan 005): the browser's recently-surprised track ids, excluded
    # from the smart sources so remove + re-press won't repeat. The floor ignores
    # this (it is the guaranteed never-dead-end fallback).
    exclude_ids = set(exclude_ids or ())
    seed_ids = {p.get("track_id") for p in seed if p.get("track_id")}
    seed_artists = {_norm(p.get("artist")) for p in seed if p.get("artist")}
    seed_genres: list[str] = []
    for p in seed:
        g = p.get("genre")
        if g and g not in seed_genres:
            seed_genres.append(g)

    # Excluded artists = the global admin exclusions ∪ the seed's own artists
    # (don't suggest the same act the user just queued).
    excl = {_norm(e) for e in (await get_exclusions()) if _norm(e)} | seed_artists

    # Diversity gate (plan 003): reject candidates whose artist (artist mode) or
    # album (album mode) is already represented in the recent window.
    gate_artists, gate_albums = _diversity_index(queue, seed, diversity)

    # Random-pick length band (plan U2): applies to every smart source. The floor
    # (shuffle_provider) is NOT filtered here — the real _shuffle_provider owns
    # its own band filtering + never-dead-end fallback (plan U3).
    min_ms, max_ms = length_bounds or (None, None)

    # Plexplayer source lock (2026-08-04-002 plan U8, R11): the enabled-Plex-
    # source ids, prefetched ONCE per resolve — async, before the sync
    # acceptable() loop, exactly how the exclusions/band inputs above are
    # gathered — so the per-candidate check below is a pure set lookup over the
    # candidate's already-loaded holds. ``None`` ⇒ gate inert (another backend
    # selected, or the native all-Plex path where every candidate is Plex-backed
    # by construction); a set (possibly empty) ⇒ only candidates holding a copy
    # on an enabled Plex source may be picked.
    lock_ids = await get_plex_lock_ids()

    def acceptable(t) -> bool:
        if t is None or t.id in seed_ids or t.id in exclude_ids:
            return False
        if not _within_length(t, min_ms, max_ms):
            return False
        if not _plex_lock_ok(t, lock_ids):
            # U8: the selected output can only play Plex-held tracks. A
            # candidate neither holds nor id-prefix can attribute to an
            # enabled Plex source fails CLOSED: suggesting it would strand
            # an unplayable track behind the U5 enqueue gate. (PLX-8:
            # native smart-source candidates attribute via their compound
            # id — see _plex_lock_ok.)
            return False
        if _norm(getattr(t, "artist", "")) in excl:
            return False
        if queue.is_duplicate(t.id):
            return False
        if diversity == "artist" and _norm(getattr(t, "artist", "")) in gate_artists:
            return False
        if diversity == "album":
            alb = getattr(t, "album_id", None)
            if alb and alb in gate_albums:
                return False
        return True

    async def try_sonic():
        if not seed:
            return None
        # Cap the seed tracks used (B): /nearest already returns a wide neighbor
        # set, so 1-2 seeds give a varied random pick while bounding the fan-out.
        tids = [p["track_id"] for p in seed[:sonic_seed_tries] if p.get("track_id")]
        if not tids:
            return None
        # Issue the per-seed /nearest calls concurrently (A); the client's
        # per-server semaphore still bounds real concurrency. return_exceptions
        # so one slow/timed-out seed doesn't torpedo the whole source — we still
        # build candidates from the seeds that did resolve (fail-soft, plan
        # 2026-06-18 surprise source resilience).
        results = await asyncio.gather(
            *(client.get_sonic_nearest(t, limit=25) for t in tids),
            return_exceptions=True,
        )
        seen, cands = set(), []
        for lst in results:
            if isinstance(lst, BaseException):
                _log.warning("surprise sonic: per-seed lookup failed: %r", lst)
                continue
            for cand in lst:
                if acceptable(cand) and cand.id not in seen:
                    seen.add(cand.id)
                    cands.append(cand)
        return random.choice(cands) if cands else None

    async def try_similar():
        if not seed:
            return None
        libs = await get_enabled_libraries()
        tids = [p["track_id"] for p in seed[:sonic_seed_tries] if p.get("track_id")]
        if not tids:
            return None
        # Fetch similar-artist names for the (capped) seed tracks concurrently (A).
        # return_exceptions so a single slow seed degrades to the names that did
        # resolve instead of aborting the source (fail-soft).
        name_lists = await asyncio.gather(
            *(client.get_artist_similar_names(t) for t in tids),
            return_exceptions=True,
        )
        names, seen_names = [], set()
        for lst in name_lists:
            if isinstance(lst, BaseException):
                _log.warning("surprise similar: similar-names lookup failed: %r", lst)
                continue
            for n in lst:
                nn = _norm(n)
                if nn and nn not in seen_names:
                    seen_names.add(nn)
                    names.append(nn)
            if len(names) >= similar_name_tries:
                break
        names = names[:similar_name_tries]  # cap total names resolved (B)
        # Pre-fetch each library's artists ONCE (cached) and index by normalized
        # name — avoids a get_artists stampede when resolving names concurrently.
        index: dict = {}
        for lib in libs:
            sk = lib.get("section_key")
            if not sk:
                continue
            # Fail-soft per library (matching try_heuristic): one unavailable
            # library must not abort the source — skip it and index the rest.
            try:
                artists = await client.get_artists(sk)
            except Exception as e:
                _log.warning("surprise similar: get_artists failed for %s: %r", sk, e)
                continue
            for a in artists:
                index.setdefault(_norm(getattr(a, "title", "")), (sk, a))
        targets = [index[n] for n in names if n in index]
        # Resolve each matched artist to a random track in parallel (A).
        # return_exceptions so one artist's slow Plex lookup degrades to the
        # artists that did resolve rather than 500-ing the press (fail-soft).
        resolved = await asyncio.gather(
            *(_artist_random_track(client, sk, a, acceptable) for sk, a in targets),
            return_exceptions=True,
        )
        seen, cands = set(), []
        for t in resolved:
            if isinstance(t, BaseException):
                _log.warning("surprise similar: artist track resolve failed: %r", t)
                continue
            if t is not None and t.id not in seen:
                seen.add(t.id)
                cands.append(t)
        return random.choice(cands) if cands else None

    async def try_heuristic():
        if not seed_genres:
            return None
        libs = await get_enabled_libraries()
        genres = list(seed_genres)
        random.shuffle(genres)
        for genre in genres:
            cands = []
            for lib in libs:
                sk = lib.get("section_key")
                if not sk:
                    continue
                # Fail-soft per library, matching the sonic/similar sources: a
                # single unavailable library must not 500 the public surprise
                # endpoint (code-review #4). Skip it and try the rest.
                try:
                    tracks = await client.get_tracks(sk, genre=genre)
                except Exception:
                    continue
                for t in tracks:
                    if acceptable(t):
                        cands.append(t)
            if cands:
                return random.choice(cands)
        return None

    # Chain-level fail-soft: a source raising (e.g. a Plex httpx.ReadTimeout in
    # try_similar/try_sonic) must degrade to the next source / random floor, never
    # 500 the public endpoint. The intra-source guards above already keep partial
    # slowness from killing a source; this loop is the comprehensive safety net so
    # any unanticipated failure still falls through to heuristic → random.
    #
    # Wall-clock budget (latency guard): the smart sources issue many Plex round
    # trips through a bounded per-server semaphore, and a stalled read can burn the
    # full 15s httpx timeout — the field-reported 15-30s press. The budget gates
    # STARTING a smart source: once the deadline has passed we skip the remaining
    # smart/heuristic sources (all Plex-bound) and fall to the random floor.
    #
    # We deliberately do NOT wait_for/cancel a source mid-flight. Cancelling an
    # in-flight httpx request interrupts it at an arbitrary await and leaves the
    # shared Plex connection pool degraded, so the very next consumer — the random
    # floor — then stalls on a poisoned connection for the full 15s and could even
    # fail (hardware-observed: an 8s cancel followed by a 15s floor ReadTimeout →
    # a ~23s press that queued NOTHING). Bounding by starting-gate + trimmed
    # fan-out (see similar_name_tries / albums_sample) keeps the common case fast
    # without that failure mode; a single started source is bounded by its own
    # trimmed work and the per-call 15s httpx ceiling. The floor is NEVER budgeted
    # (R4) and now tolerates a transient draw error (see below). The budget clock
    # (loop/deadline/_remaining) starts at function entry above, so these smart
    # sources see whatever budget the pre-resolve prefetches left.
    _SMART_SOURCES = {"sonic", "similar", "heuristic"}
    _SMART_FNS = {"sonic": try_sonic, "similar": try_similar, "heuristic": try_heuristic}
    _SMART_LABELS = {"sonic": SOURCE_PLEX_SONIC, "similar": SOURCE_PLEX_SIMILAR,
                     "heuristic": SOURCE_HEURISTIC}
    for source in chain:
        try:
            if source in _SMART_SOURCES:
                if _remaining() <= 0:
                    _log.info("surprise: resolve budget (%.1fs) spent — skipping "
                              "%r, degrading toward the random floor",
                              resolve_budget_s, source)
                    continue
                t = await _SMART_FNS[source]()
                if t is not None:
                    return t, _SMART_LABELS[source]
            elif source == "random":
                if lock_ids is None:
                    # Never-dead-end floor: retry a few draws so one transient
                    # error doesn't abandon the floor and queue nothing.
                    for _ in range(_FLOOR_ERROR_RETRIES):
                        try:
                            t = await shuffle_provider()
                        except Exception as e:
                            _log.warning("surprise floor draw failed, retrying: %r", e)
                            continue
                        if t is not None:
                            return t, SOURCE_RANDOM
                        break  # provider returned None → library genuinely empty
                    return None, None
                else:
                    # CONSCIOUS INVERSION of the never-dead-end floor rule
                    # (2026-08-04-002 plan U8, R11). docs/solutions/design-
                    # patterns/constrained-random-selection-filter-chokepoints-
                    # exempt-explicit-never-dead-end.md mandates a final
                    # UNFILTERED pick so a constrained floor never dead-ends —
                    # for VALUE constraints (length band, diversity), where an
                    # out-of-band track still plays. The plexplayer source lock
                    # is a HARD playability constraint: an unfiltered pick
                    # would hand the queue a track the selected output cannot
                    # play at all. For THIS backend only, the floor re-rolls a
                    # bounded number of times and then gives up — no pick, one
                    # debounced admin notice, the queue simply doesn't refill.
                    saw_candidate = False
                    for _ in range(_PLEX_LOCK_FLOOR_TRIES):
                        try:
                            t = await shuffle_provider()
                        except Exception as e:
                            # A transient draw error is not a dead library — try
                            # the next re-roll rather than abandoning the floor.
                            _log.warning("surprise floor draw failed, retrying: %r", e)
                            continue
                        if t is None:
                            break  # library genuinely empty — the pre-existing
                            #        no-pick condition, not a lock give-up
                        saw_candidate = True
                        if _plex_lock_ok(t, lock_ids):
                            return t, SOURCE_RANDOM
                    if saw_candidate:
                        await notify_lock_giveup()
                    return None, None
        except Exception as e:
            _log.warning("surprise source %r failed, degrading: %r", source, e)
            continue

    return None, None


async def _artist_random_track(client, sk, artist, acceptable, albums_sample: int = 1):
    """Resolve an already-matched local artist to a RANDOM acceptable track,
    cheaply: sample a couple of the artist's albums and fetch their tracks in
    parallel (speedup A/E), instead of scanning every album sequentially. Picking
    a random track (across a random album sample) keeps a chosen artist from being
    pinned to their first track. Returns None when the artist has no acceptable
    track."""
    albums = await client.get_albums(sk, artist_id=artist.id)
    if not albums:
        return None
    sample = albums if len(albums) <= albums_sample else random.sample(albums, albums_sample)
    track_lists = await asyncio.gather(*(client.get_tracks(sk, album_id=al.id) for al in sample))
    cands = [t for lst in track_lists for t in lst if acceptable(t)]
    return random.choice(cands) if cands else None
