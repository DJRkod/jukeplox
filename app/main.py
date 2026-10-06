import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import _build_info
from app.config import settings
from app.database import close_db, init_db

# Configure the root logger so app loggers (app.output.airplay,
# app.output.dlna, etc.) actually emit INFO-level messages to the
# container's stdout. Without this Python's default WARNING level
# silently drops every _log.info() call in the codebase — including
# the cliap2 stderr reader's output, which is the primary diagnostic
# surface for the AirPlay backend.
#
# Respects the LOG_LEVEL env var (the Dockerfile sets it to "info").
# Falls back to INFO when unset so dev runs without env config still
# get useful output.
_log_level_name = os.environ.get("LOG_LEVEL", "info").upper()
logging.basicConfig(
    level=getattr(logging, _log_level_name, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Log build info as the very first lifespan event so it shows up in
    # `docker logs` before any backend setup noise. Greppable banner:
    # `docker logs jukeplox | grep "Jukeplox build:"` returns one line
    # per restart with the exact commit and build timestamp running.
    logging.getLogger("app.main").info(_build_info.as_log_line())
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    await init_db()
    from app import state
    await state.setup()
    # Browse-index plan U6: warm the persistent browse index at startup so the
    # first guest browse is fast instead of paying the full cross-server crawl.
    # Fire-and-forget + single-flighted; never blocks startup.
    state.trigger_browse_index_refresh()
    # Multi-source catalog (plan U6): warm the unified catalog at startup too,
    # alongside the browse index. Fire-and-forget + single-flighted.
    state.trigger_catalog_refresh()
    # Enabled-libraries cache (2026-07-18 review): warm it at startup so the
    # first guest search finds a populated cache instead of paying the cold
    # in-path Plex-listing block. Fire-and-forget + single-flighted.
    from app.api.guest import warm_enabled_libraries
    warm_enabled_libraries()
    # Live device discovery (2026-06-11 plan U2): start the watcher AFTER
    # state.setup() so the backend singletons its register_resolved hooks
    # feed already exist. Fail-soft — a broken watcher must never take the
    # app down; we log and continue in degraded (pull-only) mode.
    try:
        from app.output.watcher import start_watcher
        await start_watcher()
    except Exception:
        logging.getLogger("app.main").warning(
            "device watcher failed to start — live discovery degraded",
            exc_info=True,
        )
    yield
    try:
        from app.output.watcher import stop_watcher
        await stop_watcher()
    except Exception:
        logging.getLogger("app.main").warning(
            "device watcher shutdown failed", exc_info=True)
    await _release_output_backends()
    await close_db()


async def _release_output_backends() -> None:
    """Shutdown is the second edge where output ownership ends (2026-08-20
    plan U6, R3/R6). Until now a backend holding a live connection at process
    exit was simply abandoned: a Cast SocketClient thread still re-dialling,
    an aiohttp session and a bound GENA callback socket, a Companion client.

    EVERY constructed backend, not just the router's active one. A backend the
    user switched away from should already have been released on the switch,
    but that release is best-effort by contract — building shutdown on the
    assumption it succeeded would leave exactly the connections most likely to
    be stuck. Release is idempotent, so releasing an already-released backend
    costs a null check.

    Runs AFTER the watcher stops: discovery is quiet first, so nothing can
    hand a backend a freshly-resolved address while it is being let go. And
    BEFORE ``close_db()``, because it is still on a live event loop here —
    which is the whole reason the drain below can work at all.

    The drain is the counterpart to release's sync signature: the teardown
    steps that are genuinely coroutines were handed to background tasks that
    would otherwise never be scheduled before the loop closes. It is bounded,
    so an unreachable renderer delays exit by seconds, not indefinitely."""
    log = logging.getLogger("app.main")
    try:
        from app import state
        from app.output.base import drain_release_tasks
        for backend in state.all_output_backends():
            try:
                backend.release()
            except Exception:
                log.warning("shutdown: %s release() failed",
                            type(backend).__name__, exc_info=True)
        await drain_release_tasks()
    except Exception:
        log.warning("shutdown: output backend release failed", exc_info=True)


app = FastAPI(title="Jukeplox", lifespan=lifespan)

app.mount("/static", StaticFiles(directory="static"), name="static")

from app.api.auth_routes import router as auth_router
from app.api.admin import router as admin_router, admin_ws_router, page_router as admin_page_router
from app.api.guest import router as guest_router
from app.api.stream import router as stream_router
from app.api.playback import router as playback_router
from app.api.radio import guest_router as radio_guest_router, admin_router as radio_admin_router

app.include_router(auth_router)
app.include_router(admin_page_router)
app.include_router(admin_router)
app.include_router(admin_ws_router)
app.include_router(guest_router)
app.include_router(stream_router)
app.include_router(playback_router)
# Radio Mode (radio plan U7): guest browse/current/stop/play/switch (guest_router,
# no auth; play/switch gated server-side) + admin play/switch/stop (require_admin).
app.include_router(radio_guest_router)
app.include_router(radio_admin_router)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/version")
async def version():
    """Build info for the running container — git SHA, build timestamp,
    image tag. No auth: the data is non-sensitive and is the answer to
    'did my deploy actually pick up the latest image?'. Curl-friendly:
    `curl http://<host>/api/version` returns JSON."""
    return _build_info.as_dict()
