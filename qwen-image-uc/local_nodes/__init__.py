"""Loaders pinned to the model card placement: DiT and VAE fully on cuda:0, text encoder fully on CPU."""
import json
import logging
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

log = logging.getLogger("qwen-image-uc")
MANIFEST = "/opt/qwen-image-uc/models.json"
verified_models = {}
EXPECTED_CUDA_DEVICES = 1


class Metrics:
    """Prometheus exposition for this profile; state lives in the process and resets on restart."""

    def __init__(self):
        self.registry = CollectorRegistry()
        self.ready = Gauge("qwen_image_uc_ready", "1 when the expected number of CUDA devices is visible",
                           registry=self.registry)
        self.queue_running = Gauge("qwen_image_uc_queue_running", "Prompts being executed", registry=self.registry)
        self.queue_pending = Gauge("qwen_image_uc_queue_pending", "Prompts waiting in the queue", registry=self.registry)
        self.generations = Counter("qwen_image_uc_generations_total", "Finished prompts", ["result"],
                                   registry=self.registry)
        self.duration = Histogram("qwen_image_uc_generation_seconds", "Prompt duration from enqueue to completion",
                                  buckets=(10, 30, 60, 120, 240, 480, 960, 1920), registry=self.registry)
        self.cuda_free = Gauge("qwen_image_uc_cuda_free_bytes", "Free VRAM per device", ["device"],
                               registry=self.registry)
        self.cuda_reserved_peak = Gauge("qwen_image_uc_cuda_reserved_peak_bytes",
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


@PromptServer.instance.routes.get("/local-qwen-image-uc/metrics")
async def metrics_status(request):
    return web.Response(body=METRICS.scrape(PromptServer.instance.prompt_queue),
                        headers={"Content-Type": CONTENT_TYPE_LATEST})


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
