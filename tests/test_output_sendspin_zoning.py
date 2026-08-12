"""U8 — Sendspin pairing + zoning: PIN-preferred pairing, sealed PSK rotation,
per-client/group volume, and the admin endpoints' auth + rotate-confirm guard.
"""

import pytest

from app import database, state
from app.config import Settings
from app.output.sendspin import SendspinBackend


class FakeAdapter:
    def __init__(self):
        self.pairing_key = "LONGTERM-PSK-abc"
        self.rotated = False
        self._clients = [{"id": "c1", "name": "Kitchen", "volume": 0.4, "muted": False}]
        self.calls = []

    async def initiate_pairing(self):
        return "PIN-1234"  # short-lived

    async def rotate_pairing(self):
        self.rotated = True
        self.pairing_key = "LONGTERM-PSK-xyz"
        return self.pairing_key

    def clients(self):
        return [dict(c) for c in self._clients]

    async def set_client_volume(self, cid, level):
        self.calls.append(("cvol", cid, level))

    async def set_client_mute(self, cid, muted):
        self.calls.append(("cmute", cid, muted))


def _backend_with_adapter():
    b = SendspinBackend()
    b._adapter = FakeAdapter()
    b._connected = True
    return b


# ── backend-level pairing ─────────────────────────────────────────────────────


async def test_get_pairing_pin_is_short_lived(monkeypatch):
    b = _backend_with_adapter()
    assert await b.get_pairing_pin() == "PIN-1234"


def test_pairing_key_is_the_longterm_psk():
    b = _backend_with_adapter()
    assert b.pairing_key() == "LONGTERM-PSK-abc"


# ── sealed PSK rotation (temp DB) ─────────────────────────────────────────────


@pytest.fixture
def tmp_settings(tmp_path, monkeypatch):
    s = Settings(data_dir=tmp_path, secret_key="test")
    monkeypatch.setattr(database, "settings", s)
    return s


@pytest.fixture
async def db(tmp_settings):
    await database.init_db()
    return tmp_settings


async def test_rotate_persists_new_psk_sealed(db):
    b = _backend_with_adapter()
    await b.rotate_pairing()
    assert b._adapter.rotated
    raw = await database.get_setting("sendspin_pairing_psk")
    assert raw.startswith("enc:fernet:")            # sealed at rest
    assert "LONGTERM-PSK-xyz" not in raw
    assert await database.get_sealed_setting("sendspin_pairing_psk") == "LONGTERM-PSK-xyz"


# ── zoning fan-out ────────────────────────────────────────────────────────────


async def test_group_volume_fans_out(monkeypatch):
    b = _backend_with_adapter()
    b._adapter._clients = [{"id": "c1", "name": "K", "volume": 0.4, "muted": False},
                           {"id": "c2", "name": "L", "volume": 0.8, "muted": False}]
    await b.set_group_volume("sendspin", 0.6)
    cvol = [c for c in b._adapter.calls if c[0] == "cvol"]
    assert {c[1] for c in cvol} == {"c1", "c2"}


# ── admin endpoints ───────────────────────────────────────────────────────────


class _EndpointBackend:
    _connected = True

    def is_connected(self):
        return True

    async def get_pairing_pin(self):
        return "PIN-9999"

    async def rotate_pairing(self):
        self.rotated = True

    async def list_zones(self):
        return [{"group_id": "sendspin", "clients": []}]

    async def set_client_volume(self, cid, level):
        self.vol = (cid, level)


def test_pairing_pin_endpoint(client, monkeypatch):
    monkeypatch.setattr(state, "get_server_fed_backend", lambda t: _EndpointBackend())
    r = client.get("/admin/output/sendspin/pairing")
    assert r.status_code == 200 and r.json()["pin"] == "PIN-9999"


def test_rotate_requires_confirm(client, monkeypatch):
    be = _EndpointBackend()
    monkeypatch.setattr(state, "get_server_fed_backend", lambda t: be)
    r0 = client.post("/admin/output/sendspin/pairing/rotate", json={"confirm": False})
    assert r0.status_code == 400  # no confirm → refused
    r1 = client.post("/admin/output/sendspin/pairing/rotate", json={"confirm": True})
    assert r1.status_code == 200 and getattr(be, "rotated", False)


def test_sendspin_zones_endpoint(client, monkeypatch):
    monkeypatch.setattr(state, "get_server_fed_backend", lambda t: _EndpointBackend())
    r = client.get("/admin/output/sendspin/zones")
    assert r.status_code == 200 and r.json()["experimental"] is True


def test_sendspin_404_when_disabled(client, monkeypatch):
    monkeypatch.setattr(state, "get_server_fed_backend", lambda t: None)
    assert client.get("/admin/output/sendspin/zones").status_code == 404


def test_sendspin_endpoints_require_admin(anon_client):
    assert anon_client.get("/admin/output/sendspin/zones").status_code in (401, 403)
