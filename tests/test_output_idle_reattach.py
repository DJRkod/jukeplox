"""Idle re-attach coordinator (2026-09-01 plan U4/U5, issue #47).

The first attempt at this feature passed its entire suite and then failed
review with three P0s, and a quieter finding was that the suite could not
detect its own regressions — the headline behaviour could be replaced with a
bare ``return`` with all 265 tests green. So the tests here are written to
FAIL when the behaviour is removed, and the mutants they must catch are listed
in the plan's U7 table and exercised in ``test_idle_reattach_guards``.
"""

import asyncio
import ast
import pathlib

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.output import hold
from app.output.base import OutputDevice
from app.output.idle_reattach import (
    RETRY_CAP_S, RETRY_START_S, IdleReattachCoordinator,
)


# ── harness ─────────────────────────────────────────────────────────────────

class FakeTimer:
    def __init__(self, delay, cb):
        self.delay, self.cb, self.cancelled = delay, cb, False

    def cancel(self):
        self.cancelled = True


class Rig:
    """A coordinator with every collaborator faked, so a test can state the
    world and assert on what the coordinator did about it."""

    def __init__(self, *, selected=("chromecast", "uuid-1"),
                 selected_name="Kitchen", devices=None, playing=False,
                 hold_active=False, attach_result=True):
        self.timers: list[FakeTimer] = []
        self.attach_calls: list[tuple] = []
        self.attach_result = attach_result
        self._selected = selected
        self._selected_name = selected_name
        self._playing = playing
        self._hold = hold_active
        self.backend = MagicMock()
        self.backend._device_id = None

        self.watcher = MagicMock()
        self.watcher.registry = {}
        self.registered: list[tuple[str, str]] = []
        self.removed: list[tuple[str, str]] = []
        self._listeners: dict[tuple[str, str], list] = {}

        def _add(bt, did, cb):
            self.registered.append((bt, did))
            self._listeners.setdefault((bt, did), []).append(cb)

        def _remove(bt, did, cb):
            self.removed.append((bt, did))
            self._listeners.get((bt, did), []).remove(cb)

        self.watcher.add_arrival_listener = _add
        self.watcher.remove_arrival_listener = _remove

        for dev in (devices or []):
            self.add_device(dev)

        async def _attach(backend, backend_type, device_id):
            self.attach_calls.append((backend, backend_type, device_id))
            return self.attach_result

        self.coord = IdleReattachCoordinator(
            timer=self._timer, attach=_attach)
        # Patch the lazy collaborators onto the instance; each is a
        # @staticmethod on the class, so an instance attribute shadows it.
        self.coord._watcher = lambda: self.watcher
        self.coord._selected_key = lambda: self._selected
        self.coord._selected_name = lambda: self._selected_name
        self.coord._backend_for = lambda bt: self.backend
        self.coord._playback_owns = self._playback_owns

    def _timer(self, delay, cb):
        t = FakeTimer(delay, cb)
        self.timers.append(t)
        return t

    def _playback_owns(self, key):
        return self._playing and self.backend._device_id == key[1]

    def add_device(self, device, *, online=True):
        entry = MagicMock()
        entry.device = device
        entry.online = online
        self.watcher.registry[(device.backend_type, device.id)] = entry

    def set_hold(self, active):
        self._hold = active

    def hold_active(self):
        return self._hold

    def fire_arrival(self, key):
        for cb in list(self._listeners.get(key, ())):
            cb()

    def sleep(self, key=None):
        """The speaker went to sleep: the watcher flips it offline.

        Tests must go through this rather than reconciling against an online
        device. The predicate's away conjunct exists so a HEALTHY idle device
        is left alone, and an earlier draft of these tests skipped the sleep
        step — which is exactly why they missed that the coordinator churned
        set_device on every routine re-announcement."""
        key = key or self._selected
        self.watcher.registry[key].online = False

    def wake(self, key=None):
        """The speaker woke: online again, and it announces itself. Matches
        DeviceWatcher._apply_arrival, which flips the entry BEFORE firing
        listeners."""
        key = key or self._selected
        self.watcher.registry[key].online = True
        self.fire_arrival(key)

    @property
    def pending(self):
        return [t for t in self.timers if not t.cancelled]

    @property
    def retry(self):
        """The retry timer the coordinator ACTUALLY holds.

        Not ``pending[0]``: a fired FakeTimer stays in ``timers`` and is never
        cancelled (the real loop drops a fired handle), so the oldest
        un-cancelled entry is a stale one and asserting on it silently reads
        the first delay forever. Read the coordinator's own handle instead."""
        return self.coord._retry_handle


@pytest.fixture(autouse=True)
def _isolate_hold(monkeypatch):
    """Every test states the hold explicitly through its Rig; keep the real
    module flag out of it."""
    monkeypatch.setattr(hold, "_output_hold", False)


def _cast(device_id="uuid-1", name="Kitchen"):
    return OutputDevice(id=device_id, name=name, backend_type="chromecast",
                        id_format="uuid")


def _air(device_id="10.0.0.5:7000", name="Patio"):
    return OutputDevice(id=device_id, name=name, backend_type="airplay",
                        id_format="host_port")


def _rig(**kw):
    rig = Rig(**kw)
    rig.coord.should_watch  # touch, so a rename here fails loudly
    return rig


async def _pump():
    for _ in range(6):
        await asyncio.sleep(0)


# ── the derived predicate (KTD1) ─────────────────────────────────────────────


async def test_watches_the_selection_when_idle_and_detached(monkeypatch):
    rig = _rig(devices=[_cast()])
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)
    rig.sleep()
    assert rig.coord.should_watch() == ("chromecast", "uuid-1")


async def test_does_not_watch_when_nothing_is_selected(monkeypatch):
    rig = _rig(selected=None)
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)
    assert rig.coord.should_watch() is None


