"""Tests for app.output.airplay — Music Assistant cliap2 subprocess backend.

The pyatv-based implementation is gone; pyatv is no longer a project
dependency. Tests here mock asyncio.subprocess.Process and os.access (via
the JUKEPLOX_CLIAP2_BIN / JUKEPLOX_CLIRAOP_BIN env vars) rather than mocking
a third-party library.

U3 covers the skeleton + interface contract; U4-U9 will add subprocess
lifecycle, IPC, stderr, DACP, discovery, and crash recovery coverage.
"""

from __future__ import annotations

import asyncio
import sys

import pytest


@pytest.fixture
def airplay_module(monkeypatch, tmp_path):
    """Reload app.output.airplay with a clean module cache so each test sees
    a fresh module state. The module-level helpers _cliap2_bin / _cliraop_bin
    read os.environ at call time (not module-load time) so reload isn't
    strictly required, but defending against accidental module-level state
    drift across tests keeps this fixture robust as U4+ add more state.
    """
    monkeypatch.delenv("JUKEPLOX_CLIAP2_BIN", raising=False)
    monkeypatch.delenv("JUKEPLOX_CLIRAOP_BIN", raising=False)
    if "app.output.airplay" in sys.modules:
        del sys.modules["app.output.airplay"]
    from app.output import airplay
    return airplay


def _stub_binaries(monkeypatch, tmp_path, present: bool) -> None:
    """Point JUKEPLOX_*_BIN at real executable files or at nonexistent paths."""
    if present:
        c2 = tmp_path / "cliap2"
        cr = tmp_path / "cliraop"
        c2.write_text("#!/bin/sh\nexit 0\n")
        cr.write_text("#!/bin/sh\nexit 0\n")
        c2.chmod(0o755)
        cr.chmod(0o755)
        monkeypatch.setenv("JUKEPLOX_CLIAP2_BIN", str(c2))
        monkeypatch.setenv("JUKEPLOX_CLIRAOP_BIN", str(cr))
    else:
        monkeypatch.setenv("JUKEPLOX_CLIAP2_BIN", str(tmp_path / "nonexistent-cliap2"))
        monkeypatch.setenv("JUKEPLOX_CLIRAOP_BIN", str(tmp_path / "nonexistent-cliraop"))


def test_backend_satisfies_abstract_output_backend_protocol(airplay_module):
    """R8 — preserving the existing OutputDevice / AbstractOutputBackend
    contract means OutputRouter and the frontend don't need to change.
    """
    from app.output.base import AbstractOutputBackend
    backend = airplay_module.AirPlayBackend()
    assert isinstance(backend, AbstractOutputBackend)


def test_backend_instantiates_without_binaries(airplay_module, monkeypatch, tmp_path):
    """When the cliap2/cliraop binaries are absent (e.g. tests, dev outside
    Docker), the backend must still import + instantiate so the rest of the
    app starts. discover_devices returns [] instead of raising.
    """
    _stub_binaries(monkeypatch, tmp_path, present=False)
    backend = airplay_module.AirPlayBackend()
    assert airplay_module._binaries_available() is False
    assert backend.is_playing is False


@pytest.mark.asyncio
async def test_discover_returns_empty_when_binaries_missing(airplay_module, monkeypatch, tmp_path):
    """No binaries → no devices, no exceptions."""
    _stub_binaries(monkeypatch, tmp_path, present=False)
    backend = airplay_module.AirPlayBackend()
    assert await backend.discover_devices() == []


def test_binaries_available_when_executable_files_present(airplay_module, monkeypatch, tmp_path):
    """Inverse of the missing case: real executable files → True."""
    _stub_binaries(monkeypatch, tmp_path, present=True)
    assert airplay_module._binaries_available() is True


@pytest.mark.asyncio
async def test_play_raises_device_not_ready_without_binaries(
    airplay_module, monkeypatch, tmp_path
):
    """Calling play() with missing binaries surfaces DeviceNotReadyError so
    the queue advance loop (app/state.py:_do_advance) treats it as a halt
    rather than draining the queue with playback failures."""
    from app.output.base import DeviceNotReadyError
    _stub_binaries(monkeypatch, tmp_path, present=False)
    backend = airplay_module.AirPlayBackend()
    with pytest.raises(DeviceNotReadyError):
        await backend.play("http://example/stream", _DummyTrack())


@pytest.mark.asyncio
async def test_play_raises_when_no_device_selected(airplay_module, monkeypatch, tmp_path):
    """Binaries exist but set_device() was not called → DeviceNotReadyError."""
    from app.output.base import DeviceNotReadyError
    _stub_binaries(monkeypatch, tmp_path, present=True)
    backend = airplay_module.AirPlayBackend()
    with pytest.raises(DeviceNotReadyError):
        await backend.play("http://example/stream", _DummyTrack())


@pytest.mark.asyncio
async def test_set_volume_clamps_and_persists(airplay_module, monkeypatch, tmp_path):
    """Volume clamps to [0.0, 1.0] and persists under the existing
    vol:airplay:<device_id> key shape so saved settings carry over from the
    pyatv-era implementation without a migration."""
    _stub_binaries(monkeypatch, tmp_path, present=True)
    backend = airplay_module.AirPlayBackend()
    backend._device_id = "192.168.1.20:7000"

    persisted: dict[str, str] = {}

    from app import database as db

    async def _fake_set_setting(key, value):
        persisted[key] = value

    monkeypatch.setattr(db, "set_setting", _fake_set_setting)

    await backend.set_volume(1.5)
    assert backend._volume == 1.0
    await backend.set_volume(-0.3)
    assert backend._volume == 0.0
    await backend.set_volume(0.7)
    assert backend._volume == 0.7
    assert persisted["vol:airplay:192.168.1.20:7000"] == "0.7"


@pytest.mark.asyncio
async def test_set_volume_stamps_echo_guard(airplay_module, monkeypatch, tmp_path):
    """The echo-guard timestamp is stamped on every set_volume so subsequent
    DACP callbacks within the window are suppressed by echo_guard_active."""
    _stub_binaries(monkeypatch, tmp_path, present=True)
    backend = airplay_module.AirPlayBackend()
    backend._device_id = "x:1"

    from app import database as db

    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr(db, "set_setting", _noop)

    pre = backend._vol_last_set
    await backend.set_volume(0.5)
    assert backend._vol_last_set > pre


@pytest.mark.asyncio
async def test_position_is_zero_when_no_session(airplay_module, monkeypatch, tmp_path):
    """Before play() runs (or after teardown), there's no anchor so
    get_position returns 0. cliap2 / cliraop are stream-forward only
    and don't surface elapsed-position over our command pipe; we infer
    it from a monotonic anchor set when play() spawns the binary."""
    _stub_binaries(monkeypatch, tmp_path, present=True)
    backend = airplay_module.AirPlayBackend()
    assert backend._playback_started_at is None
    assert await backend.get_position() == 0


@pytest.mark.asyncio
async def test_position_returns_zero_during_ntp_delay_window(airplay_module, monkeypatch, tmp_path):
    """The anchor is set to `now + ntp_delay_s` so audio doesn't
    actually start until the speaker's clock crosses t0. During the
    delay window, get_position must clamp to 0 rather than returning a
    negative number that would look like a backwards seek to the UI."""
    import time
    _stub_binaries(monkeypatch, tmp_path, present=True)
    backend = airplay_module.AirPlayBackend()
    # Anchor is 5 seconds in the future (the cliap2-style 4s delay
    # plus some headroom). Position must be 0, not negative.
    backend._playback_started_at = time.monotonic() + 5.0
    assert await backend.get_position() == 0


@pytest.mark.asyncio
async def test_position_reports_elapsed_after_anchor(airplay_module, monkeypatch, tmp_path):
    """After the anchor passes, get_position reports elapsed ms since
    the anchor — that's what drives the admin progress bar."""
    import time
    _stub_binaries(monkeypatch, tmp_path, present=True)
    backend = airplay_module.AirPlayBackend()
    backend._playback_started_at = time.monotonic() - 3.5  # 3.5s ago
    position_ms = await backend.get_position()
    # Floating-point tolerance around 3500ms; just assert close.
    assert 3400 <= position_ms <= 3600


def test_airplay_ntp_startup_delay_differs_per_binary(airplay_module):
    """cliap2 needs 4s for AP2 pair-verify; cliraop needs 0 because its
    own session-establishment plus 1250ms player buffer is enough
    headroom. Stacking 4s on top of cliraop's internal buffers gave a
    ~5s perceived delay on track-start, verified against WiiM Pro logs
    from 2026-06-07 07:41:08Z."""
    assert airplay_module._AIRPLAY_NTP_STARTUP_DELAY_S["cliap2"] == 4
    assert airplay_module._AIRPLAY_NTP_STARTUP_DELAY_S["cliraop"] == 0


@pytest.mark.asyncio
async def test_seek_is_noop(airplay_module, monkeypatch, tmp_path):
    """Seek does not raise; the queue engine handles seek by stopping +
    re-spawning at the new offset rather than mid-stream seek."""
    _stub_binaries(monkeypatch, tmp_path, present=True)
    backend = airplay_module.AirPlayBackend()
    await backend.seek(30_000)


@pytest.mark.asyncio
async def test_set_device_records_id_when_binaries_present(
    airplay_module, monkeypatch, tmp_path
):
    """set_device records the device_id so subsequent play() doesn't raise
    the 'No AirPlay device selected' guard. The discovery cache must be
    populated first; U8's discover_devices() does that."""
    _stub_binaries(monkeypatch, tmp_path, present=True)
    from app import database as db
    async def _get(k): return None
    async def _set(*a, **kw): pass
    monkeypatch.setattr(db, "get_setting", _get)
    monkeypatch.setattr(db, "set_setting", _set)

    backend = airplay_module.AirPlayBackend()
    backend._device_addr["192.168.1.20:7000"] = ("JBL", "192.168.1.20", 7000, {})
    await backend.set_device("192.168.1.20:7000")
    assert backend._device_id == "192.168.1.20:7000"


@pytest.mark.asyncio
async def test_set_device_no_op_when_binaries_missing(airplay_module, monkeypatch, tmp_path):
    """Missing binaries → set_device leaves _device_id unset, so play() then
    raises the binaries-missing DeviceNotReadyError rather than the
    no-device-selected one."""
    _stub_binaries(monkeypatch, tmp_path, present=False)
    backend = airplay_module.AirPlayBackend()
    await backend.set_device("any")
    assert backend._device_id is None


def test_no_pyatv_import_in_airplay_module(airplay_module):
    """R6 / AE7 — pyatv is removed from the project."""
    import inspect
    source = inspect.getsource(airplay_module)
    assert "import pyatv" not in source
    assert "from pyatv" not in source


def test_no_pyatv_in_project_dependencies():
    """AE7 — pyproject.toml must not list pyatv as a dependency."""
    import pathlib
    pyproject = pathlib.Path(__file__).parent.parent / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    assert "pyatv" not in text


class _DummyTrack:
    """Minimal Track-shaped object — just enough for play() signatures."""
    id = "track-id"
    title = "Test Title"
    artist = "Test Artist"
    album = "Test Album"
    thumb = None
    duration_ms = 180_000
    stream_key = "key"
    container = "flac"


# ───────────────────────────────────────────────────────────────────────────
# U4: FFmpeg + cliap2 subprocess lifecycle
# ───────────────────────────────────────────────────────────────────────────


def test_build_cliap2_args_includes_required_flags(airplay_module):
    """cliap2's CLI surface (verified against cliap2 1.5 --help) needs
    --name, --address, --hostname, --port, --txt, --ntpstart, --volume,
    --dacp_id, --pipe -, --command_pipe. Test the pure helper so we don't
    have to spawn anything."""
    args = airplay_module._build_cliap2_args(
        binary="/usr/local/bin/cliap2",
        name="JBL Charge 5 Wi-Fi SE",
        host="192.168.1.20",
        port=7000,
        txt={"am": "JBLChargeAlfa", "cn": "0,1,2,3", "et": "0,3,5"},
        ntp_start=17136054317026503486,  # 64-bit NTP timestamp
        volume_pct=70,
        dacp_id="ABCD1234EF567890",
        active_remote_id=12345678,
        cmd_pipe_path="/tmp/jukeplox-cliap2-test.cmd",
        latency_ms=1000,
        session_establishment_latency_ms=500,
    )
    # Binary first, then flag pairs.
    assert args[0] == "/usr/local/bin/cliap2"
    # Every required flag present.
    for flag in (
        "--name", "--address", "--port", "--txt", "--ntpstart", "--volume",
        "--dacp_id", "--pipe", "--command_pipe", "--latency",
        "--session_establishment_latency",
    ):
        assert flag in args, f"missing required flag {flag}"
    # Specific values land in the right slot.
    assert args[args.index("--address") + 1] == "192.168.1.20"
    assert args[args.index("--port") + 1] == "7000"
    assert args[args.index("--volume") + 1] == "70"
    assert args[args.index("--dacp_id") + 1] == "ABCD1234EF567890"
    assert args[args.index("--command_pipe") + 1] == "/tmp/jukeplox-cliap2-test.cmd"
    # --pipe accepts "-" meaning stdin.
    assert args[args.index("--pipe") + 1] == "-"


def test_build_cliraop_args_uses_single_dash_flags_and_positional_host(airplay_module):
    """cliraop's CLI is meaningfully different from cliap2's: single-dash
    flags, positional `<player_ip> <filename>` AT THE END, and individual
    -am/-et/-md/-pk flags instead of a single --txt blob. Verified against
    `cliraop --help` in the deployed binary."""
    args = airplay_module._build_cliraop_args(
        binary="/usr/local/bin/cliraop",
        host="192.168.1.20",
        port=5000,
        txt={"am": "WiiM Pro", "et": "0,1", "md": "0,1,2"},
        ntp_start=17136054317026503486,
        volume_pct=70,
        dacp_id="ABCD1234EF567890",
        active_remote_id=12345678,
        cmd_pipe_path="/tmp/jukeplox-cliraop-test.cmd",
    )
    assert args[0] == "/usr/local/bin/cliraop"
    # cliraop uses single-dash flags (NOT --double-dash like cliap2).
    for flag in ("-port", "-volume", "-dacp", "-activeremote", "-ntpstart",
                 "-cmdpipe", "-debug", "-am", "-et", "-md"):
        assert flag in args, f"missing required flag {flag}"
    # No --double-dash flags — those would be parsed as positional by
    # cliraop and break.
    for double_dash in ("--port", "--volume", "--dacp", "--command_pipe",
                        "--active_remote", "--name"):
        assert double_dash not in args, f"cliraop must not see {double_dash}"
    assert args[args.index("-port") + 1] == "5000"
    assert args[args.index("-volume") + 1] == "70"
    assert args[args.index("-dacp") + 1] == "ABCD1234EF567890"
    assert args[args.index("-activeremote") + 1] == "12345678"
    assert args[args.index("-cmdpipe") + 1] == "/tmp/jukeplox-cliraop-test.cmd"
    assert args[args.index("-am") + 1] == "WiiM Pro"
    # Positional host + filename must come AFTER all flags per cliraop's
    # argv parser. The last two positions are the speaker IP then '-'
    # (stdin sentinel).
    assert args[-2] == "192.168.1.20"
    assert args[-1] == "-"


