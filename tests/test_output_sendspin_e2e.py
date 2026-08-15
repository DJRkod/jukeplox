"""Sendspin in-image end-to-end gate.

Runs a real ``SendspinServer`` (through the backend) and a real
``SendspinClient`` in one process: connect, receive audio, receive now-playing,
receive a per-speaker volume command, and advance at track end within a bounded
time.

**What this proves and what it does not.** Two deliberate limits:

1. Both ends are the same ``aiosendspin`` build. This proves our WIRING — that
   jukeplox drives the library correctly — and cannot prove interoperability. A
   bug or divergent assumption inside the library appears identically on both
   sides and cancels itself out. Interop is proved only by a receiver built from
   a different implementation of the spec: the hardware gate in the rig
   checklist, not this file.
2. The test client connects with UNPAIRED ACCESS rather than completing a
   cryptographic pairing handshake. The pairing SURFACE — method mapping, code
   validation, bounded attempts, unpair, sealed persistence — is covered by the
   unit tests; the handshake itself is exercised by a real speaker at the
   hardware gate. Do not read a green run here as "pairing works".

Skips where ``aiosendspin`` is absent (the local 3.11 dev interpreter cannot
install it — the library needs 3.12). Inside the image it must never skip.
"""

import asyncio

import pytest

from tests.fixtures.multiroom import write_test_tone

aiosendspin = pytest.importorskip(
    "aiosendspin", reason="aiosendspin only present in the built image")


@pytest.fixture
def tone(tmp_path):
    return write_test_tone(str(tmp_path / "tone.wav"), seconds=1.0)


@pytest.fixture
async def db(tmp_path, monkeypatch):
    """Enabling the backend now loads the sealed pairing store, so the settings
    table has to exist. Always closed: aiosqlite's worker thread is
    non-daemon and would hang the process at exit."""
    from app import database
    from app.config import Settings
    monkeypatch.setattr(database, "settings",
                        Settings(data_dir=tmp_path, secret_key="test"))
    await database.init_db()
    try:
        yield
    finally:
        await database.close_db()


async def _resolved(_track):
    return (_TONE_PATH[0], {})


_TONE_PATH: list[str] = [""]


async def _make_client(name="e2e-speaker"):
    """A real player + metadata client, paired by token."""
    from aiosendspin.client.client import (
        ClientHelloPlayerSupport, PlayerCommand, Roles, SendspinClient,
        SupportedAudioFormat)
    from aiosendspin.client.models import AudioCodec
    from aiosendspin.noise.keys import Identity
    from aiosendspin.noise.trust_store import (
        ClientPairingConfig, InMemoryClientPairingStore, generate_psk)

    psk = generate_psk()
    store = InMemoryClientPairingStore()
    await store.store_pairing_config(ClientPairingConfig(
        pairing_psk_enabled=True, record_mode_psk_id=True,
        unpaired_access_enabled=True))
    await store.set_pairing_psk(psk)
    identity = Identity.generate()
    client = SendspinClient(
        identity, name,
        # Player + metadata is what this gate proves. Artwork behaviour (the
        # clear-on-no-art transition, the off-playback-path fetch) is pinned by
        # the local unit tests and would only add a support block here.
        [Roles.PLAYER, Roles.METADATA],
        pairing_store=store,
        player_support=ClientHelloPlayerSupport(
            supported_formats=[SupportedAudioFormat(
                codec=AudioCodec.PCM, channels=2, sample_rate=48000,
                bit_depth=16)],
            buffer_capacity=2_000_000,
            # Only volume and mute are valid in the hello; delay is negotiated
            # later, not advertised up front.
            supported_commands=[PlayerCommand.VOLUME, PlayerCommand.MUTE],
        ),
    )
    return client, identity.peer_id, psk


