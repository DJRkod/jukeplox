"""In-process mDNS browse source via python-zeroconf.

A drop-in alternative to ``app/output/mdns_dbus``'s ``subscribe()`` /
``unsubscribe()`` / ``subscriptions_supported()`` contract, but sourced from
an in-process ``AsyncServiceBrowser`` on the shared ``AsyncZeroconf`` instead
of a host avahi daemon reached over a D-Bus socket.  The watcher consumes the
*same* event contract either way, so swapping the source leaves the
registry / grace / debounce / broadcast state machine untouched (2026-06-15
passive-discovery plan U1).

Events delivered to ``on_event(kind, payload)`` — identical shape to
``mdns_dbus``:

- ``'new'``    — ``(name, host, port, uuid, txt, service_type)``.  ``name`` is
  the announced mDNS instance label (service-type suffix stripped, matching
  the avahi shape ``register_resolved`` expects); ``uuid`` is the Cast ``id=``
  TXT value when present, else ``None``; ``txt`` is the decoded TXT dict.
- ``'remove'`` — ``(name, service_type)`` keyed by the instance label.
- ``'status'`` — ``'up'`` once the browser is established, ``'down'`` is not
  emitted here (a python-zeroconf browser on a live ``AsyncZeroconf`` does not
  drop the way an avahi bus connection does; degraded detection is the U6
  browser-established flag, fed by whether ``subscribe`` returned a handle).

Thread model: ``AsyncServiceBrowser`` handlers may fire on zeroconf's engine
thread, not the asyncio loop.  We capture the loop at subscribe time and cross
every event back via ``call_soon_threadsafe`` (the proven ``mdns_dbus`` shape),
scheduling the ``ServiceInfo`` resolve on the loop.

Untrusted input: ``name`` and TXT come from any LAN announcer — they are
length-bounded on ingestion here (HTML-escaping at render is U6).
"""

from __future__ import annotations

import asyncio
import logging
import uuid as _uuid_mod

_log = logging.getLogger(__name__)

# Availability guard, mirroring app/output/direct.py's _GST_AVAILABLE: the
# module imports on any host and degrades cleanly when python-zeroconf is
# absent (subscribe/discover return None, exactly like mdns_dbus when avahi
# is off the bus).
_ZEROCONF_AVAILABLE = False
try:
    from zeroconf import IPVersion, ServiceStateChange
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo
    _ZEROCONF_AVAILABLE = True
except Exception as _exc:  # pragma: no cover - exercised only on hosts w/o zeroconf
    _log.warning("python-zeroconf unavailable — in-process mDNS discovery disabled: %s", _exc)

# Length bound on untrusted mDNS strings (instance name, TXT keys/values). Any
# LAN announcer can set these; bounding on ingest caps registry/payload size
# and pairs with HTML-escaping at render (U6).
_MAX_STR = 256

# ServiceInfo resolve timeout (ms). The browser's add/update fires on the
# announcement; resolving fills addresses/port/TXT. 3s matches the avahi
# one-shot's per-resolve budget.
_RESOLVE_TIMEOUT_MS = 3000.0


def _bound(value) -> str:
    """Coerce to str and length-bound untrusted mDNS input."""
    if not isinstance(value, str):
        value = str(value)
    return value[:_MAX_STR]


def _normalize_type(service_type: str) -> str:
    """python-zeroconf wants the FQDN form with a trailing dot."""
    return service_type if service_type.endswith(".") else service_type + "."


def _instance_label(name: str, service_type: str) -> str:
    """Strip the ``.<service_type>.`` suffix so ``name`` matches the avahi
    instance-label shape ``register_resolved`` / the watcher name-index use.

    ``Foo._raop._tcp.local.`` → ``Foo``.  Falls back to the trailing-dot-
    stripped name when the suffix is absent (defensive).
    """
    suffix = "." + service_type.rstrip(".") + "."
    label = name[: -len(suffix)] if name.endswith(suffix) else name.rstrip(".")
    return _bound(label)


