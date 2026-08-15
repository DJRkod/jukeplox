"""U8 — Sendspin pairing + zoning: PIN-preferred pairing, sealed PSK rotation,
per-client/group volume, and the admin endpoints' auth + rotate-confirm guard.
"""

import asyncio

import pytest

from app import database, state
from app.config import Settings
from app.output.base import DeviceNotReadyError
from app.output.sendspin import SendspinBackend


class FakeAdapter:
    def __init__(self):
        self._clients = [{"id": "c1", "name": "Kitchen", "volume": 0.4,
                          "muted": False, "delay_ms": 0}]
        self._paired = [{"id": "c1", "name": "Kitchen"}]
        self.calls = []

    # pairing
    async def begin_pairing(self, *, method, code, client_id="", timeout_s=120.0):
        if method not in ("pairing_psk", "static_pin", "dynamic_pin"):
            raise ValueError(f"unknown pairing method {method!r}")
        if not code:
            raise ValueError("a pairing code is required")
        if method == "static_pin" and not (code.isdigit() and len(code) == 8):
            raise ValueError("a fixed device code is exactly 8 digits")
        if method != "pairing_psk" and not client_id:
            raise ValueError("choose a speaker to pair with")
        target = client_id or "from-token"
        self.calls.append(("pair", method, code, target))
        return target

    async def end_pairing(self, cid):
        self.calls.append(("endpair", cid))

    async def paired_clients(self):
        # async on purpose: the real store's accessors are coroutines
        return [dict(c) for c in self._paired]

    async def unpair(self, cid):
        self._paired = [c for c in self._paired if c["id"] != cid]
        self.calls.append(("unpair", cid))

    def discovered_clients(self):
        return [dict(c, paired=True, connected=True, url="") for c in self._clients]

    # zoning
    def clients(self):
        return [dict(c) for c in self._clients]

    async def set_client_volume(self, cid, level):
        self.calls.append(("cvol", cid, level))

    async def set_client_mute(self, cid, muted):
        self.calls.append(("cmute", cid, muted))

    async def set_client_delay(self, cid, ms):
        self.calls.append(("cdelay", cid, ms))


def _backend_with_adapter():
    b = SendspinBackend()
    b._adapter = FakeAdapter()
    b._connected = True
    return b


# ── backend-level pairing ─────────────────────────────────────────────────────


async def test_each_pairing_method_reaches_the_adapter(monkeypatch):
    b = _backend_with_adapter()
    await b.pair_speaker(method="pairing_psk", code="TOKEN-abc")
    await b.pair_speaker(method="static_pin", code="12345678", client_id="c1")
    await b.pair_speaker(method="dynamic_pin", code="4821", client_id="c1")
    methods = [c[1] for c in b._adapter.calls if c[0] == "pair"]
    assert methods == ["pairing_psk", "static_pin", "dynamic_pin"]


async def test_a_pairing_token_identifies_its_own_speaker(monkeypatch):
    """A token carries the speaker's identity, so nothing needs selecting."""
    b = _backend_with_adapter()
    assert await b.pair_speaker(method="pairing_psk", code="TOKEN-abc")


async def test_pin_pairing_requires_a_chosen_speaker(monkeypatch):
    b = _backend_with_adapter()
    with pytest.raises(ValueError, match="choose a speaker"):
        await b.pair_speaker(method="dynamic_pin", code="4821")


async def test_a_wrong_length_fixed_code_is_rejected_before_the_network(monkeypatch):
    b = _backend_with_adapter()
    with pytest.raises(ValueError, match="8 digits"):
        await b.pair_speaker(method="static_pin", code="1234", client_id="c1")
    assert not [c for c in b._adapter.calls if c[0] == "pair"]


async def test_an_unknown_pairing_method_is_rejected(monkeypatch):
    b = _backend_with_adapter()
    with pytest.raises(ValueError, match="unknown pairing method"):
        await b.pair_speaker(method="telepathy", code="x", client_id="c1")


