"""Tests for the guest API routes."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

from app.plex.models import Track, Album, Artist, Library, SearchResults
from app.queue.engine import QueueLockError


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_track(tid="t1", server_name="My Plex") -> Track:
    return Track(id=tid, title="Song", artist="A", album="B", duration_ms=180000,
                 stream_key="/parts/1/f.flac", server_name=server_name)


def make_album(aid="a1") -> Album:
    return Album(id=aid, title="Album", artist="Artist A", year=2024)


def make_artist(arid="ar1") -> Artist:
    return Artist(id=arid, title="Artist A")


def make_library(key="1") -> Library:
    return Library(key=key, title="Music", type="artist")


def make_plex_client(
    libraries=None, artists=None, albums=None, tracks=None, search=None, styles=None
):
    client = MagicMock()
    client.get_libraries = AsyncMock(return_value=libraries or [make_library()])
    client.get_artists = AsyncMock(return_value=artists or [make_artist()])
    client.get_albums = AsyncMock(return_value=albums or [make_album()])
    client.get_tracks = AsyncMock(return_value=tracks or [make_track()])
    client.get_track = AsyncMock(return_value=make_track())
    client.get_album = AsyncMock(return_value=make_album())
    client.get_genres = AsyncMock(return_value=["Rock", "Pop"])
    client.get_styles_with_counts = AsyncMock(return_value=styles or [
        {"name": "Indie Rock", "count": 50},
        {"name": "Synth-pop", "count": 20},
    ])
    client.get_years = AsyncMock(return_value=[2023, 2024])
    client.search = AsyncMock(return_value=search or SearchResults(
        tracks=[make_track()], albums=[make_album()], artists=[make_artist()]
    ))
    # Tier-2 broad search: default empty; broad-search tests override per-case.
    client.search_titles = AsyncMock(return_value=SearchResults(
        tracks=[], albums=[], artists=[]
    ))
    # All Songs (plan 007): per-artist online popularity; default none.
    client.get_artist_popular_tracks = AsyncMock(return_value=[])
    return client


@pytest.fixture
def mock_deps():
    """Wire the guest API's external dependencies for tests.

    Replaces (under context-manager patches that auto-unwind at fixture
    teardown):
        - ``app.state.queue_engine`` → fresh empty ``QueueEngine``
        - ``app.state.get_plex_client`` → ``AsyncMock`` returning the
          ``MagicMock`` from ``make_plex_client()`` (one default library,
          one default artist/album/track per browse method)
        - ``app.database.get_enabled_libraries`` → returns one entry with
          ``section_key="1"`` so ``enabled_libraries()`` resolves to the
          single default library
        - ``app.database.get_genre_cache`` / ``set_genre_cache`` → empty +
          no-op so the genre-cache fast path is disabled and the test
          exercises the live merge logic
        - ``app.database.save_queue`` / ``save_history`` → no-ops so
          QueueEngine mutations don't try to hit a real SQLite handle

    Yields ``(queue_engine, plex_client_mock)`` so tests can assert on the
    queue's post-action state and/or override per-test mock methods on
    the plex client. The internal sanity assertion at the bottom catches
    accidental fixture stale-state — every test must start with an empty
    queue and an empty history.
    """
    from app.queue.engine import QueueEngine
    qe = QueueEngine()
    plex = make_plex_client()

    # ExitStack (not a comma-chained `with`) because each context manager in a
    # `with a, b, c, …` is a separately nested compiler block, and the full
    # dependency set now exceeds CPython's 20-nested-block limit (the
    # browse-index accessors pushed it over). ExitStack is one block.
    import contextlib
    patches = [
        patch("app.state.queue_engine", qe),
        patch("app.state.get_plex_client", AsyncMock(return_value=plex)),
        patch("app.state.trigger_credit_refresh", MagicMock()),
        patch("app.state.trigger_browse_index_refresh", MagicMock()),
        patch("app.state.trigger_artist_grouping_rebuild", MagicMock()),
        patch("app.state.get_artist_grouping", MagicMock(return_value=None)),
        patch("app.database.get_browse_artists", AsyncMock(return_value=[])),
        patch("app.database.get_browse_albums", AsyncMock(return_value=[])),
        patch("app.database.get_browse_artist_by_id", AsyncMock(return_value=None)),
        patch("app.database.get_browse_albums_for_artist", AsyncMock(return_value=[])),
        patch("app.database.get_browse_album_by_id", AsyncMock(return_value=None)),
        patch("app.database.get_browse_albums_by_identity", AsyncMock(return_value=[])),
        patch("app.database.get_enabled_libraries", AsyncMock(return_value=[{"section_key": "1"}])),
        patch("app.database.get_genre_cache", AsyncMock(return_value=[])),
        patch("app.database.set_genre_cache", AsyncMock()),
        patch("app.database.set_setting", AsyncMock()),
        patch("app.database.get_setting", AsyncMock(return_value=None)),
        patch("app.database.get_credit_acts", AsyncMock(return_value=[])),
        patch("app.database.get_credit_appearances", AsyncMock(return_value=[])),
        patch("app.database.get_pattern_rules", AsyncMock(return_value=[])),
        patch("app.database.get_artist_exclusions", AsyncMock(return_value=[])),
        patch("app.database.get_plex_servers", AsyncMock(return_value=[])),
        patch("app.database.save_queue", AsyncMock()),
        patch("app.database.save_history", AsyncMock()),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        # Sanity: fixture must start with an empty queue / history so tests
        # asserting on queue state don't accidentally pick up debris from a
        # prior failing test. A fresh QueueEngine() ALWAYS satisfies this;
        # the assertion is here so a future refactor that adds eager state
        # to the constructor fails loudly.
        assert qe.queue == [], "mock_deps must yield an empty queue"
        assert qe.history == [], "mock_deps must yield an empty history"
        yield qe, plex


@pytest.fixture
def client(mock_deps):
    from app.main import app
    return TestClient(app, raise_server_exceptions=True)


# ── Rail mode (U1 of rail-mode-toggle plan) ──────────────────────────────────


def test_rail_mode_returns_stored_value(mock_deps):
    """Stored rail_mode comes back via the public GET endpoint."""
    with patch("app.database.get_setting", AsyncMock(return_value="magnetic")):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/rail-mode")
    assert resp.status_code == 200
    assert resp.json() == {"rail_mode": "magnetic"}


def test_rail_mode_defaults_to_vanilla_when_unset(mock_deps):
    """No stored value → response defaults to 'vanilla' (2026-06-09 rail plan
    R7: fresh installs get the plain rail; stored settings are preserved)."""
    with patch("app.database.get_setting", AsyncMock(return_value=None)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/rail-mode")
    assert resp.status_code == 200
    assert resp.json() == {"rail_mode": "vanilla"}


def test_rail_mode_requires_no_auth(mock_deps):
    """GET /api/rail-mode is publicly accessible (no session cookie)."""
    with patch("app.database.get_setting", AsyncMock(return_value="density")):
        from app.main import app
        # No cookies set; this hits the guest router without auth.
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/rail-mode")
    assert resp.status_code == 200


# ── Surprise Me settings (2026-06-17 plan U3) ────────────────────────────────


@pytest.mark.parametrize("raw,expected", [(None, True), ("1", True), ("", True), ("0", False)])
def test_resolve_surprise_enabled(raw, expected):
    """Default ON: only an explicit '0' disables the button."""
    from app.api.guest import _resolve_surprise_enabled
    assert _resolve_surprise_enabled(raw) is expected


@pytest.mark.parametrize("raw,expected", [
    ("auto", "auto"), ("plex", "plex"), ("heuristic", "heuristic"),
    ("random", "random"), (None, "auto"), ("bogus", "auto"),
])
def test_resolve_surprise_mode(raw, expected):
    """Unknown/unset source mode falls back to auto."""
    from app.api.guest import _resolve_surprise_mode
    assert _resolve_surprise_mode(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("off", "off"), ("album", "album"), ("artist", "artist"),
    (None, "artist"), ("bogus", "artist"),
])
def test_resolve_surprise_diversity(raw, expected):
    """Diversity gate (plan 003): unknown/unset → artist (the default)."""
    from app.api.guest import _resolve_surprise_diversity
    assert _resolve_surprise_diversity(raw) == expected


def test_appearance_exposes_surprise_enabled_default_on(mock_deps):
    """The public /api/appearance carries surprise_me_enabled for the button's
    visibility gate; unset → on (Covers AE5)."""
    with patch("app.database.get_setting", AsyncMock(return_value=None)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/appearance")
    assert resp.status_code == 200
    assert resp.json()["surprise_me_enabled"] is True


def test_appearance_exposes_international_rail_defaults(mock_deps):
    """Public /api/appearance carries the alpha-rail mode + thresholds so the
    shared guest rail can build a data-derived rail; unset → english + 2/2.
    Admin-only settings must not leak onto this public endpoint (R9)."""
    with patch("app.database.get_setting", AsyncMock(return_value=None)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/appearance")
    assert resp.status_code == 200
    body = resp.json()
    assert body["rail_alpha_mode"] == "english"
    assert body["rail_artist_threshold"] == 2
    assert body["rail_album_threshold"] == 2
    assert "popular_random_threshold" not in body
    assert "surprise_me_source_mode" not in body


def test_appearance_exposes_stored_international_rail(mock_deps):
    """Covers AE5: an admin-saved international mode + thresholds are visible on
    the unauthenticated public endpoint the guest rail reads."""
    store = {"rail_alpha_mode": "international",
             "rail_artist_threshold": "3", "rail_album_threshold": "5"}
    async def fake_get(key, default=None): return store.get(key, default)
    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/appearance")
    assert resp.status_code == 200
    body = resp.json()
    assert body["rail_alpha_mode"] == "international"
    assert body["rail_artist_threshold"] == 3
    assert body["rail_album_threshold"] == 5


def test_appearance_surprise_disabled_when_stored_zero(mock_deps):
    """Covers AE5. Stored '0' → the public flag is False, so the button hides."""
    with patch("app.database.get_setting", AsyncMock(return_value="0")):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/appearance")
    assert resp.json()["surprise_me_enabled"] is False


def test_appearance_ratings_tags_visibility_default_off(mock_deps):
    """Plan U4/R7: guest ratings/tags visibility defaults OFF; the five Browse
    facets default ON."""
    with patch("app.database.get_setting", AsyncMock(return_value=None)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/appearance")
    body = resp.json()
    assert body["ratings_visible_to_guests"] is False
    assert body["tags_visible_to_guests"] is False
    assert body["browse_facets"] == {
        "genre": True, "years": True, "mostplayed": True,
        "recentlyadded": True, "highestrated": True,
    }


def test_appearance_reflects_stored_visibility_and_facets(mock_deps):
    """Admin-saved flags surface on the public endpoint the guest tab bar reads."""
    store = {"ratings_visible_to_guests": "1", "facet_years": "0"}
    async def fake_get(key, default=None):
        return store.get(key, default)
    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/appearance")
    body = resp.json()
    assert body["ratings_visible_to_guests"] is True
    assert body["tags_visible_to_guests"] is False     # still default
    assert body["browse_facets"]["years"] is False      # admin hid it
    assert body["browse_facets"]["genre"] is True       # others unaffected


def test_appearance_rating_style_default_stars(mock_deps):
    """Rating display style (2026-06-27 plan R4): /api/appearance defaults to
    stars on a fresh install."""
    with patch("app.database.get_setting", AsyncMock(return_value=None)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/appearance")
    assert resp.json()["rating_style"] == "stars"


def test_appearance_reflects_stored_rating_style(mock_deps):
    """A stored rating_style surfaces on the public endpoint the appearance
    engine reads; a garbage value falls back to stars."""
    store = {"rating_style": "dots"}
    async def fake_get(key, default=None):
        return store.get(key, default)
    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        assert c.get("/api/appearance").json()["rating_style"] == "dots"
    store["rating_style"] = "triangles"
    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        assert c.get("/api/appearance").json()["rating_style"] == "stars"


# ── Ratings/tags read paths + gating (2026-06-26 ratings-and-tags plan U3) ───

def test_track_ratings_withheld_from_guest_when_hidden(mock_deps):
    """Covers AE1 (server half): ratings hidden ⇒ guest gets an empty map."""
    with patch("app.database.get_ratings_visible_to_guests", AsyncMock(return_value=False)), \
         patch("app.database.get_all_ratings", AsyncMock(return_value={"t1": 5})):
        from app.main import app
        resp = TestClient(app, raise_server_exceptions=True).get("/api/track-ratings")
    assert resp.status_code == 200 and resp.json() == {}


def test_track_ratings_shown_to_guest_when_visible(mock_deps):
    with patch("app.database.get_ratings_visible_to_guests", AsyncMock(return_value=True)), \
         patch("app.database.get_all_ratings", AsyncMock(return_value={"t1": 5})):
        from app.main import app
        resp = TestClient(app, raise_server_exceptions=True).get("/api/track-ratings")
    assert resp.json() == {"t1": 5}


def test_track_ratings_shown_to_admin_even_when_hidden(mock_deps):
    """Covers AE1 (admin half): a valid session bypasses the guest gate."""
    with patch("app.database.get_ratings_visible_to_guests", AsyncMock(return_value=False)), \
         patch("app.database.get_all_ratings", AsyncMock(return_value={"t1": 5})), \
         patch("app.auth.session.validate_session", AsyncMock(return_value=True)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        c.cookies.set("jukeplox_session", "valid-token")
        resp = c.get("/api/track-ratings")
    assert resp.json() == {"t1": 5}


def test_track_tags_withheld_from_guest_when_hidden(mock_deps):
    """Covers AE2 (server half)."""
    with patch("app.database.get_tags_visible_to_guests", AsyncMock(return_value=False)), \
         patch("app.database.get_all_tags", AsyncMock(return_value={"t1": ["x"]})):
        from app.main import app
        resp = TestClient(app, raise_server_exceptions=True).get("/api/track-tags")
    assert resp.json() == {}


def test_track_tags_shown_to_guest_when_visible(mock_deps):
    with patch("app.database.get_tags_visible_to_guests", AsyncMock(return_value=True)), \
         patch("app.database.get_all_tags", AsyncMock(return_value={"t1": ["x"]})):
        from app.main import app
        resp = TestClient(app, raise_server_exceptions=True).get("/api/track-tags")
    assert resp.json() == {"t1": ["x"]}


def test_highest_rated_withheld_from_guest_when_hidden(mock_deps):
    """Covers AE1 (Highest Rated half): hidden ratings ⇒ guest leaderboard empty."""
    with patch("app.database.get_ratings_visible_to_guests", AsyncMock(return_value=False)), \
         patch("app.database.get_most_played_display_limit", AsyncMock(return_value=100)), \
         patch("app.database.get_top_rated_tracks", AsyncMock(return_value=[])):
        from app.main import app
        resp = TestClient(app, raise_server_exceptions=True).get("/api/highest-rated")
    assert resp.status_code == 200 and resp.json() == []


def test_highest_rated_shown_to_guest_when_visible(mock_deps):
    rows = [{"track_id": "t1", "stars": 5, "play_count": 3,
             "metadata": {"track_id": "t1", "title": "Song", "artist": "A"}}]
    with patch("app.database.get_ratings_visible_to_guests", AsyncMock(return_value=True)), \
         patch("app.database.get_most_played_display_limit", AsyncMock(return_value=100)), \
         patch("app.database.get_top_rated_tracks", AsyncMock(return_value=rows)):
        from app.main import app
        resp = TestClient(app, raise_server_exceptions=True).get("/api/highest-rated")
    body = resp.json()
    assert body[0]["rating"] == 5 and body[0]["title"] == "Song" and body[0]["play_count"] == 3


# ── Surprise Me endpoint (2026-06-17 plan U4) ────────────────────────────────


def _surprise_track(tid="c1"):
    return Track(id=tid, title="Surprise Pick", artist="X", album="Al",
                 duration_ms=1000, stream_key="/p/1.flac")


def test_surprise_enqueues_and_returns_source(client, mock_deps):
    """Covers AE1. A press enqueues one track and reports the resolved source."""
    qe, _ = mock_deps
    track = _surprise_track("c1")
    with patch("app.queue.surprise.resolve_surprise",
               AsyncMock(return_value=(track, "plex_sonic"))):
        resp = client.post(
            "/api/queue/surprise",
            json={"picks": [{"track_id": "s1", "genre": "Rock", "artist": "Seed"}]},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True and data["source"] == "plex_sonic"
    assert data["entry"]["track_id"] == "c1"
    assert [i.track_id for i in qe.queue] == ["c1"]


def test_surprise_disabled_returns_403(client):
    """Covers AE5 (server side). Master off → endpoint refuses, nothing queued."""
    async def fake_get(key, *a):
        return "0" if key == "surprise_me_enabled" else None
    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)):
        resp = client.post("/api/queue/surprise", json={"picks": []})
    assert resp.status_code == 403


def test_surprise_no_track_returns_ok_false(client, mock_deps):
    """Empty library → quiet no-op (ok:false), nothing appended, no error."""
    qe, _ = mock_deps
    with patch("app.queue.surprise.resolve_surprise",
               AsyncMock(return_value=(None, None))):
        resp = client.post("/api/queue/surprise", json={"picks": []})
    assert resp.status_code == 200
    assert resp.json() == {"ok": False}
    assert qe.queue == []


def test_surprise_queue_locked_returns_423(client, mock_deps):
    qe, _ = mock_deps
    qe.append = AsyncMock(side_effect=QueueLockError())
    with patch("app.queue.surprise.resolve_surprise",
               AsyncMock(return_value=(_surprise_track("c1"), "random"))):
        resp = client.post("/api/queue/surprise", json={"picks": []})
    assert resp.status_code == 423


def test_surprise_plex_not_configured_returns_503(client):
    with patch("app.state.get_plex_client", AsyncMock(return_value=None)):
        resp = client.post("/api/queue/surprise", json={"picks": []})
    assert resp.status_code == 503


def test_surprise_locked_short_circuits_before_plex_fanout(client, mock_deps):
    """Code-review #3: a locked queue 423s BEFORE resolve_surprise's Plex fan-out,
    not after — so a locked host can't be driven to run similarity queries."""
    qe, _ = mock_deps
    qe._locked = True
    resolve = AsyncMock(return_value=(_surprise_track("c1"), "random"))
    with patch("app.queue.surprise.resolve_surprise", resolve):
        resp = client.post("/api/queue/surprise", json={"picks": [{"track_id": "s1"}]})
    assert resp.status_code == 423
    resolve.assert_not_called()   # fan-out never happened


