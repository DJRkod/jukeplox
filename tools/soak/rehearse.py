"""The two-arm runner: rehearsal when compressed, the real soak at full length.

Same code path either way — both arms, the changeover restart between them, and
optionally a deliberately induced failure so the recovery path meets an actual
crash rather than only a unit test. `--minutes 3 --induce-failure-at 60` is the
rehearsal; `--minutes 360 --induce-failure-at 0` is a twelve-hour soak.

Runs either from a driving machine (set JP_SSH and it reaches the rig over
plink) or ON the rig itself (leave JP_SSH unset and rig commands run locally).
The second form is what survives an unattended night, because nothing outside
the rig has to stay alive for the arms to keep going.

Everything comes from the environment — no host, no credential is committed here.

    JP_BASE=http://jukebox.local JP_ADMIN_PW=... \\
    JP_SSH=user@rig JP_PW=... JP_HOSTKEY=... \\
    python3 rehearse.py --minutes 4 --induce-failure-at 90

Why a rehearsal exists at all: the recovery path is the least-exercised and
most-load-bearing code in the harness. Finding out it does not work costs ten
minutes here and six hours in the real run.
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import arm as armlib  # noqa: E402


def _scrub(text: str) -> str:
    """Remove anything secret-shaped before it can reach a log or a report.

    Truncation is not redaction: `subprocess.TimeoutExpired.__str__` carries the
    whole argv, which includes the rig password.
    """
    for var in ("JP_PW", "JP_HOSTKEY", "JP_ADMIN_PW", "JP_SSH"):
        secret = os.environ.get(var)
        if secret:
            text = text.replace(secret, f"<{var}>")
    return text


def make_rig_cmd():
    """A callable that runs one command on the rig.

    With JP_SSH set, over plink from a driving machine. Without it, locally —
    which is how an unattended run survives: the orchestration lives on the same
    box as the thing it is orchestrating, so no other machine has to stay up.
    """
    ssh = os.environ.get("JP_SSH")
    if not ssh:
        def run_local(command: str) -> str:
            try:
                p = subprocess.run(["sh", "-c", command], capture_output=True,
                                   text=True, timeout=180)
                return p.stdout
            except subprocess.TimeoutExpired:
                return ""
            except Exception as exc:
                sys.stderr.write(
                    "local command failed: " + type(exc).__name__ + "\n")
                return ""
        return run_local
    base = ["plink", "-batch"]
    if os.environ.get("JP_HOSTKEY"):
        base += ["-hostkey", os.environ["JP_HOSTKEY"]]
    if os.environ.get("JP_PW"):
        base += ["-pw", os.environ["JP_PW"]]
    base += [ssh]

    def run(command: str) -> str:
        try:
            p = subprocess.run(base + [command], capture_output=True,
                               text=True, timeout=180)
            return p.stdout
        except subprocess.TimeoutExpired:
            return ""
        except Exception as exc:
            sys.stderr.write(_scrub(f"rig command failed: {type(exc).__name__}\n"))
            return ""
    return run


def induce_failure(rig, after_s: float, log) -> threading.Thread:
    """Kill the app container after a delay, from outside the arm.

    Deliberately external and deliberately ungraceful: the arm must discover the
    failure by observing the instance, not by being told. A graceful stop here
    would rehearse the wrong thing.
    """
    def go():
        time.sleep(after_s)
        log("inducing_failure", f"killing the container at t+{after_s:.0f}s")
        rig("docker kill $(docker ps -qf name=jukeplox) >/dev/null 2>&1; true")
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--minutes", type=float, default=4.0)
    ap.add_argument("--induce-failure-at", type=float, default=90.0,
                    help="seconds into arm one; 0 disables")
    ap.add_argument("--cap-root", default="/root/soak/rehearsal")
    args = ap.parse_args()

    base = os.environ.get("JP_BASE")
    if not base:
        raise SystemExit("JP_BASE is required")

    rig = make_rig_cmd()
    deps = armlib.Deps(base, os.environ.get("JP_ADMIN_PW", ""), rig_cmd=rig)

    events = []

    def log(kind, detail=""):
        rec = {"t": int(time.time() * 1000), "kind": kind, "detail": detail}
        events.append(rec)
        print(f"[{time.strftime('%H:%M:%S')}] {kind}: {detail}", flush=True)

    rig(f"rm -rf {args.cap_root}; mkdir -p {args.cap_root}")
    results = []

    # Arm one: gappy, with a real crash in the middle.
    if args.induce_failure_at > 0:
        induce_failure(rig, args.induce_failure_at, log)
    results.append(armlib.run_arm(
        deps, "gappy", gapless=False, minutes=args.minutes,
        cap_dir=f"{args.cap_root}/gappy", on_event=log))

    # Changeover: a restart between arms so the second reads from a clean
    # baseline rather than inheriting the first arm's accumulated state.
    log("changeover", "restarting between arms")
    try:
        deps.restart_container()
    except Exception as exc:
        log("changeover_failed", type(exc).__name__)
    # Reuse the arm's readiness wait rather than a magic sleep. A hand-rolled
    # sleep(15) here is what crashed the first rehearsal: a container start is
    # not an app ready, and the very next call landed on a closed socket.
    if not armlib.wait_ready(deps):
        log("changeover_never_came_back",
            f"instance did not answer within {armlib.READY_TIMEOUT_S}s")
        return 1
    try:
        deps.login()
    except Exception as exc:
        log("changeover_relogin_failed", type(exc).__name__)

    # Arm two: gapless.
    results.append(armlib.run_arm(
        deps, "gapless", gapless=True, minutes=args.minutes,
        cap_dir=f"{args.cap_root}/gapless", on_event=log))

    print()
    print("=" * 66)
    print("REHEARSAL RESULT")
    print("=" * 66)
    ok = True
    for r in results:
        d = r.as_dict()
        print(json.dumps(d, indent=1))
        if not d["completed"]:
            ok = False
    print()
    print("arms completed :", sum(1 for r in results if r.completed), "/", len(results))
    print("recoveries     :", sum(r.recoveries for r in results))
    print("incidents      :", sum(len(r.incidents) for r in results))
    # A rehearsal that never exercised recovery has not rehearsed the thing most
    # likely to fail.
    if args.induce_failure_at > 0 and not any(r.recoveries for r in results):
        print()
        print("WARNING: a failure was induced but no recovery ran — the arm did "
              "not notice. That is a harness defect, not a clean rehearsal.")
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