async def test_unpair_drops_the_record(monkeypatch):
    b = _backend_with_adapter()
    assert [s["id"] for s in await b.paired_speakers()] == ["c1"]
    await b.unpair_speaker("c1")
    assert await b.paired_speakers() == []
    assert ("unpair", "c1") in b._adapter.calls


async def test_pairing_surface_refuses_when_disabled(monkeypatch):
    from app.output.sendspin import SendspinBackend
    b = SendspinBackend()
    with pytest.raises(DeviceNotReadyError):
        await b.pair_speaker(method="dynamic_pin", code="1", client_id="c1")
    with pytest.raises(DeviceNotReadyError):
        await b.unpair_speaker("c1")
    assert await b.paired_speakers() == []


# ── sealed PSK rotation (temp DB) ─────────────────────────────────────────────


@pytest.fixture
def tmp_settings(tmp_path, monkeypatch):
    s = Settings(data_dir=tmp_path, secret_key="test")
    monkeypatch.setattr(database, "settings", s)
    return s


@pytest.fixture
async def db(tmp_settings):
    # close_db() is NOT optional cleanup. aiosqlite runs its connection on a
    # NON-daemon worker thread; leaving it open makes threading._shutdown block
    # forever, so the whole pytest process hangs AFTER the tests pass. That is
    # why pytest-timeout never caught it — it is an interpreter-exit hang, not
    # an in-test one.
    await database.init_db()
    try:
        yield tmp_settings
    finally:
        await database.close_db()


# ── sealed pairing store (U2) ─────────────────────────────────────────────────
#
# The store's serialisation, sealing and corruption detection are pure of
# aiosendspin so they are testable on the local 3.11 interpreter; only the thin
# subclass that binds them to the library's record model is in-image-only.

from app.output import sendspin_adapter as ssa  # noqa: E402

_KEY = ssa.PAIRING_STORE_SETTING_KEY


async def test_pairing_blob_round_trips(db):
    payload = {"records": {"c1": {"psk": "AAA"}}, "staged_pairing_psks": {},
               "trusted_unpaired_clients": {}}
    await ssa.save_sealed_pairing_blob(payload)
    assert await ssa.load_sealed_pairing_blob() == payload


async def test_pairing_blob_is_sealed_at_rest(db):
    await ssa.save_sealed_pairing_blob({"records": {"c1": {"psk": "SUPERSECRET"}}})
    raw = await database.get_setting(_KEY)
    assert raw.startswith("enc:fernet:")
    assert "SUPERSECRET" not in raw


async def test_absent_store_loads_as_empty_not_an_error(db):
    assert await ssa.load_sealed_pairing_blob() is None


async def test_cleared_store_loads_as_empty(db):
    await ssa.save_sealed_pairing_blob({"records": {"c1": {}}})
    await ssa.save_sealed_pairing_blob({})
    assert await ssa.load_sealed_pairing_blob() is None


async def test_undecryptable_store_raises_instead_of_forgetting_everyone(db):
    """A rotated or lost seal key makes get_sealed_setting degrade to "" — which
    is indistinguishable from "never paired anything". Starting empty would
    present as every speaker silently forgetting its pairing, with no
    explanation. It must fail loudly instead."""
    await database.set_setting(_KEY, "enc:fernet:this-is-not-a-valid-token")
    with pytest.raises(ssa.SendspinPairingStoreError) as exc:
        await ssa.load_sealed_pairing_blob()
    assert "could not be opened" in str(exc.value).lower()


async def test_unsealable_garbage_that_is_not_json_raises(db):
    """Opens fine but is not our payload — still a corrupt store, not empty."""
    await database.set_sealed_setting(_KEY, "{not json at all")
    with pytest.raises(ssa.SendspinPairingStoreError):
        await ssa.load_sealed_pairing_blob()


async def test_a_non_object_payload_raises(db):
    await database.set_sealed_setting(_KEY, '["a", "list"]')
    with pytest.raises(ssa.SendspinPairingStoreError):
        await ssa.load_sealed_pairing_blob()


