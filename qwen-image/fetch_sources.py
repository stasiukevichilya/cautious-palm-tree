"""Fetch immutable source archives during Docker build, never at runtime."""
import io
import tarfile
import urllib.request
from pathlib import Path

SOURCES = {
    "Comfy-Org/ComfyUI": ("79be670e2d9be63e238785af307369d2b9039ed1", Path("/opt/ComfyUI")),
    "city96/ComfyUI-GGUF": ("6ea2651e7df66d7585f6ffee804b20e92fb38b8a", Path("/opt/ComfyUI/custom_nodes/ComfyUI-GGUF")),
    "pottokao-dotcom/ComfyUI-GGUF-Qwen3VL-TE": (
        "81b1ceea2fc16e52faddfd5fd6a597e4356e2709",
        Path("/opt/ComfyUI/custom_nodes/ComfyUI-GGUF-Qwen3VL-TE")),
}

for repo, (revision, target) in SOURCES.items():
    target.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(f"https://codeload.github.com/{repo}/tar.gz/{revision}", timeout=120) as response:
        archive = response.read()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as stream:
        members = stream.getmembers()
        for member in members:
            parts = Path(member.name).parts
            if len(parts) > 1:
                member.name = str(Path(*parts[1:]))
                stream.extract(member, target, filter="data")
    print(f"Fetched {repo}@{revision}", flush=True)
