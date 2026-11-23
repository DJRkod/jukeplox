"""Plex source health: reconcile, single-flight, cooldown, account status
(2026-11-21 fix-plex-source-health plan, U2).

plex.tv and every Plex server are ``respx`` mocks; the database is a fresh
SQLite file per test. Hostnames are synthetic (``*.example.invalid``) and
tokens are fakes.
"""

import asyncio
import dataclasses
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app import database
from app.api import guest as guest_api
from app.config import Settings
from app.plex import health
from app.plex import auth as plex_auth

RESOURCES = "https://plex.tv/api/v2/resources"
USER = "https://plex.tv/api/v2/user"
URL_A = "https://a.example.invalid:32400"
URL_B = "https://b.example.invalid:32400"
URL_C = "https://c.example.invalid:32400"


@pytest.fixture
def tmp_settings(tmp_path, monkeypatch):
    s = Settings(data_dir=tmp_path, secret_key="test")
    monkeypatch.setattr(database, "settings", s)
    return s


@pytest.fixture
async def db(tmp_settings):
    await database.init_db()
    try:
        yield tmp_settings
    finally:
        await database.close_db()


@pytest.fixture
def env(db, monkeypatch):
    """Fresh health module state + the state.* side effects as mocks."""
    from app import state
    health.reset_state()
    inv = MagicMock()
    browse = MagicMock()
    catalog = MagicMock()
    monkeypatch.setattr(state, "invalidate_plex_client", inv)
    monkeypatch.setattr(state, "trigger_browse_index_refresh", browse)
    monkeypatch.setattr(state, "trigger_catalog_refresh", catalog)
    monkeypatch.setattr(state, "_plex_client", None)
    monkeypatch.setattr(guest_api, "_enabled_libs_gen", 0)
    clock = [1000.0]
    monkeypatch.setattr(health, "_monotonic", lambda: clock[0])
    yield SimpleNamespace(invalidate=inv, browse=browse, catalog=catalog, clock=clock)
    health.reset_state()


# ── helpers ──────────────────────────────────────────────────────────────────

def entry(mid, url, *, owned=True, token=None, probe="reachable", name=None):
    return {"machine_id": mid, "server_url": url, "name": name or f"Server {mid}",
            "owner": "me" if owned else "friend", "owned": owned,
            "token": token or f"tok-{mid}", "client_id": "cid", "probe": probe}


def sections_json(*specs):
    out = []
    for spec in specs:
        if isinstance(spec, tuple):
            key, title = spec
            typ = "artist"
        else:
            key, title, typ = spec["key"], spec["title"], spec["type"]
        out.append({"key": str(key), "title": title, "type": typ})
    return {"MediaContainer": {"Directory": out}}


def mock_sections(respx_mock, url, *specs, status=200):
    return respx_mock.get(f"{url}/library/sections").mock(
        return_value=httpx.Response(status, json=sections_json(*specs) if status == 200 else {}))


async def seed_rows(*entries, status="usable"):
    rows = [dict(e, status=status) for e in entries]
    for r in rows:
        r.pop("probe", None)
    return await database.reconcile_plex_servers(rows, mark_missing=False)


async def rows_by_id():
    return {r["machine_id"]: r for r in await database.get_plex_servers(include_vanished=True)}


def resource(mid, *, owned=True, token=None, connections=None, name=None):
    return {"provides": "server", "clientIdentifier": mid, "name": name or f"Server {mid}",
            "owned": owned, "sourceTitle": "" if owned else "friend",
            "accessToken": token or f"tok-{mid}",
            "connections": connections if connections is not None else []}


def conn(uri, *, local=False, relay=False):
    return {"uri": uri, "address": "203.0.113.1", "port": 32400, "local": local,
            "relay": relay, "protocol": "https"}


# ── AE2: changed share token ─────────────────────────────────────────────────

async def test_changed_share_token_updates_row_and_live_client_without_invalidate(env, respx_mock):
    from app import state
    from app.plex.client import PlexClient
    from app.sources.plex import PlexSource
    from app.sources.registry import SourceRegistry
    await seed_rows(entry("S", URL_B, owned=False, token="old-token"))
    await database.toggle_library("S:7", "Friend Music", True)
    await database.add_plex_seen_libraries("S", ["7"])
    live = PlexClient(URL_B, "old-token", "cid", machine_id="S")
    state._plex_client = SourceRegistry([PlexSource(live)])
    mock_sections(respx_mock, URL_B, ("7", "Friend Music"))

    result = await health.reconcile([entry("S", URL_B, owned=False, token="new-token")],
                                    origin="daily")

    rows = await rows_by_id()
    assert rows["S"]["token"] == "new-token"
    assert rows["S"]["status"] == "usable"
    assert [r["section_key"] for r in await database.get_enabled_libraries()] == ["S:7"]
    assert live.token == "new-token"
    env.invalidate.assert_not_called()
    assert result.changed_tokens == ["S"]
    assert result.added == [] and result.returned == [] and result.vanished == []
    # No token value anywhere in the structured result.
    assert "new-token" not in repr(dataclasses.asdict(result))


async def test_changed_url_updates_row_and_live_client_without_invalidate(env, respx_mock):
    from app import state
    from app.plex.client import PlexClient
    from app.sources.plex import PlexSource
    from app.sources.registry import SourceRegistry
    await seed_rows(entry("A", URL_A))
    await database.add_plex_seen_libraries("A", ["1"])
    live = PlexClient(URL_A, "tok-A", "cid", machine_id="A")
    state._plex_client = SourceRegistry([PlexSource(live)])
    mock_sections(respx_mock, URL_C, ("1", "Music"))

    await health.reconcile([entry("A", URL_C)], origin="daily")

    assert (await rows_by_id())["A"]["server_url"] == URL_C
    assert live.server_url == URL_C
    env.invalidate.assert_not_called()


# ── AE3: 401 limiter ─────────────────────────────────────────────────────────

async def test_401_requests_are_limited_to_one_per_fifteen_minutes(env, monkeypatch):
    runs = []

    async def fake_run(reason):
        runs.append(reason)

    monkeypatch.setattr(health, "run_refresh", fake_run)
    scheduled = []
    for _ in range(10):
        scheduled.append(health.request_refresh("401"))
        if health._running is not None:
            await health._running
        env.clock[0] += 6  # ten calls inside one minute
    assert scheduled == [True] + [False] * 9
    assert runs == ["401"]

    env.clock[0] += 15 * 60
    assert health.request_refresh("401") is True
    await health._running
    assert runs == ["401", "401"]


async def test_request_refresh_drops_while_a_run_is_in_flight(env, monkeypatch):
    gate = asyncio.Event()

    async def slow_run(reason):
        await gate.wait()

    monkeypatch.setattr(health, "run_refresh", slow_run)
    assert health.request_refresh("startup") is True
    await asyncio.sleep(0)
    assert health.request_refresh("daily") is False
    gate.set()
    await health._running
    assert health.request_refresh("daily") is True
    await health._running


# ── AE4: plex.tv unreachable ─────────────────────────────────────────────────

async def test_plex_tv_connection_error_leaves_rows_identical_and_sets_cooldown(env, respx_mock):
    await seed_rows(entry("A", URL_A))
    await database.set_plex_account_token("acct-token")
    before = await rows_by_id()
    respx_mock.get(RESOURCES).mock(side_effect=httpx.ConnectError("down"))

    assert await health.run_refresh("daily") is None

    assert await rows_by_id() == before
    assert health.request_refresh("daily") is False  # cooldown holds
    env.clock[0] += 15 * 60 + 1
    assert health.status_snapshot()["cooldown_remaining_s"] == 0
    assert health.account_status == "ok"


