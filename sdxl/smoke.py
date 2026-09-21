"""Run inside the service container; outputs include GPU peaks and image validation."""
import argparse
import io
import json
import statistics
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image, ImageStat


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--duration", type=int, default=0)
    parser.add_argument("--steps", type=int, default=30)
    parser.add_argument("--interval", type=int, default=0, help="Pause between completed jobs in seconds")
    parser.add_argument("--base", default="http://127.0.0.1:8080")
    args = parser.parse_args()
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(path, body=None):
        payload = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(args.base + path, data=payload,
                                     headers={"Content-Type": "application/json"})
        with client.open(req, timeout=30) as response:
            return json.load(response)

    deadline = time.monotonic() + 600
    while True:
        try:
            ready = request("/health/ready")
            break
        except (urllib.error.URLError, TimeoutError):
            if time.monotonic() > deadline:
                raise RuntimeError("SDXL did not become ready")
            time.sleep(2)
    started = time.monotonic()
    results = []
    directory = Path("/outputs/benchmarks")
    directory.mkdir(exist_ok=True)
    path = directory / f"{ready['pipeline']['mode']}-{int(time.time())}.json"
    prompts = ["A red ceramic teapot on a white table, studio photography",
               "A glass pavilion beside a lake, morning light, architectural photography",
               "A small sailing boat on a turquoise sea, aerial photography"]
    while len(results) < args.count or time.monotonic() - started < args.duration:
        job = request("/api/generations", {"prompt": prompts[len(results) % len(prompts)],
                      "negative_prompt": "blur, low quality", "steps": args.steps,
                      "seed": 42 + len(results)})
        deadline = time.monotonic() + 900
        while job["status"] not in ("completed", "failed", "cancelled", "interrupted"):
            if time.monotonic() > deadline:
                raise RuntimeError("Generation timed out")
            time.sleep(1)
            job = request(f"/api/generations/{job['id']}")
        if job["status"] != "completed":
            raise RuntimeError(json.dumps(job))
        with client.open(args.base + f"/api/generations/{job['id']}/image", timeout=30) as response:
            image = Image.open(io.BytesIO(response.read()))
            image.load()
        if image.size != (1024, 1024) or max(ImageStat.Stat(image.convert("RGB")).var) < 1:
            raise RuntimeError("Image has an unexpected size or uniform pixels")
        results.append({"id": job["id"], **job["metadata"]})
        report = {"pipeline": ready["pipeline"], "count": len(results),
                  "wall_seconds": time.monotonic() - started, "interval_seconds": args.interval,
                  "median_seconds": statistics.median(row["seconds"] for row in results), "results": results}
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(report, indent=2) + "\n")
        temporary.replace(path)
        print(json.dumps({key: results[-1][key] for key in ("id", "seconds", "memory")}), flush=True)
        if any(gpu["free_bytes"] < 1024**3 for gpu in job["metadata"].get("memory", [])):
            raise RuntimeError(f"Less than 1 GiB VRAM remains; see {path}")
        if args.interval and (len(results) < args.count or time.monotonic() - started < args.duration):
            time.sleep(args.interval)
    report = {"pipeline": ready["pipeline"], "count": len(results),
              "wall_seconds": time.monotonic() - started,
              "median_seconds": statistics.median(row["seconds"] for row in results),
              "results": results}
    report["interval_seconds"] = args.interval
    path.write_text(json.dumps(report, indent=2) + "\n")
    print(f"PASS: {len(results)} images; median {report['median_seconds']:.2f}s; report {path}")


if __name__ == "__main__":
    main()
