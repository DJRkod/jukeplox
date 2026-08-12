"""Cast/DLNA radio transcode-proxy — endless ICY-ingest → transcode → serve (U5).

Why this exists (Key Technical Decision, radio plan): a station's stream is an
arbitrary third-party codec/container (Ogg, AAC-in-ADTS, MP3, sometimes a raw
``ICY 200 OK`` SHOUTcast handshake that breaks httpx/h11). GStreamer-direct and
AirPlay ingest such a stream *host-side* and tolerate all of it natively, so they
connect to the SSRF-validated ``url_resolved`` directly and need no proxy. **Cast
and DLNA** would otherwise fetch the URL *themselves* — a device that can hang for
minutes on Ogg (the JBL/Ogg bug) and a fetch we cannot keep under server-side SSRF
control. So for those two backends the jukebox ingests the validated final URL via
ffmpeg (ICY-tolerant), transcodes to a Cast/DLNA-safe codec, and serves it back
Range-less behind a capability URL.

This module owns its OWN ffmpeg lifecycle + single-consumer registry. It borrows
flow.py's serving POSTURE (Range-less chunked, ``bind_consumer`` + grace re-bind,
409 on a second concurrent consumer, ``_redact`` credential hygiene, SIGTERM→SIGKILL
reap) but is DELIBERATELY not coupled to the gapless ``FlowSession`` (RR-02): there
is no decode→stitch→re-encode queue pipeline here — just one long-lived
``ffmpeg -i <url>`` re-encoding the live station into one endless stream.

Capability-URL posture (SEC-001), copied from the flow route: ``session_id`` is an
unguessable ``secrets.token_urlsafe(16)`` minted per radio station start; the id IS
the credential. The route 404s on any unknown/stale id WITHOUT fetching anything
upstream, so it is never an open proxy. The id is invalidated on station
switch/stop, so a stale device cannot re-bind onto a new station.

Subprocess lifecycle (ADV-4): the ffmpeg encoder runs for the whole station
lifetime. The module-level registry supersedes the previous session (reaping its
ffmpeg) BEFORE the new one is registered, so rapid station flipping can never stack
orphaned encoders — the project's recurring process-hygiene failure class. Stop
tears down the encoder and releases the consumer bind; a stall watchdog kills a
wedged upstream.

Every subprocess seam is injectable (``ffmpeg_spawn``) so tests assert the
spawn/terminate/reap lifecycle without a real ffmpeg or real network.
"""

from __future__ import annotations

import asyncio
import collections
import inspect
import logging
import re
import secrets
from typing import Any, AsyncIterator, Awaitable, Callable

_log = logging.getLogger("jukeplox.radio")

# ── transcode target (rig-tunable; content-type derives from THIS, never the URL)
#
# mp3 CBR 192k is the Cast/DLNA "common-denominator" codec: universally decoded by
# constrained receivers, no Ogg-style multi-minute hang, and a fixed content-type
# we can advertise authoritatively. The exact codec/bitrate is deferred to rig
# validation (plan "Deferred to Implementation"); it lives here as one named
# constant so it can be re-tuned without touching the pipeline.
RADIO_ENCODE_FORMAT = "mp3"
RADIO_ENCODE_BITRATE = "192k"

# The content-type we advertise for the transcode OUTPUT. Authoritative — the Cast/
# DLNA LOAD rejects a wrong content-type BEFORE fetching, and for a transcoded
# stream the served bytes are OURS, not the upstream's (inverts the file-extension
# rule; cast-dlna-flac-content-type learning). audio/mpeg is the last-resort
# fallback if an unknown format is ever configured.
_OUTPUT_CONTENT_TYPES = {"mp3": "audio/mpeg", "aac": "audio/aac"}
_FALLBACK_CONTENT_TYPE = "audio/mpeg"

# Consumer-disconnect grace (flow.FLOW_CONSUMER_GRACE_S shape): a new GET within
# this window re-binds the SAME session; expiry notifies the consumer-gone hook.
RADIO_CONSUMER_GRACE_S = 5.0

# Encoder stall watchdog (flow.FLOW_DECODE_STALL_S shape): the encoder producing
# zero output for this long while a consumer is bound is a wedged upstream — kill
# ffmpeg so a reconnect can re-open, rather than serving silence forever.
RADIO_ENCODE_STALL_S = 30.0

