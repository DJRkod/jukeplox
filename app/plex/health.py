"""Plex source health: ONE reconciliation routine for connect, 401, startup and
the daily sweep (2026-11-21 fix-plex-source-health plan, U2; KTD1/3/7/8/9/11).

A refresh turns a plex.tv listing into row updates (through the single
``plex_servers`` writer, ``database.reconcile_plex_servers``), per-server
status, seen-library bookkeeping, and a deferred-rebuild flag. Four triggers
feed it — ``request_refresh(reason)`` for 401 / startup / daily, and the
connect flow's ``reconcile(listing, origin="connect")`` — and all of them are
single-flighted through one lock so two writers never diff the same table.

Module state (not persisted; the admin panel reads ``status_snapshot()``):

- ``account_status``: ``ok`` | ``account_rejected`` | ``missing`` | ``legacy``
- a 15-minute limiter on 401-triggered refreshes, a 15-minute cooldown after
  any failed run, and a 1h→24h doubling backoff while plex.tv rejects the
  account token (reset on the next success or on connect).

Logging rule: never a token, never an exception's text, never a URL — class
name and HTTP status code only.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

import httpx

from app import database, state
from app.plex import auth as plex_auth
from app.plex import client as plex_client

_log = logging.getLogger(__name__)

REFRESH_401_INTERVAL_S = 15 * 60
FAILURE_COOLDOWN_S = 15 * 60
ACCOUNT_BACKOFF_MIN_S = 60 * 60
ACCOUNT_BACKOFF_MAX_S = 24 * 60 * 60
PENDING_KEY = "plex_rebuild_pending"
# Deferred rebuild (KTD5): the runner re-checks every minute while the flag is
# set, fires after ten idle minutes, and is capped by the first daily tick at
# which the flag is at least six hours old.
REBUILD_IDLE_S = 10 * 60
REBUILD_CHECK_INTERVAL_S = 60.0
REBUILD_CAP_S = 6 * 60 * 60
# A refresh discarded by the generation guard is re-requested once after this.
DISCARD_RETRY_S = 60.0

# Daily sweep (KTD7): settings key, default hour, and the bound on how long
# shutdown waits for the tasks this module owns to acknowledge their cancel.
REFRESH_HOUR_KEY = "plex_refresh_hour"
DEFAULT_REFRESH_HOUR = 4
SHUTDOWN_GRACE_S = 5.0
# A daily iteration that failed before it could compute a sleep must not spin.
_FAILED_ITERATION_RETRY_S = 60.0

# Injectable clocks (tests advance them). ``_now_local`` is the host's local
# wall clock — the sweep hour is an admin setting read in local time.
_monotonic = time.monotonic
_now_local = datetime.now
_sleep = asyncio.sleep
# Aware UTC wall clock — compared against the pending flag's stored ISO time.
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)

_lock: asyncio.Lock | None = None
_running: asyncio.Task | None = None
_last_401_refresh_at: float | None = None
_cooldown_until: float = 0.0
_account_backoff_s: float = ACCOUNT_BACKOFF_MIN_S
account_status: str = "ok"
_last_run_at: str | None = None
_last_outcome: str | None = None
# Per-server 401 short-circuit: machine id -> monotonic time the server was
# last marked token_rejected. Within FAILURE_COOLDOWN_S a repeat 401 costs
# nothing (no write, no scheduling, no log line).
_server_401_at: dict[str, float] = {}
# Fire-and-forget DB writes scheduled by on_server_401 (tests drain this).
_bg_tasks: set[asyncio.Task] = set()
# Daily loop + every long-lived task this module is responsible for stopping.
_daily_task: asyncio.Task | None = None
_owned_tasks: set[asyncio.Task] = set()
_next_refresh_at: datetime | None = None
# Callbacks run (sync, each isolated) on every daily tick before the refresh;
# U4's deferred-rebuild runner registers its six-hour hard-cap check here.
on_daily_tick: list[Callable[[], None]] = []
# U4 deferred-rebuild runner: the task (alive only while the flag is set) and
# the "a daily tick arrived since my last check" latch its tick hook sets.
_rebuild_task: asyncio.Task | None = None
_tick_pending: bool = False
# A generation-discarded refresh has scheduled its one retry (cleared by the
# next run that is not discarded).
_retry_pending: bool = False


@dataclass
class ReconcileResult:
    """Machine ids and section keys only — never a token value."""
    added: list[str] = field(default_factory=list)
    returned: list[str] = field(default_factory=list)
    vanished: list[str] = field(default_factory=list)
    new_libraries: list[str] = field(default_factory=list)
    changed_tokens: list[str] = field(default_factory=list)


def reset_state() -> None:
    """Forget every in-memory limiter / status (tests; a fresh lock per event loop)."""
    global _lock, _running, _last_401_refresh_at, _cooldown_until
    global _account_backoff_s, account_status, _last_run_at, _last_outcome
    global _daily_task, _next_refresh_at, _rebuild_task, _tick_pending, _retry_pending
    _lock = None
    _rebuild_task = None
    _tick_pending = False
    _retry_pending = False
    _running = None
    _last_401_refresh_at = None
    _cooldown_until = 0.0
    _account_backoff_s = ACCOUNT_BACKOFF_MIN_S
    account_status = "ok"
    _last_run_at = None
    _last_outcome = None
    _server_401_at.clear()
    _bg_tasks.clear()
    _daily_task = None
    _owned_tasks.clear()
    _next_refresh_at = None
    on_daily_tick.clear()


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def status_snapshot() -> dict:
    """Admin-facing view of the module state (U6). No token, no URL."""
    now = _monotonic()
    return {
        "account_status": account_status,
        "cooldown_remaining_s": max(0, int(_cooldown_until - now)),
        "account_backoff_s": int(_account_backoff_s),
        "last_run_at": _last_run_at,
        "last_outcome": _last_outcome,
        "running": _running is not None and not _running.done(),
        "next_refresh_at": _next_refresh_at.isoformat(timespec="minutes")
        if _next_refresh_at else None,
        "token_rejected_servers": sorted(
            mid for mid, at in _server_401_at.items() if now - at < FAILURE_COOLDOWN_S),
    }


def next_refresh_at() -> datetime | None:
    """Local wall-clock time of the next daily sweep (for the admin Sources panel,
    U6), or ``None`` while the loop is not running / has not computed one."""
    return _next_refresh_at


# ── triggers ─────────────────────────────────────────────────────────────────

def request_refresh(reason: str) -> bool:
    """Schedule one refresh run; returns whether a run was scheduled.

    Dropped when: a 401-triggered request arrives within fifteen minutes of
    the last one (KTD7 — a storm of art 401s costs one refresh), a cooldown
    is in force (failed run, or account backoff), or a run is already in
    flight. The run is a task with ``state._log_task_exc`` as its safety net;
    ``run_refresh`` itself catches and classifies every expected failure.
    """
    global _last_401_refresh_at, _running
    now = _monotonic()
    if reason == "401" and _last_401_refresh_at is not None \
            and now - _last_401_refresh_at < REFRESH_401_INTERVAL_S:
        return False
    if now < _cooldown_until:
        return False
    if _running is not None and not _running.done():
        return False
    if reason == "401":
        _last_401_refresh_at = now
    task = asyncio.create_task(run_refresh(reason))
    task.add_done_callback(state._log_task_exc)
    _running = task
    return True


# ── 401 hook (KTD4) ──────────────────────────────────────────────────────────

def on_server_401(machine_id: str) -> None:
    """A Plex server answered 401. Sync and fire-and-forget: the client's
    ``_check_status`` is sync, and the stream proxy calls this from inside a
    streaming body. Idempotent under a storm — an album grid of art 401s costs
    one row write, one WARNING and one ``request_refresh`` per server per
    cooldown; every repeat inside the window returns before touching anything.
    Outside a running event loop (nothing to schedule on) it is a no-op."""
    if not machine_id:
        return
    now = _monotonic()
    last = _server_401_at.get(machine_id)
    if last is not None and now - last < FAILURE_COOLDOWN_S:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _server_401_at[machine_id] = now
    task = loop.create_task(_mark_token_rejected(machine_id))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    task.add_done_callback(state._log_task_exc)
    request_refresh("401")


async def _mark_token_rejected(machine_id: str) -> None:
    await database.set_plex_server_status(machine_id, "token_rejected")
    name = (await database.get_plex_server_names()).get(machine_id)
    _log.warning("plex server %s (%s) rejected its token (HTTP 401); marked "
                 "token_rejected, refresh requested",
                 name or "unnamed", machine_id)


def install_hooks() -> None:
    """Register ``on_server_401`` as the Plex client's auth-failed hook. Called
    from lifespan after ``state.setup()``; the client package stays a leaf."""
    plex_client.set_auth_failed_hook(on_server_401)
    if _rebuild_tick_hook not in on_daily_tick:
        on_daily_tick.append(_rebuild_tick_hook)


# ── daily sweep (KTD7) ───────────────────────────────────────────────────────

def _coerce_refresh_hour(raw) -> int:
    """The stored hour as an int in 0..23; WARNING and the default otherwise.
    ``None`` (never set) is the default silently."""
    if raw is None:
        return DEFAULT_REFRESH_HOUR
    try:
        hour = int(str(raw).strip())
    except (TypeError, ValueError):
        hour = -1
    if 0 <= hour <= 23:
        return hour
    _log.warning("setting %s=%r is not an hour 0..23; using %d",
                 REFRESH_HOUR_KEY, raw, DEFAULT_REFRESH_HOUR)
    return DEFAULT_REFRESH_HOUR


def _next_occurrence(hour: int, now: datetime) -> datetime:
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def _run_tick_hooks() -> None:
    for cb in list(on_daily_tick):
        try:
            cb()
        except Exception as e:  # noqa: BLE001 — one hook's bug never costs the sweep
            _log.error("daily tick hook %s raised: %s", getattr(cb, "__name__", cb),
                       type(e).__name__, exc_info=e)


async def _daily_loop() -> None:
    """Sleep until the configured local hour, fire the sweep, repeat. The hour is
    re-read on every wake so an admin change takes effect without a restart. A
    failed iteration is logged (``_log_task_exc`` style) and the loop re-arms;
    cancellation is re-raised so shutdown sees a cancelled task."""
    global _next_refresh_at
    while True:
        try:
            raw = await database.get_setting(REFRESH_HOUR_KEY)
            hour = _coerce_refresh_hour(raw)
            now = _now_local()
            target = _next_occurrence(hour, now)
            _next_refresh_at = target
            _log.info("plex daily sweep scheduled for %s (local)",
                      target.isoformat(sep=" ", timespec="minutes"))
            await _sleep((target - now).total_seconds())
            _run_tick_hooks()
            request_refresh("daily")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — re-arm; the loop must outlive its mistakes
            _log.error("plex daily sweep iteration raised: %s", type(e).__name__, exc_info=e)
            await _sleep(_FAILED_ITERATION_RETRY_S)


def start_daily_loop() -> asyncio.Task:
    """Start the daily sweep task (idempotent while one is alive) and own it."""
    global _daily_task
    if _daily_task is not None and not _daily_task.done():
        return _daily_task
    _daily_task = asyncio.create_task(_daily_loop())
    _daily_task.add_done_callback(state._log_task_exc)
    return _daily_task


def reschedule_daily_loop() -> None:
    """Re-arm a SLEEPING daily loop so a changed ``plex_refresh_hour`` takes
    effect at the next occurrence of the new hour (U6). The loop re-reads the
    hour on every wake, but it only wakes at the OLD target — saving 02:00 at
    noon while the loop sleeps toward 04:00 would otherwise land the first sweep
    at tomorrow's 02:00. A no-op when no loop is running (tests, pre-startup)."""
    global _daily_task
    if _daily_task is None or _daily_task.done():
        return
    _daily_task.cancel()
    _daily_task = None
    start_daily_loop()


