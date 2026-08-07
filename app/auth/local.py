import bcrypt

from app import database

_HASH_KEY = "admin_local_password_hash"


async def set_password(plain: str) -> None:
    hashed = bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()
    await database.set_setting(_HASH_KEY, hashed)


async def verify_password(plain: str) -> bool:
    stored = await database.get_setting(_HASH_KEY)
    if not stored:
        return False
    return bcrypt.checkpw(plain.encode(), stored.encode())


async def has_password() -> bool:
    return bool(await database.get_setting(_HASH_KEY))
