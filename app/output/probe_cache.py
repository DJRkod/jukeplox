"""Per-device, per-backend probe verdict cache.

The picker filters its Via dropdown to protocols that have been verified
to work on each device. The verdict — a (host, backend) → ok/fail bool
plus the timestamp it was checked — rides on the existing settings table
behind the ``device_protocol:`` prefix so no new schema is required.

Keys: ``device_protocol:{host}:{backend}``.
Values: JSON ``{"ok": <bool>, "checked_at": <float epoch seconds>}``.

The backend name is the **last** colon-delimited segment of the key
suffix — this keeps IPv6 hosts (which contain colons) addressable
without ambiguity, because backend names (``airplay``, ``chromecast``,
``dlna``, ``direct``) never contain colons themselves.

This module owns the prefix discipline so callers (the aggregator, the
admin discovery route) don't reach into settings keys directly.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

_PREFIX = "device_protocol:"


@dataclass(frozen=True)
class Verdict:
    """A single probe verdict.

    ``ok`` is the working-or-not answer the Via dropdown filters on.
    ``checked_at`` is when the probe completed; the frontend uses it to
    detect stuck probes (a verdict that's been "Checking…" too long
    flips to "Could not verify" in the picker).
    """
    ok: bool
    checked_at: float


def _key(host: str, backend: str) -> str:
    return f"{_PREFIX}{host}:{backend}"


def _parse(raw: str | None) -> Verdict | None:
    """Decode a stored verdict; return None on anything other than a
    well-formed payload with both required fields. Corrupted rows behave
    identically to missing rows — the caller re-probes rather than
    crashing on bad disk state."""
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    ok = data.get("ok")
    checked_at = data.get("checked_at")
    if not isinstance(ok, bool) or not isinstance(checked_at, (int, float)):
        return None
    return Verdict(ok=ok, checked_at=float(checked_at))


async def get_verdict(host: str, backend: str) -> Verdict | None:
    """Return the cached verdict for ``(host, backend)``, or None if no
    verdict is stored or the stored data is unparseable."""
    from app import database
    return _parse(await database.get_setting(_key(host, backend)))


async def set_verdict(host: str, backend: str, ok: bool) -> None:
    """Persist ``ok`` for ``(host, backend)`` with a fresh ``checked_at``
    timestamp. Overwrites any prior verdict for the same key."""
    from app import database
    payload = json.dumps({"ok": ok, "checked_at": time.time()})
    await database.set_setting(_key(host, backend), payload)


async def clear_verdicts_for_host(host: str) -> None:
    """Remove every verdict for ``host`` across all backends. Intended
    for the per-host reprobe path (deferred to a follow-up plan) and as
    a tighter alternative to ``clear_all_verdicts`` when only one
    device's state is suspect."""
    from app import database
    entries = await database.get_settings_with_prefix(_PREFIX)
    for suffix in entries:
        host_part, _, backend_part = suffix.rpartition(":")
        if not backend_part:
            # Malformed key — skip rather than mis-delete.
            continue
        if host_part == host:
            await database.delete_setting(f"{_PREFIX}{suffix}")


async def clear_all_verdicts() -> None:
    """Remove every ``device_protocol:*`` entry. Powers the ``bust=true``
    rescan path: a full rescan discards prior verdicts so re-discovered
    devices get fresh probes against the current network state."""
    from app import database
    entries = await database.get_settings_with_prefix(_PREFIX)
    for suffix in entries:
        await database.delete_setting(f"{_PREFIX}{suffix}")


async def fetch_all() -> dict[tuple[str, str], Verdict]:
    """Bulk-load every stored verdict as a ``{(host, backend): Verdict}``
    map. Used by the admin discovery route so it can feed the aggregator
    in one round-trip instead of probing the settings table once per
    (host, backend) pair. Corrupted rows are silently dropped — they
    behave like missing rows."""
    from app import database
    entries = await database.get_settings_with_prefix(_PREFIX)
    out: dict[tuple[str, str], Verdict] = {}
    for suffix, raw in entries.items():
        host_part, _, backend_part = suffix.rpartition(":")
        if not backend_part:
            continue
        verdict = _parse(raw)
        if verdict is not None:
            out[(host_part, backend_part)] = verdict
    return out
