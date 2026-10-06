"""Unit tests for the soak arm's policy decisions.

The harness itself is not unit-tested (see tests/test_soak_harness_hygiene.py) —
it drives a live deployment. But "did the arm notice its own mode silently
reverted" is a decision, not I/O, and finding out it was wrong after six
unattended hours is the expensive way to learn it.
"""
import importlib.util
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "tools" / "soak" / "arm.py"
_spec = importlib.util.spec_from_file_location("soak_arm", _SRC)
arm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(arm)


class FakeDeps:
    """Records what was asked of the instance and replays canned answers."""

    def __init__(self, settings=None, output=None, scan=None, version=None,
                 restart_raises=False):
        self._settings = settings or {"gapless_enabled": True,
                                      "queue_end_behavior": "full_random"}
        self._output = output or {"backend_type": "direct", "device_id": "default"}
        self._scan = scan if scan is not None else {"refresh_failed": False}
        self._version = version or {"git_sha": "abc1234"}
        self._restart_raises = restart_raises
        self.rig_commands = []
        self.restarts = 0
        self.logins = 0

    def set_output(self, b, d):
        self._output = {"backend_type": b, "device_id": d}

    def get_output(self):
        return self._output

    def set_settings(self, **kw):
        self._settings.update(kw)

    def get_settings(self):
        return self._settings

    def scan_status(self):
        return self._scan

    def version(self):
        return self._version

    def login(self):
        self.logins += 1
        return True

    def rig(self, cmd):
        self.rig_commands.append(cmd)
        return ""

    def restart_container(self, name="jukeplox"):
        if self._restart_raises:
            raise RuntimeError("ssh down")
        self.restarts += 1
        return arm.Deps.restart_container(self, name)


# ── mode assertion ────────────────────────────────────────────────────────────

def test_matching_mode_passes():
    assert arm.check_mode(True, {"gapless_enabled": True}) is None
    assert arm.check_mode(False, {"gapless_enabled": False}) is None


def test_a_reverted_mode_is_caught():
    """An arm that silently flips produces confident data about the wrong thing —
    worse than an arm that fails."""
    why = arm.check_mode(True, {"gapless_enabled": False})
    assert why and "gapless=True" in why


def test_a_missing_mode_field_is_not_treated_as_a_match():
    assert arm.check_mode(True, {}) is not None


# ── output pinning ────────────────────────────────────────────────────────────

def test_pinned_output_passes_and_drift_is_caught():
    assert arm.check_output("direct", {"backend_type": "direct"}) is None
    why = arm.check_output("direct", {"backend_type": "chromecast"})
    assert why and "chromecast" in why


# ── health ────────────────────────────────────────────────────────────────────

def test_healthy_instance_passes():
    assert arm.check_health({"refresh_failed": False}) is None


def test_refresh_failed_blocks_the_arm():
    """A SIGKILL restart once left the DB locked while search kept answering from
    cache. Nothing looked broken and the rest of the run was degraded."""
    assert arm.check_health({"refresh_failed": True}) is not None


def test_unavailable_scan_status_is_not_healthy():
    assert arm.check_health(None) is not None


# ── recovery budget ───────────────────────────────────────────────────────────

def test_recovery_is_allowed_within_budget():
    assert arm.should_continue(0, max_recoveries=3)
    assert arm.should_continue(2, max_recoveries=3)


def test_recovery_stops_at_the_budget():
    """Recover-and-continue, but a permanently broken instance must not spend the
    whole arm restarting."""
    assert not arm.should_continue(3, max_recoveries=3)


# ── capture actually started ──────────────────────────────────────────────────

def test_capture_is_only_started_when_a_segment_exists():
    """Launching is not starting. A four-minute capture once produced no audio
    while the sampler recorded 141 samples beside it."""
    assert arm.capture_started(lambda: ["cap-1.flac"])
    assert not arm.capture_started(lambda: [])
    assert not arm.capture_started(lambda: None)


def test_capture_check_survives_a_failing_lister():
    def boom():
        raise OSError("ssh died")
    assert not arm.capture_started(boom)


# ── prepare ───────────────────────────────────────────────────────────────────

