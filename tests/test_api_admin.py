"""Tests for the admin API routes.

Shared fixtures (mock_session, mock_state, client, anon_client) and the
make_track/make_album/make_artist factories live in tests/conftest.py.
Transport-endpoint tests for POST /admin/playback/previous live in
tests/test_api_playback.py.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.plex.models import Library, Track, Album, Artist
from app.queue.models import QueueEndBehavior

from tests.conftest import make_track, make_album, make_artist


# ── Auth guard ────────────────────────────────────────────────────────────────

def test_queue_requires_auth(anon_client, mock_session):
    resp = anon_client.get("/admin/queue")
    assert resp.status_code == 401


def test_settings_requires_auth(anon_client, mock_session):
    resp = anon_client.get("/admin/settings")
    assert resp.status_code == 401


def test_playback_pause_requires_auth(anon_client, mock_session):
    resp = anon_client.post("/admin/playback/pause")
    assert resp.status_code == 401


# ── Surprise Me settings (2026-06-17 plan U3) ────────────────────────────────

def test_settings_rejects_invalid_surprise_mode(client, mock_state):
    """A bad source mode is rejected at the Pydantic layer (Literal) → 422."""
    resp = client.post("/admin/settings", json={"surprise_me_source_mode": "bogus"})
    assert resp.status_code == 422


def test_settings_persists_surprise_fields(client, mock_state):
    """Valid enabled + source mode both persist via set_setting (200)."""
    # surprise_me_enabled now also triggers the appearance_changed broadcast
    # (code-review #6), so stub get_setting + the broadcast so this persistence
    # test stays focused on set_setting.
    with patch("app.api.admin.database.set_setting", AsyncMock()) as ss, \
         patch("app.api.admin.database.get_setting", AsyncMock(return_value=None)), \
         patch("app.events.bus.manager.broadcast_to_all", AsyncMock()):
        resp = client.post(
            "/admin/settings",
            json={"surprise_me_enabled": False, "surprise_me_source_mode": "plex"},
        )
    assert resp.status_code == 200
    persisted = {c.args[0]: c.args[1] for c in ss.call_args_list}
    assert persisted.get("surprise_me_enabled") == "0"
    assert persisted.get("surprise_me_source_mode") == "plex"


def test_settings_rejects_invalid_surprise_diversity(client, mock_state):
    resp = client.post("/admin/settings", json={"surprise_me_diversity": "bogus"})
    assert resp.status_code == 422


def test_settings_surprise_toggle_broadcasts_appearance_event(client, mock_state):
    """Code-review #6: toggling surprise_me_enabled broadcasts an appearance_changed
    event carrying the new flag, so connected clients show/hide the dock live."""
    store: dict[str, str] = {}
    async def fake_get(key, default=None): return store.get(key, default)
    async def fake_set(key, value): store[key] = value
    events: list = []
    async def cap(ev): events.append(ev)
    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)), \
         patch("app.database.set_setting", AsyncMock(side_effect=fake_set)), \
         patch("app.events.bus.manager.broadcast_to_all", AsyncMock(side_effect=cap)):
        resp = client.post("/admin/settings", json={"surprise_me_enabled": False})
    assert resp.status_code == 200
    appearance = [e for e in events if getattr(e, "type", None) == "appearance_changed"]
    assert appearance, "toggling surprise_me_enabled must broadcast appearance_changed"
    assert appearance[0].surprise_me_enabled is False


def test_settings_persists_surprise_diversity(client, mock_state):
    with patch("app.api.admin.database.set_setting", AsyncMock()) as ss:
        resp = client.post("/admin/settings", json={"surprise_me_diversity": "album"})
    assert resp.status_code == 200
    persisted = {c.args[0]: c.args[1] for c in ss.call_args_list}
    assert persisted.get("surprise_me_diversity") == "album"


# ── Random-pick length band (2026-06-20 plan U1) ─────────────────────────────

def test_settings_persists_random_length_band(client, mock_state):
    """Valid min/max persist as integer-seconds strings (200)."""
    with patch("app.api.admin.database.set_setting", AsyncMock()) as ss, \
         patch("app.api.admin.database.get_setting", AsyncMock(return_value=None)):
        resp = client.post(
            "/admin/settings",
            json={"random_min_seconds": 30, "random_max_seconds": 600},
        )
    assert resp.status_code == 200
    persisted = {c.args[0]: c.args[1] for c in ss.call_args_list}
    assert persisted.get("random_min_seconds") == "30"
    assert persisted.get("random_max_seconds") == "600"


def test_settings_random_band_zero_is_off(client, mock_state):
    """0/0 is accepted (clearing both bounds) and persists '0'/'0'."""
    with patch("app.api.admin.database.set_setting", AsyncMock()) as ss, \
         patch("app.api.admin.database.get_setting", AsyncMock(return_value=None)):
        resp = client.post(
            "/admin/settings",
            json={"random_min_seconds": 0, "random_max_seconds": 0},
        )
    assert resp.status_code == 200
    persisted = {c.args[0]: c.args[1] for c in ss.call_args_list}
    assert persisted.get("random_min_seconds") == "0"
    assert persisted.get("random_max_seconds") == "0"


def test_settings_rejects_min_ge_max(client, mock_state):
    """A contradictory band (both active, min >= max) is rejected; nothing is written."""
    with patch("app.api.admin.database.set_setting", AsyncMock()) as ss, \
         patch("app.api.admin.database.get_setting", AsyncMock(return_value=None)):
        resp = client.post(
            "/admin/settings",
            json={"random_min_seconds": 600, "random_max_seconds": 60},
        )
    assert resp.status_code == 422
    persisted = {c.args[0] for c in ss.call_args_list}
    assert "random_min_seconds" not in persisted
    assert "random_max_seconds" not in persisted


def test_settings_rejects_negative_length(client, mock_state):
    """A negative bound is rejected and writes nothing."""
    with patch("app.api.admin.database.set_setting", AsyncMock()) as ss, \
         patch("app.api.admin.database.get_setting", AsyncMock(return_value=None)):
        resp = client.post("/admin/settings", json={"random_min_seconds": -5})
    assert resp.status_code == 422
    assert not ss.call_args_list


def test_get_settings_echoes_random_band(client, mock_state):
    """GET /settings echoes the stored band as integer seconds for the form."""
    store = {"random_min_seconds": "30", "random_max_seconds": "600"}
    async def fake_get(key, default=None): return store.get(key, default)
    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)):
        resp = client.get("/admin/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["random_min_seconds"] == 30
    assert body["random_max_seconds"] == 600


# ── International rail settings (2026-06-22 plan 004) ─────────────────────────

def test_settings_persists_international_rail(client, mock_state):
    """Alpha mode + both thresholds persist via set_setting (200)."""
    with patch("app.api.admin.database.set_setting", AsyncMock()) as ss, \
         patch("app.api.admin.database.get_setting", AsyncMock(return_value=None)):
        resp = client.post(
            "/admin/settings",
            json={"rail_alpha_mode": "international",
                  "rail_artist_threshold": 3, "rail_album_threshold": 4},
        )
    assert resp.status_code == 200
    persisted = {c.args[0]: c.args[1] for c in ss.call_args_list}
    assert persisted.get("rail_alpha_mode") == "international"
    assert persisted.get("rail_artist_threshold") == "3"
    assert persisted.get("rail_album_threshold") == "4"


def test_settings_rejects_invalid_rail_alpha_mode(client, mock_state):
    """A bad alpha mode is rejected at the Pydantic layer (Literal) → 422."""
    resp = client.post("/admin/settings", json={"rail_alpha_mode": "bogus"})
    assert resp.status_code == 422


def test_settings_rejects_rail_threshold_below_one(client, mock_state):
    """A threshold < 1 is rejected and writes nothing."""
    with patch("app.api.admin.database.set_setting", AsyncMock()) as ss, \
         patch("app.api.admin.database.get_setting", AsyncMock(return_value=None)):
        resp = client.post("/admin/settings", json={"rail_artist_threshold": 0})
    assert resp.status_code == 422
    assert "rail_artist_threshold" not in {c.args[0] for c in ss.call_args_list}


def test_get_settings_echoes_international_rail(client, mock_state):
    """GET /settings echoes the stored alpha mode + thresholds for the form."""
    store = {"rail_alpha_mode": "international",
             "rail_artist_threshold": "3", "rail_album_threshold": "4"}
    async def fake_get(key, default=None): return store.get(key, default)
    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)):
        resp = client.get("/admin/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["rail_alpha_mode"] == "international"
    assert body["rail_artist_threshold"] == 3
    assert body["rail_album_threshold"] == 4


# ── Rating display style (2026-06-27 plan U1) ────────────────────────────────

def test_settings_persists_rating_style(client, mock_state):
    """A valid rating_style persists via set_setting (200)."""
    with patch("app.api.admin.database.set_setting", AsyncMock()) as ss, \
         patch("app.api.admin.database.get_setting", AsyncMock(return_value=None)):
        resp = client.post("/admin/settings", json={"rating_style": "dots"})
    assert resp.status_code == 200
    persisted = {c.args[0]: c.args[1] for c in ss.call_args_list}
    assert persisted.get("rating_style") == "dots"


def test_settings_rejects_invalid_rating_style(client, mock_state):
    """A bad rating_style is rejected at the Pydantic layer (Literal) → 422."""
    resp = client.post("/admin/settings", json={"rating_style": "triangles"})
    assert resp.status_code == 422


def test_get_settings_echoes_rating_style(client, mock_state):
    """GET /settings echoes the stored style; unset hydrates the radio as stars."""
    store = {"rating_style": "bars"}
    async def fake_get(key, default=None): return store.get(key, default)
    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)):
        resp = client.get("/admin/settings")
    assert resp.status_code == 200
    assert resp.json()["rating_style"] == "bars"
    with patch("app.database.get_setting", AsyncMock(return_value=None)):
        resp = client.get("/admin/settings")
    assert resp.json()["rating_style"] == "stars"


def test_settings_rating_style_broadcasts(client, mock_state):
    """Changing rating_style broadcasts an appearance_changed event carrying the
    new style, so connected clients (admin + guests) update live without a reload
    — consistent with scheme/view (reverses the original reload-only behavior)."""
    store: dict[str, str] = {}
    async def fake_get(key, default=None): return store.get(key, default)
    async def fake_set(key, value): store[key] = value
    events: list = []
    async def cap(ev): events.append(ev)
    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)), \
         patch("app.database.set_setting", AsyncMock(side_effect=fake_set)), \
         patch("app.events.bus.manager.broadcast_to_all", AsyncMock(side_effect=cap)):
        resp = client.post("/admin/settings", json={"rating_style": "bars"})
    assert resp.status_code == 200
    assert store.get("rating_style") == "bars"
    appearance = [e for e in events if getattr(e, "type", None) == "appearance_changed"]
    assert appearance, "changing rating_style must broadcast appearance_changed"
    assert appearance[0].rating_style == "bars"


def test_settings_international_rail_broadcasts_appearance_event(client, mock_state):
    """Saving the alpha-rail config broadcasts appearance_changed carrying the new
    values, so connected guests update their rail live (parity with rail_mode)."""
    store: dict[str, str] = {}
    async def fake_get(key, default=None): return store.get(key, default)
    async def fake_set(key, value): store[key] = value
    events: list = []
    async def cap(ev): events.append(ev)
    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)), \
         patch("app.database.set_setting", AsyncMock(side_effect=fake_set)), \
         patch("app.events.bus.manager.broadcast_to_all", AsyncMock(side_effect=cap)):
        resp = client.post(
            "/admin/settings",
            json={"rail_alpha_mode": "international", "rail_artist_threshold": 3},
        )
    assert resp.status_code == 200
    appearance = [e for e in events if getattr(e, "type", None) == "appearance_changed"]
    assert appearance, "saving rail_alpha_mode must broadcast appearance_changed"
    assert appearance[0].rail_alpha_mode == "international"
    assert appearance[0].rail_artist_threshold == 3


async def test_get_random_length_bounds_unset():
    """No stored band → (None, None) — fully opt-in."""
    from app import database
    with patch("app.database.get_setting", AsyncMock(return_value=None)):
        assert await database.get_random_length_bounds() == (None, None)


async def test_get_random_length_bounds_zero_is_none():
    """0 means 'bound off' on both ends."""
    from app import database
    store = {"random_min_seconds": "0", "random_max_seconds": "0"}
    async def fake_get(key, default=None): return store.get(key, default)
    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)):
        assert await database.get_random_length_bounds() == (None, None)


async def test_get_random_length_bounds_min_only():
    """min-only → (min_ms, None); seconds are converted to ms."""
    from app import database
    store = {"random_min_seconds": "30"}
    async def fake_get(key, default=None): return store.get(key, default)
    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)):
        assert await database.get_random_length_bounds() == (30000, None)


async def test_get_random_length_bounds_max_only():
    """max-only → (None, max_ms)."""
    from app import database
    store = {"random_max_seconds": "600"}
    async def fake_get(key, default=None): return store.get(key, default)
    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)):
        assert await database.get_random_length_bounds() == (None, 600000)


def test_surprise_recent_requires_auth(anon_client, mock_session):
    resp = anon_client.get("/admin/surprise/recent")
    assert resp.status_code == 401


def test_surprise_recent_reports_tally(client, mock_state):
    """The readout tallies recently-resolved sources (Covers AE6)."""
    from app.queue import surprise as sm
    sm._RECENT_SOURCES.clear()
    sm.record_source("plex_sonic")
    sm.record_source("plex_sonic")
    sm.record_source("random")
    resp = client.get("/admin/surprise/recent")
    assert resp.status_code == 200
    data = resp.json()
    assert data["tally"] == {"plex_sonic": 2, "random": 1}
    assert len(data["recent"]) == 3


# ── Queue read ────────────────────────────────────────────────────────────────

async def test_get_queue_empty(client, mock_state):
    resp = client.get("/admin/queue")
    assert resp.status_code == 200
    data = resp.json()
    assert data["queue"] == []
    assert data["history"] == []
    assert data["is_locked"] is False


async def test_get_queue_exposes_added_at_and_album_id(client, mock_state):
    """U1 (unify-queue-remove): the admin queue payload carries added_at and
    album_id per item so the shared renderer can group albums (by album_id) and
    remove them entry-based via /api/queue/undo (matches on track_id+added_at)."""
    qe, _ = mock_state
    await qe.append(make_track("t1"), bypass_lock=True)
    await qe.append(make_track("t2"), bypass_lock=True)
    items = client.get("/admin/queue").json()["queue"]
    assert len(items) == 2
    for item, engine_item in zip(items, qe.queue):
        assert item["added_at"] == engine_item.added_at
        assert item["added_at"]  # non-empty
        assert "album_id" in item  # present (may be None for albumless tracks)
        assert item["position"] is not None  # existing field intact


# ── Queue mutations ───────────────────────────────────────────────────────────

async def test_queue_clear_requires_confirmed(client, mock_state):
    resp = client.post("/admin/queue/clear", json={"confirmed": False})
    assert resp.status_code == 400


async def test_queue_clear_with_confirmed(client, mock_state):
    qe, _ = mock_state
    await qe.append(make_track("t1"), bypass_lock=True)
    resp = client.post("/admin/queue/clear", json={"confirmed": True})
    assert resp.status_code == 200
    assert len(qe.queue) == 0


async def test_queue_move(client, mock_state):
    qe, _ = mock_state
    await qe.append(make_track("t1"), bypass_lock=True)
    await qe.append(make_track("t2"), bypass_lock=True)
    await qe.append(make_track("t3"), bypass_lock=True)
    resp = client.post("/admin/queue/move", json={"from_position": 2, "to_position": 0})
    assert resp.status_code == 200
    assert qe.queue[0].track.id == "t3"


async def test_queue_move_out_of_range(client, mock_state):
    resp = client.post("/admin/queue/move", json={"from_position": 99, "to_position": 0})
    assert resp.status_code == 400


async def test_queue_remove(client, mock_state):
    qe, _ = mock_state
    await qe.append(make_track("t1"), bypass_lock=True)
    await qe.append(make_track("t2"), bypass_lock=True)
    resp = client.delete("/admin/queue/0")
    assert resp.status_code == 200
    assert len(qe.queue) == 1
    assert qe.queue[0].track.id == "t2"


async def test_queue_remove_out_of_range(client, mock_state):
    resp = client.delete("/admin/queue/99")
    assert resp.status_code == 400


async def test_queue_play_next(client, mock_state):
    qe, _ = mock_state
    await qe.append(make_track("t1"), bypass_lock=True)
    await qe.append(make_track("t2"), bypass_lock=True)
    await qe.append(make_track("t3"), bypass_lock=True)
    resp = client.post("/admin/queue/2/play-next")
    assert resp.status_code == 200
    assert qe.queue[0].track.id == "t3"


# ── Lock ──────────────────────────────────────────────────────────────────────

async def test_queue_lock(client, mock_state):
    qe, _ = mock_state
    resp = client.post("/admin/queue/lock")
    assert resp.status_code == 200
    assert qe.is_locked is True


async def test_queue_unlock(client, mock_state):
    qe, _ = mock_state
    await qe.lock()
    resp = client.post("/admin/queue/unlock")
    assert resp.status_code == 200
    assert qe.is_locked is False


# ── Playback controls ─────────────────────────────────────────────────────────

async def test_playback_pause(client, mock_state):
    qe, or_ = mock_state
    resp = client.post("/admin/playback/pause")
    assert resp.status_code == 200
    or_.pause.assert_awaited_once()


async def test_playback_resume(client, mock_state):
    qe, or_ = mock_state
    resp = client.post("/admin/playback/resume")
    assert resp.status_code == 200
    or_.resume.assert_awaited_once()


async def test_playback_resume_continues_after_closing(client, mock_state, monkeypatch):
    """Closing Time freeze active → resume clears the banner, re-arms (flag drops),
    and continues via _do_advance; it does NOT call output_router.resume()."""
    import app.state as st
    from app.events import bus as _bus
    qe, or_ = mock_state
    monkeypatch.setattr(st, "_closing_active", True)
    with patch.object(st, "_do_advance", AsyncMock()) as adv, \
         patch.object(_bus.manager, "broadcast_to_all", AsyncMock()) as bc:
        resp = client.post("/admin/playback/resume")
    assert resp.status_code == 200
    adv.assert_awaited_once()
    assert st._closing_active is False
    or_.resume.assert_not_awaited()
    ev = bc.await_args.args[0]
    assert ev.type == "closing_time" and ev.active is False


async def test_playback_resume_normal_when_not_closing(client, mock_state, monkeypatch):
    """No freeze → ordinary mid-track resume path (unchanged)."""
    import app.state as st
    qe, or_ = mock_state
    monkeypatch.setattr(st, "_closing_active", False)
    resp = client.post("/admin/playback/resume")
    assert resp.status_code == 200
    or_.resume.assert_awaited_once()


async def test_playback_skip_clears_closing(client, mock_state, monkeypatch):
    """Skipping during a Closing Time freeze restarts playback AND clears the
    banner everywhere (the admin must not stay locked behind the message)."""
    import app.state as st
    from app.events import bus as _bus
    qe, or_ = mock_state
    await qe.append(make_track("next"), bypass_lock=True)
    monkeypatch.setattr(st, "_closing_active", True)
    monkeypatch.setattr(st, "_closing_message", "Last call")
    with patch("app.state.get_plex_client", AsyncMock(return_value=MagicMock())), \
         patch("app.state._make_stream_url", return_value="http://stream/next"), \
         patch("app.database.increment_play_count", AsyncMock()), \
         patch("app.database.set_play_track_meta", AsyncMock()), \
         patch.object(_bus.manager, "broadcast_to_all", AsyncMock()) as bc:
        resp = client.post("/admin/playback/skip")
    assert resp.status_code == 200
    or_.play.assert_awaited_once()           # playback actually restarted
    assert st._closing_active is False
    cleared = [c.args[0] for c in bc.await_args_list
               if getattr(c.args[0], "type", None) == "closing_time"]
    assert cleared and cleared[-1].active is False


async def test_playback_skip_no_closing_broadcast_when_not_frozen(client, mock_state, monkeypatch):
    """No freeze → skip does not emit a closing_time clear (no spurious churn)."""
    import app.state as st
    from app.events import bus as _bus
    qe, or_ = mock_state
    await qe.append(make_track("next"), bypass_lock=True)
    monkeypatch.setattr(st, "_closing_active", False)
    with patch("app.state.get_plex_client", AsyncMock(return_value=MagicMock())), \
         patch("app.state._make_stream_url", return_value="http://stream/next"), \
         patch("app.database.increment_play_count", AsyncMock()), \
         patch("app.database.set_play_track_meta", AsyncMock()), \
         patch.object(_bus.manager, "broadcast_to_all", AsyncMock()) as bc:
        resp = client.post("/admin/playback/skip")
    assert resp.status_code == 200
    closing = [c for c in bc.await_args_list
               if getattr(c.args[0], "type", None) == "closing_time"]
    assert not closing


async def test_admin_now_playing_includes_closing_state(client, mock_state, monkeypatch):
    """U3: admin hydrates the banner from the now-playing GET while frozen."""
    import app.state as st
    monkeypatch.setattr(st, "_closing_active", True)
    monkeypatch.setattr(st, "_closing_message", "Last call")
    data = client.get("/admin/playback/now-playing").json()
    assert data["closing_active"] is True
    assert data["closing_message"] == "Last call"


async def test_admin_queue_includes_closing_state(client, mock_state, monkeypatch):
    """U3: the admin queue/state snapshot carries the same closing fields."""
    import app.state as st
    monkeypatch.setattr(st, "_closing_active", True)
    monkeypatch.setattr(st, "_closing_message", "Last call")
    data = client.get("/admin/queue").json()
    assert data["closing_active"] is True
    assert data["closing_message"] == "Last call"


async def test_playback_volume(client, mock_state):
    qe, or_ = mock_state
    resp = client.post("/admin/playback/volume", json={"level": 0.8})
    assert resp.status_code == 200
    or_.set_volume.assert_awaited_once_with(0.8)


async def test_playback_volume_out_of_range(client, mock_state):
    resp = client.post("/admin/playback/volume", json={"level": 1.5})
    assert resp.status_code == 422  # pydantic validation


async def test_playback_skip_does_not_call_stop_before_play(client, mock_state):
    """playback_skip must NOT call output_router.stop() before play().

    The earlier ordering (stop → advance → play) broke the DLNA backend:
    DlnaBackend.stop() cleared _dmr, and the subsequent play() raised
    RuntimeError because the backend was torn down. The natural-EOS
    advance path in state._do_advance also goes directly to play()
    without a prior stop(); this endpoint now matches that pattern.
    """
    qe, or_ = mock_state
    await qe.append(make_track("t1"), bypass_lock=True)
    await qe.append(make_track("t2"), bypass_lock=True)
    with patch("app.state.get_plex_client", AsyncMock(return_value=MagicMock())), \
         patch("app.state._make_stream_url", return_value="http://stream/t1"):
        resp = client.post("/admin/playback/skip")
    assert resp.status_code == 200
    or_.stop.assert_not_awaited(), (
        "playback_skip must NOT call output_router.stop() when there's a "
        "next track — it broke the DLNA backend via _dmr=None teardown"
    )
    or_.play.assert_awaited_once()


async def test_playback_skip_stops_when_queue_empty(client, mock_state):
    """When advance() returns no next item (queue empty), the skip
    endpoint must call stop() to halt playback fully — there's no
    next track for play() to transition the renderer to.
    """
    qe, or_ = mock_state
    # No items in queue → advance() returns None.
    resp = client.post("/admin/playback/skip")
    assert resp.status_code == 200
    or_.stop.assert_awaited_once()
    or_.play.assert_not_awaited()


async def test_playback_skip_increments_play_counts(client, mock_state):
    """Skipping forward to a track is a real play and must count — mirrors
    natural EOS advance (state._do_advance) and Skip Back. Regression guard:
    skip historically incremented nothing, so a track only ever reached by
    skipping never appeared on Most Played (the record_play unification fix)."""
    import asyncio as _asyncio
    qe, or_ = mock_state
    await qe.append(make_track("t1"), bypass_lock=True)
    await qe.append(make_track("t2"), bypass_lock=True)
    inc = AsyncMock()
    with patch("app.state.get_plex_client", AsyncMock(return_value=MagicMock())), \
         patch("app.state._make_stream_url", return_value="http://stream/t1"), \
         patch("app.database.increment_play_count", inc), \
         patch("app.database.set_play_track_meta", AsyncMock()):
        resp = client.post("/admin/playback/skip")
        assert resp.status_code == 200
        # Counts are fire-and-forget tasks on the app loop; poll briefly.
        for _ in range(100):
            if inc.await_count >= 3:
                break
            await _asyncio.sleep(0.01)
    kinds = {call.args[0] for call in inc.await_args_list}
    assert kinds == {"track", "album", "artist"}


async def test_playback_skip_captures_track_meta(client, mock_state):
    """Skip forward captures display metadata for the now-playing track so it
    surfaces on Most Played even without a later Plex backfill — same shape as
    _do_advance / Skip Back."""
    import asyncio as _asyncio
    qe, or_ = mock_state
    await qe.append(make_track("t1"), bypass_lock=True)
    await qe.append(make_track("t2"), bypass_lock=True)
    meta = AsyncMock()
    with patch("app.state.get_plex_client", AsyncMock(return_value=MagicMock())), \
         patch("app.state._make_stream_url", return_value="http://stream/t1"), \
         patch("app.database.increment_play_count", AsyncMock()), \
         patch("app.database.set_play_track_meta", meta):
        resp = client.post("/admin/playback/skip")
        assert resp.status_code == 200
        for _ in range(100):
            if meta.await_count >= 1:
                break
            await _asyncio.sleep(0.01)
    meta.assert_awaited_once()
    tid, payload = meta.await_args.args
    assert tid == "t1"
    assert payload["title"] == "Song" and "artist" in payload


# ── Settings ──────────────────────────────────────────────────────────────────

async def test_settings_round_trip(client, mock_state):
    with patch("app.database.get_setting", AsyncMock(return_value=None)), \
         patch("app.database.set_setting", AsyncMock()):
        resp = client.get("/admin/settings")
        assert resp.status_code == 200

        resp = client.post("/admin/settings", json={
            "queue_end_behavior": "full_random",
            "queue_display_n": 5,
            "queue_display_m": 3,
        })
        assert resp.status_code == 200
        qe, _ = mock_state
        assert qe.end_behavior == QueueEndBehavior.FULL_RANDOM


async def test_settings_flood_control_round_trip(client, mock_state):
    """U1 (Flood Control): the flag defaults off, persists as "1"/"0", reads
    back as a bool, and an omitted field doesn't clobber the stored value."""
    # Default off when unset.
    with patch("app.database.get_setting", AsyncMock(return_value=None)), \
         patch("app.database.set_setting", AsyncMock()) as set_mock:
        assert client.get("/admin/settings").json()["flood_control"] is False
        # POST true → persisted as "1"; POST false → "0".
        assert client.post("/admin/settings", json={"flood_control": True}).status_code == 200
        set_mock.assert_any_call("flood_control", "1")
        client.post("/admin/settings", json={"flood_control": False})
        set_mock.assert_any_call("flood_control", "0")

    # Omitted field doesn't write flood_control at all (per-field is-not-None guard).
    with patch("app.database.get_setting", AsyncMock(return_value=None)), \
         patch("app.database.set_setting", AsyncMock()) as set_mock:
        client.post("/admin/settings", json={"queue_end_behavior": "stop"})
        assert not any(c.args and c.args[0] == "flood_control" for c in set_mock.call_args_list)

    # Stored "1" reads back as bool true.
    async def _get(key, default=None):
        return "1" if key == "flood_control" else None
    with patch("app.database.get_setting", AsyncMock(side_effect=_get)), \
         patch("app.database.set_setting", AsyncMock()):
        assert client.get("/admin/settings").json()["flood_control"] is True


