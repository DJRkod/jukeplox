"""Cast flow-mode stream engine — one server-stitched, sample-accurate,
endless encoded stream per active flow playback (2026-07-11 supervisor plan U9).

Why this exists: true gapless on Chromecast is a ~decade-old rejected platform
request — native queueing is gap-*minimized* only. The industry answer (Music
Assistant "flow mode", Hi-Fi Cast "true gapless") is to decode queue tracks to
PCM server-side, stitch them sample-accurately, re-encode as ONE endless
stream, and have the device LOAD a single flow URL (docs/solutions/
tooling-decisions/gapless-and-reconnect-protocol-capability-map.md). Accepted
costs: skips lag by the device buffer, per-track device metadata is lost, and
position/play-counting are reconciled to DEVICE-reported time (U10's wiring).

Layering: this module owns its OWN decode/encode pipeline (ffmpeg subprocesses
following app/output/airplay.py's spawn/reap/log conventions) and NEVER
imports from ``app/api/*`` (the rule in app/transcode.py's docstring). The API
layer imports US — app/api/stream.py's flow route consumes ``bind_consumer``;
api→output is the allowed dependency direction.

Pipeline (every seam injectable so tests run in-python PCM fixtures on mock
clocks — no ffmpeg, no real sleeps):

    lookahead → decoder (ffmpeg → s16le 44.1kHz stereo PCM) → pace
    (bounded run-ahead) → stitch (byte-exact concat + boundary ledger)
    → encoder (one long-lived ffmpeg pcm→FLAC) → bounded out-buffer
    → HTTP consumer (the flow route's chunked, Range-less response)

Load-bearing behaviors:

- STITCH: track N+1's first PCM byte follows track N's last — zero padding,
  zero truncation. Everything is resampled to the fixed stitch format
  (44100 Hz / 2ch / s16le) at decode, so the chain is same-caps by
  construction. Boundaries are recorded as exact sample offsets in the
  stitch timeline (``boundaries``, ``track_at``, ``offset_of``).
- BOUNDARY CLOCK: the boundary event fires when the ENCODE stream crosses a
  boundary offset — i.e. when the new track's first PCM byte is fed to the
  encoder, which leads heard audio by up to the run-ahead bound plus the
  device buffer. Queue advance and Now Playing follow this clock (Music
  Assistant precedent; device lag accepted). Play COUNTS must NOT: they fire
  when device-reported ``current_time`` crosses the boundary offset — U10
  maps device time through ``track_at``/``offset_of``/
  ``held_offset_from_device_time``; this engine only emits the events.
- LOOKAHEAD: at each track's decode completion the next track is re-resolved
  through U6's ``state.effective_next_track()`` (queue edits before the
  boundary reposition the lookahead, R14). The Closing Time check is applied
  HERE (``effective_next_track`` is deliberately closing-agnostic): the flow
  never stitches past the send-off track (R21).
- PACING: the encode side may lead real-time playback by at most
  ``run_ahead_s`` of audio; ``pause()`` freezes the pacing clock. The pacing
  bound IS the memory bound — the out-buffer cap equals the run-ahead budget
  in PCM-rate bytes, so an hour-long session never grows without bound.
- STALL WATCHDOG: a source that stops producing PCM (no bytes, no EOF) for
  ``FLOW_DECODE_STALL_S`` while the session is actively pumping is treated
  as a decode failure — decoder closed, server-side skip event, next track
  spliced. Nothing else fires on a silent stall in flow mode (no boundary,
  skip is failure-only, the consumer stays bound, the Cast socket stays
  connected), so without this bound the stream froze forever. A paused
  session legitimately stops reading; the bound never applies while paused.
- SINGLE-SESSION consumer: exactly one bound consumer; a disconnect arms a
  short grace timer and a re-GET within it re-binds the SAME session
  mid-stream (Cast receivers re-request the media URL after transient
  hiccups — the stream resumes from the current encode position, never from
  zero); grace expiry notifies the consumer-gone hook (U10 turns it into
  outage-suspected); a second concurrent consumer raises
  ``FlowConsumerConflict`` (the route's 409) and never spawns a second
  stitcher.
- ENCODE FORMAT knob: ``encode_format`` — ``"flac"`` (default; one long-lived
  ffmpeg pcm→FLAC pipe, streamed, unknown duration by design) or ``"wav"``
  (raw PCM behind a streaming WAV header, no subprocess). The final
  format/bitrate decision is deferred to hardware validation on the real
  receiver (plan deferred-to-implementation); the knob exists so U10 can
  flip it without touching this engine.
"""

from __future__ import annotations

import asyncio
import collections
import inspect
import logging
import re
import secrets
import struct
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx

_log = logging.getLogger(__name__)

# ── stitch format: everything is resampled to this at decode ─────────────────
FLOW_SAMPLE_RATE = 44100
FLOW_CHANNELS = 2
FLOW_SAMPLE_BYTES = 2  # s16le
FLOW_FRAME_BYTES = FLOW_CHANNELS * FLOW_SAMPLE_BYTES
FLOW_BYTES_PER_SECOND = FLOW_SAMPLE_RATE * FLOW_FRAME_BYTES

# Bounded run-ahead: the encode clock may lead real-time playback by at most
# this much audio. Doubles as the memory bound (out-buffer cap).
FLOW_RUN_AHEAD_S = 10.0

# Consumer-disconnect grace: a new GET within this window re-binds the same
# session; expiry notifies the consumer-gone hook (U10: outage-suspected).
FLOW_CONSUMER_GRACE_S = 5.0

