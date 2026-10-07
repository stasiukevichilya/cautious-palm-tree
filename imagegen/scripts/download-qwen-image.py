"""Download pinned Qwen-Image weights using host stdlib and curl only."""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


PROFILES = {"qwen-image": ("imagegen/qwen-image/models.json", "models/qwen-image-2.1"),
            "qwen-image-uc": ("imagegen/qwen-image-uc/models.json", "models/qwen-image-2.1-uc")}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=sorted(PROFILES), default="qwen-image")
    parser.add_argument("--variant", action="append", default=[], help="also fetch an optional DiT variant; repeatable")
    args = parser.parse_args(argv)
    manifest_path, destination = PROFILES[args.profile]
    manifest = json.loads((ROOT / manifest_path).read_text())
    unknown = set(args.variant) - set(manifest.get("variants", {}))
    if unknown:
        parser.error(f"unknown variant(s) for {args.profile}: {sorted(unknown)}")
    destination = ROOT / destination
    destination.mkdir(parents=True, exist_ok=True)
    for item in manifest["files"] + [x for name in args.variant for x in manifest["variants"][name]["files"]]:
        target = destination / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.stat().st_size != item["size"] or sha256(target) != item["sha256"]:
                raise RuntimeError(f"Existing file has wrong checksum: {target}")
            print(f"Verified existing {item['path']}", flush=True)
            continue
        temporary = target.with_suffix(target.suffix + ".part")
        if not temporary.exists() or temporary.stat().st_size != item["size"]:
            print(f"Downloading {item['path']} ({item['size'] / 1e9:.2f} GB)", flush=True)
            url = f"https://huggingface.co/{item['repo']}/resolve/{item['revision']}/{item['file']}"
            subprocess.run(["curl", "--fail", "--location", "--silent", "--show-error",
                            "--retry", "5", "--continue-at", "-", "--output", str(temporary), url], check=True)
        if temporary.stat().st_size != item["size"] or sha256(temporary) != item["sha256"]:
            raise RuntimeError(f"Downloaded checksum does not match publisher: {temporary}")
        temporary.replace(target)
        print(f"Verified {item['path']}", flush=True)
    temporary = destination / "manifest.json.tmp"
    temporary.write_text(json.dumps(manifest, indent=2) + "\n")
    temporary.replace(destination / "manifest.json")
    print(f"{args.profile} snapshot complete", flush=True)


if __name__ == "__main__":
    main()