async def test_settings_lyrics_contribute_round_trip(client, mock_state):
    """Lyrics contribute prompt (contribute-prompt plan U2): the flag defaults
    ON (unset → true), persists as "1"/"0", reads back as a bool, and an omitted
    field doesn't clobber the stored value."""
    # Default ON when unset.
    with patch("app.database.get_setting", AsyncMock(return_value=None)), \
         patch("app.database.set_setting", AsyncMock()) as set_mock:
        assert client.get("/admin/settings").json()["lyrics_contribute_enabled"] is True
        # POST false → persisted as "0"; POST true → "1".
        assert client.post(
            "/admin/settings", json={"lyrics_contribute_enabled": False}
        ).status_code == 200
        set_mock.assert_any_call("lyrics_contribute_enabled", "0")
        client.post("/admin/settings", json={"lyrics_contribute_enabled": True})
        set_mock.assert_any_call("lyrics_contribute_enabled", "1")

    # Omitted field doesn't write the key (per-field is-not-None guard).
    with patch("app.database.get_setting", AsyncMock(return_value=None)), \
         patch("app.database.set_setting", AsyncMock()) as set_mock:
        client.post("/admin/settings", json={"queue_end_behavior": "stop"})
        assert not any(
            c.args and c.args[0] == "lyrics_contribute_enabled"
            for c in set_mock.call_args_list
        )

    # Stored "0" reads back as bool false.
    async def _get(key, default=None):
        return "0" if key == "lyrics_contribute_enabled" else None
    with patch("app.database.get_setting", AsyncMock(side_effect=_get)), \
         patch("app.database.set_setting", AsyncMock()):
        assert client.get("/admin/settings").json()["lyrics_contribute_enabled"] is False


