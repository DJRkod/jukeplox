"""Radio Mode HTTP surface (radio plan U7).

The browse/search/current-station read surface plus start/switch/stop control,
for both guest (unauthenticated, read-only-visible + always-stop; start/switch
gated by ``guest_radio_control``) and admin (``require_admin`` override). Also
the ``RadioStateEvent`` broadcast wiring (see ``app/state.py`` — the transition
+ title + failed hooks are registered there during ``setup()``) and the shape of
the now-playing ``radio`` snapshot block (assembled by :func:`radio_snapshot`).

Auth posture (R9)
-----------------
- GUEST, no auth:
  - ``GET  /api/radio/stations``  — browse/search + curated landing (rate-limited).
  - ``GET  /api/radio/current``   — active station + status + live_title (or idle).
  - ``POST /api/radio/stop``      — ALWAYS allowed (R9 guest stop).
- GUEST, gated by ``guest_radio_control`` (403 when off):
  - ``POST /api/radio/play``      — start (or first-takeover) a station.
  - ``POST /api/radio/switch``    — instant-switch to another station.
- ADMIN, ``require_admin`` (always permitted — admin override):
  - ``POST /admin/radio/play|switch|stop``.

stationuuid → Station resolution (SG note for U9/U10)
-----------------------------------------------------
The play/switch body carries the full station fields the browse response already
handed the client (``Station.to_dict()`` shape). We reconstruct a
:class:`~app.radio.client.Station` from that body via ``Station.from_json`` — NO
extra Radio Browser round-trip on the play path (keeps the good-citizen fetch
budget down and avoids a second SSRF surface; the URL is SSRF-validated inside
``RadioSession.start`` regardless). ``stationuuid`` is required; a body missing a
playable URL (both ``url_resolved`` and ``url`` empty) is rejected 400. U9/U10
MUST post back the station object they rendered from ``GET /api/radio/stations``.

Directory-unavailable (R13) / station-offline (R12)
---------------------------------------------------
``RadioDirectoryUnavailable`` from the client (total discovery failure) surfaces
as an explicit ``{"unavailable": true, ...}`` body on ``GET /api/radio/stations``
(HTTP 200 — a typed state the UI renders, not a 500). Station-offline surfaces as
``status == "failed"`` in ``GET /api/radio/current`` and the ``RadioStateEvent``.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from app import state
from app.api.auth_routes import require_admin
from app.radio.client import RadioDirectoryUnavailable, Station, get_radio_client

_log = logging.getLogger("jukeplox.radio")

# Guest browse routers are unauthenticated; admin routes carry require_admin.
guest_router = APIRouter(tags=["radio"])
admin_router = APIRouter(prefix="/admin", tags=["radio"],
                         dependencies=[Depends(require_admin)])


# ── per-IP rate limit for GET /api/radio/stations (SEC-003) ─────────────────────
# The station browse route is unauthenticated and proxies params into outbound
# Radio Browser calls; distinct queries bypass the client SWR cache, so a guest
# pulsing distinct searches could drive outbound fan-out and risk a directory-side
# IP ban. The U1 semaphore bounds CONCURRENCY, not RATE — this leaky/token bucket
# bounds rate. Mirrors auth_routes._check_rate_limit (in-process, resets on
# restart; sufficient for single-host deployments).
_STATIONS_RATE_MAX = 10          # requests allowed per window, per IP
_STATIONS_RATE_WINDOW_S = 10.0   # sliding window length
_stations_hits: dict[str, list[float]] = {}


def _check_stations_rate_limit(ip: str) -> None:
    now = time.monotonic()
    cutoff = now - _STATIONS_RATE_WINDOW_S
    hits = [t for t in _stations_hits.get(ip, ()) if t > cutoff]
    if len(hits) >= _STATIONS_RATE_MAX:
        _stations_hits[ip] = hits  # keep the (still-active) window
        raise HTTPException(
            status_code=429,
            detail="Too many radio browse requests — slow down",
        )
    hits.append(now)
    _stations_hits[ip] = hits
    # F12: opportunistically drop OTHER IP keys whose entire window has aged out,
    # so a churn of one-shot IPs can't grow the map unbounded. Bounded to the
    # already-stored keys; the current IP was just refreshed above so it survives.
    stale = [k for k, v in _stations_hits.items()
             if k != ip and not any(t > cutoff for t in v)]
    for k in stale:
        del _stations_hits[k]


def _reset_stations_rate_limit() -> None:
    """Test hook: clear the per-IP browse-rate buckets."""
    _stations_hits.clear()


# ── curated-landing policy (R3) — liveness-recheck / tag-filter lives here ──────
# The U1 client returns RAW tags + topclick data; the curated "popular" policy
# (SG-07) lives in THIS API layer so U9 treats the curated result as opaque and
# "popular" can be re-tuned without touching the client. Kept intentionally small:
# genre quick-picks (top tags by count) + a lastcheckok-filtered popular set.
_LANDING_TAG_COUNT = 24          # genre quick-picks surfaced on the landing
_LANDING_POPULAR_COUNT = 30      # popular stations after the liveness filter


def _curated_landing_tags(raw_tags: list) -> list[dict]:
    """Genre quick-picks: the top ``_LANDING_TAG_COUNT`` tags by station count.

    ``raw_tags`` is the client's ``get_tags()`` shape (``[{name, stationcount},
    …]``). Tolerant of missing/garbage rows (a directory can return odd data)."""
    picks: list[dict] = []
    for item in raw_tags or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        try:
            count = int(item.get("stationcount") or 0)
        except (ValueError, TypeError):
            count = 0
        picks.append({"name": name, "stationcount": count})
    picks.sort(key=lambda p: p["stationcount"], reverse=True)
    return picks[:_LANDING_TAG_COUNT]


def _curated_landing_popular(stations: list[Station]) -> list[dict]:
    """Liveness-rechecked popular set: topclick stations, ``lastcheckok``-filtered
    (a cheap, bounded liveness signal — NOT a per-station stream probe, which
    would be a directory rate-limit/fan-out risk), capped."""
    live = [s for s in stations if s.lastcheckok]
    # If the directory reported none live (odd), fall back to the raw set rather
    # than showing an empty popular row.
    chosen = live or list(stations)
    return [s.to_dict() for s in chosen[:_LANDING_POPULAR_COUNT]]


# ── request bodies ─────────────────────────────────────────────────────────────


class StationBody(BaseModel):
    """The station a play/switch targets — the ``Station.to_dict()`` shape the
    browse response handed the client (see the module docstring's resolution
    note). ``stationuuid`` required; a playable URL (url_resolved or url) required.
    Extra fields are ignored so the client can post the whole station object."""
    model_config = {"extra": "ignore"}

    stationuuid: str
    name: str = ""
    url: str = ""
    url_resolved: str = ""
    favicon: str = ""
    codec: str = ""
    bitrate: int = 0
    countrycode: str = ""
    # F11: carry tags + lastcheckok through the play/switch round-trip so the
    # snapshot/WS station dict preserves them (extra='ignore' would otherwise zero
    # them — the client renders the station it posted back).
    tags: list[str] = []
    lastcheckok: bool = False

    def to_station(self) -> Station:
        if not (self.url_resolved or self.url):
            raise HTTPException(
                status_code=400,
                detail="Station body carries no playable URL "
                       "(url_resolved/url both empty)",
            )
        return Station.from_json(self.model_dump())


# ── snapshot block (late-join resync; R12/R13) ─────────────────────────────────


def radio_snapshot() -> dict:
    """The ``radio`` block for the now-playing GET snapshots (guest + admin).

    IDENTICAL for both audiences (SG-05). A fresh / WS-gap client converges from
    this without a live ``RadioStateEvent``. Shape::

        {"active": bool, "station": dict|None, "status": str, "live_title": str|None}

    A transient snapshot-fetch failure MUST keep the last-known station — this
    reads the in-process session singleton (no I/O), so it can't transiently
    fail; the "keep last-known" contract is honored by the client on its own
    fetch error (realtime-ui-resync learning)."""
    sess = state.radio_session
    station = sess.station
    return {
        "active": sess.is_active(),
        "station": station.to_dict() if station is not None else None,
        "status": sess.status(),
        "live_title": sess.current_title(),
    }


# ── guest: browse / current / stop ─────────────────────────────────────────────


@guest_router.get("/api/radio/stations")
async def radio_stations(
    request: Request,
    q: str | None = Query(default=None, description="free-text station name search"),
    tag: str | None = Query(default=None, description="exact tag/genre filter"),
    countrycode: str | None = Query(default=None, description="ISO country code filter"),
    limit: int = Query(default=100, ge=1, le=200),
):
    """Browse/search stations, or the curated landing when no query.

    Rate-limited per IP (SEC-003). On total directory failure returns an explicit
    ``{"unavailable": true}`` state (R13), not a 500."""
    ip = request.client.host if request.client else "unknown"
    _check_stations_rate_limit(ip)

    client = get_radio_client()
    try:
        if tag:
            stations = await client.stations_by_tag_exact(tag, limit=limit)
            return {"unavailable": False, "mode": "search",
                    "stations": [s.to_dict() for s in stations]}
        if countrycode:
            stations = await client.stations_by_countrycode_exact(
                countrycode, limit=limit)
            return {"unavailable": False, "mode": "search",
                    "stations": [s.to_dict() for s in stations]}
        if q:
            stations = await client.search_stations(name=q, limit=limit)
            return {"unavailable": False, "mode": "search",
                    "stations": [s.to_dict() for s in stations]}
        # No query → curated landing (R3): genre quick-picks + popular set. The
        # liveness/tag-filter POLICY lives here (SG-07); the client returns raw.
        raw_tags = await client.get_tags()
        popular = await client.get_top_click_cached()
        return {
            "unavailable": False,
            "mode": "landing",
            "tags": _curated_landing_tags(raw_tags),
            "popular": _curated_landing_popular(popular),
        }
    except RadioDirectoryUnavailable:
        # R13: total discovery failure — an explicit typed state the UI renders,
        # NOT a 500. HTTP 200 so the client parses the body cleanly.
        _log.warning("radio: directory unavailable on /api/radio/stations")
        return {"unavailable": True, "mode": "landing",
                "tags": [], "popular": [], "stations": []}


@guest_router.get("/api/radio/current")
async def radio_current():
    """The active station + status + live_title (or idle). Same block the
    now-playing snapshot carries — the dedicated poll surface for a client that
    doesn't refetch the whole now-playing GET."""
    return radio_snapshot()


