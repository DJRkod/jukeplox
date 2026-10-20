"""Unit tests for the canary machinery: fixtures, titration, and the meta-verdict.

A canary is a build with a known defect in it. A lane earns the right to report
by being observed failing against one, and the meta-test's failure condition is
inverted: a lane that **passes** a canary has failed, because it just proved it
cannot see the thing it claims to watch for.

Three properties are load-bearing and each has a reason drawn from this repo:

  * **Two fixtures are recorded defects, not injections.** The requirement asks
    for a known-true defect from the project's history. Authored injections
    demonstrate that a detector catches defects designed by whoever wrote the
    detector, which is the weaker claim this project has already been caught by.
    A recorded fixture therefore carries no patch at all -- it exists in HEAD.
  * **An injected fixture is a magnitude series, not one dose.** A lane
    credentialed against a two-second injected stall can miss a real 80ms
    regression with nothing in its record revealing the gap. Titration records
    the smallest magnitude the lane actually resolved.
  * **A drifted anchor fails loudly.** If the source text a patch looks for has
    moved, the "canary" is an unmodified build, the lane correctly reports
    nothing wrong, and the credential records a detection that never happened.
    That is a wrong mutant reporting a false pass, which has happened here
    before.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _load(name, relpath):
    """Load a rig module by path, registering it so dataclasses can resolve.

    See the note in `tests/test_clientrig_contract.py`.
    """
    spec = importlib.util.spec_from_file_location(name, _ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


cy = _load("clientrig_canary", "tools/clientrig/canary.py")

_FIXTURE_DIR = _ROOT / "tools" / "clientrig" / "canaries"


def injected(**kw):
    base = dict(name="rail-jump-stall", kind="injected",
                defect_class="input-stall", target="static/browse/index.js",
                observable="frames from tap to first scroll write",
                anchor="scrollRoot.scrollTop = target.offsetTop",
                template="await __stall({magnitude});\n    scrollRoot.scrollTop = target.offsetTop",
                magnitudes=[2000, 500, 200, 80, 30])
    base.update(kw)
    return cy.Fixture.from_dict(base)


def recorded(**kw):
    base = dict(name="rail-jump-40px", kind="recorded",
                defect_class="jump-landing", target="static/browse/index.js",
                observable="post-landing correction count per rail tap",
                source_note="docs/solutions/ui-bugs/alphabet-rail-pointer-"
                            "capture-click-retarget-no-highlight.md",
                expected={"corrections": 1, "letters_sampled": 7,
                          "letters_affected": 6})
    base.update(kw)
    return cy.Fixture.from_dict(base)


# ── the shipped fixture set ──────────────────────────────────────────────────

def test_the_shipped_fixture_set_loads():
    fixtures = cy.load_fixtures(_FIXTURE_DIR)
    assert len(fixtures) >= 6


def test_at_least_two_shipped_fixtures_are_recorded_defects():
    """Not negotiable: a credential resting only on injections is the weaker
    claim the requirement exists to rule out."""
    kinds = [f.kind for f in cy.load_fixtures(_FIXTURE_DIR)]
    assert kinds.count("recorded") >= 2, (
        "lane credentials would rest entirely on defects the rig author wrote")


def test_the_shipped_set_covers_the_classes_the_rig_claims():
    classes = {f.defect_class for f in cy.load_fixtures(_FIXTURE_DIR)}
    for required in ("jump-landing", "input-stall", "layout-thrash",
                     "scroll-restoration", "missing-repaint", "egress"):
        assert required in classes, f"no fixture covers {required}"


def test_every_shipped_fixture_names_a_discriminating_observable():
    """"The defect appeared" is not an observable. A quantity that moves with
    the defect is."""
    for f in cy.load_fixtures(_FIXTURE_DIR):
        assert f.observable and len(f.observable) > 10, f.name


def test_every_graded_fixture_has_a_descending_magnitude_series():
    for f in cy.load_fixtures(_FIXTURE_DIR):
        if f.kind != "injected" or not f.graded:
            continue
        assert len(f.magnitudes) >= 3, f"{f.name}: a floor needs several points"
        assert list(f.magnitudes) == sorted(f.magnitudes, reverse=True), (
            f"{f.name}: magnitudes must descend so the floor is the last "
            f"detected value")


def test_binary_fixtures_declare_no_magnitudes_and_claim_no_floor():
    """The rail either repaints or it does not; data either leaves the
    installation or it does not. A magnitude series over an all-or-nothing
    defect yields a floor that looks measured and is not, which is the failure
    the floor exists to prevent."""
    binary = [f for f in cy.load_fixtures(_FIXTURE_DIR)
              if f.kind == "injected" and not f.graded]
    assert binary, "expected the repaint and egress fixtures to be binary"
    for f in binary:
        assert f.magnitudes == ()


def test_a_binary_fixture_declaring_magnitudes_is_refused():
    with pytest.raises(ValueError, match="binary"):
        injected(graded=False, magnitudes=[1, 1, 1],
                 template="scrollRoot.scrollTop = 0;")


def test_a_binary_fixture_whose_template_wants_a_magnitude_is_refused():
    with pytest.raises(ValueError, match="binary|substitute"):
        injected(graded=False, magnitudes=[],
                 template="__stall({magnitude});")


def test_a_graded_fixture_applied_without_a_magnitude_is_refused():
    src = "function jump() {\n    scrollRoot.scrollTop = target.offsetTop;\n}"
    with pytest.raises(ValueError, match="magnitude"):
        cy.apply_fixture(injected(), magnitude=None, text=src)


def test_a_binary_fixture_applies_without_a_magnitude():
    src = "function jump() {\n    scrollRoot.scrollTop = target.offsetTop;\n}"
    f = injected(graded=False, magnitudes=[],
                 template="/* suppressed */ scrollRoot.scrollTop = target.offsetTop")
    out = cy.apply_fixture(f, magnitude=None, text=src)
    assert "suppressed" in out


def test_every_shipped_recorded_fixture_cites_where_it_was_recorded():
    for f in cy.load_fixtures(_FIXTURE_DIR):
        if f.kind == "recorded":
            assert f.source_note, f"{f.name} claims to be recorded but cites nothing"


# ── fixture validation ──────────────────────────────────────────────────────

def test_a_recorded_fixture_carrying_a_patch_is_refused():
    """A recorded defect exists in HEAD. Patching one in means it is an
    injection wearing the stronger label."""
    with pytest.raises(ValueError, match="recorded"):
        recorded(anchor="something", template="something else")


def test_an_injected_fixture_without_magnitudes_is_refused():
    with pytest.raises(ValueError, match="magnitude"):
        injected(magnitudes=[])


def test_an_injected_fixture_without_an_anchor_is_refused():
    with pytest.raises(ValueError, match="anchor"):
        injected(anchor="")


def test_a_fixture_with_an_unknown_kind_is_refused():
    with pytest.raises(ValueError, match="kind"):
        injected(kind="pretend")


def test_a_template_without_a_magnitude_placeholder_is_refused():
    """A template that ignores the magnitude silently injects one fixed dose at
    every rung of the titration, so the floor it reports is meaningless."""
    with pytest.raises(ValueError, match="magnitude"):
        injected(template="scrollRoot.scrollTop = target.offsetTop")


# ── applying a fixture ──────────────────────────────────────────────────────

def test_applying_an_injected_fixture_substitutes_the_magnitude():
    src = "function jump() {\n    scrollRoot.scrollTop = target.offsetTop;\n}"
    out = cy.apply_fixture(injected(), magnitude=500, text=src)
    assert "__stall(500)" in out
    assert out != src


def test_applying_a_fixture_twice_at_different_magnitudes_differs():
    src = "function jump() {\n    scrollRoot.scrollTop = target.offsetTop;\n}"
    a = cy.apply_fixture(injected(), magnitude=2000, text=src)
    b = cy.apply_fixture(injected(), magnitude=80, text=src)
    assert a != b


def test_a_drifted_anchor_fails_loudly_rather_than_producing_a_clean_build():
    """The wrong-mutant hazard. A silently-unpatched canary makes the lane look
    correct while recording a detection that never happened."""
    with pytest.raises(cy.AnchorDrift, match="anchor"):
        cy.apply_fixture(injected(), magnitude=500,
                         text="function jump() { /* refactored away */ }")


def test_an_anchor_matching_more_than_once_fails_loudly():
    """Two matches means the patch lands somewhere unintended as well."""
    src = ("scrollRoot.scrollTop = target.offsetTop;\n"
           "scrollRoot.scrollTop = target.offsetTop;")
    with pytest.raises(cy.AnchorDrift, match="2 times|twice|multiple"):
        cy.apply_fixture(injected(), magnitude=500, text=src)


def test_applying_a_recorded_fixture_is_refused():
    with pytest.raises(ValueError, match="recorded"):
        cy.apply_fixture(recorded(), magnitude=None, text="anything")


# ── titration: the sensitivity floor ────────────────────────────────────────

def test_the_floor_is_the_smallest_detected_magnitude():
    out = cy.titrate({2000: True, 500: True, 200: True, 80: False, 30: False})
    assert out.floor == 200
    assert out.ok


def test_a_lane_detecting_nothing_has_no_floor_and_is_not_credentialed():
    out = cy.titrate({2000: False, 500: False, 200: False})
    assert out.floor is None
    assert not out.ok
    assert "nothing" in out.reason.lower() or "no magnitude" in out.reason.lower()


def test_a_lane_detecting_everything_reports_the_smallest_tried_and_says_so():
    """The floor is then a bound, not a measurement -- the series did not go low
    enough to find where the lane stops seeing."""
    out = cy.titrate({2000: True, 500: True, 200: True})
    assert out.floor == 200
    assert out.ok
    assert "smallest tried" in out.reason.lower() or "bound" in out.reason.lower()


def test_a_non_monotonic_result_is_reported_as_suspect_not_averaged():
    """Detected at 80 but not at 500 means the instrument is unstable, and
    picking a floor from it would launder noise into a credential."""
    out = cy.titrate({2000: True, 500: False, 200: True, 80: True})
    assert not out.ok
    assert "monotonic" in out.reason.lower() or "unstable" in out.reason.lower()


def test_titrating_nothing_is_refused():
    out = cy.titrate({})
    assert out.floor is None
    assert not out.ok


# ── the meta-verdict: passing is the failure condition ──────────────────────

def test_a_lane_that_detects_its_canary_is_red_which_is_the_good_outcome():
    v = cy.meta_verdict(fixture=injected(), lane="chromium-desktop",
                        detected=True, blind_classes=(),
                        credential_blind=())
    assert v.outcome == "red"
    assert v.ok


def test_a_lane_that_passes_its_canary_has_failed_the_meta_test():
    v = cy.meta_verdict(fixture=injected(), lane="chromium-desktop",
                        detected=False, blind_classes=(),
                        credential_blind=())
    assert v.outcome == "passed"
    assert not v.ok
    assert "input-stall" in v.reason


def test_a_lane_blind_per_its_credential_is_accepted_as_not_failing():
    v = cy.meta_verdict(fixture=injected(), lane="webkit-local",
                        detected=False, blind_classes=("input-stall",),
                        credential_blind=("input-stall",))
    assert v.outcome == "blind"
    assert v.ok


def test_blindness_the_credential_does_not_authorise_is_rejected():
    """The ratchet, enforced at the meta-test as well as in health.py -- this is
    the path a lane would actually take to silence a newly-failing canary."""
    v = cy.meta_verdict(fixture=injected(), lane="webkit-local",
                        detected=False, blind_classes=("input-stall",),
                        credential_blind=())
    assert not v.ok
    assert "credential" in v.reason.lower()


def test_a_lane_claiming_blindness_while_also_detecting_is_reported():
    """Contradictory self-report: it says it cannot see this class and then saw
    it. Either the blind list or the detection is wrong."""
    v = cy.meta_verdict(fixture=injected(), lane="chromium-desktop",
                        detected=True, blind_classes=("input-stall",),
                        credential_blind=("input-stall",))
    assert not v.ok
    assert "contradict" in v.reason.lower() or "both" in v.reason.lower()


# ── anchors must resolve against real source, checked on an ordinary run ────

def _injected_against_existing_targets():
    out = []
    for f in cy.load_fixtures(_FIXTURE_DIR):
        if f.kind == "injected" and (_ROOT / f.target).exists():
            out.append(f)
    return out


def test_every_injected_anchor_matches_its_real_target_exactly_once():
    """The static half of the drift guard, and the strongest check this unit can
    make without a lane.

    A fixture whose anchor has moved patches nothing, so the "canary" is a clean
    build, the lane correctly reports no defect, and the credential records a
    detection that never happened. Catching that on an ordinary test run beats
    catching it at credentialing time, and beats not catching it at all.
    """
    checked = _injected_against_existing_targets()
    assert checked, "no injected fixture has a target that exists yet"
    for f in checked:
        hits = (_ROOT / f.target).read_text(encoding="utf-8").count(f.anchor)
        assert hits == 1, (
            f"{f.name}: anchor matches {hits} times in {f.target}. 0 means "
            f"applying this fixture would silently produce a CLEAN build; "
            f">1 means the patch also lands somewhere unintended")


def test_applying_a_fixture_to_real_source_changes_it():
    """Proves the anchor and template work together against the actual file,
    not just against a hand-built string."""
    # Must be a GRADED fixture: binary ones carry no magnitudes by design, and
    # the first injected fixture with a real target became the binary egress
    # one the moment U10 built the reporting module.
    graded = [f for f in _injected_against_existing_targets() if f.graded]
    assert graded, "no graded fixture has a target that exists yet"
    f = graded[0]
    src = (_ROOT / f.target).read_text(encoding="utf-8")
    out = cy.apply_fixture(f, magnitude=f.magnitudes[0], text=src)
    assert out != src
    assert len(out) > len(src)


def test_fixtures_whose_target_does_not_exist_yet_are_named_not_hidden():
    """A fixture pointing at a file a later unit creates cannot have its anchor
    verified yet. That gap is declared here rather than passing silently, which
    is the same discipline the lanes apply to their blind classes.
    """
    pending = [f"{f.name} -> {f.target}"
               for f in cy.load_fixtures(_FIXTURE_DIR)
               if f.kind == "injected" and not (_ROOT / f.target).exists()]
    # Nothing is pending any more: the egress fixture targeted the
    # guest-reporting module, U10 built it, and this test is what said so --
    # it failed the moment the file appeared, which is how the stale anchor
    # written back in U2 was found. Every injected anchor is now checked for
    # real by the test above.
    assert pending == [], (
        f"unexpected set of unverifiable anchors: {pending} -- a fixture is "
        f"pointing at a file nothing builds, so its anchor is never checked "
        f"and the fixture could be silently dead")


# ── the descriptors on disk are reviewable data, not code ───────────────────

def test_fixture_descriptors_are_plain_json():
    for p in sorted(_FIXTURE_DIR.glob("*.json")):
        json.loads(p.read_text(encoding="utf-8"))


def test_the_canary_directory_documents_itself():
    readme = _FIXTURE_DIR / "README.md"
    assert readme.exists()
    text = readme.read_text(encoding="utf-8").lower()
    assert "passing is the failure condition" in text
