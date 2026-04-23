"""Playwright-based fallback renderer.

Invoked only when the HTTP path returns BLOCKED (captcha) or an uncertain
result. A real headless browser:
  1. Runs JS so "Currently unavailable" and buy-button DOM hydrate correctly
  2. Solves soft bot-checks that httpx can't (cookies, JS challenges)
  3. Can use a proxy per marketplace for residential IP rotation

Playwright browsers must be installed once:
    playwright install chromium
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Optional

from .marketplaces import Marketplace


@dataclass
class BrowserFetchResult:
    status_code: int
    html: str
    final_url: str
    error: str | None = None


_STEALTH_INIT = """
// Minimal stealth: hide webdriver, spoof plugins, fix languages
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
window.chrome = { runtime: {} };
"""


class BrowserFetcher:
    """Shared Playwright browser instance (one per process)."""

    def __init__(self, headless: bool = True, proxy: str | None = None):
        self.headless = headless
        self.proxy = proxy
        self._playwright = None
        self._browser = None
        self._lock = asyncio.Lock()

    async def _ensure(self):
        if self._browser is not None:
            return
        async with self._lock:
            if self._browser is not None:
                return
            try:
                from playwright.async_api import async_playwright
            except ImportError as e:
                raise RuntimeError(
                    "Playwright not installed. Run: pip install playwright && playwright install chromium"
                ) from e
            self._playwright = await async_playwright().start()
            launch_args = {"headless": self.headless}
            if self.proxy:
                launch_args["proxy"] = {"server": self.proxy}
            self._browser = await self._playwright.chromium.launch(**launch_args)

    async def close(self):
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    async def fetch(
        self,
        marketplace: Marketplace,
        url: str,
        timeout_s: float = 30.0,
    ) -> BrowserFetchResult:
        await self._ensure()
        context = await self._browser.new_context(
            locale=marketplace.accept_language.split(",")[0],
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            extra_http_headers={"Accept-Language": marketplace.accept_language},
            viewport={"width": 1366, "height": 900},
        )
        try:
            await context.add_init_script(_STEALTH_INIT)
            page = await context.new_page()
            try:
                response = await page.goto(url, wait_until="domcontentloaded",
                                           timeout=int(timeout_s * 1000))
                # Let buy-box / price widgets hydrate
                try:
                    await page.wait_for_selector(
                        "#productTitle, #dp-container, #centerCol, "
                        "#buy-now-button, #add-to-cart-button",
                        timeout=5000,
                    )
                except Exception:
                    pass
                # Small human-like idle
                await asyncio.sleep(random.uniform(0.6, 1.4))
                html = await page.content()
                final_url = page.url
                status = response.status if response else 0
                return BrowserFetchResult(status_code=status, html=html, final_url=final_url)
            finally:
                await page.close()
        except Exception as e:
            return BrowserFetchResult(
                status_code=0, html="", final_url=url,
                error=f"{type(e).__name__}: {e}",
            )
        finally:
            await context.close()


# Singleton accessor — lazily constructed so that systems without playwright
# installed can still use the HTTP-only fast path.
_fetcher: Optional[BrowserFetcher] = None


def get_fetcher(proxy: str | None = None, headless: bool = True) -> BrowserFetcher:
    global _fetcher
    if _fetcher is None:
        _fetcher = BrowserFetcher(headless=headless, proxy=proxy)
    return _fetcher


async def shutdown():
    global _fetcher
    if _fetcher is not None:
        await _fetcher.close()
        _fetcher = None
