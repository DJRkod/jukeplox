"""U17: credential seal/open chokepoint (R24).

Every stored source credential is sealed by app.sources.secrets before it
touches the DB and opened only when handed to a provider. These tests pin the
round-trip, the non-plaintext-at-rest guarantee, legacy-plaintext passthrough
(so an un-migrated token still works), the key file living outside the DB at
0600, and graceful degradation when the key is lost.
"""

import os
import stat

import pytest

from app.config import Settings


@pytest.fixture
def sealed_env(tmp_path, monkeypatch):
    from app import database
    monkeypatch.setattr(database, "settings", Settings(data_dir=tmp_path, secret_key="test"))
    return tmp_path


def test_seal_open_round_trip(sealed_env):
    from app.sources import secrets
    sealed = secrets.seal("plex-token-abc123")
    assert sealed != "plex-token-abc123"
    assert secrets.open_secret(sealed) == "plex-token-abc123"


def test_sealed_value_is_not_plaintext_and_is_prefixed(sealed_env):
    from app.sources import secrets
    sealed = secrets.seal("supersecret")
    assert "supersecret" not in sealed
    assert sealed.startswith(secrets.SEALED_PREFIX)


def test_open_legacy_plaintext_passthrough(sealed_env):
    # An un-migrated plaintext token (no sealed prefix) must still be usable —
    # open_secret returns it as-is and never touches the key file.
    from app.sources import secrets
    assert secrets.open_secret("legacy-plaintext-token") == "legacy-plaintext-token"


def test_seal_open_none_and_empty(sealed_env):
    from app.sources import secrets
    assert secrets.seal("") == ""
    assert secrets.seal(None) is None
    assert secrets.open_secret("") == ""
    assert secrets.open_secret(None) is None


def test_key_file_outside_db_and_locked_down(sealed_env):
    from app import database
    from app.sources import secrets
    secrets.seal("x")  # first seal creates the key file
    kp = secrets._key_path()
    assert kp.exists()
    assert str(kp) != str(database.settings.db_path)  # distinct from the SQLite DB
    if os.name == "posix":  # Windows can't enforce POSIX mode bits
        assert stat.S_IMODE(kp.stat().st_mode) == 0o600


def test_open_with_lost_key_degrades_to_empty(sealed_env):
    # A lost/rotated key must not crash the app — open_secret degrades to "" so
    # the source simply fails to authenticate (and can be reconnected).
    from cryptography.fernet import Fernet
    from app.sources import secrets
    sealed = secrets.seal("tok")
    secrets._key_path().write_bytes(Fernet.generate_key())  # different key now
    assert secrets.open_secret(sealed) == ""


def test_seal_is_nondeterministic_but_opens_same(sealed_env):
    # Fernet embeds a random IV → two seals of the same token differ, both open.
    from app.sources import secrets
    a, b = secrets.seal("dup"), secrets.seal("dup")
    assert a != b
    assert secrets.open_secret(a) == secrets.open_secret(b) == "dup"
