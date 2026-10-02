import argparse
import copy
import io
import json
import os
import random
import time
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image, ImageStat


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--steps", type=int, default=25)
    parser.add_argument("--seed", type=int, default=random.randrange(2**32),
                        help="base seed; distinct seeds avoid ComfyUI returning cached outputs")
    parser.add_argument("--prompt", default="A red ceramic teapot on a white table, studio photography")
    parser.add_argument("--base", default="http://127.0.0.1:8188")
    parser.add_argument("--resolution", type=int, default=1024, help="square side, multiple of 32")
    parser.add_argument("--width", type=int, help="with --height: non-square size, multiples of 32")
    parser.add_argument("--height", type=int)
    parser.add_argument("--timeout", type=int, default=900, help="seconds per image")
    args = parser.parse_args()
    if (args.width is None) != (args.height is None):
        parser.error("--width and --height must be given together")
    size = (args.width, args.height) if args.width else (args.resolution, args.resolution)

    def call(path, payload=None):
        request = urllib.request.Request(args.base + path,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    encoder = "cuda:1" if (os.getenv("QWEN_IMAGE_UC_ENCODER_DEVICE") or "cpu") == "gpu" else "cpu"
    workflow = json.loads(Path("/opt/qwen-image-uc/workflows/qwen-image21-uc.api.json").read_text())
    reports = []
    for index in range(args.count):
        graph = copy.deepcopy(workflow)
        graph["4"]["inputs"].update(prompt=args.prompt, resolution=args.resolution)
        graph["9"]["inputs"].update(width=size[0], height=size[1])
        graph["6"]["inputs"].update(steps=args.steps, seed=args.seed + index)
        started = time.monotonic()
        result = call("/prompt", {"prompt": graph, "client_id": "qwen-image-uc-smoke"})
        identifier = result["prompt_id"]
        while True:
            history = call(f"/history/{identifier}").get(identifier)
            if history:
                if history["status"]["status_str"] != "success":
                    raise RuntimeError(json.dumps(history["status"]))
                break
            if time.monotonic() - started > args.timeout:
                raise RuntimeError("Generation timed out")
            time.sleep(2)
        image_info = history["outputs"]["8"]["images"][0]
        with urllib.request.urlopen(args.base + "/view?" + urllib.parse.urlencode(image_info)) as response:
            image = Image.open(io.BytesIO(response.read()))
            image.load()
        if image.size != size or max(ImageStat.Stat(image.convert("RGB")).var) < 1:
            raise RuntimeError("Invalid image size or uniform output")
        gpu = call("/local-qwen-image-uc/devices")
        placement = {name: model["devices"] for name, model in gpu["models"].items()}
        if len(placement) != 3 or sorted(sum(placement.values(), [])) != sorted(["cuda:0", "cuda:0", encoder]):
            raise RuntimeError(f"Expected DiT and VAE on cuda:0, text encoder on {encoder}: {gpu}")
        for model in gpu["models"].values():
            if model["devices"] != [model["expected"]]:
                raise RuntimeError(f"Unexpected placement: {model}")
        # With the encoder on GPU the free VRAM belongs to bonsai-mtp; fixed caps keep the services apart.
        if encoder == "cpu" and gpu["memory"]["free_bytes"] < 1024**3:
            raise RuntimeError(f"Less than 1 GiB VRAM remains: {gpu}")
        report = {"id": identifier, "size": size, "seconds": time.monotonic() - started, "image": image_info,
                  "gpu": gpu, "system": call("/system_stats")}
        reports.append(report)
        print(json.dumps(report), flush=True)
    target = Path("/data/output/benchmarks")
    target.mkdir(exist_ok=True)
    (target / f"qwen-image21-uc-{int(time.time())}.json").write_text(json.dumps(reports, indent=2) + "\n")
    print(f"PASS: {len(reports)} images")


if __name__ == "__main__":
    main()
