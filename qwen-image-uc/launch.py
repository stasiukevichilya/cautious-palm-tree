"""Model card configuration: GGUF DiT and VAE resident on one GPU, INT8 text encoder in system RAM (CPU)."""
import json
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import torch

if not torch.cuda.is_available() or torch.cuda.device_count() != 1:
    raise RuntimeError("Qwen-Image UC requires exactly one visible GPU")
reserve = float(os.getenv("QWEN_IMAGE_UC_VRAM_RESERVE_GIB", "1.5"))
if not 1 <= reserve <= 8:
    raise ValueError("VRAM reserve must be between 1 and 8 GiB")
free, total = torch.cuda.mem_get_info(0)
budget = free - int(reserve * 1024**3)
if budget < 8 * 1024**3:
    raise RuntimeError("GPU 0: insufficient VRAM; stop other model services")
torch.cuda.set_per_process_memory_fraction(budget / total, 0)
print(f"CUDA 0: {torch.cuda.get_device_name(0)}; UUID {torch.cuda.get_device_properties(0).uuid}; "
      f"allocator budget {budget / 1024**3:.2f} GiB; CPU threads {torch.get_num_threads()}", flush=True)
# Triton links its CUDA helper with -lcuda; WSL/NVIDIA runtimes mount only libcuda.so.1.
libcuda = next(line.split()[-1] for line in subprocess.check_output(["ldconfig", "-p"], text=True).splitlines()
               if line.strip().startswith("libcuda.so.1 ") and "x86-64" in line)
link_dir = Path("/tmp/triton-libcuda")
link_dir.mkdir(exist_ok=True)
(link_dir / "libcuda.so").unlink(missing_ok=True)
(link_dir / "libcuda.so").symlink_to(libcuda)
os.environ["TRITON_LIBCUDA_PATH"] = str(link_dir)
expected = json.loads(Path("/opt/qwen-image-uc/models.json").read_text())
actual = json.loads(Path("/models/manifest.json").read_text())
if actual != expected:
    raise RuntimeError("Model manifest does not match this image")
for item in expected["files"]:
    path = Path("/models") / item["path"]
    if not path.is_file() or path.stat().st_size != item["size"]:
        raise RuntimeError(f"Incomplete snapshot: {item['path']}")
for name in ("input", "output", "user", "temp"):
    Path(f"/data/{name}").mkdir(parents=True, exist_ok=True)
workflows = Path("/data/user/default/workflows")
workflows.mkdir(parents=True, exist_ok=True)
for path in Path("/opt/qwen-image-uc/workflows").glob("*.ui.json"):
    target = workflows / path.name.replace(".ui.json", ".json")
    if not target.exists():
        shutil.copyfile(path, target)
# Fill in missing defaults only; user choices in the settings file are kept.
settings = Path("/data/user/default/comfy.settings.json")
values = json.loads(settings.read_text()) if settings.exists() else {}
defaults = {"Comfy.TutorialCompleted": True, "Comfy.NewBlankWorkflow": True}
if not defaults.keys() <= values.keys():
    settings.write_text(json.dumps({**defaults, **values}, indent=4) + "\n")
sys.path.insert(0, "/opt/ComfyUI")
# --highvram keeps the DiT and VAE on the GPU between prompts; --gpu-only is not used because it would
# also place the text encoder in VRAM, while the card recommends running it from system RAM.
sys.argv = ["main.py", "--listen", "0.0.0.0", "--port", "8188", "--highvram",
            "--disable-dynamic-vram", "--disable-async-offload", "--disable-pinned-memory",
            "--reserve-vram", str(reserve), "--use-pytorch-cross-attention", "--preview-method", "none",
            "--input-directory", "/data/input", "--output-directory", "/data/output",
            "--user-directory", "/data/user", "--temp-directory", "/data/temp",
            "--disable-auto-launch", "--disable-api-nodes"]
runpy.run_path("/opt/ComfyUI/main.py", run_name="__main__")