async def test_does_not_watch_while_an_outage_hold_owns_the_device(monkeypatch):
    """Origin R9. While the supervisor owns reconnect for a device, this
    module does not act on that device — exactly one component is ever trying
    to attach a given device."""
    rig = _rig(devices=[_cast()], hold_active=True)
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)
    rig.sleep()
    assert rig.coord.should_watch() is None


async def test_does_not_watch_while_playback_runs_on_the_selection(monkeypatch):
    """Origin R6/AE9. The device is attached and playing; re-attaching would
    tear down the transport and stall the queue with no error anywhere."""
    rig = _rig(devices=[_cast()], playing=True)
    rig.backend._device_id = "uuid-1"
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)
    rig.sleep()   # mDNS record lapsed while it kept playing
    assert rig.coord.should_watch() is None


async def test_a_sleeping_device_is_still_watched_though_its_id_is_remembered(
        monkeypatch):
    """The distinction the R6 guard has to get right. A sleeping device still
    has its id remembered by the backend — that IS this feature's situation —
    so keying the guard off remembered id alone would make the predicate
    permanently false and the whole feature inert.

    This exercises the PREDICATE's use of the guard. The guard itself is
    stubbed by the Rig, so the production _playback_owns is covered separately
    below — a mutation run proved this test alone could not see inside it."""
    rig = _rig(devices=[_cast()], playing=False)
    rig.backend._device_id = "uuid-1"   # remembered, but nothing is playing
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)
    rig.sleep()
    assert rig.coord.should_watch() == ("chromecast", "uuid-1")


async def test_predicate_never_raises(monkeypatch):
    """A coordinator that cannot evaluate its own predicate falls silent
    rather than breaking whichever caller asked."""
    rig = _rig()
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)

    def _boom():
        raise RuntimeError("state is gone")
    rig.coord._selected_key = _boom
    assert rig.coord.should_watch() is None


# ── reconcile: idempotence and the retire edges ─────────────────────────────


async def test_reconcile_registers_exactly_one_listener(monkeypatch):
    rig = _rig(devices=[_cast()])
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)
    rig.sleep()
    rig.coord.reconcile()
    rig.coord.reconcile()
    rig.coord.reconcile()
    await _pump()
    assert rig.registered == [("chromecast", "uuid-1")]


async def test_reconcile_before_the_watcher_exists_registers_nothing(
        monkeypatch):
    """The boot race, as observed on the rig 2026-09-05.

    app.main starts the watcher AFTER state.setup(), and state.setup() spawns
    _startup_reconnect as a task whose finally reconciles. On the
    cached-address path that task finishes first, so the coordinator's first
    reconcile runs with no watcher at all."""
    rig = _rig(devices=[_cast()])
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)
    rig.sleep()
    rig.coord._watcher = lambda: None

    rig.coord.reconcile()
    await _pump()

    assert rig.registered == []
    # The load-bearing half: it must not believe it registered. If _listener
    # were set here, the reconcile that runs once the watcher IS up would see
    # current == wanted and return early — leaving the coordinator inert for
    # the life of the process, which is the bug this test exists for.
    assert rig.coord._listener is None


async def test_a_later_reconcile_arms_once_the_watcher_arrives(monkeypatch):
    """The fix: app.main reconciles again after start_watcher() returns.

    This is the regression witness. Before the fix the second reconcile was
    never made, and a cold boot with the selected speaker asleep produced zero
    coordinator activity — no listener, no retry floor, nothing to heal it."""
    rig = _rig(devices=[_cast()])
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)
    rig.sleep()

    rig.coord._watcher = lambda: None
    rig.coord.reconcile()
    await _pump()
    assert rig.registered == []

    rig.coord._watcher = lambda: rig.watcher
    rig.coord.reconcile()
    await _pump()

    assert rig.registered == [("chromecast", "uuid-1")]
    # And the backoff floor is armed, so the device is recovered even if its
    # arrival announcement is missed entirely.
    assert rig.retry is not None
    rig.wake()
    await _pump()
    assert rig.attach_calls


async def test_reconcile_unregisters_when_the_predicate_goes_false(monkeypatch):
    """The shape that replaces five remembered retire edges: the listener
    simply follows the predicate."""
    rig = _rig(devices=[_cast()])
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)
    rig.sleep()
    rig.coord.reconcile()
    await _pump()
    assert rig.registered == [("chromecast", "uuid-1")]

    rig.set_hold(True)
    rig.coord.reconcile()
    assert rig.removed == [("chromecast", "uuid-1")]
    assert rig.coord.should_watch() is None


async def test_reconcile_moves_the_listener_when_the_selection_changes(
        monkeypatch):
    rig = _rig(devices=[_cast(), _air()])
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)
    rig.sleep(("chromecast", "uuid-1"))
    rig.sleep(("airplay", "10.0.0.5:7000"))
    rig.coord.reconcile()
    await _pump()

    rig._selected = ("airplay", "10.0.0.5:7000")
    rig._selected_name = "Patio"
    rig.coord.reconcile()
    await _pump()
    assert rig.removed == [("chromecast", "uuid-1")]
    assert rig.registered[-1] == ("airplay", "10.0.0.5:7000")


async def test_shutdown_unregisters_unconditionally(monkeypatch):
    """Origin R10, shutdown edge. Not routed through the predicate: at
    shutdown the answer must be "nothing", whatever the selection says."""
    rig = _rig(devices=[_cast()])
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)
    rig.sleep()
    rig.coord.reconcile()
    await _pump()

    rig.coord.shutdown()
    assert rig.removed == [("chromecast", "uuid-1")]
    assert rig.coord._listener is None
    assert rig.retry is None


async def test_reconcile_is_fail_soft(monkeypatch):
    """Called from Apply, hold transitions, boot and shutdown — none of which
    may break because reconnect bookkeeping had a bad day."""
    rig = _rig(devices=[_cast()])
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)

    rig.sleep()

    def _boom(*a, **k):
        raise RuntimeError("watcher exploded")
    rig.watcher.add_arrival_listener = _boom
    rig.coord.reconcile()          # must not raise
    assert rig.coord._listener is None


# ── attaching on arrival ────────────────────────────────────────────────────


