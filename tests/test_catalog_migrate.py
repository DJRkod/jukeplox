"""U7: offline metadata migration onto catalog identity.

Uses synthetic aliases to force ``identity != compound id`` (the cross-source
case) and verifies ratings/tags/play-counts/Most-Played rows are copied onto the
identity, originals retained (rollback), idempotent on re-run, dormant rows
(disconnected source, no identity) left untouched, and inert on a Plex-only
install (identity == compound id).
"""

import pytest

from app import database
from app.catalog import migrate, store
from app.config import Settings


@pytest.fixture
def tmp_settings(tmp_path, monkeypatch):
    s = Settings(data_dir=tmp_path, secret_key="test")
    monkeypatch.setattr(database, "settings", s)
    return s


@pytest.fixture
async def db(tmp_settings):
    await database.init_db()
    yield tmp_settings
    await database.close_db()


async def _alias(compound, ident):
    """Make ``compound`` resolve to a different catalog identity (cross-source)."""
    await store.register_alias("track", compound, ident)
    await store.register_alias("track", ident, ident)  # identity self-alias (as scan does)


# ── ratings ──────────────────────────────────────────────────────────────────

async def test_rating_copied_onto_identity_original_retained(db):
    await _alias("plex:1", "ident-A")
    await database.set_rating("plex:1", 4)
    counts = await migrate.migrate_metadata()
    assert counts["ratings"] == 1
    assert await database.get_rating("ident-A") == 4      # copied onto identity
    assert await database.get_rating("plex:1") == 4       # original retained (rollback)


async def test_rating_migration_idempotent(db):
    await _alias("plex:1", "ident-A")
    await database.set_rating("plex:1", 4)
    await migrate.migrate_metadata()
    second = await migrate.migrate_metadata()
    assert second["ratings"] == 0                          # nothing new on re-run
    assert await database.get_rating("ident-A") == 4


async def test_rating_backfill_does_not_clobber_existing_identity_rating(db):
    await _alias("plex:1", "ident-A")
    await database.set_rating("plex:1", 4)
    await database.set_rating("ident-A", 2)                # a direct identity rating exists
    await migrate.migrate_metadata()
    assert await database.get_rating("ident-A") == 2       # preserved, not overwritten


# ── tags ─────────────────────────────────────────────────────────────────────

async def test_tags_copied_onto_identity(db):
    await _alias("plex:1", "ident-A")
    await database.set_tags("plex:1", ["banger", "live"])
    counts = await migrate.migrate_metadata()
    assert counts["tags"] == 1
    assert await database.get_tags("ident-A") == ["banger", "live"]


# ── play counts + Most-Played meta ───────────────────────────────────────────

async def test_play_count_copied_onto_identity(db):
    await _alias("plex:1", "ident-A")
    await database.increment_play_count("track", "plex:1")
    await database.increment_play_count("track", "plex:1")
    counts = await migrate.migrate_metadata()
    assert counts["play_counts"] == 1
    assert await database.get_play_count("track", "ident-A") == 2


async def test_play_track_meta_copied_onto_identity(db):
    await _alias("plex:1", "ident-A")
    await database.set_play_track_meta("plex:1", {"title": "Song", "artist": "Act"})
    counts = await migrate.migrate_metadata()
    assert counts["play_track_meta"] == 1
    meta = await database.get_all_play_track_meta()
    assert meta["ident-A"]["title"] == "Song"


# ── dormant + inert ──────────────────────────────────────────────────────────

async def test_dormant_row_without_identity_is_left_untouched(db):
    # No alias for this compound id (its source is disconnected) → no identity.
    await database.set_rating("disconnected:9", 5)
    counts = await migrate.migrate_metadata()
    assert counts["ratings"] == 0
    assert await database.get_rating("disconnected:9") == 5  # kept as a dormant alias


async def test_plex_only_identity_equals_compound_is_inert(db):
    # Self-alias only (identity == compound id), as a Plex-only scan produces.
    await store.register_alias("track", "m1:5", "m1:5")
    await database.set_rating("m1:5", 3)
    await database.increment_play_count("track", "m1:5")
    counts = await migrate.migrate_metadata()
    assert counts == {"ratings": 0, "tags": 0, "play_counts": 0,
                      "play_track_meta": 0, "reconciled": 0}
    # exactly one rating row, still keyed by the compound id
    assert await database.get_all_ratings() == {"m1:5": 3}


