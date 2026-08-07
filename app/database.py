import asyncio

import aiosqlite

from app.config import settings

_db: aiosqlite.Connection | None = None
# Shared lock across all write-transactions on the connection.
# aiosqlite serializes statements per connection, but explicit
# BEGIN IMMEDIATE will throw "cannot start a transaction within a
# transaction" if a previous task left a transaction open (e.g.,
# save_queue and save_history racing on track-end — and, fresh-install
# audit F2 2026-08-06, the browse-index and catalog refreshes colliding
# on the empty-enabled fast path). INVARIANT: every explicit
# BEGIN/BEGIN IMMEDIATE transaction on this connection holds this lock
# (save_plex_servers, set_genre_cache, set_credit_cache,
# set_browse_index, catalog/store.replace_catalog), as do the
# crawl-scale implicit-transaction alias writers
# (catalog/store.register_alias / repoint_alias) whose execute→commit
# windows otherwise interleave with locked writers during identity
# resolution. Low-frequency implicit writers (set_setting etc.) remain
# unlocked — accepted residual risk until the isolation_level=None
# rework.
_write_tx_lock = asyncio.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plex_config (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    server_url  TEXT,
    token       TEXT,
    client_id   TEXT
);

CREATE TABLE IF NOT EXISTS plex_servers (
    machine_id  TEXT PRIMARY KEY,
    server_url  TEXT NOT NULL,
    name        TEXT NOT NULL,
    owner       TEXT NOT NULL,
    token       TEXT NOT NULL,
    client_id   TEXT NOT NULL,
    owned       INTEGER          -- 1/0; NULL = unknown (pre-2026-06-11 rows)
);

CREATE TABLE IF NOT EXISTS jellyfin_sources (
    source_id   TEXT PRIMARY KEY,   -- registry key namespace ({source_id}:{itemId})
    server_url  TEXT NOT NULL,
    name        TEXT NOT NULL,      -- friendly display name for the source picker
    token       TEXT NOT NULL,      -- AccessToken — credential is TOKEN-ONLY,
    user_id     TEXT NOT NULL,      -- never the account password (plan R24)
    device_id   TEXT NOT NULL       -- stable DeviceId minted at connect time
);

CREATE TABLE IF NOT EXISTS local_sources (
    source_id TEXT PRIMARY KEY,   -- registry key namespace ({source_id}:{relpath})
    name      TEXT NOT NULL,      -- friendly display name for the source picker
    root_dir  TEXT NOT NULL       -- absolute root directory crawled by LocalSource
);

