"""App-wide singletons: QueueEngine, OutputRouter, backend instances, PlexClient."""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import random
import urllib.parse
from datetime import datetime, timezone

_log = logging.getLogger(__name__)

# Freshness window for the genre/credit caches (gentle-on-Plex, 2026-06-14
# plan U2/U3). A library rarely changes mid-party; an admin rescan force-
# refreshes regardless, so a long window is safe and keeps Jukeplox off a
# shared Plex server during normal browsing.
CACHE_TTL_S = 6 * 60 * 60
_MAX_CONSECUTIVE_FAILURES = 50
# Random-pick length band (2026-06-20 plan U3): when an admin min/max is set, how
# many random traversals to try for an in-band track before the never-dead-end
# last-resort unfiltered pick. Bounds Plex load; tunable, not a contract.
_SHUFFLE_BAND_TRIES = 25
# Popular Random candidate-resolution cap (2026-06-21 plan U4): how many distinct
# popular-track ids to try resolving (for a playable stream key) before giving up
# and letting the caller fall back to Full Random. Bounds Plex load per advance.
_POPULAR_RESOLVE_TRIES = 8
# Sentinel for _shuffle_provider's optional band arg: distinguishes "not passed
# → fetch the admin band" (the Surprise Me floor's no-arg call) from an explicit
# (None, None) "no band" passed by the queue-end caller.
_UNSET = object()


def _log_task_exc(task: asyncio.Task) -> None:
    if not task.cancelled():
        exc = task.exception()
        if exc:
            _log.error("Background task raised: %s", exc, exc_info=exc)


def is_authorized_stream_key(key: str) -> bool:
    """Return True only if key matches a track that Jukeplox loaded into the queue."""
    items = list(queue_engine.queue) + list(queue_engine.history)
    if queue_engine.state.current:
        items.append(queue_engine.state.current)
    return any(item.track.stream_key == key for item in items)


from app.output.router import OutputRouter
from app.output.direct import DirectAudioBackend
from app.output.chromecast import ChromecastBackend
from app.output.dlna import DlnaBackend
from app.output.airplay import AirPlayBackend
from app.output.dacp import DacpServer
from app.queue.engine import QueueEngine
from app.lyrics.prefetch import schedule_prefetch

# ── core singletons ───────────────────────────────────────────────────────────

queue_engine = QueueEngine()
output_router = OutputRouter()

# One instance per protocol — advance_cb wired in setup()
direct_backend: DirectAudioBackend | None = None
chromecast_backend: ChromecastBackend | None = None
dlna_backend: DlnaBackend | None = None
airplay_backend: AirPlayBackend | None = None

# DACP HTTP server for speaker-initiated AirPlay volume callbacks. One per
# process; created and started in setup(), injected into airplay_backend.
dacp_server: DacpServer | None = None

# The ONE shared AsyncZeroconf, created in setup() and bound to UDP 5353
# (2026-06-15 passive-discovery plan U5). Under host networking it binds
# alongside a host avahi via SO_REUSEPORT (the primary, expected path). Both
# in-process discovery sources read it: Chromecast's CastBrowser via
# chromecast_backend._shared_zconf, and the AirPlay/generic browser via
# app.output.mdns_zeroconf (the watcher passes this instance). None only when
# python-zeroconf is unavailable or the 5353 bind raised EADDRINUSE (degraded
# state, surfaced by U6). The DACP server publishes its _dacp._tcp instance
# through the same handle, so 5353 is never bound twice.
shared_aiozc = None

# ── mDNS discovery status ─────────────────────────────────────────────────────
# True when the shared AsyncZeroconf could not bind UDP 5353 at startup
# (EADDRINUSE — a host avahi holds the port and we are not on host networking).
# In that state there is no in-process discovery source; the U6 degraded banner
# tells the operator to run with --network host. (The avahi/D-Bus fallback and
# its _dbus_* flags were retired in plan U7.)
_mdns_port_unavailable: bool = False

# ── PlexClient factory ────────────────────────────────────────────────────────

_plex_client = None
_plex_client_lock = asyncio.Lock()


async def get_plex_client():
    """Return the SourceRegistry of connected media sources, or None when none.

    Retained under the name ``get_plex_client`` for call-site and test-patch
    continuity during the multi-source transition (U3); ``get_source_registry``
    is the source-neutral alias new code should use. Plex servers register as
    ``PlexSource`` providers; future Jellyfin/local sources register the same way.
    Returns None when no source is configured (callers already degrade on None),
    so a zero-source install starts and stays safe.
    """
    global _plex_client
    if _plex_client is not None:
        return _plex_client
    async with _plex_client_lock:
        if _plex_client is not None:
            return _plex_client
        from app import database
        from app.plex.client import PlexClient
        from app.sources.plex import PlexSource
        from app.sources.registry import SourceRegistry
        sources = []
        servers = await database.get_plex_servers()
        if servers:
            sources = [
                PlexSource(PlexClient(
                    server_url=s["server_url"],
                    token=s["token"],
                    client_id=s["client_id"],
                    machine_id=s["machine_id"],
                    server_name=s.get("name", ""),
                    owner=s.get("owner", ""),
                ))
                for s in servers
            ]
        else:
            # Backward-compat: legacy single-server config (machine_id "").
            cfg = await database.get_plex_config()
            if cfg:
                sources = [PlexSource(
                    PlexClient(cfg["server_url"], cfg["token"], cfg["client_id"]),
                    source_id="",
                )]
        if sources:
            _plex_client = SourceRegistry(sources)
        return _plex_client


def invalidate_plex_client() -> None:
    global _plex_client, _ondeck, _ondeck_gen
    _plex_client = None
    from app.api import guest as _guest_api
    _guest_api._enabled_libs_cache = None
    # Selection inputs (server / library set) changed → drop any on-deck pick.
    # Sync best-effort clear + gen bump; an in-flight warm sees the bumped gen
    # under the lock and discards its result (2026-06-21 plan U4).
    _ondeck = None
    _ondeck_gen += 1


# Source-neutral aliases (U3): new code should prefer these names. The legacy
# ``get_plex_client`` / ``invalidate_plex_client`` names are retained above as the
# call-site and test-patch surface during the multi-source transition.
get_source_registry = get_plex_client
invalidate_source_registry = invalidate_plex_client


# ── playback advance ──────────────────────────────────────────────────────────

