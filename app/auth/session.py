import asyncio
import secrets
from datetime import UTC, datetime, timedelta

from app import database
from app.config import settings


def _now_str() -> str:
    return datetime.now(UTC).isoformat()


def _expires_str() -> str:
    return (datetime.now(UTC) + timedelta(hours=settings.session_ttl_hours)).isoformat()


async def create_session() -> str:
    token = secrets.token_hex(32)
    await database.create_session(token, _now_str(), _expires_str())
    return token


async def validate_session(token: str) -> bool:
    session = await database.get_session(token)
    if not session:
        return False
    expires_at = datetime.fromisoformat(session["expires_at"])
    if datetime.now(UTC) >= expires_at:
        return False
    asyncio.create_task(clean_expired())
    return True


async def invalidate_session(token: str) -> None:
    await database.delete_session(token)


async def clean_expired() -> None:
    await database.delete_expired_sessions(_now_str())