async def test_plex_tv_429_is_no_change_plus_cooldown_not_account_rejected(env, respx_mock):
    await seed_rows(entry("A", URL_A))
    await database.set_plex_account_token("acct-token")
    before = await rows_by_id()
    respx_mock.get(RESOURCES).mock(return_value=httpx.Response(429))

    await health.run_refresh("daily")

    assert await rows_by_id() == before
    assert health.account_status == "ok"
    snap = health.status_snapshot()
    assert 0 < snap["cooldown_remaining_s"] <= 15 * 60


async def test_plex_tv_401_sets_account_rejected_and_backoff_doubles_then_resets(env, respx_mock):
    await seed_rows(entry("A", URL_A))
    await database.set_plex_account_token("acct-token")
    route = respx_mock.get(RESOURCES).mock(return_value=httpx.Response(401))

    expected = [3600, 7200, 14400]
    for want in expected:
        await health.run_refresh("daily")
        assert health.account_status == "account_rejected"
        assert health.status_snapshot()["cooldown_remaining_s"] == want
        assert health.request_refresh("daily") is False
        env.clock[0] += want + 1

    route.mock(return_value=httpx.Response(200, json=[]))
    await health.run_refresh("daily")
    assert health.account_status == "ok"
    assert health.status_snapshot()["account_backoff_s"] == 3600


# ── AE9: new shared server ───────────────────────────────────────────────────

async def test_new_shared_server_usable_seen_not_enabled(env, respx_mock):
    await seed_rows(entry("A", URL_A))
    await database.add_plex_seen_libraries("A", ["1"])
    mock_sections(respx_mock, URL_A, ("1", "Music"))
    mock_sections(respx_mock, URL_B, ("7", "Friend Music"), ("8", "More"))

    result = await health.reconcile([entry("A", URL_A), entry("S", URL_B, owned=False)],
                                    origin="daily")

    assert result.added == ["S"]
    rows = await rows_by_id()
    assert rows["S"]["status"] == "usable" and rows["S"]["owned"] == 0
    assert await database.get_enabled_libraries() == []
    assert await database.get_plex_seen_libraries("S") == ["7", "8"]
    env.invalidate.assert_called()  # membership changed
    # nothing enabled → nothing to crawl → no pending flag
    assert await database.get_setting("plex_rebuild_pending") is None


# ── AE7: vanish and return ───────────────────────────────────────────────────

async def test_omitted_server_marked_no_longer_shared_and_content_dropped(env, respx_mock, monkeypatch):
    from app.catalog import store
    await seed_rows(entry("A", URL_A), entry("S", URL_B, owned=False))
    await database.add_plex_seen_libraries("A", ["1"])
    await database.add_plex_seen_libraries("S", ["7"])
    await database.toggle_library("S:7", "Friend Music", True)
    deleted = AsyncMock()
    monkeypatch.setattr(store, "delete_catalog_source", deleted)
    mock_sections(respx_mock, URL_A, ("1", "Music"))

    result = await health.reconcile([entry("A", URL_A)], origin="daily")

    assert result.vanished == ["S"]
    rows = await rows_by_id()
    assert rows["S"]["status"] == "no_longer_shared" and rows["S"]["missing_since"]
    assert rows["S"]["token"] == "tok-S"  # row retained
    assert [r["section_key"] for r in await database.get_enabled_libraries()] == ["S:7"]
    deleted.assert_awaited_once_with("S")
    env.invalidate.assert_called_once()
    env.browse.assert_called_once()
    assert await database.get_setting("plex_rebuild_pending") is None


async def test_returned_server_usable_invalidated_and_pending_set(env, respx_mock, monkeypatch):
    from app.catalog import store
    monkeypatch.setattr(store, "delete_catalog_source", AsyncMock())
    await seed_rows(entry("A", URL_A), entry("S", URL_B, owned=False))
    await database.add_plex_seen_libraries("A", ["1"])
    await database.add_plex_seen_libraries("S", ["7"])
    mock_sections(respx_mock, URL_A, ("1", "Music"))
    mock_sections(respx_mock, URL_B, ("7", "Friend Music"))
    await health.reconcile([entry("A", URL_A)], origin="daily")
    env.invalidate.reset_mock()

    result = await health.reconcile([entry("A", URL_A), entry("S", URL_B, owned=False)],
                                    origin="daily")

    assert result.returned == ["S"] and result.added == []
    rows = await rows_by_id()
    assert rows["S"]["status"] == "usable" and rows["S"]["missing_since"] is None
    env.invalidate.assert_called_once()
    assert await database.get_setting("plex_rebuild_pending")
    # A returned shared server's libraries are not reseeded.
    assert await database.get_enabled_libraries() == []


async def test_pending_flag_is_not_rewritten_when_already_set(env, respx_mock):
    await database.set_setting("plex_rebuild_pending", "2026-01-01T00:00:00+00:00")
    mock_sections(respx_mock, URL_A, ("1", "Music"))
    await health.reconcile([entry("A", URL_A)], origin="daily")  # added owned → would set
    assert await database.get_setting("plex_rebuild_pending") == "2026-01-01T00:00:00+00:00"


async def test_omitted_server_plus_probe_failure_elsewhere(env, respx_mock, monkeypatch):
    from app.catalog import store
    monkeypatch.setattr(store, "delete_catalog_source", AsyncMock())
    await seed_rows(entry("A", URL_A), entry("B", URL_B), entry("S", URL_C, owned=False))
    for mid in "ABS":
        await database.add_plex_seen_libraries(mid, ["1"])
    mock_sections(respx_mock, URL_A, ("1", "Music"))

    result = await health.reconcile(
        [entry("A", URL_A), entry("B", URL_B, probe="unreachable")], origin="daily")

    rows = await rows_by_id()
    assert result.vanished == ["S"]
    assert rows["S"]["status"] == "no_longer_shared"
    assert rows["B"]["status"] == "unreachable" and rows["B"]["server_url"] == URL_B
    assert rows["A"]["status"] == "usable"
    assert respx_mock.calls.call_count == 1  # unreachable B was never asked for sections


async def test_sections_connection_error_is_unreachable_with_url_kept(env, respx_mock):
    await seed_rows(entry("A", URL_A))
    await database.add_plex_seen_libraries("A", ["1"])
    respx_mock.get(f"{URL_A}/library/sections").mock(side_effect=httpx.ConnectTimeout("slow"))
    await health.reconcile([entry("A", URL_A)], origin="daily")
    row = (await rows_by_id())["A"]
    assert row["status"] == "unreachable" and row["server_url"] == URL_A


async def test_sections_401_with_identity_200_is_token_rejected(env, respx_mock):
    await seed_rows(entry("A", URL_A))
    await database.add_plex_seen_libraries("A", ["1"])
    respx_mock.get(f"{URL_A}/identity").mock(return_value=httpx.Response(200))
    mock_sections(respx_mock, URL_A, status=401)
    await health.reconcile([entry("A", URL_A)], origin="daily")
    assert (await rows_by_id())["A"]["status"] == "token_rejected"


# ── full refresh through plex.tv (discover_servers typed outcome) ───────────

