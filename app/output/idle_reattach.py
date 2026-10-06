"""Re-attach the selected output device when it returns while Jukeplox is idle.

Issue #47. The host switches the speaker on at the start of a party, queues
songs, and finds Jukeplox actively trying and failing to play to a device
nobody is attached to — then has to leave what they are doing and re-apply a
device they already chose. The cost is not the tap count; it is being pulled
into an administrative screen while guests arrive.

THE SCOPE OF THIS MODULE IS "GET ATTACHED", AND NOTHING ELSE (origin R7). It
never starts, stops, or resumes playback. The existing queue-changed auto-start
already covers the host's real sequence — switch the speaker on, THEN queue —
and a coordinator that also played was the single largest source of risk in the
abandoned first attempt (it caused a Closing Time defect and carried a
"plays to a device that isn't yours" exposure). ``tests`` enforce the boundary
behaviourally and an AST guard enforces it structurally: this module must not
reference the playback surface at all.

WHY OWNERSHIP IS DERIVED AND NOT STORED (plan KTD1)
---------------------------------------------------
The first attempt armed a watcher on the device's offline transition and then
had to remember to retire it on five separate edges — outage-hold entry, hold
exit, a failed Apply, a selection change, and shutdown. It defined the arming
edge and never defined the retire edges, and nearly every review finding was a
symptom of that one omission, including a P0 where it re-ran ``set_device`` on
a device that was already attached and playing, silently stalling the queue.

So nothing here stores "armed". ``should_watch()`` DERIVES whether this module
is responsible for the selection right now, and ``reconcile()`` makes the
registered listener match that answer. The five retire edges become five places
that call ``reconcile()`` — and the failure mode changes shape completely: a
forgotten call costs one delayed reconciliation, where a forgotten retire edge
cost a permanently stranded watcher.

WHERE IDENTITY SITS, AND WHY IT IS NOT A PREDICATE CONJUNCT
-----------------------------------------------------------
The plan sketched identity as one more conjunct of ``should_watch``. It is not
implemented that way, because identity is a property of an ARRIVING DEVICE, not
of world state: there is nothing to verify until something announces itself.
Identity is therefore the gate on the attach itself (``_on_arrival``), which is
the same guarantee — origin R4 says an unconfirmable device is not attached, and
it isn't — reached at the only moment the question can actually be answered.
Registering a listener for a device that later turns out to be unconfirmable
costs nothing: the listener has no side effects, and the attach declines.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from app.output import hold
from app.output.identity import Selection, identity_confirmed

_log = logging.getLogger(__name__)

# Retry cadence (plan KTD4), resolving an origin open question.
#
# The PRIMARY trigger is the watcher's arrival listener, so a returning device
# is picked up on announcement rather than on a poll. This backoff is the
# safety net for the other failure: the device is announcing but the attach
# itself fails — a speaker that answers mDNS before its audio stack is ready.
#
# Gentler than the supervisor's 5s→300s on purpose. The supervisor is
# aggressive because a human is standing there waiting for music to resume;
# nobody is waiting on the idle path, so it trades latency for a smaller
# footprint against a sleeping device. It never gives up while the predicate
# holds, because origin R5 says "until it attaches or the selection changes" —
# and a changed selection is already the predicate going false.
RETRY_START_S = 15.0
RETRY_FACTOR = 2.0
RETRY_CAP_S = 120.0


class IdleReattachCoordinator:
    """Owner of reconnect while nothing is playing (origin actor A4).

    Peer to the outage supervisor, which owns reconnect while playback is
    HELD. Exactly one of them ever acts on a given device: this one stands
    down whenever a hold owns the selection, which is the ``hold`` conjunct of
    ``should_watch``."""

    def __init__(
        self,
        *,
        timer: Callable[[float, Callable[[], None]], Any] | None = None,
        attach: Callable[..., Any] | None = None,
    ) -> None:
        # Injection points with lazy defaults, matching DeviceWatcher's
        # convention so a coordinator can be constructed in a test without
        # dragging in the event loop, the watcher or app.state.
        self._timer = timer or self._default_timer
        self._attach = attach or self._default_attach
        # The single piece of state, and it is a CACHE of what is registered
        # with the watcher, not a decision: (backend_type, device_id, cb).
        self._listener: tuple[str, str, Callable[[], None]] | None = None
        self._retry_handle: Any = None
        self._delay_s = RETRY_START_S
        self._attach_inflight = False

    # ── the derived predicate (KTD1) ─────────────────────────────────────────

    def should_watch(self) -> tuple[str, str] | None:
        """The ``(backend_type, device_id)`` this module is responsible for
        right now, or None.

        Four conjuncts, each one a requirement rather than an implementation
        detail, which is why they are readable straight off this function:

        - **there is a real selection** — a Direct or unset output has no
          remote device to re-attach;
        - **no outage hold owns it** (origin R9) — while the supervisor owns
          reconnect for a device, this module does not act on that device;
        - **the device is away** (origin R6) — see ``_device_is_away``; there
          is nothing to re-attach to a device that is present;
        - **playback is not running on it** (origin R6 again, and belt to the
          previous conjunct's braces) — a device CAN be playing while its
          mDNS record lapses, which reads as away. Re-attaching a live
          transport tears it down, and when that device is mid-playback the
          queue stops advancing with no error anywhere. That was the P0.

        Never raises: a coordinator that cannot evaluate its own predicate
        must fall silent, not break the caller that asked."""
        try:
            key = self._selected_key()
            if key is None:
                return None
            if hold.output_hold_active():
                return None
            if not self._device_is_away(key):
                return None
            if self._playback_owns(key):
                return None
            return key
        except Exception:
            _log.debug("idle re-attach: predicate evaluation failed",
                       exc_info=True)
            return None

    def _device_is_away(self, key: tuple[str, str]) -> bool:
        """True when the watcher says the selection is offline or unknown.

        This is the conjunct that makes origin R6 hold as WRITTEN — "never
        attempts to attach a device it is already attached to" — rather than
        only in its mid-playback special case. Without it the coordinator
        re-attaches a perfectly healthy idle device on every routine mDNS
        re-announcement, churning ``set_device`` for nothing.

        Note what it is NOT keyed on. The backend's ``_device_id`` is memory,
        not liveness: a sleeping speaker still has its id remembered, and that
        is precisely this feature's situation, so a guard reading it would be
        permanently true and the feature inert. Nor can the backend be asked
        whether its connection is live — a stale DLNA renderer object and a
        live one look identical without probing. The WATCHER is the component
        that actually knows reachability, so ask it.

        The asymmetry with ``_on_arrival`` is deliberate and is the reason
        these are two questions rather than one. This decides whether to
        LISTEN, and it must be false the moment the device is present.
        ``_on_arrival`` decides whether to ATTACH, and by then the arrival has
        already flipped the registry to online — so it must not re-ask this,
        or the attach it exists to perform could never happen.

        Unknown-to-the-watcher counts as away: a device evicted before the
        selection was made, or a watcher that has not started, should not
        silence the coordinator."""
        watcher = self._watcher()
        if watcher is None:
            return True
        entry = watcher.registry.get(key)
        if entry is None:
            return True
        return not getattr(entry, "online", False)

    def reconcile(self) -> None:
        """Make the registered listener match ``should_watch()``.

        Idempotent, synchronous and fail-soft — it is called from Apply, from
        hold transitions, from boot and from shutdown, and none of those may
        break because reconnect bookkeeping had a bad day."""
        try:
            wanted = self.should_watch()
            current = self._listener[:2] if self._listener else None
            if current == wanted:
                return
            if self._listener is not None:
                self._unregister()
            if wanted is not None:
                self._register(wanted)
        except Exception:
            _log.debug("idle re-attach: reconcile failed", exc_info=True)

    # ── arrival handling ────────────────────────────────────────────────────

    def _on_arrival(self, key: tuple[str, str]) -> None:
        """The watcher saw *key* announce itself. Fires on EVERY arrival, not
        just the offline→online edge, so it must be cheap and idempotent."""
        if self._attach_inflight:
            return  # single-flight: an arrival burst is one attach
        # NOT `should_watch() != key`. The arrival has already flipped the
        # registry to online, so the "device is away" conjunct is false by
        # construction here — re-asking the whole predicate would decline
        # every attach this module exists to perform. Re-check only the
        # conjuncts an arrival cannot invalidate.
        if self._still_ours(key):
            try:
                task = asyncio.ensure_future(self._attempt(key))
            except RuntimeError:
                # No running loop — nothing can attach right now. Leave the
                # listener registered so the next arrival on a live loop
                # tries again, and do NOT set _attach_inflight, which would
                # wedge the single-flight guard permanently.
                _log.debug("idle re-attach: no running loop; attach deferred")
                return
            self._attach_inflight = True
            task.add_done_callback(lambda _t: None)
            return
        self.reconcile()  # the world moved under us — re-derive and stop

    def _still_ours(self, key: tuple[str, str]) -> bool:
        """The arrival-time half of the predicate: everything except the
        away conjunct. Guards the two ways ownership can have moved between
        registering the listener and the device turning up — the supervisor
        took over (R9), or playback started on it (R6)."""
        try:
            if self._selected_key() != key:
                return False
            if hold.output_hold_active():
                return False
            return not self._playback_owns(key)
        except Exception:
            _log.debug("idle re-attach: arrival-time re-check failed",
                       exc_info=True)
            return False

    async def _attempt(self, key: tuple[str, str]) -> None:
        """Verify identity, then attach. Never raises."""
        backend_type, device_id = key
        try:
            if not self._identity_ok(key):
                # Origin R4: no attach, no playback, no message. The outcome
                # is deliberately indistinguishable from this feature not
                # existing — which is what makes the silence honest. The
                # device is still in the picker (it was never purged), so the
                # host's fallback is one tap rather than a Scan.
                _log.debug("idle re-attach: %s/%s arrived but identity is not "
                           "confirmable; standing down", backend_type, device_id)
                return
            backend = self._backend_for(backend_type)
            if backend is None:
                return
            ok = await self._attach(backend, backend_type, device_id)
            if ok:
                # Origin R2: silent. Nothing broadcast, nothing above debug.
                # The host is meant to never learn this feature exists.
                _log.debug("idle re-attach: attached %s/%s",
                           backend_type, device_id)
                self._cancel_retry()
                self._delay_s = RETRY_START_S
                self.reconcile()  # attached → predicate false → unregister
            else:
                self._arm_retry(key)
        except Exception:
            _log.debug("idle re-attach: attach attempt failed for %s/%s",
                       backend_type, device_id, exc_info=True)
            self._arm_retry(key)
        finally:
            self._attach_inflight = False

    def _identity_ok(self, key: tuple[str, str]) -> bool:
        """Origin R3: confirm the arrival is the selected device, by stable
        identifier where the backend has one and by name where it does not."""
        backend_type, device_id = key
        watcher = self._watcher()
        if watcher is None:
            return False
        entry = watcher.registry.get(key)
        if entry is None:
            return False
        siblings = [
            e.device for k, e in watcher.registry.items()
            if k[0] == backend_type
        ]
        return identity_confirmed(
            Selection(backend_type=backend_type, device_id=device_id,
                      name=self._selected_name()),
            entry.device, siblings)

    # ── retry (KTD4) ────────────────────────────────────────────────────────

    def _arm_retry(self, key: tuple[str, str], *, grow: bool = True) -> None:
        """Schedule another attempt. Never terminates while the predicate
        holds (origin R5).

        ``grow=False`` schedules at the current delay without escalating —
        used by ``_register``, which arms the floor as a safety net for a
        device that came back without announcing (or announced before this
        listener existed). Only a FAILED ATTEMPT earns an escalation, so
        registration must not silently consume the first backoff step."""
        self._cancel_retry()
        delay = self._delay_s
        if grow:
            self._delay_s = min(self._delay_s * RETRY_FACTOR, RETRY_CAP_S)

        def _fire() -> None:
            self._retry_handle = None
            # Elapsed time is NOT evidence the device is there (2026-09-04
            # plan U3). This callback used to re-enter _on_arrival directly,
            # which made a tick indistinguishable from the device having
            # announced itself — so a speaker switched off for hours drew an
            # attach every cycle, forever, at the cap.
            #
            # The backoff exists for the OTHER case, stated in KTD4 above: the
            # device IS announcing and the attach itself fails, e.g. a speaker
            # that answers mDNS before its audio stack is ready. Requiring
            # presence enforces that intent rather than changing it.
            #
            # Declining is not giving up. The listener stays registered, so a
            # genuine arrival resumes attempts — and an arrival is the only
            # thing that should.
            try:
                away = self._device_is_away(key)
            except Exception:
                # Fail-soft like every other watcher touch in this module. A
                # bare loop callback that raises would escape into asyncio's
                # default handler with the handle already cleared — silent AND
                # terminal, at the one place nothing else re-arms.
                _log.debug("idle re-attach: presence check failed at the "
                           "retry floor", exc_info=True)
                away = True
            if away:
                # Re-arm WITHOUT escalating. Declining to attach is not
                # declining to keep watching: reconcile() — the forced-Scan
                # merge — flips an entry online directly rather than through
                # _apply_arrival, so it fires no arrival listener. Returning
                # with no timer would leave a device that came back via Scan
                # permanently unattached, which is exactly the "came back
                # without announcing" case _arm_retry's docstring promises to
                # cover. The floor costs a dict lookup, not a connect.
                _log.debug("idle re-attach: %s/%s still absent at the retry "
                           "floor; re-arming", key[0], key[1])
                self._arm_retry(key, grow=False)
                return
            self._on_arrival(key)

        self._retry_handle = self._timer(delay, _fire)

    def _cancel_retry(self) -> None:
        handle, self._retry_handle = self._retry_handle, None
        if handle is None:
            return  # also the "no loop, nothing armed" case
        try:
            handle.cancel()
        except Exception:
            pass

    # ── watcher registration ────────────────────────────────────────────────

    def _register(self, key: tuple[str, str]) -> None:
        watcher = self._watcher()
        if watcher is None or not hasattr(watcher, "add_arrival_listener"):
            # Not silent. This branch used to return with no trace at all, and
            # at boot it is REACHABLE: the watcher starts after state.setup(),
            # so a reconcile from the startup-reconnect task can land here and
            # leave the coordinator permanently inert — no listener, no retry
            # floor, nothing to heal it. Cost an hour on the rig to find,
            # because "no watcher" and "armed and quietly waiting" produced
            # byte-identical logs. The caller in app/main.py reconciles again
            # once the watcher is up; this line is how anyone debugging the
            # next variant of that race sees it immediately.
            _log.debug("idle re-attach: no watcher yet for %s/%s — not "
                       "registered; a later reconcile must re-arm", *key)
            return
        backend_type, device_id = key

        def _cb() -> None:
            self._on_arrival(key)

        try:
            watcher.add_arrival_listener(backend_type, device_id, _cb)
        except Exception:
            _log.debug("idle re-attach: listener registration failed",
                       exc_info=True)
            return
        self._listener = (backend_type, device_id, _cb)
        self._delay_s = RETRY_START_S
        # Deliberately does NOT attempt an attach here. Registration only
        # happens when the device is away (the predicate's third conjunct),
        # and there is nothing to attach to an absent device — the arrival
        # listener and the backoff floor are what drive attempts. An earlier
        # draft evaluated immediately on registration, which churned
        # set_device against a healthy idle device on every reconcile.
        self._arm_retry(key, grow=False)

    def _unregister(self) -> None:
        listener, self._listener = self._listener, None
        self._cancel_retry()
        if listener is None:
            return
        backend_type, device_id, cb = listener
        watcher = self._watcher()
        if watcher is None or not hasattr(watcher, "remove_arrival_listener"):
            return
        try:
            watcher.remove_arrival_listener(backend_type, device_id, cb)
        except Exception:
            _log.debug("idle re-attach: listener removal failed", exc_info=True)

    def shutdown(self) -> None:
        """Origin R10, shutdown edge. Unregister unconditionally rather than
        via the predicate — at shutdown the answer must be "nothing", whatever
        the selection says."""
        self._unregister()

    # ── lazy collaborators ──────────────────────────────────────────────────

    @staticmethod
    def _default_timer(delay: float, cb: Callable[[], None]) -> Any:
        """Schedule *cb*, or return None when there is no loop to schedule on.

        ``reconcile()`` is synchronous and is called from places that may not
        be on the event loop (shutdown, a test constructing a coordinator
        directly). ``get_event_loop()`` is deprecated outside a running loop
        in 3.11 and raises in 3.12, so ask for the RUNNING loop and degrade to
        "no backoff floor" rather than taking down the caller. The arrival
        listener is the primary trigger either way."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _log.debug("idle re-attach: no running loop; retry floor not armed")
            return None
        return loop.call_later(delay, cb)

    @staticmethod
    async def _default_attach(backend: Any, backend_type: str,
                              device_id: str) -> bool:
        from app.output import session
        return await session.attach_without_outage(
            backend, backend_type, device_id)

    @staticmethod
    def _watcher() -> Any:
        from app.output import watcher as watcher_mod
        return watcher_mod.get_watcher()

    @staticmethod
    def _selected_key() -> tuple[str, str] | None:
        """The admin's selection, or None when there is no remote device to
        re-attach. Direct's pseudo-device never enters the registry."""
        from app import state
        backend_type, device_id = state.selected_output_key()
        if not device_id or backend_type == "direct":
            return None
        return (backend_type, device_id)

    @staticmethod
    def _selected_name() -> str:
        from app import state
        return state.selected_output_name()

    @staticmethod
    def _backend_for(backend_type: str) -> Any:
        from app import state
        return getattr(state, f"{backend_type}_backend", None)

    @staticmethod
    def _playback_owns(key: tuple[str, str]) -> bool:
        """Origin R6/AE9: True when *key* is attached AND playing.

        Deliberately narrower than "the backend remembers this device id".
        A sleeping device still has its id remembered — that is the whole
        situation this module exists for — so keying off memory alone would
        make the predicate permanently false and the feature inert. What must
        never be disturbed is a LIVE transport, and the harm origin R6
        describes is specific: "when that device is mid-playback, the queue
        stops advancing with no error anywhere"."""
        from app import state
        if not state.queue_engine.state.is_playing:
            return False
        backend = getattr(state.output_router, "active", None)
        if backend is None:
            return False
        if getattr(backend, "_device_id", None) != key[1]:
            return False
        return state.selected_output_key()[0] == key[0]


_coordinator: IdleReattachCoordinator | None = None


def get_coordinator() -> IdleReattachCoordinator:
    """The process-wide coordinator (created on first use)."""
    global _coordinator
    if _coordinator is None:
        _coordinator = IdleReattachCoordinator()
    return _coordinator


def reconcile() -> None:
    """Module-level convenience for the trigger sites: re-derive ownership.

    Fail-soft and idempotent. Every caller is a place where something changed
    that could alter the predicate — a selection, a hold transition, a boot,
    a shutdown — and none of them care about the coordinator's internals."""
    try:
        get_coordinator().reconcile()
    except Exception:
        _log.debug("idle re-attach: reconcile entry failed", exc_info=True)
