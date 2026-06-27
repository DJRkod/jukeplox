"""Tests for app.api.stream — Ogg detection and transcoding routing."""

import os
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("content_type,expected", [
    ("audio/ogg", True),
    ("audio/ogg; codecs=vorbis", True),
    ("Audio/OGG", True),
    ("audio/flac", False),
    ("audio/mpeg", False),
    ("audio/mp4", False),
    ("audio/x-flac", False),
    ("application/octet-stream", False),
    ("", False),
])
def test_needs_transcode(content_type: str, expected: bool):
    """Only Ogg-family containers should be flagged for transcoding.

    pyatv's miniaudio backend cannot decode Ogg Vorbis/Opus, so /api/stream
    must transcode it.  Everything else (FLAC, MP3, AAC, etc.) miniaudio
    handles natively or those formats reach downstream players in their
    native form.
    """
    from app.api.stream import _needs_transcode
    assert _needs_transcode(content_type) is expected


@pytest.mark.parametrize("content_type,url,expected", [
    # The bug: Plex does not reliably label Ogg parts as audio/ogg. When it
    # serves an .ogg as octet-stream / application-ogg / video-ogg, the
    # content-type signal misses it and raw Ogg reaches a Cast receiver that
    # rejects it (idle_reason=ERROR). The part's file EXTENSION is the reliable
    # signal and must trigger transcode regardless of content-type.
    ("application/octet-stream", "http://plex/library/parts/67202/1/file.ogg?X-Plex-Token=t", True),
    ("application/ogg", "http://plex/library/parts/1/2/file.oga", True),
    ("video/ogg", "http://plex/library/parts/1/2/file.ogg", True),
    ("audio/ogg", "http://plex/library/parts/1/2/file.opus?x=1", True),
    # Content-type signal still works when the URL is unknown/empty.
    ("audio/ogg", "", True),
    ("application/octet-stream", "", False),
    # Cast/DLNA-native formats stay passthrough even with the URL present.
    ("audio/flac", "http://plex/library/parts/1/2/file.flac", False),
    ("application/octet-stream", "http://plex/library/parts/1/2/file.mp3", False),
    ("audio/mpeg", "http://plex/library/parts/1/2/file.mp3", False),
])
def test_needs_transcode_extension_aware(content_type: str, url: str, expected: bool):
    """Transcode detection must consider the part's file extension, not just
    the upstream content-type — Plex mislabels Ogg parts and the content-type
    alone lets raw Ogg through to Cast/DLNA."""
    from app.api.stream import _needs_transcode
    assert _needs_transcode(content_type, url) is expected


@pytest.mark.parametrize("part_path,expected", [
    ("/library/parts/1/2/file.ogg", True),
    ("/library/parts/1/2/file.oga", True),
    ("/library/parts/1/2/file.opus", True),
    ("/library/parts/1/2/file.OGG", True),
    ("/library/parts/1/2/file.ogg?X-Plex-Token=t", True),
    ("/library/parts/1/2/file.flac", False),
    ("/library/parts/1/2/file.mp3", False),
    ("", False),
])
def test_transcodes_to_flac(part_path: str, expected: bool):
    """transcodes_to_flac is the single source of truth for "will /api/stream
    turn this source into FLAC?" — the Cast/DLNA backends consult it to advertise
    the served content-type instead of the source one."""
    from app.api.stream import transcodes_to_flac
    assert transcodes_to_flac(part_path) is expected


def test_transcodes_to_flac_matches_needs_transcode_extension_arm():
    """The device-facing predicate must agree with the proxy's own extension
    arm — they are the same OGG rule and must never drift (the 2026-06-17 Cast
    bug was the two content-type decisions diverging)."""
    from app.api.stream import transcodes_to_flac, _needs_transcode
    for ext in (".ogg", ".oga", ".opus", ".flac", ".mp3"):
        part = f"/library/parts/1/2/file{ext}"
        # _needs_transcode keys the extension arm off the same path (no content
        # -type hint), so the two must return the same answer.
        assert transcodes_to_flac(part) is _needs_transcode("", part)


@pytest.mark.parametrize("stream_url,part_path,native,expected", [
    # Proxied OGG → /api/stream serves FLAC, so advertise audio/flac, NOT the
    # source audio/ogg (the 2026-06-17 Chromecast "~1s then stops" bug).
    ("http://192.168.0.70/api/stream?key=k%2Ffile.ogg", "/library/parts/1/2/file.ogg", "audio/ogg", "audio/flac"),
    # Proxied non-OGG → no transcode, native passes through unchanged.
    ("http://192.168.0.70/api/stream?key=k%2Ffile.flac", "/library/parts/1/2/file.flac", "audio/flac", "audio/flac"),
    ("http://192.168.0.70/api/stream?key=k%2Ffile.mp3", "/library/parts/1/2/file.mp3", "audio/mpeg", "audio/mpeg"),
    # Direct Plex URL (no proxy) → /api/stream isn't in the path, nothing is
    # transcoded, keep the native source type even for OGG.
    ("http://plex.local/library/parts/1/2/file.ogg?X-Plex-Token=t", "/library/parts/1/2/file.ogg", "audio/ogg", "audio/ogg"),
    # Code-review #11: proxied OGG that Plex serves WITHOUT an ogg extension but
    # WITH an audio/ogg native type → /api/stream still transcodes (content-type
    # arm of _needs_transcode), so advertise audio/flac, not audio/ogg.
    ("http://192.168.0.70/api/stream?key=k%2Ffile.bin", "/library/parts/1/2/file.bin", "audio/ogg", "audio/flac"),
])
def test_device_stream_content_type(stream_url: str, part_path: str, native: str, expected: str):
    """The content-type a Cast/DLNA renderer is told must equal what /api/stream
    actually serves: audio/flac for a proxied OGG source, native otherwise."""
    from app.api.stream import device_stream_content_type
    assert device_stream_content_type(stream_url, part_path, native) == expected


