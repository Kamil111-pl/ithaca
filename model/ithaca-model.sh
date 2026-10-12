#!/usr/bin/env bash
# Ithaca on-device model pipeline (MLX, Apple Silicon).
#
#   distill   record an open-weight teacher driving Ithaca's agent loop → data/teacher.jsonl
#   data      build data/{train,valid}.jsonl (teacher data + open datasets)
#   train     LoRA fine-tune on the 4-bit base (fits in 16 GB with grad checkpointing)
#   fuse      merge the adapters into the full-precision base
#   quantize  4-bit quantize; default uses DWQ (the fused model distills into its quantized copy)
#   build     data → train → fuse → quantize
#   serve     serve weights/ithaca-local on :8080 (OpenAI-compatible, tool calling)
#   whisper   download the speech model used by voice (./ithaca voice)
#   vision    download + serve the local vision model (Qwen3-VL-4B) on :8090 for computer use
#   chat      quick terminal chat with the built model
#   status    show what exists
#
# Override any setting with env vars, e.g.  ITERS=1200 ./ithaca model train
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
ITHACA_HOME="${ITHACA_HOME:-$HOME/.ithaca}"
MLX="$ITHACA_HOME/mlx/bin/python"
APP_PY="$ITHACA_HOME/venv/bin/python"

# Everything large stays on this drive.
export HF_HOME="${HF_HOME:-$HERE/hf-cache}"
export HF_HUB_DISABLE_TELEMETRY=1
# The Xet transfer backend stalled on some networks during testing; plain
# HTTPS is slower at best but reliable.
export HF_HUB_DISABLE_XET="${HF_HUB_DISABLE_XET:-1}"
export PYTHONDONTWRITEBYTECODE=1

# Qwen3-4B-Instruct-2507 (Apache-2.0): strong tool calling, ~2.3 GB at 4-bit.
BASE_MODEL="${BASE_MODEL:-Qwen/Qwen3-4B-Instruct-2507}"
TRAIN_MODEL="${TRAIN_MODEL:-mlx-community/Qwen3-4B-Instruct-2507-4bit}"
ITERS="${ITERS:-600}"
BATCH="${BATCH:-1}"
LAYERS="${LAYERS:-16}"
LR="${LR:-2e-5}"
MAXLEN="${MAXLEN:-4096}"
QUANT="${QUANT:-dwq}"          # dwq | plain
# base: merge adapters into the full-precision base (best; +8 GB download)
# quantized: merge into the dequantized 4-bit base (no extra download; DWQ
#            then repairs the re-quantization error)
FUSE_FROM="${FUSE_FROM:-base}"
RUN="${RUN:-$(date +%Y%m%d-%H%M%S)}"
PORT="${PORT:-8080}"

DATA="$HERE/data"
RUNS="$HERE/runs"
WEIGHTS="$HERE/weights"
FUSED="$WEIGHTS/ithaca-fused"
FINAL="$WEIGHTS/ithaca-local"

clean_appledouble() {
  # macOS writes ._ files on exFAT; *.safetensors globs would pick them up.
  find "$@" -name '._*' -type f -delete 2>/dev/null || true
}

