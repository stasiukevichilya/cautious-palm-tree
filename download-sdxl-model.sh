#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
mkdir -p models/sdxl-base-1.0
# Download on CPU; the normal service remains strictly offline and GPU-only.
if (( $# == 0 )); then set -- docker compose; fi
"$@" --profile sdxl run --rm --no-deps \
  --user "$(id -u):$(id -g)" \
  -e HF_HUB_OFFLINE=0 -e TRANSFORMERS_OFFLINE=0 \
  -v "$PWD/models/sdxl-base-1.0:/download" \
  -e SDXL_MODEL_PATH=/download sdxl python download.py
