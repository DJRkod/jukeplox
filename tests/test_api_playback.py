"""Tests for the admin transport endpoints — POST /admin/playback/previous.

Extracted from tests/test_api_admin.py (2026-06-09 layout plan; code-review
finding: the admin API test module exceeded 1k lines). Shared fixtures
(mock_session, mock_state, client, anon_client) come from tests/conftest.py.
"""

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from tests.conftest import make_track, make_vanished_source_registry, make_sourced_track


# ── playback/previous (Skip Back) ─────────────────────────────────────────────

def test_playback_previous_requires_auth(anon_client, mock_session):
    resp = anon_client.post("/admin/playback/previous")
    assert resp.status_code == 401


async def test_playback_previous_plays_history_track(client, mock_state):
    """Covers AE4 / F3: history head becomes current and plays; the
    interrupted track lands at the front of the queue."""
    qe, or_ = mock_state
    await qe.set_playing(make_track("t1"))
    await qe.advance()                       # t1 -> history
    await qe.set_playing(make_track("t2"))   # t2 is the interrupted track
    with patch("app.state.get_plex_client", AsyncMock(return_value=MagicMock())), \
         patch("app.state._make_stream_url", return_value="http://stream/t1"), \
         patch("app.database.increment_play_count", AsyncMock()), \
         patch("app.database.set_play_track_meta", AsyncMock()):
        resp = client.post("/admin/playback/previous")
    assert resp.status_code == 200
    or_.play.assert_awaited_once()
    assert or_.play.await_args.args[0] == "http://stream/t1"
    or_.stop.assert_not_awaited()            # play-without-stop ordering (DLNA)
    assert qe.state.current.track_id == "t1"
    assert qe.queue[0].track_id == "t2"


async def test_playback_previous_empty_history_409(client, mock_state):
    """Covers AE5: 409 safety net when history is empty; engine untouched."""
    qe, or_ = mock_state
    await qe.set_playing(make_track("t2"))
    with patch("app.state.get_plex_client", AsyncMock(return_value=MagicMock())):
        resp = client.post("/admin/playback/previous")
    assert resp.status_code == 409
    or_.play.assert_not_awaited()
    assert qe.state.current.track_id == "t2"
    assert qe.queue == []


async def test_playback_previous_no_client_409_before_mutation(client, mock_state):
    """A missing Plex client must be rejected BEFORE skip_back() mutates the
    engine — otherwise the interrupted track is stranded at queue front with
    nothing playing (worse than skip's silent-200, which is recoverable)."""
    qe, or_ = mock_state
    await qe.set_playing(make_track("t1"))
    await qe.advance()
    await qe.set_playing(make_track("t2"))
    # mock_state's default get_plex_client returns None
    resp = client.post("/admin/playback/previous")
    assert resp.status_code == 409
    or_.play.assert_not_awaited()
    assert qe.state.current.track_id == "t2"   # engine NOT mutated
    assert qe.queue == []
    assert any(h.track_id == "t1" for h in qe.history)


async def test_playback_previous_play_failure_502(client, mock_state):
    qe, or_ = mock_state
    await qe.set_playing(make_track("t1"))
    await qe.advance()
    await qe.set_playing(make_track("t2"))
    or_.play.side_effect = RuntimeError("renderer offline")
    with patch("app.state.get_plex_client", AsyncMock(return_value=MagicMock())), \
         patch("app.state._make_stream_url", return_value="http://stream/t1"):
        resp = client.post("/admin/playback/previous")
    assert resp.status_code == 502
    assert qe.state.current is None            # set_stopped() reconciled state


