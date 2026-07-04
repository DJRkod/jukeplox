"""Catalog persistence layer (2026-06-27 multi-source plan U5).

Track-grained accessors over the ``catalog_*`` tables defined in
``app/database.py``'s ``_SCHEMA``. The schema lives there (with every other
table, so ``init_db`` builds it); the accessors live here to keep the growing
catalog API out of the already-large ``database`` module.

Two write surfaces with deliberately different lifecycles:

- ``replace_catalog`` atomic-replaces the CONTENT tables (artist/album/track)
  and the holds list in one transaction, mirroring ``set_browse_index`` — a
  scan fully swaps the visible catalog and a failed scan rolls back (the caller
  in U6 only replaces on a good crawl, so a provider failure never empties it).
- ``register_alias`` / ``find_identity`` touch the DURABLE alias table, which a
  scan never wipes. That durability is what makes identity allocate-once /
  match-forward: a vanished-then-returned entity, or a late external ID, maps
  back to its existing identity instead of re-minting (orchestrated by U7's
  ``identity`` module; this module only persists).

Accessors return plain dicts, matching ``database``'s convention. The
connection is shared via ``database._conn()`` (one connection, statements
serialized by aiosqlite).
"""

from app import database

# Column tuples kept as module constants so SELECT and INSERT never drift.
_ARTIST_COLS = "identity, title, base_key, thumb, release_count"
_ALBUM_COLS = (
    "identity, title, title_base, artist, artist_base_key, year, thumb,"
    " subtype, added_at, track_count"
)
_TRACK_COLS = (
    "identity, title, title_base, artist, artist_base_key, album, album_identity,"
    " album_artist, duration_ms, disc_number, track_number, genre, year, thumb"
)
_HOLD_COLS = "entity_type, identity, source_id, provider_local_key, priority, server_name"


def _row(cols: str, d: dict) -> tuple:
    """Positional values for an INSERT, in declared column order. Missing keys
    default to None so callers can omit optional fields."""
    return tuple(d.get(c) for c in cols.replace(" ", "").split(","))


# ── write: atomic content + holds replace ────────────────────────────────────

async def replace_catalog(
    artists: list[dict],
    albums: list[dict],
    tracks: list[dict],
    holds: list[dict],
) -> None:
    """Atomically replace the catalog content + holds in one transaction.

    Mirrors ``database.set_browse_index``: ``BEGIN IMMEDIATE`` → wipe → bulk
    insert → commit (rollback on any error). The alias table is intentionally
    untouched — its durability is the identity-stability guarantee. ``INSERT OR
    IGNORE`` guards against a duplicate identity within a single batch.
    """
    db = database._conn()
    await db.execute("BEGIN IMMEDIATE")
    try:
        await db.execute("DELETE FROM catalog_artist")
        await db.execute("DELETE FROM catalog_album")
        await db.execute("DELETE FROM catalog_track")
        await db.execute("DELETE FROM catalog_holds")
        if artists:
            await db.executemany(
                f"INSERT OR IGNORE INTO catalog_artist ({_ARTIST_COLS})"
                " VALUES (?, ?, ?, ?, ?)",
                [_row(_ARTIST_COLS, a) for a in artists],
            )
        if albums:
            await db.executemany(
                f"INSERT OR IGNORE INTO catalog_album ({_ALBUM_COLS})"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [_row(_ALBUM_COLS, a) for a in albums],
            )
        if tracks:
            await db.executemany(
                f"INSERT OR IGNORE INTO catalog_track ({_TRACK_COLS})"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [_row(_TRACK_COLS, t) for t in tracks],
            )
        if holds:
            await db.executemany(
                f"INSERT OR IGNORE INTO catalog_holds ({_HOLD_COLS})"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [_row(_HOLD_COLS, h) for h in holds],
            )
        await db.commit()
    except Exception:
        await db.rollback()
        raise


# ── read: artists ────────────────────────────────────────────────────────────

async def get_artists() -> list[dict]:
    """Full artist roster. Empty list when the catalog is unpopulated."""
    async with database._conn().execute(
        f"SELECT {_ARTIST_COLS} FROM catalog_artist ORDER BY title COLLATE NOCASE"
    ) as cur:
        return [dict(row) async for row in cur]


async def get_artist(identity: str) -> dict | None:
    async with database._conn().execute(
        f"SELECT {_ARTIST_COLS} FROM catalog_artist WHERE identity = ?", (identity,)
    ) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


# ── read: albums ─────────────────────────────────────────────────────────────

async def get_albums() -> list[dict]:
    async with database._conn().execute(
        f"SELECT {_ALBUM_COLS} FROM catalog_album ORDER BY title COLLATE NOCASE"
    ) as cur:
        return [dict(row) async for row in cur]


async def get_albums_for_artist(artist_base_key: str) -> list[dict]:
    """Every album whose release-artist base-normalizes to ``artist_base_key``
    (indexed — proportional to the artist's releases, not the library)."""
    async with database._conn().execute(
        f"SELECT {_ALBUM_COLS} FROM catalog_album WHERE artist_base_key = ?"
        " ORDER BY year, title COLLATE NOCASE",
        (artist_base_key,),
    ) as cur:
        return [dict(row) async for row in cur]


