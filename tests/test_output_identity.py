"""Identity verification for idle re-attach (2026-09-01 plan U3, origin R3/R4).

The gate the whole automation hangs off: "is this arriving device confirmably
the one the admin selected?" Everything here is a pure function over plain
data — no watcher, no backends, no event loop — because a predicate this
load-bearing should be provable without a harness.

The bar is deliberately asymmetric. A false negative costs the host one tap on
a device that never left the picker. A false positive attaches their party to a
stranger's speaker. So every ambiguity resolves toward NOT firing.
"""

import pytest

from app.output.base import OutputDevice
from app.output.identity import Selection, identity_confirmed


def _dev(device_id, name, *, backend="chromecast", fmt="uuid"):
    return OutputDevice(id=device_id, name=name, backend_type=backend,
                        id_format=fmt)


def _sel(device_id, name, *, backend="chromecast"):
    return Selection(backend_type=backend, device_id=device_id, name=name)


# ── strong identity: the backend keys by a stable id ─────────────────────────


def test_uuid_format_matching_id_is_confirmed():
    """Chromecast/DLNA/plexplayer carry a real identifier. Id equality is the
    whole test — the address is free to change underneath it."""
    arriving = _dev("uuid-abc", "Kitchen")
    assert identity_confirmed(_sel("uuid-abc", "Kitchen"), arriving, [arriving])


def test_uuid_format_different_id_is_not_confirmed():
    arriving = _dev("uuid-xyz", "Kitchen")
    assert not identity_confirmed(
        _sel("uuid-abc", "Kitchen"), arriving, [arriving])


def test_uuid_match_wins_even_when_the_name_changed():
    """Renaming a Cast device must not break re-attach: the stable id is the
    identity, and the name is only a fallback for backends without one."""
    arriving = _dev("uuid-abc", "Kitchen Renamed")
    assert identity_confirmed(_sel("uuid-abc", "Kitchen"), arriving, [arriving])


def test_uuid_match_ignores_a_name_collision():
    """A same-named sibling is irrelevant when identity is strong — the
    ambiguity rule exists only because host:port ids are not identities."""
    arriving = _dev("uuid-abc", "Kitchen")
    twin = _dev("uuid-def", "Kitchen")
    assert identity_confirmed(
        _sel("uuid-abc", "Kitchen"), arriving, [arriving, twin])


# ── weak identity: the backend keys by address, so the name decides ──────────


def test_host_port_single_name_match_is_confirmed():
    """AirPlay's device_id IS its network location, so id equality proves
    nothing about identity. Exactly one device carrying the stored name, and
    the arrival being that device, is the bar (KTD3)."""
    arriving = _dev("10.0.0.7:7000", "Patio", backend="airplay",
                    fmt="host_port")
    assert identity_confirmed(
        _sel("10.0.0.5:7000", "Patio", backend="airplay"),
        arriving, [arriving])


def test_host_port_confirms_across_a_changed_dhcp_lease():
    """The reason name-based identity exists at all: the speaker came back on a
    new lease. Under id-equality this device is a stranger; under the name rule
    it is correctly recognised."""
    arriving = _dev("10.0.0.99:7000", "Patio", backend="airplay",
                    fmt="host_port")
    sel = _sel("10.0.0.5:7000", "Patio", backend="airplay")
    assert arriving.id != sel.device_id
    assert identity_confirmed(sel, arriving, [arriving])


def test_host_port_two_same_named_devices_is_never_confirmed():
    """Origin AE4. Two known devices share a name and the selection is one of
    them: the automation declines, whichever one arrives. The other direction
    would attach the host's party to whichever speaker announced first."""
    a = _dev("10.0.0.7:7000", "Speaker", backend="airplay", fmt="host_port")
    b = _dev("10.0.0.8:7000", "Speaker", backend="airplay", fmt="host_port")
    sel = _sel("10.0.0.7:7000", "Speaker", backend="airplay")
    assert not identity_confirmed(sel, a, [a, b])
    assert not identity_confirmed(sel, b, [a, b])


def test_host_port_no_name_match_is_not_confirmed():
    arriving = _dev("10.0.0.7:7000", "Garage", backend="airplay",
                    fmt="host_port")
    assert not identity_confirmed(
        _sel("10.0.0.5:7000", "Patio", backend="airplay"),
        arriving, [arriving])


def test_host_port_name_match_but_a_different_device_arrived():
    """One device carries the name, but it is not the one that just arrived —
    so this arrival is some third device and must not be adopted."""
    named = _dev("10.0.0.7:7000", "Patio", backend="airplay", fmt="host_port")
    other = _dev("10.0.0.9:7000", "Garage", backend="airplay", fmt="host_port")
    assert not identity_confirmed(
        _sel("10.0.0.5:7000", "Patio", backend="airplay"),
        other, [named, other])


