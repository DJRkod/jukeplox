"""Tests for app.output.mdns_dbus — gi.repository and GLib are fully mocked."""

import asyncio
import struct
import pytest
from unittest.mock import MagicMock, patch, call


# ── helpers ────────────────────────────────────────────────────────────────────

def _make_glib_variant(values):
    """Return a mock GLib.Variant whose .unpack() returns *values*."""
    v = MagicMock()
    v.unpack.return_value = values
    return v


def _make_conn(name_answers: dict[str, bool] | None = None):
    """Return a mock Gio.DBusConnection with controllable call_sync responses."""
    conn = MagicMock()
    if name_answers:
        def _call_sync(service, path, iface, method, params, *args, **kwargs):
            if method == "NameHasOwner":
                name = params.unpack()[0]
                return _make_glib_variant((name_answers.get(name, False),))
            return _make_glib_variant(())
        conn.call_sync.side_effect = _call_sync
    return conn


def _encode_dns_name(name: str) -> bytes:
    """Encode a domain name into DNS wire format (no compression)."""
    parts = b""
    for label in name.split("."):
        encoded = label.encode()
        parts += bytes([len(encoded)]) + encoded
    return parts + b"\x00"


def _make_srv_rdata(port: int, target: str) -> bytes:
    """Build a minimal SRV RDATA (priority=0, weight=0)."""
    return struct.pack(">HHH", 0, 0, port) + _encode_dns_name(target)


def _make_a_rdata(ip: str) -> bytes:
    return bytes(int(p) for p in ip.split("."))


# ── GLib/Gio mock fixture ─────────────────────────────────────────────────────

@pytest.fixture
def gi_mock():
    """Patch gi, GLib, and Gio at import time so mdns_dbus can import them."""
    glib = MagicMock()
    gio = MagicMock()

    # Make GLib.MainContext.new() return a trackable private context mock.
    private_ctx = MagicMock(name="private_ctx")
    glib.MainContext.new.return_value = private_ctx

    # GLib.MainLoop.new() returns a loop mock.
    mock_loop = MagicMock(name="glib_loop")
    glib.MainLoop.new.return_value = mock_loop

    # SOURCE_REMOVE sentinel
    glib.SOURCE_REMOVE = False

    # timeout_source_new returns a source that can be destroyed.
    mock_source = MagicMock(name="timeout_source")
    glib.timeout_source_new.return_value = mock_source

    # GLib.Variant(fmt, args) returns a mock whose .unpack() gives back args.
    # This lets _name_on_bus correctly extract the bus name from the Variant params.
    glib.Variant.side_effect = lambda fmt, args: _make_glib_variant(args)

    # dbus_address_get_for_bus_sync returns a dummy address.
    gio.dbus_address_get_for_bus_sync.return_value = "unix:path=/run/dbus/system_bus_socket"
    gio.BusType.SYSTEM = 1
    gio.DBusConnectionFlags.AUTHENTICATION_CLIENT = 1
    gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION = 4
    gio.DBusCallFlags.NONE = 0
    gio.DBusSignalFlags.NONE = 0

    gi_module = MagicMock()
    gi_module.require_version = MagicMock()

    # gi.repository must expose Gio and GLib as attributes so that
    # `from gi.repository import Gio, GLib` resolves to our specific mocks.
    gi_repo = MagicMock()
    gi_repo.Gio = gio
    gi_repo.GLib = glib

    with patch.dict("sys.modules", {
        "gi": gi_module,
        "gi.repository": gi_repo,
        "gi.repository.Gio": gio,
        "gi.repository.GLib": glib,
    }):
        # Force mdns_dbus to re-import Gio/GLib from our patched modules.
        import importlib, app.output.mdns_dbus as m
        importlib.reload(m)
        yield {
            "glib": glib,
            "gio": gio,
            "loop": mock_loop,
            "ctx": private_ctx,
            "source": mock_source,
            "module": m,
        }


