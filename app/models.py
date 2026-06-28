"""Source-neutral library data models.

Canonical home for the dataclasses that flow through the queue, catalog, and
playback paths. Relocated here from ``app/plex/models.py`` (2026-06-27, plan U1)
so no queue/catalog/playback code path imports from ``app/plex/``. The Plex
provider still produces and consumes these; they are not Plex-specific in shape.
``app/plex/models.py`` re-exports these names for back-compat during the
transition.
"""

from dataclasses import dataclass, field


@dataclass
class Library:
    key: str        # compound: "{machine_id}:{section_key}" when multi-server
    title: str
    type: str       # "artist" for music sections
    owner: str = ""       # Plex username of the server owner
    server_name: str = "" # server friendly name


@dataclass
class Artist:
    id: str
    title: str
    thumb: str | None = None
    # Album count from Plex's childCount (2026-06-09 rail plan R15 — the
    # "N releases" row subtitle). None when the server omits the field or
    # the value is unusable; the UI omits the subtitle in that case.
    release_count: int | None = None


@dataclass
class Album:
    id: str
    title: str
    artist: str
    year: int | None = None
    thumb: str | None = None
    subtype: str | None = None  # "album", "single", "ep", "live", "compilation", etc.
    # Plex addedAt (epoch seconds) — when this copy was added to its library.
    # Recently Added plan U1; None when the server omits the field.
    added_at: int | None = None
    # Plex leafCount (total tracks), childCount fallback — the cheap content
    # signal that distinguishes same-title releases (different masters/editions)
    # for grouping and cross-server folding. Same-title plan U1; None when the
    # server omits both fields.
    track_count: int | None = None
    # Collected-library plan U2: per-server availability for this identity —
    # [{"server_name": str, "album_id": str}], priority-ordered. Attached by
    # the grouping layer in app/api/guest.py; None on raw client results.
    sources: list | None = None


@dataclass
class Track:
    id: str
    title: str
    artist: str  # per-track credited act when Plex carries one, else the release artist
    album: str
    duration_ms: int
    genre: str | None = None
    year: int | None = None
    thumb: str | None = None
    stream_key: str = ""  # relative path to media part, e.g. /library/parts/123/file.flac
    server_name: str = ""  # human-readable server name for multi-server source picker
    # 2026-06-10 per-track credits plan U1: the release (album) artist and the
    # parent album's made id — the credit-index scanner needs both to tell a
    # per-track credit apart from the release artist and map it to a release.
    album_artist: str | None = None
    album_id: str | None = None
    # 2026-06-11 multi-disc ordering: Plex carries the disc as parentIndex
    # and the track number as index on track items (verified live). Defaults
    # cover servers/items that omit them (single-disc albums often do).
    disc_number: int = 1
    track_number: int | None = None


@dataclass
class SearchResults:
    tracks: list[Track] = field(default_factory=list)
    albums: list[Album] = field(default_factory=list)
    artists: list[Artist] = field(default_factory=list)
