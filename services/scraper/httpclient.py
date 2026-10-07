"""Shared HTTP client: user agent, timeouts, retries, polite pacing between requests."""
import asyncio
import logging
import random
import time

import httpx

log = logging.getLogger("scraper.http")

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
RETRYABLE = (429, 500, 502, 503, 504)


class ScrapeError(Exception):
    """The source answered, but not with usable content (non-2xx without retries left)."""


class Client:
    def __init__(self, delay=1.0, timeout=30.0):
        self.delay = delay
        self._last = 0.0
        self.client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "ru,en"}, timeout=timeout,
            follow_redirects=True)

    async def close(self):
        await self.client.aclose()

    async def _pace(self):
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            await asyncio.sleep(wait)

    async def get_text(self, url, tries=3):
        """GET with pacing and retries on network errors and 429/5xx."""
        await self._pace()
        last_error = None
        for attempt in range(1, tries + 1):
            try:
                response = await self.client.get(url)
            except httpx.TransportError as error:
                last_error = error
                if attempt == tries:
                    raise
                backoff = 2 ** attempt + random.random()
                log.warning("Network error for %s: %s; retrying in %.1fs", url, type(error).__name__, backoff)
                await asyncio.sleep(backoff)
                continue
            self._last = time.monotonic()
            if response.status_code == 200:
                return response.text
            if response.status_code in RETRYABLE and attempt < tries:
                backoff = 2 ** attempt + random.random()
                log.warning("HTTP %s for %s; retrying in %.1fs", response.status_code, url, backoff)
                await asyncio.sleep(backoff)
                continue
            raise ScrapeError(f"HTTP {response.status_code} for {url}")
        raise last_error  # pragma: no cover
