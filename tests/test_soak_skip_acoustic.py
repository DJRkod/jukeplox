"""Tests for the acoustic skip probe's analysis half (tools/soak/skip_acoustic.py).

The probe itself only runs on the rig, but everything that turns audio into a
number is pure and belongs under test here. The point of these tests is not
coverage: it is that an instrument which silently reports a good number is worse
than no instrument, and three of the guards below exist because an earlier
version of this measurement produced four confident wrong conclusions in a row.
"""
from __future__ import annotations

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tools", "soak"))

import skip_acoustic as sa  # noqa: E402
from boundary_probe import ProbeRefusal  # noqa: E402

# Derived, never hardcoded: when the alphabet changed to make room for the
# seek trigger, literal 850/1000/1180 stopped being marker tones at all and
# six tests began asserting about frequencies the detector ignores.
T0, T1, T2, T3 = sa.FIRST_HALF_TONES
TB = sa.SECOND_HALF_TONE


# ---- helpers ---------------------------------------------------------------

def s24_tone(freq, seconds, rate=sa.CAP_RATE, amp=0.3, phase=0.0):
    """Interleaved stereo S24_3LE bytes carrying a sine at `freq`."""
    out = bytearray()
    n = int(seconds * rate)
    for i in range(n):
        v = int(amp * 8388607 * math.sin(2 * math.pi * freq * i / rate + phase))
        if v < 0:
            v += 0x1000000
        b = bytes(((v & 0xFF), (v >> 8) & 0xFF, (v >> 16) & 0xFF))
        out += b * sa.CAP_CHANNELS
    return bytes(out)


def s24_silence(seconds, rate=sa.CAP_RATE):
    n = int(seconds * rate)
    return bytes(n * sa.CAP_CHANNELS * sa.CAP_BYTES_PER_SAMPLE)


def marks_for(total_bytes, t0_ms=1000.0, chunk_frames=sa.CAP_PERIOD,
              rate=sa.CAP_RATE):
    """A clean mark timeline for a capture of `total_bytes`."""
    marks = []
    chunk = chunk_frames * sa.BYTES_PER_FRAME
    total = 0
    while total + chunk <= total_bytes:
        total += chunk
        marks.append((total, t0_ms + total / sa.BYTES_PER_FRAME / rate * 1000.0))
    return marks


# ---- tone plan -------------------------------------------------------------

def test_tone_cycle_never_repeats_within_a_skippable_distance():
    plan = sa.tone_cycle(16)
    for i in range(len(plan) - 3):
        assert plan[i] != plan[i + 1]
        assert plan[i] != plan[i + 2]
        assert plan[i] != plan[i + 3]


def test_no_marker_is_a_harmonic_of_another():
    """A harmonic collision would let distortion register as the wrong track."""
    for a in sa.MARKER_TONES:
        for b in sa.MARKER_TONES:
            if a == b:
                continue
            ratio = max(a, b) / min(a, b)
            assert abs(ratio - round(ratio)) > 0.1, (a, b)


def test_markers_stay_below_the_analysis_nyquist():
    for f in sa.MARKER_TONES:
        assert f < sa.ANALYSIS_RATE / 2


def test_tone_cycle_rejects_a_zero_length_plan():
    with pytest.raises(ValueError):
        sa.tone_cycle(0)


# ---- decimation and detection ---------------------------------------------

def test_decimation_preserves_the_tone_it_is_given():
    for freq in sa.MARKER_TONES:
        samples = sa.decimate_mono(s24_tone(freq, 0.2))
        assert sa.classify_window(samples[:sa.WINDOW]) == freq


def test_decimation_length_is_the_frame_count_over_the_factor():
    raw = s24_tone(T2, 0.1)
    frames = len(raw) // sa.BYTES_PER_FRAME
    assert len(sa.decimate_mono(raw)) == frames // sa.DECIMATE


def test_negative_samples_round_trip_through_the_24_bit_unpack():
    """A sign-extension bug here would halve the measured level and trip the
    silence floor on perfectly good audio."""
    samples = sa.decimate_mono(s24_tone(T2, 0.05, phase=math.pi))
    assert min(samples) < -0.05
    assert max(samples) > 0.05


