"""U9: source priority, play-time fallback, and removed-source handling.

The fallback state machine tries each holder in the enqueue-time snapshot in
priority order, skips a holder that fails to stream (404/gone/auth, or a removed
source), and only declares the item unplayable when none serve; a single-holder
track behaves exactly as before. Also covers holder-key authorization, the holds
snapshot round-tripping through the queue, priority ordering at enqueue, and
priority persistence.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import database, state
from app.config import Settings
from app.models import Track
from app.queue.models import QueueItem


def _track(holds=None, stream_key="primary"):
    return Track(id="x", title="Song", artist="Act", album="Rec", duration_ms=1000,
                 stream_key=stream_key, holds=holds or [])


def _client():
    c = MagicMock()
    c.stream_url = lambda k: f"url:{k}"   # no STREAM_BASE_URL → registry resolves
    # U5: header-auth source (Plex-shaped). A bare MagicMock would auto-create a
    # truthy url_borne_auth_for, wrongly forcing the URL-auth proxy branch.
    c.url_borne_auth_for = MagicMock(return_value=False)
    return c


# ── _holder_keys ─────────────────────────────────────────────────────────────

def test_holder_keys_from_snapshot_in_order():
    t = _track(holds=[{"source_id": "m1", "key": "k1"}, {"source_id": "j", "key": "k2"}])
    assert state._holder_keys(t) == ["k1", "k2"]


def test_holder_keys_fall_back_to_stream_key():
    assert state._holder_keys(_track(stream_key="primary")) == ["primary"]


def test_holder_keys_empty_when_no_holders():
    assert state._holder_keys(_track(stream_key="")) == []


# ── _play_with_fallback state machine ────────────────────────────────────────

async def test_fallback_tries_next_holder_on_failure():
    item = QueueItem(track=_track(holds=[{"source_id": "m1", "key": "k1"},
                                         {"source_id": "j", "key": "k2"}]))
    play = AsyncMock(side_effect=[Exception("404"), None])  # holder 1 fails, holder 2 plays
    orouter = MagicMock(play=play)
    with patch.object(state, "output_router", orouter), patch.object(state, "record_play", MagicMock()):
        ok = await state._play_with_fallback(item, _client())
    assert ok is True
    assert play.await_count == 2
    assert play.await_args_list[1].args[0] == "url:k2"  # streamed the fallback holder


async def test_fallback_all_holders_fail_returns_false():
    item = QueueItem(track=_track(holds=[{"source_id": "m1", "key": "k1"},
                                         {"source_id": "j", "key": "k2"}]))
    play = AsyncMock(side_effect=[Exception("gone"), Exception("gone")])
    rec = MagicMock()
    with patch.object(state, "output_router", MagicMock(play=play)), patch.object(state, "record_play", rec):
        ok = await state._play_with_fallback(item, _client())
    assert ok is False
    rec.assert_not_called()  # nothing played → no play recorded


async def test_fallback_device_not_ready_halts():
    from app.output.base import DeviceNotReadyError
    item = QueueItem(track=_track(holds=[{"source_id": "m1", "key": "k1"}]))
    play = AsyncMock(side_effect=DeviceNotReadyError())
    with patch.object(state, "output_router", MagicMock(play=play)), patch.object(state, "record_play", MagicMock()):
        with pytest.raises(DeviceNotReadyError):
            await state._play_with_fallback(item, _client())


async def test_fallback_single_holder_uses_stream_key():
    item = QueueItem(track=_track(stream_key="primary"))
    play = AsyncMock(return_value=None)
    with patch.object(state, "output_router", MagicMock(play=play)), patch.object(state, "record_play", MagicMock()):
        ok = await state._play_with_fallback(item, _client())
    assert ok is True
    assert play.await_args_list[0].args[0] == "url:primary"


async def test_removed_source_sole_holder_fails_then_skips():
    # AE8: the only holder is a now-removed source → all holders fail → False
    # (the caller advances to the next item).
    item = QueueItem(track=_track(holds=[{"source_id": "removed", "key": "k1"}]))
    play = AsyncMock(side_effect=Exception("source removed"))
    with patch.object(state, "output_router", MagicMock(play=play)), patch.object(state, "record_play", MagicMock()):
        assert await state._play_with_fallback(item, _client()) is False


# ── holder-key authorization ─────────────────────────────────────────────────

def test_authorized_stream_key_covers_all_holders():
    item = QueueItem(track=_track(holds=[{"source_id": "m1", "key": "k1"},
                                         {"source_id": "j", "key": "k2"}],
                                  stream_key="primary"))
    fake_qe = MagicMock(queue=[item], history=[], state=MagicMock(current=None))
    with patch.object(state, "queue_engine", fake_qe):
        assert state.is_authorized_stream_key("primary") is True
        assert state.is_authorized_stream_key("k1") is True   # holder key authorized
        assert state.is_authorized_stream_key("k2") is True
        assert state.is_authorized_stream_key("nope") is False


# ── holds snapshot round-trips through the queue ─────────────────────────────

def test_queue_item_round_trip_preserves_holds():
    holds = [{"source_id": "m1", "key": "k1"}, {"source_id": "j", "key": "k2"}]
    item = QueueItem(track=_track(holds=holds))
    restored = QueueItem.from_dict(item.to_dict())
    assert restored.track.holds == holds


# ── priority persistence + enqueue ordering ──────────────────────────────────

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


async def test_source_priority_persists(db):
    assert await database.get_source_priority() == []
    await database.set_source_priority(["jelly", "m1", "local"])
    assert await database.get_source_priority() == ["jelly", "m1", "local"]


async def test_attach_holds_orders_by_global_priority(db):
    from app.api.guest import _attach_holds
    from app.catalog import store
    await store.replace_catalog([], [], [{"identity": "x", "title": "S", "title_base": "s",
                                          "artist": "A", "artist_base_key": "a", "duration_ms": 1}],
                                holds=[
        {"entity_type": "track", "identity": "x", "source_id": "m1", "provider_local_key": "m1:k", "priority": 0},
        {"entity_type": "track", "identity": "x", "source_id": "jelly", "provider_local_key": "jelly:k", "priority": 1},
    ])
    await store.register_alias("track", "x", "x")  # identity self-alias (as scan does)
    await database.set_source_priority(["jelly", "m1"])  # jelly outranks m1
    track = _track(stream_key="m1:k")
    track.id = "x"
    await _attach_holds(track)
    assert [h["source_id"] for h in track.holds] == ["jelly", "m1"]  # global priority wins
    assert track.holds[0]["key"] == "jelly:k"


# ── U16: play-failure skip notification (R22) ────────────────────────────────

async def test_emit_track_skipped_admin_gets_sources_guest_does_not():
    """The skip event carries sources_tried to ADMINS only (diagnostic); the
    guest broadcast omits it (title-only)."""
    track = _track(holds=[{"source_id": "m1", "key": "k1"}, {"source_id": "j", "key": "k2"}])
    track.title = "Dead Track"
    admin_ev = AsyncMock()
    guest_ev = AsyncMock()
    with patch("app.events.bus.manager.broadcast_to_admins", admin_ev), \
         patch("app.events.bus.manager.broadcast_to_guests", guest_ev):
        await state._emit_track_skipped(track)
    a = admin_ev.await_args.args[0]
    g = guest_ev.await_args.args[0]
    assert a.type == "track_skipped"
    assert a.track_title == "Dead Track" and a.sources_tried == ["m1", "j"]
    assert g.track_title == "Dead Track" and g.sources_tried is None


async def test_emit_track_skipped_no_holders_sources_none():
    """A single-holder/pre-snapshot track has no holds snapshot → sources_tried
    is None even for admins (nothing to report), still title-carrying."""
    track = _track(stream_key="primary")
    track.title = "Solo"
    admin_ev = AsyncMock()
    with patch("app.events.bus.manager.broadcast_to_admins", admin_ev), \
         patch("app.events.bus.manager.broadcast_to_guests", AsyncMock()):
        await state._emit_track_skipped(track)
    a = admin_ev.await_args.args[0]
    assert a.track_title == "Solo" and a.sources_tried is None


async def test_emit_track_skipped_swallows_broadcast_error():
    """A broadcast failure must never block the advance (best-effort)."""
    track = _track(holds=[{"source_id": "m1", "key": "k1"}])
    with patch("app.events.bus.manager.broadcast_to_admins",
               AsyncMock(side_effect=Exception("ws gone"))), \
         patch("app.events.bus.manager.broadcast_to_guests", AsyncMock()):
        await state._emit_track_skipped(track)  # must not raise


async def test_do_advance_emits_skip_when_all_holders_fail():
    """AE8: every holder for the popped item fails → the skip notification fires
    and the advance continues to the next item (here the queue then empties)."""
    item = QueueItem(track=_track(holds=[{"source_id": "m1", "key": "k1"}]))
    item.track.title = "Dead Track"
    qe = MagicMock()
    qe.advance = AsyncMock(side_effect=[item, None])
    qe.state = MagicMock(current=None)
    emit = AsyncMock()
    with patch.object(state, "queue_engine", qe), \
         patch.object(state, "get_plex_client", AsyncMock(return_value=_client())), \
         patch.object(state, "_play_with_fallback", AsyncMock(return_value=False)), \
         patch.object(state, "_emit_track_skipped", emit):
        await state._do_advance()
    emit.assert_awaited_once()
    assert emit.await_args.args[0].title == "Dead Track"


async def test_do_advance_no_skip_when_holder_plays():
    """The happy path does not flash a skip notification."""
    item = QueueItem(track=_track(holds=[{"source_id": "m1", "key": "k1"}]))
    qe = MagicMock()
    qe.advance = AsyncMock(side_effect=[item, None])
    qe.state = MagicMock(current=None)
    emit = AsyncMock()
    with patch.object(state, "queue_engine", qe), \
         patch.object(state, "get_plex_client", AsyncMock(return_value=_client())), \
         patch.object(state, "_play_with_fallback", AsyncMock(return_value=True)), \
         patch.object(state, "_emit_track_skipped", emit):
        await state._do_advance()
    emit.assert_not_awaited()