# F7: first-byte liveness bound. A silent/wedged upstream that never emits a
# single output byte (ffmpeg connected but the source dribbles nothing) would leak
# a live encoder forever if the watchdog only ran while a consumer was bound (a
# Cast/DLNA device may never bind if the LOAD itself is what wedged). Independent
# of consumer-bind: if the encoder produces ZERO bytes within this window of a
# (re)spawn, kill it so the pump's EOF path reconnects / exhausts the budget →
# offline, rather than holding a wedged ffmpeg indefinitely.
RADIO_FIRST_BYTE_S = 20.0

# SIGTERM grace before SIGKILL on teardown (airplay.py / flow.py convention).
_STOP_GRACE_S = 2.0

_OUT_READ_CHUNK = 65536

# Any URL query string is treated as credential-bearing and scrubbed before
# logging (flow._redact shape). A station URL can legitimately carry a token query.
_URL_QUERY_RE = re.compile(r"(https?://[^\s'\"?]+)\?[^\s'\"]*")


def _redact(text: Any) -> str:
    """Scrub credential-bearing URL query strings from log-bound text."""
    return _URL_QUERY_RE.sub(r"\1?<redacted>", str(text))


def radio_output_content_type(encode_format: str = RADIO_ENCODE_FORMAT) -> str:
    """The content-type a Cast/DLNA renderer will actually receive for a radio
    stream served by this proxy — the transcode OUTPUT type (authoritative),
    ``audio/mpeg`` last-resort fallback. NEVER derived from the station URL's
    extension (the served bytes are the transcode's, not the upstream's)."""
    return _OUTPUT_CONTENT_TYPES.get(encode_format, _FALLBACK_CONTENT_TYPE)


class RadioConsumerConflict(Exception):
    """A second concurrent consumer tried to bind the single-session stream
    (the route's 409)."""


def _build_radio_ffmpeg_args(url: str, encode_format: str,
                             bitrate: str) -> list[str]:
    """ffmpeg argv ingesting the (already redirect-resolved, SSRF-validated) final
    station URL and streaming the re-encoded audio on stdout.

    ``ffmpeg -i <url>`` is ICY-tolerant: it accepts ``ICY 200 OK`` and sniffs the
    container/codec, unlike a plain httpx GET (which h11 rejects at the status
    line). ``-vn`` drops any embedded cover art. A pipe output means unknown
    duration — exactly right for an endless live stream.

    Credential note: a station URL is an arbitrary public host chosen by the
    directory. It does NOT carry Jukeplox provider credentials (radio is
    anonymous), so — unlike the flow decoder — the URL is passed on argv, which is
    the only way to hand ffmpeg an HTTP source it must ICY-negotiate itself. Its
    query is still scrubbed everywhere it reaches a log via :func:`_redact`.
    """
    codec = "libmp3lame" if encode_format == "mp3" else "aac"
    fmt = "mp3" if encode_format == "mp3" else "adts"
    return [
        "ffmpeg",
        "-nostdin",
        "-loglevel", "error",
        "-i", url,
        "-vn",
        "-acodec", codec,
        "-b:a", bitrate,
        "-f", fmt,
        "pipe:1",
    ]


async def _terminate_proc(proc: Any, label: str) -> None:
    """SIGTERM with grace, then SIGKILL (flow.py / airplay.py teardown). Tolerates
    None and already-exited processes."""
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.terminate()
    except (ProcessLookupError, OSError):
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=_STOP_GRACE_S)
    except asyncio.TimeoutError:
        _log.warning("Radio teardown: %s did not exit on SIGTERM within %.1fs "
                     "— sending SIGKILL", label, _STOP_GRACE_S)
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=_STOP_GRACE_S)
        except asyncio.TimeoutError:
            _log.error("Radio teardown: %s ignored SIGKILL", label)


async def _drain_stderr(stream: Any, label: str) -> None:
    """Drain ffmpeg stderr so its pipe can't back-pressure, redacting URL queries
    (flow._drain_stderr shape). With ``-loglevel error`` anything here is worth a
    WARNING."""
    try:
        while True:
            line_bytes = await stream.readline()
            if not line_bytes:
                return
            line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
            _log.warning("Radio %s stderr: %s", label, _redact(line))
    except asyncio.CancelledError:
        return
    except Exception:
        _log.warning("Radio %s stderr reader crashed", label, exc_info=True)


