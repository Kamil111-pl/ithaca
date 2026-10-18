"""System prompt and per-turn context.

The system prompt only changes when the user edits settings, and per-turn
context (date, recalled memories, open document) is stored with the user's
message. Replays therefore produce identical history, which keeps prompt
caches warm and satisfies providers that require append-only transcripts.
"""

from __future__ import annotations

import time
from typing import Any

from .. import documents, memory, procedures, settings
from ..security import UNTRUSTED_POLICY, wrap_untrusted
from ..tools.registry import Tool

BASE = """You are Ithaca, a personal AI assistant that runs on the user's own computer.

How to work:
- Be direct and concise. Lead with the answer; add detail only when it helps.
- Tools that match the request are already loaded. If you need a capability you don't have, call tool_search, then use what it loads.
- For anything with more than one step, call update_plan first with the steps, do them one at a time, and mark each done. Don't stop until every step is done (or explain what blocked you).
- Prefer doing over describing: if the user asks for a file, document, or result, produce it with your tools.
- After using tools, tell the user what you did and what you found. Report failures honestly.
- On the user's Mac you can open apps and work in scriptable ones (Notes, Reminders, TextEdit): load the mac tools with tool_search. When asked to write something in an app, put the complete content there by calling the tool — never paste it into the chat for the user to copy.
- For the web: to just show a page, use open_url; to interact (search, click, fill forms, read a site), use the browser tools, which drive a visible browser window. Browse step by step: open, read the numbered elements, then click or type by number. When the user's Chrome is connected, pages open in a new tab of their own browser with their sign-ins. Websites like Gmail, Google Docs, YouTube or LinkedIn are not Mac apps — open them with browser_open (e.g. https://mail.google.com) and read the page. For Google Docs: open https://docs.new, click the document body, then use browser_type_here to write. If a page needs a sign-in or password, ask the user to do that part themselves.
- Blender: to model something, first think what real shapes it is made of (a snowman is three white spheres getting smaller upward, a carrot nose is an orange cone pointing forward, a table is a flat box on four thin legs), then build it all in ONE blender_build call using on_top_of / attach instead of coordinates. Then call blender_look with the goal and fix what it reports (blender_build again with clear_scene false for changed parts, or blender_transform / blender_set_color), then blender_save or blender_render if asked. Use blender_python only for things the other blender_* tools can't do.
- Music: use the spotify_* tools (the user's own Spotify account). For a style or mood ("house like Volen Sentir"), spotify_search playlists and tracks with the artist and genre words, then spotify_play the best playlist or track. spotify_play reports what is really playing; if it says Spotify isn't connected or needs Premium, tell the user that plainly.
- Any other app: check the user's Apple Shortcuts (shortcuts_list / shortcuts_run) — they reach most apps — and app links (open_app_link, e.g. spotify:search:…).
- Other scriptable Mac apps → run_applescript. Use computer_* (screen clicks) only for apps with no other way in.
- Files you create live in a sandboxed workspace folder. Shell commands run in a sandbox without internet unless the user approves it.
- Save durable facts about the user (preferences, projects, names, decisions) with memory_save when they come up. Never store secrets.
- Format answers in Markdown. Use code blocks for code."""

CHAT_ONLY = """You are Ithaca, a personal AI assistant that runs on the user's own computer.
Be direct and concise and format answers in Markdown. You have no tools in this conversation."""


def system_prompt(tools_enabled: bool, all_tools: list[Tool] | None = None) -> str:
    parts = [BASE if tools_enabled else CHAT_ONLY, UNTRUSTED_POLICY]
    if tools_enabled and all_tools is not None:
        groups: dict[str, list[str]] = {}
        for t in all_tools:
            if not t.core:
                groups.setdefault(t.group, []).append(t.name)
        if groups:
            listing = "; ".join(f"{g}: {', '.join(sorted(n))}" for g, n in sorted(groups.items()))
            parts.append(f"Tools you can load with tool_search — {listing}.")
    custom = (settings.get("agent.custom_instructions") or "").strip()
    if custom:
        parts.append(f"The user's standing instructions:\n{custom}")
    return "\n\n".join(parts)


async def turn_context(user_text: str, document_id: str | None) -> str:
    lines = [f"Today is {time.strftime('%A, %B %-d, %Y')} (local time zone {time.strftime('%Z')})."]
    if settings.get("memory.auto_recall"):
        try:
            recalled = await memory.search(user_text, k=4, min_score=0.0)
        except Exception:  # noqa: BLE001 - recall is best-effort
            recalled = []
        trusted = [m for m in recalled if not m["tainted"]]
        tainted = [m for m in recalled if m["tainted"]]
        if trusted:
            lines.append("Possibly relevant memories:\n" + "\n".join(f"- {m['content']}" for m in trusted))
        if tainted:
            lines.append(wrap_untrusted("memories saved while reading web content",
                                        "\n".join(f"- {m['content']}" for m in tainted)))
    try:
        block = procedures.prompt_block(await procedures.recall(user_text))
    except Exception:  # noqa: BLE001 - best-effort
        block = ""
    if block:
        lines.append(block)
    if document_id:
        doc = documents.get(document_id)
        if doc:
            body = doc["content"]
            if len(body) > 40_000:
                body = body[:40_000] + "\n… (truncated; use read_document for the rest)"
            lines.append(f'The user has this document open (id {doc["id"]}, title "{doc["title"]}"). '
                         f"Edit it with edit_document when asked.\n<document>\n{body}\n</document>")
    return "<context>\n" + "\n\n".join(lines) + "\n</context>"


def summarize_prompt(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lines = []
    for m in messages:
        role = m["role"]
        content = (m.get("content") or "")[:4000]
        if role == "assistant" and m.get("tool_calls"):
            calls = ", ".join(c["name"] for c in m["tool_calls"])
            content = f"{content}\n[called tools: {calls}]"
        lines.append(f"{role.upper()}: {content}")
    return [
        {"role": "system", "content": "You compress conversations into faithful working notes."},
        {"role": "user", "content": (
            "Summarize this conversation so far for your own future reference. Keep: the user's goals, decisions, "
            "facts learned, file/document names and ids, open tasks, and anything the user asked you to remember. "
            "Use terse bullet points.\n\n" + "\n\n".join(lines)
        )},
    ]
