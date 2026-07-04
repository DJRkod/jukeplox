"""Offline re-key of local metadata onto catalog identity (2026-06-27 plan U7).

Carries existing admin-authored ratings/tags and play counts / Most-Played
display rows — all keyed today by a source compound track id — onto the track's
stable catalog identity, so they survive cross-source merges and rescans.

Properties (origin R18, plan U7):

- **Offline.** Reads only the catalog/alias + metadata tables; never calls a
  live source. A Plex-removed install still migrates from what's already stored.
- **Inert on a Plex-only install.** There, identity == the compound rating key,
  so every row is skipped — nothing changes (AE6 parity).
- **Backfill, non-clobbering.** A value is copied onto the identity only when the
  identity has none yet, so a direct identity value is never overwritten and a
  re-run is a no-op (idempotent).
- **Rollback-safe.** The original compound-keyed rows are RETAINED (this only
  adds identity-keyed rows), so the migration is reversible during Phase B's
  validation window by dropping the identity rows. A rated/tagged track whose
  source is disconnected has no catalog identity, so its row is left untouched —
  a dormant alias, never dropped.

The live read/write cutover (browse/search serving identity-keyed tracks, the
leaderboard reading by identity) lands with U8, where the frontend starts using
identities; this unit only makes the identity-keyed data exist.
"""

from __future__ import annotations

from app import database
from app.catalog import identity, store


async def migrate_metadata() -> dict:
    """Backfill ratings/tags/play-counts/Most-Played meta onto catalog identities.
    Returns per-table counts of rows newly copied, plus ``reconciled`` (stale
    duplicate play-count rows folded away — see the reconcile pass below)."""
    counts = {"ratings": 0, "tags": 0, "play_counts": 0, "play_track_meta": 0,
              "reconciled": 0}

    async def _ident(track_id: str) -> str | None:
        ident = await identity.identity_for_track_id(track_id)
        return ident if (ident and ident != track_id) else None

    for tid, stars in (await database.get_all_ratings()).items():
        ident = await _ident(tid)
        if ident and await database.get_rating(ident) is None:
            await database.set_rating(ident, stars)
            counts["ratings"] += 1

    for tid, tags in (await database.get_all_tags()).items():
        ident = await _ident(tid)
        if ident and not await database.get_tags(ident):
            await database.set_tags(ident, tags)
            counts["tags"] += 1

    # Track play counts only — album/artist counts are name-keyed and survive.
    for row in await database.get_all_play_counts("track"):
        ident = await _ident(row["entity_id"])
        if ident and await database.get_play_count("track", ident) == 0:
            await database.set_play_count("track", ident, row["count"])
            counts["play_counts"] += 1

    all_meta = await database.get_all_play_track_meta()
    for tid, meta in all_meta.items():
        ident = await _ident(tid)
        if ident and ident not in all_meta:
            await database.set_play_track_meta(ident, meta)
            counts["play_track_meta"] += 1

    # Reconcile away duplicate Most-Played rows (ce-debug 2026-07-02). The copy
    # loops above are non-clobbering and, by U7's rollback-safe design, RETAIN
    # the original source / older-identity keyed rows. Once a track's identity is
    # re-minted (identity.resolve_clusters' collision repoint) those retained
    # rows accumulate, and get_top_played_tracks reads raw play_counts — so one
    # song surfaces once per stale key (same title, identical COPIED count; the
    # stale key no longer resolves to a live release). Fold every stale track key
    # that forward-resolves to a DIFFERENT, LIVE catalog identity into that
    # identity (max, never sum — the counts are copies of the same plays, not
    # additive) and drop the stale row + its now-orphaned display meta. A dormant
    # row whose source is disconnected forward-resolves to nothing live, so it is
    # preserved — the all-time leaderboard must survive a source going away.
    for row in await database.get_all_play_counts("track"):
        key = row["entity_id"]
        ident = await _ident(key)
        if not ident or await store.get_track(ident) is None:
            continue
        if row["count"] > await database.get_play_count("track", ident):
            await database.set_play_count("track", ident, row["count"])
        await database.delete_play_count("track", key)
        await database.delete_play_track_meta(key)
        counts["reconciled"] += 1

    return counts