def test_silence_is_not_classified_as_any_tone():
    samples = sa.decimate_mono(s24_silence(0.1))
    assert sa.classify_window(samples[:sa.WINDOW]) is None


def test_quiet_audio_below_the_floor_is_refused_not_guessed():
    samples = sa.decimate_mono(s24_tone(T2, 0.1, amp=0.0005))
    assert sa.classify_window(samples[:sa.WINDOW]) is None


def test_the_floor_is_a_real_guard_and_not_a_vacuous_one():
    """The same buffer must classify when the floor is relaxed. Without this,
    a floor that rejected everything would pass the test above for the wrong
    reason -- which is exactly how a guard that can never fire ships."""
    samples = sa.decimate_mono(s24_tone(T2, 0.1, amp=0.0005))
    assert sa.classify_window(samples[:sa.WINDOW], floor_db=-100.0) == T2


def test_two_tones_at_equal_strength_are_ambiguous_rather_than_arbitrary():
    a = sa.decimate_mono(s24_tone(T0, 0.1, amp=0.2))
    b = sa.decimate_mono(s24_tone(T2, 0.1, amp=0.2))
    mixed = [x + y for x, y in zip(a, b)]
    assert sa.classify_window(mixed[:sa.WINDOW]) is None


def test_a_dominant_tone_still_wins_against_a_weak_neighbour():
    a = sa.decimate_mono(s24_tone(T1, 0.1, amp=0.3))
    b = sa.decimate_mono(s24_tone(T3, 0.1, amp=0.02))
    mixed = [x + y for x, y in zip(a, b)]
    assert sa.classify_window(mixed[:sa.WINDOW]) == T1


def test_goertzel_peaks_at_the_tone_present():
    samples = sa.decimate_mono(s24_tone(T2, 0.1))
    w = samples[:sa.WINDOW]
    at_tone = sa.goertzel_power(w, T2)
    for other in (T0, T1, T3):
        assert at_tone > 10 * sa.goertzel_power(w, other)


def test_goertzel_of_nothing_is_zero_not_an_exception():
    assert sa.goertzel_power([], T2) == 0.0


# ---- runs ------------------------------------------------------------------

def test_a_transition_is_located_within_one_window_plus_hop():
    pre, post = 0.5, 0.5
    raw = s24_tone(T0, pre) + s24_tone(T2, post)
    samples = sa.decimate_mono(raw)
    runs = sa.sustained_runs(sa.label_windows(samples))
    tones = [r[0] for r in runs]
    assert tones == [T0, T2]
    truth = int(pre * sa.ANALYSIS_RATE)
    located = runs[1][1]
    budget = sa.WINDOW + sa.HOP
    assert abs(located - truth) <= budget, (located, truth, budget)


def test_a_one_window_blip_does_not_become_a_run():
    labels = [(0, 1, T0), (1, 2, T0), (2, 3, T0), (3, 4, T2),
              (4, 5, T0), (5, 6, T0), (6, 7, T0)]
    assert [r[0] for r in sa.sustained_runs(labels, sustain=3)] == [T0, T0]


def test_unclassifiable_windows_break_a_run_rather_than_being_filled_in():
    labels = [(0, 1, T0), (1, 2, T0), (2, 3, T0),
              (3, 4, None),
              (4, 5, T2), (5, 6, T2), (6, 7, T2)]
    runs = sa.sustained_runs(labels, sustain=3)
    assert [r[0] for r in runs] == [T0, T2]


def test_a_run_shorter_than_sustain_at_the_very_end_is_dropped():
    labels = [(0, 1, T0), (1, 2, T0), (2, 3, T0), (3, 4, T2), (4, 5, T2)]
    assert [r[0] for r in sa.sustained_runs(labels, sustain=3)] == [T0]


# ---- the timebase ----------------------------------------------------------

def test_mark_interpolation_is_exact_on_a_clean_timeline():
    # Long enough that a one-second step stays inside the capture.
    total = sa.CAP_PERIOD * sa.BYTES_PER_FRAME * 500
    marks = marks_for(total)
    # One second of audio in equals one second of host time out.
    one_sec_bytes = sa.CAP_RATE * sa.BYTES_PER_FRAME
    a = sa.mark_time_ms(marks[0][0] + 1, marks)
    b = sa.mark_time_ms(marks[0][0] + 1 + one_sec_bytes, marks)
    assert abs((b - a) - 1000.0) < 1.0


