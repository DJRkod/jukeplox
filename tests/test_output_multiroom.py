"""U1 — server-fed multi-room base: redistribution helper, feed-arg builder,
feed lifecycle, and the flow-mode exit classifier.

The redistribution helper is pure and edge-case-heavy (the test-first,
high-value piece per the plan). Feed-process tests use a fake subprocess so no
ffmpeg is required in the dev env.
"""

import asyncio
import pytest

from app.output import multiroom
from app.output.multiroom import (
    FEED_SAMPLE_RATE,
    FeedStalled,
    MultiroomBackendBase,
    PcmFeed,
    build_pcm_feed_args,
    classify_feed_exit,
    group_volume,
    redistribute_group_volume,
)


# ── redistribution helper (pure) ─────────────────────────────────────────────


def _avg(xs):
    return sum(xs) / len(xs)


def test_group_volume_is_mean_and_empty_is_zero():
    assert group_volume([0.2, 0.6, 1.0]) == pytest.approx(0.6)
    assert group_volume([]) == 0.0


def test_redistribute_preserves_relative_balance_on_master_shift():
    """3 clients at distinct volumes, master shift → the group mean lands on
    target AND the movable members keep their ratios (1:3 stays 1:3)."""
    current = [0.2, 0.6, 0.4]
    result = redistribute_group_volume(current, 0.6)  # raise mean 0.4 → 0.6
    assert _avg(result) == pytest.approx(0.6, abs=1e-6)
    # ratios preserved among the (unclamped) members
    assert result[1] / result[0] == pytest.approx(0.6 / 0.2, rel=1e-6)
    assert result[2] / result[0] == pytest.approx(0.4 / 0.2, rel=1e-6)


def test_redistribute_scale_down_preserves_ratios():
    current = [0.4, 0.8]
    result = redistribute_group_volume(current, 0.3)  # mean 0.6 → 0.3
    assert _avg(result) == pytest.approx(0.3, abs=1e-6)
    assert result[1] / result[0] == pytest.approx(2.0, rel=1e-6)


def test_redistribute_spills_clamped_delta_and_terminates():
    """When one member hits the ceiling its excess spills onto the rest, and the
    group mean still reaches target if the movable headroom allows it."""
    current = [0.5, 0.95]
    result = redistribute_group_volume(current, 0.8)  # needs mean 0.8 = total 1.6
    assert all(0.0 <= v <= 1.0 for v in result)
    assert _avg(result) == pytest.approx(0.8, abs=1e-6)
    assert result[1] == pytest.approx(1.0, abs=1e-6)  # clamped at ceiling


def test_redistribute_all_pinned_cannot_exceed_bound_but_terminates():
    """Every member already at the ceiling: target above 1.0 is impossible, the
    loop terminates (no infinite spill) and leaves them pinned."""
    result = redistribute_group_volume([1.0, 1.0, 1.0], 1.0)
    assert result == [1.0, 1.0, 1.0]
    # Asking for more than achievable clamps target into range; stays pinned.
    result2 = redistribute_group_volume([1.0, 1.0], 5.0)
    assert result2 == [1.0, 1.0]


def test_redistribute_raise_from_all_zero_uses_equal_share():
    """No ratio to preserve (all zero) → equal additive share to reach target."""
    result = redistribute_group_volume([0.0, 0.0, 0.0], 0.5)
    assert result == pytest.approx([0.5, 0.5, 0.5])


def test_redistribute_single_and_empty_are_noops():
    assert redistribute_group_volume([0.3], 0.7) == pytest.approx([0.7])
    assert redistribute_group_volume([], 0.5) == []


def test_redistribute_does_not_mutate_input():
    current = [0.2, 0.6]
    _ = redistribute_group_volume(current, 0.9)
    assert current == [0.2, 0.6]


def test_redistribute_percent_scale():
    """Works on a 0-100 percent scale too (Snapcast/Sendspin clients)."""
    result = redistribute_group_volume([20.0, 60.0], 60.0, lo=0.0, hi=100.0)
    assert _avg(result) == pytest.approx(60.0, abs=1e-4)
    assert result[1] / result[0] == pytest.approx(3.0, rel=1e-6)


# ── feed-arg builder: credential hygiene + shape ─────────────────────────────


def test_feed_args_local_source_is_direct_argv():
    args = build_pcm_feed_args("/music/song.flac", None, sink="pipe:1")
    assert "/music/song.flac" in args
    assert "-i" in args and args[args.index("-i") + 1] == "/music/song.flac"
    assert str(FEED_SAMPLE_RATE) in args
    assert args[-1] == "pipe:1"


def test_feed_args_http_source_never_on_argv():
    """A Plex/Jellyfin http source with a token query + auth header must NOT
    appear on the argv — it is fed on stdin instead (creds ride the request)."""
    url = "http://plex.local/parts/1/file.flac?X-Plex-Token=SECRETTOKEN123456"
    headers = {"Authorization": "Bearer SECRETBEARER"}
    args = build_pcm_feed_args(url, headers, sink="pipe:1")
    joined = " ".join(args)
    assert "SECRETTOKEN123456" not in joined
    assert "SECRETBEARER" not in joined
    assert url not in joined
    assert args[args.index("-i") + 1] == "pipe:0"  # stdin is the input


def test_feed_args_tcp_sink_and_realtime():
    sink = "tcp://127.0.0.1:4953?listen=false"
    args = build_pcm_feed_args("/m/s.flac", None, sink=sink, realtime=True)
    assert args[-1] == sink
    assert "-re" in args
    # -re must precede the input
    assert args.index("-re") < args.index("-i")


