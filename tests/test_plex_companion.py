"""Tests for the Plex Companion protocol client (2026-08-04-002 plan, U1).

All HTTP is mocked via respx (house style: tests/test_plex_client.py).
Fixture XML shapes mirror the plex-media-player Remote-control-API wiki and
the Caldera forum-thread captures (elan-confirmed createPlayQueue flow).
"""

import httpx
import pytest

from app.plex.companion import (
    CompanionParseError,
    CompanionPlayer,
    CompanionPlayerClient,
    CompanionRequestError,
    CompanionTargetMismatchError,
    CompanionUnreachableError,
    PmsCompanionClient,
    TimelineSnapshot,
    _redact,
    parse_clients,
    parse_timeline,
    server_coordinates,
    server_item_uri,
)

PLAYER = "http://192.168.3.119:32500"
PMS = "http://plex.local:32400"


def make_player(**kwargs) -> CompanionPlayerClient:
    return CompanionPlayerClient(
        kwargs.get("host", "192.168.3.119"),
        kwargs.get("port", 32500),
        target_machine_id=kwargs.get("target_machine_id", "caldera-abc"),
        controller_id=kwargs.get("controller_id", "jukeplox-controller"),
        token=kwargs.get("token", "tok123"),
    )


def make_pms(**kwargs) -> PmsCompanionClient:
    return PmsCompanionClient(
        kwargs.get("server_url", PMS),
        kwargs.get("token", "tok123"),
        kwargs.get("client_id", "jukeplox-controller"),
    )


# ── createPlayQueue: the documented param set ─────────────────────────────────

async def test_create_play_queue_builds_documented_param_set(respx_mock):
    """Happy path: uri in server:// form with the server machineIdentifier,
    type=audio, shuffle/repeat/continuous=0, offset, the OWNING server's
    protocol/address/port, machineIdentifier + source — and NO
    providerIdentifier (the Caldera playMedia failure root cause)."""
    route = respx_mock.get(f"{PLAYER}/player/playback/createPlayQueue").mock(
        return_value=httpx.Response(200, text="OK")
    )
    client = make_player()
    await client.create_play_queue(
        server_machine_id="srv-machine-1",
        key="/library/metadata/458400",
        server_protocol="http",
        server_address="192.168.1.70",
        server_port=32400,
    )
    params = dict(route.calls[0].request.url.params)
    assert params["uri"] == (
        "server://srv-machine-1/com.plexapp.plugins.library"
        "/library/metadata/458400"
    )
    assert params["type"] == "audio"
    assert params["shuffle"] == "0"
    assert params["repeat"] == "0"
    assert params["continuous"] == "0"
    assert params["offset"] == "0"
    assert params["protocol"] == "http"
    assert params["address"] == "192.168.1.70"
    assert params["port"] == "32400"
    assert params["machineIdentifier"] == "srv-machine-1"
    assert params["source"] == "srv-machine-1"
    assert "providerIdentifier" not in params
    assert params["commandID"] == "1"


async def test_commands_carry_required_headers(respx_mock):
    """Every player command carries the controller identity headers: stable
    client id, target machine id, device-name/product (the auth.py identity),
    and the token."""
    route = respx_mock.get(f"{PLAYER}/player/playback/play").mock(
        return_value=httpx.Response(200, text="OK")
    )
    await make_player().play()
    headers = route.calls[0].request.headers
    assert headers["X-Plex-Client-Identifier"] == "jukeplox-controller"
    assert headers["X-Plex-Target-Client-Identifier"] == "caldera-abc"
    assert headers["X-Plex-Token"] == "tok123"
    assert headers["X-Plex-Product"] == "Jukeplox"
    assert headers["X-Plex-Device-Name"] == "Jukeplox"


