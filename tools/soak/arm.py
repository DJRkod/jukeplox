"""Drive one soak arm end to end: set it up, hold it, survive a failure, hand over.

Runs on the driving machine. Everything comes from the environment — no host, no
credential is committed here.

The policy lives in pure functions at the top so it can be tested without a rig;
the I/O is injected as a `Deps` bundle. The soak harness is otherwise
deliberately not unit-tested (see tests/test_soak_harness_hygiene.py), but "did
the arm notice its own mode silently reverted" is exactly the kind of decision
worth pinning without a six-hour run to find out.

Three disciplines here are not stylistic — each is a failure this project has
already paid for:

**Graceful stop only.** A SIGKILL restart once left the database locked while
search kept answering from cache, so nothing looked broken and the rest of the
run was silently degraded.

**Health check after every recovery**, or the arm keeps running against a
degraded instance and produces confident data about a broken system.

**Re-assert the mode after every start.** The toggle is restored at startup so it
*should* survive, but a startup handoff that did not fire has bitten this project
before, and an arm that silently reverts produces confident data about the wrong
thing — worse than an arm that fails.
"""
import json
import os
import time
import urllib.error
import urllib.request

# Bounded tolerance for hard failures before the arm gives up. The user's policy
# is auto-recover and continue; this exists so a permanently broken instance
# cannot burn six hours restarting in a loop.
MAX_RECOVERIES = int(os.environ.get("JP_MAX_RECOVERIES", "10"))
# Graceful stop timeout. Never a forced kill — see the module docstring.
STOP_TIMEOUT_S = int(os.environ.get("JP_STOP_TIMEOUT_S", "30"))
# How long to wait for the app to answer again after a restart.
READY_TIMEOUT_S = int(os.environ.get("JP_READY_TIMEOUT_S", "120"))


# ── policy (pure) ─────────────────────────────────────────────────────────────

def check_mode(expected_gapless: bool, settings: dict) -> str | None:
    """None when the effective mode matches the arm's intent, else why not."""
    actual = settings.get("gapless_enabled")
    if actual is None:
        return "settings did not report gapless_enabled"
    if bool(actual) != bool(expected_gapless):
        return (f"arm wants gapless={expected_gapless} but the instance reports "
                f"{bool(actual)}")
    return None


def check_output(expected_backend: str, active: dict) -> str | None:
    """None when the pinned backend is the active one, else why not."""
    actual = active.get("backend_type")
    if actual != expected_backend:
        return f"arm pinned {expected_backend!r} but the active backend is {actual!r}"
    return None


def check_health(scan_status: dict) -> str | None:
    """None when the instance is healthy enough to keep running an arm.

    `refresh_failed` matters most right after a restart: a SIGKILL once left the
    DB locked while search kept answering from cache, so the instance looked fine
    and the rest of the run was quietly degraded.
    """
    if not isinstance(scan_status, dict) or not scan_status:
        return "scan status unavailable"
    if "refresh_failed" not in scan_status:
        # An empty-ish payload is "we could not ask", which is NOT health. The
        # rehearsal on 2026-09-23 found this: a failed call became {}, {} has no
        # refresh_failed, and an unreachable instance read as healthy.
        return "scan status did not report refresh_failed"
    if scan_status.get("refresh_failed"):
        return "instance reports refresh_failed"
    return None


def should_continue(recoveries: int, max_recoveries: int = MAX_RECOVERIES) -> bool:
    """Whether another recovery is allowed.

    The policy is recover-and-continue, but not without bound: a permanently
    broken instance would otherwise spend the whole arm restarting.
    """
    return recoveries < max_recoveries


def capture_started(list_segments) -> bool:
    """Whether the recorder actually produced a segment.

    Launching is not starting. ALSA capture is single-consumer, so a leftover
    recorder makes the next one fail at open — and backgrounded, that failure is
    silent. Observed on 2026-09-23: a four-minute capture produced no audio while
    the state sampler recorded 141 samples beside it. An arm must verify a
    segment exists rather than trusting that the process launched.
    """
    try:
        return len(list_segments() or []) > 0
    except Exception:
        return False


# ── I/O seam ──────────────────────────────────────────────────────────────────

