"""Source stream proxy — serves media to Cast/DLNA devices that can't reach the source directly.

When the upstream content-type is ``audio/ogg`` (Ogg Vorbis or Ogg Opus),
the stream is transcoded to FLAC via ffmpeg on the fly.  Originally added
to work around pyatv miniaudio's inability to decode Ogg containers
(DecodeError -17 / ``MA_NO_DECODER``); the AirPlay path now uses the
cliap2 subprocess + FFmpeg chain in app/output/airplay.py, which handles
Ogg natively. The FLAC fallback is retained for Chromecast and DLNA, both
of which still benefit from the common-denominator container.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from collections import OrderedDict

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from app import state

_log = logging.getLogger(__name__)

router = APIRouter()


_OGG_EXTS = (".ogg", ".oga", ".opus")
# transcodes_to_flac / device_stream_content_type moved to the neutral app.transcode
# module (code-review #10) so the output backends don't import from the API layer.
# Re-exported here for backward compatibility (existing callers + tests import them
# from app.api.stream).
from app.transcode import transcodes_to_flac, device_stream_content_type  # noqa: E402,F401


def _needs_transcode(content_type: str, url: str = "") -> bool:
    """Whether a stream must be transcoded to FLAC for Cast/DLNA.

    Detection uses BOTH the upstream content-type AND the part's file
    extension. Plex does not reliably label Ogg parts as ``audio/ogg`` — it
    may serve them as ``application/octet-stream``, ``application/ogg``, or
    ``video/ogg`` — so the content-type signal alone misses real Ogg files,
    and raw Ogg then reaches a Cast receiver that rejects it with
    ``idle_reason=ERROR`` after a multi-minute load timeout. The part path
    always carries the true container extension, so it is the reliable signal
    (shared with the backends via :func:`transcodes_to_flac`).
    """
    if content_type.lower().startswith("audio/ogg"):
        return True
    return transcodes_to_flac(url)


@router.get("/api/stream")
async def proxy_plex_stream(key: str, request: Request):
    if not state.is_authorized_stream_key(key):
        raise HTTPException(status_code=403, detail="Stream key not authorized")
    client = await state.get_plex_client()
    if not client:
        raise HTTPException(status_code=503, detail="No media source configured")

    # Source-aware resolution (U4): the owning provider returns a StreamTarget
    # that is either a remote URL (proxied over HTTP, with any auth headers the
    # provider needs injected server-side so no credential rides the URL to LAN
    # devices) or a local filesystem path (served range-aware from disk).
    target = client.resolve_stream(key)
    if getattr(target, "path", None):
        # Local-file source. The provider enforces path containment (U12); the
        # key already passed is_authorized_stream_key above.
        return FileResponse(target.path)

    source_url = (getattr(target, "url", None) or "").strip()
    if not source_url:
        # Neither a path nor a URL: a local-file key that failed realpath
        # containment (U12 — traversal/symlink escape) or a source with no
        # resolvable target. Reject cleanly rather than fall through to an
        # empty-URL upstream fetch (which 502s with a misleading message).
        raise HTTPException(status_code=404, detail="Stream source unavailable")
    extra_headers = dict(getattr(target, "headers", None) or {})
    _log.debug("stream proxy: key=%s", key)

    # OGG-family sources are transcoded to a cached, seekable FLAC and served
    # range-aware from that one file. The part extension is authoritative (parts
    # carry the true container ext — 2026-06-14 learning), so we decide here
    # WITHOUT opening the source and NEVER forward the client's FLAC-coordinate
    # Range to the smaller Ogg upstream. (Forwarding it 416s, and re-transcoding
    # per Range request made the Cast assemble its buffer from many independent
    # transcodes — the 2026-06-17 416-spam / repeated-transcode / mid-playback
    # ERROR.)
    if transcodes_to_flac(source_url):
        return await _serve_transcoded_flac(key, source_url, extra_headers)

    # Non-Ogg: open the upstream, forwarding Range for native byte-seeking.
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=None, write=None, pool=None),
        follow_redirects=True,
    )
    range_header = request.headers.get("range")
    req_headers = dict(extra_headers)
    if range_header:
        req_headers["Range"] = range_header
    try:
        upstream_req = http_client.build_request("GET", source_url, headers=req_headers)
        upstream_resp = await http_client.send(upstream_req, stream=True)
    except Exception:
        await http_client.aclose()
        raise HTTPException(status_code=502, detail="Stream source unavailable")

    content_type = upstream_resp.headers.get("content-type", "application/octet-stream")
    # Rare: an Ogg part served without an .ogg-family extension — the
    # content-type is the only signal. Transcode it too (cached, no Range).
    if content_type.lower().startswith("audio/ogg"):
        await upstream_resp.aclose()
        await http_client.aclose()
        return await _serve_transcoded_flac(key, source_url, extra_headers)

    return _stream_passthrough(upstream_resp, http_client)


def _stream_passthrough(plex_resp, http_client: httpx.AsyncClient) -> StreamingResponse:
    resp_headers: dict[str, str] = {
        "Content-Type": plex_resp.headers.get("content-type", "application/octet-stream"),
        "Accept-Ranges": "bytes",
    }
    for h in ("content-length", "content-range"):
        if h in plex_resp.headers:
            resp_headers[h.title()] = plex_resp.headers[h]

    async def _stream():
        try:
            async for chunk in plex_resp.aiter_bytes(chunk_size=65536):
                yield chunk
        finally:
            await plex_resp.aclose()
            await http_client.aclose()

    return StreamingResponse(_stream(), status_code=plex_resp.status_code, headers=resp_headers)


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


async def _transcode_to_flac_file(plex_resp) -> str:
    """Transcode the upstream Ogg response into a **seekable** temp FLAC file.

    Writing to a real file rather than ``pipe:1`` is the whole point: ffmpeg
    can only backfill the FLAC STREAMINFO (true total-sample count) and write
    a seektable when the output is seekable. A pipe leaves the stream with an
    unknown duration and no seektable — which a constrained Cast receiver
    (e.g. a JBL Charge 5) cannot range/seek, so it tears the media session
    (and the CASTV2 control channel) down ~1s in (the 2026-06-17 bug). A real
    file also gives us a Content-Length and HTTP range support downstream.

    ``-sample_fmt s16`` keeps the output to universally supported 16-bit FLAC:
    Vorbis decodes to float, which ffmpeg would otherwise encode as 24-bit
    FLAC ("considered experimental"), another constrained-decoder risk.

    Returns the temp file path; the caller owns deletion. Raises
    ``FileNotFoundError`` if ffmpeg is absent and ``RuntimeError`` on a
    non-zero ffmpeg exit (the temp file is removed on failure).
    """
    fd, tmp_path = tempfile.mkstemp(prefix="jukeplox-cast-", suffix=".flac")
    os.close(fd)
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-loglevel", "error",
            "-i", "pipe:0",
            "-c:a", "flac",
            "-sample_fmt", "s16",
            "-y", tmp_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        _unlink_quietly(tmp_path)
        raise

    async def _feeder():
        try:
            async for chunk in plex_resp.aiter_bytes(chunk_size=65536):
                proc.stdin.write(chunk)
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # ffmpeg exited early; its non-zero rc is handled below
        except Exception:
            _log.exception("stream proxy: upstream→ffmpeg feeder failed")
        finally:
            try:
                proc.stdin.close()
                await proc.stdin.wait_closed()
            except Exception:
                pass

    feeder_task = asyncio.create_task(_feeder())
    # stderr.read() drains until ffmpeg closes the pipe (i.e. exits); the feeder
    # runs concurrently as a task, so stdin keeps flowing while we wait here.
    stderr = await proc.stderr.read()
    rc = await proc.wait()
    await feeder_task
    if rc != 0:
        _unlink_quietly(tmp_path)
        raise RuntimeError(
            f"ffmpeg exited {rc}: {stderr.decode('utf-8', errors='replace').strip()}"
        )
    await _add_flac_seektable(tmp_path)
    return tmp_path


async def _add_flac_seektable(flac_path: str) -> None:
    """Inject a SEEKTABLE into the transcoded FLAC via ``metaflac`` (best-effort).

    ffmpeg's FLAC muxer writes no seektable. Without one a constrained Cast
    receiver (JBL Charge 5) can't map seek-time -> byte: it blind-byte-estimates
    the offset, lands mid-stream, and its position model desyncs — so after a
    *seek* the track ends with idle_reason=ERROR + a long pause instead of a
    clean FINISHED (2026-06-17; straight playthrough is unaffected). A seektable
    lets it seek accurately and end cleanly.

    Best-effort by design: a missing/failed metaflac degrades to a
    seektable-less FLAC (which still plays straight through), so we log and
    continue rather than fail the stream.

    REVISIT: this is the spec-correct fix, but it is unverified on the JBL
    specifically. If seeking still mis-behaves WITH a seektable, rethink the
    approach — e.g. transcode to a CBR-seekable codec, or advance the queue
    proactively from our own position tracking instead of trusting the receiver.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "metaflac", "--add-seekpoint=10s", flac_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        _log.warning("metaflac not found — serving FLAC without a seektable; "
                     "Cast seeking may desync and ERROR at end-of-track")
        return
    try:
        err = await proc.stderr.read()
        rc = await proc.wait()
    except Exception:
        _log.warning("metaflac --add-seekpoint raised — serving without seektable",
                     exc_info=True)
        return
    if rc != 0:
        _log.warning("metaflac --add-seekpoint failed (rc=%s): %s — serving "
                     "without seektable", rc,
                     err.decode("utf-8", errors="replace").strip())


