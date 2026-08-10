"""Guest-facing API routes — no authentication required."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
import re
import time
from urllib.parse import quote, unquote

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

_log = logging.getLogger(__name__)

# Browser cache headers shared by every /api/art response. Plex art URLs embed
# a version stamp (/thumb/<version>) so URLs are content-addressed —
# `immutable` is correct because the URL itself changes when the bytes change.
_ART_CACHE_HEADERS = {
    "Cache-Control": "public, max-age=31536000, immutable",
}


def _art_etag(path: str) -> str:
    """Strong ETag derived from the validated Plex path (KTD3).
    First 16 hex chars of SHA-256 — plenty of collision resistance for
    cache-validator purposes and avoids paying for bytes hashing."""
    digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
    return f'"{digest}"'

_ID_RE = re.compile(r'^[A-Za-z0-9_-]+(?::[A-Za-z0-9_-]+)?$')


def validate_plex_id(value: str | None) -> None:
    if value and (len(value) > 128 or not _ID_RE.match(value)):
        raise HTTPException(status_code=400, detail="Invalid resource ID")

_templates = Jinja2Templates(directory="app/templates")
from app import assets as _assets
_assets.register(_templates)  # `asset_v` global → build-derived cache-buster

from app import state
from app.lyrics import cache as lyrics_cache
from app.plex.client import browse_base_key
from app.models import Album, Artist
from app.queue.engine import QueueLockError

router = APIRouter(tags=["guest"])


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def guest_index(request: Request):
    # Cache-Control: no-cache (2026-07-17 ce-debug): the ?v=<sha> asset buster
    # only fires if the HTML referencing it is fresh. With no cache headers,
    # guest phones pinned a stale page — and with it the pre-fix broad-search
    # cascade bundle — across deploys; one stale device's cascade drove live
    # Tier-1 searches to 40+ seconds for every guest while admin (whose stale
    # bundle never fired the cascade) stayed fast. no-cache forces
    # revalidation on every load so a plain reload always picks up new JS.
    return _templates.TemplateResponse(request, "guest/index.html",
                                       headers={"Cache-Control": "no-cache"})


# ── Now playing ───────────────────────────────────────────────────────────────

@router.get("/api/now-playing")
async def now_playing():
    from app.output import session as output_session
    s = state.queue_engine.state
    # Output-session state (supervisor plan U4, R20): the GUEST-LEAN snapshot
    # ({state, held} only — same state truth as the admin's rich snapshot, no
    # outage detail). Present in both branches because an outage hold clears
    # `current`, so a guest loading mid-outage rides the no-current branch and
    # still renders the "paused — output offline" note.
    output_snap = output_session.session_snapshot()
    if not s.current:
        return {
            "track_id": None, "title": None, "artist": None, "album": None,
            "album_id": None,
            "thumb": None, "duration_ms": 0, "is_playing": False, "is_paused": False,
            # Closing Time (2026-06-24 plan U3): present even with no current track,
            # since the freeze clears `current` — a late-joining guest renders the
            # banner from these on load.
            "closing_active": state._closing_active,
            "closing_message": state._closing_message,
            "output_session": output_snap,
        }
    t = s.current.track
    return {
        "track_id": t.id,
        "title": t.title,
        "artist": t.artist,
        "album": t.album,
        "album_id": t.album_id,
        "thumb": t.thumb,
        "duration_ms": t.duration_ms,
        "server_name": t.server_name,
        "is_playing": s.is_playing,
        "is_paused": s.is_paused,
        "closing_active": state._closing_active,
        "closing_message": state._closing_message,
        "output_session": output_snap,
    }


# ── Queue ─────────────────────────────────────────────────────────────────────

@router.get("/api/queue")
async def get_queue():
    from app.events.bus import manager
    q = state.queue_engine
    n = manager.guest_n
    m = manager.guest_m
    queue = q.queue[:n] if n is not None else q.queue
    history = q.history[:m] if m is not None else q.history
    # added_at is the per-entry half of the append receipt (track_id is
    # the other); the guest UI matches it against its stored receipts to
    # show a remove (✕) on the entries this browser queued. Upcoming only
    # — history/now-playing are not guest-removable. (mirrors admin's
    # per-item `position` merge in app/api/admin.py get_queue)
    # owner_token: the durable half of guest ownership (added_at is the receipt
    # half). Echoed so a reconnecting guest can match its pre-stored token and
    # restore the remove (✕) even when it never saw the append response.
    queue_rows = [{**_track_dict(i.track), "added_at": i.added_at,
                   "owner_token": i.owner_token} for i in queue]
    history_rows = [_track_dict(i.track) for i in history]
    # plex_held: identity-mode annotate (queue rows carry enqueue-time hold
    # SNAPSHOTS — the flag must resolve live; plan U4). One combined pass so
    # queue + history (the recents strip) share a single bulk holds read.
    await _annotate_plex_held(queue_rows + history_rows)
    return {
        "queue": queue_rows,
        "history": history_rows,
        "is_locked": q.is_locked,
    }


def _track_dict(t) -> dict:
    # Per-source list for the "Play From Source…" picker (parity plan U2). Built
    # from the catalog holds attached by views._track — each carries server_name
    # + source_type so the frontend renders a type-qualified label and re-POSTs
    # the chosen source. GUARDED on server_name: the enqueue-time playback holds
    # ({source_id, key} only, set by _attach_holds) carry no server_name, so
    # queue/history rows serialized here emit no `sources` (queued-item source
    # visibility is deferred). Native track dicts have empty holds → no `sources`
    # → byte-identical (R8). Emitted only when >1 source actually holds the item.
    src = [{"server_name": h.get("server_name") or "", "source_type": h.get("source_type") or ""}
           for h in (getattr(t, "holds", None) or []) if h.get("server_name")]
    d = {
        "track_id": t.id,
        "title": t.title,
        "artist": t.artist,
        # Album-level artist (2026-08-10 long-titles/VA plan U1): lets the
        # release drill-in header attribute the album correctly — a Various
        # Artists comp reads "Various Artists" instead of tracks[0]'s performer.
        # Additive; existing consumers ignore it. None for albumless/legacy rows.
        "album_artist": getattr(t, "album_artist", None),
        "album": t.album,
        # Album drill target for clickable names (2026-06-10 nav plan U1).
        # None for albumless tracks; absent in pre-change play_track_meta
        # rows — consumers must tolerate both.
        "album_id": t.album_id,
        "thumb": t.thumb,
        # Album year (release-art plan 2026-06-15 U1): surfaced so the release
        # drill-in header can read "artist · year". Additive — existing track
        # consumers ignore it. Track.year is set by _parse_track.
        "year": getattr(t, "year", None),
        # Genre (2026-06-17 Surprise Me U5): the shared seed store records it so
        # the heuristic source can pick a genre matching the user's own picks.
        # Additive — existing consumers ignore it.
        "genre": getattr(t, "genre", None),
        "duration_ms": t.duration_ms,
        "server_name": t.server_name,
        # Multi-disc ordering (2026-06-11): drives disc headers + the
        # disc-aware dedup key in the shared browse module. getattr-guarded
        # for play_track_meta-era dicts re-wrapped without these fields.
        "disc_number": getattr(t, "disc_number", 1) or 1,
        "track_number": getattr(t, "track_number", None),
    }
    if len(src) > 1:
        d["sources"] = src
    return d


async def _annotate_plex_held(rows: list[dict],
                              tracks: list | None = None) -> list[dict]:
    """Stamp the per-track ``plex_held`` flag on serialized track rows, in
    place (2026-08-04-002 plexplayer plan U4, R6 data layer).

    ALWAYS emitted, true/false, regardless of the active backend — the flag
    is the plain fact "this identity has ≥1 hold from an enabled Plex
    source", resolved live at render time (rescan-safe), with zero
    conditional-on-backend logic; whether a row *dims* is decided purely by
    the client's body-level ``source_lock`` switch (U5).

    Two resolution modes, both one registry read + one map build per request
    (never per-row queries — the browse-latency guard):

    * ``tracks`` given (catalog browse/search assembly): the parallel model
      objects already carry this request's freshly-loaded holds
      (``views._track``) — derive from them, no extra queries.
    * ``tracks`` omitted (queue snapshots, persisted most-played/
      highest-rated metadata rows): resolve live by identity through ONE
      bulk ``catalog_holds`` read (``store.get_holds_map``) — enqueue-time
      hold snapshots and record-time metadata both go stale across rescans.

    Native single-Plex path (no catalog floor): every served track IS
    Plex-backed — constant True, no catalog reads."""
    if not rows:
        return rows
    if not await _catalog_active():
        for r in rows:
            r["plex_held"] = True
        return rows
    from app.catalog import store, views
    enabled = await state.plex_enabled_source_ids()
    if not enabled:
        # No enabled Plex source exists — no holder can qualify. Constant
        # False without the holds read (also keeps annotate DB-free on
        # registry-less/mixed test doubles).
        for r in rows:
            r["plex_held"] = False
        return rows
    if tracks is not None:
        for r, t in zip(rows, tracks):
            r["plex_held"] = views.holds_plex_held(
                getattr(t, "holds", None), enabled)
        return rows
    # PLX-9: the same alias-bridged holds read the enqueue gate uses — a
    # native-id queue entry (admin album append shape) must annotate
    # exactly as the gate would decide it, or flag and gate disagree.
    holds_map = await _bridged_holds_map(
        [r.get("track_id") for r in rows if r.get("track_id")])
    for r in rows:
        r["plex_held"] = views.holds_plex_held(
            holds_map.get(r.get("track_id")), enabled)
    return rows


async def _bridged_holds_map(track_ids: list[str]) -> dict:
    """Bulk track-holds lookup WITH the alias bridge (review fix PLX-9 —
    factored out of the enqueue gate so ``_plex_playable_ids`` and
    ``_annotate_plex_held`` can never disagree): ids the holds table doesn't
    know directly (native provider ids — the admin album branch resolves
    those in catalog mode, and they land in the queue as entry track_ids)
    get one identity-resolution attempt through ``catalog_identity_alias``;
    a bridged identity's holds are surfaced under the ORIGINAL id."""
    from app.catalog import identity as cat_identity, store
    holds_map = await store.get_holds_map("track", track_ids)
    aliases = {}
    for tid in track_ids:
        if tid not in holds_map:
            ident = await cat_identity.identity_for_track_id(tid)
            if ident and ident != tid:
                aliases[tid] = ident
    if aliases:
        alias_holds = await store.get_holds_map(
            "track", list(dict.fromkeys(aliases.values())))
        for tid, ident in aliases.items():
            if ident in alias_holds:
                holds_map[tid] = alias_holds[ident]
    return holds_map


# ── Lyrics (Now Playing → Lyrics, plan 2026-06-17-008 U2) ─────────────────────
# GET /api/lyrics?track_id=… — resolves the track SERVER-SIDE (never trusts
# client-supplied match fields, which would let one bad request poison the shared
# cache) and delegates to the shared lyric cache (app/lyrics/cache.py), which both
# this endpoint and the rolling-window prefetcher share. A miss is {available:false}
# at 200 so the frontend silently shows nothing (R8); the cache stores definitive
# answers (incl. negatives) but not transient failures (the 2026-06-18 bug).
# Contribute prompt (2026-06-23): on a CONFIRMED no-match (no_match=True, not
# instrumental) with the admin toggle on, a `contribute` link is attached to a COPY
# of the result at RESPONSE TIME — never written to the cache. So toggling the
# setting takes effect on the next lookup without a cache flush, and the cached
# lyrics are never poisoned. A transient failure (no_match=False) never qualifies.
#
# The link is a CONSTANT — the LRCLIB uploader. LRCLIB exposes no URL that
# pre-fills a track: lrclib.net is an SPA that ignores query params, and the
# uploader takes no prefill params either (verified against primary sources
# 2026-06-23). So there is nothing track-specific to encode and no reason to
# resolve the track on the warm-cache path; the now-view already shows the track.
_LRCLIB_CONTRIBUTE_URL = "https://lrclibup.boidu.dev/"  # no-install browser uploader


