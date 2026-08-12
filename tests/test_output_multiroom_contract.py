"""U1 — structural enforcement of the server-fed zoning contract.

The plan's top cross-unit risk is contract drift between the two backends built
by separate units (the radio-era "unenforced contract" failure mode). This test
asserts ``SnapcastBackend`` and ``SendspinBackend`` expose the identical zoning
method set + signatures, so a rename/re-signature fails at each backend's own
unit boundary — not at U9 integration. Until U5/U7 land those classes the
backend checks skip (with a reason); the negative/self-consistency checks run
always so the enforcement mechanism itself is proven from U1.
"""

import pytest

from app.output.multiroom import (
    ZONING_CONTRACT,
    assert_zoning_contract,
    zoning_signature,
)


def test_contract_is_self_consistent():
    """Every declared contract method name is a plausible identifier and the
    parameter tuples are well-formed (no accidental self / duplicates)."""
    assert ZONING_CONTRACT, "the zoning contract must not be empty"
    for name, params in ZONING_CONTRACT.items():
        assert name.isidentifier()
        assert "self" not in params
        assert len(params) == len(set(params)), f"{name} has duplicate params"


def test_checker_accepts_a_conforming_stub():
    class _Good:
        def supports_zoning(self): ...
        async def list_zones(self): ...
        async def set_client_volume(self, client_id, level): ...
        async def set_client_mute(self, client_id, muted): ...
        async def set_group_mute(self, group_id, muted): ...
        async def set_group_volume(self, group_id, level): ...
        async def assign_client_to_group(self, client_id, group_id): ...
        def can_manage_topology(self): ...

    assert_zoning_contract(_Good, name="Good")  # must not raise


def test_checker_rejects_missing_method():
    class _MissingOne:
        def supports_zoning(self): ...
        async def list_zones(self): ...
        async def set_client_volume(self, client_id, level): ...
        async def set_client_mute(self, client_id, muted): ...
        async def set_group_mute(self, group_id, muted): ...
        async def set_group_volume(self, group_id, level): ...
        async def assign_client_to_group(self, client_id, group_id): ...
        # can_manage_topology intentionally absent

    with pytest.raises(AssertionError, match="can_manage_topology"):
        assert_zoning_contract(_MissingOne, name="MissingOne")


def test_checker_rejects_renamed_param():
    class _Drifted:
        def supports_zoning(self): ...
        async def list_zones(self): ...
        async def set_client_volume(self, cid, level): ...  # cid != client_id
        async def set_client_mute(self, client_id, muted): ...
        async def set_group_mute(self, group_id, muted): ...
        async def set_group_volume(self, group_id, level): ...
        async def assign_client_to_group(self, client_id, group_id): ...
        def can_manage_topology(self): ...

    with pytest.raises(AssertionError, match="set_client_volume"):
        assert_zoning_contract(_Drifted, name="Drifted")


def test_zoning_signature_ignores_self():
    class _X:
        async def m(self, a, b): ...

    assert zoning_signature(_X.m) == ("a", "b")


@pytest.mark.parametrize("modpath,clsname", [
    ("app.output.snapcast", "SnapcastBackend"),
    ("app.output.sendspin", "SendspinBackend"),
])
def test_concrete_backends_honor_the_contract(modpath, clsname):
    """Enforced once U5/U7 exist. Importing the backend class must NOT require
    the heavy library (snapcast/aiosendspin) — those imports are function-local
    for dormancy (R16), so the class is importable for this structural check."""
    mod = pytest.importorskip(modpath)
    cls = getattr(mod, clsname, None)
    if cls is None:
        pytest.skip(f"{clsname} not defined yet")
    assert_zoning_contract(cls, name=clsname)
