"""Summary tables for outputs/bench/ctx/<model>-<route><tag>.json (+ bili compress events by run_id).

Usage: python3 agent/bench/summarize.py qwen3.8-27b-mtp [model ...]
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CTX = ROOT / "outputs/bench/ctx"
BILI_LOG = ROOT / "outputs/agent/bili/state/billion-context/bili.log"
log = BILI_LOG.read_text(errors="replace")


def bili_events(run_id):
    lines = [l for l in log.splitlines() if run_id in l]
    done = sum("[acp-proxy: [Compressed" in l for l in lines)
    rejected = sum("compress call had no valid ranges" in l for l in lines)
    timeouts = sum("network failure" in l for l in lines)
    saved = sum(int(m) for l in lines for m in re.findall(r"~(\d+) tokens saved", l))
    return done, rejected, saved, timeouts


def row(f):
    d = json.loads(f.read_text())
    last = d["steps"][-1]["usage"]["prompt_tokens"]
    s = d["session"]
    ok = sum(r["ok"] for r in d["recall"])
    rp = [r["usage"]["prompt_tokens"] if r.get("usage") else None for r in d["recall"]]
    errs = sum(st["status"] != 200 for st in d["steps"] + d["recall"])
    bili = bili_events(d["run_id"]) if d["route"] in ("bili", "hb") else None
    return (f"| {d['route']}{f.stem.split(d['route'], 1)[1]} | {last:,} | {int(s['prompt_tokens_total']):,} | "
            f"{s['prompt_seconds_total']:.0f} | {s['wall_s']:.0f} | {ok}/4 | "
            f"{'/'.join(f'{p/1000:.1f}K' if p else '-' for p in rp)} | "
            f"{int(d['recall_cost']['prompt_tokens_total']):,} | "
            f"{f'{bili[0]} ок / {bili[1]} обрезано / {bili[3]} обрыв, ~{bili[2]:,} ток.' if bili else '—'} | {errs} |")


for model in sys.argv[1:]:
    print(f"\n### {model}\n")
    print("| Маршрут | Промпт, посл. шаг | prefill, ток. | prefill, с | wall, с | Recall | Промпт вопросов 1–4 | "
          "prefill вопросов | compress bili | Ошибки |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    files = sorted(CTX.glob(f"{model}-*.json"), key=lambda p: (len(p.stem), p.stem))
    order = {"direct": 0, "hr": 1, "bili": 2, "hb": 3}
    files.sort(key=lambda p: (len(p.stem.split(model + "-", 1)[1].split("-")), order.get(
        p.stem.split(model + "-", 1)[1].split("-")[0], 9)))
    for f in files:
        if f.stem.split(model + "-", 1)[1].split("-")[0] in order:
            print(row(f))