# ── pairing lifecycle against the REAL adapter logic (U3) ─────────────────────
#
# These drive sendspin_adapter's own begin_pairing/timer code with a stub
# *server*, rather than a fake adapter that reimplements the validation. The
# fake-adapter tests above assert a copy of the logic; these assert the logic.


class _StubServer:
    """Stands in for aiosendspin's SendspinServer — just the surface the
    adapter's pairing path touches."""

    def __init__(self, connected=("c1",), paired=()):
        self.calls = []
        self._connected = set(connected)
        self._paired = list(paired)
        self.pairing_store = self

    # discovery surface
    @property
    def clients(self):
        return [type("C", (), {"client_id": c, "name": c,
                               "is_paired": c in self._paired,
                               "is_connected": c in self._connected})()
                for c in self._connected | set(self._paired)]

    @property
    def connected_clients(self):
        return [c for c in self.clients if c.is_connected]

    def get_client_url(self, cid):
        return f"ws://192.0.2.9:8928/{cid}" if cid in self._connected else ""

    def get_client(self, cid):
        return None

    # pairing surface
    def initiate_pairing(self, cid, attempt):
        self.calls.append(("initiate", cid, attempt.method.value))

    def connect_to_client(self, url, *, pairing_attempt=None, **kw):
        self.calls.append(("dial", url))

    def end_pairing(self, cid):
        self.calls.append(("end", cid))

    def unpair(self, cid):
        self.calls.append(("unpair", cid))

    def untrust_unpaired(self, cid):
        self.calls.append(("untrust", cid))

    def trust_unpaired(self, cid):
        self.calls.append(("trust", cid))

    def disconnect_from_client(self, url):
        self.calls.append(("disconnect", url))

    # pairing-store surface
    async def list_records(self):
        return [type("R", (), {"client_id": c})() for c in self._paired]

    async def remove_record(self, cid):
        self._paired = [c for c in self._paired if c != cid]
        self.calls.append(("remove_record", cid))


def _adapter(server):
    a = ssa.SendspinAdapter(server, object())
    return a


def _seen(cid, *, connected, url="ws://192.0.2.9:8928/x"):
    return [{"id": cid, "name": cid, "paired": False,
             "connected": connected, "url": url}]


def test_pairing_plan_rejects_an_unreachable_speaker():
    with pytest.raises(ValueError, match="not reachable"):
        ssa.SendspinAdapter.plan_pairing(
            method="dynamic_pin", code="4821", client_id="c9", discovered=[])


def test_pairing_plan_uses_the_live_connection_when_there_is_one():
    route, target, _ = ssa.SendspinAdapter.plan_pairing(
        method="dynamic_pin", code="4821", client_id="c1",
        discovered=_seen("c1", connected=True))
    assert (route, target) == ("initiate", "c1")


def test_pairing_plan_dials_out_to_a_speaker_that_is_not_connected():
    """A brand-new speaker that advertised itself is onboarded server-initiated:
    an unknown speaker dialling IN is refused before pairing can start."""
    route, target, url = ssa.SendspinAdapter.plan_pairing(
        method="dynamic_pin", code="4821", client_id="c5",
        discovered=_seen("c5", connected=False))
    assert route == "dial" and target == "c5" and url.startswith("ws://")


def test_pairing_plan_rejects_a_speaker_with_no_route_at_all():
    with pytest.raises(ValueError, match="not reachable"):
        ssa.SendspinAdapter.plan_pairing(
            method="dynamic_pin", code="4821", client_id="c5",
            discovered=_seen("c5", connected=False, url=""))


def test_pairing_plan_rejects_bad_input_before_any_network_call():
    for kw, msg in (
        (dict(method="telepathy", code="x", client_id="c1"), "unknown pairing method"),
        (dict(method="dynamic_pin", code="", client_id="c1"), "code is required"),
        (dict(method="dynamic_pin", code="4821", client_id=""), "choose a speaker"),
    ):
        with pytest.raises(ValueError, match=msg):
            ssa.SendspinAdapter.plan_pairing(
                discovered=_seen("c1", connected=True), **kw)