_auto_advance_pending = False
_advance_lock = asyncio.Lock()  # guards against concurrent EOS + skip advancing
# Monotonic counter: incremented by skip so pending EOS _do_advance tasks bail when they
# see the generation changed between their lock-check and lock-acquisition.
_advance_gen: int = 0
# Closing Time mode (2026-06-24 plan U2): True between a trigger-song freeze and
# the admin's resume. In-memory only — a restart while frozen resets it (the
# party is over anyway, and the next trigger play re-arms). Gates the idle
# auto-start so the freeze isn't immediately undone (_should_auto_start).
_closing_active: bool = False
# The active send-off message, mirrored here on freeze so the now-playing/status
# snapshots can render the banner for a late-joining client without a DB read or
# drifting from what was broadcast. Empty when not frozen.
_closing_message: str = ""

# ── random auto-fill pre-buffer ("on-deck"; 2026-06-21 plan U1/U2) ────────────
# One pre-selected next random track, warmed in the background while the current
# track plays so the next random auto-fill is instant instead of paying the
# whole-library selection lag. It lives OUTSIDE the queue engine, so it can never
# appear in a queue broadcast — invisibility (R5) is structural. Consumed in
# _auto_fill_provider; warmed / invalidated via the event hub. _ondeck_gen is
# bumped on every consume/invalidate so a warm that finishes AFTER an
# invalidation discards its now-stale result (mirrors _advance_gen).
_ondeck = None                     # type: Track | None   # the buffered track, or None
_ondeck_warming: bool = False      # single-flight guard for the warm task
_ondeck_gen: int = 0               # bumped on consume/invalidate; stale-warm guard
_ondeck_lock = asyncio.Lock()      # guards slot read / clear / install


async def _on_dacp_volume_change(session, value: float, absolute: bool) -> None:
    """Bridge DACP server callbacks into the WebSocket event bus.

    Called by app/output/dacp.py when an AirPlay receiver pushes a hardware
    volume change. Applies the echo-guard window so server-initiated writes
    don't echo back as fake external changes, then routes through
    set_volume() so the new level persists to settings (vol:airplay:<id>)
    and survives restarts — without going through set_volume the level
    would only live in self._volume and disappear on the next boot.

    Echo-guard suppression must happen here, NOT inside set_volume:
    set_volume stamps _vol_last_set itself, so calling it always renews
    the window. We only want to skip the call when the incoming change
    IS the echo of our own write.
    """
    if airplay_backend is None:
        return
    from app.output.base import echo_guard_active
    if echo_guard_active(airplay_backend._vol_last_set):
        return  # this is our own write echoing back from the speaker
    if absolute:
        new_level = max(0.0, min(1.0, value))
    else:
        new_level = max(0.0, min(1.0, airplay_backend._volume + value))
    # set_volume persists to settings and writes VOLUME= to cliap2 over the
    # command pipe (a no-op if no session is active). We also broadcast the
    # event so the admin UI updates immediately — set_volume itself does
    # not broadcast (it's the receiving end of slider drags, not the
    # source of external events).
    await airplay_backend.set_volume(new_level)
    from app.events.bus import manager
    from app.events.types import VolumeChangedEvent
    try:
        await manager.broadcast_to_admins(VolumeChangedEvent(level=new_level))
    except Exception:
        _log.warning("DACP: VolumeChangedEvent broadcast failed", exc_info=True)


async def _shuffle_provider(bounds=_UNSET):
    """Return a random track from enabled libraries (artist→album→track traversal).

    This is the whole-library random floor, shared by the guest Surprise Me
    button and the Full Random queue-end mode (2026-06-21).

    Random-pick length band: when a min/max is in effect, each traversal's album
    tracks are filtered to the band and a random in-band track is returned.
    Because a random album may hold no in-band track, the traversal is retried up
    to ``_SHUFFLE_BAND_TRIES`` times; if none is found, one final UNFILTERED
    traversal returns a track anyway so the floor never dead-ends. With no band
    in effect this is a single unfiltered traversal — identical to the
    pre-feature behavior.

    ``bounds`` selects where the band comes from:
      - ``_UNSET`` (the no-arg Surprise Me call): fetch and apply the admin band
        (``get_random_length_bounds``) — unchanged 2026-06-20 behavior.
      - an explicit ``(min_ms, max_ms)`` (the queue-end caller): use it directly.
        The queue-end length-limit checkbox decides whether that is the real band
        or ``(None, None)``.
    """
    from app import database
    if bounds is _UNSET:
        min_ms, max_ms = await database.get_random_length_bounds()
    else:
        min_ms, max_ms = bounds

    # Band-invariant fetches are hoisted out of the retry loop — the client and
    # the enabled-library list don't change between attempts, so re-fetching them
    # per retry (up to _SHUFFLE_BAND_TRIES + 1 times) would be wasted work.
    client = await get_plex_client()
    if not client:
        return None
    enabled_keys = {lib["section_key"] for lib in await database.get_enabled_libraries()}
    all_libs = await client.get_libraries()
    libs = [lib for lib in all_libs if lib.key in enabled_keys]
    if not libs:
        return None

    async def _pick(*, band: bool):
        # Re-randomize lib→artist→album→track on each call so retries explore the
        # library; only the library list itself is invariant (hoisted above).
        lib = random.choice(libs)
        artists = await client.get_artists(lib.key)
        if not artists:
            return None
        artist = random.choice(artists)
        albums = await client.get_albums(lib.key, artist_id=artist.id)
        if not albums:
            return None
        album = random.choice(albums)
        tracks = await client.get_tracks(lib.key, album_id=album.id)
        if not tracks:
            return None
        if band:
            from app.queue.surprise import _within_length
            tracks = [t for t in tracks if _within_length(t, min_ms, max_ms)]
            if not tracks:
                return None
        return random.choice(tracks)

    # No band → single unfiltered traversal (unchanged behavior).
    if min_ms is None and max_ms is None:
        return await _pick(band=False)

    # Band set → bounded retries to find an in-band track, then a final
    # unfiltered traversal as the never-dead-end last resort.
    for _ in range(_SHUFFLE_BAND_TRIES):
        track = await _pick(band=True)
        if track is not None:
            return track
    return await _pick(band=False)


