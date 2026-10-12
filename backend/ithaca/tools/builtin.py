"""Built-in tools. Importing this module registers them."""

from __future__ import annotations

import fnmatch
import json
import re
import time
from pathlib import Path
from typing import Any

from .. import config, db, documents, memory, research, sandbox, web
from .registry import ToolContext, ToolResult, obj, registry, tool

# --------------------------------------------------------------------- helpers


def _ws_path(rel: str) -> Path:
    """Resolve a model-supplied path inside the workspace or raise."""
    root = config.WORKSPACE_DIR.resolve()
    rel = (rel or ".").strip()
    if rel.startswith("~"):
        raise ValueError("Paths are relative to the workspace; '~' is not available.")
    candidate = Path(rel)
    target = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"'{rel}' is outside the workspace.")
    return target


def _rel(p: Path) -> str:
    return str(p.relative_to(config.WORKSPACE_DIR.resolve())) or "."


def _skip(name: str) -> bool:
    return name.startswith("._") or name in (".DS_Store", ".tmp", "__pycache__", "node_modules", ".git")


# ---------------------------------------------------------------- tool search


@tool(
    "tool_search",
    "Find and load more tools. You start with only a few tools; call this with a short description of what you "
    "need (for example 'read and edit files', 'search the web', 'run python code', 'documents') and the matching "
    "tools become available on your next step.",
    obj({"query": {"type": "string", "description": "What you want to do."}}, ["query"]),
    core=True,
    group="core",
)
async def tool_search(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    found = registry.search(args["query"], limit=6, exclude=ctx.loaded)
    if not found:
        names = ", ".join(sorted({t.group for t in registry.all() if not t.core}))
        return ToolResult(f"No tools matched. Available tool groups: {names}. Try a different description.")
    for t in found:
        ctx.loaded.add(t.name)
    lines = [f"- {t.name}: {t.description.split('. ')[0]}." for t in found]
    return ToolResult("Loaded these tools (usable from your next step):\n" + "\n".join(lines),
                      data={"loaded": [t.name for t in found]})


# ----------------------------------------------------------------------- plan


def render_plan(plan: dict[str, Any]) -> str:
    steps, done = plan.get("steps", []), set(plan.get("done", []))
    nxt = next((i for i in range(1, len(steps) + 1) if i not in done), None)
    marks = []
    for i, s in enumerate(steps, 1):
        mark = "✓" if i in done else ("→" if i == nxt else "○")
        marks.append(f"{mark} {i}. {s}")
    return "\n".join(marks)


@tool(
    "update_plan",
    "For any request with more than one step: first call this with the full list of steps, then do them one at a "
    "time. After finishing a step, call it again with that step number added to `done`. Keep steps short and concrete.",
    obj({"steps": {"type": "array", "items": {"type": "string"}, "description": "All steps, in order."},
         "done": {"type": "array", "items": {"type": "integer"}, "description": "Numbers (1-based) of finished steps."}},
        ["steps"]),
    core=True,
    group="core",
)
async def update_plan(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    steps = [str(s).strip() for s in args.get("steps") or [] if str(s).strip()][:12]
    if not steps:
        return ToolResult("Give at least one step.", is_error=True)
    done = sorted({int(d) for d in args.get("done") or [] if str(d).lstrip("-").isdigit() and 1 <= int(d) <= len(steps)})
    ctx.services["plan"] = {"steps": steps, "done": done}
    remaining = len(steps) - len(done)
    tail = "All steps are done — give the user a short summary." if remaining == 0 else "Now do the step marked →."
    return ToolResult(f"Plan:\n{render_plan(ctx.services['plan'])}\n{tail}", data={"plan": ctx.services["plan"]})


# --------------------------------------------------------------------- memory


@tool(
    "memory_search",
    "Search the user's long-term memory for facts, preferences, and past decisions.",
    obj({"query": {"type": "string"}, "limit": {"type": "integer", "description": "Max results (default 5)."}}, ["query"]),
    core=True,
    group="memory",
)
async def memory_search(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    items = await memory.search(args["query"], k=int(args.get("limit") or 5))
    if not items:
        return ToolResult("No matching memories.")
    lines = [f"- ({m['id']}) {m['content']}" for m in items]
    text = "\n".join(lines)
    if any(m["tainted"] for m in items):
        return ToolResult(text, untrusted_source="memory saved during a web-tainted run")
    return ToolResult(text)


@tool(
    "memory_save",
    "Save a durable fact about the user or their work to long-term memory (preferences, names, decisions, "
    "ongoing projects). Write one self-contained sentence. Do not save secrets or passwords.",
    obj({"content": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}}, ["content"]),
    effects={"write"},
    core=True,
    group="memory",
)
async def memory_save(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    m = await memory.save(args["content"], args.get("tags") or [], tainted=ctx.tainted)
    note = " (marked untrusted because this run read external content)" if m["tainted"] else ""
    return ToolResult(f"Saved memory {m['id']}{note}.", data={"memory": m})


@tool(
    "memory_delete",
    "Delete a memory by id (ids are shown by memory_search).",
    obj({"id": {"type": "string"}}, ["id"]),
    effects={"write"},
    group="memory",
    keywords="forget remove",
)
async def memory_delete(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    ok = memory.delete(args["id"])
    return ToolResult("Deleted." if ok else "No memory with that id.", is_error=not ok)


# ---------------------------------------------------------------------- files


@tool(
    "list_files",
    "List files and folders in the workspace (a sandboxed folder for your files). Paths are relative to it.",
    obj({"path": {"type": "string", "description": "Folder, default '.'"},
         "recursive": {"type": "boolean"}}),
    group="files",
    keywords="ls directory folder workspace tree",
)
async def list_files(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    base = _ws_path(args.get("path") or ".")
    if not base.exists():
        return ToolResult(f"'{args.get('path')}' does not exist.", is_error=True)
    entries = []
    walker = base.rglob("*") if args.get("recursive") else base.iterdir()
    for p in sorted(walker):
        if any(_skip(part) for part in p.relative_to(base).parts):
            continue
        entries.append(f"{_rel(p)}  ({p.stat().st_size} B)" if p.is_file() else f"{_rel(p)}/")
        if len(entries) >= 400:
            entries.append("… (truncated)")
            break
    return ToolResult("\n".join(entries) or "(empty)")


@tool(
    "read_file",
    "Read a text file from the workspace. Use offset/limit (in lines) for large files.",
    obj({"path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, ["path"]),
    group="files",
    keywords="open cat view source code",
)
async def read_file(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    p = _ws_path(args["path"])
    if not p.is_file():
        return ToolResult(f"No file at '{args['path']}'.", is_error=True)
    text = p.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    offset = max(0, int(args.get("offset") or 0))
    limit = int(args.get("limit") or 800)
    chunk = lines[offset: offset + limit]
    numbered = "\n".join(f"{i + offset + 1:>5}  {line}" for i, line in enumerate(chunk))
    more = f"\n… {len(lines) - offset - limit} more lines" if len(lines) > offset + limit else ""
    return ToolResult(numbered + more if numbered else "(empty file)")


@tool(
    "write_file",
    "Create or overwrite a text file in the workspace.",
    obj({"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    effects={"write"},
    group="files",
    keywords="create save new file code script",
)
async def write_file(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    p = _ws_path(args["path"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(args["content"], encoding="utf-8")
    return ToolResult(f"Wrote {len(args['content'])} characters to {_rel(p)}.")


@tool(
    "edit_file",
    "Replace one exact, unique snippet of text in a workspace file with new text.",
    obj({"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}},
        ["path", "old_text", "new_text"]),
    effects={"write"},
    group="files",
    keywords="modify change patch replace code",
)
async def edit_file(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    p = _ws_path(args["path"])
    if not p.is_file():
        return ToolResult(f"No file at '{args['path']}'.", is_error=True)
    from ..textedit import apply_edit

    updated, msg = apply_edit(p.read_text(encoding="utf-8"), args["old_text"], args["new_text"])
    if updated is None:
        return ToolResult(msg, is_error=True)
    p.write_text(updated, encoding="utf-8")
    return ToolResult(f"{msg} {_rel(p)} now has the change.")


@tool(
    "search_files",
    "Search workspace files for a regular expression. Returns matching lines with paths and line numbers.",
    obj({"pattern": {"type": "string"}, "glob": {"type": "string", "description": "e.g. '*.py'"}}, ["pattern"]),
    group="files",
    keywords="grep find code text",
)
async def search_files(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    try:
        rx = re.compile(args["pattern"])
    except re.error as exc:
        return ToolResult(f"Invalid regex: {exc}", is_error=True)
    glob = args.get("glob") or "*"
    root = config.WORKSPACE_DIR.resolve()
    hits = []
    for p in root.rglob("*"):
        if not p.is_file() or any(_skip(part) for part in p.relative_to(root).parts):
            continue
        if not fnmatch.fnmatch(p.name, glob) or p.stat().st_size > 2_000_000:
            continue
        try:
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{_rel(p)}:{i}: {line.strip()[:200]}")
                    if len(hits) >= 200:
                        break
        except (UnicodeDecodeError, OSError):
            continue
        if len(hits) >= 200:
            break
    return ToolResult("\n".join(hits) or "No matches.")


# ---------------------------------------------------------------------- shell


@tool(
    "run_command",
    "Run a shell command (bash) in the sandboxed workspace, e.g. to run Python scripts, tests, or data "
    "processing. The sandbox cannot see the rest of the computer and has no internet unless network=true "
    "is approved by the user.",
    obj({"command": {"type": "string"},
         "timeout": {"type": "integer", "description": "Seconds, default 60, max 600."},
         "network": {"type": "boolean", "description": "Request internet access for this command."}},
        ["command"]),
    effects={"exec"},
    group="shell",
    keywords="bash terminal execute python script program test code run",
)
async def run_command(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    timeout = min(int(args.get("timeout") or 60), 600)
    result = await sandbox.run(args["command"], timeout=timeout, network=bool(args.get("network")) or None)
    body = result.output.strip() or "(no output)"
    return ToolResult(
        f"exit code {result.exit_code} [{result.backend}]\n{body}",
        is_error=result.exit_code != 0,
        data={"exit_code": result.exit_code, "backend": result.backend},
    )


# ------------------------------------------------------------------------ web


@tool(
    "web_search",
    "Search the web. Returns titles, URLs and snippets. Use web_fetch to read a page.",
    obj({"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"]),
    effects={"network"},
    group="web",
    untrusted_output=True,
    keywords="internet google lookup news latest current online",
)
async def web_search(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    try:
        results = await web.search(args["query"], limit=int(args.get("limit") or 8))
    except Exception as exc:  # noqa: BLE001
        return ToolResult(f"Search failed: {exc}", is_error=True)
    if not results:
        return ToolResult("No results.")
    text = "\n\n".join(f"[{i}] {r['title']}\n{r['url']}\n{r['snippet']}" for i, r in enumerate(results, 1))
    return ToolResult(text, untrusted_source=f"web search: {args['query'][:60]}", data={"results": results})


@tool(
    "web_fetch",
    "Fetch a web page and return its readable text.",
    obj({"url": {"type": "string"}, "max_chars": {"type": "integer"}}, ["url"]),
    effects={"network"},
    group="web",
    untrusted_output=True,
    keywords="internet read page url website download open link",
)
async def web_fetch(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    try:
        page = await web.fetch(args["url"], max_chars=min(int(args.get("max_chars") or 15000), 60000))
    except Exception as exc:  # noqa: BLE001
        return ToolResult(f"Fetch failed: {exc}", is_error=True)
    head = f"# {page['title']}\n{page['url']}\n\n" if page["title"] else f"{page['url']}\n\n"
    tail = "\n\n[truncated]" if page["truncated"] else ""
    return ToolResult(head + page["text"] + tail, untrusted_source=page["url"])


@tool(
    "deep_research",
    "Run multi-step web research on a question: plans queries, reads several sources, and writes a cited report "
    "saved as a document. Takes a minute or more. Use for questions needing several sources.",
    obj({"question": {"type": "string"}, "depth": {"type": "string", "enum": ["quick", "standard", "deep"]}},
        ["question"]),
    effects={"network", "write"},
    group="web",
    untrusted_output=True,
    keywords="research investigate report sources compare analysis",
)
async def deep_research(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    client, model = ctx.services["client"], ctx.services["model"]
    final: dict[str, Any] | None = None
    async for ev in research.run(client, model, args["question"], args.get("depth") or "quick"):
        if ev["type"] == "delta":
            continue
        if ev["type"] == "done":
            final = ev
        elif ev["type"] == "error":
            return ToolResult(ev["message"], is_error=True)
        else:
            await ctx.progress({"type": "research_progress", **ev})
    if not final:
        return ToolResult("Research did not finish.", is_error=True)
    return ToolResult(
        f"Report saved as document {final['document_id']}.\n\n{final['report']}",
        untrusted_source="deep research over web sources",
        data={"document_id": final["document_id"]},
    )


# ------------------------------------------------------------------ documents


@tool("list_documents", "List the user's documents (id, title, last update).", group="documents",
      keywords="notes docs writing library")
async def list_documents(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    rows = documents.list_all()
    if not rows:
        return ToolResult("No documents yet.")
    return ToolResult("\n".join(
        f"- {r['id']}: {r['title']} (updated {time.strftime('%Y-%m-%d', time.localtime(r['updated_at']))})" for r in rows[:200]
    ))


@tool("search_documents", "Full-text search across the user's documents.",
      obj({"query": {"type": "string"}}, ["query"]), group="documents", keywords="find notes docs")
async def search_documents(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    rows = documents.search(args["query"])
    return ToolResult("\n".join(f"- {r['id']}: {r['title']} — {r['snippet']}" for r in rows) or "No matches.")


@tool("read_document", "Read a document's full Markdown content by id.",
      obj({"id": {"type": "string"}}, ["id"]), group="documents", keywords="open view notes doc")
async def read_document(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    d = documents.get(args["id"])
    if not d:
        return ToolResult("Document not found.", is_error=True)
    return ToolResult(f"# {d['title']}\n\n{d['content']}")


@tool("create_document", "Create a new Markdown document for the user (drafts, notes, plans, essays).",
      obj({"title": {"type": "string"}, "content": {"type": "string"}}, ["title", "content"]),
      effects={"write"}, group="documents", keywords="write draft new note essay letter")
async def create_document(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    d = documents.create(args["title"], args["content"])
    return ToolResult(f"Created document {d['id']} “{d['title']}”.", data={"document_id": d["id"]})


@tool("edit_document",
      "Edit a document: replace one exact, unique snippet with new text. To rewrite everything, pass "
      "replace_all_with instead.",
      obj({"id": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"},
           "replace_all_with": {"type": "string"}}, ["id"]),
      effects={"write"}, group="documents", keywords="modify update change rewrite revise doc")
async def edit_document(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    if "replace_all_with" in args:
        d = documents.update(args["id"], content=args["replace_all_with"])
        return ToolResult("Document rewritten." if d else "Document not found.", is_error=d is None,
                          data={"document_id": args["id"]})
    if "old_text" not in args or "new_text" not in args:
        return ToolResult("Provide old_text and new_text, or replace_all_with.", is_error=True)
    ok, msg = documents.replace_text(args["id"], args["old_text"], args["new_text"])
    return ToolResult(msg, is_error=not ok, data={"document_id": args["id"]})


# ---------------------------------------------------------------------- focus


@tool("focus_stats", "Summarize the user's Pomodoro focus sessions over the last N days.",
      obj({"days": {"type": "integer"}}), group="focus", keywords="pomodoro timer productivity focus time")
async def focus_stats(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    days = int(args.get("days") or 7)
    since = time.time() - days * 86400
    rows = db.query(
        "SELECT date(started_at, 'unixepoch', 'localtime') AS day, SUM(actual_s) AS secs, COUNT(*) AS n "
        "FROM focus_sessions WHERE mode = 'focus' AND started_at >= ? GROUP BY day ORDER BY day", (since,))
    if not rows:
        return ToolResult(f"No focus sessions in the last {days} days.")
    total = sum(r["secs"] for r in rows)
    lines = [f"- {r['day']}: {r['secs'] // 60} min over {r['n']} sessions" for r in rows]
    return ToolResult(f"Total focus: {total // 60} min in {days} days.\n" + "\n".join(lines))


@tool("current_time", "Get the current local date and time.", group="general", keywords="date clock today now")
async def current_time(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    return ToolResult(time.strftime("%A %Y-%m-%d %H:%M:%S %Z"))


def describe_all() -> list[dict[str, Any]]:
    return [
        {"name": t.name, "description": t.description, "group": t.group, "core": t.core,
         "effects": sorted(t.effects), "untrusted_output": t.untrusted_output,
         "parameters": json.loads(json.dumps(t.parameters))}
        for t in registry.all()
    ]
