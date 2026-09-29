"""Loaders pinned to the model card placement: DiT and VAE fully on cuda:0, text encoder fully on CPU."""
import json
import logging
import weakref
from pathlib import Path

from aiohttp import web
from server import PromptServer

import torch
import nodes
import comfy.model_management as mm
from comfy.cli_args import args

log = logging.getLogger("qwen-image-uc")
MANIFEST = "/opt/qwen-image-uc/models.json"
verified_models = {}


def tensor_devices(model):
    return {str(t.device) for t in list(model.parameters()) + list(model.buffers())}


def pinned_file(folder, manifest=MANIFEST):
    """Files of this folder pinned in the manifest; the UI offers nothing else."""
    return [Path(x["path"]).name for x in json.loads(Path(manifest).read_text())["files"]
            if x["path"].startswith(folder + "/")]


@PromptServer.instance.routes.get("/local-qwen-image-uc/devices")
async def device_status(request):
    models = {}
    for name, (reference, target) in verified_models.items():
        model = reference()
        if model is not None:
            models[name] = {"expected": target, "devices": sorted(tensor_devices(model))}
    free, total = torch.cuda.mem_get_info(0)
    memory = {"name": torch.cuda.get_device_name(0), "free_bytes": free, "total_bytes": total,
              "allocated_bytes": torch.cuda.memory_allocated(0),
              "reserved_peak_bytes": torch.cuda.max_memory_reserved(0)}
    return web.json_response({"highvram": args.highvram, "models": models, "memory": memory})


def require_mode():
    if not args.highvram or args.gpu_only or torch.cuda.device_count() != 1:
        raise RuntimeError("Qwen-Image UC requires --highvram (not --gpu-only) and exactly one visible GPU")


def verify(patcher, target):
    devices = tensor_devices(patcher.model)
    if devices != {target}:
        raise RuntimeError(f"Model tensors are not entirely on {target}: {devices}")
    log.info("Verified placement: %s", json.dumps({"device": target, "type": type(patcher.model).__name__}))
    verified_models[type(patcher.model).__name__] = (weakref.ref(patcher.model), target)


def fully_load_gpu(patcher):
    # With --highvram ComfyUI keeps the DiT on the GPU; the VAE keeps its CPU offload_device but
    # disable_offload stops it from moving there. What counts is where the tensors end up.
    if patcher.load_device != torch.device("cuda:0"):
        raise RuntimeError(f"Refusing off-GPU placement: {patcher.load_device}")
    mm.load_models_gpu([patcher], force_full_load=True)
    verify(patcher, "cuda:0")


class QwenImageUCDiTGPU:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"model_name": (pinned_file("diffusion_models"),)}}
    RETURN_TYPES = ("MODEL",)
    FUNCTION = "load"
    CATEGORY = "local/qwen-image-uc"

    def load(self, model_name):
        require_mode()
        model, = nodes.NODE_CLASS_MAPPINGS["UnetLoaderGGUF"]().load_unet(model_name)
        fully_load_gpu(model)
        return (model,)


class QwenImageUCEncoderCPU:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"encoder_name": (pinned_file("text_encoders"),)}}
    RETURN_TYPES = ("CLIP",)
    FUNCTION = "load"
    CATEGORY = "local/qwen-image-uc"

    def load(self, encoder_name):
        require_mode()
        clip, = nodes.CLIPLoader().load_clip(encoder_name, "qwen_image", device="cpu")
        if clip.patcher.load_device != torch.device("cpu"):
            raise RuntimeError(f"Text encoder must run on CPU, got {clip.patcher.load_device}")
        mm.load_models_gpu([clip.patcher], force_full_load=True)
        verify(clip.patcher, "cpu")
        return (clip,)


class QwenImageUCVAEGPU:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"vae_name": (pinned_file("vae"),)}}
    RETURN_TYPES = ("VAE",)
    FUNCTION = "load"
    CATEGORY = "local/qwen-image-uc"

    def load(self, vae_name):
        require_mode()
        vae, = nodes.VAELoader().load_vae(vae_name)
        vae.disable_offload = True
        fully_load_gpu(vae.patcher)
        return (vae,)


WEB_DIRECTORY = "web"
NODE_CLASS_MAPPINGS = {cls.__name__: cls for cls in (QwenImageUCDiTGPU, QwenImageUCEncoderCPU, QwenImageUCVAEGPU)}
