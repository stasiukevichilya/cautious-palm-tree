"""Client for the market scraper: per-user search queries and product price tracking."""
import logging

import httpx

log = logging.getLogger("tgbot.market")

# Same noise as the scraper's shared watches: laptops, PC builds, coolers, "I will buy" ads.
NOISE_EXCLUDES = ["ноутбук", "куплю", "купить", "компьютер", "пк", "pc", "сборка", "системн",
                  "охлажд", "охлад", "вентилятор", "heatkiller", "waterblock"]


class Market:
    def __init__(self, url, admin_key, timeout=30.0, client=None):
        self.url = url.rstrip("/")
        self.admin_key = admin_key
        self.client = client or httpx.AsyncClient(timeout=timeout)

    async def close(self):
        await self.client.aclose()

    async def _request(self, method, path, **kwargs):
        response = await self.client.request(
            method, f"{self.url}{path}", headers={"Authorization": f"Bearer {self.admin_key}"}, **kwargs)
        response.raise_for_status()
        return response.json()

    @staticmethod
    def query_watch(query, owner):
        """A kufar full-text search watch for a user's query."""
        return {"source": "kufar", "ref": f"/l?query={query.replace(' ', '+')}&sort=lst.d",
                "label": f"Kufar: {query}", "filter": [query.lower()],
                "exclude": NOISE_EXCLUDES, "params": {"max_pages": 8}, "owner": owner}

    async def find_watch(self, source, ref):
        """A point lookup on the server; 404 means 'no such watch'. Fetching the
        whole watch table here would cost every user's listings per /query."""
        try:
            return await self._request("GET", "/api/watches/find", params={"source": source, "ref": ref})
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 404:
                return None
            raise

    async def create_watch(self, watch):
        return await self._request("POST", "/api/watches", json=watch)

    async def delete_watch(self, watch_id):
        await self._request("DELETE", f"/api/watches/{watch_id}")

    async def subscribe(self, watch_id, tg_id):
        await self._request("POST", f"/api/watches/{watch_id}/subscribe", json={"tg_id": tg_id})

    async def unsubscribe(self, watch_id, tg_id):
        await self._request("DELETE", f"/api/watches/{watch_id}/subscribe", params={"tg_id": tg_id})

    async def my_watches(self, owner):
        return await self._request("GET", "/api/watches", params={"owner": owner})

    async def add_item(self, owner, url):
        return await self._request("POST", "/api/items", json={"owner": owner, "url": url})

    async def my_items(self, owner):
        return await self._request("GET", "/api/items", params={"owner": owner})

    async def delete_item(self, item_id, owner):
        await self._request("DELETE", f"/api/items/{item_id}", params={"owner": owner})

    async def check_item(self, item_id):
        return await self._request("POST", f"/api/items/{item_id}/check")