# ── transcoded-stream delivery (2026-06-17 Chromecast bug) ───────────────────
# The proxy used to pipe ffmpeg's stdout (`-f flac pipe:1`), which produces a
# FLAC with no STREAMINFO duration and no seektable, served chunked with no
# Content-Length and Accept-Ranges:none. A constrained Cast receiver (JBL
# Charge 5) ranges/seeks the resource, can't, and drops the session ~1s in.
# The fix transcodes to a SEEKABLE temp file and serves it range-aware.


class _FakeResp:
    """Minimal stand-in for an httpx streaming response: an async byte source
    plus an awaitable aclose(), enough for the transcode/serve helpers."""

    def __init__(self, data: bytes = b""):
        self._data = data
        self.closed = False

    async def aiter_bytes(self, chunk_size: int = 65536):
        for i in range(0, len(self._data), chunk_size):
            yield self._data[i:i + chunk_size]

    async def aclose(self):
        self.closed = True


class _FakeClient:
    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


async def test_serve_transcoded_flac_returns_range_capable_file_response(tmp_path, monkeypatch):
    """The transcoded stream is served as a seekable, length-known FileResponse
    (Content-Length + Accept-Ranges + 206), NOT a chunked StreamingResponse with
    Accept-Ranges:none — otherwise a JBL Charge 5 ranges it and fails (2026-06-17)."""
    from app.api import stream as s
    from fastapi.responses import FileResponse

    flac = tmp_path / "out.flac"
    flac.write_bytes(b"fLaC" + b"\0" * 4096)

    async def _fake_get_or_transcode(key, plex_url):
        return str(flac)

    monkeypatch.setattr(s, "_get_or_transcode", _fake_get_or_transcode)

    resp = await s._serve_transcoded_flac("k", "http://plex/x.ogg")
    assert isinstance(resp, FileResponse)
    assert resp.media_type == "audio/flac"


async def test_get_or_transcode_caches_one_transcode_per_key(tmp_path, monkeypatch):
    """A burst of Cast probe/Range requests for the same track must transcode
    exactly once and reuse the cached file — the 2026-06-17 fix for the Cast
    416-spam / repeated full-re-transcode storm and the gap before advancing."""
    from app.api import stream as s

    s._TRANSCODE_CACHE.clear()
    calls = {"n": 0}

    async def _fake_fetch(plex_url):
        calls["n"] += 1
        p = tmp_path / f"f{calls['n']}.flac"
        p.write_bytes(b"fLaC")
        return str(p)

    monkeypatch.setattr(s, "_fetch_and_transcode", _fake_fetch)
    try:
        p1 = await s._get_or_transcode("KEY", "http://plex/x.ogg")
        p2 = await s._get_or_transcode("KEY", "http://plex/x.ogg")
        p3 = await s._get_or_transcode("KEY", "http://plex/x.ogg")
        assert calls["n"] == 1        # transcoded exactly once
        assert p1 == p2 == p3         # same cached file reused for every request
    finally:
        s._TRANSCODE_CACHE.clear()


async def test_get_or_transcode_coalesces_same_key_parallelizes_distinct(tmp_path, monkeypatch):
    """Code-review #2: concurrent requests for the SAME key transcode once
    (coalesced); requests for DIFFERENT keys transcode CONCURRENTLY — the lock no
    longer serializes unrelated tracks behind one slow transcode."""
    import asyncio as aio
    from app.api import stream as s

    s._TRANSCODE_CACHE.clear()
    s._transcode_inflight.clear()
    release = aio.Event()
    inflight = {"n": 0, "peak": 0}
    calls = {"n": 0}

    async def _fake_fetch(plex_url):
        calls["n"] += 1
        inflight["n"] += 1
        inflight["peak"] = max(inflight["peak"], inflight["n"])
        await release.wait()
        inflight["n"] -= 1
        p = tmp_path / f"{calls['n']}.flac"
        p.write_bytes(b"fLaC")
        return str(p)

    monkeypatch.setattr(s, "_fetch_and_transcode", _fake_fetch)
    try:
        a = aio.ensure_future(s._get_or_transcode("K", "u"))
        b = aio.ensure_future(s._get_or_transcode("K", "u"))   # same key → coalesces with a
        await aio.sleep(0.02)
        c = aio.ensure_future(s._get_or_transcode("K2", "u2"))  # different key → parallel
        await aio.sleep(0.02)
        assert inflight["peak"] >= 2     # K and K2 transcoding at the same time
        release.set()
        ra, rb, rc = await aio.gather(a, b, c)
        assert ra == rb                  # same-key callers got the one shared file
        assert calls["n"] == 2           # K once + K2 once (not 3) — a/b coalesced
    finally:
        release.set()
        s._TRANSCODE_CACHE.clear()
        s._transcode_inflight.clear()


