"""A controllable synthetic DLNA MediaRenderer.

Built for the idle-reattach work (issue #47): the acceptance criteria all turn
on a device disappearing and coming back, and nothing on the validation rig can
power-cycle a real speaker unattended. This renderer makes "the speaker slept"
a ``stop()`` and "the speaker woke" a ``start()``.

It is deliberately a REAL server, not a mock: it serves genuine device-
description and SCPD XML over HTTP, answers SOAP control actions, and accepts
event SUBSCRIBEs. Jukeplox's DLNA backend talks to it through its ordinary
``UpnpFactory`` path with nothing stubbed, so the e2e exercises the real
description fetch, the real service parse, and the real attach.

WHAT THIS DOES NOT PROVE
------------------------
Only the DLNA attach branch. Synthetic Chromecast needs protobuf over TLS and
synthetic AirPlay needs RAOP plus pairing; faking those badly would be worse
than not faking them. The Cast/AirPlay branches rest on unit coverage plus the
hardware pass in the rig checklist. It also cannot reproduce what a genuine
wake does — fresh DHCP lease, real mDNS re-announce, transport torn down by the
peer — which is the other half of why the hardware pass is not optional.

Usage (also see tools/synthetic/README.md)::

    r = SyntheticRenderer(friendly_name="Sleepy Speaker")
    await r.start()
    r.location          # -> http://127.0.0.1:<port>/desc.xml
    r.usn               # -> uuid:<stable-id>::urn:...:MediaRenderer:1
    await r.stop()      # the speaker "sleeps" — socket closed, port released
    await r.start()     # wakes on the SAME port unless port=0 was requested

Every instance must be stopped; ``async with`` is the safe form. Leaking one
leaves a listening socket behind, which the repo's process-hygiene standard
treats as a defect, not an inconvenience.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

_log = logging.getLogger(__name__)

MEDIA_RENDERER_TYPE = "urn:schemas-upnp-org:device:MediaRenderer:1"
AVTRANSPORT_TYPE = "urn:schemas-upnp-org:service:AVTransport:1"
RENDERING_CONTROL_TYPE = "urn:schemas-upnp-org:service:RenderingControl:1"


def _device_description(udn: str, friendly_name: str, base: str) -> str:
    """Minimal-but-valid MediaRenderer description.

    The backend's discovery check is a PREFIX match on ``<deviceType>`` and a
    non-empty ``<friendlyName>``; its attach path additionally needs a service
    whose key contains ``AVTransport``. RenderingControl is included because a
    renderer without it is unusual enough that a real device parser might
    reasonably object.
    """
    return f"""<?xml version="1.0" encoding="utf-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <device>
    <deviceType>{MEDIA_RENDERER_TYPE}</deviceType>
    <friendlyName>{friendly_name}</friendlyName>
    <manufacturer>Jukeplox Test Harness</manufacturer>
    <modelName>Synthetic Renderer</modelName>
    <modelNumber>1.0</modelNumber>
    <UDN>{udn}</UDN>
    <serviceList>
      <service>
        <serviceType>{AVTRANSPORT_TYPE}</serviceType>
        <serviceId>urn:upnp-org:serviceId:AVTransport</serviceId>
        <SCPDURL>{base}/AVTransport.xml</SCPDURL>
        <controlURL>{base}/AVTransport/control</controlURL>
        <eventSubURL>{base}/AVTransport/event</eventSubURL>
      </service>
      <service>
        <serviceType>{RENDERING_CONTROL_TYPE}</serviceType>
        <serviceId>urn:upnp-org:serviceId:RenderingControl</serviceId>
        <SCPDURL>{base}/RenderingControl.xml</SCPDURL>
        <controlURL>{base}/RenderingControl/control</controlURL>
        <eventSubURL>{base}/RenderingControl/event</eventSubURL>
      </service>
    </serviceList>
  </device>
