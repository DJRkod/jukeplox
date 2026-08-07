"""Tests for the cache-aware /api/art handler in app/api/guest.py."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.cache.art_cache import ArtCache


# ── helpers ────────────────────────────────────────────────────────────────────

VALID_PATH = "feedbeef00112233445566778899aabbccddeeff:/library/metadata/53521/thumb/1780564533"


def _mk_plex_client(data: bytes = b"plex-bytes", content_type: str = "image/jpeg") -> MagicMock:
    """Mock PlexClient whose fetch_art returns the given (bytes, content_type)."""
    client = MagicMock()
    client.fetch_art = AsyncMock(return_value=(data, content_type))
    return client


def _replace_cache(monkeypatch, tmp_path: Path, size_mb: int = 1) -> ArtCache:
    """Swap the module-level cache singleton for a fresh one rooted under tmp_path.

    Both the import path inside the guest handler (`from app.cache import cache`)
    and the standalone module attribute need to be patched so the handler sees
    the test instance.
    """
    fresh = ArtCache(data_dir=tmp_path / "art-cache", size_mb=size_mb)
    monkeypatch.setattr("app.cache.cache", fresh, raising=True)
    return fresh


# ── happy path: miss → fetch → cache → headers ────────────────────────────────

async def test_cache_miss_fetches_plex_caches_response_and_sets_headers(tmp_path, monkeypatch):
    """Covers AE1: first request → Plex → cache populated → response carries cache headers."""
    from app.api import guest
    cache = _replace_cache(monkeypatch, tmp_path)
    client = _mk_plex_client(b"first-bytes", "image/jpeg")
    monkeypatch.setattr("app.state.get_plex_client", AsyncMock(return_value=client))

    response = await guest.art_proxy(path=VALID_PATH)

    assert response.body == b"first-bytes"
    assert response.media_type == "image/jpeg"
    assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert response.headers["ETag"].startswith('"') and response.headers["ETag"].endswith('"')
    # Plex was called exactly once (full image — no width requested here)
    client.fetch_art.assert_awaited_once_with(VALID_PATH, width=None)
    # Cache was populated
    hit = await cache.get(VALID_PATH)
    assert hit == (b"first-bytes", "image/jpeg")


# ── resized thumbnails (2026-06-25 deep-jump reveal fix) ──────────────────────

async def test_width_request_resizes_and_caches_under_width_key(tmp_path, monkeypatch):
    """A `w` query → fetch_art(width=w); the resized variant caches under a
    width-suffixed key so it never collides with the full image, and its ETag
    differs from the full image's."""
    from app.api import guest
    from app.api.guest import _art_etag
    cache = _replace_cache(monkeypatch, tmp_path)
    client = _mk_plex_client(b"small-bytes", "image/jpeg")
    monkeypatch.setattr("app.state.get_plex_client", AsyncMock(return_value=client))

    response = await guest.art_proxy(path=VALID_PATH, w=144)

    assert response.body == b"small-bytes"
    client.fetch_art.assert_awaited_once_with(VALID_PATH, width=144)
    # resized variant cached under the width-suffixed key, NOT the bare path
    assert await cache.get(f"{VALID_PATH}|w144") == (b"small-bytes", "image/jpeg")
    assert await cache.get(VALID_PATH) is None
    assert response.headers["ETag"] == _art_etag(f"{VALID_PATH}|w144")
    assert _art_etag(f"{VALID_PATH}|w144") != _art_etag(VALID_PATH)


async def test_out_of_range_width_falls_back_to_full(tmp_path, monkeypatch):
    """An absurd width is clamped to None → full image fetched + cached under the
    bare path key."""
    from app.api import guest
    cache = _replace_cache(monkeypatch, tmp_path)
    client = _mk_plex_client(b"full-bytes", "image/jpeg")
    monkeypatch.setattr("app.state.get_plex_client", AsyncMock(return_value=client))

    await guest.art_proxy(path=VALID_PATH, w=999999)

    client.fetch_art.assert_awaited_once_with(VALID_PATH, width=None)
    assert await cache.get(VALID_PATH) == (b"full-bytes", "image/jpeg")


# ── cache hit avoids Plex call ────────────────────────────────────────────────

