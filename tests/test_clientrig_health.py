"""Unit tests for the client-experience rig's lane-honesty checks.

Every function under test returns `None` when a lane is trustworthy and a
human-readable reason when it is not, mirroring `tools/soak/verdict.py`. The
reason that shape matters here is the same reason it mattered there: a rig that
finds nothing has two possible meanings, and the report has to tell them apart.

Two checks exist specifically to close holes a reviewer found in the plan before
any of this was written:

  * `credential_health` ratchets the blind list. Without it, the cheapest way to
    make a failing lane go green again is to declare it blind to whatever it
    just stopped catching. That repair is mechanically correct, reads as honesty
    in review, and leaves the rig blind.
  * `ownership_health` refuses a lane that owns a report while being blind to
    that report's class, which would otherwise be a lane permanently unable to
    do its only job.

Each check is run through five questions in its own test: can it fire, can it
*not* fire, does it fire on a single population, can it reject a correct result,
and can it express the thing it exists to detect. The fifth is the one that
catches a guard which passes the other four while being structurally unable to
notice its own subject.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _load(name, relpath):
    """Load a rig module by path, registering it so dataclasses can resolve.

    See the note in `tests/test_clientrig_contract.py`: without the sys.modules
    entry, `dataclasses` cannot resolve the string annotations produced by
    `from __future__ import annotations` and class creation fails at import.
    """
    spec = importlib.util.spec_from_file_location(name, _ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


h = _load("clientrig_health", "tools/clientrig/health.py")
c = _load("clientrig_contract", "tools/clientrig/contract.py")


def credential(blind=("gpu-cost",), owns=("layout-stall",), floor=200.0,
               reissued_reason=None, reissued_date=None,
               recorded_defect="rail-jump-40px"):
    return h.Credential(
        lane="chromium-desktop",
        blind_classes=tuple(blind),
        owns_classes=tuple(owns),
        sensitivity_floor_ms=floor,
        recorded_defect=recorded_defect,
        observed_failing=True,
        observed_passing_clean=True,
        reissued_reason=reissued_reason,
        reissued_date=reissued_date,
    )


# ── credential_health: the blind-list ratchet ────────────────────────────────

def test_a_lane_matching_its_credential_passes():
    cred = credential(blind=("gpu-cost",))
    assert h.credential_health(cred, declared_blind=("gpu-cost",)) is None


def test_a_grown_blind_list_is_rejected():
    """Can it fire? The whole point of the ratchet."""
    cred = credential(blind=("gpu-cost",))
    why = h.credential_health(cred, declared_blind=("gpu-cost", "scroll-settling"))
    assert why and "scroll-settling" in why
    assert "credential" in why.lower()


def test_a_grown_blind_list_is_accepted_once_the_credential_is_reissued():
    cred = credential(blind=("gpu-cost", "scroll-settling"),
                      reissued_reason="compositing disabled by driver update",
                      reissued_date="2026-10-19")
    assert h.credential_health(
        cred, declared_blind=("gpu-cost", "scroll-settling")) is None


def test_a_reissue_without_a_reason_is_still_rejected():
    """Can it reject a correct-looking result? A dated re-issue with no stated
    reason is the paperwork without the thinking."""
    cred = credential(blind=("gpu-cost", "scroll-settling"),
                      reissued_reason=None, reissued_date="2026-10-19")
    why = h.credential_health(
        cred, declared_blind=("gpu-cost", "scroll-settling"))
    assert why and "reason" in why.lower()


def test_a_shrunk_blind_list_passes_because_seeing_more_is_never_the_hazard():
    """Can it NOT fire? A lane that gained a capability is not cheating."""
    cred = credential(blind=("gpu-cost", "scroll-settling"))
    assert h.credential_health(cred, declared_blind=("gpu-cost",)) is None


def test_an_uncredentialed_lane_is_rejected_before_it_can_report_anything():
    cred = h.Credential(
        lane="new-lane", blind_classes=(), owns_classes=("layout-stall",),
        sensitivity_floor_ms=None, recorded_defect=None,
        observed_failing=False, observed_passing_clean=False,
        reissued_reason=None, reissued_date=None)
    why = h.credential_health(cred, declared_blind=())
    assert why and ("never" in why.lower() or "uncredentialed" in why.lower())


def test_a_credential_without_a_recorded_defect_is_rejected():
    """The requirement asks for a known-true defect from the repo's history.
    Four authored injections prove only that a detector catches defects designed
    by whoever wrote the detector."""
    cred = credential(recorded_defect=None)
    why = h.credential_health(cred, declared_blind=("gpu-cost",))
    assert why and "recorded" in why.lower()


def test_the_check_distinguishes_two_lanes_rather_than_firing_on_all():
    """Does it fire on a single population? A check that rejects every lane is
    not a check."""
    good = credential(blind=("gpu-cost",))
    bad = credential(blind=("gpu-cost",))
    assert h.credential_health(good, declared_blind=("gpu-cost",)) is None
    assert h.credential_health(bad, declared_blind=("gpu-cost", "thermal")) is not None


def test_the_check_can_express_a_silently_widened_list_not_merely_a_count():
    """Can it express its subject? A rule keyed on the SIZE of the blind list
    would miss a swap -- one class dropped and a different one added leaves the
    count unchanged while the lane stops catching something it used to."""
    cred = credential(blind=("gpu-cost",))
    why = h.credential_health(cred, declared_blind=("thermal",))
    assert why, "a same-size swap must still be caught"
    assert "thermal" in why


# ── ownership_health: a lane cannot be blind to its own job ──────────────────

def test_a_lane_owning_a_class_it_can_see_passes():
    assert h.ownership_health(credential(blind=("gpu-cost",),
                                        owns=("layout-stall",))) is None


def test_a_lane_blind_to_a_class_it_owns_is_rejected():
    cred = credential(blind=("scroll-restoration",),
                      owns=("scroll-restoration",))
    why = h.ownership_health(cred)
    assert why and "scroll-restoration" in why


def test_ownership_check_passes_a_lane_that_owns_nothing():
    """Can it NOT fire? A support lane with no owned reports is legitimate."""
    assert h.ownership_health(credential(owns=())) is None


# ── reading_health: a sentinel is not a measurement ──────────────────────────

def test_a_real_reading_passes():
    r = c.Reading(timing_ms=180.0, quiescence_ms=320.0,
                  timing_resolution_ms=8.0, quiescence_resolution_ms=16.7)
    assert h.reading_health(r) is None


def test_a_sentinel_reading_is_reported_with_its_reason():
    r = c.Reading.sentinel(c.NO_DEVICE, "no device answered adb")
    why = h.reading_health(r)
    assert why and "adb" in why


def test_a_reading_at_exactly_zero_is_treated_as_suspect_not_as_instant():
    """Zero milliseconds from input delivery to a settled screen is not a fast
    result, it is a measurement that did not happen."""
    r = c.Reading(timing_ms=0.0, quiescence_ms=0.0,
                  timing_resolution_ms=8.0, quiescence_resolution_ms=16.7)
    why = h.reading_health(r)
    assert why and ("zero" in why.lower() or "suspect" in why.lower())


# ── warmup_health: an instrument that primes the state it measures ───────────

def test_a_lane_with_no_preceding_interaction_passes():
    assert h.warmup_health(interactions_before_measured=0) is None


def test_a_lane_that_interacted_before_measuring_is_rejected():
    """This is the exact instrument error that published 'the dropdown was never
    slow' against a 41-53 second freeze. First-interaction cost IS the
    phenomenon, so any warm-up deletes the subject."""
    why = h.warmup_health(interactions_before_measured=1)
    assert why and ("warm" in why.lower() or "first" in why.lower())


def test_warmup_check_names_how_many_interactions_it_saw():
    why = h.warmup_health(interactions_before_measured=3)
    assert "3" in why


# ── synthetic_input_health: dispatched events are a different code path ──────

def test_real_input_passes():
    assert h.synthetic_input_health(input_kind="dispatched-touch") is None
    assert h.synthetic_input_health(input_kind="os-injected-tap") is None


def test_a_handler_invoked_directly_is_rejected_however_good_the_numbers():
    why = h.synthetic_input_health(input_kind="element-click")
    assert why and ("synthetic" in why.lower() or "trusted" in why.lower())


def test_the_input_check_can_express_the_distinction_it_exists_for():
    """Can it express its subject? A check keyed on 'an input happened' would
    pass a scripted click, which produces no interaction-timing entry at all and
    skips hit testing and main-thread queueing."""
    assert h.synthetic_input_health(input_kind="element-click") is not None
    assert h.synthetic_input_health(input_kind="dispatch-event") is not None
    assert h.synthetic_input_health(input_kind="dispatched-touch") is None
