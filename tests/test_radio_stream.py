"""Cast/DLNA radio transcode-proxy: ingest → transcode → endless-serve (plan U5).

Everything runs on a FAKE ffmpeg spawner (no real encoder) and the suite's
``FakeTimerFactory`` (no real sleeps — repo pytest-hang policy). The tests assert
the LIFECYCLE the plan calls out: the capability-URL 404-before-any-spawn posture
(SEC-001), the reap-before-respawn / stop-teardown discipline (ADV-4), Range-less
chunked serving, reconnect-on-drop with no byte offset, transcode-output
content-type, and single-consumer 409 / grace re-bind.

Device-level playback + the exact codec target are rig-validated (headless can't
drive a real Cast/DLNA receiver or ffmpeg).
"""

import asyncio

import httpx
import pytest

from app.radio import stream as radio_stream
from tests.conftest import FakeTimerFactory

# A documentation-range example host — never a real IP (credential/PII hygiene).
STATION_URL = "http://stream.example.com/jazz"


# ── fake ffmpeg subprocess (the injectable spawn seam) ────────────────────────


class FakeProc:
    """A stand-in for an ffmpeg subprocess: stdout yields queued chunks; the test
    controls EOF; terminate()/kill() record the reap and unblock a parked read."""

    def __init__(self, chunks=None):
        self.returncode = None
        self.terminated = False
        self.killed = False
        self._q: asyncio.Queue = asyncio.Queue()
        self._eof = False
        for c in (chunks or []):
            self._q.put_nowait(c)
        self.stdout = self._Stdout(self)
        self.stderr = None  # no stderr drain in tests (keeps the lifecycle clean)

    def feed(self, chunk: bytes) -> None:
        self._q.put_nowait(chunk)

    def eof(self) -> None:
        self._eof = True
        self._q.put_nowait(b"")   # wake a parked read → returns b"" (EOF)

    class _Stdout:
        def __init__(self, proc):
            self._proc = proc

        async def read(self, n: int) -> bytes:
            p = self._proc
            if p.returncode is not None:
                return b""
            chunk = await p._q.get()
            return chunk

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self._q.put_nowait(b"")   # unblock any parked read

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self._q.put_nowait(b"")

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(0)
        return self.returncode


class SpawnRecorder:
    """The injectable ``ffmpeg_spawn`` seam: records every spawn's argv and hands
    back a FakeProc so the test can drive/inspect it. ``procs`` is spawn order."""

    def __init__(self):
        self.args: list[list[str]] = []
        self.procs: list[FakeProc] = []
        self._pending: list[FakeProc] = []

    def queue(self, proc: FakeProc) -> None:
        """Pre-seed the FakeProc a subsequent spawn should return."""
        self._pending.append(proc)

    async def __call__(self, args: list[str]) -> FakeProc:
        self.args.append(args)
        proc = self._pending.pop(0) if self._pending else FakeProc()
        self.procs.append(proc)
        return proc


@pytest.fixture(autouse=True)
def _radio_registry_reset():
    radio_stream._current_session = None
    yield
    radio_stream._current_session = None


async def settle(ticks: int = 60) -> None:
    for _ in range(ticks):
        await asyncio.sleep(0)


def _asgi_client():
    from app.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")


# ══════════════════════════════════════════════════════════════════════════════
# Session lifecycle (mock ffmpeg — no real encoder)
# ══════════════════════════════════════════════════════════════════════════════


async def test_start_spawns_encoder_with_ingest_url_and_serves_chunks():
    """Happy path: the session spawns one ffmpeg ingesting the validated final
    URL and the bound consumer receives the transcoded bytes."""
    spawn = SpawnRecorder()
    proc = FakeProc([b"ID3aud", b"iodata"])
    spawn.queue(proc)
    sess = await radio_stream.start_radio_stream(
        STATION_URL, ffmpeg_spawn=spawn, timer_factory=FakeTimerFactory())
    try:
        await settle()
        assert len(spawn.args) == 1                 # one encoder spawned
        assert spawn.args[0][0] == "ffmpeg"
        assert spawn.args[0][spawn.args[0].index("-i") + 1] == STATION_URL
        assert spawn.args[0][-1] == "pipe:1"

        g = sess.bind_consumer()
        c1 = await asyncio.wait_for(g.__anext__(), 5)
        c2 = await asyncio.wait_for(g.__anext__(), 5)
        assert c1 + c2 == b"ID3audiodata"
        await g.aclose()
    finally:
        await radio_stream.close_radio_stream()