# ── reconcile: fold retained stale duplicate play_counts into the live identity ─

async def _live_track(identity_val, title="Wish", artist="Nine Inch Nails"):
    """Make ``identity_val`` a CURRENT catalog track (single-track catalog).

    ``replace_catalog`` atomic-swaps the content tables, so this defines the
    whole live catalog — the reconcile's liveness check reads ``catalog_track``.
    """
    await store.replace_catalog(
        artists=[],
        albums=[],
        tracks=[{
            "identity": identity_val, "title": title, "title_base": title.lower(),
            "artist": artist, "artist_base_key": artist.lower().replace(" ", ""),
        }],
        holds=[],
    )


async def test_reconcile_folds_stale_duplicate_play_counts_into_live_identity(db):
    # A track re-minted twice: the source key S and the older identity I2 both
    # alias forward to the current live identity I3 (as resolve_clusters' repoint
    # leaves every current lookup key pointing at the newest identity).
    await store.register_alias("track", "I3", "I3")   # current identity self-alias
    await store.repoint_alias("track", "S", "I3")     # source compound id → current
    await store.repoint_alias("track", "I2", "I3")    # older identity → current
    await _live_track("I3")                            # only I3 is a live catalog track
    # The symptom: retained duplicate rows, all carrying the same COPIED count.
    for k in ("S", "I2", "I3"):
        await database.set_play_count("track", k, 13)
        await database.set_play_track_meta(k, {"title": "Wish", "artist": "Nine Inch Nails"})

    counts = await migrate.migrate_metadata()

    assert counts["reconciled"] == 2                  # S and I2 folded away
    # Exactly ONE leaderboard row remains — the live identity, count preserved.
    wish = [r for r in await database.get_top_played_tracks(None)
            if r["track_id"] in ("S", "I2", "I3")]
    assert [r["track_id"] for r in wish] == ["I3"]
    assert wish[0]["count"] == 13
    assert await database.get_play_count("track", "S") == 0     # stale rows deleted
    assert await database.get_play_count("track", "I2") == 0
    # Orphaned display meta for the folded keys is cleaned up.
    meta = await database.get_all_play_track_meta()
    assert "S" not in meta and "I2" not in meta and meta["I3"]["title"] == "Wish"


async def test_reconcile_populates_missing_identity_count_from_stale_row(db):
    # Identity row absent (0); the stale row holds the count. Fold should carry it.
    await store.register_alias("track", "I3", "I3")
    await store.repoint_alias("track", "S", "I3")
    await _live_track("I3")
    await database.set_play_count("track", "S", 9)     # no I3 play_counts row yet
    counts = await migrate.migrate_metadata()
    assert counts["reconciled"] == 1
    assert await database.get_play_count("track", "I3") == 9
    assert await database.get_play_count("track", "S") == 0


async def test_reconcile_preserves_dormant_disconnected_play_count(db):
    # A track whose source is gone: its identity is not in the live catalog.
    await store.register_alias("track", "gone:1", "gone:1")   # self-alias, dormant
    await database.set_play_count("track", "gone:1", 7)
    await _live_track("I3")                                   # some OTHER track is live
    counts = await migrate.migrate_metadata()
    assert counts["reconciled"] == 0
    assert await database.get_play_count("track", "gone:1") == 7   # preserved


async def test_reconcile_does_not_fold_into_absent_identity(db):
    # 'old:1' aliases forward to 'ident-X', but ident-X is NOT a live catalog
    # track — never fold a still-needed row into a vanished target.
    await store.repoint_alias("track", "old:1", "ident-X")
    await database.set_play_count("track", "old:1", 4)
    await _live_track("I3")                                   # ident-X absent from catalog
    counts = await migrate.migrate_metadata()
    assert counts["reconciled"] == 0
    assert await database.get_play_count("track", "old:1") == 4    # preserved
