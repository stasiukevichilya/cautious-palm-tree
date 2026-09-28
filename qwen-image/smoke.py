import argparse
import copy
import io
import json
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
    parser.add_argument("--width", type=int, help="with --height: non-square size, multiples of 16")
    parser.add_argument("--height", type=int)
    parser.add_argument("--timeout", type=int, default=900, help="seconds per image")
    parser.add_argument("--workflow", help="API graph name in /opt/qwen-image/workflows; "
                        "default: the one matching the served DiT (QWEN_IMAGE_DIT)")
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

    args.workflow = args.workflow or call("/local-qwen-image/config")["workflow"]
    workflow = json.loads(Path(f"/opt/qwen-image/workflows/{args.workflow}.api.json").read_text())
    reports = []
    for index in range(args.count):
        graph = copy.deepcopy(workflow)
        graph["4"]["inputs"].update(prompt=args.prompt, resolution=args.resolution)
        if args.width:
            graph["9"] = {"class_type": "EmptySD3LatentImage",
                          "inputs": {"width": args.width, "height": args.height, "batch_size": 1}}
            graph["6"]["inputs"]["latent_image"] = ["9", 0]
        graph["6"]["inputs"].update(steps=args.steps, seed=args.seed + index)
        started = time.monotonic()
        result = call("/prompt", {"prompt": graph, "client_id": "qwen-image-smoke"})
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
        gpu = call("/local-qwen-image/gpu")
        if not gpu["gpu_only"] or len(gpu["models"]) != 3:
            raise RuntimeError(f"Missing GPU placement evidence: {gpu}")
        for model in gpu["models"].values():
            if model["devices"] != [model["expected"]]:
                raise RuntimeError(f"Unexpected offload: {model}")
        if any(item["free_bytes"] < 1024**3 for item in gpu["memory"]):
            raise RuntimeError(f"Less than 1 GiB VRAM remains: {gpu}")
        report = {"id": identifier, "size": size, "seconds": time.monotonic() - started, "image": image_info,
                  "gpu": gpu, "system": call("/system_stats")}
        reports.append(report)
        print(json.dumps(report), flush=True)
    target = Path("/data/output/benchmarks")
    target.mkdir(exist_ok=True)
    (target / f"{args.workflow}-{int(time.time())}.json").write_text(json.dumps(reports, indent=2) + "\n")
    print(f"PASS: {len(reports)} images")


if __name__ == "__main__":
    main()