@router.get("/api/lyrics")
async def lyrics(track_id: str = Query(..., max_length=128)):
    validate_plex_id(track_id)
    result = lyrics_cache.cached(track_id)
    if result is None:
        client = await state.get_plex_client()
        if not client:
            return dict(lyrics_cache.MISS)  # additive: no Plex → no lyrics, not a 503 (uncached)
        try:
            track = await client.get_track(track_id)
            dur_s = (track.duration_ms / 1000) if track.duration_ms else None
        except Exception:
            # Plex resolve failed — return a miss but DON'T cache it (a later play retries).
            return dict(lyrics_cache.MISS)
        # get_or_fetch handles the cache, the in-flight guard, and the transient-vs-
        # definitive caching decision; it returns an (uncached) miss on transient failure.
        result = await lyrics_cache.get_or_fetch(track_id, track.artist, track.title, track.album, dur_s)
    return await _maybe_attach_contribute(result)


async def _maybe_attach_contribute(result: dict) -> dict:
    """On a CONFIRMED no-match with the admin toggle on, return a COPY of ``result``
    carrying the constant ``contribute`` link; otherwise return ``result`` unchanged.
    Never mutates the cached dict (contribute-prompt plan U3)."""
    from app import database
    if not result.get("no_match") or result.get("instrumental"):
        return result
    if await database.get_setting("lyrics_contribute_enabled") == "0":
        return result  # default ON: only an explicit "0" suppresses the prompt
    return {**result, "contribute": {"url": _LRCLIB_CONTRIBUTE_URL}}


# ── Rail mode (public read) ───────────────────────────────────────────────────

@router.get("/api/rail-mode")
async def get_rail_mode():
    """Public read-only endpoint exposing the install-wide rail_mode setting.
    A single-key endpoint keeps the auth surface tight (no leaking the full
    /admin/settings dict to unauthenticated callers). Defaults to 'vanilla'
    (2026-06-09 rail plan R7) when the setting key is absent."""
    from app import database
    return {"rail_mode": await database.get_setting("rail_mode") or "vanilla"}


# ── Appearance defaults (2026-06-11 glow-up plan U1) ─────────────────────────
# Scheme ids are the server-side canon (names/colors are frontend data in
# static/shared.js's SCHEMES table — keep the id lists in lockstep).

SCHEME_IDS = (
    "gold-rush", "king-crimson", "case-of-blue", "onion-green",
    "ladyland-orange", "chasing-rabbits", "sympathy-lime", "pink-side",
    "silver-mountains", "rainy-purple",
    # 2026-06-14 scheme expansion — background recolors, gradients, light themes.
    # ladyland-orange id is UNCHANGED (only its display name became "Ladyland").
    "dark-side", "bloody-pink", "tubular-blue", "peel-slowly", "inertia", "medusa",
)
RAIL_MODES = ("vanilla", "magnetic", "waveform", "loupe", "vu")
VIEW_MODES = ("list", "tile")


def _resolve_scheme(raw: str | None) -> str:
    return raw if raw in SCHEME_IDS else "gold-rush"


def _resolve_rail_mode(raw: str | None) -> str:
    # Density retired (R3): stored 'density' renders Waveform — mapped at
    # every read edge, never rewritten in the DB.
    if raw == "density":
        return "waveform"
    return raw if raw in RAIL_MODES else "vanilla"


def _resolve_view(raw: str | None) -> str:
    # Tile-view plan (2026-06-15) U1: install-wide default browse/search view.
    # Unknown/unset → list (the shipped default).
    return raw if raw in VIEW_MODES else "list"


RATING_STYLES = ("stars", "dots", "bars")


def _resolve_rating_style(raw: str | None) -> str:
    # Rating display style (2026-06-27): how the 0–5 rating renders everywhere
    # (guest read-only, admin editable control, Highest Rated leaderboard).
    # Install-wide, applied client-side as a :root[data-rating-style] attribute;
    # reload-only (deliberately NOT broadcast — see plan R7). Unknown/unset →
    # stars (the default).
    return raw if raw in RATING_STYLES else "stars"


SURPRISE_SOURCE_MODES = ("auto", "plex", "heuristic", "random")


def _resolve_surprise_enabled(raw: str | None) -> bool:
    # Surprise Me (2026-06-17) is ON by default: only an explicit "0" disables it.
    return raw != "0"


def _resolve_surprise_mode(raw: str | None) -> str:
    # Unknown/unset → auto (full Plex→heuristic→random chain).
    return raw if raw in SURPRISE_SOURCE_MODES else "auto"


SURPRISE_DIVERSITY_MODES = ("off", "album", "artist")


def _resolve_surprise_diversity(raw: str | None) -> str:
    # Diversity gate (2026-06-17 plan 003): default ARTIST (max variety). "off"
    # reproduces the legacy album-walk; unknown/unset → artist.
    return raw if raw in SURPRISE_DIVERSITY_MODES else "artist"


@router.get("/api/appearance")
async def get_appearance():
    """Public appearance defaults — the shared engine's single fetch.
    Same no-auth posture as /api/pattern-rules."""
    from app import database
    return {
        "scheme": _resolve_scheme(await database.get_setting("default_scheme")),
        "rail_mode": _resolve_rail_mode(await database.get_setting("rail_mode")),
        "view": _resolve_view(await database.get_setting("default_view")),
        # Surprise Me button visibility (2026-06-17): the shared playback module
        # reads this to show/hide the Now-dock button. The source MODE is
        # admin-only (see /admin/settings) — guests only need the on/off flag.
        "surprise_me_enabled": _resolve_surprise_enabled(
            await database.get_setting("surprise_me_enabled")
        ),
        # International rail (2026-06-22 plan 004): the shared browse module reads
        # these to build a data-derived rail on the guest page too. Public — only
        # the rail layout knobs ride here, not the admin-only settings dict.
        "rail_alpha_mode": await database.get_rail_alpha_mode(),
        "rail_artist_threshold": await database.get_rail_artist_threshold(),
        "rail_album_threshold": await database.get_rail_album_threshold(),
        # Track ratings + tags (2026-06-26 plan U4): the shared module reads these
        # to decide whether to render ratings/tags and whether to wire the Highest
        # Rated tab + sort for guests. The flags are not the data — the actual
        # rating/tag/leaderboard DATA is withheld server-side on the guest read
        # paths when off (plan R8). browse_facets gates the toggleable guest tabs.
        "ratings_visible_to_guests": await database.get_ratings_visible_to_guests(),
        "tags_visible_to_guests": await database.get_tags_visible_to_guests(),
        "browse_facets": await database.get_browse_facets(),
        # Rating display style (2026-06-27): the shared appearance engine reads
        # this once on load and sets :root[data-rating-style]; CSS in rail.css
        # restyles every .trk-pip. Reload-only (not in the appearance broadcast).
        "rating_style": _resolve_rating_style(await database.get_setting("rating_style")),
    }


@router.get("/api/servers")
async def get_servers():
    """Public server metadata for source-priority ranking (collected-
    library plan U1). Names already appear in guest track payloads;
    `owned` is 1/0, or null for servers linked before ownership was
    persisted (rank treats null as unknown-last)."""
    from app import database
    servers = await database.get_plex_servers()
    return [
        {"name": s["name"],
         "owned": None if s.get("owned") is None else bool(s["owned"])}
        for s in servers
    ]


def _server_rank_key(meta: dict):
    """Priority sort key (R4): owned servers first, known-unowned next,
    unknown last; alphabetical within each band. MIRRORED in the JS rank
    twin in static/browse/index.js — keep semantics in lockstep (the
    shared contract is the vector test in tests/test_api_guest.py)."""
    owned = meta.get("owned")
    band = 0 if owned in (1, True) else (1 if owned in (0, False) else 2)
    return (band, (meta.get("name") or "").lower())


@router.get("/api/pattern-rules")
async def get_pattern_rules_public():
    """Public read-only pattern rules for the shared browse module's
    sort/bucket normalization (rail-mode pattern). Only VALID rules are
    served — inert ones (fewer than two filled strings) are filtered here
    so the frontend never carries validation logic."""
    from app import database
    from app.normalize import valid_rules
    return {"rules": valid_rules(await database.get_pattern_rules())}


# ── Browse ────────────────────────────────────────────────────────────────────

def _log_per_lib_failures(libs: list, results: list, op: str) -> int:
    """Log a WARNING per library that raised in `asyncio.gather(..., return_exceptions=True)`.

    Returns the count of failures so callers can decide whether to escalate
    a total-failure response (503) vs return the partial union.
    """
    failures = 0
    for lib, batch in zip(libs, results):
        if isinstance(batch, BaseException):
            failures += 1
            _log.warning(
                "browse %s failed for library %s (%s): %s",
                op, getattr(lib, "server_name", "?"), getattr(lib, "key", "?"),
                type(batch).__name__,
            )
    return failures


async def _compiled_rules():
    """Per-request compiled pattern rules (2026-06-10 pattern-rules plan U2).
    Cheap: one settings read + list comprehension; rosters are small."""
    from app import database
    from app.normalize import compile_rules
    return compile_rules(await database.get_pattern_rules())


def _norm(s: str | None, compiled) -> str:
    from app.normalize import normalize
    return normalize(s, compiled).strip()


def _dedup_artists(artists: list, compiled=()) -> list:
    seen: dict[str, int] = {}
    result = []
    for a in artists:
        key = _norm(a.title, compiled)
        if key not in seen:
            seen[key] = len(result)
            result.append(a)
        else:
            # Same-titled artist in another enabled library: the survivor's
            # single-library childCount would understate the drill-in union
            # (browse_artist_albums merges every enabled library), so the
            # count is SUPPRESSED — no count is more honest than a wrong one
            # (2026-06-09 rail plan U5). replace() rather than mutation: the
            # Artist objects live in the per-client cache and the suppression
            # must not leak into other library combinations.
            idx = seen[key]
            if getattr(result[idx], 'release_count', None) is not None:
                result[idx] = dataclasses.replace(result[idx], release_count=None)
    return result


def _srv_rank(order: dict, name: str):
    """Sort key for a server name against the persisted priority order.
    Unknown names (legacy/bare configs) rank after every known server,
    alphabetically among themselves."""
    if name in order:
        return (0, order[name], "")
    return (1, 0, (name or "").lower())


async def _ranked_server_order() -> dict[str, int]:
    """server_name -> priority index (R4: owned first, known-unowned next,
    unknown last, alphabetical within bands — see _server_rank_key)."""
    from app import database
    servers = await database.get_plex_servers()
    ranked = sorted(servers, key=_server_rank_key)
    return {s["name"]: i for i, s in enumerate(ranked)}


def _group_albums(tagged: list, compiled=(), order: dict | None = None) -> list:
    """Content-aware cross-server album dedup (same-title plan U4, R2–R5).

    `tagged` is (Album, server_name) pairs in render order. Albums collapse
    across servers ONLY when they are the same RELEASE — same normalized identity
    (title|artist) AND same track_count. Distinct same-title releases (different
    masters/editions Plex filed under one title) stay separate rows; copies that
    repeat on a single server all survive (R2). Each release sub-group emits the
    highest-ranked server's copies, so a release present only on a non-priority
    server still gets a row rather than being hidden (R5). Rows carry
    sources=[{server_name, album_id}] (one matching copy per server in THIS
    release, priority-ordered) for Play From Source… / Queue Release routing.

    A track_count of None (count unknown — stale index, or a surface like search
    that doesn't carry it) buckets together per identity, reproducing the prior
    title-only grouping for those copies. dataclasses.replace keeps client-cache
    objects unmutated."""
    order = order or {}
    # identity (title|artist) → release bucket (track_count) → {server: [albums]},
    # all insertion-ordered so render order is preserved.
    groups: dict[str, dict] = {}
    for album, srv in tagged:
        key = _norm(album.title, compiled) + '|' + _norm(album.artist, compiled)
        releases = groups.setdefault(key, {})
        servers = releases.setdefault(album.track_count, {})
        servers.setdefault(srv or "", []).append(album)
    out = []
    for releases in groups.values():
        for servers in releases.values():
            names = sorted(servers.keys(), key=lambda n: _srv_rank(order, n))
            sources = [{"server_name": n, "album_id": servers[n][0].id} for n in names]
            for copy in servers[names[0]]:
                out.append(dataclasses.replace(copy, sources=sources))
    return out