async def test_command_id_increments_monotonically_per_player(respx_mock):
    """commandID increments on EVERY command including timeline polls (spec);
    two independent clients keep independent counters (per-device session
    structs, R10)."""
    respx_mock.get(f"{PLAYER}/player/playback/play").mock(
        return_value=httpx.Response(200, text="OK")
    )
    respx_mock.get(f"{PLAYER}/player/playback/pause").mock(
        return_value=httpx.Response(200, text="OK")
    )
    poll = respx_mock.get(f"{PLAYER}/player/timeline/poll").mock(
        return_value=httpx.Response(
            200, text='<MediaContainer commandID="3"></MediaContainer>')
    )
    client = make_player()
    await client.play()
    await client.pause()
    await client.poll_timeline()
    ids = [dict(c.request.url.params)["commandID"]
           for c in respx_mock.calls]
    assert ids == ["1", "2", "3"]
    assert client.command_id == 3
    assert dict(poll.calls[0].request.url.params)["commandID"] == "3"
    # A second client starts its own counter at 1.
    other = make_player(target_machine_id="plexamp-xyz")
    await other.play()
    assert other.command_id == 1


async def test_plain_text_ok_body_does_not_raise(respx_mock):
    """Plexamp-family players return 200 with a plain-text 'OK' body, not
    XML — commands must tolerate it (no body parse on command paths)."""
    respx_mock.get(f"{PLAYER}/player/playback/createPlayQueue").mock(
        return_value=httpx.Response(200, text="OK")
    )
    respx_mock.get(f"{PLAYER}/player/playback/stop").mock(
        return_value=httpx.Response(200, text="OK")
    )
    client = make_player()
    await client.create_play_queue(
        server_machine_id="srv1", key="/library/metadata/1",
        server_protocol="http", server_address="10.0.0.1", server_port=32400,
    )
    await client.stop()  # no raise = pass


async def test_transport_controls_carry_music_type_and_args(respx_mock):
    """seekTo carries offset, setParameters carries volume, refreshPlayQueue
    carries playQueueID — all with the mandatory type=music."""
    seek = respx_mock.get(f"{PLAYER}/player/playback/seekTo").mock(
        return_value=httpx.Response(200, text="OK"))
    vol = respx_mock.get(f"{PLAYER}/player/playback/setParameters").mock(
        return_value=httpx.Response(200, text="OK"))
    refresh = respx_mock.get(f"{PLAYER}/player/playback/refreshPlayQueue").mock(
        return_value=httpx.Response(200, text="OK"))
    client = make_player()
    await client.seek_to(42000)
    await client.set_parameters(volume=55)
    await client.refresh_play_queue(101)
    assert dict(seek.calls[0].request.url.params)["offset"] == "42000"
    assert dict(seek.calls[0].request.url.params)["type"] == "music"
    vol_params = dict(vol.calls[0].request.url.params)
    assert vol_params["volume"] == "55"
    assert vol_params["type"] == "music"
    assert "shuffle" not in vol_params and "repeat" not in vol_params
    assert dict(refresh.calls[0].request.url.params)["playQueueID"] == "101"


# ── typed errors ──────────────────────────────────────────────────────────────

async def test_404_surfaces_typed_target_mismatch(respx_mock):
    """Spec: a player MUST 404 when X-Plex-Target-Client-Identifier doesn't
    match — a typed error, distinguishable from generic failures (a stale
    registry address, not a dead device)."""
    respx_mock.get(f"{PLAYER}/player/playback/play").mock(
        return_value=httpx.Response(404)
    )
    with pytest.raises(CompanionTargetMismatchError):
        await make_player().play()


async def test_connect_timeout_is_retryable_unreachable(respx_mock):
    respx_mock.get(f"{PLAYER}/player/playback/play").mock(
        side_effect=httpx.ConnectTimeout("SYN blackholed")
    )
    with pytest.raises(CompanionUnreachableError) as exc_info:
        await make_player().play()
    assert exc_info.value.retryable is True


async def test_connect_error_is_retryable_unreachable(respx_mock):
    respx_mock.get(f"{PLAYER}/player/timeline/poll").mock(
        side_effect=httpx.ConnectError("refused")
    )
    with pytest.raises(CompanionUnreachableError):
        await make_player().poll_timeline()


