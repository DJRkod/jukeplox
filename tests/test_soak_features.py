"""Unit tests for the soak's audio feature extraction.

The soak harness itself is deliberately not unit-tested — it drives a live
deployment through a real browser (see tests/test_soak_harness_hygiene.py). The
parsing in `tools/soak/rig/features.py` is the exception: it is pure text→data
with no I/O, and it is the layer that decides whether a boundary gap gets
measured or silently dropped. A detector with no known-bad fixture is not a
detector, so every case below includes input the parser must reject or handle,
not only input it should accept.
"""
import importlib.util
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "tools" / "soak" / "rig" / "features.py"
_spec = importlib.util.spec_from_file_location("soak_features", _SRC)
features = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(features)


# ── silencedetect parsing ─────────────────────────────────────────────────────

def _sd(*pairs, trailing_start=None):
    """Build ffmpeg-shaped silencedetect stderr."""
    lines = []
    for s, e in pairs:
        lines.append(f"[silencedetect @ 0x1] silence_start: {s}")
        lines.append(f"[silencedetect @ 0x1] silence_end: {e} | "
                     f"silence_duration: {round(e - s, 3)}")
    if trailing_start is not None:
        lines.append(f"[silencedetect @ 0x1] silence_start: {trailing_start}")
    return "\n".join(lines)


def test_single_silence_is_parsed_with_its_duration():
    out = features.parse_silences(_sd((12.0, 14.0)))
    assert len(out) == 1
    assert out[0]["start_s"] == 12.0
    assert out[0]["end_s"] == 14.0
    assert out[0]["dur_s"] == pytest.approx(2.0)
    assert out[0]["open_ended"] is False


def test_several_silences_keep_their_order():
    out = features.parse_silences(_sd((1.0, 1.5), (10.0, 12.5), (30.0, 30.6)))
    assert [o["start_s"] for o in out] == [1.0, 10.0, 30.0]
    assert [o["dur_s"] for o in out] == [pytest.approx(0.5), pytest.approx(2.5),
                                        pytest.approx(0.6)]


def test_no_silence_reported_yields_nothing():
    """Continuous audio must produce an empty list, not a zero-length interval."""
    assert features.parse_silences("nothing of interest here") == []


def test_silence_running_to_the_end_is_closed_at_the_segment_duration():
    """The common case at a boundary straddling a segment edge. Dropping it would
    lose exactly the silences most likely to matter."""
    out = features.parse_silences(_sd((1.0, 2.0), trailing_start=58.5),
                                  segment_duration_s=60.0)
    assert len(out) == 2
    assert out[1]["start_s"] == 58.5
    assert out[1]["end_s"] == 60.0
    assert out[1]["dur_s"] == pytest.approx(1.5)
    assert out[1]["open_ended"] is True


def test_open_ended_silence_without_a_known_duration_is_flagged_not_dropped():
    out = features.parse_silences(_sd(trailing_start=58.5))
    assert len(out) == 1
    assert out[0]["open_ended"] is True
    assert out[0]["end_s"] is None
    assert out[0]["dur_s"] is None


def test_a_wholly_silent_segment_is_one_interval_spanning_it():
    out = features.parse_silences(_sd(trailing_start=0.0), segment_duration_s=30.0)
    assert len(out) == 1
    assert out[0]["start_s"] == 0.0 and out[0]["end_s"] == 30.0


# ── level parsing ─────────────────────────────────────────────────────────────

def test_levels_are_parsed():
    text = ("[Parsed_volumedetect_0 @ 0x1] mean_volume: -21.4 dB\n"
            "[Parsed_volumedetect_0 @ 0x1] max_volume: -7.6 dB")
    assert features.parse_levels(text) == {"mean": -21.4, "max": -7.6}


def test_missing_levels_yield_an_empty_mapping_not_a_zero():
    """A zero would read as full scale — the loudest possible signal — which is
    the opposite of 'we could not measure it'."""
    assert features.parse_levels("no levels here") == {}


def test_duration_is_parsed_from_the_header():
    text = "  Duration: 00:05:00.02, start: 0.000000, bitrate: 1536 kb/s"
    assert features.parse_duration_s(text) == pytest.approx(300.02)


def test_missing_duration_is_none():
    assert features.parse_duration_s("no duration line") is None


# ── per-channel RMS ───────────────────────────────────────────────────────────

def _astats(*rms):
    lines = []
    for i, v in enumerate(rms, start=1):
        lines.append(f"[Parsed_astats_2 @ 0x1] Channel: {i}")
        lines.append(f"[Parsed_astats_2 @ 0x1] RMS level dB: {v}")
    return "\n".join(lines)


def test_per_channel_rms_is_parsed_in_order():
    assert features.parse_channel_rms(_astats(-21.5, -21.3)) == [-21.5, -21.3]


def test_a_dead_channel_is_visible_per_channel():
    """Whole-mix level cannot see this: a stereo pair with one side silent still
    reads as healthy audio overall."""
    ch = features.parse_channel_rms(_astats(-20.1, -91.0))
    assert ch[0] > -30 and ch[1] < -80


def test_channel_rms_absent_yields_empty():
    assert features.parse_channel_rms("no astats output") == []


# ── segment filename → absolute time ──────────────────────────────────────────

def test_segment_start_is_derived_from_the_filename():
    """Injecting mktime keeps this independent of the test machine's timezone."""
    seen = {}

    def fake_mktime(st):
        seen["st"] = st
        return 1_700_000_000.0

    ms = features.segment_start_ms("cap-20260923-222748.flac", mktime=fake_mktime)
    assert ms == 1_700_000_000_000
    st = seen["st"]
    assert (st.tm_year, st.tm_mon, st.tm_mday) == (2026, 9, 23)
    assert (st.tm_hour, st.tm_min, st.tm_sec) == (22, 27, 48)


def test_segment_start_handles_a_full_path():
    assert features.segment_start_ms("/root/soak/cap/cap-20260923-222748.flac",
                                     mktime=lambda st: 1.0) == 1000


def test_a_non_matching_filename_returns_none_rather_than_guessing():
    for name in ("notes.txt", "cap-2026-09-23.flac", "cap-20260923.flac", ""):
        assert features.segment_start_ms(name, mktime=lambda st: 1.0) is None
