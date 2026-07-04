"""AirPlay output backend — Music Assistant cliap2 / cliraop subprocess wrapper.

Replaces the previous pyatv-based implementation, which produced silent
audio on JBL Charge 5 Wi-Fi SE, WiiM Pro, and other strict third-party
AirPlay 2 receivers (SNTP-vs-PTP timing mismatch + HKDF audio-key
incompatibility — see
docs/solutions/integration-issues/airplay-2-pyatv-rtp-silent-no-audio-jbl-wiim.md).

Architecture per Music Assistant's open-source provider:
- Audio: Plex HTTP stream → FFmpeg → binary stdin (16-bit 44.1 kHz PCM)
- Commands: newline-delimited KEY=value over a named pipe (set_volume,
  pause/resume, metadata)
- Status: cliap2 stderr → coroutine reader → advance_cb on EOS, log on errors
- Volume callback: speaker → DACP HTTP server (app/output/dacp.py) →
  WebSocket broadcast

Binaries are vendored at vendor/airplay/ and installed into /usr/local/bin
by the Dockerfile. Override the path via JUKEPLOX_CLIAP2_BIN /
JUKEPLOX_CLIRAOP_BIN for local development or test.

Reference: https://github.com/music-assistant/server/tree/dev/music_assistant/providers/airplay
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
import re
import secrets
import signal
import tempfile
import time
import uuid
from typing import Any

from app.output.base import AdvanceCallback, DeviceNotReadyError, OutputDevice
from app.models import Track

_log = logging.getLogger(__name__)


_CLIAP2_BIN_DEFAULT = "/usr/local/bin/cliap2"
_CLIRAOP_BIN_DEFAULT = "/usr/local/bin/cliraop"

# Streaming constants per Music Assistant's AIRPLAY_PCM_FORMAT (constants.py).
# cliap2 expects raw 16-bit signed little-endian 44.1 kHz stereo PCM on stdin.
_PCM_SAMPLE_RATE = 44100
_PCM_CHANNELS = 2
_PCM_FORMAT = "s16le"

# Latency budgets per MA constants.py: a 1s output buffer absorbs WiFi jitter
# without making transport controls feel sluggish; 500ms is the default
# session-establishment grace before cliap2 starts complaining about pair-verify.
_DEFAULT_LATENCY_MS = 1000
_DEFAULT_SESSION_ESTABLISHMENT_LATENCY_MS = 500

# Stop-grace before SIGKILL when the subprocess doesn't exit on SIGTERM.
# cliap2's RTSP TEARDOWN typically takes ~200-400ms; 2s leaves headroom.
_STOP_GRACE_S = 2.0

# Routine cliraop/cliap2 stderr lines that fire every second or every 20s
# during normal playback. We demote them to DEBUG so production INFO logs
# stay scan-able. The pattern matches by the source line tag emitted by
# the binary (e.g. "main:695") which is stable across sessions.
_AIRPLAY_NOISE_PATTERN = re.compile(
    r"\bmain:695 elapsed milliseconds:"  # per-second heartbeat
    r"|\bCmdPipeReaderThread:186 .*sending keepalive packet"  # ~20s keepalive
    r"|\braopcl_send_chunk:590"  # ~60s chunk health probe
    r"|\braopcl_accept_frames:(?:418|426)"  # per-stream-restart marker
)

# Discovery-time probe: spawn cliap2 with a silent input and watch stderr
# for known failure markers within this window. 8s covers cliap2 startup +
# HAP pair-verify + initial RTSP setup (~600ms typical) with margin for
# slow networks. Per plan D3.
_AIRPLAY_PROBE_WINDOW_S = 8.0

# cliap2 stderr signals that mean "this device won't play AP2 audio". When
# any of these match within the probe window, the probe persists `ap1`.
# `[ WARN] Not using AirPlay 2` is the WiiM-shape failure (cliap2 refuses
# internally despite TXT advertising AP2). `[FATAL]` / `[ERROR]` cover the
# auth/transport/HAP failures cliap2 emits before audio frames start.
#
# Format note: cliap2 1.5 right-pads log levels to a 5-character field,
# so 4-char levels carry a leading space (`[ WARN]`, `[ INFO]`, `[ SPAM]`)
# and 5-char ones are unpadded (`[DEBUG]`, `[FATAL]`, `[ERROR]`). The
# `\s*` accommodates both widths so a future cliap2 rebuild that changes
# the field width can't silently break this detection. The prior form
# `\[WARN\]` matched only the unpadded variant and silently misclassified
# every real `[ WARN] Not using AirPlay 2` cliap2 1.5 actually emits —
# fixed 2026-06-07 (TrueNAS log diagnosis).
_AIRPLAY_PROBE_FAILURE_RE = re.compile(
    r"\[\s*FATAL\s*\]"
    r"|\[\s*WARN\s*\][^\n]*Not using AirPlay 2"
    r"|\[\s*ERROR\s*\]"
)

# Cap on concurrent probe subprocesses across all devices. Per-device locks
# in AirPlayBackend dedupe same-device probes; this semaphore caps the
# cross-device fan-out so a fresh-boot discovery of N AP2-capable speakers
# doesn't spawn N concurrent cliap2 subprocesses each holding an RTSP
# attempt for 8 seconds. 2 is a balance: parallelism for the typical 1-2
# device install, throttling for the venue-scale edge case.
_AIRPLAY_PROBE_CONCURRENCY = 2

# Seconds to push -ntpstart into the future per binary. This is the
# perceived "click to first audio sample" delay — the smallest value the
# binary needs to complete its own session establishment before the
# speaker's audio clock reaches t0.
#
# cliap2 (AP2) explicitly warns "ntpstart time too soon ... increase
# ntpstart by at least 1806 ms to prevent loss of audio" — first ~2s of
# every track gets dropped otherwise. 4s gives ~2s of headroom on top of
# cliap2's reported floor plus subprocess spawn / OS scheduling jitter.
#
# cliraop (AP1/RAOP) doesn't have a pair-verify step and handles its own
# session-establishment-latency (500ms default) plus a 1250ms player
# buffer internally — adding our own 4s on top stacks to a ~5s perceived
# delay on track-start. Verified against WiiM Pro logs from 2026-06-07
# 07:41:08Z. 0s here lets cliraop's internal buffers be the only delay
# (audio actually starts ~1.5-2s after we call play()).
_AIRPLAY_NTP_STARTUP_DELAY_S: dict[str, int] = {
    "cliap2": 4,
    "cliraop": 0,
}


def _cliap2_bin() -> str:
    return os.environ.get("JUKEPLOX_CLIAP2_BIN", _CLIAP2_BIN_DEFAULT)


def _cliraop_bin() -> str:
    return os.environ.get("JUKEPLOX_CLIRAOP_BIN", _CLIRAOP_BIN_DEFAULT)


def _binaries_available() -> bool:
    """True only when both cliap2 and cliraop are present and executable.

    Called dynamically rather than cached at module import so tests can
    monkeypatch the JUKEPLOX_*_BIN env vars without reloading the module.
    """
    return os.access(_cliap2_bin(), os.X_OK) and os.access(_cliraop_bin(), os.X_OK)


def _format_txt_kv(txt: dict[str, str]) -> str:
    """Format avahi TXT records as the quoted-space kv string cliap2 expects.

    cliap2 1.5 parses --txt with a strict format: each KV pair must be
    literally quoted (`"key=value"`) and pairs are separated by spaces.
    The comma-join shape (`am=X,cn=Y`) makes cliap2 abort at startup with
    FATAL "Keyval string must start with a double quote (\\"), not with..."
    before it opens the command pipe — surfacing as a 5s TimeoutError on
    the Python side when O_WRONLY blocks for a read end that never opens.

    Verified by running cliap2 with both formats: comma exits with code
    255 immediately; quoted-space proceeds through socket bind, command
    pipe open, and audio-stream wait.
    """
    return " ".join(f'"{k}={v}"' for k, v in txt.items())


_DEVICEID_NAME_RE = re.compile(r"^([0-9A-Fa-f]{12})@")


def _strip_raop_mac_prefix(name: str) -> str:
    """Return *name* with the `<12-hex-mac>@` prefix removed for display.

    Avahi advertises RAOP service instances as `<12-hex-mac>@<friendly>`
    (e.g. `A1B2C3D4E5F6@WiiM Pro-E5F6`). The MAC prefix is meaningful to
    cliap2 — `_ensure_deviceid` extracts it to synthesize the `deviceid`
    TXT entry — but it's unreadable noise for the admin device picker.
    The picker consumes this stripped form; `_device_addr` still stores
    the raw avahi name so the cliap2 spawn path is unaffected.

    Falls back to the raw input when stripping would leave an empty
    string (a name that is just the prefix and nothing else). An empty
    display label would render as a blank picker entry, which is worse
    than an unattractive hex one — the operator can still identify a
    device by its MAC, but cannot identify a blank.
    """
    stripped = _DEVICEID_NAME_RE.sub("", name)
    return stripped if stripped else name


# Bit 38 of the AirPlay `features` field signals AP2 audio support per the
# OwnTone source. If the bit is set the receiver speaks AirPlay 2 RTSP +
# HAP-encrypted streaming and cliap2 will use the AP2 setup path. If it's
# not set (or `features` is missing entirely) cliap2 logs
# `Not using AirPlay 2 for device 'X' as it does not have required 'features'
# in TXT field` and never opens an RTSP session — bytes flow into cliap2's
# input buffer but never out to the speaker.
_FEATURES_AP2_AUDIO_BIT = 1 << 38


def _use_ap2(txt: dict[str, str]) -> bool:
    """Permissive AP2-capability heuristic from a receiver's avahi TXT records.

    Two signals are treated as positive:
    - `features` (or its `ft` abbreviation) carries bit 38 (AP2 audio).
    - `pk` (public key) is present — only AP2-capable receivers advertise it.

    Permissive on purpose: a positive answer here lets `play()` try cliap2.
    The discovery-time probe (`_probe_device`) and the user-facing
    "No audio?" recovery exist precisely to catch the over-permissive
    cases — devices that advertise AP2 in TXT but cliap2 either refuses
    internally (e.g. "[WARN] Not using AirPlay 2") or streams to a silent
    speaker (the WiiM Pro). The probe's verdict, when present, takes
    precedence over this heuristic via the per-device protocol cache
    consulted in `play()`.

    Returning True here on a device that the probe later marks as `ap1`
    is the expected first-discovery state — the probe persists `"ap1"`
    and subsequent plays bypass cliap2 entirely.
    """
    if "pk" in txt and txt.get("pk"):
        return True
    raw = txt.get("features") or txt.get("ft")
    if not raw:
        return False
    try:
        # `features` is advertised as a comma-separated pair of 32-bit hex
        # words ("0x40000003,0x...") representing the low and high halves
        # of a 64-bit bitfield. Combine and test bit 38.
        parts = [p.strip() for p in raw.split(",")]
        combined = 0
        for i, part in enumerate(parts[:2]):
            combined |= int(part, 0) << (32 * i)
        return bool(combined & _FEATURES_AP2_AUDIO_BIT)
    except (ValueError, AttributeError):
        return False


# Per-device protocol cache key shape. Values: "ap2" (probe-confirmed
# cliap2 works), "ap1" (probe or "No audio?" button marked it cliraop-only),
# or absent (unprobed; `play()` falls back to the `_use_ap2(txt)` heuristic).
# The prefix is namespaced so app/api/admin.py's list_airplay_protocols
# endpoint can `database.get_settings_with_prefix(_AIRPLAY_PROTOCOL_KEY_PREFIX)`
# to bulk-load all verdicts in one round-trip.
_AIRPLAY_PROTOCOL_KEY_PREFIX = "airplay:protocol:"


async def _get_per_device_protocol(device_id: str) -> str | None:
    """Return the cached AP2/AP1 verdict for a device, or None if unprobed.

    Reads from the existing settings table. Lazy-imports `app.database`
    to mirror the existing pattern in this module (avoids the circular
    import that lifting it to module-level would introduce).
    """
    from app import database
    value = await database.get_setting(f"{_AIRPLAY_PROTOCOL_KEY_PREFIX}{device_id}")
    if value in ("ap2", "ap1"):
        return value
    return None


async def _set_per_device_protocol(device_id: str, protocol: str) -> None:
    """Persist the AP2/AP1 verdict for a device.

    `protocol` must be `"ap2"` or `"ap1"`. Invalid values are rejected
    rather than silently coerced — the caller (probe, "No audio?",
    Re-test) always knows which it intends.
    """
    if protocol not in ("ap2", "ap1"):
        raise ValueError(
            f"invalid airplay protocol {protocol!r}; expected 'ap2' or 'ap1'"
        )
    from app import database
    await database.set_setting(
        f"{_AIRPLAY_PROTOCOL_KEY_PREFIX}{device_id}", protocol
    )


# Short-form mDNS TXT keys advertised on `_raop._tcp.local` (the service
# our discovery currently queries) and their long-form equivalents on
# `_airplay._tcp.local` (the AP2 service cliap2 expects). cliap2 inherits
# OwnTone's check that calls `keyval_get(txt, "features")` and rejects
# the device with `Not using AirPlay 2 ... does not have required
# 'features' in TXT field` when only the abbreviated `ft` is present.
# Same shape for `model`/`am`. The `deviceid` field has its own handling
# in `_ensure_deviceid` below — kept separate because deviceid is
# synthesised from the device name when missing, not a simple key rename.
_AIRPLAY_TXT_KEY_EXPANSIONS = {
    "ft": "features",
    "am": "model",
}


def _expand_airplay_txt_keys(txt: dict[str, str]) -> dict[str, str]:
    """Return a new TXT dict with short-form mDNS keys expanded to their
    long-form `_airplay._tcp.local` equivalents so cliap2 finds them.

    Existing meaningful long-form values win — if both `features` and `ft`
    carry values, `features` is preserved unchanged. An empty or missing
    long-form value, however, is replaced by the short-form value if one
    is present, because cliap2's `keyval_get` treats an empty `features=`
    identically to a missing key (the device gets rejected either way).
    Returns a new dict; the input is never mutated.
    """
    out = dict(txt)
    for short, long in _AIRPLAY_TXT_KEY_EXPANSIONS.items():
        if not out.get(long) and short in out:
            out[long] = out[short]
    return out


def _ensure_deviceid(name: str, txt: dict[str, str]) -> dict[str, str]:
    """Return a TXT dict that always carries a `deviceid` for cliap2.

    cliap2 logs `airplay: AirPlay device 'X' is missing a device ID` when
    the TXT dict doesn't include `deviceid`, and the speaker name doesn't
    follow the `<MAC-no-colons>@<friendly>` shape cliap2 expects. On a
    WiiM Pro the avahi-advertised name has the right prefix
    (`A1B2C3D4E5F6@WiiM Pro-E5F6`) but the TXT dict didn't always include
    `deviceid` — extract the MAC from the name prefix, format with the
    colon separators cliap2's RAOP code matches against, and inject so
    the warning goes away and the pair-verify path has the id it needs.

    Returns a new dict; the input is never mutated.
    """
    if "deviceid" in txt:
        return txt
    match = _DEVICEID_NAME_RE.match(name)
    if match is None:
        return txt
    hex_mac = match.group(1).upper()
    formatted = ":".join(hex_mac[i:i + 2] for i in range(0, 12, 2))
    return {**txt, "deviceid": formatted}


def _build_cliap2_args(
    *,
    binary: str,
    name: str,
    host: str,
    port: int,
    txt: dict[str, str],
    ntp_start: int,
    volume_pct: int,
    dacp_id: str,
    active_remote_id: int,
    cmd_pipe_path: str,
    latency_ms: int = _DEFAULT_LATENCY_MS,
    session_establishment_latency_ms: int = _DEFAULT_SESSION_ESTABLISHMENT_LATENCY_MS,
    loglevel: int = 3,
) -> list[str]:
    """Construct the cliap2 invocation. Pure function — no side effects.

    The TXT dict passed by callers is the raw discovery payload from
    `_raop._tcp.local`. Two normalisation steps run before formatting:
    short-form keys (`ft`, `am`) are expanded to the long-form names
    cliap2 reads (`features`, `model`), then `deviceid` is synthesised
    from the avahi service name when absent. Owning the chain here
    means a future caller cannot spawn cliap2 with un-prepared TXT.

    Flag surface verified against `cliap2 1.5 --help` output in the
    deployed binary. `--pipe -` tells cliap2 to read audio from stdin;
    `--command_pipe` is the FIFO for runtime control commands;
    `--ntpstart` is a 64-bit NTP timestamp (see _ntp_now); `--loglevel`
    is a number 0-5.
    """
    prepared_txt = _ensure_deviceid(name, _expand_airplay_txt_keys(txt))
    return [
        binary,
        "--name", name,
        "--hostname", host,
        "--address", host,
        "--port", str(port),
        "--txt", _format_txt_kv(prepared_txt),
        "--ntpstart", str(ntp_start),
        "--volume", str(volume_pct),
        "--loglevel", str(loglevel),
        "--dacp_id", dacp_id,
        "--pipe", "-",
        "--command_pipe", cmd_pipe_path,
        "--latency", str(latency_ms),
        "--session_establishment_latency", str(session_establishment_latency_ms),
    ]


def _build_cliraop_args(
    *,
    binary: str,
    host: str,
    port: int,
    txt: dict[str, str],
    ntp_start: int,
    volume_pct: int,
    dacp_id: str,
    active_remote_id: int,
    cmd_pipe_path: str,
    debug_level: int = 3,
) -> list[str]:
    """Construct the cliraop invocation for an AP1/RAOP-only receiver.

    cliraop's CLI is meaningfully different from cliap2's:
    - single-dash flags (`-port` not `--port`)
    - positional `<player_ip> <filename>` at the end (we always pass `-`
      so cliraop reads PCM from stdin, matching the cliap2 path's
      FFmpeg-to-stdin shape)
    - `-cmdpipe` (not `--command_pipe`) for the MA-protocol metadata/
      command FIFO
    - `-activeremote` is one word (not the `--active_remote` cliap2
      flag — though cliap2 1.5 in fact dropped that flag entirely)
    - individual `-am`/`-et`/`-md`/`-pk`/`-pw` flags instead of cliap2's
      single `--txt "key=value" "key=value"` blob; we forward only the
      fields the receiver actually advertised, so a missing field
      passes through as cliraop's compiled-in default

    Verified against `cliraop --help` output in the deployed binary:
    `usage: cliraop <options> <player_ip> <filename ('-' for stdin)>`.
    """
    args: list[str] = [
        binary,
        "-port", str(port),
        "-volume", str(volume_pct),
        "-dacp", dacp_id,
        "-activeremote", str(active_remote_id),
        "-ntpstart", str(ntp_start),
        "-cmdpipe", cmd_pipe_path,
        "-debug", str(debug_level),
    ]
    # Forward only the TXT fields that have direct cliraop equivalents.
    # Passing an empty `-pk` would make cliraop try to validate an empty
    # key and refuse the connection — skip empties.
    for txt_key, flag in (("am", "-am"), ("et", "-et"), ("md", "-md"),
                          ("pk", "-pk"), ("pw", "-pw")):
        value = txt.get(txt_key)
        if value:
            args.extend([flag, value])
    # Positional speaker IP + filename ('-' = stdin) MUST come last per
    # cliraop's argv parser — flags before positionals.
    args.extend([host, "-"])
    return args


def _build_ffmpeg_args(stream_url: str, start_offset_ms: int = 0) -> list[str]:
    """Construct the ffmpeg invocation that resamples whatever Plex serves
    into the 16-bit 44.1 kHz stereo PCM that cliap2 expects on stdin.

    -nostdin keeps ffmpeg from waiting on a controlling tty; -loglevel error
    suppresses progress chatter so stderr stays focused on real failures.
    The output target `pipe:1` makes ffmpeg write PCM to the inherited stdout
    file descriptor, which is wired to cliap2's stdin by the caller.

    Seek implementation note: `-ss <offset>` is placed AFTER `-i` (output-
    side seek) rather than before (input-side seek). Input-side `-ss` on an
    HTTP source triggers a storm of Range-byte probe requests as ffmpeg
    hunts for the FLAC frame boundary — observed 20+ HTTP requests over
    several seconds during a single seek, which is exactly the
    user-perceived "seek is slow" symptom. Output-side seek issues one
    sequential HTTP GET and decode-and-discards PCM up to the offset.
    For typical seek targets (under a few minutes) the decode-and-discard
    is fast on the CPU and HTTP cost stays bounded.
    """
    args = [
        "ffmpeg",
        "-nostdin",
        "-loglevel", "error",
        "-i", stream_url,
    ]
    if start_offset_ms > 0:
        args += ["-ss", f"{start_offset_ms / 1000:.3f}"]
    args += [
        "-acodec", "pcm_s16le",
        "-ac", str(_PCM_CHANNELS),
        "-ar", str(_PCM_SAMPLE_RATE),
        "-f", _PCM_FORMAT,
        "pipe:1",
    ]
    return args


def _generate_dacp_id() -> str:
    """Placeholder DACP-ID — 16 uppercase hex characters per MA's convention.

    U7 replaces this with a stable id derived from output_dacp_id setting so
    AP2 pair-verify state survives Jukeplox restarts. Until U7 is wired in,
    pair-verify happens fresh on every track which works but is slower."""
    return secrets.token_hex(8).upper()


def _generate_active_remote_id() -> int:
    """Per-session 32-bit Active-Remote token used to authenticate DACP
    callbacks from the speaker. cliap2 passes this in the RTSP SET_PARAMETER
    payload so the speaker echoes it back as an Active-Remote header on
    volume callbacks."""
    return secrets.randbits(32)


def _ntp_now() -> int:
    """Current time as a 64-bit NTP timestamp.

    Format matches `cliap2 --ntp` output exactly: high 32 bits = seconds
    since 1900-01-01 UTC, low 32 bits = fractional second (2^32 ticks per
    second). cliap2's --ntpstart parses this format directly.

    Previously implemented as microseconds-since-NTP-epoch — a wrong unit
    that made cliap2 compute "Audio starts in 305583489 secs" and
    effectively hang for ~9 years before the first sample reached the
    speaker. Verified by matching against `cliap2 --ntp` output.
    """
    NTP_EPOCH_OFFSET_S = 2208988800  # seconds from NTP epoch (1900) to Unix epoch (1970)
    now = time.time()
    seconds = int(now) + NTP_EPOCH_OFFSET_S
    fraction = int((now - int(now)) * (1 << 32))
    return (seconds << 32) | fraction


class _PipeWriter:
    """Tiny asyncio.StreamWriter-shaped adapter over a WriteTransport.

    We can't use asyncio.StreamWriter for FIFO writes because its
    wait_closed() calls Protocol._get_close_waiter, which exists on
    StreamReaderProtocol but not on the bare Protocol we register here.
    The adapter exposes the four methods _send_command + _teardown care
    about — write, drain, close, wait_closed — and uses the transport
    directly. is_closing() lets tests check teardown state without
    touching internals.
    """

    def __init__(self, transport: asyncio.WriteTransport) -> None:
        self._transport = transport
        self._closed: bool = False

    def write(self, data: bytes) -> None:
        if self._closed:
            raise BrokenPipeError("pipe writer is closed")
        try:
            self._transport.write(data)
        except RuntimeError as exc:
            # uvloop's WriteUnixTransport raises RuntimeError "the handler
            # is closed" when the transport was closed underneath us
            # (cliap2 exited). Normalize to BrokenPipeError so the
            # existing except clauses in _teardown / _send_command catch
            # it as a closed-pipe condition rather than 500'ing the
            # admin HTTP request that triggered the write.
            self._closed = True
            raise BrokenPipeError(f"pipe writer transport closed: {exc}") from None

    async def drain(self) -> None:
        # The transport buffers; for a FIFO the kernel buffer (~64KB on
        # Linux) absorbs typical command bursts. A real drain would
        # require a Protocol with the asyncio flow-control hooks; for
        # our command volume the transport buffering is sufficient.
        await asyncio.sleep(0)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._transport.close()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    async def wait_closed(self) -> None:
        # The transport's close is synchronous from our perspective; the
        # kernel handles the pipe close. No additional wait needed.
        return None

    def is_closing(self) -> bool:
        return self._closed


class AirPlayBackend:
    """Plays audio via cliap2 (AirPlay 2) / cliraop (AirPlay 1) subprocesses.

    Each playback session spawns one FFmpeg + one binary process. FFmpeg
    pulls the Plex HTTP stream and emits raw PCM into the binary's stdin via
    file-descriptor inheritance; commands flow newline-delimited over a
    named pipe; status events arrive on the binary's stderr.

    Speakers confirmed working with cliap2: JBL Charge 5 Wi-Fi SE, WiiM Pro,
    and similar third-party AP2 receivers using NTP timing. HomePod and
    strict PTP-required receivers are explicitly out of scope.
    """

    def __init__(self, advance_cb: AdvanceCallback | None = None) -> None:
        self._advance_cb = advance_cb
        self._device_id: str | None = None

        # Discovery cache populated by U8: device_id → (name, host, port, txt).
        # TXT is retained because cliap2's --txt flag consumes the avahi TXT
        # records as a kv string during HAP pair-verify.
        self._device_addr: dict[str, tuple[str, str, int, dict[str, str]]] = {}
        self._scan_lock = asyncio.Lock()  # serializes one-shot Scan re-browses
        # Per-device asyncio.Lock for the discovery-time AP2 probe. Created
        # lazily on first probe for each device_id; ensures concurrent
        # probe-trigger calls for the same device collapse to one cliap2
        # subprocess. Re-test (U5) uses the same locks so a Re-test
        # initiated while a background probe is in flight is serialized.
        self._probe_locks: dict[str, asyncio.Lock] = {}
        # Bounded fan-out of probe subprocesses across all devices.
        # See _AIRPLAY_PROBE_CONCURRENCY for the rationale.
        self._probe_semaphore = asyncio.Semaphore(_AIRPLAY_PROBE_CONCURRENCY)

        # Per-session subprocess state — populated in play(), torn down in stop().
        self._cliap2_proc: asyncio.subprocess.Process | None = None
        self._ffmpeg_proc: asyncio.subprocess.Process | None = None
        self._cmd_pipe_path: str | None = None
        self._cmd_pipe_writer: asyncio.StreamWriter | None = None
        self._stderr_reader_task: asyncio.Task | None = None
        self._ffmpeg_stderr_reader_task: asyncio.Task | None = None
        self._process_watcher_task: asyncio.Task | None = None
        # Tracks which AirPlay binary the current session was spawned
        # with ("cliap2" or "cliraop"); used by the stderr reader to
        # label log lines with the correct origin.
        self._airplay_binary: str = "airplay"
        # Monotonic clock anchor for get_position(). Set in play() to
        # the time audio is expected to start (now + NTP startup delay
        # - start_offset). None when no session is active. Pause/resume
        # isn't tracked here — the position keeps advancing across pauses.
        # Good enough for the progress bar; precise pause tracking would
        # need to subtract paused-time from the elapsed calculation.
        self._playback_started_at: float | None = None
        # Cached stream URL + metadata for the current session, used by
        # seek() to respawn ffmpeg + cliraop with `-ss <offset>`. cliap2
        # and cliraop read from stdin and cannot seek mid-stream — the
        # only way to scrub is to tear down and restart from the new
        # offset. Cleared in _teardown() so seek() on an idle backend
        # is a safe no-op.
        self._current_stream_url: str | None = None
        self._current_metadata: Track | None = None

        # Bounded buffer of recent stderr lines for crash diagnosis (U9).
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=20)
        # Dedup flag between U6 stderr EOS path and U9 watcher exit path.
        self._exit_handled: bool = False

        # Volume + echo-guard — mirrors Chromecast / DLNA shape.
        self._volume: float = 0.5
        self._vol_last_set: float = 0.0

        # DACP server is injected by app.state during setup(). Avoiding a
        # direct import here keeps the module-level dependency surface narrow
        # and dodges a circular import between airplay.py and dacp.py.
        self._dacp_server: Any = None
        # Per-stream DACP session minted in play(), released in _teardown().
        self._dacp_session: Any = None

        self._is_playing: bool = False
        self._stop_requested: bool = False

    # ── device discovery ──────────────────────────────────────────────────────

    def register_resolved(
        self,
        name: str,
        host: str,
        port: int,
        uuid: str | None,
        txt: dict[str, str],
    ) -> OutputDevice:
        """Feed one avahi-resolved RAOP advertisement into the address cache.

        Called by the device watcher (2026-06-11 live-discovery plan U2,
        KTD9) on every subscription arrival. cliap2 spawn, probes, and
        admin's ``_host_for`` all read ``_device_addr``; an arrival that
        skipped it would be registry-visible but unplayable.

        Writes EXACTLY the entry shape ``discover_devices`` writes — the
        RAW avahi name is stored (cliap2's ``_ensure_deviceid`` needs the
        MAC prefix) while the returned OutputDevice carries the stripped
        display label, mirroring the one-shot path. ``uuid`` is accepted
        for signature parity with the Chromecast hook but ignored: this
        backend keys devices by ``host:port`` on every path (KTD10 —
        registry ids must equal backend-cache ids).

        Thread-safety: a single dict-item assignment, and every writer
        (this hook on the asyncio loop, ``discover_devices`` under its
        asyncio lock on the same loop) runs on the event-loop thread —
        no torn state is possible, and nothing is ever cleared here.
        """
        del uuid  # parity with ChromecastBackend.register_resolved; unused
        device_id = f"{host}:{port}"
        self._device_addr[device_id] = (name, host, port, txt or {})
        return OutputDevice(
            id=device_id,
            name=_strip_raop_mac_prefix(name),
            backend_type="airplay",
            id_format="host_port",
            hint=None,
        )

    async def discover_devices(self) -> list[OutputDevice]:
        if not _binaries_available():
            return []
        async with self._scan_lock:
            # In-process one-shot browse on the shared AsyncZeroconf first;
            # if 5353 couldn't bind (a host avahi owns it) fall back to avahi
            # over D-Bus (2026-06-16 cross-host fix). Both return the same
            # five-field tuple shape, so the merge below is unchanged.
            from app import state
            from app.output import mdns_dbus, mdns_zeroconf
            found = await mdns_zeroconf.discover("_raop._tcp.local", state.shared_aiozc)
            if found is None:
                found = await mdns_dbus.discover("_raop._tcp.local")
            if found is None:
                # Neither in-process nor D-Bus available; the U6 degraded
                # banner distinguishes "no devices" from "discovery unavailable".
                return []
            fresh: dict[str, tuple[str, str, int, dict[str, str]]] = {}
            devices: list[OutputDevice] = []
            for name, host, port, _uuid, txt in found:
                device_id = f"{host}:{port}"
                # _device_addr stores the raw avahi name (cliap2 needs the
                # MAC prefix for _ensure_deviceid); OutputDevice.name is the
                # cleaned-up display label the admin picker shows.
                fresh[device_id] = (name, host, port, txt)
                devices.append(OutputDevice(
                    id=device_id,
                    name=_strip_raop_mac_prefix(name),
                    backend_type="airplay",
                    id_format="host_port",
                    hint=None,  # U10: pyatv-era warning is no longer accurate
                ))
            # KTD9 merge (2026-06-11 live-discovery U5): UPDATE the cache,
            # never wholesale-replace it. The watcher's Scan reconcile
            # RETAINS devices a one-shot window missed (online entries,
            # and offline ghosts until a Scan confirms their absence);
            # clearing here would orphan those — registry-visible but
            # addressless, so unplayable and invisible to the aggregator.
            # Re-seen devices overwrite their entries in place.
            self._device_addr.update(fresh)
            _log.debug("AirPlay: D-Bus discovery found %d device(s): %s",
                       len(devices), [d.name for d in devices])
            # Schedule a background probe for any AP2-capable device that
            # doesn't yet have a cached protocol verdict. The probe runs
            # fire-and-forget; discover_devices returns on its existing
            # latency budget. Per-device locks prevent duplicates if
            # discovery cycles fire repeatedly before a probe completes;
            # _probe_semaphore caps cross-device concurrency. Only THIS
            # scan's arrivals are considered (`fresh`, not the merged
            # cache) so a retained-offline ghost never re-probes on every
            # one-shot.
            for _dev_id, (_name, _host, _port, _txt) in fresh.items():
                if _use_ap2(_txt):
                    _t = asyncio.create_task(
                        self._probe_device_if_unprobed(_dev_id, _name, _host, _port, _txt)
                    )
                    # Retrieve any unhandled exception via done_callback so
                    # asyncio doesn't emit the "Task exception was never
                    # retrieved" warning on shutdown. Mirrors the pattern
                    # used by app/state.py:_log_task_exc for background tasks.
                    _t.add_done_callback(
                        lambda t: t.exception() if not t.cancelled() and t.exception() else None
                    )
            # Prune stale per-device probe locks. Devices that disappeared
            # from discovery (DHCP churn, mDNS expiry) leave their locks
            # in _probe_locks; clean them up to prevent slow growth across
            # long-running deployments. Only prune unlocked entries so we
            # don't yank a lock out from under an in-flight probe.
            stale_dev_ids = [
                d for d, lock in self._probe_locks.items()
                if d not in self._device_addr and not lock.locked()
            ]
            for d in stale_dev_ids:
                del self._probe_locks[d]
            return devices

    async def _probe_device_if_unprobed(
        self, device_id: str, name: str, host: str, port: int, txt: dict[str, str]
    ) -> None:
        """Run the AP2 probe iff the device has no cached verdict yet.
        Suppresses exceptions — discovery should never be impacted by a
        probe crash."""
        try:
            cached = await _get_per_device_protocol(device_id)
            if cached is not None:
                return
            await self._probe_device(device_id, name, host, port, txt)
        except Exception:
            _log.warning(
                "AirPlay probe scheduling failed for %r", device_id, exc_info=True
            )

    async def probe_device(self, device_id: str) -> bool:
        """Picker-facing probe: True if AirPlay is verified to work on the
        device addressed by *device_id*.

        Wraps the existing per-device AP1/AP2 probe path. A cached verdict
        (``ap1`` or ``ap2``) short-circuits to True — both are working
        verdicts from the picker's perspective; the cliap2-vs-cliraop
        pivot is internal to play(). Absence of a verdict triggers the
        existing _probe_device which spawns cliap2 against a silent
        input and persists the verdict.

        Never raises. ``DeviceNotReadyError`` (binaries missing) returns
        False — the picker must NOT show AirPlay as verified when cliap2
        isn't installed. Any other exception returns False with a
        WARNING log naming the device_id and reason.
        """
        try:
            cached = await _get_per_device_protocol(device_id)
        except Exception:
            _log.warning(
                "AirPlay probe_device: cache read failed for %r",
                device_id, exc_info=True,
            )
            return False
        if cached is not None:
            return True
        info = self._device_addr.get(device_id)
        if info is None:
            return False
        name, host, port, txt = info
        try:
            verdict = await self._probe_device(device_id, name, host, port, txt)
        except DeviceNotReadyError:
            _log.warning(
                "AirPlay probe_device: binaries unavailable for %r", device_id,
            )
            return False
        except Exception:
            _log.warning(
                "AirPlay probe_device failed for %r", device_id, exc_info=True,
            )
            return False
        return verdict in ("ap1", "ap2")

    async def _probe_device(
        self, device_id: str, name: str, host: str, port: int, txt: dict[str, str],
        *, force: bool = False,
    ) -> str:
        """Spawn cliap2 against a silent input, watch stderr for known
        failure markers within the probe window, persist the verdict, and
        broadcast AirPlayProtocolChangedEvent. Returns `"ap2"` or `"ap1"`.

        Per-device asyncio.Lock dedupes concurrent callers: the second
        caller observes the cached verdict the first caller persisted and
        returns it without re-running cliap2.

        `force=True` skips the cached-result early-return. Re-test (U5)
        sets this so a user-initiated re-probe always actually re-runs
        cliap2 instead of returning the stale verdict.

        Always SIGTERMs cliap2 and unlinks the probe FIFO at the end —
        even on a clean `"ap2"` verdict — so probes never leave residual
        subprocesses or filesystem objects behind.
        """
        if not _binaries_available():
            raise DeviceNotReadyError(
                "AirPlay binaries unavailable — cannot probe AP2 support"
            )
        lock = self._probe_locks.setdefault(device_id, asyncio.Lock())
        async with lock:
            if not force:
                # The second concurrent caller observes whatever the first
                # persisted and skips re-running cliap2.
                cached = await _get_per_device_protocol(device_id)
                if cached is not None:
                    return cached

            verdict = await self._run_probe_subprocess(device_id, name, host, port, txt)

            # Re-check the cache AFTER the probe window closes (unless this
            # is a forced Re-test). If a user clicked "No audio?" while the
            # probe was running, the no-audio endpoint will have persisted
            # `ap1` directly — without this re-check the probe's verdict
            # (typically `ap2` because cliap2's stderr stayed clean for the
            # silent-fail case) would clobber the user's explicit ap1
            # decision. force=True (Re-test) intentionally bypasses this
            # because the user explicitly asked for a fresh verdict.
            if not force:
                user_written = await _get_per_device_protocol(device_id)
                if user_written is not None and user_written != verdict:
                    _log.info(
                        "AirPlay probe: %r verdict was %s but user wrote %s "
                        "during probe; keeping user value",
                        name, verdict, user_written,
                    )
                    return user_written

            await _set_per_device_protocol(device_id, verdict)
            _log.info(
                "AirPlay probe verdict for %r: protocol=%s", name, verdict,
            )
            await self._broadcast_protocol_change(device_id, verdict)
            return verdict

    async def _run_probe_subprocess(
        self, device_id: str, name: str, host: str, port: int, txt: dict[str, str]
    ) -> str:
        """Subprocess-level half of the probe — owns spawn, stderr watch,
        teardown. Separated from _probe_device so the lock-and-persist
        wrapper stays readable.

        Acquires the cross-device probe semaphore so a fresh-boot discovery
        of N AP2-capable speakers doesn't spawn N concurrent cliap2 subprocesses
        each holding an RTSP attempt for 8 seconds."""
        async with self._probe_semaphore:
            probe_fifo = os.path.join(
                tempfile.gettempdir(),
                f"jukeplox-probe-{uuid.uuid4().hex}.cmd",
            )
            try:
                os.mkfifo(probe_fifo, mode=0o600)
            except OSError as exc:
                _log.warning(
                    "AirPlay probe: mkfifo failed for %s: %s — defaulting to ap1",
                    probe_fifo, exc,
                )
                return "ap1"

            args = _build_cliap2_args(
                binary=_cliap2_bin(),
                name=name,
                host=host,
                port=port,
                txt=txt,
                ntp_start=_ntp_now(),
                volume_pct=0,  # silent probe — minimize speaker-side disruption
                dacp_id=_generate_dacp_id(),
                active_remote_id=_generate_active_remote_id(),
                cmd_pipe_path=probe_fifo,
            )
            proc: asyncio.subprocess.Process | None = None
            verdict = "ap2"  # optimistic; flipped to ap1 on any failure marker
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdin=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                verdict = await self._watch_probe_stderr(proc)
            except Exception:
                _log.warning(
                    "AirPlay probe: subprocess error for %r — defaulting to ap1",
                    device_id, exc_info=True,
                )
                verdict = "ap1"
            finally:
                # Wrap terminate in its own try/except so an unhandled
                # exception during process termination doesn't skip the
                # FIFO unlink. Python's finally-block semantics: if a
                # statement raises, subsequent statements in the same
                # finally are skipped, which would leak the FIFO.
                if proc is not None and proc.returncode is None:
                    try:
                        await self._terminate_proc(proc, "cliap2-probe")
                    except Exception:
                        _log.warning(
                            "AirPlay probe: terminate raised; continuing to "
                            "unlink FIFO %s", probe_fifo, exc_info=True,
                        )
                try:
                    os.unlink(probe_fifo)
                except OSError:
                    pass
            return verdict

    async def _watch_probe_stderr(self, proc: Any) -> str:
        """Read cliap2's stderr for up to _AIRPLAY_PROBE_WINDOW_S, returning
        `"ap1"` on the first failure-marker match OR non-zero process exit,
        `"ap2"` if neither occurs within the window."""
        async def _scan() -> str:
            assert proc.stderr is not None
            while True:
                line_bytes = await proc.stderr.readline()
                if not line_bytes:
                    # cliap2 closed stderr — likely exited. Check returncode.
                    rc = await proc.wait()
                    return "ap1" if rc != 0 else "ap2"
                line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
                _log.debug("AirPlay probe stderr: %s", line)
                if _AIRPLAY_PROBE_FAILURE_RE.search(line):
                    _log.info(
                        "AirPlay probe: failure marker matched: %s", line,
                    )
                    return "ap1"

        try:
            return await asyncio.wait_for(_scan(), timeout=_AIRPLAY_PROBE_WINDOW_S)
        except asyncio.TimeoutError:
            # No failure marker in the window AND cliap2 didn't exit — that
            # means HAP handshake + initial RTSP setup completed without
            # complaint. Sufficient signal that cliap2 *can* talk to this
            # device. Silent-success-no-audio falls outside this detection
            # by design (the "No audio?" button is the recovery for that).
            return "ap2"

    async def _broadcast_protocol_change(self, device_id: str, protocol: str) -> None:
        """Emit AirPlayProtocolChangedEvent on the admin bus. Best-effort —
        bus failures are logged but don't propagate."""
        try:
            from app.events.bus import manager
            from app.events.types import AirPlayProtocolChangedEvent
            event = AirPlayProtocolChangedEvent(
                device_id=device_id, protocol=protocol,
            )
            await manager.broadcast_to_admins(event)
        except Exception:
            _log.warning(
                "AirPlay: protocol-change broadcast failed for %r",
                device_id, exc_info=True,
            )

    async def set_device(self, device_id: str) -> None:
        if not _binaries_available():
            return
        if device_id not in self._device_addr:
            raise RuntimeError(
                f"AirPlay device {device_id!r} not found in discovery cache — "
                "rediscover before playback"
            )
        # Tear down any prior session before switching device — protects
        # against the queue trying to play through a stale cliap2 pointed at
        # a different speaker.
        await self._teardown(send_stop=self._is_playing, caller="set_device")

        self._device_id = device_id

        # Load persisted volume; default to 0.5 if absent. Preserves the
        # pyatv-era setting key so saved volumes migrate without action.
        from app import database
        stored_volume = await database.get_setting(f"vol:airplay:{device_id}")
        self._volume = float(stored_volume) if stored_volume else 0.5

        # Persist resolved address so app/state.py:_startup_reconnect can
        # rebind on the next boot without re-running discovery.
        import json
        name, host, port, _txt = self._device_addr[device_id]
        try:
            await database.set_setting(
                f"output_addr:{device_id}",
                json.dumps({"name": name, "host": host, "port": port}),
            )
        except Exception:
            _log.warning("AirPlay: set_setting(output_addr) failed", exc_info=True)

    # ── playback (U4) ─────────────────────────────────────────────────────────

    async def play(
        self,
        stream_url: str,
        metadata: Track,
        *,
        start_offset_ms: int = 0,
    ) -> None:
        if not _binaries_available():
            raise DeviceNotReadyError(
                "AirPlay binaries (cliap2/cliraop) are not available in this "
                "environment — install them at /usr/local/bin or set "
                "JUKEPLOX_CLIAP2_BIN / JUKEPLOX_CLIRAOP_BIN."
            )
        if self._device_id is None:
            raise DeviceNotReadyError(
                "No AirPlay device selected — call set_device() first"
            )
        # Tear down any prior session first — protects against back-to-back
        # play() calls from the queue advance loop double-spawning cliap2.
        if self._is_playing or self._cliap2_proc is not None:
            await self._teardown(send_stop=True, caller="play")

        addr = self._device_addr.get(self._device_id)
        if addr is None:
            raise DeviceNotReadyError(
                f"No cached address for AirPlay device {self._device_id!r} — "
                "rediscover before playback."
            )
        name, host, port, txt = addr

        # Per-session FIFO path so we never collide with a leftover from a
        # crashed prior session. tempfile.gettempdir() is /tmp on Linux and
        # %TEMP% on Windows (won't exist in production but lets tests run).
        cmd_pipe_path = os.path.join(
            tempfile.gettempdir(),
            f"jukeplox-cliap2-{uuid.uuid4().hex}.cmd",
        )
        os.mkfifo(cmd_pipe_path, mode=0o600)
        self._cmd_pipe_path = cmd_pipe_path

        # OS pipe carries PCM audio from FFmpeg to cliap2. Python does not
        # touch the bytes — read end is inherited by cliap2 as stdin, write
        # end is inherited by FFmpeg as stdout, both file descriptors are
        # closed in this process so EOF propagates correctly when FFmpeg
        # exits.
        read_fd, write_fd = os.pipe()
        try:
            # Mint a DACP session if the server is wired in; otherwise fall
            # back to fresh per-stream ids. Without the server, speaker-side
            # volume callbacks have nowhere to land — but audio still plays.
            if self._dacp_server is not None:
                self._dacp_session = self._dacp_server.new_session()
                dacp_id = self._dacp_session.dacp_id
                active_remote_id = self._dacp_session.active_remote_id
            else:
                self._dacp_session = None
                dacp_id = _generate_dacp_id()
                active_remote_id = _generate_active_remote_id()
            volume_pct = int(round(self._volume * 100))
            # AP2 vs AP1 detection — cliap2 is the AirPlay 2 binary and
            # against a RAOP-only receiver (WiiM Pro, JBL Charge 5, most
            # pre-2018 receivers) it logs "Not using AirPlay 2 ..." and
            # silently fails to transmit. cliraop is the right binary for
            # those — see _use_ap2 for the detection criteria and
            # docs/solutions/integration-issues/ for the full failure
            # mode write-up.
            # Per-binary NTP startup delay — see _AIRPLAY_NTP_STARTUP_DELAY_S.
            # cliap2 needs 4s for AP2 pair-verify; cliraop needs ~0
            # because its own session-establishment plus 1250ms player
            # buffer already provides enough headroom.
            # Consult the per-device protocol cache before falling back to
            # the TXT heuristic. A cached "ap1" forces cliraop irrespective
            # of TXT (probe or the "No audio?" button marked this device
            # cliap2-broken); a cached "ap2" is the probe confirming TXT
            # was right. Absent cache means unprobed — use the TXT heuristic
            # tentatively and let the background probe persist a verdict
            # for next time. Source string is logged so production logs
            # make the routing decision visible at a glance.
            cached_protocol = await _get_per_device_protocol(self._device_id)
            if cached_protocol == "ap1":
                airplay_binary = "cliraop"
                binary_source = "cached ap1"
            elif cached_protocol == "ap2":
                airplay_binary = "cliap2"
                binary_source = "cached ap2"
            elif _use_ap2(txt):
                airplay_binary = "cliap2"
                binary_source = "txt-heuristic ap2"
            else:
                airplay_binary = "cliraop"
                binary_source = "txt-heuristic ap1"
            _log.info(
                "AirPlay: selected %s for device %r (source=%s)",
                airplay_binary, name, binary_source,
            )
            ntp_delay_s = _AIRPLAY_NTP_STARTUP_DELAY_S.get(airplay_binary, 0)
            ntp_start = _ntp_now() + (ntp_delay_s << 32)

            if airplay_binary == "cliap2":
                child_args = _build_cliap2_args(
                    binary=_cliap2_bin(),
                    name=name,
                    host=host,
                    port=port,
                    txt=txt,
                    ntp_start=ntp_start,
                    volume_pct=volume_pct,
                    dacp_id=dacp_id,
                    active_remote_id=active_remote_id,
                    cmd_pipe_path=cmd_pipe_path,
                )
            else:
                child_args = _build_cliraop_args(
                    binary=_cliraop_bin(),
                    host=host,
                    port=port,
                    txt=txt,
                    ntp_start=ntp_start,
                    volume_pct=volume_pct,
                    dacp_id=dacp_id,
                    active_remote_id=active_remote_id,
                    cmd_pipe_path=cmd_pipe_path,
                )
            _log.info(
                "AirPlay: spawning %s for device %r (port %d)",
                airplay_binary, name, port,
            )
            # Remember the binary name so the stderr reader prefix can
            # say "cliraop stderr" instead of always "cliap2 stderr" —
            # otherwise log scans against the cliraop migration logs
            # would be misleading.
            self._airplay_binary = airplay_binary
            # Anchor for get_position(). Audio actually reaches the
            # speaker ntp_delay_s seconds after spawn (plus the
            # binary's internal session-establishment buffer), so the
            # progress bar tracks closely enough for UI purposes.
            # Subtracting start_offset_ms shifts the anchor backward so
            # `elapsed = now - anchor` reads as `start_offset + real_elapsed`
            # for the seek case — the bar resumes at the seek point and
            # keeps incrementing from there.
            self._playback_started_at = (
                time.monotonic() + ntp_delay_s - (start_offset_ms / 1000)
            )
            # Cache for seek(). Set before the spawn so a crash during
            # ffmpeg startup still leaves seek() a stable reference; the
            # teardown path clears these unconditionally.
            self._current_stream_url = stream_url
            self._current_metadata = metadata
            self._cliap2_proc = await asyncio.create_subprocess_exec(
                *child_args,
                stdin=read_fd,
                stderr=asyncio.subprocess.PIPE,
            )
            os.close(read_fd)
            read_fd = -1  # consumed; do not double-close in finally

            # Spawn the cliap2 stderr reader RIGHT NOW, before everything
            # else. Two reasons it has to be here, not later:
            # 1. The kernel pipe buffer is ~64KB. cliap2 emits ~20 stderr
            #    lines during startup + pair-verify. If we don't drain
            #    the buffer, cliap2's main loop eventually blocks on its
            #    own stderr write — silent freeze. Visible symptom on
            #    TrueNAS: cliap2 running but no audio to the speaker.
            # 2. cliap2's startup + pair-verify is the period during
            #    which it most needs visibility for debugging. The
            #    previous shape spawned the reader AFTER _open_cmd_pipe_writer
            #    (up to 5s blocking) + _send_metadata — during which any
            #    cliap2 error would have been entirely invisible.
            if self._cliap2_proc.stderr is not None:
                self._stderr_reader_task = asyncio.create_task(
                    self._stderr_reader_body(self._cliap2_proc.stderr)
                )

            # Process watcher spawned alongside the stderr reader so cliap2
            # crashes during pair-verify are detected immediately.
            self._process_watcher_task = asyncio.create_task(
                self._process_watcher_body(self._cliap2_proc)
            )

            ffmpeg_args = _build_ffmpeg_args(stream_url, start_offset_ms=start_offset_ms)
            self._ffmpeg_proc = await asyncio.create_subprocess_exec(
                *ffmpeg_args,
                stdout=write_fd,
                # Capture FFmpeg stderr too — silent FFmpeg crashes were
                # invisible with DEVNULL, and a FFmpeg crash causes cliap2
                # to see EOF on stdin, which it logs as "end of stream
                # reached" + "restarting w/o pause". Diagnosing that
                # chain required knowing whether FFmpeg was healthy.
                stderr=asyncio.subprocess.PIPE,
            )
            os.close(write_fd)
            write_fd = -1

            if self._ffmpeg_proc.stderr is not None:
                self._ffmpeg_stderr_reader_task = asyncio.create_task(
                    self._ffmpeg_stderr_reader_body(self._ffmpeg_proc.stderr)
                )
        except Exception:
            # On any spawn failure, close any FDs we still hold and tear down
            # whichever process did spawn — leaving zombies + orphaned FDs is
            # how repeated play() failures turn into resource exhaustion.
            if read_fd != -1:
                try:
                    os.close(read_fd)
                except OSError:
                    pass
            if write_fd != -1:
                try:
                    os.close(write_fd)
                except OSError:
                    pass
            await self._teardown(send_stop=False, caller="play_spawn_failed")
            raise

        # Command pipe writer — open the FIFO's write end. cliap2 opens the
        # read end on startup; the blocking open here waits until it does.
        # Wrapped in to_thread so the event loop stays free during that wait.
        await self._open_cmd_pipe_writer(cmd_pipe_path)

        self._is_playing = True
        self._stop_requested = False
        self._exit_handled = False

        # Push track metadata to the receiver. Title/artist/album/art appear
        # on speakers that surface AirPlay metadata (WiiM, JBL with companion
        # app, etc.). Failure is non-fatal — audio still plays.
        try:
            await self._send_metadata(metadata)
        except Exception:
            _log.warning("AirPlay: metadata send failed", exc_info=True)

        # stderr reader and process watcher are already running — they were
        # spawned right after the cliap2 process exec'd, before the FIFO open
        # blocked.

    async def _open_cmd_pipe_writer(self, path: str) -> None:
        """Open the FIFO write end and wrap it in a tiny pipe-writer adapter.

        Two reliability concerns drive the shape here:

        - O_WRONLY on a FIFO blocks until the read end is open. cliap2
          opens read on startup, but if it crashes pre-pair-verify the
          open never completes. asyncio.wait_for(timeout) caps the wait;
          on timeout the spawn-failure path tears everything down rather
          than wedging a threadpool thread.
        - asyncio.StreamWriter requires a Protocol with _get_close_waiter
          (StreamReaderProtocol provides it). A bare asyncio.Protocol()
          does not — wait_closed() raises AttributeError on every
          teardown. We bypass the StreamWriter entirely and write through
          the WriteTransport directly: drain() routes through the
          loop's flow-control mechanism via _drain_helper.
        """
        fd = await asyncio.wait_for(
            asyncio.to_thread(os.open, path, os.O_WRONLY),
            timeout=5.0,
        )
        loop = asyncio.get_running_loop()
        transport, _ = await loop.connect_write_pipe(
            asyncio.Protocol,
            os.fdopen(fd, "wb"),
        )
        self._cmd_pipe_writer = _PipeWriter(transport)

    async def pause(self) -> None:
        await self._send_command("ACTION", "PAUSE")

    async def resume(self) -> None:
        await self._send_command("ACTION", "PLAY")

    async def _send_command(self, key: str, value: Any) -> None:
        """Format and drain a single `KEY=value\\n` line to the FIFO.

        Tolerates a missing writer (set_volume before play()) and a closed
        pipe (cliap2 exited unexpectedly). Both cases log at WARNING and
        return — the caller is expected to recover via the watcher path in
        U9 rather than catching here.

        Sanitization: \\r and \\n are stripped from values before encoding.
        cliap2's command pipe is line-oriented; a metadata field
        containing a literal newline (Plex tracks with multi-line
        descriptions, lyric snippets, etc.) would otherwise inject a
        second KEY=value line below the intended one.
        """
        writer = self._cmd_pipe_writer
        if writer is None:
            return
        value_str = str(value).replace("\r", " ").replace("\n", " ")
        line = f"{key}={value_str}\n".encode("utf-8", errors="replace")
        try:
            writer.write(line)
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            _log.warning("AirPlay cmd pipe write failed (%s=%s): %s", key, value, exc)

    async def _process_watcher_body(self, proc: Any) -> None:
        """Await cliap2's exit and route the outcome.

        Three branches:

        - User-initiated stop (_stop_requested True): exit is expected,
          regardless of returncode. No advance, no event.
        - EOS already handled by U6's stderr reader (_exit_handled True):
          U6 already fired advance_cb; we no-op here to dedup.
        - Unexpected exit: log the stderr tail, broadcast a crash event so
          the admin UI surfaces the failure, fire advance_cb so the queue
          progresses past the dead track.
        """
        try:
            returncode = await proc.wait()
        except asyncio.CancelledError:
            return
        except Exception:
            _log.warning("AirPlay watcher: wait() raised", exc_info=True)
            return

        if self._stop_requested or self._exit_handled:
            return  # expected exit — U6 / _teardown owns the recovery

        self._exit_handled = True
        self._is_playing = False
        tail_str = "\n  ".join(self._stderr_tail) if self._stderr_tail else "(no output)"
        # returncode=0 means the binary processed the entire stream and
        # exited cleanly — that's the natural "track ended" path for
        # cliraop and shouldn't surface as a warning. Non-zero exits
        # are real crashes and still warrant the WARNING + crash event
        # broadcast below.
        if returncode == 0:
            _log.info(
                "AirPlay %s reached end of stream (returncode=0); "
                "firing advance_cb",
                self._airplay_binary,
            )
        else:
            _log.warning(
                "AirPlay %s exited unexpectedly (returncode=%s). Last stderr:\n  %s",
                self._airplay_binary, returncode, tail_str,
            )

        # Tear down the leftover FFmpeg process + FIFO + cmd-pipe writer
        # before broadcasting and advancing. Without this teardown, FFmpeg
        # keeps running (cliap2 is dead, so EPIPE will eventually fire,
        # but we shouldn't depend on it) and the FIFO stays on disk until
        # the next play() overwrites it. Scheduled rather than awaited so
        # the crash broadcast and advance_cb don't wait on SIGTERM grace.
        #
        # Clear our own task ref FIRST so the scheduled teardown's
        # cancel-list (which sweeps _stderr_reader_task,
        # _ffmpeg_stderr_reader_task, _process_watcher_task) doesn't
        # include the currently-running watcher. Without this we'd cancel
        # ourselves, and the CancelledError would land on the
        # `await self._advance_cb()` below, killing queue advance
        # mid-flight after queue_engine.advance() had already popped the
        # next track into _current and persisted save_queue([]) — the
        # "queue clears, music stops, only one song plays" failure mode.
        self._process_watcher_task = None
        asyncio.create_task(
            self._teardown(send_stop=False, caller="watcher_eos")
        )

        # Broadcast a crash event only for actual crashes (non-zero
        # exit). A clean end-of-stream exit is the queue's job to chain
        # to the next track — surfacing a "failed!" toast there would
        # be a confusing UI signal.
        if returncode != 0:
            try:
                from app.events.bus import manager
                from app.events.types import OutputChangedEvent
                event = OutputChangedEvent(
                    backend_type="error",
                    device_name=(
                        f"AirPlay playback failed (exit {returncode}); "
                        "advancing to next track"
                    ),
                )
                await manager.broadcast_to_admins(event)
            except Exception:
                _log.warning("AirPlay watcher: crash broadcast failed", exc_info=True)

        if self._advance_cb is not None:
            try:
                await self._advance_cb()
            except Exception:
                _log.warning("AirPlay watcher: advance_cb raised", exc_info=True)

    async def _stderr_reader_body(self, stream: asyncio.StreamReader) -> None:
        """Read cliap2's stderr line-by-line until EOF or cancellation.

        Two responsibilities only: (1) log every line so production has
        diagnostic visibility into cliap2's lifecycle, (2) populate the
        bounded _stderr_tail buffer so crash recovery in
        _process_watcher_body can report the last 20 lines.

        Notably NOT responsible for: detecting cliap2 exit. The
        authoritative exit signal is proc.wait() in the process watcher.
        Earlier versions of this method treated the literal stderr line
        'end of stream reached' as track-ended and fired advance_cb —
        but that line is cliap2's TRANSIENT input-buffer-drain marker
        (immediately followed by 'restarting w/o pause'), not a process
        exit. Treating it as exit caused a silent no-audio bug:
        is_playing flipped to False on a transient drain while cliap2
        was still running RTSP+RTP to the speaker, breaking the
        queue/UI state machine without surfacing any failure.

        cliap2 prefixes structured log lines with bracketed level tags:
        [FATAL] [ERROR] [WARN] [LOG] [INFO] [DEBUG] [SPAM]. Promote the
        attention-worthy ones to WARNING so they survive any future
        downgrade of the root log level.
        """
        try:
            while True:
                line_bytes = await stream.readline()
                if not line_bytes:
                    return  # EOF — process watcher handles exit
                line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
                self._stderr_tail.append(line)
                # Use the actual binary name in the prefix so cliap2/
                # cliraop log lines are visually distinct in the
                # container output. _airplay_binary is set in play()
                # before the reader spawns; fallback covers the
                # unlikely race where the reader's first read beats
                # the attribute assignment.
                binary_label = getattr(self, "_airplay_binary", "airplay")
                if (
                    "[FATAL]" in line
                    or "[ERROR]" in line
                    or "[WARN]" in line
                    or line.startswith(("ERROR", "Error:"))
                ):
                    _log.warning("AirPlay %s stderr: %s", binary_label, line)
                elif _AIRPLAY_NOISE_PATTERN.search(line):
                    # Routine per-second / per-20s heartbeat lines. Keep
                    # them at DEBUG so they survive an explicit DEBUG
                    # opt-in but don't drown out important events in
                    # production INFO logs. Without this filter every
                    # 60s of playback generates ~120 INFO lines from a
                    # single cliraop session, making it impossible to
                    # scan for real signals like teardown or advance_cb.
                    _log.debug("AirPlay %s stderr: %s", binary_label, line)
                else:
                    _log.info("AirPlay %s stderr: %s", binary_label, line)
        except asyncio.CancelledError:
            return
        except Exception:
            _log.warning("AirPlay stderr reader crashed", exc_info=True)

    async def _ffmpeg_stderr_reader_body(
        self, stream: asyncio.StreamReader
    ) -> None:
        """Drain FFmpeg's stderr so it doesn't fill the kernel pipe buffer
        (which would back-pressure FFmpeg's stderr write and freeze its
        decode loop) and surface its output for diagnostics.

        FFmpeg with -loglevel error only writes when something goes wrong,
        so anything we see here is worth attention. Promote at WARNING.
        """
        try:
            while True:
                line_bytes = await stream.readline()
                if not line_bytes:
                    return
                line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
                _log.warning("AirPlay ffmpeg stderr: %s", line)
        except asyncio.CancelledError:
            return
        except Exception:
            _log.warning("AirPlay ffmpeg stderr reader crashed", exc_info=True)

    async def _send_metadata(self, metadata: Track) -> None:
        """Push track fields to the receiver via cliap2's metadata commands.

        Order mirrors MA's protocols/_protocol.py: TITLE → ARTIST → ALBUM →
        DURATION → ARTWORK → ACTION=SENDMETA. SENDMETA flushes the buffered
        fields in a single RTSP SET_PARAMETER, so partial sends never reach
        the speaker in an inconsistent state.
        """
        title = getattr(metadata, "title", None) or ""
        artist = getattr(metadata, "artist", None) or ""
        album = getattr(metadata, "album", None) or ""
        duration_ms = getattr(metadata, "duration_ms", None) or 0
        artwork = getattr(metadata, "thumb", None) or ""

        if title:
            await self._send_command("TITLE", title)
        if artist:
            await self._send_command("ARTIST", artist)
        if album:
            await self._send_command("ALBUM", album)
        if duration_ms:
            await self._send_command("DURATION", int(duration_ms))
        # cliap2 treats ARTWORK as an HTTP URL to fetch. Plex thumb keys
        # come through as `<server-id>:/library/metadata/...` — cliap2
        # tries `curl` on that, fails with "Unsupported protocol", then
        # its artwork parser corrupts internal state ('j�|�') and gives
        # up on the entire command pipe with "Error parsing incoming
        # data on command pipe ..., will stop reading". After that any
        # subsequent VOLUME / ACTION=PAUSE / ACTION=STOP write vanishes
        # silently and pause/skip surface as a `WriteUnixTransport
        # closed=True` warning from _PipeWriter. Skip non-HTTP URLs
        # entirely until thumbs are proxied through a real http endpoint.
        if artwork and (artwork.startswith("http://") or artwork.startswith("https://")):
            await self._send_command("ARTWORK", artwork)
        # Flush — without SENDMETA the receiver never sees the buffered fields.
        await self._send_command("ACTION", "SENDMETA")

    async def stop(self) -> None:
        self._stop_requested = True
        await self._teardown(send_stop=True, caller="stop")

    async def _teardown(self, *, send_stop: bool, caller: str = "unknown") -> None:
        """Shared teardown for stop(), play()-after-play(), and U9's crash
        recovery. Idempotent — calling it on an already-torn-down session is
        safe.

        Order matters: send ACTION=STOP first so cliap2 can TEARDOWN the
        RTSP session cleanly; SIGTERM cliap2 with 2s grace before SIGKILL;
        SIGTERM ffmpeg the same way; close the command pipe; unlink the FIFO.

        `caller` is a free-form label for the entry point that called
        teardown (e.g. "play", "stop", "seek", "watcher_eos"). Logged at
        INFO so production logs reveal *why* a live cliraop got SIGTERMed
        — last round the logs only showed "exited unexpectedly
        (returncode=-15)" with no breadcrumb back to the call site.
        """
        _log.info(
            "AirPlay teardown invoked from=%s send_stop=%s is_playing=%s "
            "proc_alive=%s",
            caller, send_stop, self._is_playing,
            self._cliap2_proc is not None,
        )
        # ACTION=STOP — best-effort write; the FIFO may already be closed if
        # cliap2 exited unexpectedly.
        if send_stop and self._cmd_pipe_writer is not None:
            try:
                self._cmd_pipe_writer.write(b"ACTION=STOP\n")
                await self._cmd_pipe_writer.drain()
            except (BrokenPipeError, ConnectionResetError, OSError):
                _log.debug("AirPlay teardown: cmd pipe already closed")

        # cliap2 and ffmpeg can be terminated in parallel — once cliap2
        # exits ffmpeg sees EPIPE anyway, but waiting in parallel halves
        # the worst-case 4s grace to 2s when SIGKILL is needed.
        await asyncio.gather(
            self._terminate_proc(self._cliap2_proc, "cliap2"),
            self._terminate_proc(self._ffmpeg_proc, "ffmpeg"),
            return_exceptions=True,
        )
        self._cliap2_proc = None
        self._ffmpeg_proc = None

        # Close the command pipe writer.
        if self._cmd_pipe_writer is not None:
            try:
                self._cmd_pipe_writer.close()
                await self._cmd_pipe_writer.wait_closed()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            self._cmd_pipe_writer = None

        # Unlink the FIFO last so process teardown can't race with the file
        # going away.
        if self._cmd_pipe_path is not None:
            try:
                os.unlink(self._cmd_pipe_path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                _log.warning("AirPlay teardown: failed to unlink FIFO %s: %s",
                             self._cmd_pipe_path, exc)
            self._cmd_pipe_path = None

        # Clear position anchor so get_position() returns 0 between
        # sessions instead of an ever-growing stale value.
        self._playback_started_at = None
        # Clear the seek cache so seek() on an idle backend is a no-op
        # rather than respawning a stale URL.
        self._current_stream_url = None
        self._current_metadata = None

        # Cancel reader / watcher tasks and await their cancellation so a
        # stale watcher cannot fire advance_cb after a new session starts.
        # Without the await, task.cancel() schedules the CancelledError
        # but the task may still execute its next branch before the loop
        # processes it.
        pending_tasks = [
            t for t in (
                self._stderr_reader_task,
                self._ffmpeg_stderr_reader_task,
                self._process_watcher_task,
            )
            if t is not None and not t.done()
        ]
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        self._stderr_reader_task = None
        self._ffmpeg_stderr_reader_task = None
        self._process_watcher_task = None

        # Release the DACP session so subsequent callbacks with this token
        # get 401 instead of routing to an inactive stream.
        if self._dacp_session is not None and self._dacp_server is not None:
            try:
                self._dacp_server.end_session(self._dacp_session.active_remote_id)
            except Exception:
                _log.warning("DACP end_session failed", exc_info=True)
            self._dacp_session = None

        self._is_playing = False

    async def _terminate_proc(self, proc: Any, label: str) -> None:
        """SIGTERM with grace, then SIGKILL. Tolerates None and already-exited
        processes; both happen during double-teardown and crash recovery."""
        if proc is None:
            return
        if proc.returncode is not None:
            return  # already exited
        try:
            proc.terminate()
        except (ProcessLookupError, OSError):
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=_STOP_GRACE_S)
        except asyncio.TimeoutError:
            _log.warning(
                "AirPlay teardown: %s did not exit on SIGTERM within %.1fs — sending SIGKILL",
                label, _STOP_GRACE_S,
            )
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=_STOP_GRACE_S)
            except asyncio.TimeoutError:
                _log.error("AirPlay teardown: %s ignored SIGKILL", label)

    # ── volume (U5) ──────────────────────────────────────────────────────────

    async def set_volume(self, level: float) -> None:
        self._volume = max(0.0, min(1.0, level))
        # Stamp echo-guard BEFORE the device write so the DACP callback the
        # speaker may emit in response is suppressed within the window.
        self._vol_last_set = time.monotonic()
        # Push to cliap2 over the cmd pipe (no-op if play() hasn't run yet).
        await self._send_command("VOLUME", int(round(self._volume * 100)))
        if self._device_id:
            from app import database
            await database.set_setting(f"vol:airplay:{self._device_id}", str(self._volume))

    async def get_volume(self) -> float:
        return self._volume

    # ── position (sender side does not surface this) ─────────────────────────

    async def get_position(self) -> int:
        """Estimated playback position in milliseconds since track start.

        Computed from `time.monotonic()` anchored at play() time plus
        the per-binary NTP startup delay — neither cliap2 nor cliraop
        report position back over the command-pipe protocol we use, so
        we infer it from wall-clock elapsed. Reset to 0 between sessions
        (via _teardown). Doesn't subtract paused time — the position
        keeps advancing across pauses, which is good enough for the
        admin progress bar but technically over-counts during pauses.
        """
        anchor = self._playback_started_at
        if anchor is None:
            return 0
        elapsed_s = time.monotonic() - anchor
        if elapsed_s < 0:
            return 0  # session hasn't crossed t0 yet (ntp_delay window)
        return int(elapsed_s * 1000)

    async def seek(self, position_ms: int) -> None:
        """Scrub the current track by respawning ffmpeg + cliraop at the
        new offset. cliap2/cliraop read PCM from stdin and cannot seek
        mid-stream, so the only available mechanism is to tear the
        pipeline down and rebuild it with ffmpeg's `-ss <offset>` flag
        on the input side. play() does its own teardown, so we can just
        delegate.
        """
        url = self._current_stream_url
        metadata = self._current_metadata
        if url is None or metadata is None:
            return  # nothing currently loaded — nothing to seek within
        # Mark the upcoming teardown as a user-initiated stop so the
        # watcher's exit handler treats it as expected and doesn't fire
        # advance_cb on the killed cliraop. play() resets _stop_requested
        # implicitly via the fresh spawn path.
        self._stop_requested = True
        await self.play(url, metadata, start_offset_ms=max(0, position_ms))

    @property
    def is_playing(self) -> bool:
        return self._is_playing
