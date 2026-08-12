"""Tests for the admin API routes.

Shared fixtures (mock_session, mock_state, client, anon_client) and the
make_track/make_album/make_artist factories live in tests/conftest.py.
Transport-endpoint tests for POST /admin/playback/previous live in
tests/test_api_playback.py.
"""

import contextlib

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.output import hold  # the hold flag's home since the session decomposition
from app.plex.models import Library, Track, Album, Artist
from app.queue.models import QueueEndBehavior

from tests.conftest import make_track, make_album, make_artist


# ── Auth guard ────────────────────────────────────────────────────────────────

def test_queue_requires_auth(anon_client, mock_session):
    resp = anon_client.get("/admin/queue")
    assert resp.status_code == 401


def test_settings_requires_auth(anon_client, mock_session):
    resp = anon_client.get("/admin/settings")
    assert resp.status_code == 401


def test_playback_pause_requires_auth(anon_client, mock_session):
    resp = anon_client.post("/admin/playback/pause")
    assert resp.status_code == 401


# ── Multi-source Sources panel (U14) ──────────────────────────────────────────

def test_sources_endpoints_require_auth(anon_client, mock_session):
    # Security (R26): every source op rejects an unauthenticated request.
    assert anon_client.get("/admin/sources").status_code == 401
    assert anon_client.post("/admin/sources/jellyfin", json={}).status_code == 401
    assert anon_client.delete("/admin/sources/jellyfin/x").status_code == 401
    assert anon_client.post("/admin/sources/local", json={}).status_code == 401
    assert anon_client.delete("/admin/sources/local/x").status_code == 401
    assert anon_client.post("/admin/sources/rescan").status_code == 401
    assert anon_client.get("/admin/scan-status").status_code == 401  # U15 (R26)


def test_scan_status_returns_snapshot(client, mock_state):
    # U15: the admin scan badge reads the shared scan_status snapshot.
    snap = {"sources": 1, "scanning": True, "scanned": False, "empty": True}
    with patch("app.state.scan_status", AsyncMock(return_value=snap)):
        resp = client.get("/admin/scan-status")
    assert resp.status_code == 200
    assert resp.json() == snap


def test_list_sources_combines_plex_and_jellyfin(client, mock_state):
    with patch("app.api.admin.database.get_plex_servers",
               AsyncMock(return_value=[{"machine_id": "m1", "name": "Home Plex"}])), \
         patch("app.api.admin.database.get_jellyfin_sources",
               AsyncMock(return_value=[{"source_id": "jf-1", "name": "Den Jelly"}])), \
         patch("app.api.admin.database.get_subsonic_sources", AsyncMock(return_value=[])), \
         patch("app.api.admin.database.get_emby_sources", AsyncMock(return_value=[])), \
         patch("app.api.admin.database.get_disabled_sources", AsyncMock(return_value=[])), \
         patch("app.api.admin.database.get_local_sources", AsyncMock(return_value=[])):
        resp = client.get("/admin/sources")
    assert resp.status_code == 200
    srcs = resp.json()["sources"]
    assert {"source_id": "m1", "type": "plex", "name": "Home Plex", "enabled": True} in srcs
    assert {"source_id": "jf-1", "type": "jellyfin", "name": "Den Jelly", "enabled": True} in srcs