async def test_the_pairing_timer_can_actually_complete_its_cleanup():
    """The expiry task calls end_pairing, which cancels the pairing timer — the
    task it is running inside. Cancelling itself killed the cleanup at its first
    await, and CancelledError being a BaseException meant nothing even logged
    it: the one reaper in the pairing system never finished."""
    srv = _StubServer(connected=("c1",))
    a = _adapter(srv)
    a._arm_pairing_timeout("c1", 0.05)
    await asyncio.sleep(0.35)
    assert ("end", "c1") in srv.calls, "the pairing attempt was never reaped"


async def test_a_successful_pair_is_not_torn_down_when_the_window_closes():
    """The timer is armed for 120s and pairing usually succeeds long before. If
    expiry reaped unconditionally it would disconnect a working speaker
    mid-track, minutes after it paired."""
    srv = _StubServer(connected=("c1",))
    a = _adapter(srv)
    a._arm_pairing_timeout("c1", 0.05)
    srv._paired = ["c1"]                        # pairing completes meanwhile
    await asyncio.sleep(0.35)
    assert ("end", "c1") not in srv.calls


async def test_unpairing_an_offline_speaker_still_revokes_it():
    """The library resolves a live connection BEFORE removing the record, so it
    raises for an offline speaker — exactly the case revocation exists for (a
    speaker taken, lost or handed on). The record must go regardless."""
    srv = _StubServer(connected=(), paired=("c7",))

    def _boom(cid):
        raise ValueError(f"client {cid} is not connected")

    srv.unpair = _boom
    a = _adapter(srv)
    await a.unpair("c7")
    assert ("remove_record", "c7") in srv.calls
    assert await a.paired_clients() == []


async def test_unpair_also_drops_any_trusted_unpaired_grant():
    srv = _StubServer(connected=("c1",), paired=("c1",))
    a = _adapter(srv)
    await a.unpair("c1")
    assert ("untrust", "c1") in srv.calls


async def test_trusted_unpaired_access_is_refused_outside_the_test_harness():
    """It grants full transport with no pairing at all, so it must not be
    reachable on a running server."""
    a = _adapter(_StubServer())
    with pytest.raises(RuntimeError, match="refused outside the test harness"):
        await a.allow_unpaired("c1")


async def test_a_stream_without_listeners_does_not_count_as_a_stream():
    """The queue-drain P0 came back once because has_stream() only asked "did we
    ever create a stream". After the room empties there is a stream object and
    nobody to hear it, and the feed must stay in its wall-clock pacing branch."""
    srv = _StubServer(connected=("c1",))
    a = _adapter(srv)
    a._stream = object()                      # a stream exists
    assert a.has_stream() is True
    srv._connected = set()                    # everyone leaves
    assert a.has_stream() is False


async def test_an_empty_room_drops_the_group_instead_of_reusing_a_stale_one():
    srv = _StubServer(connected=("c1",))
    a = _adapter(srv)
    a._group = object()                       # group built while occupied
    srv._connected = set()
    assert a._ensure_group() is None
    assert a._group is None, "a stale group would produce a listener-less stream"


async def test_reconcile_tears_the_stream_down_when_the_last_speaker_leaves():
    srv = _StubServer(connected=())
    a = _adapter(srv)
    stopped = []
    a._stream = type("S", (), {"stop": lambda self: stopped.append(True)})()
    await a.reconcile_stream()
    assert stopped and a._stream is None


async def test_unpair_ends_a_pending_pairing_attempt():
    """Leaving an attempt registered — while cancelling the timer that would
    have reaped it — lets the speaker we just revoked re-pair itself with the
    code it already holds."""
    srv = _StubServer(connected=("c1",), paired=("c1",))
    a = _adapter(srv)
    a._arm_pairing_timeout("c1", 300)          # an attempt is outstanding
    await a.unpair("c1")
    assert ("end", "c1") in srv.calls


