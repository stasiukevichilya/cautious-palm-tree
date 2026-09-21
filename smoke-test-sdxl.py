"""Use the image's Pillow installation; no host Python packages are needed."""
import subprocess
import sys
from pathlib import Path

raise SystemExit(subprocess.call(
    ["docker", "compose", "--profile", "sdxl", "exec", "-T", "sdxl", "python", "smoke.py", *sys.argv[1:]],
    cwd=Path(__file__).resolve().parent,
))
