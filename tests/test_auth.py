import pytest
from unittest.mock import AsyncMock

from app import database
from app.auth import local as local_auth
from app.auth import session as session_mgr
from app.config import Settings


@pytest.fixture
def tmp_settings(tmp_path, monkeypatch):
    s = Settings(data_dir=tmp_path, secret_key="test", session_ttl_hours=8)
    monkeypatch.setattr(database, "settings", s)
    monkeypatch.setattr(session_mgr, "settings", s)
    monkeypatch.setattr(local_auth, "database", database)
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


# ── local password ────────────────────────────────────────────────────────────

async def test_set_and_verify_password(db):
    await local_auth.set_password("hunter2")
    assert await local_auth.verify_password("hunter2") is True


async def test_wrong_password_fails(db):
    await local_auth.set_password("correct")
    assert await local_auth.verify_password("wrong") is False


async def test_no_password_fails(db):
    assert await local_auth.verify_password("anything") is False


async def test_has_password_false_initially(db):
    assert await local_auth.has_password() is False


async def test_has_password_true_after_set(db):
    await local_auth.set_password("pw")
    assert await local_auth.has_password() is True


# ── sessions ──────────────────────────────────────────────────────────────────

async def test_create_session_returns_token(db):
    token = await session_mgr.create_session()
    assert isinstance(token, str)
    assert len(token) == 64  # 32 bytes hex


async def test_validate_fresh_session(db):
    token = await session_mgr.create_session()
    assert await session_mgr.validate_session(token) is True


async def test_validate_nonexistent_session(db):
    assert await session_mgr.validate_session("bogus") is False


async def test_invalidate_session(db):
    token = await session_mgr.create_session()
    await session_mgr.invalidate_session(token)
    assert await session_mgr.validate_session(token) is False