def test_prepare_sets_and_verifies_everything():
    d = FakeDeps(settings={"gapless_enabled": False, "queue_end_behavior": "stop"})
    r = arm.prepare_arm(d, gapless=True, backend="direct", device="default",
                        queue_end_behavior="full_random", result=arm.ArmResult("on"))
    assert r.incidents == []
    assert d.get_settings()["gapless_enabled"] is True
    assert d.get_settings()["queue_end_behavior"] == "full_random"
    assert r.build == "abc1234"


def test_prepare_flags_a_setting_that_did_not_take():
    """A write that returns 200 and does not take is the failure mode that
    produces a confidently-wrong arm."""
    class Stubborn(FakeDeps):
        def set_settings(self, **kw):
            pass  # accepts, changes nothing

    d = Stubborn(settings={"gapless_enabled": False, "queue_end_behavior": "stop"})
    r = arm.prepare_arm(d, gapless=True, backend="direct", device="default",
                        queue_end_behavior="full_random", result=arm.ArmResult("on"))
    kinds = {i["kind"] for i in r.incidents}
    assert "mode_mismatch" in kinds
    assert "queue_end_not_set" in kinds


def test_prepare_records_the_build_so_the_arm_is_attributable():
    d = FakeDeps(version={"git_sha": "deadbee"})
    r = arm.prepare_arm(d, True, "direct", "default", "full_random",
                        arm.ArmResult("on"))
    assert r.build == "deadbee"


# ── recovery ──────────────────────────────────────────────────────────────────

def test_recovery_restarts_gracefully_never_forcibly():
    """`docker rm -f` is what left the database locked."""
    d = FakeDeps()
    r = arm.ArmResult("on")
    assert arm.recover(d, True, "direct", "default", r) is True
    cmd = d.rig_commands[-1]
    assert "docker stop -t" in cmd
    assert "rm -f" not in cmd


def test_restart_works_on_a_container_that_is_already_stopped():
    """The rehearsal's third finding. `docker stop $(docker ps -qf ...) && start`
    works only while the container is RUNNING — after a crash `docker ps -q` is
    empty, the stop fails on a missing argument and the `&&` swallows the start.
    It restarted healthy containers faultlessly and was useless in the one case
    it exists for."""
    d = FakeDeps()
    arm.recover(d, True, "direct", "default", arm.ArmResult("on"))
    cmd = d.rig_commands[-1]
    assert "$(docker ps" not in cmd, "must not depend on the container running"
    assert "&&" not in cmd, "a failed stop must not swallow the start"
    assert "docker start" in cmd


def test_recovery_stops_when_the_instance_is_unhealthy_afterwards():
    d = FakeDeps(scan={"refresh_failed": True})
    r = arm.ArmResult("on")
    assert arm.recover(d, True, "direct", "default", r) is False
    assert any(i["kind"] == "unhealthy_after_restart" for i in r.incidents)


def test_recovery_stops_when_the_mode_did_not_survive():
    """Covers AE3."""
    d = FakeDeps(settings={"gapless_enabled": False})
    r = arm.ArmResult("on")
    assert arm.recover(d, True, "direct", "default", r) is False
    assert any(i["kind"] == "mode_lost_on_restart" for i in r.incidents)


def test_recovery_stops_when_the_output_did_not_survive():
    d = FakeDeps(output={"backend_type": "chromecast"})
    r = arm.ArmResult("on")
    assert arm.recover(d, True, "direct", "default", r) is False
    assert any(i["kind"] == "output_lost_on_restart" for i in r.incidents)


def test_a_failed_restart_is_recorded_not_swallowed():
    d = FakeDeps(restart_raises=True)
    r = arm.ArmResult("on")
    assert arm.recover(d, True, "direct", "default", r) is False
    assert any(i["kind"] == "restart_failed" for i in r.incidents)


def test_repeated_recoveries_are_counted_individually():
    """Covers AE2 — the arm keeps going, and each failure is its own record."""
    d = FakeDeps()
    r = arm.ArmResult("on")
    for _ in range(3):
        assert arm.recover(d, True, "direct", "default", r) is True
    assert r.recoveries == 3
    assert d.restarts == 3


def test_recovery_refuses_past_the_budget():
    d = FakeDeps()
    r = arm.ArmResult("on")
    r.recoveries = arm.MAX_RECOVERIES
    assert arm.recover(d, True, "direct", "default", r) is False
    assert any(i["kind"] == "recovery_budget_exhausted" for i in r.incidents)
    assert d.restarts == 0


