"""Result records and rendering: a green that can be attributed, or no green.

Two jobs, both of them about refusing to overstate what was observed.

**Attribution.** Every result carries the full configuration that produced it --
engine, engine version, device profile, viewport, pixel ratio, rail mode, build
identity, lane order, throttling. The project has a measured 5,939x gap between
desktop Chrome and mobile Chrome on one defect, so "Chrome was fine" is not a
statement about anything unless it says which Chrome on what hardware. Results
only aggregate with results sharing every one of those fields.

**Honest verdicts.** `render` can return `pass`, `not observed`, or `no reading`,
and it is structurally unable to return `pass` for a class the lane declared
itself blind to -- asking it to raises. That asymmetry is the whole design: a
lane that cannot see something must not be able to say it looked.

`render_comparison` adds the sensitivity floor. A lane credentialed against a
large injected defect can miss a real regression an order of magnitude smaller,
so a measured difference below the floor the lane actually demonstrated renders
as `not observed` -- in both directions, because a below-floor difference is no
more an improvement than it is a regression.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

# Deliberately no import of `contract`: these modules are loaded by file path
# (the `tests/test_soak_*.py` precedent), so a relative import would fail with no
# parent package. `Reading` is consumed structurally -- `.is_measurement`,
# `.sentinel_name`, `.reason` -- which keeps the two loadable independently.


@dataclass(frozen=True)
class LaneConfig:
    """What produced a reading. Every field is required, deliberately.

    There are no defaults because a result with an unstamped field is a result
    that cannot be attributed, and an unattributable green is not evidence.
    """

    engine: str
    engine_version: str
    device_profile: str
    viewport: tuple[int, int]
    dpr: float
    rail_mode: str
    build_sha: str
    lane_order: int
    throttling: str

    def aggregation_key(self) -> tuple:
        """Everything that must match for two readings to pool.

        `lane_order` is excluded: it records which position in the run produced
        the reading (order is a variable worth recording) but it is not a
        condition of the measurement.
        """
        d = asdict(self)
        d.pop("lane_order")
        return tuple(sorted((k, str(v)) for k, v in d.items()))


@dataclass(frozen=True)
class Result:
    """One reading plus the configuration and credential context around it."""

    config: LaneConfig
    reading: object
    sensitivity_floor_ms: float | None = None


@dataclass(frozen=True)
class Verdict:
    verdict: str
    detail: str


def group_for_aggregation(results: list[Result]) -> dict:
    """Bucket results by full configuration, so unlike things never pool."""
    groups: dict = {}
    for r in results:
        groups.setdefault(r.config.aggregation_key(), []).append(r)
    return groups


def _lane_label(config: LaneConfig) -> str:
    return f"{config.engine} {config.engine_version} on {config.device_profile}"


def render(result: Result, defect_class: str, blind_classes, *,
           force_verdict: str | None = None) -> Verdict:
    """What this lane is entitled to say about this defect class.

    `force_verdict` exists only so the refusal is testable and so a caller
    cannot smuggle a pass past the blind check by constructing the Verdict
    itself -- it is never the right thing to pass in production.
    """
    blind = tuple(blind_classes or ())
    if defect_class in blind:
        if force_verdict == "pass":
            raise ValueError(
                f"{_lane_label(result.config)} is blind to {defect_class!r}; "
                f"rendering that as a pass is the failure this rig exists to "
                f"prevent -- report it as not observed")
        return Verdict(
            "not observed",
            f"{defect_class} is not observable by this lane "
            f"({_lane_label(result.config)}); absence of failure here is "
            f"absence of looking")
    if not result.reading.is_measurement:
        return Verdict(
            "no reading",
            f"{result.reading.sentinel_name}: {result.reading.reason}")
    if force_verdict:
        return Verdict(force_verdict, f"forced on {_lane_label(result.config)}")
    return Verdict("pass",
                   f"no failure observed by {_lane_label(result.config)}")


def render_comparison(result: Result, measured_difference_ms: float,
                      defect_class: str, blind_classes) -> Verdict:
    """A difference between two arms, gated on what this lane can resolve."""
    blind = tuple(blind_classes or ())
    if defect_class in blind:
        return Verdict(
            "not observed",
            f"{defect_class} is not observable by this lane "
            f"({_lane_label(result.config)})")
    if not result.reading.is_measurement:
        return Verdict(
            "no reading",
            f"{result.reading.sentinel_name}: {result.reading.reason}")
    floor = result.sensitivity_floor_ms
    if floor is None:
        raise ValueError(
            f"{_lane_label(result.config)} has no recorded sensitivity floor; "
            f"an uncredentialed lane cannot report a difference -- run the "
            f"canary titration first")
    if abs(measured_difference_ms) < floor:
        return Verdict(
            "not observed",
            f"{measured_difference_ms:+.1f} ms is below this lane's "
            f"sensitivity floor of {floor:.1f} ms -- the lane never "
            f"demonstrated it can resolve a difference this small, so this is "
            f"not a finding in either direction")
    return Verdict(
        "observed",
        f"{measured_difference_ms:+.1f} ms, above the lane's "
        f"{floor:.1f} ms floor")