def _decode_txt(info) -> dict[str, str]:
    """Decode a ServiceInfo's TXT records into a ``{key: value}`` str dict.

    Uses ``decoded_properties`` (UTF-8/ASCII decoded by zeroconf); a key with
    no value (``key`` with no ``=``) maps to ``""``, matching mdns_dbus's
    ``_parse_txt_dict``.  Malformed entries are skipped so one bad record
    cannot poison the dict.
    """
    out: dict[str, str] = {}
    try:
        props = info.decoded_properties
    except Exception:
        props = None
    if not props:
        return out
    for key, value in props.items():
        try:
            if not key:
                continue
            out[_bound(key)] = _bound("" if value is None else value)
        except Exception:
            pass
    return out


def _cast_uuid(txt: dict[str, str]) -> str | None:
    """Cast device UUID from the ``id=`` TXT entry, validated; else None.

    Mirrors mdns_dbus._parse_txt_uuid — a 32-hex (dashless) or canonical UUID
    string both parse via ``uuid.UUID``.
    """
    val = txt.get("id")
    if not val:
        return None
    try:
        _uuid_mod.UUID(val)
        return val
    except ValueError:
        _log.debug("mdns_zeroconf: ignoring malformed id= TXT value %r", val)
        return None


def _first_host(info) -> str | None:
    """First IPv4 address (preferred), else first address of any family."""
    try:
        v4 = info.parsed_addresses(IPVersion.V4Only)
        if v4:
            return v4[0]
    except Exception:
        pass
    try:
        addrs = info.parsed_addresses()
    except Exception:
        addrs = []
    return addrs[0] if addrs else None


def subscriptions_supported(aiozc) -> bool:
    """True when python-zeroconf is importable AND a shared AsyncZeroconf bound.

    The watcher polls this alongside the ``'status'`` events to decide whether
    the in-process source is a live discovery surface (U6 degraded detection).
    """
    return bool(_ZEROCONF_AVAILABLE and aiozc is not None)


async def subscribe(service_type: str, on_event, aiozc) -> "_ZeroconfSubscription | None":
    """Open a persistent in-process browse for *service_type*; return a handle.

    *aiozc* is the shared ``AsyncZeroconf`` from ``app/state.py`` (no second
    5353 bind).  *on_event* is invoked on the caller's asyncio loop (captured
    here) as ``on_event(kind, payload)`` — see the module docstring.

    Returns ``None`` when python-zeroconf is unavailable, no shared
    ``AsyncZeroconf`` was provided, or browser setup fails.  Never raises.
    """
    if not subscriptions_supported(aiozc):
        return None
    loop = asyncio.get_running_loop()
    sub = _ZeroconfSubscription(service_type, on_event, aiozc, loop)
    try:
        sub._start()
    except Exception:
        _log.warning("mdns_zeroconf: subscribe failed for %s", service_type, exc_info=True)
        return None
    sub._emit("status", "up")
    return sub


async def unsubscribe(handle: "_ZeroconfSubscription | None") -> None:
    """Cancel *handle*'s browser and stop event delivery.  Never raises."""
    if handle is None:
        return
    try:
        await handle._stop()
    except Exception:
        _log.debug("mdns_zeroconf: unsubscribe failed", exc_info=True)


async def discover(service_type: str, aiozc, *, timeout: float = 3.0):
    """One-shot in-process browse: return current ``(name, host, port, uuid,
    txt)`` tuples for *service_type*, or ``None`` when unavailable.

    Used by the manual Scan re-browse (plan U7) as the in-process replacement
    for ``mdns_dbus.discover`` — same five-field tuple shape (no trailing
    ``service_type``), so existing callers consume it unchanged.  Returns ``[]``
    when zeroconf is reachable but nothing answered within *timeout*.
    """
    if not subscriptions_supported(aiozc):
        return None
    results: dict[str, tuple] = {}
    pending: dict[str, asyncio.Task] = {}
    loop = asyncio.get_running_loop()
    norm_type = _normalize_type(service_type)

    async def _resolve(name: str) -> None:
        try:
            info = AsyncServiceInfo(norm_type, name)
            ok = await info.async_request(aiozc.zeroconf, _RESOLVE_TIMEOUT_MS)
            if not ok:
                return
            host = _first_host(info)
            if not host:
                return
            txt = _decode_txt(info)
            results[name] = (
                _instance_label(name, service_type), host,
                int(info.port or 0), _cast_uuid(txt), txt,
            )
        except Exception:
            _log.debug("mdns_zeroconf.discover: resolve failed for %r", name, exc_info=True)

    def _on_change(zeroconf, service_type, name, state_change) -> None:
        if state_change is ServiceStateChange.Removed:
            return
        if name in pending or name in results:
            return
        pending[name] = asyncio.ensure_future(_resolve(name))

    browser = AsyncServiceBrowser(aiozc.zeroconf, norm_type, handlers=[_on_change])
    try:
        await asyncio.sleep(timeout)
        if pending:
            await asyncio.gather(*pending.values(), return_exceptions=True)
    finally:
        try:
            await browser.async_cancel()
        except Exception:
            _log.debug("mdns_zeroconf.discover: browser cancel failed", exc_info=True)
    return list(results.values())


