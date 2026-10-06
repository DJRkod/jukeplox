"""End-to-end idle re-attach against a real DLNA renderer (plan U6).

Nothing is mocked at the seam under test. A real ``SyntheticRenderer`` serves
genuine description and SCPD XML over HTTP; the real ``DlnaBackend`` fetches,
parses, and attaches through its ordinary ``UpnpFactory`` path; the real
``DeviceWatcher`` runs the registry state machine; the real
``IdleReattachCoordinator`` does the reconnecting. "The speaker slept" is the
renderer's socket closing, and "the speaker woke" is it reopening.

There is no resume path to exercise: the coordinator's responsibility ends at
being attached (origin R7). So where the abandoned version asserted that a
wake resumed the party, these assert that a wake attaches and starts NOTHING,
and origin AE2 — the host queues a track and it plays via the pre-existing
auto-start — is covered in the unit suite where the queue can be driven.

SCOPE, stated plainly: this covers the DLNA attach branch only. Synthetic
Chromecast needs protobuf over TLS and synthetic AirPlay needs RAOP plus
pairing, so those branches rest on the unit suites plus the hardware pass in
docs/plans/2026-09-01-001-idle-reattach-rig-checklist.md. Nor can a process
restart reproduce what a genuine wake does — fresh DHCP lease, real mDNS
re-announce, transport torn down by the peer. That is the other half of why
the hardware pass is not optional.

Process hygiene: the ``renderer`` fixture stops its server on teardown
including on failure, so a red test never leaves a listening socket behind.
"""

import asyncio
import contextlib
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import app.state as st
from app.output import hold, session
from app.output.dlna import DlnaBackend
from app.output import idle_reattach
from app.output.idle_reattach import IdleReattachCoordinator
from app.output.session import OutputSessionSupervisor
from app.output.watcher import DeviceWatcher, GRACE_S

from tests.conftest import FakeTimerFactory
from tools.synthetic.dlna_renderer import SyntheticRenderer

# set_device does real UPnP work (factory, NotifyServer, SUBSCRIBE); generous
# but bounded so a hang fails the test instead of hanging the run.
ATTACH_TIMEOUT_S = 30


@pytest.fixture
async def renderer():
    r = SyntheticRenderer(friendly_name="Sleepy Speaker")
    await r.start()
    try:
        yield r
    finally:
        await r.stop()          # hygiene: also runs when the test fails


@pytest.fixture
async def db(tmp_path, monkeypatch):
    """set_device persists/reads volume settings, so it needs a real DB."""
    import app.database as database
    from app.config import Settings
    monkeypatch.setattr(database, "settings",
                        Settings(data_dir=tmp_path, secret_key="test"))
    await database.init_db()
    yield database
    with contextlib.suppress(Exception):
        await database.close_db()


class Rig:
    """The wired-together system under test."""

    def __init__(self, backend, watcher, coord, timers, renderer):
        self.backend = backend
        self.watcher = watcher
        self.coord = coord
        self.timers = timers
        self.renderer = renderer

    async def attach(self):
        await asyncio.wait_for(self.backend.set_device(self.renderer.usn),
                               ATTACH_TIMEOUT_S)

    @property
    def key(self):
        return ("dlna", self.renderer.usn)

    @property
    def watching(self):
        """Whether the coordinator currently owns THIS renderer.

        The old design had an ``armed`` flag; the rework has no such field on
        purpose — ownership is DERIVED, and a stored flag was the thing whose
        five retire edges went undefined. So ownership is read off the
        registered listener, which is the only durable trace reconcile()
        leaves.

        Deliberately compares the listener's KEY rather than merely asking
        whether some listener exists. After a switch to another device the
        coordinator is legitimately watching the NEW selection, and a
        bare "is there a listener" check reads that as "still watching the
        sleeper" — which would have made the manual-switch test unfailable in
        the wrong direction."""
        return self.watching_key == self.key

    @property
    def watching_key(self):
        listener = self.coord._listener
        return listener[:2] if listener else None

    def reconcile(self):
        self.coord.reconcile()

    def go_offline(self):
        """Sweep misses the device, grace expires → offline edge fires."""
        self.watcher._sweep_merge("dlna", [])
        for t in list(self.timers.timers):
            if not t.cancelled and t.delay == GRACE_S:
                t.fire()

    async def announce(self, device):
        """The device turns up again — the arrival path the watcher drives."""
        self.watcher._apply_arrival(self.key, device,
                                    probe_host="127.0.0.1")
        await _settle()


async def _settle(rounds=40):
    for _ in range(rounds):
        await asyncio.sleep(0)


