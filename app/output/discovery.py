"""Per-host aggregation of per-backend discovery results.

The admin discovery API gathers ``OutputDevice`` lists from every output
backend. The picker, however, consumes one record per physical device —
deduplicated across protocols by IP/host — with the verified-protocol
set attached. This module owns that transformation as a pure function so
the admin route stays focused on I/O orchestration.

Core entry point: :func:`aggregate_devices` — no global state, no I/O,
no backend instances; the caller passes everything in.

2026-06-11 live-discovery plan U5 (KTD11): the snapshot builders also
live here — :func:`host_for` (lifted from admin.py's ``_host_for``),
:func:`build_devices_snapshot` (the GET-response serialization loop) and
:func:`build_registry_snapshot` (the same payload built from the device
watcher's registry). Both the admin route and the watcher's
``devices_changed`` broadcast call these, so push and pull share ONE
render path (KTD5). watcher → admin imports would invert layering
(admin.py pulls in auth, templates, guest at import time), hence the
output-side home. The builders may read app.state's backend singletons
and the probe cache (lazy, function-level) but never import app.api.*.

The display-name priority rule encoded here matches the brainstorm's
explicit decision: DLNA and Chromecast supply user-set friendly names
and tie for highest priority; AirPlay (post-MAC-strip) supplies a name
that often carries a model-serial suffix and is lower priority. When
both DLNA and Chromecast can supply a name, first-arrival wins so the
picker label is stable across discovery cycles.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

from app.output.base import OutputDevice
from app.output.probe_cache import Verdict

_log = logging.getLogger(__name__)

# (backend, device_id) → (online, offline_since). The watcher-sourced
# availability view build_devices_snapshot threads into the aggregator
# (KTD8). None means "no live view" — the legacy pull path — and every
# discovered device counts as online (today's semantics).
AvailabilityMap = dict[tuple[str, str], tuple[bool, float | None]]

# Sentinel host used for the Direct ("System Audio") pseudo-device. It is
# not network-bound and must never merge with any real host; the frontend
# treats this value as the "always available" pre-selection target.
DIRECT_HOST = "__direct__"

# Per-backend display-name priority. DLNA and Chromecast tie at the top
# (both expose user-set friendly names). AirPlay is lower because its
# post-strip name often carries a model-serial suffix the operator may
# not have chosen explicitly. The Direct backend gets its own slot so
# its sentinel name flows through without special-casing.
_NAME_PRIORITY = {
    "direct": 0,
    "airplay": 1,
    "chromecast": 2,
    "dlna": 2,
    # Plex Companion players report the user-set player name via the PMS
    # /clients list — friendly-name tier, same rank as DLNA/Chromecast
    # (first-arrival wins among equals, so a Caldera sharing a host with a
    # DLNA renderer keeps a stable label across cycles).
    "plexplayer": 2,
}

# Deterministic walk order across backends. Direct first so its sentinel
# entry seeds the result; otherwise alphabetical for predictability.
# plexplayer is appended last (2026-08-04-002 plan U3) so the pre-existing
# backends' walk order — and therefore their name seeding — is unchanged.
_BACKEND_ORDER = ("direct", "airplay", "chromecast", "dlna", "plexplayer")

# Per-protocol gapless capability (2026-07-11 supervisor plan U5, R10/R12).
# Static per-backend verdicts: Direct chains via GStreamer about-to-finish
# (U7) and Chromecast via the server-stitched flow stream (U9/U10) — both
# "supported"; AirPlay stays per-track (plan scope) — "unsupported"; DLNA's
# "unverified" is the DEFAULT for devices with no cached behavioral verdict:
# U8's lazy verification (SetNextAVTransportURI is advertised-but-ignored on
# some renderers) decides per device at the first armed boundary, and
# build_devices_snapshot overrides this map with the persisted per-device
# verdict. Serialized onto every protocol entry in build_devices_snapshot,
# so the picker chip and GET /admin/output/devices carry the same value on
# both the pull and push paths (KTD5).
GAPLESS_CAPABILITY = {
    "direct": "supported",
    "chromecast": "supported",
    "dlna": "unverified",
    "airplay": "unsupported",
    # Plex Companion players (2026-08-04-002 plan U3): DLNA semantics —
    # "unverified" is the no-evidence default, promoted per DEVICE by U7's
    # behavioral verdict after the first successful armed boundary
    # (PUT-append to a player-owned queue is a hardware-validation item).
    "plexplayer": "unverified",
    # Server-fed multi-room backends (2026-08-11 plan U5/U7): one continuous
    # server-stitched feed → gapless is a natural property of the transport.
    "snapcast": "supported",
    "sendspin": "supported",
}

# Backends whose gapless verdict is per-DEVICE (behavioral, cached in the
# settings store): the static map above is only their no-evidence default,
# overridden in build_devices_snapshot by database.get_gapless_verdicts.
_PER_DEVICE_GAPLESS_BACKENDS = ("dlna", "plexplayer")


@dataclass
class ProtocolEntry:
    """One backend's reachability for a single physical device.

    ``verified`` is the picker's filter signal:
      - ``True`` — probe passed; entry appears as a selectable Via option.
      - ``False`` — probe failed; entry is filtered out of the Via list.
      - ``None`` — no verdict yet; entry shows as "Checking…".

    ``checked_at`` carries the verdict's timestamp so the frontend can
    detect stuck probes (Checking… that's been running too long flips
    to "Could not verify"). ``None`` when no verdict has been recorded.
    """
    backend: str
    device_id: str
    verified: bool | None
    checked_at: float | None = None


@dataclass
class AggregatedDevice:
    """One physical device as the picker should render it.

    ``host`` is the dedupe key (or :data:`DIRECT_HOST` for System Audio).
    ``name`` is the highest-priority friendly name across protocols.
    ``protocols`` is the per-backend reachability list.

    ``online`` / ``offline_since`` carry the watcher's availability view
    (KTD8): a physical device is online while ANY contributing backend
    entry is online; fully offline, ``offline_since`` is the moment the
    LAST entry flipped (the device became unreachable on every protocol
    then). Without an availability map — the legacy pull path, and the
    Direct pseudo-device always — devices are online with no timestamp.
    """
    host: str
    name: str
    protocols: list[ProtocolEntry] = field(default_factory=list)
    online: bool = True
    offline_since: float | None = None
    # Internal: the priority rank of the backend that seeded ``name``.
    # Used during aggregation to decide whether a later arrival should
    # replace the current label. Not serialized to the frontend.
    _name_rank: int = -1


def _verdict_to_entry(
    backend: str,
    device_id: str,
    verdicts: dict[tuple[str, str], Verdict],
    host: str,
) -> ProtocolEntry:
    v = verdicts.get((host, backend))
    if v is None:
        return ProtocolEntry(backend=backend, device_id=device_id, verified=None)
    return ProtocolEntry(
        backend=backend,
        device_id=device_id,
        verified=v.ok,
        checked_at=v.checked_at,
    )


def aggregate_devices(
    per_backend: dict[str, list[OutputDevice]],
    host_for,
    verdicts: dict[tuple[str, str], Verdict],
    availability: AvailabilityMap | None = None,
) -> list[AggregatedDevice]:
    """Collapse the per-backend lists into a per-physical-device list.

    *per_backend* maps backend name → list of OutputDevice. Backends not
    in :data:`_BACKEND_ORDER` are walked last in arbitrary order, so a
    future backend integrates cleanly without changing the algorithm.

    *host_for* is a callable ``(d, backend) -> str | None`` that lifts
    the per-backend host extraction out of the aggregator. Caller-supplied
    so the aggregator stays backend-agnostic — :func:`host_for` below
    knows how to read ``_device_addr`` / ``_cast_infos`` /
    ``_device_locations``, not this function. Devices for which the
    callable returns ``None`` are dropped (no key to bucket on).

    *verdicts* is the ``{(host, backend): Verdict}`` map loaded from
    :mod:`app.output.probe_cache`. Missing entries → ``verified=None``.

    *availability* is the watcher-sourced :data:`AvailabilityMap` (KTD8);
    ``None`` (legacy pull) marks every device online. Direct is always
    online — it has no network surface to lose.

    Returns a list with Direct first (when present), then alphabetically
    by name.
    """
    buckets: dict[str, AggregatedDevice] = {}

    def _merge_availability(bucket: AggregatedDevice, backend: str, dev_id: str):
        """OR this entry's liveness into the bucket. A physical device is
        offline only when EVERY contributing entry is, and 'offline since'
        is then the latest flip — the moment the last protocol went."""
        if availability is None:
            online, off_since = True, None
        else:
            online, off_since = availability.get((backend, dev_id), (True, None))
        bucket.online = bucket.online or online
        if bucket.online:
            bucket.offline_since = None
        else:
            stamps = [t for t in (bucket.offline_since, off_since) if t is not None]
            bucket.offline_since = max(stamps) if stamps else None

    def _walk_order(names):
        seen = set(_BACKEND_ORDER)
        for n in _BACKEND_ORDER:
            if n in names:
                yield n
                seen.add(n)
        for n in names:
            if n not in seen:
                yield n

    for backend in _walk_order(per_backend.keys()):
        devices = per_backend.get(backend) or []
        rank = _NAME_PRIORITY.get(backend, 0)
        for d in devices:
            if backend == "direct":
                # Direct bypasses host-based dedupe — single sentinel
                # bucket regardless of input. Always online (KTD8).
                bucket = buckets.get(DIRECT_HOST)
                if bucket is None:
                    bucket = AggregatedDevice(
                        host=DIRECT_HOST, name=d.name, _name_rank=rank,
                    )
                    buckets[DIRECT_HOST] = bucket
                bucket.protocols.append(ProtocolEntry(
                    backend="direct", device_id=d.id, verified=True,
                ))
                continue

            host = host_for(d, backend)
            if not host:
                # No host → no key to bucket on. The admin route logs
                # this; the aggregator stays quiet to keep the test
                # surface free of log assertions.
                continue

            bucket = buckets.get(host)
            if bucket is None:
                # online=False seed: the first _merge_availability call
                # below decides — a default-True seed would mask an
                # all-offline device behind the OR.
                bucket = AggregatedDevice(host=host, name=d.name,
                                          online=False, _name_rank=rank)
                buckets[host] = bucket
            elif rank > bucket._name_rank:
                bucket.name = d.name
                bucket._name_rank = rank
            _merge_availability(bucket, backend, d.id)

            # Same-backend-twice at the same host: drop the duplicate so
            # the Via dropdown doesn't show two entries for one protocol.
            if any(p.backend == backend for p in bucket.protocols):
                continue
            bucket.protocols.append(_verdict_to_entry(backend, d.id, verdicts, host))

    # Direct first; remaining devices alphabetical by name.
    direct = [b for b in buckets.values() if b.host == DIRECT_HOST]
    others = sorted(
        (b for b in buckets.values() if b.host != DIRECT_HOST),
        key=lambda b: b.name.lower(),
    )
    return direct + others


# ── snapshot builders (U5, KTD11) ─────────────────────────────────────────────

def _state_backend(backend: str) -> Any:
    """Default backend lookup: the live app.state singletons. Lazy
    function-level import so this module stays importable without
    app.state (and the watcher's injected lookup can replace it in
    tests)."""
    from app import state
    inst = {
        "direct": state.direct_backend,
        "airplay": state.airplay_backend,
        "chromecast": state.chromecast_backend,
        "dlna": state.dlna_backend,
        "plexplayer": state.plexplayer_backend,
    }.get(backend)
    if inst is not None:
        return inst
    # Server-fed backends (2026-08-11 plan U5/U7) live in the lazy activation
    # cache, not a module global — None when the backend is dormant.
    if state.is_server_fed_backend(backend):
        return state.get_server_fed_backend(backend)
    return None


def host_for(d, backend: str, backend_for: Callable[[str], Any] | None = None,
             ) -> str | None:
    """Resolve the IP/host the aggregator uses as a dedupe key.

    Lifted from app/api/admin.py (KTD11) so the watcher's broadcast can
    use it without importing the route module. Each backend stores its
    addressing data in a different cache:
    - AirPlay: ``_device_addr[d.id] = (name, host, port, txt)``
    - Chromecast: ``_dbus_index[d.id] = (name, host, port)`` (D-Bus path)
                  or ``_cast_infos[d.id].host`` (live-browser path)
    - DLNA: ``_device_locations[d.id]`` is the LOCATION URL; the host
            comes from urlparse.
    - PlexPlayer: the backend exposes ``device_host(d.id)`` over its
            ``_device_addresses`` cache (fed by the /clients sweep and the
            persisted-address re-bind).

    *backend_for* maps a backend name to its instance; defaults to the
    app.state singletons. Returns ``None`` when the backend doesn't carry
    the device — the aggregator drops those rather than bucketing under
    a junk key.
    """
    backend_inst = (backend_for or _state_backend)(backend)
    if backend_inst is None:
        return None
    if backend == "airplay":
        info = backend_inst._device_addr.get(d.id)
        return info[1] if info else None
    if backend == "chromecast":
        with backend_inst._discover_lock:
            dbus_info = backend_inst._dbus_index.get(d.id)
            cast_info = backend_inst._cast_infos.get(d.id)
        if dbus_info is not None:
            return dbus_info[1]
        if cast_info is not None:
            return cast_info.host
        return None
    if backend == "dlna":
        location = backend_inst._device_locations.get(d.id)
        if not location:
            return None
        parsed = urlparse(location)
        return parsed.hostname
    if backend == "plexplayer":
        getter = getattr(backend_inst, "device_host", None)
        return getter(d.id) if callable(getter) else None
    return None


async def build_devices_snapshot(
    per_backend: dict[str, list[OutputDevice]],
    *,
    availability: AvailabilityMap | None = None,
    backend_for: Callable[[str], Any] | None = None,
) -> tuple[list[dict], list[AggregatedDevice]]:
    """Aggregate + serialize the GET /admin/output/devices ``devices``
    payload (the loop lifted from admin.py per KTD11).

    Loads the verdict map from the probe cache (fail-soft — a cache
    error means the aggregator runs unverified, exactly the route's old
    behavior), aggregates with the lifted :func:`host_for`, and returns
    ``(payload, aggregated)``: the JSON-ready dict list every render
    path shares (KTD5), plus the AggregatedDevice list the route feeds
    to probe_runner.schedule_probes (the watcher's broadcast ignores it).
    """
    from app.output import probe_cache
    try:
        verdicts = await probe_cache.fetch_all()
    except Exception:
        _log.warning("Probe cache bulk-load failed; aggregator runs unverified",
                     exc_info=True)
        verdicts = {}

    # Per-device gapless verdicts (supervisor plan U8; plexplayer joins in
    # 2026-08-04-002 U3 with the same DLNA semantics): the static map's
    # "unverified" is only the no-evidence default — a device whose first
    # armed boundary decided a behavioral verdict carries that instead.
    # Bulk-loaded from the settings store exactly like the probe cache above
    # (this builder is already async and already does that per-snapshot DB
    # read), which stays correct across restarts and for devices never
    # selected in this process — an in-memory-only read couldn't. Fail-soft:
    # on error the static default stands.
    device_gapless: dict[str, dict[str, str]] = {}
    try:
        from app import database
        for _backend_name in _PER_DEVICE_GAPLESS_BACKENDS:
            device_gapless[_backend_name] = (
                await database.get_gapless_verdicts(_backend_name))
    except Exception:
        _log.warning("Gapless verdict bulk-load failed; DLNA/plexplayer "
                     "entries read the static capability", exc_info=True)
        device_gapless = {}

    aggregated = aggregate_devices(
        per_backend,
        lambda d, b: host_for(d, b, backend_for),
        verdicts,
        availability=availability,
    )

    # Serialize: AggregatedDevice -> dict; drop the internal _name_rank.
    payload = []
    for dev in aggregated:
        payload.append({
            "host": dev.host,
            "name": dev.name,
            "online": dev.online,
            "offline_since": dev.offline_since,
            "protocols": [
                {
                    "backend": p.backend,
                    "device_id": p.device_id,
                    "verified": p.verified,
                    "checked_at": p.checked_at,
                    # Gapless capability chip (plan U5) — per backend type; an
                    # unknown future backend defaults to "unsupported" (honest
                    # until it earns a verdict). DLNA and plexplayer are per
                    # DEVICE (plan U8 / 2026-08-04-002 U3): the cached
                    # behavioral verdict overrides the static "unverified"
                    # default.
                    "gapless": (
                        device_gapless.get(p.backend, {}).get(
                            p.device_id, GAPLESS_CAPABILITY[p.backend])
                        if p.backend in _PER_DEVICE_GAPLESS_BACKENDS
                        else GAPLESS_CAPABILITY.get(p.backend, "unsupported")
                    ),
                }
                for p in dev.protocols
            ],
        })
    return payload, aggregated


async def build_registry_snapshot(
    registry: dict,
    *,
    backend_for: Callable[[str], Any] | None = None,
) -> tuple[list[dict], list[AggregatedDevice]]:
    """The devices payload built from the watcher's registry (KTD7 fast
    path + the watcher's broadcast snapshot).

    *registry* is the watcher's ``(backend, device_id) → RegistryEntry``
    dict — duck-typed (entries need ``.device``/``.online``/
    ``.offline_since``) so this module never imports the watcher. The
    Direct pseudo-device is appended via its backend's discover (local
    GStreamer sink enumeration — no network I/O), exactly as the pull
    flow does; a Direct failure is fail-soft (snapshot just lacks it).
    """
    per_backend: dict[str, list[OutputDevice]] = {}
    availability: AvailabilityMap = {}
    for (backend, device_id), entry in registry.items():
        per_backend.setdefault(backend, []).append(entry.device)
        availability[(backend, device_id)] = (entry.online, entry.offline_since)

    direct_backend = (backend_for or _state_backend)("direct")
    if direct_backend is not None:
        try:
            per_backend["direct"] = await direct_backend.discover_devices()
        except Exception:
            _log.warning("Registry snapshot: Direct discover failed",
                         exc_info=True)

    return await build_devices_snapshot(
        per_backend, availability=availability, backend_for=backend_for)
