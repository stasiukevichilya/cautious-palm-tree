import os
from dataclasses import dataclass
from pathlib import Path

MODEL_ID = "stabilityai/stable-diffusion-xl-base-1.0"
REVISION = "462165984030d82259a11f4367a4eed129e94a7b"
COMPONENTS = ("unet", "vae", "text_encoder", "text_encoder_2")
FILES = (
    "model_index.json", "scheduler/scheduler_config.json", "LICENSE.md",
    "text_encoder/config.json", "text_encoder/model.fp16.safetensors",
    "text_encoder_2/config.json", "text_encoder_2/model.fp16.safetensors",
    "unet/config.json", "unet/diffusion_pytorch_model.fp16.safetensors",
    "vae/config.json", "vae/diffusion_pytorch_model.fp16.safetensors",
    *[f"{directory}/{name}" for directory in ("tokenizer", "tokenizer_2")
      for name in ("merges.txt", "vocab.json", "special_tokens_map.json", "tokenizer_config.json")],
)


@dataclass(frozen=True)
class Settings:
    model_path: Path = Path("/models")
    output_path: Path = Path("/outputs")
    mode: str = "single"
    queue_size: int = 4
    max_results: int = 100
    retention_hours: int = 168
    vae_tiling: bool = True
    vram_reserve_gib: float = 1.5
    gpu0_budget: str = "11GiB"
    gpu1_budget: str = "9GiB"

    @classmethod
    def from_env(cls):
        config = cls(
            model_path=Path(os.getenv("SDXL_MODEL_PATH", "/models")),
            output_path=Path(os.getenv("SDXL_OUTPUT_PATH", "/outputs")),
            mode=os.getenv("SDXL_MODE", "single"),
            queue_size=int(os.getenv("SDXL_QUEUE_SIZE", "4")),
            max_results=int(os.getenv("SDXL_MAX_RESULTS", "100")),
            retention_hours=int(os.getenv("SDXL_RETENTION_HOURS", "168")),
            vae_tiling=os.getenv("SDXL_VAE_TILING", "1") == "1",
            vram_reserve_gib=float(os.getenv("SDXL_VRAM_RESERVE_GIB", "1.5")),
            gpu0_budget=os.getenv("SDXL_GPU0_BUDGET", "11GiB"),
            gpu1_budget=os.getenv("SDXL_GPU1_BUDGET", "9GiB"),
        )
        if config.mode not in ("single", "dual"):
            raise ValueError("SDXL_MODE must be single or dual")
        if min(config.queue_size, config.max_results, config.retention_hours) < 1:
            raise ValueError("Queue and retention limits must be positive")
        if not 1 <= config.vram_reserve_gib <= 8:
            raise ValueError("SDXL_VRAM_RESERVE_GIB must be between 1 and 8")
        return config