def test_result_serialises_for_the_verdict():
    r = arm.ArmResult("gapless")
    r.incident("mode_mismatch", "because")
    r.build = "abc1234"
    d = r.as_dict()
    assert d["arm"] == "gapless" and d["build"] == "abc1234"
    assert d["incidents"][0]["kind"] == "mode_mismatch"
    assert "t" in d["incidents"][0]


# ── disk headroom, re-checked mid-run ─────────────────────────────────────────

def test_ample_disk_passes():
    assert arm.disk_headroom_ok(free_mb=39_000, hours_remaining=6) is None


def test_insufficient_disk_for_the_remainder_is_caught():
    """record.sh pre-flights once then execs ffmpeg and cannot check again. An
    arm that fills the disk at hour five loses five hours of recording, and the
    failure arrives as a corrupt segment rather than a message."""
    why = arm.disk_headroom_ok(free_mb=500, hours_remaining=6)
    assert why and "needed" in why


def test_no_time_remaining_needs_no_headroom():
    assert arm.disk_headroom_ok(free_mb=0, hours_remaining=0) is None
    assert arm.disk_headroom_ok(free_mb=0, hours_remaining=-1) is None


def test_headroom_scales_with_the_measured_rate():
    # 342 MB/h measured; at 400 MB/h with 2h left we need ~800 MB.
    assert arm.disk_headroom_ok(900, 2, mb_per_hour=400) is None
    assert arm.disk_headroom_ok(700, 2, mb_per_hour=400) is not None


# ── the arm loop ──────────────────────────────────────────────────────────────

class LoopDeps(FakeDeps):
    """FakeDeps plus capture control and a scriptable liveness sequence."""

    def __init__(self, segments=("cap-1.flac",), liveness_fails_at=None,
                 revert_mode_on_restart=False, **kw):
        super().__init__(**kw)
        self._segments = list(segments)
        self._liveness_fails_at = set(liveness_fails_at or [])
        self._revert = revert_mode_on_restart
        self.scan_calls = 0
        self.captures_started = 0
        self.samplers_started = 0
        self.captures_stopped = 0

    def scan_status(self):
        self.scan_calls += 1
        if self.scan_calls in self._liveness_fails_at:
            raise OSError("connection refused")
        return self._scan

    def restart_container(self, name="jukeplox"):
        out = super().restart_container(name)
        if self._revert:
            # The hazard this models: a startup handoff that does not fire, so
            # the instance comes back on its default rather than the arm's mode.
            self._settings["gapless_enabled"] = True
        return out

    def start_capture(self, cap_dir, minutes):
        self.captures_started += 1
        return ""

    def stop_capture(self):
        self.captures_stopped += 1
        return ""

    def start_sampler(self, cap_dir, minutes):
        self.samplers_started += 1
        return ""

    def list_segments(self, cap_dir):
        return self._segments


def test_an_arm_runs_to_completion():
    d = LoopDeps()
    r = arm.run_arm(d, "gappy", gapless=False, minutes=0.02, cap_dir="/tmp/x",
                    poll_s=0.01)
    assert r.completed is True
    assert d.captures_started == 1 and d.samplers_started == 1
    assert d.captures_stopped >= 1


def test_an_arm_refuses_to_run_when_the_capture_never_started():
    """Launching is not starting. Six hours of state samples beside no audio is
    the failure this prevents."""
    d = LoopDeps(segments=[])
    r = arm.run_arm(d, "gappy", gapless=False, minutes=0.05, cap_dir="/tmp/x",
                    poll_s=0.01)
    assert r.completed is False
    assert any(i["kind"] == "capture_did_not_start" for i in r.incidents)
    assert d.samplers_started == 0, "no point sampling state with no audio"


def test_a_hard_failure_is_recovered_and_the_arm_still_finishes():
    """Covers AE2 — the user's chosen policy: record it, restart, carry on."""
    d = LoopDeps(liveness_fails_at=[2])
    r = arm.run_arm(d, "gappy", gapless=False, minutes=0.06, cap_dir="/tmp/x",
                    poll_s=0.01)
    assert any(i["kind"] == "hard_failure" for i in r.incidents)
    assert r.recoveries == 1
    assert r.completed is True


