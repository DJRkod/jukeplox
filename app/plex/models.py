"""Back-compat re-export shim.

The canonical home for these source-neutral models is ``app/models.py``
(relocated 2026-06-27, plan U1). New code should import from ``app.models``;
this shim keeps ``from app.plex.models import ...`` working during the
multi-source transition and will be removed once all importers are migrated.
"""

from app.models import Album, Artist, Library, SearchResults, Track  # noqa: F401

__all__ = ["Album", "Artist", "Library", "SearchResults", "Track"]
