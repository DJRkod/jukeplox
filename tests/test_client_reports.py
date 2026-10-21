"""Server-side guards for guest experience reporting (client-experience rig U10).

The browser half is guarded in `tests/test_clientrig_guest_reporting.py`, which
drives a real engine and enforces the egress guarantee. This file covers what
happens once a batch arrives: the default, the bound, the stripping, and the
retention.

Three of these exist because the plan's own review found them missing, and each
is the kind of thing that is cheap now and awkward later:

  * the feature is OFF unless an operator says otherwise, and a failed read of
    the setting also means off -- the safe direction for something that observes
    guests;
  * the endpoint is a new UNAUTHENTICATED write on a LAN-reachable app, so it
    carries the per-IP rate limit this repo already applies to two other
    guest-facing routes, plus a server-side batch cap;
  * records are pruned to a row cap on write, because an unattended installation
    should not accumulate guest data across parties indefinitely.
"""
import pytest

from app import database
from app.api import guest as guest_api
from app.config import Settings


@pytest.fixture
def tmp_settings(tmp_path, monkeypatch):
    s = Settings(data_dir=tmp_path, secret_key="test", session_ttl_hours=8)
    monkeypatch.setattr(database, "settings", s)
    return s


@pytest.fixture
async def db(tmp_settings):
    # close_db() is mandatory: aiosqlite runs on a non-daemon thread, so an open
    # connection hangs the interpreter at exit, after the tests have passed.
    await database.init_db()
    try:
        yield tmp_settings
    finally:
        await database.close_db()


def rec(kind="interaction", **kw):
    base = {"view": "browse:albums", "engine": "chromium", "engineMajor": 155,
            "band": "sm", "dpr": 2.4, "minute": "2026-10-19T14:05",
            "kind": kind, "caps": {"event": True, "loaf": True}}
    base.update(kw)
    return base


# ── off by default, and off in the failure direction ───────────────────────

async def test_reporting_is_off_when_the_setting_was_never_written(db):
    assert await guest_api._client_reporting_enabled() is False


async def test_reporting_is_on_only_for_an_explicit_truthy_value(db):
    for raw, expected in (("1", True), ("true", True), ("on", True),
                          ("0", False), ("", False), ("no", False)):
        await database.set_setting(guest_api.CLIENT_REPORT_SETTING, raw)
        assert await guest_api._client_reporting_enabled() is expected, raw


# ── the rate limit ─────────────────────────────────────────────────────────

# Addresses below are from the RFC 5737 documentation ranges, which exist for
# exactly this and can never be a real host -- so a committed fixture can never
# be mistaken for, or collide with, something on anyone's network.

def test_a_normal_client_is_not_limited(monkeypatch):
    monkeypatch.setattr(guest_api, "_client_report_hits", {})
    for _ in range(guest_api._CLIENT_REPORT_MAX - 1):
        guest_api._check_client_report_rate_limit("192.0.2.1")  # no raise


def test_a_flooding_client_is_refused(monkeypatch):
    from fastapi import HTTPException
    monkeypatch.setattr(guest_api, "_client_report_hits", {})
    for _ in range(guest_api._CLIENT_REPORT_MAX):
        guest_api._check_client_report_rate_limit("192.0.2.1")
    with pytest.raises(HTTPException) as exc:
        guest_api._check_client_report_rate_limit("192.0.2.1")
    assert exc.value.status_code == 429


def test_one_flooding_client_does_not_lock_out_another(monkeypatch):
    """A single misbehaving phone at a party must not silence everyone else."""
    monkeypatch.setattr(guest_api, "_client_report_hits", {})
    for _ in range(guest_api._CLIENT_REPORT_MAX):
        guest_api._check_client_report_rate_limit("192.0.2.1")
    guest_api._check_client_report_rate_limit("192.0.2.2")  # no raise


def test_the_hit_map_does_not_grow_without_bound(monkeypatch):
    """A churn of one-shot addresses must not accumulate keys forever."""
    import time as _t
    monkeypatch.setattr(guest_api, "_client_report_hits", {})
    base = _t.monotonic()
    # Seed stale entries whose whole window has aged out.
    for i in range(50):
        guest_api._client_report_hits[f"198.51.100.{i}"] = [
            base - guest_api._CLIENT_REPORT_WINDOW_S - 10]
    guest_api._check_client_report_rate_limit("203.0.113.1")
    assert len(guest_api._client_report_hits) == 1, (
        "stale per-IP windows were not dropped")


# ── storage, stripping and retention ───────────────────────────────────────