async def get_album(identity: str) -> dict | None:
    async with database._conn().execute(
        f"SELECT {_ALBUM_COLS} FROM catalog_album WHERE identity = ?", (identity,)
    ) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


# ── read: tracks ─────────────────────────────────────────────────────────────

async def get_tracks_for_album(album_identity: str) -> list[dict]:
    """An album's tracks in disc/track order (NULLs sort last via COALESCE)."""
    async with database._conn().execute(
        f"SELECT {_TRACK_COLS} FROM catalog_track WHERE album_identity = ?"
        " ORDER BY COALESCE(disc_number, 1), COALESCE(track_number, 1000000),"
        " title COLLATE NOCASE",
        (album_identity,),
    ) as cur:
        return [dict(row) async for row in cur]


async def get_track(identity: str) -> dict | None:
    async with database._conn().execute(
        f"SELECT {_TRACK_COLS} FROM catalog_track WHERE identity = ?", (identity,)
    ) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_all_tracks() -> list[dict]:
    """Every track row — the candidate pool for whole-library random and the
    multi-source search floor (U8/U13)."""
    async with database._conn().execute(
        f"SELECT {_TRACK_COLS} FROM catalog_track"
    ) as cur:
        return [dict(row) async for row in cur]


async def is_empty() -> bool:
    """True when no tracks are catalogued — distinguishes the 'scanned, nothing
    found' state from the zero-source state (U15)."""
    async with database._conn().execute(
        "SELECT 1 FROM catalog_track LIMIT 1"
    ) as cur:
        return (await cur.fetchone()) is None


# ── read: holds ──────────────────────────────────────────────────────────────

async def get_holds(entity_type: str, identity: str) -> list[dict]:
    """An entity's holds, priority-ascending (highest-priority source first),
    ``source_id`` as a stable tie-break. This is the ordered list U9 snapshots
    onto a queue item and walks for play-time fallback."""
    async with database._conn().execute(
        f"SELECT {_HOLD_COLS} FROM catalog_holds"
        " WHERE entity_type = ? AND identity = ?"
        " ORDER BY priority, source_id",
        (entity_type, identity),
    ) as cur:
        return [dict(row) async for row in cur]


async def get_identities_held_by_source(
    source_id: str, entity_type: str = "track"
) -> list[str]:
    """Identities a given source holds — used when a source is removed to find
    its solely-held tracks (U9)."""
    async with database._conn().execute(
        "SELECT identity FROM catalog_holds WHERE source_id = ? AND entity_type = ?",
        (source_id, entity_type),
    ) as cur:
        return [row["identity"] async for row in cur]


# ── durable identity alias (find-or-create lookup; logic lives in U7) ─────────

async def find_identity(entity_type: str, lookup_key: str) -> str | None:
    """The stable identity a lookup key (external ID or normalized hash) resolves
    to, or None if unseen. The durable half of allocate-once identity."""
    async with database._conn().execute(
        "SELECT identity FROM catalog_identity_alias"
        " WHERE entity_type = ? AND lookup_key = ?",
        (entity_type, lookup_key),
    ) as cur:
        row = await cur.fetchone()
        return row["identity"] if row else None


async def register_alias(entity_type: str, lookup_key: str, identity: str) -> None:
    """Bind a lookup key to an identity, durably. ``INSERT OR IGNORE`` — an
    existing binding is never clobbered, so a key can't be re-pointed at a new
    identity (re-minting is what U7 forbids)."""
    db = database._conn()
    await db.execute(
        "INSERT OR IGNORE INTO catalog_identity_alias (entity_type, lookup_key, identity)"
        " VALUES (?, ?, ?)",
        (entity_type, lookup_key, identity),
    )
    await db.commit()


async def repoint_alias(entity_type: str, lookup_key: str, identity: str) -> None:
    """Force a lookup key to a new identity (INSERT OR REPLACE), CORRECTING a stale
    binding from a prior over-merge. Unlike ``register_alias`` (OR IGNORE, the
    never-re-mint guarantee), this overwrites — used only by the collision path in
    ``identity.resolve_clusters`` when two distinct clusters would otherwise share
    an identity (ce-debug 2026-06-29)."""
    db = database._conn()
    await db.execute(
        "INSERT OR REPLACE INTO catalog_identity_alias (entity_type, lookup_key, identity)"
        " VALUES (?, ?, ?)",
        (entity_type, lookup_key, identity),
    )
    await db.commit()


async def get_aliases_for_identity(entity_type: str, identity: str) -> list[str]:
    """Every lookup key bound to an identity — lets U7 see that a late external
    ID and an earlier normalized-hash key both point at one entity."""
    async with database._conn().execute(
        "SELECT lookup_key FROM catalog_identity_alias"
        " WHERE entity_type = ? AND identity = ? ORDER BY lookup_key",
        (entity_type, identity),
    ) as cur:
        return [row["lookup_key"] async for row in cur]
