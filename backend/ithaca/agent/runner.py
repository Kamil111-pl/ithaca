"""The agent loop and the manager for in-flight runs.

A run executes as a background task, so closing the browser tab does not
kill it. Every state change is appended to the event log first and then
broadcast to any connected subscribers; a client that reconnects reloads the
conversation and resubscribes.
"""

from __future__ import annotations

import asyncio
import json
import re
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from .. import db, events, local_servers, procedures, providers, sandbox, security, settings
from ..providers.base import ProviderError
from ..tools.registry import ToolContext, ToolResult, registry, validate_args
from . import prompts

log = logging.getLogger("ithaca.agent")

UI_RESULT_CHARS = 6000
CHARS_PER_TOKEN = 3.6
# Runaway guards: small models sometimes emit dozens of parallel calls or
# repeat the same call forever. The runtime caps both instead of trusting them.
MAX_CALLS_PER_STEP = 8
MAX_IDENTICAL_CALLS = 3


@dataclass
class LiveRun:
    id: str
    conversation_id: str
    task: asyncio.Task | None = None
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    backlog: list[dict[str, Any]] = field(default_factory=list)
    approvals: dict[str, asyncio.Future] = field(default_factory=dict)
    done: bool = False

    async def publish(self, event: dict[str, Any]) -> None:
        event = {**event, "run_id": self.id}
        self.backlog.append(event)
        for q in list(self.subscribers):
            q.put_nowait(event)

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        q: asyncio.Queue = asyncio.Queue()
        for ev in self.backlog:
            q.put_nowait(ev)
        self.subscribers.append(q)
        try:
            while True:
                if self.done and q.empty():
                    return
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield {"type": "ping"}
                    continue
                yield ev
                if ev["type"] == "run_finished":
                    return
        finally:
            if q in self.subscribers:
                self.subscribers.remove(q)


