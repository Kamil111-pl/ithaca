# Ithaca

A private, local-first AI workspace for your Mac: an agent that can use tools safely, documents, deep research, long-term memory, and a focus timer, wrapped in an iOS 26–style Liquid Glass interface. It ships with a pipeline to fine-tune, distill and quantize its own on-device model that runs on a 16 GB MacBook.

Ithaca is a ground-up redesign of the ideas in [Odysseus](https://github.com/odysseus-dev/odysseus), built to fix that project's structural problems (see [Why Ithaca](#why-ithaca)). In the Odyssey, Ithaca is where Odysseus was trying to get home to.

```bash
./ithaca setup      # Python env + web build (first run only)
./ithaca start      # → http://127.0.0.1:7100
```

Add a model in **Settings → Models** (Claude, Ollama, LM Studio, OpenAI-compatible, or Ithaca's own local model), and start chatting.

---

## What's inside

| Area | What it does |
|---|---|
| **Chat + agent** | Streaming agent with native tool calling, live tool cards, inline approvals, reasoning disclosure, model picker, reconnect-safe runs. |
| **Tools** | Workspace files (read/write/edit/search), sandboxed shell, web search + fetch, documents, memory, deep research, focus stats, plus any MCP server. |
| **Documents** | Markdown editor with live preview, autosave, AI Assist (revise a selection or the whole doc), "chat about this document". |
| **Research** | Plans queries → searches → reads sources in parallel → writes a cited report saved as a document, with a live timeline. |
| **Memory** | Hybrid recall (SQLite FTS5 + vectors, rank-fused), auto-recall in chat, full editing, and tainted-memory tracking. |
| **Voice** | Say “Hey Ithaca, …” anywhere on your Mac (`./ithaca voice`), or use the mic in chat. Local Whisper; replies spoken aloud. |
| **Agent reliability** | Tools auto-loaded per request, a visible plan the model keeps updated, and checks that send it back when it claims work it didn't do or stops mid-plan. |
| **Focus** | A Pomodoro timer modeled on Tomato: progress ring, wavy break ring, cycle dots, stats, streaks, chime and notifications. |
| **MCP** | Connect local MCP servers (commands) or remote ones by URL — Streamable HTTP or SSE, with API-key headers or OAuth sign-in (PKCE, dynamic registration, token refresh). |
| **Settings** | Providers with one-click tool-support probing, agent policy, sandbox controls and self-test, MCP servers, embeddings, appearance, export. |
| **On-device model** | `./ithaca model build` produces `ithaca-local`: Qwen3-4B fine-tuned on agent trajectories, then DWQ-quantized to 4 bits (~2.3 GB). |

## Why Ithaca

Each fix targets a downside found while reading the Odysseus code:

| Odysseus | Ithaca |
|---|---|
| Tool calls parsed out of prose with a dozen regex formats; native tools enabled from a list of model-name substrings | Native tool calling only, through one provider-neutral adapter. Tool support is **probed once per model** and remembered. The single text fallback is the Hermes `<tool_call>` format the local model's own chat template emits. |
| Regex intent classifier and keyword lists decide which tools a turn gets | **Tool search**: the model starts with 3 core tools and loads others with `tool_search` (BM25 over tool docs). Prompts stay small for 4k–32k-context local models. |
| Shell and file tools run as the app user with no sandbox (its own threat model's #1 gap) | Every command runs in **macOS Seatbelt** (or Docker): writes confined to the workspace, no reads of `$HOME` or other drives, no network unless approved. |
| Prompt-injection defense is mainly a "don't follow instructions" preamble | Enforced by the runtime: **taint tracking**. Once a run reads web or MCP content, writes, code execution, external calls and URL-borne data leaks need your approval. Memories saved in tainted runs are replayed as untrusted data. |
| SQLite + several JSON files + ChromaDB, 54 hand-rolled `ALTER TABLE`s | **One SQLite file**: chats, an append-only event log, documents, memory vectors, settings. Numbered migrations. |
| Chat history mutated in place | **Append-only event log**: runs survive tab closes, clients reattach, history replays byte-identically (prompt caches and preserved-thinking stay valid). |
| 6k-line agent loop, 41k-line stylesheet, 96 global scripts | Small modules: agent loop ≈ 420 lines; React + TypeScript UI with a token-based design system. |
| No measurement of agent quality | **Eval suite** (`./ithaca eval`): 100 tasks with real tools and a simulated world (web, the user's Chrome with Google Docs and Gmail, Mac apps), including prompt-injection and approval tasks. Each run is compared with the previous one and fails on a regression. |

## Architecture

```
web/ (React + TS + Vite)            backend/ithaca (FastAPI)
  Liquid Glass UI  ── SSE/JSON ──▶  main.py           HTTP API, auth, host/CSRF guard
                                    agent/runner.py   agent loop, approvals, runaway guards
                                    agent/prompts.py  stable system prompt + stored turn context
                                    providers/        Anthropic SDK · OpenAI-compatible · tag stream
                                    tools/            registry (effects, tool search) + built-ins
                                    security.py       taint tracking + approval policy
                                    sandbox.py        Seatbelt / Docker execution
                                    events.py         append-only log → provider-neutral history
                                    memory.py         FTS5 + vectors, rank fusion
                                    research.py       plan → search → read → cite
                                    mcp_client.py     MCP stdio servers → tools
                                    distill.py        teacher trajectories → training data
                                    db.py + migrations/  one SQLite file
model/  MLX pipeline: data → LoRA → fuse → DWQ → serve
evals/  agent task suite + runner
```

**A turn, end to end.** The user message is stored with its turn context (date, recalled memories, attached document). The loop replays the event log into neutral messages and streams from the provider. Tool calls are checked for availability and argument schema, then put through `security.decide`: allowed calls run immediately; risky ones become an approval card and the run waits. Results from outside the machine are wrapped as `<untrusted-content>` and taint the run. Everything is appended to the log and broadcast to connected clients.

## Security model

- Single user, bound to `127.0.0.1`; password with scrypt hash; `SameSite=Strict` cookie; custom-header CSRF check; Host allow-list against DNS rebinding.
- Shell: Seatbelt profile (workspace-only writes, `$HOME` and `/Volumes` unreadable, network off). Check it in **Settings → Sandbox → Run self-test**.
- Workspace file tools reject paths that resolve outside the workspace.
- `web_fetch` blocks private, loopback and link-local addresses at every redirect hop.
- Approval modes: **Smart** (default; asks when risky), **Always ask**, **Never**.

## The on-device model

`./ithaca model build` runs the whole pipeline on a 16 GB Apple Silicon Mac (details in [model/README.md](model/README.md)):

1. **Distill** (optional, recommended): an open-weight teacher (default Qwen3-14B, run locally) drives Ithaca's real agent loop; every step becomes a training example with the exact prompt and tool schemas the student will see.
2. **Data**: teacher trajectories + Hermes function-calling (Apache-2.0) + everyday conversation.
3. **Train**: LoRA on Qwen3-4B-Instruct-2507 (4-bit base, gradient checkpointing).
4. **Fuse** into the full-precision base.
5. **Quantize with DWQ**: the fused model is the teacher for its own 4-bit copy (two phases, so they never share memory).
6. **Serve**: `./ithaca model serve` → OpenAI-compatible, with tool calling, on `:8080`.

> **Why not distill Claude?** Anthropic's terms (like OpenAI's and Google's) prohibit using model outputs to train other models, and model weights and "knowledge" can't be exported anyway. `ithaca.distill` refuses those endpoints. It works with open-weight teachers whose licenses allow it, such as Qwen3, DeepSeek-V3 or Llama (Llama requires "Llama" in the name if you distribute the result).

## Commands

```bash
./ithaca setup | start | dev | build | test
./ithaca voice                                  # "Hey Ithaca" listener
./ithaca eval --url http://127.0.0.1:8080/v1 --model ithaca-local
./ithaca eval --kind anthropic --model claude-opus-5-5
./ithaca eval --category browser,mac --repeat 3 -v   # flakiness, with failed checks
python evals/run.py --validate                     # check tasks.json
./ithaca model build | serve | status | chat | distill … | data | train | fuse | quantize | base
```

## Where things live

Everything that matters is on this drive: source, `data/ithaca.db` (your chats, documents, memory, settings and API keys; keep it private), `data/workspace/` (the agent's sandbox), `model/weights/`, and `model/hf-cache/`.
Disposable environments live in `~/.ithaca` (Python venvs, `node_modules`). The drive is exFAT, which has no symlinks, and macOS writes `._` AppleDouble files there that break Python packaging. Delete `~/.ithaca` any time; `./ithaca setup` recreates it.

## Design credits

- Visual language: the [Liquid Glass – iOS 26 Figma study](https://www.figma.com/design/0eCdekIwbXHUPx8ZLmoidJ/Liquid-Glass---ios-26--Community-) by Armando Qose: monochrome spotlight, frosted panels with bright rims, pill-highlighted menu rows.
- Refraction: [liquid-glass-react](https://github.com/rdev/liquid-glass-react) (MIT) for the tab bar, timer and stop controls.
- Focus timer: design inspired by [Tomato](https://github.com/nsh07/Tomato) (GPL-3.0; no code used), restyled from Material 3 Expressive to liquid glass.
- Chat bubbles: shapes, entry motion, typing dots and reply chips after [chat-bubble](https://github.com/dmitrizzle/chat-bubble) (MIT).

See [NOTICE.md](NOTICE.md) for third-party licenses.