async def test_playback_previous_skips_history_head_whose_prefix_has_no_source(client, mock_state, caplog):
    """2026-11-22 review #4, mirror of tests/test_state.py::
    test_do_advance_skips_item_whose_prefix_has_no_source for Skip Back: the
    most recent history item's server vanished, so its URL build raises
    KeyError from the registry. Back must drop the dead item (R22 notice) and
    replay the item before it -- 200, the interrupted track at queue front --
    never a 500 and never a stranded engine."""
    import logging
    qe, or_ = mock_state
    live = make_sourced_track("machine-live", 2, "Live Song")
    gone = make_sourced_track("machine-gone", 1, "Gone Song")
    await qe.set_playing(live)
    await qe.advance()                       # live -> history
    await qe.set_playing(gone)
    await qe.advance()                       # gone -> history head (most recent)
    await qe.set_playing(make_track("t2"))   # t2 is the interrupted track
    skipped = AsyncMock()
    with patch("app.state.get_plex_client",
               AsyncMock(return_value=make_vanished_source_registry())), \
         patch("app.state._stream_url_base", lambda: ""), \
         patch("app.state._emit_track_skipped", skipped), \
         caplog.at_level(logging.WARNING):
        resp = client.post("/admin/playback/previous")
    assert resp.status_code == 200
    or_.play.assert_awaited_once()
    assert or_.play.await_args.args[0] == "http://plex-live.test/machine-live:part2"
    assert or_.play.await_args.args[1] is live
    or_.stop.assert_not_awaited()
    skipped.assert_awaited_once()
    assert skipped.await_args.args[0] is gone
    assert qe.state.current.track is live
    assert qe.queue[0].track_id == "t2"
    assert all(h.track_id != gone.id for h in qe.history), "the dead item must leave history"
    warns = [r for r in caplog.records
             if r.name == "app.playback_control" and r.levelno == logging.WARNING]
    assert warns
    for r in warns:
        msg = r.getMessage()
        assert "machine-gone" in msg and "part1" not in msg and "http" not in msg, msg
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_playback_previous_only_dead_history_409_engine_not_stranded(client, mock_state):
    """Every history item is unroutable: Back drops them and answers 409 with
    the interrupted track still current and the queue untouched (the
    before-mutation rule), rather than skip_back()-ing onto a track nothing
    can play."""
    qe, or_ = mock_state
    gone = make_sourced_track("machine-gone", 1, "Gone Song")
    await qe.set_playing(gone)
    await qe.advance()
    await qe.set_playing(make_track("t2"))
    with patch("app.state.get_plex_client",
               AsyncMock(return_value=make_vanished_source_registry())), \
         patch("app.state._stream_url_base", lambda: ""), \
         patch("app.state._emit_track_skipped", AsyncMock()):
        resp = client.post("/admin/playback/previous")
    assert resp.status_code == 409
    or_.play.assert_not_awaited()
    assert qe.state.current.track_id == "t2"
    assert qe.queue == []
    assert qe.history == []


async def test_playback_previous_bumps_advance_generation(client, mock_state):
    """A pending EOS advance scheduled before Skip Back must not fire after
    it — the endpoint bumps state._advance_gen like playback_skip does."""
    from app import state as state_module
    qe, or_ = mock_state
    await qe.set_playing(make_track("t1"))
    await qe.advance()
    await qe.set_playing(make_track("t2"))
    gen_before = state_module._advance_gen
    with patch("app.state.get_plex_client", AsyncMock(return_value=MagicMock())), \
         patch("app.state._make_stream_url", return_value="http://stream/t1"), \
         patch("app.database.increment_play_count", AsyncMock()), \
         patch("app.database.set_play_track_meta", AsyncMock()):
        resp = client.post("/admin/playback/previous")
    assert resp.status_code == 200
    assert state_module._advance_gen == gen_before + 1


async def test_playback_previous_counts_via_confirmed_start_chokepoint(client, mock_state, fresh_supervisor):
    """Replay counts as a real play — but only once the backend CONFIRMS
    playback started (2026-07-11 supervisor plan U1). Skip Back reports the
    dispatch to the output-session supervisor instead of calling record_play;
    all three entry points share that chokepoint."""
    sup, timers, rec = fresh_supervisor
    qe, or_ = mock_state
    await qe.set_playing(make_track("t1"))
    await qe.advance()
    with patch("app.state.get_plex_client", AsyncMock(return_value=MagicMock())), \
         patch("app.state._make_stream_url", return_value="http://stream/t1"):
        resp = client.post("/admin/playback/previous")
        assert resp.status_code == 200
        rec.assert_not_called()                  # dispatch alone must not count
        token = sup.current_token()
        assert token is not None                 # dispatch was reported
        sup.on_playback_confirmed(token)         # backend confirms playback
    rec.assert_called_once()
    assert rec.call_args.args[0].id == "t1"      # the dispatched track counted