async def _default_ffmpeg_spawn(args: list[str]) -> Any:
    """Spawn ffmpeg (the production subprocess seam). Injectable so tests assert
    the lifecycle without a real encoder."""
    return await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


class RadioStreamSession:
    """One radio transcode-proxy playback: ingest the validated final URL via a
    long-lived ffmpeg encoder and serve the re-encoded bytes to a single Cast/DLNA
    consumer, Range-less and endless.

    Lifecycle (ADV-4):
    - :meth:`start` spawns the encoder + reader + stall watchdog.
    - :meth:`bind_consumer` binds the ONE consumer (409 on a second); a disconnect
      arms a grace timer and a re-GET within it re-binds the SAME encoder.
    - a mid-stream encoder EOF/death → :meth:`_reconnect` respawns ffmpeg against
      the same URL with NO byte offset (a live stream has no byte-addressable past;
      never a ``_stream_passthrough``-style resume).
    - :meth:`close` reaps the encoder, cancels the reader/watchdog/grace, and wakes
      every waiter so the consumer terminates. Idempotent.
    """

    def __init__(
        self,
        final_url: str,
        *,
        session_id: str | None = None,
        encode_format: str = RADIO_ENCODE_FORMAT,
        bitrate: str = RADIO_ENCODE_BITRATE,
        ffmpeg_spawn: Callable[[list[str]], Awaitable[Any]] | None = None,
        grace_s: float = RADIO_CONSUMER_GRACE_S,
        stall_s: float = RADIO_ENCODE_STALL_S,
        timer_factory: Callable[[float, Callable[[], None]], Any] | None = None,
    ) -> None:
        self._final_url = final_url
        self._session_id = session_id or secrets.token_urlsafe(16)
        self._encode_format = encode_format
        self._bitrate = bitrate
        self._ffmpeg_spawn = ffmpeg_spawn or _default_ffmpeg_spawn
        self._grace_s = grace_s
        self._stall_s = stall_s
        self._timer_factory = timer_factory or _default_timer_factory

        self._proc: Any = None
        self._stderr_task: asyncio.Task | None = None
        self._reader_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None

        self._started = False
        self._closed = False
        # Set when the ingest pump exits without close() (clean encoder end or
        # reconnect budget exhausted). The consumer then drains the tail and
        # terminates instead of parking forever — R12 "never indefinite silence":
        # the device re-GETs / the backend surfaces offline rather than hearing an
        # eternal silent stream.
        self._ingest_ended = False

        # out-buffer (encoder → consumer). Bounded so a lagging/absent consumer
        # can't grow memory without limit; the reader parks when it's full.
        self._out_chunks: collections.deque[bytes] = collections.deque()
        self._out_bytes = 0
        self._out_cap = max(_OUT_READ_CHUNK, int(self._grace_s + 4) * 128 * 1024)
        self._out_data = asyncio.Event()
        self._out_space = asyncio.Event()
        self._out_space.set()

        # consumer binding / grace (flow posture).
        self._consumer_bound = False
        self._bind_seq = 0
        self._grace_handle: Any = None
        self._consumer_gone_listeners: list[Callable[[], Any]] = []

        # stall watchdog: last time the encoder produced output.
        self._last_output_at: float = 0.0
        # F7: first-byte liveness — when the current encoder was (re)spawned and
        # whether it has produced ANY output byte yet. A wedged upstream that never
        # emits a byte is killed by the watchdog even with no consumer bound.
        self._spawned_at: float = 0.0
        self._produced_output: bool = False

        # reconnect bookkeeping: a live-stream drop respawns from "now", never a
        # byte offset. Bounded so a permanently-dead upstream self-terminates
        # rather than respawning ffmpeg forever.
        self._reconnects = 0

    # ── identity ────────────────────────────────────────────────────────────────

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def url_path(self) -> str:
        """The capability route path for this session (Cast/DLNA compose the
        absolute device-facing URL with ``state._stream_url_base``)."""
        return f"/api/radio/stream/{self._session_id}"

    @property
    def content_type(self) -> str:
        return radio_output_content_type(self._encode_format)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def final_url(self) -> str:
        return self._final_url

    @property
    def reconnects(self) -> int:
        return self._reconnects

    def add_consumer_gone_listener(self, cb: Callable[[], Any]) -> None:
        """``cb()`` when the disconnect grace expires with no re-bind."""
        self._consumer_gone_listeners.append(cb)

    # ── lifecycle ─────────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the encoder + reader + watchdog (requires a running loop).
        Idempotent."""
        if self._started or self._closed:
            return
        self._started = True
        loop = asyncio.get_running_loop()
        self._reader_task = loop.create_task(self._run())
        self._reader_task.add_done_callback(_log_task_exc)

    async def _spawn_encoder(self) -> None:
        args = _build_radio_ffmpeg_args(self._final_url, self._encode_format,
                                        self._bitrate)
        _log.info("Radio session %s: spawning encoder for %s",
                  self._session_id, _redact(self._final_url))
        self._proc = await self._ffmpeg_spawn(args)
        stderr = getattr(self._proc, "stderr", None)
        if stderr is not None:
            self._stderr_task = asyncio.create_task(
                _drain_stderr(stderr, "encode"))
        self._last_output_at = _now()
        # F7: mark the (re)spawn time and reset the first-byte flag so the watchdog
        # can kill a wedged upstream that never emits a single output byte, even
        # with no consumer bound.
        self._spawned_at = _now()
        self._produced_output = False

    async def _run(self) -> None:
        """Spawn the encoder, arm the stall watchdog, and pump encoded bytes into
        the out-buffer. On a mid-stream encoder drop, reconnect (respawn from now)
        rather than advancing or going silent (R12)."""
        try:
            await self._spawn_encoder()
            self._watchdog_task = asyncio.get_running_loop().create_task(
                self._watchdog())
            self._watchdog_task.add_done_callback(_log_task_exc)
            while not self._closed:
                chunk = await self._proc.stdout.read(_OUT_READ_CHUNK)
                if not chunk:
                    # Encoder EOF: upstream ended/dropped. A live stream never
                    # ends cleanly — respawn from "now", bounded.
                    if self._closed:
                        return
                    if not await self._reconnect():
                        return
                    continue
                self._last_output_at = _now()
                self._produced_output = True  # F7: first byte seen
                await self._push(chunk)
        except asyncio.CancelledError:
            raise
        except Exception:
            if self._closed:
                return
            _log.exception("Radio session %s: pump crashed — closing",
                           self._session_id)
            await self.close()
        finally:
            # The pump exited (clean end / budget exhausted / crash-then-close):
            # wake the consumer so it drains the tail and terminates rather than
            # parking forever on a stream that will never produce more bytes.
            self._ingest_ended = True
            self._out_data.set()

    async def _reconnect(self) -> bool:
        """Respawn ffmpeg against the SAME final URL from "now" (no byte offset).
        Returns True if a fresh encoder is running, False once the bounded
        reconnect budget is exhausted (⇒ the pump ends; U4/U7 own the offline
        state on the backend side)."""
        self._reconnects += 1
        _log.info("Radio session %s: encoder ended — reconnecting (attempt %d)",
                  self._session_id, self._reconnects)
        await _terminate_proc(self._proc, "encode-ffmpeg")
        await self._cancel_stderr()
        if self._reconnects > _RADIO_STREAM_RECONNECT_MAX:
            _log.warning("Radio session %s: reconnect budget exhausted — ending",
                         self._session_id)
            return False
        try:
            await self._spawn_encoder()
        except Exception:
            _log.warning("Radio session %s: reconnect spawn failed",
                         self._session_id, exc_info=True)
            return False
        return True

    async def _watchdog(self) -> None:
        """Kill a wedged encoder so the pump's EOF path reconnects (F7).

        Two independent triggers:
        - **first-byte liveness (F7):** if the encoder has produced ZERO output
          bytes within :data:`RADIO_FIRST_BYTE_S` of its (re)spawn, kill it —
          even with NO consumer bound. A silent/wedged upstream (device never
          binds because the LOAD itself wedged) would otherwise leak a live
          ffmpeg forever.
        - **stall (while serving):** once bytes have flowed, a bound consumer
          seeing zero new output for ``stall_s`` is a wedged upstream — kill to
          force a reconnect. Only while a consumer is bound (an idle, unbound
          session that already produced its first byte legitimately parks)."""
        try:
            while not self._closed:
                await asyncio.sleep(min(self._stall_s, 5.0))
                if self._closed:
                    continue
                # First-byte liveness: applies regardless of consumer bind.
                if (not self._produced_output
                        and self._spawned_at > 0.0
                        and _now() - self._spawned_at >= RADIO_FIRST_BYTE_S):
                    _log.warning("Radio session %s: no first byte within %.0fs of "
                                 "spawn — killing wedged encoder",
                                 self._session_id, RADIO_FIRST_BYTE_S)
                    self._spawned_at = _now()  # avoid a kill storm before respawn
                    await _terminate_proc(self._proc,
                                          "encode-ffmpeg (no first byte)")
                    continue
                if not self._consumer_bound:
                    continue
                if _now() - self._last_output_at >= self._stall_s:
                    _log.warning("Radio session %s: encoder stalled %.0fs — "
                                 "killing to force reconnect",
                                 self._session_id, self._stall_s)
                    self._last_output_at = _now()  # avoid a kill storm
                    await _terminate_proc(self._proc, "encode-ffmpeg (stalled)")
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning("Radio session %s: watchdog crashed",
                         self._session_id, exc_info=True)

    async def _push(self, chunk: bytes) -> None:
        while (self._out_bytes + len(chunk) > self._out_cap
               and not self._closed):
            self._out_space.clear()
            await self._out_space.wait()
        if self._closed:
            return
        self._out_chunks.append(chunk)
        self._out_bytes += len(chunk)
        self._out_data.set()

    async def _cancel_stderr(self) -> None:
        if self._stderr_task is not None and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
        self._stderr_task = None

    async def close(self) -> None:
        """Idempotent teardown: kill/reap ffmpeg, cancel reader/watchdog/grace,
        wake every waiter so a bound consumer terminates."""
        if self._closed:
            return
        self._closed = True
        self._cancel_grace()
        self._out_data.set()
        self._out_space.set()
        current = asyncio.current_task()
        pending = [t for t in (self._reader_task, self._watchdog_task)
                   if t is not None and not t.done() and t is not current]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await _terminate_proc(self._proc, "encode-ffmpeg")
        await self._cancel_stderr()
        global _current_session
        if _current_session is self:
            _current_session = None
        _log.info("Radio session %s: closed", self._session_id)

    # ── consumer binding (the route's body) ───────────────────────────────────────

    def bind_consumer(self) -> AsyncIterator[bytes]:
        """Bind THE single consumer and return the chunk iterator. A second
        concurrent bind raises :class:`RadioConsumerConflict` (409). Re-binding
        within the disconnect grace cancels the grace timer and resumes from the
        current encode position (buffered chunks retained across the gap).

        The route MUST start the returned generator (prime the first chunk) so a
        never-started generator can't strand the binding — identical to the flow
        route's priming discipline."""
        if self._closed:
            raise RuntimeError("radio stream session is closed")
        if self._consumer_bound:
            raise RadioConsumerConflict(
                f"radio session {self._session_id} already has a consumer")
        self._consumer_bound = True
        self._cancel_grace()
        self._bind_seq += 1
        return self._consume(self._bind_seq)

    async def _consume(self, bind_id: int) -> AsyncIterator[bytes]:
        try:
            while not self._closed:
                while not self._out_chunks:
                    if self._closed or self._ingest_ended:
                        return
                    self._out_data.clear()
                    await self._out_data.wait()
                chunk = self._out_chunks.popleft()
                self._out_bytes -= len(chunk)
                self._out_space.set()
                yield chunk
        finally:
            self._consumer_disconnected(bind_id)

    def _consumer_disconnected(self, bind_id: int) -> None:
        if bind_id != self._bind_seq:
            return  # a stale generator finalizing after a re-bind took over
        self._consumer_bound = False
        if self._closed:
            return
        _log.info("Radio session %s: consumer disconnected — %.1fs grace",
                  self._session_id, self._grace_s)
        self._grace_handle = self._timer_factory(self._grace_s,
                                                 self._grace_expired)

    def _cancel_grace(self) -> None:
        if self._grace_handle is not None:
            try:
                self._grace_handle.cancel()
            except Exception:
                pass
            self._grace_handle = None

    def _grace_expired(self) -> None:
        self._grace_handle = None
        if self._closed or self._consumer_bound:
            return
        _log.warning("Radio session %s: consumer gone beyond the %.1fs grace",
                     self._session_id, self._grace_s)
        for cb in list(self._consumer_gone_listeners):
            try:
                res = cb()
                if inspect.isawaitable(res):
                    _spawn(res)
            except Exception:
                _log.warning("Radio: consumer-gone listener raised", exc_info=True)


