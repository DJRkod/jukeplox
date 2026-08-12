# Installing Jukeplox

A self-hosted party jukebox for your music — Plex, Jellyfin, Emby, an OpenSubsonic server
(Navidrome & friends), or a local folder, merged into one library. Guests queue songs from
their phones; playback goes to a Chromecast/AirPlay/DLNA speaker or to speakers wired to the
machine.

**Linux + Docker only** (Mac/Windows Docker Desktop can't discover speakers). On
**TrueNAS Scale**, use the Apps UI instead — see `TrueNAS-README.md`.

## Quick start

One command — installs Docker if needed, starts Jukeplox, opens the browser (needs `curl`):

```bash
curl -fsSL https://raw.githubusercontent.com/DJRkod/jukeplox/main/install.sh | sh
```

Run it as your normal user, then finish in the browser ([step 5](#5-set-it-up)). Prefer to
read it first? Download the script (URL above) with `curl -fsSLO`, skim, then `sh install.sh`.

## Manual install

### 1. Install Docker (skip if you have it)

```bash
curl -fsSL https://get.docker.com | sh
```

### 2. Find your LAN IP

```bash
hostname -I | awk '{print $1}'
```

### 3. Start Jukeplox

Put your IP from step 2 in `BIND_HOST` (**casting fails silently if it's wrong**):

```bash
docker run -d --name jukeplox \
  --network host \
  --restart unless-stopped \
  -v jukeplox-data:/data \
  -e BIND_HOST=192.168.1.50 \
  djrkod/jukeplox:latest
```

**On a NAS with its own avahi/mDNS daemon** (e.g. TrueNAS — where you'd normally use the Apps
UI instead), also add `-v /run/dbus/system_bus_socket:/run/dbus/system_bus_socket` to borrow the
host's speaker-finder. Regular Linux (desktop, Pi, generic server) uses in-process discovery and
doesn't need it — that socket usually doesn't exist there, and mounting a missing path just makes
Docker create a stray directory. (The quick-start `install.sh` detects this automatically and
mounts the socket only when it exists.)

**Port 80 taken?** Add `-e PORT=8096`; if you cast, also `-e STREAM_BASE_URL=http://<IP>:8096`.

**Adding a local music folder?** Mount it **read-only** and connect the *container* path in
step 5: add `-v /srv/music:/music:ro` to the command above. (Network servers — Jellyfin, Emby,
OpenSubsonic — need no mount; they're reached over the network.)

**Casting from an OpenSubsonic server (Navidrome, gonic, …)?** Its streams are always proxied
through Jukeplox, so a cast device must be able to reach this box. Under host networking with a
correct `BIND_HOST` that's automatic; under Docker **bridge** networking set
`-e STREAM_BASE_URL=http://<IP>` too, or cast devices fetch from an unreachable container
address and play nothing. (Details: README → *Casting a Subsonic source*.)

### 4. (Optional) Play to the machine's own speakers

Add to the `docker run`:

- **Headless / NAS / Pi:** `--device /dev/snd --group-add audio`
- **Linux desktop:** `-e PULSE_SERVER=unix:/run/user/$(id -u)/pulse/native -v /run/user/$(id -u)/pulse/native:/run/user/$(id -u)/pulse/native` — run as the desktop user; don't use `/dev/snd` ("device busy").

On the headless path, two host quirks bite on some systems:

- Docker masks `/proc/asound` by default, so ALSA can't enumerate cards even with `--device /dev/snd`. Add `--security-opt systempaths=unconfined` (compose: `security_opt: [systempaths=unconfined]`).
- The container doesn't inherit the host's `/etc/asound.conf`. If the host defines a custom `default` PCM (softvol / dmix / fixed rate), bind-mount it: `-v /etc/asound.conf:/etc/asound.conf:ro`.

### 5. Set it up

Open `http://<IP>` and:

1. Set an admin password — **≥12 chars, no reset** (forgetting it means wiping data).
2. Add a music source under **Setup → Libraries** — Plex (enter the code at plex.tv),
   Jellyfin (sign in), Emby (URL + username + password), OpenSubsonic (URL + API key +
   username — Navidrome is the validated reference, others are best-effort), or a local
   folder (`/music` from the mount above). Your own servers' music libraries switch on and
   start indexing automatically; untick any under **Edit libraries…**. A server *shared with
   you* connects too but stays off until you tick its libraries there. A big library indexes
   in the background — search fills in as it runs. (Per-server key/password details, and
   casting from OpenSubsonic, are in the README.)
3. Pick a speaker under **Setup → Output**.

Share `http://<IP>` with guests — no app or password needed.

### 6. (Optional) Synchronized multi-room

Under **Setup → Optional integrations** you can enable two **off-by-default**,
server-fed multi-room backends (both baked into the image; nothing runs until
enabled):

- **Snapcast** — jukeplox runs an embedded `snapserver` and streams the current
  queue in sync to any Snapcast clients; group them into zones with per-client
  volume. Optionally point at an external snapserver by host/port (read + assign
  only — jukeplox never rewrites a server you don't own).
- **Sendspin** *(experimental)* — an in-process, mDNS-advertised Sendspin server;
  pair clients with a short PIN.

They stream the single shared queue (one stream, zoned — not different songs per
room) and don't change local output: **System Audio** stays how the host's own
speakers play. Set `BIND_HOST` to the server's LAN IP so Sendspin advertises a
reachable address. snapserver's control/web ports bind loopback; the client
stream port (1704) is LAN-facing.

## Settings (`-e NAME=value`)

| Variable | Default | What it's for |
|---|---|---|
| `BIND_HOST` | `0.0.0.0` | Your LAN IP — set it for Chromecast/DLNA casting. |
| `PORT` | `80` | Web port (the host port directly, under host networking). |
| `STREAM_BASE_URL` | from `BIND_HOST` | `http://<IP>:<PORT>` when `PORT` ≠ 80 with Chromecast/DLNA, or behind HTTPS. |
| `LOG_LEVEL` | `info` | `debug` for more detail when troubleshooting. |
| `SESSION_TTL_HOURS` | `8` | How long an admin login lasts. |
| `COOKIE_SECURE` | `false` | Set `true` behind an HTTPS reverse proxy. |

## Update

```bash
docker pull djrkod/jukeplox:latest
docker rm -f jukeplox
# re-run your step 3 command (same flags)
```

Data is kept; **run-flags aren't** — `docker inspect jukeplox` first to recover them.
Confirm the build via the `git_sha` at `http://<IP>/api/version` (the `image_tag` field
may show an internal release-train name rather than `latest` — that's expected).

## Troubleshooting

- **Page won't load** — `docker ps`, `docker logs jukeplox`; check `http://<IP>` (or `:<PORT>`) and the firewall.
- **"Could not pull"** — the image must be public; anonymous Docker Hub pulls are rate-limited. `docker login` or wait, then retry.
- **No speakers** — needs `--network host`. On NAS the dbus mount is required ("Falling back to avahi over D-Bus" is normal).
- **No sound from local speakers** — desktop: use the PulseAudio route, not `/dev/snd`. Headless: confirm `--device /dev/snd --group-add audio` and **System Audio** in Setup → Output; `-e GST_DEBUG=3` shows the error.
- **Forgot admin password** — no reset; start fresh: `docker rm -f jukeplox && docker volume rm jukeplox-data` (keeps your Plex library; clears settings + queue).
- **Plex plays via the internet instead of your LAN** — a containerized Plex server (e.g. a TrueNAS app) may only advertise an internal container address, so Jukeplox falls back to your public address. Fix it server-side: in Plex, **Settings → Network → Custom server access URLs**, add `http://<plex-LAN-IP>:32400`, then reconnect Plex in Jukeplox.
- **Security** — exposes the admin port on all interfaces. Run on a trusted LAN; firewall if internet-reachable — guest pages need no login.
