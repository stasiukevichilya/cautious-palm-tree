#!/bin/sh
# Context-compression benchmark (agent/bench/ctx_bench.py) inside the compose network.
# Usage: agent/bench/run.sh --model qwen3.8-27b-mtp [--routes direct,hr,bili,hb] [--steps N]
set -e
cd "$(dirname "$0")/../.."
mkdir -p outputs/bench/ctx
git log --stat -15 > outputs/bench/ctx/git-log.txt
exec docker run --rm --network ml-local_default \
  -v "$PWD":/repo:ro -v "$PWD/agent/bench":/bench-src:ro -v "$PWD/outputs/bench/ctx":/out \
  -v "$PWD/outputs/bench/ctx/git-log.txt":/bench/git-log.txt:ro \
  --entrypoint python local/ml-headroom:1 -u /bench-src/ctx_bench.py "$@"
