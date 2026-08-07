"""Catalog-backed browse/search — the merged "universal floor" (plan U8).

Serves a single, source-invisible, already-deduped view from the unified catalog
for multi-source (or non-native single-source) installs. A lone native source
(e.g. Plex) keeps its specialized native pipeline instead (R15/AE6); ``guest.py``
routes between the two. Builders return the SAME source-neutral model objects
(``Artist``/``Album``/``Track``) the native endpoints return, so the shared
frontend renders them unchanged — albums carry ``sources`` (their holds) for
drill-in routing; tracks carry the highest-priority hold's key as ``stream_key``
so enqueue/stream resolves through the registry, and the catalog identity as
``id`` so ratings/play-counts (re-keyed in U7) line up.
"""

from __future__ import annotations

from app.catalog import store
from app.models import Album, Artist, Track


async def _source_types() -> dict:
    """Map ``source_id`` → ``source_type`` from the live registry, so per-source
    holds can carry a type ("plex"/"jellyfin"/"local") for type-qualified picker
    labels (parity plan U2) without guessing from the id. Empty when no registry
    is configured — the view builders stay usable in isolation/tests, where
    ``source_type`` then degrades to ``""`` (frontend falls back to bare names)."""
    from app import state
    try:
        reg = await state.get_plex_client()
    except Exception:
        return {}
    srcs = getattr(reg, "sources", None)
    if not isinstance(srcs, list):
        return {}
    return {getattr(s, "source_id", None): getattr(s, "source_type", "")
            for s in srcs if getattr(s, "source_id", None)}


def holds_plex_held(holds, enabled_plex_ids: set) -> bool:
    """The per-track playability predicate (plan U4, R6 data layer / AE4):
    does this identity have ≥1 hold from an enabled source of type "plex"?

    Sync + batch-friendly by design: callers build ``enabled_plex_ids`` ONCE
    per request (``plex_enabled_source_ids``) and evaluate every row against
    already-loaded holds — one registry read + one map build per request,
    never per-row queries. Accepts every hold shape in the codebase (catalog
    rows, ``views._track`` hold_list entries, ``_attach_holds`` playback
    snapshots) since all carry ``source_id``. Tolerant of identities with no
    Plex-shaped keys: local/Jellyfin-only rows, empty/None hold lists, or
    malformed entries return False without raising."""
    for h in holds or ():
        sid = h.get("source_id") if isinstance(h, dict) else None
        if sid and sid in enabled_plex_ids:
            return True
    return False


def _artist(row: dict) -> Artist:
    return Artist(id=row["identity"], title=row["title"], thumb=row["thumb"],
                  release_count=row["release_count"])


async def _album(row: dict, types: dict | None = None) -> Album:
    if types is None:
        types = await _source_types()
    holds = await store.get_holds("album", row["identity"])
    sources = [{"server_name": h["server_name"] or "", "album_id": h["provider_local_key"],
                "source_type": types.get(h["source_id"], "")}
               for h in holds] or None
    return Album(id=row["identity"], title=row["title"], artist=row["artist"],
                 year=row["year"], thumb=row["thumb"], subtype=row["subtype"],
                 track_count=row["track_count"], sources=sources)


async def _track(row: dict, types: dict | None = None) -> Track:
    if types is None:
        types = await _source_types()
    holds = await store.get_holds("track", row["identity"])
    primary = holds[0] if holds else None
    # Attach the full priority-ordered holds (with server_name + source_type) so
    # _track_dict can serialize a per-source list for the "Play From Source…"
    # picker (parity plan U2). stream_key/server_name stay the primary holder
    # (unchanged default). The enqueue path overwrites Track.holds with its own
    # {source_id, key} playback snapshot via _attach_holds, so this richer shape
    # is browse-only and never leaks provider keys (it isn't in _track_dict).
    hold_list = [{"source_id": h["source_id"], "key": h["provider_local_key"],
                  "server_name": h["server_name"] or "",
                  "source_type": types.get(h["source_id"], "")}
                 for h in holds]
    return Track(
        id=row["identity"], title=row["title"], artist=row["artist"],
        album=row["album"] or "", duration_ms=row["duration_ms"] or 0,
        genre=row["genre"], year=row["year"], thumb=row["thumb"],
        stream_key=(primary["provider_local_key"] if primary else ""),
        server_name=(primary["server_name"] if primary else "") or "",
        album_artist=row["album_artist"], album_id=row["album_identity"],
        disc_number=row["disc_number"] or 1, track_number=row["track_number"],
        holds=hold_list,
    )


