"""U2 — cross-backend enforcement of the attach-ownership contract.

``AbstractOutputBackend`` described how to *acquire* a device (``set_device``)
and never how to let one go, so no backend answered the question and an adopted
connection was only ever freed by a *subsequent* ``set_device`` on the same
backend. This file is the structural answer: four device backends (plus the two
backends whose attach genuinely adopts nothing) answering ONE question the same
way, which is worth more than four per-backend suites that each pass alone —
the same posture ``tests/test_output_multiroom_contract.py`` takes on zoning.

The contract, from the plan's High-Level Technical Design:

* An attach either ADOPTS its connection or RELEASES it. No third outcome.
* Exactly one adopted connection per backend at a time.
* Release is NON-BLOCKING and NEVER RAISES — it runs on paths that already have
  a real error to report, or on a shutdown that cannot wait.
* After release, the backend reports no adopted connection.

Two of those properties are enforced by the *signature*, not by convention:
``release`` is a plain ``def``, so it cannot ``await`` a device round-trip, and
the tests below additionally assert that no async teardown has even been
awaited at the moment ``release()`` returns (it is deferred to a background
task). "Non-blocking" is therefore asserted on the call shape, never by timing
— #48 measured ``Chromecast.disconnect()`` blocking 25.0s on the rig and a
wall-clock assertion would have been the wrong tool to catch it.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.output.base import (
    AbstractOutputBackend,
    AttachGeneration,
    AttachSuperseded,
    assert_release_contract,
    release_signature,
)


# ── per-backend specs ────────────────────────────────────────────────────────
# Each spec says how to build a backend, how to hand it a fake adopted
# connection, and which instance attributes must read empty afterwards. The
# generic tests below drive every spec identically; backend-specific structural
# assertions (Chromecast's socket_client route, DLNA's three resources) get
# their own named tests further down.


@dataclass
class BackendSpec:
    name: str
    build: Callable[[], Any]
    # adopt(backend) -> dict of the fake objects the test may assert on
    adopt: Callable[[Any], dict]
    # instance attributes that must be None once release() has returned
    adopted_attrs: tuple[str, ...]
    # (backend, fakes) -> list of AsyncMocks that release must NOT have awaited
    # by the time it returns, but which its deferred teardown does await
    deferred: Callable[[Any, dict], list] = field(
        default=lambda _b, _f: [])
    # names of teardown callables on the fakes that must be made to raise for
    # the never-raises test
    def_break: Callable[[dict], None] = field(default=lambda _f: None)


def _spec_chromecast() -> BackendSpec:
    def build():
        from app.output.chromecast import ChromecastBackend
        return ChromecastBackend()

    def adopt(b):
        from app.output.chromecast import _AdvanceListener, _VolumeListener
        cc = MagicMock()
        b._cast = cc
        b._device_id = "cast-1"
        b._listener = _AdvanceListener(b)
        b._vol_listener = _VolumeListener(b, b._cast)
        b._is_playing = True
        return {"cast": cc}

    def break_it(f):
        f["cast"].socket_client.disconnect.side_effect = OSError("socket gone")
        f["cast"].media_controller.unregister_status_listener.side_effect = \
            RuntimeError("listener registry corrupt")
        f["cast"].unregister_status_listener.side_effect = RuntimeError("boom")

    return BackendSpec(
        name="chromecast", build=build, adopt=adopt,
        adopted_attrs=("_cast", "_listener", "_vol_listener"),
        def_break=break_it,
    )


def _spec_dlna() -> BackendSpec:
    def build():
        from app.output.dlna import DlnaBackend
        return DlnaBackend()

    def adopt(b):
        dmr = MagicMock()
        dmr.async_unsubscribe_services = AsyncMock()
        # spec=[] on purpose (2026-08-20 review R3/ADV-11). The real
        # AiohttpSessionRequester borrows the session and has no close(); a
        # bare MagicMock manufactures one on demand, which is exactly how a
        # teardown step that raised AttributeError on every single release
        # stayed invisible to this entire suite.
        requester = MagicMock(spec=[])
        session = MagicMock()
        session.close = AsyncMock()
        notify = MagicMock()
        notify.async_stop_server = AsyncMock()
        b._dmr = dmr
        b._requester = requester
        b._dlna_session = session
        b._notify_server = notify
        b._device_id = "dlna-1"
        b._is_playing = True
        return {"dmr": dmr, "requester": requester, "session": session,
                "notify": notify}

    def deferred(_b, f):
        # No requester entry: it owns nothing and has no close() — see adopt().
        return [f["dmr"].async_unsubscribe_services, f["notify"].async_stop_server,
                f["session"].close]

    def break_it(f):
        f["dmr"].async_unsubscribe_services.side_effect = OSError("unreachable")
        f["notify"].async_stop_server.side_effect = OSError("already down")
        f["session"].close.side_effect = OSError("closed twice")

    return BackendSpec(
        name="dlna", build=build, adopt=adopt,
        adopted_attrs=("_dmr", "_requester", "_dlna_session", "_notify_server"),
        deferred=deferred, def_break=break_it,
    )


def _spec_plexplayer() -> BackendSpec:
    def build():
        from app.output.plexplayer import PlexPlayerBackend
        return PlexPlayerBackend()

    def adopt(b):
        from app.output.plexplayer import _PlayerSession
        client = MagicMock()
        client.aclose = AsyncMock()
        sess = _PlayerSession(device_id="plx-1", client=client, name="Banana")
        b._session = sess
        b._device_id = "plx-1"
        b._is_playing = True
        return {"client": client, "session": sess}

    def deferred(_b, f):
        return [f["client"].aclose]

    def break_it(f):
        f["client"].aclose.side_effect = OSError("transport already dead")

    return BackendSpec(
        name="plexplayer", build=build, adopt=adopt,
        adopted_attrs=("_session",),
        deferred=deferred, def_break=break_it,
    )


def _spec_airplay() -> BackendSpec:
    def build():
        from app.output import airplay
        return airplay.AirPlayBackend()

    def adopt(b):
        # AirPlay's set_device is a cache write — it adopts nothing. The fake
        # "adoption" here is exactly that: a selected device id and no
        # connection, which release must leave alone (the honest no-op).
        b._device_id = "airplay-1"
        return {}

    return BackendSpec(name="airplay", build=build, adopt=adopt,
                       adopted_attrs=())


def _spec_direct() -> BackendSpec:
    def build():
        from app.output.direct import DirectAudioBackend
        return DirectAudioBackend()

    def adopt(b):
        b._device_id = "default"
        return {}

    return BackendSpec(name="direct", build=build, adopt=adopt,
                       adopted_attrs=())


SPECS = [
    _spec_chromecast(),
    _spec_dlna(),
    _spec_plexplayer(),
    _spec_airplay(),
    _spec_direct(),
]

# Every concrete class the router can hold. Kept separate from SPECS because
# the server-fed backends' attach is literally ``return None`` — they have no
# adoption to fake, but they must still answer the contract.
BACKEND_CLASSES = [
    ("app.output.chromecast", "ChromecastBackend"),
    ("app.output.dlna", "DlnaBackend"),
    ("app.output.plexplayer", "PlexPlayerBackend"),
    ("app.output.airplay", "AirPlayBackend"),
    ("app.output.direct", "DirectAudioBackend"),
    ("app.output.snapcast", "SnapcastBackend"),
    ("app.output.sendspin", "SendspinBackend"),
    ("app.output.router", "OutputRouter"),
]


# The specs whose backend actually adopts something. AirPlay and Direct are in
# SPECS to prove their release is an honest no-op; they have no connection and
# no playing flag to reset, so assertions about letting go do not apply.
ADOPTING_SPECS = [s for s in SPECS if s.adopted_attrs]


def _ids(specs):
    return [s.name for s in specs]


async def _pump(times: int = 6) -> None:
    """Let deferred release tasks run to completion."""
    for _ in range(times):
        await asyncio.sleep(0)


# ── the contract itself ──────────────────────────────────────────────────────


@pytest.mark.parametrize("modpath,clsname", BACKEND_CLASSES)
def test_every_backend_implements_release(modpath, clsname):
    """A backend that does not answer the ownership question is a test
    failure, not a runtime surprise. Required, not optional-with-a-default:
    an inherited no-op would let a future backend silently skip the
    obligation, which is the exact failure this unit exists to prevent."""
    mod = pytest.importorskip(modpath)
    cls = getattr(mod, clsname)
    assert_release_contract(cls, name=clsname)


def test_the_contract_checker_rejects_a_backend_without_release():
    """Proof the enforcement mechanism itself works (the multiroom-contract
    posture): a class missing release must fail, not quietly pass."""
    class _NoRelease:
        async def set_device(self, device_id): ...

    with pytest.raises(AssertionError, match="release"):
        assert_release_contract(_NoRelease, name="NoRelease")


def test_the_contract_checker_rejects_an_async_release():
    """``async def release`` is rejected by construction: an awaitable release
    can await a device round-trip, and #48 measured that costing 25.0s. The
    non-blocking guarantee is carried by the signature, not by review."""
    class _AsyncRelease:
        async def release(self): ...

    with pytest.raises(AssertionError, match="coroutine|async"):
        assert_release_contract(_AsyncRelease, name="AsyncRelease")


def test_the_contract_checker_rejects_a_release_taking_arguments():
    class _Parameterised:
        def release(self, force): ...

    with pytest.raises(AssertionError, match="release"):
        assert_release_contract(_Parameterised, name="Parameterised")


def test_release_signature_ignores_self():
    class _X:
        def release(self): ...

    assert release_signature(_X.release) == ()


@pytest.mark.parametrize("spec", SPECS, ids=_ids(SPECS))
def test_backend_satisfies_the_protocol_including_release(spec):
    """The Protocol is runtime_checkable, so growing it by one method must not
    quietly drop any backend out of ``isinstance``."""
    assert isinstance(spec.build(), AbstractOutputBackend)


@pytest.mark.parametrize("spec", SPECS, ids=_ids(SPECS))
def test_release_with_nothing_adopted_is_a_noop(spec):
    """Release on a backend that never attached must not raise."""
    backend = spec.build()
    assert backend.release() is None
    for attr in spec.adopted_attrs:
        assert getattr(backend, attr) is None


@pytest.mark.parametrize("spec", SPECS, ids=_ids(SPECS))
async def test_after_release_the_backend_reports_no_adopted_connection(spec):
    backend = spec.build()
    spec.adopt(backend)
    backend.release()
    for attr in spec.adopted_attrs:
        assert getattr(backend, attr) is None, (
            f"{spec.name}: {attr} still held after release()")
    await _pump()


@pytest.mark.parametrize("spec", SPECS, ids=_ids(SPECS))
async def test_release_twice_in_a_row_is_safe(spec):
    backend = spec.build()
    spec.adopt(backend)
    backend.release()
    backend.release()
    for attr in spec.adopted_attrs:
        assert getattr(backend, attr) is None
    await _pump()


@pytest.mark.parametrize("spec", SPECS, ids=_ids(SPECS))
async def test_a_teardown_that_raises_does_not_propagate(spec):
    """Release runs on paths that already have a real error to report; a
    cleanup problem must never mask the caller's error."""
    backend = spec.build()
    fakes = spec.adopt(backend)
    spec.def_break(fakes)
    assert backend.release() is None          # must not raise
    for attr in spec.adopted_attrs:
        assert getattr(backend, attr) is None
    await _pump()                              # deferred failures swallowed too


