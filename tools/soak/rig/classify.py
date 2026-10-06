"""Classify every silent interval, and measure the gaps at track boundaries.

The analytic core of the acoustic arm. Pure functions over two timelines — the
audio features from `features.py` and the playback state from
`state_sampler.py`, both recorded on the rig's clock — so it is unit-testable
without audio hardware.

**Silence is not a defect.** A recording of a party contains a great deal of
legitimate silence: nothing queued, the host pressed skip, a track boundary, a
dispatch that has not started flowing yet. Reporting raw silences would bury the
one case that matters under hundreds that do not. The ordering below subtracts
the explainable cases first, and only what survives is an incident:

    nothing playing                      -> explained (idle)
    an operator action landed in window  -> explained (operator)
    inside the post-dispatch settle      -> pending, resolved by the next capture
    overlaps a track boundary            -> boundary gap, measured not judged
    otherwise                            -> INCIDENT

Two things that make this project-specific rather than generic:

`status: playing` flips on DISPATCH, not when audio flows. A lone silent window
right after a track change is a timing artefact, not a fault — on 2026-08-12 a
first capture read -90.3 dB against a working backend, and reported as-is it
would have condemned it. Hence `pending` rather than an immediate incident.

The run contains synthetic drivers acting as the operator. A silence next to a
driver-initiated skip is theirs. Subtracting the control plane before reading the
timeline as system behaviour is the same discipline that killed a
"flapping-advance fault" which turned out to be a person clicking skip
(docs/solutions/developer-experience/count-is-not-evidence-without-its-distribution.md).
"""
import bisect

# Audio is dispatched before it flows; this is how long a boundary is allowed to
# look silent before it counts as anything.
DEFAULT_SETTLE_MS = 8000
# How close an operator action has to be to own a silence.
DEFAULT_OPERATOR_WINDOW_MS = 5000
# How far a reported track change may sit from the silence it explains. Kept
# deliberately much tighter than the settle window: widening this to the settle
# window makes every post-dispatch silence look like a measured boundary gap, and
# `pending_settle` can then never fire at all. The two must stay distinguishable
# — one is a measurement, the other is "ask again".
DEFAULT_BOUNDARY_TOLERANCE_MS = 1500
# A position going backwards by more than this, and landing near zero, is a new
# track starting rather than a seek.
POSITION_RESET_DROP_MS = 10000
POSITION_RESET_NEAR_ZERO_MS = 5000
# A flagged silence whose measured level sits this far above the run's noise
# floor contains QUIET AUDIO, not silence. Measured 2026-09-24 over 12 hours of
# real music: 17 of 18 flagged silences sat at -67 to -82 dB against a -90.3 dB
# floor — quiet passages that dipped below silencedetect's -55 dB trigger while
# still producing sound. Only one was at the floor. Without this the detector
# has a 17-in-18 false-positive rate on a real library.
QUIET_AUDIO_MARGIN_DB = 8.0
# Silence in the first moments of a capture is the run starting, not a fault:
# the app resumes, the pipeline builds, and audio follows a beat later.
STARTUP_WINDOW_MS = 15000
# How stale a state sample may be and still describe a moment.
DEFAULT_MAX_STALENESS_MS = 5000


def index_state(rows: list[dict]) -> tuple[list[int], list[dict]]:
    """Sort state samples by time and return (times, rows) for bisect lookup."""
    ordered = sorted((r for r in rows if r.get("t") is not None),
                     key=lambda r: r["t"])
    return [r["t"] for r in ordered], ordered


def state_at(index, t_ms: int, max_staleness_ms: int = DEFAULT_MAX_STALENESS_MS):
    """The state sample describing `t_ms`, or None when none is close enough.

    Returns None rather than the nearest-at-any-distance: a sample from minutes
    away describes a different moment, and silently using it would attribute a
    silence to whatever happened to be true much earlier.
    """
    times, rows = index
    if not times:
        return None
    i = bisect.bisect_right(times, t_ms)
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(rows):
            d = abs(rows[j]["t"] - t_ms)
            if d <= max_staleness_ms and (best is None or d < best[0]):
                best = (d, rows[j])
    return best[1] if best else None