async def test_other_http_error_is_typed_request_error(respx_mock):
    respx_mock.get(f"{PLAYER}/player/playback/play").mock(
        return_value=httpx.Response(500)
    )
    with pytest.raises(CompanionRequestError) as exc_info:
        await make_player().play()
    assert exc_info.value.status_code == 500
    assert exc_info.value.retryable is False


async def test_transport_failure_logs_redacted(respx_mock, caplog):
    """Failures are logged, and URL query strings (credential-bearing on PMS
    URLs) are scrubbed — mirror of the flow._redact contract."""
    import logging
    respx_mock.get(f"{PMS}/clients").mock(
        side_effect=httpx.ConnectTimeout("dead")
    )
    with caplog.at_level(logging.WARNING, logger="app.plex.companion"):
        with pytest.raises(CompanionUnreachableError):
            await make_pms().get_clients()
    assert caplog.records, "a transport failure must be logged, never silent"


def test_redact_strips_url_query_strings():
    text = "GET http://plex.local:32400/playQueues/9?X-Plex-Token=secret123 failed"
    assert "secret123" not in _redact(text)
    assert "http://plex.local:32400/playQueues/9?<redacted>" in _redact(text)


# ── timeline parsing ──────────────────────────────────────────────────────────

_PLAYING_TIMELINE = """
<MediaContainer commandID="7" location="fullScreenMusic">
  <Timeline type="video" state="stopped"/>
  <Timeline type="music" state="playing" time="42442" duration="235225"
            machineIdentifier="srv-machine-1" address="192.168.1.70"
            port="32400" protocol="http" key="/library/metadata/30"
            ratingKey="30" containerKey="/playQueues/101" volume="75"
            playQueueID="101" playQueueItemID="9001" playQueueVersion="2"
            continuing="1"/>
</MediaContainer>
"""


async def test_poll_timeline_parses_playing_snapshot(respx_mock):
    route = respx_mock.get(f"{PLAYER}/player/timeline/poll").mock(
        return_value=httpx.Response(200, text=_PLAYING_TIMELINE)
    )
    snap = await make_player().poll_timeline(wait=0)
    assert isinstance(snap, TimelineSnapshot)
    assert snap.state == "playing"
    assert snap.time == 42442
    assert snap.duration == 235225
    assert snap.key == "/library/metadata/30"
    assert snap.rating_key == "30"
    assert snap.container_key == "/playQueues/101"
    assert snap.machine_identifier == "srv-machine-1"
    assert snap.address == "192.168.1.70"
    assert snap.port == 32400
    assert snap.volume == 75
    assert snap.play_queue_id == 101
    assert snap.play_queue_item_id == 9001
    assert snap.play_queue_version == 2
    assert snap.command_id == 7  # container echo
    assert snap.continuing is True
    assert dict(route.calls[0].request.url.params)["wait"] == "0"


async def test_poll_timeline_wait_param_forwarded(respx_mock):
    route = respx_mock.get(f"{PLAYER}/player/timeline/poll").mock(
        return_value=httpx.Response(200, text=_PLAYING_TIMELINE)
    )
    await make_player().poll_timeline(wait=1)
    assert dict(route.calls[0].request.url.params)["wait"] == "1"


def test_idle_timeline_parses_to_none_fields():
    """Edge case: a stopped/idle timeline omits playQueue*/time/duration —
    every missing field parses to None, never a raise."""
    snap = parse_timeline(
        '<MediaContainer location="navigation">'
        '<Timeline type="music" state="stopped"/>'
        "</MediaContainer>"
    )
    assert snap.state == "stopped"
    assert snap.time is None
    assert snap.duration is None
    assert snap.key is None
    assert snap.rating_key is None
    assert snap.container_key is None
    assert snap.machine_identifier is None
    assert snap.address is None
    assert snap.port is None
    assert snap.volume is None
    assert snap.play_queue_id is None
    assert snap.play_queue_item_id is None
    assert snap.play_queue_version is None
    assert snap.continuing is False


def test_empty_container_timeline_parses_without_raising():
    snap = parse_timeline('<MediaContainer commandID="2"></MediaContainer>')
    assert snap.state is None
    assert snap.command_id == 2


