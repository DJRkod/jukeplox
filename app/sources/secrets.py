"""Credential seal/open chokepoint (plan U17 / R24).

Every stored source credential — the Plex token, the Jellyfin access token, any
future local-source secret — passes through this one helper: ``seal`` before it
touches the database, ``open_secret`` when it is handed to a provider. So a
single mechanism governs every credential at rest, and no caller hand-rolls its
own storage format.

Mechanism: application-level envelope encryption with Fernet (AES-128-CBC +
HMAC-SHA256, random IV per message). The symmetric key lives in a file OUTSIDE
the SQLite DB, created at mode ``0600`` (owner-only) on first use, so leaking the
DB file alone never yields a usable token (R24). The Jellyfin account PASSWORD is
never persisted at all — only the sealed token (enforced in app/database.py and
app/sources/jellyfin.py).

Design notes:
- Sealed values carry a ``SEALED_PREFIX`` so ``open_secret`` can tell a sealed
  value from a legacy plaintext one. That makes the upgrade migration trivial and
  safe: an un-migrated plaintext token still opens (returned as-is) until the
  one-time re-seal pass (database._migrate_seal_credentials) rewrites it.
- A lost/rotated key degrades ``open_secret`` to ``""`` (logged) rather than
  crashing the registry build — the affected source simply fails to authenticate
  and can be reconnected.
- No process-level key cache: seal/open run only on connect and registry build
  (rare), and re-reading a tiny key file keeps tests isolated and key rotation
  honored immediately.
"""

from __future__ import annotations

import logging
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

_log = logging.getLogger(__name__)

# Discriminator for sealed values. A real Plex/Jellyfin token (alphanumeric/hex)
# never starts with this, and a Fernet token (urlsafe-base64) never contains the
# colon-delimited prefix, so it unambiguously separates sealed from plaintext.
SEALED_PREFIX = "enc:fernet:"

_KEY_FILENAME = "credentials.key"


def _key_path() -> Path:
    """Path to the symmetric key file — alongside the DB dir but a SEPARATE file
    (never inside the SQLite DB). Resolved from the live settings so tests using a
    temp data_dir are isolated."""
    from app import database
    return Path(database.settings.data_dir) / _KEY_FILENAME


def _load_or_create_key() -> bytes:
    """Return the Fernet key bytes, creating the key file (0600) on first use."""
    path = _key_path()
    if path.exists():
        return path.read_bytes().strip()
    key = Fernet.generate_key()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create with owner-only permissions from the start (avoid a brief world-
    # readable window). os.open honors the mode on creation; chmod after covers
    # platforms/umasks that ignore it. Windows ignores POSIX bits — acceptable;
    # the file still sits outside the DB.
    import os
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass
    return key


def seal(plaintext: str | None) -> str | None:
    """Encrypt a credential for storage. ``None``/``""`` pass through unchanged
    (no credential to protect). The result carries ``SEALED_PREFIX``."""
    if not plaintext:
        return plaintext
    token = Fernet(_load_or_create_key()).encrypt(plaintext.encode("utf-8"))
    return SEALED_PREFIX + token.decode("ascii")


def open_secret(stored: str | None) -> str | None:
    """Decrypt a stored credential. ``None``/``""`` pass through. A value without
    ``SEALED_PREFIX`` is treated as legacy plaintext and returned as-is (so an
    un-migrated token still works). A sealed value that fails to decrypt (lost or
    rotated key, tampering) degrades to ``""`` with a warning rather than raising,
    so one bad credential never crashes the registry build."""
    if not stored:
        return stored
    if not stored.startswith(SEALED_PREFIX):
        return stored  # legacy plaintext — usable until the migration re-seals it
    body = stored[len(SEALED_PREFIX):].encode("ascii")
    try:
        return Fernet(_load_or_create_key()).decrypt(body).decode("utf-8")
    except (InvalidToken, ValueError):
        _log.warning("open_secret: a stored credential could not be decrypted "
                     "(key lost/rotated?) — treating as empty; reconnect the source")
        return ""


def is_sealed(stored: str | None) -> bool:
    """True when ``stored`` is already sealed — lets the migration skip rows that
    don't need re-sealing (idempotency)."""
    return bool(stored) and stored.startswith(SEALED_PREFIX)