async def configured_refresh_hour() -> int:
    """The sweep hour the admin has configured, coerced exactly as the loop
    coerces it (0..23, default 4) — the GET /admin/settings echo (U6)."""
    return _coerce_refresh_hour(await database.get_setting(REFRESH_HOUR_KEY))


async def projected_next_refresh_at() -> datetime:
    """Local wall-clock time of the next sweep as the CURRENT setting implies.
    Unlike ``next_refresh_at`` (the sleeping loop's target) this follows a saved
    hour immediately and is defined while the loop is not running, so the admin
    panel's "Next sweep" line is always populated and always current (U6)."""
    return _next_occurrence(await configured_refresh_hour(), _now_local())


def own_task(task: asyncio.Task) -> asyncio.Task:
    """Register a long-lived task for ``shutdown()`` to cancel (U4's rebuild
    runner registers itself here). Returns the task for chaining."""
    _owned_tasks.add(task)
    task.add_done_callback(_owned_tasks.discard)
    return task


async def shutdown(grace: float = SHUTDOWN_GRACE_S) -> None:
    """Cancel everything this module owns — the daily loop, any registered
    runner, an in-flight refresh and pending 401 writes — and wait at most
    ``grace`` seconds for them to finish. Never raises."""
    global _daily_task, _rebuild_task
    tasks: set[asyncio.Task] = set(_owned_tasks) | set(_bg_tasks)
    if _daily_task is not None:
        tasks.add(_daily_task)
    if _running is not None:
        tasks.add(_running)
    pending = [t for t in tasks if not t.done()]
    for t in pending:
        t.cancel()
    if pending:
        _, still = await asyncio.wait(pending, timeout=grace)
        if still:
            _log.warning("plex health: %d task(s) did not stop within %ss", len(still), grace)
    _daily_task = None
    _owned_tasks.clear()
    _rebuild_task = None


