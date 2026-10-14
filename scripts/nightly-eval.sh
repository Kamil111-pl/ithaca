#!/usr/bin/env bash
# Nightly agent eval: run the full suite against the local model, compare with
# the previous run, and post a macOS notification with the result.
#
#   scripts/nightly-eval.sh            run now
#   ./ithaca eval-nightly install      run every night at 03:30 (launchd)
#   ./ithaca eval-nightly uninstall
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOME_DIR="${ITHACA_HOME:-$HOME/.ithaca}"
LOG="$HOME_DIR/eval-nightly.log"
MODEL=""
for w in ${ITHACA_MODEL:-} qwen3.5-4b ithaca-local base; do  # same order as ./ithaca start
  [ -d "$ROOT/model/weights/$w" ] && { MODEL="$w"; break; }
done

started_model=""
if ! curl -sf -o /dev/null -m 2 http://127.0.0.1:8080/health; then
  "$ROOT/model/ithaca-model.sh" serve "$MODEL" >> "$HOME_DIR/mlx-server.log" 2>&1 &
  started_model=$!
  for _ in $(seq 1 120); do curl -sf -o /dev/null -m 2 http://127.0.0.1:8080/health && break; sleep 2; done
fi

{
  echo "=== $(date '+%Y-%m-%d %H:%M') · $MODEL · $(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null)"
  "$ROOT/ithaca" eval --model "$MODEL"
  echo "exit=$?"
} >> "$LOG" 2>&1
status=$(tail -1 "$LOG")

[ -n "$started_model" ] && kill "$started_model" 2>/dev/null

summary=$(grep -E "pass rate over|^vs " "$LOG" | tail -2 | tr '\n' ' ')
title="Ithaca nightly eval"
[ "$status" != "exit=0" ] && title="Ithaca eval: regression"
osascript -e "display notification \"${summary//\"/}\" with title \"$title\"" >/dev/null 2>&1 || true
