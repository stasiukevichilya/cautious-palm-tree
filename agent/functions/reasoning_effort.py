"""
title: Reasoning effort
description: Passes the chat's reasoning effort (the selector next to the model picker) to the model as chat template kwargs.
version: 1.0.0
"""
# Installed by `make agent-functions` as a global filter. The selector in agent/webui/loader.js sets
# params.reasoning_effort on the chat request (a model's own params value applies when it is on "auto"),
# and Open WebUI sends it as the top-level OpenAI field. The local servers read the thinking level from
# the chat template instead: Qwen3.8 templates take reasoning_effort low / medium / xhigh and
# enable_thinking=false, through chat_template_kwargs (llama.cpp --jinja, vLLM, Strata). So the "request"
# hook (right before every upstream call, tool-loop steps included) moves the field there. The top-level
# field stays as null: some servers forward it to the template too ("high" is not a template value), and
# Open WebUI's OpenAI router re-adds a model's stored reasoning_effort only when the key is missing.

from pydantic import BaseModel, Field

TEMPLATE_KWARGS = {
    "none": {"enable_thinking": False},
    "low": {"reasoning_effort": "low"},
    "medium": {"reasoning_effort": "medium"},
    "high": {"reasoning_effort": "xhigh"},
    "xhigh": {"reasoning_effort": "xhigh"},
}


class Filter:
    class Valves(BaseModel):
        priority: int = Field(default=150, description="Filter order")

    def __init__(self):
        self.valves = self.Valves()

    def request(self, body: dict) -> dict:
        effort = body.get("reasoning_effort")
        kwargs = TEMPLATE_KWARGS.get(str(effort).strip().lower()) if effort is not None else None
        if kwargs is None:
            return body
        body["reasoning_effort"] = None
        existing = body.get("chat_template_kwargs")
        body["chat_template_kwargs"] = {**(existing if isinstance(existing, dict) else {}), **kwargs}
        return body