class RunManager:
    def __init__(self) -> None:
        self.runs: dict[str, LiveRun] = {}

    def active_for(self, conversation_id: str) -> LiveRun | None:
        for r in self.runs.values():
            if r.conversation_id == conversation_id and not r.done:
                return r
        return None

    def start(self, conversation_id: str, user_text: str, *, provider_id: str, model: str,
              document_id: str | None = None, auto_approve: bool = False) -> LiveRun:
        if self.active_for(conversation_id):
            raise RuntimeError("A run is already in progress for this conversation.")
        run_id = db.new_id("r_")
        db.execute("INSERT INTO runs (id, conversation_id, status, created_at) VALUES (?, ?, 'running', ?)",
                   (run_id, conversation_id, db.now()))
        live = LiveRun(run_id, conversation_id)
        self.runs[run_id] = live
        live.task = asyncio.create_task(
            _execute(live, user_text, provider_id, model, document_id, auto_approve), name=f"run-{run_id}"
        )
        live.task.add_done_callback(lambda _t: self._forget_later(run_id))
        return live

    def _forget_later(self, run_id: str) -> None:
        asyncio.get_event_loop().call_later(120, lambda: self.runs.pop(run_id, None))

    def cancel(self, run_id: str) -> bool:
        live = self.runs.get(run_id)
        if not live or live.done or not live.task:
            return False
        live.task.cancel()
        return True

    def decide(self, approval_id: str, approve: bool) -> bool:
        row = db.query_one("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        if not row or row["status"] != "pending":
            return False
        live = self.runs.get(row["run_id"])
        fut = live.approvals.get(approval_id) if live else None
        if not fut or fut.done():
            return False
        fut.set_result(approve)
        return True


manager = RunManager()


def mark_interrupted_runs() -> None:
    """On startup, runs left 'running' by a previous process can never finish."""
    for r in db.query("SELECT id, conversation_id FROM runs WHERE status IN ('running', 'waiting_approval')"):
        db.execute("UPDATE runs SET status = 'cancelled', finished_at = ? WHERE id = ?", (db.now(), r["id"]))
        db.execute("UPDATE approvals SET status = 'denied', decided_at = ? WHERE run_id = ? AND status = 'pending'",
                   (db.now(), r["id"]))
        events.append(r["conversation_id"], r["id"], "run_finished", {"status": "cancelled", "reason": "server restarted"})


# ---------------------------------------------------------------------- run


def _estimate_tokens(messages: list[dict[str, Any]]) -> int:
    total = 0
    for m in messages:
        total += len(m.get("content") or "")
        for c in m.get("tool_calls") or []:
            total += len(json.dumps(c.get("arguments") or {}))
    return int(total / CHARS_PER_TOKEN) + 8 * len(messages)


def _context_window(provider_row: dict[str, Any], model: str) -> int:
    for m in provider_row.get("models", []):
        if m.get("id") == model and m.get("context"):
            return int(m["context"])
    return 1_000_000 if provider_row.get("kind") == "anthropic" else 32_768


async def _maybe_compact(live: LiveRun, client, model: str, window: int, kind: str) -> None:
    """Summarize older turns when a small-context model is near its limit.

    Everything before the second-to-last user message is folded into a
    `compaction` event; later turns are replayed verbatim.
    """
    if kind == "anthropic":
        return  # 1M-token context; replay stays verbatim
    if _estimate_tokens(events.history(live.conversation_id)) < window * 0.7:
        return
    user_events = [r for r in events.list_for(live.conversation_id) if r["type"] == "user_message"]
    if len(user_events) < 3:
        return
    boundary = user_events[-2]["id"]
    older = events.history(live.conversation_id, before_event_id=boundary)
    if not older:
        return
    await live.publish({"type": "status", "message": "Compacting earlier conversation…"})
    summary = await client.complete(model, prompts.summarize_prompt(older), max_tokens=1500)
    if summary.strip():
        events.append(live.conversation_id, live.id, "compaction",
                      {"summary": summary.strip(), "upto_event_id": boundary - 1})


def _tool_specs(tools_enabled: bool, loaded: set[str]):
    if not tools_enabled:
        return []
    if settings.get("agent.tool_loading") == "all":
        return [t.spec() for t in registry.all()]
    names = {t.name for t in registry.core()} | loaded
    return [t.spec() for t in registry.all() if t.name in names]


async def _execute(live: LiveRun, user_text: str, provider_id: str, model: str,
                   document_id: str | None, auto_approve: bool) -> None:
    conv = live.conversation_id
    status, error_message = "done", None
    usage_total = {"input_tokens": 0, "output_tokens": 0}
    try:
        provider_row = providers.get_row(provider_id, secret=False)
        if not provider_row:
            raise ProviderError("The selected provider no longer exists. Pick a model in the composer.")
        client = providers.get_client(provider_id)
        resolved = await providers.resolve_local_model(provider_id, model)
        if resolved != model:
            model = resolved
            db.execute("UPDATE conversations SET model = ? WHERE id = ?", (model, conv))
        model_meta = providers.model_info(provider_id, model)
        tools_enabled = model_meta.get("tools") is not False
        vision = model_meta.get("vision")
        if vision is None:
            vision = provider_row["kind"] == "anthropic"
        window = _context_window(provider_row, model)

        procedures.note_feedback(conv, user_text)  # a complaint counts against the last run's procedure
        context = await prompts.turn_context(user_text, document_id)
        user_eid = events.append(conv, live.id, "user_message", {"content": user_text, "context": context})
        await live.publish({"type": "user_message", "event_id": user_eid, "content": user_text})
        events.append(conv, live.id, "run_started", {"provider_id": provider_id, "model": model})
        await live.publish({"type": "run_started", "model": model, "provider_id": provider_id,
                            "tools_enabled": tools_enabled})
        if not tools_enabled:
            await live.publish({"type": "status", "message": "This model can't call tools, so Ithaca is in chat-only mode."})

        backend = await sandbox.resolve_backend()

        async def emit(payload: dict[str, Any]) -> None:
            await live.publish(payload)

        ctx = ToolContext(
            run_id=live.id, conversation_id=conv, loaded=events.loaded_tools(conv), document_id=document_id,
            services={"client": client, "model": model, "vision": bool(vision),
                      "coords": model_meta.get("coords") or "pixels"}, emit=emit,
        )
        max_rounds = int(settings.get("agent.max_rounds") or 24)
        seen_calls: dict[str, int] = {}
        nudged = False
        plan_nudges = 0
        if tools_enabled:
            await _preload_tools(live, ctx, user_text)
        if provider_row["kind"] == "openai" and hasattr(client, "temperature"):
            # Lower temperature makes small local models far more consistent at tool use.
            client.temperature = float(settings.get("agent.local_temperature"))
        effort = settings.get("agent.effort") or None

        for round_no in range(1, max_rounds + 1):
            await _maybe_compact(live, client, model, window, provider_row["kind"])
            system = prompts.system_prompt(tools_enabled, registry.all() if tools_enabled else None)
            messages = [{"role": "system", "content": system}, *events.history(conv)]
            specs = _tool_specs(tools_enabled, ctx.loaded)

            text_parts: list[str] = []
            thinking_parts: list[str] = []
            calls: list[dict[str, Any]] = []
            done_ev: dict[str, Any] = {}
            await live.publish({"type": "assistant_start", "round": round_no})
            attempt = 0
            restarted = False
            while True:
                try:
                    async for ev in client.stream(model, messages, specs, max_tokens=_max_tokens(provider_row, window),
                                                  effort=effort):
                        if ev["type"] == "text":
                            text_parts.append(ev["text"])
                            await live.publish({"type": "text", "text": ev["text"]})
                        elif ev["type"] == "thinking":
                            thinking_parts.append(ev["text"])
                            await live.publish({"type": "thinking", "text": ev["text"]})
                        elif ev["type"] == "tool_call":
                            calls.append(ev)
                        elif ev["type"] == "done":
                            done_ev = ev
                    break
                except ProviderError as exc:
                    attempt += 1
                    base_url = provider_row.get("base_url") or ""
                    if (not restarted and not text_parts and local_servers.port_of(base_url)
                            and local_servers.is_crash(str(exc))):
                        # The local model server crashed (usually out of GPU memory). Relaunch it and retry once.
                        restarted = True
                        await live.publish({"type": "status", "message": "The local model crashed — restarting it…"})
                        if await local_servers.restart(base_url, model):
                            await live.publish({"type": "status", "message": "Local model restarted. Retrying…"})
                            continue
                        raise ProviderError("The local model server crashed and could not be restarted. "
                                            "Close other heavy apps or stop the vision model, then try again.") from exc
                    if not exc.retryable or attempt > 2 or text_parts:
                        raise
                    await live.publish({"type": "status", "message": f"Retrying after error: {exc}"})
                    await asyncio.sleep(2 * attempt)

            usage = done_ev.get("usage") or {}
            for k in usage_total:
                usage_total[k] += int(usage.get(k) or 0)
            tool_calls = [{"id": c["id"], "name": c["name"], "arguments": c["arguments"]} for c in calls]
            events.append(conv, live.id, "assistant_message", {
                "content": "".join(text_parts),
                "thinking": "".join(thinking_parts)[:20000],
                "tool_calls": tool_calls,
                "raw": done_ev.get("raw") or {},
                "model": model,
                "provider_id": provider_id,
                "stop_reason": done_ev.get("stop_reason"),
                "usage": usage,
            })
            await live.publish({"type": "assistant_end", "stop_reason": done_ev.get("stop_reason"), "usage": usage})

            if not calls:
                plan = ctx.services.get("plan")
                remaining = [s for i, s in enumerate((plan or {}).get("steps", []), 1) if i not in set(plan.get("done", []))] if plan else []
                if remaining and plan_nudges < 2:
                    plan_nudges += 1
                    note = ("Your plan still has unfinished steps: " + "; ".join(remaining) + ". Continue with the next "
                            "one now. If a step is already done, mark it with update_plan; if it can't be done, say why.")
                    events.append(conv, live.id, "runtime_note", {"content": note})
                    await live.publish({"type": "status", "message": "Continuing the plan…"})
                    continue
                if not nudged and _should_nudge(conv, live.id, "".join(text_parts)):
                    # Small models often announce or even claim an action without
                    # calling any tool. Send them back once to actually do it.
                    nudged = True
                    suggest = [t.name for t in registry.search(user_text, limit=6)
                               if t.effects & (ACTION_EFFECTS | {"read", "network"}) and t.name in ctx.loaded | {c.name for c in registry.core()}][:3]
                    hint = f" The tools for this are: {', '.join(suggest)}." if suggest else ""
                    note = ("No tool was called in this task, so nothing has actually been done yet — and you do have "
                            "tools for files, the web, documents, memory and apps." + hint + " Call the right tool now "
                            "with the complete content. Never say you did (or can't do) something without trying the tool.")
                    events.append(conv, live.id, "runtime_note", {"content": note})
                    await live.publish({"type": "status", "message": "Reminding the model to use its tools…"})
                    continue
                break
            step_keys: set[str] = set()
            for i, call in enumerate(calls):
                key = f"{call['name']}:{json.dumps(call.get('arguments') or {}, sort_keys=True)}"
                if i >= MAX_CALLS_PER_STEP:
                    reason = f"Skipped: at most {MAX_CALLS_PER_STEP} tool calls run per step."
                elif key in step_keys:
                    reason = "Skipped: duplicate of another call in this step."
                elif seen_calls.get(key, 0) >= MAX_IDENTICAL_CALLS:
                    reason = "Skipped: this exact call already ran several times. Use the earlier result or try something different."
                else:
                    reason = None
                step_keys.add(key)
                if reason:
                    await _skip_tool(live, call, reason)
                    continue
                seen_calls[key] = seen_calls.get(key, 0) + 1
                await _run_tool(live, ctx, call, backend, auto_approve)
        else:
            note = f"I stopped after {max_rounds} steps. Say “continue” if you want me to keep going."
            events.append(conv, live.id, "assistant_message", {"content": note, "tool_calls": [], "model": model,
                                                                "provider_id": provider_id, "stop_reason": "max_rounds"})
            await live.publish({"type": "assistant_start", "round": max_rounds + 1})
            await live.publish({"type": "text", "text": note})
            await live.publish({"type": "assistant_end", "stop_reason": "max_rounds"})

        db.execute("UPDATE runs SET tainted = ?, taint_sources = ? WHERE id = ?",
                   (int(ctx.tainted), db.dumps(ctx.taint_sources), live.id))
    except asyncio.CancelledError:
        status = "cancelled"
    except ProviderError as exc:
        status, error_message = "error", str(exc)
    except Exception as exc:  # noqa: BLE001
        log.exception("run %s failed", live.id)
        status, error_message = "error", f"{exc.__class__.__name__}: {exc}"
    finally:
        for fut in live.approvals.values():
            if not fut.done():
                fut.cancel()
        db.execute("UPDATE approvals SET status = 'denied', decided_at = ? WHERE run_id = ? AND status = 'pending'",
                   (db.now(), live.id))
        db.execute("UPDATE runs SET status = ?, usage = ?, finished_at = ? WHERE id = ?",
                   (status, db.dumps(usage_total), db.now(), live.id))
        payload = {"status": status, "usage": usage_total}
        if error_message:
            payload["error"] = error_message
            events.append(conv, live.id, "error", {"message": error_message})
        events.append(conv, live.id, "run_finished", payload)
        if status == "done":
            try:
                await procedures.record(conv, live.id, user_text)
            except Exception:  # noqa: BLE001 - learning is best-effort
                log.exception("could not record procedure for run %s", live.id)
        live.done = True
        await live.publish({"type": "run_finished", **payload})


def _max_tokens(provider_row: dict[str, Any], window: int) -> int:
    if provider_row.get("kind") == "anthropic":
        return 64_000
    return max(1024, min(8192, window // 4))


async def _preload_tools(live: LiveRun, ctx: ToolContext, user_text: str) -> None:
    """Load the tools that best match the request up front.

    Saves small models the tool_search round trip, which they often skip or
    fumble. Recorded as an event so replays offer the same tools.
    """
    if settings.get("agent.tool_loading") != "search":
        return
    limit = int(settings.get("agent.preload_tools") if settings.get("agent.preload_tools") is not None else 6)
    if limit <= 0:
        return
    found = [t.name for t in registry.search(user_text, limit=limit, exclude=ctx.loaded)]
    if found:
        ctx.loaded.update(found)
        events.append(live.conversation_id, live.id, "tools_loaded", {"loaded": found})


ACTION_EFFECTS = {"host", "write", "exec", "external", "computer"}
PASSIVE_TOOLS = {"tool_search", "memory_search", "current_time"}


CLAIM_RE = re.compile(
    r"\b(i(?:'ve| have)\s+(?:now\s+|just\s+|successfully\s+)?(?:created|saved|written|wrote|added|opened|made|updated|"
    r"edited|run|ran|stored|remembered|set up|scheduled|sent|moved|deleted|marked)|i (?:created|saved|wrote|added|opened|"
    r"made|updated|edited|ran|stored|noted)|i(?:'ll| will) remember|i remember that|i've noted|has been (?:created|saved|added|updated))\b",
    re.I,
)


# "I'll update the file… here's the new content" — narrating instead of acting.
INTENT_RE = re.compile(r"\bi(?:'ll| will| am going to|'m going to) (?:now )?(?:update|create|write|save|add|edit|open|run|"
                       r"make|change|remember|store|fetch|read|search|mark)\b", re.I)
# Denying abilities it has: "I don't have access to files / the web".
DENIAL_RE = re.compile(r"\b(?:i (?:don't|do not|can't|cannot|am unable to|'m unable to|am not able to) (?:have )?"
                       r"(?:access|browse|read|open|run|execute|fetch|save|remember|create|reach)|no (?:web|file|internet) "
                       r"(?:content|access)|don't have (?:access|the ability)|can't access)", re.I)


def _claims_action(text: str) -> bool:
    return bool(CLAIM_RE.search(text or "") or INTENT_RE.search(text or "") or DENIAL_RE.search(text or ""))


def _should_nudge(conversation_id: str, run_id: str, final_text: str = "") -> bool:
    """True when the run did no real work but either the model loaded action
    tools itself (tool_search) or its reply claims, announces or denies an
    action. Tools the runtime preloaded don't count, so plain Q&A isn't nudged."""
    loaded: set[str] = set()
    used: set[str] = set()
    for r in events.list_for(conversation_id):
        if r["run_id"] != run_id:
            continue
        d = r["data"]
        if r["type"] == "tool_result" and d.get("name") == "tool_search":
            loaded.update((d.get("data") or {}).get("loaded", []))
        elif r["type"] == "assistant_message":
            used.update(c["name"] for c in d.get("tool_calls") or [])
    if used - PASSIVE_TOOLS:
        return False
    if _claims_action(final_text):
        return True
    return any((t := registry.get(n)) is not None and t.effects & ACTION_EFFECTS for n in loaded)


async def _skip_tool(live: LiveRun, call: dict[str, Any], reason: str) -> None:
    """Record a result for a call the runtime refused, keeping the transcript valid."""
    events.append(live.conversation_id, live.id, "tool_result", {
        "tool_call_id": call["id"], "name": call["name"], "content": reason, "display": reason,
        "is_error": True, "untrusted_source": None, "data": None, "duration_ms": 0,
    })
    await live.publish({"type": "tool_start", "tool_call_id": call["id"], "name": call["name"],
                        "arguments": call.get("arguments") or {}})
    await live.publish({"type": "tool_result", "tool_call_id": call["id"], "name": call["name"], "display": reason,
                        "is_error": True, "untrusted": False, "data": None, "duration_ms": 0})


async def _run_tool(live: LiveRun, ctx: ToolContext, call: dict[str, Any], backend: str, auto_approve: bool) -> None:
    conv = live.conversation_id
    name, args, call_id = call["name"], call.get("arguments") or {}, call["id"]
    tool = registry.get(name)
    started = time.monotonic()
    await live.publish({"type": "tool_start", "tool_call_id": call_id, "name": name, "arguments": args})

    result: ToolResult
    available = tool is not None and (tool.core or name in ctx.loaded or settings.get("agent.tool_loading") == "all")
    if tool is not None and not available and not call.get("parse_error"):
        # The model named a real tool it hadn't loaded: its intent is clear, so load
        # it rather than bouncing it back to tool_search (approvals still apply below).
        ctx.loaded.add(name)
        events.append(conv, live.id, "tools_loaded", {"loaded": [name]})
    if call.get("parse_error"):
        result = ToolResult(call["parse_error"], is_error=True)
    elif tool is None:
        result = ToolResult(f"There is no tool named '{name}'. Use tool_search to find tools.", is_error=True)
    elif (problem := validate_args(tool.parameters, args)) is not None:
        result = ToolResult(problem, is_error=True)
    else:
        decision = security.decide(tool, args, ctx, backend)
        approved = decision.allow or auto_approve
        if not decision.allow and not auto_approve:
            approved = await _ask(live, call_id, name, args, decision.reason)
        if approved:
            ctx.services.setdefault("approved_effects", set()).update(tool.effects & set(security.ONCE_PER_RUN))
        if not approved:
            result = ToolResult("The user declined this action. Do not retry it; ask what they would like instead.",
                                is_error=True)
        else:
            try:
                result = await asyncio.wait_for(tool.handler(args, ctx), timeout=900)
            except asyncio.TimeoutError:
                result = ToolResult("The tool timed out.", is_error=True)
            except (ValueError, KeyError, OSError) as exc:
                result = ToolResult(str(exc), is_error=True)
            except Exception as exc:  # noqa: BLE001
                log.exception("tool %s failed", name)
                result = ToolResult(f"{exc.__class__.__name__}: {exc}", is_error=True)

    untrusted = result.untrusted_source or (tool.untrusted_output and not result.is_error if tool else None)
    content = result.content
    if result.is_error:
        content += "\n\n(The call failed. Read the error, fix the arguments or pick another tool, and try again.)"
    if untrusted:
        source = result.untrusted_source or name
        security.mark_tainted(ctx, source)
        content = security.wrap_untrusted(source, content)

    plan = ctx.services.get("plan")
    if plan and name != "update_plan":
        # Keep the plan in view after every action; small models lose track otherwise.
        from ..tools.builtin import render_plan

        content += f"\n\n[Plan status]\n{render_plan(plan)}\n(Mark finished steps with update_plan.)"

    duration_ms = int((time.monotonic() - started) * 1000)
    display = result.content if len(result.content) <= UI_RESULT_CHARS else result.content[:UI_RESULT_CHARS] + "\n…"
    events.append(conv, live.id, "tool_result", {
        "tool_call_id": call_id, "name": name, "content": content, "display": display,
        "is_error": result.is_error, "untrusted_source": result.untrusted_source if untrusted else None,
        "data": result.data, "duration_ms": duration_ms, "images": result.images or [],
    })
    await live.publish({
        "type": "tool_result", "tool_call_id": call_id, "name": name, "display": display, "is_error": result.is_error,
        "untrusted": bool(untrusted), "data": result.data, "duration_ms": duration_ms, "tainted": ctx.tainted,
        "image": _preview(result.images),
    })


def _preview(images: list[dict[str, str]] | None) -> str | None:
    if not images:
        return None
    img = images[-1]
    return f"data:{img['media_type']};base64,{img['data']}"


async def _ask(live: LiveRun, call_id: str, name: str, args: dict[str, Any], reason: str) -> bool:
    approval_id = db.new_id("a_")
    db.execute(
        "INSERT INTO approvals (id, run_id, tool_call_id, tool, args, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (approval_id, live.id, call_id, name, db.dumps(args), reason, db.now()),
    )
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    live.approvals[approval_id] = fut
    db.execute("UPDATE runs SET status = 'waiting_approval' WHERE id = ?", (live.id,))
    payload = {"approval_id": approval_id, "tool_call_id": call_id, "name": name, "arguments": args, "reason": reason}
    events.append(live.conversation_id, live.id, "approval_requested", payload)
    await live.publish({"type": "approval_requested", **payload})
    try:
        approved = bool(await fut)
    finally:
        live.approvals.pop(approval_id, None)
    db.execute("UPDATE approvals SET status = ?, decided_at = ? WHERE id = ?",
               ("approved" if approved else "denied", db.now(), approval_id))
    db.execute("UPDATE runs SET status = 'running' WHERE id = ?", (live.id,))
    events.append(live.conversation_id, live.id, "approval_decided", {"approval_id": approval_id, "approved": approved})
    await live.publish({"type": "approval_decided", "approval_id": approval_id, "approved": approved})
    return approved
