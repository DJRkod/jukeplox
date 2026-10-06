"""Abstract output backend protocol and shared models."""

import asyncio
import inspect
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

AdvanceCallback = Callable[[], Coroutine[Any, Any, Any]]

from app.models import Track

_log = logging.getLogger(__name__)


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


class DiscoveryUnavailable(RuntimeError):
    """The discovery substrate failed, so this pass produced NO SCAN DATA.

    Distinct from a successful scan that found nothing, and the distinction is
    load-bearing: the watcher's sweep treats an empty result as authoritative
    evidence of absence, so it grace-flips and then evicts every known device
    of that backend. Raising instead means "we learned nothing this cycle" and
    the registry is left exactly as it was (2026-09-04 plan U1).

    That difference is not hypothetical. An admin's picker emptied itself over
    11 hours because a timed-out avahi browse returned ``[]`` and every sweep
    read it as an empty network — while the discovery banner still reported
    healthy, because nothing on the sweep path ever degrades that signal.

    The rule this restores is already stated in
    ``PlexPlayerBackend.sweep_devices``: no scan data is never an
    eviction or grace source. Only the mDNS backends lacked a way to say it.
    """


class DeviceLostError(DeviceNotReadyError):
    """Device-level playback failure (2026-07-11 supervisor plan U2, R15).

    The device that was (or should be) rendering is unreachable: connection
    lost, transport dead, sink gone. Subclasses ``DeviceNotReadyError`` so it
    inherits the "don't drain the queue" semantics every existing handler
    already applies; the output-session supervisor additionally routes it to
    an outage hold (pause + re-front-insert the interrupted track) instead of
    the skip/holder-fallback path a track-level failure takes.
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

    def release(self) -> None: ...

    @property
    def is_playing(self) -> bool: ...


# ── attach ownership (2026-08-20 plan U2) ────────────────────────────────────
#
# ``set_device`` above says how to ACQUIRE a device. Until this unit, nothing
# said who frees what it acquired — so nobody did, and an adopted connection
# was only ever released by a SUBSEQUENT ``set_device`` on the same backend.
# Switching away therefore left a socket thread, an fd and a session alive
# against a device with a small concurrent-connection budget, plus a listener
# whose staleness guard could not fire because the backend still considered
# that connection current.
#
# ``release`` is the answer, and it is REQUIRED rather than optional-with-a-
# default-no-op on purpose: a default would let a fifth backend inherit the
# obligation by omission and silently skip it, which is the exact failure this
# unit exists to prevent. AirPlay and Direct genuinely adopt nothing, so their
# implementations are explicit, commented no-ops.
#
# THREE PROPERTIES, and note which of them the *signature* carries:
#
#   1. NON-BLOCKING. ``release`` is a plain ``def``. It cannot ``await``, so an
#      unreachable device physically cannot park the caller inside it. This is
#      not a stylistic choice — #48 measured pychromecast's convenience
#      ``Chromecast.disconnect()`` (a ``socket_client.disconnect()`` plus a
#      ``join(timeout=None)``, documented "block forever") blocking for 25.0
#      seconds on the arm64 rig against an unreachable host, while holding the
#      cross-backend attach lock. Release runs on switch-away and on shutdown;
#      neither can wait. Teardown that genuinely needs the event loop (aiohttp
#      session close, Companion client aclose) goes to ``release_in_background``
#      and the caller does not wait on it.
#   2. NEVER RAISES. Release runs on paths that already have a real error to
#      report, or on a shutdown with nowhere to report to. A cleanup problem
#      must not mask the caller's error.
#   3. IDEMPOTENT + OBSERVABLE. After release the backend reports no adopted
#      connection, and calling it again is a no-op.
#
# Enforced by ``tests/test_output_attach_ownership.py`` across every backend.

_release_tasks: set[asyncio.Task] = set()


def release_in_background(coro: Coroutine[Any, Any, Any], *, label: str) -> None:
    """Run *coro* as fire-and-forget teardown; return to the caller at once.

    The bridge between a synchronous ``release()`` and the teardown steps that
    are genuinely coroutines (``aiohttp.ClientSession.close``,
    ``AiohttpNotifyServer.async_stop_server``, a Companion client's
    ``aclose``). Awaiting those in the caller would reintroduce exactly the
    stall the sync signature exists to prevent — a DLNA GENA UNSUBSCRIBE to a
    renderer that has been unplugged has no bound on it.

    A strong reference is held until the task finishes: the event loop only
    keeps weak references to tasks, so a fire-and-forget task with no other
    referent can be garbage-collected mid-flight.

    With no running loop there is nothing that could run the coroutine, so it
    is closed rather than dropped — a dropped coroutine emits a "coroutine was
    never awaited" RuntimeWarning at GC time and warnings-as-errors runs would
    turn a cleanup path into a failure. Never raises."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        _log.debug("%s: no running event loop; deferred release skipped", label)
        return

    def _done(task: asyncio.Task) -> None:
        _release_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _log.debug("%s: deferred release failed", label, exc_info=exc)

    try:
        task = loop.create_task(coro)
    except Exception:
        coro.close()
        _log.debug("%s: could not schedule deferred release", label,
                   exc_info=True)
        return
    _release_tasks.add(task)
    task.add_done_callback(_done)