async def test_arrival_attaches_the_verified_device(monkeypatch):
    """Origin AE1/F1: the device returns, the coordinator attaches, and the
    host never learns the feature exists.

    Verified to FAIL when the attach call in _attempt is removed."""
    rig = _rig(devices=[_cast()])
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)
    rig.sleep()
    rig.coord.reconcile()
    rig.wake()
    await _pump()
    assert rig.attach_calls == [(rig.backend, "chromecast", "uuid-1")]


async def test_a_successful_attach_stops_watching(monkeypatch):
    """Origin R10, "the device attaches" edge — reached by re-deriving, not by
    a bespoke retire call."""
    rig = _rig(devices=[_cast()])
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)
    rig.sleep()
    rig.coord.reconcile()
    rig.wake()
    await _pump()
    # Attached and back online → the predicate goes false on the away
    # conjunct alone; playing makes it doubly so.
    rig._playing = True
    rig.backend._device_id = "uuid-1"
    rig.coord.reconcile()
    assert rig.removed == [("chromecast", "uuid-1")]


async def test_unverifiable_arrival_is_not_attached(monkeypatch):
    """Origin R4/AE4: two known devices share a name and the selection is one
    of them — no attach, and nothing said about it.

    Verified to FAIL when the identity gate in _attempt is removed."""
    rig = _rig(selected=("airplay", "10.0.0.5:7000"), selected_name="Speaker",
               devices=[_air("10.0.0.5:7000", "Speaker"),
                        _air("10.0.0.6:7000", "Speaker")])
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)
    rig.sleep()
    rig.coord.reconcile()
    rig.wake()
    await _pump()
    assert rig.attach_calls == []


async def test_airplay_attaches_across_a_changed_dhcp_lease(monkeypatch):
    """The case name-based identity exists for. The selection's stored id is a
    stale address; the device is back on a new one and is still recognised."""
    rig = _rig(selected=("airplay", "10.0.0.5:7000"), selected_name="Patio",
               devices=[_air("10.0.0.99:7000", "Patio")])
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)
    # The listener is keyed to the selection, but the arrival is the device
    # actually in the registry — which is how the watcher reports it.
    rig.coord._identity_ok = lambda key: True
    rig.sleep(("airplay", "10.0.0.99:7000"))
    rig._selected = ("airplay", "10.0.0.99:7000")
    rig.coord.reconcile()
    rig.wake(("airplay", "10.0.0.99:7000"))
    await _pump()
    assert rig.attach_calls == [(rig.backend, "airplay", "10.0.0.99:7000")]


async def test_arrival_while_a_hold_owns_the_device_does_nothing(monkeypatch):
    """Origin AE6. The coordinator is watching, playback then fails into an
    outage hold on the same device, and the device returns: only the
    supervisor attaches it."""
    rig = _rig(devices=[_cast()])
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)
    rig.sleep()
    rig.coord.reconcile()
    rig.attach_calls.clear()

    rig.set_hold(True)
    rig.wake()
    await _pump()
    assert rig.attach_calls == []


async def test_arrival_of_an_attached_playing_device_does_not_reattach(
        monkeypatch):
    """Origin AE9, and the P0 from the first attempt: routine discovery sees a
    device the supervisor already recovered and playback is running on it.
    Nothing re-attaches it; playback is uninterrupted."""
    rig = _rig(devices=[_cast()])
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)
    rig.sleep()
    rig.coord.reconcile()
    rig.attach_calls.clear()

    rig._playing = True
    rig.backend._device_id = "uuid-1"
    rig.wake()
    await _pump()
    assert rig.attach_calls == []


async def test_arrival_burst_is_a_single_attach(monkeypatch):
    """Arrivals fire on EVERY announcement, not just the offline→online edge,
    so a burst must collapse to one attach."""
    rig = _rig(devices=[_cast()])
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)

    gate = asyncio.Event()

    async def _slow(backend, bt, did):
        rig.attach_calls.append((backend, bt, did))
        await gate.wait()
        return True
    rig.coord._attach = _slow

    rig.sleep()
    rig.coord.reconcile()
    await asyncio.sleep(0)
    for _ in range(5):
        rig.wake()
    await asyncio.sleep(0)
    assert len(rig.attach_calls) == 1
    gate.set()
    await _pump()


# ── retry (KTD4) ────────────────────────────────────────────────────────────


async def test_failed_attach_is_retried(monkeypatch):
    """Origin R5: a device slow to wake is retried until it attaches.

    Verified to FAIL when _arm_retry's call in the failure path is removed."""
    rig = _rig(devices=[_cast()], attach_result=False)
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)
    rig.sleep()
    rig.coord.reconcile()
    rig.wake()
    await _pump()

    assert len(rig.attach_calls) == 1
    assert rig.retry is not None and rig.retry.delay == RETRY_START_S

    rig.attach_result = True
    rig.retry.cb()
    await _pump()
    assert len(rig.attach_calls) == 2


async def test_retry_backs_off_to_the_cap_and_no_further(monkeypatch):
    rig = _rig(devices=[_cast()], attach_result=False)
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)
    rig.sleep()
    rig.coord.reconcile()
    rig.wake()
    await _pump()

    seen = []
    for _ in range(12):
        armed = rig.retry
        assert armed is not None, (
            "R5: retry must never give up while the predicate holds")
        seen.append(armed.delay)
        armed.cb()
        await _pump()

    assert seen[0] == RETRY_START_S
    assert seen[-1] == RETRY_CAP_S
    assert max(seen) == RETRY_CAP_S
    assert seen == sorted(seen), "delay must grow monotonically"


async def test_a_successful_attach_resets_the_backoff(monkeypatch):
    rig = _rig(devices=[_cast()], attach_result=False)
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)
    rig.sleep()
    rig.coord.reconcile()
    rig.wake()
    await _pump()
    rig.retry.cb()
    await _pump()
    assert rig.coord._delay_s > RETRY_START_S

    rig.attach_result = True
    rig.retry.cb()
    await _pump()
    assert rig.coord._delay_s == RETRY_START_S


