"""Create or update the Open WebUI functions in this directory; run inside agent-webui (make agent-functions).

billion_context: toggle filter, global (the switch appears in every chat, off by default).
headroom: toggle filter, global, the same way (hr.<model>).
context_usage: always-on global filter.
tool_args_guard: always-on global filter (broken tool-call arguments in the history).
reasoning_effort: always-on global filter (the chat's reasoning effort -> chat template kwargs).
The bili.<model>, hr.<model> and hb.<model> copies are hidden from the model picker: the toggles are the way to use them.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8080"
FUNCTIONS = {"billion_context": "Billion context", "headroom": "Headroom", "context_usage": "Context usage",
             "tool_args_guard": "Tool arguments guard", "reasoning_effort": "Reasoning effort"}
PROXY_MODELS = ["qwen3.8-27b", "qwen3.8-27b-mtp", "bonsai2-27b-uc-mtp", "qwen3.8-flash-next-iq2_xs"]
# Model id prefix -> name suffix and the toggle that uses the copy.
PROXIES = {"bili": ("billion-context", "Billion context"), "hr": ("Headroom", "Headroom"),
           "hb": ("Headroom + billion-context", "Billion context + Headroom")}


def call(path, data=None, token=None):
    headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})}
    request = urllib.request.Request(BASE + path, json.dumps(data).encode() if data is not None else None, headers)
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def main():
    token = call("/api/v1/auths/signin", {"email": os.environ["WEBUI_ADMIN_EMAIL"],
                                          "password": os.environ["WEBUI_ADMIN_PASSWORD"]})["token"]
    existing = {f["id"]: f for f in call("/api/v1/functions/", token=token)}
    for function_id, name in FUNCTIONS.items():
        content = (Path(sys.argv[1]) / f"{function_id}.py").read_text()
        form = {"id": function_id, "name": name, "content": content,
                "meta": {"description": content.split("description:", 1)[1].split("\n", 1)[0].strip()}}
        if function_id in existing:
            call(f"/api/v1/functions/id/{function_id}/update", form, token)
        else:
            call("/api/v1/functions/create", form, token)
        state = call(f"/api/v1/functions/id/{function_id}", token=token)
        if not state["is_active"]:
            call(f"/api/v1/functions/id/{function_id}/toggle", {}, token)
        if not state["is_global"]:
            call(f"/api/v1/functions/id/{function_id}/toggle/global", {}, token)
        state = call(f"/api/v1/functions/id/{function_id}", token=token)
        print(f"{function_id}: active={state['is_active']} global={state['is_global']}")

    for prefix, (suffix, toggle) in PROXIES.items():
        for model in PROXY_MODELS:
            model_id = f"{prefix}.{model}"
            form = {"id": model_id, "name": f"{model} ({suffix})", "base_model_id": None, "params": {},
                    "meta": {"hidden": True, "description": f"Used by the {toggle} toggle"}}
            try:
                call(f"/api/v1/models/model?id={model_id}", token=token)
                call(f"/api/v1/models/model/update?id={model_id}", form, token)
            except urllib.error.HTTPError:
                call("/api/v1/models/create", form, token)
        print("hidden:", ", ".join(f"{prefix}.{m}" for m in PROXY_MODELS))


if __name__ == "__main__":
    main()
