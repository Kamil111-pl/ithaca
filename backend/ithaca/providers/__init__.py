"""Provider registry backed by the `providers` table."""

from __future__ import annotations

import os
from typing import Any

from .. import config, db
from .anthropic_provider import DEFAULT_MODEL as CLAUDE_DEFAULT
from .anthropic_provider import AnthropicProvider
from .base import ModelInfo, ProviderError, ToolSpec
from .openai_compat import OpenAICompatProvider

__all__ = ["ProviderError", "ToolSpec", "get_client", "list_rows", "get_row", "create", "update", "delete", "probe"]

LOCAL_PROVIDER_ID = "local-mlx"
LOCAL_VISION_ID = "local-vision"
LOCAL_IDS = (LOCAL_PROVIDER_ID, LOCAL_VISION_ID)


def _public(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["models"] = db.loads(row["models"], [])
    key = row.get("api_key") or ""
    out["api_key"] = ""
    out["has_api_key"] = bool(key) or (row["kind"] == "anthropic" and bool(os.environ.get("ANTHROPIC_API_KEY")))
    return out


def list_rows() -> list[dict[str, Any]]:
    return [_public(r) for r in db.query("SELECT * FROM providers ORDER BY created_at")]


def get_row(provider_id: str, *, secret: bool = False) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM providers WHERE id = ?", (provider_id,))
    if row is None:
        return None
    if secret:
        row = dict(row)
        row["models"] = db.loads(row["models"], [])
        return row
    return _public(row)


def get_client(provider_id: str):
    row = get_row(provider_id, secret=True)
    if row is None:
        raise ProviderError(f"Unknown provider {provider_id!r}")
    if row["kind"] == "anthropic":
        return AnthropicProvider(api_key=row["api_key"], base_url=row["base_url"])
    return OpenAICompatProvider(row["base_url"], row["api_key"])


def create(name: str, kind: str, base_url: str = "", api_key: str = "", default_model: str = "",
           models: list[dict[str, Any]] | None = None, provider_id: str | None = None) -> dict[str, Any]:
    if kind not in ("openai", "anthropic"):
        raise ValueError("kind must be 'openai' or 'anthropic'")
    if kind == "openai" and not base_url:
        raise ValueError("base_url is required for OpenAI-compatible providers")
    pid = provider_id or db.new_id("p_")
    db.execute(
        "INSERT INTO providers (id, name, kind, base_url, api_key, models, default_model, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (pid, name, kind, base_url.rstrip("/"), api_key, db.dumps(models or []), default_model, db.now()),
    )
    return get_row(pid)  # type: ignore[return-value]


def update(provider_id: str, fields: dict[str, Any]) -> dict[str, Any]:
    allowed = {"name", "base_url", "api_key", "default_model", "models"}
    sets, params = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "api_key" and v == "":
            continue  # empty means "keep the stored key"
        if k == "models":
            v = db.dumps(v)
        if k == "base_url":
            v = str(v).rstrip("/")
        sets.append(f"{k} = ?")
        params.append(v)
    if sets:
        db.execute(f"UPDATE providers SET {', '.join(sets)} WHERE id = ?", (*params, provider_id))
    row = get_row(provider_id)
    if row is None:
        raise ProviderError("Unknown provider")
    return row


def delete(provider_id: str) -> None:
    db.execute("DELETE FROM providers WHERE id = ?", (provider_id,))


def model_info(provider_id: str, model: str) -> dict[str, Any]:
    row = get_row(provider_id, secret=True)
    if not row:
        return {}
    for m in row["models"]:
        if m.get("id") == model:
            return m
    return {}


async def refresh_models(provider_id: str) -> dict[str, Any]:
    """Fetch the provider's model list, keeping capability flags already probed."""
    client = get_client(provider_id)
    found: list[ModelInfo] = await client.list_models()
    if provider_id in LOCAL_IDS:
        # mlx_lm.server reports the absolute weights path; it is launched from
        # model/weights, so the folder name is the stable, shorter model id.
        for info in found:
            if info.id.startswith("/"):
                info.id = info.id.rstrip("/").rsplit("/", 1)[-1]
    row = get_row(provider_id, secret=True) or {}
    known = {m["id"]: m for m in row.get("models", [])}
    merged = []
    for info in found:
        prev = known.get(info.id, {})
        merged.append({
            "id": info.id,
            "tools": prev.get("tools", info.tools),
            "vision": prev.get("vision"),
            "coords": prev.get("coords"),
            "context": prev.get("context") or info.context,
        })
    fields: dict[str, Any] = {"models": merged}
    if merged and row.get("default_model") not in {m["id"] for m in merged}:
        fields["default_model"] = merged[0]["id"]
    return update(provider_id, fields)


_PROBE_TOOL = ToolSpec(
    name="get_current_time",
    description="Returns the current time. Call this whenever the user asks for the time.",
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
)


async def probe(provider_id: str, model: str) -> dict[str, Any]:
    """Check once whether `model` emits structured tool calls, and remember it.

    This replaces guessing tool support from model names.
    """
    client = get_client(provider_id)
    messages = [
        {"role": "system", "content": "You are a test harness. Use tools when they can answer."},
        {"role": "user", "content": "What time is it right now? Use the tool."},
    ]
    supports = False
    error = None
    try:
        async for ev in client.stream(model, messages, [_PROBE_TOOL], max_tokens=1024, effort="low"):
            if ev["type"] == "tool_call" and ev["name"] == _PROBE_TOOL.name:
                supports = True
    except ProviderError as exc:
        error = str(exc)
    row = get_row(provider_id, secret=True) or {"models": []}
    models = row["models"]
    for m in models:
        if m["id"] == model:
            m["tools"] = supports if error is None else m.get("tools")
            break
    else:
        models.append({"id": model, "tools": supports if error is None else None, "context": None})
    update(provider_id, {"models": models})
    return {"model": model, "tools": supports, "error": error}


def ensure_local_vision() -> None:
    """Register the local vision model server (Qwen3-VL via mlx-vlm) if missing."""
    if get_row(LOCAL_VISION_ID):
        return
    create(
        "Ithaca Vision (MLX)",
        "openai",
        base_url=config.LOCAL_VISION_URL,
        default_model="vision",
        # Qwen-VL models answer GUI positions on a 0–1000 grid.
        models=[{"id": "vision", "tools": True, "vision": True, "coords": "norm1000", "context": 32768}],
        provider_id=LOCAL_VISION_ID,
    )


def bootstrap_defaults() -> None:
    """First boot: register the local MLX servers and Claude (if a key is in the env)."""
    ensure_local_vision()
    if db.query_one("SELECT 1 AS x FROM providers WHERE id != ? LIMIT 1", (LOCAL_VISION_ID,)):
        return
    create(
        "Ithaca Local (MLX)",
        "openai",
        base_url=config.LOCAL_MODEL_URL,
        default_model="ithaca-local",
        models=[{"id": "ithaca-local", "tools": True, "context": 32768}],
        provider_id=LOCAL_PROVIDER_ID,
    )
    if os.environ.get("ANTHROPIC_API_KEY"):
        create(
            "Claude",
            "anthropic",
            default_model=CLAUDE_DEFAULT,
            models=[{"id": CLAUDE_DEFAULT, "tools": True, "context": 1_000_000}],
            provider_id="claude",
        )


async def resolve_local_model(provider_id: str, model: str) -> str:
    """For the bundled local MLX server, follow whatever model is being served.

    The server is started with one model folder (base, ithaca-local, …). If the
    requested id isn't served, refresh the list and use the served model, so a
    chat saved with an old id keeps working after you switch weights.
    """
    if provider_id not in LOCAL_IDS:
        return model
    try:
        row = await refresh_models(provider_id)
    except ProviderError:
        return model
    ids = [m["id"] for m in row.get("models", [])]
    if model in ids or not ids:
        return model
    return row.get("default_model") or ids[0]