async def test_second_request_hits_cache_and_does_not_call_plex(tmp_path, monkeypatch):
    """Covers AE2: with a warm cache, Plex is not called again for the same path."""
    from app.api import guest
    cache = _replace_cache(monkeypatch, tmp_path)
    # Warm the cache directly
    await cache.put(VALID_PATH, b"cached-bytes", "image/png")
    client = _mk_plex_client(b"should-not-see-this", "image/jpeg")
    monkeypatch.setattr("app.state.get_plex_client", AsyncMock(return_value=client))

    response = await guest.art_proxy(path=VALID_PATH)

    assert response.body == b"cached-bytes"
    assert response.media_type == "image/png"
    assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    client.fetch_art.assert_not_awaited()


# ── Plex error paths ──────────────────────────────────────────────────────────

async def test_plex_error_on_cache_miss_returns_404(tmp_path, monkeypatch):
    """Existing behavior preserved when there's no cached entry to fall back to."""
    from fastapi import HTTPException
    from app.api import guest
    _replace_cache(monkeypatch, tmp_path)
    client = MagicMock()
    client.fetch_art = AsyncMock(side_effect=RuntimeError("plex unreachable"))
    monkeypatch.setattr("app.state.get_plex_client", AsyncMock(return_value=client))

    with pytest.raises(HTTPException) as exc_info:
        await guest.art_proxy(path=VALID_PATH)
    assert exc_info.value.status_code == 404


async def test_plex_error_on_cached_path_serves_stale_with_warning(tmp_path, monkeypatch, caplog):
    """Covers AE4: Plex down + path already cached → serve cached bytes + WARNING log."""
    import logging
    from app.api import guest
    cache = _replace_cache(monkeypatch, tmp_path)
    # Warm the cache, then make Plex unreachable.
    await cache.put(VALID_PATH, b"warm-bytes", "image/jpeg")
    client = MagicMock()
    client.fetch_art = AsyncMock(side_effect=ConnectionError("plex unreachable"))
    monkeypatch.setattr("app.state.get_plex_client", AsyncMock(return_value=client))

    # Bypass the cache hit on the first read so we exercise the fallback path
    # rather than the fast hit-before-fetch branch. We do this by patching
    # cache.get to return None on the first call (simulating a race where
    # eviction occurred between miss and fetch) and the real value on the
    # second call (the post-failure recheck).
    real_get = cache.get
    calls = {"n": 0}

    async def get_with_miss_then_hit(path):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return await real_get(path)

    monkeypatch.setattr(cache, "get", get_with_miss_then_hit)

    with caplog.at_level(logging.WARNING, logger="app.api.guest"):
        response = await guest.art_proxy(path=VALID_PATH)

    assert response.body == b"warm-bytes"
    assert response.media_type == "image/jpeg"
    assert any("Plex unreachable" in rec.message for rec in caplog.records)
    assert any("ConnectionError" in rec.message for rec in caplog.records)


# ── cache write failure is non-fatal ──────────────────────────────────────────

async def test_cache_write_failure_does_not_break_response(tmp_path, monkeypatch):
    """KTD5: a cache.put failure must not surface as a 5xx — the Plex bytes
    still flow through to the client."""
    from app.api import guest
    cache = _replace_cache(monkeypatch, tmp_path)
    # Force cache.put to raise.
    cache.put = AsyncMock(side_effect=OSError("disk full"))
    client = _mk_plex_client(b"fresh-bytes", "image/jpeg")
    monkeypatch.setattr("app.state.get_plex_client", AsyncMock(return_value=client))

    response = await guest.art_proxy(path=VALID_PATH)

    assert response.body == b"fresh-bytes"
    assert response.status_code == 200
    client.fetch_art.assert_awaited_once()


# ── ETag derivation ───────────────────────────────────────────────────────────

def test_etag_is_stable_per_path():
    """Same input → same ETag, deterministic."""
    from app.api.guest import _art_etag
    assert _art_etag(VALID_PATH) == _art_etag(VALID_PATH)


def test_etag_differs_per_path():
    """Different paths produce different ETags (hash collision is astronomically
    unlikely for the 16-hex-char prefix at expected cache sizes)."""
    from app.api.guest import _art_etag
    other = "feedbeef00112233445566778899aabbccddeeff:/library/metadata/99999/thumb/1234567890"
    assert _art_etag(VALID_PATH) != _art_etag(other)


def test_etag_is_quoted_strong_validator():
    """Per RFC 7232: strong ETags are wrapped in straight double quotes."""
    from app.api.guest import _art_etag
    etag = _art_etag(VALID_PATH)
    assert etag.startswith('"')
    assert etag.endswith('"')
    # Not weak (no W/ prefix)
    assert not etag.startswith('W/')