def test_surprise_drops_invalid_seed_ids(client, mock_deps):
    """Code-review #1: guest seed track_ids that fail the id pattern are dropped
    before reaching the Plex similarity calls (path-interpolation guard); valid
    ones pass through."""
    captured = {}
    async def fake_resolve(seed, mode, **kw):
        captured["seed"] = seed
        captured["exclude"] = kw.get("exclude_ids")
        return (_surprise_track("c1"), "random")
    with patch("app.queue.surprise.resolve_surprise", AsyncMock(side_effect=fake_resolve)):
        resp = client.post("/api/queue/surprise", json={
            "picks": [{"track_id": "good1"}, {"track_id": "bad id/../x"}, {"track_id": "ok:2"}],
            "exclude": ["good3", "../etc", "bad?q"],
        })
    assert resp.status_code == 200
    seed_ids = [p["track_id"] for p in captured["seed"]]
    assert seed_ids == ["good1", "ok:2"]          # malformed "bad id/../x" dropped
    assert captured["exclude"] == {"good3"}        # "../etc" and "bad?q" dropped


def test_surprise_skips_when_resolved_track_already_queued(client, mock_deps):
    """Code-review #8: if the resolved track became a duplicate between resolve and
    append (concurrent press), no-op rather than appending a back-to-back dup."""
    qe, _ = mock_deps
    qe.is_duplicate = lambda tid: True   # simulate the track now being in the queue
    with patch("app.queue.surprise.resolve_surprise",
               AsyncMock(return_value=(_surprise_track("c1"), "random"))):
        resp = client.post("/api/queue/surprise", json={"picks": []})
    assert resp.status_code == 200
    assert resp.json() == {"ok": False}
    assert qe.queue == []


def test_surprise_passes_diversity_setting_to_resolver(client, mock_deps):
    """The stored surprise_me_diversity flows into resolve_surprise (plan 003 U1)."""
    async def fake_get(key, *a):
        return {"surprise_me_diversity": "album"}.get(key)
    captured = {}
    async def fake_resolve(seed, mode, **kw):
        captured.update(kw)
        return (_surprise_track("c1"), "plex_sonic")
    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)), \
         patch("app.queue.surprise.resolve_surprise", AsyncMock(side_effect=fake_resolve)):
        resp = client.post("/api/queue/surprise", json={"picks": []})
    assert resp.status_code == 200
    assert captured.get("diversity") == "album"


def test_surprise_passes_exclude_to_resolver(client, mock_deps):
    """The browser-sent exclude list flows into resolve_surprise as exclude_ids (plan 005 U2)."""
    captured = {}
    async def fake_resolve(seed, mode, **kw):
        captured.update(kw)
        return (_surprise_track("c1"), "plex_sonic")
    with patch("app.queue.surprise.resolve_surprise", AsyncMock(side_effect=fake_resolve)):
        resp = client.post("/api/queue/surprise", json={"picks": [], "exclude": ["r1", "r2"]})
    assert resp.status_code == 200
    assert captured.get("exclude_ids") == {"r1", "r2"}


def test_surprise_broadcasts_recorded_event_to_admins(client, mock_deps):
    """A successful press pushes a surprise_recorded event (with the tally) to
    admins so the Recent-suggestions readout updates live, no reload (ce-debug)."""
    from app.queue import surprise as sm
    sm._RECENT_SOURCES.clear()
    sent = []
    async def fake_broadcast(event):
        sent.append(event)
    with patch("app.queue.surprise.resolve_surprise",
               AsyncMock(return_value=(_surprise_track("c1"), "plex_sonic"))), \
         patch("app.events.bus.manager.broadcast_to_admins", AsyncMock(side_effect=fake_broadcast)):
        resp = client.post("/api/queue/surprise", json={"picks": []})
    assert resp.status_code == 200
    ev = next((e for e in sent if getattr(e, "type", None) == "surprise_recorded"), None)
    assert ev is not None, "expected a surprise_recorded broadcast to admins"
    assert ev.tally.get("plex_sonic") == 1


# ── Color schemes ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "scheme_id",
    ["dark-side", "bloody-pink", "tubular-blue", "peel-slowly", "inertia", "medusa"],
)
def test_resolve_scheme_accepts_new_ids(scheme_id):
    """The six 2026-06-14 expansion schemes resolve to themselves (AE1)."""
    from app.api.guest import _resolve_scheme
    assert _resolve_scheme(scheme_id) == scheme_id


@pytest.mark.parametrize("raw", [None, "", "ladyland", "bogus-scheme", "DARK-SIDE"])
def test_resolve_scheme_unknown_and_legacy_fall_back_to_gold(raw):
    """Unknown/None and the legacy 'ladyland' (renamed display only; id stayed
    'ladyland-orange') fall back to the gold default — never a hard error (AE1)."""
    from app.api.guest import _resolve_scheme
    assert _resolve_scheme(raw) == "gold-rush"


def test_scheme_ids_lockstep_with_frontend_table():
    """SCHEME_IDS (server canon) must equal the APPEARANCE_SCHEMES keys in
    static/shared.js exactly — a scheme added to one side only is a cross-layer
    drift bug (R9). Ids are kept stable on rename (D5), so this is set equality."""
    import re
    from pathlib import Path
    from app.api.guest import SCHEME_IDS

    shared_js = (
        Path(__file__).resolve().parents[1] / "static" / "shared.js"
    ).read_text(encoding="utf-8")
    # Each APPEARANCE_SCHEMES row is `'<id>': { name: …` — a shape unique to the
    # scheme table (rail modes use `{ id: '…' }`, no quoted-key-colon).
    frontend_ids = set(re.findall(r"^\s*'([a-z0-9-]+)':\s*\{\s*name:", shared_js, re.M))
    assert frontend_ids, "No APPEARANCE_SCHEMES rows parsed — pattern drift?"
    assert frontend_ids == set(SCHEME_IDS), (
        f"Scheme lockstep drift: only in shared.js: {frontend_ids - set(SCHEME_IDS)}; "
        f"only in SCHEME_IDS: {set(SCHEME_IDS) - frontend_ids}. Register every "
        f"scheme in BOTH static/shared.js APPEARANCE_SCHEMES and "
        f"app/api/guest.py SCHEME_IDS."
    )


# ── Broad search (Tier 2) ──────────────────────────────────────────────────────