def test_timeline_without_type_attr_still_selected():
    """Some players omit type on the only timeline they send — fall back to
    the first Timeline element."""
    snap = parse_timeline(
        "<MediaContainer>"
        '<Timeline state="playing" time="1000"/>'
        "</MediaContainer>"
    )
    assert snap.state == "playing"
    assert snap.time == 1000


def test_malformed_timeline_xml_raises_parse_error():
    with pytest.raises(CompanionParseError):
        parse_timeline("this is not xml <at all")


async def test_malformed_poll_body_raises_parse_error(respx_mock):
    respx_mock.get(f"{PLAYER}/player/timeline/poll").mock(
        return_value=httpx.Response(200, text="OK")  # plain text on a PARSED path
    )
    with pytest.raises(CompanionParseError):
        await make_player().poll_timeline()


# ── PMS /clients parsing and capability filtering ─────────────────────────────

_CLIENTS_XML = """
<MediaContainer size="3">
  <Server name="Caldera Living Room" host="192.168.3.119" port="32500"
          machineIdentifier="caldera-abc" product="Caldera"
          protocolVersion="1"
          protocolCapabilities="timeline,playback,playqueues,playqueues-creation"/>
  <Server name="Old TV" host="192.168.3.50" port="32500"
          machineIdentifier="tv-1" product="Plex for Samsung"
          protocolCapabilities="timeline,playback,navigation"/>
  <Server name="Broken Entry" product="Mystery"/>
</MediaContainer>
"""


async def test_get_clients_parses_players_and_capabilities(respx_mock):
    route = respx_mock.get(f"{PMS}/clients").mock(
        return_value=httpx.Response(200, text=_CLIENTS_XML)
    )
    players = await make_pms().get_clients()
    # The incomplete entry (no machineIdentifier/host/port) is skipped.
    assert [p.machine_identifier for p in players] == ["caldera-abc", "tv-1"]
    caldera = players[0]
    assert isinstance(caldera, CompanionPlayer)
    assert caldera.name == "Caldera Living Room"
    assert caldera.address == "192.168.3.119"
    assert caldera.port == 32500
    assert caldera.product == "Caldera"
    assert "playqueues-creation" in caldera.protocol_capabilities
    # PMS request carried token + client identity headers.
    headers = route.calls[0].request.headers
    assert headers["X-Plex-Token"] == "tok123"
    assert headers["X-Plex-Client-Identifier"] == "jukeplox-controller"


async def test_client_without_playqueues_creation_marked_ineligible(respx_mock):
    """Edge case: a /clients entry lacking playqueues-creation in its
    protocolCapabilities is ineligible (v1 gates on the capability rather
    than shipping a legacy playMedia path)."""
    respx_mock.get(f"{PMS}/clients").mock(
        return_value=httpx.Response(200, text=_CLIENTS_XML)
    )
    players = await make_pms().get_clients()
    by_id = {p.machine_identifier: p for p in players}
    assert by_id["caldera-abc"].supports_playqueues_creation is True
    assert by_id["tv-1"].supports_playqueues_creation is False


def test_parse_clients_empty_container():
    assert parse_clients("<MediaContainer size='0'></MediaContainer>") == []


def test_parse_clients_malformed_raises_parse_error():
    with pytest.raises(CompanionParseError):
        parse_clients("not xml")


# ── PMS play-queue window ops ─────────────────────────────────────────────────

_PLAYQUEUE_XML = """
<MediaContainer identifier="com.plexapp.plugins.library" playQueueID="101"
                playQueueVersion="3" playQueueSelectedItemID="9001"
                playQueueSelectedItemOffset="0" playQueueTotalCount="2"
                playQueueSourceURI="server://srv-machine-1/com.plexapp.plugins.library/library/metadata/30">
  <Track ratingKey="30" key="/library/metadata/30" playQueueItemID="9001"
         title="Come Together" duration="259000"/>
  <Track ratingKey="31" key="/library/metadata/31" playQueueItemID="9002"
         title="Something" duration="182000"/>
</MediaContainer>
"""


