"""Download only the pinned FP16 pipeline, then atomically mark it complete."""
import hashlib
import json
import os
from pathlib import Path

from huggingface_hub import snapshot_download
from safetensors import safe_open

from settings import FILES, MODEL_ID, REVISION


def main():
    target = Path(os.getenv("SDXL_MODEL_PATH", "/models"))
    target.mkdir(parents=True, exist_ok=True)
    marker = target / "manifest.json"
    if marker.exists() and json.loads(marker.read_text())["revision"] != REVISION:
        raise RuntimeError("Existing snapshot uses a different revision; use another directory")
    snapshot_download(MODEL_ID, revision=REVISION, local_dir=target,
                      allow_patterns=list(FILES), max_workers=2)
    inventory = {}
    for name in FILES:
        path = target / name
        if not path.is_file() or not path.stat().st_size:
            raise RuntimeError(f"Missing snapshot file: {name}")
        if name.endswith(".safetensors"):
            with safe_open(path, framework="pt", device="cpu") as weights:
                if not list(weights.keys()):
                    raise RuntimeError(f"Empty checkpoint: {name}")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        inventory[name] = {"size": path.stat().st_size, "sha256": digest}
    temporary = target / "manifest.json.tmp"
    temporary.write_text(json.dumps({"model_id": MODEL_ID, "revision": REVISION,
                                    "files": inventory}, indent=2) + "\n")
    temporary.replace(marker)
    print(f"Verified {len(inventory)} files at revision {REVISION}", flush=True)


if __name__ == "__main__":
    main()

