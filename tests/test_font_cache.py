"""
Tests for the RAM font cache: POST/DELETE /fonts, font_ids in /render,
per-client namespaces, TTL/LRU, staleness, per-client render limit.

    uv run pytest tests/test_font_cache.py -v
"""

import asyncio
import hashlib

import pytest
from starlette.testclient import TestClient

from font_render import app as appmod, auth, config, pool
from font_render.font_cache import FontCache
from test_geometry import _find_font
from test_pool import fake_pool  # noqa: F401  (fixture)
from test_staleness import _unhinted

LEGACY = "legacy-secret"
FORM = {"ppem": "9,12,16", "text": "Hamburgefontsiv", "layout": "full"}


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UPDATE_TOKEN", LEGACY)
    monkeypatch.setattr(auth, "registry", auth.ClientRegistry(tmp_path / "clients.json"))
    with TestClient(appmod.app) as client:
        client.tokens = {}
        for cid in ("alice", "bob"):
            r = client.post("/admin/clients/add", data={"client_id": cid, "token": LEGACY})
            client.tokens[cid] = {"Authorization": f"Bearer {r.json()['token']}"}
        yield client


@pytest.fixture(scope="module")
def fonts():
    a = _find_font()
    return a, _unhinted(a)


def _upload(c, who, *named):
    r = c.post("/fonts", files=[("fonts", (n, d)) for n, d in named], headers=c.tokens[who])
    assert r.status_code == 200, r.text
    return [f["sha256"] for f in r.json()["fonts"]]


def _render(c, who, ids=(), files=(), **extra):
    return c.post("/render", data={**FORM, "font_ids": ",".join(ids), **extra},
                  files=[("fonts", (n, d)) for n, d in files] or None, headers=c.tokens[who])


def test_upload_returns_hashes_in_order(c, fonts):
    a, a2 = fonts
    r = c.post("/fonts", files=[("fonts", ("a.ttf", a)), ("fonts", ("b.ttf", a2))],
               headers=c.tokens["alice"])
    assert r.json() == {"fonts": [
        {"sha256": hashlib.sha256(a).hexdigest(), "size": len(a)},
        {"sha256": hashlib.sha256(a2).hexdigest(), "size": len(a2)},
    ]}


@pytest.mark.parametrize("layout", ["full", "plain"])
@pytest.mark.parametrize("geometry", ["false", "true"])
def test_font_ids_render_identical_to_upload(c, fonts, layout, geometry):
    a, a2 = fonts
    ids = _upload(c, "alice", ("a.ttf", a), ("b.ttf", a2))
    by_id = _render(c, "alice", ids=ids, layout=layout, geometry=geometry)
    by_upload = _render(c, "alice", files=[("a.ttf", a), ("b.ttf", a2)], layout=layout, geometry=geometry)
    assert by_id.status_code == 200
    assert by_id.content == by_upload.content


def test_mixed_ids_first_then_files(c, fonts):
    a, a2 = fonts
    [id_a2] = _upload(c, "alice", ("b.ttf", a2))
    mixed = _render(c, "alice", ids=[id_a2], files=[("a.ttf", a)])
    assert mixed.content == _render(c, "alice", files=[("b.ttf", a2), ("a.ttf", a)]).content


def test_missing_returns_409_then_reupload(c, fonts):
    a, _ = fonts
    sha = hashlib.sha256(a).hexdigest()
    r = _render(c, "alice", ids=[sha, sha])
    assert r.status_code == 409
    assert r.json() == {"error": "missing_fonts", "missing": [sha]}
    _upload(c, "alice", ("a.ttf", a))
    assert _render(c, "alice", ids=[sha]).status_code == 200


def test_namespaces_are_isolated(c, fonts):
    a, _ = fonts
    [sha] = _upload(c, "alice", ("a.ttf", a))
    assert _render(c, "bob", ids=[sha]).status_code == 409
    info = c.get("/info", headers=c.tokens["bob"]).json()
    assert info["font_cache"]["client"] == {"fonts": 0, "bytes": 0}
    assert c.get("/info", headers=c.tokens["alice"]).json()["font_cache"]["client"]["fonts"] == 1


