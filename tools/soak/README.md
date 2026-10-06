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
#
# Container PID 1 is the `sh -c` wrapper, because CMD is shell-form — reading
# /proc/1 reports the shell and yields a flat ~1.4MB forever, which looks like
# a perfectly stable process. Find the uvicorn process instead.
CID=$(docker ps -qf name=jukeplox)
docker exec "$CID" sh -c '
  pid=""
  for p in /proc/[0-9]*; do
    c=$(cat "$p/comm" 2>/dev/null)
    case "$c" in uvicorn*|python*|gunicorn*) pid=${p#/proc/}; break;; esac
  done
  [ -z "$pid" ] && { echo "0 0 0 0 0"; exit 0; }
  rss=$(awk "/VmRSS/ {print \$2}" /proc/$pid/status)
  thr=$(awk "/Threads/ {print \$2}" /proc/$pid/status)
  fds=$(ls /proc/$pid/fd 2>/dev/null | wc -l)
  kids=$(ls -d /proc/[0-9]* 2>/dev/null | wc -l)
  media=$(grep -lE "ffmpeg|gst" /proc/[0-9]*/comm 2>/dev/null | wc -l)
  echo "$rss $thr $fds $kids $media"
'
```

A run of `0 0 0 0 0` means the process match failed — fix that before trusting
the resource trend, because `analyse.py` cannot tell a failed match from a
genuinely flat process.

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

## Acoustic arm

`capture_check.py` runs **on the rig** (it needs `arecord` and `ffmpeg`) and
answers one question: is audio actually reaching the loopback right now?

```sh
JP_ALSA_CAPTURE=default python3 capture_check.py            # assert audio present
JP_ALSA_CAPTURE=default python3 capture_check.py --floor    # measure the silence floor
```

Exit 0 = audio present, 1 = silent, 2 = the capture itself failed. The three are
deliberately distinct: a failed capture reported as silence would read downstream
as a dropout.

Measured on the validation rig 2026-09-23, and reproduced to the decimal hours
apart:

| Condition | mean | max |
|---|---|---|
| Silence floor (paused) | -90.3 dB | -76.3 dB |
| Live Direct playback | -21.4 dB | -8.4 dB |

Two things this interface will bite you with. It accepts **only** `S24_3LE` —
asking for the obvious `S16_LE` fails with "Sample format non available", which
reads like a broken device rather than a wrong argument. And `arecord -d` takes
whole seconds; `5.0` is rejected outright.

**A silent capture is not a defect on its own.** It is equally consistent with
the wrong backend owning the device, an unplugged cable, and a real fault.
Separating those needs the device's own session state — see
`docs/solutions/developer-experience/2026-09-05-acoustic-arm-must-assert-the-session.md`.
On 2026-09-23 this exact reading turned out to be a three-day-old sessionless
wedge (issue #57), and only the receiver's own `status: none` distinguished it
from a cabling fault.

**`media_procs` is not a liveness signal for the Direct backend.** Direct's
GStreamer pipeline runs in-process, so it never appears as an ffmpeg/gst
subprocess and the probe's fifth field stays at 0 while audio plays perfectly.
The signal that the card is held is the container's `/dev/snd` descriptor count.
`media_procs` counts flow-mode ffmpeg, which is a Cast concern.

### Rig-side capture (`tools/soak/rig/`)

Both run ON the rig, on one clock, because joining an audio timeline recorded
here against a state timeline recorded on the driving machine means joining
across two unsynchronised clocks — and a boundary gap is tens of milliseconds.

| File | Role |
|---|---|
| `record.sh` | Segmented lossless capture of the loopback |
| `state_sampler.py` | ~1 Hz playback-state timeline on the same clock |

```sh
JP_CAP_DIR=/root/soak/cap JP_CAP_HOURS=6 sh rig/record.sh
JP_BASE="http://$(hostname -I | awk '{print $1}')" JP_MINUTES=360 python3 rig/state_sampler.py
```

**Use the host's own address, not loopback.** uvicorn binds to the LAN address
rather than `0.0.0.0`, so `http://127.0.0.1` fails with a URLError even from the
rig itself.

Measured 2026-09-23: FLAC capture is ~342 MB/hour, so a 12-hour run is ~4.1 GB.
`record.sh` refuses to start when the disk cannot hold `JP_CAP_HOURS` of it,
rather than discovering the problem at hour nine.

**Position comes from `/api/playback/position`, not `/api/now-playing`** — the
latter carries no position field at all, so reading one from it returns null
forever and looks like a lost session. The position endpoint asks the output
router for the backend's own position, so a value that ADVANCES across samples
is evidence the device is really playing. A static position while `is_playing`
is true is the signal worth catching; paired with the `/dev/snd` descriptor
count it gives two independent views of whether anything is rendering.

### Features and classification

| File | Role |
|---|---|
| `rig/features.py` | Segments -> silent intervals, levels, per-channel RMS |
| `rig/classify.py` | Joins audio against state; labels every silence; measures boundary gaps |
| `boundary_probe.py` | Measures ONE track boundary to within ~100 ms (see below) |

```sh
python3 rig/features.py /root/soak/cap     # -> features.jsonl
```

Thresholds are the ones proven on this rig (`silencedetect=noise=-55dB:d=0.4`),
which sits between the measured -90.3 dB floor and roughly -21 dB of live
playback rather than being a guess.

**Silence is not a defect.** The classifier subtracts the explainable cases in
order — nothing playing, an operator action, the post-dispatch settle window, a
track boundary — and only what survives is an incident. Incidents are split in
two because they point at different layers: `incident_no_pipeline` (reported
playing, nothing holds the sound card) and `incident_silent_while_playing`
(pipeline live, no audio). Anything that cannot be placed is `unknown`, never
silently clean.

**A boundary is not only a track_id change.** A queue can hold the same track
twice running, and then `track_id` never changes across a real boundary — on
2026-09-23 a capture spanning one reported zero boundaries for exactly that
reason. Position dropping from near the end of a track to near zero is the
second detector. A backward seek is excluded by requiring the landing point to
be near zero, and operator actions are attributed before boundaries are
considered at all.

First end-to-end run against real audio (2026-09-23, Direct, gapless enabled):
one boundary found via position reset, both channels alive at -21.2/-22.4 dB,
measured gap **5.582 s**. Recorded here as a single observation, not a verdict —
prior rig figures put an unarmed boundary at 9.2 s and an armed one at zero
silence >=0.4 s, so this wants a distribution behind it before it means anything.

### Gapless A/B, 2026-09-23 (Direct backend)

Same 43.5 s track queued repeatedly so its intrinsic silence is identical at
every boundary and cancels between arms. 11 boundaries per arm.

| | total silence per boundary | mean |
|---|---|---|
| Gapless ON | 0.885 s at **every** boundary | 0.885 s |
| Gapless OFF | 0.507 - 1.222 s | 0.898 s |

**Gapless works on Direct.** The means are indistinguishable because this
track's own ~0.9 s of silence dominates both. The signal is the VARIANCE: with
gapless on, every boundary measured identically to the millisecond — a pipeline
that never stops, so the capture contains the file's encoded silence and nothing
else. With it off, the same transition scattered, sometimes shorter than the
content (teardown clipping the fade) and sometimes longer (rebuild delay).

Two traps this run walked into, both worth avoiding next time:

**Total silence at a boundary is mostly the track, not the player.** A first
measurement on a different track read 5.582 s and looked alarming; 5.449 s of it
was the outgoing track's fade-out. Never read boundary silence as a system gap
without a control arm on the same audio.

**The pre/post split is limited by the sampling rate.** The boundary timestamp
comes from the state timeline, so at the default 1 Hz it carries ~1 s of
uncertainty — coarser than the sub-second gaps it is splitting. Above, total was
constant to the millisecond while post ranged 0.000-0.885 s purely from sampler
jitter. For a run that needs the split to mean anything, set
`JP_STATE_INTERVAL` well below the gaps of interest (0.1 is ~65 MB of JSONL over
12 hours) — or treat the total as the only reliable figure.

### Arm orchestration, peaks and the verdict

| File | Role |
|---|---|
| `arm.py` | One arm end to end: set up, verify it took, recover, hand over |
| `probe_window.py` | Scheduled load peaks + the latency contract a probe reports against |
| `verdict.py` | Trends, instrument health, incident ranking (used by `analyse.py`) |

**Everything the arm sets, it reads back.** A write that returns 200 and does not
take is what produces a confidently-wrong arm, so the mode, the pinned output and
the queue-end behaviour are each verified after being set, and again after every
recovery.

Recovery is `docker stop -t 30` then start — **never `rm -f`**. A SIGKILL restart
once left the database locked while search kept answering from cache, so nothing
looked broken and the rest of the run was silently degraded. After every restart
the arm re-checks health (`refresh_failed`) and re-asserts its own mode before
continuing. Recoveries are bounded so a permanently broken instance cannot spend
six hours restarting.

**The verdict can tell "nothing broke" from "nobody was looking."** `analyse.py`
now reports instrument health before anything else: an all-zero container sample
run is a broken sampler, not a stable system; a perfectly flat RSS suggests the
probe is reading PID 1 (a shell wrapper that reports ~1.4 MB forever); a party
that added almost nothing is a broken harness, because a soak where the selectors
matched nothing looks exactly like a soak where everything worked.

Two reporting rules that exist to stop the report overclaiming:

- **Rising RSS is not a leak.** Freed objects return to allocator arenas rather
  than the OS, so churn and a leak look identical here. The note says what would
  settle it (an idle tail, and two workload cycles compared) rather than asserting.
- **`media_procs` means nothing on Direct.** Its GStreamer pipeline runs
  in-process, so the count sits at 0 while audio plays perfectly. Set
  `JP_BACKEND=direct` and the report says so instead of reporting reassuring
  flatness. Use the `/dev/snd` descriptor count from the rig-local timeline.

Browser teardown was already correct and is left alone: `cdp.mjs` kills the
process tree, keys every sweep on a run-unique profile tag so it can never touch
your own Chrome, and verifies with `Get-CimInstance` rather than `wmic | grep` —
which returns nothing on a UTF-16 stream and once reported zero leftovers while
there were dozens.

## Measuring a single track boundary — `boundary_probe.py`

The soak arm answers "did anything break over hours". `boundary_probe.py`
answers "is THIS boundary clean, to within 100 ms", which the soak instrument
cannot: `rig/features.py` runs `silencedetect` with `d=0.4`, and that is
ffmpeg's **minimum duration**, not a threshold — below it a gap is not
measured-and-passed, it is never emitted at all, so a gapping build and a
fixed build both report zero silences.

Three prerequisites, all enforced by the module rather than left to the caller:

- `JP_SILENCE_MIN_S` is lowered to 0.02 for every probe run.
- The boundary timestamp comes from an HTTP-only poller, and the **achieved**
  sample interval is recorded and checked — `rig/state_sampler.py` shells out
  to `docker` twice per sample, so it runs near 1 Hz whatever it is asked for.
- A measured noise floor is required; without it the quiet-audio filter is
  silently off, which had a measured 17-in-18 false-positive rate.

Anything it cannot trust raises `ProbeRefusal` rather than returning an
optimistic number, and a pass additionally requires a positive control
recovered in the same session.

```bash
JP_BASE="http://<rig-lan-ip>" JP_ADMIN_PW=...   python3 boundary_probe.py --capture cap.wav --poll poll.jsonl     --cap-t0-ms <epoch-ms-of-capture-start> --measure-floor
```

Exit 3 means refused (the reason is printed as JSON); exit 0 means a verdict
was produced.