@pytest.mark.parametrize("spec", SPECS, ids=_ids(SPECS))
async def test_a_failing_release_does_not_mask_the_callers_error(spec):
    """The shape release is actually used in: the caller is already handling a
    real failure and lets go of the connection on the way out. A cleanup
    exception surfacing here would replace the diagnosis with a red herring —
    the same discipline ``_release_cast`` documents for the connect path."""
    backend = spec.build()
    fakes = spec.adopt(backend)
    spec.def_break(fakes)

    with pytest.raises(RuntimeError, match="the real failure"):
        try:
            raise RuntimeError("the real failure")
        except RuntimeError:
            backend.release()
            raise
    await _pump()


@pytest.mark.parametrize("spec", SPECS, ids=_ids(SPECS))
async def test_release_does_not_await_device_teardown(spec):
    """*Covers AC4.* Structural non-blocking assertion, in two parts.

    1. ``release`` is a plain ``def`` — it cannot await, so an unreachable
       device physically cannot park the caller inside it.
    2. Any teardown that IS a coroutine has not been awaited at the moment
       release returns: it was handed to a background task. If a future edit
       switched release to ``async def`` and awaited these, this fails.
    """
    backend = spec.build()
    assert not inspect.iscoroutinefunction(backend.release)
    fakes = spec.adopt(backend)
    pending = spec.deferred(backend, fakes)
    backend.release()
    for mock in pending:
        mock.assert_not_awaited()
    await _pump()
    for mock in pending:
        mock.assert_awaited()


@pytest.mark.parametrize("spec", SPECS, ids=_ids(SPECS))
async def test_release_returns_while_the_device_hangs(spec):
    """The unreachable-device shape: every async teardown blocks forever.
    Release must still return, and the backend must already report no adopted
    connection — the caller does not wait on the dead device."""
    backend = spec.build()
    fakes = spec.adopt(backend)
    gate = asyncio.Event()

    async def _hang(*_a, **_kw):
        await gate.wait()

    hung = spec.deferred(backend, fakes)
    for mock in hung:
        mock.side_effect = _hang

    backend.release()                      # returns with the device still hung
    for attr in spec.adopted_attrs:
        assert getattr(backend, attr) is None

    gate.set()                             # let the background teardown finish
    await _pump()


@pytest.mark.parametrize("spec", SPECS, ids=_ids(SPECS))
async def test_release_outside_a_running_loop_does_not_raise(spec):
    """Release is called from shutdown paths and from sync router code. With
    no loop to schedule the deferred teardown on there is nothing to run it —
    that must degrade to a logged no-op, never an exception or an
    "never awaited" warning."""
    backend = spec.build()
    spec.adopt(backend)

    def _sync_release():
        backend.release()

    await asyncio.get_running_loop().run_in_executor(None, _sync_release)
    for attr in spec.adopted_attrs:
        assert getattr(backend, attr) is None


# ── backend-specific structural assertions ───────────────────────────────────


def test_chromecast_release_uses_socket_client_disconnect_not_the_convenience_method():
    """#48's measured finding, re-pinned on the release path.

    ``Chromecast.disconnect()`` is ``socket_client.disconnect()`` followed by
    ``join(timeout=None)`` — documented "block forever", measured 25.0s on the
    arm64 rig against an unreachable host. Only the stop signal is wanted."""
    from app.output.chromecast import ChromecastBackend
    backend = ChromecastBackend()
    cc = MagicMock()
    backend._cast = cc

    backend.release()

    cc.socket_client.disconnect.assert_called_once_with()
    cc.disconnect.assert_not_called()


def test_chromecast_release_unregisters_its_listeners():
    """A released cast keeps no listeners: U5 makes an orphan inert, this
    makes sure it is not even wired."""
    from app.output.chromecast import (ChromecastBackend, _AdvanceListener,
                                       _VolumeListener)
    backend = ChromecastBackend()
    cc = MagicMock()
    backend._cast = cc
    listener = _AdvanceListener(backend)
    vol_listener = _VolumeListener(backend, backend._cast)
    backend._listener = listener
    backend._vol_listener = vol_listener

    backend.release()

    cc.media_controller.unregister_status_listener.assert_called_once_with(
        listener)
    cc.unregister_status_listener.assert_called_once_with(vol_listener)


async def test_dlna_release_frees_the_session_and_the_notify_server():
    """Everything its attach creates that actually has to be freed, plus the
    DmrDevice handle — freeing one and leaking the others is the failure this
    test names. A missed notify-server stop leaves a bound socket; a missed
    session close leaks an aiohttp connection pool per attach.

    The requester is dropped from the backend but NOT closed, and the mock is
    built with ``spec=[]`` so this test would fail if it ever were again:
    ``AiohttpSessionRequester`` borrows the session and has no ``close()``
    (2026-08-20 review R3). Closing the session is what frees it."""
    from app.output.dlna import DlnaBackend
    backend = DlnaBackend()
    dmr = MagicMock(async_unsubscribe_services=AsyncMock())
    requester = MagicMock(spec=[])
    session = MagicMock(close=AsyncMock())
    notify = MagicMock(async_stop_server=AsyncMock())
    backend._dmr = dmr
    backend._requester = requester
    backend._dlna_session = session
    backend._notify_server = notify

    backend.release()

    assert backend._dmr is None
    assert backend._requester is None
    assert backend._dlna_session is None
    assert backend._notify_server is None

    await _pump()
    notify.async_stop_server.assert_awaited_once()
    session.close.assert_awaited_once()


async def test_dlna_release_cancels_the_poll_loop():
    """A poll task outliving its renderer is the "abandoned listener reports
    an outage against whichever backend is active now" shape."""
    from app.output.dlna import DlnaBackend
    backend = DlnaBackend()
    backend._dlna_session = MagicMock(close=AsyncMock())

    async def _forever():
        await asyncio.Event().wait()

    task = asyncio.get_running_loop().create_task(_forever())
    backend._poll_task = task

    backend.release()
    assert backend._poll_task is None
    await _pump()
    assert task.cancelled() or task.done()


async def test_plexplayer_release_closes_the_client_and_retires_its_tasks():
    from app.output.plexplayer import PlexPlayerBackend, _PlayerSession
    backend = PlexPlayerBackend()
    client = MagicMock(aclose=AsyncMock())

    async def _forever():
        await asyncio.Event().wait()

    loop = asyncio.get_running_loop()
    poll = loop.create_task(_forever())
    watchdog = loop.create_task(_forever())
    sess = _PlayerSession(device_id="plx-1", client=client, poll_task=poll,
                          watchdog_task=watchdog)
    backend._session = sess
    backend._is_playing = True

    backend.release()

    assert backend._session is None
    assert backend.is_playing is False
    # A self-induced teardown: any terminal read that races in must not advance
    assert sess.self_stopped is True
    await _pump()
    client.aclose.assert_awaited_once()
    assert poll.cancelled() or poll.done()
    assert watchdog.cancelled() or watchdog.done()


def test_airplay_release_is_an_honest_noop():
    """AirPlay's ``set_device`` adopts nothing — it writes the selected id and
    the persisted volume. Release must therefore free nothing, and in
    particular must NOT reach into the cliap2 playback session, which
    ``stop()`` owns (stopping is not releasing, and releasing is not
    stopping)."""
    from app.output import airplay
    backend = airplay.AirPlayBackend()
    proc = MagicMock()
    backend._device_id = "airplay-1"
    backend._cliap2_proc = proc

    assert backend.release() is None

    assert backend._device_id == "airplay-1"
    assert backend._cliap2_proc is proc
    proc.terminate.assert_not_called()
    proc.kill.assert_not_called()


def test_release_is_declared_on_the_protocol():
    """The obligation is structural: it lives on the abstraction every backend
    conforms to, so a fifth backend cannot be written without answering it."""
    assert callable(getattr(AbstractOutputBackend, "release", None))


# ── U3: adopt by compare-and-swap ────────────────────────────────────────────
#
# U1 put every attach behind one lock, so the interleaving these tests force is
# supposed to be unreachable. U3 is the belt to that braces: if a caller is ever
# added outside the lock — and the first attempt at idle re-attach (#47) is the
# evidence that new callers to this path do get added — the failure must degrade
# to "released", not "leaked".
#
# The harnesses below drive a REAL ``set_device`` on each adopting backend with
# its device library faked, and hold one attach inside the connect while another
# runs. Each one exposes the same questions so the scenarios can be written once
# and asserted identically on all three:
#
#   start(device)      launch an attach as a task (never awaited inline — the
#                      whole point is two of them in flight at once)
#   hold(device)       make that device's connect block partway through
#   let_go()           release the hold
#   built(device)      the connection object that attach produced, or None if
#                      the connect has not completed
#   adopted(device)    is THAT attach's connection the backend's current one
#   released(device)   was it torn down through the backend's release path
#
# ``built`` is what pins the "cannot be cancelled" scenario: a superseded attach
# must still be shown to have produced a connection, because an implementation
# that returned early instead would abandon exactly the object the executor
# thread / in-flight coroutine goes on to create regardless.


class _CastAttachEnv:
    """Chromecast: the adopted thing is one cast object, and the connect runs
    in an EXECUTOR THREAD — genuinely uninterruptible, which is why the hold
    here is a ``threading.Event`` and not an asyncio one."""

    name = "chromecast"

    def __enter__(self):
        from app.output.chromecast import ChromecastBackend
        self._patches = [
            patch("app.output.chromecast._CAST_AVAILABLE", True),
            patch("app.database.get_setting", AsyncMock(return_value=None)),
            patch("app.database.set_setting", AsyncMock()),
        ]
        for p in self._patches:
            p.start()
        self.backend = ChromecastBackend()
        self._conns: dict[str, Any] = {}
        self._gates: dict[str, threading.Event] = {}

        def _sync_connect(device_id: str) -> Any:
            gate = self._gates.get(device_id)
            if gate is not None:
                # A real thread blocking for real: nothing on the event loop
                # can interrupt this, exactly like a pychromecast connect.
                assert gate.wait(timeout=10), "connect gate never opened"
            cc = MagicMock(name=f"cast::{device_id}")
            self._conns[device_id] = cc
            # U4: the connect returns the address it resolved WITH the
            # connection, so each attach carries its own (see
            # test_output_chromecast.py for the race this closes).
            from app.output.chromecast import _ResolvedAddr
            return cc, _ResolvedAddr(f"name::{device_id}",
                                     f"192.0.2.{len(self._conns)}", 8009)

        self.backend._sync_connect = _sync_connect
        return self

    def __exit__(self, *exc):
        self.let_go()
        for p in reversed(self._patches):
            p.stop()
        return False

    def start(self, device_id: str) -> asyncio.Task:
        return asyncio.get_running_loop().create_task(
            self.backend.set_device(device_id))

    def hold(self, device_id: str) -> None:
        self._gates[device_id] = threading.Event()

    def let_go(self, device_id: str | None = None) -> None:
        gates = (self._gates.values() if device_id is None
                 else [self._gates[device_id]])
        for gate in gates:
            gate.set()

    def built(self, device_id: str) -> Any:
        return self._conns.get(device_id)

    def adopted(self, device_id: str) -> bool:
        conn = self._conns.get(device_id)
        return conn is not None and self.backend._cast is conn

    def released(self, device_id: str) -> bool:
        conn = self._conns.get(device_id)
        return conn is not None and conn.socket_client.disconnect.called


