"""Tests for the rolling-window lyric prefetcher (plan 2026-06-18-001 U2)."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.lyrics import cache as lyrics_cache
from app.lyrics import prefetch
from app.lyrics.client import LyricsFetchError

_LYRICS = {"available": True, "instrumental": False, "synced": None, "plain": "hi"}


def _track(tid, dur_ms=200000):
    return SimpleNamespace(id=tid, artist="A", title=f"S{tid}", album="Al", duration_ms=dur_ms)


@pytest.fixture(autouse=True)
def _clear_cache():
    lyrics_cache._CACHE.clear()
    lyrics_cache._INFLIGHT.clear()
    yield
    lyrics_cache._CACHE.clear()
    lyrics_cache._INFLIGHT.clear()


async def test_warm_upcoming_bounds_to_window():
    """Only the first N tracks are warmed, regardless of how many are queued (R1)."""
    gof = AsyncMock(return_value=dict(_LYRICS))
    tracks = [_track(f"t{i}") for i in range(5)]
    with patch("app.lyrics.cache.get_or_fetch", gof):
        await prefetch.warm_upcoming(tracks, n=3)
    assert gof.call_count == 3
    warmed_ids = [c.args[0] for c in gof.call_args_list]
    assert warmed_ids == ["t0", "t1", "t2"]          # the first N, in order


async def test_warm_upcoming_skips_already_cached():
    """A pre-warmed track is not re-fetched — get_or_fetch short-circuits on the
    cache hit, so only the uncached upcoming tracks hit LRCLIB (R4)."""
    lyrics_cache._CACHE["t0"] = dict(_LYRICS)        # already warm
    fl = AsyncMock(return_value=dict(_LYRICS))
    with patch("app.lyrics.client.fetch_lyrics", fl):
        await prefetch.warm_upcoming([_track("t0"), _track("t1"), _track("t2")], n=3)
    fetched = {c.kwargs.get("title") or c.args[1] for c in fl.call_args_list}
    assert fl.call_count == 2                          # t0 skipped (cached), t1+t2 fetched


async def test_warm_upcoming_best_effort_one_failure_doesnt_stop_others():
    """A transient failure on one track must not stop the rest of the window (R5)."""
    async def fetch(artist, title, album, duration_s):
        if title == "St1":
            raise LyricsFetchError("timeout")
        return dict(_LYRICS)

    fl = AsyncMock(side_effect=fetch)
    with patch("app.lyrics.client.fetch_lyrics", fl):
        await prefetch.warm_upcoming([_track("t0"), _track("t1"), _track("t2")], n=3)
    # t1 failed (uncached), t0 + t2 succeeded and are cached
    assert lyrics_cache.cached("t0") is not None
    assert lyrics_cache.cached("t1") is None
    assert lyrics_cache.cached("t2") is not None


async def test_warm_upcoming_empty_is_noop():
    await prefetch.warm_upcoming([], n=3)
    await prefetch.warm_upcoming(None, n=3)           # tolerate None


async def test_schedule_prefetch_is_non_blocking_and_safe():
    """schedule_prefetch returns immediately and swallows a failing task."""
    gof = AsyncMock(side_effect=RuntimeError("boom"))
    with patch("app.lyrics.cache.get_or_fetch", gof):
        result = prefetch.schedule_prefetch([_track("t0")], n=3)
        assert result is None                          # fire-and-forget, no await
        await asyncio.sleep(0.02)                       # let the task run + fail
    # No exception propagated to us; the failing task was logged, not raised.


# ── U3 wiring pin ─────────────────────────────────────────────────────────────
# _on_event in app/state.py is a closure inside the async init function (with
# manager / event types / queue_engine in local scope), so it is not unit-callable
# without booting full state init — and the existing event tests don't exercise it
# either. U2 above proves warm_upcoming/schedule_prefetch behavior; this source-level
# pin proves they are triggered on the right events: the prefetch must be scheduled
# in BOTH the queue_changed and now_playing_changed branches (R2 — re-target on every
# queue mutation incl. Play-next, and after advance).

def test_state_wires_prefetch_into_queue_and_nowplaying_events():
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app/state.py").read_text(encoding="utf-8")
    assert "from app.lyrics.prefetch import schedule_prefetch" in src, (
        "app/state.py must import schedule_prefetch."
    )
    calls = re.findall(r"schedule_prefetch\(\[i\.track for i in queue_engine\.queue\]\)", src)
    assert len(calls) >= 2, (
        f"schedule_prefetch must be wired into BOTH the queue_changed and "
        f"now_playing_changed branches of _on_event (found {len(calls)} call(s))."
    )