# ── discover() — top-level async wrapper ──────────────────────────────────────

async def test_discover_times_out_returns_none(gi_mock):
    """If _discover_sync hangs, the 15s wait_for ceiling returns None (socket-absent sentinel)."""
    import asyncio as _asyncio

    with patch("app.output.mdns_dbus.asyncio.wait_for", side_effect=_asyncio.TimeoutError):
        result = await gi_mock["module"].discover("_googlecast._tcp.local")
    assert result is None


async def test_discover_returns_none_when_dbus_unavailable(gi_mock):
    """D-Bus socket absent → discover() returns None (not []) to signal socket-absent."""
    gio = gi_mock["gio"]
    gio.dbus_address_get_for_bus_sync.side_effect = Exception("no socket")
    result = await gi_mock["module"].discover("_googlecast._tcp.local")
    assert result is None


async def test_discover_returns_none_when_gio_import_fails():
    """If gi/Gio is not installed, discover() returns None to signal socket-absent."""
    with patch.dict("sys.modules", {"gi": None}):
        import importlib, app.output.mdns_dbus as m
        importlib.reload(m)
        result = await m.discover("_googlecast._tcp.local")
    assert result is None


async def test_discover_returns_empty_when_neither_service_found(gi_mock):
    gio = gi_mock["gio"]
    conn = MagicMock()
    conn.call_sync.return_value = _make_glib_variant((False,))
    gio.DBusConnection.new_for_address_sync.return_value = conn
    result = await gi_mock["module"].discover("_googlecast._tcp.local")
    assert result == []


# ── private GLib.MainContext ──────────────────────────────────────────────────

async def test_avahi_uses_private_main_context_not_default(gi_mock):
    """The avahi signal loop must use a newly created private GLib.MainContext."""
    glib = gi_mock["glib"]
    gio = gi_mock["gio"]

    conn = _make_conn({"org.freedesktop.Avahi": True})
    # ServiceBrowserNew returns an object path.
    conn.call_sync.side_effect = None
    responses = iter([
        _make_glib_variant((True,)),       # NameHasOwner avahi → True
        _make_glib_variant(("/browser1",)),  # ServiceBrowserNew
        _make_glib_variant(()),             # Free
    ])
    conn.call_sync.side_effect = lambda *a, **k: next(responses)
    conn.signal_subscribe.return_value = 1
    gio.DBusConnection.new_for_address_sync.return_value = conn

    await gi_mock["module"].discover("_googlecast._tcp.local")

    # A new private context must have been created (not MainContext.default()).
    glib.MainContext.new.assert_called_once()
    glib.MainContext.default.assert_not_called()

    # The private context must have been used for MainLoop.new().
    private_ctx = gi_mock["ctx"]
    glib.MainLoop.new.assert_called_once_with(private_ctx, False)


# ── avahi happy path ──────────────────────────────────────────────────────────

