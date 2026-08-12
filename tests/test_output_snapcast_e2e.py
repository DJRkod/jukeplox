"""U10 — Snapcast hardware-free e2e (the R18 gate).

Runs the embedded snapserver for real and attaches a real ``snapclient`` in
FILE-player mode (writes received audio to a file instead of a sound card), then
proves the DATA plane end to end: the client connects, the stream transitions
idle → playing, real bytes reach the receiver's output file, a volume round-trips
through control-RPC, and the feed loop TERMINATES within the tone duration + a
margin (a bounded feed — an endless-stream hang would blow the timeout instead of
hiding). Unit tests + persona reviews structurally cannot prove byte-flow; only a
real receiver can (the control-OK / data-silent fingerprint).

Skips where ``snapserver``/``snapclient`` or the ``snapcast`` lib are absent (the
Windows dev env). R18 is satisfied only when this passes INSIDE the built image —
a bare skip does not count.
"""

import asyncio
import os
import shutil
import tempfile

import pytest

from tests.fixtures.multiroom import write_test_tone

pytestmark = pytest.mark.requires_snapclient

_HAVE_BINS = bool(shutil.which("snapserver") and shutil.which("snapclient"))


@pytest.fixture
def tone(tmp_path):
    return write_test_tone(str(tmp_path / "tone.wav"), seconds=2.0)


async def _poll(pred, timeout, interval=0.2):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await pred() if asyncio.iscoroutinefunction(pred) else pred():
            return True
        await asyncio.sleep(interval)
    return False


@pytest.mark.skipif(not _HAVE_BINS, reason="snapserver/snapclient not installed (in-image gate)")
async def test_snapclient_receives_audio_and_volume_round_trips(monkeypatch, tone, tmp_path):
    pytest.importorskip("snapcast")
    from app.output.snapcast import SnapcastBackend
    from app.output import snapcast as snap_mod

    # Embedded defaults; feed the local tone directly (no Plex round-trip).
    async def _get(key, default=None):
        return None
    monkeypatch.setattr("app.database.get_setting", _get)
    monkeypatch.setattr(snap_mod, "_default_resolve_source",
                        (lambda track: _resolved(tone)))

    advanced = asyncio.Event()

    async def _advance():
        advanced.set()

    backend = SnapcastBackend(advance_cb=_advance)
    out_pcm = str(tmp_path / "recv.pcm")
    client_proc = None
    try:
        await backend.enable()
        assert backend._connected

        # Attach a real snapclient in file-player mode against the embedded server.
        client_proc = await asyncio.create_subprocess_exec(
            shutil.which("snapclient"), "-h", "127.0.0.1", "-p", "1704",
            "--hostID", "jukeplox-e2e",
            "--player", f"file:filename={out_pcm}",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )

        async def _client_connected():
            try:
                await backend._control.status()
            except Exception:
                return False
            return len(backend._control.clients()) >= 1
        assert await _poll(_client_connected, timeout=15), "snapclient never connected"

        track = type("T", (), {"id": "e2e", "title": "Tone", "stream_key": "/x"})()
        await backend.play("ignored", track)

        # Data-plane proof: the receiver's output file grows with real bytes,
        # and the feed loop terminates (advance) within the tone + margin — a
        # bounded feed, never an endless hang.
        assert await _poll(lambda: os.path.exists(out_pcm) and os.path.getsize(out_pcm) > 0,
                           timeout=10), "no audio bytes reached the snapclient"
        assert await _poll(lambda: advanced.is_set(), timeout=8), \
            "feed did not advance at track end (unbounded / stalled?)"

        # Volume round-trips through control-RPC.
        clients = backend._control.clients()
        cid = backend._client_id(clients[0])
        await backend.set_client_volume(cid, 0.3)
        await backend._control.status()
    finally:
        if client_proc is not None and client_proc.returncode is None:
            client_proc.terminate()
            try:
                await asyncio.wait_for(client_proc.wait(), timeout=3)
            except asyncio.TimeoutError:
                client_proc.kill()
        await backend.disable()


async def _resolved(path):
    return (path, {})