# Transcoded-FLAC cache: stream key → temp FLAC path. A Cast media player
# issues many probe/Range GETs for one track; without a cache each would
# re-fetch from Plex and re-transcode the whole file, and the receiver would
# assemble its buffer from many independent transcodes (the 2026-06-17 416-spam
# / repeated-transcode / mid-playback ERROR). Playback is sequential, so a tiny
# LRU is plenty. Files are unlinked on eviction; on Linux an in-flight
# FileResponse keeps its already-open fd valid after the unlink.
_TRANSCODE_CACHE: "OrderedDict[str, str]" = OrderedDict()
_TRANSCODE_CACHE_MAX = 4
_cache_lock = asyncio.Lock()                            # guards the OrderedDict (O(1) ops only)
_transcode_inflight: "dict[str, asyncio.Future]" = {}  # key -> in-flight transcode task


def _pinned_keys() -> set[str]:
    """Stream keys the LRU must never evict: every holder key of the
    CURRENTLY-PLAYING track (2026-07-11 supervisor plan U6 sizing guard).

    The effective-next prefetch (``state._warm_next_transcode``) now inserts
    entries while the current track's artifact is still being range-served —
    without the pin, a warm plus a burst of probe GETs could push the current
    track out of LRU order, unlink its file, and force a mid-playback
    re-transcode on the next Range request (the 2026-06-17 failure class).
    Pinning by key (rather than raising capacity) is the minimal mechanism:
    LRU semantics stay intact for everything else, no new tunable, and the
    worst-case overshoot is bounded by ONE track's holder count. Reads the
    queue engine at eviction time so the pin follows playback with no
    write-through bookkeeping to forget."""
    cur = state.queue_engine.state.current
    if cur is None:
        return set()
    keys = {h.get("key") for h in (getattr(cur.track, "holds", None) or [])}
    keys.discard(None)
    if cur.track.stream_key:
        keys.add(cur.track.stream_key)
    return keys


