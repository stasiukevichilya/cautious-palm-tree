"""Delivery of alerts and reports to the tgbot service (POST /api/notify)."""
import logging

import httpx

log = logging.getLogger("scraper.tgbot")

# Telegram limits a message to 4096 characters; leave margin for encoding.
CHUNK = 3500


class Refused(Exception):
    """The tgbot rejected the recipient (409: no access / no admins); retries cannot succeed."""


class Tgbot:
    def __init__(self, url, admin_key, timeout=30.0):
        self.url = url.rstrip("/")
        self.admin_key = admin_key
        self.client = httpx.AsyncClient(timeout=timeout)

    async def close(self):
        await self.client.aclose()

    async def _post(self, path, payload):
        response = await self.client.post(f"{self.url}{path}", json=payload,
                                          headers={"Authorization": f"Bearer {self.admin_key}"})
        if response.status_code == 409:
            raise Refused(response.text[:200])
        response.raise_for_status()

    async def notify(self, text):
        for start in range(0, len(text), CHUNK):
            await self._post("/api/notify", {"text": text[start:start + CHUNK]})

    async def notify_user(self, tg_id, text):
        """Deliver to one user; a refusal (blocked user, bot stopped) raises for the caller."""
        for start in range(0, len(text), CHUNK):
            await self._post("/api/notify-user", {"tg_id": tg_id, "text": text[start:start + CHUNK]})
