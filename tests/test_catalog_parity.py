"""Native ↔ catalog parity regression guard (parity plan U5).

When a non-Plex source connects, browse flips from the native Plex pipeline onto
the catalog floor. This test pins the parity properties that flip must preserve,
so a future change can't silently reintroduce the regressions this plan fixed:

1. **Fold parity** — catalog album folding equals the native ``_group_albums``
   fold across all four track_count cases, using native as the ground-truth
   oracle (R5/R6; AE3/AE4/AE7).
2. **Gate dormancy** — a Plex-only / multi-Plex-server registry keeps the
   catalog floor inactive (R8/AE6).
3. **Per-source picker present + play-from-source resolves** the chosen copy on
   a merged catalog item (R2/R7; AE1).
"""

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import database
from app.api import guest
from app.catalog import merge, store, views
from app.config import Settings
from app.models import Album
from app.sources.base import Capabilities


# ── 1. Fold parity: catalog == native across the four count cases ─────────────
# Each case is a set of (track_count, server) copies of ONE title|artist. Native
# _group_albums buckets by exact track_count (None its own value); catalog
# merge.album_same must produce the SAME number of folded groups.

def _native_group_count(rows) -> int:
    tagged = [(Album(id=f"{srv}:{i}", title="Rec", artist="Act", track_count=tc), srv)
              for i, (tc, srv) in enumerate(rows)]
    return len(guest._group_albums(tagged))


def _catalog_group_count(rows) -> int:
    items = [{"match_ids": {}, "source_id": srv, "local_key": f"{srv}:{i}",
              "title_base": "rec", "artist_base_key": "act", "track_count": tc}
             for i, (tc, srv) in enumerate(rows)]
    return len(merge.group(items, merge.album_coarse, merge.album_same))


# (rows, expected folded-group count) — the oracle pins the expected value too,
# so an identical regression in BOTH pipelines can't slip through count-equality.
_FOLD_CASES = {
    "both_unknown":      ([(None, "Plex"), (None, "Jelly")], 1),   # AE3: folds
    "known_and_unknown": ([(10, "Plex"), (None, "Jelly")], 2),     # AE7: splits
    "equal_known":       ([(10, "Plex"), (10, "Jelly")], 1),       # folds
    "different_known":   ([(10, "Plex"), (12, "Jelly")], 2),       # AE4: distinct
    "three_way_mixed":   ([(10, "Plex"), (10, "Jelly"), (None, "Local")], 2),
}


@pytest.mark.parametrize("rows,expected", list(_FOLD_CASES.values()),
                         ids=list(_FOLD_CASES))
def test_fold_parity_matches_native(rows, expected):
    native = _native_group_count(rows)
    catalog = _catalog_group_count(rows)
    assert catalog == native == expected


# ── 2. Gate dormancy: Plex-only stays native (R8/AE6) ────────────────────────

class _FakeSource:
    def __init__(self, source_type="plex"):
        self.capabilities = Capabilities(native_search=True)
        self.source_type = source_type


class _FakeRegistry:
    def __init__(self, sources):
        self.sources = sources


async def _gate_with(reg):
    with patch("app.state.get_plex_client", AsyncMock(return_value=reg)):
        return await guest._catalog_active()


async def test_gate_dormant_for_single_plex():
    assert await _gate_with(_FakeRegistry([_FakeSource("plex")])) is False


async def test_gate_dormant_for_multi_plex_servers():
    assert await _gate_with(_FakeRegistry([_FakeSource("plex"), _FakeSource("plex")])) is False


async def test_gate_active_once_non_plex_connects():
    assert await _gate_with(_FakeRegistry([_FakeSource("plex"), _FakeSource("jellyfin")])) is True


# ── 3. Picker present + play-from-source resolves the chosen copy ─────────────

@pytest.fixture
async def seeded(tmp_path, monkeypatch):
    """A merged album/track held by Plex(m1, prio0) + Jellyfin(jelly, prio1)."""
    s = Settings(data_dir=tmp_path, secret_key="test")
    monkeypatch.setattr(database, "settings", s)
    await database.init_db()
    await store.replace_catalog(
        artists=[{"identity": "ar", "title": "Act", "base_key": "act"}],
        albums=[{"identity": "al", "title": "Rec", "title_base": "rec", "artist": "Act",
                 "artist_base_key": "act", "year": 2020, "track_count": 2}],
        tracks=[{"identity": "t1", "title": "Song One", "title_base": "song one",
                 "artist": "Act", "artist_base_key": "act", "album": "Rec",
                 "album_identity": "al", "duration_ms": 180000,
                 "disc_number": 1, "track_number": 1}],
        holds=[
            {"entity_type": "album", "identity": "al", "source_id": "m1", "provider_local_key": "m1:al", "priority": 0, "server_name": "Plex"},
            {"entity_type": "album", "identity": "al", "source_id": "jelly", "provider_local_key": "jelly:al", "priority": 1, "server_name": "Jelly"},
            {"entity_type": "track", "identity": "t1", "source_id": "m1", "provider_local_key": "m1:p1", "priority": 0, "server_name": "Plex"},
            {"entity_type": "track", "identity": "t1", "source_id": "jelly", "provider_local_key": "jelly:p1", "priority": 1, "server_name": "Jelly"},
        ],
    )
    try:
        yield
    finally:
        await database.close_db()


async def test_merged_item_exposes_per_source_picker(seeded):
    # The merged track carries holds for both sources, so _track_dict emits a
    # per-source list the shared module renders as "Play From Source…".
    typed = _FakeRegistry([type("S", (), {"source_id": "m1", "source_type": "plex"})(),
                           type("S", (), {"source_id": "jelly", "source_type": "jellyfin"})()])
    with patch("app.state.get_plex_client", AsyncMock(return_value=typed)):
        tracks = await views.album_tracks("al")
    d = guest._track_dict(tracks[0])
    assert d["sources"] == [{"server_name": "Plex", "source_type": "plex"},
                            {"server_name": "Jelly", "source_type": "jellyfin"}]


async def test_play_from_source_resolves_chosen_copy(seeded):
    # Picking Jellyfin on the merged track enqueues the Jellyfin copy as primary
    # while Plex stays as fallback — the gate flip preserves play-from-source.
    from app.queue.engine import QueueEngine
    qe = QueueEngine()
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("app.state.queue_engine", qe))
        stack.enter_context(patch("app.state.get_plex_client", AsyncMock(return_value=MagicMock())))
        stack.enter_context(patch("app.api.guest._catalog_active", AsyncMock(return_value=True)))
        stack.enter_context(patch("app.database.save_queue", AsyncMock()))
        stack.enter_context(patch("app.database.save_history", AsyncMock()))
        await guest.append_to_queue(guest.QueueAppendRequest(track_id="t1", source_server_name="Jelly"))
    assert [h["source_id"] for h in qe.queue[0].track.holds] == ["jelly", "m1"]
    assert qe.queue[0].track.stream_key == "jelly:p1"
