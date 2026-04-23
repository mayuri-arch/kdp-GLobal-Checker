"""Hybrid marketplace checker: httpx fast path + Playwright fallback.

Strategy:
  1. Try httpx (fast, cheap).
  2. If result is BLOCKED or (optionally) NOT_FOUND without ASIN confirmation,
     escalate to Playwright for a rendered re-check.
  3. Retry transient failures with exponential backoff + jitter.
"""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Iterable

import httpx

from .detector import AvailabilityStatus, PageAnalysis, analyze_page
from .marketplaces import MARKETPLACES, MARKETPLACES_BY_CODE, Marketplace


_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.6 Safari/605.1.15",
]


def _build_headers(marketplace: Marketplace) -> dict[str, str]:
    return {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,*/*;q=0.8"),
        "Accept-Language": marketplace.accept_language,
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "DNT": "1",
    }


@dataclass
class CheckResult:
    marketplace: Marketplace
    url: str
    analysis: PageAnalysis
    attempts: int = 1
    elapsed_ms: int = 0
    used_browser: bool = False

    def to_dict(self) -> dict:
        return {
            "code": self.marketplace.code,
            "country": self.marketplace.country,
            "domain": f"amazon.{self.marketplace.domain}",
            "url": self.url,
            "attempts": self.attempts,
            "elapsed_ms": self.elapsed_ms,
            "used_browser": self.used_browser,
            **self.analysis.to_dict(),
        }


@dataclass
class MarketplaceChecker:
    concurrency: int = 4
    max_retries: int = 3
    timeout_s: float = 20.0
    min_delay_ms: int = 400
    max_delay_ms: int = 1200
    http2: bool = True
    proxy: str | None = None
    follow_redirects: bool = True
    use_browser_fallback: bool = True
    browser_proxy: str | None = None

    _semaphore: asyncio.Semaphore = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._semaphore = asyncio.Semaphore(self.concurrency)

    async def _fetch_http(
        self, client: httpx.AsyncClient, marketplace: Marketplace, url: str
    ) -> tuple[int, str, str]:
        resp = await client.get(url, headers=_build_headers(marketplace), timeout=self.timeout_s)
        return resp.status_code, resp.text, str(resp.url)

    async def _fetch_browser(self, marketplace: Marketplace, url: str):
        from .browser import get_fetcher
        fetcher = get_fetcher(proxy=self.browser_proxy)
        return await fetcher.fetch(marketplace, url, timeout_s=self.timeout_s + 10)

    async def check_one(
        self, client: httpx.AsyncClient, marketplace: Marketplace, asin: str,
    ) -> CheckResult:
        url = marketplace.product_url(asin)
        loop = asyncio.get_running_loop()
        start = loop.time()
        attempts = 0
        analysis: PageAnalysis | None = None
        used_browser = False

        async with self._semaphore:
            await asyncio.sleep(random.uniform(0, self.min_delay_ms / 1000))

            # ---- HTTP path with retries ----
            for attempt in range(1, self.max_retries + 1):
                attempts = attempt
                try:
                    status, body, final_url = await self._fetch_http(client, marketplace, url)
                    analysis = analyze_page(marketplace, body, status, final_url, asin, source="http")
                    if analysis.status in (AvailabilityStatus.BLOCKED, AvailabilityStatus.ERROR) \
                            and attempt < self.max_retries:
                        await asyncio.sleep((2 ** attempt) + random.uniform(0, 1))
                        continue
                    break
                except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPError) as e:
                    if attempt < self.max_retries:
                        await asyncio.sleep((2 ** attempt) + random.uniform(0, 1))
                        continue
                    analysis = PageAnalysis(
                        status=AvailabilityStatus.ERROR, reason="Transport error",
                        error=f"{type(e).__name__}: {e}", source="http",
                    )
                    break

            # ---- Browser fallback ----
            if self.use_browser_fallback and analysis and analysis.status in (
                AvailabilityStatus.BLOCKED, AvailabilityStatus.ERROR,
            ):
                try:
                    fb = await self._fetch_browser(marketplace, url)
                    if fb.error:
                        if analysis.status == AvailabilityStatus.ERROR:
                            analysis.error = (analysis.error or "") + f" | browser: {fb.error}"
                    else:
                        analysis = analyze_page(
                            marketplace, fb.html, fb.status_code, fb.final_url,
                            asin, source="browser",
                        )
                        used_browser = True
                except RuntimeError as e:
                    # Playwright not installed — leave HTTP result alone, add a note.
                    analysis.reason = (analysis.reason or "") + f" (browser fallback unavailable: {e})"

            # polite pacing
            await asyncio.sleep(random.uniform(
                self.min_delay_ms / 1000, self.max_delay_ms / 1000
            ))

        elapsed = int((loop.time() - start) * 1000)
        assert analysis is not None
        return CheckResult(
            marketplace=marketplace, url=url, analysis=analysis,
            attempts=attempts, elapsed_ms=elapsed, used_browser=used_browser,
        )

    async def run(
        self, asin: str,
        marketplaces: Iterable[Marketplace] | None = None,
        progress_cb=None,
    ) -> list[CheckResult]:
        targets = list(marketplaces) if marketplaces is not None else MARKETPLACES
        limits = httpx.Limits(
            max_connections=self.concurrency * 2,
            max_keepalive_connections=self.concurrency,
        )
        async with httpx.AsyncClient(
            http2=self.http2, follow_redirects=self.follow_redirects,
            limits=limits, proxy=self.proxy,
        ) as client:
            tasks = [
                asyncio.create_task(self.check_one(client, m, asin), name=m.code)
                for m in targets
            ]
            results: list[CheckResult] = []
            for coro in asyncio.as_completed(tasks):
                r = await coro
                results.append(r)
                if progress_cb:
                    progress_cb(r)
            order = {m.code: i for i, m in enumerate(targets)}
            results.sort(key=lambda r: order[r.marketplace.code])
            return results


def check_asin(asin: str, codes: list[str] | None = None, **kwargs) -> list[CheckResult]:
    targets = (
        [MARKETPLACES_BY_CODE[c.upper()] for c in codes] if codes else None
    )
    checker = MarketplaceChecker(**kwargs)
    return asyncio.run(checker.run(asin, targets))
