"""
Tests for the web admin: login and session cookie, CSRF origin check, lockout
guards, status/requests/logs endpoints, maintenance actions.

    uv run pytest tests/test_web_admin.py -v
"""

import pytest
from starlette.testclient import TestClient

from font_render import app as appmod, auth, config, web
from font_render.stats import stats
from test_geometry import _find_font

LEGACY = "legacy-secret"
SAME_ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UPDATE_TOKEN", LEGACY)
    monkeypatch.setattr(config, "SESSION_KEY_FILE", tmp_path / ".session_key")
    monkeypatch.setattr(config, "LOG_FILE", tmp_path / "service.log")
    monkeypatch.setattr(auth, "registry", auth.ClientRegistry(tmp_path / "clients.json"))
    web._login_failures.clear()
    with TestClient(appmod.app) as client:
        yield client


def _login(c, token=LEGACY):
    return c.post("/admin/login", data={"token": token})


def _add(c, client_id, scopes="render"):
    r = c.post("/admin/clients/add", data={"client_id": client_id, "scopes": scopes, "token": LEGACY})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_page_is_public_api_is_not(c):
    r = c.get("/admin/")
    assert r.status_code == 200 and "Font Render" in r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert c.get("/admin/static/admin.js").status_code == 200
    assert c.get("/admin/static/../app.py").status_code == 404
    assert c.get("/admin/api/status").status_code == 401


def test_login_legacy_and_status(c):
    r = _login(c)
    assert r.status_code == 200 and r.json()["subject"] == auth.LEGACY_SUBJECT
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    s = c.get("/admin/api/status").json()
    assert s["subject"] == auth.LEGACY_SUBJECT
    assert s["server"]["render_version"] == config.RENDER_VERSION
    assert {"pool", "font_cache", "browsers", "clients", "config", "settings"} <= s.keys()
    assert c.get("/admin/api/me").json() == {"subject": auth.LEGACY_SUBJECT, "via": "session"}


def test_login_rejects_render_token_and_rate_limits(c):
    render_token = _add(c, "renderer")
    assert _login(c, render_token).status_code == 401
    for _ in range(web.LOGIN_MAX_FAILURES - 1):
        assert _login(c, "wrong").status_code == 401
    assert _login(c, LEGACY).status_code == 429  # locked even for the right token
    assert "retry-after" in _login(c, LEGACY).headers


def test_login_client_field_must_match_the_token(c):
    token = _add(c, "boss", "admin")
    for client, code in (("boss", 200), ("", 200), ("admin", 401)):
        c.cookies.clear()  # a session cookie would make the next login a cross-origin POST
        assert c.post("/admin/login", data={"client": client, "token": token}).status_code == code, client


def test_admin_client_session_ends_on_rotate_and_disable(c):
    token = _add(c, "boss", "admin")
    assert _login(c, token).json()["subject"] == "boss"
    assert c.get("/admin/api/status").status_code == 200
    r = c.post("/admin/clients/rotate", data={"client_id": "boss"}, headers=SAME_ORIGIN)
    assert r.status_code == 200
    assert c.get("/admin/api/status").status_code == 401  # old credential fingerprint
    assert _login(c, r.json()["token"]).status_code == 200
    c.cookies.clear()
    _login(c)
    c.post("/admin/clients/disable", data={"client_id": "boss"}, headers=SAME_ORIGIN)
    assert auth.read_session(auth.issue_session("boss")) is None


def test_session_survives_restart_and_tampering_fails(c):
    _login(c)
    value = c.cookies.get(auth.SESSION_COOKIE)
    auth._session_keys.clear()  # a restart re-reads the key file
    assert auth.read_session(value) == auth.LEGACY_SUBJECT
    b64, sig = value.rsplit(".", 1)
    assert auth.read_session(b64 + "." + "0" * len(sig)) is None
    assert auth.read_session("garbage") is None


def test_cookie_post_needs_same_origin(c):
    _login(c)
    form = {"client_id": "x1", "scopes": "render"}
    assert c.post("/admin/clients/add", data=form).status_code == 403
    assert c.post("/admin/clients/add", data=form, headers={"Origin": "http://evil.example"}).status_code == 403
    assert c.post("/admin/clients/add", data=form, headers=SAME_ORIGIN).status_code == 200
    # Bearer callers (CLI) don't need an Origin
    c.cookies.clear()
    admin_token = _add(c, "ops", "admin")
    r = c.post("/admin/fonts/clear", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200


def test_cookie_does_not_authorize_render(c):
    c.post("/admin/settings", data={"allow_anonymous_render": "false", "token": LEGACY})
    _login(c)
    r = c.post("/render", files=[("fonts", ("a.ttf", _find_font()))], data={"ppem": "12"},
               headers=SAME_ORIGIN)
    assert r.status_code == 401


def test_lockout_guards(c):
    r = c.post("/admin/settings", data={"legacy_admin_token": "false", "token": LEGACY})
    assert r.status_code == 409  # no admin client yet
    admin_token = _add(c, "boss", "admin")
    bearer = {"Authorization": f"Bearer {admin_token}"}
    assert c.post("/admin/settings", data={"legacy_admin_token": "false"}, headers=bearer).status_code == 200
    for action in ("disable", "remove"):
        r = c.post(f"/admin/clients/{action}", data={"client_id": "boss"}, headers=bearer)
        assert r.status_code == 409
    assert c.post("/admin/clients/rotate", data={"client_id": "boss"}, headers=bearer).status_code == 200


def test_logs_tail_and_filter(c):
    config.LOG_FILE.write_text("".join(f"line {i} {'ERROR' if i % 10 == 0 else 'INFO'}\n" for i in range(1000)))
    _login(c)
    r = c.get("/admin/api/logs", params={"lines": 5}).json()
    assert r["lines"] == [f"line {i} INFO" for i in range(995, 1000)]
    r = c.get("/admin/api/logs", params={"lines": 3, "q": "error"}).json()
    assert r["lines"] == ["line 970 ERROR", "line 980 ERROR", "line 990 ERROR"]
    config.LOG_FILE.unlink()
    assert c.get("/admin/api/logs").json()["exists"] is False


def test_requests_recorded_without_admin_polling(c):
    stats.recent.clear()
    stats.clients.clear()
    token = _add(c, "renderer")
    r = c.post("/render", files=[("fonts", ("a.ttf", _find_font()))],
               data={"ppem": "12", "layout": "plain", "text": "a"},
               headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    _login(c)
    c.get("/admin/api/status")
    c.get("/health")
    rows = c.get("/admin/api/requests").json()["requests"]
    paths = [x["path"] for x in rows]
    assert "/render" in paths and "/admin/api/status" not in paths and "/health" not in paths
    s = c.get("/admin/api/status").json()
    assert s["activity"]["renderer"]["renders"] == 1
    assert s["render_latency"]["count"] == 1
    assert c.get("/admin/api/requests", params={"client": "renderer"}).json()["requests"][0]["client"] == "renderer"


def test_recycle_and_clear(c):
    _login(c)
    fc = appmod.app.state.font_cache
    fc.put("someone", b"\x00\x01\x00\x00fontdata", "x.ttf")
    r = c.post("/admin/fonts/clear", headers=SAME_ORIGIN).json()
    assert r == {"deleted": 1} and fc.bytes == 0
    r = c.post("/admin/browsers/recycle", headers=SAME_ORIGIN)
    assert r.status_code == 200 and r.json()["recycled"] >= 0


def test_logout_clears_cookie(c):
    _login(c)
    c.post("/admin/logout", headers=SAME_ORIGIN)
    assert c.get("/admin/api/status").status_code == 401
