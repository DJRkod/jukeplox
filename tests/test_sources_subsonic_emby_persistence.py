"""U3: persistence + credential sealing for the Subsonic / Emby source tables.

Mirrors the jellyfin_sources coverage: the stored credential lives in a `token`
column sealed at rest (Fernet, `enc:fernet:` prefix), opens back to plaintext on
read, deletes by source_id, and is re-sealed once by the idempotent migration.
The stored server_url stays credential-free. Legacy plaintext and a lost-key
decrypt failure both degrade without raising (inherited from app.sources.secrets).
"""

import aiosqlite
import pytest

from app import database
from app.config import Settings


@pytest.fixture
def tmp_settings(tmp_path, monkeypatch):
    s = Settings(data_dir=tmp_path, secret_key="test")
    monkeypatch.setattr(database, "settings", s)
    return s


@pytest.fixture
async def db(tmp_settings):
    # close_db() is mandatory, not tidiness: aiosqlite runs its connection on a
    # NON-daemon thread, so leaving it open blocks threading._shutdown and hangs
    # the pytest process AFTER the tests have passed. pytest-timeout cannot
    # catch that — it is an interpreter-exit hang, not an in-test one.
    await database.init_db()
    try:
        yield tmp_settings
    finally:
        await database.close_db()


async def _raw_token(db_path, table, source_id):
    """Read the token column straight from the DB, bypassing open_secret."""
    async with aiosqlite.connect(db_path) as conn:
        async with conn.execute(
            f"SELECT token FROM {table} WHERE source_id = ?", (source_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def _raw_row(db_path, table, source_id):
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            f"SELECT * FROM {table} WHERE source_id = ?", (source_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


# ── tables exist ──────────────────────────────────────────────────────────────

async def test_init_db_creates_new_source_tables(db):
    async with aiosqlite.connect(db.db_path) as conn:
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ) as cur:
            names = {row[0] async for row in cur}
    assert {"subsonic_sources", "emby_sources"} <= names


# ── subsonic round-trip / seal at rest ────────────────────────────────────────

async def test_subsonic_save_get_round_trip_sealed_at_rest(db):
    from app.sources import secrets
    await database.save_subsonic_source(
        source_id="sub1", server_url="http://navidrome.lan:4533", name="Navidrome",
        token="subsonic-api-key-xyz", user="dj", client="jukeplox",
        auth_mode="apikey",
    )
    # Raw stored value is sealed (not plaintext).
    raw = await _raw_token(db.db_path, "subsonic_sources", "sub1")
    assert raw.startswith(secrets.SEALED_PREFIX)
    assert "subsonic-api-key-xyz" not in raw
    # Opened on read.
    rows = await database.get_subsonic_sources()
    assert len(rows) == 1
    r = rows[0]
    assert r["source_id"] == "sub1"
    assert r["server_url"] == "http://navidrome.lan:4533"
    assert r["name"] == "Navidrome"
    assert r["token"] == "subsonic-api-key-xyz"
    assert r["user"] == "dj"
    assert r["client"] == "jukeplox"
    assert r["auth_mode"] == "apikey"


async def test_subsonic_token_mode_round_trip_seals_password(db):
    # token+salt fallback (2026-08-11-003 U2): the stored secret is the account
    # PASSWORD, sealed at rest, and auth_mode round-trips as 'token'.
    from app.sources import secrets
    await database.save_subsonic_source(
        source_id="subT", server_url="http://gonic.lan:4747", name="gonic",
        token="my-account-password", user="dj", client="jukeplox",
        auth_mode="token",
    )
    raw = await _raw_token(db.db_path, "subsonic_sources", "subT")
    assert raw.startswith(secrets.SEALED_PREFIX)
    assert "my-account-password" not in raw  # never plaintext at rest (AE3)
    rows = {r["source_id"]: r for r in await database.get_subsonic_sources()}
    assert rows["subT"]["auth_mode"] == "token"
    assert rows["subT"]["token"] == "my-account-password"


async def test_subsonic_preexisting_row_defaults_to_apikey(db):
    # R6/AE6: a row written without auth_mode (pre-fallback shape) reads back as
    # 'apikey' via the column DEFAULT — existing apiKey sources are unchanged.
    await database._conn().execute(
        "INSERT INTO subsonic_sources (source_id, server_url, name, token, user, client) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("subOld", "http://old.lan", "Old", "some-key", "u", "c"),
    )
    await database._conn().commit()
    rows = {r["source_id"]: r for r in await database.get_subsonic_sources()}
    assert rows["subOld"]["auth_mode"] == "apikey"


async def test_migrate_columns_adds_auth_mode_to_legacy_table(db):
    # Exercises the ALTER TABLE path (not just the CREATE-TABLE DEFAULT): a
    # pre-fallback subsonic_sources table without auth_mode gains the column and
    # existing rows read back as 'apikey' after _migrate_columns (R6/AE6).
    conn = database._conn()
    await conn.execute("DROP TABLE subsonic_sources")
    await conn.execute(
        "CREATE TABLE subsonic_sources (source_id TEXT PRIMARY KEY, "
        "server_url TEXT NOT NULL, name TEXT NOT NULL, token TEXT NOT NULL, "
        "user TEXT NOT NULL, client TEXT NOT NULL)")
    await conn.execute(
        "INSERT INTO subsonic_sources (source_id, server_url, name, token, user, client) "
        "VALUES ('legacy', 'http://a.lan', 'A', 'k', 'u', 'c')")
    await conn.commit()
    async with conn.execute("PRAGMA table_info(subsonic_sources)") as cur:
        assert "auth_mode" not in {r["name"] async for r in cur}

    await database._migrate_columns()
    await conn.commit()

    async with conn.execute("PRAGMA table_info(subsonic_sources)") as cur:
        assert "auth_mode" in {r["name"] async for r in cur}
    rows = {r["source_id"]: r for r in await database.get_subsonic_sources()}
    assert rows["legacy"]["auth_mode"] == "apikey"
    # Idempotent: a second run does not error or duplicate the column.
    await database._migrate_columns()
    await conn.commit()


async def test_subsonic_upsert_updates_in_place(db):
    await database.save_subsonic_source(
        source_id="sub1", server_url="http://a.lan", name="A",
        token="k1", user="u1", client="c1", auth_mode="apikey",
    )
    # A reconnect can flip mode: apikey → token, secret+mode overwrite together.
    await database.save_subsonic_source(
        source_id="sub1", server_url="http://b.lan", name="B",
        token="k2", user="u2", client="c2", auth_mode="token",
    )
    rows = await database.get_subsonic_sources()
    assert len(rows) == 1
    assert rows[0]["server_url"] == "http://b.lan"
    assert rows[0]["name"] == "B"
    assert rows[0]["token"] == "k2"
    assert rows[0]["user"] == "u2"
    assert rows[0]["auth_mode"] == "token"


async def test_subsonic_delete_removes_row(db):
    await database.save_subsonic_source(
        source_id="sub1", server_url="http://a.lan", name="A",
        token="k1", user="u1", client="c1", auth_mode="apikey",
    )
    await database.delete_subsonic_source("sub1")
    assert await database.get_subsonic_sources() == []


async def test_subsonic_server_url_never_contains_credential(db):
    await database.save_subsonic_source(
        source_id="sub1", server_url="http://navidrome.lan:4533", name="Navidrome",
        token="subsonic-api-key-xyz", user="dj", client="jukeplox",
        auth_mode="apikey",
    )
    row = await _raw_row(db.db_path, "subsonic_sources", "sub1")
    assert "subsonic-api-key-xyz" not in row["server_url"]


# ── emby round-trip / seal at rest ────────────────────────────────────────────

async def test_emby_save_get_round_trip_sealed_at_rest(db):
    from app.sources import secrets
    await database.save_emby_source(
        source_id="emby1", server_url="http://emby.lan:8096", name="Emby",
        token="emby-token-abc", user_id="user-guid", device_id="dev-guid",
    )
    raw = await _raw_token(db.db_path, "emby_sources", "emby1")
    assert raw.startswith(secrets.SEALED_PREFIX)
    assert "emby-token-abc" not in raw
    rows = await database.get_emby_sources()
    assert len(rows) == 1
    r = rows[0]
    assert r["source_id"] == "emby1"
    assert r["server_url"] == "http://emby.lan:8096"
    assert r["name"] == "Emby"
    assert r["token"] == "emby-token-abc"
    assert r["user_id"] == "user-guid"
    assert r["device_id"] == "dev-guid"


async def test_emby_upsert_updates_in_place(db):
    await database.save_emby_source(
        source_id="emby1", server_url="http://a.lan:8096", name="A",
        token="t1", user_id="u1", device_id="d1",
    )
    await database.save_emby_source(
        source_id="emby1", server_url="http://b.lan:8096", name="B",
        token="t2", user_id="u2", device_id="d2",
    )
    rows = await database.get_emby_sources()
    assert len(rows) == 1          # upsert in place, not a second row
    assert rows[0]["server_url"] == "http://b.lan:8096"
    assert rows[0]["name"] == "B"
    assert rows[0]["token"] == "t2"   # second token wins
    assert rows[0]["user_id"] == "u2"
    assert rows[0]["device_id"] == "d2"


async def test_emby_delete_removes_row(db):
    await database.save_emby_source(
        source_id="emby1", server_url="http://emby.lan:8096", name="Emby",
        token="emby-token-abc", user_id="uid", device_id="did",
    )
    await database.delete_emby_source("emby1")
    assert await database.get_emby_sources() == []


async def test_emby_server_url_never_contains_credential(db):
    await database.save_emby_source(
        source_id="emby1", server_url="http://emby.lan:8096", name="Emby",
        token="emby-token-abc", user_id="uid", device_id="did",
    )
    row = await _raw_row(db.db_path, "emby_sources", "emby1")
    assert "emby-token-abc" not in row["server_url"]


# ── migration: re-seal existing plaintext rows, idempotent ────────────────────

async def test_migration_seals_preexisting_plaintext_and_is_idempotent(db):
    from app.sources import secrets

    # Simulate a pre-U3 DB: rows written with a PLAINTEXT token (bypass save_*).
    await database._conn().execute(
        "INSERT INTO subsonic_sources (source_id, server_url, name, token, user, client) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("subL", "http://a.lan", "A", "legacy-sub-key", "u", "c"),
    )
    await database._conn().execute(
        "INSERT INTO emby_sources (source_id, server_url, name, token, user_id, device_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("embL", "http://e.lan", "E", "legacy-emby-token", "uid", "did"),
    )
    await database._conn().commit()

    # Plaintext at rest before migration.
    assert not secrets.is_sealed(await _raw_token(db.db_path, "subsonic_sources", "subL"))
    assert not secrets.is_sealed(await _raw_token(db.db_path, "emby_sources", "embL"))

    await database._migrate_seal_credentials()
    await database._conn().commit()

    sub_after = await _raw_token(db.db_path, "subsonic_sources", "subL")
    emb_after = await _raw_token(db.db_path, "emby_sources", "embL")
    assert secrets.is_sealed(sub_after)
    assert secrets.is_sealed(emb_after)
    # Still opens to the original plaintext.
    assert secrets.open_secret(sub_after) == "legacy-sub-key"
    assert secrets.open_secret(emb_after) == "legacy-emby-token"

    # Idempotent: running again must not double-seal (sealed bytes unchanged).
    await database._migrate_seal_credentials()
    await database._conn().commit()
    assert await _raw_token(db.db_path, "subsonic_sources", "subL") == sub_after
    assert await _raw_token(db.db_path, "emby_sources", "embL") == emb_after


# ── graceful degradation: legacy plaintext + lost key ─────────────────────────

async def test_get_degrades_on_legacy_plaintext_and_lost_key(db):
    from cryptography.fernet import Fernet
    from app.sources import secrets

    # Legacy plaintext row (never migrated) — get_* returns it as-is, no raise.
    await database._conn().execute(
        "INSERT INTO subsonic_sources (source_id, server_url, name, token, user, client) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("plain", "http://a.lan", "A", "raw-plaintext-key", "u", "c"),
    )
    await database._conn().commit()
    rows = {r["source_id"]: r for r in await database.get_subsonic_sources()}
    assert rows["plain"]["token"] == "raw-plaintext-key"

    # Sealed row whose key is then lost/rotated — get_* degrades to "", no raise.
    await database.save_subsonic_source(
        source_id="sealed", server_url="http://b.lan", name="B",
        token="will-be-lost", user="u", client="c", auth_mode="apikey",
    )
    secrets._key_path().write_bytes(Fernet.generate_key())  # rotate → decrypt fails
    rows = {r["source_id"]: r for r in await database.get_subsonic_sources()}
    assert rows["sealed"]["token"] == ""
