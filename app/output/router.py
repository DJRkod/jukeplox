"""Output router — delegates to whichever backend is currently active.

Switching backends applies at the *next* track: if playback is running the old
backend continues until advance() is called, at which point the pending backend
is swapped in.
"""

from app.output.base import AbstractOutputBackend, OutputDevice
from app.models import Track


class OutputRouter:
    def __init__(self) -> None:
        self._active: AbstractOutputBackend | None = None
        self._pending: AbstractOutputBackend | None = None

    def set_backend(self, backend: AbstractOutputBackend) -> None:
        """Schedule a backend switch.  If nothing is playing, switch immediately."""
        if self._active is None or not self._active.is_playing:
            self._active = backend
            self._pending = None
        else:
            self._pending = backend

    async def swap_pending(self) -> None:
        """Called by advance() to activate a pending backend switch.
        Stops the old backend so its EOS/poll tasks cannot fire on the new active backend.
        """
        if self._pending is not None:
            old = self._active
            self._active = self._pending
            self._pending = None
            if old is not None:
                try:
                    await old.stop()
                except Exception:
                    pass

    @property
    def active(self) -> AbstractOutputBackend | None:
        return self._active

    @property
    def has_pending(self) -> bool:
        return self._pending is not None

    def _require_active(self) -> AbstractOutputBackend:
        if self._active is None:
            raise RuntimeError("No output backend configured")
        return self._active

    async def play(self, stream_url: str, metadata: Track) -> None:
        await self.swap_pending()
        await self._require_active().play(stream_url, metadata)

    async def pause(self) -> None:
        await self._require_active().pause()

    async def resume(self) -> None:
        await self._require_active().resume()

    async def stop(self) -> None:
        if self._active:
            await self._active.stop()

    async def set_volume(self, level: float) -> None:
        await self._require_active().set_volume(level)

    async def get_volume(self) -> float:
        return await self._require_active().get_volume()

    async def discover_devices(self) -> list[OutputDevice]:
        if self._active:
            return await self._active.discover_devices()
        return []

    async def set_device(self, device_id: str) -> None:
        await self._require_active().set_device(device_id)

    async def get_position(self) -> int:
        if self._active:
            return await self._active.get_position()
        return 0

    async def seek(self, position_ms: int) -> None:
        if self._active:
            await self._active.seek(position_ms)

    @property
    def is_playing(self) -> bool:
        return bool(self._active and self._active.is_playing)
