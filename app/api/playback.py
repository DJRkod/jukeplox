"""Playback position and seek endpoints."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app import state
from app.api.auth_routes import require_admin

router = APIRouter(tags=["playback"])


@router.get("/api/playback/position")
async def playback_position():
    """Current playback position — accessible to guests for the progress bar."""
    s = state.queue_engine.state
    if not s.current:
        return {
            "position_ms": 0, "duration_ms": 0, "is_playing": False, "is_paused": False,
            # Closing Time (2026-06-24 plan U3): the freeze clears `current`, so a
            # client polling position while frozen still learns the banner state.
            "closing_active": state._closing_active,
            "closing_message": state._closing_message,
        }
    position_ms = 0
    try:
        position_ms = await state.output_router.get_position()
    except Exception:
        pass
    return {
        "position_ms": position_ms,
        "duration_ms": s.current.track.duration_ms,
        "is_playing": s.is_playing,
        "is_paused": s.is_paused,
        "closing_active": state._closing_active,
        "closing_message": state._closing_message,
    }


class SeekRequest(BaseModel):
    position_ms: int = Field(..., ge=0)


@router.post("/admin/playback/seek", dependencies=[Depends(require_admin)])
async def playback_seek(body: SeekRequest):
    """Seek to position — admin only."""
    from app import playback_control
    return await playback_control.playback_seek(body.position_ms)
