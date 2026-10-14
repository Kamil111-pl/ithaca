"""HTTP API and static web app."""

from __future__ import annotations

import asyncio
import json
import secrets
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import auth, config, db, documents, events, memory, providers, research, sandbox, settings, voice
from .agent.runner import manager as runs
from .agent.runner import mark_interrupted_runs
from .mcp_client import manager as mcp
from .providers.base import ProviderError
from .tools import blender, browser, builtin, computer, mac  # noqa: F401 - registers tools
from .tools.builtin import describe_all

log = logging.getLogger("ithaca")
logging.basicConfig(level=os.environ.get("ITHACA_LOG", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.conn()  # opens and migrates on first use
    providers.bootstrap_defaults()
    mark_interrupted_runs()
    await mcp.start_all()
    log.info("Ithaca ready on http://%s:%s (data: %s)", config.HOST, config.PORT, config.DATA_DIR)
    yield
    await mcp.stop_all()
    await browser.manager.close()


app = FastAPI(title="Ithaca", lifespan=lifespan)

_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "[::1]"} | {
    h.strip() for h in os.environ.get("ITHACA_ALLOWED_HOSTS", "").split(",") if h.strip()
}
_OPEN_PATHS = {"/api/health", "/api/auth/status", "/api/auth/setup", "/api/auth/login", "/api/mcp/oauth/callback"}


def _hostname(host_header: str) -> str:
    if host_header.startswith("["):  # IPv6 literal, e.g. [::1]:7100
        return host_header.split("]")[0] + "]"
    return host_header.rsplit(":", 1)[0]


@app.middleware("http")
async def guard(request: Request, call_next):
    host = _hostname(request.headers.get("host") or "")
    if host not in _ALLOWED_HOSTS:
        # Blocks DNS-rebinding attacks against the local server.
        return JSONResponse({"detail": f"Host '{host}' is not allowed. Set ITHACA_ALLOWED_HOSTS."}, status_code=421)
    path = request.url.path
    if path.startswith("/api/"):
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-ithaca") != "1":
            # Custom header => browsers must preflight cross-site requests, which we never allow.
            return JSONResponse({"detail": "Missing X-Ithaca header."}, status_code=403)
        voice_ok = (path.startswith(("/api/voice/", "/api/runs/"))
                    and secrets.compare_digest(request.headers.get("x-ithaca-voice-token", ""), voice.voice_token()))
        if path not in _OPEN_PATHS and not voice_ok and not auth.valid(request.cookies.get(auth.COOKIE)):
            return JSONResponse({"detail": "Not signed in."}, status_code=401)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    return response


def _sse(gen: AsyncIterator[dict[str, Any]]) -> StreamingResponse:
    async def body():
        async for ev in gen:
            if ev.get("type") == "ping":
                yield ": ping\n\n"
            else:
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"

    return StreamingResponse(body(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _404(what: str = "Not found") -> HTTPException:
    return HTTPException(404, what)


# ------------------------------------------------------------------ auth


class PasswordBody(BaseModel):
    password: str


class ChangePasswordBody(BaseModel):
    current: str
    new: str


def _login_response(token: str) -> JSONResponse:
    resp = JSONResponse({"ok": True})
    resp.set_cookie(auth.COOKIE, token, httponly=True, samesite="strict", max_age=config.SESSION_TTL_SECONDS,
                    secure=os.environ.get("ITHACA_SECURE_COOKIES") == "1")
    return resp


@app.get("/api/health")
async def health():
    return {"ok": True, "version": "1.0.0"}


@app.get("/api/auth/status")
async def auth_status(request: Request):
    return {"configured": auth.is_configured(), "signed_in": auth.valid(request.cookies.get(auth.COOKIE))}


@app.post("/api/auth/setup")
async def auth_setup(body: PasswordBody):
    if auth.is_configured():
        raise HTTPException(409, "A password is already set.")
    try:
        auth.set_password(body.password)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _login_response(auth.new_session())


@app.post("/api/auth/login")
async def auth_login(body: PasswordBody):
    if not auth.check_password(body.password):
        await asyncio.sleep(0.5)
        raise HTTPException(401, "Wrong password.")
    return _login_response(auth.new_session())


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    auth.end_session(request.cookies.get(auth.COOKIE))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE)
    return resp


@app.post("/api/auth/password")
async def auth_change(body: ChangePasswordBody):
    if not auth.check_password(body.current):
        raise HTTPException(401, "Current password is wrong.")
    try:
        auth.set_password(body.new)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _login_response(auth.new_session())


# ------------------------------------------------------------- providers


class ProviderBody(BaseModel):
    name: str
    kind: str
    base_url: str = ""
    api_key: str = ""
    default_model: str = ""


class ProviderPatch(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    default_model: str | None = None
    models: list[dict[str, Any]] | None = None


@app.get("/api/providers")
async def providers_list():
    return providers.list_rows()


@app.post("/api/providers")
async def providers_create(body: ProviderBody):
    try:
        row = providers.create(body.name, body.kind, body.base_url, body.api_key, body.default_model)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        row = await providers.refresh_models(row["id"])
    except ProviderError as exc:
        row = {**row, "warning": f"Saved, but listing models failed: {exc}"}
    return row


@app.patch("/api/providers/{pid}")
async def providers_patch(pid: str, body: ProviderPatch):
    if not providers.get_row(pid):
        raise _404()
    return providers.update(pid, body.model_dump(exclude_none=True))


@app.delete("/api/providers/{pid}")
async def providers_delete(pid: str):
    providers.delete(pid)
    return {"ok": True}


@app.post("/api/providers/{pid}/refresh")
async def providers_refresh(pid: str):
    try:
        return await providers.refresh_models(pid)
    except ProviderError as exc:
        raise HTTPException(502, str(exc)) from exc


class ProbeBody(BaseModel):
    model: str


@app.post("/api/providers/{pid}/probe")
async def providers_probe(pid: str, body: ProbeBody):
    if not providers.get_row(pid):
        raise _404()
    return await providers.probe(pid, body.model)


@app.get("/api/models")
async def models_flat():
    # Keep the bundled local server's list current (it changes when you serve other weights).
    for pid in providers.LOCAL_IDS:
        if providers.get_row(pid):
            try:
                await asyncio.wait_for(providers.refresh_models(pid), timeout=2)
            except (ProviderError, asyncio.TimeoutError):
                pass
    out = []
    for p in providers.list_rows():
        for m in p["models"]:
            out.append({"provider_id": p["id"], "provider": p["name"], "kind": p["kind"], "model": m["id"],
                        "tools": m.get("tools"), "context": m.get("context"),
                        "vision": m.get("vision") if m.get("vision") is not None else p["kind"] == "anthropic",
                        "default": m["id"] == p["default_model"]})
    return out


# ---------------------------------------------------------- conversations


class ConversationBody(BaseModel):
    title: str | None = None
    provider_id: str | None = None
    model: str | None = None
    document_id: str | None = None


class ConversationPatch(BaseModel):
    title: str | None = None
    pinned: bool | None = None
    provider_id: str | None = None
    model: str | None = None
    document_id: str | None = Field(default=None)
    clear_document: bool = False


def _default_model() -> tuple[str | None, str | None]:
    pid, model = settings.get("chat.default_provider_id"), settings.get("chat.default_model")
    if pid and model and providers.get_row(pid):
        return pid, model
    rows = providers.list_rows()
    for p in rows:
        if p["kind"] == "anthropic" and p["has_api_key"]:
            return p["id"], p["default_model"] or providers.CLAUDE_DEFAULT
    for p in rows:
        if p["default_model"]:
            return p["id"], p["default_model"]
    return None, None


@app.get("/api/conversations")
async def conversations_list(q: str = ""):
    if q:
        like = f"%{q}%"
        return db.query(
            "SELECT DISTINCT c.* FROM conversations c LEFT JOIN events e ON e.conversation_id = c.id "
            "AND e.type IN ('user_message','assistant_message') WHERE c.title LIKE ? OR e.data LIKE ? "
            "ORDER BY c.pinned DESC, c.updated_at DESC LIMIT 200", (like, like))
    return db.query("SELECT * FROM conversations ORDER BY pinned DESC, updated_at DESC LIMIT 500")


@app.post("/api/conversations")
async def conversations_create(body: ConversationBody):
    pid, model = body.provider_id, body.model
    if not (pid and model):
        pid, model = _default_model()
    cid = db.new_id("c_")
    t = db.now()
    db.execute(
        "INSERT INTO conversations (id, title, provider_id, model, document_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (cid, body.title or "New chat", pid, model, body.document_id, t, t),
    )
    return db.query_one("SELECT * FROM conversations WHERE id = ?", (cid,))


def _ui_items(cid: str) -> list[dict[str, Any]]:
    approvals = {a["id"]: a for a in db.query(
        "SELECT a.* FROM approvals a JOIN runs r ON r.id = a.run_id WHERE r.conversation_id = ?", (cid,))}
    items = []
    for e in events.list_for(cid):
        d, t = e["data"], e["type"]
        base = {"event_id": e["id"], "run_id": e["run_id"], "created_at": e["created_at"]}
        if t == "user_message":
            items.append({**base, "kind": "user", "content": d.get("content", "")})
        elif t == "assistant_message":
            items.append({**base, "kind": "assistant", "content": d.get("content", ""), "thinking": d.get("thinking", ""),
                          "tool_calls": d.get("tool_calls") or [], "model": d.get("model"),
                          "stop_reason": d.get("stop_reason"), "usage": d.get("usage") or {}})
        elif t == "tool_result":
            items.append({**base, "kind": "tool_result", "tool_call_id": d["tool_call_id"], "name": d.get("name"),
                          "display": d.get("display", ""), "is_error": d.get("is_error", False),
                          "untrusted": bool(d.get("untrusted_source")), "data": d.get("data"),
                          "duration_ms": d.get("duration_ms"),
                          "image": (f"data:{d['images'][-1]['media_type']};base64,{d['images'][-1]['data']}"
                                    if d.get("images") else None)})
        elif t == "approval_requested":
            a = approvals.get(d["approval_id"], {})
            items.append({**base, "kind": "approval", **d, "status": a.get("status", "denied")})
        elif t == "error":
            items.append({**base, "kind": "error", "message": d.get("message", "")})
        elif t == "compaction":
            items.append({**base, "kind": "notice", "text": "Earlier messages were summarized to fit the model's context."})
    return items


@app.get("/api/conversations/{cid}")
async def conversations_get(cid: str):
    conv = db.query_one("SELECT * FROM conversations WHERE id = ?", (cid,))
    if not conv:
        raise _404()
    live = runs.active_for(cid)
    run = db.query_one("SELECT * FROM runs WHERE conversation_id = ? ORDER BY created_at DESC LIMIT 1", (cid,))
    return {**conv, "items": _ui_items(cid), "active_run_id": live.id if live else None,
            "tainted": bool(run and run["tainted"])}


@app.patch("/api/conversations/{cid}")
async def conversations_patch(cid: str, body: ConversationPatch):
    if not db.query_one("SELECT id FROM conversations WHERE id = ?", (cid,)):
        raise _404()
    fields = body.model_dump(exclude_none=True, exclude={"clear_document"})
    if "pinned" in fields:
        fields["pinned"] = int(fields["pinned"])
    if body.clear_document:
        fields["document_id"] = None
    if fields:
        sets = ", ".join(f"{k} = ?" for k in fields)
        db.execute(f"UPDATE conversations SET {sets} WHERE id = ?", (*fields.values(), cid))
    return db.query_one("SELECT * FROM conversations WHERE id = ?", (cid,))


@app.delete("/api/conversations/{cid}")
async def conversations_delete(cid: str):
    live = runs.active_for(cid)
    if live:
        runs.cancel(live.id)
    with db.transaction() as c:
        c.execute("DELETE FROM approvals WHERE run_id IN (SELECT id FROM runs WHERE conversation_id = ?)", (cid,))
        c.execute("DELETE FROM runs WHERE conversation_id = ?", (cid,))
        c.execute("DELETE FROM events WHERE conversation_id = ?", (cid,))
        c.execute("DELETE FROM conversations WHERE id = ?", (cid,))
    return {"ok": True}


class MessageBody(BaseModel):
    content: str
    provider_id: str | None = None
    model: str | None = None


@app.post("/api/conversations/{cid}/messages")
async def conversations_send(cid: str, body: MessageBody):
    conv = db.query_one("SELECT * FROM conversations WHERE id = ?", (cid,))
    if not conv:
        raise _404()
    content = body.content.strip()
    if not content:
        raise HTTPException(400, "Message is empty.")
    pid = body.provider_id or conv["provider_id"]
    model = body.model or conv["model"]
    if not (pid and model):
        pid, model = _default_model()
    if not (pid and model):
        raise HTTPException(400, "No model is configured. Add a provider in Settings.")
    updates: dict[str, Any] = {"provider_id": pid, "model": model}
    if conv["title"] == "New chat":
        first_line = content.splitlines()[0]
        updates["title"] = first_line[:60] + ("…" if len(first_line) > 60 else "")
    db.execute(f"UPDATE conversations SET {', '.join(f'{k} = ?' for k in updates)} WHERE id = ?", (*updates.values(), cid))
    try:
        live = runs.start(cid, content, provider_id=pid, model=model, document_id=conv["document_id"])
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _sse(live.subscribe())


@app.get("/api/runs/{run_id}/stream")
async def runs_stream(run_id: str):
    live = runs.runs.get(run_id)
    if not live:
        raise _404("Run is not active.")
    return _sse(live.subscribe())


@app.post("/api/runs/{run_id}/cancel")
async def runs_cancel(run_id: str):
    return {"ok": runs.cancel(run_id)}


class ApprovalBody(BaseModel):
    approve: bool


@app.post("/api/approvals/{approval_id}")
async def approvals_decide(approval_id: str, body: ApprovalBody):
    if not runs.decide(approval_id, body.approve):
        raise HTTPException(409, "This approval is no longer pending.")
    return {"ok": True}


# -------------------------------------------------------------- documents


class DocumentBody(BaseModel):
    title: str = "Untitled"
    content: str = ""


class DocumentPatch(BaseModel):
    title: str | None = None
    content: str | None = None


@app.get("/api/documents")
async def documents_list(q: str = ""):
    return documents.search(q) if q else documents.list_all()


@app.post("/api/documents")
async def documents_create(body: DocumentBody):
    return documents.create(body.title, body.content)


@app.get("/api/documents/{did}")
async def documents_get(did: str):
    d = documents.get(did)
    if not d:
        raise _404()
    return d


@app.patch("/api/documents/{did}")
async def documents_patch(did: str, body: DocumentPatch):
    d = documents.update(did, title=body.title, content=body.content)
    if not d:
        raise _404()
    return d


@app.delete("/api/documents/{did}")
async def documents_delete(did: str):
    documents.delete(did)
    return {"ok": True}


class AssistBody(BaseModel):
    instruction: str
    selection: str | None = None
    provider_id: str | None = None
    model: str | None = None


def _resolve_model(pid: str | None, model: str | None) -> tuple[str, str]:
    if not (pid and model):
        pid, model = _default_model()
    if not (pid and model):
        raise HTTPException(400, "No model is configured. Add a provider in Settings.")
    return pid, model


@app.post("/api/documents/{did}/assist")
async def documents_assist(did: str, body: AssistBody):
    d = documents.get(did)
    if not d:
        raise _404()
    pid, model = _resolve_model(body.provider_id, body.model)
    client = providers.get_client(pid)
    target = body.selection or d["content"]
    scope = "the selected passage" if body.selection else "the whole document"
    messages = [
        {"role": "system", "content": "You are an expert editor. Return only the revised text, in Markdown, with no preamble or commentary."},
        {"role": "user", "content": (
            f"Document title: {d['title']}\n\n" + (f"Full document for context:\n<document>\n{d['content'][:30000]}\n</document>\n\n" if body.selection else "")
            + f"Instruction: {body.instruction}\n\nRevise {scope}:\n<text>\n{target}\n</text>"
        )},
    ]

    async def gen():
        try:
            async for ev in client.stream(model, messages, [], max_tokens=16000, effort="low"):
                if ev["type"] == "text":
                    yield {"type": "text", "text": ev["text"]}
            yield {"type": "done"}
        except ProviderError as exc:
            yield {"type": "error", "message": str(exc)}

    return _sse(gen())


# ----------------------------------------------------------------- memory


class MemoryBody(BaseModel):
    content: str
    tags: list[str] = []


@app.get("/api/procedures")
async def list_procedures() -> list[dict[str, Any]]:
    from . import procedures

    return procedures.list_all()


@app.delete("/api/procedures/{pid}")
async def delete_procedure(pid: str) -> dict[str, bool]:
    from . import procedures

    if not procedures.delete(pid):
        raise HTTPException(404, "No such procedure.")
    return {"ok": True}


@app.get("/api/memories")
async def memories_list(q: str = ""):
    return await memory.search(q, k=50) if q else memory.list_all()


@app.post("/api/memories")
async def memories_create(body: MemoryBody):
    try:
        return await memory.save(body.content, body.tags)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.patch("/api/memories/{mid}")
async def memories_patch(mid: str, body: MemoryBody):
    m = await memory.update(mid, body.content)
    if not m:
        raise _404()
    return m


@app.delete("/api/memories/{mid}")
async def memories_delete(mid: str):
    memory.delete(mid)
    return {"ok": True}


# --------------------------------------------------------------- research


class ResearchBody(BaseModel):
    question: str
    depth: str = "standard"
    provider_id: str | None = None
    model: str | None = None


@app.post("/api/research")
async def research_run(body: ResearchBody):
    pid, model = _resolve_model(body.provider_id, body.model)
    client = providers.get_client(pid)

    async def gen():
        try:
            async for ev in research.run(client, model, body.question, body.depth):
                yield ev
        except ProviderError as exc:
            yield {"type": "error", "message": str(exc)}

    return _sse(gen())


# ------------------------------------------------------------------ focus


class FocusBody(BaseModel):
    mode: str
    label: str = ""
    planned_s: int
    actual_s: int
    completed: bool
    started_at: float
    ended_at: float


@app.post("/api/focus/sessions")
async def focus_log(body: FocusBody):
    if body.mode not in ("focus", "short_break", "long_break"):
        raise HTTPException(400, "Unknown mode.")
    fid = db.new_id("f_")
    db.execute(
        "INSERT INTO focus_sessions (id, mode, label, planned_s, actual_s, completed, started_at, ended_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (fid, body.mode, body.label, body.planned_s, max(0, body.actual_s), int(body.completed), body.started_at, body.ended_at),
    )
    return {"id": fid}


@app.get("/api/focus/stats")
async def focus_stats(days: int = 14):
    since = time.time() - days * 86400
    per_day = db.query(
        "SELECT date(started_at, 'unixepoch', 'localtime') AS day, SUM(actual_s) AS seconds, "
        "SUM(completed) AS completed FROM focus_sessions WHERE mode = 'focus' AND started_at >= ? GROUP BY day ORDER BY day",
        (since,))
    today = time.strftime("%Y-%m-%d")
    by_day = {r["day"]: r for r in per_day}
    streak = 0
    t = time.time()
    while True:
        day = time.strftime("%Y-%m-%d", time.localtime(t))
        if day in by_day and by_day[day]["seconds"] >= 60:
            streak += 1
            t -= 86400
        elif day == today:
            t -= 86400  # today not started yet doesn't break the streak
        else:
            break
        if streak > 3650:
            break
    labels = db.query(
        "SELECT label, SUM(actual_s) AS seconds FROM focus_sessions WHERE mode = 'focus' AND started_at >= ? AND label != '' "
        "GROUP BY label ORDER BY seconds DESC LIMIT 6", (since,))
    return {
        "days": per_day,
        "today_seconds": by_day.get(today, {}).get("seconds", 0) or 0,
        "today_sessions": by_day.get(today, {}).get("completed", 0) or 0,
        "total_seconds": sum(r["seconds"] or 0 for r in per_day),
        "streak": streak,
        "labels": labels,
    }


# --------------------------------------------------------- settings & ops


@app.get("/api/settings")
async def settings_get():
    return settings.all_public()


@app.put("/api/settings")
async def settings_put(values: dict[str, Any]):
    try:
        return settings.update(values)
    except KeyError as exc:
        raise HTTPException(400, f"Unknown setting: {exc}") from exc


@app.get("/api/tools")
async def tools_list():
    return describe_all()


@app.get("/api/sandbox")
async def sandbox_status():
    return await sandbox.status()


@app.post("/api/sandbox/test")
async def sandbox_test():
    result = await sandbox.run("echo sandbox ok; uname -sm; (ls ~/Desktop >/dev/null 2>&1 && echo 'home: READABLE') || echo 'home: blocked'; "
                               "(curl -s -m 4 https://example.com >/dev/null && echo 'network: on') || echo 'network: blocked'",
                               timeout=20)
    return {"exit_code": result.exit_code, "backend": result.backend, "output": result.output}


@app.get("/api/computer")
async def computer_status():
    return {"enabled": bool(settings.get("computer.enabled")), **computer.permissions()}


@app.post("/api/computer/request-permissions")
async def computer_request():
    return computer.request_permissions()


@app.websocket("/api/browser-ext/ws")
async def browser_extension_ws(websocket: WebSocket):
    from .extension_bridge import bridge

    await bridge.serve(websocket)


@app.get("/api/browser/status")
async def browser_status():
    from .extension_bridge import bridge, pairing_token

    return {"connected": bridge.connected, "version": bridge.version, "pairing_code": pairing_token(),
            "extension_path": str(config.ROOT / "extension"), "target": settings.get("browser.target")}


@app.post("/api/browser/repair")
async def browser_repair():
    """Issue a new pairing code; the old one stops working."""
    from .extension_bridge import bridge

    (config.DATA_DIR / "extension.token").unlink(missing_ok=True)
    if bridge.ws is not None:
        await bridge.ws.close(code=4401)
    return await browser_status()


@app.get("/api/voice/status")
async def voice_status():
    return {"available": voice.available(), "model": str(voice.WHISPER_DIR)}


@app.post("/api/voice/transcribe")
async def voice_transcribe(request: Request):
    if not voice.available():
        raise HTTPException(503, "Speech model not installed. Run: ./ithaca model whisper")
    data = await request.body()
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(413, "Recording too long.")
    try:
        return {"text": await voice.transcribe_wav(data)}
    except (ValueError, EOFError) as exc:
        raise HTTPException(400, f"Could not read audio: {exc}") from exc


class VoiceCommand(BaseModel):
    text: str


@app.post("/api/voice/command")
async def voice_command(body: VoiceCommand):
    """Start a task from the voice listener: new chat, default model."""
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Empty command.")
    pid, model = _default_model()
    if not (pid and model):
        raise HTTPException(400, "No model is configured.")
    cid = db.new_id("c_")
    t = db.now()
    title = "🎙 " + (text[:58] + "…" if len(text) > 58 else text)
    db.execute("INSERT INTO conversations (id, title, provider_id, model, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
               (cid, title, pid, model, t, t))
    live = runs.start(cid, text, provider_id=pid, model=model)
    return {"conversation_id": cid, "run_id": live.id}


@app.get("/api/voice/result/{cid}")
async def voice_result(cid: str):
    live = runs.active_for(cid)
    if live:
        pending = db.query_one("SELECT 1 AS x FROM approvals WHERE run_id = ? AND status = 'pending'", (live.id,))
        return {"done": False, "pending_approval": bool(pending)}
    last = db.query_one("SELECT data FROM events WHERE conversation_id = ? AND type = 'assistant_message' "
                        "ORDER BY id DESC LIMIT 1", (cid,))
    err = db.query_one("SELECT data FROM events WHERE conversation_id = ? AND type = 'error' ORDER BY id DESC LIMIT 1", (cid,))
    text = db.loads(last["data"], {}).get("content", "") if last else ""
    if not text and err:
        text = "Sorry, that failed: " + db.loads(err["data"], {}).get("message", "")
    return {"done": True, "speech": voice.speakable(text)}


@app.get("/api/local-model")
async def local_model_status():
    weights = config.ROOT / "model" / "weights"
    present = sorted(p.name for p in weights.iterdir() if p.is_dir() and not p.name.startswith(".")) if weights.exists() else []
    info: dict[str, Any] = {"url": config.LOCAL_MODEL_URL, "running": False, "models": [], "weights": present}
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{config.LOCAL_MODEL_URL}/models")
        if r.status_code == 200:
            info["running"] = True
            info["models"] = [m.get("id") for m in r.json().get("data", [])]
    except httpx.HTTPError:
        pass
    return info


class MCPBody(BaseModel):
    name: str
    transport: str = "stdio"          # stdio | http | sse | auto
    command: str = ""
    args: list[str] = []
    env: dict[str, str] = {}
    url: str = ""
    auth: str = "none"                # none | headers | oauth
    headers: dict[str, str] = {}
    trusted: bool = False
    enabled: bool = True


def _mcp_public(r: dict[str, Any]) -> dict[str, Any]:
    oauth = db.loads(r.get("oauth"), {})
    return {
        **{k: v for k, v in r.items() if k not in ("oauth", "headers", "env")},
        "args": db.loads(r["args"], []),
        "env": {k: "•••" for k in db.loads(r["env"], {})},
        "headers": {k: "•••" for k in db.loads(r.get("headers"), {})},
        "signed_in": bool(oauth.get("tokens")),
        "trusted": bool(r["trusted"]), "enabled": bool(r["enabled"]),
        **mcp.status(r["id"]),
    }


@app.get("/api/mcp")
async def mcp_list():
    return [_mcp_public(r) for r in db.query("SELECT * FROM mcp_servers ORDER BY created_at")]


@app.post("/api/mcp")
async def mcp_create(body: MCPBody):
    from .mcp_client import validate_remote_url

    if body.transport not in ("stdio", "http", "sse", "auto") or body.auth not in ("none", "headers", "oauth"):
        raise HTTPException(400, "Unknown transport or auth type.")
    if body.transport == "stdio":
        if not body.command.strip():
            raise HTTPException(400, "A command is required for local servers.")
    else:
        if (problem := validate_remote_url(body.url)):
            raise HTTPException(400, problem)
    sid = db.new_id("s_")
    try:
        db.execute(
            "INSERT INTO mcp_servers (id, name, command, args, env, enabled, trusted, created_at, transport, url, headers, auth) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, body.name.strip(), body.command.strip(), db.dumps(body.args), db.dumps(body.env), int(body.enabled),
             int(body.trusted), db.now(), body.transport, body.url.strip(), db.dumps(body.headers), body.auth))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"Could not save server: {exc}") from exc
    status: dict[str, Any] = {}
    if body.enabled:
        status = await mcp.connect(sid)
    return {"id": sid, **status}


@app.post("/api/mcp/{sid}/connect")
async def mcp_connect(sid: str):
    try:
        return await mcp.connect(sid)
    except KeyError as exc:
        raise _404() from exc


@app.get("/api/mcp/{sid}/status")
async def mcp_status(sid: str):
    row = db.query_one("SELECT * FROM mcp_servers WHERE id = ?", (sid,))
    if not row:
        raise _404()
    return _mcp_public(row)


@app.post("/api/mcp/{sid}/signout")
async def mcp_signout(sid: str):
    """Forget stored OAuth tokens (and the registered client) for a remote server."""
    await mcp.disconnect(sid)
    db.execute("UPDATE mcp_servers SET oauth = '{}' WHERE id = ?", (sid,))
    return {"ok": True}


@app.get("/api/mcp/oauth/callback", include_in_schema=False)
async def mcp_oauth_callback(state: str = "", code: str = "", error: str = "", error_description: str = ""):
    """Where the provider sends the browser after sign-in. Matched to a pending
    flow by its unguessable state value, so it needs no session cookie (strict
    cookies aren't sent on this cross-site redirect)."""
    from fastapi.responses import HTMLResponse

    ok = bool(code) and mcp.complete_oauth(state, code)
    if error:
        msg, ok = f"Sign-in failed: {error_description or error}", False
    elif ok:
        msg = "Signed in. You can close this tab and return to Ithaca."
    else:
        msg = "This sign-in link has expired. Start the sign-in again from Ithaca's settings."
    page = f"""<!doctype html><meta charset=utf-8><title>Ithaca</title>
<body style="font:16px -apple-system,system-ui;background:#141414;color:#f5f5f7;display:grid;place-items:center;height:100vh;margin:0">
<div style="text-align:center"><div style="font-size:40px">{'✓' if ok else '⚠️'}</div><p>{msg}</p></div>
<script>{'setTimeout(()=>window.close(),1500)' if ok else ''}</script></body>"""
    return HTMLResponse(page, status_code=200 if ok else 400)


class MCPPatch(BaseModel):
    trusted: bool | None = None
    enabled: bool | None = None


@app.patch("/api/mcp/{sid}")
async def mcp_patch(sid: str, body: MCPPatch):
    if body.trusted is not None:
        db.execute("UPDATE mcp_servers SET trusted = ? WHERE id = ?", (int(body.trusted), sid))
    if body.enabled is not None:
        db.execute("UPDATE mcp_servers SET enabled = ? WHERE id = ?", (int(body.enabled), sid))
        if not body.enabled:
            await mcp.disconnect(sid)
    if body.enabled or body.trusted is not None:
        try:
            await mcp.connect(sid, wait=False)
        except KeyError:
            pass
    return {"ok": True, **mcp.status(sid)}


@app.delete("/api/mcp/{sid}")
async def mcp_delete(sid: str):
    await mcp.disconnect(sid)
    db.execute("DELETE FROM mcp_servers WHERE id = ?", (sid,))
    return {"ok": True}


@app.get("/api/export")
async def export_all():
    """Everything except secrets, as one JSON file."""
    data = {
        "exported_at": time.time(),
        "conversations": db.query("SELECT * FROM conversations"),
        "events": db.query("SELECT * FROM events ORDER BY id"),
        "documents": db.query("SELECT * FROM documents"),
        "memories": [m for m in memory.list_all(100000)],
        "focus_sessions": db.query("SELECT * FROM focus_sessions"),
        "settings": settings.all_public(),
    }
    return Response(json.dumps(data, ensure_ascii=False, indent=1), media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="ithaca-export.json"'})


# ------------------------------------------------------------- web app


@app.get("/{path:path}", include_in_schema=False)
async def spa(path: str):
    if path.startswith("api/"):
        raise _404()
    dist = config.WEB_DIST
    target = (dist / path).resolve()
    if path and target.is_file() and dist in target.parents and not target.name.startswith("._"):
        headers = {"Cache-Control": "public, max-age=31536000, immutable"} if "/assets/" in f"/{path}" else {}
        return FileResponse(target, headers=headers)
    index = dist / "index.html"
    if index.exists():
        return FileResponse(index, headers={"Cache-Control": "no-cache"})
    return JSONResponse({"detail": "Web app not built. Run ./ithaca build"}, status_code=503)


def run() -> None:
    import uvicorn

    uvicorn.run("ithaca.main:app", host=config.HOST, port=config.PORT, log_level="info")


if __name__ == "__main__":
    run()