async def test_avahi_returns_resolved_devices(gi_mock):
    """Avahi path: ItemNew + AllForNow → ResolveService → (name, host, port)."""
    glib = gi_mock["glib"]
    gio = gi_mock["gio"]
    loop_mock = gi_mock["loop"]

    conn = MagicMock()
    gio.DBusConnection.new_for_address_sync.return_value = conn

    captured_callback = [None]

    def _signal_subscribe(*args, **kwargs):
        # 7th positional arg is the callback.
        captured_callback[0] = args[6]
        return 42

    conn.signal_subscribe.side_effect = _signal_subscribe

    # loop.run() calls ItemNew then AllForNow via captured callback.
    def _run_loop():
        cb = captured_callback[0]
        if cb:
            # Simulate ItemNew
            params_item = _make_glib_variant((0, 0, "Living Room", "_googlecast._tcp", "local", 0))
            cb(conn, "org.freedesktop.Avahi", "/browser1", "org.freedesktop.Avahi.ServiceBrowser",
               "ItemNew", params_item, None)
            # Simulate AllForNow
            params_all = _make_glib_variant(())
            cb(conn, "org.freedesktop.Avahi", "/browser1", "org.freedesktop.Avahi.ServiceBrowser",
               "AllForNow", params_all, None)

    loop_mock.run.side_effect = _run_loop

    def _call_sync(service, path, iface, method, params, *args, **kwargs):
        if method == "NameHasOwner":
            name_val = params.unpack()[0]
            return _make_glib_variant((name_val == "org.freedesktop.Avahi",))
        if method == "ServiceBrowserNew":
            return _make_glib_variant(("/browser1",))
        if method == "ResolveService":
            # (interface, protocol, name, type, domain, host, aprotocol, address, port, txt, flags)
            return _make_glib_variant((0, 0, "Living Room", "_googlecast._tcp", "local",
                                       "living-room.local", 0, "192.168.1.10", 8009, [], 0))
        if method == "Free":
            return _make_glib_variant(())
        return _make_glib_variant(())

    conn.call_sync.side_effect = _call_sync

    result = await gi_mock["module"].discover("_googlecast._tcp.local")

    assert result == [("Living Room", "192.168.1.10", 8009, None, {})]


async def test_avahi_returns_uuid_from_txt_records(gi_mock):
    """UUID in avahi TXT records is returned as the 4th tuple element."""
    gio = gi_mock["gio"]
    loop_mock = gi_mock["loop"]
    conn = MagicMock()
    gio.DBusConnection.new_for_address_sync.return_value = conn
    captured_callback = [None]

    def _signal_subscribe(*args, **kwargs):
        captured_callback[0] = args[6]
        return 42

    conn.signal_subscribe.side_effect = _signal_subscribe

    def _run_loop():
        cb = captured_callback[0]
        if cb:
            params_item = _make_glib_variant((0, 0, "Office TV", "_googlecast._tcp", "local", 0))
            cb(conn, "org.freedesktop.Avahi", "/browser1", "org.freedesktop.Avahi.ServiceBrowser",
               "ItemNew", params_item, None)
            params_all = _make_glib_variant(())
            cb(conn, "org.freedesktop.Avahi", "/browser1", "org.freedesktop.Avahi.ServiceBrowser",
               "AllForNow", params_all, None)

    loop_mock.run.side_effect = _run_loop

    # TXT records as list of byte arrays — simulates avahi returning id=<uuid>
    uuid_str = "308c00d1-117f-a74c-600c-b4c97d433fd4"
    txt_records = [list(f"id={uuid_str}".encode())]

    def _call_sync(service, path, iface, method, params, *args, **kwargs):
        if method == "NameHasOwner":
            name_val = params.unpack()[0]
            return _make_glib_variant((name_val == "org.freedesktop.Avahi",))
        if method == "ServiceBrowserNew":
            return _make_glib_variant(("/browser1",))
        if method == "ResolveService":
            return _make_glib_variant((0, 0, "Office TV", "_googlecast._tcp", "local",
                                       "office-tv.local", 0, "192.168.1.11", 8009,
                                       txt_records, 0))
        if method == "Free":
            return _make_glib_variant(())
        return _make_glib_variant(())

    conn.call_sync.side_effect = _call_sync

    result = await gi_mock["module"].discover("_googlecast._tcp.local")

    assert result == [("Office TV", "192.168.1.11", 8009, uuid_str, {"id": uuid_str})]


