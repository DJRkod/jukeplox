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


class _SwitchIntent:
    """One requested output switch, from request to resolution.

    Exists so that three properties hold by construction rather than by the
    caller remembering them:

    * **Captured once.** ``was_playing`` is read in ``__init__``, before the
      attach can touch it. The previous field-based version re-read it on
      every ``begin_switch``, so a second Apply during the first attach saw
      the state the first attach had already changed — plexplayer clears
      ``_is_playing`` before its first await — and the immediate-vs-deferred
      decision then cut audio mid-track.
    * **Always retired.** ``__exit__`` runs on every route out of the block,
      including ``CancelledError``, which the old ``except Exception`` around
      the attach did not catch. Missing that clear left the router believing a
      switch was forever in flight, which permanently denied outage authority
      to the live output — the queue silently stopped self-healing.
    * **Owns only itself.** The clear is guarded on identity, so a switch
      finishing cannot retire a DIFFERENT switch that started after it.

    Not reusable and not re-entrant: one object per requested switch."""

    def __init__(self, router: "OutputRouter",
                 backend: AbstractOutputBackend) -> None:
        self.router = router
        self.backend = backend
        self.was_playing = bool(router._active and router._active.is_playing)
        self.committed = False

    def __enter__(self) -> "_SwitchIntent":
        self.router._intent = self
        # Revoking a device-armed next belongs to the REQUEST, not the commit:
        # the outgoing backend must not gaplessly consume another track during
        # a switch the admin has already asked for.
        from app import state
        state.trigger_arming_eval()
        return self

    def __exit__(self, *exc: object) -> bool:
        if self.router._intent is self:
            self.router._intent = None
        return False

    def commit(self) -> None:
        """Make this switch's target the authoritative output.

        Deliberately separate from ``__exit__``: the attach must succeed first,
        because a failed switch that had already repointed the router would
        retire — and release — the output the user is still using."""
        self.committed = True
        self.router._apply_backend(self.backend, was_playing=self.was_playing)