# ── name comparison rules (KTD3) ────────────────────────────────────────────


@pytest.mark.parametrize("stored,announced", [
    ("Patio", "patio"),
    ("Patio", "PATIO"),
    ("Patio", " Patio "),
    (" Patio", "Patio "),
])
def test_name_match_is_trimmed_and_case_insensitive(stored, announced):
    """Whitespace and case drift through mDNS re-registration; a host who
    typed "Patio" should not lose re-attach because the record says "patio"."""
    arriving = _dev("10.0.0.7:7000", announced, backend="airplay",
                    fmt="host_port")
    assert identity_confirmed(
        _sel("10.0.0.5:7000", stored, backend="airplay"),
        arriving, [arriving])


def test_case_insensitivity_makes_near_duplicates_ambiguous():
    """The deliberate cost of the rule above, and the safe direction: because
    "Kitchen" and "kitchen" compare equal, two such devices are a COLLISION
    and the automation declines. Treating them as distinct would let it pick
    one, and picking wrong is the failure that matters."""
    a = _dev("10.0.0.7:7000", "Kitchen", backend="airplay", fmt="host_port")
    b = _dev("10.0.0.8:7000", "kitchen", backend="airplay", fmt="host_port")
    assert not identity_confirmed(
        _sel("10.0.0.7:7000", "Kitchen", backend="airplay"), a, [a, b])


def test_empty_stored_name_is_never_a_wildcard():
    """A selection with no resolvable name cannot use name identity. It must
    read as unverifiable, NOT as "matches anything" — the latter would attach
    to the first device that showed up on that backend."""
    arriving = _dev("10.0.0.7:7000", "", backend="airplay", fmt="host_port")
    assert not identity_confirmed(
        _sel("10.0.0.5:7000", "", backend="airplay"), arriving, [arriving])
    named = _dev("10.0.0.7:7000", "Patio", backend="airplay", fmt="host_port")
    assert not identity_confirmed(
        _sel("10.0.0.5:7000", "", backend="airplay"), named, [named])


def test_whitespace_only_stored_name_is_not_a_wildcard():
    """The trimming rule must not turn "   " into a match-everything name."""
    arriving = _dev("10.0.0.7:7000", "   ", backend="airplay", fmt="host_port")
    assert not identity_confirmed(
        _sel("10.0.0.5:7000", "   ", backend="airplay"), arriving, [arriving])


# ── scoping and degenerate input ────────────────────────────────────────────


def test_siblings_from_another_backend_do_not_create_ambiguity():
    """A Cast device and an AirPlay device may legitimately share a name — the
    admin named both "Kitchen". Only same-backend devices can collide."""
    air = _dev("10.0.0.7:7000", "Kitchen", backend="airplay", fmt="host_port")
    cast = _dev("uuid-1", "Kitchen", backend="chromecast")
    assert identity_confirmed(
        _sel("10.0.0.5:7000", "Kitchen", backend="airplay"),
        air, [air, cast])


def test_arrival_on_a_different_backend_is_not_confirmed():
    """Backend must match before anything else is even considered."""
    arriving = _dev("uuid-abc", "Kitchen", backend="dlna")
    assert not identity_confirmed(
        _sel("uuid-abc", "Kitchen", backend="chromecast"),
        arriving, [arriving])


def test_no_known_devices_at_all_is_not_confirmed():
    """Weak path with an empty registry view: nothing to count, so nothing to
    confirm. (An arrival always appears in its own siblings in production;
    this covers a caller that passes a stale or emptied view.)"""
    arriving = _dev("10.0.0.7:7000", "Patio", backend="airplay",
                    fmt="host_port")
    assert not identity_confirmed(
        _sel("10.0.0.5:7000", "Patio", backend="airplay"), arriving, [])


def test_empty_selection_id_is_not_confirmed():
    """Nothing meaningfully selected — the coordinator has no business acting."""
    arriving = _dev("uuid-abc", "Kitchen")
    assert not identity_confirmed(_sel("", "Kitchen"), arriving, [arriving])


def test_unknown_id_format_falls_back_to_the_weak_path():
    """Fail CLOSED on an unexpected declaration: an id_format this module does
    not recognise must not be treated as a stable identifier. A future backend
    that forgets to declare gets name verification, not a free pass."""
    arriving = _dev("something-odd", "Patio", backend="airplay",
                    fmt="future-format")
    # Weak path is satisfied here (one name match), so it confirms...
    assert identity_confirmed(
        _sel("other-id", "Patio", backend="airplay"), arriving, [arriving])
    # ...but crucially NOT via id equality, which would have matched blindly.
    twin = _dev("another", "Patio", backend="airplay", fmt="future-format")
    assert not identity_confirmed(
        _sel("something-odd", "Patio", backend="airplay"),
        arriving, [arriving, twin])