def test_connect_jellyfin_success_saves_token_only_and_scans(client, mock_state):
    # Covers AE5: connect via the form → validated, saved, scan starts.
    save = AsyncMock()
    with patch("app.sources.jellyfin.authenticate",
               AsyncMock(return_value={"token": "tok", "user_id": "u1",
                                       "server_id": "srv9"})), \
         patch("app.api.admin.database.save_jellyfin_source", save), \
         patch("app.state.trigger_catalog_refresh", MagicMock()) as scan, \
         patch("app.state.invalidate_plex_client", MagicMock()):
        resp = client.post("/admin/sources/jellyfin", json={
            "server_url": "http://jf.local:8096", "username": "admin",
            "password": "secret", "name": "Den"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["source_id"] == "jf-srv9" and body["name"] == "Den"
    save.assert_awaited_once()
    kwargs = save.call_args.kwargs
    assert kwargs["token"] == "tok" and kwargs["user_id"] == "u1"
    assert not ({"password", "pw"} & set(kwargs))  # token-only persisted (R24)
    scan.assert_called_once()


def test_connect_jellyfin_enables_its_library(client, mock_state):
    """Regression (ce-debug 2026-07-24): the Jellyfin connect path must also enable
    its libraries — same fix as connect_local, and the path where get_libraries()
    actually does network I/O. Symmetry with connect_local is easy to break."""
    from app.models import Library
    sid = "jf-srv9"

    class _FakeSrc:
        source_id = sid
        async def get_libraries(self):
            return [Library(key=f"{sid}:music1", title="Music", type="artist", server_name="Den")]

    class _Reg:
        sources = [_FakeSrc()]

    toggle = AsyncMock()
    with patch("app.sources.jellyfin.authenticate",
               AsyncMock(return_value={"token": "tok", "user_id": "u1", "server_id": "srv9"})), \
         patch("app.api.admin.database.save_jellyfin_source", AsyncMock()), \
         patch("app.state.get_plex_client", AsyncMock(return_value=_Reg())), \
         patch("app.database.toggle_library", toggle), \
         patch("app.state.trigger_catalog_refresh", MagicMock()), \
         patch("app.state.invalidate_plex_client", MagicMock()):
        resp = client.post("/admin/sources/jellyfin", json={
            "server_url": "http://jf.local:8096", "username": "admin",
            "password": "secret", "name": "Den"})
    assert resp.status_code == 200
    toggle.assert_awaited_once_with(f"{sid}:music1", "Music", enabled=True)


def test_connect_jellyfin_enable_failure_does_not_500(client, mock_state):
    """A post-auth get_libraries() failure during enable must NOT fail the connect:
    the source is already saved, so connect still returns 200 and the catalog
    refresh still fires; the enable is best-effort (ce-code-review 2026-07-24, #1)."""
    class _FakeSrc:
        source_id = "jf-srv9"
        async def get_libraries(self):
            raise RuntimeError("jellyfin Views fetch failed")

    class _Reg:
        sources = [_FakeSrc()]

    with patch("app.sources.jellyfin.authenticate",
               AsyncMock(return_value={"token": "tok", "user_id": "u1", "server_id": "srv9"})), \
         patch("app.api.admin.database.save_jellyfin_source", AsyncMock()), \
         patch("app.state.get_plex_client", AsyncMock(return_value=_Reg())), \
         patch("app.database.toggle_library", AsyncMock()), \
         patch("app.state.trigger_catalog_refresh", MagicMock()) as scan, \
         patch("app.state.invalidate_plex_client", MagicMock()):
        resp = client.post("/admin/sources/jellyfin", json={
            "server_url": "http://jf.local:8096", "username": "admin",
            "password": "x", "name": "Den"})
    assert resp.status_code == 200          # enable failure did not 500 the connect
    scan.assert_called_once()               # and trigger_catalog_refresh still fired


def test_connect_jellyfin_bad_credentials_not_saved(client, mock_state):
    # Error path (R21): auth rejected → categorized inline, source not saved.
    from app.sources.jellyfin import JellyfinAuthError
    save = AsyncMock()
    with patch("app.sources.jellyfin.authenticate",
               AsyncMock(side_effect=JellyfinAuthError("nope"))), \
         patch("app.api.admin.database.save_jellyfin_source", save):
        resp = client.post("/admin/sources/jellyfin", json={
            "server_url": "http://jf.local:8096", "username": "admin",
            "password": "bad"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["category"] == "auth_rejected"
    save.assert_not_awaited()


def test_connect_jellyfin_unreachable_not_saved(client, mock_state):
    # Error path (R21): unreachable server → categorized inline, source not saved.
    import httpx
    save = AsyncMock()
    with patch("app.sources.jellyfin.authenticate",
               AsyncMock(side_effect=httpx.ConnectError("boom"))), \
         patch("app.api.admin.database.save_jellyfin_source", save):
        resp = client.post("/admin/sources/jellyfin", json={
            "server_url": "http://nope.local:8096", "username": "admin",
            "password": "x"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["category"] == "unreachable"
    save.assert_not_awaited()


def test_remove_jellyfin_source(client, mock_state):
    dele = AsyncMock()
    with patch("app.api.admin.database.delete_jellyfin_source", dele), \
         patch("app.state.trigger_catalog_refresh", MagicMock()), \
         patch("app.state.invalidate_plex_client", MagicMock()):
        resp = client.delete("/admin/sources/jellyfin/jf-1")
    assert resp.status_code == 200
    dele.assert_awaited_once_with("jf-1")


# ── Subsonic / Emby connect (2026-08-10-003 U4) ───────────────────────────────

def _no_dupes():
    """Patch context: no existing sources so _reject_duplicate_source is inert."""
    return [
        patch("app.api.admin.database.get_plex_servers", AsyncMock(return_value=[])),
        patch("app.api.admin.database.get_jellyfin_sources", AsyncMock(return_value=[])),
        patch("app.api.admin.database.get_subsonic_sources", AsyncMock(return_value=[])),
        patch("app.api.admin.database.get_emby_sources", AsyncMock(return_value=[])),
    ]


def _lib(sid, type_="artist"):
    from app.models import Library
    return Library(key=f"{sid}:root", title="Music", type=type_, server_name="")


def test_connect_subsonic_success_saves_credential_free_url_and_scans(client, mock_state):
    # Covers AE1: valid key → saved (credential-free URL), enable runs between
    # invalidate and refresh, refresh triggered.
    save = AsyncMock()
    order = []
    with contextlib.ExitStack() as es:
        for p in _no_dupes():
            es.enter_context(p)
        es.enter_context(patch("app.api.admin._validate_source_url", AsyncMock()))
        es.enter_context(patch("app.sources.subsonic.SubsonicSource._probe_extensions_unauth",
                               AsyncMock(side_effect=lambda: order.append("probe") or True)))
        es.enter_context(patch("app.sources.subsonic.SubsonicSource.validate_credentials",
                               AsyncMock()))
        es.enter_context(patch("app.sources.subsonic.SubsonicSource.get_libraries",
                               AsyncMock(side_effect=lambda: [_lib("subsonic")])))
        es.enter_context(patch("app.api.admin.database.save_subsonic_source", save))
        es.enter_context(patch("app.api.admin._clear_source_veto", AsyncMock()))
        es.enter_context(patch("app.api.admin._enable_new_source_libraries",
                               AsyncMock(side_effect=lambda sid: order.append("enable"))))
        es.enter_context(patch("app.state.invalidate_plex_client",
                               MagicMock(side_effect=lambda: order.append("invalidate"))))
        es.enter_context(patch("app.state.trigger_catalog_refresh",
                               MagicMock(side_effect=lambda: order.append("refresh"))))
        resp = client.post("/admin/sources/subsonic", json={
            "server_url": "http://nav.local:4533", "secret": "APIKEY-abc",
            "username": "dj", "name": "Navidrome"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "subsonic" and body["name"] == "Navidrome"
    assert body["source_id"].startswith("subsonic-")
    assert body["auth_mode"] == "apikey"   # apiKey extension present → apiKey mode
    save.assert_awaited_once()
    kwargs = save.call_args.kwargs
    assert kwargs["token"] == "APIKEY-abc" and kwargs["user"] == "dj"
    assert kwargs["auth_mode"] == "apikey"
    # credential-free URL: no api key stored in server_url
    assert "APIKEY-abc" not in kwargs["server_url"]
    assert kwargs["server_url"] == "http://nav.local:4533"
    # enable runs BETWEEN invalidate and refresh (empty-catalog regression order)
    assert order == ["probe", "invalidate", "enable", "refresh"]


def _subsonic_connect_probes(es, save, *, has_apikey=True):
    """Shared patch set for a successful Subsonic connect (probe + validate +
    save + finish). ``has_apikey`` toggles the detected mode."""
    es.enter_context(patch("app.api.admin._validate_source_url", AsyncMock()))
    es.enter_context(patch("app.sources.subsonic.SubsonicSource._probe_extensions_unauth",
                           AsyncMock(return_value=has_apikey)))
    es.enter_context(patch("app.sources.subsonic.SubsonicSource.validate_credentials",
                           AsyncMock()))
    es.enter_context(patch("app.sources.subsonic.SubsonicSource.get_libraries",
                           AsyncMock(return_value=[_lib("subsonic")])))
    es.enter_context(patch("app.api.admin.database.save_subsonic_source", save))
    es.enter_context(patch("app.api.admin._clear_source_veto", AsyncMock()))
    es.enter_context(patch("app.api.admin._enable_new_source_libraries", AsyncMock()))
    es.enter_context(patch("app.state.invalidate_plex_client", MagicMock()))
    es.enter_context(patch("app.state.trigger_catalog_refresh", MagicMock()))


def test_connect_subsonic_reports_resolved_stream_base(client, mock_state):
    # A URL-auth (Subsonic) source surfaces the device-facing proxy base so a wrong
    # LAN-IP auto-detection is visible in the UI before a silent no-audio cast.
    save = AsyncMock()
    with contextlib.ExitStack() as es:
        for p in _no_dupes():
            es.enter_context(p)
        _subsonic_connect_probes(es, save)
        es.enter_context(patch("app.state.resolved_proxy_base_for_url_auth",
                               MagicMock(return_value="http://192.168.1.50")))
        resp = client.post("/admin/sources/subsonic", json={
            "server_url": "http://nav.local:4533", "secret": "k", "username": "dj"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["resolved_stream_base"] == "http://192.168.1.50"
    save.assert_awaited_once()


def test_connect_subsonic_resolved_stream_base_null_when_unresolvable(client, mock_state):
    # Best-effort: when no device-reachable base exists (RuntimeError), report null so
    # the UI shows the actionable "set STREAM_BASE_URL" guidance — the connect still
    # succeeds (base detection must never fail the connect).
    save = AsyncMock()
    with contextlib.ExitStack() as es:
        for p in _no_dupes():
            es.enter_context(p)
        _subsonic_connect_probes(es, save)
        es.enter_context(patch("app.state.resolved_proxy_base_for_url_auth",
                               MagicMock(side_effect=RuntimeError("no device-reachable base"))))
        resp = client.post("/admin/sources/subsonic", json={
            "server_url": "http://nav.local:4533", "secret": "k", "username": "dj"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["resolved_stream_base"] is None
    save.assert_awaited_once()


def test_connect_subsonic_no_extension_uses_token_mode(client, mock_state):
    # AE1 (token+salt fallback): a server WITHOUT the apiKeyAuthentication
    # extension is NO LONGER rejected — the secret is routed as a password and the
    # source connects via token+salt, saved with auth_mode='token'.
    save = AsyncMock()
    with contextlib.ExitStack() as es:
        for p in _no_dupes():
            es.enter_context(p)
        _subsonic_connect_probes(es, save, has_apikey=False)
        resp = client.post("/admin/sources/subsonic", json={
            "server_url": "http://old.local:4040", "secret": "my-password",
            "username": "dj"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["auth_mode"] == "token"
    save.assert_awaited_once()
    kwargs = save.call_args.kwargs
    assert kwargs["auth_mode"] == "token" and kwargs["token"] == "my-password"


def test_connect_subsonic_wrong_password_token_mode_not_saved(client, mock_state):
    # AE5 (the gap get_libraries would have missed): a wrong password in token mode
    # → the authenticated validate raises → auth_rejected, source NOT saved.
    from app.sources.subsonic import SubsonicAuthError
    save = AsyncMock()
    with contextlib.ExitStack() as es:
        for p in _no_dupes():
            es.enter_context(p)
        es.enter_context(patch("app.api.admin._validate_source_url", AsyncMock()))
        es.enter_context(patch("app.sources.subsonic.SubsonicSource._probe_extensions_unauth",
                               AsyncMock(return_value=False)))  # → token mode
        es.enter_context(patch("app.sources.subsonic.SubsonicSource.validate_credentials",
                               AsyncMock(side_effect=SubsonicAuthError("wrong password"))))
        es.enter_context(patch("app.api.admin.database.save_subsonic_source", save))
        resp = client.post("/admin/sources/subsonic", json={
            "server_url": "http://gonic.local:4747", "secret": "badpw",
            "username": "dj"})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["category"] == "auth_rejected"
    # AE5: the message is token-mode-specific (about the password), not the apiKey text.
    assert "password" in detail["message"].lower()
    save.assert_not_awaited()


def _subsonic_row(url, auth_mode):
    import hashlib
    sid = "subsonic-" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    return {"source_id": sid, "server_url": url, "name": "N", "token": "x",
            "user": "dj", "client": "Jukeplox", "auth_mode": auth_mode}


def test_connect_subsonic_probe_error_no_downgrade_of_stored_apikey(client, mock_state):
    # security-lens P2: a probe hard-error must NOT silently downgrade a source
    # already stored as auth_mode='apikey'. Uses the REAL dup-check: the same-URL
    # apiKey row is exempted from the duplicate gate (reconnect), the prior-mode
    # lookup finds it, and the failed probe → refuse (unreachable), source intact.
    import httpx
    url = "http://nav.local:4533"
    save = AsyncMock()
    with contextlib.ExitStack() as es:
        es.enter_context(patch("app.api.admin.database.get_plex_servers", AsyncMock(return_value=[])))
        es.enter_context(patch("app.api.admin.database.get_jellyfin_sources", AsyncMock(return_value=[])))
        es.enter_context(patch("app.api.admin.database.get_subsonic_sources",
                               AsyncMock(return_value=[_subsonic_row(url, "apikey")])))
        es.enter_context(patch("app.api.admin.database.get_emby_sources", AsyncMock(return_value=[])))
        es.enter_context(patch("app.api.admin._validate_source_url", AsyncMock()))
        es.enter_context(patch("app.sources.subsonic.SubsonicSource._probe_extensions_unauth",
                               AsyncMock(side_effect=httpx.ConnectError("probe boom"))))
        es.enter_context(patch("app.api.admin.database.save_subsonic_source", save))
        resp = client.post("/admin/sources/subsonic", json={
            "server_url": url, "secret": "somekey", "username": "dj"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["category"] == "unreachable"
    save.assert_not_awaited()  # existing apiKey source left intact, not downgraded


def test_connect_subsonic_reconnect_same_url_not_duplicate_flips_mode(client, mock_state):
    # A source stored as apiKey can be RE-connected at the same URL (not rejected as
    # duplicate) and flip to token+salt when the server now lacks the extension.
    url = "http://nav.local:4533"
    save = AsyncMock()
    with contextlib.ExitStack() as es:
        es.enter_context(patch("app.api.admin.database.get_plex_servers", AsyncMock(return_value=[])))
        es.enter_context(patch("app.api.admin.database.get_jellyfin_sources", AsyncMock(return_value=[])))
        es.enter_context(patch("app.api.admin.database.get_subsonic_sources",
                               AsyncMock(return_value=[_subsonic_row(url, "apikey")])))
        es.enter_context(patch("app.api.admin.database.get_emby_sources", AsyncMock(return_value=[])))
        es.enter_context(patch("app.api.admin._validate_source_url", AsyncMock()))
        es.enter_context(patch("app.sources.subsonic.SubsonicSource._probe_extensions_unauth",
                               AsyncMock(return_value=False)))  # server now lacks apiKey
        es.enter_context(patch("app.sources.subsonic.SubsonicSource.validate_credentials", AsyncMock()))
        es.enter_context(patch("app.sources.subsonic.SubsonicSource.get_libraries",
                               AsyncMock(return_value=[_lib("subsonic")])))
        es.enter_context(patch("app.api.admin.database.save_subsonic_source", save))
        es.enter_context(patch("app.api.admin._clear_source_veto", AsyncMock()))
        es.enter_context(patch("app.api.admin._enable_new_source_libraries", AsyncMock()))
        es.enter_context(patch("app.state.invalidate_plex_client", MagicMock()))
        es.enter_context(patch("app.state.trigger_catalog_refresh", MagicMock()))
        resp = client.post("/admin/sources/subsonic", json={
            "server_url": url + "/", "secret": "the-password", "username": "dj"})
    assert resp.status_code == 200          # NOT rejected as duplicate
    assert resp.json()["auth_mode"] == "token"
    save.assert_awaited_once()
    assert save.call_args.kwargs["auth_mode"] == "token"


def test_connect_subsonic_probe_error_new_source_falls_back_to_token(client, mock_state):
    # A probe hard-error on a NEW url (no prior row) falls back to token mode and
    # saves after validate_credentials succeeds.
    import httpx
    save = AsyncMock()
    with contextlib.ExitStack() as es:
        for p in _no_dupes():
            es.enter_context(p)
        es.enter_context(patch("app.api.admin._validate_source_url", AsyncMock()))
        es.enter_context(patch("app.sources.subsonic.SubsonicSource._probe_extensions_unauth",
                               AsyncMock(side_effect=httpx.ConnectError("probe boom"))))
        es.enter_context(patch("app.sources.subsonic.SubsonicSource.validate_credentials", AsyncMock()))
        es.enter_context(patch("app.sources.subsonic.SubsonicSource.get_libraries",
                               AsyncMock(return_value=[_lib("subsonic")])))
        es.enter_context(patch("app.api.admin.database.save_subsonic_source", save))
        es.enter_context(patch("app.api.admin._clear_source_veto", AsyncMock()))
        es.enter_context(patch("app.api.admin._enable_new_source_libraries", AsyncMock()))
        es.enter_context(patch("app.state.invalidate_plex_client", MagicMock()))
        es.enter_context(patch("app.state.trigger_catalog_refresh", MagicMock()))
        resp = client.post("/admin/sources/subsonic", json={
            "server_url": "http://newbox.local:4533", "secret": "pw", "username": "dj"})
    assert resp.status_code == 200
    assert resp.json()["auth_mode"] == "token"
    save.assert_awaited_once()


def test_connect_subsonic_probe_error_and_lookup_failure_refuses(client, mock_state):
    # reliability P1: if the prior-mode DB read fails AND the probe fails, we can't
    # rule out an existing apiKey source → refuse (fail-safe), do not save token.
    import httpx
    save = AsyncMock()
    with contextlib.ExitStack() as es:
        es.enter_context(patch("app.api.admin.database.get_plex_servers", AsyncMock(return_value=[])))
        es.enter_context(patch("app.api.admin.database.get_jellyfin_sources", AsyncMock(return_value=[])))
        es.enter_context(patch("app.api.admin.database.get_emby_sources", AsyncMock(return_value=[])))
        es.enter_context(patch("app.api.admin._validate_source_url", AsyncMock()))
        # The shared prior-mode read raises; the dup-check's own fallback read
        # then returns [] — prior_lookup_ok is False, so a probe failure refuses.
        es.enter_context(patch("app.api.admin.database.get_subsonic_sources",
                               AsyncMock(side_effect=[RuntimeError("db down"), []])))
        es.enter_context(patch("app.sources.subsonic.SubsonicSource._probe_extensions_unauth",
                               AsyncMock(side_effect=httpx.ConnectError("probe boom"))))
        es.enter_context(patch("app.api.admin.database.save_subsonic_source", save))
        resp = client.post("/admin/sources/subsonic", json={
            "server_url": "http://nav.local:4533", "secret": "k", "username": "dj"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["category"] == "unreachable"
    save.assert_not_awaited()


def test_connect_subsonic_unreachable_not_saved(client, mock_state):
    import httpx
    save = AsyncMock()
    with contextlib.ExitStack() as es:
        for p in _no_dupes():
            es.enter_context(p)
        es.enter_context(patch("app.api.admin._validate_source_url", AsyncMock()))
        es.enter_context(patch("app.sources.subsonic.SubsonicSource._probe_extensions_unauth",
                               AsyncMock(return_value=True)))
        es.enter_context(patch("app.sources.subsonic.SubsonicSource.validate_credentials",
                               AsyncMock(side_effect=httpx.ConnectError("boom"))))
        es.enter_context(patch("app.api.admin.database.save_subsonic_source", save))
        resp = client.post("/admin/sources/subsonic", json={
            "server_url": "http://nope.local:4533", "secret": "k", "username": "dj"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["category"] == "unreachable"
    save.assert_not_awaited()


def test_connect_subsonic_bad_key_auth_rejected(client, mock_state):
    # apiKey mode: a wrong key → the authenticated validate raises → auth_rejected.
    from app.sources.subsonic import SubsonicAuthError
    save = AsyncMock()
    with contextlib.ExitStack() as es:
        for p in _no_dupes():
            es.enter_context(p)
        es.enter_context(patch("app.api.admin._validate_source_url", AsyncMock()))
        es.enter_context(patch("app.sources.subsonic.SubsonicSource._probe_extensions_unauth",
                               AsyncMock(return_value=True)))
        es.enter_context(patch("app.sources.subsonic.SubsonicSource.validate_credentials",
                               AsyncMock(side_effect=SubsonicAuthError("bad api key"))))
        es.enter_context(patch("app.api.admin.database.save_subsonic_source", save))
        resp = client.post("/admin/sources/subsonic", json={
            "server_url": "http://nav.local:4533", "secret": "wrong", "username": "dj"})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["category"] == "auth_rejected"
    # AE5: apiKey-mode message names the API key, not the password.
    assert "api key" in detail["message"].lower()
    save.assert_not_awaited()


def test_connect_subsonic_no_music_libraries_warns_but_saves(client, mock_state):
    # Connected but zero music sections → source still saved (warning-level, R10).
    save = AsyncMock()
    with contextlib.ExitStack() as es:
        for p in _no_dupes():
            es.enter_context(p)
        es.enter_context(patch("app.api.admin._validate_source_url", AsyncMock()))
        es.enter_context(patch("app.sources.subsonic.SubsonicSource._probe_extensions_unauth",
                               AsyncMock(return_value=True)))
        es.enter_context(patch("app.sources.subsonic.SubsonicSource.validate_credentials",
                               AsyncMock()))
        es.enter_context(patch("app.sources.subsonic.SubsonicSource.get_libraries",
                               AsyncMock(return_value=[])))
        es.enter_context(patch("app.api.admin.database.save_subsonic_source", save))
        es.enter_context(patch("app.api.admin._clear_source_veto", AsyncMock()))
        es.enter_context(patch("app.api.admin._enable_new_source_libraries", AsyncMock()))
        es.enter_context(patch("app.state.invalidate_plex_client", MagicMock()))
        es.enter_context(patch("app.state.trigger_catalog_refresh", MagicMock()))
        resp = client.post("/admin/sources/subsonic", json={
            "server_url": "http://nav.local:4533", "secret": "k", "username": "dj"})
    assert resp.status_code == 200
    save.assert_awaited_once()   # saved despite no music libraries
    # The zero-music-libraries case returns a warning on the 200 (no error category).
    assert resp.json().get("warning")


def test_connect_subsonic_duplicate_url_rejected(client, mock_state):
    # A source already at the same normalized URL → duplicate, rejected before probe.
    save = AsyncMock()
    with patch("app.api.admin.database.get_plex_servers", AsyncMock(return_value=[])), \
         patch("app.api.admin.database.get_jellyfin_sources", AsyncMock(return_value=[])), \
         patch("app.api.admin.database.get_subsonic_sources",
               AsyncMock(return_value=[{"source_id": "subsonic-x",
                                        "server_url": "http://nav.local:4533"}])), \
         patch("app.api.admin.database.get_emby_sources", AsyncMock(return_value=[])), \
         patch("app.api.admin.database.save_subsonic_source", save):
        # trailing slash + default-equivalent form still collides after normalization
        resp = client.post("/admin/sources/subsonic", json={
            "server_url": "http://nav.local:4533/", "secret": "k", "username": "dj"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["category"] == "duplicate"
    save.assert_not_awaited()


def test_connect_subsonic_enable_failure_does_not_500(client, mock_state):
    # A post-save library-seed failure (the empty-catalog gate's own get_libraries()
    # raising inside _enable_new_source_libraries) must NOT 500 the connect nor skip
    # the refresh — the enable is best-effort/WARNING-guarded.
    import hashlib
    sid = "subsonic-" + hashlib.sha1(
        "http://nav.local:4533".encode("utf-8")).hexdigest()[:12]

    class _FakeSrc:
        source_id = sid
        async def get_libraries(self):
            raise RuntimeError("subsonic getArtists failed")

    class _Reg:
        sources = [_FakeSrc()]

    save = AsyncMock()
    with contextlib.ExitStack() as es:
        for p in _no_dupes():
            es.enter_context(p)
        # First call = connect probe (succeeds), then _enable reads the registry.
        es.enter_context(patch("app.api.admin._validate_source_url", AsyncMock()))
        es.enter_context(patch("app.sources.subsonic.SubsonicSource._probe_extensions_unauth",
                               AsyncMock(return_value=True)))
        es.enter_context(patch("app.sources.subsonic.SubsonicSource.validate_credentials",
                               AsyncMock()))
        es.enter_context(patch("app.sources.subsonic.SubsonicSource.get_libraries",
                               AsyncMock(return_value=[_lib("subsonic")])))
        es.enter_context(patch("app.api.admin.database.save_subsonic_source", save))
        es.enter_context(patch("app.api.admin._clear_source_veto", AsyncMock()))
        es.enter_context(patch("app.state.get_plex_client", AsyncMock(return_value=_Reg())))
        es.enter_context(patch("app.database.toggle_library", AsyncMock()))
        es.enter_context(patch("app.state.invalidate_plex_client", MagicMock()))
        scan = es.enter_context(patch("app.state.trigger_catalog_refresh", MagicMock()))
        resp = client.post("/admin/sources/subsonic", json={
            "server_url": "http://nav.local:4533", "secret": "k", "username": "dj"})
    assert resp.status_code == 200        # enable failure did not 500 the connect
    scan.assert_called_once()             # trigger_catalog_refresh still fired


def test_remove_subsonic_source(client, mock_state):
    dele = AsyncMock()
    with patch("app.api.admin.database.delete_subsonic_source", dele), \
         patch("app.state.trigger_catalog_refresh", MagicMock()), \
         patch("app.state.invalidate_plex_client", MagicMock()):
        resp = client.delete("/admin/sources/subsonic/subsonic-abc")
    assert resp.status_code == 200
    dele.assert_awaited_once_with("subsonic-abc")


def test_connect_emby_success_saves_token_only_and_scans(client, mock_state):
    # Sign-in exchanges username/password for a token; password discarded, scan runs.
    save = AsyncMock()
    with contextlib.ExitStack() as es:
        for p in _no_dupes():
            es.enter_context(p)
        es.enter_context(patch("app.api.admin._validate_source_url", AsyncMock()))
        es.enter_context(patch("app.sources.emby.authenticate",
                               AsyncMock(return_value={"token": "etok", "user_id": "eu1",
                                                       "server_id": "esrv"})))
        es.enter_context(patch("app.sources.emby.EmbySource.get_libraries",
                               AsyncMock(return_value=[_lib("emby-esrv")])))
        es.enter_context(patch("app.api.admin.database.save_emby_source", save))
        es.enter_context(patch("app.api.admin._clear_source_veto", AsyncMock()))
        es.enter_context(patch("app.api.admin._enable_new_source_libraries", AsyncMock()))
        es.enter_context(patch("app.state.invalidate_plex_client", MagicMock()))
        scan = es.enter_context(patch("app.state.trigger_catalog_refresh", MagicMock()))
        resp = client.post("/admin/sources/emby", json={
            "server_url": "http://emby.local:8096", "username": "admin",
            "password": "secret", "name": "Living Room"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "emby" and body["name"] == "Living Room"
    assert body["source_id"] == "emby-esrv"
    save.assert_awaited_once()
    kwargs = save.call_args.kwargs
    assert kwargs["token"] == "etok" and kwargs["user_id"] == "eu1"
    assert not ({"password", "pw"} & set(kwargs))   # password never persisted (R5)
    scan.assert_called_once()


def test_connect_emby_bad_credentials_not_saved(client, mock_state):
    from app.sources.emby import EmbyAuthError
    save = AsyncMock()
    with contextlib.ExitStack() as es:
        for p in _no_dupes():
            es.enter_context(p)
        es.enter_context(patch("app.api.admin._validate_source_url", AsyncMock()))
        es.enter_context(patch("app.sources.emby.authenticate",
                               AsyncMock(side_effect=EmbyAuthError("nope"))))
        es.enter_context(patch("app.api.admin.database.save_emby_source", save))
        resp = client.post("/admin/sources/emby", json={
            "server_url": "http://emby.local:8096", "username": "admin", "password": "bad"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["category"] == "auth_rejected"
    save.assert_not_awaited()


def test_connect_emby_unreachable_not_saved(client, mock_state):
    import httpx
    save = AsyncMock()
    with contextlib.ExitStack() as es:
        for p in _no_dupes():
            es.enter_context(p)
        es.enter_context(patch("app.api.admin._validate_source_url", AsyncMock()))
        es.enter_context(patch("app.sources.emby.authenticate",
                               AsyncMock(side_effect=httpx.ConnectError("boom"))))
        es.enter_context(patch("app.api.admin.database.save_emby_source", save))
        resp = client.post("/admin/sources/emby", json={
            "server_url": "http://nope.local:8096", "username": "admin", "password": "x"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["category"] == "unreachable"
    save.assert_not_awaited()


def test_connect_emby_no_music_libraries_warns_but_saves(client, mock_state):
    # Connected but zero music sections → source still saved (warning-level, R10).
    # Mirrors the Subsonic no-music test; asserts the `warning` field the fix added
    # in place of the dead `no_music_libraries` error category.
    save = AsyncMock()
    with contextlib.ExitStack() as es:
        for p in _no_dupes():
            es.enter_context(p)
        es.enter_context(patch("app.api.admin._validate_source_url", AsyncMock()))
        es.enter_context(patch("app.sources.emby.authenticate",
                               AsyncMock(return_value={"token": "etok", "user_id": "eu1",
                                                       "server_id": "esrv"})))
        es.enter_context(patch("app.sources.emby.EmbySource.get_libraries",
                               AsyncMock(return_value=[])))
        es.enter_context(patch("app.api.admin.database.save_emby_source", save))
        es.enter_context(patch("app.api.admin._clear_source_veto", AsyncMock()))
        es.enter_context(patch("app.api.admin._enable_new_source_libraries", AsyncMock()))
        es.enter_context(patch("app.state.invalidate_plex_client", MagicMock()))
        es.enter_context(patch("app.state.trigger_catalog_refresh", MagicMock()))
        resp = client.post("/admin/sources/emby", json={
            "server_url": "http://emby.local:8096", "username": "admin",
            "password": "secret"})
    assert resp.status_code == 200
    save.assert_awaited_once()   # saved despite no music libraries
    # The zero-music-libraries case returns a warning on the 200 (no error category).
    assert resp.json().get("warning")


def test_connect_emby_duplicate_url_rejected(client, mock_state):
    # A source already at the same normalized URL → duplicate, rejected before the
    # authenticate probe. Trailing-slash variant still collides after normalization.
    save = AsyncMock()
    auth = AsyncMock()
    with patch("app.api.admin.database.get_plex_servers", AsyncMock(return_value=[])), \
         patch("app.api.admin.database.get_jellyfin_sources", AsyncMock(return_value=[])), \
         patch("app.api.admin.database.get_subsonic_sources", AsyncMock(return_value=[])), \
         patch("app.api.admin.database.get_emby_sources",
               AsyncMock(return_value=[{"source_id": "emby-x",
                                        "server_url": "http://emby.local:8096"}])), \
         patch("app.sources.emby.authenticate", auth), \
         patch("app.api.admin.database.save_emby_source", save):
        # trailing slash still collides after normalization
        resp = client.post("/admin/sources/emby", json={
            "server_url": "http://emby.local:8096/", "username": "admin",
            "password": "x"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["category"] == "duplicate"
    auth.assert_not_awaited()     # rejected before the outbound probe
    save.assert_not_awaited()


def test_remove_emby_source(client, mock_state):
    dele = AsyncMock()
    with patch("app.api.admin.database.delete_emby_source", dele), \
         patch("app.state.trigger_catalog_refresh", MagicMock()), \
         patch("app.state.invalidate_plex_client", MagicMock()):
        resp = client.delete("/admin/sources/emby/emby-abc")
    assert resp.status_code == 200
    dele.assert_awaited_once_with("emby-abc")


# ── U6: connect-time SSRF URL validation ──────────────────────────────────────
#
# Helpers: patch DNS so a hostname resolves to a chosen IP without touching the
# network, and toggle settings.allow_private_sources. Probes are patched to a
# sentinel so a test can assert the outbound call NEVER happened on rejection.

def _fake_getaddrinfo(ip):
    """A socket.getaddrinfo stand-in that resolves any host to ``ip``."""
    def _resolver(host, port, *a, **kw):
        return [(2, 1, 6, "", (ip, 0))]
    return _resolver


def _resolve_to(ip):
    """Patch the resolver used by _validate_source_url to return ``ip``."""
    return patch("socket.getaddrinfo", side_effect=_fake_getaddrinfo(ip))


def _allow_private(value):
    return patch("app.config.settings.allow_private_sources", value)


def test_connect_subsonic_private_lan_allowed_by_default(client, mock_state):
    # Happy path — default ALLOW_PRIVATE_SOURCES=True: a first-run LAN connect to a
    # 192.168.x.x server reaches the probe and succeeds.
    save = AsyncMock()
    probe = AsyncMock(return_value=True)
    with contextlib.ExitStack() as es:
        for p in _no_dupes():
            es.enter_context(p)
        es.enter_context(_allow_private(True))
        es.enter_context(_resolve_to("192.168.1.50"))
        es.enter_context(patch("app.sources.subsonic.SubsonicSource._probe_extensions_unauth",
                               probe))
        es.enter_context(patch("app.sources.subsonic.SubsonicSource.validate_credentials",
                               AsyncMock()))
        es.enter_context(patch("app.sources.subsonic.SubsonicSource.get_libraries",
                               AsyncMock(return_value=[_lib("subsonic")])))
        es.enter_context(patch("app.api.admin.database.save_subsonic_source", save))
        es.enter_context(patch("app.api.admin._clear_source_veto", AsyncMock()))
        es.enter_context(patch("app.api.admin._enable_new_source_libraries", AsyncMock()))
        es.enter_context(patch("app.state.invalidate_plex_client", MagicMock()))
        es.enter_context(patch("app.state.trigger_catalog_refresh", MagicMock()))
        resp = client.post("/admin/sources/subsonic", json={
            "server_url": "http://nav.lan:4533", "secret": "k", "username": "dj"})
    assert resp.status_code == 200
    probe.assert_awaited()          # reached the outbound probe
    save.assert_awaited_once()


def test_connect_subsonic_private_rejected_when_flag_off(client, mock_state):
    # Error path — ALLOW_PRIVATE_SOURCES=False: a 192.168.x.x URL is rejected BEFORE
    # any outbound call, with a category naming the flag.
    save = AsyncMock()
    probe = AsyncMock()
    with contextlib.ExitStack() as es:
        for p in _no_dupes():
            es.enter_context(p)
        es.enter_context(_allow_private(False))
        es.enter_context(_resolve_to("192.168.1.50"))
        es.enter_context(patch("app.sources.subsonic.SubsonicSource._probe_extensions_unauth",
                               probe))
        es.enter_context(patch("app.api.admin.database.save_subsonic_source", save))
        resp = client.post("/admin/sources/subsonic", json={
            "server_url": "http://nav.lan:4533", "secret": "k", "username": "dj"})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["category"] == "blocked_private"
    assert "ALLOW_PRIVATE_SOURCES" in detail["message"]
    probe.assert_not_awaited()      # rejected before the outbound probe
    save.assert_not_awaited()


@pytest.mark.parametrize("private_ip", ["192.168.1.10", "10.0.0.5", "172.16.4.9"])
def test_connect_subsonic_all_rfc1918_ranges_rejected_when_flag_off(
        client, mock_state, private_ip):
    # Error path — every RFC-1918 range (192.168/16, 10/8, 172.16/12) is rejected
    # pre-probe with the flag off.
    save = AsyncMock()
    probe = AsyncMock()
    with contextlib.ExitStack() as es:
        for p in _no_dupes():
            es.enter_context(p)
        es.enter_context(_allow_private(False))
        es.enter_context(_resolve_to(private_ip))
        es.enter_context(patch("app.sources.subsonic.SubsonicSource._probe_extensions_unauth",
                               probe))
        es.enter_context(patch("app.api.admin.database.save_subsonic_source", save))
        resp = client.post("/admin/sources/subsonic", json={
            "server_url": f"http://{private_ip}:4533", "secret": "k", "username": "dj"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["category"] == "blocked_private"
    probe.assert_not_awaited()
    save.assert_not_awaited()


@pytest.mark.parametrize("blocked_ip", ["127.0.0.1", "169.254.10.5", "::1", "fe80::1"])
@pytest.mark.parametrize("allow", [True, False])
def test_connect_subsonic_loopback_and_linklocal_always_rejected(
        client, mock_state, blocked_ip, allow):
    # Edge case — loopback (127.0.0.1, ::1) and link-local (169.254.x.x, fe80::/10)
    # are rejected regardless of the flag (even with ALLOW_PRIVATE_SOURCES=True).
    # The IPv6 cases (::1, fe80::1) match the _validate_source_url docstring, which
    # names ::1 and fe80::/10 as always-blocked.
    save = AsyncMock()
    probe = AsyncMock()
    with contextlib.ExitStack() as es:
        for p in _no_dupes():
            es.enter_context(p)
        es.enter_context(_allow_private(allow))
        es.enter_context(_resolve_to(blocked_ip))
        es.enter_context(patch("app.sources.subsonic.SubsonicSource._probe_extensions_unauth",
                               probe))
        es.enter_context(patch("app.api.admin.database.save_subsonic_source", save))
        # Bracket IPv6 literals so urlparse yields the host (bare v6 breaks parsing).
        host = f"[{blocked_ip}]" if ":" in blocked_ip else blocked_ip
        resp = client.post("/admin/sources/subsonic", json={
            "server_url": f"http://{host}:4533", "secret": "k", "username": "dj"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["category"] == "blocked_private"
    probe.assert_not_awaited()
    save.assert_not_awaited()


def test_connect_subsonic_hostname_resolving_to_private_treated_as_private(
        client, mock_state):
    # A hostname (not a literal IP) that RESOLVES to a private IP is rejected like a
    # private-IP URL when the flag is off.
    save = AsyncMock()
    probe = AsyncMock()
    with contextlib.ExitStack() as es:
        for p in _no_dupes():
            es.enter_context(p)
        es.enter_context(_allow_private(False))
        es.enter_context(_resolve_to("10.1.2.3"))   # public-looking host, private A record
        es.enter_context(patch("app.sources.subsonic.SubsonicSource._probe_extensions_unauth",
                               probe))
        es.enter_context(patch("app.api.admin.database.save_subsonic_source", save))
        resp = client.post("/admin/sources/subsonic", json={
            "server_url": "http://music.example.com", "secret": "k", "username": "dj"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["category"] == "blocked_private"
    probe.assert_not_awaited()
    save.assert_not_awaited()


def test_connect_subsonic_public_url_unaffected(client, mock_state):
    # Edge case — a public-resolving host connects normally (validation is a no-op).
    save = AsyncMock()
    probe = AsyncMock(return_value=True)
    with contextlib.ExitStack() as es:
        for p in _no_dupes():
            es.enter_context(p)
        es.enter_context(_allow_private(False))      # even hardened, public is fine
        es.enter_context(_resolve_to("93.184.216.34"))   # public IP
        es.enter_context(patch("app.sources.subsonic.SubsonicSource._probe_extensions_unauth",
                               probe))
        es.enter_context(patch("app.sources.subsonic.SubsonicSource.validate_credentials",
                               AsyncMock()))
        es.enter_context(patch("app.sources.subsonic.SubsonicSource.get_libraries",
                               AsyncMock(return_value=[_lib("subsonic")])))
        es.enter_context(patch("app.api.admin.database.save_subsonic_source", save))
        es.enter_context(patch("app.api.admin._clear_source_veto", AsyncMock()))
        es.enter_context(patch("app.api.admin._enable_new_source_libraries", AsyncMock()))
        es.enter_context(patch("app.state.invalidate_plex_client", MagicMock()))
        es.enter_context(patch("app.state.trigger_catalog_refresh", MagicMock()))
        resp = client.post("/admin/sources/subsonic", json={
            "server_url": "https://music.example.net", "secret": "k", "username": "dj"})
    assert resp.status_code == 200
    probe.assert_awaited()
    save.assert_awaited_once()


def test_connect_subsonic_dns_failure_fails_closed(client, mock_state):
    # P2: a host that can't be resolved must FAIL CLOSED — rejected as
    # 'unreachable' before the outbound probe, never let through the SSRF gate.
    import socket as _socket
    save = AsyncMock()
    probe = AsyncMock()
    with contextlib.ExitStack() as es:
        for p in _no_dupes():
            es.enter_context(p)
        es.enter_context(patch("socket.getaddrinfo",
                               side_effect=_socket.gaierror("name resolution failed")))
        es.enter_context(patch("app.sources.subsonic.SubsonicSource._probe_extensions_unauth",
                               probe))
        es.enter_context(patch("app.api.admin.database.save_subsonic_source", save))
        resp = client.post("/admin/sources/subsonic", json={
            "server_url": "http://does-not-resolve.invalid:4533", "secret": "k",
            "username": "dj"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["category"] == "unreachable"
    probe.assert_not_awaited()   # never reached the outbound probe
    save.assert_not_awaited()


def test_connect_subsonic_loopback_rejected_before_source_constructed(client, mock_state):
    # P2 (fix #5): a loopback/blocked URL is rejected and NO source row is saved
    # and NO SubsonicSource (httpx client) is constructed — validation runs FIRST,
    # before the source alloc.
    save = AsyncMock()
    ctor = MagicMock(side_effect=AssertionError(
        "SubsonicSource must not be constructed for a blocked URL"))
    with contextlib.ExitStack() as es:
        for p in _no_dupes():
            es.enter_context(p)
        es.enter_context(_allow_private(True))          # even permissive, loopback is blocked
        es.enter_context(_resolve_to("127.0.0.1"))
        es.enter_context(patch("app.sources.subsonic.SubsonicSource", ctor))
        es.enter_context(patch("app.api.admin.database.save_subsonic_source", save))
        resp = client.post("/admin/sources/subsonic", json={
            "server_url": "http://127.0.0.1:4533", "secret": "k", "username": "dj"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["category"] == "blocked_private"
    ctor.assert_not_called()      # no client allocated (no leak)
    save.assert_not_awaited()     # no source row saved


def test_connect_subsonic_probe_client_does_not_follow_redirects():
    # Redirect / DNS-rebind hardening — the Subsonic probe client is constructed
    # without follow_redirects (httpx default False), so a public host cannot 302
    # the probe to a private target. Asserted structurally on the built client.
    from app.sources.subsonic import SubsonicSource
    import asyncio as _asyncio
    src = SubsonicSource(server_url="http://nav.lan:4533", api_key="k", username="dj")
    try:
        assert src._http.follow_redirects is False
    finally:
        _asyncio.new_event_loop().run_until_complete(src._http.aclose())


def test_connect_emby_private_rejected_when_flag_off(client, mock_state):
    # Error path — Emby connect obeys the same SSRF guard: private IP rejected
    # pre-probe with the flag off, message names the flag.
    save = AsyncMock()
    auth = AsyncMock(return_value={"token": "t", "user_id": "u", "server_id": "s"})
    with contextlib.ExitStack() as es:
        for p in _no_dupes():
            es.enter_context(p)
        es.enter_context(_allow_private(False))
        es.enter_context(_resolve_to("192.168.1.10"))
        es.enter_context(patch("app.sources.emby.authenticate", auth))
        es.enter_context(patch("app.api.admin.database.save_emby_source", save))
        resp = client.post("/admin/sources/emby", json={
            "server_url": "http://emby.lan:8096", "username": "admin", "password": "x"})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["category"] == "blocked_private"
    assert "ALLOW_PRIVATE_SOURCES" in detail["message"]
    auth.assert_not_awaited()       # rejected before the outbound authenticate
    save.assert_not_awaited()


def test_connect_emby_loopback_rejected_regardless_of_flag(client, mock_state):
    # Edge case — Emby loopback rejected even with the flag on.
    save = AsyncMock()
    auth = AsyncMock()
    with contextlib.ExitStack() as es:
        for p in _no_dupes():
            es.enter_context(p)
        es.enter_context(_allow_private(True))
        es.enter_context(_resolve_to("127.0.0.1"))
        es.enter_context(patch("app.sources.emby.authenticate", auth))
        es.enter_context(patch("app.api.admin.database.save_emby_source", save))
        resp = client.post("/admin/sources/emby", json={
            "server_url": "http://localhost:8096", "username": "admin", "password": "x"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["category"] == "blocked_private"
    auth.assert_not_awaited()
    save.assert_not_awaited()


def test_connect_emby_authenticate_client_does_not_follow_redirects():
    # Redirect hardening for the Emby probe: emby.authenticate builds its httpx
    # client without follow_redirects (default False). Verified by capturing the
    # client passed through to the request.
    import asyncio as _asyncio
    import app.sources.emby as emby_mod

    captured = {}
    orig_client_cls = emby_mod.httpx.AsyncClient

    def _spy(*a, **kw):
        c = orig_client_cls(*a, **kw)
        captured["follow_redirects"] = c.follow_redirects
        return c

    async def _run():
        with patch.object(emby_mod.httpx, "AsyncClient", _spy):
            with contextlib.suppress(Exception):
                # No server; the request fails, but the client was built first.
                await emby_mod.authenticate(
                    "http://127.0.0.1:1", "u", "p", device_id="d")
    _asyncio.new_event_loop().run_until_complete(_run())
    assert captured.get("follow_redirects") is False


def test_list_sources_includes_subsonic_and_emby(client, mock_state):
    with patch("app.api.admin.database.get_plex_servers", AsyncMock(return_value=[])), \
         patch("app.api.admin.database.get_plex_config", AsyncMock(return_value=None)), \
         patch("app.api.admin.database.get_jellyfin_sources", AsyncMock(return_value=[])), \
         patch("app.api.admin.database.get_subsonic_sources",
               AsyncMock(return_value=[{"source_id": "subsonic-1", "name": "Navidrome"}])), \
         patch("app.api.admin.database.get_emby_sources",
               AsyncMock(return_value=[{"source_id": "emby-1", "name": "Living Room"}])), \
         patch("app.api.admin.database.get_local_sources", AsyncMock(return_value=[])), \
         patch("app.api.admin.database.get_disabled_sources",
               AsyncMock(return_value=["emby-1"])):
        resp = client.get("/admin/sources")
    assert resp.status_code == 200
    srcs = {s["source_id"]: s for s in resp.json()["sources"]}
    assert srcs["subsonic-1"] == {"source_id": "subsonic-1", "type": "subsonic",
                                  "name": "Navidrome", "enabled": True}
    assert srcs["emby-1"] == {"source_id": "emby-1", "type": "emby",
                              "name": "Living Room", "enabled": False}


def test_sources_new_endpoints_require_auth(anon_client, mock_session):
    assert anon_client.post("/admin/sources/subsonic", json={}).status_code == 401
    assert anon_client.delete("/admin/sources/subsonic/x").status_code == 401
    assert anon_client.post("/admin/sources/emby", json={}).status_code == 401
    assert anon_client.delete("/admin/sources/emby/x").status_code == 401


def test_rescan_sources_triggers_refresh(client, mock_state):
    with patch("app.state.trigger_catalog_refresh", MagicMock()) as scan, \
         patch("app.state.trigger_browse_index_refresh", MagicMock()):
        resp = client.post("/admin/sources/rescan")
    assert resp.status_code == 200 and resp.json()["ok"] is True
    scan.assert_called_once()


def test_list_sources_includes_local(client, mock_state):
    with patch("app.api.admin.database.get_plex_servers", AsyncMock(return_value=[])), \
         patch("app.api.admin.database.get_plex_config", AsyncMock(return_value=None)), \
         patch("app.api.admin.database.get_jellyfin_sources", AsyncMock(return_value=[])), \
         patch("app.api.admin.database.get_subsonic_sources", AsyncMock(return_value=[])), \
         patch("app.api.admin.database.get_emby_sources", AsyncMock(return_value=[])), \
         patch("app.api.admin.database.get_disabled_sources", AsyncMock(return_value=[])), \
         patch("app.api.admin.database.get_local_sources",
               AsyncMock(return_value=[{"source_id": "local-abc", "name": "Vinyl Rips"}])):
        resp = client.get("/admin/sources")
    assert resp.status_code == 200
    assert {"source_id": "local-abc", "type": "local", "name": "Vinyl Rips", "enabled": True} in resp.json()["sources"]


def test_connect_local_success_saves_and_scans(client, mock_state, tmp_path):
    # Covers AE1 onboarding: a real readable directory → validated, saved, scan.
    music = tmp_path / "music"
    music.mkdir()
    save = AsyncMock()
    with patch("app.api.admin.database.save_local_source", save), \
         patch("app.state.trigger_catalog_refresh", MagicMock()) as scan, \
         patch("app.state.invalidate_plex_client", MagicMock()):
        resp = client.post("/admin/sources/local", json={"root_dir": str(music), "name": "My Music"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "local" and body["name"] == "My Music"
    assert body["source_id"].startswith("local-")
    save.assert_awaited_once()
    kwargs = save.call_args.kwargs
    import os
    assert kwargs["root_dir"] == os.path.realpath(str(music))  # canonical root stored
    scan.assert_called_once()


def test_connect_local_enables_its_library(client, mock_state, tmp_path):
    """Regression (ce-debug 2026-07-24): connecting a local source must enable its
    library. The catalog scan filters EVERY source's libraries by the enabled set,
    which is populated only by the Plex dashboard — so without this, a local (or
    Jellyfin) library is never enabled and the scan drops it, leaving an empty
    catalog. Verified live on a Raspberry Pi: connect → 0 albums until enabled."""
    import hashlib
    import os
    from app.models import Library
    music = tmp_path / "music"
    music.mkdir()
    real = os.path.realpath(str(music))
    sid = f"local-{hashlib.sha1(real.encode('utf-8')).hexdigest()[:12]}"

    class _FakeSrc:
        source_id = sid
        async def get_libraries(self):
            return [Library(key=f"{sid}:lib", title="My Music", type="artist", server_name="")]

    class _Reg:
        sources = [_FakeSrc()]

    toggle = AsyncMock()
    with patch("app.api.admin.database.save_local_source", AsyncMock()), \
         patch("app.state.get_plex_client", AsyncMock(return_value=_Reg())), \
         patch("app.database.toggle_library", toggle), \
         patch("app.state.trigger_catalog_refresh", MagicMock()), \
         patch("app.state.invalidate_plex_client", MagicMock()):
        resp = client.post("/admin/sources/local", json={"root_dir": str(music), "name": "My Music"})
    assert resp.status_code == 200
    toggle.assert_awaited_once_with(f"{sid}:lib", "My Music", enabled=True)


def test_connect_local_missing_dir_not_saved(client, mock_state, tmp_path):
    # Error path (R21): a non-existent directory → categorized inline, not saved.
    save = AsyncMock()
    with patch("app.api.admin.database.save_local_source", save):
        resp = client.post("/admin/sources/local",
                           json={"root_dir": str(tmp_path / "nope")})
    assert resp.status_code == 400
    assert resp.json()["detail"]["category"] == "dir_not_found"
    save.assert_not_awaited()


def test_connect_local_derives_stable_id_from_path(client, mock_state, tmp_path):
    # Reconnecting the same directory yields the same source_id (upsert, not dup).
    music = tmp_path / "music"
    music.mkdir()
    ids = []
    with patch("app.api.admin.database.save_local_source", AsyncMock()), \
         patch("app.state.trigger_catalog_refresh", MagicMock()), \
         patch("app.state.invalidate_plex_client", MagicMock()):
        for _ in range(2):
            r = client.post("/admin/sources/local", json={"root_dir": str(music)})
            ids.append(r.json()["source_id"])
    assert ids[0] == ids[1]


def test_remove_local_source(client, mock_state):
    dele = AsyncMock()
    with patch("app.api.admin.database.delete_local_source", dele), \
         patch("app.state.trigger_catalog_refresh", MagicMock()), \
         patch("app.api.admin.database.get_disabled_sources", AsyncMock(return_value=[])), \
         patch("app.state.invalidate_plex_client", MagicMock()):
        resp = client.delete("/admin/sources/local/local-abc")
    assert resp.status_code == 200
    dele.assert_awaited_once_with("local-abc")


# ── Whole-source on/off switch + veto lifecycle (Libraries-panel U3) ──────────

def test_list_sources_reports_veto_as_disabled(client, mock_state):
    # The whole-source switch: a vetoed source_id reports enabled=False.
    with patch("app.api.admin.database.get_plex_servers", AsyncMock(return_value=[])), \
         patch("app.api.admin.database.get_plex_config", AsyncMock(return_value=None)), \
         patch("app.api.admin.database.get_jellyfin_sources",
               AsyncMock(return_value=[{"source_id": "jf-1", "name": "Den"}])), \
         patch("app.api.admin.database.get_subsonic_sources", AsyncMock(return_value=[])), \
         patch("app.api.admin.database.get_emby_sources", AsyncMock(return_value=[])), \
         patch("app.api.admin.database.get_local_sources",
               AsyncMock(return_value=[{"source_id": "local-x", "name": "Vinyl"}])), \
         patch("app.api.admin.database.get_disabled_sources",
               AsyncMock(return_value=["jf-1"])):
        resp = client.get("/admin/sources")
    srcs = {s["source_id"]: s["enabled"] for s in resp.json()["sources"]}
    assert srcs == {"jf-1": False, "local-x": True}


def test_disable_source_writes_veto_and_reconciles(client, mock_state):
    setv = AsyncMock()
    with patch("app.api.admin.database.get_disabled_sources", AsyncMock(return_value=[])), \
         patch("app.api.admin.database.set_disabled_sources", setv), \
         patch("app.state.invalidate_plex_client", MagicMock()) as inval, \
         patch("app.state.trigger_browse_index_refresh", MagicMock()) as bidx, \
         patch("app.state.trigger_catalog_refresh", MagicMock()) as scan:
        resp = client.post("/admin/sources/jf-1/disable")
    assert resp.status_code == 200 and resp.json()["ok"] is True
    setv.assert_awaited_once_with(["jf-1"])   # added to the veto set
    inval.assert_called_once()                # SWR generation bumped (native immediacy)
    bidx.assert_called_once()                 # browse index reconciled
    scan.assert_called_once()                 # catalog reconciled


def test_enable_source_clears_veto(client, mock_state):
    setv = AsyncMock()
    with patch("app.api.admin.database.get_disabled_sources",
               AsyncMock(return_value=["jf-1", "local-x"])), \
         patch("app.api.admin.database.set_disabled_sources", setv), \
         patch("app.state.invalidate_plex_client", MagicMock()), \
         patch("app.state.trigger_catalog_refresh", MagicMock()):
        resp = client.post("/admin/sources/jf-1/enable")
    assert resp.status_code == 200
    setv.assert_awaited_once_with(["local-x"])   # jf-1 removed, local-x kept


def test_source_toggle_does_not_touch_library_rows(client, mock_state):
    # The remember guarantee: a source toggle only writes disabled_sources, never
    # enabled_libraries — so an off->on toggle restores the exact selection.
    toggle = AsyncMock()
    with patch("app.api.admin.database.get_disabled_sources", AsyncMock(return_value=[])), \
         patch("app.api.admin.database.set_disabled_sources", AsyncMock()), \
         patch("app.database.toggle_library", toggle), \
         patch("app.state.invalidate_plex_client", MagicMock()), \
         patch("app.state.trigger_catalog_refresh", MagicMock()):
        client.post("/admin/sources/jf-1/disable")
        client.post("/admin/sources/jf-1/enable")
    toggle.assert_not_awaited()   # per-library rows untouched (remembered)


def test_source_toggle_refresh_failure_does_not_500(client, mock_state):
    # Best-effort reconcile: a refresh-trigger failure must not fail the toggle; the
    # veto is already persisted (control-plane-success / best-effort-after-commit).
    setv = AsyncMock()
    with patch("app.api.admin.database.get_disabled_sources", AsyncMock(return_value=[])), \
         patch("app.api.admin.database.set_disabled_sources", setv), \
         patch("app.state.invalidate_plex_client", MagicMock()), \
         patch("app.state.trigger_catalog_refresh",
               MagicMock(side_effect=RuntimeError("boom"))):
        resp = client.post("/admin/sources/jf-1/disable")
    assert resp.status_code == 200           # did not 500
    setv.assert_awaited_once_with(["jf-1"])  # veto still persisted before the reconcile


def test_reconnect_clears_stale_veto(client, mock_state):
    # A reconnect must not inherit a stale veto (source_ids are deterministic), else
    # the source connects but stays invisible. connect_jellyfin clears it.
    setv = AsyncMock()
    with patch("app.sources.jellyfin.authenticate",
               AsyncMock(return_value={"token": "t", "user_id": "u", "server_id": "srv9"})), \
         patch("app.api.admin.database.save_jellyfin_source", AsyncMock()), \
         patch("app.api.admin.database.get_disabled_sources", AsyncMock(return_value=["jf-srv9"])), \
         patch("app.api.admin.database.set_disabled_sources", setv), \
         patch("app.state.get_plex_client", AsyncMock(return_value=None)), \
         patch("app.state.trigger_catalog_refresh", MagicMock()), \
         patch("app.state.invalidate_plex_client", MagicMock()):
        resp = client.post("/admin/sources/jellyfin", json={
            "server_url": "http://jf.local:8096", "username": "a",
            "password": "b", "name": "Den"})
    assert resp.status_code == 200
    setv.assert_awaited_once_with([])   # jf-srv9 veto removed on reconnect


def test_remove_source_clears_orphan_veto(client, mock_state):
    setv = AsyncMock()
    with patch("app.api.admin.database.delete_jellyfin_source", AsyncMock()), \
         patch("app.api.admin.database.get_disabled_sources",
               AsyncMock(return_value=["jf-1", "local-x"])), \
         patch("app.api.admin.database.set_disabled_sources", setv), \
         patch("app.state.trigger_catalog_refresh", MagicMock()), \
         patch("app.state.invalidate_plex_client", MagicMock()):
        resp = client.delete("/admin/sources/jellyfin/jf-1")
    assert resp.status_code == 200
    setv.assert_awaited_once_with(["local-x"])   # orphan veto for jf-1 dropped


def test_list_sources_fails_open_when_veto_unreadable(client, mock_state):
    # A veto-store read error must not 500 the panel — sources show enabled (U3).
    with patch("app.api.admin.database.get_plex_servers", AsyncMock(return_value=[])), \
         patch("app.api.admin.database.get_plex_config", AsyncMock(return_value=None)), \
         patch("app.api.admin.database.get_jellyfin_sources",
               AsyncMock(return_value=[{"source_id": "jf-1", "name": "Den"}])), \
         patch("app.api.admin.database.get_subsonic_sources", AsyncMock(return_value=[])), \
         patch("app.api.admin.database.get_emby_sources", AsyncMock(return_value=[])), \
         patch("app.api.admin.database.get_local_sources", AsyncMock(return_value=[])), \
         patch("app.api.admin.database.get_disabled_sources",
               AsyncMock(side_effect=RuntimeError("settings read failed"))):
        resp = client.get("/admin/sources")
    assert resp.status_code == 200
    assert resp.json()["sources"] == [{"source_id": "jf-1", "type": "jellyfin",
                                       "name": "Den", "enabled": True}]


def test_plex_poll_clears_veto_only_for_newly_added_server(client, mock_state):
    # A reconnect that adds server B must clear B's veto but leave a deliberately-
    # disabled, already-connected server A vetoed (ce-code-review 2026-07-28).
    setv = AsyncMock()
    with patch("app.api.admin.plex_oauth.complete_flow", AsyncMock(return_value=True)), \
         patch("app.api.admin.database.get_plex_servers",
               AsyncMock(side_effect=[[{"machine_id": "A"}], [{"machine_id": "A"}, {"machine_id": "B"}]])), \
         patch("app.api.admin.database.get_disabled_sources", AsyncMock(return_value=["A", "B"])), \
         patch("app.api.admin.database.set_disabled_sources", setv), \
         patch("app.api.admin._enable_new_source_libraries", AsyncMock()), \
         patch("app.state.trigger_catalog_refresh", MagicMock()), \
         patch("app.state.invalidate_plex_client", MagicMock()):
        resp = client.get("/admin/plex/connect/poll/123?client_id=cid")
    assert resp.status_code == 200
    setv.assert_awaited_once_with(["A"])   # B (newly added) cleared; A (already-connected) kept


def test_plex_poll_does_not_clear_veto_of_already_connected_server(client, mock_state):
    # Re-auth with NO new server must not touch an existing server's veto (no over-clear).
    setv = AsyncMock()
    with patch("app.api.admin.plex_oauth.complete_flow", AsyncMock(return_value=True)), \
         patch("app.api.admin.database.get_plex_servers",
               AsyncMock(side_effect=[[{"machine_id": "A"}], [{"machine_id": "A"}]])), \
         patch("app.api.admin.database.get_disabled_sources", AsyncMock(return_value=["A"])), \
         patch("app.api.admin.database.set_disabled_sources", setv), \
         patch("app.api.admin._enable_new_source_libraries", AsyncMock()), \
         patch("app.state.trigger_catalog_refresh", MagicMock()), \
         patch("app.state.invalidate_plex_client", MagicMock()):
        resp = client.get("/admin/plex/connect/poll/123?client_id=cid")
    assert resp.status_code == 200
    setv.assert_not_awaited()   # A stays vetoed — no over-clear on re-auth


def test_plex_poll_seeds_new_owned_server_libraries_and_scans(client, mock_state):
    # Fresh-install audit F1 (2026-08-06): a NEWLY connected OWNED Plex server's
    # libraries seed enabled-by-default — a single-library server rendered no
    # per-library control, so without seeding a doc-following fresh install
    # dead-ended with a permanently empty catalog. A crawl also kicks off without
    # a manual Rescan, matching the Jellyfin/local connect handlers.
    seed = AsyncMock()
    trig = MagicMock()
    with patch("app.api.admin.plex_oauth.complete_flow", AsyncMock(return_value=True)), \
         patch("app.api.admin.database.get_plex_servers",
               AsyncMock(side_effect=[[], [{"machine_id": "B", "owned": 1}]])), \
         patch("app.api.admin.database.get_disabled_sources", AsyncMock(return_value=[])), \
         patch("app.api.admin.database.set_disabled_sources", AsyncMock()), \
         patch("app.api.admin._enable_new_source_libraries", seed), \
         patch("app.state.trigger_catalog_refresh", trig), \
         patch("app.state.invalidate_plex_client", MagicMock()):
        resp = client.get("/admin/plex/connect/poll/123?client_id=cid")
    assert resp.status_code == 200
    seed.assert_awaited_once_with("B")
    trig.assert_called_once()


def test_plex_poll_never_seeds_shared_server_libraries(client, mock_state):
    # Owned-only seeding policy (2026-08-06): a friend's shared library must not
    # auto-crawl into the party catalog. Shared (owned=0) and legacy (owned=NULL)
    # servers stay opt-in via the always-rendered Libraries drill-in.
    seed = AsyncMock()
    trig = MagicMock()
    with patch("app.api.admin.plex_oauth.complete_flow", AsyncMock(return_value=True)), \
         patch("app.api.admin.database.get_plex_servers",
               AsyncMock(side_effect=[[], [{"machine_id": "S", "owned": 0},
                                           {"machine_id": "L", "owned": None}]])), \
         patch("app.api.admin.database.get_disabled_sources", AsyncMock(return_value=[])), \
         patch("app.api.admin.database.set_disabled_sources", AsyncMock()), \
         patch("app.api.admin._enable_new_source_libraries", seed), \
         patch("app.state.trigger_catalog_refresh", trig), \
         patch("app.state.invalidate_plex_client", MagicMock()):
        resp = client.get("/admin/plex/connect/poll/123?client_id=cid")
    assert resp.status_code == 200
    seed.assert_not_awaited()
    trig.assert_not_called()   # nothing seeded → no auto-crawl


def test_plex_poll_mixed_batch_seeds_only_the_owned_server(client, mock_state):
    # One connect adding an owned NAS + a shared server: only the owned one
    # seeds; the crawl fires once.
    seed = AsyncMock()
    trig = MagicMock()
    with patch("app.api.admin.plex_oauth.complete_flow", AsyncMock(return_value=True)), \
         patch("app.api.admin.database.get_plex_servers",
               AsyncMock(side_effect=[[], [{"machine_id": "OWN", "owned": 1},
                                           {"machine_id": "SHARED", "owned": 0}]])), \
         patch("app.api.admin.database.get_disabled_sources", AsyncMock(return_value=[])), \
         patch("app.api.admin.database.set_disabled_sources", AsyncMock()), \
         patch("app.api.admin._enable_new_source_libraries", seed), \
         patch("app.state.trigger_catalog_refresh", trig), \
         patch("app.state.invalidate_plex_client", MagicMock()):
        resp = client.get("/admin/plex/connect/poll/123?client_id=cid")
    assert resp.status_code == 200
    seed.assert_awaited_once_with("OWN")
    trig.assert_called_once()


def test_plex_poll_reauth_never_reseeds_libraries(client, mock_state):
    # A RE-auth (no new machine_id) must never overwrite a remembered per-library
    # selection: no seeding, no auto-crawl (fresh-install audit F1 scope guard).
    seed = AsyncMock()
    trig = MagicMock()
    with patch("app.api.admin.plex_oauth.complete_flow", AsyncMock(return_value=True)), \
         patch("app.api.admin.database.get_plex_servers",
               AsyncMock(side_effect=[[{"machine_id": "A"}], [{"machine_id": "A"}]])), \
         patch("app.api.admin.database.get_disabled_sources", AsyncMock(return_value=[])), \
         patch("app.api.admin.database.set_disabled_sources", AsyncMock()), \
         patch("app.api.admin._enable_new_source_libraries", seed), \
         patch("app.state.trigger_catalog_refresh", trig), \
         patch("app.state.invalidate_plex_client", MagicMock()):
        resp = client.get("/admin/plex/connect/poll/123?client_id=cid")
    assert resp.status_code == 200
    seed.assert_not_awaited()
    trig.assert_not_called()


# ── Surprise Me settings (2026-06-17 plan U3) ────────────────────────────────

def test_settings_rejects_invalid_surprise_mode(client, mock_state):
    """A bad source mode is rejected at the Pydantic layer (Literal) → 422."""
    resp = client.post("/admin/settings", json={"surprise_me_source_mode": "bogus"})
    assert resp.status_code == 422


def test_settings_persists_surprise_fields(client, mock_state):
    """Valid enabled + source mode both persist via set_setting (200)."""
    # surprise_me_enabled now also triggers the appearance_changed broadcast
    # (code-review #6), so stub get_setting + the broadcast so this persistence
    # test stays focused on set_setting.
    with patch("app.api.admin.database.set_setting", AsyncMock()) as ss, \
         patch("app.api.admin.database.get_setting", AsyncMock(return_value=None)), \
         patch("app.events.bus.manager.broadcast_to_all", AsyncMock()):
        resp = client.post(
            "/admin/settings",
            json={"surprise_me_enabled": False, "surprise_me_source_mode": "plex"},
        )
    assert resp.status_code == 200
    persisted = {c.args[0]: c.args[1] for c in ss.call_args_list}
    assert persisted.get("surprise_me_enabled") == "0"
    assert persisted.get("surprise_me_source_mode") == "plex"


def test_settings_rejects_invalid_surprise_diversity(client, mock_state):
    resp = client.post("/admin/settings", json={"surprise_me_diversity": "bogus"})
    assert resp.status_code == 422


def test_settings_surprise_toggle_broadcasts_appearance_event(client, mock_state):
    """Code-review #6: toggling surprise_me_enabled broadcasts an appearance_changed
    event carrying the new flag, so connected clients show/hide the dock live."""
    store: dict[str, str] = {}
    async def fake_get(key, default=None): return store.get(key, default)
    async def fake_set(key, value): store[key] = value
    events: list = []
    async def cap(ev): events.append(ev)
    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)), \
         patch("app.database.set_setting", AsyncMock(side_effect=fake_set)), \
         patch("app.events.bus.manager.broadcast_to_all", AsyncMock(side_effect=cap)):
        resp = client.post("/admin/settings", json={"surprise_me_enabled": False})
    assert resp.status_code == 200
    appearance = [e for e in events if getattr(e, "type", None) == "appearance_changed"]
    assert appearance, "toggling surprise_me_enabled must broadcast appearance_changed"
    assert appearance[0].surprise_me_enabled is False


def test_settings_persists_surprise_diversity(client, mock_state):
    with patch("app.api.admin.database.set_setting", AsyncMock()) as ss:
        resp = client.post("/admin/settings", json={"surprise_me_diversity": "album"})
    assert resp.status_code == 200
    persisted = {c.args[0]: c.args[1] for c in ss.call_args_list}
    assert persisted.get("surprise_me_diversity") == "album"


# ── Random-pick length band (2026-06-20 plan U1) ─────────────────────────────

def test_settings_persists_random_length_band(client, mock_state):
    """Valid min/max persist as integer-seconds strings (200)."""
    with patch("app.api.admin.database.set_setting", AsyncMock()) as ss, \
         patch("app.api.admin.database.get_setting", AsyncMock(return_value=None)):
        resp = client.post(
            "/admin/settings",
            json={"random_min_seconds": 30, "random_max_seconds": 600},
        )
    assert resp.status_code == 200
    persisted = {c.args[0]: c.args[1] for c in ss.call_args_list}
    assert persisted.get("random_min_seconds") == "30"
    assert persisted.get("random_max_seconds") == "600"


def test_settings_random_band_zero_is_off(client, mock_state):
    """0/0 is accepted (clearing both bounds) and persists '0'/'0'."""
    with patch("app.api.admin.database.set_setting", AsyncMock()) as ss, \
         patch("app.api.admin.database.get_setting", AsyncMock(return_value=None)):
        resp = client.post(
            "/admin/settings",
            json={"random_min_seconds": 0, "random_max_seconds": 0},
        )
    assert resp.status_code == 200
    persisted = {c.args[0]: c.args[1] for c in ss.call_args_list}
    assert persisted.get("random_min_seconds") == "0"
    assert persisted.get("random_max_seconds") == "0"


def test_settings_rejects_min_ge_max(client, mock_state):
    """A contradictory band (both active, min >= max) is rejected; nothing is written."""
    with patch("app.api.admin.database.set_setting", AsyncMock()) as ss, \
         patch("app.api.admin.database.get_setting", AsyncMock(return_value=None)):
        resp = client.post(
            "/admin/settings",
            json={"random_min_seconds": 600, "random_max_seconds": 60},
        )
    assert resp.status_code == 422
    persisted = {c.args[0] for c in ss.call_args_list}
    assert "random_min_seconds" not in persisted
    assert "random_max_seconds" not in persisted


def test_settings_rejects_negative_length(client, mock_state):
    """A negative bound is rejected and writes nothing."""
    with patch("app.api.admin.database.set_setting", AsyncMock()) as ss, \
         patch("app.api.admin.database.get_setting", AsyncMock(return_value=None)):
        resp = client.post("/admin/settings", json={"random_min_seconds": -5})
    assert resp.status_code == 422
    assert not ss.call_args_list


def test_get_settings_echoes_random_band(client, mock_state):
    """GET /settings echoes the stored band as integer seconds for the form."""
    store = {"random_min_seconds": "30", "random_max_seconds": "600"}
    async def fake_get(key, default=None): return store.get(key, default)
    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)):
        resp = client.get("/admin/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["random_min_seconds"] == 30
    assert body["random_max_seconds"] == 600


# ── International rail settings (2026-06-22 plan 004) ─────────────────────────

def test_settings_persists_international_rail(client, mock_state):
    """Alpha mode + both thresholds persist via set_setting (200)."""
    with patch("app.api.admin.database.set_setting", AsyncMock()) as ss, \
         patch("app.api.admin.database.get_setting", AsyncMock(return_value=None)):
        resp = client.post(
            "/admin/settings",
            json={"rail_alpha_mode": "international",
                  "rail_artist_threshold": 3, "rail_album_threshold": 4},
        )
    assert resp.status_code == 200
    persisted = {c.args[0]: c.args[1] for c in ss.call_args_list}
    assert persisted.get("rail_alpha_mode") == "international"
    assert persisted.get("rail_artist_threshold") == "3"
    assert persisted.get("rail_album_threshold") == "4"


def test_settings_rejects_invalid_rail_alpha_mode(client, mock_state):
    """A bad alpha mode is rejected at the Pydantic layer (Literal) → 422."""
    resp = client.post("/admin/settings", json={"rail_alpha_mode": "bogus"})
    assert resp.status_code == 422


def test_settings_rejects_rail_threshold_below_one(client, mock_state):
    """A threshold < 1 is rejected and writes nothing."""
    with patch("app.api.admin.database.set_setting", AsyncMock()) as ss, \
         patch("app.api.admin.database.get_setting", AsyncMock(return_value=None)):
        resp = client.post("/admin/settings", json={"rail_artist_threshold": 0})
    assert resp.status_code == 422
    assert "rail_artist_threshold" not in {c.args[0] for c in ss.call_args_list}


def test_get_settings_echoes_international_rail(client, mock_state):
    """GET /settings echoes the stored alpha mode + thresholds for the form."""
    store = {"rail_alpha_mode": "international",
             "rail_artist_threshold": "3", "rail_album_threshold": "4"}
    async def fake_get(key, default=None): return store.get(key, default)
    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)):
        resp = client.get("/admin/settings")
    assert resp.status_code == 200
    body = resp.json()
    assert body["rail_alpha_mode"] == "international"
    assert body["rail_artist_threshold"] == 3
    assert body["rail_album_threshold"] == 4


# ── Volume bar orientation (2026-08-04 volume rework U3) ─────────────────────

def test_settings_persists_volume_orientation(client, mock_state):
    """A valid orientation persists via set_setting (200)."""
    with patch("app.api.admin.database.set_setting", AsyncMock()) as ss, \
         patch("app.api.admin.database.get_setting", AsyncMock(return_value=None)):
        resp = client.post("/admin/settings", json={"volume_orientation": "vertical"})
    assert resp.status_code == 200
    persisted = {c.args[0]: c.args[1] for c in ss.call_args_list}
    assert persisted.get("volume_orientation") == "vertical"


def test_settings_rejects_invalid_volume_orientation(client, mock_state):
    """A bad orientation is rejected at the Pydantic layer (Literal) → 422,
    and the atomic-validation rule means nothing persists."""
    with patch("app.api.admin.database.set_setting", AsyncMock()) as ss:
        resp = client.post("/admin/settings", json={"volume_orientation": "diagonal"})
    assert resp.status_code == 422
    assert not ss.call_args_list


def test_get_settings_echoes_volume_orientation(client, mock_state):
    """GET /settings echoes the stored orientation; unset reads horizontal."""
    async def fake_get(key, default=None):
        return {"volume_orientation": "vertical"}.get(key, default)
    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)):
        resp = client.get("/admin/settings")
    assert resp.status_code == 200
    assert resp.json()["volume_orientation"] == "vertical"

    with patch("app.database.get_setting", AsyncMock(return_value=None)):
        resp = client.get("/admin/settings")
    assert resp.status_code == 200
    assert resp.json()["volume_orientation"] == "horizontal"


# ── Rating display style (2026-06-27 plan U1) ────────────────────────────────

def test_settings_persists_rating_style(client, mock_state):
    """A valid rating_style persists via set_setting (200)."""
    with patch("app.api.admin.database.set_setting", AsyncMock()) as ss, \
         patch("app.api.admin.database.get_setting", AsyncMock(return_value=None)):
        resp = client.post("/admin/settings", json={"rating_style": "dots"})
    assert resp.status_code == 200
    persisted = {c.args[0]: c.args[1] for c in ss.call_args_list}
    assert persisted.get("rating_style") == "dots"


def test_settings_rejects_invalid_rating_style(client, mock_state):
    """A bad rating_style is rejected at the Pydantic layer (Literal) → 422."""
    resp = client.post("/admin/settings", json={"rating_style": "triangles"})
    assert resp.status_code == 422


def test_get_settings_echoes_rating_style(client, mock_state):
    """GET /settings echoes the stored style; unset hydrates the radio as stars."""
    store = {"rating_style": "bars"}
    async def fake_get(key, default=None): return store.get(key, default)
    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)):
        resp = client.get("/admin/settings")
    assert resp.status_code == 200
    assert resp.json()["rating_style"] == "bars"
    with patch("app.database.get_setting", AsyncMock(return_value=None)):
        resp = client.get("/admin/settings")
    assert resp.json()["rating_style"] == "stars"


def test_settings_rating_style_broadcasts(client, mock_state):
    """Changing rating_style broadcasts an appearance_changed event carrying the
    new style, so connected clients (admin + guests) update live without a reload
    — consistent with scheme/view (reverses the original reload-only behavior)."""
    store: dict[str, str] = {}
    async def fake_get(key, default=None): return store.get(key, default)
    async def fake_set(key, value): store[key] = value
    events: list = []
    async def cap(ev): events.append(ev)
    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)), \
         patch("app.database.set_setting", AsyncMock(side_effect=fake_set)), \
         patch("app.events.bus.manager.broadcast_to_all", AsyncMock(side_effect=cap)):
        resp = client.post("/admin/settings", json={"rating_style": "bars"})
    assert resp.status_code == 200
    assert store.get("rating_style") == "bars"
    appearance = [e for e in events if getattr(e, "type", None) == "appearance_changed"]
    assert appearance, "changing rating_style must broadcast appearance_changed"
    assert appearance[0].rating_style == "bars"


def test_settings_international_rail_broadcasts_appearance_event(client, mock_state):
    """Saving the alpha-rail config broadcasts appearance_changed carrying the new
    values, so connected guests update their rail live (parity with rail_mode)."""
    store: dict[str, str] = {}
    async def fake_get(key, default=None): return store.get(key, default)
    async def fake_set(key, value): store[key] = value
    events: list = []
    async def cap(ev): events.append(ev)
    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)), \
         patch("app.database.set_setting", AsyncMock(side_effect=fake_set)), \
         patch("app.events.bus.manager.broadcast_to_all", AsyncMock(side_effect=cap)):
        resp = client.post(
            "/admin/settings",
            json={"rail_alpha_mode": "international", "rail_artist_threshold": 3},
        )
    assert resp.status_code == 200
    appearance = [e for e in events if getattr(e, "type", None) == "appearance_changed"]
    assert appearance, "saving rail_alpha_mode must broadcast appearance_changed"
    assert appearance[0].rail_alpha_mode == "international"
    assert appearance[0].rail_artist_threshold == 3


async def test_get_random_length_bounds_unset():
    """No stored band → (None, None) — fully opt-in."""
    from app import database
    with patch("app.database.get_setting", AsyncMock(return_value=None)):
        assert await database.get_random_length_bounds() == (None, None)


async def test_get_random_length_bounds_zero_is_none():
    """0 means 'bound off' on both ends."""
    from app import database
    store = {"random_min_seconds": "0", "random_max_seconds": "0"}
    async def fake_get(key, default=None): return store.get(key, default)
    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)):
        assert await database.get_random_length_bounds() == (None, None)


async def test_get_random_length_bounds_min_only():
    """min-only → (min_ms, None); seconds are converted to ms."""
    from app import database
    store = {"random_min_seconds": "30"}
    async def fake_get(key, default=None): return store.get(key, default)
    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)):
        assert await database.get_random_length_bounds() == (30000, None)


async def test_get_random_length_bounds_max_only():
    """max-only → (None, max_ms)."""
    from app import database
    store = {"random_max_seconds": "600"}
    async def fake_get(key, default=None): return store.get(key, default)
    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)):
        assert await database.get_random_length_bounds() == (None, 600000)


def test_surprise_recent_requires_auth(anon_client, mock_session):
    resp = anon_client.get("/admin/surprise/recent")
    assert resp.status_code == 401


def test_surprise_recent_reports_tally(client, mock_state):
    """The readout tallies recently-resolved sources (Covers AE6)."""
    from app.queue import surprise as sm
    sm._RECENT_SOURCES.clear()
    sm.record_source("plex_sonic")
    sm.record_source("plex_sonic")
    sm.record_source("random")
    resp = client.get("/admin/surprise/recent")
    assert resp.status_code == 200
    data = resp.json()
    assert data["tally"] == {"plex_sonic": 2, "random": 1}
    assert len(data["recent"]) == 3


# ── Queue read ────────────────────────────────────────────────────────────────

async def test_get_queue_empty(client, mock_state):
    resp = client.get("/admin/queue")
    assert resp.status_code == 200
    data = resp.json()
    assert data["queue"] == []
    assert data["history"] == []
    assert data["is_locked"] is False


async def test_get_queue_exposes_added_at_and_album_id(client, mock_state):
    """U1 (unify-queue-remove): the admin queue payload carries added_at and
    album_id per item so the shared renderer can group albums (by album_id) and
    remove them entry-based via /api/queue/undo (matches on track_id+added_at)."""
    qe, _ = mock_state
    await qe.append(make_track("t1"), bypass_lock=True)
    await qe.append(make_track("t2"), bypass_lock=True)
    items = client.get("/admin/queue").json()["queue"]
    assert len(items) == 2
    for item, engine_item in zip(items, qe.queue):
        assert item["added_at"] == engine_item.added_at
        assert item["added_at"]  # non-empty
        assert "album_id" in item  # present (may be None for albumless tracks)
        assert item["position"] is not None  # existing field intact


# ── Queue mutations ───────────────────────────────────────────────────────────

async def test_queue_clear_requires_confirmed(client, mock_state):
    resp = client.post("/admin/queue/clear", json={"confirmed": False})
    assert resp.status_code == 400


async def test_queue_clear_with_confirmed(client, mock_state):
    qe, _ = mock_state
    await qe.append(make_track("t1"), bypass_lock=True)
    resp = client.post("/admin/queue/clear", json={"confirmed": True})
    assert resp.status_code == 200
    assert len(qe.queue) == 0


async def test_queue_move(client, mock_state):
    qe, _ = mock_state
    await qe.append(make_track("t1"), bypass_lock=True)
    await qe.append(make_track("t2"), bypass_lock=True)
    await qe.append(make_track("t3"), bypass_lock=True)
    resp = client.post("/admin/queue/move", json={"from_position": 2, "to_position": 0})
    assert resp.status_code == 200
    assert qe.queue[0].track.id == "t3"


async def test_queue_move_out_of_range(client, mock_state):
    resp = client.post("/admin/queue/move", json={"from_position": 99, "to_position": 0})
    assert resp.status_code == 400


async def test_queue_remove(client, mock_state):
    qe, _ = mock_state
    await qe.append(make_track("t1"), bypass_lock=True)
    await qe.append(make_track("t2"), bypass_lock=True)
    resp = client.delete("/admin/queue/0")
    assert resp.status_code == 200
    assert len(qe.queue) == 1
    assert qe.queue[0].track.id == "t2"


async def test_queue_remove_out_of_range(client, mock_state):
    resp = client.delete("/admin/queue/99")
    assert resp.status_code == 400


async def test_queue_play_next(client, mock_state):
    qe, _ = mock_state
    await qe.append(make_track("t1"), bypass_lock=True)
    await qe.append(make_track("t2"), bypass_lock=True)
    await qe.append(make_track("t3"), bypass_lock=True)
    resp = client.post("/admin/queue/2/play-next")
    assert resp.status_code == 200
    assert qe.queue[0].track.id == "t3"


# ── Lock ──────────────────────────────────────────────────────────────────────

async def test_queue_lock(client, mock_state):
    qe, _ = mock_state
    resp = client.post("/admin/queue/lock")
    assert resp.status_code == 200
    assert qe.is_locked is True


async def test_queue_unlock(client, mock_state):
    qe, _ = mock_state
    await qe.lock()
    resp = client.post("/admin/queue/unlock")
    assert resp.status_code == 200
    assert qe.is_locked is False


# ── Playback controls ─────────────────────────────────────────────────────────

async def test_playback_pause(client, mock_state):
    qe, or_ = mock_state
    resp = client.post("/admin/playback/pause")
    assert resp.status_code == 200
    or_.pause.assert_awaited_once()


async def test_playback_resume(client, mock_state):
    qe, or_ = mock_state
    resp = client.post("/admin/playback/resume")
    assert resp.status_code == 200
    or_.resume.assert_awaited_once()


async def test_playback_resume_continues_after_closing(client, mock_state, monkeypatch):
    """Closing Time freeze active → resume clears the banner, re-arms (flag drops),
    and continues via _do_advance; it does NOT call output_router.resume()."""
    import app.state as st
    from app.events import bus as _bus
    qe, or_ = mock_state
    monkeypatch.setattr(st, "_closing_active", True)
    with patch.object(st, "_do_advance", AsyncMock()) as adv, \
         patch.object(_bus.manager, "broadcast_to_all", AsyncMock()) as bc:
        resp = client.post("/admin/playback/resume")
    assert resp.status_code == 200
    adv.assert_awaited_once()
    assert st._closing_active is False
    or_.resume.assert_not_awaited()
    ev = bc.await_args.args[0]
    assert ev.type == "closing_time" and ev.active is False


async def test_playback_resume_normal_when_not_closing(client, mock_state, monkeypatch):
    """No freeze → ordinary mid-track resume path (unchanged)."""
    import app.state as st
    qe, or_ = mock_state
    monkeypatch.setattr(st, "_closing_active", False)
    resp = client.post("/admin/playback/resume")
    assert resp.status_code == 200
    or_.resume.assert_awaited_once()


async def test_playback_skip_clears_closing(client, mock_state, monkeypatch):
    """Skipping during a Closing Time freeze restarts playback AND clears the
    banner everywhere (the admin must not stay locked behind the message)."""
    import app.state as st
    from app.events import bus as _bus
    qe, or_ = mock_state
    await qe.append(make_track("next"), bypass_lock=True)
    monkeypatch.setattr(st, "_closing_active", True)
    monkeypatch.setattr(st, "_closing_message", "Last call")
    with patch("app.state.get_plex_client", AsyncMock(return_value=MagicMock())), \
         patch("app.state._make_stream_url", return_value="http://stream/next"), \
         patch("app.database.increment_play_count", AsyncMock()), \
         patch("app.database.set_play_track_meta", AsyncMock()), \
         patch.object(_bus.manager, "broadcast_to_all", AsyncMock()) as bc:
        resp = client.post("/admin/playback/skip")
    assert resp.status_code == 200
    or_.play.assert_awaited_once()           # playback actually restarted
    assert st._closing_active is False
    cleared = [c.args[0] for c in bc.await_args_list
               if getattr(c.args[0], "type", None) == "closing_time"]
    assert cleared and cleared[-1].active is False


async def test_playback_skip_no_closing_broadcast_when_not_frozen(client, mock_state, monkeypatch):
    """No freeze → skip does not emit a closing_time clear (no spurious churn)."""
    import app.state as st
    from app.events import bus as _bus
    qe, or_ = mock_state
    await qe.append(make_track("next"), bypass_lock=True)
    monkeypatch.setattr(st, "_closing_active", False)
    with patch("app.state.get_plex_client", AsyncMock(return_value=MagicMock())), \
         patch("app.state._make_stream_url", return_value="http://stream/next"), \
         patch("app.database.increment_play_count", AsyncMock()), \
         patch("app.database.set_play_track_meta", AsyncMock()), \
         patch.object(_bus.manager, "broadcast_to_all", AsyncMock()) as bc:
        resp = client.post("/admin/playback/skip")
    assert resp.status_code == 200
    closing = [c for c in bc.await_args_list
               if getattr(c.args[0], "type", None) == "closing_time"]
    assert not closing


async def test_admin_now_playing_includes_closing_state(client, mock_state, monkeypatch):
    """U3: admin hydrates the banner from the now-playing GET while frozen."""
    import app.state as st
    monkeypatch.setattr(st, "_closing_active", True)
    monkeypatch.setattr(st, "_closing_message", "Last call")
    data = client.get("/admin/playback/now-playing").json()
    assert data["closing_active"] is True
    assert data["closing_message"] == "Last call"


async def test_admin_queue_includes_closing_state(client, mock_state, monkeypatch):
    """U3: the admin queue/state snapshot carries the same closing fields."""
    import app.state as st
    monkeypatch.setattr(st, "_closing_active", True)
    monkeypatch.setattr(st, "_closing_message", "Last call")
    data = client.get("/admin/queue").json()
    assert data["closing_active"] is True
    assert data["closing_message"] == "Last call"


async def test_playback_resume_during_hold_routes_to_manual_resume(
        client, mock_state, fresh_supervisor, monkeypatch):
    """Supervisor plan U3 (R17): Play while an outage holds the queue is the
    MANUAL RESUME — routed to the supervisor (attach + held-position play),
    never output_router.resume() (there is no live session to resume)."""
    from app.output import session
    qe, or_ = mock_state
    sup, timers, rec = fresh_supervisor
    monkeypatch.setattr(hold, "_output_hold", True)
    manual = AsyncMock(return_value=True)
    monkeypatch.setattr(sup, "manual_resume", manual)
    resp = client.post("/admin/playback/resume")
    assert resp.status_code == 200
    manual.assert_awaited_once()
    or_.resume.assert_not_awaited()


async def test_playback_resume_during_hold_unreachable_409(
        client, mock_state, fresh_supervisor, monkeypatch):
    """Manual resume while the device is still gone → 409, hold intact.
    F16 contract update: the detail is machine-readable — a
    device_unreachable code plus a message naming the device (the old static
    prose also fired for resolved landings and single-flight losses)."""
    from app.output import session
    qe, or_ = mock_state
    sup, timers, rec = fresh_supervisor
    monkeypatch.setattr(hold, "_output_hold", True)
    ot = session._Outage("connection_lost")
    ot.device_id = "dev-9"
    sup._outage = ot
    monkeypatch.setattr(sup, "manual_resume", AsyncMock(return_value=False))
    resp = client.post("/admin/playback/resume")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "device_unreachable"
    assert "dev-9" in detail["message"]
    or_.resume.assert_not_awaited()


async def test_playback_resume_during_hold_inflight_race_409_attempt_in_progress(
        client, mock_state, fresh_supervisor, monkeypatch):
    """F16: losing the single-flight race is NOT 'unreachable' — the 409
    carries attempt_in_progress so the UI can say 'try again shortly'.
    Exercises the REAL manual_resume single-flight path."""
    from app.output import session
    qe, or_ = mock_state
    sup, timers, rec = fresh_supervisor
    monkeypatch.setattr(hold, "_output_hold", True)
    ot = session._Outage("connection_lost")
    ot.backend = MagicMock()
    ot.device_id = "dev-9"
    ot.attempt_inflight = True                   # another attempt is running
    sup._outage = ot
    resp = client.post("/admin/playback/resume")
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "attempt_in_progress"
    or_.resume.assert_not_awaited()


async def test_playback_resume_during_hold_clears_closing_on_success(
        client, mock_state, fresh_supervisor, monkeypatch):
    """F2: a successful manual hold-resume restarts (or resolves) playback —
    an active Closing Time freeze must clear exactly as on the live resume
    path; the old early return left the banner frozen on every screen."""
    import app.state as st
    from app.events import bus as _bus
    from app.output import session
    qe, or_ = mock_state
    sup, timers, rec = fresh_supervisor
    monkeypatch.setattr(hold, "_output_hold", True)
    monkeypatch.setattr(sup, "manual_resume", AsyncMock(return_value=True))
    monkeypatch.setattr(st, "_closing_active", True)
    monkeypatch.setattr(st, "_closing_message", "Last call")
    with patch.object(_bus.manager, "broadcast_to_all", AsyncMock()) as bc:
        resp = client.post("/admin/playback/resume")
    assert resp.status_code == 200
    assert st._closing_active is False
    ev = bc.await_args.args[0]
    assert ev.type == "closing_time" and ev.active is False


async def test_playback_volume_during_hold_accepted_and_persisted(
        client, mock_state, fresh_supervisor, monkeypatch):
    """Supervisor plan U3 (R17): volume during a hold is accepted (200) and
    persisted via the held-volume path — never a live device write, which
    would raise against the dead output."""
    from app.output import session
    qe, or_ = mock_state
    sup, timers, rec = fresh_supervisor
    monkeypatch.setattr(hold, "_output_hold", True)
    held = AsyncMock()
    monkeypatch.setattr(session, "set_held_volume", held)
    resp = client.post("/admin/playback/volume", json={"level": 0.7})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "level": 0.7}
    held.assert_awaited_once_with(0.7)
    or_.set_volume.assert_not_awaited()


async def test_playback_skip_consumes_r19_mark(client, mock_state, fresh_supervisor):
    """Supervisor plan U3 (R19): a held item dispatched via admin Skip carries
    its play_recorded mark into the dispatch (confirm must not re-count) and
    the mark is consumed on success so a later organic replay counts."""
    qe, or_ = mock_state
    sup, timers, rec = fresh_supervisor
    item = await qe.append(make_track("held"), bypass_lock=True)
    item.play_recorded = True
    with patch("app.state.get_plex_client", AsyncMock(return_value=MagicMock())), \
         patch("app.state._make_stream_url", return_value="http://stream/held"):
        resp = client.post("/admin/playback/skip")
    assert resp.status_code == 200
    or_.play.assert_awaited_once()
    assert qe.state.current.play_recorded is False   # mark consumed
    sup.on_playback_confirmed(sup.current_token())   # dispatch carried it:
    rec.assert_not_called()                          # no double count


async def test_playback_volume(client, mock_state):
    qe, or_ = mock_state
    resp = client.post("/admin/playback/volume", json={"level": 0.8})
    assert resp.status_code == 200
    or_.set_volume.assert_awaited_once_with(0.8)


async def test_playback_volume_out_of_range(client, mock_state):
    resp = client.post("/admin/playback/volume", json={"level": 1.5})
    assert resp.status_code == 422  # pydantic validation


async def test_playback_skip_does_not_call_stop_before_play(client, mock_state):
    """playback_skip must NOT call output_router.stop() before play().

    The earlier ordering (stop → advance → play) broke the DLNA backend:
    DlnaBackend.stop() cleared _dmr, and the subsequent play() raised
    RuntimeError because the backend was torn down. The natural-EOS
    advance path in state._do_advance also goes directly to play()
    without a prior stop(); this endpoint now matches that pattern.
    """
    qe, or_ = mock_state
    await qe.append(make_track("t1"), bypass_lock=True)
    await qe.append(make_track("t2"), bypass_lock=True)
    with patch("app.state.get_plex_client", AsyncMock(return_value=MagicMock())), \
         patch("app.state._make_stream_url", return_value="http://stream/t1"):
        resp = client.post("/admin/playback/skip")
    assert resp.status_code == 200
    or_.stop.assert_not_awaited(), (
        "playback_skip must NOT call output_router.stop() when there's a "
        "next track — it broke the DLNA backend via _dmr=None teardown"
    )
    or_.play.assert_awaited_once()


async def test_playback_skip_stops_when_queue_empty(client, mock_state):
    """When advance() returns no next item (queue empty), the skip
    endpoint must call stop() to halt playback fully — there's no
    next track for play() to transition the renderer to.
    """
    qe, or_ = mock_state
    # No items in queue → advance() returns None.
    resp = client.post("/admin/playback/skip")
    assert resp.status_code == 200
    or_.stop.assert_awaited_once()
    or_.play.assert_not_awaited()


async def test_playback_skip_counts_via_confirmed_start_chokepoint(client, mock_state, fresh_supervisor):
    """Skipping forward to a track is a real play and must count — but only
    once the backend CONFIRMS playback started (2026-07-11 supervisor plan U1).
    The endpoint reports the dispatch to the output-session supervisor and no
    longer calls record_play itself; all three entry points (natural advance,
    Skip, Skip Back) share that chokepoint."""
    sup, timers, rec = fresh_supervisor
    qe, or_ = mock_state
    await qe.append(make_track("t1"), bypass_lock=True)
    await qe.append(make_track("t2"), bypass_lock=True)
    with patch("app.state.get_plex_client", AsyncMock(return_value=MagicMock())), \
         patch("app.state._make_stream_url", return_value="http://stream/t1"):
        resp = client.post("/admin/playback/skip")
        assert resp.status_code == 200
        rec.assert_not_called()                  # dispatch alone must not count
        token = sup.current_token()
        assert token is not None                 # dispatch was reported
        sup.on_playback_confirmed(token)         # backend confirms playback
    rec.assert_called_once()
    assert rec.call_args.args[0].id == "t1"      # the dispatched track counted


async def test_playback_skip_captures_track_meta_after_confirm(client, mock_state, monkeypatch):
    """Skip forward captures display metadata for the now-playing track so it
    surfaces on Most Played — via the real record_play, fired from the
    supervisor's confirmed-start chokepoint (U1), not at dispatch."""
    import asyncio as _asyncio
    from app.output import session
    sup = session.OutputSessionSupervisor(timer_factory=lambda d, cb: MagicMock())
    monkeypatch.setattr(session, "_supervisor", sup)
    qe, or_ = mock_state
    await qe.append(make_track("t1"), bypass_lock=True)
    await qe.append(make_track("t2"), bypass_lock=True)
    meta = AsyncMock()
    with patch("app.state.get_plex_client", AsyncMock(return_value=MagicMock())), \
         patch("app.state._make_stream_url", return_value="http://stream/t1"), \
         patch("app.database.increment_play_count", AsyncMock()), \
         patch("app.database.set_play_track_meta", meta):
        resp = client.post("/admin/playback/skip")
        assert resp.status_code == 200
        assert meta.await_count == 0             # nothing captured at dispatch
        sup.on_playback_confirmed(sup.current_token())
        # record_play is fire-and-forget tasks on this loop; poll briefly.
        for _ in range(100):
            if meta.await_count >= 1:
                break
            await _asyncio.sleep(0.01)
    meta.assert_awaited_once()
    tid, payload = meta.await_args.args
    assert tid == "t1"
    assert payload["title"] == "Song" and "artist" in payload


# ── Settings ──────────────────────────────────────────────────────────────────

async def test_settings_round_trip(client, mock_state):
    with patch("app.database.get_setting", AsyncMock(return_value=None)), \
         patch("app.database.set_setting", AsyncMock()):
        resp = client.get("/admin/settings")
        assert resp.status_code == 200

        resp = client.post("/admin/settings", json={
            "queue_end_behavior": "full_random",
            "queue_display_n": 5,
            "queue_display_m": 3,
        })
        assert resp.status_code == 200
        qe, _ = mock_state
        assert qe.end_behavior == QueueEndBehavior.FULL_RANDOM


async def test_settings_flood_control_round_trip(client, mock_state):
    """U1 (Flood Control): the flag defaults off, persists as "1"/"0", reads
    back as a bool, and an omitted field doesn't clobber the stored value."""
    # Default off when unset.
    with patch("app.database.get_setting", AsyncMock(return_value=None)), \
         patch("app.database.set_setting", AsyncMock()) as set_mock:
        assert client.get("/admin/settings").json()["flood_control"] is False
        # POST true → persisted as "1"; POST false → "0".
        assert client.post("/admin/settings", json={"flood_control": True}).status_code == 200
        set_mock.assert_any_call("flood_control", "1")
        client.post("/admin/settings", json={"flood_control": False})
        set_mock.assert_any_call("flood_control", "0")

    # Omitted field doesn't write flood_control at all (per-field is-not-None guard).
    with patch("app.database.get_setting", AsyncMock(return_value=None)), \
         patch("app.database.set_setting", AsyncMock()) as set_mock:
        client.post("/admin/settings", json={"queue_end_behavior": "stop"})
        assert not any(c.args and c.args[0] == "flood_control" for c in set_mock.call_args_list)

    # Stored "1" reads back as bool true.
    async def _get(key, default=None):
        return "1" if key == "flood_control" else None
    with patch("app.database.get_setting", AsyncMock(side_effect=_get)), \
         patch("app.database.set_setting", AsyncMock()):
        assert client.get("/admin/settings").json()["flood_control"] is True


async def test_settings_lyrics_contribute_round_trip(client, mock_state):
    """Lyrics contribute prompt (contribute-prompt plan U2): the flag defaults
    ON (unset → true), persists as "1"/"0", reads back as a bool, and an omitted
    field doesn't clobber the stored value."""
    # Default ON when unset.
    with patch("app.database.get_setting", AsyncMock(return_value=None)), \
         patch("app.database.set_setting", AsyncMock()) as set_mock:
        assert client.get("/admin/settings").json()["lyrics_contribute_enabled"] is True
        # POST false → persisted as "0"; POST true → "1".
        assert client.post(
            "/admin/settings", json={"lyrics_contribute_enabled": False}
        ).status_code == 200
        set_mock.assert_any_call("lyrics_contribute_enabled", "0")
        client.post("/admin/settings", json={"lyrics_contribute_enabled": True})
        set_mock.assert_any_call("lyrics_contribute_enabled", "1")

    # Omitted field doesn't write the key (per-field is-not-None guard).
    with patch("app.database.get_setting", AsyncMock(return_value=None)), \
         patch("app.database.set_setting", AsyncMock()) as set_mock:
        client.post("/admin/settings", json={"queue_end_behavior": "stop"})
        assert not any(
            c.args and c.args[0] == "lyrics_contribute_enabled"
            for c in set_mock.call_args_list
        )

    # Stored "0" reads back as bool false.
    async def _get(key, default=None):
        return "0" if key == "lyrics_contribute_enabled" else None
    with patch("app.database.get_setting", AsyncMock(side_effect=_get)), \
         patch("app.database.set_setting", AsyncMock()):
        assert client.get("/admin/settings").json()["lyrics_contribute_enabled"] is False


async def test_settings_invalid_end_behavior(client, mock_state):
    resp = client.post("/admin/settings", json={"queue_end_behavior": "invalid"})
    assert resp.status_code == 400


# ── Queue-end rework (2026-06-21 plan U2) ────────────────────────────────────

async def test_settings_popular_random_round_trip(client, mock_state):
    with patch("app.database.get_setting", AsyncMock(return_value=None)), \
         patch("app.database.set_setting", AsyncMock()):
        resp = client.post("/admin/settings", json={"queue_end_behavior": "popular_random"})
        assert resp.status_code == 200
        qe, _ = mock_state
        assert qe.end_behavior == QueueEndBehavior.POPULAR_RANDOM


def test_settings_migrates_legacy_behavior_on_read(client, mock_state):
    """AE6: a stored legacy 'shuffle'/'repeat' echoes as a current value."""
    for stored, expected in (("shuffle", "full_random"), ("repeat", "stop")):
        async def fake_get(key, default=None, _v=stored):
            return _v if key == "queue_end_behavior" else default
        with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)):
            body = client.get("/admin/settings").json()
        assert body["queue_end_behavior"] == expected


def test_settings_persists_popular_threshold(client, mock_state):
    with patch("app.api.admin.database.set_setting", AsyncMock()) as ss, \
         patch("app.api.admin.database.get_setting", AsyncMock(return_value=None)):
        resp = client.post("/admin/settings", json={"popular_random_threshold": 5})
    assert resp.status_code == 200
    persisted = {c.args[0]: c.args[1] for c in ss.call_args_list}
    assert persisted.get("popular_random_threshold") == "5"


def test_settings_rejects_threshold_below_one(client, mock_state):
    with patch("app.api.admin.database.set_setting", AsyncMock()) as ss, \
         patch("app.api.admin.database.get_setting", AsyncMock(return_value=None)):
        resp = client.post("/admin/settings", json={"popular_random_threshold": 0})
    assert resp.status_code == 422
    assert "popular_random_threshold" not in {c.args[0] for c in ss.call_args_list}


def test_get_settings_queue_end_defaults(client, mock_state):
    """Unset → threshold default 2, length-limit off."""
    with patch("app.database.get_setting", AsyncMock(return_value=None)):
        body = client.get("/admin/settings").json()
    assert body["popular_random_threshold"] == 2
    assert body["queue_end_length_limit"] is False


def test_settings_persists_most_played_display_limit(client, mock_state):
    with patch("app.api.admin.database.set_setting", AsyncMock()) as ss, \
         patch("app.api.admin.database.get_setting", AsyncMock(return_value=None)):
        resp = client.post("/admin/settings", json={"most_played_display_limit": 25})
    assert resp.status_code == 200
    persisted = {c.args[0]: c.args[1] for c in ss.call_args_list}
    assert persisted.get("most_played_display_limit") == "25"


def test_settings_rejects_display_limit_below_one(client, mock_state):
    with patch("app.api.admin.database.set_setting", AsyncMock()) as ss, \
         patch("app.api.admin.database.get_setting", AsyncMock(return_value=None)):
        resp = client.post("/admin/settings", json={"most_played_display_limit": 0})
    assert resp.status_code == 422
    assert "most_played_display_limit" not in {c.args[0] for c in ss.call_args_list}


def test_get_settings_most_played_display_limit_default(client, mock_state):
    """Unset → display limit default 100."""
    with patch("app.database.get_setting", AsyncMock(return_value=None)):
        body = client.get("/admin/settings").json()
    assert body["most_played_display_limit"] == 100


# ── Gapless settings + auto-resume window (2026-07-11 supervisor plan U5) ────

@contextlib.contextmanager
def _gapless_state_reset():
    """Save/restore app.state's live gapless flag + arming generation around a
    test (module-level state, mirrors test_state.py's _ondeck_reset shape)."""
    import app.state as st
    saved = (st._gapless_enabled, st._arming_gen)
    st._gapless_enabled, st._arming_gen = False, 0
    try:
        yield st
    finally:
        st._gapless_enabled, st._arming_gen = saved


async def test_settings_gapless_round_trip_and_live_apply(client, mock_state):
    """Plan U5: the toggle defaults off, persists as "1"/"0", reads back as a
    bool, and the live flag flips WITHOUT a restart (accessor reflects the POST
    immediately — the queue_end_behavior live-apply shape)."""
    with _gapless_state_reset() as st:
        # Default off when unset.
        with patch("app.database.get_setting", AsyncMock(return_value=None)), \
             patch("app.database.set_setting", AsyncMock()) as set_mock:
            assert client.get("/admin/settings").json()["gapless_enabled"] is False
            # POST true → persisted "1" + live flag applied without restart.
            assert client.post(
                "/admin/settings", json={"gapless_enabled": True}
            ).status_code == 200
            set_mock.assert_any_call("gapless_enabled", "1")
            assert st.gapless_enabled() is True
            # POST false → persisted "0" + live flag back off.
            client.post("/admin/settings", json={"gapless_enabled": False})
            set_mock.assert_any_call("gapless_enabled", "0")
            assert st.gapless_enabled() is False

        # Omitted field: no write, no live-flag touch (per-field guard).
        with patch("app.database.get_setting", AsyncMock(return_value=None)), \
             patch("app.database.set_setting", AsyncMock()) as set_mock:
            client.post("/admin/settings", json={"queue_end_behavior": "stop"})
            assert not any(
                c.args and c.args[0] == "gapless_enabled"
                for c in set_mock.call_args_list
            )
            assert st.gapless_enabled() is False

    # Stored "1" reads back as bool true (GET hydration).
    async def _get(key, default=None):
        return "1" if key == "gapless_enabled" else None
    with patch("app.database.get_setting", AsyncMock(side_effect=_get)), \
         patch("app.database.set_setting", AsyncMock()):
        assert client.get("/admin/settings").json()["gapless_enabled"] is True


async def test_settings_gapless_flip_bumps_arming_generation(client, mock_state):
    """Plan U5 → U6 hook: a toggle FLIP through the endpoint increments the
    arming generation (U6 keys device-side revocation off it); a same-value
    write does not bump (no revoke churn)."""
    with _gapless_state_reset() as st:
        with patch("app.database.get_setting", AsyncMock(return_value=None)), \
             patch("app.database.set_setting", AsyncMock()):
            gen0 = st.arming_gen()
            client.post("/admin/settings", json={"gapless_enabled": True})
            assert st.arming_gen() == gen0 + 1
            # Same value re-posted → no flip, no bump.
            client.post("/admin/settings", json={"gapless_enabled": True})
            assert st.arming_gen() == gen0 + 1
            # Flip back off → bump again.
            client.post("/admin/settings", json={"gapless_enabled": False})
            assert st.arming_gen() == gen0 + 2


def test_settings_persists_resume_window_minutes(client, mock_state):
    with patch("app.api.admin.database.set_setting", AsyncMock()) as ss, \
         patch("app.api.admin.database.get_setting", AsyncMock(return_value=None)):
        resp = client.post("/admin/settings", json={"resume_window_minutes": 15})
    assert resp.status_code == 200
    persisted = {c.args[0]: c.args[1] for c in ss.call_args_list}
    assert persisted.get("resume_window_minutes") == "15"


def test_settings_rejects_resume_window_below_one(client, mock_state):
    """Floor validation (plan U5): 0 and negative → 422, nothing persisted."""
    for bad in (0, -5):
        with patch("app.api.admin.database.set_setting", AsyncMock()) as ss, \
             patch("app.api.admin.database.get_setting", AsyncMock(return_value=None)):
            resp = client.post("/admin/settings", json={"resume_window_minutes": bad})
        assert resp.status_code == 422
        assert "resume_window_minutes" not in {c.args[0] for c in ss.call_args_list}


async def test_settings_mixed_invalid_request_applies_nothing(client, mock_state):
    """Validate-then-apply atomicity (2026-07-12 review C4): a mixed request
    whose resume_window_minutes fails the >= 1 floor must apply NOTHING —
    previously gapless_enabled was persisted + live-applied BEFORE the floor
    check raised, leaving the request half-applied on a 422."""
    with _gapless_state_reset() as st:
        with patch("app.api.admin.database.set_setting", AsyncMock()) as ss, \
             patch("app.api.admin.database.get_setting", AsyncMock(return_value=None)):
            resp = client.post("/admin/settings", json={
                "gapless_enabled": True, "resume_window_minutes": 0,
            })
        assert resp.status_code == 422
        assert st.gapless_enabled() is False       # live flag never applied
        assert ss.call_args_list == []             # nothing persisted at all


def test_get_settings_resume_window_default(client, mock_state):
    """Unset → resume window default 60 (the U3 accessor resolves it)."""
    with patch("app.database.get_setting", AsyncMock(return_value=None)):
        body = client.get("/admin/settings").json()
    assert body["resume_window_minutes"] == 60


async def test_get_settings_resume_window_reads_stored_value(client, mock_state):
    """Stored 15 → accessor returns 15 on the GET (decision-time read; the
    same accessor U3's supervisor consults at resume time)."""
    async def _get(key, default=None):
        return "15" if key == "resume_window_minutes" else None
    with patch("app.database.get_setting", AsyncMock(side_effect=_get)):
        assert client.get("/admin/settings").json()["resume_window_minutes"] == 15


# ── Closing Time mode (2026-06-24 plan U1) ───────────────────────────────────

def test_settings_persists_closing_time_fields(client, mock_state):
    """All four fields persist: bool as "1", strings trimmed."""
    with patch("app.api.admin.database.set_setting", AsyncMock()) as ss, \
         patch("app.api.admin.database.get_setting", AsyncMock(return_value=None)):
        resp = client.post("/admin/settings", json={
            "closing_time_enabled": True,
            "closing_time_title": "  Last Call  ",
            "closing_time_artist": " The Band ",
            "closing_time_message": "  Drink up.  ",
        })
    assert resp.status_code == 200
    persisted = {c.args[0]: c.args[1] for c in ss.call_args_list}
    assert persisted.get("closing_time_enabled") == "1"
    assert persisted.get("closing_time_title") == "Last Call"
    assert persisted.get("closing_time_artist") == "The Band"
    assert persisted.get("closing_time_message") == "Drink up."


def test_settings_closing_time_disabled_persists_zero(client, mock_state):
    with patch("app.api.admin.database.set_setting", AsyncMock()) as ss, \
         patch("app.api.admin.database.get_setting", AsyncMock(return_value=None)):
        resp = client.post("/admin/settings", json={"closing_time_enabled": False})
    assert resp.status_code == 200
    persisted = {c.args[0]: c.args[1] for c in ss.call_args_list}
    assert persisted.get("closing_time_enabled") == "0"


def test_settings_closing_time_partial_update(client, mock_state):
    """Posting only the toggle does not clobber title/artist/message."""
    with patch("app.api.admin.database.set_setting", AsyncMock()) as ss, \
         patch("app.api.admin.database.get_setting", AsyncMock(return_value=None)):
        resp = client.post("/admin/settings", json={"closing_time_enabled": True})
    assert resp.status_code == 200
    keys = {c.args[0] for c in ss.call_args_list}
    assert "closing_time_enabled" in keys
    assert "closing_time_title" not in keys
    assert "closing_time_artist" not in keys
    assert "closing_time_message" not in keys


def test_get_settings_closing_time_defaults(client, mock_state):
    """Unset → off with the Semisonic defaults."""
    async def fake_get(key, default=None):
        return {}.get(key, default)
    with patch("app.database.get_setting", fake_get):
        body = client.get("/admin/settings").json()
    assert body["closing_time_enabled"] is False
    assert body["closing_time_title"] == "Closing Time"
    assert body["closing_time_artist"] == "Semisonic"
    assert body["closing_time_message"] == "You don't have to go home, but you can't stay here."


def test_get_settings_echoes_closing_time(client, mock_state):
    """GET echoes stored values + enabled true (apostrophes intact)."""
    store = {
        "closing_time_enabled": "1",
        "closing_time_title": "Last Call",
        "closing_time_artist": "The Band",
        "closing_time_message": "You don't have to go home.",
    }
    async def fake_get(key, default=None):
        return store.get(key, default)
    with patch("app.database.get_setting", fake_get):
        body = client.get("/admin/settings").json()
    assert body["closing_time_enabled"] is True
    assert body["closing_time_title"] == "Last Call"
    assert body["closing_time_artist"] == "The Band"
    assert body["closing_time_message"] == "You don't have to go home."


def test_settings_queue_end_length_limit_round_trip(client, mock_state):
    with patch("app.api.admin.database.set_setting", AsyncMock()) as ss, \
         patch("app.api.admin.database.get_setting", AsyncMock(return_value=None)):
        assert client.post(
            "/admin/settings", json={"queue_end_length_limit": True}
        ).status_code == 200
        ss.assert_any_call("queue_end_length_limit", "1")
        client.post("/admin/settings", json={"queue_end_length_limit": False})
        ss.assert_any_call("queue_end_length_limit", "0")
    async def _get(key, default=None):
        return "1" if key == "queue_end_length_limit" else None
    with patch("app.database.get_setting", AsyncMock(side_effect=_get)):
        assert client.get("/admin/settings").json()["queue_end_length_limit"] is True


async def test_get_popular_random_threshold_accessor():
    from app import database
    for raw, expected in (("5", 5), ("0", 1), ("-3", 1), ("abc", 2), (None, 2)):
        with patch("app.database.get_setting", AsyncMock(return_value=raw)):
            assert await database.get_popular_random_threshold() == expected


async def test_get_queue_end_length_limit_accessor():
    from app import database
    for raw, expected in (("1", True), ("0", False), (None, False), ("x", False)):
        with patch("app.database.get_setting", AsyncMock(return_value=raw)):
            assert await database.get_queue_end_length_limit() is expected


# ── On-deck pre-buffer invalidation (2026-06-21 plan U4) ─────────────────────

async def test_settings_change_invalidates_ondeck(client, mock_state):
    """AE4: changing any selection input invalidates the on-deck buffer."""
    with patch("app.database.get_setting", AsyncMock(return_value=None)), \
         patch("app.database.set_setting", AsyncMock()), \
         patch("app.state.invalidate_ondeck", AsyncMock()) as inv:
        for field in (
            {"queue_end_behavior": "full_random"},
            {"popular_random_threshold": 3},
            {"random_min_seconds": 30},
            {"queue_end_length_limit": True},
        ):
            inv.reset_mock()
            resp = client.post("/admin/settings", json=field)
            assert resp.status_code == 200
            inv.assert_awaited_once()


async def test_unrelated_setting_does_not_invalidate_ondeck(client, mock_state):
    with patch("app.database.get_setting", AsyncMock(return_value=None)), \
         patch("app.database.set_setting", AsyncMock()), \
         patch("app.state.invalidate_ondeck", AsyncMock()) as inv:
        resp = client.post("/admin/settings", json={"rail_mode": "vanilla"})
        assert resp.status_code == 200
        inv.assert_not_awaited()


async def test_enable_library_invalidates_ondeck(client, mock_state):
    from types import SimpleNamespace
    fake_client = MagicMock()
    fake_client.get_libraries = AsyncMock(return_value=[SimpleNamespace(key="5", title="Music")])
    fake_client.invalidate_cache = MagicMock()
    with patch("app.state.get_plex_client", AsyncMock(return_value=fake_client)), \
         patch("app.database.toggle_library", AsyncMock()), \
         patch("app.state.invalidate_ondeck", AsyncMock()) as inv:
        resp = client.post("/admin/plex/libraries/5/enable")
        assert resp.status_code == 200
        inv.assert_awaited_once()


async def test_disable_library_invalidates_ondeck(client, mock_state):
    fake_client = MagicMock()
    fake_client.invalidate_cache = MagicMock()
    with patch("app.state.get_plex_client", AsyncMock(return_value=fake_client)), \
         patch("app.database.toggle_library", AsyncMock()), \
         patch("app.state.invalidate_ondeck", AsyncMock()) as inv:
        resp = client.post("/admin/plex/libraries/5/disable")
        assert resp.status_code == 200
        inv.assert_awaited_once()


# ── Browse index rescan / invalidation / status (2026-06-21 plan U6) ─────────

async def test_rescan_triggers_browse_index_refresh(client, mock_state):
    # Covers AE4 (manual Rescan rebuilds the index).
    fake_client = MagicMock()
    fake_client.invalidate_cache = MagicMock()
    fake_client.get_libraries = AsyncMock(return_value=[])
    with patch("app.state.get_plex_client", AsyncMock(return_value=fake_client)), \
         patch("app.database.get_enabled_libraries", AsyncMock(return_value=[])), \
         patch("app.state.trigger_genre_refresh", MagicMock()), \
         patch("app.state.trigger_credit_refresh", MagicMock()), \
         patch("app.state.trigger_browse_index_refresh", MagicMock()) as trig:
        resp = client.post("/admin/plex/rescan")
    assert resp.status_code == 200
    trig.assert_called_once()


async def test_enable_library_triggers_browse_index_refresh(client, mock_state):
    # Covers AE4 (library toggle refreshes the index, R9).
    from types import SimpleNamespace
    fake_client = MagicMock()
    fake_client.get_libraries = AsyncMock(return_value=[SimpleNamespace(key="5", title="Music")])
    fake_client.invalidate_cache = MagicMock()
    with patch("app.state.get_plex_client", AsyncMock(return_value=fake_client)), \
         patch("app.database.toggle_library", AsyncMock()), \
         patch("app.state.invalidate_ondeck", AsyncMock()), \
         patch("app.state.trigger_browse_index_refresh", MagicMock()) as trig:
        resp = client.post("/admin/plex/libraries/5/enable")
    assert resp.status_code == 200
    trig.assert_called_once()


async def test_disable_library_triggers_browse_index_refresh(client, mock_state):
    fake_client = MagicMock()
    fake_client.invalidate_cache = MagicMock()
    with patch("app.state.get_plex_client", AsyncMock(return_value=fake_client)), \
         patch("app.database.toggle_library", AsyncMock()), \
         patch("app.state.invalidate_ondeck", AsyncMock()), \
         patch("app.state.trigger_browse_index_refresh", MagicMock()) as trig:
        resp = client.post("/admin/plex/libraries/5/disable")
    assert resp.status_code == 200
    trig.assert_called_once()


async def test_index_status_reports_stamp(client, mock_state):
    with patch("app.database.get_setting", AsyncMock(return_value="2026-06-21T10:00:00+00:00")), \
         patch("app.state._browse_index_refresh_running", False):
        resp = client.get("/admin/plex/index-status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["computed_at"] == "2026-06-21T10:00:00+00:00"
    assert body["building"] is False


async def test_index_status_never_built(client, mock_state):
    with patch("app.database.get_setting", AsyncMock(return_value=None)):
        resp = client.get("/admin/plex/index-status")
    assert resp.status_code == 200
    assert resp.json()["computed_at"] is None


async def test_set_pattern_rules_triggers_grouping_rebuild(client, mock_state):
    # Rule-grouping plan U2/R6: saving rules rebuilds the grouping map locally
    # (no Plex re-crawl) via the single mutation path.
    with patch("app.database.set_pattern_rules", AsyncMock()) as setrules, \
         patch("app.state.trigger_artist_grouping_rebuild", MagicMock()) as trig:
        resp = client.post("/admin/pattern-rules", json={"rules": [["a", "b"]]})
    assert resp.status_code == 200
    setrules.assert_awaited_once()
    trig.assert_called_once()


# ── Rail mode setting (U1 of rail-mode-toggle plan) ───────────────────────────

async def test_settings_rail_mode_density_write_rejected(client, mock_state):
    """Glow-up plan U1 (R3): density retired - writes 422 at the Literal."""
    resp = client.post("/admin/settings", json={"rail_mode": "density"})
    assert resp.status_code == 422


async def test_settings_legacy_density_reads_as_waveform(client, mock_state):
    """Covers AE6 (admin read edge): stored 'density' hydrates as waveform."""
    async def fake_get(key, default=None):
        return {"rail_mode": "density"}.get(key, default)

    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)):
        get_resp = client.get("/admin/settings")
        assert get_resp.status_code == 200
        assert get_resp.json()["rail_mode"] == "waveform"


async def test_settings_rail_mode_magnetic_persists(client, mock_state):
    """POST /admin/settings with rail_mode='magnetic' persists; GET returns it."""
    store: dict[str, str] = {}

    async def fake_get(key, default=None):
        return store.get(key, default)

    async def fake_set(key, value):
        store[key] = value

    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)), \
         patch("app.database.set_setting", AsyncMock(side_effect=fake_set)):
        resp = client.post("/admin/settings", json={"rail_mode": "magnetic"})
        assert resp.status_code == 200
        assert store["rail_mode"] == "magnetic"
        get_resp = client.get("/admin/settings")
        assert get_resp.json()["rail_mode"] == "magnetic"


async def test_settings_rail_mode_invalid_returns_422(client, mock_state):
    """Pydantic Literal validation rejects unknown rail_mode values with 422."""
    resp = client.post("/admin/settings", json={"rail_mode": "invalid"})
    assert resp.status_code == 422


async def test_settings_rail_mode_default_vanilla(client, mock_state):
    """When the rail_mode key is absent, GET defaults to 'vanilla' (2026-06-09
    rail plan R7 — fresh installs get the plain rail; stored values preserved)."""
    with patch("app.database.get_setting", AsyncMock(return_value=None)):
        resp = client.get("/admin/settings")
        assert resp.status_code == 200
        assert resp.json()["rail_mode"] == "vanilla"


# ── Output ────────────────────────────────────────────────────────────────────

async def test_output_devices_returns_grouped(client, mock_state):
    resp = client.get("/admin/output/devices")
    assert resp.status_code == 200
    assert isinstance(resp.json(), dict)


async def test_set_output_invalid_backend(client, mock_state):
    with patch("app.state.activate_backend", AsyncMock()):
        resp = client.post("/admin/output/active", json={"backend_type": "bluetooth"})
        assert resp.status_code in (400, 422)  # Pydantic Literal rejects unknown values with 422


async def test_set_output_device_not_found_returns_409(client, mock_state):
    """RuntimeError from activate_backend (device not in D-Bus map) yields 409, not 500."""
    with patch("app.state.activate_backend", AsyncMock(side_effect=RuntimeError("device not found"))):
        resp = client.post("/admin/output/active", json={"backend_type": "chromecast", "device_id": "dead-uuid"})
    assert resp.status_code == 409
    assert "device not found" in resp.json()["detail"]


async def test_set_output_library_error_returns_502(client, mock_state):
    """Non-RuntimeError from activate_backend yields 502, not 500."""
    with patch("app.state.activate_backend", AsyncMock(side_effect=OSError("connection refused"))):
        resp = client.post("/admin/output/active", json={"backend_type": "chromecast", "device_id": "dead-uuid"})
    assert resp.status_code == 502


async def test_set_output_device_id_too_long_returns_422(client, mock_state):
    """device_id longer than 128 chars is rejected with 422."""
    resp = client.post("/admin/output/active", json={"backend_type": "chromecast", "device_id": "x" * 129})
    assert resp.status_code == 422


async def test_get_output_active_includes_mdns_status(client, mock_state):
    """GET /admin/output/active response includes mdns_status field."""
    with patch("app.database.get_setting", AsyncMock(return_value=None)):
        resp = client.get("/admin/output/active")
    assert resp.status_code == 200
    assert "mdns_status" in resp.json()


# ── Libraries serializer: per-source-type indicator (ce-debug 2026-06-29) ─────

def test_serialize_libraries_emits_source_type_and_server_name():
    from app.api.admin import _serialize_libraries
    libs = [
        Library(key="m1:1", title="Music", type="artist", owner="Alice",
                server_name="Home", source_type="plex"),
        Library(key="jf:9", title="Music", type="artist",
                server_name="Den", source_type="jellyfin"),
    ]
    out = _serialize_libraries(libs, {"m1:1"})
    assert out[0]["source_type"] == "plex"
    assert out[0]["server_name"] == "Home"
    assert out[0]["enabled"] is True
    assert out[1]["source_type"] == "jellyfin"
    assert out[1]["server_name"] == "Den"
    assert out[1]["enabled"] is False  # only m1:1 enabled


# ── Admin queue append bypasses lock (U2) ─────────────────────────────────────

def _make_plex_client(tracks=None):
    pc = MagicMock()
    pc.get_track = AsyncMock(return_value=make_track("t1"))
    pc.get_libraries = AsyncMock(return_value=[Library(key="1", title="Music", type="artist", server_name="Music")])
    pc.get_tracks = AsyncMock(return_value=tracks or [make_track("t1"), make_track("t2")])
    # Mocks for _resolve_album_tracks chain (U1 of the unification plan): the
    # endpoint now calls get_album → get_artists → get_albums → get_tracks for
    # the album branch. Defaults align so the single-library happy path resolves.
    pc.get_album = AsyncMock(return_value=make_album())
    pc.get_artists = AsyncMock(return_value=[make_artist()])
    pc.get_albums = AsyncMock(return_value=[make_album()])
    pc.invalidate_cache = MagicMock()
    return pc


def _two_libs():
    """Library 0 (host, machineA) + Library 1 (shared, machineB) with server_names set."""
    return [
        Library(key="machineA:1", title="Host", type="artist", server_name="Host"),
        Library(key="machineB:2", title="Shared", type="artist", server_name="Shared"),
    ]


def _wire_two_library_album(plex, *, host_tracks, shared_tracks):
    """Wire a plex mock so 'Album A' by 'Prince' exists in both _two_libs() libraries
    with the given per-library track lists. Mirrors tests/test_api_guest.py's pattern."""
    plex.get_album = AsyncMock(return_value=Album(
        id="machineA:100", title="Album A", artist="Prince",
    ))

    async def fake_get_artists(section_key):
        if section_key == "machineA:1":
            return [Artist(id="machineA:42", title="Prince")]
        if section_key == "machineB:2":
            return [Artist(id="machineB:78", title="Prince")]
        return []

    async def fake_get_albums(section_key, artist_id=None, **kw):
        if section_key == "machineA:1":
            return [Album(id="machineA:100", title="Album A", artist="Prince")]
        if section_key == "machineB:2":
            return [Album(id="machineB:200", title="Album A", artist="Prince")]
        return []

    async def fake_get_tracks(section_key, album_id=None, **kw):
        if section_key == "machineA:1" and album_id == "machineA:100":
            return host_tracks
        if section_key == "machineB:2" and album_id == "machineB:200":
            return shared_tracks
        return []

    plex.get_artists.side_effect = fake_get_artists
    plex.get_albums.side_effect = fake_get_albums
    plex.get_tracks.side_effect = fake_get_tracks


async def test_admin_append_track_bypasses_lock(client, mock_state):
    qe, _ = mock_state
    await qe.lock()
    plex = _make_plex_client()
    with patch("app.state.get_plex_client", AsyncMock(return_value=plex)), \
         patch("app.database.get_enabled_libraries", AsyncMock(return_value=[{"section_key": "1"}])):
        resp = client.post("/admin/queue", json={"track_id": "t1"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert len(qe.queue) == 1


async def test_admin_append_duplicate_exempt_from_flood_control(client, mock_state):
    """Flood Control (2026-06-16 plan U2): the admin add path never consults
    flood_control. With the toggle on AND the track already in the queue, the
    admin add still succeeds (admin can always add duplicates, mirroring how
    admin bypasses the queue lock). Guests-only is the whole point of the gate."""
    qe, _ = mock_state
    await qe.append(make_track("t1"), bypass_lock=True)  # t1 already queued
    plex = _make_plex_client()
    with patch("app.state.get_plex_client", AsyncMock(return_value=plex)), \
         patch("app.database.get_enabled_libraries", AsyncMock(return_value=[{"section_key": "1"}])), \
         patch("app.database.get_setting", AsyncMock(return_value="1")):  # FC ON
        resp = client.post("/admin/queue", json={"track_id": "t1"})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert len(qe.queue) == 2  # duplicate added anyway


async def test_admin_append_album_bypasses_lock(client, mock_state):
    qe, _ = mock_state
    await qe.lock()
    plex = _make_plex_client(tracks=[make_track("t1"), make_track("t2")])
    with patch("app.state.get_plex_client", AsyncMock(return_value=plex)), \
         patch("app.database.get_enabled_libraries", AsyncMock(return_value=[{"section_key": "1"}])):
        resp = client.post("/admin/queue", json={"album_id": "a1"})
    assert resp.status_code == 200
    assert resp.json()["tracks_added"] == 2
    assert len(qe.queue) == 2


async def test_admin_append_no_id_returns_400(client, mock_state):
    plex = _make_plex_client()
    with patch("app.state.get_plex_client", AsyncMock(return_value=plex)):
        resp = client.post("/admin/queue", json={})
    assert resp.status_code == 400


async def test_admin_append_no_plex_returns_503(client, mock_state):
    resp = client.post("/admin/queue", json={"track_id": "t1"})
    assert resp.status_code == 503


# ── Admin queue source_server_name filter (U1 of unification plan) ────────────


async def test_admin_append_album_no_source_filter_unions_libraries(client, mock_state):
    """Backward-compat: POST {album_id} with no source_server_name enqueues
    the union of all matching libraries' tracks (post-multi-library behavior
    matching what guest already does on /api/queue)."""
    qe, _ = mock_state
    plex = _make_plex_client()
    _wire_two_library_album(
        plex,
        host_tracks=[make_track("machineA:t1"), make_track("machineA:t2")],
        shared_tracks=[make_track("machineB:t1")],
    )
    with patch("app.state.get_plex_client", AsyncMock(return_value=plex)), \
         patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        resp = client.post("/admin/queue", json={"album_id": "machineA:100"})
    assert resp.status_code == 200
    assert resp.json()["tracks_added"] == 3
    assert len(qe.queue) == 3


async def test_admin_append_album_with_source_filter_scopes_to_one_library(client, mock_state):
    """Covers AE2 (admin half). source_server_name='Shared' enqueues only
    Shared library's tracks; tracks_added reflects the filtered count."""
    qe, _ = mock_state
    plex = _make_plex_client()
    _wire_two_library_album(
        plex,
        host_tracks=[make_track("machineA:t1"), make_track("machineA:t2")],
        shared_tracks=[make_track("machineB:t1")],
    )
    with patch("app.state.get_plex_client", AsyncMock(return_value=plex)), \
         patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        resp = client.post("/admin/queue", json={
            "album_id": "machineA:100",
            "source_server_name": "Shared",
        })
    assert resp.status_code == 200
    assert resp.json()["tracks_added"] == 1
    assert len(qe.queue) == 1
    assert qe.queue[0].track.id == "machineB:t1"


async def test_admin_append_album_with_source_filter_includes_bonus_tracks(client, mock_state):
    """Covers AE3 (admin half). Host=2 tracks, Shared=3 tracks with bonus.
    Filtering to 'Shared' enqueues all 3; no Host tracks queued."""
    qe, _ = mock_state
    plex = _make_plex_client()
    _wire_two_library_album(
        plex,
        host_tracks=[make_track("machineA:t1"), make_track("machineA:t2")],
        shared_tracks=[
            make_track("machineB:t1"),
            make_track("machineB:t2"),
            make_track("machineB:bonus"),
        ],
    )
    with patch("app.state.get_plex_client", AsyncMock(return_value=plex)), \
         patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        resp = client.post("/admin/queue", json={
            "album_id": "machineA:100",
            "source_server_name": "Shared",
        })
    assert resp.status_code == 200
    assert resp.json()["tracks_added"] == 3
    queued_ids = [i.track.id for i in qe.queue]
    assert "machineB:bonus" in queued_ids
    assert all(tid.startswith("machineB:") for tid in queued_ids)


async def test_admin_append_track_ignores_source_server_name(client, mock_state):
    """Track branch: source_server_name is accepted but ignored (track_id
    already identifies the library; admin still bypasses the lock)."""
    qe, _ = mock_state
    plex = _make_plex_client()
    with patch("app.state.get_plex_client", AsyncMock(return_value=plex)):
        resp = client.post("/admin/queue", json={
            "track_id": "t1",
            "source_server_name": "Anything",
        })
    assert resp.status_code == 200
    assert resp.json()["tracks_added"] == 1
    assert len(qe.queue) == 1


async def test_admin_append_album_source_filter_no_match_returns_404(client, mock_state):
    """source_server_name doesn't match any enabled library → 404."""
    plex = _make_plex_client()
    _wire_two_library_album(
        plex,
        host_tracks=[make_track("machineA:t1")],
        shared_tracks=[make_track("machineB:t1")],
    )
    with patch("app.state.get_plex_client", AsyncMock(return_value=plex)), \
         patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        resp = client.post("/admin/queue", json={
            "album_id": "machineA:100",
            "source_server_name": "Nonexistent",
        })
    assert resp.status_code == 404


async def test_admin_append_album_source_filter_case_insensitive(client, mock_state):
    """source_server_name match is case-insensitive after trim."""
    qe, _ = mock_state
    plex = _make_plex_client()
    _wire_two_library_album(
        plex,
        host_tracks=[make_track("machineA:t1")],
        shared_tracks=[make_track("machineB:t1")],
    )
    with patch("app.state.get_plex_client", AsyncMock(return_value=plex)), \
         patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        resp = client.post("/admin/queue", json={
            "album_id": "machineA:100",
            "source_server_name": "host",
        })
    assert resp.status_code == 200
    assert qe.queue[0].track.id == "machineA:t1"


async def test_admin_append_album_get_album_keyerror_returns_404(client, mock_state):
    """get_album raising KeyError → 404 (matches /api/queue's behavior)."""
    plex = _make_plex_client()
    plex.get_album = AsyncMock(side_effect=KeyError("not found"))
    with patch("app.state.get_plex_client", AsyncMock(return_value=plex)), \
         patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        resp = client.post("/admin/queue", json={
            "album_id": "machineA:9999",
            "source_server_name": "Host",
        })
    assert resp.status_code == 404


# ── Backend parity: /api/queue vs /admin/queue equivalence ────────────────────


async def test_guest_admin_queue_endpoints_parity(client, mock_state):
    """Both endpoints accept the same request body shape and produce equivalent
    observable behavior (same tracks_added, same enqueued track IDs) for an
    album with source_server_name. Catches future endpoint divergence that the
    unified frontend would otherwise mask."""
    qe, _ = mock_state
    plex = _make_plex_client()
    _wire_two_library_album(
        plex,
        host_tracks=[make_track("machineA:t1")],
        shared_tracks=[make_track("machineB:t1")],
    )

    # Round 1: POST to /admin/queue.
    with patch("app.state.get_plex_client", AsyncMock(return_value=plex)), \
         patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        admin_resp = client.post("/admin/queue", json={
            "album_id": "machineA:100",
            "source_server_name": "Shared",
        })
    admin_queued = [i.track.id for i in qe.queue]
    admin_count = admin_resp.json()["tracks_added"]

    # Reset queue for guest round.
    while qe.queue:
        await qe.remove(0)
    # Re-wire mocks (gather() consumed side_effect iterators in some versions —
    # the AsyncMock side_effects defined as `async def` callables are idempotent,
    # so this is a no-op; explicit re-set is defensive).
    _wire_two_library_album(
        plex,
        host_tracks=[make_track("machineA:t1")],
        shared_tracks=[make_track("machineB:t1")],
    )

    # Round 2: POST same body to /api/queue.
    with patch("app.state.get_plex_client", AsyncMock(return_value=plex)), \
         patch("app.api.guest.enabled_libraries", AsyncMock(return_value=_two_libs())):
        guest_resp = client.post("/api/queue", json={
            "album_id": "machineA:100",
            "source_server_name": "Shared",
        })
    guest_queued = [i.track.id for i in qe.queue]
    guest_count = guest_resp.json()["tracks_added"]

    assert admin_resp.status_code == 200
    assert guest_resp.status_code == 200
    assert admin_count == guest_count == 1
    assert admin_queued == guest_queued == ["machineB:t1"]


# ── Plex rescan (U3) ──────────────────────────────────────────────────────────

async def test_plex_rescan_returns_library_list(client, mock_state):
    plex = _make_plex_client()
    with patch("app.state.get_plex_client", AsyncMock(return_value=plex)), \
         patch("app.database.get_enabled_libraries", AsyncMock(return_value=[{"section_key": "1"}])):
        resp = client.post("/admin/plex/rescan")
    assert resp.status_code == 200
    plex.invalidate_cache.assert_called_once()
    data = resp.json()
    assert isinstance(data, list)
    assert data[0]["key"] == "1"


async def test_plex_rescan_no_plex_returns_503(client, mock_state):
    resp = client.post("/admin/plex/rescan")
    assert resp.status_code == 503


# ── Discovery serving model (U5: registry snapshot vs legacy pull) ───────────
# KTD7 removed the route's 30s HTTP cache: while the watcher runs, GET
# serves the live registry snapshot (no network I/O); without it, the
# legacy pull flow scans on every call. The fixtures below install a
# running watcher singleton the way the lifespan would — conftest's
# mock_state resets the singleton between tests.


def _install_running_watcher(mdns=None):
    """A DeviceWatcher singleton with running=True for route tests.

    Subscription handles are faked (no avahi here) and the injected timer
    swallows the debounce scheduling, so the snapshot/reconcile path runs
    without live timers or the websocket manager."""
    import app.output.watcher as watcher_module
    w = watcher_module.DeviceWatcher(timer=lambda delay, cb: MagicMock())
    w._handles = {"airplay": object(), "chromecast": object()}
    if mdns:
        w._mdns_status.update(mdns)
    watcher_module._watcher = w
    return w


def _entry(device, online=True, offline_since=None):
    from app.output.watcher import RegistryEntry
    return RegistryEntry(device=device, online=online,
                         offline_since=offline_since)


def _airplay_device(host="10.0.0.9", port=7000, name="WiiM"):
    from app.output.base import OutputDevice
    return OutputDevice(id=f"{host}:{port}", name=name,
                        backend_type="airplay", id_format="host_port")


def _stub_airplay(*devices):
    """Backend stub shaped like AirPlayBackend's address cache: host_for
    resolves through _device_addr; the one-shot returns nothing unless a
    test overrides discover_devices."""
    ab = MagicMock()
    ab._device_addr = {
        d.id: (d.name, d.id.rsplit(":", 1)[0], int(d.id.rsplit(":", 1)[1]), {})
        for d in devices
    }
    ab.discover_devices = AsyncMock(return_value=[])
    return ab


async def test_legacy_pull_scans_on_every_get(client, mock_state):
    """No watcher (or degraded) → the legacy pull flow runs the one-shot
    discovers on EVERY call; the old 30s response cache is gone (KTD7 —
    while the watcher runs the registry IS the cache)."""
    scan_count = []

    async def counting_discover():
        scan_count.append(1)
        return []

    with patch("app.state.direct_backend") as db, \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.dlna_backend") as lb, \
         patch("app.state.airplay_backend") as ab:
        db.discover_devices = counting_discover
        cb.discover_devices = counting_discover
        lb.discover_devices = counting_discover
        ab.discover_devices = counting_discover

        client.get("/admin/output/devices")
        client.get("/admin/output/devices")

    assert len(scan_count) == 8, \
        f"Expected 8 scan calls (2 GETs × 4 backends, no cache), got {len(scan_count)}"


async def test_degraded_watcher_falls_back_to_legacy_pull(client, mock_state):
    """A watcher that exists but isn't running (avahi absent → degraded
    mode) must not capture the route: the legacy pull flow serves,
    byte-compatible with the no-watcher path (origin R5)."""
    import app.output.watcher as watcher_module
    watcher_module._watcher = watcher_module.DeviceWatcher(
        timer=lambda delay, cb: MagicMock())  # no handles → running=False

    scan_count = []

    async def counting_discover():
        scan_count.append(1)
        return []

    with patch("app.state.direct_backend") as db, \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.dlna_backend") as lb, \
         patch("app.state.airplay_backend") as ab, \
         patch("app.state.shared_aiozc", object()):  # in-process mDNS bound
        db.discover_devices = counting_discover
        cb.discover_devices = counting_discover
        lb.discover_devices = counting_discover
        ab.discover_devices = counting_discover
        resp = client.get("/admin/output/devices")

    assert len(scan_count) == 4
    payload = resp.json()
    assert payload["devices"] == []
    assert payload["mdns_status"] == {
        "direct": "ok", "airplay": "ok", "chromecast": "ok", "dlna": "ok",
        "discovery": "ok"}


async def test_snapshot_path_serves_registry_without_discover(client, mock_state):
    """KTD7 fast path: watcher running → GET serves the registry snapshot
    with NO one-shot discovers, and mdns_status comes from the watcher."""
    dev = _airplay_device()
    watcher = _install_running_watcher(mdns={"chromecast": "unavailable"})
    watcher.registry[("airplay", dev.id)] = _entry(dev)

    with patch("app.state.airplay_backend", _stub_airplay(dev)) as ab, \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.dlna_backend") as lb:
        cb.discover_devices = AsyncMock(return_value=[])
        lb.discover_devices = AsyncMock(return_value=[])
        resp = client.get("/admin/output/devices")

    assert ab.discover_devices.await_count == 0
    assert cb.discover_devices.await_count == 0
    assert lb.discover_devices.await_count == 0
    payload = resp.json()
    assert [d["host"] for d in payload["devices"]] == ["10.0.0.9"]
    assert payload["devices"][0]["online"] is True
    assert payload["devices"][0]["protocols"][0]["device_id"] == dev.id
    assert payload["mdns_status"]["chromecast"] == "unavailable"
    assert payload["mdns_status"]["airplay"] == "ok"


async def test_snapshot_path_offline_entry_present_with_flag(client, mock_state):
    """An offline-retained registry ghost stays IN the GET payload,
    flagged online=False with its offline_since stamp — greyed out in
    the picker, never vanishing (origin R2)."""
    dev = _airplay_device()
    watcher = _install_running_watcher()
    watcher.registry[("airplay", dev.id)] = _entry(
        dev, online=False, offline_since=1234.5)

    with patch("app.state.airplay_backend", _stub_airplay(dev)):
        resp = client.get("/admin/output/devices")

    devices = resp.json()["devices"]
    assert len(devices) == 1
    assert devices[0]["online"] is False
    assert devices[0]["offline_since"] == 1234.5


async def test_force_scan_drops_still_absent_offline_ghosts(client, mock_state):
    """Scan (?bust=1) is the only eviction path (origin R6): an offline
    ghost the one-shots still can't see is dropped from the registry and
    the response."""
    dev = _airplay_device()
    watcher = _install_running_watcher()
    watcher.registry[("airplay", dev.id)] = _entry(
        dev, online=False, offline_since=1234.5)

    with patch("app.state.airplay_backend", _stub_airplay(dev)), \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.dlna_backend") as lb:
        cb.snapshot_devices = MagicMock(return_value=[])
        lb.discover_devices = AsyncMock(return_value=[])
        resp = client.get("/admin/output/devices?bust=1")

    assert ("airplay", dev.id) not in watcher.registry
    assert resp.json()["devices"] == []


async def test_force_scan_keeps_reappeared_ghost_online(client, mock_state):
    """Scan finding a retained ghost again flips it back online instead
    of dropping it — reconcile upserts found devices."""
    dev = _airplay_device()
    watcher = _install_running_watcher()
    watcher.registry[("airplay", dev.id)] = _entry(
        dev, online=False, offline_since=1234.5)
    ab = _stub_airplay(dev)
    ab.discover_devices = AsyncMock(return_value=[dev])

    with patch("app.state.airplay_backend", ab), \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.dlna_backend") as lb:
        cb.snapshot_devices = MagicMock(return_value=[])
        lb.discover_devices = AsyncMock(return_value=[])
        resp = client.get("/admin/output/devices?bust=1")

    entry = watcher.registry[("airplay", dev.id)]
    assert entry.online is True
    assert entry.offline_since is None
    devices = resp.json()["devices"]
    assert devices[0]["online"] is True
    assert devices[0]["offline_since"] is None


async def test_force_scan_uses_chromecast_snapshot_when_5353_bound(client, mock_state):
    """With 5353 bound (in-process source live), Scan's chromecast one-shot
    reconciles from the live CastBrowser snapshot (uuid-keyed), NOT
    discover_devices and NOT the D-Bus fallback."""
    _install_running_watcher()

    with patch("app.state.airplay_backend") as ab, \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.dlna_backend") as lb, \
         patch("app.state._mdns_port_unavailable", False):
        ab.discover_devices = AsyncMock(return_value=[])
        ab._device_addr = {}
        cb.snapshot_devices = MagicMock(return_value=[])
        cb._dbus_discover = AsyncMock(return_value=[])
        cb.discover_devices = AsyncMock(return_value=[])
        lb.discover_devices = AsyncMock(return_value=[])
        client.get("/admin/output/devices?bust=1")

    assert cb.snapshot_devices.call_count == 1
    assert cb._dbus_discover.await_count == 0


async def test_force_scan_uses_chromecast_dbus_when_degraded(client, mock_state):
    """When 5353 is unavailable (host avahi owns it), Scan's chromecast one-shot
    uses the avahi/D-Bus fallback (_dbus_discover), not the CastBrowser snapshot."""
    _install_running_watcher()

    with patch("app.state.airplay_backend") as ab, \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.dlna_backend") as lb, \
         patch("app.state._mdns_port_unavailable", True):
        ab.discover_devices = AsyncMock(return_value=[])
        ab._device_addr = {}
        cb._dbus_discover = AsyncMock(return_value=[])
        cb.snapshot_devices = MagicMock(return_value=[])
        cb.discover_devices = AsyncMock(return_value=[])
        lb.discover_devices = AsyncMock(return_value=[])
        client.get("/admin/output/devices?bust=1")

    assert cb._dbus_discover.await_count == 1
    assert cb.snapshot_devices.call_count == 0
    assert cb.discover_devices.await_count == 0


async def test_force_scan_never_evicts_scan_missed_online_device_ktd9(
        client, mock_state):
    """KTD9: a device the live subscription says is online survives a
    one-shot window that missed it — entry retained AND its AirPlay
    address still resolves (the merged _device_addr was not cleared), so
    the device stays in the payload with its host."""
    dev = _airplay_device()
    watcher = _install_running_watcher()
    watcher.registry[("airplay", dev.id)] = _entry(dev)  # online
    ab = _stub_airplay(dev)  # one-shot returns [] — a missed window

    with patch("app.state.airplay_backend", ab), \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.dlna_backend") as lb:
        cb.snapshot_devices = MagicMock(return_value=[])
        lb.discover_devices = AsyncMock(return_value=[])
        resp = client.get("/admin/output/devices?bust=1")

    assert ("airplay", dev.id) in watcher.registry
    assert watcher.registry[("airplay", dev.id)].online is True
    devices = resp.json()["devices"]
    assert [d["host"] for d in devices] == ["10.0.0.9"]
    assert dev.id in ab._device_addr


async def test_force_scan_avahi_down_reconciles_dlna_only_ae4(client, mock_state):
    """AE4: avahi outage (watcher mdns_status unavailable) → Scan still
    runs and reconciles the DLNA one-shot, but the mDNS one-shots' empty
    results are NOT treated as evidence — offline mDNS ghosts survive."""
    from app.output.base import OutputDevice
    ghost = _airplay_device()
    dlna_dev = OutputDevice(id="uuid:dlna-1", name="Renderer",
                            backend_type="dlna")
    watcher = _install_running_watcher(
        mdns={"airplay": "unavailable", "chromecast": "unavailable"})
    watcher.registry[("airplay", ghost.id)] = _entry(
        ghost, online=False, offline_since=99.0)

    with patch("app.state.airplay_backend", _stub_airplay(ghost)), \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.dlna_backend") as lb:
        # avahi down: the D-Bus one-shots return [] without raising.
        cb.snapshot_devices = MagicMock(return_value=[])
        lb.discover_devices = AsyncMock(return_value=[dlna_dev])
        lb._device_locations = {"uuid:dlna-1": "http://10.0.0.77:49152/desc.xml"}
        resp = client.get("/admin/output/devices?bust=1")

    # DLNA reconciled in, mDNS ghost untouched.
    assert watcher.registry[("dlna", "uuid:dlna-1")].online is True
    assert ("airplay", ghost.id) in watcher.registry
    by_host = {d["host"]: d for d in resp.json()["devices"]}
    assert by_host["10.0.0.77"]["online"] is True
    assert by_host["10.0.0.9"]["online"] is False
    assert resp.json()["mdns_status"]["airplay"] == "unavailable"


async def test_force_scan_failed_one_shot_keeps_ghosts_and_flags_status(
        client, mock_state):
    """A one-shot that RAISES is excluded from the reconcile (its ghosts
    survive) and overlays 'unavailable' on the response's mdns_status
    even while the watcher itself still reports the bus as ok."""
    ghost = _airplay_device()
    watcher = _install_running_watcher()  # watcher view: all ok
    watcher.registry[("airplay", ghost.id)] = _entry(
        ghost, online=False, offline_since=99.0)
    ab = _stub_airplay(ghost)
    ab.discover_devices = AsyncMock(side_effect=RuntimeError("scan failed"))

    with patch("app.state.airplay_backend", ab), \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.dlna_backend") as lb:
        cb.snapshot_devices = MagicMock(return_value=[])
        lb.discover_devices = AsyncMock(return_value=[])
        resp = client.get("/admin/output/devices?bust=1")

    assert ("airplay", ghost.id) in watcher.registry
    assert resp.json()["mdns_status"]["airplay"] == "unavailable"


async def test_get_output_active_mdns_status_reads_watcher(client, mock_state):
    """/output/active sources mdns_status from the running watcher (the
    old answer read the deleted route cache); without one it defaults to
    all-ok (legacy)."""
    with patch("app.database.get_setting", AsyncMock(return_value=None)), \
         patch("app.state.shared_aiozc", object()):  # in-process mDNS bound
        resp = client.get("/admin/output/active")
        assert resp.json()["mdns_status"] == {
            "direct": "ok", "airplay": "ok", "chromecast": "ok", "dlna": "ok",
            "discovery": "ok"}

        _install_running_watcher(mdns={"airplay": "unavailable"})
        resp = client.get("/admin/output/active")
        assert resp.json()["mdns_status"]["airplay"] == "unavailable"


# ── mdns_status and ?bust=1 (U5) ─────────────────────────────────────────────

def _with_mdns_state(*, port_unavailable, cc_ok=False, ap_ok=False, dbus_reachable=None):
    """Patch the mDNS port-availability flag. The _dbus_* flags were retired
    in plan U7 (no avahi/D-Bus fallback); the extra kwargs are accepted for
    call-site compatibility but ignored."""
    import app.state as st
    return patch.object(st, "_mdns_port_unavailable", port_unavailable)


async def test_mdns_status_all_ok_when_every_backend_returns(client, mock_state):
    """Every backend's discover_devices completes without raising →
    per-backend mdns_status is all 'ok'. Zero devices returned is NOT
    an unavailability — an empty network is just empty."""
    with patch("app.state.direct_backend") as db, \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.dlna_backend") as lb, \
         patch("app.state.airplay_backend") as ab, \
         patch("app.state.shared_aiozc", object()):  # in-process mDNS bound
        db.discover_devices = AsyncMock(return_value=[])
        cb.discover_devices = AsyncMock(return_value=[])
        lb.discover_devices = AsyncMock(return_value=[])
        ab.discover_devices = AsyncMock(return_value=[])
        resp = client.get("/admin/output/devices")

    status = resp.json()["mdns_status"]
    assert status == {"direct": "ok", "airplay": "ok", "chromecast": "ok", "dlna": "ok",
                      "discovery": "ok"}


async def test_devices_payload_flags_discovery_unavailable_when_degraded(
        client, mock_state):
    """U6 AE5: no in-process mDNS (shared_aiozc None, watcher degraded) → the
    devices payload's mdns_status.discovery is 'unavailable', which drives the
    host-networking banner instead of a silent empty menu."""
    with patch("app.state.direct_backend") as db, \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.dlna_backend") as lb, \
         patch("app.state.airplay_backend") as ab, \
         patch("app.state.shared_aiozc", None):  # 5353 never bound
        for m in (db, cb, lb, ab):
            m.discover_devices = AsyncMock(return_value=[])
        resp = client.get("/admin/output/devices")

    assert resp.json()["mdns_status"]["discovery"] == "unavailable"


async def test_devices_payload_discovery_ok_when_watcher_running(client, mock_state):
    """U6: a running watcher with live subscription handles reports in_process
    'ok' (healthy discovery → no banner)."""
    _install_running_watcher()  # handles present → running
    resp = client.get("/admin/output/devices")
    assert resp.json()["mdns_status"]["discovery"] == "ok"


async def test_mdns_status_chromecast_unavailable_when_scan_raises(client, mock_state):
    """A backend whose discover_devices() raises → its slot reports
    'unavailable'; the other backends are unaffected. The frontend
    surfaces this through the partial-coverage banner."""
    with patch("app.state.direct_backend") as db, \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.dlna_backend") as lb, \
         patch("app.state.airplay_backend") as ab:
        db.discover_devices = AsyncMock(return_value=[])
        cb.discover_devices = AsyncMock(side_effect=RuntimeError("scan failed"))
        lb.discover_devices = AsyncMock(return_value=[])
        ab.discover_devices = AsyncMock(return_value=[])
        resp = client.get("/admin/output/devices")

    status = resp.json()["mdns_status"]
    assert status["chromecast"] == "unavailable"
    assert status["direct"] == "ok"
    assert status["airplay"] == "ok"
    assert status["dlna"] == "ok"


async def test_mdns_status_multiple_unavailable_when_multiple_raise(client, mock_state):
    """Multiple backends failing → each reports 'unavailable' independently.
    The frontend banner concatenates the names."""
    with patch("app.state.direct_backend") as db, \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.dlna_backend") as lb, \
         patch("app.state.airplay_backend") as ab:
        db.discover_devices = AsyncMock(return_value=[])
        cb.discover_devices = AsyncMock(side_effect=RuntimeError("cc failed"))
        lb.discover_devices = AsyncMock(side_effect=RuntimeError("dlna failed"))
        ab.discover_devices = AsyncMock(return_value=[])
        resp = client.get("/admin/output/devices")

    status = resp.json()["mdns_status"]
    assert status["chromecast"] == "unavailable"
    assert status["dlna"] == "unavailable"
    assert status["airplay"] == "ok"
    assert status["direct"] == "ok"


async def test_mdns_status_backend_missing_reports_unavailable(client, mock_state):
    """A backend that isn't wired up in state (None) reports 'unavailable'
    rather than crashing the discovery route."""
    with patch("app.state.direct_backend") as db, \
         patch("app.state.chromecast_backend", None), \
         patch("app.state.dlna_backend") as lb, \
         patch("app.state.airplay_backend") as ab:
        db.discover_devices = AsyncMock(return_value=[])
        lb.discover_devices = AsyncMock(return_value=[])
        ab.discover_devices = AsyncMock(return_value=[])
        resp = client.get("/admin/output/devices")

    status = resp.json()["mdns_status"]
    assert status["chromecast"] == "unavailable"


async def test_bust_param_scans_on_legacy_path(client, mock_state):
    """?bust=1 without a running watcher runs the legacy forced scan
    (one one-shot per backend, fresh response — no cache to bypass)."""
    scan_count = []

    async def counting_discover():
        scan_count.append(1)
        return []

    with _with_mdns_state(port_unavailable=False, cc_ok=False, ap_ok=False), \
         patch("app.state.direct_backend") as db, \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.dlna_backend") as lb, \
         patch("app.state.airplay_backend") as ab:
        db.discover_devices = counting_discover
        cb.discover_devices = counting_discover
        lb.discover_devices = counting_discover
        ab.discover_devices = counting_discover
        client.get("/admin/output/devices?bust=1")

    assert len(scan_count) == 4, "bust=1 must run every backend's one-shot"


# ── U5: aggregated payload + per-device Via persistence ──────────────────────


async def test_output_devices_returns_flat_aggregated_list(client, mock_state):
    """Response shape: `devices` is a flat list of per-physical-device
    records (host, name, protocols) — not the old per-backend dict.
    Each protocol entry carries the verified state for the picker."""
    from app.output.base import OutputDevice

    with patch("app.state.direct_backend") as db, \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.dlna_backend") as lb, \
         patch("app.state.airplay_backend") as ab:
        db.discover_devices = AsyncMock(return_value=[
            OutputDevice(id="default", name="System Audio", backend_type="direct"),
        ])
        cb.discover_devices = AsyncMock(return_value=[])
        lb.discover_devices = AsyncMock(return_value=[])
        ab.discover_devices = AsyncMock(return_value=[])
        resp = client.get("/admin/output/devices")

    payload = resp.json()
    assert isinstance(payload["devices"], list)
    assert len(payload["devices"]) == 1
    direct = payload["devices"][0]
    assert direct["host"] == "__direct__"
    assert direct["name"] == "System Audio"
    assert direct["protocols"][0]["backend"] == "direct"
    assert direct["protocols"][0]["verified"] is True
    # KTD8: the payload carries availability; on the legacy pull path
    # every discovered device is online with no offline stamp.
    assert direct["online"] is True
    assert direct["offline_since"] is None


async def test_output_devices_bust_does_not_clear_verdict_cache(client, mock_state):
    """bust=true RE-PROBES (overwriting verdicts as new probes complete)
    but does NOT call clear_all_verdicts.

    The original design cleared first so re-discovered devices got fresh
    probes against the current network state. In practice it produced a
    stuck-"Checking…" loop: bust cleared verdicts, response returned
    immediately with everything verified=None, probes wrote new verdicts
    async — but the frontend never re-fetched, so the operator stayed
    looking at Checking… across every rescan click. The right invariant
    is 'overwrite on probe completion', not 'wipe-then-re-probe'."""
    clear_mock = AsyncMock()
    with patch("app.output.probe_cache.clear_all_verdicts", clear_mock), \
         patch("app.output.probe_cache.fetch_all", AsyncMock(return_value={})), \
         patch("app.state.direct_backend") as db, \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.dlna_backend") as lb, \
         patch("app.state.airplay_backend") as ab:
        db.discover_devices = AsyncMock(return_value=[])
        cb.discover_devices = AsyncMock(return_value=[])
        lb.discover_devices = AsyncMock(return_value=[])
        ab.discover_devices = AsyncMock(return_value=[])
        client.get("/admin/output/devices?bust=1")

    clear_mock.assert_not_awaited()


async def test_output_devices_no_bust_does_not_clear_cache(client, mock_state):
    """Without bust=true, verdicts persist across the scan (the picker
    relies on this for 'verified across sessions')."""
    clear_mock = AsyncMock()
    with patch("app.output.probe_cache.clear_all_verdicts", clear_mock), \
         patch("app.output.probe_cache.fetch_all", AsyncMock(return_value={})), \
         patch("app.state.direct_backend") as db, \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.dlna_backend") as lb, \
         patch("app.state.airplay_backend") as ab:
        db.discover_devices = AsyncMock(return_value=[])
        cb.discover_devices = AsyncMock(return_value=[])
        lb.discover_devices = AsyncMock(return_value=[])
        ab.discover_devices = AsyncMock(return_value=[])
        client.get("/admin/output/devices")

    clear_mock.assert_not_awaited()


async def test_output_devices_bust_reschedules_every_entry(client, mock_state):
    """bust=true probes every (host, backend) regardless of current verdict.
    The non-bust default only probes entries with verified=None (so
    already-verified protocols don't re-probe on every page load)."""
    import asyncio
    from app.output.base import OutputDevice
    from app.output.probe_cache import Verdict

    # Existing verdict for AirPlay on the device — bust should still
    # re-probe it; non-bust should skip it.
    verdicts = {("192.168.1.50", "airplay"): Verdict(ok=True, checked_at=1.0)}

    probed_targets = []

    async def fake_probe(self, device_id):
        probed_targets.append((self.backend_name, device_id))
        return True

    with patch("app.output.probe_cache.fetch_all", AsyncMock(return_value=verdicts)), \
         patch("app.state.direct_backend") as db, \
         patch("app.state.dlna_backend") as lb, \
         patch("app.state.chromecast_backend") as cb, \
         patch("app.state.airplay_backend") as ab:
        db.discover_devices = AsyncMock(return_value=[])
        lb.discover_devices = AsyncMock(return_value=[])
        cb.discover_devices = AsyncMock(return_value=[])
        ab.discover_devices = AsyncMock(return_value=[
            OutputDevice(id="192.168.1.50:7000", name="WiiM",
                         backend_type="airplay", id_format="host_port"),
        ])
        ab._device_addr = {"192.168.1.50:7000": ("WiiM", "192.168.1.50", 7000, {})}
        ab.probe_device = AsyncMock(return_value=True)

        # Non-bust call: device's existing verdict means we should NOT re-probe.
        client.get("/admin/output/devices")
        # Drain any background tasks the route scheduled.
        await asyncio.sleep(0.01)
        non_bust_calls = ab.probe_device.await_count
        ab.probe_device.reset_mock()

        # Bust call: must re-probe even though the verdict is already true.
        client.get("/admin/output/devices?bust=1")
        await asyncio.sleep(0.01)
        bust_calls = ab.probe_device.await_count

    assert non_bust_calls == 0, "verified entries must not re-probe on default cycles"
    assert bust_calls == 1, "bust=true must re-probe verified entries"


async def test_set_output_active_persists_host_and_via(client, mock_state):
    """POST /output/active with a `host` field writes both `output_host`
    (canonical active-host signal) and `device_via:{host}` (per-device
    Via preference for next-session restore). Without host, neither
    setting is written."""
    settings: dict[str, str] = {}

    async def fake_set(k, v):
        settings[k] = str(v)

    with patch("app.state.activate_backend", AsyncMock()) as ab, \
         patch("app.database.set_setting", side_effect=fake_set):
        resp = client.post(
            "/admin/output/active",
            json={"backend_type": "chromecast", "device_id": "uuid-1",
                  "host": "192.168.1.50"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["host"] == "192.168.1.50"
    # activate_backend got the host kwarg.
    ab.assert_awaited_once_with("chromecast", "uuid-1", host="192.168.1.50")


async def test_set_output_active_without_host_passes_none(client, mock_state):
    """Backwards-compat: an Apply call from a legacy client (no `host`
    field) doesn't crash — activate_backend just gets host=None and the
    setting writes are skipped at the state.py layer."""
    with patch("app.state.activate_backend", AsyncMock()) as ab:
        resp = client.post(
            "/admin/output/active",
            json={"backend_type": "direct", "device_id": "default"},
        )

    assert resp.status_code == 200
    ab.assert_awaited_once_with("direct", "default", host=None)


async def test_get_output_active_returns_host_and_via(client, mock_state):
    """GET /output/active surfaces persisted `host` and `via` so the
    frontend can pre-select both dropdowns on page load before discovery
    completes — works after a server restart with a cold cache."""
    settings = {
        "output_backend_type": "chromecast",
        "output_device_id": "uuid-1",
        "output_host": "192.168.1.50",
        "device_via:192.168.1.50": "chromecast",
    }

    async def fake_get(key, default=None):
        return settings.get(key, default)

    with patch("app.database.get_setting", side_effect=fake_get):
        resp = client.get("/admin/output/active")

    body = resp.json()
    assert body["host"] == "192.168.1.50"
    assert body["via"] == "chromecast"
    assert body["backend_type"] == "chromecast"
    assert body["device_id"] == "uuid-1"


async def test_get_output_active_handles_missing_host_gracefully(client, mock_state):
    """No `output_host` persisted (clean install) → host and via are null;
    the frontend falls back to defaults."""
    async def fake_get(key, default=None):
        return default  # always return the default (i.e. None)

    with patch("app.database.get_setting", side_effect=fake_get):
        resp = client.get("/admin/output/active")

    body = resp.json()
    assert body["host"] is None
    assert body["via"] is None


# ── U4: POST /admin/playback/no-audio ────────────────────────────────────────


def _make_airplay_backend(device_id: str = "d1"):
    """Build a minimal AirPlayBackend with a populated discovery cache so
    isinstance() checks succeed and `_device_id` resolves to a real value."""
    from app.output.airplay import AirPlayBackend
    backend = AirPlayBackend()
    backend._device_id = device_id
    backend._device_addr[device_id] = ("JBL", "192.168.1.20", 7000, {"pk": "x"})
    return backend


def test_no_audio_persists_ap1_and_restarts_via_cliraop(client, mock_state):
    """Covers F3. Active AirPlay session → POST /admin/playback/no-audio →
    `airplay:protocol:<device_id> = ap1` persisted, output_router.stop()
    called, output_router.play() called to respawn (cliraop now selected
    because of the cached ap1), AirPlayProtocolChangedEvent broadcast."""
    qe, or_ = mock_state
    backend = _make_airplay_backend("d1")
    or_.active = backend

    # Seed the queue with a current track directly (bypass the async
    # set_playing path — sync TestClient can't easily drive it).
    from app.queue.models import QueueItem
    track = make_track("t1")
    qe._current = QueueItem(track=track)

    persisted: dict[str, str] = {}
    broadcasts: list = []

    async def _set_setting(key, value):
        persisted[key] = value

    async def _broadcast(event):
        broadcasts.append(event)

    with patch("app.database.set_setting", side_effect=_set_setting), \
         patch("app.events.bus.manager.broadcast_to_admins", side_effect=_broadcast), \
         patch("app.state.get_plex_client", AsyncMock(return_value=MagicMock())), \
         patch("app.state._make_stream_url", return_value="http://stream/t1"):
        resp = client.post("/admin/playback/no-audio")

    assert resp.status_code == 200
    body = resp.json()
    assert body["device_id"] == "d1"
    assert body["protocol"] == "ap1"
    assert persisted == {"airplay:protocol:d1": "ap1"}
    assert or_.stop.await_count >= 1
    assert or_.play.await_count >= 1
    assert len(broadcasts) == 1
    assert broadcasts[0].device_id == "d1"
    assert broadcasts[0].protocol == "ap1"


def test_no_audio_returns_400_when_active_backend_not_airplay(client, mock_state):
    """A Chromecast/DLNA/Direct active backend → endpoint refuses with 400.
    The button is hidden in UI for these backends; the server also refuses
    so a curl/manual POST can't corrupt state."""
    qe, or_ = mock_state
    # Use a plain mock that is NOT an AirPlayBackend instance.
    or_.active = MagicMock(name="ChromecastBackend")
    resp = client.post("/admin/playback/no-audio")
    assert resp.status_code == 400


def test_no_audio_returns_400_when_no_current_track(client, mock_state):
    """No track currently in the queue's `current` slot → no-audio has
    nothing to restart. Refuse with 400 so the UI surfaces an error
    rather than silently spawning a fresh session with a stale track."""
    qe, or_ = mock_state
    backend = _make_airplay_backend("d1")
    or_.active = backend
    # qe.state.current is None by default (fresh QueueEngine).
    resp = client.post("/admin/playback/no-audio")
    assert resp.status_code == 400


def test_no_audio_requires_auth(anon_client, mock_session):
    """Unauthenticated callers get 401 like every other admin endpoint."""
    resp = anon_client.post("/admin/playback/no-audio")
    assert resp.status_code == 401


# ── U5: POST /admin/output/devices/{device_id}/retest-ap2 ────────────────────


def test_retest_ap2_reruns_probe_and_returns_new_verdict(client, mock_state):
    """Covers F4. Endpoint awaits the probe synchronously and returns
    the new verdict. The probe coroutine internally persists and
    broadcasts; the endpoint just relays the result."""
    backend = _make_airplay_backend("d1")

    async def _fake_probe(self, device_id, name, host, port, txt, *, force=False):
        assert force is True  # Re-test must bypass the cache
        return "ap2"

    with patch("app.state.airplay_backend", backend), \
         patch("app.output.airplay.AirPlayBackend._probe_device", _fake_probe):
        resp = client.post("/admin/output/devices/d1/retest-ap2")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"device_id": "d1", "protocol": "ap2"}


def test_retest_ap2_returns_404_for_unknown_device(client, mock_state):
    """Device id not in the AirPlay backend's discovery cache → 404.
    Prevents the endpoint from probing arbitrary device IDs."""
    backend = _make_airplay_backend("d1")
    # Empty the discovery cache to simulate "unknown device".
    backend._device_addr.clear()

    with patch("app.state.airplay_backend", backend):
        resp = client.post("/admin/output/devices/unknown/retest-ap2")
    assert resp.status_code == 404


def test_retest_ap2_returns_503_when_airplay_backend_unavailable(client, mock_state):
    """If the AirPlay backend itself is missing (binaries not installed,
    init failed), Re-test can't proceed. Return 503 rather than 500."""
    with patch("app.state.airplay_backend", None):
        resp = client.post("/admin/output/devices/d1/retest-ap2")
    assert resp.status_code == 503


def test_retest_ap2_requires_auth(anon_client, mock_session):
    resp = anon_client.post("/admin/output/devices/d1/retest-ap2")
    assert resp.status_code == 401


def test_retest_ap2_returns_409_for_currently_playing_active_device(
    client, mock_state
):
    """Refuse Re-test against the actively-playing speaker. Probing it
    would spawn a competing cliap2 session on the same speaker and
    disrupt the active stream. User must stop playback first."""
    backend = _make_airplay_backend("d1")
    backend._is_playing = True
    qe, or_ = mock_state
    or_.active = backend

    with patch("app.state.airplay_backend", backend):
        resp = client.post("/admin/output/devices/d1/retest-ap2")
    assert resp.status_code == 409
    assert "currently-playing" in resp.json()["detail"]


def test_retest_ap2_proceeds_when_active_device_is_not_playing(
    client, mock_state
):
    """If the device is the active output but not currently playing,
    Re-test is safe — no live RTSP session to disrupt."""
    backend = _make_airplay_backend("d1")
    backend._is_playing = False  # selected as output but idle
    qe, or_ = mock_state
    or_.active = backend

    async def _fake_probe(self, device_id, name, host, port, txt, *, force=False):
        return "ap2"

    with patch("app.state.airplay_backend", backend), \
         patch("app.output.airplay.AirPlayBackend._probe_device", _fake_probe):
        resp = client.post("/admin/output/devices/d1/retest-ap2")
    assert resp.status_code == 200
    assert resp.json() == {"device_id": "d1", "protocol": "ap2"}



async def test_settings_rail_mode_vanilla_round_trips(client, mock_state):
    """The third rail mode (2026-06-09 rail plan R5/R7) is accepted and
    persisted; the settings GET returns it back."""
    store: dict[str, str] = {}

    async def fake_set(key, value):
        store[key] = value

    async def fake_get(key):
        return store.get(key)

    with patch("app.database.set_setting", AsyncMock(side_effect=fake_set)), \
         patch("app.database.get_setting", AsyncMock(side_effect=fake_get)):
        resp = client.post("/admin/settings", json={"queue_end_behavior": "stop",
                                                    "rail_mode": "vanilla"})
        assert resp.status_code == 200
        resp = client.get("/admin/settings")
        assert resp.status_code == 200
        assert resp.json()["rail_mode"] == "vanilla"


# ── pattern rules + artist exclusions (2026-06-10 pattern-rules plan U4) ─────

def test_pattern_rules_require_auth(anon_client, mock_session):
    assert anon_client.get("/admin/pattern-rules").status_code == 401
    assert anon_client.post("/admin/pattern-rules", json={"rules": []}).status_code == 401


def test_artist_exclusions_require_auth(anon_client, mock_session):
    assert anon_client.get("/admin/artist-exclusions").status_code == 401


async def test_pattern_rules_round_trip_preserves_order_and_inert(client, mock_state):
    rules = [["&", "and"], ["'", ""], ["e", "é", "è"]]  # middle rule inert
    stored = {}

    async def fake_set(r): stored["rules"] = r
    async def fake_get(): return stored.get("rules", [])

    with patch("app.database.set_pattern_rules", AsyncMock(side_effect=fake_set)), \
         patch("app.database.get_pattern_rules", AsyncMock(side_effect=fake_get)):
        resp = client.post("/admin/pattern-rules", json={"rules": rules})
        assert resp.status_code == 200
        got = client.get("/admin/pattern-rules").json()
    # Inert rules round-trip unmodified — only the public endpoint filters.
    assert got == {"rules": rules}


async def test_artist_exclusions_round_trip(client, mock_state):
    stored = {}

    async def fake_set(n): stored["names"] = n
    async def fake_get(): return stored.get("names", [])

    with patch("app.database.set_artist_exclusions", AsyncMock(side_effect=fake_set)), \
         patch("app.database.get_artist_exclusions", AsyncMock(side_effect=fake_get)):
        resp = client.post("/admin/artist-exclusions", json={"names": ["[dialogue]"]})
        assert resp.status_code == 200
        assert client.get("/admin/artist-exclusions").json() == {"names": ["[dialogue]"]}


async def test_pattern_rules_caps_rejected(client, mock_state):
    too_many_strings = [["x"] * 21]
    assert client.post("/admin/pattern-rules", json={"rules": too_many_strings}).status_code == 422
    too_long = [["a" * 201, "b"]]
    assert client.post("/admin/pattern-rules", json={"rules": too_long}).status_code == 422
    too_many_rules = [["a", "b"]] * 101
    assert client.post("/admin/pattern-rules", json={"rules": too_many_rules}).status_code == 422


async def test_artist_exclusions_caps_rejected(client, mock_state):
    assert client.post("/admin/artist-exclusions", json={"names": ["x" * 201]}).status_code == 422
    assert client.post("/admin/artist-exclusions", json={"names": ["a"] * 501}).status_code == 422


# ── appearance defaults + broadcast (2026-06-11 glow-up plan U1) ──────────────

async def test_settings_default_scheme_persists_and_broadcasts(client, mock_state):
    store: dict[str, str] = {}

    async def fake_get(key, default=None):
        return store.get(key, default)

    async def fake_set(key, value):
        store[key] = value

    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)), \
         patch("app.database.set_setting", AsyncMock(side_effect=fake_set)), \
         patch("app.events.bus.manager.broadcast_to_all", AsyncMock()) as bc:
        resp = client.post("/admin/settings", json={
            "default_scheme": "king-crimson", "rail_mode": "vu",
        })
        assert resp.status_code == 200
        assert store["default_scheme"] == "king-crimson"
        assert store["rail_mode"] == "vu"
        assert bc.await_count == 1
        ev = bc.await_args.args[0]
        assert ev.to_json() == {
            "type": "appearance_changed", "scheme": "king-crimson", "rail_mode": "vu",
            "view": "list", "surprise_me_enabled": True,
            "rail_alpha_mode": "english",
            "rail_artist_threshold": 2, "rail_album_threshold": 2,
            "rating_style": "stars",
        }
        assert client.get("/admin/settings").json()["default_scheme"] == "king-crimson"


async def test_settings_unknown_scheme_rejected(client, mock_state):
    resp = client.post("/admin/settings", json={"default_scheme": "hot-dog-stand"})
    assert resp.status_code == 422


async def test_settings_default_view_persists_and_broadcasts(client, mock_state):
    """Tile-view U1: POST default_view='tile' persists, GET returns it, and the
    appearance_changed broadcast carries view alongside scheme/rail_mode."""
    store: dict[str, str] = {}

    async def fake_get(key, default=None):
        return store.get(key, default)

    async def fake_set(key, value):
        store[key] = value

    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)), \
         patch("app.database.set_setting", AsyncMock(side_effect=fake_set)), \
         patch("app.events.bus.manager.broadcast_to_all", AsyncMock()) as bc:
        resp = client.post("/admin/settings", json={"default_view": "tile"})
        assert resp.status_code == 200
        assert store["default_view"] == "tile"
        assert bc.await_count == 1
        ev = bc.await_args.args[0]
        assert ev.to_json()["view"] == "tile"
        assert client.get("/admin/settings").json()["default_view"] == "tile"


async def test_settings_default_view_invalid_returns_422(client, mock_state):
    """Pydantic Literal rejects unknown view values."""
    resp = client.post("/admin/settings", json={"default_view": "mosaic"})
    assert resp.status_code == 422


async def test_settings_default_view_default_list(client, mock_state):
    """When default_view is unset, GET /admin/settings defaults to 'list'."""
    with patch("app.database.get_setting", AsyncMock(return_value=None)):
        resp = client.get("/admin/settings")
        assert resp.status_code == 200
        assert resp.json()["default_view"] == "list"


async def test_settings_non_appearance_save_does_not_broadcast(client, mock_state):
    with patch("app.database.set_setting", AsyncMock()), \
         patch("app.events.bus.manager.broadcast_to_all", AsyncMock()) as bc:
        resp = client.post("/admin/settings", json={"queue_display_n": 5})
        assert resp.status_code == 200
        assert bc.await_count == 0


# ── Track ratings + tags authoring (2026-06-26 ratings-and-tags plan U2) ─────

def test_track_rating_requires_auth(anon_client, mock_session):
    resp = anon_client.post("/admin/track-rating", json={"track_id": "t1", "stars": 4})
    assert resp.status_code == 401


def test_track_tags_requires_auth(anon_client, mock_session):
    resp = anon_client.post("/admin/track-tags", json={"track_id": "t1", "tags": ["x"]})
    assert resp.status_code == 401


def test_set_rating_persists(client, mock_state):
    with patch("app.api.admin.database.set_rating", AsyncMock()) as sr:
        resp = client.post("/admin/track-rating", json={"track_id": "srv:42", "stars": 4})
    assert resp.status_code == 200
    assert resp.json() == {"track_id": "srv:42", "stars": 4}
    sr.assert_awaited_once_with("srv:42", 4)


def test_set_rating_zero_clears(client, mock_state):
    with patch("app.api.admin.database.set_rating", AsyncMock()) as sr:
        resp = client.post("/admin/track-rating", json={"track_id": "t1", "stars": 0})
    assert resp.status_code == 200
    assert resp.json()["stars"] is None          # cleared → null
    sr.assert_awaited_once_with("t1", 0)


def test_set_rating_out_of_range_422(client, mock_state):
    with patch("app.api.admin.database.set_rating", AsyncMock()) as sr:
        resp = client.post("/admin/track-rating", json={"track_id": "t1", "stars": 6})
    assert resp.status_code == 422
    sr.assert_not_awaited()                       # rejected before any write


def test_set_rating_invalid_track_id_400(client, mock_state):
    with patch("app.api.admin.database.set_rating", AsyncMock()) as sr:
        resp = client.post("/admin/track-rating", json={"track_id": "bad id!", "stars": 3})
    assert resp.status_code == 400
    sr.assert_not_awaited()


def test_set_tags_echoes_normalized(client, mock_state):
    # set_tags is the normalization chokepoint; the endpoint echoes whatever it
    # stores. Mock it to return the normalized set and assert the echo.
    with patch("app.api.admin.database.set_tags",
               AsyncMock(return_value=["Fun", "Chill"])) as st:
        resp = client.post("/admin/track-tags",
                           json={"track_id": "t1", "tags": ["  Fun ", "fun", "Chill"]})
    assert resp.status_code == 200
    assert resp.json() == {"track_id": "t1", "tags": ["Fun", "Chill"]}
    st.assert_awaited_once()


def test_set_tags_absurd_payload_422(client, mock_state):
    with patch("app.api.admin.database.set_tags", AsyncMock()) as st:
        resp = client.post("/admin/track-tags",
                           json={"track_id": "t1", "tags": [f"t{i}" for i in range(101)]})
    assert resp.status_code == 422
    st.assert_not_awaited()


def test_settings_persists_visibility_and_facet_flags(client, mock_state):
    """Plan U4: the seven bool flags persist as '1'/'0' via set_setting."""
    with patch("app.database.set_setting", AsyncMock()) as ss, \
         patch("app.events.bus.manager.broadcast_to_all", AsyncMock()):
        resp = client.post("/admin/settings", json={
            "ratings_visible_to_guests": True,
            "tags_visible_to_guests": False,
            "facet_years": False,
            "facet_highestrated": True,
        })
    assert resp.status_code == 200
    persisted = {c.args[0]: c.args[1] for c in ss.call_args_list}
    assert persisted.get("ratings_visible_to_guests") == "1"
    assert persisted.get("tags_visible_to_guests") == "0"
    assert persisted.get("facet_years") == "0"
    assert persisted.get("facet_highestrated") == "1"


# ── Radio Mode guest-control toggle (2026-08-11 plan U8, R9) ─────────────────

def test_settings_persists_guest_radio_control(client, mock_state):
    """U8/R9: guest_radio_control persists as '1'/'0' via set_setting, like the
    sibling guest-visibility flags."""
    with patch("app.database.set_setting", AsyncMock()) as ss, \
         patch("app.events.bus.manager.broadcast_to_all", AsyncMock()):
        resp_on = client.post("/admin/settings", json={"guest_radio_control": True})
        persisted_on = {c.args[0]: c.args[1] for c in ss.call_args_list}
        ss.reset_mock()
        resp_off = client.post("/admin/settings", json={"guest_radio_control": False})
        persisted_off = {c.args[0]: c.args[1] for c in ss.call_args_list}
    assert resp_on.status_code == 200 and resp_off.status_code == 200
    assert persisted_on.get("guest_radio_control") == "1"
    assert persisted_off.get("guest_radio_control") == "0"


def test_get_settings_echoes_guest_radio_control(client, mock_state):
    """GET /settings reflects the stored flag; unset (default) hydrates False."""
    store = {"guest_radio_control": "1"}
    async def fake_get(key, default=None): return store.get(key, default)
    with patch("app.database.get_setting", AsyncMock(side_effect=fake_get)):
        resp = client.get("/admin/settings")
    assert resp.status_code == 200
    assert resp.json()["guest_radio_control"] is True
    # Default (unset) is off.
    with patch("app.database.get_setting", AsyncMock(return_value=None)):
        resp = client.get("/admin/settings")
    assert resp.json()["guest_radio_control"] is False


def test_settings_guest_radio_control_requires_auth(anon_client, mock_session):
    """A non-admin POST /settings is 401 (existing admin-auth pattern)."""
    resp = anon_client.post("/admin/settings", json={"guest_radio_control": True})
    assert resp.status_code == 401


# ── Play-data curation endpoints (2026-07-03 plan U4) ────────────────────────

def test_curation_endpoints_require_auth(anon_client, mock_session):
    # AE6: both mutations reject an unauthenticated request.
    assert anon_client.post("/admin/history/remove-play",
                            json={"track_id": "t1", "added_at": "x"}).status_code == 401
    assert anon_client.post("/admin/most-played/remove",
                            json={"track_id": "t1"}).status_code == 401


def test_remove_play_uncounts_then_removes_entry(client, mock_state):
    qe, _ = mock_state
    from app.queue.models import QueueItem
    qe._history.appendleft(QueueItem(track=make_track("t1"), added_at="ts-1"))
    with patch("app.state.unrecord_play", AsyncMock()) as un:
        resp = client.post("/admin/history/remove-play",
                           json={"track_id": "t1", "added_at": "ts-1"})
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    un.assert_awaited_once_with("t1", "B", "A")          # album/artist from the entry
    assert all(h.track_id != "t1" for h in qe.history)   # entry removed


def test_remove_play_absent_entry_404_and_no_uncount(client, mock_state):
    with patch("app.state.unrecord_play", AsyncMock()) as un:
        resp = client.post("/admin/history/remove-play",
                           json={"track_id": "t1", "added_at": "nope"})
    assert resp.status_code == 404
    un.assert_not_awaited()                              # no mutation on a miss


def test_remove_play_uncount_failure_leaves_history_intact(client, mock_state):
    qe, _ = mock_state
    from app.queue.models import QueueItem
    qe._history.appendleft(QueueItem(track=make_track("t1"), added_at="ts-1"))
    with patch("app.state.unrecord_play", AsyncMock(side_effect=RuntimeError("db"))):
        with pytest.raises(RuntimeError):
            client.post("/admin/history/remove-play",
                        json={"track_id": "t1", "added_at": "ts-1"})
    # Least-harm order: un-count runs first; its failure leaves the entry in place.
    assert any(h.track_id == "t1" for h in qe.history)


def test_remove_from_most_played_purges_track(client, mock_state):
    with patch("app.state.purge_play_track", AsyncMock()) as purge:
        resp = client.post("/admin/most-played/remove", json={"track_id": "t1"})
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    purge.assert_awaited_once_with("t1")


# ── Outage-hold transport semantics + observability (supervisor plan U4) ──────
# R17: skip/previous while held move the HELD POINTER only (no dispatch to the
# dead device, no set_stopped destruction, no 502); queue clear drops the held
# item and re-targets any in-flight resume; R20: both admin GET snapshots
# mirror the OutputSessionEvent fields for late joiners / WS-gap resync.


async def test_playback_skip_while_held_moves_pointer_no_dispatch(
        client, mock_state, fresh_supervisor, monkeypatch):
    """U4 scenario (b), the R17 regression: Skip during an outage hold retires
    the held front to history (mark intact — it WAS counted) and the next
    queued item becomes the held front (unplayed -> mark False). NOTHING
    dispatches to the dead device and nothing set_stops — the old path 502'd
    and destroyed the popped held item. Hold survives; the gen bump re-targets
    any in-flight auto-resume at the new front from 0:00 (U3 contract)."""
    import app.state as st
    from app.output import session
    qe, or_ = mock_state
    sup, timers, rec = fresh_supervisor
    monkeypatch.setattr(hold, "_output_hold", True)
    held = await qe.append(make_track("held"), bypass_lock=True)
    held.play_recorded = True                    # it played + counted pre-outage
    await qe.append(make_track("t2"), bypass_lock=True)
    gen = st._advance_gen

    resp = client.post("/admin/playback/skip")

    assert resp.status_code == 200               # regression: no 502
    or_.play.assert_not_awaited()                # no dispatch to the dead device
    or_.stop.assert_not_awaited()                # no set_stopped-style teardown
    assert [i.track_id for i in qe.queue] == ["t2"]
    assert qe.queue[0].play_recorded is False    # new held front: unplayed
    assert qe.history[0].track_id == "held"      # retired where advance() would
    assert qe.history[0].play_recorded is True   # skipped-away item keeps its mark
    assert qe.state.current is None              # nothing pretends to play
    assert session.output_hold_active() is True  # hold preserved
    assert st._advance_gen == gen + 1            # in-flight resume re-targeted


async def test_playback_skip_while_held_empty_queue_is_quiet_noop(
        client, mock_state, fresh_supervisor, monkeypatch):
    """Skip while held with an empty queue: 200, nothing dispatched, nothing
    torn down — never a 5xx from a transport tap while held."""
    from app.output import session
    qe, or_ = mock_state
    monkeypatch.setattr(hold, "_output_hold", True)
    resp = client.post("/admin/playback/skip")
    assert resp.status_code == 200
    or_.play.assert_not_awaited()
    or_.stop.assert_not_awaited()
    assert session.output_hold_active() is True


async def test_playback_previous_while_held_front_inserts_history(
        client, mock_state, fresh_supervisor, monkeypatch):
    """R17: Skip Back while held front-inserts the previous history item as
    the new held front — a pure pointer move needing NO media source
    (mock_state's get_plex_client is None, which 409s the live path), no
    dispatch, hold preserved, gen bumped so the resume targets it at 0:00."""
    import app.state as st
    from app.output import session
    from app.queue.models import QueueItem
    qe, or_ = mock_state
    monkeypatch.setattr(hold, "_output_hold", True)
    await qe.append(make_track("held"), bypass_lock=True)
    qe._history.appendleft(QueueItem(track=make_track("prev")))
    gen = st._advance_gen

    resp = client.post("/admin/playback/previous")

    assert resp.status_code == 200
    or_.play.assert_not_awaited()
    or_.stop.assert_not_awaited()
    assert [i.track_id for i in qe.queue] == ["prev", "held"]
    assert qe.queue[0].play_recorded is False    # organic replay re-counts (R19)
    assert not qe.history
    assert qe.state.current is None
    assert session.output_hold_active() is True
    assert st._advance_gen == gen + 1


async def test_playback_previous_while_held_empty_history_409(
        client, mock_state, fresh_supervisor, monkeypatch):
    """Skip Back while held with no history: same 409 contract as the live
    path (button-race safety net) — and still no dispatch/teardown."""
    from app.output import session
    qe, or_ = mock_state
    monkeypatch.setattr(hold, "_output_hold", True)
    await qe.append(make_track("held"), bypass_lock=True)
    resp = client.post("/admin/playback/previous")
    assert resp.status_code == 409
    or_.play.assert_not_awaited()
    or_.stop.assert_not_awaited()
    assert [i.track_id for i in qe.queue] == ["held"]


async def test_queue_clear_while_held_drops_held_item_and_retargets_resume(
        client, mock_state, fresh_supervisor, monkeypatch):
    """U4 scenario (c), endpoint half: clearing during an outage drops the
    held item with the rest of the queue. The hold STAYS (device still gone;
    U3's resume path lands idle on an empty queue at re-attach — pinned in
    tests/test_output_session.py), and the gen bump stops an in-flight resume
    from seeking the dropped track's position into a later-queued front."""
    import app.state as st
    from app.output import session
    qe, or_ = mock_state
    monkeypatch.setattr(hold, "_output_hold", True)
    await qe.append(make_track("held"), bypass_lock=True)
    await qe.append(make_track("t2"), bypass_lock=True)
    gen = st._advance_gen

    resp = client.post("/admin/queue/clear", json={"confirmed": True})

    assert resp.status_code == 200
    assert qe.queue == []                        # held item dropped too (R17)
    assert session.output_hold_active() is True  # hold stays; re-attach lands idle
    assert st._advance_gen == gen + 1


async def test_queue_clear_not_held_does_not_bump_gen(client, mock_state):
    """Guard: the gen bump is held-path only — a normal clear keeps today's
    behavior (no pending-advance invalidation side effect)."""
    import app.state as st
    qe, or_ = mock_state
    await qe.append(make_track("t1"), bypass_lock=True)
    gen = st._advance_gen
    resp = client.post("/admin/queue/clear", json={"confirmed": True})
    assert resp.status_code == 200
    assert st._advance_gen == gen


async def test_queue_remove_while_held_bumps_gen(
        client, mock_state, fresh_supervisor, monkeypatch):
    """F5: removing during an outage can drop the HELD front — the gen bump
    (the queue_clear mechanic) stops an in-flight resume from seeking the
    removed track's held position into whatever is in front next."""
    import app.state as st
    from app.output import session
    qe, or_ = mock_state
    monkeypatch.setattr(hold, "_output_hold", True)
    await qe.append(make_track("held"), bypass_lock=True)
    await qe.append(make_track("t2"), bypass_lock=True)
    gen = st._advance_gen
    resp = client.delete("/admin/queue/0")
    assert resp.status_code == 200
    assert [i.track_id for i in qe.queue] == ["t2"]
    assert st._advance_gen == gen + 1
    assert session.output_hold_active() is True


async def test_queue_move_while_held_bumps_gen(
        client, mock_state, fresh_supervisor, monkeypatch):
    """F5: moving during an outage can change the HELD front — same gen-bump
    contract as remove/clear."""
    import app.state as st
    from app.output import session
    qe, or_ = mock_state
    monkeypatch.setattr(hold, "_output_hold", True)
    await qe.append(make_track("held"), bypass_lock=True)
    await qe.append(make_track("t2"), bypass_lock=True)
    gen = st._advance_gen
    resp = client.post("/admin/queue/move",
                       json={"from_position": 1, "to_position": 0})
    assert resp.status_code == 200
    assert [i.track_id for i in qe.queue] == ["t2", "held"]
    assert st._advance_gen == gen + 1


async def test_queue_play_next_while_held_bumps_gen(
        client, mock_state, fresh_supervisor, monkeypatch):
    """F5: promoting during an outage replaces the HELD front — same gen-bump
    contract as remove/clear."""
    import app.state as st
    from app.output import session
    qe, or_ = mock_state
    monkeypatch.setattr(hold, "_output_hold", True)
    await qe.append(make_track("held"), bypass_lock=True)
    await qe.append(make_track("t2"), bypass_lock=True)
    gen = st._advance_gen
    resp = client.post("/admin/queue/1/play-next")
    assert resp.status_code == 200
    assert [i.track_id for i in qe.queue] == ["t2", "held"]
    assert st._advance_gen == gen + 1


async def test_queue_ops_not_held_do_not_bump_gen(client, mock_state):
    """Guard (mirror of the clear guard): remove/move/promote bump the gen on
    the held path only."""
    import app.state as st
    qe, or_ = mock_state
    await qe.append(make_track("t1"), bypass_lock=True)
    await qe.append(make_track("t2"), bypass_lock=True)
    gen = st._advance_gen
    assert client.delete("/admin/queue/1").status_code == 200
    assert client.post("/admin/queue/move",
                       json={"from_position": 0, "to_position": 0}).status_code == 200
    assert client.post("/admin/queue/0/play-next").status_code == 200
    assert st._advance_gen == gen


async def test_playback_pause_during_hold_records_intent_no_device_write(
        client, mock_state, fresh_supervisor, monkeypatch):
    """F12: pause during an outage hold is an INTENT, not a device write —
    200, no router.pause (the old path wrote to the dead device: DLNA's
    unguarded async_pause → 500), was_paused recorded on the supervisor AND
    the outage context so re-attach lands PAUSED instead of auto-playing."""
    from app.output import session
    qe, or_ = mock_state
    sup, timers, rec = fresh_supervisor
    monkeypatch.setattr(hold, "_output_hold", True)
    ot = session._Outage("connection_lost")
    sup._outage = ot
    resp = client.post("/admin/playback/pause")
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    or_.pause.assert_not_awaited()
    assert sup.was_paused is True
    assert ot.was_paused is True


async def test_playback_skip_race_hold_cleared_while_waiting_takes_live_path(
        mock_state, fresh_supervisor, monkeypatch):
    """F10: an in-flight resume (holding _advance_lock) clears the hold while
    Skip waits for the lock. Skip must re-check the hold INSIDE the lock and
    take the LIVE path — the old pre-lock check pointer-popped the now-live
    front into history without dispatching it (a queued track silently
    vanished). Calls the endpoint coroutine directly so both tasks share
    this test's event loop."""
    import asyncio
    import app.state as st
    from app.api.admin import playback_skip
    from app.output import session
    qe, or_ = mock_state
    monkeypatch.setattr(st, "_advance_lock", asyncio.Lock())  # loop-fresh lock
    monkeypatch.setattr(hold, "_output_hold", True)
    await qe.append(make_track("t2"), bypass_lock=True)
    resume_done = asyncio.Event()

    async def fake_resume():
        async with st._advance_lock:
            await resume_done.wait()             # Skip queues on the lock…
            hold._output_hold = False            # …resume clears the hold

    with patch("app.state.get_plex_client", AsyncMock(return_value=MagicMock())), \
         patch("app.state._make_stream_url", return_value="http://stream/t2"):
        resume_task = asyncio.create_task(fake_resume())
        await asyncio.sleep(0)                   # resume owns the lock
        assert st._advance_lock.locked()
        skip_task = asyncio.create_task(playback_skip())
        for _ in range(3):
            await asyncio.sleep(0)               # Skip parks on the lock
        resume_done.set()
        _, skip_result = await asyncio.gather(resume_task, skip_task)

    assert skip_result == {"ok": True}
    or_.play.assert_awaited_once()               # LIVE path dispatched t2
    assert qe.state.current.track_id == "t2"
    assert not any(h.track_id == "t2" for h in qe.history)  # nothing vanished


async def test_admin_now_playing_carries_output_session_snapshot(
        client, mock_state, fresh_supervisor, monkeypatch):
    """R20 (U4 scenario a, GET half): the admin now-playing snapshot mirrors
    the OutputSessionEvent fields — admin-rich shape, present on the
    no-current branch (the one a late joiner hits mid-outage, since the hold
    clears `current`)."""
    from app.output import session
    sup, timers, rec = fresh_supervisor
    monkeypatch.setattr(hold, "_output_hold", True)
    monkeypatch.setattr(hold, "_output_hold_reason", "connection_lost")
    sup.session_state = session.STATE_OUTAGE_PAUSED

    data = client.get("/admin/playback/now-playing").json()

    snap = data["output_session"]
    assert snap["state"] == "outage_paused"
    assert snap["held"] is True
    assert snap["reason"] == "connection_lost"
    # Admin-rich keys are always present (None when no reconnect context).
    for key in ("backend_type", "device_id", "device_name", "attempts",
                "next_retry_s", "window_remaining_s", "was_paused",
                "flap_tripped", "idle_paused_reason"):
        assert key in snap


async def test_admin_queue_carries_output_session_snapshot(
        client, mock_state, fresh_supervisor, monkeypatch):
    """R20: /admin/queue — the page's actual resync pull (refreshQueueState on
    load / WS reconnect / tab refocus) — carries the same admin-rich shape."""
    from app.output import session
    sup, timers, rec = fresh_supervisor
    monkeypatch.setattr(hold, "_output_hold", True)
    monkeypatch.setattr(hold, "_output_hold_reason", "poll_errors")
    sup.session_state = session.STATE_OUTAGE_PAUSED

    data = client.get("/admin/queue").json()

    snap = data["output_session"]
    assert snap["state"] == "outage_paused" and snap["held"] is True
    assert snap["reason"] == "poll_errors"
    assert "window_remaining_s" in snap and "attempts" in snap


async def test_admin_snapshots_not_held_read_idle_and_unheld(
        client, mock_state, fresh_supervisor):
    """No outage: both admin snapshots still carry the field (clients hide the
    banner from it) with held=False."""
    for path in ("/admin/playback/now-playing", "/admin/queue"):
        snap = client.get(path).json()["output_session"]
        assert snap["held"] is False
        assert snap["state"] == "idle"


async def test_playback_volume_during_hold_reflects_in_volume_snapshot(
        client, mock_state, fresh_supervisor, monkeypatch):
    """U4 scenario (d), snapshot half: the held volume write lands on the
    backend's in-memory level — the source GET /admin/playback/volume reads —
    so the slider re-hydrates to the accepted value while the device is still
    gone. Runs the REAL set_held_volume (unlike the U3 routing test above);
    the live device write path is never touched."""
    from app.output import session
    qe, or_ = mock_state
    monkeypatch.setattr(hold, "_output_hold", True)
    resp = client.post("/admin/playback/volume", json={"level": 0.35})
    assert resp.status_code == 200
    assert or_.active._volume == 0.35    # in-memory level = get_volume's source
    or_.set_volume.assert_not_awaited()  # never a live write to the dead output


# ── plexplayer backend plumbing (2026-08-04-002 plan U3) ──────────────────────

async def test_set_output_accepts_plexplayer(client, mock_state):
    """SetOutputRequest's Literal gained 'plexplayer' — the route routes it
    to activate_backend like any other backend type."""
    with patch("app.state.activate_backend", AsyncMock()) as act:
        resp = client.post(
            "/admin/output/active",
            json={"backend_type": "plexplayer", "device_id": "caldera-1"})
    assert resp.status_code == 200
    act.assert_awaited_once_with("plexplayer", "caldera-1", host=None)
    body = resp.json()
    assert body["backend_type"] == "plexplayer"
    assert body["device_id"] == "caldera-1"


def test_set_output_request_confirmed_defaults_false():
    """The two-phase switch-confirm field (U6) rides the model from U3's
    single Literal edit: defaults False, accepts an explicit value."""
    from app.api.admin import SetOutputRequest
    assert SetOutputRequest(backend_type="plexplayer").confirmed is False
    assert SetOutputRequest(backend_type="direct", confirmed=True).confirmed \
        is True


async def test_set_output_confirmed_accepted_on_wire_and_ignored(client, mock_state):
    """A confirmed:true POST validates and behaves identically today — the
    route consumes the field only from U6 on."""
    with patch("app.state.activate_backend", AsyncMock()) as act:
        resp = client.post(
            "/admin/output/active",
            json={"backend_type": "direct", "device_id": "default",
                  "confirmed": True})
    assert resp.status_code == 200
    act.assert_awaited_once_with("direct", "default", host=None)


async def test_legacy_pull_lists_plexplayer_devices_without_mdns_key(client, mock_state):
    """Legacy (watcher-absent) GET /output/devices: plexplayer devices show
    up in the aggregated payload with gapless "unverified", while
    mdns_status carries NO plexplayer key — its liveness rides /clients,
    and key-absence (never "unavailable") is what keeps the frontend
    banner from rendering the backend degraded (availability decision,
    plan U3)."""
    from types import SimpleNamespace
    from app.output.base import OutputDevice
    pp = SimpleNamespace(
        discover_devices=AsyncMock(return_value=[
            OutputDevice(id="caldera-1", name="Caldera",
                         backend_type="plexplayer", id_format="uuid")]),
        device_host=lambda did: "192.168.1.88",
        probe_device=AsyncMock(return_value=True),
    )
    with patch("app.state.plexplayer_backend", pp):
        resp = client.get("/admin/output/devices")
    assert resp.status_code == 200
    body = resp.json()
    assert "plexplayer" not in body["mdns_status"]
    entry = next(d for d in body["devices"] if d["host"] == "192.168.1.88")
    assert entry["name"] == "Caldera"
    protos = {p["backend"]: p for p in entry["protocols"]}
    assert protos["plexplayer"]["device_id"] == "caldera-1"
    assert protos["plexplayer"]["gapless"] == "unverified"


async def test_legacy_pull_missing_plexplayer_backend_stays_silent(client, mock_state):
    """A None plexplayer backend (state not set up) must not surface an
    "unavailable" flag — the guarded mdns_status writes skip it (the
    frontend would otherwise show a 'plexplayer scan unavailable'
    banner)."""
    resp = client.get("/admin/output/devices")  # mock_state: all backends None
    assert resp.status_code == 200
    assert "plexplayer" not in resp.json()["mdns_status"]


# ── plex_held on admin queue rows + source_lock snapshot (plan U4) ───────────


async def test_admin_queue_rows_carry_plex_held(client, mock_state):
    """The admin queue/recents payload carries the backend-independent
    per-track flag too — the shared queue renderer paints both pages from
    one shape. Native single-Plex path → constant True."""
    qe, _ = mock_state
    await qe.append(make_track("t1"), bypass_lock=True)
    await qe.append(make_track("t2"), bypass_lock=True)
    data = client.get("/admin/queue").json()
    assert data["queue"] and all(r["plex_held"] is True for r in data["queue"])


async def test_admin_queue_output_session_carries_source_lock(
        client, mock_state, fresh_supervisor, monkeypatch):
    """The admin-rich snapshot inherits the lean truth: source_lock rides
    /admin/queue's output_session (the page's resync pull), keyed off the
    persisted selection — same field the OutputSessionEvent push carries."""
    import app.state as st
    data = client.get("/admin/queue").json()
    assert data["output_session"]["source_lock"] is None
    monkeypatch.setattr(st, "_selected_output_backend", "plexplayer")
    data2 = client.get("/admin/queue").json()
    assert data2["output_session"]["source_lock"] == "plex"


async def test_admin_now_playing_output_session_carries_source_lock(
        client, mock_state, fresh_supervisor, monkeypatch):
    import app.state as st
    monkeypatch.setattr(st, "_selected_output_backend", "plexplayer")
    data = client.get("/admin/playback/now-playing").json()
    assert data["output_session"]["source_lock"] == "plex"


# ── U5 enqueue gate — admin endpoints (2026-08-04-002 plan U5; R6) ────────────
# The admin queue endpoints run the SAME server-side gate as the guest's (no
# admin bypass): the shared resolver in app/api/guest.py keys off the
# persisted selection and resolves holds live from the catalog.


@pytest.fixture
async def admin_catalog_env(tmp_path, monkeypatch):
    """Seeded catalog + real QueueEngine with plexplayer selected, for the
    admin gate: t1 = Plex-held (m1), tjo = Jellyfin-only; album 'mix' holds
    both. Mirrors tests/test_api_guest.py's catalog_env shape."""
    from app import database, state
    from app.catalog import store
    from app.config import Settings
    from app.queue.engine import QueueEngine

    s = Settings(data_dir=tmp_path, secret_key="test")
    monkeypatch.setattr(database, "settings", s)
    await database.init_db()
    await store.replace_catalog(
        artists=[{"identity": "ar", "title": "Act", "base_key": "act"}],
        albums=[
            {"identity": "mix", "title": "Mix", "title_base": "mix", "artist": "Act",
             "artist_base_key": "act", "year": 2020, "track_count": 2},
        ],
        tracks=[
            {"identity": "t1", "title": "Song One", "title_base": "song one", "artist": "Act",
             "artist_base_key": "act", "album": "Mix", "album_identity": "mix",
             "duration_ms": 180000, "disc_number": 1, "track_number": 1},
            {"identity": "tjo", "title": "J Song", "title_base": "j song", "artist": "Act",
             "artist_base_key": "act", "album": "Mix", "album_identity": "mix",
             "duration_ms": 150000, "disc_number": 1, "track_number": 2},
        ],
        holds=[
            {"entity_type": "track", "identity": "t1", "source_id": "m1",
             "provider_local_key": "m1:p1", "priority": 0, "server_name": "Plex"},
            {"entity_type": "track", "identity": "tjo", "source_id": "jelly",
             "provider_local_key": "jelly:pjo", "priority": 1, "server_name": "Jelly"},
        ],
    )
    qe = QueueEngine()
    registry = MagicMock()
    registry.get_track = AsyncMock(return_value=make_track("t1"))
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("app.state.queue_engine", qe))
        stack.enter_context(patch("app.state.get_plex_client",
                                  AsyncMock(return_value=registry)))
        stack.enter_context(patch("app.api.guest._catalog_active",
                                  AsyncMock(return_value=True)))
        stack.enter_context(patch("app.database.save_queue", AsyncMock()))
        stack.enter_context(patch("app.database.save_history", AsyncMock()))
        stack.enter_context(patch.object(state, "_selected_output_backend", "plexplayer"))
        # S-1: the gate entry (state.plex_lock_enabled_ids) owns the catalog
        # check + enabled-id build - patch the state seams.
        stack.enter_context(patch("app.state.catalog_active",
                                  AsyncMock(return_value=True)))
        stack.enter_context(patch("app.state.plex_enabled_source_ids",
                                  AsyncMock(return_value={"m1"})))
        try:
            yield qe
        finally:
            await database.close_db()


async def test_admin_append_track_gated_no_bypass(admin_catalog_env):
    # bypass_lock is an admin privilege; the source lock is NOT — same 409
    # shape as the guest endpoint, nothing queued.
    from fastapi import HTTPException
    from app.api.admin import admin_append_to_queue, AdminQueueAppendRequest
    with pytest.raises(HTTPException) as ei:
        await admin_append_to_queue(AdminQueueAppendRequest(track_id="tjo"))
    assert ei.value.status_code == 409
    assert ei.value.detail == "output_source_lock"
    assert admin_catalog_env.queue == []


async def test_admin_append_plex_held_track_allowed(admin_catalog_env):
    from app.api.admin import admin_append_to_queue, AdminQueueAppendRequest
    res = await admin_append_to_queue(AdminQueueAppendRequest(track_id="t1"))
    assert res["ok"] is True and res["tracks_added"] == 1
    assert admin_catalog_env.queue[0].track_id == "t1"


async def test_admin_append_gate_inert_on_other_backend(admin_catalog_env):
    import app.state as st
    from app.api.admin import admin_append_to_queue, AdminQueueAppendRequest
    with patch.object(st, "_selected_output_backend", "direct"):
        res = await admin_append_to_queue(AdminQueueAppendRequest(track_id="tjo"))
    assert res["tracks_added"] == 1
    assert len(admin_catalog_env.queue) == 1


async def test_admin_album_mixed_enqueues_playable_subset(admin_catalog_env):
    # Subset policy on the admin batch path: only the Plex-held track lands;
    # the response reports added vs filtered counts.
    from app.api.admin import admin_append_to_queue, AdminQueueAppendRequest
    with patch("app.api.admin._resolve_album_tracks",
               AsyncMock(return_value=[make_track("t1"), make_track("tjo")])):
        res = await admin_append_to_queue(AdminQueueAppendRequest(album_id="mix"))
    assert res["tracks_added"] == 1
    assert res["tracks_filtered"] == 1
    assert [it.track_id for it in admin_catalog_env.queue] == ["t1"]


async def test_admin_album_zero_playable_rejected(admin_catalog_env):
    from fastapi import HTTPException
    from app.api.admin import admin_append_to_queue, AdminQueueAppendRequest
    with patch("app.api.admin._resolve_album_tracks",
               AsyncMock(return_value=[make_track("tjo")])):
        with pytest.raises(HTTPException) as ei:
            await admin_append_to_queue(AdminQueueAppendRequest(album_id="mix"))
    assert ei.value.status_code == 409
    assert ei.value.detail == "output_source_lock"
    assert admin_catalog_env.queue == []


# ── U6 two-phase switch confirm + mid-session re-validation ──────────────────
# (2026-08-04-002 plan U6; R7/R8/R12, AE1/AE2, F1.) The stranded evaluation
# runs in the route (outside activate_backend — rollback semantics stay
# clean); removal + held-front gen-bump live in the shared state helpers the
# R12 re-validation hooks reuse.


_U6_CONFIRM_DETAIL = {"reason": "output_switch_confirm", "stranded_count": 3,
                      "confirm_required": True}


@pytest.fixture
async def switch_catalog_env(tmp_path, monkeypatch):
    """Seeded catalog + real QueueEngine for the U6 stranded flows:
    p1/p2 Plex-held (m1), j1..j4 Jellyfin-only (stranded under the lock).
    The persisted selection starts on 'direct' — the switch-time pre-check
    evaluates the TARGET backend via assume_lock, so it must work while the
    current selection is NOT plexplayer; re-validation tests flip the
    selection themselves. Mirrors admin_catalog_env's patch surface."""
    from app import database, state
    from app.catalog import store
    from app.config import Settings
    from app.queue.engine import QueueEngine

    s = Settings(data_dir=tmp_path, secret_key="test")
    monkeypatch.setattr(database, "settings", s)
    await database.init_db()
    tracks, holds = [], []
    for tid, src, server in (("p1", "m1", "Plex"), ("p2", "m1", "Plex"),
                             ("j1", "jelly", "Jelly"), ("j2", "jelly", "Jelly"),
                             ("j3", "jelly", "Jelly"), ("j4", "jelly", "Jelly")):
        tracks.append({"identity": tid, "title": tid, "title_base": tid,
                       "artist": "Act", "artist_base_key": "act",
                       "album": "Mix", "album_identity": "mix",
                       "duration_ms": 180000, "disc_number": 1,
                       "track_number": len(tracks) + 1})
        holds.append({"entity_type": "track", "identity": tid,
                      "source_id": src, "provider_local_key": f"{src}:{tid}",
                      "priority": 0, "server_name": server})
    await store.replace_catalog(
        artists=[{"identity": "ar", "title": "Act", "base_key": "act"}],
        albums=[{"identity": "mix", "title": "Mix", "title_base": "mix",
                 "artist": "Act", "artist_base_key": "act", "year": 2020,
                 "track_count": 6}],
        tracks=tracks, holds=holds)
    qe = QueueEngine()
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("app.state.queue_engine", qe))
        stack.enter_context(patch("app.api.guest._catalog_active",
                                  AsyncMock(return_value=True)))
        stack.enter_context(patch("app.database.save_queue", AsyncMock()))
        stack.enter_context(patch("app.database.save_history", AsyncMock()))
        stack.enter_context(patch.object(state, "_selected_output_backend",
                                         "direct"))
        # S-1: the gate entry (state.plex_lock_enabled_ids) owns the catalog
        # check + enabled-id build - patch the state seams.
        stack.enter_context(patch("app.state.catalog_active",
                                  AsyncMock(return_value=True)))
        stack.enter_context(patch("app.state.plex_enabled_source_ids",
                                  AsyncMock(return_value={"m1"})))
        try:
            yield qe
        finally:
            await database.close_db()


async def _seed_queue(qe, ids):
    for tid in ids:
        await qe.append(make_track(tid), bypass_lock=True)


async def test_switch_warns_then_confirmed_resend_removes_stranded(switch_catalog_env):
    """Covers AE1: mixed queue → 409 with stranded_count 3 and NO state
    change; the confirmed resend removes exactly those 3 (order of the rest
    preserved, receipts matched), reports the actual removed count, and
    proceeds to activate."""
    from fastapi import HTTPException
    from app.api.admin import set_output_active, SetOutputRequest
    qe = switch_catalog_env
    await _seed_queue(qe, ("p1", "j1", "p2", "j2", "j3"))
    with patch("app.state.activate_backend", AsyncMock()) as act:
        with pytest.raises(HTTPException) as ei:
            await set_output_active(SetOutputRequest(
                backend_type="plexplayer", device_id="c1"))
        assert ei.value.status_code == 409
        assert ei.value.detail == _U6_CONFIRM_DETAIL
        act.assert_not_awaited()  # warning phase: output untouched
        assert [i.track_id for i in qe.queue] == ["p1", "j1", "p2", "j2", "j3"]
        res = await set_output_active(SetOutputRequest(
            backend_type="plexplayer", device_id="c1", confirmed=True))
    assert res["removed_count"] == 3
    assert [i.track_id for i in qe.queue] == ["p1", "p2"]
    act.assert_awaited_once_with("plexplayer", "c1", host=None)


async def test_switch_reject_leaves_queue_and_output_untouched(switch_catalog_env):
    """Covers AE2: the admin rejects (never resends confirmed) — the 409
    changed nothing, so queue and output stay exactly as they were."""
    from fastapi import HTTPException
    from app.api.admin import set_output_active, SetOutputRequest
    qe = switch_catalog_env
    await _seed_queue(qe, ("p1", "j1"))
    with patch("app.state.activate_backend", AsyncMock()) as act:
        with pytest.raises(HTTPException) as ei:
            await set_output_active(SetOutputRequest(
                backend_type="plexplayer", device_id="c1"))
    assert ei.value.status_code == 409
    assert [i.track_id for i in qe.queue] == ["p1", "j1"]
    act.assert_not_awaited()


async def test_confirmed_switch_activate_failure_restores_queue(switch_catalog_env):
    """Review fix PLX-3: activate_backend raising AFTER the confirmed
    removal (DeviceNotReadyError → plain-string 409, network 502, ...)
    means the switch never happened and the old backend keeps playing —
    the removed stranded entries must be restored at their original
    positions, receipts intact (guest remove-own still works), and the
    failure response must not claim a removal."""
    from fastapi import HTTPException
    from app.api.admin import set_output_active, SetOutputRequest
    qe = switch_catalog_env
    await _seed_queue(qe, ("p1", "j1", "p2", "j2", "j3"))
    before_ids = [id(i) for i in qe.queue]
    before = [(i.track_id, i.added_at) for i in qe.queue]
    act = AsyncMock(side_effect=RuntimeError("device not ready — rescan"))
    with patch("app.state.activate_backend", act):
        with pytest.raises(HTTPException) as ei:
            await set_output_active(SetOutputRequest(
                backend_type="plexplayer", device_id="c1", confirmed=True))
    assert ei.value.status_code == 409
    assert isinstance(ei.value.detail, str)          # no removal claim
    act.assert_awaited_once()
    # Byte-identical queue: same receipts, same order, the SAME objects.
    assert [(i.track_id, i.added_at) for i in qe.queue] == before
    assert [id(i) for i in qe.queue] == before_ids


async def test_switch_confirm_recomputes_live_stranded_set(switch_catalog_env):
    """Race case: a guest enqueues a 4th unplayable track between warning
    and confirm — the confirm-phase recompute wins and removal reports 4."""
    from fastapi import HTTPException
    from app.api.admin import set_output_active, SetOutputRequest
    qe = switch_catalog_env
    await _seed_queue(qe, ("p1", "j1", "j2", "j3"))
    with patch("app.state.activate_backend", AsyncMock()) as act:
        with pytest.raises(HTTPException) as ei:
            await set_output_active(SetOutputRequest(
                backend_type="plexplayer", device_id="c1"))
        assert ei.value.detail["stranded_count"] == 3
        await _seed_queue(qe, ("j4",))  # guest slips one in mid-dialog
        res = await set_output_active(SetOutputRequest(
            backend_type="plexplayer", device_id="c1", confirmed=True))
    assert res["removed_count"] == 4
    assert [i.track_id for i in qe.queue] == ["p1"]
    act.assert_awaited_once()


async def test_switch_all_playable_queue_activates_directly(switch_catalog_env):
    """All-playable queue → no 409, no dialog round-trip: the switch
    behaves exactly like every pre-U6 switch (removed_count 0)."""
    from app.api.admin import set_output_active, SetOutputRequest
    qe = switch_catalog_env
    await _seed_queue(qe, ("p1", "p2"))
    with patch("app.state.activate_backend", AsyncMock()) as act:
        res = await set_output_active(SetOutputRequest(
            backend_type="plexplayer", device_id="c1"))
    assert res["ok"] is True and res["removed_count"] == 0
    assert [i.track_id for i in qe.queue] == ["p1", "p2"]
    act.assert_awaited_once_with("plexplayer", "c1", host=None)


async def test_switch_non_plexplayer_target_never_gated(switch_catalog_env):
    """A non-plexplayer target skips the gate entirely — byte-identical
    existing behavior even with a fully stranded queue (switching AWAY from
    the lock never warns; plan scope)."""
    from app.api.admin import set_output_active, SetOutputRequest
    qe = switch_catalog_env
    await _seed_queue(qe, ("j1", "j2"))
    with patch("app.state.activate_backend", AsyncMock()) as act:
        res = await set_output_active(SetOutputRequest(
            backend_type="chromecast", device_id="cc-1"))
    assert res["ok"] is True and res["removed_count"] == 0
    assert [i.track_id for i in qe.queue] == ["j1", "j2"]
    act.assert_awaited_once_with("chromecast", "cc-1", host=None)


async def test_switch_confirm_409_wire_shape_distinguishable(client, mock_state):
    """Pins the wire shape: the confirm 409's detail serializes as a JSON
    OBJECT with reason 'output_switch_confirm' — structurally distinguishable
    from this endpoint's plain-STRING 409 details (activate_backend failures)
    and from the queue endpoints' 'output_source_lock', so a genuine switch
    failure can never open the client's confirm dialog."""
    qe, _ = mock_state
    item = await qe.append(make_track("tjo"), bypass_lock=True)
    with patch("app.state.plex_stranded_entries",
               AsyncMock(return_value=[item])):
        resp = client.post(
            "/admin/output/active",
            json={"backend_type": "plexplayer", "device_id": "c1"})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert isinstance(detail, dict) and detail != "output_source_lock"
    assert detail == {"reason": "output_switch_confirm", "stranded_count": 1,
                      "confirm_required": True}


async def test_confirmed_removal_of_held_front_bumps_advance_gen(
        switch_catalog_env, monkeypatch):
    """Held-front discipline (the queue_clear mechanic): confirming a switch
    during an outage hold whose HELD front entry is stranded must bump
    _advance_gen so an in-flight resume treats the new front as fresh at
    0:00 — never seeking the removed track's held position into it."""
    import app.state as st
    from app.api.admin import set_output_active, SetOutputRequest
    qe = switch_catalog_env
    await _seed_queue(qe, ("j1", "p1"))
    monkeypatch.setattr(st, "_advance_gen", 41)
    with patch("app.state.activate_backend", AsyncMock()), \
         patch("app.output.session.output_hold_active",
               MagicMock(return_value=True)):
        res = await set_output_active(SetOutputRequest(
            backend_type="plexplayer", device_id="c1", confirmed=True))
    assert res["removed_count"] == 1
    assert st._advance_gen == 42
    assert [i.track_id for i in qe.queue] == ["p1"]


async def test_rescan_completion_removes_newly_stranded(
        switch_catalog_env, monkeypatch):
    """R12: catalog-refresh completion runs the re-validation pass while
    plexplayer is selected — the newly stranded entry is auto-removed (no
    dialog) and the admin notice carries the removed count. The currently-
    playing track is not queue state, so it is untouched by construction."""
    import app.state as st
    qe = switch_catalog_env
    await _seed_queue(qe, ("p1", "j1"))
    monkeypatch.setattr(st, "_selected_output_backend", "plexplayer")
    notice = AsyncMock()
    with patch("app.catalog.scan.scan_and_replace",
               AsyncMock(return_value=True)), \
         patch("app.state.get_plex_client",
               AsyncMock(return_value=MagicMock())), \
         patch("app.events.bus.manager.broadcast_to_admins", notice):
        await st._refresh_catalog()
    assert [i.track_id for i in qe.queue] == ["p1"]
    notice.assert_awaited_once()
    ev = notice.call_args.args[0]
    assert ev.backend_type == "error"
    assert "1" in ev.device_name  # the count rides the notice copy


async def test_disabled_sources_change_revalidates_queue(
        switch_catalog_env, monkeypatch):
    """R12: a Libraries-panel whole-source veto change triggers the same
    re-validation immediately (the read-time predicate flips at veto save,
    before any rescan completes)."""
    import app.state as st
    from app.api.admin import _set_source_disabled
    qe = switch_catalog_env
    await _seed_queue(qe, ("p1", "j1"))
    monkeypatch.setattr(st, "_selected_output_backend", "plexplayer")
    notice = AsyncMock()
    with patch("app.state.trigger_browse_index_refresh", MagicMock()), \
         patch("app.state.trigger_catalog_refresh", MagicMock()), \
         patch("app.state.invalidate_ondeck", AsyncMock()), \
         patch("app.events.bus.manager.broadcast_to_admins", notice):
        await _set_source_disabled("jelly", disabled=True)
    assert [i.track_id for i in qe.queue] == ["p1"]
    notice.assert_awaited_once()


async def test_revalidate_noop_without_lock(switch_catalog_env):
    """No lock active (non-plexplayer selection) → re-validation is a
    no-op: nothing removed, no notice."""
    from app import state
    qe = switch_catalog_env
    await _seed_queue(qe, ("p1", "j1"))
    notice = AsyncMock()
    with patch("app.events.bus.manager.broadcast_to_admins", notice):
        removed = await state.revalidate_plex_queue(trigger="rescan")
    assert removed == 0
    assert [i.track_id for i in qe.queue] == ["p1", "j1"]
    notice.assert_not_awaited()