def test_capture_and_sampler_are_both_restarted_after_a_recovery():
    """A recorder outliving its sampler leaves audio nobody can classify — the
    two `unknown` silences observed on 2026-09-23 were exactly that."""
    d = LoopDeps(liveness_fails_at=[2])
    arm.run_arm(d, "gappy", gapless=False, minutes=0.06, cap_dir="/tmp/x",
                poll_s=0.01)
    assert d.captures_started == 2
    assert d.samplers_started == 2


def test_an_arm_gives_up_when_it_cannot_restore_its_own_state():
    """Continuing past this point produces confident data about something else."""
    d = LoopDeps(liveness_fails_at=[2], revert_mode_on_restart=True)
    r = arm.run_arm(d, "gappy", gapless=False, minutes=0.06, cap_dir="/tmp/x",
                    poll_s=0.01)
    assert r.completed is False
    assert any(i["kind"] == "mode_lost_on_restart" for i in r.incidents)


def test_liveness_reports_an_unreachable_instance():
    d = LoopDeps(liveness_fails_at=[1])
    assert arm.liveness(d) is not None
    assert arm.liveness(d) is None


def test_events_are_emitted_for_the_timeline():
    seen = []
    d = LoopDeps(liveness_fails_at=[2])
    arm.run_arm(d, "gappy", gapless=False, minutes=0.06, cap_dir="/tmp/x",
                poll_s=0.01, on_event=lambda k, v: seen.append(k))
    assert "arm_start" in seen and "capture_started" in seen
    assert "hard_failure" in seen and "recovered" in seen
    assert "arm_done" in seen


# ── defects the rehearsal found that the fakes did not ────────────────────────

def test_an_unreachable_instance_is_not_reported_as_healthy():
    """The rehearsal's first finding: a failed call became {}, {} has no
    refresh_failed, and an unreachable instance read as perfectly healthy."""
    assert arm.check_health({}) is not None
    assert arm.check_health({"something_else": 1}) is not None
    assert arm.check_health({"refresh_failed": False}) is None


def test_safe_reports_failure_as_none_not_as_an_empty_success():
    def boom():
        raise OSError("refused")
    assert arm._safe(boom) is None
    assert arm._safe(lambda: {"a": 1}) == {"a": 1}


class SlowStart(LoopDeps):
    """An instance that needs a few polls after restart before it answers."""

    def __init__(self, polls_before_ready=3, **kw):
        super().__init__(**kw)
        self._need = polls_before_ready
        self._restarted = False

    def restart_container(self, name="jukeplox"):
        self._restarted = True
        self.restarts += 1
        return ""

    def scan_status(self):
        if self._restarted and self._need > 0:
            self._need -= 1
            raise OSError("connection refused")
        return self._scan


def test_recovery_waits_for_the_app_rather_than_the_container(monkeypatch):
    """A container START is not an app READY. Without the wait the next call
    lands on a socket nothing is listening on yet, and recovery raises instead
    of recovering."""
    d = SlowStart(polls_before_ready=3)
    r = arm.ArmResult("gapless")
    # The fake's settings report gapless on, so the arm must ask for the same
    # thing or it fails the mode check for an unrelated reason.
    assert arm.recover(d, True, "direct", "default", r) is True
    assert r.recoveries == 1


def test_recovery_gives_up_when_the_app_never_comes_back(monkeypatch):
    # Pin the timeout rather than inheriting the real 120s one: a unit test that
    # takes two minutes is a unit test nobody runs.
    monkeypatch.setattr(arm, "READY_TIMEOUT_S", 0.3)
    d = SlowStart(polls_before_ready=10_000)
    d._restarted = True          # the fake only refuses once it has restarted
    r = arm.ArmResult("gapless")
    assert arm.wait_ready(d, timeout_s=0.3, poll_s=0.05) is False
    assert arm.recover(d, True, "direct", "default", r) is False
    assert any(i["kind"] == "never_came_back" for i in r.incidents)


def test_a_failed_relogin_is_recorded_but_not_fatal():
    class NoLogin(LoopDeps):
        def login(self):
            raise OSError("refused")

    d = NoLogin()
    r = arm.ArmResult("gappy")
    assert arm.recover(d, True, "direct", "default", r) is True
    assert any(i["kind"] == "relogin_failed" for i in r.incidents)
