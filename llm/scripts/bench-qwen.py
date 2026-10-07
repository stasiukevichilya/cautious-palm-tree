#!/usr/bin/env python3
"""Benchmark the running llama-server: prefill/decode t/s, MTP acceptance, peak VRAM.

Greedy (temperature 0) chat completions over prompts of fixed token lengths, built from
the repo's own source files. Outputs are saved so runs with and without speculative
decoding can be compared: python3 bench-qwen.py --compare a.json b.json
"""
import argparse
import glob
import json
import subprocess
import threading
import time
import urllib.request

# Globs from the repository root (run it from there).
SOURCES = ["services/tgbot/*.py", "services/scraper/*.py", "services/torrent/*.py", "imagegen/sdxl/*.py",
           "imagegen/qwen-image/*.py", "*.md", "*/*.md"]
TASK = ("\n\nReview the code and documents above: for each service describe what it does, "
        "list concrete bugs and risks you see, and propose fixes with code.")


def post(url, body, timeout=1800):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def corpus():
    parts = []
    for pattern in SOURCES:
        for path in sorted(glob.glob(pattern)):
            with open(path, encoding="utf-8", errors="replace") as f:
                parts.append(f"### {path}\n{f.read()}")
    return "\n\n".join(parts)


def prompt_text(url, length):
    tokens = post(f"{url}/tokenize", {"content": corpus()})["tokens"]
    while len(tokens) < length:
        tokens = tokens + tokens
    # Distinct head per length so no run reuses another run's prompt cache.
    text = post(f"{url}/detokenize", {"tokens": tokens[:length]})["content"]
    return f"Run {length}\n\n{text}{TASK}"


class VramPeak(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.peak, self.total, self.stop = {}, {}, threading.Event()

    def run(self):
        while not self.stop.is_set():
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=index,memory.used,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True).stdout
            for line in out.strip().splitlines():
                i, used, total = (int(x) for x in line.split(","))
                self.peak[i] = max(self.peak.get(i, 0), used)
                self.total[i] = total
            time.sleep(0.5)


def bench(args):
    results = []
    for length in args.lengths:
        prompt = prompt_text(args.url, length)
        vram = VramPeak()
        vram.start()
        r = post(f"{args.url}/v1/chat/completions", {
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": args.n_predict, "temperature": 0, "top_k": 1,
        })
        vram.stop.set()
        vram.join()
        t = r["timings"]
        row = {
            "ctx": r["usage"]["prompt_tokens"],
            "prefill_tps": round(t["prompt_per_second"], 1),
            "decode_tps": round(t["predicted_per_second"], 2),
            "draft_n": t.get("draft_n"),
            "draft_accepted": t.get("draft_n_accepted"),
            "free_mib": {i: vram.total[i] - vram.peak[i] for i in sorted(vram.peak)},
            "text": (r["choices"][0]["message"].get("reasoning_content") or "")
            + (r["choices"][0]["message"].get("content") or ""),
        }
        results.append(row)
        print(json.dumps({k: v for k, v in row.items() if k != "text"}), flush=True)
    if args.out:
        with open(args.out, "w") as f:
            json.dump(results, f, ensure_ascii=False, indent=1)


def compare(a, b):
    ra, rb = json.load(open(a)), json.load(open(b))
    for x, y in zip(ra, rb):
        same = 0
        while same < min(len(x["text"]), len(y["text"])) and x["text"][same] == y["text"][same]:
            same += 1
        print(f"ctx {x['ctx']}: decode {x['decode_tps']} -> {y['decode_tps']} t/s "
              f"({y['decode_tps'] / x['decode_tps'] - 1:+.0%}), prefill {x['prefill_tps']} -> {y['prefill_tps']}, "
              f"identical chars {same}/{max(len(x['text']), len(y['text']))}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:8080")
    p.add_argument("--lengths", type=int, nargs="+", default=[1000, 32000, 64000, 120000])
    p.add_argument("--n-predict", type=int, default=768)
    p.add_argument("--out")
    p.add_argument("--compare", nargs=2)
    a = p.parse_args()
    compare(*a.compare) if a.compare else bench(a)