# How long shutdown waits for the deferred teardowns above, and how long it then
# waits for the cancellation of whatever was still running. Both deliberately
# short: the whole point of the sync ``release`` is that an unreachable device
# cannot delay its caller, and process exit is the caller least able to wait.
RELEASE_DRAIN_TIMEOUT = 2.0
RELEASE_DRAIN_CANCEL_GRACE = 0.25
# How often the drain re-checks for a teardown queued after it started. Short
# enough that an empty-at-first shutdown is not measurably slower, long enough
# not to spin: the drain's whole budget is RELEASE_DRAIN_TIMEOUT.
_DRAIN_POLL_INTERVAL = 0.02
# How long the drain keeps looking when NOTHING is pending yet. Covers a
# teardown scheduled from a callback as an attach unwinds; deliberately
# small, because a clean shutdown must not pay the full drain budget just
# to confirm there is nothing to do.
_DRAIN_EMPTY_GRACE = 0.05


async def drain_release_tasks(timeout: float = RELEASE_DRAIN_TIMEOUT) -> None:
    """Wait — briefly — for the deferred teardowns scheduled by
    ``release_in_background`` (2026-08-20 plan U6).

    ``release`` is sync by contract, so the parts that genuinely need the loop
    (aiohttp session close, GENA UNSUBSCRIBE, Companion ``aclose``) run as
    background tasks. During normal operation nobody waits on them and that is
    correct. Shutdown is the one exception: the loop is about to close, and a
    task that never got a chance to run leaves its aiohttp session unclosed —
    surfacing as "Unclosed client session" on exit.

    So shutdown DRAINS rather than the signature changing. The wait is bounded
    twice over: *timeout* for the teardowns to finish, then a short grace for
    the cancellation of any that did not. Blocking process exit on an
    unreachable renderer is precisely the failure the sync contract exists to
    prevent — a drain that could hang would reintroduce it at the one moment
    the user is least able to tolerate it.

    Never raises: shutdown has nowhere to report a cleanup problem to.

    The set is re-read after the first wait (2026-08-20 review R4), because
    ``release()`` is no longer the only producer. Since it began superseding
    in-flight attaches, the task that frees a connection can be scheduled by
    the ATTACH — when it finally unwinds and finds itself superseded — which
    may be many seconds after release() returned. A single snapshot taken
    before that would drain an empty set, return in microseconds, and let the
    process exit with the teardown never having been queued."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    seen: set[asyncio.Task] = set()
    still_running: set[asyncio.Task] = set()

    # Poll until the budget is spent rather than snapshotting once. Two earlier
    # versions got this wrong in the same direction (2026-08-20 R4, corrected
    # 2026-08-21): the first took a single snapshot, and the second added a
    # re-read that sat BEHIND an ``if not pending: return`` — so neither could
    # fire in the motivating case, where a superseded attach has not yet
    # scheduled its teardown and the set is EMPTY at the moment shutdown runs.
    #
    # A real deadline also fixes the budget: the previous re-read gave
    # latecomers only RELEASE_DRAIN_CANCEL_GRACE (0.25s), which is less than
    # dlna's own 1.5s teardown bound, so a latecomer was guaranteed to be
    # cancelled inside a teardown deliberately sized to fit.
    # The empty case gets a SHORT grace, not the whole budget: a teardown
    # queued by an attach unwinding right now appears within a loop turn or
    # two, and spending seconds to discover that a clean shutdown really has
    # nothing to drain would tax every ordinary exit. A teardown that is still
    # many seconds away — an attach parked in a 15s connect — cannot be caught
    # by any bounded drain, and this does not pretend otherwise.
    empty_deadline = loop.time() + _DRAIN_EMPTY_GRACE
    while True:
        now = loop.time()
        pending = {t for t in _release_tasks if not t.done()}
        still_running = pending
        if not pending:
            # The grace is CONTINUOUS emptiness, and it is not cancelled by
            # having drained something earlier (2026-08-22 review). Gating it
            # on ``seen`` defeated the whole rewrite in the one case that
            # matters: real shutdown releases every backend first, so ``seen``
            # is non-empty exactly when a latecomer is possible — measured
            # returning in 0.0ms and missing the teardown with ~1.95s of budget
            # unspent.
            if now >= empty_deadline or now >= deadline:
                return          # nothing pending for a full grace window
            await asyncio.sleep(_DRAIN_POLL_INTERVAL)
            continue
        # Pending work resets the grace: the window must measure quiet since
        # the LAST thing finished, not since the drain started.
        empty_deadline = loop.time() + _DRAIN_EMPTY_GRACE
        remaining = deadline - now
        if remaining <= 0:
            break
        seen |= pending
        try:
            _, still_running = await asyncio.wait(pending, timeout=remaining)
        except Exception:  # pragma: no cover — asyncio.wait itself failing
            _log.debug("release drain: wait failed", exc_info=True)
            return
        if still_running:
            break
    if not still_running:
        return
    _log.warning(
        "release drain: %d deferred teardown(s) did not finish within %.1fs — "
        "abandoning them so shutdown is not held by an unreachable device",
        len(still_running), timeout)
    for task in still_running:
        task.cancel()
    try:
        await asyncio.wait(still_running, timeout=RELEASE_DRAIN_CANCEL_GRACE)
    except Exception:  # pragma: no cover
        _log.debug("release drain: cancellation wait failed", exc_info=True)


# ── adopt by compare-and-swap (2026-08-20 plan U3) ───────────────────────────
#
# U1 put every attach behind one lock, so two attaches can no longer interleave
# on a backend. This is the belt to that braces. If a future caller is ever
# added outside the lock — and the first attempt at idle re-attach (#47) is
# direct evidence that new callers to this path DO get added — or an attach is
# cancelled mid-flight, the failure degrades to "released" rather than
# "leaked".
#
# The rule: the LAST attach to start is the one entitled to adopt. Every
# earlier one releases what it built and returns without storing it, so the
# contract above ("an attach either adopts its connection or releases it, no
# third outcome") holds even under an interleaving that should be unreachable.
#
# Why a generation counter and not ``self._device_id != device_id``: every
# backend's ``set_device`` assigns ``_device_id`` ITSELF, so an attach checking
# that field is partly checking against a value it is about to write — and a
# competing attach that has not reached its own assignment yet has left the
# field showing the PREVIOUS device. The token has neither problem, and it also
# supersedes correctly when both attaches name the same device (a re-attach to
# the same speaker is still a new attach, and the old connection is still an
# orphan).


class AttachSuperseded(Exception):
    """``set_device`` connected, then found it no longer owned the backend.

    The third outcome. ``set_device`` used to have two — it returned, or it
    raised — and every caller read "did not raise" as "the device is attached".
    A superseded attach satisfied that test while adopting nothing, so
    ``activate_backend`` persisted the selection and reported success onto a
    backend holding no connection, ``_seed_and_set_device`` told the supervisor
    the re-attach worked and resumed playback into it, and
    ``_startup_reconnect`` returned instead of falling through to discovery.
    All three then failed at the next ``play()`` with a device error the user
    could only clear by pressing Apply again.

    Naming the outcome is what lets each caller do the right thing, and they
    differ: a manual Apply should surface the failure, the supervisor should
    keep retrying, and a boot reconnect should stand down (something newer
    owns the backend, so starting yet another attach would just re-enter the
    race).

    Nothing leaked when this is raised — the attach releases what it built
    before raising. It reports lost ownership, not a failed connection."""


class AttachGeneration:
    """Mixin giving a backend the attach token its ``set_device`` swaps on.

    Three calls, at the three moments that matter::

        async def set_device(self, device_id):
            token = self._begin_attach()      # entry: claim this attach
            <free whatever this backend currently holds>
            conn = await ...connect...        # cannot be cancelled
            if self._attach_superseded(token):
                <release conn>                # never stored, so never adopted
                raise AttachSuperseded(...)
            self._conn = conn                 # the swap

        def release(self):
            self._supersede_attaches()        # ownership ended: nothing may adopt
            ...

    ONE counter, and the "free at entry" line above is what lets it stay one.

    The router's retire stands down when a newer attach has taken over, on the
    premise that the newer attach frees whatever it replaced. A second counter
    was briefly added here (2026-08-21) because Chromecast alone freed its
    outgoing connection at the SWAP, which made that premise false for an
    attach that started and then failed — it freed nothing, so standing down
    leaked a live socket thread. The counter fixed that and immediately broke
    the opposite case: an attach still CONNECTING has not adopted either, so
    the retire released the backend out from under a live re-Apply and the
    user got a 409 for a switch nobody competed with.

    One bit cannot answer both "may this attach adopt?" and "did an attach
    take the connection?". Rather than add a third mechanism, Chromecast was
    changed to free its outgoing at entry like every other backend, which
    makes the premise unconditionally true and leaves one counter sufficient.

    The check belongs as LATE as possible — immediately before the assignment
    that makes the connection current — so everything the attach needed to do
    has been done and only the adoption is skipped. Checking earlier would
    return before a connect that cannot be interrupted anyway, leaking exactly
    the connection this exists to release.

    The default lives on the class, not in ``__init__``, so a backend picks the
    behaviour up by inheritance alone and a partially-constructed instance
    still reads a sane generation."""

    _attach_gen: int = 0

    def _begin_attach(self) -> int:
        """Claim this attach and return its token. Any attach that starts
        after this one supersedes it."""
        self._attach_gen += 1
        return self._attach_gen

    def _attach_superseded(self, token: int) -> bool:
        """True when a later attach has started on this backend, meaning the
        caller must release what it built rather than store it."""
        return self._attach_gen != token

    def _supersede_attaches(self) -> None:
        """End every in-flight attach's claim without starting a new one.

        Call from ``release()``. Burning a generation costs nothing (the token
        is a plain int, and only the holder of the current value may adopt),
        and it converts the one interleaving the compare-and-swap could not
        see — retire-then-adopt — into the same outcome as attach-then-attach:
        the connection is released by its own builder rather than stored."""
        self._attach_gen += 1



def release_signature(method: Callable) -> tuple[str, ...]:
    """The ordered non-self parameter names of a ``release`` implementation.
    Mirrors ``app.output.multiroom.zoning_signature`` — the contract is about
    the call shape, not annotations."""
    sig = inspect.signature(method)
    return tuple(p for p in sig.parameters if p != "self")


def assert_release_contract(backend_cls: type, *, name: str | None = None) -> None:
    """Raise ``AssertionError`` unless *backend_cls* answers the ownership
    question the way every other backend does.

    Checks the three things a reviewer would otherwise have to check by eye:
    the method exists, it is NOT a coroutine function (an awaitable release can
    await a device round-trip — the 25s stall), and it takes no arguments
    (release is unconditional; a ``force=`` flag would mean there is a second
    outcome, and the contract says there is not).

    SCOPE, stated plainly because the earlier wording overclaimed it: this
    verifies the SHAPE of the call, not its behaviour. A plain ``def release``
    that calls ``time.sleep``, joins a thread, or opens a socket passes every
    assertion below while blocking its caller — which is the whole of what #48
    was, since that bug's fix was not "make it synchronous" but "stop calling
    the convenience method that joins the socket thread". The behavioural half
    is enforced separately and statically by
    ``test_no_release_body_makes_a_blocking_call``; neither check can be
    complete, so the property still needs a reviewer's eye on any new
    implementation."""
    label = name or backend_cls.__name__
    member = getattr(backend_cls, "release", None)
    assert member is not None, (
        f"{label} does not implement release() — every output backend must "
        f"name who frees what its attach adopted (2026-08-20 plan U2)")
    assert callable(member), f"{label}.release is not callable"
    assert not inspect.iscoroutinefunction(member), (
        f"{label}.release is a coroutine function; release must be a plain "
        f"def so it cannot await an unreachable device (#48 measured 25.0s)")
    params = release_signature(member)
    assert params == (), (
        f"{label}.release must take no arguments besides self, got {params}")