async def test_unregistering_cancels_a_pending_retry(monkeypatch):
    """No leaked timers: the watcher's own no-leak invariant, applied here."""
    rig = _rig(devices=[_cast()], attach_result=False)
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)
    rig.sleep()
    rig.coord.reconcile()
    rig.wake()
    await _pump()
    armed = rig.retry
    assert armed is not None

    rig.set_hold(True)
    rig.coord.reconcile()
    assert rig.retry is None
    assert armed.cancelled is True


async def test_an_attach_that_raises_is_retried_not_lost(monkeypatch):
    rig = _rig(devices=[_cast()])
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)

    async def _boom(*a):
        raise RuntimeError("connect blew up")
    rig.coord._attach = _boom

    rig.sleep()
    rig.coord.reconcile()
    rig.wake()
    await _pump()
    assert rig.retry is not None and rig.retry.delay == RETRY_START_S
    assert rig.coord._attach_inflight is False


# ── R7: this module does not touch playback ─────────────────────────────────


async def test_attach_starts_nothing(monkeypatch):
    """Origin R7/AE3: tracks left over from a previous party, the speaker
    returns, and nothing plays. The coordinator's responsibility ends at
    being attached."""
    import app.state as st
    rig = _rig(devices=[_cast()])
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)

    played = []
    monkeypatch.setattr(st, "_trigger_auto_advance",
                        AsyncMock(side_effect=lambda: played.append("advance")))
    monkeypatch.setattr(st, "_auto_advance_pending", False)

    rig.sleep()
    rig.coord.reconcile()
    rig.wake()
    await _pump()

    assert rig.attach_calls, "the attach itself must have happened"
    assert played == []
    assert st._auto_advance_pending is False


def test_main_reconciles_after_the_watcher_starts():
    """The boot handoff must live AFTER start_watcher() in app.main.

    A structural guard rather than a behavioural one because the thing that
    broke is an ORDERING, and ordering is what a lifespan test with both
    collaborators faked would stop checking. The two tests above prove the
    coordinator arms when reconciled with a watcher present; this proves the
    application actually reconciles at a point where one is.

    Deleting the call, or hoisting it above start_watcher(), fails here."""
    src = pathlib.Path("app/main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    lifespan = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
        and n.name == "lifespan")

    start_line = reconcile_line = None
    for node in ast.walk(lifespan):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "start_watcher":
            start_line = node.lineno
        if (isinstance(func, ast.Attribute) and func.attr == "reconcile"
                and isinstance(func.value, ast.Name)
                and func.value.id == "idle_reattach"):
            reconcile_line = node.lineno

    assert start_line is not None, "lifespan no longer starts the watcher"
    assert reconcile_line is not None, (
        "lifespan does not hand off to the idle re-attach coordinator; a cold "
        "boot with the selected device asleep will register no listener")
    assert reconcile_line > start_line, (
        "the handoff runs before the watcher exists — _register has nothing "
        "to register with, which is the 2026-09-05 rig failure exactly")


def test_module_never_references_the_playback_surface():
    """R7 made STRUCTURAL rather than remembered (plan U7). A behavioural test
    proves today's code does not play; this proves a future refactor cannot
    quietly introduce it, which is the same technique
    tests/test_output_attach_ownership.py uses for the release contract."""
    src = pathlib.Path("app/output/idle_reattach.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    forbidden = {
        "_trigger_auto_advance", "_do_advance", "_should_auto_start",
        "_auto_advance_pending", "play", "pause", "stop", "skip",
        "enqueue", "advance", "close_out", "clear_output_hold",
        "begin_outage",
    }
    hits = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in forbidden:
            hits.add(node.attr)
        elif isinstance(node, ast.Name) and node.id in forbidden:
            hits.add(node.id)
    assert not hits, (
        f"idle_reattach.py must not touch the playback surface; found: "
        f"{sorted(hits)}. Its responsibility ends at being attached (R7).")


def test_the_ast_guard_is_not_vacuous():
    """A guard that cannot fail is not a guard. Prove it fires on a module
    that DOES call the playback surface."""
    tree = ast.parse("from app import state\n"
                     "async def go():\n"
                     "    await state._trigger_auto_advance()\n")
    forbidden = {"_trigger_auto_advance"}
    hits = {n.attr for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and n.attr in forbidden}
    assert hits == {"_trigger_auto_advance"}


# ── the production R6 guard, unstubbed ──────────────────────────────────────
#
# Every test above replaces _playback_owns with the Rig's stub, so none of them
# can see a defect INSIDE it. A mutation run made that concrete: deleting the
# is_playing conjunct left all 26 tests green. These exercise the real thing.


def _guard(monkeypatch, *, playing, active_backend, active_device_id,
           selected=("chromecast", "uuid-1")):
    import app.state as st
    backend = MagicMock()
    backend._device_id = active_device_id
    router = MagicMock()
    router.active = active_backend if active_backend is None else backend
    monkeypatch.setattr(st, "output_router", router)
    qe = MagicMock()
    qe.state.is_playing = playing
    monkeypatch.setattr(st, "queue_engine", qe)
    monkeypatch.setattr(st, "_selected_output_backend", selected[0])
    monkeypatch.setattr(st, "_selected_output_device", selected[1])
    return IdleReattachCoordinator._playback_owns(selected)


def test_real_guard_blocks_an_attached_playing_device(monkeypatch):
    """AE9: playback is running on the selection — hands off."""
    assert _guard(monkeypatch, playing=True, active_backend=object(),
                  active_device_id="uuid-1") is True


def test_real_guard_allows_a_sleeping_device(monkeypatch):
    """The load-bearing case. The backend still REMEMBERS the device id while
    the speaker sleeps, so a guard keyed on memory alone would be permanently
    true and the feature would never fire. Nothing is playing, so the guard
    must be false.

    Verified to FAIL when the is_playing conjunct is removed — the mutation
    that survived the Rig-stubbed tests."""
    assert _guard(monkeypatch, playing=True, active_backend=object(),
                  active_device_id="uuid-1") is True   # not vacuous
    assert _guard(monkeypatch, playing=False, active_backend=object(),
                  active_device_id="uuid-1") is False


