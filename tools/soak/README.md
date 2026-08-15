# Party soak

A long, synthetic party against a running jukeplox: headless browsers behaving
like unruly guests, a host hammering the transport and flipping settings
mid-song, and an observer recording whether playback stayed honest.

This is how the four defects filed on 2026-08-15 were found. None of them would
have been caught by the unit suite — they need a real deployment, real audio
hardware, and hours of overlapping abuse.

## What it proves, and what it doesn't

**It proves** that the guest UI survives concurrent real-browser use: every
guest action is a genuine DOM click on a live element, with real event handlers
and real websocket updates, against a real library.

**It does not prove** the admin UI works. Transport churn (`admin_chaos.py`) and
settings churn (`admin_toggles.py`) are driven over the API, not through the
admin page. A bug that only exists in the admin front-end will not show up here.
Guests have no transport controls at all — play/pause/skip/seek/volume are
absent from the guest DOM, not merely hidden — which is why host abuse is a
separate driver rather than another guest behaviour.

It is also not a pass/fail gate. `analyse.py` produces a verdict a human reads.

## Layout

| File | Role |
|---|---|
| `party.mjs` | Synthetic guests driving the real guest UI |
| `cdp.mjs` | Browser launch, CDP session, and teardown |
| `jp_http.py` | Shared target resolution, HTTP client and JSONL recorder |
| `admin_chaos.py` | Transport churn (skip/pause/seek/volume) |
| `admin_toggles.py` | Settings churn during playback |
| `monitor.py` | Observer — pure reads plus container sampling |
| `analyse.py` | Turns the four logs into a verdict |

## Requirements

- Node with a global `WebSocket` (Node 22+). No npm dependencies.
- Python 3.11+. Standard library only.
- Google Chrome or Chromium.
- A running jukeplox with a populated library and a working output backend.

## Configuration

Everything comes from the environment. Nothing is committed — no host address,
no password, no SSH key.

| Variable | Used by | Meaning |
|---|---|---|
| `JP_BASE` | all | **Required.** Base URL of the running instance, e.g. `http://jukebox.local` |
| `JP_ADMIN_PW` | admin drivers, monitor | Admin password, for the login the drivers perform |
| `JP_MINUTES` | all | Run length in minutes (default 30) |
| `JP_GUESTS` | `party.mjs` | Concurrent headless guests (default 4) |
| `JP_CHROME` | `party.mjs` | Chrome/Chromium binary, if not in a standard location |
| `JP_RIG` | `monitor.py` | Set to enable container sampling over SSH |
| `JP_SSH`, `JP_PW`, `JP_HOSTKEY` | `monitor.py` | `plink` target, password, and host-key fingerprint |
| `JP_PROBE` | `monitor.py` | Probe command on the rig (default `sh /root/jp-probe.sh`) |
| `JP_LOG` | `party.mjs`, `analyse.py` | Guest action log (default `party-actions.jsonl`) |
| `JP_ADMIN_LOG` | `admin_chaos.py`, `analyse.py` | Transport log (default `admin-actions.jsonl`) |
| `JP_TOGGLE_LOG` | `admin_toggles.py`, `analyse.py` | Settings log (default `toggle-actions.jsonl`) |
| `JP_MON` | `monitor.py`, `analyse.py` | Observer log (default `monitor.jsonl`) |

`analyse.py` reads the same four log variables, so a run with custom log paths
is analysed by exporting the same values.

There is deliberately no default for `JP_BASE`. A soak points at a real
deployment; the target must be stated every time.

## Running it

Each driver is independent and writes its own JSONL. Run them concurrently, in
the same directory, for the same duration:

```bash
export JP_BASE=http://jukebox.local
export JP_ADMIN_PW='...'
export JP_MINUTES=45

node tools/soak/party.mjs        &   # synthetic guests
python tools/soak/monitor.py     &   # observer (pure reads)
python tools/soak/admin_chaos.py &   # transport churn
python tools/soak/admin_toggles.py & # settings churn
wait

python tools/soak/analyse.py         # verdict
```

