"""Playback transport operations shared by every caller.

These were route handlers in ``app/api/admin.py``. They moved here because a
second caller appeared: a paired Sendspin speaker can drive transport, and
``app/output/sendspin.py`` was reaching up into ``app.api.admin`` to reuse
them. That inverted the layering ``app/output/discovery.py`` documents
("never import app.api.*") and left the two layers mutually dependent, saved
from a circular import only by keeping the imports function-local — one tidy-up
pass away from a server that will not boot.

The bodies are moved VERBATIM. Skip and Previous in particular carry
outage-hold and history behaviour that was expensive to get right, and this is
a relocation, not a rewrite. ``HTTPException`` travels with them: it is a
FastAPI import rather than an ``app.api`` one, so nothing here depends on the
API layer and the dependency graph stays a DAG.

Route handlers are now thin wrappers over these; the Sendspin backend calls
them directly. One implementation, so a speaker's Next and the web UI's Next
cannot drift apart.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException

from app import state

_log = logging.getLogger(__name__)


def _key_prefix(stream_key) -> str:
    """The source prefix of a compound stream key, for log lines: never the key
    body and never a URL (a Plex URL carries its token)."""
    key = stream_key or ""
    return key.split(":", 1)[0] if ":" in key else "<none>"

async def playback_pause():
    from app.output import session as output_session
    if output_session.output_hold_active():
        # Pause during an outage hold (R17): the output is GONE — a live
        # pause write would raise against the dead device (DLNA's
        # async_pause has no guard → 500). Record the intent so re-attach
        # lands PAUSED; the queue is already paused (the hold did that).
        output_session.get_supervisor().set_held_paused_intent()
        return {"ok": True}
    await state.output_router.pause()
    await state.queue_engine.set_paused(True)
    return {"ok": True}


async def playback_resume():
    # Manual resume from an output-outage hold (supervisor plan U3, R17):
    # works in OutagePaused (attach now, then play), Paused-after-reattach
    # and IdlePaused (window expired / flap guard), playing from the held
    # position. Checked BEFORE the Closing Time branch — while the queue is
    # held there is no playback to "continue" until the device is back, and
    # the admin pressing Play is the manual override the gates defer to.
    from app.output import session as output_session
    if output_session.output_hold_active():
        sup = output_session.get_supervisor()
        ok = await sup.manual_resume()
        if not ok:
            # Honest, machine-readable failure: tell a single-flight loss
            # (another attempt already running) apart from a device that is
            # genuinely still unreachable.
            ot = sup.peek_outage()
            if ot is not None and ot.attempt_inflight:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "attempt_in_progress",
                        "message": "a reconnect attempt is already running "
                                   "— try again shortly",
                    },
                )
            device = None
            if ot is not None:
                device = (getattr(ot.backend, "_resolved_name", None)
                          or ot.device_id)
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "device_unreachable",
                    "message": (f"Output device {device!r} is unreachable — "
                                f"still retrying" if device else
                                "Output device is unreachable — still retrying"),
                },
            )
        # The resume restarted (or resolved) playback — an active Closing
        # Time freeze is over, same as the live-resume path below.
        if state._closing_active:
            await state.clear_closing()
        return {"ok": True}
    # Closing Time (2026-06-24 plan U2): after a trigger-song freeze there is no
    # paused output to resume — clear the banner and continue with the next
    # queued track instead. Otherwise this is a normal mid-track resume.
    if state._closing_active:
        await state.clear_closing_and_continue()
        return {"ok": True}
    await state.output_router.resume()
    await state.queue_engine.set_paused(False)
    return {"ok": True}


async def playback_skip():
    """Skip to the next track in the queue.

    Calls `advance()` first, then `play()` if there's a next track, else
    `stop()`. Critically does NOT call `stop()` before `play()` — the
    earlier ordering broke the DLNA backend (stop() cleared `_dmr`, then
    play() raised RuntimeError because the backend was torn down). The
    natural-EOS advance path in `state._do_advance` also goes directly
    to play() without a prior stop(); this endpoint now matches that
    pattern. Each backend's play() handles the renderer-side transition
    from the currently-playing media to the next.

    While an outage holds the queue (supervisor plan U4, R17), Skip moves
    the HELD POINTER only — no dispatch, no set_stopped, no 502.
    """
    from app.output import session as output_session
    state._advance_gen += 1  # invalidate any pending EOS _do_advance task
    async with state._advance_lock:
        # The held check lives INSIDE the lock: an in-flight resume holds it
        # and may clear the hold before we run — deciding early would
        # pointer-pop the then-LIVE queue front into history undispatched.
        if output_session.output_hold_active():
            # R17: the old path dispatched to the dead device → 502, and its
            # set_stopped error handling destroyed the popped held item.
            # Instead: retire the held front to history (its play_recorded
            # mark intact) and let the next queued item become the held
            # front, position 0:00. The gen bump re-targets any in-flight
            # auto-resume at the new front from 0:00 (U3 pins that
            # contract). The hold — and its retry loop — survive untouched.
            # Closing Time is NOT cleared here: a pointer move restarts
            # nothing.
            await state.queue_engine.skip_held_front()
            return {"ok": True}
        next_item = await state.queue_engine.advance()
        if next_item:
            client = await state.get_plex_client()
            if client:
                # 2026-11-22 review #4: a queued track whose source vanished
                # (plex.tv stopped sharing the server -- health plan U5 / #44)
                # makes the URL build raise KeyError from the registry. That is
                # a skip past the dead item, exactly as state._do_advance
                # treats it, not a 500 on the Skip button. Bounded like
                # _do_advance's consecutive-failure loop.
                url = None
                for _ in range(state._MAX_CONSECUTIVE_FAILURES):
                    try:
                        url = state._make_stream_url(next_item.track.stream_key, client)
                        break
                    except KeyError:
                        _log.warning(
                            "playback_skip: source prefix %s of %r names no registered "
                            "source; skipping to the next item",
                            _key_prefix(next_item.track.stream_key), next_item.track.title)
                        await state._emit_track_skipped(next_item.track)
                        next_item = await state.queue_engine.advance()
                        if not next_item:
                            break
                if not next_item:
                    # The dead item(s) drained the queue: nothing left to play.
                    await state.output_router.stop()
                elif url is None:
                    _log.error("playback_skip: gave up after %d consecutive unroutable items",
                               state._MAX_CONSECUTIVE_FAILURES)
            if client and next_item and url is not None:
                try:
                    # A track you skip forward to and listen to is a real play,
                    # so it counts — via the output-session supervisor's
                    # confirmed-start chokepoint (2026-07-11 plan U1), shared
                    # with state._do_advance and playback_previous. Dispatching
                    # reports the play; record_play fires only when the backend
                    # confirms playback actually started.
                    await state.dispatch_play(
                        url, next_item.track,
                        play_recorded=bool(getattr(next_item, "play_recorded", False)),
                        # Holder handshake (2026-08-04-002 U3): this site
                        # dispatches the primary holder's stream_key.
                        holder_key=next_item.track.stream_key or None,
                    )
                    # The R19 mark protected THIS pending play; consume it so
                    # a later organic replay counts again (supervisor plan U3
                    # unified this with _play_with_fallback's consumption).
                    next_item.play_recorded = False
                except Exception:
                    _log.exception("playback_skip: play() failed for %r", next_item.track.title)
                    await state.queue_engine.set_stopped()
                    raise HTTPException(status_code=502, detail="Playback failed")
        else:
            # Queue is empty after advance — stop playback fully.
            await state.output_router.stop()
    # The admin took manual control — if a Closing Time freeze was active, the
    # party's back on: clear the banner everywhere (no-op when not frozen).
    await state.clear_closing()
    return {"ok": True}


async def playback_previous():
    """Skip Back: replay the most recently played track.

    Mirrors playback_skip's generation-bump + lock + play-without-stop
    shape, with one deliberate divergence: the Plex client is checked
    BEFORE the engine mutates. The ordering matters for the success path
    only — skip_back() itself is atomic and mutates nothing when history
    is empty (the 409 is safe regardless of ordering), but a successful
    skip_back() followed by no-client/failed play() would strand the
    interrupted track at queue front with nothing playing, which is
    harder to recover from than advance()'s history push (skip's
    silent-200 no-client path is acceptable there, not here).

    409 covers both "no history" (the button is disabled client-side;
    this is the race-condition safety net for history emptying between
    the last queue_changed event and the press) and "no Plex client".

    While an outage holds the queue (supervisor plan U4, R17), Skip Back
    moves the HELD POINTER only — a pointer move dispatches nothing and
    needs no media source, so the client gate belongs to the live branch.
    """
    from app.output import session as output_session
    state._advance_gen += 1  # invalidate any pending EOS _do_advance task
    async with state._advance_lock:
        # The held check lives INSIDE the lock: an in-flight resume holds it
        # and may clear the hold before we run — deciding early would
        # pointer-move a LIVE session's queue without dispatching.
        if output_session.output_hold_active():
            # R17: front-insert the previous history item as the new held
            # front (no dispatch, no set_stopped, no 502; hold + retry loop
            # survive). Its play_recorded mark rides along: a skipped-away
            # held item keeps its counted mark; an organically played
            # history item re-counts at resume, matching live Skip Back. The
            # gen bump re-targets any in-flight auto-resume at the new front
            # from 0:00 (U3 contract).
            prev_item = await state.queue_engine.skip_back_held_front()
            if prev_item is None:
                raise HTTPException(status_code=409,
                                    detail="No history to skip back to")
            return {"ok": True}
        client = await state.get_plex_client()
        if not client:
            raise HTTPException(status_code=409, detail="No media source available")
        # 2026-11-22 review #4: resolve the history head's URL BEFORE skip_back()
        # mutates (the same before-mutation principle as the client gate). A
        # head whose source vanished (KeyError from the registry, health plan
        # U5 / #44) is unplayable from any screen: drop it from history, flash
        # the R22 skip notice, and go back to the item before it -- the
        # closest analogue of state._do_advance skipping a dead item. Bounded
        # like its consecutive-failure loop.
        url = None
        for _ in range(state._MAX_CONSECUTIVE_FAILURES):
            history = state.queue_engine.history
            if not history:
                raise HTTPException(status_code=409, detail="No history to skip back to")
            head = history[0]
            try:
                url = state._make_stream_url(head.track.stream_key, client)
                break
            except KeyError:
                _log.warning(
                    "playback_previous: source prefix %s of %r names no registered "
                    "source; dropping it from history and going back further",
                    _key_prefix(head.track.stream_key), head.track.title)
                await state._emit_track_skipped(head.track)
                await state.queue_engine.remove_history_entry(head.track_id, head.added_at)
        if url is None:
            _log.error("playback_previous: gave up after %d consecutive unroutable history items",
                       state._MAX_CONSECUTIVE_FAILURES)
            raise HTTPException(status_code=409, detail="No playable history to skip back to")
        prev_item = await state.queue_engine.skip_back()
        if prev_item is None:
            raise HTTPException(status_code=409, detail="No history to skip back to")
        try:
            # A replay is a real play, so it counts — via the supervisor's
            # confirmed-start chokepoint (2026-07-11 plan U1), shared with
            # _do_advance and playback_skip: dispatch reports the play,
            # record_play fires only on the backend's confirmed start.
            await state.dispatch_play(
                url, prev_item.track,
                play_recorded=bool(getattr(prev_item, "play_recorded", False)),
                # Holder handshake (2026-08-04-002 U3): this site dispatches
                # the primary holder's stream_key.
                holder_key=prev_item.track.stream_key or None,
            )
            # The R19 mark protected THIS pending play; consume it so a later
            # organic replay counts again (supervisor plan U3 unified this
            # with _play_with_fallback's consumption).
            prev_item.play_recorded = False
        except Exception:
            _log.exception("playback_previous: play() failed for %r", prev_item.track.title)
            await state.queue_engine.set_stopped()
            raise HTTPException(status_code=502, detail="Playback failed")
    # Skip Back restarted playback — clear any active Closing Time freeze too.
    await state.clear_closing()
    return {"ok": True}


async def playback_volume(level: float):
    # Volume during an outage hold (supervisor plan U3, R17): accepted +
    # persisted + applied at re-attach before audio — a live device write
    # would raise against the dead output and 500 this endpoint.
    from app.output import session as output_session
    if output_session.output_hold_active():
        await output_session.set_held_volume(level)
        return {"ok": True, "level": level}
    await state.output_router.set_volume(level)
    return {"ok": True, "level": level}


# ── Settings ──────────────────────────────────────────────────────────────────


async def playback_seek(position_ms: int):
    """Seek to a position. Trivial today, but it lives here with its siblings so
    every transport verb has exactly one implementation."""
    await state.output_router.seek(position_ms)
    return {"ok": True}
