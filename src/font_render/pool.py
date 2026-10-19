"""Browser pool: N browsers per (engine, cleartype), a global concurrency limit,
a bounded wait queue, a per-client limit, and per-browser caches of loaded
pages that are reused for requests with the same fonts.
"""

import asyncio
import json
import math
import os
import time
from collections import OrderedDict
from contextlib import asynccontextmanager

from playwright.async_api import Browser, async_playwright

from . import config
from .font_cache import FontCache
from .geometry import GEOMETRY_JS, scale_geometry
from .page import PageParts

_playwright = None
_playwright_lock = asyncio.Lock()
_browser_lock = asyncio.Lock()


def reset_locks():
    """asyncio primitives are bound to the running loop: recreate per app lifetime."""
    global _playwright_lock, _browser_lock
    _playwright_lock = asyncio.Lock()
    _browser_lock = asyncio.Lock()


async def stop_playwright():
    global _playwright
    if _playwright:
        await _playwright.stop()
        _playwright = None


async def ensure_playwright():
    global _playwright
    async with _playwright_lock:
        if _playwright is None:
            _playwright = await async_playwright().start()
    return _playwright


async def launch_browser(browser_name: str, cleartype: bool) -> Browser:
    pw = await ensure_playwright()
    if browser_name == "chromium":
        args = ["--font-render-hinting=full", "--disable-gpu", "--disk-cache-size=0"]
        if not cleartype:
            args.append("--disable-lcd-text")
        return await pw.chromium.launch(args=args)
    if browser_name == "firefox":
        return await pw.firefox.launch(firefox_user_prefs=firefox_prefs())
    return await pw.webkit.launch()


