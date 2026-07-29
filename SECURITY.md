# Security Policy

## Reporting a vulnerability

Please report security issues **privately**, not as a public issue.

Use GitHub's private vulnerability reporting: go to this repository's **Security** tab
and click **Report a vulnerability** (or use the "Report a vulnerability" button on the
Security Advisories page). This opens a private channel visible only to the maintainer.

Please include what you found, how to reproduce it, and the impact you expect. There is
no formal SLA — this is a small, LLM-maintained project — but reports are read and taken
seriously.

## Exposure model (know before you deploy)

Jukeplox runs with Docker **host networking**, which puts the admin port on all of the
host's interfaces. It is designed to run on a **trusted LAN**:

- **Guest pages need no login** — anyone who can reach the address can browse and queue.
- **The admin surface is password-protected** (set at first run; no reset), but the port
  is open on the LAN.

If the host is reachable from the internet, firewall the port. When serving over HTTPS
(e.g. behind a reverse proxy), set `COOKIE_SECURE=true`. See the README's **Notes →
Security** for details.