async def test_connected_speaker_receives_audio_metadata_and_advances(
        monkeypatch, tone, db):
    from app.output.sendspin import SendspinBackend
    from app.output import sendspin as ss_mod
    from app.output import sendspin_adapter as ssa
    from app.output.sendspin_adapter import DEFAULT_PORT

    _TONE_PATH[0] = tone
    monkeypatch.setattr(ss_mod, "_default_resolve_source", _resolved)
    # No art fetch in-image — the point here is the audio and metadata path.
    monkeypatch.setattr(ss_mod, "_ART_WIDTH", 64)

    advanced = asyncio.Event()

    async def _advance():
        advanced.set()

    backend = SendspinBackend(advance_cb=_advance,
                              host_resolver=lambda: "127.0.0.1",
                              port=DEFAULT_PORT)
    client = None
    chunks: list[bytes] = []
    metadata: list[object] = []
    commands: list[object] = []
    try:
        await backend.enable()
        assert backend._connected

        client, client_id, psk = await _make_client()
        client.add_audio_chunk_listener(lambda *a: chunks.append(a[0] if a else b""))
        client.add_metadata_listener(lambda *a: metadata.append(a[0] if a else None))
        client.add_server_command_listener(lambda *a: commands.append(a))

        # The speaker connects FIRST — the pairing handshake runs over a live
        # connection — and only then does the operator enter its code. An
        # unknown speaker dialling IN is refused outright, so it has to be
        # allowed through by name first. (A speaker that advertises itself is
        # onboarded the other way round: jukeplox dials out with the attempt
        # attached, which is what begin_pairing does when it has a URL.)
        monkeypatch.setattr(ssa, "ALLOW_UNPAIRED_FOR_TESTS", True)
        await backend._adapter.allow_unpaired(client_id)
        client.open_pairing_window()
        await client.connect(
            f"ws://127.0.0.1:{DEFAULT_PORT}{backend._adapter.api_path}")
        assert await _wait(
            lambda: any(c["connected"]
                        for c in backend._adapter.discovered_clients()), 10), \
            "the speaker never connected"

        assert await _wait(lambda: len(backend._adapter.clients()) >= 1, 10), \
            "the speaker never appeared on the server"

        track = type("T", (), {
            "id": "e2e", "title": "Tone", "artist": "Test", "album": "Fixtures",
            "album_artist": "Test", "duration_ms": 1000, "thumb": None,
            "stream_key": "/x",
        })()
        await backend.play("ignored", track)

        assert await _wait(lambda: chunks, 10), "no audio reached the speaker"
        assert await _wait(lambda: metadata, 10), "no now-playing reached the speaker"

        # A per-speaker volume write REACHES the speaker. Asserted at the
        # receiving end rather than by reading the server's own state back,
        # which would only prove the server talked to itself.
        cid = backend._adapter.clients()[0]["id"]
        await backend.set_client_volume(cid, 0.25)
        assert await _wait(lambda: commands, 5), \
            "the per-speaker volume write never reached the speaker"

        # BOUNDED: the push loop terminates at track end rather than hanging.
        assert advanced.is_set() or await _wait(lambda: advanced.is_set(), 10), \
            "feed did not advance at track end — unbounded or hung push loop"
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass
        await backend.disable()


async def test_pairing_records_survive_a_restart(tmp_path, monkeypatch):
    """The pairing record is sealed into the settings table, so a fresh server
    must still recognise a speaker paired before the restart."""
    from app import database
    from app.config import Settings
    from app.output import sendspin_adapter as ssa

    monkeypatch.setattr(database, "settings",
                        Settings(data_dir=tmp_path, secret_key="test"))
    await database.init_db()
    try:
        payload = {"records": {"abc": {"client_id": "abc"}},
                   "staged_pairing_psks": {}, "trusted_unpaired_clients": {}}
        await ssa.save_sealed_pairing_blob(payload)
        # A brand-new read, as a restarted process would do.
        assert await ssa.load_sealed_pairing_blob() == payload
        raw = await database.get_setting(ssa.PAIRING_STORE_SETTING_KEY)
        assert raw.startswith("enc:fernet:")
    finally:
        await database.close_db()


async def _wait(pred, timeout, interval=0.1):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pred():
            return True
        await asyncio.sleep(interval)
    return False
