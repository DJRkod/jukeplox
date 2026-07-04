"""Abstract output backend protocol and shared models."""

import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

AdvanceCallback = Callable[[], Coroutine[Any, Any, Any]]

from app.models import Track


# Shared echo-guard window used by every backend that emits volume_changed
# (Chromecast, DLNA, AirPlay). Server-side: set_volume() stamps the backend's
# `_vol_last_set = time.monotonic()` immediately before the device write so
# the device's own confirmation NOTIFY/listener-callback is suppressed by
# echo_guard_active() during the next ECHO_GUARD_WINDOW seconds. Without this,
# every server-initiated volume write would echo back to admin clients and
# cause slider snap-back during user drags.
ECHO_GUARD_WINDOW = 2.0


def echo_guard_active(last_set: float) -> bool:
    """Return True when an incoming device-event is within the echo window
    of a server-initiated write and should be suppressed."""
    return time.monotonic() - last_set <= ECHO_GUARD_WINDOW


class DeviceNotReadyError(RuntimeError):
    """Raised by a backend when no device is connected.

    Unlike a normal playback failure, this signals that the queue should not be
    drained — the device is temporarily unavailable, not the content.
    """


@dataclass
class OutputDevice:
    id: str
    name: str
    backend_type: str  # "direct" | "chromecast" | "airplay" | "dlna"
    id_format: str = "uuid"  # "uuid" | "host_port"
    # Optional advisory text surfaced in the UI next to the device picker.
    # Generic carrier; currently unused after the AirPlay backend migrated
    # from pyatv to cliap2 (the pyatv-era "Likely silent on AirPlay" hint
    # is no longer relevant — see docs/plans/2026-06-06-004-feat-airplay-
    # cliap2-migration-plan.md). Retained for future per-device advisories.
    hint: str | None = None


@runtime_checkable
class AbstractOutputBackend(Protocol):
    async def play(self, stream_url: str, metadata: Track) -> None: ...
    async def pause(self) -> None: ...
    async def resume(self) -> None: ...
    async def stop(self) -> None: ...
    async def set_volume(self, level: float) -> None: ...
    async def get_volume(self) -> float: ...
    async def discover_devices(self) -> list[OutputDevice]: ...
    async def set_device(self, device_id: str) -> None: ...
    async def get_position(self) -> int: ...
    async def seek(self, position_ms: int) -> None: ...

    @property
    def is_playing(self) -> bool: ...
