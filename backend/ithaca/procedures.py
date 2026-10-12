"""Learned procedures: reuse the tool steps of runs that worked.

When a run finishes cleanly after doing real work, its successful tool calls
are saved as a *skeleton*: tool names plus structural arguments (well-known
URLs, app names, keys, paths). Free text — what was typed, written or
searched — is replaced with a placeholder. For a similar request later, the
skeleton is shown to the model as a worked example. Small models follow a
known-good sequence far more reliably than they plan one from scratch.

Safety: skeletons never carry free text, so a web page that injected
instructions into a run can't persist them through a procedure. Only URLs the
user typed, or well-known sites, survive. A complaint in the user's next
message ("that didn't work") counts against the procedure, and a procedure
that fails as often as it works is no longer suggested.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import urlparse

import numpy as np

from . import db, embeddings, events, settings

# arguments kept verbatim (short, structural); everything else becomes "…"
KEEP_ARGS = {"name", "app", "key", "keys", "direction", "folder", "list", "path", "index", "new_tab", "submit",
             "button", "clicks", "query_kind"}
KNOWN_HOSTS = {"docs.new", "doc.new", "docs.google.com", "mail.google.com", "calendar.google.com", "drive.google.com",
               "sheets.google.com", "www.youtube.com", "youtube.com", "github.com", "www.google.com", "maps.google.com"}
SKIP_TOOLS = {"tool_search", "update_plan", "current_time", "memory_search"}
FAILURE_RE = re.compile(r"\b(couldn'?t|could not|unable to|failed|didn'?t work|wasn'?t able|sorry|I can'?t|not possible)\b", re.I)
COMPLAINT_RE = re.compile(r"\b(didn'?t work|did not work|doesn'?t work|not working|that'?s wrong|wrong|you didn'?t|it failed|"
                          r"that failed|not what I|nothing happened|undo|try again)\b", re.I)
MAX_REQUESTS = 6


def _keep_url(url: str, user_text: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in KNOWN_HOSTS or (host and host in user_text.lower())


def skeletonize(calls: list[dict[str, Any]], user_text: str) -> list[dict[str, Any]]:
    out = []
    for c in calls:
        args = {}
        for k, v in (c.get("arguments") or {}).items():
            if k == "url" and isinstance(v, str):
                args[k] = v if _keep_url(v, user_text) else "<url>"
            elif k == "ref":
                args[k] = "<element number from the page>"
            elif k in KEEP_ARGS and isinstance(v, (str, int, bool)) and len(str(v)) <= 60:
                args[k] = v
            else:
                args[k] = "…"
        step = {"tool": c["name"], "args": args}
        if not out or out[-1] != step:  # collapse exact repeats
            out.append(step)
    return out


def _render(skeleton: list[dict[str, Any]]) -> str:
    def arg(v: Any) -> str:
        return v if isinstance(v, str) and v in ("…",) else json.dumps(v)

    return " → ".join(f"{s['tool']}({', '.join(f'{k}={arg(v)}' for k, v in s['args'].items())})" for s in skeleton)


def _run_calls(conversation_id: str, run_id: str) -> tuple[list[dict[str, Any]], str, bool]:
    """Successful tool calls of a run (in order), its final text, and whether its last tool call failed."""
    rows = [r for r in events.list_for(conversation_id) if r.get("run_id") == run_id]
    results = {r["data"].get("tool_call_id"): r["data"] for r in rows if r["type"] == "tool_result"}
    calls, final_text, last_failed = [], "", False
    for r in rows:
        if r["type"] != "assistant_message":
            continue
        final_text = r["data"].get("content") or final_text
        for c in r["data"].get("tool_calls") or []:
            res = results.get(c.get("id")) or {}
            last_failed = bool(res.get("is_error"))
            if not res.get("is_error") and c["name"] not in SKIP_TOOLS:
                calls.append(c)
    return calls, final_text, last_failed


async def record(conversation_id: str, run_id: str, user_text: str) -> dict[str, Any] | None:
    """Save the run's procedure if it did real work and ended cleanly."""
    if not settings.get("agent.learn_procedures"):
        return None
    from .tools.registry import registry

    calls, final_text, last_failed = _run_calls(conversation_id, run_id)
    acting = [c for c in calls if (t := registry.get(c["name"])) and t.effects - {"read"}]
    if not acting or last_failed or FAILURE_RE.search(final_text or ""):
        return None
    skeleton = skeletonize(calls, user_text)
    if len(skeleton) > 12:
        return None  # long, wandering runs make poor examples
    shash = hashlib.sha256(json.dumps(skeleton, sort_keys=True).encode()).hexdigest()
    request = " ".join(user_text.split())[:300]
    now = db.now()
    row = db.query_one("SELECT * FROM procedures WHERE skeleton_hash = ?", (shash,))
    if row:
        reqs = [request] + [r for r in row["requests"].split("\n") if r and r != request]
        text = "\n".join(reqs[:MAX_REQUESTS])
        (vec,), model = await embeddings.embed([text])
        db.execute("UPDATE procedures SET requests = ?, successes = successes + 1, embedding = ?, embed_model = ?, "
                   "last_conversation_id = ?, last_run_id = ?, updated_at = ? WHERE id = ?",
                   (text, embeddings.to_blob(vec), model, conversation_id, run_id, now, row["id"]))
        pid = row["id"]
    else:
        (vec,), model = await embeddings.embed([request])
        pid = db.new_id("p_")
        db.execute("INSERT INTO procedures (id, skeleton, skeleton_hash, requests, embedding, embed_model, "
                   "last_conversation_id, last_run_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (pid, db.dumps(skeleton), shash, request, embeddings.to_blob(vec), model, conversation_id, run_id, now, now))
    return get(pid)


def note_feedback(conversation_id: str, user_text: str) -> None:
    """A complaint right after a run counts against the procedure that run produced."""
    if not COMPLAINT_RE.search(user_text or ""):
        return
    row = db.query_one("SELECT id FROM procedures WHERE last_conversation_id = ? ORDER BY updated_at DESC LIMIT 1",
                       (conversation_id,))
    if row:
        db.execute("UPDATE procedures SET failures = failures + 1, last_conversation_id = NULL WHERE id = ?", (row["id"],))


async def recall(user_text: str, k: int = 2, min_sim: float = 0.35) -> list[dict[str, Any]]:
    if not settings.get("agent.learn_procedures") or not user_text.strip():
        return []
    rows = db.query("SELECT * FROM procedures WHERE failures < successes AND embedding IS NOT NULL")
    if not rows:
        return []
    (qvec,), model = await embeddings.embed([user_text])
    rows = [r for r in rows if r["embed_model"] == model]
    if not rows:
        return []
    sims = np.stack([embeddings.from_blob(r["embedding"]) for r in rows]) @ qvec
    words = {w for w in re.findall(r"[a-z0-9]{3,}", user_text.lower()) if w not in embeddings.STOPWORDS}
    scored = []
    for r, s in zip(rows, sims):
        overlap = len(words & set(re.findall(r"[a-z0-9]{3,}", r["requests"].lower()))) / max(len(words), 1)
        score = 0.7 * float(s) + 0.3 * overlap
        if score >= min_sim:
            scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    return [{**_public(r), "score": round(sc, 3)} for sc, r in scored[:k]]


def prompt_block(found: list[dict[str, Any]]) -> str:
    if not found:
        return ""
    lines = ["Procedures that worked before for similar requests (follow one if it fits, filling in the details "
             "marked …; element numbers come from the page you see):"]
    for p in found:
        example = p["requests"][0] if p["requests"] else ""
        lines.append(f"- For “{example}”: {_render(p['skeleton'])}")
    return "\n".join(lines)


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": row["id"], "skeleton": db.loads(row["skeleton"], []), "steps": _render(db.loads(row["skeleton"], [])),
            "requests": [r for r in row["requests"].split("\n") if r], "successes": row["successes"],
            "failures": row["failures"], "updated_at": row["updated_at"]}


def get(pid: str) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM procedures WHERE id = ?", (pid,))
    return _public(row) if row else None


def list_all() -> list[dict[str, Any]]:
    return [_public(r) for r in db.query("SELECT * FROM procedures ORDER BY updated_at DESC")]


def delete(pid: str) -> bool:
    return db.execute("DELETE FROM procedures WHERE id = ?", (pid,)) > 0