def test_delete_drops_only_own_namespace(c, fonts):
    a, _ = fonts
    [sha] = _upload(c, "alice", ("a.ttf", a))
    _upload(c, "bob", ("a.ttf", a))
    r = c.delete("/fonts", headers=c.tokens["alice"])
    assert r.json() == {"deleted": 1}
    assert _render(c, "alice", ids=[sha]).status_code == 409
    assert _render(c, "bob", ids=[sha]).status_code == 200


def test_ttl_expiry(c, fonts):
    a, _ = fonts
    [sha] = _upload(c, "alice", ("a.ttf", a))
    appmod._font_cache.ttl_s = 0
    assert _render(c, "alice", ids=[sha]).status_code == 409


def test_invalid_ids(c):
    r = _render(c, "alice", ids=["xyz"])
    assert r.status_code == 400


def test_font_ids_follow_edited_bytes(c, fonts):
    """Staleness guard on the font_ids path: same file name, new bytes."""
    a, a2 = fonts
    form = {"layout": "plain", "ppem": "9,10,11,12"}
    [id_a] = _upload(c, "alice", ("font.ttf", a))
    first = _render(c, "alice", ids=[id_a], **form).content
    [id_a2] = _upload(c, "alice", ("font.ttf", a2))
    edited = _render(c, "alice", ids=[id_a2], **form).content
    assert first != edited
    assert _render(c, "alice", ids=[id_a], **form).content == first
    assert _render(c, "alice", ids=[id_a2], **form).content == edited
    assert edited == _render(c, "alice", files=[("font.ttf", a2)], **form).content


def test_info_api3(c):
    info = c.get("/info", headers=c.tokens["alice"]).json()
    assert info["api"] == 3
    assert info["font_cache"]["enabled"] is True
    assert set(info["limits"]) == {"max_fonts", "max_font_size", "max_ppem_values"}
    assert info["pool"]["client_max"] >= 1


def test_lru_bounded_by_bytes():
    cache = FontCache(max_bytes=10, ttl_s=60)
    s1 = cache.put("x", b"aaaa", "1")
    s2 = cache.put("x", b"bbbb", "2")
    cache.get("x", s1)  # s1 becomes most recent
    cache.put("y", b"cccc", "3")
    assert cache.bytes <= 10
    assert cache.get("x", s2) is None and cache.get("x", s1) is not None


def test_per_client_limit(fake_pool):  # noqa: F811
    async def run():
        bp, _ = fake_pool(CLIENT_MAX=1, CONCURRENCY=4, QUEUE_MAX=4)
        release = asyncio.Event()

        async def hold(cid):
            async with bp.acquire("chromium", True, {}, client_id=cid):
                await release.wait()

        task = asyncio.create_task(hold("alice"))
        await asyncio.sleep(0)
        with pytest.raises(pool.PoolBusy):
            async with bp.acquire("chromium", True, {}, client_id="alice"):
                pass
        async with bp.acquire("chromium", True, {}, client_id="bob"):
            pass
        release.set()
        await task
        assert bp.per_client == {}

    asyncio.run(run())


def test_info_engines(c):
    info = c.get("/info", headers=c.tokens["alice"]).json()
    assert info["engines"] == ["chromium"]
    assert info["render_versions"]["chromium"] == info["render_version"]
    assert "windows" in info  # None off Windows


def test_render_variations_validation(c, fonts):
    """Static font with a location, a tag not in fvar, too many locations: 400 before rendering."""
    [fid] = _upload(c, "alice", ("a.ttf", fonts[0]))
    r = _render(c, "alice", ids=[fid], variations="wght=700")
    assert r.status_code == 400 and r.json()["error"] == "bad_variations"
    r = _render(c, "alice", ids=[fid], variations=";;")
    assert r.status_code == 400
