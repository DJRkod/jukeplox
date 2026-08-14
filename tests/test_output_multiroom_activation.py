"""U2 — dormant opt-in activation + persistence for the server-fed backends.

Proves the dormancy contract that resolves the top doc-review finding: a
disabled server-fed backend constructs/imports nothing, a disabled OR
selected-but-unconstructed server-fed type NEVER resolves to ``direct_backend``
(the R17 host-speaker leak), enabled-ness survives restart, and the Sendspin PSK
is sealed at rest. The concrete backends (U5/U7) don't exist yet, so the
construction chokepoint is monkeypatched with a fake backend.
"""

import sys

import pytest
from unittest.mock import MagicMock

from app import database, state
from app.config import Settings


class FakeServerFed:
    def __init__(self):
        self.enabled = False
        self.disabled = False
        self.is_playing = False
        self.enable_should_fail = False

    async def enable(self):
        if self.enable_should_fail:
            raise RuntimeError("server start failed")
        self.enabled = True

    async def disable(self):
        self.disabled = True


@pytest.fixture(autouse=True)
def reset_activation(monkeypatch):
    """Isolate the module-level server-fed registry + router per test."""
    monkeypatch.setattr(state, "_server_fed_backends", {})
    monkeypatch.setattr(
        state, "_server_fed_enabled",
        {t: False for t in state.SERVER_FED_BACKEND_TYPES})
    monkeypatch.setattr(state, "output_router", MagicMock())
    yield


@pytest.fixture
def fake_construct(monkeypatch):
    made = {}

    def _construct(backend_type):
        inst = FakeServerFed()
        made[backend_type] = inst
        return inst

    monkeypatch.setattr(state, "_construct_server_fed_backend", _construct)
    return made


# ── enable / disable / registry ──────────────────────────────────────────────


async def test_enable_constructs_caches_and_registers(fake_construct):
    backend = await state.enable_server_fed_backend("snapcast")
    assert backend is fake_construct["snapcast"]
    assert backend.enabled is True
    assert state.server_fed_backend_enabled("snapcast") is True
    assert state.get_server_fed_backend("snapcast") is backend
    assert state._get_backend("snapcast") is backend
    assert state._backend_type_of(backend) == "snapcast"


async def test_enable_is_idempotent(fake_construct):
    b1 = await state.enable_server_fed_backend("snapcast")
    b2 = await state.enable_server_fed_backend("snapcast")
    assert b1 is b2  # not reconstructed


async def test_disable_tears_down_and_evicts(fake_construct):
    backend = await state.enable_server_fed_backend("snapcast")
    await state.disable_server_fed_backend("snapcast")
    assert backend.disabled is True
    assert state.server_fed_backend_enabled("snapcast") is False
    assert state.get_server_fed_backend("snapcast") is None
    # The critical R17 property: a disabled server-fed type resolves to None,
    # NOT direct_backend.
    assert state._get_backend("snapcast") is None


async def test_get_backend_disabled_is_none_never_direct(monkeypatch, fake_construct):
    sentinel = object()
    monkeypatch.setattr(state, "direct_backend", sentinel)
    got = state._get_backend("sendspin")  # never enabled
    assert got is None
    assert got is not sentinel
    # The construction chokepoint must NOT have been touched (no import).
    assert "sendspin" not in fake_construct


async def test_set_backend_by_type_holds_for_unconstructed_server_fed(fake_construct):
    # snapcast selected but never enabled → HOLD, router never pointed anywhere.
    state._set_backend_by_type("snapcast")
    state.output_router.set_backend.assert_not_called()


async def test_set_backend_by_type_activates_enabled_server_fed(fake_construct):
    backend = await state.enable_server_fed_backend("snapcast")
    state._set_backend_by_type("snapcast")
    state.output_router.set_backend.assert_called_once_with(backend)