class _DlnaParts:
    session = None
    requester = None
    notify = None
    dmr = None


class _DlnaAttachEnv:
    """DLNA: the adopted thing is four objects, so ``released`` means all four
    were freed — the "frees one and leaks the other three" failure the U2
    contract test already names, here on the superseded-attach path.

    The per-attach parts are bucketed by ``asyncio.current_task()``: the
    factories the backend calls take no device id, and with two attaches in
    flight their creation order interleaves."""

    name = "dlna"

    def __enter__(self):
        from app.output.dlna import DlnaBackend
        self._parts: dict[str, _DlnaParts] = {}
        self._task_device: dict[Any, str] = {}
        self._gates: dict[str, asyncio.Event] = {}

        def bucket() -> _DlnaParts:
            device_id = self._task_device[asyncio.current_task()]
            return self._parts.setdefault(device_id, _DlnaParts())

        def _session_factory():
            s = MagicMock(name="session", close=AsyncMock())
            bucket().session = s
            return s

        def _requester_factory(session=None):
            r = MagicMock(name="requester", close=AsyncMock())
            bucket().requester = r
            return r

        def _upnp_factory(requester):
            f = MagicMock()

            async def _create(location):
                gate = self._gates.get(location.rsplit("/", 1)[-1])
                if gate is not None:
                    await gate.wait()
                return MagicMock(name="upnp_device")

            f.async_create_device = AsyncMock(side_effect=_create)
            return f

        def _notify_factory(requester, source=None):
            n = MagicMock(name="notify", async_start_server=AsyncMock(),
                          async_stop_server=AsyncMock(),
                          event_handler=MagicMock())
            bucket().notify = n
            return n

        def _dmr_factory(upnp_device, event_handler=None):
            d = MagicMock(name="dmr", async_subscribe_services=AsyncMock(),
                          async_unsubscribe_services=AsyncMock())
            bucket().dmr = d
            return d

        aiohttp_mock = MagicMock()
        aiohttp_mock.ClientSession = MagicMock(side_effect=_session_factory)
        self._patches = [
            patch("app.output.dlna._DLNA_AVAILABLE", True),
            patch("app.output.dlna.aiohttp", aiohttp_mock, create=True),
            patch("app.output.dlna.AiohttpSessionRequester",
                  MagicMock(side_effect=_requester_factory), create=True),
            patch("app.output.dlna.UpnpFactory",
                  MagicMock(side_effect=_upnp_factory), create=True),
            patch("app.output.dlna.AiohttpNotifyServer",
                  MagicMock(side_effect=_notify_factory), create=True),
            patch("app.output.dlna.DmrDevice",
                  MagicMock(side_effect=_dmr_factory), create=True),
            patch("app.database.get_setting", AsyncMock(return_value=None)),
            patch("app.database.set_setting", AsyncMock()),
            patch("app.database.get_gapless_verdict",
                  AsyncMock(return_value=None)),
        ]
        for p in self._patches:
            p.start()
        self.backend = DlnaBackend()
        return self

    def __exit__(self, *exc):
        self.let_go()
        for p in reversed(self._patches):
            p.stop()
        return False

    def start(self, device_id: str) -> asyncio.Task:
        self.backend._device_locations[device_id] = (
            f"http://renderer/{device_id}")
        task = asyncio.get_running_loop().create_task(
            self.backend.set_device(device_id))
        self._task_device[task] = device_id
        return task

    def hold(self, device_id: str) -> None:
        self._gates[device_id] = asyncio.Event()

    def let_go(self, device_id: str | None = None) -> None:
        gates = (self._gates.values() if device_id is None
                 else [self._gates[device_id]])
        for gate in gates:
            gate.set()

    def built(self, device_id: str) -> Any:
        parts = self._parts.get(device_id)
        return parts.dmr if parts else None

    def adopted(self, device_id: str) -> bool:
        parts = self._parts.get(device_id)
        if parts is None or parts.dmr is None:
            return False
        b = self.backend
        return (b._dmr is parts.dmr and b._notify_server is parts.notify
                and b._requester is parts.requester
                and b._dlna_session is parts.session)

    def released(self, device_id: str) -> bool:
        parts = self._parts.get(device_id)
        if parts is None or parts.dmr is None:
            return False
        # Not just the handle: a superseded attach that unsubscribed the
        # DmrDevice and left the notify server bound and the aiohttp pool open
        # has not released anything worth the name.
        #
        # The requester is deliberately absent (2026-08-20 review R3).
        # AiohttpSessionRequester borrows the session rather than owning it and
        # has no close() at all — the teardown used to call it anyway and raise
        # AttributeError on every release. Closing the session is what frees it.
        return all([
            parts.dmr.async_unsubscribe_services.called,
            parts.notify is not None and parts.notify.async_stop_server.called,
            parts.session is not None and parts.session.close.called,
        ])


class _PlexPlayerAttachEnv:
    """Plex player: the adopted thing is one ``_PlayerSession`` wrapping a
    Companion HTTP client. The hold sits in the persisted-address read, the
    last await before the client is built."""

    name = "plexplayer"

    def __enter__(self):
        from app.output.plexplayer import PlexPlayerBackend
        self._clients: dict[str, Any] = {}
        self._gates: dict[str, asyncio.Event] = {}

        async def _get_setting(key, default=None):
            if key.startswith("output_addr:"):
                gate = self._gates.get(key.split(":", 1)[1])
                if gate is not None:
                    await gate.wait()
                return json.dumps({"host": "192.168.1.30", "port": 32500,
                                   "name": key.split(":", 1)[1]})
            return None

        self._patches = [
            patch("app.database.get_setting",
                  AsyncMock(side_effect=_get_setting)),
            patch("app.database.set_setting", AsyncMock()),
            patch("app.database.get_gapless_verdict",
                  AsyncMock(return_value=None)),
        ]
        for p in self._patches:
            p.start()

        def _client_factory(host, port, device_id):
            c = MagicMock(name=f"client::{device_id}", aclose=AsyncMock())
            self._clients[device_id] = c
            return c

        self.backend = PlexPlayerBackend(client_factory=_client_factory)
        return self

    def __exit__(self, *exc):
        self.let_go()
        for p in reversed(self._patches):
            p.stop()
        return False

    def start(self, device_id: str) -> asyncio.Task:
        return asyncio.get_running_loop().create_task(
            self.backend.set_device(device_id))

    def hold(self, device_id: str) -> None:
        self._gates[device_id] = asyncio.Event()

    def let_go(self, device_id: str | None = None) -> None:
        gates = (self._gates.values() if device_id is None
                 else [self._gates[device_id]])
        for gate in gates:
            gate.set()

    def built(self, device_id: str) -> Any:
        return self._clients.get(device_id)

    def adopted(self, device_id: str) -> bool:
        client = self._clients.get(device_id)
        sess = self.backend._session
        return (client is not None and sess is not None
                and sess.client is client)

    def released(self, device_id: str) -> bool:
        client = self._clients.get(device_id)
        return client is not None and client.aclose.called


ATTACH_ENVS = [_CastAttachEnv, _DlnaAttachEnv, _PlexPlayerAttachEnv]
ATTACH_IDS = [e.name for e in ATTACH_ENVS]

WINNER = "device-winner"
LOSER = "device-loser"


async def _settle(*tasks: asyncio.Task) -> None:
    """Drain the given attaches and any deferred release they scheduled.

    ``AttachSuperseded`` is expected here, not exceptional: it is how a losing
    attach now reports that it connected and then found it no longer owned the
    backend. Tests that care which outcome happened use ``_settle_superseded``
    or assert on the task themselves."""
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
        for t in tasks:
            exc = t.exception() if not t.cancelled() else None
            if exc is not None and not isinstance(exc, AttachSuperseded):
                raise exc
    await _pump(12)


async def _settle_superseded(task: asyncio.Task) -> None:
    """Drain a task that MUST report itself superseded.

    The assertion is the point (2026-08-20 re-review ADV-8/ADV-14): before the
    typed outcome existed, a losing attach returned normally and every caller
    read that as "attached". Letting these tests accept either outcome would
    give the contract back nothing."""
    with pytest.raises(AttachSuperseded):
        await task
    await _pump(12)


async def _run_race(env) -> None:
    """LOSER attaches first and is held inside its connect; WINNER starts
    after it and runs to completion, so WINNER is the attach entitled to
    adopt. Both are finished when this returns."""
    env.hold(LOSER)
    loser = env.start(LOSER)
    await _pump()                       # let it reach the held connect
    winner = env.start(WINNER)
    await _settle(winner)               # WINNER commits while LOSER is stuck
    env.let_go()
    await _settle_superseded(loser)


# ── the token itself ─────────────────────────────────────────────────────────


def test_begin_attach_issues_a_fresh_token_each_time():
    class _B(AttachGeneration):
        pass

    b = _B()
    assert b._begin_attach() != b._begin_attach()


def test_a_token_is_superseded_only_by_a_later_attach():
    class _B(AttachGeneration):
        pass

    b = _B()
    token = b._begin_attach()
    assert b._attach_superseded(token) is False   # nothing else has started
    later = b._begin_attach()
    assert b._attach_superseded(token) is True    # the earlier one lost
    assert b._attach_superseded(later) is False   # the later one still holds


def test_the_token_is_not_a_device_id_comparison():
    """Why a generation and not ``self._device_id != device_id``: a re-attach
    to the SAME device is still a new attach, and the first one's connection is
    still an orphan. A device-id comparison would call that not-superseded and
    leak it — and it would also be comparing against a field ``set_device``
    writes itself."""
    class _B(AttachGeneration):
        pass

    b = _B()
    token = b._begin_attach()
    b._begin_attach()                              # same device, second attach
    assert b._attach_superseded(token) is True


def test_exactly_the_backends_that_adopt_something_use_the_attach_token():
    """Structural scope pin, in both directions. A new backend that adopts a
    connection but skips the swap fails here rather than leaking in the field;
    equally, AirPlay and Direct adopt NOTHING — their attach produces no
    connection object, so there is nothing to swap and nothing to release, and
    bolting the token on would be inventing state to guard."""
    adopting = {s.name for s in SPECS if s.adopted_attrs}
    tokened = {s.name for s in SPECS
               if isinstance(s.build(), AttachGeneration)}
    assert tokened == adopting == {"chromecast", "dlna", "plexplayer"}


# ── the scenarios ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("spec", ADOPTING_SPECS, ids=_ids(ADOPTING_SPECS))
async def test_release_clears_the_playing_flag(spec):
    """A released backend must not still report itself playing.

    Found by mutation, not by reading (2026-08-20 review): deleting
    ``self._is_playing = False`` from Chromecast's release left all 279 tests
    in its own file green, and the same deletion in DLNA's left all 233 in
    its. The generic loop above only checks ``adopted_attrs``, and only
    plexplayer happened to have a hand-written assertion for it.

    It is load-bearing: ``OutputRouter.is_playing`` reads straight off the
    active backend's flag, and switch paths branch on it — ``set_backend``
    chooses immediate-vs-deferred on exactly this value. A released backend
    that still claims to be playing is the stale derived state the rest of
    this suite exists to prevent, in the one place nothing was watching."""
    backend = spec.build()
    spec.adopt(backend)
    assert backend.is_playing is True, "the fixture must start out playing"

    backend.release()
    await _pump()

    assert backend.is_playing is False, (
        "release left the backend reporting playback on a connection it no "
        "longer holds")


