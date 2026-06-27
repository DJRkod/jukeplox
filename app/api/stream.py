"""Plex stream proxy — serves media to Cast/DLNA devices that can't reach Plex directly.

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
from fastapi.responses import FileResponse, StreamingResponse

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
        raise HTTPException(status_code=503, detail="Plex not configured")

    plex_url = client.stream_url(key)
    _log.debug("stream proxy: key=%s", key)

    # OGG-family sources are transcoded to a cached, seekable FLAC and served
    # range-aware from that one file. The part extension is authoritative (Plex
    # parts always carry the true container ext — 2026-06-14 learning), so we
    # decide here WITHOUT opening Plex and NEVER forward the client's
    # FLAC-coordinate Range to the smaller Ogg upstream. (Forwarding it 416s
    # against Plex, and re-transcoding per Range request made the Cast assemble
    # its buffer from many independent transcodes — the 2026-06-17 416-spam /
    # repeated-transcode / mid-playback ERROR.)
    if transcodes_to_flac(plex_url):
        return await _serve_transcoded_flac(key, plex_url)

    # Non-Ogg: open the upstream, forwarding Range for native byte-seeking.
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=None, write=None, pool=None),
        follow_redirects=True,
    )
    range_header = request.headers.get("range")
    try:
        plex_req = http_client.build_request(
            "GET", plex_url,
            headers={"Range": range_header} if range_header else {},
        )
        plex_resp = await http_client.send(plex_req, stream=True)
    except Exception:
        await http_client.aclose()
        raise HTTPException(status_code=502, detail="Plex stream unavailable")

    content_type = plex_resp.headers.get("content-type", "application/octet-stream")
    # Rare: an Ogg part Plex serves without an .ogg-family extension — the
    # content-type is the only signal. Transcode it too (cached, no Range).
    if content_type.lower().startswith("audio/ogg"):
        await plex_resp.aclose()
        await http_client.aclose()
        return await _serve_transcoded_flac(key, plex_url)

    return _stream_passthrough(plex_resp, http_client)


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


async def _fetch_and_transcode(plex_url: str) -> str:
    """Fetch the FULL upstream Ogg (no Range) and transcode it to a seekable
    temp FLAC file. The client's Range is never forwarded — FLAC byte offsets
    don't map to Ogg offsets, and a FLAC-coordinate Range 416s against the
    smaller Ogg source."""
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=None, write=None, pool=None),
        follow_redirects=True,
    )
    try:
        plex_resp = await http_client.send(
            http_client.build_request("GET", plex_url), stream=True
        )
    except Exception:
        await http_client.aclose()
        raise HTTPException(status_code=502, detail="Plex stream unavailable")
    try:
        return await _transcode_to_flac_file(plex_resp)
    finally:
        await plex_resp.aclose()
        await http_client.aclose()


async def _get_or_transcode(key: str, plex_url: str) -> str:
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
            task = asyncio.ensure_future(_fetch_and_transcode(plex_url))
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
            while len(_TRANSCODE_CACHE) > _TRANSCODE_CACHE_MAX:
                _, evicted = _TRANSCODE_CACHE.popitem(last=False)
                if evicted != new_path:  # never unlink the file we just made
                    _unlink_quietly(evicted)
            _log.info("stream proxy: transcoded → seekable FLAC (%d bytes); "
                      "cached as %s, serving range-aware", os.path.getsize(new_path), key)
    return new_path


async def _serve_transcoded_flac(key: str, plex_url: str):
    """Serve the cached, seekable FLAC for *key* via a range-aware FileResponse
    (Content-Length + Accept-Ranges + 206). Transcodes once on cache miss."""
    try:
        path = await _get_or_transcode(key, plex_url)
    except HTTPException:
        raise
    except FileNotFoundError:
        _log.error("ffmpeg not found in PATH — cannot transcode %s", plex_url)
        raise HTTPException(status_code=502, detail="Transcoder unavailable")
    except Exception as exc:
        _log.warning("stream proxy: transcode failed for %s: %s", plex_url, exc)
        raise HTTPException(status_code=502, detail="Transcode failed")
    return FileResponse(path, media_type="audio/flac")