# Decode-stall watchdog: a source that produces zero PCM (no bytes, no EOF)
# for this long while the session is ACTIVELY pumping is treated as a decode
# failure (server-side skip + splice next). Nothing else can fire in flow
# mode — no boundary crosses, skip is decode-failure-only, the consumer stays
# bound, the Cast socket stays connected and the outage watchdog is disarmed —
# so without this bound a stalled source froze the stream forever.
FLOW_DECODE_STALL_S = 30.0

# The encode-format knob (see module docstring). "flac" | "wav".
FLOW_ENCODE_FORMAT_DEFAULT = "flac"

_PCM_READ_CHUNK = 65536   # ~0.37s of stitch-format audio per decoder read
_ENC_READ_CHUNK = 65536   # encoder→out-buffer granularity

# SIGTERM grace before SIGKILL on subprocess teardown (airplay.py convention).
_STOP_GRACE_S = 2.0


class FlowDecodeError(Exception):
    """A track's PCM decode failed (ffmpeg non-zero exit / read error)."""


class FlowEncodeError(Exception):
    """The long-lived encoder died — the flow session cannot continue."""


class FlowConsumerConflict(Exception):
    """A second concurrent consumer tried to bind the single-session stream."""


@dataclass(frozen=True)
class FlowBoundary:
    """One track boundary in the stitch timeline, as emitted to listeners.

    ``offset_samples``/``offset_ms`` are the exact stitch-timeline position of
    the track's FIRST sample; ``reposition`` marks the single event a
    ``reposition()`` (skip/seek) emits, so the consumer can distinguish a
    natural gapless boundary from a jump."""

    track: Any
    offset_samples: int
    offset_ms: int
    reposition: bool = False


# Any URL query string is treated as credential-bearing (Plex rides
# ``?X-Plex-Token=…`` on the part URL) and scrubbed before logging.
_URL_QUERY_RE = re.compile(r"(https?://[^\s'\"?]+)\?[^\s'\"]*")


def _redact(text: Any) -> str:
    """Scrub credential-bearing URL query strings from text bound for the
    logs — ffmpeg stderr lines and fetch errors may echo source URLs."""
    return _URL_QUERY_RE.sub(r"\1?<redacted>", str(text))


def _is_http_source(source: str) -> bool:
    return source.lower().startswith(("http://", "https://"))


def _same_track(a: Any, b: Any) -> bool:
    if a is b:
        return True
    ia, ib = getattr(a, "id", None), getattr(b, "id", None)
    return ia is not None and ia == ib


def _default_timer_factory(delay_s: float, cb: Callable[[], None]):
    """``loop.call_later`` behind the injectable seam (the supervisor's
    timer-factory shape) so tests drive the grace window with fake timers."""
    return asyncio.get_running_loop().call_later(delay_s, cb)


def _log_task_exc(task: asyncio.Task) -> None:
    if not task.cancelled():
        exc = task.exception()
        if exc:
            _log.error("Flow task raised: %s", exc, exc_info=exc)