class OutputRouter:
    def __init__(self) -> None:
        self._active: AbstractOutputBackend | None = None
        self._pending: AbstractOutputBackend | None = None
        self._intent: "_SwitchIntent | None" = None

    # ── switch intent (2026-08-20 re-review ADV-13/CR-4) ─────────────────────
    #
    # A switch has TWO moments, and conflating them is what the review caught.
    # ``set_backend`` is the moment the new backend becomes AUTHORITATIVE, and
    # it must wait for the attach to succeed — otherwise a failed switch leaves
    # the user's working output released (review F2). But several things key
    # off the switch having been REQUESTED, and those must not wait, because
    # the attach can take up to ~30s on DLNA:
    #
    #   * Outage authority. U5 exists so a backend the user is leaving cannot
    #     hold the queue. If it keeps authority for the whole attach, a poll
    #     strike on the dying renderer opens a hold — pausing the queue and
    #     re-inserting the current item — against a device already abandoned.
    #   * Arming. ``set_backend``'s own comment says a switch request makes any
    #     device-armed next stale; delaying that lets the outgoing backend
    #     gaplessly consume a track during a switch already asked for.
    #   * The immediate-vs-deferred decision, which reads ``is_playing``. Read
    #     after the attach it can be the INCOMING attach's own doing —
    #     plexplayer and AirPlay both clear the flag inside ``set_device`` — so
    #     a same-instance device change mid-playback silently flipped from the
    #     deferred branch to the immediate one, skipping the boundary stop and
    #     leaving a stale arm nobody revokes.

    # The intent is a SCOPED OBJECT, not a pair of fields (2026-08-21 review).
    # The field version needed the caller to remember to clear on every exit
    # path, and three separate defects came out of that single requirement: a
    # cancellation skipped the clear and muted outage reporting on the live
    # output for the rest of the process; a second Apply re-read the playing
    # state after the first attach had already changed it; and an unconditional
    # clear let one switch wipe another's in-flight intent. A context manager
    # cannot forget, captures once, and clears only what it owns.

    def switching_to(self, backend: AbstractOutputBackend) -> "_SwitchIntent":
        """Scope a requested switch to *backend*.

        Use as ``with router.switching_to(b) as switch:`` and commit inside the
        block. Leaving the block by ANY route — success, exception, or
        cancellation — retires the intent."""
        return _SwitchIntent(self, backend)

    def is_switching_away_from(self, backend: AbstractOutputBackend) -> bool:
        """Has the admin already committed to leaving *backend*?

        True only for the CURRENT output while a switch to a different backend
        is in flight. A backend that is merely not active is handled by the
        ordinary active-output check; this answers the narrower question the
        window opened."""
        intent = self._intent
        return (intent is not None
                and backend is self._active
                and backend is not intent.backend)

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

        Direct entry point for callers that switch without a request phase.
        Anything going through ``activate_backend`` uses ``switching_to(...)``
        and ``commit()`` instead, so the immediate-vs-deferred decision is made
        from the state at REQUEST time rather than from whatever the incoming
        attach has since done to it.
        """
        self._apply_backend(
            backend,
            was_playing=bool(self._active and self._active.is_playing))

    def _apply_backend(self, backend: AbstractOutputBackend, *,
                       was_playing: bool) -> None:
        """The switch itself, with the branch decision handed in.

        Split out so the two entry points cannot drift: ``set_backend`` reads
        the playing state now, a committed ``_SwitchIntent`` supplies what it
        captured when the admin asked."""
        if self._active is None or not was_playing:
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
        Stops the old backend so its EOS/poll tasks cannot fire on the new
        active backend.

        Note the pending backend can be the SAME instance as the active one (a
        device change within one backend, requested mid-playback, routes here);
        the stop is deliberately unconditional, and ``_stop_and_warn`` explains
        why the release it now performs is not.
        """
        if self._pending is not None:
            old = self._active
            self._active = self._pending
            self._pending = None
            if old is not None:
                await self._stop_and_warn(old)

    async def _stop_and_warn(self, old: AbstractOutputBackend) -> None:
        """Retire an outgoing backend: stop it (never letting a failure block
        the switch), release what its attach adopted, clear any stale
        dispatch-holder key deposited on it (belt-and-suspenders for PLX-1 —
        a key meant for a superseded dispatch must not leak into a later
        one), then surface the teardown-verification warning if the backend
        exposes one. Shared by swap_pending (deferred switch) and
        set_backend's immediate branch (PLX-2).

        This is the switch-away half of the ownership contract's end of life
        (2026-08-20 plan U6, R3), and the release it now performs is the one
        step here that is CONDITIONAL: it happens only when the backend being
        retired is not the router's active output.

        That guard is not theoretical. ``swap_pending`` retires whatever was
        active without asking whether the pending backend is the same object,
        and it legitimately can be — changing Cast speaker A to speaker B
        mid-playback re-selects the SAME backend instance, so it lands in the
        deferred branch with ``pending is active``. Stopping there is harmless
        (a stop immediately before the boundary's play), but releasing would
        drop the connection the very next ``play()`` needs, and the switch the
        user asked for would fail with a device error. The same-instance case
        belongs to ``set_device``, which tears down the connection it is
        replacing itself. The immediate branch already refuses to retire a
        backend it is swapping in; this makes the deferred branch agree, for
        the one step where it matters.

        Order is load-bearing, and it is: stop, THEN release, THEN the notice.

        * Release cannot come first. Every backend's ``stop()`` speaks to the
          device over exactly the connection release drops — a released Cast
          has no socket to send the stop on, a released plexplayer has no
          Companion client, a released renderer has no DmrDevice. Releasing
          first would silently downgrade every switch-away stop to a no-op and
          leave the outgoing device playing.
        * Release must not come after the notice. ``_notify_teardown_warning``
          awaits a broadcast, and every await here is a window in which the
          abandoned connection's listeners are still live and still consider
          themselves current. Releasing before that await closes the window at
          the first opportunity — the moment the stop has been delivered and
          nothing else needs the connection.
        * Failure is contained exactly like the stop above it: release is
          contractually non-blocking and never-raising, but this is the caller
          that must not care if a backend ever breaks that promise. A retire
          that fails to let go still completes the switch."""
        # Snapshot the outgoing backend's attach generation at the moment the
        # retire is decided, before the stop below yields (2026-08-20 review
        # CR-3/ADV-9; corrected twice, see below). This task is detached and
        # takes no lock, so it can run arbitrarily late — and ``stop()`` is a
        # real device round trip that can take seconds against a sleeping
        # speaker. In that gap the admin can Apply this very backend again.
        #
        # That matters because ``release()`` supersedes in-flight attaches.
        # Without a check here, "Apply DLNA, change mind, Apply Cast again"
        # had the stale retire cancel the NEW attach's adoption: the
        # connection was built and then dropped, and activate_backend reported
        # success onto a backend holding nothing.
        #
        # The generation moves at attach ENTRY, so this stands down for a
        # newer attach whether it goes on to succeed or fail. That is only
        # safe because every backend now frees its outgoing connection at
        # entry too — so "a newer attach started" already implies "the old
        # connection has been freed", and there is nothing left for this
        # retire to release. Chromecast was the one exception and was changed
        # to match (2026-08-22 review); if a future backend defers its
        # outgoing teardown to the swap again, this stand-down silently starts
        # leaking and the fix belongs THERE, not in a second counter here.
        gen_at_decision = getattr(old, "_attach_gen", None)
        try:
            await old.stop()
        except Exception:
            _log.warning("output router: retiring backend stop() failed",
                         exc_info=True)
        newer_attach_took_over = (
            gen_at_decision is not None
            and getattr(old, "_attach_gen", None) != gen_at_decision)
        if newer_attach_took_over:
            _log.info("output router: not releasing %s — a newer attach "
                      "adopted it while this retire was stopping it",
                      type(old).__name__)
        if old is not self._active and not newer_attach_took_over:
            try:
                old.release()
            except Exception:
                # Includes a backend with no release() at all: the Protocol
                # requires one and the cross-backend contract test enforces
                # it, so an AttributeError here is a bug to log — never a
                # reason to strand the user mid-switch.
                _log.warning("output router: retiring backend release() failed",
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

    def release(self) -> None:
        """Delegate the ownership contract to the active backend (2026-08-20
        plan U2). The router mirrors the backend surface, so it must answer
        this question too — sync, non-blocking and never raising, exactly like
        the implementations it forwards to.

        This is the facade method only — it releases whatever is active NOW.
        The two moments ownership actually ends are wired elsewhere (U6):
        the outgoing backend on a switch, in ``_stop_and_warn`` above, and
        every constructed backend at shutdown, in ``app.main``'s lifespan."""
        if self._active is not None:
            self._active.release()

    @property
    def is_playing(self) -> bool:
        return bool(self._active and self._active.is_playing)
