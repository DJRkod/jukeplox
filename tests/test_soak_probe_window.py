"""Unit tests for peak scheduling and the probe's latency contract."""
import importlib.util
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "tools" / "soak" / "probe_window.py"
_spec = importlib.util.spec_from_file_location("soak_probe", _SRC)
pw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pw)


# ── scheduling ────────────────────────────────────────────────────────────────

def test_two_peaks_fit_inside_a_six_hour_arm():
    s = pw.peak_schedule(arm_minutes=360, n_peaks=2, peak_minutes=4)
    assert len(s) == 2
    assert all(w["end_min"] <= 360 for w in s)
    assert s[0]["end_min"] <= s[1]["start_min"]


def test_a_peak_never_overruns_the_arm_boundary():
    """A truncated peak is not a peak — its probe would measure a system that is
    already winding down."""
    for n in (1, 2, 3, 5):
        for arm in (20, 60, 360):
            for peak in (2, 4, 10):
                for w in pw.peak_schedule(arm, n, peak):
                    assert w["end_min"] <= arm
                    assert w["start_min"] >= 0
                    assert w["end_min"] > w["start_min"]


def test_windows_never_overlap():
    s = pw.peak_schedule(arm_minutes=30, n_peaks=5, peak_minutes=4)
    for a, b in zip(s, s[1:]):
        assert a["end_min"] <= b["start_min"]


def test_an_arm_too_short_for_the_request_gets_fewer_peaks_not_overlapping_ones():
    s = pw.peak_schedule(arm_minutes=8, n_peaks=4, peak_minutes=3)
    assert len(s) <= 1
    for w in s:
        assert w["end_min"] <= 8


def test_an_arm_shorter_than_the_lead_in_gets_no_peaks():
    assert pw.peak_schedule(arm_minutes=3, n_peaks=2, peak_minutes=4) == []


def test_degenerate_requests_yield_nothing_rather_than_raising():
    assert pw.peak_schedule(0, 2, 4) == []
    assert pw.peak_schedule(360, 0, 4) == []
    assert pw.peak_schedule(360, 2, 0) == []
    assert pw.peak_schedule(-10, 2, 4) == []


def test_a_single_peak_lands_inside_the_arm_not_at_its_edges():
    s = pw.peak_schedule(arm_minutes=60, n_peaks=1, peak_minutes=4)
    assert len(s) == 1
    assert s[0]["start_min"] > 0
    assert s[0]["end_min"] < 60


# ── measurement ───────────────────────────────────────────────────────────────

def test_every_surface_is_reported_even_when_it_fails(monkeypatch):
    """A surface that timed out is the most interesting result a probe can get.
    Dropping it would silently shrink the sample and flatter the verdict."""
    def boom(req, timeout=None):
        raise TimeoutError("too slow")
    monkeypatch.setattr(pw.urllib.request, "urlopen", boom)

    rows = pw.measure("http://example.invalid")
    assert len(rows) == len(pw.DEFAULT_SURFACES)
    assert all("err" in r for r in rows)
    assert all("ms" in r for r in rows), "a failure still took time; record it"
    assert all("t" in r for r in rows)


def test_a_successful_measurement_records_timing_and_size(monkeypatch):
    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"x" * 128

    monkeypatch.setattr(pw.urllib.request, "urlopen",
                        lambda req, timeout=None: FakeResp())
    rows = pw.measure("http://example.invalid",
                      surfaces=[("now_playing", "/api/now-playing")])
    assert len(rows) == 1
    assert rows[0]["bytes"] == 128
    assert rows[0]["ms"] >= 0
    assert "err" not in rows[0]


def test_one_failing_surface_does_not_hide_the_others(monkeypatch):
    calls = {"n": 0}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"ok"

    def flaky(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("first one hangs")
        return FakeResp()

    monkeypatch.setattr(pw.urllib.request, "urlopen", flaky)
    rows = pw.measure("http://example.invalid",
                      surfaces=[("a", "/a"), ("b", "/b"), ("c", "/c")])
    assert len(rows) == 3
    assert "err" in rows[0]
    assert "err" not in rows[1] and "err" not in rows[2]


def test_default_surfaces_are_guest_read_paths():
    """The question is what a party guest experiences, not what an admin does."""
    paths = [p for _, p in pw.DEFAULT_SURFACES]
    assert all(p.startswith("/api/") for p in paths)
    assert not any("/admin" in p for p in paths)
