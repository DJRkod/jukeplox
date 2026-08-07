"""Module-level art-cache singleton.

Wires the ArtCache instance from the active Settings at import time so
callers can simply ``from app.cache import cache``. The cache directory
is not created until the first write — instantiation is cheap.
"""

from app.cache.art_cache import ArtCache
from app.config import settings

cache = ArtCache(
    data_dir=settings.data_dir / "art-cache",
    size_mb=settings.art_cache_size_mb,
)

__all__ = ["cache", "ArtCache"]