# ── deferred rebuild (U4, KTD5) ──────────────────────────────────────────────

def _rebuild_tick_hook() -> None:
    """Registered on ``on_daily_tick``: latch "a tick arrived" for the runner's
    next check. No DB read here — the hook is sync and the runner owns the
    read-then-clear so the flag is cleared exactly once."""
    global _tick_pending
    if _rebuild_task is not None and not _rebuild_task.done():
        _tick_pending = True


async def pending_rebuild_since() -> str | None:
    """The ISO UTC time the deferred rebuild was requested, or None when no
    rebuild is pending. Token-free; the admin Sources payload reads this."""
    return await database.get_setting(PENDING_KEY)


def _flag_age_s(raw: str) -> float:
    """Seconds since the flag was written. An unparseable value reads as
    infinitely old so a corrupt flag is consumed at the next tick, not kept."""
    try:
        set_at = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        _log.warning("setting %s=%r is not an ISO time; treating as stale", PENDING_KEY, raw)
        return float("inf")
    if set_at.tzinfo is None:
        set_at = set_at.replace(tzinfo=timezone.utc)
    return (_now_utc() - set_at).total_seconds()


async def rebuild_if_idle() -> bool:
    """One check. Returns True when the runner is finished (no flag, or the
    rebuild fired), False when it should check again.

    Fires when playback has been idle ``REBUILD_IDLE_S``, or when a daily tick
    has arrived since the last check and the flag is ``REBUILD_CAP_S`` old. The
    flag is deleted BEFORE the triggers so a trigger that raises cannot make
    the runner fire twice; both refreshes are single-flighted on their side."""
    global _tick_pending
    raw = await database.get_setting(PENDING_KEY)
    if raw is None:
        _tick_pending = False
        return True
    idle_for = state.playback_idle_for()
    reason = None
    if idle_for is not None and idle_for >= REBUILD_IDLE_S:
        reason = "idle"
    elif _tick_pending:
        _tick_pending = False
        if _flag_age_s(raw) >= REBUILD_CAP_S:
            reason = "daily-cap"
    if reason is None:
        return False
    if state.catalog_building():
        # trigger_catalog_refresh is single-flighted and would return without
        # doing anything; consuming the flag now would lose the rebuild. Keep
        # it and look again next minute (2026-11-22 review #1).
        return False
    await database.delete_setting(PENDING_KEY)
    _log.info("plex rebuild pending since %s: running now (%s)", raw, reason)
    state.trigger_catalog_refresh()
    state.trigger_browse_index_refresh()
    return True


