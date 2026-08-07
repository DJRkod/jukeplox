import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum

from app.models import Track


class QueueEndBehavior(str, Enum):
    STOP = "stop"
    POPULAR_RANDOM = "popular_random"
    FULL_RANDOM = "full_random"


# Legacy stored values retired in the 2026-06-21 queue-end rework. Mapped at the
# read edge (startup restore + admin GET echo) so existing installs migrate
# without a crash: shuffle behaved like the whole-library floor → FULL_RANDOM;
# repeat (history replay) has no successor → STOP.
_LEGACY_BEHAVIOR_MAP = {"shuffle": QueueEndBehavior.FULL_RANDOM,
                        "repeat": QueueEndBehavior.STOP}


def coerce_queue_end_behavior(stored: str | None) -> QueueEndBehavior:
    """Map a stored queue_end_behavior string to a current enum value.

    Valid current values pass through; legacy ``shuffle``/``repeat`` migrate;
    anything else (including ``None`` / unknown) falls back to STOP."""
    if stored in _LEGACY_BEHAVIOR_MAP:
        return _LEGACY_BEHAVIOR_MAP[stored]
    try:
        return QueueEndBehavior(stored)
    except ValueError:
        return QueueEndBehavior.STOP


@dataclass
class QueueItem:
    track: Track
    added_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    # Output-session supervisor plan U2 (R19): True when this item's play was
    # already counted before an outage hold re-front-inserted it — the resume
    # dispatch bypasses the play-count chokepoint so the play never counts
    # twice. Persisted inside metadata_json (no schema change) so the mark
    # survives a restart with the held item at the queue front (R18).
    play_recorded: bool = False

    @property
    def track_id(self) -> str:
        return self.track.id

    def to_dict(self) -> dict:
        return {
            "track_id": self.track.id,
            "metadata_json": json.dumps({
                "id": self.track.id,
                "title": self.track.title,
                "artist": self.track.artist,
                "album": self.track.album,
                "duration_ms": self.track.duration_ms,
                "genre": self.track.genre,
                "year": self.track.year,
                "thumb": self.track.thumb,
                "stream_key": self.track.stream_key,
                "server_name": self.track.server_name,
                # Multi-source plan U9: persist the holds snapshot so play-time
                # fallback survives a restart/restore.
                "holds": self.track.holds,
                # Supervisor plan U2 (R19): item-level held-play mark; rides
                # the metadata blob to avoid a queue_state schema migration.
                "play_recorded": self.play_recorded,
            }),
            "added_at": self.added_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "QueueItem":
        meta = json.loads(data["metadata_json"])
        track = Track(
            id=meta["id"],
            title=meta["title"],
            artist=meta["artist"],
            album=meta["album"],
            duration_ms=meta["duration_ms"],
            genre=meta.get("genre"),
            year=meta.get("year"),
            thumb=meta.get("thumb"),
            stream_key=meta.get("stream_key", ""),
            server_name=meta.get("server_name", ""),
            holds=meta.get("holds", []) or [],
        )
        return cls(track=track, added_at=data["added_at"],
                   play_recorded=bool(meta.get("play_recorded", False)))


@dataclass
class PlaybackState:
    current: QueueItem | None = None
    is_playing: bool = False
    is_paused: bool = False