latest_run() { ls -1d "$RUNS"/*/ 2>/dev/null | sort | tail -1; }

cmd="${1:-help}"; shift || true
case "$cmd" in
  distill)
    [ -x "$APP_PY" ] || { echo "Run ./ithaca setup first." >&2; exit 1; }
    PYTHONPATH="$ROOT/backend" exec "$APP_PY" -m ithaca.distill --out "$DATA/teacher.jsonl" "$@"
    ;;

  data)
    mkdir -p "$DATA"
    "$MLX" "$HERE/scripts/prepare_data.py" "$@"
    clean_appledouble "$DATA"
    ;;

  train)
    [ -f "$DATA/train.jsonl" ] || "$0" data
    mkdir -p "$RUNS/$RUN"
    clean_appledouble "$DATA" "$HF_HOME"
    cat > "$RUNS/$RUN/config.yaml" <<YAML
model: "$TRAIN_MODEL"
train: true
data: "$DATA"
fine_tune_type: lora
mask_prompt: true
batch_size: $BATCH
iters: $ITERS
num_layers: $LAYERS
learning_rate: $LR
max_seq_length: $MAXLEN
grad_checkpoint: true
steps_per_report: 10
steps_per_eval: 100
save_every: 100
val_batches: 20
adapter_path: "$RUNS/$RUN/adapters"
lora_parameters:
  rank: 16
  scale: 20.0
  dropout: 0.05
YAML
    echo "→ Training run $RUN ($ITERS iters on $TRAIN_MODEL)"
    "$MLX" -m mlx_lm lora --config "$RUNS/$RUN/config.yaml" 2>&1 | tee "$RUNS/$RUN/train.log"
    ;;

  fuse)
    run="$(latest_run)"; [ -n "$run" ] || { echo "No training run found." >&2; exit 1; }
    clean_appledouble "$run" "$HF_HOME"
    rm -rf "$FUSED"
    if [ "$FUSE_FROM" = "quantized" ]; then
      echo "→ Fusing $(basename "$run") adapters into dequantized $TRAIN_MODEL"
      "$MLX" -m mlx_lm fuse --model "$TRAIN_MODEL" --adapter-path "$run/adapters" --save-path "$FUSED" --dequantize
    else
      echo "→ Fusing $(basename "$run") adapters into $BASE_MODEL"
      "$MLX" -m mlx_lm fuse --model "$BASE_MODEL" --adapter-path "$run/adapters" --save-path "$FUSED"
    fi
    clean_appledouble "$FUSED"
    ;;

  quantize)
    [ -d "$FUSED" ] || { echo "Run fuse first." >&2; exit 1; }
    rm -rf "$FINAL" "$WEIGHTS/ithaca-plain4"
    "$MLX" -m mlx_lm convert --hf-path "$FUSED" --mlx-path "$WEIGHTS/ithaca-plain4" -q --q-bits 4 --q-group-size 64
    clean_appledouble "$WEIGHTS/ithaca-plain4"
    if [ "$QUANT" = "dwq" ]; then
      # Two-phase DWQ so teacher and student never share 16 GB of memory:
      #  1) the fused full-precision model writes its top-k logits to disk,
      #  2) the 4-bit student is trained to match them (KL divergence).
      TARGETS="$HERE/runs/dwq-targets"
      rm -rf "$TARGETS"
      DWQ_ARGS=(--num-samples "${DWQ_SAMPLES:-256}" --max-seq-length "${DWQ_MAXLEN:-1024}" --batch-size 1 --target-dir "$TARGETS")
      [ -n "${DWQ_DATA:-}" ] && DWQ_ARGS+=(--data-path "$DWQ_DATA")
      echo "→ DWQ 1/2: teacher targets"
      if "$MLX" -m mlx_lm dwq --model "$FUSED" "${DWQ_ARGS[@]}" --targets-only && \
         clean_appledouble "$TARGETS" && \
         echo "→ DWQ 2/2: distilling into the 4-bit student" && \
         "$MLX" -m mlx_lm dwq --model "$FUSED" --quantized-model "$WEIGHTS/ithaca-plain4" --mlx-path "$FINAL" \
             --bits 4 --group-size 64 --grad-checkpoint "${DWQ_ARGS[@]}"; then
        rm -rf "$TARGETS" "$WEIGHTS/ithaca-plain4"
      else
        echo "DWQ failed; keeping plain 4-bit quantization." >&2
        rm -rf "$FINAL"
        mv "$WEIGHTS/ithaca-plain4" "$FINAL"
        QUANT=plain
      fi
    else
      mv "$WEIGHTS/ithaca-plain4" "$FINAL"
    fi
    clean_appledouble "$FINAL"
    run="$(latest_run)"
    cat > "$FINAL/ITHACA.md" <<MD
# ithaca-local

- Base: $( [ "$FUSE_FROM" = quantized ] && echo "$TRAIN_MODEL (dequantized)" || echo "$BASE_MODEL") (Apache-2.0)
- Fine-tune: LoRA on $(grep '^model:' "${run}config.yaml" 2>/dev/null | cut -d'"' -f2), run $(basename "${run:-none}"), $(grep '^iters:' "${run}config.yaml" 2>/dev/null | awk '{print $2}') iters
- Data: $(wc -l < "$DATA/train.jsonl" | tr -d ' ') examples (open-weight teacher trajectories + Apache-2.0 datasets; see model/README.md)
- Quantization: 4-bit, group size 64 ($QUANT)
- Built: $(date)
MD
    du -sh "$FINAL"
    ;;

  build)
    "$0" data
    "$0" train
    "$0" fuse
    "$0" quantize
    echo "✓ Built $FINAL — start it with: ./ithaca model serve"
    ;;

  serve)
    target="${1:-ithaca-local}"
    [ -d "$WEIGHTS/$target" ] || { echo "No weights at $WEIGHTS/$target. Run ./ithaca model build, or ./ithaca model base and then ./ithaca model serve base." >&2; exit 1; }
    clean_appledouble "$WEIGHTS/$target"
    cd "$WEIGHTS"
    # Served under its folder name, so the API model id is "$target". The
    # server lists every model in the HF cache on /v1/models; point it at an
    # empty cache so only this model shows (and exFAT ._ files can't break it).
    mkdir -p "$ITHACA_HOME/hf-serve/hub"
    # Memory-safe defaults for a 16 GB Mac: small prefill chunks, a bounded
    # prompt cache, and one request at a time (OOM kills the generation thread).
    HF_HOME="$ITHACA_HOME/hf-serve" exec "$MLX" -m mlx_lm server --model "$target" --host 127.0.0.1 --port "$PORT" \
      --temp 0.6 --top-p 0.9 --max-tokens 4096 --prefill-step-size "${PREFILL_STEP:-512}" \
      --prompt-cache-size 2 --prompt-cache-bytes "${PROMPT_CACHE_BYTES:-1500000000}" \
      --decode-concurrency 1 --prompt-concurrency 1
    ;;

  whisper)
    # Speech recognition for voice: Whisper small.en (MIT), ~480 MB.
    mkdir -p "$WEIGHTS"
    "$ITHACA_HOME/venv/bin/python" - "${WHISPER_MODEL:-mlx-community/whisper-small.en-mlx}" "$WEIGHTS/whisper" <<'PY'
import shutil, sys
from huggingface_hub import snapshot_download
shutil.copytree(snapshot_download(sys.argv[1]), sys.argv[2], dirs_exist_ok=True)
print("copied", sys.argv[1], "→", sys.argv[2])
PY
    clean_appledouble "$WEIGHTS/whisper"
    ;;

  vision)
    # Local vision model for computer use: Qwen3-VL-4B (Apache-2.0), served by mlx-vlm on :8090.
    VLM="${VISION_MODEL:-mlx-community/Qwen3-VL-4B-Instruct-4bit}"
    "$MLX" -c "import mlx_vlm" 2>/dev/null || "$ITHACA_HOME/mlx/bin/pip" install -q mlx-vlm
    if [ ! -d "$WEIGHTS/vision" ]; then
      mkdir -p "$WEIGHTS"
      "$MLX" - "$VLM" "$WEIGHTS/vision" <<'PY'
import shutil, sys
from huggingface_hub import snapshot_download
shutil.copytree(snapshot_download(sys.argv[1]), sys.argv[2], dirs_exist_ok=True)
print("copied", sys.argv[1], "→", sys.argv[2])
PY
      clean_appledouble "$WEIGHTS/vision"
    fi
    cd "$WEIGHTS"
    mkdir -p "$ITHACA_HOME/hf-serve/hub"
    HF_HOME="$ITHACA_HOME/hf-serve" exec "$MLX" -m mlx_vlm.server --host 127.0.0.1 --port "${VISION_PORT:-8090}" --model vision --max-tokens 2048 \
      --max-kv-size "${VISION_MAX_KV:-32768}" --max-num-seqs 1 --vision-cache-size 4 --prefill-step-size 512
    ;;

  base)
    # Stock 4-bit base in weights/base: serve it before a build finishes
    # (./ithaca model serve base) or as the baseline in evals.
    mkdir -p "$WEIGHTS"
    "$MLX" - "$TRAIN_MODEL" "$WEIGHTS/base" <<'PY'
import shutil, sys
from huggingface_hub import snapshot_download
src = snapshot_download(sys.argv[1])
shutil.copytree(src, sys.argv[2], dirs_exist_ok=True)
print("copied", sys.argv[1], "→", sys.argv[2])
PY
    clean_appledouble "$WEIGHTS/base"
    ;;

  chat)
    clean_appledouble "$FINAL"
    exec "$MLX" -m mlx_lm chat --model "$FINAL"
    ;;

  status)
    echo "HF cache:  $HF_HOME ($(du -sh "$HF_HOME" 2>/dev/null | cut -f1))"
    echo "Data:      $( [ -f "$DATA/train.jsonl" ] && echo "$(wc -l < "$DATA/train.jsonl" | tr -d ' ') train / $(wc -l < "$DATA/valid.jsonl" | tr -d ' ') valid" || echo none)"
    echo "Teacher:   $( [ -f "$DATA/teacher.jsonl" ] && echo "$(wc -l < "$DATA/teacher.jsonl" | tr -d ' ') steps" || echo none)"
    echo "Last run:  $(latest_run || echo none)"
    echo "Fused:     $( [ -d "$FUSED" ] && du -sh "$FUSED" | cut -f1 || echo none)"
    echo "Final:     $( [ -d "$FINAL" ] && du -sh "$FINAL" | cut -f1 || echo none)"
    ;;

  *)
    sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
    ;;
esac