async def _fetch_and_transcode(plex_url: str, headers: dict | None = None) -> str:
    """Fetch the FULL upstream Ogg (no Range) and transcode it to a seekable
    temp FLAC file. The client's Range is never forwarded — FLAC byte offsets
    don't map to Ogg offsets, and a FLAC-coordinate Range 416s against the
    smaller Ogg source. Any provider auth headers are injected server-side."""
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=None, write=None, pool=None),
        follow_redirects=True,
    )
    try:
        plex_resp = await http_client.send(
            http_client.build_request("GET", plex_url, headers=headers or {}), stream=True
        )
    except Exception:
        await http_client.aclose()
        raise HTTPException(status_code=502, detail="Stream source unavailable")
    try:
        return await _transcode_to_flac_file(plex_resp)
    finally:
        await plex_resp.aclose()
        await http_client.aclose()


async def _get_or_transcode(key: str, plex_url: str, headers: dict | None = None) -> str:
    """Return a cached transcoded FLAC path for *key*, transcoding once on miss.

    Concurrent requests for the SAME key coalesce onto one transcode (a per-key
    in-flight future), so a burst of Cast Range probes transcodes exactly once;
    requests for DIFFERENT keys transcode CONCURRENTLY. The lock is held only for
    O(1) OrderedDict reads/writes — never across the transcode itself — so one
    slow track no longer blocks unrelated tracks (code-review #2, 2026-06-18).
    Evicts (and unlinks) the least-recently-used entries.
    """
    async with _cache_lock:
        path = _TRANSCODE_CACHE.get(key)
        if path and os.path.exists(path):
            _TRANSCODE_CACHE.move_to_end(key)
            return path
        if path:  # cached entry whose temp file vanished — drop and remake
            _TRANSCODE_CACHE.pop(key, None)
        task = _transcode_inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(_fetch_and_transcode(plex_url, headers))
            _transcode_inflight[key] = task
            task.add_done_callback(lambda _t, k=key: _transcode_inflight.pop(k, None))

    # Await OUTSIDE the lock so different keys transcode concurrently; same-key
    # callers share this one task. shield keeps a cancelled awaiter (client
    # disconnect) from cancelling the shared transcode.
    new_path = await asyncio.shield(task)

    async with _cache_lock:
        if key not in _TRANSCODE_CACHE:
            _TRANSCODE_CACHE[key] = new_path
            _TRANSCODE_CACHE.move_to_end(key)
            # Evict LRU-first, skipping the currently-playing track's keys and
            # the entry just made (U6 sizing guard — see _pinned_keys). All
            # candidates pinned → stop: a bounded overshoot beats unlinking
            # the artifact a device is actively range-reading.
            pinned = _pinned_keys()
            pinned.add(key)
            while len(_TRANSCODE_CACHE) > _TRANSCODE_CACHE_MAX:
                victim = next((k for k in _TRANSCODE_CACHE if k not in pinned),
                              None)
                if victim is None:
                    break
                _unlink_quietly(_TRANSCODE_CACHE.pop(victim))
            _log.info("stream proxy: transcoded → seekable FLAC (%d bytes); "
                      "cached as %s, serving range-aware", os.path.getsize(new_path), key)
    return new_path


