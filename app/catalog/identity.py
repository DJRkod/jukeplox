"""Allocate-once / match-forward stable identity (2026-06-27 multi-source plan U7).

A cluster's identity is allocated ONCE and matched forward across rescans via the
durable ``catalog_identity_alias`` table: every external id and every per-source
``local_key`` for the entity is registered as an alias of its identity. So

- an unchanged rescan reuses the same identity (any alias still resolves);
- a track that later gains an external id keeps its original identity — the new
  id attaches as an alias, never re-minting (so ratings stay attached);
- a per-source id (the compound rating key) resolves straight to the identity via
  ``identity_for_track_id``, which the migration (U7) and catalog-backed reads
  (U8) use to bridge the old compound-keyed metadata onto the new identities.

The minted identity is ``lookup_keys[0]`` — the smallest external id, else the
smallest source compound id — so on a Plex-only install identity equals the
existing rating key and the migration is inert.
"""

from __future__ import annotations

from app.catalog import merge, store


async def resolve_cluster(entity_type: str, cluster: list[dict]) -> str:
    """Return the stable identity for a merged cluster, allocating once.

    Reuse the identity any of the cluster's lookup keys already resolves to;
    otherwise mint ``lookup_keys[0]``. Either way, bind every lookup key to the
    chosen identity (``register_alias`` is OR-IGNORE, so an existing binding is
    never re-pointed — that is the never-re-mint guarantee)."""
    keys = merge.lookup_keys(cluster)
    if not keys:
        return f"empty:{entity_type}"

    identity: str | None = None
    for k in keys:
        identity = await store.find_identity(entity_type, k)
        if identity:
            break
    if identity is None:
        identity = keys[0]

    for k in keys:
        await store.register_alias(entity_type, k, identity)
    return identity


async def resolve_clusters(entity_type: str, clusters: list[list[dict]]) -> list[str]:
    """Resolve a whole scan's clusters to identities, guaranteeing they are
    DISTINCT.

    ``resolve_cluster`` reuses the identity any lookup key already resolves to
    (allocate-once). But a stale alias from a PRIOR over-merge can make two now-
    distinct clusters resolve to the SAME identity — and ``replace_catalog``'s
    ``INSERT OR IGNORE`` would then silently drop one entity's rows, leaving e.g.
    a second same-title album with zero tracks (ce-debug 2026-06-29). On such a
    collision, re-mint the later cluster to its own smallest lookup key and
    re-point its aliases (``repoint_alias`` overwrites), correcting the corrupt
    mapping so the catalog is consistent now and the next scan is clean. With no
    collision this is identical to calling ``resolve_cluster`` per cluster, so the
    allocate-once / match-forward guarantees are unchanged for the common case."""
    seen: set[str] = set()
    out: list[str] = []
    for c in clusters:
        ident = await resolve_cluster(entity_type, c)
        if ident in seen:
            keys = merge.lookup_keys(c)
            fresh = next((k for k in keys if k not in seen), None)
            if fresh is None:  # pathological: every key already taken → suffix
                base = keys[0] if keys else f"empty:{entity_type}"
                n = 2
                while f"{base}#{n}" in seen:
                    n += 1
                fresh = f"{base}#{n}"
            for k in keys:
                await store.repoint_alias(entity_type, k, fresh)
            ident = fresh
        seen.add(ident)
        out.append(ident)
    return out


async def identity_for_track_id(track_id: str) -> str | None:
    """The catalog identity a source's compound track id maps to, or None when
    the track isn't catalogued (e.g. its source is disconnected). On a Plex-only
    install this returns the track id unchanged (it is its own identity)."""
    return await store.find_identity("track", track_id)