async def _popular_provider(bounds):
    """Return a random locally-popular track for the Popular Random queue-end
    mode, or None when none qualifies/resolves (caller falls back to Full
    Random) — 2026-06-21 plan U4.

    Candidates are tracks whose LOCAL play count (``play_counts``, via
    ``get_top_played_tracks``) is >= the admin threshold. The stored play
    metadata has no ``stream_key``, so each random pick is resolved to a playable
    Track through ``client.get_track``. Unresolvable ids (deleted / unreachable)
    are skipped; when a length band is in effect (``bounds`` from the queue-end
    checkbox) out-of-band picks are skipped too. Resolution is bounded by
    ``_POPULAR_RESOLVE_TRIES`` so a pathological pool can't stall the advance.
    """
    from app import database
    client = await get_plex_client()
    if not client:
        return None
    threshold = await database.get_popular_random_threshold()
    # Unbounded (limit=None): the candidate pool is gated by the play-count
    # threshold alone, NOT by the Most Played display cap — a track that clears
    # the threshold is eligible even if it falls outside the leaderboard's top N.
    rows = await database.get_top_played_tracks(None)  # count-desc, with metadata
    candidates = [r["track_id"] for r in rows if r["count"] >= threshold]
    if not candidates:
        return None

    min_ms, max_ms = bounds
    from app.queue.surprise import _within_length
    random.shuffle(candidates)
    for track_id in candidates[:_POPULAR_RESOLVE_TRIES]:
        try:
            track = await client.get_track(track_id)
        except Exception:
            continue  # deleted / unreachable id — skip (mirrors /api/most-played)
        if track is None:
            continue
        if not _within_length(track, min_ms, max_ms):
            continue  # out-of-band (band only active when the checkbox is on)
        return track
    return None


async def _select_auto_fill_track(behavior):
    """Synchronously select an auto-fill track for a random queue-end mode
    (2026-06-21 queue-end rework; extracted in the pre-buffer plan U1).

    POPULAR_RANDOM picks a local-popular track and falls back to Full Random when
    none qualifies; FULL_RANDOM uses the whole-library floor directly. The admin
    length band constrains these modes ONLY when the opt-in queue-end
    length-limit checkbox is on (default off). Surprise Me is unaffected — it
    calls ``_shuffle_provider()`` with no bounds, which keeps fetching+applying
    the band as before.

    This is the SLOW path (whole-library traversal / Plex resolve). The on-deck
    pre-buffer warms it in the background and serves the result instantly via
    ``_auto_fill_provider``.
    """
    from app import database
    from app.queue.models import QueueEndBehavior
    bounds = (
        await database.get_random_length_bounds()
        if await database.get_queue_end_length_limit()
        else (None, None)
    )
    if behavior == QueueEndBehavior.POPULAR_RANDOM:
        track = await _popular_provider(bounds)
        if track is not None:
            return track
        # never dead-end: fall through to the whole-library floor
    return await _shuffle_provider(bounds)


async def _auto_fill_provider(behavior):
    """Return the next random auto-fill track for ``QueueEngine.advance()``.

    Serves the pre-warmed on-deck track instantly when one is buffered (2026-06-21
    pre-buffer plan U1); otherwise selects synchronously via
    ``_select_auto_fill_track``. The buffer is best-effort — when the slot is empty
    this is byte-identical to the pre-buffer behavior, so liveness never depends on
    it (R4 never-dead-end fallback). Consuming the slot bumps ``_ondeck_gen`` so an
    in-flight warm cannot reinstall the just-consumed generation.
    """
    global _ondeck, _ondeck_gen
    async with _ondeck_lock:
        if _ondeck is not None:
            track = _ondeck
            _ondeck = None
            _ondeck_gen += 1
            return track
    return await _select_auto_fill_track(behavior)


async def invalidate_ondeck() -> None:
    """Discard any buffered on-deck track and bump the generation so an in-flight
    warm discards its now-stale result (2026-06-21 plan U2). Called on preemption
    (a user queued a track) and whenever a selection input changes."""
    global _ondeck, _ondeck_gen
    async with _ondeck_lock:
        _ondeck = None
        _ondeck_gen += 1


def trigger_ondeck_warm() -> None:
    """Fire-and-forget: warm the next on-deck random track when it would help.

    Single-flight (no-op while a warm is in flight). Conditions (R1/R9): a random
    queue-end mode is active, a track is playing, the queue tail is empty, and no
    on-deck track is already buffered. Sync + no awaits before the flag is set, so
    the check-then-set is atomic on the event loop (mirrors trigger_genre_refresh).
    """
    global _ondeck_warming
    from app.queue.models import QueueEndBehavior
    if _ondeck_warming or _ondeck is not None:
        return
    if queue_engine.end_behavior not in (
        QueueEndBehavior.POPULAR_RANDOM, QueueEndBehavior.FULL_RANDOM
    ):
        return
    if not queue_engine.state.is_playing or queue_engine.queue:
        return
    _ondeck_warming = True
    task = asyncio.create_task(_warm_ondeck(queue_engine.end_behavior, _ondeck_gen))
    task.add_done_callback(_log_task_exc)


async def _warm_ondeck(behavior, gen: int) -> None:
    """Background warm: select a random track (the slow path) and install it as
    on-deck IFF the generation is unchanged and the slot is still empty, then
    pre-cache its lyrics (R10). A generation bump (consume/invalidate) during the
    select discards the result. Best-effort — exceptions surface via the task's
    done-callback, never to a caller."""
    global _ondeck, _ondeck_warming
    try:
        track = await _select_auto_fill_track(behavior)
        if track is None:
            return
        async with _ondeck_lock:
            if gen == _ondeck_gen and _ondeck is None:
                _ondeck = track
                schedule_prefetch([track], n=1)
    finally:
        _ondeck_warming = False


async def _ondeck_react(event: str) -> None:
    """React to a playback/queue event for the on-deck buffer (2026-06-21 plan U3).

    ``now_playing_changed`` → warm the next pick (a track started; warm if the tail
    is empty and a random mode is active). ``queue_changed`` → a non-empty queue
    discards the buffered pick (preemption, R6); an empty queue warms the next one
    (R7). The gating lives in ``trigger_ondeck_warm`` / ``invalidate_ondeck``."""
    if event == "now_playing_changed":
        trigger_ondeck_warm()
    elif event == "queue_changed":
        if queue_engine.queue:
            await invalidate_ondeck()
        else:
            trigger_ondeck_warm()


def _make_stream_url(stream_key: str, client) -> str:
    """Return the playback URL for a track.

    When BIND_HOST is set to a specific IP (not 0.0.0.0), or STREAM_BASE_URL is
    set explicitly, returns a /api/stream proxy URL so Cast/DLNA devices that
    can't reach Plex directly fetch the audio through Jukeplox instead.
    """
    from app.config import settings
    base = settings.stream_base_url
    if not base and settings.bind_host and settings.bind_host != "0.0.0.0":
        base = f"http://{settings.bind_host}"
    if base:
        return (base.rstrip("/")
                + "/api/stream?key="
                + urllib.parse.quote(stream_key, safe=""))
    return client.stream_url(stream_key)


