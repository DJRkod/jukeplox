"""U7: allocate-once / match-forward identity.

resolve_cluster mints an identity once and reuses it across rescans via the
durable alias table; a late external id attaches as an alias rather than
re-minting (so attached ratings never orphan); identity_for_track_id bridges a
source compound id to its identity.
"""

import pytest

from app import database
from app.catalog import identity, store
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


def _item(local_key, mbid=None):
    return {"match_ids": ({"mbid": mbid} if mbid else {}), "local_key": local_key}


# ── allocate-once + mint ──────────────────────────────────────────────────────

async def test_mint_uses_smallest_external_id(db):
    ident = await identity.resolve_cluster("track", [_item("plex:1", mbid="m-x"),
                                                     _item("jelly:2", mbid="m-a")])
    assert ident == "mbid:m-a"
    # every lookup key now resolves to it
    assert await store.find_identity("track", "plex:1") == "mbid:m-a"
    assert await store.find_identity("track", "jelly:2") == "mbid:m-a"


async def test_mint_no_external_uses_compound_id(db):
    # Plex-only: identity is the bare compound id == the existing rating key.
    ident = await identity.resolve_cluster("track", [_item("m1:42")])
    assert ident == "m1:42"
    assert await identity.identity_for_track_id("m1:42") == "m1:42"


async def test_reuse_identity_across_rescans(db):
    first = await identity.resolve_cluster("track", [_item("m1:42")])
    # A later scan of the same track reuses the same identity (idempotent).
    again = await identity.resolve_cluster("track", [_item("m1:42")])
    assert first == again == "m1:42"


# ── match-forward: late external id attaches, never re-mints ──────────────────

async def test_late_external_id_attaches_as_alias_no_remint(db):
    # First seen with no external id → identity is the compound id.
    ident = await identity.resolve_cluster("track", [_item("m1:42")])
    assert ident == "m1:42"
    # Re-scanned later WITH an external id → identity stays the same; the new id
    # becomes an alias (so a rating keyed to m1:42 is never orphaned).
    ident2 = await identity.resolve_cluster("track", [_item("m1:42", mbid="m-new")])
    assert ident2 == "m1:42"
    assert await store.find_identity("track", "mbid:m-new") == "m1:42"


async def test_cross_source_merge_points_other_source_at_existing_identity(db):
    # Plex track minted first (no id). A Jellyfin copy that shares its strict-name
    # cluster (carrying the same compound? no — different source) merges via the
    # alias once they're one cluster: resolving the combined cluster reuses the
    # Plex identity and registers the Jellyfin local id as an alias.
    await identity.resolve_cluster("track", [_item("m1:42")])
    merged = await identity.resolve_cluster("track", [_item("m1:42"), _item("jelly:7")])
    assert merged == "m1:42"
    assert await identity.identity_for_track_id("jelly:7") == "m1:42"


async def test_never_repoints_an_existing_binding(db):
    await identity.resolve_cluster("track", [_item("m1:1")])
    await identity.resolve_cluster("track", [_item("m1:2")])
    # m1:1 and m1:2 were minted as separate identities; a cluster naming both
    # reuses the FIRST match found and never re-points the other's binding.
    merged = await identity.resolve_cluster("track", [_item("m1:1"), _item("m1:2")])
    assert merged in ("m1:1", "m1:2")
    assert await store.find_identity("track", "m1:1") == "m1:1"  # unchanged
    assert await store.find_identity("track", "m1:2") == "m1:2"  # unchanged


async def test_identity_for_unknown_track_is_none(db):
    assert await identity.identity_for_track_id("never:seen") is None


# ── collision resolution across a scan's clusters (ce-debug 2026-06-29) ───────

async def test_resolve_clusters_breaks_stale_alias_collision(db):
    # Stale aliases from a PRIOR over-merge map two DISTINCT clusters' keys to one
    # identity. Resolving them per-cluster would hand both the SAME identity, and
    # replace_catalog's INSERT OR IGNORE would then silently drop one entity's rows
    # — the "two album rows, one empty" bug. resolve_clusters must yield DISTINCT
    # identities (re-minting the colliding cluster to its own key) and re-point its
    # stale alias so the next scan is clean.
    await store.register_alias("track", "local:us-t1", "SHARED")
    await store.register_alias("track", "local:jp-t1", "SHARED")
    idents = await identity.resolve_clusters(
        "track", [[_item("local:us-t1")], [_item("local:jp-t1")]])
    assert len(set(idents)) == 2, idents
    # the first cluster keeps the shared identity (its history); the second is
    # re-minted to its own key, and that alias is corrected (re-pointed).
    assert idents[0] == "SHARED"
    assert idents[1] == "local:jp-t1"
    assert await store.find_identity("track", "local:jp-t1") == "local:jp-t1"
    assert await store.find_identity("track", "local:us-t1") == "SHARED"


async def test_resolve_clusters_preserves_allocate_once_without_collision(db):
    # No collision → identical to per-cluster resolve_cluster, and idempotent.
    a = await identity.resolve_clusters("track", [[_item("m1:1")], [_item("m1:2", mbid="m-x")]])
    assert a == ["m1:1", "mbid:m-x"]
    b = await identity.resolve_clusters("track", [[_item("m1:1")], [_item("m1:2", mbid="m-x")]])
    assert b == a  # stable across rescans
