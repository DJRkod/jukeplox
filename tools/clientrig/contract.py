"""The client-experience rig's measurement contract: what a reading IS.

One rule governs this module, and it is the reason the module exists rather than
the measurement being a bare float: **a responsiveness reading is two figures
with two different error bars, and they are never summed.**

The timing half answers "how long from a real input being delivered until the
screen next painted". It comes from the browser's own interaction-timing entry,
which is quantised to 8ms and whose observer floors at 16ms, so sub-frame
effects are invisible to it by construction.

The quiescence half answers "how long until the screen stopped changing". It
comes from counting consecutive still frames, so its bound is a frame interval.

Adding them produces a number that looks as precise as the finer bound while
actually carrying the coarser one, and the project has already paid for that
mistake in a different form: a figure reported from a single run with a tight
within-run spread, where the between-run spread was an order of magnitude
larger. A tight spread is the output format of confidence, not evidence of it.

Sentinels, not zeros
--------------------
Every way a reading can fail to happen has its own negative code. A silent `0`
reads exactly like a perfect result, which is how a blind instrument passes for
a healthy one -- and this repo has the scar: a container probe printed five
zeros when its process match failed, and a run of all-zero samples looked like a
beautifully stable system until something was written to say otherwise.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

# ── failure modes, each distinguishable from the others and from a real zero ──
UNREACHED = -1      # the interaction never happened (target absent, nav failed)
NO_DEVICE = -2      # no device answered (asleep, unauthorised, unplugged)
NO_READING = -3     # the interaction happened but produced no observation
BLIND = -4          # this lane cannot observe this class at all

_SENTINEL_NAMES = {
    UNREACHED: "UNREACHED",
    NO_DEVICE: "NO_DEVICE",
    NO_READING: "NO_READING",
    BLIND: "BLIND",
}


@dataclass(frozen=True)
class Reading:
    """One measured interaction, or one named reason there is no measurement.

    `timing_ms` and `quiescence_ms` deliberately have no combined accessor. Each
    carries its own resolution bound, and a caller that wants one number has to
    decide which bound it is willing to inherit.
    """

    timing_ms: float
    quiescence_ms: float
    timing_resolution_ms: float | None
    quiescence_resolution_ms: float | None
    reason: str = ""
    is_measurement: bool = True

    def __post_init__(self) -> None:
        if not self.is_measurement:
            return
        missing = [n for n, v in (("timing", self.timing_resolution_ms),
                                  ("quiescence", self.quiescence_resolution_ms))
                   if v is None]
        if missing:
            raise ValueError(
                f"missing resolution bound for: {', '.join(missing)} -- a number "
                f"with no stated precision is the shape that produced a "
                f"confident wrong answer in the October investigation")

    @classmethod
    def sentinel(cls, code: int, reason: str) -> "Reading":
        """A reading that did not happen, carrying which way it failed and why."""
        if code not in _SENTINEL_NAMES:
            raise ValueError(f"unknown sentinel {code!r}")
        return cls(timing_ms=code, quiescence_ms=code,
                   timing_resolution_ms=None, quiescence_resolution_ms=None,
                   reason=reason, is_measurement=False)

    @property
    def sentinel_name(self) -> str | None:
        return None if self.is_measurement else _SENTINEL_NAMES.get(int(self.timing_ms))

    def as_duration_ms(self) -> float:
        """The timing half as a number, refused for a sentinel.

        Letting a sentinel through here is how `-2` becomes a data point.
        """
        if not self.is_measurement:
            raise ValueError(
                f"{self.sentinel_name} is a sentinel, not a duration: "
                f"{self.reason}")
        return self.timing_ms


@dataclass(frozen=True)
class Aggregate:
    """Median of runs plus the spread that actually matters.

    `between_run_spread_ms` is `None` for a single run rather than `0.0`,
    because one run has no between-run spread to report and printing zero
    claims perfect repeatability the run never demonstrated.
    """

    n: int
    median_timing_ms: float
    median_quiescence_ms: float
    between_run_spread_ms: float | None
    within_run_spread_ms: float | None
    spread_note: str = field(default="")


_SPREAD_NOTE = (
    "within-run spread is a resolution bound on this instrument, not a "
    "confidence interval; the between-run spread is what a claim rests on"
)


def aggregate(readings: list[Reading]) -> Aggregate:
    """Median-of-runs for a set of readings taken under one configuration.

    Readings that are sentinels are excluded from the median rather than
    counted as zero -- a missing sample is not a measurement of nothing.
    """
    real = [r for r in readings if r.is_measurement]
    if not real:
        return Aggregate(n=0, median_timing_ms=NO_READING,
                         median_quiescence_ms=NO_READING,
                         between_run_spread_ms=None, within_run_spread_ms=None,
                         spread_note="no usable readings; " + _SPREAD_NOTE)
    timings = [r.timing_ms for r in real]
    quiets = [r.quiescence_ms for r in real]
    spread = (max(timings) - min(timings)) if len(real) > 1 else None
    within = max(r.timing_resolution_ms for r in real) if real else None
    return Aggregate(
        n=len(real),
        median_timing_ms=statistics.median(timings),
        median_quiescence_ms=statistics.median(quiets),
        between_run_spread_ms=spread,
        within_run_spread_ms=within,
        spread_note=_SPREAD_NOTE,
    )
