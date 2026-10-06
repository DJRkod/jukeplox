"""Is this arriving device confirmably the one the admin selected?

The gate the idle re-attach automation hangs off (2026-09-01 plan U3, origin
R3/R4). Deliberately a pure function over plain data: no watcher, no backends,
no event loop, no I/O, no logging. The coordinator supplies a view; this module
only decides.

WHY THIS EXISTS AT ALL. Not every backend has a device identity. AirPlay's
``device_id`` is ``host:port`` — the identity IS the network location — and
that breaks in both directions. A stranger who inherits the DHCP lease looks
like your speaker, and your real speaker returning on a new lease looks like a
different device and is never found. An automation that re-attaches on id
equality alone would therefore be both unsafe and ineffective on exactly the
backend where sleeping speakers are most common.

THE DISCRIMINATOR ALREADY EXISTED. ``OutputDevice.id_format`` declares
``"uuid"`` vs ``"host_port"`` per DEVICE, which is finer than per backend and
that finer grain matters: Chromecast declares ``uuid`` only when the mDNS TXT
carried an ``id=`` entry and ``host_port`` otherwise, so a per-backend rule
would have mis-declared it. Read the declaration; do not re-derive it.

THE BAR IS ASYMMETRIC ON PURPOSE. A false negative costs the host one tap on a
device that never left the picker (the purge exemption keeps it listed either
way). A false positive attaches their party to a stranger's speaker. So every
ambiguity — a name shared by two devices, a name that is empty, an id_format
this module does not recognise — resolves toward NOT firing. The gate fails
closed, which is also what makes the origin's decision to show nothing honest:
when it declines, the outcome is indistinguishable from the feature not
existing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.output.base import OutputDevice

# id_format values whose id is a genuine, location-independent identifier.
# Anything else — including a value added by a future backend that forgets to
# declare one — routes to name verification rather than being trusted.
_STABLE_ID_FORMATS = frozenset({"uuid"})


@dataclass(frozen=True)
class Selection:
    """The admin's persisted output choice, as the identity gate needs it.

    Mirrors ``state.selected_output_key()`` plus ``state.selected_output_name()``
    rather than reading them, so this module stays a pure function and the
    coordinator owns where the values come from."""

    backend_type: str
    device_id: str
    name: str


def _normalize(name: str) -> str:
    """The name-comparison rule (KTD3): trimmed, case-folded.

    Case-insensitivity is chosen knowing it WIDENS what counts as a collision:
    "Kitchen" and "kitchen" become the same name, so owning both means the
    automation declines. That is the safe direction. The alternative — treating
    them as distinct — lets the gate pick one, and picking wrong is the failure
    that actually costs the host something.

    Returns "" for a name that is empty or whitespace-only, which callers treat
    as unverifiable rather than as a wildcard."""
    return (name or "").strip().casefold()


def identity_confirmed(
    selection: Selection,
    arriving: OutputDevice,
    siblings: Iterable[OutputDevice],
) -> bool:
    """True when *arriving* is confirmably *selection*'s device.

    *siblings* is every device currently known for the same backend (the
    arrival included). Only same-backend devices can collide on a name: an
    admin may legitimately have called both a Cast device and an AirPlay
    receiver "Kitchen", and that is not an ambiguity.
    """
    if not selection.device_id or not selection.backend_type:
        return False
    if arriving.backend_type != selection.backend_type:
        return False

    if arriving.id_format in _STABLE_ID_FORMATS:
        # Strong path. The id is an identity, so equality settles it and the
        # name is irrelevant — a renamed Cast device must still re-attach, and
        # a same-named sibling must not block it.
        return arriving.id == selection.device_id

    # Weak path: the id is an address, so it proves nothing. Exactly one known
    # device may carry the stored name, and the arrival must be that one.
    wanted = _normalize(selection.name)
    if not wanted:
        return False  # nothing to verify against; never a match-everything
    matches = [
        device for device in siblings
        if device.backend_type == selection.backend_type
        and _normalize(device.name) == wanted
    ]
    if len(matches) != 1:
        return False  # zero: not our device. two or more: ambiguous (AE4).
    return matches[0].id == arriving.id