# ── invalid path is rejected before cache lookup ──────────────────────────────

async def test_invalid_path_returns_400_without_cache_lookup(tmp_path, monkeypatch):
    from fastapi import HTTPException
    from app.api import guest
    cache = _replace_cache(monkeypatch, tmp_path)
    cache.get = AsyncMock(return_value=None)  # would record a call if reached
    monkeypatch.setattr("app.state.get_plex_client", AsyncMock(return_value=_mk_plex_client()))

    with pytest.raises(HTTPException) as exc_info:
        await guest.art_proxy(path="not-a-valid-plex-path")  # missing colon
    assert exc_info.value.status_code == 400
    cache.get.assert_not_awaited()


# ── U12: non-Plex (local / Jellyfin) art bypasses the Plex part-path allowlist ──

def test_valid_art_path_nonplex_prefix_bypasses_allowlist():
    """A non-Plex source key (Jellyfin item image / local relpath) is allowed when
    its prefix is a known non-Plex source — the provider enforces art access
    itself. Without the allowlist (Plex-only install) the same keys are rejected,
    proving the bypass is scoped to connected non-Plex sources."""
    from app.api.guest import _valid_art_path
    # local relpath + Jellyfin "Items/…" both fail the Plex /library//photo/ check
    assert not _valid_art_path("loc:A/Alb/cover.jpg")
    assert not _valid_art_path("jf-srv1:Items/abc/Images/Primary")
    # but pass once their source id is in the non-Plex allowlist
    assert _valid_art_path("loc:A/Alb/cover.jpg", allow_prefixes={"loc"})
    assert _valid_art_path("jf-srv1:Items/abc/Images/Primary", allow_prefixes={"jf-srv1"})


def test_valid_art_path_plex_allowlist_still_enforced():
    """The Plex part-path allowlist is unchanged for Plex keys even with a
    non-Plex source connected (a different prefix in the allowlist does not open
    Plex traversal)."""
    from app.api.guest import _valid_art_path
    assert _valid_art_path("machineabc:/library/metadata/1/thumb/2", allow_prefixes={"loc"})
    assert not _valid_art_path("machineabc:/etc/passwd", allow_prefixes={"loc"})


def test_nonplex_source_ids_safe_for_mock_client():
    """A unit-test MagicMock client (no real .sources list) yields an empty set,
    so Plex validation is unchanged in those tests."""
    from unittest.mock import MagicMock
    from app.api.guest import _nonplex_source_ids
    assert _nonplex_source_ids(MagicMock()) == set()


async def test_local_art_served_via_provider(tmp_path, monkeypatch):
    """A local art key bypasses the Plex allowlist and is served by LocalSource's
    contained fetch_art (covers the proxy delivery half of U12)."""
    from app.api import guest
    from app.sources.local import LocalSource
    from app.sources.registry import SourceRegistry
    _replace_cache(monkeypatch, tmp_path)
    (tmp_path / "music" / "A" / "Alb").mkdir(parents=True)
    (tmp_path / "music" / "A" / "Alb" / "cover.jpg").write_bytes(b"JPEGDATA")
    reg = SourceRegistry([LocalSource(root_dir=str(tmp_path / "music"), source_id="loc")])
    monkeypatch.setattr("app.state.get_plex_client", AsyncMock(return_value=reg))

    resp = await guest.art_proxy(path="loc:A/Alb/cover.jpg")
    assert resp.body == b"JPEGDATA"
    assert resp.media_type == "image/jpeg"


async def test_local_art_traversal_rejected(tmp_path, monkeypatch):
    """A traversal art key passes the (bypassed) allowlist but is rejected by the
    provider's realpath containment → 404, with no file read outside the root."""
    from fastapi import HTTPException
    from app.api import guest
    from app.sources.local import LocalSource
    from app.sources.registry import SourceRegistry
    _replace_cache(monkeypatch, tmp_path)
    (tmp_path / "music").mkdir()
    (tmp_path / "secret.txt").write_bytes(b"TOPSECRET")
    reg = SourceRegistry([LocalSource(root_dir=str(tmp_path / "music"), source_id="loc")])
    monkeypatch.setattr("app.state.get_plex_client", AsyncMock(return_value=reg))

    with pytest.raises(HTTPException) as exc_info:
        await guest.art_proxy(path="loc:../secret.txt")
    assert exc_info.value.status_code == 404
