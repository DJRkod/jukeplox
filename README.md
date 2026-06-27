# Jukeplox

A self-hosted party jukebox for your Plex music. Run it on a Linux box; guests open a web
page on their phones to browse your library and queue songs — playing to a Chromecast,
AirPlay, or DLNA speaker, or to speakers wired to the machine. No app, no account, no
password for guests.

**Linux only** — Docker host networking (required for speaker discovery) isn't available
on Mac/Windows Docker Desktop.

## Quick start

```bash
curl -fsSL https://raw.githubusercontent.com/DJRkod/jukeplox/main/install.sh | sh
```

Installs Docker if needed, starts Jukeplox, and opens the browser. Run it as your normal
user (needs `curl`). Then set an admin password, connect Plex, and pick a speaker.

_Prefer to read it first?_ Download the script (the URL above) with `curl -fsSLO`, skim it, then run `sh install.sh`.

**You need:** a Linux host · a Plex server on the same LAN (+ a Plex account) · a
Chromecast/AirPlay/DLNA speaker, or speakers attached to the machine.

## Manual install

```bash
docker run -d --name jukeplox \
  --network host \
  --restart unless-stopped \
  -v jukeplox-data:/data \
  -v /run/dbus/system_bus_socket:/run/dbus/system_bus_socket \
  -e BIND_HOST=192.168.1.50 \
  djrkod/jukeplox:latest
```

1. Replace `192.168.1.50` with your LAN IP (`hostname -I`). **Casting fails silently if `BIND_HOST` is wrong.**
2. Open `http://<IP>` → set an admin password (**≥12 chars, no reset**) → connect Plex at the code shown → pick a speaker under **Setup → Output**.
3. Share `http://<IP>` with guests.

Flags: `--network host` finds speakers · `BIND_HOST` lets Chromecast/DLNA pull audio · the
dbus mount borrows the host's speaker-finder (NAS boxes).

**Port 80 taken?** Add `-e PORT=8096` (keep `--network host`). If you cast, also add
`-e STREAM_BASE_URL=http://<IP>:8096` — or cast devices fetch from :80 and play nothing.

## Play to the machine's own speakers

Add to the `docker run`:

- **Headless / NAS / Pi:** `--device /dev/snd --group-add audio`
- **Linux desktop** (PulseAudio/PipeWire — `/dev/snd` gives "device busy"): `-e PULSE_SERVER=unix:/run/user/$(id -u)/pulse/native -v /run/user/$(id -u)/pulse/native:/run/user/$(id -u)/pulse/native`, run as the desktop user.

Then choose **System Audio** in Setup → Output.

## Settings (`-e NAME=value`)

| Variable | Default | What it's for |
|---|---|---|
| `BIND_HOST` | `0.0.0.0` | Your LAN IP — set it for Chromecast/DLNA casting. |
| `PORT` | `80` | Web port (the host port directly, under host networking). |
| `STREAM_BASE_URL` | from `BIND_HOST` | `http://<IP>:<PORT>` when `PORT` ≠ 80 with Chromecast/DLNA, or behind HTTPS. |
| `LOG_LEVEL` | `info` | `debug` for troubleshooting. |
| `SESSION_TTL_HOURS` | `8` | How long an admin login lasts. |
| `COOKIE_SECURE` | `false` | Set `true` behind an HTTPS reverse proxy. |

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
- **TrueNAS Scale:** install via the Apps UI — see `TrueNAS-README.md`.
- **Step-by-step walkthrough:** `INSTALL.md`.

## License

Jukeplox is licensed under **GPL-2.0** — see [`LICENSE`](LICENSE). It bundles third-party
AirPlay binaries and builds on system libraries (ffmpeg, GStreamer) and Python packages,
documented in [`THIRD_PARTY_LICENSES`](THIRD_PARTY_LICENSES).
