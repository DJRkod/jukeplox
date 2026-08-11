"""U3: SourceRegistry routes by source_id prefix, aggregates libraries across
sources, fans out cache invalidation, and survives a failing source — mirroring
the MultiPlexClient contract it replaces, but over MusicSource providers."""

from unittest.mock import AsyncMock, MagicMock

from app.models import Library
from app.sources.base import StreamTarget
from app.sources.registry import SourceRegistry


def _fake_source(source_id, lib_title="L"):
    s = MagicMock()
    s.source_id = source_id
    s.get_libraries = AsyncMock(return_value=[Library(key=f"{source_id}:1", title=lib_title, type="artist")])
    s.get_artists = AsyncMock(return_value=[f"{source_id}-artist"])
    s.get_track = AsyncMock(return_value=f"{source_id}-track")
    s.get_genres = AsyncMock(return_value=[f"{source_id}-genre"])
    s.fetch_art = AsyncMock(return_value=(b"", "image/jpeg"))
    s.resolve_stream = MagicMock(return_value=StreamTarget(url=f"http://{source_id}/stream"))
    s.invalidate_cache = MagicMock()
    # U5: default header-auth (no credential in URL). A MagicMock would otherwise
    # auto-create a truthy url_borne_auth, wrongly flagging every fake as URL-auth.
    s.url_borne_auth = False
    return s


async def test_routes_by_source_id_prefix():
    a, b = _fake_source("A"), _fake_source("B")
    reg = SourceRegistry([a, b])
    await reg.get_artists("B:section")
    b.get_artists.assert_awaited_once()
    a.get_artists.assert_not_awaited()
    await reg.get_track("A:42")
    a.get_track.assert_awaited_once()


async def test_get_album_track_counts_routes_to_owning_source():
    # Regression (ce-debug 2026-08-10): _refresh_browse_index calls
    # registry.get_album_track_counts to derive album lengths (Plex's newer agent
    # drops leafCount). The registry MUST expose it and route to the owning source
    # — it was added only to the legacy MultiPlexClient at first, so the real
    # SourceRegistry raised AttributeError mid-refresh and failed every scan.
    a, b = _fake_source("A"), _fake_source("B")
    a.get_album_track_counts = AsyncMock(return_value={"A:10": 11})
    b.get_album_track_counts = AsyncMock(return_value={})
    reg = SourceRegistry([a, b])
    assert await reg.get_album_track_counts("A:section") == {"A:10": 11}
    a.get_album_track_counts.assert_awaited_once()
    b.get_album_track_counts.assert_not_awaited()


async def test_unknown_or_prefixless_key_falls_back_to_first_source():
    a, b = _fake_source("A"), _fake_source("B")
    reg = SourceRegistry([a, b])
    await reg.get_artists("ZZ:section")   # unknown id
    a.get_artists.assert_awaited_once()
    await reg.get_track("bareid")          # no prefix
    a.get_track.assert_awaited_once()


async def test_get_libraries_aggregates_across_all_sources():
    a, b = _fake_source("A"), _fake_source("B")
    libs = await SourceRegistry([a, b]).get_libraries()
    assert {l.key for l in libs} == {"A:1", "B:1"}


async def test_get_libraries_stamps_source_type():
    # The admin Libraries list needs each library tagged with its owning source's
    # type so a Jellyfin "Music" and a Plex "Music" are distinguishable (ce-debug
    # 2026-06-29). The registry is the only point that knows the mapping.
    a, b = _fake_source("A"), _fake_source("B")
    a.source_type = "plex"
    b.source_type = "jellyfin"
    libs = await SourceRegistry([a, b]).get_libraries()
    assert {l.key: l.source_type for l in libs} == {"A:1": "plex", "B:1": "jellyfin"}


async def test_failing_source_does_not_break_library_aggregation():
    a = _fake_source("A")
    bad = _fake_source("B")
    bad.get_libraries = AsyncMock(side_effect=RuntimeError("down"))
    libs = await SourceRegistry([a, bad]).get_libraries()
    assert [l.key for l in libs] == ["A:1"]


def test_stream_url_returns_routed_source_url():
    a, b = _fake_source("A"), _fake_source("B")
    reg = SourceRegistry([a, b])
    assert reg.stream_url("B:part") == "http://B/stream"
    b.resolve_stream.assert_called_once_with("B:part")


# ── U5: url_borne_auth routing + stream_url() refuses URL-auth sources ─────────

def test_url_borne_auth_for_reads_owning_source_flag():
    a, b = _fake_source("A"), _fake_source("B")
    b.url_borne_auth = True          # B is a URL-auth (Subsonic-shaped) source
    reg = SourceRegistry([a, b])
    assert reg.url_borne_auth_for("B:part") is True
    assert reg.url_borne_auth_for("A:part") is False


def test_url_borne_auth_for_defaults_false_and_handles_fallback():
    # Prefixless / unknown-id keys route to the first source; its flag is read.
    a = _fake_source("A")
    reg = SourceRegistry([a])
    assert reg.url_borne_auth_for("bareid") is False
    assert reg.url_borne_auth_for("ZZ:unknown") is False


def test_stream_url_refuses_url_auth_source_p0():
    # P0: a raw credentialed upstream URL must never escape via stream_url() for a
    # URL-auth source — it returns "" so _make_stream_url force-proxies instead.
    a, b = _fake_source("A"), _fake_source("B")
    b.url_borne_auth = True
    b.resolve_stream = MagicMock(
        return_value=StreamTarget(url="http://B/rest/stream.view?id=x&apiKey=LEAKED"))
    reg = SourceRegistry([a, b])
    assert reg.stream_url("B:part") == ""
    # Header-auth source is unaffected — still returns its raw URL.
    assert reg.stream_url("A:part") == "http://A/stream"


def test_noop_source_has_url_borne_auth_false():
    from app.sources.registry import _NoopSource
    assert _NoopSource().url_borne_auth is False


def test_invalidate_cache_fans_out():
    a, b = _fake_source("A"), _fake_source("B")
    SourceRegistry([a, b]).invalidate_cache()
    a.invalidate_cache.assert_called_once()
    b.invalidate_cache.assert_called_once()


async def test_enrichment_routes_by_prefix():
    a, b = _fake_source("A"), _fake_source("B")
    reg = SourceRegistry([a, b])
    assert await reg.get_genres("A:1") == ["A-genre"]


def test_resolve_stream_routes_to_owning_provider():
    a, b = _fake_source("A"), _fake_source("B")
    reg = SourceRegistry([a, b])
    target = reg.resolve_stream("B:part")
    assert target.url == "http://B/stream"
    b.resolve_stream.assert_called_once_with("B:part")