async def test_content_type_derives_from_transcode_output_not_url():
    """Edge: content-type is the transcode OUTPUT type (mp3 → audio/mpeg), never
    the station URL extension (the URL here ends in .ogg — which would wrongly be
    audio/ogg if derived from the extension)."""
    sess = await radio_stream.start_radio_stream(
        "http://stream.example.com/feed.ogg",
        ffmpeg_spawn=SpawnRecorder(), timer_factory=FakeTimerFactory())
    try:
        assert sess.content_type == "audio/mpeg"          # mp3 default output
        assert radio_stream.radio_output_content_type("aac") == "audio/aac"
        assert radio_stream.radio_output_content_type("mp3") == "audio/mpeg"
        # Unknown format → last-resort audio/mpeg (never crashes / never a URL type).
        assert radio_stream.radio_output_content_type("zzz") == "audio/mpeg"
    finally:
        await radio_stream.close_radio_stream()


async def test_instant_switch_reaps_prior_encoder_before_new_spawn():
    """ADV-4: minting a new session (instant switch) supersedes + reaps the prior
    ffmpeg — no stacked/orphaned encoders. The old session is closed and its proc
    terminated; a fresh session_id invalidates the old capability URL."""
    spawn = SpawnRecorder()
    old_proc = FakeProc()
    spawn.queue(old_proc)
    old = await radio_stream.start_radio_stream(
        STATION_URL, ffmpeg_spawn=spawn, timer_factory=FakeTimerFactory())
    await settle()
    assert old_proc.terminated is False

    new = await radio_stream.start_radio_stream(
        "http://stream.example.com/rock", ffmpeg_spawn=spawn,
        timer_factory=FakeTimerFactory())
    await settle()

    assert new.session_id != old.session_id           # id rotated (stale URL 404s)
    assert old.closed is True                          # prior session torn down
    assert old_proc.terminated is True                 # prior ffmpeg reaped
    assert radio_stream.current_radio_stream() is new
    await radio_stream.close_radio_stream()
    assert new.closed is True


async def test_stop_tears_down_encoder_and_releases_bind():
    """ADV-4: stop (close) reaps the encoder AND releases the consumer bind — no
    orphan process, and a subsequent bind on the closed session is rejected."""
    spawn = SpawnRecorder()
    proc = FakeProc([b"aa"])
    spawn.queue(proc)
    sess = await radio_stream.start_radio_stream(
        STATION_URL, ffmpeg_spawn=spawn, timer_factory=FakeTimerFactory())
    await settle()
    g = sess.bind_consumer()
    await asyncio.wait_for(g.__anext__(), 5)
    assert sess._consumer_bound is True

    await radio_stream.close_radio_stream()

    assert sess.closed is True
    assert proc.terminated is True                      # encoder reaped
    # The bound consumer terminates (no forever-park) and the bind is released.
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(g.__anext__(), 5)
    with pytest.raises(RuntimeError):
        sess.bind_consumer()                            # closed → no re-bind


async def test_upstream_drop_midbody_reconnects_no_byte_offset():
    """Error path: an encoder EOF mid-body (upstream drop) respawns ffmpeg against
    the SAME final URL from 'now' — no byte-offset Range in the argv (a live
    stream has no byte-addressable past; never a _stream_passthrough resume)."""
    spawn = SpawnRecorder()
    p1 = FakeProc([b"before"])
    p2 = FakeProc([b"after"])
    spawn.queue(p1)
    spawn.queue(p2)
    sess = await radio_stream.start_radio_stream(
        STATION_URL, ffmpeg_spawn=spawn, timer_factory=FakeTimerFactory())
    try:
        g = sess.bind_consumer()
        assert await asyncio.wait_for(g.__anext__(), 5) == b"before"
        p1.eof()                                        # upstream drop
        assert await asyncio.wait_for(g.__anext__(), 5) == b"after"
        await g.aclose()

        assert sess.reconnects == 1
        assert len(spawn.args) == 2                     # respawned once
        # No byte offset anywhere in the reconnect argv (no Range / -ss seek).
        assert "-ss" not in spawn.args[1]
        assert not any("Range" in a or "bytes=" in a for a in spawn.args[1])
        # Same ingest URL, re-opened from now.
        assert spawn.args[1][spawn.args[1].index("-i") + 1] == STATION_URL
    finally:
        await radio_stream.close_radio_stream()


