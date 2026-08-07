"""Rolling-window lyric prefetcher (plan 2026-06-18-001 U2).

Warms the shared lyric cache for the next few upcoming queue tracks so that when
one starts playing the Lyrics button is instant instead of waiting ~6s for
LRCLIB. Driven from `app/state.py` on every queue mutation and on advance (U3),
so an admin "Play next" bump that lands a track in the window warms it
immediately.

Gentle on LRCLIB and best-effort: it delegates to `cache.get_or_fetch`, which
skips already-cached tracks and collapses concurrent lookups (the in-flight
guard); per-track failures are swallowed so one bad lookup never stops the rest;
and it never blocks the caller (`schedule_prefetch` is fire-and-forget). The
on-demand `/api/lyrics` fetch remains the fallback for anything not yet warmed.
"""
from __future__ import annotations

import asyncio
import logging

from app.lyrics import cache as lyrics_cache

_log = logging.getLogger(__name__)

PREFETCH_WINDOW = 3  # how many upcoming tracks to keep warm; tunable


def _log_task_exc(task: asyncio.Task) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:  # best-effort — a prefetch failure must never surface
        _log.debug("lyric prefetch task failed", exc_info=True)


async def warm_upcoming(tracks, n: int = PREFETCH_WINDOW) -> None:
    """Warm lyrics for the first ``n`` of ``tracks`` (a list of Track objects).

    Sequential by design — one cold LRCLIB lookup at a time is already gentle.
    Per-track errors are swallowed; cache hits / in-flight fetches are skipped
    inside ``get_or_fetch``."""
    for track in (tracks or [])[:n]:
        try:
            dur_s = (track.duration_ms / 1000) if getattr(track, "duration_ms", None) else None
            await lyrics_cache.get_or_fetch(
                track.id, track.artist, track.title, track.album, dur_s
            )
        except Exception:  # defensive — get_or_fetch shouldn't raise, but never let one track stop the window
            _log.debug("lyric prefetch skipped a track", exc_info=True)


def schedule_prefetch(tracks, n: int = PREFETCH_WINDOW) -> None:
    """Fire-and-forget a window prefetch. Returns immediately; never raises.

    Used on the event/broadcast path, so it must not block or throw — a slow or
    failing LRCLIB can never delay a WS broadcast, playback, or queue ops."""
    try:
        task = asyncio.ensure_future(warm_upcoming(tracks, n))
        task.add_done_callback(_log_task_exc)
    except RuntimeError:
        # No running event loop (e.g., called outside async context) — skip.
        _log.debug("schedule_prefetch with no running loop; skipping", exc_info=True)
