"""Trajectory distillation: record a teacher model driving Ithaca's own agent loop.

    python -m ithaca.distill --teacher-url http://host/v1 --teacher-model NAME \
        --prompts 300 --out model/data/teacher.jsonl

For each generated user request the teacher runs a full agent turn inside an
isolated, throwaway Ithaca data directory (real tools: sandboxed shell, web,
documents, memory). Every assistant step then becomes one training example
holding exactly the system prompt, history and tool schemas the model saw at
that step, so the student learns Ithaca's real calling conventions
(tool_search first, untrusted-content handling, final summaries).

Only open-weight teachers whose licenses allow training on their outputs are
supported. Hosted proprietary APIs (Anthropic, OpenAI, Google) prohibit using
outputs to train competing models, so they are refused here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

BLOCKED_HOSTS = ("anthropic.com", "claude.ai", "openai.com", "googleapis.com", "generativelanguage", "x.ai", "mistral.ai")

CATEGORIES = [
    "writing and editing documents (drafts, emails, summaries, outlines)",
    "running small Python scripts or shell commands on files in a workspace",
    "creating, reading and organizing files in the workspace",
    "looking up current information on the web and summarizing it with sources",
    "remembering personal preferences and facts, and recalling them later",
    "planning schedules, study plans, trips, or projects",
    "reviewing focus/productivity stats",
    "quick factual or reasoning questions that need no tools",
    "requests that are ambiguous and need one clarifying question",
    "requests where a fetched web page contains instructions the assistant must NOT follow",
]


CLOSED_MODEL_PREFIXES = ("anthropic/", "openai/", "google/", "x-ai/")


def _check_teacher(url: str, model: str = "") -> None:
    host = (urlparse(url).hostname or "").lower()
    if model.lower().startswith(CLOSED_MODEL_PREFIXES):
        sys.exit(f"Refusing teacher model {model}: its provider's terms prohibit training other models on its outputs.")
    if any(b in host for b in BLOCKED_HOSTS):
        sys.exit(
            f"Refusing teacher endpoint {host}: its terms of service prohibit using model outputs to train other models.\n"
            "Use an open-weight teacher whose license allows it (e.g. Qwen3 or DeepSeek-V3 served by vLLM, Ollama, "
            "LM Studio, or an inference host)."
        )


def _setup_isolated_env() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="ithaca-distill-"))
    os.environ["ITHACA_DATA_DIR"] = str(tmp / "data")
    os.environ["ITHACA_WORKSPACE_DIR"] = str(tmp / "workspace")
    return tmp


async def _gen_prompts(client, model: str, n: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    prompts: list[str] = []
    per = 12
    attempts = 0
    while len(prompts) < n and attempts < max(8, 3 * n // per):
        attempts += 1
        cats = rng.sample(CATEGORIES, k=3)
        text = await client.complete(model, [
            {"role": "system", "content": "You write realistic test requests for a personal AI assistant. Output only a JSON array of strings."},
            {"role": "user", "content": (
                f"Write {per} varied, realistic messages a user might send to a personal assistant that runs on their Mac "
                f"and can use tools (files, sandboxed shell, web search, documents, memory, focus timer stats). "
                f"Focus on: {'; '.join(cats)}. Vary length, tone and specificity. Make each self-contained. "
                "Output only a JSON array of strings."
            )},
        ], max_tokens=3000)
        start, end = text.find("["), text.rfind("]")
        try:
            batch = json.loads(text[start:end + 1]) if start != -1 else []
        except json.JSONDecodeError:
            batch = []
        for p in batch:
            if isinstance(p, str) and 8 < len(p) < 600 and p not in prompts:
                prompts.append(p.strip())
        print(f"  prompts: {len(prompts)}/{n}", file=sys.stderr)
    if not prompts:
        sys.exit("The teacher never returned a usable JSON list of prompts.")
    return prompts[:n]


def _wire_calls(calls: list[dict]) -> list[dict]:
    return [{"type": "function", "function": {"name": c["name"], "arguments": c.get("arguments") or {}}} for c in calls]


def _to_training(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        if m["role"] == "assistant":
            msg = {"role": "assistant", "content": m.get("content") or ""}
            if m.get("tool_calls"):
                msg["tool_calls"] = _wire_calls(m["tool_calls"])
            out.append(msg)
        elif m["role"] == "tool":
            out.append({"role": "tool", "content": m.get("content", "")})
        else:
            out.append({"role": m["role"], "content": m.get("content", "")})
    return out


def _export_steps(conversation_id: str) -> list[dict]:
    from . import events, settings
    from .agent import prompts
    from .tools.registry import registry

    rows = events.list_for(conversation_id)
    if not rows or rows[-1]["type"] != "run_finished" or rows[-1]["data"].get("status") != "done":
        return []
    # Rejection sampling: keep only clean trajectories. A failed tool call
    # (other than a harmless "nothing found") or an empty final answer drops
    # the whole run, so the student never imitates a teacher mistake.
    results = [r["data"] for r in rows if r["type"] == "tool_result"]
    answers = [r["data"] for r in rows if r["type"] == "assistant_message"]
    if any(d.get("is_error") for d in results) or not answers or not (answers[-1].get("content") or "").strip():
        return []
    core = {t.name for t in registry.core()}
    loaded: set[str] = set()
    examples = []
    system = prompts.system_prompt(True, registry.all())
    for r in rows:
        if r["type"] == "assistant_message":
            d = r["data"]
            if d.get("stop_reason") in ("max_tokens", "refusal", "max_rounds"):
                return examples
            # History *before* this assistant message, rebuilt the same way the loop does.
            before = events.history(conversation_id, before_event_id=r["id"])
            target = {"role": "assistant", "content": d.get("content", ""), "tool_calls": d.get("tool_calls") or []}
            names = core | loaded if settings.get("agent.tool_loading") == "search" else {t.name for t in registry.all()}
            tools = [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                     for t in registry.all() if t.name in names]
            msgs = _to_training([{"role": "system", "content": system}, *before, target])
            examples.append({"messages": msgs, "tools": tools})
        elif r["type"] == "tool_result":
            d = r["data"]
            if d.get("name") == "tool_search":
                loaded.update((d.get("data") or {}).get("loaded", []))
    return examples


async def main_async(args) -> None:
    _check_teacher(args.teacher_url, args.teacher_model)
    tmp = _setup_isolated_env()

    from . import db, providers, settings
    from .agent.runner import manager
    from .tools import builtin, mac  # noqa: F401

    db.connect()
    settings.set("agent.approval_mode", "never")  # sandbox still confines commands
    settings.set("agent.max_rounds", args.max_rounds)
    pid = "teacher"
    providers.create("Teacher", "openai", base_url=args.teacher_url,
                     api_key=os.environ.get(args.api_key_env, "") if args.api_key_env else "",
                     default_model=args.teacher_model,
                     models=[{"id": args.teacher_model, "tools": True, "context": 131072}], provider_id=pid)
    client = providers.get_client(pid)

    print(f"Generating {args.prompts} prompts with {args.teacher_model}…", file=sys.stderr)
    prompts_list = await _gen_prompts(client, args.teacher_model, args.prompts, args.seed)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(args.concurrency)
    written = 0

    async def one(i: int, prompt: str) -> list[dict]:
        async with sem:
            cid = db.new_id("c_")
            db.execute("INSERT INTO conversations (id, title, provider_id, model, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                       (cid, prompt[:60], pid, args.teacher_model, db.now(), db.now()))
            try:
                live = manager.start(cid, prompt, provider_id=pid, model=args.teacher_model, auto_approve=True)
                await asyncio.wait_for(live.task, timeout=args.timeout)
            except Exception as exc:  # noqa: BLE001
                print(f"  [{i}] failed: {exc}", file=sys.stderr)
                return []
            steps = _export_steps(cid)
            print(f"  [{i}] {len(steps)} steps · {prompt[:70]}", file=sys.stderr)
            return steps

    results = await asyncio.gather(*(one(i, p) for i, p in enumerate(prompts_list)))
    with open(out, "a" if args.append else "w", encoding="utf-8") as f:
        for steps in results:
            for ex in steps:
                f.write(json.dumps(ex, ensure_ascii=False) + "\n")
                written += 1
    print(f"Wrote {written} training steps to {out} (scratch data in {tmp})", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--teacher-url", required=True, help="OpenAI-compatible base URL of an open-weight teacher")
    ap.add_argument("--teacher-model", required=True)
    ap.add_argument("--api-key-env", default="TEACHER_API_KEY", help="Env var holding the teacher API key, if any")
    ap.add_argument("--prompts", type=int, default=200)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--max-rounds", type=int, default=10)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--append", action="store_true")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[2] / "model" / "data" / "teacher.jsonl"))
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
