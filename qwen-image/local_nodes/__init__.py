"""Select GPUs through ComfyUI's native CUDA context, then assert full placement."""
import importlib
import json
import logging
import os
import sys
import weakref
from pathlib import Path

from aiohttp import web
from server import PromptServer

import torch
import nodes
import comfy.model_management as mm
from comfy.cli_args import args

log = logging.getLogger("qwen-image-gpu")
verified_models = {}


def tensor_devices(model):
    return {str(t.device) for t in list(model.parameters()) + list(model.buffers())}


@PromptServer.instance.routes.get("/local-qwen-image/gpu")
async def gpu_status(request):
    models = {}
    for name, (reference, target) in verified_models.items():
        model = reference()
        if model is not None:
            models[name] = {"expected": target, "devices": sorted(tensor_devices(model))}
    memory = []
    for index in range(torch.cuda.device_count()):
        free, total = torch.cuda.mem_get_info(index)
        memory.append({"index": index, "name": torch.cuda.get_device_name(index),
                       "free_bytes": free, "total_bytes": total,
                       "allocated_bytes": torch.cuda.memory_allocated(index),
                       "reserved_bytes": torch.cuda.memory_reserved(index),
                       "allocated_peak_bytes": torch.cuda.max_memory_allocated(index),
                       "reserved_peak_bytes": torch.cuda.max_memory_reserved(index)})
    return web.json_response({"gpu_only": args.gpu_only, "models": models, "memory": memory})


def require_gpu_mode():
    if not args.gpu_only or torch.cuda.device_count() != 2:
        raise RuntimeError("Qwen-Image requires --gpu-only and exactly two visible CUDA GPUs")


def fully_load(patcher, device):
    target = torch.device(device)
    if patcher.load_device != target or patcher.offload_device != target:
        raise RuntimeError(f"Refusing CPU/off-device placement: {patcher.load_device}, {patcher.offload_device}")
    mm.load_models_gpu([patcher], force_full_load=True)
    devices = tensor_devices(patcher.model)
    if devices != {str(target)}:
        raise RuntimeError(f"Model tensors are not entirely on {target}: {devices}")
    log.info("Verified GPU-only model: %s", json.dumps({"device": str(target), "type": type(patcher.model).__name__}))
    verified_models[type(patcher.model).__name__] = (weakref.ref(patcher.model), str(target))


def served_dit(manifest="/opt/qwen-image/models.json", variant=None):
    """The single DiT this process serves (QWEN_IMAGE_DIT, checked by launch.py) and its workflow."""
    data = json.loads(Path(manifest).read_text())
    variant = variant or os.getenv("QWEN_IMAGE_DIT", "base")
    files = data["files"] if variant == "base" else data["variants"][variant]["files"]
    name, = (Path(x["path"]).name for x in files if x["path"].startswith("diffusion_models/"))
    workflow = "qwen-image21" if variant == "base" else data["variants"][variant]["workflow"]
    return {"variant": variant, "model_name": name, "workflow": workflow}


@PromptServer.instance.routes.get("/local-qwen-image/config")
async def config(request):
    return web.json_response(served_dit())


def register_qwen_image_arch():
    """sd.cpp-style GGUFs (unsloth) have no general.architecture; ComfyUI-GGUF then guesses
    the arch from tensor keys, and its pinned list has no Qwen-Image entry."""
    loader = sys.modules[nodes.NODE_CLASS_MAPPINGS["UnetLoaderGGUF"].__module__]
    convert = importlib.import_module(".tools.convert", loader.__package__)
    if any(arch.arch == "qwen_image" for arch in convert.arch_list):
        return

    class ModelQwenImage(convert.ModelTemplate):
        arch = "qwen_image"
        keys_detect = [("img_in.weight", "txt_in.text_norm.weight", "transformer_blocks.0.img_mlp.gate_up.weight")]

    convert.arch_list.insert(0, ModelQwenImage)


class QwenImageDiTGPU:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"model_name": ([served_dit()["model_name"]],)}}
    RETURN_TYPES = ("MODEL",)
    FUNCTION = "load"
    CATEGORY = "local/qwen-image"

    def load(self, model_name):
        require_gpu_mode()
        register_qwen_image_arch()
        with torch.cuda.device(0):
            model, = nodes.NODE_CLASS_MAPPINGS["UnetLoaderGGUF"]().load_unet(model_name)
            fully_load(model, "cuda:0")
        return (model,)


class QwenImageEncoderGPU:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"encoder_name": (["qwen3vl_8b_heretic-Q6_K.gguf"],)}}
    RETURN_TYPES = ("CLIP",)
    FUNCTION = "load"
    CATEGORY = "local/qwen-image"

    def load(self, encoder_name):
        require_gpu_mode()
        with torch.cuda.device(1):
            clip, = nodes.NODE_CLASS_MAPPINGS["CLIPLoaderGGUF"]().load_clip(encoder_name, "qwen_image")
            fully_load(clip.patcher, "cuda:1")
        return (clip,)


class QwenImageVAEGPU:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"vae_name": (["qwen_image_2.1_vae_bf16.safetensors"],)}}
    RETURN_TYPES = ("VAE",)
    FUNCTION = "load"
    CATEGORY = "local/qwen-image"

    def load(self, vae_name):
        require_gpu_mode()
        with torch.cuda.device(0):
            vae, = nodes.VAELoader().load_vae(vae_name)
            vae.disable_offload = True
            fully_load(vae.patcher, "cuda:0")
        return (vae,)


WEB_DIRECTORY = "web"
NODE_CLASS_MAPPINGS = {cls.__name__: cls for cls in (QwenImageDiTGPU, QwenImageEncoderGPU, QwenImageVAEGPU)}