# Bounded reconnect budget for a live-stream drop inside the proxy. Small — a
# healthy station respawns on the first try; a permanently-dead upstream must
# self-terminate rather than respawn ffmpeg forever (mirrors radio_endless's
# RADIO_RECONNECT_MAX_ATTEMPTS intent, applied to the ingest encoder here).
_RADIO_STREAM_RECONNECT_MAX = 5


# ── small helpers (injectable time + fire-and-forget, flow.py shapes) ────────────


def _now() -> float:
    import time
    return time.monotonic()


def _default_timer_factory(delay_s: float, cb: Callable[[], None]):
    return asyncio.get_running_loop().call_later(delay_s, cb)


def _log_task_exc(task: asyncio.Task) -> None:
    if not task.cancelled():
        exc = task.exception()
        if exc:
            _log.error("Radio task raised: %s", exc, exc_info=exc)


def _spawn(coro: Any) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return
    task = loop.create_task(coro)
    task.add_done_callback(_log_task_exc)


# ── module-level single-session registry (the radio session drives it) ───────────
#
# SINGLE-SESSION resource, exactly like flow.py: one active radio transcode-proxy
# at a time (there is one shared output). Minting a new session (station start /
# instant switch) supersedes and reaps the previous one BEFORE registering the new
# one, so rapid station flipping can never stack orphaned encoders (ADV-4). The
# route looks the id up here — a wrong or stale id reads as no session (404).

