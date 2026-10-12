"""Append-only conversation event log and history reconstruction."""

from __future__ import annotations

from typing import Any

from . import db

# Event types that make up the model-visible transcript.
TRANSCRIPT_TYPES = ("user_message", "assistant_message", "tool_result", "compaction", "runtime_note")


def append(conversation_id: str, run_id: str | None, type_: str, data: dict[str, Any]) -> int:
    eid = db.execute(
        "INSERT INTO events (conversation_id, run_id, type, data, created_at) VALUES (?, ?, ?, ?, ?)",
        (conversation_id, run_id, type_, db.dumps(data), db.now()),
    )
    db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (db.now(), conversation_id))
    return eid


def list_for(conversation_id: str) -> list[dict[str, Any]]:
    rows = db.query("SELECT * FROM events WHERE conversation_id = ? ORDER BY id", (conversation_id,))
    for r in rows:
        r["data"] = db.loads(r["data"], {})
    return rows


def render_user(data: dict[str, Any]) -> str:
    context = data.get("context")
    text = data.get("content", "")
    return f"{context}\n\n{text}" if context else text


def history(conversation_id: str, before_event_id: int | None = None) -> list[dict[str, Any]]:
    """Rebuild provider-neutral messages from the log.

    Assistant tool calls that never got a result (cancelled or crashed runs)
    receive a synthetic result so every provider accepts the transcript.
    """
    rows = [r for r in list_for(conversation_id) if r["type"] in TRANSCRIPT_TYPES]
    # The latest compaction summarizes every transcript event up to
    # `upto_event_id`; later events are replayed verbatim.
    upto, summary = 0, None
    for r in rows:
        if r["type"] == "compaction":
            upto, summary = r["data"]["upto_event_id"], r["data"]["summary"]
    rows = [r for r in rows if r["type"] != "compaction" and r["id"] > upto
            and (before_event_id is None or r["id"] < before_event_id)]
    messages: list[dict[str, Any]] = []
    if summary:
        messages.append({"role": "user", "content": f"<conversation-summary>\n{summary}\n</conversation-summary>"})
        messages.append({"role": "assistant", "content": "Understood, I have the summary of our earlier conversation."})

    pending: dict[str, str] = {}

    def close_pending() -> None:
        for call_id, name in list(pending.items()):
            messages.append({"role": "tool", "tool_call_id": call_id, "name": name,
                             "content": "(This tool call was not executed: the run was interrupted.)", "is_error": True})
        pending.clear()

    for r in rows:
        d = r["data"]
        if r["type"] == "user_message":
            close_pending()
            messages.append({"role": "user", "content": render_user(d)})
        elif r["type"] == "assistant_message":
            close_pending()
            messages.append({
                "role": "assistant",
                "content": d.get("content", ""),
                "tool_calls": d.get("tool_calls") or [],
                "raw": d.get("raw") or {},
            })
            for c in d.get("tool_calls") or []:
                pending[c["id"]] = c["name"]
        elif r["type"] == "runtime_note":
            close_pending()
            messages.append({"role": "user", "content": f"<runtime-note>{d.get('content', '')}</runtime-note>"})
        elif r["type"] == "tool_result":
            pending.pop(d["tool_call_id"], None)
            msg = {"role": "tool", "tool_call_id": d["tool_call_id"], "name": d.get("name", ""),
                   "content": d.get("content", ""), "is_error": bool(d.get("is_error"))}
            if d.get("images"):
                msg["images"] = d["images"]
            messages.append(msg)
    close_pending()
    return messages


def loaded_tools(conversation_id: str) -> set[str]:
    """Tools loaded through tool_search earlier in this conversation stay loaded."""
    names: set[str] = set()
    for r in db.query(
        "SELECT type, data FROM events WHERE conversation_id = ? AND type IN ('tool_result', 'tools_loaded') ORDER BY id",
        (conversation_id,),
    ):
        d = db.loads(r["data"], {})
        if r["type"] == "tools_loaded" or d.get("name") == "tool_search":
            names.update((d.get("data") or d).get("loaded", []))
    return names


def transcript_event_ids(conversation_id: str) -> list[int]:
    return [r["id"] for r in db.query(
        "SELECT id FROM events WHERE conversation_id = ? AND type IN ('user_message','assistant_message','tool_result') ORDER BY id",
        (conversation_id,),
    )]