Start a soak against a **rebuilt image**, not a stale one, and note the
`git_sha` from `/api/version` — `monitor.py` records it in its first line so a
run can be attributed to a build later.

## Cleanup

`party.mjs` kills the browsers it spawned on normal exit, on error, and on
Ctrl-C, then **verifies** nothing carrying its run signature is left and says so
on stderr. Two details in there are load-bearing and were wrong in the first
draft of this harness:

- It kills the process **tree** (`taskkill /T /F`), not the spawned PID. On
  Windows, killing the launcher leaves the browser, renderer, gpu, crashpad and
  utility children orphaned.
- It verifies with `Get-CimInstance`, not `wmic | grep`. `wmic` emits UTF-16, so
  piping it into a text matcher reports zero leftovers every time — including
  when there are dozens.

Every match is keyed on a run-unique profile tag, so a sweep can never touch the
user's own Chrome. See the project process-hygiene standard in `CLAUDE.md` and
the reference implementation in `tools/perf/browse-bench.mjs`.

If a run is killed hard enough to skip teardown:

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*jp-soak-*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

## Container sampling

`monitor.py` samples RSS, thread count, FDs and child processes so `analyse.py`
can flag a resource trend. This needs a small probe script on the rig, because
the image has no `ps`:

```sh
# /root/jp-probe.sh — emits: rss_kb threads fds children media_procs
CID=$(docker ps -qf name=jukeplox)
docker exec "$CID" sh -c '
  pid=1
  rss=$(awk "/VmRSS/ {print \$2}" /proc/$pid/status)
  thr=$(awk "/Threads/ {print \$2}" /proc/$pid/status)
  fds=$(ls /proc/$pid/fd 2>/dev/null | wc -l)
  kids=$(ls -d /proc/[0-9]* 2>/dev/null | wc -l)
  media=$(grep -lE "ffmpeg|gst" /proc/[0-9]*/comm 2>/dev/null | wc -l)
  echo "$rss $thr $fds $kids $media"
'
```

Without `JP_RIG` set, the monitor still runs — it just records no container
samples, and `analyse.py` says so rather than reporting a clean trend.

## Reading the output

`analyse.py` reads the three JSONL streams and prints five sections: guest
activity, transport churn, playback integrity, play-count correctness, and the
resource trend. The lines worth looking at first:

- **`counted MORE than once`** — a track whose play count moved by more than one
  per airing. Exactly-once play recording is the invariant most likely to break
  under skip abuse.
- **`ratio increments/transitions`** — 1.00 is ideal. Below 1.00 means plays were
  missed entirely.
- **`transitions <2s apart`** — a fast-drain symptom: the queue advancing without
  anything actually playing.
- **`of those, queue grew >1`** — a triple-tap that produced duplicates.
- **`RSS grew`** / **`fds grew`** — the resource trend that led to the memory
  issue being filed.
- **`flips that STOPPED playback`** — a settings change that killed the music.
  That is always a bug; the section names which flip did it.

### When RSS grows

The resource trend shows the *shape* of growth, never its cause. To get a named
allocation site, bracket the run with the admin memory probe (see
`app/memory_probe.py`):

```bash
curl -X POST "$JP_BASE/admin/diagnostics/memory/start"
curl -X POST "$JP_BASE/admin/diagnostics/memory/snapshot" -d '{"label":"base"}'
# ... run the soak ...
curl -X POST "$JP_BASE/admin/diagnostics/memory/snapshot" -d '{"label":"after"}'
curl "$JP_BASE/admin/diagnostics/memory/diff?before=base&after=after"
curl -X POST "$JP_BASE/admin/diagnostics/memory/stop"
```

Those endpoints need the admin session cookie, and tracing is not free — stop it
when the run is done. `analyse.py` prints this reminder whenever RSS grew.

A run is only meaningful if the guests actually did something: check
`tracks added via search` is in the hundreds before trusting anything else. A
soak where the selectors silently matched nothing looks exactly like a soak
where everything worked.
