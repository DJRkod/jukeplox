"""Lifespan wiring for Plex source health (2026-11-21 plan, U3).

Startup registers the client 401 hook, schedules the startup refresh WITHOUT
awaiting it, and starts the daily sweep; shutdown cancels everything the health
module owns before the output backends are released. plex.tv is a ``respx``
mock that never answers, so a startup that awaited the refresh would hang —
``/api/version`` answering is the proof it did not.

Everything else lifespan touches (state.setup, the browse/catalog warms, the
device watcher, output release) is patched out: this file tests the health
wiring, not the rest of boot.
"""

import asyncio
import contextlib
import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app import database
from app.config import Settings
from app.plex import client as plex_client_mod
from app.plex import health

RESOURCES = "https://plex.tv/api/v2/resources"


@pytest.fixture
def boot(tmp_path, monkeypatch):
    """A tmp data dir, a seeded server row + account token (so the startup
    refresh reaches plex.tv), and the non-health lifespan collaborators mocked."""
    s = Settings(data_dir=tmp_path, secret_key="test")
    monkeypatch.setattr(database, "settings", s)
    monkeypatch.setattr("app.main.settings", s)
    health.reset_state()
    plex_client_mod.set_auth_failed_hook(None)

    async def seed():
        await database.init_db()
        await database.reconcile_plex_servers([{
            "machine_id": "A", "server_url": "https://a.example.invalid:32400",
            "name": "Server A", "owner": "me", "owned": True, "token": "tok-A",
            "client_id": "cid", "status": "usable"}], mark_missing=False)
        await database.set_plex_account_token("acct-token")
        await database.close_db()
    asyncio.run(seed())

    release = AsyncMock()
    stack = contextlib.ExitStack()
    for target, repl in [
        ("app.state.setup", AsyncMock()),
        ("app.state.load_prepared_answers", AsyncMock()),
        ("app.state.trigger_browse_index_refresh", MagicMock()),
        ("app.state.trigger_catalog_refresh", MagicMock()),
        ("app.api.guest.warm_enabled_libraries", MagicMock()),
        ("app.output.watcher.start_watcher", AsyncMock()),
        ("app.output.watcher.stop_watcher", AsyncMock()),
        ("app.output.idle_reattach.reconcile", MagicMock()),
        ("app.main._release_output_backends", release),
    ]:
        stack.enter_context(patch(target, repl))
    # The daily loop's first sleep is long; make it a never-returning wait so the
    # loop is provably alive (not finished) when shutdown cancels it.
    async def hang_sleep(_seconds):
        await asyncio.Event().wait()
    stack.enter_context(patch.object(health, "_sleep", hang_sleep))
    stack.enter_context(patch.object(health, "_now_local",
                                     lambda: datetime(2026, 11, 22, 2, 0)))
    with stack:
        yield MagicMock(release=release)
    health.reset_state()
    plex_client_mod.set_auth_failed_hook(None)


def test_startup_schedules_refresh_without_awaiting_and_shutdown_cancels_everything(boot):
    from app.main import app
    seen = {"resources": 0}

    async def never_answers(request):
        seen["resources"] += 1
        await asyncio.Event().wait()

    with respx.mock(assert_all_called=False) as rx:
        rx.get(RESOURCES).mock(side_effect=never_answers)
        with TestClient(app) as c:
            # Startup has returned (the client is usable) — now wait until the
            # background refresh is parked on plex.tv, which never answers.
            deadline = time.monotonic() + 5
            while (seen["resources"] < 1 or health.next_refresh_at() is None)                     and time.monotonic() < deadline:
                time.sleep(0.02)
            assert seen["resources"] == 1
            r = c.get("/api/version")
            assert r.status_code == 200                    # served while plex.tv hangs
            snap = health.status_snapshot()
            assert snap["running"] is True                 # refresh in flight, not awaited
            assert plex_client_mod._auth_failed_hook is health.on_server_401
            assert health._daily_task is not None and not health._daily_task.done()
            assert snap["next_refresh_at"] is not None
            daily_task = health._daily_task
            refresh_task = health._running
    assert seen["resources"] == 1                          # plex.tv was asked exactly once
    assert daily_task.cancelled()
    assert refresh_task.cancelled()
    assert health._daily_task is None
    boot.release.assert_awaited_once()


def test_shutdown_cancels_a_runner_registered_by_a_later_unit(boot):
    from app.main import app
    registered = {}

    async def hang():
        await asyncio.Event().wait()

    async def register():
        registered["task"] = health.own_task(asyncio.get_running_loop().create_task(hang()))

    with respx.mock(assert_all_called=False) as rx:
        rx.get(RESOURCES).mock(return_value=httpx.Response(200, json=[]))
        with TestClient(app) as c:
            c.portal.call(register)
    assert registered["task"].cancelled()
