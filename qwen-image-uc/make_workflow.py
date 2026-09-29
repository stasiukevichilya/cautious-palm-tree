"""Generate the editable ComfyUI workflow from the API graph, reusing qwen-image's generator."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LAYOUT = {
    "1": ([], ["MODEL"], ["model_name"], [0, 0]),
    "2": ([], ["CLIP"], ["encoder_name"], [0, 200]),
    "3": ([], ["VAE"], ["vae_name"], [0, 400]),
    "4": (["clip", "vae"], ["CONDITIONING", "CONDITIONING", "LATENT"],
          ["prompt", "negative_prompt", "resolution"], [370, 0]),
    "5": (["model"], ["MODEL"], ["device", "dtype"], [370, 470]),
    "9": ([], ["LATENT"], ["width", "height", "batch_size"], [370, 650]),
    "6": (["model", "positive", "negative", "latent_image"], ["LATENT"],
          ["seed", "@randomize", "steps", "cfg", "sampler_name", "scheduler", "denoise"], [780, 0]),
    "7": (["samples", "vae"], ["IMAGE"], [], [1140, 0]),
    "8": (["images"], [], ["filename_prefix"], [1480, 0]),
}


def build(graph):
    spec = importlib.util.spec_from_file_location("qwen_image_workflow", ROOT.parent / "qwen-image/make_workflow.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build(graph, LAYOUT)


if __name__ == "__main__":
    graph = json.loads((ROOT / "workflows/qwen-image21-uc.api.json").read_text())
    (ROOT / "workflows/qwen-image21-uc.ui.json").write_text(json.dumps(build(graph), indent=2) + "\n")
