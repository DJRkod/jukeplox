"""Unit tests for the soak's silence classifier and boundary-gap measurement.

Pure functions over two timelines, so they are testable without audio hardware.
Every case here includes input the classifier must NOT flag as well as input it
must — a detector that has only ever been observed saying yes is not a detector.
"""
import importlib.util
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "tools" / "soak" / "rig" / "classify.py"
_spec = importlib.util.spec_from_file_location("soak_classify", _SRC)
cls = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cls)

T0 = 1_790_000_000_000


def state(t_off_ms, track="a", playing=True, snd=2, pos=None):
    return {"t": T0 + t_off_ms, "track_id": track, "is_playing": playing,
            "snd_fds": snd, "position_ms": pos}


def timeline(*rows):
    return list(rows)


def silence(start_off_ms, dur_ms):
    return {"t_start": T0 + start_off_ms, "t_end": T0 + start_off_ms + dur_ms,
            "dur_s": dur_ms / 1000.0}


# ── explained cases: these must NOT become incidents ──────────────────────────

def test_silence_while_nothing_plays_is_explained():
    """Covers AE4. The queue ran dry; the recorder captured silence; that is not
    a dropout."""
    rows = timeline(state(0, playing=False, snd=0), state(1000, playing=False, snd=0))
    out = cls.classify([silence(0, 900)], rows)
    assert out[0]["label"] == "explained_idle"


def test_silence_beside_an_operator_action_is_attributed_to_the_operator():
    """The run's own drivers act as the operator. Their skips must be subtracted
    before anything is read as system behaviour."""
    rows = timeline(state(0), state(2000))
    out = cls.classify([silence(1000, 500)], rows, operator_events=[T0 + 1200])
    assert out[0]["label"] == "explained_operator"
    assert out[0]["operator_t"] == T0 + 1200


def test_an_operator_action_far_away_does_not_excuse_a_silence():
    """The negative half of the same rule — otherwise any run with drivers in it
    could explain away every fault."""
    rows = timeline(*[state(i * 1000) for i in range(0, 61)])
    out = cls.classify([silence(30_000, 3000)], rows, operator_events=[T0 + 1])
    assert out[0]["label"].startswith("incident")


def test_silence_in_the_settle_window_is_pending_not_an_incident():
    """`status: playing` flips on dispatch, not when audio flows. A lone silent
    window right after a change is a timing artefact — reporting it would have
    condemned a working backend on 2026-08-12."""
    rows = timeline(*[state(i * 500, track=("a" if i * 500 < 1000 else "b"))
                      for i in range(0, 20)])
    # Silence well after the boundary but still inside the settle window.
    out = cls.classify([silence(6000, 400)], rows, settle_ms=8000)
    assert out[0]["label"] == "pending_settle"


# ── the measurement ───────────────────────────────────────────────────────────

def test_silence_spanning_a_track_change_is_a_boundary_gap():
    rows = timeline(state(0, track="a"), state(1000, track="b"), state(2000, track="b"))
    out = cls.classify([silence(800, 400)], rows)
    assert out[0]["label"] == "boundary_gap"
    assert out[0]["from_track"] == "a" and out[0]["to_track"] == "b"


def test_a_start_from_idle_is_not_a_boundary():
    """Measuring a gap across a start would report the length of the idle period
    as though it were a gapless failure."""
    rows = timeline(state(0, track=None, playing=False, snd=0),
                    state(5000, track="a"))
    assert cls.track_boundaries(rows) == []


def test_a_stop_is_not_a_boundary():
    rows = timeline(state(0, track="a"), state(5000, track=None, playing=False, snd=0))
    assert cls.track_boundaries(rows) == []


