"""
title: Context usage
description: Live and per-answer context statistics for the context ring and the per-message lines.
version: 2.4.0
"""
# Installed by `make agent-functions` as a global filter. After each answer it stores
# message.contextStats (window, this turn's prompt / cache / generated / KV total, speed), which
# agent/webui/loader.js shows as the ring + popover next to the model selector, like the llama.cpp UI.
#
# Open WebUI sums usage over all model calls of a turn (tool calls), but keeps llama.cpp timings of
# the last call: KV = cache_n + prompt_n + predicted_n is exact. Through billion-context there are no
# timings, so KV falls back to prompt + completion (an overestimate when the turn called tools).

import time

import aiohttp
from pydantic import BaseModel, Field


class Filter:
    class Valves(BaseModel):
        priority: int = Field(default=100, description="Run after other outlet filters")
        servers: dict = Field(
            default={"qwen3.8-27b": "http://qwen:8080", "qwen3.8-27b-mtp": "http://qwen-mtp:8080",
                     "bonsai2-27b-uc-mtp": "http://bonsai-mtp:8080",
                     "qwen3.8-flash-next-iq2_xs": "http://strata:8080"},
            description="Model id -> llama.cpp server, for the live context size",
        )
        windows: dict = Field(
            default={"qwen3.8-27b": 196608, "qwen3.8-27b-mtp": 163840, "bonsai2-27b-uc-mtp": 262144,
                     "qwen3.8-flash-next-iq2_xs": 65536},
            description="Fallback context windows",
        )
        show_status: bool = Field(default=False, description="Also write a 'Контекст: X / N' line above the answer")
        live_timings: bool = Field(default=True, description="Ask llama.cpp for timings in every stream chunk (live line)")

    def __init__(self):
        self.valves = self.Valves()
        self.cache = {}  # model -> (time, n_ctx)

    async def window(self, model):
        cached = self.cache.get(model)
        if cached and time.monotonic() - cached[0] < 300:
            return cached[1]
        n_ctx = None
        url = self.valves.servers.get(model)
        if url:
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as session:
                    async with session.get(f"{url}/props") as response:
                        props = await response.json()
                n_ctx = (props.get("default_generation_settings") or {}).get("n_ctx")
            except Exception:
                pass
        n_ctx = n_ctx or self.valves.windows.get(model)
        if n_ctx:
            self.cache[model] = (time.monotonic(), n_ctx)
        return n_ctx

    async def inlet(self, body: dict) -> dict:
        # llama.cpp: timings in every chunk and prompt-processing progress chunks; Open WebUI relays
        # them to the browser as chat:completion usage events, which loader.js shows live.
        # Not through billion-context: its stream rewrite drops timings anyway.
        if self.valves.live_timings and not str(body.get("model") or "").startswith(("bili.", "hb.")):
            body["timings_per_token"] = True
            body["return_progress"] = True
        return body

    @staticmethod
    def turn_stats(usage):
        prompt_total = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        completion_total = usage.get("completion_tokens") or usage.get("output_tokens") or 0
        cached_total = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        timed = "prompt_n" in usage and "predicted_n" in usage
        if timed:  # llama.cpp timings: the last call of the turn
            prompt = (usage.get("cache_n") or 0) + (usage.get("prompt_n") or 0)
            generated = usage.get("predicted_n") or 0
        else:
            prompt, generated = prompt_total, completion_total
        return {
            "prompt": prompt,
            "generated": generated,
            "kv": prompt + generated,
            "exact": timed,
            # Whole turn (all calls): what the server actually evaluated and generated.
            "evaluated": max(0, prompt_total - cached_total),
            "completion": completion_total,
            "speed": usage.get("predicted_per_second"),
            "prompt_speed": usage.get("prompt_per_second"),
            # Per-message line (llama.cpp style); timings are those of the turn's last model call.
            "prompt_n": usage.get("prompt_n"),
            "prompt_ms": usage.get("prompt_ms"),
            "predicted_n": usage.get("predicted_n"),
            "predicted_ms": usage.get("predicted_ms"),
        }

    async def outlet(self, body: dict, __event_emitter__=None, __model__=None, __request__=None) -> dict:
        last = next((m for m in reversed(body.get("messages") or []) if m.get("role") == "assistant"), None)
        usage = (last or {}).get("usage") or ((last or {}).get("info") or {}).get("usage") or {}
        if not last or not (usage.get("prompt_tokens") or usage.get("input_tokens")):
            return body
        model = body.get("model") or (__model__ or {}).get("id") or ""
        # The Billion context / Headroom toggles reroute in their inlets, but the outlet still sees the
        # chat's model id. With both on, the request goes through hb.<model>: headroom -> billion-context.
        filter_ids = body.get("filter_ids") or []
        bili = model.startswith(("bili.", "hb.")) or "billion_context" in filter_ids
        headroom = model.startswith(("hr.", "hb.")) or "headroom" in filter_ids
        # The Headroom filter skips the proxy while it is down (or has no hr. copy) and notes the chat.
        if headroom and __request__ is not None and body.get("chat_id") in getattr(
                __request__.app.state, "headroom_skipped", ()):
            headroom = False
        model = model.removeprefix("bili.").removeprefix("hr.").removeprefix("hb.")
        stats = {"model": model, "window": await self.window(model), "bili": bili, "headroom": headroom,
                 "at": int(time.time()),
                 **self.turn_stats(usage)}

        chat_id, message_id = body.get("chat_id"), last.get("id") or body.get("id")
        if chat_id and message_id and not str(chat_id).startswith("local:"):
            from open_webui.models.chats import Chats

            await Chats.upsert_message_to_chat_by_id_and_message_id(
                chat_id, message_id, {"contextStats": stats}, touch=False)

        if self.valves.show_status and __event_emitter__:
            number = lambda n: f"{n:,}".replace(",", " ")
            text = f"Контекст: {number(stats['kv'])}"
            if stats["window"]:
                text += f" / {number(stats['window'])} ({100 * stats['kv'] / stats['window']:.0f}%)"
            if bili:
                text += " · billion-context"
            if headroom:
                text += " · Headroom"
            await __event_emitter__({"type": "status", "data": {"description": text, "done": True}})
        return body