async def _teardown_backend(backend):
    """Full destructive cleanup of a DlnaBackend.

    ``DlnaBackend.stop()`` is deliberately NON-destructive — it keeps the
    aiohttp session and GENA subscription alive so a subsequent play() reuses
    them, and in production the next set_device() closes the prior session
    (dlna.py: "a missed close here leaks an aiohttp connection pool per
    set_device call"). That is correct for a long-lived backend and wrong for
    a test process, which would otherwise exit with open connectors and print
    "Unclosed client session" over the results. Nothing here is asserting on
    production behaviour; this is hygiene only."""
    with contextlib.suppress(Exception):
        await asyncio.wait_for(backend.stop(), 10)
    with contextlib.suppress(Exception):
        if backend._notify_server is not None:
            await backend._notify_server.async_stop_server()
            backend._notify_server = None
    with contextlib.suppress(Exception):
        if backend._requester is not None:
            await backend._requester.close()
            backend._requester = None
    with contextlib.suppress(Exception):
        if backend._dlna_session is not None:
            await backend._dlna_session.close()
            backend._dlna_session = None
    await _settle()


async def _build_rig(monkeypatch, renderer, *, queue=None, playing=False):
    backend = DlnaBackend()
    device = await backend.describe_renderer(renderer.location, renderer.usn)
    assert device is not None, "synthetic renderer failed its own description"

    timers = FakeTimerFactory()
    sup = OutputSessionSupervisor(
        record_play=MagicMock(), timer_factory=timers,
        identity_check=AsyncMock(return_value=True),
        held_track_check=AsyncMock(return_value=True),
        window_minutes=AsyncMock(return_value=60),
    )
    monkeypatch.setattr(session, "_supervisor", sup)
    monkeypatch.setattr(hold, "_output_hold", False)

    selection = {"key": ("dlna", renderer.usn)}
    coord = IdleReattachCoordinator(timer=timers)
    coord._selected_key = lambda: selection["key"]
    coord._selected_name = lambda: "Sleepy Speaker"
    coord._backend_for = lambda bt: backend
    # Install it as the process coordinator. The trigger sites (the watcher's
    # offline edge, hold transitions, Apply, boot) all reconcile through
    # idle_reattach.reconcile() -> get_coordinator(), so a rig-local instance
    # that is not installed would be driven by nothing and every "the offline
    # edge started a watch" assertion would be testing the fixture.
    monkeypatch.setattr(idle_reattach, "_coordinator", coord)

    watcher = DeviceWatcher(
        snapshot=lambda: [],
        broadcast=AsyncMock(),
        subscribe=AsyncMock(return_value=None),
        unsubscribe=AsyncMock(),
        backend_for=lambda name: backend if name == "dlna" else None,
        probe=lambda *a: None,
        timer=timers,
        selected_key_for=lambda: ("dlna", renderer.usn),
        ssdp_listen=AsyncMock(side_effect=RuntimeError("no ssdp in tests")),
        dbus_available=AsyncMock(return_value=False),
    )
    monkeypatch.setattr("app.output.watcher._watcher", watcher)
    coord._watcher = lambda: watcher

    # The device is known and online to begin with.
    watcher._apply_arrival(("dlna", renderer.usn), device)

    # The rework has no resume path to stub: the coordinator's
    # responsibility ends at being attached (origin R7), and the pre-existing
    # queue-changed auto-start covers playback. Instead of a resume spy the
    # rig watches the whole playback surface and asserts it stays untouched.
    played = []
    monkeypatch.setattr(st, "_trigger_auto_advance",
                        AsyncMock(side_effect=lambda: played.append("advance")))
    monkeypatch.setattr(st, "_auto_advance_pending", False)

    rig = Rig(backend, watcher, coord, timers, renderer)
    rig.device = device
    rig.selection = selection
    rig.played = played
    return rig


@pytest.fixture
async def rig(monkeypatch, renderer, db):
    r = await _build_rig(monkeypatch, renderer)
    try:
        yield r
    finally:
        await _teardown_backend(r.backend)


# ── the core loop ─────────────────────────────────────────────────────────────

async def test_sleep_then_wake_reattaches_with_no_admin_action(rig):
    """Origin AE1 end to end: attach, the speaker sleeps, the speaker wakes,
    and Jukeplox is attached again without anyone touching Setup.

    The whole loop runs through real components — the watcher's own offline
    transition fires the reconcile, so nothing here reaches in to arm the
    coordinator by hand."""
    await rig.attach()
    assert rig.backend._device_id == rig.renderer.usn
    subs_before = rig.renderer.subscriptions
    assert rig.watching is False, "a healthy attached device is left alone"

    # Sleep. The watcher's offline edge triggers the re-derivation itself.
    await rig.renderer.stop()
    rig.go_offline()
    assert rig.watcher.registry[rig.key].online is False
    assert rig.watching is True, "the offline edge did not start a watch"

    # Wake.
    await rig.renderer.start()
    await rig.announce(rig.device)
    await asyncio.wait_for(_until(lambda: not rig.watching), ATTACH_TIMEOUT_S)

    assert rig.watching is False, "ownership should end once attached"
    assert rig.renderer.subscriptions > subs_before, \
        "no fresh SUBSCRIBE — the device was never really re-attached"


