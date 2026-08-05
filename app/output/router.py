"""Output router — delegates to whichever backend is currently active.

Switching backends applies at the *next* track: if playback is running the old
backend continues until advance() is called, at which point the pending backend
is swapped in.
"""

import asyncio
import logging

from app.output.base import AbstractOutputBackend, OutputDevice
from app.models import Track

_log = logging.getLogger(__name__)


class OutputRouter:
    def __init__(self) -> None:
        self._active: AbstractOutputBackend | None = None
        self._pending: AbstractOutputBackend | None = None

    def set_backend(self, backend: AbstractOutputBackend) -> None:
        """Schedule a backend switch.  If nothing is playing, switch immediately.

        The immediate branch also STOPS the outgoing backend (review fix
        PLX-2): "not is_playing" includes a PAUSED backend — without the stop
        a retired plexplayer kept its poll task + open client alive (its
        3-strike could fire ``notify_outage`` into the NEW backend's session)
        and a retired DLNA renderer leaked its poll loop. stop() when idle is
        cheap/no-op for every backend. Scheduled as a task because this
        method is sync by contract; the teardown-warning notice rides the
        same task (mirror of swap_pending's ordering).
        """
        if self._active is None or not self._active.is_playing:
            old = self._active
            self._active = backend
            self._pending = None
            if old is not None and old is not backend:
                try:
                    asyncio.get_running_loop().create_task(
                        self._stop_and_warn(old))
                except RuntimeError:
                    pass  # no running loop (sync test path) — nothing playing
        else:
            self._pending = backend
        # Arming lifecycle (2026-07-11 supervisor plan U6, flow Gap 9b): a
        # switch request makes any device-armed next stale — on the deferred
        # path the boundary must return to server control so swap_pending()
        # owns the next track, and on the immediate path the arm belongs to
        # the OLD backend. The state-level reconcile revokes it (has_pending /
        # backend-changed both read as stale there); cheap no-op when nothing
        # is armed. Late import: app.state imports this module at load time.
        from app import state
        state.trigger_arming_eval()

    async def swap_pending(self) -> None:
        """Called by advance() to activate a pending backend switch.
        Stops the old backend so its EOS/poll tasks cannot fire on the new active backend.
        """
        if self._pending is not None:
            old = self._active
            self._active = self._pending
            self._pending = None
            if old is not None:
                await self._stop_and_warn(old)

    async def _stop_and_warn(self, old: AbstractOutputBackend) -> None:
        """Retire an outgoing backend: stop it (never letting a failure block
        the switch), clear any stale dispatch-holder key deposited on it
        (belt-and-suspenders for PLX-1 — a key meant for a superseded
        dispatch must not leak into a later one), then surface the
        teardown-verification warning if the backend exposes one. Shared by
        swap_pending (deferred switch) and set_backend's immediate branch
        (PLX-2)."""
        try:
            await old.stop()
        except Exception:
            _log.warning("output router: retiring backend stop() failed",
                         exc_info=True)
        clear_holder = getattr(old, "set_dispatch_holder", None)
        if callable(clear_holder):
            clear_holder(None)
        await self._notify_teardown_warning(old)

    @staticmethod
    async def _notify_teardown_warning(old) -> None:
        """Admin notice for a switch-away teardown the old backend could not
        verify (2026-08-04-002 plexplayer plan U7 wiring of the U2
        ``last_teardown_warning`` seam). Emitted ONLY from _stop_and_warn —
        the one place both switch paths (deferred swap_pending, immediate
        set_backend) stop the outgoing backend — so the notice fires exactly
        on switch-away, never on internal stops (queue end, admin Stop),
        where the attribute is still set but the player staying live is the
        admin's own explicit action to notice. hasattr-gated: only
        plexplayer exposes the attribute today (an autonomous device can
        keep playing after a failed stop; URL-fed renderers just starve).
        Best-effort — a broadcast failure never blocks the swap."""
        warning = getattr(old, "last_teardown_warning", None)
        if not warning:
            return
        from app.events.bus import notify_admin_error
        await notify_admin_error(
            "Plex player may still be playing — stop it from a Plex app")

    @property
    def active(self) -> AbstractOutputBackend | None:
        return self._active

    def effective_backend(self) -> AbstractOutputBackend | None:
        """The backend the NEXT play() will actually use: the pending switch
        target when a deferred swap is queued, else the active backend
        (review fix PLX-1). ``dispatch_play`` deposits the per-attempt
        holder key on THIS backend — depositing on ``active`` under a
        deferred switch handed the key to the outgoing backend, so the
        incoming plexplayer degraded to metadata.id parsing (or a stale key
        deposited earlier was consumed by a later unrelated dispatch)."""
        return self._pending or self._active

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
