"""Unit tests for the soak verdict's trends, instrument health and ranking.

The instrument-health checks are the point. A soak that finds nothing has two
possible meanings — nothing broke, or nobody was looking — and the report has to
tell them apart. Every check below is tested against input it must reject as
well as input it must accept.
"""
import importlib.util
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "tools" / "soak" / "verdict.py"
_spec = importlib.util.spec_from_file_location("soak_verdict", _SRC)
v = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(v)


def poll(t, rss=200_000, threads=12, fds=30, kids=6, media=0, **kw):
    return {"kind": "poll", "t": t, "rss_kb": rss, "threads": threads,
            "fds": fds, "children": kids, "media_procs": media, **kw}


# ── sampler health ────────────────────────────────────────────────────────────

def test_a_working_sampler_passes():
    rows = [poll(i, rss=200_000 + i * 10) for i in range(10)]
    assert v.sampler_health(rows) is None


def test_all_zero_samples_are_a_broken_sampler_not_a_stable_system():
    """jp-probe.sh prints five zeros when its process match fails, which looks
    exactly like a beautifully stable app."""
    rows = [poll(i, rss=0, threads=0, fds=0, kids=0, media=0) for i in range(10)]
    why = v.sampler_health(rows)
    assert why and "broken sampler" in why


def test_no_samples_at_all_is_reported():
    assert v.sampler_health([]) is not None
    assert v.sampler_health([{"kind": "start"}]) is not None


def test_every_sample_failing_is_reported_with_the_reason():
    rows = [{"kind": "poll", "t": i, "_proc_err": "probe timed out after 45s"}
            for i in range(5)]
    why = v.sampler_health(rows)
    assert why and "timed out" in why


def test_a_perfectly_flat_rss_is_flagged_as_probably_the_wrong_process():
    """PID 1 is a shell wrapper and reports a flat ~1.4 MB forever."""
    rows = [poll(i, rss=1400) for i in range(10)]
    why = v.sampler_health(rows)
    assert why and "wrong process" in why


def test_a_few_identical_samples_are_not_flagged():
    """Short runs legitimately look flat; the check must not cry wolf."""
    rows = [poll(i, rss=1400) for i in range(3)]
    assert v.sampler_health(rows) is None


def test_some_failures_among_good_samples_do_not_condemn_the_run():
    rows = [poll(i, rss=200_000 + i) for i in range(8)]
    rows.append({"kind": "poll", "t": 9, "_proc_err": "probe timed out"})
    assert v.sampler_health(rows) is None


# ── harness health ────────────────────────────────────────────────────────────

def test_a_busy_party_passes():
    rows = [{"act": "searchAndAdd", "added": True} for _ in range(50)]
    assert v.harness_health(rows) is None


def test_a_party_that_added_nothing_is_a_broken_harness():
    """A soak where the selectors matched nothing looks exactly like a soak
    where everything worked."""
    rows = [{"act": "searchAndAdd", "added": False} for _ in range(200)]
    why = v.harness_health(rows)
    assert why and "selectors matched nothing" in why


def test_no_guest_actions_at_all_is_reported():
    assert v.harness_health([]) is not None


def test_drill_adds_count_toward_harness_health():
    rows = [{"act": "drillArtistAlbum", "c": {"added": True}} for _ in range(30)]
    assert v.harness_health(rows) is None


# ── trends ────────────────────────────────────────────────────────────────────

def test_trend_reports_delta_and_rate():
    rows = [poll(0, rss=100_000), poll(3600, rss=200_000)]
    t = v.trend(rows, "rss_kb")
    assert t["first"] == 100_000 and t["last"] == 200_000
    assert t["delta"] == 100_000
    assert t["per_hour"] == pytest.approx(100_000, rel=0.01)


def test_trend_needs_two_points():
    assert v.trend([poll(0)], "rss_kb")["per_hour"] is None
    assert v.trend([], "rss_kb")["per_hour"] is None


def test_trend_skips_missing_fields_rather_than_counting_them_as_zero():
    """A missing sample is not a measurement of nothing — counting it as zero
    would invent a cliff."""
    rows = [poll(0, rss=100_000),
            {"kind": "poll", "t": 1800, "_proc_err": "boom"},
            poll(3600, rss=110_000)]
    t = v.trend(rows, "rss_kb")
    assert t["n"] == 2
    assert t["min"] == 100_000


