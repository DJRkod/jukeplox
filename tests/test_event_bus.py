"""Tests for the WebSocket event bus."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.events.bus import ConnectionManager
from app.events.types import (
    AirPlayProtocolChangedEvent,
    DevicesChangedEvent,
    LockChangedEvent,
    NowPlayingEvent,
    QueueChangedEvent,
    QueueItem,
    VolumeChangedEvent,
)


def make_ws(fail=False) -> MagicMock:
    ws = MagicMock()
    if fail:
        ws.send_json = AsyncMock(side_effect=RuntimeError("disconnected"))
    else:
        ws.send_json = AsyncMock()
    return ws


# ── connect / disconnect ──────────────────────────────────────────────────────

def test_connect_adds_to_correct_set():
    mgr = ConnectionManager()
    admin_ws = make_ws()
    guest_ws = make_ws()
    mgr.connect(admin_ws, "admin")
    mgr.connect(guest_ws, "guest")
    assert mgr.admin_count == 1
    assert mgr.guest_count == 1


def test_disconnect_removes_from_correct_set():
    mgr = ConnectionManager()
    ws = make_ws()
    mgr.connect(ws, "guest")
    mgr.disconnect(ws, "guest")
    assert mgr.guest_count == 0


def test_disconnect_nonexistent_is_safe():
    mgr = ConnectionManager()
    ws = make_ws()
    mgr.disconnect(ws, "admin")  # should not raise


# ── broadcast ─────────────────────────────────────────────────────────────────

async def test_broadcast_to_guests_sends_to_guests_only():
    mgr = ConnectionManager()
    admin_ws = make_ws()
    guest_ws = make_ws()
    mgr.connect(admin_ws, "admin")
    mgr.connect(guest_ws, "guest")
    event = NowPlayingEvent(title="Song", is_playing=True)
    await mgr.broadcast_to_guests(event)
    guest_ws.send_json.assert_awaited_once()
    admin_ws.send_json.assert_not_awaited()


async def test_broadcast_to_admins_sends_to_admins_only():
    mgr = ConnectionManager()
    admin_ws = make_ws()
    guest_ws = make_ws()
    mgr.connect(admin_ws, "admin")
    mgr.connect(guest_ws, "guest")
    await mgr.broadcast_to_admins(NowPlayingEvent())
    admin_ws.send_json.assert_awaited_once()
    guest_ws.send_json.assert_not_awaited()


async def test_broadcast_to_all_sends_to_both():
    mgr = ConnectionManager()
    a, g = make_ws(), make_ws()
    mgr.connect(a, "admin")
    mgr.connect(g, "guest")
    await mgr.broadcast_to_all(NowPlayingEvent())
    a.send_json.assert_awaited_once()
    g.send_json.assert_awaited_once()


# ── dead connection cleanup ───────────────────────────────────────────────────

async def test_dead_guest_removed_on_send():
    mgr = ConnectionManager()
    dead = make_ws(fail=True)
    live = make_ws()
    mgr.connect(dead, "guest")
    mgr.connect(live, "guest")
    await mgr.broadcast_to_guests(NowPlayingEvent())
    assert mgr.guest_count == 1  # dead one removed


async def test_dead_admin_removed_on_send():
    mgr = ConnectionManager()
    dead = make_ws(fail=True)
    mgr.connect(dead, "admin")
    await mgr.broadcast_to_admins(NowPlayingEvent())
    assert mgr.admin_count == 0


# ── queue truncation ──────────────────────────────────────────────────────────

def _qi(tid: str) -> QueueItem:
    return QueueItem(track_id=tid, title="T", artist="A", album="B")


async def test_guest_queue_truncated_to_n():
    mgr = ConnectionManager()
    mgr.guest_n = 2
    mgr.guest_m = None
    ws = make_ws()
    mgr.connect(ws, "guest")
    event = QueueChangedEvent(
        queue=[_qi("t1"), _qi("t2"), _qi("t3"), _qi("t4")],
        history=[],
    )
    await mgr.broadcast_to_guests(event)
    sent = ws.send_json.call_args[0][0]
    assert len(sent["queue"]) == 2


async def test_guest_history_truncated_to_m():
    mgr = ConnectionManager()
    mgr.guest_n = None
    mgr.guest_m = 1
    ws = make_ws()
    mgr.connect(ws, "guest")
    event = QueueChangedEvent(
        queue=[],
        history=[_qi("h1"), _qi("h2"), _qi("h3")],
    )
    await mgr.broadcast_to_guests(event)
    sent = ws.send_json.call_args[0][0]
    assert len(sent["history"]) == 1


async def test_admin_receives_full_queue():
    mgr = ConnectionManager()
    mgr.guest_n = 2
    ws = make_ws()
    mgr.connect(ws, "admin")
    event = QueueChangedEvent(queue=[_qi(f"t{i}") for i in range(10)], history=[])
    await mgr.broadcast_to_admins(event)
    sent = ws.send_json.call_args[0][0]
    assert len(sent["queue"]) == 10


async def test_none_limits_means_unlimited():
    mgr = ConnectionManager()
    mgr.guest_n = None
    mgr.guest_m = None
    ws = make_ws()
    mgr.connect(ws, "guest")
    event = QueueChangedEvent(queue=[_qi(f"t{i}") for i in range(20)], history=[])
    await mgr.broadcast_to_guests(event)
    sent = ws.send_json.call_args[0][0]
    assert len(sent["queue"]) == 20


# ── lock changed event ────────────────────────────────────────────────────────

async def test_lock_changed_event_serialises():
    mgr = ConnectionManager()
    ws = make_ws()
    mgr.connect(ws, "guest")
    await mgr.broadcast_to_guests(LockChangedEvent(is_locked=True))
    sent = ws.send_json.call_args[0][0]
    assert sent["type"] == "lock_changed"
    assert sent["is_locked"] is True


# ── volume changed event ─────────────────────────────────────────────────────

def test_volume_changed_event_to_json_happy_path():
    """Round-trip the wire shape backends will produce.

    Per KTD1, the event carries only level: float — no backend_type or
    device_id. Single backend is active at a time; adding fields later is
    a non-breaking dataclass change.
    """
    assert VolumeChangedEvent(level=0.6).to_json() == {
        "type": "volume_changed",
        "level": 0.6,
    }


def test_volume_changed_event_default_level_is_zero():
    """Default level is 0.0 so an instance with no kwargs is still serialisable."""
    assert VolumeChangedEvent().to_json() == {
        "type": "volume_changed",
        "level": 0.0,
    }


async def test_volume_changed_event_admin_only_fanout():
    """volume_changed reaches admins via broadcast_to_admins and not guests.

    The admin-only fanout matches what backends will use; guests don't
    render the admin volume slider so they receive nothing on this event.
    """
    mgr = ConnectionManager()
    admin_ws = make_ws()
    guest_ws = make_ws()
    mgr.connect(admin_ws, "admin")
    mgr.connect(guest_ws, "guest")
    await mgr.broadcast_to_admins(VolumeChangedEvent(level=0.75))
    admin_ws.send_json.assert_awaited_once_with(
        {"type": "volume_changed", "level": 0.75}
    )
    guest_ws.send_json.assert_not_awaited()


# ── airplay protocol changed event ───────────────────────────────────────────

def test_airplay_protocol_changed_event_to_json_happy_path():
    """Wire shape: type/device_id/protocol. The admin UI keys protocol
    labels by device_id and reads protocol to render 'AirPlay 2'/'AirPlay 1'."""
    assert AirPlayProtocolChangedEvent(
        device_id="42FDF3255868@WiiM Pro-5868", protocol="ap1"
    ).to_json() == {
        "type": "airplay_protocol_changed",
        "device_id": "42FDF3255868@WiiM Pro-5868",
        "protocol": "ap1",
    }


def test_airplay_protocol_changed_event_default_fields_empty():
    """Default-constructed event still serialises (defaults are empty strings
    rather than None to avoid JSON-side null-vs-string-empty ambiguity)."""
    assert AirPlayProtocolChangedEvent().to_json() == {
        "type": "airplay_protocol_changed",
        "device_id": "",
        "protocol": "",
    }


async def test_airplay_protocol_changed_event_admin_only_fanout():
    """The event is admin-only: only the admin UI shows the protocol label
    and the No-audio button. Guests have no need for protocol info."""
    mgr = ConnectionManager()
    admin_ws = make_ws()
    guest_ws = make_ws()
    mgr.connect(admin_ws, "admin")
    mgr.connect(guest_ws, "guest")
    await mgr.broadcast_to_admins(
        AirPlayProtocolChangedEvent(device_id="d1", protocol="ap2")
    )
    admin_ws.send_json.assert_awaited_once_with(
        {"type": "airplay_protocol_changed", "device_id": "d1", "protocol": "ap2"}
    )
    guest_ws.send_json.assert_not_awaited()


# ── devices changed event (2026-06-11 live-discovery plan U2) ─────────────────

def test_devices_changed_event_to_json_happy_path():
    """Wire shape per KTD5: payload-carrying — `devices` holds dicts the
    watcher's snapshot builder already serialized (U5 swaps in the real
    aggregator), `mdns_status` mirrors admin.py's per-backend map."""
    assert DevicesChangedEvent(
        devices=[{
            "backend": "airplay", "id": "192.168.1.20:7000",
            "name": "WiiM Pro", "online": False,
            "offline_since": 1770000000.0,
        }],
        mdns_status={"airplay": "ok", "chromecast": "unavailable"},
    ).to_json() == {
        "type": "devices_changed",
        "devices": [{
            "backend": "airplay", "id": "192.168.1.20:7000",
            "name": "WiiM Pro", "online": False,
            "offline_since": 1770000000.0,
        }],
        "mdns_status": {"airplay": "ok", "chromecast": "unavailable"},
    }