def test_real_guard_allows_a_different_device_playing(monkeypatch):
    """Something else is playing on another device: the selection is still
    detached and still ours to re-attach."""
    assert _guard(monkeypatch, playing=True, active_backend=object(),
                  active_device_id="uuid-OTHER") is False


def test_real_guard_allows_when_no_backend_is_active(monkeypatch):
    assert _guard(monkeypatch, playing=True, active_backend=None,
                  active_device_id="uuid-1") is False


# ── U5: the reconcile triggers (origin R10) ─────────────────────────────────
#
# Origin R10 asks that every way the coordinator stops being responsible be
# "defined and exercised". Under the derived model those are not five pieces
# of logic — they are five places that call reconcile(). So what these tests
# exercise is that each site actually calls it, and that a raising reconcile
# cannot break the thing it was called from.


def _spy_reconcile(monkeypatch):
    from app.output import idle_reattach as ir
    calls = []
    monkeypatch.setattr(ir, "reconcile", lambda: calls.append(True))
    return calls


async def test_hold_clear_reconciles(monkeypatch):
    """Origin F3, the hand-back half: the supervisor resolves or the hold
    clears, and ownership returns to the coordinator — but only if the device
    is still detached, which the predicate decides rather than this site."""
    calls = _spy_reconcile(monkeypatch)
    monkeypatch.setattr(hold, "_output_hold", True)
    monkeypatch.setattr(hold, "_output_hold_reason", "connection_lost")
    import app.output.session_events as se
    monkeypatch.setattr(se, "_schedule_emit", lambda: None)
    from app.output import session
    monkeypatch.setattr(session.get_supervisor(), "retire_outage", lambda: None)

    hold.clear_output_hold()
    assert calls, "hold exit must re-derive idle ownership"


async def test_hold_clear_survives_a_raising_reconcile(monkeypatch):
    """Fail-soft: the hold must still clear."""
    from app.output import idle_reattach as ir

    def _boom():
        raise RuntimeError("coordinator exploded")
    monkeypatch.setattr(ir, "reconcile", _boom)
    monkeypatch.setattr(hold, "_output_hold", True)
    import app.output.session_events as se
    monkeypatch.setattr(se, "_schedule_emit", lambda: None)
    from app.output import session
    monkeypatch.setattr(session.get_supervisor(), "retire_outage", lambda: None)

    hold.clear_output_hold()
    assert hold.output_hold_active() is False


async def test_outage_entry_reconciles(monkeypatch):
    """Origin AE6/R9, the stand-down half: the supervisor takes ownership and
    the coordinator stops acting on that device."""
    calls = _spy_reconcile(monkeypatch)
    from app.output import session
    import app.state as st
    sup = session.OutputSessionSupervisor()
    monkeypatch.setattr(st, "_backend_type_of", lambda b: "chromecast")
    monkeypatch.setattr(st, "advance_gen", lambda: 0)

    backend = MagicMock()
    backend._device_id = "uuid-1"
    sup.begin_outage("connection_lost", backend=backend, was_paused=False,
                     position_ms=0)
    assert calls, "outage entry must re-derive idle ownership"


async def test_outage_entry_with_no_addressable_device_still_reconciles(
        monkeypatch):
    """The early-return branch. An outage with no addressable device still
    OWNS the hold, and the coordinator's predicate keys off the hold — so
    skipping the reconcile here would leave it watching a device the
    supervisor now owns.

    Verified to FAIL when the reconcile is moved below the early return."""
    calls = _spy_reconcile(monkeypatch)
    from app.output import session
    import app.state as st
    sup = session.OutputSessionSupervisor()
    monkeypatch.setattr(st, "_backend_type_of", lambda b: "")
    monkeypatch.setattr(st, "advance_gen", lambda: 0)

    backend = MagicMock()
    backend._device_id = None      # nothing addressable
    sup.begin_outage("connection_lost", backend=backend, was_paused=False,
                     position_ms=0)
    assert calls


async def test_apply_success_reconciles_and_refreshes_the_exemption(
        monkeypatch):
    from app.api import admin
    from app.output import idle_reattach as ir, watcher as watcher_mod

    calls, refreshed = [], []
    monkeypatch.setattr(ir, "reconcile", lambda: calls.append(True))
    w = MagicMock()
    w.refresh_purge_exemption = lambda: refreshed.append(True)
    monkeypatch.setattr(watcher_mod, "get_watcher", lambda: w)

    admin._reconcile_idle_reattach()
    assert calls and refreshed


async def test_apply_reconcile_is_fail_soft_on_both_collaborators(monkeypatch):
    """A successful Apply must not become an error response, and a failed one
    must not have its real error masked, because reconnect bookkeeping had a
    bad day."""
    from app.api import admin
    from app.output import idle_reattach as ir, watcher as watcher_mod

    def _boom(*a, **k):
        raise RuntimeError("nope")
    monkeypatch.setattr(ir, "reconcile", _boom)
    w = MagicMock()
    w.refresh_purge_exemption = _boom
    monkeypatch.setattr(watcher_mod, "get_watcher", lambda: w)

    admin._reconcile_idle_reattach()   # must not raise


async def test_a_watcher_without_the_refresh_hook_is_tolerated(monkeypatch):
    """hasattr-guarded like the rest of the watcher-to-backend chain, so an
    older or stubbed watcher does not break Apply."""
    from app.api import admin
    from app.output import idle_reattach as ir, watcher as watcher_mod

    monkeypatch.setattr(ir, "reconcile", lambda: None)
    monkeypatch.setattr(watcher_mod, "get_watcher", lambda: object())
    admin._reconcile_idle_reattach()


