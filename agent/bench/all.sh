#!/bin/sh
cd /home/ilya/mlstuff/local
for pair in qwen-mtp:qwen3.8-27b-mtp:8081 qwen:qwen3.8-27b:8080 bonsai-mtp:bonsai2-27b-uc-mtp:8089; do
  svc=${pair%%:*}; rest=${pair#*:}; model=${rest%%:*}; port=${rest#*:}
  curl -sf http://127.0.0.1:$port/health >/dev/null || make $svc
  until curl -sf http://127.0.0.1:$port/health >/dev/null; do sleep 5; done
  echo "=== $model $(date +%T)"
  agent/bench/run.sh --model $model > outputs/bench/ctx/$model.log 2>&1
  tail -3 outputs/bench/ctx/$model.log
done
echo "=== done $(date +%T)"