</root>"""


# The action set Jukeplox actually drives, plus the state variables those
# actions reference. A trimmed SCPD is fine — the factory parses what is
# declared — but every argument's relatedStateVariable must resolve or the
# service fails to build.
_AVTRANSPORT_SCPD = """<?xml version="1.0" encoding="utf-8"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action>
      <name>SetAVTransportURI</name>
      <argumentList>
        <argument><name>InstanceID</name><direction>in</direction>
          <relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
        <argument><name>CurrentURI</name><direction>in</direction>
          <relatedStateVariable>AVTransportURI</relatedStateVariable></argument>
        <argument><name>CurrentURIMetaData</name><direction>in</direction>
          <relatedStateVariable>AVTransportURIMetaData</relatedStateVariable></argument>
      </argumentList>
    </action>
    <action>
      <name>SetNextAVTransportURI</name>
      <argumentList>
        <argument><name>InstanceID</name><direction>in</direction>
          <relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
        <argument><name>NextURI</name><direction>in</direction>
          <relatedStateVariable>NextAVTransportURI</relatedStateVariable></argument>
        <argument><name>NextURIMetaData</name><direction>in</direction>
          <relatedStateVariable>NextAVTransportURIMetaData</relatedStateVariable></argument>
      </argumentList>
    </action>
    <action>
      <name>Play</name>
      <argumentList>
        <argument><name>InstanceID</name><direction>in</direction>
          <relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
        <argument><name>Speed</name><direction>in</direction>
          <relatedStateVariable>TransportPlaySpeed</relatedStateVariable></argument>
      </argumentList>
    </action>
    <action>
      <name>Pause</name>
      <argumentList>
        <argument><name>InstanceID</name><direction>in</direction>
          <relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
      </argumentList>
    </action>
    <action>
      <name>Stop</name>
      <argumentList>
        <argument><name>InstanceID</name><direction>in</direction>
          <relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
      </argumentList>
    </action>
    <action>
      <name>GetTransportInfo</name>
      <argumentList>
        <argument><name>InstanceID</name><direction>in</direction>
          <relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
        <argument><name>CurrentTransportState</name><direction>out</direction>
          <relatedStateVariable>TransportState</relatedStateVariable></argument>
        <argument><name>CurrentTransportStatus</name><direction>out</direction>
          <relatedStateVariable>TransportStatus</relatedStateVariable></argument>
        <argument><name>CurrentSpeed</name><direction>out</direction>
          <relatedStateVariable>TransportPlaySpeed</relatedStateVariable></argument>
      </argumentList>
    </action>
    <action>
      <name>GetPositionInfo</name>
      <argumentList>
        <argument><name>InstanceID</name><direction>in</direction>
          <relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
        <argument><name>Track</name><direction>out</direction>
          <relatedStateVariable>CurrentTrack</relatedStateVariable></argument>
        <argument><name>TrackDuration</name><direction>out</direction>
          <relatedStateVariable>CurrentTrackDuration</relatedStateVariable></argument>
        <argument><name>TrackURI</name><direction>out</direction>
          <relatedStateVariable>CurrentTrackURI</relatedStateVariable></argument>
        <argument><name>RelTime</name><direction>out</direction>
          <relatedStateVariable>RelativeTimePosition</relatedStateVariable></argument>
      </argumentList>
    </action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_InstanceID</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>AVTransportURI</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>AVTransportURIMetaData</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>NextAVTransportURI</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>NextAVTransportURIMetaData</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>TransportPlaySpeed</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="yes"><name>TransportState</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>TransportStatus</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>CurrentTrack</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>CurrentTrackDuration</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>CurrentTrackURI</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>RelativeTimePosition</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="yes"><name>LastChange</name><dataType>string</dataType></stateVariable>
  </serviceStateTable>
