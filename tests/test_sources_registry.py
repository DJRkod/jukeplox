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
    return s


async def test_routes_by_source_id_prefix():
    a, b = _fake_source("A"), _fake_source("B")
    reg = SourceRegistry([a, b])
    await reg.get_artists("B:section")
    b.get_artists.assert_awaited_once()
    a.get_artists.assert_not_awaited()
    await reg.get_track("A:42")
    a.get_track.assert_awaited_once()


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
