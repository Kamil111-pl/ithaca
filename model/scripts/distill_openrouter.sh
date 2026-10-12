#!/usr/bin/env bash
# Distill an open-weight teacher served by OpenRouter (default: GLM-5, MIT
# license) into ithaca-local, then build and evaluate it.
#
# Your key is read from $OPENROUTER_API_KEY, or from ~/.ithaca/openrouter.key
# (create it with:  umask 077; pbpaste > ~/.ithaca/openrouter.key ).
#
#   model/scripts/distill_openrouter.sh
#   TEACHER_MODEL=z-ai/glm-5.3 PROMPTS=300 model/scripts/distill_openrouter.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"; ROOT="$(dirname "$HERE")"
KEY_FILE="${ITHACA_HOME:-$HOME/.ithaca}/openrouter.key"
if [ -z "${OPENROUTER_API_KEY:-}" ] && [ -f "$KEY_FILE" ]; then
  OPENROUTER_API_KEY="$(tr -d '[:space:]' < "$KEY_FILE")"
fi
[ -n "${OPENROUTER_API_KEY:-}" ] || { echo "Set OPENROUTER_API_KEY or create $KEY_FILE first." >&2; exit 1; }

MODEL="${TEACHER_MODEL:-z-ai/glm-5}"
echo "[$(date +%T)] distilling $MODEL via OpenRouter (${PROMPTS:-200} prompts)"
TEACHER_API_KEY="$OPENROUTER_API_KEY" "$ROOT/ithaca" model distill \
  --teacher-url https://openrouter.ai/api/v1 --teacher-model "$MODEL" \
  --prompts "${PROMPTS:-200}" --concurrency "${CONCURRENCY:-4}" --max-rounds 8 --timeout 600
unset OPENROUTER_API_KEY TEACHER_API_KEY

echo "[$(date +%T)] waiting for the student model download (if still running)"
while pgrep -f "snapshot_download" >/dev/null; do sleep 20; done

TEACHER=none FUSE_FROM="${FUSE_FROM:-quantized}" ITERS="${ITERS:-600}" MAXLEN="${MAXLEN:-6144}" \
  "$HERE/scripts/build_and_eval.sh"
