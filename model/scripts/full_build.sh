#!/usr/bin/env bash
# End-to-end build of ithaca-local on a 16 GB Apple Silicon Mac:
#   1. serve an open-weight teacher (Qwen3-14B, Apache-2.0) locally
#   2. record it driving Ithaca's agent loop (trajectory distillation)
#   3. mix with open datasets, LoRA-train Qwen3-4B-Instruct-2507 (4-bit base)
#   4. fuse into the full-precision base, then DWQ-quantize to 4 bits
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"; ROOT="$(dirname "$HERE")"
MLX="${ITHACA_HOME:-$HOME/.ithaca}/mlx/bin/python"
export HF_HOME="$HERE/hf-cache"
# TEACHER=none: skip distillation and use model/data/teacher.jsonl as is
#   (e.g. after scripts/distill_openrouter.sh).
# TEACHER=self: no extra download. The 4-bit student base drives Ithaca and
# only fully successful trajectories are kept (rejection-sampled
# self-distillation): it learns Ithaca's conventions from its own best runs.
TEACHER="${TEACHER:-mlx-community/Qwen3-14B-4bit}"
STUDENT="${TRAIN_MODEL:-mlx-community/Qwen3-4B-Instruct-2507-4bit}"
[ "$TEACHER" = "self" ] && TEACHER="$STUDENT"
PROMPTS="${PROMPTS:-48}"
cd "$ROOT"

if [ "$TEACHER" != "none" ]; then
echo "[$(date +%T)] 1/5 teacher: $TEACHER"
HF_HUB_DISABLE_XET="${HF_HUB_DISABLE_XET:-1}" "$MLX" -c "from huggingface_hub import snapshot_download; snapshot_download('$TEACHER')" >/dev/null
find "$HF_HOME" -name '._*' -delete 2>/dev/null || true
"$MLX" -m mlx_lm server --model "$TEACHER" --port 8081 --temp 0.7 --max-tokens 2048 \
    --chat-template-args '{"enable_thinking": false}' > "$HERE/runs/teacher-server.log" 2>&1 &
TPID=$!
trap 'kill $TPID 2>/dev/null || true' EXIT
for _ in $(seq 1 120); do curl -sf -o /dev/null http://127.0.0.1:8081/health && break; sleep 2; done

echo "[$(date +%T)] 2/5 distilling $PROMPTS teacher trajectories"
"$HERE/ithaca-model.sh" distill --teacher-url http://127.0.0.1:8081/v1 --teacher-model "$TEACHER" \
    --prompts "$PROMPTS" --concurrency 1 --max-rounds 8 --timeout 900
kill $TPID 2>/dev/null || true; trap - EXIT; sleep 3
else
echo "[$(date +%T)] 1-2/5 using existing teacher data: $(wc -l < "$HERE/data/teacher.jsonl" | tr -d ' ') steps"
fi

echo "[$(date +%T)] 3/5 dataset"
"$HERE/ithaca-model.sh" data --hermes "${HERMES:-800}" --chat "${CHAT:-250}"

echo "[$(date +%T)] 4/5 LoRA training"
ITERS="${ITERS:-500}" MAXLEN="${MAXLEN:-6144}" RUN="${RUN:-ithaca-4b-$(date +%Y%m%d)}" "$HERE/ithaca-model.sh" train

echo "[$(date +%T)] 5/5 fuse + DWQ quantize"
"$HERE/ithaca-model.sh" fuse
DWQ_DATA="$HERE/data" "$HERE/ithaca-model.sh" quantize
echo "[$(date +%T)] done → $HERE/weights/ithaca-local"
