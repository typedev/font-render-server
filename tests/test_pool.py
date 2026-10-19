"""
Tests for the browser pool / bounded queue behind POST /render.

Queue, recycling and crash handling are tested with a fake browser (no
Chromium needed); the timeout path renders for real.

    uv run pytest tests/test_pool.py -v
"""

import asyncio

import pytest
from starlette.testclient import TestClient

from font_render import app as appmod, config, pool
from test_geometry import _find_font


class FakeBrowser:
    def __init__(self):
        self.connected = True
        self.closed = False
        self.version = "fake"

    def is_connected(self):
        return self.connected

    async def close(self):
        self.closed = True
        self.connected = False


@pytest.fixture
def fake_pool(monkeypatch):
    launched = []

    async def launch(name, cleartype):
        b = FakeBrowser()
        launched.append(b)
        return b

    monkeypatch.setattr(pool, "launch_browser", launch)

    def make(**cfg):
        for k, v in cfg.items():
            monkeypatch.setattr(config, k, v)
        pool._browser_lock = asyncio.Lock()
        return pool.BrowserPool(), launched

    return make


def test_queue_full_returns_busy(fake_pool):
    async def run():
        bp, _ = fake_pool(CONCURRENCY=1, QUEUE_MAX=1, POOL_SIZE=1)
        release = asyncio.Event()

        async def hold():
            async with bp.acquire("chromium", True, {}):
                await release.wait()

        holder = asyncio.create_task(hold())
        await asyncio.sleep(0)
        waiter = asyncio.create_task(hold())  # takes the single queue place
        await asyncio.sleep(0)
        assert bp.busy == 1 and bp.waiting == 1
        with pytest.raises(pool.PoolBusy) as exc:
            async with bp.acquire("chromium", True, {}):
                pass
        assert exc.value.retry_after >= 1
        release.set()
        await asyncio.gather(holder, waiter)
        assert bp.busy == 0 and bp.waiting == 0

    asyncio.run(run())


def test_pool_grows_to_size_and_balances(fake_pool):
    async def run():
        bp, launched = fake_pool(CONCURRENCY=4, QUEUE_MAX=4, POOL_SIZE=2)
        release = asyncio.Event()
        used = []

        async def hold():
            async with bp.acquire("chromium", True, {}) as slot:
                used.append(slot.browser)
                await release.wait()

        tasks = [asyncio.create_task(hold()) for _ in range(4)]
        await asyncio.sleep(0.01)
        assert len(launched) == 2  # never more than POOL_SIZE per key
        assert used.count(launched[0]) == 2 and used.count(launched[1]) == 2
        release.set()
        await asyncio.gather(*tasks)

    asyncio.run(run())


def test_recycle_after_k_renders(fake_pool):
    async def run():
        bp, launched = fake_pool(POOL_SIZE=1, RECYCLE_AFTER=3)
        for _ in range(3):
            async with bp.acquire("chromium", True, {}):
                pass
        await asyncio.sleep(0)  # let the background close run
        assert launched[0].closed
        async with bp.acquire("chromium", True, {}) as slot:
            assert slot.browser is launched[1]

    asyncio.run(run())


def test_crashed_browser_is_replaced(fake_pool):
    async def run():
        bp, launched = fake_pool(POOL_SIZE=1)
        async with bp.acquire("chromium", True, {}):
            pass
        launched[0].connected = False  # simulate a crash
        async with bp.acquire("chromium", True, {}) as slot:
            assert slot.browser is launched[1]

    asyncio.run(run())


def test_keys_are_separate(fake_pool):
    async def run():
        bp, launched = fake_pool(POOL_SIZE=1)
        async with bp.acquire("chromium", True, {}) as a:
            pass
        async with bp.acquire("chromium", False, {}) as b:
            pass
        assert a.browser is not b.browser
        assert len(launched) == 2

    asyncio.run(run())


def test_timeout_returns_504_and_server_recovers(monkeypatch):
    font = _find_font()
    form = {"ppem": "12", "layout": "plain"}
    with TestClient(appmod.app) as c:
        monkeypatch.setattr(config, "TIMEOUT_S", 0.001)
        r = c.post("/render", files=[("fonts", ("a.ttf", font))], data=form)
        assert r.status_code == 504
        assert r.json() == {"error": "timeout"}
        monkeypatch.setattr(config, "TIMEOUT_S", 60)
        r = c.post("/render", files=[("fonts", ("a.ttf", font))], data=form)
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert "queue;dur=" in r.headers["server-timing"]


def test_info_reports_pool():
    with TestClient(appmod.app) as c:
        pool = c.get("/info").json()["pool"]
    for k in ("browsers", "busy", "queued", "queue_max"):
        assert k in pool