def test_search_broad_returns_title_results(mock_deps):
    """Broad endpoint fans search_titles and returns its tracks, shaped like the
    main search payload (covers AE2 at the API layer)."""
    qe, plex = mock_deps
    plex.search_titles = AsyncMock(return_value=SearchResults(
        tracks=[make_track()], albums=[], artists=[]))
    from app.main import app
    c = TestClient(app, raise_server_exceptions=True)
    resp = c.get("/api/search/broad", params={"q": "Cicada", "types": "track"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["tracks"]) == 1 and body["albums"] == []
    assert plex.search_titles.await_args.kwargs["types"] == ("track",)


def test_search_broad_respects_album_filter(mock_deps):
    """AE5: types=album fetches only albums."""
    qe, plex = mock_deps
    plex.search_titles = AsyncMock(return_value=SearchResults(
        tracks=[], albums=[make_album()], artists=[]))
    from app.main import app
    c = TestClient(app, raise_server_exceptions=True)
    body = c.get("/api/search/broad", params={"q": "Cicada", "types": "album"}).json()
    assert body["tracks"] == [] and len(body["albums"]) >= 1
    assert plex.search_titles.await_args.kwargs["types"] == ("album",)


def test_search_broad_paging_advances_offset(mock_deps):
    """page=N requests the Nth slab (start = N × page size)."""
    qe, plex = mock_deps
    plex.search_titles = AsyncMock(return_value=SearchResults(tracks=[], albums=[], artists=[]))
    from app.main import app
    c = TestClient(app, raise_server_exceptions=True)
    c.get("/api/search/broad", params={"q": "Cicada", "types": "track", "page": 1})
    assert plex.search_titles.await_args.kwargs["start"] == 30  # 1 × _BROAD_PAGE_SIZE


def test_search_broad_unknown_types_skips_plex(mock_deps):
    """Only track/album are valid broad types; anything else returns empty
    without touching Plex (artists/genres stay Tier 1's)."""
    qe, plex = mock_deps
    plex.search_titles = AsyncMock(return_value=SearchResults(tracks=[], albums=[], artists=[]))
    from app.main import app
    c = TestClient(app, raise_server_exceptions=True)
    resp = c.get("/api/search/broad", params={"q": "Cicada", "types": "artist"})
    assert resp.json() == {"tracks": [], "albums": []}
    plex.search_titles.assert_not_awaited()


# ── Now playing ───────────────────────────────────────────────────────────────

async def test_now_playing_idle(client, mock_deps):
    resp = client.get("/api/now-playing")
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_playing"] is False


async def test_now_playing_closing_default_inactive(client, mock_deps):
    """Closing Time (U3): idle snapshot carries the (inactive) closing fields."""
    data = client.get("/api/now-playing").json()
    assert data["closing_active"] is False
    assert data["closing_message"] == ""


async def test_now_playing_includes_closing_state(client, mock_deps, monkeypatch):
    """U3: a late-joining guest learns the banner state from the GET (current is
    cleared by the freeze, so this rides the no-current branch)."""
    import app.state as st
    monkeypatch.setattr(st, "_closing_active", True)
    monkeypatch.setattr(st, "_closing_message", "Last call")
    data = client.get("/api/now-playing").json()
    assert data["closing_active"] is True
    assert data["closing_message"] == "Last call"


# ── Queue display ─────────────────────────────────────────────────────────────

async def test_get_queue_empty(client, mock_deps):
    resp = client.get("/api/queue")
    assert resp.status_code == 200
    data = resp.json()
    assert data["queue"] == []
    assert data["is_locked"] is False


async def test_get_queue_respects_n_limit(client, mock_deps):
    qe, _ = mock_deps
    for i in range(10):
        await qe.append(make_track(f"t{i}"), bypass_lock=True)
    from app.events.bus import manager
    manager.guest_n = 3
    try:
        resp = client.get("/api/queue")
        assert len(resp.json()["queue"]) == 3
    finally:
        manager.guest_n = None


async def test_get_queue_exposes_added_at_for_receipt_matching(client, mock_deps):
    """U1: each upcoming queue item carries added_at (the per-entry half of the
    append receipt) so the guest UI can match its stored receipts and show a ✕
    on entries this browser queued. History stays Track-only (not removable)."""
    qe, _ = mock_deps
    await qe.append(make_track("t1"), bypass_lock=True)
    await qe.append(make_track("t2"), bypass_lock=True)
    data = client.get("/api/queue").json()
    assert len(data["queue"]) == 2
    for item, engine_item in zip(data["queue"], qe.queue):
        assert item["added_at"] == engine_item.added_at
        assert item["added_at"]  # non-empty
        assert item["track_id"] == engine_item.track_id  # existing field intact
    # History does not gain added_at — removal is upcoming-only.
    qe._history.append(qe.queue[0])
    hist = client.get("/api/queue").json()["history"]
    assert hist and "added_at" not in hist[0]


async def test_get_queue_empty_has_no_items(client, mock_deps):
    """U1: empty queue still serializes cleanly (no added_at merge error)."""
    resp = client.get("/api/queue")
    assert resp.status_code == 200
    assert resp.json()["queue"] == []


# ── Browse ────────────────────────────────────────────────────────────────────

def test_browse_artists(client, mock_deps):
    resp = client.get("/api/browse/artists")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1


def test_browse_artists_deduplicates(mock_deps):
    _, plex = mock_deps
    plex.get_artists.return_value = [
        Artist(id="s1:10", title="Prince"),
        Artist(id="s2:10", title="Prince"),   # duplicate from second library
        Artist(id="s1:11", title="The Cure"),
    ]
    from app.main import app
    c = TestClient(app, raise_server_exceptions=True)
    resp = c.get("/api/browse/artists")
    assert resp.status_code == 200
    data = resp.json()
    titles = [a["title"] for a in data]
    assert titles.count("Prince") == 1
    assert "The Cure" in titles


def test_browse_albums(client, mock_deps):
    resp = client.get("/api/browse/albums")
    assert resp.status_code == 200
    assert len(resp.json()) >= 1


def test_browse_artists_all_libraries_failed_returns_503(mock_deps, caplog):
    """When every enabled library raises (e.g., Plex server down across the
    board), browse_artists must escalate to 503 — returning an empty 200 OK
    would render as "no artists found" in the UI, masking the outage."""
    import logging
    _, plex = mock_deps
    plex.get_artists.side_effect = RuntimeError("plex unreachable")

    with patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        with caplog.at_level(logging.WARNING, logger="app.api.guest"):
            resp = c.get("/api/browse/artists")

    assert resp.status_code == 503
    # Per-lib failures logged so ops can see which server is down.
    assert any("Host" in rec.message for rec in caplog.records)
    assert any("Shared" in rec.message for rec in caplog.records)


def test_browse_artists_partial_failure_still_returns_200(mock_deps):
    """When ONE library fails but another succeeds, response is 200 with the
    surviving library's contribution — no 503 escalation."""
    _, plex = mock_deps

    async def fake_get_artists(section_key):
        if section_key == "machineA:1":
            return [Artist(id="machineA:42", title="Prince")]
        raise RuntimeError("plex unreachable")

    plex.get_artists.side_effect = fake_get_artists

    with patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/artists")

    assert resp.status_code == 200
    titles = [a["title"] for a in resp.json()]
    assert "Prince" in titles


def test_browse_albums_all_libraries_failed_returns_503(mock_deps):
    _, plex = mock_deps
    plex.get_albums.side_effect = RuntimeError("plex unreachable")

    with patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/albums")

    assert resp.status_code == 503


# ── Browse index serving (2026-06-21 plan U3) ────────────────────────────────

def _artist_idx_row(aid, title, server="ServerA", **kw):
    return {"artist_id": aid, "title": title, "base_key": title.lower().strip(),
            "thumb": kw.get("thumb"), "release_count": kw.get("release_count"),
            "server_name": server, "section_key": "A:1"}


def _album_idx_row(aid, title, artist, server="ServerA", **kw):
    return {"album_id": aid, "title": title, "title_base": title.lower().strip(),
            "artist": artist, "artist_base_key": artist.lower().strip(),
            "year": kw.get("year"), "thumb": kw.get("thumb"),
            "subtype": kw.get("subtype"), "track_count": kw.get("track_count"),
            "section_key": kw.get("section_key", "A:1"), "server_name": server}


def test_browse_artists_warm_index_no_plex_fanout(mock_deps):
    """Warm index serves the roster with ZERO live Plex work."""
    _, plex = mock_deps
    rows = [_artist_idx_row("A:1", "Radiohead", release_count=9),
            _artist_idx_row("B:1", "Björk", server="ServerB")]
    with patch("app.database.get_browse_artists", AsyncMock(return_value=rows)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/artists")
    assert resp.status_code == 200
    titles = {a["title"] for a in resp.json()}
    assert {"Radiohead", "Björk"} <= titles
    plex.get_artists.assert_not_called()  # served entirely from the index


def test_browse_artists_empty_index_falls_back_and_triggers_build(mock_deps):
    """Cold index → live fan-out this time AND a background rebuild is scheduled."""
    with patch("app.state.trigger_browse_index_refresh", MagicMock()) as trig:
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/artists")
    assert resp.status_code == 200
    assert len(resp.json()) >= 1       # served live (fallback)
    trig.assert_called_once()          # self-heal scheduled


def test_browse_artists_exclusion_applies_on_warm_index_without_rebuild(mock_deps):
    """AE5: an artist-exclusion edit takes effect at request time over the warm
    index with NO index rebuild."""
    import datetime
    fresh = datetime.datetime.now(datetime.timezone.utc).isoformat()
    rows = [_artist_idx_row("A:1", "Radiohead"), _artist_idx_row("A:2", "Banned Act")]

    async def _get_setting(key, default=None):
        return fresh if key == "browse_index_computed_at" else None

    with patch("app.database.get_browse_artists", AsyncMock(return_value=rows)), \
         patch("app.database.get_setting", AsyncMock(side_effect=_get_setting)), \
         patch("app.database.get_artist_exclusions", AsyncMock(return_value=["Banned Act"])), \
         patch("app.state.trigger_browse_index_refresh", MagicMock()) as trig:
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/artists")
    titles = {a["title"] for a in resp.json()}
    assert "Radiohead" in titles
    assert "Banned Act" not in titles  # excluded at request time
    trig.assert_not_called()           # fresh index → no rebuild needed


def test_browse_albums_warm_index_groups_cross_server_no_fanout(mock_deps):
    """Warm index serves albums tagged by server; the existing cross-server
    grouping collapses per-server copies into one row — no Plex fan-out."""
    _, plex = mock_deps
    rows = [_album_idx_row("A:10", "Kid A", "Radiohead", server="ServerA"),
            _album_idx_row("B:55", "Kid A", "Radiohead", server="ServerB")]
    with patch("app.database.get_browse_albums", AsyncMock(return_value=rows)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/albums")
    assert resp.status_code == 200
    kid_a = [d for d in resp.json() if d["title"] == "Kid A"]
    assert len(kid_a) == 1             # collapsed across servers
    plex.get_albums.assert_not_called()


def test_browse_albums_warm_index_exposes_track_count(mock_deps):
    """U3: track_count from the index surfaces on the album response — the data
    the disambiguator (U6) and content-aware grouping (U4) read."""
    rows = [_album_idx_row("A:10", "Loveless", "My Bloody Valentine",
                           server="ServerA", track_count=11)]
    with patch("app.database.get_browse_albums", AsyncMock(return_value=rows)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/albums")
    assert resp.status_code == 200
    alb = [d for d in resp.json() if d["title"] == "Loveless"]
    assert len(alb) == 1 and alb[0]["track_count"] == 11


def test_group_albums_distinct_counts_get_own_sources(mock_deps):
    """U4/AE2: same-title releases with different track counts in one library are
    separate rows, each with sources pointing at its OWN copy — not the first
    copy of the title (the old title-only grouping mis-pointed both rows)."""
    rows = [_album_idx_row("A:US", "Further Down the Spiral", "Nine Inch Nails",
                           server="ServerA", track_count=13),
            _album_idx_row("A:JP", "Further Down the Spiral", "Nine Inch Nails",
                           server="ServerA", track_count=15)]
    with patch("app.database.get_browse_albums", AsyncMock(return_value=rows)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/albums")
    fds = [d for d in resp.json() if d["title"] == "Further Down the Spiral"]
    assert len(fds) == 2
    by_count = {d["track_count"]: d for d in fds}
    assert by_count[13]["sources"][0]["album_id"] == "A:US"
    assert by_count[15]["sources"][0]["album_id"] == "A:JP"


def test_group_albums_release_only_on_shared_server_not_hidden(mock_deps):
    """U4/AE3/R5: a count-distinct release present only on the non-priority
    (shared) server still gets its own row — never hidden behind the priority
    server's same-title copies."""
    rows = [_album_idx_row("A:JP", "Further Down the Spiral", "Nine Inch Nails",
                           server="ServerA", track_count=15),
            _album_idx_row("B:JP", "Further Down the Spiral", "Nine Inch Nails",
                           server="ServerB", track_count=15),
            _album_idx_row("B:US", "Further Down the Spiral", "Nine Inch Nails",
                           server="ServerB", track_count=13)]
    with patch("app.database.get_browse_albums", AsyncMock(return_value=rows)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/albums")
    fds = [d for d in resp.json() if d["title"] == "Further Down the Spiral"]
    assert sorted(d["track_count"] for d in fds) == [13, 15]  # US (shared-only) kept


def test_group_albums_same_server_same_count_keeps_repeats(mock_deps):
    """U4/AE1/R2: three same-title same-count copies on one server stay three
    rows (the deliberate masters case)."""
    rows = [_album_idx_row(f"A:{i}", "Loveless", "My Bloody Valentine",
                           server="ServerA", track_count=11) for i in range(3)]
    with patch("app.database.get_browse_albums", AsyncMock(return_value=rows)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/albums")
    assert len([d for d in resp.json() if d["title"] == "Loveless"]) == 3


def test_group_albums_genuine_shared_album_still_folds(mock_deps):
    """U4 no-regression: one album, same count on two servers, collapses to a
    single row carrying both servers as sources."""
    rows = [_album_idx_row("A:1", "OK Computer", "Radiohead", server="ServerA",
                           track_count=12),
            _album_idx_row("B:1", "OK Computer", "Radiohead", server="ServerB",
                           track_count=12)]
    with patch("app.database.get_browse_albums", AsyncMock(return_value=rows)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/albums")
    okc = [d for d in resp.json() if d["title"] == "OK Computer"]
    assert len(okc) == 1
    assert {s["server_name"] for s in okc[0]["sources"]} == {"ServerA", "ServerB"}


# ── U5: per-release track resolution (index path) ────────────────────────────

def _idx_arow(aid, server="ServerA", count=11, **kw):
    return {"album_id": aid, "title": "Loveless", "title_base": "loveless",
            "artist": "My Bloody Valentine", "artist_base_key": "my bloody valentine",
            "year": 1991, "thumb": None, "subtype": None, "added_at": None,
            "track_count": count, "server_name": server,
            "section_key": kw.get("section_key", server + ":1")}


def test_resolve_tracks_same_server_siblings_not_unioned(mock_deps):
    """U5/AE1: clicking one of three same-title same-server masters returns ONLY
    that master's tracks — the same-server union (tripling) is gone."""
    _, plex = mock_deps
    arow = _idx_arow("A:1")
    copies = [arow, _idx_arow("A:2"), _idx_arow("A:3")]

    async def fake_get_tracks(section_key, album_id=None, **kw):
        return [_t(f"{album_id}:t1", f"{album_id} Track 1", server_name="ServerA")]
    plex.get_tracks.side_effect = fake_get_tracks

    with patch("app.database.get_browse_album_by_id", AsyncMock(return_value=arow)), \
         patch("app.database.get_browse_albums_by_identity", AsyncMock(return_value=copies)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/albums/A:1/tracks")
    assert resp.status_code == 200
    assert [t["track_id"] for t in resp.json()] == ["A:1:t1"]   # not A:2 / A:3


def test_resolve_tracks_cross_server_folds_only_count_match(mock_deps):
    """U5/AE2: clicking the US edition unions its own tracks with the SAME-count
    copy on the other server, never the same-server JP sibling or the other
    server's mismatched-count copy."""
    _, plex = mock_deps
    arow = {**_idx_arow("A:US", count=13), "title": "FDS", "title_base": "fds"}
    copies = [
        arow,
        {**arow, "album_id": "A:JP", "track_count": 15},                       # same-server sibling
        {**arow, "album_id": "B:US", "track_count": 13, "server_name": "ServerB", "section_key": "B:1"},
        {**arow, "album_id": "B:JP", "track_count": 15, "server_name": "ServerB", "section_key": "B:1"},
    ]

    async def fake_get_tracks(section_key, album_id=None, **kw):
        return [_t(f"{album_id}:t1", f"{album_id} Track 1", server_name="S")]
    plex.get_tracks.side_effect = fake_get_tracks

    with patch("app.database.get_browse_album_by_id", AsyncMock(return_value=arow)), \
         patch("app.database.get_browse_albums_by_identity", AsyncMock(return_value=copies)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/albums/A:US/tracks")
    assert resp.status_code == 200
    assert sorted(t["track_id"] for t in resp.json()) == ["A:US:t1", "B:US:t1"]


def test_resolve_tracks_ambiguous_cross_server_falls_back_to_own(mock_deps):
    """U5/AE4: when the other server has multiple same-count copies (identical
    masters), the mapping is ambiguous — fall back to the clicked copy's own
    tracks rather than guess."""
    _, plex = mock_deps
    arow = _idx_arow("A:1", count=11)
    copies = [arow,
              _idx_arow("B:1", server="ServerB", count=11),
              _idx_arow("B:2", server="ServerB", count=11),
              _idx_arow("B:3", server="ServerB", count=11)]

    async def fake_get_tracks(section_key, album_id=None, **kw):
        return [_t(f"{album_id}:t1", f"{album_id} Track 1", server_name="S")]
    plex.get_tracks.side_effect = fake_get_tracks

    with patch("app.database.get_browse_album_by_id", AsyncMock(return_value=arow)), \
         patch("app.database.get_browse_albums_by_identity", AsyncMock(return_value=copies)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/albums/A:1/tracks")
    assert resp.status_code == 200
    assert [t["track_id"] for t in resp.json()] == ["A:1:t1"]   # B skipped (ambiguous)


def test_resolve_tracks_genuine_shared_album_still_unions(mock_deps):
    """U5 no-regression: a single album genuinely shared across two servers (one
    unique count-match each) still unions both servers' tracks."""
    _, plex = mock_deps
    arow = _idx_arow("A:1", count=10)
    copies = [arow, _idx_arow("B:1", server="ServerB", count=10)]

    async def fake_get_tracks(section_key, album_id=None, **kw):
        return [_t(f"{album_id}:t1", f"{album_id} Track 1", server_name="S")]
    plex.get_tracks.side_effect = fake_get_tracks

    with patch("app.database.get_browse_album_by_id", AsyncMock(return_value=arow)), \
         patch("app.database.get_browse_albums_by_identity", AsyncMock(return_value=copies)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/albums/A:1/tracks")
    assert resp.status_code == 200
    assert sorted(t["track_id"] for t in resp.json()) == ["A:1:t1", "B:1:t1"]


def test_resolve_tracks_live_fallback_excludes_same_library_siblings(mock_deps):
    """U5: on a cold index (live fallback) the same-library same-title siblings
    are still not unioned — only the clicked album's tracks return."""
    _, plex = mock_deps
    plex.get_album = AsyncMock(return_value=Album(
        id="machineA:1", title="Loveless", artist="MBV", track_count=11))

    async def fake_get_artists(section_key):
        return [Artist(id="machineA:42", title="MBV")] if section_key == "machineA:1" else []

    async def fake_get_albums(section_key, artist_id=None, **kw):
        if section_key == "machineA:1":
            return [Album(id=f"machineA:{i}", title="Loveless", artist="MBV", track_count=11)
                    for i in (1, 2, 3)]
        return []

    async def fake_get_tracks(section_key, album_id=None, **kw):
        return [_t(f"{album_id}:t1", "T1", server_name="Host")]

    plex.get_artists.side_effect = fake_get_artists
    plex.get_albums.side_effect = fake_get_albums
    plex.get_tracks.side_effect = fake_get_tracks

    with patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/albums/machineA:1/tracks")
    assert resp.status_code == 200
    assert [t["track_id"] for t in resp.json()] == ["machineA:1:t1"]  # not :2 / :3


def test_browse_album_tracks(client, mock_deps):
    resp = client.get("/api/browse/albums/a1/tracks")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 1


# ── Multi-library browse: artist → albums (U2) ───────────────────────────────

def _two_libs():
    """Library 0 (host, machineA) + Library 1 (shared, machineB).

    Each carries a distinct `server_name` so source-filter tests can
    name-match them (matches what production MultiPlexClient sets when
    libraries come from different Plex servers).
    """
    return [
        Library(key="machineA:1", title="Host", type="artist", server_name="Host"),
        Library(key="machineB:2", title="Shared", type="artist", server_name="Shared"),
    ]


def test_browse_artist_albums_unions_across_libraries(mock_deps):
    """Covers AE1. Both libraries contribute albums for a shared artist;
    response is the dedup'd union of (Album A from both, Album B from host,
    Album C from shared)."""
    _, plex = mock_deps

    async def fake_get_artists(section_key):
        if section_key == "machineA:1":
            return [Artist(id="machineA:42", title="Prince")]
        if section_key == "machineB:2":
            return [Artist(id="machineB:78", title="Prince")]
        return []

    async def fake_get_albums(section_key, artist_id=None, **kw):
        if section_key == "machineA:1" and artist_id == "machineA:42":
            return [Album(id="machineA:100", title="Album A", artist="Prince"),
                    Album(id="machineA:101", title="Album B", artist="Prince")]
        if section_key == "machineB:2" and artist_id == "machineB:78":
            return [Album(id="machineB:200", title="Album A", artist="Prince"),
                    Album(id="machineB:201", title="Album C", artist="Prince")]
        return []

    plex.get_artists.side_effect = fake_get_artists
    plex.get_albums.side_effect = fake_get_albums

    with patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/artists/machineA:42/albums")

    assert resp.status_code == 200
    data = resp.json()
    titles = sorted([a["title"] for a in data])
    assert titles == ["Album A", "Album B", "Album C"]


def test_browse_artist_albums_one_library_no_matching_artist(mock_deps):
    """Covers AE2. Artist exists only in lib_0; lib_1 returns no matching artist.
    Response is just lib_0's albums; get_albums not called for lib_1."""
    _, plex = mock_deps
    albums_calls: list[tuple[str, str | None]] = []

    async def fake_get_artists(section_key):
        if section_key == "machineA:1":
            return [Artist(id="machineA:42", title="Prince")]
        return [Artist(id="machineB:99", title="The Cure")]  # different artist

    async def fake_get_albums(section_key, artist_id=None, **kw):
        albums_calls.append((section_key, artist_id))
        if section_key == "machineA:1":
            return [Album(id="machineA:100", title="Album A", artist="Prince"),
                    Album(id="machineA:101", title="Album B", artist="Prince")]
        return []

    plex.get_artists.side_effect = fake_get_artists
    plex.get_albums.side_effect = fake_get_albums

    with patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/artists/machineA:42/albums")

    assert resp.status_code == 200
    titles = sorted([a["title"] for a in resp.json()])
    assert titles == ["Album A", "Album B"]
    # get_albums called only for machineA:1
    assert ("machineA:1", "machineA:42") in albums_calls
    assert all(call[0] != "machineB:2" for call in albums_calls)


def test_browse_artist_albums_unknown_id_returns_404(mock_deps):
    _, plex = mock_deps
    plex.get_artists.side_effect = AsyncMock(return_value=[Artist(id="machineA:42", title="Prince")])

    with patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/artists/machineA:9999/albums")

    assert resp.status_code == 404


def test_browse_artist_albums_no_plex_returns_503():
    with patch("app.state.get_plex_client", AsyncMock(return_value=None)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/artists/machineA:42/albums")
    assert resp.status_code == 503


def test_browse_artist_albums_one_library_get_artists_fails(mock_deps):
    """When one library's get_artists raises, response still includes the
    succeeding library's contribution."""
    _, plex = mock_deps

    async def fake_get_artists(section_key):
        if section_key == "machineA:1":
            return [Artist(id="machineA:42", title="Prince")]
        raise RuntimeError("plex unreachable")

    async def fake_get_albums(section_key, artist_id=None, **kw):
        return [Album(id="machineA:100", title="Album A", artist="Prince")]

    plex.get_artists.side_effect = fake_get_artists
    plex.get_albums.side_effect = fake_get_albums

    with patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/artists/machineA:42/albums")

    assert resp.status_code == 200
    titles = [a["title"] for a in resp.json()]
    assert "Album A" in titles


def test_browse_artist_albums_one_library_get_albums_fails(mock_deps):
    """When a matched library's get_albums raises, response still includes the
    other library's contribution."""
    _, plex = mock_deps

    async def fake_get_artists(section_key):
        if section_key == "machineA:1":
            return [Artist(id="machineA:42", title="Prince")]
        return [Artist(id="machineB:78", title="Prince")]

    async def fake_get_albums(section_key, artist_id=None, **kw):
        if section_key == "machineA:1":
            return [Album(id="machineA:100", title="Album A", artist="Prince")]
        raise RuntimeError("plex unreachable")

    plex.get_artists.side_effect = fake_get_artists
    plex.get_albums.side_effect = fake_get_albums

    with patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/artists/machineA:42/albums")

    assert resp.status_code == 200
    titles = [a["title"] for a in resp.json()]
    assert titles == ["Album A"]


def test_browse_artist_albums_case_insensitive_cross_library_match(mock_deps):
    """'The Beatles' in lib_0 and 'the beatles' in lib_1 are merged via the
    lower-cased dedup key."""
    _, plex = mock_deps

    async def fake_get_artists(section_key):
        if section_key == "machineA:1":
            return [Artist(id="machineA:42", title="The Beatles")]
        return [Artist(id="machineB:78", title="the beatles")]

    async def fake_get_albums(section_key, artist_id=None, **kw):
        if section_key == "machineA:1":
            return [Album(id="machineA:100", title="Abbey Road", artist="The Beatles")]
        if section_key == "machineB:2":
            return [Album(id="machineB:200", title="Revolver", artist="the beatles")]
        return []

    plex.get_artists.side_effect = fake_get_artists
    plex.get_albums.side_effect = fake_get_albums

    with patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/artists/machineA:42/albums")

    assert resp.status_code == 200
    titles = sorted([a["title"] for a in resp.json()])
    assert titles == ["Abbey Road", "Revolver"]


def test_browse_artist_albums_whitespace_tolerance(mock_deps):
    """Trimmed comparison: '  Prince ' merges with 'Prince'."""
    _, plex = mock_deps

    async def fake_get_artists(section_key):
        if section_key == "machineA:1":
            return [Artist(id="machineA:42", title="  Prince ")]
        return [Artist(id="machineB:78", title="Prince")]

    async def fake_get_albums(section_key, artist_id=None, **kw):
        if section_key == "machineA:1":
            return [Album(id="machineA:100", title="Album A", artist="Prince")]
        return [Album(id="machineB:200", title="Album C", artist="Prince")]

    plex.get_artists.side_effect = fake_get_artists
    plex.get_albums.side_effect = fake_get_albums

    with patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/artists/machineA:42/albums")

    assert resp.status_code == 200
    titles = sorted([a["title"] for a in resp.json()])
    assert titles == ["Album A", "Album C"]


def test_browse_artist_albums_matched_library_returns_no_albums(mock_deps):
    """If lib_1 matches by artist name but its get_albums returns [], response
    is just lib_0's albums."""
    _, plex = mock_deps

    async def fake_get_artists(section_key):
        if section_key == "machineA:1":
            return [Artist(id="machineA:42", title="Prince")]
        return [Artist(id="machineB:78", title="Prince")]

    async def fake_get_albums(section_key, artist_id=None, **kw):
        if section_key == "machineA:1":
            return [Album(id="machineA:100", title="Album A", artist="Prince")]
        return []

    plex.get_artists.side_effect = fake_get_artists
    plex.get_albums.side_effect = fake_get_albums

    with patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/artists/machineA:42/albums")

    assert resp.status_code == 200
    titles = [a["title"] for a in resp.json()]
    assert titles == ["Album A"]


def test_browse_artist_albums_invalid_id_returns_400(client, mock_deps):
    """validate_plex_id rejects malformed ids before any library queries."""
    resp = client.get("/api/browse/artists/not%20a%20valid%20id/albums")
    assert resp.status_code == 400


# ── Artist → All Songs (plan 007 U2) ─────────────────────────────────────────

def _one_lib():
    return [Library(key="machineA:1", title="Host", type="artist", server_name="Host")]


def test_browse_artist_songs_enriches_own_tracks(mock_deps):
    """Covers R3/R10/AE1. Own-release tracks carry local plays + title-matched
    pop_rank; popular_available true."""
    _, plex = mock_deps

    async def fake_artists(section_key):
        return [Artist(id="machineA:42", title="Prince")] if section_key == "machineA:1" else []

    async def fake_albums(section_key, artist_id=None, **kw):
        if section_key == "machineA:1" and artist_id == "machineA:42":
            return [Album(id="machineA:100", title="Album A", artist="Prince", year=1999)]
        return []

    async def fake_tracks(section_key, album_id=None, **kw):
        if album_id == "machineA:100":
            return [Track(id="machineA:t1", title="Kiss", artist="Prince", album="Album A", duration_ms=1, stream_key="/k", server_name="Host"),
                    Track(id="machineA:t2", title="1999", artist="Prince", album="Album A", duration_ms=1, stream_key="/k", server_name="Host")]
        return []

    plex.get_artists.side_effect = fake_artists
    plex.get_albums.side_effect = fake_albums
    plex.get_tracks.side_effect = fake_tracks
    plex.get_artist_popular_tracks = AsyncMock(return_value=[
        {"title": "1999", "rating_key": "x"}, {"title": "Kiss", "rating_key": "y"}])

    with patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_one_lib())), \
         patch("app.database.get_play_counts", AsyncMock(return_value={"machineA:t1": 12})):
        from app.main import app
        resp = TestClient(app, raise_server_exceptions=True).get("/api/browse/artists/machineA:42/songs")

    assert resp.status_code == 200
    data = resp.json()
    assert data["popular_available"] is True
    by_title = {t["title"]: t for t in data["tracks"]}
    assert set(by_title) == {"Kiss", "1999"}
    assert by_title["Kiss"]["plays"] == 12 and by_title["1999"]["plays"] == 0
    assert by_title["1999"]["pop_rank"] == 0 and by_title["Kiss"]["pop_rank"] == 1
    assert all(t["kind"] == "own" for t in data["tracks"])


def test_browse_artist_songs_filters_appears_on_to_artist(mock_deps):
    """Covers R3 (filter). A Various-Artists comp the artist appears on
    contributes ONLY the artist's track, not the other artists' tracks."""
    _, plex = mock_deps

    async def fake_artists(section_key):
        return [Artist(id="machineA:42", title="Prince")] if section_key == "machineA:1" else []

    async def fake_albums(section_key, artist_id=None, **kw):
        return []  # purely appears-on

    async def fake_tracks(section_key, album_id=None, **kw):
        if album_id == "machineA:900":
            return [Track(id="machineA:c1", title="Prince Cut", artist="Prince", album="VA Comp", duration_ms=1, stream_key="/k", server_name="Host"),
                    Track(id="machineA:c2", title="Other One", artist="Some Other Band", album="VA Comp", duration_ms=1, stream_key="/k", server_name="Host"),
                    Track(id="machineA:c3", title="Third Thing", artist="Third Act", album="VA Comp", duration_ms=1, stream_key="/k", server_name="Host")]
        return []

    plex.get_artists.side_effect = fake_artists
    plex.get_albums.side_effect = fake_albums
    plex.get_tracks.side_effect = fake_tracks

    with patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_one_lib())), \
         patch("app.database.get_credit_acts", AsyncMock(return_value=[{"name": "Prince", "name_lower": "prince"}])), \
         patch("app.database.get_credit_appearances", AsyncMock(return_value=[
             {"album_id": "machineA:900", "album_title": "VA Comp", "album_artist": "Various Artists", "album_year": 1990, "album_thumb": None}])), \
         patch("app.database.get_play_counts", AsyncMock(return_value={})):
        from app.main import app
        resp = TestClient(app, raise_server_exceptions=True).get("/api/browse/artists/machineA:42/songs")

    assert resp.status_code == 200
    data = resp.json()
    assert [t["title"] for t in data["tracks"]] == ["Prince Cut"]
    assert data["tracks"][0]["kind"] == "appears"


def test_browse_artist_songs_popular_unavailable(mock_deps):
    """Covers R7/AE2. No matching popular leaves → popular_available false, pop_rank None."""
    _, plex = mock_deps

    async def fake_artists(section_key):
        return [Artist(id="machineA:42", title="Prince")] if section_key == "machineA:1" else []

    async def fake_albums(section_key, artist_id=None, **kw):
        return [Album(id="machineA:100", title="Album A", artist="Prince", year=1999)] if artist_id == "machineA:42" else []

    async def fake_tracks(section_key, album_id=None, **kw):
        if album_id == "machineA:100":
            return [Track(id="machineA:t1", title="Kiss", artist="Prince", album="Album A", duration_ms=1, stream_key="/k", server_name="Host")]
        return []

    plex.get_artists.side_effect = fake_artists
    plex.get_albums.side_effect = fake_albums
    plex.get_tracks.side_effect = fake_tracks
    plex.get_artist_popular_tracks = AsyncMock(return_value=[])

    with patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_one_lib())), \
         patch("app.database.get_play_counts", AsyncMock(return_value={})):
        from app.main import app
        resp = TestClient(app, raise_server_exceptions=True).get("/api/browse/artists/machineA:42/songs")

    data = resp.json()
    assert data["popular_available"] is False
    assert data["tracks"] and all(t["pop_rank"] is None for t in data["tracks"])


def test_browse_artist_songs_unknown_artist_404(mock_deps):
    """An id that resolves to no artist → 404 (mirrors /albums)."""
    _, plex = mock_deps
    plex.get_artists = AsyncMock(return_value=[])
    with patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_one_lib())):
        from app.main import app
        resp = TestClient(app, raise_server_exceptions=True).get("/api/browse/artists/machineA:9999/songs")
    assert resp.status_code == 404


# ── Now Playing → Lyrics (plan 008 U2) ───────────────────────────────────────
# Unique track_ids per test avoid the module-level _LYRICS_CACHE bleeding across tests.

def _lyr_track(tid, title="Song", artist="A", album="B", dur=180000):
    return Track(id=tid, title=title, artist=artist, album=album, duration_ms=dur,
                 stream_key="/k", server_name="S")


def test_lyrics_returns_synced(mock_deps):
    _, plex = mock_deps
    plex.get_track = AsyncMock(return_value=_lyr_track("lyrsync"))
    result = {"available": True, "instrumental": False, "synced": [{"t_ms": 0, "line": "hi"}], "plain": None}
    with patch("app.lyrics.client.fetch_lyrics", AsyncMock(return_value=result)):
        from app.main import app
        resp = TestClient(app, raise_server_exceptions=True).get("/api/lyrics?track_id=lyrsync")
    assert resp.status_code == 200
    assert resp.json()["available"] is True and resp.json()["synced"][0]["line"] == "hi"


def test_lyrics_no_match_is_silent_200(mock_deps):
    """Covers R8/AE3: a no-match track returns {available:false} at 200 (not an error)."""
    _, plex = mock_deps
    plex.get_track = AsyncMock(return_value=_lyr_track("lyrmiss"))
    with patch("app.lyrics.client.fetch_lyrics", AsyncMock(return_value={"available": False, "instrumental": False, "synced": None, "plain": None})):
        from app.main import app
        resp = TestClient(app, raise_server_exceptions=True).get("/api/lyrics?track_id=lyrmiss")
    assert resp.status_code == 200 and resp.json()["available"] is False


def test_lyrics_instrumental(mock_deps):
    """Covers R9/AE4."""
    _, plex = mock_deps
    plex.get_track = AsyncMock(return_value=_lyr_track("lyrinst"))
    with patch("app.lyrics.client.fetch_lyrics", AsyncMock(return_value={"available": True, "instrumental": True, "synced": None, "plain": None})):
        from app.main import app
        resp = TestClient(app, raise_server_exceptions=True).get("/api/lyrics?track_id=lyrinst")
    assert resp.json() == {"available": True, "instrumental": True, "synced": None, "plain": None}


def test_lyrics_caches_negative_result(mock_deps):
    """A repeated no-match track_id does not re-invoke fetch_lyrics (negative caching)."""
    _, plex = mock_deps
    plex.get_track = AsyncMock(return_value=_lyr_track("lyrcache"))
    fl = AsyncMock(return_value={"available": False, "instrumental": False, "synced": None, "plain": None})
    with patch("app.lyrics.client.fetch_lyrics", fl):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        c.get("/api/lyrics?track_id=lyrcache")
        c.get("/api/lyrics?track_id=lyrcache")
    assert fl.call_count == 1


def test_lyrics_transient_error_not_cached(mock_deps):
    """A TRANSIENT LRCLIB failure (LyricsFetchError — timeout/network/429/5xx)
    returns {available:false} at 200 but is NOT cached, so a later play retries
    instead of being permanently stuck on no-lyrics (the 2026-06-18 bug). The
    contrast with the negative-cache test above: a *definitive* miss is cached,
    a transient failure is not."""
    from app.lyrics.client import LyricsFetchError
    _, plex = mock_deps
    plex.get_track = AsyncMock(return_value=_lyr_track("lyrtransient"))
    fl = AsyncMock(side_effect=LyricsFetchError("timeout"))
    with patch("app.lyrics.client.fetch_lyrics", fl):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        r1 = c.get("/api/lyrics?track_id=lyrtransient")
        r2 = c.get("/api/lyrics?track_id=lyrtransient")
    assert r1.status_code == 200 and r1.json()["available"] is False
    assert r2.status_code == 200 and r2.json()["available"] is False
    assert fl.call_count == 2   # NOT cached → re-invoked on the second call


def test_lyrics_resolves_server_side_ignoring_client_fields(mock_deps):
    """Poisoning guard: match inputs come from the server-resolved track, never from
    client-supplied query fields."""
    _, plex = mock_deps
    plex.get_track = AsyncMock(return_value=_lyr_track("lyrpoison", title="Real", artist="RealArtist"))
    captured = {}

    async def fake_fetch(artist, title, album, duration_s):
        captured.update(artist=artist, title=title)
        return {"available": False, "instrumental": False, "synced": None, "plain": None}

    with patch("app.lyrics.client.fetch_lyrics", fake_fetch):
        from app.main import app
        TestClient(app, raise_server_exceptions=True).get("/api/lyrics?track_id=lyrpoison&artist=BOGUS&title=BOGUS")
    assert captured["artist"] == "RealArtist" and captured["title"] == "Real"


def test_lyrics_invalid_track_id_400(client):
    resp = client.get("/api/lyrics?track_id=not%20a%20valid%20id")
    assert resp.status_code == 400


# ── Contribute prompt on a confirmed miss (contribute-prompt plan 2026-06-23 U3) ─
# Unique track_ids per test (the lyric cache is module-level and not cleared here).

_DEF_MISS = {"available": False, "instrumental": False, "synced": None,
             "plain": None, "no_match": True}


def test_lyrics_contribute_on_confirmed_miss_when_enabled(mock_deps):
    """Covers AE1: a confirmed no-match + toggle on → a contribute link to LRCLIB's
    uploader. The link is a constant — LRCLIB exposes no URL that pre-fills a track
    (lrclib.net is an SPA that ignores query params; verified 2026-06-23) — so no
    artist/title is encoded; the now-view already shows the track."""
    _, plex = mock_deps
    plex.get_track = AsyncMock(return_value=_lyr_track("lyrcontrib1", title="Heads", artist="le_mol"))
    with patch("app.lyrics.client.fetch_lyrics", AsyncMock(return_value=dict(_DEF_MISS))), \
         patch("app.database.get_setting", AsyncMock(return_value=None)):   # unset → default on
        from app.main import app
        resp = TestClient(app, raise_server_exceptions=True).get("/api/lyrics?track_id=lyrcontrib1")
    body = resp.json()
    assert resp.status_code == 200 and body["available"] is False
    assert body["contribute"]["url"] == "https://lrclibup.boidu.dev/"


def test_lyrics_no_contribute_when_disabled(mock_deps):
    """Covers AE5: toggle off → no contribute link (silent, as before)."""
    _, plex = mock_deps
    plex.get_track = AsyncMock(return_value=_lyr_track("lyrcontrib2"))
    with patch("app.lyrics.client.fetch_lyrics", AsyncMock(return_value=dict(_DEF_MISS))), \
         patch("app.database.get_setting", AsyncMock(return_value="0")):
        from app.main import app
        resp = TestClient(app, raise_server_exceptions=True).get("/api/lyrics?track_id=lyrcontrib2")
    assert resp.status_code == 200 and "contribute" not in resp.json()


def test_lyrics_no_contribute_on_transient_miss(mock_deps):
    """Covers AE3: a transient failure is not a confirmed miss → no contribute,
    even with the toggle on."""
    from app.lyrics.client import LyricsFetchError
    _, plex = mock_deps
    plex.get_track = AsyncMock(return_value=_lyr_track("lyrcontrib3"))
    with patch("app.lyrics.client.fetch_lyrics", AsyncMock(side_effect=LyricsFetchError("timeout"))), \
         patch("app.database.get_setting", AsyncMock(return_value="1")):
        from app.main import app
        resp = TestClient(app, raise_server_exceptions=True).get("/api/lyrics?track_id=lyrcontrib3")
    assert resp.status_code == 200 and "contribute" not in resp.json()


def test_lyrics_no_contribute_on_plain_lyrics(mock_deps):
    """Covers AE2: present-but-plain lyrics are not a miss → no contribute."""
    _, plex = mock_deps
    plex.get_track = AsyncMock(return_value=_lyr_track("lyrcontrib4"))
    plain = {"available": True, "instrumental": False, "synced": None, "plain": "words"}
    with patch("app.lyrics.client.fetch_lyrics", AsyncMock(return_value=plain)), \
         patch("app.database.get_setting", AsyncMock(return_value="1")):
        from app.main import app
        resp = TestClient(app, raise_server_exceptions=True).get("/api/lyrics?track_id=lyrcontrib4")
    assert resp.json()["available"] is True and "contribute" not in resp.json()


def test_lyrics_no_contribute_on_instrumental(mock_deps):
    """Covers AE4: instrumental keeps its tag, no contribute link."""
    _, plex = mock_deps
    plex.get_track = AsyncMock(return_value=_lyr_track("lyrcontrib5"))
    inst = {"available": True, "instrumental": True, "synced": None, "plain": None}
    with patch("app.lyrics.client.fetch_lyrics", AsyncMock(return_value=inst)), \
         patch("app.database.get_setting", AsyncMock(return_value="1")):
        from app.main import app
        resp = TestClient(app, raise_server_exceptions=True).get("/api/lyrics?track_id=lyrcontrib5")
    assert "contribute" not in resp.json()


def test_lyrics_contribute_not_cached_and_toggle_immediate(mock_deps):
    """The contribute decision is made at response time: never written to the
    cache, and toggling the setting takes effect on the very next lookup."""
    _, plex = mock_deps
    plex.get_track = AsyncMock(return_value=_lyr_track("lyrcontrib6"))
    flips = iter(["1", "0"])   # first request: on; second request: off
    with patch("app.lyrics.client.fetch_lyrics", AsyncMock(return_value=dict(_DEF_MISS))), \
         patch("app.database.get_setting", AsyncMock(side_effect=lambda *a, **k: next(flips))):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        r1 = c.get("/api/lyrics?track_id=lyrcontrib6")    # on → contribute
        r2 = c.get("/api/lyrics?track_id=lyrcontrib6")    # off → none (warm cache)
    assert "contribute" in r1.json()
    assert "contribute" not in r2.json()
    from app.lyrics import cache as lyrics_cache
    cached = lyrics_cache.cached("lyrcontrib6")
    assert cached is not None and "contribute" not in cached   # cache never poisoned


def test_lyrics_contribute_on_warm_cached_miss_does_not_reresolve(mock_deps):
    """A confirmed miss that is already cached still attaches the contribute link
    with the toggle on — and the warm path does NOT re-resolve the track from Plex.
    The link is a constant, so there's nothing track-specific to fetch; this locks in
    the warm-cache fast path restored by the 2026-06-23 link fix."""
    _, plex = mock_deps
    plex.get_track = AsyncMock(return_value=_lyr_track("lyrcontrib7", title="Kara", artist="le_mol"))
    with patch("app.lyrics.client.fetch_lyrics", AsyncMock(return_value=dict(_DEF_MISS))), \
         patch("app.database.get_setting", AsyncMock(return_value="1")):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        c.get("/api/lyrics?track_id=lyrcontrib7")             # cold → resolves once, caches the miss
        resp = c.get("/api/lyrics?track_id=lyrcontrib7")      # warm → cache hit, no second resolve
    assert resp.json()["contribute"]["url"] == "https://lrclibup.boidu.dev/"
    assert plex.get_track.call_count == 1   # warm path must not re-resolve to build the URL


# ── Multi-library browse: album → tracks (U3) ────────────────────────────────


def _t(tid, title, server_name, artist="Prince", album="Album A") -> Track:
    return Track(id=tid, title=title, artist=artist, album=album,
                 duration_ms=180000, stream_key=f"/parts/{tid}/f.flac",
                 server_name=server_name)


def test_browse_album_tracks_unions_across_libraries(mock_deps):
    """Covers AE3. Shared album exists in both libraries; tracks are unioned
    un-deduped and each track carries the producing library's server_name
    so the frontend source-picker can distinguish sources."""
    _, plex = mock_deps

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
            return [_t("machineA:t1", "Track 1", server_name="Host"),
                    _t("machineA:t2", "Track 2", server_name="Host")]
        if section_key == "machineB:2" and album_id == "machineB:200":
            return [_t("machineB:t1", "Track 1", server_name="Shared"),
                    _t("machineB:t2", "Track 2", server_name="Shared")]
        return []

    plex.get_artists.side_effect = fake_get_artists
    plex.get_albums.side_effect = fake_get_albums
    plex.get_tracks.side_effect = fake_get_tracks

    with patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/albums/machineA:100/tracks")

    assert resp.status_code == 200
    tracks = resp.json()
    # Four total (no backend dedup); frontend deduplicateTracks handles grouping
    assert len(tracks) == 4
    server_names = {t["server_name"] for t in tracks}
    assert server_names == {"Host", "Shared"}


def test_browse_album_tracks_unknown_id_returns_404(mock_deps):
    _, plex = mock_deps
    plex.get_album = AsyncMock(side_effect=KeyError("not found"))

    with patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/albums/machineA:9999/tracks")

    assert resp.status_code == 404


def test_browse_album_tracks_no_plex_returns_503():
    with patch("app.state.get_plex_client", AsyncMock(return_value=None)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/albums/machineA:100/tracks")
    assert resp.status_code == 503


def test_track_dict_includes_year():
    """Release-art U1: the track serializer surfaces album year for the
    release drill-in header (additive field)."""
    from app.api.guest import _track_dict
    t = Track(id="t1", title="Song", artist="A", album="B", duration_ms=1000,
              stream_key="/p", server_name="X", year=1980)
    assert _track_dict(t)["year"] == 1980


def test_track_dict_year_none_serializes_null():
    """A track with no year serializes year: null (no crash)."""
    from app.api.guest import _track_dict
    t = Track(id="t1", title="Song", artist="A", album="B", duration_ms=1000,
              stream_key="/p", server_name="X")
    assert _track_dict(t)["year"] is None


def test_browse_album_tracks_no_matching_artist_returns_empty(mock_deps):
    """get_album succeeds but no library has an artist with that name."""
    _, plex = mock_deps
    plex.get_album = AsyncMock(return_value=Album(
        id="machineA:100", title="Album A", artist="Ghost Artist",
    ))

    async def fake_get_artists(section_key):
        return [Artist(id=f"{section_key}:42", title="Different Artist")]

    plex.get_artists.side_effect = fake_get_artists

    with patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/albums/machineA:100/tracks")

    assert resp.status_code == 200
    assert resp.json() == []


def test_browse_album_tracks_no_matching_album_returns_empty(mock_deps):
    """Artists match by name in every library, but no library has an album
    matching the requested title."""
    _, plex = mock_deps
    plex.get_album = AsyncMock(return_value=Album(
        id="machineA:100", title="Album A", artist="Prince",
    ))

    async def fake_get_artists(section_key):
        if section_key == "machineA:1":
            return [Artist(id="machineA:42", title="Prince")]
        return [Artist(id="machineB:78", title="Prince")]

    async def fake_get_albums(section_key, artist_id=None, **kw):
        return [Album(id=f"{section_key}:200", title="Unrelated Album", artist="Prince")]

    plex.get_artists.side_effect = fake_get_artists
    plex.get_albums.side_effect = fake_get_albums

    with patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/albums/machineA:100/tracks")

    assert resp.status_code == 200
    assert resp.json() == []


def test_browse_album_tracks_one_library_get_artists_fails(mock_deps):
    """When one library's get_artists raises, response still includes the
    surviving library's tracks."""
    _, plex = mock_deps
    plex.get_album = AsyncMock(return_value=Album(
        id="machineA:100", title="Album A", artist="Prince",
    ))

    async def fake_get_artists(section_key):
        if section_key == "machineA:1":
            return [Artist(id="machineA:42", title="Prince")]
        raise RuntimeError("plex unreachable")

    async def fake_get_albums(section_key, artist_id=None, **kw):
        return [Album(id="machineA:100", title="Album A", artist="Prince")]

    async def fake_get_tracks(section_key, album_id=None, **kw):
        return [_t("machineA:t1", "Track 1", server_name="Host")]

    plex.get_artists.side_effect = fake_get_artists
    plex.get_albums.side_effect = fake_get_albums
    plex.get_tracks.side_effect = fake_get_tracks

    with patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/albums/machineA:100/tracks")

    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_browse_album_tracks_one_library_get_tracks_fails(mock_deps):
    """When a matched library's get_tracks raises, response still includes the
    other library's tracks."""
    _, plex = mock_deps
    plex.get_album = AsyncMock(return_value=Album(
        id="machineA:100", title="Album A", artist="Prince",
    ))

    async def fake_get_artists(section_key):
        if section_key == "machineA:1":
            return [Artist(id="machineA:42", title="Prince")]
        return [Artist(id="machineB:78", title="Prince")]

    async def fake_get_albums(section_key, artist_id=None, **kw):
        if section_key == "machineA:1":
            return [Album(id="machineA:100", title="Album A", artist="Prince")]
        return [Album(id="machineB:200", title="Album A", artist="Prince")]

    async def fake_get_tracks(section_key, album_id=None, **kw):
        if section_key == "machineA:1":
            return [_t("machineA:t1", "Track 1", server_name="Host")]
        raise RuntimeError("plex unreachable")

    plex.get_artists.side_effect = fake_get_artists
    plex.get_albums.side_effect = fake_get_albums
    plex.get_tracks.side_effect = fake_get_tracks

    with patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/albums/machineA:100/tracks")

    assert resp.status_code == 200
    titles = [t["title"] for t in resp.json()]
    assert titles == ["Track 1"]


def test_browse_album_tracks_case_insensitive_matching(mock_deps):
    """'ALBUM A' / 'album a' across libraries are merged via the lower-cased key.
    Same for artists."""
    _, plex = mock_deps
    plex.get_album = AsyncMock(return_value=Album(
        id="machineA:100", title="ALBUM A", artist="PRINCE",
    ))

    async def fake_get_artists(section_key):
        if section_key == "machineA:1":
            return [Artist(id="machineA:42", title="prince")]
        return [Artist(id="machineB:78", title="Prince")]

    async def fake_get_albums(section_key, artist_id=None, **kw):
        if section_key == "machineA:1":
            return [Album(id="machineA:100", title="album a", artist="prince")]
        return [Album(id="machineB:200", title="Album A", artist="Prince")]

    async def fake_get_tracks(section_key, album_id=None, **kw):
        if section_key == "machineA:1":
            return [_t("machineA:t1", "T", server_name="Host", album="album a", artist="prince")]
        return [_t("machineB:t1", "T", server_name="Shared", album="Album A", artist="Prince")]

    plex.get_artists.side_effect = fake_get_artists
    plex.get_albums.side_effect = fake_get_albums
    plex.get_tracks.side_effect = fake_get_tracks

    with patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/albums/machineA:100/tracks")

    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_browse_album_tracks_invalid_id_returns_400(client, mock_deps):
    resp = client.get("/api/browse/albums/not%20valid/tracks")
    assert resp.status_code == 400


def test_browse_genres_returns_style_objects_sorted_by_count(client, mock_deps):
    resp = client.get("/api/browse/genres")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert data[0]["name"] == "Indie Rock"
    assert data[0]["count"] == 50
    assert data[1]["name"] == "Synth-pop"
    assert data[1]["count"] == 20


def test_browse_genres_empty_when_all_zero_counts(mock_deps):
    _, plex = mock_deps
    plex.get_styles_with_counts.return_value = []
    from app.main import app
    c = TestClient(app, raise_server_exceptions=True)
    resp = c.get("/api/browse/genres")
    assert resp.status_code == 200
    assert resp.json() == []


# ── genre cache freshness gate (U2) ───────────────────────────────────────────

def test_browse_genres_warm_fresh_does_not_trigger_genre_refresh(mock_deps):
    """A warm + fresh genre cache returns cached without a genre recompute.
    (Credit-side gating is U3; full AE2 is asserted there.)"""
    from app.main import app
    with patch("app.database.get_genre_cache",
               AsyncMock(return_value=[{"name": "Rock", "count": 5}])), \
         patch("app.state.cache_is_fresh", AsyncMock(return_value=True)), \
         patch("app.state.trigger_genre_refresh", MagicMock()) as tg, \
         patch("app.state.trigger_credit_refresh", MagicMock()):
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/genres")
    assert resp.status_code == 200
    assert resp.json() == [{"name": "Rock", "count": 5}]
    tg.assert_not_called()


def test_browse_genres_warm_stale_triggers_refresh(mock_deps):
    """A warm + stale genre cache returns cached AND fires a background recompute. (R6)"""
    from app.main import app
    with patch("app.database.get_genre_cache",
               AsyncMock(return_value=[{"name": "Rock", "count": 5}])), \
         patch("app.state.cache_is_fresh", AsyncMock(return_value=False)), \
         patch("app.state.trigger_genre_refresh", MagicMock()) as tg:
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/genres")
    assert resp.status_code == 200
    tg.assert_called_once()


def test_browse_genres_cold_computes_and_stamps_freshness(mock_deps):
    """A cold cache self-populates inline AND stamps the freshness timestamp so
    the next read is warm. Covers AE3 / R7."""
    from app.main import app
    stamped = {}

    async def fake_set_setting(key, value):
        stamped[key] = value

    with patch("app.database.get_genre_cache", AsyncMock(return_value=[])), \
         patch("app.database.set_genre_cache", AsyncMock()), \
         patch("app.database.set_setting", fake_set_setting):
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/genres")
    assert resp.status_code == 200
    assert "genre_cache_computed_at" in stamped


# ── credit cache freshness gate (U3) ──────────────────────────────────────────

def test_browse_genres_warm_fresh_skips_both_refreshes(mock_deps):
    """Full AE2: a warm + fresh cache fires neither genre nor credit recompute."""
    from app.main import app
    with patch("app.database.get_genre_cache",
               AsyncMock(return_value=[{"name": "Rock", "count": 5}])), \
         patch("app.state.cache_is_fresh", AsyncMock(return_value=True)), \
         patch("app.state.trigger_genre_refresh", MagicMock()) as tg, \
         patch("app.state.trigger_credit_refresh", MagicMock()) as tc:
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/genres")
    assert resp.status_code == 200
    tg.assert_not_called()
    tc.assert_not_called()


def test_browse_artists_warm_fresh_credit_skips_refresh(mock_deps):
    """A warm + fresh credit cache means browsing artists fires no credit recompute."""
    from app.main import app
    with patch("app.state.cache_is_fresh", AsyncMock(return_value=True)), \
         patch("app.state.trigger_credit_refresh", MagicMock()) as tc:
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/artists")
    assert resp.status_code == 200
    tc.assert_not_called()


def test_browse_artists_stale_credit_triggers_refresh(mock_deps):
    """A cold/stale credit cache self-heals via a background recompute. (R6/R7)"""
    from app.main import app
    with patch("app.state.cache_is_fresh", AsyncMock(return_value=False)), \
         patch("app.state.trigger_credit_refresh", MagicMock()) as tc:
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/artists")
    assert resp.status_code == 200
    tc.assert_called_once()


def test_browse_genre_albums_returns_album_list(client, mock_deps):
    resp = client.get("/api/browse/genres/albums?style=Indie+Rock")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 1


def test_browse_genre_albums_requires_style_param(client, mock_deps):
    resp = client.get("/api/browse/genres/albums")
    assert resp.status_code == 422


def test_browse_genre_albums_passes_style_to_client(mock_deps):
    _, plex = mock_deps
    plex.get_albums.return_value = [make_album("a2")]
    from app.main import app
    c = TestClient(app, raise_server_exceptions=True)
    resp = c.get("/api/browse/genres/albums?style=Acid+Jazz")
    assert resp.status_code == 200
    plex.get_albums.assert_called_with("1", style="Acid Jazz")


def test_browse_years(client, mock_deps):
    resp = client.get("/api/browse/years")
    assert resp.status_code == 200
    assert 2024 in resp.json()


# ── Search ────────────────────────────────────────────────────────────────────

def test_search_returns_grouped_results(client, mock_deps):
    resp = client.get("/api/search?q=beatles")
    assert resp.status_code == 200
    data = resp.json()
    assert "tracks" in data
    assert "albums" in data
    assert "artists" in data


def test_search_missing_q_returns_422(client, mock_deps):
    resp = client.get("/api/search")
    assert resp.status_code == 422


_GENRE_CACHE = [
    {"name": "Rock", "count": 40},
    {"name": "Krautrock", "count": 12},
    {"name": "Jazz", "count": 9},
]


def test_search_genres_substring_match_preserves_count_desc_order(mock_deps):
    with patch("app.database.get_genre_cache", AsyncMock(return_value=_GENRE_CACHE)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/search?q=rock")
    assert resp.status_code == 200
    data = resp.json()
    assert data["genres"] == [
        {"name": "Rock", "count": 40},
        {"name": "Krautrock", "count": 12},
    ]
    # Existing keys unchanged alongside the additive genres key.
    assert "tracks" in data and "albums" in data and "artists" in data


def test_search_genres_case_insensitive(mock_deps):
    with patch("app.database.get_genre_cache", AsyncMock(return_value=_GENRE_CACHE)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/search?q=KRAUT")
    assert [g["name"] for g in resp.json()["genres"]] == ["Krautrock"]


def test_search_genres_no_match_returns_empty(mock_deps):
    with patch("app.database.get_genre_cache", AsyncMock(return_value=_GENRE_CACHE)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/search?q=zydeco")
    assert resp.json()["genres"] == []


def test_search_genres_empty_cache_returns_empty(client, mock_deps):
    # mock_deps patches get_genre_cache to [] — the cold-start case.
    resp = client.get("/api/search?q=rock")
    assert resp.status_code == 200
    assert resp.json()["genres"] == []


# ── Queue append ──────────────────────────────────────────────────────────────

async def test_append_track(client, mock_deps):
    qe, _ = mock_deps
    resp = client.post("/api/queue", json={"track_id": "t1"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["tracks_added"] == 1
    assert len(qe.queue) == 1


async def test_append_track_locked_returns_423(client, mock_deps):
    qe, _ = mock_deps
    await qe.lock()
    resp = client.post("/api/queue", json={"track_id": "t1"})
    assert resp.status_code == 423
    assert resp.json()["detail"] == "queue_locked"


async def test_append_duplicate_track_returns_warning(client, mock_deps):
    qe, _ = mock_deps
    await qe.append(make_track("t1"), bypass_lock=True)
    resp = client.post("/api/queue", json={"track_id": "t1"})
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("warning") == "already_in_queue"


async def test_append_album(client, mock_deps):
    qe, plex = mock_deps
    plex.get_tracks = AsyncMock(return_value=[make_track("t1"), make_track("t2")])
    resp = client.post("/api/queue", json={"album_id": "a1"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["tracks_added"] == 2
    assert len(qe.queue) == 2


async def test_append_album_locked_returns_423(client, mock_deps):
    qe, _ = mock_deps
    await qe.lock()
    resp = client.post("/api/queue", json={"album_id": "a1"})
    assert resp.status_code == 423


async def test_append_no_id_returns_400(client, mock_deps):
    resp = client.post("/api/queue", json={})
    assert resp.status_code == 400


# ── Flood Control (2026-06-16 plan U2) ────────────────────────────────────────
# When the admin Flood Control toggle is on, a guest single-track add of a track
# that is currently playing or already in the upcoming queue is hard-blocked
# (409 duplicate_blocked, nothing appended). Off keeps today's soft-warn. The
# block reads `flood_control` at add-time via app.database.get_setting; mock_deps
# defaults it to None (off), so the on-cases patch it to "1" around the request.


async def test_flood_control_blocks_duplicate_in_queue_when_on(client, mock_deps):
    """FC on + track already in the upcoming queue → 409, nothing appended."""
    qe, _ = mock_deps
    await qe.append(make_track("t1"), bypass_lock=True)
    with patch("app.database.get_setting", AsyncMock(return_value="1")):
        resp = client.post("/api/queue", json={"track_id": "t1"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "duplicate_blocked"
    assert len(qe.queue) == 1  # not appended


async def test_flood_control_blocks_now_playing_track_when_on(client, mock_deps):
    """FC on + track is the currently-playing item → 409, nothing appended
    (is_duplicate covers the now-playing slot, not just the upcoming queue)."""
    qe, _ = mock_deps
    await qe.set_playing(make_track("t1"))
    with patch("app.database.get_setting", AsyncMock(return_value="1")):
        resp = client.post("/api/queue", json={"track_id": "t1"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "duplicate_blocked"
    assert len(qe.queue) == 0  # nothing queued


async def test_flood_control_allows_non_duplicate_when_on(client, mock_deps):
    """FC on + a NON-duplicate single track → appended normally (200)."""
    qe, _ = mock_deps
    await qe.append(make_track("t1"), bypass_lock=True)
    with patch("app.database.get_setting", AsyncMock(return_value="1")):
        resp = client.post("/api/queue", json={"track_id": "t2"})
    assert resp.status_code == 200
    assert len(qe.queue) == 2


async def test_flood_control_off_duplicate_keeps_soft_warning(client, mock_deps):
    """FC off (default) + duplicate single track → appended + the existing
    `already_in_queue` warning (today's behavior, byte-for-byte)."""
    qe, _ = mock_deps
    await qe.append(make_track("t1"), bypass_lock=True)
    resp = client.post("/api/queue", json={"track_id": "t1"})
    assert resp.status_code == 200
    assert resp.json().get("warning") == "already_in_queue"
    assert len(qe.queue) == 2  # added anyway


async def test_flood_control_album_is_exempt_when_on(client, mock_deps):
    """FC on + a guest album add whose tracks include a duplicate → the album
    still adds in full (the album branch never consults Flood Control)."""
    qe, plex = mock_deps
    plex.get_tracks = AsyncMock(return_value=[make_track("t1"), make_track("t2")])
    await qe.append(make_track("t1"), bypass_lock=True)  # t1 already queued
    with patch("app.database.get_setting", AsyncMock(return_value="1")):
        resp = client.post("/api/queue", json={"album_id": "a1"})
    assert resp.status_code == 200
    assert resp.json()["tracks_added"] == 2
    assert len(qe.queue) == 3  # 1 pre-seeded + 2 from the album (dup included)


async def test_flood_control_locked_still_returns_423(client, mock_deps):
    """FC on but the add is a non-duplicate while the queue is locked → the
    locked rejection (423) still applies; flood-blocked and locked are distinct
    and the locked case is unaffected by Flood Control."""
    qe, _ = mock_deps
    await qe.lock()
    with patch("app.database.get_setting", AsyncMock(return_value="1")):
        resp = client.post("/api/queue", json={"track_id": "t1"})
    assert resp.status_code == 423
    assert resp.json()["detail"] == "queue_locked"


# ── Queue album with source filter (U1) ──────────────────────────────────────


def _wire_two_library_album(plex, *, host_tracks, shared_tracks):
    """Configure a plex mock so the album 'Album A' by 'Prince' exists in
    both libraries from _two_libs(), with the given per-library track lists."""
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


async def test_append_album_no_source_filter_unions_libraries(mock_deps):
    """Backward-compat: POST {album_id} with no source_server_name enqueues
    the union of all matching libraries' tracks (today's post-multi-library
    net behavior)."""
    qe, plex = mock_deps
    _wire_two_library_album(
        plex,
        host_tracks=[make_track("machineA:t1"), make_track("machineA:t2")],
        shared_tracks=[make_track("machineB:t1")],
    )

    with patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.post("/api/queue", json={"album_id": "machineA:100"})

    assert resp.status_code == 200
    assert resp.json()["tracks_added"] == 3
    assert len(qe.queue) == 3


async def test_append_album_with_source_filter_scopes_to_one_library(mock_deps):
    """Covers AE2 (backend half). POST with source_server_name='Shared' enqueues
    only the Shared library's tracks; tracks_added reflects filtered count."""
    qe, plex = mock_deps
    _wire_two_library_album(
        plex,
        host_tracks=[make_track("machineA:t1"), make_track("machineA:t2")],
        shared_tracks=[make_track("machineB:t1")],
    )

    with patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.post("/api/queue", json={
            "album_id": "machineA:100",
            "source_server_name": "Shared",
        })

    assert resp.status_code == 200
    assert resp.json()["tracks_added"] == 1
    assert len(qe.queue) == 1
    assert qe.queue[0].track.id == "machineB:t1"


async def test_append_album_with_source_filter_includes_bonus_tracks(mock_deps):
    """Covers AE3. Host has 2 tracks, Shared has 3 (with bonus). Filtering to
    'Shared' enqueues all 3, including the bonus; no Host tracks queued."""
    qe, plex = mock_deps
    _wire_two_library_album(
        plex,
        host_tracks=[make_track("machineA:t1"), make_track("machineA:t2")],
        shared_tracks=[
            make_track("machineB:t1"),
            make_track("machineB:t2"),
            make_track("machineB:bonus"),
        ],
    )

    with patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.post("/api/queue", json={
            "album_id": "machineA:100",
            "source_server_name": "Shared",
        })

    assert resp.status_code == 200
    assert resp.json()["tracks_added"] == 3
    queued_ids = [i.track.id for i in qe.queue]
    assert "machineB:bonus" in queued_ids
    assert all(tid.startswith("machineB:") for tid in queued_ids)


async def test_append_album_source_filter_no_match_returns_404(mock_deps):
    """source_server_name doesn't match any enabled library → no tracks
    resolved → 404 (matches existing 'Album not found or no tracks' shape)."""
    _, plex = mock_deps
    _wire_two_library_album(
        plex,
        host_tracks=[make_track("machineA:t1")],
        shared_tracks=[make_track("machineB:t1")],
    )

    with patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.post("/api/queue", json={
            "album_id": "machineA:100",
            "source_server_name": "Nonexistent",
        })

    assert resp.status_code == 404


async def test_append_album_source_filter_case_insensitive(mock_deps):
    """source_server_name match is case-insensitive after trim, matching the
    existing dedup-key casing policy."""
    qe, plex = mock_deps
    _wire_two_library_album(
        plex,
        host_tracks=[make_track("machineA:t1")],
        shared_tracks=[make_track("machineB:t1")],
    )

    with patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.post("/api/queue", json={
            "album_id": "machineA:100",
            "source_server_name": "host",  # lowercase, library is "Host"
        })

    assert resp.status_code == 200
    assert resp.json()["tracks_added"] == 1
    assert qe.queue[0].track.id == "machineA:t1"


async def test_append_album_source_filter_matches_multiple_libraries(mock_deps):
    """KTD4: if two enabled libraries share a server_name, the filter matches
    all of them (no rejection — fan out across both)."""
    qe, plex = mock_deps
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
        if section_key == "machineA:1":
            return [make_track("machineA:t1")]
        if section_key == "machineB:2":
            return [make_track("machineB:t1")]
        return []

    plex.get_artists.side_effect = fake_get_artists
    plex.get_albums.side_effect = fake_get_albums
    plex.get_tracks.side_effect = fake_get_tracks

    # Both libraries share the same server_name "Music"
    libs = [
        Library(key="machineA:1", title="A", type="artist", server_name="Music"),
        Library(key="machineB:2", title="B", type="artist", server_name="Music"),
    ]
    with patch("app.api.guest.enabled_libraries", AsyncMock(return_value=libs)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.post("/api/queue", json={
            "album_id": "machineA:100",
            "source_server_name": "Music",
        })

    assert resp.status_code == 200
    assert resp.json()["tracks_added"] == 2


async def test_append_album_source_filter_queue_locked_returns_423(mock_deps):
    """Source filter does not bypass the queue-locked guard."""
    qe, plex = mock_deps
    _wire_two_library_album(
        plex,
        host_tracks=[make_track("machineA:t1")],
        shared_tracks=[make_track("machineB:t1")],
    )
    await qe.lock()

    with patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.post("/api/queue", json={
            "album_id": "machineA:100",
            "source_server_name": "Host",
        })

    assert resp.status_code == 423


async def test_append_album_get_album_keyerror_returns_404(mock_deps):
    """get_album raising KeyError surfaces as 404 — same shape as
    browse_album_tracks unknown-id behavior."""
    _, plex = mock_deps
    plex.get_album = AsyncMock(side_effect=KeyError("not found"))

    with patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.post("/api/queue", json={
            "album_id": "machineA:9999",
            "source_server_name": "Host",
        })

    assert resp.status_code == 404


# ── Plex not configured ───────────────────────────────────────────────────────

def test_browse_artists_no_plex(mock_deps):
    with patch("app.state.get_plex_client", AsyncMock(return_value=None)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/artists")
        assert resp.status_code == 503


# ── server_name propagation ───────────────────────────────────────────────────

async def test_now_playing_includes_server_name(client, mock_deps):
    from app.queue.models import QueueItem
    qe, _ = mock_deps
    track = make_track("t1", server_name="Living Room")
    qe._current = QueueItem(track=track)
    qe._is_playing = True
    resp = client.get("/api/now-playing")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("server_name") == "Living Room"


async def test_queue_items_include_server_name(client, mock_deps):
    qe, _ = mock_deps
    track = make_track("t1", server_name="Office")
    await qe.append(track, bypass_lock=True)
    resp = client.get("/api/queue")
    assert resp.status_code == 200
    items = resp.json()["queue"]
    assert len(items) == 1
    assert items[0]["server_name"] == "Office"


def test_track_search_results_include_server_name(client, mock_deps):
    _, plex = mock_deps
    plex.search.return_value = __import__("app.plex.models", fromlist=["SearchResults"]).SearchResults(
        tracks=[make_track("t1", server_name="Basement")],
        albums=[],
        artists=[],
    )
    resp = client.get("/api/search?q=song")
    assert resp.status_code == 200
    tracks = resp.json()["tracks"]
    assert len(tracks) == 1
    assert tracks[0]["server_name"] == "Basement"


def test_browse_artists_dedupe_suppresses_count(mock_deps):
    """2026-06-09 rail plan U5: when same-titled artists collapse across
    enabled libraries, the survivor's count is suppressed — a single-library
    childCount would understate the cross-library drill-in union."""
    _, plex = mock_deps
    plex.get_artists.return_value = [
        Artist(id="s1:10", title="Prince", release_count=12),
        Artist(id="s2:10", title="Prince", release_count=7),   # second library
        Artist(id="s1:11", title="The Cure", release_count=14),
    ]
    from app.main import app
    c = TestClient(app, raise_server_exceptions=True)
    resp = c.get("/api/browse/artists")
    assert resp.status_code == 200
    by_title = {a["title"]: a for a in resp.json()}
    assert by_title["Prince"]["release_count"] is None          # suppressed
    assert by_title["The Cure"]["release_count"] == 14          # untouched


def test_browse_artists_dedupe_does_not_mutate_cached_objects(mock_deps):
    """Suppression must not leak into the client cache — the original Artist
    objects keep their counts (dataclasses.replace, not mutation)."""
    _, plex = mock_deps
    originals = [
        Artist(id="s1:10", title="Prince", release_count=12),
        Artist(id="s2:10", title="Prince", release_count=7),
    ]
    plex.get_artists.return_value = originals
    from app.main import app
    c = TestClient(app, raise_server_exceptions=True)
    resp = c.get("/api/browse/artists")
    assert resp.status_code == 200
    assert originals[0].release_count == 12
    assert originals[1].release_count == 7


def test_browse_artists_partial_failure_suppresses_all_counts(mock_deps):
    """Review fix: when any library batch fails, dedupe can't see that
    library's artists — counts are withheld for the whole degraded response
    rather than risking an unsuppressed single-library count."""
    _, plex = mock_deps
    plex.get_artists.side_effect = [
        [Artist(id="s1:10", title="Prince", release_count=12)],
        RuntimeError("library B unreachable"),
    ]
    with patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/artists")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["release_count"] is None


def test_browse_artists_dedupe_survivor_none_count_ordering(mock_deps):
    """First-seen artist has no count, duplicate carries one — the survivor
    stays None (no crash, no count resurrection from the dropped duplicate)."""
    _, plex = mock_deps
    plex.get_artists.return_value = [
        Artist(id="s1:10", title="Prince", release_count=None),
        Artist(id="s2:10", title="Prince", release_count=7),
    ]
    from app.main import app
    c = TestClient(app, raise_server_exceptions=True)
    resp = c.get("/api/browse/artists")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["release_count"] is None


# ── per-track credits (2026-06-10 plan U3) ───────────────────────────────────

_CREDIT_ACTS = [
    {"name": "13th Floor Elevators", "name_lower": "13th floor elevators", "release_count": 2},
    {"name": "Artist A", "name_lower": "artist a", "release_count": 1},  # collides with real artist
]

_CREDIT_APPEARANCES = [
    {"name": "13th Floor Elevators", "album_id": "al-nuggets", "album_title": "Nuggets",
     "album_artist": "Various Artists", "album_thumb": None, "album_year": 1972,
     "server_name": "My Plex"},
]


def test_browse_artists_merges_credit_only_acts(mock_deps):
    """Covers AE4 + AE5/R8: credit-only act joins the roster; an act whose
    name matches a real artist is NOT duplicated."""
    with patch("app.database.get_credit_acts", AsyncMock(return_value=_CREDIT_ACTS)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/artists")
    assert resp.status_code == 200
    data = resp.json()
    titles = [a["title"] for a in data]
    assert "13th Floor Elevators" in titles
    assert titles.count("Artist A") == 1  # no duplicate for the colliding act
    synth = next(a for a in data if a["title"] == "13th Floor Elevators")
    assert synth["id"].startswith("credit:")
    assert synth["release_count"] == 2


def test_browse_artists_fires_credit_refresh_trigger(mock_deps):
    """Empty-cache self-heal (review fix): pass-through response AND the
    fire-and-forget trigger invoked."""
    with patch("app.state.trigger_credit_refresh", MagicMock()) as trig:
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/artists")
    assert resp.status_code == 200
    assert all(not a["title"] == "13th Floor Elevators" for a in resp.json())
    trig.assert_called()


def test_search_returns_credited_act_not_compilation(mock_deps):
    """Covers AE2/R3/R4: the act appears under artists; albums don't gain
    the compilation."""
    with patch("app.database.get_credit_acts", AsyncMock(return_value=_CREDIT_ACTS)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/search?q=13th+floor")
    data = resp.json()
    artist_titles = [a["title"] for a in data["artists"]]
    assert "13th Floor Elevators" in artist_titles
    assert all(al["title"] != "Nuggets" for al in data["albums"])


def test_search_credit_act_deduped_against_plex_artist(mock_deps):
    """R8: an act already present as a Plex artist hit is not added twice."""
    with patch("app.database.get_credit_acts", AsyncMock(return_value=_CREDIT_ACTS)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/search?q=artist+a")
    titles = [a["title"] for a in resp.json()["artists"]]
    assert titles.count("Artist A") == 1


def test_credit_artist_albums_returns_appears_on(mock_deps):
    """Covers AE3: credit: id serves appears-on rows, no validator rejection.
    (Appearances resolve via the acts list since the pattern-rules union —
    equivalent spellings merge — so the acts patch is part of the contract.)"""
    with patch("app.database.get_credit_acts", AsyncMock(return_value=_CREDIT_ACTS)), \
         patch("app.database.get_credit_appearances", AsyncMock(return_value=_CREDIT_APPEARANCES)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/artists/credit:13th%2520Floor%2520Elevators/albums")
    # NOTE: TestClient decodes once in transit; the path above double-encodes
    # so the route sees credit:13th%20Floor%20Elevators and unquote() yields
    # the act name — mirroring the browser's behavior with encodeURIComponent.
    assert resp.status_code == 200
    data = resp.json()
    assert [a["id"] for a in data] == ["al-nuggets"]
    assert data[0]["subtype"] == "appears_on"
    assert data[0]["artist"] == "Various Artists"


def test_credit_artist_albums_unknown_name_returns_empty_200(mock_deps):
    """Review decision: unknown credit name → 200 + [] (frontend shows
    'No releases.'), not 404."""
    from app.main import app
    c = TestClient(app, raise_server_exceptions=True)
    resp = c.get("/api/browse/artists/credit:nobody/albums")
    assert resp.status_code == 200
    assert resp.json() == []


def test_credit_artist_albums_length_cap_returns_400(mock_deps):
    from app.main import app
    c = TestClient(app, raise_server_exceptions=True)
    resp = c.get("/api/browse/artists/credit:" + "x" * 300 + "/albums")
    assert resp.status_code == 400


def test_real_artist_albums_appends_appears_on_deduped_by_id(mock_deps):
    """Covers AE5 + the dedupe-by-id review pin: appears-on rows join the
    real artist's albums; a row sharing an album id with an own release is
    suppressed even though title|artist dedup couldn't match it."""
    # Distinct titles so the existing title|artist dedup keeps both own
    # albums — the point of this test is the SECOND dedup stage (by id).
    own = Album(id="al-own", title="Solo Album", artist="Artist A", year=2020)
    own_compilation = Album(id="al-nuggets", title="Nuggets", artist="Artist A", year=1972)
    _, plex = mock_deps
    plex.get_albums.return_value = [own, own_compilation]
    apps = _CREDIT_APPEARANCES + [
        {"name": "Artist A", "album_id": "al-other", "album_title": "Other Comp",
         "album_artist": "Various Artists", "album_thumb": None, "album_year": 1990,
         "server_name": "My Plex"},
    ]
    with patch("app.database.get_credit_acts", AsyncMock(return_value=_CREDIT_ACTS)), \
         patch("app.database.get_credit_appearances", AsyncMock(return_value=apps)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/artists/ar1/albums")
    assert resp.status_code == 200
    data = resp.json()
    ids = [a["id"] for a in data]
    assert ids.count("al-nuggets") == 1          # deduped by album id
    assert "al-other" in ids                      # genuinely new appears-on row
    appears = [a for a in data if a.get("subtype") == "appears_on"]
    assert {a["id"] for a in appears} == {"al-other"}


def test_browse_artists_requests_va_gated_acts(mock_deps):
    """Browse-VA-gate R1/R3: the roster merge asks for VA-gated acts; the
    search merge (R2) keeps the unfiltered set."""
    with patch("app.database.get_credit_acts", AsyncMock(return_value=[])) as acts:
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        c.get("/api/browse/artists")
        assert acts.await_args.kwargs.get("va_only") is True
        c.get("/api/search?q=floor")
        assert acts.await_args.kwargs.get("va_only") is not True


# ── pattern rules public endpoint (2026-06-10 pattern-rules plan U1) ─────────

def test_pattern_rules_endpoint_serves_valid_rules_only(mock_deps):
    stored = [["&", "and"], ["'", ""], ["e", "é"]]  # middle rule inert
    with patch("app.database.get_pattern_rules", AsyncMock(return_value=stored)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/pattern-rules")
    assert resp.status_code == 200
    assert resp.json() == {"rules": [["&", "and"], ["e", "é"]]}


def test_pattern_rules_endpoint_empty_default(mock_deps):
    with patch("app.database.get_pattern_rules", AsyncMock(return_value=[])):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/pattern-rules")
    assert resp.json() == {"rules": []}


# ── pattern rules: server-side application (2026-06-10 plan U2) ──────────────

_AMP_RULE = [["&", "and"]]


def test_browse_artists_rule_merges_spelling_variants(mock_deps):
    """Covers AE2 (merge) + AE8/R4: one roster entry, first-seen spelling."""
    _, plex = mock_deps
    plex.get_artists.return_value = [
        Artist(id="s1:1", title="Belle & Sebastian", release_count=4),
        Artist(id="s2:1", title="Belle and Sebastian", release_count=3),
    ]
    with patch("app.database.get_pattern_rules", AsyncMock(return_value=_AMP_RULE)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/artists")
    data = resp.json()
    belles = [a for a in data if "belle" in a["title"].lower()]
    assert len(belles) == 1
    assert belles[0]["title"] == "Belle & Sebastian"  # first-seen, raw spelling
    assert belles[0]["release_count"] is None  # cross-library merge suppresses count


def test_search_expands_query_variants_and_keeps_counts(mock_deps):
    """Covers AE1 + the count-preservation review fix: variant fan-out finds
    the artist; same-id collapse keeps its release_count."""
    _, plex = mock_deps
    belle = Artist(id="ar-belle", title="Belle & Sebastian", release_count=4)

    async def fake_search(lib_key, query):
        if "&" in query:
            return SearchResults(tracks=[], albums=[], artists=[belle])
        return SearchResults(tracks=[], albums=[], artists=[])

    plex.search = AsyncMock(side_effect=fake_search)
    with patch("app.database.get_pattern_rules", AsyncMock(return_value=_AMP_RULE)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/search?q=belle+and")
    data = resp.json()
    queries = [call.args[1] for call in plex.search.await_args_list]
    assert "belle and" in queries and "belle &" in queries
    assert [a["title"] for a in data["artists"]] == ["Belle & Sebastian"]
    assert data["artists"][0]["release_count"] == 4  # same-id collapse, no suppression


def test_search_variant_tracks_deduped_by_id(mock_deps):
    """Review fix: a track returned by two variant queries appears once."""
    _, plex = mock_deps
    track = make_track("t-dup")

    async def fake_search(lib_key, query):
        return SearchResults(tracks=[track], albums=[], artists=[])

    plex.search = AsyncMock(side_effect=fake_search)
    with patch("app.database.get_pattern_rules", AsyncMock(return_value=_AMP_RULE)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/search?q=rock+and+roll")
    ids = [t["track_id"] for t in resp.json()["tracks"]]
    assert ids == ["t-dup"]


def test_search_credit_act_apostrophe_rule_match(mock_deps):
    """Covers AE4 on the local credit-act surface."""
    acts = [{"name": "Don’t Ask", "name_lower": "don’t ask", "release_count": 1}]
    with patch("app.database.get_credit_acts", AsyncMock(return_value=acts)), \
         patch("app.database.get_pattern_rules", AsyncMock(return_value=[["'", "’"]])):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/search?q=don't")
    assert "Don’t Ask" in [a["title"] for a in resp.json()["artists"]]


def test_search_credit_acts_grouped_under_rule(mock_deps):
    """Credit acts whose names normalize equal become one entry, counts sum."""
    acts = [
        {"name": "X & Y", "name_lower": "x & y", "release_count": 2},
        {"name": "X and Y", "name_lower": "x and y", "release_count": 1},
    ]
    with patch("app.database.get_credit_acts", AsyncMock(return_value=acts)), \
         patch("app.database.get_pattern_rules", AsyncMock(return_value=_AMP_RULE)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/search?q=x+%26+y")
    entries = [a for a in resp.json()["artists"] if "x" in a["title"].lower()]
    assert len(entries) == 1
    assert entries[0]["title"] == "X & Y"
    assert entries[0]["release_count"] == 3


def test_search_no_rules_single_plex_call_per_library(mock_deps):
    """Pass-through pin: zero rules → exactly one client.search per library,
    response shape unchanged."""
    _, plex = mock_deps
    from app.main import app
    c = TestClient(app, raise_server_exceptions=True)
    resp = c.get("/api/search?q=beatles")
    assert plex.search.await_count == 1
    data = resp.json()
    assert set(data.keys()) == {"tracks", "albums", "artists", "genres"}


def test_search_variant_cap_bounds_plex_calls(mock_deps):
    _, plex = mock_deps
    rules = [["e", "ë", "è", "é", "ê"]]
    with patch("app.database.get_pattern_rules", AsyncMock(return_value=rules)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        c.get("/api/search?q=eee+eee")
    assert plex.search.await_count <= 8  # one library × ≤8 variants


# ── artist exclusion (2026-06-10 pattern-rules plan U3) ──────────────────────

def test_exclusion_drops_synthesized_act_from_roster_only(mock_deps):
    """Covers AE6: excluded from browse; search and drill-in untouched."""
    acts = [{"name": "[dialogue]", "name_lower": "[dialogue]", "release_count": 1}]
    with patch("app.database.get_credit_acts", AsyncMock(return_value=acts)), \
         patch("app.database.get_artist_exclusions", AsyncMock(return_value=["[dialogue]"])):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        roster = c.get("/api/browse/artists").json()
        search = c.get("/api/search?q=dialogue").json()
    assert "[dialogue]" not in [a["title"] for a in roster]
    assert "[dialogue]" in [a["title"] for a in search["artists"]]


def test_exclusion_case_insensitive_whole_string(mock_deps):
    """Covers AE7 + whole-string boundary."""
    _, plex = mock_deps
    plex.get_artists.return_value = [
        Artist(id="x1", title="[dialogue]"),
        Artist(id="x2", title="dialog"),  # substring — must survive
    ]
    with patch("app.database.get_artist_exclusions", AsyncMock(return_value=["[Dialogue]"])):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        titles = [a["title"] for a in c.get("/api/browse/artists").json()]
    assert "[dialogue]" not in titles
    assert "dialog" in titles


def test_exclusion_drops_plex_artist(mock_deps):
    _, plex = mock_deps
    plex.get_artists.return_value = [Artist(id="x1", title="Artist A"), Artist(id="x2", title="Keep Me")]
    with patch("app.database.get_artist_exclusions", AsyncMock(return_value=["artist a"])):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        titles = [a["title"] for a in c.get("/api/browse/artists").json()]
    assert titles == ["Keep Me"]


def test_exclusion_empty_list_pass_through(client, mock_deps):
    resp = client.get("/api/browse/artists")
    assert resp.status_code == 200
    assert len(resp.json()) >= 1


# ── most played endpoint (2026-06-10 most-played plan U1) ────────────────────

def _mp_row(tid, count, with_meta=True):
    meta = {"track_id": tid, "title": f"Song {tid}", "artist": "A", "album": "B",
            "thumb": None, "duration_ms": 1000, "server_name": "My Plex"} if with_meta else None
    return {"track_id": tid, "count": count, "metadata": meta}


def test_most_played_returns_count_desc_rows(mock_deps):
    """Covers AE1 (data): rows carry full track fields + play_count."""
    rows = [_mp_row("t5", 5), _mp_row("t3", 3)]
    with patch("app.database.get_top_played_tracks", AsyncMock(return_value=rows)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/most-played")
    data = resp.json()
    assert [d["play_count"] for d in data] == [5, 3]
    assert data[0]["title"] == "Song t5" and data[0]["track_id"] == "t5"


def test_most_played_backfills_missing_meta_and_skips_failures(mock_deps):
    """Covers AE4 + backfill: live lookup fills pre-feature counts; failures
    are skipped; successes are upserted for the next load."""
    _, plex = mock_deps
    ok_track = make_track("t-old")
    plex.get_track = AsyncMock(side_effect=[ok_track, KeyError("gone")])
    rows = [_mp_row("t-old", 9, with_meta=False), _mp_row("t-gone", 7, with_meta=False)]
    with patch("app.database.get_top_played_tracks", AsyncMock(return_value=rows)), \
         patch("app.database.set_play_track_meta", AsyncMock()) as meta:
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/most-played")
    data = resp.json()
    assert [d["track_id"] for d in data] == ["t-old"]  # t-gone skipped (AE4)
    assert data[0]["play_count"] == 9
    meta.assert_awaited_once()  # backfill write for the resolvable one


def test_most_played_respects_display_limit_setting(mock_deps):
    """The configured most_played_display_limit (default 100) is passed to the
    DB query — display-only cap, independent of Popular Random's pool."""
    gtp = AsyncMock(return_value=[_mp_row("t5", 5)])
    with patch("app.database.get_setting", AsyncMock(return_value="2")), \
         patch("app.database.get_top_played_tracks", gtp):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/most-played")
    assert resp.status_code == 200
    gtp.assert_awaited_once_with(2)


def test_most_played_empty_returns_empty_200(mock_deps):
    """Covers AE3 (data): fresh install → []"""
    with patch("app.database.get_top_played_tracks", AsyncMock(return_value=[])):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/most-played")
    assert resp.status_code == 200
    assert resp.json() == []


def test_most_played_serves_meta_rows_without_plex(mock_deps):
    """Review note pinned: DB-backed endpoint — meta rows serve even when
    no Plex client exists; only backfill is skipped."""
    rows = [_mp_row("t1", 2), _mp_row("t-nometa", 1, with_meta=False)]
    with patch("app.database.get_top_played_tracks", AsyncMock(return_value=rows)), \
         patch("app.state.get_plex_client", AsyncMock(return_value=None)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/most-played")
    data = resp.json()
    assert resp.status_code == 200
    assert [d["track_id"] for d in data] == ["t1"]


# ── album_id propagation (2026-06-10 clickable-names plan U1) ─────────────────
# Album-name taps drill by id; every REST track payload must carry album_id
# (None when albumless) so the frontend never name-guesses albums.

async def test_now_playing_includes_album_id(client, mock_deps):
    from app.queue.models import QueueItem
    qe, _ = mock_deps
    track = make_track("t1")
    track.album_id = "alb9"
    qe._current = QueueItem(track=track)
    qe._is_playing = True
    resp = client.get("/api/now-playing")
    assert resp.status_code == 200
    assert resp.json().get("album_id") == "alb9"


async def test_now_playing_idle_album_id_null(client, mock_deps):
    resp = client.get("/api/now-playing")
    assert resp.json()["album_id"] is None


async def test_queue_items_include_album_id(client, mock_deps):
    qe, _ = mock_deps
    track = make_track("t1")
    track.album_id = "alb7"
    await qe.append(track, bypass_lock=True)
    items = client.get("/api/queue").json()["queue"]
    assert len(items) == 1
    assert items[0]["album_id"] == "alb7"


async def test_queue_item_without_album_serializes_null_album_id(client, mock_deps):
    qe, _ = mock_deps
    await qe.append(make_track("t1"), bypass_lock=True)
    items = client.get("/api/queue").json()["queue"]
    assert items[0]["album_id"] is None


# ── /api/servers + source priority rank (collected-library plan U1) ──────────

def test_servers_endpoint_returns_name_and_owned(client, mock_deps):
    rows = [
        {"machine_id": "m1", "name": "Zeta", "owned": 1},
        {"machine_id": "m2", "name": "Alpha", "owned": 0},
        {"machine_id": "m3", "name": "Mu", "owned": None},
    ]
    with patch("app.database.get_plex_servers", AsyncMock(return_value=rows)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/servers")
    assert resp.status_code == 200
    data = resp.json()
    assert data == [
        {"name": "Zeta", "owned": True},
        {"name": "Alpha", "owned": False},
        {"name": "Mu", "owned": None},
    ]


def test_servers_endpoint_empty_when_no_servers(client, mock_deps):
    with patch("app.database.get_plex_servers", AsyncMock(return_value=[])):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/servers")
    assert resp.status_code == 200
    assert resp.json() == []


def test_server_rank_vectors():
    """MIRRORED CONTRACT with the JS rank twin in static/browse/index.js
    (collected-library plan U3) — owned first, known-unowned next,
    unknown last, alphabetical within each band. Change semantics there
    and here in lockstep."""
    from app.api.guest import _server_rank_key
    servers = [
        {"name": "Alpha", "owned": 0},
        {"name": "Zeta", "owned": 1},
        {"name": "Beta", "owned": 0},
        {"name": "Aleph", "owned": None},
        {"name": "Yankee", "owned": 1},
    ]
    ordered = [s["name"] for s in sorted(servers, key=_server_rank_key)]
    # Covers AE5: owned (Yankee, Zeta alpha) > unowned (Alpha, Beta) > unknown
    assert ordered == ["Yankee", "Zeta", "Alpha", "Beta", "Aleph"]


# ── album grouping with sources (collected-library plan U2) ──────────────────

def _albums_by_lib(plex, mapping):
    async def fake_get_albums(section_key, **kw):
        return mapping.get(section_key, [])
    plex.get_albums = AsyncMock(side_effect=fake_get_albums)


def test_browse_albums_groups_across_servers_with_sources(client, mock_deps):
    """Covers AE4 (cross-server half): one identity on two servers -> one
    row carrying both sources, emitted copy from the priority server
    (no ownership data -> alphabetical: Host before Shared)."""
    _, plex = mock_deps
    _albums_by_lib(plex, {
        "machineA:1": [Album(id="machineA:11", title="Purple Rain", artist="Prince")],
        "machineB:2": [Album(id="machineB:99", title="Purple Rain", artist="Prince")],
    })
    with patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        rows = c.get("/api/browse/albums").json()
    assert len(rows) == 1
    assert rows[0]["id"] == "machineA:11"  # Host wins alphabetically
    assert rows[0]["sources"] == [
        {"server_name": "Host", "album_id": "machineA:11"},
        {"server_name": "Shared", "album_id": "machineB:99"},
    ]


def test_browse_albums_within_server_copies_survive(client, mock_deps):
    """Covers AE4 (within-server half): two copies on ONE server both
    render (R3 re-permits within-server duplicates)."""
    _, plex = mock_deps
    _albums_by_lib(plex, {
        "machineA:1": [
            Album(id="machineA:11", title="Greatest Hits", artist="Prince"),
            Album(id="machineA:12", title="Greatest Hits", artist="Prince"),
        ],
        "machineB:2": [],
    })
    with patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        rows = c.get("/api/browse/albums").json()
    assert [r["id"] for r in rows] == ["machineA:11", "machineA:12"]
    for r in rows:
        assert len(r["sources"]) == 1


def test_album_priority_prefers_owned_server(client, mock_deps):
    """Covers AE5 (album pick): owned Shared beats unowned Host even though
    Host sorts first alphabetically."""
    _, plex = mock_deps
    _albums_by_lib(plex, {
        "machineA:1": [Album(id="machineA:11", title="Purple Rain", artist="Prince")],
        "machineB:2": [Album(id="machineB:99", title="Purple Rain", artist="Prince")],
    })
    servers = [
        {"machine_id": "machineA", "name": "Host", "owned": 0},
        {"machine_id": "machineB", "name": "Shared", "owned": 1},
    ]
    with patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())), \
         patch("app.database.get_plex_servers", AsyncMock(return_value=servers)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        rows = c.get("/api/browse/albums").json()
    assert len(rows) == 1
    assert rows[0]["id"] == "machineB:99"
    assert rows[0]["sources"][0]["server_name"] == "Shared"


def test_single_server_albums_carry_single_source(client, mock_deps):
    """Single-server installs: behavior identical to before, plus a
    one-entry sources list."""
    resp = client.get("/api/browse/albums")
    rows = resp.json()
    assert len(rows) >= 1
    assert len(rows[0]["sources"]) == 1


def test_year_albums_grouped_too(client, mock_deps):
    """Year lists previously skipped dedup; the grouping now applies."""
    _, plex = mock_deps
    _albums_by_lib(plex, {
        "machineA:1": [Album(id="machineA:11", title="1999", artist="Prince", year=1982)],
        "machineB:2": [Album(id="machineB:99", title="1999", artist="Prince", year=1982)],
    })
    with patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        rows = c.get("/api/browse/years/1982/albums").json()
    assert len(rows) == 1
    assert len(rows[0]["sources"]) == 2


def test_search_albums_carry_sources(client, mock_deps):
    resp = client.get("/api/search?q=Album")
    albums = resp.json()["albums"]
    assert albums and "sources" in albums[0]
    assert len(albums[0]["sources"]) == 1


# ── queue undo (collected-library plan U5) ────────────────────────────────────

async def test_append_track_returns_undo_receipt(client, mock_deps):
    qe, _ = mock_deps
    resp = client.post("/api/queue", json={"track_id": "t1"})
    assert resp.status_code == 200
    entry = resp.json()["entry"]
    assert entry["track_id"] == qe.queue[0].track_id
    assert entry["added_at"] == qe.queue[0].added_at


async def test_undo_removes_exactly_the_receipt_entry(client, mock_deps):
    """Covers AE7 (backend): duplicate same-track entries (distinct added_at)
    — only the receipt's entry is removed. U3: `removed` is now a count."""
    qe, _ = mock_deps
    first = client.post("/api/queue", json={"track_id": "t1"}).json()["entry"]
    entry = client.post("/api/queue", json={"track_id": "t1"}).json()["entry"]
    resp = client.post("/api/queue/undo", json=entry)
    assert resp.status_code == 200
    assert resp.json()["removed"] == 1
    assert len(qe.queue) == 1
    assert qe.queue[0].added_at == first["added_at"]  # the other entry survives


async def test_undo_missing_entry_is_quiet_noop(client, mock_deps):
    qe, _ = mock_deps
    entry = client.post("/api/queue", json={"track_id": "t1"}).json()["entry"]
    assert client.post("/api/queue/undo", json=entry).json()["removed"] == 1
    second = client.post("/api/queue/undo", json=entry)
    assert second.status_code == 200
    assert second.json()["removed"] == 0  # already gone — quiet no-op
    assert len(qe.queue) == 0


async def test_undo_garbage_receipt_is_200_noop(client, mock_deps):
    qe, _ = mock_deps
    client.post("/api/queue", json={"track_id": "t1"})
    resp = client.post("/api/queue/undo", json={"track_id": "<nope>", "added_at": "zzz"})
    assert resp.status_code == 200
    assert resp.json()["removed"] == 0
    assert len(qe.queue) == 1  # present-but-nonmatching is a no-op, not a reject


async def test_undo_batch_removes_album_as_a_unit(client, mock_deps):
    """U3: a batch (album) receipt removes all its still-upcoming entries in one
    call. Covers the album-as-unit removal path."""
    qe, _ = mock_deps
    entries = client.post("/api/queue", json={"album_id": "a1"}).json()["entries"]
    assert len(qe.queue) == len(entries) >= 1
    resp = client.post("/api/queue/undo", json={"entries": entries})
    assert resp.status_code == 200
    assert resp.json()["removed"] == len(entries)
    assert len(qe.queue) == 0


async def test_undo_batch_removes_only_present_entries(client, mock_deps):
    """U3: a batch where some entries are already gone removes only the present
    ones; `removed` reflects the actual count. (Two single adds stand in for a
    multi-track batch — the mock album resolves to one track.)"""
    qe, _ = mock_deps
    e1 = client.post("/api/queue", json={"track_id": "t1"}).json()["entry"]
    e2 = client.post("/api/queue", json={"track_id": "t2"}).json()["entry"]
    # e1 already gone (played / removed) before the batch undo.
    client.post("/api/queue/undo", json=e1)
    resp = client.post("/api/queue/undo", json={"entries": [e1, e2]})
    assert resp.json()["removed"] == 1  # only e2 was still present
    assert len(qe.queue) == 0


async def test_undo_empty_body_is_rejected(client, mock_deps):
    """U3: a body carrying neither a single receipt nor a non-empty entries
    list is a client error (not a silent no-op)."""
    qe, _ = mock_deps
    client.post("/api/queue", json={"track_id": "t1"})
    assert client.post("/api/queue/undo", json={}).status_code == 400
    assert client.post("/api/queue/undo", json={"entries": []}).status_code == 400
    assert len(qe.queue) == 1  # nothing removed


async def test_remove_entries_emits_single_queue_changed(mock_deps):
    """U3 (engine integration): a batch removal emits exactly one queue_changed
    for the whole batch, not one per entry."""
    qe, _ = mock_deps
    items = await qe.append_many(
        [make_track("t1"), make_track("t2"), make_track("t3")], bypass_lock=True)
    events = []

    async def _cb(ev, payload=None):
        events.append(ev)

    qe.add_callback(_cb)  # registered after append_many, so only the remove counts
    removed = await qe.remove_entries([(i.track_id, i.added_at) for i in items])
    assert removed == 3
    assert events.count("queue_changed") == 1
    assert len(qe.queue) == 0


async def test_album_append_returns_batch_receipt(client, mock_deps):
    """U2: album appends now return one receipt per created entry (`entries`,
    plural) so the guest UI can remove the album as a unit. The single-track
    `entry` (singular) shape is NOT used for albums."""
    qe, _ = mock_deps
    resp = client.post("/api/queue", json={"album_id": "a1"})
    assert resp.status_code == 200
    body = resp.json()
    assert "entry" not in body  # singular reserved for single-track adds
    entries = body["entries"]
    assert len(entries) == body["tracks_added"] == len(qe.queue)
    # Every receipt addresses a live upcoming queue item.
    for entry, engine_item in zip(entries, qe.queue):
        assert entry["track_id"] == engine_item.track_id
        assert entry["added_at"] == engine_item.added_at


async def test_append_many_returns_created_items(mock_deps):
    """U2 (engine): append_many hands back the items it created, in order, so
    the route can build the batch receipt under the same lock."""
    qe, _ = mock_deps
    items = await qe.append_many([make_track("t1"), make_track("t2")], bypass_lock=True)
    assert [i.track_id for i in items] == ["t1", "t2"]
    assert items == qe.queue  # same objects now live in the queue


# ── appearance defaults (2026-06-11 glow-up plan U1) ──────────────────────────

def test_appearance_fresh_install_defaults(client, mock_deps):
    with patch("app.database.get_setting", AsyncMock(return_value=None)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/appearance")
    assert resp.status_code == 200
    assert resp.json() == {"scheme": "gold-rush", "rail_mode": "vanilla", "view": "list",
                           "surprise_me_enabled": True,
                           "rail_alpha_mode": "english",
                           "rail_artist_threshold": 2, "rail_album_threshold": 2,
                           "ratings_visible_to_guests": False, "tags_visible_to_guests": False,
                           "browse_facets": {"genre": True, "years": True, "mostplayed": True,
                                             "recentlyadded": True, "highestrated": True},
                           "rating_style": "stars"}


def test_appearance_density_maps_to_waveform(client, mock_deps):
    """Covers AE6: stored legacy 'density' renders as waveform."""
    async def fake_get(key, default=None):
        return {"rail_mode": "density", "default_scheme": "rainy-purple"}.get(key)
    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        data = c.get("/api/appearance").json()
    assert data == {"scheme": "rainy-purple", "rail_mode": "waveform", "view": "list",
                    "surprise_me_enabled": True,
                    "rail_alpha_mode": "english",
                    "rail_artist_threshold": 2, "rail_album_threshold": 2,
                    "ratings_visible_to_guests": False, "tags_visible_to_guests": False,
                    "browse_facets": {"genre": True, "years": True, "mostplayed": True,
                                      "recentlyadded": True, "highestrated": True},
                    "rating_style": "stars"}


def test_appearance_garbage_values_fall_back(client, mock_deps):
    async def fake_get(key, default=None):
        return {"rail_mode": "spinny", "default_scheme": "hot-pink-disaster"}.get(key)
    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        data = c.get("/api/appearance").json()
    assert data == {"scheme": "gold-rush", "rail_mode": "vanilla", "view": "list",
                    "surprise_me_enabled": True,
                    "rail_alpha_mode": "english",
                    "rail_artist_threshold": 2, "rail_album_threshold": 2,
                    "ratings_visible_to_guests": False, "tags_visible_to_guests": False,
                    "browse_facets": {"genre": True, "years": True, "mostplayed": True,
                                      "recentlyadded": True, "highestrated": True},
                    "rating_style": "stars"}


def test_appearance_default_view_tile(client, mock_deps):
    """Tile-view U1: stored default_view='tile' surfaces on /api/appearance."""
    async def fake_get(key, default=None):
        return {"default_view": "tile"}.get(key)
    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        data = c.get("/api/appearance").json()
    assert data["view"] == "tile"


def test_appearance_view_garbage_falls_back_to_list(client, mock_deps):
    """Tile-view U1: an unknown stored view resolves to 'list'."""
    async def fake_get(key, default=None):
        return {"default_view": "mosaic"}.get(key)
    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        data = c.get("/api/appearance").json()
    assert data["view"] == "list"


# ── Browse index drill-ins (2026-06-21 plan U4) ──────────────────────────────

def test_artist_albums_warm_index_no_artist_fanout(mock_deps):
    """Covers AE1. Warm index resolves the artist's releases via an indexed
    lookup — no per-library artist fan-out — and collapses cross-server copies."""
    _, plex = mock_deps
    arow = _artist_idx_row("machineA:42", "Prince", server="Host")
    albums = [_album_idx_row("machineA:100", "Album A", "Prince", server="Host"),
              _album_idx_row("machineB:200", "Album A", "Prince", server="Shared"),
              _album_idx_row("machineB:201", "Album C", "Prince", server="Shared")]
    with patch("app.database.get_browse_artist_by_id", AsyncMock(return_value=arow)), \
         patch("app.database.get_browse_albums_for_artist", AsyncMock(return_value=albums)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/artists/machineA:42/albums")
    assert resp.status_code == 200
    titles = sorted(a["title"] for a in resp.json())
    assert titles == ["Album A", "Album C"]   # Album A collapsed across servers
    plex.get_artists.assert_not_called()       # zero full-list re-scan


def test_artist_albums_warm_index_no_rules_skips_roster_scan(mock_deps):
    """With no pattern rules (mock_deps default), the drill-in resolves via a
    single base-key lookup and must NOT scan the full artist roster (the O(1)
    fast path — review finding #5)."""
    arow = _artist_idx_row("A:1", "Prince")
    albums = [_album_idx_row("A:10", "Purple Rain", "Prince")]
    roster_mock = AsyncMock(return_value=[arow])
    with patch("app.database.get_browse_artist_by_id", AsyncMock(return_value=arow)), \
         patch("app.database.get_browse_albums_for_artist", AsyncMock(return_value=albums)), \
         patch("app.database.get_browse_artists", roster_mock):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/artists/A:1/albums")
    assert resp.status_code == 200
    assert [a["title"] for a in resp.json()] == ["Purple Rain"]
    roster_mock.assert_not_called()  # no full-roster normalize pass when rules empty


def test_artist_albums_warm_index_pattern_rule_sibling_merge(mock_deps):
    """Pattern-rule sibling spellings union at request time (no rebuild): an act
    split across two base spellings on two servers resolves whole."""
    _, plex = mock_deps
    rule = [["beatles", "the beatles"]]
    arow = _artist_idx_row("A:1", "The Beatles", server="ServerA")  # base "the beatles"
    roster = [arow, _artist_idx_row("B:1", "Beatles", server="ServerB")]  # base "beatles"
    by_base = {
        "the beatles": [_album_idx_row("A:10", "Abbey Road", "The Beatles", server="ServerA")],
        "beatles": [_album_idx_row("B:20", "Revolver", "Beatles", server="ServerB")],
    }

    async def _albs(bk):
        return by_base.get(bk, [])

    with patch("app.database.get_browse_artist_by_id", AsyncMock(return_value=arow)), \
         patch("app.database.get_browse_artists", AsyncMock(return_value=roster)), \
         patch("app.database.get_browse_albums_for_artist", AsyncMock(side_effect=_albs)), \
         patch("app.database.get_pattern_rules", AsyncMock(return_value=rule)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/artists/A:1/albums")
    assert resp.status_code == 200
    titles = sorted(a["title"] for a in resp.json())
    assert titles == ["Abbey Road", "Revolver"]   # both spellings unioned
    plex.get_artists.assert_not_called()


def test_artist_albums_warm_index_sorted_chronologically(mock_deps):
    """Regression (release-ordering bug 2026-06-23): an artist's own releases
    must drill in earliest-year-first, not in index/crawl order. The warm-index
    path (get_browse_albums_for_artist has no ORDER BY, _group_albums preserves
    insertion order) previously emitted crawl order — e.g. le_mol showed
    2013, 2017, 2015 instead of 2013, 2015, 2017."""
    arow = _artist_idx_row("A:1", "le_mol")
    # Deliberately out of chronological order, mirroring the reported le_mol case.
    albums = [_album_idx_row("A:3", "Kara Oh Kee", "le_mol", year=2015),
              _album_idx_row("A:1", "Aleph One", "le_mol", year=2013),
              _album_idx_row("A:2", "Heads Heads Heads", "le_mol", year=2017)]
    with patch("app.database.get_browse_artist_by_id", AsyncMock(return_value=arow)), \
         patch("app.database.get_browse_albums_for_artist", AsyncMock(return_value=albums)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/artists/A:1/albums")
    assert resp.status_code == 200
    assert [a["title"] for a in resp.json()] == ["Aleph One", "Kara Oh Kee", "Heads Heads Heads"]


def test_artist_albums_warm_index_same_year_title_tiebreak(mock_deps):
    """Same-year releases order by title so the drill-in is deterministic rather
    than dependent on index/crawl order (pins the sort's tiebreak)."""
    arow = _artist_idx_row("A:1", "X")
    albums = [_album_idx_row("A:2", "Bravo", "X", year=2000),
              _album_idx_row("A:1", "Alpha", "X", year=2000)]
    with patch("app.database.get_browse_artist_by_id", AsyncMock(return_value=arow)), \
         patch("app.database.get_browse_albums_for_artist", AsyncMock(return_value=albums)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/artists/A:1/albums")
    assert resp.status_code == 200
    assert [a["title"] for a in resp.json()] == ["Alpha", "Bravo"]


def test_album_tracks_warm_index_fetches_by_rating_key_no_fanout(mock_deps):
    """Covers AE2. Warm index resolves per-server copies and fetches tracks by
    exact rating-key (/children) — no whole-section album pull, no album re-fetch."""
    _, plex = mock_deps
    # A genuine cross-server shared album: same track_count on both servers is
    # what makes it fold under the content-aware gate (same-title plan U5).
    arow = _album_idx_row("machineA:100", "Kid A", "Radiohead", server="Host", track_count=10)
    copies = [arow, _album_idx_row("machineB:200", "Kid A", "Radiohead",
                                   server="Shared", track_count=10)]

    async def fake_tracks(section_key, album_id=None, **kw):
        return [make_track(tid=f"{album_id}-t1")]

    plex.get_tracks.side_effect = fake_tracks
    with patch("app.database.get_browse_album_by_id", AsyncMock(return_value=arow)), \
         patch("app.database.get_browse_albums_by_identity", AsyncMock(return_value=copies)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/albums/machineA:100/tracks")
    assert resp.status_code == 200
    assert len(resp.json()) == 2                 # one track per server copy
    plex.get_album.assert_not_called()           # no album metadata re-fetch
    plex.get_albums.assert_not_called()          # no whole-section pull
    fetched = {call.kwargs.get("album_id") for call in plex.get_tracks.call_args_list}
    assert fetched == {"machineA:100", "machineB:200"}


def test_album_tracks_index_miss_uses_live(mock_deps):
    """Index miss → live name-resolution fallback (get_album is consulted)."""
    _, plex = mock_deps  # get_browse_album_by_id defaults to None in mock_deps
    from app.main import app
    c = TestClient(app, raise_server_exceptions=True)
    resp = c.get("/api/browse/albums/a1/tracks")
    assert resp.status_code == 200
    plex.get_album.assert_called()               # live resolution used


# ── Artist grouping map drill-in (2026-06-22 rule-grouping plan U3) ──────────

def _grouping_albums_by_base():
    async def _albs(bk):
        return {
            "beyoncé": [_album_idx_row("A:10", "Lemonade", "Beyoncé", server="A")],
            "beyonce": [_album_idx_row("B:20", "4", "Beyonce", server="B")],
        }.get(bk, [])
    return _albs


def test_artist_albums_warm_grouping_map_no_roster_scan(mock_deps):
    """Covers AE1 + R1. Warm signature-guarded map → sibling base-keys resolved
    by O(1) lookup; the full roster is NOT scanned on the hot path."""
    arow = _artist_idx_row("A:1", "Beyoncé")          # base_key "beyoncé"
    grouping = {"beyonce": {"beyoncé", "beyonce"}}    # rule-norm → both spellings
    roster_mock = AsyncMock(return_value=[])
    with patch("app.database.get_browse_artist_by_id", AsyncMock(return_value=arow)), \
         patch("app.database.get_browse_albums_for_artist", AsyncMock(side_effect=_grouping_albums_by_base())), \
         patch("app.database.get_browse_artists", roster_mock), \
         patch("app.database.get_pattern_rules", AsyncMock(return_value=[["beyonce", "beyoncé"]])), \
         patch("app.state.get_artist_grouping", MagicMock(return_value=grouping)):
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/artists/A:1/albums")
    assert resp.status_code == 200
    assert sorted(a["title"] for a in resp.json()) == ["4", "Lemonade"]
    roster_mock.assert_not_called()   # no full-roster normalize pass


def test_artist_albums_stale_map_falls_back_to_scan(mock_deps):
    """Pins the async-rebuild decision (R4/R5): map not current (returns None) →
    drill-in falls back to the roster scan, returns the same union, and triggers
    a rebuild to warm the next request."""
    arow = _artist_idx_row("A:1", "Beyoncé")
    roster = [arow, _artist_idx_row("B:1", "Beyonce", server="B")]
    with patch("app.database.get_browse_artist_by_id", AsyncMock(return_value=arow)), \
         patch("app.database.get_browse_artists", AsyncMock(return_value=roster)), \
         patch("app.database.get_browse_albums_for_artist", AsyncMock(side_effect=_grouping_albums_by_base())), \
         patch("app.database.get_pattern_rules", AsyncMock(return_value=[["beyonce", "beyoncé"]])), \
         patch("app.state.get_artist_grouping", MagicMock(return_value=None)), \
         patch("app.state.trigger_artist_grouping_rebuild", MagicMock()) as trig:
        from app.main import app
        c = TestClient(app, raise_server_exceptions=True)
        resp = c.get("/api/browse/artists/A:1/albums")
    assert resp.status_code == 200
    assert sorted(a["title"] for a in resp.json()) == ["4", "Lemonade"]
    trig.assert_called_once()


def test_artist_albums_map_path_matches_fallback(mock_deps):
    """R5 parity (hard requirement): the map fast-path and the scan fallback
    produce byte-identical drill-in results for the same data."""
    arow = _artist_idx_row("A:1", "Beyoncé")
    roster = [arow, _artist_idx_row("B:1", "Beyonce", server="B")]
    grouping = {"beyonce": {"beyoncé", "beyonce"}}

    def run(grouping_ret):
        with patch("app.database.get_browse_artist_by_id", AsyncMock(return_value=arow)), \
             patch("app.database.get_browse_artists", AsyncMock(return_value=roster)), \
             patch("app.database.get_browse_albums_for_artist", AsyncMock(side_effect=_grouping_albums_by_base())), \
             patch("app.database.get_pattern_rules", AsyncMock(return_value=[["beyonce", "beyoncé"]])), \
             patch("app.state.get_artist_grouping", MagicMock(return_value=grouping_ret)), \
             patch("app.state.trigger_artist_grouping_rebuild", MagicMock()):
            from app.main import app
            c = TestClient(app, raise_server_exceptions=True)
            return c.get("/api/browse/artists/A:1/albums").json()

    assert run(grouping) == run(None)   # map-hit == fallback-scan


# ── Recently Added feed (plan 006) ────────────────────────────────────────────

def _ra_pair(id, title, artist, added_at, server):
    from app.plex.models import Album
    return Album(id=id, title=title, artist=artist, added_at=added_at), server


def test_recent_added_dedups_to_one_per_identity_earliest_date():
    """Covers AE1: same release on two servers → one row, dated by the earliest
    add; both servers listed in sources."""
    from app.api.guest import _recent_added
    tagged = [
        _ra_pair("A:1", "Kid A", "Radiohead", 2000, "A"),
        _ra_pair("B:9", "Kid A", "Radiohead", 9999, "B"),  # later add on server B
    ]
    out = _recent_added(tagged, (), {"A": 0, "B": 1}, 100)
    assert len(out) == 1
    assert out[0].added_at == 2000  # earliest wins
    assert {s["server_name"] for s in out[0].sources} == {"A", "B"}


def test_recent_added_new_server_import_does_not_resurface_old_catalog():
    """Covers AE2: re-adding an owned album to a new server keeps its old
    earliest date, so a genuinely-new release outranks it."""
    from app.api.guest import _recent_added
    tagged = [
        _ra_pair("OLD:1", "Abbey Road", "The Beatles", 100, "Old"),
        _ra_pair("NEW:1", "Abbey Road", "The Beatles", 9999, "New"),  # just imported
        _ra_pair("NEW:2", "Brand New", "Fresh Act", 9998, "New"),     # genuinely new
    ]
    out = _recent_added(tagged, (), {"Old": 0, "New": 1}, 100)
    by_title = {a.title: a for a in out}
    assert by_title["Abbey Road"].added_at == 100
    assert out[0].title == "Brand New"  # the only genuinely-recent release leads


def test_recent_added_orders_newest_first_and_caps():
    """Covers AE3: capped at the limit, newest-first."""
    from app.api.guest import _recent_added
    tagged = [_ra_pair(f"A:{i}", f"T{i}", "Ar", i, "A") for i in range(150)]
    out = _recent_added(tagged, (), {}, 100)
    assert len(out) == 100
    assert out[0].added_at == 149   # newest first
    assert out[-1].added_at == 50   # oldest 50 dropped by the cap


def test_recent_added_quiet_library_still_returns():
    """Covers AE4: all dates old → still non-empty, newest-first."""
    from app.api.guest import _recent_added
    tagged = [_ra_pair("A:1", "Old One", "Ar", 10, "A"),
              _ra_pair("A:2", "Older", "Ar", 5, "A")]
    out = _recent_added(tagged, (), {}, 100)
    assert [a.title for a in out] == ["Old One", "Older"]


def test_recent_added_none_dates_sort_last():
    from app.api.guest import _recent_added
    tagged = [_ra_pair("A:1", "Has Date", "Ar", 5, "A"),
              _ra_pair("A:2", "No Date", "Ar", None, "A")]
    out = _recent_added(tagged, (), {}, 100)
    assert [a.title for a in out] == ["Has Date", "No Date"]


def test_recent_added_same_date_tiebreak_title_asc():
    from app.api.guest import _recent_added
    tagged = [_ra_pair("A:2", "Bravo", "Ar", 5, "A"),
              _ra_pair("A:1", "Alpha", "Ar", 5, "A")]
    out = _recent_added(tagged, (), {}, 100)
    assert [a.title for a in out] == ["Alpha", "Bravo"]


def test_recent_added_earliest_ignores_none_among_copies():
    """A None-dated copy must not be treated as oldest within a group — the
    group's earliest is the min of its DATED copies."""
    from app.api.guest import _recent_added
    tagged = [_ra_pair("A:1", "Kid A", "Radiohead", None, "A"),
              _ra_pair("B:9", "Kid A", "Radiohead", 2000, "B")]
    out = _recent_added(tagged, (), {"A": 0, "B": 1}, 100)
    assert len(out) == 1 and out[0].added_at == 2000


def test_recently_added_empty_index_returns_empty_and_triggers_refresh(client):
    """Covers AE5 (backend): empty index → [] and a background refresh fires."""
    import app.state as st
    resp = client.get("/api/recently-added")
    assert resp.status_code == 200
    assert resp.json() == []
    assert st.trigger_browse_index_refresh.called


def test_recently_added_serves_index_deduped_and_dated(client):
    """End-to-end: route reads the index, dedups, dates by earliest, and the
    JSON carries added_at + sources."""
    rows = [
        {"album_id": "A:1", "title": "Kid A", "artist": "Radiohead", "year": 2000,
         "thumb": None, "subtype": None, "added_at": 2000, "server_name": "A", "section_key": "A:1"},
        {"album_id": "B:9", "title": "Kid A", "artist": "Radiohead", "year": 2000,
         "thumb": None, "subtype": None, "added_at": 9999, "server_name": "B", "section_key": "B:1"},
        {"album_id": "A:2", "title": "In Rainbows", "artist": "Radiohead", "year": 2007,
         "thumb": None, "subtype": None, "added_at": 3000, "server_name": "A", "section_key": "A:1"},
    ]
    with patch("app.database.get_browse_albums", AsyncMock(return_value=rows)), \
         patch("app.state.cache_is_fresh", AsyncMock(return_value=True)):
        resp = client.get("/api/recently-added")
    assert resp.status_code == 200
    data = resp.json()
    assert [d["title"] for d in data] == ["In Rainbows", "Kid A"]  # 3000 > earliest 2000
    kid = next(d for d in data if d["title"] == "Kid A")
    assert kid["added_at"] == 2000  # earliest, serialized to the client
    assert {s["server_name"] for s in kid["sources"]} == {"A", "B"}
