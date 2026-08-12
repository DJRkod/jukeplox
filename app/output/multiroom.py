"""Server-fed multi-room output foundation (2026-08-11 plan U1).

Both the Snapcast and Sendspin backends are **server-fed**: jukeplox runs an
embedded sync server, decodes the single active queue track to one PCM feed,
and pushes that feed into the server, which syncs many clients. This module is
the shared *shape* both backends build on — deliberately NOT one shared feed
producer (the sinks genuinely differ: Snapcast's ffmpeg writes straight to a
``tcp://`` source, Sendspin's ffmpeg writes PCM to a pipe that an in-process
loop pushes). What is shared:

1. **The master → per-client volume redistribution helper** (pure, intrinsic —
   NO dependency on any library method): ``redistribute_group_volume``. Snapcast
   applies it via a ``Client.SetVolume`` fan-out; Sendspin's own group-volume
   uses the same shape. This is the R5 "proportional scaling" contract.
2. **The PCM feed-arg builder + spawn/reap lifecycle** (``build_pcm_feed_args`` /
   ``PcmFeed``): source → ``48000:16:2`` PCM to a configurable sink, credentials
   off the ffmpeg argv (http sources fed on stdin, the flow.py posture), with
   supersede-before-spawn and a first-byte stall watchdog.
3. **The flow-mode advance/outage posture** (``MultiroomBackendBase``): the
   queue+supervisor is the clock (there is no stitched server byte-clock like
   flow.py), a clean track-EOF advances, a mid-track feed death holds (outage) —
   distinguished by ``classify_feed_exit`` on exit status + queue position,
   never "advance on any ffmpeg exit". Plus the echo-guarded volume state and
   ``_stream_url_base()`` gating helper.
4. **The zoning control contract** (``ZONING_CONTRACT`` + ``assert_zoning_contract``):
   an ENFORCED structural contract so ``SnapcastBackend`` and ``SendspinBackend``
   cannot drift apart — a renamed/missing zoning method fails at each backend's
   own unit boundary, not at U9 integration (targets the radio-era
   "unenforced contract" multi-agent failure mode).

``app/output/base.py``'s ``AbstractOutputBackend`` Protocol is UNCHANGED — the
zoning methods are hasattr-guarded optional capabilities, exactly like
``arm_next``. This module has **no** ``snapcast``/``aiosendspin`` dependency.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Any, Awaitable, Callable

# Genuinely-shared feed plumbing lives in flow.py; reuse it rather than fork.
# ``_default_resolve_source`` is the sanctioned reuse point (plan Key Decisions):
# direct local-path / provider-URL-with-headers, HTTP sources fed on stdin so
# credentials never reach the ffmpeg argv. The small proc helpers are shared to
# keep one teardown discipline across every server-fed feed.
from app.output.flow import (
    _default_resolve_source as resolve_feed_source,
    _drain_stderr,
    _is_http_source,
    _redact,
    _terminate_proc,
)
from app.output.base import ECHO_GUARD_WINDOW, echo_guard_active

_log = logging.getLogger(__name__)

# The single per-backend feed format. 16-bit satisfies BOTH backends (Sendspin
# is 16-bit only); 48 kHz is the Sendspin/Opus-friendly rate and snapserver
# accepts it directly. Each library encodes its own FLAC/Opus downstream.
FEED_SAMPLE_RATE = 48000
FEED_CHANNELS = 2
FEED_BYTES_PER_SECOND = FEED_SAMPLE_RATE * FEED_CHANNELS * 2  # s16le

# First-byte stall watchdog for a pipe-consumed feed: if the ffmpeg feed
# produces no PCM within this window the feed is treated as failed (→ the
# concrete backend routes it through its outage/skip path). Deliberately only
# guards the FIRST byte — a live, producing feed is paced by the sink's own
# backpressure (Sendspin ``sleep_to_limit_buffer`` / snapserver ``idle_threshold``),
# and a mid-stream stall on a finite track surfaces as a clean/early ffmpeg exit.
FEED_FIRST_BYTE_STALL_S = 30.0

_STOP_GRACE_S = 2.0  # SIGTERM grace before SIGKILL (airplay.py / flow.py discipline)


# ── master → per-client volume redistribution (pure, intrinsic) ──────────────


def group_volume(members: list[float]) -> float:
    """A group's volume is the **mean** of its member volumes (0.0 for an empty
    group). This is the single definition of "group volume" both backends use —
    there is no wire ``Group.SetVolume`` on Snapcast, so a group's level is
    always a function of its clients."""
    return sum(members) / len(members) if members else 0.0


def redistribute_group_volume(
    current: list[float],
    target: float,
    *,
    lo: float = 0.0,
    hi: float = 1.0,
    max_iter: int = 64,
    eps: float = 1e-9,
) -> list[float]:
    """Set a group's mean volume to ``target`` by scaling members
    **proportionally**, spilling any volume that clamps at ``[lo, hi]`` onto the
    still-movable members (R5 "proportional scaling"). Pure — returns a NEW list,
    never mutates the input, and calls no library method.

    Semantics (why proportional, not additive-delta):
    - The group mean lands on ``target`` (clamped into ``[lo, hi]``) exactly,
      unless every member is pinned at a bound in the needed direction.
    - Ratios among the still-movable members are preserved — a client at 20% and
      one at 60% keep their 1:3 relationship as the master rises, until one hits
      the ceiling, at which point its excess spills proportionally onto the rest.
    - Spill is bounded: a member that clamps is fixed and dropped from further
      redistribution, so the loop strictly shrinks the movable set and always
      terminates (the ``max_iter`` cap is a belt-and-suspenders backstop, not the
      primary terminator).
    - Degenerate up-scale from an all-zero movable set (no ratio to preserve)
      falls back to an equal additive share — the only sensible reading of
      "raise a group that is currently silent".
    """
    n = len(current)
    if n == 0:
        return []
    target = min(max(target, lo), hi)
    target_total = target * n
    result = [min(max(v, lo), hi) for v in current]
    fixed = [False] * n

    for _ in range(max_iter):
        deficit = target_total - sum(result)
        if abs(deficit) < eps:
            break
        if deficit > 0:
            movable = [i for i in range(n) if not fixed[i] and result[i] < hi - eps]
        else:
            movable = [i for i in range(n) if not fixed[i] and result[i] > lo + eps]
        if not movable:
            break  # everyone is pinned against the bound — cannot reach target
        movable_sum = sum(result[i] for i in movable)
        if movable_sum > eps:
            # Proportional scale of the movable members to absorb the deficit.
            scale = max(0.0, (movable_sum + deficit) / movable_sum)
            for i in movable:
                nv = result[i] * scale
                if nv >= hi - eps:
                    nv, fixed[i] = hi, True
                elif nv <= lo + eps:
                    nv, fixed[i] = lo, True
                result[i] = nv
        else:
            # movable_sum ≈ 0: no ratio to preserve (all movable at 0 while we
            # raise). Distribute the deficit equally — the only sensible reading.
            share = deficit / len(movable)
            for i in movable:
                nv = result[i] + share
                if nv >= hi - eps:
                    nv, fixed[i] = hi, True
                result[i] = nv
    return result


# ── PCM feed: source → 48000:16:2 to a configurable sink ─────────────────────


def build_pcm_feed_args(
    source: str,
    headers: dict | None,
    *,
    sink: str,
    start_offset_ms: int = 0,
    sample_rate: int = FEED_SAMPLE_RATE,
    channels: int = FEED_CHANNELS,
    realtime: bool = False,
) -> list[str]:
    """ffmpeg argv decoding ``source`` to ``sample_rate:16:channels`` PCM written
    to ``sink`` — ``"pipe:1"`` (Sendspin: Python reads the PCM) or a ``tcp://…``
    URL (Snapcast: ffmpeg writes straight to the snapserver source, Python never
    sees the bytes).

    Credential hygiene (flow.py posture): ``headers`` NEVER joins argv, and
    neither does an http(s) URL — a Plex part URL carries ``?X-Plex-Token=`` and
    Jellyfin/Emby auth rides an ``Authorization`` header, both visible in the
    process list if passed to ffmpeg. http(s) sources are fetched by the caller's
    httpx feeder (auth in the REQUEST) and fed on stdin (``-i pipe:0``); local
    paths carry no credentials and stay direct argv. A test asserts no credential
    reaches this argv.

    ``realtime`` adds ``-re`` so ffmpeg paces the output at playback speed — the
    Snapcast ``tcp://`` source wants this (paced feed + ``idle_threshold`` gives
    gapless-across-gaps); the pipe-consumed Sendspin feed is paced by the sink's
    own buffer backpressure and does not.

    Output-side ``-ss`` (after ``-i``) mirrors flow.py/airplay.py: input-side
    ``-ss`` on an HTTP source triggers a Range-probe storm."""
    del headers  # never on argv — the caller's httpx feeder carries auth
    args = ["ffmpeg"]
    if realtime:
        args += ["-re"]
    if _is_http_source(source):
        args += ["-loglevel", "error", "-i", "pipe:0"]  # stdin IS the input
    else:
        args += ["-nostdin", "-loglevel", "error", "-i", source]
    if start_offset_ms > 0:
        args += ["-ss", f"{start_offset_ms / 1000:.3f}"]
    args += [
        "-acodec", "pcm_s16le",
        "-ac", str(channels),
        "-ar", str(sample_rate),
        "-f", "s16le",
        sink,
    ]
    return args


class FeedStalled(Exception):
    """The feed produced no first byte within the stall window (pipe sink)."""


class PcmFeed:
    """One ffmpeg feed subprocess with the shared spawn/reap/stall discipline.

    Two sink modes:
    - ``consume_stdout=True`` (Sendspin): ffmpeg writes PCM to stdout; the caller
      pulls it with ``read()``. The first-byte stall watchdog applies here.
    - ``consume_stdout=False`` (Snapcast): ffmpeg's output target is the
      ``tcp://`` sink in its argv; stdout is unused. The caller only ``wait()``s
      on exit — snapserver readiness and byte-flow are proven separately (U10).

    Credentials for http(s) sources ride the httpx feeder (stdin), never argv.
    ``close()`` is idempotent and reaps every child task/process (airplay.py /
    flow.py teardown discipline)."""

    def __init__(
        self,
        source: str,
        headers: dict | None,
        *,
        sink: str,
        consume_stdout: bool,
        start_offset_ms: int = 0,
        realtime: bool = False,
        stall_s: float = FEED_FIRST_BYTE_STALL_S,
        label: str = "feed",
    ) -> None:
        self._source = source
        self._headers = dict(headers or {})
        self._pipe_source = _is_http_source(source)
        self._consume_stdout = consume_stdout
        self._stall_s = stall_s
        self._label = label
        self._args = build_pcm_feed_args(
            source, headers, sink=sink, start_offset_ms=start_offset_ms,
            realtime=realtime,
        )
        self._proc: Any = None
        self._stderr_task: asyncio.Task | None = None
        self._feeder_task: asyncio.Task | None = None
        self._closed = False
        self._first_byte_seen = False
        self._source_failed = False

    @property
    def args(self) -> list[str]:
        return list(self._args)

    async def start(self) -> None:
        if self._proc is not None or self._closed:
            return
        self._proc = await asyncio.create_subprocess_exec(
            *self._args,
            stdin=asyncio.subprocess.PIPE if self._pipe_source else None,
            stdout=asyncio.subprocess.PIPE if self._consume_stdout
            else asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        if self._proc.stderr is not None:
            self._stderr_task = asyncio.create_task(
                _drain_stderr(self._proc.stderr, self._label))
        if self._pipe_source:
            self._feeder_task = asyncio.create_task(self._feed_source(self._proc))

    async def read(self, n: int) -> bytes:
        """Read up to ``n`` PCM bytes (consume_stdout mode). Raises
        ``FeedStalled`` if the first byte never arrives within the stall window,
        and surfaces a mid-stream source failure as an exception rather than a
        clean EOF (so a truncated feed can't masquerade as end-of-track)."""
        if not self._consume_stdout:
            raise RuntimeError("PcmFeed(consume_stdout=False) has no readable stdout")
        if self._proc is None:
            await self.start()
        if self._closed:
            return b""
        try:
            if not self._first_byte_seen:
                chunk = await asyncio.wait_for(self._proc.stdout.read(n), self._stall_s)
            else:
                chunk = await self._proc.stdout.read(n)
        except asyncio.TimeoutError as exc:
            raise FeedStalled(
                f"{self._label}: no PCM within {self._stall_s:.0f}s") from exc
        if chunk:
            self._first_byte_seen = True
            return chunk
        rc = await self._proc.wait()
        if rc != 0 and not self._closed:
            raise FeedStalled(f"{self._label}: ffmpeg exited {rc}")
        if self._source_failed and not self._closed:
            raise FeedStalled(f"{self._label}: source fetch failed mid-stream")
        return b""

    async def wait(self) -> int:
        """Wait for the ffmpeg process to exit and return its code. Used by the
        tcp-sink (Snapcast) path, which does not read stdout."""
        if self._proc is None:
            await self.start()
        return await self._proc.wait()

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode if self._proc is not None else None

    async def _feed_source(self, proc: Any) -> None:
        """Stream an http(s) source into ffmpeg stdin — credentials ride the
        httpx REQUEST, never argv (flow.py's FFmpegPCMDecoder feeder)."""
        import httpx
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=self._stall_s + 30.0,
                                  write=None, pool=None),
            follow_redirects=True,
        )
        try:
            resp = await client.send(
                client.build_request("GET", self._source, headers=self._headers),
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
            pass  # ffmpeg exited early; its rc surfaces through read()/wait()
        except Exception:
            self._source_failed = True
            _log.warning("Multiroom %s: source fetch failed for %s",
                         self._label, _redact(self._source), exc_info=True)
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
        self._closed = True
        if self._feeder_task is not None and not self._feeder_task.done():
            self._feeder_task.cancel()
            try:
                await self._feeder_task
            except asyncio.CancelledError:
                pass
        self._feeder_task = None
        await _terminate_proc(self._proc, f"{self._label}-ffmpeg")
        if self._stderr_task is not None and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
        self._stderr_task = None


# ── zoning control contract (enforced structurally, see U1 tests) ────────────

# The hasattr-guarded zoning capability every server-fed backend must expose
# with the SAME names + signatures. Kept as an explicit spec (name → ordered
# parameter names, excluding ``self``) so drift on either backend fails a
# structural test at that backend's own unit boundary — not at U9 integration.
# ``base.py``'s Protocol is untouched; these live outside it like ``arm_next``.
ZONING_CONTRACT: dict[str, tuple[str, ...]] = {
    "supports_zoning": (),                       # -> bool (embedded/external capable)
    "list_zones": (),                            # -> async list[dict] client/group tree
    "set_client_volume": ("client_id", "level"),  # async
    "set_client_mute": ("client_id", "muted"),    # async
    "set_group_mute": ("group_id", "muted"),       # async
    "set_group_volume": ("group_id", "level"),     # async (client fan-out)
    "assign_client_to_group": ("client_id", "group_id"),  # async
    "can_manage_topology": (),                   # -> bool (embedded full / external read+assign)
}


def zoning_signature(method: Callable) -> tuple[str, ...]:
    """The ordered non-self parameter names of a zoning method, for contract
    comparison. Ignores ``*args``/``**kwargs`` and default values — the contract
    is about the positional call shape, not annotations."""
    sig = inspect.signature(method)
    names = [p for p in sig.parameters if p != "self"]
    return tuple(names)


def assert_zoning_contract(backend_cls: type, *, name: str | None = None) -> None:
    """Raise ``AssertionError`` if ``backend_cls`` does not expose every
    ``ZONING_CONTRACT`` method with the exact ordered parameter names. Called by
    the U1 contract test against ``SnapcastBackend``/``SendspinBackend`` so a
    renamed or re-signatured zoning method fails at that backend's unit — the
    enforcement that closes the "unenforced cross-unit contract" failure mode."""
    label = name or backend_cls.__name__
    for method_name, params in ZONING_CONTRACT.items():
        member = getattr(backend_cls, method_name, None)
        assert member is not None, (
            f"{label} is missing zoning-contract method {method_name!r}")
        assert callable(member), (
            f"{label}.{method_name} is not callable")
        got = zoning_signature(member)
        assert got == params, (
            f"{label}.{method_name} signature drift: expected params {params}, "
            f"got {got}")


# ── flow-mode advance/outage posture + volume state (base mixin) ─────────────


def classify_feed_exit(returncode: int | None, *, expected_end: bool) -> str:
    """Classify a feed ffmpeg exit as ``"advance"`` or ``"outage"``.

    The queue+supervisor is the clock (there is no stitched server byte-clock);
    advance authority is the *track boundary*, not "any ffmpeg exit". A clean
    exit (rc 0) at the point the track was expected to end advances; a non-zero
    exit, or ANY exit that was NOT expected (mid-track feed death), holds as an
    outage — the concrete backend raises ``DeviceLostError`` so the supervisor's
    outage-hold + auto-resume path runs instead of draining the queue.

    ``expected_end`` is the caller's evidence that the feed reached the track's
    end (its own paced-EOF / duration signal), NOT merely "the process exited"."""
    if expected_end and (returncode == 0 or returncode is None):
        return "advance"
    return "outage"


class MultiroomBackendBase:
    """Shared state + posture for a server-fed backend. Concrete backends
    (``SnapcastBackend``, ``SendspinBackend``) mix this in and supply their own
    sink (tcp / push) via the ``PcmFeed`` utility — this is NOT a shared feed
    producer, only shared posture + state.

    Provides: echo-guarded master-volume state (the ``volume_changed`` pipeline
    contract every backend shares), the ``_stream_url_base()`` gating helper, the
    feed-exit classifier, and the master → per-client redistribution wiring
    (which calls the pure ``redistribute_group_volume`` helper)."""

    def __init__(self, advance_cb: Callable[[], Awaitable[Any]] | None = None) -> None:
        self._advance_cb = advance_cb
        self._volume: float = 0.5
        self._vol_last_set: float = 0.0
        self._is_playing: bool = False
        # Confirmed-start token (the OutputSessionSupervisor contract every
        # backend obeys): captured in play() from current_token(), fired once
        # when the feed is up. Without it the 12s confirmation deadline expires
        # and healthy server-fed playback is misclassified as an outage.
        self._confirm_token: int | None = None

    # ── output-session supervisor integration (shared) ──────────────────────

    def _capture_confirm_token(self) -> None:
        """Grab the current dispatch's confirmation token at play() start —
        the same hop direct/chromecast/dlna make (they read
        ``current_token()``)."""
        from app.output import session
        self._confirm_token = session.get_supervisor().current_token()

    def _confirm_started(self) -> None:
        """One-shot confirmed-start once the feed is up (the data-plane-proxy
        signal for a server-fed backend: the feed process is spawned and, for a
        pipe-consumed feed, producing). Mirrors every other backend's
        ``notify_confirmed`` so the play is counted and the confirmation
        deadline is satisfied — a 0-client backend is legitimately audible-less,
        NOT an outage (R15)."""
        token = self._confirm_token
        if token is None:
            return
        self._confirm_token = None
        from app.output import session
        session.notify_confirmed(token)

    def _notify_feed_outage(self, reason: str) -> None:
        """Route a mid-track feed death to the supervisor's outage-hold (so the
        queue is held, not drained and not dead-stalled). MUST be called on the
        loop — the feed watcher already runs there. Replaces raising
        ``DeviceLostError`` from a detached task, which nobody awaits so the
        hold never runs."""
        from app.output import session
        session.notify_outage(reason)

    def _spawn_advance(self) -> None:
        """Fire the queue advance on a FRESH loop task, never inline from the
        feed task. Firing ``await self._advance_cb()`` inline re-enters this
        backend's ``play()`` (advance → dispatch → router.play → play →
        _teardown_feed), which cancels-and-awaits the very task it runs inside —
        a reentrancy deadlock on every gapless boundary (AE1). Scheduling on a
        new task lets the feed task complete first, so the subsequent teardown
        cancels the OLD (now-finished) task, not itself. This is the same
        separate-task hop the device backends make from their EOS callbacks."""
        cb = self._advance_cb
        if cb is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(cb())
        task.add_done_callback(self._log_advance_exc)

    @staticmethod
    def _log_advance_exc(task: "asyncio.Task") -> None:
        if not task.cancelled() and task.exception() is not None:
            _log.error("multiroom advance raised: %s", task.exception(),
                       exc_info=task.exception())

    # ── volume / echo-guard (shared pipeline contract) ──────────────────────

    async def get_volume(self) -> float:
        return self._volume

    def _stamp_volume_write(self) -> None:
        """Stamp the echo-guard window immediately before a server-initiated
        volume write, so the server's own confirmation event is suppressed
        (base.py ``echo_guard_active``) and admin sliders don't snap back."""
        self._vol_last_set = time.monotonic()

    def _echo_guard_active(self) -> bool:
        return echo_guard_active(self._vol_last_set)

    # ── stream-base gating (degrade like flow when unset) ───────────────────

    @staticmethod
    def _stream_url_base() -> str:
        """The device-reachable proxy base (STREAM_BASE_URL / specific BIND_HOST),
        or "" — single-sourced from state so the feed can never disagree with the
        rest of the app about what "reachable base" means. A Snapcast tcp feed and
        the snapserver bind both need this; Sendspin does NOT (clients connect to
        the in-process server directly — see U7)."""
        from app import state
        return state._stream_url_base()

    # ── flow-mode advance / outage ──────────────────────────────────────────

    @property
    def is_playing(self) -> bool:
        return self._is_playing

    def is_connected(self) -> bool:
        """Whether the backend's server/control is live. Named accessor so the
        admin layer never reaches into the private ``_connected`` attribute."""
        return bool(getattr(self, "_connected", False))

    def classify_feed_exit(self, returncode: int | None, *, expected_end: bool) -> str:
        return classify_feed_exit(returncode, expected_end=expected_end)

    # ── master → per-client redistribution wiring ───────────────────────────

    def redistribute(
        self, current: list[float], target: float, *, lo: float = 0.0, hi: float = 1.0
    ) -> list[float]:
        """Backend-facing wrapper over the pure helper — one place both backends
        call so the master-volume semantics are identical."""
        return redistribute_group_volume(current, target, lo=lo, hi=hi)
