"""Append SDXL-specific defaults without changing existing environment values."""
import csv
import os
import subprocess
import tempfile
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    destination = root / ".env"
    if not destination.is_file():
        raise SystemExit("Prepare the existing stack first: .env is missing")
    output = subprocess.check_output([
        "nvidia-smi", "--query-gpu=uuid,name", "--format=csv,noheader"], text=True)
    devices = [(row[0].strip(), row[1].strip()) for row in csv.reader(output.splitlines())]
    primary = next((uid for uid, name in devices if "4070 Ti SUPER" in name), None)
    secondary = next((uid for uid, name in devices if "3080 Ti" in name), None)
    if not primary:
        raise SystemExit("RTX 4070 Ti SUPER not found; configure SDXL_GPU0 manually")
    defaults = {"SDXL_GPU0": primary, "SDXL_UID": str(os.getuid()), "SDXL_GID": str(os.getgid()),
                "SDXL_QUEUE_SIZE": "4", "SDXL_MAX_RESULTS": "100", "SDXL_RETENTION_HOURS": "168",
                "SDXL_VAE_TILING": "1", "SDXL_VRAM_RESERVE_GIB": "1.5"}
    if secondary:
        defaults["SDXL_GPU1"] = secondary
    existing = destination.read_text()
    keys = {line.split("=", 1)[0].strip() for line in existing.splitlines()
            if "=" in line and not line.lstrip().startswith("#")}
    additions = {key: value for key, value in defaults.items() if key not in keys}
    if additions:
        content = existing.rstrip("\n") + "\n\n# SDXL\n" + "".join(f"{k}={v}\n" for k, v in additions.items())
        descriptor, temporary = tempfile.mkstemp(prefix=".env.sdxl-", dir=root)
        try:
            with os.fdopen(descriptor, "w") as stream:
                stream.write(content)
            os.chmod(temporary, destination.stat().st_mode & 0o777)
            os.replace(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
    for directory in ("models/sdxl-base-1.0", "outputs/sdxl"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    print("SDXL settings ready; existing values preserved")


if __name__ == "__main__":
    main()