def record_play(track) -> None:
    """Record one play of ``track`` across the leaderboard stores.

    The single source of truth for "a track started playing": increments the
    track/album/artist play counts and upserts the display metadata that backs
    /api/most-played. Fire-and-forget so counting never blocks or fails the
    playback path. EVERY play-start path (natural EOS advance, forward Skip,
    Skip Back) must call this — centralising it is what stops a new play path
    from silently forgetting to count (the bug that kept skipped-to tracks off
    Most Played).
    """
    from app import database
    # guest.py imports app.state at module level, so the shared serializer is
    # imported at call time to avoid the admin↔guest module cycle — ONE dict
    # shape for capture and the /api/most-played response.
    from app.api.guest import _track_dict
    _t1 = asyncio.create_task(database.increment_play_count("track", track.id))
    _t1.add_done_callback(_log_task_exc)
    _tm = asyncio.create_task(database.set_play_track_meta(track.id, _track_dict(track)))
    _tm.add_done_callback(_log_task_exc)
    if track.album:
        _t2 = asyncio.create_task(database.increment_play_count("album", track.album))
        _t2.add_done_callback(_log_task_exc)
    if track.artist:
        _t3 = asyncio.create_task(database.increment_play_count("artist", track.artist))
        _t3.add_done_callback(_log_task_exc)


# ── Closing Time mode (2026-06-24 plan U2) ───────────────────────────────────

def _norm(s: str | None) -> str:
    """Case- and whitespace-insensitive normalization for trigger matching:
    collapse internal/edge whitespace and casefold. No feat./remaster fuzzing —
    the admin sets the exact title."""
    return " ".join((s or "").split()).casefold()


async def _closing_trigger_message(track) -> str | None:
    """Return the send-off message if Closing Time is enabled AND ``track`` is
    the configured trigger (title + artist), else None. Settings are read here
    (decision time) so an admin edit/enable takes effect on the next song-end."""
    from app import database
    enabled, title, artist, message = await database.get_closing_time_config()
    if not enabled:
        return None
    if _norm(track.title) == _norm(title) and _norm(track.artist) == _norm(artist):
        return message
    return None


async def _fire_closing(message: str) -> None:
    """Freeze the queue for Closing Time: retire the played trigger to history
    without advancing, then broadcast the banner to every screen. Sets
    ``_closing_active`` BEFORE close_out() emits queue_changed so the idle
    auto-start (which would restart the next track) is suppressed."""
    global _closing_active, _closing_message
    from app.events.bus import manager
    from app.events.types import ClosingTimeEvent
    _closing_active = True
    _closing_message = message
    await queue_engine.close_out()
    try:
        await manager.broadcast_to_all(ClosingTimeEvent(active=True, message=message))
    except Exception:
        _log.warning("Closing Time: banner broadcast failed", exc_info=True)


async def clear_closing() -> None:
    """Drop an active Closing Time freeze and tell every client to hide the
    banner. Idempotent no-op when no freeze is active (avoids spurious banner
    churn). Called by the resume endpoint AND by any admin action that restarts
    playback — Skip / Skip Back — since once music is playing again the freeze is
    over. Does NOT itself start playback; the caller owns that."""
    global _closing_active, _closing_message
    if not _closing_active:
        return
    from app.events.bus import manager
    from app.events.types import ClosingTimeEvent
    _closing_active = False
    _closing_message = ""
    try:
        await manager.broadcast_to_all(ClosingTimeEvent(active=False, message=""))
    except Exception:
        _log.warning("Closing Time: clear broadcast failed", exc_info=True)


async def clear_closing_and_continue() -> None:
    """Admin resumed after a Closing Time freeze: clear the banner everywhere,
    re-arm the trigger (the flag drops, so the next trigger play fires again),
    and continue with the next queued track. Called by the resume endpoint."""
    await clear_closing()
    await _do_advance()


def _should_auto_start() -> bool:
    """Whether a queue_changed should kick off playback: something is queued,
    nothing is playing, no advance already pending, and NOT frozen for Closing
    Time — the freeze must not be undone by the idle auto-start."""
    return (not queue_engine.state.is_playing
            and bool(queue_engine.queue)
            and not _auto_advance_pending
            and not _closing_active)


async def _do_advance() -> None:
    """Pop the next queued track and start playing it. Called by backend EOS callbacks."""
    from app.output.base import DeviceNotReadyError
    captured_gen = _advance_gen
    if _advance_lock.locked():
        return  # skip is in progress; let it own the advance
    async with _advance_lock:
        if _advance_gen != captured_gen:
            return  # a skip occurred between our locked() check and lock acquisition
        # Closing Time: if the track that just ended is the configured trigger,
        # freeze instead of advancing. Checked BEFORE advance() so it beats both
        # the next-track pop and random autofill (both live inside advance()).
        outgoing = queue_engine.state.current
        if outgoing is not None:
            _msg = await _closing_trigger_message(outgoing.track)
            if _msg is not None:
                await _fire_closing(_msg)
                return
        for _ in range(_MAX_CONSECUTIVE_FAILURES):
            next_item = await queue_engine.advance()
            if not next_item:
                return
            client = await get_plex_client()
            if not client:
                return
            url = _make_stream_url(next_item.track.stream_key, client)
            try:
                await output_router.play(url, next_item.track)
                record_play(next_item.track)
                return
            except DeviceNotReadyError:
                _log.warning("_do_advance: output backend has no device connected; halting advance")
                return
            except Exception:
                _log.exception("Playback failed for %r; advancing to next", next_item.track.title)
        _log.error("_do_advance: gave up after %d consecutive playback failures",
                   _MAX_CONSECUTIVE_FAILURES)


async def _trigger_auto_advance() -> None:
    """Triggered when the queue gains items while playback is idle.

    Guards against double-invocation: _auto_advance_pending is set True before this
    task is created, and cleared in finally so future queue_changed events can re-arm.
    """
    global _auto_advance_pending
    try:
        if not queue_engine.state.is_playing and queue_engine.queue:
            await _do_advance()
    finally:
        _auto_advance_pending = False


# ── genre cache background refresh ───────────────────────────────────────────

_genre_refresh_running = False