async def _serve_transcoded_flac(key: str, plex_url: str, headers: dict | None = None):
    """Serve the cached, seekable FLAC for *key* via a range-aware FileResponse
    (Content-Length + Accept-Ranges + 206). Transcodes once on cache miss."""
    try:
        path = await _get_or_transcode(key, plex_url, headers)
    except HTTPException:
        raise
    except FileNotFoundError:
        _log.error("ffmpeg not found in PATH — cannot transcode %s", plex_url)
        raise HTTPException(status_code=502, detail="Transcoder unavailable")
    except Exception as exc:
        _log.warning("stream proxy: transcode failed for %s: %s", plex_url, exc)
        raise HTTPException(status_code=502, detail="Transcode failed")
    return FileResponse(path, media_type="audio/flac")


# ── Cast flow-mode stream (2026-07-11 supervisor plan U9) ─────────────────────
# The api→output import direction is the allowed one (app/transcode.py's
# layering rule forbids only the reverse); the flow ENGINE lives in
# app/output/flow.py and this route is its single HTTP consumer surface.
from app.output import flow as output_flow  # noqa: E402


async def _primed_flow_body(first: bytes, rest):
    """The flow response body: the primed first chunk, then the bound
    generator (``itertools.chain`` shape). The finally closes ``rest``
    deterministically whenever this wrapper starts; if Starlette abandons
    the wrapper UNSTARTED (instant disconnect), dropping it releases the
    only reference to ``rest`` — which IS started (suspended at a yield),
    so asyncio's async-generator finalization runs its binding-release
    finally promptly."""
    try:
        yield first
        async for chunk in rest:
            yield chunk
    finally:
        await rest.aclose()


