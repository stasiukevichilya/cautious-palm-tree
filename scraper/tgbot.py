"""Delivery of alerts and reports to the tgbot service (POST /api/notify)."""
import logging

import httpx

log = logging.getLogger("scraper.tgbot")

# Telegram limits a message to 4096 characters; leave margin for encoding.
CHUNK = 3500


class Tgbot:
    def __init__(self, url, admin_key, timeout=30.0):
        self.url = url.rstrip("/")
        self.admin_key = admin_key
        self.client = httpx.AsyncClient(timeout=timeout)

    async def close(self):
        await self.client.aclose()

    async def notify(self, text):
        for start in range(0, len(text), CHUNK):
            response = await self.client.post(
                f"{self.url}/api/notify",
                json={"text": text[start:start + CHUNK]},
                headers={"Authorization": f"Bearer {self.admin_key}"})
            response.raise_for_status()

    async def notify_user(self, tg_id, text):
        """Deliver to one user; a refusal (blocked user, bot stopped) raises for the caller."""
        for start in range(0, len(text), CHUNK):
            response = await self.client.post(
                f"{self.url}/api/notify-user",
                json={"tg_id": tg_id, "text": text[start:start + CHUNK]},
                headers={"Authorization": f"Bearer {self.admin_key}"})
            response.raise_for_status()
