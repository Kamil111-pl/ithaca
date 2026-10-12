#!/usr/bin/env bash
# Unattended: build ithaca-local, then evaluate it against the stock base.
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"; ROOT="$(dirname "$HERE")"
MLX="${ITHACA_HOME:-$HOME/.ithaca}/mlx/bin/python"
export HF_HOME="$HERE/hf-cache"
cd "$ROOT"

"$HERE/scripts/full_build.sh" || { echo "BUILD FAILED"; exit 1; }

echo "[$(date +%T)] eval: stock base vs ithaca-local"
serve_eval() {  # $1 = model dir or repo, $2 = port, $3 = label
  ( cd "$HERE/weights" && HF_HOME="${ITHACA_HOME:-$HOME/.ithaca}/hf-serve" "$MLX" -m mlx_lm server --model "$1" --port "$2" --temp 0.6 --top-p 0.9 --max-tokens 2048 >"$HERE/runs/serve-$3.log" 2>&1 ) &
  local pid=$!
  for _ in $(seq 1 90); do curl -sf -o /dev/null "http://127.0.0.1:$2/health" && break; sleep 2; done
  "$ROOT/ithaca" eval --url "http://127.0.0.1:$2/v1" --model "$1" --timeout 240 | tee "$HERE/runs/eval-$3.txt"
  kill $pid 2>/dev/null; sleep 3
}
mkdir -p "${ITHACA_HOME:-$HOME/.ithaca}/hf-serve/hub"
# The stock 4-bit base, loaded from the HF cache path.
BASE_DIR="$(HF_HUB_DISABLE_XET=1 "$MLX" -c "from huggingface_hub import snapshot_download; print(snapshot_download('mlx-community/Qwen3-4B-Instruct-2507-4bit'))")"
serve_eval "$BASE_DIR" 8082 base
serve_eval ithaca-local 8083 ithaca-local
echo "[$(date +%T)] all done"
