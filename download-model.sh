#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
mkdir -p models
repo=unsloth/Qwen3.8-27B-GGUF
file=Qwen3.8-27B-UD-Q4_K_M.gguf
if [[ ! -f models/revision.txt ]]; then
  revision=$(curl --fail --silent --show-error --location "https://huggingface.co/api/models/$repo" | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha"])')
  [[ "$revision" =~ ^[0-9a-f]{40}$ ]]
  printf '%s\n' "$revision" > models/revision.txt
fi
revision=$(cat models/revision.txt)
curl --fail --location --retry 5 --continue-at - \
  "https://huggingface.co/$repo/resolve/$revision/$file" -o "models/$file"
(cd models && sha256sum "$file" > SHA256SUMS)
echo 'Model downloaded; model revision and local SHA256 recorded.'