def test_boundary_distributions_separate_a_gappy_arm_from_a_gapless_one():
    """Covers AE6. Not a threshold on any single boundary — the distributions."""
    gappy = [{"label": "boundary_gap", "dur_s": d} for d in (1.9, 2.0, 2.1, 2.0, 1.95)]
    gapless = [{"label": "boundary_gap", "dur_s": d} for d in
               (0.0, 0.01, 0.0, 0.02, 0.0)]
    g1 = cls.gap_summary(gappy)["boundary_gaps"]
    g2 = cls.gap_summary(gapless)["boundary_gaps"]
    assert g1["percentiles_s"]["p50"] > 1.5
    assert g2["percentiles_s"]["p50"] < 0.1
    assert g1["n"] == g2["n"] == 5


def test_drift_is_visible_as_a_moving_distribution():
    """Covers AE7. Gaps growing across an arm, where no single one is an outlier
    against a fixed threshold."""
    early = cls.percentiles([0.10, 0.11, 0.12, 0.10])
    late = cls.percentiles([0.40, 0.45, 0.42, 0.44])
    assert late["p50"] > early["p50"] * 3


def test_percentiles_of_nothing_is_empty_not_zero():
    """Zero would read as 'perfect gapless performance' when it means 'no data'."""
    assert cls.percentiles([]) == {}


# ── incidents: the cases the run exists to find ───────────────────────────────

def test_silence_while_playing_with_a_live_pipeline_is_an_incident():
    """Covers AE5. The data-plane failure — pipeline up, nothing coming out."""
    rows = timeline(*[state(i * 1000, snd=2) for i in range(0, 61)])
    out = cls.classify([silence(40_000, 4000)], rows)
    assert out[0]["label"] == "incident_silent_while_playing"


def test_playing_with_nothing_holding_the_card_is_its_own_incident():
    """Distinguished from the above because it points at a different layer: no
    pipeline at all, rather than a live pipeline emitting nothing. This is the
    shape the rig was found in on 2026-09-23 (issue #57)."""
    rows = timeline(*[state(i * 1000, snd=0) for i in range(0, 61)])
    out = cls.classify([silence(40_000, 4000)], rows)
    assert out[0]["label"] == "incident_no_pipeline"
    assert out[0]["snd_fds"] == 0


# ── honesty about the evidence ────────────────────────────────────────────────

def test_a_silence_with_no_nearby_state_is_unknown_not_clean():
    """A gap in the state timeline must not silently become a clean verdict."""
    rows = timeline(state(0), state(1000))
    out = cls.classify([silence(600_000, 1000)], rows)
    assert out[0]["label"] == "unknown"


def test_a_silence_without_a_timestamp_is_unknown():
    out = cls.classify([{"t_start": None, "t_end": None, "dur_s": 1.0}], timeline(state(0)))
    assert out[0]["label"] == "unknown"


def test_stale_state_is_not_stretched_to_cover_a_distant_moment():
    idx = cls.index_state(timeline(state(0)))
    assert cls.state_at(idx, T0 + 1000, max_staleness_ms=5000) is not None
    assert cls.state_at(idx, T0 + 60_000, max_staleness_ms=5000) is None


def test_every_silence_is_labelled():
    """An unclassified silence is a harness gap, not a result — so the classifier
    must never return fewer labels than it was given silences."""
    rows = timeline(state(0, playing=False, snd=0), state(10_000, snd=2),
                    state(20_000, track="b", snd=2))
    sil = [silence(0, 500), silence(9500, 800), silence(15_000, 3000),
           {"t_start": None, "t_end": None, "dur_s": 1.0}]
    out = cls.classify(sil, rows)
    assert len(out) == len(sil)
    assert all("label" in r for r in out)


def test_classification_is_deterministic():
    rows = timeline(state(0), state(10_000, track="b"), state(20_000, track="b"))
    sil = [silence(9800, 400), silence(15_000, 2000)]
    first = cls.classify(sil, rows)
    second = cls.classify(sil, rows)
    assert [r["label"] for r in first] == [r["label"] for r in second]


def test_summary_counts_every_label():
    rows = timeline(state(0, playing=False, snd=0), state(10_000, snd=2))
    out = cls.classify([silence(0, 500), silence(9000, 3000)], rows)
    summary = cls.gap_summary(out)
    assert sum(summary["labels"].values()) == 2


