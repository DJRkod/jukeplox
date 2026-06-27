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
    await close_db()


app = FastAPI(title="Jukeplox", lifespan=lifespan)

app.mount("/static", StaticFiles(directory="static"), name="static")

from app.api.auth_routes import router as auth_router
from app.api.admin import router as admin_router, admin_ws_router, page_router as admin_page_router
from app.api.guest import router as guest_router
from app.api.stream import router as stream_router
from app.api.playback import router as playback_router

app.include_router(auth_router)
app.include_router(admin_page_router)
app.include_router(admin_router)
app.include_router(admin_ws_router)
app.include_router(guest_router)
app.include_router(stream_router)
app.include_router(playback_router)


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