async def test_avahi_returns_none_uuid_when_txt_missing_id(gi_mock):
    """When TXT records contain no id= entry, UUID is None."""
    gio = gi_mock["gio"]
    loop_mock = gi_mock["loop"]
    conn = MagicMock()
    gio.DBusConnection.new_for_address_sync.return_value = conn
    captured_callback = [None]

    def _signal_subscribe(*args, **kwargs):
        captured_callback[0] = args[6]
        return 42

    conn.signal_subscribe.side_effect = _signal_subscribe

    def _run_loop():
        cb = captured_callback[0]
        if cb:
            params_item = _make_glib_variant((0, 0, "Unknown", "_googlecast._tcp", "local", 0))
            cb(conn, "org.freedesktop.Avahi", "/b", "org.freedesktop.Avahi.ServiceBrowser",
               "ItemNew", params_item, None)
            cb(conn, "org.freedesktop.Avahi", "/b", "org.freedesktop.Avahi.ServiceBrowser",
               "AllForNow", _make_glib_variant(()), None)

    loop_mock.run.side_effect = _run_loop

    def _call_sync(service, path, iface, method, params, *args, **kwargs):
        if method == "NameHasOwner":
            return _make_glib_variant((params.unpack()[0] == "org.freedesktop.Avahi",))
        if method == "ServiceBrowserNew":
            return _make_glib_variant(("/b",))
        if method == "ResolveService":
            # TXT records with unrelated entries, no id=
            txt_records = [list(b"fn=Unknown"), list(b"md=Chromecast")]
            return _make_glib_variant((0, 0, "Unknown", "_googlecast._tcp", "local",
                                       "unknown.local", 0, "192.168.1.99", 8009,
                                       txt_records, 0))
        if method == "Free":
            return _make_glib_variant(())
        return _make_glib_variant(())

    conn.call_sync.side_effect = _call_sync

    result = await gi_mock["module"].discover("_googlecast._tcp.local")
    assert result == [("Unknown", "192.168.1.99", 8009, None, {"fn": "Unknown", "md": "Chromecast"})]


async def test_avahi_all_for_now_empty_does_not_quit_loop(gi_mock):
    """AllForNow with no items must NOT quit the GLib loop.

    avahi fires AllForNow immediately when its cache is empty, then continues
    browsing on the network. We must keep the loop alive so ItemNew signals
    from fresh PTR-query responses can still arrive.
    """
    gio = gi_mock["gio"]
    loop_mock = gi_mock["loop"]
    conn = MagicMock()
    gio.DBusConnection.new_for_address_sync.return_value = conn
    conn.signal_subscribe.return_value = 1

    def _run_loop():
        captured = []
        for call_args in conn.signal_subscribe.call_args_list:
            cb = call_args[0][6] if len(call_args[0]) > 6 else None
            if cb:
                captured.append(cb)
        if captured:
            cb = captured[-1]
            cb(conn, "org.freedesktop.Avahi", "/b",
               "org.freedesktop.Avahi.ServiceBrowser", "AllForNow",
               _make_glib_variant(()), None)

    def _subscribe(*args, **kwargs):
        return 1

    conn.signal_subscribe.side_effect = _subscribe
    loop_mock.run.side_effect = _run_loop

    def _call(service, path, iface, method, params, *args, **kwargs):
        if method == "NameHasOwner":
            return _make_glib_variant((params.unpack()[0] == "org.freedesktop.Avahi",))
        if method == "ServiceBrowserNew":
            return _make_glib_variant(("/b",))
        if method == "Free":
            return _make_glib_variant(())
        return _make_glib_variant(())

    conn.call_sync.side_effect = _call

    await gi_mock["module"].discover("_googlecast._tcp.local")

    loop_mock.quit.assert_not_called()