async def test_reconnect_budget_bounds_a_dead_upstream():
    """A permanently-dead upstream (every spawn EOFs immediately) trips the
    bounded reconnect budget and the pump ends rather than respawning forever."""
    spawn = SpawnRecorder()
    # Every proc EOFs at once — the pump keeps reconnecting until the budget caps.
    for _ in range(radio_stream._RADIO_STREAM_RECONNECT_MAX + 3):
        p = FakeProc()
        p.eof()
        spawn.queue(p)
    sess = await radio_stream.start_radio_stream(
        STATION_URL, ffmpeg_spawn=spawn, timer_factory=FakeTimerFactory())
    try:
        await settle(200)
        # Initial spawn + exactly the bounded budget of reconnect spawns.
        assert sess.reconnects == radio_stream._RADIO_STREAM_RECONNECT_MAX + 1
        assert len(spawn.args) <= radio_stream._RADIO_STREAM_RECONNECT_MAX + 1
    finally:
        await radio_stream.close_radio_stream()


async def test_f7_watchdog_kills_encoder_with_no_first_byte_even_unbound(
        monkeypatch):
    """F7: a wedged upstream that never emits a single output byte is killed by
    the watchdog EVEN WITH NO CONSUMER BOUND — the encoder can't leak forever
    waiting for a device that may never bind. Driven by a fake clock so no real
    sleeps (repo pytest-hang policy)."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(radio_stream, "_now", lambda: clock["t"])

    sess = radio_stream.RadioStreamSession(
        STATION_URL, ffmpeg_spawn=SpawnRecorder(),
        timer_factory=FakeTimerFactory())

    # A proc that parks forever (never yields a byte).
    proc = FakeProc()                                # empty queue → read() parks
    sess._proc = proc
    sess._spawned_at = clock["t"]
    sess._produced_output = False
    # No consumer bound (the wedged-before-bind case).
    assert sess._consumer_bound is False

    # Patch the watchdog's sleep to advance the clock past the first-byte bound,
    # then stop the loop after one effective tick.
    ticks = {"n": 0}

    async def _fake_sleep(_delay):
        ticks["n"] += 1
        clock["t"] += radio_stream.RADIO_FIRST_BYTE_S + 1
        if ticks["n"] >= 2:
            sess._closed = True                      # end the watchdog loop

    monkeypatch.setattr(radio_stream.asyncio, "sleep", _fake_sleep)
    await sess._watchdog()

    assert proc.terminated is True, \
        "the watchdog killed the wedged encoder with no first byte (F7)"


# ══════════════════════════════════════════════════════════════════════════════
# The route: capability URL, Range-less, single-consumer (SEC-001 + serving)
# ══════════════════════════════════════════════════════════════════════════════


async def test_route_unknown_session_404_without_spawning(monkeypatch):
    """SEC-001: an unknown/stale session_id 404s and NEVER initiates an upstream
    fetch/spawn. We assert no ffmpeg is spawned by trapping create_subprocess_exec
    (the real spawn seam) and confirming it is never called."""
    spawned = {"n": 0}

    async def _trap(*a, **k):
        spawned["n"] += 1
        raise AssertionError("route spawned ffmpeg for an unknown session")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _trap)

    async with _asgi_client() as client:
        r = await asyncio.wait_for(
            client.get("/api/radio/stream/not-a-real-session"), 10)
    assert r.status_code == 404
    assert spawned["n"] == 0                            # no upstream work done
    assert radio_stream.current_radio_stream() is None  # nothing minted


async def test_route_serves_chunked_rangeless_with_output_content_type():
    """Happy path: the route serves a chunked, Range-less response
    (Accept-Ranges: none, no-store) with the transcode-output content-type."""
    spawn = SpawnRecorder()
    proc = FakeProc([b"chunk1", b"chunk2"])
    proc.eof()                                          # finite fixture body
    spawn.queue(proc)
    # Reconnect procs also EOF at once so the ingest ends (budget exhausts) and the
    # route body terminates for the test — production radio never ends this way.
    for _ in range(radio_stream._RADIO_STREAM_RECONNECT_MAX + 1):
        p = FakeProc()
        p.eof()
        spawn.queue(p)
    sess = await radio_stream.start_radio_stream(
        STATION_URL, ffmpeg_spawn=spawn, timer_factory=FakeTimerFactory())
    try:
        async with _asgi_client() as client:
            r = await asyncio.wait_for(client.get(sess.url_path), 10)
            assert r.status_code == 200
            assert r.headers["content-type"] == "audio/mpeg"   # transcode output
            assert r.headers["accept-ranges"] == "none"        # Range-less live
            assert r.headers["cache-control"] == "no-store"
            assert "content-length" not in r.headers           # endless/chunked
            assert b"chunk1" in r.content and b"chunk2" in r.content
    finally:
        await radio_stream.close_radio_stream()


async def test_route_ignores_range_header_serves_full_stream():
    """Behavior: a Range header on the request is ignored — the full stream is
    served (Accept-Ranges: none; 200, never a 206)."""
    spawn = SpawnRecorder()
    proc = FakeProc([b"fullbody"])
    proc.eof()
    spawn.queue(proc)
    for _ in range(radio_stream._RADIO_STREAM_RECONNECT_MAX + 1):
        p = FakeProc()
        p.eof()
        spawn.queue(p)
    sess = await radio_stream.start_radio_stream(
        STATION_URL, ffmpeg_spawn=spawn, timer_factory=FakeTimerFactory())
    try:
        async with _asgi_client() as client:
            r = await asyncio.wait_for(
                client.get(sess.url_path, headers={"Range": "bytes=2-"}), 10)
            assert r.status_code == 200                 # not 206
            assert r.headers["accept-ranges"] == "none"
            assert r.content == b"fullbody"             # from byte 0, ignored Range
    finally:
        await radio_stream.close_radio_stream()


async def test_route_second_concurrent_bind_409_and_regrace_rebinds():
    """Integration: a second concurrent bind is rejected 409 while the first
    holds; a re-GET within the grace window re-binds the SAME session."""
    spawn = SpawnRecorder()
    timers = FakeTimerFactory()
    proc = FakeProc([b"aa"])                            # 1 chunk, then parks (no eof)
    spawn.queue(proc)
    sess = await radio_stream.start_radio_stream(
        STATION_URL, ffmpeg_spawn=spawn, timer_factory=timers)
    try:
        g1 = sess.bind_consumer()                       # the device's stream
        await asyncio.wait_for(g1.__anext__(), 5)

        # A second concurrent GET while g1 is bound → 409.
        async with _asgi_client() as client:
            r = await asyncio.wait_for(client.get(sess.url_path), 10)
            assert r.status_code == 409
        assert not sess.closed

        # The device hiccups: g1 disconnects → grace armed.
        await g1.aclose()
        assert len(timers.timers) == 1
        assert timers.timers[0].delay == radio_stream.RADIO_CONSUMER_GRACE_S

        # A re-GET within grace re-binds the SAME session (grace cancelled).
        g2 = sess.bind_consumer()
        assert timers.timers[0].cancelled is True
        await g2.aclose()
    finally:
        await radio_stream.close_radio_stream()


async def test_grace_expiry_fires_consumer_gone_hook():
    """A disconnect with no re-bind within grace notifies the consumer-gone hook
    (the session itself stays open — the hook owner decides)."""
    spawn = SpawnRecorder()
    timers = FakeTimerFactory()
    proc = FakeProc([b"aa"])
    spawn.queue(proc)
    sess = await radio_stream.start_radio_stream(
        STATION_URL, ffmpeg_spawn=spawn, timer_factory=timers)
    gone = {"n": 0}
    sess.add_consumer_gone_listener(lambda: gone.__setitem__("n", gone["n"] + 1))
    try:
        g1 = sess.bind_consumer()
        await asyncio.wait_for(g1.__anext__(), 5)
        await g1.aclose()
        timers.timers[0].fire()                         # grace elapses
        assert gone["n"] == 1
        assert not sess.closed
    finally:
        await radio_stream.close_radio_stream()


async def test_f3_minted_session_gets_default_consumer_gone_listener():
    """F3: start_radio_stream attaches the registered DEFAULT consumer-gone
    callback to every newly-minted per-station session (the boot-time one-shot
    attach against current_radio_stream() — None at boot — never covered them),
    and grace-expiry fires the offline transition through it."""
    fired = {"n": 0}
    radio_stream.set_default_consumer_gone_callback(
        lambda: fired.__setitem__("n", fired["n"] + 1))
    try:
        spawn = SpawnRecorder()
        timers = FakeTimerFactory()
        spawn.queue(FakeProc([b"aa"]))
        sess = await radio_stream.start_radio_stream(
            STATION_URL, ffmpeg_spawn=spawn, timer_factory=timers)
        try:
            g1 = sess.bind_consumer()
            await asyncio.wait_for(g1.__anext__(), 5)
            await g1.aclose()                           # consumer gone → grace armed
            assert timers.timers, "a disconnect arms the grace timer"
            timers.timers[0].fire()                     # grace elapses → offline
            assert fired["n"] == 1, \
                "grace-expiry fires the DEFAULT consumer-gone listener (F3)"
        finally:
            await radio_stream.close_radio_stream()
    finally:
        radio_stream.set_default_consumer_gone_callback(None)


async def test_f4_switch_reaps_old_before_new_spawn():
    """F4: on an instant switch, start_radio_stream awaits the prior session's
    ffmpeg reap BEFORE spawning the new encoder — the old proc is terminated by
    the time the new spawn's argv is recorded (no overlapping/stacked encoders)."""
    spawn = SpawnRecorder()
    old_proc = FakeProc([b"old"])
    spawn.queue(old_proc)
    old = await radio_stream.start_radio_stream(
        STATION_URL, ffmpeg_spawn=spawn, timer_factory=FakeTimerFactory())
    await settle()
    assert len(spawn.args) == 1            # only the old encoder spawned so far
    assert old_proc.terminated is False

    # Switch: the new mint must reap old BEFORE the new spawn.
    new = await radio_stream.start_radio_stream(
        "http://stream.example.com/rock", ffmpeg_spawn=spawn,
        timer_factory=FakeTimerFactory())
    # By the time start_radio_stream RETURNS, the old proc is already terminated
    # (awaited reap) — and the new session is registered + started.
    assert old.closed is True
    assert old_proc.terminated is True, "old ffmpeg reaped before the new spawn"
    assert new.session_id != old.session_id
    assert radio_stream.current_radio_stream() is new
    await settle()
    assert len(spawn.args) == 2            # new encoder spawned after the reap
    await radio_stream.close_radio_stream()