async def test_get_or_transcode_evicts_and_unlinks_lru(tmp_path, monkeypatch):
    """The LRU evicts and unlinks old transcodes so temp files don't pile up."""
    from app.api import stream as s

    s._TRANSCODE_CACHE.clear()
    monkeypatch.setattr(s, "_TRANSCODE_CACHE_MAX", 2)
    seq = {"n": 0}

    async def _fake_fetch(plex_url):
        p = tmp_path / f"{seq['n']}.flac"
        seq["n"] += 1
        p.write_bytes(b"fLaC")
        return str(p)

    monkeypatch.setattr(s, "_fetch_and_transcode", _fake_fetch)
    try:
        a = await s._get_or_transcode("A", "u")
        b = await s._get_or_transcode("B", "u")
        await s._get_or_transcode("C", "u")  # over capacity → evicts A (LRU)
        assert not os.path.exists(a)          # A's temp file was unlinked
        assert os.path.exists(b)
    finally:
        for p in list(s._TRANSCODE_CACHE.values()):
            if os.path.exists(p):
                os.unlink(p)
        s._TRANSCODE_CACHE.clear()


def _ffmpeg_tools_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


@pytest.mark.skipif(not _ffmpeg_tools_available(), reason="ffmpeg/ffprobe not installed")
async def test_transcode_to_flac_file_is_seekable_and_16bit(tmp_path):
    """The transcode must write a SEEKABLE file so ffmpeg backfills STREAMINFO
    (finite duration + seektable) — a pipe leaves duration unknown and the FLAC
    unrangeable. Output must be 16-bit (Vorbis floats would otherwise become
    experimental 24-bit FLAC). This is the hardware-independent proof of the
    2026-06-17 Chromecast root cause."""
    from app.api import stream as s

    ogg = tmp_path / "src.ogg"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=2", "-c:a", "libvorbis", str(ogg)],
        check=True,
    )

    out_path = await s._transcode_to_flac_file(_FakeResp(ogg.read_bytes()))
    try:
        duration = subprocess.check_output(
            ["ffprobe", "-hide_banner", "-loglevel", "error",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", out_path],
        ).decode().strip()
        # The pipe path produced "N/A" here; a seekable file yields a real value.
        assert duration not in ("", "N/A")
        assert abs(float(duration) - 2.0) < 0.5

        sample_fmt = subprocess.check_output(
            ["ffprobe", "-hide_banner", "-loglevel", "error",
             "-show_entries", "stream=sample_fmt",
             "-of", "default=noprint_wrappers=1:nokey=1", out_path],
        ).decode().strip()
        assert sample_fmt == "s16"
    finally:
        os.unlink(out_path)


async def test_add_flac_seektable_never_raises(tmp_path):
    """metaflac is best-effort — it must never raise into the request path,
    whether metaflac is absent (host) or the file is unusable (rc!=0)."""
    from app.api import stream as s

    flac = tmp_path / "x.flac"
    flac.write_bytes(b"fLaC" + b"\0" * 64)  # not a real FLAC; metaflac would error
    await s._add_flac_seektable(str(flac))  # no exception regardless of outcome


def _flac_block_types(path: str) -> list[str]:
    names = {0: "STREAMINFO", 1: "PADDING", 2: "APPLICATION", 3: "SEEKTABLE",
             4: "VORBIS_COMMENT", 5: "CUESHEET", 6: "PICTURE"}
    data = open(path, "rb").read()
    assert data[:4] == b"fLaC"
    i, out = 4, []
    while True:
        head = data[i]
        size = int.from_bytes(data[i + 1:i + 4], "big")
        out.append(names.get(head & 0x7f, f"?{head & 0x7f}"))
        i += 4 + size
        if head >> 7:  # last-metadata-block flag
            break
    return out


def _flac_tools_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("metaflac"))


@pytest.mark.skipif(not _flac_tools_available(), reason="ffmpeg/metaflac not installed")
async def test_transcode_adds_seektable(tmp_path):
    """End-to-end: the transcode + metaflac step yields a FLAC WITH a SEEKTABLE,
    so a constrained Cast receiver seeks accurately instead of blind-estimating
    and ERRORing at end-of-track after a seek (2026-06-17)."""
    from app.api import stream as s

    ogg = tmp_path / "src.ogg"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=30", "-c:a", "libvorbis", str(ogg)],
        check=True,
    )
    out_path = await s._transcode_to_flac_file(_FakeResp(ogg.read_bytes()))
    try:
        blocks = _flac_block_types(out_path)
        assert "SEEKTABLE" in blocks, blocks
    finally:
        os.unlink(out_path)