async def test_avahi_finds_device_responding_after_all_for_now(gi_mock):
    """Device arriving after AllForNow (cold avahi cache) is captured in results.

    avahi cache empty → AllForNow fires immediately → we keep loop running →
    Chromecast responds to fresh PTR query → ItemNew arrives → device returned.
    """
    gio = gi_mock["gio"]
    loop_mock = gi_mock["loop"]
    conn = MagicMock()
    gio.DBusConnection.new_for_address_sync.return_value = conn
    captured_cb = [None]

    def _subscribe(*args, **kwargs):
        captured_cb[0] = args[6]
        return 42

    conn.signal_subscribe.side_effect = _subscribe

    def _run_loop():
        cb = captured_cb[0]
        if not cb:
            return
        # AllForNow fires first — empty cache, must not quit
        cb(conn, "org.freedesktop.Avahi", "/b",
           "org.freedesktop.Avahi.ServiceBrowser", "AllForNow",
           _make_glib_variant(()), None)
        # Chromecast responds to avahi's fresh PTR query
        params_item = _make_glib_variant((0, 0, "Living Room", "_googlecast._tcp", "local", 0))
        cb(conn, "org.freedesktop.Avahi", "/b",
           "org.freedesktop.Avahi.ServiceBrowser", "ItemNew", params_item, None)
        # Timeout fires (simulated via second AllForNow — now items present → quits)
        cb(conn, "org.freedesktop.Avahi", "/b",
           "org.freedesktop.Avahi.ServiceBrowser", "AllForNow",
           _make_glib_variant(()), None)

    loop_mock.run.side_effect = _run_loop

    def _call(service, path, iface, method, params, *args, **kwargs):
        if method == "NameHasOwner":
            return _make_glib_variant((params.unpack()[0] == "org.freedesktop.Avahi",))
        if method == "ServiceBrowserNew":
            return _make_glib_variant(("/b",))
        if method == "ResolveService":
            return _make_glib_variant((0, 0, "Living Room", "_googlecast._tcp", "local",
                                       "lr.local", 0, "192.168.1.10", 8009, [], 0))
        if method == "Free":
            return _make_glib_variant(())
        return _make_glib_variant(())

    conn.call_sync.side_effect = _call

    result = await gi_mock["module"].discover("_googlecast._tcp.local")

    assert result == [("Living Room", "192.168.1.10", 8009, None, {})], (
        "Device responding to avahi's fresh PTR query (after empty AllForNow) must be returned"
    )


async def test_avahi_returns_empty_on_no_items(gi_mock):
    """Avahi AllForNow with no ItemNew → []."""
    gio = gi_mock["gio"]
    loop_mock = gi_mock["loop"]
    conn = MagicMock()
    gio.DBusConnection.new_for_address_sync.return_value = conn
    captured = [None]

    def _subscribe(*args, **kwargs):
        captured[0] = args[6]
        return 1

    conn.signal_subscribe.side_effect = _subscribe

    def _run():
        cb = captured[0]
        if cb:
            cb(conn, "", "/b", "", "AllForNow", _make_glib_variant(()), None)

    loop_mock.run.side_effect = _run

    def _call(service, path, iface, method, params, *args, **kwargs):
        if method == "NameHasOwner":
            return _make_glib_variant((params.unpack()[0] == "org.freedesktop.Avahi",))
        if method == "ServiceBrowserNew":
            return _make_glib_variant(("/b",))
        if method == "Free":
            return _make_glib_variant(())
        return _make_glib_variant(())

    conn.call_sync.side_effect = _call
    result = await gi_mock["module"].discover("_googlecast._tcp.local")
    assert result == []


async def test_avahi_skips_device_on_resolve_failure(gi_mock):
    """If ResolveService raises for one device, that device is skipped."""
    gio = gi_mock["gio"]
    loop_mock = gi_mock["loop"]
    conn = MagicMock()
    gio.DBusConnection.new_for_address_sync.return_value = conn
    captured = [None]

    def _subscribe(*args, **kwargs):
        captured[0] = args[6]
        return 1

    conn.signal_subscribe.side_effect = _subscribe

    def _run():
        cb = captured[0]
        if cb:
            params = _make_glib_variant((0, 0, "Bad Device", "_googlecast._tcp", "local", 0))
            cb(conn, "", "/b", "", "ItemNew", params, None)
            cb(conn, "", "/b", "", "AllForNow", _make_glib_variant(()), None)

    loop_mock.run.side_effect = _run

    call_count = [0]

    def _call(service, path, iface, method, params, *args, **kwargs):
        if method == "NameHasOwner":
            return _make_glib_variant((params.unpack()[0] == "org.freedesktop.Avahi",))
        if method == "ServiceBrowserNew":
            return _make_glib_variant(("/b",))
        if method == "ResolveService":
            raise Exception("D-Bus error: timeout")
        if method == "Free":
            return _make_glib_variant(())
        return _make_glib_variant(())

    conn.call_sync.side_effect = _call
    result = await gi_mock["module"].discover("_googlecast._tcp.local")
    assert result == []  # skipped, not raised


