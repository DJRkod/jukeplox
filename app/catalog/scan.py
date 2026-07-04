"""Catalog scan (2026-06-27 multi-source plan U6/U7).

Pure dedup is split from stateful identity allocation:

- ``cluster_sources`` (pure) crawls nothing — it reshapes per-source results into
  normalized items and groups them with ``merge`` (ID-first → strict-name).
- ``_rows`` (pure) turns clusters + their resolved identities into catalog rows
  and a priority-ordered holds list.
- ``build_catalog`` (pure) = cluster + deterministic identities + rows, kept for
  unit tests.
- ``scan_and_replace`` (I/O) crawls every source, allocates allocate-once
  identities via ``identity.resolve_cluster`` (U7), atomically replaces the
  catalog, then runs the offline metadata migration. It carries the browse-index
  refresh's "every section failed → don't wipe" guard, generalized across
  sources.

Durations are normalized to milliseconds at the PROVIDER boundary (each source
returns ``Track.duration_ms`` in ms), so the merge tolerance compares ms directly.
"""

from __future__ import annotations

import logging

from app.catalog import merge, store

_log = logging.getLogger(__name__)


def _representative(cluster: list[dict]) -> dict:
    """The display member of a cluster: highest-priority source (lowest priority
    number), ``local_key`` as a stable tie-break."""
    return min(cluster, key=lambda it: (it.get("priority", 0), str(it.get("local_key"))))


def _holds(cluster: list[dict], entity_type: str, identity: str, key_field: str) -> list[dict]:
    """One hold per (entity, source): the provider-local key from ``key_field``
    (the resolvable stream key for tracks, the drill-in id for albums/artists)."""
    return [{
        "entity_type": entity_type, "identity": identity,
        "source_id": it.get("source_id"), "provider_local_key": it.get(key_field),
        "priority": it.get("priority", 0), "server_name": it.get("server_name"),
    } for it in cluster]


