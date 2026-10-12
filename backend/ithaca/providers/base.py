"""Provider-neutral message and streaming types.

Every provider adapter converts to and from this one shape, so the agent
loop never sees vendor formats:

    {"role": "system" | "user", "content": str}
    {"role": "assistant", "content": str,
     "tool_calls": [{"id", "name", "arguments": dict}],
     "raw": {...}}                      # provider-specific replay data
    {"role": "tool", "tool_call_id": str, "name": str, "content": str}

Adapters yield StreamEvent dicts:

    {"type": "text", "text": str}
    {"type": "thinking", "text": str}
    {"type": "tool_call", "id", "name", "arguments": dict}
    {"type": "done", "stop_reason": str, "usage": {...}, "raw": {...}}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol

Message = dict[str, Any]
StreamEvent = dict[str, Any]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class ModelInfo:
    id: str
    tools: bool | None = None       # None = not probed yet
    context: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class ProviderError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class Provider(Protocol):
    kind: str

    def stream(
        self,
        model: str,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        max_tokens: int,
        effort: str | None = None,
    ) -> AsyncIterator[StreamEvent]: ...

    async def list_models(self) -> list[ModelInfo]: ...

    async def complete(self, model: str, messages: list[Message], *, max_tokens: int = 2048) -> str: ...