def test_a_single_mark_cannot_support_a_time_claim():
    with pytest.raises(ProbeRefusal):
        sa.mark_time_ms(10, [(100, 1000.0)])


def test_an_offset_past_the_capture_refuses_rather_than_extrapolating():
    marks = marks_for(sa.CAP_PERIOD * sa.BYTES_PER_FRAME * 10)
    with pytest.raises(ProbeRefusal):
        sa.mark_time_ms(marks[-1][0] + 1, marks)


def test_an_offset_before_the_capture_refuses():
    marks = marks_for(sa.CAP_PERIOD * sa.BYTES_PER_FRAME * 10)
    with pytest.raises(ProbeRefusal):
        sa.mark_time_ms(0, marks)


def test_a_clean_timeline_validates():
    marks = marks_for(sa.CAP_PERIOD * sa.BYTES_PER_FRAME * 200)
    info = sa.check_mark_timeline(marks)
    assert info["worst_deviation_ms"] < 1.0
    assert 0.99 < info["fit_slope"] < 1.01


def test_a_stalled_reader_is_refused_not_absorbed():
    """A stall means some samples were stamped far later than they were
    captured. Absorbing it would shift every latency in the run."""
    marks = marks_for(sa.CAP_PERIOD * sa.BYTES_PER_FRAME * 200)
    half = len(marks) // 2
    stalled = marks[:half] + [(b, t + 900.0) for b, t in marks[half:]]
    with pytest.raises(ProbeRefusal) as exc:
        sa.check_mark_timeline(stalled)
    assert "250 ms claim" in str(exc.value)


def test_a_timeline_within_the_budget_is_accepted():
    """The stall guard must not be so tight that ordinary jitter refuses."""
    marks = marks_for(sa.CAP_PERIOD * sa.BYTES_PER_FRAME * 200)
    jittered = [(b, t + (10.0 if i % 2 else 0.0))
                for i, (b, t) in enumerate(marks)]
    sa.check_mark_timeline(jittered)


def test_a_capture_clock_at_the_wrong_rate_is_refused():
    total = sa.CAP_PERIOD * sa.BYTES_PER_FRAME * 200
    marks = [(b, 1000.0 + (t - 1000.0) * 1.5) for b, t in marks_for(total)]
    with pytest.raises(ProbeRefusal) as exc:
        sa.check_mark_timeline(marks)
    assert "do not map to real time" in str(exc.value)


def test_too_few_marks_to_validate_is_a_refusal():
    with pytest.raises(ProbeRefusal):
        sa.check_mark_timeline([(100, 1.0), (200, 2.0)])


# ---- skip_to_audible -------------------------------------------------------

def _runs_and_marks(pre_s, post_s, from_tone=None, to_tone=None):
    from_tone = T0 if from_tone is None else from_tone
    to_tone = T2 if to_tone is None else to_tone
    raw = s24_tone(from_tone, pre_s) + s24_tone(to_tone, post_s)
    marks = marks_for(len(raw))
    runs = sa.sustained_runs(sa.label_windows(sa.decimate_mono(raw)))
    return runs, marks


def test_skip_to_audible_measures_the_gap_between_dispatch_and_the_new_tone():
    runs, marks = _runs_and_marks(2.0, 1.0)
    # Dispatch half a second into the old tone: 1.5 s should remain of it.
    dispatch = marks[0][1] + 500.0
    value, detail = sa.skip_to_audible_ms(runs, marks, dispatch, T0, T2)
    assert value > 0
    assert abs(value - 1500.0) < 150.0
    assert detail["audible_at_ms"] > dispatch


def test_an_earlier_appearance_of_the_destination_tone_is_not_counted():
    """The marker cycle means the destination tone also appears EARLIER in the
    capture. Accepting that run would report a near-zero or negative latency --
    a fake perfect fix, and the exact shape of error this probe exists to avoid.
    """
    raw = (s24_tone(T2, 0.5) + s24_tone(T0, 1.5) + s24_tone(T2, 0.8))
    marks = marks_for(len(raw))
    runs = sa.sustained_runs(sa.label_windows(sa.decimate_mono(raw)))
    assert [r[0] for r in runs] == [T2, T0, T2]
    dispatch = marks[0][1] + 1000.0   # inside the 700 Hz stretch
    value, _detail = sa.skip_to_audible_ms(runs, marks, dispatch, T0, T2)
    assert value > 0
    assert abs(value - 1000.0) < 150.0


