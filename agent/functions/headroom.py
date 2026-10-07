"""
title: Headroom
description: Toggle in the chat: route this chat through the Headroom prompt-compression proxy (hr.<model>).
version: 1.1.0
"""
# Installed by `make agent-functions` (agent/functions/install.py). The hr.<model> copies come from the
# http://headroom:8787/v1 connections with prefix_id "hr" in compose.yaml, hb.<model> from those with "hb".

import time

import aiohttp
from pydantic import BaseModel, Field

# The hr. / hb. copies stay in the model list while headroom is down (their model ids are fixed in the
# connection config), so routing is decided by a health probe, cached per process.
_health = {"ok": False, "at": 0.0}


class Filter:
    class Valves(BaseModel):
        priority: int = Field(default=1, description="Filter order: after Billion context")
        prefix: str = Field(default="hr.", description="Model id prefix of the Headroom connections")
        chain_prefix: str = Field(default="hb.", description="Model id prefix of the Headroom -> billion-context connections")
        health_url: str = Field(default="http://headroom:8787/health", description="Headroom health check")
        health_ttl: int = Field(default=15, description="Seconds to reuse a health check result")

    def __init__(self):
        self.valves = self.Valves()
        self.toggle = True  # shown as a switch next to the chat input
        self.icon = (
            "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAy"
            "NCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSJjdXJyZW50Q29sb3IiIHN0cm9rZS13aWR0aD0iMS41Ij48cGF0aCBkPSJNNCA0djE2TTIwIDR2"
            "MTZNOCAxMmg4TTEwIDlsLTIgMyAyIDNNMTQgOWwyIDMtMiAzIi8+PC9zdmc+"
        )

    async def _healthy(self) -> bool:
        if time.monotonic() - _health["at"] < self.valves.health_ttl:
            return _health["ok"]
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1.5)) as session:
                async with session.get(self.valves.health_url) as response:
                    ok = response.status == 200
        except Exception:
            ok = False
        _health.update(ok=ok, at=time.monotonic())
        return ok

    async def inlet(self, body: dict, __request__=None, __metadata__=None, __event_emitter__=None) -> dict:
        model = body.get("model") or ""
        # Chats whose last request skipped Headroom: context_usage leaves the Headroom badge off for them.
        skipped = getattr(__request__.app.state, "headroom_skipped", None) if __request__ is not None else None
        if __request__ is not None and skipped is None:
            skipped = __request__.app.state.headroom_skipped = set()
        chat_id = (__metadata__ or {}).get("chat_id")
        if model.startswith((self.valves.prefix, self.valves.chain_prefix)):
            return body
        # Both toggles on: Billion context has already switched to bili.<model>; hb.<model> is headroom
        # in front of billion-context.
        if model.startswith("bili."):
            target = self.valves.chain_prefix + model.removeprefix("bili.")
        else:
            target = self.valves.prefix + model
        if __request__ is None or target not in (__request__.app.state.MODELS or {}):
            if skipped is not None and chat_id:
                skipped.add(chat_id)
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {
                    "description": f"Headroom: у модели {model} нет подключения {target}, запрос идёт без Headroom",
                    "done": True}})
            return body
        if not await self._healthy():
            if skipped is not None and chat_id:
                skipped.add(chat_id)
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {
                    "description": "Headroom недоступен, запрос идёт без Headroom", "done": True}})
            return body
        if skipped is not None:
            skipped.discard(chat_id)
        body["model"] = target
        return body