class Deps:
    """Everything the runner touches outside itself, in one injectable bundle."""

    def __init__(self, base: str, admin_pw: str = "", rig_cmd=None):
        self.base = base.rstrip("/")
        self.admin_pw = admin_pw
        self._rig_cmd = rig_cmd
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor())

    # -- app control ----------------------------------------------------------
    def _req(self, path: str, payload=None, method=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            f"{self.base}{path}", data=data,
            headers={"Content-Type": "application/json"} if data else {},
            method=method or ("POST" if data is not None else "GET"))
        with self._opener.open(req, timeout=30) as r:
            body = r.read().decode("utf-8")
        return json.loads(body) if body.strip() else {}

    def login(self) -> bool:
        try:
            self._req("/admin/auth/login/local", {"password": self.admin_pw})
            return True
        except urllib.error.HTTPError:
            return False

    def get_settings(self) -> dict:
        return self._req("/admin/settings")

    def set_settings(self, **kw) -> dict:
        return self._req("/admin/settings", kw)

    def get_output(self) -> dict:
        return self._req("/admin/output/active")

    def set_output(self, backend_type: str, device_id: str) -> dict:
        return self._req("/admin/output/active",
                         {"backend_type": backend_type, "device_id": device_id})

    def scan_status(self) -> dict:
        return self._req("/api/scan-status")

    def version(self) -> dict:
        return self._req("/api/version")


    # -- rig-side capture control --------------------------------------------
    def start_capture(self, cap_dir: str, minutes: float) -> str:
        hours = max(1, int(minutes / 60) + 1)
        return self.rig(
            f"nohup sh -c 'JP_CAP_DIR={cap_dir} JP_CAP_HOURS={hours} "
            f"JP_SEGMENT_SEC=900 sh /root/soak/rig/record.sh' "
            f">/tmp/arm_rec.log 2>&1 & sleep 4; true")

    def stop_capture(self) -> str:
        # By process NAME, never `pkill -f` on a pattern the invoking shell's own
        # command line also matches — that kills the caller.
        return self.rig("pkill -x ffmpeg; true")

    def start_sampler(self, cap_dir: str, minutes: float) -> str:
        return self.rig(
            f"nohup sh -c 'JP_BASE=http://$(hostname -I | awk \"{{print \$1}}\") "
            f"JP_STATE_LOG={cap_dir}/state.jsonl JP_MINUTES={minutes} "
            f"python3 /root/soak/rig/state_sampler.py' "
            f">/tmp/arm_state.log 2>&1 & true")

    def list_segments(self, cap_dir: str) -> list:
        out = self.rig(f"ls {cap_dir}/*.flac 2>/dev/null || true")
        return [x for x in out.split() if x.strip()]

    # -- rig control ----------------------------------------------------------
    def rig(self, command: str) -> str:
        """Run a command on the rig. Injected so tests never need SSH."""
        if self._rig_cmd is None:
            raise RuntimeError("no rig command configured")
        return self._rig_cmd(command)

    def restart_container(self, name: str = "jukeplox") -> str:
        """Graceful stop then start. NEVER `rm -f`.

        Addressed BY NAME and sequenced with `;` rather than `&&`. The first
        version used `docker stop $(docker ps -qf ...) && docker start ...`,
        which works only while the container is running — after a crash
        `docker ps -q` is empty, the stop fails on a missing argument, and the
        `&&` means the start never runs. It restarted healthy containers
        faultlessly and was useless in the one case it exists for. Found by the
        rehearsal, not by any fake.
        """
        return self.rig(f"docker stop -t {STOP_TIMEOUT_S} {name} >/dev/null 2>&1; "
                        f"docker start {name}")


# ── the runner ────────────────────────────────────────────────────────────────

class ArmResult:
    def __init__(self, name: str):
        self.name = name
        self.incidents: list[dict] = []
        self.recoveries = 0
        self.completed = False
        self.build = None

    def incident(self, kind: str, detail: str) -> None:
        self.incidents.append({"t": int(time.time() * 1000),
                               "kind": kind, "detail": detail})

    def as_dict(self) -> dict:
        return {"arm": self.name, "completed": self.completed,
                "recoveries": self.recoveries, "build": self.build,
                "incidents": self.incidents}


def prepare_arm(deps: Deps, gapless: bool, backend: str, device: str,
                queue_end_behavior: str, result: ArmResult) -> ArmResult:
    """Put the instance into the arm's intended state and PROVE it took.

    Every setting is read back rather than assumed. A write that returns 200 and
    does not take is the failure mode that produces a confidently-wrong arm.
    """
    deps.set_output(backend, device)
    deps.set_settings(gapless_enabled=gapless,
                      queue_end_behavior=queue_end_behavior)

    why = check_output(backend, deps.get_output())
    if why:
        result.incident("output_not_pinned", why)

    settings = deps.get_settings()
    why = check_mode(gapless, settings)
    if why:
        result.incident("mode_mismatch", why)

    if settings.get("queue_end_behavior") != queue_end_behavior:
        # Without this the queue can drain and every silence afterwards is
        # "explained idle" rather than a measurement.
        result.incident(
            "queue_end_not_set",
            f"wanted {queue_end_behavior!r}, got "
            f"{settings.get('queue_end_behavior')!r}")

    try:
        result.build = (deps.version() or {}).get("git_sha")
    except Exception:
        result.incident("build_unknown", "could not read /api/version")
    return result