def firefox_prefs() -> dict:
    """Prefs from config.FIREFOX_PREFS_FILE (set in the web admin), {} if none."""
    try:
        prefs = json.loads(config.FIREFOX_PREFS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        config.log.error("firefox prefs unreadable, ignored: %s", e)
        return {}
    return prefs if isinstance(prefs, dict) else {}


class PoolBusy(Exception):
    def __init__(self, retry_after: int, reason: str = "queue_full"):
        self.retry_after = retry_after
        self.reason = reason  # "queue_full" or "client_limit"


class _CachedPage:
    def __init__(self, context, page):
        self.context = context
        self.page = page
        self.busy = True
        self.last_used = time.monotonic()


class _Slot:
    """One browser instance in the pool, with its cache of loaded pages."""

    def __init__(self, browser: Browser):
        self.browser = browser
        self.active = 0
        self.renders = 0
        self.retiring = False
        # (scale, fonts_key) -> page with that @font-face block already loaded
        self.pages: OrderedDict[tuple, _CachedPage] = OrderedDict()

    @property
    def alive(self) -> bool:
        return self.browser.is_connected()

    def has_idle_page(self, key) -> bool:
        cached = self.pages.get(key)
        return cached is not None and not cached.busy

    def take_page(self, key) -> _CachedPage | None:
        if not self.has_idle_page(key):
            return None
        cached = self.pages[key]
        cached.busy = True
        self.pages.move_to_end(key)
        return cached

    async def put_page(self, key, context, page):
        """Return a page to the cache after a successful render (LRU-bounded)."""
        cached = self.pages.get(key)
        if cached is not None:
            if cached.context is context:
                cached.busy = False
                cached.last_used = time.monotonic()
            else:  # rendered on a fresh page because the cached one was busy
                await context.close()
            return
        cached = _CachedPage(context, page)
        cached.busy = False
        self.pages[key] = cached
        while len(self.pages) > config.PAGE_CACHE:
            old_key = next((k for k, c in self.pages.items() if not c.busy), None)
            if old_key is None:
                break
            await self.pages.pop(old_key).context.close()

    def drop_page(self, key, context):
        cached = self.pages.get(key)
        if cached is not None and cached.context is context:
            del self.pages[key]

    async def sweep(self, now: float):
        """Close pages idle longer than config.PAGE_TTL_S."""
        for key in [k for k, c in self.pages.items() if not c.busy and now - c.last_used > config.PAGE_TTL_S]:
            await self.pages.pop(key).context.close()


class BrowserPool:
    def __init__(self):
        self.slots: dict[tuple[str, bool], list[_Slot]] = {}
        self.sem = asyncio.Semaphore(config.CONCURRENCY)
        self.waiting = 0
        self.busy = 0
        self.per_client: dict[str, int] = {}  # in flight + queued
        self.avg_s = 0.5  # rolling average render duration, for Retry-After

    @staticmethod
    def key(browser_name: str, cleartype: bool) -> tuple[str, bool]:
        # ClearType toggle only matters for Chromium
        return (browser_name, cleartype if browser_name == "chromium" else True)

    def retry_after(self) -> int:
        return max(1, math.ceil((self.waiting + 1) * self.avg_s / config.CONCURRENCY))

    async def _pick(self, key: tuple[str, bool], page_key: tuple | None) -> _Slot:
        # Serialize launches so concurrent cold requests don't start duplicate browsers
        async with _browser_lock:
            slots = [s for s in self.slots.get(key, []) if s.alive or s.active]
            self.slots[key] = slots
            usable = [s for s in slots if s.alive and not s.retiring]
            # Prefer a browser that already has these fonts loaded in an idle page
            for s in usable:
                if page_key is not None and s.has_idle_page(page_key):
                    return s
            if len(usable) < config.POOL_SIZE and all(s.active for s in usable):
                slot = _Slot(await launch_browser(*key))
                slots.append(slot)
                return slot
            return min(usable, key=lambda s: s.active)

    @asynccontextmanager
    async def acquire(
        self, browser_name: str, cleartype: bool, timing: dict[str, float],
        page_key: tuple | None = None, client_id: str = "-",
    ):
        """Wait for a render slot and yield the least busy browser for the key."""
        if self.sem.locked() and self.waiting >= config.QUEUE_MAX:
            raise PoolBusy(self.retry_after())
        if self.per_client.get(client_id, 0) >= config.CLIENT_MAX:
            raise PoolBusy(self.retry_after(), "client_limit")
        self.per_client[client_id] = self.per_client.get(client_id, 0) + 1
        try:
            async with self._acquire_slot(browser_name, cleartype, timing, page_key) as slot:
                yield slot
        finally:
            self.per_client[client_id] -= 1
            if not self.per_client[client_id]:
                del self.per_client[client_id]

    @asynccontextmanager
    async def _acquire_slot(self, browser_name, cleartype, timing, page_key):
        t0 = time.perf_counter()
        self.waiting += 1
        try:
            await self.sem.acquire()
        finally:
            self.waiting -= 1
        timing["queue"] = time.perf_counter() - t0
        slot = None
        self.busy += 1
        t1 = time.perf_counter()
        try:
            slot = await self._pick(self.key(browser_name, cleartype), page_key)
            slot.active += 1
            yield slot
        finally:
            self.busy -= 1
            self.sem.release()
            if slot is not None:
                slot.active -= 1
                slot.renders += 1
                if slot.renders >= config.RECYCLE_AFTER:
                    slot.retiring = True
                if slot.retiring and slot.active == 0 and slot.alive:
                    asyncio.create_task(self._close(slot))
                self.avg_s = 0.8 * self.avg_s + 0.2 * (time.perf_counter() - t1)

    @staticmethod
    async def _close(slot: _Slot):
        try:
            await slot.browser.close()
        except Exception:
            pass

    async def warm_up(self):
        """Launch config.POOL_SIZE browsers for each warm key and render a trivial page."""
        for key in config.WARM_KEYS:
            for _ in range(config.POOL_SIZE):
                async with _browser_lock:
                    slot = _Slot(await launch_browser(*key))
                    self.slots.setdefault(key, []).append(slot)
                context = await slot.browser.new_context()
                try:
                    page = await context.new_page()
                    await page.set_content("<p>warm-up</p>")
                    await page.screenshot(type="png")
                finally:
                    await context.close()

    async def janitor(self, font_cache: FontCache, interval: float = 60):
        """Periodically drop idle cached pages and fonts (fonts don't linger in memory)."""
        while True:
            await asyncio.sleep(interval)
            now = time.monotonic()
            font_cache.sweep(now)
            for slots in list(self.slots.values()):
                for slot in slots:
                    if slot.alive:
                        try:
                            await slot.sweep(now)
                        except Exception:
                            pass

    async def close(self):
        for slots in self.slots.values():
            for slot in slots:
                if slot.alive:
                    await slot.browser.close()
        self.slots.clear()

    def info(self) -> dict:
        return {
            "browsers": config.POOL_SIZE,
            "concurrency": config.CONCURRENCY,
            "busy": self.busy,
            "queued": self.waiting,
            "queue_max": config.QUEUE_MAX,
            "client_max": config.CLIENT_MAX,
            "timeout_s": config.TIMEOUT_S,
            "recycle_after": config.RECYCLE_AFTER,
            "page_cache": {
                "per_browser": config.PAGE_CACHE,
                "ttl_s": config.PAGE_TTL_S,
                "pages": sum(len(s.pages) for slots in self.slots.values() for s in slots),
            },
            "live": {
                f"{name}(ct={ct})": sum(1 for s in slots if s.alive)
                for (name, ct), slots in self.slots.items()
            },
        }

    def slots_info(self) -> list[dict]:
        """Per-browser details for the web admin."""
        return [
            {"key": f"{name}(ct={ct})", "alive": s.alive, "active": s.active,
             "renders": s.renders, "retiring": s.retiring, "pages": len(s.pages),
             "version": s.browser.version if s.alive else None}
            for (name, ct), slots in self.slots.items() for s in slots
        ]

    def recycle(self, browser_name: str | None = None) -> int:
        """Retire every live browser (of one engine, if given): idle ones close now, busy
        ones after their render. The next requests launch fresh browsers."""
        n = 0
        for (name, _ct), slots in self.slots.items():
            if browser_name and name != browser_name:
                continue
            for s in slots:
                if s.alive and not s.retiring:
                    s.retiring = True
                    n += 1
                    if s.active == 0:
                        asyncio.create_task(self._close(s))
        return n

    def live_browsers(self) -> dict[tuple[str, bool], Browser]:
        """First live browser per key (for /info)."""
        out = {}
        for key, slots in self.slots.items():
            for s in slots:
                if s.alive:
                    out[key] = s.browser
                    break
        return out



class FontNotLoaded(Exception):
    """The browser rejected an embedded font; carries 0-based font indices."""

    def __init__(self, indices: list[int]):
        super().__init__(indices)
        self.indices = indices


# Starts the load of every @font-face and waits for it. document.fonts.ready
# alone is not enough: it is a Promise (wait_for_function took it as truthy at
# once) and it does not cover faces whose load has not started yet, so a fresh
# page could be shot with the fallback font. Returns the faces that failed.
_LOAD_FONTS_JS = """async () => {
    const faces = [...document.fonts];
    const res = await Promise.allSettled(faces.map(f => f.load()));
    await document.fonts.ready;
    return faces.map((f, i) => ({
        family: f.family.replace(/^["']|["']$/g, ''),
        status: f.status,
        error: res[i].status === 'rejected' ? String(res[i].reason) : '',
    })).filter(f => f.status !== 'loaded');
}"""


_SWAP_JS = """([style, body]) => {
    document.getElementById('st').textContent = style;
    document.body.innerHTML = body;
}"""


async def render_screenshot(
    parts: PageParts,
    scale_factor: float,
    viewport_width: int,
    slot: _Slot,
    delay_font: int = 0,
    delay_resize: int = 0,
    collect_geometry: bool = False,
    timing: dict[str, float] | None = None,
) -> tuple[bytes, list[dict] | None]:
    if timing is None:
        timing = {}
    t0 = time.perf_counter()
    page_key = (scale_factor, parts.fonts_key)

    cached = slot.take_page(page_key) if config.PAGE_CACHE else None
    if cached is not None:
        # Same fonts already loaded: swap only the style and body
        context, page = cached.context, cached.page
        timing["_reused"] = True
    else:
        context = await slot.browser.new_context(
            viewport={"width": viewport_width, "height": 800},
            device_scale_factor=scale_factor,
        )
    keep = False
    try:
        if cached is not None:
            await page.set_viewport_size({"width": viewport_width, "height": 800})
            await page.evaluate(_SWAP_JS, [parts.style, parts.body])
        else:
            page = await context.new_page()
            # Load HTML with embedded fonts
            await page.set_content(parts.html(), wait_until="load")

        failed = await page.evaluate(_LOAD_FONTS_JS)
        if failed:
            index = {}
            for i, f in enumerate(parts.families):
                index.setdefault(f, i)  # a family shared by several rows: its first font
            indices = sorted({i for f in failed for i, fam in enumerate(parts.families)
                              if fam == f["family"]})
            # positions and errors only: font names and hashes are never logged
            config.log.warning(
                "font load failed (%s page): %s", "reused" if cached else "new",
                "; ".join(f"#{index.get(f['family'], '?')} {f['status']} {f['error']}" for f in failed))
            raise FontNotLoaded(indices)
        if delay_font > 0:
            await page.wait_for_timeout(delay_font)

        # Fit viewport to actual body size (body uses width: fit-content)
        body_size = await page.evaluate(
            "() => { const r = document.body.getBoundingClientRect();"
            " return {w: Math.ceil(r.width), h: Math.ceil(r.height)}; }"
        )
        fit_w = max(1, body_size["w"])
        fit_h = max(1, body_size["h"])
        await page.set_viewport_size({"width": fit_w, "height": fit_h})
        if delay_resize > 0:
            await page.wait_for_timeout(delay_resize)

        t1 = time.perf_counter()
        timing["load"] = t1 - t0

        # Take screenshot
        screenshot = await page.screenshot(full_page=True, type="png")
        t2 = time.perf_counter()
        timing["shot"] = t2 - t1

        geometry = None
        if collect_geometry:
            geometry = await page.evaluate(GEOMETRY_JS)
            geometry = scale_geometry(geometry, scale_factor)
            timing["geom"] = time.perf_counter() - t2
        keep = config.PAGE_CACHE > 0 and not slot.retiring
    finally:
        if keep:
            await slot.put_page(page_key, context, page)
        else:
            if cached is not None:
                slot.drop_page(page_key, context)
            await context.close()
    return screenshot, geometry



async def browsers_installed() -> dict[str, bool]:
    """Check which Playwright browsers are installed for the current OS user.

    Uses the global async Playwright instance to read ``executable_path``
    and checks whether the binary actually exists on disk.
    """
    pw = await ensure_playwright()
    result = {}
    for name in config.SUPPORTED_BROWSERS:
        exe = getattr(pw, name).executable_path
        result[name] = os.path.isfile(exe)
    return result
