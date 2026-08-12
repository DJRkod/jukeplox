"""U9 — Admin → Setup "Optional integrations" panel: integrations status +
toggle endpoints, the guest-invisibility structural guard (AE7), and the
experimental opt-in surface (AE6). The shared-UI discipline is enforced
separately by tests/test_static_discipline.py (ADMIN_ALLOWED extended).
"""

from pathlib import Path

import pytest
from unittest.mock import AsyncMock

from app import state

ROOT = Path(__file__).resolve().parents[1]


# ── integrations status endpoint ──────────────────────────────────────────────


def test_integrations_status_disabled(client, monkeypatch):
    monkeypatch.setattr(state, "get_server_fed_backend", lambda t: None)
    monkeypatch.setattr(state, "server_fed_backend_enabled", lambda t: False)
    r = client.get("/admin/output/integrations")
    assert r.status_code == 200
    data = r.json()
    assert set(data) == {"snapcast", "sendspin"}
    assert data["sendspin"]["experimental"] is True
    assert data["snapcast"]["experimental"] is False
    assert data["snapcast"]["enabled"] is False
    assert data["snapcast"]["connected"] is False


def test_integrations_status_connected_with_clients(client, monkeypatch):
    class _B:
        _connected = True
        def is_connected(self):
            return True
        async def discover_devices(self):
            return [object(), object()]
    monkeypatch.setattr(state, "get_server_fed_backend",
                        lambda t: _B() if t == "snapcast" else None)
    monkeypatch.setattr(state, "server_fed_backend_enabled", lambda t: t == "snapcast")
    data = client.get("/admin/output/integrations").json()
    assert data["snapcast"]["connected"] is True
    assert data["snapcast"]["client_count"] == 2


# ── toggle endpoint ───────────────────────────────────────────────────────────


def test_toggle_enable_ok(client, monkeypatch):
    calls = []
    async def _set(backend, enabled):
        calls.append((backend, enabled))
    monkeypatch.setattr(state, "set_backend_enabled", _set)
    monkeypatch.setattr(state, "server_fed_backend_enabled", lambda t: True)
    r = client.post("/admin/output/integrations/snapcast/toggle", json={"enabled": True})
    assert r.status_code == 200 and r.json()["enabled"] is True
    assert calls == [("snapcast", True)]


def test_toggle_enable_failure_latches_502(client, monkeypatch):
    async def _boom(backend, enabled):
        raise RuntimeError("control port 1705 is already in use")
    monkeypatch.setattr(state, "set_backend_enabled", _boom)
    r = client.post("/admin/output/integrations/sendspin/toggle", json={"enabled": True})
    assert r.status_code == 502
    assert r.json()["detail"]["category"] == "enable_failed"
    assert "in use" in r.json()["detail"]["message"]


def test_toggle_unknown_backend_404(client):
    r = client.post("/admin/output/integrations/bogus/toggle", json={"enabled": True})
    assert r.status_code == 404


def test_integrations_require_admin(anon_client):
    assert anon_client.get("/admin/output/integrations").status_code in (401, 403)


# ── AE7: guest never sees the config surface (structural) ─────────────────────


def test_guest_template_has_no_multiroom_panel():
    guest = (ROOT / "app/templates/guest/index.html").read_text(encoding="utf-8")
    assert "optional-integrations" not in guest
    assert "multiroom-panel" not in guest


def test_admin_template_has_multiroom_panel():
    admin = (ROOT / "app/templates/admin/dashboard.html").read_text(encoding="utf-8")
    assert 'id="optional-integrations"' in admin
    assert 'id="multiroom-panel"' in admin


# ── AE6 + interaction states surfaced in the admin JS ─────────────────────────


def test_admin_js_has_multiroom_wiring():
    js = (ROOT / "static/admin/app.js").read_text(encoding="utf-8")
    # enable toggle + zoning + pairing wiring lands in the existing per-page file
    for needle in ("loadIntegrations", "renderMultiroomPanel", "renderZoneSlider",
                   "renderPairingPanel"):
        assert needle in js, f"admin/app.js missing {needle}"
    # Experimental badge (AE6), the not-audible zero-clients callout (R15/AE2),
    # the failed-latch copy, and per-client aria-labels (a11y).
    assert "Experimental" in js
    assert "not audible" in js
    assert "aria-label" in js and "volume" in js
    # zone sliders reuse the --vol-fill visual treatment, not a parallel master path
    assert "--vol-fill" in js


def test_admin_js_full_zoning_control_plane():
    """Agent-native parity follow-up: the browser reaches the full control plane
    the API exposes — client mute, group volume/mute, assign, rename/dissolve,
    and the external-Snapcast config form."""
    js = (ROOT / "static/admin/app.js").read_text(encoding="utf-8")
    for needle in ("renderGroupHeader", "renderExternalConfig", "zonePost"):
        assert needle in js, f"admin/app.js missing {needle}"
    # every zone endpoint has a UI caller
    assert "/client/" in js and "/mute" in js            # client mute
    assert "/group/" in js and "/volume" in js           # group volume
    assert "/rename" in js and "/assign" in js           # topology + assign
    assert "method: 'DELETE'" in js                       # dissolve
    assert "snapcast/connect" in js                       # external config form
    # topology controls gated on can_manage_topology (embedded only)
    assert "canManageTopology" in js