@pytest.mark.parametrize("env_cls", ATTACH_ENVS, ids=ATTACH_IDS)
async def test_a_superseded_attach_releases_and_does_not_adopt(env_cls):
    """*Covers AC1.* The orphan case, forced. An attach whose target changed
    while it ran must release what it built instead of storing it — the
    difference between a freed socket and one held against a device with a
    small concurrent-connection budget for the life of the process."""
    with env_cls() as env:
        await _run_race(env)

        assert env.built(LOSER) is not None, "the loser did build a connection"
        assert not env.adopted(LOSER), "a superseded attach must not adopt"
        assert env.released(LOSER), (
            "a superseded attach must release what it built")
        # Nor may it claim the backend's device selection on the way out: the
        # id and the connection are adopted together or not at all, otherwise
        # the backend reports playing on a device it never attached to.
        assert env.backend._device_id == WINNER


@pytest.mark.parametrize("env_cls", ATTACH_ENVS, ids=ATTACH_IDS)
async def test_the_winning_attach_is_unaffected_and_remains_adopted(env_cls):
    """The loser's cleanup must not reach into the winner's connection: the
    swap releases what THIS attach built, never ``self.release()``, which frees
    whatever is current and would leave the backend with nothing."""
    with env_cls() as env:
        await _run_race(env)

        assert env.adopted(WINNER), "the winner must still be the adopted one"
        assert not env.released(WINNER), (
            "the winner's connection must not have been torn down")


@pytest.mark.parametrize("env_cls", ATTACH_ENVS, ids=ATTACH_IDS)
async def test_an_uncontended_attach_releases_nothing(env_cls):
    """The scoping pin. Without it, an implementation that ALWAYS released
    would pass every other test in this section while leaving the user with no
    working output at all."""
    with env_cls() as env:
        await _settle(env.start(WINNER))

        assert env.adopted(WINNER)
        assert not env.released(WINNER), (
            "an attach that was never superseded must not be released")


@pytest.mark.parametrize("env_cls", ATTACH_ENVS, ids=ATTACH_IDS)
async def test_release_supersedes_an_attach_already_in_flight(env_cls):
    """*Covers R1/R3, 2026-08-20 review F1.* Ownership ends by RELEASE too, not
    only by a later attach — and until this test, only the second was wired.

    A later attach on the same backend bumps the generation, so the earlier one
    stands down. Nothing bumped it when the ROUTER retired the backend: the
    competing caller was a DIFFERENT backend object, or no attach at all. So a
    connect still parked in flight sailed through the compare-and-swap and
    stored itself onto a backend that had already been released. The
    switch-away edge was spent by then and shutdown was the only thing left
    that would ever free it — an orphan re-dialling for the life of the
    process.

    Reachable from the plan's own Problem Frame: a boot reconnect parked in its
    connect while the admin applies a different output.

    And worse than a plain leak, because the orphan genuinely IS ``self._cast``
    / ``self._dmr`` / ``self._session``: the staleness guards that exist to
    make an abandoned connection inert read it as CURRENT, so its listeners
    keep writing volume and state for a device the user walked away from."""
    with env_cls() as env:
        env.hold(WINNER)
        task = env.start(WINNER)
        await _pump()                 # parked inside the connect
        env.backend.release()         # …and the router retires it right now
        env.let_go(WINNER)
        await _settle_superseded(task)

        assert env.built(WINNER) is not None, (
            "the attach must still have built its connection — it cannot be "
            "interrupted, which is the whole reason this race exists")
        assert not env.adopted(WINNER), (
            "an attach that finished after its backend was released must not "
            "adopt: nothing would ever free what it stored")
        assert env.released(WINNER), (
            "...and it must release what it built on the way out")


@pytest.mark.parametrize("env_cls", ATTACH_ENVS, ids=ATTACH_IDS)
async def test_a_superseded_attach_completes_its_connect_before_releasing(
        env_cls):
    """The call the attach is parked in cannot be cancelled — Chromecast's is a
    blocking connect on an executor thread, and the others are in-flight
    coroutines nobody is going to interrupt. So the check must sit AFTER it:
    bailing out early would abandon the connection the call goes on to produce
    regardless, which is the leak this unit exists to prevent.

    Pinned by asserting the loser's connection was fully built AND released,
    not merely that the loser failed to adopt."""
    with env_cls() as env:
        env.hold(LOSER)
        loser = env.start(LOSER)
        await _pump()
        winner = env.start(WINNER)
        await _settle(winner)

        assert env.built(LOSER) is None, (
            "precondition: the loser is still inside its connect")
        assert not loser.done()

        env.let_go()
        await _settle_superseded(loser)

        assert env.built(LOSER) is not None, (
            "the uninterruptible connect ran to completion anyway")
        assert env.released(LOSER)
        assert env.adopted(WINNER)


@pytest.mark.parametrize("env_cls", ATTACH_ENVS, ids=ATTACH_IDS)
async def test_an_attach_is_superseded_by_one_that_has_merely_started(env_cls):
    """Both attaches in flight at once, neither committed. The loser must still
    stand down, because the user's last instruction was the winner's device.

    This is what a token buys over "is anything else adopted yet?": at the
    moment the loser checks, the backend has adopted NOTHING, so a
    compare-against-current-state check would happily adopt the wrong device
    and the winner would be the one left orphaned."""
    with env_cls() as env:
        env.hold(LOSER)
        env.hold(WINNER)
        loser = env.start(LOSER)
        await _pump()
        winner = env.start(WINNER)      # starts, does not finish
        await _pump()
        assert not winner.done() and not loser.done()

        env.let_go(LOSER)               # the loser lands first, uncommitted
        await _settle_superseded(loser)
        assert not env.adopted(LOSER)
        assert env.released(LOSER)

        env.let_go(WINNER)
        await _settle(winner)
        assert env.adopted(WINNER)
        assert not env.released(WINNER)


@pytest.mark.parametrize("env_cls", ATTACH_ENVS, ids=ATTACH_IDS)
async def test_a_race_leaves_exactly_one_adopted_connection(env_cls):
    """*Covers AC1* as the requirement states it: of the connections two
    concurrent attaches produce, exactly one is adopted and every other is
    released. No third outcome, no orphans."""
    with env_cls() as env:
        await _run_race(env)

        outcomes = [(env.adopted(d), env.released(d)) for d in (LOSER, WINNER)]
        assert sum(1 for adopted, _ in outcomes if adopted) == 1
        for adopted, released in outcomes:
            assert adopted != released, (
                "every connection must be adopted or released — never both, "
                "and never neither")


# ── U4: what an attach persists comes from that attach ───────────────────────
#
# Chromecast resolved an address on an executor thread, parked it in
# ``self._resolved_{host,port,name}``, and read those fields back AFTER the
# await to persist ``output_addr:{device_id}``. One set of fields, two attaches:
# the second overwrote them before the first resumed, so device X's stored
# address became device Y's — and because that value is loaded into the address
# cache on the next boot and consulted BEFORE discovery, it shadowed the right
# entry and selecting X kept playing on Y across restarts (issue #49, AC2).
#
# The behavioural fix and its race are pinned in tests/test_output_chromecast.py
# because only Chromecast had the pattern. THIS test is the reason it cannot
# come back somewhere else: it reads every output module and fails if the
# address a backend persists is traceable to shared instance state rather than
# to the attach's own scope.
#
# Deliberately scoped to the DEVICE ADDRESS, not to every persisted value.
# ``vol:{backend}:{self._device_id}`` reads instance state on purpose — the
# volume belongs to the backend's current device, whatever that now is. The
# address is different: it is written *against a device id* and is the value a
# later boot dials. Widening this to all of ``set_setting`` would fail those
# and say nothing true.

_ADDRESS_KEY_PREFIX = "output_addr:"


def _assignment_map(func: ast.AST) -> dict[str, list[ast.AST]]:
    """Local name → the expressions assigned to it inside *func*. Single
    targets and tuple-unpacking both, so ``cast, resolved = await …`` resolves
    ``resolved`` back to the connect that produced it."""
    out: dict[str, list[ast.AST]] = {}
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value:
            targets, value = [node.target], node.value
        else:
            continue
        for target in targets:
            names = ([e for e in ast.walk(target) if isinstance(e, ast.Name)]
                     if isinstance(target, (ast.Tuple, ast.List))
                     else [target] if isinstance(target, ast.Name) else [])
            for name in names:
                out.setdefault(name.id, []).append(value)
    return out


def _is_self_attr(node: ast.AST) -> bool:
    return (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "self")


def _keyed_by(node: ast.AST, key_names: set[str]) -> set[ast.AST]:
    """The ``self.<map>`` reads in *node* that are looked up BY the device id
    the setting is keyed under — ``self._device_addr[device_id]`` and
    ``self._device_locations.get(device_id)``.

    Those are not the defect. A per-device map returns this device's entry
    however many attaches are in flight; the pattern that broke was a single
    "last resolved" slot, which returns whoever wrote it last."""
    exempt: set[ast.AST] = set()
    for sub in ast.walk(node):
        target, keys = None, []
        if isinstance(sub, ast.Subscript) and _is_self_attr(sub.value):
            target, keys = sub.value, [sub.slice]
        elif (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
              and sub.func.attr in {"get", "setdefault", "pop"}
              and _is_self_attr(sub.func.value)):
            target, keys = sub.func.value, list(sub.args)
        if target is not None and any(
                isinstance(n, ast.Name) and n.id in key_names
                for k in keys for n in ast.walk(k)):
            exempt.add(target)
    return exempt