async def _rebuild_runner() -> None:
    """Check, sleep a minute, repeat, until ``rebuild_if_idle`` says done.
    Cancellation (shutdown) is re-raised and never touches the flag — the next
    boot's ``ensure_rebuild_runner()`` picks it up."""
    while True:
        try:
            if await rebuild_if_idle():
                return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — a failed check is retried next minute
            _log.error("plex rebuild check raised: %s", type(e).__name__, exc_info=e)
        await _sleep(REBUILD_CHECK_INTERVAL_S)


def ensure_rebuild_runner() -> asyncio.Task:
    """Start the deferred-rebuild runner if none is alive, and own it so
    ``shutdown()`` cancels it. Idempotent: two flag writes start one runner.
    Called after the flag is written and at startup (the runner exits on its
    first check when no flag is set, so the startup call is unconditional)."""
    global _rebuild_task
    if _rebuild_task is not None and not _rebuild_task.done():
        return _rebuild_task
    if _rebuild_tick_hook not in on_daily_tick:
        on_daily_tick.append(_rebuild_tick_hook)
    task = asyncio.create_task(_rebuild_runner())
    task.add_done_callback(state._log_task_exc)
    _rebuild_task = own_task(task)
    return task


def _enabled_gen() -> int:
    """The enabled-libraries generation (read under the health lock before a
    refresh's first probe so a source reconfiguration during probing is
    detected and the result dropped)."""
    from app.api import guest as _guest_api
    return _guest_api._enabled_libs_gen