async def test_startup_reconnect_reconciles_on_every_path(monkeypatch):
    """A boot-time miss stops being terminal: the device may simply be asleep,
    so it is handed to the coordinator to await rather than leaving the
    admin rescan message as the only way back.

    Both the failure path AND the success path are covered — the reconcile is
    in a `finally` precisely so no branch can forget it."""
    import app.state as st
    from app.output import idle_reattach as ir

    calls = []
    monkeypatch.setattr(ir, "reconcile", lambda: calls.append(True))
    monkeypatch.setattr(st, "get_plex_client", AsyncMock())

    backend = MagicMock()
    backend.set_device = AsyncMock(side_effect=RuntimeError("unreachable"))
    backend.discover_devices = AsyncMock(return_value=[])
    with patch("app.database.get_setting", AsyncMock(return_value=None)), \
         patch("app.events.bus.manager.broadcast_to_admins", AsyncMock()):
        await st._startup_reconnect(backend, "uuid-1")
    assert len(calls) == 1, "the failure path must hand off to the coordinator"

    backend.set_device = AsyncMock()
    with patch("app.database.get_setting", AsyncMock(return_value=None)):
        await st._startup_reconnect(backend, "uuid-1")
    assert len(calls) == 2, "the success path reconciles too (a cheap no-op)"


async def test_shutdown_tears_the_coordinator_down(monkeypatch):
    """Origin R10, shutdown edge, at the real call site."""
    import app.main as main_mod
    from app.output import idle_reattach as ir

    torn = []
    fake = MagicMock()
    fake.shutdown = lambda: torn.append(True)
    monkeypatch.setattr(ir, "get_coordinator", lambda: fake)
    import app.state as st
    monkeypatch.setattr(st, "all_output_backends", lambda: [])

    await main_mod._release_output_backends()
    assert torn, "shutdown must unregister the coordinator listener"


# ── AE2: the integration this whole feature exists to enable ────────────────


async def test_queueing_after_reattach_plays_via_the_existing_auto_start(
        monkeypatch):
    """Origin AE2, and the one case where playback SHOULD follow an idle
    re-attach — through the pre-existing queue-changed auto-start, never
    through the coordinator.

    This is the flow from the problem frame end to end: the speaker returns,
    Jukeplox attaches silently, the host queues one track, and it plays. If
    the coordinator attach somehow suppressed auto-start (by leaving a hold
    set, or by claiming _auto_advance_pending), the host actual sequence
    would still be broken and every other test here would still pass."""
    import app.state as st

    rig = _rig(devices=[_cast()])
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)
    monkeypatch.setattr(hold, "_output_hold", False)

    rig.sleep()
    rig.coord.reconcile()
    rig.wake()
    await _pump()
    assert rig.attach_calls, "precondition: the coordinator attached"

    # Now the host queues a track. The canonical auto-start predicate must
    # say yes — nothing the attach did may have closed that gate.
    qe = MagicMock()
    qe.state.is_playing = False
    qe.queue = [MagicMock()]
    monkeypatch.setattr(st, "queue_engine", qe)
    monkeypatch.setattr(st, "_auto_advance_pending", False)
    monkeypatch.setattr(st, "_closing_active", False)
    monkeypatch.setattr(st, "radio_active", lambda: False)

    assert st._should_auto_start() is True


# ── R6 as written: never attach a device already attached ───────────────────
#
# Found while writing the end-to-end suite, and it was a real defect rather
# than a test problem. The predicate originally guarded only the mid-playback
# case, so a HEALTHY IDLE attached device was watched and re-attached on every
# routine mDNS re-announcement — churning set_device for nothing.
#
# Every attach-path test above had modelled an online device and then expected
# an attach, which is why none of them could see it. They now go through
# sleep() first, and these pin the boundary directly.


async def test_an_online_attached_device_is_not_watched_at_all(monkeypatch):
    """R6 as written, not just its mid-playback special case. Nothing to
    re-attach to a device that is present.

    Verified to FAIL when the away conjunct is removed from should_watch."""
    rig = _rig(devices=[_cast()])
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)
    rig.backend._device_id = "uuid-1"      # attached
    assert rig.coord.should_watch() is None
    rig.coord.reconcile()
    await _pump()
    assert rig.registered == []
    assert rig.attach_calls == []


async def test_routine_reannouncement_of_a_healthy_device_causes_no_attach(
        monkeypatch):
    """The churn this guard exists to prevent. A device that never slept
    announces itself repeatedly, as mDNS devices do; nothing should happen.

    Verified to FAIL when the away conjunct is removed."""
    rig = _rig(devices=[_cast()])
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)
    rig.coord.reconcile()
    for _ in range(5):
        rig.fire_arrival(("chromecast", "uuid-1"))
    await _pump()
    assert rig.attach_calls == []


async def test_a_device_unknown_to_the_watcher_counts_as_away(monkeypatch):
    """A device evicted before the selection was made, or a watcher that has
    not started yet, must not silence the coordinator — otherwise a restart
    with a long-asleep speaker would never re-attach it."""
    rig = _rig(devices=[])          # registry has no entry for the selection
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)
    assert rig.coord.should_watch() == ("chromecast", "uuid-1")


async def test_no_watcher_at_all_counts_as_away(monkeypatch):
    rig = _rig(devices=[_cast()])
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)
    rig.coord._watcher = lambda: None
    assert rig.coord.should_watch() == ("chromecast", "uuid-1")


async def test_arrival_does_not_re_ask_the_away_conjunct(monkeypatch):
    """The asymmetry that makes the whole thing work, stated as a test.

    DeviceWatcher._apply_arrival flips the entry to online BEFORE firing
    listeners, so by the time the coordinator is called the device is present
    and the away conjunct is false. If _on_arrival re-asked the full
    predicate it would decline every single attach the module exists to
    perform — the feature would be inert while every other test passed.

    Verified to FAIL when _on_arrival is changed back to comparing against
    should_watch()."""
    rig = _rig(devices=[_cast()])
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)
    rig.sleep()
    rig.coord.reconcile()
    assert rig.coord.should_watch() == ("chromecast", "uuid-1")

    rig.wake()                       # online flips first, then the callback
    await _pump()
    assert rig.coord.should_watch() is None, "precondition: now present"
    assert rig.attach_calls == [(rig.backend, "chromecast", "uuid-1")]