async def test_expired_session_invalid(db, monkeypatch):
    from datetime import UTC, datetime, timedelta
    import app.auth.session as sess_module

    # Override _expires_str to return a past time
    monkeypatch.setattr(
        sess_module,
        "_expires_str",
        lambda: (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    )
    token = await session_mgr.create_session()
    assert await session_mgr.validate_session(token) is False


async def test_clean_expired_removes_old_sessions(db):
    token = await session_mgr.create_session()
    # Manually expire it
    await database.delete_session(token)
    await database.create_session(token, "2020-01-01T00:00:00+00:00", "2020-01-01T08:00:00+00:00")
    await session_mgr.clean_expired()
    assert await session_mgr.validate_session(token) is False


# ── plex_oauth.complete_flow() ────────────────────────────────────────────────

def _patch_flow(monkeypatch, *, servers, reconcile=None):
    """Common wiring: a resolved PIN, a canned discovery, no real DB writes."""
    from app import state
    from app.plex import health
    import app.plex.auth as plex_auth_mod

    monkeypatch.setattr(plex_auth_mod, "poll_pin", AsyncMock(return_value="TOKEN"))
    monkeypatch.setattr(plex_auth_mod, "discover_servers", AsyncMock(return_value=servers))
    monkeypatch.setattr(plex_auth_mod, "discover_server", AsyncMock(return_value="http://plex:32400"))
    monkeypatch.setattr("app.database.set_plex_config", AsyncMock())
    stored = AsyncMock()
    monkeypatch.setattr("app.database.set_plex_account_token", stored)
    rec = reconcile or AsyncMock(return_value=health.ReconcileResult())
    monkeypatch.setattr(health, "reconcile", rec)
    monkeypatch.setattr(state, "invalidate_plex_client", lambda: None)
    return stored, rec


async def test_complete_flow_returns_result_on_resolved_pin(monkeypatch):
    from app.auth import plex_oauth
    from app.plex import health
    _patch_flow(monkeypatch, servers=[])

    result = await plex_oauth.complete_flow(1, "client-x")
    assert isinstance(result, health.ReconcileResult)


async def test_complete_flow_returns_none_when_poll_fails(monkeypatch):
    from app.auth import plex_oauth
    import app.plex.auth as plex_auth_mod

    monkeypatch.setattr(plex_auth_mod, "poll_pin", AsyncMock(return_value=None))

    result = await plex_oauth.complete_flow(1, "client-x")
    assert result is None


async def test_complete_flow_stores_account_token_and_reconciles_with_connect_origin(monkeypatch):
    """KTD1/KTD3: the connect flow is no longer a writer of the server table —
    the listing goes through health.reconcile(origin="connect") and the account
    token is persisted for the refresh path."""
    from app.auth import plex_oauth
    from app.plex import health
    listing = [{"machine_id": "A", "server_url": "https://a.example.invalid:32400",
                "name": "A", "owner": "me", "owned": True, "token": "srv-tok",
                "client_id": "client-x", "probe": "reachable"}]
    want = health.ReconcileResult(added=["A"])
    stored, rec = _patch_flow(monkeypatch, servers=listing,
                              reconcile=AsyncMock(return_value=want))

    result = await plex_oauth.complete_flow(1, "client-x")

    stored.assert_awaited_once_with("TOKEN")
    rec.assert_awaited_once_with(listing, origin="connect")
    assert result is want
    assert not hasattr(__import__("app.database", fromlist=["x"]), "save_plex_servers")


async def test_complete_flow_zero_server_listing_writes_nothing(monkeypatch):
    """A plex.tv 200 listing zero servers never reaches reconcile (guard kept)."""
    from app.auth import plex_oauth
    _, rec = _patch_flow(monkeypatch, servers=[])
    await plex_oauth.complete_flow(1, "client-x")
    rec.assert_not_awaited()


async def test_complete_flow_invalidates_plex_client_on_success(monkeypatch):
    from app.auth import plex_oauth
    from app import state
    _patch_flow(monkeypatch, servers=[{"machine_id": "A", "server_url": "http://plex",
                                        "probe": "reachable"}])

    invalidated = []
    monkeypatch.setattr(state, "invalidate_plex_client", lambda: invalidated.append(True))

    await plex_oauth.complete_flow(1, "client-x")
    assert invalidated == [True]


async def test_complete_flow_no_account_lock(monkeypatch):
    """complete_flow() succeeds regardless of any stored admin_plex_username."""
    from app.auth import plex_oauth
    from app import database
    _patch_flow(monkeypatch, servers=[])

    get_setting_calls = []

    async def fake_get_setting(key):
        get_setting_calls.append(key)
        if key == "admin_plex_username":
            return "different-account"  # would have blocked in old code
        return None

    monkeypatch.setattr(database, "get_setting", fake_get_setting)

    result = await plex_oauth.complete_flow(1, "client-x")
    assert result is not None
    assert "admin_plex_username" not in get_setting_calls


async def test_complete_flow_no_set_setting_for_admin_plex_username(monkeypatch):
    """complete_flow() never writes admin_plex_username to the database."""
    from app.auth import plex_oauth
    from app import database
    _patch_flow(monkeypatch, servers=[])

    set_setting_calls = []

    async def fake_set_setting(key, value):
        set_setting_calls.append(key)

    monkeypatch.setattr(database, "set_setting", fake_set_setting)

    await plex_oauth.complete_flow(1, "client-x")
    assert "admin_plex_username" not in set_setting_calls


def test_fetch_plex_username_removed():
    """_fetch_plex_username no longer exists in plex_oauth."""
    from app.auth import plex_oauth
    assert not hasattr(plex_oauth, "_fetch_plex_username")


def test_plex_admin_user_key_removed():
    """_PLEX_ADMIN_USER_KEY constant no longer exists in plex_oauth."""
    from app.auth import plex_oauth
    assert not hasattr(plex_oauth, "_PLEX_ADMIN_USER_KEY")