_current_session: RadioStreamSession | None = None

# F3: default consumer-gone callback, registered once by state.setup(). Every
# newly-minted session gets it attached at start_radio_stream time, so the
# per-station proxy sessions (minted by radio_proxy_url) actually fire the
# "station offline" path when their disconnect grace expires — the boot-time
# one-shot attach against current_radio_stream() (None at boot) never covered
# per-station sessions.
_default_consumer_gone_callback: Callable[[], Any] | None = None


def set_default_consumer_gone_callback(cb: Callable[[], Any] | None) -> None:
    """Register (or clear) the default consumer-gone callback attached to every
    session minted by :func:`start_radio_stream`. Called once by state.setup()."""
    global _default_consumer_gone_callback
    _default_consumer_gone_callback = cb


async def start_radio_stream(final_url: str, **kwargs: Any) -> RadioStreamSession:
    """Create, register, and START a radio transcode-proxy session for the
    SSRF-validated final URL (requires a running loop). Supersedes any still-open
    previous session, **awaiting its full reap BEFORE spawning the new encoder**
    (F4) so rapid station-flipping can never stack overlapping ffmpeg encoders —
    the fire-and-forget ``_spawn(old.close())`` this replaces let the old and new
    spawns race."""
    global _current_session
    old = _current_session
    # F4: synchronous reap-before-spawn. Await the old session's teardown (its
    # ffmpeg terminated + reaped) BEFORE the new session spawns its encoder, so
    # no two encoders are ever alive at once on a switch. Detach _current_session
    # first so a concurrent get_radio_stream can't hand a device the dying session.
    if old is not None and not old.closed:
        _log.info("Radio: superseding session %s (awaiting reap before spawn)",
                  old.session_id)
        _current_session = None
        await old.close()
    session = RadioStreamSession(final_url, **kwargs)
    # F3: attach the registered default consumer-gone listener so grace-expiry
    # fires the "station offline" transition for THIS per-station session.
    if _default_consumer_gone_callback is not None:
        session.add_consumer_gone_listener(_default_consumer_gone_callback)
    _current_session = session
    session.start()
    return session


def get_radio_stream(session_id: str) -> RadioStreamSession | None:
    """The current session iff ``session_id`` names it and it is still open (the
    route's lookup — a wrong or stale id reads as no session, so the route 404s
    WITHOUT fetching anything upstream, SEC-001)."""
    s = _current_session
    if s is not None and s.session_id == session_id and not s.closed:
        return s
    return None


def current_radio_stream() -> RadioStreamSession | None:
    """The current open radio stream session, or None (Cast/DLNA read this to
    build their device-facing capability URL)."""
    s = _current_session
    return s if (s is not None and not s.closed) else None


async def close_radio_stream() -> None:
    """Close and unregister the current session (idempotent) — station stop /
    switch invalidates the capability id so a stale device can't re-bind."""
    s = _current_session
    if s is not None:
        await s.close()
