"""D-Bus mDNS discovery helper.

Queries the host's mDNS daemon (avahi or systemd-resolved) via D-Bus to
discover services without binding UDP 5353.  Used as a fallback when
AsyncZeroconf raises EADDRINUSE at startup.

All GLib/GIO calls are blocking; call the async wrappers — discover() and
dbus_discovery_available(), which wrap everything in run_in_executor — from
the asyncio event loop, never the _*_sync() helpers directly.

This module is one-shot only: each discover() builds and tears down a private
MainContext/MainLoop/DBusConnection per call. The watcher reaches Cast/AirPlay
over D-Bus by polling discover() on its periodic sweep (see
app/output/watcher.py). The former persistent avahi ServiceBrowser
subscription was retired in 2026-06-16 — its GLib cross-thread context
handling delivered no live events, so the sweep replaced it.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import uuid as _uuid_mod

_log = logging.getLogger(__name__)


async def discover(service_type: str) -> list[tuple[str, str, int, str | None, dict[str, str]]] | None:
    """Return ``(name, host, port, uuid, txt)`` tuples for *service_type* via D-Bus mDNS.

    ``uuid`` is the device's Cast UUID when present in mDNS TXT records (avahi
    path only), or ``None`` when unavailable (systemd-resolved path, or devices
    without an ``id=`` TXT entry).

    ``txt`` is the device's full TXT-record dict (parsed key=value entries) when
    present (avahi path only).  Empty dict on the systemd-resolved path, which
    does not expose TXT records via ``ResolveRecord``.  pyatv's RAOP backend
    requires these properties for RTSP SETUP authentication negotiation.

    Returns ``None`` when the system D-Bus socket is unreachable (socket absent,
    GLib/Gio not installed).  Returns ``[]`` when D-Bus is reachable but no
    matching services were found.  Never raises.
    """
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, _discover_sync, service_type),
            timeout=15.0,
        )
    except asyncio.TimeoutError:
        _log.warning("mdns_dbus.discover: timed out after 15s for %s", service_type)
        return None
    except Exception:
        _log.debug("mdns_dbus.discover: unexpected error", exc_info=True)
        return []


async def dbus_discovery_available() -> bool:
    """True when a *browsable* mDNS daemon (avahi, or systemd-resolved) is
    reachable over the system D-Bus.

    Cheap reachability signal — connects to the system bus and asks whether
    the daemon owns its name. No ServiceBrowser, no full discover. Used by
    the watcher's sweep-mode health marker and the watcher-absent banner to
    distinguish "discovery genuinely unavailable" (no host networking AND no
    D-Bus socket → actionable guidance) from "reachable but no devices".
    Runs the blocking GLib/Gio probe in the executor (same pattern as
    discover()); never raises.
    """
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, _dbus_available_sync),
            timeout=5.0,
        )
    except Exception:
        _log.debug("mdns_dbus.dbus_discovery_available: probe failed", exc_info=True)
        return False


# ── internal synchronous helpers ──────────────────────────────────────────────

def _dbus_available_sync() -> bool:
    """Blocking reachability probe for dbus_discovery_available — must run in
    a thread pool, not the event loop. Returns False on any failure."""
    try:
        import gi  # type: ignore
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib  # type: ignore  # noqa: F401
    except Exception:
        return False
    ctx = GLib.MainContext.new()
    ctx.push_thread_default()
    try:
        try:
            addr = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SYSTEM, None)
            conn = Gio.DBusConnection.new_for_address_sync(
                addr,
                Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                None,
                None,
            )
        except Exception:
            return False  # system bus socket absent / not mounted
        try:
            return (_name_on_bus(conn, "org.freedesktop.Avahi")
                    or _name_on_bus(conn, "org.freedesktop.resolve1"))
        finally:
            try:
                conn.close_sync(None)
            except Exception:
                pass
    finally:
        ctx.pop_thread_default()

def _discover_sync(service_type: str) -> list[tuple[str, str, int, str | None, dict[str, str]]] | None:
    """Blocking entry point — must run in a thread pool, not the event loop.

    Returns ``None`` when D-Bus is unreachable, ``[]`` when reachable but empty.
    """
    try:
        import gi  # type: ignore
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib  # type: ignore  # noqa: F401
    except Exception as exc:
        _log.warning("mdns_dbus: PyGObject/Gio not available — D-Bus mDNS fallback disabled: %s", exc)
        return None

    # A private GLib.MainContext keeps avahi signal dispatch isolated from
    # GStreamer's existing GLib event loop which owns the default context.
    ctx = GLib.MainContext.new()
    loop = GLib.MainLoop.new(ctx, False)
    ctx.push_thread_default()
    try:
        return _discover_in_context(service_type, ctx, loop)
    except Exception:
        _log.warning("mdns_dbus: discovery failed", exc_info=True)
        return []
    finally:
        ctx.pop_thread_default()


def _discover_in_context(
    service_type: str,
    ctx,  # GLib.MainContext
    glib_loop,  # GLib.MainLoop
) -> list[tuple[str, str, int, str | None, dict[str, str]]] | None:
    from gi.repository import Gio, GLib

    # Create a private D-Bus connection inside our private context so that
    # incoming signals are dispatched on ctx, not the default context.
    try:
        addr = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SYSTEM, None)
        conn = Gio.DBusConnection.new_for_address_sync(
            addr,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None,
            None,
        )
    except Exception as exc:
        _log.warning("mdns_dbus: cannot connect to system D-Bus — %s "
                     "(is /run/dbus/system_bus_socket mounted in the container?)", exc)
        return None  # socket absent — callers use this to distinguish from "no devices"

    try:
        if _name_on_bus(conn, "org.freedesktop.Avahi"):
            _log.debug("mdns_dbus: using avahi for %s", service_type)
            return _avahi_discover(conn, service_type, ctx, glib_loop)
        if _name_on_bus(conn, "org.freedesktop.resolve1"):
            _log.debug("mdns_dbus: using systemd-resolved for %s", service_type)
            return _resolved_discover(conn, service_type)
        _log.debug("mdns_dbus: neither avahi nor systemd-resolved found on D-Bus")
        return []
    finally:
        try:
            conn.close_sync(None)
        except Exception:
            pass


def _name_on_bus(conn, name: str) -> bool:
    """Return True if *name* is currently owned on the bus."""
    from gi.repository import Gio, GLib
    try:
        result = conn.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameHasOwner",
            GLib.Variant("(s)", (name,)),
            GLib.VariantType("(b)"),
            Gio.DBusCallFlags.NONE,
            2000,
            None,
        )
        return bool(result.unpack()[0])
    except Exception:
        return False


# ── avahi path ────────────────────────────────────────────────────────────────

def _avahi_discover(
    conn,
    service_type: str,
    ctx,  # private GLib.MainContext
    glib_loop,  # GLib.MainLoop on that context
) -> list[tuple[str, str, int, str | None, dict[str, str]]]:
    from gi.repository import Gio, GLib

    # avahi ServiceBrowserNew's 'type' parameter must not include the domain.
    # Callers pass "_googlecast._tcp.local"; strip the suffix so avahi does not
    # browse "_googlecast._tcp.local.local." which never exists.
    svc_type = service_type.rstrip(".").removesuffix(".local")

    try:
        reply = conn.call_sync(
            "org.freedesktop.Avahi",
            "/",
            "org.freedesktop.Avahi.Server",
            "ServiceBrowserNew",
            GLib.Variant("(iissu)", (-1, -1, svc_type, "local", 0)),
            GLib.VariantType("(o)"),
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
    except Exception:
        _log.debug("mdns_dbus: avahi ServiceBrowserNew failed", exc_info=True)
        return []

    browser_path = reply.unpack()[0]
    found_items: list[tuple] = []

    def _on_signal(conn, sender, path, iface, signal, params, user_data):
        if signal == "ItemNew":
            iface_idx, protocol, name, stype, domain, flags = params.unpack()
            found_items.append((iface_idx, protocol, name, stype, domain))
        elif signal == "AllForNow":
            _log.debug("mdns_dbus: avahi AllForNow for %s — %d item(s) so far",
                       service_type, len(found_items))
            if found_items:
                # Cache was warm — all entries delivered, exit early.
                glib_loop.quit()
            # No items yet: avahi cache is empty. Keep the loop running so we
            # catch device responses to avahi's fresh PTR query (up to 5s timeout).
        elif signal == "Failure":
            _log.warning("mdns_dbus: avahi Failure signal for %s — params: %s",
                         service_type, params.unpack() if params else "(none)")
            glib_loop.quit()

    sub_id = conn.signal_subscribe(
        "org.freedesktop.Avahi",
        "org.freedesktop.Avahi.ServiceBrowser",
        None,  # subscribe to all signals on this interface
        browser_path,
        None,
        Gio.DBusSignalFlags.NONE,
        _on_signal,
        None,
    )

    def _on_timeout():
        _log.warning("mdns_dbus: avahi browse timed out after 5s for %s — %d item(s) found",
                     service_type, len(found_items))
        glib_loop.quit()
        return GLib.SOURCE_REMOVE

    # Attach the timeout to our private context explicitly so it fires there.
    timeout_source = GLib.timeout_source_new(5000)
    timeout_source.set_callback(_on_timeout)
    timeout_source.attach(ctx)

    try:
        glib_loop.run()
    finally:
        timeout_source.destroy()
        conn.signal_unsubscribe(sub_id)

    try:
        conn.call_sync(
            "org.freedesktop.Avahi",
            browser_path,
            "org.freedesktop.Avahi.ServiceBrowser",
            "Free",
            None,
            None,
            Gio.DBusCallFlags.NONE,
            1000,
            None,
        )
    except Exception:
        pass

    results: list[tuple[str, str, int, str | None, dict[str, str]]] = []
    for iface_idx, protocol, name, stype, domain in found_items:
        try:
            reply = conn.call_sync(
                "org.freedesktop.Avahi",
                "/",
                "org.freedesktop.Avahi.Server",
                "ResolveService",
                GLib.Variant(
                    "(iisssiu)",
                    (iface_idx, protocol, name, stype, domain, -1, 0),
                ),
                GLib.VariantType("(iissssisqaayu)"),
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )
            vals = reply.unpack()
            # (interface, protocol, name, type, domain, host, aprotocol, address, port, txt, flags)
            svc_name = vals[2]
            address = vals[7]
            port = int(vals[8])
            txt = _parse_txt_dict(vals[9])
            uuid = _parse_txt_uuid(vals[9])
            results.append((svc_name, address, port, uuid, txt))
        except Exception:
            _log.warning("mdns_dbus: avahi ResolveService failed for %r", name, exc_info=True)

    return results


# ── systemd-resolved path ─────────────────────────────────────────────────────

_RESOLVED_SERVICE = "org.freedesktop.resolve1"
_RESOLVED_OBJECT = "/org/freedesktop/resolve1"
_RESOLVED_IFACE = "org.freedesktop.resolve1.Manager"


def _resolved_discover(conn, service_type: str) -> list[tuple[str, str, int, str | None, dict[str, str]]]:
    from gi.repository import Gio, GLib

    # PTR query — type 12, class IN (1).
    try:
        reply = conn.call_sync(
            _RESOLVED_SERVICE,
            _RESOLVED_OBJECT,
            _RESOLVED_IFACE,
            "ResolveRecord",
            GLib.Variant("(isqqt)", (0, service_type, 1, 12, 0)),
            GLib.VariantType("(a(iqqay)t)"),
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
    except Exception:
        _log.debug("mdns_dbus: resolved PTR query failed", exc_info=True)
        return []

    records, _flags = reply.unpack()
    instance_names = [_parse_dns_name(bytes(data)) for _, _, _, data in records]
    instance_names = [n for n in instance_names if n]

    results: list[tuple[str, str, int, str | None, dict[str, str]]] = []
    for instance in instance_names:
        # SRV query — type 33.
        try:
            reply = conn.call_sync(
                _RESOLVED_SERVICE,
                _RESOLVED_OBJECT,
                _RESOLVED_IFACE,
                "ResolveRecord",
                GLib.Variant("(isqqt)", (0, instance, 1, 33, 0)),
                GLib.VariantType("(a(iqqay)t)"),
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )
        except Exception:
            _log.debug("mdns_dbus: resolved SRV query failed for %r", instance, exc_info=True)
            continue

        srv_records, _ = reply.unpack()
        for _, _, _, data in srv_records:
            data = bytes(data)
            if len(data) < 7:
                continue
            port = struct.unpack_from(">H", data, 4)[0]
            target = _parse_dns_name(data, 6)
            if not target:
                continue

            # A query — type 1.
            try:
                reply = conn.call_sync(
                    _RESOLVED_SERVICE,
                    _RESOLVED_OBJECT,
                    _RESOLVED_IFACE,
                    "ResolveRecord",
                    GLib.Variant("(isqqt)", (0, target, 1, 1, 0)),
                    GLib.VariantType("(a(iqqay)t)"),
                    Gio.DBusCallFlags.NONE,
                    5000,
                    None,
                )
            except Exception:
                _log.debug("mdns_dbus: resolved A query failed for %r", target, exc_info=True)
                continue

            a_records, _ = reply.unpack()
            for _, _, _, ip_data in a_records:
                ip_data = bytes(ip_data)
                if len(ip_data) == 4:
                    ip = ".".join(str(b) for b in ip_data)
                    # Use the first label of the instance name as the friendly name.
                    friendly = instance.split(".")[0]
                    # systemd-resolved path: TXT records not fetched (would need
                    # separate type-16 queries per instance); return empty dict.
                    results.append((friendly, ip, port, None, {}))
                    break  # first valid A record is enough
            break  # first valid SRV record is enough

    return results


def _parse_txt_dict(txt_records: list) -> dict[str, str]:
    """Parse avahi TXT records into a ``{key: value}`` dict.

    *txt_records* is the GLib-unpacked ``aay`` value from avahi's
    ``ResolveService``: a list of byte sequences, each encoding one TXT
    ``key=value`` entry.  Returns a string dict; entries without ``=`` are
    stored as ``{key: ""}``.  Malformed entries are silently skipped so a
    single bad record cannot poison the result.

    pyatv's RAOP backend reads this dict via ``RaopService.properties`` to
    negotiate RTSP authentication, encryption type, and codec selection.
    """
    result: dict[str, str] = {}
    for record in txt_records:
        try:
            entry = bytes(record).decode("ascii", errors="replace")
            key, _, value = entry.partition("=")
            if key:
                result[key] = value
        except Exception:
            pass
    return result


def _parse_txt_uuid(txt_records: list) -> str | None:
    """Extract Cast device UUID from avahi TXT records (``id=<uuid>`` entry).

    *txt_records* is the GLib-unpacked ``aayu`` value from ``ResolveService``:
    a list of byte-sequences, each encoding one TXT key=value entry.
    Returns the validated UUID string or ``None`` if absent, empty, or malformed.
    """
    for record in txt_records:
        try:
            entry = bytes(record).decode("ascii", errors="replace")
            if entry.startswith("id="):
                val = entry[3:]
                if not val:
                    continue
                try:
                    _uuid_mod.UUID(val)
                    return val
                except ValueError:
                    _log.debug("mdns_dbus: ignoring malformed id= TXT value %r", val)
                    continue
        except Exception:
            pass
    return None


def _parse_dns_name(data: bytes, offset: int = 0) -> str:
    """Parse a DNS domain name from wire-format *data* starting at *offset*.

    Returns an empty string if the data is malformed.
    """
    parts: list[str] = []
    pos = offset
    while pos < len(data):
        length = data[pos]
        pos += 1
        if length == 0:
            break
        if (length & 0xC0) == 0xC0:
            # Compression pointer — not expected in RDATA returned by resolved,
            # but skip gracefully rather than crashing.
            pos += 1
            break
        if pos + length > len(data):
            return ""
        label = data[pos : pos + length]
        parts.append(label.decode("ascii", errors="replace"))
        pos += length
    return ".".join(parts)