</scpd>"""

_RENDERING_CONTROL_SCPD = """<?xml version="1.0" encoding="utf-8"?>
<scpd xmlns="urn:schemas-upnp-org:service-1-0">
  <specVersion><major>1</major><minor>0</minor></specVersion>
  <actionList>
    <action>
      <name>SetVolume</name>
      <argumentList>
        <argument><name>InstanceID</name><direction>in</direction>
          <relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
        <argument><name>Channel</name><direction>in</direction>
          <relatedStateVariable>A_ARG_TYPE_Channel</relatedStateVariable></argument>
        <argument><name>DesiredVolume</name><direction>in</direction>
          <relatedStateVariable>Volume</relatedStateVariable></argument>
      </argumentList>
    </action>
    <action>
      <name>GetVolume</name>
      <argumentList>
        <argument><name>InstanceID</name><direction>in</direction>
          <relatedStateVariable>A_ARG_TYPE_InstanceID</relatedStateVariable></argument>
        <argument><name>Channel</name><direction>in</direction>
          <relatedStateVariable>A_ARG_TYPE_Channel</relatedStateVariable></argument>
        <argument><name>CurrentVolume</name><direction>out</direction>
          <relatedStateVariable>Volume</relatedStateVariable></argument>
      </argumentList>
    </action>
  </actionList>
  <serviceStateTable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_InstanceID</name><dataType>ui4</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>A_ARG_TYPE_Channel</name><dataType>string</dataType></stateVariable>
    <stateVariable sendEvents="no"><name>Volume</name><dataType>ui2</dataType></stateVariable>
    <stateVariable sendEvents="yes"><name>LastChange</name><dataType>string</dataType></stateVariable>
  </serviceStateTable>