async def test_registration_arms_the_floor_without_spending_a_backoff_step(
        monkeypatch):
    """A device can come back without announcing — it may have returned
    before this listener existed — so registration arms the retry floor as a
    safety net. But only a failed ATTEMPT may escalate the delay, or the
    first real retry would already be at double the floor."""
    rig = _rig(devices=[_cast()], attach_result=False)
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)
    rig.sleep()
    rig.coord.reconcile()
    await _pump()

    assert rig.attach_calls == [], "nothing to attach to an absent device"
    assert rig.retry is not None and rig.retry.delay == RETRY_START_S
    assert rig.coord._delay_s == RETRY_START_S, "registration must not escalate"


# ── loop-less contexts ──────────────────────────────────────────────────────
#
# reconcile() is synchronous and is called from places that may not be on the
# event loop — shutdown, and any caller constructing a coordinator directly.
# get_event_loop() is deprecated outside a running loop in 3.11 and raises in
# 3.12, and ensure_future raises outright, so both paths degrade explicitly
# rather than taking down whatever called reconcile.


def test_reconcile_off_the_loop_does_not_raise():
    """A bare synchronous caller with no running loop. The listener still
    registers (the arrival path is the primary trigger); only the backoff
    floor is skipped."""
    coord = IdleReattachCoordinator()
    watcher = MagicMock()
    watcher.registry = {}
    registered = []
    watcher.add_arrival_listener = lambda bt, d, cb: registered.append((bt, d))
    watcher.remove_arrival_listener = lambda *a: None
    coord._watcher = lambda: watcher
    coord._selected_key = lambda: ("chromecast", "uuid-1")
    coord._selected_name = lambda: "Kitchen"
    coord._playback_owns = lambda k: False

    coord.reconcile()                       # must not raise

    assert registered == [("chromecast", "uuid-1")]
    assert coord._retry_handle is None, "no loop, so nothing to arm"


def test_arrival_off_the_loop_does_not_wedge_single_flight():
    """The trap this guards. Setting _attach_inflight before discovering
    there is no loop would leave the flag True forever, and every later
    arrival — on a perfectly good loop — would be dropped as a duplicate."""
    coord = IdleReattachCoordinator()
    watcher = MagicMock()
    watcher.registry = {}
    watcher.add_arrival_listener = lambda *a: None
    watcher.remove_arrival_listener = lambda *a: None
    coord._watcher = lambda: watcher
    coord._selected_key = lambda: ("chromecast", "uuid-1")
    coord._selected_name = lambda: "Kitchen"
    coord._playback_owns = lambda k: False

    coord._on_arrival(("chromecast", "uuid-1"))     # must not raise

    assert coord._attach_inflight is False, (
        "single-flight guard wedged: no later arrival could ever attach")


async def test_the_real_default_timer_arms_on_a_live_loop():
    """The positive half — proving the degradation above is a degradation and
    not the only behaviour."""
    fired = []
    handle = IdleReattachCoordinator._default_timer(0.001, lambda: fired.append(1))
    assert handle is not None
    await asyncio.sleep(0.02)
    assert fired == [1]


# ── the boot path's OTHER branch, found on the validation rig 2026-09-03 ────
#
# test_startup_reconnect_reconciles_on_every_path above stubs get_setting to
# None, so `addr_raw` is falsy and only the DISCOVERY branch ever runs. It
# asserted "every path" while covering one — a false witness, and the reason
# a real defect shipped to the rig.
#
# The cached-address branch returns early on success and is the NORMAL boot
# path once output_addr:{device_id} has been persisted. With the reconcile in
# a `finally` inside the body, that early return skipped it, leaving the
# coordinator unarmed. A restart with a sleeping speaker then never
# re-attached: the device is absent from the rebuilt registry so no offline
# edge fires either, and boot is the only trigger there is.


async def test_startup_reconnect_reconciles_on_the_cached_address_path(
        monkeypatch):
    """Verified to FAIL when the reconcile is moved back inside the body."""
    import app.state as st
    from app.output import idle_reattach as ir

    calls = []
    monkeypatch.setattr(ir, "reconcile", lambda: calls.append(True))
    monkeypatch.setattr(st, "_seed_startup_address", lambda *a: None)

    backend = MagicMock()
    backend.set_device = AsyncMock()          # cached address attaches cleanly
    backend.discover_devices = AsyncMock(return_value=[])

    with patch("app.database.get_setting",
               AsyncMock(return_value='{"host": "192.0.2.9", "port": 8009}')):
        await st._startup_reconnect(backend, "uuid-1")

    # Not vacuous: prove this really took the cached-address branch.
    backend.set_device.assert_awaited_once()
    backend.discover_devices.assert_not_awaited()
    assert calls == [True], (
        "the cached-address success path must reconcile — it is the normal "
        "boot path, and skipping it leaves idle re-attach permanently inert")


async def test_startup_reconnect_reconciles_when_cached_attach_superseded(
        monkeypatch):
    """The other early return out of the cached-address branch."""
    import app.state as st
    from app.output import base as output_base
    from app.output import idle_reattach as ir

    calls = []
    monkeypatch.setattr(ir, "reconcile", lambda: calls.append(True))
    monkeypatch.setattr(st, "_seed_startup_address", lambda *a: None)

    backend = MagicMock()
    backend.set_device = AsyncMock(
        side_effect=output_base.AttachSuperseded("newer attach owns it"))
    backend.discover_devices = AsyncMock(return_value=[])

    with patch("app.database.get_setting",
               AsyncMock(return_value='{"host": "192.0.2.9", "port": 8009}')):
        await st._startup_reconnect(backend, "uuid-1")

    assert calls == [True]
    backend.discover_devices.assert_not_awaited()


