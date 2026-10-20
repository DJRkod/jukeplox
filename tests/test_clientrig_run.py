"""Unit tests for the client-experience rig's multi-lane runner policy.

The runner owns the two things this repo has been burned by twice, so both are
pure functions with the I/O injected, provable here with no browser and no
device attached:

  * **Process teardown.** On 2026-06-10 roughly 120 Chrome processes were left
    behind; on 2026-06-24, 140 from leaked perf-benchmark runs. The sweep has to
    match on a signature the rig chose, spare everything it did not launch, and
    verify the residue rather than assuming the kill worked. It must also
    exclude its own process -- `pkill -f <pattern>` matching the invoking shell
    has destroyed measurements in this repo before.
  * **Settings restore.** On 2026-09-24 a harness arm left `queue_end_behavior`
    flipped and the rig auto-played roughly 3,000 tracks over nine days,
    contaminating play history. The process sweep that day was completely clean.
    A mutated setting has no PID to look for, so the only defence is an explicit
    snapshot unwound in a `finally` at runner level.

The restore test drives the runner with an injected failure, because a restore
path that has only ever been observed on the happy path is unverified.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _load(name, relpath):
    """Load a rig module by path, registering it so dataclasses can resolve.

    See the note in `tests/test_clientrig_contract.py`: without the sys.modules
    entry, `dataclasses` cannot resolve the string annotations produced by
    `from __future__ import annotations` and class creation fails at import.
    """
    spec = importlib.util.spec_from_file_location(name, _ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


r = _load("clientrig_run", "tools/clientrig/run.py")


class FakeDeps:
    """Everything the runner touches outside itself, recorded instead of done."""

    def __init__(self, settings=None, processes=None, fail_on_lane=None):
        self._settings = dict(settings or {})
        self.processes = list(processes or [])
        self.fail_on_lane = fail_on_lane
        self.writes = []
        self.killed = []
        self.lanes_run = []

    def get_settings(self):
        return dict(self._settings)

    def set_settings(self, **kw):
        self.writes.append(dict(kw))
        self._settings.update(kw)
        return dict(self._settings)

    def list_processes(self):
        return list(self.processes)

    def kill(self, pid):
        self.killed.append(pid)
        self.processes = [p for p in self.processes if p["pid"] != pid]

    def run_lane(self, lane):
        self.lanes_run.append(lane)
        if lane == self.fail_on_lane:
            raise RuntimeError("lane blew up mid-flight")
        return {"lane": lane, "ok": True}


def proc(pid, cmdline):
    return {"pid": pid, "cmdline": cmdline}


# ── the run tag ───────────────────────────────────────────────────────────────

def test_a_run_tag_is_unique_per_run():
    assert r.make_run_tag() != r.make_run_tag()


def test_a_run_tag_is_recognisable_as_this_rigs():
    assert r.make_run_tag().startswith("jp-clientrig-")


# ── stray detection: match our signature, spare everything else ─────────────

def test_a_browser_carrying_our_tag_is_found():
    tag = "jp-clientrig-123-abc"
    procs = [proc(900, f"chrome.exe --user-data-dir=C:\\Temp\\{tag} --remote-debugging-port=9311")]
    assert r.stray_pids(procs, tag, own_pid=1) == [900]


def test_the_users_own_chrome_is_never_touched():
    """The user's real browser can be ~30 processes. Matching on the process
    NAME rather than our signature would kill all of them."""
    tag = "jp-clientrig-123-abc"
    procs = [proc(p, "chrome.exe --type=renderer") for p in range(400, 430)]
    assert r.stray_pids(procs, tag, own_pid=1) == []


def test_another_rig_runs_browsers_are_spared():
    """Two concurrent runs must not reap each other."""
    procs = [proc(900, "chrome.exe --user-data-dir=/tmp/jp-clientrig-OTHER-xyz")]
    assert r.stray_pids(procs, "jp-clientrig-123-abc", own_pid=1) == []


def test_the_sweeps_own_process_is_excluded_even_when_it_matches():
    """The invoking shell's command line contains the tag it is searching for.
    `pkill -f` on that pattern has killed the measurement in this repo."""
    tag = "jp-clientrig-123-abc"
    procs = [proc(42, f"python run.py --tag {tag}"),
             proc(900, f"chrome.exe --user-data-dir=/tmp/{tag}")]
    assert r.stray_pids(procs, tag, own_pid=42) == [900]


def test_a_tagged_helper_process_tree_is_all_collected():
    tag = "jp-clientrig-123-abc"
    procs = [proc(900, f"chrome.exe --user-data-dir=/tmp/{tag}"),
             proc(901, f"chrome.exe --type=gpu-process --user-data-dir=/tmp/{tag}"),
             proc(902, f"chrome.exe --type=renderer --user-data-dir=/tmp/{tag}")]
    assert sorted(r.stray_pids(procs, tag, own_pid=1)) == [900, 901, 902]


# ── teardown verifies rather than assumes ───────────────────────────────────

def test_teardown_reports_clean_when_nothing_is_left():
    tag = "jp-clientrig-123-abc"
    deps = FakeDeps(processes=[proc(900, f"chrome.exe --user-data-dir=/tmp/{tag}")])
    outcome = r.teardown(deps, tag, own_pid=1)
    assert outcome.residue == 0
    assert outcome.ok
    assert 900 in deps.killed


def test_teardown_fails_loudly_when_residue_survives():
    """A warning is not enough -- the recorded incidents were both discovered by
    the user, not by the harness."""
    tag = "jp-clientrig-123-abc"

    class Stubborn(FakeDeps):
        def kill(self, pid):
            self.killed.append(pid)  # refuses to die

    deps = Stubborn(processes=[proc(900, f"chrome.exe --user-data-dir=/tmp/{tag}")])
    outcome = r.teardown(deps, tag, own_pid=1)
    assert not outcome.ok
    assert outcome.residue == 1
    assert tag in outcome.reason


def test_teardown_counts_residue_after_killing_not_before():
    tag = "jp-clientrig-123-abc"
    deps = FakeDeps(processes=[proc(900, f"chrome.exe --user-data-dir=/tmp/{tag}")])
    outcome = r.teardown(deps, tag, own_pid=1)
    assert outcome.residue == 0, (
        "counting before the kill would report every run as dirty")


# ── settings restore, including on the failure path ─────────────────────────

def test_settings_touched_by_a_lane_are_snapshotted_and_restored():
    deps = FakeDeps(settings={"rail_mode": "vanilla", "default_view": "list"})
    with r.settings_guard(deps, ("rail_mode", "default_view")):
        deps.set_settings(rail_mode="magnetic")
    assert deps.get_settings()["rail_mode"] == "vanilla"


def test_a_setting_the_lane_did_not_declare_is_left_alone():
    deps = FakeDeps(settings={"rail_mode": "vanilla", "queue_end_behavior": "stop"})
    with r.settings_guard(deps, ("rail_mode",)):
        deps.set_settings(rail_mode="magnetic")
    assert deps.get_settings()["queue_end_behavior"] == "stop"


def test_a_run_aborted_mid_flight_still_restores_every_mutated_setting():
    """The nine-day incident. An exception must not skip the unwind."""
    deps = FakeDeps(settings={"rail_mode": "vanilla", "default_view": "list"})
    with pytest.raises(RuntimeError):
        with r.settings_guard(deps, ("rail_mode", "default_view")):
            deps.set_settings(rail_mode="magnetic", default_view="tile")
            raise RuntimeError("lane blew up mid-flight")
    restored = deps.get_settings()
    assert restored["rail_mode"] == "vanilla"
    assert restored["default_view"] == "list"


def test_the_guard_restores_at_runner_level_once_not_per_lane():
    """Restoring per-arm would fight the next arm, which needs the mutation."""
    deps = FakeDeps(settings={"rail_mode": "vanilla"})
    with r.settings_guard(deps, ("rail_mode",)):
        deps.set_settings(rail_mode="magnetic")
        mid = deps.get_settings()["rail_mode"]
    assert mid == "magnetic", "the guard must not unwind between lanes"
    assert deps.get_settings()["rail_mode"] == "vanilla"


def test_restore_is_skipped_for_settings_that_were_never_changed():
    deps = FakeDeps(settings={"rail_mode": "vanilla"})
    with r.settings_guard(deps, ("rail_mode",)):
        pass
    assert deps.writes == [], "an unchanged setting needs no write to restore"


# ── the whole run: lanes ordered, teardown and restore both fire ───────────

def test_a_full_run_records_lane_order_on_every_result():
    deps = FakeDeps(settings={"rail_mode": "vanilla"})
    out = r.run_lanes(deps, ["chromium-desktop", "webkit-local"],
                      touches=("rail_mode",), own_pid=1)
    assert [x["lane"] for x in out.results] == ["chromium-desktop", "webkit-local"]
    assert out.lane_order == {"chromium-desktop": 0, "webkit-local": 1}, (
        "order is a variable -- pin it or randomise it, but always record it")


def test_a_failing_lane_does_not_prevent_restore_or_teardown():
    tag_proc = []
    deps = FakeDeps(settings={"rail_mode": "vanilla"},
                    processes=tag_proc, fail_on_lane="webkit-local")
    out = r.run_lanes(deps, ["chromium-desktop", "webkit-local"],
                      touches=("rail_mode",), own_pid=1)
    assert deps.get_settings()["rail_mode"] == "vanilla"
    assert out.teardown.ok
    assert any(x.get("error") for x in out.results)


def test_a_failing_lane_is_recorded_as_broken_not_as_a_pass():
    deps = FakeDeps(settings={"rail_mode": "vanilla"}, fail_on_lane="webkit-local")
    out = r.run_lanes(deps, ["webkit-local"], touches=("rail_mode",), own_pid=1)
    failed = [x for x in out.results if x.get("error")]
    assert len(failed) == 1
    assert "blew up" in failed[0]["error"]
    assert failed[0].get("ok") is not True