async def test_enable_failure_stays_dormant(monkeypatch):
    def _construct(_bt):
        inst = FakeServerFed()
        inst.enable_should_fail = True
        return inst

    monkeypatch.setattr(state, "_construct_server_fed_backend", _construct)
    with pytest.raises(RuntimeError, match="server start failed"):
        await state.enable_server_fed_backend("snapcast")
    # Half-built instance evicted; mirror stays False.
    assert state.get_server_fed_backend("snapcast") is None
    assert state.server_fed_backend_enabled("snapcast") is False


def test_get_backend_reads_dict_without_importing(monkeypatch):
    called = []
    monkeypatch.setattr(state, "_construct_server_fed_backend",
                        lambda bt: called.append(bt))
    state._get_backend("snapcast")  # disabled
    assert called == []  # never constructs / imports on a plain lookup


def test_boot_does_not_import_heavy_libs():
    """Importing app.state must not drag in the heavy server-fed libraries —
    they live behind the function-local import in _construct (dormancy)."""
    assert "aiosendspin" not in sys.modules
    assert "snapcast" not in sys.modules


# ── persistence (temp DB) ────────────────────────────────────────────────────


@pytest.fixture
def tmp_settings(tmp_path, monkeypatch):
    s = Settings(data_dir=tmp_path, secret_key="test")
    monkeypatch.setattr(database, "settings", s)
    return s


@pytest.fixture
async def db(tmp_settings):
    # Must close: aiosqlite's connection runs on a NON-daemon thread, so leaving
    # it open hangs the pytest process at interpreter exit, after the tests have
    # already passed.
    await database.init_db()
    try:
        yield tmp_settings
    finally:
        await database.close_db()


async def test_set_backend_enabled_persists_and_applies(db, fake_construct):
    backend = await state.set_backend_enabled("snapcast", True)
    assert backend is fake_construct["snapcast"]
    assert await database.get_backend_enabled("snapcast") is True
    await state.set_backend_enabled("snapcast", False)
    assert await database.get_backend_enabled("snapcast") is False
    assert state.get_server_fed_backend("snapcast") is None


async def test_backend_enabled_default_off(db):
    assert await database.get_backend_enabled("snapcast") is False
    assert await database.get_backend_enabled("sendspin") is False


async def test_boot_restore_enables_persisted_not_direct(db, fake_construct):
    # Persist enabled=True, then run the boot-restore snippet's logic.
    await database.set_backend_enabled("sendspin", True)
    for sf in state.SERVER_FED_BACKEND_TYPES:
        if await database.get_backend_enabled(sf):
            await state.enable_server_fed_backend(sf)
    assert state._get_backend("sendspin") is fake_construct["sendspin"]
    assert state._get_backend("sendspin") is not None  # not a direct fallback


async def test_selected_but_not_enabled_holds(db):
    # sendspin persisted as selected but NOT enabled at boot → held, not direct.
    assert await database.get_backend_enabled("sendspin") is False
    assert state._get_backend("sendspin") is None


async def test_sendspin_psk_sealed_at_rest(db):
    await database.set_sealed_setting("sendspin_pairing_psk", "s3cr3t-psk")
    raw = await database.get_setting("sendspin_pairing_psk")
    assert raw is not None
    assert raw.startswith("enc:fernet:")  # sealed, not plaintext
    assert "s3cr3t-psk" not in raw
    assert await database.get_sealed_setting("sendspin_pairing_psk") == "s3cr3t-psk"


async def test_zone_volume_round_trips(db):
    assert await database.get_zone_volume("snapcast", "g1") is None
    await database.set_zone_volume("snapcast", "g1", 0.42)
    assert await database.get_zone_volume("snapcast", "g1") == pytest.approx(0.42, abs=1e-4)
    # corrupted value degrades to None, not a crash
    await database.set_setting("zone_vol:snapcast:g2", "not-a-float")
    assert await database.get_zone_volume("snapcast", "g2") is None


async def test_migration_idempotent(db):
    await database.init_db()  # second init must not raise
    await database.set_zone_volume("snapcast", "g1", 0.5)
    assert await database.get_zone_volume("snapcast", "g1") == pytest.approx(0.5, abs=1e-4)