def test_devices_changed_event_defaults_serialise():
    """Default-constructed event still serialises: empty list/map, not None."""
    assert DevicesChangedEvent().to_json() == {
        "type": "devices_changed", "devices": [], "mdns_status": {},
    }


async def test_devices_changed_event_admin_only_fanout():
    """devices_changed is admin-only (KTD5): the device picker is admin
    chrome; guests never render output devices."""
    mgr = ConnectionManager()
    admin_ws = make_ws()
    guest_ws = make_ws()
    mgr.connect(admin_ws, "admin")
    mgr.connect(guest_ws, "guest")
    await mgr.broadcast_to_admins(
        DevicesChangedEvent(devices=[{"id": "d1"}], mdns_status={"dlna": "ok"}))
    admin_ws.send_json.assert_awaited_once_with({
        "type": "devices_changed",
        "devices": [{"id": "d1"}],
        "mdns_status": {"dlna": "ok"},
    })
    guest_ws.send_json.assert_not_awaited()


# ── album_id on WS payloads (2026-06-10 clickable-names plan U1) ──────────────
# The WS events drive the Now Playing / micro-bar / queue surfaces; their
# wire shape must carry album_id (None tolerated) for album-name taps.

def test_now_playing_event_serialises_album_id():
    assert NowPlayingEvent(album_id="alb1").to_json()["album_id"] == "alb1"
    assert NowPlayingEvent().to_json()["album_id"] is None


def test_queue_changed_event_items_serialise_album_id():
    ev = QueueChangedEvent(queue=[QueueItem(
        track_id="t1", title="T", artist="A", album="B", album_id="alb2")])
    payload = ev.to_json()
    assert payload["queue"][0]["album_id"] == "alb2"
    bare = QueueChangedEvent(queue=[_qi("t2")]).to_json()
    assert bare["queue"][0]["album_id"] is None


# ── appearance_changed (2026-06-11 glow-up plan U1) ───────────────────────────

def test_appearance_changed_event_serialises():
    from app.events.types import AppearanceChangedEvent
    ev = AppearanceChangedEvent(scheme="king-crimson", rail_mode="vu", view="tile",
                                rating_style="dots")
    assert ev.to_json() == {
        "type": "appearance_changed", "scheme": "king-crimson", "rail_mode": "vu",
        "view": "tile", "surprise_me_enabled": True,
        "rail_alpha_mode": "english",
        "rail_artist_threshold": 2, "rail_album_threshold": 2,
        "rating_style": "dots",
    }