def _shape_items(sources: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Reshape per-source dataclass results into normalized merge items."""
    from app.plex.client import browse_base_key

    artist_items: list[dict] = []
    album_items: list[dict] = []
    track_items: list[dict] = []
    for src in sources:
        sid, prio, sname = src.get("source_id"), src.get("priority", 0), src.get("server_name")
        for a in src.get("artists") or []:
            artist_items.append({
                "match_ids": {}, "source_id": sid, "priority": prio, "server_name": sname,
                "local_key": a.id, "title": a.title, "base_key": browse_base_key(a.title),
                "thumb": a.thumb, "release_count": a.release_count,
            })
        for alb in src.get("albums") or []:
            album_items.append({
                "match_ids": getattr(alb, "match_ids", {}) or {},
                "source_id": sid, "priority": prio, "server_name": sname,
                "local_key": alb.id, "title": alb.title,
                "title_base": browse_base_key(alb.title), "artist": alb.artist,
                "artist_base_key": browse_base_key(alb.artist), "year": alb.year,
                "thumb": alb.thumb, "subtype": alb.subtype, "added_at": alb.added_at,
                "track_count": alb.track_count,
            })
        for t in src.get("tracks") or []:
            track_items.append({
                "match_ids": getattr(t, "match_ids", {}) or {},
                "source_id": sid, "priority": prio, "server_name": sname,
                "local_key": t.id, "hold_key": t.stream_key or t.id,
                "album_local_id": t.album_id, "title": t.title,
                "title_base": browse_base_key(t.title), "artist": t.artist,
                "artist_base_key": browse_base_key(t.artist), "album": t.album,
                "album_artist": t.album_artist, "duration_ms": t.duration_ms,
                "disc_number": t.disc_number, "track_number": t.track_number,
                "genre": t.genre, "year": t.year, "thumb": t.thumb,
            })
    return artist_items, album_items, track_items


def cluster_sources(sources: list[dict]) -> dict:
    """Pure: group every source's items into dedup clusters per entity type."""
    artist_items, album_items, track_items = _shape_items(sources)
    return {
        "artists": merge.group(artist_items, merge.artist_coarse, lambda a, b: True),
        "albums": merge.group(album_items, merge.album_coarse, merge.album_same),
        "tracks": merge.group(track_items, merge.track_coarse, merge.track_same),
    }


def _rows(clustered: dict, artist_idents: list[str], album_idents: list[str],
          track_idents: list[str]) -> dict:
    """Pure: build catalog rows + holds from clusters and their resolved identities
    (lists parallel to ``clustered[...]``)."""
    artist_rows, album_rows, track_rows, holds = [], [], [], []

    for cluster, ident in zip(clustered["artists"], artist_idents):
        rep = _representative(cluster)
        artist_rows.append({
            "identity": ident, "title": rep["title"], "base_key": rep["base_key"],
            "thumb": rep["thumb"], "release_count": rep["release_count"],
        })
        holds += _holds(cluster, "artist", ident, "local_key")

    album_id_to_identity: dict[tuple, str] = {}
    for cluster, ident in zip(clustered["albums"], album_idents):
        rep = _representative(cluster)
        album_rows.append({
            "identity": ident, "title": rep["title"], "title_base": rep["title_base"],
            "artist": rep["artist"], "artist_base_key": rep["artist_base_key"],
            "year": rep["year"], "thumb": rep["thumb"], "subtype": rep["subtype"],
            "added_at": rep["added_at"], "track_count": rep["track_count"],
        })
        holds += _holds(cluster, "album", ident, "local_key")
        for it in cluster:
            album_id_to_identity[(it["source_id"], it["local_key"])] = ident

    for cluster, ident in zip(clustered["tracks"], track_idents):
        rep = _representative(cluster)
        album_identity = album_id_to_identity.get((rep["source_id"], rep["album_local_id"]))
        track_rows.append({
            "identity": ident, "title": rep["title"], "title_base": rep["title_base"],
            "artist": rep["artist"], "artist_base_key": rep["artist_base_key"],
            "album": rep["album"], "album_identity": album_identity,
            "album_artist": rep["album_artist"], "duration_ms": rep["duration_ms"],
            "disc_number": rep["disc_number"], "track_number": rep["track_number"],
            "genre": rep["genre"], "year": rep["year"], "thumb": rep["thumb"],
        })
        holds += _holds(cluster, "track", ident, "hold_key")

    # Drop album rows (and their holds) with no linked tracks. When a track dedups
    # across sources but its album does NOT (e.g. one source omits track_count),
    # the merged track takes the representative source's album_identity, orphaning
    # the other source's album row — an empty, duplicate album the UI must not show
    # (ce-debug 2026-06-29, Bug C). Gated on the catalog HAVING tracks: a content
    # scan always crawls tracks, so a trackless album there is a genuine orphan; a
    # degenerate album-only scan (track crawl yielded nothing) is left untouched
    # rather than emptying the whole album list.
    if track_rows:
        linked = {t["album_identity"] for t in track_rows if t["album_identity"]}
        album_rows = [a for a in album_rows if a["identity"] in linked]
        holds = [h for h in holds if h["entity_type"] != "album" or h["identity"] in linked]

    return {"artists": artist_rows, "albums": album_rows, "tracks": track_rows, "holds": holds}


def _aliases(clustered: dict, artist_idents, album_idents, track_idents) -> list[tuple]:
    """Every lookup key (external ids + local ids) → identity, for seeding the
    durable alias table (the deterministic build_catalog path)."""
    out: list[tuple] = []
    for etype, key in (("artist", "artists"), ("album", "albums"), ("track", "tracks")):
        idents = {"artists": artist_idents, "albums": album_idents, "tracks": track_idents}[key]
        for cluster, ident in zip(clustered[key], idents):
            for k in merge.lookup_keys(cluster):
                out.append((etype, k, ident))
    return out


def build_catalog(sources: list[dict]) -> dict:
    """Pure: deduped catalog rows + holds + alias seeds, using deterministic
    identities (``merge.cluster_identity``). Used by unit tests; the live path
    (``scan_and_replace``) substitutes allocate-once identities."""
    clustered = cluster_sources(sources)
    a = [merge.cluster_identity(c, "artist") for c in clustered["artists"]]
    al = [merge.cluster_identity(c, "album") for c in clustered["albums"]]
    t = [merge.cluster_identity(c, "track") for c in clustered["tracks"]]
    out = _rows(clustered, a, al, t)
    out["aliases"] = _aliases(clustered, a, al, t)
    return out


# ── I/O orchestration ─────────────────────────────────────────────────────────

async def _safe(coro):
    """Await ``coro``, returning ``None`` on any error (a failed section just
    contributes nothing rather than aborting the crawl)."""
    try:
        return await coro
    except Exception:
        return None


async def scan_and_replace(registry, enabled_section_keys: set | None = None) -> bool:
    """Crawl every connected source and atomically rebuild the catalog, then run
    the offline metadata migration (U7).

    Returns ``True`` when the catalog was replaced, ``False`` when skipped (no
    sources, or every section failed — the don't-wipe guard). With
    ``enabled_section_keys`` only libraries whose key is in the set are crawled —
    for EVERY source type, so the admin's enabled-library checkbox gates import
    uniformly (the Jellyfin/non-Plex libraries were previously crawled in full
    regardless of their checkbox; ce-debug 2026-06-29)."""
    if registry is None:
        return False
    sources = getattr(registry, "sources", None) or []
    if not sources:
        return False

    crawled: list[dict] = []
    any_section_ok = False
    for idx, src in enumerate(sources):
        libs = await _safe(src.get_libraries())
        if libs is None:
            continue
        if enabled_section_keys is not None:
            libs = [l for l in libs if l.key in enabled_section_keys]
        artists, albums, tracks, server_name = [], [], [], ""
        for lib in libs:
            server_name = getattr(lib, "server_name", "") or server_name
            a = await _safe(src.get_artists(lib.key))
            al = await _safe(src.get_albums(lib.key))
            tr = await _safe(src.get_tracks(lib.key))
            if a is not None:
                artists.extend(a)
                any_section_ok = True
            if al is not None:
                albums.extend(al)
            if tr is not None:
                tracks.extend(tr)
        crawled.append({
            "source_id": src.source_id, "priority": idx, "server_name": server_name,
            "artists": artists, "albums": albums, "tracks": tracks,
        })

    if not any_section_ok:
        _log.warning("Catalog scan: every section failed — catalog untouched")
        return False

    from app.catalog import identity, migrate

    clustered = cluster_sources(crawled)
    # resolve_clusters (not per-cluster resolve_cluster) so a stale alias from a
    # prior over-merge can't make two distinct clusters share an identity — which
    # replace_catalog's INSERT OR IGNORE would turn into silently-dropped rows
    # (empty-album bug, ce-debug 2026-06-29). It self-heals corrupt aliases on scan.
    artist_idents = await identity.resolve_clusters("artist", clustered["artists"])
    album_idents = await identity.resolve_clusters("album", clustered["albums"])
    track_idents = await identity.resolve_clusters("track", clustered["tracks"])
    rows = _rows(clustered, artist_idents, album_idents, track_idents)
    await store.replace_catalog(rows["artists"], rows["albums"], rows["tracks"], rows["holds"])

    # Re-key existing ratings/play-counts onto the (possibly new) identities.
    # Inert on a Plex-only install where identity == the compound rating key.
    migrated = await migrate.migrate_metadata()

    _log.info(
        "Catalog rebuilt: %d artists, %d albums, %d tracks across %d source(s); migrated %s",
        len(rows["artists"]), len(rows["albums"]), len(rows["tracks"]), len(crawled), migrated,
    )
    return True