def test_trend_is_order_independent():
    a = v.trend([poll(3600, rss=200_000), poll(0, rss=100_000)], "rss_kb")
    assert a["first"] == 100_000 and a["last"] == 200_000


# ── memory caveat ─────────────────────────────────────────────────────────────

def test_growing_memory_is_reported_with_its_caveat_not_as_a_leak():
    note = v.memory_note(v.trend([poll(0, rss=100_000), poll(3600, rss=400_000)],
                                 "rss_kb"))
    assert "does NOT establish a leak" in note
    assert "arena" in note


def test_flat_memory_says_so_plainly():
    note = v.memory_note(v.trend([poll(0, rss=100_000), poll(3600, rss=100_000)],
                                 "rss_kb"))
    assert "No leak indicated" in note


def test_memory_note_without_samples_does_not_pretend():
    assert "not enough samples" in v.memory_note(v.trend([], "rss_kb"))


# ── incident ranking ──────────────────────────────────────────────────────────

def test_incidents_rank_above_everything_explained():
    rows = [{"label": "explained_idle", "dur_s": 100},
            {"label": "boundary_gap", "dur_s": 5},
            {"label": "incident_silent_while_playing", "dur_s": 0.2},
            {"label": "incident_no_pipeline", "dur_s": 0.1}]
    out = v.rank_labelled(rows)
    assert out[0]["label"] == "incident_silent_while_playing"
    assert out[1]["label"] == "incident_no_pipeline"
    assert out[-1]["label"] == "explained_idle"


def test_unknown_ranks_above_explained_because_it_is_a_coverage_gap():
    rows = [{"label": "explained_operator", "dur_s": 9},
            {"label": "unknown", "dur_s": 1}]
    assert v.rank_labelled(rows)[0]["label"] == "unknown"


def test_longer_wins_within_a_label():
    rows = [{"label": "boundary_gap", "dur_s": 0.2},
            {"label": "boundary_gap", "dur_s": 4.0}]
    assert v.rank_labelled(rows)[0]["dur_s"] == 4.0


def test_ranking_tolerates_a_missing_duration():
    rows = [{"label": "boundary_gap"}, {"label": "boundary_gap", "dur_s": 1.0}]
    assert len(v.rank_labelled(rows)) == 2


# ── subprocess note ───────────────────────────────────────────────────────────

def test_direct_backend_is_told_that_media_procs_means_nothing_there():
    """Flat zero by construction on Direct — reporting 'no subprocess growth'
    would be the flat-looks-healthy trap one level up."""
    note = v.subprocess_note(v.trend([poll(0), poll(3600)], "media_procs"),
                             backend="direct")
    assert "not a liveness signal" in note
    assert "/dev/snd" in note


def test_growing_subprocess_count_is_called_out_as_the_known_signature():
    rows = [poll(0, media=0), poll(3600, media=8)]
    note = v.subprocess_note(v.trend(rows, "media_procs"), backend="chromecast")
    assert "#51" in note


def test_stable_subprocess_count_reports_its_range():
    rows = [poll(0, media=2), poll(3600, media=2)]
    note = v.subprocess_note(v.trend(rows, "media_procs"), backend="chromecast")
    assert "stable" in note


# ── shape adaptation ──────────────────────────────────────────────────────────

def test_container_fields_are_lifted_to_the_top_level():
    """monitor.py nests the container sample; every function here reads flat
    fields. A silent mismatch would make every trend read as 'no samples',
    which looks identical to a healthy flat process."""
    rows = [{"kind": "poll", "t": 0,
             "container": {"rss_kb": 100_000, "media_procs": 2}}]
    flat = v.flatten_container(rows)
    assert flat[0]["rss_kb"] == 100_000
    assert flat[0]["media_procs"] == 2
    assert flat[0]["kind"] == "poll"


def test_flatten_leaves_rows_without_a_container_alone():
    rows = [{"kind": "start", "t": 0},
            {"kind": "poll", "t": 1, "container": {"_proc_err": "boom"}}]
    flat = v.flatten_container(rows)
    assert flat[0] == {"kind": "start", "t": 0}
    assert flat[1]["_proc_err"] == "boom"


def test_trend_works_on_flattened_monitor_rows():
    rows = [{"kind": "poll", "t": 0, "container": {"rss_kb": 100_000}},
            {"kind": "poll", "t": 3600, "container": {"rss_kb": 150_000}}]
    t = v.trend(v.flatten_container(rows), "rss_kb")
    assert t["delta"] == 50_000