def _release_chrono_key(album):
    """Chronological release ordering (earliest first), with title as a
    deterministic tiebreak for same-year releases. Unknown year (None) sorts
    first via ``year or 0`` — matching the live get_albums path
    (app/plex/client.py), so the warm-index and live fallbacks agree.

    Why this exists: the warm browse index (get_browse_albums_for_artist) has no
    ORDER BY and _group_albums preserves insertion order, so an artist's own
    releases would otherwise render in crawl order rather than chronologically
    (release-ordering bug 2026-06-23)."""
    return (album.year or 0, (album.title or "").casefold())


_enabled_libs_cache: list | None = None
_enabled_libs_cache_at: float = 0.0
_ENABLED_LIBS_TTL = 30.0
# Single-flight background refresh task (stale-while-revalidate below).
_enabled_libs_refresh_task: asyncio.Task | None = None
# Bumped by state.invalidate_plex_client() on any source reconfiguration. A
# refresh that began BEFORE the bump is fetching the old source set, so it must
# not write its now-stale result back over the just-cleared cache (2026-07-18
# review: the cache-resurrection race). Mirrors state._ondeck_gen.
_enabled_libs_gen: int = 0


async def _refresh_enabled_libraries() -> list:
    """The actual refresh: PMS library listing filtered to the enabled set.
    Updates the module cache on success; on failure the STALE cache survives
    and the failure is logged (2026-07-17 ce-debug: the old silent in-path
    refresh let one dead server tax every 30s-expiry caller 15 seconds with
    zero log evidence)."""
    global _enabled_libs_cache, _enabled_libs_cache_at
    gen = _enabled_libs_gen
    try:
        from app import database
        client = await state.get_plex_client()
        if not client:
            return _enabled_libs_cache or []
        enabled_keys = {lib["section_key"] for lib in await database.get_effective_enabled_libraries()}
        all_libs = await client.get_libraries()
        result = [lib for lib in all_libs if lib.key in enabled_keys]
    except Exception:
        _log.warning("enabled-libraries refresh failed — serving stale list",
                     exc_info=True)
        return _enabled_libs_cache or []
    if gen != _enabled_libs_gen:
        # A source reconfiguration landed while we were fetching — this result is
        # for the OLD source set. Drop it rather than resurrecting a cache that
        # invalidate_plex_client() just cleared (2026-07-18 review).
        return _enabled_libs_cache or []
    _enabled_libs_cache = result
    _enabled_libs_cache_at = time.monotonic()
    return result


async def enabled_libraries() -> list:
    """Enabled PMS libraries, cached with STALE-WHILE-REVALIDATE semantics.

    A fresh cache is returned directly. An EXPIRED cache is ALSO returned
    directly — the refresh runs in a single-flight background task, so the
    request path never blocks on a PMS listing call (2026-07-17 ce-debug:
    the in-path refresh + a blackholed second Plex server = every guest
    search after the 30s TTL stalling ~15s, which presented as "guest search
    always slow, admin always fast" purely through human search pacing).
    Only the very first call of the process (no cache at all) blocks."""
    global _enabled_libs_refresh_task
    if _enabled_libs_cache is not None:
        if (time.monotonic() - _enabled_libs_cache_at) >= _ENABLED_LIBS_TTL:
            if _enabled_libs_refresh_task is None or _enabled_libs_refresh_task.done():
                _enabled_libs_refresh_task = asyncio.create_task(
                    _refresh_enabled_libraries())
                _enabled_libs_refresh_task.add_done_callback(state._log_task_exc)
        return _enabled_libs_cache
    return await _refresh_enabled_libraries()


def warm_enabled_libraries() -> None:
    """Fire the enabled-libraries refresh at startup (fire-and-forget) so the
    first guest search finds a warm cache instead of paying the cold in-path
    listing block (2026-07-18 review). Shares the single-flight task guard with
    enabled_libraries(); a no-op if a refresh is already in flight."""
    global _enabled_libs_refresh_task
    if _enabled_libs_refresh_task is None or _enabled_libs_refresh_task.done():
        _enabled_libs_refresh_task = asyncio.create_task(_refresh_enabled_libraries())
        _enabled_libs_refresh_task.add_done_callback(state._log_task_exc)


async def _catalog_active() -> bool:
    """True when browse/search should serve the merged catalog floor rather than
    Plex's native pipeline (plan U8/R15).

    The native pipeline is Plex-specific AND already merges multiple Plex servers
    (cross-server dedup lives there), and the registry holds one ``PlexSource``
    PER server — so source COUNT is not the signal. It stays in charge whenever
    every source is Plex, regardless of how many servers (AE6 parity). The
    catalog floor takes over only once a NON-Plex source (Jellyfin/local) is
    connected. The predicate lives in ``state.catalog_active`` so the source-
    neutral random/Surprise/genre paths there (U13) share one definition; this
    thin wrapper keeps the many guest call sites unchanged."""
    return await state.catalog_active()


@router.get("/api/scan-status")
async def scan_status():
    """Catalog/scan state for the guest onboarding empty-states (plan U15/R19/R20):
    ``{sources, scanning, scanned, empty}``. Public like the rest of browse — it
    carries no library content, only counts/flags. The browse module consults it
    only when a list comes back empty, to pick which empty state to show."""
    return await state.scan_status()


@router.get("/api/browse/artists")
async def browse_artists():
    if await _catalog_active():
        from app.catalog import views
        return await views.artists()
    client = await state.get_plex_client()
    if not client:
        raise HTTPException(status_code=503, detail="No media source configured")
    from app import database
    # Browse-index plan U3: the persistent index is the data source; the live
    # per-library fan-out is the fallback when the index is empty/cold (R10).
    # Either way the SAME dedup/credit/exclusion pipeline below runs unchanged,
    # so request-time policy (R11) is untouched by where the rows came from.
    index_rows = await database.get_browse_artists()
    partial_failure = False
    if index_rows:
        # Stale-while-revalidate: serve instantly, refresh in the background
        # only when actually stale (gentle-on-Plex, mirrors browse_genres).
        if not await state.cache_is_fresh("browse_index_computed_at"):
            state.trigger_browse_index_refresh()
        artists = [
            Artist(id=r["artist_id"], title=r["title"], thumb=r["thumb"],
                   release_count=r["release_count"])
            for r in index_rows
        ]
    else:
        # Cold/empty index: self-heal in the background, serve live this time.
        state.trigger_browse_index_refresh()
        libs = await enabled_libraries()
        results = await asyncio.gather(*[client.get_artists(lib.key) for lib in libs], return_exceptions=True)
        artists = [a for batch in results if not isinstance(batch, BaseException) for a in batch]
        # Total-failure escalation: when every enabled library raised AND zero
        # results came back, that's an outage, not an empty library — return 503
        # so the frontend retries rather than rendering "no artists found".
        if libs and not artists:
            failures = _log_per_lib_failures(libs, results, "artists")
            if failures == len(libs):
                raise HTTPException(status_code=503, detail="All libraries unreachable")
        partial_failure = any(isinstance(b, BaseException) for b in results)
    compiled = await _compiled_rules()
    deduped = _dedup_artists(artists, compiled)
    # Partial-failure count suppression (2026-06-09 rail plan U5 policy):
    # if any library's batch failed, dedupe couldn't see its artists — a
    # same-titled artist in the failed library would normally have
    # suppressed the survivor's count. Serving single-library counts from a
    # partial union risks exactly the wrong-count display the suppression
    # policy exists to prevent (and the counts would flip-flop as the
    # library blips), so all counts are withheld for this degraded response.
    # (Index-served responses are always whole, so this never fires for them.)
    if partial_failure:
        deduped = [
            dataclasses.replace(a, release_count=None)
            if getattr(a, 'release_count', None) is not None else a
            for a in deduped
        ]
    # Per-track credits plan U3: fire-and-forget index refresh from the
    # consuming path (single-flighted no-op while fresh/in-flight) so an
    # empty cache self-heals, then merge credit-only acts into the roster.
    # An act whose name matches an existing release artist is NOT added —
    # one identity per act (R8); their appearances surface in the drill-in.
    if not await state.cache_is_fresh("credit_cache_computed_at"):
        state.trigger_credit_refresh()
    # va_only (browse-VA-gate, R1): the roster only gains acts with a
    # Various Artists appearance; non-VA collaboration variations stay
    # findable via Search but don't browse.
    credit_acts = await database.get_credit_acts(va_only=True)
    if credit_acts:
        existing = {_norm(a.title, compiled) for a in deduped}
        # Pattern-rules plan U2: group credit acts whose names normalize
        # equal (one identity per act under the rules); first-seen spelling
        # displays, appears-on counts sum.
        groups: dict[str, dict] = {}
        for act in credit_acts:
            key = _norm(act["name"], compiled)
            if key in existing:
                continue
            if key in groups:
                groups[key]["release_count"] += act["release_count"]
            else:
                groups[key] = dict(act)
        if groups:
            deduped = list(deduped) + [_credit_artist_dict(g) for g in groups.values()]
    # Artist Exclusion (pattern-rules plan U3/R8): whole-string,
    # case-insensitive match on the RAW displayed name — not the
    # rule-normalized form — applied LAST so it catches Plex artists and
    # synthesized acts alike. Roster-only: search/drill-ins untouched.
    exclusions = {e.lower().strip() for e in await database.get_artist_exclusions() if e.strip()}
    if exclusions:
        deduped = [
            a for a in deduped
            if (a["title"] if isinstance(a, dict) else (a.title or "")).lower().strip() not in exclusions
        ]
    return deduped


def _credit_artist_dict(act: dict) -> dict:
    """Artist-shaped dict for a credited act with no Plex artist object.

    The reserved `credit:` id prefix routes the drill-in to the index
    (percent-encoded so names like "AC/DC" survive path interpolation)."""
    return {
        "id": "credit:" + quote(act["name"], safe=""),
        "title": act["name"],
        "thumb": None,
        "release_count": act["release_count"],
    }


async def _appearances_for_norm(database, artist_norm: str, compiled) -> list[dict]:
    """Appears-on rows for every credit act whose name normalizes to
    artist_norm (pattern-rules plan U2: equivalent spellings union),
    deduped by album id."""
    rows: dict[str, dict] = {}
    for act in await database.get_credit_acts():
        if _norm(act["name"], compiled) == artist_norm:
            for r in await database.get_credit_appearances(act["name_lower"]):
                rows.setdefault(r["album_id"], r)
    return list(rows.values())


def _appearance_album_dict(row: dict) -> dict:
    """Album-shaped dict for an appears-on release from the credit index."""
    return {
        "id": row["album_id"],
        "title": row["album_title"],
        "artist": row["album_artist"],
        "year": row["album_year"],
        "thumb": row["album_thumb"],
        "subtype": "appears_on",
    }