def test_build_cliraop_args_omits_unset_txt_fields(airplay_module):
    """When the receiver doesn't advertise a `pk` field, cliraop must not
    see `-pk` with an empty value — that would make it try to validate
    an empty pairing key and refuse the connection."""
    args = airplay_module._build_cliraop_args(
        binary="/usr/local/bin/cliraop",
        host="192.168.1.20",
        port=5000,
        txt={"am": "WiiM Pro"},  # only am present
        ntp_start=17136054317026503486,
        volume_pct=70,
        dacp_id="ABCD",
        active_remote_id=1,
        cmd_pipe_path="/tmp/test.cmd",
    )
    assert "-pk" not in args
    assert "-pw" not in args
    assert "-et" not in args
    assert "-md" not in args
    # -am IS present because it was advertised.
    assert "-am" in args


def test_use_ap2_true_when_features_bit_38_set(airplay_module):
    """`features` bit 38 = 0x40_0000_0000 is the AP2-audio capability flag
    advertised by AP2-capable receivers. Permissive on purpose: a True
    return lets `play()` try cliap2, and the discovery-time probe
    plus the "No audio?" recovery absorb over-permissive cases (e.g.
    the WiiM Pro that advertises bit 38 but silently fails cliap2)."""
    # High word 0x40 sets bit 38 of the combined 64-bit features value.
    txt = {"features": "0x00000003,0x00000040"}
    assert airplay_module._use_ap2(txt) is True


def test_use_ap2_true_when_only_pk_present(airplay_module):
    """`pk` (HAP public key) is a positive AP2 signal even without
    `features`. The probe is the safety net for AP1 receivers that
    happen to advertise `pk` for non-AP2 authentication."""
    txt = {"pk": "0123abcd" * 8, "am": "WiiM Pro"}
    assert airplay_module._use_ap2(txt) is True


def test_use_ap2_true_when_both_pk_and_features_bit_set(airplay_module):
    """Both signals together — typical HomePod / Apple TV 4K shape."""
    txt = {"pk": "0123abcd" * 8, "features": "0x00000003,0x00000040"}
    assert airplay_module._use_ap2(txt) is True


def test_use_ap2_false_for_raop_only_receiver(airplay_module):
    """A receiver advertising neither `pk` nor a features value with
    bit 38 set is AP1-only by this heuristic."""
    txt = {"am": "WiiM Pro", "et": "0,1", "md": "0,1,2", "cn": "0,1,2,3"}
    assert airplay_module._use_ap2(txt) is False


def test_use_ap2_false_when_features_lacks_ap2_bit(airplay_module):
    """A `features` value present but without bit 38 is AP1-only.
    Bit 38 = 0x40_0000_0000; a value with only low-word bits set
    leaves bit 38 clear."""
    txt = {"features": "0x445F8A00,0x00000000"}
    assert airplay_module._use_ap2(txt) is False


def test_use_ap2_true_for_ft_abbreviation(airplay_module):
    """`ft` is the two-letter abbreviation of `features` used by some
    receivers. The heuristic treats both keys identically."""
    txt = {"ft": "0x00000003,0x00000040"}
    assert airplay_module._use_ap2(txt) is True


def test_use_ap2_handles_malformed_features_value(airplay_module):
    """A broken `features` value (non-hex garbage) returns False rather
    than crashing — conservatively misclassifies as AP1, which cliraop
    handles correctly even on AP2-capable receivers."""
    txt = {"features": "not a hex value"}
    assert airplay_module._use_ap2(txt) is False


@pytest.mark.asyncio
async def test_per_device_protocol_roundtrip(airplay_module, monkeypatch):
    """The persistence helpers store and retrieve `ap2`/`ap1` via
    database.get_setting / set_setting, keyed by device_id."""
    from app import database
    store: dict[str, str] = {}

    async def _fake_get(key, default=None):
        return store.get(key, default)

    async def _fake_set(key, value):
        store[key] = value

    monkeypatch.setattr(database, "get_setting", _fake_get)
    monkeypatch.setattr(database, "set_setting", _fake_set)

    assert await airplay_module._get_per_device_protocol("dev1") is None
    await airplay_module._set_per_device_protocol("dev1", "ap1")
    assert await airplay_module._get_per_device_protocol("dev1") == "ap1"
    await airplay_module._set_per_device_protocol("dev1", "ap2")
    assert await airplay_module._get_per_device_protocol("dev1") == "ap2"
    # Other device unaffected.
    assert await airplay_module._get_per_device_protocol("dev2") is None


@pytest.mark.asyncio
async def test_per_device_protocol_ignores_invalid_cached_value(
    airplay_module, monkeypatch
):
    """A stray value in the settings DB (manual edit, schema drift) is
    treated as 'no cached value' rather than coerced or trusted blindly."""
    from app import database

    async def _fake_get(key, default=None):
        return "garbage" if key.startswith("airplay:protocol:") else default

    monkeypatch.setattr(database, "get_setting", _fake_get)
    assert await airplay_module._get_per_device_protocol("dev1") is None


@pytest.mark.asyncio
async def test_set_per_device_protocol_rejects_invalid_value(airplay_module):
    """Writers always know which verdict they intend; refusing invalid
    inputs prevents accidentally persisting an uninterpretable value."""
    with pytest.raises(ValueError):
        await airplay_module._set_per_device_protocol("dev1", "neither")


def test_format_txt_kv_uses_quoted_space_format(airplay_module):
    """cliap2 1.5 rejects comma-joined TXT (`am=X,cn=Y`) with a FATAL parse
    error: 'Keyval string must start with a double quote'. The accepted
    format is `"key=value" "key=value"` per its main: txt parser. Verified
    by running cliap2 with both formats — comma exits with code 255, quoted
    space proceeds to bind sockets and open the command pipe."""
    out = airplay_module._format_txt_kv({"am": "JBL", "cn": "0,1,2,3", "et": "0,3,5"})
    # Each entry is its own quoted KV pair.
    assert '"am=JBL"' in out
    assert '"cn=0,1,2,3"' in out
    assert '"et=0,3,5"' in out
    # Pairs are separated by a single space, NOT a comma. A bare comma
    # outside a quoted value would cause cliap2's parser to choke on the
    # next entry's first character.
    assert " " in out
    # No bare commas between quoted entries.
    assert '","' not in out


def test_ntp_now_returns_64bit_ntp_timestamp(airplay_module, monkeypatch):
    """cliap2 1.5's --ntpstart expects a 64-bit NTP timestamp: high 32 bits
    = seconds since 1900, low 32 bits = fractional second. Verified by
    matching against cliap2 --ntp output. Microseconds-since-NTP-epoch
    (the prior implementation) caused cliap2 to log
    'Audio starts in 305583489 secs' — silent hang for the user."""
    import time as _time
    # Pin time so the math is deterministic.
    monkeypatch.setattr(_time, "time", lambda: 1780809756.5)

    ntp = airplay_module._ntp_now()
    NTP_EPOCH_OFFSET_S = 2208988800
    expected_seconds = 1780809756 + NTP_EPOCH_OFFSET_S
    # High 32 bits = seconds since 1900.
    assert (ntp >> 32) == expected_seconds
    # Low 32 bits = fractional second. For .5 the value is half of 2^32.
    fraction = ntp & 0xFFFFFFFF
    assert abs(fraction - (1 << 31)) < 2  # exact = 2^31 modulo float rounding


def test_build_ffmpeg_args_emits_44100_16bit_stereo_pcm(airplay_module):
    """cliap2 expects raw 16-bit signed little-endian 44.1 kHz stereo PCM on
    stdin (AIRPLAY_PCM_FORMAT in MA's constants.py)."""
    args = airplay_module._build_ffmpeg_args("http://plex.local/stream?key=abc")
    assert args[0] == "ffmpeg"
    assert "-i" in args
    assert args[args.index("-i") + 1] == "http://plex.local/stream?key=abc"
    # Output format: signed little-endian 16-bit PCM.
    assert "-f" in args and args[args.index("-f") + 1] == "s16le"
    # 44.1 kHz sample rate, 2 channels.
    assert "-ar" in args and args[args.index("-ar") + 1] == "44100"
    assert "-ac" in args and args[args.index("-ac") + 1] == "2"
    # stdout target.
    assert args[-1] == "pipe:1"


