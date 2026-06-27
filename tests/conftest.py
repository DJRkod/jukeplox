"""Shared fixtures and factories for the admin-facing API test modules.

Moved out of tests/test_api_admin.py when the Skip Back transport tests were
extracted to tests/test_api_playback.py (2026-06-09 layout plan, code-review
finding #1) so both modules consume one fixture set. Modules with their own
local fixtures (e.g., tests/test_api_guest.py's `client`) override these per
pytest's normal resolution order.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

from app.plex.models import Track, Album, Artist


def make_track(tid="t1") -> Track:
    return Track(id=tid, title="Song", artist="A", album="B", duration_ms=180000,
                 stream_key="/parts/1/f.flac")


def make_album(aid="a1") -> Album:
    return Album(id=aid, title="Album", artist="Artist A", year=2024)


def make_artist(arid="ar1") -> Artist:
    return Artist(id=arid, title="Artist A")


def _authenticated_client(app):
    """Return a TestClient with a pre-set session cookie."""
    client = TestClient(app, raise_server_exceptions=True)
    client.cookies.set("jukeplox_session", "valid-token")
    return client


@pytest.fixture
def mock_session():
    """Patch session validation to always succeed for 'valid-token'."""
    async def fake_validate(token):
        return token == "valid-token"
    with patch("app.auth.session.validate_session", side_effect=fake_validate):
        yield


@pytest.fixture
def mock_state(mock_session):
    """Wire up a mock QueueEngine and OutputRouter in app.state."""
    from app.queue.engine import QueueEngine
    from app.output.router import OutputRouter

    qe = QueueEngine()
    or_ = MagicMock(spec=OutputRouter)
    or_.pause = AsyncMock()
    or_.resume = AsyncMock()
    or_.stop = AsyncMock()
    or_.set_volume = AsyncMock()
    or_.play = AsyncMock()
    or_.active = MagicMock()

    # Live-discovery state hygiene (U5: the route's _devices_cache globals
    # are gone — the watcher registry replaced them). Reset the watcher
    # singleton so a test-installed instance never leaks its registry into
    # the next test, and drop the probe semaphore — it lazily binds to the
    # event loop active at first use, and every TestClient runs its own.
    import app.output.watcher as watcher_module
    import app.output.probe_runner as probe_runner_module
    watcher_module._watcher = None
    probe_runner_module._probe_semaphore = None

    # Default probe_cache patches: admin tests don't exercise verdict
    # storage, so route the calls through AsyncMocks that never touch
    # the real settings store. Tests that DO want to assert on
    # probe_cache behavior override these locally.
    with patch("app.state.queue_engine", qe), \
         patch("app.queue.engine.database.save_queue", AsyncMock()), \
         patch("app.queue.engine.database.save_history", AsyncMock()), \
         patch("app.state.output_router", or_), \
         patch("app.state.get_plex_client", AsyncMock(return_value=None)), \
         patch("app.state.trigger_browse_index_refresh", MagicMock()), \
         patch("app.state.trigger_artist_grouping_rebuild", MagicMock()), \
         patch("app.database.get_browse_album_by_id", AsyncMock(return_value=None)), \
         patch("app.database.get_browse_albums_by_identity", AsyncMock(return_value=[])), \
         patch("app.state.direct_backend", None), \
         patch("app.state.chromecast_backend", None), \
         patch("app.state.dlna_backend", None), \
         patch("app.state.airplay_backend", None), \
         patch("app.output.probe_cache.fetch_all", AsyncMock(return_value={})), \
         patch("app.output.probe_cache.clear_all_verdicts", AsyncMock()), \
         patch("app.output.probe_cache.set_verdict", AsyncMock()):
        yield qe, or_

    # Teardown mirror of the pre-test reset: a watcher a test installed
    # must not survive into tests that don't use this fixture.
    watcher_module._watcher = None
    probe_runner_module._probe_semaphore = None


@pytest.fixture
def client(mock_state):
    from app.main import app
    return _authenticated_client(app)


@pytest.fixture
def anon_client():
    from app.main import app
    return TestClient(app, raise_server_exceptions=True)