def _self_reads(roots: list[ast.AST], assigns: dict[str, list[ast.AST]],
                methods: set[str], key_names: set[str]) -> set[str]:
    """The ``self.<attr>`` DATA reads reachable from *roots*, following local
    aliases so a value laundered through a variable is still caught.

    Two things are not shared-state reads. An attribute naming a method of the
    class — the connect this unit introduced is handed to ``run_in_executor``
    as ``self._sync_connect``, a call and not a field — and a map indexed by
    the device id the value is being stored against (see ``_keyed_by``)."""
    seen_names: set[str] = set()
    found: set[str] = set()
    stack = list(roots)
    while stack:
        node = stack.pop()
        called = {n.func for n in ast.walk(node)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        exempt = _keyed_by(node, key_names)
        for sub in ast.walk(node):
            if (_is_self_attr(sub) and sub not in called and sub not in exempt
                    and sub.attr not in methods):
                found.add(sub.attr)
            elif isinstance(sub, ast.Name) and sub.id in assigns:
                if sub.id not in seen_names:
                    seen_names.add(sub.id)
                    stack.extend(assigns[sub.id])
    return found


@dataclass
class _PersistSite:
    filename: str
    call: ast.Call
    func: ast.AST
    cls: ast.AST | None
    key_names: set[str]


def _address_persist_sites() -> list[_PersistSite]:
    """Every ``set_setting("output_addr:…", …)`` call under app/output, with
    the function and class it sits in."""
    root = Path(__file__).resolve().parents[1] / "app" / "output"
    sites = []
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node

        def enclosing(node, types):
            cur = parents.get(node)
            while cur is not None and not isinstance(cur, types):
                cur = parents.get(cur)
            return cur

        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "set_setting"
                    and node.args):
                continue
            func = enclosing(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            if func is None:
                continue
            # The key may be a literal, an f-string, or a local holding either.
            key_roots = [node.args[0]] + [
                v for name in (n.id for n in ast.walk(node.args[0])
                               if isinstance(n, ast.Name))
                for v in _assignment_map(func).get(name, [])]
            literals = [c.value for r in key_roots for c in ast.walk(r)
                        if isinstance(c, ast.Constant) and isinstance(c.value, str)]
            if not any(lit.startswith(_ADDRESS_KEY_PREFIX) for lit in literals):
                continue
            # The names the setting is keyed under — the device identity a
            # per-device lookup is allowed to be indexed by.
            key_names = {n.id for r in key_roots for n in ast.walk(r)
                         if isinstance(n, ast.Name)}
            sites.append(_PersistSite(path.name, node, func,
                                      enclosing(node, (ast.ClassDef,)),
                                      key_names))
    return sites


def test_no_backend_persists_a_device_address_read_off_the_instance():
    """*Covers AC2* structurally, for every backend rather than the one where
    it was diagnosed.

    The address written against a device id must be traceable to the attach
    that resolved it — a local, or a lookup keyed by that same device id — and
    never to a single shared "last resolved" slot on the backend, which a
    concurrent attach can overwrite while this one is parked on an await."""
    offenders = []
    for site in _address_persist_sites():
        methods = {n.name for n in ast.walk(site.cls)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))} \
            if site.cls is not None else set()
        roots = ([*site.call.args]
                 + [kw.value for kw in site.call.keywords])
        reads = _self_reads(roots, _assignment_map(site.func), methods,
                            site.key_names)
        if reads:
            offenders.append(
                f"{site.filename}:{site.call.lineno} in {site.func.name}() "
                f"persists the device address from shared instance state: "
                f"{', '.join('self.' + a for a in sorted(reads))}")
    assert not offenders, (
        "an output backend persists output_addr:{device_id} from a value read "
        "off the instance across an await — under two concurrent attaches that "
        "stores one device's address against another's id, and the next boot "
        "dials it before discovery (2026-08-20 plan U4 / issue #49 AC2):\n  "
        + "\n  ".join(offenders))


def test_the_address_persist_guard_actually_sees_every_backend_that_persists():
    """The guard above is only worth its runtime if it is looking at something.
    A rename or a move that took the persist out of its view would otherwise
    turn it green and silent."""
    modules = {site.filename for site in _address_persist_sites()}
    assert {"chromecast.py", "dlna.py", "plexplayer.py",
            "airplay.py"} <= modules, (
        f"the output_addr persist sites moved — guard now sees only {modules}")


