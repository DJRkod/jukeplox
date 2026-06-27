from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class NowPlayingEvent:
    type: str = "now_playing_changed"
    track_id: str | None = None
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    album_id: str | None = None
    thumb: str | None = None
    duration_ms: int = 0
    is_playing: bool = False
    is_paused: bool = False
    server_name: str | None = None

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class QueueItem:
    track_id: str
    title: str
    artist: str
    album: str
    thumb: str | None = None
    duration_ms: int = 0
    server_name: str = ""
    album_id: str | None = None
    # Per-entry receipt half (remove-own-queued-tracks U4): lets the guest UI
    # keep its remove (✕) on owned rows across queue_changed re-renders, which
    # paint straight from this WS payload (not a refetch). Unused for history
    # rows, which the guest never makes removable.
    added_at: str = ""


@dataclass
class QueueChangedEvent:
    type: str = "queue_changed"
    queue: list[QueueItem] = field(default_factory=list)
    history: list[QueueItem] = field(default_factory=list)
    is_locked: bool = False

    def to_json(self) -> dict:
        return asdict(self)

    def truncated(self, n: int | None, m: int | None) -> "QueueChangedEvent":
        """Return a copy with queue/history capped to N and M entries (None = unlimited)."""
        q = self.queue[:n] if n is not None else self.queue
        h = self.history[:m] if m is not None else self.history
        return QueueChangedEvent(type=self.type, queue=q, history=h, is_locked=self.is_locked)


@dataclass
class AppearanceChangedEvent:
    """Admin changed a default appearance knob (2026-06-11 glow-up plan
    U1). Carries BOTH current defaults so clients re-resolve in one shot;
    devices with a local override keep it (the engine decides)."""
    type: str = "appearance_changed"
    scheme: str = "gold-rush"
    rail_mode: str = "vanilla"
    view: str = "list"
    # Server-side feature flag (not a per-device display knob) carried on this
    # event so toggling it reaches connected clients live (code-review #6).
    surprise_me_enabled: bool = True
    # International rail (2026-06-22 plan 004): install-wide alpha-rail mode +
    # per-rail thresholds, carried so an admin change reaches connected clients
    # live, the same way rail_mode does.
    rail_alpha_mode: str = "english"
    rail_artist_threshold: int = 2
    rail_album_threshold: int = 2
    # Rating display style (2026-06-27): carried so an admin change reaches
    # connected clients live (admin + guests), the same way scheme/view do.
    rating_style: str = "stars"

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class LockChangedEvent:
    type: str = "lock_changed"
    is_locked: bool = False

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class PlaybackStateEvent:
    type: str = "playback_state_changed"
    is_playing: bool = False
    is_paused: bool = False

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class OutputChangedEvent:
    type: str = "output_changed"
    backend_type: str = ""
    device_name: str = ""

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class VolumeChangedEvent:
    type: str = "volume_changed"
    level: float = 0.0

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class SurpriseRecordedEvent:
    """A Surprise Me pick was resolved + recorded — pushed to ADMINS so the Setup
    "Recent suggestions" readout updates live (no reload). Payload-carrying
    (mirrors DevicesChangedEvent): carries the same ``{recent, tally}`` shape as
    GET /admin/surprise/recent so the admin renders both the GET response and the
    push through one path."""
    type: str = "surprise_recorded"
    recent: list = field(default_factory=list)
    tally: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class DevicesChangedEvent:
    """Live output-device registry changed (2026-06-11 live-discovery plan U2).

    Broadcast on the ADMIN socket only (the device picker is admin chrome),
    debounced 750ms trailing-edge by the watcher so registry churn — a
    Chromecast announces several records at once — emits one frame (KTD5).

    ``devices`` carries ALREADY-SERIALIZED dicts: the watcher injects a
    snapshot-builder callable and broadcasts whatever it returns, so this
    event never needs to know the payload schema. U5 swaps the minimal
    default builder for the real aggregator (the GET /admin/output/devices
    body) without touching this type — payload-carrying by design so the
    frontend has ONE render path for both the GET response and the push.

    ``mdns_status`` mirrors admin.py's per-backend availability map
    (``{"airplay": "ok" | "unavailable", ...}``).
    """
    type: str = "devices_changed"
    devices: list = field(default_factory=list)
    mdns_status: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class ClosingTimeEvent:
    """Closing Time mode fired/cleared (2026-06-24 plan U2). Broadcast to ALL
    clients: ``active=True`` with the send-off ``message`` when the trigger song
    finishes and the queue freezes; ``active=False`` (empty message) when the
    admin resumes. Clients show/hide the closing banner accordingly; a fresh
    client renders from the ``closing_active``/``closing_message`` snapshot
    fields instead (see the now-playing GETs)."""
    type: str = "closing_time"
    active: bool = False
    message: str = ""

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class AirPlayProtocolChangedEvent:
    """Per-device AirPlay protocol decision changed (cliap2 vs cliraop).

    Broadcast on probe completion, on user click of the "No audio?"
    button, and on Re-test results. The admin UI subscribes via the
    existing /admin/ws WebSocket and updates the protocol label
    in-place without a page reload.
    """
    type: str = "airplay_protocol_changed"
    device_id: str = ""
    protocol: str = ""  # "ap2" or "ap1"

    def to_json(self) -> dict:
        return asdict(self)