CREATE TABLE IF NOT EXISTS enabled_libraries (
    section_key TEXT PRIMARY KEY,
    name        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    token       TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS queue_state (
    position      INTEGER PRIMARY KEY,
    track_id      TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    added_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS play_counts (
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    count       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (entity_type, entity_id)
);

CREATE TABLE IF NOT EXISTS play_history (
    position      INTEGER PRIMARY KEY,
    track_id      TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    played_at     TEXT NOT NULL
);

-- 2026-06-10 most-played plan U1: display metadata for counted tracks,
-- captured at count time (play_history is a bounded deque and cannot back
-- an all-time leaderboard). JSON is the same dict /api/most-played serves.
CREATE TABLE IF NOT EXISTS play_track_meta (
    track_id      TEXT PRIMARY KEY,
    metadata_json TEXT NOT NULL
);

-- 2026-06-26 track-ratings-and-tags plan U1: admin-authored, Jukeplox-LOCAL
-- per-track rating + tags, keyed by the compound track_id (NEVER written to
-- Plex). One rating row per track (1-5; 0/clear deletes the row). Tags are a
-- single JSON array per track (play_track_meta pattern), capped/normalized in
-- the setter — the small per-track cap doesn't justify a row-per-tag table.
CREATE TABLE IF NOT EXISTS track_ratings (
    track_id TEXT PRIMARY KEY,
    stars    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS track_tags (
    track_id  TEXT PRIMARY KEY,
    tags_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS genre_cache (
    name  TEXT NOT NULL,
    count INTEGER NOT NULL
);

-- 2026-06-10 per-track credits plan U2: one row per (credited act, release)
-- pair scanned from track credits. name_lower is the merge/lookup key;
-- album_id is the made (possibly server-compound) id usable by the existing
-- album drill-ins.
CREATE TABLE IF NOT EXISTS credit_cache (
    name         TEXT NOT NULL,
    name_lower   TEXT NOT NULL,
    album_id     TEXT NOT NULL,
    album_title  TEXT NOT NULL,
    album_artist TEXT NOT NULL,
    album_thumb  TEXT,
    album_year   INTEGER,
    server_name  TEXT,
    PRIMARY KEY (name_lower, album_id)
);
CREATE INDEX IF NOT EXISTS idx_credit_cache_name_lower ON credit_cache (name_lower);

-- 2026-06-21 browse-index plan U1: persistent cross-server browse index.
-- Generalizes the credit_cache pattern (atomic-replace cache keyed by a
-- normalized lookup column) to the full artist + album roster, so drill-ins
-- resolve via an indexed lookup instead of re-loading every library's full
-- artist list. base_key / *_base columns hold RULE-INDEPENDENT normalization
-- (app.plex.client._normalize_text — case+typographic fold+strip, NO pattern
-- rules); the rule-aware merge stays at request time so rule edits need no
-- rebuild (plan R11). Raw title/artist are preserved so the existing
-- request-time dedup/group pipeline reconstructs Artist/Album objects unchanged.
CREATE TABLE IF NOT EXISTS browse_artist_index (
    artist_id     TEXT PRIMARY KEY,   -- compound "{machine_id}:{rating_key}"
    title         TEXT NOT NULL,
    base_key      TEXT NOT NULL,
    thumb         TEXT,
    release_count INTEGER,
    server_name   TEXT,
    section_key   TEXT
);
CREATE INDEX IF NOT EXISTS idx_browse_artist_base ON browse_artist_index (base_key);

CREATE TABLE IF NOT EXISTS browse_album_index (
    album_id        TEXT PRIMARY KEY,  -- compound "{machine_id}:{rating_key}"
    title           TEXT NOT NULL,
    title_base      TEXT NOT NULL,
    artist          TEXT NOT NULL,
    artist_base_key TEXT NOT NULL,
    year            INTEGER,
    thumb           TEXT,
    subtype         TEXT,
    added_at        INTEGER,
    track_count     INTEGER,
    server_name     TEXT,
    section_key     TEXT
);
CREATE INDEX IF NOT EXISTS idx_browse_album_artist ON browse_album_index (artist_base_key);
CREATE INDEX IF NOT EXISTS idx_browse_album_identity ON browse_album_index (title_base, artist_base_key);

-- 2026-06-27 multi-source plan U5: track-grained UNIFIED CATALOG. Supersedes
-- the browse-index (artist+album only) by adding a track table, a STABLE
-- allocate-once local identity per entity (the `identity` PK), an
-- external-ID / normalized-hash lookup table (catalog_identity_alias) that
-- makes identity match-forward across rescans, and a priority-ordered
-- per-entity holds list (catalog_holds) naming which sources hold each entity
-- plus the provider-local stream/drill key for each. The browse-index tables
-- are retained IN PARALLEL until a new source type validates (plan U7 rollback
-- window). Content tables (artist/album/track/holds) are atomic-replaced per
-- scan; the alias table is DURABLE (never wiped) so stable identities — and the
-- ratings/play-counts re-keyed onto them (U7) — survive rebuilds. base_key /
-- *_base columns hold rule-independent normalization (the rule-aware merge runs
-- at request time, mirroring the browse-index), so rule edits need no rebuild.
CREATE TABLE IF NOT EXISTS catalog_artist (
    identity      TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    base_key      TEXT NOT NULL,
    thumb         TEXT,
    release_count INTEGER
);
CREATE INDEX IF NOT EXISTS idx_catalog_artist_base ON catalog_artist (base_key);

CREATE TABLE IF NOT EXISTS catalog_album (
    identity        TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    title_base      TEXT NOT NULL,
    artist          TEXT NOT NULL,
    artist_base_key TEXT NOT NULL,
    year            INTEGER,
    thumb           TEXT,
    subtype         TEXT,
    added_at        INTEGER,
    track_count     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_catalog_album_artist ON catalog_album (artist_base_key);
CREATE INDEX IF NOT EXISTS idx_catalog_album_identity ON catalog_album (title_base, artist_base_key);

CREATE TABLE IF NOT EXISTS catalog_track (
    identity        TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    title_base      TEXT NOT NULL,
    artist          TEXT NOT NULL,
    artist_base_key TEXT NOT NULL,
    album           TEXT,
    album_identity  TEXT,
    album_artist    TEXT,
    duration_ms     INTEGER,
    disc_number     INTEGER,
    track_number    INTEGER,
    genre           TEXT,
    year            INTEGER,
    thumb           TEXT
);
CREATE INDEX IF NOT EXISTS idx_catalog_track_album ON catalog_track (album_identity);
CREATE INDEX IF NOT EXISTS idx_catalog_track_identity ON catalog_track (title_base, artist_base_key);

-- Which sources hold each entity, and the provider-local key (stream-key
-- remainder for tracks; per-source drill key for albums). priority is the
-- global source-priority rank captured at scan time (lower = higher); fallback
-- (U9) walks this list. PK is per (entity, source) so one source holds an
-- entity once.
CREATE TABLE IF NOT EXISTS catalog_holds (
    entity_type        TEXT NOT NULL,    -- 'track' | 'album' | 'artist'
    identity           TEXT NOT NULL,
    source_id          TEXT NOT NULL,
    provider_local_key TEXT NOT NULL,
    priority           INTEGER NOT NULL,
    server_name        TEXT,
    PRIMARY KEY (entity_type, identity, source_id)
);
CREATE INDEX IF NOT EXISTS idx_catalog_holds_entity ON catalog_holds (entity_type, identity);
CREATE INDEX IF NOT EXISTS idx_catalog_holds_source ON catalog_holds (source_id);

-- DURABLE find-or-create lookup: external ID (MusicBrainz etc.) or
-- normalized-hash key → stable local identity. Never wiped by a scan, so a
-- late external ID can attach as an alias of an existing identity (U7) and a
-- vanished-then-returned entity reuses its identity. Lookup keys carry an
-- entity_type scope so a track and album sharing a raw id never collide.
CREATE TABLE IF NOT EXISTS catalog_identity_alias (
    entity_type TEXT NOT NULL,
    lookup_key  TEXT NOT NULL,
    identity    TEXT NOT NULL,
    PRIMARY KEY (entity_type, lookup_key)
);
CREATE INDEX IF NOT EXISTS idx_catalog_alias_identity ON catalog_identity_alias (entity_type, identity);
"""


def _conn() -> aiosqlite.Connection:
    assert _db is not None, "Database not initialized — call init_db() first"
    return _db


async def init_db() -> None:
    global _db
    _db = await aiosqlite.connect(settings.db_path)
    _db.row_factory = aiosqlite.Row
    await _db.executescript(_SCHEMA)
    await _migrate_columns()
    await _migrate_seal_credentials()
    await _db.commit()


async def _migrate_columns() -> None:
    """Idempotent column additions for pre-existing databases.

    CREATE TABLE IF NOT EXISTS never alters existing tables, so columns
    added after a table first shipped need a guarded ALTER. SQLite ADD
    COLUMN is metadata-only (cheap, non-destructive); existing rows get
    NULL, which callers must treat as "unknown".
    """
    async with _db.execute("PRAGMA table_info(plex_servers)") as cur:
        cols = {row["name"] async for row in cur}
    if "owned" not in cols:
        # Collected-library plan U1 (2026-06-11): NULL = ownership unknown
        # (server linked before this shipped) → ranks after known-owned,
        # alphabetical fallback. A re-link/re-save populates it.
        await _db.execute("ALTER TABLE plex_servers ADD COLUMN owned INTEGER")

    async with _db.execute("PRAGMA table_info(browse_album_index)") as cur:
        album_cols = {row["name"] async for row in cur}
    if "added_at" not in album_cols:
        # Recently Added plan U2: NULL = add-date unknown (index built before
        # this shipped) → sorts last in the feed. The next crawl populates it.
        await _db.execute("ALTER TABLE browse_album_index ADD COLUMN added_at INTEGER")
    if "track_count" not in album_cols:
        # Same-title plan U2: NULL = track count unknown (index built before this
        # shipped) → can't confirm "same release" for cross-server folding, so
        # treated conservatively (no fold). The next crawl populates it.
        await _db.execute("ALTER TABLE browse_album_index ADD COLUMN track_count INTEGER")


async def _migrate_seal_credentials() -> None:
    """One-time upgrade: re-seal any plaintext credential rows at rest (U17/R24).

    Runs on every init_db but is idempotent — already-sealed rows are skipped, so
    after the first upgrade it's a few cheap SELECTs over tiny tables. Reads the
    RAW stored value (not via the opening accessors) and rewrites it sealed. A
    leaked pre-U17 DB still has plaintext tokens until this runs once."""
    from app.sources import secrets

    async def _reseal(select_sql: str, update_sql: str, key_col: str) -> None:
        async with _db.execute(select_sql) as cur:
            rows = [(r[key_col], r["token"]) async for r in cur]
        for key, tok in rows:
            if tok and not secrets.is_sealed(tok):
                await _db.execute(update_sql, (secrets.seal(tok), key))

    await _reseal(
        "SELECT id, token FROM plex_config",
        "UPDATE plex_config SET token = ? WHERE id = ?", "id")
    await _reseal(
        "SELECT machine_id, token FROM plex_servers",
        "UPDATE plex_servers SET token = ? WHERE machine_id = ?", "machine_id")
    await _reseal(
        "SELECT source_id, token FROM jellyfin_sources",
        "UPDATE jellyfin_sources SET token = ? WHERE source_id = ?", "source_id")


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None


# ── settings ─────────────────────────────────────────────────────────────────

async def get_setting(key: str, default: str | None = None) -> str | None:
    async with _conn().execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
        row = await cur.fetchone()
        return row["value"] if row else default


async def set_setting(key: str, value: str) -> None:
    db = _conn()
    await db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    await db.commit()


async def get_settings_with_prefix(prefix: str) -> dict[str, str]:
    """Return all settings whose key starts with `prefix`, keyed by the
    suffix (prefix stripped). Used by callers that need to bulk-load
    namespaced keys (e.g. per-device AirPlay protocol verdicts) without
    issuing one round-trip per device.
    """
    async with _conn().execute(
        "SELECT key, value FROM settings WHERE key LIKE ?",
        (f"{prefix}%",),
    ) as cur:
        return {row["key"][len(prefix):]: row["value"] async for row in cur}


async def delete_setting(key: str) -> None:
    db = _conn()
    await db.execute("DELETE FROM settings WHERE key = ?", (key,))
    await db.commit()


# ── plex config ───────────────────────────────────────────────────────────────

async def get_plex_config() -> dict | None:
    from app.sources import secrets
    async with _conn().execute("SELECT * FROM plex_config WHERE id = 1") as cur:
        row = await cur.fetchone()
        if not row:
            return None
        d = dict(row)
        d["token"] = secrets.open_secret(d.get("token"))  # U17: opened for use
        return d


async def set_plex_config(server_url: str, token: str, client_id: str) -> None:
    from app.sources import secrets
    db = _conn()
    await db.execute(
        "INSERT INTO plex_config (id, server_url, token, client_id) VALUES (1, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "server_url = excluded.server_url, token = excluded.token, client_id = excluded.client_id",
        (server_url, secrets.seal(token), client_id),  # U17: sealed at rest (R24)
    )
    await db.commit()


# ── plex servers (multi-server support) ──────────────────────────────────────

async def save_plex_servers(servers: list[dict]) -> None:
    from app.sources import secrets
    db = _conn()
    async with _write_tx_lock:
        await db.execute("BEGIN")
        try:
            await db.execute("DELETE FROM plex_servers")
            for s in servers:
                # owned: 1/0 when discovery supplied it, NULL when absent
                # (legacy callers) — NULL means "unknown" to the rank logic.
                owned = s.get("owned")
                await db.execute(
                    "INSERT OR REPLACE INTO plex_servers (machine_id, server_url, name, owner, token, client_id, owned) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (s["machine_id"], s["server_url"], s["name"], s["owner"],
                     secrets.seal(s["token"]), s["client_id"],  # U17: sealed at rest (R24)
                     None if owned is None else int(bool(owned))),
                )
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def get_plex_servers() -> list[dict]:
    from app.sources import secrets
    async with _conn().execute("SELECT * FROM plex_servers") as cur:
        rows = [dict(row) async for row in cur]
    for r in rows:
        r["token"] = secrets.open_secret(r.get("token"))  # U17: opened for use
    return rows


# ── jellyfin sources (multi-source plan U10) ─────────────────────────────────

async def get_jellyfin_sources() -> list[dict]:
    from app.sources import secrets
    async with _conn().execute("SELECT * FROM jellyfin_sources") as cur:
        rows = [dict(row) async for row in cur]
    for r in rows:
        r["token"] = secrets.open_secret(r.get("token"))  # U17: opened for use
    return rows


async def save_jellyfin_source(
    source_id: str, server_url: str, name: str, token: str, user_id: str, device_id: str,
) -> None:
    """Upsert one Jellyfin source. Stores the AccessToken + UserId only — the
    account password is never accepted here (plan R24: credential token-only).
    The token is sealed at rest via app.sources.secrets (U17)."""
    from app.sources import secrets
    db = _conn()
    await db.execute(
        "INSERT INTO jellyfin_sources (source_id, server_url, name, token, user_id, device_id) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(source_id) DO UPDATE SET "
        "server_url = excluded.server_url, name = excluded.name, token = excluded.token, "
        "user_id = excluded.user_id, device_id = excluded.device_id",
        (source_id, server_url, name, secrets.seal(token), user_id, device_id),  # U17 (R24)
    )
    await db.commit()


async def delete_jellyfin_source(source_id: str) -> None:
    db = _conn()
    await db.execute("DELETE FROM jellyfin_sources WHERE source_id = ?", (source_id,))
    await db.commit()


# ── local file sources (multi-source plan U11) ───────────────────────────────

async def get_local_sources() -> list[dict]:
    async with _conn().execute("SELECT * FROM local_sources") as cur:
        return [dict(row) async for row in cur]


async def save_local_source(source_id: str, name: str, root_dir: str) -> None:
    """Upsert one local-files source (a crawled root directory). Read-only — no
    credential, no media writes (plan R6)."""
    db = _conn()
    await db.execute(
        "INSERT INTO local_sources (source_id, name, root_dir) VALUES (?, ?, ?) "
        "ON CONFLICT(source_id) DO UPDATE SET name = excluded.name, root_dir = excluded.root_dir",
        (source_id, name, root_dir),
    )
    await db.commit()


async def delete_local_source(source_id: str) -> None:
    db = _conn()
    await db.execute("DELETE FROM local_sources WHERE source_id = ?", (source_id,))
    await db.commit()


# ── enabled libraries ─────────────────────────────────────────────────────────

async def get_enabled_libraries() -> list[dict]:
    async with _conn().execute("SELECT * FROM enabled_libraries ORDER BY name") as cur:
        return [dict(row) async for row in cur]


def source_id_from_section_key(section_key) -> str:
    """The owning source_id of a library section key. Keys are
    ``"{source_id}:{section}"``; the legacy single Plex server (machine_id == "")
    emits a bare, colon-less key whose source_id is "" (Libraries-panel U2)."""
    k = str(section_key)
    return k.split(":", 1)[0] if ":" in k else ""


async def get_effective_enabled_libraries() -> list[dict]:
    """Enabled-library rows MINUS any whose source is vetoed via
    ``disabled_sources`` (Libraries-panel U2). This is the guest-visible gate:
    every read path that decides what content reaches guests filters through here,
    so a whole-source OFF switch hides all of that source's libraries WITHOUT
    deleting their ``enabled_libraries`` rows — the per-library selection is
    remembered across an off->on toggle. The admin per-library listing keeps using
    ``get_enabled_libraries`` (raw) so the drill-in still shows a vetoed source's
    remembered checkboxes."""
    rows = await get_enabled_libraries()
    try:
        disabled = set(await get_disabled_sources())
    except Exception:
        # Veto set unreadable (e.g. DB error) -> fail open, serve all enabled, same
        # availability posture as get_enabled_libraries itself. A dead settings read
        # would break get_enabled_libraries too, so nothing is lost by not vetoing.
        return rows
    if not disabled:
        return rows
    return [r for r in rows if source_id_from_section_key(r["section_key"]) not in disabled]


async def toggle_library(section_key: str, name: str, enabled: bool) -> None:
    db = _conn()
    if enabled:
        await db.execute(
            "INSERT INTO enabled_libraries (section_key, name) VALUES (?, ?) "
            "ON CONFLICT(section_key) DO UPDATE SET name = excluded.name",
            (section_key, name),
        )
    else:
        await db.execute(
            "DELETE FROM enabled_libraries WHERE section_key = ?", (section_key,)
        )
    await db.commit()


# ── sessions ──────────────────────────────────────────────────────────────────

async def create_session(token: str, created_at: str, expires_at: str) -> None:
    db = _conn()
    await db.execute(
        "INSERT INTO admin_sessions (token, created_at, expires_at) VALUES (?, ?, ?)",
        (token, created_at, expires_at),
    )
    await db.commit()


async def get_session(token: str) -> dict | None:
    async with _conn().execute(
        "SELECT * FROM admin_sessions WHERE token = ?", (token,)
    ) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


async def delete_session(token: str) -> None:
    db = _conn()
    await db.execute("DELETE FROM admin_sessions WHERE token = ?", (token,))
    await db.commit()


async def delete_expired_sessions(now: str) -> None:
    db = _conn()
    await db.execute("DELETE FROM admin_sessions WHERE expires_at < ?", (now,))
    await db.commit()


async def delete_all_sessions() -> None:
    db = _conn()
    await db.execute("DELETE FROM admin_sessions")
    await db.commit()


# ── queue state ───────────────────────────────────────────────────────────────

async def save_queue(items: list[dict]) -> None:
    async with _write_tx_lock:
        db = _conn()
        # aiosqlite's default mode auto-manages the transaction around
        # the DELETE + executemany + commit pair. Explicit
        # BEGIN IMMEDIATE was throwing "cannot start a transaction
        # within a transaction" when save_queue and save_history fired
        # concurrently on track-end, which broke queue auto-advance.
        try:
            await db.execute("DELETE FROM queue_state")
            await db.executemany(
                "INSERT INTO queue_state (position, track_id, metadata_json, added_at) "
                "VALUES (?, ?, ?, ?)",
                [(i, item["track_id"], item["metadata_json"], item["added_at"]) for i, item in enumerate(items)],
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def load_queue() -> list[dict]:
    async with _conn().execute("SELECT * FROM queue_state ORDER BY position") as cur:
        return [dict(row) async for row in cur]


# ── play history ─────────────────────────────────────────────────────────────

async def save_history(items: list[dict]) -> None:
    async with _write_tx_lock:
        db = _conn()
        try:
            await db.execute("DELETE FROM play_history")
            await db.executemany(
                "INSERT INTO play_history (position, track_id, metadata_json, played_at) "
                "VALUES (?, ?, ?, ?)",
                [(i, item["track_id"], item["metadata_json"], item["added_at"]) for i, item in enumerate(items)],
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def load_history() -> list[dict]:
    async with _conn().execute("SELECT * FROM play_history ORDER BY position") as cur:
        rows = []
        async for row in cur:
            d = dict(row)
            d["added_at"] = d.pop("played_at")
            rows.append(d)
        return rows


# ── play counts ───────────────────────────────────────────────────────────────

async def increment_play_count(entity_type: str, entity_id: str) -> None:
    """Increment play count for a track, album, or artist. entity_type: 'track'|'album'|'artist'."""
    db = _conn()
    await db.execute(
        "INSERT INTO play_counts (entity_type, entity_id, count) VALUES (?, ?, 1) "
        "ON CONFLICT(entity_type, entity_id) DO UPDATE SET count = count + 1",
        (entity_type, entity_id),
    )
    await db.commit()


async def decrement_play_count(entity_type: str, entity_id: str) -> None:
    """Decrement a play count, floored at zero (never negative); a no-op when the
    row is absent. The inverse of one ``increment_play_count`` — used by
    ``state.unrecord_play`` to roll back a single play (admin play-data curation)."""
    db = _conn()
    await db.execute(
        "UPDATE play_counts SET count = MAX(count - 1, 0) "
        "WHERE entity_type = ? AND entity_id = ?",
        (entity_type, entity_id),
    )
    await db.commit()


async def set_play_count(entity_type: str, entity_id: str, count: int) -> None:
    """Set an absolute play count (catalog identity re-key migration, plan U7).
    ``increment_play_count`` is the normal path; this exists so the migration can
    copy a counted track's total onto its catalog identity."""
    db = _conn()
    await db.execute(
        "INSERT INTO play_counts (entity_type, entity_id, count) VALUES (?, ?, ?) "
        "ON CONFLICT(entity_type, entity_id) DO UPDATE SET count = excluded.count",
        (entity_type, entity_id, int(count)),
    )
    await db.commit()


async def delete_play_count(entity_type: str, entity_id: str) -> None:
    """Remove a play-count row. Used by the catalog reconcile (migrate.py) to
    drop a stale keyed row once its count has been folded onto the live identity,
    so the leaderboard no longer shows the same song once per retained key."""
    db = _conn()
    await db.execute(
        "DELETE FROM play_counts WHERE entity_type = ? AND entity_id = ?",
        (entity_type, entity_id),
    )
    await db.commit()


async def set_play_track_meta(track_id: str, metadata: dict) -> None:
    """Upsert display metadata for a counted track (most-played plan U1).
    Fresh metadata wins — a re-played track self-corrects stale rows."""
    db = _conn()
    await db.execute(
        "INSERT INTO play_track_meta (track_id, metadata_json) VALUES (?, ?) "
        "ON CONFLICT(track_id) DO UPDATE SET metadata_json = excluded.metadata_json",
        (track_id, _json.dumps(metadata)),
    )
    await db.commit()


async def delete_play_track_meta(track_id: str) -> None:
    """Remove a captured-metadata row. Reconcile cleanup for a stale key whose
    play-count row was folded onto the live identity (avoids orphan meta rows)."""
    db = _conn()
    await db.execute("DELETE FROM play_track_meta WHERE track_id = ?", (track_id,))
    await db.commit()


async def get_top_played_tracks(limit: int | None = 100) -> list[dict]:
    """Top played tracks joined with captured metadata, count-descending.

    Rows: {"track_id", "count", "metadata"} where metadata is the parsed
    dict or None when no meta row exists yet (pre-feature counts — the
    endpoint live-backfills those).

    ``limit=None`` returns ALL counted tracks (SQLite ``LIMIT -1``). Popular
    Random uses this so its candidate pool is gated only by the play-count
    threshold, not by the leaderboard's display cap — the two are unrelated."""
    async with _conn().execute(
        "SELECT pc.entity_id AS track_id, pc.count, ptm.metadata_json"
        " FROM play_counts pc"
        " LEFT JOIN play_track_meta ptm ON ptm.track_id = pc.entity_id"
        " WHERE pc.entity_type = 'track' AND pc.count > 0"
        " ORDER BY pc.count DESC, pc.entity_id"
        " LIMIT ?",
        (-1 if limit is None else limit,),
    ) as cur:
        rows = []
        async for row in cur:
            meta = None
            if row["metadata_json"]:
                try:
                    meta = _json.loads(row["metadata_json"])
                except (ValueError, TypeError):
                    meta = None
            rows.append({"track_id": row["track_id"], "count": row["count"], "metadata": meta})
        return rows


async def get_all_play_track_meta() -> dict[str, dict]:
    """Every counted track's captured display metadata, as ``{track_id: dict}``.
    Used by the catalog identity re-key migration (plan U7) to carry Most-Played
    display rows onto the new identities."""
    async with _conn().execute("SELECT track_id, metadata_json FROM play_track_meta") as cur:
        out: dict[str, dict] = {}
        async for row in cur:
            try:
                data = _json.loads(row["metadata_json"]) if row["metadata_json"] else None
            except (ValueError, TypeError):
                data = None
            if isinstance(data, dict):
                out[row["track_id"]] = data
        return out


async def get_play_count(entity_type: str, entity_id: str) -> int:
    async with _conn().execute(
        "SELECT count FROM play_counts WHERE entity_type = ? AND entity_id = ?",
        (entity_type, entity_id),
    ) as cur:
        row = await cur.fetchone()
        return row["count"] if row else 0


async def get_play_counts(entity_type: str, entity_ids: list[str]) -> dict[str, int]:
    """Batch fetch play counts for a list of IDs. Returns {id: count}."""
    if not entity_ids:
        return {}
    placeholders = ",".join("?" * len(entity_ids))
    async with _conn().execute(
        f"SELECT entity_id, count FROM play_counts WHERE entity_type = ? AND entity_id IN ({placeholders})",
        [entity_type, *entity_ids],
    ) as cur:
        return {row["entity_id"]: row["count"] async for row in cur}


async def get_all_play_counts(entity_type: str) -> list[dict]:
    """Return all play counts for a given entity type, ordered by count descending."""
    async with _conn().execute(
        "SELECT entity_id, count FROM play_counts WHERE entity_type = ? ORDER BY count DESC",
        (entity_type,),
    ) as cur:
        return [{"entity_id": row["entity_id"], "count": row["count"]} async for row in cur]


# ── track ratings + tags (2026-06-26 track-ratings-and-tags plan U1) ─────────
# Admin-authored, Jukeplox-LOCAL per-track metadata keyed by compound track_id.
# Plex's native rating is never read or written (plan R3). Caps live here as the
# single normalization chokepoint so every writer (endpoint, future importer)
# enforces them identically.

TAG_MAX_LEN = 40          # characters per tag (plan U1 decision)
TAGS_MAX_PER_TRACK = 12   # tags per track (plan U1 decision)


async def get_rating(track_id: str) -> int | None:
    """A track's 0-5 rating, or None when unrated."""
    async with _conn().execute(
        "SELECT stars FROM track_ratings WHERE track_id = ?", (track_id,)
    ) as cur:
        row = await cur.fetchone()
        return row["stars"] if row else None


async def set_rating(track_id: str, stars: int) -> None:
    """Set a track's rating. ``stars <= 0`` clears it (deletes the row) — there
    is no distinct 'rated zero' state (plan U1 decision); 1-5 upserts, clamped
    to 5."""
    db = _conn()
    if stars <= 0:
        await db.execute("DELETE FROM track_ratings WHERE track_id = ?", (track_id,))
    else:
        await db.execute(
            "INSERT INTO track_ratings (track_id, stars) VALUES (?, ?) "
            "ON CONFLICT(track_id) DO UPDATE SET stars = excluded.stars",
            (track_id, min(int(stars), 5)),
        )
    await db.commit()


async def get_ratings(track_ids: list[str]) -> dict[str, int]:
    """Batch fetch ratings for list rendering. Returns {track_id: stars} for
    the rated subset; unrated ids are simply absent."""
    if not track_ids:
        return {}
    placeholders = ",".join("?" * len(track_ids))
    async with _conn().execute(
        f"SELECT track_id, stars FROM track_ratings WHERE track_id IN ({placeholders})",
        track_ids,
    ) as cur:
        return {row["track_id"]: row["stars"] async for row in cur}


def normalize_tags(tags: list[str]) -> list[str]:
    """Trim, drop empties, case-insensitive dedupe (first spelling wins), and
    cap per-tag length + per-track count. The single source of truth for tag
    normalization (plan R6/U1)."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in tags or []:
        if not isinstance(raw, str):
            continue
        t = raw.strip()[:TAG_MAX_LEN].strip()
        if not t:
            continue
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
        if len(out) >= TAGS_MAX_PER_TRACK:
            break
    return out


async def get_tags(track_id: str) -> list[str]:
    """A track's tags in stored order, or [] when none."""
    async with _conn().execute(
        "SELECT tags_json FROM track_tags WHERE track_id = ?", (track_id,)
    ) as cur:
        row = await cur.fetchone()
    if not row or not row["tags_json"]:
        return []
    try:
        data = _json.loads(row["tags_json"])
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


async def set_tags(track_id: str, tags: list[str]) -> list[str]:
    """Replace a track's tags with the normalized set. An empty result deletes
    the row. Returns the stored (normalized) list so callers can echo the
    truth rather than the raw request."""
    norm = normalize_tags(tags)
    db = _conn()
    if not norm:
        await db.execute("DELETE FROM track_tags WHERE track_id = ?", (track_id,))
    else:
        await db.execute(
            "INSERT INTO track_tags (track_id, tags_json) VALUES (?, ?) "
            "ON CONFLICT(track_id) DO UPDATE SET tags_json = excluded.tags_json",
            (track_id, _json.dumps(norm)),
        )
    await db.commit()
    return norm


async def get_tags_bulk(track_ids: list[str]) -> dict[str, list[str]]:
    """Batch fetch tags for list rendering. Returns {track_id: [tags]} for the
    tagged subset; untagged ids are absent."""
    if not track_ids:
        return {}
    placeholders = ",".join("?" * len(track_ids))
    async with _conn().execute(
        f"SELECT track_id, tags_json FROM track_tags WHERE track_id IN ({placeholders})",
        track_ids,
    ) as cur:
        out: dict[str, list[str]] = {}
        async for row in cur:
            try:
                data = _json.loads(row["tags_json"]) if row["tags_json"] else []
                out[row["track_id"]] = data if isinstance(data, list) else []
            except (ValueError, TypeError):
                out[row["track_id"]] = []
        return out


async def get_all_ratings() -> dict[str, int]:
    """Every track rating as a {track_id: stars} map (mirrors
    get_all_play_counts). Powers the shared module's global rating map for
    rendering pips + the 'rated' sort."""
    async with _conn().execute("SELECT track_id, stars FROM track_ratings") as cur:
        return {row["track_id"]: row["stars"] async for row in cur}


async def get_all_tags() -> dict[str, list[str]]:
    """Every track's tags as a {track_id: [tags]} map. Untagged tracks absent."""
    async with _conn().execute("SELECT track_id, tags_json FROM track_tags") as cur:
        out: dict[str, list[str]] = {}
        async for row in cur:
            try:
                data = _json.loads(row["tags_json"]) if row["tags_json"] else []
                out[row["track_id"]] = data if isinstance(data, list) else []
            except (ValueError, TypeError):
                out[row["track_id"]] = []
        return out


async def get_top_rated_tracks(limit: int | None = 100) -> list[dict]:
    """Top-rated tracks for the Highest Rated leaderboard (plan U1/R11).

    Ordered rating DESC, then local play count DESC, then track_id (a stable,
    deterministic tie-break mirroring get_top_played_tracks). **Rated tracks
    only** — unrated never appear. Rows: {track_id, stars, play_count,
    metadata} where metadata is the parsed play_track_meta dict or None when no
    capture exists yet (the endpoint live-backfills those, as /api/most-played
    does). ``limit=None`` returns every rated track (SQLite ``LIMIT -1``)."""
    async with _conn().execute(
        "SELECT tr.track_id AS track_id, tr.stars AS stars,"
        " COALESCE(pc.count, 0) AS play_count, ptm.metadata_json"
        " FROM track_ratings tr"
        " LEFT JOIN play_counts pc"
        "   ON pc.entity_type = 'track' AND pc.entity_id = tr.track_id"
        " LEFT JOIN play_track_meta ptm ON ptm.track_id = tr.track_id"
        " ORDER BY tr.stars DESC, play_count DESC, tr.track_id"
        " LIMIT ?",
        (-1 if limit is None else limit,),
    ) as cur:
        rows = []
        async for row in cur:
            meta = None
            if row["metadata_json"]:
                try:
                    meta = _json.loads(row["metadata_json"])
                except (ValueError, TypeError):
                    meta = None
            rows.append({
                "track_id": row["track_id"], "stars": row["stars"],
                "play_count": row["play_count"], "metadata": meta,
            })
        return rows


# ── guest visibility + Browse facet toggles (2026-06-26 ratings-and-tags U4) ──
# Visibility flags default OFF — guests see ratings/tags only when the admin
# opts in (stored "1" = on). Facet flags default ON — every toggleable Browse
# tab shows until the admin hides it (only an explicit "0" hides one).

# The five toggleable Browse facets. The id is the canonical key the shared
# module's tab gating uses; Search/Artists/Albums/Now are NOT toggleable.
BROWSE_FACETS = ("genre", "years", "mostplayed", "recentlyadded", "highestrated")


async def get_ratings_visible_to_guests() -> bool:
    return (await get_setting("ratings_visible_to_guests")) == "1"


async def get_tags_visible_to_guests() -> bool:
    return (await get_setting("tags_visible_to_guests")) == "1"


async def get_browse_facets() -> dict[str, bool]:
    """Per-facet guest visibility, all default True. {facet_id: visible}."""
    return {
        f: (await get_setting(f"facet_{f}")) != "0"
        for f in BROWSE_FACETS
    }


# ── genre cache ───────────────────────────────────────────────────────────────

async def get_genre_cache() -> list[dict]:
    """Return cached genre list ordered by count descending. Empty list if unpopulated."""
    async with _conn().execute("SELECT name, count FROM genre_cache ORDER BY count DESC") as cur:
        return [{"name": row["name"], "count": row["count"]} async for row in cur]


async def set_genre_cache(genres: list[dict]) -> None:
    """Atomically replace the genre cache with a new merged list."""
    db = _conn()
    async with _write_tx_lock:
        await db.execute("BEGIN IMMEDIATE")
        try:
            await db.execute("DELETE FROM genre_cache")
            if genres:
                await db.executemany(
                    "INSERT INTO genre_cache (name, count) VALUES (?, ?)",
                    [(g["name"], g["count"]) for g in genres],
                )
            await db.commit()
        except Exception:
            await db.rollback()
            raise


# ── pattern rules + artist exclusions (2026-06-10 pattern-rules plan U1) ─────

import json as _json


async def get_pattern_rules() -> list[list[str]]:
    """Admin-defined equivalence rules (lists of strings), as saved —
    including inert ones.

    Never-saved installs get a copy of DEFAULT_PATTERN_RULES (2026-06-10
    follow-up): the defaults are editable data, pre-populating the Setup
    editor. A SAVED empty list stays empty — deleting every rule is a
    choice, not a reset to defaults."""
    raw = await get_setting("pattern_rules")
    if raw is None:
        from app.normalize import DEFAULT_PATTERN_RULES
        return [list(r) for r in DEFAULT_PATTERN_RULES]
    try:
        data = _json.loads(raw)
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


async def set_pattern_rules(rules: list[list[str]]) -> None:
    await set_setting("pattern_rules", _json.dumps(rules))


async def get_artist_exclusions() -> list[str]:
    """Raw artist names excluded from the Browse roster. Empty when unset."""
    raw = await get_setting("artist_exclusions")
    if not raw:
        return []
    try:
        data = _json.loads(raw)
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


async def set_artist_exclusions(names: list[str]) -> None:
    await set_setting("artist_exclusions", _json.dumps(names))


async def get_source_priority() -> list[str]:
    """Admin-defined global source priority order — source_ids, highest priority
    first (multi-source plan U9/R12). Empty when unset, in which case scan/
    registry order applies. Used to order a track's holds snapshot at enqueue."""
    raw = await get_setting("source_priority")
    if not raw:
        return []
    try:
        data = _json.loads(raw)
        return [str(x) for x in data] if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


async def set_source_priority(source_ids: list[str]) -> None:
    await set_setting("source_priority", _json.dumps([str(s) for s in source_ids]))


async def get_disabled_sources() -> list[str]:
    """Admin-vetoed sources — source_ids whose whole-source switch is OFF
    (Libraries-panel redesign U1). A source_id present here means every one of its
    libraries is excluded from guest-visible content, regardless of the per-library
    enabled_libraries rows (which are left intact so the selection is remembered
    across an off->on toggle). Empty (the default) = all sources on."""
    raw = await get_setting("disabled_sources")
    if not raw:
        return []
    try:
        data = _json.loads(raw)
        return [str(x) for x in data] if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


async def set_disabled_sources(source_ids: list[str]) -> None:
    await set_setting("disabled_sources", _json.dumps([str(s) for s in source_ids]))


async def get_random_length_bounds() -> tuple[int | None, int | None]:
    """Min/max length band (in milliseconds) for random track selection —
    Surprise Me + Shuffle (2026-06-20 plan U1).

    Each bound is stored as integer seconds under ``random_min_seconds`` /
    ``random_max_seconds``. A missing, unparseable, or ``<= 0`` value means "no
    bound" and returns ``None`` for that end. Both ``None`` (the default)
    reproduces the pre-feature behavior, so the filter is fully opt-in.
    """
    def _ms(raw: str | None) -> int | None:
        try:
            secs = int(raw) if raw is not None else 0
        except (ValueError, TypeError):
            return None
        return secs * 1000 if secs > 0 else None

    return (
        _ms(await get_setting("random_min_seconds")),
        _ms(await get_setting("random_max_seconds")),
    )


async def get_popular_random_threshold() -> int:
    """Minimum local play count for a track to count as "popular" in the
    Popular Random queue-end mode (2026-06-21 plan U2).

    Stored as an integer string under ``popular_random_threshold``; default 2,
    inclusive (a track with exactly 2 local plays qualifies). A missing or
    unparseable value falls back to 2; a value below 1 clamps to 1."""
    raw = await get_setting("popular_random_threshold")
    try:
        n = int(raw) if raw is not None else 2
    except (ValueError, TypeError):
        return 2
    return n if n >= 1 else 1


async def get_most_played_display_limit() -> int:
    """How many tracks the Most Played leaderboard shows (admin Setup).

    Stored as an integer string under ``most_played_display_limit``; default
    100. A missing or unparseable value falls back to 100; a value below 1
    clamps to 1. Display-only — it does NOT bound the Popular Random pool."""
    raw = await get_setting("most_played_display_limit")
    try:
        n = int(raw) if raw is not None else 100
    except (ValueError, TypeError):
        return 100
    return n if n >= 1 else 1


async def get_resume_window_minutes() -> int:
    """Auto-resume window for output-outage recovery (2026-07-11 supervisor
    plan U3, R8): how long after an outage entered the supervisor may still
    auto-resume playback on re-attach. Per-outage-entry; checked at the
    moment audio would start, never via an expiry timer.

    Stored as an integer string under ``resume_window_minutes``; default 60.
    A missing or unparseable value falls back to 60; a value below 1 clamps
    to 1 (the admin Settings surface — plan U5 — rejects 0/negative at the
    endpoint). Read at decision time so an admin edit applies immediately."""
    raw = await get_setting("resume_window_minutes")
    try:
        n = int(raw) if raw is not None else 60
    except (ValueError, TypeError):
        return 60
    return n if n >= 1 else 1


async def get_gapless_enabled() -> bool:
    """Gapless playback master toggle (2026-07-11 supervisor plan U5, R10).

    Stored as ``"1"``/``"0"`` under ``gapless_enabled``; default OFF — only an
    explicit ``"1"`` enables. Read once at startup into app.state's live module
    flag (``state.gapless_enabled()``) and on the settings GET for hydration;
    playback paths consult the live flag, never this accessor."""
    return (await get_setting("gapless_enabled")) == "1"


# Valid stored values for the per-device gapless behavioral verdict (2026-07-11
# supervisor plan U8). Absence of a row IS the third state ("unverified") —
# only DECIDED verdicts are stored, so clearing a row re-opens behavioral
# verification for that device.
_GAPLESS_VERDICTS = ("supported", "unsupported")


async def get_gapless_verdict(backend: str, device_id: str) -> str | None:
    """Per-device gapless behavioral verdict (2026-07-11 supervisor plan U8):
    lazily established by the backend at the first armed boundary (DLNA
    SetNext is verify-then-trust — advertised capability ≠ conformance) and
    cached under ``gapless_verdict:{backend}:{device_id}``. Returns
    ``"supported"`` / ``"unsupported"``, or ``None`` when the device is
    unverified; an unrecognized stored value also reads as ``None`` so a
    corrupted row degrades to re-verification, never to a wrong capability."""
    raw = await get_setting(f"gapless_verdict:{backend}:{device_id}")
    return raw if raw in _GAPLESS_VERDICTS else None


async def set_gapless_verdict(backend: str, device_id: str,
                              verdict: str) -> None:
    """Persist a DECIDED per-device gapless verdict (see
    :func:`get_gapless_verdict`). "unverified" is represented by the ABSENCE
    of the row and is not storable — re-verification is opened by deleting
    the setting, never by writing a third value."""
    if verdict not in _GAPLESS_VERDICTS:
        raise ValueError(f"invalid gapless verdict: {verdict!r}")
    await set_setting(f"gapless_verdict:{backend}:{device_id}", verdict)


async def get_gapless_verdicts(backend: str) -> dict[str, str]:
    """Every cached gapless verdict for *backend*, keyed by device_id — the
    devices-snapshot builder's bulk load (one roundtrip for the whole picker,
    the same posture as the probe cache's ``fetch_all``). Unrecognized values
    are dropped (those devices read unverified)."""
    rows = await get_settings_with_prefix(f"gapless_verdict:{backend}:")
    return {k: v for k, v in rows.items() if v in _GAPLESS_VERDICTS}


# Closing Time mode (2026-06-24 plan): defaults live here so the getter, the
# settings echo, and the EOS detector all resolve the same Semisonic defaults.
CLOSING_TIME_DEFAULT_TITLE = "Closing Time"
CLOSING_TIME_DEFAULT_ARTIST = "Semisonic"
CLOSING_TIME_DEFAULT_MESSAGE = "You don't have to go home, but you can't stay here."


async def get_closing_time_config() -> tuple[bool, str, str, str]:
    """Closing Time mode config (admin Setup, 2026-06-24 plan U1).

    When enabled, the queue freezes after the trigger song (matched on title +
    artist) plays to its natural end, and the message is broadcast to every
    screen until the admin resumes. ``closing_time_enabled`` is stored as
    ``"1"``/``"0"`` (default off); title/artist/message are free strings with
    the Semisonic defaults. Read at decision time so edits take effect
    immediately. Returns ``(enabled, title, artist, message)``. Defaults are
    applied Python-side (single-arg ``get_setting``) to match the other getters;
    a stored empty string is kept as-is (the admin deliberately cleared it)."""
    enabled = (await get_setting("closing_time_enabled")) == "1"
    title = await get_setting("closing_time_title")
    artist = await get_setting("closing_time_artist")
    message = await get_setting("closing_time_message")
    return (
        enabled,
        title if title is not None else CLOSING_TIME_DEFAULT_TITLE,
        artist if artist is not None else CLOSING_TIME_DEFAULT_ARTIST,
        message if message is not None else CLOSING_TIME_DEFAULT_MESSAGE,
    )


async def get_queue_end_length_limit() -> bool:
    """Whether the random length band constrains the queue-end random modes
    (Popular Random + Full Random) — 2026-06-21 plan U2.

    Stored as ``"1"`` / ``"0"`` under ``queue_end_length_limit``; default False
    (off), so queue-end auto-fill is unconstrained by length until the admin
    opts in. Distinct from Surprise Me, whose band applies whenever set."""
    return (await get_setting("queue_end_length_limit")) == "1"


async def get_rail_alpha_mode() -> str:
    """Alphabet-rail construction mode (2026-06-22 plan 004, international rail).

    ``"english"`` (default) keeps the shipped fixed A–Z + per-script trailing
    rail; ``"international"`` rebuilds the rail from the library's actual first
    characters with per-rail thresholds. Stored under ``rail_alpha_mode``;
    unknown/unset → ``"english"`` (the shipped behavior)."""
    raw = await get_setting("rail_alpha_mode")
    return raw if raw in ("english", "international") else "english"


async def _rail_threshold(key: str) -> int:
    """Shared coercion for the International rail thresholds: integer string,
    default 2, unparseable → 2, below 1 clamps to 1 (mirrors the popular-random
    threshold getter)."""
    raw = await get_setting(key)
    try:
        n = int(raw) if raw is not None else 2
    except (ValueError, TypeError):
        return 2
    return n if n >= 1 else 1


async def get_rail_artist_threshold() -> int:
    """Minimum artists sharing a first character for that character to earn its
    own bucket in the International artists rail (2026-06-22 plan 004). Default 2."""
    return await _rail_threshold("rail_artist_threshold")


async def get_rail_album_threshold() -> int:
    """Album-rail counterpart of :func:`get_rail_artist_threshold` (plan 004)."""
    return await _rail_threshold("rail_album_threshold")


# ── credit cache (per-track credited acts; 2026-06-10 plan U2) ───────────────

async def get_credit_acts(va_only: bool = False) -> list[dict]:
    """Distinct credited acts with their appears-on release counts.

    Ordered by name for stable browse merges. Empty list if unpopulated.

    va_only (2026-06-10 browse-VA-gate refinement, R1): restrict to acts
    with at least one appearance on a Various Artists release. The Browse
    roster uses this so collaboration variations credited only on an
    artist's own releases ("!!! & Angelica Garcia" on a "!!!" album) don't
    clutter the artist list; Search keeps the unfiltered set so guest
    appearances stay findable.
    """
    having = (
        " HAVING SUM(LOWER(album_artist) = 'various artists') > 0" if va_only else ""
    )
    async with _conn().execute(
        "SELECT name, name_lower, COUNT(*) AS release_count"
        " FROM credit_cache GROUP BY name_lower" + having +
        " ORDER BY name COLLATE NOCASE"
    ) as cur:
        return [
            {"name": row["name"], "name_lower": row["name_lower"],
             "release_count": row["release_count"]}
            async for row in cur
        ]


async def get_credit_appearances(name_lower: str) -> list[dict]:
    """Releases a credited act appears on (parameterized lookup)."""
    async with _conn().execute(
        "SELECT album_id, album_title, album_artist, album_thumb, album_year, server_name"
        " FROM credit_cache WHERE name_lower = ? ORDER BY album_year, album_title",
        (name_lower,),
    ) as cur:
        return [dict(row) async for row in cur]


async def set_credit_cache(rows: list[dict]) -> None:
    """Atomically replace the credit cache (genre_cache replace pattern)."""
    db = _conn()
    async with _write_tx_lock:
        await db.execute("BEGIN IMMEDIATE")
        try:
            await db.execute("DELETE FROM credit_cache")
            if rows:
                await db.executemany(
                    "INSERT OR IGNORE INTO credit_cache"
                    " (name, name_lower, album_id, album_title, album_artist,"
                    "  album_thumb, album_year, server_name)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (r["name"], r["name_lower"], r["album_id"], r["album_title"],
                         r["album_artist"], r.get("album_thumb"), r.get("album_year"),
                         r.get("server_name"))
                        for r in rows
                    ],
                )
            await db.commit()
        except Exception:
            await db.rollback()
            raise


# ── browse index (cross-server artist+album roster; 2026-06-21 plan U1) ──────

_BROWSE_ARTIST_COLS = (
    "artist_id, title, base_key, thumb, release_count, server_name, section_key"
)
_BROWSE_ALBUM_COLS = (
    "album_id, title, title_base, artist, artist_base_key, year, thumb,"
    " subtype, added_at, track_count, server_name, section_key"
)


async def get_browse_artists() -> list[dict]:
    """Full artist roster from the browse index. Empty list if unpopulated.

    Rows carry the raw title (for the request-time dedup/credit/exclusion
    pipeline) plus the rule-independent base_key and per-row server_name."""
    async with _conn().execute(
        f"SELECT {_BROWSE_ARTIST_COLS} FROM browse_artist_index"
    ) as cur:
        return [dict(row) async for row in cur]


async def get_browse_artist_by_id(artist_id: str) -> dict | None:
    """One artist row by compound id, or None. O(1) drill-in entry point."""
    async with _conn().execute(
        f"SELECT {_BROWSE_ARTIST_COLS} FROM browse_artist_index WHERE artist_id = ?",
        (artist_id,),
    ) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_browse_albums() -> list[dict]:
    """Full album roster from the browse index. Empty list if unpopulated."""
    async with _conn().execute(
        f"SELECT {_BROWSE_ALBUM_COLS} FROM browse_album_index"
    ) as cur:
        return [dict(row) async for row in cur]


async def get_browse_albums_for_artist(artist_base_key: str) -> list[dict]:
    """Every album (across all servers) whose release artist base-normalizes to
    artist_base_key. Indexed — proportional to the artist's releases, not the
    library. The cross-server grouping/rule layer runs on top at request time."""
    async with _conn().execute(
        f"SELECT {_BROWSE_ALBUM_COLS} FROM browse_album_index"
        " WHERE artist_base_key = ?",
        (artist_base_key,),
    ) as cur:
        return [dict(row) async for row in cur]


async def get_browse_album_by_id(album_id: str) -> dict | None:
    """One album row by compound id, or None. Drill-in entry point for the
    album→tracks identity resolution."""
    async with _conn().execute(
        f"SELECT {_BROWSE_ALBUM_COLS} FROM browse_album_index WHERE album_id = ?",
        (album_id,),
    ) as cur:
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_browse_albums_by_identity(
    title_base: str, artist_base_key: str
) -> list[dict]:
    """Every per-server copy of one release identity (same base title + base
    artist). Used to resolve a release to its rating-keys across servers so
    album→tracks fetches /children by exact id without re-scanning libraries."""
    async with _conn().execute(
        f"SELECT {_BROWSE_ALBUM_COLS} FROM browse_album_index"
        " WHERE title_base = ? AND artist_base_key = ?",
        (title_base, artist_base_key),
    ) as cur:
        return [dict(row) async for row in cur]


async def set_browse_index(artists: list[dict], albums: list[dict]) -> None:
    """Atomically replace BOTH browse-index tables in one transaction
    (credit_cache replace pattern). INSERT OR IGNORE guards against a duplicate
    compound id within a single crawl batch."""
    db = _conn()
    async with _write_tx_lock:
        await db.execute("BEGIN IMMEDIATE")
        try:
            await db.execute("DELETE FROM browse_artist_index")
            await db.execute("DELETE FROM browse_album_index")
            if artists:
                await db.executemany(
                    "INSERT OR IGNORE INTO browse_artist_index"
                    f" ({_BROWSE_ARTIST_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        (a["artist_id"], a["title"], a["base_key"], a.get("thumb"),
                         a.get("release_count"), a.get("server_name"), a.get("section_key"))
                        for a in artists
                    ],
                )
            if albums:
                await db.executemany(
                    "INSERT OR IGNORE INTO browse_album_index"
                    f" ({_BROWSE_ALBUM_COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (a["album_id"], a["title"], a["title_base"], a["artist"],
                         a["artist_base_key"], a.get("year"), a.get("thumb"),
                         a.get("subtype"), a.get("added_at"), a.get("track_count"),
                         a.get("server_name"), a.get("section_key"))
                        for a in albums
                    ],
                )
            await db.commit()
        except Exception:
            await db.rollback()
            raise