# ── boundaries the track id cannot see ────────────────────────────────────────

def test_a_position_reset_is_a_boundary_even_when_the_track_repeats():
    """A queue can hold the same track twice running, and then track_id never
    changes across a genuine boundary. Observed on the rig 2026-09-23: a capture
    spanning a real boundary reported zero of them, because the queue held three
    consecutive copies of one track."""
    rows = timeline(state(0, track="a", pos=250_000),
                    state(1000, track="a", pos=258_851),
                    state(2000, track="a", pos=0),
                    state(3000, track="a", pos=1200))
    b = cls.track_boundaries(rows)
    assert len(b) == 1
    assert b[0]["via"] == "position_reset"
    assert b[0]["t"] == T0 + 2000
    assert b[0]["from_position_ms"] == 258_851


def test_a_backward_seek_is_not_a_boundary():
    """Backwards, but nowhere near zero — the run's churn driver seeks, and every
    seek must not manufacture a boundary."""
    rows = timeline(state(0, track="a", pos=200_000),
                    state(1000, track="a", pos=120_000),
                    state(2000, track="a", pos=121_000))
    assert cls.track_boundaries(rows) == []


def test_normal_forward_progress_is_not_a_boundary():
    rows = timeline(*[state(i * 1000, track="a", pos=i * 1000) for i in range(0, 20)])
    assert cls.track_boundaries(rows) == []


def test_a_track_id_change_is_still_detected_and_labelled():
    rows = timeline(state(0, track="a", pos=5000), state(1000, track="b", pos=0))
    b = cls.track_boundaries(rows)
    assert len(b) == 1 and b[0]["via"] == "track_id"


def test_a_reset_while_stopped_is_not_a_boundary():
    rows = timeline(state(0, track="a", pos=250_000),
                    state(1000, track="a", pos=0, playing=False, snd=0))
    assert cls.track_boundaries(rows) == []


# ── the track's own silence is not the player's fault ─────────────────────────

def test_a_boundary_gap_is_split_at_the_transition():
    """Total silence conflates the outgoing track's fade with the player's start
    delay. Measured on the rig 2026-09-23: 5.582 s total, of which 5.449 s was
    the track's own outro and only 0.133 s fell after the transition."""
    rows = timeline(state(0, track="a", pos=250_000),
                    state(5449, track="a", pos=255_449),
                    state(5500, track="a", pos=0),
                    state(6000, track="a", pos=500))
    sil = [{"t_start": T0 + 51, "t_end": T0 + 5633, "dur_s": 5.582}]
    out = cls.classify(sil, rows, boundary_tolerance_ms=1500)
    assert out[0]["label"] == "boundary_gap"
    assert out[0]["pre_boundary_s"] == pytest.approx(5.449, abs=0.01)
    assert out[0]["post_boundary_s"] == pytest.approx(0.133, abs=0.01)


def test_the_summary_reports_post_boundary_separately():
    labelled = [
        {"label": "boundary_gap", "dur_s": 5.6, "post_boundary_s": 0.13},
        {"label": "boundary_gap", "dur_s": 5.5, "post_boundary_s": 0.12},
        {"label": "boundary_gap", "dur_s": 7.9, "post_boundary_s": 2.40},
    ]
    s = cls.gap_summary(labelled)
    # Total silence barely moves between a good and a bad boundary; the part
    # after the transition is where the difference actually shows.
    assert s["boundary_gaps"]["percentiles_s"]["p50"] == pytest.approx(5.6)
    assert s["post_boundary"]["n"] == 3
    assert s["post_boundary"]["max_s"] == pytest.approx(2.40)
    assert s["post_boundary"]["min_s"] == pytest.approx(0.12)


