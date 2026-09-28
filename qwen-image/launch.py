import json
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import torch

if not torch.cuda.is_available() or torch.cuda.device_count() != 2:
    raise RuntimeError("Qwen-Image requires both selected GPUs")
reserve = float(os.getenv("QWEN_IMAGE_VRAM_RESERVE_GIB", "1.5"))
if not 1 <= reserve <= 8:
    raise ValueError("VRAM reserve must be between 1 and 8 GiB")
for i in range(2):
    free, total = torch.cuda.mem_get_info(i)
    budget = free - int(reserve * 1024**3)
    if budget < 8 * 1024**3:
        raise RuntimeError(f"GPU {i}: insufficient VRAM; stop other model services")
    torch.cuda.set_per_process_memory_fraction(budget / total, i)
    print(f"CUDA {i}: {torch.cuda.get_device_name(i)}; "
          f"UUID {torch.cuda.get_device_properties(i).uuid}; allocator budget {budget / 1024**3:.2f} GiB",
          flush=True)
# Triton links its CUDA helper with -lcuda; WSL/NVIDIA runtimes mount only libcuda.so.1.
libcuda = next(line.split()[-1] for line in subprocess.check_output(["ldconfig", "-p"], text=True).splitlines()
               if line.strip().startswith("libcuda.so.1 ") and "x86-64" in line)
link_dir = Path("/tmp/triton-libcuda")
link_dir.mkdir(exist_ok=True)
(link_dir / "libcuda.so").unlink(missing_ok=True)
(link_dir / "libcuda.so").symlink_to(libcuda)
os.environ["TRITON_LIBCUDA_PATH"] = str(link_dir)
expected = json.loads(Path("/opt/qwen-image/models.json").read_text())
actual = json.loads(Path("/models/manifest.json").read_text())
if actual != expected:
    raise RuntimeError("Model manifest does not match this image")
for item in expected["files"]:
    path = Path("/models") / item["path"]
    if not path.is_file() or path.stat().st_size != item["size"]:
        raise RuntimeError(f"Incomplete snapshot: {item['path']}")
# One DiT per process: with --gpu-only a replaced DiT is never evicted, so switching needs a restart.
dit = os.getenv("QWEN_IMAGE_DIT", "base")
if dit != "base" and dit not in expected.get("variants", {}):
    raise RuntimeError(f"Unknown QWEN_IMAGE_DIT {dit!r}; choose base or {sorted(expected.get('variants', {}))}")
for item in expected["variants"][dit]["files"] if dit != "base" else []:
    path = Path("/models") / item["path"]
    if not path.is_file() or path.stat().st_size != item["size"]:
        raise RuntimeError(f"Variant {dit} is not downloaded: {item['path']}")
for name in ("input", "output", "user", "temp"):
    Path(f"/data/{name}").mkdir(parents=True, exist_ok=True)
workflows = Path("/data/user/default/workflows")
workflows.mkdir(parents=True, exist_ok=True)
for path in Path("/opt/qwen-image/workflows").glob("*.ui.json"):
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
sys.argv = ["main.py", "--listen", "0.0.0.0", "--port", "8188", "--gpu-only",
            "--disable-dynamic-vram", "--disable-async-offload", "--disable-pinned-memory",
            "--reserve-vram", str(reserve), "--use-pytorch-cross-attention", "--preview-method", "none",
            "--input-directory", "/data/input", "--output-directory", "/data/output",
            "--user-directory", "/data/user", "--temp-directory", "/data/temp",
            "--disable-auto-launch", "--disable-api-nodes"]
runpy.run_path("/opt/ComfyUI/main.py", run_name="__main__")