async def test_empty_connections_is_unreachable_keeps_saved_url_never_missing(env, respx_mock):
    await seed_rows(entry("A", URL_A), entry("S", URL_B, owned=False))
    await database.add_plex_seen_libraries("A", ["1"])
    await database.add_plex_seen_libraries("S", ["7"])
    await database.set_plex_account_token("acct-token")
    respx_mock.get(USER).mock(return_value=httpx.Response(200, json={"title": "me"}))
    respx_mock.get(RESOURCES).mock(return_value=httpx.Response(200, json=[
        resource("A", connections=[conn(URL_A)]),
        resource("S", owned=False, connections=[]),
    ]))
    respx_mock.get(f"{URL_A}/identity").mock(return_value=httpx.Response(200))
    respx_mock.get(f"{URL_B}/identity").mock(side_effect=httpx.ConnectError("down"))
    mock_sections(respx_mock, URL_A, ("1", "Music"))

    result = await health.run_refresh("daily")

    rows = await rows_by_id()
    assert result.vanished == []
    assert rows["S"]["status"] == "unreachable" and rows["S"]["server_url"] == URL_B
    assert rows["S"]["missing_since"] is None
    assert rows["A"]["status"] == "usable"


async def test_refresh_keeps_saved_shared_url_when_it_still_answers(env, respx_mock):
    pinned = "https://pinned.example.invalid:32400"
    await seed_rows(entry("S", pinned, owned=False))
    await database.add_plex_seen_libraries("S", ["7"])
    await database.set_plex_account_token("acct-token")
    respx_mock.get(USER).mock(return_value=httpx.Response(200, json={"title": "me"}))
    respx_mock.get(RESOURCES).mock(return_value=httpx.Response(200, json=[
        resource("S", owned=False, connections=[conn(URL_B)]),
    ]))
    respx_mock.get(f"{pinned}/identity").mock(return_value=httpx.Response(200))
    respx_mock.get(f"{URL_B}/identity").mock(return_value=httpx.Response(200))
    mock_sections(respx_mock, pinned, ("7", "Friend Music"))

    await health.run_refresh("daily")
    assert (await rows_by_id())["S"]["server_url"] == pinned


# ── KTD9: seen set and baseline ──────────────────────────────────────────────

async def test_baseline_records_without_enabling_then_new_owned_library_enabled(env, respx_mock):
    await seed_rows(entry("A", URL_A))
    await database.mark_plex_seen_baseline_pending(["A"])  # what the U1 migration does
    await database.toggle_library("A:2", "B", True)  # A unticked, B ticked, no seen rows
    route = mock_sections(respx_mock, URL_A, ("1", "A"), ("2", "B"))

    result = await health.reconcile([entry("A", URL_A)], origin="startup")

    assert await database.get_plex_seen_libraries("A") == ["1", "2"]
    assert [r["section_key"] for r in await database.get_enabled_libraries()] == ["A:2"]
    assert result.new_libraries == []
    assert await database.get_setting("plex_rebuild_pending") is None
    env.invalidate.assert_not_called()

    route.mock(return_value=httpx.Response(200, json=sections_json(("1", "A"), ("2", "B"), ("3", "C"))))
    result = await health.reconcile([entry("A", URL_A)], origin="daily")

    assert result.new_libraries == ["A:3"]
    enabled = sorted(r["section_key"] for r in await database.get_enabled_libraries())
    assert enabled == ["A:2", "A:3"]  # C on, A still off
    assert await database.get_plex_seen_libraries("A") == ["1", "2", "3"]
    assert await database.get_setting("plex_rebuild_pending")
    env.invalidate.assert_called_once()


async def test_new_owned_server_first_seen_after_upgrade_enables_all(env, respx_mock):
    await seed_rows(entry("A", URL_A))
    await database.add_plex_seen_libraries("A", ["1"])
    mock_sections(respx_mock, URL_A, ("1", "Music"))
    mock_sections(respx_mock, URL_B, ("4", "Music"), ("5", "Vinyl"),
                  {"key": "6", "title": "Films", "type": "movie"})

    result = await health.reconcile([entry("A", URL_A), entry("N", URL_B)], origin="daily")

    assert result.added == ["N"]
    enabled = sorted(r["section_key"] for r in await database.get_enabled_libraries())
    assert enabled == ["N:4", "N:5"]  # music only
    assert await database.get_plex_seen_libraries("N") == ["4", "5"]
    assert await database.get_setting("plex_rebuild_pending")


async def test_shared_server_new_library_recorded_not_enabled(env, respx_mock):
    await seed_rows(entry("S", URL_B, owned=False))
    await database.add_plex_seen_libraries("S", ["7"])
    mock_sections(respx_mock, URL_B, ("7", "Friend Music"), ("9", "New Friend Music"))

    result = await health.reconcile([entry("S", URL_B, owned=False)], origin="daily")

    assert result.new_libraries == ["S:9"]
    assert await database.get_enabled_libraries() == []
    assert await database.get_plex_seen_libraries("S") == ["7", "9"]
    assert await database.get_setting("plex_rebuild_pending") is None
    env.invalidate.assert_not_called()


# ── KTD10: tombstones ────────────────────────────────────────────────────────

async def test_tombstoned_server_skipped_and_connect_clears_tombstones(env, respx_mock):
    await seed_rows(entry("A", URL_A), entry("R", URL_B))
    await database.add_plex_seen_libraries("A", ["1"])
    await database.delete_plex_server("R")
    assert await database.get_plex_tombstones() == ["R"]
    mock_sections(respx_mock, URL_A, ("1", "Music"))
    mock_sections(respx_mock, URL_B, ("2", "Music"))

    result = await health.reconcile([entry("A", URL_A), entry("R", URL_B)], origin="daily")
    assert result.added == []
    assert "R" not in await rows_by_id()
    assert await database.get_plex_tombstones() == ["R"]

    result = await health.reconcile([entry("A", URL_A), entry("R", URL_B)], origin="connect")
    assert result.added == ["R"]
    assert "R" in await rows_by_id()
    assert await database.get_plex_tombstones() == []


# ── KTD1: connect origin ─────────────────────────────────────────────────────

async def test_connect_origin_added_server_seeds_and_clears_only_its_veto(env, respx_mock):
    await seed_rows(entry("A", URL_A))
    await database.add_plex_seen_libraries("A", ["1"])
    await database.set_disabled_sources(["A", "N"])
    mock_sections(respx_mock, URL_A, ("1", "Music"))
    mock_sections(respx_mock, URL_B, ("4", "Music"))

    result = await health.reconcile([entry("A", URL_A), entry("N", URL_B)], origin="connect")

    assert result.added == ["N"]
    assert sorted(r["section_key"] for r in await database.get_enabled_libraries()) == ["N:4"]
    assert await database.get_disabled_sources() == ["A"]  # A's veto untouched
    # Existing install → deferred rebuild, not an immediate crawl.
    env.catalog.assert_not_called()
    assert await database.get_setting("plex_rebuild_pending")


async def test_connect_origin_fresh_install_crawls_immediately(env, respx_mock):
    mock_sections(respx_mock, URL_A, ("1", "Music"))
    result = await health.reconcile([entry("A", URL_A)], origin="connect")
    assert result.added == ["A"]
    env.catalog.assert_called_once()
    assert await database.get_setting("plex_rebuild_pending") is None