def test_a_destination_that_never_becomes_audible_is_unreached_not_zero():
    runs, marks = _runs_and_marks(2.0, 0.0)
    dispatch = marks[0][1] + 100.0
    value, _detail = sa.skip_to_audible_ms(runs, marks, dispatch, T0, T2)
    assert value == sa.UNREACHED


def test_identical_origin_and_destination_tones_refuse():
    runs, marks = _runs_and_marks(1.0, 1.0)
    with pytest.raises(ProbeRefusal):
        sa.skip_to_audible_ms(runs, marks, marks[0][1] + 10.0, T0, T0)


def test_every_sentinel_is_negative_so_none_can_read_as_a_measurement():
    for sentinel in (sa.UNREACHED, sa.NOT_FLOW, sa.AMBIGUOUS):
        assert sentinel < 0
    assert len({sa.UNREACHED, sa.NOT_FLOW, sa.AMBIGUOUS}) == 3


# ---- reporting -------------------------------------------------------------

def test_refusals_are_counted_separately_and_never_averaged_in():
    out = sa.summarise([1000.0, 2000.0, sa.UNREACHED, sa.NOT_FLOW,
                        sa.AMBIGUOUS], "t")
    assert out["n"] == 2
    assert out["n_refused"] == 3
    assert out["median_ms"] == 1500.0


def test_an_all_refused_arm_reports_no_median_rather_than_a_passing_one():
    out = sa.summarise([sa.UNREACHED, sa.NOT_FLOW], "t")
    assert out["n"] == 0
    assert "median_ms" not in out
    assert "meets_target" not in out


def test_the_target_verdict_follows_the_median():
    assert sa.summarise([2000.0] * 5, "t", target_ms=2500.0)["meets_target"]
    assert not sa.summarise([3000.0] * 5, "t", target_ms=2500.0)["meets_target"]


def test_a_split_distribution_is_flagged_rather_than_reported_as_one_number():
    """A median across two mechanisms describes neither of them."""
    out = sa.summarise([900.0, 950.0, 1000.0, 11000.0, 11050.0, 11100.0], "t")
    assert out["possibly_bimodal"] is True


def test_a_single_cluster_is_not_flagged_as_split():
    out = sa.summarise([1000.0, 1100.0, 1200.0, 1300.0, 1400.0, 1500.0], "t")
    assert out.get("possibly_bimodal") is False


def test_the_distribution_is_reported_not_just_a_headline():
    out = sa.summarise([1000.0, 1800.0, 2600.0], "t")
    for key in ("min_ms", "max_ms", "spread_ms", "samples_ms", "n"):
        assert key in out


# ---- the seek trigger, and keeping three events distinguishable ------------

def test_the_second_half_tone_is_never_a_track_first_half():
    """The whole seek design rests on this. Every marker is <own tone> then the
    shared second-half tone, so:
        seek     = first-half tone -> second-half tone
        boundary = second-half tone -> the NEXT track's first-half tone
        skip     = whatever is playing -> the destination's first-half tone
    If the second-half tone were also some track's first half, a seek and a
    boundary could produce the identical transition and the probe would report
    one as the other."""
    assert sa.SECOND_HALF_TONE not in sa.FIRST_HALF_TONES
    assert sa.SECOND_HALF_TONE in sa.MARKER_TONES
    assert set(sa.FIRST_HALF_TONES) < set(sa.MARKER_TONES)


def test_tone_cycle_draws_only_from_the_first_half_alphabet():
    """A track whose first half were the shared second-half tone would make its
    own mid-track seek undetectable."""
    plan = sa.tone_cycle(12)
    assert set(plan) <= set(sa.FIRST_HALF_TONES)
    assert sa.SECOND_HALF_TONE not in plan


