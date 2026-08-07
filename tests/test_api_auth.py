"""Regression tests for auth API routes after Plex OAuth removal."""

import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from app.main import app
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture
def mock_session():
    async def fake_validate(token):
        return token == "valid-token"
    with patch("app.auth.session.validate_session", side_effect=fake_validate):
        yield


@pytest.fixture
def authed_client(mock_session):
    from app.main import app
    c = TestClient(app, raise_server_exceptions=True)
    c.cookies.set("jukeplox_session", "valid-token")
    return c


# ── Removed Plex auth endpoints → 404 (AC2) ──────────────────────────────────

def test_plex_pin_endpoint_removed(client):
    resp = client.get("/admin/auth/plex/pin")
    assert resp.status_code == 404


def test_plex_poll_endpoint_removed(client):
    resp = client.get("/admin/auth/plex/poll/1?client_id=abc")
    assert resp.status_code == 404


# ── In-dashboard Plex connect unaffected (AC4) ────────────────────────────────

def test_plex_connect_pin_still_works(authed_client):
    with patch(
        "app.auth.plex_oauth.start_flow",
        AsyncMock(return_value={"id": 1, "code": "ABCD", "client_id": "x", "auth_url": "https://plex.tv"}),
    ):
        resp = authed_client.get("/admin/plex/connect/pin")
    assert resp.status_code == 200
    assert resp.json()["id"] == 1


# ── Local auth still works (AC3) ─────────────────────────────────────────────

def test_setup_still_works(client):
    with patch("app.database.get_setting", AsyncMock(return_value=None)), \
         patch("app.auth.local.set_password", AsyncMock()), \
         patch("app.database.set_setting", AsyncMock()), \
         patch("app.auth.session.create_session", AsyncMock(return_value="tok")), \
         patch("app.config.settings") as ms:
        ms.cookie_secure = False
        resp = client.post("/admin/auth/setup", json={"password": "hunter2-hunter2"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


# ── 12-char minimum (fresh-install audit F4: docs promised it, code now keeps it) ──

def test_setup_rejects_password_under_12_chars(client):
    with patch("app.database.get_setting", AsyncMock(return_value=None)):
        resp = client.post("/admin/auth/setup", json={"password": "elevenchars"})
    assert resp.status_code == 422   # 11 chars — Pydantic min_length=12


def test_change_password_rejects_short_new_password(authed_client):
    with patch("app.auth.local.verify_password", AsyncMock(return_value=True)):
        resp = authed_client.post(
            "/admin/auth/change-password",
            json={"current_password": "old", "new_password": "elevenchars"},
        )
    assert resp.status_code == 422   # new_password gated; current_password is not


def test_login_accepts_short_legacy_password(client):
    # Upgrade safety: an existing install with a pre-enforcement short password
    # must still log in — only setup/change gate on length, never login.
    with patch("app.auth.local.has_password", AsyncMock(return_value=True)), \
         patch("app.auth.local.verify_password", AsyncMock(return_value=True)), \
         patch("app.database.set_setting", AsyncMock()), \
         patch("app.auth.session.create_session", AsyncMock(return_value="tok")), \
         patch("app.config.settings") as ms:
        ms.cookie_secure = False
        resp = client.post("/admin/auth/login/local", json={"password": "short"})
    assert resp.status_code == 200


def test_login_local_still_works(client):
    with patch("app.auth.local.has_password", AsyncMock(return_value=True)), \
         patch("app.auth.local.verify_password", AsyncMock(return_value=True)), \
         patch("app.database.set_setting", AsyncMock()), \
         patch("app.auth.session.create_session", AsyncMock(return_value="tok")), \
         patch("app.config.settings") as ms:
        ms.cookie_secure = False
        resp = client.post("/admin/auth/login/local", json={"password": "hunter2"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