async def test_ffmpeg_argv_is_icy_tolerant_and_transcodes():
    """Edge (ICY 200 OK): ingest is `ffmpeg -i <url>` (tolerant of the ICY status
    line, unlike a plain httpx GET), re-encoding to the Cast/DLNA-safe codec on a
    pipe — the pure-arg pin proving the ingest path is the tolerant one."""
    args = radio_stream._build_radio_ffmpeg_args(
        STATION_URL, "mp3", radio_stream.RADIO_ENCODE_BITRATE)
    assert args[0] == "ffmpeg"
    assert args[args.index("-i") + 1] == STATION_URL    # ffmpeg ingests the URL
    assert args[args.index("-acodec") + 1] == "libmp3lame"
    assert args[args.index("-f") + 1] == "mp3"
    assert args[-1] == "pipe:1"                          # endless pipe, unknown dur
    assert "-vn" in args                                 # drop embedded art
    # No byte-offset / Range machinery — this is a live ingest, not a resume.
    assert "-ss" not in args


def test_redact_scrubs_station_url_query():
    """A station URL query (a token-bearing station link) is scrubbed before any
    log — the flow._redact posture, re-implemented locally."""
    assert radio_stream._redact(
        "open http://stream.example.com/s?auth=SECRET failed"
    ) == "open http://stream.example.com/s?<redacted> failed"
    assert radio_stream._redact("no urls here") == "no urls here"


