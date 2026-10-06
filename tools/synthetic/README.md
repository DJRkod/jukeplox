# Synthetic devices

Controllable fake devices for testing behaviour that depends on real hardware
appearing and disappearing.

## Why this exists

Issue #47's acceptance criteria all turn on a speaker going away and coming
back. Nothing on the validation rig can power-cycle a real speaker unattended,
so an overnight autonomous run had no way to prove the fix. These fakes make
"the speaker slept" a `stop()` and "the speaker woke" a `start()`.

They are **real servers, not mocks**. Jukeplox talks to them through its
ordinary code paths with nothing stubbed at the seam under test.

## `dlna_renderer.py` — `SyntheticRenderer`

A DLNA MediaRenderer serving genuine device-description and SCPD XML over HTTP,
answering SOAP control actions and accepting event SUBSCRIBEs. Enough for
`DlnaBackend.describe_renderer()`, `probe_device()` and the full `set_device()`
attach (UpnpFactory → NotifyServer → SUBSCRIBE) to succeed against it.

```python
from tools.synthetic.dlna_renderer import SyntheticRenderer

async with SyntheticRenderer(friendly_name="Sleepy Speaker") as r:
    r.location          # http://127.0.0.1:<port>/desc.xml
    r.usn               # uuid:...::urn:schemas-upnp-org:device:MediaRenderer:1
    await r.stop()      # sleeps: socket closed, port released
    await r.start()     # wakes on the SAME port
```

Useful knobs:

| | |
|---|---|
| `port=0` (default) | ephemeral port, **reused** across restarts — a wake looks like the same device at the same address |
| `rotate_port=True` | comes back somewhere else, modelling a changed DHCP lease |
| `.actions` | SOAP actions the controller sent, in order |
| `.subscriptions` | GENA SUBSCRIBE count — a fresh one proves a genuine re-attach |
| `.description_fetches` | description XML fetch count |

Run it standalone to point a real Jukeplox at it:

```bash
python -m tools.synthetic.dlna_renderer
```

### What it does not prove

**The DLNA attach branch only.** Synthetic Chromecast needs protobuf over TLS
and synthetic AirPlay needs RAOP plus pairing; faking those badly would be
worse than not faking them, so those branches rest on unit coverage plus a
hardware pass.

**Not a substitute for a real wake.** A process restart cannot reproduce a
fresh DHCP lease, a genuine mDNS re-announce, a transport torn down by the peer,
or a speaker that takes eight seconds to join wifi. This repo has been bitten
before by device bugs that every test and review missed and only real hardware
surfaced — see `docs/plans/2026-08-17-001-idle-reattach-rig-checklist.md`.

## Process hygiene

Every instance must be stopped. `async with` is the safe form; the pytest
fixtures in `tests/test_idle_reattach_e2e.py` stop theirs in a `finally` so a
failing test still releases its socket. A leaked renderer is a listening socket
that survives the run, which this repo treats as a defect rather than an
inconvenience.