async def test_a_batch_is_stored_and_read_back_verbatim(db):
    stored = await database.add_client_reports([rec(), rec(kind="restore", ok=True)])
    assert stored == 2
    rows = await database.get_client_reports()
    assert len(rows) == 2
    assert rows[0]["payload"]["view"] == "browse:albums"


async def test_records_are_returned_newest_first(db):
    await database.add_client_reports([rec(view="first")])
    await database.add_client_reports([rec(view="second")])
    rows = await database.get_client_reports()
    assert rows[0]["payload"]["view"] == "second"


async def test_storage_is_pruned_to_the_row_cap_on_write(db, monkeypatch):
    """Retention is applied on write rather than on a timer, because a timer is
    a thing that can silently stop."""
    monkeypatch.setattr(database, "CLIENT_REPORT_ROW_CAP", 10)
    for _ in range(4):
        await database.add_client_reports([rec() for _ in range(5)])
    assert await database.count_client_reports() <= 10


async def test_the_operator_can_erase_everything(db):
    await database.add_client_reports([rec() for _ in range(5)])
    deleted = await database.clear_client_reports()
    assert deleted == 5
    assert await database.count_client_reports() == 0


async def test_an_empty_batch_stores_nothing_and_does_not_error(db):
    assert await database.add_client_reports([]) == 0
    assert await database.count_client_reports() == 0


async def test_an_oversized_payload_is_truncated_rather_than_rejected(db):
    """A client sending something enormous should not be able to fill the disk,
    and should also not lose the whole batch."""
    big = rec(detail="x" * 20000)
    assert await database.add_client_reports([big]) == 1
    rows = await database.get_client_reports()
    assert rows
    payload = rows[0]["payload"]
    # Still readable. Slicing the serialised JSON would have produced something
    # unparseable, and the operator-facing view would show a placeholder rather
    # than the record -- worse than storing less of it.
    assert "unparseable" not in payload, payload
    assert payload.get("_truncated") is True
    assert payload.get("kind") == "interaction"


# ── Reachability: the tests above proved the logic, not the URL ───────────────
#
# Everything above this line calls the handlers and the database layer
# directly. All twelve passed while the two admin routes were registered at
# `/admin/admin/client-reports` -- the decorators repeated a prefix the router
# already carries, so the documented path 404'd and nothing could reach them.
# It took deploying to the rig to notice.
#
# Proving a handler's logic is not proving a handler is reachable. These two
# tests close that gap, and the structural one closes it for every route in
# the app rather than only for this feature.

def test_admin_client_report_routes_are_reachable_at_their_real_paths(
        client, mock_session):
    """The path an operator would actually request.

    The database is stubbed because what is under test is routing, not
    storage -- storage is covered above. A 404 here is the defect; a 200
    means the request reached the handler.
    """
    from unittest.mock import AsyncMock, patch

    with patch.object(database, "get_setting", AsyncMock(return_value="0")),          patch.object(database, "count_client_reports", AsyncMock(return_value=0)),          patch.object(database, "get_client_reports", AsyncMock(return_value=[])),          patch.object(database, "clear_client_reports", AsyncMock(return_value=0)):
        got = client.get("/admin/client-reports")
        assert got.status_code == 200, got.text
        assert got.json()["row_cap"] == database.CLIENT_REPORT_ROW_CAP
        assert client.delete("/admin/client-reports").status_code == 200


def test_admin_client_report_routes_require_auth(anon_client, mock_session):
    assert anon_client.get("/admin/client-reports").status_code == 401
    assert anon_client.delete("/admin/client-reports").status_code == 401


def test_no_registered_route_repeats_a_path_segment():
    """A decorator that restates the router's own prefix yields `/admin/admin/
    ...`, which answers 404 at the path everyone writes down.

    FastAPI will not warn: the doubled path is a perfectly valid route, just
    not the one anybody requests. This sweeps every router in the app, so the
    next person to paste a decorator into a prefixed router is caught by a
    failing test rather than by a deployment.
    """
    from app.main import app

    doubled = []
    for route in app.routes:
        path = getattr(route, "path", "") or ""
        parts = [p for p in path.split("/") if p]
        for a, b in zip(parts, parts[1:]):
            # Literal repeats only. `/x/{id}/{id}` is a different mistake and
            # a path parameter legitimately repeats a literal in some designs.
            if a == b and not a.startswith("{"):
                doubled.append(path)
                break
    assert doubled == [], (
        f"these routes repeat a path segment, which usually means the "
        f"decorator restated the router's prefix: {doubled}")
