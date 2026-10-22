"""Tests for tools/soak/skip_probe.py.

The load-bearing ones here are the cases where a wrong instrument would report
success. This probe measures how long a listener keeps hearing a track they
rejected, and every tempting shortcut for detecting "the new track is audible"
reads ~0 on a fully broken build:

- Now Playing names the requested track the instant the queue advances, which
  is the start of the stale window, not its end.
- ``/api/playback/position`` maps DEVICE time through the stitch, so while the
  device is still inside the old track it returns that track's position — a
  positive number.

So ``test_now_playing_leading_the_device_does_not_read_as_zero`` is the real
regression guard: it drives exactly the timeline a broken build produces and
asserts the probe reports the true delay rather than agreeing with the UI.

The gate tests matter for the same reason. A gate that cannot fail is not a
gate, so each stop condition is exercised, and an unmeasured input halts rather
than defaulting to proceed.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools", "soak"))

import skip_probe as sp  # noqa: E402
from boundary_probe import ProbeRefusal  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────────


def _row(t, heard, *, playing=True, known=True, dev_ms=1234, age=200):
    return {"t": t, "heard_track_id": heard, "playing": playing,
            "device_time_known": known, "device_stream_ms": dev_ms,
            "device_time_age_ms": age}


class FakeDeps:
    """Scripted stand-in for boundary_probe.Deps.

    ``flow_states`` is consumed one entry per ``/admin/diagnostics/flow`` call
    so a test can walk the device from the old track to the new one.
    """

    def __init__(self, flow_states, queue_front="t-new", now_playing=None,
                 freeze_device=False):
        self.flow_states = list(flow_states)
        self.queue_front = queue_front
        # Real payload shape, verified against the rig: flat track_id, and
        # is_paused reported alongside is_playing.
        self._np = now_playing or {"is_playing": True, "is_paused": False,
                                   "track_id": "t-new"}
        self.freeze_device = freeze_device
        self._dev = 1234
        self._age = 100
        self._calls_since_status = 0
        self.calls = []

    def _next_flow(self, dev_step=120):
        """Advance the device clock per call unless frozen.

        A FROZEN device clock is a real failure mode (the production 5 s status
        cadence looks frozen to a 7 Hz poller), and the probe refuses on it --
        so the default fake has to advance, and `freeze_device` exercises the
        refusal.
        """
        st = (self.flow_states.pop(0) if len(self.flow_states) > 1
              else self.flow_states[0])
        st = json.loads(json.dumps(st))
        f = st.get("flow") or {}
        if f.get("device_stream_ms") is not None:
            self._dev += dev_step
            f["device_stream_ms"] = self._dev
            self._calls_since_status += 1
            if self.freeze_device:
                # No status ever lands: the age only ever climbs.
                self._age += 150
            elif self._calls_since_status >= 3:
                self._age = 40          # a fresh status landed -> age drops
                self._calls_since_status = 0
            else:
                self._age += 150
            f["device_time_age_ms"] = self._age
        return st

    def _req(self, path, payload=None, method=None, **kw):
        self.calls.append(path)
        if path == "/admin/diagnostics/flow":
            return self._next_flow()
        if path == "/api/queue":
            # Real shape, verified against the rig: rows come from
            # guest._track_dict and carry `track_id`, under a `queue` key.
            if self.queue_front is None:
                return {"queue": [], "history": [], "is_locked": False}
            return {"queue": [{"track_id": self.queue_front, "title": "x"}],
                    "history": [], "is_locked": False}
        if path == "/admin/playback/skip":
            return {"ok": True}
        return {}

    def now_playing(self):
        return self._np

    def redact(self, text):
        return str(text)


def _flow(heard, *, active=True, known=True, lead=4200, dev_ms=1234,
          fmt="flac", out_bytes=900_000, age=200):
    return {
        "gapless_flow_active": active,
        "flow": {
            "session": "ab12",
            "encode_format": fmt,
            "device_time_known": known,
            "encode_clock_ms": dev_ms + lead,
            "device_stream_ms": dev_ms,
            "held_offset_ms": dev_ms,
            "heard_track_id": heard,
            "total_lead_ms": lead,
            "device_time_age_ms": age,
            "session_elapsed_ms": dev_ms + lead - 2_000,
            "server_lead_ms": 2_000,
            "device_lag_ms": max(0, lead - 2_000),
            "out_buffer_bytes_lower_bound": out_bytes,
            "out_buffer_cap_bytes": 1_764_000,
        },
    }


# ── skip_to_audible_ms ───────────────────────────────────────────────────────


def test_reports_delta_when_device_reaches_the_requested_track():
    rows = [_row(1000, "t-old"), _row(1500, "t-old"), _row(5200, "t-new")]
    assert sp.skip_to_audible_ms(rows, 1000, "t-new") == 4200.0


def test_now_playing_leading_the_device_does_not_read_as_zero():
    """The regression guard. Now Playing names the new track at dispatch and
    the device-derived position is positive throughout, because the device is
    still playing the OLD track. A probe keyed on either would report ~0."""
    rows = [
        _row(0, "t-old", dev_ms=30_000),
        _row(500, "t-old", dev_ms=30_500),
        _row(3000, "t-old", dev_ms=33_000),
        _row(4800, "t-new", dev_ms=34_800),
    ]
    # Every row says "playing" with a positive device position, and the UI said
    # t-new from the start -- yet the honest answer is 4.8 s, not 0.
    assert sp.skip_to_audible_ms(rows, 0, "t-new") == 4800.0


def test_rows_before_dispatch_are_ignored():
    rows = [_row(100, "t-new"), _row(900, "t-old"), _row(2000, "t-new")]
    assert sp.skip_to_audible_ms(rows, 1000, "t-new") == 1000.0


def test_unreached_is_its_own_sentinel_not_zero():
    rows = [_row(0, "t-old"), _row(900, "t-old")]
    assert sp.skip_to_audible_ms(rows, 0, "t-new") == sp.UNREACHED
    assert sp.UNREACHED < 0


def test_paused_or_clockless_rows_cannot_satisfy_the_trigger():
    rows = [
        _row(100, "t-new", playing=False),
        _row(200, "t-new", known=False),
        _row(300, "t-new", dev_ms=0),
        _row(400, "t-new"),
    ]
    assert sp.skip_to_audible_ms(rows, 0, "t-new") == 400.0


def test_missing_requested_track_refuses():
    with pytest.raises(ProbeRefusal):
        sp.skip_to_audible_ms([_row(0, "t-new")], 0, None)


# ── lead_at_dispatch_ms / out_buffer_note ────────────────────────────────────


def test_lead_is_encode_clock_minus_device_position():
    assert sp.lead_at_dispatch_ms(_flow("t-old", lead=6100)["flow"]) == 6100


def test_no_device_clock_yields_its_own_sentinel():
    snap = _flow("t-old", known=False)["flow"]
    assert sp.lead_at_dispatch_ms(snap) == sp.NO_DEVICE_TIME
    assert sp.NO_DEVICE_TIME < 0


def test_absent_flow_session_refuses_rather_than_returning_zero():
    with pytest.raises(ProbeRefusal):
        sp.lead_at_dispatch_ms(None)


def test_lead_refuses_when_the_device_clock_is_too_stale_to_extrapolate():
    """The backend extrapolates device position from the last status, which is
    only valid while playback is uninterrupted. Past a long gap a pause or
    re-buffer is plausible and extrapolation would invent position."""
    snap = _flow("t-old", age=sp.MAX_DEVICE_AGE_MS + 1)["flow"]
    with pytest.raises(ProbeRefusal, match="stale"):
        sp.lead_at_dispatch_ms(snap)


def test_lead_refuses_on_a_clamped_zero_lead():
    """held_offset_from_device_time clamps to [0, fed_ms], so an extrapolation
    that overshot the encode clock arrives as a silent zero. A live session
    cannot have the device level with the encoder."""
    snap = _flow("t-old", lead=0)["flow"]
    snap["total_lead_ms"] = 0
    with pytest.raises(ProbeRefusal, match="overshot"):
        sp.lead_at_dispatch_ms(snap)


def test_lead_accepts_a_fresh_plausible_reading():
    assert sp.lead_at_dispatch_ms(_flow("t-old", lead=9500, age=300)["flow"]) == 9500


def test_out_buffer_ms_conversion_only_valid_for_wav():
    assert sp.out_buffer_note(_flow("t", fmt="flac")["flow"])["ms_conversion_valid"] is False
    assert sp.out_buffer_note(_flow("t", fmt="wav")["flow"])["ms_conversion_valid"] is True


def test_out_buffer_note_states_it_is_a_lower_bound():
    note = sp.out_buffer_note(_flow("t")["flow"])
    assert "lower bound" in note["basis"]
    assert note["out_buffer_bytes"] == 900_000


# ── modality ─────────────────────────────────────────────────────────────────


def test_modality_declines_a_verdict_on_too_few_samples():
    assert sp.modality([1000.0, 1100.0])["bimodal"] is None


def test_unimodal_spread_is_not_called_bimodal():
    assert sp.modality([3000, 3200, 3400, 3300, 3100, 3250])["bimodal"] is False


def test_a_tight_distribution_is_not_called_bimodal():
    """A relative-only gap rule is scale-blind. These five rig samples span
    298 ms with a 126 ms largest gap -- one population -- and the old rule
    flagged them bimodal, which would have halted the gate on noise."""
    out = sp.modality([11718.0, 11796.0, 11882.0, 11890.0, 12016.0])
    assert out["bimodal"] is False


def test_a_real_outlier_at_the_same_scale_is_still_detected():
    """The guard must not be loosened into uselessness: the 8,750 ms sample
    against a ~11.9 s cluster is a 2,156 ms gap and must still register."""
    out = sp.modality([8750.0, 10906.0, 11000.0, 11063.0, 11156.0, 11234.0])
    assert out["bimodal"] is True


def test_two_clusters_are_detected():
    out = sp.modality([500, 520, 540, 6000, 6100, 6200])
    assert out["bimodal"] is True
    assert len(out["clusters"]) == 2


def test_modality_ignores_sentinels():
    out = sp.modality([sp.UNREACHED, 3000, 3100, 3200, 3300])
    assert out["n"] == 4


# ── gate ─────────────────────────────────────────────────────────────────────


def _clear_samples():
    return [3000.0, 3100.0, 3200.0, 3050.0, 3150.0]


def test_gate_proceeds_only_when_every_condition_is_measured_and_clear():
    out = sp.gate(_clear_samples(), [600, 650, 620, 610, 640], reload_ms=500)
    assert out["proceed"] is True


def test_gate_halts_on_insufficient_samples():
    out = sp.gate([3000.0, 3100.0], [600, 600], reload_ms=500)
    assert out["proceed"] is False
    assert out["reason"] == "insufficient_samples"


def test_gate_halts_on_bimodal_distribution():
    out = sp.gate([500, 520, 540, 6000, 6100, 6200],
                  [100, 100, 100, 100, 100, 100], reload_ms=200)
    assert out["proceed"] is False
    assert out["reason"] == "bimodal_distribution"


def test_gate_halts_when_the_reload_cost_was_never_measured():
    """The 499 ms per-track boundary figure is a different operation, so an
    unmeasured re-LOAD is a halt rather than an assumed pass."""
    out = sp.gate(_clear_samples(), [600, 600, 600, 600, 600], reload_ms=None)
    assert out["proceed"] is False
    assert out["reason"] == "reload_cost_unmeasured"


def test_gate_reports_the_split_even_when_the_reload_is_unmeasured():
    """The split is the condition the plan review added, and it was invisible
    behind the re-LOAD check. On the rig the split came back 99% server-held
    while the gate reported only reload_cost_unmeasured."""
    out = sp.gate(_clear_samples(), [2900, 2950, 2975, 2925, 2960],
                  reload_ms=None)
    assert out["reason"] == "reload_cost_unmeasured"
    assert out["split"]["server_held_fraction"] > 0.9


def test_gate_halts_when_the_reload_approaches_the_baseline():
    out = sp.gate(_clear_samples(), [600, 600, 600, 600, 600], reload_ms=2800)
    assert out["proceed"] is False
    assert out["reason"] == "reload_cost_approaches_baseline"


def test_gate_does_not_fire_server_dominant_when_the_device_holds_it():
    """The tautology this metric replaced. Condition 3 used to key on
    total_lead_ms, which is approximately the whole skip-to-audible by
    construction -- so the ratio sat near 1.0 and the condition fired on every
    run regardless of the truth. Rig-shaped numbers: ~11.9s delay of which only
    ~2s is ours. The honest verdict is that the DEVICE holds it."""
    baseline = [11_882.0, 11_718.0, 12_016.0, 11_890.0, 11_796.0]
    server_leads = [2_000, 1_950, 2_100, 2_050, 1_900]
    out = sp.gate(baseline, server_leads, reload_ms=600)
    assert out["reason"] != "server_held_dominant"
    assert out["split"]["server_held_fraction"] < 0.25


def test_gate_split_reports_the_server_share_not_the_total():
    out = sp.gate([11_882.0] * 5, [2_000] * 5, reload_ms=None)
    assert out["split"]["server_lead_median_ms"] == 2_000
    # The total would have been ~11.8s; keying on it is what broke this.
    assert out["split"]["server_held_fraction"] == pytest.approx(0.168, abs=0.01)


def test_split_at_dispatch_separates_the_two_shares():
    snap = _flow("t-old", lead=11_000)["flow"]
    out = sp.split_at_dispatch(snap)
    assert out["server_lead_ms"] == 2_000
    assert out["device_lag_ms"] == 9_000


def test_gate_halts_when_the_server_holds_most_of_the_delay():
    """Condition 3 — the one the plan originally lacked. If the lead we own is
    most of the delay, the cheap levers become the primary fix."""
    out = sp.gate(_clear_samples(), [2600, 2700, 2650, 2800, 2550],
                  reload_ms=500)
    assert out["proceed"] is False
    assert out["reason"] == "server_held_dominant"


def test_gate_halts_when_no_dispatch_carried_a_device_clock():
    out = sp.gate(_clear_samples(),
                  [sp.NO_DEVICE_TIME] * 5, reload_ms=500)
    assert out["proceed"] is False
    assert out["reason"] == "lead_unmeasured"


# ── watch_for_audible / one_skip (injected I/O) ──────────────────────────────


class _Clock:
    """Advances once per call. The poll loop reads the clock twice per sample
    (the deadline check and the row stamp), so the inter-row delta the probe
    measures is 2 x step -- which is the point: the ACHIEVED interval is
    derived from the rows, never from the requested one."""

    def __init__(self, step=0.05):
        self.t = 0.0
        self.step = step

    def __call__(self):
        self.t += self.step
        return self.t


def test_watch_refuses_when_the_achieved_interval_is_too_coarse():
    """The REQUESTED interval is not evidence. A sampler that ran at 1 Hz
    cannot support a claim about a sub-second effect."""
    deps = FakeDeps([_flow("t-old")])
    clock = _Clock(step=1.0)  # 1000 ms per sample
    with pytest.raises(ProbeRefusal, match="achieved sample interval"):
        sp.watch_for_audible(deps, 0, "t-new", timeout_s=20.0,
                             sleep=lambda _s: None, clock=clock)


def test_device_cadence_is_read_from_age_drops_not_value_changes():
    """device_stream_ms is extrapolated now, so it advances every sample
    regardless of whether the device said anything -- a transition check on it
    could never fail. The age field drops only when a real status lands, and
    the gap between drops is the actual cadence."""
    rows = [
        _row(0, "t-old", age=50),      # fresh status
        _row(100, "t-old", age=150),
        _row(200, "t-old", age=250),
        _row(300, "t-old", age=50),    # drop -> status at t=300
        _row(400, "t-old", age=150),
        _row(500, "t-old", age=250),
        _row(600, "t-old", age=50),    # drop -> status at t=600
    ]
    assert sp.achieved_device_interval_ms(rows) == 300.0


def test_device_cadence_is_none_when_no_status_ever_lands():
    """A monotonically climbing age means nothing refreshed the clock all
    window. Measured on the rig: the age reached 89,708 ms with no drop."""
    rows = [_row(t, "t-old", age=100 + t) for t in (0, 100, 200, 300, 400)]
    assert sp.achieved_device_interval_ms(rows) is None


def test_watch_refuses_when_the_device_clock_is_effectively_frozen():
    """The defect this guard exists for. Polling fast against the production
    5 s status cadence produced six skips at 15.9 s +/- 0.2 s while the
    buffered lead varied 22-40 s -- a figure quantised to the device cadence,
    reported alongside a healthy-looking 219 ms achieved interval."""
    deps = FakeDeps([_flow("t-old")], freeze_device=True)
    with pytest.raises(ProbeRefusal,
                       match="device-clock updates|device clock advanced"):
        sp.watch_for_audible(deps, 0, "t-new", timeout_s=4.0,
                             sleep=lambda _s: None, clock=_Clock(step=0.05))


def test_one_skip_refuses_when_cast_degraded_to_per_track():
    deps = FakeDeps([_flow("t-old", active=False)])
    with pytest.raises(ProbeRefusal, match="gapless_flow_active"):
        sp.one_skip(deps, sleep=lambda _s: None, clock=_Clock())


def test_a_paused_instance_cannot_satisfy_the_trigger():
    """Verified against the rig: /api/now-playing reports is_playing=True AND
    is_paused=True while paused, with the position frozen. Keying on
    is_playing alone would call a paused speaker "audible" -- which is exactly
    the class of error this probe exists to avoid."""
    deps = FakeDeps([_flow("t-new")],
                    now_playing={"is_playing": True, "is_paused": True,
                                 "track_id": "t-new"})
    row = sp.flow_row(deps, clock=lambda: 1.0)
    assert row["playing"] is False
    assert sp.skip_to_audible_ms([row], 0, "t-new") == sp.UNREACHED


def test_flow_row_reads_the_flat_track_id():
    deps = FakeDeps([_flow("t-old")])
    row = sp.flow_row(deps, clock=lambda: 1.0)
    assert row["np_track_id"] == "t-new"
    # And it is recorded only to expose the UI/device disagreement -- the
    # trigger uses heard_track_id, which still says the OLD track here.
    assert row["heard_track_id"] == "t-old"


def test_one_skip_refuses_on_an_empty_queue_front():
    deps = FakeDeps([_flow("t-old")], queue_front=None)
    with pytest.raises(ProbeRefusal, match="queue front is empty"):
        sp.one_skip(deps, sleep=lambda _s: None, clock=_Clock())


def test_queue_front_is_read_from_track_id_not_id():
    """Regression: queue rows carry `track_id`. Reading `id` returned None on a
    full queue, and every skip refused with "queue front is empty"."""
    deps = FakeDeps([_flow("t-old")] * 9 + [_flow("t-new")],
                    queue_front="t-new")
    out = sp.one_skip(deps, sleep=lambda _s: None, clock=_Clock(step=0.05))
    assert out["requested_track_id"] == "t-new"


def test_one_skip_measures_a_non_trivial_latency_end_to_end():
    """Characterization: against a timeline where the device lags, the probe
    reports the lag rather than the UI's instant agreement."""
    states = [_flow("t-old")] * 9 + [_flow("t-new")]
    deps = FakeDeps(states)
    out = sp.one_skip(deps, sleep=lambda _s: None, clock=_Clock(step=0.05))
    assert out["skip_to_audible_ms"] > 0
    assert out["lead_at_dispatch_ms"] == 4200
    assert out["session_at_dispatch"] == "ab12"
    assert out["out_buffer"]["ms_conversion_valid"] is False


