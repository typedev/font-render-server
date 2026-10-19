"""
Tests for client auth: Bearer tokens with scopes, allow_anonymous_render,
legacy admin token, client management endpoints.

    uv run pytest tests/test_auth.py -v
"""

import asyncio
import json

import pytest
from starlette.testclient import TestClient

from font_render import app as appmod, auth, config
from test_geometry import _find_font

LEGACY = "legacy-secret"
FORM = {"ppem": "12", "layout": "plain", "text": "a"}


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UPDATE_TOKEN", LEGACY)
    monkeypatch.setattr(auth, "registry", auth.ClientRegistry(tmp_path / "clients.json"))
    with TestClient(appmod.app) as client:
        yield client


@pytest.fixture(scope="module")
def font():
    return _find_font()


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _render(c, font, headers=None):
    return c.post("/render", files=[("fonts", ("a.ttf", font))], data=FORM, headers=headers or {})


def _add(c, client_id, scopes="render"):
    r = c.post("/admin/clients/add", data={"client_id": client_id, "scopes": scopes, "token": LEGACY})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _settings(c, **kw):
    data = {k: str(v).lower() for k, v in kw.items()}
    r = c.post("/admin/settings", data={**data, "token": LEGACY})
    assert r.status_code == 200, r.text


def test_anonymous_allowed_by_default(c, font):
    assert _render(c, font).status_code == 200
    # unknown Bearer is treated as anonymous while migrating
    assert _render(c, font, _bearer("nope")).status_code == 200
    auth = c.get("/info").json()["auth"]
    assert auth == {"required": False, "client": None}


def test_token_identifies_client_and_is_stored_hashed(c, tmp_path):
    token = _add(c, "hintbench")
    assert c.get("/info", headers=_bearer(token)).json()["auth"]["client"] == "hintbench"
    stored = (tmp_path / "clients.json").read_text()
    assert token not in stored
    assert json.loads(stored)["clients"]["hintbench"]["scopes"] == ["render"]


def test_anonymous_off(c, font):
    token = _add(c, "bot")
    _settings(c, allow_anonymous_render=False)
    r = _render(c, font)
    assert r.status_code == 401 and r.json() == {"error": "unauthorized"}
    assert _render(c, font, _bearer("nope")).status_code == 401
    assert _render(c, font, _bearer(token)).status_code == 200
    for path in ("/info", "/docs", "/openapi.json", "/"):
        assert c.get(path).status_code == 401, path
        assert c.get(path, headers=_bearer(token)).status_code == 200, path
    assert c.get("/info", headers=_bearer(token)).json()["auth"]["required"] is True
    health = c.get("/health")
    assert health.status_code == 200 and set(health.json()) == {"status", "version", "release"}


def test_admin_scope(c):
    render_token = _add(c, "bot")
    admin_token = _add(c, "ops", "render,admin")
    r = c.post("/admin/clients/list", headers=_bearer(render_token))
    assert r.status_code == 403 and r.json() == {"error": "forbidden"}
    assert c.post("/admin/clients/list").status_code == 401
    assert c.post("/admin/clients/list", data={"token": "wrong"}).status_code == 403
    assert c.post("/admin/clients/list", headers=_bearer(admin_token)).status_code == 200
    # a render-scope Bearer plus the legacy form token still works (client.py --update)
    assert c.post("/admin/clients/list", headers=_bearer(render_token),
                  data={"token": LEGACY}).status_code == 200
    _settings(c, legacy_admin_token=False)
    assert c.post("/admin/clients/list", data={"token": LEGACY}).status_code == 401
    assert c.post("/admin/clients/list", headers=_bearer(admin_token)).status_code == 200


def test_rotate_disable_remove(c, font):
    token = _add(c, "bot")
    _settings(c, allow_anonymous_render=False)
    r = c.post("/admin/clients/rotate", data={"client_id": "bot", "token": LEGACY})
    new_token = r.json()["token"]
    assert _render(c, font, _bearer(token)).status_code == 401
    assert _render(c, font, _bearer(new_token)).status_code == 200
    c.post("/admin/clients/disable", data={"client_id": "bot", "token": LEGACY})
    assert _render(c, font, _bearer(new_token)).status_code == 401
    c.post("/admin/clients/enable", data={"client_id": "bot", "token": LEGACY})
    assert _render(c, font, _bearer(new_token)).status_code == 200
    c.post("/admin/clients/remove", data={"client_id": "bot", "token": LEGACY})
    assert _render(c, font, _bearer(new_token)).status_code == 401


def test_validation(c):
    assert _add(c, "bot")
    r = c.post("/admin/clients/add", data={"client_id": "bot", "token": LEGACY})
    assert r.status_code == 409
    for bad in ({"client_id": "Bad Id"}, {"client_id": "x", "scopes": "root"}):
        r = c.post("/admin/clients/add", data={**bad, "token": LEGACY})
        assert r.status_code == 400


def test_file_reloaded_on_change(c, font, tmp_path):
    path = tmp_path / "clients.json"
    path.write_text(json.dumps({"settings": {"allow_anonymous_render": False}, "clients": {}}))
    assert _render(c, font).status_code == 401
    path.write_text(json.dumps({"settings": {"allow_anonymous_render": True}, "clients": {}}) + " ")
    assert _render(c, font).status_code == 200


def test_rejected_before_body_is_read(tmp_path, monkeypatch):
    """401 is sent by the middleware without consuming the upload."""
    monkeypatch.setattr(auth, "registry", auth.ClientRegistry(tmp_path / "clients.json"))
    (tmp_path / "clients.json").write_text(json.dumps({"settings": {"allow_anonymous_render": False}}))

    async def app(scope, receive, send):
        raise AssertionError("app must not be called")

    async def receive():
        raise AssertionError("body must not be read")

    sent = []

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/render", "headers": [], "client": ("1.2.3.4", 1)}
    asyncio.run(auth.AuthMiddleware(app)(scope, receive, send))
    assert sent[0]["status"] == 401
