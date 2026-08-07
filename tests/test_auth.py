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
    await database.init_db()
    return tmp_settings


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

async def test_complete_flow_returns_true_on_resolved_pin(monkeypatch):
    from app.auth import plex_oauth
    from app import state
    import app.plex.auth as plex_auth_mod

    monkeypatch.setattr(plex_auth_mod, "poll_pin", AsyncMock(return_value="TOKEN"))
    monkeypatch.setattr(plex_auth_mod, "discover_servers", AsyncMock(return_value=[]))
    monkeypatch.setattr(plex_auth_mod, "discover_server", AsyncMock(return_value="http://plex:32400"))
    monkeypatch.setattr("app.database.set_plex_config", AsyncMock())
    monkeypatch.setattr(state, "invalidate_plex_client", lambda: None)

    result = await plex_oauth.complete_flow(1, "client-x")
    assert result is True


async def test_complete_flow_returns_false_when_poll_fails(monkeypatch):
    from app.auth import plex_oauth
    import app.plex.auth as plex_auth_mod

    monkeypatch.setattr(plex_auth_mod, "poll_pin", AsyncMock(return_value=None))

    result = await plex_oauth.complete_flow(1, "client-x")
    assert result is False


async def test_complete_flow_invalidates_plex_client_on_success(monkeypatch):
    from app.auth import plex_oauth
    from app import state
    import app.plex.auth as plex_auth_mod

    monkeypatch.setattr(plex_auth_mod, "poll_pin", AsyncMock(return_value="TOKEN"))
    monkeypatch.setattr(plex_auth_mod, "discover_servers", AsyncMock(return_value=[{"uri": "http://plex"}]))
    monkeypatch.setattr("app.database.save_plex_servers", AsyncMock())

    invalidated = []
    monkeypatch.setattr(state, "invalidate_plex_client", lambda: invalidated.append(True))

    await plex_oauth.complete_flow(1, "client-x")
    assert invalidated == [True]


async def test_complete_flow_no_account_lock(monkeypatch):
    """complete_flow() succeeds regardless of any stored admin_plex_username."""
    from app.auth import plex_oauth
    from app import state, database
    import app.plex.auth as plex_auth_mod

    monkeypatch.setattr(plex_auth_mod, "poll_pin", AsyncMock(return_value="TOKEN"))
    monkeypatch.setattr(plex_auth_mod, "discover_servers", AsyncMock(return_value=[]))
    monkeypatch.setattr(plex_auth_mod, "discover_server", AsyncMock(return_value="http://plex:32400"))
    monkeypatch.setattr(state, "invalidate_plex_client", lambda: None)

    get_setting_calls = []

    async def fake_get_setting(key):
        get_setting_calls.append(key)
        if key == "admin_plex_username":
            return "different-account"  # would have blocked in old code
        return None

    monkeypatch.setattr(database, "get_setting", fake_get_setting)
    monkeypatch.setattr(database, "set_plex_config", AsyncMock())

    result = await plex_oauth.complete_flow(1, "client-x")
    assert result is True
    assert "admin_plex_username" not in get_setting_calls


async def test_complete_flow_no_set_setting_for_admin_plex_username(monkeypatch):
    """complete_flow() never writes admin_plex_username to the database."""
    from app.auth import plex_oauth
    from app import state, database
    import app.plex.auth as plex_auth_mod

    monkeypatch.setattr(plex_auth_mod, "poll_pin", AsyncMock(return_value="TOKEN"))
    monkeypatch.setattr(plex_auth_mod, "discover_servers", AsyncMock(return_value=[]))
    monkeypatch.setattr(plex_auth_mod, "discover_server", AsyncMock(return_value="http://plex:32400"))
    monkeypatch.setattr(state, "invalidate_plex_client", lambda: None)

    set_setting_calls = []

    async def fake_set_setting(key, value):
        set_setting_calls.append(key)

    monkeypatch.setattr(database, "set_setting", fake_set_setting)
    monkeypatch.setattr(database, "set_plex_config", AsyncMock())

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
