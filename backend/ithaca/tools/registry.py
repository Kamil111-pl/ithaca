"""Tool registry, per-run tool context, and tool search.

Tools declare their *effects* so policy can be enforced by the runtime
instead of by prompt text:

    read      reads local data (workspace files, documents, memory)
    write     changes local data
    exec      runs code (always through the sandbox)
    network   talks to the internet
    external  calls a third-party system (MCP servers)

`untrusted_output` marks tools whose results come from outside (web pages,
MCP servers). Reading such output taints the run; see security.py.
"""

from __future__ import annotations

import json

import math
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..providers.base import ToolSpec


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    # Set when this particular result came from outside the machine, even if
    # the tool is not always untrusted (e.g. read_document of a web report).
    untrusted_source: str | None = None
    data: dict[str, Any] | None = None   # structured payload for the UI
    # [{"media_type": "image/jpeg", "data": <base64>}] — e.g. screenshots
    images: list[dict[str, str]] | None = None


@dataclass
class ToolContext:
    run_id: str
    conversation_id: str
    tainted: bool = False
    taint_sources: list[str] = field(default_factory=list)
    loaded: set[str] = field(default_factory=set)
    document_id: str | None = None
    # Per-run services a tool may need (the run's model client and model id).
    services: dict[str, Any] = field(default_factory=dict)
    emit: Callable[[dict[str, Any]], Awaitable[None]] | None = None

    async def progress(self, payload: dict[str, Any]) -> None:
        if self.emit:
            await self.emit(payload)


Handler = Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult]]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    effects: frozenset[str] = frozenset({"read"})
    group: str = "general"
    core: bool = False
    untrusted_output: bool = False
    trusted_external: bool = False
    keywords: str = ""

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.parameters)


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        self._tools[tool.name] = tool
        return tool

    def unregister_prefix(self, prefix: str) -> None:
        for name in [n for n in self._tools if n.startswith(prefix)]:
            del self._tools[name]

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return sorted(self._tools.values(), key=lambda t: (t.group, t.name))

    def core(self) -> list[Tool]:
        return [t for t in self.all() if t.core]

    def search(self, query: str, limit: int = 6, exclude: set[str] | None = None) -> list[Tool]:
        """BM25 over name, group, description and keywords."""
        exclude = exclude or set()
        docs = [t for t in self.all() if t.name not in exclude and not t.core]
        if not docs:
            return []
        q_terms = _terms(query)
        if not q_terms:
            return []
        tokenized = {t.name: _terms(f"{t.name} {t.group} {t.description} {t.keywords}") for t in docs}
        n = len(docs)
        avgdl = sum(len(v) for v in tokenized.values()) / n
        df: dict[str, int] = {}
        for terms in tokenized.values():
            for term in set(terms):
                df[term] = df.get(term, 0) + 1
        k1, b = 1.4, 0.75
        scored = []
        for t in docs:
            terms = tokenized[t.name]
            tf: dict[str, int] = {}
            for term in terms:
                tf[term] = tf.get(term, 0) + 1
            score = 0.0
            for q in q_terms:
                if q not in tf:
                    continue
                idf = math.log(1 + (n - df[q] + 0.5) / (df[q] + 0.5))
                score += idf * tf[q] * (k1 + 1) / (tf[q] + k1 * (1 - b + b * len(terms) / avgdl))
            if set(q_terms) & set(_terms(t.name.replace("_", " "))):
                score += 1.5
            if score > 0:
                scored.append((score, t))
        scored.sort(key=lambda x: -x[0])
        return [t for _, t in scored[:limit]]


_STOP = {"the", "a", "an", "to", "of", "and", "or", "for", "in", "on", "with", "my", "me", "i", "is", "it", "this", "that", "please", "can", "you"}


def _stem(word: str) -> str:
    for suffix in ("ing", "ies", "es", "s", "ed"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def _terms(text: str) -> list[str]:
    return [_stem(w) for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP]


registry = Registry()


def tool(
    name: str,
    description: str,
    parameters: dict[str, Any] | None = None,
    *,
    effects: set[str] | frozenset[str] = frozenset({"read"}),
    group: str = "general",
    core: bool = False,
    untrusted_output: bool = False,
    keywords: str = "",
):
    """Decorator that registers an async handler as a tool."""

    def wrap(fn: Handler) -> Handler:
        registry.register(Tool(
            name=name,
            description=description,
            parameters=parameters or {"type": "object", "properties": {}, "additionalProperties": False},
            handler=fn,
            effects=frozenset(effects),
            group=group,
            core=core,
            untrusted_output=untrusted_output,
            keywords=keywords,
        ))
        return fn

    return wrap


def obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


def coerce_args(schema: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """Turn string values into the schema's type when they parse cleanly ("4" → 4, "True" → True).

    Models that write tool calls as XML (Qwen3.5) send every value as text; the
    server only converts types for tools described in the prompt."""
    if not isinstance(args, dict):
        return args
    props = schema.get("properties", {})
    out = dict(args)
    for key, value in args.items():
        expected = (props.get(key) or {}).get("type")
        if not isinstance(value, str) or expected in (None, "string"):
            continue
        text = value.strip()
        try:
            if expected == "integer" and re.fullmatch(r"-?\d+", text):
                out[key] = int(text)
            elif expected == "number" and re.fullmatch(r"-?\d+(\.\d+)?([eE][-+]?\d+)?", text):
                out[key] = float(text) if any(c in text for c in ".eE") else int(text)
            elif expected == "boolean" and text.lower() in ("true", "false"):
                out[key] = text.lower() == "true"
            elif expected in ("array", "object"):
                parsed = json.loads(text)
                if isinstance(parsed, list if expected == "array" else dict):
                    out[key] = parsed
        except (ValueError, json.JSONDecodeError):
            pass
    return out


def validate_args(schema: dict[str, Any], args: dict[str, Any]) -> str | None:
    """Minimal JSON-schema check: required keys and primitive types."""
    if not isinstance(args, dict):
        return "Arguments must be an object."
    props = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in args:
            return f"Missing required argument '{key}'."
    for key, value in args.items():
        if key not in props:
            if schema.get("additionalProperties") is False:
                return f"Unknown argument '{key}'."
            continue
        expected = props[key].get("type")
        ok = {
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
            "array": isinstance(value, list),
            "object": isinstance(value, dict),
        }.get(expected, True)
        if not ok:
            return f"Argument '{key}' must be of type {expected}."
        if "enum" in props[key] and value not in props[key]["enum"]:
            return f"Argument '{key}' must be one of {props[key]['enum']}."
    return None
