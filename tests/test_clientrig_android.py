"""Unit tests for the Android cable lane's pure parts.

This lane is the only one in the rig that measures the stated definition of
responsiveness literally rather than by proxy: input injected by the operating
system above the browser, and frame-presentation timestamps read out of the
display pipeline. Everything else approximates one or both.

What is tested here is everything that does not need a phone: parsing the
frame-statistics dumps, deciding whether a device is usable, and building the
input commands. The lane's *credential* -- being observed failing against the
recorded 40-pixel defect -- needs the device attached and is recorded then.
Until that happens the lane is formally uncredentialed, and the honesty core
refuses to let an uncredentialed lane report anything.

The parsing tests carry more weight than they look. `dumpsys SurfaceFlinger
--latency` marks frames it has no data for with the maximum 64-bit integer, and
sometimes with zeros. Treating either as a timestamp produces a duration of
roughly 292 years or a frame presented at the epoch, and averaging those into a
settle time would yield a confident, wrong, and very stable-looking number.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_INT64_MAX = 9223372036854775807


def _load(name, relpath):
    """Load a rig module by path, registering it so dataclasses can resolve.

    See the note in `tests/test_clientrig_contract.py`.
    """
    spec = importlib.util.spec_from_file_location(name, _ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


ad = _load("clientrig_adb", "tools/clientrig/adb.py")
c = _load("clientrig_contract", "tools/clientrig/contract.py")

_REFRESH = 16_666_667  # 60Hz in nanoseconds, as SurfaceFlinger reports it


def sf_dump(rows, refresh=_REFRESH):
    """Build a SurfaceFlinger latency dump: refresh period, then one row each."""
    lines = [str(refresh)]
    for desired, actual, ready in rows:
        lines.append(f"{desired}\t{actual}\t{ready}")
    return "\n".join(lines) + "\n"


def steady(n, start=1_000_000_000, refresh=_REFRESH):
    """n frames presented one refresh period apart."""
    return [(start + i * refresh, start + i * refresh + 2_000_000,
             start + i * refresh + 4_000_000) for i in range(n)]


# ── SurfaceFlinger latency parsing ──────────────────────────────────────────

def test_the_refresh_period_is_read_from_the_header():
    out = ad.parse_sf_latency(sf_dump(steady(3)))
    assert out.refresh_period_ns == _REFRESH


def test_presented_frames_are_parsed_in_order():
    out = ad.parse_sf_latency(sf_dump(steady(5)))
    assert len(out.frames) == 5
    assert out.present_ns == sorted(out.present_ns)


def test_pending_frames_marked_with_max_int64_are_discarded():
    """SurfaceFlinger uses INT64_MAX for a frame it has no data for. Read as a
    timestamp that is roughly 292 years, and it would dominate any average."""
    rows = steady(3) + [(_INT64_MAX, _INT64_MAX, _INT64_MAX)]
    out = ad.parse_sf_latency(sf_dump(rows))
    assert len(out.frames) == 3
    assert all(p < _INT64_MAX for p in out.present_ns)


def test_zero_rows_are_discarded_rather_than_treated_as_the_epoch():
    rows = [(0, 0, 0)] + steady(3)
    out = ad.parse_sf_latency(sf_dump(rows))
    assert len(out.frames) == 3


def test_a_dump_with_no_usable_rows_reports_why_rather_than_zero_frames():
    out = ad.parse_sf_latency(sf_dump([(0, 0, 0), (_INT64_MAX, _INT64_MAX, _INT64_MAX)]))
    assert out.frames == ()
    assert out.reason
    assert "no usable" in out.reason.lower() or "pending" in out.reason.lower()


def test_a_malformed_dump_is_reported_not_guessed_at():
    out = ad.parse_sf_latency("not a dump at all\n")
    assert out.frames == ()
    assert out.reason


def test_an_empty_dump_is_reported():
    out = ad.parse_sf_latency("")
    assert out.frames == ()
    assert out.reason


def test_a_row_with_the_wrong_column_count_is_skipped_not_misread():
    text = f"{_REFRESH}\n1000\t2000\n3000\t4000\t5000\n"
    out = ad.parse_sf_latency(text)
    assert len(out.frames) == 1


# ── intervals and quiescence ────────────────────────────────────────────────

def test_intervals_between_presented_frames_are_computed_in_ms():
    out = ad.parse_sf_latency(sf_dump(steady(4)))
    gaps = ad.present_intervals_ms(out)
    assert len(gaps) == 3
    assert all(abs(g - 16.667) < 0.01 for g in gaps)


def test_intervals_of_a_single_frame_are_empty_not_zero():
    out = ad.parse_sf_latency(sf_dump(steady(1)))
    assert ad.present_intervals_ms(out) == []


def test_quiescence_is_measured_to_the_last_frame_before_a_real_gap():
    """The screen stopped moving when the compositor stopped presenting."""
    start = 1_000_000_000
    frames = steady(6, start=start)
    # then a long idle gap, then unrelated later activity
    late = steady(2, start=start + 60 * _REFRESH)
    out = ad.parse_sf_latency(sf_dump(frames + late))
    q = ad.quiescence_ms(out, input_ns=start, idle_gap_frames=3)
    expected = (frames[-1][1] - start) / 1e6
    assert abs(q - expected) < 0.01


def test_quiescence_with_no_gap_reports_the_series_was_never_still():
    """A screen still painting when the dump ended has not settled, and
    reporting the last frame as a settle time would understate it."""
    out = ad.parse_sf_latency(sf_dump(steady(40)))
    q = ad.quiescence_ms(out, input_ns=1_000_000_000, idle_gap_frames=3)
    assert q == c.UNREACHED


def test_quiescence_before_any_frame_is_a_sentinel_not_zero():
    out = ad.parse_sf_latency(sf_dump(steady(4)))
    q = ad.quiescence_ms(out, input_ns=9_000_000_000, idle_gap_frames=3)
    assert q == c.NO_READING


def test_quiescence_of_an_unusable_dump_is_a_sentinel():
    out = ad.parse_sf_latency("")
    assert ad.quiescence_ms(out, input_ns=0, idle_gap_frames=3) == c.NO_READING


# ── device enumeration and usability ────────────────────────────────────────

def test_an_attached_device_is_parsed():
    out = ad.parse_devices("List of devices attached\nABC123\tdevice\n\n")
    assert out == [("ABC123", "device")]


def test_no_devices_is_an_empty_list_not_an_error():
    assert ad.parse_devices("List of devices attached\n\n") == []


def test_several_devices_are_all_parsed():
    out = ad.parse_devices(
        "List of devices attached\nABC123\tdevice\nDEF456\tunauthorized\n")
    assert len(out) == 2


def test_no_device_yields_the_no_device_sentinel_with_a_reason():
    code, why = ad.device_check([])
    assert code == c.NO_DEVICE
    assert why


def test_an_unauthorised_device_says_to_accept_the_prompt():
    code, why = ad.device_check([("ABC123", "unauthorized")])
    assert code == c.NO_DEVICE
    assert "authoris" in why.lower() or "authoriz" in why.lower() or "prompt" in why.lower()


def test_an_offline_device_is_refused_with_its_state_named():
    code, why = ad.device_check([("ABC123", "offline")])
    assert code == c.NO_DEVICE
    assert "offline" in why


def test_a_single_usable_device_passes():
    assert ad.device_check([("ABC123", "device")]) == (None, "")


def test_two_usable_devices_without_a_chosen_serial_is_refused():
    """Guessing which phone to measure would silently attribute a result to the
    wrong hardware, and every result is stamped with its device profile."""
    code, why = ad.device_check([("ABC123", "device"), ("DEF456", "device")])
    assert code == c.NO_DEVICE
    assert "serial" in why.lower()


def test_two_usable_devices_with_a_chosen_serial_passes():
    assert ad.device_check([("ABC123", "device"), ("DEF456", "device")],
                           serial="ABC123") == (None, "")


def test_a_chosen_serial_that_is_not_attached_is_refused():
    code, why = ad.device_check([("ABC123", "device")], serial="NOPE")
    assert code == c.NO_DEVICE
    assert "NOPE" in why


# ── input: injected by the OS, never by the page ────────────────────────────

def test_a_tap_is_an_os_level_input_command():
    cmd = ad.tap_argv(serial="ABC123", x=412, y=733)
    assert cmd[:3] == ["adb", "-s", "ABC123"]
    assert "input" in cmd and "tap" in cmd
    assert "412" in cmd and "733" in cmd


def test_a_swipe_carries_its_duration_so_the_gesture_is_frame_paced():
    cmd = ad.swipe_argv(serial="ABC123", x1=400, y1=900, x2=400, y2=300,
                        duration_ms=250)
    assert "swipe" in cmd
    assert "250" in cmd


def test_input_commands_never_evaluate_script_in_the_page():
    """The whole point of this lane. A scripted click produces no interaction
    timing entry at all and skips hit testing and main-thread queueing."""
    for cmd in (ad.tap_argv(serial="A", x=1, y=2),
                ad.swipe_argv(serial="A", x1=1, y1=2, x2=3, y2=4,
                              duration_ms=100)):
        joined = " ".join(cmd)
        assert "evaluate" not in joined
        assert "Runtime." not in joined
        assert "click()" not in joined


def test_the_input_kind_this_lane_reports_is_accepted_by_the_honesty_core():
    h = _load("clientrig_health2", "tools/clientrig/health.py")
    assert h.synthetic_input_health(ad.INPUT_KIND) is None


# ── the sentinels must not drift between modules ───────────────────────────

def test_adbs_sentinels_agree_with_the_measurement_contract():
    """`adb.py` restates the sentinel values rather than importing them, because
    these modules are loaded by file path and a relative import would fail with
    no parent package.

    Restating means they can drift, and a drift would be silent: the lane would
    return -2 for a missing device while the renderer expected -2 to mean
    something else, and every sentinel would be misattributed. This test is the
    invariant that makes the duplication safe.
    """
    assert ad.UNREACHED == c.UNREACHED
    assert ad.NO_DEVICE == c.NO_DEVICE
    assert ad.NO_READING == c.NO_READING


# ── no baked-in target ──────────────────────────────────────────────────────

def test_the_module_hardcodes_no_address_or_serial():
    """No host and no device identity may be baked in.

    Checked by SHAPE rather than by searching for this installation's actual
    values: embedding them would publish a fragment of the very thing the guard
    protects, and a probe hunting one known prefix would miss any other real
    serial that got committed.
    """
    import re
    text = (_ROOT / "tools" / "clientrig" / "adb.py").read_text(encoding="utf-8")

    private_ip = re.compile(
        r"\b(?:192\.168|10)\.\d{1,3}\.\d{1,3}(?:\.\d{1,3})?\b")
    assert not private_ip.search(text), (
        f"a private network address is baked into adb.py: "
        f"{private_ip.search(text).group(0)!r}")

    local_host = re.compile(r"\b[a-z0-9-]+\.(?:lan|local|home|internal)\b", re.I)
    assert not local_host.search(text), (
        f"a local hostname is baked into adb.py: "
        f"{local_host.search(text).group(0)!r}")

    # An Android serial is a longish run of upper-case letters and digits
    # containing both. Device serials must come from `adb devices` at run time,
    # never from source.
    serial = re.compile(r"\b(?=[A-Z0-9]{10,20}\b)(?=[^\s]*\d)[A-Z0-9]+\b")
    hits = [h for h in serial.findall(text) if not h.isdigit()]
    assert not hits, f"something serial-shaped is baked into adb.py: {hits[:2]}"


# ── the lane: declarations and preconditions, provable without hardware ─────

lane = _load("clientrig_android_lane", "tools/clientrig/lanes/android_cable.py")
h = _load("clientrig_health3", "tools/clientrig/health.py")


def test_the_lane_starts_uncredentialed_and_cannot_report():
    """A lane that has never been observed failing is unproven, not quiet."""
    cred = h.Credential(**lane.credential_template())
    why = h.credential_health(cred, declared_blind=lane.BLIND_CLASSES)
    assert why and "uncredentialed" in why.lower()


def test_the_lane_never_owns_a_class_it_is_blind_to():
    cred = h.Credential(**lane.credential_template())
    assert h.ownership_health(cred) is None


def test_the_lane_declares_blindness_to_ios_because_no_apple_hardware_exists():
    assert "ios-behaviour" in lane.BLIND_CLASSES


def test_the_lane_owns_the_recorded_defect_it_is_credentialed_against():
    cred = lane.credential_template()
    assert cred["recorded_defect"] == "rail-jump-40px"
    assert "jump-landing" in cred["owns_classes"]


def test_a_mobile_viewport_plan_taps_albums_before_measuring():
    """Below the breakpoint the guest page opens on Now, so a lane that skips
    this measures an empty surface and reports no rail -- which reads as a very
    fast rail."""
    steps = lane.navigation_plan(viewport_width_px=443)
    assert "tap-albums-tab" in steps
    assert steps.index("tap-albums-tab") < steps.index("wait-for-rail-visible")


def test_a_desktop_viewport_plan_does_not_tap_albums():
    assert "tap-albums-tab" not in lane.navigation_plan(viewport_width_px=1400)


def test_preflight_refuses_with_a_reason_when_no_device_is_attached():
    code, why = lane.preflight([], None, ad.device_check)
    assert code == c.NO_DEVICE
    assert lane.LANE_NAME in why


def test_preflight_passes_with_one_usable_device():
    assert lane.preflight([("ABC123", "device")], None,
                          ad.device_check) == (None, "")
