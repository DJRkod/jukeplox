"""Unit tests for the client-experience rig's measurement contract and result record.

The contract is where this rig's one non-negotiable lives: a responsiveness
measurement is TWO figures with two different error bars, never one. The timing
half comes from the browser's own interaction entry, which is quantised to 8ms
and floored at 16ms; the quiescence half comes from counting still frames. Add
them together and the coarser bound contaminates the finer one, so the contract
refuses to produce a single number.

Everything below is tested against input it must REJECT as well as input it must
accept. The repo's scar is specific: a probe that warmed the page before every
arm measured only the already-fast case and published "the dropdown was never
slow" against a real 41-53 second freeze. It passed a positive control and a
negative control. The only defence is an instrument that cannot express a
healthy answer it has not earned.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _load(name, relpath):
    """Load a rig module by path, registering it so dataclasses can resolve.

    `tests/test_soak_arm.py` loads by path without registering, which works only
    because those modules use plain classes. These use dataclasses under
    `from __future__ import annotations`, so every annotation is a string and
    `dataclasses` resolves them via `sys.modules[cls.__module__]` -- absent from
    there, that lookup returns None and class creation dies at import time.
    """
    spec = importlib.util.spec_from_file_location(name, _ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


c = _load("clientrig_contract", "tools/clientrig/contract.py")
rep = _load("clientrig_report", "tools/clientrig/report.py")


def cfg(**kw):
    """A lane configuration with every field the contract requires stamped."""
    base = dict(engine="chromium", engine_version="156.0.8078.12",
                device_profile="desktop-throttled-mid", viewport=(1400, 900),
                dpr=1.0, rail_mode="vanilla", build_sha="65c741c",
                lane_order=0, throttling="calibrated-mid")
    base.update(kw)
    return rep.LaneConfig(**base)


def reading(timing=180.0, quiescence=320.0, t_res=8.0, q_res=16.7):
    return c.Reading(timing_ms=timing, quiescence_ms=quiescence,
                     timing_resolution_ms=t_res, quiescence_resolution_ms=q_res)


# ── the two halves stay apart ─────────────────────────────────────────────────

def test_a_reading_keeps_timing_and_quiescence_separate():
    r = reading()
    assert r.timing_ms == 180.0
    assert r.quiescence_ms == 320.0
    assert not hasattr(r, "total_ms"), (
        "a combined figure would carry the coarser of the two resolution "
        "bounds while looking as precise as the finer one")


def test_each_half_carries_its_own_resolution_bound():
    r = reading(t_res=8.0, q_res=16.7)
    assert r.timing_resolution_ms == 8.0
    assert r.quiescence_resolution_ms == 16.7


def test_a_reading_without_a_resolution_bound_is_refused():
    """A number with no stated precision is the shape that produced a confident
    wrong answer in the October investigation."""
    with pytest.raises(ValueError, match="resolution"):
        c.Reading(timing_ms=180.0, quiescence_ms=320.0,
                  timing_resolution_ms=None, quiescence_resolution_ms=16.7)


# ── sentinels: no reading is never zero ──────────────────────────────────────

def test_every_failure_mode_has_its_own_sentinel():
    codes = {c.UNREACHED, c.NO_DEVICE, c.NO_READING, c.BLIND}
    assert len(codes) == 4, "a shared sentinel cannot say which failure happened"
    assert 0 not in codes, (
        "a silent zero reads exactly like a perfect result, which is how a "
        "blind instrument passes for a healthy one")
    assert all(v < 0 for v in codes)


def test_a_sentinel_reading_is_not_a_measurement():
    r = c.Reading.sentinel(c.NO_DEVICE, "no device answered adb")
    assert not r.is_measurement
    assert r.timing_ms == c.NO_DEVICE
    assert "adb" in r.reason


def test_a_sentinel_reading_refuses_to_be_read_as_a_duration():
    r = c.Reading.sentinel(c.NO_READING, "observer produced no entry")
    with pytest.raises(ValueError, match="sentinel"):
        r.as_duration_ms()


# ── aggregation: median of runs, with the between-run spread ─────────────────

def test_aggregate_reports_median_and_between_run_spread():
    rs = [reading(timing=t) for t in (170.0, 180.0, 230.0)]
    agg = c.aggregate(rs)
    assert agg.median_timing_ms == 180.0
    assert agg.between_run_spread_ms == 60.0
    assert agg.n == 3


def test_a_single_run_reports_its_spread_as_unavailable_not_zero():
    """A tight spread is the output format of confidence. One run has no
    between-run spread at all, and printing 0.0 claims perfect repeatability."""
    agg = c.aggregate([reading()])
    assert agg.n == 1
    assert agg.between_run_spread_ms is None
    assert agg.within_run_spread_ms is None or agg.within_run_spread_ms >= 0


def test_within_run_spread_is_labelled_a_resolution_bound_not_a_confidence():
    """The note must say what the within-run spread IS and point at the figure a
    claim actually rests on.

    This asserts intent rather than vocabulary: an earlier draft forbade the
    substring "confidence" anywhere in the note, which rejected the clearer
    wording "not a confidence interval". A guard that can reject a correct
    result is one of the five failure shapes this rig is built to avoid, so it
    should not appear in the rig's own tests.
    """
    agg = c.aggregate([reading(timing=t) for t in (170.0, 180.0, 230.0)])
    note = agg.spread_note.lower()
    assert "resolution bound" in note
    assert "between-run" in note
    assert "is a confidence interval" not in note


def test_aggregate_of_nothing_is_a_sentinel_not_a_zero():
    agg = c.aggregate([])
    assert agg.n == 0
    assert agg.median_timing_ms == c.NO_READING


# ── configuration is part of the measurement ─────────────────────────────────

def test_results_differing_only_in_rail_mode_do_not_aggregate_together():
    """Rail mode changes what the rail renders per frame, so pooling two modes
    averages away the thing being measured."""
    a = rep.Result(config=cfg(rail_mode="vanilla"), reading=reading(timing=170.0))
    b = rep.Result(config=cfg(rail_mode="magnetic"), reading=reading(timing=600.0))
    groups = rep.group_for_aggregation([a, b])
    assert len(groups) == 2, "two rail modes must not pool into one figure"


def test_results_sharing_a_full_configuration_do_aggregate():
    a = rep.Result(config=cfg(), reading=reading(timing=170.0))
    b = rep.Result(config=cfg(), reading=reading(timing=190.0))
    assert len(rep.group_for_aggregation([a, b])) == 1


def test_a_result_missing_any_configuration_field_is_refused():
    """A green that cannot be attributed to a configuration is not evidence."""
    with pytest.raises(TypeError):
        rep.LaneConfig(engine="chromium")


def test_every_result_stamps_the_build_it_measured():
    r = rep.Result(config=cfg(build_sha="65c741c"), reading=reading())
    assert r.config.build_sha == "65c741c", (
        "without the build identity an A/B/A comparison cannot prove the arms "
        "differed")


# ── rendering: absence of failure is never a pass ────────────────────────────

def test_a_clean_run_renders_as_pass_for_classes_the_lane_can_see():
    r = rep.Result(config=cfg(), reading=reading())
    out = rep.render(r, defect_class="layout-stall", blind_classes=("gpu-cost",))
    assert out.verdict == "pass"


def test_a_blind_class_renders_as_not_observed():
    r = rep.Result(config=cfg(), reading=reading())
    out = rep.render(r, defect_class="gpu-cost", blind_classes=("gpu-cost",))
    assert out.verdict == "not observed"
    assert "chromium" in out.detail.lower() or "lane" in out.detail.lower()


def test_rendering_a_blind_class_as_a_pass_raises_rather_than_downgrading():
    """The whole design turns on this: a lane that cannot see a class must be
    structurally unable to report green for it."""
    r = rep.Result(config=cfg(), reading=reading())
    with pytest.raises(ValueError, match="blind"):
        rep.render(r, defect_class="gpu-cost", blind_classes=("gpu-cost",),
                   force_verdict="pass")


def test_a_sentinel_result_renders_its_reason_not_a_timing():
    r = rep.Result(config=cfg(),
                   reading=c.Reading.sentinel(c.NO_DEVICE, "device asleep"))
    out = rep.render(r, defect_class="layout-stall", blind_classes=())
    assert out.verdict == "no reading"
    assert "asleep" in out.detail
    assert "0" != out.detail.strip()


# ── the sensitivity floor ────────────────────────────────────────────────────

def test_a_difference_below_the_lanes_floor_renders_as_not_observed():
    """A lane credentialed on a large injected defect would otherwise report a
    small real regression as a clean pass."""
    r = rep.Result(config=cfg(), reading=reading(), sensitivity_floor_ms=200.0)
    out = rep.render_comparison(r, measured_difference_ms=40.0,
                                defect_class="layout-stall", blind_classes=())
    assert out.verdict == "not observed"
    assert "floor" in out.detail.lower()


def test_a_difference_below_the_floor_is_not_reported_as_an_improvement():
    r = rep.Result(config=cfg(), reading=reading(), sensitivity_floor_ms=200.0)
    out = rep.render_comparison(r, measured_difference_ms=-40.0,
                                defect_class="layout-stall", blind_classes=())
    assert out.verdict == "not observed"
    assert "improve" not in out.detail.lower()


def test_a_difference_above_the_floor_is_a_real_observation():
    r = rep.Result(config=cfg(), reading=reading(), sensitivity_floor_ms=200.0)
    out = rep.render_comparison(r, measured_difference_ms=47_000.0,
                                defect_class="layout-stall", blind_classes=())
    assert out.verdict == "observed"


def test_a_result_with_no_recorded_floor_cannot_render_a_comparison():
    """An uncredentialed lane has no business reporting a difference."""
    r = rep.Result(config=cfg(), reading=reading(), sensitivity_floor_ms=None)
    with pytest.raises(ValueError, match="floor"):
        rep.render_comparison(r, measured_difference_ms=40.0,
                              defect_class="layout-stall", blind_classes=())