async def test_startup_reconnect_reconciles_even_if_the_attach_raises_oddly(
        monkeypatch):
    """Whatever the body does, the handoff happens. This is what makes the
    wrapper's placement worth having over five remembered call sites."""
    import app.state as st
    from app.output import idle_reattach as ir

    calls = []
    monkeypatch.setattr(ir, "reconcile", lambda: calls.append(True))

    async def _boom(backend, device_id):
        raise KeyboardInterrupt("something no handler expects")
    monkeypatch.setattr(st, "_startup_reconnect_attach", _boom)

    with pytest.raises(KeyboardInterrupt):
        await st._startup_reconnect(MagicMock(), "uuid-1")
    assert calls == [True]


# ── U3: the retry acts on presence, not on elapsed time ────────────────────
#
# _arm_retry's timer callback re-entered _on_arrival, which made a tick
# indistinguishable from the device having announced itself. On a real arrival
# the device has just been seen and attaching is right; on a tick nothing has
# been seen, so the coordinator attached a speaker that had been switched off
# for hours — indefinitely, at the backoff cap.
#
# The backoff's stated purpose (KTD4) is the OTHER case: the device is
# announcing but the attach fails, e.g. a speaker that answers mDNS before its
# audio stack is ready. This enforces that intent rather than changing it.


async def test_retry_tick_attempts_nothing_while_the_device_is_absent(
        monkeypatch):
    """Covers AE3. Verified to FAIL when the presence check is removed from
    the retry path."""
    rig = _rig(devices=[_cast()], attach_result=False)
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)
    rig.sleep()
    rig.coord.reconcile()
    await _pump()
    rig.attach_calls.clear()

    armed = rig.retry
    assert armed is not None, "precondition: the floor is armed"
    armed.cb()                      # time passes; nothing announced
    await _pump()

    assert rig.attach_calls == [], (
        "a backoff tick is not evidence the device is there")


async def test_retry_tick_does_attempt_while_the_device_is_present(
        monkeypatch):
    """The case the backoff exists for and must not regress: the device IS
    announcing, the attach itself keeps failing."""
    rig = _rig(devices=[_cast()], attach_result=False)
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)
    rig.sleep()
    rig.coord.reconcile()
    rig.wake()                      # present again, but attach fails
    await _pump()
    n = len(rig.attach_calls)
    assert n >= 1

    rig.retry.cb()
    await _pump()
    assert len(rig.attach_calls) > n, (
        "a present device with a failing attach must keep retrying")


async def test_absent_retry_leaves_the_listener_registered(monkeypatch):
    """Declining to attempt must not mean giving up: the arrival listener is
    what resumes attempts, so it has to still be there."""
    rig = _rig(devices=[_cast()], attach_result=False)
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)
    rig.sleep()
    rig.coord.reconcile()
    await _pump()
    rig.retry.cb()
    await _pump()

    assert rig.coord._listener is not None
    rig.wake()
    await _pump()
    assert rig.attach_calls, "a real arrival must still drive an attach"


async def test_absent_retry_does_not_escalate_the_backoff(monkeypatch):
    """A tick that attempts nothing has learned nothing, so it must not spend
    a backoff step — otherwise an overnight absence walks the delay to its cap
    for no reason."""
    rig = _rig(devices=[_cast()], attach_result=False)
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)
    rig.sleep()
    rig.coord.reconcile()
    await _pump()
    before = rig.coord._delay_s

    for i in range(3):
        armed = rig.retry
        assert armed is not None, (
            f"tick {i}: the floor must stay armed while the device is absent")
        armed.cb()
        await _pump()
        assert rig.coord._delay_s == before, (
            f"tick {i}: a tick that attempted nothing escalated the backoff")

    assert rig.coord._delay_s == before


# ── code-review findings, 2026-09-04 ────────────────────────────────────────


async def test_absent_retry_keeps_the_floor_armed(monkeypatch):
    """Review finding 1. Declining to attach must not mean declining to watch.

    reconcile() — the forced-Scan merge — sets entry.online directly rather
    than going through _apply_arrival, so it fires NO arrival listener. With
    the floor disarmed, a device that came back via Scan would never be
    attached: no listener callback, no pending timer, and should_watch() now
    false so a later reconcile just unregisters.

    That is exactly the case _arm_retry's docstring promises to cover.

    Verified to FAIL when the decline path returns without re-arming."""
    rig = _rig(devices=[_cast()], attach_result=False)
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)
    rig.sleep()
    rig.coord.reconcile()
    await _pump()

    for _ in range(4):
        armed = rig.retry
        assert armed is not None, "the floor disarmed itself while absent"
        armed.cb()
        await _pump()

    assert rig.attach_calls == [], "still no attach — only presence checks"


async def test_device_returning_without_an_arrival_is_still_picked_up(
        monkeypatch):
    """The scenario finding 1 describes, end to end: the speaker comes back by
    a route that fires no listener (a forced Scan), and the floor is what
    notices."""
    rig = _rig(devices=[_cast()], attach_result=True)
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)
    rig.sleep()
    rig.coord.reconcile()
    await _pump()
    assert rig.attach_calls == []

    # Online again, but via a path that fires no arrival callback.
    rig.watcher.registry[("chromecast", "uuid-1")].online = True
    rig.retry.cb()
    await _pump()

    assert rig.attach_calls == [(rig.backend, "chromecast", "uuid-1")]


async def test_presence_check_failure_does_not_kill_the_floor(monkeypatch):
    """Review finding 5. _fire runs as a bare loop callback; a raise there
    would escape into asyncio's handler with the handle already cleared —
    silent and terminal at the one place nothing else re-arms."""
    rig = _rig(devices=[_cast()], attach_result=False)
    monkeypatch.setattr(hold, "output_hold_active", rig.hold_active)
    rig.sleep()
    rig.coord.reconcile()
    await _pump()

    def _boom(key):
        raise RuntimeError("registry exploded")
    rig.coord._device_is_away = _boom

    rig.retry.cb()          # must not raise
    await _pump()
    assert rig.retry is not None, "a failed check must still leave the floor armed"
