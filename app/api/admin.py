"""Admin-facing API routes. All routes require a valid admin session."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import asdict
from typing import Annotated, Literal

_log = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator

from app.api.auth_routes import require_admin, SESSION_COOKIE
from app.api.guest import (
    enabled_libraries,
    validate_plex_id,
    _annotate_plex_held,
    _filter_playable,
    _require_playable,
    _resolve_album_tracks,
)
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
    # Cache-Control: no-cache on every HTML page (2026-07-17 ce-debug): the
    # ?v=<sha> asset buster only fires if the HTML referencing it is fresh —
    # header-less pages let browsers pin a stale JS bundle across deploys.
    return _templates.TemplateResponse(
        request, "admin/login.html", {"setup_required": setup_required},
        headers={"Cache-Control": "no-cache"})


@page_router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
async def admin_dashboard_page(request: Request):
    from app.auth import session as session_mgr
    token = request.cookies.get(SESSION_COOKIE)
    if not token or not await session_mgr.validate_session(token):
        return RedirectResponse(url="/admin/login")
    # no-cache: same stale-bundle guard as the login page / guest index.
    return _templates.TemplateResponse(request, "admin/dashboard.html",
                                       headers={"Cache-Control": "no-cache"})

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])

# The WebSocket route can't carry the Depends(...) on the router level the same
# way (WS doesn't send cookies in all browsers), so auth is checked manually there.


# ── Plex library config ───────────────────────────────────────────────────────

def _serialize_libraries(libraries: list, enabled_keys: set) -> list:
    return [
        {
            "key": lib.key,
            # The owning source_id (key prefix) so the frontend groups libraries
            # under their source without re-deriving it in JS (Libraries-panel U3).
            "source_id": database.source_id_from_section_key(lib.key),
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


class SubsonicConnectRequest(BaseModel):
    # OpenSubsonic connect (U4/R5): an API key (never a password) plus the
    # username the key belongs to (Subsonic's ``u`` param) and an optional client
    # name (``c``, defaulted in the adapter). server_url is stored credential-free.
    server_url: str = Field(min_length=1, max_length=512)
    api_key: str = Field(min_length=1, max_length=512)
    username: str = Field(min_length=1, max_length=256)
    name: str = Field(default="", max_length=128)


class EmbyConnectRequest(BaseModel):
    # Emby connect (U4/R5): username/password sign-in exchanged for a token; the
    # password is discarded (never persisted), mirroring JellyfinConnectRequest.
    server_url: str = Field(min_length=1, max_length=512)
    username: str = Field(min_length=1, max_length=256)
    password: str = Field(default="", max_length=512)
    name: str = Field(default="", max_length=128)


def _hostname(url: str) -> str:
    from urllib.parse import urlparse
    try:
        return urlparse(url if "://" in url else f"http://{url}").hostname or ""
    except Exception:
        return ""


async def _validate_source_url(url: str) -> None:
    """Connect-time SSRF guard for an admin-supplied source URL (U6/R12).

    Parses ``url``, resolves its hostname to IP(s), and rejects targets that
    could be used to reach internal infrastructure:

    - **Always** rejects loopback (``127.0.0.0/8``, ``::1``) and link-local
      (``169.254.0.0/16``, ``fe80::/10``) — regardless of the flag.
    - Rejects RFC-1918 private ranges (``10/8``, ``172.16/12``, ``192.168/16``)
      and unique-local IPv6 (``fc00::/7``) **only when
      ``settings.allow_private_sources`` is False** — the LAN-first default
      (True) lets a self-hosted server on the LAN connect out of the box.

    A rejection raises ``_source_error("blocked_private", …)`` whose message
    names ``ALLOW_PRIVATE_SOURCES`` so the admin knows the flag to flip.

    **DNS is resolved off the event loop** (``run_in_executor``) so a slow
    resolver can't block the async connect handler, and resolution **fails
    closed**: a host that can't be resolved raises the ``unreachable``
    ``_source_error`` rather than falling through to the probe — an unresolvable
    host is never let through the SSRF gate.

    Both literal-IP URLs and hostnames that *resolve* to a blocked range are
    caught: every resolved address is checked, so a hostname pointing at a
    private IP is treated exactly like a private-IP URL.

    Redirect / DNS-rebinding hardening: the connect probes use ``httpx``'s
    default ``follow_redirects=False`` (verified for both the Subsonic client
    and ``emby.authenticate``'s client), so a public host cannot 302 the probe
    to a private target. This function resolves and checks by IP, mirroring the
    resolution the probe will make. A small residual TOCTOU window remains
    (DNS could change between this check and the probe's own resolution, since
    the probe re-resolves the hostname rather than dialing the pinned IP);
    closing it fully would require pinning the connection to the resolved IP —
    larger surgery deferred. The no-follow-redirect posture is the primary
    defense against the redirect-based bypass."""
    import asyncio
    import ipaddress
    import socket
    from urllib.parse import urlparse

    from app.config import settings

    host = ""
    try:
        parsed = urlparse(url if "://" in (url or "") else f"http://{url or ''}")
        host = parsed.hostname or ""
    except Exception:
        host = ""
    if not host:
        raise _source_error("unreachable", "Could not parse the server URL")

    # Collect every IP the host resolves to (or the literal IP itself).
    addrs: list[str] = []
    try:
        ipaddress.ip_address(host)  # literal IP URL?
        addrs = [host]
    except ValueError:
        try:
            # Resolve OFF the event loop — getaddrinfo is blocking and a slow
            # resolver would otherwise stall the async connect handler.
            loop = asyncio.get_running_loop()
            infos = await loop.run_in_executor(None, socket.getaddrinfo, host, None)
            addrs = sorted({info[4][0] for info in infos})
        except OSError:
            # Fail CLOSED: a host we can't resolve must not fall through to the
            # outbound probe (an unresolvable host is never let through the SSRF
            # gate). The no-follow-redirect posture bounds residual TOCTOU.
            raise _source_error(
                "unreachable",
                "Could not resolve the server hostname. Check the URL.")
    if not addrs:
        raise _source_error(
            "unreachable",
            "Could not resolve the server hostname. Check the URL.")

    allow_private = settings.allow_private_sources
    for raw in addrs:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        # Always-blocked: loopback + link-local, flag-independent.
        if ip.is_loopback or ip.is_link_local:
            raise _source_error(
                "blocked_private",
                "That address (loopback/link-local) can't be used as a source. "
                "This is blocked for security and can't be overridden.")
        # Private / unique-local: blocked only when the operator has opted in.
        if not allow_private and ip.is_private:
            raise _source_error(
                "blocked_private",
                "That server is on a private/internal network, which is blocked "
                "by this install's security policy. Set ALLOW_PRIVATE_SOURCES=true "
                "to allow connecting to LAN sources.")


def _source_error(category: str, message: str) -> HTTPException:
    # R21: a categorized, legible failure; the source is NOT saved. The frontend
    # surfaces ``category`` (unreachable / auth_rejected / no_music_libraries /
    # duplicate) inline.
    return HTTPException(status_code=400, detail={"category": category, "message": message})


# Default ports stripped during URL normalization so http://host and
# http://host:80 (and the https/443 pair) compare equal for duplicate detection.
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _normalize_source_url(url: str) -> str:
    """Return a canonical form of a source URL for duplicate detection (U4).

    Normalizes scheme + host case, strips a trailing slash on the path, and drops
    the scheme's default port so ``http://Host:80/`` and ``http://host`` collide.
    A parse failure degrades to a lowercased, right-stripped string so two
    identical raw URLs still match."""
    from urllib.parse import urlparse
    raw = (url or "").strip()
    try:
        parsed = urlparse(raw if "://" in raw else f"http://{raw}")
        scheme = (parsed.scheme or "http").lower()
        host = (parsed.hostname or "").lower()
        port = parsed.port
        if port is not None and _DEFAULT_PORTS.get(scheme) == port:
            port = None
        netloc = host if port is None else f"{host}:{port}"
        path = (parsed.path or "").rstrip("/")
        return f"{scheme}://{netloc}{path}"
    except Exception:
        return raw.rstrip("/").lower()


async def _reject_duplicate_source(server_url: str) -> None:
    """Raise a ``duplicate`` source error if any configured source already lives at
    the same normalized URL (U4/R10). Reads the credential-free stored server_url
    of every non-local source type. Best-effort read: a store hiccup does not block
    a connect (worst case an admin sees two rows for one server)."""
    target = _normalize_source_url(server_url)
    try:
        existing: list[str] = []
        for s in await database.get_plex_servers():
            existing.append(s.get("server_url") or "")
        for j in await database.get_jellyfin_sources():
            existing.append(j.get("server_url") or "")
        for sub in await database.get_subsonic_sources():
            existing.append(sub.get("server_url") or "")
        for e in await database.get_emby_sources():
            existing.append(e.get("server_url") or "")
    except Exception:
        _log.warning("duplicate-source pre-check read failed for a connect", exc_info=True)
        return
    if any(_normalize_source_url(u) == target for u in existing if u):
        raise _source_error("duplicate", "A source at this URL is already configured.")


@router.get("/sources")
async def list_sources():
    """Connected media sources for the Sources panel + the priority list (U14).

    Each entry is ``{source_id, type, name, enabled}``. ``enabled`` is the
    whole-source on/off switch (False when the source is in the disabled_sources
    veto set, Libraries-panel U3). Plex servers come from ``plex_servers`` (or the
    legacy single-server config); Jellyfin from ``jellyfin_sources``; local-files
    directories from ``local_sources`` (U11)."""
    try:
        disabled = set(await database.get_disabled_sources())
    except Exception:
        disabled = set()   # veto read fails open (show enabled); source-list reads below surface errors normally

    def _entry(source_id: str, type_: str, name: str) -> dict:
        return {"source_id": source_id, "type": type_, "name": name,
                "enabled": source_id not in disabled}

    out: list[dict] = []
    for s in await database.get_plex_servers():
        out.append(_entry(s["machine_id"], "plex", s.get("name") or "Plex"))
    if not out:
        cfg = await database.get_plex_config()
        if cfg:
            out.append(_entry("", "plex", "Plex"))
    for j in await database.get_jellyfin_sources():
        out.append(_entry(j["source_id"], "jellyfin", j.get("name") or "Jellyfin"))
    for sub in await database.get_subsonic_sources():
        out.append(_entry(sub["source_id"], "subsonic", sub.get("name") or "Subsonic"))
    for e in await database.get_emby_sources():
        out.append(_entry(e["source_id"], "emby", e.get("name") or "Emby"))
    for l in await database.get_local_sources():
        out.append(_entry(l["source_id"], "local", l.get("name") or "Local"))
    return {"sources": out}


@router.get("/scan-status")
async def scan_status():
    """Catalog scan state for the admin Sources scan badge (plan U15/R20):
    ``{sources, scanning, scanned, empty}`` — same snapshot the guest onboarding
    states read, so admin and guest agree on one source of truth. Admin-gated by
    the router-level require_admin (R26)."""
    return await state.scan_status()


async def _enable_new_source_libraries(source_id: str) -> None:
    """Enable a freshly-connected source's libraries by default.

    The catalog scan filters EVERY source's libraries by the enabled-library set
    (uniform gating, ce-debug 2026-06-29), so an unseeded source is silently
    dropped → an empty catalog. Local-folder and Jellyfin connects have always
    seeded here (ce-debug 2026-07-24); NEWLY connected Plex servers seed too
    (fresh-install audit F1, 2026-08-06 — a single-library server had no UI path
    to its checkbox, so a doc-following fresh install dead-ended empty). The
    Libraries panel remains the opt-out: per-library checkboxes and the
    whole-source switch can still exclude content, and a Plex RE-auth never
    reaches this (callers scope it to newly-added machine_ids), so a remembered
    selection is never overwritten.
    """
    # Best-effort, mirroring scan.py's _safe posture. By the time this runs the
    # source is already saved and invalidate_plex_client() has cleared the cache,
    # so a failure here must NOT 500 the connect nor skip the caller's
    # trigger_catalog_refresh(). For Jellyfin, get_libraries() is a live HTTP call
    # that can raise post-auth (transient 5xx, token race, missing 'Id'); for local
    # it can't. The WARNING is load-bearing — it is the only signal that the source
    # connected but its libraries weren't seeded (empty catalog until a reconnect).
    try:
        registry = await state.get_plex_client()
        src = next((s for s in getattr(registry, "sources", []) or []
                    if getattr(s, "source_id", None) == source_id), None)
        if src is None:
            _log.warning("enable-libraries: source %s absent from the rebuilt registry — "
                         "libraries not seeded; reconnect the source to retry", source_id)
            return
        for lib in await src.get_libraries():
            await database.toggle_library(lib.key, getattr(lib, "title", "") or "", enabled=True)
    except Exception:
        _log.warning("enable-libraries failed for %s — libraries not seeded; "
                     "reconnect the source to retry", source_id, exc_info=True)


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
    await _clear_source_veto(source_id)   # reconnect must not inherit a stale veto (U3)
    state.invalidate_plex_client()   # rebuild the registry with the new source
    await _enable_new_source_libraries(source_id)  # non-Plex libs have no UI → enable by default
    state.trigger_catalog_refresh()  # crawl it into the unified catalog
    return {"ok": True, "source_id": source_id, "name": name, "type": "jellyfin"}


@router.delete("/sources/jellyfin/{source_id}")
async def remove_jellyfin(source_id: str):
    """Remove a Jellyfin source and re-resolve the registry/catalog (U14/R7)."""
    await database.delete_jellyfin_source(source_id)
    await _clear_source_veto(source_id)   # don't leave an orphan veto entry (U3)
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
    await _clear_source_veto(source_id)   # reconnect must not inherit a stale veto (U3)
    state.invalidate_plex_client()   # rebuild the registry with the new source
    await _enable_new_source_libraries(source_id)  # non-Plex libs have no UI → enable by default
    state.trigger_catalog_refresh()  # crawl it into the unified catalog
    return {"ok": True, "source_id": source_id, "name": name, "type": "local"}


@router.delete("/sources/local/{source_id}")
async def remove_local(source_id: str):
    """Remove a local-files source and re-resolve the registry/catalog (R7)."""
    await database.delete_local_source(source_id)
    await _clear_source_veto(source_id)   # don't leave an orphan veto entry (U3)
    state.invalidate_plex_client()
    state.trigger_catalog_refresh()
    return {"ok": True}


# ── Subsonic / Emby connect (2026-08-10-003 U4) ───────────────────────────────

_MUSIC_LIBRARY_TYPES = {"artist", "album", "music"}


def _has_music_libraries(libraries: list) -> bool:
    """True iff the source exposes at least one music section (U4). Subsonic/Emby
    music libraries carry ``type`` in {artist, album, music}; an empty list or a
    server exposing only non-music sections counts as zero."""
    return any(
        (getattr(lib, "type", "") or "").lower() in _MUSIC_LIBRARY_TYPES
        for lib in (libraries or [])
    )


async def _finish_source_connect(source_id: str) -> None:
    """The shared post-save connect sequence (U4), identical to connect_jellyfin:
    clear a stale veto → rebuild the registry → seed libraries (best-effort,
    WARNING-guarded so a post-auth failure can't 500 the connect or skip the
    refresh) → trigger a catalog crawl."""
    await _clear_source_veto(source_id)   # reconnect must not inherit a stale veto (U3)
    state.invalidate_plex_client()   # rebuild the registry with the new source
    await _enable_new_source_libraries(source_id)  # non-Plex libs have no UI → enable by default
    state.trigger_catalog_refresh()  # crawl it into the unified catalog


@router.post("/sources/subsonic")
async def connect_subsonic(body: SubsonicConnectRequest):
    """Connect an OpenSubsonic source by API key (R5 — a password is never stored).

    Validated live: the server MUST advertise the OpenSubsonic API-Key extension
    (rejected otherwise, never falling back to password/token+salt) and a
    ``get_libraries()`` probe must succeed. On success the API key is saved sealed
    and a catalog scan kicks off. A duplicate URL, unreachable server, bad key, or
    missing extension surfaces an inline-categorized error and the source is NOT
    saved. ``no_music_libraries`` is a warning that still saves the source.
    Admin-gated by the router-level require_admin (R26)."""
    from app.sources.subsonic import CLIENT_NAME, SubsonicAuthError, SubsonicSource
    await _reject_duplicate_source(body.server_url)
    server_url = body.server_url.rstrip("/")
    # Validate FIRST, before allocating a SubsonicSource (which opens an httpx
    # client) — a rejection must not leave a client unclosed / waste an alloc.
    # Mirrors connect_emby's validate → construct → try/finally-probe order.
    await _validate_source_url(body.server_url)  # reject loopback/link-local/private-per-flag pre-probe
    source = SubsonicSource(
        server_url=server_url, api_key=body.api_key, username=body.username,
        server_name=body.name.strip() or _hostname(body.server_url) or "Subsonic",
    )
    # ── outbound probe ──
    try:
        # API-Key extension gate FIRST: a no-extension server is rejected before
        # anything is saved (R5). Then a live get_libraries() validates the key +
        # reachability and yields the music-section count.
        await source.require_api_key_support()
        libraries = await source.get_libraries()
    except SubsonicAuthError as e:
        raise _source_error("auth_rejected", str(e) or "Subsonic rejected the API key")
    except Exception:
        raise _source_error("unreachable", "Could not reach the Subsonic server")
    finally:
        with contextlib.suppress(Exception):
            await source._http.aclose()
    # ── end outbound probe ──
    no_music = not _has_music_libraries(libraries)
    warning: str | None = None
    if no_music:
        _log.warning("subsonic connect: %s exposed no music libraries — saving anyway",
                     _hostname(body.server_url))
        warning = ("Source saved, but no music libraries were found — check that "
                   "the server exposes a Music library and the account has access.")
    # Stable id across reconnects: derive from the normalized URL.
    import hashlib
    source_id = "subsonic-" + hashlib.sha1(
        _normalize_source_url(server_url).encode("utf-8")).hexdigest()[:12]
    name = body.name.strip() or _hostname(body.server_url) or "Subsonic"
    await database.save_subsonic_source(
        source_id=source_id, server_url=server_url, name=name,
        token=body.api_key, user=body.username, client=CLIENT_NAME)
    await _finish_source_connect(source_id)
    # Surface the device-facing proxy base a URL-auth (Subsonic) source will
    # stream through so a wrong LAN-IP auto-detection is visible in the UI (U7)
    # BEFORE a silent no-audio cast. Best-effort: base detection must never fail
    # the connect — on RuntimeError (no device-reachable base) report null so the
    # UI shows the actionable "set STREAM_BASE_URL" guidance instead.
    resolved_stream_base: str | None
    try:
        resolved_stream_base = state.resolved_proxy_base_for_url_auth()
    except Exception:
        resolved_stream_base = None
    return {"ok": True, "source_id": source_id, "name": name, "type": "subsonic",
            "resolved_stream_base": resolved_stream_base, "warning": warning}


@router.delete("/sources/subsonic/{source_id}")
async def remove_subsonic(source_id: str):
    """Remove an OpenSubsonic source and re-resolve the registry/catalog (R7)."""
    await database.delete_subsonic_source(source_id)
    await _clear_source_veto(source_id)   # don't leave an orphan veto entry (U3)
    state.invalidate_plex_client()
    state.trigger_catalog_refresh()
    return {"ok": True}


@router.post("/sources/emby")
async def connect_emby(body: EmbyConnectRequest):
    """Connect an Emby source by account sign-in (R5). Validated by signing in; on
    success the credential is saved TOKEN-ONLY (password discarded) and a catalog
    scan kicks off. A duplicate URL, bad credentials, or unreachable server
    surfaces an inline-categorized error and the source is NOT saved.
    ``no_music_libraries`` is a warning that still saves the source. Admin-gated by
    the router-level require_admin (R26)."""
    from app.sources import emby as emby_mod
    from app.sources.emby import EmbyAuthError, EmbySource
    await _reject_duplicate_source(body.server_url)
    server_url = body.server_url.rstrip("/")
    device_id = emby_mod.new_device_id()
    # ── outbound probe (U6: URL/SSRF validation runs immediately before this) ──
    await _validate_source_url(body.server_url)  # reject loopback/link-local/private-per-flag pre-probe
    try:
        creds = await emby_mod.authenticate(
            body.server_url, body.username, body.password, device_id=device_id)
    except EmbyAuthError as e:
        raise _source_error("auth_rejected", str(e) or "Emby rejected the credentials")
    except Exception:
        raise _source_error("unreachable", "Could not reach the Emby server")
    # ── end outbound probe ──
    source_id = f"emby-{creds.get('server_id') or device_id}"
    name = body.name.strip() or _hostname(body.server_url) or "Emby"
    # A get_libraries() probe surfaces the music-section count (warning-only, R10).
    source = EmbySource(
        server_url=server_url, token=creds["token"], user_id=creds["user_id"],
        source_id=source_id, server_name=name, device_id=device_id,
    )
    try:
        libraries = await source.get_libraries()
    except Exception:
        # A post-auth probe hiccup is non-fatal (enable-libs retries), but must
        # not be swallowed silently — log at WARNING so the failure is visible.
        libraries = []
        _log.warning("emby connect: post-auth get_libraries() failed for %s — "
                     "proceeding without the music-library count; enable-libs "
                     "will retry", _hostname(body.server_url), exc_info=True)
    finally:
        with contextlib.suppress(Exception):
            await source._http.aclose()
    warning: str | None = None
    if not _has_music_libraries(libraries):
        _log.warning("emby connect: %s exposed no music libraries — saving anyway",
                     _hostname(body.server_url))
        warning = ("Source saved, but no music libraries were found — check that "
                   "the server exposes a Music library and the account has access.")
    await database.save_emby_source(
        source_id=source_id, server_url=server_url, name=name,
        token=creds["token"], user_id=creds["user_id"], device_id=device_id)
    await _finish_source_connect(source_id)
    return {"ok": True, "source_id": source_id, "name": name, "type": "emby",
            "warning": warning}


@router.delete("/sources/emby/{source_id}")
async def remove_emby(source_id: str):
    """Remove an Emby source and re-resolve the registry/catalog (R7)."""
    await database.delete_emby_source(source_id)
    await _clear_source_veto(source_id)   # don't leave an orphan veto entry (U3)
    state.invalidate_plex_client()
    state.trigger_catalog_refresh()
    return {"ok": True}


async def _clear_source_veto(source_id: str) -> None:
    """Remove a source from the disabled_sources veto set (Libraries-panel U3).
    Called on connect/reconnect so a freshly-seeded source honors its default-ON
    switch instead of inheriting a stale veto ("connected but invisible"), and on
    removal so the veto set doesn't accumulate orphan ids. No-op when absent.

    Best-effort: this cleanup rides along with a connect/remove, so a veto-store
    hiccup must not fail that primary op. Worst case a stale veto lingers until the
    next explicit toggle."""
    try:
        disabled = await database.get_disabled_sources()
        if source_id in disabled:
            await database.set_disabled_sources([s for s in disabled if s != source_id])
    except Exception:
        _log.warning("clear-source-veto failed for %s", source_id, exc_info=True)


async def _set_source_disabled(source_id: str, *, disabled: bool) -> None:
    """The whole-source on/off switch (Libraries-panel U3): add/remove source_id in
    the disabled_sources veto set, then reconcile caches. The veto write is the
    must-succeed record; the cache invalidate + catalog/index refresh are wrapped
    best-effort so a refresh failure can't 500 the toggle or skip the persisted veto
    (control-plane-success / best-effort-after-commit)."""
    current = await database.get_disabled_sources()
    if disabled and source_id not in current:
        await database.set_disabled_sources(current + [source_id])
    elif not disabled and source_id in current:
        await database.set_disabled_sources([s for s in current if s != source_id])
    # Bump the SWR generation immediately (sync + infallible) so the native-path cache
    # can't resurrect the pre-toggle set even if the best-effort reconcile below fails.
    state.invalidate_plex_client()
    try:
        # Reconcile the rest best-effort: on-deck slot + the table-backed catalog and
        # browse index (multi-source path). A failure here must not undo the veto write.
        await state.invalidate_ondeck()
        state.trigger_browse_index_refresh()
        state.trigger_catalog_refresh()
    except Exception:
        _log.warning("source-toggle reconcile failed for %s (disabled=%s) — veto saved; "
                     "reconciles on next scan", source_id, disabled, exc_info=True)
    # R12 mid-session re-validation (2026-08-04-002 plexplayer plan U6): the
    # whole-source veto flips the READ-TIME playability predicate immediately
    # (plex_enabled_source_ids reads disabled_sources live), so while a Plex
    # player is the selected output, queued tracks whose only Plex holder just
    # got vetoed are stranded NOW — remove them here rather than waiting for
    # the triggered background rescan to finish (that hook still runs; a
    # second pass finds nothing and stays silent). Runs on enable too:
    # harmless no-op (enabling can only widen playability). Best-effort — a
    # re-validation failure must not fail the persisted veto toggle.
    try:
        await state.revalidate_plex_queue(trigger="library change")
    except Exception:
        _log.warning("post-veto queue re-validation failed for %s",
                     source_id, exc_info=True)


@router.post("/sources/{source_id}/enable")
async def enable_source(source_id: str):
    """Turn a whole source ON (remove its veto); its remembered per-library selection
    re-applies (Libraries-panel U3)."""
    await _set_source_disabled(source_id, disabled=False)
    return {"ok": True}


@router.post("/sources/{source_id}/disable")
async def disable_source(source_id: str):
    """Turn a whole source OFF (veto it): its content is excluded from guest-visible
    catalog/browse/random, but its enabled_libraries rows are kept so ON restores the
    exact selection (Libraries-panel U3)."""
    await _set_source_disabled(source_id, disabled=True)
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
    # Snapshot connected Plex machine_ids BEFORE the flow so we clear the veto ONLY for
    # a server this connect NEWLY added — never silently re-enable an already-connected,
    # deliberately-disabled co-owned Plex server on a re-auth (ce-code-review 2026-07-28).
    try:
        before = {s["machine_id"] for s in await database.get_plex_servers()}
    except Exception:
        before = set()
    resolved = await plex_oauth.complete_flow(pin_id, client_id)
    if resolved:
        # A NEWLY (re)connected Plex server honors its default-ON switch: clear its veto
        # so it can't come back invisible (U3). Plex has no panel Remove, so there is no
        # Plex-disconnect veto path. Best-effort — a veto-store hiccup must not fail connect.
        new_ids: set[str] = set()
        owned_new_ids: set[str] = set()
        try:
            after_rows = await database.get_plex_servers()
            after = {s["machine_id"] for s in after_rows}
            new_ids = after - before
            # owned is 1/0/NULL; NULL (legacy rows — discovery didn't say) counts
            # as NOT owned: never auto-crawl a library we can't prove belongs to
            # this account (owned-only seeding policy, 2026-08-06).
            owned_new_ids = {s["machine_id"] for s in after_rows
                             if s["machine_id"] in new_ids and s.get("owned")}
            disabled = await database.get_disabled_sources()
            remaining = [d for d in disabled if d not in new_ids]
            if len(remaining) != len(disabled):
                await database.set_disabled_sources(remaining)
                state.invalidate_plex_client()  # veto set changed -> bump the SWR generation
        except Exception:
            _log.warning("plex-connect veto clear failed", exc_info=True)
        # Seed a NEWLY connected OWNED server's libraries enabled-by-default, matching
        # the non-Plex connects above. Without this, a fresh install whose server holds
        # a single music library dead-ends: no enabled_libraries row is ever written and
        # Rescan clears an already-empty catalog (fresh-install audit F1, 2026-08-06).
        # Scoped to newly-added machine_ids so a RE-auth never overwrites a remembered
        # per-library selection, and to OWNED servers so a friend's shared library is
        # never auto-crawled into the party catalog — shared servers stay opt-in via
        # the always-rendered Libraries drill-in.
        for sid in owned_new_ids:
            await _enable_new_source_libraries(sid)
        if owned_new_ids:
            state.trigger_catalog_refresh()  # crawl the new server without a manual Rescan
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
        "plexplayer": state.plexplayer_backend,
    }
    results: dict = {name: [] for name in backends}
    # plexplayer is deliberately ABSENT from mdns_status (2026-08-04-002
    # plan U3): its liveness rides authenticated PMS /clients polling, not
    # mDNS, and the frontend banner treats a missing key as fine while an
    # "unavailable" value would render the backend degraded.
    mdns_status: _MdnsStatus = {
        name: "ok" for name in backends if name != "plexplayer"}

    async def _discover(name, backend):
        if not backend:
            if name in mdns_status:
                mdns_status[name] = "unavailable"
            return
        try:
            found = await backend.discover_devices()
            _log.info("Discovery [%s]: %d device(s) found", name, len(found))
            results[name] = found
        except Exception:
            _log.exception("Discovery [%s]: exception during scan", name)
            results[name] = []
            if name in mdns_status:
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
        # plexplayer is HTTP to the PMS (/clients) — no shared socket, safe
        # to run concurrently with everything else.
        _discover("plexplayer", state.plexplayer_backend),
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
    backend_type: Literal['direct', 'chromecast', 'dlna', 'airplay',
                          'plexplayer']
    device_id: str = "default"
    host: str | None = None
    # Two-phase stranded-tracks switch confirm (2026-08-04-002 plan U6 —
    # field added with U3's Literal change per the plan's single-model-edit
    # note). Consumed by _switch_stranded_gate: a plexplayer target with
    # stranded queue entries 409s until the client resends confirmed=true;
    # defaults False so every existing caller is unchanged.
    confirmed: bool = False

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
    # plexplayer is deliberately absent here AND from the watcher's map
    # (2026-08-04-002 plan U3): no mDNS involvement, and a missing key is
    # what keeps the frontend from rendering it degraded.
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


async def _switch_stranded_gate(body: SetOutputRequest) -> int:
    """Two-phase stranded-tracks confirm for a switch TO plexplayer
    (2026-08-04-002 plan U6; R7/R8, AE1/AE2, F1). Runs in the route, OUTSIDE
    ``activate_backend`` — the switch itself keeps its raise-before-any-
    state-change rollback semantics, and the 409 lives with the API layer's
    other 409s. Recomputes the stranded set LIVE on EVERY call (both
    phases), so a guest enqueue between warning and confirm is caught by
    the confirm-phase recompute (race-safe; the response reports the
    actual removed count).

    * stranded + not confirmed → 409 with a STRUCTURED dict detail
      (``reason: "output_switch_confirm"``) — deliberately distinguishable
      from the plain-string 409s on this endpoint (``output_source_lock``
      enqueue gate, activate_backend RuntimeErrors), so a genuine switch
      failure can never open the client's confirm dialog. NO state change.
    * stranded + confirmed → remove the stranded entries (receipts kept,
      held-front ``_advance_gen`` discipline inside the state helper),
      return the actual removed count PLUS a positional restore snapshot;
      the caller proceeds to activate and ROLLS THE REMOVAL BACK if the
      activate raises (review fix PLX-3 — a failed switch leaves the old
      backend playing, so the guests' tracks must not stay silently gone).
      Removal-before-activate is the plan's order: the activate path
      auto-starts playback when the queue has items, so a stranded front
      entry must be gone before that dispatch can pick it.
    * all-playable queue / gate inert → (0, []), straight activate (no 409).

    Switching AWAY from plexplayer never routes here (plan scope: every
    track is playable on URL-dispatch backends)."""
    stranded = await state.plex_stranded_entries(assume_lock=True)
    if not stranded:
        return 0, []
    if not body.confirmed:
        raise HTTPException(status_code=409, detail={
            "reason": "output_switch_confirm",
            "stranded_count": len(stranded),
            "confirm_required": True,
        })
    snapshot = state.snapshot_stranded_positions(stranded)
    return await state.remove_stranded_entries(stranded), snapshot


@router.post("/output/active")
async def set_output_active(body: SetOutputRequest):
    removed_count, restore_snapshot = 0, []
    if body.backend_type == "plexplayer":
        removed_count, restore_snapshot = await _switch_stranded_gate(body)
    try:
        await state.activate_backend(body.backend_type, body.device_id, host=body.host)
    except Exception as exc:
        # PLX-3 rollback: the switch did NOT happen (activate raises before
        # any state change), so a confirmed stranded removal must be undone
        # — re-insert the captured entries at their original positions
        # (receipts intact) before reporting the failure. The error response
        # never claims a removal.
        if restore_snapshot:
            try:
                await state.restore_stranded_entries(restore_snapshot)
            except Exception:
                _log.warning("stranded-removal rollback failed after "
                             "activate_backend error", exc_info=True)
        if isinstance(exc, HTTPException):
            raise
        if isinstance(exc, RuntimeError):
            raise HTTPException(status_code=409, detail=str(exc))
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
        # U6: the confirmed-switch response carries the ACTUAL number of
        # stranded entries removed (0 on every non-plexplayer / all-playable
        # switch) — the admin dialog toasts this, never the warned count.
        "removed_count": removed_count,
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
        # U5 source-lock gate — IDENTICAL to the guest endpoint's (no admin
        # bypass, R6): the SAME shared helper from app/api/guest.py (S-3),
        # evaluated live server-side.
        await _require_playable(body.track_id)
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
    # U5 subset policy — the guest album branch's SAME shared helper (S-3).
    # (The resolver alias-bridges the native provider ids this branch yields
    # in catalog mode back to catalog identities before deciding.)
    tracks, tracks_filtered = await _filter_playable(tracks)
    # All-or-nothing batch append: validate full batch under one lock so a
    # partial album never lands when the queue is at the cap.
    await q.append_many(tracks, bypass_lock=True)
    return {"ok": True, "tracks_added": len(tracks),
            "tracks_filtered": tracks_filtered}


@router.get("/queue")
async def get_queue():
    from app.output import session as output_session
    q = state.queue_engine
    queue_rows = [{**_queue_item_dict(i), "position": idx}
                  for idx, i in enumerate(q.queue)]
    history_rows = [_queue_item_dict(i) for i in q.history]
    # plex_held on the admin queue/recents rows too (plan U4) — the shared
    # queue renderer paints both pages from one payload shape. One combined
    # pass so queue + history share a single bulk holds read.
    await _annotate_plex_held(queue_rows + history_rows)
    return {
        "queue": queue_rows,
        "history": history_rows,
        "is_locked": q.is_locked,
        "current": _queue_item_dict(q.state.current) if q.state.current else None,
        "is_playing": q.state.is_playing,
        "is_paused": q.state.is_paused,
        # Closing Time (2026-06-24 plan U3): admin hydrates the banner from here.
        "closing_active": state._closing_active,
        "closing_message": state._closing_message,
        # Output-session state (supervisor plan U4, R20): the admin-rich
        # snapshot — the SAME shape the OutputSessionEvent broadcasts carry —
        # so the page's refreshQueueState resync (load / WS reconnect / tab
        # refocus) hydrates the outage banner through one render path.
        "output_session": await output_session.session_snapshot_admin(),
    }


@router.get("/playback/now-playing")
async def admin_now_playing():
    from app.output import session as output_session
    s = state.queue_engine.state
    # Output-session state (supervisor plan U4, R20): the admin-rich snapshot
    # mirror of the OutputSessionEvent broadcasts — present in BOTH branches
    # because an outage hold clears `current` (the held item re-front-inserts),
    # so the no-current branch is exactly the one late joiners hit mid-outage.
    output_snap = await output_session.session_snapshot_admin()
    # Radio Mode (radio plan U7): the `radio` block — IDENTICAL to the guest
    # snapshot (SG-05), so both pages converge from one shape. Present in BOTH
    # branches (a radio takeover holds `current`).
    from app.api.radio import radio_snapshot
    radio_snap = radio_snapshot()
    if not s.current:
        return {
            "is_playing": False, "is_paused": False,
            "closing_active": state._closing_active,
            "closing_message": state._closing_message,
            "output_session": output_snap,
            "radio": radio_snap,
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
        "output_session": output_snap,
        "radio": radio_snap,
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
        # owner_token: align the admin GET shape with the guest GET / queue_changed
        # rows (which carry it for durable guest ownership). Always None on admin
        # appends — admin removal is not ownership-scoped — but keeping the field
        # present (null) makes the two queue-row shapes a consistent superset.
        "owner_token": getattr(item, "owner_token", None),
        "thumb": t.thumb,
        "duration_ms": t.duration_ms,
        "server_name": t.server_name,
    }


class MoveRequest(BaseModel):
    from_position: int
    to_position: int


@router.post("/queue/move")
async def queue_move(body: MoveRequest):
    from app.output import session as output_session
    if output_session.output_hold_active():
        # R17 (the queue_clear mechanic): moving during an outage can change
        # the HELD front — the gen bump makes any in-flight resume treat the
        # new front as fresh at 0:00 instead of seeking the displaced
        # track's held position into it.
        state._advance_gen += 1
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
    from app.output import session as output_session
    if output_session.output_hold_active():
        # R17 (supervisor plan U4): clearing during an outage drops the HELD
        # item too — it sits at the queue front, so clear() removes it with
        # the rest. The hold itself stays (the device is still gone; U3's
        # resume path lands IDLE on an empty queue at re-attach, so no orphan
        # resume fires later). The gen bump makes any in-flight resume treat
        # whatever the queue holds NEXT (a later append) as a fresh front at
        # 0:00 instead of seeking the dropped track's held position into it.
        state._advance_gen += 1
    await state.queue_engine.clear()
    return {"ok": True}


@router.delete("/queue/{position}")
async def queue_remove(position: int):
    from app.output import session as output_session
    if output_session.output_hold_active():
        # R17 (the queue_clear mechanic): removing during an outage can drop
        # the HELD front — the gen bump re-targets any in-flight resume at
        # whatever is in front next, at 0:00.
        state._advance_gen += 1
    try:
        await state.queue_engine.remove(position)
    except IndexError:
        raise HTTPException(status_code=400, detail="Position out of range")
    return {"ok": True}


@router.post("/queue/{position}/play-next")
async def queue_play_next(position: int):
    from app.output import session as output_session
    if output_session.output_hold_active():
        # R17 (the queue_clear mechanic): promoting during an outage replaces
        # the HELD front — the gen bump re-targets any in-flight resume at
        # the promoted track from 0:00.
        state._advance_gen += 1
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
    from app.output import session as output_session
    if output_session.output_hold_active():
        # Pause during an outage hold (R17): the output is GONE — a live
        # pause write would raise against the dead device (DLNA's
        # async_pause has no guard → 500). Record the intent so re-attach
        # lands PAUSED; the queue is already paused (the hold did that).
        output_session.get_supervisor().set_held_paused_intent()
        return {"ok": True}
    await state.output_router.pause()
    await state.queue_engine.set_paused(True)
    return {"ok": True}


@router.post("/playback/resume")
async def playback_resume():
    # Manual resume from an output-outage hold (supervisor plan U3, R17):
    # works in OutagePaused (attach now, then play), Paused-after-reattach
    # and IdlePaused (window expired / flap guard), playing from the held
    # position. Checked BEFORE the Closing Time branch — while the queue is
    # held there is no playback to "continue" until the device is back, and
    # the admin pressing Play is the manual override the gates defer to.
    from app.output import session as output_session
    if output_session.output_hold_active():
        sup = output_session.get_supervisor()
        ok = await sup.manual_resume()
        if not ok:
            # Honest, machine-readable failure: tell a single-flight loss
            # (another attempt already running) apart from a device that is
            # genuinely still unreachable.
            ot = sup.peek_outage()
            if ot is not None and ot.attempt_inflight:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "attempt_in_progress",
                        "message": "a reconnect attempt is already running "
                                   "— try again shortly",
                    },
                )
            device = None
            if ot is not None:
                device = (getattr(ot.backend, "_resolved_name", None)
                          or ot.device_id)
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "device_unreachable",
                    "message": (f"Output device {device!r} is unreachable — "
                                f"still retrying" if device else
                                "Output device is unreachable — still retrying"),
                },
            )
        # The resume restarted (or resolved) playback — an active Closing
        # Time freeze is over, same as the live-resume path below.
        if state._closing_active:
            await state.clear_closing()
        return {"ok": True}
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

    While an outage holds the queue (supervisor plan U4, R17), Skip moves
    the HELD POINTER only — no dispatch, no set_stopped, no 502.
    """
    from app.output import session as output_session
    state._advance_gen += 1  # invalidate any pending EOS _do_advance task
    async with state._advance_lock:
        # The held check lives INSIDE the lock: an in-flight resume holds it
        # and may clear the hold before we run — deciding early would
        # pointer-pop the then-LIVE queue front into history undispatched.
        if output_session.output_hold_active():
            # R17: the old path dispatched to the dead device → 502, and its
            # set_stopped error handling destroyed the popped held item.
            # Instead: retire the held front to history (its play_recorded
            # mark intact) and let the next queued item become the held
            # front, position 0:00. The gen bump re-targets any in-flight
            # auto-resume at the new front from 0:00 (U3 pins that
            # contract). The hold — and its retry loop — survive untouched.
            # Closing Time is NOT cleared here: a pointer move restarts
            # nothing.
            await state.queue_engine.skip_held_front()
            return {"ok": True}
        next_item = await state.queue_engine.advance()
        if next_item:
            client = await state.get_plex_client()
            if client:
                url = state._make_stream_url(next_item.track.stream_key, client)
                try:
                    # A track you skip forward to and listen to is a real play,
                    # so it counts — via the output-session supervisor's
                    # confirmed-start chokepoint (2026-07-11 plan U1), shared
                    # with state._do_advance and playback_previous. Dispatching
                    # reports the play; record_play fires only when the backend
                    # confirms playback actually started.
                    await state.dispatch_play(
                        url, next_item.track,
                        play_recorded=bool(getattr(next_item, "play_recorded", False)),
                        # Holder handshake (2026-08-04-002 U3): this site
                        # dispatches the primary holder's stream_key.
                        holder_key=next_item.track.stream_key or None,
                    )
                    # The R19 mark protected THIS pending play; consume it so
                    # a later organic replay counts again (supervisor plan U3
                    # unified this with _play_with_fallback's consumption).
                    next_item.play_recorded = False
                except Exception:
                    _log.exception("playback_skip: play() failed for %r", next_item.track.title)
                    await state.queue_engine.set_stopped()
                    raise HTTPException(status_code=502, detail="Playback failed")
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

    While an outage holds the queue (supervisor plan U4, R17), Skip Back
    moves the HELD POINTER only — a pointer move dispatches nothing and
    needs no media source, so the client gate belongs to the live branch.
    """
    from app.output import session as output_session
    state._advance_gen += 1  # invalidate any pending EOS _do_advance task
    async with state._advance_lock:
        # The held check lives INSIDE the lock: an in-flight resume holds it
        # and may clear the hold before we run — deciding early would
        # pointer-move a LIVE session's queue without dispatching.
        if output_session.output_hold_active():
            # R17: front-insert the previous history item as the new held
            # front (no dispatch, no set_stopped, no 502; hold + retry loop
            # survive). Its play_recorded mark rides along: a skipped-away
            # held item keeps its counted mark; an organically played
            # history item re-counts at resume, matching live Skip Back. The
            # gen bump re-targets any in-flight auto-resume at the new front
            # from 0:00 (U3 contract).
            prev_item = await state.queue_engine.skip_back_held_front()
            if prev_item is None:
                raise HTTPException(status_code=409,
                                    detail="No history to skip back to")
            return {"ok": True}
        client = await state.get_plex_client()
        if not client:
            raise HTTPException(status_code=409, detail="No media source available")
        prev_item = await state.queue_engine.skip_back()
        if prev_item is None:
            raise HTTPException(status_code=409, detail="No history to skip back to")
        url = state._make_stream_url(prev_item.track.stream_key, client)
        try:
            # A replay is a real play, so it counts — via the supervisor's
            # confirmed-start chokepoint (2026-07-11 plan U1), shared with
            # _do_advance and playback_skip: dispatch reports the play,
            # record_play fires only on the backend's confirmed start.
            await state.dispatch_play(
                url, prev_item.track,
                play_recorded=bool(getattr(prev_item, "play_recorded", False)),
                # Holder handshake (2026-08-04-002 U3): this site dispatches
                # the primary holder's stream_key.
                holder_key=prev_item.track.stream_key or None,
            )
            # The R19 mark protected THIS pending play; consume it so a later
            # organic replay counts again (supervisor plan U3 unified this
            # with _play_with_fallback's consumption).
            prev_item.play_recorded = False
        except Exception:
            _log.exception("playback_previous: play() failed for %r", prev_item.track.title)
            await state.queue_engine.set_stopped()
            raise HTTPException(status_code=502, detail="Playback failed")
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
    # Volume during an outage hold (supervisor plan U3, R17): accepted +
    # persisted + applied at re-attach before audio — a live device write
    # would raise against the dead output and 500 this endpoint.
    from app.output import session as output_session
    if output_session.output_hold_active():
        await output_session.set_held_volume(body.level)
        return {"ok": True, "level": body.level}
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
        # Radio Mode (2026-08-11 plan U8, R9): guest radio-control toggle
        # (default off). Hydrates the Setup checkbox.
        "guest_radio_control": await database.get_guest_radio_control(),
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
        # Gapless playback (2026-07-11 supervisor plan U5, R10): default-off
        # master toggle; per-protocol capability shows in the output picker.
        "gapless_enabled": await database.get_gapless_enabled(),
        # Auto-resume window (supervisor plan U3/U5, R8): minutes after an
        # output outage within which the supervisor may still auto-resume. The
        # getter resolves the default (60), so the box shows the live value.
        "resume_window_minutes": await database.get_resume_window_minutes(),
        # Volume bar orientation (2026-08-04 volume rework U3): horizontal
        # default; only Literal-validated values can be stored.
        "volume_orientation": (await database.get_setting("volume_orientation")) or "horizontal",
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
    # Radio Mode (2026-08-11 plan U8, R9): let guests start/switch radio
    # stations. Default OFF; persisted "1"/"0"; server-enforced in
    # app/api/radio.py (guest stop + browse always allowed). Client dim is
    # cosmetic — the route is the enforcement.
    guest_radio_control: bool | None = None
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
    # Output-session supervisor + gapless (2026-07-11 plan U5).
    # gapless_enabled: default-off master toggle (R10), persisted "1"/"0" and
    # live-applied via state.set_gapless_enabled (no restart; a flip bumps the
    # arming generation for U6's revoke lifecycle). resume_window_minutes: the
    # auto-resume window (R8; >= 1, default 60) consumed at decision time by
    # U3's supervisor — the >= 1 floor is enforced in update_settings (mirrors
    # most_played_display_limit).
    gapless_enabled: bool | None = None
    resume_window_minutes: int | None = None
    # Volume bar orientation (2026-08-04 volume rework U3): install-wide render
    # orientation for the admin volume control. Literal → 422 on a bad value.
    # Deliberately a single global setting — no per-device override (the
    # surface is 1-2 admins) and no appearance broadcast (other open admin
    # devices pick it up on next load; the saving admin applies it live).
    volume_orientation: Literal['horizontal', 'vertical'] | None = None


@router.post("/settings")
async def update_settings(body: SettingsRequest):
    # ── validation, ALL of it, before ANY persist/live-apply ─────────────────
    # A mixed request must be atomic on the invalid path: every field check
    # runs up front so a 4xx applies NOTHING (previously gapless_enabled was
    # persisted+live-applied before resume_window_minutes' floor check raised,
    # partially applying the request). Valid requests are untouched — the
    # apply blocks below run in their original order.
    eb: QueueEndBehavior | None = None
    if body.queue_end_behavior is not None:
        try:
            eb = QueueEndBehavior(body.queue_end_behavior)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid queue_end_behavior")

    # Random-pick length band (2026-06-20 plan U1): validate the EFFECTIVE pair
    # (incoming value wins; fall back to the stored value for an omitted side).
    # 0 = bound off; a band is only contradictory when BOTH ends are active and
    # min >= max.
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

    if body.popular_random_threshold is not None and body.popular_random_threshold < 1:
        raise HTTPException(
            status_code=422, detail="popular_random_threshold must be >= 1"
        )

    if body.most_played_display_limit is not None and body.most_played_display_limit < 1:
        raise HTTPException(
            status_code=422, detail="most_played_display_limit must be >= 1"
        )

    # International rail thresholds: >= 1 floor (same shape as
    # popular_random_threshold); the mode is Literal-validated by Pydantic.
    for _field, _key in (
        (body.rail_artist_threshold, "rail_artist_threshold"),
        (body.rail_album_threshold, "rail_album_threshold"),
    ):
        if _field is not None and _field < 1:
            raise HTTPException(status_code=422, detail=f"{_key} must be >= 1")

    # Auto-resume window (supervisor plan U5): >= 1 floor, same validation
    # shape as most_played_display_limit.
    if body.resume_window_minutes is not None and body.resume_window_minutes < 1:
        raise HTTPException(
            status_code=422, detail="resume_window_minutes must be >= 1"
        )

    # ── apply (validated above; original order preserved) ────────────────────
    if eb is not None:
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

    # Random-pick length band (2026-06-20 plan U1); the effective-pair
    # validation ran up top, so a rejected request wrote nothing.
    if body.random_min_seconds is not None:
        await database.set_setting("random_min_seconds", str(body.random_min_seconds))
    if body.random_max_seconds is not None:
        await database.set_setting("random_max_seconds", str(body.random_max_seconds))

    # Queue-end rework (2026-06-21 plan U2): Popular Random threshold + the opt-in
    # length-limit gate for the queue-end random modes.
    if body.popular_random_threshold is not None:
        await database.set_setting(
            "popular_random_threshold", str(body.popular_random_threshold)
        )

    if body.queue_end_length_limit is not None:
        await database.set_setting(
            "queue_end_length_limit", "1" if body.queue_end_length_limit else "0"
        )

    # Most Played leaderboard size (2026-06-23): display-only floor of 1
    # (validated up top).
    if body.most_played_display_limit is not None:
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

    # Arming lifecycle (2026-07-11 supervisor plan U6, R21): the Closing Time
    # config is an arming input — any edit revokes a device-armed next
    # outright (the armed decision was made under the OLD config) and
    # re-evaluates, so a next armed past a freshly-configured send-off track
    # can never survive the edit. No-op when nothing is armed.
    if any(v is not None for v in (
        body.closing_time_enabled, body.closing_time_title,
        body.closing_time_artist, body.closing_time_message,
    )):
        await state.notify_closing_config_changed()

    # International rail (2026-06-22 plan 004): alpha-rail mode + per-rail first-char
    # thresholds (the >= 1 floor validated up top).
    if body.rail_alpha_mode is not None:
        await database.set_setting("rail_alpha_mode", body.rail_alpha_mode)

    for _field, _key in (
        (body.rail_artist_threshold, "rail_artist_threshold"),
        (body.rail_album_threshold, "rail_album_threshold"),
    ):
        if _field is not None:
            await database.set_setting(_key, str(_field))

    # Gapless toggle (2026-07-11 supervisor plan U5): persist + live-apply,
    # queue_end_behavior-style. The state setter bumps the arming generation
    # on a real flip so U6's device-side armed next is revoked; no playback
    # path consults the flag yet, so toggle-off stays byte-identical.
    if body.gapless_enabled is not None:
        await database.set_setting(
            "gapless_enabled", "1" if body.gapless_enabled else "0"
        )
        state.set_gapless_enabled(body.gapless_enabled)

    # Auto-resume window (supervisor plan U5): >= 1 floor validated up top.
    # Decision-time read (U3's supervisor calls
    # database.get_resume_window_minutes at resume time) — no live-apply
    # machinery needed.
    if body.resume_window_minutes is not None:
        await database.set_setting(
            "resume_window_minutes", str(body.resume_window_minutes)
        )

    # Volume bar orientation (2026-08-04 volume rework U3): persist only —
    # the saving admin's client applies it locally on save success; no
    # broadcast by design (see SettingsRequest comment).
    if body.volume_orientation is not None:
        await database.set_setting("volume_orientation", body.volume_orientation)

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
                  "guest_radio_control",
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