async def test_get_play_queue_window_params_and_parse(respx_mock):
    route = respx_mock.get(f"{PMS}/playQueues/101").mock(
        return_value=httpx.Response(200, text=_PLAYQUEUE_XML)
    )
    window = await make_pms().get_play_queue(101, window=10)
    params = dict(route.calls[0].request.url.params)
    assert params["window"] == "10"
    assert params["own"] == "0"
    assert params["includeBefore"] == "1"
    assert params["includeAfter"] == "1"
    assert window.play_queue_id == 101
    assert window.version == 3
    assert window.selected_item_id == 9001
    assert window.selected_item_offset == 0
    assert window.total_count == 2
    assert window.source_uri.startswith("server://srv-machine-1/")
    assert [i.play_queue_item_id for i in window.items] == [9001, 9002]
    assert window.items[0].rating_key == "30"
    assert window.items[0].title == "Come Together"
    assert window.items[0].duration_ms == 259000


async def test_append_to_play_queue_put_with_uri_and_next(respx_mock):
    route = respx_mock.put(f"{PMS}/playQueues/101").mock(
        return_value=httpx.Response(200, text=_PLAYQUEUE_XML)
    )
    uri = server_item_uri("srv-machine-1", "/library/metadata/31")
    window = await make_pms().append_to_play_queue(101, uri, play_next=True)
    req = route.calls[0].request
    assert req.method == "PUT"
    params = dict(req.url.params)
    assert params["uri"] == ("server://srv-machine-1/com.plexapp.plugins."
                             "library/library/metadata/31")
    assert params["next"] == "1"
    assert window.version == 3


async def test_append_without_next_omits_the_param(respx_mock):
    route = respx_mock.put(f"{PMS}/playQueues/101").mock(
        return_value=httpx.Response(200, text=_PLAYQUEUE_XML)
    )
    await make_pms().append_to_play_queue(
        101, server_item_uri("u", "/library/metadata/31"))
    assert "next" not in dict(route.calls[0].request.url.params)


async def test_delete_play_queue_item(respx_mock):
    route = respx_mock.delete(f"{PMS}/playQueues/101/items/9002").mock(
        return_value=httpx.Response(200, text=_PLAYQUEUE_XML)
    )
    window = await make_pms().delete_play_queue_item(101, 9002)
    assert route.called
    assert window.play_queue_id == 101


async def test_pms_404_is_request_error_not_target_mismatch(respx_mock):
    """A 404 from the PMS (queue expired) is NOT the player's typed
    target-identifier mismatch — that classification is player-only."""
    respx_mock.get(f"{PMS}/playQueues/999").mock(
        return_value=httpx.Response(404)
    )
    with pytest.raises(CompanionRequestError) as exc_info:
        await make_pms().get_play_queue(999)
    assert exc_info.value.status_code == 404
    assert not isinstance(exc_info.value, CompanionTargetMismatchError)


# ── uri / coordinate helpers ──────────────────────────────────────────────────

def test_server_item_uri_shape():
    assert server_item_uri("m1", "/library/metadata/42") == (
        "server://m1/com.plexapp.plugins.library/library/metadata/42"
    )


def test_server_coordinates_explicit_port():
    assert server_coordinates("http://192.168.1.70:32400") == (
        "http", "192.168.1.70", 32400)


def test_server_coordinates_https_default_port():
    assert server_coordinates("https://x.plex.direct") == (
        "https", "x.plex.direct", 443)


def test_from_plex_client_reuses_coordinates():
    """PmsCompanionClient.from_plex_client lifts server_url/token/client_id
    straight off an app.plex.client.PlexClient — no client.py change needed."""
    from app.plex.client import PlexClient
    pc = PlexClient("http://plex.local:32400/", "tokX", "cidX",
                    machine_id="m1", max_concurrency=2)
    companion = PmsCompanionClient.from_plex_client(pc)
    assert companion.server_url == "http://plex.local:32400"
    assert companion._headers["X-Plex-Token"] == "tokX"
    assert companion._headers["X-Plex-Client-Identifier"] == "cidX"