def _spawn(coro: Any) -> None:
    """Fire-and-forget with exception logging (app.state._log_task_exc shape).
    No running loop (sync caller during shutdown) → drop the work."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return
    task = loop.create_task(coro)
    task.add_done_callback(_log_task_exc)


# ── production lookahead / source resolution (injectable; late imports) ──────


async def _default_next_track(prev_track: Any) -> Any | None:
    """The queue's effective next via U6's machinery, with the Closing Time
    check applied here (R21): ``effective_next_track`` is deliberately
    closing-agnostic, so the flow lookahead must never stitch past the
    send-off track — when ``prev_track`` is the configured trigger the flow
    ends there and the queue freeze happens exactly as in per-track mode."""
    from app import state
    if prev_track is not None:
        try:
            closing = (await state._closing_trigger_message(prev_track)) is not None
        except Exception:
            # Unreadable config is not a freeze (matches the U6 reconcile's
            # read-at-decision posture).
            closing = False
        if closing:
            _log.info("Flow: %r is the Closing Time send-off — not stitching "
                      "past it", getattr(prev_track, "title", "?"))
            return None
    return state.effective_next_track()


async def _default_resolve_source(track: Any) -> tuple[str, dict] | None:
    """Resolve ``track`` to the ffmpeg decode input: a local filesystem path,
    else the direct source URL plus any provider auth headers. The decode runs
    ON the server, so it reads the source directly instead of looping back
    through the app's own HTTP stream proxy. Walks the holder keys in priority
    order (resolution-level fallback); returns None when nothing resolves —
    the pump then skips the track server-side."""
    from app import state
    client = await state.get_plex_client()
    if client is None:
        return None
    for key in state._holder_keys(track):
        try:
            target = client.resolve_stream(key)
        except Exception:
            continue
        path = getattr(target, "path", None)
        if path:
            return (path, {})
        url = (getattr(target, "url", None) or "").strip()
        if url:
            return (url, dict(getattr(target, "headers", None) or {}))
    return None


# ── ffmpeg argv builders (pure functions — airplay.py conventions) ───────────


def _build_flow_decode_args(source: str, headers: dict | None,
                            start_offset_ms: int = 0) -> list[str]:
    """ffmpeg invocation decoding one track's source to the stitch format.

    Mirrors airplay.py's ``_build_ffmpeg_args``: ``-loglevel error`` (stderr
    stays failure-only), ``-nostdin`` for direct-argv inputs (no tty wait),
    and the seek ``-ss`` placed AFTER ``-i`` (output-side seek) — input-side
    ``-ss`` on an HTTP source triggers a Range-probe storm hunting the frame
    boundary; output-side issues one sequential GET (or one sequential pipe
    read) and decode-discards up to the offset.

    Credential hygiene: ``headers`` NEVER joins argv, and neither does an
    http(s) URL — a Plex part URL carries ``?X-Plex-Token=`` and Jellyfin
    auth rides an ``Authorization`` header, both of which would be visible
    in the process list (and in argv-echoing stderr) if passed to ffmpeg.
    http(s) sources are instead fetched by the decoder's httpx feeder (auth
    in the REQUEST) and fed on stdin (``-i pipe:0`` — the same posture as
    the API proxy's httpx+pipe transcode). Local paths carry no credentials
    and stay direct argv."""
    del headers  # never on argv — the decoder's httpx feeder carries auth
    if _is_http_source(source):
        # No -nostdin: stdin IS the input (matches _build_flow_encode_args).
        args = ["ffmpeg", "-loglevel", "error", "-i", "pipe:0"]
    else:
        args = ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", source]
    if start_offset_ms > 0:
        args += ["-ss", f"{start_offset_ms / 1000:.3f}"]
    args += [
        "-acodec", "pcm_s16le",
        "-ac", str(FLOW_CHANNELS),
        "-ar", str(FLOW_SAMPLE_RATE),
        "-f", "s16le",
        "pipe:1",
    ]
    return args


def _build_flow_encode_args() -> list[str]:
    """The single long-lived pcm→FLAC encode: raw stitch-format PCM on stdin,
    streamed FLAC on stdout. A pipe output means ffmpeg cannot backfill
    STREAMINFO — unknown duration is exactly right for an endless live
    stream (unlike the per-track proxy, which NEEDS the seekable file)."""
    return [
        "ffmpeg",
        "-loglevel", "error",
        "-f", "s16le",
        "-ar", str(FLOW_SAMPLE_RATE),
        "-ac", str(FLOW_CHANNELS),
        "-i", "pipe:0",
        "-f", "flac",
        "pipe:1",
    ]


async def _terminate_proc(proc: Any, label: str) -> None:
    """SIGTERM with grace, then SIGKILL (airplay.py's teardown discipline).
    Tolerates None and already-exited processes."""
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.terminate()
    except (ProcessLookupError, OSError):
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=_STOP_GRACE_S)
    except asyncio.TimeoutError:
        _log.warning("Flow teardown: %s did not exit on SIGTERM within %.1fs "
                     "— sending SIGKILL", label, _STOP_GRACE_S)
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=_STOP_GRACE_S)
        except asyncio.TimeoutError:
            _log.error("Flow teardown: %s ignored SIGKILL", label)


async def _drain_stderr(stream: asyncio.StreamReader, label: str) -> None:
    """Drain a subprocess's stderr so the kernel pipe can't back-pressure its
    write side (the airplay.py freeze mode), surfacing lines at WARNING —
    with ``-loglevel error`` anything here is worth attention."""
    try:
        while True:
            line_bytes = await stream.readline()
            if not line_bytes:
                return
            line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
            _log.warning("Flow %s stderr: %s", label, _redact(line))
    except asyncio.CancelledError:
        return
    except Exception:
        _log.warning("Flow %s stderr reader crashed", label, exc_info=True)


class FFmpegPCMDecoder:
    """Default per-track decoder: an ffmpeg subprocess emitting stitch-format
    PCM on stdout. Spawned lazily on first ``read`` (keeps the factory sync);
    ``read`` returns b"" on clean EOF and raises ``FlowDecodeError`` on a
    non-zero exit; ``close`` kills and reaps (idempotent).

    http(s) sources are fetched via httpx and streamed into ffmpeg stdin by
    a feeder task, so provider credentials (token query strings,
    Authorization headers) never appear on the process list — the API
    proxy's httpx+pipe:0 posture, implemented locally because this module
    never imports ``app/api/*``. Local paths are read directly by ffmpeg."""

    def __init__(self, source: str, headers: dict | None,
                 start_offset_ms: int = 0) -> None:
        self._source = source
        self._headers = dict(headers or {})
        self._pipe_source = _is_http_source(source)
        self._args = _build_flow_decode_args(source, headers, start_offset_ms)
        self._proc: Any = None
        self._stderr_task: asyncio.Task | None = None
        self._feeder_task: asyncio.Task | None = None
        self._closed = False

    async def read(self, n: int) -> bytes:
        if self._closed:
            return b""
        if self._proc is None:
            self._proc = await asyncio.create_subprocess_exec(
                *self._args,
                stdin=asyncio.subprocess.PIPE if self._pipe_source else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            if self._proc.stderr is not None:
                self._stderr_task = asyncio.create_task(
                    _drain_stderr(self._proc.stderr, "decode"))
            if self._pipe_source:
                self._feeder_task = asyncio.create_task(
                    self._feed_source(self._proc))
                self._feeder_task.add_done_callback(_log_task_exc)
        chunk = await self._proc.stdout.read(n)
        if chunk:
            return chunk
        rc = await self._proc.wait()
        if rc != 0 and not self._closed:
            raise FlowDecodeError(f"ffmpeg decode exited {rc}")
        return b""

    async def _feed_source(self, proc: Any) -> None:
        """Stream the http(s) source into ffmpeg stdin in bounded chunks —
        credentials ride the httpx REQUEST, never argv. EOF closes stdin so
        ffmpeg drains and exits; ffmpeg dying early (BrokenPipe) just ends
        the feed — its exit code surfaces through ``read``."""
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=None, write=None,
                                  pool=None),
            follow_redirects=True,
        )
        try:
            resp = await client.send(
                client.build_request("GET", self._source,
                                     headers=self._headers),
                stream=True)
            try:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    proc.stdin.write(chunk)
                    await proc.stdin.drain()
            finally:
                await resp.aclose()
        except asyncio.CancelledError:
            raise
        except (BrokenPipeError, ConnectionResetError):
            pass  # ffmpeg exited early; its non-zero rc is handled by read()
        except Exception:
            _log.warning("Flow decode: source fetch failed for %s",
                         _redact(self._source), exc_info=True)
        finally:
            try:
                await client.aclose()
            except Exception:
                pass
            try:
                proc.stdin.close()
                await proc.stdin.wait_closed()
            except Exception:
                pass

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True  # set FIRST so an unblocked read reports EOF, not error
        if self._feeder_task is not None and not self._feeder_task.done():
            self._feeder_task.cancel()
            try:
                await self._feeder_task
            except asyncio.CancelledError:
                pass
        self._feeder_task = None
        await _terminate_proc(self._proc, "decode-ffmpeg")
        if self._stderr_task is not None and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
        self._stderr_task = None


class FFmpegFlowEncoder:
    """Default encoder: ONE long-lived ffmpeg pcm→FLAC pipe for the whole
    session. ``feed`` writes PCM to stdin; ``read`` pulls encoded bytes from
    stdout (b"" only after ``finalize`` drained); ``close`` kills and reaps."""

    def __init__(self) -> None:
        self._proc: Any = None
        self._stderr_task: asyncio.Task | None = None
        self._closed = False

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *_build_flow_encode_args(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if self._proc.stderr is not None:
            self._stderr_task = asyncio.create_task(
                _drain_stderr(self._proc.stderr, "encode"))

    async def feed(self, pcm: bytes) -> None:
        if self._closed or self._proc is None:
            raise FlowEncodeError("encoder is not running")
        try:
            self._proc.stdin.write(pcm)
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            raise FlowEncodeError(f"encoder pipe write failed: {exc}") from exc

    async def finalize(self) -> None:
        """Close stdin so ffmpeg flushes its last FLAC frames and exits."""
        if self._proc is None or self._proc.stdin is None:
            return
        try:
            self._proc.stdin.close()
            await self._proc.stdin.wait_closed()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    async def read(self, n: int) -> bytes:
        if self._proc is None:
            return b""
        return await self._proc.stdout.read(n)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await _terminate_proc(self._proc, "encode-ffmpeg")
        if self._stderr_task is not None and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
        self._stderr_task = None


def _wav_stream_header(sample_rate: int, channels: int, bits: int = 16) -> bytes:
    """A WAV header for an endless stream: RIFF/data sizes set to 0xFFFFFFFF
    (the streaming-WAV convention — receivers treat it as unknown length)."""
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    return (b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate,
                                    byte_rate, block_align, bits)
            + b"data" + struct.pack("<I", 0xFFFFFFFF))


class WavFlowEncoder:
    """The ``encode_format="wav"`` fallback: raw stitch PCM behind a streaming
    WAV header — no subprocess at all (the deferred-to-hardware option should
    the receiver reject streamed FLAC)."""

    def __init__(self, sample_rate: int = FLOW_SAMPLE_RATE,
                 channels: int = FLOW_CHANNELS) -> None:
        self._buf = bytearray(_wav_stream_header(sample_rate, channels))
        self._finalized = False
        self._closed = False
        self._data = asyncio.Event()

    async def start(self) -> None:
        return None

    async def feed(self, pcm: bytes) -> None:
        if self._closed:
            raise FlowEncodeError("encoder closed")
        self._buf += pcm
        self._data.set()

    async def finalize(self) -> None:
        self._finalized = True
        self._data.set()

    async def read(self, n: int) -> bytes:
        while not self._buf:
            if self._finalized or self._closed:
                return b""
            self._data.clear()
            await self._data.wait()
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    async def close(self) -> None:
        self._closed = True
        self._data.set()


def _default_decoder_factory(source: str, headers: dict | None,
                             start_offset_ms: int) -> FFmpegPCMDecoder:
    return FFmpegPCMDecoder(source, headers, start_offset_ms)


def _default_encoder_factory(encode_format: str, sample_rate: int,
                             channels: int):
    if encode_format == "wav":
        return WavFlowEncoder(sample_rate, channels)
    return FFmpegFlowEncoder()


_CONTENT_TYPES = {"flac": "audio/flac", "wav": "audio/wav"}


class FlowSession:
    """One flow playback: stitches queue tracks into a single continuous
    encoded stream with server-clock track boundaries. See the module
    docstring for the design; U10 drives the lifecycle through the
    module-level registry (``create_flow_session``/``get``/``close``)."""

    def __init__(
        self,
        first_track: Any,
        *,
        start_offset_ms: int = 0,
        session_id: str | None = None,
        encode_format: str = FLOW_ENCODE_FORMAT_DEFAULT,
        decoder_factory: Callable[[str, dict | None, int], Any] | None = None,
        encoder_factory: Callable[[str, int, int], Any] | None = None,
        next_track_fn: Callable[[Any], Awaitable[Any | None]] | None = None,
        source_resolver: Callable[[Any], Awaitable[tuple[str, dict] | None]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        timer_factory: Callable[[float, Callable[[], None]], Any] | None = None,
        run_ahead_s: float = FLOW_RUN_AHEAD_S,
        grace_s: float = FLOW_CONSUMER_GRACE_S,
        decode_stall_s: float = FLOW_DECODE_STALL_S,
        sample_rate: int = FLOW_SAMPLE_RATE,
        channels: int = FLOW_CHANNELS,
    ) -> None:
        if encode_format not in _CONTENT_TYPES:
            raise ValueError(f"unknown flow encode_format {encode_format!r}")
        self._session_id = session_id or secrets.token_urlsafe(16)
        self._encode_format = encode_format
        self._decoder_factory = decoder_factory or _default_decoder_factory
        self._encoder_factory = encoder_factory or _default_encoder_factory
        self._next_track_fn = next_track_fn or _default_next_track
        self._resolve_source = source_resolver or _default_resolve_source
        self._clock = clock
        self._sleep = sleep
        self._timer_factory = timer_factory or _default_timer_factory
        self._run_ahead_s = run_ahead_s
        self._grace_s = grace_s
        self._decode_stall_s = decode_stall_s
        self._sample_rate = sample_rate
        self._channels = channels
        self._frame_bytes = channels * FLOW_SAMPLE_BYTES
        self._bytes_per_s = sample_rate * self._frame_bytes
        # The pacing bound IS the memory bound: buffered-but-unsent encoded
        # bytes are capped at the run-ahead budget in PCM-rate bytes (encoded
        # output is never larger than the PCM it came from for FLAC/WAV).
        self._out_cap = max(_ENC_READ_CHUNK, int(run_ahead_s * self._bytes_per_s))

        self._encoder = self._encoder_factory(encode_format, sample_rate, channels)

        # ── stitch timeline state ─────────────────────────────────────────
        self._fed_bytes = 0                       # encode-stream position (PCM bytes fed)
        self._boundaries: list[tuple[int, Any]] = []   # (offset_samples, track)
        self._device_epoch_offset_ms = 0          # stitch offset of the device's current LOAD

        # ── pump / generation state ───────────────────────────────────────
        self._gen = 0                             # bumped by reposition(); stales in-flight work
        self._pending_entry: tuple[Any, int, bool, bool] | None = (
            first_track, max(0, start_offset_ms), False, False)
        self._active_decoder: Any = None
        self._last_failed: Any = None
        self._started = False
        self._closed = False
        self._ended = False

        # ── pacing state ──────────────────────────────────────────────────
        self._started_at: float | None = None
        self._paused_at: float | None = None
        self._paused_accum = 0.0
        self._resume_evt = asyncio.Event()
        self._resume_evt.set()

        # ── out-buffer (encoder → consumer) ───────────────────────────────
        self._out_chunks: collections.deque[bytes] = collections.deque()
        self._out_bytes = 0
        self._out_data = asyncio.Event()
        self._out_space = asyncio.Event()
        self._out_space.set()

        # ── consumer binding / grace ──────────────────────────────────────
        self._consumer_bound = False
        self._bind_seq = 0
        self._grace_handle: Any = None

        # ── listeners ─────────────────────────────────────────────────────
        self._boundary_listeners: list[Callable[[FlowBoundary], Any]] = []
        self._skip_listeners: list[Callable[[Any, str], Any]] = []
        self._consumer_gone_listeners: list[Callable[[], Any]] = []
        self._ended_listeners: list[Callable[[], Any]] = []

        self._pump_task: asyncio.Task | None = None
        self._enc_reader_task: asyncio.Task | None = None

    # ── identity / observability ───────────────────────────────────────────

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def url_path(self) -> str:
        """The flow route path for this session (U10 composes the absolute
        URL with the same base logic as per-track dispatch)."""
        return f"/api/stream/flow/{self._session_id}"

    @property
    def content_type(self) -> str:
        return _CONTENT_TYPES[self._encode_format]

    @property
    def encode_format(self) -> str:
        return self._encode_format

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def ended(self) -> bool:
        """True once the queue exhausted and the encoder flushed cleanly."""
        return self._ended

    @property
    def paused(self) -> bool:
        return self._paused_at is not None

    @property
    def position_ms(self) -> int:
        """Encode-clock position: milliseconds of audio fed to the encoder
        (leads heard audio by up to the run-ahead bound)."""
        return int(self._fed_bytes * 1000 // self._bytes_per_s)

    @property
    def buffered_unsent_bytes(self) -> int:
        """Encoded bytes buffered but not yet read by the consumer — bounded
        by ``out_buffer_cap_bytes`` (the run-ahead budget)."""
        return self._out_bytes

    @property
    def out_buffer_cap_bytes(self) -> int:
        return self._out_cap

    @property
    def boundaries(self) -> list[tuple[int, Any]]:
        """The stitch-timeline boundary ledger: ``(offset_samples, track)``
        in feed order (the initial track is recorded at offset 0)."""
        return list(self._boundaries)

    # ── stitch-timeline mapping (U10: device time ↔ tracks) ────────────────

    def track_at(self, stream_position_ms: int) -> Any | None:
        """The track audible at ``stream_position_ms`` of the stitch timeline
        (device-reported ``current_time`` mapped through the boundary ledger).
        Positions beyond the encode clock clamp to the last track."""
        if not self._boundaries:
            return None
        samples = int(stream_position_ms * self._sample_rate // 1000)
        found = self._boundaries[0][1]
        for offset, track in self._boundaries:
            if offset <= samples:
                found = track
            else:
                break
        return found

    def offset_of(self, track: Any) -> int | None:
        """Stitch-timeline offset (ms) of ``track``'s most recent boundary,
        or None when the track never entered the stitch."""
        for offset, t in reversed(self._boundaries):
            if _same_track(t, track):
                return int(offset * 1000 // self._sample_rate)
        return None

    def set_device_epoch_offset(self, offset_ms: int) -> None:
        """Rebase device-reported time onto the stitch timeline: the stitch
        offset at which the device's CURRENT load of the flow URL began
        (0 for the initial LOAD; U10 sets the held offset on a resume
        re-LOAD so ``held_offset_from_device_time`` keeps mapping)."""
        self._device_epoch_offset_ms = max(0, int(offset_ms))

    def held_offset_from_device_time(self, device_time_s: float | None) -> int:
        """Map a device-reported ``current_time`` (seconds into the device's
        current load) to the stitch-timeline offset (ms) — the outage-resume
        held position. ``None`` (no device report available) falls back to
        the documented estimate: encode clock minus the run-ahead margin."""
        fed_ms = self.position_ms
        if device_time_s is None:
            return max(0, int(fed_ms - self._run_ahead_s * 1000))
        offset = self._device_epoch_offset_ms + int(device_time_s * 1000)
        return max(0, min(offset, fed_ms))

    # ── listener registration (U10 wires these) ────────────────────────────

    def add_boundary_listener(self, cb: Callable[[FlowBoundary], Any]) -> None:
        """``cb(FlowBoundary)`` on every encode-clock boundary crossing
        (async callbacks are awaited — boundary ordering is strict)."""
        self._boundary_listeners.append(cb)

    def add_skip_listener(self, cb: Callable[[Any, str], Any]) -> None:
        """``cb(track, reason)`` when a track is skipped server-side (decode
        failure / unresolvable source). Async callbacks are AWAITED before
        the lookahead re-resolves, so the consumer can drop the failed item
        from the queue first (the production wiring must — see the
        same-track spin guard in the pump)."""
        self._skip_listeners.append(cb)

    def add_consumer_gone_listener(self, cb: Callable[[], Any]) -> None:
        """``cb()`` when the consumer disconnect grace expires with no
        re-bind (U10 turns this into outage-suspected)."""
        self._consumer_gone_listeners.append(cb)

    def add_ended_listener(self, cb: Callable[[], Any]) -> None:
        """``cb()`` when the queue exhausted and the stream finalized
        cleanly (natural end — never fired on ``close()``)."""
        self._ended_listeners.append(cb)

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the pump (requires a running event loop). Idempotent."""
        if self._started or self._closed:
            return
        self._started = True
        self._started_at = self._clock()
        self._pump_task = asyncio.get_running_loop().create_task(self._pump())
        self._pump_task.add_done_callback(_log_task_exc)

    def pause(self) -> None:
        """Freeze the pacing clock: the encode side stops leading once the
        run-ahead budget is consumed. Idempotent."""
        if self._paused_at is None:
            self._paused_at = self._clock()
            self._resume_evt.clear()

    def resume(self) -> None:
        """Unfreeze the pacing clock. Idempotent."""
        if self._paused_at is not None:
            self._paused_accum += self._clock() - self._paused_at
            self._paused_at = None
            self._resume_evt.set()

    async def reposition(self, track: Any, offset_ms: int = 0) -> bool:
        """Skip/seek: restart the stitch pipeline at ``track``/``offset_ms``.
        Bumps the generation (in-flight decode from the old position is
        invalidated and its subprocess closed), splices the new track into
        the SAME encoder at the current encode position, and emits exactly
        ONE boundary event (``reposition=True``) — never a double advance.
        Returns False when the session already ended/closed (the encoder is
        finalized — U10 must start a fresh session instead)."""
        if self._closed or self._ended:
            return False
        self._gen += 1
        self._pending_entry = (track, max(0, offset_ms), True, True)
        dec = self._active_decoder
        if dec is not None:
            # Close out-of-band so a decoder blocked on a stalled source
            # unblocks promptly; the pump's generation check drops its bytes.
            _spawn(dec.close())
        _log.info("Flow session %s: reposition to %r @ %dms (gen %d)",
                  self._session_id, getattr(track, "title", "?"),
                  offset_ms, self._gen)
        return True

    async def close(self) -> None:
        """Idempotent teardown: cancels the pump and encoder reader, closes
        (kills/reaps) any live decode/encode subprocess, cancels the grace
        timer, and wakes every waiter so consumers terminate."""
        if self._closed:
            return
        self._closed = True
        self._cancel_grace()
        # Wake everything parked on session events.
        self._resume_evt.set()
        self._out_data.set()
        self._out_space.set()
        current = asyncio.current_task()
        pending = [t for t in (self._pump_task, self._enc_reader_task)
                   if t is not None and not t.done() and t is not current]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        dec = self._active_decoder
        self._active_decoder = None
        if dec is not None:
            try:
                await dec.close()
            except Exception:
                _log.warning("Flow session %s: decoder close failed",
                             self._session_id, exc_info=True)
        try:
            await self._encoder.close()
        except Exception:
            _log.warning("Flow session %s: encoder close failed",
                         self._session_id, exc_info=True)
        global _current_session
        if _current_session is self:
            _current_session = None
        _log.info("Flow session %s: closed", self._session_id)

    # ── consumer binding (the flow route's body) ───────────────────────────

    def bind_consumer(self) -> AsyncIterator[bytes]:
        """Bind THE single consumer and return the chunk iterator. A second
        concurrent bind raises ``FlowConsumerConflict`` (the route's 409)
        without touching the bound consumer or the boundary clock. Re-binding
        within the disconnect grace cancels the grace timer and resumes from
        the current encode position — unread buffered chunks are retained
        across the gap, so nothing is dropped or replayed.

        The caller MUST start the returned generator (first ``__anext__``):
        the binding is released only by the generator body's finally, and a
        never-STARTED async generator runs no body and no finally — the flow
        route primes the first chunk before handing the body to Starlette so
        an instant client drop can never strand the binding."""
        if self._closed:
            raise RuntimeError("flow session is closed")
        if self._consumer_bound:
            raise FlowConsumerConflict(
                f"flow session {self._session_id} already has a consumer")
        self._consumer_bound = True
        self._cancel_grace()
        self._bind_seq += 1
        return self._consume(self._bind_seq)

    async def _consume(self, bind_id: int) -> AsyncIterator[bytes]:
        try:
            while not self._closed:
                while not self._out_chunks:
                    if self._ended or self._closed:
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
        if self._closed or (self._ended and not self._out_chunks):
            return  # closed, or the stream ended naturally and fully drained
        _log.info("Flow session %s: consumer disconnected — %.1fs grace",
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
        _log.warning("Flow session %s: consumer gone beyond the %.1fs grace",
                     self._session_id, self._grace_s)
        for cb in list(self._consumer_gone_listeners):
            try:
                res = cb()
                if inspect.isawaitable(res):
                    _spawn(res)
            except Exception:
                _log.warning("Flow: consumer-gone listener raised", exc_info=True)

    # ── pump: decode → pace → stitch → encode ──────────────────────────────

    async def _pump(self) -> None:
        try:
            await self._encoder.start()
            self._enc_reader_task = asyncio.get_running_loop().create_task(
                self._read_encoded())
            self._enc_reader_task.add_done_callback(_log_task_exc)
            prev: Any = None
            while not self._closed:
                gen = self._gen
                pending = self._pending_entry
                if pending is not None:
                    self._pending_entry = None
                    track, offset_ms, emit, repos = pending
                    gen = self._gen
                else:
                    track = await self._lookahead(prev)
                    if self._gen != gen or self._closed:
                        continue  # a reposition landed mid-lookahead
                    if track is None:
                        break  # queue exhausted → finalize
                    if (self._last_failed is not None
                            and _same_track(track, self._last_failed)):
                        # The consumer's skip listener did not remove the
                        # failed item — ending beats a decode-fail spin.
                        _log.warning(
                            "Flow session %s: lookahead re-returned the "
                            "just-skipped track %r — ending the flow",
                            self._session_id, getattr(track, "title", "?"))
                        break
                    offset_ms, emit, repos = 0, True, False
                self._last_failed = None
                completed = await self._stream_one(
                    gen, track, offset_ms, emit_boundary=emit,
                    reposition=repos)
                if completed:
                    prev = track
            if not self._closed:
                await self._finalize()
        except asyncio.CancelledError:
            raise
        except FlowEncodeError:
            _log.error("Flow session %s: encoder died — closing",
                       self._session_id, exc_info=True)
            await self.close()
        except Exception:
            _log.exception("Flow session %s: pump crashed — closing",
                           self._session_id)
            await self.close()

    async def _lookahead(self, prev: Any) -> Any | None:
        """Re-resolve the effective next at the moment the next decode must
        start (R14 — queue edits before the boundary reposition the
        lookahead). Any failure ends the flow cleanly rather than stalling."""
        try:
            return await self._next_track_fn(prev)
        except Exception:
            _log.warning("Flow session %s: lookahead failed — ending the flow",
                         self._session_id, exc_info=True)
            return None

    async def _stream_one(self, gen: int, track: Any, offset_ms: int, *,
                          emit_boundary: bool, reposition: bool) -> bool:
        """Decode one track and splice it into the stitch. Returns True when
        the flow should proceed to the lookahead (clean EOF, or a decode
        failure that emitted a skip event); False when this work was
        superseded (generation bumped / session closed)."""
        try:
            resolved = await self._resolve_source(track)
        except Exception:
            _log.warning("Flow: source resolution raised for %r",
                         getattr(track, "title", "?"), exc_info=True)
            resolved = None
        if self._gen != gen or self._closed:
            return False
        if resolved is None:
            await self._skip(track, "unresolvable source")
            return True
        source, headers = resolved
        try:
            dec = self._decoder_factory(source, headers, offset_ms)
        except Exception as exc:
            await self._skip(track, f"decoder start failed: {exc}")
            return True
        self._active_decoder = dec
        boundary_pending = True
        try:
            while True:
                try:
                    chunk = await self._read_bounded(dec)
                except Exception as exc:
                    if self._gen != gen or self._closed:
                        return False
                    await self._skip(track, f"decode failed: {exc}")
                    return True
                if self._gen != gen or self._closed:
                    return False
                if not chunk:
                    return True  # clean EOF — sample-exact splice point
                await self._pace(len(chunk), gen)
                if self._gen != gen or self._closed:
                    return False
                if boundary_pending:
                    # The encode stream is crossing this track's boundary
                    # offset RIGHT NOW (first byte about to be fed).
                    boundary_pending = False
                    await self._record_boundary(track, emit=emit_boundary,
                                                reposition=reposition)
                await self._encoder.feed(chunk)
                self._fed_bytes += len(chunk)
        finally:
            if self._active_decoder is dec:
                self._active_decoder = None
            try:
                await dec.close()
            except Exception:
                _log.warning("Flow: decoder close failed", exc_info=True)

    async def _read_bounded(self, dec: Any) -> bytes:
        """One decoder read under the stall watchdog: a source that produces
        zero PCM (no bytes, no EOF) for ``decode_stall_s`` while the session
        is actively pumping raises ``FlowDecodeError`` — the caller routes it
        through the existing decode-failure path (server-side skip event +
        splice next track), and ``_stream_one``'s finally closes the stalled
        decoder. A paused session legitimately stops reading, so the bound
        applies only while actively pumping: the read parks on the resume
        event first, and a timeout that lands while paused is retried, never
        turned into a stall verdict."""
        while True:
            if not self._resume_evt.is_set():
                await self._resume_evt.wait()
                continue
            try:
                return await asyncio.wait_for(dec.read(_PCM_READ_CHUNK),
                                              self._decode_stall_s)
            except asyncio.TimeoutError:
                if self.paused:
                    continue  # not actively pumping — no verdict, re-read
                raise FlowDecodeError(
                    f"stalled — no PCM for {self._decode_stall_s:.1f}s")

    async def _pace(self, next_chunk_bytes: int, gen: int) -> None:
        """Bounded run-ahead: block until feeding ``next_chunk_bytes`` keeps
        the encode clock within ``run_ahead_s`` of the (pause-aware) playback
        clock. Driven entirely by the injectable clock/sleep — tests use
        fakes, production sleeps are at most ~one chunk duration."""
        while not self._closed and self._gen == gen:
            if not self._resume_evt.is_set():
                await self._resume_evt.wait()
                continue
            lead_s = ((self._fed_bytes + next_chunk_bytes) / self._bytes_per_s
                      - self._elapsed_s())
            excess = lead_s - self._run_ahead_s
            if excess <= 0:
                return
            await self._sleep(excess)

    def _elapsed_s(self) -> float:
        if self._started_at is None:
            return 0.0
        now = self._paused_at if self._paused_at is not None else self._clock()
        return now - self._started_at - self._paused_accum

    async def _record_boundary(self, track: Any, *, emit: bool,
                               reposition: bool) -> None:
        offset_samples = self._fed_bytes // self._frame_bytes
        self._boundaries.append((offset_samples, track))
        if not emit:
            return  # the initial track: its start is the dispatch, not a boundary
        event = FlowBoundary(
            track=track,
            offset_samples=offset_samples,
            offset_ms=int(offset_samples * 1000 // self._sample_rate),
            reposition=reposition,
        )
        _log.info("Flow session %s: boundary → %r @ %dms%s",
                  self._session_id, getattr(track, "title", "?"),
                  event.offset_ms, " (reposition)" if reposition else "")
        for cb in list(self._boundary_listeners):
            try:
                res = cb(event)
                if inspect.isawaitable(res):
                    await res
            except Exception:
                _log.warning("Flow: boundary listener raised", exc_info=True)

    async def _skip(self, track: Any, reason: str) -> None:
        """Server-side skip: the flow never stalls on a bad track. Listeners
        are awaited so the consumer can drop the failed queue item before the
        lookahead re-resolves (U10 also turns this into TrackSkippedEvent)."""
        self._last_failed = track
        _log.warning("Flow session %s: skipping %r server-side (%s)",
                     self._session_id, getattr(track, "title", "?"), reason)
        for cb in list(self._skip_listeners):
            try:
                res = cb(track, reason)
                if inspect.isawaitable(res):
                    await res
            except Exception:
                _log.warning("Flow: skip listener raised", exc_info=True)

    async def _finalize(self) -> None:
        """Queue exhausted: flush the encoder, drain its tail into the
        out-buffer, then mark the stream ended (natural end)."""
        _log.info("Flow session %s: queue exhausted — finalizing the stream",
                  self._session_id)
        try:
            await self._encoder.finalize()
        except Exception:
            _log.warning("Flow session %s: encoder finalize failed",
                         self._session_id, exc_info=True)
        reader = self._enc_reader_task
        if reader is not None:
            try:
                await reader  # exits once the encoder reports EOF
            except asyncio.CancelledError:
                return  # close() raced us and owns the teardown
        self._ended = True
        self._out_data.set()  # wake the consumer so it can drain and finish
        for cb in list(self._ended_listeners):
            try:
                res = cb()
                if inspect.isawaitable(res):
                    await res
            except Exception:
                _log.warning("Flow: ended listener raised", exc_info=True)

    async def _read_encoded(self) -> None:
        """Move encoded bytes into the bounded out-buffer. Blocks for space
        when the consumer lags — with the encoder pipe behind it, that
        back-pressure is what makes the run-ahead budget the memory bound."""
        try:
            while not self._closed:
                chunk = await self._encoder.read(_ENC_READ_CHUNK)
                if not chunk:
                    return  # encoder EOF (post-finalize) — _finalize owns the rest
                while (self._out_bytes + len(chunk) > self._out_cap
                       and not self._closed):
                    self._out_space.clear()
                    await self._out_space.wait()
                if self._closed:
                    return
                self._out_chunks.append(chunk)
                self._out_bytes += len(chunk)
                self._out_data.set()
        except asyncio.CancelledError:
            raise
        except Exception:
            if self._closed:
                return
            _log.exception("Flow session %s: encoder reader crashed — closing",
                           self._session_id)
            # Mirror the pump's FlowEncodeError handler: without the close,
            # _ended/_closed never flip and the HTTP consumer parks forever
            # on the out-buffer wait. close() skips cancelling the current
            # task, so calling it from inside the reader is safe.
            await self.close()


# ── module-level current-session registry (U10 drives it) ────────────────────

_current_session: FlowSession | None = None


def create_flow_session(first_track: Any, **kwargs: Any) -> FlowSession:
    """Create, register, and START the flow session (requires a running event
    loop). SINGLE-SESSION resource: a still-open previous session is
    superseded — closed fire-and-forget so its subprocesses are reaped."""
    global _current_session
    old = _current_session
    session = FlowSession(first_track, **kwargs)
    _current_session = session
    if old is not None and not old.closed:
        _log.info("Flow: superseding session %s with %s",
                  old.session_id, session.session_id)
        _spawn(old.close())
    session.start()
    return session


def get_flow_session(session_id: str) -> FlowSession | None:
    """The current session iff ``session_id`` names it and it is still open
    (the flow route's lookup — a wrong or stale id reads as no session)."""
    s = _current_session
    if s is not None and s.session_id == session_id and not s.closed:
        return s
    return None


def current_flow_session() -> FlowSession | None:
    s = _current_session
    return s if (s is not None and not s.closed) else None


async def close_flow_session() -> None:
    """Close and unregister the current session (idempotent)."""
    s = _current_session
    if s is not None:
        await s.close()
