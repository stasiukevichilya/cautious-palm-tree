"""Proxy /image to whichever generator is running: SDXL service or Qwen-Image (ComfyUI)."""
import asyncio
import copy
import json
import random
import time

import httpx

from llm import Unavailable


class Images:
    def __init__(self, workflows_path, client=None, poll_seconds=2):
        self.workflows_path = workflows_path
        self.client = client or httpx.AsyncClient()
        self.poll_seconds = poll_seconds

    async def active(self, backends):
        for name, url in backends.items():
            probe = "/health/ready" if name == "sdxl" else "/system_stats"
            try:
                response = await self.client.get(url + probe, timeout=3)
            except httpx.HTTPError:
                continue
            if response.status_code == 200:
                return name, url
        raise Unavailable("Генератор изображений не запущен: выполните make sdxl или make qwen-image")

    async def generate(self, backends, prompt, timeout):
        """Returns (generator name, PNG bytes, seed)."""
        name, url = await self.active(backends)
        seed = random.randrange(2**32)
        deadline = time.monotonic() + timeout
        try:
            if name == "sdxl":
                return name, await self.sdxl(url, prompt, seed, deadline), seed
            return name, await self.comfy(url, prompt, seed, deadline), seed
        except httpx.HTTPError as error:
            raise Unavailable(f"{name}: {type(error).__name__}") from error

    async def wait(self, deadline):
        if time.monotonic() > deadline:
            raise Unavailable("Превышено время ожидания изображения")
        await asyncio.sleep(self.poll_seconds)

    async def sdxl(self, url, prompt, seed, deadline):
        response = await self.client.post(f"{url}/api/generations", json={"prompt": prompt, "seed": seed}, timeout=10)
        if response.status_code == 429:
            raise Unavailable("Очередь SDXL заполнена, повторите позже")
        response.raise_for_status()
        identifier = response.json()["id"]
        while True:
            record = (await self.client.get(f"{url}/api/generations/{identifier}", timeout=10)).json()
            if record["status"] == "completed":
                break
            if record["status"] in ("failed", "cancelled", "interrupted"):
                raise Unavailable(f"SDXL: {record.get('error') or record['status']}")
            await self.wait(deadline)
        image = await self.client.get(f"{url}/api/generations/{identifier}/image", timeout=30)
        image.raise_for_status()
        return image.content

    def comfy_graph(self, workflow, prompt, seed):
        graph = copy.deepcopy(json.loads((self.workflows_path / f"{workflow}.api.json").read_text()))
        graph["4"]["inputs"]["prompt"] = prompt
        graph["6"]["inputs"]["seed"] = seed
        graph["8"]["inputs"]["filename_prefix"] = "tgbot"
        return graph

    async def comfy(self, url, prompt, seed, deadline):
        config = await self.client.get(f"{url}/local-qwen-image/config", timeout=5)
        config.raise_for_status()
        graph = self.comfy_graph(config.json()["workflow"], prompt, seed)
        response = await self.client.post(f"{url}/prompt", json={"prompt": graph, "client_id": "tgbot"}, timeout=10)
        if response.status_code != 200:
            raise Unavailable(f"Qwen-Image: HTTP {response.status_code} {response.text[:300]}")
        identifier = response.json()["prompt_id"]
        while True:
            history = (await self.client.get(f"{url}/history/{identifier}", timeout=10)).json().get(identifier)
            if history:
                if history["status"]["status_str"] != "success":
                    raise Unavailable("Qwen-Image: ошибка генерации, см. make logs-qwen-image")
                break
            await self.wait(deadline)
        info = history["outputs"]["8"]["images"][0]
        image = await self.client.get(f"{url}/view", params=info, timeout=30)
        image.raise_for_status()
        return image.content
