# ithaca-local: the on-device model

A small model tuned to drive Ithaca's agent, quantized to fit comfortably on a 16 GB Apple Silicon Mac alongside the app.

| | |
|---|---|
| Student / base | `Qwen/Qwen3-4B-Instruct-2507` (Apache-2.0), trained as QLoRA on `mlx-community/Qwen3-4B-Instruct-2507-4bit` |
| Default teacher | `mlx-community/Qwen3-14B-4bit` (Apache-2.0), served locally by MLX |
| Final size | ~2.3 GB (4-bit, group size 64, DWQ) |
| Runtime | `mlx_lm.server`: OpenAI-compatible `/v1`, native tool-call parsing |

## One command

```bash
./ithaca model build          # data → train → fuse → quantize   (no teacher data)
model/scripts/full_build.sh   # teacher → distill → data → train → fuse → quantize
./ithaca model serve          # http://127.0.0.1:8080/v1, model id "ithaca-local"
```

Then in Ithaca: **Settings → Models → Ithaca Local (MLX) → refresh**, and **Test** to confirm tool calling.

### Build modes

| Env | Effect |
|---|---|
| `TEACHER=mlx-community/Qwen3-14B-4bit` (default) | A stronger open-weight teacher drives Ithaca. Best data; 8.3 GB extra download. |
| `TEACHER=self` | The 4-bit student itself drives Ithaca, and only clean runs are kept (rejection-sampled self-distillation). No extra download; it teaches Ithaca's conventions rather than new knowledge. |
| `FUSE_FROM=base` (default) | Merge adapters into the full-precision base (8 GB download). |
| `FUSE_FROM=quantized` | Merge into the dequantized 4-bit base, with no extra download. DWQ then repairs the re-quantization error. |

`model/scripts/build_and_eval.sh` runs the build, then evaluates the stock base and `ithaca-local` on the same suite (reports in `model/runs/eval-*.txt`).

Downloads use plain HTTPS (`HF_HUB_DISABLE_XET=1`), because the Xet backend stalled on some networks. Set `HF_TOKEN` for higher Hugging Face rate limits.

Until a build finishes, `./ithaca model base` puts the stock 4-bit base in `weights/base`; serve it with `./ithaca model serve base`.

## Steps

### 1. Distill: teacher trajectories (`ithaca.distill`)

```bash
./ithaca model distill --teacher-url http://127.0.0.1:8081/v1 --teacher-model mlx-community/Qwen3-14B-4bit --prompts 200
```

The teacher writes realistic requests, then runs each one through **Ithaca's real agent loop** in a throwaway data directory (sandboxed shell, real web, documents, memory). Each assistant step becomes one example containing exactly the system prompt, history and tool schemas present at that step. The student therefore learns Ithaca's actual conventions: call `tool_search` first, use the loaded tools, treat `<untrusted-content>` as data, and summarize at the end.

The script refuses Anthropic, OpenAI, Google, xAI and Mistral endpoints, because their terms forbid training other models on outputs. Use open-weight models whose licenses permit it, served locally (MLX, Ollama, LM Studio, vLLM) or by an inference host.

### 2. Data (`scripts/prepare_data.py`)

- Teacher steps, weighted ×2.
- `NousResearch/hermes-function-calling-v1` (Apache-2.0): general function calling, converted to structured `tool_calls`.
- `HuggingFaceTB/smoltalk` everyday conversations: cut at a substantive answer. mlx-lm's `--mask-prompt` trains only on the final assistant turn, and these threads end with "You're welcome!", which collapsed an early test model into pleasantries.

### 3. Train

LoRA rank 16 on 16 layers, LR 2e-5, batch 1, gradient checkpointing, prompt masking, max length 6144 (Ithaca prompts carry tool schemas). Peak memory stays well under 16 GB. Override settings with env vars: `ITERS=1200 LR=1e-5 ./ithaca model train`.

### 4. Fuse

The adapters are merged into the full-precision base (`weights/ithaca-fused`).

### 5. Quantize with DWQ

Distilled Weight Quantization trains the 4-bit model to match the full-precision model's output distribution (KL divergence on its top-1024 logits). On 16 GB it runs in two phases: the fused teacher writes its targets to disk, then only the 4-bit student is loaded to learn them. If DWQ fails, plain 4-bit quantization is kept. Set `QUANT=plain` to skip DWQ.

### 6. Evaluate

```bash
./ithaca model serve &                       # fine-tuned
./ithaca eval --model ithaca-local
```

To compare against the stock base: `./ithaca model base && ./ithaca model serve base`, then `./ithaca eval --model base`.

## Files

```
model/
  ithaca-model.sh          pipeline commands
  scripts/prepare_data.py  dataset builder
  scripts/full_build.sh    teacher → … → quantized model, unattended
  scripts/build_and_eval.sh  full_build + base-vs-tuned evaluation
  data/                    train/valid/teacher jsonl        (git-ignored)
  runs/                    training runs, logs, DWQ targets (git-ignored)
  weights/                 ithaca-fused, ithaca-local        (git-ignored)
  hf-cache/                Hugging Face downloads            (git-ignored)
```
