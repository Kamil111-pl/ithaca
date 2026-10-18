"""User-editable settings stored in the `settings` table."""

from __future__ import annotations

from typing import Any

from . import db

DEFAULTS: dict[str, Any] = {
    # Model selection used for new conversations.
    "chat.default_provider_id": "",
    "chat.default_model": "",
    # "search": the model starts with a handful of core tools and loads more
    # through tool_search. "all": every tool schema is sent up front.
    "agent.tool_loading": "search",
    # "smart": ask only for risky calls (see security.py). "always": ask for
    # every side effect. "never": run everything (sandbox still applies).
    "agent.approval_mode": "smart",
    "agent.max_rounds": 24,
    # Reasoning effort for Claude models (low | medium | high | xhigh | max).
    "agent.effort": "medium",
    "agent.custom_instructions": "",
    # Tools auto-loaded per request (tool-search mode) and sampling for local models.
    "agent.preload_tools": 6,
    "agent.local_temperature": 0.3,
    "agent.learn_procedures": True,
    "agent.local_thinking": False,
    "agent.think_budget": 0,  # >0: local models think, capped at this many tokens per reply
    "apps.blender_path": "",
    "spotify.client_id": "",  # from the user's own app at developer.spotify.com (public with PKCE)
    # Inject the most relevant memories into each turn.
    "memory.auto_recall": True,
    # auto | seatbelt | docker | off
    "sandbox.mode": "auto",
    "sandbox.allow_network": False,
    "sandbox.timeout_s": 60,
    "sandbox.docker_image": "python:3.12-slim",
    # Vision computer use (screenshots + mouse/keyboard). Off until the user enables it.
    "computer.enabled": False,
    "computer.settle_s": 0.7,
    # Browser tools: "auto" uses your Chrome when the Ithaca extension is connected,
    # "chrome" requires it, "separate" always uses Ithaca's own browser window.
    "browser.target": "auto",
    # The user's browser app opened via macOS automation when the extension isn't connected.
    "browser.app": "Google Chrome",
    # duckduckgo | searxng
    "web.search_backend": "duckduckgo",
    "web.searxng_url": "",
    # Optional URL or email added to the fetch user-agent (Wikipedia requires one).
    "web.contact": "",
    # Empty provider → built-in hashed embeddings (no download, lexical-ish).
    "embeddings.provider_id": "",
    "embeddings.model": "",
}


def get(key: str) -> Any:
    row = db.query_one("SELECT value FROM settings WHERE key = ?", (key,))
    if row is None:
        return DEFAULTS.get(key)
    return db.loads(row["value"])


def set(key: str, value: Any) -> None:  # noqa: A001 - mirrors get()
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, db.dumps(value)),
    )


def all_public() -> dict[str, Any]:
    """Every user-facing setting with defaults filled in."""
    stored = {r["key"]: db.loads(r["value"]) for r in db.query("SELECT key, value FROM settings")}
    return {k: stored.get(k, v) for k, v in DEFAULTS.items()}


def update(values: dict[str, Any]) -> dict[str, Any]:
    unknown = [k for k in values if k not in DEFAULTS]
    if unknown:
        raise KeyError(", ".join(unknown))
    for k, v in values.items():
        set(k, v)
    return all_public()
