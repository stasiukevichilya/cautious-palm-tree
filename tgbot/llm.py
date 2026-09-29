"""Proxy to whichever llama.cpp server (OpenAI-compatible API) is currently running."""
import json
import time

import httpx


class Unavailable(Exception):
    pass


class LLM:
    def __init__(self, client=None, cache_seconds=10):
        self.client = client or httpx.AsyncClient()
        self.cache_seconds = cache_seconds
        self.cached = (0.0, None, None)

    async def active(self, backends):
        """(name, base_url) of the first healthy backend; cached briefly to avoid probing on every message."""
        checked, key, found = self.cached
        if key == backends and time.monotonic() - checked < self.cache_seconds and found:
            return found
        found = None
        for name, url in backends.items():
            try:
                response = await self.client.get(f"{url}/health", timeout=3)
            except httpx.HTTPError:
                continue
            if response.status_code == 200:
                found = (name, url)
                break
        self.cached = (time.monotonic(), dict(backends), found)
        if not found:
            raise Unavailable("LLM не запущена: выполните make qwen или make gemma")
        return found

    async def stream(self, backends, messages, max_tokens, temperature, timeout):
        """Yield text deltas of a chat completion."""
        name, url = await self.active(backends)
        payload = {"messages": messages, "stream": True, "max_tokens": max_tokens, "temperature": temperature}
        try:
            async with self.client.stream("POST", f"{url}/v1/chat/completions", json=payload,
                                          timeout=httpx.Timeout(timeout, connect=5)) as response:
                if response.status_code != 200:
                    body = (await response.aread()).decode(errors="replace")[:300]
                    raise Unavailable(f"{name}: HTTP {response.status_code} {body}")
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    choices = json.loads(data).get("choices") or [{}]
                    delta = choices[0].get("delta", {}).get("content")
                    if delta:
                        yield delta
        except httpx.HTTPError as error:
            self.cached = (0.0, None, None)
            raise Unavailable(f"{name}: {type(error).__name__}") from error