async def cache_is_fresh(setting_key: str, ttl_s: int = CACHE_TTL_S) -> bool:
    """True when the cache stamped at `setting_key` was computed within `ttl_s`.

    Missing or unparseable stamp → not fresh (so a cold cache always recomputes).
    Used at the read call sites to gate the stale-while-revalidate refresh so a
    warm cache does zero Plex work (gentle-on-Plex U2/U3)."""
    from app import database
    stamp = await database.get_setting(setting_key)
    if not stamp:
        return False
    try:
        computed = datetime.fromisoformat(stamp)
        # TypeError guards a naive (tz-less) legacy/hand-edited stamp: we only
        # ever write tz-aware stamps, but subtracting naive from aware raises —
        # degrade to "not fresh" (recompute) rather than 500 the endpoint.
        return (datetime.now(timezone.utc) - computed).total_seconds() < ttl_s
    except (ValueError, TypeError):
        return False


async def stamp_cache(setting_key: str) -> None:
    """Record 'computed just now' for a cache, in one canonical timestamp format."""
    from app import database
    await database.set_setting(setting_key, datetime.now(timezone.utc).isoformat())


async def _refresh_genre_cache() -> None:
    global _genre_refresh_running
    from app import database
    try:
        client = await get_plex_client()
        if not client:
            return
        libs = await database.get_enabled_libraries()
        if not libs:
            return
        results = await asyncio.gather(
            *[client.get_styles_with_counts(lib["section_key"]) for lib in libs],
            return_exceptions=True,
        )
        counts: dict[str, int] = {}
        names: dict[str, str] = {}
        for batch in results:
            if isinstance(batch, BaseException):
                continue
            for item in batch:
                norm = item["name"].lower()
                counts[norm] = counts.get(norm, 0) + item["count"]
                names.setdefault(norm, item["name"])
        merged = [{"name": names[k], "count": v} for k, v in counts.items() if v > 0]
        merged.sort(key=lambda x: x["count"], reverse=True)
        await database.set_genre_cache(merged)
        await stamp_cache("genre_cache_computed_at")
    except Exception:
        _log.exception("Genre cache refresh failed")
    finally:
        _genre_refresh_running = False


def trigger_genre_refresh() -> None:
    """Fire-and-forget genre cache refresh. No-op if a refresh is already in-flight."""
    global _genre_refresh_running
    if _genre_refresh_running:
        return
    _genre_refresh_running = True
    task = asyncio.create_task(_refresh_genre_cache())
    task.add_done_callback(_log_task_exc)


# ── credit cache background refresh (2026-06-10 per-track credits plan U2) ───

_credit_refresh_running = False


async def _refresh_credit_cache() -> None:
    """Scan every enabled library's tracks for per-track credits and rebuild
    the credit_cache. Background-only — NEVER called inline on a request
    path; a section-wide track pull is too heavy for that (unlike the genre
    refresh's styles query)."""
    global _credit_refresh_running
    from app import database
    try:
        client = await get_plex_client()
        if not client:
            return
        libs = await database.get_enabled_libraries()
        if not libs:
            return
        results = await asyncio.gather(
            *[client.get_tracks(lib["section_key"]) for lib in libs],
            return_exceptions=True,
        )
        # A fully-failed scan must not wipe a good cache: bail before the
        # atomic replace when every library errored.
        if results and all(isinstance(b, BaseException) for b in results):
            _log.warning("Credit cache refresh: all libraries failed; cache untouched")
            return
        rows: dict[tuple[str, str], dict] = {}
        for batch in results:
            if isinstance(batch, BaseException):
                continue
            for t in batch:
                credit = (t.artist or "").strip()
                release_artist = (t.album_artist or "").strip()
                # Post-U1, Track.artist IS the per-track credit when one
                # exists; a row is index-worthy only when it differs from
                # the release artist (case-insensitive) and both ends are
                # usable.
                if not credit or not t.album_id:
                    continue
                if credit.lower() == release_artist.lower():
                    continue
                key = (credit.lower(), t.album_id)
                rows.setdefault(key, {
                    "name": credit,
                    "name_lower": credit.lower(),
                    "album_id": t.album_id,
                    "album_title": t.album or "",
                    "album_artist": release_artist,
                    "album_thumb": t.thumb,
                    "album_year": t.year,
                    "server_name": t.server_name or None,
                })
        await database.set_credit_cache(list(rows.values()))
        await stamp_cache("credit_cache_computed_at")
        _log.info("Credit cache refreshed: %d act-release rows", len(rows))
    except Exception:
        _log.exception("Credit cache refresh failed")
    finally:
        _credit_refresh_running = False


def trigger_credit_refresh() -> None:
    """Fire-and-forget credit cache refresh. Single-flighted — no-op while a
    scan is in flight, so guest-reachable call sites cannot stack
    concurrent full-library track pulls (review fix, security)."""
    global _credit_refresh_running
    if _credit_refresh_running:
        return
    _credit_refresh_running = True
    task = asyncio.create_task(_refresh_credit_cache())
    task.add_done_callback(_log_task_exc)


# ── browse index background refresh (2026-06-21 browse-index plan U2) ────────

_browse_index_refresh_running = False


def _build_browse_index_rows(libs_data: list) -> tuple[list[dict], list[dict]]:
    """Pure transform: (server_name, section_key, artists, albums) tuples →
    (artist_rows, album_rows) ready for database.set_browse_index. No I/O, so
    it is unit-testable with fake Artist/Album objects.

    Both the artist base_key and an album's artist_base_key derive from the
    SAME browse_base_key over the artist name (album.artist == parentTitle ==
    the release artist), so get_browse_albums_for_artist(artist.base_key)
    reproduces today's "albums whose parent is this artist" set."""
    from app.plex.client import browse_base_key
    artist_rows: list[dict] = []
    album_rows: list[dict] = []
    for server_name, section_key, artists, albums in libs_data:
        for a in artists:
            artist_rows.append({
                "artist_id": a.id,
                "title": a.title,
                "base_key": browse_base_key(a.title),
                "thumb": a.thumb,
                "release_count": a.release_count,
                "server_name": server_name,
                "section_key": section_key,
            })
        for alb in albums:
            album_rows.append({
                "album_id": alb.id,
                "title": alb.title,
                "title_base": browse_base_key(alb.title),
                "artist": alb.artist,
                "artist_base_key": browse_base_key(alb.artist),
                "year": alb.year,
                "thumb": alb.thumb,
                "subtype": alb.subtype,
                "added_at": alb.added_at,
                "track_count": alb.track_count,
                "server_name": server_name,
                "section_key": section_key,
            })
    return artist_rows, album_rows