async def _assemble_artist_releases(artist_id: str, client, compiled):
    """Shared artist-releases assembly behind /albums and /songs (All Songs U2).

    Returns ``(own, appears, artist_norm)`` where ``own`` is the cross-server
    de-duplicated list of the artist's own ``Album`` objects, ``appears`` is the
    list of appears-on release dicts (per-track-credited compilations/features,
    minus any that are already own releases), and ``artist_norm`` is the resolved
    normalized name (``None`` when a real artist id resolves to nothing). For a
    synthesized ``credit:`` act, ``own`` is empty and ``artist_norm`` is the name's
    norm. Callers own the HTTP guards (length cap, validate_plex_id, 404)."""
    from app import database
    if artist_id.startswith("credit:"):
        name = unquote(artist_id[len("credit:"):])
        norm = _norm(name, compiled)
        rows = await _appearances_for_norm(database, norm, compiled)
        return [], [_appearance_album_dict(r) for r in rows], norm
    # Browse-index plan U4: resolve own releases via the index — an O(1) id
    # lookup plus an indexed album fetch — instead of re-loading every library's
    # full artist list. Index miss falls through to the live fan-out below (R10).
    arow = await database.get_browse_artist_by_id(artist_id)
    if arow is not None:
        artist_norm = _norm(arow["title"], compiled)
        base_keys = {arow["base_key"]}
        # Rule-grouping plan U3: when rules can merge different base spellings (the
        # default ruleset does), union every sibling base-key. FAST path — a
        # signature-guarded in-memory map (rebuilt off index-refresh + rule-save,
        # plan U1/U2) gives the sibling set in one O(1) lookup. FALLBACK — if the
        # map isn't current (startup, mid-rebuild, just-changed rules), scan the
        # roster once and trigger a rebuild; results are identical either way, the
        # signature guard makes that structural (plan R4/R5). With no rules
        # configured, base_key already equals the rule-norm group, so neither path
        # runs (R9).
        if compiled:
            grouping = state.get_artist_grouping(
                (state.browse_index_gen(), state.rules_sig(compiled))
            )
            if grouping is not None:
                base_keys = set(grouping.get(artist_norm, base_keys))  # copy: don't mutate the cached set
            else:
                for r in await database.get_browse_artists():
                    if r["base_key"] not in base_keys and _norm(r["title"], compiled) == artist_norm:
                        base_keys.add(r["base_key"])
                state.trigger_artist_grouping_rebuild()
        album_rows: list[dict] = []
        for bk in base_keys:
            album_rows.extend(await database.get_browse_albums_for_artist(bk))
        tagged = [
            (Album(id=r["album_id"], title=r["title"], artist=r["artist"],
                   year=r["year"], thumb=r["thumb"], subtype=r["subtype"],
                   track_count=r["track_count"]),
             r["server_name"])
            for r in album_rows
        ]
        deduped = _group_albums(tagged, compiled, await _ranked_server_order())
        deduped.sort(key=_release_chrono_key)  # chronological own releases (parity with live path)
        rows = await _appearances_for_norm(database, artist_norm, compiled)
        own_ids = {a.id for a in deduped}
        appears = [_appearance_album_dict(r) for r in rows if r["album_id"] not in own_ids] if rows else []
        return list(deduped), appears, artist_norm
    libs = await enabled_libraries()
    # Fan out artist lists across every enabled library so we can name-resolve
    # the artist regardless of which server owns artist_id (cross-server
    # ratingKeys are independent; the only reliable merge key is the title).
    per_lib_artists = await asyncio.gather(
        *[client.get_artists(lib.key) for lib in libs], return_exceptions=True
    )
    artist_norm: str | None = None
    for batch in per_lib_artists:
        if isinstance(batch, BaseException):
            continue
        for a in batch:
            if a.id == artist_id:
                artist_norm = _norm(a.title, compiled)
                break
        if artist_norm is not None:
            break
    if artist_norm is None:
        return [], [], None
    matches: list[tuple] = []
    for lib, batch in zip(libs, per_lib_artists):
        if isinstance(batch, BaseException):
            continue
        for a in batch:
            if _norm(a.title, compiled) == artist_norm:
                matches.append((lib, a.id))
    per_lib_albums = await asyncio.gather(
        *[client.get_albums(lib.key, artist_id=aid) for lib, aid in matches],
        return_exceptions=True,
    )
    tagged = [
        (a, lib.server_name) for (lib, _aid), batch in zip(matches, per_lib_albums)
        if not isinstance(batch, BaseException) for a in batch
    ]
    deduped = _group_albums(tagged, compiled, await _ranked_server_order())
    deduped.sort(key=_release_chrono_key)  # chronological own releases (parity with index path)
    # Appears On (credits plan U3/R6): deduped BY ALBUM ID against own releases.
    rows = await _appearances_for_norm(database, artist_norm, compiled)
    own_ids = {a.id for a in deduped}
    appears = [_appearance_album_dict(r) for r in rows if r["album_id"] not in own_ids] if rows else []
    return list(deduped), appears, artist_norm


@router.get("/api/browse/artists/{artist_id}/albums")
async def browse_artist_albums(artist_id: str):
    if await _catalog_active():
        from app.catalog import views
        return await views.artist_albums(artist_id)
    # Per-track credits plan U3: synthesized acts bypass validate_plex_id on
    # the reserved prefix; the branch carries its own guards — length cap in
    # place of the validator's, and 200 + [] for an unknown name (the frontend's
    # existing "No releases." path; review decision over a new 404 error state).
    if artist_id.startswith("credit:"):
        if len(artist_id) > 256:
            raise HTTPException(status_code=400, detail="Invalid resource ID")
    else:
        validate_plex_id(artist_id)
    client = await state.get_plex_client()
    if not client:
        raise HTTPException(status_code=503, detail="No media source configured")
    compiled = await _compiled_rules()
    own, appears, artist_norm = await _assemble_artist_releases(artist_id, client, compiled)
    if artist_norm is None:
        raise HTTPException(status_code=404, detail="Artist not found")
    return own + appears


@router.get("/api/browse/artists/{artist_id}/songs")
async def browse_artist_songs(artist_id: str):
    """All Songs (plan 007): the artist's tracks across own + appears-on
    releases, enriched for the client to group/dedup/sort. Own releases
    contribute all children; appears-on releases (VA comps) are filtered to
    only the tracks crediting this artist. See
    docs/plans/2026-06-17-007-feat-artist-all-songs-plan.md."""
    if await _catalog_active():
        # Return the SAME payload shape as the native branch below — the All-Songs
        # frontend reads data.tracks / data.releases / data.popular_available; a
        # bare list renders "No songs." (ce-debug 2026-06-29). releases = the
        # artist's own albums; popularity is a Plex specialization the floor lacks.
        from app import database
        from app.catalog import store, views
        artist = await store.get_artist(artist_id)
        if not artist:
            return {"popular_available": False, "releases": [], "tracks": []}
        releases = [{"id": a["identity"], "title": a["title"], "year": a["year"], "kind": "own"}
                    for a in await store.get_albums_for_artist(artist["base_key"])]
        songs = await views.artist_songs(artist_id)
        tracks = [{**_track_dict(t), "release": t.album, "release_year": t.year,
                   "kind": "own", "pop_rank": None}
                  for t in songs]
        # plex_held from the holds views.artist_songs just loaded (plan U4 —
        # no second holds pass).
        await _annotate_plex_held(tracks, songs)
        counts = await database.get_play_counts("track", [t["track_id"] for t in tracks])
        for t in tracks:
            t["plays"] = counts.get(t["track_id"], 0)
        # Popular fold-in (plan U13): popularity is a Plex specialization, but a
        # Plex-backed artist in a MIXED install should still get it. When this
        # catalog artist has a Plex hold, decorate the catalog tracks with Plex
        # popularity ranks; local/Jellyfin-only artists keep popular_available
        # False (no popularity signal). Same title-matching as the native branch.
        popular_available = await _decorate_plex_popularity(artist_id, tracks)
        return {"popular_available": popular_available, "releases": releases, "tracks": tracks}
    if artist_id.startswith("credit:"):
        if len(artist_id) > 256:
            raise HTTPException(status_code=400, detail="Invalid resource ID")
    else:
        validate_plex_id(artist_id)
    client = await state.get_plex_client()
    if not client:
        raise HTTPException(status_code=503, detail="No media source configured")
    compiled = await _compiled_rules()
    own, appears, artist_norm = await _assemble_artist_releases(artist_id, client, compiled)
    if artist_norm is None:
        raise HTTPException(status_code=404, detail="Artist not found")

    # Releases in display order: own first, then appears-on.
    releases = [{"id": a.id, "title": a.title, "year": a.year, "kind": "own"} for a in own]
    releases += [{"id": r["id"], "title": r["title"], "year": r.get("year"), "kind": "appears"} for r in appears]

    # One children fetch per release, routed by the (compound) release id, under
    # the client's per-server concurrency cap; fail-soft per release.
    async def _children(rel_id):
        try:
            return await client.get_tracks(rel_id, album_id=rel_id)
        except Exception:
            return []
    track_lists = await asyncio.gather(*[_children(r["id"]) for r in releases])

    # Emit full track-row dicts (so the shared renderer can render + queue them)
    # plus the All-Songs extras the client groups/sorts on: release, release_year,
    # kind. `_track_dict` supplies track_id/title/artist/album/album_id/thumb/
    # duration/server_name/disc/track for the shared track-row renderer.
    tracks: list[dict] = []
    for rel, tlist in zip(releases, track_lists):
        for t in tlist:
            # Appears-on (VA comp) children are all artists' tracks — keep only
            # those crediting this artist (Track.artist = originalTitle/grandparent).
            if rel["kind"] == "appears" and _norm(t.artist, compiled) != artist_norm:
                continue
            tracks.append({
                **_track_dict(t),
                "release": rel["title"],
                "release_year": rel["year"],
                "kind": rel["kind"],
            })
    await _annotate_plex_held(tracks)  # native branch → constant True (plan U4)

    # Most Played source: the app's local play_counts store (NOT Plex viewCount).
    from app import database
    counts = await database.get_play_counts("track", [t["track_id"] for t in tracks])
    for t in tracks:
        t["plays"] = counts.get(t["track_id"], 0)

    # Popular: online-metadata leaves matched to local tracks by normalized title
    # (online keyspace differs from local ids). Contiguous rank over unique titles.
    popular = [] if artist_id.startswith("credit:") else await client.get_artist_popular_tracks(artist_id)
    pop_by_norm: dict[str, int] = {}
    for p in popular:
        nk = _norm(p.get("title"), compiled)
        if nk and nk not in pop_by_norm:
            pop_by_norm[nk] = len(pop_by_norm)
    matched = False
    for t in tracks:
        rank = pop_by_norm.get(_norm(t["title"], compiled))
        t["pop_rank"] = rank
        if rank is not None:
            matched = True

    return {
        "popular_available": matched,
        "releases": releases,
        "tracks": tracks,
    }


async def _decorate_plex_popularity(artist_identity: str, tracks: list[dict]) -> bool:
    """Rank catalog ``tracks`` by Plex popularity when this artist is Plex-backed
    (plan U13 fold-in), and report whether any matched.

    Reuses the native All-Songs popularity path: fetch the Plex provider's
    popular tracks for the artist and rank the local tracks by normalized title.
    The catalog artist's Plex hold carries the Plex artist key as its
    ``provider_local_key``; routing ``get_artist_popular_tracks`` through the
    registry on that key reaches the Plex source. No-op (returns False, leaves
    ``pop_rank`` None) when the artist has no Plex hold or Plex returns nothing —
    Jellyfin/local have no popularity signal, so those artists stay
    ``popular_available`` False (correct degradation, not a regression)."""
    from app.catalog import store, views
    types = await views._source_types()
    holds = await store.get_holds("artist", artist_identity)
    plex_hold = next((h for h in holds if types.get(h["source_id"]) == "plex"), None)
    if not plex_hold:
        return False
    client = await state.get_plex_client()
    try:
        popular = await client.get_artist_popular_tracks(plex_hold["provider_local_key"])
    except Exception:
        return False
    compiled = await _compiled_rules()
    pop_by_norm: dict[str, int] = {}
    for p in popular:
        nk = _norm(p.get("title"), compiled)
        if nk and nk not in pop_by_norm:
            pop_by_norm[nk] = len(pop_by_norm)
    matched = False
    for t in tracks:
        rank = pop_by_norm.get(_norm(t["title"], compiled))
        t["pop_rank"] = rank
        if rank is not None:
            matched = True
    return matched


@router.get("/api/browse/albums")
async def browse_albums():
    if await _catalog_active():
        from app.catalog import views
        return await views.albums()
    client = await state.get_plex_client()
    if not client:
        raise HTTPException(status_code=503, detail="No media source configured")
    from app import database
    # Browse-index plan U3: index-first with live fallback (see browse_artists).
    index_rows = await database.get_browse_albums()
    if index_rows:
        if not await state.cache_is_fresh("browse_index_computed_at"):
            state.trigger_browse_index_refresh()
        tagged = [
            (Album(id=r["album_id"], title=r["title"], artist=r["artist"],
                   year=r["year"], thumb=r["thumb"], subtype=r["subtype"],
                   track_count=r["track_count"]),
             r["server_name"])
            for r in index_rows
        ]
    else:
        state.trigger_browse_index_refresh()
        libs = await enabled_libraries()
        results = await asyncio.gather(*[client.get_albums(lib.key) for lib in libs], return_exceptions=True)
        tagged = [
            (a, lib.server_name) for lib, batch in zip(libs, results)
            if not isinstance(batch, BaseException) for a in batch
        ]
        # Total-failure escalation (see browse_artists rationale).
        if libs and not tagged:
            failures = _log_per_lib_failures(libs, results, "albums")
            if failures == len(libs):
                raise HTTPException(status_code=503, detail="All libraries unreachable")
    return _group_albums(tagged, await _compiled_rules(), await _ranked_server_order())


_RECENTLY_ADDED_LIMIT = 100