</scpd>"""


def _soap_envelope(body: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f"<s:Body>{body}</s:Body></s:Envelope>"
    )


class SyntheticRenderer:
    """A DLNA MediaRenderer you can switch off and on.

    ``port=0`` picks an ephemeral port on first start and REUSES it across
    restarts, so a wake looks like the same device at the same address. Pass an
    explicit port only when a test needs to predict the URL up front; pass
    ``rotate_port=True`` to model a device that came back somewhere else (the
    DHCP-reuse case the attach path's identity check guards).
    """

    def __init__(self, *, friendly_name: str = "Synthetic Renderer",
                 udn: str = "uuid:jukeplox-synthetic-0001",
                 host: str = "127.0.0.1", port: int = 0,
                 rotate_port: bool = False) -> None:
        self.friendly_name = friendly_name
        self.udn = udn
        self.host = host
        self._requested_port = port
        self._port: int | None = None if port == 0 else port
        self._rotate_port = rotate_port
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        # Observability for assertions: what the controller actually sent.
        self.actions: list[str] = []
        self.subscriptions = 0
        self.description_fetches = 0

    # ── identity ──────────────────────────────────────────────────────────

    @property
    def usn(self) -> str:
        """The id Jukeplox stores as the device_id (USN from an SSDP hit)."""
        return f"{self.udn}::{MEDIA_RENDERER_TYPE}"

    @property
    def port(self) -> int:
        if self._port is None:
            raise RuntimeError("renderer has never been started — no port yet")
        return self._port

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def location(self) -> str:
        return f"{self.base_url}/desc.xml"

    @property
    def running(self) -> bool:
        return self._site is not None

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> "SyntheticRenderer":
        if self.running:
            return self
        app = web.Application()
        app.router.add_get("/desc.xml", self._handle_description)
        app.router.add_get("/AVTransport.xml", self._handle_avtransport_scpd)
        app.router.add_get("/RenderingControl.xml",
                           self._handle_rendering_scpd)
        app.router.add_post("/AVTransport/control", self._handle_control)
        app.router.add_post("/RenderingControl/control", self._handle_control)
        for path in ("/AVTransport/event", "/RenderingControl/event"):
            app.router.add_route("SUBSCRIBE", path, self._handle_subscribe)
            app.router.add_route("UNSUBSCRIBE", path, self._handle_unsubscribe)

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        port = 0 if (self._port is None or self._rotate_port) else self._port
        self._site = web.TCPSite(self._runner, self.host, port)
        await self._site.start()
        if port == 0:
            # Read back whatever the OS handed us so restarts can reuse it.
            sockets = getattr(self._site._server, "sockets", None) or ()
            self._port = sockets[0].getsockname()[1] if sockets else None
        _log.info("synthetic renderer %r listening at %s",
                  self.friendly_name, self.location)
        return self

    async def stop(self) -> None:
        """The speaker goes to sleep: socket closed, port released."""
        site, self._site = self._site, None
        runner, self._runner = self._runner, None
        if site is not None:
            try:
                await site.stop()
            except Exception:
                _log.debug("synthetic renderer: site stop failed",
                           exc_info=True)
        if runner is not None:
            try:
                await runner.cleanup()
            except Exception:
                _log.debug("synthetic renderer: runner cleanup failed",
                           exc_info=True)

    async def __aenter__(self) -> "SyntheticRenderer":
        return await self.start()

    async def __aexit__(self, *_exc: Any) -> None:
        await self.stop()

    # ── handlers ──────────────────────────────────────────────────────────

    async def _handle_description(self, _request: web.Request) -> web.Response:
        self.description_fetches += 1
        body = _device_description(self.udn, self.friendly_name, self.base_url)
        return web.Response(text=body, content_type="text/xml")

    async def _handle_avtransport_scpd(self, _r: web.Request) -> web.Response:
        return web.Response(text=_AVTRANSPORT_SCPD, content_type="text/xml")

    async def _handle_rendering_scpd(self, _r: web.Request) -> web.Response:
        return web.Response(text=_RENDERING_CONTROL_SCPD,
                            content_type="text/xml")

    async def _handle_control(self, request: web.Request) -> web.Response:
        """SOAP control. Canned success responses — this harness exists to
        exercise DISCOVERY and ATTACH, not transport semantics."""
        soap_action = request.headers.get("SOAPACTION", "").strip('"')
        action = soap_action.rsplit("#", 1)[-1] if soap_action else "unknown"
        self.actions.append(action)
        await request.read()

        if action == "GetTransportInfo":
            body = (
                '<u:GetTransportInfoResponse xmlns:u="%s">'
                "<CurrentTransportState>STOPPED</CurrentTransportState>"
                "<CurrentTransportStatus>OK</CurrentTransportStatus>"
                "<CurrentSpeed>1</CurrentSpeed>"
                "</u:GetTransportInfoResponse>" % AVTRANSPORT_TYPE
            )
        elif action == "GetPositionInfo":
            body = (
                '<u:GetPositionInfoResponse xmlns:u="%s">'
                "<Track>1</Track>"
                "<TrackDuration>00:03:00</TrackDuration>"
                "<TrackURI></TrackURI>"
                "<RelTime>00:00:00</RelTime>"
                "</u:GetPositionInfoResponse>" % AVTRANSPORT_TYPE
            )
        elif action == "GetVolume":
            body = (
                '<u:GetVolumeResponse xmlns:u="%s">'
                "<CurrentVolume>50</CurrentVolume>"
                "</u:GetVolumeResponse>" % RENDERING_CONTROL_TYPE
            )
        else:
            body = f'<u:{action}Response xmlns:u="{AVTRANSPORT_TYPE}"/>'
        return web.Response(text=_soap_envelope(body), content_type="text/xml")

    async def _handle_subscribe(self, _request: web.Request) -> web.Response:
        self.subscriptions += 1
        return web.Response(
            status=200,
            headers={"SID": f"uuid:synthetic-sub-{self.subscriptions}",
                     "TIMEOUT": "Second-1800"},
        )

    async def _handle_unsubscribe(self, _request: web.Request) -> web.Response:
        return web.Response(status=200)


async def _demo() -> None:  # pragma: no cover - manual smoke helper
    async with SyntheticRenderer() as r:
        print(f"location: {r.location}\nusn:      {r.usn}")
        await asyncio.sleep(3600)


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(_demo())
    except KeyboardInterrupt:
        pass
