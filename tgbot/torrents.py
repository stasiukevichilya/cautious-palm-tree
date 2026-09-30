"""Client for the torrent service (metadata + downloads), addressed inside compose."""
import re

import httpx


class TorrentError(Exception):
    """A user-facing error from the torrent service."""


class InvalidMagnet(TorrentError):
    pass


class Duplicate(TorrentError):
    pass


class NotFound(TorrentError):
    pass


class Timeout(TorrentError):
    pass


def magnet_hash(magnet):
    """Hex hash from a magnet link: btih (40 hex) or btmh (64 hex); None if absent."""
    if not isinstance(magnet, str) or not magnet.strip().lower().startswith("magnet:?"):
        return None
    for prefix, length in (("btih", 40), ("btmh", 64)):
        match = re.search(r"xt=urn:" + prefix + r":([0-9a-fA-F]{" + str(length) + r"})", magnet)
        if match:
            return match.group(1).lower()
    return None


def human_size(value):
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if value < 1024 or unit == "ТБ":
            return f"{int(value)} {unit}" if unit == "Б" else f"{value:.1f} {unit}"
        value /= 1024


class Torrents:
    def __init__(self, url, admin_key, timeout=30.0):
        self.url = url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=timeout)
        self.headers = {"Authorization": f"Bearer {admin_key}"}

    async def close(self):
        await self.client.aclose()

    def _detail(self, response, fallback):
        try:
            return response.json().get("detail") or fallback
        except Exception:
            return fallback

    def _check(self, response):
        if response.status_code == 400:
            raise InvalidMagnet(self._detail(response, "Это не magnet-ссылка."))
        if response.status_code == 404:
            raise NotFound(self._detail(response, "Загрузка не найдена."))
        if response.status_code == 409:
            raise Duplicate(self._detail(response, "Загрузка уже запущена."))
        if response.status_code == 504:
            raise Timeout(self._detail(response, "Не удалось получить метаданные (нет сидов)."))
        if response.status_code >= 400:
            raise TorrentError(f"Сервис: HTTP {response.status_code}.")

    async def metadata(self, magnet):
        response = await self.client.post(f"{self.url}/api/metadata", json={"magnet": magnet},
                                          headers=self.headers)
        self._check(response)
        return response.json()

    async def start(self, magnet):
        response = await self.client.post(f"{self.url}/api/torrents", json={"magnet": magnet},
                                          headers=self.headers)
        self._check(response)
        return response.json()

    async def list(self):
        response = await self.client.get(f"{self.url}/api/torrents", headers=self.headers)
        self._check(response)
        return response.json()

    async def remove(self, info_hash):
        response = await self.client.delete(f"{self.url}/api/torrents/{info_hash}",
                                            headers=self.headers)
        self._check(response)
        return response.json()