def test_post_boundary_is_never_negative():
    """A silence entirely before the transition must report zero after it, not a
    negative number that would drag an average downwards."""
    rows = timeline(state(0, track="a", pos=250_000),
                    state(3000, track="a", pos=253_000),
                    state(3100, track="a", pos=0),
                    state(4000, track="a", pos=900))
    sil = [{"t_start": T0 + 100, "t_end": T0 + 2000, "dur_s": 1.9}]
    out = cls.classify(sil, rows, boundary_tolerance_ms=1500)
    assert out[0]["post_boundary_s"] == 0.0


def test_boundary_split_is_clamped_to_the_silence_span():
    """A boundary reported just outside the silence, but inside the tolerance,
    must not make either half exceed the total — observed on the rig as
    total=1.165 with pre=1.224, which is nonsense and inflates any average."""
    rows = timeline(state(0, track="a", pos=40_000),
                    state(1400, track="a", pos=41_400),
                    state(1500, track="a", pos=0),
                    state(2500, track="a", pos=1000))
    # Silence ends before the reported boundary.
    sil = [{"t_start": T0 + 100, "t_end": T0 + 1265, "dur_s": 1.165}]
    out = cls.classify(sil, rows, boundary_tolerance_ms=1500)
    assert out[0]["label"] == "boundary_gap"
    assert out[0]["pre_boundary_s"] <= out[0]["dur_s"]
    assert out[0]["post_boundary_s"] <= out[0]["dur_s"]
    assert out[0]["post_boundary_s"] == 0.0


# ── quiet music is not silence (measured over 12 hours, 2026-09-24) ───────────

def test_quiet_audio_above_the_floor_is_not_an_incident():
    """silencedetect triggers below -55 dB, which real music dips under while
    still sounding. Over 12 hours of a real library, 17 of 18 flagged silences
    measured -67 to -82 dB against a -90.3 dB floor — every one of them audible.
    Without this the detector has a 17-in-18 false-positive rate."""
    rows = timeline(*[state(i * 1000, snd=2) for i in range(0, 61)])
    sil = [{**silence(40_000, 1500), "level_db": -70.0}]
    out = cls.classify(sil, rows, floor_db=-90.3)
    assert out[0]["label"] == "explained_quiet_audio"


def test_silence_at_the_noise_floor_is_still_an_incident():
    """The one that mattered: at the floor, nothing is coming out."""
    rows = timeline(*[state(i * 1000, snd=2) for i in range(0, 61)])
    sil = [{**silence(40_000, 4000), "level_db": -90.3}]
    out = cls.classify(sil, rows, floor_db=-90.3)
    assert out[0]["label"] == "incident_silent_while_playing"


def test_without_a_known_floor_the_level_check_does_not_fire():
    """No floor measured means no basis to call it quiet audio — fall through to
    the existing rules rather than silently excusing it."""
    rows = timeline(*[state(i * 1000, snd=2) for i in range(0, 61)])
    sil = [{**silence(40_000, 1500), "level_db": -70.0}]
    out = cls.classify(sil, rows, floor_db=None)
    assert out[0]["label"].startswith("incident")


def test_silence_at_the_very_start_of_a_capture_is_start_up_not_a_fault():
    """The run resumes, the pipeline builds, audio follows a beat later. The only
    floor-level silence in 12 hours was exactly this — 4.7s at the head of an arm
    after the changeover restart, with no boundary within 3.5 minutes."""
    rows = timeline(*[state(i * 1000, snd=2) for i in range(0, 31)])
    sil = [{**silence(2000, 4700), "level_db": -90.3}]
    out = cls.classify(sil, rows, floor_db=-90.3)
    assert out[0]["label"] == "explained_startup"


def test_floor_level_silence_well_past_start_up_is_still_an_incident():
    rows = timeline(*[state(i * 1000, snd=2) for i in range(0, 61)])
    sil = [{**silence(40_000, 4700), "level_db": -90.3}]
    out = cls.classify(sil, rows, floor_db=-90.3)
    assert out[0]["label"] == "incident_silent_while_playing"
