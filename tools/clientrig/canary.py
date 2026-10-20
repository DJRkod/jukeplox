"""Canary fixtures: the builds a lane must be caught failing against.

A lane is untrusted until it has been observed failing against a known defect
and passing against the clean build, with both observations stored. This module
is the machinery for that: what a fixture is, how it is applied, how the
magnitude series becomes a sensitivity floor, and the meta-verdict whose failure
condition is inverted -- a lane that **passes** a canary has failed, because it
just demonstrated it cannot see what it claims to watch.

Why two kinds of fixture
------------------------
`recorded` fixtures are real defects from this project's history that still
exist in HEAD. They carry no patch, because there is nothing to patch in -- the
defect is already there. The two that qualify are the rail jump that lands forty
pixels short and the pointer-capture click retarget, both measured and written
up. These are the credential.

`injected` fixtures are authored, and they cover classes the recorded pair do
not reach. They are explicitly the weaker evidence: an injection demonstrates
that a detector catches defects designed by whoever wrote the detector, which is
not the same as catching one shaped the way real defects arrive. They are kept
as additional controls, never as the credential on their own.

Why a magnitude series rather than one dose
-------------------------------------------
This project's reference defect was a 47-second freeze. A lane credentialed
against an injected stall of that size proves almost nothing about catching a
200-millisecond one, and because the fixtures are authored, their size is chosen
rather than discovered. Each injected fixture therefore declares a descending
series, and titration records the smallest rung the lane still detected above
its own between-run spread. That figure travels on every result the lane later
produces, so a difference below it renders as "not observed" instead of a pass.

Why a drifted anchor is fatal
-----------------------------
If the source text a patch looks for has moved, the patch silently does nothing,
the "canary" is a clean build, the lane correctly reports no defect, and the
credential records a detection that never happened. That is a wrong mutant
reporting a false pass, and it has happened in this repo before -- one mutant
accidentally implemented the fix it was supposed to break. Anchors must match
exactly once or the application raises.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

_KINDS = ("recorded", "injected")
_MAGNITUDE_SLOT = "{magnitude}"


class AnchorDrift(Exception):
    """The patch's anchor text is absent, or present more than once.

    Either way the fixture cannot be trusted to inject what it claims, and
    producing a silently-clean build would be worse than failing here.
    """


@dataclass(frozen=True)
class Fixture:
    """One canary. `graded` decides whether it has a sensitivity floor at all.

    Most defects have a magnitude -- a stall is 2000ms or 40ms, a scroll restore
    is off by forty rows or by one -- and those get titrated. Some are binary:
    the rail either repaints or it does not; data either leaves the installation
    or it does not. Forcing a magnitude series onto a binary defect produces a
    floor that looks like a measurement and is not one, which is the exact
    failure mode the floor exists to prevent. A binary fixture therefore
    credentials detection and declares no floor, and the comparison renderer
    already refuses to compare magnitudes without one.
    """

    name: str
    kind: str
    defect_class: str
    target: str
    observable: str
    graded: bool = True
    anchor: str = ""
    template: str = ""
    magnitudes: tuple = ()
    source_note: str = ""
    expected: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "Fixture":
        kind = d.get("kind")
        if kind not in _KINDS:
            raise ValueError(
                f"{d.get('name')!r}: unknown kind {kind!r}; expected one of "
                f"{', '.join(_KINDS)}")

        observable = (d.get("observable") or "").strip()
        if len(observable) <= 10:
            raise ValueError(
                f"{d.get('name')!r}: needs a discriminating observable -- a "
                f"quantity that moves with the defect, not 'the defect "
                f"appeared'")

        anchor = (d.get("anchor") or "").strip()
        template = d.get("template") or ""
        magnitudes = tuple(d.get("magnitudes") or ())
        graded = bool(d.get("graded", True))

        if kind == "recorded":
            if anchor or template or magnitudes:
                raise ValueError(
                    f"{d.get('name')!r}: a recorded fixture carries no patch -- "
                    f"the defect already exists in HEAD. Patching one in makes "
                    f"it an injection wearing the stronger label")
            if not (d.get("source_note") or "").strip():
                raise ValueError(
                    f"{d.get('name')!r}: a recorded fixture must cite where it "
                    f"was recorded")
        else:
            if not anchor:
                raise ValueError(
                    f"{d.get('name')!r}: an injected fixture needs an anchor to "
                    f"patch against")
            if graded:
                if not magnitudes:
                    raise ValueError(
                        f"{d.get('name')!r}: a graded fixture needs a magnitude "
                        f"series -- one dose cannot produce a sensitivity floor. "
                        f"If the defect is all-or-nothing, set graded=false")
                if _MAGNITUDE_SLOT not in template:
                    raise ValueError(
                        f"{d.get('name')!r}: template ignores "
                        f"{_MAGNITUDE_SLOT}, so every rung of the titration "
                        f"would inject the same dose and the reported floor "
                        f"would be meaningless")
            else:
                if magnitudes:
                    raise ValueError(
                        f"{d.get('name')!r}: a binary fixture declares no "
                        f"magnitudes -- a floor derived from identical rungs "
                        f"looks like a measurement and is not one")
                if _MAGNITUDE_SLOT in template:
                    raise ValueError(
                        f"{d.get('name')!r}: template references "
                        f"{_MAGNITUDE_SLOT} but the fixture is binary, so "
                        f"there is no magnitude to substitute")

        return cls(
            name=d["name"], kind=kind, defect_class=d["defect_class"],
            target=d["target"], observable=observable, graded=graded,
            anchor=anchor, template=template, magnitudes=magnitudes,
            source_note=(d.get("source_note") or "").strip(),
            expected=dict(d.get("expected") or {}),
        )


def load_fixtures(directory) -> list[Fixture]:
    """Every fixture descriptor in a directory, validated on the way in."""
    out = []
    for path in sorted(Path(directory).glob("*.json")):
        out.append(Fixture.from_dict(json.loads(path.read_text(encoding="utf-8"))))
    return out


def apply_fixture(fixture: Fixture, magnitude, text: str) -> str:
    """Return `text` with the fixture's defect injected at `magnitude`.

    Raises on a recorded fixture (nothing to apply) and on an anchor that does
    not match exactly once.
    """
    if fixture.kind == "recorded":
        raise ValueError(
            f"{fixture.name}: recorded fixtures are not applied -- the defect "
            f"is already in HEAD; point the lane at the unmodified build")

    if fixture.graded and magnitude is None:
        raise ValueError(
            f"{fixture.name}: graded fixtures must be applied at a stated "
            f"magnitude -- that is what the titration varies")

    hits = text.count(fixture.anchor)
    if hits == 0:
        raise AnchorDrift(
            f"{fixture.name}: anchor not found in {fixture.target}. The source "
            f"has moved, so applying this fixture would produce a CLEAN build "
            f"and record a detection that never happened. Re-anchor it")
    if hits > 1:
        raise AnchorDrift(
            f"{fixture.name}: anchor matches {hits} times in {fixture.target}, "
            f"so the patch would also land somewhere unintended. Narrow it")

    return text.replace(fixture.anchor,
                        fixture.template.replace(_MAGNITUDE_SLOT, str(magnitude)))


@dataclass(frozen=True)
class Titration:
    floor: float | None
    ok: bool
    reason: str
    detected: tuple = ()


def titrate(detections: dict) -> Titration:
    """Turn per-magnitude detection results into a sensitivity floor.

    `detections` maps magnitude to whether the lane saw the defect at that
    magnitude. The floor is the smallest magnitude still detected -- but only
    when the results are monotonic, because a lane that saw the defect at 80ms
    and missed it at 500ms is reporting instability, and picking a floor out of
    that would launder noise into a credential.
    """
    if not detections:
        return Titration(None, False,
                         "no magnitudes were tried, so there is no floor to "
                         "report")

    rungs = sorted(detections.items(), key=lambda kv: kv[0], reverse=True)
    seen = [bool(v) for _, v in rungs]

    if not any(seen):
        return Titration(
            None, False,
            f"the lane detected nothing at any magnitude tried "
            f"({', '.join(str(m) for m, _ in rungs)}) -- it is not credentialed "
            f"for this class")

    # Monotonic means: detected at the large end, then stops, and never resumes.
    first_miss = next((i for i, v in enumerate(seen) if not v), None)
    if first_miss is not None and any(seen[first_miss:]):
        resumed = [str(rungs[i][0]) for i in range(first_miss, len(seen)) if seen[i]]
        return Titration(
            None, False,
            f"detection is not monotonic: the lane missed "
            f"{rungs[first_miss][0]} but then detected "
            f"{', '.join(resumed)}. That is an unstable instrument, not a "
            f"floor -- raise the run count and re-titrate")

    detected = tuple(m for m, v in rungs if v)
    floor = min(detected)
    if first_miss is None:
        return Titration(
            floor, True,
            f"detected at every magnitude tried; {floor} is the smallest tried "
            f"and therefore a bound, not a measured floor -- extend the series "
            f"downward to find where this lane actually stops seeing",
            detected)
    return Titration(
        floor, True,
        f"floor {floor}: detected down to {floor}, missed at "
        f"{rungs[first_miss][0]}",
        detected)


@dataclass(frozen=True)
class MetaVerdict:
    outcome: str
    ok: bool
    reason: str


def meta_verdict(fixture: Fixture, lane: str, detected: bool,
                 blind_classes, credential_blind) -> MetaVerdict:
    """The inverted check: passing a canary is the failure condition."""
    declared = tuple(blind_classes or ())
    authorised = tuple(credential_blind or ())
    cls = fixture.defect_class
    claims_blind = cls in declared

    if claims_blind and detected:
        return MetaVerdict(
            "contradictory", False,
            f"{lane} declares blindness to {cls} and then detected it -- both "
            f"cannot be true, so either the blind list or the detection is "
            f"wrong")

    if claims_blind:
        if cls not in authorised:
            return MetaVerdict(
                "unauthorised-blind", False,
                f"{lane} declares blindness to {cls}, which its credential "
                f"does not authorise. Widening a blind list is the cheapest "
                f"way to silence a failing canary; re-issue the credential "
                f"with a reason and a date instead")
        return MetaVerdict(
            "blind", True,
            f"{lane} is credentialed blind to {cls}; {fixture.name} is "
            f"recorded as not observed rather than passed")

    if detected:
        return MetaVerdict(
            "red", True,
            f"{lane} detected {fixture.name} via {fixture.observable}")

    return MetaVerdict(
        "passed", False,
        f"{lane} PASSED {fixture.name} without declaring blindness to {cls}. "
        f"Passing a canary is the failure condition: the lane just showed it "
        f"cannot see {cls}, so none of its greens for that class mean anything")
