"""Tests for app.output.dacp — the DACP HTTP server for speaker-initiated
volume callbacks. The server runs as a real asyncio.start_server bound to
loopback for these tests; httpx makes the HTTP requests.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest


# ───────────────────────────────────────────────────────────────────────────
# Test fixtures
# ───────────────────────────────────────────────────────────────────────────


@pytest.fixture
async def dacp_server(monkeypatch):
    """Start a DacpServer for the test, tear it down after.

    Persistence is intercepted so each test gets a fresh DACP-ID instead of
    sharing one across the suite via the real settings table.
    """
    from app import database as db
    state: dict[str, str] = {}

    async def fake_get_setting(key):
        return state.get(key)

    async def fake_set_setting(key, value):
        state[key] = value

    monkeypatch.setattr(db, "get_setting", fake_get_setting)
    monkeypatch.setattr(db, "set_setting", fake_set_setting)

    from app.output.dacp import DacpServer
    server = DacpServer()
    await server.start(shared_aiozc=None)  # no mDNS publish in unit tests
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
def base_url(dacp_server):
    return f"http://127.0.0.1:{dacp_server.port}"


# ───────────────────────────────────────────────────────────────────────────
# Tests
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_server_binds_in_ma_port_range(dacp_server):
    """MA's chosen range is 39831-49831. We don't expose the port for
    callers to configure — the speaker reads it from mDNS — but it should
    always land in this window."""
    assert 39831 <= dacp_server.port <= 49831


@pytest.mark.asyncio
async def test_dacp_id_is_persistent(monkeypatch):
    """Two starts against the same settings store should yield the same
    DACP-ID so AP2 pair-verify state survives Jukeplox restarts."""
    from app import database as db
    state: dict[str, str] = {}

    async def _get(k):
        return state.get(k)

    async def _set(k, v):
        state[k] = v

    monkeypatch.setattr(db, "get_setting", _get)
    monkeypatch.setattr(db, "set_setting", _set)

    from app.output.dacp import DacpServer
    s1 = DacpServer()
    await s1.start(shared_aiozc=None)
    id1 = s1.dacp_id
    await s1.stop()

    s2 = DacpServer()
    await s2.start(shared_aiozc=None)
    id2 = s2.dacp_id
    await s2.stop()

    assert id1 == id2
    assert len(id1) == 16
    assert id1.upper() == id1  # uppercase hex


@pytest.mark.asyncio
async def test_dacp_id_format_is_16_hex_chars(dacp_server):
    """MA's convention: 16-character uppercase hex string."""
    assert len(dacp_server.dacp_id) == 16
    int(dacp_server.dacp_id, 16)  # parseable as hex
    assert dacp_server.dacp_id.upper() == dacp_server.dacp_id


@pytest.mark.asyncio
async def test_setproperty_with_valid_active_remote_fires_callback(
    dacp_server, base_url
):
    """The happy path: speaker reports new absolute volume; server fires
    the registered callback."""
    session = dacp_server.new_session()

    captured: list[tuple[float, bool]] = []

    async def _on_change(sess, value, absolute):
        captured.append((value, absolute))

    dacp_server._on_volume_change = _on_change

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{base_url}/ctrl-int/1/setproperty",
            params={"dmcp.device-volume": "0.65"},
            headers={"Active-Remote": str(session.active_remote_id)},
        )
    assert resp.status_code == 204
    assert captured == [(0.65, True)]


@pytest.mark.asyncio
async def test_setproperty_accepts_dmcp_volume_alias(dacp_server, base_url):
    """Some receiver firmwares emit dmcp.volume instead of
    dmcp.device-volume. Both reach the same callback."""
    session = dacp_server.new_session()
    captured: list[float] = []

    async def _on_change(sess, value, absolute):
        captured.append(value)

    dacp_server._on_volume_change = _on_change

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{base_url}/ctrl-int/1/setproperty",
            params={"dmcp.volume": "0.3"},
            headers={"Active-Remote": str(session.active_remote_id)},
        )
    assert resp.status_code == 204
    assert captured == [0.3]


@pytest.mark.asyncio
async def test_request_without_active_remote_returns_401(dacp_server, base_url):
    """No header → 401. Defends against rogue LAN callers."""
    captured: list = []
    dacp_server._on_volume_change = AsyncMock(side_effect=captured.append)

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{base_url}/ctrl-int/1/setproperty",
            params={"dmcp.device-volume": "0.5"},
        )
    assert resp.status_code == 401
    assert captured == []


@pytest.mark.asyncio
async def test_request_with_invalid_active_remote_returns_401(dacp_server, base_url):
    """Bogus token → 401, no callback. The token is per-session; a stale
    one from a closed session must not be honored."""
    captured: list = []
    dacp_server._on_volume_change = AsyncMock(side_effect=captured.append)

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{base_url}/ctrl-int/1/setproperty",
            params={"dmcp.device-volume": "0.5"},
            headers={"Active-Remote": "99999999"},  # unregistered
        )
    assert resp.status_code == 401
    assert captured == []


@pytest.mark.asyncio
async def test_volumeup_fires_relative_callback(dacp_server, base_url):
    """Hardware button presses come as /volumeup or /volumedown without a
    level. We forward as a relative delta; the application layer decides
    step size."""
    session = dacp_server.new_session()
    captured: list[tuple[float, bool]] = []

    async def _on_change(sess, value, absolute):
        captured.append((value, absolute))

    dacp_server._on_volume_change = _on_change

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{base_url}/ctrl-int/1/volumeup",
            headers={"Active-Remote": str(session.active_remote_id)},
        )
    assert resp.status_code == 204
    assert len(captured) == 1
    value, absolute = captured[0]
    assert absolute is False
    assert value > 0  # positive delta


@pytest.mark.asyncio
async def test_volumedown_emits_negative_delta(dacp_server, base_url):
    session = dacp_server.new_session()
    captured: list[float] = []

    async def _on_change(sess, value, absolute):
        captured.append(value)

    dacp_server._on_volume_change = _on_change

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{base_url}/ctrl-int/1/volumedown",
            headers={"Active-Remote": str(session.active_remote_id)},
        )
    assert resp.status_code == 204
    assert captured[0] < 0


@pytest.mark.asyncio
async def test_unknown_path_returns_404(dacp_server, base_url):
    session = dacp_server.new_session()
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{base_url}/some/other/path",
            headers={"Active-Remote": str(session.active_remote_id)},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_end_session_invalidates_token(dacp_server, base_url):
    """After end_session(), the token is gone. Stale callbacks from the
    speaker (it doesn't know we closed the session) get 401."""
    session = dacp_server.new_session()
    captured: list = []
    dacp_server._on_volume_change = AsyncMock(side_effect=captured.append)

    dacp_server.end_session(session.active_remote_id)

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{base_url}/ctrl-int/1/setproperty",
            params={"dmcp.device-volume": "0.5"},
            headers={"Active-Remote": str(session.active_remote_id)},
        )
    assert resp.status_code == 401
    assert captured == []


@pytest.mark.asyncio
async def test_new_session_returns_unique_active_remotes(dacp_server):
    s1 = dacp_server.new_session()
    s2 = dacp_server.new_session()
    assert s1.active_remote_id != s2.active_remote_id
    assert s1.dacp_id == s2.dacp_id  # same server, same id