async def _refresh_browse_index() -> None:
    """Rebuild the cross-server browse index from BULK per-section crawls — one
    get_artists + one get_albums call per enabled library, NOT per-artist pulls.
    Background-only (a section-wide album pull is too heavy for a request path),
    single-flighted by trigger_browse_index_refresh. Clones the credit-cache
    refresh shape, including the 'all libraries failed → don't wipe a good
    index' guard."""
    global _browse_index_refresh_running, _browse_index_gen
    from app import database
    try:
        client = await get_plex_client()
        if not client:
            return
        all_libs = await client.get_libraries()
        enabled_keys = {lib["section_key"] for lib in await database.get_enabled_libraries()}
        libs = [l for l in all_libs if l.key in enabled_keys]
        if not libs:
            return
        artist_results = await asyncio.gather(
            *[client.get_artists(l.key) for l in libs], return_exceptions=True
        )
        album_results = await asyncio.gather(
            *[client.get_albums(l.key) for l in libs], return_exceptions=True
        )
        # Require BOTH an artist call AND an album call to have succeeded before
        # the atomic replace — never install a PARTIAL index. An asymmetric
        # failure (e.g. every section-wide album query times out while the artist
        # lists return) would otherwise install an artists-only index AND stamp it
        # fresh, so drill-ins show "no releases" with no live fallback until the
        # 6h TTL or a manual Rescan. An empty library is fine: an empty list is a
        # success, not an exception. (Tightens the credit-cache don't-wipe guard
        # for this two-call crawl — review finding #1.)
        artists_ok = any(not isinstance(r, BaseException) for r in artist_results)
        albums_ok = any(not isinstance(r, BaseException) for r in album_results)
        if not (artists_ok and albums_ok):
            _log.warning(
                "Browse index refresh: artists_ok=%s albums_ok=%s — index untouched",
                artists_ok, albums_ok,
            )
            return
        libs_data = []
        for lib, ar, br in zip(libs, artist_results, album_results):
            artists = ar if not isinstance(ar, BaseException) else []
            albums = br if not isinstance(br, BaseException) else []
            libs_data.append((lib.server_name, lib.key, artists, albums))
        artist_rows, album_rows = _build_browse_index_rows(libs_data)
        await database.set_browse_index(artist_rows, album_rows)
        await stamp_cache("browse_index_computed_at")
        # Roster changed: invalidate the old grouping signature and rebuild the
        # map for the fresh roster (plan U2). Bump first so the rebuild stamps
        # the new generation; both are in-memory, off-request.
        _browse_index_gen += 1
        await _rebuild_artist_grouping()
        _log.info(
            "Browse index refreshed: %d artists, %d albums",
            len(artist_rows), len(album_rows),
        )
    except Exception:
        _log.exception("Browse index refresh failed")
    finally:
        _browse_index_refresh_running = False


def trigger_browse_index_refresh() -> None:
    """Fire-and-forget browse-index rebuild. Single-flighted — no-op while a
    crawl is in flight, so concurrent triggers (stale read, Rescan, library
    toggle) cannot stack full-library crawls (mirrors trigger_credit_refresh)."""
    global _browse_index_refresh_running
    if _browse_index_refresh_running:
        return
    _browse_index_refresh_running = True
    task = asyncio.create_task(_refresh_browse_index())
    task.add_done_callback(_log_task_exc)


def browse_index_building() -> bool:
    """True while a browse-index crawl is in flight — for the admin status
    badge (plan U6). Accessor so callers don't reach into the module global."""
    return _browse_index_refresh_running


# ── artist grouping map (rule-norm → base_keys; 2026-06-22 plan U1) ──────────
# Derived, signature-guarded, best-effort cache that lets the artist drill-in
# resolve rule-merged sibling base-keys with an O(1) lookup instead of
# normalizing the WHOLE roster per request (review finding #2). The signature
# ties the map to the (browse-index generation, active rules) it was built
# under; the drill-in consults the map ONLY when the signature matches current,
# else falls back to the live per-request scan — so a stale map can never return
# wrong/fewer releases (plan R5). Held as ONE tuple reference so a concurrent
# read sees either the whole old or whole new (sig, map), never a torn pair (R7).

_browse_index_gen: int = 0
_artist_grouping: tuple | None = None  # ((index_gen, rules_sig), {norm: set(base_key)})


def browse_index_gen() -> int:
    """Monotonic counter of browse-index replacements — one half of the
    grouping-map signature. Bumped on each set_browse_index (see
    _refresh_browse_index)."""
    return _browse_index_gen


def rules_sig(compiled) -> str:
    """Stable signature of the active compiled pattern rules — the other half of
    the grouping-map signature. Changes iff the rule set changes. The compiled
    rules are tiny, so repr() is a cheap, deterministic key."""
    return repr(compiled)


def _build_artist_grouping(rows: list[dict], compiled) -> dict:
    """Pure: roster rows (from database.get_browse_artists()) + compiled rules →
    {rule_norm: set(base_key)}. Uses the SAME normalization as the drill-in's
    _norm (normalize + strip) so map keys line up with lookup keys. No I/O —
    unit-testable with fake rows."""
    from app.normalize import normalize
    out: dict[str, set] = {}
    for r in rows:
        norm = normalize(r.get("title"), compiled).strip()
        out.setdefault(norm, set()).add(r.get("base_key"))
    return out


def _swap_artist_grouping(signature, mapping: dict) -> None:
    """Atomically install a freshly-built grouping under `signature`. Single
    global-reference assignment → concurrent drill-in reads see whole-old or
    whole-new, never a half-built map (R7)."""
    global _artist_grouping
    _artist_grouping = (signature, mapping)


def get_artist_grouping(signature) -> dict | None:
    """Return the grouping map IFF it was built under `signature`
    (= (browse_index_gen(), rules_sig(compiled))); else None. This guard is what
    makes the drill-in's use of the map parity-safe: any drift (startup before
    first build, mid-rebuild race, rules changed but map not yet rebuilt) yields
    None → the caller falls back to the proven per-request scan (plan R4/R5)."""
    g = _artist_grouping
    if g is not None and g[0] == signature:
        return g[1]
    return None


_grouping_rebuild_running = False


async def _rebuild_artist_grouping() -> None:
    """Rebuild + swap the grouping map from the current stored roster and active
    rules, stamped with the FULL (browse_index_gen, rules_sig) signature. DB +
    in-memory only (no Plex). Called inline by the index refresh (gen already
    bumped) and by the rule-save trigger (gen unchanged, rules_sig advanced) —
    both stamp the full signature via this one path."""
    from app import database
    from app.normalize import compile_rules
    try:
        rows = await database.get_browse_artists()
        compiled = compile_rules(await database.get_pattern_rules())
        mapping = _build_artist_grouping(rows, compiled)
        _swap_artist_grouping((browse_index_gen(), rules_sig(compiled)), mapping)
    except Exception:
        _log.exception("Artist grouping rebuild failed")


