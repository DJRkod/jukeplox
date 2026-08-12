"""U6 — Snapcast zoning: topology guard (embedded full / external read+assign),
echo-guarded live-update hook, master/group fan-out, and the admin zone
endpoints' auth + topology 403.
"""

import asyncio

import pytest
from unittest.mock import AsyncMock

from app import state
from app.output import snapcast as snap_mod
from app.output.snapcast import SnapcastBackend


class FakeClient:
    def __init__(self, cid, name, volume=50):
        self.identifier, self.friendly_name, self.volume, self.muted = cid, name, volume, False


class FakeGroup:
    def __init__(self, gid, clients):
        self.identifier, self.clients, self.muted = gid, clients, False


class FakeControl:
    def __init__(self):
        self._groups = [FakeGroup("g1", [FakeClient("c1", "Kitchen", 40),
                                         FakeClient("c2", "Living", 80)])]
        self.calls = []

    async def status(self):
        return {}

    def groups(self):
        return list(self._groups)

    def clients(self):
        return [c for g in self._groups for c in g.clients]

    def client(self, cid):
        return next(c for c in self.clients() if c.identifier == cid)

    def group(self, gid):
        return next(g for g in self._groups if g.identifier == gid)

    async def client_set_volume(self, cid, pct):
        self.calls.append(("cvol", cid, pct))

    async def client_set_muted(self, cid, m):
        self.calls.append(("cmute", cid, m))

    async def group_set_muted(self, gid, m):
        self.calls.append(("gmute", gid, m))

    async def group_set_clients(self, gid, cids):
        self.calls.append(("gclients", gid, list(cids)))

    async def group_set_name(self, gid, name):
        self.calls.append(("gname", gid, name))

    def set_on_update(self, cb):
        pass

    async def disconnect(self):
        pass


async def _make(monkeypatch, mode="embedded"):
    settings = {"snapcast_mode": mode, "snapcast_external_host": "8.8.8.8",
                "snapcast_external_feed_url": "tcp://8.8.8.8:4953"}

    async def _get(key, default=None):
        return settings.get(key, default)

    monkeypatch.setattr("app.database.get_setting", _get)
    ctrl = FakeControl()
    b = SnapcastBackend(control_factory=AsyncMock(return_value=ctrl),
                        supervisor_factory=lambda: _FakeSup())
    await b.enable()
    b._ctrl = ctrl
    return b


class _FakeSup:
    control_host, control_port = "127.0.0.1", 1705
    source_feed_url = "tcp://127.0.0.1:4953"
    is_running = True

    async def start(self): ...
    async def stop(self): ...


# ── topology guard (AE3) ──────────────────────────────────────────────────────


async def test_embedded_can_rename_and_dissolve(monkeypatch):
    b = await _make(monkeypatch, "embedded")
    assert b.can_manage_topology() is True
    await b.rename_group("g1", "Downstairs")
    await b.dissolve_group("g1")
    kinds = [c[0] for c in b._ctrl.calls]
    assert "gname" in kinds and ("gclients", "g1", []) in b._ctrl.calls


async def test_external_topology_denied_but_assign_allowed(monkeypatch):
    b = await _make(monkeypatch, "external")
    assert b.can_manage_topology() is False
    with pytest.raises(PermissionError):
        await b.rename_group("g1", "X")
    with pytest.raises(PermissionError):
        await b.dissolve_group("g1")
    # assign-between-existing is still allowed on external (non-destructive).
    await b.assign_client_to_group("c2", "g1")
    assert any(c[0] == "gclients" for c in b._ctrl.calls)


# ── master / group fan-out (AE5) ──────────────────────────────────────────────


async def test_group_volume_fans_out_proportionally(monkeypatch):
    b = await _make(monkeypatch, "embedded")
    await b.set_group_volume("g1", 0.6)
    cvol = [c for c in b._ctrl.calls if c[0] == "cvol"]
    assert {c[1] for c in cvol} == {"c1", "c2"}


# ── echo-guarded live-update hook ─────────────────────────────────────────────


async def test_update_hook_fires_when_not_echo(monkeypatch):
    b = await _make(monkeypatch, "embedded")
    fired = []
    b.set_zones_changed_hook(lambda: fired.append(1))
    b._on_control_update()
    assert fired == [1]


async def test_update_hook_suppressed_during_echo_window(monkeypatch):
    b = await _make(monkeypatch, "embedded")
    fired = []
    b.set_zones_changed_hook(lambda: fired.append(1))
    b._stamp_volume_write()          # our own write → echo window active
    b._on_control_update()
    assert fired == []               # suppressed (no slider snap-back)


# ── admin endpoints: auth + topology 403 ──────────────────────────────────────


def test_zones_404_when_disabled(client, monkeypatch):
    monkeypatch.setattr(state, "get_server_fed_backend", lambda t: None)
    r = client.get("/admin/output/snapcast/zones")
    assert r.status_code == 404


def test_zones_returns_tree(client, monkeypatch):
    class _B:
        _connected = True
        def is_connected(self): return True
        def can_manage_topology(self): return True
        async def list_zones(self): return [{"group_id": "g1", "clients": []}]
    monkeypatch.setattr(state, "get_server_fed_backend", lambda t: _B())
    r = client.get("/admin/output/snapcast/zones")
    assert r.status_code == 200
    assert r.json()["zones"][0]["group_id"] == "g1"
    assert r.json()["can_manage_topology"] is True


def test_group_rename_403_on_external(client, monkeypatch):
    class _B:
        _connected = True
        def is_connected(self): return True
        def can_manage_topology(self): return False
        async def rename_group(self, gid, name):
            raise PermissionError("external server — read+assign only")
    monkeypatch.setattr(state, "get_server_fed_backend", lambda t: _B())
    r = client.post("/admin/output/snapcast/group/g1/rename", json={"name": "X"})
    assert r.status_code == 403


def test_group_assign_ok(client, monkeypatch):
    calls = []
    class _B:
        _connected = True
        def is_connected(self): return True
        def can_manage_topology(self): return True
        async def assign_client_to_group(self, cid, gid): calls.append((cid, gid))
    monkeypatch.setattr(state, "get_server_fed_backend", lambda t: _B())
    r = client.post("/admin/output/snapcast/group/g1/assign", json={"client_id": "c2"})
    assert r.status_code == 200
    assert calls == [("c2", "g1")]


def test_zone_endpoints_require_admin(anon_client):
    # Router-level require_admin (AE7): an unauthenticated caller is rejected.
    r = anon_client.get("/admin/output/snapcast/zones")
    assert r.status_code in (401, 403)