async def test_connect_origin_returned_server_is_not_added(env, respx_mock, monkeypatch):
    from app.catalog import store
    monkeypatch.setattr(store, "delete_catalog_source", AsyncMock())
    await seed_rows(entry("A", URL_A), entry("S", URL_B, owned=False))
    await database.add_plex_seen_libraries("A", ["1"])
    await database.add_plex_seen_libraries("S", ["7"])
    await database.set_disabled_sources(["S"])
    mock_sections(respx_mock, URL_A, ("1", "Music"))
    mock_sections(respx_mock, URL_B, ("7", "Friend Music"))
    await health.reconcile([entry("A", URL_A)], origin="daily")

    result = await health.reconcile([entry("A", URL_A), entry("S", URL_B, owned=False)],
                                    origin="connect")

    assert result.returned == ["S"] and result.added == []
    assert await database.get_enabled_libraries() == []
    assert await database.get_disabled_sources() == ["S"]  # veto untouched


async def test_connect_origin_resets_account_rejected(env):
    health.account_status = "account_rejected"
    health._cooldown_until = env.clock[0] + 9999
    await health.reconcile([], origin="connect")
    assert health.account_status == "ok"
    assert health.status_snapshot()["cooldown_remaining_s"] == 0


async def test_empty_listing_writes_nothing(env):
    await seed_rows(entry("A", URL_A))
    before = await rows_by_id()
    result = await health.reconcile([], origin="daily")
    assert result.vanished == []
    assert await rows_by_id() == before


# ── KTD3: account token bootstrap ────────────────────────────────────────────

async def test_bootstrap_adopts_owned_token_on_200(env, respx_mock):
    await seed_rows(entry("A", URL_A, token="owned-tok"))
    await database.add_plex_seen_libraries("A", ["1"])
    respx_mock.get(USER).mock(return_value=httpx.Response(200, json={"title": "me"}))
    respx_mock.get(RESOURCES).mock(return_value=httpx.Response(200, json=[
        resource("A", token="owned-tok", connections=[conn(URL_A)])]))
    respx_mock.get(f"{URL_A}/identity").mock(return_value=httpx.Response(200))
    mock_sections(respx_mock, URL_A, ("1", "Music"))

    await health.run_refresh("startup")

    assert await database.get_plex_account_token() == "owned-tok"
    assert health.account_status == "ok"
    assert (await rows_by_id())["A"]["status"] == "usable"


async def test_bootstrap_401_sets_unknown_and_never_lists_resources(env, respx_mock):
    await seed_rows(entry("A", URL_A, token="owned-tok"))
    respx_mock.get(USER).mock(return_value=httpx.Response(401))
    res = respx_mock.get(RESOURCES).mock(return_value=httpx.Response(200, json=[]))

    await health.run_refresh("startup")

    assert res.call_count == 0
    assert await database.get_plex_account_token() is None
    assert (await rows_by_id())["A"]["status"] == "unknown"
    assert health.account_status == "missing"


async def test_shared_only_install_without_account_token_is_missing(env, respx_mock):
    await seed_rows(entry("S", URL_B, owned=False))
    res = respx_mock.get(RESOURCES).mock(return_value=httpx.Response(200, json=[]))
    await health.run_refresh("startup")
    assert res.call_count == 0
    assert health.account_status == "missing"
    assert (await rows_by_id())["S"]["status"] == "unknown"


# ── KTD11: legacy install ────────────────────────────────────────────────────

async def test_legacy_plex_config_no_network_status_legacy(env, respx_mock):
    await database.set_plex_config("https://legacy.example.invalid:32400", "legacy-tok", "cid")
    await health.run_refresh("startup")
    assert health.account_status == "legacy"
    assert respx_mock.calls.call_count == 0


# ── generation guard ─────────────────────────────────────────────────────────

async def test_generation_moved_during_probing_discards_result(env, monkeypatch):
    await seed_rows(entry("A", URL_A, token="old"))
    await database.add_plex_seen_libraries("A", ["1"])
    before = await rows_by_id()

    async def moving_list_sections(url, token):
        guest_api._enabled_libs_gen += 1
        return {"status": "usable", "sections": [{"key": "1", "title": "M", "type": "artist"}],
                "status_code": 200}

    monkeypatch.setattr(health.plex_auth, "list_sections", moving_list_sections)
    result = await health.reconcile([entry("A", URL_A, token="new")], origin="daily")
    assert result is None
    assert await rows_by_id() == before


# ── hygiene ──────────────────────────────────────────────────────────────────

async def test_failed_run_logs_class_and_status_only(env, respx_mock, caplog):
    await seed_rows(entry("A", URL_A))
    await database.set_plex_account_token("acct-token-secret")
    respx_mock.get(RESOURCES).mock(return_value=httpx.Response(503))
    with caplog.at_level(logging.INFO, logger="app.plex.health"):
        await health.run_refresh("daily")
    text = caplog.text
    assert "503" in text
    assert "acct-token-secret" not in text
    assert "plex.tv/api" not in text


# ═════════════════════════════════════════════════════════════════════════════
# U3: the 401 hook, the storm short-circuit, and the daily loop
# ═════════════════════════════════════════════════════════════════════════════

from datetime import datetime, timedelta, timezone  # noqa: E402


@pytest.fixture
def hook_env(env, monkeypatch):
    """env + the client hook reset on both sides (install_hooks is module-global)."""
    from app.plex import client as client_mod
    client_mod.set_auth_failed_hook(None)
    yield env
    client_mod.set_auth_failed_hook(None)


async def _drain_bg():
    tasks = list(health._bg_tasks)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# ── on_server_401 ────────────────────────────────────────────────────────────

async def test_fifty_401s_from_one_server_cost_one_status_write_and_one_refresh(hook_env, monkeypatch, caplog):
    await seed_rows(entry("B", URL_B, owned=False))
    writes: list[tuple[str, str]] = []
    real_set = database.set_plex_server_status

    async def counting_set(mid, status):
        writes.append((mid, status))
        await real_set(mid, status)
    monkeypatch.setattr(database, "set_plex_server_status", counting_set)
    req = MagicMock(return_value=True)
    monkeypatch.setattr(health, "request_refresh", req)

    with caplog.at_level(logging.WARNING, logger="app.plex.health"):
        for _ in range(50):
            health.on_server_401("B")
        await _drain_bg()

    assert writes == [("B", "token_rejected")]
    assert (await rows_by_id())["B"]["status"] == "token_rejected"
    req.assert_called_once_with("401")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "Server B" in warnings[0].getMessage()
    assert "tok-B" not in caplog.text


async def test_on_server_401_fires_again_after_the_cooldown(hook_env, monkeypatch):
    await seed_rows(entry("B", URL_B, owned=False))
    req = MagicMock(return_value=True)
    monkeypatch.setattr(health, "request_refresh", req)
    health.on_server_401("B")
    await _drain_bg()
    hook_env.clock[0] += health.FAILURE_COOLDOWN_S + 1
    health.on_server_401("B")
    await _drain_bg()
    assert req.call_count == 2


async def test_on_server_401_is_per_server(hook_env, monkeypatch):
    await seed_rows(entry("A", URL_A), entry("B", URL_B, owned=False))
    req = MagicMock(return_value=True)
    monkeypatch.setattr(health, "request_refresh", req)
    health.on_server_401("A")
    health.on_server_401("B")
    await _drain_bg()
    rows = await rows_by_id()
    assert rows["A"]["status"] == "token_rejected" and rows["B"]["status"] == "token_rejected"
    assert req.call_count == 2   # request_refresh itself applies the 15-minute gate


