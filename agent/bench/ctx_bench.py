"""Context-compression benchmark: the same scripted agent session direct, through Headroom, billion-context
and Headroom -> billion-context.

Runs inside the compose network (make ctx-bench). The trajectory is fixed: the model answers every step, but
the next scripted tool call and its output are appended regardless, so every route sees the same session.
Costs come from the llama.cpp /metrics deltas, so billion-context's own summary calls are counted too.
At the end four recall questions check facts that sit in early tool outputs.
"""
import argparse
import json
import random
import re
import subprocess
import time
from pathlib import Path

import httpx

SERVERS = {"qwen3.8-27b": "http://qwen:8080", "qwen3.8-27b-mtp": "http://qwen-mtp:8080",
           "bonsai2-27b-uc-mtp": "http://bonsai-mtp:8080", "qwen3.8-flash-next-iq2_xs": "http://strata:8080"}
REPO = Path("/repo")
TOOLS = [
    {"type": "function", "function": {"name": "read_file", "description": "Read a file of the project",
     "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "run", "description": "Run a shell command in the project",
     "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}}},
    {"type": "function", "function": {"name": "http_get", "description": "GET a URL, returns the body",
     "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
]
SYSTEM = ("You are a coding agent working in the project at /repo (a local ML stack: llama.cpp models, a Telegram "
          "bot, Open WebUI). Use the tools to investigate. Be brief between tool calls.")
TASK = ("CI is red after the last merge and the payments worker keeps restarting in staging. Investigate the "
        "repository, the CI results, the issue tracker and the logs, then propose a fix plan. Remember the "
        "concrete identifiers you find: you will be asked about them later.")

# Recall checks: the fact, where it is planted, and accepted answers.
QUESTIONS = [
    ("Which user is assigned to tracker issue #4187, and which label does it carry?", ["marta.kowalczyk", "flaky-ci"]),
    ("In the staging logs, which batch_id was being processed when worker-7 was OOM-killed?", ["a91f3c"]),
    ("Which test failed with an off-by-one amount, and what were the two numbers in the assertion?",
     ["test_refund_rounding", "1374", "1375"]),
    ("What is retry_backoff_ms in config/payments.toml?", ["2750"]),
]


def issues_json(rng, count=140, needle=True):
    users = ["alex.brown", "j.ito", "marta.kowalczyk", "p.santos", "lee.kim", "o.petrenko", "s.nakamura"]
    labels = ["bug", "flaky-ci", "payments", "infra", "ui", "docs", "perf", "security"]
    items = []
    for n in range(4120, 4120 + count):
        items.append({
            "id": n, "title": rng.choice(["Refund", "Webhook", "Retry", "Ledger", "Invoice", "Payout"]) + " " +
            rng.choice(["fails on edge case", "times out", "double-charges", "logs PII", "drops events",
                        "rounds incorrectly", "is slow under load"]),
            "state": rng.choice(["open", "open", "closed"]),
            "assignee": rng.choice(users), "labels": rng.sample(labels, 2),
            "created_at": f"2026-0{rng.randint(6, 9)}-{rng.randint(10, 28)}T{rng.randint(10, 23)}:{rng.randint(10, 59)}:00Z",
            "comments": rng.randint(0, 30), "milestone": rng.choice([None, "v2.14", "v2.15"]),
            "reactions": {"+1": rng.randint(0, 9), "-1": rng.randint(0, 2), "eyes": rng.randint(0, 4)},
        })
    if needle:  # recall fact 1
        items[67] = {**items[67], "id": 4187, "title": "Refund rounds incorrectly for JPY", "state": "open",
                     "assignee": "marta.kowalczyk", "labels": ["flaky-ci", "payments"]}
    return json.dumps({"total_count": len(items), "items": items}, indent=2)


def staging_logs(rng, count=520, needle=True):
    lines = []
    for i in range(count):
        t = f"2026-10-05T03:{10 + i // 60:02d}:{i % 60:02d}.{rng.randint(100, 999)}Z"
        w = f"worker-{rng.randint(1, 9)}"
        kind = rng.random()
        if kind < 0.7:
            lines.append(f"{t} INFO  {w} processed batch_id={rng.getrandbits(24):06x} items={rng.randint(10, 500)} "
                         f"ms={rng.randint(40, 900)}")
        elif kind < 0.9:
            lines.append(f"{t} DEBUG {w} heartbeat queue_depth={rng.randint(0, 4000)} rss_mb={rng.randint(300, 1900)}")
        else:
            lines.append(f"{t} WARN  {w} slow upstream ledger-api p99={rng.randint(900, 4000)}ms retry=1")
    if needle:  # recall fact 2
        lines[301] = ("2026-10-05T03:15:01.204Z ERROR worker-7 container OOMKilled rss_mb=2048 limit_mb=2048 "
                      "batch_id=a91f3c items=48211 phase=reconcile")
    return "\n".join(lines)


def pytest_output(rng):
    out = ["============================= test session starts ==============================",
           "platform linux -- Python 3.12.3, pytest-8.3.2, pluggy-1.5.0", "rootdir: /repo", "collected 412 items", ""]
    mods = ["test_ledger", "test_webhooks", "test_payouts", "test_invoices", "test_refunds", "test_api", "test_auth"]
    for i in range(412):
        mod = mods[i % len(mods)]
        name = f"tests/{mod}.py::test_{rng.choice(['create', 'update', 'list', 'retry', 'cancel', 'sync'])}_{i:03d}"
        out.append(f"{name} PASSED{' ' * 20}[{100 * (i + 1) // 412:3d}%]")
    out[200] = "tests/test_refunds.py::test_refund_rounding FAILED                    [ 47%]"
    out += ["", "=================================== FAILURES ===================================",
            "____________________________ test_refund_rounding ______________________________", "",
            "    def test_refund_rounding():", "        refund = compute_refund(Money(13745, 'JPY'), ratio=0.1)",
            ">       assert refund.minor == 1375", "E       AssertionError: assert 1374 == 1375",
            "E        +  where 1374 = Money(minor=1374, currency='JPY').minor", "",
            "tests/test_refunds.py:88: AssertionError",
            "=========================== short test summary info ============================",
            "FAILED tests/test_refunds.py::test_refund_rounding - AssertionError: assert 1374 == 1375",
            "======================== 1 failed, 411 passed in 48.31s ========================"]
    return "\n".join(out)


def payments_toml(rng):
    out = ["# payments worker configuration", "[worker]"]
    for i in range(60):
        out.append(f"{rng.choice(['queue', 'batch', 'ledger', 'webhook', 'payout'])}_{rng.choice(['size', 'timeout_ms', 'limit', 'concurrency', 'ttl_s'])}_{i} = {rng.randint(1, 9000)}")
    out.insert(31, "retry_backoff_ms = 2750")
    out += ["", "[ledger]", 'url = "http://ledger-api:9000"', "timeout_ms = 4000", "", "[limits]", "memory_mb = 2048"]
    return "\n".join(out)


def metrics_json(rng):
    series = [{"metric": {"__name__": "payments_batch_duration_seconds", "worker": f"worker-{w}", "quantile": q},
               "values": [[1791200000 + 60 * k, f"{rng.uniform(0.05, 4):.3f}"] for k in range(40)]}
              for w in range(1, 6) for q in ("0.5", "0.99")]
    return json.dumps({"status": "success", "data": {"resultType": "matrix", "result": series}})


def repo_file(path, limit=40000):
    return (REPO / path).read_text(errors="replace")[:limit]


# compose.yaml and the Makefile are split into group files; the agent "reads" them as one, as before the split.
GROUPS = ["observability", "llm", "imagegen", "agent", "services"]
GROUP_FILES = {"compose.yaml": [f"{g}/compose.yaml" for g in GROUPS], "Makefile": [f"{g}/{g}.mk" for g in GROUPS]}


def repo_cat(*paths, limit=40000):
    return "\n".join(f"# --- {p}\n{(REPO / p).read_text(errors='replace')}" for p in paths)[:limit]


def shell(cmd):
    # No git in the image: the git log is captured on the host (agent/bench/run.sh).
    if cmd.startswith("git "):
        return Path("/bench/git-log.txt").read_text()[:40000]
    return subprocess.run(cmd, shell=True, cwd=REPO, capture_output=True, text=True).stdout[:40000]


def trajectory():
    """[(tool, args, output)] in session order."""
    rng = random.Random(42)
    return [
        ("run", {"cmd": "git log --stat -15"}, shell("git log --stat -15")),
        ("http_get", {"url": "https://ci.local/api/runs/latest/log"}, pytest_output(rng)),
        ("http_get", {"url": "https://tracker.local/api/issues?label=payments&per_page=140"}, issues_json(rng)),
        ("read_file", {"path": "config/payments.toml"}, payments_toml(rng)),
        ("run", {"cmd": "kubectl logs deploy/payments-worker --since=1h"}, staging_logs(rng, 400)),
        ("read_file", {"path": "compose.yaml"}, repo_cat("compose.yaml", *GROUP_FILES["compose.yaml"])),
        ("read_file", {"path": "Makefile"}, repo_cat("Makefile", *GROUP_FILES["Makefile"])),
        ("read_file", {"path": "tgbot/chat.py"}, repo_file("services/tgbot/chat.py")),
        ("http_get", {"url": "https://prom.local/api/v1/query_range?query=payments_batch_duration_seconds"}, metrics_json(rng)),
        ("read_file", {"path": "tgbot/app.py"}, repo_file("services/tgbot/app.py")),
        ("run", {"cmd": "grep -rn 'def ' --include=*.py ."}, shell("grep -rn 'def ' --include=*.py tgbot agent")),
        ("read_file", {"path": "tgbot/tests/test_bot.py"}, repo_file("services/tgbot/tests/test_bot.py")),
        ("read_file", {"path": "README-RU.md"}, repo_file("README-RU.md")),
        ("run", {"cmd": "kubectl logs deploy/payments-worker --previous"}, staging_logs(random.Random(7), 200, needle=False)),
        ("read_file", {"path": "agent/README-RU.md"}, repo_file("agent/README-RU.md")),
        ("read_file", {"path": "tgbot/llm.py"}, repo_file("services/tgbot/llm.py")),
        ("read_file", {"path": "tgbot/images.py"}, repo_file("services/tgbot/images.py")),
        ("http_get", {"url": "https://tracker.local/api/issues?label=infra&per_page=60"}, issues_json(random.Random(9), 60, needle=False)),
        ("read_file", {"path": "agent/sync_skills.py"}, repo_file("agent/sync_skills.py")),
        ("read_file", {"path": "tgbot/static/app.js"}, repo_file("services/tgbot/static/app.js")),
        ("run", {"cmd": "ls -la tgbot agent agent/functions"}, shell("ls -la tgbot agent agent/functions")),
        ("read_file", {"path": "tgbot/settings.py"}, repo_file("services/tgbot/settings.py")),
    ]


def metrics(server):
    text = httpx.get(f"{server}/metrics", timeout=10).text
    if text.startswith("{"):  # Strata: JSON totals; prompt_tokens includes the reused (cached) part
        t = json.loads(text)["totals"]
        return {"prompt_tokens_total": t["prompt_tokens"] - t["reused"], "prompt_tokens_cached_total": t["reused"],
                "prompt_seconds_total": t["prompt_ms"] / 1000, "tokens_predicted_total": t["output_tokens"],
                "tokens_predicted_seconds_total": t["decode_ms"] / 1000, "n_decode_total": t["requests"]}
    return {m[0].split(":", 1)[1]: float(m[1]) for m in re.findall(r"^(llamacpp:\w+) ([\d.e+-]+)$", text, re.M)}


def delta(after, before):
    keys = ["prompt_tokens_total", "prompt_tokens_cached_total", "prompt_seconds_total", "tokens_predicted_total",
            "tokens_predicted_seconds_total", "n_decode_total"]
    return {k: round(after.get(k, 0) - before.get(k, 0), 2) for k in keys}


def route(name, model, run_id, headroom="http://headroom:8787"):
    upstream = SERVERS[model]
    if name == "direct":
        return f"{upstream}/v1", {}
    if name == "bili":
        return f"http://bili:8787/bili/{upstream}/v1", {}
    if name == "hr":
        return f"{headroom}/v1", {"x-headroom-base-url": upstream}
    if name == "hb":
        return f"{headroom}/v1", {"x-headroom-base-url": f"http://bili:8787/bili/{upstream}"}
    raise ValueError(name)


def call(client, base, headers, model, messages, key, max_tokens):
    t = time.time()
    r = client.post(f"{base}/chat/completions", headers={"Authorization": "Bearer local", **headers}, json={
        "model": model, "messages": messages, "tools": TOOLS, "max_tokens": max_tokens, "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False}, "prompt_cache_key": key, "stream": False})
    elapsed = time.time() - t
    try:
        body = r.json()
    except ValueError:
        body = {"error": r.text[:500]}
    hr = {k.removeprefix("x-headroom-"): v for k, v in r.headers.items() if k.startswith("x-headroom-tokens")}
    msg = ((body.get("choices") or [{}])[0]).get("message") or {}
    return {"status": r.status_code, "s": round(elapsed, 1), "usage": body.get("usage"), "headroom": hr,
            "content": (msg.get("content") or "")[:2000],
            "tool_calls": [c.get("function", {}).get("name") for c in msg.get("tool_calls") or []],
            "error": body.get("error")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--routes", default="direct,hr,bili,hb")
    ap.add_argument("--out", default="/out")
    ap.add_argument("--steps", type=int, default=0, help="Limit steps (0 = all)")
    ap.add_argument("--recall", choices=["fork", "chain"], default="fork",
                    help="fork: each question on the bare session; chain: questions follow each other with the "
                         "answers kept, as in a chat (Headroom's cache mode only compresses append-only history)")
    ap.add_argument("--tag", default="", help="Suffix for the result file name")
    ap.add_argument("--headroom-url", default="http://headroom:8787", help="Headroom proxy for the hr / hb routes")
    ap.add_argument("--max-tokens", type=int, default=0,
                    help="max_tokens for steps and questions (0 = 2048 / 1024). billion-context's compress call "
                         "is generated within it, a truncated one fails")
    args = ap.parse_args()
    server = SERVERS[args.model]
    steps = trajectory()[: args.steps or None]
    print(f"{len(steps)} steps, {sum(len(s[2]) for s in steps):,} chars of tool output", flush=True)
    client = httpx.Client(timeout=3600)
    for name in args.routes.split(","):
        run_id = f"bench-{name}-{args.model}-{int(time.time())}"
        base, headers = route(name, args.model, run_id, args.headroom_url)
        # A nonce first: no route reuses the llama.cpp prefix cache of the previous one.
        messages = [{"role": "system", "content": f"[run {run_id}]\n{SYSTEM}"}, {"role": "user", "content": TASK}]
        log = {"route": name, "model": args.model, "run_id": run_id, "steps": [], "recall": []}
        start, before = time.time(), metrics(server)
        for i, (tool, targs, output) in enumerate(steps):
            step = call(client, base, headers, args.model, messages, run_id, args.max_tokens or 2048)
            step["i"] = i
            log["steps"].append(step)
            print(f"[{name}] step {i:2d} {step['status']} {step['s']:6.1f}s prompt={((step['usage'] or {}).get('prompt_tokens'))} "
                  f"hr={step['headroom'].get('tokens-before', '-')}->{step['headroom'].get('tokens-after', '-')} "
                  f"tools={step['tool_calls']}", flush=True)
            cid = f"call_{i}"
            messages.append({"role": "assistant", "content": "", "tool_calls": [
                {"id": cid, "type": "function", "function": {"name": tool, "arguments": json.dumps(targs)}}]})
            messages.append({"role": "tool", "tool_call_id": cid, "content": output})
        mid = metrics(server)
        log["session"] = {"wall_s": round(time.time() - start, 1), **delta(mid, before)}
        for q, expected in QUESTIONS:
            ask = messages + [{"role": "user", "content": q + " Answer from the session so far, without calling tools."}]
            res = call(client, base, headers, args.model, ask, run_id, args.max_tokens or 1024)
            if args.recall == "chain":
                messages = ask + [{"role": "assistant", "content": res["content"]}]
            text = res["content"].lower()
            res.update(question=q, expected=expected, hits=[e for e in expected if e.lower() in text])
            res["ok"] = len(res["hits"]) == len(expected)
            log["recall"].append(res)
            print(f"[{name}] recall ok={res['ok']} hits={res['hits']} tools={res['tool_calls']} "
                  f"prompt={(res['usage'] or {}).get('prompt_tokens')}", flush=True)
        log["recall_cost"] = delta(metrics(server), mid)
        log["total_wall_s"] = round(time.time() - start, 1)
        log["recall_mode"], log["max_tokens"] = args.recall, args.max_tokens or "2048/1024"
        Path(args.out, f"{args.model}-{name}{args.tag}.json").write_text(json.dumps(log, ensure_ascii=False, indent=1))
        print(f"[{name}] session {log['session']}", flush=True)


if __name__ == "__main__":
    main()