def track_boundaries(rows: list[dict]) -> list[dict]:
    """Points where one track ended and another began.

    Detected two ways, because neither alone is sufficient:

    **track_id changed.** The obvious case. Only transitions between two known,
    different tracks count — a transition into or out of "nothing playing" is a
    stop or a start, and measuring a gap across it would report the length of an
    idle period as though it were a gapless failure.

    **position reset.** A queue can hold the same track twice in a row, and then
    `track_id` never changes across a genuine boundary. That is not hypothetical:
    on 2026-09-23 a capture spanning a real boundary reported zero of them,
    because the queue held three consecutive copies of one track. The position
    going from near the end of the track to near zero is the boundary that
    `track_id` cannot see.

    A backward seek looks similar, which is why the reset must land near zero
    rather than merely go backwards — and why `classify` attributes operator
    actions before it considers boundaries at all.
    """
    _, ordered = index_state(rows)
    out = []
    prev_tid = None
    prev_pos = None
    for r in ordered:
        tid = r.get("track_id")
        pos = r.get("position_ms")

        if tid != prev_tid:
            if prev_tid is not None and tid is not None:
                out.append({"t": r["t"], "from": prev_tid, "to": tid,
                            "via": "track_id"})
            prev_tid = tid
        elif (prev_pos is not None and pos is not None
              and prev_pos - pos > POSITION_RESET_DROP_MS
              and pos < POSITION_RESET_NEAR_ZERO_MS
              and r.get("is_playing")):
            out.append({"t": r["t"], "from": tid, "to": tid,
                        "via": "position_reset",
                        "from_position_ms": prev_pos})

        if pos is not None:
            prev_pos = pos
    return out


def _overlaps(a0, a1, b0, b1) -> bool:
    return a0 <= b1 and b0 <= a1


def classify(
    silences: list[dict],
    state_rows: list[dict],
    operator_events: list[int] | None = None,
    settle_ms: int = DEFAULT_SETTLE_MS,
    operator_window_ms: int = DEFAULT_OPERATOR_WINDOW_MS,
    boundary_tolerance_ms: int = DEFAULT_BOUNDARY_TOLERANCE_MS,
    floor_db: float | None = None,
) -> list[dict]:
    """Label every silent interval. Returns the input enriched, never filtered.

    `operator_events` are epoch-ms timestamps of control-plane actions. They come
    from the driving machine's logs, so the caller is responsible for correcting
    any clock offset before passing them — this function will not guess at one.
    """
    idx = index_state(state_rows)
    bounds = track_boundaries(state_rows)
    ops = sorted(operator_events or [])
    times, _ = idx
    first_t = times[0] if times else None
    out = []

    for s in silences:
        t0, t1 = s.get("t_start"), s.get("t_end")
        rec = dict(s)
        if t0 is None:
            rec["label"] = "unknown"
            rec["why"] = "silence has no absolute timestamp"
            out.append(rec)
            continue
        t1 = t1 if t1 is not None else t0

        st = state_at(idx, t0)
        if st is None:
            rec["label"] = "unknown"
            rec["why"] = "no state sample near this silence"
            out.append(rec)
            continue

        # 1. Nothing was playing. Not a defect.
        if not st.get("is_playing"):
            rec["label"] = "explained_idle"
            rec["why"] = "no track playing"
            out.append(rec)
            continue

        # 2. An operator action owns it.
        near_op = next((o for o in ops
                        if _overlaps(t0 - operator_window_ms,
                                     t1 + operator_window_ms, o, o)), None)
        if near_op is not None:
            rec["label"] = "explained_operator"
            rec["why"] = "control-plane action within the window"
            rec["operator_t"] = near_op
            out.append(rec)
            continue

        # 3. Overlaps a reported track boundary: this is the measurement, and it
        #    is deliberately checked BEFORE the no-pipeline incident, because a
        #    non-gapless boundary legitimately tears the pipeline down and back up.
        b = next((x for x in bounds if _overlaps(t0, t1, x["t"], x["t"])), None)
        if b is None:
            # A boundary can be reported a beat after the audio actually changed.
            b = next((x for x in bounds
                      if _overlaps(t0 - boundary_tolerance_ms,
                                   t1 + boundary_tolerance_ms,
                                   x["t"], x["t"])), None)
        if b is not None:
            rec["label"] = "boundary_gap"
            rec["why"] = "silence spans a reported track change"
            rec["boundary_t"] = b["t"]
            rec["from_track"] = b["from"]
            rec["to_track"] = b["to"]
            rec["via"] = b.get("via")
            # Split the silence at the transition point. Total duration alone
            # conflates two unrelated things, and reading it as a system gap is
            # wrong: on 2026-09-23 a boundary measured 5.582 s of silence, of
            # which 5.449 s was the outgoing track's own fade-out and only
            # 0.133 s fell after the transition. The track's intrinsic silence
            # is not the player's fault.
            # CAVEAT, measured 2026-09-23: this split is only as good as the
            # state sampling rate. The boundary timestamp comes from the state
            # timeline, so at the default 1 Hz it carries ~1 s of uncertainty —
            # coarser than the gaps being split. In an A/B where total silence
            # was identical to the millisecond across 11 boundaries, post ranged
            # 0.000-0.885 s purely from where the sampler happened to observe
            # the reset. Treat the split as indicative and the TOTAL as the
            # reliable figure, unless JP_STATE_INTERVAL is well below the gaps
            # of interest.
            #
            # Clamped to the silence span at BOTH ends. A boundary reported
            # just outside the silence (within the tolerance) would otherwise
            # make one half exceed the total, which is nonsense and inflates any
            # average built on it.
            span_s = max(0.0, (t1 - t0) / 1000.0)
            rec["pre_boundary_s"] = round(
                min(span_s, max(0.0, (b["t"] - t0) / 1000.0)), 3)
            rec["post_boundary_s"] = round(
                min(span_s, max(0.0, (t1 - b["t"]) / 1000.0)), 3)
            out.append(rec)
            continue

        # 4. Still inside a settle window after the most recent boundary.
        recent = [x["t"] for x in bounds if 0 <= t0 - x["t"] <= settle_ms]
        if recent:
            rec["label"] = "pending_settle"
            rec["why"] = "within the post-dispatch settle window; re-capture to resolve"
            out.append(rec)
            continue

        # 5. Is this actually silence? silencedetect triggers at -55 dB, but
        #    music routinely dips below that while still sounding. Only a level
        #    at the run's noise floor is true silence.
        lvl = s.get("level_db")
        if lvl is not None and floor_db is not None and lvl > floor_db + QUIET_AUDIO_MARGIN_DB:
            rec["label"] = "explained_quiet_audio"
            rec["why"] = (f"measured {lvl:.1f} dB against a {floor_db:.1f} dB floor "
                          f"— quiet passage, not silence")
            out.append(rec)
            continue

        # 6. The capture had only just started; the run is still coming up.
        if first_t is not None and (t0 - first_t) <= STARTUP_WINDOW_MS:
            rec["label"] = "explained_startup"
            rec["why"] = "within the capture's start-up window"
            out.append(rec)
            continue

        # 7. What survives is a defect. Distinguish the two shapes, because they
        #    point at different layers: nothing holding the card at all is a
        #    missing pipeline; a live pipeline emitting nothing is a data-plane
        #    failure (the #55 gap).
        snd = st.get("snd_fds")
        if snd is not None and snd == 0:
            rec["label"] = "incident_no_pipeline"
            rec["why"] = "reported playing, but nothing holds the sound card"
        else:
            rec["label"] = "incident_silent_while_playing"
            rec["why"] = "reported playing, pipeline live, no audio"
        rec["snd_fds"] = snd
        out.append(rec)

    return out


