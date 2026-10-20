"""Lane-honesty checks: None when a lane is trustworthy, else why not.

Same shape as `tools/soak/verdict.py`, for the same reason. A rig that finds
nothing has two possible meanings -- nothing broke, or nobody was looking -- and
the report has to be able to tell them apart.

Two of these checks exist because a reviewer found holes in the plan before any
of it was built, and both holes were of the same kind: a mechanism that fails
*toward* looking healthy.

`credential_health` ratchets the blind list. A lane is allowed to declare which
defect classes it cannot observe, and the meta-test treats "declared blind" as
equivalent to "caught the canary". Without a ratchet, the cheapest repair for a
lane that has stopped catching something is to declare it blind -- mechanically
correct, reads as honesty in review, and leaves the rig blind. The blind list
therefore lives in the frozen credential, and growing it beyond that requires a
re-issue carrying a reason and a date.

`ownership_health` refuses a lane that owns a report while being blind to that
report's class, which is a lane structurally unable to do its only job.

The remaining three encode instrument errors this project has already paid for:
a sentinel read as a duration, a warm-up that deleted the first-interaction cost
it was supposed to measure, and a scripted click standing in for a real touch.
"""
from __future__ import annotations

from dataclasses import dataclass

# Input kinds that reach the engine's own input pipeline and produce a trusted
# event. Anything else invokes a handler directly: no hit testing, no queueing
# behind a busy main thread, and no interaction-timing entry at all.
_REAL_INPUT = frozenset({
    "dispatched-touch",     # CDP Input.dispatchTouchEvent and equivalents
    "dispatched-mouse",
    "os-injected-tap",      # adb shell input tap -- above the browser entirely
    "os-injected-swipe",
})


@dataclass(frozen=True)
class Credential:
    """What a lane earned, frozen at the moment it earned it.

    `blind_classes` is the authoritative list. A lane declaring more than this
    at run time is widening its own exemption, which is what the ratchet exists
    to catch.
    """

    lane: str
    blind_classes: tuple
    owns_classes: tuple
    sensitivity_floor_ms: float | None
    recorded_defect: str | None
    observed_failing: bool
    observed_passing_clean: bool
    reissued_reason: str | None = None
    reissued_date: str | None = None


def credential_health(cred: Credential, declared_blind) -> str | None:
    """None when this lane may report, else why it may not.

    Checked in order of how badly wrong things are: never credentialed at all,
    credentialed against nothing real, a re-issue with missing paperwork, then
    the ratchet itself.
    """
    if not (cred.observed_failing and cred.observed_passing_clean):
        missing = []
        if not cred.observed_failing:
            missing.append("never observed failing against a canary")
        if not cred.observed_passing_clean:
            missing.append("never observed passing against the clean build")
        return (f"{cred.lane} is uncredentialed: {'; '.join(missing)}. A check "
                f"that has only ever been observed passing is unverified, not "
                f"passing")

    if not cred.recorded_defect:
        return (f"{cred.lane} holds no recorded defect: its credential rests "
                f"only on authored injections, which demonstrate that a "
                f"detector catches defects designed by whoever wrote the "
                f"detector. Reproduce a defect from the repo's history")

    if cred.reissued_date and not cred.reissued_reason:
        return (f"{cred.lane} carries a re-issue dated {cred.reissued_date} "
                f"with no recorded reason -- the paperwork without the "
                f"thinking")
    if cred.reissued_reason and not cred.reissued_date:
        return (f"{cred.lane} carries a re-issue reason with no date, so the "
                f"ratchet cannot tell when the exemption was granted")

    declared = tuple(declared_blind or ())
    grown = [cls for cls in declared if cls not in cred.blind_classes]
    if grown:
        return (f"{cred.lane} declares blindness to {', '.join(sorted(grown))}, "
                f"which its credential does not authorise. Widening a blind "
                f"list is the cheapest way to silence a failing canary; "
                f"re-issue the credential with a reason and a date instead")
    return None


def ownership_health(cred: Credential) -> str | None:
    """None unless a lane owns a report it cannot possibly observe."""
    conflict = sorted(set(cred.owns_classes) & set(cred.blind_classes))
    if conflict:
        return (f"{cred.lane} owns {', '.join(conflict)} while declaring "
                f"blindness to it -- a lane cannot be the authority on a class "
                f"it cannot see. Reassign the report or drop the blindness")
    return None


def reading_health(reading) -> str | None:
    """None for a real measurement, else why this is not one."""
    if not reading.is_measurement:
        return f"{reading.sentinel_name}: {reading.reason}"
    if reading.timing_ms == 0.0 and reading.quiescence_ms == 0.0:
        return ("both halves read exactly zero, which is suspect rather than "
                "instant: zero milliseconds from input delivery to a settled "
                "screen is a measurement that did not happen")
    return None


def warmup_health(interactions_before_measured: int) -> str | None:
    """None when the measured interaction was genuinely the first one.

    The recorded instrument error: a desktop probe ran a double animation-frame
    settle before every arm, so it only ever measured the already-warm case and
    published "the dropdown was never slow" against a real 41-53 second freeze.
    It passed both a positive and a negative control. First-interaction cost is
    the phenomenon, so any priming deletes the subject.
    """
    if interactions_before_measured > 0:
        return (f"{interactions_before_measured} interaction(s) ran before the "
                f"measured one: the first-interaction cost is the phenomenon, "
                f"so a warm-up deletes it while passing every control")
    return None


def synthetic_input_health(input_kind: str) -> str | None:
    """None for input that reached the engine's own pipeline, else why not.

    A scripted click is not a weaker version of a real touch -- it exercises a
    different code path. Switching to a dispatched touch is what made a
    48-second frame appear at all in the October investigation, and by
    specification a scripted click produces no interaction-timing entry, so the
    measurement does not merely degrade: it does not exist.
    """
    if input_kind not in _REAL_INPUT:
        return (f"{input_kind!r} is synthetic: it invokes the handler directly, "
                f"skipping hit testing, input queueing behind a busy main "
                f"thread, and the trusted-event path that interaction timing "
                f"requires. Any figure from it is measuring a different code "
                f"path than a finger")
    return None
