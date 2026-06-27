import time
from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app import database
from app.auth import local as local_auth
from app.auth import session as session_mgr

router = APIRouter(prefix="/admin/auth", tags=["auth"])

# ── Brute-force guard ─────────────────────────────────────────────────────────
# In-process counter; resets on restart. Sufficient for single-host deployments.
_MAX_ATTEMPTS = 10
_LOCKOUT_SECONDS = 300
_fail_counts: dict[str, list[float]] = defaultdict(list)


def _check_rate_limit(ip: str) -> None:
    now = time.monotonic()
    cutoff = now - _LOCKOUT_SECONDS
    _fail_counts[ip] = [t for t in _fail_counts[ip] if t > cutoff]
    if len(_fail_counts[ip]) >= _MAX_ATTEMPTS:
        raise HTTPException(status_code=429, detail="Too many failed attempts — try again later")


def _record_failure(ip: str) -> None:
    _fail_counts[ip].append(time.monotonic())

SESSION_COOKIE = "jukeplox_session"


def _set_session_cookie(response: Response, token: str) -> None:
    from app.config import settings
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
    )


async def get_session_token(request: Request) -> str | None:
    return request.cookies.get(SESSION_COOKIE)


async def require_admin(request: Request) -> None:
    token = await get_session_token(request)
    if not token or not await session_mgr.validate_session(token):
        raise HTTPException(status_code=401, detail="Authentication required")


# ── setup (first-run only) ────────────────────────────────────────────────────

async def _is_setup_complete() -> bool:
    return bool(await database.get_setting("setup_complete"))


class SetupRequest(BaseModel):
    password: str = Field(..., min_length=1)


@router.post("/setup")
async def setup(body: SetupRequest, response: Response):
    if await _is_setup_complete():
        raise HTTPException(status_code=403, detail="Setup already complete")
    await local_auth.set_password(body.password)
    await database.set_setting("setup_complete", "1")
    token = await session_mgr.create_session()
    _set_session_cookie(response, token)
    return {"ok": True}


# ── local password ────────────────────────────────────────────────────────────

class LocalLoginRequest(BaseModel):
    password: str = Field(..., min_length=1)


@router.post("/login/local")
async def login_local(body: LocalLoginRequest, request: Request, response: Response):
    ip = request.client.host if request.client else "unknown"
    _check_rate_limit(ip)
    if not await local_auth.has_password():
        raise HTTPException(status_code=403, detail="No admin password configured")
    if not await local_auth.verify_password(body.password):
        _record_failure(ip)
        raise HTTPException(status_code=401, detail="Invalid password")
    _fail_counts.pop(ip, None)
    await database.set_setting("setup_complete", "1")
    token = await session_mgr.create_session()
    _set_session_cookie(response, token)
    return {"ok": True}


# ── change password ───────────────────────────────────────────────────────────

class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=1)


@router.post("/change-password", dependencies=[Depends(require_admin)])
async def change_password(body: ChangePasswordRequest):
    if not await local_auth.verify_password(body.current_password):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    await local_auth.set_password(body.new_password)
    await database.delete_all_sessions()
    return {"ok": True}


# ── logout ────────────────────────────────────────────────────────────────────

@router.post("/logout")
async def logout(request: Request, response: Response):
    token = await get_session_token(request)
    if token:
        await session_mgr.invalidate_session(token)
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}