def test_feed_args_output_side_seek():
    args = build_pcm_feed_args("/m/s.flac", None, sink="pipe:1", start_offset_ms=5000)
    assert "-ss" in args
    assert args.index("-ss") > args.index("-i")  # output-side (after -i)


# ── flow-mode exit classifier ────────────────────────────────────────────────


def test_classify_feed_exit_clean_eof_advances():
    assert classify_feed_exit(0, expected_end=True) == "advance"
    assert classify_feed_exit(None, expected_end=True) == "advance"


def test_classify_feed_exit_midtrack_death_holds():
    # Non-zero exit is always an outage.
    assert classify_feed_exit(1, expected_end=True) == "outage"
    # A zero exit that was NOT expected (mid-track) still holds — advance
    # authority is the boundary, never "any ffmpeg exit".
    assert classify_feed_exit(0, expected_end=False) == "outage"
    assert classify_feed_exit(255, expected_end=False) == "outage"


# ── PcmFeed lifecycle (fake subprocess — no ffmpeg needed) ────────────────────


class _FakeStream:
    """Minimal StreamReader stand-in."""

    def __init__(self, chunks=None, hang=False):
        self._chunks = list(chunks or [])
        self._hang = hang

    async def read(self, n):
        if self._hang:
            await asyncio.Event().wait()  # never returns
        if self._chunks:
            return self._chunks.pop(0)
        return b""

    async def readline(self):
        return b""  # stderr EOF immediately


class _FakeProc:
    def __init__(self, *, stdout=None, hang_stdout=False, exit_rc=0):
        self.stdout = stdout if stdout is not None else _FakeStream(hang=hang_stdout)
        self.stderr = _FakeStream()
        self.stdin = None
        self.returncode = None
        self._rc = exit_rc
        self.terminated = False
        self.killed = False

    async def wait(self):
        self.returncode = self._rc
        return self._rc

    def terminate(self):
        self.terminated = True
        self.returncode = self._rc

    def kill(self):
        self.killed = True
        self.returncode = self._rc


@pytest.fixture
def fake_spawn(monkeypatch):
    created = []

    def _install(proc):
        async def _fake_exec(*args, **kwargs):
            created.append((args, kwargs, proc))
            return proc
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        return created

    return _install


async def test_pcmfeed_stall_watchdog_fires_on_no_first_byte(fake_spawn):
    proc = _FakeProc(hang_stdout=True)
    fake_spawn(proc)
    feed = PcmFeed("/m/s.flac", None, sink="pipe:1", consume_stdout=True,
                   stall_s=0.05, label="test")
    with pytest.raises(FeedStalled):
        await feed.read(4096)
    await feed.close()
    assert proc.terminated  # reaped on close


async def test_pcmfeed_reads_bytes_then_clean_eof(fake_spawn):
    proc = _FakeProc(stdout=_FakeStream(chunks=[b"PCMDATA", b""]))
    fake_spawn(proc)
    feed = PcmFeed("/m/s.flac", None, sink="pipe:1", consume_stdout=True,
                   stall_s=1.0, label="test")
    assert await feed.read(4096) == b"PCMDATA"
    assert await feed.read(4096) == b""  # clean EOF
    await feed.close()


async def test_pcmfeed_nonzero_exit_is_stall_error(fake_spawn):
    proc = _FakeProc(stdout=_FakeStream(chunks=[b""]), exit_rc=1)
    fake_spawn(proc)
    feed = PcmFeed("/m/s.flac", None, sink="pipe:1", consume_stdout=True,
                   stall_s=1.0, label="test")
    with pytest.raises(FeedStalled):
        await feed.read(4096)
    await feed.close()


async def test_pcmfeed_tcp_sink_waits_on_exit(fake_spawn):
    """The Snapcast tcp-sink path: no stdout consumption, only wait()."""
    proc = _FakeProc(exit_rc=0)
    fake_spawn(proc)
    feed = PcmFeed("/m/s.flac", None, sink="tcp://127.0.0.1:4953",
                   consume_stdout=False, label="snap")
    assert await feed.wait() == 0
    # read() must refuse on a non-consuming feed
    with pytest.raises(RuntimeError):
        await feed.read(1)
    await feed.close()


async def test_pcmfeed_close_is_idempotent(fake_spawn):
    proc = _FakeProc(exit_rc=0)
    fake_spawn(proc)
    feed = PcmFeed("/m/s.flac", None, sink="pipe:1", consume_stdout=True)
    await feed.start()
    await feed.close()
    await feed.close()  # no raise


# ── base mixin: volume echo-guard + redistribution wiring ─────────────────────


async def test_base_volume_echo_guard_stamps_and_suppresses():
    b = MultiroomBackendBase()
    assert await b.get_volume() == 0.5
    assert not b._echo_guard_active()  # never written
    b._stamp_volume_write()
    assert b._echo_guard_active()  # within window right after a write


def test_base_redistribute_delegates_to_pure_helper():
    b = MultiroomBackendBase()
    out = b.redistribute([0.2, 0.6], 0.6, lo=0.0, hi=1.0)
    assert _avg(out) == pytest.approx(0.6, abs=1e-6)


def test_base_classify_feed_exit():
    b = MultiroomBackendBase()
    assert b.classify_feed_exit(0, expected_end=True) == "advance"
    assert b.classify_feed_exit(0, expected_end=False) == "outage"


def test_base_is_playing_default_false():
    assert MultiroomBackendBase().is_playing is False
