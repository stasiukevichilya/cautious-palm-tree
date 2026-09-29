"""Select GPUs through ComfyUI's native CUDA context, then assert full placement."""
import importlib
import json
import logging
import os
import sys
import time
import weakref
from pathlib import Path

from aiohttp import web
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from server import PromptServer

import torch
import nodes
import comfy.model_management as mm
from comfy.cli_args import args

log = logging.getLogger("qwen-image-gpu")
verified_models = {}
EXPECTED_CUDA_DEVICES = 2


class Metrics:
    """Prometheus exposition for this profile; state lives in the process and resets on restart."""

    def __init__(self):
        self.registry = CollectorRegistry()
        self.ready = Gauge("qwen_image_ready", "1 when the expected number of CUDA devices is visible",
                           registry=self.registry)
        self.queue_running = Gauge("qwen_image_queue_running", "Prompts being executed", registry=self.registry)
        self.queue_pending = Gauge("qwen_image_queue_pending", "Prompts waiting in the queue", registry=self.registry)
        self.generations = Counter("qwen_image_generations_total", "Finished prompts", ["result"],
                                   registry=self.registry)
        self.duration = Histogram("qwen_image_generation_seconds", "Prompt duration from enqueue to completion",
                                  buckets=(10, 30, 60, 120, 240, 480, 960, 1920), registry=self.registry)
        self.cuda_free = Gauge("qwen_image_cuda_free_bytes", "Free VRAM per device", ["device"],
                               registry=self.registry)
        self.cuda_reserved_peak = Gauge("qwen_image_cuda_reserved_peak_bytes",
                                        "Peak VRAM reserved by torch per device", ["device"],
                                        registry=self.registry)
        self.first_seen = {}
        self.finished = set()

    def scrape(self, prompt_queue):
        running, pending = prompt_queue.get_current_queue_volatile()
        self.queue_running.set(len(running))
        self.queue_pending.set(len(pending))
        now = time.monotonic()
        for item in (*running, *pending):
            self.first_seen.setdefault(item[1], now)
        history = prompt_queue.get_history()
        for prompt_id, entry in history.items():
            if prompt_id in self.finished:
                continue
            status = (entry or {}).get("status") or {}
            result = status.get("status_str")
            self.generations.labels(result if result in ("success", "error") else "error").inc()
            self.finished.add(prompt_id)
            seconds = self._duration(entry, prompt_id)
            self.first_seen.pop(prompt_id, None)
            if seconds is not None:
                self.duration.observe(seconds)
        for prompt_id in [p for p, seen_at in self.first_seen.items() if now - seen_at > 3600 and p not in history]:
            self.first_seen.pop(prompt_id, None)
        self._gpu_gauges()
        return generate_latest(self.registry)

    def _duration(self, entry, prompt_id):
        item = (entry or {}).get("prompt")
        if isinstance(item, tuple):
            extra = item[3] if len(item) > 3 else None
            create_time = extra.get("create_time") if isinstance(extra, dict) else None
            if isinstance(create_time, (int, float)) and not isinstance(create_time, bool):
                return max(0.0, time.time() - create_time / 1000.0)
        seen_at = self.first_seen.get(prompt_id)
        if seen_at is not None:
            return max(0.0, time.monotonic() - seen_at)
        return None

    def _gpu_gauges(self):
        try:
            count = torch.cuda.device_count()
        except Exception:
            count = 0
        self.ready.set(int(count == EXPECTED_CUDA_DEVICES))
        for index in range(count):
            try:
                free, _total = torch.cuda.mem_get_info(index)
                self.cuda_free.labels(str(index)).set(free)
                self.cuda_reserved_peak.labels(str(index)).set(torch.cuda.max_memory_reserved(index))
            except Exception:
                continue


METRICS = Metrics()


@PromptServer.instance.routes.get("/local-qwen-image/metrics")
async def metrics_status(request):
    return web.Response(body=METRICS.scrape(PromptServer.instance.prompt_queue),
                        headers={"Content-Type": CONTENT_TYPE_LATEST})


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
