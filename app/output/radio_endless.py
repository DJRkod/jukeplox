"""Shared endless-mode (radio) reconnect policy for the output backends (U4).

Radio Mode plays an arbitrary *endless* stream through the shared output. A
station has no track boundaries, no duration, and no clean EOS: an upstream
drop surfaces as a source ERROR (GStreamer), a receiver ``IDLE(ERROR)`` (Cast),
a ``2×STOPPED`` (DLNA), or an ffmpeg end-of-input (AirPlay). In every case the
correct response is to RECONNECT (re-open the same URL, live from "now"), never
to fire ``advance_cb`` (there is nothing to advance to — R7) and never to go
silent (R12).

This module holds the pieces that are IDENTICAL across all four backends so the
per-backend suppression stays a thin, additive branch:

- the sentinel detection re-export (:func:`is_radio_track`);
- the tunable reconnect bounds as named constants (rig-tuned defaults);
- :class:`ReconnectPolicy`, the bounded + *sustained-progress-aware* attempt
  counter (ADV-5: a station dribbling a few bytes per attempt must still trip
  the cap — only SUSTAINED playback resets it, never any-byte progress);
- the failed-state hook shape (:data:`RadioFailedHook`) that U7 wires to a
  ``RadioStateEvent`` so a capped-out station surfaces as "station offline"
  rather than indefinite silence.

Device-level runtime behavior (that a re-LOAD actually re-buffers on a real
Cast, that ``playbin`` re-opens a live ``souphttpsrc``, etc.) is rig-validated;
these helpers own the LOGIC (advance-suppressed, bounded reconnect, sustained
reset, failed-state surfaced) that the mock-driven tests assert.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

# Re-export the U3 detection predicate so backends import their radio helpers
# from one place. is_radio_track(track) is True for the pseudo-Track U3 hands
# the output router (duration_ms=0 + the _radio_endless sentinel).
from app.radio.session import is_radio_track  # noqa: F401  (re-exported)

__all__ = [
    "is_radio_track",
    "RADIO_RECONNECT_MAX_ATTEMPTS",
    "RADIO_RECONNECT_BACKOFF_S",
    "RADIO_SUSTAINED_PROGRESS_S",
    "RadioFailedHook",
    "RadioTitleHook",
    "ReconnectPolicy",
    "radio_proxy_url",
    "fire_radio_failed_hook",
]


def fire_radio_failed_hook(hook: "RadioFailedHook | None",
                           log: logging.Logger, label: str) -> None:
    """F13: fire a backend's radio failed/offline hook, swallowing any exception.

    The 4 backends' ``_radio_notify_failed`` all did the identical
    try/except-swallow around ``hook()``; this is the one shared implementation.
    ``label`` names the backend in the ignored-exception debug log. Any
    backend-specific extra (e.g. Cast's ``_is_playing = False``) stays at the call
    site — this only owns the shared hook-invocation + swallow."""
    if hook is None:
        return
    try:
        hook()
    except Exception:  # pragma: no cover - hook is best-effort
        log.debug("%s radio: failed-state hook raised (ignored)", label,
                  exc_info=True)


async def radio_proxy_url(final_url: str) -> tuple[str, str] | None:
    """Cast/DLNA transcode-proxy capability URL for a radio ``final_url`` (U5).

    Only **Cast and DLNA** route radio through the server-side transcode-proxy
    (GStreamer-direct and AirPlay connect to ``final_url`` directly, host-side, and
    never call this). This is the single place those two backends turn a validated
    station URL into a device-facing capability URL, so they can never disagree:

    - mints (or supersedes) the single module-level
      :class:`~app.radio.stream.RadioStreamSession` for ``final_url`` — an instant
      switch reaps the prior encoder BEFORE the new one starts (ADV-4, no stacked
      ffmpeg), and the fresh ``session_id`` invalidates the old capability URL;
    - builds the device-facing absolute URL from ``state._stream_url_base()`` (the
      SAME base logic per-track dispatch and the flow route use — STREAM_BASE_URL,
      else a specific BIND_HOST);
    - returns ``(device_url, content_type)`` where ``content_type`` is the transcode
      OUTPUT type (authoritative), or **None** when no device-reachable base is
      configured — the caller then degrades to the direct ``final_url`` (mirrors the
      Cast flow ``_flow_base_url() is None`` fallback).
    """
    from app import state
    from app.radio import stream as radio_stream

    base = state._stream_url_base()
    if not base:
        return None
    # F4: start_radio_stream awaits the prior session's reap before spawning the
    # new encoder — no stacked ffmpeg on a rapid switch.
    sess = await radio_stream.start_radio_stream(final_url)
    return base.rstrip("/") + sess.url_path, sess.content_type

# ── reconnect bounds (rig-tuned; kept here as the single source of truth) ──────
#
# Bounded attempts before a station is declared offline (R12: "never indefinite
# silence"). Deliberately small — a healthy station reconnects on the first or
# second try; more than a handful of failures in a row means the station is
# genuinely down, and holding forever would mask that (ADV-5). Rig-tuned per the
# plan's "Deferred to Implementation" note.
RADIO_RECONNECT_MAX_ATTEMPTS = 5

# Base back-off between reconnect attempts (seconds). Backends that can pace
# their own retry loop use :meth:`ReconnectPolicy.backoff_s`; those whose
# transport re-drives immediately (e.g. GStreamer re-PLAY) may ignore it. Kept
# modest so a transient blip recovers quickly.
RADIO_RECONNECT_BACKOFF_S = 2.0

# SUSTAINED-progress threshold (seconds). The attempt counter resets ONLY after
# a connection has played for at least this long since it (re)connected — proof
# the station is actually delivering audio, not just dribbling a handshake's
# worth of bytes on each attempt. This is the ADV-5 correction: an any-byte
# reset would let a degraded station "reconnect forever". Rig-tuned.
RADIO_SUSTAINED_PROGRESS_S = 15.0


# Failed-state hook shape (U7 wires this to a RadioStateEvent so the station
# surfaces as "offline"). Called with NO arguments once the reconnect cap is
# exhausted. Deliberately parameter-less: the session/U7 already knows which
# station is active, and the backend has none of the RadioStateEvent context.
RadioFailedHook = Callable[[], None]


# Live-title hook shape (radio plan U6). Called with the newest sanitized live
# title (or None when a station has no title / a title clears). U7 wires this to
# a RadioStateEvent so the "now playing" line updates as the station's
# StreamTitle changes. The value is UNTRUSTED third-party text already sanitized
# to plain text (SEC-004) — the WS sink carries it as a JSON string the client
# renders via textContent (never innerHTML); a DIDL/Cast sink XML-escapes it.
RadioTitleHook = Callable[[Optional[str]], None]


class ReconnectPolicy:
    """Bounded, sustained-progress-aware reconnect counter for one station.

    Lifecycle, per backend:

    1. ``begin()`` when a radio play starts (resets the counter and marks the
       first connection's start time).
    2. on each upstream drop the backend asks ``should_reconnect()``:
       - it first folds in whether the just-ended connection made SUSTAINED
         progress (``>= RADIO_SUSTAINED_PROGRESS_S`` since its connect); if so
         the attempt counter is reset to zero (the station WAS working — a fresh
         drop deserves the full budget again);
       - then it increments the attempt counter and returns True while under
         :data:`RADIO_RECONNECT_MAX_ATTEMPTS`, False once the cap is hit.
    3. the backend calls ``mark_connected()`` right after it re-opens the URL so
       the next drop can measure that connection's lifetime.

    ``should_reconnect()`` returning False is the "station offline" verdict — the
    caller fires its :data:`RadioFailedHook`.

    A ``time_fn`` seam (defaults to ``time.monotonic``) keeps the sustained-vs-
    dribble distinction unit-testable without real sleeps.
    """

    def __init__(self, *, time_fn: Callable[[], float] = time.monotonic) -> None:
        self._time_fn = time_fn
        self._attempts = 0
        self._connected_at: float | None = None

    def begin(self) -> None:
        """Arm the policy for a fresh radio play (first, healthy connection)."""
        self._attempts = 0
        self._connected_at = self._time_fn()

    def mark_connected(self) -> None:
        """Record that a (re)connection just opened — starts its lifetime clock."""
        self._connected_at = self._time_fn()

    def _made_sustained_progress(self) -> bool:
        """True iff the current/just-ended connection played long enough to be
        real progress (not a few-bytes dribble). Unknown start ⇒ not sustained
        (fail-closed toward tripping the cap — ADV-5)."""
        if self._connected_at is None:
            return False
        return (self._time_fn() - self._connected_at) >= RADIO_SUSTAINED_PROGRESS_S

    def should_reconnect(self) -> bool:
        """Fold in sustained progress, then bump the attempt counter.

        Returns True while another reconnect is permitted, False once the cap is
        exhausted (⇒ the caller surfaces the failed/offline state). Only
        SUSTAINED progress resets the budget; a station that drops again within
        :data:`RADIO_SUSTAINED_PROGRESS_S` keeps burning attempts toward the cap.
        """
        if self._made_sustained_progress():
            # The connection actually worked for a while — a fresh drop starts
            # from a clean budget.
            self._attempts = 0
        # This drop consumes one attempt regardless.
        self._connected_at = None
        self._attempts += 1
        return self._attempts <= RADIO_RECONNECT_MAX_ATTEMPTS

    @property
    def attempts(self) -> int:
        """Reconnect attempts consumed since the last sustained-progress reset."""
        return self._attempts

    def backoff_s(self) -> float:
        """Suggested delay before the next attempt (flat back-off; kept simple —
        rig tuning owns whether a backend needs a ramp)."""
        return RADIO_RECONNECT_BACKOFF_S
