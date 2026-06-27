# Jukeplox on TrueNAS Scale

Install via the **Apps** UI. You need a dataset for app data (e.g. `/mnt/<pool>/jukeplox`)
and a Plex account. Playback goes to network speakers (Chromecast/AirPlay/DLNA).

## 1. Create a Custom App

Apps → **Discover Apps** → **Custom App**. Set:

| Field | Value |
|---|---|
| Application Name | `jukeplox` |
| Image Repository | `djrkod/jukeplox` |
| Image Tag | `latest` |
| Pull Policy | **Always** (so redeploys grab the newest image) |

## 2. Container

- **Environment Variables** → add `BIND_HOST` = your TrueNAS LAN IP (e.g. `192.168.0.70`). Lets Chromecast/DLNA fetch audio through Jukeplox. *(Skip only if you use AirPlay exclusively.)*
- **Restart Policy** → **Unless Stopped** — so it recovers from a crash. The default ("No") leaves Jukeplox down until you restart it by hand.

## 3. Network

Turn on **Host Network**. Required — Jukeplox finds Cast/AirPlay over multicast (UDP 5353),
which Docker's normal network blocks; it also binds the port directly on the host.

## 4. Storage

Under **Storage and Persistence**, add two **Host Path** mounts:

| Host Path | Mount Path | Why |
|---|---|---|
| `/mnt/<pool>/jukeplox` (your dataset) | `/data` | database, settings, artwork — survives updates |
| `/run/dbus/system_bus_socket` | `/run/dbus/system_bus_socket` | finds Cast/AirPlay devices (see below) |

TrueNAS runs its own `avahi-daemon` (which owns UDP 5353), so Jukeplox asks avahi over the
system D-Bus socket instead — that's the second mount. No host reconfiguration needed.

## 5. Install and set up

Click **Install** and wait for **Running** (~30s). Open `http://<your-truenas-ip>`, then:

1. Set an admin password — **≥12 chars, no reset** (forgetting it means wiping app data).
2. Connect Plex — enter the code it shows at plex.tv.
3. Pick a speaker under **Setup → Output**.

Share `http://<your-truenas-ip>` with guests — no app or password needed. *(Optional: add a
**Portal** — HTTP, Use Node IP, port 80 — for a clickable link in the TrueNAS Apps UI.)*

**Port 80 in use?** Add env var `PORT` = a free port (e.g. `8096`) → app at `http://<IP>:8096`.
If you also cast, add `STREAM_BASE_URL` = `http://<IP>:8096`, or cast devices try :80 and play nothing.

## Update

Apps → jukeplox → ⋮ → **Edit** → **Update**. With Pull Policy = **Always** this re-pulls
`latest`; your `/data` dataset is untouched. Confirm the build at `http://<IP>/api/version`.

## Troubleshooting

- **No Cast/AirPlay devices** — confirm **Host Network** (step 3) and the **D-Bus mount** (step 4). In Logs, `Cannot bind mDNS port 5353 … Falling back to avahi over D-Bus` is **expected** and means discovery works; other avahi/D-Bus errors mean the mount is wrong.
- **Can't reach the admin page** — use `http://`, not `https://`; the app is on port 80 (`netstat -tlnp | grep ':80 '`).
- **Security** — host networking exposes the admin port on all interfaces. Run on a trusted LAN; firewall the port if the box is internet-reachable — guest pages need no login.