def test_one_skip_records_the_achieved_interval_beside_the_figure():
    """The figure is MEASURED from the timeline, not echoed from the request.
    The step is deliberately chosen so 2 x step differs from
    POLL_INTERVAL_S -- a probe that reported the requested cadence would pass
    an equality against 100 ms and hide a sampler running at a third of it."""
    states = [_flow("t-old")] * 9 + [_flow("t-new")]
    deps = FakeDeps(states)
    out = sp.one_skip(deps, sleep=lambda _s: None, clock=_Clock(step=0.03))
    assert out["achieved_interval_ms"] == pytest.approx(60.0, abs=1.0)
    assert out["achieved_interval_ms"] != sp.POLL_INTERVAL_S * 1000


# ── credential hygiene ───────────────────────────────────────────────────────


def test_probe_never_emits_a_full_flow_session_id():
    """The flow session id IS the stream route's bearer credential — that route
    carries no is_authorized_stream_key check by design. The diagnostics
    payload carries a truncated handle, and the probe must not widen it."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "tools", "soak",
                            "skip_probe.py"), encoding="utf-8").read()
    assert "session_id" not in src
    states = [_flow("t-old")] * 9 + [_flow("t-new")]
    out = sp.one_skip(FakeDeps(states), sleep=lambda _s: None,
                      clock=_Clock(step=0.05))
    assert len(out["session_at_dispatch"]) <= 4