class _ZeroconfSubscription:
    """Opaque handle for one live AsyncServiceBrowser (one per subscribe())."""

    def __init__(self, service_type: str, on_event, aiozc, loop) -> None:
        self.service_type = service_type            # original, e.g. "_raop._tcp.local"
        self._browse_type = _normalize_type(service_type)
        self.on_event = on_event
        self._aiozc = aiozc
        self._loop = loop                            # asyncio loop captured at subscribe()
        self.active = True
        self._browser = None
        self._resolve_tasks: set[asyncio.Task] = set()

    def _start(self) -> None:
        self._browser = AsyncServiceBrowser(
            self._aiozc.zeroconf, self._browse_type, handlers=[self._on_state_change]
        )

    # — browser callback (may run off the asyncio loop) ————————————————————————

    def _on_state_change(self, zeroconf, service_type, name, state_change) -> None:
        """ServiceBrowser handler. May fire on zeroconf's engine thread, so
        cross to the asyncio loop via ``call_soon_threadsafe`` before doing any
        asyncio work (resolve scheduling, emit)."""
        if not self.active:
            return
        try:
            self._loop.call_soon_threadsafe(self._dispatch, name, state_change)
        except RuntimeError:
            pass  # asyncio loop already closed — nowhere to deliver

    def _dispatch(self, name, state_change) -> None:
        """Runs on the asyncio loop (scheduled from _on_state_change)."""
        if not self.active:
            return
        if state_change is ServiceStateChange.Removed:
            self._emit("remove", (_instance_label(name, self.service_type), self.service_type))
            return
        # Added / Updated → resolve ServiceInfo, then emit 'new' (upsert).
        task = asyncio.ensure_future(self._resolve_and_emit(name))
        self._resolve_tasks.add(task)
        task.add_done_callback(self._resolve_tasks.discard)

    async def _resolve_and_emit(self, name) -> None:
        try:
            info = AsyncServiceInfo(self._browse_type, name)
            ok = await info.async_request(self._aiozc.zeroconf, _RESOLVE_TIMEOUT_MS)
            if not ok or not self.active:
                return
            host = _first_host(info)
            if not host:
                return  # unresolvable address — nothing the backend can use
            txt = _decode_txt(info)
            self._emit("new", (
                _instance_label(name, self.service_type), host,
                int(info.port or 0), _cast_uuid(txt), txt, self.service_type,
            ))
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning("mdns_zeroconf: resolve failed for %r", name, exc_info=True)

    def _emit(self, kind: str, payload) -> None:
        """Deliver one event to the subscriber. Called on the asyncio loop;
        the ``active`` guard silences in-flight resolves that complete after
        unsubscribe()."""
        if not self.active:
            return
        try:
            self.on_event(kind, payload)
        except Exception:
            _log.warning("mdns_zeroconf: on_event(%s) raised", kind, exc_info=True)

    async def _stop(self) -> None:
        self.active = False  # silences in-flight resolves too
        for task in list(self._resolve_tasks):
            task.cancel()
        self._resolve_tasks.clear()
        browser = self._browser
        self._browser = None
        if browser is not None:
            await browser.async_cancel()