def recover(deps: Deps, gapless: bool, backend: str, device: str,
            result: ArmResult) -> bool:
    """Restart gracefully, verify health, and re-assert the arm's intent.

    Returns False when the arm should stop rather than continue on an instance
    that cannot be brought back to the state the arm is measuring.
    """
    if not should_continue(result.recoveries):
        result.incident("recovery_budget_exhausted",
                        f"{result.recoveries} recoveries already")
        return False

    result.recoveries += 1
    try:
        deps.restart_container()
    except Exception as exc:
        result.incident("restart_failed", type(exc).__name__)
        return False

    # A container START is not an app READY. Without this the next call lands on
    # a socket nothing is listening on yet and the recovery raises instead of
    # recovering — found by the rehearsal, invisible to every fake.
    if not wait_ready(deps):
        result.incident("never_came_back",
                        f"instance did not answer within {READY_TIMEOUT_S}s of restart")
        return False

    why = check_health(_safe(deps.scan_status))
    if why:
        # Continuing here is how a run silently produces degraded data.
        result.incident("unhealthy_after_restart", why)
        return False

    try:
        deps.login()
    except Exception:
        # A failed re-login is not fatal on its own — the checks below read
        # endpoints that may not need it, and they report their own failure.
        result.incident("relogin_failed", "could not re-authenticate after restart")
    why = check_mode(gapless, _safe(deps.get_settings) or {})
    if why:
        result.incident("mode_lost_on_restart", why)
        return False
    why = check_output(backend, _safe(deps.get_output) or {})
    if why:
        result.incident("output_lost_on_restart", why)
        return False
    return True


def wait_ready(deps, timeout_s: float = None, poll_s: float = 2.0) -> bool:
    """Poll until the instance answers again, or give up.

    Restarting a container returns as soon as the process is launched; the app
    behind it still has to start, open its socket and restore settings.
    """
    timeout_s = READY_TIMEOUT_S if timeout_s is None else timeout_s
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if liveness(deps) is None:
            return True
        time.sleep(poll_s)
    return False


def _safe(fn):
    """The call's result, or None when it FAILED.

    Deliberately not `{}` on failure: an empty dict reads as a successful call
    that returned nothing, and every caller here then treats "could not ask" as
    "nothing wrong".
    """
    try:
        return fn()
    except Exception:
        return None


def disk_headroom_ok(free_mb: int, hours_remaining: float,
                     mb_per_hour: int = 400) -> str | None:
    """None when the disk can still hold the rest of the arm, else why not.

    `record.sh` pre-flights the disk once, then execs ffmpeg and cannot check
    again — so the arm re-checks periodically. An arm that fills the disk at
    hour five loses the recording it spent five hours making, and the failure
    arrives as a corrupt final segment rather than a message.
    """
    if hours_remaining <= 0:
        return None
    need = int(hours_remaining * mb_per_hour)
    if free_mb < need:
        return (f"{free_mb} MB free but ~{need} MB needed for the remaining "
                f"{hours_remaining:.1f}h of capture")
    return None


# ── liveness + the arm loop ───────────────────────────────────────────────────

def liveness(deps: "Deps") -> str | None:
    """None when the instance is answering, else why not.

    Deliberately a READ that touches the app, not a container-level check: a
    container can be up while the app inside it is wedged, and the arm cares
    about the second.
    """
    try:
        deps.scan_status()
        return None
    except Exception as exc:
        return f"instance not answering ({type(exc).__name__})"


def run_arm(deps: "Deps", name: str, gapless: bool, minutes: float,
            cap_dir: str, backend: str = "direct", device: str = "default",
            queue_end_behavior: str = "full_random",
            poll_s: float = 10.0, on_event=None) -> ArmResult:
    """Drive one arm to its full duration, recovering from hard failures.

    Returns when the duration elapses or the arm gives up. The arm gives up only
    when it cannot restore the state it is measuring — continuing past that
    point produces confident data about something else.
    """
    def emit(kind, detail=""):
        if on_event:
            on_event(kind, detail)

    result = ArmResult(name)
    emit("arm_start", f"{name} gapless={gapless} {minutes}min")
    deps.login()
    prepare_arm(deps, gapless, backend, device, queue_end_behavior, result)

    deps.rig(f"mkdir -p {cap_dir}")
    deps.start_capture(cap_dir, minutes)
    if not capture_started(lambda: deps.list_segments(cap_dir)):
        # Launching is not starting; a silent capture failure would give six
        # hours of state samples beside no audio at all.
        result.incident("capture_did_not_start",
                        "no segment appeared after launching the recorder")
        emit("capture_failed")
        return result
    deps.start_sampler(cap_dir, minutes)
    emit("capture_started", cap_dir)

    end_at = time.time() + minutes * 60
    while time.time() < end_at:
        time.sleep(min(poll_s, max(0.0, end_at - time.time())))
        if time.time() >= end_at:
            break
        why = liveness(deps)
        if why:
            result.incident("hard_failure", why)
            emit("hard_failure", why)
            if not recover(deps, gapless, backend, device, result):
                emit("gave_up", "could not restore the arm's state")
                return result
            emit("recovered", f"recovery #{result.recoveries}")
            # The recorder outliving the sampler leaves audio nobody can
            # classify, so both are restarted together for the remainder.
            remaining = max(0.5, (end_at - time.time()) / 60.0)
            deps.stop_capture()
            deps.start_capture(cap_dir, remaining)
            deps.start_sampler(cap_dir, remaining)

    deps.stop_capture()
    result.completed = True
    emit("arm_done", f"{name} recoveries={result.recoveries}")
    return result