def _recent_added(tagged: list, compiled=(), order: dict | None = None,
                  limit: int = _RECENTLY_ADDED_LIMIT) -> list:
    """Recently Added feed (plan 006 U3): collapse to ONE row per release
    identity, date each row by the EARLIEST add across its copies, sort
    newest-first (undated last), tiebreak title ascending, cap to `limit`.

    Stricter than _group_albums, which keeps same-server repeats — the feed
    shows each release exactly once (R5). `tagged` is (Album, server_name)
    pairs; the earliest of a group's DATED copies wins (R6), so a copy with no
    add-date never drags a release's date down."""
    order = order or {}
    groups: dict[str, dict] = {}
    seq: list[str] = []
    for album, srv in tagged:
        key = _norm(album.title, compiled) + '|' + _norm(album.artist, compiled)
        g = groups.get(key)
        if g is None:
            g = {"servers": {}, "added": []}
            groups[key] = g
            seq.append(key)
        g["servers"].setdefault(srv or "", []).append(album)
        if album.added_at is not None:
            g["added"].append(album.added_at)
    out = []
    for key in seq:
        g = groups[key]
        servers = g["servers"]
        names = sorted(servers.keys(), key=lambda n: _srv_rank(order, n))
        sources = [{"server_name": n, "album_id": servers[n][0].id} for n in names]
        rep = servers[names[0]][0]
        earliest = min(g["added"]) if g["added"] else None
        out.append(dataclasses.replace(rep, sources=sources, added_at=earliest))
    out.sort(key=lambda a: (a.added_at is None, -(a.added_at or 0), (a.title or "").casefold()))
    return out[:limit]


@router.get("/api/recently-added")
async def recently_added():
    """Newest-added albums merged + deduped across all enabled libraries
    (plan 006). Index-only and DB-backed like /api/most-played: an unpopulated
    index returns [] and triggers a background crawl rather than falling back to
    live per-library Plex calls."""
    from app import database
    index_rows = await database.get_browse_albums()
    if not index_rows:
        state.trigger_browse_index_refresh()
        return []
    if not await state.cache_is_fresh("browse_index_computed_at"):
        state.trigger_browse_index_refresh()
    tagged = [
        (Album(id=r["album_id"], title=r["title"], artist=r["artist"],
               year=r["year"], thumb=r["thumb"], subtype=r["subtype"],
               added_at=r["added_at"]),
         r["server_name"])
        for r in index_rows
    ]
    return _recent_added(tagged, await _compiled_rules(), await _ranked_server_order())


def _select_release_copies(arow: dict, copies: list, clicked_id: str) -> list:
    """Per-release copy selection for album→tracks (same-title plan U5, R1/R4/R6).

    Returns the index rows whose tracks make up THE clicked release: always the
    clicked copy itself; never a same-server sibling (R1 — unioning those is the
    tripling bug); and, for each OTHER server, the unique copy whose track_count
    matches the clicked release. Zero or multiple matches on a server is
    ambiguous (e.g. identical masters), so that server is skipped and the clicked
    copy's own tracks stand alone (R6). A clicked release with unknown count
    (stale index) folds nothing — own tracks only."""
    own_server = (arow.get("server_name") or "").lower().strip()
    own_count = arow.get("track_count")
    selected = [arow]
    by_server: dict[str, list] = {}
    for c in copies:
        if c["album_id"] == clicked_id:
            continue  # the clicked copy is already included
        if (c.get("server_name") or "").lower().strip() == own_server:
            continue  # same-server sibling — never unioned (R1)
        by_server.setdefault((c.get("server_name") or "").lower().strip(), []).append(c)
    if own_count is not None:
        for group in by_server.values():
            matches = [c for c in group if c.get("track_count") == own_count]
            if len(matches) == 1:
                selected.append(matches[0])
    return selected


async def _resolve_album_tracks(client, album_id: str, *, source_server_name: str | None = None) -> list:
    """Resolve a shared album to its full track list across enabled libraries.

    Cross-server Plex ratingKeys are independent, so we look up the album's
    (title, artist) once, then name-match across every enabled library that
    holds the same artist+album. Returns raw `Track` objects so callers can
    decide whether to serialize via `_track_dict` (browse) or enqueue (queue).

    Raises:
        KeyError: from `client.get_album` when album_id is unknown.

    When `source_server_name` is provided, libraries are filtered to those
    whose `server_name` matches case-insensitively after trimming.
    """
    from app import database
    # Browse-index plan U4: resolve the release's per-server copies via the
    # index (id → identity → sibling rating-keys) and fetch tracks by exact
    # rating-key (/children, reliable for tracks). No artist/album re-scan.
    # Index miss falls through to the live name-resolution below (R10).
    arow = await database.get_browse_album_by_id(album_id)
    if arow is not None:
        copies = await database.get_browse_albums_by_identity(
            arow["title_base"], arow["artist_base_key"]
        )
        selected = _select_release_copies(arow, copies, album_id)
        if source_server_name and source_server_name.strip():
            wanted = source_server_name.lower().strip()
            selected = [c for c in selected if (c["server_name"] or "").lower().strip() == wanted]
        per_album_tracks = await asyncio.gather(
            *[client.get_tracks(c["section_key"], album_id=c["album_id"]) for c in selected],
            return_exceptions=True,
        )
        return [
            t for batch in per_album_tracks
            if not isinstance(batch, BaseException) for t in batch
        ]
    album = await client.get_album(album_id)
    title_lower = (album.title or "").lower().strip()
    artist_lower = (album.artist or "").lower().strip()
    own_count = album.track_count
    all_libs = await enabled_libraries()
    # Treat empty / whitespace-only source_server_name as "no filter" — frontend
    # may pass "" when the picker collapses to the unfiltered branch.
    if source_server_name and source_server_name.strip():
        wanted = source_server_name.lower().strip()
        libs = [lib for lib in all_libs if (lib.server_name or "").lower().strip() == wanted]
    else:
        libs = all_libs
    per_lib_artists = await asyncio.gather(
        *[client.get_artists(lib.key) for lib in libs], return_exceptions=True
    )
    artist_matches: list[tuple[str, str]] = []
    for lib, batch in zip(libs, per_lib_artists):
        if isinstance(batch, BaseException):
            continue
        for a in batch:
            if (a.title or "").lower().strip() == artist_lower:
                artist_matches.append((lib.key, a.id))
    per_lib_albums = await asyncio.gather(
        *[client.get_albums(lk, artist_id=aid) for lk, aid in artist_matches],
        return_exceptions=True,
    )
    album_matches: list[tuple[str, str, int | None]] = []
    for (lk, _), batch in zip(artist_matches, per_lib_albums):
        if isinstance(batch, BaseException):
            continue
        for alb in batch:
            if (alb.title or "").lower().strip() == title_lower:
                album_matches.append((lk, alb.id, alb.track_count))
    # Per-release gate (same-title plan U5, R1/R6) — mirrors the index path:
    # the clicked album always; never a same-library sibling (the tripling bug);
    # for OTHER libraries the unique track_count match. When the clicked album's
    # count is unknown (cold metadata), fall back to the historical cross-library
    # title union (same-library siblings still excluded).
    clicked_lib = next((lk for lk, aid, _ in album_matches if aid == album_id), None)
    selected_matches: list[tuple[str, str]] = []
    by_lib: dict[str, list] = {}
    for lk, aid, tc in album_matches:
        if aid == album_id:
            selected_matches.append((lk, aid))
        elif lk == clicked_lib:
            continue  # same-library sibling — never unioned (R1)
        else:
            by_lib.setdefault(lk, []).append((aid, tc))
    for lk, cands in by_lib.items():
        if own_count is not None:
            matches = [aid for aid, tc in cands if tc == own_count]
            if len(matches) == 1:
                selected_matches.append((lk, matches[0]))
        else:
            selected_matches.extend((lk, aid) for aid, _ in cands)
    per_lib_tracks = await asyncio.gather(
        *[client.get_tracks(lk, album_id=aid) for lk, aid in selected_matches],
        return_exceptions=True,
    )
    return [
        t for batch in per_lib_tracks
        if not isinstance(batch, BaseException) for t in batch
    ]