async def test_settings_invalid_end_behavior(client, mock_state):
    resp = client.post("/admin/settings", json={"queue_end_behavior": "invalid"})
    assert resp.status_code == 400


# ── Queue-end rework (2026-06-21 plan U2) ────────────────────────────────────

async def test_settings_popular_random_round_trip(client, mock_state):
    with patch("app.database.get_setting", AsyncMock(return_value=None)), \
         patch("app.database.set_setting", AsyncMock()):
        resp = client.post("/admin/settings", json={"queue_end_behavior": "popular_random"})
        assert resp.status_code == 200
        qe, _ = mock_state
        assert qe.end_behavior == QueueEndBehavior.POPULAR_RANDOM


def test_settings_migrates_legacy_behavior_on_read(client, mock_state):
    """AE6: a stored legacy 'shuffle'/'repeat' echoes as a current value."""
    for stored, expected in (("shuffle", "full_random"), ("repeat", "stop")):
        async def fake_get(key, default=None, _v=stored):
            return _v if key == "queue_end_behavior" else default
        with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)):
            body = client.get("/admin/settings").json()
        assert body["queue_end_behavior"] == expected


def test_settings_persists_popular_threshold(client, mock_state):
    with patch("app.api.admin.database.set_setting", AsyncMock()) as ss, \
         patch("app.api.admin.database.get_setting", AsyncMock(return_value=None)):
        resp = client.post("/admin/settings", json={"popular_random_threshold": 5})
    assert resp.status_code == 200
    persisted = {c.args[0]: c.args[1] for c in ss.call_args_list}
    assert persisted.get("popular_random_threshold") == "5"


def test_settings_rejects_threshold_below_one(client, mock_state):
    with patch("app.api.admin.database.set_setting", AsyncMock()) as ss, \
         patch("app.api.admin.database.get_setting", AsyncMock(return_value=None)):
        resp = client.post("/admin/settings", json={"popular_random_threshold": 0})
    assert resp.status_code == 422
    assert "popular_random_threshold" not in {c.args[0] for c in ss.call_args_list}


def test_get_settings_queue_end_defaults(client, mock_state):
    """Unset → threshold default 2, length-limit off."""
    with patch("app.database.get_setting", AsyncMock(return_value=None)):
        body = client.get("/admin/settings").json()
    assert body["popular_random_threshold"] == 2
    assert body["queue_end_length_limit"] is False


def test_settings_persists_most_played_display_limit(client, mock_state):
    with patch("app.api.admin.database.set_setting", AsyncMock()) as ss, \
         patch("app.api.admin.database.get_setting", AsyncMock(return_value=None)):
        resp = client.post("/admin/settings", json={"most_played_display_limit": 25})
    assert resp.status_code == 200
    persisted = {c.args[0]: c.args[1] for c in ss.call_args_list}
    assert persisted.get("most_played_display_limit") == "25"


def test_settings_rejects_display_limit_below_one(client, mock_state):
    with patch("app.api.admin.database.set_setting", AsyncMock()) as ss, \
         patch("app.api.admin.database.get_setting", AsyncMock(return_value=None)):
        resp = client.post("/admin/settings", json={"most_played_display_limit": 0})
    assert resp.status_code == 422
    assert "most_played_display_limit" not in {c.args[0] for c in ss.call_args_list}


def test_get_settings_most_played_display_limit_default(client, mock_state):
    """Unset → display limit default 100."""
    with patch("app.database.get_setting", AsyncMock(return_value=None)):
        body = client.get("/admin/settings").json()
    assert body["most_played_display_limit"] == 100


# ── Closing Time mode (2026-06-24 plan U1) ───────────────────────────────────

def test_settings_persists_closing_time_fields(client, mock_state):
    """All four fields persist: bool as "1", strings trimmed."""
    with patch("app.api.admin.database.set_setting", AsyncMock()) as ss, \
         patch("app.api.admin.database.get_setting", AsyncMock(return_value=None)):
        resp = client.post("/admin/settings", json={
            "closing_time_enabled": True,
            "closing_time_title": "  Last Call  ",
            "closing_time_artist": " The Band ",
            "closing_time_message": "  Drink up.  ",
        })
    assert resp.status_code == 200
    persisted = {c.args[0]: c.args[1] for c in ss.call_args_list}
    assert persisted.get("closing_time_enabled") == "1"
    assert persisted.get("closing_time_title") == "Last Call"
    assert persisted.get("closing_time_artist") == "The Band"
    assert persisted.get("closing_time_message") == "Drink up."


def test_settings_closing_time_disabled_persists_zero(client, mock_state):
    with patch("app.api.admin.database.set_setting", AsyncMock()) as ss, \
         patch("app.api.admin.database.get_setting", AsyncMock(return_value=None)):
        resp = client.post("/admin/settings", json={"closing_time_enabled": False})
    assert resp.status_code == 200
    persisted = {c.args[0]: c.args[1] for c in ss.call_args_list}
    assert persisted.get("closing_time_enabled") == "0"


def test_settings_closing_time_partial_update(client, mock_state):
    """Posting only the toggle does not clobber title/artist/message."""
    with patch("app.api.admin.database.set_setting", AsyncMock()) as ss, \
         patch("app.api.admin.database.get_setting", AsyncMock(return_value=None)):
        resp = client.post("/admin/settings", json={"closing_time_enabled": True})
    assert resp.status_code == 200
    keys = {c.args[0] for c in ss.call_args_list}
    assert "closing_time_enabled" in keys
    assert "closing_time_title" not in keys
    assert "closing_time_artist" not in keys
    assert "closing_time_message" not in keys


def test_get_settings_closing_time_defaults(client, mock_state):
    """Unset → off with the Semisonic defaults."""
    async def fake_get(key, default=None):
        return {}.get(key, default)
    with patch("app.database.get_setting", fake_get):
        body = client.get("/admin/settings").json()
    assert body["closing_time_enabled"] is False
    assert body["closing_time_title"] == "Closing Time"
    assert body["closing_time_artist"] == "Semisonic"
    assert body["closing_time_message"] == "You don't have to go home, but you can't stay here."


def test_get_settings_echoes_closing_time(client, mock_state):
    """GET echoes stored values + enabled true (apostrophes intact)."""
    store = {
        "closing_time_enabled": "1",
        "closing_time_title": "Last Call",
        "closing_time_artist": "The Band",
        "closing_time_message": "You don't have to go home.",
    }
    async def fake_get(key, default=None):
        return store.get(key, default)
    with patch("app.database.get_setting", fake_get):
        body = client.get("/admin/settings").json()
    assert body["closing_time_enabled"] is True
    assert body["closing_time_title"] == "Last Call"
    assert body["closing_time_artist"] == "The Band"
    assert body["closing_time_message"] == "You don't have to go home."


def test_settings_queue_end_length_limit_round_trip(client, mock_state):
    with patch("app.api.admin.database.set_setting", AsyncMock()) as ss, \
         patch("app.api.admin.database.get_setting", AsyncMock(return_value=None)):
        assert client.post(
            "/admin/settings", json={"queue_end_length_limit": True}
        ).status_code == 200
        ss.assert_any_call("queue_end_length_limit", "1")
        client.post("/admin/settings", json={"queue_end_length_limit": False})
        ss.assert_any_call("queue_end_length_limit", "0")
    async def _get(key, default=None):
        return "1" if key == "queue_end_length_limit" else None
    with patch("app.database.get_setting", AsyncMock(side_effect=_get)):
        assert client.get("/admin/settings").json()["queue_end_length_limit"] is True


async def test_get_popular_random_threshold_accessor():
    from app import database
    for raw, expected in (("5", 5), ("0", 1), ("-3", 1), ("abc", 2), (None, 2)):
        with patch("app.database.get_setting", AsyncMock(return_value=raw)):
            assert await database.get_popular_random_threshold() == expected


async def test_get_queue_end_length_limit_accessor():
    from app import database
    for raw, expected in (("1", True), ("0", False), (None, False), ("x", False)):
        with patch("app.database.get_setting", AsyncMock(return_value=raw)):
            assert await database.get_queue_end_length_limit() is expected


# ── On-deck pre-buffer invalidation (2026-06-21 plan U4) ─────────────────────

async def test_settings_change_invalidates_ondeck(client, mock_state):
    """AE4: changing any selection input invalidates the on-deck buffer."""
    with patch("app.database.get_setting", AsyncMock(return_value=None)), \
         patch("app.database.set_setting", AsyncMock()), \
         patch("app.state.invalidate_ondeck", AsyncMock()) as inv:
        for field in (
            {"queue_end_behavior": "full_random"},
            {"popular_random_threshold": 3},
            {"random_min_seconds": 30},
            {"queue_end_length_limit": True},
        ):
            inv.reset_mock()
            resp = client.post("/admin/settings", json=field)
            assert resp.status_code == 200
            inv.assert_awaited_once()


async def test_unrelated_setting_does_not_invalidate_ondeck(client, mock_state):
    with patch("app.database.get_setting", AsyncMock(return_value=None)), \
         patch("app.database.set_setting", AsyncMock()), \
         patch("app.state.invalidate_ondeck", AsyncMock()) as inv:
        resp = client.post("/admin/settings", json={"rail_mode": "vanilla"})
        assert resp.status_code == 200
        inv.assert_not_awaited()


