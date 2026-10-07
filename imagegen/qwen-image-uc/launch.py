"""Model card configuration: GGUF DiT and VAE resident on one GPU, INT8 text encoder in system RAM (CPU).

QWEN_IMAGE_UC_ENCODER_DEVICE=gpu puts the encoder on a second GPU instead (no CPU offload at all)."""
import json
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import torch

encoder_device = (os.getenv("QWEN_IMAGE_UC_ENCODER_DEVICE") or "cpu")
if encoder_device not in ("cpu", "gpu"):
    raise ValueError("QWEN_IMAGE_UC_ENCODER_DEVICE must be cpu or gpu")
devices = 2 if encoder_device == "gpu" else 1
if not torch.cuda.is_available() or torch.cuda.device_count() != devices:
    raise RuntimeError(f"Qwen-Image UC requires exactly {devices} visible GPU(s)")
reserve = float(os.getenv("QWEN_IMAGE_UC_VRAM_RESERVE_GIB", "1.5"))
if not 1 <= reserve <= 8:
    raise ValueError("VRAM reserve must be between 1 and 8 GiB")


def cap_allocator(index, minimum_gib, limit_gib=None):
    """Without a limit take free VRAM minus the reserve; with one (GPU shared with another service) take
    exactly the limit, so the budget does not depend on which service started first."""
    free, total = torch.cuda.mem_get_info(index)
    budget = free - int(reserve * 1024**3) if limit_gib is None else int(limit_gib * 1024**3)
    if budget < minimum_gib * 1024**3 or budget > free:
        raise RuntimeError(f"GPU {index}: insufficient VRAM; stop other model services")
    torch.cuda.set_per_process_memory_fraction(budget / total, index)
    print(f"CUDA {index}: {torch.cuda.get_device_name(index)}; UUID {torch.cuda.get_device_properties(index).uuid}; "
          f"allocator budget {budget / 1024**3:.2f} GiB", flush=True)


limit = os.getenv("QWEN_IMAGE_UC_VRAM_GIB")
# 7 GiB is the measured 1024x1024 peak of the DiT and VAE.
cap_allocator(0, 7, float(limit) if limit else None)
if devices == 2:
    # The INT8 encoder without its unused LM head and vision tower takes 7.1 GiB; the rest is activations.
    cap_allocator(1, 7.5, float(os.getenv("QWEN_IMAGE_UC_ENCODER_VRAM_GIB", "8")))
print(f"Text encoder: {'cuda:1' if devices == 2 else 'CPU'}; CPU threads {torch.get_num_threads()}", flush=True)
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
# also place the text encoder in VRAM of cuda:0; the encoder node picks its device itself.
sys.argv = ["main.py", "--listen", "0.0.0.0", "--port", "8188", "--highvram",
            "--disable-dynamic-vram", "--disable-async-offload", "--disable-pinned-memory",
            "--reserve-vram", str(reserve), "--use-pytorch-cross-attention", "--preview-method", "none",
            "--input-directory", "/data/input", "--output-directory", "/data/output",
            "--user-directory", "/data/user", "--temp-directory", "/data/temp",
            "--disable-auto-launch", "--disable-api-nodes"]
runpy.run_path("/opt/ComfyUI/main.py", run_name="__main__")
