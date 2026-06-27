"""Background probe scheduling for discovered output devices.

2026-06-11 live-discovery plan U4 (KTD6): the scheduling helper and its
semaphore, lifted verbatim from app/api/admin.py so the device watcher
can probe hosts on arrival / offline→online without importing the admin
route module (watcher → admin would invert layering — admin.py pulls in
auth, templates and guest at import time). admin.py imports the helper
back from here; route behavior is unchanged, and the concurrency cap now
bounds watcher- and route-triggered probes together via the one shared
semaphore.

Probes only write Via-level verdicts (probe_cache); they never touch the
watcher's registry — a failed probe greys a Via option, it does not make
a device vanish.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.output import probe_cache
from app.output.discovery import DIRECT_HOST, AggregatedDevice, ProtocolEntry

_log = logging.getLogger(__name__)

# Bound on how many background probes can run concurrently across the
# whole app (admin route + device watcher). Probes are HTTP/RTSP against
# known host:port pairs (they do NOT re-bind 5353), so the cap exists
# purely to avoid stampeding the local network when a large device set
# is freshly discovered.
_PROBE_CONCURRENCY = 6
_probe_semaphore: asyncio.Semaphore | None = None


def _get_probe_semaphore() -> asyncio.Semaphore:
    """Lazy-init so the semaphore lives on whatever event loop is active
    when discovery first runs. Tests can swap it without import-time
    coupling."""
    global _probe_semaphore
    if _probe_semaphore is None:
        _probe_semaphore = asyncio.Semaphore(_PROBE_CONCURRENCY)
    return _probe_semaphore


def _default_backends() -> dict[str, Any]:
    """Backend map built lazily from app.state at call time (KTD6: the
    function-level import keeps probe_runner importable without dragging
    in app.state — and thus without a cycle) so callers that don't
    already hold the map, i.e. the watcher, never construct it."""
    from app import state
    return {
        "direct": state.direct_backend,
        "chromecast": state.chromecast_backend,
        "dlna": state.dlna_backend,
        "airplay": state.airplay_backend,
    }


async def schedule_probes(
    devices, backends: dict[str, Any] | None = None, *, force: bool = False,
) -> None:
    """Fire-and-forget background probe tasks for (host, backend) entries.

    When ``force`` is False (default page-load / TTL refresh), only entries
    with no verdict (``verified is None``) are probed — verified entries
    stay cached.

    When ``force`` is True (``bust=true`` rescan), every entry is probed
    regardless of current verdict; new probes overwrite prior verdicts as
    they complete. Crucially we do NOT clear the cache first — operators
    keep seeing the prior verified state during the rescan window, which
    is strictly better UX than a guaranteed empty state.

    *backends* defaults to the live app.state map (:func:`_default_backends`);
    the admin route keeps passing the map it already built.

    Bounded by ``_probe_semaphore`` so a freshly-rescanned network with
    many devices doesn't stampede the LAN with concurrent connects."""
    if backends is None:
        backends = _default_backends()
    semaphore = _get_probe_semaphore()

    async def _one(host: str, backend_name: str, device_id: str):
        backend = backends.get(backend_name)
        if backend is None:
            return
        async with semaphore:
            try:
                ok = await backend.probe_device(device_id)
            except Exception:
                _log.warning(
                    "Probe scheduling: backend %s raised for %r",
                    backend_name, device_id, exc_info=True,
                )
                ok = False
            try:
                await probe_cache.set_verdict(host, backend_name, ok)
            except Exception:
                _log.warning(
                    "Probe scheduling: cache write failed for %s/%s",
                    host, backend_name, exc_info=True,
                )

    for dev in devices:
        if dev.host == DIRECT_HOST:
            continue
        for entry in dev.protocols:
            if force or entry.verified is None:
                task = asyncio.create_task(_one(dev.host, entry.backend, entry.device_id))
                # Retrieve any exception so asyncio doesn't warn at GC.
                task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)


def probe_host(host: str, backend: str, device_id: str) -> None:
    """The watcher's probe trigger (U4): fire-and-forget probe of ONE
    (host, backend, device_id) entry on arrival / offline→online.

    Wraps the entry in the AggregatedDevice shape :func:`schedule_probes`
    consumes. The constructed entry carries no verdict (``verified=None``)
    so it is always probed — a returning device's stale cached verdict is
    re-verified hands-off (R4) and overwritten on completion, exactly like
    the route's ``bust=true`` path.

    Sync by design: the watcher calls it from loop-threadsafe callbacks
    that cannot await; the actual probing rides the task + shared
    semaphore like every route-triggered probe.
    """
    dev = AggregatedDevice(host=host, name="", protocols=[
        ProtocolEntry(backend=backend, device_id=device_id, verified=None),
    ])
    task = asyncio.create_task(schedule_probes([dev]))
    # Retrieve any exception so asyncio doesn't warn at GC.
    task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