async def test_enable_library_invalidates_ondeck(client, mock_state):
    from types import SimpleNamespace
    fake_client = MagicMock()
    fake_client.get_libraries = AsyncMock(return_value=[SimpleNamespace(key="5", title="Music")])
    fake_client.invalidate_cache = MagicMock()
    with patch("app.state.get_plex_client", AsyncMock(return_value=fake_client)), \
         patch("app.database.toggle_library", AsyncMock()), \
         patch("app.state.invalidate_ondeck", AsyncMock()) as inv:
        resp = client.post("/admin/plex/libraries/5/enable")
        assert resp.status_code == 200
        inv.assert_awaited_once()


async def test_disable_library_invalidates_ondeck(client, mock_state):
    fake_client = MagicMock()
    fake_client.invalidate_cache = MagicMock()
    with patch("app.state.get_plex_client", AsyncMock(return_value=fake_client)), \
         patch("app.database.toggle_library", AsyncMock()), \
         patch("app.state.invalidate_ondeck", AsyncMock()) as inv:
        resp = client.post("/admin/plex/libraries/5/disable")
        assert resp.status_code == 200
        inv.assert_awaited_once()


# ── Browse index rescan / invalidation / status (2026-06-21 plan U6) ─────────

async def test_rescan_triggers_browse_index_refresh(client, mock_state):
    # Covers AE4 (manual Rescan rebuilds the index).
    fake_client = MagicMock()
    fake_client.invalidate_cache = MagicMock()
    fake_client.get_libraries = AsyncMock(return_value=[])
    with patch("app.state.get_plex_client", AsyncMock(return_value=fake_client)), \
         patch("app.database.get_enabled_libraries", AsyncMock(return_value=[])), \
         patch("app.state.trigger_genre_refresh", MagicMock()), \
         patch("app.state.trigger_credit_refresh", MagicMock()), \
         patch("app.state.trigger_browse_index_refresh", MagicMock()) as trig:
        resp = client.post("/admin/plex/rescan")
    assert resp.status_code == 200
    trig.assert_called_once()


async def test_enable_library_triggers_browse_index_refresh(client, mock_state):
    # Covers AE4 (library toggle refreshes the index, R9).
    from types import SimpleNamespace
    fake_client = MagicMock()
    fake_client.get_libraries = AsyncMock(return_value=[SimpleNamespace(key="5", title="Music")])
    fake_client.invalidate_cache = MagicMock()
    with patch("app.state.get_plex_client", AsyncMock(return_value=fake_client)), \
         patch("app.database.toggle_library", AsyncMock()), \
         patch("app.state.invalidate_ondeck", AsyncMock()), \
         patch("app.state.trigger_browse_index_refresh", MagicMock()) as trig:
        resp = client.post("/admin/plex/libraries/5/enable")
    assert resp.status_code == 200
    trig.assert_called_once()


async def test_disable_library_triggers_browse_index_refresh(client, mock_state):
    fake_client = MagicMock()
    fake_client.invalidate_cache = MagicMock()
    with patch("app.state.get_plex_client", AsyncMock(return_value=fake_client)), \
         patch("app.database.toggle_library", AsyncMock()), \
         patch("app.state.invalidate_ondeck", AsyncMock()), \
         patch("app.state.trigger_browse_index_refresh", MagicMock()) as trig:
        resp = client.post("/admin/plex/libraries/5/disable")
    assert resp.status_code == 200
    trig.assert_called_once()


async def test_index_status_reports_stamp(client, mock_state):
    with patch("app.database.get_setting", AsyncMock(return_value="2026-06-21T10:00:00+00:00")), \
         patch("app.state._browse_index_refresh_running", False):
        resp = client.get("/admin/plex/index-status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["computed_at"] == "2026-06-21T10:00:00+00:00"
    assert body["building"] is False


async def test_index_status_never_built(client, mock_state):
    with patch("app.database.get_setting", AsyncMock(return_value=None)):
        resp = client.get("/admin/plex/index-status")
    assert resp.status_code == 200
    assert resp.json()["computed_at"] is None


async def test_set_pattern_rules_triggers_grouping_rebuild(client, mock_state):
    # Rule-grouping plan U2/R6: saving rules rebuilds the grouping map locally
    # (no Plex re-crawl) via the single mutation path.
    with patch("app.database.set_pattern_rules", AsyncMock()) as setrules, \
         patch("app.state.trigger_artist_grouping_rebuild", MagicMock()) as trig:
        resp = client.post("/admin/pattern-rules", json={"rules": [["a", "b"]]})
    assert resp.status_code == 200
    setrules.assert_awaited_once()
    trig.assert_called_once()


# ── Rail mode setting (U1 of rail-mode-toggle plan) ───────────────────────────

async def test_settings_rail_mode_density_write_rejected(client, mock_state):
    """Glow-up plan U1 (R3): density retired - writes 422 at the Literal."""
    resp = client.post("/admin/settings", json={"rail_mode": "density"})
    assert resp.status_code == 422


async def test_settings_legacy_density_reads_as_waveform(client, mock_state):
    """Covers AE6 (admin read edge): stored 'density' hydrates as waveform."""
    async def fake_get(key, default=None):
        return {"rail_mode": "density"}.get(key, default)

    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)):
        get_resp = client.get("/admin/settings")
        assert get_resp.status_code == 200
        assert get_resp.json()["rail_mode"] == "waveform"


async def test_settings_rail_mode_magnetic_persists(client, mock_state):
    """POST /admin/settings with rail_mode='magnetic' persists; GET returns it."""
    store: dict[str, str] = {}

    async def fake_get(key, default=None):
        return store.get(key, default)

    async def fake_set(key, value):
        store[key] = value

    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)), \
         patch("app.database.set_setting", AsyncMock(side_effect=fake_set)):
        resp = client.post("/admin/settings", json={"rail_mode": "magnetic"})
        assert resp.status_code == 200
        assert store["rail_mode"] == "magnetic"
        get_resp = client.get("/admin/settings")
        assert get_resp.json()["rail_mode"] == "magnetic"


async def test_settings_rail_mode_invalid_returns_422(client, mock_state):
    """Pydantic Literal validation rejects unknown rail_mode values with 422."""
    resp = client.post("/admin/settings", json={"rail_mode": "invalid"})
    assert resp.status_code == 422


async def test_settings_rail_mode_default_vanilla(client, mock_state):
    """When the rail_mode key is absent, GET defaults to 'vanilla' (2026-06-09
    rail plan R7 — fresh installs get the plain rail; stored values preserved)."""
    with patch("app.database.get_setting", AsyncMock(return_value=None)):
        resp = client.get("/admin/settings")
        assert resp.status_code == 200
        assert resp.json()["rail_mode"] == "vanilla"


# ── Output ────────────────────────────────────────────────────────────────────

async def test_output_devices_returns_grouped(client, mock_state):
    resp = client.get("/admin/output/devices")
    assert resp.status_code == 200
    assert isinstance(resp.json(), dict)


async def test_set_output_invalid_backend(client, mock_state):
    with patch("app.state.activate_backend", AsyncMock()):
        resp = client.post("/admin/output/active", json={"backend_type": "bluetooth"})
        assert resp.status_code in (400, 422)  # Pydantic Literal rejects unknown values with 422


async def test_set_output_device_not_found_returns_409(client, mock_state):
    """RuntimeError from activate_backend (device not in D-Bus map) yields 409, not 500."""
    with patch("app.state.activate_backend", AsyncMock(side_effect=RuntimeError("device not found"))):
        resp = client.post("/admin/output/active", json={"backend_type": "chromecast", "device_id": "dead-uuid"})
    assert resp.status_code == 409
    assert "device not found" in resp.json()["detail"]


async def test_set_output_library_error_returns_502(client, mock_state):
    """Non-RuntimeError from activate_backend yields 502, not 500."""
    with patch("app.state.activate_backend", AsyncMock(side_effect=OSError("connection refused"))):
        resp = client.post("/admin/output/active", json={"backend_type": "chromecast", "device_id": "dead-uuid"})
    assert resp.status_code == 502


async def test_set_output_device_id_too_long_returns_422(client, mock_state):
    """device_id longer than 128 chars is rejected with 422."""
    resp = client.post("/admin/output/active", json={"backend_type": "chromecast", "device_id": "x" * 129})
    assert resp.status_code == 422


async def test_get_output_active_includes_mdns_status(client, mock_state):
    """GET /admin/output/active response includes mdns_status field."""
    with patch("app.database.get_setting", AsyncMock(return_value=None)):
        resp = client.get("/admin/output/active")
    assert resp.status_code == 200
    assert "mdns_status" in resp.json()


# ── Admin queue append bypasses lock (U2) ─────────────────────────────────────

def _make_plex_client(tracks=None):
    pc = MagicMock()
    pc.get_track = AsyncMock(return_value=make_track("t1"))
    pc.get_libraries = AsyncMock(return_value=[Library(key="1", title="Music", type="artist", server_name="Music")])
    pc.get_tracks = AsyncMock(return_value=tracks or [make_track("t1"), make_track("t2")])
    # Mocks for _resolve_album_tracks chain (U1 of the unification plan): the
    # endpoint now calls get_album → get_artists → get_albums → get_tracks for
    # the album branch. Defaults align so the single-library happy path resolves.
    pc.get_album = AsyncMock(return_value=make_album())
    pc.get_artists = AsyncMock(return_value=[make_artist()])
    pc.get_albums = AsyncMock(return_value=[make_album()])
    pc.invalidate_cache = MagicMock()
    return pc


def _two_libs():
    """Library 0 (host, machineA) + Library 1 (shared, machineB) with server_names set."""
    return [
        Library(key="machineA:1", title="Host", type="artist", server_name="Host"),
        Library(key="machineB:2", title="Shared", type="artist", server_name="Shared"),
    ]


def _wire_two_library_album(plex, *, host_tracks, shared_tracks):
    """Wire a plex mock so 'Album A' by 'Prince' exists in both _two_libs() libraries
    with the given per-library track lists. Mirrors tests/test_api_guest.py's pattern."""
    plex.get_album = AsyncMock(return_value=Album(
        id="machineA:100", title="Album A", artist="Prince",
    ))

    async def fake_get_artists(section_key):
        if section_key == "machineA:1":
            return [Artist(id="machineA:42", title="Prince")]
        if section_key == "machineB:2":
            return [Artist(id="machineB:78", title="Prince")]
        return []

    async def fake_get_albums(section_key, artist_id=None, **kw):
        if section_key == "machineA:1":
            return [Album(id="machineA:100", title="Album A", artist="Prince")]
        if section_key == "machineB:2":
            return [Album(id="machineB:200", title="Album A", artist="Prince")]
        return []

    async def fake_get_tracks(section_key, album_id=None, **kw):
        if section_key == "machineA:1" and album_id == "machineA:100":
            return host_tracks
        if section_key == "machineB:2" and album_id == "machineB:200":
            return shared_tracks
        return []

    plex.get_artists.side_effect = fake_get_artists
    plex.get_albums.side_effect = fake_get_albums
    plex.get_tracks.side_effect = fake_get_tracks