async def test_playback_previous_consumes_r19_mark(client, mock_state, fresh_supervisor):
    """Supervisor plan U3 (R19): a history item carrying the play_recorded
    mark (an outage-held replay) dispatches WITH the mark — confirming must
    not re-count — and the mark is consumed on success so a later organic
    replay counts again (unified with _play_with_fallback / Skip)."""
    sup, timers, rec = fresh_supervisor
    qe, or_ = mock_state
    await qe.set_playing(make_track("t1"))
    await qe.advance()                           # t1 → history
    qe.history[0].play_recorded = True           # already counted pre-outage
    with patch("app.state.get_plex_client", AsyncMock(return_value=MagicMock())), \
         patch("app.state._make_stream_url", return_value="http://stream/t1"):
        resp = client.post("/admin/playback/previous")
    assert resp.status_code == 200
    or_.play.assert_awaited_once()
    assert qe.state.current.play_recorded is False   # mark consumed
    sup.on_playback_confirmed(sup.current_token())   # carried into dispatch:
    rec.assert_not_called()                          # no double count


async def test_playback_previous_while_paused_results_in_playing(client, mock_state):
    """The endpoint must not gate on is_paused — Skip Back from a paused
    session plays the history track (mirrors the engine-level test)."""
    qe, or_ = mock_state
    await qe.set_playing(make_track("t1"))
    await qe.advance()
    await qe.set_playing(make_track("t2"))
    await qe.set_paused(True)
    with patch("app.state.get_plex_client", AsyncMock(return_value=MagicMock())), \
         patch("app.state._make_stream_url", return_value="http://stream/t1"), \
         patch("app.database.increment_play_count", AsyncMock()), \
         patch("app.database.set_play_track_meta", AsyncMock()):
        resp = client.post("/admin/playback/previous")
    assert resp.status_code == 200
    or_.play.assert_awaited_once()
    assert qe.state.is_playing is True
    assert qe.state.is_paused is False


async def test_playback_previous_while_idle_plays_history_without_requeue(client, mock_state):
    """After the final track ends (or a restart), current is None but
    persisted history remains — previous plays the history head and inserts
    nothing into the queue (no interrupted track exists)."""
    qe, or_ = mock_state
    await qe.set_playing(make_track("t1"))
    await qe.advance()                       # idle; history [t1]
    with patch("app.state.get_plex_client", AsyncMock(return_value=MagicMock())), \
         patch("app.state._make_stream_url", return_value="http://stream/t1"), \
         patch("app.database.increment_play_count", AsyncMock()), \
         patch("app.database.set_play_track_meta", AsyncMock()):
        resp = client.post("/admin/playback/previous")
    assert resp.status_code == 200
    or_.play.assert_awaited_once()
    assert qe.state.current.track_id == "t1"
    assert qe.queue == []


async def test_playback_previous_captures_track_meta_after_confirm(client, mock_state, monkeypatch):
    """Most-played plan U1: a replay is a real play — the meta upsert fires
    with the track's display dict once the supervisor's confirmed-start
    chokepoint runs the real record_play (never at dispatch)."""
    from app.output import session
    sup = session.OutputSessionSupervisor(timer_factory=lambda d, cb: MagicMock())
    monkeypatch.setattr(session, "_supervisor", sup)
    qe, or_ = mock_state
    await qe.set_playing(make_track("t1"))
    await qe.advance()
    await qe.set_playing(make_track("t2"))
    with patch("app.state.get_plex_client", AsyncMock(return_value=MagicMock())), \
         patch("app.state._make_stream_url", return_value="http://stream/t1"), \
         patch("app.database.increment_play_count", AsyncMock()), \
         patch("app.database.set_play_track_meta", AsyncMock()) as meta:
        resp = client.post("/admin/playback/previous")
        assert resp.status_code == 200
        assert meta.await_count == 0             # nothing captured at dispatch
        sup.on_playback_confirmed(sup.current_token())
        # record_play is fire-and-forget tasks on this loop; poll briefly.
        for _ in range(100):
            if meta.await_count >= 1:
                break
            await asyncio.sleep(0.01)
    meta.assert_awaited_once()
    tid, payload = meta.await_args.args
    assert tid == "t1"
    assert payload["title"] == "Song" and "artist" in payload


# ── Closing Time snapshot fields (2026-06-24 plan U3) ────────────────────────

async def test_playback_position_includes_closing_state(client, mock_state, monkeypatch):
    """The shared progress poll learns the banner state even while frozen (no current)."""
    import app.state as st
    monkeypatch.setattr(st, "_closing_active", True)
    monkeypatch.setattr(st, "_closing_message", "Last call")
    data = client.get("/api/playback/position").json()
    assert data["closing_active"] is True
    assert data["closing_message"] == "Last call"


async def test_playback_position_closing_default_inactive(client, mock_state):
    data = client.get("/api/playback/position").json()
    assert data["closing_active"] is False
    assert data["closing_message"] == ""