async def test_avahi_strips_local_suffix_in_service_browser_new(gi_mock):
    """ServiceBrowserNew must receive '_googlecast._tcp', not '_googlecast._tcp.local'.

    Avahi's ServiceBrowserNew takes type and domain as separate parameters.
    Passing '_googlecast._tcp.local' as type with domain='local' causes avahi
    to browse '_googlecast._tcp.local.local.' — which never exists.
    """
    gio = gi_mock["gio"]
    loop_mock = gi_mock["loop"]
    conn = MagicMock()
    gio.DBusConnection.new_for_address_sync.return_value = conn
    conn.signal_subscribe.return_value = 1
    captured_svc_type = [None]

    def _call(service, path, iface, method, params, *args, **kwargs):
        if method == "NameHasOwner":
            return _make_glib_variant((params.unpack()[0] == "org.freedesktop.Avahi",))
        if method == "ServiceBrowserNew":
            # GLib.Variant("(iissu)", (-1, -1, svc_type, "local", 0)) — index 2 is svc_type
            captured_svc_type[0] = params.unpack()[2]
            return _make_glib_variant(("/b",))
        if method == "Free":
            return _make_glib_variant(())
        return _make_glib_variant(())

    conn.call_sync.side_effect = _call

    await gi_mock["module"].discover("_googlecast._tcp.local")

    assert captured_svc_type[0] == "_googlecast._tcp", (
        f"ServiceBrowserNew received {captured_svc_type[0]!r}; expected '_googlecast._tcp'. "
        "Passing .local suffix causes avahi to browse _googlecast._tcp.local.local. (no devices found)."
    )


async def test_avahi_timeout_quits_loop(gi_mock):
    """Avahi: a 5s timeout source is attached to the private context."""
    gio = gi_mock["gio"]
    glib = gi_mock["glib"]
    loop_mock = gi_mock["loop"]
    conn = MagicMock()
    gio.DBusConnection.new_for_address_sync.return_value = conn
    conn.signal_subscribe.return_value = 1

    def _call(service, path, iface, method, params, *args, **kwargs):
        if method == "NameHasOwner":
            return _make_glib_variant((params.unpack()[0] == "org.freedesktop.Avahi",))
        if method == "ServiceBrowserNew":
            return _make_glib_variant(("/b",))
        if method == "Free":
            return _make_glib_variant(())
        return _make_glib_variant(())

    conn.call_sync.side_effect = _call

    await gi_mock["module"].discover("_googlecast._tcp.local")

    # A 5000ms timeout source should have been created and attached to our private context.
    glib.timeout_source_new.assert_called_once_with(5000)
    private_ctx = gi_mock["ctx"]
    gi_mock["source"].attach.assert_called_once_with(private_ctx)


# ── systemd-resolved path ─────────────────────────────────────────────────────

