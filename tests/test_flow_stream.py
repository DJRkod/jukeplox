"""Tests for app.output.flow + the /api/stream/flow route (2026-07-11
supervisor plan U9 — Cast flow-mode stream engine).

Everything runs on in-python PCM fixtures through the injectable decoder/
encoder seams (no ffmpeg on the dev box) and on mock clocks/timers (repo
pytest-hang policy: no real sleeps — the pacing tests drive fake clocks).
"""

import asyncio
import gc
import logging
import struct
from types import SimpleNamespace

import httpx
import pytest

from app.output import flow
from tests.conftest import FakeTimerFactory

BPS = flow.FLOW_BYTES_PER_SECOND        # 176400 bytes of stitch PCM per second
FRAME = flow.FLOW_FRAME_BYTES           # 4 bytes per sample-frame (s16le stereo)


def make_pcm(frames: int, seed: int = 0) -> bytes:
    """Deterministic, per-seed-distinct stitch-format PCM (frame-aligned)."""
    return bytes(((i * 7 + seed) % 256) for i in range(frames * FRAME))


def trk(tid: str) -> SimpleNamespace:
    return SimpleNamespace(id=tid, title=f"Track {tid}")


async def settle(ticks: int = 80) -> None:
    """Drain the ready queue without real time passing."""
    for _ in range(ticks):
        await asyncio.sleep(0)


async def wait_decoder(env, key: str, timeout: float = 5.0):
    """Wait until the pump has created the decoder for ``key``."""

    async def _wait():
        while key not in env.decoders:
            await asyncio.sleep(0)
        return env.decoders[key][0]

    return await asyncio.wait_for(_wait(), timeout)


