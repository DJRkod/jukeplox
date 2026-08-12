"""Tests for the Radio Mode HTTP surface + RadioStateEvent wiring (radio plan U7).

DB-light by construction: the routes touch three collaborators — the radio
session singleton (``app.state.radio_session``), the Radio Browser client
(``app.api.radio.get_radio_client``), and the ``guest_radio_control`` settings
accessor. All three are mocked, so these tests never open aiosqlite (avoiding the
known full-file teardown hang) and never hit the network.

Auth: admin routes carry ``require_admin`` — patched via the shared conftest
``mock_session`` (a cookie of ``valid-token`` authenticates). Anonymous POSTs to
``/admin/radio/*`` must 401.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.radio.client import RadioDirectoryUnavailable, Station


# ── helpers ──────────────────────────────────────────────────────────────────


def _station(uuid: str = "uuid-1", name: str = "Jazz FM",
             url: str = "http://stream.example/jazz",
             lastcheckok: bool = True) -> Station:
    return Station(
        stationuuid=uuid, name=name, url=url, url_resolved=url, favicon="",
        codec="MP3", bitrate=128, tags=["jazz"], countrycode="US",
        lastcheckok=lastcheckok,
    )


def _station_body(uuid: str = "uuid-1") -> dict:
    """The Station.to_dict() shape the browse UI posts back to play/switch."""
    return _station(uuid).to_dict()


def _make_session_mock(*, active: bool = False, status: str = "idle",
                       station: Station | None = None,
                       live_title: str | None = None) -> MagicMock:
    """A stand-in for ``app.state.radio_session`` with the U7-read surface.

    ``start``/``stop``/``mark_failed`` are (async where needed) mocks; the read
    accessors (``is_active``/``status``/``station``/``current_title``) return the
    configured snapshot so ``radio_snapshot`` renders it."""
    sess = MagicMock()
    sess.is_active = MagicMock(return_value=active)
    sess.status = MagicMock(return_value=status)
    sess.current_title = MagicMock(return_value=live_title)
    # `station` is a property on the real session; a plain attribute here is fine.
    sess.station = station
    sess.start = AsyncMock()
    sess.stop = AsyncMock()
    sess.mark_failed = MagicMock()
    return sess


def _radio_client_mock() -> MagicMock:
    client = MagicMock()
    client.get_tags = AsyncMock(return_value=[
        {"name": "jazz", "stationcount": 500},
        {"name": "rock", "stationcount": 900},
        {"name": "", "stationcount": 10},          # garbage row → skipped
    ])
    client.get_top_click_cached = AsyncMock(return_value=[
        _station("pop-1", "Popular One"),
        _station("pop-2", "Popular Two", lastcheckok=False),  # filtered out
    ])
    client.search_stations = AsyncMock(return_value=[_station("s-1", "Found")])
    client.stations_by_tag_exact = AsyncMock(return_value=[_station("t-1", "Tagged")])
    client.stations_by_countrycode_exact = AsyncMock(
        return_value=[_station("c-1", "CC")])
    return client


@pytest.fixture
def radio_env(mock_session):
    """Patch the radio session singleton + client + guest-control accessor.

    Yields ``(session_mock, client_mock)``. ``mock_session`` (from conftest)
    makes ``valid-token`` authenticate for the admin routes. ``guest_radio_control``
    defaults ON here so play/switch happy-paths work; the 403 tests re-patch it
    off locally."""
    import app.api.radio as radio_api
    from app.main import app

    sess = _make_session_mock()
    client = _radio_client_mock()
    radio_api._reset_stations_rate_limit()
    with patch("app.state.radio_session", sess), \
         patch("app.api.radio.get_radio_client", return_value=client), \
         patch("app.database.get_guest_radio_control", AsyncMock(return_value=True)):
        yield sess, client
    radio_api._reset_stations_rate_limit()


@pytest.fixture
def anon(radio_env):
    from app.main import app
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture
def admin(radio_env):
    from app.main import app
    c = TestClient(app, raise_server_exceptions=True)
    c.cookies.set("jukeplox_session", "valid-token")
    return c


# ── GET /api/radio/stations — curated landing + search + rate limit + R13 ───────


def test_stations_curated_landing(anon, radio_env):
    """No query → curated landing: genre quick-picks (top tags) + liveness-
    filtered popular set."""
    _sess, _client = radio_env
    resp = anon.get("/api/radio/stations")
    assert resp.status_code == 200
    body = resp.json()
    assert body["unavailable"] is False
    assert body["mode"] == "landing"
    names = [t["name"] for t in body["tags"]]
    assert "rock" in names and "jazz" in names
    assert "" not in names                       # garbage tag skipped
    # Popular: pop-2 has lastcheckok=False → filtered out of the popular set.
    pop_uuids = [s["stationuuid"] for s in body["popular"]]
    assert pop_uuids == ["pop-1"]


def test_stations_search_by_name(anon, radio_env):
    _sess, client = radio_env
    resp = anon.get("/api/radio/stations", params={"q": "found"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "search"
    assert [s["stationuuid"] for s in body["stations"]] == ["s-1"]
    client.search_stations.assert_awaited_once()


def test_stations_search_by_tag(anon, radio_env):
    _sess, client = radio_env
    resp = anon.get("/api/radio/stations", params={"tag": "jazz"})
    assert resp.status_code == 200
    assert resp.json()["stations"][0]["stationuuid"] == "t-1"
    client.stations_by_tag_exact.assert_awaited_once()


def test_stations_directory_unavailable_is_explicit_state(anon, radio_env):
    """AE10: RadioDirectoryUnavailable → explicit {unavailable: true}, NOT a 500."""
    _sess, client = radio_env
    client.get_tags = AsyncMock(side_effect=RadioDirectoryUnavailable("down"))
    resp = anon.get("/api/radio/stations")
    assert resp.status_code == 200
    body = resp.json()
    assert body["unavailable"] is True
    assert body["tags"] == [] and body["popular"] == []


def test_stations_rate_limit_trips_429(anon, radio_env):
    """SEC-003: rapid distinct queries from one IP trip the per-IP cap (429),
    independent of the concurrency semaphore."""
    _sess, _client = radio_env
    # The bucket allows _STATIONS_RATE_MAX (10) per window; distinct queries all
    # count (they bypass the SWR cache). The 11th trips.
    codes = []
    for i in range(12):
        codes.append(anon.get("/api/radio/stations",
                              params={"q": f"query-{i}"}).status_code)
    assert codes[:10] == [200] * 10
    assert 429 in codes[10:]


# ── GET /api/radio/current — reflects the session snapshot ─────────────────────


def test_current_reflects_playing_station(anon):
    """current mirrors the session: playing station + status + live_title."""
    import app.api.radio as radio_api
    sess = _make_session_mock(active=True, status="playing",
                              station=_station("uuid-9", "Live"),
                              live_title="Artist - Song")
    with patch("app.state.radio_session", sess):
        resp = anon.get("/api/radio/current")
    assert resp.status_code == 200
    body = resp.json()
    assert body["active"] is True
    assert body["status"] == "playing"
    assert body["station"]["stationuuid"] == "uuid-9"
    assert body["live_title"] == "Artist - Song"


def test_current_idle_when_inactive(anon, radio_env):
    resp = anon.get("/api/radio/current")
    assert resp.status_code == 200
    body = resp.json()
    assert body["active"] is False
    assert body["status"] == "idle"
    assert body["station"] is None


# ── POST /api/radio/stop — always allowed (AE11) ───────────────────────────────


def test_guest_stop_always_allowed(anon, radio_env):
    """AE11: guest stop is allowed and triggers session.stop — even with
    guest_radio_control OFF."""
    sess, _client = radio_env
    with patch("app.database.get_guest_radio_control", AsyncMock(return_value=False)):
        resp = anon.post("/api/radio/stop")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    sess.stop.assert_awaited_once()


# ── POST /api/radio/play|switch — 403 gate (AE3) ───────────────────────────────


def test_guest_play_403_when_control_off(anon, radio_env):
    """AE3: guest play is 403 when guest_radio_control is off; session untouched."""
    sess, _client = radio_env
    with patch("app.database.get_guest_radio_control", AsyncMock(return_value=False)):
        resp = anon.post("/api/radio/play", json=_station_body())
    assert resp.status_code == 403
    sess.start.assert_not_awaited()


def test_guest_switch_403_when_control_off(anon, radio_env):
    sess, _client = radio_env
    with patch("app.database.get_guest_radio_control", AsyncMock(return_value=False)):
        resp = anon.post("/api/radio/switch", json=_station_body("uuid-2"))
    assert resp.status_code == 403
    sess.start.assert_not_awaited()


def test_guest_play_allowed_when_control_on(anon, radio_env):
    """With the toggle on, a guest start resolves the body → Station → session.start."""
    sess, _client = radio_env
    resp = anon.post("/api/radio/play", json=_station_body("uuid-7"))
    assert resp.status_code == 200
    sess.start.assert_awaited_once()
    started = sess.start.call_args.args[0]
    assert isinstance(started, Station)
    assert started.stationuuid == "uuid-7"


def test_play_body_without_url_rejected_400(anon, radio_env):
    """A station body carrying no playable URL (url_resolved/url both empty) → 400."""
    sess, _client = radio_env
    body = _station_body()
    body["url"] = ""
    body["url_resolved"] = ""
    resp = anon.post("/api/radio/play", json=body)
    assert resp.status_code == 400
    sess.start.assert_not_awaited()


# ── admin routes — always permitted + auth (401) ───────────────────────────────


def test_admin_play_always_succeeds(admin, radio_env):
    """AE3: admin play always succeeds regardless of the guest toggle."""
    sess, _client = radio_env
    with patch("app.database.get_guest_radio_control", AsyncMock(return_value=False)):
        resp = admin.post("/admin/radio/play", json=_station_body("uuid-a"))
    assert resp.status_code == 200
    sess.start.assert_awaited_once()


def test_admin_stop_succeeds(admin, radio_env):
    sess, _client = radio_env
    resp = admin.post("/admin/radio/stop")
    assert resp.status_code == 200
    sess.stop.assert_awaited_once()


def test_anon_admin_routes_401(anon, radio_env):
    """Auth: an anonymous POST to /admin/radio/* is 401 (require_admin)."""
    sess, _client = radio_env
    assert anon.post("/admin/radio/play", json=_station_body()).status_code == 401
    assert anon.post("/admin/radio/switch", json=_station_body()).status_code == 401
    assert anon.post("/admin/radio/stop").status_code == 401
    sess.start.assert_not_awaited()
    sess.stop.assert_not_awaited()


# ── now-playing snapshot radio block (late-join convergence) ───────────────────


def test_now_playing_snapshot_has_radio_block(anon):
    """Integration: the now-playing snapshot's radio block lets a late-joining
    client converge on the active station without a live event. Uses the guest
    mock_deps fixture (from test_api_guest) indirectly via a light state patch."""
    import contextlib
    from app.queue.engine import QueueEngine
    from app.main import app

    sess = _make_session_mock(active=True, status="playing",
                              station=_station("uuid-np", "NowPlaying FM"),
                              live_title="Live Set")
    qe = QueueEngine()  # empty → no-current branch
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("app.state.radio_session", sess))
        stack.enter_context(patch("app.state.queue_engine", qe))
        stack.enter_context(patch("app.database.get_setting",
                                  AsyncMock(return_value=None)))
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/now-playing")
    assert resp.status_code == 200
    radio = resp.json()["radio"]
    assert radio["active"] is True
    assert radio["status"] == "playing"
    assert radio["station"]["stationuuid"] == "uuid-np"
    assert radio["live_title"] == "Live Set"


# ── RadioStateEvent wiring — transitions, title change, failed (DL-007 / AE9) ───


def test_radio_state_event_broadcasts_on_transition_and_title(radio_env):
    """DL-007: a RadioStateEvent carries live_title; a title change emits an event
    with the new title; null → name-only. Exercises the REAL session + the
    state.py listener wiring against a mocked bus, marshaling synchronously."""
    import asyncio
    import app.state as st
    from app.radio.session import RadioSession
    from app.events.types import RadioStateEvent

    async def _run():
        broadcasts: list[RadioStateEvent] = []

        manager = MagicMock()
        async def _cap(ev):
            broadcasts.append(ev)
        manager.broadcast_to_all = AsyncMock(side_effect=_cap)

        # A real session with fully-injected seams (no output/DB/network).
        sess = RadioSession(
            stop_output=AsyncMock(),
            hold_queue=AsyncMock(),
            has_queue_current=lambda: False,
            play_url=AsyncMock(),
            resume_queue=AsyncMock(),
            report_click=lambda uuid: None,
            validate_url=AsyncMock(return_value="http://stream.example/jazz"),
            teardown_stream=AsyncMock(),
            read_title=AsyncMock(return_value=None),
        )
        with patch("app.state.radio_session", sess), \
             patch("app.state._radio_loop", asyncio.get_running_loop()), \
             patch("app.events.bus.manager", manager):
            # Direct broadcast (synchronous await) rather than the
            # run_coroutine_threadsafe indirection, so the assertion is
            # deterministic on this loop.
            sess.add_state_listener(
                lambda: asyncio.ensure_future(st._do_broadcast_radio_state()))
            sess.add_title_listener(
                lambda _t: asyncio.ensure_future(st._do_broadcast_radio_state()))

            await sess.start(_station("uuid-e", "Event FM"))
            await asyncio.sleep(0)  # let the ensure_future broadcasts run
            # start emits transition events (connecting → playing). The marshaled
            # broadcast reads the CURRENT snapshot, so the terminal status is
            # "playing"; what matters is that transitions fired an event carrying
            # the station + playing status (DL-007).
            assert broadcasts, "a start transition must emit a RadioStateEvent"
            assert broadcasts[-1].status == "playing"
            assert broadcasts[-1].station["stationuuid"] == "uuid-e"

            broadcasts.clear()
            sess.set_title("Artist - New Song")
            await asyncio.sleep(0)
            assert broadcasts, "a title change must emit a RadioStateEvent"
            assert broadcasts[-1].live_title == "Artist - New Song"

            broadcasts.clear()
            sess.set_title(None)              # null → name-only
            await asyncio.sleep(0)
            assert broadcasts[-1].live_title is None

    asyncio.run(_run())


def test_radio_failed_sets_offline_status(radio_env):
    """AE9: after the backend failed-hook fires, mark_failed flips the active
    station to 'failed' (offline) and it appears in current + a RadioStateEvent —
    while inactive it is a no-op (the event must not fire when radio is off)."""
    import asyncio
    import app.state as st
    from app.radio.session import RadioSession

    async def _run():
        sess = RadioSession(
            stop_output=AsyncMock(), hold_queue=AsyncMock(),
            has_queue_current=lambda: False, play_url=AsyncMock(),
            resume_queue=AsyncMock(), report_click=lambda u: None,
            validate_url=AsyncMock(return_value="http://stream.example/jazz"),
            teardown_stream=AsyncMock(), read_title=AsyncMock(return_value=None),
        )
        with patch("app.state.radio_session", sess):
            # Inactive: mark_failed is a no-op (no phantom offline event).
            st._on_radio_failed()
            assert sess.status() == "idle"

            await sess.start(_station("uuid-f", "Fail FM"))
            assert sess.status() == "playing"
            st._on_radio_failed()
            assert sess.status() == "failed"
            assert sess.is_active() is True   # station stays selected (R12)

    asyncio.run(_run())


# ── F2: RadioStateEvent.to_json() carries the `active` field ───────────────────


def test_f2_radio_state_event_to_json_has_active_key():
    """The JS reads !!data.active on the WS path; without an `active` field the
    widget never activates from a live push. to_json must include it, matching
    the session active state."""
    from app.events.types import RadioStateEvent

    ev = RadioStateEvent(active=True, station={"stationuuid": "x"},
                         status="playing", live_title=None)
    j = ev.to_json()
    assert "active" in j
    assert j["active"] is True
    assert j["type"] == "radio_state"

    # Default (idle) construction carries active=False, not undefined.
    j2 = RadioStateEvent().to_json()
    assert j2["active"] is False


def test_f2_broadcast_populates_active_from_snapshot(radio_env):
    """state._do_broadcast_radio_state must copy `active` from the radio snapshot
    into the RadioStateEvent (so the WS push carries the true active state)."""
    import asyncio
    import app.state as st
    from app.radio.session import RadioSession
    from app.events.types import RadioStateEvent

    async def _run():
        broadcasts: list[RadioStateEvent] = []
        manager = MagicMock()
        manager.broadcast_to_all = AsyncMock(
            side_effect=lambda ev: broadcasts.append(ev))
        sess = RadioSession(
            stop_output=AsyncMock(), hold_queue=AsyncMock(),
            has_queue_current=lambda: False, play_url=AsyncMock(),
            resume_queue=AsyncMock(), report_click=lambda u: None,
            validate_url=AsyncMock(return_value="http://stream.example/jazz"),
            teardown_stream=AsyncMock(), read_title=AsyncMock(return_value=None),
        )
        with patch("app.state.radio_session", sess), \
             patch("app.events.bus.manager", manager):
            await sess.start(_station("uuid-a", "Active FM"))
            await st._do_broadcast_radio_state()
            assert broadcasts and broadcasts[-1].active is True
            await sess.stop()
            await st._do_broadcast_radio_state()
            assert broadcasts[-1].active is False

    asyncio.run(_run())


# ── F19: countrycode search branch calls stations_by_countrycode_exact ─────────


def test_f19_stations_search_by_countrycode(anon, radio_env):
    _sess, client = radio_env
    resp = anon.get("/api/radio/stations", params={"countrycode": "US"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "search"
    assert [s["stationuuid"] for s in body["stations"]] == ["c-1"]
    client.stations_by_countrycode_exact.assert_awaited_once()


# ── F19: admin switch returns 200 + calls session.start ────────────────────────


def test_f19_admin_switch_succeeds_and_starts(admin, radio_env):
    sess, _client = radio_env
    resp = admin.post("/admin/radio/switch", json=_station_body("uuid-sw"))
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    sess.start.assert_awaited_once()
    started = sess.start.call_args.args[0]
    assert isinstance(started, Station)
    assert started.stationuuid == "uuid-sw"


# ── F19: a second consecutive mark_failed does not double-notify (idempotency) ──


def test_f19_second_mark_failed_does_not_double_notify():
    """mark_failed transitions once (fires listeners) then no-ops on the second
    call (returns False, does NOT re-notify) — the offline edge is idempotent."""
    import asyncio
    from app.radio.session import RadioSession

    async def _run():
        notifications: list[str] = []
        sess = RadioSession(
            stop_output=AsyncMock(), hold_queue=AsyncMock(),
            has_queue_current=lambda: False, play_url=AsyncMock(),
            resume_queue=AsyncMock(), report_click=lambda u: None,
            validate_url=AsyncMock(return_value="http://stream.example/jazz"),
            teardown_stream=AsyncMock(), read_title=AsyncMock(return_value=None),
        )
        await sess.start(_station("uuid-i", "Idem FM"))
        sess.add_state_listener(lambda: notifications.append(sess.status()))

        assert sess.mark_failed() is True          # real transition
        assert notifications == ["failed"]         # fired once
        assert sess.mark_failed() is False         # idempotent no-op
        assert notifications == ["failed"]         # NOT re-notified

    asyncio.run(_run())


# ── F11: StationBody preserves tags + lastcheckok through play/switch ──────────


def test_f11_station_body_preserves_tags_and_lastcheckok(anon, radio_env):
    """StationBody must carry tags + lastcheckok (not zero them via extra=ignore)
    so the snapshot/WS station dict round-trips them."""
    sess, _client = radio_env
    body = _station_body("uuid-tags")
    body["tags"] = ["jazz", "blues"]
    body["lastcheckok"] = True
    resp = anon.post("/api/radio/play", json=body)
    assert resp.status_code == 200
    started = sess.start.call_args.args[0]
    assert started.tags == ["jazz", "blues"]
    assert started.lastcheckok is True


# ── F12: the per-IP rate-limit map prunes fully-aged-out IPs ───────────────────


def test_f12_rate_limit_map_prunes_stale_ips():
    """F12: an IP whose entire window has aged out is dropped from the hit map on
    a subsequent request from ANOTHER IP — the map can't grow unbounded."""
    import app.api.radio as radio_api

    radio_api._reset_stations_rate_limit()
    # Seed a stale entry for an IP that will never come back, with timestamps far
    # in the past (fully aged out relative to the window).
    old = 0.0
    radio_api._stations_hits["1.2.3.4"] = [old, old, old]
    # A request from a different, live IP triggers the opportunistic sweep.
    radio_api._check_stations_rate_limit("9.9.9.9")
    assert "1.2.3.4" not in radio_api._stations_hits, \
        "a fully-aged-out IP key must be pruned (F12)"
    assert "9.9.9.9" in radio_api._stations_hits   # the live IP survives
    radio_api._reset_stations_rate_limit()