async def test_sleeping_device_stays_in_the_registry(rig):
    """Origin AE5: the entry survives — greyed, not gone. This is what stops
    the picker emptying and forcing a Scan."""
    await rig.attach()
    await rig.renderer.stop()
    rig.go_offline()

    assert rig.key in rig.watcher.registry
    assert rig.watcher.registry[rig.key].online is False
    assert rig.watcher.registry[rig.key].offline_since is not None
    # Exempt from purge entirely — not merely given a longer window.
    assert rig.watcher._purge_timers == {}


async def test_forced_scan_while_asleep_does_not_delete_it(rig):
    await rig.attach()
    await rig.renderer.stop()
    rig.go_offline()

    rig.watcher.reconcile({"dlna": []})

    assert rig.key in rig.watcher.registry


async def test_wake_attaches_and_starts_nothing(rig):
    """Origin R7/AE3, against real I/O. Tracks left from a previous party do
    NOT start playing when the speaker comes back; the coordinator's
    responsibility ends at being attached.

    This is the assertion that replaced the abandoned version's
    "wake resumes a queued party". Resume was cut from scope during the
    brainstorm: it was the single largest source of risk (it caused a Closing
    Time defect and carried a "plays to a device that isn't yours" exposure),
    and the pre-existing queue-changed auto-start already covers the host's
    real sequence — switch the speaker on, THEN queue."""
    await rig.attach()
    await rig.renderer.stop()
    rig.go_offline()

    await rig.renderer.start()
    await rig.announce(rig.device)
    await asyncio.wait_for(_until(lambda: not rig.watching), ATTACH_TIMEOUT_S)

    assert rig.backend._device_id == rig.renderer.usn, "precondition: attached"
    assert rig.played == [], "an idle re-attach must not start playback"


async def test_repeated_sleep_wake_cycles_never_overlap_attaches(rig):
    """Origin AE9 under real I/O: five cycles, and the coordinator is idle
    and correct at the end of each rather than accumulating in-flight
    attempts or leaking listeners."""
    await rig.attach()

    for cycle in range(5):
        await rig.renderer.stop()
        rig.go_offline()
        assert rig.watching is True, f"cycle {cycle}: not watching"

        await rig.renderer.start()
        await rig.announce(rig.device)
        await asyncio.wait_for(_until(lambda: not rig.watching),
                              ATTACH_TIMEOUT_S)
        assert rig.coord._attach_inflight is False, \
            f"cycle {cycle}: attempt leaked"
        assert rig.coord._retry_handle is None, f"cycle {cycle}: timer leaked"

    assert rig.backend._device_id == rig.renderer.usn
    assert rig.played == [], "five sleep/wake cycles must start nothing"


async def test_wake_at_a_different_address_still_attaches(monkeypatch, db):
    """The device came back on a new port — the DHCP-lease-changed shape. The
    attach must use the freshly-registered address, not the stale cached one,
    or every real overnight wake behind a short lease would fail."""
    r = SyntheticRenderer(friendly_name="Wanderer")
    await r.start()
    try:
        rig = await _build_rig(monkeypatch, r)
        try:
            await rig.attach()
            old_port = r.port

            await r.stop()
            rig.go_offline()

            # Comes back somewhere else.
            r._rotate_port = True
            await r.start()
            assert r.port != old_port, "port did not actually rotate"

            fresh = await rig.backend.describe_renderer(r.location, r.usn)
            await rig.announce(fresh)
            await asyncio.wait_for(_until(lambda: not rig.watching),
                                  ATTACH_TIMEOUT_S)

            assert rig.backend._device_locations[r.usn] == r.location
        finally:
            await _teardown_backend(rig.backend)
    finally:
        await r.stop()


async def test_dead_device_keeps_retrying_and_does_not_give_up(rig):
    """Origin R5. A wake that answers discovery but refuses the connection
    must keep the coordinator on the device. Giving up here is the original
    complaint in a new costume."""
    await rig.attach()
    await rig.renderer.stop()
    rig.go_offline()

    # Announce while the renderer is still down — the attach will fail.
    await rig.announce(rig.device)
    await _settle()

    assert rig.watching is True
    assert rig.coord._retry_handle is not None, \
        "no backoff timer armed after a failed attach"
    assert rig.coord._retry_handle.delay > 0


async def test_manual_switch_while_asleep_wins_permanently(rig):
    """Origin R10, the "admin selects something else" edge, end to end. The
    sleeper waking later must not take the output back.

    Note what drives the stand-down: the SELECTION changed, so the derived
    predicate went false. Nothing had to remember to retire a watcher — which
    is the failure the whole rework is shaped around."""
    await rig.attach()
    await rig.renderer.stop()
    rig.go_offline()
    assert rig.watching is True

    rig.selection["key"] = ("dlna", "uuid:some-other-speaker")
    rig.reconcile()
    assert rig.watching is False

    await rig.renderer.start()
    await rig.announce(rig.device)
    await _settle()

    assert rig.watching is False
    assert rig.played == []


async def _until(pred, rounds=4000):
    """Yield until *pred* holds. Real I/O is in flight, so this cannot be a
    fixed number of event-loop turns."""
    for _ in range(rounds):
        if pred():
            return True
        await asyncio.sleep(0.005)
    raise AssertionError("condition never became true")