@pytest.mark.asyncio
async def test_play_spawns_cliap2_then_ffmpeg(airplay_module, monkeypatch, tmp_path):
    """Spawn order: cliap2 first (with stdin pointed at the read end of an
    os.pipe()), then ffmpeg (with stdout pointed at the write end). The OS
    pipe carries PCM audio between the two; Python doesn't touch it."""
    _stub_binaries(monkeypatch, tmp_path, present=True)

    spawned: list[tuple[str, ...]] = []
    procs: list[_FakeProc] = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        spawned.append(tuple(args))
        proc = _FakeProc(name=args[0])
        procs.append(proc)
        return proc

    monkeypatch.setattr(
        "asyncio.create_subprocess_exec", fake_create_subprocess_exec
    )

    # FIFO + OS pipe stubs so we don't actually touch the filesystem.
    # Windows lacks os.mkfifo natively; raising=False lets monkeypatch create
    # the attribute on the os module just for this test scope.
    monkeypatch.setattr(airplay_module.os, "mkfifo", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(airplay_module.os, "pipe", lambda: (3, 4))
    monkeypatch.setattr(airplay_module.os, "close", lambda fd: None)
    monkeypatch.setattr(airplay_module.os, "unlink", lambda p: None)

    async def _fake_open_cmd_pipe(self, path):
        # Don't actually open a fifo; just install a sentinel writer.
        self._cmd_pipe_writer = _FakeWriter()

    monkeypatch.setattr(
        airplay_module.AirPlayBackend, "_open_cmd_pipe_writer", _fake_open_cmd_pipe
    )

    from app import database as db
    async def _get(k): return None
    async def _set(*a, **kw): pass
    monkeypatch.setattr(db, "get_setting", _get)
    monkeypatch.setattr(db, "set_setting", _set)

    backend = airplay_module.AirPlayBackend()
    # _use_ap2 returns False unconditionally today (forces cliraop for
    # every device). To keep coverage on play()'s cliap2 branch — which
    # is dormant but not deleted, ready for future
    # _airplay._tcp.local-based routing — monkeypatch _use_ap2 to True
    # so this test exercises the cliap2 spawn path explicitly.
    monkeypatch.setattr(airplay_module, "_use_ap2", lambda txt: True)
    backend._device_addr["d1"] = (
        "JBL", "192.168.1.20", 7000,
        {"am": "JBL", "features": "0x00000003,0x00000040"},
    )
    await backend.set_device("d1")
    await backend.play("http://stream/abc", _DummyTrack())

    # Two processes spawned.
    assert len(spawned) == 2
    # cliap2 first.
    assert spawned[0][0].endswith("cliap2") or spawned[0][0].endswith("cliap2.exe")
    # ffmpeg second.
    assert spawned[1][0] == "ffmpeg"
    # is_playing now True.
    assert backend.is_playing is True
    # Subprocess handles tracked on the backend.
    assert backend._cliap2_proc is procs[0]
    assert backend._ffmpeg_proc is procs[1]


@pytest.mark.asyncio
async def test_play_spawns_cliraop_for_raop_only_receiver(airplay_module, monkeypatch, tmp_path):
    """RAOP-only receivers (no `features` AP2 bit, no `pk`) must route to
    cliraop, NOT cliap2. cliap2 against a RAOP-only receiver logs
    'Not using AirPlay 2 ...' and silently buffers PCM without ever
    transmitting — verified against the WiiM Pro in TrueNAS logs from
    2026-06-07 06:53:28Z. This test pins the routing decision."""
    _stub_binaries(monkeypatch, tmp_path, present=True)

    spawned: list[tuple[str, ...]] = []
    procs: list[_FakeProc] = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        spawned.append(tuple(args))
        proc = _FakeProc(name=args[0])
        procs.append(proc)
        return proc

    monkeypatch.setattr(
        "asyncio.create_subprocess_exec", fake_create_subprocess_exec
    )
    monkeypatch.setattr(airplay_module.os, "mkfifo", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(airplay_module.os, "pipe", lambda: (3, 4))
    monkeypatch.setattr(airplay_module.os, "close", lambda fd: None)
    monkeypatch.setattr(airplay_module.os, "unlink", lambda p: None)

    async def _fake_open_cmd_pipe(self, path):
        self._cmd_pipe_writer = _FakeWriter()

    monkeypatch.setattr(
        airplay_module.AirPlayBackend, "_open_cmd_pipe_writer", _fake_open_cmd_pipe
    )

    from app import database as db
    async def _get(k): return None
    async def _set(*a, **kw): pass
    monkeypatch.setattr(db, "get_setting", _get)
    monkeypatch.setattr(db, "set_setting", _set)

    backend = airplay_module.AirPlayBackend()
    # The WiiM Pro shape from the TrueNAS logs — just am/et/md/cn,
    # no features and no pk. _use_ap2 returns False.
    backend._device_addr["wiim"] = (
        "A1B2C3D4E5F6@WiiM Pro-E5F6", "192.168.1.42", 7000,
        {"am": "WiiM Pro", "et": "0,1", "md": "0,1,2", "cn": "0,1,2,3"},
    )
    await backend.set_device("wiim")
    await backend.play("http://stream/abc", _DummyTrack())

    assert len(spawned) == 2
    # cliraop first (NOT cliap2).
    assert spawned[0][0].endswith("cliraop") or spawned[0][0].endswith("cliraop.exe")
    # cliap2 must NOT appear.
    assert not (spawned[0][0].endswith("cliap2") or spawned[0][0].endswith("cliap2.exe"))
    # ffmpeg second.
    assert spawned[1][0] == "ffmpeg"
    # cliraop's positional args (speaker IP + stdin sentinel) at the end.
    cliraop_argv = list(spawned[0])
    assert cliraop_argv[-2] == "192.168.1.42"
    assert cliraop_argv[-1] == "-"


@pytest.mark.asyncio
async def test_stop_sends_action_stop_and_terminates_processes(
    airplay_module, monkeypatch, tmp_path
):
    """stop() writes ACTION=STOP\\n to the command pipe (best-effort), then
    terminates cliap2 and ffmpeg with SIGTERM. ACTION=STOP lets cliap2 close
    the RTSP session cleanly so the speaker doesn't show a stuck-stream
    indicator."""
    _stub_binaries(monkeypatch, tmp_path, present=True)

    backend = airplay_module.AirPlayBackend()
    fake_cliap2 = _FakeProc(name="cliap2")
    fake_ffmpeg = _FakeProc(name="ffmpeg")
    fake_writer = _FakeWriter()

    backend._cliap2_proc = fake_cliap2
    backend._ffmpeg_proc = fake_ffmpeg
    backend._cmd_pipe_writer = fake_writer
    backend._cmd_pipe_path = str(tmp_path / "fifo")
    (tmp_path / "fifo").write_text("")  # so unlink succeeds
    backend._is_playing = True

    await backend.stop()

    assert b"ACTION=STOP\n" in fake_writer.written
    assert fake_cliap2.terminated is True
    assert fake_ffmpeg.terminated is True
    assert backend._is_playing is False
    assert backend._stop_requested is True


@pytest.mark.asyncio
async def test_stop_when_nothing_playing_is_noop(airplay_module, monkeypatch, tmp_path):
    """stop() with no live subprocess does not raise. The queue engine calls
    stop() during backend swaps without checking is_playing first; tolerating
    the empty state keeps that path simple."""
    _stub_binaries(monkeypatch, tmp_path, present=False)
    backend = airplay_module.AirPlayBackend()
    await backend.stop()  # must not raise
    assert backend._is_playing is False


@pytest.mark.asyncio
async def test_play_tears_down_existing_session_before_new_spawn(
    airplay_module, monkeypatch, tmp_path
):
    """Back-to-back play() calls teardown the old chain before spawning the
    new one. Without this, the old cliap2 keeps streaming alongside the new
    one and the speaker double-buffers."""
    _stub_binaries(monkeypatch, tmp_path, present=True)

    spawned: list[tuple[str, ...]] = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        spawned.append(tuple(args))
        return _FakeProc(name=args[0])

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)
    # Windows lacks os.mkfifo natively; raising=False lets monkeypatch create
    # the attribute on the os module just for this test scope.
    monkeypatch.setattr(airplay_module.os, "mkfifo", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(airplay_module.os, "pipe", lambda: (3, 4))
    monkeypatch.setattr(airplay_module.os, "close", lambda fd: None)
    monkeypatch.setattr(airplay_module.os, "unlink", lambda p: None)

    async def _fake_open_cmd_pipe(self, path):
        self._cmd_pipe_writer = _FakeWriter()

    monkeypatch.setattr(
        airplay_module.AirPlayBackend, "_open_cmd_pipe_writer", _fake_open_cmd_pipe
    )

    from app import database as db
    async def _get(k): return None
    async def _set(*a, **kw): pass
    monkeypatch.setattr(db, "get_setting", _get)
    monkeypatch.setattr(db, "set_setting", _set)

    backend = airplay_module.AirPlayBackend()
    backend._device_addr["d1"] = ("JBL", "192.168.1.20", 7000, {})
    await backend.set_device("d1")

    await backend.play("http://stream/track1", _DummyTrack())
    first_cliap2_proc = backend._cliap2_proc
    await backend.play("http://stream/track2", _DummyTrack())

    # 4 total spawns: 2 per play call (cliap2 + ffmpeg).
    assert len(spawned) == 4
    # The first cliap2 process got terminated during the second play's teardown.
    assert first_cliap2_proc.terminated is True
    # The current process handles are different from the first.
    assert backend._cliap2_proc is not first_cliap2_proc


async def _setup_play_backend_for_selection(
    airplay_module, monkeypatch, tmp_path, *, txt, cached_protocol
):
    """Wire up a backend ready to receive a single play() call, mocking
    every subprocess + filesystem touch. Returns (backend, spawned) where
    spawned is a list that fake_create_subprocess_exec appends to."""
    _stub_binaries(monkeypatch, tmp_path, present=True)
    spawned: list[tuple[str, ...]] = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        spawned.append(tuple(args))
        return _FakeProc(name=args[0])

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(airplay_module.os, "mkfifo", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(airplay_module.os, "pipe", lambda: (3, 4))
    monkeypatch.setattr(airplay_module.os, "close", lambda fd: None)
    monkeypatch.setattr(airplay_module.os, "unlink", lambda p: None)

    async def _fake_open_cmd_pipe(self, path):
        self._cmd_pipe_writer = _FakeWriter()

    monkeypatch.setattr(
        airplay_module.AirPlayBackend, "_open_cmd_pipe_writer", _fake_open_cmd_pipe
    )

    from app import database as db
    store: dict[str, str] = {}
    if cached_protocol is not None:
        store["airplay:protocol:d1"] = cached_protocol

    async def _get(k, default=None):
        return store.get(k, default)
    async def _set(k, v):
        store[k] = v

    monkeypatch.setattr(db, "get_setting", _get)
    monkeypatch.setattr(db, "set_setting", _set)

    backend = airplay_module.AirPlayBackend()
    backend._device_addr["d1"] = ("JBL", "192.168.1.20", 7000, txt)
    await backend.set_device("d1")
    return backend, spawned


@pytest.mark.asyncio
async def test_play_selects_cliraop_when_cached_ap1_regardless_of_txt(
    airplay_module, monkeypatch, tmp_path
):
    """Covers F2: a cached `ap1` verdict (from probe or 'No audio?' button)
    forces cliraop even when TXT advertises AP2. The cache is the
    authoritative override for a device known to silently fail cliap2."""
    txt_with_ap2 = {"pk": "abc", "features": "0x00000003,0x00000040"}
    backend, spawned = await _setup_play_backend_for_selection(
        airplay_module, monkeypatch, tmp_path,
        txt=txt_with_ap2, cached_protocol="ap1",
    )
    await backend.play("http://stream/track", _DummyTrack())
    # First spawn is the AirPlay binary, second is ffmpeg.
    assert spawned[0][0].endswith("cliraop")
    assert backend._airplay_binary == "cliraop"


@pytest.mark.asyncio
async def test_play_selects_cliap2_when_cached_ap2(
    airplay_module, monkeypatch, tmp_path
):
    """Covers F1: a cached `ap2` verdict (probe confirmed cliap2 works)
    routes to cliap2 directly without re-consulting the TXT heuristic."""
    txt = {"pk": "abc"}
    backend, spawned = await _setup_play_backend_for_selection(
        airplay_module, monkeypatch, tmp_path,
        txt=txt, cached_protocol="ap2",
    )
    await backend.play("http://stream/track", _DummyTrack())
    assert spawned[0][0].endswith("cliap2")
    assert backend._airplay_binary == "cliap2"


@pytest.mark.asyncio
async def test_play_selects_cliap2_when_no_cache_and_txt_signals_ap2(
    airplay_module, monkeypatch, tmp_path
):
    """No cache yet: fall back to the TXT heuristic. AP2-advertising TXT
    routes to cliap2 tentatively; the background probe will persist a
    verdict for the next play."""
    txt = {"pk": "abc"}
    backend, spawned = await _setup_play_backend_for_selection(
        airplay_module, monkeypatch, tmp_path,
        txt=txt, cached_protocol=None,
    )
    await backend.play("http://stream/track", _DummyTrack())
    assert spawned[0][0].endswith("cliap2")


@pytest.mark.asyncio
async def test_play_selects_cliraop_when_no_cache_and_txt_lacks_ap2(
    airplay_module, monkeypatch, tmp_path
):
    """No cache and TXT lacks AP2 signals: route to cliraop. No probe is
    needed for these devices because _use_ap2(txt) already says no."""
    txt = {"am": "old receiver", "et": "0,1"}
    backend, spawned = await _setup_play_backend_for_selection(
        airplay_module, monkeypatch, tmp_path,
        txt=txt, cached_protocol=None,
    )
    await backend.play("http://stream/track", _DummyTrack())
    assert spawned[0][0].endswith("cliraop")


@pytest.mark.asyncio
async def test_play_logs_binary_selection_source(
    airplay_module, monkeypatch, tmp_path, caplog
):
    """The selection source string ('cached ap1', 'cached ap2',
    'txt-heuristic ap2', 'txt-heuristic ap1') is logged at INFO so
    production logs make the routing decision visible at a glance."""
    import logging
    caplog.set_level(logging.INFO, logger="app.output.airplay")
    txt = {"pk": "abc"}
    backend, _ = await _setup_play_backend_for_selection(
        airplay_module, monkeypatch, tmp_path,
        txt=txt, cached_protocol="ap1",
    )
    await backend.play("http://stream/track", _DummyTrack())
    log_lines = [r.message for r in caplog.records]
    assert any(
        "selected cliraop" in line and "cached ap1" in line
        for line in log_lines
    ), f"Expected source-tagged selection log, got: {log_lines}"


# ── U2: Discovery-time background probe ──────────────────────────────────────


class _ProbeStderrProc:
    """Fake asyncio.subprocess.Process that emits a scripted stderr line
    sequence on demand. Used to drive `_watch_probe_stderr` to its
    decision point without spawning real cliap2."""

    def __init__(
        self,
        stderr_lines: list[bytes],
        returncode: int | None = None,
        delay_between_lines: float = 0.0,
    ) -> None:
        self._lines = list(stderr_lines)
        self.returncode = returncode  # None = still running until lines drained
        self.terminated = False
        self.killed = False
        self._delay = delay_between_lines
        self.stderr = self
        self.stdin = None

    async def readline(self) -> bytes:
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._lines:
            return self._lines.pop(0)
        # Out of lines — simulate stderr EOF
        return b""

    async def wait(self) -> int:
        return self.returncode if self.returncode is not None else 0

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


async def _setup_probe_backend(
    airplay_module, monkeypatch, tmp_path, *, stderr_lines, returncode=None,
    delay_between_lines=0.0, store=None, broadcasts=None
):
    """Wire up the AirPlay backend for a probe test with everything mocked.
    Returns the backend so the test can call _probe_device directly."""
    _stub_binaries(monkeypatch, tmp_path, present=True)

    fake_proc = _ProbeStderrProc(stderr_lines, returncode, delay_between_lines)

    async def fake_create_subprocess_exec(*args, **kwargs):
        return fake_proc

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(airplay_module.os, "mkfifo", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(airplay_module.os, "unlink", lambda p: None)

    from app import database as db
    _store = store if store is not None else {}

    async def _get(k, default=None):
        return _store.get(k, default)
    async def _set(k, v):
        _store[k] = v

    monkeypatch.setattr(db, "get_setting", _get)
    monkeypatch.setattr(db, "set_setting", _set)

    # Capture broadcasts.
    _broadcasts = broadcasts if broadcasts is not None else []
    from app.events import bus as bus_module
    async def _broadcast(event):
        _broadcasts.append(event)
    monkeypatch.setattr(bus_module.manager, "broadcast_to_admins", _broadcast)

    backend = airplay_module.AirPlayBackend()
    return backend, fake_proc, _store, _broadcasts


@pytest.mark.asyncio
async def test_probe_persists_ap1_on_not_using_airplay2_warning(
    airplay_module, monkeypatch, tmp_path
):
    """Covers F2. The WiiM-shape failure: cliap2 emits its WARN line
    formatted with a right-padded level field `[ WARN]` (not `[WARN]`)
    because cliap2 1.5 pads all log levels to 5 chars. Regression
    against the production-observed format from 2026-06-07 TrueNAS
    logs — the prior form `b"[WARN] ..."` was a test-format assumption
    that masked a regex mismatch in `_AIRPLAY_PROBE_FAILURE_RE`. Real
    cliap2 output:
        `[ WARN] [     cliap2 (32)]  airplay: Not using AirPlay 2 for
         device 'X' as it does not have required 'features' in TXT field`
    """
    stderr = [
        b"[ INFO] [     cliap2 (32)]     main: cliap2 version 1.5 taking off\n",
        b"[ WARN] [     cliap2 (32)]  airplay: Not using AirPlay 2 for device 'WiiM Pro' as it does not have required 'features' in TXT field\n",
    ]
    backend, _, store, broadcasts = await _setup_probe_backend(
        airplay_module, monkeypatch, tmp_path, stderr_lines=stderr,
    )
    verdict = await backend._probe_device(
        "d1", "WiiM Pro", "192.168.1.20", 7000, {"pk": "abc"},
    )
    assert verdict == "ap1"
    assert store["airplay:protocol:d1"] == "ap1"
    assert len(broadcasts) == 1
    assert broadcasts[0].device_id == "d1"
    assert broadcasts[0].protocol == "ap1"


def test_probe_failure_re_matches_cliap2_padded_log_format(airplay_module):
    """Lock in the regex's handling of cliap2 1.5's actual log format.
    cliap2 right-pads log levels to 5 chars: 4-char levels get a leading
    space (`[ WARN]`, `[ INFO]`, `[ SPAM]`), 3-char levels get two leading
    spaces (`[  LOG]`), 5-char levels are unpadded (`[DEBUG]`, `[FATAL]`,
    `[ERROR]`). The probe failure regex must match the failure-level
    variants (FATAL / ERROR / WARN-with-not-using-airplay-2) in both
    padded and unpadded forms so this doesn't regress if cliap2's logger
    changes its field width — and must NOT match unrelated WARN/INFO
    lines that would cause a false ap1 verdict."""
    re_ = airplay_module._AIRPLAY_PROBE_FAILURE_RE

    # WiiM-shape WARN, padded — the production format that previously slipped.
    assert re_.search(
        "[ WARN] [     cliap2 (32)]  airplay: Not using AirPlay 2 "
        "for device 'WiiM Pro' as it does not have required 'features' in TXT field"
    )
    # WARN unpadded — defensive, in case cliap2's logger field-width changes.
    assert re_.search("[WARN] Not using AirPlay 2 for device 'X'")
    # FATAL (5-char, unpadded — cliap2's actual format for FATAL).
    assert re_.search("[FATAL] [     cliap2 (32)] main: Pair-verify failed")
    # ERROR (5-char, unpadded).
    assert re_.search("[ERROR] [     cliap2 (32)] airplay: HAP setup timeout")

    # Negative cases — these must NOT match, or the probe falsely returns ap1.
    # The non-AirPlay-2 WARN is real production output during every play and
    # must not trigger a fallback by itself.
    assert not re_.search(
        "[ WARN] [     cliap2 (32)]   player: Output buffer duration is "
        "configured to a non-standard value 1250"
    )
    assert not re_.search("[ INFO] [     cliap2 (32)]     main: cliap2 version 1.5 taking off")
    assert not re_.search("[DEBUG] [     cliap2 (32)]     main: DACP ID set to: ABC")
    assert not re_.search("[ SPAM] [   mass_cmd (40)]     fifo: pipe_metadata_read_cb")


@pytest.mark.asyncio
async def test_probe_persists_ap1_on_fatal(airplay_module, monkeypatch, tmp_path):
    """`[FATAL]` from cliap2 (e.g. RTSP/HAP setup failure) → ap1 verdict."""
    stderr = [
        b"[INFO] Connecting\n",
        b"[FATAL] Pair-verify failed\n",
    ]
    backend, _, store, _ = await _setup_probe_backend(
        airplay_module, monkeypatch, tmp_path, stderr_lines=stderr,
    )
    verdict = await backend._probe_device("d1", "X", "1.2.3.4", 7000, {"pk": "a"})
    assert verdict == "ap1"
    assert store["airplay:protocol:d1"] == "ap1"


@pytest.mark.asyncio
async def test_probe_persists_ap1_on_nonzero_exit(
    airplay_module, monkeypatch, tmp_path
):
    """If cliap2 closes stderr (EOF) and proc.wait() returns non-zero,
    that's a process-level failure → ap1."""
    stderr = []  # immediate EOF
    backend, _, store, _ = await _setup_probe_backend(
        airplay_module, monkeypatch, tmp_path,
        stderr_lines=stderr, returncode=1,
    )
    verdict = await backend._probe_device("d1", "X", "1.2.3.4", 7000, {"pk": "a"})
    assert verdict == "ap1"
    assert store["airplay:protocol:d1"] == "ap1"


@pytest.mark.asyncio
async def test_probe_persists_ap2_when_stderr_clean_through_window(
    airplay_module, monkeypatch, tmp_path
):
    """Covers F1. Clean stderr through the probe window means cliap2
    successfully reached pair-verified + RTSP setup → ap2."""
    # Use a short window override so the test doesn't actually wait 8s.
    monkeypatch.setattr(airplay_module, "_AIRPLAY_PROBE_WINDOW_S", 0.2)
    # No failure-marker lines; some benign INFO logs.
    stderr = [
        b"[INFO] Connecting\n",
        b"[INFO] HAP pair-verify ok\n",
    ]
    backend, fake_proc, store, broadcasts = await _setup_probe_backend(
        airplay_module, monkeypatch, tmp_path,
        stderr_lines=stderr, returncode=None,
        delay_between_lines=0.05,  # keep cliap2 "running" through the window
    )
    verdict = await backend._probe_device("d1", "X", "1.2.3.4", 7000, {"pk": "a"})
    assert verdict == "ap2"
    assert store["airplay:protocol:d1"] == "ap2"
    assert broadcasts[0].protocol == "ap2"
    # Probe always SIGTERMs cliap2 at the end, even on clean ap2 verdict.
    assert fake_proc.terminated is True


@pytest.mark.asyncio
async def test_probe_always_sigterms_cliap2_even_on_ap2_verdict(
    airplay_module, monkeypatch, tmp_path
):
    """Explicit assertion of the SIGTERM-on-success contract. A long-running
    cliap2 with stdin=DEVNULL would otherwise hang forever after the
    probe verdict landed."""
    monkeypatch.setattr(airplay_module, "_AIRPLAY_PROBE_WINDOW_S", 0.1)
    backend, fake_proc, _, _ = await _setup_probe_backend(
        airplay_module, monkeypatch, tmp_path,
        stderr_lines=[b"[INFO] noise\n"],
        returncode=None,
        delay_between_lines=0.05,
    )
    verdict = await backend._probe_device("d1", "X", "1.2.3.4", 7000, {"pk": "a"})
    assert verdict == "ap2"
    assert fake_proc.terminated is True


@pytest.mark.asyncio
async def test_probe_is_idempotent_when_cached(
    airplay_module, monkeypatch, tmp_path
):
    """When the device already has a cached verdict, the probe returns
    that verdict immediately and does not spawn cliap2 again."""
    spawn_calls: list = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        spawn_calls.append(args)
        return _ProbeStderrProc([], returncode=0)

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)
    _stub_binaries(monkeypatch, tmp_path, present=True)
    monkeypatch.setattr(airplay_module.os, "mkfifo", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(airplay_module.os, "unlink", lambda p: None)

    from app import database as db
    store = {"airplay:protocol:d1": "ap1"}
    async def _get(k, default=None): return store.get(k, default)
    async def _set(k, v): store[k] = v
    monkeypatch.setattr(db, "get_setting", _get)
    monkeypatch.setattr(db, "set_setting", _set)

    from app.events import bus as bus_module
    async def _broadcast(event): pass
    monkeypatch.setattr(bus_module.manager, "broadcast_to_admins", _broadcast)

    backend = airplay_module.AirPlayBackend()
    verdict = await backend._probe_device("d1", "X", "1.2.3.4", 7000, {"pk": "a"})
    assert verdict == "ap1"
    assert spawn_calls == []  # never spawned cliap2


@pytest.mark.asyncio
async def test_probe_concurrent_callers_collapse_to_single_subprocess(
    airplay_module, monkeypatch, tmp_path
):
    """Two probes for the same device_id fired concurrently must serialize
    on the per-device lock. After the first completes, the second sees
    the cached verdict and returns it without spawning cliap2 again."""
    monkeypatch.setattr(airplay_module, "_AIRPLAY_PROBE_WINDOW_S", 0.1)
    spawn_calls: list = []
    procs: list[_ProbeStderrProc] = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        spawn_calls.append(args)
        # Slow stderr to keep the first probe in flight.
        proc = _ProbeStderrProc(
            [b"[INFO] hi\n"], returncode=None, delay_between_lines=0.05,
        )
        procs.append(proc)
        return proc

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)
    _stub_binaries(monkeypatch, tmp_path, present=True)
    monkeypatch.setattr(airplay_module.os, "mkfifo", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(airplay_module.os, "unlink", lambda p: None)

    from app import database as db
    store: dict[str, str] = {}
    async def _get(k, default=None): return store.get(k, default)
    async def _set(k, v): store[k] = v
    monkeypatch.setattr(db, "get_setting", _get)
    monkeypatch.setattr(db, "set_setting", _set)

    from app.events import bus as bus_module
    async def _broadcast(event): pass
    monkeypatch.setattr(bus_module.manager, "broadcast_to_admins", _broadcast)

    backend = airplay_module.AirPlayBackend()
    # Fire two concurrent probes for the same device.
    results = await asyncio.gather(
        backend._probe_device("d1", "X", "1.2.3.4", 7000, {"pk": "a"}),
        backend._probe_device("d1", "X", "1.2.3.4", 7000, {"pk": "a"}),
    )
    assert results == ["ap2", "ap2"]
    # Only one cliap2 subprocess spawned despite two callers.
    assert len(spawn_calls) == 1


@pytest.mark.asyncio
async def test_probe_does_not_block_discover_devices(
    airplay_module, monkeypatch, tmp_path
):
    """Probe is fire-and-forget — discover_devices returns on its existing
    latency budget regardless of probe duration. Verify by patching the
    probe to never complete and confirming discover_devices returns."""
    _stub_binaries(monkeypatch, tmp_path, present=True)

    # Stub the in-process discovery to return a single AP2-capable device.
    from app.output import mdns_zeroconf
    async def _fake_discover(service_type, aiozc=None):
        return [("WiiM Pro", "192.168.1.20", 7000, "uuid-1", {"pk": "abc"})]
    monkeypatch.setattr(mdns_zeroconf, "discover", _fake_discover)

    # No DB cache — probe will run.
    from app import database as db
    async def _get(k, default=None): return default
    async def _set(k, v): pass
    monkeypatch.setattr(db, "get_setting", _get)
    monkeypatch.setattr(db, "set_setting", _set)

    # Replace _probe_device with a hang. discover_devices should return
    # before the probe completes because the probe runs in a background task.
    probe_started = asyncio.Event()
    async def _hanging_probe(self, device_id, name, host, port, txt):
        probe_started.set()
        await asyncio.sleep(30)
    monkeypatch.setattr(
        airplay_module.AirPlayBackend, "_probe_device_if_unprobed", _hanging_probe,
    )

    backend = airplay_module.AirPlayBackend()
    devices = await asyncio.wait_for(backend.discover_devices(), timeout=2.0)
    assert len(devices) == 1
    # The probe was scheduled and started (proving discover_devices triggered it).
    await asyncio.wait_for(probe_started.wait(), timeout=1.0)


@pytest.mark.asyncio
async def test_probe_preserves_user_written_ap1_during_probe_window(
    airplay_module, monkeypatch, tmp_path
):
    """Race regression: while a probe is running (cliap2 stderr clean,
    will return ap2), the user clicks "No audio?" — no-audio persists
    ap1 directly. After the probe window closes, the probe MUST re-read
    the cache and keep the user's ap1 rather than clobber it with ap2.
    Without this re-check, the user's explicit silent-fail recovery
    silently reverts a few seconds later."""
    monkeypatch.setattr(airplay_module, "_AIRPLAY_PROBE_WINDOW_S", 0.1)
    _stub_binaries(monkeypatch, tmp_path, present=True)

    fake_proc = _ProbeStderrProc([b"[INFO] clean\n"], returncode=None, delay_between_lines=0.05)
    async def fake_create_subprocess_exec(*args, **kwargs):
        return fake_proc
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(airplay_module.os, "mkfifo", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(airplay_module.os, "unlink", lambda p: None)

    from app import database as db
    store: dict[str, str] = {}
    set_calls: list[tuple[str, str]] = []
    async def _get(k, default=None): return store.get(k, default)
    async def _set(k, v):
        store[k] = v
        set_calls.append((k, v))
    monkeypatch.setattr(db, "get_setting", _get)
    monkeypatch.setattr(db, "set_setting", _set)

    from app.events import bus as bus_module
    broadcasts: list = []
    async def _broadcast(event): broadcasts.append(event)
    monkeypatch.setattr(bus_module.manager, "broadcast_to_admins", _broadcast)

    backend = airplay_module.AirPlayBackend()

    # Simulate: user wrote ap1 while probe was in flight by pre-seeding
    # the store AFTER the probe starts. We do this by running the probe
    # and injecting the store mutation between _run_probe_subprocess and
    # the post-window re-check via a patched _run_probe_subprocess.
    real_run_probe = backend._run_probe_subprocess
    async def _run_then_user_writes(device_id, name, host, port, txt):
        verdict = await real_run_probe(device_id, name, host, port, txt)
        # Simulate the user clicking "No audio?" during the probe window:
        store["airplay:protocol:d1"] = "ap1"
        return verdict
    backend._run_probe_subprocess = _run_then_user_writes  # type: ignore[assignment]

    result = await backend._probe_device("d1", "X", "1.2.3.4", 7000, {"pk": "a"})
    assert result == "ap1", "user-written ap1 must survive the probe completing"
    # set_setting must NOT have been called by the probe — the user's
    # write is preserved as-is.
    assert ("airplay:protocol:d1", "ap2") not in set_calls
    # The probe-driven broadcast should NOT fire when we kept the user's value.
    # (broadcasts may still be empty because the probe returned early.)
    assert all(b.protocol != "ap2" for b in broadcasts)


@pytest.mark.asyncio
async def test_probe_force_true_bypasses_user_value_recheck(
    airplay_module, monkeypatch, tmp_path
):
    """Re-test (force=True) must actually re-probe and persist the new
    verdict even if a cached value exists. Without bypass, Re-test would
    short-circuit to the cached value and never run cliap2."""
    monkeypatch.setattr(airplay_module, "_AIRPLAY_PROBE_WINDOW_S", 0.1)
    _stub_binaries(monkeypatch, tmp_path, present=True)

    fake_proc = _ProbeStderrProc(
        [b"[WARN] Not using AirPlay 2 for device 'X'\n"], returncode=None,
        delay_between_lines=0.01,
    )
    async def fake_create_subprocess_exec(*args, **kwargs):
        return fake_proc
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(airplay_module.os, "mkfifo", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(airplay_module.os, "unlink", lambda p: None)

    from app import database as db
    store: dict[str, str] = {"airplay:protocol:d1": "ap2"}  # stale verdict
    async def _get(k, default=None): return store.get(k, default)
    async def _set(k, v): store[k] = v
    monkeypatch.setattr(db, "get_setting", _get)
    monkeypatch.setattr(db, "set_setting", _set)

    from app.events import bus as bus_module
    async def _broadcast(event): pass
    monkeypatch.setattr(bus_module.manager, "broadcast_to_admins", _broadcast)

    backend = airplay_module.AirPlayBackend()
    verdict = await backend._probe_device(
        "d1", "X", "1.2.3.4", 7000, {"pk": "a"}, force=True,
    )
    assert verdict == "ap1"  # new probe verdict overrides stale ap2
    assert store["airplay:protocol:d1"] == "ap1"


@pytest.mark.asyncio
async def test_discover_skips_probe_for_non_ap2_devices(
    airplay_module, monkeypatch, tmp_path
):
    """A device whose TXT lacks AP2 signals does not get probed —
    _use_ap2 returns False and the heuristic alone is enough to route
    to cliraop forever."""
    _stub_binaries(monkeypatch, tmp_path, present=True)

    from app.output import mdns_zeroconf
    async def _fake_discover(service_type, aiozc=None):
        # No pk, no features bit 38 — TXT doesn't advertise AP2.
        return [("OldAirport", "192.168.1.99", 7000, "uuid-2",
                 {"am": "AirPort Express"})]
    monkeypatch.setattr(mdns_zeroconf, "discover", _fake_discover)

    from app import database as db
    async def _get(k, default=None): return default
    async def _set(k, v): pass
    monkeypatch.setattr(db, "get_setting", _get)
    monkeypatch.setattr(db, "set_setting", _set)

    probe_count = 0
    async def _count_probe(self, device_id, name, host, port, txt):
        nonlocal probe_count
        probe_count += 1
    monkeypatch.setattr(
        airplay_module.AirPlayBackend, "_probe_device_if_unprobed", _count_probe,
    )

    backend = airplay_module.AirPlayBackend()
    await backend.discover_devices()
    # Yield a tick so any scheduled task would have run.
    await asyncio.sleep(0.05)
    assert probe_count == 0


# ───────────────────────────────────────────────────────────────────────────
# Helpers — fake asyncio.subprocess.Process and FIFO writer for U4 tests
# ───────────────────────────────────────────────────────────────────────────


class _FakeProc:
    """Stand-in for asyncio.subprocess.Process. Tracks terminate() / kill()
    calls and exposes await wait() that resolves immediately."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.terminated = False
        self.killed = False
        self.returncode = None
        # stdin / stderr exposed as sentinels — U6's stderr reader replaces
        # these in its own tests.
        self.stdin = None
        self.stderr = None

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode if self.returncode is not None else 0


class _FakeWriter:
    """Stand-in for asyncio.StreamWriter on the FIFO command pipe. Captures
    every byte written for assertion."""

    def __init__(self) -> None:
        self.written = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass

    def is_closing(self) -> bool:
        return self.closed


# ───────────────────────────────────────────────────────────────────────────
# U5: IPC command pipe — VOLUME, ACTION, metadata sends
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_volume_sends_volume_command(airplay_module, monkeypatch, tmp_path):
    """set_volume(0.7) writes VOLUME=70 to the command pipe. The 0-100 scale
    matches cliap2's --volume arg + the volumeup/volumedown DACP semantics."""
    _stub_binaries(monkeypatch, tmp_path, present=True)
    backend = airplay_module.AirPlayBackend()
    backend._device_id = "x:1"
    backend._cmd_pipe_writer = _FakeWriter()
    fake_writer = backend._cmd_pipe_writer

    from app import database as db
    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(db, "set_setting", _noop)

    await backend.set_volume(0.7)

    assert b"VOLUME=70\n" in fake_writer.written


@pytest.mark.asyncio
async def test_set_volume_clamped_command_values(airplay_module, monkeypatch, tmp_path):
    """1.5 → 100, -0.2 → 0. cliap2 rejects out-of-range volumes; clamping
    before the write keeps the contract clean from the application layer."""
    _stub_binaries(monkeypatch, tmp_path, present=True)
    backend = airplay_module.AirPlayBackend()
    backend._device_id = "x:1"
    backend._cmd_pipe_writer = _FakeWriter()
    fake_writer = backend._cmd_pipe_writer

    from app import database as db
    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(db, "set_setting", _noop)

    await backend.set_volume(1.5)
    await backend.set_volume(-0.2)

    assert b"VOLUME=100\n" in fake_writer.written
    assert b"VOLUME=0\n" in fake_writer.written


@pytest.mark.asyncio
async def test_set_volume_without_pipe_persists_and_does_not_raise(
    airplay_module, monkeypatch, tmp_path
):
    """When called before play() (e.g. restoring saved volume during
    set_device), the cmd pipe doesn't exist yet. The call still persists
    and updates state without raising."""
    _stub_binaries(monkeypatch, tmp_path, present=True)
    backend = airplay_module.AirPlayBackend()
    backend._device_id = "x:1"
    assert backend._cmd_pipe_writer is None

    from app import database as db
    persisted: dict[str, str] = {}
    async def _set(key, value):
        persisted[key] = value
    monkeypatch.setattr(db, "set_setting", _set)

    await backend.set_volume(0.4)

    assert persisted["vol:airplay:x:1"] == "0.4"
    assert backend._volume == 0.4


@pytest.mark.asyncio
async def test_pause_and_resume_send_action_commands(airplay_module, monkeypatch, tmp_path):
    """pause → ACTION=PAUSE; resume → ACTION=PLAY. cliap2 forwards these to
    the speaker via RTSP."""
    _stub_binaries(monkeypatch, tmp_path, present=True)
    backend = airplay_module.AirPlayBackend()
    backend._cmd_pipe_writer = _FakeWriter()
    fake_writer = backend._cmd_pipe_writer

    await backend.pause()
    await backend.resume()

    assert b"ACTION=PAUSE\n" in fake_writer.written
    assert b"ACTION=PLAY\n" in fake_writer.written


@pytest.mark.asyncio
async def test_send_metadata_batches_track_fields(airplay_module, monkeypatch, tmp_path):
    """Metadata batch order from MA's protocols/_protocol.py: TITLE, ARTIST,
    ALBUM, DURATION, ARTWORK, then ACTION=SENDMETA to flush. Each field on
    its own line; SENDMETA at the end tells cliap2 to push the accumulated
    fields to the receiver in a single RTSP SET_PARAMETER."""
    _stub_binaries(monkeypatch, tmp_path, present=True)
    backend = airplay_module.AirPlayBackend()
    backend._cmd_pipe_writer = _FakeWriter()
    fake_writer = backend._cmd_pipe_writer

    track = _DummyTrack()
    track.title = "Smells Like Teen Spirit"
    track.artist = "Nirvana"
    track.album = "Nevermind"
    track.duration_ms = 301_000
    track.thumb = "http://art/url.jpg"

    await backend._send_metadata(track)

    written = fake_writer.written
    assert b"TITLE=Smells Like Teen Spirit\n" in written
    assert b"ARTIST=Nirvana\n" in written
    assert b"ALBUM=Nevermind\n" in written
    assert b"DURATION=301000\n" in written
    assert b"ARTWORK=http://art/url.jpg\n" in written
    assert b"ACTION=SENDMETA\n" in written
    # SENDMETA must be last so cliap2 flushes after the fields are buffered.
    assert written.rindex(b"ACTION=SENDMETA") > written.rindex(b"TITLE=")


@pytest.mark.asyncio
async def test_send_metadata_skips_non_http_artwork(airplay_module, monkeypatch, tmp_path):
    """Plex thumbs come through as `<server-id>:/library/metadata/...` —
    not an HTTP URL. cliap2 tries to fetch it via libcurl, fails with
    'Unsupported protocol', then its artwork parser corrupts internal
    state and tears down the entire command pipe with 'Error parsing
    incoming data on command pipe ..., will stop reading'. After that
    every subsequent VOLUME / ACTION=PAUSE / ACTION=STOP write vanishes
    silently. Send ARTWORK only when it's a real HTTP(S) URL."""
    _stub_binaries(monkeypatch, tmp_path, present=True)
    backend = airplay_module.AirPlayBackend()
    backend._cmd_pipe_writer = _FakeWriter()
    fake_writer = backend._cmd_pipe_writer

    track = _DummyTrack()
    track.title = "Foo"
    track.artist = "Bar"
    track.album = "Baz"
    track.duration_ms = 42_000
    # The exact format from the TrueNAS logs that broke the command pipe.
    track.thumb = "feedbeef00112233445566778899aabbccddeeff:/library/metadata/76928/thumb/1764584001"

    await backend._send_metadata(track)

    written = fake_writer.written
    # Other fields still send.
    assert b"TITLE=Foo\n" in written
    assert b"ARTIST=Bar\n" in written
    assert b"ALBUM=Baz\n" in written
    assert b"DURATION=42000\n" in written
    # SENDMETA still fires.
    assert b"ACTION=SENDMETA\n" in written
    # But the Plex stream-key URL is suppressed.
    assert b"ARTWORK=" not in written


@pytest.mark.asyncio
async def test_send_metadata_sends_https_artwork(airplay_module, monkeypatch, tmp_path):
    """https:// artwork URLs pass through unchanged."""
    _stub_binaries(monkeypatch, tmp_path, present=True)
    backend = airplay_module.AirPlayBackend()
    backend._cmd_pipe_writer = _FakeWriter()
    fake_writer = backend._cmd_pipe_writer

    track = _DummyTrack()
    track.title = "Foo"
    track.thumb = "https://example.com/art.jpg"

    await backend._send_metadata(track)

    assert b"ARTWORK=https://example.com/art.jpg\n" in fake_writer.written


def test_ensure_deviceid_synthesizes_from_name_prefix(airplay_module):
    """cliap2 logs `airplay: AirPlay device 'X' is missing a device ID`
    when --txt doesn't carry `deviceid`. The avahi-advertised RAOP
    service name follows `<12-hex>@<friendly>` (e.g.
    `A1B2C3D4E5F6@WiiM Pro-E5F6`); extract the MAC and inject as a
    colon-separated `deviceid` so cliap2 has the id its RAOP code
    matches against."""
    out = airplay_module._ensure_deviceid(
        "A1B2C3D4E5F6@WiiM Pro-E5F6",
        {"am": "WiiM Pro", "cn": "0,1,2,3"},
    )
    assert out["deviceid"] == "A1:B2:C3:D4:E5:F6"
    # Other entries preserved.
    assert out["am"] == "WiiM Pro"
    assert out["cn"] == "0,1,2,3"


def test_ensure_deviceid_does_not_overwrite_existing(airplay_module):
    """A speaker that already advertises `deviceid` in its TXT records
    keeps the advertised value — never silently replace the speaker's
    own identity."""
    out = airplay_module._ensure_deviceid(
        "A1B2C3D4E5F6@WiiM Pro-E5F6",
        {"deviceid": "AA:BB:CC:DD:EE:FF", "am": "WiiM Pro"},
    )
    assert out["deviceid"] == "AA:BB:CC:DD:EE:FF"


def test_ensure_deviceid_skips_when_name_lacks_mac_prefix(airplay_module):
    """Some receivers (older Apple TVs, some HomePods) advertise their
    RAOP service with a non-`<MAC>@<friendly>` shape. We don't fabricate
    a deviceid from random characters — leave the TXT dict alone and let
    cliap2's degraded-mode path handle it."""
    out = airplay_module._ensure_deviceid(
        "Living Room Speaker",
        {"am": "AppleTV"},
    )
    assert "deviceid" not in out


def test_ensure_deviceid_returns_new_dict_not_mutation(airplay_module):
    """Caller's TXT dict must not be mutated — defensive against future
    callers who pass a shared/cached TXT dict from discovery state."""
    original = {"am": "WiiM Pro"}
    out = airplay_module._ensure_deviceid("A1B2C3D4E5F6@WiiM Pro-E5F6", original)
    assert out is not original
    assert "deviceid" not in original


def test_expand_airplay_txt_keys_promotes_short_form_to_long_form(airplay_module):
    """cliap2 reads `keyval_get(txt, "features")` and rejects the device
    when only the abbreviated `ft` is present. Our discovery pulls TXT
    from `_raop._tcp.local`, where the short forms (`ft`, `am`) are the
    norm; this helper exists to give cliap2 the long-form names it
    actually looks for."""
    out = airplay_module._expand_airplay_txt_keys(
        {"ft": "0x445D0A00,0x1C340", "am": "WiiM Pro", "cn": "0,1"},
    )
    assert out["features"] == "0x445D0A00,0x1C340"
    assert out["model"] == "WiiM Pro"
    # Short-form keys preserved alongside the expansions — helper does
    # not strip the originals, since other consumers may still read `ft`.
    assert out["ft"] == "0x445D0A00,0x1C340"
    assert out["am"] == "WiiM Pro"
    assert out["cn"] == "0,1"


def test_expand_airplay_txt_keys_does_not_overwrite_meaningful_long_form(airplay_module):
    """If a future discovery cycle ever surfaces TXT with both short and
    long keys (e.g. a device that also advertises on `_airplay._tcp.local`),
    the meaningful long-form value already there is authoritative — never
    silently overwrite it with the short-form value."""
    out = airplay_module._expand_airplay_txt_keys(
        {"features": "0xLONG", "ft": "0xSHORT", "model": "Real", "am": "Short"},
    )
    assert out["features"] == "0xLONG"
    assert out["model"] == "Real"


def test_expand_airplay_txt_keys_replaces_empty_long_form_with_short_value(airplay_module):
    """cliap2's `keyval_get(txt, "features")` returns an empty string the
    same way it returns NULL when the key is missing — either way the
    device gets rejected. So an empty `features=` (or `model=`) must be
    treated as no-value and replaced by the short-form value when one
    is present, rather than silently preserved."""
    out = airplay_module._expand_airplay_txt_keys(
        {"features": "", "ft": "0x445D0A00,0x1C340", "model": "", "am": "WiiM Pro"},
    )
    assert out["features"] == "0x445D0A00,0x1C340"
    assert out["model"] == "WiiM Pro"


def test_expand_airplay_txt_keys_is_noop_when_short_keys_absent(airplay_module):
    """Devices that already advertise the long-form keys (or that
    happen to omit `ft`/`am` entirely) pass through unchanged."""
    src = {"deviceid": "AA:BB:CC:DD:EE:FF", "cn": "0,1", "vs": "366.0"}
    out = airplay_module._expand_airplay_txt_keys(src)
    assert out == src


def test_expand_airplay_txt_keys_does_not_mutate_input(airplay_module):
    """The helper returns a new dict — callers that reuse the input
    elsewhere (e.g. for logging or persistence) must not see surprise
    new keys appear."""
    src = {"ft": "0x1,0x2", "am": "WiiM"}
    snapshot = dict(src)
    out = airplay_module._expand_airplay_txt_keys(src)
    assert src == snapshot
    assert out is not src


def test_build_cliap2_args_normalises_raw_discovery_txt(airplay_module):
    """`_build_cliap2_args` owns the TXT-prep chain (short-form expansion
    + deviceid synthesis) so callers cannot spawn cliap2 with raw
    `_raop._tcp.local` TXT that would trip cliap2's `Not using AirPlay 2`
    rejection. Pass raw discovery TXT in and verify the `--txt` value
    carries the long-form `features=` and `model=` keys cliap2 reads,
    plus the deviceid synthesised from the avahi service name. This pins
    the contract regardless of how many spawn call sites exist."""
    args = airplay_module._build_cliap2_args(
        binary="/usr/local/bin/cliap2",
        name="A1B2C3D4E5F6@WiiM Pro-E5F6",
        host="192.168.1.117",
        port=7000,
        txt={"ft": "0x445D0A00,0x1C340", "am": "WiiM Pro", "cn": "0,1"},
        ntp_start=17136054317026503486,
        volume_pct=50,
        dacp_id="ABCD1234EF567890",
        active_remote_id=12345678,
        cmd_pipe_path="/tmp/jukeplox-cliap2-test.cmd",
        latency_ms=1000,
        session_establishment_latency_ms=500,
    )
    txt_value = args[args.index("--txt") + 1]
    assert '"features=0x445D0A00,0x1C340"' in txt_value
    assert '"model=WiiM Pro"' in txt_value
    assert '"deviceid=A1:B2:C3:D4:E5:F6"' in txt_value


@pytest.mark.asyncio
async def test_send_command_tolerates_broken_pipe(airplay_module, monkeypatch, tmp_path):
    """When cliap2 has already exited, writes raise BrokenPipeError. The
    command-send path swallows that at WARNING level so the backend can
    still tear down cleanly."""
    _stub_binaries(monkeypatch, tmp_path, present=True)
    backend = airplay_module.AirPlayBackend()

    class _BrokenWriter:
        def write(self, data):
            raise BrokenPipeError("cliap2 already exited")
        async def drain(self):
            pass
        def close(self):
            pass
        async def wait_closed(self):
            pass
        def is_closing(self):
            return True

    backend._cmd_pipe_writer = _BrokenWriter()

    # Must not raise.
    await backend._send_command("VOLUME", "50")
    await backend.pause()


# ───────────────────────────────────────────────────────────────────────────
# U6: stderr status reader — EOS and error logging
# ───────────────────────────────────────────────────────────────────────────


def _fake_stderr(lines: list[bytes]) -> "asyncio.StreamReader":
    """Build an asyncio.StreamReader pre-loaded with `lines`, then EOF.
    Each line should end with b'\\n' to match cliap2's actual output."""
    import asyncio as _asyncio
    reader = _asyncio.StreamReader()
    for line in lines:
        reader.feed_data(line)
    reader.feed_eof()
    return reader


@pytest.mark.asyncio
async def test_stderr_end_of_stream_does_NOT_fire_advance(
    airplay_module, monkeypatch, tmp_path
):
    """Regression: cliap2 1.5 emits 'end of stream reached' whenever its
    input fifo briefly drains, then logs 'restarting w/o pause' and keeps
    running. The line is NOT a track-ended signal. Verified by running
    cliap2 in a container with no audio piped — it produces
    'end of stream reached' immediately on startup but the process stays
    alive.

    Treating this line as track-ended caused production no-audio on
    TrueNAS: cliap2 was alive, doing RTSP+RTP to the JBL/WiiM, but
    Jukeplox's stderr reader fired advance_cb prematurely on a transient
    input buffer drain. is_playing flipped to False; queue advance ran;
    the user heard nothing.

    The authoritative cliap2-exit signal is proc.wait() in
    _process_watcher_body, not a stderr substring. The reader's only job
    is to log lines and populate _stderr_tail.
    """
    _stub_binaries(monkeypatch, tmp_path, present=True)

    advance_called = False
    async def _advance():
        nonlocal advance_called
        advance_called = True

    backend = airplay_module.AirPlayBackend(advance_cb=_advance)
    backend._is_playing = True
    fake_proc = _FakeProc(name="cliap2")
    fake_proc.stderr = _fake_stderr([
        b"[INFO] [input] fifo: play:JBL:end of stream reached\n",
        b"[INFO] [mass_aud] fifo: pipe_watch_update:JBL: restarting w/o pause\n",
    ])
    backend._cliap2_proc = fake_proc

    await backend._stderr_reader_body(fake_proc.stderr)

    # The stderr line MUST NOT fire advance_cb. Only the process watcher's
    # proc.wait() return is authoritative for cliap2 exit.
    assert advance_called is False, (
        "end-of-stream line in cliap2 stderr is a transient input drain, "
        "not a track end — must not fire advance_cb"
    )
    # And is_playing stays True because cliap2 is still running.
    assert backend._is_playing is True


@pytest.mark.asyncio
async def test_pipe_writer_write_catches_runtime_error_from_closed_transport(
    airplay_module
):
    """uvloop's WriteUnixTransport raises RuntimeError(
    'unable to perform operation on ... the handler is closed') when the
    transport was closed underneath the writer (e.g., cliap2 exited).
    Before this fix _PipeWriter.write let it propagate, surfacing as a
    500 on POST /admin/playback/skip when the user pressed skip after
    cliap2 had exited. Wrap it as BrokenPipeError so _teardown's
    existing except clause catches it."""

    class _ClosedTransport:
        def write(self, data):
            raise RuntimeError(
                "unable to perform operation on "
                "<WriteUnixTransport closed=True>; the handler is closed"
            )

    writer = airplay_module._PipeWriter(_ClosedTransport())
    import pytest as _pytest
    with _pytest.raises(BrokenPipeError):
        writer.write(b"ACTION=STOP\n")
    # After the failed write, the writer marks itself closed so subsequent
    # writes skip the transport entirely.
    assert writer.is_closing() is True


@pytest.mark.asyncio
async def test_stderr_error_lines_buffer_in_tail(airplay_module, monkeypatch, tmp_path):
    """Lines starting with ERROR or Error: are buffered for later crash
    diagnosis. The tail is bounded at 20 entries so an error storm can't
    blow memory."""
    _stub_binaries(monkeypatch, tmp_path, present=True)
    backend = airplay_module.AirPlayBackend()
    backend._is_playing = True
    fake_proc = _FakeProc(name="cliap2")
    fake_proc.stderr = _fake_stderr([
        b"ERROR: pair-verify failed\n",
        b"Error: RTSP timeout\n",
        b"normal log line\n",
    ])
    backend._cliap2_proc = fake_proc

    await backend._stderr_reader_body(fake_proc.stderr)

    tail = list(backend._stderr_tail)
    assert any("pair-verify failed" in line for line in tail)
    assert any("RTSP timeout" in line for line in tail)
    # Non-error lines are also buffered for diagnostic completeness.
    assert any("normal log line" in line for line in tail)


@pytest.mark.asyncio
async def test_stderr_tail_bounded_at_20(airplay_module, monkeypatch, tmp_path):
    """Feed 50 lines; only the most recent 20 remain. Defends the crash-tail
    feature against a runaway log surface from cliap2."""
    _stub_binaries(monkeypatch, tmp_path, present=True)
    backend = airplay_module.AirPlayBackend()
    fake_proc = _FakeProc(name="cliap2")
    fake_proc.stderr = _fake_stderr([f"line {i}\n".encode() for i in range(50)])
    backend._cliap2_proc = fake_proc

    await backend._stderr_reader_body(fake_proc.stderr)

    assert len(backend._stderr_tail) == 20
    # The recent ones survive, the early ones do not.
    assert any("line 49" in line for line in backend._stderr_tail)
    assert not any("line 0" in line for line in backend._stderr_tail)


@pytest.mark.asyncio
async def test_stderr_reader_exits_on_eof(airplay_module, monkeypatch, tmp_path):
    """Closed stream → reader returns without raising. The watcher in U9 is
    responsible for distinguishing clean EOF from crash."""
    _stub_binaries(monkeypatch, tmp_path, present=True)
    backend = airplay_module.AirPlayBackend()
    fake_proc = _FakeProc(name="cliap2")
    fake_proc.stderr = _fake_stderr([])  # EOF immediately
    backend._cliap2_proc = fake_proc

    # Should complete promptly without exception.
    await backend._stderr_reader_body(fake_proc.stderr)


# ───────────────────────────────────────────────────────────────────────────
# U8: Discovery via D-Bus mDNS + set_device
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_discover_devices_returns_output_devices(
    airplay_module, monkeypatch, tmp_path
):
    """mdns_zeroconf.discover returns (name, host, port, uuid, txt) tuples for
    `_raop._tcp.local` services. discover_devices maps those onto OutputDevice
    with backend_type=airplay, id="host:port", id_format=host_port, hint=None."""
    _stub_binaries(monkeypatch, tmp_path, present=True)

    from app.output import mdns_zeroconf

    async def fake_discover(service_type, aiozc=None):
        assert service_type == "_raop._tcp.local"
        return [
            ("112233445566@JBL Charge 5 Wi-Fi SE", "192.168.1.20", 7000, None,
             {"am": "JBLChargeAlfa", "cn": "0,1,2,3"}),
            ("WiiM Pro", "192.168.1.21", 7000, None, {"am": "WiiM"}),
        ]

    monkeypatch.setattr(mdns_zeroconf, "discover", fake_discover)

    backend = airplay_module.AirPlayBackend()
    devices = await backend.discover_devices()

    assert len(devices) == 2
    ids = {d.id for d in devices}
    assert "192.168.1.20:7000" in ids
    assert "192.168.1.21:7000" in ids
    for d in devices:
        assert d.backend_type == "airplay"
        assert d.id_format == "host_port"
        # U10 removes the obsolete "Likely silent on AirPlay" hint.
        assert d.hint is None


@pytest.mark.asyncio
async def test_discover_devices_populates_device_addr_cache(
    airplay_module, monkeypatch, tmp_path
):
    """The 4-tuple cache (name, host, port, txt) is what set_device + play()
    consume. TXT must survive discovery so cliap2 --txt has the avahi
    properties during pair-verify."""
    _stub_binaries(monkeypatch, tmp_path, present=True)
    from app.output import mdns_zeroconf

    txt = {"am": "JBLChargeAlfa", "cn": "0,1,2,3", "et": "0,3,5"}

    async def fake_discover(service_type, aiozc=None):
        return [("JBL", "192.168.1.20", 7000, None, txt)]

    monkeypatch.setattr(mdns_zeroconf, "discover", fake_discover)

    backend = airplay_module.AirPlayBackend()
    await backend.discover_devices()

    assert backend._device_addr["192.168.1.20:7000"] == ("JBL", "192.168.1.20", 7000, txt)


@pytest.mark.asyncio
async def test_discover_devices_merges_cache_never_evicts_missed_devices(
    airplay_module, monkeypatch, tmp_path
):
    """KTD9 merge (2026-06-11 live-discovery U5): the one-shot UPDATES
    _device_addr instead of wholesale-replacing it. Devices the scan
    window missed — the watcher's registry retains them online (live
    subscription) or as offline ghosts — must keep their addresses, or a
    Scan would leave them registry-visible yet unplayable. Re-seen
    devices still overwrite their entries in place."""
    _stub_binaries(monkeypatch, tmp_path, present=True)
    from app.output import mdns_zeroconf

    backend = airplay_module.AirPlayBackend()
    # Pre-existing cache: one device this scan will re-see (stale port
    # tuple to prove overwrite), one it will miss entirely.
    backend._device_addr["192.168.1.20:7000"] = ("OLD@JBL", "192.168.1.20", 7000, {})
    backend._device_addr["10.0.0.9:7000"] = ("AA@Retained", "10.0.0.9", 7000, {"am": "X"})

    txt = {"am": "JBLChargeAlfa"}

    async def fake_discover(service_type, aiozc=None):
        return [("112233445566@JBL", "192.168.1.20", 7000, None, txt)]

    monkeypatch.setattr(mdns_zeroconf, "discover", fake_discover)
    devices = await backend.discover_devices()

    # Scan result lists only what the one-shot saw (unchanged behavior)…
    assert [d.id for d in devices] == ["192.168.1.20:7000"]
    # …but the cache merged: the missed device keeps its address, and the
    # re-seen one was overwritten with the fresh tuple.
    assert backend._device_addr["10.0.0.9:7000"] == (
        "AA@Retained", "10.0.0.9", 7000, {"am": "X"})
    assert backend._device_addr["192.168.1.20:7000"] == (
        "112233445566@JBL", "192.168.1.20", 7000, txt)


@pytest.mark.asyncio
async def test_discover_handles_both_sources_unavailable(airplay_module, monkeypatch, tmp_path):
    """Both in-process (mdns_zeroconf) and the avahi/D-Bus fallback return None
    → discover_devices surfaces [] rather than propagating None into UI code."""
    _stub_binaries(monkeypatch, tmp_path, present=True)
    from app.output import mdns_dbus, mdns_zeroconf

    async def zc_none(service_type, aiozc=None):
        return None

    async def dbus_none(service_type):
        return None

    monkeypatch.setattr(mdns_zeroconf, "discover", zc_none)
    monkeypatch.setattr(mdns_dbus, "discover", dbus_none)
    backend = airplay_module.AirPlayBackend()
    assert await backend.discover_devices() == []


@pytest.mark.asyncio
async def test_discover_falls_back_to_dbus(airplay_module, monkeypatch, tmp_path):
    """2026-06-16 fix: when in-process mDNS can't browse (5353 unavailable,
    mdns_zeroconf.discover → None), discover_devices falls back to avahi over
    D-Bus and maps its results onto OutputDevices + the address cache."""
    _stub_binaries(monkeypatch, tmp_path, present=True)
    from app.output import mdns_dbus, mdns_zeroconf

    txt = {"am": "WiiM", "ft": "0x445F8A00,0x1C340"}

    async def zc_none(service_type, aiozc=None):
        return None

    async def fake_dbus(service_type):
        assert service_type == "_raop._tcp.local"
        return [("A1B2C3D4E5F6@WiiM Pro-E5F6", "192.168.1.50", 7000, None, txt)]

    monkeypatch.setattr(mdns_zeroconf, "discover", zc_none)
    monkeypatch.setattr(mdns_dbus, "discover", fake_dbus)

    backend = airplay_module.AirPlayBackend()
    devices = await backend.discover_devices()
    assert [d.id for d in devices] == ["192.168.1.50:7000"]
    assert devices[0].name == "WiiM Pro-E5F6"  # MAC prefix stripped for display
    # Raw avahi name retained in the cache (cliap2 _ensure_deviceid needs it).
    assert backend._device_addr["192.168.1.50:7000"][0] == "A1B2C3D4E5F6@WiiM Pro-E5F6"


# ── U1: _strip_raop_mac_prefix helper + display-name integration ─────────────


def test_strip_raop_mac_prefix_strips_canonical_form(airplay_module):
    """Avahi RAOP service names follow `<12-hex-mac>@<friendly>`; the canonical
    shape strips to just the friendly part so the picker shows clean labels."""
    strip = airplay_module._strip_raop_mac_prefix
    assert strip("A1B2C3D4E5F6@WiiM Pro-E5F6") == "WiiM Pro-E5F6"
    assert strip("112233445566@JBL Charge 5 Wi-Fi SE") == "JBL Charge 5 Wi-Fi SE"


def test_strip_raop_mac_prefix_passes_through_no_prefix(airplay_module):
    """Names without the MAC prefix are returned unchanged — chromecast-style
    plain names should not be mangled."""
    strip = airplay_module._strip_raop_mac_prefix
    assert strip("Living Room Speaker") == "Living Room Speaker"


def test_strip_raop_mac_prefix_leaves_non_hex_prefix_alone(airplay_module):
    """Only the canonical 12-hex shape strips. A prefix-shaped string with
    non-hex characters is intentional naming that we must not destroy."""
    strip = airplay_module._strip_raop_mac_prefix
    assert strip("XYZ@thing") == "XYZ@thing"
    assert strip("42FD@short") == "42FD@short"  # 4 hex chars, not 12


def test_strip_raop_mac_prefix_handles_empty(airplay_module):
    """Empty input returns empty — no IndexError, no None."""
    strip = airplay_module._strip_raop_mac_prefix
    assert strip("") == ""


def test_strip_raop_mac_prefix_falls_back_when_strip_would_be_empty(airplay_module):
    """If a name is JUST the prefix (`A1B2C3D4E5F6@`), stripping would leave
    the display name empty — the picker would show a blank entry. Fall back
    to the raw form in that case so the operator can still identify the
    device by its hex id."""
    strip = airplay_module._strip_raop_mac_prefix
    assert strip("A1B2C3D4E5F6@") == "A1B2C3D4E5F6@"


@pytest.mark.asyncio
async def test_discover_devices_strips_mac_prefix_from_display_name(
    airplay_module, monkeypatch, tmp_path
):
    """End-to-end: avahi yields a MAC-prefixed name; OutputDevice.name is
    stripped for the picker; _device_addr still holds the raw avahi name
    so cliap2's _ensure_deviceid synthesis path keeps working."""
    _stub_binaries(monkeypatch, tmp_path, present=True)

    from app.output import mdns_zeroconf

    raw_avahi_name = "A1B2C3D4E5F6@WiiM Pro-E5F6"
    txt = {"am": "WiiM", "ft": "0x445F8A00,0x1C340"}

    async def fake_discover(service_type, aiozc=None):
        return [(raw_avahi_name, "192.168.1.50", 7000, None, txt)]

    monkeypatch.setattr(mdns_zeroconf, "discover", fake_discover)

    backend = airplay_module.AirPlayBackend()
    devices = await backend.discover_devices()

    assert len(devices) == 1
    assert devices[0].name == "WiiM Pro-E5F6"
    # The raw name MUST survive in _device_addr so cliap2's deviceid
    # synthesis (extracts the 12-hex MAC from the prefix) keeps working.
    cached = backend._device_addr["192.168.1.50:7000"]
    assert cached[0] == raw_avahi_name


@pytest.mark.asyncio
async def test_discover_devices_passes_through_clean_names(
    airplay_module, monkeypatch, tmp_path
):
    """Devices that already advertise a clean name (no MAC prefix) flow
    through unchanged — the strip helper is a no-op for them."""
    _stub_binaries(monkeypatch, tmp_path, present=True)

    from app.output import mdns_zeroconf

    async def fake_discover(service_type, aiozc=None):
        return [("Living Room Speaker", "192.168.1.21", 7000, None, {})]

    monkeypatch.setattr(mdns_zeroconf, "discover", fake_discover)

    backend = airplay_module.AirPlayBackend()
    devices = await backend.discover_devices()

    assert len(devices) == 1
    assert devices[0].name == "Living Room Speaker"


# ── U2: probe_device picker-facing wrapper ───────────────────────────────────


@pytest.mark.asyncio
async def test_probe_device_returns_true_when_ap2_verdict_cached(
    airplay_module, monkeypatch, tmp_path
):
    """A cached AP2 verdict means the AirPlay path was proven working —
    picker shows AirPlay as verified without re-running cliap2."""
    _stub_binaries(monkeypatch, tmp_path, present=True)

    async def fake_get(device_id):
        return "ap2"

    monkeypatch.setattr(airplay_module, "_get_per_device_protocol", fake_get)

    backend = airplay_module.AirPlayBackend()
    assert await backend.probe_device("192.168.1.50:7000") is True


@pytest.mark.asyncio
async def test_probe_device_returns_true_when_ap1_verdict_cached(
    airplay_module, monkeypatch, tmp_path
):
    """AP1 is the cliraop fallback verdict — still 'AirPlay works on this
    device'. Picker treats AP1 and AP2 identically for verified-or-not."""
    _stub_binaries(monkeypatch, tmp_path, present=True)

    async def fake_get(device_id):
        return "ap1"

    monkeypatch.setattr(airplay_module, "_get_per_device_protocol", fake_get)

    backend = airplay_module.AirPlayBackend()
    assert await backend.probe_device("192.168.1.50:7000") is True


@pytest.mark.asyncio
async def test_probe_device_runs_underlying_probe_when_no_verdict(
    airplay_module, monkeypatch, tmp_path
):
    """No cached verdict + an entry in _device_addr → the wrapper triggers
    the existing _probe_device path and translates the verdict to bool."""
    _stub_binaries(monkeypatch, tmp_path, present=True)

    async def fake_get(device_id):
        return None

    monkeypatch.setattr(airplay_module, "_get_per_device_protocol", fake_get)

    backend = airplay_module.AirPlayBackend()
    backend._device_addr["192.168.1.50:7000"] = (
        "WiiM Pro", "192.168.1.50", 7000, {"am": "WiiM"},
    )

    async def fake_probe(self, device_id, name, host, port, txt):
        assert device_id == "192.168.1.50:7000"
        return "ap2"

    monkeypatch.setattr(airplay_module.AirPlayBackend, "_probe_device", fake_probe)

    assert await backend.probe_device("192.168.1.50:7000") is True


@pytest.mark.asyncio
async def test_probe_device_returns_false_when_unknown_device_id(
    airplay_module, monkeypatch, tmp_path
):
    """A device_id that isn't in _device_addr → False without raising.
    The picker treats unknown IDs as broken rather than crashing — keeps
    stale verdicts from a prior session from triggering a probe against
    a device we no longer have addressing info for."""
    _stub_binaries(monkeypatch, tmp_path, present=True)

    async def fake_get(device_id):
        return None

    monkeypatch.setattr(airplay_module, "_get_per_device_protocol", fake_get)

    backend = airplay_module.AirPlayBackend()
    # _device_addr is empty.
    assert await backend.probe_device("192.168.1.50:7000") is False


@pytest.mark.asyncio
async def test_probe_device_returns_false_when_binaries_missing(
    airplay_module, monkeypatch, tmp_path
):
    """DeviceNotReadyError from _probe_device (cliap2 binaries absent)
    means the picker MUST NOT show AirPlay as verified. False is the
    correct answer; the device may verify under a different protocol."""
    from app.output.base import DeviceNotReadyError
    _stub_binaries(monkeypatch, tmp_path, present=False)  # binaries absent

    async def fake_get(device_id):
        return None

    monkeypatch.setattr(airplay_module, "_get_per_device_protocol", fake_get)

    backend = airplay_module.AirPlayBackend()
    backend._device_addr["192.168.1.50:7000"] = (
        "WiiM Pro", "192.168.1.50", 7000, {},
    )

    async def fake_probe(self, *args, **kwargs):
        raise DeviceNotReadyError("AirPlay binaries unavailable")

    monkeypatch.setattr(airplay_module.AirPlayBackend, "_probe_device", fake_probe)

    assert await backend.probe_device("192.168.1.50:7000") is False


@pytest.mark.asyncio
async def test_probe_device_swallows_unexpected_exception(
    airplay_module, monkeypatch, tmp_path
):
    """An unexpected error from _probe_device (subprocess crash, network
    glitch) returns False rather than propagating. The probe is a
    boundary; the picker downstream is allowed to assume probe_device
    never raises."""
    _stub_binaries(monkeypatch, tmp_path, present=True)

    async def fake_get(device_id):
        return None

    monkeypatch.setattr(airplay_module, "_get_per_device_protocol", fake_get)

    backend = airplay_module.AirPlayBackend()
    backend._device_addr["192.168.1.50:7000"] = (
        "WiiM Pro", "192.168.1.50", 7000, {},
    )

    async def fake_probe(self, *args, **kwargs):
        raise RuntimeError("subprocess crashed")

    monkeypatch.setattr(airplay_module.AirPlayBackend, "_probe_device", fake_probe)

    assert await backend.probe_device("192.168.1.50:7000") is False


@pytest.mark.asyncio
async def test_set_device_persists_resolved_address(
    airplay_module, monkeypatch, tmp_path
):
    """set_device writes output_addr:<id> JSON for startup-reconnect to
    consume, and loads any persisted vol:airplay:<id> into the backend's
    cached volume."""
    _stub_binaries(monkeypatch, tmp_path, present=True)

    persisted: dict[str, str] = {}
    settings: dict[str, str] = {"vol:airplay:192.168.1.20:7000": "0.42"}

    from app import database as db

    async def _get(key):
        return settings.get(key)

    async def _set(key, value):
        persisted[key] = value

    monkeypatch.setattr(db, "get_setting", _get)
    monkeypatch.setattr(db, "set_setting", _set)

    backend = airplay_module.AirPlayBackend()
    backend._device_addr["192.168.1.20:7000"] = ("JBL", "192.168.1.20", 7000, {"am": "JBL"})

    await backend.set_device("192.168.1.20:7000")

    assert backend._device_id == "192.168.1.20:7000"
    assert backend._volume == 0.42

    import json
    addr_raw = persisted["output_addr:192.168.1.20:7000"]
    addr = json.loads(addr_raw)
    assert addr["name"] == "JBL"
    assert addr["host"] == "192.168.1.20"
    assert addr["port"] == 7000


@pytest.mark.asyncio
async def test_set_device_unknown_device_raises(airplay_module, monkeypatch, tmp_path):
    """Selecting a device not in the discovery cache means we either lost
    state or the device is offline. Surface as RuntimeError so the UI shows
    a real failure rather than silently picking up a stale state."""
    _stub_binaries(monkeypatch, tmp_path, present=True)
    backend = airplay_module.AirPlayBackend()
    with pytest.raises(RuntimeError, match="not found"):
        await backend.set_device("nope:9999")


@pytest.mark.asyncio
async def test_set_device_uses_cached_volume_default_when_unset(
    airplay_module, monkeypatch, tmp_path
):
    """No persisted volume → fall back to 0.5 (the per-device default)."""
    _stub_binaries(monkeypatch, tmp_path, present=True)
    from app import database as db

    async def _get(key):
        return None

    async def _set(*a, **kw):
        pass

    monkeypatch.setattr(db, "get_setting", _get)
    monkeypatch.setattr(db, "set_setting", _set)

    backend = airplay_module.AirPlayBackend()
    backend._device_addr["192.168.1.20:7000"] = ("JBL", "192.168.1.20", 7000, {})

    await backend.set_device("192.168.1.20:7000")
    assert backend._volume == 0.5


# ───────────────────────────────────────────────────────────────────────────
# U9: Process crash recovery
# ───────────────────────────────────────────────────────────────────────────


class _ExitingProc:
    """_FakeProc variant whose wait() returns a configurable returncode after
    a sentinel event. Used to drive the watcher coroutine to its decision
    point without actually starting a subprocess."""

    def __init__(self, returncode: int) -> None:
        self.returncode = returncode
        self.terminated = False
        self.killed = False
        self.stderr = None
        self.stdin = None
        self._wait_event = asyncio.Event()

    def signal_exit(self) -> None:
        self._wait_event.set()

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        await self._wait_event.wait()
        return self.returncode


@pytest.mark.asyncio
async def test_watcher_nonzero_exit_reports_outage_not_advance(
    airplay_module, monkeypatch, tmp_path, fresh_supervisor
):
    """cliap2 exits non-zero before stderr emitted EOS → the crash path is
    re-pointed to outage-suspected (supervisor plan U2, R16) instead of the
    old broadcast+advance. The classifier probes reachability and either
    holds (speaker dead — an outage must not consume queue items) or skips
    (speaker up — today's behavior, with the skip toast)."""
    _stub_binaries(monkeypatch, tmp_path, present=True)
    sup, timers, rec = fresh_supervisor
    outages = []
    sup.add_outage_listener(lambda token, track, reason: outages.append(reason))

    advance_calls: list[int] = []
    async def _advance():
        advance_calls.append(1)

    backend = airplay_module.AirPlayBackend(advance_cb=_advance)
    backend._is_playing = True
    backend._stderr_tail.extend(["last error", "another line"])

    proc = _ExitingProc(returncode=1)
    backend._cliap2_proc = proc
    backend._exit_handled = False

    broadcast_calls: list = []
    async def _broadcast(*args, **kwargs):
        broadcast_calls.append((args, kwargs))

    from app.events import bus as bus_module
    monkeypatch.setattr(bus_module.manager, "broadcast_to_admins", _broadcast)

    # Run watcher; signal exit so wait() returns.
    proc.signal_exit()
    await backend._process_watcher_body(proc)

    assert advance_calls == []                 # never advances directly (R16)
    assert broadcast_calls == []               # classifier owns visibility now
    assert outages == ["process_crash"]


@pytest.mark.asyncio
async def test_watcher_clean_exit_still_advances(
    airplay_module, monkeypatch, tmp_path, fresh_supervisor
):
    """returncode=0 (cliraop processed the whole stream) stays the natural
    end-of-stream advance — U2's re-point touches only the crash path."""
    _stub_binaries(monkeypatch, tmp_path, present=True)
    sup, timers, rec = fresh_supervisor
    outages = []
    sup.add_outage_listener(lambda *a: outages.append(a))

    advance_calls: list[int] = []
    async def _advance():
        advance_calls.append(1)

    backend = airplay_module.AirPlayBackend(advance_cb=_advance)
    backend._is_playing = True
    backend._exit_handled = False

    proc = _ExitingProc(returncode=0)
    backend._cliap2_proc = proc

    proc.signal_exit()
    await backend._process_watcher_body(proc)

    assert advance_calls == [1]
    assert outages == []


@pytest.mark.asyncio
async def test_watcher_no_op_when_eos_already_handled(
    airplay_module, monkeypatch, tmp_path
):
    """cliap2 exits 0 after stderr already routed EOS → watcher must not
    double-advance or emit a crash event. _exit_handled is the dedup flag."""
    _stub_binaries(monkeypatch, tmp_path, present=True)

    advance_calls: list[int] = []
    async def _advance():
        advance_calls.append(1)

    backend = airplay_module.AirPlayBackend(advance_cb=_advance)
    backend._is_playing = False  # EOS already set it
    backend._exit_handled = True  # U6 already routed

    proc = _ExitingProc(returncode=0)
    backend._cliap2_proc = proc

    broadcast_calls: list = []
    async def _broadcast(*args, **kwargs):
        broadcast_calls.append((args, kwargs))

    from app.events import bus as bus_module
    monkeypatch.setattr(bus_module.manager, "broadcast_to_admins", _broadcast)

    proc.signal_exit()
    await backend._process_watcher_body(proc)

    assert advance_calls == []
    assert broadcast_calls == []


@pytest.mark.asyncio
async def test_watcher_silent_when_stop_requested(airplay_module, monkeypatch, tmp_path):
    """User pressed stop → cliap2's exit is expected. No advance, no crash
    event regardless of returncode."""
    _stub_binaries(monkeypatch, tmp_path, present=True)

    advance_calls: list[int] = []
    async def _advance():
        advance_calls.append(1)

    backend = airplay_module.AirPlayBackend(advance_cb=_advance)
    backend._is_playing = True
    backend._stop_requested = True
    backend._exit_handled = False

    proc = _ExitingProc(returncode=1)
    backend._cliap2_proc = proc

    broadcast_calls: list = []
    async def _broadcast(*args, **kwargs):
        broadcast_calls.append((args, kwargs))

    from app.events import bus as bus_module
    monkeypatch.setattr(bus_module.manager, "broadcast_to_admins", _broadcast)

    proc.signal_exit()
    await backend._process_watcher_body(proc)

    assert advance_calls == []
    assert broadcast_calls == []


def test_build_ffmpeg_args_adds_ss_when_offset_positive(airplay_module):
    """seek() relies on `-ss <seconds>` to restart ffmpeg at the new offset.
    Without `-ss`, the respawned pipeline plays from track start regardless
    of the seek target — the bug that made the progress bar look like it
    refused to scrub."""
    args = airplay_module._build_ffmpeg_args(
        "http://plex.local/stream?key=abc", start_offset_ms=42_500
    )
    assert "-ss" in args
    ss_value = args[args.index("-ss") + 1]
    assert float(ss_value) == pytest.approx(42.5, abs=0.001)
    # -ss must appear AFTER -i for output-side seek. Input-side seek
    # over an HTTP source triggers a storm of Range-byte probe requests
    # as ffmpeg hunts for the FLAC frame boundary — observed 20+
    # requests over several seconds during a single seek, which is the
    # user-perceived "seek is slow" symptom. Output-side seek is a
    # single sequential GET plus decode-and-discard.
    assert args.index("-ss") > args.index("-i")


def test_build_ffmpeg_args_omits_ss_when_offset_zero(airplay_module):
    """Default path (no seek) must not pass -ss at all; an extra -ss 0 flag
    would force ffmpeg into seek-then-decode mode for every track start."""
    args = airplay_module._build_ffmpeg_args(
        "http://plex.local/stream?key=abc", start_offset_ms=0
    )
    assert "-ss" not in args


@pytest.mark.asyncio
async def test_seek_with_active_session_respawns_ffmpeg_with_ss_flag(
    airplay_module, monkeypatch, tmp_path
):
    """When a session is active, seek() must tear down and respawn with
    ffmpeg's `-ss <offset>` flag — cliraop reads from stdin so there's no
    mid-stream seek path."""
    _stub_binaries(monkeypatch, tmp_path, present=True)

    spawned: list[tuple[str, ...]] = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        spawned.append(tuple(args))
        return _FakeProc(name=args[0])

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(airplay_module.os, "mkfifo", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(airplay_module.os, "pipe", lambda: (3, 4))
    monkeypatch.setattr(airplay_module.os, "close", lambda fd: None)
    monkeypatch.setattr(airplay_module.os, "unlink", lambda p: None)

    async def _fake_open_cmd_pipe(self, path):
        self._cmd_pipe_writer = _FakeWriter()

    monkeypatch.setattr(
        airplay_module.AirPlayBackend, "_open_cmd_pipe_writer", _fake_open_cmd_pipe
    )

    from app import database as db
    async def _get(k): return None
    async def _set(*a, **kw): pass
    monkeypatch.setattr(db, "get_setting", _get)
    monkeypatch.setattr(db, "set_setting", _set)

    backend = airplay_module.AirPlayBackend()
    backend._device_addr["d1"] = ("JBL", "192.168.1.20", 7000, {})
    await backend.set_device("d1")

    await backend.play("http://stream/track1", _DummyTrack())
    assert backend._current_stream_url == "http://stream/track1"

    spawned.clear()
    await backend.seek(75_000)

    # Two new spawns: cliraop/cliap2 + ffmpeg. The ffmpeg invocation
    # carries the -ss flag computed from position_ms.
    ffmpeg_argv = next(args for args in spawned if args[0] == "ffmpeg")
    assert "-ss" in ffmpeg_argv
    ss_value = ffmpeg_argv[ffmpeg_argv.index("-ss") + 1]
    assert float(ss_value) == pytest.approx(75.0, abs=0.001)


@pytest.mark.asyncio
async def test_seek_with_no_active_session_is_noop(
    airplay_module, monkeypatch, tmp_path
):
    """Without a cached stream URL (idle backend or post-teardown), seek()
    must not raise and must not attempt to spawn a respawn pipeline."""
    _stub_binaries(monkeypatch, tmp_path, present=True)

    spawned: list = []
    async def fake_create_subprocess_exec(*args, **kwargs):
        spawned.append(args)
        return _FakeProc(name="x")

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    backend = airplay_module.AirPlayBackend()
    # No play() call → _current_stream_url stays None.
    await backend.seek(30_000)
    assert spawned == []


@pytest.mark.asyncio
async def test_seek_anchors_position_to_offset(
    airplay_module, monkeypatch, tmp_path
):
    """After seek(N), get_position() must read approximately N immediately —
    the progress bar should jump to the seek point, not back to 0."""
    _stub_binaries(monkeypatch, tmp_path, present=True)

    async def fake_create_subprocess_exec(*args, **kwargs):
        return _FakeProc(name=args[0])

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(airplay_module.os, "mkfifo", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(airplay_module.os, "pipe", lambda: (3, 4))
    monkeypatch.setattr(airplay_module.os, "close", lambda fd: None)
    monkeypatch.setattr(airplay_module.os, "unlink", lambda p: None)

    async def _fake_open_cmd_pipe(self, path):
        self._cmd_pipe_writer = _FakeWriter()

    monkeypatch.setattr(
        airplay_module.AirPlayBackend, "_open_cmd_pipe_writer", _fake_open_cmd_pipe
    )

    from app import database as db
    async def _get(k): return None
    async def _set(*a, **kw): pass
    monkeypatch.setattr(db, "get_setting", _get)
    monkeypatch.setattr(db, "set_setting", _set)

    backend = airplay_module.AirPlayBackend()
    backend._device_addr["d1"] = ("JBL", "192.168.1.20", 7000, {})
    await backend.set_device("d1")

    await backend.play("http://stream/track1", _DummyTrack())
    await backend.seek(60_000)

    pos_ms = await backend.get_position()
    # cliraop has NTP delay 0; after seek to 60s the position should
    # be ~60_000 ms with at most a few ms of test-execution drift.
    assert 59_900 <= pos_ms <= 60_500


@pytest.mark.asyncio
async def test_watcher_clears_own_task_ref_before_teardown(
    airplay_module, monkeypatch, tmp_path
):
    """The auto-advance self-cancel race: _teardown() iterates the trio of
    (stderr_reader, ffmpeg_stderr_reader, process_watcher) tasks and cancels
    each one. When _process_watcher_body schedules its own teardown and then
    awaits advance_cb, that cancel would land on the currently-running
    watcher mid-advance — killing queue_engine.advance() after it had
    already popped the next track and persisted save_queue([]). Clearing
    _process_watcher_task BEFORE scheduling teardown removes the watcher
    from the cancel-list."""
    _stub_binaries(monkeypatch, tmp_path, present=True)

    # advance_cb yields control once — long enough for the scheduled
    # teardown task to run and try to cancel the watcher mid-await.
    advance_completed = False
    async def _advance():
        nonlocal advance_completed
        await asyncio.sleep(0.05)
        advance_completed = True

    backend = airplay_module.AirPlayBackend(advance_cb=_advance)
    backend._is_playing = True
    backend._exit_handled = False

    # Spy on the teardown so we can observe whether process_watcher_task
    # was already cleared by the time teardown runs.
    teardown_saw_watcher_task: list[bool] = []
    real_teardown = backend._teardown

    async def _spy_teardown(*, send_stop: bool, caller: str = "unknown"):
        teardown_saw_watcher_task.append(backend._process_watcher_task is not None)
        await real_teardown(send_stop=send_stop, caller=caller)

    backend._teardown = _spy_teardown  # type: ignore[assignment]

    # Plant a fake watcher task ref so we can verify it gets cleared.
    fake_watcher = asyncio.create_task(asyncio.sleep(1))
    backend._process_watcher_task = fake_watcher

    proc = _ExitingProc(returncode=0)
    backend._cliap2_proc = proc

    proc.signal_exit()
    await backend._process_watcher_body(proc)
    # Let any scheduled teardown task drain.
    await asyncio.sleep(0.1)

    assert advance_completed, (
        "advance_cb was cancelled mid-flight by teardown's cancel sweep — "
        "queue auto-advance is broken"
    )
    assert teardown_saw_watcher_task == [False], (
        "teardown ran while _process_watcher_task still pointed at the "
        "running watcher; that's the self-cancel race"
    )
    fake_watcher.cancel()
    try:
        await fake_watcher
    except asyncio.CancelledError:
        pass


# ── confirmed-start proxy (2026-07-11 supervisor plan U1) ─────────────────────
# AirPlay has no data-plane "audio is rendering" signal (the pyatv-era
# control-plane-success/data-plane-silent ceiling), so the backend reports a
# PROXY: sender subprocess alive past the NTP startup delay + grace, with the
# position anchor progressing. asyncio.sleep is patched — no real waits.

async def test_confirm_proxy_fires_when_sender_alive_past_startup(airplay_module, fresh_supervisor):
    from unittest.mock import AsyncMock, MagicMock, patch
    sup, timers, rec = fresh_supervisor
    backend = airplay_module.AirPlayBackend()
    token = sup.on_dispatched(_DummyTrack())
    proc = MagicMock()
    proc.returncode = None                     # sender still running
    backend._cliap2_proc = proc
    backend._is_playing = True
    backend._stop_requested = False
    backend._playback_started_at = 123.0       # anchor set → position progressing
    ntp_delay_s = 0
    sleep_mock = AsyncMock(return_value=None)
    with patch("app.output.airplay.asyncio.sleep", sleep_mock):
        await backend._confirm_start_body(proc, ntp_delay_s, token)
    rec.assert_called_once()
    # Startup-delay guard: the proxy waits exactly NTP delay + grace before
    # judging liveness — never confirms early.
    sleep_mock.assert_awaited_once_with(
        ntp_delay_s + airplay_module._AIRPLAY_CONFIRM_GRACE_S)


async def test_confirm_proxy_declines_when_sender_died_during_startup(airplay_module, fresh_supervisor):
    """A pre-startup crash never confirms — the process watcher owns that
    exit and the supervisor's deadline classifies the dispatch."""
    from unittest.mock import AsyncMock, MagicMock, patch
    sup, timers, rec = fresh_supervisor
    backend = airplay_module.AirPlayBackend()
    token = sup.on_dispatched(_DummyTrack())
    proc = MagicMock()
    proc.returncode = 1                        # crashed
    backend._cliap2_proc = proc
    backend._is_playing = True
    backend._stop_requested = False
    backend._playback_started_at = 123.0
    with patch("app.output.airplay.asyncio.sleep", AsyncMock(return_value=None)):
        await backend._confirm_start_body(proc, 0, token)
    rec.assert_not_called()


async def test_confirm_proxy_declines_after_stop_or_supersede(airplay_module, fresh_supervisor):
    from unittest.mock import AsyncMock, MagicMock, patch
    sup, timers, rec = fresh_supervisor
    backend = airplay_module.AirPlayBackend()
    token = sup.on_dispatched(_DummyTrack())
    proc = MagicMock()
    proc.returncode = None
    backend._is_playing = True
    backend._stop_requested = False
    backend._playback_started_at = 123.0
    backend._cliap2_proc = MagicMock()         # a NEWER session's process
    with patch("app.output.airplay.asyncio.sleep", AsyncMock(return_value=None)):
        await backend._confirm_start_body(proc, 0, token)   # stale proc
    rec.assert_not_called()

    backend._cliap2_proc = proc
    backend._stop_requested = True             # user stop before audio
    with patch("app.output.airplay.asyncio.sleep", AsyncMock(return_value=None)):
        await backend._confirm_start_body(proc, 0, token)
    rec.assert_not_called()


async def test_teardown_cancels_confirm_task(airplay_module):
    """_teardown sweeps the confirm-proxy task with the reader/watcher tasks
    so a stale proxy can't confirm after a new session starts."""
    backend = airplay_module.AirPlayBackend()
    task = asyncio.create_task(asyncio.sleep(60))
    backend._confirm_task = task
    await backend._teardown(send_stop=False, caller="test")
    assert task.done()
    assert backend._confirm_task is None


# ── reachability probe (2026-07-11 supervisor plan U2) ────────────────────────
# The plan's KTD defines probe semantics for Cast/DLNA/Direct; AirPlay's
# equivalent is a TCP connect to the cached receiver address (cliap2 exposes
# no transport state). Reachable → the classifier keeps today's skip; socket
# dead → device-level hold.

@pytest.mark.asyncio
async def test_probe_liveness_tcp_connect_success_is_reachable(
    airplay_module, monkeypatch, tmp_path
):
    from unittest.mock import AsyncMock, MagicMock
    _stub_binaries(monkeypatch, tmp_path, present=True)
    backend = airplay_module.AirPlayBackend()
    backend._device_id = "dev1"
    backend._device_addr["dev1"] = ("Speaker", "192.168.1.40", 7000, {})

    writer = MagicMock()
    writer.close = MagicMock()
    writer.wait_closed = AsyncMock()
    opened = []

    async def _open_connection(host, port):
        opened.append((host, port))
        return (MagicMock(), writer)

    monkeypatch.setattr(airplay_module.asyncio, "open_connection", _open_connection)
    assert await backend.probe_liveness() == (True, None)
    assert opened == [("192.168.1.40", 7000)]
    writer.close.assert_called_once()


@pytest.mark.asyncio
async def test_probe_liveness_refused_connection_is_unreachable(
    airplay_module, monkeypatch, tmp_path
):
    _stub_binaries(monkeypatch, tmp_path, present=True)
    backend = airplay_module.AirPlayBackend()
    backend._device_id = "dev1"
    backend._device_addr["dev1"] = ("Speaker", "192.168.1.40", 7000, {})

    async def _open_connection(host, port):
        raise ConnectionRefusedError("down")

    monkeypatch.setattr(airplay_module.asyncio, "open_connection", _open_connection)
    assert await backend.probe_liveness() == (False, None)


@pytest.mark.asyncio
async def test_probe_liveness_no_cached_address_is_unreachable(
    airplay_module, monkeypatch, tmp_path
):
    _stub_binaries(monkeypatch, tmp_path, present=True)
    backend = airplay_module.AirPlayBackend()
    backend._device_id = None
    assert await backend.probe_liveness() == (False, None)
