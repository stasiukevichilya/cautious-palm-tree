#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
stack_dir=$PWD
docker() { bash "$stack_dir/docker-wsl.sh" "$@"; }
if [[ -e .env ]]; then
  echo '.env already exists; keep it to preserve pinned versions.'
  exit 1
fi
mkdir -p models workspace
umask 077
tmp=$(mktemp .env.XXXXXX)
trap 'rm -f "$tmp"' EXIT
pin_image() {
  local key=$1 tag=$2 digest
  docker pull "$tag" >&2
  digest=$(docker image inspect "$tag" --format '{{index .RepoDigests 0}}')
  printf '%s=%s\n' "$key" "$digest" >> "$tmp"
}
pin_image NODE_IMAGE node:24-bookworm-slim
node_image=$(sed -n 's/^NODE_IMAGE=//p' "$tmp")
dsh_version=$(docker run --rm "$node_image" npm view @deepseek-ai/dsh version | tr -d '\r')
if [[ ! "$dsh_version" =~ ^0\.1\. ]]; then
  echo "Check LoongSuite compatibility before using DSH $dsh_version" >&2
  exit 1
fi
printf 'DSH_VERSION=%s\n' "$dsh_version" >> "$tmp"
pin_image LLAMA_IMAGE ghcr.io/ggml-org/llama.cpp:server-cuda
pin_image GPU_IMAGE utkuozdemir/nvidia_gpu_exporter:latest
pin_image OTEL_IMAGE otel/opentelemetry-collector-contrib:latest
pin_image PROM_IMAGE prom/prometheus:latest
pin_image TEMPO_IMAGE grafana/tempo:2.9.0
pin_image GRAFANA_IMAGE grafana/grafana:latest
pin_image SOCAT_IMAGE alpine/socat:latest
printf 'GRAFANA_PASSWORD=%s\n' "$(python3 -c 'import secrets; print(secrets.token_hex(24))')" >> "$tmp"
cat >> "$tmp" <<'EOF'
CUDA_VISIBLE_DEVICES=0,1
QWEN_TENSOR_SPLIT=0.57,0.43
QWEN_CTX_SIZE=131072
QWEN_UBATCH_SIZE=128
QWEN_MODEL_FILE=Qwen3.8-27B-UD-Q4_K_M.gguf
EOF
mv -- "$tmp" .env
trap - EXIT
echo 'Created .env with fixed image digests and DSH version. Check GPU order before starting.'