async def test_resolved_returns_resolved_devices(gi_mock):
    """systemd-resolved PTR→SRV→A chain returns (name, ip, port)."""
    gio = gi_mock["gio"]
    conn = MagicMock()
    gio.DBusConnection.new_for_address_sync.return_value = conn

    instance = "Living Room._googlecast._tcp.local"
    ptr_data = _encode_dns_name(instance)
    srv_data = _make_srv_rdata(8009, "living-room.local")
    a_data = _make_a_rdata("192.168.1.20")

    call_num = [0]

    def _call(service, path, iface, method, params, *args, **kwargs):
        if method == "NameHasOwner":
            name_v = params.unpack()[0]
            # avahi absent, resolved present
            return _make_glib_variant((name_v == "org.freedesktop.resolve1",))
        if method == "ResolveRecord":
            call_num[0] += 1
            n = call_num[0]
            if n == 1:  # PTR query
                return _make_glib_variant(([(0, 1, 12, list(ptr_data))], 0))
            if n == 2:  # SRV query
                return _make_glib_variant(([(0, 1, 33, list(srv_data))], 0))
            if n == 3:  # A query
                return _make_glib_variant(([(0, 1, 1, list(a_data))], 0))
        return _make_glib_variant(())

    conn.call_sync.side_effect = _call

    result = await gi_mock["module"].discover("_googlecast._tcp.local")

    assert result == [("Living Room", "192.168.1.20", 8009, None, {})]


async def test_resolved_returns_empty_on_ptr_failure(gi_mock):
    """systemd-resolved: PTR query fails → []."""
    gio = gi_mock["gio"]
    conn = MagicMock()
    gio.DBusConnection.new_for_address_sync.return_value = conn

    def _call(service, path, iface, method, params, *args, **kwargs):
        if method == "NameHasOwner":
            return _make_glib_variant((params.unpack()[0] == "org.freedesktop.resolve1",))
        if method == "ResolveRecord":
            raise Exception("DNS timeout")
        return _make_glib_variant(())

    conn.call_sync.side_effect = _call
    result = await gi_mock["module"].discover("_googlecast._tcp.local")
    assert result == []


# ── _parse_dns_name ────────────────────────────────────────────────────────────

def test_parse_dns_name_simple():
    from app.output.mdns_dbus import _parse_dns_name
    data = _encode_dns_name("Living Room._googlecast._tcp.local")
    assert _parse_dns_name(data) == "Living Room._googlecast._tcp.local"


def test_parse_dns_name_with_offset():
    from app.output.mdns_dbus import _parse_dns_name
    # SRV rdata has 6 bytes before the target name.
    srv = _make_srv_rdata(8009, "myhost.local")
    assert _parse_dns_name(srv, 6) == "myhost.local"


def test_parse_dns_name_empty_root():
    from app.output.mdns_dbus import _parse_dns_name
    assert _parse_dns_name(b"\x00") == ""


def test_parse_dns_name_malformed_returns_empty():
    from app.output.mdns_dbus import _parse_dns_name
    # Length byte claims 20 bytes but data is only 5 bytes.
    assert _parse_dns_name(b"\x14abc") == ""


def test_parse_dns_name_compression_pointer_returns_partial(monkeypatch):
    """Compression pointer (0xC0) mid-name: return the labels collected so far, no crash."""
    from app.output.mdns_dbus import _parse_dns_name
    # Encode "foo" then inject a compression pointer byte (0xC0 0x00).
    data = b"\x03foo\xc0\x00"
    result = _parse_dns_name(data)
    assert result == "foo"


async def test_resolved_short_srv_skipped(gi_mock):
    """SRV RDATA with fewer than 7 bytes is silently skipped; discover returns []."""
    gio = gi_mock["gio"]
    conn = MagicMock()
    gio.DBusConnection.new_for_address_sync.return_value = conn

    ptr_data = _encode_dns_name("Broken._googlecast._tcp.local")
    short_srv = b"\x00\x00\x00\x00\x00\x00"  # exactly 6 bytes — below the 7-byte guard

    call_num = [0]

    def _call(service, path, iface, method, params, *args, **kwargs):
        if method == "NameHasOwner":
            return _make_glib_variant((params.unpack()[0] == "org.freedesktop.resolve1",))
        if method == "ResolveRecord":
            call_num[0] += 1
            if call_num[0] == 1:  # PTR query
                return _make_glib_variant(([(0, 1, 12, list(ptr_data))], 0))
            if call_num[0] == 2:  # SRV query — returns short rdata
                return _make_glib_variant(([(0, 1, 33, list(short_srv))], 0))
        return _make_glib_variant(())

    conn.call_sync.side_effect = _call

    result = await gi_mock["module"].discover("_googlecast._tcp.local")
    assert result == []


