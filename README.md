# Jukeplox

A self-hosted party jukebox for your music. Run it on a Linux box; guests open a web
page on their phones to browse your library and queue songs — playing to a Chromecast,
AirPlay, or DLNA speaker, or to speakers wired to the machine. No app, no account, no
password for guests.

Point it at a **Plex** or **Jellyfin** server, a **local folder** of audio files, or any
mix — Jukeplox merges them into one library.

**Linux only** — Docker host networking (required for speaker discovery) isn't available
on Mac/Windows Docker Desktop.

## Features

- **Multi-source library** — Plex, Jellyfin, and local folders merged into one catalog.
- **Cast anywhere** — Chromecast, AirPlay 2, DLNA/UPnP, or speakers wired to the host.
- **No-app guest access** — guests browse and queue from a phone web page; no install, no
  account, no password.
- **Party-ready** — shared queue, now playing, search, genre and most-played browse,
  gapless playback.
- **Self-hosted** — one Docker container on any Linux box (Raspberry Pi, NAS, or server).

## Screenshots

<p align="center">
  <img src="docs/screenshots/guest-browse.png" alt="Browsing the merged library" width="820"><br>
  <em>Guests browse the merged library — sources combined into one catalog.</em>
</p>
<p align="center">
  <img src="docs/screenshots/guest-phone.png" alt="Guest now-playing on a phone" width="300"><br>
  <em>…and queue from a phone — no app, no account, no login.</em>
</p>

## Built entirely with AI

Jukeplox was designed and written end-to-end by a large language model (Claude), directed
by a human. Every line of code, test, and doc came from an LLM — there is no hand-written
code. It works and is tested, but review it before trusting it on your network, and expect
the quirks that implies. Maintenance is LLM-driven too — see [CONTRIBUTING.md](CONTRIBUTING.md).

## Quick start

```bash
curl -fsSL https://raw.githubusercontent.com/DJRkod/jukeplox/main/install.sh | sh
```

Installs Docker if needed, starts Jukeplox, and opens the browser. Run it as your normal
user (needs `curl`). Then set an admin password, connect Plex, and pick a speaker.

_Prefer to read it first?_ Download the script (the URL above) with `curl -fsSLO`, skim it, then run `sh install.sh`.

**You need:** a Linux host · at least one music source — a Plex server (+ account), a
Jellyfin server, or a folder of audio files · a Chromecast/AirPlay/DLNA speaker, or
speakers attached to the machine.

## Manual install

```bash
docker run -d --name jukeplox \
  --network host \
  --restart unless-stopped \
  -v jukeplox-data:/data \
  -e BIND_HOST=192.168.1.50 \
  djrkod/jukeplox:latest
```

1. Replace `192.168.1.50` with your LAN IP (`hostname -I`). **Casting fails silently if `BIND_HOST` is wrong.**
2. Open `http://<IP>` → set an admin password (**≥12 chars, no reset**) → add a source under **Setup → Libraries** (Plex, Jellyfin, or a local folder) → pick a speaker under **Setup → Output**.
3. Share `http://<IP>` with guests.

Flags: `--network host` finds speakers · `BIND_HOST` lets Chromecast/DLNA pull audio.

**On a NAS that runs its own avahi/mDNS daemon** (e.g. TrueNAS — though there you'd use the
Apps UI, not this command), also add `-v /run/dbus/system_bus_socket:/run/dbus/system_bus_socket`
so Jukeplox can borrow the host's speaker-finder. Regular Linux (desktop, Pi, generic server)
uses in-process discovery and does **not** need it — that socket usually doesn't exist there,
and mounting a missing path makes Docker create a stray directory.

**Port 80 taken?** Add `-e PORT=8096` (keep `--network host`). If you cast, also add
`-e STREAM_BASE_URL=http://<IP>:8096` — or cast devices fetch from :80 and play nothing.

## Play to the machine's own speakers

Add to the `docker run`:

- **Headless / NAS / Pi:** `--device /dev/snd --group-add audio`
- **Linux desktop** (PulseAudio/PipeWire — `/dev/snd` gives "device busy"): `-e PULSE_SERVER=unix:/run/user/$(id -u)/pulse/native -v /run/user/$(id -u)/pulse/native:/run/user/$(id -u)/pulse/native`, run as the desktop user.

Two headless-ALSA gotchas on some hosts:

- Docker masks `/proc/asound` by default, so ALSA can't enumerate cards even with `--device /dev/snd`. Add `--security-opt systempaths=unconfined` (compose: `security_opt: [systempaths=unconfined]`).
- The container doesn't inherit the host's `/etc/asound.conf`. If your host defines a custom `default` PCM (softvol / dmix / a fixed rate), bind-mount it: `-v /etc/asound.conf:/etc/asound.conf:ro`.

Then choose **System Audio** in Setup → Output.

## More music sources

Add sources under **Setup → Libraries**, then **Rescan** (the catalog rebuilds on connect
or a manual rescan, not on restart).

- **Jellyfin** — **Connect Jellyfin** and sign in. Nothing to mount; it's reached over the
  network like Plex, and your password is never stored (only a token).
- **Local folder** — mount the folder **read-only**, then connect the *container* path (it's
  validated inside the container, so it must be a mount, not a bare host path). Add to the
  `docker run`:

  ```bash
  -v /srv/music:/music:ro
  ```

  Then **Connect Local Folder** → `/music`.

## Settings (`-e NAME=value`)

| Variable | Default | What it's for |
|---|---|---|
| `BIND_HOST` | `0.0.0.0` | Your LAN IP — set it for Chromecast/DLNA casting. |
| `PORT` | `80` | Web port (the host port directly, under host networking). |
| `STREAM_BASE_URL` | from `BIND_HOST` | `http://<IP>:<PORT>` when `PORT` ≠ 80 with Chromecast/DLNA, or behind HTTPS. |
| `LOG_LEVEL` | `info` | `debug` for troubleshooting. |
| `SESSION_TTL_HOURS` | `8` | How long an admin login lasts. |
| `COOKIE_SECURE` | `false` | Set `true` behind an HTTPS reverse proxy. |
| `ART_CACHE_SIZE_MB` | `512` | Max size of the on-disk album-art cache (LRU eviction). |
| `PLEX_MAX_CONCURRENCY` | `6` | Cap on concurrent requests to any one Plex server (protects small/shared servers). |

## Update

```bash
docker pull djrkod/jukeplox:latest
docker rm -f jukeplox
# then re-run your original command
```

Data in the volume is kept; **run-flags are not** — re-run with the exact flags you used
(`docker inspect jukeplox` first to recover them). Build info at `/api/version`.

## Notes

- **Security:** host networking exposes the admin port on all interfaces. Run on a trusted
  LAN; firewall the port if the box is internet-reachable — guest pages need no login.
  Reporting a vulnerability: [`SECURITY.md`](SECURITY.md).
- **Contributing / issues:** [`CONTRIBUTING.md`](CONTRIBUTING.md) (this is an LLM-maintained project).
- **TrueNAS Scale:** install via the Apps UI — see `TrueNAS-README.md`.
- **Step-by-step walkthrough:** `INSTALL.md`.

## License

Jukeplox is licensed under **GPL-2.0** — see [`LICENSE`](LICENSE). It bundles third-party
AirPlay binaries and builds on system libraries (ffmpeg, GStreamer) and Python packages,
documented in [`THIRD_PARTY_LICENSES`](THIRD_PARTY_LICENSES).