async def test_unpair_raises_when_the_record_survives():
    """Revocation is the only security control a speaker has; reporting success
    while the key is still stored is worse than failing."""
    srv = _StubServer(connected=("c1",), paired=("c1",))

    async def _noop(cid):
        pass

    srv.remove_record = _noop                  # store silently keeps it
    a = _adapter(srv)
    with pytest.raises(RuntimeError, match="did not take effect"):
        await a.unpair("c1")


async def test_unpair_raises_when_there_is_no_store_at_all():
    srv = _StubServer(connected=("c1",))
    srv.pairing_store = None
    a = _adapter(srv)
    with pytest.raises(RuntimeError, match="NOT revoked"):
        await a.unpair("c1")


# ── zoning fan-out ────────────────────────────────────────────────────────────


async def test_group_volume_fans_out(monkeypatch):
    b = _backend_with_adapter()
    b._adapter._clients = [{"id": "c1", "name": "K", "volume": 0.4, "muted": False},
                           {"id": "c2", "name": "L", "volume": 0.8, "muted": False}]
    await b.set_group_volume("sendspin", 0.6)
    cvol = [c for c in b._adapter.calls if c[0] == "cvol"]
    assert {c[1] for c in cvol} == {"c1", "c2"}


async def test_group_volume_preserves_the_balance_between_rooms(monkeypatch):
    """Snapcast's group slider scales clients PROPORTIONALLY, keeping one room
    quieter than another. Parity means Sendspin must not flatten them — which is
    also why the library's single group-level volume is deliberately unused."""
    b = _backend_with_adapter()
    b._adapter._clients = [{"id": "c1", "name": "K", "volume": 0.2, "muted": False},
                           {"id": "c2", "name": "L", "volume": 0.8, "muted": False}]
    await b.set_group_volume("sendspin", 0.75)
    levels = {c[1]: c[2] for c in b._adapter.calls if c[0] == "cvol"}
    assert levels["c1"] < levels["c2"]        # the quiet room stays the quiet one
    assert 0.0 <= levels["c1"] <= 1.0 and 0.0 <= levels["c2"] <= 1.0


async def test_delay_trim_round_trips_through_list_zones(monkeypatch):
    b = _backend_with_adapter()
    b._adapter._clients = [{"id": "c1", "name": "K", "volume": 0.4,
                            "muted": False, "delay_ms": 35}]
    zones = await b.list_zones()
    assert zones[0]["clients"][0]["delay_ms"] == 35


async def test_delay_trim_is_clamped_non_negative(monkeypatch):
    b = _backend_with_adapter()
    await b.set_client_delay("c1", -20)
    assert ("cdelay", "c1", 0) in b._adapter.calls


async def test_volume_is_clamped_rather_than_rejected(monkeypatch):
    b = _backend_with_adapter()
    await b.set_client_volume("c1", 4.2)
    await b.set_client_volume("c1", -1.0)
    levels = [c[2] for c in b._adapter.calls if c[0] == "cvol"]
    assert levels == [1.0, 0.0]


async def test_group_volume_with_no_speakers_is_a_noop(monkeypatch):
    b = _backend_with_adapter()
    b._adapter._clients = []
    await b.set_group_volume("sendspin", 0.5)   # must not raise
    assert not [c for c in b._adapter.calls if c[0] == "cvol"]


# ── admin endpoints ───────────────────────────────────────────────────────────


class _EndpointBackend:
    _connected = True

    def is_connected(self):
        return True

    async def discovered_speakers(self):
        return [{"id": "c3", "name": "Porch", "paired": False,
                 "connected": False, "url": ""}]

    async def paired_speakers(self):
        return [{"id": "c1", "name": "Kitchen"}]

    async def pair_speaker(self, *, method, code, client_id=""):
        if method == "static_pin" and len(code) != 8:
            raise ValueError("a fixed device code is exactly 8 digits")
        self.paired_with = (method, code, client_id)
        return client_id or "from-token"

    async def unpair_speaker(self, cid):
        self.unpaired = cid

    async def list_zones(self):
        return [{"group_id": "sendspin", "clients": []}]

    async def set_client_volume(self, cid, level):
        self.vol = (cid, level)

    async def set_client_delay(self, cid, ms):
        self.delay = (cid, ms)