def test_on_server_401_outside_a_running_loop_is_a_noop():
    health.reset_state()
    health.on_server_401("B")   # must not raise, must not schedule
    assert not health._bg_tasks
    health.reset_state()


async def test_install_hooks_registers_the_client_hook(hook_env):
    from app.plex import client as client_mod
    health.install_hooks()
    assert client_mod._auth_failed_hook is health.on_server_401


async def test_ae1_server_b_401_marks_b_and_a_still_searches(hook_env, respx_mock, monkeypatch):
    """AE1: client receives 401 from server B; hook called once with B; status row
    token_rejected; exactly one refresh scheduled; search over A still answers."""
    from app.plex.client import PlexClient
    from app.plex.auth import PlexAuthError
    from app.sources.plex import PlexSource
    from app.sources.registry import SourceRegistry
    await seed_rows(entry("A", URL_A), entry("B", URL_B, owned=False))
    req = MagicMock(return_value=True)
    monkeypatch.setattr(health, "request_refresh", req)
    health.install_hooks()
    reg = SourceRegistry([PlexSource(PlexClient(URL_A, "tok-A", "cid", machine_id="A")),
                          PlexSource(PlexClient(URL_B, "tok-B", "cid", machine_id="B"))])
    respx_mock.get(f"{URL_A}/hubs/search").mock(return_value=httpx.Response(200, json={
        "MediaContainer": {"Hub": [{"type": "track", "Metadata": [{
            "ratingKey": "7", "title": "Song", "grandparentTitle": "Artist",
            "parentTitle": "Album", "duration": 1000, "librarySectionID": 1,
            "Media": [{"Part": [{"key": "/library/parts/7/1/file.flac"}]}]}]}]}}))
    respx_mock.get(f"{URL_B}/hubs/search").mock(return_value=httpx.Response(401))

    for _ in range(4):
        with pytest.raises(PlexAuthError):
            await reg.search("B:1", "song")
    results = await reg.search("A:1", "song")
    await _drain_bg()

    assert [t.title for t in results.tracks] == ["Song"]
    rows = await rows_by_id()
    assert rows["B"]["status"] == "token_rejected" and rows["A"]["status"] == "usable"
    req.assert_called_once_with("401")


# ── daily loop ───────────────────────────────────────────────────────────────

