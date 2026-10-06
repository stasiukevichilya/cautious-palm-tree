"""
title: Headroom
description: Toggle in the chat: route this chat through the Headroom prompt-compression proxy (hr.<model>).
version: 1.0.0
"""
# Installed by `make agent-functions` (agent/functions/install.py). The hr.<model> copies come from the
# http://headroom:8787/v1 connections with prefix_id "hr" in compose.yaml, hb.<model> from those with "hb".

from pydantic import BaseModel, Field


class Filter:
    class Valves(BaseModel):
        priority: int = Field(default=1, description="Filter order: after Billion context")
        prefix: str = Field(default="hr.", description="Model id prefix of the Headroom connections")
        chain_prefix: str = Field(default="hb.", description="Model id prefix of the Headroom -> billion-context connections")

    def __init__(self):
        self.valves = self.Valves()
        self.toggle = True  # shown as a switch next to the chat input
        self.icon = (
            "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAy"
            "NCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSJjdXJyZW50Q29sb3IiIHN0cm9rZS13aWR0aD0iMS41Ij48cGF0aCBkPSJNNCA0djE2TTIwIDR2"
            "MTZNOCAxMmg4TTEwIDlsLTIgMyAyIDNNMTQgOWwyIDMtMiAzIi8+PC9zdmc+"
        )

    async def inlet(self, body: dict, __request__=None, __event_emitter__=None) -> dict:
        model = body.get("model") or ""
        if model.startswith((self.valves.prefix, self.valves.chain_prefix)):
            return body
        # Both toggles on: Billion context has already switched to bili.<model>; hb.<model> is headroom
        # in front of billion-context.
        if model.startswith("bili."):
            target = self.valves.chain_prefix + model.removeprefix("bili.")
        else:
            target = self.valves.prefix + model
        if __request__ is None or target not in (__request__.app.state.MODELS or {}):
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {
                    "description": f"Headroom: у модели {model} нет подключения {target}, запрос идёт без Headroom",
                    "done": True}})
            return body
        body["model"] = target
        return body