async def artists() -> list[Artist]:
    return [_artist(r) for r in await store.get_artists()]


async def albums() -> list[Album]:
    types = await _source_types()
    return [await _album(r, types) for r in await store.get_albums()]


async def artist_albums(artist_identity: str) -> list[Album]:
    artist = await store.get_artist(artist_identity)
    if not artist:
        return []
    types = await _source_types()
    rows = await store.get_albums_for_artist(artist["base_key"])
    return [await _album(r, types) for r in rows]


async def artist_songs(artist_identity: str) -> list[Track]:
    """Every track on the artist's own releases — the floor's All-Songs.

    Gathers via the artist's ALBUMS (every child of each release), NOT by matching
    each track's per-track artist: a track's credited artist often differs from the
    release artist (features, collabs, Various-Artists comps), and filtering on it
    drops those tracks — emptying All-Songs entirely for an all-featured or VA
    artist (ce-debug 2026-06-29, Bug A). This mirrors the native All-Songs, which
    returns all children of own releases; the Plex VA appears-on enrichment stays a
    native specialization the floor doesn't reproduce."""
    artist = await store.get_artist(artist_identity)
    if not artist:
        return []
    types = await _source_types()
    out = []
    for alb in await store.get_albums_for_artist(artist["base_key"]):
        for r in await store.get_tracks_for_album(alb["identity"]):
            out.append(await _track(r, types))
    return out


async def album_tracks(album_identity: str) -> list[Track]:
    types = await _source_types()
    return [await _track(r, types) for r in await store.get_tracks_for_album(album_identity)]


async def genres() -> list[dict]:
    """Genre tag counts from the catalog's tracks — the source-neutral genres
    list (plan U13/R16).

    One row per distinct non-empty track genre, ``count`` = the number of tracks
    carrying it, highest first. Case-insensitive merge with the first-seen
    spelling winning, mirroring the Plex styles merge in ``guest.browse_genres``
    so the payload shape is identical (``[{name, count}]``). No tagged genres →
    ``[]`` (browse degrades to an empty Genres tab, never an error)."""
    counts: dict[str, int] = {}
    names: dict[str, str] = {}
    for r in await store.get_all_tracks():
        g = (r.get("genre") or "").strip()
        if not g:
            continue
        k = g.lower()
        counts[k] = counts.get(k, 0) + 1
        names.setdefault(k, g)
    merged = [{"name": names[k], "count": v} for k, v in counts.items()]
    merged.sort(key=lambda x: x["count"], reverse=True)
    return merged


async def genre_albums(style: str) -> list[Album]:
    """Albums whose tracks carry genre ``style`` (case-insensitive) — the
    catalog-backed Genres drill-in (plan U13).

    Returns ``Album`` objects (with their holds) so the shared renderer treats
    them like any other catalog album list. Album order follows first
    appearance among the matching tracks. An unknown/empty style → ``[]``."""
    want = (style or "").strip().lower()
    if not want:
        return []
    album_ids: list[str] = []
    seen: set[str] = set()
    for r in await store.get_all_tracks():
        if (r.get("genre") or "").strip().lower() != want:
            continue
        aid = r.get("album_identity")
        if aid and aid not in seen:
            seen.add(aid)
            album_ids.append(aid)
    types = await _source_types()
    out: list[Album] = []
    for aid in album_ids:
        row = await store.get_album(aid)
        if row:
            out.append(await _album(row, types))
    return out


async def search(query: str) -> dict:
    """Catalog substring search with pattern-rule normalization (the floor).

    Returns ``{tracks, albums, artists, genres}`` where tracks/albums/artists are
    model objects (the caller wraps tracks with ``_track_dict``) and genres are
    cache rows — matching the native ``/api/search`` payload shape."""
    from app import database
    from app.normalize import compile_rules, normalize

    compiled = compile_rules(await database.get_pattern_rules())
    nq = normalize(query, compiled).strip()
    if not nq:
        return {"tracks": [], "albums": [], "artists": [], "genres": []}

    def _hit(*values) -> bool:
        return any(nq in normalize(v, compiled) for v in values if v)

    found_artists = [_artist(r) for r in await store.get_artists() if _hit(r["title"])]
    found_albums = [await _album(r) for r in await store.get_albums()
                    if _hit(r["title"], r["artist"])]
    found_tracks = [await _track(r) for r in await store.get_all_tracks()
                    if _hit(r["title"], r["artist"])]
    genres = [g for g in await database.get_genre_cache() if _hit(g["name"])]
    return {"tracks": found_tracks, "albums": found_albums,
            "artists": found_artists, "genres": genres}