def _fail(outcome: str, delay_s: float) -> None:
    """Record a failed run: cooldown for ``delay_s`` and the outcome label."""
    global _cooldown_until, _last_outcome
    _cooldown_until = _monotonic() + delay_s
    _last_outcome = outcome


async def run_refresh(reason: str) -> ReconcileResult | None:
    """One full refresh under the shared lock: account token (with the KTD3
    bootstrap), plex.tv listing, per-server probing, reconcile. Returns the
    reconcile result, or ``None`` when nothing was written (legacy install,
    no account token, plex.tv failure, generation moved)."""
    global _last_run_at, _retry_pending
    async with _get_lock():
        # The generation is read INSIDE the lock: a connect reconcile holding
        # it bumps the counter, and a value captured while queued behind it
        # would discard this run for a change it is about to observe
        # (2026-11-22 review #5).
        gen = _enabled_gen()
        _last_run_at = database.utc_now_iso()
        try:
            result = await _refresh_locked(reason, gen)
        except Exception as e:  # noqa: BLE001 — class only; the task callback is the safety net
            _fail("error", FAILURE_COOLDOWN_S)
            _log.warning("plex refresh (%s) failed: %s — cooldown %ss",
                         reason, type(e).__name__, FAILURE_COOLDOWN_S)
            return None
    if _last_outcome == "discarded":
        # A discard sets no cooldown and nothing else would retry it before
        # the next daily tick. Re-request ONCE after a short delay; a second
        # discard in a row waits for the next trigger.
        if not _retry_pending:
            _retry_pending = True
            own_task(asyncio.create_task(_retry_after_discard(reason)))
    else:
        _retry_pending = False
    return result


async def _retry_after_discard(reason: str) -> None:
    await _sleep(DISCARD_RETRY_S)
    request_refresh(reason)


async def _refresh_locked(reason: str, gen: int) -> ReconcileResult | None:
    global account_status, _account_backoff_s, _last_outcome
    rows = await database.get_plex_servers(include_vanished=True)
    if not rows:
        if await database.get_plex_config():
            # KTD11: legacy single-server install — no machine id to upsert
            # against; the admin list carries the "reconnect to enable
            # self-heal" line. No network.
            account_status = "legacy"
            _last_outcome = "legacy"
            return None
        _last_outcome = "nothing_configured"
        return None

    client_id = next((r.get("client_id") for r in rows if r.get("client_id")), "") or ""
    token = await database.get_plex_account_token()
    if not token:
        # KTD3 bootstrap: an upgraded install has no account token yet; the
        # owned server's saved token usually IS the account token.
        owned = [r for r in rows if r.get("owned") and r.get("token")]
        if owned:
            verdict, _ = await plex_auth.verify_account_token(owned[0]["token"], client_id)
            if verdict == "ok":
                token = owned[0]["token"]
                await database.set_plex_account_token(token)
                _log.info("plex refresh: adopted the owned server's token as the account token")
            elif verdict != "rejected":
                # No verdict (connection error, 5xx, 429): plex.tv did not say
                # the token is bad, so nothing about the rows or the account is
                # known to have changed. Cooldown and retry later; only a hard
                # rejection falls through to "missing" (2026-11-22 review #6).
                _fail("plex_tv_unreachable", FAILURE_COOLDOWN_S)
                _log.warning("plex refresh (%s): account token check got no verdict — "
                             "no change, cooldown %ss", reason, FAILURE_COOLDOWN_S)
                return None
        if not token:
            account_status = "missing"
            _last_outcome = "no_account_token"
            for r in rows:
                if r.get("status") != "no_longer_shared":
                    await database.set_plex_server_status(r["machine_id"], "unknown")
            _log.warning("plex refresh (%s): no account token — reconnect Plex to enable self-heal",
                         reason)
            return None

    known_urls = {r["machine_id"]: r["server_url"] for r in rows if r.get("server_url")}
    try:
        listing = await plex_auth.discover_servers(token, client_id, known_urls=known_urls)
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        if code in (401, 403):
            account_status = "account_rejected"
            _fail("account_rejected", _account_backoff_s)
            _log.warning("plex refresh (%s): plex.tv rejected the account token (HTTP %s) — "
                         "backoff %ss", reason, code, int(_account_backoff_s))
            _account_backoff_s = min(_account_backoff_s * 2, ACCOUNT_BACKOFF_MAX_S)
        else:
            _fail("plex_tv_error", FAILURE_COOLDOWN_S)
            _log.warning("plex refresh (%s): plex.tv answered HTTP %s — no change, cooldown %ss",
                         reason, code, FAILURE_COOLDOWN_S)
        return None
    except (httpx.HTTPError, OSError) as e:
        _fail("plex_tv_unreachable", FAILURE_COOLDOWN_S)
        _log.warning("plex refresh (%s): plex.tv unreachable (%s) — no change, cooldown %ss",
                     reason, type(e).__name__, FAILURE_COOLDOWN_S)
        return None

    result = await _reconcile_locked(listing, origin=reason, gen=gen, before_rows=rows)
    account_status = "ok"
    _account_backoff_s = ACCOUNT_BACKOFF_MIN_S
    _last_outcome = "ok" if result is not None else "discarded"
    if result is not None:
        for mid in result.changed_tokens:
            _server_401_at.pop(mid, None)
    return result


