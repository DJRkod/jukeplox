"""Admin-facing API routes. All routes require a valid admin session."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from typing import Annotated, Literal

_log = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator

from app.api.auth_routes import require_admin, SESSION_COOKIE
from app.api.guest import enabled_libraries, validate_plex_id, _resolve_album_tracks
from app import database, state
from app.auth import plex_oauth
# Snapshot building lives output-side since the live-discovery plan U5
# (KTD11) — the device watcher's broadcast shares the exact serialization
# without importing this module. Probe scheduling moved likewise (U4/KTD6).
from app.output.discovery import build_devices_snapshot, build_registry_snapshot
from app.output.probe_runner import schedule_probes
from app.queue.models import QueueEndBehavior

_templates = Jinja2Templates(directory="app/templates")
from app import assets as _assets
_assets.register(_templates)  # `asset_v` global → build-derived cache-buster

# ── Page routes (no auth guard on router level) ───────────────────────────────

page_router = APIRouter(tags=["admin-pages"])


@page_router.get("/admin/login", response_class=HTMLResponse, include_in_schema=False)
async def admin_login_page(request: Request):
    from app import database
    setup_required = not bool(await database.get_setting("setup_complete"))
    return _templates.TemplateResponse(request, "admin/login.html", {"setup_required": setup_required})


@page_router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
async def admin_dashboard_page(request: Request):
    from app.auth import session as session_mgr
    token = request.cookies.get(SESSION_COOKIE)
    if not token or not await session_mgr.validate_session(token):
        return RedirectResponse(url="/admin/login")
    return _templates.TemplateResponse(request, "admin/dashboard.html")

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])

# The WebSocket route can't carry the Depends(...) on the router level the same
# way (WS doesn't send cookies in all browsers), so auth is checked manually there.


# ── Plex library config ───────────────────────────────────────────────────────

def _serialize_libraries(libraries: list, enabled_keys: set) -> list:
    return [
        {
            "key": lib.key,
            "title": lib.title,
            "type": lib.type,
            "enabled": lib.key in enabled_keys,
            "owner": lib.owner,
            # Per-source-type indicator for the Libraries list (ce-debug
            # 2026-06-29): the frontend renders "(Plex — <name>)" / "(Jellyfin —
            # <name>)" so same-named libraries across source types are
            # distinguishable. server_name is the Jellyfin disambiguator (it
            # carries no owner).
            "source_type": getattr(lib, "source_type", "") or "",
            "server_name": getattr(lib, "server_name", "") or "",
        }
        for lib in libraries
    ]


@router.get("/plex/libraries")
async def list_libraries():
    client = await state.get_plex_client()
    if not client:
        raise HTTPException(status_code=503, detail="No media source configured")
    libraries = await client.get_libraries()
    enabled_keys = {lib["section_key"] for lib in await database.get_enabled_libraries()}
    return _serialize_libraries(libraries, enabled_keys)


@router.post("/plex/libraries/{key}/enable")
async def enable_library(key: str):
    client = await state.get_plex_client()
    if not client:
        raise HTTPException(status_code=503, detail="No media source configured")
    libraries = await client.get_libraries()
    lib = next((l for l in libraries if l.key == key), None)
    if not lib:
        raise HTTPException(status_code=404, detail="Library not found")
    await database.toggle_library(key, lib.title, enabled=True)
    client.invalidate_cache()
    await state.invalidate_ondeck()  # enabled-library set changed (plan U4)
    state.trigger_browse_index_refresh()  # browse-index plan U6/R9
    state.trigger_catalog_refresh()  # multi-source catalog (plan U6)
    return {"ok": True}


@router.post("/plex/libraries/{key}/disable")
async def disable_library(key: str):
    await database.toggle_library(key, "", enabled=False)
    client = await state.get_plex_client()
    if client:
        client.invalidate_cache()
    await state.invalidate_ondeck()  # enabled-library set changed (plan U4)
    state.trigger_browse_index_refresh()  # browse-index plan U6/R9
    state.trigger_catalog_refresh()  # multi-source catalog (plan U6)
    return {"ok": True}


@router.post("/plex/rescan")
async def plex_rescan():
    """Invalidate in-memory Plex caches and return the refreshed library list."""
    client = await state.get_plex_client()
    if not client:
        raise HTTPException(status_code=503, detail="No media source configured")
    client.invalidate_cache()
    state.trigger_genre_refresh()
    state.trigger_credit_refresh()
    state.trigger_browse_index_refresh()  # browse-index plan U6/R7
    state.trigger_catalog_refresh()  # multi-source catalog (plan U6)
    libraries = await client.get_libraries()
    enabled_keys = {lib["section_key"] for lib in await database.get_enabled_libraries()}
    return _serialize_libraries(libraries, enabled_keys)


class SourcePriorityRequest(BaseModel):
    order: list[str] = Field(default_factory=list, max_length=64)


@router.get("/sources/priority")
async def get_source_priority():
    """The global source-priority order (highest first) — multi-source plan U9/R12."""
    return {"order": await database.get_source_priority()}


@router.post("/sources/priority")
async def set_source_priority(body: SourcePriorityRequest):
    """Persist the global source-priority order. Effective at the NEXT enqueue's
    holds snapshot and stream resolution — no rescan needed (R12). Admin-gated by
    the router-level require_admin (R26). The drag-reorder UI is U14."""
    await database.set_source_priority(body.order)
    return {"ok": True, "order": body.order}


# ── Multi-source Sources panel (plan U14) ─────────────────────────────────────

class JellyfinConnectRequest(BaseModel):
    server_url: str = Field(min_length=1, max_length=512)
    username: str = Field(min_length=1, max_length=256)
    password: str = Field(default="", max_length=512)
    name: str = Field(default="", max_length=128)


class LocalConnectRequest(BaseModel):
    root_dir: str = Field(min_length=1, max_length=4096)
    name: str = Field(default="", max_length=128)


def _hostname(url: str) -> str:
    from urllib.parse import urlparse
    try:
        return urlparse(url if "://" in url else f"http://{url}").hostname or ""
    except Exception:
        return ""


def _source_error(category: str, message: str) -> HTTPException:
    # R21: a categorized, legible failure; the source is NOT saved. The frontend
    # surfaces ``category`` (unreachable / auth_rejected) inline.
    return HTTPException(status_code=400, detail={"category": category, "message": message})


@router.get("/sources")
async def list_sources():
    """Connected media sources for the Sources panel + the priority list (U14).

    Each entry is ``{source_id, type, name}``. Plex servers come from
    ``plex_servers`` (or the legacy single-server config); Jellyfin from
    ``jellyfin_sources``; local-files directories from ``local_sources`` (U11)."""
    out: list[dict] = []
    for s in await database.get_plex_servers():
        out.append({"source_id": s["machine_id"], "type": "plex",
                    "name": s.get("name") or "Plex"})
    if not out:
        cfg = await database.get_plex_config()
        if cfg:
            out.append({"source_id": "", "type": "plex", "name": "Plex"})
    for j in await database.get_jellyfin_sources():
        out.append({"source_id": j["source_id"], "type": "jellyfin",
                    "name": j.get("name") or "Jellyfin"})
    for l in await database.get_local_sources():
        out.append({"source_id": l["source_id"], "type": "local",
                    "name": l.get("name") or "Local"})
    return {"sources": out}


@router.get("/scan-status")
async def scan_status():
    """Catalog scan state for the admin Sources scan badge (plan U15/R20):
    ``{sources, scanning, scanned, empty}`` — same snapshot the guest onboarding
    states read, so admin and guest agree on one source of truth. Admin-gated by
    the router-level require_admin (R26)."""
    return await state.scan_status()


@router.post("/sources/jellyfin")
async def connect_jellyfin(body: JellyfinConnectRequest):
    """Connect a Jellyfin source by account sign-in (R5). Validated by signing in;
    on success the credential is saved TOKEN-ONLY (R24) and a catalog scan kicks
    off. A bad URL/credentials surfaces an inline-categorized error and the source
    is NOT saved (R21). Admin-gated by the router-level require_admin (R26)."""
    from app.sources import jellyfin as jf
    device_id = jf.new_device_id()
    try:
        creds = await jf.authenticate(
            body.server_url, body.username, body.password, device_id=device_id)
    except jf.JellyfinAuthError as e:
        raise _source_error("auth_rejected", str(e) or "Jellyfin rejected the credentials")
    except Exception:
        raise _source_error("unreachable", "Could not reach the Jellyfin server")
    # Stable id across reconnects: prefer the server's id, else the device id.
    source_id = f"jf-{creds.get('server_id') or device_id}"
    name = body.name.strip() or _hostname(body.server_url) or "Jellyfin"
    await database.save_jellyfin_source(
        source_id=source_id, server_url=body.server_url.rstrip("/"), name=name,
        token=creds["token"], user_id=creds["user_id"], device_id=device_id)
    state.invalidate_plex_client()   # rebuild the registry with the new source
    state.trigger_catalog_refresh()  # crawl it into the unified catalog
    return {"ok": True, "source_id": source_id, "name": name, "type": "jellyfin"}


@router.delete("/sources/jellyfin/{source_id}")
async def remove_jellyfin(source_id: str):
    """Remove a Jellyfin source and re-resolve the registry/catalog (U14/R7)."""
    await database.delete_jellyfin_source(source_id)
    state.invalidate_plex_client()
    state.trigger_catalog_refresh()
    return {"ok": True}


@router.post("/sources/local")
async def connect_local(body: LocalConnectRequest):
    """Connect a local-files source by directory path (R6). Validated by checking
    the directory exists and is readable; on success it is saved and a catalog
    scan kicks off. A missing/unreadable directory surfaces an inline-categorized
    error and the source is NOT saved (R21). Admin-gated by require_admin (R26).

    The realpath is stored (canonical root for LocalSource's containment) and a
    stable source_id is derived from it, so reconnecting the same directory
    updates the existing source rather than duplicating it."""
    import hashlib
    import os
    real = os.path.realpath(body.root_dir.strip())
    if not os.path.isdir(real):
        raise _source_error("dir_not_found", "That folder does not exist")
    if not os.access(real, os.R_OK):
        raise _source_error("unreadable", "That folder is not readable")
    source_id = f"local-{hashlib.sha1(real.encode('utf-8')).hexdigest()[:12]}"
    name = body.name.strip() or os.path.basename(real.rstrip("/\\")) or "Local Music"
    await database.save_local_source(source_id=source_id, name=name, root_dir=real)
    state.invalidate_plex_client()   # rebuild the registry with the new source
    state.trigger_catalog_refresh()  # crawl it into the unified catalog
    return {"ok": True, "source_id": source_id, "name": name, "type": "local"}


@router.delete("/sources/local/{source_id}")
async def remove_local(source_id: str):
    """Remove a local-files source and re-resolve the registry/catalog (R7)."""
    await database.delete_local_source(source_id)
    state.invalidate_plex_client()
    state.trigger_catalog_refresh()
    return {"ok": True}


@router.post("/sources/rescan")
async def rescan_sources():
    """Re-crawl every connected source into the catalog (U14/R7). Invalidates
    in-memory caches and triggers the catalog (+ Plex browse-index) refresh;
    effective without a restart."""
    client = await state.get_plex_client()
    if client:
        client.invalidate_cache()
    state.trigger_browse_index_refresh()
    state.trigger_catalog_refresh()
    return {"ok": True}


@router.get("/plex/index-status")
async def plex_index_status():
    """Browse-index freshness for the admin UI (plan U6/R7): the last-built
    timestamp (ISO string or null if never built) and whether a crawl is
    currently in flight."""
    return {
        "computed_at": await database.get_setting("browse_index_computed_at"),
        "building": state.browse_index_building(),
    }


# ── Plex connect (in-dashboard) ───────────────────────────────────────────────

@router.get("/plex/connect/pin")
async def plex_connect_pin():
    return await plex_oauth.start_flow()


@router.get("/plex/connect/poll/{pin_id}")
async def plex_connect_poll(pin_id: int, client_id: str):
    resolved = await plex_oauth.complete_flow(pin_id, client_id)
    return {"resolved": resolved}


# ── Output configuration ──────────────────────────────────────────────────────

# Per-backend mDNS status surfaced on every discovery response. "ok" means
# the backend's scan completed without raising; "unavailable" means it
# raised (the partial-coverage banner in the picker calls this out). Direct
# is always "ok" — it has no network surface to fail.
_MdnsStatus = dict[str, Literal["ok", "unavailable"]]


async def _forced_one_shots(watcher) -> tuple[dict, _MdnsStatus]:
    """Scan's one-shot discovers while the watcher runs (KTD7 force path).

    Returns ``(found, scan_status)``: the per-backend results to feed
    ``watcher.reconcile`` and the per-backend scan health. The route is a
    thin reader — all registry mutation happens inside reconcile.

    Two rules keep a Scan from orphaning live state:

    - Chromecast reconciles from the live CastBrowser snapshot
      (``snapshot_devices()``, uuid-keyed), never a one-shot D-Bus browse.
      Both the continuous subscription and the snapshot key by ``str(uuid)``,
      so a Scan racing a live arrival of the same device yields one registry
      entry, not two (plan U7 uuid-key consistency).
    - AE4: a backend is included in ``found`` (and can therefore evict
      its offline ghosts) only when its scan actually saw the network.
      Backend missing / discover raised → "unavailable", omitted. For the
      mDNS backends a degraded in-process source makes the one-shot return []
      WITHOUT raising (mdns_zeroconf.discover/snapshot → empty), so we
      additionally gate them on the watcher's live mdns_status — discovery
      down means the Scan still reconciles DLNA but never touches mDNS ghosts.
    """
    live_status = watcher.mdns_status()
    found: dict = {}
    scan_status: _MdnsStatus = {}

    async def _one(name: str, discover) -> None:
        if discover is None:
            scan_status[name] = "unavailable"
            return
        try:
            results = await discover()
            _log.info("Scan [%s]: %d device(s) found", name, len(results))
        except Exception:
            _log.exception("Scan [%s]: exception during forced discover", name)
            scan_status[name] = "unavailable"
            return
        scan_status[name] = "ok"
        if name in ("airplay", "chromecast") and live_status.get(name) != "ok":
            return  # avahi outage: an empty one-shot is not evidence (AE4)
        found[name] = results

    # Both mDNS one-shots are in-process now (plan U7): AirPlay forces a fresh
    # zeroconf browse; Chromecast reads the continuous CastBrowser snapshot
    # (uuid-keyed). Sequential keeps the LAN multicast burst gentle.
    async def _mdns():
        ap = state.airplay_backend
        cc = state.chromecast_backend
        await _one("airplay", ap.discover_devices if ap else None)

        # Chromecast: when 5353 is bound, reconcile from the live CastBrowser
        # snapshot (uuid-keyed). When it's not (host avahi owns it), use the
        # avahi/D-Bus one-shot — uuid-keyed too, so keys stay consistent.
        if cc is None:
            cast_source = None
        elif state._mdns_port_unavailable:
            cast_source = cc._dbus_discover
        else:
            async def _cast_snapshot():
                return cc.snapshot_devices()
            cast_source = _cast_snapshot
        await _one("chromecast", cast_source)

    dl = state.dlna_backend
    await asyncio.gather(
        _one("dlna", dl.discover_devices if dl else None),
        _mdns(),
        return_exceptions=True,
    )
    return found, scan_status


@router.get("/output/devices")
async def list_output_devices(bust: bool = Query(False)):
    # KTD7: while the watcher runs, GET serves the live registry snapshot
    # — no network I/O, no HTTP cache (the old 30s TTL globals are gone;
    # the registry IS the cache, kept fresh by subscriptions + sweep).
    from app.output.watcher import get_watcher
    watcher = get_watcher()
    if watcher is not None and watcher.running:
        scan_status: _MdnsStatus = {}
        if bust:
            # Scan = forced reconcile (origin R6): one-shot discovers,
            # merge into the registry (upsert found / drop still-absent
            # OFFLINE ghosts — the only eviction path), then re-probe.
            found, scan_status = await _forced_one_shots(watcher)
            watcher.reconcile(found)
        payload, aggregated = await build_registry_snapshot(watcher.registry)
        # Probe refresh, same semantics as the pull flow: default cycles
        # probe only unverified entries; Scan re-probes everything (new
        # probes overwrite prior verdicts as they complete — never clear
        # first, see the legacy branch's comment).
        asyncio.create_task(schedule_probes(aggregated, force=bust))
        # mdns_status: the watcher's live view, overlaid with any scan
        # failure so a Scan that hit a broken backend still surfaces the
        # partial-coverage banner.
        mdns_status = watcher.mdns_status()
        for name, status_value in scan_status.items():
            if status_value == "unavailable":
                mdns_status[name] = "unavailable"
        return {"devices": payload, "mdns_status": mdns_status}

    # Watcher absent / degraded (no avahi) → the legacy pull flow,
    # unchanged (origin R5): every GET runs the one-shot discovers.
    #
    # bust=true RE-PROBES every (host, backend) entry — but does NOT
    # clear the verdict cache first. The probes overwrite via set_verdict
    # as they complete; the operator keeps seeing the prior verified
    # state during the rescan window instead of a guaranteed empty state.
    # Clearing-then-rescanning was the original design but produced a
    # stuck-on-"Checking…" loop because the response is computed before
    # the async probes finish writing. See ce-debug session 2026-06-08.
    backends = {
        "direct": state.direct_backend,
        "chromecast": state.chromecast_backend,
        "dlna": state.dlna_backend,
        "airplay": state.airplay_backend,
    }
    results: dict = {name: [] for name in backends}
    mdns_status: _MdnsStatus = {name: "ok" for name in backends}

    async def _discover(name, backend):
        if not backend:
            mdns_status[name] = "unavailable"
            return
        try:
            found = await backend.discover_devices()
            _log.info("Discovery [%s]: %d device(s) found", name, len(found))
            results[name] = found
        except Exception:
            _log.exception("Discovery [%s]: exception during scan", name)
            results[name] = []
            mdns_status[name] = "unavailable"

    # AirPlay and Chromecast both bind UDP 5353 for mDNS; run them sequentially
    # (via _discover_mdns) so the shared Zeroconf socket is reused rather than
    # raced. Direct and DLNA (ports 0 / 1900) are safe to run concurrently.
    async def _discover_mdns():
        await _discover("airplay", state.airplay_backend)
        await _discover("chromecast", state.chromecast_backend)

    await asyncio.gather(
        _discover("direct", state.direct_backend),
        _discover("dlna", state.dlna_backend),
        _discover_mdns(),
        return_exceptions=True,
    )

    # Aggregate + serialize through the shared builder (KTD11). No
    # availability map on the pull path: every discovered device is
    # online (KTD8 legacy semantics).
    payload, aggregated = await build_devices_snapshot(results)

    # Schedule background probes. Default cycles probe only unverified
    # entries; bust=true re-probes everything so a manual rescan refreshes
    # cached verdicts (new probes overwrite the prior cache entries).
    asyncio.create_task(schedule_probes(aggregated, backends, force=bust))

    # Degraded-state signal on this (watcher-absent / degraded) path: discovery
    # is available if EITHER the in-process source bound 5353 (shared_aiozc) OR
    # the avahi/D-Bus fallback is reachable. Only when neither holds does the
    # admin banner show actionable guidance instead of a silent empty menu.
    mdns_status["discovery"] = "ok" if await _discovery_available() else "unavailable"

    return {"devices": payload, "mdns_status": mdns_status}


class SetOutputRequest(BaseModel):
    backend_type: Literal['direct', 'chromecast', 'dlna', 'airplay']
    device_id: str = "default"
    host: str | None = None

    @field_validator("device_id")
    @classmethod
    def validate_device_id(cls, v: str) -> str:
        if len(v) > 128:
            raise ValueError("device_id must be 128 characters or fewer")
        return v

    @field_validator("host")
    @classmethod
    def validate_host(cls, v: str | None) -> str | None:
        if v is not None and len(v) > 128:
            raise ValueError("host must be 128 characters or fewer")
        return v


@router.get("/output/active")
async def get_output_active():
    from app import database
    backend_type = await database.get_setting("output_backend_type") or "direct"
    device_id = await database.get_setting("output_device_id") or "default"
    host = await database.get_setting("output_host")
    via = await database.get_setting(f"device_via:{host}") if host else None
    return {
        "backend_type": backend_type,
        "device_id": device_id,
        "host": host,
        "via": via,
        "mdns_status": await _current_mdns_status(),
    }


async def _current_mdns_status() -> _MdnsStatus:
    """Return the current per-backend availability map.

    While the watcher runs, its live mdns_status is authoritative (U5 —
    the old answer read the deleted route cache). Otherwise default to
    all-``ok`` — the picker will refresh on its own GET /output/devices
    and show real status moments later (legacy semantics).
    """
    from app.output.watcher import get_watcher
    watcher = get_watcher()
    if watcher is not None and watcher.running:
        return watcher.mdns_status()
    status = {name: "ok" for name in ("direct", "airplay", "chromecast", "dlna")}
    status["discovery"] = "ok" if await _discovery_available() else "unavailable"
    return status


async def _discovery_available() -> bool:
    """True when a Cast/AirPlay discovery source is reachable on the
    watcher-absent / degraded path: the in-process source bound 5353
    (state.shared_aiozc) OR a browsable D-Bus daemon is reachable. When the
    watcher is running this is moot — its mdns_status() carries the live
    ``discovery`` key, derived from in-process handles or the D-Bus sweep."""
    if state.shared_aiozc is not None:
        return True
    try:
        from app.output import mdns_dbus
        return await mdns_dbus.dbus_discovery_available()
    except Exception:
        return False


@router.post("/output/active")
async def set_output_active(body: SetOutputRequest):
    try:
        await state.activate_backend(body.backend_type, body.device_id, host=body.host)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    from app.events.bus import manager
    from app.events.types import OutputChangedEvent
    await manager.broadcast_to_admins(OutputChangedEvent(
        backend_type=body.backend_type,
        device_name=body.device_id,
    ))
    return {
        "ok": True,
        "backend_type": body.backend_type,
        "device_id": body.device_id,
        "host": body.host,
    }


# ── Queue management ──────────────────────────────────────────────────────────

class AdminQueueAppendRequest(BaseModel):
    track_id: str | None = None
    album_id: str | None = None
    # Optional library filter for the album branch. Mirrors the field on
    # QueueAppendRequest in app/api/guest.py. Ignored for the track branch
    # (track_id already identifies a single library's track).
    source_server_name: str | None = None


@router.post("/queue")
async def admin_append_to_queue(body: AdminQueueAppendRequest):
    """Add track or album to the queue, bypassing the lock (admin privilege)."""
    if not body.track_id and not body.album_id:
        raise HTTPException(status_code=400, detail="Provide track_id or album_id")
    if body.track_id:
        validate_plex_id(body.track_id)
    if body.album_id:
        validate_plex_id(body.album_id)
    client = await state.get_plex_client()
    if not client:
        raise HTTPException(status_code=503, detail="No media source configured")
    q = state.queue_engine
    if body.track_id:
        track = await client.get_track(body.track_id)
        item = await q.append(track, bypass_lock=True)
        # Undo receipt (collected-library plan U5): same shape as the guest
        # endpoint so the SHARED row-tap handler gets parity on both pages;
        # admin redeems it via the same public POST /api/queue/undo.
        return {"ok": True, "tracks_added": 1,
                "entry": {"track_id": item.track_id, "added_at": item.added_at}}
    # Album — name-resolve across enabled libraries (optionally filtered by
    # source_server_name) so cross-server shared albums queue correctly.
    # Shares the helper with /api/queue so a future endpoint divergence here
    # is caught by tests/test_api_admin.py::test_guest_admin_queue_endpoints_parity.
    try:
        tracks = await _resolve_album_tracks(
            client, body.album_id or "", source_server_name=body.source_server_name,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Album not found")
    if not tracks:
        raise HTTPException(status_code=404, detail="Album not found or no tracks")
    # All-or-nothing batch append: validate full batch under one lock so a
    # partial album never lands when the queue is at the cap.
    await q.append_many(tracks, bypass_lock=True)
    return {"ok": True, "tracks_added": len(tracks)}


@router.get("/queue")
async def get_queue():
    q = state.queue_engine
    return {
        "queue": [{**_queue_item_dict(i), "position": idx} for idx, i in enumerate(q.queue)],
        "history": [_queue_item_dict(i) for i in q.history],
        "is_locked": q.is_locked,
        "current": _queue_item_dict(q.state.current) if q.state.current else None,
        "is_playing": q.state.is_playing,
        "is_paused": q.state.is_paused,
        # Closing Time (2026-06-24 plan U3): admin hydrates the banner from here.
        "closing_active": state._closing_active,
        "closing_message": state._closing_message,
    }


@router.get("/playback/now-playing")
async def admin_now_playing():
    s = state.queue_engine.state
    if not s.current:
        return {
            "is_playing": False, "is_paused": False,
            "closing_active": state._closing_active,
            "closing_message": state._closing_message,
        }
    t = s.current.track
    return {
        "track_id": t.id,
        "title": t.title,
        "artist": t.artist,
        "album": t.album,
        "thumb": t.thumb,
        "duration_ms": t.duration_ms,
        "server_name": t.server_name,
        "is_playing": s.is_playing,
        "is_paused": s.is_paused,
        "closing_active": state._closing_active,
        "closing_message": state._closing_message,
    }


def _queue_item_dict(item) -> dict:
    t = item.track
    return {
        "track_id": t.id,
        "title": t.title,
        "artist": t.artist,
        "album": t.album,
        # album_id + added_at (unify-queue-remove U1): the shared queue renderer
        # groups an admin's albums by consecutive same-album_id and removes them
        # entry-based via /api/queue/undo (which matches on track_id + added_at).
        # The guest GET /api/queue and the queue_changed WS payload already carry
        # both; this aligns the admin GET path.
        "album_id": t.album_id,
        "added_at": item.added_at,
        "thumb": t.thumb,
        "duration_ms": t.duration_ms,
        "server_name": t.server_name,
    }


class MoveRequest(BaseModel):
    from_position: int
    to_position: int


@router.post("/queue/move")
async def queue_move(body: MoveRequest):
    try:
        await state.queue_engine.move(body.from_position, body.to_position)
    except IndexError:
        raise HTTPException(status_code=400, detail="Position out of range")
    return {"ok": True}


class ClearRequest(BaseModel):
    confirmed: bool = False


@router.post("/queue/clear")
async def queue_clear(body: ClearRequest):
    if not body.confirmed:
        raise HTTPException(status_code=400, detail="Must confirm queue clear")
    await state.queue_engine.clear()
    return {"ok": True}


@router.delete("/queue/{position}")
async def queue_remove(position: int):
    try:
        await state.queue_engine.remove(position)
    except IndexError:
        raise HTTPException(status_code=400, detail="Position out of range")
    return {"ok": True}


@router.post("/queue/{position}/play-next")
async def queue_play_next(position: int):
    try:
        await state.queue_engine.promote(position)
    except IndexError:
        raise HTTPException(status_code=400, detail="Position out of range")
    return {"ok": True}


@router.post("/queue/lock")
async def queue_lock():
    await state.queue_engine.lock()
    return {"ok": True, "is_locked": True}


@router.post("/queue/unlock")
async def queue_unlock():
    await state.queue_engine.unlock()
    return {"ok": True, "is_locked": False}


# ── Playback controls ─────────────────────────────────────────────────────────

@router.post("/playback/pause")
async def playback_pause():
    await state.output_router.pause()
    await state.queue_engine.set_paused(True)
    return {"ok": True}


@router.post("/playback/resume")
async def playback_resume():
    # Closing Time (2026-06-24 plan U2): after a trigger-song freeze there is no
    # paused output to resume — clear the banner and continue with the next
    # queued track instead. Otherwise this is a normal mid-track resume.
    if state._closing_active:
        await state.clear_closing_and_continue()
        return {"ok": True}
    await state.output_router.resume()
    await state.queue_engine.set_paused(False)
    return {"ok": True}


@router.get("/output/airplay-protocols")
async def list_airplay_protocols():
    """Return cached per-device AirPlay protocol verdicts (`ap2`/`ap1`)
    so the admin UI can hydrate its protocol-label cache on page load.
    Subsequent updates arrive via `airplay_protocol_changed` events on
    the WebSocket. Devices with no cached verdict are omitted; the UI
    treats absence as 'unknown' rather than guessing from TXT.
    """
    from app.output.airplay import _AIRPLAY_PROTOCOL_KEY_PREFIX
    from app import database
    all_with_prefix = await database.get_settings_with_prefix(
        _AIRPLAY_PROTOCOL_KEY_PREFIX
    )
    return {
        device_id: protocol
        for device_id, protocol in all_with_prefix.items()
        if protocol in ("ap2", "ap1")
    }


@router.post("/output/devices/{device_id}/retest-ap2")
async def retest_airplay_protocol(device_id: str):
    """Per-device 'Re-test AirPlay 2 support' affordance for F4 — firmware
    update recovery. Re-runs the discovery-time probe synchronously
    (bypasses the cached verdict via `force=True`) and returns the new
    verdict. The probe coroutine internally persists the verdict and
    broadcasts AirPlayProtocolChangedEvent.

    Refuses with 409 when the device is currently the active output AND
    audio is playing — the probe would spawn a competing cliap2 session
    against the same speaker and disrupt playback. The user must stop
    playback or switch active output first.
    """
    from app.output.airplay import AirPlayBackend
    airplay_backend = state.airplay_backend
    if airplay_backend is None:
        raise HTTPException(
            status_code=503, detail="AirPlay backend unavailable"
        )
    addr = airplay_backend._device_addr.get(device_id)
    if addr is None:
        raise HTTPException(
            status_code=404,
            detail=f"AirPlay device {device_id} not in discovery cache",
        )
    # Refuse Re-test on the currently-playing speaker. Probing it would
    # spawn a competing cliap2 session and disrupt the active stream.
    active = state.output_router.active
    if (
        isinstance(active, AirPlayBackend)
        and active._device_id == device_id
        and active.is_playing
    ):
        raise HTTPException(
            status_code=409,
            detail="Cannot re-test the currently-playing device; stop playback first",
        )
    name, host, port, txt = addr
    verdict = await airplay_backend._probe_device(
        device_id, name, host, port, txt, force=True,
    )
    return {"device_id": device_id, "protocol": verdict}


@router.post("/playback/no-audio")
async def playback_no_audio():
    """Recovery affordance for AirPlay sessions that pass the probe but
    stream silently (the WiiM case). Kills the active cliap2, persists
    `ap1` for the active device's device_id so subsequent plays use
    cliraop, broadcasts AirPlayProtocolChangedEvent, then restarts the
    current track via the standard play() path which now selects cliraop.

    Ordering matters: we persist `ap1` BEFORE play() so play()'s binary
    selection reads the cached verdict and routes to cliraop. We broadcast
    the protocol-change event only AFTER play() succeeds so admin clients
    aren't told 'protocol changed and is now playing' if the cliraop
    restart fails. On failure the cache stays as `ap1` (the user's
    explicit intent) but the UI label doesn't flip — the next manual
    play will surface the new state.

    Uses `state._advance_gen` + `state._advance_lock` like /playback/skip
    so a concurrent EOS-fired advance_cb doesn't race the stop+play.
    """
    from app.output.airplay import AirPlayBackend, _set_per_device_protocol
    from app.events.bus import manager as event_manager
    from app.events.types import AirPlayProtocolChangedEvent

    active = state.output_router.active
    if not isinstance(active, AirPlayBackend):
        raise HTTPException(
            status_code=400, detail="No active AirPlay session"
        )
    device_id = active._device_id
    if not device_id:
        raise HTTPException(
            status_code=400, detail="Active AirPlay backend has no device selected"
        )
    current = state.queue_engine.state.current
    if current is None:
        raise HTTPException(
            status_code=400, detail="No current track to restart"
        )

    state._advance_gen += 1  # invalidate any pending EOS _do_advance task
    async with state._advance_lock:
        # Persist `ap1` BEFORE play() so the binary-selection logic in
        # play() reads the cached verdict and routes to cliraop.
        await _set_per_device_protocol(device_id, "ap1")
        # Tear down the active (silent) cliap2 session, then restart the
        # current track from position 0 via the standard play() path.
        await state.output_router.stop()
        client = await state.get_plex_client()
        if client is None:
            raise HTTPException(status_code=502, detail="No media source configured")
        url = state._make_stream_url(current.track.stream_key, client)
        try:
            await state.output_router.play(url, current.track)
        except Exception:
            _log.exception("playback_no_audio: cliraop restart failed")
            raise HTTPException(status_code=502, detail="Playback restart failed")

    # Broadcast ONLY after the restart succeeds — otherwise admin clients
    # would be told "protocol flipped to ap1 and is now playing" even
    # though the playback restart failed and the speaker is silent.
    await event_manager.broadcast_to_admins(
        AirPlayProtocolChangedEvent(device_id=device_id, protocol="ap1")
    )
    return {"device_id": device_id, "protocol": "ap1"}


@router.post("/playback/skip")
async def playback_skip():
    """Skip to the next track in the queue.

    Calls `advance()` first, then `play()` if there's a next track, else
    `stop()`. Critically does NOT call `stop()` before `play()` — the
    earlier ordering broke the DLNA backend (stop() cleared `_dmr`, then
    play() raised RuntimeError because the backend was torn down). The
    natural-EOS advance path in `state._do_advance` also goes directly
    to play() without a prior stop(); this endpoint now matches that
    pattern. Each backend's play() handles the renderer-side transition
    from the currently-playing media to the next.
    """
    state._advance_gen += 1  # invalidate any pending EOS _do_advance task
    async with state._advance_lock:
        next_item = await state.queue_engine.advance()
        if next_item:
            client = await state.get_plex_client()
            if client:
                url = state._make_stream_url(next_item.track.stream_key, client)
                try:
                    await state.output_router.play(url, next_item.track)
                except Exception:
                    _log.exception("playback_skip: play() failed for %r", next_item.track.title)
                    await state.queue_engine.set_stopped()
                    raise HTTPException(status_code=502, detail="Playback failed")
                # A track you skip forward to and listen to is a real play, so
                # it counts — mirrors state._do_advance and playback_previous via
                # the shared record_play. (Skip historically counted nothing,
                # which kept skipped-to tracks off Most Played.)
                state.record_play(next_item.track)
        else:
            # Queue is empty after advance — stop playback fully.
            await state.output_router.stop()
    # The admin took manual control — if a Closing Time freeze was active, the
    # party's back on: clear the banner everywhere (no-op when not frozen).
    await state.clear_closing()
    return {"ok": True}


@router.post("/playback/previous")
async def playback_previous():
    """Skip Back: replay the most recently played track.

    Mirrors playback_skip's generation-bump + lock + play-without-stop
    shape, with one deliberate divergence: the Plex client is checked
    BEFORE the engine mutates. The ordering matters for the success path
    only — skip_back() itself is atomic and mutates nothing when history
    is empty (the 409 is safe regardless of ordering), but a successful
    skip_back() followed by no-client/failed play() would strand the
    interrupted track at queue front with nothing playing, which is
    harder to recover from than advance()'s history push (skip's
    silent-200 no-client path is acceptable there, not here).

    409 covers both "no history" (the button is disabled client-side;
    this is the race-condition safety net for history emptying between
    the last queue_changed event and the press) and "no Plex client".
    """
    client = await state.get_plex_client()
    if not client:
        raise HTTPException(status_code=409, detail="No media source available")
    state._advance_gen += 1  # invalidate any pending EOS _do_advance task
    async with state._advance_lock:
        prev_item = await state.queue_engine.skip_back()
        if prev_item is None:
            raise HTTPException(status_code=409, detail="No history to skip back to")
        url = state._make_stream_url(prev_item.track.stream_key, client)
        try:
            await state.output_router.play(url, prev_item.track)
        except Exception:
            _log.exception("playback_previous: play() failed for %r", prev_item.track.title)
            await state.queue_engine.set_stopped()
            raise HTTPException(status_code=502, detail="Playback failed")
        # A replay is a real play, so it counts — shared with _do_advance and
        # playback_skip via state.record_play (one canonical play-record path).
        state.record_play(prev_item.track)
    # Skip Back restarted playback — clear any active Closing Time freeze too.
    await state.clear_closing()
    return {"ok": True}


class VolumeRequest(BaseModel):
    level: float = Field(..., ge=0.0, le=1.0)


@router.get("/playback/volume")
async def get_playback_volume():
    try:
        level = await state.output_router.get_volume()
    except RuntimeError:
        level = 0.5
    return {"level": level}


@router.post("/playback/volume")
async def playback_volume(body: VolumeRequest):
    await state.output_router.set_volume(body.level)
    return {"ok": True, "level": body.level}


# ── Settings ──────────────────────────────────────────────────────────────────

@router.get("/settings")
async def get_settings():
    # Glow-up plan U1: rail/scheme pass through the same resolvers the
    # public endpoint uses — stored 'density' hydrates the Setup radio as
    # Waveform (review fix: second read edge), unknown schemes as gold.
    from app.api.guest import (
        _resolve_rail_mode, _resolve_scheme, _resolve_view, _resolve_rating_style,
        _resolve_surprise_enabled, _resolve_surprise_mode, _resolve_surprise_diversity,
    )
    from app.queue.models import coerce_queue_end_behavior
    # Migrate retired shuffle/repeat at the read edge so the Setup radio always
    # finds a current value (2026-06-21 plan U2).
    end_behavior = coerce_queue_end_behavior(
        await database.get_setting("queue_end_behavior")
    ).value
    n = await database.get_setting("queue_display_n")
    m = await database.get_setting("queue_display_m")
    rmin = await database.get_setting("random_min_seconds")
    rmax = await database.get_setting("random_max_seconds")
    ct_enabled, ct_title, ct_artist, ct_message = await database.get_closing_time_config()
    return {
        "queue_end_behavior": end_behavior,
        "queue_display_n": int(n) if n else None,
        "queue_display_m": int(m) if m else None,
        "rail_mode": _resolve_rail_mode(await database.get_setting("rail_mode")),
        "default_scheme": _resolve_scheme(await database.get_setting("default_scheme")),
        "default_view": _resolve_view(await database.get_setting("default_view")),
        # Rating display style (2026-06-27): hydrate the Setup radio; default stars.
        "rating_style": _resolve_rating_style(await database.get_setting("rating_style")),
        # Flood Control (off by default): when on, a guest re-add of a track
        # already playing/queued is blocked (see app/api/guest.py). Stored as
        # "1"/"0"; surfaced as a bool for the admin toggle's hydration.
        "flood_control": await database.get_setting("flood_control") == "1",
        # Lyrics contribute prompt (2026-06-23): default ON — show the quiet
        # "No lyrics found — contribute some?" link on a confirmed LRCLIB no-match.
        # Unset reads as on; only an explicit "0" turns it off. Consumed server-side
        # in app/api/guest.py (/api/lyrics); the browser never receives the flag.
        "lyrics_contribute_enabled": await database.get_setting("lyrics_contribute_enabled") != "0",
        # Surprise Me (2026-06-17): master on/off (default on) + the source mode
        # that drives the degradation chain.
        "surprise_me_enabled": _resolve_surprise_enabled(
            await database.get_setting("surprise_me_enabled")
        ),
        "surprise_me_source_mode": _resolve_surprise_mode(
            await database.get_setting("surprise_me_source_mode")
        ),
        "surprise_me_diversity": _resolve_surprise_diversity(
            await database.get_setting("surprise_me_diversity")
        ),
        # Random-pick length band (2026-06-20 plan U1): exclude tracks shorter
        # than min / longer than max seconds from random selection. 0 = bound
        # off. Echoed as seconds for the admin form.
        "random_min_seconds": int(rmin) if rmin and rmin.lstrip("-").isdigit() else 0,
        "random_max_seconds": int(rmax) if rmax and rmax.lstrip("-").isdigit() else 0,
        # Queue-end rework (2026-06-21 plan U2): popularity threshold (default 2)
        # for Popular Random + the opt-in length-limit checkbox for the queue-end
        # random modes.
        "popular_random_threshold": await database.get_popular_random_threshold(),
        "queue_end_length_limit": await database.get_queue_end_length_limit(),
        # Most Played leaderboard size (2026-06-23): rows /api/most-played returns.
        "most_played_display_limit": await database.get_most_played_display_limit(),
        # Track ratings + tags (2026-06-26 plan U4): guest-visibility flags (default
        # off) + per-facet Browse toggles (default on). Hydrate the Setup checkboxes.
        "ratings_visible_to_guests": await database.get_ratings_visible_to_guests(),
        "tags_visible_to_guests": await database.get_tags_visible_to_guests(),
        **{f"facet_{k}": v for k, v in (await database.get_browse_facets()).items()},
        # Closing Time mode (2026-06-24): off by default. Trigger song (title +
        # artist) and the send-off message are admin-editable; defaults are
        # "Closing Time" / "Semisonic" / the bar-closing line.
        "closing_time_enabled": ct_enabled,
        "closing_time_title": ct_title,
        "closing_time_artist": ct_artist,
        "closing_time_message": ct_message,
        # International rail (2026-06-22 plan 004): alpha-rail mode + the per-rail
        # first-character thresholds that bound a data-derived International rail.
        "rail_alpha_mode": await database.get_rail_alpha_mode(),
        "rail_artist_threshold": await database.get_rail_artist_threshold(),
        "rail_album_threshold": await database.get_rail_album_threshold(),
    }


class SettingsRequest(BaseModel):
    queue_end_behavior: str | None = None
    queue_display_n: int | None = None
    queue_display_m: int | None = None
    # R2/R9: install-wide rail mode. Literal validation rejects unknown values
    # with 422 at Pydantic layer. Density retired (glow-up R3) — writes of
    # 'density' are rejected; stored legacy values map at read time.
    rail_mode: Literal['vanilla', 'magnetic', 'waveform', 'loupe', 'vu'] | None = None
    # Glow-up plan U1 (R9): install-wide default color scheme.
    default_scheme: Literal[
        'gold-rush', 'king-crimson', 'case-of-blue', 'onion-green',
        'ladyland-orange', 'chasing-rabbits', 'sympathy-lime', 'pink-side',
        'silver-mountains', 'rainy-purple',
    ] | None = None
    # Tile-view plan (2026-06-15) U1: install-wide default browse/search view.
    default_view: Literal['list', 'tile'] | None = None
    # Rating display style (2026-06-27): install-wide look for the 0–5 rating.
    # Literal → 422 on a bad value. Reload-only (not broadcast).
    rating_style: Literal['stars', 'dots', 'bars'] | None = None
    # Flood Control (2026-06-16): when on, block guest re-adds of an
    # already-playing/queued track. Instant toggle — the admin checkbox POSTs
    # just this field on change.
    flood_control: bool | None = None
    # Lyrics contribute prompt (2026-06-23): default-on toggle for the
    # "contribute lyrics" link shown on a confirmed no-match (see app/api/guest.py).
    lyrics_contribute_enabled: bool | None = None
    # Surprise Me (2026-06-17): master on/off (the button disappears when off)
    # and the source mode driving the suggestion chain. Literal → 422 on bad mode.
    surprise_me_enabled: bool | None = None
    surprise_me_source_mode: Literal['auto', 'plex', 'heuristic', 'random'] | None = None
    # Diversity gate (2026-06-17 plan 003): off / album / artist (default artist).
    surprise_me_diversity: Literal['off', 'album', 'artist'] | None = None
    # Random-pick length band (2026-06-20 plan U1): exclude tracks shorter than
    # min / longer than max SECONDS from random selection (Surprise Me +
    # Shuffle). 0 (or unset) = that bound off; opt-in. Cross-field min<max
    # validation lives in update_settings (Pydantic can't see both fields).
    random_min_seconds: int | None = None
    random_max_seconds: int | None = None
    # Queue-end rework (2026-06-21 plan U2). popular_random_threshold: min local
    # plays for Popular Random (>= 1, default 2). queue_end_length_limit: opt-in
    # gate for the random length band over the queue-end modes (default off).
    popular_random_threshold: int | None = None
    queue_end_length_limit: bool | None = None
    # Most Played leaderboard size (2026-06-23): how many rows /api/most-played
    # returns. Display-only (>= 1, default 100); independent of the Popular
    # Random candidate pool, which is gated solely by popular_random_threshold.
    most_played_display_limit: int | None = None
    # Track ratings + tags (2026-06-26 plan U4). Guest visibility flags default
    # OFF; the five Browse-facet flags default ON. Persisted as "1"/"0".
    ratings_visible_to_guests: bool | None = None
    tags_visible_to_guests: bool | None = None
    facet_genre: bool | None = None
    facet_years: bool | None = None
    facet_mostplayed: bool | None = None
    facet_recentlyadded: bool | None = None
    facet_highestrated: bool | None = None
    # Closing Time mode (2026-06-24 plan): off-by-default toggle plus the
    # admin-editable trigger song (title + artist) and send-off message. Strings
    # persist as-given (trimmed); an enabled-but-blank trigger simply never fires.
    closing_time_enabled: bool | None = None
    closing_time_title: str | None = None
    closing_time_artist: str | None = None
    closing_time_message: str | None = None
    # International rail (2026-06-22 plan 004): alpha-rail mode + per-rail first-char
    # thresholds (>= 1, default 2). Literal → 422 on a bad mode; the >= 1 floor is
    # enforced in update_settings (mirrors popular_random_threshold).
    rail_alpha_mode: Literal['english', 'international'] | None = None
    rail_artist_threshold: int | None = None
    rail_album_threshold: int | None = None


@router.post("/settings")
async def update_settings(body: SettingsRequest):
    if body.queue_end_behavior is not None:
        try:
            eb = QueueEndBehavior(body.queue_end_behavior)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid queue_end_behavior")
        await database.set_setting("queue_end_behavior", eb.value)
        state.queue_engine.end_behavior = eb

    if body.queue_display_n is not None:
        await database.set_setting("queue_display_n", str(body.queue_display_n))
        from app.events.bus import manager
        manager.guest_n = body.queue_display_n or None

    if body.queue_display_m is not None:
        await database.set_setting("queue_display_m", str(body.queue_display_m))
        from app.events.bus import manager
        manager.guest_m = body.queue_display_m or None

    if body.rail_mode is not None:
        await database.set_setting("rail_mode", body.rail_mode)

    if body.default_scheme is not None:
        await database.set_setting("default_scheme", body.default_scheme)

    if body.default_view is not None:
        await database.set_setting("default_view", body.default_view)

    # Rating display style (2026-06-27): persisted like the other appearance
    # defaults and carried on the appearance broadcast below, so a change reaches
    # connected clients (admin + guests) live without a reload.
    if body.rating_style is not None:
        await database.set_setting("rating_style", body.rating_style)

    if body.flood_control is not None:
        await database.set_setting("flood_control", "1" if body.flood_control else "0")

    if body.lyrics_contribute_enabled is not None:
        await database.set_setting(
            "lyrics_contribute_enabled", "1" if body.lyrics_contribute_enabled else "0"
        )

    if body.surprise_me_enabled is not None:
        await database.set_setting(
            "surprise_me_enabled", "1" if body.surprise_me_enabled else "0"
        )

    if body.surprise_me_source_mode is not None:
        await database.set_setting("surprise_me_source_mode", body.surprise_me_source_mode)

    if body.surprise_me_diversity is not None:
        await database.set_setting("surprise_me_diversity", body.surprise_me_diversity)

    # Random-pick length band (2026-06-20 plan U1). Validate the EFFECTIVE pair
    # (incoming value wins; fall back to the stored value for an omitted side)
    # before persisting either, so a rejected request writes nothing. 0 = bound
    # off; a band is only contradictory when BOTH ends are active and min >= max.
    if body.random_min_seconds is not None or body.random_max_seconds is not None:
        def _stored_secs(raw: str | None) -> int:
            return int(raw) if raw and raw.lstrip("-").isdigit() else 0

        eff_min = (body.random_min_seconds if body.random_min_seconds is not None
                   else _stored_secs(await database.get_setting("random_min_seconds")))
        eff_max = (body.random_max_seconds if body.random_max_seconds is not None
                   else _stored_secs(await database.get_setting("random_max_seconds")))
        if eff_min < 0 or eff_max < 0:
            raise HTTPException(status_code=422, detail="length bounds must be non-negative")
        if eff_min > 0 and eff_max > 0 and eff_min >= eff_max:
            raise HTTPException(
                status_code=422,
                detail="random_min_seconds must be less than random_max_seconds",
            )
        if body.random_min_seconds is not None:
            await database.set_setting("random_min_seconds", str(body.random_min_seconds))
        if body.random_max_seconds is not None:
            await database.set_setting("random_max_seconds", str(body.random_max_seconds))

    # Queue-end rework (2026-06-21 plan U2): Popular Random threshold + the opt-in
    # length-limit gate for the queue-end random modes.
    if body.popular_random_threshold is not None:
        if body.popular_random_threshold < 1:
            raise HTTPException(
                status_code=422, detail="popular_random_threshold must be >= 1"
            )
        await database.set_setting(
            "popular_random_threshold", str(body.popular_random_threshold)
        )

    if body.queue_end_length_limit is not None:
        await database.set_setting(
            "queue_end_length_limit", "1" if body.queue_end_length_limit else "0"
        )

    # Most Played leaderboard size (2026-06-23): display-only floor of 1, same
    # validation shape as popular_random_threshold.
    if body.most_played_display_limit is not None:
        if body.most_played_display_limit < 1:
            raise HTTPException(
                status_code=422, detail="most_played_display_limit must be >= 1"
            )
        await database.set_setting(
            "most_played_display_limit", str(body.most_played_display_limit)
        )

    # Closing Time mode (2026-06-24 plan): bool stored as "1"/"0"; trigger
    # title/artist and message persist trimmed. No 422 — an enabled-but-blank
    # trigger just never matches at end-of-song.
    if body.closing_time_enabled is not None:
        await database.set_setting(
            "closing_time_enabled", "1" if body.closing_time_enabled else "0"
        )
    for _val, _key in (
        (body.closing_time_title, "closing_time_title"),
        (body.closing_time_artist, "closing_time_artist"),
        (body.closing_time_message, "closing_time_message"),
    ):
        if _val is not None:
            await database.set_setting(_key, _val.strip())

    # International rail (2026-06-22 plan 004): alpha-rail mode + per-rail first-char
    # thresholds. Mode is Literal-validated by Pydantic; thresholds enforce the >= 1
    # floor here (same shape as popular_random_threshold).
    if body.rail_alpha_mode is not None:
        await database.set_setting("rail_alpha_mode", body.rail_alpha_mode)

    for _field, _key in (
        (body.rail_artist_threshold, "rail_artist_threshold"),
        (body.rail_album_threshold, "rail_album_threshold"),
    ):
        if _field is not None:
            if _field < 1:
                raise HTTPException(status_code=422, detail=f"{_key} must be >= 1")
            await database.set_setting(_key, str(_field))

    # On-deck pre-buffer (2026-06-21 plan U4): any change to a selection input
    # invalidates a buffered random pick so the next warm reflects the new
    # settings. The next playback/queue event re-warms (handlers stay cheap).
    if any(v is not None for v in (
        body.queue_end_behavior, body.popular_random_threshold,
        body.random_min_seconds, body.random_max_seconds, body.queue_end_length_limit,
    )):
        await state.invalidate_ondeck()

    # Glow-up plan U1 (R9), extended for tile view (2026-06-15 U1): a change to
    # ANY appearance default broadcasts ALL current values; clients re-resolve
    # (their local overrides win client-side). One event, all knobs.
    if (
        body.rail_mode is not None
        or body.default_scheme is not None
        or body.default_view is not None
        or body.surprise_me_enabled is not None
        or body.rail_alpha_mode is not None
        or body.rail_artist_threshold is not None
        or body.rail_album_threshold is not None
        or body.rating_style is not None
    ):
        from app.api.guest import (
            _resolve_rail_mode, _resolve_scheme, _resolve_view, _resolve_surprise_enabled,
            _resolve_rating_style,
        )
        from app.events.bus import manager
        from app.events.types import AppearanceChangedEvent
        await manager.broadcast_to_all(AppearanceChangedEvent(
            scheme=_resolve_scheme(await database.get_setting("default_scheme")),
            rail_mode=_resolve_rail_mode(await database.get_setting("rail_mode")),
            view=_resolve_view(await database.get_setting("default_view")),
            surprise_me_enabled=_resolve_surprise_enabled(
                await database.get_setting("surprise_me_enabled")
            ),
            rail_alpha_mode=await database.get_rail_alpha_mode(),
            rail_artist_threshold=await database.get_rail_artist_threshold(),
            rail_album_threshold=await database.get_rail_album_threshold(),
            rating_style=_resolve_rating_style(await database.get_setting("rating_style")),
        ))

    # Track ratings + tags (2026-06-26 plan U4): guest-visibility + Browse-facet
    # flags, all bool → "1"/"0". No appearance broadcast — guests pick these up
    # on the next load (live propagation is deferred per the plan).
    for _flag in ("ratings_visible_to_guests", "tags_visible_to_guests",
                  "facet_genre", "facet_years", "facet_mostplayed",
                  "facet_recentlyadded", "facet_highestrated"):
        _v = getattr(body, _flag)
        if _v is not None:
            await database.set_setting(_flag, "1" if _v else "0")

    return {"ok": True}


# ── Track ratings + tags authoring (2026-06-26 ratings-and-tags plan U2) ─────
# Admin-only (router-level require_admin). Ratings/tags are Jukeplox-LOCAL and
# never touch Plex (R3). Per-tag length + per-track count caps and normalization
# live in app/database.py (the single chokepoint); these endpoints validate the
# rating range and echo the stored (normalized) set so the client renders the
# truth rather than its raw request.

class TrackRatingRequest(BaseModel):
    track_id: str
    stars: int  # 0 clears the rating (no distinct "rated zero" state); 1-5 sets.


class TrackTagsRequest(BaseModel):
    track_id: str
    tags: list[str] = Field(default_factory=list)


@router.post("/track-rating")
async def set_track_rating(body: TrackRatingRequest):
    validate_plex_id(body.track_id)
    if not 0 <= body.stars <= 5:
        raise HTTPException(status_code=422, detail="stars must be 0-5 (0 clears)")
    await database.set_rating(body.track_id, body.stars)
    return {"track_id": body.track_id, "stars": body.stars or None}


@router.post("/track-tags")
async def set_track_tags(body: TrackTagsRequest):
    validate_plex_id(body.track_id)
    # Defensive bound before normalization — the admin is trusted, but reject an
    # absurd payload rather than iterate it. Per-tag length and per-track count
    # are enforced (silently capped/deduped) inside database.set_tags.
    if len(body.tags) > 100:
        raise HTTPException(status_code=422, detail="too many tags in request")
    stored = await database.set_tags(body.track_id, body.tags)
    return {"track_id": body.track_id, "tags": stored}


# ── Play-data curation: prune plays / remove from Most Played (2026-07-03 U4) ─
# Admin-only (router-level require_admin). Mutates the LOCAL play stores only.

class RemovePlayRequest(BaseModel):
    track_id: str
    # added_at is matched by string equality against the in-memory history deque
    # (not parsed or queried), so a length cap is sufficient (mirrors QueueUndoRequest).
    added_at: str = Field(..., max_length=64)


class RemoveFromMostPlayedRequest(BaseModel):
    track_id: str


@router.post("/history/remove-play")
async def remove_play(body: RemovePlayRequest):
    """Undo one recent play (R2/R3): roll back its contribution to the track,
    album, and artist counts, then remove the history entry. Un-count first
    (awaited) is the least-harm order — a DB failure leaves both stores unchanged.
    404 when the entry is no longer in the current history (stale strip)."""
    validate_plex_id(body.track_id)
    q = state.queue_engine
    entry = next((i for i in q.history
                  if i.track_id == body.track_id and i.added_at == body.added_at), None)
    if entry is None:
        raise HTTPException(status_code=404, detail="History entry not found")
    await state.unrecord_play(body.track_id, entry.track.album, entry.track.artist)
    await q.remove_history_entry(body.track_id, body.added_at)
    return {"ok": True}


@router.post("/most-played/remove")
async def remove_from_most_played(body: RemoveFromMostPlayedRequest):
    """Remove a track from Most Played (R5/R6): delete its accumulated count, its
    re-mint sibling keys, and their captured meta. Track-scoped — album/artist
    name-keyed aggregates are untouched."""
    validate_plex_id(body.track_id)
    await state.purge_play_track(body.track_id)
    return {"ok": True}


# ── Surprise Me: source-attribution readout (2026-06-17 plan U6) ─────────────

@router.get("/surprise/recent")
async def get_surprise_recent():
    """Dev observability: the recently-resolved Surprise sources + a per-source
    tally, so an operator can tell whether Plex similarity is actually firing or
    the random floor is quietly catching everything — without any guest-visible
    signal. Process-local (resets on restart). Admin-only."""
    from app.queue.surprise import recent_sources, recent_source_tally
    return {"recent": recent_sources(), "tally": recent_source_tally()}


# ── Pattern rules + artist exclusions (2026-06-10 pattern-rules plan U4) ─────
# Dedicated endpoints instead of overloading SettingsRequest: clean list
# models with sanity caps. POST replaces the whole set, matching the Setup
# editors' Save-posts-everything model. Inert rules round-trip unmodified —
# only the public /api/pattern-rules endpoint filters them.

class PatternRulesRequest(BaseModel):
    rules: list[list[str]] = Field(..., max_length=100)

    @field_validator("rules")
    @classmethod
    def _cap_rule_shape(cls, v):
        for rule in v:
            if len(rule) > 20:
                raise ValueError("a rule may have at most 20 strings")
            for s in rule:
                if len(s) > 200:
                    raise ValueError("rule strings are capped at 200 characters")
        return v


class ArtistExclusionsRequest(BaseModel):
    names: list[str] = Field(..., max_length=500)

    @field_validator("names")
    @classmethod
    def _cap_name_length(cls, v):
        for s in v:
            if len(s) > 200:
                raise ValueError("exclusion entries are capped at 200 characters")
        return v


@router.get("/pattern-rules")
async def get_pattern_rules_admin():
    return {"rules": await database.get_pattern_rules()}


@router.post("/pattern-rules")
async def set_pattern_rules_admin(body: PatternRulesRequest):
    await database.set_pattern_rules(body.rules)
    # Rules changed → the artist grouping map's rules signature is now stale.
    # Rebuild it locally from the stored roster (no Plex re-crawl, R3/R6); the
    # drill-in's signature guard falls back to the live scan until it lands.
    state.trigger_artist_grouping_rebuild()
    return {"ok": True}


@router.get("/artist-exclusions")
async def get_artist_exclusions_admin():
    return {"names": await database.get_artist_exclusions()}


@router.post("/artist-exclusions")
async def set_artist_exclusions_admin(body: ArtistExclusionsRequest):
    await database.set_artist_exclusions(body.names)
    return {"ok": True}


# ── Admin WebSocket ───────────────────────────────────────────────────────────

# WebSocket endpoint cannot use the router-level Depends(require_admin) because
# browser WebSocket upgrades don't carry Authorization headers; we check the
# session cookie manually.
admin_ws_router = APIRouter(tags=["admin"])


@admin_ws_router.websocket("/admin/ws")
async def admin_websocket(websocket: WebSocket):
    from app.auth import session as session_mgr
    from app.events.bus import manager

    token = websocket.cookies.get(SESSION_COOKIE)
    if not token or not await session_mgr.validate_session(token):
        await websocket.close(code=4401)
        return

    await websocket.accept()
    manager.connect(websocket, "admin")
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket, "admin")