async def test_admin_append_track_bypasses_lock(client, mock_state):
    qe, _ = mock_state
    await qe.lock()
    plex = _make_plex_client()
    with patch("app.state.get_plex_client", AsyncMock(return_value=plex)), \
         patch("app.database.get_enabled_libraries", AsyncMock(return_value=[{"section_key": "1"}])):
        resp = client.post("/admin/queue", json={"track_id": "t1"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert len(qe.queue) == 1


async def test_admin_append_duplicate_exempt_from_flood_control(client, mock_state):
    """Flood Control (2026-06-16 plan U2): the admin add path never consults
    flood_control. With the toggle on AND the track already in the queue, the
    admin add still succeeds (admin can always add duplicates, mirroring how
    admin bypasses the queue lock). Guests-only is the whole point of the gate."""
    qe, _ = mock_state
    await qe.append(make_track("t1"), bypass_lock=True)  # t1 already queued
    plex = _make_plex_client()
    with patch("app.state.get_plex_client", AsyncMock(return_value=plex)), \
         patch("app.database.get_enabled_libraries", AsyncMock(return_value=[{"section_key": "1"}])), \
         patch("app.database.get_setting", AsyncMock(return_value="1")):  # FC ON
        resp = client.post("/admin/queue", json={"track_id": "t1"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert len(qe.queue) == 2  # duplicate added anyway


async def test_admin_append_album_bypasses_lock(client, mock_state):
    qe, _ = mock_state
    await qe.lock()
    plex = _make_plex_client(tracks=[make_track("t1"), make_track("t2")])
    with patch("app.state.get_plex_client", AsyncMock(return_value=plex)), \
         patch("app.database.get_enabled_libraries", AsyncMock(return_value=[{"section_key": "1"}])):
        resp = client.post("/admin/queue", json={"album_id": "a1"})
    assert resp.status_code == 200
    assert resp.json()["tracks_added"] == 2
    assert len(qe.queue) == 2


async def test_admin_append_no_id_returns_400(client, mock_state):
    plex = _make_plex_client()
    with patch("app.state.get_plex_client", AsyncMock(return_value=plex)):
        resp = client.post("/admin/queue", json={})
    assert resp.status_code == 400


async def test_admin_append_no_plex_returns_503(client, mock_state):
    resp = client.post("/admin/queue", json={"track_id": "t1"})
    assert resp.status_code == 503


# ── Admin queue source_server_name filter (U1 of unification plan) ────────────


async def test_admin_append_album_no_source_filter_unions_libraries(client, mock_state):
    """Backward-compat: POST {album_id} with no source_server_name enqueues
    the union of all matching libraries' tracks (post-multi-library behavior
    matching what guest already does on /api/queue)."""
    qe, _ = mock_state
    plex = _make_plex_client()
    _wire_two_library_album(
        plex,
        host_tracks=[make_track("machineA:t1"), make_track("machineA:t2")],
        shared_tracks=[make_track("machineB:t1")],
    )
    with patch("app.state.get_plex_client", AsyncMock(return_value=plex)), \
         patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        resp = client.post("/admin/queue", json={"album_id": "machineA:100"})
    assert resp.status_code == 200
    assert resp.json()["tracks_added"] == 3
    assert len(qe.queue) == 3


async def test_admin_append_album_with_source_filter_scopes_to_one_library(client, mock_state):
    """Covers AE2 (admin half). source_server_name='Shared' enqueues only
    Shared library's tracks; tracks_added reflects the filtered count."""
    qe, _ = mock_state
    plex = _make_plex_client()
    _wire_two_library_album(
        plex,
        host_tracks=[make_track("machineA:t1"), make_track("machineA:t2")],
        shared_tracks=[make_track("machineB:t1")],
    )
    with patch("app.state.get_plex_client", AsyncMock(return_value=plex)), \
         patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        resp = client.post("/admin/queue", json={
            "album_id": "machineA:100",
            "source_server_name": "Shared",
        })
    assert resp.status_code == 200
    assert resp.json()["tracks_added"] == 1
    assert len(qe.queue) == 1
    assert qe.queue[0].track.id == "machineB:t1"


async def test_admin_append_album_with_source_filter_includes_bonus_tracks(client, mock_state):
    """Covers AE3 (admin half). Host=2 tracks, Shared=3 tracks with bonus.
    Filtering to 'Shared' enqueues all 3; no Host tracks queued."""
    qe, _ = mock_state
    plex = _make_plex_client()
    _wire_two_library_album(
        plex,
        host_tracks=[make_track("machineA:t1"), make_track("machineA:t2")],
        shared_tracks=[
            make_track("machineB:t1"),
            make_track("machineB:t2"),
            make_track("machineB:bonus"),
        ],
    )
    with patch("app.state.get_plex_client", AsyncMock(return_value=plex)), \
         patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        resp = client.post("/admin/queue", json={
            "album_id": "machineA:100",
            "source_server_name": "Shared",
        })
    assert resp.status_code == 200
    assert resp.json()["tracks_added"] == 3
    queued_ids = [i.track.id for i in qe.queue]
    assert "machineB:bonus" in queued_ids
    assert all(tid.startswith("machineB:") for tid in queued_ids)


async def test_admin_append_track_ignores_source_server_name(client, mock_state):
    """Track branch: source_server_name is accepted but ignored (track_id
    already identifies the library; admin still bypasses the lock)."""
    qe, _ = mock_state
    plex = _make_plex_client()
    with patch("app.state.get_plex_client", AsyncMock(return_value=plex)):
        resp = client.post("/admin/queue", json={
            "track_id": "t1",
            "source_server_name": "Anything",
        })
    assert resp.status_code == 200
    assert resp.json()["tracks_added"] == 1
    assert len(qe.queue) == 1


async def test_admin_append_album_source_filter_no_match_returns_404(client, mock_state):
    """source_server_name doesn't match any enabled library → 404."""
    plex = _make_plex_client()
    _wire_two_library_album(
        plex,
        host_tracks=[make_track("machineA:t1")],
        shared_tracks=[make_track("machineB:t1")],
    )
    with patch("app.state.get_plex_client", AsyncMock(return_value=plex)), \
         patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        resp = client.post("/admin/queue", json={
            "album_id": "machineA:100",
            "source_server_name": "Nonexistent",
        })
    assert resp.status_code == 404


async def test_admin_append_album_source_filter_case_insensitive(client, mock_state):
    """source_server_name match is case-insensitive after trim."""
    qe, _ = mock_state
    plex = _make_plex_client()
    _wire_two_library_album(
        plex,
        host_tracks=[make_track("machineA:t1")],
        shared_tracks=[make_track("machineB:t1")],
    )
    with patch("app.state.get_plex_client", AsyncMock(return_value=plex)), \
         patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        resp = client.post("/admin/queue", json={
            "album_id": "machineA:100",
            "source_server_name": "host",
        })
    assert resp.status_code == 200
    assert qe.queue[0].track.id == "machineA:t1"


async def test_admin_append_album_get_album_keyerror_returns_404(client, mock_state):
    """get_album raising KeyError → 404 (matches /api/queue's behavior)."""
    plex = _make_plex_client()
    plex.get_album = AsyncMock(side_effect=KeyError("not found"))
    with patch("app.state.get_plex_client", AsyncMock(return_value=plex)), \
         patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        resp = client.post("/admin/queue", json={
            "album_id": "machineA:9999",
            "source_server_name": "Host",
        })
    assert resp.status_code == 404


# ── Backend parity: /api/queue vs /admin/queue equivalence ────────────────────


async def test_guest_admin_queue_endpoints_parity(client, mock_state):
    """Both endpoints accept the same request body shape and produce equivalent
    observable behavior (same tracks_added, same enqueued track IDs) for an
    album with source_server_name. Catches future endpoint divergence that the
    unified frontend would otherwise mask."""
    qe, _ = mock_state
    plex = _make_plex_client()
    _wire_two_library_album(
        plex,
        host_tracks=[make_track("machineA:t1")],
        shared_tracks=[make_track("machineB:t1")],
    )

    # Round 1: POST to /admin/queue.
    with patch("app.state.get_plex_client", AsyncMock(return_value=plex)), \
         patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        admin_resp = client.post("/admin/queue", json={
            "album_id": "machineA:100",
            "source_server_name": "Shared",
        })
    admin_queued = [i.track.id for i in qe.queue]
    admin_count = admin_resp.json()["tracks_added"]

    # Reset queue for guest round.
    while qe.queue:
        await qe.remove(0)
    # Re-wire mocks (gather() consumed side_effect iterators in some versions —
    # the AsyncMock side_effects defined as `async def` callables are idempotent,
    # so this is a no-op; explicit re-set is defensive).
    _wire_two_library_album(
        plex,
        host_tracks=[make_track("machineA:t1")],
        shared_tracks=[make_track("machineB:t1")],
    )

    # Round 2: POST same body to /api/queue.
    with patch("app.state.get_plex_client", AsyncMock(return_value=plex)), \
         patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        guest_resp = client.post("/api/queue", json={
            "album_id": "machineA:100",
            "source_server_name": "Shared",
        })
    guest_queued = [i.track.id for i in qe.queue]
    guest_count = guest_resp.json()["tracks_added"]

    assert admin_resp.status_code == 200
    assert guest_resp.status_code == 200
    assert admin_count == guest_count == 1
    assert admin_queued == guest_queued == ["machineB:t1"]


# ── Plex rescan (U3) ──────────────────────────────────────────────────────────

async def test_plex_rescan_returns_library_list(client, mock_state):
    plex = _make_plex_client()
    with patch("app.state.get_plex_client", AsyncMock(return_value=plex)), \
         patch("app.database.get_enabled_libraries", AsyncMock(return_value=[{"section_key": "1"}])):
        resp = client.post("/admin/plex/rescan")
    assert resp.status_code == 200
    plex.invalidate_cache.assert_called_once()
    data = resp.json()
    assert isinstance(data, list)
    assert data[0]["key"] == "1"


async def test_plex_rescan_no_plex_returns_503(client, mock_state):
    resp = client.post("/admin/plex/rescan")
    assert resp.status_code == 503


# ── Discovery serving model (U5: registry snapshot vs legacy pull) ───────────
# KTD7 removed the route's 30s HTTP cache: while the watcher runs, GET
# serves the live registry snapshot (no network I/O); without it, the
# legacy pull flow scans on every call. The fixtures below install a
# running watcher singleton the way the lifespan would — conftest's
# mock_state resets the singleton between tests.


def _install_running_watcher(mdns=None):
    """A DeviceWatcher singleton with running=True for route tests.

    Subscription handles are faked (no avahi here) and the injected timer
    swallows the debounce scheduling, so the snapshot/reconcile path runs
    without live timers or the websocket manager."""
    import app.output.watcher as watcher_module
    w = watcher_module.DeviceWatcher(timer=lambda delay, cb: MagicMock())
    w._handles = {"airplay": object(), "chromecast": object()}
    if mdns:
        w._mdns_status.update(mdns)
    watcher_module._watcher = w
    return w


def _entry(device, online=True, offline_since=None):
    from app.output.watcher import RegistryEntry
    return RegistryEntry(device=device, online=online,
                         offline_since=offline_since)


def _airplay_device(host="10.0.0.9", port=7000, name="WiiM"):
    from app.output.base import OutputDevice
    return OutputDevice(id=f"{host}:{port}", name=name,
                        backend_type="airplay", id_format="host_port")


def _stub_airplay(*devices):
    """Backend stub shaped like AirPlayBackend's address cache: host_for
    resolves through _device_addr; the one-shot returns nothing unless a
    test overrides discover_devices."""
    ab = MagicMock()
    ab._device_addr = {
        d.id: (d.name, d.id.rsplit(":", 1)[0], int(d.id.rsplit(":", 1)[1]), {})
        for d in devices
    }
    ab.discover_devices = AsyncMock(return_value=[])
    return ab


async def test_legacy_pull_scans_on_every_get(client, mock_state):
    """No watcher (or degraded) → the legacy pull flow runs the one-shot
    discovers on EVERY call; the old 30s response cache is gone (KTD7 —
    while the watcher runs the registry IS the cache)."""
    scan_count = []

    async def counting_discover():
        scan_count.append(1)
        return []

    with patch("app.state.direct_backend") as db, \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.dlna_backend") as lb, \
         patch("app.state.airplay_backend") as ab:
        db.discover_devices = counting_discover
        cb.discover_devices = counting_discover
        lb.discover_devices = counting_discover
        ab.discover_devices = counting_discover

        client.get("/admin/output/devices")
        client.get("/admin/output/devices")

    assert len(scan_count) == 8, \
        f"Expected 8 scan calls (2 GETs × 4 backends, no cache), got {len(scan_count)}"


async def test_degraded_watcher_falls_back_to_legacy_pull(client, mock_state):
    """A watcher that exists but isn't running (avahi absent → degraded
    mode) must not capture the route: the legacy pull flow serves,
    byte-compatible with the no-watcher path (origin R5)."""
    import app.output.watcher as watcher_module
    watcher_module._watcher = watcher_module.DeviceWatcher(
        timer=lambda delay, cb: MagicMock())  # no handles → running=False

    scan_count = []

    async def counting_discover():
        scan_count.append(1)
        return []

    with patch("app.state.direct_backend") as db, \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.dlna_backend") as lb, \
         patch("app.state.airplay_backend") as ab, \
         patch("app.state.shared_aiozc", object()):  # in-process mDNS bound
        db.discover_devices = counting_discover
        cb.discover_devices = counting_discover
        lb.discover_devices = counting_discover
        ab.discover_devices = counting_discover
        resp = client.get("/admin/output/devices")

    assert len(scan_count) == 4
    payload = resp.json()
    assert payload["devices"] == []
    assert payload["mdns_status"] == {
        "direct": "ok", "airplay": "ok", "chromecast": "ok", "dlna": "ok",
        "discovery": "ok"}


async def test_snapshot_path_serves_registry_without_discover(client, mock_state):
    """KTD7 fast path: watcher running → GET serves the registry snapshot
    with NO one-shot discovers, and mdns_status comes from the watcher."""
    dev = _airplay_device()
    watcher = _install_running_watcher(mdns={"chromecast": "unavailable"})
    watcher.registry[("airplay", dev.id)] = _entry(dev)

    with patch("app.state.airplay_backend", _stub_airplay(dev)) as ab, \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.dlna_backend") as lb:
        cb.discover_devices = AsyncMock(return_value=[])
        lb.discover_devices = AsyncMock(return_value=[])
        resp = client.get("/admin/output/devices")

    assert ab.discover_devices.await_count == 0
    assert cb.discover_devices.await_count == 0
    assert lb.discover_devices.await_count == 0
    payload = resp.json()
    assert [d["host"] for d in payload["devices"]] == ["10.0.0.9"]
    assert payload["devices"][0]["online"] is True
    assert payload["devices"][0]["protocols"][0]["device_id"] == dev.id
    assert payload["mdns_status"]["chromecast"] == "unavailable"
    assert payload["mdns_status"]["airplay"] == "ok"


async def test_snapshot_path_offline_entry_present_with_flag(client, mock_state):
    """An offline-retained registry ghost stays IN the GET payload,
    flagged online=False with its offline_since stamp — greyed out in
    the picker, never vanishing (origin R2)."""
    dev = _airplay_device()
    watcher = _install_running_watcher()
    watcher.registry[("airplay", dev.id)] = _entry(
        dev, online=False, offline_since=1234.5)

    with patch("app.state.airplay_backend", _stub_airplay(dev)):
        resp = client.get("/admin/output/devices")

    devices = resp.json()["devices"]
    assert len(devices) == 1
    assert devices[0]["online"] is False
    assert devices[0]["offline_since"] == 1234.5


async def test_force_scan_drops_still_absent_offline_ghosts(client, mock_state):
    """Scan (?bust=1) is the only eviction path (origin R6): an offline
    ghost the one-shots still can't see is dropped from the registry and
    the response."""
    dev = _airplay_device()
    watcher = _install_running_watcher()
    watcher.registry[("airplay", dev.id)] = _entry(
        dev, online=False, offline_since=1234.5)

    with patch("app.state.airplay_backend", _stub_airplay(dev)), \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.dlna_backend") as lb:
        cb.snapshot_devices = MagicMock(return_value=[])
        lb.discover_devices = AsyncMock(return_value=[])
        resp = client.get("/admin/output/devices?bust=1")

    assert ("airplay", dev.id) not in watcher.registry
    assert resp.json()["devices"] == []


async def test_force_scan_keeps_reappeared_ghost_online(client, mock_state):
    """Scan finding a retained ghost again flips it back online instead
    of dropping it — reconcile upserts found devices."""
    dev = _airplay_device()
    watcher = _install_running_watcher()
    watcher.registry[("airplay", dev.id)] = _entry(
        dev, online=False, offline_since=1234.5)
    ab = _stub_airplay(dev)
    ab.discover_devices = AsyncMock(return_value=[dev])

    with patch("app.state.airplay_backend", ab), \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.dlna_backend") as lb:
        cb.snapshot_devices = MagicMock(return_value=[])
        lb.discover_devices = AsyncMock(return_value=[])
        resp = client.get("/admin/output/devices?bust=1")

    entry = watcher.registry[("airplay", dev.id)]
    assert entry.online is True
    assert entry.offline_since is None
    devices = resp.json()["devices"]
    assert devices[0]["online"] is True
    assert devices[0]["offline_since"] is None


async def test_force_scan_uses_chromecast_snapshot_when_5353_bound(client, mock_state):
    """With 5353 bound (in-process source live), Scan's chromecast one-shot
    reconciles from the live CastBrowser snapshot (uuid-keyed), NOT
    discover_devices and NOT the D-Bus fallback."""
    _install_running_watcher()

    with patch("app.state.airplay_backend") as ab, \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.dlna_backend") as lb, \
         patch("app.state._mdns_port_unavailable", False):
        ab.discover_devices = AsyncMock(return_value=[])
        ab._device_addr = {}
        cb.snapshot_devices = MagicMock(return_value=[])
        cb._dbus_discover = AsyncMock(return_value=[])
        cb.discover_devices = AsyncMock(return_value=[])
        lb.discover_devices = AsyncMock(return_value=[])
        client.get("/admin/output/devices?bust=1")

    assert cb.snapshot_devices.call_count == 1
    assert cb._dbus_discover.await_count == 0


async def test_force_scan_uses_chromecast_dbus_when_degraded(client, mock_state):
    """When 5353 is unavailable (host avahi owns it), Scan's chromecast one-shot
    uses the avahi/D-Bus fallback (_dbus_discover), not the CastBrowser snapshot."""
    _install_running_watcher()

    with patch("app.state.airplay_backend") as ab, \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.dlna_backend") as lb, \
         patch("app.state._mdns_port_unavailable", True):
        ab.discover_devices = AsyncMock(return_value=[])
        ab._device_addr = {}
        cb._dbus_discover = AsyncMock(return_value=[])
        cb.snapshot_devices = MagicMock(return_value=[])
        cb.discover_devices = AsyncMock(return_value=[])
        lb.discover_devices = AsyncMock(return_value=[])
        client.get("/admin/output/devices?bust=1")

    assert cb._dbus_discover.await_count == 1
    assert cb.snapshot_devices.call_count == 0
    assert cb.discover_devices.await_count == 0


async def test_force_scan_never_evicts_scan_missed_online_device_ktd9(
        client, mock_state):
    """KTD9: a device the live subscription says is online survives a
    one-shot window that missed it — entry retained AND its AirPlay
    address still resolves (the merged _device_addr was not cleared), so
    the device stays in the payload with its host."""
    dev = _airplay_device()
    watcher = _install_running_watcher()
    watcher.registry[("airplay", dev.id)] = _entry(dev)  # online
    ab = _stub_airplay(dev)  # one-shot returns [] — a missed window

    with patch("app.state.airplay_backend", ab), \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.dlna_backend") as lb:
        cb.snapshot_devices = MagicMock(return_value=[])
        lb.discover_devices = AsyncMock(return_value=[])
        resp = client.get("/admin/output/devices?bust=1")

    assert ("airplay", dev.id) in watcher.registry
    assert watcher.registry[("airplay", dev.id)].online is True
    devices = resp.json()["devices"]
    assert [d["host"] for d in devices] == ["10.0.0.9"]
    assert dev.id in ab._device_addr


async def test_force_scan_avahi_down_reconciles_dlna_only_ae4(client, mock_state):
    """AE4: avahi outage (watcher mdns_status unavailable) → Scan still
    runs and reconciles the DLNA one-shot, but the mDNS one-shots' empty
    results are NOT treated as evidence — offline mDNS ghosts survive."""
    from app.output.base import OutputDevice
    ghost = _airplay_device()
    dlna_dev = OutputDevice(id="uuid:dlna-1", name="Renderer",
                            backend_type="dlna")
    watcher = _install_running_watcher(
        mdns={"airplay": "unavailable", "chromecast": "unavailable"})
    watcher.registry[("airplay", ghost.id)] = _entry(
        ghost, online=False, offline_since=99.0)

    with patch("app.state.airplay_backend", _stub_airplay(ghost)), \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.dlna_backend") as lb:
        # avahi down: the D-Bus one-shots return [] without raising.
        cb.snapshot_devices = MagicMock(return_value=[])
        lb.discover_devices = AsyncMock(return_value=[dlna_dev])
        lb._device_locations = {"uuid:dlna-1": "http://10.0.0.77:49152/desc.xml"}
        resp = client.get("/admin/output/devices?bust=1")

    # DLNA reconciled in, mDNS ghost untouched.
    assert watcher.registry[("dlna", "uuid:dlna-1")].online is True
    assert ("airplay", ghost.id) in watcher.registry
    by_host = {d["host"]: d for d in resp.json()["devices"]}
    assert by_host["10.0.0.77"]["online"] is True
    assert by_host["10.0.0.9"]["online"] is False
    assert resp.json()["mdns_status"]["airplay"] == "unavailable"


async def test_force_scan_failed_one_shot_keeps_ghosts_and_flags_status(
        client, mock_state):
    """A one-shot that RAISES is excluded from the reconcile (its ghosts
    survive) and overlays 'unavailable' on the response's mdns_status
    even while the watcher itself still reports the bus as ok."""
    ghost = _airplay_device()
    watcher = _install_running_watcher()  # watcher view: all ok
    watcher.registry[("airplay", ghost.id)] = _entry(
        ghost, online=False, offline_since=99.0)
    ab = _stub_airplay(ghost)
    ab.discover_devices = AsyncMock(side_effect=RuntimeError("scan failed"))

    with patch("app.state.airplay_backend", ab), \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.dlna_backend") as lb:
        cb.snapshot_devices = MagicMock(return_value=[])
        lb.discover_devices = AsyncMock(return_value=[])
        resp = client.get("/admin/output/devices?bust=1")

    assert ("airplay", ghost.id) in watcher.registry
    assert resp.json()["mdns_status"]["airplay"] == "unavailable"


async def test_get_output_active_mdns_status_reads_watcher(client, mock_state):
    """/output/active sources mdns_status from the running watcher (the
    old answer read the deleted route cache); without one it defaults to
    all-ok (legacy)."""
    with patch("app.database.get_setting", AsyncMock(return_value=None)), \
         patch("app.state.shared_aiozc", object()):  # in-process mDNS bound
        resp = client.get("/admin/output/active")
        assert resp.json()["mdns_status"] == {
            "direct": "ok", "airplay": "ok", "chromecast": "ok", "dlna": "ok",
            "discovery": "ok"}

        _install_running_watcher(mdns={"airplay": "unavailable"})
        resp = client.get("/admin/output/active")
        assert resp.json()["mdns_status"]["airplay"] == "unavailable"


# ── mdns_status and ?bust=1 (U5) ─────────────────────────────────────────────

def _with_mdns_state(*, port_unavailable, cc_ok=False, ap_ok=False, dbus_reachable=None):
    """Patch the mDNS port-availability flag. The _dbus_* flags were retired
    in plan U7 (no avahi/D-Bus fallback); the extra kwargs are accepted for
    call-site compatibility but ignored."""
    import app.state as st
    return patch.object(st, "_mdns_port_unavailable", port_unavailable)


async def test_mdns_status_all_ok_when_every_backend_returns(client, mock_state):
    """Every backend's discover_devices completes without raising →
    per-backend mdns_status is all 'ok'. Zero devices returned is NOT
    an unavailability — an empty network is just empty."""
    with patch("app.state.direct_backend") as db, \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.dlna_backend") as lb, \
         patch("app.state.airplay_backend") as ab, \
         patch("app.state.shared_aiozc", object()):  # in-process mDNS bound
        db.discover_devices = AsyncMock(return_value=[])
        cb.discover_devices = AsyncMock(return_value=[])
        lb.discover_devices = AsyncMock(return_value=[])
        ab.discover_devices = AsyncMock(return_value=[])
        resp = client.get("/admin/output/devices")

    status = resp.json()["mdns_status"]
    assert status == {"direct": "ok", "airplay": "ok", "chromecast": "ok", "dlna": "ok",
                      "discovery": "ok"}


async def test_devices_payload_flags_discovery_unavailable_when_degraded(
        client, mock_state):
    """U6 AE5: no in-process mDNS (shared_aiozc None, watcher degraded) → the
    devices payload's mdns_status.discovery is 'unavailable', which drives the
    host-networking banner instead of a silent empty menu."""
    with patch("app.state.direct_backend") as db, \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.dlna_backend") as lb, \
         patch("app.state.airplay_backend") as ab, \
         patch("app.state.shared_aiozc", None):  # 5353 never bound
        for m in (db, cb, lb, ab):
            m.discover_devices = AsyncMock(return_value=[])
        resp = client.get("/admin/output/devices")

    assert resp.json()["mdns_status"]["discovery"] == "unavailable"


async def test_devices_payload_discovery_ok_when_watcher_running(client, mock_state):
    """U6: a running watcher with live subscription handles reports in_process
    'ok' (healthy discovery → no banner)."""
    _install_running_watcher()  # handles present → running
    resp = client.get("/admin/output/devices")
    assert resp.json()["mdns_status"]["discovery"] == "ok"


async def test_mdns_status_chromecast_unavailable_when_scan_raises(client, mock_state):
    """A backend whose discover_devices() raises → its slot reports
    'unavailable'; the other backends are unaffected. The frontend
    surfaces this through the partial-coverage banner."""
    with patch("app.state.direct_backend") as db, \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.dlna_backend") as lb, \
         patch("app.state.airplay_backend") as ab:
        db.discover_devices = AsyncMock(return_value=[])
        cb.discover_devices = AsyncMock(side_effect=RuntimeError("scan failed"))
        lb.discover_devices = AsyncMock(return_value=[])
        ab.discover_devices = AsyncMock(return_value=[])
        resp = client.get("/admin/output/devices")

    status = resp.json()["mdns_status"]
    assert status["chromecast"] == "unavailable"
    assert status["direct"] == "ok"
    assert status["airplay"] == "ok"
    assert status["dlna"] == "ok"


async def test_mdns_status_multiple_unavailable_when_multiple_raise(client, mock_state):
    """Multiple backends failing → each reports 'unavailable' independently.
    The frontend banner concatenates the names."""
    with patch("app.state.direct_backend") as db, \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.dlna_backend") as lb, \
         patch("app.state.airplay_backend") as ab:
        db.discover_devices = AsyncMock(return_value=[])
        cb.discover_devices = AsyncMock(side_effect=RuntimeError("cc failed"))
        lb.discover_devices = AsyncMock(side_effect=RuntimeError("dlna failed"))
        ab.discover_devices = AsyncMock(return_value=[])
        resp = client.get("/admin/output/devices")

    status = resp.json()["mdns_status"]
    assert status["chromecast"] == "unavailable"
    assert status["dlna"] == "unavailable"
    assert status["airplay"] == "ok"
    assert status["direct"] == "ok"


async def test_mdns_status_backend_missing_reports_unavailable(client, mock_state):
    """A backend that isn't wired up in state (None) reports 'unavailable'
    rather than crashing the discovery route."""
    with patch("app.state.direct_backend") as db, \
         patch("app.state.chromecast_backend", None), \
         patch("app.state.dlna_backend") as lb, \
         patch("app.state.airplay_backend") as ab:
        db.discover_devices = AsyncMock(return_value=[])
        lb.discover_devices = AsyncMock(return_value=[])
        ab.discover_devices = AsyncMock(return_value=[])
        resp = client.get("/admin/output/devices")

    status = resp.json()["mdns_status"]
    assert status["chromecast"] == "unavailable"


async def test_bust_param_scans_on_legacy_path(client, mock_state):
    """?bust=1 without a running watcher runs the legacy forced scan
    (one one-shot per backend, fresh response — no cache to bypass)."""
    scan_count = []

    async def counting_discover():
        scan_count.append(1)
        return []

    with _with_mdns_state(port_unavailable=False, cc_ok=False, ap_ok=False), \
         patch("app.state.direct_backend") as db, \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.dlna_backend") as lb, \
         patch("app.state.airplay_backend") as ab:
        db.discover_devices = counting_discover
        cb.discover_devices = counting_discover
        lb.discover_devices = counting_discover
        ab.discover_devices = counting_discover
        client.get("/admin/output/devices?bust=1")

    assert len(scan_count) == 4, "bust=1 must run every backend's one-shot"


# ── U5: aggregated payload + per-device Via persistence ──────────────────────


async def test_output_devices_returns_flat_aggregated_list(client, mock_state):
    """Response shape: `devices` is a flat list of per-physical-device
    records (host, name, protocols) — not the old per-backend dict.
    Each protocol entry carries the verified state for the picker."""
    from app.output.base import OutputDevice

    with patch("app.state.direct_backend") as db, \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.dlna_backend") as lb, \
         patch("app.state.airplay_backend") as ab:
        db.discover_devices = AsyncMock(return_value=[
            OutputDevice(id="default", name="System Audio", backend_type="direct"),
        ])
        cb.discover_devices = AsyncMock(return_value=[])
        lb.discover_devices = AsyncMock(return_value=[])
        ab.discover_devices = AsyncMock(return_value=[])
        resp = client.get("/admin/output/devices")

    payload = resp.json()
    assert isinstance(payload["devices"], list)
    assert len(payload["devices"]) == 1
    direct = payload["devices"][0]
    assert direct["host"] == "__direct__"
    assert direct["name"] == "System Audio"
    assert direct["protocols"][0]["backend"] == "direct"
    assert direct["protocols"][0]["verified"] is True
    # KTD8: the payload carries availability; on the legacy pull path
    # every discovered device is online with no offline stamp.
    assert direct["online"] is True
    assert direct["offline_since"] is None


async def test_output_devices_bust_does_not_clear_verdict_cache(client, mock_state):
    """bust=true RE-PROBES (overwriting verdicts as new probes complete)
    but does NOT call clear_all_verdicts.

    The original design cleared first so re-discovered devices got fresh
    probes against the current network state. In practice it produced a
    stuck-"Checking…" loop: bust cleared verdicts, response returned
    immediately with everything verified=None, probes wrote new verdicts
    async — but the frontend never re-fetched, so the operator stayed
    looking at Checking… across every rescan click. The right invariant
    is 'overwrite on probe completion', not 'wipe-then-re-probe'."""
    clear_mock = AsyncMock()
    with patch("app.output.probe_cache.clear_all_verdicts", clear_mock), \
         patch("app.output.probe_cache.fetch_all", AsyncMock(return_value={})), \
         patch("app.state.direct_backend") as db, \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.dlna_backend") as lb, \
         patch("app.state.airplay_backend") as ab:
        db.discover_devices = AsyncMock(return_value=[])
        cb.discover_devices = AsyncMock(return_value=[])
        lb.discover_devices = AsyncMock(return_value=[])
        ab.discover_devices = AsyncMock(return_value=[])
        client.get("/admin/output/devices?bust=1")

    clear_mock.assert_not_awaited()


async def test_output_devices_no_bust_does_not_clear_cache(client, mock_state):
    """Without bust=true, verdicts persist across the scan (the picker
    relies on this for 'verified across sessions')."""
    clear_mock = AsyncMock()
    with patch("app.output.probe_cache.clear_all_verdicts", clear_mock), \
         patch("app.output.probe_cache.fetch_all", AsyncMock(return_value={})), \
         patch("app.state.direct_backend") as db, \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.dlna_backend") as lb, \
         patch("app.state.airplay_backend") as ab:
        db.discover_devices = AsyncMock(return_value=[])
        cb.discover_devices = AsyncMock(return_value=[])
        lb.discover_devices = AsyncMock(return_value=[])
        ab.discover_devices = AsyncMock(return_value=[])
        client.get("/admin/output/devices")

    clear_mock.assert_not_awaited()


async def test_output_devices_bust_reschedules_every_entry(client, mock_state):
    """bust=true probes every (host, backend) regardless of current verdict.
    The non-bust default only probes entries with verified=None (so
    already-verified protocols don't re-probe on every page load)."""
    import asyncio
    from app.output.base import OutputDevice
    from app.output.probe_cache import Verdict

    # Existing verdict for AirPlay on the device — bust should still
    # re-probe it; non-bust should skip it.
    verdicts = {("192.168.1.50", "airplay"): Verdict(ok=True, checked_at=1.0)}

    probed_targets = []

    async def fake_probe(self, device_id):
        probed_targets.append((self.backend_name, device_id))
        return True

    with patch("app.output.probe_cache.fetch_all", AsyncMock(return_value=verdicts)), \
         patch("app.state.direct_backend") as db, \
         patch("app.state.dlna_backend") as lb, \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.airplay_backend") as ab:
        db.discover_devices = AsyncMock(return_value=[])
        lb.discover_devices = AsyncMock(return_value=[])
        cb.discover_devices = AsyncMock(return_value=[])
        ab.discover_devices = AsyncMock(return_value=[
            OutputDevice(id="192.168.1.50:7000", name="WiiM",
                         backend_type="airplay", id_format="host_port"),
        ])
        ab._device_addr = {"192.168.1.50:7000": ("WiiM", "192.168.1.50", 7000, {})}
        ab.probe_device = AsyncMock(return_value=True)

        # Non-bust call: device's existing verdict means we should NOT re-probe.
        client.get("/admin/output/devices")
        # Drain any background tasks the route scheduled.
        await asyncio.sleep(0.01)
        non_bust_calls = ab.probe_device.await_count
        ab.probe_device.reset_mock()

        # Bust call: must re-probe even though the verdict is already true.
        client.get("/admin/output/devices?bust=1")
        await asyncio.sleep(0.01)
        bust_calls = ab.probe_device.await_count

    assert non_bust_calls == 0, "verified entries must not re-probe on default cycles"
    assert bust_calls == 1, "bust=true must re-probe verified entries"


async def test_set_output_active_persists_host_and_via(client, mock_state):
    """POST /output/active with a `host` field writes both `output_host`
    (canonical active-host signal) and `device_via:{host}` (per-device
    Via preference for next-session restore). Without host, neither
    setting is written."""
    settings: dict[str, str] = {}

    async def fake_set(k, v):
        settings[k] = str(v)

    with patch("app.state.activate_backend", AsyncMock()) as ab, \
         patch("app.database.set_setting", side_effect=fake_set):
        resp = client.post(
            "/admin/output/active",
            json={"backend_type": "chromecast", "device_id": "uuid-1",
                  "host": "192.168.1.50"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["host"] == "192.168.1.50"
    # activate_backend got the host kwarg.
    ab.assert_awaited_once_with("chromecast", "uuid-1", host="192.168.1.50")


async def test_set_output_active_without_host_passes_none(client, mock_state):
    """Backwards-compat: an Apply call from a legacy client (no `host`
    field) doesn't crash — activate_backend just gets host=None and the
    setting writes are skipped at the state.py layer."""
    with patch("app.state.activate_backend", AsyncMock()) as ab:
        resp = client.post(
            "/admin/output/active",
            json={"backend_type": "direct", "device_id": "default"},
        )

    assert resp.status_code == 200
    ab.assert_awaited_once_with("direct", "default", host=None)


async def test_get_output_active_returns_host_and_via(client, mock_state):
    """GET /output/active surfaces persisted `host` and `via` so the
    frontend can pre-select both dropdowns on page load before discovery
    completes — works after a server restart with a cold cache."""
    settings = {
        "output_backend_type": "chromecast",
        "output_device_id": "uuid-1",
        "output_host": "192.168.1.50",
        "device_via:192.168.1.50": "chromecast",
    }

    async def fake_get(key, default=None):
        return settings.get(key, default)

    with patch("app.database.get_setting", side_effect=fake_get):
        resp = client.get("/admin/output/active")

    body = resp.json()
    assert body["host"] == "192.168.1.50"
    assert body["via"] == "chromecast"
    assert body["backend_type"] == "chromecast"
    assert body["device_id"] == "uuid-1"


async def test_get_output_active_handles_missing_host_gracefully(client, mock_state):
    """No `output_host` persisted (clean install) → host and via are null;
    the frontend falls back to defaults."""
    async def fake_get(key, default=None):
        return default  # always return the default (i.e. None)

    with patch("app.database.get_setting", side_effect=fake_get):
        resp = client.get("/admin/output/active")

    body = resp.json()
    assert body["host"] is None
    assert body["via"] is None


# ── U4: POST /admin/playback/no-audio ────────────────────────────────────────


def _make_airplay_backend(device_id: str = "d1"):
    """Build a minimal AirPlayBackend with a populated discovery cache so
    isinstance() checks succeed and `_device_id` resolves to a real value."""
    from app.output.airplay import AirPlayBackend
    backend = AirPlayBackend()
    backend._device_id = device_id
    backend._device_addr[device_id] = ("JBL", "192.168.1.20", 7000, {"pk": "x"})
    return backend


def test_no_audio_persists_ap1_and_restarts_via_cliraop(client, mock_state):
    """Covers F3. Active AirPlay session → POST /admin/playback/no-audio →
    `airplay:protocol:<device_id> = ap1` persisted, output_router.stop()
    called, output_router.play() called to respawn (cliraop now selected
    because of the cached ap1), AirPlayProtocolChangedEvent broadcast."""
    qe, or_ = mock_state
    backend = _make_airplay_backend("d1")
    or_.active = backend

    # Seed the queue with a current track directly (bypass the async
    # set_playing path — sync TestClient can't easily drive it).
    from app.queue.models import QueueItem
    track = make_track("t1")
    qe._current = QueueItem(track=track)

    persisted: dict[str, str] = {}
    broadcasts: list = []

    async def _set_setting(key, value):
        persisted[key] = value

    async def _broadcast(event):
        broadcasts.append(event)

    with patch("app.database.set_setting", side_effect=_set_setting), \
         patch("app.events.bus.manager.broadcast_to_admins", side_effect=_broadcast), \
         patch("app.state.get_plex_client", AsyncMock(return_value=MagicMock())), \
         patch("app.state._make_stream_url", return_value="http://stream/t1"):
        resp = client.post("/admin/playback/no-audio")

    assert resp.status_code == 200
    body = resp.json()
    assert body["device_id"] == "d1"
    assert body["protocol"] == "ap1"
    assert persisted == {"airplay:protocol:d1": "ap1"}
    assert or_.stop.await_count >= 1
    assert or_.play.await_count >= 1
    assert len(broadcasts) == 1
    assert broadcasts[0].device_id == "d1"
    assert broadcasts[0].protocol == "ap1"


def test_no_audio_returns_400_when_active_backend_not_airplay(client, mock_state):
    """A Chromecast/DLNA/Direct active backend → endpoint refuses with 400.
    The button is hidden in UI for these backends; the server also refuses
    so a curl/manual POST can't corrupt state."""
    qe, or_ = mock_state
    # Use a plain mock that is NOT an AirPlayBackend instance.
    or_.active = MagicMock(name="ChromecastBackend")
    resp = client.post("/admin/playback/no-audio")
    assert resp.status_code == 400


def test_no_audio_returns_400_when_no_current_track(client, mock_state):
    """No track currently in the queue's `current` slot → no-audio has
    nothing to restart. Refuse with 400 so the UI surfaces an error
    rather than silently spawning a fresh session with a stale track."""
    qe, or_ = mock_state
    backend = _make_airplay_backend("d1")
    or_.active = backend
    # qe.state.current is None by default (fresh QueueEngine).
    resp = client.post("/admin/playback/no-audio")
    assert resp.status_code == 400


def test_no_audio_requires_auth(anon_client, mock_session):
    """Unauthenticated callers get 401 like every other admin endpoint."""
    resp = anon_client.post("/admin/playback/no-audio")
    assert resp.status_code == 401


# ── U5: POST /admin/output/devices/{device_id}/retest-ap2 ────────────────────


def test_retest_ap2_reruns_probe_and_returns_new_verdict(client, mock_state):
    """Covers F4. Endpoint awaits the probe synchronously and returns
    the new verdict. The probe coroutine internally persists and
    broadcasts; the endpoint just relays the result."""
    backend = _make_airplay_backend("d1")

    async def _fake_probe(self, device_id, name, host, port, txt, *, force=False):
        assert force is True  # Re-test must bypass the cache
        return "ap2"

    with patch("app.state.airplay_backend", backend), \
         patch("app.output.airplay.AirPlayBackend._probe_device", _fake_probe):
        resp = client.post("/admin/output/devices/d1/retest-ap2")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"device_id": "d1", "protocol": "ap2"}


def test_retest_ap2_returns_404_for_unknown_device(client, mock_state):
    """Device id not in the AirPlay backend's discovery cache → 404.
    Prevents the endpoint from probing arbitrary device IDs."""
    backend = _make_airplay_backend("d1")
    # Empty the discovery cache to simulate "unknown device".
    backend._device_addr.clear()

    with patch("app.state.airplay_backend", backend):
        resp = client.post("/admin/output/devices/unknown/retest-ap2")
    assert resp.status_code == 404


def test_retest_ap2_returns_503_when_airplay_backend_unavailable(client, mock_state):
    """If the AirPlay backend itself is missing (binaries not installed,
    init failed), Re-test can't proceed. Return 503 rather than 500."""
    with patch("app.state.airplay_backend", None):
        resp = client.post("/admin/output/devices/d1/retest-ap2")
    assert resp.status_code == 503


def test_retest_ap2_requires_auth(anon_client, mock_session):
    resp = anon_client.post("/admin/output/devices/d1/retest-ap2")
    assert resp.status_code == 401


def test_retest_ap2_returns_409_for_currently_playing_active_device(
    client, mock_state
):
    """Refuse Re-test against the actively-playing speaker. Probing it
    would spawn a competing cliap2 session on the same speaker and
    disrupt the active stream. User must stop playback first."""
    backend = _make_airplay_backend("d1")
    backend._is_playing = True
    qe, or_ = mock_state
    or_.active = backend

    with patch("app.state.airplay_backend", backend):
        resp = client.post("/admin/output/devices/d1/retest-ap2")
    assert resp.status_code == 409
    assert "currently-playing" in resp.json()["detail"]


def test_retest_ap2_proceeds_when_active_device_is_not_playing(
    client, mock_state
):
    """If the device is the active output but not currently playing,
    Re-test is safe — no live RTSP session to disrupt."""
    backend = _make_airplay_backend("d1")
    backend._is_playing = False  # selected as output but idle
    qe, or_ = mock_state
    or_.active = backend

    async def _fake_probe(self, device_id, name, host, port, txt, *, force=False):
        return "ap2"

    with patch("app.state.airplay_backend", backend), \
         patch("app.output.airplay.AirPlayBackend._probe_device", _fake_probe):
        resp = client.post("/admin/output/devices/d1/retest-ap2")
    assert resp.status_code == 200
    assert resp.json() == {"device_id": "d1", "protocol": "ap2"}



async def test_settings_rail_mode_vanilla_round_trips(client, mock_state):
    """The third rail mode (2026-06-09 rail plan R5/R7) is accepted and
    persisted; the settings GET returns it back."""
    store: dict[str, str] = {}

    async def fake_set(key, value):
        store[key] = value

    async def fake_get(key):
        return store.get(key)

    with patch("app.database.set_setting", AsyncMock(side_effect=fake_set)), \
         patch("app.database.get_setting", AsyncMock(side_effect=fake_get)):
        resp = client.post("/admin/settings", json={"queue_end_behavior": "stop",
                                                    "rail_mode": "vanilla"})
        assert resp.status_code == 200
        resp = client.get("/admin/settings")
        assert resp.status_code == 200
        assert resp.json()["rail_mode"] == "vanilla"


# ── pattern rules + artist exclusions (2026-06-10 pattern-rules plan U4) ─────

def test_pattern_rules_require_auth(anon_client, mock_session):
    assert anon_client.get("/admin/pattern-rules").status_code == 401
    assert anon_client.post("/admin/pattern-rules", json={"rules": []}).status_code == 401


def test_artist_exclusions_require_auth(anon_client, mock_session):
    assert anon_client.get("/admin/artist-exclusions").status_code == 401


async def test_pattern_rules_round_trip_preserves_order_and_inert(client, mock_state):
    rules = [["&", "and"], ["'", ""], ["e", "é", "è"]]  # middle rule inert
    stored = {}

    async def fake_set(r): stored["rules"] = r
    async def fake_get(): return stored.get("rules", [])

    with patch("app.database.set_pattern_rules", AsyncMock(side_effect=fake_set)), \
         patch("app.database.get_pattern_rules", AsyncMock(side_effect=fake_get)):
        resp = client.post("/admin/pattern-rules", json={"rules": rules})
        assert resp.status_code == 200
        got = client.get("/admin/pattern-rules").json()
    # Inert rules round-trip unmodified — only the public endpoint filters.
    assert got == {"rules": rules}


async def test_artist_exclusions_round_trip(client, mock_state):
    stored = {}

    async def fake_set(n): stored["names"] = n
    async def fake_get(): return stored.get("names", [])

    with patch("app.database.set_artist_exclusions", AsyncMock(side_effect=fake_set)), \
         patch("app.database.get_artist_exclusions", AsyncMock(side_effect=fake_get)):
        resp = client.post("/admin/artist-exclusions", json={"names": ["[dialogue]"]})
        assert resp.status_code == 200
        assert client.get("/admin/artist-exclusions").json() == {"names": ["[dialogue]"]}


async def test_pattern_rules_caps_rejected(client, mock_state):
    too_many_strings = [["x"] * 21]
    assert client.post("/admin/pattern-rules", json={"rules": too_many_strings}).status_code == 422
    too_long = [["a" * 201, "b"]]
    assert client.post("/admin/pattern-rules", json={"rules": too_long}).status_code == 422
    too_many_rules = [["a", "b"]] * 101
    assert client.post("/admin/pattern-rules", json={"rules": too_many_rules}).status_code == 422


async def test_artist_exclusions_caps_rejected(client, mock_state):
    assert client.post("/admin/artist-exclusions", json={"names": ["x" * 201]}).status_code == 422
    assert client.post("/admin/artist-exclusions", json={"names": ["a"] * 501}).status_code == 422


# ── appearance defaults + broadcast (2026-06-11 glow-up plan U1) ──────────────

async def test_settings_default_scheme_persists_and_broadcasts(client, mock_state):
    store: dict[str, str] = {}

    async def fake_get(key, default=None):
        return store.get(key, default)

    async def fake_set(key, value):
        store[key] = value

    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)), \
         patch("app.database.set_setting", AsyncMock(side_effect=fake_set)), \
         patch("app.events.bus.manager.broadcast_to_all", AsyncMock()) as bc:
        resp = client.post("/admin/settings", json={
            "default_scheme": "king-crimson", "rail_mode": "vu",
        })
        assert resp.status_code == 200
        assert store["default_scheme"] == "king-crimson"
        assert store["rail_mode"] == "vu"
        assert bc.await_count == 1
        ev = bc.await_args.args[0]
        assert ev.to_json() == {
            "type": "appearance_changed", "scheme": "king-crimson", "rail_mode": "vu",
            "view": "list", "surprise_me_enabled": True,
            "rail_alpha_mode": "english",
            "rail_artist_threshold": 2, "rail_album_threshold": 2,
            "rating_style": "stars",
        }
        assert client.get("/admin/settings").json()["default_scheme"] == "king-crimson"


async def test_settings_unknown_scheme_rejected(client, mock_state):
    resp = client.post("/admin/settings", json={"default_scheme": "hot-dog-stand"})
    assert resp.status_code == 422


async def test_settings_default_view_persists_and_broadcasts(client, mock_state):
    """Tile-view U1: POST default_view='tile' persists, GET returns it, and the
    appearance_changed broadcast carries view alongside scheme/rail_mode."""
    store: dict[str, str] = {}

    async def fake_get(key, default=None):
        return store.get(key, default)

    async def fake_set(key, value):
        store[key] = value

    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)), \
         patch("app.database.set_setting", AsyncMock(side_effect=fake_set)), \
         patch("app.events.bus.manager.broadcast_to_all", AsyncMock()) as bc:
        resp = client.post("/admin/settings", json={"default_view": "tile"})
        assert resp.status_code == 200
        assert store["default_view"] == "tile"
        assert bc.await_count == 1
        ev = bc.await_args.args[0]
        assert ev.to_json()["view"] == "tile"
        assert client.get("/admin/settings").json()["default_view"] == "tile"


async def test_settings_default_view_invalid_returns_422(client, mock_state):
    """Pydantic Literal rejects unknown view values."""
    resp = client.post("/admin/settings", json={"default_view": "mosaic"})
    assert resp.status_code == 422


async def test_settings_default_view_default_list(client, mock_state):
    """When default_view is unset, GET /admin/settings defaults to 'list'."""
    with patch("app.database.get_setting", AsyncMock(return_value=None)):
        resp = client.get("/admin/settings")
        assert resp.status_code == 200
        assert resp.json()["default_view"] == "list"


async def test_settings_non_appearance_save_does_not_broadcast(client, mock_state):
    with patch("app.database.set_setting", AsyncMock()), \
         patch("app.events.bus.manager.broadcast_to_all", AsyncMock()) as bc:
        resp = client.post("/admin/settings", json={"queue_display_n": 5})
        assert resp.status_code == 200
        assert bc.await_count == 0


# ── Track ratings + tags authoring (2026-06-26 ratings-and-tags plan U2) ─────

def test_track_rating_requires_auth(anon_client, mock_session):
    resp = anon_client.post("/admin/track-rating", json={"track_id": "t1", "stars": 4})
    assert resp.status_code == 401


def test_track_tags_requires_auth(anon_client, mock_session):
    resp = anon_client.post("/admin/track-tags", json={"track_id": "t1", "tags": ["x"]})
    assert resp.status_code == 401


def test_set_rating_persists(client, mock_state):
    with patch("app.api.admin.database.set_rating", AsyncMock()) as sr:
        resp = client.post("/admin/track-rating", json={"track_id": "srv:42", "stars": 4})
    assert resp.status_code == 200
    assert resp.json() == {"track_id": "srv:42", "stars": 4}
    sr.assert_awaited_once_with("srv:42", 4)


def test_set_rating_zero_clears(client, mock_state):
    with patch("app.api.admin.database.set_rating", AsyncMock()) as sr:
        resp = client.post("/admin/track-rating", json={"track_id": "t1", "stars": 0})
    assert resp.status_code == 200
    assert resp.json()["stars"] is None          # cleared → null
    sr.assert_awaited_once_with("t1", 0)


def test_set_rating_out_of_range_422(client, mock_state):
    with patch("app.api.admin.database.set_rating", AsyncMock()) as sr:
        resp = client.post("/admin/track-rating", json={"track_id": "t1", "stars": 6})
    assert resp.status_code == 422
    sr.assert_not_awaited()                       # rejected before any write


def test_set_rating_invalid_track_id_400(client, mock_state):
    with patch("app.api.admin.database.set_rating", AsyncMock()) as sr:
        resp = client.post("/admin/track-rating", json={"track_id": "bad id!", "stars": 3})
    assert resp.status_code == 400
    sr.assert_not_awaited()


def test_set_tags_echoes_normalized(client, mock_state):
    # set_tags is the normalization chokepoint; the endpoint echoes whatever it
    # stores. Mock it to return the normalized set and assert the echo.
    with patch("app.api.admin.database.set_tags",
               AsyncMock(return_value=["Fun", "Chill"])) as st:
        resp = client.post("/admin/track-tags",
                           json={"track_id": "t1", "tags": ["  Fun ", "fun", "Chill"]})
    assert resp.status_code == 200
    assert resp.json() == {"track_id": "t1", "tags": ["Fun", "Chill"]}
    st.assert_awaited_once()


def test_set_tags_absurd_payload_422(client, mock_state):
    with patch("app.api.admin.database.set_tags", AsyncMock()) as st:
        resp = client.post("/admin/track-tags",
                           json={"track_id": "t1", "tags": [f"t{i}" for i in range(101)]})
    assert resp.status_code == 422
    st.assert_not_awaited()


def test_settings_persists_visibility_and_facet_flags(client, mock_state):
    """Plan U4: the seven bool flags persist as '1'/'0' via set_setting."""
    with patch("app.database.set_setting", AsyncMock()) as ss, \
         patch("app.events.bus.manager.broadcast_to_all", AsyncMock()):
        resp = client.post("/admin/settings", json={
            "ratings_visible_to_guests": True,
            "tags_visible_to_guests": False,
            "facet_years": False,
            "facet_highestrated": True,
        })
    assert resp.status_code == 200
    persisted = {c.args[0]: c.args[1] for c in ss.call_args_list}
    assert persisted.get("ratings_visible_to_guests") == "1"
    assert persisted.get("tags_visible_to_guests") == "0"
    assert persisted.get("facet_years") == "0"
    assert persisted.get("facet_highestrated") == "1"