@router.get("/api/stream/flow/{session_id}")
async def stream_flow(session_id: str, request: Request):
    """Serve the continuous flow-mode stream for the active flow session.

    SINGLE-SESSION resource, entirely unlike the seekable per-track proxy
    above: exactly one consumer may be bound. A consumer disconnect arms the
    session's short grace timer, and a new GET within it RE-BINDS the same
    session mid-stream (Cast receivers re-request the media URL after
    transient hiccups — the stream resumes from the current encode position,
    never from zero). A second concurrent GET while a consumer is bound is
    rejected with 409 and never spawns a second stitcher.

    Auth posture: capability URL — deliberately NO ``is_authorized_stream_key``
    check on this route. ``session_id`` is an unguessable 128-bit token
    (``secrets.token_urlsafe(16)``) minted per flow session, and the id
    itself IS the credential: presenting it is the authorization, the same
    no-cookie posture as the per-track route's queue-authorized ``?key=``
    (renderers can't send cookies, so a bearer-style URL secret is the only
    workable scheme). A wrong or stale id 404s below having done no work,
    so unauthenticated probing costs one registry dict lookup — there is
    nothing an extra key check would reject earlier or more cheaply.

    Served chunked and Range-less (the Cast live-read pattern): any Range
    header is ignored and the response advertises ``Accept-Ranges: none`` —
    a flow stream has no byte-addressable past to seek into.

    The binding is PRIMED here rather than handed to Starlette cold:
    ``bind_consumer`` marks the single consumer bound synchronously, but
    only the generator body's finally releases it — and a never-STARTED
    async generator runs no body and no finally, so a client that dropped
    before Starlette's first ``__anext__`` would strand the binding forever
    (every receiver re-request 409s, with the recovery grace armed only in
    that same finally). Awaiting the first chunk in the route guarantees the
    generator is started, and a started generator's finally IS guaranteed —
    cancellation mid-priming, ``aclose``, and GC finalization all run it.
    No await separates the conflict check from the bind, so two rapid GETs
    still resolve to exactly one 200 and one 409.
    """
    del request  # Range-less by design; the request carries nothing we honor
    session = output_flow.get_flow_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="No active flow session")
    headers = {"Accept-Ranges": "none", "Cache-Control": "no-store"}
    try:
        body = session.bind_consumer()
    except output_flow.FlowConsumerConflict:
        raise HTTPException(
            status_code=409, detail="Flow stream already bound to a consumer")
    try:
        first = await body.__anext__()  # the priming read (see docstring)
    except StopAsyncIteration:
        # Ended-and-drained session: nothing left to stream. The priming ran
        # the generator to completion, finally included — nothing to release.
        return Response(b"", media_type=session.content_type, headers=headers)
    return StreamingResponse(
        _primed_flow_body(first, body),
        media_type=session.content_type,
        headers=headers,
    )