def test_marker_filename_changed_with_the_content_shape():
    """generate_markers skips files that already exist, so the single-tone
    leftovers had to stop matching the new names or an arm would quietly
    measure them and no seek would ever be detectable."""
    name = sa.marker_filename(0, sa.FIRST_HALF_TONES[0])
    assert "seg" in name and "tone" not in name


# ---- the per-backend gapless guard ----------------------------------------

class _FakeDeps:
    """Just enough Deps to exercise gapless_path_active."""

    def __init__(self, active_backend, flow=False):
        self._active = active_backend
        self._flow = flow
        self.asked = []

    def _req(self, path, payload=None, method=None):
        self.asked.append(path)
        return {"backend_type": self._active}

    def now_playing(self):
        self.asked.append("/api/now-playing")
        return {"output_session": {"gapless_flow_active": self._flow}}


def test_cast_gapless_is_checked_against_the_real_flow_session():
    d = _FakeDeps("chromecast", flow=True)
    assert sa.gapless_path_active(d, "chromecast") is True
    assert any("now-playing" in p for p in d.asked)

    d = _FakeDeps("chromecast", flow=False)
    assert sa.gapless_path_active(d, "chromecast") is False


def test_other_backends_fall_back_to_the_active_backend_check():
    """Weaker on purpose -- Direct and DLNA arm the next track with no session
    object to interrogate. The arm summary names which check ran so the weak
    one cannot be mistaken for the strong one."""
    d = _FakeDeps("dlna")
    assert sa.gapless_path_active(d, "dlna") is True
    assert not any("now-playing" in p for p in d.asked)

    d = _FakeDeps("direct")
    assert sa.gapless_path_active(d, "dlna") is False


def test_summarise_reports_the_row_count_alongside_the_refusals():
    """A sub-mode that ran and refused everything must be distinguishable from
    one that never ran. The mixed arms reported n_refused 0 beside nine
    refusals because the refusal rows had lost their cycle_mode, and the
    summary had no row count to contradict it."""
    out = sa.summarise([sa.UNREACHED, sa.NOT_FLOW, 1200.0], "t")
    assert out["n"] == 1
    assert out["n_refused"] == 2
    assert out["n_rows"] == 3

    never_ran = sa.summarise([], "t")
    assert never_ran["n"] == 0
    assert never_ran["n_refused"] == 0
    assert never_ran["n_rows"] == 0


def test_a_seamless_boundary_is_measured_as_zero_not_unreached():
    """Overlapping analysis windows mean the new tone's first window can start
    just before the old tone's last window ends. The matcher used to require
    the new run to begin at or after the old one ended, so a PERFECT join came
    back UNREACHED -- the guard hid the success it existed to confirm. Cast
    flow measured -10, -21 and -21 ms of overlap this way."""
    rate = sa.CAP_RATE
    bpf = sa.BYTES_PER_FRAME
    dec = sa.DECIMATE
    marks = marks_for(sa.CAP_PERIOD * bpf * 400)
    t0 = marks[0][1]

    def at(ms):
        """decimated-sample index for a host time `ms` after the first mark"""
        return int((ms / 1000.0) * rate / dec)

    # old tone ends at 1000 ms, new tone starts 15 ms EARLIER
    # Starts after the first mark: an offset before it is outside the
    # byte-to-time map and the run would be skipped, not matched.
    runs = [(TB, at(200), at(1000)), (T0, at(985), at(3000))]
    gap, detail = sa.boundary_gap_ms(runs, marks, TB, T0, t0)
    assert gap == 0.0, (gap, detail)
    assert detail["overlap_ms"] > 0


def test_an_overlap_larger_than_the_window_is_not_treated_as_consecutive():
    """Beyond one window span the overlap is not detector geometry, and
    accepting it would let an unrelated earlier run satisfy the match."""
    rate = sa.CAP_RATE
    marks = marks_for(sa.CAP_PERIOD * sa.BYTES_PER_FRAME * 400)
    t0 = marks[0][1]

    def at(ms):
        return int((ms / 1000.0) * rate / sa.DECIMATE)

    runs = [(TB, at(200), at(2000)), (T0, at(500), at(4000))]
    gap, _detail = sa.boundary_gap_ms(runs, marks, TB, T0, t0)
    assert gap == sa.UNREACHED