def trigger_artist_grouping_rebuild() -> None:
    """Fire-and-forget grouping-map rebuild for the rule-save path. Single-
    flighted. Correctness never depends on it landing: the drill-in's signature
    guard falls back to the live scan until the new map is in place (plan R4/R5)."""
    global _grouping_rebuild_running
    if _grouping_rebuild_running:
        return
    _grouping_rebuild_running = True

    async def _run():
        global _grouping_rebuild_running
        try:
            await _rebuild_artist_grouping()
        finally:
            _grouping_rebuild_running = False

    task = asyncio.create_task(_run())
    task.add_done_callback(_log_task_exc)


async def _startup_reconnect(backend, device_id: str) -> None:
    """Reconnect to the last-used device. Fire-and-forget; never raises."""
    from app import database

    # R3: Try cached host:port first — no mDNS, no D-Bus, no socket mount required.
    addr_raw = await database.get_setting(f"output_addr:{device_id}")
    if addr_raw:
        try:
            addr = json.loads(addr_raw)
            host = addr["host"]
            port = int(addr["port"])
            name = addr.get("name", device_id)
            from app.output.chromecast import ChromecastBackend
            from app.output.airplay import AirPlayBackend
            if isinstance(backend, ChromecastBackend):
                backend._dbus_index[device_id] = (name, host, port)
            elif isinstance(backend, AirPlayBackend):
                # Empty TXT dict on cached-reconnect path: cliap2 will then
                # likely fail HAP pair-verify and surface a re-pair event on
                # stderr, which the watcher converts to an OutputChangedEvent.
                # That signals the user to rescan rather than silently using
                # a stale cached address.
                backend._device_addr[device_id] = (name, host, port, {})
            await backend.set_device(device_id)
            return  # R4: connected via cached address, no discovery needed
        except Exception:
            pass  # R6: stale address — fall through silently to discover_devices

    # R5/R6: Fall through to discovery (unchanged from original path).
    try:
        await backend.discover_devices()
    except Exception:
        _log.warning("startup reconnect discover failed", exc_info=True)
    try:
        await backend.set_device(device_id)
    except Exception:
        # R7: both cached address and discovery failed — emit specific actionable message.
        _log.warning("startup reconnect failed for device %r", device_id, exc_info=True)
        try:
            from app.events.bus import manager
            from app.events.types import OutputChangedEvent
            await manager.broadcast_to_admins(OutputChangedEvent(
                backend_type="error",
                device_name=(
                    "Device address may have changed — "
                    "open Output settings and rescan to reconnect"
                ),
            ))
        except Exception:
            pass


# ── startup wiring ────────────────────────────────────────────────────────────