def test_speakers_endpoint_reports_discovered_and_paired(client, monkeypatch):
    monkeypatch.setattr(state, "get_server_fed_backend", lambda t: _EndpointBackend())
    r = client.get("/admin/output/sendspin/speakers")
    assert r.status_code == 200
    body = r.json()
    assert [s["id"] for s in body["discovered"]] == ["c3"]
    assert [s["id"] for s in body["paired"]] == ["c1"]
    # The pairing contract travels with the response, so an API caller learns
    # the rules here instead of by provoking 400s.
    by_id = {m["id"]: m for m in body["methods"]}
    assert set(by_id) == {"pairing_psk", "static_pin", "dynamic_pin"}
    assert by_id["pairing_psk"]["requires_client_id"] is False
    assert by_id["static_pin"]["requires_client_id"] is True
    assert "8 digits" in by_id["static_pin"]["code_format"]


def test_pair_endpoint_passes_the_operator_code_through(client, monkeypatch):
    be = _EndpointBackend()
    monkeypatch.setattr(state, "get_server_fed_backend", lambda t: be)
    r = client.post("/admin/output/sendspin/pair",
                    json={"method": "dynamic_pin", "code": "4821",
                          "client_id": "c3"})
    assert r.status_code == 200 and r.json()["client_id"] == "c3"
    assert be.paired_with == ("dynamic_pin", "4821", "c3")


def test_pair_endpoint_surfaces_a_bad_code_as_a_400(client, monkeypatch):
    monkeypatch.setattr(state, "get_server_fed_backend", lambda t: _EndpointBackend())
    r = client.post("/admin/output/sendspin/pair",
                    json={"method": "static_pin", "code": "1234",
                          "client_id": "c3"})
    assert r.status_code == 400 and "8 digits" in r.json()["detail"]


def test_unpair_endpoint_revokes(client, monkeypatch):
    be = _EndpointBackend()
    monkeypatch.setattr(state, "get_server_fed_backend", lambda t: be)
    assert client.post("/admin/output/sendspin/client/c1/unpair").status_code == 200
    assert be.unpaired == "c1"


def test_delay_endpoint_sets_the_room_trim(client, monkeypatch):
    be = _EndpointBackend()
    monkeypatch.setattr(state, "get_server_fed_backend", lambda t: be)
    r = client.post("/admin/output/sendspin/client/c1/delay", json={"delay_ms": 40})
    assert r.status_code == 200 and be.delay == ("c1", 40)


def test_sendspin_zones_endpoint(client, monkeypatch):
    monkeypatch.setattr(state, "get_server_fed_backend", lambda t: _EndpointBackend())
    r = client.get("/admin/output/sendspin/zones")
    assert r.status_code == 200 and r.json()["experimental"] is True


def test_sendspin_404_when_disabled(client, monkeypatch):
    monkeypatch.setattr(state, "get_server_fed_backend", lambda t: None)
    assert client.get("/admin/output/sendspin/zones").status_code == 404


def test_sendspin_endpoints_require_admin(anon_client):
    assert anon_client.get("/admin/output/sendspin/zones").status_code in (401, 403)
    # Pairing IS the authority boundary for a speaker, so the routes that grant
    # and revoke it must never be reachable anonymously.
    assert anon_client.get("/admin/output/sendspin/speakers").status_code in (401, 403)
    assert anon_client.post(
        "/admin/output/sendspin/pair",
        json={"method": "dynamic_pin", "code": "1", "client_id": "c1"},
    ).status_code in (401, 403)
    assert anon_client.post(
        "/admin/output/sendspin/client/c1/unpair").status_code in (401, 403)