# ── _parse_txt_uuid ────────────────────────────────────────────────────────────

def _b(s: str) -> list[int]:
    """Encode a TXT record entry to a list of ints (as GLib returns)."""
    return list(s.encode("ascii"))


def test_parse_txt_uuid_valid(gi_mock):
    fn = gi_mock["module"]._parse_txt_uuid
    uuid_str = "308c00d1-117f-a74c-600c-b4c97d433fd4"
    assert fn([_b(f"id={uuid_str}")]) == uuid_str


def test_parse_txt_uuid_malformed_returns_none(gi_mock):
    """Non-RFC-4122 id= value is rejected and returns None."""
    fn = gi_mock["module"]._parse_txt_uuid
    assert fn([_b("id=not-a-valid-uuid")]) is None


def test_parse_txt_uuid_empty_value_returns_none(gi_mock):
    """Empty id= entry (no value after '=') returns None."""
    fn = gi_mock["module"]._parse_txt_uuid
    assert fn([_b("id=")]) is None


def test_parse_txt_uuid_no_id_record_returns_none(gi_mock):
    """TXT records with no id= entry return None."""
    fn = gi_mock["module"]._parse_txt_uuid
    assert fn([_b("md=0"), _b("ve=05")]) is None


def test_parse_txt_uuid_malformed_then_valid_returns_uuid(gi_mock):
    """Malformed id= entry is skipped; a subsequent valid id= entry is returned."""
    fn = gi_mock["module"]._parse_txt_uuid
    uuid_str = "308c00d1-117f-a74c-600c-b4c97d433fd4"
    assert fn([_b("id=not-a-uuid"), _b(f"id={uuid_str}")]) == uuid_str


# ── dbus_discovery_available() — cheap reachability probe (U2/U3) ──────────────

async def test_dbus_discovery_available_true_when_avahi_present(gi_mock):
    """avahi owns its bus name → a browsable daemon is reachable → True."""
    gio = gi_mock["gio"]
    conn = _make_conn({"org.freedesktop.Avahi": True})
    gio.DBusConnection.new_for_address_sync.return_value = conn
    assert await gi_mock["module"].dbus_discovery_available() is True


async def test_dbus_discovery_available_true_when_only_resolved_present(gi_mock):
    """systemd-resolved owns its name (avahi absent) → still reachable → True."""
    gio = gi_mock["gio"]
    conn = _make_conn({"org.freedesktop.resolve1": True})
    gio.DBusConnection.new_for_address_sync.return_value = conn
    assert await gi_mock["module"].dbus_discovery_available() is True


async def test_dbus_discovery_available_false_when_no_daemon(gi_mock):
    """D-Bus reachable but neither avahi nor resolved owns a name → False
    (the discovery-unavailable banner should fire)."""
    gio = gi_mock["gio"]
    conn = _make_conn({"org.freedesktop.Avahi": False,
                       "org.freedesktop.resolve1": False})
    gio.DBusConnection.new_for_address_sync.return_value = conn
    assert await gi_mock["module"].dbus_discovery_available() is False


async def test_dbus_discovery_available_false_when_socket_absent(gi_mock):
    """System bus socket not mounted → connect raises → False."""
    gio = gi_mock["gio"]
    gio.dbus_address_get_for_bus_sync.side_effect = Exception("no socket")
    assert await gi_mock["module"].dbus_discovery_available() is False


async def test_dbus_discovery_available_false_when_gio_import_fails():
    """PyGObject/Gio not installed → False, never raises."""
    with patch.dict("sys.modules", {"gi": None}):
        import importlib, app.output.mdns_dbus as m
        importlib.reload(m)
        assert await m.dbus_discovery_available() is False
