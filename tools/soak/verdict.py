"""Verdict helpers: trends, instrument health, and incident ranking.

Pure functions over the soak's JSONL streams, kept out of `analyse.py` so they
can be tested without running the report.

The instrument-health checks are the important part. A soak that finds nothing
has two possible meanings — nothing broke, or nobody was looking — and the
report must be able to tell them apart. This repo has the scar: a fix that was
rig-validated, 48-test green and mutation-tested still leaked two ffmpeg
processes per operation, because every gate measured the endpoint rather than
the resource.
"""

# The container probe emits five zeros when its process match fails, which is
# indistinguishable from a genuinely flat process unless something says so.
_ZERO_SAMPLE = {"rss_kb": 0, "threads": 0, "fds": 0, "children": 0,
                "media_procs": 0}


def sampler_health(mon_rows: list[dict]) -> str | None:
    """None when the container sampler was actually working, else why not.

    `/root/jp-probe.sh` prints `0 0 0 0 0` when it cannot find the app process
    inside the container — and a run of all-zero samples looks exactly like a
    beautifully stable system. Reporting that as a clean result is how a blind
    instrument passes for a healthy one.
    """
    polls = [r for r in mon_rows if r.get("kind") == "poll"]
    if not polls:
        return "no container samples at all"
    errs = [r for r in polls if r.get("_proc_err")]
    if len(errs) == len(polls):
        return f"every container sample failed ({errs[0].get('_proc_err')})"
    usable = [r for r in polls if "rss_kb" in r]
    if not usable:
        return "no container sample carried process fields"
    if all(all(r.get(k) == v for k, v in _ZERO_SAMPLE.items()) for r in usable):
        return ("every container sample is all-zero — the probe's process match "
                "failed; this is a broken sampler, not a stable system")
    if all(r.get("rss_kb") == usable[0].get("rss_kb") for r in usable) and len(usable) > 5:
        return ("every container sample reports an identical RSS — suspect the "
                "probe is reading the wrong process (PID 1 is a shell wrapper "
                "and reports a flat value forever)")
    return None


def harness_health(party_rows: list[dict], min_adds: int = 20) -> str | None:
    """None when the guests actually did something, else why not.

    A soak where the selectors silently matched nothing looks exactly like a
    soak where everything worked.
    """
    if not party_rows:
        return "no guest actions recorded at all"
    adds = sum(1 for r in party_rows
               if r.get("act") == "searchAndAdd" and r.get("added"))
    adds += sum(1 for r in party_rows
                if r.get("act") == "drillArtistAlbum"
                and (r.get("c") or {}).get("added"))
    if adds < min_adds:
        return (f"only {adds} tracks were added by guests — suspect the "
                f"selectors matched nothing rather than a quiet party")
    return None


def trend(rows: list[dict], field: str) -> dict:
    """First/last/delta for a numeric field, plus a per-hour rate.

    Rows without the field are skipped rather than counted as zero — a missing
    sample is not a measurement of nothing.
    """
    pts = [(r.get("t"), r.get(field)) for r in rows
           if isinstance(r.get(field), (int, float)) and r.get("t") is not None]
    if len(pts) < 2:
        return {"n": len(pts), "first": None, "last": None, "delta": None,
                "per_hour": None}
    pts.sort(key=lambda p: p[0])
    (t0, v0), (t1, v1) = pts[0], pts[-1]
    hours = (t1 - t0) / 3600.0 if t1 > t0 else None
    return {
        "n": len(pts), "first": v0, "last": v1, "delta": v1 - v0,
        "per_hour": round((v1 - v0) / hours, 1) if hours else None,
        "min": min(v for _, v in pts), "max": max(v for _, v in pts),
    }


def memory_note(rss_trend: dict) -> str:
    """What the RSS trend does and does not establish.

    Freed Python objects return to allocator arenas rather than the OS, so a
    rising RSS is consistent with both a leak and ordinary churn. Saying so is
    the difference between a finding and a guess.
    """
    if rss_trend.get("per_hour") is None:
        return "RSS trend: not enough samples to say anything."
    per_h = rss_trend["per_hour"] / 1024.0
    if per_h <= 0:
        return (f"RSS trend: {per_h:+.1f} MB/hour — not growing. "
                f"No leak indicated.")
    return (
        f"RSS trend: {per_h:+.1f} MB/hour "
        f"({rss_trend['first'] / 1024:.0f} -> {rss_trend['last'] / 1024:.0f} MB). "
        "This does NOT establish a leak on its own: freed objects return to "
        "allocator arenas rather than the OS, so churn and a leak look alike "
        "here. To settle it, run an idle tail after the load stops and compare "
        "RSS across two workload cycles — a plateau is arena reuse, two similar "
        "growths is a leak."
    )


_SEVERITY = {
    "incident_silent_while_playing": 0,
    "incident_no_pipeline": 1,
    "unknown": 2,
    "pending_settle": 3,
    "boundary_gap": 4,
    "explained_operator": 5,
    "explained_idle": 6,
    "explained_quiet_audio": 7,
    "explained_startup": 8,
}


def rank_labelled(labelled: list[dict]) -> list[dict]:
    """Most severe first, longest first within a label.

    Silence while the system claims to be playing is the headline defect this
    whole arm exists to catch, so it sorts above everything — including above
    `unknown`, which is a harness gap rather than a product fault but still
    ranks above anything explained.
    """
    return sorted(
        labelled,
        key=lambda r: (_SEVERITY.get(r.get("label"), 9), -(r.get("dur_s") or 0)),
    )


def subprocess_note(media_trend: dict, backend: str | None = None) -> str:
    """What the media-subprocess count means, which depends on the backend.

    Direct's GStreamer pipeline runs IN-PROCESS and never appears as an
    ffmpeg/gst subprocess, so on a Direct-pinned arm this count is flat zero by
    construction. Reporting "no subprocess growth" there would be the same
    flat-looks-healthy trap the all-zero sampler check exists for, one level up.
    """
    if backend == "direct":
        return ("Media subprocess count is not a liveness signal on the Direct "
                "backend — its GStreamer pipeline runs in-process, so this stays "
                "at 0 while audio plays. Use the /dev/snd descriptor count from "
                "the rig-local state timeline instead.")
    if media_trend.get("delta") is None:
        return "Media subprocess count: not enough samples."
    if media_trend["delta"] > 0:
        return (f"Media subprocess count grew {media_trend['first']} -> "
                f"{media_trend['last']}. A steady climb is the #51 signature "
                f"(two unreaped ffmpeg per Apply-while-playing) and should be "
                f"treated as a finding, not a chart.")
    return (f"Media subprocess count stable "
            f"({media_trend['min']}-{media_trend['max']}).")


def flatten_container(rows: list[dict]) -> list[dict]:
    """Lift `container` sub-dict fields to the top level.

    `monitor.py` nests its container sample under a `container` key, while every
    function here reads flat fields. Converting explicitly beats each function
    guessing at the shape — and a silent mismatch would make every trend read as
    "no samples", which looks identical to a healthy flat process.
    """
    out = []
    for r in rows:
        c = r.get("container")
        out.append({**r, **c} if isinstance(c, dict) else dict(r))
    return out