# ══════════════════════════════════════════════════════════════════════════════
# Cast/DLNA wiring: radio routes through the proxy capability URL (not raw)
# ══════════════════════════════════════════════════════════════════════════════


async def test_radio_proxy_url_builds_capability_url_and_mints_session(monkeypatch):
    """radio_proxy_url (the single Cast/DLNA seam) mints a proxy session and
    returns the device-facing capability URL + transcode-output content-type."""
    from app.output import radio_endless

    monkeypatch.setattr("app.state._stream_url_base", lambda: "http://10.0.0.5")
    monkeypatch.setattr(radio_stream, "_default_ffmpeg_spawn", SpawnRecorder())
    try:
        result = await radio_endless.radio_proxy_url(STATION_URL)
        assert result is not None
        url, ctype = result
        sess = radio_stream.current_radio_stream()
        assert sess is not None
        assert url == "http://10.0.0.5" + sess.url_path
        assert url.startswith("http://10.0.0.5/api/radio/stream/")
        assert ctype == "audio/mpeg"                    # transcode output type
    finally:
        await radio_stream.close_radio_stream()


async def test_radio_proxy_url_none_without_reachable_base(monkeypatch):
    """No STREAM_BASE_URL / specific BIND_HOST → None (the caller degrades to the
    direct station URL, mirroring the Cast flow _flow_base_url fallback). No proxy
    session is minted / no ffmpeg spawned."""
    from app.output import radio_endless

    monkeypatch.setattr("app.state._stream_url_base", lambda: "")
    assert await radio_endless.radio_proxy_url(STATION_URL) is None
    assert radio_stream.current_radio_stream() is None
