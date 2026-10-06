"""Tests for tools/soak/boundary_probe.py.

The load-bearing ones here are the negative cases. This probe's job is to
assert an ABSENCE (no gap at the boundary), which is the assertion shape that
passes vacuously most easily — so the tests that matter are the ones proving
it still *fails* when it should: a gap below the old 0.4 s detector minimum, a
missing noise floor, a sampler that did not achieve its requested rate, and a
restore that must happen even when the run blew up.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "soak"))

import boundary_probe as bp  # noqa: E402


# ── parse_silences ───────────────────────────────────────────────────────────


def test_parse_silences_pairs_starts_and_ends():
    text = ("silence_start: 1.5\n"
            "silence_end: 1.75 | silence_duration: 0.25\n")
    out = bp.parse_silences(text)
    assert out == [{"start_s": 1.5, "end_s": 1.75, "dur_s": 0.25, "open_ended": False}]


def test_parse_silences_closes_an_open_interval_at_duration():
    out = bp.parse_silences("silence_start: 9.0\n", 10.0)
    assert out[0]["open_ended"] is True
    assert out[0]["dur_s"] == 1.0


def test_parse_silences_keeps_open_interval_when_duration_unknown():
    out = bp.parse_silences("silence_start: 9.0\n")
    assert out[0]["open_ended"] is True and out[0]["end_s"] is None


# ── boundary detection ───────────────────────────────────────────────────────


def _row(t, tid, pos=1000, playing=True):
    return {"t": t, "track_id": tid, "position_ms": pos, "is_playing": playing}


def test_boundary_from_poll_detects_track_id_change():
    rows = [_row(1000, "A"), _row(1100, "A"), _row(1200, "B")]
    b = bp.boundary_from_poll(rows)
    assert b["t"] == 1200 and b["from"] == "A" and b["to"] == "B"
    assert b["via"] == "track_id"
    assert b["prev_t"] == 1100


def test_boundary_from_poll_detects_position_reset_for_a_repeated_track():
    # A queue can legitimately hold the same track twice; track_id never changes.
    rows = [_row(1000, "A", pos=300000), _row(1100, "A", pos=310000),
            _row(1200, "A", pos=500)]
    b = bp.boundary_from_poll(rows)
    assert b is not None and b["via"] == "position_reset"


def test_boundary_from_poll_ignores_error_rows():
    rows = [_row(1000, "A"), {"t": 1100, "err": "timeout"}, _row(1200, "B")]
    assert bp.boundary_from_poll(rows)["t"] == 1200


def test_boundary_from_poll_returns_none_without_a_transition():
    assert bp.boundary_from_poll([_row(1000, "A"), _row(1100, "A")]) is None


# ── achieved sampler interval ────────────────────────────────────────────────


def test_achieved_interval_reports_the_real_median_not_the_request():
    # Requested 100 ms; actually ran at ~1 Hz. The probe must report 1000.
    rows = [{"t": 0}, {"t": 1000}, {"t": 2000}, {"t": 3010}]
    assert bp.achieved_interval_ms(rows) == pytest.approx(1000, abs=10)


def test_achieved_interval_none_when_too_few_samples():
    assert bp.achieved_interval_ms([{"t": 0}, {"t": 100}]) is None


# ── pre/post split ───────────────────────────────────────────────────────────


def test_split_attributes_fade_out_to_the_outgoing_track():
    # The repo's own 5.582 s "gapless failure": 5.449 s of fade-out before the
    # transition and 0.133 s of real gap after it. Only the post side is ours.
    pre, post = bp.split_at_boundary(start_ms=0, end_ms=5582, boundary_ms=5449)
    assert pre == pytest.approx(5.449, abs=0.001)
    assert post == pytest.approx(0.133, abs=0.001)


def test_split_entirely_after_the_boundary_is_all_post():
    pre, post = bp.split_at_boundary(1000, 1300, 1000)
    assert pre == 0.0 and post == pytest.approx(0.3, abs=0.001)


def test_split_entirely_before_the_boundary_is_all_pre():
    pre, post = bp.split_at_boundary(1000, 1300, 5000)
    assert post == 0.0 and pre == pytest.approx(0.3, abs=0.001)


# ── floor handling: the refusal that keeps a blind run from reporting clean ──


def test_boundary_silences_refuses_without_a_floor():
    sil = [{"start_s": 1.0, "end_s": 1.2, "dur_s": 0.2, "level_db": -88.0}]
    with pytest.raises(bp.ProbeRefusal):
        bp.boundary_silences(sil, cap_t0_ms=0, boundary_ms=1000, floor_db=None)


def test_quiet_music_over_the_floor_is_not_counted_as_gap():
    # -70 dB against a -90 dB floor is 20 dB up: quiet audio, not silence.
    sil = [{"start_s": 1.0, "end_s": 1.3, "dur_s": 0.3, "level_db": -70.0}]
    out = bp.boundary_silences(sil, 0, 1000, floor_db=-90.3)
    assert out[0]["label"] == "quiet_audio"
    assert bp.post_boundary_total_s(out) == 0.0


def test_true_silence_near_the_floor_is_counted_as_gap():
    sil = [{"start_s": 1.0, "end_s": 1.3, "dur_s": 0.3, "level_db": -89.0}]
    out = bp.boundary_silences(sil, 0, 1000, floor_db=-90.3)
    assert out[0]["label"] == "boundary_gap"
    assert bp.post_boundary_total_s(out) == pytest.approx(0.3, abs=0.001)


def test_silence_far_from_the_boundary_is_not_attributed_to_it():
    sil = [{"start_s": 50.0, "end_s": 50.5, "dur_s": 0.5, "level_db": -89.0}]
    out = bp.boundary_silences(sil, 0, 1000, floor_db=-90.3)
    assert out == []


# ── the P0 regression: a sub-400ms gap must be visible ───────────────────────


def test_a_150ms_gap_survives_the_pipeline_at_the_probe_minimum():
    """The defect this probe exists to catch.

    At features.py's 0.4 default, ffmpeg never emits a 150 ms interval at all,
    so a gapping build and a fixed build both report zero silences. The probe's
    own minimum must be well below the gap of interest.
    """
    assert float(bp.SILENCE_MIN_S) < 0.15
    sil = [{"start_s": 1.0, "end_s": 1.15, "dur_s": 0.15, "level_db": -89.0}]
    out = bp.boundary_silences(sil, 0, 1000, floor_db=-90.3)
    assert bp.post_boundary_total_s(out) == pytest.approx(0.15, abs=0.001)


# ── level matching between the digital and acoustic arms ─────────────────────


def test_gain_matches_a_full_scale_stitch_to_the_loopback_level():
    # Digital stitch near full scale, Cast->WiiM loopback at -38.9 dB mean.
    assert bp.gain_to_match_db(-3.0, -38.9) == pytest.approx(-35.9, abs=0.01)


# ── verdict and controls ─────────────────────────────────────────────────────


def test_verdict_passes_within_tolerance_of_the_control():
    v = bp.verdict(acoustic_post_s=0.05, control_post_s=0.0)
    assert v["pass"] is True and v["delta_ms"] == pytest.approx(50.0, abs=0.1)


def test_verdict_fails_a_300ms_pause_the_old_gate_would_have_passed():
    v = bp.verdict(acoustic_post_s=0.30, control_post_s=0.0)
    assert v["pass"] is False


def test_verdict_does_not_credit_beating_the_source_control():
    # A reading far below the control is just as much a mismatch.
    v = bp.verdict(acoustic_post_s=0.0, control_post_s=0.5)
    assert v["pass"] is False


def test_graduated_controls_must_be_recovered_at_their_own_magnitude():
    assert bp.control_recovered(250, 0.26) is True
    # A 1 s control recovered says nothing about 100 ms sensitivity.
    assert bp.control_recovered(100, 0.0) is False


# ── loop termination ─────────────────────────────────────────────────────────


def test_three_consecutive_passes_terminate_the_series():
    res = [{"pass": True}, {"pass": True}, {"pass": True}]
    assert bp.series_pass(res, control_recovered=True)["done"] is True


def test_a_failure_resets_the_streak():
    res = [{"pass": True}, {"pass": True}, {"pass": False}, {"pass": True}]
    out = bp.series_pass(res, control_recovered=True)
    assert out["done"] is False and out["streak"] == 1


def test_an_abort_breaks_the_streak_so_consecutive_means_consecutive():
    """Otherwise an instrument that mostly refuses can assemble three
    scattered passes into false confidence."""
    res = [{"pass": True}, {"aborted": True}, {"pass": True}, {"pass": True}]
    out = bp.series_pass(res, control_recovered=True)
    assert out["done"] is False


def test_no_pass_without_a_recovered_positive_control():
    """An absence assertion from an instrument never shown to detect the
    thing is not evidence, however many times it repeats."""
    res = [{"pass": True}, {"pass": True}, {"pass": True}]
    out = bp.series_pass(res)
    assert out["done"] is False and out["verdict"] == "unverified_instrument"
    out = bp.series_pass(res, control_recovered=False)
    assert out["done"] is False


# ── subprocess failure must refuse, never read as a clean boundary ──────────


class RunDeps:
    """Minimal Deps stand-in for the ffmpeg call sites."""

    def __init__(self, rc=0, err="", out=""):
        self.rc, self.err, self.out = rc, err, out

    def run(self, args, timeout=180):
        return self.rc, self.out, self.err

    def redact(self, text):
        return str(text)


def test_a_failed_ffmpeg_analysis_refuses_instead_of_reporting_zero_gap():
    """Silences are parsed out of stderr, so a crashed ffmpeg yields an empty
    list — which downstream reads as a perfectly clean boundary."""
    with pytest.raises(bp.ProbeRefusal):
        bp.analyse_file(RunDeps(rc=1, err="ffmpeg: no such file"), "/nope.wav")


def test_a_failed_level_measurement_refuses_instead_of_inventing_a_gap():
    """Returning None would be read as 'level unmeasurable', and an
    unmeasurable level is counted as a real gap — so a crashed ffmpeg would
    manufacture gaps rather than hide them."""
    with pytest.raises(bp.ProbeRefusal):
        bp.level_in(RunDeps(rc=1, err="boom"), "/x.wav", 1.0, 0.2)


def test_an_open_ended_silence_refuses_rather_than_vanishing():
    """A capture that stopped INSIDE the gap would otherwise report 0 ms."""
    sil = [{"start_s": 9.0, "end_s": None, "dur_s": None, "open_ended": True}]
    with pytest.raises(bp.ProbeRefusal):
        bp.boundary_silences(sil, 0, 9500, floor_db=-90.3)


# ── credential redaction ─────────────────────────────────────────────────────


def test_redact_removes_the_admin_password_from_emitted_text():
    deps = bp.Deps(base="http://rig.invalid", admin_pw="hunter2")
    assert "hunter2" not in deps.redact("login failed for pw=hunter2")


def test_redact_also_catches_a_url_encoded_password():
    deps = bp.Deps(base="http://rig.invalid", admin_pw="a b&c")
    assert "a%20b%26c" not in deps.redact("GET /x?pw=a%20b%26c")


def test_a_network_call_refuses_without_a_configured_target():
    """No default target, ever.

    The harness hygiene test forbids a committed host literal, so an unset
    JP_BASE must fail loudly at the call rather than quietly pointing the probe
    somewhere unintended. Constructing Deps for analysis-only use stays legal.
    """
    deps = bp.Deps()
    with pytest.raises(bp.ProbeRefusal):
        deps.now_playing()


# ── rig state restore ────────────────────────────────────────────────────────


class FakeDeps:
    """Mirrors the real API split: /admin/settings does NOT carry the output
    routing keys, /admin/output/active does."""

    def __init__(self, settings, output, fail_on=(), fail_output=False):
        self.settings = dict(settings)
        self.output = dict(output)
        self.fail_on = set(fail_on)
        self.fail_output = fail_output
        self.writes = []
        self.output_writes = []

    def get_settings(self):
        return dict(self.settings)

    def get_output(self):
        return dict(self.output)

    def set_setting(self, key, value):
        self.writes.append((key, value))
        if key in self.fail_on:
            raise RuntimeError("boom")
        self.settings[key] = value

    def set_output(self, backend_type, device_id, host):
        self.output_writes.append((backend_type, device_id, host))
        if self.fail_output:
            raise RuntimeError("boom")
        self.output = {"backend_type": backend_type, "device_id": device_id,
                       "host": host}

    def redact(self, text):
        return str(text)


SNAP = {"gapless_enabled": True, "queue_end_behavior": "stop"}
OUT = {"backend_type": "direct", "device_id": "default", "host": "192.0.2.10"}


def test_restores_settings_and_output_routing_on_clean_exit():
    deps = FakeDeps(SNAP, OUT)
    with bp.RigState(deps):
        deps.set_setting("queue_end_behavior", "full_random")
        deps.set_output("chromecast", "cast-id", "10.0.0.9")
    assert deps.settings == SNAP
    assert deps.output == OUT


def test_output_routing_is_restored_even_though_settings_does_not_carry_it():
    """The bug this split exists for.

    GET /admin/settings returns gapless_enabled and queue_end_behavior but not
    output_backend_type/device_id/host. Snapshotting all five from the settings
    payload yields None for the output three, and a restore that skips None
    leaves the rig pointed at whatever the probe last selected — the exact
    unrestored-setting failure this class was written to prevent.
    """
    deps = FakeDeps(SNAP, OUT)
    with bp.RigState(deps):
        deps.set_output("chromecast", "cast-id", "10.0.0.9")
    assert deps.output_writes[-1] == ("direct", "default", "192.0.2.10")
    assert deps.output == OUT


def test_output_routing_restores_as_one_call_not_piecemeal():
    """backend/device/host are applied together; writing them separately would
    briefly point the router at a combination never selected."""
    deps = FakeDeps(SNAP, OUT)
    with bp.RigState(deps):
        deps.set_output("chromecast", "cast-id", "10.0.0.9")
    restore_calls = deps.output_writes[1:]
    assert len(restore_calls) == 1


def test_restores_on_exception_and_does_not_swallow_it():
    deps = FakeDeps(SNAP, OUT)
    with pytest.raises(ValueError):
        with bp.RigState(deps):
            deps.set_setting("queue_end_behavior", "full_random")
            deps.set_output("chromecast", "cast-id", "10.0.0.9")
            raise ValueError("mid-run failure")
    assert deps.settings["queue_end_behavior"] == "stop"
    assert deps.output == OUT


def test_every_key_is_attempted_even_when_one_restore_raises():
    """A partial restore that stops at the first error is how the worst leaks
    survive — the 9-day auto-DJ was a single un-restored setting."""
    deps = FakeDeps(SNAP, OUT, fail_on={"gapless_enabled"})
    with bp.RigState(deps) as st:
        deps.set_setting("queue_end_behavior", "full_random")
        deps.set_output("chromecast", "cast-id", "10.0.0.9")
    attempted = {k for k, _ in deps.writes}
    assert set(bp.MANAGED_SETTINGS) <= attempted
    assert st.restore_errors and "gapless_enabled" in st.restore_errors[0]
    # The settings failure must not prevent the output restore.
    assert deps.output == OUT


def test_a_failed_output_restore_is_recorded_not_swallowed():
    deps = FakeDeps(SNAP, OUT, fail_output=True)
    with bp.RigState(deps) as st:
        pass
    assert any("output" in e for e in st.restore_errors)


def test_refuses_to_run_when_a_managed_key_cannot_be_snapshotted():
    """A key the snapshot could not read is a key the restore cannot put back.
    Finding that out after the run is how a setting gets left flipped."""
    partial = {"gapless_enabled": True}   # queue_end_behavior absent
    deps = FakeDeps(partial, OUT)
    with pytest.raises(bp.ProbeRefusal):
        with bp.RigState(deps):
            pass


def test_refuses_when_the_output_endpoint_is_missing_a_key():
    deps = FakeDeps(SNAP, {"backend_type": "direct"})
    with pytest.raises(bp.ProbeRefusal):
        with bp.RigState(deps):
            pass


# ── analyse_boundary orchestration guards ───────────────────────────────────


class AnalyseDeps(RunDeps):
    """Drives analyse_boundary with a canned ffmpeg analysis."""

    def __init__(self, analysis):
        super().__init__()
        self.analysis = analysis


def _patched_analyse(monkeypatch, analysis):
    monkeypatch.setattr(bp, "analyse_file", lambda deps, path, **kw: analysis)


ROWS = [{"t": 1000, "track_id": "A", "position_ms": 1000, "is_playing": True},
        {"t": 1050, "track_id": "A", "position_ms": 1050, "is_playing": True},
        {"t": 1100, "track_id": "B", "position_ms": 10, "is_playing": True}]


def test_refuses_when_the_transition_falls_outside_the_capture(monkeypatch):
    """A wrong cap_t0_ms puts the boundary outside the audio, nothing matches
    the window, and the result is a confident 0 ms PASS measured from a
    recording that never contained the transition."""
    _patched_analyse(monkeypatch, {"duration_s": 1.0, "levels": {}, "silences": [],
                                   "min_silence_s": "0.02"})
    with pytest.raises(bp.ProbeRefusal, match="outside the capture"):
        bp.analyse_boundary(RunDeps(), "/c.wav", cap_t0_ms=900_000,
                            poll_rows=ROWS, floor_db=-90.3)


def test_refuses_when_the_transition_is_pinned_coarser_than_the_bar(monkeypatch):
    _patched_analyse(monkeypatch, {"duration_s": 60.0, "levels": {}, "silences": [],
                                   "min_silence_s": "0.02"})
    coarse = [{"t": 1000, "track_id": "A", "position_ms": 1000, "is_playing": True},
              {"t": 3000, "track_id": "A", "position_ms": 3000, "is_playing": True},
              {"t": 5000, "track_id": "B", "position_ms": 10, "is_playing": True}]
    with pytest.raises(bp.ProbeRefusal, match="pinned only"):
        bp.analyse_boundary(RunDeps(), "/c.wav", cap_t0_ms=0,
                            poll_rows=coarse, floor_db=-90.3,
                            requested_interval_ms=2000)


def test_refuses_when_the_sampler_missed_its_requested_rate(monkeypatch):
    _patched_analyse(monkeypatch, {"duration_s": 60.0, "levels": {}, "silences": [],
                                   "min_silence_s": "0.02"})
    slow = [{"t": 0, "track_id": "A", "position_ms": 1, "is_playing": True},
            {"t": 1000, "track_id": "A", "position_ms": 2, "is_playing": True},
            {"t": 2000, "track_id": "B", "position_ms": 3, "is_playing": True}]
    with pytest.raises(bp.ProbeRefusal, match="achieved"):
        bp.analyse_boundary(RunDeps(), "/c.wav", cap_t0_ms=0, poll_rows=slow,
                            floor_db=-90.3, requested_interval_ms=100)


def test_the_rate_guard_is_on_by_default_not_opt_in(monkeypatch):
    """It used to be gated behind an optional CLI flag defaulting to None, so
    the protection was absent exactly when a caller forgot it."""
    _patched_analyse(monkeypatch, {"duration_s": 60.0, "levels": {}, "silences": [],
                                   "min_silence_s": "0.02"})
    slow = [{"t": 0, "track_id": "A", "position_ms": 1, "is_playing": True},
            {"t": 1000, "track_id": "A", "position_ms": 2, "is_playing": True},
            {"t": 2000, "track_id": "B", "position_ms": 3, "is_playing": True}]
    with pytest.raises(bp.ProbeRefusal):
        bp.analyse_boundary(RunDeps(), "/c.wav", cap_t0_ms=0, poll_rows=slow,
                            floor_db=-90.3)


def test_refuses_when_no_transition_is_present(monkeypatch):
    _patched_analyse(monkeypatch, {"duration_s": 60.0, "levels": {}, "silences": [],
                                   "min_silence_s": "0.02"})
    flat = [{"t": 0, "track_id": "A", "position_ms": 1, "is_playing": True},
            {"t": 100, "track_id": "A", "position_ms": 2, "is_playing": True},
            {"t": 200, "track_id": "A", "position_ms": 3, "is_playing": True}]
    with pytest.raises(bp.ProbeRefusal, match="no track transition"):
        bp.analyse_boundary(RunDeps(), "/c.wav", cap_t0_ms=0, poll_rows=flat,
                            floor_db=-90.3)


# ── authentication ──────────────────────────────────────────────────────────


def test_admin_calls_refuse_without_a_password():
    """The probe's whole restore guarantee runs through /admin; failing loud
    beats 401-ing on every call and refusing at snapshot time for a reason
    that looks unrelated."""
    deps = bp.Deps(base="http://rig.invalid", admin_pw="")
    with pytest.raises(bp.ProbeRefusal, match="JP_ADMIN_PW"):
        deps.login()


def test_the_first_admin_call_authenticates_before_the_request(monkeypatch):
    """Without this the probe 401s on every /admin call and RigState refuses
    at snapshot time for a reason that looks unrelated to auth."""
    deps = bp.Deps(base="http://rig.invalid", admin_pw="pw")
    seen = []

    def fake_open(req, timeout=20):
        seen.append(req.full_url)

        class R:
            def read(self_inner):
                return b'{"ok": true}'

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False
        return R()

    monkeypatch.setattr(deps, "_opener", lambda: SimpleOpener(fake_open))
    deps._req("/admin/settings")
    assert seen[0].endswith("/admin/auth/login/local")
    assert seen[1].endswith("/admin/settings")


class SimpleOpener:
    def __init__(self, fn):
        self.open = fn


def test_run_redacts_stdout_as_well_as_stderr():
    deps = bp.Deps(base="http://rig.invalid", admin_pw="hunter2")
    import subprocess as sp

    class P:
        returncode = 0
        stdout = "echoed hunter2"
        stderr = "err hunter2"
    deps_run_orig = sp.run
    try:
        sp.run = lambda *a, **k: P()
        rc, out, err = deps.run(["true"])
    finally:
        sp.run = deps_run_orig
    assert "hunter2" not in out and "hunter2" not in err
