import secrets
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    data_dir: Path = Path("/data")
    secret_key: str = ""
    log_level: str = "info"
    session_ttl_hours: int = 8
    cookie_secure: bool = False  # set True when serving over HTTPS
    bind_host: str = "0.0.0.0"  # mirrors BIND_HOST used by uvicorn; non-0.0.0.0 enables stream proxy auto-detection
    stream_base_url: str = ""   # overrides bind_host-derived proxy URL when set explicitly
    # Maximum size of the on-disk art cache under data_dir/art-cache/ in MB.
    # Plex art URLs embed a version stamp so entries are content-addressed;
    # LRU eviction at this cap is the only size-management mechanism.
    art_cache_size_mb: int = 512
    # Hard ceiling on concurrent outbound requests Jukeplox makes to ANY single
    # Plex server, so a fan-out (genre/credit recompute, search, art grid) can't
    # flood a small/shared server. Enforced per PlexClient — see app/plex/client.py.
    plex_max_concurrency: int = 6

    @model_validator(mode="after")
    def _resolve_secret_key(self) -> "Settings":
        if self.secret_key:
            return self
        key_file = self.data_dir / "secret.key"
        try:
            if key_file.exists():
                self.secret_key = key_file.read_text().strip()
            else:
                key = secrets.token_hex(32)
                self.data_dir.mkdir(parents=True, exist_ok=True)
                key_file.write_text(key)
                self.secret_key = key
        except OSError:
            self.secret_key = secrets.token_hex(32)
        return self

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jukeplox.db"


settings = Settings()
