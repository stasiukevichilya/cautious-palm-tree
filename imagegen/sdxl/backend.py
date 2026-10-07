import gc
import importlib.metadata
import json
import logging
import resource
import time

from settings import COMPONENTS, FILES, MODEL_ID, REVISION

log = logging.getLogger("sdxl")


class Cancelled(Exception):
    pass


def assert_cuda_components(pipe):
    placement = {}
    for name in COMPONENTS:
        module = getattr(pipe, name)
        devices = {str(t.device) for t in list(module.parameters()) + list(module.buffers())}
        if not devices or any(not d.startswith("cuda:") for d in devices):
            raise RuntimeError(f"{name} is not entirely on CUDA: {sorted(devices)}")
        placement[name] = sorted(devices)
    return placement


class Backend:
    def __init__(self, settings):
        self.settings = settings
        self.pipe = None
        self.info = {}

    def load(self):
        started = time.monotonic()
        import torch
        from diffusers import StableDiffusionXLPipeline

        self.torch = torch
        required = 1 if self.settings.mode == "single" else 2
        if not torch.cuda.is_available() or torch.cuda.device_count() != required:
            raise RuntimeError(f"{self.settings.mode} mode requires exactly {required} visible CUDA GPU(s)")
        budgets = []
        for index in range(required):
            free, total = torch.cuda.mem_get_info(index)
            budget = free - int(self.settings.vram_reserve_gib * 1024**3)
            if budget < 4 * 1024**3:
                raise RuntimeError(f"CUDA {index}: insufficient free VRAM; stop other GPU workloads")
            torch.cuda.set_per_process_memory_fraction(budget / total, index)
            budgets.append({"index": index, "startup_free_bytes": free, "allocator_budget_bytes": budget})
        manifest = json.loads((self.settings.model_path / "manifest.json").read_text())
        if manifest["revision"] != REVISION or manifest["model_id"] != MODEL_ID:
            raise RuntimeError("Snapshot revision does not match this image")
        for name in FILES:
            path = self.settings.model_path / name
            if not path.is_file() or path.stat().st_size != manifest["files"][name]["size"]:
                raise RuntimeError(f"Incomplete snapshot: {name}")
        options = dict(torch_dtype=torch.float16, variant="fp16", use_safetensors=True,
                       local_files_only=True, low_cpu_mem_usage=True, add_watermarker=False)
        if required == 2:
            options.update(device_map="balanced", max_memory={
                0: self.settings.gpu0_budget, 1: self.settings.gpu1_budget})
        self.pipe = StableDiffusionXLPipeline.from_pretrained(str(self.settings.model_path), **options)
        if required == 1:
            self.pipe.to("cuda:0")
        if self.settings.vae_tiling:
            self.pipe.enable_vae_tiling()
            # SDXL's default threshold is 1024, so it would not tile our 1024px outputs.
            self.pipe.vae.tile_sample_min_size = 512
            self.pipe.vae.tile_latent_min_size = 512 // self.pipe.vae_scale_factor
        self.pipe.set_progress_bar_config(disable=True)
        self.info = {
            "model_id": MODEL_ID, "revision": REVISION, "mode": self.settings.mode,
            "placement": assert_cuda_components(self.pipe),
            "device_map": getattr(self.pipe, "hf_device_map", None),
            "scheduler": type(self.pipe.scheduler).__name__,
            "versions": {name: importlib.metadata.version(name) for name in
                         ("torch", "diffusers", "transformers", "accelerate")},
            "gpus": [{"index": i, "name": torch.cuda.get_device_name(i),
                      "uuid": str(getattr(torch.cuda.get_device_properties(i), "uuid", "unavailable"))}
                     for i in range(required)],
            "watermark": False,
            "vae_tiling": self.settings.vae_tiling,
            "vae_tile_size": 512 if self.settings.vae_tiling else None,
            "allocator_budgets": budgets,
        }
        log.info("Pipeline placement: %s", json.dumps(self.info))
        self.warmup()
        torch.cuda.empty_cache()
        self.info["startup_seconds"] = time.monotonic() - started
        self.info["startup_peak_rss_bytes"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024

    def warmup(self):
        with self.torch.inference_mode():
            self.pipe(prompt="A ceramic cup", width=1024, height=1024,
                      num_inference_steps=2, guidance_scale=5.0,
                      generator=self.torch.Generator(device=self.pipe._execution_device).manual_seed(0))
        self.synchronize()
        assert_cuda_components(self.pipe)

    def synchronize(self):
        for i in range(self.torch.cuda.device_count()):
            self.torch.cuda.synchronize(i)

    def generate(self, parameters, cancel, progress):
        torch = self.torch
        warnings = []
        # Diffusers otherwise logs the truncated text, which may contain private prompts.
        for field in ("prompt", "negative_prompt"):
            for tokenizer in (self.pipe.tokenizer, self.pipe.tokenizer_2):
                if len(tokenizer(parameters[field])["input_ids"]) > tokenizer.model_max_length:
                    warnings.append(f"{field} exceeds {tokenizer.model_max_length} tokens; shorten it")
        if warnings:
            raise ValueError("; ".join(sorted(set(warnings))))
        for i in range(torch.cuda.device_count()):
            torch.cuda.reset_peak_memory_stats(i)

        def on_step(pipe, index, timestep, kwargs):
            if cancel.is_set():
                raise Cancelled()
            progress(index + 1)
            return kwargs

        started = time.monotonic()
        try:
            with torch.inference_mode():
                result = self.pipe(
                    **{k: parameters[k] for k in ("prompt", "negative_prompt", "width", "height", "guidance_scale")},
                    num_inference_steps=parameters["steps"],
                    generator=torch.Generator(device=self.pipe._execution_device).manual_seed(parameters["seed"]),
                    callback_on_step_end=on_step,
                ).images[0]
            if cancel.is_set():
                raise Cancelled()
            self.synchronize()
            memory = [{"index": i, "allocated_peak_bytes": torch.cuda.max_memory_allocated(i),
                       "reserved_peak_bytes": torch.cuda.max_memory_reserved(i),
                       "free_bytes": torch.cuda.mem_get_info(i)[0]}
                      for i in range(torch.cuda.device_count())]
            return result, {**self.info, "seconds": time.monotonic() - started, "memory": memory,
                            "process_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024}
        finally:
            self.synchronize()

    def is_oom(self, error):
        return isinstance(error, self.torch.cuda.OutOfMemoryError)

    def recover(self):
        # An interrupted decode may have left the VAE upcast; restore its initial dtype.
        self.pipe.vae.to(dtype=self.torch.float16)
        gc.collect()
        self.torch.cuda.empty_cache()
        self.warmup()
