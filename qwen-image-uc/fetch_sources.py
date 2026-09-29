"""Fetch immutable source archives during Docker build, never at runtime."""
import io
import tarfile
import urllib.request
from pathlib import Path

# Same ComfyUI revision as qwen-image; the model card asks for the leejet ComfyUI-GGUF fork, which adds
# the qwen_image21 architecture of these sd.cpp-converted GGUFs and dequantizes their quantized 1D norms.
SOURCES = {
    "Comfy-Org/ComfyUI": ("79be670e2d9be63e238785af307369d2b9039ed1", Path("/opt/ComfyUI")),
    "leejet/ComfyUI-GGUF": ("373048b8403a7820620065210a691263d4da0a61",
                            Path("/opt/ComfyUI/custom_nodes/ComfyUI-GGUF")),
}

for repo, (revision, target) in SOURCES.items():
    target.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(f"https://codeload.github.com/{repo}/tar.gz/{revision}", timeout=120) as response:
        archive = response.read()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as stream:
        for member in stream.getmembers():
            parts = Path(member.name).parts
            if len(parts) > 1:
                member.name = str(Path(*parts[1:]))
                stream.extract(member, target, filter="data")
    print(f"Fetched {repo}@{revision}", flush=True)
