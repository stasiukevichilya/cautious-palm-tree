"""Notifications to the tgbot service (POST /api/notify)."""
import logging

import httpx

log = logging.getLogger("torrent.tgbot")


class Tgbot:
    def __init__(self, url, admin_key, timeout=30.0):
        self.url = url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=timeout)
        self.headers = {"Authorization": f"Bearer {admin_key}"}

    async def close(self):
        await self.client.aclose()

    async def notify(self, text):
        response = await self.client.post(f"{self.url}/api/notify", json={"text": text},
                                          headers=self.headers)
        response.raise_for_status()