# ── reconcile ────────────────────────────────────────────────────────────────

async def reconcile(listing: list[dict], *, origin: str) -> ReconcileResult | None:
    """Apply a plex.tv listing (``discover_servers`` output) under the shared
    lock. ``origin`` is ``"connect"`` for the PIN flow, else the refresh
    reason. Returns ``None`` only when a refresh-origin result was discarded
    because the enabled-libraries generation moved during probing."""
    global account_status, _cooldown_until, _account_backoff_s
    async with _get_lock():
        gen = _enabled_gen()  # inside the lock — see run_refresh
        if origin == "connect":
            # A fresh account token was just captured: the account is no
            # longer rejected and nothing is cooling down.
            account_status = "ok"
            _account_backoff_s = ACCOUNT_BACKOFF_MIN_S
            _cooldown_until = 0.0
        return await _reconcile_locked(listing, origin=origin, gen=gen)


def _music_sections(sections: list[dict]) -> list[tuple[str, str]]:
    return [(s["key"], s["title"]) for s in sections if s.get("type") == "artist"]


async def _reconcile_locked(listing: list[dict], *, origin: str, gen: int,
                            before_rows: list[dict] | None = None) -> ReconcileResult | None:
    """``before_rows`` lets a refresh pass the ``plex_servers`` rows it already
    read instead of re-fetching them; the connect path leaves it ``None``."""
    from app.catalog import store as catalog_store

    connect = origin == "connect"
    if connect:
        await database.clear_plex_tombstones()  # KTD10: connect asks for the whole account again

    result = ReconcileResult()
    if not listing:
        # A 200 listing of zero servers writes nothing (connect-flow guard kept
        # for every origin: an account with no servers is far rarer than a
        # plex.tv hiccup that answers an empty list).
        return result

    if before_rows is None:
        before_rows = await database.get_plex_servers(include_vanished=True)
    before = {r["machine_id"]: r for r in before_rows}
    tombstoned = set(await database.get_plex_tombstones())
    listed = [e for e in listing if e.get("machine_id") and e["machine_id"] not in tombstoned]

    # Authenticated section listing, concurrently, only where an address answered.
    probe_targets = [e for e in listed if e.get("probe") == "reachable" and e.get("server_url")]
    outcomes = await asyncio.gather(
        *(plex_auth.list_sections(e["server_url"], e.get("token") or "") for e in probe_targets))
    sections_by_id = {e["machine_id"]: o for e, o in zip(probe_targets, outcomes)}

    if not connect and _enabled_gen() != gen:
        # A source reconfiguration landed while we were probing — this result
        # describes the OLD source set. Drop it (docs/solutions/design-patterns/
        # async-refresh-must-generation-guard-against-invalidation.md).
        _log.info("plex refresh (%s): generation moved during probing — result discarded", origin)
        return None

    rows: list[dict] = []
    for e in listed:
        mid = e["machine_id"]
        outcome = sections_by_id.get(mid)
        status = outcome["status"] if outcome else "unreachable"
        if not e.get("server_url") and mid not in before:
            # Nothing to reach it by and nothing saved: not a row yet. It is
            # still "listed" — but with no row it cannot be marked missing.
            continue
        rows.append({
            "machine_id": mid, "server_url": e.get("server_url") or "",
            "name": e.get("name") or "", "owner": e.get("owner") or "",
            "owned": e.get("owned"), "token": e.get("token") or "",
            "client_id": e.get("client_id") or "", "status": status,
        })

    db_result = await database.reconcile_plex_servers(rows, mark_missing=True)
    result.added = list(db_result["added"])
    result.returned = list(db_result["returned"])
    result.vanished = list(db_result["vanished"])
    result.changed_tokens = list(db_result["changed_tokens"])

    # The rows are committed. Everything below is a follow-up step that must
    # not leave the in-memory world describing the OLD table if a sibling step
    # raises (2026-11-22 review #7): the registry is invalidated FIRST, each
    # later step is isolated, and the deferred-rebuild flag is written in a
    # ``finally`` whenever a rebuild is owed.
    membership_changed = bool(result.added or result.returned or result.vanished)
    if membership_changed:
        state.invalidate_plex_client()

    # KTD8: token / URL changes reach the live client in place; no invalidate.
    try:
        changed_urls = [r["machine_id"] for r in rows
                        if r["machine_id"] in before and r["server_url"]
                        and r["server_url"] != (before[r["machine_id"]].get("server_url") or "")]
        _apply_live_client_updates(rows, set(result.changed_tokens) | set(changed_urls))
    except Exception as e:  # noqa: BLE001
        _log.warning("plex reconcile (%s): live client update failed (%s)",
                     origin, type(e).__name__)

    # KTD6: a vanished server's content goes at once — no crawl needed. A purge
    # that raises is retried on the next sweep: every no_longer_shared server
    # that still holds catalog rows is purged again (2026-11-22 review #8).
    purge_failed = False
    try:
        statuses = await database.get_plex_server_statuses()
        held = await catalog_store.source_ids_with_holds()
        stale = [mid for mid, st in statuses.items()
                 if st == "no_longer_shared" and mid in held and mid not in result.vanished]
    except Exception as e:  # noqa: BLE001
        stale = []
        _log.warning("plex reconcile (%s): stale-purge scan failed (%s)", origin, type(e).__name__)
    for mid in [*result.vanished, *sorted(stale)]:
        try:
            await catalog_store.delete_catalog_source(mid)
        except Exception as e:  # noqa: BLE001
            purge_failed = True
            _log.warning("plex reconcile (%s): catalog purge of %s failed (%s) — retried next sweep",
                         origin, mid, type(e).__name__)

    # KTD9: seen set, baseline, new-library enabling (owned only).
    seeding = {"enabled_any": False, "failed": False}
    added = set(result.added)
    try:
        await _seed_libraries(listed, sections_by_id, added, result, seeding)
    except Exception as e:  # noqa: BLE001 — libraries may be half-enabled: owe a rebuild
        seeding["failed"] = True
        _log.warning("plex reconcile (%s): library seeding failed (%s)", origin, type(e).__name__)
    enabled_any = seeding["enabled_any"]

    # A newly added server honours its default-ON switch: clear its veto only.
    if added:
        try:
            disabled = await database.get_disabled_sources()
            remaining = [d for d in disabled if d not in added]
            if len(remaining) != len(disabled):
                await database.set_disabled_sources(remaining)
        except Exception as e:  # noqa: BLE001
            _log.warning("plex reconcile: veto clear failed (%s)", type(e).__name__)

    if enabled_any and not membership_changed:
        state.invalidate_plex_client()
    if result.vanished or stale:
        try:
            state.trigger_browse_index_refresh()
        except Exception as e:  # noqa: BLE001
            _log.warning("plex reconcile (%s): browse refresh trigger failed (%s)",
                         origin, type(e).__name__)

    # A vanish during an in-flight crawl: that crawl captured the registry
    # before the vanish and will write the dead source's rows back after the
    # prefix delete above, so a rebuild without it is owed (2026-11-22 review
    # #2; the crawl's own generation guard in state._refresh_catalog is the
    # other half). A vanish with no crawl running needs none (KTD6).
    vanish_during_crawl = bool(result.vanished) and state.catalog_building()
    needs_rebuild = (bool(result.returned) or enabled_any or seeding["failed"]
                     or purge_failed or vanish_during_crawl)
    if needs_rebuild:
        if connect and not before_rows:
            # Brand-new install: crawl now, as the connect flow always has.
            state.trigger_catalog_refresh()
        else:
            await _set_pending_flag(origin)

    _log.info("plex reconcile (%s): added=%d returned=%d vanished=%d new_libraries=%d "
              "changed_tokens=%d", origin, len(result.added), len(result.returned),
              len(result.vanished), len(result.new_libraries), len(result.changed_tokens))
    return result