@router.get("/api/browse/albums/{album_id}/tracks")
async def browse_album_tracks(album_id: str):
    if await _catalog_active():
        from app.catalog import views
        cat_tracks = await views.album_tracks(album_id)
        # plex_held from the holds views.album_tracks just loaded (plan U4).
        return await _annotate_plex_held(
            [_track_dict(t) for t in cat_tracks], cat_tracks)
    validate_plex_id(album_id)
    client = await state.get_plex_client()
    if not client:
        raise HTTPException(status_code=503, detail="No media source configured")
    try:
        tracks = await _resolve_album_tracks(client, album_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Album not found")
    return await _annotate_plex_held([_track_dict(t) for t in tracks])


@router.get("/api/browse/genres")
async def browse_genres():
    from app import database
    catalog = await _catalog_active()
    cached = await database.get_genre_cache()
    if cached:
        # Stale-while-revalidate, but only when actually stale (gentle-on-Plex
        # U2): a warm+fresh cache returns with zero Plex work.
        if not await state.cache_is_fresh("genre_cache_computed_at"):
            state.trigger_genre_refresh()
        # Per-track credits are a Plex specialization (U13 capability
        # degradation): only refresh that cache in a native (Plex-only) install.
        if not catalog and not await state.cache_is_fresh("credit_cache_computed_at"):
            state.trigger_credit_refresh()
        return cached

    # Cold cache, catalog floor (U13): compute genres from the merged catalog's
    # track tags rather than Plex styles, then stamp so the next read is warm.
    if catalog:
        from app.catalog import views
        merged = await views.genres()
        _t = asyncio.create_task(database.set_genre_cache(merged))
        _t.add_done_callback(lambda t: t.exception() if not t.cancelled() and t.exception() else None)
        await state.stamp_cache("genre_cache_computed_at")
        return merged

    client = await state.get_plex_client()
    if not client:
        raise HTTPException(status_code=503, detail="No media source configured")
    libs = await enabled_libraries()
    results = await asyncio.gather(
        *[client.get_styles_with_counts(lib.key) for lib in libs], return_exceptions=True
    )
    # Merge across libraries: sum counts for same style name (case-insensitive key)
    counts: dict[str, int] = {}
    names: dict[str, str] = {}  # normalised_key -> display name (first seen wins)
    for batch in results:
        if isinstance(batch, BaseException):
            continue
        for item in batch:
            norm = item["name"].lower()
            counts[norm] = counts.get(norm, 0) + item["count"]
            names.setdefault(norm, item["name"])
    merged = [{"name": names[k], "count": v} for k, v in counts.items() if v > 0]
    merged.sort(key=lambda x: x["count"], reverse=True)
    _t = asyncio.create_task(database.set_genre_cache(merged))
    _t.add_done_callback(lambda t: t.exception() if not t.cancelled() and t.exception() else None)
    # Stamp freshness so the next read is warm (gentle-on-Plex U2 / R7).
    await state.stamp_cache("genre_cache_computed_at")
    return merged


@router.get("/api/browse/genres/albums")
async def browse_genre_albums(style: str = Query(..., min_length=1)):
    if await _catalog_active():
        # Catalog floor (U13): albums whose tracks carry this genre, as Album
        # objects (with holds) so the shared renderer handles them like any
        # other catalog album list.
        from app.catalog import views
        return await views.genre_albums(style)
    client = await state.get_plex_client()
    if not client:
        raise HTTPException(status_code=503, detail="No media source configured")
    libs = await enabled_libraries()
    results = await asyncio.gather(
        *[client.get_albums(lib.key, style=style) for lib in libs], return_exceptions=True
    )
    tagged = [
        (a, lib.server_name) for lib, batch in zip(libs, results)
        if not isinstance(batch, BaseException) for a in batch
    ]
    return _group_albums(tagged, await _compiled_rules(), await _ranked_server_order())


@router.get("/api/browse/years")
async def browse_years():
    client = await state.get_plex_client()
    if not client:
        raise HTTPException(status_code=503, detail="No media source configured")
    libs = await enabled_libraries()
    results = await asyncio.gather(*[client.get_years(lib.key) for lib in libs], return_exceptions=True)
    return sorted({y for batch in results if not isinstance(batch, BaseException) for y in batch}, reverse=True)


@router.get("/api/browse/years/{year}/albums")
async def browse_year_albums(year: int):
    client = await state.get_plex_client()
    if not client:
        raise HTTPException(status_code=503, detail="No media source configured")
    libs = await enabled_libraries()
    results = await asyncio.gather(*[client.get_albums(lib.key, year=year) for lib in libs], return_exceptions=True)
    tagged = [
        (a, lib.server_name) for lib, batch in zip(libs, results)
        if not isinstance(batch, BaseException) for a in batch
    ]
    # Year lists previously skipped dedup entirely — the collected-library
    # grouping (R3) applies to every album surface.
    return _group_albums(tagged, await _compiled_rules(), await _ranked_server_order())


# ── Search ────────────────────────────────────────────────────────────────────

@router.get("/api/play-counts")
async def browse_play_counts(type: str = Query(...)):
    if type not in ("track", "album", "artist"):
        raise HTTPException(status_code=400, detail="type must be track, album, or artist")
    from app import database
    return await database.get_all_play_counts(type)


async def _refresh_album_drill(rows: list[dict]) -> None:
    """Re-resolve each leaderboard row's album drill target from the CURRENT
    catalog, mutating ``row['metadata']`` in place.

    ``play_track_meta`` snapshots ``album_id`` at play time, but album identities
    are re-clustered on every scan (the album-dedup / self-heal changes re-mint
    them) and ``catalog_album`` is atomic-replaced — so a snapshot captured before
    the latest scan points at an album identity that no longer exists. 'Go to
    Album' then greys out (missing id) or opens a blank release (stale id resolves
    to zero tracks) — ce-debug 2026-07-03. The track identity (the row key) is
    stable, so resolve the drill live: for a still-catalogued track, overlay the
    catalog's current album_id / album / thumb. A track whose source is gone isn't
    in the catalog → its snapshot is left as the best available; on a Plex-only
    install (no catalog) ids are stable rating keys and nothing is touched."""
    if not await _catalog_active():
        return
    from app.catalog import store
    for r in rows:
        meta = r.get("metadata")
        if not meta:
            continue
        ct = await store.get_track(r["track_id"])
        if ct is None:
            continue
        meta["album_id"] = ct["album_identity"]
        meta["album"] = ct["album"] or ""
        if ct.get("thumb") is not None:
            meta["thumb"] = ct["thumb"]


@router.get("/api/most-played")
async def most_played():
    """Top played tracks with display metadata (most-played plan U1).

    DB-backed: meta-captured rows serve even with no Plex client — only
    the live backfill (for counts recorded before metadata capture
    shipped) needs Plex, and unresolvable ids are skipped (R3)."""
    from app import database
    limit = await database.get_most_played_display_limit()
    rows = await database.get_top_played_tracks(limit)
    missing = [r for r in rows if r["metadata"] is None]
    if missing:
        client = await state.get_plex_client()
        if client:
            fetched = await asyncio.gather(
                *[client.get_track(r["track_id"]) for r in missing],
                return_exceptions=True,
            )
            for r, t in zip(missing, fetched):
                if isinstance(t, BaseException):
                    continue  # deleted/unreachable track: skipped (R3)
                r["metadata"] = _track_dict(t)
                # Backfill so the next load is pure-DB (fire-and-forget).
                _bt = asyncio.create_task(database.set_play_track_meta(r["track_id"], r["metadata"]))
                _bt.add_done_callback(state._log_task_exc)
    await _refresh_album_drill(rows)
    # plex_held: identity-mode annotate — metadata rows are RECORD-TIME
    # snapshots; the flag must resolve live from current holds (plan U4).
    return await _annotate_plex_held([
        {**r["metadata"], "play_count": r["count"]}
        for r in rows if r["metadata"] is not None
    ])


# ── Track ratings + tags read paths (2026-06-26 ratings-and-tags plan U3) ────
# Server-side gating (plan R8): guests get rating/tag/leaderboard DATA only when
# the matching visibility flag is on; the admin (valid session) always gets the
# full data so they can curate. The shared browse module calls the same URLs on
# both pages — the gate distinguishes by session, not by client trust.

async def _viewer_is_admin(request: Request) -> bool:
    from app.api.auth_routes import SESSION_COOKIE
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return False
    from app.auth import session as session_mgr
    return bool(await session_mgr.validate_session(token))


@router.get("/api/track-ratings")
async def track_ratings(request: Request):
    """Map {track_id: stars} for pip rendering + the 'rated' sort. Empty for
    guests when ratings are not guest-visible; full for the admin (R8)."""
    from app import database
    if not await database.get_ratings_visible_to_guests() and not await _viewer_is_admin(request):
        return {}
    return await database.get_all_ratings()


@router.get("/api/track-tags")
async def track_tags(request: Request):
    """Map {track_id: [tags]} for tag-chip rendering. Same gating as ratings."""
    from app import database
    if not await database.get_tags_visible_to_guests() and not await _viewer_is_admin(request):
        return {}
    return await database.get_all_tags()


@router.get("/api/highest-rated")
async def highest_rated(request: Request):
    """Top-rated tracks leaderboard, mirroring /api/most-played (DB-backed with
    live metadata backfill). Withheld for guests when ratings are not
    guest-visible; admin always (R10/R12). Each row carries its rating."""
    from app import database
    if not await database.get_ratings_visible_to_guests() and not await _viewer_is_admin(request):
        return []
    limit = await database.get_most_played_display_limit()
    rows = await database.get_top_rated_tracks(limit)
    missing = [r for r in rows if r["metadata"] is None]
    if missing:
        client = await state.get_plex_client()
        if client:
            fetched = await asyncio.gather(
                *[client.get_track(r["track_id"]) for r in missing],
                return_exceptions=True,
            )
            for r, t in zip(missing, fetched):
                if isinstance(t, BaseException):
                    continue  # deleted/unreachable track: skipped
                r["metadata"] = _track_dict(t)
                _bt = asyncio.create_task(database.set_play_track_meta(r["track_id"], r["metadata"]))
                _bt.add_done_callback(state._log_task_exc)
    await _refresh_album_drill(rows)
    # plex_held: identity-mode annotate (record-time snapshots — resolve
    # live; plan U4, same as /api/most-played).
    return await _annotate_plex_held([
        {**r["metadata"], "rating": r["stars"], "play_count": r["play_count"]}
        for r in rows if r["metadata"] is not None
    ])


def _dedup_by_id(items):
    """Collapse same-id duplicates (e.g. one item returned by two query
    variants) preserving order. Shared by /api/search and /api/search/broad."""
    seen: set = set()
    out = []
    for it in items:
        if it.id not in seen:
            seen.add(it.id)
            out.append(it)
    return out


@router.get("/api/search")
async def search(q: str = Query(..., min_length=1)):
    if await _catalog_active():
        from app.catalog import views
        res = await views.search(q)
        found = res["tracks"]
        # plex_held from the holds views.search just loaded (plan U4).
        res["tracks"] = await _annotate_plex_held(
            [_track_dict(t) for t in found], found)
        return res
    client = await state.get_plex_client()
    if not client:
        raise HTTPException(status_code=503, detail="No media source configured")
    from app import database
    from app.normalize import query_variants
    libs = await enabled_libraries()
    compiled = await _compiled_rules()
    # Plex hub search does the text matching for tracks/albums/artists
    # (diacritic-folding, relevance-ranked; client filters to the requested
    # section — 2026-06-10 hub-search plan). Admin pattern rules still reach
    # those results via capped query-variant expansion ("belle and" also
    # searches "belle &") since Plex can't know custom rules. Local surfaces
    # below (genres, credit acts) use direct normalization instead.
    variants = query_variants(q, await database.get_pattern_rules())
    per_call = await asyncio.gather(
        *[client.search(lib.key, v) for lib in libs for v in variants],
        return_exceptions=True,
    )
    ok = [r for r in per_call if not isinstance(r, BaseException)]
    # Same-id collapse BEFORE the name dedups (review fix): the same item
    # returned by two variants is not a cross-library duplicate — without
    # this, _dedup_artists would wrongly suppress its release count, and
    # tracks (which have no server-side dedup at all) would duplicate in
    # the payload.
    all_tracks = await _annotate_plex_held(
        [_track_dict(t) for t in _dedup_by_id([t for r in ok for t in r.tracks])])
    # Albums keep their library attribution (plan U2): per_call order is
    # [lib × variant], so zip against the same pairs; _by_id collapse runs
    # on the tagged stream (same id ⇒ same server, first tag wins).
    pairs = [(lib, v) for lib in libs for v in variants]
    seen_album_ids: set = set()
    tagged_albums = []
    for (lib, _v), r in zip(pairs, per_call):
        if isinstance(r, BaseException):
            continue
        for a in r.albums:
            if a.id in seen_album_ids:
                continue
            seen_album_ids.add(a.id)
            tagged_albums.append((a, lib.server_name))
    all_albums = _group_albums(tagged_albums, compiled, await _ranked_server_order())
    all_artists = _dedup_artists(_dedup_by_id([a for r in ok for a in r.artists]), compiled)
    # Genres come from the local style cache (count-desc order preserved),
    # never a per-keystroke Plex call. Empty cache → empty list; the
    # browse-tab path owns populating/refreshing the cache.
    genre_cache = await database.get_genre_cache()
    norm_q = _norm(q, compiled)
    all_genres = [g for g in genre_cache if norm_q in _norm(g["name"], compiled)]
    # Credited acts (credits plan U3/R3): merge index acts matching the
    # query into the artist results so compilation-only acts are findable.
    # Under pattern rules, matching and identity both use normalized forms
    # (R8 one-identity); albums untouched — matching is on the act, never
    # the compilation (R4). Same self-heal trigger as browse_artists.
    if not await state.cache_is_fresh("credit_cache_computed_at"):
        state.trigger_credit_refresh()
    credit_acts = await database.get_credit_acts()
    if credit_acts:
        present = {_norm(a.title, compiled) for a in all_artists}
        groups: dict[str, dict] = {}
        for act in credit_acts:
            key = _norm(act["name"], compiled)
            if norm_q not in key or key in present:
                continue
            if key in groups:
                groups[key]["release_count"] += act["release_count"]
            else:
                groups[key] = dict(act)
        if groups:
            all_artists = list(all_artists) + [_credit_artist_dict(g) for g in groups.values()]
    return {"tracks": all_tracks, "albums": all_albums, "artists": all_artists, "genres": all_genres}


# ── Broad search (Tier 2: on-demand literal title-substring expansion) ──────────
# Per-source page size for search_titles — one slab per (library × query variant)
# per page. "Page N" requests the Nth slab from each source, then dedups the
# union; exact cross-source ordering is not required for a scroll expansion.
_BROAD_PAGE_SIZE = 30


@router.get("/api/search/broad")
async def search_broad(
    q: str = Query(..., min_length=1),
    types: str = Query("track,album"),
    page: int = Query(0, ge=0),
):
    """Tier 2: literal per-section title matches hub search omits, loaded on
    scroll. Tracks/albums only (artists/genres/credits stay Tier 1's). Diacritic-
    blind (folding is Tier 1's); pattern-rule variants still apply. Each
    search_titles call rides the per-server concurrency semaphore."""
    if await _catalog_active():
        # Catalog-mode Tier 1 (views.search) is ALREADY a full normalized
        # substring scan over every catalog track/album — the exact space this
        # tier exists to cover for native Plex, whose hub search omits literal
        # title-substring matches (2026-07-17 ce-debug: the live per-(library x
        # variant) fan-out below made guest search crawl through a serial page
        # cascade that deduped to nothing). Nothing broader exists to serve;
        # an empty page marks the client's tier done after one local call.
        # Catalog staleness is not a gap: Tier 1 reads the same catalog, and
        # the install's freshness model is Rescan (never live top-up).
        return {"tracks": [], "albums": []}
    client = await state.get_plex_client()
    if not client:
        raise HTTPException(status_code=503, detail="No media source configured")
    from app import database
    from app.normalize import query_variants
    want = tuple(t for t in ("track", "album")
                 if t in {s.strip() for s in types.split(",")})
    if not want:
        return {"tracks": [], "albums": []}
    libs = await enabled_libraries()
    compiled = await _compiled_rules()
    variants = query_variants(q, await database.get_pattern_rules())
    start = page * _BROAD_PAGE_SIZE
    per_call = await asyncio.gather(
        *[client.search_titles(lib.key, v, types=want, start=start, size=_BROAD_PAGE_SIZE)
          for lib in libs for v in variants],
        return_exceptions=True,
    )
    out: dict = {"tracks": [], "albums": []}
    if "track" in want:
        out["tracks"] = await _annotate_plex_held([
            _track_dict(t) for t in _dedup_by_id(
                [t for r in per_call if not isinstance(r, BaseException) for t in r.tracks]
            )
        ])
    if "album" in want:
        # Same lib×variant zip + id-tag pattern as /api/search (first tag wins).
        pairs = [(lib, v) for lib in libs for v in variants]
        seen_album_ids: set = set()
        tagged_albums = []
        for (lib, _v), r in zip(pairs, per_call):
            if isinstance(r, BaseException):
                continue
            for a in r.albums:
                if a.id in seen_album_ids:
                    continue
                seen_album_ids.add(a.id)
                tagged_albums.append((a, lib.server_name))
        out["albums"] = _group_albums(tagged_albums, compiled, await _ranked_server_order())
    return out


# ── Queue append ──────────────────────────────────────────────────────────────

async def _attach_holds(track, *, chosen_server_name: str | None = None) -> None:
    """Capture a track's priority-ordered holds snapshot from the catalog onto
    ``track.holds`` (multi-source plan U9), so play-time fallback has its
    alternates and the live queue is rescan-immune. No-op when the track isn't
    catalogued (single-holder install) — playback falls back to ``stream_key``.
    Best-effort: an error here never blocks the enqueue.

    ``chosen_server_name`` (parity plan U4): when a guest picks "Play From
    Source: X", promote that source's holder to primary while keeping the rest in
    priority order as fallback — a preference, not a pin (R3). Only ever passed
    from the catalog enqueue branch, so the native path is byte-identical (R8):
    the second sort and the stream_key alignment below are both gated on it."""
    try:
        from app import database
        from app.catalog import identity as cat_identity, store
        ident = await cat_identity.identity_for_track_id(track.id) or track.id
        holds = await store.get_holds("track", ident)
        if not holds:
            return
        order = {sid: i for i, sid in enumerate(await database.get_source_priority())}
        holds.sort(key=lambda h: (order.get(h["source_id"], len(order) + (h.get("priority") or 0)),
                                  h["source_id"]))
        if chosen_server_name and chosen_server_name.strip():
            wanted = chosen_server_name.strip().lower()
            # Stable partition: chosen source(s) first, the rest keep priority
            # order. Python's sort is stable, so within each group priority holds.
            holds.sort(key=lambda h: 0 if (h.get("server_name") or "").strip().lower() == wanted else 1)
        track.holds = [{"source_id": h["source_id"], "key": h["provider_local_key"]} for h in holds]
        if chosen_server_name and track.holds:
            # Align the single-key stream path with the promoted primary so the
            # holds-empty fallback and any stream_key consumer agree with the
            # chosen source. Catalog branch only — native never passes a chosen
            # source, so its stream_key (the per-server id the client returned)
            # is untouched.
            track.stream_key = track.holds[0]["key"]
    except Exception:
        pass


async def _catalog_track(track_id: str):
    """Build a queue Track for a catalog identity (parity plan U4).

    In catalog mode ``track_id`` is a catalog IDENTITY, not a provider rating
    key — feeding it to ``client.get_track`` mis-routes (the registry splits on
    the first ':') and returns a silent empty result. So resolve it from the
    catalog store directly: ``views._track`` builds the full Track with the
    primary holder's resolvable stream key. A miss is a real not-found, logged
    (silent-empty guard) and surfaced as 404."""
    from app.catalog import store, views
    row = await store.get_track(track_id)
    if row is None:
        _log.warning("Catalog enqueue: track identity %r not in catalog", track_id)
        raise HTTPException(status_code=404, detail="Track not found")
    return await views._track(row)


async def _catalog_album_tracks(album_id: str) -> list:
    """Resolve a catalog album IDENTITY to its merged tracks (parity plan U4).

    The native ``_resolve_album_tracks`` resolves through the Plex browse-index
    keyed on rating keys and can't address a catalog identity (esp. a Jellyfin-
    only album whose identity is no Plex key — AE8). Resolve from the store. An
    empty result is logged (silent-empty guard); the caller turns it into a 404."""
    from app.catalog import store, views
    rows = await store.get_tracks_for_album(album_id)
    if not rows:
        _log.warning("Catalog enqueue: album identity %r has no catalog tracks", album_id)
        return []
    return [await views._track(r) for r in rows]


async def _plex_playable_ids(track_ids: list[str], *,
                             assume_lock: bool = False) -> set[str] | None:
    """The server-side enqueue gate's resolver (2026-08-04-002 plexplayer
    plan U5; R6, AE3, F2): which of *track_ids* may be enqueued while the
    selected output can only play Plex tracks — or ``None`` when the gate
    is inert. Shared verbatim by the guest AND admin queue endpoints (no
    admin bypass); the client's gray-out is UX, THIS is the gate.

    Inert (``None``) when:
    * ``state.output_requires_plex()`` is False — the U4 gate truth, read
      from the PERSISTED selected backend, never ``output_router.active``
      (whose swap defers mid-play; see the loud warning on the predicate).
      ``assume_lock=True`` skips ONLY this check: the U6 switch-time
      stranded pre-check needs the answer for a TARGET backend before
      ``activate_backend`` persists the selection (the lock isn't active
      yet, but the caller is deciding whether to make it so);
    * the catalog floor is inactive — the native single/multi-Plex path,
      where every served track IS Plex-backed (mirrors
      ``_annotate_plex_held``'s constant-True branch exactly). This check
      is NEVER skipped — on the native path every track is Plex-held, so
      there is nothing to strand regardless of the target.

    Active: resolved LIVE from catalog holds via the U4 predicate pieces
    (one registry read + one bulk holds read — never a client-supplied
    flag, never the enqueue-time hold snapshots, which go stale across
    rescans). Ids the holds table doesn't know directly (admin album
    appends resolve native provider ids, not identities, in catalog mode)
    get one alias-bridge attempt through ``catalog_identity_alias`` before
    failing the predicate."""
    from app.catalog import views
    # S-1: the ONE gate entry decides inert-vs-active (persisted-selection
    # mirror + catalog-floor check + enabled-id build all live there).
    enabled = await state.plex_lock_enabled_ids(assume_lock=assume_lock)
    if enabled is None:
        return None
    if not enabled:
        # No enabled Plex source can hold anything — nothing is playable.
        return set()
    ids = [t for t in track_ids if t]
    holds_map = await _bridged_holds_map(ids)
    return {tid for tid in ids
            if views.holds_plex_held(holds_map.get(tid), enabled)}


async def _require_playable(track_id: str) -> None:
    """U5 per-track enqueue gate (S-3: defined once beside the resolver,
    shared verbatim by the guest AND admin queue endpoints — no admin
    bypass): while the selected output can only play Plex tracks, a track
    with no enabled-Plex holder is rejected 409 ``output_source_lock``
    (the detail the shared module toasts distinctly)."""
    playable = await _plex_playable_ids([track_id])
    if playable is not None and track_id not in playable:
        raise HTTPException(status_code=409, detail="output_source_lock")


async def _filter_playable(tracks: list) -> tuple[list, int]:
    """U5 album-subset policy (S-3, both queue endpoints): keep only the
    playable subset and report the withheld count; a zero-playable batch
    gets the same 409 shape as the per-track rejection. Gate inert →
    ``(tracks, 0)`` untouched."""
    playable = await _plex_playable_ids([t.id for t in tracks])
    if playable is None:
        return tracks, 0
    kept = [t for t in tracks if t.id in playable]
    if not kept:
        raise HTTPException(status_code=409, detail="output_source_lock")
    return kept, len(tracks) - len(kept)


class QueueAppendRequest(BaseModel):
    track_id: str | None = None
    album_id: str | None = None
    # Optional library filter for the album branch. When provided, only the
    # library whose server_name matches (case-insensitive, trimmed) contributes
    # tracks. Ignored for the track branch (track_id already identifies a
    # single library's track). Absent on the request body = today's union
    # behavior across all matching libraries.
    # 128-char cap matches the existing validate_plex_id() cap on track_id /
    # album_id — gives a Pydantic-layer reject before the route logic runs.
    source_server_name: str | None = Field(default=None, max_length=128)


@router.post("/api/queue")
async def append_to_queue(body: QueueAppendRequest):
    if not body.track_id and not body.album_id:
        raise HTTPException(status_code=400, detail="Provide track_id or album_id")

    client = await state.get_plex_client()
    if not client:
        raise HTTPException(status_code=503, detail="No media source configured")

    q = state.queue_engine

    validate_plex_id(body.track_id)
    validate_plex_id(body.album_id)

    if body.track_id:
        # Single track. U5 source-lock gate FIRST (R6/AE3): the fundamental
        # "can't play here" outranks the incidental "already queued".
        await _require_playable(body.track_id)
        is_dup = q.is_duplicate(body.track_id)
        # Flood Control (2026-06-16): when the admin toggle is on, a guest may
        # not re-add a track that's currently playing or already in the upcoming
        # queue (is_duplicate covers both). Read at add-time so the toggle takes
        # effect on the next add. Guests only — admin's POST /admin/queue and the
        # album branch below never consult this. 409 (distinct from 423 locked)
        # so the guest UI can message it separately. Nothing is appended.
        if is_dup:
            from app import database
            if await database.get_setting("flood_control") == "1":
                raise HTTPException(status_code=409, detail="duplicate_blocked")
        warning = "already_in_queue" if is_dup else None
        try:
            # Catalog mode: body.track_id is a catalog identity → resolve via the
            # store and reorder holds for the chosen source (preference). Native
            # mode: the provider rating key resolves through the registry directly.
            if await _catalog_active():
                track = await _catalog_track(body.track_id)
                await _attach_holds(track, chosen_server_name=body.source_server_name)
            else:
                track = await client.get_track(body.track_id)
                await _attach_holds(track)
            item = await q.append(track)
        except QueueLockError:
            raise HTTPException(status_code=423, detail="queue_locked")
        # Undo receipt (collected-library plan U5): track branch ONLY —
        # album appends are batches with no single entry to undo and keep
        # their plain {ok, tracks_added} shape.
        result = {"ok": True, "tracks_added": 1,
                  "entry": {"track_id": item.track_id, "added_at": item.added_at}}
        if warning:
            result["warning"] = warning
        return result

    # Album. Catalog mode: body.album_id is a catalog identity → resolve to the
    # merged tracks from the store (works for a Jellyfin-only album whose identity
    # is no Plex key, AE8). Native mode: name-resolve across enabled libraries
    # (optionally filtered by source_server_name) for cross-server shared albums.
    catalog = await _catalog_active()
    if catalog:
        tracks = await _catalog_album_tracks(body.album_id or "")
    else:
        try:
            tracks = await _resolve_album_tracks(
                client, body.album_id or "", source_server_name=body.source_server_name,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="Album not found")

    if not tracks:
        raise HTTPException(status_code=404, detail="Album not found or no tracks")

    # U5 subset policy (batch path): only the playable subset is enqueued;
    # the response reports the filtered count so the shared module can
    # toast "Added N of M" (S-3 shared helper).
    tracks, tracks_filtered = await _filter_playable(tracks)

    # All-or-nothing batch append: validate full batch under one lock so a
    # partial album never lands when the queue is at the cap (was: per-track
    # appends could land N tracks then 423 on the N+1th, leaving the queue
    # holding a half-album). In catalog mode the chosen source is promoted to
    # primary per track (preference); native mode keeps global priority order.
    for t in tracks:
        await _attach_holds(t, chosen_server_name=body.source_server_name if catalog else None)
    try:
        items = await q.append_many(tracks)
    except QueueLockError:
        raise HTTPException(status_code=423, detail="queue_locked")

    # Batch receipt (remove-own-queued-tracks U2): album appends now return
    # one receipt per created entry so the guest UI can group them and offer
    # "remove these N tracks I added" as a unit. The single-track branch keeps
    # its `entry` (singular) shape; albums use `entries` (plural).
    # tracks_filtered (plan U5): how many of the album's tracks the source
    # lock withheld — 0 whenever the gate is inert, so the shape is stable.
    return {"ok": True, "tracks_added": len(items),
            "tracks_filtered": tracks_filtered,
            "entries": [{"track_id": it.track_id, "added_at": it.added_at}
                        for it in items]}


# ── Surprise Me (2026-06-17 plan U4) ─────────────────────────────────────────


class SurprisePick(BaseModel):
    """One of the pressing browser's own picks, used to seed the suggestion."""
    track_id: str = Field(..., max_length=128)
    genre: str | None = Field(default=None, max_length=128)
    artist: str | None = Field(default=None, max_length=256)


class SurpriseRequest(BaseModel):
    # The browser sends its own recent picks; an empty list (fresh visitor) is
    # valid and resolves to a random track.
    picks: list[SurprisePick] = Field(default_factory=list, max_length=50)
    # Anti-repeat (plan 005): the browser's recently-surprised track ids, excluded
    # from the smart sources so remove + re-press won't return the same track.
    exclude: list[str] = Field(default_factory=list, max_length=50)
    # Durable ownership token (remove-own-surprise-after-screen-off): the browser
    # generates + persists this BEFORE the press and sends it here; the server
    # stamps it on the queued item so the guest can still remove its own track
    # when this response is lost (phone slept during a slow resolve). Opaque;
    # optional for back-compat with older clients.
    owner_token: str | None = Field(default=None, max_length=128)


@router.post("/api/queue/surprise")
async def surprise_me(body: SurpriseRequest):
    """Enqueue ONE track fitting the presser's own picks via the degradation
    chain (Plex similar → heuristic → random). Always succeeds when a library
    has content; never surfaces a failure to the guest. Returns the resolved
    source for dev observability."""
    from app import database
    if not _resolve_surprise_enabled(await database.get_setting("surprise_me_enabled")):
        # Defense in depth — the button is also hidden client-side when off.
        raise HTTPException(status_code=403, detail="surprise_disabled")

    client = await state.get_plex_client()
    if not client:
        raise HTTPException(status_code=503, detail="No media source configured")

    # Short-circuit a locked queue BEFORE the Plex similarity fan-out — otherwise a
    # locked host still pays unbounded similarity queries per press and only learns
    # of the lock at append() (code-review #3, 2026-06-18).
    if state.queue_engine.is_locked:
        raise HTTPException(status_code=423, detail="queue_locked")

    mode = _resolve_surprise_mode(await database.get_setting("surprise_me_source_mode"))
    # Capability degradation (plan U13): the smart sources (Plex sonic/similar)
    # are Plex specializations that can't reason over a merged Jellyfin/local
    # catalog. With a non-Plex source connected, Surprise Me uses the whole-
    # library random floor (now catalog-backed); single-source Plex keeps the
    # full smart chain (AE6). The admin Setup surfaces this with a note (U13).
    if await _catalog_active():
        mode = "random"
    diversity = _resolve_surprise_diversity(await database.get_setting("surprise_me_diversity"))
    # Random-pick length band (2026-06-20 plan U2): exclude tracks outside the
    # admin's min/max from the smart sources. (None, None) when unset → no-op.
    length_bounds = await database.get_random_length_bounds()
    # Validate guest-supplied seed ids before they reach Plex path interpolation
    # (get_sonic_nearest / get_artist_similar_names). Invalid ids are dropped, not
    # 400'd — seeds are best-effort and an empty seed resolves to random. Mirrors
    # the validate_plex_id guard every other guest endpoint applies (code-review #1).
    seed = [p.model_dump() for p in body.picks if _ID_RE.match(p.track_id or "")]
    exclude_ids = {tid for tid in body.exclude if _ID_RE.match(tid or "")}

    from app.queue.surprise import (
        resolve_surprise, record_source, recent_sources, recent_source_tally,
    )
    track, source = await resolve_surprise(
        seed, mode, client=client, queue=state.queue_engine, diversity=diversity,
        exclude_ids=exclude_ids, length_bounds=length_bounds,
    )
    if track is None:
        # Only happens when no enabled library has content. Quiet no-op — the
        # guest UI shows nothing (data-plane silent); the log carries the signal.
        _log.info("surprise: no track available (mode=%s, seed=%d picks)", mode, len(seed))
        return {"ok": False}

    # Re-check duplication right before append: resolve_surprise's acceptable()
    # gate ran against an earlier queue snapshot, so a concurrent press/add could
    # have queued this same track in between (TOCTOU). Quiet no-op rather than
    # appending a back-to-back duplicate (code-review #8).
    if state.queue_engine.is_duplicate(track.id):
        _log.info("surprise: resolved track %r now duplicate; skipping", track.title)
        return {"ok": False}

    try:
        item = await state.queue_engine.append(track, owner_token=body.owner_token)
    except QueueLockError:
        raise HTTPException(status_code=423, detail="queue_locked")

    record_source(source)
    # Push the updated attribution to admins so the Setup "Recent suggestions"
    # readout updates live (no reload). Payload-carrying, mirroring the
    # GET /admin/surprise/recent shape so the admin has one render path.
    try:
        from app.events.bus import manager
        from app.events.types import SurpriseRecordedEvent
        await manager.broadcast_to_admins(
            SurpriseRecordedEvent(recent=recent_sources(), tally=recent_source_tally())
        )
    except Exception:
        _log.warning("surprise: recorded-event broadcast failed", exc_info=True)
    _log.info("surprise: queued %r via %s (mode=%s)", track.title, source, mode)
    return {
        "ok": True,
        "tracks_added": 1,
        "entry": {"track_id": item.track_id, "added_at": item.added_at},
        "source": source,
    }


class QueueEntryReceipt(BaseModel):
    track_id: str = Field(..., max_length=128)
    added_at: str = Field(..., max_length=64)


class QueueUndoRequest(BaseModel):
    """One receipt (single-track, backward compatible) OR a batch (album).

    Single form: ``{track_id, added_at}``. Batch form: ``{entries: [...]}``.
    `normalized()` flattens both to a list of `(track_id, added_at)` tuples."""
    track_id: str | None = Field(default=None, max_length=128)
    added_at: str | None = Field(default=None, max_length=64)
    entries: list[QueueEntryReceipt] | None = None

    def normalized(self) -> list[tuple[str, str]]:
        if self.entries:
            return [(e.track_id, e.added_at) for e in self.entries]
        if self.track_id is not None and self.added_at is not None:
            return [(self.track_id, self.added_at)]
        return []


@router.post("/api/queue/undo")
async def undo_queue_append(body: QueueUndoRequest):
    """Redeem one or more append receipts (remove-own-queued-tracks U3):
    removes the matching UPCOMING queue entries. Accepts a single
    ``{track_id, added_at}`` (backward compatible) or a batch
    ``{entries: [...]}`` (album-as-unit removal); returns ``removed`` as the
    count actually taken out.

    A receipt whose entry is gone (already playing, admin-removed, or garbage)
    is a quiet no-op — matched by pure equality, so a malformed receipt simply
    matches nothing and contributes 0. A body carrying neither form is a
    client error (400). Never removes the currently-playing track."""
    entries = body.normalized()
    if not entries:
        raise HTTPException(status_code=400,
                            detail="Provide track_id+added_at or a non-empty entries list")
    from app.output import session as output_session
    if output_session.output_hold_active():
        # R17 (the admin queue_clear mechanic): a receipt-removal during an
        # outage can drop the HELD front — the gen bump re-targets any
        # in-flight resume at the new front from 0:00 instead of seeking the
        # removed track's held position into it.
        state._advance_gen += 1
    removed = await state.queue_engine.remove_entries(entries)
    return {"ok": True, "removed": removed}


# ── Album art proxy ───────────────────────────────────────────────────────────

_ALLOWED_ART_PREFIXES = ("/library/", "/photo/")


def _nonplex_source_ids(client) -> set:
    """Source ids of connected NON-Plex providers (U12). Their art keys bypass
    the Plex ``/library//photo/`` allowlist because the owning provider enforces
    its own art access — a remote authenticated fetch (Jellyfin) or a
    realpath-under-root containment check (local files, R23). Returns an empty set
    for a client without a real source list (e.g. a unit-test MagicMock), so the
    Plex allowlist behaviour is unchanged there."""
    srcs = getattr(client, "sources", None)
    if not isinstance(srcs, (list, tuple)):
        return set()
    return {getattr(s, "source_id", None) for s in srcs
            if getattr(s, "source_type", "plex") != "plex"}


def _valid_art_path(path: str, allow_prefixes=()) -> bool:
    if ":" in path and not path.startswith("/"):
        prefix, bare = path.split(":", 1)
        # A non-Plex source key (Jellyfin item image / local file, U12) skips the
        # Plex part-path allowlist: the owning provider gates access itself
        # (Jellyfin fetches a remote URL with its token; LocalSource realpaths the
        # file and rejects anything outside the configured root). Without this,
        # every Jellyfin/local art key (bare "Items/…" or a relpath) fails the
        # "/library//photo/" check and 400s.
        if prefix in allow_prefixes:
            return True
        if not _ID_RE.match(prefix):
            return False  # scheme-like prefix is not a valid Plex machine_id
    else:
        bare = path
    # Decode twice to catch double-encoded traversal sequences like %252e%252e
    bare = unquote(bare)
    return ".." not in bare and any(bare.startswith(p) for p in _ALLOWED_ART_PREFIXES)


@router.get("/api/art")
async def art_proxy(path: str = Query(...), w: int | None = None):
    client = await state.get_plex_client()
    if not client:
        raise HTTPException(status_code=503, detail="No media source configured")
    if not _valid_art_path(path, allow_prefixes=_nonplex_source_ids(client)):
        raise HTTPException(status_code=400, detail="Invalid art path")
    # Clamp the requested thumbnail width to a sane range; out-of-range or absent
    # → full image. Plain default (not Query(...)) so direct callers/tests get
    # None rather than the Query sentinel object.
    if not (isinstance(w, int) and 16 <= w <= 2048):
        w = None

    from app.cache import cache

    # Resized variants cache under a width-suffixed key so they never collide with
    # the full image or each other (2026-06-25 deep-jump reveal fix — browse rows
    # request a small w so a 48px slot doesn't decode a 150KB full cover).
    cache_key = path if not w else f"{path}|w{w}"
    headers = {**_ART_CACHE_HEADERS, "ETag": _art_etag(cache_key)}

    # Cache hit → serve immediately, skip Plex round trip entirely.
    # Symmetric with the cache.put swallow below (KTD5): a cache.get failure
    # is a degraded cache, not a request failure — treat any exception as a
    # miss and fall through to Plex.
    try:
        cached = await cache.get(cache_key)
    except Exception:
        _log.warning("art cache get failed for path=%s", cache_key[:80], exc_info=True)
        cached = None
    if cached is not None:
        data, content_type = cached
        return Response(content=data, media_type=content_type, headers=headers)

    # Cache miss → fetch from Plex (resized when w given), cache the response, serve.
    try:
        data, content_type = await client.fetch_art(path, width=w)
    except HTTPException:
        raise
    except Exception as exc:
        # R5/AE4: if Plex is unreachable but we already have a cached entry
        # (race: another request may have warmed it between our miss and now),
        # serve it with a WARNING so the operator sees the upstream failure
        # without breaking the response. Same swallow policy applies to this
        # recheck — a cache.get exception here means "no usable stale entry".
        try:
            stale = await cache.get(cache_key)
        except Exception:
            _log.warning("art cache get (stale recheck) failed for path=%s",
                         cache_key[:80], exc_info=True)
            stale = None
        if stale is not None:
            stale_data, stale_ct = stale
            _log.warning("Plex unreachable for art %s; serving cached: %s",
                         path[:80], type(exc).__name__)
            return Response(content=stale_data, media_type=stale_ct, headers=headers)
        raise HTTPException(status_code=404, detail="Art not found")

    # Best-effort write (KTD5): a cache.put failure must not break the response.
    try:
        await cache.put(cache_key, data, content_type)
    except Exception:
        _log.warning("art cache put failed for path=%s", cache_key[:80], exc_info=True)
    return Response(content=data, media_type=content_type, headers=headers)


# ── Guest WebSocket ───────────────────────────────────────────────────────────

@router.websocket("/ws")
async def guest_websocket(websocket: WebSocket):
    from app.events.bus import manager
    await websocket.accept()
    manager.connect(websocket, "guest")
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket, "guest")
