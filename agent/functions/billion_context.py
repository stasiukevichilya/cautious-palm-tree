"""
title: Billion context
description: Toggle in the chat: route this chat through the billion-context compression proxy (bili.<model>).
version: 1.0.0
"""
# Installed by `make agent-functions` (agent/functions/install.py). The bili.<model> copies come from the
# http://bili:8787/bili/<upstream> connections with prefix_id "bili" in compose.yaml.

from pydantic import BaseModel, Field


class Filter:
    class Valves(BaseModel):
        priority: int = Field(default=0, description="Filter order")
        prefix: str = Field(default="bili.", description="Model id prefix of the billion-context connections")

    def __init__(self):
        self.valves = self.Valves()
        self.toggle = True  # shown as a switch next to the chat input
        self.icon = (
            "data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAy"
            "NCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSJjdXJyZW50Q29sb3IiIHN0cm9rZS13aWR0aD0iMS41Ij48cGF0aCBkPSJNNCA2aDE2TTQgMTJo"
            "MTBNNCAxOGg2Ii8+PHBhdGggZD0iTTE3IDE1bDMgM2wtMyAzIi8+PC9zdmc+"
        )

    async def inlet(self, body: dict, __request__=None, __metadata__=None, __event_emitter__=None) -> dict:
        model = body.get("model") or ""
        if not model.startswith(self.valves.prefix):
            target = self.valves.prefix + model
            if __request__ is not None and target in (__request__.app.state.MODELS or {}):
                body["model"] = target
            elif __event_emitter__:
                await __event_emitter__({"type": "status", "data": {
                    "description": f"Billion context: у модели {model} нет bili-подключения, запрос идёт напрямую",
                    "done": True}})
                return body
        # billion-context keeps its compression state per conversation; prompt_cache_key is the
        # OpenAI-wire field it reads the conversation id from (Open WebUI sends no session header).
        chat_id = (__metadata__ or {}).get("chat_id")
        if chat_id:
            body["prompt_cache_key"] = f"owui-{chat_id}"
        return body