@guest_router.post("/api/radio/stop")
async def radio_stop():
    """Stop the station and resume the held queue. ALWAYS allowed for guests
    (R9) — no toggle check. Idempotent (a no-op when already idle)."""
    await state.radio_session.stop()
    return {"ok": True, "radio": radio_snapshot()}


async def _guest_radio_control_enabled() -> bool:
    """Whether guests may start/switch stations (R9). U8 owns the Setup UI; the
    accessor already lives in ``app.database`` (default off)."""
    from app import database
    return await database.get_guest_radio_control()


async def _radio_start(body: StationBody) -> dict:
    """F16: the single start/switch body shared by guest play/switch (after the
    gate) AND admin play/switch — ``start()`` is the one entry point (it detects
    active → instant switch, no re-hold), so play and switch can't diverge. Kept
    as separate routes; the body is one function so they stay identical."""
    await state.radio_session.start(body.to_station())
    return {"ok": True, "radio": radio_snapshot()}


@guest_router.post("/api/radio/play")
async def radio_play(body: StationBody):
    """Guest start/first-takeover a station. Gated by ``guest_radio_control`` —
    403 when off (R9, enforced server-side; the UI dim is cosmetic)."""
    if not await _guest_radio_control_enabled():
        raise HTTPException(status_code=403,
                            detail="Guest radio control is disabled")
    return await _radio_start(body)


@guest_router.post("/api/radio/switch")
async def radio_switch(body: StationBody):
    """Guest instant-switch to another station (R5). Same gate as play — a switch
    is a control action, so it is 403 when ``guest_radio_control`` is off."""
    if not await _guest_radio_control_enabled():
        raise HTTPException(status_code=403,
                            detail="Guest radio control is disabled")
    return await _radio_start(body)


# ── admin: play / switch / stop (always permitted — admin override) ─────────────


@admin_router.post("/radio/play")
async def admin_radio_play(body: StationBody):
    return await _radio_start(body)


@admin_router.post("/radio/switch")
async def admin_radio_switch(body: StationBody):
    return await _radio_start(body)


@admin_router.post("/radio/stop")
async def admin_radio_stop():
    await state.radio_session.stop()
    return {"ok": True, "radio": radio_snapshot()}