def percentiles(values: list[float], ps=(50, 90, 95, 99)) -> dict:
    """Nearest-rank percentiles. Empty input yields an empty mapping.

    Distributions rather than outliers, deliberately: a count only becomes
    evidence against the distribution of its comparable units."""
    if not values:
        return {}
    ordered = sorted(values)
    out = {}
    for p in ps:
        k = max(1, int(round(p / 100.0 * len(ordered))))
        out[f"p{p}"] = ordered[min(k, len(ordered)) - 1]
    return out


def gap_summary(labelled: list[dict]) -> dict:
    """Boundary-gap distribution, plus counts for every other label."""
    gaps = [r["dur_s"] for r in labelled
            if r.get("label") == "boundary_gap" and r.get("dur_s") is not None]
    post = [r["post_boundary_s"] for r in labelled
            if r.get("label") == "boundary_gap"
            and r.get("post_boundary_s") is not None]
    counts: dict[str, int] = {}
    for r in labelled:
        counts[r.get("label", "unknown")] = counts.get(r.get("label", "unknown"), 0) + 1
    return {
        "labels": counts,
        "boundary_gaps": {
            "n": len(gaps),
            "min_s": min(gaps) if gaps else None,
            "max_s": max(gaps) if gaps else None,
            "percentiles_s": percentiles(gaps),
        },
        # The headline number. Silence AFTER the transition is the part the
        # player is responsible for; silence before it belongs to the track.
        "post_boundary": {
            "n": len(post),
            "min_s": min(post) if post else None,
            "max_s": max(post) if post else None,
            "percentiles_s": percentiles(post),
        },
    }