async def setup() -> None:
    """Initialize backends and wire event callbacks. Called at app startup."""
    global direct_backend, chromecast_backend, dlna_backend, airplay_backend
    global _mdns_port_unavailable, shared_aiozc

    # Enforce the art-cache size cap as a background task — it loads the
    # on-disk index and evicts down to settings.art_cache_size_mb if it
    # exceeded the cap (e.g., cap was reduced between runs per R7).
    #
    # Scheduled via `asyncio.create_task` instead of awaited so a huge
    # on-disk cache doesn't delay the lifespan yield (and thus delay every
    # backend init + the first request being served). The task runs in
    # the background, and any exception is logged + swallowed inside
    # `enforce_cap_async` itself. Concurrent get/put requests coordinate
    # with the load via the cache's internal `_load_event` so they
    # observe the index population atomically once the executor completes.
    try:
        from app.cache import cache as _art_cache
        _t = asyncio.create_task(_art_cache.enforce_cap_async())
        _t.add_done_callback(
            lambda t: t.exception() if not t.cancelled() and t.exception() else None,
        )
    except Exception:
        _log.warning("art cache startup enforcement scheduling failed", exc_info=True)

    direct_backend = DirectAudioBackend(advance_cb=_do_advance)
    chromecast_backend = ChromecastBackend(advance_cb=_do_advance)
    dlna_backend = DlnaBackend(advance_cb=_do_advance)
    airplay_backend = AirPlayBackend(advance_cb=_do_advance)

    # Create ONE shared AsyncZeroconf — the single in-process mDNS stack for
    # ALL passive discovery (2026-06-15 plan U5). Chromecast's CastBrowser and
    # the AirPlay/generic browser (app.output.mdns_zeroconf) both read it, and
    # the DACP server publishes its _dacp._tcp.local. instance through it, so
    # 5353 is bound exactly once. Under host networking it binds alongside a
    # host avahi via SO_REUSEPORT — the primary, expected path. A failed bind
    # is the degraded state the U6 banner surfaces (run with --network host),
    # not a reason to crash; discovery simply has no in-process source.
    _shared_aiozc = None
    try:
        from zeroconf.asyncio import AsyncZeroconf as _AsyncZeroconf
        _shared_aiozc = _AsyncZeroconf()
    except OSError as exc:
        if getattr(exc, "errno", None) == errno.EADDRINUSE:
            _mdns_port_unavailable = True
            _log.warning(
                "Cannot bind mDNS port 5353: %s — a host avahi likely owns it. "
                "Falling back to avahi over D-Bus for Cast/AirPlay discovery "
                "(needs /run/dbus/system_bus_socket mounted). To use the faster "
                "in-process path instead, run with --network host on a host "
                "where avahi permits other stacks (disallow-other-stacks=no).",
                exc,
            )
        else:
            _log.error("Shared Zeroconf init failed: %s", exc)
    except ImportError:
        pass

    # Publish module-level so the device watcher's default subscribe path can
    # hand this single instance to app.output.mdns_zeroconf (no second bind).
    shared_aiozc = _shared_aiozc

    if _shared_aiozc is not None:
        # One Zeroconf, two browsers: CastBrowser (sync Zeroconf) for Cast,
        # AsyncServiceBrowser (the shared AsyncZeroconf) for AirPlay/_raop.
        chromecast_backend._shared_zconf = _shared_aiozc.zeroconf

    # ── DACP server (speaker-initiated AirPlay volume) ───────────────────
    global dacp_server
    dacp_server = DacpServer()
    try:
        await dacp_server.start(shared_aiozc=_shared_aiozc)
    except Exception as exc:
        _log.warning(
            "DACP server failed to start: %s — speaker-initiated AirPlay "
            "volume changes will not surface, but UI-to-speaker volume still "
            "works", exc,
        )
        dacp_server = None
    if dacp_server is not None:
        airplay_backend._dacp_server = dacp_server
        dacp_server._on_volume_change = _on_dacp_volume_change

    queue_engine._auto_fill_provider = _auto_fill_provider

    # Restore the saved queue-end behavior (2026-06-21 plan U2). Without this the
    # engine silently reverts to STOP on every restart until the admin re-saves,
    # so Popular/Full Random would not survive a reboot. coerce_* migrates the
    # retired shuffle/repeat values and defaults unknown/missing → STOP.
    from app import database
    from app.queue.models import coerce_queue_end_behavior
    queue_engine.end_behavior = coerce_queue_end_behavior(
        await database.get_setting("queue_end_behavior")
    )

    # Restore last active backend from DB
    backend_type = await database.get_setting("output_backend_type") or "direct"
    device_id = await database.get_setting("output_device_id") or "default"
    _set_backend_by_type(backend_type)
    if device_id != "default" and output_router.active:
        if backend_type in {"chromecast", "airplay", "dlna"}:
            # Discovery takes time — run in background to avoid blocking startup
            asyncio.create_task(_startup_reconnect(output_router.active, device_id))
        else:
            try:
                await output_router.active.set_device(device_id)
            except Exception:
                pass

    # Wire queue engine callbacks → WebSocket broadcasts
    from app.events.bus import manager
    from app.events.types import (
        LockChangedEvent,
        NowPlayingEvent,
        PlaybackStateEvent,
        QueueChangedEvent,
        QueueItem,
    )

    def _qi(item) -> QueueItem:
        t = item.track
        return QueueItem(
            track_id=t.id,
            title=t.title,
            artist=t.artist,
            album=t.album,
            thumb=t.thumb,
            duration_ms=t.duration_ms,
            album_id=t.album_id,
            added_at=item.added_at,
        )

    async def _on_event(event: str, payload=None) -> None:
        global _auto_advance_pending
        if event == "queue_changed":
            ev = QueueChangedEvent(
                queue=[_qi(i) for i in queue_engine.queue],
                history=[_qi(i) for i in queue_engine.history],
                is_locked=queue_engine.is_locked,
            )
            await manager.broadcast_to_all(ev)
            # Warm lyrics for the next-up window. Re-runs on every queue mutation
            # (add / remove / reorder / Play-next), so a bumped track that lands in
            # the window is warmed immediately. Fire-and-forget — never blocks the
            # broadcast (plan 2026-06-18-001 U3).
            schedule_prefetch([i.track for i in queue_engine.queue])
            # On-deck pre-buffer reaction (2026-06-21 plan U3).
            await _ondeck_react("queue_changed")
            # Auto-start: if nothing is playing and the queue just got items, begin
            # playback — unless a Closing Time freeze is active (which must not be
            # undone by restarting the next track). See _should_auto_start.
            if _should_auto_start():
                _auto_advance_pending = True
                _adv_task = asyncio.create_task(_trigger_auto_advance())
                _adv_task.add_done_callback(_log_task_exc)
        elif event == "lock_changed":
            await manager.broadcast_to_all(LockChangedEvent(is_locked=bool(payload)))
        elif event == "now_playing_changed":
            state = queue_engine.state
            if state.current:
                t = state.current.track
                ev = NowPlayingEvent(
                    track_id=t.id,
                    title=t.title,
                    artist=t.artist,
                    album=t.album,
                    album_id=t.album_id,
                    thumb=t.thumb,
                    duration_ms=t.duration_ms,
                    is_playing=state.is_playing,
                    is_paused=state.is_paused,
                )
            else:
                ev = NowPlayingEvent(is_playing=False, is_paused=False)
            await manager.broadcast_to_all(ev)
            # After an advance the window slides forward — warm the new next-up.
            schedule_prefetch([i.track for i in queue_engine.queue])
            # On-deck pre-buffer reaction (2026-06-21 plan U3).
            await _ondeck_react("now_playing_changed")
        elif event == "playback_state_changed":
            state = queue_engine.state
            await manager.broadcast_to_all(
                PlaybackStateEvent(is_playing=state.is_playing, is_paused=state.is_paused)
            )

    queue_engine.add_callback(_on_event)

    # Restore queue from DB
    await queue_engine.load_from_db()


def _get_backend(backend_type: str):
    return {
        "direct": direct_backend,
        "chromecast": chromecast_backend,
        "dlna": dlna_backend,
        "airplay": airplay_backend,
    }.get(backend_type, direct_backend)


def _set_backend_by_type(backend_type: str) -> None:
    backend = _get_backend(backend_type)
    if backend:
        output_router.set_backend(backend)


async def activate_backend(
    backend_type: str, device_id: str, *, host: str | None = None,
) -> None:
    """Switch active backend, persist the choice, and auto-start if queue has items.

    When *host* is provided (the cross-protocol picker sets it on every
    Apply), also persist:
      - ``output_host`` — the canonical "which host is currently active"
        signal independent of the in-memory discovery cache, so a server
        restart still surfaces the right host to the frontend on first
        page load.
      - ``device_via:{host}`` — the per-device Via preference, so the
        next session pre-selects the same protocol without a second
        click.
    """
    global _auto_advance_pending
    from app import database
    from app.config import settings
    if backend_type in ("chromecast", "dlna") and not settings.stream_base_url:
        _log.warning(
            "SECURITY: STREAM_BASE_URL is not set — Plex auth tokens will be embedded "
            "in %s stream URLs and sent in cleartext over the LAN. "
            "Set STREAM_BASE_URL to a Jukeplox-reachable URL to use the stream proxy instead.",
            backend_type,
        )
    new_backend = _get_backend(backend_type)
    if new_backend:
        prev_backend = output_router.active
        output_router.set_backend(new_backend)
        if device_id != "default":
            try:
                await new_backend.set_device(device_id)
            except Exception:
                output_router.set_backend(prev_backend)
                raise
    await database.set_setting("output_backend_type", backend_type)
    await database.set_setting("output_device_id", device_id)
    if host:
        await database.set_setting("output_host", host)
        await database.set_setting(f"device_via:{host}", backend_type)
    # If tracks are queued and nothing is playing, start now on the newly selected backend
    if (not queue_engine.state.is_playing
            and queue_engine.queue
            and not _auto_advance_pending):
        _auto_advance_pending = True
        asyncio.create_task(_trigger_auto_advance())