async def _seed_libraries(listed: list[dict], sections_by_id: dict, added: set[str],
                          result: ReconcileResult, seeding: dict) -> None:
    """KTD9 per server: record the seen set; enable a NEW owned server's music
    libraries (R13) and a known owned server's NEW libraries; shared servers
    are recorded only (R15). ``seeding["enabled_any"]`` is updated as it goes
    so a raise midway still reports what was enabled (review #7)."""
    for e in listed:
        mid = e["machine_id"]
        outcome = sections_by_id.get(mid)
        if not outcome or outcome["status"] != "usable":
            continue
        music = _music_sections(outcome["sections"])
        keys = [k for k, _ in music]
        seen = set(await database.get_plex_seen_libraries(mid))
        owned = bool(e.get("owned"))
        if seen:
            fresh = [(k, t) for k, t in music if k not in seen]
            result.new_libraries.extend(f"{mid}:{k}" for k, _ in fresh)
            if owned and fresh:
                for k, title in fresh:
                    await database.toggle_library(f"{mid}:{k}", title, enabled=True)
                seeding["enabled_any"] = True
        elif mid not in added and await database.plex_seen_baseline_pending(mid):
            # A server saved before this build (the U1 migration marked it):
            # record its libraries, enable nothing — an unticked owned library
            # has no row and must stay off.
            pass
        elif owned:
            # First usable sight of a NEW server — whether plex.tv listed it
            # for the first time this sweep (``added``) or it was unreachable
            # at first sight and has no seen rows and no baseline marker
            # (2026-11-22 review #9): owned libraries on (R13), shared off (R15).
            for k, title in music:
                await database.toggle_library(f"{mid}:{k}", title, enabled=True)
            if music:
                seeding["enabled_any"] = True
        # Only the unseen keys: a no-change sweep inserts nothing.
        await database.add_plex_seen_libraries(mid, [k for k in keys if k not in seen])


