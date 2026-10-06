"""
title: Tool arguments guard
description: Replaces tool-call arguments that are not valid JSON (a call cut off by the context limit) so the chat keeps working.
version: 1.0.0
"""
# Installed by `make agent-functions` as a global filter. A model that runs out of context in the
# middle of a tool call leaves truncated JSON arguments in the chat history. Strata parses the
# arguments of every earlier call (its chat template needs a mapping) and answers HTTP 400
# "Unterminated string ..." to every later request of that chat. The "request" hook runs right
# before each upstream call (the first one and every step of the tool loop) and swaps such
# arguments for valid JSON that tells the model the call was cut off.

import json

from pydantic import BaseModel, Field


class Filter:
    class Valves(BaseModel):
        priority: int = Field(default=200, description="Run after the other request filters")
        keep_chars: int = Field(default=2000, description="How much of the broken arguments to keep for the model")

    def __init__(self):
        self.valves = self.Valves()

    def request(self, body: dict) -> dict:
        for message in body.get("messages") or []:
            for call in message.get("tool_calls") or []:
                function = call.get("function") if isinstance(call, dict) else None
                arguments = function.get("arguments") if isinstance(function, dict) else None
                if not isinstance(arguments, str) or not arguments.strip():
                    continue
                try:
                    json.loads(arguments)
                except ValueError:
                    function["arguments"] = json.dumps(
                        {"_truncated_arguments": arguments[: self.valves.keep_chars],
                         "_note": "This call was cut off (context limit) and was not valid JSON."},
                        ensure_ascii=False)
        return body