class _FakeClock:
    """Patched local clock + sleep: sleeping advances the clock, records the
    duration, and yields to the loop so the body can be observed between ticks."""

    def __init__(self, start: datetime):
        self.now = start
        self.sleeps: list[float] = []

    def now_local(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now = self.now + timedelta(seconds=seconds)
        await asyncio.sleep(0)


@pytest.fixture
def daily(hook_env, monkeypatch):
    clock = _FakeClock(datetime(2026, 11, 22, 2, 0, 0))
    monkeypatch.setattr(health, "_now_local", clock.now_local)
    monkeypatch.setattr(health, "_sleep", clock.sleep)
    fired = asyncio.Event()
    calls: list[str] = []

    def fake_request(reason):
        calls.append(reason)
        fired.set()
        return True
    monkeypatch.setattr(health, "request_refresh", fake_request)
    yield SimpleNamespace(clock=clock, calls=calls, fired=fired)


async def _wait_calls(d, n, timeout=2.0):
    async def _until():
        while len(d.calls) < n:
            d.fired.clear()
            await asyncio.wait_for(d.fired.wait(), timeout)
    await asyncio.wait_for(_until(), timeout)


async def test_daily_loop_fires_at_the_configured_local_hour_and_rearms(daily, caplog):
    await database.set_setting("plex_refresh_hour", "6")
    with caplog.at_level(logging.INFO, logger="app.plex.health"):
        task = health.start_daily_loop()
        await _wait_calls(daily, 2)
    await health.shutdown()
    assert daily.calls[:2] == ["daily", "daily"]
    assert daily.clock.sleeps[0] == 4 * 3600        # 02:00 → 06:00 same day
    assert daily.clock.sleeps[1] == 24 * 3600       # then exactly one day
    assert task.cancelled()
    assert any("06:00" in r.getMessage() for r in caplog.records)


async def test_next_refresh_at_is_exposed_for_the_admin_panel(daily):
    await database.set_setting("plex_refresh_hour", "6")
    assert health.next_refresh_at() is None
    health.start_daily_loop()
    await _wait_calls(daily, 1)
    nxt = health.next_refresh_at()
    await health.shutdown()
    assert nxt is not None and nxt.hour == 6
    assert "next_refresh_at" in health.status_snapshot()


@pytest.mark.parametrize("stored", ["25", "abc", "-1", ""])
async def test_daily_loop_invalid_hour_warns_and_fires_at_0400(daily, caplog, stored):
    await database.set_setting("plex_refresh_hour", stored)
    with caplog.at_level(logging.WARNING, logger="app.plex.health"):
        health.start_daily_loop()
        await _wait_calls(daily, 1)
    await health.shutdown()
    assert daily.clock.sleeps[0] == 2 * 3600        # 02:00 → 04:00
    assert any("plex_refresh_hour" in r.getMessage() and r.levelno == logging.WARNING
               for r in caplog.records)


async def test_daily_loop_unset_hour_defaults_to_4_without_warning(daily, caplog):
    with caplog.at_level(logging.WARNING, logger="app.plex.health"):
        health.start_daily_loop()
        await _wait_calls(daily, 1)
    await health.shutdown()
    assert daily.clock.sleeps[0] == 2 * 3600
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


async def test_daily_loop_past_hour_today_means_tomorrow(daily):
    daily.clock.now = datetime(2026, 11, 22, 4, 0, 1)   # one second past 04:00
    health.start_daily_loop()
    await _wait_calls(daily, 1)
    await health.shutdown()
    assert daily.clock.sleeps[0] == 24 * 3600 - 1


async def test_daily_tick_hooks_run_and_a_raising_hook_is_logged_not_fatal(daily, caplog):
    ticks: list[int] = []

    def good():
        ticks.append(1)

    def bad():
        raise RuntimeError("runner bug")
    health.on_daily_tick.append(bad)
    health.on_daily_tick.append(good)
    with caplog.at_level(logging.ERROR, logger="app.plex.health"):
        health.start_daily_loop()
        await _wait_calls(daily, 2)
    await health.shutdown()
    assert len(ticks) >= 2                       # the good hook ran on every tick
    assert daily.calls[:2] == ["daily", "daily"]  # the refresh still fired both days
    assert any("runner bug" in (r.getMessage() + str(r.exc_info)) for r in caplog.records
               if r.levelno == logging.ERROR)


async def test_daily_loop_iteration_exception_is_logged_and_the_loop_fires_again(daily, monkeypatch, caplog):
    attempts = {"n": 0}

    def flaky(reason):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("boom")
        daily.calls.append(reason)
        daily.fired.set()
        return True
    monkeypatch.setattr(health, "request_refresh", flaky)
    with caplog.at_level(logging.ERROR, logger="app.plex.health"):
        health.start_daily_loop()
        await _wait_calls(daily, 1)
    await health.shutdown()
    assert attempts["n"] >= 2
    assert any(r.levelno == logging.ERROR and "RuntimeError" in (r.getMessage() + str(r.exc_info))
               for r in caplog.records)
    # Re-arm after a failure: a short retry pause (not a hot spin), then the
    # next 04:00 — the loop fired again the following day.
    assert health._FAILED_ITERATION_RETRY_S in daily.clock.sleeps
    assert daily.calls == ["daily"] and daily.clock.now.hour == 4


async def test_shutdown_cancels_the_loop_and_owned_runners_within_the_bound(hook_env, monkeypatch):
    import time as _time
    monkeypatch.setattr(health, "_sleep", asyncio.sleep)   # real, long sleep
    loop_task = health.start_daily_loop()
    for _ in range(5):
        await asyncio.sleep(0)

    async def runner():
        await asyncio.Event().wait()
    runner_task = health.own_task(asyncio.create_task(runner()))
    t0 = _time.monotonic()
    await health.shutdown()
    assert _time.monotonic() - t0 < health.SHUTDOWN_GRACE_S
    assert loop_task.cancelled() and runner_task.cancelled()
    assert health._daily_task is None


async def test_daily_loop_reraises_cancelled_error(hook_env, monkeypatch):
    monkeypatch.setattr(health, "_sleep", asyncio.sleep)
    task = health.start_daily_loop()
    for _ in range(5):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    health._daily_task = None


async def test_start_daily_loop_is_idempotent(hook_env, monkeypatch):
    monkeypatch.setattr(health, "_sleep", asyncio.sleep)
    t1 = health.start_daily_loop()
    t2 = health.start_daily_loop()
    assert t1 is t2
    await health.shutdown()


# ── deferred rebuild runner (U4, KTD5) ───────────────────────────────────────
# The pending flag is autonomous-behaviour state: settings-backed so it is
# visible (the admin payload reads it) and survives a restart; the runner clears
# it exactly once — when playback has been idle ten minutes, or at the first
# daily tick after the flag is six hours old.

def _utc(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


@pytest.fixture
async def rebuild(env, monkeypatch):
    """env + a fake runner sleep (advances the idle clock and the wall clock,
    yields), and the state idle-for seam as a plain mutable."""
    from app import state
    idle: dict = {"for": None}
    monkeypatch.setattr(state, "playback_idle_for", lambda: idle["for"])
    wall = [_utc(2026, 11, 22, 3, 50)]
    monkeypatch.setattr(health, "_now_utc", lambda: wall[0])
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if idle["for"] is not None:
            idle["for"] += seconds
        wall[0] = wall[0] + timedelta(seconds=seconds)
        await asyncio.sleep(0)
    monkeypatch.setattr(health, "_sleep", fake_sleep)

    async def set_flag(at=None):
        await database.set_setting(health.PENDING_KEY,
                                   (at or wall[0]).isoformat(timespec="seconds"))

    async def ticks(n, timeout=5.0):
        """Let the runner complete n more check iterations (each ends in one
        fake sleep) or exit, whichever comes first. Each check awaits real
        aiosqlite I/O, so this waits on wall time, not yield counts."""
        target = len(sleeps) + n
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            t = health._rebuild_task
            if len(sleeps) >= target or t is None or t.done():
                return
            await asyncio.sleep(0.005)
        raise AssertionError(f"runner did not reach {target} sleeps (got {len(sleeps)})")
    yield SimpleNamespace(idle=idle, wall=wall, sleeps=sleeps, set_flag=set_flag,
                          ticks=ticks, env=env)
    await health.shutdown()


async def _settle(timeout=5.0):
    """Wait (wall time) for the runner task to finish or until the timeout."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        t = health._rebuild_task
        if t is None or t.done():
            return
        await asyncio.sleep(0.005)


async def test_ae5_flag_waits_for_ten_idle_minutes_then_fires_once_and_clears(rebuild):
    r = rebuild
    r.idle["for"] = None                  # a track is playing
    await r.set_flag()
    task = health.ensure_rebuild_runner()
    await r.ticks(3)
    assert len(r.sleeps) == 3 and all(s == health.REBUILD_CHECK_INTERVAL_S for s in r.sleeps)
    r.env.catalog.assert_not_called()
    r.env.browse.assert_not_called()
    assert await database.get_setting(health.PENDING_KEY) is not None

    r.idle["for"] = 0.0                   # playback stops → stamp set
    await r.ticks(9)                      # 540 s idle: still waiting
    r.env.catalog.assert_not_called()
    await r.ticks(1)                      # 600 s idle → fires
    await _settle()
    r.env.catalog.assert_called_once()
    r.env.browse.assert_called_once()
    assert await database.get_setting(health.PENDING_KEY) is None
    assert task.done() and not task.cancelled()


async def test_runner_never_fires_on_the_idle_path_while_not_idle(rebuild):
    """playback_idle_for() is None whenever a track plays, a paused item exists
    or radio is active (the stamp tests in test_state prove that side); the
    runner treats None as "not idle" indefinitely."""
    r = rebuild
    await r.set_flag()
    r.idle["for"] = None
    health.ensure_rebuild_runner()
    await r.ticks(20)
    assert len(r.sleeps) == 20
    r.env.catalog.assert_not_called()
    assert await database.get_setting(health.PENDING_KEY) is not None


async def test_flag_set_at_0350_the_0400_tick_does_not_fire_but_next_days_does(rebuild):
    r = rebuild
    r.idle["for"] = None                  # the party never ends
    r.wall[0] = _utc(2026, 11, 22, 3, 50)
    await r.set_flag()
    health.ensure_rebuild_runner()
    await r.ticks(10)                     # → 04:00
    health._run_tick_hooks()              # what the daily loop does at the hour
    await r.ticks(2)
    r.env.catalog.assert_not_called()
    r.env.browse.assert_not_called()
    assert await database.get_setting(health.PENDING_KEY) is not None
    r.wall[0] = _utc(2026, 11, 23, 4, 0)  # next day's tick
    health._run_tick_hooks()
    await r.ticks(2)
    await _settle()
    r.env.catalog.assert_called_once()
    r.env.browse.assert_called_once()
    assert await database.get_setting(health.PENDING_KEY) is None


async def test_flag_set_at_2100_the_0400_tick_fires(rebuild):
    r = rebuild
    r.idle["for"] = None
    r.wall[0] = _utc(2026, 11, 21, 21, 0)
    await r.set_flag()
    health.ensure_rebuild_runner()
    await r.ticks(2)
    r.env.catalog.assert_not_called()
    r.wall[0] = _utc(2026, 11, 22, 4, 0)
    health._run_tick_hooks()
    await r.ticks(2)
    await _settle()
    r.env.catalog.assert_called_once()
    r.env.browse.assert_called_once()
    assert await database.get_setting(health.PENDING_KEY) is None


async def test_restart_with_flag_set_starts_the_runner_and_without_it_exits(rebuild):
    r = rebuild
    r.idle["for"] = None
    # No flag: the startup call is harmless — the runner checks once and exits.
    t = health.ensure_rebuild_runner()
    await _settle()
    assert t.done() and not t.cancelled()
    # Flag left in the DB by the previous process: the runner stays alive.
    await r.set_flag()
    t2 = health.ensure_rebuild_runner()
    await r.ticks(3)
    assert not t2.done() and health._rebuild_task is t2


async def test_two_flag_writes_start_one_runner(rebuild, respx_mock, monkeypatch):
    """The U2 reconcile path writes the flag and starts the runner; a later
    reconcile that would write it again neither rewrites it nor starts a second."""
    from app.catalog import store
    monkeypatch.setattr(store, "delete_catalog_source", AsyncMock())
    r = rebuild
    r.idle["for"] = None
    await seed_rows(entry("A", URL_A), entry("S", URL_B, owned=False))
    await database.add_plex_seen_libraries("A", ["1"])
    await database.add_plex_seen_libraries("S", ["7"])
    mock_sections(respx_mock, URL_A, ("1", "Music"))
    mock_sections(respx_mock, URL_B, ("7", "Friend Music"))

    await health.reconcile([entry("A", URL_A)], origin="daily")            # S vanishes
    res1 = await health.reconcile([entry("A", URL_A), entry("S", URL_B, owned=False)],
                                  origin="daily")                          # S returns
    assert res1.returned == ["S"]
    t1 = health._rebuild_task
    assert t1 is not None and not t1.done()
    flag1 = await database.get_setting(health.PENDING_KEY)
    assert flag1

    await health.reconcile([entry("A", URL_A)], origin="daily")            # vanishes again
    res2 = await health.reconcile([entry("A", URL_A), entry("S", URL_B, owned=False)],
                                  origin="daily")                          # returns again
    assert res2.returned == ["S"]
    assert health._rebuild_task is t1 and not t1.done()
    assert await database.get_setting(health.PENDING_KEY) == flag1
    assert health.ensure_rebuild_runner() is t1


async def test_shutdown_cancels_a_waiting_rebuild_runner_within_the_bound(rebuild, monkeypatch):
    import time as _time
    r = rebuild
    r.idle["for"] = None
    await r.set_flag()
    monkeypatch.setattr(health, "_sleep", asyncio.sleep)    # a real 60 s sleep
    t = health.ensure_rebuild_runner()
    await _settle()
    assert not t.done()
    t0 = _time.monotonic()
    await health.shutdown()
    assert _time.monotonic() - t0 < health.SHUTDOWN_GRACE_S
    assert t.cancelled()
    assert health._rebuild_task is None
    # A cancel never clears the flag: the next boot picks it up.
    assert await database.get_setting(health.PENDING_KEY) is not None


async def test_install_hooks_registers_the_rebuild_tick_hook_once(hook_env):
    health.install_hooks()
    health.install_hooks()
    assert health.on_daily_tick.count(health._rebuild_tick_hook) == 1


# ── U6: admin-facing projection + re-arm of the sweep hour ───────────────────

async def _until(pred, timeout=2.0):
    async def _w():
        while not pred():
            await asyncio.sleep(0.01)
    await asyncio.wait_for(_w(), timeout)


async def test_projected_next_refresh_at_follows_the_setting(daily):
    """The Sources panel's 'Next sweep' reads the CURRENT setting, so it moves
    the moment the admin saves a new hour — not at the loop's next wake."""
    assert (await health.projected_next_refresh_at()).hour == 4     # unset → default
    await database.set_setting("plex_refresh_hour", "6")
    nxt = await health.projected_next_refresh_at()
    assert nxt == datetime(2026, 11, 22, 6, 0)                       # clock is 02:00
    await database.set_setting("plex_refresh_hour", "1")
    assert (await health.projected_next_refresh_at()) == datetime(2026, 11, 23, 1, 0)
    assert await health.configured_refresh_hour() == 1


async def test_reschedule_daily_loop_rearms_on_the_new_hour(daily):
    """A sleeping loop targets the hour it read on its last wake; saving a new
    hour must re-arm it so today's sweep lands on the new hour, not tomorrow's."""
    await database.set_setting("plex_refresh_hour", "6")
    first = health.start_daily_loop()
    await _until(lambda: health.next_refresh_at() is not None)
    assert health.next_refresh_at().hour == 6
    await database.set_setting("plex_refresh_hour", "10")
    health.reschedule_daily_loop()
    await _until(lambda: first.cancelled()
                 and health.next_refresh_at() is not None
                 and health.next_refresh_at().hour == 10)
    await health.shutdown()


async def test_reschedule_daily_loop_is_a_noop_without_a_loop(daily):
    health.reschedule_daily_loop()              # nothing running: nothing started
    assert health.next_refresh_at() is None


# ── 2026-11-22 code review follow-ups ────────────────────────────────────────

async def test_rebuild_waits_while_a_crawl_is_in_flight_then_fires_once(rebuild, monkeypatch):
    """Review #1: trigger_catalog_refresh is single-flighted — consuming the flag
    while a crawl runs would lose the rebuild. The runner keeps the flag and
    re-checks next minute."""
    from app import state
    r = rebuild
    building = [True]
    monkeypatch.setattr(state, "catalog_building", lambda: building[0])
    r.idle["for"] = 600.0
    await r.set_flag()
    task = health.ensure_rebuild_runner()
    await r.ticks(3)
    r.env.catalog.assert_not_called()
    assert await database.get_setting(health.PENDING_KEY) is not None
    assert not task.done()

    building[0] = False
    await r.ticks(1)
    await _settle()
    r.env.catalog.assert_called_once()
    r.env.browse.assert_called_once()
    assert await database.get_setting(health.PENDING_KEY) is None
    assert task.done() and not task.cancelled()


async def test_vanish_during_an_in_flight_crawl_owes_a_deferred_rebuild(env, respx_mock, monkeypatch):
    """Review #2 (health half): the crawl captured the registry before the
    vanish and will write the dead source back after the prefix delete, so a
    rebuild without it is scheduled. With no crawl running none is (KTD6)."""
    from app import state
    from app.catalog import store
    monkeypatch.setattr(store, "delete_catalog_source", AsyncMock())
    monkeypatch.setattr(state, "catalog_building", lambda: True)
    await seed_rows(entry("A", URL_A), entry("S", URL_B, owned=False))
    await database.add_plex_seen_libraries("A", ["1"])
    await database.add_plex_seen_libraries("S", ["7"])
    mock_sections(respx_mock, URL_A, ("1", "Music"))

    result = await health.reconcile([entry("A", URL_A)], origin="daily")

    assert result.vanished == ["S"]
    assert await database.get_setting(health.PENDING_KEY)
    assert health._rebuild_task is not None and not health._rebuild_task.done()
    await health.shutdown()


async def test_refresh_queued_behind_a_connect_that_bumps_the_generation_still_runs(env, respx_mock):
    """Review #5: the generation is read inside the lock, so a bump made by the
    reconcile this run waited behind does not discard it."""
    await seed_rows(entry("A", URL_A))
    await database.add_plex_seen_libraries("A", ["1"])
    await database.set_plex_account_token("acct-token")
    respx_mock.get(RESOURCES).mock(return_value=httpx.Response(200, json=[
        resource("A", connections=[conn(URL_A)])]))
    respx_mock.get(f"{URL_A}/identity").mock(return_value=httpx.Response(200))
    mock_sections(respx_mock, URL_A, ("1", "Music"))

    lock = health._get_lock()
    await lock.acquire()
    task = asyncio.create_task(health.run_refresh("daily"))
    await asyncio.sleep(0.01)
    assert not task.done()
    guest_api._enabled_libs_gen += 1          # what a connect reconcile does under the lock
    lock.release()
    result = await task

    assert result is not None and health._last_outcome == "ok"
    assert (await rows_by_id())["A"]["status"] == "usable"


async def test_discarded_refresh_is_re_requested_once(env, respx_mock, monkeypatch):
    """Review #5: a generation-discarded run sets no cooldown, so it re-requests
    itself once after DISCARD_RETRY_S; a second discard in a row does not."""
    await seed_rows(entry("A", URL_A))
    await database.add_plex_seen_libraries("A", ["1"])
    await database.set_plex_account_token("acct-token")
    respx_mock.get(RESOURCES).mock(return_value=httpx.Response(200, json=[
        resource("A", connections=[conn(URL_A)])]))
    respx_mock.get(f"{URL_A}/identity").mock(return_value=httpx.Response(200))

    async def moving_list_sections(url, token):
        guest_api._enabled_libs_gen += 1
        return {"status": "usable", "sections": [{"key": "1", "title": "M", "type": "artist"}],
                "status_code": 200}
    monkeypatch.setattr(health.plex_auth, "list_sections", moving_list_sections)
    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)
        await asyncio.sleep(0)
    monkeypatch.setattr(health, "_sleep", fake_sleep)
    rerequest = MagicMock(return_value=True)
    monkeypatch.setattr(health, "request_refresh", rerequest)

    assert await health.run_refresh("daily") is None
    assert health._last_outcome == "discarded"
    assert health.status_snapshot()["cooldown_remaining_s"] == 0
    await _until(lambda: rerequest.call_count == 1)
    assert slept == [health.DISCARD_RETRY_S]
    rerequest.assert_called_once_with("daily")

    assert await health.run_refresh("daily") is None       # discarded again
    await asyncio.sleep(0.02)
    assert rerequest.call_count == 1                        # no second retry
    await health.shutdown()


@pytest.mark.parametrize("failure", ["503", "connect_error"])
async def test_bootstrap_no_verdict_keeps_rows_and_account_status(env, respx_mock, failure):
    """Review #6: verify_account_token's 'error' (5xx / 429 / connection) is
    not a missing token — rows and account_status are untouched, a cooldown is
    set, plex.tv resources are never listed."""
    await seed_rows(entry("A", URL_A, token="owned-tok"))
    if failure == "503":
        respx_mock.get(USER).mock(return_value=httpx.Response(503))
    else:
        respx_mock.get(USER).mock(side_effect=httpx.ConnectError("down"))
    res = respx_mock.get(RESOURCES).mock(return_value=httpx.Response(200, json=[]))

    assert await health.run_refresh("startup") is None

    assert res.call_count == 0
    assert (await rows_by_id())["A"]["status"] == "usable"
    assert health.account_status == "ok"
    assert health._last_outcome == "plex_tv_unreachable"
    assert health.status_snapshot()["cooldown_remaining_s"] == health.FAILURE_COOLDOWN_S
    assert await database.get_plex_account_token() is None


async def test_seeding_failure_still_invalidates_and_sets_the_pending_flag(env, respx_mock, monkeypatch):
    """Review #7: a raise in a post-commit step must not strand the registry
    on the old table or lose the rebuild."""
    await seed_rows(entry("A", URL_A))
    await database.add_plex_seen_libraries("A", ["1"])
    mock_sections(respx_mock, URL_A, ("1", "Music"))
    mock_sections(respx_mock, URL_B, ("4", "Music"))
    monkeypatch.setattr(database, "toggle_library", AsyncMock(side_effect=RuntimeError("disk")))

    result = await health.reconcile([entry("A", URL_A), entry("N", URL_B)], origin="daily")

    assert result is not None and result.added == ["N"]
    assert (await rows_by_id())["N"]["status"] == "usable"
    env.invalidate.assert_called_once()
    assert await database.get_setting(health.PENDING_KEY)
    await health.shutdown()


async def test_failed_catalog_purge_is_retried_on_the_next_sweep(env, respx_mock, monkeypatch):
    """Review #8: a vanished server whose purge raised still holds catalog rows;
    the next sweep purges every no_longer_shared server with holds."""
    from app.catalog import store
    await seed_rows(entry("A", URL_A), entry("S", URL_B, owned=False))
    await database.add_plex_seen_libraries("A", ["1"])
    await database.add_plex_seen_libraries("S", ["7"])
    await store.replace_catalog(
        artists=[], albums=[],
        tracks=[{"identity": "S:t1", "title": "Song", "title_base": "song", "artist": "Act",
                 "artist_base_key": "act", "album_identity": "S:al1", "duration_ms": 1000}],
        holds=[{"entity_type": "track", "identity": "S:t1", "source_id": "S",
                "provider_local_key": "t1", "priority": 0, "server_name": None}])
    deleted = AsyncMock(side_effect=[RuntimeError("locked"), None])
    monkeypatch.setattr(store, "delete_catalog_source", deleted)
    mock_sections(respx_mock, URL_A, ("1", "Music"))

    result = await health.reconcile([entry("A", URL_A)], origin="daily")
    assert result.vanished == ["S"]
    deleted.assert_awaited_once_with("S")
    assert await database.get_setting(health.PENDING_KEY)   # a rebuild is owed

    result = await health.reconcile([entry("A", URL_A)], origin="daily")
    assert result.vanished == []
    assert deleted.await_count == 2 and deleted.await_args.args == ("S",)
    assert env.browse.call_count == 2
    await health.shutdown()


async def test_new_owned_server_unreachable_at_first_sight_is_enabled_when_it_answers(env, respx_mock):
    """Review #9: a server plex.tv lists for the first time while it is down is
    in ``added`` only once; when it answers later it must still be treated as
    new (owned libraries on), not baselined like a pre-upgrade row."""
    await seed_rows(entry("A", URL_A))
    await database.add_plex_seen_libraries("A", ["1"])
    mock_sections(respx_mock, URL_A, ("1", "Music"))
    mock_sections(respx_mock, URL_B, ("4", "Music"), ("5", "Vinyl"))

    first = await health.reconcile([entry("A", URL_A), entry("N", URL_B, probe="unreachable")],
                                   origin="daily")
    assert first.added == ["N"]
    assert (await rows_by_id())["N"]["status"] == "unreachable"
    assert await database.get_enabled_libraries() == []

    second = await health.reconcile([entry("A", URL_A), entry("N", URL_B)], origin="daily")
    assert second.added == []
    enabled = sorted(r["section_key"] for r in await database.get_enabled_libraries())
    assert enabled == ["N:4", "N:5"]
    assert await database.get_plex_seen_libraries("N") == ["4", "5"]
    assert await database.get_setting(health.PENDING_KEY)
    await health.shutdown()


async def test_pre_upgrade_server_unreachable_at_first_sweep_still_baselines(env, respx_mock):
    """The other side of review #9: the migration's baseline marker survives
    an unreachable first sweep, so the admin's unticked libraries stay off."""
    await seed_rows(entry("A", URL_A))
    await database.mark_plex_seen_baseline_pending(["A"])
    await database.toggle_library("A:2", "B", True)
    mock_sections(respx_mock, URL_A, ("1", "A"), ("2", "B"))

    await health.reconcile([entry("A", URL_A, probe="unreachable")], origin="startup")
    assert await database.plex_seen_baseline_pending("A")

    result = await health.reconcile([entry("A", URL_A)], origin="daily")
    assert result.new_libraries == []
    assert [r["section_key"] for r in await database.get_enabled_libraries()] == ["A:2"]
    assert await database.get_plex_seen_libraries("A") == ["1", "2"]
    assert not await database.plex_seen_baseline_pending("A")
    assert await database.get_setting(health.PENDING_KEY) is None
