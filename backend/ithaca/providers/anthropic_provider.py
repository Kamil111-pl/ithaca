"""Adapter for Claude via the official Anthropic Python SDK.

Notes that shape this file:
- Current Claude models run adaptive thinking; we ask for summarized
  thinking so the UI can show reasoning progress.
- Assistant turns are replayed with their original content blocks
  (including thinking blocks) whenever the model is unchanged, which keeps
  the history append-only as preserved thinking requires.
- `fallbacks: "default"` re-runs a safety-declined request on Anthropic's
  recommended fallback model server-side instead of failing the turn.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

import anthropic

from .base import Message, ModelInfo, ProviderError, StreamEvent, ToolSpec

DEFAULT_MODEL = "claude-opus-5-5"

NO_KEY = ("No Anthropic credentials. Add an API key in Settings → Models, or set ANTHROPIC_API_KEY "
          "(or run `ant auth login`) before starting Ithaca.")

_REPLAYABLE_BLOCKS = {"text", "thinking", "redacted_thinking", "tool_use"}


def _supports_adaptive(model: str) -> bool:
    return not model.startswith(("claude-haiku", "claude-3"))


def _supports_server_fallback(model: str) -> bool:
    return model.startswith(("claude-opus-5", "claude-fable-5", "claude-sonnet-5-5"))


class AnthropicProvider:
    kind = "anthropic"

    def __init__(self, api_key: str = "", base_url: str = ""):
        kwargs: dict[str, Any] = {}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        self.client = anthropic.AsyncAnthropic(**kwargs)

    # ------------------------------------------------------------------ format

    @staticmethod
    def to_wire(messages: list[Message], model: str) -> tuple[str, list[dict[str, Any]]]:
        system_parts: list[str] = []
        out: list[dict[str, Any]] = []

        def push(role: str, blocks: list[dict[str, Any]]) -> None:
            if out and out[-1]["role"] == role:
                out[-1]["content"].extend(blocks)
            else:
                out.append({"role": role, "content": list(blocks)})

        for m in messages:
            role = m["role"]
            if role == "system":
                system_parts.append(m.get("content", ""))
            elif role == "user":
                push("user", [{"type": "text", "text": m.get("content") or " "}])
            elif role == "tool":
                content: Any = m.get("content") or "(no output)"
                if m.get("images"):
                    content = [{"type": "text", "text": content}] + [
                        {"type": "image", "source": {"type": "base64", "media_type": i["media_type"], "data": i["data"]}}
                        for i in m["images"]
                    ]
                block: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": m["tool_call_id"],
                    "content": content,
                }
                if m.get("is_error"):
                    block["is_error"] = True
                push("user", [block])
            elif role == "assistant":
                raw = (m.get("raw") or {}).get("anthropic")
                if raw and raw.get("model") == model:
                    blocks = [b for b in raw["content"] if b.get("type") in _REPLAYABLE_BLOCKS]
                else:
                    blocks = []
                    if m.get("content"):
                        blocks.append({"type": "text", "text": m["content"]})
                    for c in m.get("tool_calls") or []:
                        blocks.append({"type": "tool_use", "id": c["id"], "name": c["name"], "input": c.get("arguments") or {}})
                if blocks:
                    push("assistant", blocks)
        return "\n\n".join(p for p in system_parts if p), out

    @staticmethod
    def tools_to_wire(tools: list[ToolSpec]) -> list[dict[str, Any]]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.parameters,
                # Large inputs (file contents, documents) stream as generated.
                "eager_input_streaming": True,
            }
            for t in tools
        ]

    # ------------------------------------------------------------------ calls

    def _request(self, model: str, messages: list[Message], tools: list[ToolSpec], max_tokens: int, effort: str | None):
        system, wire = self.to_wire(messages, model)
        params: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "messages": wire}
        if system:
            params["system"] = system
        if tools:
            params["tools"] = self.tools_to_wire(tools)
        betas: list[str] = []
        extra_body: dict[str, Any] = {}
        if _supports_adaptive(model):
            params["thinking"] = {"type": "adaptive", "display": "summarized"}
            if effort:
                params["output_config"] = {"effort": effort}
        if _supports_server_fallback(model):
            betas.append("server-side-fallback-2026-07-01")
            extra_body["fallbacks"] = "default"
        if betas:
            params["betas"] = betas
        if extra_body:
            params["extra_body"] = extra_body
        return params

    async def stream(
        self,
        model: str,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        max_tokens: int,
        effort: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        params = self._request(model, messages, tools, max_tokens, effort)
        try:
            async with self.client.beta.messages.stream(**params) as stream:
                async for event in stream:
                    if event.type == "content_block_delta":
                        delta = event.delta
                        if delta.type == "text_delta":
                            yield {"type": "text", "text": delta.text}
                        elif delta.type == "thinking_delta" and delta.thinking:
                            yield {"type": "thinking", "text": delta.thinking}
                final = await stream.get_final_message()
        except TypeError as exc:
            if "authentication" in str(exc):
                raise ProviderError(NO_KEY, status=401) from exc
            raise
        except ValueError as exc:
            # The SDK could not parse a streamed tool input at all.
            raise ProviderError(f"Malformed tool input from model: {exc}", retryable=True) from exc
        except anthropic.RateLimitError as exc:
            raise ProviderError(f"Rate limited: {exc.message}", status=429, retryable=True) from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(
                f"Anthropic API {exc.status_code}: {exc.message}",
                status=exc.status_code,
                retryable=exc.status_code >= 500,
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError(f"Could not reach the Anthropic API: {exc}", retryable=True) from exc

        stop_reason = final.stop_reason or "end_turn"
        blocks = [b.model_dump(exclude_none=True) for b in final.content]

        if stop_reason == "refusal":
            yield {"type": "text", "text": "\n\n_The model declined to continue this request._"}
        elif stop_reason != "max_tokens":
            # A truncated tool input parses as a valid partial object, so tool
            # calls only run when the turn ended normally.
            for b in final.content:
                if b.type != "tool_use":
                    continue
                args = b.input if isinstance(b.input, dict) else {}
                error = None if isinstance(b.input, dict) else "Tool input was not a JSON object."
                yield {"type": "tool_call", "id": b.id, "name": b.name, "arguments": args, "parse_error": error}

        usage = {
            "input_tokens": final.usage.input_tokens,
            "output_tokens": final.usage.output_tokens,
            "cache_read_tokens": getattr(final.usage, "cache_read_input_tokens", 0) or 0,
        }
        yield {
            "type": "done",
            "stop_reason": stop_reason,
            "usage": usage,
            "raw": {"anthropic": {"model": model, "content": blocks}},
        }

    async def complete(self, model: str, messages: list[Message], *, max_tokens: int = 4096) -> str:
        parts: list[str] = []
        async for ev in self.stream(model, messages, [], max_tokens=max_tokens, effort="low"):
            if ev["type"] == "text":
                parts.append(ev["text"])
        return "".join(parts)

    async def list_models(self) -> list[ModelInfo]:
        try:
            page = await self.client.models.list(limit=100)
        except TypeError as exc:
            if "authentication" in str(exc):
                raise ProviderError(NO_KEY, status=401) from exc
            raise
        except anthropic.APIStatusError as exc:
            raise ProviderError(f"Anthropic API {exc.status_code}: {exc.message}", status=exc.status_code) from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError(f"Could not reach the Anthropic API: {exc}", retryable=True) from exc
        out = []
        for m in page.data:
            out.append(ModelInfo(id=m.id, tools=True, context=getattr(m, "max_input_tokens", None)))
        return out