def test_the_address_persist_guard_catches_the_pattern_it_bans():
    """Proof the enforcement mechanism works, in the shape the defect actually
    had: resolve on a thread into an instance field, read it back after the
    await, persist that. Same posture as the release-contract checker's own
    negative tests above."""
    source = '''
class Backend:
    async def set_device(self, device_id):
        await self._connect(device_id)          # writes self._resolved_host
        blob = json.dumps({"host": self._resolved_host})
        await database.set_setting(f"output_addr:{device_id}", blob)
'''
    tree = ast.parse(source)
    cls = tree.body[0]
    func = cls.body[0]
    call = [n for n in ast.walk(func)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "set_setting"][0]
    methods = {n.name for n in ast.walk(cls)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    reads = _self_reads(list(call.args), _assignment_map(func), methods,
                        {"device_id"})
    assert reads == {"_resolved_host"}, (
        "the guard must follow a value laundered through a local, and must "
        "not count self._connect (a method call) as shared state")


# ── U6: release on switch-away and on shutdown ───────────────────────────────
#
# U2 gave every backend a release; U3-U5 made a non-adopted connection
# harmless. None of it fires on its own. There are exactly two moments at which
# ownership of a device genuinely ends, and this section pins both — plus, just
# as deliberately, the moment that LOOKS like one and is not.
#
#   switch away  → app/output/router.py::_stop_and_warn, the single retire path
#                  both switch flows (immediate set_backend, deferred
#                  swap_pending) already share.
#   shutdown     → app/main.py's lifespan, after the yield.
#   a plain stop → NOT a release. Stopping playback is not relinquishing the
#                  device: the connection survives so a resume, a skip or the
#                  next track costs nothing. Conflating the two would force a
#                  reconnect on every pause, and it is the scoping decision
#                  most likely to be silently broken by a later edit — so it is
#                  pinned behaviourally at the router, behaviourally on a real
#                  backend, and structurally across every implementation.


class _RetireProbe:
    """A backend that records the retire steps in the order the router runs
    them. Deliberately not a MagicMock: the ORDER of stop/release/holder-clear
    is the thing under test, and a mock's call list would not span the three
    different objects those calls land on."""

    def __init__(self, *, is_playing: bool = False, release_error=None,
                 stop_error=None, teardown_warning=None) -> None:
        self.calls: list[str] = []
        self.holder_keys: list = []
        self._is_playing = is_playing
        self._release_error = release_error
        self._stop_error = stop_error
        self.last_teardown_warning = teardown_warning

    @property
    def is_playing(self) -> bool:
        return self._is_playing

    async def stop(self) -> None:
        self.calls.append("stop")
        if self._stop_error is not None:
            raise self._stop_error

    def release(self) -> None:
        self.calls.append("release")
        if self._release_error is not None:
            raise self._release_error

    def set_dispatch_holder(self, key) -> None:
        self.calls.append("holder")
        self.holder_keys.append(key)


def _router_with(active, pending=None):
    """An OutputRouter pointed at *active* without going through set_backend
    (which fires the arming hook and schedules the retire on a task)."""
    from app.output.router import OutputRouter
    router = OutputRouter()
    router._active = active
    router._pending = pending
    return router


async def test_deferred_switch_releases_the_outgoing_backend():
    """*Covers AC3.* The switch that happens at the next track boundary:
    swap_pending retires the outgoing backend, and retiring now includes
    letting go of what its attach adopted."""
    old, new = _RetireProbe(is_playing=True), _RetireProbe()
    router = _router_with(old, pending=new)

    await router.swap_pending()

    assert router.active is new
    assert "release" in old.calls
    assert "release" not in new.calls, "the INCOMING backend must not be released"


async def test_immediate_switch_releases_the_outgoing_backend():
    """*Covers AC3.* The other switch flow — nothing playing, so set_backend
    swaps at once and retires the old backend on a task. Both flows share one
    retire path precisely so a fix like this cannot land on only one of them."""
    old, new = _RetireProbe(is_playing=False), _RetireProbe()
    router = _router_with(old)
    with patch("app.state.trigger_arming_eval", MagicMock()):
        router.set_backend(new)
    await _pump(10)

    assert router.active is new
    assert "release" in old.calls


async def test_the_retire_path_stops_before_it_releases():
    """Order is load-bearing and runs stop → release → holder-clear.

    Release drops the very connection ``stop()`` speaks over: a released Cast
    has no socket for the stop command, a released plexplayer no Companion
    client, a released renderer no DmrDevice. Releasing first would silently
    turn every switch-away stop into a no-op and leave the outgoing device
    playing."""
    old = _RetireProbe(is_playing=True)
    router = _router_with(old, pending=_RetireProbe())

    await router.swap_pending()

    assert old.calls == ["stop", "release", "holder"]
    assert old.holder_keys == [None]


async def test_release_happens_before_the_teardown_notice_await():
    """The notice is an awaited broadcast, and every await is a window in
    which the abandoned connection's listeners are still live and still
    consider themselves current. Release closes that window at the first
    moment nothing needs the connection any more."""
    order: list[str] = []
    old = _RetireProbe(is_playing=True,
                       teardown_warning="player may still be playing")
    old_release = old.release

    def _record_release():
        order.append("release")
        old_release()

    old.release = _record_release

    async def _notice(*_a, **_kw):
        order.append("notice")

    router = _router_with(old, pending=_RetireProbe())
    with patch("app.events.bus.manager.broadcast_to_admins",
               AsyncMock(side_effect=_notice)):
        await router.swap_pending()

    assert order == ["release", "notice"]


async def test_reselecting_the_same_backend_does_not_release():
    """A device change WITHIN one backend is not a switch away: the retire
    path is never entered, and the backend's own ``set_device`` owns the
    teardown of the connection it is replacing. Releasing here would drop a
    connection nobody asked us to drop — and, on a same-device re-select, one
    the incoming attach is about to rebuild."""
    backend = _RetireProbe(is_playing=False)
    router = _router_with(backend)
    with patch("app.state.trigger_arming_eval", MagicMock()):
        router.set_backend(backend)
    await _pump(10)

    assert backend.calls == []


async def test_a_deferred_swap_to_the_same_backend_stops_but_does_not_release():
    """The same rule on the DEFERRED path, where it is much easier to miss.

    Changing Cast speaker A to speaker B *while music plays* re-selects the
    same backend instance, so ``set_backend`` parks it as pending and
    ``swap_pending`` later retires an "old" backend that IS the incoming one.
    The stop there is harmless — it lands immediately before the boundary's
    play — but a release would drop the connection that play needs, turning a
    routine speaker change into a device error on the next track."""
    backend = _RetireProbe(is_playing=True)
    router = _router_with(backend, pending=backend)

    await router.swap_pending()

    assert router.active is backend
    assert "release" not in backend.calls
    assert backend.calls == ["stop", "holder"]   # pre-U6 behaviour, unchanged


async def test_switching_back_before_the_retire_runs_does_not_release_it():
    """The immediate branch retires on a task, so the router's active backend
    can change before that task runs — two quick Applies is all it takes. The
    guard is read at release time, not at schedule time, so the backend the
    user has landed on keeps its connection while the genuinely-abandoned one
    is still let go."""
    a, b = _RetireProbe(is_playing=False), _RetireProbe(is_playing=False)
    router = _router_with(a)
    with patch("app.state.trigger_arming_eval", MagicMock()):
        router.set_backend(b)                    # retire A, on a task
        router.set_backend(a)                    # …and straight back to A
    await _pump(10)

    assert router.active is a
    assert "release" not in a.calls, "released the backend now active"
    assert "release" in b.calls, "the abandoned backend was not released"


async def test_release_failure_during_switch_away_does_not_block_or_fail_it():
    """Release is contractually never-raising, but the retire path is the
    caller that must not care if a backend ever breaks that promise: the
    switch is a user action already in flight, and a cleanup fault cannot be
    allowed to strand it half-done. Everything after the release still runs."""
    old = _RetireProbe(is_playing=True,
                       release_error=RuntimeError("teardown exploded"),
                       teardown_warning="player may still be playing")
    new = _RetireProbe()
    router = _router_with(old, pending=new)
    notice = AsyncMock()

    with patch("app.events.bus.manager.broadcast_to_admins", notice):
        await router.swap_pending()          # must not raise

    assert router.active is new
    assert old.holder_keys == [None]         # holder still cleared
    notice.assert_awaited_once()             # notice still delivered


async def test_a_stop_failure_still_reaches_the_release():
    """The device that is hardest to stop is exactly the one whose connection
    most needs freeing. A failed stop is logged and the retire continues."""
    old = _RetireProbe(is_playing=True, stop_error=OSError("device unreachable"))
    router = _router_with(old, pending=_RetireProbe())

    await router.swap_pending()

    assert old.calls == ["stop", "release", "holder"]


async def test_a_backend_without_release_still_completes_the_switch():
    """The Protocol requires release and the contract test above enforces it,
    so this cannot happen — but if it ever did, the failure mode must be a
    logged warning, not a user stranded mid-switch with no output."""
    from types import SimpleNamespace
    old = SimpleNamespace(is_playing=True, stop=AsyncMock())
    new = _RetireProbe()
    router = _router_with(old, pending=new)

    await router.swap_pending()              # must not raise

    assert router.active is new
    old.stop.assert_awaited_once()


@pytest.mark.parametrize("spec", SPECS, ids=_ids(SPECS))
async def test_switch_away_frees_what_each_backend_actually_adopted(spec):
    """*Covers AC3*, R6: the same switch, driven against every real backend
    rather than a probe. What each one adopts differs; that all of them are
    empty-handed after the switch does not."""
    backend = spec.build()
    spec.adopt(backend)
    router = _router_with(backend, pending=_RetireProbe())

    await router.swap_pending()

    for attr in spec.adopted_attrs:
        assert getattr(backend, attr) is None, (
            f"{spec.name}: {attr} still adopted after a switch away")
    await _pump()


# ── the scoping pin: stopping is not releasing ───────────────────────────────


async def test_a_plain_stop_does_not_release():
    """THE scoping decision. ``router.stop()`` is queue-end, admin Stop, the
    first half of a skip — playback ending, not the device being handed back.
    A release here would cost a full reconnect on the next play (and #48
    measured on the rig what a Cast reconnect can cost)."""
    backend = _RetireProbe()
    router = _router_with(backend)

    await router.stop()

    assert backend.calls == ["stop"]


async def test_pause_and_resume_do_not_release():
    """The pause case stated in the plan: a paused backend keeps its device,
    so resume is instant."""
    backend = MagicMock(is_playing=True)
    backend.pause = AsyncMock()
    backend.resume = AsyncMock()
    router = _router_with(backend)

    await router.pause()
    await router.resume()

    backend.release.assert_not_called()


async def test_a_plain_stop_leaves_a_real_backends_connection_adopted():
    """Behavioural, on the backend where a reconnect is most expensive: after
    ``stop()`` the Cast connection is still there to play the next track
    over."""
    from app.output.chromecast import ChromecastBackend
    backend = ChromecastBackend()
    cast = MagicMock()
    backend._cast = cast
    backend._device_id = "cast-1"
    backend._is_playing = True

    await backend.stop()

    assert backend._cast is cast, "stop() released the connection"
    assert backend.is_playing is False
    cast.socket_client.disconnect.assert_not_called()


def _release_calls_in(node: ast.AST) -> bool:
    """Does *node* contain a ``self.release()`` call?"""
    return any(isinstance(n, ast.Call) and _is_self_attr(n.func)
               and n.func.attr == "release" for n in ast.walk(node))


def test_no_backend_releases_from_a_playback_method():
    """The structural half of the scoping pin, across every implementation at
    once — the behavioural tests above cover the router and one backend, and
    this covers the rest, which a later edit could quietly change.

    ``set_device`` is deliberately absent from the banned list: an attach
    releasing the connection it is replacing is the contract working."""
    banned = {"stop", "pause", "resume", "seek", "set_volume", "get_volume",
              "play"}
    root = Path(__file__).resolve().parents[1] / "app" / "output"
    offenders = []
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            methods = [n for n in cls.body
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            if not any(m.name == "release" for m in methods):
                continue
            for method in methods:
                if method.name in banned and _release_calls_in(method):
                    offenders.append(
                        f"{path.name}:{method.lineno} {cls.name}.{method.name}()")
    assert not offenders, (
        "stopping is not releasing (2026-08-20 plan U6): these playback "
        "methods let go of the adopted connection, which forces a reconnect "
        "on the next play and on every resume:\n  " + "\n  ".join(offenders))


def _output_sources() -> list[Path]:
    root = Path(__file__).resolve().parents[1] / "app" / "output"
    return sorted(root.rglob("*.py"))     # rglob: a backend may live in a subpackage


def _class_methods(cls: ast.ClassDef) -> dict[str, ast.AST]:
    return {m.name: m for m in cls.body
            if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _discovered_backend_classes() -> set[str]:
    """Every concrete output backend in the tree, found STRUCTURALLY.

    Discovery used to key off the class NAME ending in "Backend", which simply
    traded "remember to update a list" for "remember to name your class right"
    — the same silent-omission failure mode, as a review demonstrated with a
    working ``SonosOutput`` that both guards ignored. A backend is now anything
    that answers ``set_device`` and at least one of ``play``/``release``:
    that is what the router requires of it, so a class meeting it is one
    whether or not its name says so.

    Bases and the Protocol are excluded — they define the obligation rather
    than carrying it, and their concrete subclasses are discovered on their
    own."""
    found = set()
    for path in _output_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            if cls.name == "AbstractOutputBackend" or cls.name.endswith("Base"):
                continue
            methods = _class_methods(cls)
            if "set_device" in methods and (
                    "play" in methods or "release" in methods):
                found.add(cls.name)
    return found


def test_the_backend_discovery_is_not_name_based():
    """Negative control for the guard below. A detector that can only find
    classes already named the expected way proves nothing about the one
    somebody names differently — which is the case that actually escapes."""
    tree = ast.parse(
        "class SonosOutput:\n"
        "    async def set_device(self, d): ...\n"
        "    async def play(self, u, m): ...\n"
        "    async def release(self): ...\n")
    cls = tree.body[0]
    methods = _class_methods(cls)
    assert "set_device" in methods and "play" in methods, (
        "the structural rule no longer recognises a plainly-shaped backend")


def test_the_backend_list_is_not_missing_anyone():
    """*Covers R6, 2026-08-20 review.* ``BACKEND_CLASSES`` is the input to the
    cross-backend contract tests, and it was hand-maintained — so the one
    failure the release contract exists to prevent, a NEW backend inheriting
    the obligation by omission and silently skipping it, would sail through
    every test in this file. The list cannot police itself; the source can.

    This file already AST-guards the shutdown list for exactly this drift. The
    inconsistency was that the list feeding every other guard had no guard of
    its own."""
    listed = {name for _mod, name in BACKEND_CLASSES}
    missing = _discovered_backend_classes() - listed
    assert not missing, (
        "these output backends exist in app/output/ but are not in "
        f"BACKEND_CLASSES, so nothing checks their release contract: {missing}")


# Call shapes that block the calling thread. Not exhaustive by construction —
# no static check can be — but each one is a real way a plain ``def release``
# has blocked in this codebase's history or its dependencies'.
_BLOCKING_CALLS = {
    "time.sleep", "subprocess.run", "subprocess.call", "subprocess.check_output",
    "socket.create_connection", "loop.run_until_complete",
    "asyncio.run", "future.result", "requests.get", "requests.post",
}


def _local_calls_in(node: ast.AST) -> set[str]:
    """Bare-name calls made from *node* — candidates for module-local helpers."""
    return {sub.func.id for sub in ast.walk(node)
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)}


def _is_thread_join(call: ast.Call) -> bool:
    """``.join`` means a thread join here — but ``", ".join(parts)`` and
    ``os.path.join(...)`` are not that, and a guard that fails the build on a
    log-message edit with the diagnosis "release() must not block its caller"
    teaches maintainers to delete it (2026-08-20 review T6)."""
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr != "join":
        return False
    if isinstance(func.value, ast.Constant):          # ", ".join(...)
        return False
    return ast.unparse(func.value) not in {"os.path", "Path", "posixpath"}


def _blocking_calls_in(node: ast.AST) -> list[str]:
    hits = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        name = ast.unparse(sub.func)
        if name in _BLOCKING_CALLS or _is_thread_join(sub):
            hits.append(name)
    return hits


def test_no_release_body_makes_a_blocking_call():
    """*Covers R4, 2026-08-20 review.* ``assert_release_contract`` proves the
    SHAPE of release — a plain ``def``, no arguments — and the review was right
    that shape is not the property that matters. A synchronous ``release`` that
    calls ``time.sleep`` or joins a thread passes that checker cleanly while
    blocking its caller, which is exactly the class of bug #48 was: the fix
    there was not "make it sync", it was "stop calling the convenience method
    that joins the socket thread".

    So this is the behavioural half, as far as static reading can carry it.
    ``.join(`` is included on purpose and by suffix: ``Chromecast.disconnect``
    was measured blocking for 25 seconds on the rig precisely because it joins,
    and pychromecast is not the only library that offers one."""
    offenders = []
    for path in _output_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # Module-level helpers, so the guard can follow one hop. #48's actual
        # blocking call lived in chromecast's _release_cast, not in the
        # release() body — a guard that stops at the method boundary cannot
        # see the very bug it cites (2026-08-20 review T7).
        helpers = {n.name: n for n in tree.body
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            for m in cls.body:
                # AsyncFunctionDef too: an `async def release` is already
                # banned by the contract checker, but only for classes that
                # are IN the backend list — precisely the set an undiscovered
                # backend is missing from (review T5).
                if not isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if m.name != "release":
                    continue
                scan = [m] + [helpers[n] for n in _local_calls_in(m)
                              if n in helpers]
                for node in scan:
                    where = ("" if node is m
                             else f" (via {node.name}())")
                    for call in _blocking_calls_in(node):
                        offenders.append(
                            f"{path.name}:{node.lineno} "
                            f"{cls.name}.release(){where} -> {call}")
    assert not offenders, (
        "release() must not block its caller — it runs on switch-away and on "
        "shutdown, and neither can wait:\n  " + "\n  ".join(offenders))


def test_the_blocking_release_guard_catches_the_pattern_it_bans():
    """Positive control. A detector nobody has watched fail is not evidence —
    and this file already had one guard whose only proof exercised an inner
    helper rather than the shape it actually bans."""
    tree = ast.parse(
        "class B:\n"
        "    def release(self):\n"
        "        self._cast.socket_client.join(timeout=None)\n"
        "        time.sleep(1)\n")
    hits = _blocking_calls_in(tree.body[0].body[0])
    assert "time.sleep" in hits
    assert any(h.endswith(".join") for h in hits)


def test_the_playback_release_guard_sees_the_classes_it_is_meant_to():
    """The guard is only worth its runtime if it is looking at something: a
    release that moved or a class that was renamed must not turn it silent."""
    root = Path(__file__).resolve().parents[1] / "app" / "output"
    seen = set()
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            if any(isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and m.name == "release" for m in cls.body):
                seen.add(cls.name)
    assert {"ChromecastBackend", "DlnaBackend", "PlexPlayerBackend",
            "AirPlayBackend", "DirectAudioBackend", "OutputRouter"} <= seen, (
        f"the release implementations moved — guard now sees only {seen}")


def test_the_playback_release_guard_catches_the_pattern_it_bans():
    """Proof the guard above is looking for the right shape."""
    tree = ast.parse(
        "class B:\n"
        "    def release(self): ...\n"
        "    async def stop(self):\n"
        "        await self._device.stop()\n"
        "        self.release()\n")
    stop = tree.body[0].body[1]
    assert _release_calls_in(stop)


# ── shutdown: the second edge ────────────────────────────────────────────────


def _shutdown_env(monkeypatch, *eager, server_fed=()):
    """Point app.state's constructed-backend globals at *eager* (padded with
    None) and its server-fed map at *server_fed*."""
    import app.state as st
    padded = list(eager) + [None] * (len(st.EAGER_BACKEND_ATTRS) - len(eager))
    for attr, backend in zip(st.EAGER_BACKEND_ATTRS, padded):
        monkeypatch.setattr(st, attr, backend, raising=False)
    monkeypatch.setattr(st, "_server_fed_backends",
                        {f"sf{i}": b for i, b in enumerate(server_fed)})


def test_the_shutdown_list_covers_every_backend_the_router_can_be_given():
    """Drift guard. ``state.all_output_backends`` reads a tuple of global
    names; ``state._get_backend`` routes a backend TYPE to those same globals.
    A sixth eager backend added to the router's map but not to the tuple would
    never be released at shutdown, and nothing else would notice."""
    import app.state as st
    source = Path(st.__file__).read_text(encoding="utf-8")
    func = next(n for n in ast.walk(ast.parse(source))
                if isinstance(n, ast.FunctionDef) and n.name == "_get_backend")
    routed = {n.id for d in ast.walk(func) if isinstance(d, ast.Dict)
              for v in d.values for n in ast.walk(v) if isinstance(n, ast.Name)}
    assert routed == set(st.EAGER_BACKEND_ATTRS), (
        "app.state._get_backend routes to globals that shutdown does not "
        f"release (or vice versa): routed={sorted(routed)} "
        f"released={sorted(st.EAGER_BACKEND_ATTRS)}")


async def test_shutdown_releases_every_backend_that_has_adopted_something(
        monkeypatch):
    """A backend holding a live connection at process exit used to be simply
    abandoned — a Cast socket thread still re-dialling, an aiohttp session and
    a bound GENA callback socket, a Companion client."""
    from app.main import _release_output_backends
    probes = [_RetireProbe() for _ in range(3)]
    _shutdown_env(monkeypatch, *probes[:2], server_fed=(probes[2],))

    await _release_output_backends()

    for probe in probes:
        assert probe.calls == ["release"], "not released at shutdown"


async def test_shutdown_releases_backends_the_user_switched_away_from(
        monkeypatch):
    """EVERY constructed backend, not just ``output_router.active``. The
    switch-away release is best-effort by contract, so the backends most
    likely to still be holding something are exactly the ones the router no
    longer points at."""
    from app.main import _release_output_backends
    import app.state as st
    active, retired = _RetireProbe(), _RetireProbe()
    _shutdown_env(monkeypatch, active, retired)
    monkeypatch.setattr(st, "output_router", _router_with(active))

    await _release_output_backends()

    assert retired.calls == ["release"]
    assert active.calls == ["release"]


async def test_shutdown_survives_a_backend_whose_release_raises(monkeypatch):
    """One broken teardown must not abandon the connections after it in the
    list, and must not take the shutdown down with it."""
    from app.main import _release_output_backends
    boom = _RetireProbe(release_error=RuntimeError("teardown exploded"))
    healthy = _RetireProbe()
    _shutdown_env(monkeypatch, boom, healthy)

    await _release_output_backends()          # must not raise

    assert healthy.calls == ["release"]


async def test_shutdown_before_setup_has_nothing_to_release(monkeypatch):
    """A process that dies before ``state.setup()`` ran has no backends at
    all — the globals are still None. Shutdown must be a no-op, not an
    AttributeError storm in the exit path."""
    from app.main import _release_output_backends
    _shutdown_env(monkeypatch)

    await _release_output_backends()


async def test_shutdown_releases_the_real_backends(monkeypatch):
    """The probes above prove the wiring; this proves it against the actual
    classes, so a backend whose release drifted is caught here and not on the
    rig."""
    from app.main import _release_output_backends
    backends = []
    for spec in SPECS:
        backend = spec.build()
        spec.adopt(backend)
        backends.append(backend)
    _shutdown_env(monkeypatch, *backends[:5], server_fed=backends[5:])

    await _release_output_backends()

    for spec, backend in zip(SPECS, backends):
        for attr in spec.adopted_attrs:
            assert getattr(backend, attr) is None, (
                f"{spec.name}: {attr} still held at shutdown")


async def test_the_lifespan_releases_output_backends_after_the_yield(
        tmp_path, monkeypatch):
    """The wiring itself: shutdown must go through the release, between the
    watcher stopping (discovery quiet, so nothing can hand a backend a fresh
    address mid-release) and the database closing (the release still needs a
    live event loop for its drain)."""
    from app import main as appmain
    order: list[str] = []

    async def _stop_watcher():
        order.append("watcher")

    async def _release():
        order.append("release")

    async def _close_db():
        order.append("db")

    monkeypatch.setattr(appmain.settings, "data_dir", tmp_path)
    with patch.object(appmain, "init_db", AsyncMock()), \
         patch.object(appmain, "close_db", _close_db), \
         patch.object(appmain, "_release_output_backends", _release), \
         patch("app.state.setup", AsyncMock()), \
         patch("app.state.trigger_browse_index_refresh", MagicMock()), \
         patch("app.state.trigger_catalog_refresh", MagicMock()), \
         patch("app.api.guest.warm_enabled_libraries", MagicMock()), \
         patch("app.output.watcher.start_watcher", AsyncMock()), \
         patch("app.output.watcher.stop_watcher", _stop_watcher):
        async with appmain.lifespan(MagicMock()):
            assert order == []               # nothing released while running

    assert order == ["watcher", "release", "db"]


# ── the drain: deferred teardown must not outlive the loop ───────────────────


async def test_the_drain_lets_a_deferred_teardown_finish():
    """Release is sync, so its genuinely-async steps run as background tasks
    nobody waits on. At shutdown that is the difference between an aiohttp
    session closed and an "Unclosed client session" on exit."""
    from app.output.base import drain_release_tasks, release_in_background
    closed = []

    async def _close():
        await asyncio.sleep(0)
        closed.append(True)

    release_in_background(_close(), label="test")
    assert closed == []                       # release itself did not wait

    await drain_release_tasks()

    assert closed == [True]


async def test_the_shutdown_drain_is_bounded_when_a_teardown_never_finishes():
    """The failure the bound exists to prevent: process exit held open by an
    unreachable renderer's GENA unsubscribe. Waiting forever is worse than
    abandoning the teardown, so the drain gives up and cancels."""
    from app.output import base
    from app.output.base import drain_release_tasks, release_in_background

    async def _never():
        await asyncio.Event().wait()

    release_in_background(_never(), label="test")
    hung = [t for t in base._release_tasks if not t.done()]
    assert len(hung) == 1

    await asyncio.wait_for(drain_release_tasks(timeout=0.05), timeout=5)

    assert hung[0].cancelled() or hung[0].done()


async def test_the_drain_returns_at_once_with_nothing_pending():
    from app.output.base import drain_release_tasks
    await asyncio.wait_for(drain_release_tasks(), timeout=5)


async def test_the_drain_does_not_raise_when_a_teardown_failed():
    """A deferred teardown that raised is already logged by
    ``release_in_background``; the drain collecting it must not re-raise into
    the shutdown path."""
    from app.output.base import drain_release_tasks, release_in_background

    async def _boom():
        raise OSError("renderer went away")

    release_in_background(_boom(), label="test")
    await drain_release_tasks()


# ── the end-to-end payoff of U5 + U6 together ────────────────────────────────


async def test_a_drop_from_a_released_device_does_not_hold_the_new_backend(
        monkeypatch):
    """*Covers AC3*, the whole point of the issue.

    Play on Cast, switch output to something else, and later the abandoned
    speaker sleeps. Before this plan that device's connection was still live,
    its listener still considered itself current, and the outage it reported
    opened a hold against whatever backend was playing perfectly at the time.

    Two independent barriers now, and this asserts BOTH: U6's release clears
    the reference the listener checks itself against (so the signal never
    leaves the socket thread's hop), and U5's reporter guard drops the report
    even when something reaches the handler directly."""
    from types import SimpleNamespace
    import app.state as st
    from app.output import session
    from app.output.chromecast import ChromecastBackend, _ConnectionListener
    from app.output.session import OutputSessionSupervisor

    sup = OutputSessionSupervisor(record_play=MagicMock(),
                                  timer_factory=lambda delay, cb: MagicMock())
    monkeypatch.setattr(session, "_supervisor", sup)
    outages = []
    sup.add_outage_listener(lambda *a: outages.append(a))

    cast = ChromecastBackend()
    device = MagicMock()
    cast._cast = device
    cast._device_id = "cast-1"
    cast._loop = asyncio.get_running_loop()
    cast._is_playing = True
    listener = _ConnectionListener(cast, device)

    new_backend = _RetireProbe()
    router = _router_with(cast, pending=new_backend)
    monkeypatch.setattr(st, "output_router", router)

    await router.swap_pending()               # the user switches output
    assert router.active is new_backend
    assert cast._cast is None                 # U6: the connection was released

    # The abandoned speaker drops, delivered exactly as pychromecast does it.
    listener.new_connection_status(SimpleNamespace(status="LOST"))
    await _pump()
    assert outages == [], "a released device's drop reached the supervisor"

    # And with the listener's own guard bypassed, U5 still refuses the report:
    # the reporter is not the active output.
    cast._is_playing = True
    cast._on_connection_lost()
    await _pump()
    assert outages == [], "a retired backend held the queue on the new one"


async def test_the_drain_waits_for_a_teardown_queued_after_it_started():
    """*Covers R4.* The drain re-reads the task set — from an EMPTY start.

    The first version of this test was a FALSE WITNESS, proven by mutation:
    deleting the re-poll left it green. It set the gate before its only
    ``sleep(0)``, so the "late" task was registered BEFORE the drain was ever
    called and the very first snapshot already contained it. The branch under
    test never ran.

    The empty start is the whole point, and it is the real shape: a superseded
    attach parked in its connect has scheduled nothing yet, so ``_release_tasks``
    is empty at the moment shutdown begins. A drain that returns on an empty
    snapshot exits in microseconds and the teardown is queued into a closing
    loop — which is exactly what the docstring on ``drain_release_tasks``
    describes and what the code still did."""
    from app.output import base
    freed = []
    drain_started = asyncio.Event()

    async def _late():
        freed.append("late")

    async def _queue_the_latecomer():
        # Only queues once the drain is genuinely under way, so the initial
        # snapshot is empty and the re-poll is the only thing that can catch it.
        await drain_started.wait()
        base.release_in_background(_late(), label="late teardown")

    watcher = asyncio.get_running_loop().create_task(_queue_the_latecomer())
    assert not [t for t in base._release_tasks if not t.done()], (
        "precondition: nothing is pending when the drain starts")

    drain = asyncio.get_running_loop().create_task(
        base.drain_release_tasks(timeout=1.0))
    await asyncio.sleep(0)
    drain_started.set()
    await drain
    # Sampled the instant the drain returns, BEFORE awaiting the watcher.
    # Reading `freed` after `await watcher` was the second false witness in
    # this one test: awaiting the watcher gives the loop a chance to run the
    # late teardown, so the assertion passed on scheduling rather than on the
    # drain having waited for anything.
    freed_when_drain_returned = list(freed)
    await watcher
    await _pump()

    assert freed_when_drain_returned == ["late"], (
        "the drain returned before a teardown that was queued after it "
        f"started; freed at that moment: {freed_when_drain_returned}")


_REPORTER_FUNCS = {"notify_outage", "notify_outage_threadsafe",
                   "notify_reconnect_trigger"}


def _reporter_calls_without_backend(tree: ast.AST) -> list[ast.Call]:
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(
            func, "id", None)
        if name not in _REPORTER_FUNCS:
            continue
        if not any(kw.arg == "backend" for kw in node.keywords):
            out.append(node)
    return out


def test_every_outage_report_names_its_reporter():
    """*Covers R3/R6.* The reporter-naming rule, enforced instead of remembered.

    U5's premise is that a connection which is not the adopted one has no
    authority — and the gate can only apply to a report that says who is
    making it. That was a per-call-site convention, and conventions drift: a
    review pass whose commit message was specifically about naming reporters
    still missed ``direct.py``'s sink error, because enumerating call sites by
    hand is exactly the thing this file already AST-guards two other lists to
    avoid.

    Scoped to ``app/output/`` because that is where reporters live; the
    supervisor's own internal calls are the thing being protected, not a
    reporter."""
    root = Path(__file__).resolve().parents[1] / "app" / "output"
    offenders = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in _reporter_calls_without_backend(tree):
            offenders.append(f"{path.name}:{call.lineno}")
    assert not offenders, (
        "these outage/reconnect reports do not name the backend making them, "
        "so the U5 authority gate cannot apply to them and a retired "
        "connection can hold the queue on whichever backend is active now:\n  "
        + "\n  ".join(offenders))


def test_the_reporter_guard_catches_an_unnamed_call():
    """Positive control — the detector must be able to fail."""
    bad = ast.parse("session.notify_outage('poll_errors')\n")
    good = ast.parse("session.notify_outage('poll_errors', backend=self)\n")
    assert len(_reporter_calls_without_backend(bad)) == 1
    assert _reporter_calls_without_backend(good) == []


class _RetireBackend(AttachGeneration):
    """A backend whose stop() can be held open, so a retire can be caught
    mid-flight the way the detached ``_stop_and_warn`` task really is."""

    def __init__(self) -> None:
        self.released = 0
        self.is_playing = False
        self._stop_gate = asyncio.Event()

    async def stop(self) -> None:
        await self._stop_gate.wait()          # a slow, real device round trip

    def release(self) -> None:
        self._supersede_attaches()
        self.released += 1

    def finish_stop(self) -> None:
        self._stop_gate.set()

    # Stand-ins for what a real backend holds and how it lets go. Every real
    # set_device frees the prior connection at entry, before its connect.
    held = None

    def adopt(self, conn) -> None:
        self.held = conn

    def start_attach_freeing_prior(self) -> int:
        token = self._begin_attach()
        self.held = None
        return token


async def test_a_stale_retire_stands_down_for_any_newer_attach():
    """*CR-3/ADV-9, settled 2026-08-22.* The retire stands down as soon as a
    newer attach has STARTED — it does not wait to see whether that attach
    succeeds.

    ``_stop_and_warn`` is a detached task holding no lock, and the ``stop()``
    it awaits is a real device round trip. In that gap the admin can Apply this
    very backend again ("Apply DLNA, change mind, Apply back"). Releasing then
    would supersede the live attach, and activate_backend would report success
    onto a backend holding nothing — or, after the typed outcome landed, 409 a
    switch nobody competed with.

    Standing down this early is safe only because of the sibling guarantee in
    ``test_a_newer_attach_frees_the_outgoing_before_the_retire_could``: every
    backend frees its outgoing connection at attach ENTRY, so by the time the
    generation has moved there is nothing left for this retire to free."""
    from app.output.router import OutputRouter
    old, new = _RetireBackend(), _RetireBackend()
    router = OutputRouter()
    router._active = old

    retire = asyncio.get_running_loop().create_task(router._stop_and_warn(old))
    await _pump()                              # parked inside stop()

    router._active = new                       # the switch completed…
    old._begin_attach()                        # …then the admin came back
    old.finish_stop()
    await retire

    assert old.released == 0, (
        "a stale retire released a backend with a newer attach in flight")


async def test_a_newer_attach_frees_the_outgoing_before_the_retire_could():
    """The guarantee the stand-down above rests on, stated as its own test.

    A newer attach silences the retire whether it succeeds or fails, so the
    connection it displaces must already be gone by then. Every real backend
    does this at entry — pinned per-backend in their own suites (see
    ``test_a_failed_attach_still_freed_the_outgoing_cast`` for Chromecast, the
    one that used to defer it to the swap and forced a second counter).

    Here the invariant itself: once an attach has claimed a generation, the
    backend must no longer hold what it held before."""
    from app.output.router import OutputRouter
    old = _RetireBackend()
    router = OutputRouter()
    router._active = old
    old.adopt("conn-A")                        # something is held

    old.start_attach_freeing_prior()           # a newer attach begins

    assert old.held is None, (
        "an attach that claimed a generation left the previous connection "
        "adopted — the retire will now stand down and nothing will free it")


async def test_a_retire_with_no_newer_attach_still_releases():
    """Scoping pin. A guard that always stood down would pass both tests above
    while silently restoring the orphan-on-switch-away bug U6 exists to fix."""
    from app.output.router import OutputRouter
    old, new = _RetireBackend(), _RetireBackend()
    router = OutputRouter()
    router._active = new                       # the switch already happened
    old.finish_stop()
    await router._stop_and_warn(old)

    assert old.released == 1, "an ordinary switch-away must still release"


# ── switch intent: the scoped-object contract (2026-08-21 redesign) ─────────


class _IntentBackend:
    """Minimal backend for router-level switch-intent tests."""

    def __init__(self, playing: bool = False) -> None:
        self.is_playing = playing
        self.released = 0

    async def stop(self) -> None:
        pass

    def release(self) -> None:
        self.released += 1


async def test_the_router_reports_a_switch_away_from_the_moment_it_is_requested():
    """*Covers ADV-13.* The router half of the switch-intent contract.

    The supervisor-side test pins what the outage gate does GIVEN a router
    reporting a switch in progress; it patches a stub, so it cannot see whether
    the real router ever reports one. Both halves are needed, and a mutation
    pass proved it: blanking ``is_switching_away_from`` left that test green."""
    from app.output.router import OutputRouter
    old, new = _IntentBackend(), _IntentBackend()
    router = OutputRouter()
    router._active = old

    assert not router.is_switching_away_from(old), "no switch is in flight yet"

    with router.switching_to(new) as switch:
        assert router.is_switching_away_from(old), (
            "the backend the admin committed to leaving still holds authority")
        assert not router.is_switching_away_from(new), (
            "the incoming backend is not the one being left")
        switch.commit()
    await _pump()

    assert not router.is_switching_away_from(old), (
        "the intent outlived the switch that committed it")


@pytest.mark.parametrize("blow_up", [RuntimeError, asyncio.CancelledError],
                         ids=["exception", "cancellation"])
async def test_the_intent_is_retired_however_the_switch_ends(blow_up):
    """*Covers the 2026-08-21 redesign.* The property the field-based version
    could not hold.

    Cancellation is the case that mattered and the case that was missed: the
    old code cleared the intent in an ``except Exception``, and
    ``CancelledError`` is a ``BaseException``. A cancelled Apply therefore left
    the router believing a switch was in flight FOREVER, which made
    ``_reporter_is_active`` deny authority to the live output for the rest of
    the process — the queue silently stopped self-healing, with no error
    anywhere. Parametrized over both so the ordinary-failure case cannot be
    the only one exercised."""
    from app.output.router import OutputRouter
    old, new = _IntentBackend(), _IntentBackend()
    router = OutputRouter()
    router._active = old

    with pytest.raises(blow_up):
        with router.switching_to(new):
            assert router.is_switching_away_from(old)
            raise blow_up()

    assert router._intent is None, "the intent survived the block"
    assert not router.is_switching_away_from(old), (
        "the live output can no longer report an outage — permanently")


async def test_a_second_switch_does_not_recapture_the_first_switchs_state():
    """*Covers the double-Apply defect.* ``was_playing`` is captured per
    intent, once.

    The field-based version re-read it on every request, and plexplayer clears
    ``_is_playing`` synchronously inside ``set_device`` — so a second Apply
    arriving during the first attach re-read the flag the FIRST attach had
    already cleared, and the immediate-vs-deferred decision then cut audio
    mid-track. Each intent must remember the world as it was when ITS user
    pressed the button."""
    from app.output.router import OutputRouter
    old, new = _IntentBackend(playing=True), _IntentBackend()
    router = OutputRouter()
    router._active = old

    first = router.switching_to(old)          # same instance, device change
    assert first.was_playing is True

    old.is_playing = False                    # what the attach does to it
    second = router.switching_to(new)
    assert second.was_playing is False, "the second intent reads the world now"
    assert first.was_playing is True, (
        "the first intent's captured state was overwritten by a later request")


async def test_one_switch_does_not_retire_anothers_intent():
    """*Covers the cross-clear defect.* The clear is identity-guarded.

    The old ``end_switch()`` took no argument and cleared unconditionally, so a
    switch finishing wiped the intent of a DIFFERENT switch that started after
    it — handing outage authority back to a backend the user had already left."""
    from app.output.router import OutputRouter
    old, a, b = _IntentBackend(), _IntentBackend(), _IntentBackend()
    router = OutputRouter()
    router._active = old

    first = router.switching_to(a).__enter__()
    second = router.switching_to(b).__enter__()
    assert router._intent is second

    first.__exit__(None, None, None)          # the earlier switch resolves
    assert router._intent is second, (
        "an earlier switch retired the later switch's intent")
    assert router.is_switching_away_from(old), (
        "authority came back to the outgoing backend mid-switch")

    second.__exit__(None, None, None)
    assert router._intent is None


async def test_the_drain_waits_for_a_latecomer_even_after_draining_something():
    """*2026-08-22 review, measured.* The empty grace is not cancelled by
    having already drained a task.

    The previous version gated the grace on ``seen``, which defeated the whole
    rewrite in the ONE case that matters. Real shutdown releases every backend
    before draining, so ``seen`` is non-empty exactly when a latecomer is
    possible — a superseded attach queues its teardown when it finally unwinds,
    seconds after ``release()`` returned. Measured: drain returned in 0.0ms and
    the teardown never ran, with ~1.95s of its budget unspent.

    The sibling test starts from a genuinely empty set; this one seeds an
    earlier task first, which is what production does and what makes the two
    differ."""
    from app.output import base
    freed = []

    async def early():
        freed.append("early")

    async def late():
        freed.append("late")

    async def queue_late():
        await asyncio.sleep(0.03)      # after the early task has drained
        base.release_in_background(late(), label="late teardown")

    watcher = asyncio.get_running_loop().create_task(queue_late())
    base.release_in_background(early(), label="early teardown")

    await base.drain_release_tasks(timeout=1.0)
    freed_when_drain_returned = list(freed)
    await watcher
    await _pump()

    assert "late" in freed_when_drain_returned, (
        "the drain returned without the latecomer because it had already "
        f"drained something; freed at that moment: {freed_when_drain_returned}")