class FakePCMDecoder:
    """In-python decoder: yields fixture PCM, optionally blocking at a byte
    offset (releasable / unblocked by close) or failing at a byte offset."""

    def __init__(self, data: bytes, *, offset_ms: int = 0,
                 block_after: int | None = None,
                 fail_after: int | None = None) -> None:
        skip = (int(offset_ms * BPS / 1000) // FRAME) * FRAME
        self.data = data[skip:]
        self.offset_ms = offset_ms
        self.pos = 0
        self.block_after = block_after
        self.fail_after = fail_after
        self.closed = False
        self.blocked = asyncio.Event()   # observable: the decoder hit its gate
        self._wake = asyncio.Event()

    def release(self) -> None:
        self.block_after = None
        self._wake.set()

    async def read(self, n: int) -> bytes:
        await asyncio.sleep(0)  # model subprocess-read latency (interleaving)
        if self.closed:
            return b""
        if self.fail_after is not None and self.pos >= self.fail_after:
            raise flow.FlowDecodeError("fixture decode failure")
        if self.block_after is not None and self.pos >= self.block_after:
            self.blocked.set()
            self._wake.clear()
            await self._wake.wait()
            if self.closed:
                return b""
        take = min(n, len(self.data) - self.pos)
        if self.block_after is not None:
            take = min(take, self.block_after - self.pos)
        if self.fail_after is not None:
            take = min(take, self.fail_after - self.pos)
        if take <= 0:
            return b""
        chunk = self.data[self.pos:self.pos + take]
        self.pos += take
        return chunk

    async def close(self) -> None:
        self.closed = True
        self._wake.set()


class PassthroughEncoder:
    """Identity 'encoder': encoded output == fed PCM. Implements the encoder
    contract (start/feed/finalize/read/close)."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.fed_bytes = 0
        self.started = False
        self.finalized = False
        self.closed = False
        self._data = asyncio.Event()

    async def start(self) -> None:
        self.started = True

    async def feed(self, pcm: bytes) -> None:
        if self.closed:
            raise flow.FlowEncodeError("encoder closed")
        self.buf += pcm
        self.fed_bytes += len(pcm)
        self._data.set()

    async def finalize(self) -> None:
        self.finalized = True
        self._data.set()

    async def read(self, n: int) -> bytes:
        while not self.buf:
            if self.finalized or self.closed:
                return b""
            self._data.clear()
            await self._data.wait()
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    async def close(self) -> None:
        self.closed = True
        self._data.set()


class AutoClock:
    """sleep() jumps the clock forward — pacing resolves instantly."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.t += max(0.0, s)
        await asyncio.sleep(0)


class ManualClock:
    """sleep() parks until the test advances the clock past the target."""

    def __init__(self) -> None:
        self.t = 0.0
        self.waiters: list[list] = []

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        if s <= 0:
            await asyncio.sleep(0)
            return
        evt = asyncio.Event()
        self.waiters.append([self.t + s, evt])
        await evt.wait()

    def advance(self, dt: float) -> None:
        self.t += dt
        for w in self.waiters[:]:
            if w[0] <= self.t + 1e-9:
                self.waiters.remove(w)
                w[1].set()


BROKEN_FACTORY = object()  # pcm_map sentinel: decoder factory raises


@pytest.fixture(autouse=True)
def _flow_registry_reset():
    flow._current_session = None
    yield
    flow._current_session = None


@pytest.fixture
async def flow_factory():
    """Build a FlowSession over fixture PCM with every seam injected.

    ``pcm_map``: track id → PCM bytes, a dict spec ({"data", "block_after",
    "fail_after"}), or BROKEN_FACTORY. Tracks absent from the map are
    unresolvable. ``queue`` is the mutable fake queue the lookahead pops
    (consume-on-read stands in for U10's boundary-driven queue advance)."""
    created: list[flow.FlowSession] = []

    def make(first, queue, pcm_map, *, start=True, **over):
        env = SimpleNamespace(
            queue=queue, decoders={}, boundaries=[], skips=[],
            gone=0, ended=0, timers=FakeTimerFactory(),
        )

        def dec_factory(source, headers, offset_ms):
            spec = pcm_map[source]
            if spec is BROKEN_FACTORY:
                raise RuntimeError("decoder factory boom")
            if isinstance(spec, dict):
                d = FakePCMDecoder(spec["data"], offset_ms=offset_ms,
                                   block_after=spec.get("block_after"),
                                   fail_after=spec.get("fail_after"))
            else:
                d = FakePCMDecoder(spec, offset_ms=offset_ms)
            env.decoders.setdefault(source, []).append(d)
            return d

        async def resolver(track):
            return (track.id, {}) if track.id in pcm_map else None

        async def next_fn(prev):
            return queue.pop(0) if queue else None

        cfg = dict(
            decoder_factory=dec_factory,
            encoder_factory=lambda fmt, sr, ch: PassthroughEncoder(),
            source_resolver=resolver,
            next_track_fn=next_fn,
            run_ahead_s=1e9,
            timer_factory=env.timers,
        )
        cfg.update(over)
        s = flow.FlowSession(first, **cfg)
        s.add_boundary_listener(env.boundaries.append)
        s.add_skip_listener(lambda t, reason: env.skips.append((t, reason)))

        def _gone():
            env.gone += 1

        def _ended():
            env.ended += 1

        s.add_consumer_gone_listener(_gone)
        s.add_ended_listener(_ended)
        created.append(s)
        if start:
            s.start()
        return s, env

    yield make
    for s in created:
        await s.close()


async def collect(session, timeout: float = 5.0) -> bytes:
    out = bytearray()

    async def _run():
        async for chunk in session.bind_consumer():
            out.extend(chunk)

    await asyncio.wait_for(_run(), timeout)
    return bytes(out)


# ── stitch exactness + boundary clock ─────────────────────────────────────────


async def test_two_fixtures_stitch_sample_exact(flow_factory):
    """Track B's first PCM byte follows track A's last — zero padding, zero
    truncation — and the boundary event fires at the exact stream offset."""
    a, b = trk("a"), trk("b")
    pcm_a, pcm_b = make_pcm(4410, seed=1), make_pcm(2205, seed=2)
    session, env = flow_factory(a, [b], {"a": pcm_a, "b": pcm_b})

    out = await collect(session)

    assert out == pcm_a + pcm_b            # sample-count exact, no drop/dup
    assert len(env.boundaries) == 1        # the initial track is the dispatch,
    ev = env.boundaries[0]                 # not a boundary — only A→B fires
    assert ev.track is b
    assert ev.offset_samples == 4410
    assert ev.offset_ms == 100
    assert ev.reposition is False
    assert session.boundaries == [(0, a), (4410, b)]
    assert session.ended and env.ended == 1


async def test_track_mapping_through_boundary_ledger(flow_factory):
    """track_at/offset_of map stitch-timeline positions to tracks — the U10
    device-time reconciliation surface."""
    a, b = trk("a"), trk("b")
    session, _env = flow_factory(
        a, [b], {"a": make_pcm(4410, 1), "b": make_pcm(2205, 2)})
    await collect(session)

    assert session.track_at(0) is a
    assert session.track_at(99) is a
    assert session.track_at(100) is b
    assert session.track_at(10_000) is b     # beyond the encode clock → clamp
    assert session.offset_of(a) == 0
    assert session.offset_of(b) == 100
    assert session.offset_of(trk("zz")) is None


async def test_held_offset_from_device_time_mapping(flow_factory):
    """Device-reported time maps through the stitch timeline (with the U10
    re-LOAD rebase), clamped to the encode clock; None falls back to the
    documented encode-clock-minus-run-ahead estimate."""
    a = trk("a")
    clock = AutoClock()
    session, _env = flow_factory(
        a, [], {"a": make_pcm(44100 * 3, 1)},        # 3s track
        run_ahead_s=2.0, clock=clock, sleep=clock.sleep)
    await collect(session)

    assert session.position_ms == 3000
    assert session.held_offset_from_device_time(1.5) == 1500
    assert session.held_offset_from_device_time(100.0) == 3000   # clamp to fed
    session.set_device_epoch_offset(1000)                        # resume re-LOAD rebase
    assert session.held_offset_from_device_time(1.5) == 2500
    # No device report → encode clock minus the run-ahead margin.
    assert session.held_offset_from_device_time(None) == 1000


# ── lookahead follows the queue (R14) ─────────────────────────────────────────


async def test_queue_edit_before_boundary_repositions_lookahead(flow_factory):
    """A queue edit landing before the boundary re-resolves the lookahead —
    the boundary event names the NEW track."""
    a, b, c = trk("a"), trk("b"), trk("c")
    pcm_a, pcm_c = make_pcm(4410, 1), make_pcm(2205, 3)
    queue = [b]
    session, env = flow_factory(
        a, queue,
        {"a": {"data": pcm_a, "block_after": len(pcm_a) // 2},
         "b": make_pcm(2205, 2), "c": pcm_c})

    consumer = asyncio.ensure_future(collect(session))
    dec_a = await wait_decoder(env, "a")
    await asyncio.wait_for(dec_a.blocked.wait(), 5)
    queue[:] = [c]                     # the edit, before A's decode completes
    dec_a.release()

    out = await asyncio.wait_for(consumer, 5)
    assert out == pcm_a + pcm_c        # C spliced, B never decoded
    assert [ev.track for ev in env.boundaries] == [c]
    assert env.boundaries[0].offset_samples == 4410
    assert "b" not in env.decoders


# ── skip/seek repositioning ───────────────────────────────────────────────────


async def test_skip_mid_track_repositions_with_one_boundary(flow_factory):
    """reposition() invalidates the in-flight decode (generation), splices the
    new track at the current encode position, and emits exactly ONE boundary
    event — no double-advance."""
    a, b = trk("a"), trk("b")
    pcm_a, pcm_b = make_pcm(88200, 1), make_pcm(4410, 2)   # A=2s, B=0.1s
    session, env = flow_factory(
        a, [], {"a": {"data": pcm_a, "block_after": BPS}, "b": pcm_b})

    consumer = asyncio.ensure_future(collect(session))
    dec_a = await wait_decoder(env, "a")
    await asyncio.wait_for(dec_a.blocked.wait(), 5)        # 1s of A fed

    assert await session.reposition(b, 0) is True

    out = await asyncio.wait_for(consumer, 5)
    assert out == pcm_a[:BPS] + pcm_b      # old-position decode never leaks
    assert dec_a.closed                    # in-flight decoder reaped
    assert len(env.boundaries) == 1        # ONE event for the reposition
    ev = env.boundaries[0]
    assert ev.track is b and ev.reposition is True
    assert ev.offset_samples == BPS // FRAME
    assert session.boundaries == [(0, a), (BPS // FRAME, b)]


async def test_reposition_after_end_returns_false(flow_factory):
    a, b = trk("a"), trk("b")
    session, _env = flow_factory(a, [], {"a": make_pcm(441, 1),
                                         "b": make_pcm(441, 2)})
    await collect(session)
    assert session.ended
    assert await session.reposition(b, 0) is False


# ── queue exhaustion → clean finalize ─────────────────────────────────────────


async def test_queue_exhausts_stream_ends_cleanly(flow_factory):
    """Lookahead returns None → encoder flushed, consumer terminates, ended
    listener fires, close is idempotent, and NO consumer-gone/grace fires for
    a naturally-drained disconnect."""
    a = trk("a")
    session, env = flow_factory(a, [], {"a": make_pcm(4410, 1)})

    out = await collect(session)

    assert out == make_pcm(4410, 1)
    assert session.ended and env.ended == 1
    assert session._encoder.finalized      # encoder flushed
    assert env.timers.timers == []         # no grace armed on natural end
    assert env.gone == 0
    await session.close()
    await session.close()                  # idempotent
    assert session.closed
    with pytest.raises(RuntimeError):
        session.bind_consumer()


# ── decode failure → server-side skip, never a stall ──────────────────────────


async def test_decode_failure_mid_flow_skips_and_splices(flow_factory):
    """A decode dying mid-track emits a skip event and splices the next track
    at the failure offset — the stream never stalls."""
    a, b, c = trk("a"), trk("b"), trk("c")
    pcm_a, pcm_b, pcm_c = make_pcm(4410, 1), make_pcm(44100, 2), make_pcm(2205, 3)
    fail_at = 8820  # bytes of B that decode before the failure
    session, env = flow_factory(
        a, [b, c],
        {"a": pcm_a, "b": {"data": pcm_b, "fail_after": fail_at}, "c": pcm_c})

    out = await collect(session)

    assert out == pcm_a + pcm_b[:fail_at] + pcm_c
    assert [t for t, _ in env.skips] == [b]
    assert "decode failed" in env.skips[0][1]
    assert [ev.track for ev in env.boundaries] == [b, c]
    assert env.boundaries[1].offset_samples == (len(pcm_a) + fail_at) // FRAME
    assert session.ended


async def test_decoder_factory_failure_skips_track(flow_factory):
    """A track whose decoder can't even start is skipped — no boundary is
    recorded for it (no audio entered the stitch)."""
    a, b, c = trk("a"), trk("b"), trk("c")
    pcm_a, pcm_c = make_pcm(4410, 1), make_pcm(2205, 3)
    session, env = flow_factory(
        a, [b, c], {"a": pcm_a, "b": BROKEN_FACTORY, "c": pcm_c})

    out = await collect(session)

    assert out == pcm_a + pcm_c
    assert [t for t, _ in env.skips] == [b]
    assert [ev.track for ev in env.boundaries] == [c]
    assert session.offset_of(b) is None


async def test_unresolvable_track_skips(flow_factory):
    """No resolvable source → skip event with the unresolvable reason."""
    a, b, c = trk("a"), trk("b"), trk("c")
    session, env = flow_factory(
        a, [b, c], {"a": make_pcm(441, 1), "c": make_pcm(441, 3)})  # no "b"

    out = await collect(session)

    assert out == make_pcm(441, 1) + make_pcm(441, 3)
    assert env.skips == [(b, "unresolvable source")]


async def test_failed_track_spin_guard_ends_flow(flow_factory):
    """If the lookahead keeps returning the just-skipped track (the consumer
    never removed it), the flow ends instead of spinning on decode failures."""
    a, b = trk("a"), trk("b")

    async def stuck_next(prev):
        return b

    session, env = flow_factory(
        a, [], {"a": make_pcm(441, 1), "b": BROKEN_FACTORY},
        next_track_fn=stuck_next)

    out = await collect(session)

    assert out == make_pcm(441, 1)
    assert [t for t, _ in env.skips] == [b]   # skipped exactly once, no spin
    assert session.ended


# ── encoder-reader death → session closes, consumer never parks forever ───────


class ExplodingEncoder(PassthroughEncoder):
    """PassthroughEncoder whose read() raises once ``explode_after`` encoded
    bytes have been handed out — models the encoder pipe dying mid-stream."""

    def __init__(self, explode_after: int) -> None:
        super().__init__()
        self.explode_after = explode_after
        self.read_out = 0

    async def read(self, n: int) -> bytes:
        if self.read_out >= self.explode_after:
            raise OSError("encoder read blew up")
        out = await super().read(n)
        self.read_out += len(out)
        return out


async def test_encoder_reader_crash_closes_session(flow_factory):
    """The encoder-reader task dying mid-stream must CLOSE the session —
    without it _ended/_closed never flip and the bound consumer parks forever
    on the out-buffer wait. The consumer's async-for terminates, no ended/
    consumer-gone fires, and the in-flight decoder is reaped."""
    a = trk("a")
    pcm_a = make_pcm(44100, 1)
    fed = 8820                              # bytes fed before the reader dies
    session, env = flow_factory(
        a, [], {"a": {"data": pcm_a, "block_after": fed}},
        encoder_factory=lambda fmt, sr, ch: ExplodingEncoder(fed))

    g = session.bind_consumer()             # bound before the crash
    out = bytearray()

    async def drain():
        async for chunk in g:
            out.extend(chunk)

    await asyncio.wait_for(drain(), 5)      # terminates — never a forever-park

    assert session.closed
    assert bytes(out) == pcm_a[:fed]        # delivered bytes intact
    assert not session.ended and env.ended == 0   # a crash is not a clean end
    assert env.gone == 0
    assert env.timers.timers == []          # no grace armed on a closed session

    async def _reaped():                    # close() finishes off-consumer —
        while not env.decoders["a"][0].closed:    # bounded wait for teardown
            await asyncio.sleep(0)

    await asyncio.wait_for(_reaped(), 5)    # in-flight decoder reaped
    assert session._encoder.closed


# ── decode stall → watchdog skip, never a frozen stream ───────────────────────


async def test_stalled_source_skipped_server_side(flow_factory):
    """A source that stalls (no bytes, no EOF) is treated as a decode failure
    within the stall bound: the decoder is closed, a server-side skip event
    fires, and the next track splices — the stream continues to a clean end
    instead of freezing forever (no boundary, no consumer disconnect, and no
    outage watchdog can fire on a silent stall in flow mode)."""
    a, b = trk("a"), trk("b")
    pcm_a, pcm_b = make_pcm(44100, 1), make_pcm(2205, 2)
    stall_at = 8820                         # bytes of A decoded before the stall
    session, env = flow_factory(
        a, [b],
        {"a": {"data": pcm_a, "block_after": stall_at}, "b": pcm_b},
        decode_stall_s=0.05)                # injectable bound — keeps the test fast

    out = await asyncio.wait_for(collect(session), 5)

    assert out == pcm_a[:stall_at] + pcm_b  # spliced at the stall point
    assert [t for t, _ in env.skips] == [a]
    assert "stalled" in env.skips[0][1]
    assert env.decoders["a"][0].closed      # the stalled decoder was reaped
    assert [ev.track for ev in env.boundaries] == [b]
    assert env.boundaries[0].offset_samples == stall_at // FRAME
    assert session.ended


async def test_paused_session_never_stall_skips(flow_factory):
    """pause() legitimately stops reading — a stall bound elapsing while
    paused is retried, never a skip verdict; resume() continues the SAME
    decode to a clean, skip-free end."""
    a = trk("a")
    pcm_a = make_pcm(4410, 1)
    gate = len(pcm_a) // 2
    read_cancelled = asyncio.Event()        # observable: the bounded read timed out

    class ProbeDecoder(FakePCMDecoder):
        async def read(self, n: int) -> bytes:
            try:
                return await super().read(n)
            except asyncio.CancelledError:
                read_cancelled.set()
                raise

    decoders: list[ProbeDecoder] = []

    def dec_factory(source, headers, offset_ms):
        d = ProbeDecoder(pcm_a, block_after=gate)
        decoders.append(d)
        return d

    session, env = flow_factory(
        a, [], {"a": pcm_a},
        decoder_factory=dec_factory, decode_stall_s=0.25)

    consumer = asyncio.ensure_future(collect(session))

    async def _decoder():
        while not decoders:
            await asyncio.sleep(0)

    await asyncio.wait_for(_decoder(), 5)
    await asyncio.wait_for(decoders[0].blocked.wait(), 5)   # read in flight
    session.pause()
    # The stall bound elapses WHILE paused: the guarded read is cancelled…
    await asyncio.wait_for(read_cancelled.wait(), 5)
    await settle()
    assert env.skips == []                  # …but no stall verdict
    assert not decoders[0].closed           # the decode is still live

    session.resume()
    decoders[0].release()
    out = await asyncio.wait_for(consumer, 5)
    assert out == pcm_a                     # the SAME decode completed
    assert env.skips == []
    assert len(decoders) == 1               # never re-resolved / restarted
    assert session.ended


# ── pacing: bounded run-ahead, pause freezes the encode clock ─────────────────


async def test_pacing_bounds_run_ahead_and_pause_freezes_clock(flow_factory):
    """The encode side never leads the (pause-aware) playback clock by more
    than run_ahead_s; pause() stops the encode clock. Manual fake clock —
    no real sleeps."""
    mc = ManualClock()
    a = trk("a")
    session, _env = flow_factory(
        a, [], {"a": make_pcm(BPS * 5 // FRAME, 1)},        # 5s track
        run_ahead_s=1.0, clock=mc, sleep=mc.sleep)
    await settle()

    # At t=0 the pump may feed at most 1s of audio (chunk-granular under).
    fed0 = session._fed_bytes
    assert BPS - 65536 <= fed0 <= BPS

    session.pause()
    mc.advance(10.0)
    await settle()
    assert session._fed_bytes == fed0      # encode clock frozen
    mc.advance(100.0)
    await settle()
    assert session._fed_bytes == fed0      # still frozen, however long

    session.resume()
    await settle()
    assert session._fed_bytes == fed0      # elapsed unchanged by the pause
    mc.advance(1.0)                        # now 1s of playback has elapsed
    await settle()
    fed1 = session._fed_bytes
    assert 2 * BPS - 65536 <= fed1 <= 2 * BPS
    await session.close()


async def test_memory_bound_long_session(flow_factory):
    """Buffered-but-unsent bytes never exceed the run-ahead budget across a
    long many-track session — the pacing bound IS the memory bound."""
    clock = AutoClock()
    tracks = [trk(f"t{i}") for i in range(12)]
    pcm_map = {t.id: make_pcm(44100 // 2, i) for i, t in enumerate(tracks)}
    per_track = 44100 // 2 * FRAME
    session, env = flow_factory(
        tracks[0], tracks[1:], pcm_map,
        run_ahead_s=2.0, clock=clock, sleep=clock.sleep)
    cap = session.out_buffer_cap_bytes
    assert cap == int(2.0 * BPS)

    violations = []

    async def monitor():
        while not session.ended and not session.closed:
            if session.buffered_unsent_bytes > cap:
                violations.append(session.buffered_unsent_bytes)
            await asyncio.sleep(0)

    mon = asyncio.ensure_future(monitor())
    total = 0
    peak = 0

    async def consume():
        nonlocal total, peak
        async for chunk in session.bind_consumer():
            total += len(chunk)
            peak = max(peak, session.buffered_unsent_bytes)

    await asyncio.wait_for(consume(), 10)
    mon.cancel()

    assert violations == []
    assert peak <= cap
    assert total == per_track * 12         # nothing dropped across 12 splices
    assert len(env.boundaries) == 11       # every non-initial track crossed
    assert session.ended


# ── consumer binding: grace re-bind, conflict, consumer-gone ──────────────────


async def test_consumer_rebind_within_grace_resumes_same_session(flow_factory):
    """A disconnect arms the grace timer; a re-bind within it cancels the
    timer, fires no consumer-gone, spawns no second stitcher, and resumes
    from the current encode position (no lost or replayed bytes)."""
    a = trk("a")
    pcm_a = make_pcm(4410, 1)
    session, env = flow_factory(
        a, [], {"a": {"data": pcm_a, "block_after": len(pcm_a) // 2}})

    g1 = session.bind_consumer()
    first = await asyncio.wait_for(g1.__anext__(), 5)
    await g1.aclose()                       # consumer disconnect
    assert len(env.timers.timers) == 1      # grace armed
    assert env.timers.timers[0].delay == flow.FLOW_CONSUMER_GRACE_S

    g2 = session.bind_consumer()            # re-GET within grace
    assert env.timers.timers[0].cancelled   # grace cancelled
    assert env.gone == 0                    # never went outage-suspected

    env.decoders["a"][0].release()
    rest = bytearray()

    async def drain():
        async for chunk in g2:
            rest.extend(chunk)

    await asyncio.wait_for(drain(), 5)
    assert first + bytes(rest) == pcm_a     # byte-continuous across the gap
    assert len(env.decoders["a"]) == 1      # the SAME stitcher, not a second


async def test_grace_expiry_fires_consumer_gone(flow_factory):
    """Grace expiring with no re-bind notifies the consumer-gone hook (U10
    turns it into outage-suspected); the engine itself keeps the session."""
    a = trk("a")
    session, env = flow_factory(
        a, [], {"a": {"data": make_pcm(4410, 1), "block_after": 8820}})

    g1 = session.bind_consumer()
    await asyncio.wait_for(g1.__anext__(), 5)
    await g1.aclose()
    env.timers.timers[0].fire()             # the grace window elapses

    assert env.gone == 1
    assert not session.closed               # hook only — U10 decides


async def test_second_concurrent_consumer_rejected(flow_factory):
    """A second bind while one consumer is bound raises (the route's 409);
    the bound consumer and the stitcher are unaffected."""
    a = trk("a")
    pcm_a = make_pcm(4410, 1)
    session, env = flow_factory(
        a, [], {"a": {"data": pcm_a, "block_after": 8820}})

    g1 = session.bind_consumer()
    first = await asyncio.wait_for(g1.__anext__(), 5)
    with pytest.raises(flow.FlowConsumerConflict):
        session.bind_consumer()

    env.decoders["a"][0].release()          # the bound consumer still works
    rest = bytearray()

    async def drain():
        async for chunk in g1:
            rest.extend(chunk)

    await asyncio.wait_for(drain(), 5)
    assert first + bytes(rest) == pcm_a
    assert len(env.decoders["a"]) == 1


async def test_close_reaps_decoder_and_unblocks_consumer(flow_factory):
    """close() mid-stream terminates the consumer, reaps the in-flight
    decoder, and closes the encoder — idempotently."""
    a = trk("a")
    session, env = flow_factory(
        a, [], {"a": {"data": make_pcm(88200, 1), "block_after": 8820}})
    g1 = session.bind_consumer()
    await asyncio.wait_for(g1.__anext__(), 5)

    await session.close()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(g1.__anext__(), 5)
    assert env.decoders["a"][0].closed
    assert session._encoder.closed
    await session.close()                   # idempotent


# ── encode-format knob ────────────────────────────────────────────────────────


async def test_wav_encode_format_serves_streaming_header_plus_pcm(flow_factory):
    """encode_format='wav' (the deferred-to-hardware fallback) uses the pure-
    python WAV passthrough: a streaming header (unknown sizes) then raw PCM."""
    a = trk("a")
    pcm_a = make_pcm(4410, 1)
    session, _env = flow_factory(
        a, [], {"a": pcm_a},
        encode_format="wav", encoder_factory=None)   # default factory → WAV

    out = await collect(session)

    assert session.content_type == "audio/wav"
    assert out[:4] == b"RIFF"
    assert out[8:12] == b"WAVE"
    assert struct.unpack("<I", out[4:8])[0] == 0xFFFFFFFF   # endless stream
    assert out[44:] == pcm_a                                # passthrough PCM


async def test_encode_format_knob_validation(flow_factory):
    a = trk("a")
    session, _env = flow_factory(a, [], {"a": make_pcm(441, 1)}, start=False)
    assert session.content_type == "audio/flac"             # default knob
    with pytest.raises(ValueError):
        flow.FlowSession(a, encode_format="mp3")


def test_ffmpeg_decode_args_follow_airplay_conventions():
    """Pure-arg pin: -nostdin, failure-only stderr, stitch format, and the
    output-side -ss (AFTER -i — input-side seeks Range-storm HTTP sources)."""
    args = flow._build_flow_decode_args("/srv/music/x.flac", None)
    assert args[:4] == ["ffmpeg", "-nostdin", "-loglevel", "error"]
    assert "-ss" not in args
    for pair in (["-acodec", "pcm_s16le"], ["-ac", "2"], ["-ar", "44100"],
                 ["-f", "s16le"]):
        i = args.index(pair[0])
        assert args[i + 1] == pair[1]
    assert args[args.index("-i") + 1] == "/srv/music/x.flac"
    assert args[-1] == "pipe:1"

    seek = flow._build_flow_decode_args("/srv/music/x.flac", None, 5000)
    assert seek.index("-ss") > seek.index("-i")
    assert seek[seek.index("-ss") + 1] == "5.000"


def test_flow_decode_args_http_source_keeps_credentials_off_argv():
    """http(s) sources decode via httpx→stdin: the ffmpeg argv carries no
    source URL, no token query, and no auth header — provider credentials
    never reach the process list (the per-track proxy's pipe:0 posture)."""
    args = flow._build_flow_decode_args(
        "http://plex:32400/library/parts/1/file.flac?X-Plex-Token=SECRET",
        {"Authorization": "MediaBrowser Token=SECRET2"})
    joined = " ".join(args)
    assert "SECRET" not in joined
    assert "X-Plex-Token" not in joined
    assert "Authorization" not in joined
    assert "-headers" not in args
    assert args[args.index("-i") + 1] == "pipe:0"
    assert args[-1] == "pipe:1"

    # Output-side seek is preserved in pipe mode (still after -i).
    seek = flow._build_flow_decode_args("https://src/x?token=s", None, 5000)
    assert seek.index("-ss") > seek.index("-i")
    assert seek[seek.index("-i") + 1] == "pipe:0"


def test_redact_scrubs_url_query_strings():
    assert flow._redact("open http://h:32400/p.flac?X-Plex-Token=abc&x=1 "
                        "failed") == "open http://h:32400/p.flac?<redacted> failed"
    assert flow._redact("no urls here") == "no urls here"
    assert flow._redact("https://h/p") == "https://h/p"   # no query → untouched


async def test_drain_stderr_redacts_token_urls(caplog):
    """ffmpeg stderr lines are logged with URL queries scrubbed — a token-
    bearing URL never reaches the WARNING log."""
    reader = asyncio.StreamReader()
    reader.feed_data(
        b"http://plex:32400/x.flac?X-Plex-Token=SECRET: I/O error\n")
    reader.feed_eof()
    with caplog.at_level(logging.WARNING, logger="app.output.flow"):
        await flow._drain_stderr(reader, "decode")
    assert "SECRET" not in caplog.text
    assert "<redacted>" in caplog.text
    assert "I/O error" in caplog.text      # the diagnostic itself is kept


def test_ffmpeg_encode_args_stream_flac_from_pcm():
    args = flow._build_flow_encode_args()
    assert args.index("-f") < args.index("-i")               # input format first
    assert args[args.index("-i") + 1] == "pipe:0"
    assert args[-3:] == ["-f", "flac", "pipe:1"]


# ── module registry (create/get/close — U10 drives it) ────────────────────────


def _registry_kwargs(pcm_map):
    async def resolver(track):
        return (track.id, {}) if track.id in pcm_map else None

    async def next_fn(prev):
        return None

    return dict(
        decoder_factory=lambda src, hdrs, off: FakePCMDecoder(pcm_map[src]),
        encoder_factory=lambda fmt, sr, ch: PassthroughEncoder(),
        source_resolver=resolver,
        next_track_fn=next_fn,
        run_ahead_s=1e9,
        timer_factory=FakeTimerFactory(),
    )


async def test_registry_single_session_lifecycle():
    a, b = trk("a"), trk("b")
    pcm_map = {"a": make_pcm(441, 1), "b": make_pcm(441, 2)}
    s1 = flow.create_flow_session(a, **_registry_kwargs(pcm_map))
    try:
        assert flow.current_flow_session() is s1
        assert flow.get_flow_session(s1.session_id) is s1
        assert flow.get_flow_session("nope") is None
        assert s1.url_path == f"/api/stream/flow/{s1.session_id}"

        s2 = flow.create_flow_session(b, **_registry_kwargs(pcm_map))
        await settle()                       # the superseding close runs
        assert s1.closed                     # never two stitchers
        assert flow.current_flow_session() is s2
        assert flow.get_flow_session(s1.session_id) is None

        await flow.close_flow_session()
        assert s2.closed
        assert flow.current_flow_session() is None
    finally:
        await flow.close_flow_session()
        if not s1.closed:
            await s1.close()


# ── the flow route: chunked, Range-less, single-session ───────────────────────


def _asgi_client():
    from app.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")


async def test_flow_route_serves_chunked_rangeless_stream():
    """GET on the flow URL streams the session's encoded bytes chunked and
    Range-less — alongside (not disturbing) the seekable per-track proxy,
    whose auth contract still answers on the same router."""
    a = trk("a")
    pcm_a = make_pcm(4410, 1)
    session = flow.create_flow_session(a, **_registry_kwargs({"a": pcm_a}))
    try:
        async with _asgi_client() as client:
            r = await asyncio.wait_for(client.get(session.url_path), 10)
            assert r.status_code == 200
            assert r.content == pcm_a                      # passthrough encoder
            assert r.headers["content-type"] == "audio/flac"
            assert r.headers["accept-ranges"] == "none"    # Range-less live read
            assert r.headers["cache-control"] == "no-store"
            assert "content-length" not in r.headers       # chunked, endless shape

            # The per-track proxy contract is untouched next door: an
            # unauthorized key still 403s through the same router.
            r2 = await asyncio.wait_for(
                client.get("/api/stream", params={"key": "nope"}), 10)
            assert r2.status_code == 403
    finally:
        await flow.close_flow_session()


async def test_flow_route_unknown_or_closed_session_404():
    a = trk("a")
    session = flow.create_flow_session(
        a, **_registry_kwargs({"a": make_pcm(441, 1)}))
    async with _asgi_client() as client:
        r = await asyncio.wait_for(
            client.get("/api/stream/flow/not-a-session"), 10)
        assert r.status_code == 404
        await session.close()                              # closed → gone
        r2 = await asyncio.wait_for(client.get(session.url_path), 10)
        assert r2.status_code == 404


async def test_flow_route_second_concurrent_get_409():
    """A second GET while a consumer is bound is rejected with 409 — the
    bound consumer, the session, and the boundary clock are unaffected."""
    a = trk("a")
    pcm_map = {"a": {"data": make_pcm(4410, 1), "block_after": 8820}}

    async def resolver(track):
        return (track.id, {})

    async def next_fn(prev):
        return None

    decoders = []

    def dec_factory(src, hdrs, off):
        d = FakePCMDecoder(pcm_map[src]["data"],
                           block_after=pcm_map[src]["block_after"])
        decoders.append(d)
        return d

    session = flow.create_flow_session(
        a,
        decoder_factory=dec_factory,
        encoder_factory=lambda fmt, sr, ch: PassthroughEncoder(),
        source_resolver=resolver,
        next_track_fn=next_fn,
        run_ahead_s=1e9,
        timer_factory=FakeTimerFactory(),
    )
    try:
        g1 = session.bind_consumer()                       # the device's stream
        await asyncio.wait_for(g1.__anext__(), 5)
        async with _asgi_client() as client:
            r = await asyncio.wait_for(client.get(session.url_path), 10)
            assert r.status_code == 409
        assert not session.closed                          # session unaffected
        assert len(decoders) == 1                          # no second stitcher
    finally:
        await flow.close_flow_session()


async def test_flow_route_drop_during_priming_releases_binding():
    """A client that vanishes while the route awaits the first chunk (the
    request task is cancelled mid-priming) must release the binding: the
    route-primed generator is STARTED, so cancellation runs its finally, the
    grace arms, and the receiver's re-GET binds instead of 409ing forever."""
    from app.api.stream import stream_flow

    a = trk("a")
    pcm_a = make_pcm(4410, 1)
    decoders: list[FakePCMDecoder] = []

    def dec_factory(source, headers, offset_ms):
        d = FakePCMDecoder(pcm_a, block_after=0)   # nothing decoded yet —
        decoders.append(d)                         # priming parks on data
        return d

    async def resolver(track):
        return (track.id, {})

    async def next_fn(prev):
        return None

    timers = FakeTimerFactory()
    session = flow.create_flow_session(
        a,
        decoder_factory=dec_factory,
        encoder_factory=lambda fmt, sr, ch: PassthroughEncoder(),
        source_resolver=resolver,
        next_track_fn=next_fn,
        run_ahead_s=1e9,
        timer_factory=timers,
    )
    try:
        req_task = asyncio.ensure_future(
            stream_flow(session.session_id, request=None))

        async def _bound():
            while not session._consumer_bound:
                await asyncio.sleep(0)

        await asyncio.wait_for(_bound(), 5)        # route bound, priming parked
        req_task.cancel()                          # the client drop
        with pytest.raises(asyncio.CancelledError):
            await req_task

        assert session._consumer_bound is False    # binding released
        assert len(timers.timers) == 1             # disconnect grace armed
        g2 = session.bind_consumer()               # re-GET binds — no 409
        assert timers.timers[0].cancelled          # grace cancelled by re-bind

        async def _decoder():
            while not decoders:
                await asyncio.sleep(0)

        await asyncio.wait_for(_decoder(), 5)
        decoders[0].release()
        out = bytearray()

        async def drain():
            async for chunk in g2:
                out.extend(chunk)

        await asyncio.wait_for(drain(), 5)
        assert bytes(out) == pcm_a                 # same session, full stream
    finally:
        await flow.close_flow_session()


async def test_flow_body_abandoned_before_start_releases_binding(flow_factory):
    """Worst case: Starlette abandons the response-body wrapper UNSTARTED
    (client gone before the first body __anext__). The route already primed
    the bound generator, so dropping the wrapper releases the last reference
    to a STARTED generator — asyncio's async-gen finalization runs its
    finally, the binding releases, and the next bind succeeds (never a
    permanent 409)."""
    from app.api.stream import _primed_flow_body

    a = trk("a")
    pcm_a = make_pcm(4410, 1)
    session, env = flow_factory(
        a, [], {"a": {"data": pcm_a, "block_after": len(pcm_a) // 2}})

    body = session.bind_consumer()
    first = await asyncio.wait_for(body.__anext__(), 5)  # the route's priming
    wrapper = _primed_flow_body(first, body)             # never started
    del body
    del wrapper                                          # the instant drop
    gc.collect()

    async def _released():
        while session._consumer_bound:
            await asyncio.sleep(0)

    await asyncio.wait_for(_released(), 5)     # finalizer ran the finally
    assert len(env.timers.timers) == 1         # disconnect grace armed
    g2 = session.bind_consumer()               # no permanent 409
    env.decoders["a"][0].release()
    rest = bytearray()

    async def drain():
        async for chunk in g2:
            rest.extend(chunk)

    await asyncio.wait_for(drain(), 5)
    assert first + bytes(rest) == pcm_a        # byte-continuous across the drop


async def test_flow_route_ended_drained_session_serves_empty_body():
    """A GET on an ended-and-drained (but not closed) session gets a clean
    empty 200 — the priming read hits StopAsyncIteration, not a hang."""
    a = trk("a")
    session = flow.create_flow_session(
        a, **_registry_kwargs({"a": make_pcm(441, 1)}))
    try:
        await collect(session)                 # natural end, fully drained
        assert session.ended and not session.closed
        async with _asgi_client() as client:
            r = await asyncio.wait_for(client.get(session.url_path), 10)
            assert r.status_code == 200
            assert r.content == b""
            assert r.headers["content-type"] == "audio/flac"
            assert r.headers["accept-ranges"] == "none"
    finally:
        await flow.close_flow_session()