async def _set_pending_flag(origin: str) -> None:
    """KTD5: the rebuild waits for idle playback; U4's runner consumes this
    flag. The value is when it was set; never rewritten. Never raises — a
    failed write is logged (class only) and the next sweep owes it again."""
    try:
        if await database.get_setting(PENDING_KEY) is None:
            await database.set_setting(PENDING_KEY, database.utc_now_iso())
        ensure_rebuild_runner()
    except Exception as e:  # noqa: BLE001
        _log.warning("plex reconcile (%s): could not record the pending rebuild (%s)",
                     origin, type(e).__name__)


def _apply_live_client_updates(rows: list[dict], machine_ids: set[str]) -> None:
    """Push a changed token / URL into the running ``PlexClient`` (KTD8). Headers
    and stream URLs are built per request from ``token`` / ``server_url``, so no
    httpx client is rebuilt. Touches only an existing registry — never builds one."""
    if not machine_ids:
        return
    registry = getattr(state, "_plex_client", None)
    if registry is None:
        return
    by_id = {r["machine_id"]: r for r in rows}
    for src in getattr(registry, "sources", []) or []:
        if getattr(src, "source_type", "") != "plex":
            continue
        mid = getattr(src, "source_id", None)
        if mid not in machine_ids:
            continue
        client = getattr(src, "_client", None)
        row = by_id.get(mid)
        if client is None or row is None:
            continue
        if row.get("token"):
            client.token = row["token"]
        if row.get("server_url"):
            client.server_url = row["server_url"].rstrip("/")
